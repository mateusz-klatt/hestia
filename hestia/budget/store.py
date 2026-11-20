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
from ..db import BudgetImportBatch as ImportBatch, BudgetImportUnresolved as ImportUnresolved


class BudgetStoreError(ValueError):
    """Safe errors for immutable identity, selection and owner checks."""


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _safe_error(code):
    allowed = {"authorization", "rate_limit", "unavailable", "network", "invalid_response",
               "provider_error", "invalid_data", "configuration", "unsupported_amount", "internal_error",
               "invalid_session", "expired_session", "session_mismatch",
               "account_selection", "invalid_pagination", "limit_exceeded"}
    return code if code in allowed else "invalid_data"


def _ledger_reference(tx):
    return tx["entry_reference"] if (tx["status"] == "BOOK" and (tx["booking_date"] or tx["value_date"])) else None


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
        references = Counter(filter(None, map(_ledger_reference, observation.transactions)))
        for tx in observation.transactions:
            conflict = tx["entry_reference"] is not None and references[tx["entry_reference"]] > 1
            ordinal = self._import_transaction(session, account.id, tx, conflict, ordinal, now)

    @staticmethod
    def _import_transaction(session, account_id, tx, conflict, ordinal, now):
        ref = tx["entry_reference"]
        if conflict:
            prior = session.get(Transaction, (account_id, ref))
            if prior is None:
                # A durable uncertain head also protects references that have
                # never had a canonical entry from an older offline backfill.
                prior = Transaction(account_id=account_id, entry_reference=ref,
                                    payload_json=_json(tx), observed_at=now, uncertain=True,
                                    source="api", api_observed_at=now)
                session.add(prior)
                session.flush()
            prior.uncertain, prior.api_observed_at = True, now
        if conflict or _ledger_reference(tx) is None:
            session.add(Unresolved(account_id=account_id, ordinal=ordinal,
                                   payload_json=_json(tx), observed_at=now))
            return ordinal + 1
        existing = session.get(Transaction, (account_id, ref))
        if existing is None:
            session.add(Transaction(account_id=account_id, entry_reference=ref,
                                    payload_json=_json(tx), observed_at=now, uncertain=False,
                                    source="api", api_observed_at=now))
            session.flush()
        else:
            existing.payload_json, existing.observed_at, existing.uncertain = _json(tx), now, False
            existing.source, existing.api_observed_at = "api", now
        return ordinal

    def import_snapshot(self, config, snapshot):
        """Add offline history without touching API state or scheduling."""
        item = next((c for c in config.connections if c.id == snapshot.connection_id), None)
        if item is None or snapshot.identification_hash not in item.accounts:
            raise BudgetStoreError("Import requires an explicitly selected bank account")
        with self.transaction() as session:
            self._owner(session, item.owner)
            account = self._offline_account(session, config, item, snapshot.identification_hash)
            batch = session.scalar(select(ImportBatch).where(ImportBatch.fingerprint == snapshot.digest))
            if batch is not None:
                if batch.account_id != account.id:
                    raise BudgetStoreError("Offline import identity mismatch")
                return {**json.loads(batch.report_json), "duplicate": True}
            self._restore_api_conflicts(session, account.id)
            batch = ImportBatch(id=str(uuid.uuid4()), fingerprint=snapshot.digest,
                                account_id=account.id, fetched_at=snapshot.fetched_at,
                                imported_at=self.clock(), balances_json=(None if snapshot.balances is None
                                                                         else _json(snapshot.balances)), report_json="{}")
            session.add(batch)
            session.flush()
            report = {"batch_id": batch.id, "duplicate": False, "inserted": 0, "updated": 0,
                      "preserved_api": 0, "stale": 0, "unresolved": 0, "unchanged": 0}
            references = Counter(filter(None, map(_ledger_reference, snapshot.transactions)))
            for tx in snapshot.transactions:
                ref = _ledger_reference(tx)
                conflict = ref is not None and references[ref] > 1
                action, reason = self._offline_transaction(session, account.id, tx, conflict, snapshot.fetched_at)
                if action is not None:
                    report[action] += 1
                if reason is not None:
                    session.add(ImportUnresolved(batch_id=batch.id, ordinal=report["unresolved"],
                                                 reason=reason, payload_json=_json(tx)))
                    report["unresolved"] += 1
            batch.report_json = _json(report)
            return report

    @staticmethod
    def _offline_account(session, config, item, identity_hash):
        row = session.get(Connection, item.id)
        identity = (item.owner, config.application_id, item.bank_name, item.country)
        if row is None:
            row = Connection(id=item.id, owner=item.owner, application_id=config.application_id,
                             bank_name=item.bank_name, country=item.country, enabled=False,
                             next_attempt=0, failures=0)
            session.add(row)
            session.flush()
        elif (row.owner, row.application_id, row.bank_name, row.country) != identity:
            raise BudgetStoreError("Bank connection identity cannot be reassigned")
        account = session.scalar(select(Account).where(Account.owner == item.owner,
                                                       Account.identification_hash == identity_hash))
        if account is None:
            account = Account(id=str(uuid.uuid4()), owner=item.owner, connection_id=item.id,
                              identification_hash=identity_hash, enabled=False, balances_json="[]")
            session.add(account)
            session.flush()
        elif account.connection_id != item.id:
            raise BudgetStoreError("An account may belong to only one connection per owner")
        return account

    @staticmethod
    def _restore_api_conflicts(session, account_id):
        """Protect pre-0007 API conflicts that lacked a canonical placeholder."""
        rows = list(session.scalars(select(Unresolved).where(Unresolved.account_id == account_id)))
        payloads = [(row, json.loads(row.payload_json)) for row in rows]
        refs = Counter(filter(None, (_ledger_reference(tx) for _, tx in payloads)))
        for row, tx in payloads:
            ref = _ledger_reference(tx)
            if ref is not None and refs[ref] > 1 and session.get(Transaction, (account_id, ref)) is None:
                session.add(Transaction(account_id=account_id, entry_reference=ref, payload_json=row.payload_json,
                                        observed_at=row.observed_at, uncertain=True, source="api",
                                        api_observed_at=row.observed_at))
                session.flush()

    @staticmethod
    def _offline_transaction(session, account_id, tx, conflict, fetched_at):
        ref = _ledger_reference(tx)
        if ref is None:
            return None, "non_canonical"
        existing = session.get(Transaction, (account_id, ref))
        if existing is not None and (existing.source == "api" or existing.api_observed_at is not None):
            reason = "api_preserved" if conflict or existing.uncertain or existing.payload_json != _json(tx) else None
            return "preserved_api", reason
        if existing is not None and fetched_at < existing.observed_at:
            return "stale", "stale_conflict" if conflict else None
        if conflict:
            BudgetStore._offline_conflict(session, account_id, ref, tx, existing, fetched_at)
            return None, "reference_conflict"
        if existing is None:
            session.add(Transaction(account_id=account_id, entry_reference=ref, payload_json=_json(tx),
                                    observed_at=fetched_at, uncertain=False, source="offline_import"))
            session.flush()
            return "inserted", None
        return BudgetStore._update_offline(existing, tx, fetched_at)

    @staticmethod
    def _offline_conflict(session, account_id, ref, tx, existing, fetched_at):
        if existing is None:
            existing = Transaction(account_id=account_id, entry_reference=ref, payload_json=_json(tx),
                                   observed_at=fetched_at, uncertain=True, source="offline_import")
            session.add(existing)
            session.flush()
        existing.uncertain, existing.observed_at = True, fetched_at

    @staticmethod
    def _update_offline(existing, tx, fetched_at):
        if fetched_at == existing.observed_at:
            if existing.payload_json != _json(tx) or existing.uncertain:
                existing.uncertain = True
                return None, "same_time_conflict"
            return "unchanged", None
        existing.payload_json, existing.observed_at, existing.uncertain = _json(tx), fetched_at, False
        return "updated", None

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
                imports = select(ImportBatch.id).where(ImportBatch.account_id.in_(accounts))
                import_count = session.scalar(select(func.count()).select_from(ImportBatch).where(
                    ImportBatch.account_id.in_(accounts)))
                import_unresolved = session.scalar(select(func.count()).select_from(ImportUnresolved).where(
                    ImportUnresolved.batch_id.in_(imports)))
                imported = session.scalar(select(func.count()).select_from(Transaction).where(
                    Transaction.account_id.in_(accounts), Transaction.source == "offline_import",
                    Transaction.uncertain.is_(False)))
                result.append({"connection": row.id, "owner": row.owner, "enabled": row.enabled,
                               "selected_accounts": selected, "transactions": canonical, "unresolved": unresolved,
                               "uncertain_transactions": uncertain,
                               "offline_imports": import_count, "offline_transactions": imported,
                               "offline_unresolved": import_unresolved,
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
