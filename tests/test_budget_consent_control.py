"""Control pacing uses synthetic SQLite only; no network, secrets or sleeps."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from fractions import Fraction
from threading import Barrier
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import select

from hestia import db
from hestia.budget.config import AcquisitionPolicy
from hestia.budget.consent_control import BudgetConsentControlStore
from hestia.budget.job_store import BudgetUsersRequired
from hestia.budget.rate_limit import RateScope
from hestia.budget.scheduler import BudgetDeferredError, BudgetScheduler, provider_key
from hestia.budget.store import BudgetNotFound, BudgetStoreError
from tests.test_budget_job_store import JobStoreCase


class ConsentControlTests(JobStoreCase):
    def setUp(self):
        super().setUp()
        self.store = BudgetConsentControlStore(self.sessions, clock=lambda: self.now)
        self.selected = self.config.connections[0]
        self.application = self.config.application_id

    def wait(self, connection=None, **kwargs):
        return self.store.control_wait("owner", self.selected if connection is None else connection,
                                       application_id=self.application, **kwargs)

    def limit(self, scope, key, **values):
        with self.sessions.begin() as session:
            row = session.get(db.BudgetRequestLimit, (scope, key))
            if row is None:
                row = db.BudgetRequestLimit(scope=scope, key=key)
                session.add(row)
            for name, value in values.items():
                setattr(row, name, value)

    def immutable(self):
        with self.sessions() as session:
            tables = (db.BudgetAccount, db.BudgetComponent, db.BudgetAcquisition,
                      db.BudgetAccountAudit, db.BudgetConnectionAudit, db.BudgetConsentAudit)
            return {table.__tablename__: [self.store._internal(row) for row in session.scalars(select(table))]
                    for table in tables}

    def test_explicit_renewal_ignores_acquisition_state_without_mutating_it(self):
        for account in self.store.accounts("owner"):
            self.store.set_account_selected("owner", account["id"], False, actor="owner")
        with self.sessions.begin() as session:
            connection = session.get(db.BudgetConnection, "bank")
            connection.paused, connection.attention = True, "expired_session"
            connection.valid_until, connection.next_attempt = self.now - 1, self.now + 999999
            connection.failures, connection.last_error = 3, "authorization"
            component = session.get(db.BudgetComponent, (self.accounts["hash-a"], "transactions"))
            component.suspended, component.next_attempt = True, self.now + 9999999
            session.add(db.BudgetAcquisition(id="prior", account_id=self.accounts["hash-a"],
                        operation="transactions", attempt_id="prior", started_at=self.now - 2, origin="api"))
        before = self.immutable()
        connection_before = self.store._internal(self.connection())
        self.assertEqual(self.wait(), 0)
        self.assertEqual(self.immutable(), before)
        self.assertEqual(self.store._internal(self.connection()), connection_before)
        application = self.rows(db.BudgetRequestLimit)[0]
        self.assertEqual((application.scope, application.key, application.last_request_at, application.next_request_at),
                         ("application", self.application, self.now, self.now + 1))

    def test_cross_connection_and_restarted_workers_share_one_transport_slot(self):
        self.assertEqual(self.wait(), 0)
        self.store = BudgetConsentControlStore(self.sessions, clock=lambda: self.now)
        other = self.config.connections[1]
        self.assertEqual(self.store.control_wait("other", other, application_id=self.application), 1)
        self.assertEqual(self.wait(), 1)
        self.now += 1
        self.assertEqual(self.store.control_wait("other", other, application_id=self.application), 0)
        self.assertEqual(self.wait(), 1)
        self.assertEqual(len(self.rows(db.BudgetRequestLimit)), 1)
        self.assertEqual(self.rows(db.BudgetAcquisition), [])

    def test_each_shared_scope_honors_full_deadline_and_suspension_without_reserving(self):
        scopes = (("application", self.application), ("connection", "bank"),
                  ("provider", provider_key(self.selected.bank_name, self.selected.country)))
        for scope, key in scopes:
            with self.subTest(scope=scope):
                self.limit(scope, key, next_attempt=self.now + 3600, retry_at=self.now + 90000)
                self.assertEqual(self.wait(), 90000)
                limit = next(row for row in self.rows(db.BudgetRequestLimit) if row.scope == scope)
                self.assertIsNone(limit.last_request_at)
                self.limit(scope, key, suspended=True)
                with self.assertRaises(BudgetDeferredError):
                    self.wait()
                self.limit(scope, key, next_attempt=0, retry_at=None, suspended=False)
        self.assertEqual(self.wait(), 0)
        self.assertEqual(self.rows(db.BudgetAcquisition), [])

    def test_latest_shared_floor_and_pacing_win_without_capping_server_deadlines(self):
        self.limit("application", self.application, next_attempt=self.now + 50,
                   last_request_at=self.now, next_request_at=self.now + 100)
        self.limit("connection", "bank", retry_at=self.now + 200)
        self.limit("provider", provider_key(self.selected.bank_name, self.selected.country), next_attempt=self.now + 300)
        self.assertEqual(self.wait(), 300)
        application = next(row for row in self.rows(db.BudgetRequestLimit) if row.scope == "application")
        self.assertEqual(application.last_request_at, self.now)
        self.limit("provider", provider_key(self.selected.bank_name, self.selected.country), next_attempt=1e128)
        self.assertEqual(self.wait(), 1e128 - self.now)

    def test_configured_interval_tightening_and_operator_minimum_never_shorten_prior_pacing(self):
        self.assertEqual(self.wait(), 0)
        stricter = replace(self.selected, policy=AcquisitionPolicy(request_interval_seconds=10))
        self.assertEqual(self.wait(stricter), 10)
        self.assertEqual(self.wait(stricter, request_interval_seconds=2), 10)
        self.assertEqual(self.wait(stricter, request_interval_seconds=20), 20)
        self.now += 20
        self.assertEqual(self.wait(stricter, request_interval_seconds=20), 0)
        self.assertEqual(self.wait(), 20)
        for value in (0, -1, 61, True, "1", 1.5):
            with self.subTest(interval=value), self.assertRaises(BudgetStoreError):
                self.wait(request_interval_seconds=value)

    def test_clock_rollback_and_fractional_deadlines_remain_conservative(self):
        self.now = 1_000_000.1
        original = self.now
        self.assertEqual(self.wait(), 0)
        application = self.rows(db.BudgetRequestLimit)[0]
        self.assertGreaterEqual(Fraction(application.next_request_at), Fraction(original) + 1)
        self.now -= .25
        self.assertEqual(self.wait(), application.next_request_at - self.now)
        self.now = application.next_request_at
        self.assertEqual(self.wait(), 0)
        for value in (-1, None, "bad", True, float("nan"), float("inf"), 10 ** 1000):
            self.now = value
            with self.subTest(clock=type(value).__name__), self.assertRaises(BudgetStoreError):
                self.wait()

    def test_owned_enabled_identity_and_authoritative_admin_checked_before_reservation(self):
        for changes in ({"id": "missing"}, {"id": "other-bank", "owner": "other"},
                        {"owner": "other"}, {"bank_name": "Another Bank"}, {"country": "IE"}):
            with self.subTest(changes=changes), self.assertRaises(BudgetNotFound):
                self.wait(replace(self.selected, **changes))
        with self.assertRaises(BudgetNotFound):
            self.store.control_wait("owner", self.selected, application_id="another-application")
        with self.sessions.begin() as session:
            session.get(db.BudgetConnection, "bank").enabled = False
        with self.assertRaises(BudgetNotFound):
            self.wait()
        with self.sessions.begin() as session:
            session.get(db.BudgetConnection, "bank").enabled = True
            session.get(db.User, "owner").disabled = True
        with self.assertRaises(BudgetStoreError):
            self.wait()
        with self.sessions.begin() as session:
            session.get(db.User, "owner").disabled = False
            session.delete(session.get(db.AppMeta, "users_authoritative"))
        with self.assertRaises(BudgetUsersRequired):
            self.wait()
        self.assertEqual(self.rows(db.BudgetRequestLimit), [])

    def test_two_workers_cannot_both_reserve_the_same_application_instant(self):
        barrier = Barrier(2)
        def worker():
            store = BudgetConsentControlStore(self.sessions, clock=lambda: self.now)
            barrier.wait(timeout=5)
            return store.control_wait("owner", self.selected, application_id=self.application)
        with ThreadPoolExecutor(max_workers=2) as pool:
            answers = list(pool.map(lambda _: worker(), range(2)))
        self.assertEqual(sorted(answers), [0, 1])
        self.assertEqual(self.rows(db.BudgetAcquisition), [])

    def test_acquisition_and_control_share_the_existing_application_transport_clock(self):
        self.assertEqual(self.wait(), 0)
        scheduler = BudgetScheduler(self.store, application_id=self.application)
        self.assertEqual(scheduler.request_wait(self.selected), 1)
        self.now += 1
        self.assertEqual(scheduler.request_wait(self.selected), 0)
        self.assertEqual(self.wait(), 1)

    def test_429_records_late_shared_metadata_without_debits_or_lost_owner_revocation(self):
        before = self.immutable()
        with self.sessions.begin() as session:
            session.get(db.User, "owner").disabled = True
        error = SimpleNamespace(status=429, code="rate_limit", rate_scope=RateScope.CONNECTION,
                                retry_at=self.now + 90000, retry_suspended=False)
        self.store.record_control_rate_limit(self.selected, error, application_id=self.application)
        self.assertEqual(self.immutable(), before)
        row = self.rows(db.BudgetRequestLimit)[0]
        self.assertEqual((row.scope, row.next_attempt, row.retry_at), ("connection", self.now + 90000, self.now + 90000))
        self.assertEqual(self.connection().next_attempt, self.now + 90000)
        with self.sessions.begin() as session:
            session.get(db.User, "owner").disabled = False
        self.assertEqual(self.wait(), 90000)
        error.rate_scope, error.retry_at, error.retry_suspended = RateScope.APPLICATION, self.now + 900000, True
        self.store.record_control_rate_limit(self.selected, error, application_id=self.application)
        with self.assertRaises(BudgetDeferredError):
            self.wait()
        self.assertEqual(self.rows(db.BudgetAcquisition), [])

    def test_wait_holds_no_lock_after_return_and_samples_time_inside_transaction(self):
        held = [False]
        original = self.store.transaction
        from contextlib import contextmanager
        @contextmanager
        def checked():
            with original() as session:
                held[0] = True
                try:
                    yield session
                finally:
                    held[0] = False
        self.store.clock = lambda: self.now if held[0] else (_ for _ in ()).throw(AssertionError("Clock sampled too early"))
        with mock.patch.object(self.store, "transaction", checked):
            self.assertEqual(self.wait(), 0)
        self.assertFalse(held[0])
        with self.sessions.begin() as session:
            self.assertIsNotNone(session.get(db.BudgetRequestLimit, ("application", self.application)))
