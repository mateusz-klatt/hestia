"""Import a newly discovered document once booked card evidence corroborates it."""
import json
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, or_, select

from ..db import AppMeta, BudgetGmailMessage, BudgetReceipt, BudgetReceiptLine
from .analytics_core import canonical_money
from .gmail_document import ImportedDocument
from .gmail_payment import _payee_key
from .receipt_reconcile import audit_payload, evidence_json, write_audit

_PREFIX = "budget_gmail_card_wait:"
_WARSAW = ZoneInfo("Europe/Warsaw")


def _prefix(owner):
    return _PREFIX + owner.encode().hex() + ":"


def waiting_key(owner, source_hmac):
    return _prefix(owner) + source_hmac


def forget_waiting(session, owner):
    session.execute(delete(AppMeta).where(AppMeta.key.startswith(_prefix(owner))))


def expire_waiting(session, owner, now):
    prefix = _PREFIX if owner is None else _prefix(owner)
    session.execute(delete(AppMeta).where(AppMeta.key.startswith(prefix),
                                          func.json_extract(AppMeta.value, "$.expires") <= now),
                    execution_options={"synchronize_session": False})


def stage(session, owner, item, expires_at):
    """Keep structured document fields locally while a charge is pending. No MIME body or Gmail ID."""
    data = {"invoice": asdict(item.invoice), "provenance": item.document_provenance,
            "trust": item.trust_path, "kind": item.source_kind, "rfc": item.rfc_message_id_hmac,
            "received": item.internal_date, "expires": expires_at}
    session.add(AppMeta(key=waiting_key(owner, item.gmail_id_hmac), value=json.dumps(data)))


def _identity(invoice):
    return (_payee_key(invoice.issuer), (invoice.invoice_number or "").casefold(),
            invoice.currency, canonical_money(invoice.amount))


def receipt_document(receipt):
    return SimpleNamespace(issuer=receipt.raw_shop_name, nip=receipt.raw_nip, invoice_number=receipt.invoice_number,
                           amount=receipt.total_amount, currency=receipt.currency, issue_date=receipt.receipt_date,
                           due_date=receipt.due_date, document_kind=(
                               "receipt" if receipt.document_kind == "payment_confirmation" else receipt.document_kind))


def _received_date(stamp):
    return datetime.fromtimestamp(stamp, tz=UTC).astimezone(_WARSAW).date()


def _record(session, owner, key, data, now, outcome, receipt_id=None):
    session.add(BudgetGmailMessage(owner=owner, gmail_id_hmac=key.rsplit(":", 1)[1],
                                   rfc_message_id_hmac=data["rfc"], internal_date=data["received"],
                                   trust_path=data["trust"], outcome=outcome, created_at=now, receipt_id=receipt_id))


def _pending_receipts(session, owner):
    return session.execute(select(BudgetReceipt, BudgetGmailMessage.internal_date).join(
        BudgetGmailMessage, (BudgetGmailMessage.receipt_id == BudgetReceipt.id)
        & (BudgetGmailMessage.owner == BudgetReceipt.owner)).where(
        BudgetReceipt.owner == owner, BudgetReceipt.status == "imported", BudgetReceipt.version == 1,
        BudgetReceipt.gateway.is_(None), BudgetReceipt.evidence_for_receipt_id.is_(None),
        BudgetReceipt.linked_account_id.is_(None), BudgetReceipt.forwarded_manually == 0,
        BudgetReceipt.raw_shop_name.is_not(None), BudgetReceipt.total_amount.is_not(None),
        BudgetReceipt.currency.is_not(None), BudgetReceipt.receipt_kind == "purchase"))


def _confirm(session, owner, receipt, match, now):
    before = audit_payload(receipt, "confirm")
    if session.get(BudgetReceiptLine, (receipt.id, 1)) is None:
        session.add(BudgetReceiptLine(receipt_id=receipt.id, owner=owner, line_no=1, line_kind="other",
                                     raw_name="Dokument", raw_name_key="dokument", quantity="1",
                                     gross_line_total=receipt.total_amount, net_line_total=receipt.total_amount))
    receipt.receipt_date = receipt.receipt_date or match.receipt_date
    receipt.status, receipt.payment_method, receipt.import_expires_at = "confirmed", "card", None
    receipt.linked_account_id, receipt.linked_entry_ref = match.account_id, match.entry_reference
    receipt.linked_fingerprint, receipt.linked_evidence = match.fingerprint, evidence_json(match.evidence)
    receipt.version += 1
    receipt.updated_at = now
    write_audit(session, owner, receipt, "confirm", before, audit_payload(receipt, "confirm", "gmail_import"), now)


