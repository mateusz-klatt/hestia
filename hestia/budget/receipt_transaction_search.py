"""Owner-private stored transaction search and manual selection validation."""
from __future__ import annotations

from datetime import date
import hashlib
import hmac
import json
import re
from typing import NamedTuple

from sqlalchemy import and_, bindparam, exists, func, literal_column, or_, select, text

from ..db import (
    BudgetAccount as Account, BudgetAccountDisplay as Display, BudgetConnection as Connection,
    BudgetReceipt as Receipt, BudgetReceiptLinkGroup as Group, BudgetReceiptLinkMember as Member,
    BudgetTransaction as Transaction, BudgetTransactionMetadata as Metadata, BudgetTransferLeg as Leg,
)
from .analytics_core import AnalyticsError, canonical_money, compare_money, financial_fingerprint
from .receipt_parser import ReceiptError, fold_text, normalize_shop_name, redact_digit_field, redact_pan
from .receipt_reconcile import _payload_party_name, evidence_json, link_evidence, resolved_party_names
from .receipt_store import BudgetReceiptStore, _ROW_ID, _enabled_account_filters, _json, _shift_clamped, _short_account_label
from .views import BudgetHTTPStore, BudgetQueryError, _date_filter

MANUAL_LINK_VERSION = (0, 67, 0)
_FIRST_DAY = "0001-01-01"
_BOOKING_DATE = literal_column("""CASE WHEN json_valid(budget_transactions.payload_json) THEN coalesce(
    json_extract(budget_transactions.payload_json, '$.booking_date'),
    json_extract(budget_transactions.payload_json, '$.value_date'),
    json_extract(budget_transactions.payload_json, '$.transaction_date'),
    '0001-01-01'
) END""")
_ACTIVE_LINK = text("budget_receipts.status IN ('confirmed', 'needs_review')")
_MASKED_SUFFIX = re.compile(r"[A-Za-z0-9]{1,4}\Z")
_ACCOUNT_DIGITS = re.compile(r"\d{8,}")
_STALE = "budget_receipt_stale_source"
_OCCUPIED = "budget_receipt_tx_already_linked"


class SearchFilters(NamedTuple):
    date_from: str | None
    date_to: str | None
    amount_min: str | None
    amount_max: str | None
    query: str
    account_id: str | None


def _date_bound(values, key, receipt_date, offset):
    if key in values:
        return _date_filter(values[key])
    return _shift_clamped(date.fromisoformat(receipt_date), offset).isoformat() if receipt_date else None


def _filters(values, receipt):
    try:
        date_from = _date_bound(values, "date_from", receipt.receipt_date, -14)
        date_to = _date_bound(values, "date_to", receipt.receipt_date, 30)
    except (BudgetQueryError, ValueError):
        raise ReceiptError() from None
    amount_min, amount_max = values.get("amount_min"), values.get("amount_max")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ReceiptError()
    if amount_min is not None and amount_max is not None and compare_money(amount_min, amount_max) > 0:
        raise ReceiptError()
    return SearchFilters(date_from, date_to, amount_min, amount_max,
                         fold_text((values.get("query") or "").strip()), values.get("account_id"))


def _bank_text(payload, metadata_json, metadata_fingerprint, fingerprint):
    creditor = None
    if metadata_fingerprint == fingerprint:
        try:
            creditor = _payload_party_name(_json(metadata_json), "creditor")
        except (TypeError, ValueError, RecursionError):
            pass
    title = payload.get("title") or payload.get("description")
    lines = payload.get("remittance_information")
    titles = [title] if isinstance(title, str) and title else []
    if isinstance(lines, list):
        titles.extend(line for line in lines if isinstance(line, str) and line)
    return creditor or _payload_party_name(payload, "creditor"), titles


def _matches(payload_json, metadata_json, metadata_fingerprint, minimum, maximum, query):
    """SQLite predicate for exact decimal bounds and literal, Unicode-folded text."""
    try:
        payload = _json(payload_json)
        fingerprint = financial_fingerprint(payload)
    except (AnalyticsError, TypeError, ValueError, RecursionError):
        return False
    amount = payload["amount"]
    if minimum is not None and compare_money(amount, minimum) < 0:
        return False
    if maximum is not None and compare_money(amount, maximum) > 0:
        return False
    if not query:
        return True
    creditor, titles = _bank_text(payload, metadata_json, metadata_fingerprint, fingerprint)
    return any(query in fold_text(_private_text(value) or "") for value in [creditor, *titles])


