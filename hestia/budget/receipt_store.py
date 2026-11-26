"""Owner-private receipt persistence, exact bank matching and retention sweeps."""
from __future__ import annotations

import base64
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import hmac
import json
import math
import os
import re
from types import SimpleNamespace
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import delete, exists, func, literal_column, or_, select
from sqlalchemy.exc import IntegrityError

from .. import authz
from ..db import (AppMeta, BudgetAccount as Account, BudgetConnection as Connection, BudgetReceipt as Receipt,
                  BudgetReceiptDeviceToken as DeviceToken, BudgetReceiptLine as Line,
                  BudgetReceiptLinkGroup as ReceiptLinkGroup, BudgetShop as Shop,
                  BudgetTransaction as Transaction, BudgetTransferLeg as Leg)
from .analytics_core import AnalyticsError, canonical_money, financial_fingerprint, negate_money, sum_money
from .device_token import DeviceTokenError, _issued_mask, _legacy_bearer_allowed, write_device_audit
from .gmail_config import normalize_card_accounts
from .gmail_payment import bank_payee_texts, bank_ref_tokens, canonical_gateway
from .gmail_store import (GATEWAY_REF_DOMAIN, _payment_payee_key, digest, receipt_ref_hmacs,
                          retain_imports, tombstone_receipt)
from .receipt_parser import (GATEWAY_PHRASE_TOKENS, _IBAN_MAX, _IBAN_MIN, _STOP_TOKENS,
                             CONFIRMED_TEXT_TTL_SECONDS, DRAFT_TTL_SECONDS, MAX_RECEIPT_LINES, ReceiptError,
                             _iban_checksum, chain_for, contains_gateway_marker, merchant_score_groups,
                             merchant_tokens, merchant_word_tokens, normalize_shop_name, parse_receipt,
                             redact_digit_field, valid_nip, validate_confirm, validate_confirm_line_totals)
from .receipt_reconcile import (audit_payload, entry_reference_digest, evidence_json,
                                erase_receipt_audit, link_evidence, resolved_party_names,
                                transaction_party_names, write_audit)
from .views import BudgetHTTPStore

MAX_DRAFTS = 20
MAX_ACTOR_DRAFTS = 3
OWNER_DRAFT_RESERVE = 5
MAX_CANDIDATES = 100
CURSOR_TTL_SECONDS = 900
MAX_CURSOR_CHARS = 8192
_CURSOR_DOMAIN = b"hestia/budget/receipts/cursor/v1"
_CANDIDATE_DOMAIN = b"hestia/budget/receipts/candidate/v1"
_TOKEN_INFO = b"hestia/budget/receipts/candidate-token/v1"
_EVIDENCE_INFO = b"hestia/budget/receipts/evidence-mac/v1"
_TOKEN_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
_TOKEN_LIMIT = 4096
_MAX_ROW_ID = 2**63 - 1
_ROW_ID = literal_column("budget_transactions.rowid")
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z", re.ASCII)
_MONTH = re.compile(r"[0-9]{4}-[0-9]{2}\Z", re.ASCII)
_SUMMARY_FIELDS = (
    "receipt_id", "shop_id", "chain_key", "raw_shop_name", "raw_nip", "receipt_kind", "receipt_date",
    "receipt_time", "total_amount", "currency", "payment_method", "status", "source_kind", "linked",
    "linked_account_id", "linked_entry_ref", "version", "created_at", "updated_at", "image_available",
    "image_expires_at", "raw_text_expires_at", "math_delta", "document_kind", "invoice_number", "due_date",
    "import_expires_at", "forwarded_manually", "forwarded_sender_domain",
    "gateway", "gateway_status", "fee_amount", "payee_host", "evidence_for_receipt_id", "has_document",
    "card_last4",
)


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def constant(_value):
        raise ValueError

    return json.loads(raw, object_pairs_hook=unique, parse_constant=constant)


def _valid_date(value):
    try:
        return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _shift_clamped(day, days):
    if days >= 0:
        return date.max if (date.max - day).days < days else day + timedelta(days=days)
    return date.min if (day - date.min).days < -days else day + timedelta(days=days)


def _date_distance(payload, receipt_date, *, document_kind=None, due_date=None):
    if not _valid_date(receipt_date):
        return None
    target = date.fromisoformat(receipt_date)
    if document_kind == "invoice":
        due = date.fromisoformat(due_date) if _valid_date(due_date) else target
        start = _shift_clamped(target, -1)
        end = _shift_clamped(max(due, target), 14)
    else:
        start = _shift_clamped(target, -4)
        end = _shift_clamped(target, 4)
    distances = [abs((tx_date - target).days)
                 for value in (payload.get("transaction_date"), payload.get("booking_date"), payload.get("value_date"))
                 if _valid_date(value) and start <= (tx_date := date.fromisoformat(value)) <= end]
    return min(distances) if distances else None


_TX_DIGIT_RUN_RE = re.compile(r"[0-9]+(?:[\s/.\-\u2010-\u2015]+[0-9]+)*")
_TOKEN_SPAN_RE = re.compile(r"[A-Za-z0-9]+")
_IBAN_PREFIX_RE = re.compile(r"[A-Za-z]{2}[0-9]{2}")
_EXCLUDED_NIP_PREFIX_RE = re.compile(
    r"(?<![a-z0-9])(?:nr|faktura|fv|invoice|no)(?![a-z0-9])[\s:/.\-#\u2010-\u2015]*\Z",
    re.IGNORECASE | re.ASCII,
)
_STRICT_NIP_LABEL_RE = re.compile(
    r"(?:(?<![a-z0-9])/nip/"
    r"|(?<![a-z0-9])(?:nip(?:[\s:/.\-]+pl)?|vat(?:[\s:/.\-]+pl|[\s-]+id)|tax\s+id)(?![a-z0-9]))"
    r"[\s:/.\-\u2010-\u2015]*\Z",
    re.IGNORECASE | re.ASCII,
)
_GATEWAY_DOMAINS = frozenset({
    "przelewy24.pl", "p24.pl", "paypro.pl", "autopay.pl", "bluemedia.pl", "tpay.com",
    "paynow.pl", "dotpay.pl", "stripe.com", "paypal.com", "paypal.pl", "adyen.com", "klarna.com",
    "eservice.com.pl", "polcard.pl", "ecard.pl", "cashbill.pl", "hotpay.pl", "imoje.pl",
})


def _is_gateway_domain(domain):
    if not isinstance(domain, str):
        return False
    cleaned = domain.strip().lower().rstrip(".")
    if "." not in cleaned:
        return False
    if any(cleaned == root or cleaned.endswith("." + root) for root in _GATEWAY_DOMAINS):
        return True
    labels = cleaned.split(".")
    return any(label in ("payu", "bluemedia") for label in labels[:-1])


def _is_gateway_receipt(session, row):
    if getattr(row, "gateway", None) is not None:
        return True
    row_id = getattr(row, "id", None)
    if row_id is not None and session.get(AppMeta, _payment_payee_key(row_id)) is not None:
        return True
    if contains_gateway_marker(getattr(row, "raw_shop_name", None)):
        return True
    return any(
        _is_gateway_domain(getattr(row, field, None))
        for field in ("forwarded_sender_domain", "sender_domain")
    )


