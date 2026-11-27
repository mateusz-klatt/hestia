"""Import a newly discovered document once booked card evidence corroborates it."""
import json
from collections import defaultdict
from dataclasses import asdict, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, delete, func, or_, select, tuple_

from ..db import AppMeta, BudgetAnalyticsAudit, BudgetGmailMessage, BudgetReceipt, BudgetReceiptLine
from .analytics_core import AnalyticsError, canonical_money
from .gmail_document import ImportedDocument
from .gmail_payment import _payee_key
from .receipt_parser import merchant_word_tokens
from .receipt_reconcile import _latch_receipt_source_changed, audit_payload, evidence_json, write_audit

_PREFIX = "budget_gmail_card_wait:"
_GMAIL_IMPORT_ACTOR = "gmail_import"
_LINK_BATCH_SIZE = 400
_WARSAW = ZoneInfo("Europe/Warsaw")


def _prefix(owner):
    return _PREFIX + owner.encode().hex() + ":"


def waiting_key(owner, source_hmac):
    return _prefix(owner) + source_hmac


def forget_waiting(session, owner):
    session.execute(delete(AppMeta).where(AppMeta.key.startswith(_prefix(owner))))


def _waiting_filter(owner):
    prefix = _PREFIX if owner is None else _prefix(owner)
    return AppMeta.key.startswith(prefix, autoescape=True)


def _staged_groups(session, owner):
    groups = defaultdict(list)
    rows = session.scalars(select(AppMeta).where(_waiting_filter(owner)).order_by(AppMeta.key))
    for meta in rows:
        data = json.loads(meta.value)
        reference = data["provenance"]["number_hmac"] or data["rfc"] or meta.key
        owner_key = meta.key.rsplit(":", 1)[0]
        groups[owner_key, data["provenance"]["seller_key"], reference].append((meta, data))
    return groups


def expire_waiting(session, owner, now):
    """Expire complete document and RFC copy clusters together."""
    matches = _waiting_filter(owner)
    expires = func.json_extract(case((matches, AppMeta.value)), "$.expires")
    if session.scalar(select(AppMeta.key).where(matches, expires <= now).limit(1)) is None:
        return

    groups = _staged_groups(session, owner)
    groups_by_rfc = _groups_by_rfc(groups)
    remaining = set(groups)
    while remaining:
        pending = [remaining.pop()]
        cluster = []
        while pending:
            group = pending.pop()
            copies = groups[group]
            cluster.extend(copies)
            for _, data in copies:
                for neighbor in groups_by_rfc.pop((group[0], data["rfc"]), ()):
                    if neighbor in remaining:
                        remaining.remove(neighbor)
                        pending.append(neighbor)
        if all(data["expires"] <= now for _, data in cluster):
            for meta, _ in cluster:
                session.delete(meta)


def stage(session, owner, item, expires_at):
    """Keep structured document fields locally while a charge is pending. No MIME body or Gmail ID."""
    data = {"invoice": asdict(item.invoice), "provenance": item.document_provenance,
            "trust": item.trust_path, "kind": item.source_kind, "rfc": item.rfc_message_id_hmac,
            "received": item.internal_date, "expires": expires_at}
    session.add(AppMeta(key=waiting_key(owner, item.gmail_id_hmac), value=json.dumps(data)))


def _amount_key(amount):
    try:
        return True, canonical_money(amount)
    except AnalyticsError:
        return False, amount


def _identity(data, invoice):
    return (
        _payee_key(invoice.issuer),
        merchant_word_tokens(invoice.issuer),
        invoice.nip,
        (invoice.invoice_number or "").casefold(),
        (invoice.receipt_number or "").casefold(),
        invoice.currency,
        _amount_key(invoice.amount),
        invoice.issue_date,
        None if invoice.issue_date else _received_date(data["received"]),
        invoice.due_date,
        invoice.card_last4,
        invoice.payment_method,
        invoice.document_kind,
    )


def _matching_variants(copies):
    variants = {}
    for _, data, invoice in copies:
        variants.setdefault(_identity(data, invoice), (data, invoice))
    return variants


def _groups_by_rfc(groups):
    groups_by_rfc = defaultdict(set)
    for key, copies in groups.items():
        for _, data in copies:
            if data["rfc"] is not None:
                groups_by_rfc[key[0], data["rfc"]].add(key)
    return groups_by_rfc