def _private_text(value):
    if not value or not value.strip():
        return None
    try:
        masked = _ACCOUNT_DIGITS.sub("••••", redact_pan(value))
        return redact_digit_field(masked)[:240]
    except ReceiptError:
        return None


def _other_receipt(owner, receipt_id, account_id, reference):
    return exists().where(
        Receipt.owner == owner, Receipt.id != receipt_id, _ACTIVE_LINK,
        Receipt.linked_account_id.is_not(None), Receipt.linked_entry_ref.is_not(None),
        Receipt.linked_account_id == account_id, Receipt.linked_entry_ref == reference,
    )


def _group_reserved(owner, account_id, reference, *, own_receipt_id=None):
    reservation = exists().where(Group.owner == owner, Group.account_id == account_id, Group.entry_reference == reference)
    if own_receipt_id is not None:
        own_member = exists().where(Member.receipt_id == own_receipt_id, Member.owner == owner,
                                    Member.group_id == Group.id)
        reservation = reservation.where(Group.evidence_receipt_id.is_distinct_from(own_receipt_id), ~own_member)
    return reservation


def _transfer_leg(account_id, reference):
    return exists().where(Leg.account_id == account_id, Leg.entry_reference == reference)


def _query(owner, receipt, filters, after, limit):
    account_id, reference = Transaction.account_id, Transaction.entry_reference
    payload = Transaction.payload_json
    # CASE protects the JSON functions even when SQLite reorders predicates.
    valid_payload = func.iif(func.json_valid(payload), payload, None)
    statement = (
        select(_BOOKING_DATE.label("booking_date"), _ROW_ID.label("row_id"), account_id, reference, payload,
               Connection.bank_name, Display.product, Display.masked_suffix,
               Metadata.payload_json.label("metadata_json"), Metadata.ledger_fingerprint)
        .select_from(Account)
        .join(Transaction, account_id == Account.id)
        .join(Connection, Connection.id == Account.connection_id)
        .outerjoin(Display, Display.account_id == account_id)
        .outerjoin(Metadata, and_(Metadata.account_id == account_id, Metadata.entry_reference == reference,
                                  Metadata.owner == owner))
        .where(
            *_enabled_account_filters(owner, require_enabled_connection=False),
            Transaction.uncertain.is_(False),
            _BOOKING_DATE >= bindparam("picker_start", filters.date_from or _FIRST_DAY),
            _BOOKING_DATE <= bindparam("picker_end", filters.date_to or date.max.isoformat()),
            func.json_extract(valid_payload, "$.status") == "BOOK",
            func.json_extract(valid_payload, "$.credit_debit_indicator") == "DBIT",
            func.json_extract(valid_payload, "$.currency") == receipt.currency,
            ~_other_receipt(owner, receipt.id, account_id, reference),
            ~_group_reserved(owner, account_id, reference, own_receipt_id=receipt.id),
            ~_transfer_leg(account_id, reference),
            func.hestia_receipt_tx_matches(payload, Metadata.payload_json, Metadata.ledger_fingerprint,
                                           filters.amount_min, filters.amount_max, filters.query),
        )
        .order_by(_BOOKING_DATE.desc(), _ROW_ID)
        .limit(limit + 1)
    )
    if filters.account_id is not None:
        statement = statement.where(account_id == filters.account_id)
    if after is not None:
        last_date, last_id = after
        boundary = bindparam("picker_boundary", last_date)
        boundary_id = bindparam("picker_boundary_id", int(last_id))
        statement = statement.where(_BOOKING_DATE <= boundary, or_(_BOOKING_DATE < boundary, _ROW_ID > boundary_id))
    return statement


