"""SQLite grant fences, invoice storage and keyed Gmail provenance."""
from __future__ import annotations

from dataclasses import dataclass
import hmac
import json
import uuid

from sqlalchemy import and_, case, delete, func, or_, select, update

from ..db import AppMeta, BudgetAnalyticsAudit, BudgetGmailMessage, BudgetGmailState, BudgetReceipt, BudgetReceiptLine
from .gmail_client import GmailError
from .store import BudgetStore
from .views import BudgetHTTPStore

DAY = 86400
IMPORT_TTL = 30 * DAY
PROVENANCE_TTL = 400 * DAY
MAX_PENDING_IMPORTS = 200
GMAIL_MESSAGE_DOMAIN = b"hestia/budget/gmail-msg-id/v1\0"
RFC_MESSAGE_DOMAIN = b"hestia/budget/gmail-rfc-msg-id/v1\0"
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


def digest(secret, domain, value):
    return hmac.digest(secret, domain + value.encode("ascii"), "sha256").hex()


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


def _linked_manual_forward():
    return select(BudgetReceipt.id).where(
        BudgetReceipt.id == BudgetGmailMessage.receipt_id, BudgetReceipt.forwarded_manually == 1,
    ).exists()


def _tombstone_messages(session, *predicates):
    session.execute(update(BudgetGmailMessage).where(*predicates).values(
        receipt_id=None, outcome="deleted",
        rfc_message_id_hmac=case((_linked_manual_forward(), None), else_=BudgetGmailMessage.rfc_message_id_hmac),
    ))


def tombstone_receipt(session, owner, receipt_id):
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
        _tombstone_messages(session, BudgetGmailMessage.receipt_id.in_(receipts))
    session.execute(delete(BudgetReceiptLine).where(BudgetReceiptLine.receipt_id.in_(receipts)))
    session.execute(delete(BudgetReceipt).where(BudgetReceipt.id.in_(receipts)))


def retain_imports(session, owner, now):
    _delete_imports(session, owner, now)
    prune = delete(BudgetGmailMessage).where(BudgetGmailMessage.internal_date < now - PROVENANCE_TTL)
    if owner is not None:
        prune = prune.where(BudgetGmailMessage.owner == owner)
    session.execute(prune)


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
                cutoff = max(0, now - backfill_days * DAY)
                session.execute(delete(BudgetGmailMessage).where(
                    BudgetGmailMessage.owner == owner,
                    BudgetGmailMessage.outcome == "parse_failed",
                    BudgetGmailMessage.receipt_id.is_(None),
                    BudgetGmailMessage.internal_date >= cutoff,
                ))
                key = _retry_window_key(owner)
                meta = session.get(AppMeta, key)
                if meta is None:
                    session.add(AppMeta(key=key, value="pending"))
                else:
                    meta.value = "pending"
                force_full_resync = True
            if row.run_requested:
                if force_full_resync and (retry_parse_failed or row.sync_phase != "full_resync"):
                    row.force_full_resync = 1
                return {"ok": True, "sync_phase": row.sync_phase, "version": row.version}
            session.add(row)
            row.next_sync_at = now
            row.run_requested = 1
            if force_full_resync:
                row.force_full_resync = 1
            row.version += 1
            return {"ok": True, "sync_phase": row.sync_phase, "version": row.version}

    def retry_full_window(self, owner):
        with self.sessions() as session:
            return session.get(AppMeta, _retry_window_key(owner)) is not None

    def has_pending_retry_window(self, owner):
        with self.sessions() as session:
            row = session.get(AppMeta, _retry_window_key(owner))
            return row is not None and row.value != "running"

    def check_version(self, owner, expected):
        self._version(self.state(owner), expected)

    @staticmethod
    def _pending_count(session, owner):
        return session.scalar(select(func.count()).select_from(BudgetReceipt).where(
            BudgetReceipt.owner == owner, _unconfirmed_import()))

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

    def transition(self, owner, expected, status, mailbox_hmac=None, *, queue_sync=False):
        with self.transaction() as session:
            BudgetHTTPStore._owner(session, owner)
            row = self._state(session, owner)
            self._version(row, expected)
            now = self.clock()
            session.add(row)
            for key, value in GRANT_RESET.items():
                setattr(row, key, value)
            session.execute(delete(AppMeta).where(AppMeta.key.in_((_reasons_key(owner), _retry_window_key(owner)))))
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

    def commit(self, owner, generation, changes, pending=(), *, reset_reasons=False, retry_full_cutoff=None):
        """Commit up to one chunk; never advance a page past an uncommitted message."""
        imported = skipped = 0
        drained = True
        new_reasons = []
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
                if outcome == "imported":
                    if self._pending_count(session, owner) >= MAX_PENDING_IMPORTS:
                        drained = False
                        break
                    receipt_id = (self._manual(session, owner, item, now) if item.forwarded_manually
                                  else self._invoice(session, owner, item, now))
                    imported += 1
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
            if retry_full_cutoff is not None:
                retry_meta = session.get(AppMeta, _retry_window_key(owner))
                if retry_meta is not None:
                    changes = {**changes, "resume_cutoff_ts": retry_full_cutoff}
                    retry_meta.value = "running"
            if drained:
                was_recovery = row.sync_phase != "incremental"
                for key, value in changes.items():
                    setattr(row, key, value)
                if changes.get("sync_phase") == "idle":
                    # A force request may arrive after the worker chose what to scan.
                    row.run_requested = int(bool(row.force_full_resync))
                    if was_recovery:
                        session.execute(delete(AppMeta).where(
                            AppMeta.key == _retry_window_key(owner),
                            AppMeta.value == "running",
                        ))
        return imported, skipped, drained

    def retain(self, owner, generation):
        with self.transaction() as session:
            BudgetHTTPStore._owner(session, owner)
            self._active(self._state(session, owner), generation)
            retain_imports(session, owner, self.clock())
