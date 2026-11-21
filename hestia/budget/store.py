"""Atomic snapshots on the existing Hestia SQLite engine, without session secrets."""
from __future__ import annotations

from datetime import date
import json
import re
import time
import uuid
from collections import Counter
from contextlib import contextmanager

from sqlalchemy import func, select, text

from ..db import BudgetAccount as Account, BudgetConnection as Connection
from ..db import BudgetTransaction as Transaction, BudgetUnresolved as Unresolved, User
from ..db import BudgetImportBatch as ImportBatch, BudgetImportUnresolved as ImportUnresolved
from ..db import BudgetAccountAudit as AccountAudit
from ..db import BudgetConnectionAudit as ConnectionAudit
from ..db import BudgetComponent as Component
from .account_metadata import BudgetMetadataError, apply_account_metadata
from .bank_metadata import apply_bank_metadata
from .client import safe_http_status, safe_provider_error
from .analytics_reconcile import reconcile_transfers

ACCOUNT_KINDS = frozenset({"current", "savings", "credit_card", "other"})
OPERATIONS = ("transactions", "balances")
STALE_AFTER = {"transactions": 129600, "balances": 86400}
# Failures that stop unattended acquisition until an explicit owner refresh verifies the session.
ATTENTION = frozenset({"authorization", "invalid_session", "expired_session", "session_mismatch", "configuration"})
_SELECTION_CHANGED = "Bank account selection changed during acquisition"
_INVALID_WINDOW = "Invalid snapshot date window"
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)


class BudgetStoreError(ValueError):
    """Safe errors for immutable identity, selection and owner checks."""


class BudgetNotFound(BudgetStoreError):
    """Unknown and foreign resources share one safe diagnostic."""

    def __init__(self):
        super().__init__("Unknown bank account")


class BudgetControlConflict(BudgetStoreError):
    def __init__(self, current):
        self.current = current
        super().__init__("Budget control version changed")


class BudgetPausedError(BudgetStoreError):
    """A local pause stops work without changing retry or failure state."""


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def safe_error(code):
    allowed = {"authorization", "rate_limit", "unavailable", "network", "invalid_response",
               "provider_error", "invalid_data", "configuration", "unsupported_amount", "internal_error",
               "invalid_session", "expired_session", "session_mismatch",
               "account_selection", "invalid_pagination", "limit_exceeded", "deferred", "acquisition_timeout",
               "control_changed", "busy", "interrupted", "background_disabled"}
    return code if code in allowed else "invalid_data"


def set_diagnostic(row, diagnostic=None):
    """Store only a valid status and a documented identifier; None clears both."""
    row.last_http_status = safe_http_status(getattr(diagnostic, "http_status", None))
    row.last_provider_error = safe_provider_error(getattr(diagnostic, "provider_error", None))


def _ledger_reference(tx):
    return tx["entry_reference"] if (tx["status"] == "BOOK" and (tx["booking_date"] or tx["value_date"])) else None


def _iso_date(value):
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        raise BudgetStoreError(_INVALID_WINDOW)
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise BudgetStoreError(_INVALID_WINDOW) from None


def _snapshot_window(snapshot):
    if not hasattr(snapshot, "date_from") and not hasattr(snapshot, "date_to"):
        return None
    start = _iso_date(getattr(snapshot, "date_from", None))
    end = _iso_date(getattr(snapshot, "date_to", None))
    if start > end:
        raise BudgetStoreError(_INVALID_WINDOW)
    return start, end


def _effective_date(tx):
    return tx.get("booking_date") or tx.get("value_date")


