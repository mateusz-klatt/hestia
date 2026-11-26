"""Optional structured bank evidence, separate from canonical ledger payloads.

Enable Banking AccountIdentification/GenericIdentification and Transaction fields:
https://enablebanking.com/docs/api/reference/ . No identity is inferred from a
session UID, identification hash, description, party private ID or bank name.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
import unicodedata

MAX_IDENTITIES = 16
MAX_METADATA_BYTES = 8 * 1024 * 1024
# GenericIdentification also includes personal identifiers, masked numbers and
# OAuth tokens. They are not account evidence and must never enter this sidecar.
_ACCOUNT_SCHEMES = frozenset({"IBAN", "BBAN", "BGNR", "PGNR"})
_IBAN = re.compile(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}\Z", re.ASCII)
_MCC = re.compile(r"[0-9]{4}\Z", re.ASCII)
_FP = re.compile(r"[a-f0-9]{64}\Z", re.ASCII)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _iban(value):
    if not isinstance(value, str) or len(value) > 128 or not value.isascii():
        return None
    value = value.replace(" ", "").upper()
    if not _IBAN.fullmatch(value):
        return None
    digits = "".join(str(ord(char) - 55) if char.isalpha() else char for char in value[4:] + value[:4])
    return value if int(digits) % 97 == 1 else None


def _identity(value):
    if not isinstance(value, dict):
        return None
    scheme, identifier = value.get("scheme_name"), value.get("identification")
    if not isinstance(scheme, str) or scheme not in _ACCOUNT_SCHEMES:
        return None
    if scheme == "IBAN":
        identifier = _iban(identifier)
    elif (not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 128
          or not identifier.isascii() or any(ord(char) < 32 or ord(char) == 127 for char in identifier)):
        return None
    else:
        identifier = identifier.strip()
    return None if identifier is None else {"scheme": scheme, "identification": identifier}


def _aliases(identity, country):
    if identity["scheme"] != "BBAN":
        return []
    value = identity["identification"]
    iban = _iban(value)
    if iban is None and country == "PL" and re.fullmatch(r"[0-9]{26}", value, re.ASCII):
        iban = _iban("PL" + value)
    return [] if iban is None else [{"scheme": "IBAN", "identification": iban}]


def _generic_list(raw, country=None):
    if raw is None:
        return [], 0
    if not isinstance(raw, list) or len(raw) > MAX_IDENTITIES:
        return [], 1
    values, rejected = [], 0
    for item in raw:
        identity = _identity(item)
        if identity is None:
            rejected += 1
        else:
            values.append(identity)
            values.extend(_aliases(identity, country))
    return values, rejected


def _account_id(raw, country=None):
    if raw is None:
        return [], 0
    if not isinstance(raw, dict):
        return [], 1
    values, rejected = [], 0
    if raw.get("iban") is not None:
        identity = _identity({"scheme_name": "IBAN", "identification": raw["iban"]})
        if identity is None:
            rejected += 1
        else:
            values.append(identity)
    if raw.get("other") is not None:
        identity = _identity(raw["other"])
        if identity is None:
            rejected += 1
        else:
            values.append(identity)
            values.extend(_aliases(identity, country))
    return values, rejected


def _unique(values):
    identities = sorted({(item["scheme"], item["identification"]) for item in values})
    return (() if len(identities) > MAX_IDENTITIES else
            tuple({"scheme": scheme, "identification": identifier} for scheme, identifier in identities))


def _account_patch(raw, country=None):
    if raw is None:
        return (), 0
    if not isinstance(raw, dict):
        return (), 1
    values, rejected = _account_id(raw.get("account_id"), country)
    additional, bad = _generic_list(raw.get("all_account_ids"), country)
    unique = _unique(values + additional)
    return unique, rejected + bad + int(bool(values + additional) and not unique)


def normalize_account_identity(raw, *, country=None):
    """Return actual structured account numbers, never hashes or transient UIDs."""
    return _account_patch(raw, country)[0]


def _name(raw):
    if not isinstance(raw, dict) or raw.get("name") is None:
        return None, int(raw is not None and not isinstance(raw, dict))
    value = raw["name"]
    if not isinstance(value, str) or len(value) > 1024:
        return None, 1
    value = " ".join("".join(" " if char.isspace() else char for char in unicodedata.normalize("NFC", value)
                             if unicodedata.category(char) not in ("Cc", "Cf") or char.isspace()).split())
    return (value, 0) if 0 < len(value) <= 256 else (None, int(bool(value)))


def exchange_evidence(raw):
    """Allowlisted Enable Banking FX fields, excluding contract identifiers."""
    if not isinstance(raw, dict):
        return {}
    fields = {}
    for key, data, amount_key, currency_key in (
        ("instructed_amount", raw.get("instructed_amount"), "amount", "currency"),
        ("rate", raw, "exchange_rate", "unit_currency"),
    ):
        if not isinstance(data, dict):
            continue
        amount, currency = data.get(amount_key), data.get(currency_key)
        if (isinstance(amount, str) and re.fullmatch(r"[0-9]{1,16}(?:\.[0-9]{1,12})?", amount)
                and amount.strip("0.") and isinstance(currency, str) and re.fullmatch("[A-Z]{3}", currency)):
            if key == "instructed_amount":
                fields[key] = {"currency": currency, "amount": amount}
            else:
                fields.update(unit_currency=currency, exchange_rate=amount)
    return fields


def _transaction_patch(raw, country=None):
    if not isinstance(raw, dict):
        return {}, 1
    patch, rejected = {}, 0
    for party in ("creditor", "debtor"):
        name, bad = _name(raw.get(party))
        rejected += bad
        if name is not None:
            patch[party + "_name"] = name
        identities, bad = _account_id(raw.get(party + "_account"), country)
        additional, extra_bad = _generic_list(raw.get(party + "_account_additional_identification"), country)
        rejected += bad + extra_bad
        values = _unique(identities + additional)
        if values:
            patch[party + "_accounts"] = list(values)
        elif identities or additional:
            rejected += 1
    if exchange := exchange_evidence(raw.get("exchange_rate")):
        patch["exchange_rate"] = exchange
    mcc = raw.get("merchant_category_code")
    if mcc is not None:
        if isinstance(mcc, str) and _MCC.fullmatch(mcc):
            patch["mcc"] = mcc
        else:
            rejected += 1
    return patch, rejected


def normalize_transaction_metadata(raw, *, country=None):
    """Return names, account identifiers, FX evidence and MCC; invalid optional fields skip."""
    return _transaction_patch(raw, country)[0]


@dataclass(frozen=True)
class TransactionMetadata:
    entry_reference: str
    ledger_fingerprint: str
    payload: dict


@dataclass(frozen=True)
class BankMetadata:
    account_identities: tuple[dict, ...] = ()
    transactions: tuple[TransactionMetadata, ...] = ()
    digest: str = ""
    rejected: int = 0


class MetadataCollector:
    """Separate bounded sidecar budget; metadata overflow never rejects core money."""
    def __init__(self, account=None, *, country=None):
        self.country = country
        self.identities, self.rejected = _account_patch(account, country)
        self.rows, self.ambiguous, self.size = {}, {}, len(_json(self.identities))

    def add(self, raw, transaction):
        from .analytics_core import financial_fingerprint
        patch, bad = _transaction_patch(raw, self.country)
        self.rejected += bad
        ref = transaction.get("entry_reference")
        if not patch or not isinstance(ref, str) or not ref or len(ref) > 1024:
            return
        fp = financial_fingerprint(transaction)
        key = (ref, fp)
        merged = dict(self.rows.get(key, {}))
        conflicts = set(self.ambiguous.get(key, ()))
        for field, value in patch.items():
            if field in conflicts:
                continue
            if field in merged and merged[field] != value:
                del merged[field]
                conflicts.add(field)
                self.rejected += 1
            else:
                merged[field] = value
        increase = len(_json((ref, fp, merged))) - (len(_json((ref, fp, self.rows[key]))) if key in self.rows else 0)
        if self.size + increase > MAX_METADATA_BYTES:
            self.rejected += 1
            return
        self.rows[key], self.ambiguous[key], self.size = merged, conflicts, self.size + increase

    def finish(self, *, context=None):
        if not self.identities and not self.rows and not self.rejected:
            return None
        rows = tuple(TransactionMetadata(ref, fp, patch) for (ref, fp), patch in sorted(self.rows.items()) if patch)
        digest = hashlib.sha256(_json({"context": context, "account": self.identities,
                         "transactions": [(row.entry_reference, row.ledger_fingerprint, row.payload) for row in rows]}).encode()).hexdigest()
        return BankMetadata(self.identities, rows, digest, self.rejected)


def parse_bank_metadata(raw, transactions, *, context=None, collector=None, country=None):
    """Parse a separate import sidecar, bound to unique core entries in that file."""
    country = context.get("country") if country is None and isinstance(context, dict) else country
    collector = MetadataCollector(country=country) if collector is None else collector
    if not isinstance(raw, dict):
        collector.rejected += 1
        return collector.finish(context=context)
    collector.rejected += len(set(raw) - {"account", "transactions"})
    identities, bad = _account_patch(raw.get("account"), collector.country)
    combined = (*collector.identities, *identities)
    collector.identities = _unique(combined)
    collector.rejected += bad + int(bool(combined) and not collector.identities)
    by_ref = {}
    for row in transactions:
        ref = row["entry_reference"]
        by_ref[ref] = row if ref not in by_ref else None
    rows = raw.get("transactions", [])
    if not isinstance(rows, list) or len(rows) > 100_000:
        collector.rejected += 1
    else:
        for item in rows:
            ref = item.get("entry_reference") if isinstance(item, dict) else None
            core = by_ref.get(ref) if isinstance(ref, str) else None
            if core is None:
                collector.rejected += 1
            else:
                collector.add(item, core)
    return collector.finish(context=context)


def _owned(session, account_id):
    from ..db import BudgetAccount, BudgetConnection
    if not isinstance(account_id, str):
        return None
    account = session.get(BudgetAccount, account_id)
    connection = None if account is None else session.get(BudgetConnection, account.connection_id)
    return account if connection is not None and account.owner == connection.owner else None


def _observation(source, observed_at):
    try:
        return source in ("api", "offline_import") and type(observed_at) in (int, float) and math.isfinite(observed_at) and observed_at >= 0
    except OverflowError:
        return False


def _outdated(row, source, observed_at):
    return row is not None and (observed_at < row.observed_at or row.source == "api" and source != "api")


def _identities_patch(patch):
    if not isinstance(patch, (tuple, list)) or len(patch) > MAX_IDENTITIES:
        return None
    values = []
    for item in patch:
        if not isinstance(item, dict) or set(item) != {"scheme", "identification"}:
            return None
        value = _identity({"scheme_name": item["scheme"], "identification": item["identification"]})
        if value != item:
            return None
        values.append(value)
    return _unique(values)


def _payload_patch(patch):
    if not isinstance(patch, dict) or set(patch) - {"creditor_name", "debtor_name", "creditor_accounts", "debtor_accounts", "mcc", "exchange_rate"}:
        return None
    result = {}
    for key, value in patch.items():
        if key.endswith("_accounts"):
            normalized = _identities_patch(value)
            if not normalized:
                return None
            result[key] = list(normalized)
        elif key == "exchange_rate":
            if not value or exchange_evidence(value) != value:
                return None
            result[key] = value
        elif key == "mcc":
            if not isinstance(value, str) or not _MCC.fullmatch(value):
                return None
            result[key] = value
        else:
            normalized, bad = _name({"name": value})
            if normalized is None or bad or normalized != value:
                return None
            result[key] = value
    return result


def apply_account_identity(session, account_id, patch, source, observed_at):
    from ..db import BudgetAccountIdentity
    account = _owned(session, account_id)
    if account is None or not _observation(source, observed_at):
        return "invalid"
    patch = _identities_patch(patch)
    if patch is None:
        return "invalid"
    if not patch:
        return "empty"
    row = session.get(BudgetAccountIdentity, account_id)
    if row is not None and row.owner != account.owner:
        return "invalid"
    if _outdated(row, source, observed_at):
        return "older"
    value = _json(patch)
    if row is not None and row.identities_json == value:
        row.source, row.observed_at = source, max(row.observed_at, observed_at)
        return "unchanged"
    if row is None:
        session.add(BudgetAccountIdentity(account_id=account_id, owner=account.owner, identities_json=value,
                                         source=source, observed_at=observed_at, version=1))
    else:
        row.identities_json, row.source, row.observed_at = value, source, observed_at
        row.version += 1
    session.flush()
    return "updated"


def _merge_fields(row, ledger_fp, patch, source, observed_at):
    same = row is not None and row.ledger_fingerprint == ledger_fp
    retained = json.loads(row.payload_json) if same else {}
    provenance = json.loads(row.provenance_json) if same else {}
    accepted = False
    for key, value in patch.items():
        prior = provenance.get(key, {"source": row.source, "observed_at": row.observed_at}) if same else None
        if key in retained and (prior["source"] == "api" and source != "api" or observed_at < prior["observed_at"]
                or observed_at == prior["observed_at"] and prior["source"] == source and retained[key] != value):
            continue
        retained[key], provenance[key] = value, {"source": source, "observed_at": observed_at}
        accepted = True
    return retained, provenance, accepted, same


def apply_transaction_metadata(session, account_id, ref, ledger_fp, patch, source, observed_at):
    from ..db import BudgetTransaction, BudgetTransactionMetadata
    from .analytics_core import financial_fingerprint
    account = _owned(session, account_id)
    if account is None or not _observation(source, observed_at) or not isinstance(ledger_fp, str) or not _FP.fullmatch(ledger_fp):
        return "invalid"
    canonical = session.get(BudgetTransaction, (account_id, ref))
    if canonical is None or canonical.uncertain or financial_fingerprint(json.loads(canonical.payload_json)) != ledger_fp:
        return "mismatch"
    patch = _payload_patch(patch)
    if patch is None:
        return "invalid"
    if not patch:
        return "empty"
    row = session.get(BudgetTransactionMetadata, (account_id, ref))
    if row is not None and row.owner != account.owner:
        return "invalid"
    retained, provenance, accepted, same = _merge_fields(row, ledger_fp, patch, source, observed_at)
    if not accepted:
        return "older"
    value = _json(retained)
    if same and row.payload_json == value:
        row.provenance_json = _json(provenance)
        if observed_at >= row.observed_at:
            row.source, row.observed_at = source, observed_at
        return "unchanged"
    if row is None:
        session.add(BudgetTransactionMetadata(account_id=account_id, entry_reference=ref, owner=account.owner,
                    ledger_fingerprint=ledger_fp, payload_json=value, source=source, observed_at=observed_at,
                    provenance_json=_json(provenance), version=1))
    else:
        row.ledger_fingerprint, row.payload_json, row.provenance_json = ledger_fp, value, _json(provenance)
        if not same or observed_at >= row.observed_at:
            row.source, row.observed_at = source, observed_at
        row.version += 1
    session.flush()
    return "updated"


def apply_bank_metadata(session, account_id, metadata, *, source, observed_at):
    """Apply already-normalized observations only within the caller's transaction."""
    if metadata is None:
        return None
    counts = {"updated": 0, "unchanged": 0, "older": 0, "empty": 0, "mismatch": 0, "invalid": 0,
              "rejected": metadata.rejected}
    state = apply_account_identity(session, account_id, metadata.account_identities, source, observed_at)
    counts[state] += 1
    for row in metadata.transactions:
        state = apply_transaction_metadata(session, account_id, row.entry_reference, row.ledger_fingerprint,
                                           row.payload, source, observed_at)
        counts[state] += 1
    return counts