def _existing_document(session, owner, invoice, provenance):
    from .gmail_store import _document_number_key

    if invoice.invoice_number is None:
        return None
    rows = session.execute(select(BudgetReceipt, AppMeta.value).outerjoin(
        AppMeta, AppMeta.key == _document_number_key(BudgetReceipt.id)).where(
        BudgetReceipt.owner == owner,
        or_(func.lower(BudgetReceipt.invoice_number) == invoice.invoice_number.casefold(),
            func.json_extract(AppMeta.value, "$.number_hmac") == provenance["number_hmac"])))
    outcomes = set()
    for receipt, meta in rows:
        seller = json.loads(meta)["seller_key"] if meta else _payee_key(receipt.raw_shop_name or "")
        if seller == provenance["seller_key"]:
            agrees = (receipt.total_amount is not None and receipt.currency == invoice.currency
                      and canonical_money(receipt.total_amount) == canonical_money(invoice.amount))
            outcomes.add("skipped_duplicate" if agrees else "parse_failed")
    return "parse_failed" if "parse_failed" in outcomes else next(iter(outcomes), None)


def _waiting_documents(store, session, owner, now):
    groups = defaultdict(list)
    rows = session.scalars(select(AppMeta).where(AppMeta.key.startswith(_prefix(owner))).order_by(AppMeta.key))
    for meta in rows:
        data = json.loads(meta.value)
        invoice = ImportedDocument(**data["invoice"])
        outcome = ("skipped_duplicate" if store._duplicate(session, owner, data["rfc"]) else
                   _existing_document(session, owner, invoice, data["provenance"]))
        if outcome:
            _record(session, owner, meta.key, data, now, outcome)
            session.delete(meta)
        else:
            reference = data["provenance"]["number_hmac"] or data["rfc"] or meta.key
            key = data["provenance"]["seller_key"], reference
            groups[key].append((meta, data, invoice))
    return groups


def _import_copies(store, session, owner, copies, now):
    from .gmail_store import PendingMessage

    meta, data, invoice = copies[0]
    source_hmac = meta.key.rsplit(":", 1)[1]
    item = PendingMessage(source_hmac, data["received"], data["trust"], "imported",
                          rfc_message_id_hmac=data["rfc"], invoice=invoice, source_kind=data["kind"],
                          document_provenance=data["provenance"], card_required=True)
    receipt_id = store._invoice(session, owner, item, now)
    _record(session, owner, meta.key, data, now, "imported", receipt_id)
    session.delete(meta)
    for duplicate, duplicate_data, _ in copies[1:]:
        _record(session, owner, duplicate.key, duplicate_data, now, "skipped_duplicate")
        session.delete(duplicate)
    return session.get(BudgetReceipt, receipt_id)


def settle(store, session, owner, now):
    # The receipt candidate store also uses Gmail ledger retention.
    from .gmail_card_match import match_card_purchase

    expire_waiting(session, owner, now)
    claims = defaultdict(dict)
    for receipt, stamp in _pending_receipts(session, owner):
        match = match_card_purchase(session, owner, receipt_document(receipt), received_date=_received_date(stamp),
                                    include_receipt_id=receipt.id)
        if match:
            claims[match.account_id, match.entry_reference][receipt.id] = receipt, match

    waiting = _waiting_documents(store, session, owner, now)
    for key, copies in waiting.items():
        if len({(_identity(invoice), invoice.issue_date) for _, _, invoice in copies}) > 1:
            continue
        _, data, invoice = copies[0]
        match = match_card_purchase(session, owner, invoice, received_date=_received_date(data["received"]))
        if match:
            claims[match.account_id, match.entry_reference][key] = None, match

    created = 0
    for entries in claims.values():
        if len(entries) != 1:
            continue
        identity, (receipt, match) = next(iter(entries.items()))
        if receipt is None:
            receipt = _import_copies(store, session, owner, waiting[identity], now)
            created += 1
        _confirm(session, owner, receipt, match, now)
    return created