def _covered(stamp, window):
    return window is None or stamp is None or window[0] <= stamp <= window[1]


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
                row.configured = False
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
                              identification_hash=identity_hash, enabled=False, configured=True, balances_json="[]")
            session.add(account)
            session.flush()
        elif account.connection_id != item.id:
            raise BudgetStoreError("An account may belong to only one connection per owner")
        account.configured = True
        for operation in OPERATIONS:
            BudgetStore._component(session, account.id, operation)

    @staticmethod
    def _component(session, account_id, operation):
        row = session.get(Component, (account_id, operation))
        if row is None:
            row = Component(account_id=account_id, operation=operation, failures=0, next_attempt=0)
            session.add(row)
            session.flush()
        return row

    def _account_dict(self, account, connection, *, session=None):
        return {"id": account.id, "connection": account.connection_id, "configured": account.configured,
                "selected": account.enabled,
                "active": account.enabled and account.configured and connection.enabled and not connection.paused,
                "paused": connection.paused, "control_version": account.control_version,
                "kind": account.kind, "kind_confirmed_at": account.kind_confirmed_at,
                "kind_confirmed_by": account.kind_confirmed_by,
                "kind_confirmation_origin": account.kind_confirmation_origin}

    def accounts(self, owner):
        """Owner-scoped control metadata, without financial records or hashes."""
        with self.sessions() as session:
            session.execute(text("BEGIN"))
            self._owner(session, owner)
            rows = session.execute(select(Account, Connection).join(Connection).where(
                Account.owner == owner, Connection.owner == owner).order_by(Account.id))
            return [self._account_dict(account, connection, session=session) for account, connection in rows]

    @staticmethod
    def _actor(actor, origin, *, owner=None):
        if (not isinstance(actor, str) or not actor.strip() or len(actor) > 128
                or any(ord(c) < 32 or ord(c) == 127 for c in actor) or origin not in ("operator", "web")
                or (origin == "web" and actor != owner)):
            raise BudgetStoreError("Invalid account control actor or origin")

    def _owned_account(self, session, owner, account_id):
        self._owner(session, owner)
        if not isinstance(account_id, str) or not account_id:
            raise BudgetNotFound()
        account = session.get(Account, account_id)
        if account is None or account.owner != owner:
            raise BudgetNotFound()
        connection = session.get(Connection, account.connection_id)
        if connection is None or connection.owner != owner:
            raise BudgetNotFound()
        return account, connection

    def _owned_connection(self, session, owner, connection_id):
        self._owner(session, owner)
        if not isinstance(connection_id, str) or not connection_id:
            raise BudgetNotFound()
        connection = session.get(Connection, connection_id)
        if connection is None or connection.owner != owner:
            raise BudgetNotFound()
        return connection

    def _connection_dict(self, connection):
        return {"id": connection.id, "enabled": connection.enabled, "paused": connection.paused,
                "background_enabled": connection.background_enabled, "attention": connection.attention,
                "control_version": connection.control_version, "bank_name": connection.bank_name,
                "country": connection.country, "valid_until": connection.valid_until,
                "renewal_due": connection.valid_until is not None and connection.valid_until - self.clock() <= 604800,
                "last_attempt": connection.last_attempt, "last_success": connection.last_success,
                "last_error": connection.last_error}

    @staticmethod
    def _check_version(row, expected, changed, current, origin):
        if expected is None:
            if origin == "web":
                raise BudgetControlConflict(current)
            return
        if type(expected) is not int or expected < 1:
            raise BudgetStoreError("Invalid budget control version")
        if expected != row.control_version and changed:
            raise BudgetControlConflict(current)

    def _audit(self, session, account, actor, origin, field, old, new):
        session.add(AccountAudit(account_id=account.id, owner=account.owner, actor=actor, origin=origin,
                                 field=field, old_value_json=_json(old), new_value_json=_json(new),
                                 changed_at=self.clock()))

    def set_account_selected(self, owner, account_id, selected, *, actor, origin="operator", expected_version=None):
        """Explicit owner selection; allowlist reconciliation cannot override it."""
        self._actor(actor, origin, owner=owner)
        if type(selected) is not bool:
            raise BudgetStoreError("Account selection must be a boolean")
        with self.transaction() as session:
            account, connection = self._owned_account(session, owner, account_id)
            changed = account.enabled != selected
            self._check_version(account, expected_version, changed,
                                self._account_dict(account, connection, session=session), origin)
            if selected and not (account.configured and connection.enabled):
                raise BudgetStoreError("Bank account is outside the configured allowlist")
            if changed:
                self._audit(session, account, actor, origin, "selected", account.enabled, selected)
                account.enabled = selected
                account.control_version += 1
            return {**self._account_dict(account, connection, session=session), "changed": changed}

    def confirm_account_kind(self, owner, account_id, kind, *, actor, origin="operator", expected_version=None):
        """Record an explicit confirmation without selecting or moving history."""
        self._actor(actor, origin, owner=owner)
        if not isinstance(kind, str) or kind not in ACCOUNT_KINDS:
            raise BudgetStoreError("Unknown account kind")
        with self.transaction() as session:
            account, connection = self._owned_account(session, owner, account_id)
            changed = account.kind != kind or account.kind_confirmed_at is None
            self._check_version(account, expected_version, changed,
                                self._account_dict(account, connection, session=session), origin)
            if changed:
                old = {"kind": account.kind, "confirmed": account.kind_confirmed_at is not None}
                self._audit(session, account, actor, origin, "kind", old, {"kind": kind, "confirmed": True})
                account.kind, account.kind_confirmed_at = kind, self.clock()
                account.kind_confirmed_by, account.kind_confirmation_origin = actor, origin
                account.control_version += 1
            return {**self._account_dict(account, connection, session=session), "changed": changed}

    def set_connection_paused(self, owner, connection_id, paused, *, actor, origin="operator", expected_version=None):
        self._actor(actor, origin, owner=owner)
        if type(paused) is not bool:
            raise BudgetStoreError("Connection pause must be a boolean")
        with self.transaction() as session:
            connection = self._owned_connection(session, owner, connection_id)
            changed = connection.paused != paused
            self._check_version(connection, expected_version, changed, self._connection_dict(connection), origin)
            if changed:
                session.add(ConnectionAudit(connection_id=connection.id, owner=owner, actor=actor, origin=origin,
                                             field="paused", old_value_json=_json(connection.paused),
                                             new_value_json=_json(paused), changed_at=self.clock()))
                connection.paused = paused
                connection.control_version += 1
            return {**self._connection_dict(connection), "changed": changed}

    @staticmethod
    def _selected_hashes(session, connection_id, owner):
        return set(session.scalars(select(Account.identification_hash).where(
            Account.connection_id == connection_id, Account.owner == owner, Account.enabled, Account.configured)))

    def selected_hashes(self, connection_id, owner):
        with self.sessions() as session:
            session.execute(text("BEGIN"))
            self._owner(session, owner)
            row = session.get(Connection, connection_id)
            if row is None or row.owner != owner or not row.enabled or row.paused:
                return set()
            return self._selected_hashes(session, connection_id, owner)

    def acquisition_version(self, connection_id, owner, *, expected_version=None, expected_selection=None):
        """A pause/resume or account-selection change invalidates work begun at an earlier state."""
        with self.sessions() as session:
            session.execute(text("BEGIN"))
            connection = self._owned_connection(session, owner, connection_id)
            if connection.paused or (expected_version is not None and connection.control_version != expected_version):
                raise BudgetPausedError("Bank acquisition control changed")
            if (expected_selection is not None
                    and self._selected_hashes(session, connection_id, owner) != set(expected_selection)):
                raise BudgetPausedError(_SELECTION_CHANGED)
            return connection.control_version

    def start_attempt(self, connection_id):
        """Reserve quota BEFORE HTTP; a crash cannot cause an immediate duplicate fetch."""
        now = self.clock()
        with self.transaction() as session:
            row = session.get(Connection, connection_id)
            if row is None or not row.enabled:
                raise BudgetStoreError("Unknown or disabled bank connection")
            if row.paused:
                raise BudgetPausedError("Bank connection is paused")
            self._owner(session, row.owner)
            selected = session.scalar(select(func.count()).select_from(Account).where(
                Account.connection_id == connection_id, Account.owner == row.owner, Account.enabled, Account.configured))
            if not selected or row.next_attempt > now:
                return False
            row.last_attempt, row.next_attempt = now, now + 43200
            return True

    def start_component_attempt(self, connection_id, identity_hash, operation, *, attempt_id):
        """Persist an in-flight marker in a short transaction BEFORE endpoint HTTP."""
        if operation not in OPERATIONS or not isinstance(attempt_id, str) or not attempt_id:
            raise BudgetStoreError("Invalid component acquisition")
        now = self.clock()
        with self.transaction() as session:
            connection = session.get(Connection, connection_id)
            if connection is None or not connection.enabled:
                raise BudgetStoreError("Unknown or disabled bank connection")
            if connection.paused:
                raise BudgetPausedError("Bank connection is paused")
            self._owner(session, connection.owner)
            account = session.scalar(select(Account).where(
                Account.connection_id == connection_id, Account.owner == connection.owner,
                Account.identification_hash == identity_hash, Account.configured, Account.enabled))
            if account is None:
                raise BudgetStoreError(_SELECTION_CHANGED)
            row = self._component(session, account.id, operation)
            if row.lease_until is not None and row.lease_until > now:
                return False
            row.last_attempt, row.attempt_id, row.lease_until = now, attempt_id, now + 900
            row.next_attempt = max(row.next_attempt, connection.next_attempt)
            account.last_attempt = now
            return True

    def record_failure(self, connection_id, code, *, attempt_id=None, diagnostic=None):
        # Do not persist arbitrary exception strings, provider bodies or identifiers.
        safe_code = safe_error(code)
        with self.transaction() as session:
            row = session.get(Connection, connection_id)
            row.failures += 1
            row.last_error = safe_code
            set_diagnostic(row, diagnostic)
            if safe_code in ATTENTION:
                row.attention = safe_code
            # Keep the 12-hour pre-HTTP quota reservation, including after a crash
            # or partial fetch. A later explicit run safely retries after restart.
            row.next_attempt = max(row.next_attempt, self.clock() + 21600)
            if attempt_id is not None:
                accounts = select(Account.id).where(Account.connection_id == connection_id)
                for component in session.scalars(select(Component).where(
                        Component.account_id.in_(accounts), Component.attempt_id == attempt_id)):
                    self._component_error(component, safe_code, diagnostic)

    def release_round(self, connection_id, attempt_id):
        """Release only this round's markers; immutable debits and floors survive."""
        with self.transaction() as session:
            accounts = select(Account.id).where(Account.connection_id == connection_id)
            for component in session.scalars(select(Component).where(
                    Component.account_id.in_(accounts), Component.attempt_id == attempt_id)):
                component.attempt_id, component.lease_until = None, None

    def _snapshot_accounts(self, session, connection, snapshot, *, expected_selection=None):
        self._owner(session, connection.owner)
        accounts = {a.identification_hash: a for a in session.scalars(select(Account).where(
            Account.connection_id == connection.id, Account.owner == connection.owner, Account.enabled, Account.configured))}
        if expected_selection is not None and set(accounts) != set(expected_selection):
            raise BudgetPausedError(_SELECTION_CHANGED)
        observations = (*snapshot.accounts, *snapshot.errors)
        if (len(observations) != len(accounts)
                or {a.identification_hash for a in observations} != set(accounts)):
            raise BudgetStoreError(_SELECTION_CHANGED)
        return accounts

    def commit_snapshot(self, connection_id, snapshot, *, attempt_id=None, expected_version=None,
                        expected_selection=None):
        window = _snapshot_window(snapshot)
        now = self.clock()
        with self.transaction() as session:
            row = session.get(Connection, connection_id)
            if row is None or not row.enabled:
                raise BudgetStoreError("Unknown or disabled bank connection")
            if row.paused or (expected_version is not None and row.control_version != expected_version):
                raise BudgetPausedError("Bank connection is paused")
            accounts = self._snapshot_accounts(session, row, snapshot, expected_selection=expected_selection)
            if snapshot.valid_until <= now:
                raise BudgetStoreError("Bank session has expired")
            for error in snapshot.errors:
                if error.code != "limit_exceeded":
                    raise BudgetStoreError("Invalid global acquisition failure")
                account = accounts[error.identification_hash]
                # A global resource cap can skip an account without any HTTP.
                account.last_error = safe_error(error.code)
            completed, errors = 0, len(snapshot.errors)
            for observation in snapshot.accounts:
                good, bad = self._import_account(session, accounts[observation.identification_hash],
                                               observation, now, attempt_id, window)
                completed += good
                errors += bad
                apply_bank_metadata(session, accounts[observation.identification_hash].id,
                                    getattr(observation, "bank_metadata", None), source="api", observed_at=now)
            row.valid_until = snapshot.valid_until
            codes = {safe_error(getattr(observation, op + "_error")) for observation in snapshot.accounts
                     for op in OPERATIONS if getattr(observation, op + "_error", None) is not None}
            if codes & ATTENTION:
                # Central, so the CLI and every worker stop unattended retries alike.
                row.attention = min(codes & ATTENTION)
            elif completed:
                row.attention = None
            if errors:
                row.failures += 1
                row.last_error = "partial_data" if completed else "account_errors"
                set_diagnostic(row)  # The component rows carry the per-request diagnostics.
            elif completed == len(accounts) * len(OPERATIONS):
                row.last_success = now
                row.failures, row.last_error = 0, None
                set_diagnostic(row)
            reconcile_transfers(session, row.owner, now)

    @staticmethod
    def _component_error(component, code, diagnostic=None):
        component.last_error = code
        set_diagnostic(component, diagnostic)
        component.failures += 1
        component.attempt_id, component.lease_until = None, None

    def _apply_component(self, session, account, observation, operation, now, attempt_id, window=None):
        data = getattr(observation, operation)
        error = getattr(observation, operation + "_error", None)
        if data is None and error is None:
            return 0, 0
        if data is not None and error is not None:
            raise BudgetStoreError("Ambiguous component result")
        component = self._component(session, account.id, operation)
        if attempt_id is not None and component.attempt_id != attempt_id:
            foreign_active = (component.attempt_id is not None
                              and (component.lease_until is None or component.lease_until > now))
            if error != "limit_exceeded" or foreign_active:
                raise BudgetStoreError("Component acquisition reservation changed")
        if attempt_id is None:
            component.last_attempt = now
        if error is not None:
            self._component_error(component, safe_error(error), getattr(observation, operation + "_diagnostic", None))
            return 0, 1
        if operation == "balances":
            account.balances_json = _json(data)
        else:
            self._import_transactions(session, account, data, now, window)
        component.last_success, component.last_error, component.failures = now, None, 0
        set_diagnostic(component)
        component.attempt_id, component.lease_until = None, None
        return 1, 0

    def _import_account(self, session, account, observation, now, attempt_id, window=None):
        results = [self._apply_component(session, account, observation, operation, now, attempt_id, window)
                   for operation in OPERATIONS]
        completed = sum(good for good, _ in results)
        errors = sum(bad for _, bad in results)
        if completed or errors:
            if attempt_id is None:
                account.last_attempt = now
            failures = [(op, self._component(session, account.id, op).last_error) for op in OPERATIONS]
            failures = [(op, code) for op, code in failures if code is not None]
            account.last_error = ("component_errors" if len(failures) == 2 else
                                  failures[0][0] + "_" + failures[0][1] if failures else None)
        if completed == len(OPERATIONS):
            account.observed_at = now
        return completed, errors

    def _import_transactions(self, session, account, transactions, now, window=None):
        kept = []
        for row in session.scalars(select(Unresolved).where(Unresolved.account_id == account.id)):
            payload = json.loads(row.payload_json)
            if _covered(_effective_date(payload), window):
                session.delete(row)
            else:
                kept.append((row.ordinal, payload))
        session.flush()
        ordinal = max((item[0] for item in kept), default=-1) + 1
        kept_payloads = {_json(payload) for _, payload in kept}
        kept_refs = Counter(filter(None, (_ledger_reference(payload) for _, payload in kept)))
        incoming = [tx for tx in transactions
                    if not (tx["entry_reference"] is not None and not _covered(_effective_date(tx), window)
                            and _json(tx) in kept_payloads)]
        references = kept_refs + Counter(filter(None, map(_ledger_reference, incoming)))
        for tx in incoming:
            conflict = tx["entry_reference"] is not None and references[tx["entry_reference"]] > 1
            ordinal = self._import_transaction(session, account.id, tx, conflict, ordinal, now, window)

    @staticmethod
    def _uncertain_outside_window(existing, window):
        if existing is None or not existing.uncertain or window is None:
            return False
        return not _covered(_effective_date(json.loads(existing.payload_json)), window)

    @staticmethod
    def _import_transaction(session, account_id, tx, conflict, ordinal, now, window=None):
        ref = tx["entry_reference"]
        existing = None if ref is None else session.get(Transaction, (account_id, ref))
        if not conflict and _ledger_reference(tx) is not None and BudgetStore._uncertain_outside_window(existing, window):
            conflict = True
        if conflict:
            prior = existing
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
            metadata = {}
            if snapshot.metadata is not None:
                try:
                    state = apply_account_metadata(session, account.id, snapshot.metadata, now=self.clock())
                    metadata = {"metadata": {"state": state, "product_rejected": int(snapshot.metadata.product_rejected)}}
                except BudgetMetadataError as exc:
                    raise BudgetStoreError("Invalid account display metadata: " + exc.code) from None
            batch = session.scalar(select(ImportBatch).where(ImportBatch.fingerprint == snapshot.digest))
            if batch is not None:
                if batch.account_id != account.id:
                    raise BudgetStoreError("Offline import identity mismatch")
                report = {**json.loads(batch.report_json), "duplicate": True}
                for field in ("inserted", "updated", "preserved_api", "stale", "unresolved", "unchanged"):
                    report[field] = 0
                bank = apply_bank_metadata(session, account.id, getattr(snapshot, "bank_metadata", None),
                                           source="offline_import", observed_at=snapshot.fetched_at)
                if bank is not None:
                    metadata["bank_metadata"] = bank
                reconcile_transfers(session, item.owner, self.clock())
                return {**report, **metadata}
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
            bank = apply_bank_metadata(session, account.id, getattr(snapshot, "bank_metadata", None),
                                       source="offline_import", observed_at=snapshot.fetched_at)
            if bank is not None:
                metadata["bank_metadata"] = bank
            reconcile_transfers(session, item.owner, self.clock())
            return {**report, **metadata}

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

    @staticmethod
    def _component_status(component, operation, account, connection, now, shared):
        success = getattr(component, "last_success", None)
        age = None if success is None else max(0, now - success)
        error = getattr(component, "last_error", None)
        deadlines = [stamp for stamp in (getattr(component, "retry_at", None), shared["retry_at"]) if stamp is not None]
        retry_at = max(deadlines, default=None)
        suspended = getattr(component, "suspended", False) or shared["suspended"]
        next_attempt = max(shared["next_attempt"], getattr(component, "next_attempt", 0),
                           getattr(component, "backoff_until", 0), retry_at or 0)
        state, error = BudgetStore._component_state(component, account, connection, now,
                                                    success, age, error, next_attempt, operation, suspended)
        return {"last_attempt": getattr(component, "last_attempt", None),
                "last_success": success, "last_error": error,
                "failures": getattr(component, "failures", 0),
                "next_attempt": next_attempt, "state": state,
                "retry_at": retry_at, "suspended": suspended,
                "stale": age is None or age >= STALE_AFTER[operation],
                "age_seconds": age, "stale_after_seconds": STALE_AFTER[operation]}

    @staticmethod
    def _component_blocked(account, connection, now):
        return (not (account.enabled and account.configured and connection.enabled) or connection.paused
                or connection.valid_until is not None and connection.valid_until <= now
                or connection.last_error in {"authorization", "invalid_session", "expired_session", "session_mismatch"})

    @staticmethod
    def _component_state(component, account, connection, now, success, age, error, next_attempt, operation, suspended=False):
        if suspended or BudgetStore._component_blocked(account, connection, now):
            return "blocked", error
        if getattr(component, "attempt_id", None) is not None:
            lease = component.lease_until
            return ("running", error) if lease is not None and lease > now else ("error", "interrupted")
        if error is not None:
            return "error", error
        if next_attempt > now:
            return "deferred", None
        if success is None:
            return "never", None
        return ("ready" if age >= STALE_AFTER[operation] else "ok"), None

    def _account_status(self, session, account, connection, now, shared):
        return {"id": account.id, "enabled": account.enabled, "configured": account.configured,
                "control_version": account.control_version, "paused": connection.paused,
                "kind": account.kind, "kind_confirmed_at": account.kind_confirmed_at,
                "last_attempt": account.last_attempt, "last_success": account.observed_at,
                "last_error": account.last_error,
                "components": {op: self._component_status(session.get(Component, (account.id, op)),
                                                           op, account, connection, now, shared) for op in OPERATIONS}}

    def status(self, *, owner=None):
        now = self.clock()
        with self.sessions() as session:
            session.execute(text("BEGIN"))
            return self._status_rows(session, owner, now)

    def _status_rows(self, session, owner, now):
        from .scheduler import shared_state

        query = select(Connection).order_by(Connection.id)
        if owner is not None:
            query = query.where(Connection.owner == owner)
        result = []
        for row in session.scalars(query):
            shared = shared_state(session, row)
            accounts = select(Account.id).where(Account.connection_id == row.id, Account.owner == row.owner)
            selected = session.scalar(select(func.count()).select_from(Account).where(
                Account.connection_id == row.id, Account.owner == row.owner, Account.enabled, Account.configured))
            configured = session.scalar(select(func.count()).select_from(Account).where(
                Account.connection_id == row.id, Account.owner == row.owner, Account.configured))
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
                               "paused": row.paused, "control_version": row.control_version,
                           "selection_policy_version": 2, "configured_accounts": configured,
                           "selected_accounts": selected, "transactions": canonical, "unresolved": unresolved,
                           "uncertain_transactions": uncertain,
                           "offline_imports": import_count, "offline_transactions": imported,
                           "offline_unresolved": import_unresolved,
                           "last_attempt": row.last_attempt, "last_success": row.last_success,
                           "next_attempt": row.next_attempt, "last_error": row.last_error,
                           "failures": row.failures, "valid_until": row.valid_until,
                           "request_limits": shared,
                           "renewal_due": row.valid_until is not None and row.valid_until - now <= 604800,
                           "accounts": [self._account_status(session, account, row, now, shared)
                                        for account in session.scalars(select(Account).where(
                                            Account.connection_id == row.id, Account.owner == row.owner).order_by(Account.id))]})
        return result
