"""SQLite grant fences, receipt storage and keyed Gmail provenance."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
import hmac
import json
import logging
import uuid

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.orm import aliased

from ..db import AppMeta, BudgetAnalyticsAudit, BudgetGmailMessage, BudgetGmailState, BudgetReceipt, BudgetReceiptLine
from .gmail_client import GmailError
from .gmail_payment import ImportedPayment, _payee_key
from .receipt_parser import fold_text
from .receipt_reconcile import audit_payload, erase_receipt_audit, write_audit
from .store import BudgetStore
from .views import BudgetHTTPStore

DAY = 86400
IMPORT_TTL = 30 * DAY
PROVENANCE_TTL = 400 * DAY
MAX_PENDING_IMPORTS = 200
MAX_GATEWAY_REFS = 4
GMAIL_MESSAGE_DOMAIN = b"hestia/budget/gmail-msg-id/v1\0"
RFC_MESSAGE_DOMAIN = b"hestia/budget/gmail-rfc-msg-id/v1\0"
PAYMENT_SENDER_DOMAIN = b"hestia/budget/gmail-payment-sender/v1\0"
PAYMENT_PAYEE_DOMAIN = b"hestia/budget/gmail-payment-payee/v1\0"
ORDER_REFERENCE_DOMAIN = b"hestia/budget/gmail-order-reference/v1\0"
GATEWAY_REF_DOMAIN = b"hestia/budget/gmail-gateway-ref/v1\0"
SCOPE_DOMAIN = b"hestia/budget/gmail-scope-query/v1\0"
MAILBOX_DOMAIN = b"hestia/budget/gmail-mailbox/v1\0"

_PHASE_RESET = {
    "pending_history_id": None, "resume_page_token": None, "history_page_token": None,
    "scope_scan_page_token": None, "scope_scan_query_hmac": None, "resume_cutoff_ts": None,
    "batch_processed_count": 0,
}

GRANT_RESET = {
    **_PHASE_RESET, "history_id": None, "sync_phase": "idle", "mailbox_hmac": None,
    "last_sync_at": None, "last_attempt_at": None, "next_sync_at": 0, "backoff_until": 0, "failures": 0,
    "attention": None, "last_http_status": None, "last_error": None, "backfill_completed_at": None,
    "run_requested": 0, "force_full_resync": 0,
}

STATUS_FIELDS = ("grant_status", "grant_generation", "version", "sync_phase", "attention", "last_sync_at",
                 "next_sync_at", "backoff_until", "failures", "last_http_status", "last_error",
                 "batch_processed_count")
_PARSE_FAILED_REASONS_PREFIX = "budget_gmail_parse_failed_reasons:"
_RETRY_WINDOW_PREFIX = "budget_gmail_retry_full_window:"
_INC_RESET_PREFIX = "budget_gmail_inc_reset:"
_PAYMENT_SENDER_PREFIX = "budget_gmail_payment_sender:"
_PAYMENT_ORDER_PREFIX = "budget_gmail_payment_order:"
_PAYMENT_PAYEE_PREFIX = "budget_gmail_payment_payee:"
_GATEWAY_FAILED_PREFIX = "budget_gmail_gateway_failed:"
_GATEWAY_REF_PREFIX = "budget_gmail_gateway_ref:"
PARSE_FAILED_REASONS = (
    "charset_error",
    "message_defects",
    "mime_error",
    "other",
    "part_defects",
    "pdf_unsupported",
    "recursion",
    "unicode_error",
    "worker_rejected",
)
_PARSE_FAILED_REASON_SET = frozenset(PARSE_FAILED_REASONS)
log = logging.getLogger("hestia.budget.gmail_store")


def normalize_parse_failed_reason(reason):
    """Map unknown or missing parse_failed reason codes to 'other'."""
    return reason if isinstance(reason, str) and reason in _PARSE_FAILED_REASON_SET else "other"


def _normalize_reason_counts(raw):
    counts = {}
    for reason, count in raw.items():
        key = normalize_parse_failed_reason(reason)
        counts[key] = counts.get(key, 0) + count
    return counts


def _reasons_key(owner):
    return _PARSE_FAILED_REASONS_PREFIX + owner


def _retry_window_key(owner):
    return _RETRY_WINDOW_PREFIX + owner


def _inc_reset_key(owner):
    return _INC_RESET_PREFIX + owner


def digest(secret, domain, value):
    return hmac.digest(secret, domain + value.encode("utf-8"), "sha256").hex()


@dataclass(frozen=True, repr=False)
class PendingPayment:
    fields: ImportedPayment
    receipt_date: date
    sender_hmac: str
    order_reference_hmac: str | None = None
    gateway: bool = False
    payee_hmac: str | None = None
    status: str | None = None
    gateway_ref_hmac: str | None = None
    gateway_name: str | None = None
    gateway_ref_secondary_hmac: str | None = None


@dataclass(frozen=True, repr=False)
class PendingMessage:
    gmail_id_hmac: str
    internal_date: float
    trust_path: str
    outcome: str
    rfc_message_id_hmac: str | None = None
    invoice: object = None
    source_kind: str = "email"
    forwarded_manually: bool = False
    sender_domain: str | None = None
    parse_failed_reason: str | None = None
    payment: PendingPayment | None = None
    failed_gateway: tuple[str, ...] | None = None


def _linked_manual_forward():
    return select(BudgetReceipt.id).where(
        BudgetReceipt.id == BudgetGmailMessage.receipt_id, BudgetReceipt.forwarded_manually == 1,
    ).exists()


def _payment_sender_key(receipt_id):
    return _PAYMENT_SENDER_PREFIX + receipt_id


def _payment_order_key(receipt_id):
    return _PAYMENT_ORDER_PREFIX + receipt_id


def _payment_payee_key(receipt_id):
    return _PAYMENT_PAYEE_PREFIX + receipt_id


def _gateway_ref_key(receipt_id):
    return _GATEWAY_REF_PREFIX + receipt_id


def _stored_gateway_ref_hmacs(meta):
    if meta is None:
        return ()
    try:
        refs = json.loads(meta.value)
    except (TypeError, ValueError, RecursionError):
        return ()
    if (not isinstance(refs, list) or len(refs) >= MAX_GATEWAY_REFS
            or any(not isinstance(ref, str) or not ref for ref in refs)):
        return ()
    return tuple(dict.fromkeys(refs))


def receipt_ref_hmacs(session, receipt) -> frozenset[str]:
    if getattr(receipt, "gateway", None) is None:
        return frozenset()
    primary = getattr(receipt, "gateway_ref_hmac", None)
    receipt_id = getattr(receipt, "id", None)
    secondary = session.get(AppMeta, _gateway_ref_key(receipt_id)) if receipt_id is not None else None
    return frozenset(ref for ref in (primary, *_stored_gateway_ref_hmacs(secondary)) if ref is not None)


def _write_gateway_ref_hmacs(session, receipt_id, primary, refs):
    secondary = [ref for ref in dict.fromkeys(refs) if ref is not None and ref != primary]
    key = _gateway_ref_key(receipt_id)
    if secondary:
        session.merge(AppMeta(key=key, value=json.dumps(secondary)))
    else:
        session.execute(delete(AppMeta).where(AppMeta.key == key))


def _failed_gateway_prefix(owner):
    return _GATEWAY_FAILED_PREFIX + owner.encode("utf-8").hex() + ":"


def _failed_gateway_key(owner, gateway, ref_hmac):
    return f"{_failed_gateway_prefix(owner)}{gateway}:{ref_hmac}"


def _forget_failed_gateways(session, owner, now=None):
    prefix = _GATEWAY_FAILED_PREFIX if owner is None else _failed_gateway_prefix(owner)
    matches = AppMeta.key.startswith(prefix, autoescape=True)
    statement = delete(AppMeta).where(matches)
    if now is not None:
        # Other AppMeta values need not be JSON.
        value = case((matches, AppMeta.value))
        statement = statement.where(func.json_extract(value, "$.expires_at") <= now)
    session.execute(statement)


def _forget_payment_provenance(session, receipt_ids):
    keys = select(_payment_sender_key(receipt_ids)).union_all(
        select(_payment_order_key(receipt_ids)),
        select(_payment_payee_key(receipt_ids)),
        select(_gateway_ref_key(receipt_ids)),
    )
    session.execute(delete(AppMeta).where(AppMeta.key.in_(keys)))


def _forget_payment_senders(session, *predicates):
    receipts = select(BudgetGmailMessage.receipt_id).where(
        BudgetGmailMessage.receipt_id.is_not(None), *predicates,
    ).subquery()
    _forget_payment_provenance(session, receipts.c.receipt_id)


def _tombstone_messages(session, *predicates):
    session.execute(update(BudgetGmailMessage).where(*predicates).values(
        receipt_id=None, outcome="deleted",
        rfc_message_id_hmac=case((_linked_manual_forward(), None), else_=BudgetGmailMessage.rfc_message_id_hmac),
    ))


def tombstone_receipt(session, owner, receipt_id):
    owned_receipt = select(BudgetReceipt.id).where(BudgetReceipt.id == receipt_id, BudgetReceipt.owner == owner)
    keys = (_payment_sender_key(receipt_id), _payment_order_key(receipt_id), _payment_payee_key(receipt_id),
            _gateway_ref_key(receipt_id))
    session.execute(delete(AppMeta).where(AppMeta.key.in_(keys), owned_receipt.exists()))
    _tombstone_messages(session, BudgetGmailMessage.owner == owner, BudgetGmailMessage.receipt_id == receipt_id)


def _unconfirmed_import():
    # Confirmation sets a payment method. Ledger drift can later return the receipt to review.
    return or_(BudgetReceipt.status == "imported",
               and_(BudgetReceipt.status == "needs_review", BudgetReceipt.forwarded_manually == 1,
                    BudgetReceipt.payment_method.is_(None)))


def _delete_imports(session, owner, expires_at=None):
    receipts = select(BudgetReceipt.id).where(_unconfirmed_import())
    if owner is not None:
        receipts = receipts.where(BudgetReceipt.owner == owner)
    if expires_at is not None:
        # Earlier manual-review rows had no explicit expiry.
        deadline = func.coalesce(BudgetReceipt.import_expires_at, BudgetReceipt.created_at + IMPORT_TTL)
        receipts = receipts.where(deadline <= expires_at)
    _forget_payment_provenance(session, receipts.subquery().c.id)
    if expires_at is not None:
        _tombstone_messages(session, BudgetGmailMessage.receipt_id.in_(receipts))
    session.execute(delete(BudgetReceiptLine).where(BudgetReceiptLine.receipt_id.in_(receipts)))
    session.execute(delete(BudgetReceipt).where(BudgetReceipt.id.in_(receipts)))


def retain_imports(session, owner, now):
    _delete_imports(session, owner, now)
    expired = (BudgetGmailMessage.internal_date < now - PROVENANCE_TTL,)
    if owner is not None:
        expired += (BudgetGmailMessage.owner == owner,)
    _forget_payment_senders(session, *expired)
    session.execute(delete(BudgetGmailMessage).where(*expired))
    linked = select(BudgetGmailMessage.receipt_id).where(BudgetGmailMessage.receipt_id.is_not(None))
    orphaned = select(BudgetReceipt.id).where(BudgetReceipt.id.not_in(linked))
    if owner is not None:
        orphaned = orphaned.where(BudgetReceipt.owner == owner)
    _forget_payment_provenance(session, orphaned.subquery().c.id)
    _forget_failed_gateways(session, owner, now)


class GmailStore(BudgetStore):
    def state(self, owner):
        with self.sessions() as session:
            BudgetHTTPStore._owner(session, owner)
            return self._state(session, owner)

    @staticmethod
    def _state(session, owner):
        row = session.get(BudgetGmailState, owner)
        if row is not None:
            return row
        return BudgetGmailState(owner=owner, grant_status="disabled", grant_generation=0, version=1, **GRANT_RESET)

    @staticmethod
    def _active(row, generation):
        if row.grant_status != "active" or row.grant_generation != generation:
            raise GmailError("budget_receipt_gmail_disabled", 409)

    @staticmethod
    def _version(row, expected):
        if row.version != expected:
            raise GmailError("budget_receipt_gmail_stale_version", 409)

    def fence(self, owner, generation):
        self._active(self.state(owner), generation)

    def schedule_sync(self, owner, *, now, force_full_resync, cooldown, retry_parse_failed=False, backfill_days=365):
        """Queue a poller run. The request does not fetch or parse mail."""
        with self.transaction() as session:
            BudgetHTTPStore._owner(session, owner)
            row = self._state(session, owner)
            if row.grant_status != "active":
                raise GmailError("budget_receipt_gmail_disabled", 409)
            if row.attention == "identity_mismatch":
                raise GmailError("mailbox_mismatch", 403, attention="identity_mismatch")
            if row.backoff_until > now:
                raise GmailError("budget_receipt_gmail_backoff", 429)
            if not row.run_requested and row.last_attempt_at is not None and row.last_attempt_at + cooldown > now:
                raise GmailError("budget_receipt_gmail_cooldown", 429)
            if retry_parse_failed:
                cutoff = self._request_full_window(session, row, now, backfill_days)
                session.execute(delete(BudgetGmailMessage).where(
                    BudgetGmailMessage.owner == owner,
                    BudgetGmailMessage.outcome == "parse_failed",
                    BudgetGmailMessage.receipt_id.is_(None),
                    BudgetGmailMessage.internal_date >= cutoff,
                ))
            elif force_full_resync and (not row.run_requested or row.sync_phase != "full_resync"):
                row.force_full_resync = 1
            self._request_sync(row, now)
            return {"ok": True, "sync_phase": row.sync_phase, "version": row.version}

    @staticmethod
    def _request_sync(row, now):
        if not row.run_requested:
            row.next_sync_at = now
            row.run_requested = 1
            row.version += 1

    @staticmethod
    def _request_full_window(session, row, now, backfill_days):
        cutoff = max(0, int(now) - backfill_days * DAY)
        key = _retry_window_key(row.owner)
        meta = session.get(AppMeta, key)
        if meta is not None:
            cutoff = min(cutoff, json.loads(meta.value)["cutoff"])
        session.merge(AppMeta(key=key, value=json.dumps({"state": "pending", "cutoff": cutoff})))
        row.force_full_resync = 1
        return cutoff

    def retry_full_window(self, owner):
        with self.sessions() as session:
            return session.get(AppMeta, _retry_window_key(owner)) is not None

    def check_version(self, owner, expected):
        self._version(self.state(owner), expected)

    @staticmethod
    def _pending_count(session, owner):
        # Evidence rows attached to a shop document are not pending imports.
        return session.scalar(select(func.count()).select_from(BudgetReceipt).where(
            BudgetReceipt.owner == owner, _unconfirmed_import(),
            BudgetReceipt.evidence_for_receipt_id.is_(None)))

    @staticmethod
    def _parse_failed_reasons(session, owner):
        row = session.get(AppMeta, _reasons_key(owner))
        return _normalize_reason_counts(json.loads(row.value)) if row is not None else {}

    def status(self, owner):
        with self.sessions() as session:
            BudgetHTTPStore._owner(session, owner)
            row = self._state(session, owner)
            return {**{key: getattr(row, key) for key in STATUS_FIELDS},
                    "pending_imported_count": self._pending_count(session, owner),
                    "parse_failed_reasons": self._parse_failed_reasons(session, owner)}

    def at_pending_cap(self, owner):
        with self.sessions() as session:
            return self._pending_count(session, owner) >= MAX_PENDING_IMPORTS

    def reset_unconfirmed_payments(self, owner, generation, *, backfill_days, dry_run):
        """Clear eligible imports and queue a full backfill under the grant fence."""
        with self.transaction() as session:
            BudgetHTTPStore._owner(session, owner)
            row = self._state(session, owner)
            self._active(row, generation)
            if row.attention == "identity_mismatch":
                raise GmailError("mailbox_mismatch", 403, attention="identity_mismatch")
            receipts = select(BudgetReceipt).where(
                BudgetReceipt.owner == owner,
                BudgetReceipt.document_kind == "payment_confirmation",
                BudgetReceipt.status == "imported",
                BudgetReceipt.source_kind == "email",
                BudgetReceipt.linked_account_id.is_(None),
                BudgetReceipt.forwarded_manually == 0,
            )
            selected = session.scalars(receipts).all()
            # Capture the provenance before the tombstone path clears its receipt_id.
            messages = session.scalars(select(BudgetGmailMessage).where(
                BudgetGmailMessage.owner == owner,
                or_(BudgetGmailMessage.receipt_id.in_(receipts.with_only_columns(BudgetReceipt.id)),
                    BudgetGmailMessage.outcome == "skipped_duplicate",
                    and_(BudgetGmailMessage.outcome == "skipped_non_receipt",
                         BudgetGmailMessage.trust_path == "direct_allowlist")),
            )).all()
            for receipt in selected:
                self._delete_receipt(session, owner, receipt.id)
            for message in messages:
                session.delete(message)
            _forget_failed_gateways(session, owner)
            now = self.clock()
            self._request_full_window(session, row, now, backfill_days)
            self._request_sync(row, now)
            if dry_run:
                session.flush()
                session.rollback()
            return {"receipts": len(selected), "gmail_messages": len(messages)}

    @staticmethod
    def _delete_receipt(session, owner, receipt_id):
        tombstone_receipt(session, owner, receipt_id)
        session.execute(delete(BudgetReceiptLine).where(
            BudgetReceiptLine.receipt_id == receipt_id, BudgetReceiptLine.owner == owner))
        erase_receipt_audit(session, owner, receipt_id)
        session.delete(session.get(BudgetReceipt, receipt_id))

    def transition(self, owner, expected, status, mailbox_hmac=None, *, queue_sync=False):
        with self.transaction() as session:
            BudgetHTTPStore._owner(session, owner)
            row = self._state(session, owner)
            self._version(row, expected)
            now = self.clock()
            session.add(row)
            for key, value in GRANT_RESET.items():
                setattr(row, key, value)
            session.execute(delete(AppMeta).where(
                AppMeta.key.in_((_reasons_key(owner), _retry_window_key(owner), _inc_reset_key(owner))),
            ))
            row.grant_status = status
            row.grant_generation += 1
            row.version += 1
            if status == "active":
                row.mailbox_hmac = mailbox_hmac
                row.granted_at, row.revoked_at = now, None
                if queue_sync:
                    row.run_requested = 1
                    row.next_sync_at = now
            elif status == "revoked":
                row.revoked_at = now
                receipts = select(BudgetReceipt.id).where(BudgetReceipt.owner == owner).subquery()
                _forget_payment_provenance(session, receipts.c.id)
                _forget_failed_gateways(session, owner)
                session.execute(update(BudgetGmailMessage).where(BudgetGmailMessage.owner == owner)
                                .values(receipt_id=None))
                _delete_imports(session, owner)
                session.execute(delete(BudgetGmailMessage).where(BudgetGmailMessage.owner == owner))
            audit = {"action": {"active": "enable", "disabled": "disable", "revoked": "revoke"}[status],
                     "grant_status": status, "grant_generation": row.grant_generation, "version": row.version}
            session.add(BudgetAnalyticsAudit(owner=owner, subject_type="gmail_grant", subject_id="grant",
                                             action=audit["action"], before_json="{}",
                                             after_json=json.dumps(audit), changed_at=now))

    def opt_in_error(self, owner, expected, *, attention, code, http_status):
        with self.transaction() as session:
            BudgetHTTPStore._owner(session, owner)
            row = self._state(session, owner)
            self._version(row, expected)
            session.add(row)
            row.attention, row.last_error, row.last_http_status = attention, code, http_status
            if row.grant_status == "active" and attention == "identity_mismatch":
                row.grant_generation += 1

    def known(self, owner, gmail_hmac):
        with self.sessions() as session:
            return session.get(BudgetGmailMessage, (owner, gmail_hmac)) is not None

    @staticmethod
    def _duplicate(session, owner, rfc_hmac):
        return rfc_hmac is not None and session.scalar(select(BudgetGmailMessage.gmail_id_hmac).where(
            BudgetGmailMessage.owner == owner, BudgetGmailMessage.rfc_message_id_hmac == rfc_hmac,
            BudgetGmailMessage.outcome.in_(("imported", "deleted")), ~_linked_manual_forward(),
        ).limit(1)) is not None

    @staticmethod
    def _gateway_payees_match(payment, stored_hmac, stored_merchant):
        if payment.fields.payee_from_config or stored_hmac == "":
            return True
        if stored_hmac is None or payment.payee_hmac is None:
            # Older receipts lack the original payee provenance.
            return _payee_key(stored_merchant or "") == _payee_key(payment.fields.merchant)
        return stored_hmac == payment.payee_hmac

    @staticmethod
    def _duplicate_payment(session, owner, payment):
        fields = payment.fields
        sender, order_reference, payee = aliased(AppMeta), aliased(AppMeta), aliased(AppMeta)
        receipt_day = func.julianday(payment.receipt_date.isoformat())
        distance = func.abs(func.julianday(BudgetReceipt.receipt_date) - receipt_day)
        incoming_reference = payment.order_reference_hmac
        # `column == None` compiles to IS NULL; rank exact matches only for a known value.
        ranking = []
        if incoming_reference is not None:
            ranking.append((order_reference.value == incoming_reference).desc())
        if payment.payee_hmac is not None:
            ranking.append((payee.value == payment.payee_hmac).desc())
        candidates = session.execute(select(
            BudgetReceipt.id, order_reference.value, payee.value, BudgetReceipt.raw_shop_name,
        ).select_from(BudgetReceipt).join(
            sender, sender.key == _payment_sender_key(BudgetReceipt.id),
        ).outerjoin(
            order_reference, order_reference.key == _payment_order_key(BudgetReceipt.id),
        ).outerjoin(
            payee, payee.key == _payment_payee_key(BudgetReceipt.id),
        ).where(
            BudgetReceipt.owner == owner, BudgetReceipt.document_kind == "payment_confirmation",
            BudgetReceipt.total_amount == fields.total_amount, BudgetReceipt.currency == fields.currency,
            sender.value == payment.sender_hmac, distance <= 7,
        ).order_by(*ranking, distance, BudgetReceipt.id))
        for receipt_id, reference_hmac, payee_hmac, merchant in candidates:
            if reference_hmac is not None and incoming_reference is not None and reference_hmac != incoming_reference:
                continue
            if payment.gateway and not GmailStore._gateway_payees_match(payment, payee_hmac, merchant):
                continue
            if reference_hmac is None and incoming_reference is not None:
                session.add(AppMeta(key=_payment_order_key(receipt_id), value=incoming_reference))
            if payee_hmac == "" and payment.payee_hmac is not None:
                session.get(AppMeta, _payment_payee_key(receipt_id)).value = payment.payee_hmac
            return "skipped_duplicate"
        return None

    @staticmethod
    def _gateway_rows(session, owner, gateway, ref_hmac, secondary_ref_hmac=None):
        refs = frozenset(ref for ref in (ref_hmac, secondary_ref_hmac) if ref is not None)
        if not refs:
            return []
        secondary = aliased(AppMeta)
        secondary_match = and_(secondary.key.is_not(None), or_(
            *(secondary.value.like(f'%"{ref}"%') for ref in refs),
        ))
        candidates = session.execute(select(BudgetReceipt, secondary).outerjoin(
            secondary, secondary.key == _gateway_ref_key(BudgetReceipt.id),
        ).where(
            BudgetReceipt.owner == owner, BudgetReceipt.gateway == gateway,
            or_(BudgetReceipt.gateway_ref_hmac.in_(refs), secondary_match),
        ).order_by(BudgetReceipt.id)).all()
        return [row for row, stored in candidates
                if refs & {row.gateway_ref_hmac, *_stored_gateway_ref_hmacs(stored)}]

    def _merge_gateway(self, session, owner, payment, now):
        """Merge by any stored gateway ref. Returns None when the row must be inserted."""
        gateway, ref_hmac = payment.gateway_name, payment.gateway_ref_hmac
        secondary_ref_hmac = payment.gateway_ref_secondary_hmac
        rows = self._gateway_rows(session, owner, gateway, ref_hmac, secondary_ref_hmac)
        if len(rows) > 1:
            log.warning("gmail gateway merge ambiguous match_count=%d", len(rows))
            return "skipped_duplicate"
        row = rows[0] if rows else None
        incoming_refs = {ref for ref in (ref_hmac, secondary_ref_hmac) if ref is not None}
        if row is None:
            if payment.status == "registered":
                for ref in incoming_refs:
                    failed = session.get(AppMeta, _failed_gateway_key(owner, gateway, ref))
                    if failed is not None and json.loads(failed.value)["expires_at"] > now:
                        return "skipped_non_receipt"
            return None
        if payment.status == "registered" or row.currency != payment.fields.currency:
            return "skipped_duplicate"
        stored_payee = session.get(AppMeta, _payment_payee_key(row.id))
        if not self._gateway_payees_match(
            payment, stored_payee.value if stored_payee is not None else None, row.raw_shop_name,
        ):
            return "skipped_duplicate"
        refs = incoming_refs | receipt_ref_hmacs(session, row)
        session.execute(delete(AppMeta).where(AppMeta.key.in_(
            [_failed_gateway_key(owner, gateway, ref) for ref in refs],
        )))
        if row.gateway_status == "registered":
            if row.status == "imported":
                self._upgrade_gateway(session, owner, row, payment, now)
            elif row.total_amount == payment.fields.total_amount:
                # Owner-confirmed amounts never move; only the status annotation may.
                row.gateway_status = "paid"
        return "skipped_duplicate"

    def _upgrade_gateway(self, session, owner, row, payment, now):
        fields = payment.fields
        before = audit_payload(row, "gateway_upgrade")
        incoming = tuple(dict.fromkeys(ref for ref in (
            payment.gateway_ref_hmac, payment.gateway_ref_secondary_hmac,
        ) if ref is not None))
        stored_refs = session.get(AppMeta, _gateway_ref_key(row.id))
        existing = tuple(ref for ref in (*_stored_gateway_ref_hmacs(stored_refs), row.gateway_ref_hmac)
                         if ref is not None)
        collision = session.scalar(select(BudgetReceipt.id).where(
            BudgetReceipt.owner == owner, BudgetReceipt.gateway == row.gateway,
            BudgetReceipt.gateway_ref_hmac == payment.gateway_ref_hmac, BudgetReceipt.id != row.id,
        ).limit(1))
        primary = row.gateway_ref_hmac if collision is not None else payment.gateway_ref_hmac
        others = tuple(dict.fromkeys(ref for ref in (*existing, *incoming) if ref != primary))
        refs = (*others[-(MAX_GATEWAY_REFS - 1):], primary)
        _write_gateway_ref_hmacs(session, row.id, primary, refs)
        row.gateway_ref_hmac = primary
        if payment.payee_hmac is not None:
            stored_payee = session.get(AppMeta, _payment_payee_key(row.id))
            if stored_payee is None or stored_payee.value == "":
                session.merge(AppMeta(key=_payment_payee_key(row.id), value=payment.payee_hmac))
        receipt_date = payment.receipt_date.isoformat()
        evidence_changed = (row.total_amount, row.fee_amount, row.receipt_date) != (
            fields.total_amount, fields.fee_amount, receipt_date)
        row.gateway_status = "paid"
        if not fields.payee_from_config:
            row.raw_shop_name = fields.merchant
        row.fee_amount = fields.fee_amount
        row.total_amount = fields.total_amount
        if fields.payee_host is not None:
            row.payee_host = fields.payee_host
        row.receipt_date = receipt_date
        line = session.scalar(select(BudgetReceiptLine).where(
            BudgetReceiptLine.receipt_id == row.id, BudgetReceiptLine.owner == owner))
        if line is not None:
            line.gross_line_total = fields.total_amount
            line.net_line_total = fields.total_amount
        row.version += 1
        row.updated_at = now
        write_audit(session, owner, row, "gateway_upgrade", before, audit_payload(row, "gateway_upgrade"), now)
        self._attach_evidence(session, owner, row, revalidate=evidence_changed)

    def _failed_gateway(self, session, owner, failed, *, now, expires_at):
        """Remove an unconfirmed failed registration and keep its refs until expiry."""
        gateway_name, *ref_hmacs = failed
        rows = self._gateway_rows(session, owner, gateway_name, ref_hmacs[0],
                                  ref_hmacs[1] if len(ref_hmacs) > 1 else None)
        if len(rows) > 1:
            log.warning("gmail gateway failed disposition ambiguous match_count=%d", len(rows))
            return "skipped_duplicate"
        row = rows[0] if rows else None
        if row is not None:
            if row.gateway_status != "registered" or row.status != "imported" or row.linked_account_id is not None:
                return "skipped_non_receipt"
            self._delete_receipt(session, owner, row.id)
        if expires_at > now:
            for ref_hmac in dict.fromkeys(ref_hmacs):
                key = _failed_gateway_key(owner, gateway_name, ref_hmac)
                meta = session.get(AppMeta, key)
                if meta is None or json.loads(meta.value)["expires_at"] < expires_at:
                    session.merge(AppMeta(key=key, value=json.dumps({
                        "gateway": gateway_name, "ref_hmac": ref_hmac, "expires_at": expires_at,
                    })))
        return "skipped_non_receipt"

    @staticmethod
    def _evidence_match(gateway_row, document):
        if gateway_row.currency is None or gateway_row.currency != document.currency:
            return False
        if gateway_row.total_amount is None or document.total_amount is None:
            return False
        if gateway_row.receipt_date is None or document.receipt_date is None:
            return False
        total = Decimal(gateway_row.total_amount)
        document_total = Decimal(document.total_amount)
        fee = None if gateway_row.fee_amount is None else Decimal(gateway_row.fee_amount)
        if document_total != total and (fee is None or document_total != total - fee):
            return False
        gateway_date = date.fromisoformat(gateway_row.receipt_date)
        document_date = date.fromisoformat(document.receipt_date)
        if document.document_kind == "invoice":
            due = date.fromisoformat(document.due_date) if document.due_date else document_date
            return document_date - timedelta(days=1) <= gateway_date <= max(due, document_date) + timedelta(days=14)
        return abs((gateway_date - document_date).days) <= 1

    def _evidence_candidates(self, session, owner, gateway_row):
        documents = session.scalars(select(BudgetReceipt).where(
            BudgetReceipt.owner == owner, BudgetReceipt.gateway.is_(None),
            BudgetReceipt.evidence_for_receipt_id.is_(None), BudgetReceipt.status != "draft",
            BudgetReceipt.document_kind.in_(("invoice", "receipt", "payment_confirmation")),
            BudgetReceipt.receipt_kind == gateway_row.receipt_kind,
            BudgetReceipt.currency == gateway_row.currency,
        )).all()
        qualified = [document for document in documents if self._evidence_match(gateway_row, document)]
        preferred = [document for document in qualified
                     if (document.raw_shop_name and gateway_row.raw_shop_name
                         and _payee_key(document.raw_shop_name) == _payee_key(gateway_row.raw_shop_name))
                     or (document.raw_nip and gateway_row.raw_nip and document.raw_nip == gateway_row.raw_nip)]
        return preferred or qualified

    def _attach_evidence(self, session, owner, gateway_row, *, revalidate=False):
        """Attach a gateway row to its shop document only when exactly one qualifies."""
        if gateway_row.evidence_for_receipt_id is not None:
            if not revalidate or session.get(BudgetReceipt, gateway_row.evidence_for_receipt_id).status != "imported":
                return
        candidates = self._evidence_candidates(session, owner, gateway_row)
        gateway_row.evidence_for_receipt_id = candidates[0].id if len(candidates) == 1 else None

    def _attach_gateway_for_document(self, session, owner, document):
        """Reverse check on document insert: attach a unique unattached gateway row."""
        if document.gateway is not None or document.evidence_for_receipt_id is not None:
            return
        gateway_rows = session.scalars(select(BudgetReceipt).where(
            BudgetReceipt.owner == owner, BudgetReceipt.gateway.is_not(None),
            BudgetReceipt.evidence_for_receipt_id.is_(None), BudgetReceipt.status == "imported",
            BudgetReceipt.receipt_kind == document.receipt_kind,
            BudgetReceipt.currency == document.currency,
        )).all()
        for gateway_row in gateway_rows:
            candidates = self._evidence_candidates(session, owner, gateway_row)
            if len(candidates) == 1 and candidates[0].id == document.id:
                gateway_row.evidence_for_receipt_id = document.id

    @staticmethod
    def _invoice(session, owner, item, now):
        invoice = item.invoice
        receipt = BudgetReceipt(id=str(uuid.uuid4()), owner=owner, raw_shop_name=invoice.issuer, raw_nip=invoice.nip,
                                invoice_number=invoice.invoice_number, receipt_date=invoice.issue_date,
                                due_date=invoice.due_date, total_amount=invoice.amount, currency=invoice.currency,
                                status="imported", document_kind="invoice", source_kind=item.source_kind,
                                version=1, created_at=now, updated_at=now, import_expires_at=now + IMPORT_TTL)
        session.add(receipt)
        session.flush()
        return receipt.id

    @staticmethod
    def _payment(session, owner, payment, now):
        fields = payment.fields
        receipt = BudgetReceipt(
            id=str(uuid.uuid4()), owner=owner, raw_shop_name=fields.merchant,
            receipt_date=payment.receipt_date.isoformat(), total_amount=fields.total_amount, currency=fields.currency,
            status="imported", document_kind="payment_confirmation", source_kind="email",
            gateway=payment.gateway_name, gateway_status=payment.status,
            gateway_ref_hmac=payment.gateway_ref_hmac, fee_amount=fields.fee_amount,
            payee_host=fields.payee_host,
            version=1, created_at=now, updated_at=now, import_expires_at=now + IMPORT_TTL,
        )
        session.add(receipt)
        session.flush()
        session.add(BudgetReceiptLine(
            receipt_id=receipt.id, owner=owner, line_no=1, line_kind="other", raw_name=fields.description,
            raw_name_key=fold_text(fields.description)[:128], quantity="1",
            gross_line_total=fields.total_amount, net_line_total=fields.total_amount,
        ))
        session.add(AppMeta(key=_payment_sender_key(receipt.id), value=payment.sender_hmac))
        refs = tuple(dict.fromkeys(ref for ref in (
            payment.gateway_ref_hmac, payment.gateway_ref_secondary_hmac,
        ) if ref is not None))
        if payment.gateway_ref_secondary_hmac is not None:
            _write_gateway_ref_hmacs(session, receipt.id, payment.gateway_ref_hmac, refs)
        if payment.gateway_ref_hmac is not None and payment.status == "paid":
            for ref_hmac in sorted(refs):
                key = _failed_gateway_key(owner, payment.gateway_name, ref_hmac)
                session.execute(delete(AppMeta).where(AppMeta.key == key))
        if payment.order_reference_hmac is not None:
            session.add(AppMeta(key=_payment_order_key(receipt.id), value=payment.order_reference_hmac))
        if payment.gateway and (fields.payee_from_config or payment.payee_hmac is not None):
            session.add(AppMeta(key=_payment_payee_key(receipt.id), value=payment.payee_hmac or ""))
        return receipt.id

    @staticmethod
    def _manual(session, owner, item, now):
        """A manual forward waits for the owner. No body, full address, or transaction link is stored."""
        receipt = BudgetReceipt(
            id=str(uuid.uuid4()), owner=owner, raw_shop_name="przekazane ręcznie",
            forwarded_sender_domain=item.sender_domain, forwarded_manually=1,
            status="needs_review", document_kind="invoice", source_kind=item.source_kind,
            version=1, created_at=now, updated_at=now, import_expires_at=now + IMPORT_TTL)
        session.add(receipt)
        session.flush()
        return receipt.id

    def commit(self, owner, generation, changes, pending=(), *, starting_recovery=False, starting_incremental=False):
        """Commit up to one chunk; never advance a page past an uncommitted message."""
        imported = skipped = 0
        drained = True
        new_reasons = []
        reset_reasons = False
        with self.transaction() as session:
            BudgetHTTPStore._owner(session, owner)
            row = self._state(session, owner)
            self._active(row, generation)
            now = self.clock()
            for item in pending:
                if session.get(BudgetGmailMessage, (owner, item.gmail_id_hmac)) is not None:
                    continue
                receipt_id = None
                outcome = item.outcome
                # Review placeholders are keyed by their outer Gmail ID.
                rfc_hmac = None if item.forwarded_manually else item.rfc_message_id_hmac
                if self._duplicate(session, owner, rfc_hmac):
                    outcome = "skipped_duplicate"
                elif item.failed_gateway is not None:
                    outcome = self._failed_gateway(session, owner, item.failed_gateway, now=now,
                                                   expires_at=item.internal_date + PROVENANCE_TTL)
                elif item.payment is not None:
                    if item.payment.gateway_ref_hmac is not None:
                        outcome = self._merge_gateway(session, owner, item.payment, now) or outcome
                    else:
                        outcome = self._duplicate_payment(session, owner, item.payment) or outcome
                if outcome == "imported":
                    if self._pending_count(session, owner) >= MAX_PENDING_IMPORTS:
                        drained = False
                        break
                    if item.forwarded_manually:
                        receipt_id = self._manual(session, owner, item, now)
                    elif item.payment is not None:
                        receipt_id = self._payment(session, owner, item.payment, now)
                    else:
                        receipt_id = self._invoice(session, owner, item, now)
                    imported += 1
                    receipt_row = session.get(BudgetReceipt, receipt_id)
                    if item.payment is not None and item.payment.gateway_name is not None:
                        self._attach_evidence(session, owner, receipt_row)
                    elif not item.forwarded_manually:
                        self._attach_gateway_for_document(session, owner, receipt_row)
                else:
                    skipped += 1
                    if outcome == "parse_failed":
                        new_reasons.append(normalize_parse_failed_reason(item.parse_failed_reason))
                session.add(BudgetGmailMessage(owner=owner, gmail_id_hmac=item.gmail_id_hmac,
                                               rfc_message_id_hmac=rfc_hmac,
                                               internal_date=item.internal_date, trust_path=item.trust_path,
                                               outcome=outcome, created_at=now, receipt_id=receipt_id))
            if row.sync_phase in ("backfill", "full_resync"):
                row.batch_processed_count += imported + skipped
            if starting_incremental:
                session.merge(AppMeta(key=_inc_reset_key(owner), value="1"))
            if imported or skipped:
                inc_meta = session.get(AppMeta, _inc_reset_key(owner))
                if inc_meta is not None:
                    reset_reasons = True
                    session.delete(inc_meta)
            if starting_recovery:
                session.execute(delete(AppMeta).where(AppMeta.key == _inc_reset_key(owner)))
                retry_meta = session.get(AppMeta, _retry_window_key(owner))
                if retry_meta is not None:
                    marker = json.loads(retry_meta.value)
                    changes = {
                        **changes,
                        "resume_cutoff_ts": min(changes["resume_cutoff_ts"], marker["cutoff"]),
                        "force_full_resync": 0,
                        "sync_phase": "full_resync",
                    }
                    if marker["state"] == "pending":
                        reset_reasons = True
                        retry_meta.value = json.dumps({"state": "running", "cutoff": marker["cutoff"]})
                else:
                    reset_reasons = True
            if reset_reasons or new_reasons:
                key = _reasons_key(owner)
                meta = session.get(AppMeta, key)
                counts = {} if reset_reasons or meta is None else _normalize_reason_counts(json.loads(meta.value))
                for reason in new_reasons:
                    counts[reason] = counts.get(reason, 0) + 1
                if counts:
                    encoded = json.dumps(counts, sort_keys=True)
                    if meta is None:
                        session.add(AppMeta(key=key, value=encoded))
                    else:
                        meta.value = encoded
                elif meta is not None:
                    session.delete(meta)
            if drained:
                was_recovery = row.sync_phase != "incremental"
                for key, value in changes.items():
                    setattr(row, key, value)
                if changes.get("sync_phase") == "idle":
                    # A force request may arrive after the worker chose what to scan.
                    row.run_requested = int(bool(row.force_full_resync))
                    if was_recovery:
                        retry_meta = session.get(AppMeta, _retry_window_key(owner))
                        if retry_meta is not None and json.loads(retry_meta.value)["state"] == "running":
                            session.delete(retry_meta)
                    else:
                        session.execute(delete(AppMeta).where(AppMeta.key == _inc_reset_key(owner)))
        return imported, skipped, drained

    def retain(self, owner, generation):
        with self.transaction() as session:
            BudgetHTTPStore._owner(session, owner)
            self._active(self._state(session, owner), generation)
            retain_imports(session, owner, self.clock())
