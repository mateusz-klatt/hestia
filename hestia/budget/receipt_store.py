"""Owner-private receipt persistence, exact bank matching and retention sweeps."""
from __future__ import annotations

import base64
from datetime import date
import hashlib
import hmac
import json
import math
import re
from types import SimpleNamespace
import uuid

from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy.exc import IntegrityError

from ..db import (BudgetAccount as Account, BudgetConnection as Connection, BudgetReceipt as Receipt,
                  BudgetReceiptLine as Line, BudgetShop as Shop, BudgetTransaction as Transaction,
                  BudgetTransferLeg as Leg)
from .analytics_core import (AnalyticsError, canonical_money, compare_money, financial_fingerprint, negate_money,
                             sum_money)
from .receipt_parser import (CONFIRMED_TEXT_TTL_SECONDS, DRAFT_TTL_SECONDS, MAX_RECEIPT_LINES, ReceiptError,
                             merchant_tokens, parse_receipt, validate_confirm)
from .receipt_reconcile import (_EVIDENCE_KEYS, audit_payload, creditor_name, evidence_json, erase_receipt_audit,
                                link_evidence, write_audit)
from .views import BudgetHTTPStore

MAX_DRAFTS = 20
MAX_CANDIDATES = 100
CURSOR_TTL_SECONDS = 900
MAX_CURSOR_CHARS = 8192
_CURSOR_DOMAIN = b"hestia/budget/receipts/cursor/v1"
_CANDIDATE_DOMAIN = b"hestia/budget/receipts/candidate/v1"
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z", re.ASCII)
_MONTH = re.compile(r"[0-9]{4}-[0-9]{2}\Z", re.ASCII)
_SUMMARY_FIELDS = (
    "receipt_id", "shop_id", "chain_key", "raw_shop_name", "raw_nip", "receipt_kind", "receipt_date",
    "receipt_time", "total_amount", "currency", "payment_method", "status", "source_kind", "linked",
    "linked_account_id", "linked_entry_ref", "version", "created_at", "updated_at", "image_available",
    "image_expires_at", "raw_text_expires_at", "math_delta",
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


def _date_distance(payload, receipt_date):
    if not _valid_date(receipt_date):
        return None
    target = date.fromisoformat(receipt_date)
    distances = [abs((date.fromisoformat(value) - target).days)
                 for value in (payload.get("transaction_date"), payload.get("booking_date"), payload.get("value_date"))
                 if _valid_date(value)]
    return min(distances) if distances else None


def _line_dict(row):
    return {
        "line_no": row.line_no, "line_kind": row.line_kind, "raw_name": row.raw_name,
        "raw_name_key": row.raw_name_key, "ptu_code": row.ptu_code, "quantity": row.quantity,
        "raw_unit": row.raw_unit, "unit_price_raw": row.unit_price_raw,
        "gross_line_total": row.gross_line_total, "discount_amount": row.discount_amount,
        "net_line_total": row.net_line_total,
    }


class BudgetReceiptStore(BudgetHTTPStore):
    def __init__(self, sessions, *, clock, cursor_secret):
        super().__init__(sessions, clock=clock, cursor_secret=cursor_secret)
        self._receipt_cursor_key = hashlib.sha256(_CURSOR_DOMAIN + b"\0" + cursor_secret).digest()
        self._candidate_key = hashlib.sha256(_CANDIDATE_DOMAIN + b"\0" + cursor_secret).digest()

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
        if row.total_amount is None:
            return None
        net = sum_money(session.scalars(select(Line.net_line_total).where(
            Line.receipt_id == row.id, Line.owner == row.owner)))
        return sum_money((net, negate_money(row.total_amount)))

    def _summary(self, session, row):
        shop = None if row.shop_id is None else session.get(Shop, row.shop_id)
        values = {
            "receipt_id": row.id, "shop_id": row.shop_id, "chain_key": None if shop is None else shop.chain_key,
            "raw_shop_name": row.raw_shop_name, "raw_nip": row.raw_nip, "receipt_kind": row.receipt_kind,
            "receipt_date": row.receipt_date, "receipt_time": row.receipt_time, "total_amount": row.total_amount,
            "currency": row.currency, "payment_method": row.payment_method, "status": row.status,
            "source_kind": row.source_kind, "linked": row.linked_account_id is not None,
            "linked_account_id": row.linked_account_id, "linked_entry_ref": row.linked_entry_ref,
            "version": row.version, "created_at": row.created_at, "updated_at": row.updated_at,
            "image_available": row.image_relpath is not None, "image_expires_at": row.image_expires_at,
            "raw_text_expires_at": row.raw_text_expires_at, "math_delta": self._math_delta(session, row),
        }
        return {key: values[key] for key in _SUMMARY_FIELDS}

    def _detail(self, session, row):
        lines = session.scalars(select(Line).where(Line.receipt_id == row.id, Line.owner == row.owner)
                                .order_by(Line.line_no))
        return {**self._summary(session, row), "raw_ocr_text": row.raw_ocr_text,
                "lines": [_line_dict(line) for line in lines]}

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
                self._owner(session, owner)
            return self._purge(session, owner, self._now())

    def parse_text(self, owner, text):
        parsed = parse_receipt(text)
        candidates = self._read_candidates(owner, parsed)
        with self.transaction() as session:
            self._owner(session, owner)
            now = self._now()
            self._purge(session, owner, now)
            drafts = session.scalar(select(func.count()).select_from(Receipt).where(
                Receipt.owner == owner, Receipt.status == "draft"))
            if drafts >= MAX_DRAFTS:
                raise ReceiptError("budget_receipt_quota_exceeded", 413)
            identifier = str(uuid.uuid4())
            row = Receipt(
                id=identifier, owner=owner, shop_id=self._shop_id(session, owner, parsed),
                raw_shop_name=parsed["raw_shop_name"], raw_nip=parsed["raw_nip"],
                receipt_kind=parsed["receipt_kind"], receipt_date=parsed["receipt_date"],
                receipt_time=parsed["receipt_time"], total_amount=parsed["total_amount"],
                currency=parsed["currency"], payment_method=parsed["payment_method"], status="draft",
                source_kind="live_text", image_relpath=None, image_expires_at=None,
                raw_ocr_text=parsed["raw_ocr_text"], raw_text_expires_at=now + DRAFT_TTL_SECONDS,
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
            write_audit(session, owner, row, "create", None, audit_payload(row, "create"), now)
            detail = self._detail(session, row)
        return {"receipt_id": identifier, "draft": detail, "candidates": candidates, "version": 1}

    def _read_candidates(self, owner, parsed):
        """Match in a deferred read snapshot so the scan does not hold the writer lock.

        ``confirm`` re-checks the chosen link inside ``BEGIN IMMEDIATE``.
        """
        preview = SimpleNamespace(
            total_amount=parsed["total_amount"], currency=parsed["currency"],
            receipt_date=parsed["receipt_date"], payment_method=parsed["payment_method"],
            receipt_kind=parsed["receipt_kind"], raw_shop_name=parsed["raw_shop_name"],
            raw_nip=parsed["raw_nip"],
        )
        with self._read(owner) as session:
            return self._candidates(session, owner, preview, parsed)

    @staticmethod
    def _candidate_rows(session, owner):
        active_link = exists().where(
            Receipt.owner == owner,
            Receipt.linked_account_id == Transaction.account_id,
            Receipt.linked_entry_ref == Transaction.entry_reference,
            Receipt.status.in_(("confirmed", "needs_review")),
        )
        transfer_leg = exists().where(
            Leg.account_id == Transaction.account_id, Leg.entry_reference == Transaction.entry_reference,
            Leg.account_id == Account.id, Account.owner == owner,
        )
        return session.scalars(select(Transaction).join(Account).join(Connection).where(
            Account.owner == owner, Connection.owner == owner, Account.enabled.is_(True),
            Transaction.uncertain.is_(False), ~active_link, ~transfer_leg,
        ))

    def _candidates(self, session, owner, row, parsed):
        if (row.total_amount is None or row.currency is None or row.receipt_date is None
                or row.payment_method in ("cash", "mixed")):
            return []
        try:
            total = canonical_money(row.total_amount)
        except AnalyticsError:
            return []
        direction = "CRDT" if row.receipt_kind == "refund" else "DBIT"
        tokens = merchant_tokens(row.raw_shop_name, row.raw_nip)
        ranked = []
        for transaction in self._candidate_rows(session, owner):
            try:
                payload = _json(transaction.payload_json)
                if (not isinstance(payload, dict) or payload.get("status") != "BOOK"
                        or payload.get("credit_debit_indicator") != direction
                        or payload.get("currency") != row.currency
                        or canonical_money(payload.get("amount")) != total):
                    continue
                distance = _date_distance(payload, row.receipt_date)
                if distance is None or distance > 4:
                    continue
            except (AnalyticsError, ValueError, TypeError, KeyError, RecursionError):
                continue
            try:
                fingerprint = financial_fingerprint(payload)
            except AnalyticsError:
                continue
            try:
                creditor = creditor_name(session, owner, transaction.account_id, transaction.entry_reference, fingerprint)
                evidence = link_evidence(
                    owner, transaction.account_id, transaction.entry_reference, payload,
                    row.raw_shop_name, row.raw_nip, creditor)
                token = self._candidate_token(evidence)
            except (AnalyticsError, ValueError, TypeError, RecursionError):
                continue
            remittance = payload.get("remittance_information")
            if not isinstance(remittance, list):
                remittance = []
            description = " ".join(value for value in remittance if isinstance(value, str))
            haystack = fold_for_match(description + " " + (creditor or ""))
            hit = any(token in haystack for token in tokens)
            candidate = {
                "account_id": transaction.account_id, "entry_reference": transaction.entry_reference,
                "amount": canonical_money(payload["amount"]), "currency": payload["currency"],
                "credit_debit_indicator": direction,
                "transaction_date": evidence["transaction_date"],
                "booking_date": evidence["booking_date"], "value_date": evidence["value_date"],
                "remittance_information": remittance, "creditor_name": creditor,
                "merchant_token_hit": hit, "date_distance": distance,
                "candidate_token": token,
            }
            ranked.append((not hit, distance, transaction.entry_reference, transaction.account_id, candidate))
        ranked.sort(key=lambda item: item[:4])
        return [item[-1] for item in ranked[:MAX_CANDIDATES]]

    def _verify_link(self, session, owner, cleaned, link, receipt_id):
        account = session.get(Account, link["account_id"])
        if account is None or account.owner != owner or not account.enabled:
            raise ReceiptError()
        connection = session.get(Connection, account.connection_id)
        if connection is None or connection.owner != owner:
            raise ReceiptError()
        transaction = session.get(Transaction, (link["account_id"], link["entry_reference"]))
        if transaction is None:
            raise ReceiptError("budget_receipt_not_found", 404)
        if session.get(Leg, (link["account_id"], link["entry_reference"])) is not None:
            raise ReceiptError()
        other = session.scalar(select(Receipt).where(
            Receipt.owner == owner,
            Receipt.linked_account_id == link["account_id"], Receipt.linked_entry_ref == link["entry_reference"],
            Receipt.status.in_(("confirmed", "needs_review")),
        ).limit(1))
        if other is not None and other.id != receipt_id:
            raise ReceiptError("budget_receipt_tx_already_linked", 409)
        try:
            payload = _json(transaction.payload_json)
            expected_direction = "CRDT" if cleaned["receipt_kind"] == "refund" else "DBIT"
            if (transaction.uncertain or not isinstance(payload, dict) or payload.get("status") != "BOOK"
                    or payload.get("credit_debit_indicator") != expected_direction
                    or payload.get("currency") != cleaned["currency"]
                    or canonical_money(payload.get("amount")) != cleaned["total_amount"]):
                raise ReceiptError()
            distance = _date_distance(payload, cleaned["receipt_date"])
            if distance is None or distance > 4:
                raise ReceiptError()
            fingerprint = financial_fingerprint(payload)
        except ReceiptError:
            raise
        except (AnalyticsError, ValueError, TypeError, KeyError, RecursionError):
            raise ReceiptError() from None
        return fingerprint

    def _candidate_token(self, evidence):
        raw = evidence_json(evidence).encode("ascii")
        signature = hmac.new(self._candidate_key, raw, hashlib.sha256).digest()
        body = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        mac = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        return body + "." + mac

    def _presented_evidence(self, token, owner, link):
        try:
            if not isinstance(token, str) or token.count(".") != 1:
                raise ValueError
            encoded, signature = token.split(".")
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            supplied = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
            if not hmac.compare_digest(hmac.new(self._candidate_key, raw, hashlib.sha256).digest(), supplied):
                raise ValueError
            evidence = _json(raw)
            if (not isinstance(evidence, dict) or set(evidence) != set(_EVIDENCE_KEYS)
                    or evidence["owner"] != owner or evidence["account_id"] != link["account_id"]
                    or evidence["entry_reference"] != link["entry_reference"]):
                raise ValueError
            return evidence
        except (ValueError, TypeError, KeyError, RecursionError, AnalyticsError):
            raise ReceiptError() from None

    def _confirmed_link(self, session, owner, cleaned, link, row):
        presented = self._presented_evidence(link["candidate_token"], owner, link)
        transaction = session.get(Transaction, (link["account_id"], link["entry_reference"]))
        if transaction is None:
            raise ReceiptError("budget_receipt_not_found", 404)
        try:
            payload = _json(transaction.payload_json)
            fingerprint = None if transaction.uncertain else financial_fingerprint(payload)
            creditor = creditor_name(session, owner, link["account_id"], link["entry_reference"], fingerprint)
            current = link_evidence(
                owner, link["account_id"], link["entry_reference"], payload,
                cleaned["raw_shop_name"], cleaned["raw_nip"], creditor)
        except (AnalyticsError, ValueError, TypeError, KeyError, RecursionError):
            raise ReceiptError("budget_receipt_stale_source", 409) from None
        if current != presented:
            raise ReceiptError("budget_receipt_stale_source", 409)
        return self._verify_link(session, owner, cleaned, link, row.id), evidence_json(current)

    def confirm(self, owner, values):
        cleaned = validate_confirm(values)
        with self.transaction() as session:
            self._owner(session, owner)
            now = self._now()
            self._purge(session, owner, now)
            row = self._owned(session, owner, values["receipt_id"])
            if row.version != values["expected_version"]:
                raise ReceiptError("budget_receipt_conflict", 409)
            row_was_draft = row.status == "draft"
            link = cleaned["link"]
            fingerprint = None if link is None else self._confirmed_link(session, owner, cleaned, link, row)
            before = audit_payload(row, "confirm")
            row.shop_id = self._shop_id(session, owner, cleaned)
            for key in ("raw_shop_name", "raw_nip", "receipt_kind", "receipt_date", "receipt_time",
                        "total_amount", "currency", "payment_method"):
                setattr(row, key, cleaned[key])
            session.execute(delete(Line).where(Line.receipt_id == row.id, Line.owner == owner))
            for line in cleaned["lines"]:
                session.add(Line(receipt_id=row.id, owner=owner, **line))
            if link is None:
                row.linked_account_id = row.linked_entry_ref = row.linked_fingerprint = row.linked_evidence = None
            else:
                row.linked_account_id = link["account_id"]
                row.linked_entry_ref = link["entry_reference"]
                row.linked_fingerprint, row.linked_evidence = fingerprint
            row.status, row.version, row.updated_at = "confirmed", row.version + 1, now
            if row_was_draft:
                deadline = now + CONFIRMED_TEXT_TTL_SECONDS
                if row.raw_ocr_text is not None:
                    row.raw_text_expires_at = deadline
                row.image_expires_at = deadline
            try:
                session.flush()
            except IntegrityError:
                raise ReceiptError("budget_receipt_tx_already_linked", 409) from None
            write_audit(session, owner, row, "confirm", before, audit_payload(row, "confirm"), now)
            return {"ok": True, "receipt": self._detail(session, row)}

    def _encode_cursor(self, owner, context, row):
        now = self._now()
        payload = {"owner": owner, "context": context, "last_date": row.receipt_date, "last_id": row.id,
                   "expires_at": now + CURSOR_TTL_SECONDS}
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=True, allow_nan=False,
                         separators=(",", ":")).encode("ascii")
        encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        signature = base64.urlsafe_b64encode(hmac.new(self._receipt_cursor_key, raw, hashlib.sha256).digest())
        return encoded + "." + signature.rstrip(b"=").decode("ascii")

    def _decode_cursor(self, owner, context, cursor):
        if cursor is None:
            return None
        try:
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= MAX_CURSOR_CHARS:
                raise ValueError
            encoded, signature = cursor.split(".")
            raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
            supplied = base64.b64decode(signature + "=" * (-len(signature) % 4), altchars=b"-_", validate=True)
            expected = hmac.new(self._receipt_cursor_key, raw, hashlib.sha256).digest()
            if not hmac.compare_digest(supplied, expected):
                raise ValueError
            value = _json(raw)
            if (not isinstance(value, dict)
                    or set(value) != {"owner", "context", "last_date", "last_id", "expires_at"}
                    or value["owner"] != owner or value["context"] != context
                    or type(value["expires_at"]) not in (int, float)
                    or not math.isfinite(value["expires_at"]) or not self._now() < value["expires_at"]
                    or not _UUID.fullmatch(value["last_id"])
                    or (value["last_date"] is not None and not _valid_date(value["last_date"]))):
                raise ValueError
            return value["last_date"], value["last_id"]
        except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
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
        allowed = {"query", "limit", "cursor"} if search else {"month", "status", "linked", "limit", "cursor"}
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
        if status is not None and status not in ("draft", "confirmed", "needs_review"):
            raise ReceiptError()
        linked = values.get("linked")
        if linked is not None and type(linked) is not bool:
            raise ReceiptError()
        query = values.get("query")
        if search and (not isinstance(query, str) or not query.strip() or len(query) > 128):
            raise ReceiptError()
        context_values = {"query": fold_for_match(query.strip())} if search else {
            "month": month, "status": status, "linked": linked,
        }
        context = hashlib.sha256(json.dumps(context_values, sort_keys=True, ensure_ascii=True,
                                            separators=(",", ":")).encode("ascii")).hexdigest()
        return limit, month, status, linked, query, context

    def _list(self, owner, values, *, search):
        limit, month, status, linked, query, context = self._filters(values, search=search)
        with self.transaction() as session:
            self._owner(session, owner)
            now = self._now()
            self._purge(session, owner, now)
            cursor = self._decode_cursor(owner, context, values.get("cursor"))
            query_obj = select(Receipt).where(Receipt.owner == owner)
            if month is not None:
                query_obj = query_obj.where(Receipt.receipt_date >= month + "-01", Receipt.receipt_date <= month + "-31")
            if status is not None:
                query_obj = query_obj.where(Receipt.status == status)
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
        if row.raw_ocr_text is not None and needle in fold_for_match(row.raw_ocr_text):
            return True
        return session.scalar(select(Line.receipt_id).where(
            Line.receipt_id == row.id, Line.owner == row.owner,
            Line.raw_name_key.like("%" + _like_escape(needle) + "%", escape="\\"),
        ).limit(1)) is not None

    def detail(self, owner, receipt_id):
        with self.transaction() as session:
            self._owner(session, owner)
            now = self._now()
            self._purge(session, owner, now)
            row = self._owned(session, owner, receipt_id)
            return {"receipt": self._detail(session, row)}

    def image(self, owner, receipt_id):
        with self.transaction() as session:
            self._owner(session, owner)
            self._owned(session, owner, receipt_id)
        raise ReceiptError("budget_receipt_not_found", 404)

    def delete(self, owner, values):
        if (not isinstance(values, dict) or set(values) != {"receipt_id", "expected_version"}
                or type(values["expected_version"]) is not int or values["expected_version"] < 1):
            raise ReceiptError()
        with self.transaction() as session:
            self._owner(session, owner)
            now = self._now()
            self._purge(session, owner, now)
            row = self._owned(session, owner, values["receipt_id"])
            if row.version != values["expected_version"]:
                raise ReceiptError("budget_receipt_conflict", 409)
            session.execute(delete(Line).where(Line.receipt_id == row.id, Line.owner == owner))
            erase_receipt_audit(session, owner, row.id)
            session.delete(row)
            session.flush()
            return {"ok": True, "deleted": True}


def fold_for_match(value):
    from .receipt_parser import fold_text
    return fold_text(value)


def _like_escape(value):
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