def receipt_document(receipt):
    return SimpleNamespace(issuer=receipt.raw_shop_name, nip=receipt.raw_nip, invoice_number=receipt.invoice_number,
                           amount=receipt.total_amount, currency=receipt.currency, issue_date=receipt.receipt_date,
                           due_date=receipt.due_date, card_last4=receipt.card_last4,
                           payment_method=receipt.payment_method, document_kind=(
                               "receipt" if receipt.document_kind == "payment_confirmation" else receipt.document_kind))


def _received_date(stamp):
    return datetime.fromtimestamp(stamp, tz=UTC).astimezone(_WARSAW).date()


def _record(session, owner, key, data, now, outcome, receipt_id=None):
    session.add(BudgetGmailMessage(owner=owner, gmail_id_hmac=key.rsplit(":", 1)[1],
                                   rfc_message_id_hmac=data["rfc"], internal_date=data["received"],
                                   trust_path=data["trust"], outcome=outcome, created_at=now, receipt_id=receipt_id))


def _gmail_receipt_audit(action):
    return select(BudgetAnalyticsAudit.id).where(
        BudgetAnalyticsAudit.owner == BudgetReceipt.owner,
        BudgetAnalyticsAudit.subject_type == "receipt",
        BudgetAnalyticsAudit.subject_id == BudgetReceipt.id,
        BudgetAnalyticsAudit.action == action,
        func.json_extract(BudgetAnalyticsAudit.after_json, "$.actor") == _GMAIL_IMPORT_ACTOR,
        func.json_extract(BudgetAnalyticsAudit.after_json, "$.version") == BudgetReceipt.version,
    )


def _pending_receipts(session, owner):
    from .gmail_store import _document_number_key

    imported = BudgetReceipt.status == "imported"
    version_two_invoice = and_(BudgetReceipt.version == 2, BudgetReceipt.document_kind == "invoice")
    auto_promoted = and_(version_two_invoice, func.json_extract(AppMeta.value, "$.auto_promoted_version") == 2)
    auto_eligible = and_(imported, or_(BudgetReceipt.version == 1, auto_promoted))
    pending = and_(imported, BudgetGmailMessage.receipt_id.is_not(None),
                   or_(BudgetReceipt.version == 1, version_two_invoice))
    # Keep an unlinked receipt in the graph after reopening it, even if its Gmail ledger entry has expired.
    reopened = and_(
        BudgetReceipt.status == "needs_review",
        _gmail_receipt_audit("source_changed").where(
            func.json_extract(BudgetAnalyticsAudit.before_json, "$.linked") == 1,
            func.json_extract(BudgetAnalyticsAudit.after_json, "$.linked") == 0,
        ).exists(),
    )
    return session.execute(select(BudgetReceipt, BudgetGmailMessage.internal_date, auto_eligible).outerjoin(
        BudgetGmailMessage, (BudgetGmailMessage.receipt_id == BudgetReceipt.id)
        & (BudgetGmailMessage.owner == BudgetReceipt.owner)).outerjoin(
        AppMeta, AppMeta.key == _document_number_key(BudgetReceipt.id)).where(
        BudgetReceipt.owner == owner, or_(pending, reopened),
        BudgetReceipt.gateway.is_(None), BudgetReceipt.evidence_for_receipt_id.is_(None),
        BudgetReceipt.linked_account_id.is_(None), BudgetReceipt.forwarded_manually == 0,
        BudgetReceipt.raw_shop_name.is_not(None), BudgetReceipt.total_amount.is_not(None),
        BudgetReceipt.currency.is_not(None), BudgetReceipt.receipt_kind == "purchase",
    ).order_by(BudgetGmailMessage.internal_date))


def _confirm(session, owner, receipt, match, now):
    before = audit_payload(receipt, "confirm")
    if session.get(BudgetReceiptLine, (receipt.id, 1)) is None:
        session.add(BudgetReceiptLine(receipt_id=receipt.id, owner=owner, line_no=1, line_kind="other",
                                     raw_name="Dokument", raw_name_key="dokument", quantity="1",
                                     gross_line_total=receipt.total_amount, net_line_total=receipt.total_amount))
    receipt.receipt_date = receipt.receipt_date or match.receipt_date
    receipt.status, receipt.import_expires_at = "confirmed", None
    receipt.payment_method = receipt.payment_method or "card"
    receipt.linked_account_id, receipt.linked_entry_ref = match.account_id, match.entry_reference
    receipt.linked_fingerprint, receipt.linked_evidence = match.fingerprint, evidence_json(match.evidence)
    receipt.version += 1
    receipt.updated_at = now
    write_audit(session, owner, receipt, "confirm", before, audit_payload(receipt, "confirm", _GMAIL_IMPORT_ACTOR), now)


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