def _payload_party_texts(payload):
    texts = []
    for key in ("creditor_name", "debtor_name"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            texts.append(value)
    for key in ("creditor", "debtor"):
        party = payload.get(key)
        if isinstance(party, dict) and isinstance(party.get("name"), str) and party["name"]:
            texts.append(party["name"])
    return texts


_IBAN_COUNTRY_LENGTHS = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BG": 22, "BH": 22, "BR": 29,
    "BY": 28, "CH": 21, "CR": 22, "CY": 28, "CZ": 24, "DE": 22, "DK": 18, "DO": 28, "EE": 20, "EG": 29,
    "ES": 24, "FI": 18, "FO": 18, "FR": 27, "GB": 22, "GE": 22, "GI": 23, "GL": 18, "GR": 27, "GT": 28,
    "HR": 21, "HU": 28, "IE": 22, "IL": 23, "IQ": 23, "IS": 26, "IT": 27, "JO": 30, "KW": 30, "KZ": 20,
    "LB": 28, "LC": 32, "LI": 21, "LT": 20, "LU": 20, "LV": 21, "MC": 27, "MD": 24, "ME": 22, "MK": 19,
    "MR": 27, "MT": 31, "MU": 30, "NL": 18, "NO": 15, "PK": 24, "PL": 28, "PS": 29, "PT": 25, "QA": 29,
    "RO": 24, "RS": 22, "SA": 24, "SC": 31, "SE": 24, "SI": 19, "SK": 24, "SM": 27, "ST": 25, "SV": 28,
    "TL": 23, "TN": 24, "TR": 26, "UA": 29, "VA": 22, "VG": 24, "XK": 20,
}


def _is_iban_candidate(compact):
    return _IBAN_MIN <= len(compact) <= _IBAN_MAX and (
        len(compact) == _IBAN_COUNTRY_LENGTHS.get(compact[:2]) or _iban_checksum(compact)
    )


def _mask_iban_sequences(haystack):
    """Replace real IBAN sequences (15..34 chars matching country length or mod-97) with spaces.

    ``PL`` followed by exactly 10 digits is a Polish VAT ID, never an IBAN.
    """
    spans = list(_TOKEN_SPAN_RE.finditer(haystack))
    if not spans:
        return haystack
    chars = list(haystack)
    index = 0
    total = len(spans)
    while index < total:
        token = spans[index].group(0)
        start_pos = spans[index].start()
        if (start_pos > 0 and haystack[start_pos - 1] == "_") or _IBAN_PREFIX_RE.match(token) is None:
            index += 1
            continue
        if len(token) == 12 and token[:2].lower() == "pl" and token[2:].isdigit():
            index += 1
            continue
        compact = token.upper()
        saw_digits = any(ch.isdigit() for ch in compact[4:])
        matched_last = index if saw_digits and _is_iban_candidate(compact) else None
        last = index
        cursor = index + 1
        while cursor < total:
            gap = haystack[spans[last].end():spans[cursor].start()]
            if not gap or not all(ch in " -" for ch in gap):
                break
            nxt = spans[cursor].group(0)
            if len(compact) + len(nxt) > _IBAN_MAX:
                break
            if nxt.isalpha():
                if saw_digits or len(nxt) > 4:
                    break
            else:
                saw_digits = True
            compact += nxt.upper()
            last = cursor
            cursor += 1
            if saw_digits and _is_iban_candidate(compact):
                matched_last = last
        if matched_last is not None:
            for pos in range(spans[index].start(), spans[matched_last].end()):
                chars[pos] = " "
            index = matched_last + 1
            continue
        index += 1
    return "".join(chars)


def _matched_nip_prefix(text, match, nip):
    """Return ``(before_pl, start)`` if ``match`` is a boundary-valid non-doc hit for ``nip``."""
    start, end = match.span()
    before_pl = (
        start >= 2 and text[start - 2:start].lower() == "pl"
        and (start == 2 or not (text[start - 3].isalnum() or text[start - 3] == "_"))
    )
    if start > 0 and not before_pl and (text[start - 1].isalnum() or text[start - 1] == "_"):
        return None
    if end < len(text) and (text[end].isalnum() or text[end] == "_"):
        return None
    if re.sub(r"\D", "", match.group(0)) != nip:
        return None
    doc_prefix = text[:start - 2] if before_pl else text[:start]
    if _EXCLUDED_NIP_PREFIX_RE.search(doc_prefix) is not None:
        return None
    return before_pl, start


def _nip_match(nip, haystack):
    """Return ``(hit, labeled)`` extracting labeled NIPs before IBAN masking, then unlabeled after."""
    if not valid_nip(nip):
        return False, False
    for match in _TX_DIGIT_RUN_RE.finditer(haystack):
        matched = _matched_nip_prefix(haystack, match, nip)
        if matched is None:
            continue
        before_pl, start = matched
        if before_pl or _STRICT_NIP_LABEL_RE.search(haystack[:start]) is not None:
            return True, True
    masked = _mask_iban_sequences(haystack)
    for match in _TX_DIGIT_RUN_RE.finditer(masked):
        if _matched_nip_prefix(masked, match, nip) is not None:
            return True, False
    return False, False


def _nip_in_text(nip, haystack):
    return _nip_match(nip, haystack)[0]


def _subkey(material, info):
    """One HKDF-SHA256 key. A fresh instance, because derive() accepts one call."""
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(material)


def _line_dict(row):
    return {
        "line_no": row.line_no, "line_kind": row.line_kind, "raw_name": redact_digit_field(row.raw_name),
        "raw_name_key": redact_digit_field(row.raw_name_key), "ptu_code": row.ptu_code,
        "quantity": row.quantity, "raw_unit": redact_digit_field(row.raw_unit),
        "unit_price_raw": row.unit_price_raw,
        "gross_line_total": row.gross_line_total, "discount_amount": row.discount_amount,
        "net_line_total": row.net_line_total,
    }


_DIGEST_HEX = re.compile(r"[0-9a-f]{64}")


def _group_reservation(owner, account_id, entry_reference):
    # Both confirmed and needs_review groups reserve the transaction.
    return exists().where(
        ReceiptLinkGroup.owner == owner, ReceiptLinkGroup.account_id == account_id,
        ReceiptLinkGroup.entry_reference == entry_reference,
    )


def _fee_adjusted_total(total_amount, fee_amount):
    if fee_amount is None:
        return None
    total = Decimal(canonical_money(total_amount))
    fee = Decimal(canonical_money(fee_amount))
    if fee <= 0 or total <= fee:
        return None
    return canonical_money(format(total - fee, "f"))


_BANK_GATEWAY_PHRASE_WORDS = frozenset(word for phrase in GATEWAY_PHRASE_TOKENS for word in phrase)
_SECONDARY_BANK_FILLER_TOKENS = frozenset({
    "numer", "tytul", "tytulem", "data", "dnia", "kwota", "odbiorca", "nadawca",
    "identyfikator", "szczegoly", "rzecz", "reference", "transaction", "transakcji",
    "opt", "iu",
})
_SECONDARY_SHORT_FILLER = frozenset({
    "w", "z", "i", "u", "o", "do", "od", "na", "za", "po", "ze", "dla",
    "nr", "id", "pl", "sa", "eu", "bv", "ag", "ab", "oy", "as", "kft",
})


def _receipt_ref_targets(session, row) -> list[tuple[str, str, bool]]:
    targets: list[tuple[str, str, bool]] = []
    seen: set[tuple[str, str]] = set()
    gw_key = canonical_gateway(getattr(row, "gateway", None))
    if gw_key is not None:
        primary_hmac = getattr(row, "gateway_ref_hmac", None)
        for ref_hmac in sorted(receipt_ref_hmacs(session, row)):
            seen.add((gw_key, ref_hmac))
            targets.append((gw_key, ref_hmac, ref_hmac != primary_hmac))
    blik_hmac = getattr(row, "blik_ref_hmac", None)
    if isinstance(blik_hmac, str) and ("blik", blik_hmac) not in seen:
        targets.append(("blik", blik_hmac, False))
    return targets


