"""Owner-private stored views; no configuration, acquisition or credential I/O."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import date
import hashlib
import json
import math
import os
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import and_, func, or_, select, text

from .. import authz, store_sql
from ..db import AppMeta, User, BudgetAccount as Account, BudgetConnection as Connection
from ..db import BudgetComponent as Component, BudgetTransaction as Transaction, BudgetImportBatch as ImportBatch
from ..db import BudgetRequestLimit as RequestLimit
from ..db import BudgetAccountDisplay as AccountDisplay
from .limits import BudgetLimits, SHARED_SCOPES
from .scheduler import shared_state
from .job_store import BudgetJobStore
from .store import (BudgetControlConflict, BudgetNotFound, BudgetStore, BudgetStoreError, OPERATIONS,
                    load_account_credit_limit, observed_balance_currencies)

CURSOR_TTL = 900
MAX_CURSOR_CHARS = 8192
MAX_BALANCES = 200
MAX_ARCHIVED_IMPORTS = 20
_CURSOR_DOMAIN = b"hestia/budget/transactions/cursor/v1"
_TRANSACTION_FIELDS = ("amount", "currency", "credit_debit_indicator", "status", "booking_date", "value_date",
                       "transaction_date", "remittance_information")
_DISPLAY_FIELDS = ("product", "currency", "masked_suffix", "source", "observed_at", "imported_at")


class BudgetQueryError(BudgetStoreError):
    """Fixed safe query diagnostics, never request or bank values."""


class BudgetUsersBackendRequired(BudgetStoreError):
    def __init__(self):
        super().__init__("budget_requires_sqlite_users")


class BudgetAccessDenied(BudgetStoreError):
    def __init__(self, status):
        self.status = status
        super().__init__("Budget access denied")


def _date_filter(value):
    if value is None:
        return None
    try:
        if not isinstance(value, str) or len(value) != 10 or date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        raise BudgetQueryError("Invalid transaction dates") from None
    return value


class BudgetHTTPStore(BudgetJobStore):
    def __init__(self, sessions, *, clock=time.time, cursor_secret: bytes):
        super().__init__(sessions, clock=clock)
        if not isinstance(cursor_secret, bytes) or not cursor_secret:
            raise BudgetQueryError("Invalid cursor configuration")
        # Retained for one-off row keys. Those HMAC with this same session secret;
        # consent aliases derive their key from it the same way. It is not logged.
        self._cursor_secret = cursor_secret
        self._cipher = AESGCM(hashlib.sha256(_CURSOR_DOMAIN + b"\0" + cursor_secret).digest())

    @staticmethod
    def _owner(session, username):
        store_sql.auth_session_phase(session)
        phase = authz.require_supported_policy_phase(session)
        if session.get(AppMeta, "users_authoritative") is None:
            raise BudgetUsersBackendRequired()
        user = session.get(User, username)
        if user is None or user.disabled:
            raise BudgetAccessDenied(401)
        if user.role != "admin" or (phase == authz.POLICY_PHASE_COMPAT and not user.budget_owner):
            raise BudgetAccessDenied(403)

    @contextmanager
    def _read(self, owner):
        with self.sessions() as session:
            session.execute(text("BEGIN"))
            self._owner(session, owner)
            yield session

    def _account_dict(self, account, connection, *, session=None):
        metadata = super()._account_dict(account, connection, session=session)
        state = shared_state(session, connection)
        display = session.get(AccountDisplay, account.id)
        currencies = observed_balance_currencies(account)
        return {**metadata, "components": self._account_status(session, account, connection, self.clock(), state)["components"],
                "display": None if display is None else {field: getattr(display, field) for field in _DISPLAY_FIELDS},
                "observed_balance_currencies": currencies,
                "credit_limit": load_account_credit_limit(session, account.id)}

    def connections(self, owner):
        with self._read(owner) as session:
            return [self._connection_dict(row) for row in session.scalars(
                select(Connection).where(Connection.owner == owner).order_by(Connection.id))]

    def accounts(self, owner, connection_id=None):
        with self._read(owner) as session:
            query = select(Account, Connection).join(Connection).where(Account.owner == owner, Connection.owner == owner)
            if connection_id is not None:
                self._owned_connection(session, owner, connection_id)
                query = query.where(Connection.id == connection_id)
            return [self._account_dict(account, connection, session=session)
                    for account, connection in session.execute(query.order_by(Account.id))]

    def status(self, owner):
        with self._read(owner) as session:
            return self._status_rows(session, owner, self.clock())

    def limits(self, owner):
        with self._read(owner) as session:
            result = []
            for connection in session.scalars(select(Connection).where(Connection.owner == owner).order_by(Connection.id)):
                for scope in SHARED_SCOPES:
                    key = BudgetLimits._shared_key(connection, scope)
                    target = session.get(RequestLimit, (scope, key))
                    result.append(BudgetLimits._entry(connection, scope, target, {}))
                accounts = session.scalars(select(Account).where(Account.owner == owner,
                                           Account.connection_id == connection.id).order_by(Account.id))
                for account in accounts:
                    for operation in OPERATIONS:
                        target = session.get(Component, (account.id, operation))
                        details = {"account": account.id, "operation": operation}
                        result.append(BudgetLimits._entry(connection, "component", target, details))
            return result

    def _cursor(self, context, last, now):
        payload = {**context, "last_date": last[0], "issued_at": now,
                   "expires_at": now + CURSOR_TTL}
        nonce = os.urandom(12)
        header = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        # References can contain 1024 control characters. Keep their exact UTF-8
        # outside JSON so escaping cannot expand an otherwise valid cursor URL.
        data = len(header).to_bytes(2, "big") + header + last[1].encode("utf-8")
        return base64.urlsafe_b64encode(nonce + self._cipher.encrypt(nonce, data, _CURSOR_DOMAIN)).rstrip(b"=").decode("ascii")

    def _decode_cursor(self, cursor, context, now):
        if cursor is None:
            return None
        try:
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= MAX_CURSOR_CHARS:
                raise ValueError
            raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            data = self._cipher.decrypt(raw[:12], raw[12:], _CURSOR_DOMAIN)
            boundary = 2 + int.from_bytes(data[:2], "big")
            payload = json.loads(data[2:boundary])
            reference = data[boundary:].decode("utf-8")
            if not isinstance(payload, dict) or set(payload) != set(context) | {"last_date", "issued_at", "expires_at"}:
                raise ValueError
            if any(payload[key] != value for key, value in context.items()):
                raise ValueError
            issued, expires = payload["issued_at"], payload["expires_at"]
            if (type(issued) not in (int, float) or type(expires) not in (int, float)
                    or not math.isfinite(issued) or not math.isfinite(expires)
                    or not issued <= now < expires <= issued + CURSOR_TTL):
                raise ValueError
            stamp = _date_filter(payload["last_date"])
            if stamp is None or not 1 <= len(reference) <= 1024:
                raise ValueError
            return stamp, reference
        except (ValueError, TypeError, InvalidTag, RecursionError, OverflowError):
            raise BudgetQueryError("Invalid transaction cursor") from None

    @staticmethod
    def _transaction_result(row):
        payload = json.loads(row.payload_json)
        return {"entry_reference": row.entry_reference, **{key: payload[key] for key in _TRANSACTION_FIELDS},
                "source": row.source, "observed_at": row.observed_at, "api_observed_at": row.api_observed_at}

    def transactions(self, owner, account_id, *, date_from=None, date_to=None, limit=50, cursor=None):
        date_from, date_to = _date_filter(date_from), _date_filter(date_to)
        if date_from is not None and date_to is not None and date_from > date_to:
            raise BudgetQueryError("Invalid transaction dates")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise BudgetQueryError("Invalid transaction page size")
        context = {"version": 1, "owner": owner, "account": account_id,
                   "date_from": date_from, "date_to": date_to, "limit": limit}
        now = self.clock()
        with self._read(owner) as session:
            self._owned_account(session, owner, account_id)
            last = self._decode_cursor(cursor, context, now)
            stamp = func.coalesce(func.json_extract(Transaction.payload_json, "$.booking_date"),
                                  func.json_extract(Transaction.payload_json, "$.value_date"))
            base = select(Transaction).where(Transaction.account_id == account_id, Transaction.uncertain.is_(False),
                                             func.json_extract(Transaction.payload_json, "$.status") == "BOOK", stamp.is_not(None))
            total = session.scalar(select(func.count()).select_from(base.subquery()))
            uncertain = session.scalar(select(func.count()).select_from(Transaction).where(
                Transaction.account_id == account_id, Transaction.uncertain.is_(True)))
            query = base
            if date_from is not None:
                query = query.where(stamp >= date_from)
            if date_to is not None:
                query = query.where(stamp <= date_to)
            if last is not None:
                query = query.where(or_(stamp < last[0], and_(stamp == last[0], Transaction.entry_reference < last[1])))
            rows = list(session.scalars(query.order_by(stamp.desc(), Transaction.entry_reference.desc()).limit(limit + 1)))
            page, next_cursor = rows[:limit], None
            if len(rows) > limit:
                payload = json.loads(page[-1].payload_json)
                next_cursor = self._cursor(context, (payload["booking_date"] or payload["value_date"], page[-1].entry_reference), now)
            return {"account_id": account_id, "transactions": [self._transaction_result(row) for row in page],
                    "account_total": total, "uncertain_total": uncertain, "next_cursor": next_cursor}

    @staticmethod
    def _balance_result(value):
        return {"name": value["name"], "balance_type": value["balance_type"],
                "balance_amount": {key: value["balance_amount"][key] for key in ("amount", "currency")},
                "reference_date": value.get("reference_date")}

    def balances(self, owner, account_id):
        with self._read(owner) as session:
            account, connection = self._owned_account(session, owner, account_id)
            raw = json.loads(account.balances_json)
            live = [self._balance_result(value) for value in raw[:MAX_BALANCES]]
            query = select(ImportBatch.id, ImportBatch.fetched_at, ImportBatch.imported_at,
                           func.json_array_length(ImportBatch.balances_json)).where(
                ImportBatch.account_id == account.id, ImportBatch.balances_json.is_not(None)).order_by(
                    ImportBatch.fetched_at.desc(), ImportBatch.id.desc()).limit(MAX_ARCHIVED_IMPORTS + 1)
            archived = list(session.execute(query))
            state = shared_state(session, connection)
            freshness = self._component_status(session.get(Component, (account.id, "balances")), "balances",
                                                account, connection, self.clock(), state)
            return {"account_id": account_id, "balances": live, "balances_total": len(raw),
                    "balances_truncated": len(raw) > MAX_BALANCES, "freshness": freshness,
                    "archived_imports": [{"id": row[0], "fetched_at": row[1], "imported_at": row[2],
                                          "balance_count": row[3]} for row in archived[:MAX_ARCHIVED_IMPORTS]],
                    "archived_has_more": len(archived) > MAX_ARCHIVED_IMPORTS}
