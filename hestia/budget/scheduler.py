"""Short SQLite reservations; no sleeping, network or secret reads occur here."""
from __future__ import annotations

from fractions import Fraction
import hashlib
import json
import math
import uuid

from sqlalchemy import select

from ..db import BudgetAccount as Account, BudgetAcquisition as Acquisition
from ..db import BudgetComponent as Component, BudgetConnection as Connection
from ..db import BudgetRequestLimit as RequestLimit
from .normalize import BudgetDataError
from .rate_limit import RateScope
from .store import BudgetPausedError, BudgetStoreError, OPERATIONS, _SELECTION_CHANGED

WINDOW = 86400
LEASE_SECONDS = 900
RATE_BACKOFF = 21600


class BudgetDeferredError(BudgetDataError):
    def __init__(self):
        super().__init__("deferred")


def provider_key(bank_name, country):
    """Stable provider identity, independent of session and account UIDs."""
    raw = json.dumps([bank_name, country], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _future(now, seconds):
    exact = Fraction(now) + seconds
    try:
        deadline = float(exact)
    except OverflowError:
        raise BudgetStoreError("Invalid acquisition clock") from None
    if Fraction(deadline) < exact:
        deadline = math.nextafter(deadline, math.inf)
    if not math.isfinite(deadline):
        raise BudgetStoreError("Invalid acquisition clock")
    return deadline


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _valid_until(value, now):
    return _finite(value) and value > now


def _lease(component, attempt_id, now):
    return (component is not None and component.attempt_id == attempt_id
            and component.lease_until is not None and component.lease_until > now)


def shared_state(session, connection):
    """Read effective shared state without creating rows or exposing scope keys."""
    rows = [row for row in BudgetScheduler._shared(session, connection) if row is not None]
    deadlines = [row.retry_at for row in rows if row.retry_at is not None]
    return {"next_attempt": max([connection.next_attempt] + [max(row.next_attempt, row.retry_at or 0) for row in rows]),
            "retry_at": max(deadlines, default=None), "suspended": any(row.suspended for row in rows)}


class BudgetScheduler:
    def __init__(self, store, *, application_id=None):
        self.store, self.application_id = store, application_id

    def _now(self):
        try:
            now = self.store.clock()
            if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
                raise ValueError
        except (ValueError, OverflowError):
            raise BudgetStoreError("Invalid acquisition clock") from None
        return now

    def _connection(self, session, config, *, active=True):
        row = session.get(Connection, config.id)
        if row is None or (row.owner, row.bank_name, row.country) != (config.owner, config.bank_name, config.country):
            raise BudgetStoreError("Bank connection identity changed")
        if self.application_id is not None and row.application_id != self.application_id:
            raise BudgetStoreError("Bank connection identity changed")
        if active:
            self.store._owner(session, row.owner)
            if not row.enabled:
                raise BudgetStoreError("Unknown or disabled bank connection")
        return row

    @staticmethod
    def _account(session, config, primary, *, active=True):
        account = session.scalar(select(Account).where(Account.connection_id == config.id,
                                                        Account.owner == config.owner,
                                                        Account.identification_hash == primary))
        if account is None or (active and (primary not in config.accounts or not account.enabled or not account.configured)):
            raise BudgetStoreError(_SELECTION_CHANGED)
        return account

    @staticmethod
    def _shared(session, connection):
        keys = (("application", connection.application_id), ("connection", connection.id),
                ("provider", provider_key(connection.bank_name, connection.country)))
        return [session.get(RequestLimit, key) for key in keys]

    @staticmethod
    def _blocked(connection, limits, now):
        return connection.paused or connection.next_attempt > now or any(
            row is not None and (row.suspended or max(row.next_attempt, row.retry_at or 0) > now)
            for row in limits)

    @staticmethod
    def _component_blocked(component, now):
        return component is not None and (
            component.suspended or max(component.next_attempt, component.retry_at or 0, component.backoff_until) > now
            or (component.lease_until is not None and component.lease_until > now))

    @staticmethod
    def _attempts(session, account_id, operation, policy, now):
        lower = math.nextafter(now - max(WINDOW, policy.minimum_interval_seconds), -math.inf)
        return list(session.scalars(select(Acquisition.started_at).where(
            Acquisition.account_id == account_id, Acquisition.operation == operation,
            Acquisition.started_at > lower).order_by(Acquisition.started_at.desc()).limit(policy.max_acquisitions_24h)))

    def _due(self, session, account, component, operation, policy, now):
        attempts = self._attempts(session, account.id, operation, policy, now)
        previous = attempts + ([component.last_attempt] if component is not None and component.last_attempt is not None else [])
        floor = _future(max(previous), policy.minimum_interval_seconds) if previous else 0
        recent = [stamp for stamp in attempts if Fraction(stamp) > Fraction(now) - WINDOW]
        if len(recent) >= policy.max_acquisitions_24h:
            floor = max(floor, _future(recent[-1], WINDOW))
        if component is not None:
            component.next_attempt = max(component.next_attempt, floor)
        return floor <= now and not self._component_blocked(component, now)

    def _active_accounts(self, session, config):
        return list(session.scalars(select(Account).where(
            Account.connection_id == config.id, Account.owner == config.owner, Account.enabled, Account.configured,
            Account.identification_hash.in_(config.accounts))))

    def _any_due(self, session, accounts, policy, now, target=None):
        return any(self._due(session, account, session.get(Component, (account.id, operation)),
                             operation, policy, now) for account in accounts for operation in OPERATIONS
                   if target is None or (account.identification_hash, operation) in target)

    def can_start(self, connection, *, target=None):
        """Whether any (targeted) selected component is due; never performs HTTP or secret reads."""
        now = self._now()
        with self.store.transaction() as session:
            row = self._connection(session, connection)
            if self._blocked(row, self._shared(session, row), now):
                return False
            return self._any_due(session, self._active_accounts(session, connection), connection.policy, now, target)

    @staticmethod
    def _validate_attempt(operation, attempt_id):
        if operation not in OPERATIONS or not isinstance(attempt_id, str) or not attempt_id or len(attempt_id) > 128:
            raise BudgetStoreError("Invalid component acquisition")

    def _check_selection(self, session, connection, expected_selection):
        if (expected_selection is not None
                and self.store._selected_hashes(session, connection.id, connection.owner) != set(expected_selection)):
            raise BudgetPausedError(_SELECTION_CHANGED)

    def reserve_component(self, connection, primary, operation, *, attempt_id, valid_until, expected_selection=None):
        self._validate_attempt(operation, attempt_id)
        now = self._now()
        with self.store.transaction() as session:
            row = self._connection(session, connection)
            self._check_selection(session, connection, expected_selection)
            account = self._account(session, connection, primary)
            if not _valid_until(valid_until, now) or self._blocked(row, self._shared(session, row), now):
                return False
            component = session.get(Component, (account.id, operation))
            prior = session.scalar(select(Acquisition.id).where(Acquisition.account_id == account.id,
                                   Acquisition.operation == operation, Acquisition.attempt_id == attempt_id))
            if prior is not None:
                return (_lease(component, attempt_id, now) and not component.suspended
                        and max(component.retry_at or 0, component.backoff_until) <= now)
            if not self._due(session, account, component, operation, connection.policy, now):
                return False
            component = self.store._component(session, account.id, operation)
            session.add(Acquisition(id=str(uuid.uuid4()), account_id=account.id, operation=operation,
                                    attempt_id=attempt_id, started_at=now, origin="api"))
            component.last_attempt, component.attempt_id, component.lease_until = now, attempt_id, _future(now, LEASE_SECONDS)
            interval = max(connection.policy.minimum_interval_seconds,
                           WINDOW if connection.policy.max_acquisitions_24h == 1 else 0)
            component.next_attempt = max(component.next_attempt, _future(now, interval))
            account.last_attempt = now
            return True

    def _request_component(self, session, connection, primary, operation, attempt_id, valid_until, now):
        self._validate_attempt(operation, attempt_id)
        account = self._account(session, connection, primary)
        component = session.get(Component, (account.id, operation))
        if not _valid_until(valid_until, now) or not _lease(component, attempt_id, now):
            raise BudgetDeferredError()
        debit = session.scalar(select(Acquisition.id).where(Acquisition.account_id == account.id,
                                Acquisition.operation == operation, Acquisition.attempt_id == attempt_id))
        if debit is None or component.suspended or max(component.retry_at or 0, component.backoff_until) > now:
            raise BudgetDeferredError()

    def request_wait(self, connection, *, primary=None, operation=None, attempt_id=None, valid_until=None,
                     target=None, expected_selection=None):
        if (primary is None) != (operation is None):
            raise BudgetStoreError("Invalid component request")
        now = self._now()
        with self.store.transaction() as session:
            row = self._connection(session, connection)
            self._check_selection(session, connection, expected_selection)
            limits = self._shared(session, row)
            if self._blocked(row, limits, now):
                raise BudgetDeferredError()
            if primary is not None:
                self._request_component(session, connection, primary, operation, attempt_id, valid_until, now)
            else:
                accounts = self._active_accounts(session, connection)
                if not accounts:
                    raise BudgetStoreError(_SELECTION_CHANGED)
                if not self._any_due(session, accounts, connection.policy, now, target):
                    raise BudgetDeferredError()
            application = limits[0]
            if application is None:
                application = RequestLimit(scope="application", key=row.application_id)
                session.add(application)
                session.flush()
            pacing = max(application.next_request_at,
                         _future(application.last_request_at, connection.policy.request_interval_seconds)
                         if application.last_request_at is not None else 0)
            if pacing > now:
                delay = pacing - now
                if delay > 60:
                    raise BudgetDeferredError()
                return delay
            application.last_request_at = now
            application.next_request_at = _future(now, connection.policy.request_interval_seconds)
            if primary is None:
                row.last_attempt = now
            return 0.0

    @staticmethod
    def _limit(session, scope, key):
        row = session.get(RequestLimit, (scope, key))
        if row is None:
            row = RequestLimit(scope=scope, key=key)
            session.add(row)
            session.flush()
        return row

    @staticmethod
    def _retry_metadata(error, valid_until, now):
        retry_at = getattr(error, "retry_at", None)
        valid = _finite(retry_at) and retry_at >= 0
        suspended = bool(getattr(error, "retry_suspended", False)) or (retry_at is not None and not valid)
        if valid and type(valid_until) in (int, float) and retry_at >= valid_until:
            suspended = True
        if valid and Fraction(retry_at) - Fraction(now) > 604800:
            suspended = True
        return retry_at if valid else None, suspended

    def record_rate_limit(self, connection, *, primary, operation, error, valid_until=None):
        if getattr(error, "status", None) != 429 and getattr(error, "code", None) != "rate_limit":
            return
        now = self._now()
        retry_at, suspended = self._retry_metadata(error, valid_until, now)
        with self.store.transaction() as session:
            row = self._connection(session, connection, active=False)
            scope = getattr(error, "rate_scope", RateScope.CONNECTION)
            if scope is RateScope.ACCOUNT_ENDPOINT and primary is not None and operation in OPERATIONS:
                account = self._account(session, connection, primary, active=False)
                target = self.store._component(session, account.id, operation)
                target.backoff_until = max(target.backoff_until, _future(now, RATE_BACKOFF), retry_at or 0)
            else:
                scope = "application" if scope is RateScope.APPLICATION else "connection"
                target = self._limit(session, scope, row.application_id if scope == "application" else row.id)
                if scope == "connection":
                    row.next_attempt = max(row.next_attempt, _future(now, RATE_BACKOFF), retry_at or 0)
            target.next_attempt = max(target.next_attempt, _future(now, RATE_BACKOFF), retry_at or 0)
            if retry_at is not None:
                target.retry_at = max(target.retry_at or 0, retry_at)
            target.suspended = target.suspended or suspended