def _secondary_payee_guard(row, counterparty, remit_texts, matched_ref_tokens) -> bool:
    chain = chain_for(row.raw_shop_name, row.raw_nip)
    groups, _ = merchant_score_groups(row.raw_shop_name, row.raw_nip)
    if not groups:
        return False
    description = " ".join(value for value in remit_texts if isinstance(value, str))
    bank_text = "\n".join(part for part in (description, counterparty) if part)
    evidence, remaining = bank_payee_texts(bank_text, matched_ref_tokens)
    bank_words = frozenset(merchant_word_tokens(evidence))
    distinctive_score = sum(1 for group in groups if any(alias.issubset(bank_words) for alias in group))
    chain_group_hit = chain is not None and any(alias.issubset(bank_words) for alias in groups[0])
    if not (chain_group_hit or distinctive_score == len(groups)):
        return False
    receipt_words = [
        tok for tok in merchant_word_tokens(row.raw_shop_name)
        if tok not in _STOP_TOKENS and not tok.isdigit()
    ]
    if chain is None and any(tok not in bank_words for tok in receipt_words):
        return False
    allowed = (
        set(merchant_tokens(row.raw_shop_name, row.raw_nip))
        | set(receipt_words)
        | _STOP_TOKENS
        | _BANK_GATEWAY_PHRASE_WORDS
        | _SECONDARY_BANK_FILLER_TOKENS
    )
    for tok in merchant_word_tokens(remaining):
        if tok.isdigit() or tok in allowed:
            continue
        if (len(tok) >= 4 and sum(1 for ch in tok if ch.isalpha()) >= 3) or (
            len(tok) < 4 and any(ch.isalpha() for ch in tok) and tok not in _SECONDARY_SHORT_FILLER
        ):
            return False
    return True


def _earliest_payload_date(raw_json):
    try:
        payload = _json(raw_json)
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    dates = [
        value for value in (payload.get("transaction_date"), payload.get("booking_date"), payload.get("value_date"))
        if _valid_date(value)
    ]
    return min(dates) if dates else None