def _duplicate_rfc(store, session, owner, rfc_hmac):
    from .gmail_store import _linked_manual_forward

    if rfc_hmac is None:
        return False
    if store._duplicate(session, owner, rfc_hmac):
        return True
    return session.scalar(select(BudgetGmailMessage.gmail_id_hmac).where(
        BudgetGmailMessage.owner == owner, BudgetGmailMessage.rfc_message_id_hmac == rfc_hmac,
        BudgetGmailMessage.outcome == "skipped_duplicate", ~_linked_manual_forward(),
    ).limit(1)) is not None


def _waiting_documents(store, session, owner, now):
    staged = _staged_groups(session, owner)
    shared_rfcs = {rfc for (_, rfc), keys in _groups_by_rfc(staged).items() if len(keys) > 1}
    groups = {}
    pruned = []
    for key, rows in staged.items():
        copies = [(meta, data, ImportedDocument(**data["invoice"])) for meta, data in rows]
        # Pruning a conflicting copy could make the remaining copy look corroborated.
        if len(_matching_variants(copies)) > 1:
            groups[key] = copies
            continue
        _, data, invoice = copies[0]
        if any(_duplicate_rfc(store, session, owner, data["rfc"]) for _, data, _ in copies):
            outcome = "skipped_duplicate"
        else:
            outcome = _existing_document(session, owner, invoice, data["provenance"])
        if outcome:
            pruned.extend((meta, data, outcome) for meta, data, _ in copies)
        else:
            groups[key] = copies
    for meta, data, outcome in pruned:
        _record(session, owner, meta.key, data, now, outcome)
        session.delete(meta)
    return groups, shared_rfcs


def _import_copies(store, session, owner, copies, now):
    from .gmail_store import PendingMessage

    meta, data, invoice = copies[0]
    provenance = dict(data["provenance"])
    provenance["paid"] = any(bool(copy_data["provenance"].get("paid")) for _, copy_data, _ in copies)
    invoice = replace(invoice, paid=provenance["paid"])
    source_hmac = meta.key.rsplit(":", 1)[1]
    item = PendingMessage(source_hmac, data["received"], data["trust"], "imported",
                          rfc_message_id_hmac=data["rfc"], invoice=invoice, source_kind=data["kind"],
                          document_provenance=provenance, card_required=True)
    receipt_id = store._invoice(session, owner, item, now)
    _record(session, owner, meta.key, data, now, "imported", receipt_id)
    session.delete(meta)
    for duplicate, duplicate_data, _ in copies[1:]:
        _record(session, owner, duplicate.key, duplicate_data, now, "skipped_duplicate")
        session.delete(duplicate)
    return session.get(BudgetReceipt, receipt_id)


def _record_claims(claims, claims_by_document, document, candidates):
    for candidate in candidates:
        tx_key = candidate.transaction_key
        claims_by_document[document].add(tx_key)
        # An uncorroborated debit affects this document's uniqueness only.
        if candidate.is_corroborated:
            claims[tx_key].setdefault(document, candidate)


def _waiting_candidates(session, owner, copies, *, shared_rfcs):
    from .gmail_card_match import card_purchase_candidates

    variants = _matching_variants(copies)
    contested = len(variants) > 1 or any(data["rfc"] in shared_rfcs for _, data, _ in copies)
    candidates = []
    for data, invoice in variants.values():
        candidates.extend(card_purchase_candidates(
            session, owner, invoice, received_date=_received_date(data["received"]),
        ))
    return not contested, candidates


def _document_identifiers(seller, number, number_hmac, rfcs):
    identifiers = {("rfc", rfc) for rfc in rfcs if rfc is not None}
    if number:
        identifiers.add(("invoice", seller, number.casefold()))
    if number_hmac:
        identifiers.add(("number_hmac", seller, number_hmac))
    return identifiers


def _receipt_identifiers(session, owner, receipt):
    from .gmail_store import _document_number_key

    meta = session.get(AppMeta, _document_number_key(receipt.id))
    provenance = json.loads(meta.value) if meta is not None else {}
    rfcs = session.scalars(select(BudgetGmailMessage.rfc_message_id_hmac).where(
        BudgetGmailMessage.owner == owner, BudgetGmailMessage.receipt_id == receipt.id,
    ))
    return _document_identifiers(
        provenance.get("seller_key") or _payee_key(receipt.raw_shop_name or ""), receipt.invoice_number,
        provenance.get("number_hmac"), rfcs,
    )


