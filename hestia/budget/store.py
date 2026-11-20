"""Atomic snapshots on the existing Hestia SQLite engine, without session secrets."""
from __future__ import annotations

import json
import time
import uuid
from collections import Counter
from contextlib import contextmanager

from sqlalchemy import delete, func, select, text

from ..db import BudgetAccount as Account, BudgetConnection as Connection
from ..db import BudgetTransaction as Transaction, BudgetUnresolved as Unresolved, User


class BudgetStoreError(ValueError):
    """Safe errors for immutable identity, selection and owner checks."""


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _safe_error(code):
    allowed = {"authorization", "rate_limit", "unavailable", "network", "invalid_response",
               "provider_error", "invalid_data", "configuration", "unsupported_amount",
               "invalid_session", "expired_session", "session_mismatch",
               "account_selection", "invalid_pagination", "limit_exceeded"}
    return code if code in allowed else "invalid_data"


class BudgetStore:
    def __init__(self, sessions, *, clock=time.time):
        self.sessions, self.clock = sessions, clock

    @contextmanager
    def transaction(self):
        with self.sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            yield session

    @staticmethod
    def _owner(session, username):
        user = session.get(User, username)
        if user is None or user.disabled or user.role != "admin":
            raise BudgetStoreError("Bank connection requires an existing enabled admin owner")

    def configure(self, config):
        with self.transaction() as session:
            for row in session.scalars(select(Connection)):
                row.enabled = False
            for row in session.scalars(select(Account)):
                row.enabled = False
            for item in config.connections:
                self._owner(session, item.owner)
                row = session.get(Connection, item.id)
                identity = (item.owner, config.application_id, item.bank_name, item.country)
                if row is None:
                    row = Connection(id=item.id, owner=item.owner, application_id=config.application_id,
                                     bank_name=item.bank_name, country=item.country, enabled=True,
                                     next_attempt=0, failures=0)
                    session.add(row)
                    session.flush()
                elif (row.owner, row.application_id, row.bank_name, row.country) != identity:
                    raise BudgetStoreError("Bank connection identity cannot be reassigned")
                row.enabled = True
                for identity_hash in item.accounts:
                    self._configure_account(session, item, identity_hash)

    @staticmethod
    def _configure_account(session, item, identity_hash):
        account = session.scalar(select(Account).where(Account.owner == item.owner,
                                                       Account.identification_hash == identity_hash))
        if account is None:
            account = Account(id=str(uuid.uuid4()), owner=item.owner, connection_id=item.id,
                              identification_hash=identity_hash, enabled=True, balances_json="[]")
            session.add(account)
            session.flush()
        elif account.connection_id != item.id:
            raise BudgetStoreError("An account may belong to only one connection per owner")
        account.enabled = True

    def start_attempt(self, connection_id):
        """Reserve quota BEFORE HTTP; a crash cannot cause an immediate duplicate fetch."""
        now = self.clock()
        with self.transaction() as session:
            row = session.get(Connection, connection_id)
            if row is None or not row.enabled:
                raise BudgetStoreError("Unknown or disabled bank connection")
            self._owner(session, row.owner)
            selected = session.scalar(select(func.count()).select_from(Account).where(
                Account.connection_id == connection_id, Account.enabled))
            if not selected or row.next_attempt > now:
                return False
            row.last_attempt, row.next_attempt = now, now + 43200
            return True

    def record_failure(self, connection_id, code):
        # Do not persist arbitrary exception strings, provider bodies or identifiers.
        safe_code = _safe_error(code)
        with self.transaction() as session:
            row = session.get(Connection, connection_id)
            row.failures += 1
            row.last_error = safe_code
            # Keep the 12-hour pre-HTTP quota reservation, including after a crash
            # or partial fetch. A later explicit run safely retries after restart.
            row.next_attempt = max(row.next_attempt, self.clock() + 21600)

    def commit_snapshot(self, connection_id, snapshot):
        now = self.clock()
        if snapshot.valid_until <= now:
            raise BudgetStoreError("Bank session has expired")
        with self.transaction() as session:
            row = session.get(Connection, connection_id)
            if row is None or not row.enabled:
                raise BudgetStoreError("Unknown or disabled bank connection")
            self._owner(session, row.owner)
            accounts = {a.identification_hash: a for a in session.scalars(select(Account).where(
                Account.connection_id == connection_id, Account.enabled))}
            observations = (*snapshot.accounts, *snapshot.errors)
            if (len(observations) != len(accounts)
                    or {a.identification_hash for a in observations} != set(accounts)):
                raise BudgetStoreError("Bank account selection changed during acquisition")
            for error in snapshot.errors:
                account = accounts[error.identification_hash]
                account.last_attempt, account.last_error = now, _safe_error(error.code)
            for observation in snapshot.accounts:
                self._import_account(session, accounts[observation.identification_hash], observation, now)
            row.valid_until = snapshot.valid_until
            if snapshot.errors:
                row.failures += 1
                row.last_error = "partial_data" if snapshot.accounts else "account_errors"
            else:
                row.last_success = now
                row.failures, row.last_error = 0, None

    def _import_account(self, session, account, observation, now):
        account.balances_json, account.observed_at = _json(observation.balances), now
        account.last_attempt, account.last_error = now, None
        session.execute(delete(Unresolved).where(Unresolved.account_id == account.id))
        ordinal = 0
        references = Counter(tx["entry_reference"] for tx in observation.transactions
                             if tx["entry_reference"] is not None)
        for tx in observation.transactions:
            conflict = tx["entry_reference"] is not None and references[tx["entry_reference"]] > 1
            ordinal = self._import_transaction(session, account.id, tx, conflict, ordinal, now)

    @staticmethod
    def _import_transaction(session, account_id, tx, conflict, ordinal, now):
        ref = tx["entry_reference"]
        if conflict:
            prior = session.get(Transaction, (account_id, ref))
            if prior is not None:
                prior.uncertain = True
        if conflict or tx["status"] != "BOOK" or ref is None or not (tx["booking_date"] or tx["value_date"]):
            session.add(Unresolved(account_id=account_id, ordinal=ordinal,
                                   payload_json=_json(tx), observed_at=now))
            return ordinal + 1
        existing = session.get(Transaction, (account_id, ref))
        if existing is None:
            session.add(Transaction(account_id=account_id, entry_reference=ref,
                                    payload_json=_json(tx), observed_at=now, uncertain=False))
            session.flush()
        else:
            existing.payload_json, existing.observed_at, existing.uncertain = _json(tx), now, False
        return ordinal

    def status(self, *, owner=None):
        now = self.clock()
        with self.sessions() as session:
            session.execute(text("BEGIN"))
            query = select(Connection).order_by(Connection.id)
            if owner is not None:
                query = query.where(Connection.owner == owner)
            result = []
            for row in session.scalars(query):
                accounts = select(Account.id).where(Account.connection_id == row.id)
                selected = session.scalar(select(func.count()).select_from(Account).where(
                    Account.connection_id == row.id, Account.enabled))
                canonical = session.scalar(select(func.count()).select_from(Transaction).where(
                    Transaction.account_id.in_(accounts), Transaction.uncertain.is_(False)))
                uncertain = session.scalar(select(func.count()).select_from(Transaction).where(
                    Transaction.account_id.in_(accounts), Transaction.uncertain.is_(True)))
                unresolved = session.scalar(select(func.count()).select_from(Unresolved).where(
                    Unresolved.account_id.in_(accounts)))
                result.append({"connection": row.id, "owner": row.owner, "enabled": row.enabled,
                               "selected_accounts": selected, "transactions": canonical, "unresolved": unresolved,
                               "uncertain_transactions": uncertain,
                               "last_attempt": row.last_attempt, "last_success": row.last_success,
                               "next_attempt": row.next_attempt, "last_error": row.last_error,
                               "failures": row.failures, "valid_until": row.valid_until,
                               "renewal_due": row.valid_until is not None and row.valid_until - now <= 604800,
                               "accounts": [{"id": account.id, "enabled": account.enabled,
                                             "last_attempt": account.last_attempt,
                                             "last_success": account.observed_at, "last_error": account.last_error}
                                            for account in session.scalars(select(Account).where(
                                                Account.connection_id == row.id).order_by(Account.id))]})
            return result