class ReceiptTransactionSearch(BudgetReceiptStore):
    def _transaction_id(self, owner, receipt_id, row_id, account_id, reference, fingerprint):
        data = json.dumps([owner, receipt_id, row_id, account_id, reference, fingerprint], ensure_ascii=True)
        digest = hmac.new(self._evidence_key, b"manual-transaction-v1\0" + data.encode("ascii"), hashlib.sha256)
        return f"{row_id}.{digest.hexdigest()}"

    def search_transactions(self, owner, values):
        with self.sessions() as session:
            session.execute(text("BEGIN"))
            owner = self._owner(session, owner, "budget.receipts.match", require_same_target=True)
            receipt = self._owned(session, owner, values["receipt_id"])
            filters = _filters(values, receipt)
            context = {"operation": "receipt-transaction-search", "owner": owner, "receipt": receipt.id,
                       "currency": receipt.currency, **filters._asdict()}
            now = self._now()
            try:
                after = BudgetHTTPStore._decode_cursor(self, values.get("cursor"), context, now)
            except BudgetQueryError:
                raise ReceiptError() from None
            connection = session.connection().connection.driver_connection
            connection.create_function("hestia_receipt_tx_matches", 6, _matches, deterministic=True)
            limit = values.get("limit", 25)
            results = session.execute(_query(owner, receipt, filters, after, limit)).all()
            page = results[:limit]
            next_cursor = None
            if len(results) > limit:
                last = page[-1]
                next_cursor = self._cursor(context, (last.booking_date, str(last.row_id)), now)
            return {"items": [self._item(owner, receipt.id, entry) for entry in page], "next_cursor": next_cursor}

    def _item(self, owner, receipt_id, entry):
        payload = _json(entry.payload_json)
        fingerprint = financial_fingerprint(payload)
        creditor, titles = _bank_text(payload, entry.metadata_json, entry.ledger_fingerprint, fingerprint)
        suffix = entry.masked_suffix
        label = _short_account_label(
            _private_text(entry.bank_name) or "", _private_text(entry.product),
            suffix if suffix and _MASKED_SUFFIX.fullmatch(suffix) else None,
        )
        return {
            "id": self._transaction_id(owner, receipt_id, entry.row_id, entry.account_id,
                                       entry.entry_reference, fingerprint),
            "date": payload.get("booking_date") or payload.get("value_date") or payload.get("transaction_date"),
            "amount": canonical_money(payload["amount"]), "currency": payload["currency"],
            "account_label": label, "counterparty": _private_text(creditor),
            "title": _private_text("\n".join(titles)),
        }

    def confirm_manual(self, owner, values):
        # Supply the established confirmation method with its internal link shape.
        # _confirmed_link fills both ledger keys inside the confirmation transaction.
        link = {"account_id": "manual", "entry_reference": "manual", "candidate_token": values["link"]["transaction_id"]}
        return self.confirm(owner, {**values, "link": link})

    def _confirmed_link(self, session, owner, cleaned, link, row):
        transaction_id = link["candidate_token"]
        row_id = int(transaction_id.split(".", 1)[0])
        if row_id > 2**63 - 1:
            raise ReceiptError(_STALE, 409)
        transaction = session.scalar(
            select(Transaction).join(Account).join(Connection).where(
                _ROW_ID == row_id, *_enabled_account_filters(owner, require_enabled_connection=False),
                Transaction.uncertain.is_(False),
            )
        )
        if transaction is None or cleaned["receipt_kind"] != "purchase" or cleaned["currency"] != row.currency:
            raise ReceiptError(_STALE, 409)
        account_id, reference = transaction.account_id, transaction.entry_reference
        try:
            payload = _json(transaction.payload_json)
            fingerprint = financial_fingerprint(payload)
        except (AnalyticsError, TypeError, ValueError, RecursionError):
            raise ReceiptError(_STALE, 409) from None
        if (payload["status"] != "BOOK" or payload["credit_debit_indicator"] != "DBIT"
                or payload["currency"] != row.currency
                or not hmac.compare_digest(transaction_id, self._transaction_id(
                    owner, row.id, row_id, account_id, reference, fingerprint,
                ))):
            raise ReceiptError(_STALE, 409)
        if session.scalar(select(
            _other_receipt(owner, row.id, account_id, reference) | _group_reserved(owner, account_id, reference)
            | _transfer_leg(account_id, reference)
        )):
            raise ReceiptError(_OCCUPIED, 409)
        creditor, debtor = resolved_party_names(session, owner, account_id, reference, payload, fingerprint)
        evidence = link_evidence(
            owner, account_id, reference, payload, normalize_shop_name(cleaned["raw_shop_name"]),
            cleaned["raw_nip"], creditor, fingerprint, debtor=debtor,
        )
        link.update(account_id=account_id, entry_reference=reference)
        return fingerprint, evidence_json(evidence)