class BudgetReceiptStore(BudgetHTTPStore):
    def __init__(self, sessions, *, clock, cursor_secret, device_id=None, actor=None,
                 ref_hmac_secret=None, card_accounts=None):
        super().__init__(sessions, clock=clock, cursor_secret=cursor_secret)
        self._device_id = device_id
        self._session_actor = actor
        self._actor = f"device:{device_id}" if device_id is not None else None
        self._match_allowed = False
        self._ref_hmac_secret = ref_hmac_secret
        self._card_accounts = (
            normalize_card_accounts(card_accounts, allow_tuples=True) if card_accounts is not None else {}
        )
        self._receipt_cursor_key = hashlib.sha256(_CURSOR_DOMAIN + b"\0" + cursor_secret).digest()
        material = hashlib.sha256(_CANDIDATE_DOMAIN + b"\0" + cursor_secret).digest()
        self._token_key = _subkey(material, _TOKEN_INFO)
        self._evidence_key = _subkey(material, _EVIDENCE_INFO)

    def _owner(self, session, owner, permission=None, *, require_same_target=False):
        token = None
        actor = self._session_actor or owner
        if self._device_id is not None:
            token = session.get(DeviceToken, self._device_id)
            if token is None or token.owner != owner:
                raise DeviceTokenError(401, "device_token_invalid")
            if token.revoked_at is not None:
                raise DeviceTokenError(401, "device_token_revoked")
            if self._now() >= min(token.expires_at, token.hard_expires_at):
                raise DeviceTokenError(401, "device_token_expired")
            actor = token.actor or token.owner
            target = owner
        elif authz.policy_phase(session) == authz.POLICY_PHASE_LEGACY:
            target = owner
        else:
            target = authz.resolve_budget_target(session, actor)
            if target is None or (require_same_target and target != owner):
                raise DeviceTokenError(403, "budget_forbidden")
        super()._owner(session, target)
        self._session_actor = actor
        phase = authz.policy_phase(session)
        if phase == authz.POLICY_PHASE_LEGACY:
            permissions = authz.LEGACY_RECEIPT_PERMISSIONS
        elif token is not None and token.issued_permissions is None:
            if not _legacy_bearer_allowed(session, token):
                raise DeviceTokenError(403, "budget_forbidden")
            permissions = authz.LEGACY_RECEIPT_PERMISSIONS
        else:
            live = authz.effective_permissions(session, actor, owner=target)
            permissions = live
            if token is not None:
                permissions = _issued_mask(token) & live & authz.DEVICE_ELIGIBLE_PERMISSIONS
                if not permissions:
                    token.revoked_at = self._now()
                    raise DeviceTokenError(403, "budget_forbidden")
                if permissions != _issued_mask(token):
                    token.issued_permissions = json.dumps(sorted(permissions))
        if permission is not None and permission not in permissions:
            raise DeviceTokenError(403, "budget_forbidden")
        self._permissions = permissions
        self._match_allowed = "budget.receipts.match" in permissions
        self._actor = f"device:{self._device_id}" if self._device_id is not None else (
            actor if actor != target else None
        )
        return target

    def _now(self):
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise ReceiptError("budget_receipt_unavailable", 503)
        return now

    @staticmethod
    def _owned(session, owner, receipt_id):
        if not isinstance(receipt_id, str) or not _UUID.fullmatch(receipt_id):
            raise ReceiptError("budget_receipt_not_found", 404)
        row = session.get(Receipt, receipt_id)
        if row is None or row.owner != owner:
            raise ReceiptError("budget_receipt_not_found", 404)
        return row

    @staticmethod
    def _shop_id(session, owner, parsed):
        nip, chain = parsed.get("raw_nip"), parsed.get("chain_key")
        if nip is not None:
            row = session.scalar(select(Shop).where(Shop.owner == owner, Shop.nip == nip).order_by(Shop.id).limit(1))
            if row is not None:
                return row.id
        if chain is not None:
            row = session.scalar(select(Shop).where(Shop.owner == owner, Shop.chain_key == chain)
                                 .order_by(Shop.id).limit(1))
            if row is not None:
                return row.id
        return None

    @staticmethod
    def _math_delta(session, row):
        if row.total_amount is None or row.status == "imported":
            return None
        net = sum_money(session.scalars(select(Line.net_line_total).where(
            Line.receipt_id == row.id, Line.owner == row.owner)))
        return sum_money((net, negate_money(row.total_amount)))

    def _summary(self, session, row):
        shop = None if row.shop_id is None else session.get(Shop, row.shop_id)
        values = {
            "receipt_id": row.id, "shop_id": row.shop_id, "chain_key": None if shop is None else shop.chain_key,
            "raw_shop_name": redact_digit_field(row.raw_shop_name), "raw_nip": row.raw_nip,
            "receipt_kind": row.receipt_kind,
            "receipt_date": row.receipt_date, "receipt_time": row.receipt_time, "total_amount": row.total_amount,
            "currency": row.currency, "payment_method": row.payment_method, "status": row.status,
            "source_kind": row.source_kind, "linked": row.linked_account_id is not None,
            "linked_account_id": row.linked_account_id if self._match_allowed else None,
            "linked_entry_ref": entry_reference_digest(row.linked_entry_ref) if self._match_allowed else None,
            "version": row.version, "created_at": row.created_at, "updated_at": row.updated_at,
            "image_available": row.image_relpath is not None, "image_expires_at": row.image_expires_at,
            "raw_text_expires_at": row.raw_text_expires_at, "math_delta": self._math_delta(session, row),
            "document_kind": row.document_kind, "invoice_number": redact_digit_field(row.invoice_number),
            "due_date": row.due_date, "import_expires_at": row.import_expires_at,
            "forwarded_manually": bool(row.forwarded_manually),
            "forwarded_sender_domain": row.forwarded_sender_domain,
            "gateway": row.gateway, "gateway_status": row.gateway_status, "fee_amount": row.fee_amount,
            "payee_host": row.payee_host, "evidence_for_receipt_id": row.evidence_for_receipt_id,
            "has_document": row.gateway is None or row.evidence_for_receipt_id is not None,
            "card_last4": row.card_last4,
        }
        return {key: values[key] for key in _SUMMARY_FIELDS}

    def _detail(self, session, row):
        lines = session.scalars(select(Line).where(Line.receipt_id == row.id, Line.owner == row.owner)
                                .order_by(Line.line_no))
        evidence = session.scalars(select(Receipt).where(
            Receipt.owner == row.owner, Receipt.evidence_for_receipt_id == row.id,
        ).order_by(Receipt.receipt_date, Receipt.id))
        payment_evidence = [{
            "receipt_id": item.id, "gateway": item.gateway, "gateway_status": item.gateway_status,
            "total_amount": item.total_amount, "fee_amount": item.fee_amount, "receipt_date": item.receipt_date,
        } for item in evidence]
        return {**self._summary(session, row), "lines": [_line_dict(line) for line in lines],
                "payment_evidence": payment_evidence}

    def _purge(self, session, owner, now):
        conditions = [Receipt.status == "draft",
                      or_(Receipt.raw_text_expires_at <= now, Receipt.created_at <= now - DRAFT_TTL_SECONDS)]
        query = select(Receipt).where(*conditions)
        if owner is not None:
            query = query.where(Receipt.owner == owner)
        drafts = list(session.scalars(query))
        draft_count = 0
        for row in drafts:
            session.execute(delete(Line).where(Line.receipt_id == row.id, Line.owner == row.owner))
            erase_receipt_audit(session, row.owner, row.id)
            session.delete(row)
            draft_count += 1
        query = select(Receipt).where(
            Receipt.status.in_(("confirmed", "needs_review")), Receipt.raw_ocr_text.is_not(None),
            Receipt.raw_text_expires_at.is_not(None), Receipt.raw_text_expires_at <= now,
        )
        if owner is not None:
            query = query.where(Receipt.owner == owner)
        text_count = 0
        for row in session.scalars(query):
            before = audit_payload(row, "text_purge")
            row.raw_ocr_text, row.raw_text_expires_at = None, None
            row.image_relpath, row.image_expires_at = None, None
            write_audit(session, row.owner, row, "text_purge", before, audit_payload(row, "text_purge"), now)
            text_count += 1
        return {"drafts_purged": draft_count, "raw_texts_purged": text_count}

    def purge_expired(self, owner=None):
        with self.transaction() as session:
            if owner is not None:
                owner = self._owner(session, owner)
            now = self._now()
            retain_imports(session, owner, now)
            return self._purge(session, owner, now)

    def parse_text(self, owner, text, *, source_kind="paste"):
        stored_kind = {"paste": "live_text", "local_ocr": "local_ocr"}.get(source_kind)
        if stored_kind is None:
            raise ReceiptError()
        parsed = parse_receipt(text)
        with self.sessions() as session:
            owner = self._owner(session, owner, "budget.receipts.add")
        candidates, no_bank_data = self._read_candidates(owner, parsed)
        with self.transaction() as session:
            self._owner(session, owner, "budget.receipts.add", require_same_target=True)
            now = self._now()
            self._purge(session, owner, now)
            drafts = session.scalar(select(func.count()).select_from(Receipt).where(
                Receipt.owner == owner, Receipt.status == "draft"))
            actor_drafts = session.scalar(select(func.count()).select_from(Receipt).where(
                Receipt.owner == owner, Receipt.status == "draft",
                Receipt.submitted_by == self._session_actor,
            ))
            if (
                drafts >= MAX_DRAFTS
                or (self._session_actor != owner and actor_drafts >= MAX_ACTOR_DRAFTS)
                or (self._session_actor != owner and drafts >= MAX_DRAFTS - OWNER_DRAFT_RESERVE)
            ):
                raise ReceiptError("budget_receipt_quota_exceeded", 413)
            identifier = str(uuid.uuid4())
            row = Receipt(
                id=identifier, owner=owner, submitted_by=self._session_actor,
                shop_id=self._shop_id(session, owner, parsed),
                raw_shop_name=parsed["raw_shop_name"], raw_nip=parsed["raw_nip"],
                receipt_kind=parsed["receipt_kind"], receipt_date=parsed["receipt_date"],
                receipt_time=parsed["receipt_time"], total_amount=parsed["total_amount"],
                currency=parsed["currency"], payment_method=parsed["payment_method"], status="draft",
                source_kind=stored_kind, image_relpath=None, image_expires_at=None,
                raw_ocr_text=None, raw_text_expires_at=None,
                linked_account_id=None, linked_entry_ref=None, linked_fingerprint=None, version=1,
                created_at=now, updated_at=now,
            )
            session.add(row)
            session.flush()
            for values in parsed["lines"]:
                session.add(Line(receipt_id=identifier, owner=owner, **{
                    key: value for key, value in values.items() if key != "line_no"
                }, line_no=values["line_no"]))
            session.flush()
            write_audit(session, owner, row, "create", None, audit_payload(row, "create", self._actor), now)
            detail = self._detail(session, row)
        return {
            "receipt_id": identifier, "draft": detail,
            "candidates": candidates if self._match_allowed else [], "version": 1,
            **({"no_bank_data": no_bank_data} if self._match_allowed else {}),
        }

    def _read_candidates(self, owner, parsed):
        """Match in a deferred read snapshot so the scan does not hold the writer lock.

        ``confirm`` re-checks the chosen link inside ``BEGIN IMMEDIATE``.
        """
        if not self._match_allowed:
            return [], False
        preview = SimpleNamespace(
            total_amount=parsed["total_amount"], currency=parsed["currency"],
            receipt_date=parsed["receipt_date"], payment_method=parsed["payment_method"],
            receipt_kind=parsed["receipt_kind"], raw_shop_name=parsed["raw_shop_name"],
            raw_nip=parsed["raw_nip"], document_kind=parsed.get("document_kind"),
            due_date=parsed.get("due_date"),
        )
        with self._read(owner, permission=None) as session:
            self._owner(session, owner, "budget.receipts.add", require_same_target=True)
            if not self._match_allowed:
                return [], False
            candidates = self._candidates(session, owner, preview, parsed)
            no_bank_data = False if candidates else self._no_bank_data(session, owner, preview)
            return candidates, no_bank_data

    def _scope_accounts(self, session, owner, row) -> tuple[set[str], bool]:
        enabled = set(session.scalars(select(Account.id).join(Connection).where(
            Account.owner == owner, Connection.owner == owner, Account.enabled.is_(True),
        )).all())
        card_last4 = getattr(row, "card_last4", None)
        if isinstance(card_last4, str) and card_last4 in self._card_accounts:
            return enabled & set(self._card_accounts[card_last4]), True
        return enabled, False

    @staticmethod
    def _is_matchable(row) -> bool:
        return not (
            row.total_amount is None or row.currency is None or not _valid_date(row.receipt_date)
            or row.payment_method in ("cash", "mixed")
            or getattr(row, "evidence_for_receipt_id", None) is not None
        )

    def _no_bank_data(self, session, owner, row) -> bool:
        if not self._is_matchable(row):
            return False
        scope_accounts, card_filtered = self._scope_accounts(session, owner, row)
        if not scope_accounts:
            return False
        raw_payloads = session.scalars(select(Transaction.payload_json).where(
            Transaction.account_id.in_(scope_accounts),
        )).all()
        earliest = None
        for raw_json in raw_payloads:
            found = _earliest_payload_date(raw_json)
            if found is not None and (earliest is None or found < earliest):
                earliest = found
        if earliest is None:
            return card_filtered
        return row.receipt_date < earliest

    def _unique_id_matches(self, session, owner, row) -> set[tuple[str, str]]:
        targets = _receipt_ref_targets(session, row)
        if self._ref_hmac_secret is None or not targets or not _valid_date(getattr(row, "receipt_date", None)):
            return set()
        transfer_leg = exists().where(
            Leg.account_id == Transaction.account_id, Leg.entry_reference == Transaction.entry_reference,
            Leg.account_id == Account.id, Account.owner == owner,
        )
        # Receipt-linked and grouped transactions still count when testing ID uniqueness.
        tx_rows = session.scalars(select(Transaction).join(Account).join(Connection).where(
            Account.owner == owner, Connection.owner == owner, Account.enabled.is_(True),
            Transaction.uncertain.is_(False), ~transfer_leg,
        )).all()
        matches_by_target: dict[tuple[str, str, bool], set[tuple[str, str]]] = {target: set() for target in targets}
        tx_context: dict[tuple[str, str], tuple[str, list[str]]] = {}
        matched_sec_tokens: dict[tuple[str, str], set[str]] = {}
        doc_kind = getattr(row, "document_kind", None)
        due = getattr(row, "due_date", None)
        counterparty_is_debtor = getattr(row, "receipt_kind", None) == "refund"
        for tx in tx_rows:
            try:
                payload = _json(tx.payload_json)
                if not isinstance(payload, dict) or payload.get("status") != "BOOK":
                    continue
                if _date_distance(payload, row.receipt_date, document_kind=doc_kind, due_date=due) is None:
                    continue
                fingerprint = financial_fingerprint(payload)
                creditor, debtor = resolved_party_names(
                    session, owner, tx.account_id, tx.entry_reference, payload, fingerprint,
                )
            except (AnalyticsError, ValueError, TypeError, KeyError, RecursionError):
                continue
            remittance = payload.get("remittance_information")
            remit_texts = [value for value in remittance if isinstance(value, str)] if isinstance(remittance, list) else []
            texts = [t for t in (creditor, debtor, *remit_texts, *_payload_party_texts(payload)) if isinstance(t, str) and t]
            combined_text = "\n".join(texts)
            tx_key = (tx.account_id, tx.entry_reference)
            counterparty = debtor if counterparty_is_debtor else creditor
            for channel, target_hmac, is_secondary in targets:
                for tok in bank_ref_tokens(combined_text, channel):
                    if hmac.compare_digest(digest(self._ref_hmac_secret, GATEWAY_REF_DOMAIN, tok), target_hmac):
                        matches_by_target[(channel, target_hmac, is_secondary)].add(tx_key)
                        if is_secondary:
                            tx_context[tx_key] = (counterparty, remit_texts)
                            matched_sec_tokens.setdefault(tx_key, set()).add(tok)
                        break
        unique_primary: set[tuple[str, str]] = set()
        unique_secondary: set[tuple[str, str]] = set()
        for (_ch, _hmac, is_secondary), tx_keys in matches_by_target.items():
            if len(tx_keys) == 1:
                (unique_secondary if is_secondary else unique_primary).update(tx_keys)
        for tx_key in unique_secondary - unique_primary:
            counterparty, remit_texts = tx_context[tx_key]
            if _secondary_payee_guard(row, counterparty, remit_texts, matched_sec_tokens[tx_key]):
                unique_primary.add(tx_key)
        return unique_primary

    @staticmethod
    def _candidate_rows(session, owner, *, include_receipt_id=None):
        # Confirmed and needs_review rows reserve a transaction. Re-issuing for one
        # needs_review receipt must still see that receipt's own reservation.
        held = [
            Receipt.owner == owner,
            Receipt.linked_account_id == Transaction.account_id,
            Receipt.linked_entry_ref == Transaction.entry_reference,
            Receipt.status.in_(("confirmed", "needs_review")),
        ]
        if include_receipt_id is not None:
            held.append(Receipt.id != include_receipt_id)
        active_link = exists().where(*held)
        transfer_leg = exists().where(
            Leg.account_id == Transaction.account_id, Leg.entry_reference == Transaction.entry_reference,
            Leg.account_id == Account.id, Account.owner == owner,
        )
        grouped = _group_reservation(owner, Transaction.account_id, Transaction.entry_reference)
        return session.execute(select(Transaction, _ROW_ID).join(Account).join(Connection).where(
            Account.owner == owner, Connection.owner == owner, Account.enabled.is_(True),
            Transaction.uncertain.is_(False), ~active_link, ~transfer_leg, ~grouped,
        )).all()

    def _candidates(self, session, owner, row, parsed, *, include_receipt_id=None):
        if (row.total_amount is None or row.currency is None or row.receipt_date is None
                or row.payment_method in ("cash", "mixed")
                or getattr(row, "evidence_for_receipt_id", None) is not None):
            return []
        try:
            total = canonical_money(row.total_amount)
            fee_adjusted = _fee_adjusted_total(row.total_amount, getattr(row, "fee_amount", None))
        except AnalyticsError:
            return []
        scope_accounts, _ = self._scope_accounts(session, owner, row)
        if not scope_accounts:
            return []
        unique_id_txs = self._unique_id_matches(session, owner, row)
        direction = "CRDT" if row.receipt_kind == "refund" else "DBIT"
        shop_name = normalize_shop_name(row.raw_shop_name)
        chain = chain_for(row.raw_shop_name, row.raw_nip)
        groups, generic_tokens = merchant_score_groups(row.raw_shop_name, row.raw_nip)
        receipt_is_gateway = _is_gateway_receipt(session, row)
        linked_key = (getattr(row, "linked_account_id", None), getattr(row, "linked_entry_ref", None))
        ranked = []
        for transaction, tx_row_id in self._candidate_rows(session, owner, include_receipt_id=include_receipt_id):
            if transaction.account_id not in scope_accounts:
                continue
            id_hit = (transaction.account_id, transaction.entry_reference) in unique_id_txs
            try:
                payload = _json(transaction.payload_json)
                if (not isinstance(payload, dict) or payload.get("status") != "BOOK"
                        or payload.get("credit_debit_indicator") != direction
                        or payload.get("currency") != row.currency):
                    continue
                tx_amount = canonical_money(payload.get("amount"))
                if tx_amount != total and not (id_hit and fee_adjusted is not None and tx_amount == fee_adjusted):
                    continue
                distance = _date_distance(
                    payload, row.receipt_date,
                    document_kind=getattr(row, "document_kind", None),
                    due_date=getattr(row, "due_date", None),
                )
                if distance is None:
                    continue
            except (AnalyticsError, ValueError, TypeError, KeyError, RecursionError):
                continue
            try:
                fingerprint = financial_fingerprint(payload)
            except AnalyticsError:
                continue
            try:
                creditor, debtor = resolved_party_names(
                    session, owner, transaction.account_id, transaction.entry_reference, payload, fingerprint)
                evidence = link_evidence(
                    owner, transaction.account_id, transaction.entry_reference, payload,
                    shop_name, row.raw_nip, creditor, fingerprint, debtor=debtor)
                token = self._candidate_token(evidence, transaction.account_id, tx_row_id, id_match=id_hit)
            except (AnalyticsError, ValueError, TypeError, RecursionError):
                continue
            remittance = payload.get("remittance_information")
            if not isinstance(remittance, list):
                remittance = []
            description = " ".join(value for value in remittance if isinstance(value, str))
            party_texts = _payload_party_texts(payload)
            counterparty = debtor if direction == "CRDT" else creditor
            haystack = fold_for_match(" ".join(part for part in (description, counterparty) if part))
            haystack_words = frozenset(merchant_word_tokens(haystack))
            nip_hit, nip_labeled = _nip_match(row.raw_nip, haystack)
            distinctive_score = sum(
                1 for group in groups if any(alias.issubset(haystack_words) for alias in group)
            )
            generic_score = sum(1 for token in generic_tokens if token in haystack_words)
            sole_generic_hit = not groups and len(generic_tokens) == 1 and generic_score > 0
            hit = nip_hit or distinctive_score > 0 or sole_generic_hit
            chain_group_hit = (
                chain is not None
                and any(alias.issubset(haystack_words) for alias in groups[0])
            )
            all_distinctive_hit = bool(groups) and distinctive_score == len(groups)
            bank_has_gateway = any(
                contains_gateway_marker(text)
                for text in (description, creditor, debtor, *party_texts)
            )
            if id_hit:
                strength = "id"
            elif receipt_is_gateway or bank_has_gateway:
                strength = "none"
            elif nip_labeled:
                strength = "nip"
            elif chain_group_hit or all_distinctive_hit:
                strength = "distinctive"
            elif generic_score > 0:
                strength = "generic"
            else:
                strength = "none"
            is_current_link = (transaction.account_id, transaction.entry_reference) == linked_key
            candidate = {
                "account_id": transaction.account_id,
                "entry_reference": entry_reference_digest(transaction.entry_reference),
                "amount": canonical_money(payload["amount"]), "currency": payload["currency"],
                "credit_debit_indicator": direction,
                "transaction_date": evidence["transaction_date"],
                "booking_date": evidence["booking_date"], "value_date": evidence["value_date"],
                "merchant_token_hit": hit, "match_strength": strength, "date_distance": distance,
                "candidate_token": token,
            }
            ranked.append((
                not is_current_link, not id_hit, not nip_hit, -distinctive_score, -generic_score, distance,
                transaction.entry_reference, transaction.account_id, candidate,
            ))
        # Rank across all matching rows before slicing MAX_CANDIDATES so hits and current link survive the cap.
        ranked.sort(key=lambda item: item[:7])
        return [item[-1] for item in ranked[:MAX_CANDIDATES]]

    @staticmethod
    def _owned_account(session, owner, account_id):
        account = session.get(Account, account_id)
        if account is None or account.owner != owner or not account.enabled:
            raise ReceiptError()
        connection = session.get(Connection, account.connection_id)
        if connection is None or connection.owner != owner:
            raise ReceiptError()
        return account

    def _verify_link(self, session, owner, cleaned, link, receipt_id, *, row=None, issued_as_id=False):
        self._owned_account(session, owner, link["account_id"])
        transaction = session.get(Transaction, (link["account_id"], link["entry_reference"]))
        if transaction is None:
            raise ReceiptError("budget_receipt_stale_source", 409)
        if session.get(Leg, (link["account_id"], link["entry_reference"])) is not None:
            raise ReceiptError()
        other_link = exists().where(
            Receipt.owner == owner, Receipt.id != receipt_id,
            Receipt.linked_account_id == link["account_id"], Receipt.linked_entry_ref == link["entry_reference"],
            Receipt.status.in_(("confirmed", "needs_review")),
        )
        grouped = _group_reservation(owner, link["account_id"], link["entry_reference"])
        if session.scalar(select(other_link | grouped)):
            raise ReceiptError("budget_receipt_tx_already_linked", 409)
        try:
            payload = _json(transaction.payload_json)
            expected_direction = "CRDT" if cleaned["receipt_kind"] == "refund" else "DBIT"
            if (transaction.uncertain or not isinstance(payload, dict) or payload.get("status") != "BOOK"
                    or payload.get("credit_debit_indicator") != expected_direction
                    or payload.get("currency") != cleaned["currency"]):
                raise ReceiptError()
            tx_amount = canonical_money(payload.get("amount"))
            if issued_as_id:
                assert row is not None
                scope_accounts, _ = self._scope_accounts(session, owner, row)
                if (row.total_amount != cleaned["total_amount"]
                        or link["account_id"] not in scope_accounts
                        or (link["account_id"], link["entry_reference"]) not in self._unique_id_matches(
                            session, owner, row,
                        )):
                    raise ReceiptError("budget_receipt_stale_source", 409)
                fee_adjusted = _fee_adjusted_total(row.total_amount, getattr(row, "fee_amount", None))
                if tx_amount != cleaned["total_amount"] and (fee_adjusted is None or tx_amount != fee_adjusted):
                    raise ReceiptError()
            elif tx_amount != cleaned["total_amount"]:
                raise ReceiptError()
            distance = _date_distance(
                payload, cleaned["receipt_date"],
                document_kind=cleaned.get("document_kind"),
                due_date=cleaned.get("due_date"),
            )
            if distance is None:
                raise ReceiptError()
            fingerprint = financial_fingerprint(payload)
        except ReceiptError:
            raise
        except (AnalyticsError, ValueError, TypeError, KeyError, RecursionError):
            raise ReceiptError() from None
        return fingerprint

    def _evidence_mac(self, evidence):
        """HMAC over a digest of the evidence, not the evidence itself."""
        raw = evidence_json(evidence).encode("ascii")
        digest = hashlib.sha256(raw).digest()
        return hmac.new(self._evidence_key, digest, hashlib.sha256).digest()

    def _candidate_token(self, evidence, account_id, row_id, *, id_match=False):
        """Seal the ledger row id. The client sees the digest, not the provider reference."""
        if not isinstance(account_id, str) or type(row_id) is not int or not 1 <= row_id <= _MAX_ROW_ID:
            raise AnalyticsError()
        raw_payload = {"a": account_id, "i": row_id, "m": self._evidence_mac(evidence).hex()}
        if id_match:
            raw_payload["id"] = True
        payload = json.dumps(raw_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        nonce = os.urandom(12)
        blob = nonce + AESGCM(self._token_key).encrypt(nonce, payload, None)
        token = base64.urlsafe_b64encode(blob).rstrip(b"=").decode("ascii")
        if any(char not in _TOKEN_ALPHABET for char in token):
            raise ReceiptError()
        if len(token) > _TOKEN_LIMIT:
            raise AnalyticsError()
        return token

    def _open_candidate_token(self, token):
        """The signed row inside a candidate token. A bad token never touches the ledger."""
        if not isinstance(token, str) or any(char not in _TOKEN_ALPHABET for char in token):
            raise ReceiptError()
        try:
            padded = token + "=" * ((4 - len(token) % 4) % 4)
            blob = base64.urlsafe_b64decode(padded.encode("ascii"))
            data = json.loads(AESGCM(self._token_key).decrypt(blob[:12], blob[12:], None))
            account_id, row_id = data["a"], data["i"]
            mac = bytes.fromhex(data["m"])
            id_flag = data.get("id")
        except (InvalidTag, ValueError, KeyError, TypeError):
            raise ReceiptError() from None
        if (not isinstance(account_id, str) or type(row_id) is not int or not 1 <= row_id <= _MAX_ROW_ID
                or len(mac) != 32 or id_flag not in (None, True)):
            raise ReceiptError()
        return account_id, row_id, mac, bool(id_flag)

    @staticmethod
    def _named_transaction(session, owner, account_id, row_id):
        """Point lookup of the row id the token names, scoped to that owner and account."""
        return session.scalar(select(Transaction).join(Account).join(Connection).where(
            _ROW_ID == row_id, Transaction.account_id == account_id,
            Account.owner == owner, Connection.owner == owner,
        ))

    def _confirmed_link(self, session, owner, cleaned, link, row):
        account_id, row_id, mac, issued_as_id = self._open_candidate_token(link["candidate_token"])
        if account_id != link["account_id"]:
            raise ReceiptError()
        self._owned_account(session, owner, account_id)
        transaction = self._named_transaction(session, owner, account_id, row_id)
        if transaction is None:
            raise ReceiptError("budget_receipt_stale_source", 409)
        reference = transaction.entry_reference
        digest = entry_reference_digest(reference)
        presented = link["entry_reference"]
        # Only a lowercase ASCII sha256 hex can match; anything else (incl. non-ASCII, which would make
        # compare_digest raise TypeError) is a client error, never a 500.
        if not _DIGEST_HEX.fullmatch(presented) \
                or not hmac.compare_digest(digest, presented):
            raise ReceiptError()
        try:
            payload = _json(transaction.payload_json)
            fingerprint = None if transaction.uncertain else financial_fingerprint(payload)
            creditor, debtor = resolved_party_names(session, owner, account_id, reference, payload, fingerprint)
            current = link_evidence(
                owner, account_id, reference, payload,
                normalize_shop_name(cleaned["raw_shop_name"]), cleaned["raw_nip"], creditor, fingerprint,
                debtor=debtor)
            expected = self._evidence_mac(current)
            stored = evidence_json(current)
        except (AnalyticsError, ValueError, TypeError, KeyError, RecursionError):
            raise ReceiptError("budget_receipt_stale_source", 409) from None
        if not hmac.compare_digest(expected, mac):
            raise ReceiptError("budget_receipt_stale_source", 409)
        resolved = {**link, "entry_reference": reference}
        verified = self._verify_link(
            session, owner, cleaned, resolved, row.id, row=row, issued_as_id=issued_as_id,
        )
        link["entry_reference"] = reference
        return verified, stored

    @staticmethod
    def _check_stored_lines(session, owner, receipt_id, total_amount):
        stored = list(session.scalars(select(Line).where(
            Line.receipt_id == receipt_id, Line.owner == owner).order_by(Line.line_no)))
        validate_confirm_line_totals(
            [
                {
                    "line_kind": line.line_kind,
                    "gross_line_total": line.gross_line_total,
                    "discount_amount": line.discount_amount,
                    "net_line_total": line.net_line_total,
                }
                for line in stored
            ],
            total_amount,
        )

    def confirm(self, owner, values):
        cleaned = validate_confirm(values)
        with self.transaction() as session:
            owner = self._owner(session, owner, "budget.receipts.add")
            now = self._now()
            self._purge(session, owner, now)
            row = self._owned(session, owner, values["receipt_id"])
            own_draft = row.submitted_by == self._session_actor or (
                row.submitted_by is None and self._session_actor == owner
            )
            if row.status != "draft" and own_draft and "budget.receipts.edit" not in self._permissions:
                raise DeviceTokenError(409, "already_submitted")
            if not own_draft:
                self._owner(session, owner, "budget.receipts.edit")
            if row.version != values["expected_version"]:
                raise ReceiptError("budget_receipt_conflict", 409)
            if row.evidence_for_receipt_id is not None:
                raise ReceiptError("budget_receipt_stale_source", 409)
            effective = dict(cleaned)
            effective.setdefault("raw_shop_name", row.raw_shop_name)
            effective.setdefault("raw_nip", row.raw_nip)
            effective["chain_key"] = chain_for(effective["raw_shop_name"], effective["raw_nip"])
            effective["document_kind"] = row.document_kind
            effective["due_date"] = row.due_date
            if effective["lines"] is None:
                self._check_stored_lines(session, owner, row.id, effective["total_amount"])
            row_was_draft = row.status == "draft"
            link = effective["link"]
            preserve_link = row.linked_account_id is not None and not self._match_allowed
            if link is not None and not self._match_allowed:
                raise DeviceTokenError(403, "budget_forbidden")
            sensitive_changed = any(
                effective[key] != getattr(row, key)
                for key in ("raw_shop_name", "raw_nip", "receipt_kind", "receipt_date",
                            "total_amount", "currency", "payment_method")
            )
            fingerprint = None if link is None else self._confirmed_link(session, owner, effective, link, row)
            before = audit_payload(row, "confirm")
            row.shop_id = self._shop_id(session, owner, effective)
            for key in ("raw_shop_name", "raw_nip", "receipt_kind", "receipt_date", "receipt_time",
                        "total_amount", "currency", "payment_method"):
                setattr(row, key, effective[key])
            if effective["lines"] is not None:
                session.execute(delete(Line).where(Line.receipt_id == row.id, Line.owner == owner))
                for line in effective["lines"]:
                    session.add(Line(receipt_id=row.id, owner=owner, **line))
            if not preserve_link:
                if link is None:
                    row.linked_account_id = row.linked_entry_ref = row.linked_fingerprint = row.linked_evidence = None
                else:
                    row.linked_account_id = link["account_id"]
                    row.linked_entry_ref = link["entry_reference"]
                    row.linked_fingerprint, row.linked_evidence = fingerprint
            row.status = "needs_review" if preserve_link and (sensitive_changed or row.status == "needs_review") else "confirmed"
            row.version, row.updated_at = row.version + 1, now
            row.import_expires_at = None
            if row_was_draft:
                row.image_expires_at = now + CONFIRMED_TEXT_TTL_SECONDS
            try:
                session.flush()
            except IntegrityError:
                raise ReceiptError("budget_receipt_tx_already_linked", 409) from None
            write_audit(session, owner, row, "confirm", before, audit_payload(row, "confirm", self._actor), now)
            return {"ok": True, "receipt": self._detail(session, row)}

    def _encode_cursor(self, owner, context, row):
        now = self._now()
        payload = {"owner": owner, "actor": self._session_actor, "context": context,
                   "last_date": row.receipt_date, "last_id": row.id, "expires_at": now + CURSOR_TTL_SECONDS}
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=True, allow_nan=False,
                         separators=(",", ":")).encode("ascii")
        nonce = os.urandom(12)
        protected = b"\x01" + nonce + AESGCM(self._receipt_cursor_key).encrypt(nonce, raw, _CURSOR_DOMAIN)
        encoded = base64.urlsafe_b64encode(protected).rstrip(b"=").decode("ascii")
        signature = base64.urlsafe_b64encode(hmac.new(self._receipt_cursor_key, protected, hashlib.sha256).digest())
        return encoded + "." + signature.rstrip(b"=").decode("ascii")

    def _decode_cursor(self, owner, context, cursor):
        if cursor is None:
            return None
        try:
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= MAX_CURSOR_CHARS:
                raise ValueError
            encoded, signature = cursor.split(".")
            protected = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
            supplied = base64.b64decode(signature + "=" * (-len(signature) % 4), altchars=b"-_", validate=True)
            expected = hmac.new(self._receipt_cursor_key, protected, hashlib.sha256).digest()
            if not hmac.compare_digest(supplied, expected):
                raise ValueError
            if protected.startswith(b"\x01"):
                raw = AESGCM(self._receipt_cursor_key).decrypt(protected[1:13], protected[13:], _CURSOR_DOMAIN)
            else:
                if (self._session_actor or owner) != owner:
                    raise ValueError
                raw = protected
            value = _json(raw)
            if (not isinstance(value, dict)
                    or set(value) not in (
                        {"owner", "actor", "context", "last_date", "last_id", "expires_at"},
                        {"owner", "context", "last_date", "last_id", "expires_at"},
                    )
                    or value["owner"] != owner
                    or value.get("actor", owner) != (self._session_actor or owner)
                    or value["context"] != context
                    or type(value["expires_at"]) not in (int, float)
                    or not math.isfinite(value["expires_at"]) or not self._now() < value["expires_at"]
                    or not _UUID.fullmatch(value["last_id"])
                    or (value["last_date"] is not None and not _valid_date(value["last_date"]))):
                raise ValueError
            return value["last_date"], value["last_id"]
        except (InvalidTag, ValueError, TypeError, KeyError, RecursionError, OverflowError):
            raise ReceiptError() from None

    @staticmethod
    def _after_cursor(row, cursor):
        """Rows strictly after the cursor under ``ORDER BY receipt_date DESC, id``.

        SQLite sorts NULL dates last on DESC, then ``id`` ascending. A NULL cursor
        therefore excludes every dated row, and a dated cursor includes every NULL row.
        """
        if cursor is None:
            return True
        last_date, last_id = cursor
        row_date = row.receipt_date
        if row_date == last_date:
            return row.id > last_id
        if row_date is None:
            return True
        if last_date is None:
            return False
        return row_date < last_date

    @staticmethod
    def _filters(values, *, search):
        allowed = ({"query", "limit", "cursor"} if search
                   else {"month", "status", "linked", "document_kind", "evidence", "limit", "cursor"})
        if not isinstance(values, dict) or set(values) - allowed:
            raise ReceiptError()
        limit = values.get("limit", 50)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ReceiptError()
        month = values.get("month")
        if month is not None:
            try:
                if not isinstance(month, str) or not _MONTH.fullmatch(month) or not 1 <= int(month[5:]) <= 12:
                    raise ValueError
            except ValueError:
                raise ReceiptError() from None
        status = values.get("status")
        if status is not None and status not in ("draft", "confirmed", "needs_review", "imported"):
            raise ReceiptError()
        kind = values.get("document_kind")
        if kind is not None and kind not in ("receipt", "invoice", "payment_confirmation"):
            raise ReceiptError()
        linked = values.get("linked")
        if linked is not None and type(linked) is not bool:
            raise ReceiptError()
        evidence = values.get("evidence", "exclude")
        if evidence not in ("exclude", "only", "include"):
            raise ReceiptError()
        query = values.get("query")
        if search and (not isinstance(query, str) or not query.strip() or len(query) > 128):
            raise ReceiptError()
        context_values = {"query": fold_for_match(query.strip())} if search else {
            "month": month, "status": status, "linked": linked,
        }
        if kind is not None:
            context_values["document_kind"] = kind
        if evidence != "exclude":
            context_values["evidence"] = evidence
        context = hashlib.sha256(json.dumps(context_values, sort_keys=True, ensure_ascii=True,
                                            separators=(",", ":")).encode("ascii")).hexdigest()
        return limit, month, status, linked, query, kind, evidence, context

    def _list(self, owner, values, *, search):
        limit, month, status, linked, query, kind, evidence, context = self._filters(values, search=search)
        with self.transaction() as session:
            owner = self._owner(session, owner, "budget.receipts.view")
            now = self._now()
            self._purge(session, owner, now)
            cursor = self._decode_cursor(owner, context, values.get("cursor"))
            query_obj = select(Receipt).where(Receipt.owner == owner)
            if month is not None:
                query_obj = query_obj.where(Receipt.receipt_date >= month + "-01", Receipt.receipt_date <= month + "-31")
            if status is not None:
                query_obj = query_obj.where(Receipt.status == status)
            if kind is not None:
                query_obj = query_obj.where(Receipt.document_kind == kind)
            if not search:
                if evidence == "exclude":
                    query_obj = query_obj.where(Receipt.evidence_for_receipt_id.is_(None))
                elif evidence == "only":
                    query_obj = query_obj.where(Receipt.evidence_for_receipt_id.is_not(None))
            if linked is not None:
                query_obj = query_obj.where(Receipt.linked_account_id.is_not(None) if linked
                                            else Receipt.linked_account_id.is_(None))
            rows = session.scalars(query_obj.order_by(Receipt.receipt_date.desc(), Receipt.id)).all()
            needle = fold_for_match(query.strip()) if search else None
            selected = []
            for row in rows:
                if not self._after_cursor(row, cursor):
                    continue
                if search and not self._matches(session, row, needle):
                    continue
                selected.append(row)
                if len(selected) > limit:
                    break
            more = len(selected) > limit
            selected = selected[:limit]
            next_cursor = self._encode_cursor(owner, context, selected[-1]) if more and selected else None
            return {"receipts": [self._summary(session, row) for row in selected], "next_cursor": next_cursor}

    def list(self, owner, values):
        return self._list(owner, values, search=False)

    def search(self, owner, values):
        return self._list(owner, values, search=True)

    @staticmethod
    def _matches(session, row, needle):
        if needle in fold_for_match(row.raw_shop_name or "") or needle in fold_for_match(row.raw_nip or ""):
            return True
        return session.scalar(select(Line.receipt_id).where(
            Line.receipt_id == row.id, Line.owner == row.owner,
            Line.raw_name_key.like("%" + _like_escape(needle) + "%", escape="\\"),
        ).limit(1)) is not None

    def detail(self, owner, receipt_id):
        with self.transaction() as session:
            owner = self._owner(session, owner, "budget.receipts.view")
            now = self._now()
            self._purge(session, owner, now)
            row = self._owned(session, owner, receipt_id)
            receipt = self._detail(session, row)
            # Re-issue outside the writer lock. The token is bound to this receipt's
            # own reservation; confirm checks the link again inside BEGIN IMMEDIATE.
            preview = None if row.status not in ("imported", "draft", "needs_review") or not self._match_allowed else SimpleNamespace(
                total_amount=row.total_amount, currency=row.currency, receipt_date=row.receipt_date,
                payment_method=row.payment_method, receipt_kind=row.receipt_kind,
                raw_shop_name=row.raw_shop_name, raw_nip=row.raw_nip,
                document_kind=row.document_kind, due_date=row.due_date, id=row.id,
                forwarded_sender_domain=row.forwarded_sender_domain,
                gateway=row.gateway, gateway_ref_hmac=row.gateway_ref_hmac,
                blik_ref_hmac=row.blik_ref_hmac, card_last4=row.card_last4,
                fee_amount=row.fee_amount, evidence_for_receipt_id=row.evidence_for_receipt_id,
                linked_account_id=row.linked_account_id, linked_entry_ref=row.linked_entry_ref,
            )
        if preview is None:
            return {"receipt": receipt}
        with self._read(owner, permission=None) as session:
            self._owner(session, owner, "budget.receipts.view", require_same_target=True)
            if not self._match_allowed:
                row = self._owned(session, owner, receipt_id)
                return {"receipt": self._detail(session, row)}
            candidates = self._candidates(session, owner, preview, {}, include_receipt_id=preview.id)
            no_bank_data = False if candidates else self._no_bank_data(session, owner, preview)
        return {"receipt": receipt, "candidates": candidates, "no_bank_data": no_bank_data}

    def image(self, owner, receipt_id):
        with self.transaction() as session:
            owner = self._owner(session, owner, "budget.receipts.view")
            self._owned(session, owner, receipt_id)
        raise ReceiptError("budget_receipt_not_found", 404)

    def delete(self, owner, values):
        if (not isinstance(values, dict) or set(values) != {"receipt_id", "expected_version"}
                or type(values["expected_version"]) is not int or values["expected_version"] < 1):
            raise ReceiptError()
        with self.transaction() as session:
            owner = self._owner(session, owner, "budget.receipts.add")
            now = self._now()
            self._purge(session, owner, now)
            row = self._owned(session, owner, values["receipt_id"])
            own_draft = row.status == "draft" and (
                row.submitted_by == self._session_actor
                or (row.submitted_by is None and self._session_actor == owner)
            )
            if not own_draft:
                self._owner(session, owner, "budget.receipts.edit")
            if row.linked_account_id is not None and not self._match_allowed:
                raise DeviceTokenError(403, "budget_forbidden")
            if row.version != values["expected_version"]:
                raise ReceiptError("budget_receipt_conflict", 409)
            if row.evidence_for_receipt_id is not None:
                raise ReceiptError("budget_receipt_stale_source", 409)
            tombstone_receipt(session, owner, row.id)
            session.execute(delete(Line).where(Line.receipt_id == row.id, Line.owner == owner))
            erase_receipt_audit(session, owner, row.id)
            session.delete(row)
            if self._device_id is not None:
                write_device_audit(session, owner, self._device_id, "receipt_delete", now)
            session.flush()
            return {"ok": True, "deleted": True}


def fold_for_match(value):
    from .receipt_parser import fold_text
    return fold_text(value)


def _like_escape(value):
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