def _has_distinct_claim(session, owner, row, claimers, receipts, waiting):
    original = _receipt_identifiers(session, owner, row)
    for kind, key in claimers:
        if kind == "receipt":
            receipt = receipts[key]
            if receipt.status != "imported":
                continue
            identifiers = _receipt_identifiers(session, owner, receipt)
        else:
            identifiers = set()
            for _, data, invoice in waiting[key]:
                provenance = data["provenance"]
                identifiers.update(_document_identifiers(
                    provenance["seller_key"], invoice.invoice_number, provenance["number_hmac"], [data["rfc"]],
                ))
        if original.isdisjoint(identifiers):
            return True
    return False


def _reopen_competing_links(session, owner, now, claims, receipts, waiting):
    occupied_claims = {}
    for key, claimers in claims.items():
        occupied = [document for document, candidate in claimers.items() if candidate.is_occupied]
        if occupied:
            occupied_claims[key] = occupied
    blocked_keys = list(occupied_claims)
    evidence = case((func.json_valid(BudgetReceipt.linked_evidence), BudgetReceipt.linked_evidence))
    # Manual relinks replace the marker. A manual confirmation that preserves the bank link
    # still increments the version and cannot start from an imported receipt with a link.
    auto_confirmed = _gmail_receipt_audit("confirm").where(
        func.json_extract(BudgetAnalyticsAudit.before_json, "$.status") == "imported",
        func.json_extract(BudgetAnalyticsAudit.after_json, "$.linked") == 1,
    ).exists()
    for start in range(0, len(blocked_keys), _LINK_BATCH_SIZE):
        keys = blocked_keys[start:start + _LINK_BATCH_SIZE]
        rows = session.scalars(select(BudgetReceipt).where(
            BudgetReceipt.owner == owner, BudgetReceipt.status == "confirmed",
            tuple_(BudgetReceipt.linked_account_id, BudgetReceipt.linked_entry_ref).in_(keys),
            func.json_type(evidence, "$.gmail_card") == "true", auto_confirmed,
        ))
        for row in rows:
            key = row.linked_account_id, row.linked_entry_ref
            if _has_distinct_claim(session, owner, row, occupied_claims[key], receipts, waiting):
                _latch_receipt_source_changed(session, owner, row, now, unlink=True, actor=_GMAIL_IMPORT_ACTOR)


def settle(store, session, owner, now):
    # The receipt candidate store also uses Gmail ledger retention.
    from .gmail_card_match import card_purchase_candidates

    expire_waiting(session, owner, now)
    claims = defaultdict(dict)
    claims_by_document = defaultdict(set)
    receipts = {}
    claim_only = set()
    for receipt, stamp, auto_eligible in _pending_receipts(session, owner):
        document = ("receipt", receipt.id)
        receipts[receipt.id] = receipt
        if not auto_eligible:
            claim_only.add(document)
        candidates = card_purchase_candidates(
            session, owner, receipt_document(receipt), received_date=None if stamp is None else _received_date(stamp),
            include_receipt_id=receipt.id,
        )
        _record_claims(claims, claims_by_document, document, candidates)

    waiting, shared_rfcs = _waiting_documents(store, session, owner, now)
    for key, copies in waiting.items():
        document = ("waiting", key)
        can_settle, candidates = _waiting_candidates(session, owner, copies, shared_rfcs=shared_rfcs)
        _record_claims(claims, claims_by_document, document, candidates)
        if not can_settle:
            claim_only.add(document)

    # The graph was built with the earlier links still occupied. Keep those claims
    # unavailable for this pass even when the original receipt is reopened.
    _reopen_competing_links(session, owner, now, claims, receipts, waiting)

    created = 0
    for claimers in claims.values():
        if len(claimers) != 1:
            continue
        document, candidate = next(iter(claimers.items()))
        match = candidate.available_purchase
        if match is None or document in claim_only or len(claims_by_document[document]) != 1:
            continue
        kind, key = document
        if kind == "receipt":
            receipt = receipts[key]
        else:
            receipt = _import_copies(store, session, owner, waiting[key], now)
            created += 1
        _confirm(session, owner, receipt, match, now)
    return created
