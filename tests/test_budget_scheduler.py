"""Synthetic durable quota, transport pacing, scoped backoff and migration."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from fractions import Fraction
import math
import sys
from threading import Barrier
from types import SimpleNamespace
import unittest

from alembic import command
from sqlalchemy import select

from hestia import db
from hestia.budget.config import AcquisitionPolicy
from hestia.budget.rate_limit import RateScope
from hestia.budget.scheduler import BudgetDeferredError, BudgetScheduler, provider_key, shared_state
from hestia.budget.store import BudgetStore, BudgetStoreError
from tests import test_budget_store as fixtures


class BudgetSchedulerTests(unittest.TestCase):
    setUp_database = fixtures.BudgetStoreTests.setUp
    tearDown = fixtures.BudgetStoreTests.tearDown
    rows = fixtures.BudgetStoreTests.rows

    def setUp(self):
        self.setUp_database()
        self.now = 1000000
        self.config = fixtures.configuration(accounts=("synthetic-hash", "second"))
        self.connection = self.config.connections[0]
        fixtures.prepare_selected(self.store, self.config)
        self.scheduler = BudgetScheduler(self.store, application_id=fixtures.APP)
        self.expiry = self.now + 2000000

    def component(self, primary="synthetic-hash", operation="transactions"):
        account = next(row for row in self.rows(db.BudgetAccount) if row.identification_hash == primary)
        return next(row for row in self.rows(db.BudgetComponent)
                    if row.account_id == account.id and row.operation == operation)

    def reserve(self, primary="synthetic-hash", operation="transactions", attempt="round", **kwargs):
        return self.scheduler.reserve_component(self.connection, primary, operation,
                                                attempt_id=attempt, valid_until=kwargs.get("expiry", self.expiry))

    def request(self, primary="synthetic-hash", operation="transactions", attempt="round", **kwargs):
        return self.scheduler.request_wait(self.connection, primary=primary, operation=operation,
                                           attempt_id=attempt, valid_until=kwargs.get("expiry", self.expiry))

    def limit(self, scope, key, **fields):
        with self.sessions.begin() as session:
            row = session.get(db.BudgetRequestLimit, (scope, key))
            if row is None:
                row = db.BudgetRequestLimit(scope=scope, key=key)
                session.add(row)
            for field, value in fields.items():
                setattr(row, field, value)

    def error(self, scope=RateScope.ACCOUNT_ENDPOINT, **changes):
        return SimpleNamespace(**dict({"status": 429, "code": "rate_limit", "rate_scope": scope,
                                       "retry_at": None, "retry_suspended": False}, **changes))

    def record(self, error, **kwargs):
        self.scheduler.record_rate_limit(self.connection, primary=kwargs.get("primary", "synthetic-hash"),
                                         operation=kwargs.get("operation", "transactions"), error=error,
                                         valid_until=kwargs.get("expiry", self.expiry))

    def test_due_selection_ignores_expired_stored_session_and_creates_no_transport(self):
        with self.sessions.begin() as session:
            session.get(db.BudgetConnection, "bank").valid_until = 1
        self.assertTrue(self.scheduler.can_start(self.connection))
        self.assertEqual(self.rows(db.BudgetAcquisition), [])
        self.assertEqual(self.rows(db.BudgetRequestLimit), [])
        for account in self.store.accounts("owner"):
            self.store.set_account_selected("owner", account["id"], False, actor="operator")
        self.assertFalse(self.scheduler.can_start(self.connection))
        with self.assertRaises(BudgetStoreError):
            self.scheduler.request_wait(self.connection)

    def test_reservation_is_idempotent_and_each_account_operation_has_own_quota(self):
        for expiry in (None, self.now, self.now - 1, math.nan, True, 10 ** 1000):
            with self.subTest(expiry=expiry):
                self.assertFalse(self.reserve(expiry=expiry))
        self.assertTrue(self.reserve())
        self.assertTrue(self.reserve())
        self.assertFalse(self.reserve(attempt="other-round"))
        self.assertTrue(self.reserve(operation="balances"))
        self.assertTrue(self.reserve(primary="second"))
        self.assertEqual(len(self.rows(db.BudgetAcquisition)), 3)
        self.assertTrue(all(row.origin == "api" for row in self.rows(db.BudgetAcquisition)))
        component = self.component()
        self.assertEqual(component.last_attempt, self.now)
        self.assertEqual(component.lease_until, self.now + 900)
        self.assertEqual(component.next_attempt, self.now + 43200)
        self.now += 900
        self.assertFalse(self.reserve())

    def test_twelve_hour_and_rolling_twenty_four_hour_boundaries_survive_restart(self):
        start = self.now
        self.assertTrue(self.reserve(attempt="first"))
        self.now = start + 43200 - .25
        self.assertFalse(self.reserve(attempt="second"))
        self.now = start + 43200
        self.assertTrue(self.reserve(attempt="second"))
        db.reset_engine_cache()
        _, self.sessions = db.init_db(self.path)
        self.store = BudgetStore(self.sessions, clock=lambda: self.now)
        self.scheduler = BudgetScheduler(self.store)
        self.now = start + 86400 - .25
        self.assertFalse(self.reserve(attempt="third"))
        self.now = start + 86400
        self.assertTrue(self.reserve(attempt="third"))
        self.assertEqual(len(self.rows(db.BudgetAcquisition)), 3)

    def test_stricter_policy_cannot_reset_prior_attempts_or_future_deadlines(self):
        start = self.now
        for primary in self.connection.accounts:
            for operation in ("transactions", "balances"):
                self.assertTrue(self.reserve(primary, operation))
        self.now += 43200
        stricter = replace(self.connection, policy=AcquisitionPolicy(max_acquisitions_24h=1))
        self.assertFalse(self.scheduler.can_start(stricter))
        self.assertTrue(all(row.next_attempt == start + 86400 for row in self.rows(db.BudgetComponent)))
        self.now = start + 86400
        self.assertTrue(self.scheduler.can_start(stricter))
        renewed = replace(stricter, session_file="/test/renewed-session.json")
        self.assertTrue(self.scheduler.reserve_component(renewed, "synthetic-hash", "transactions",
                                                         attempt_id="renewed", valid_until=self.expiry))
        self.assertEqual(self.component().next_attempt, self.now + 86400)
        restrictive = replace(self.connection, policy=AcquisitionPolicy(minimum_interval_seconds=604800))
        self.assertFalse(self.scheduler.reserve_component(restrictive, "synthetic-hash", "transactions",
                                                          attempt_id="later", valid_until=self.expiry))
        self.assertEqual(self.component().next_attempt, self.now + 604800)

    def test_history_alone_enforces_rolling_ceiling_when_cadence_metadata_is_missing(self):
        component = self.component()
        with self.sessions.begin() as session:
            for ordinal, age in enumerate((100, 200)):
                session.add(db.BudgetAcquisition(id=str(ordinal), account_id=component.account_id,
                                                 operation="transactions", attempt_id=str(ordinal),
                                                 started_at=self.now - age, origin="api"))
        self.assertFalse(self.reserve())
        self.assertEqual(self.component().next_attempt, self.now - 200 + 86400)

    def test_competing_reservations_charge_once(self):
        barrier = Barrier(2)

        def attempt(identifier):
            barrier.wait()
            return self.reserve(attempt=identifier)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ("first", "second")))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(self.rows(db.BudgetAcquisition)), 1)

    def test_identity_role_selection_and_application_are_rechecked(self):
        for change in ({"id": "missing"}, {"owner": "other"}, {"bank_name": "different"}, {"country": "IE"}):
            with self.subTest(change=change), self.assertRaises(BudgetStoreError):
                self.scheduler.can_start(replace(self.connection, **change))
        with self.assertRaises(BudgetStoreError):
            BudgetScheduler(self.store, application_id="different-app").request_wait(self.connection)
        with self.assertRaises(BudgetStoreError):
            self.reserve(primary="unselected")
        with self.assertRaises(BudgetStoreError):
            self.scheduler.reserve_component(replace(self.connection, accounts=()), "synthetic-hash", "transactions",
                                             attempt_id="round", valid_until=self.expiry)
        account_id = self.component().account_id
        for field in ("enabled", "configured"):
            with self.sessions.begin() as session:
                setattr(session.get(db.BudgetAccount, account_id), field, False)
            with self.assertRaises(BudgetStoreError):
                self.reserve()
            with self.sessions.begin() as session:
                setattr(session.get(db.BudgetAccount, account_id), field, True)
        with self.sessions.begin() as session:
            session.get(db.BudgetConnection, "bank").enabled = False
        with self.assertRaises(BudgetStoreError):
            self.scheduler.can_start(self.connection)
        with self.sessions.begin() as session:
            session.get(db.BudgetConnection, "bank").enabled = True
            session.get(db.User, "owner").disabled = True
        with self.assertRaises(BudgetStoreError):
            self.scheduler.request_wait(self.connection)

    def test_request_context_and_unexpired_debit_lease_are_required(self):
        for operation, attempt in (("unknown", "round"), ("transactions", None),
                                   ("transactions", ""), ("transactions", "x" * 129)):
            with self.subTest(operation=operation, attempt=attempt), self.assertRaises(BudgetStoreError):
                self.reserve(operation=operation, attempt=attempt)
        for kwargs in ({"primary": "synthetic-hash"}, {"operation": "transactions"}):
            with self.assertRaises(BudgetStoreError):
                self.scheduler.request_wait(self.connection, **kwargs)
        with self.assertRaises(BudgetDeferredError):
            self.request()
        component = self.component()
        with self.sessions.begin() as session:
            current = session.get(db.BudgetComponent, (component.account_id, component.operation))
            current.attempt_id, current.lease_until = "round", self.now + 900
        with self.assertRaises(BudgetDeferredError):
            self.request()
        with self.sessions.begin() as session:
            session.get(db.BudgetComponent, (component.account_id, component.operation)).lease_until = None
        self.assertTrue(self.reserve())
        with self.assertRaises(BudgetDeferredError):
            self.request(attempt="wrong")
        with self.assertRaises(BudgetDeferredError):
            self.request(expiry=self.now)
        self.now += 900
        with self.assertRaises(BudgetDeferredError):
            self.request()

    def test_pagination_shares_one_debit_and_every_get_is_paced(self):
        self.assertEqual(self.scheduler.request_wait(self.connection), 0)
        self.assertEqual(self.scheduler.request_wait(self.connection), 1)
        self.assertTrue(self.reserve())
        self.assertEqual(self.request(), 1)
        self.now += 1
        self.assertEqual(self.request(), 0)
        self.assertEqual(self.request(), 1)
        self.now += 1
        self.assertEqual(self.request(), 0)
        self.assertEqual(len(self.rows(db.BudgetAcquisition)), 1)
        self.assertEqual(self.rows(db.BudgetConnection)[0].last_attempt, self.now - 2)
        self.assertEqual(self.rows(db.BudgetRequestLimit)[0].last_request_at, self.now)

    def test_application_pacing_spans_connections_and_never_reserves_a_future_slot(self):
        other = replace(self.connection, id="other-bank", owner="other", accounts=("other-hash",))
        fixtures.prepare_selected(self.store, replace(self.config, connections=(self.connection, other)))
        self.assertEqual(self.scheduler.request_wait(self.connection), 0)
        self.assertEqual(self.scheduler.request_wait(other), 1)
        restrictive = replace(other, policy=AcquisitionPolicy(request_interval_seconds=60))
        self.assertEqual(self.scheduler.request_wait(restrictive), 60)
        self.assertEqual(self.rows(db.BudgetRequestLimit)[0].next_request_at, self.now + 1)
        self.now += 60
        self.assertEqual(self.scheduler.request_wait(restrictive), 0)
        self.assertEqual(self.scheduler.request_wait(self.connection), 60)
        self.limit("application", fixtures.APP, next_request_at=self.now + 61)
        with self.assertRaises(BudgetDeferredError):
            self.scheduler.request_wait(self.connection)

    def test_session_request_rechecks_that_some_component_remains_due(self):
        for primary in self.connection.accounts:
            for operation in ("transactions", "balances"):
                self.assertTrue(self.reserve(primary, operation))
        with self.assertRaises(BudgetDeferredError):
            self.scheduler.request_wait(self.connection)
        self.assertEqual(self.rows(db.BudgetRequestLimit), [])

    def test_all_shared_scopes_and_legacy_floor_are_consulted_without_creating_rows(self):
        with self.sessions() as session:
            row = session.get(db.BudgetConnection, "bank")
            self.assertEqual(shared_state(session, row), {"next_attempt": 0, "retry_at": None, "suspended": False})
        self.assertEqual(self.rows(db.BudgetRequestLimit), [])
        for scope, key in (("application", fixtures.APP), ("connection", "bank"),
                           ("provider", provider_key(self.connection.bank_name, "PL"))):
            self.limit(scope, key, next_attempt=self.now + 10)
            self.assertFalse(self.scheduler.can_start(self.connection))
            self.assertFalse(self.reserve())
            with self.assertRaises(BudgetDeferredError):
                self.scheduler.request_wait(self.connection)
            self.now += 10
        self.limit("provider", provider_key(self.connection.bank_name, "PL"), retry_at=self.now + 50, suspended=True)
        with self.sessions() as session:
            state = shared_state(session, session.get(db.BudgetConnection, "bank"))
        self.assertEqual(state, {"next_attempt": self.now + 50, "retry_at": self.now + 50, "suspended": True})
        self.now += 100
        self.assertFalse(self.scheduler.can_start(self.connection))
        with self.sessions.begin() as session:
            session.get(db.BudgetRequestLimit, ("provider", provider_key(self.connection.bank_name, "PL"))).suspended = False
            session.get(db.BudgetConnection, "bank").next_attempt = self.now + 1
        self.assertFalse(self.scheduler.can_start(self.connection))
        self.assertNotEqual(provider_key("ab", "c"), provider_key("a", "bc"))

    def test_endpoint_429_retains_siblings_and_blocks_same_round_even_without_hint(self):
        self.assertTrue(self.reserve())
        self.record(self.error())
        component = self.component()
        self.assertEqual(component.backoff_until, self.now + 21600)
        self.assertIsNone(component.retry_at)
        self.assertEqual(self.rows(db.BudgetConnection)[0].next_attempt, 0)
        self.assertFalse(self.reserve())
        with self.assertRaises(BudgetDeferredError):
            self.request()
        self.assertTrue(self.scheduler.can_start(self.connection))
        self.assertTrue(self.reserve(operation="balances"))
        self.assertEqual(self.request(operation="balances"), 0)

    def test_shared_and_explicit_application_rate_limits_are_persistent(self):
        self.record(self.error(status=500, code="unavailable"))
        self.assertEqual(self.rows(db.BudgetRequestLimit), [])
        self.record(self.error(RateScope.APPLICATION, retry_at=self.now + 70000))
        self.assertEqual(self.rows(db.BudgetConnection)[0].next_attempt, 0)
        self.assertFalse(self.scheduler.can_start(self.connection))
        app = self.rows(db.BudgetRequestLimit)[0]
        self.assertEqual((app.scope, app.next_attempt, app.retry_at), ("application", self.now + 70000, self.now + 70000))
        self.record(self.error("application", status=429, code="acquisition_timeout", retry_at=self.now + 80000))
        self.assertEqual(self.rows(db.BudgetConnection)[0].next_attempt, self.now + 80000)
        self.record(self.error(RateScope.ACCOUNT_ENDPOINT), primary=None, operation=None)
        self.assertEqual(self.rows(db.BudgetConnection)[0].next_attempt, self.now + 80000)
        self.record(self.error(RateScope.CONNECTION, status=None, code="rate_limit", retry_at=self.now + 10))
        self.assertEqual(self.rows(db.BudgetConnection)[0].next_attempt, self.now + 80000)

    def test_original_retry_deadline_suspensions_and_late_revocation_are_preserved(self):
        self.record(self.error(retry_at=self.now + 604801))
        self.assertTrue(self.component().suspended)
        self.record(self.error(retry_at=self.now + 1))
        self.assertEqual(self.component().retry_at, self.now + 604801)
        self.assertTrue(self.component().suspended)
        self.assertFalse(self.reserve())
        with self.sessions.begin() as session:
            session.get(db.User, "owner").disabled = True
            session.get(db.BudgetAccount, self.component().account_id).enabled = False
        self.record(self.error(retry_at=self.expiry), expiry=self.expiry)
        self.assertEqual(self.component().retry_at, self.expiry)
        for invalid in (math.nan, -1, "SECRET", 10 ** 1000):
            self.record(self.error(operation="unused", retry_at=invalid), operation="balances")
        self.assertTrue(self.component(operation="balances").suspended)
        self.record(self.error(retry_suspended=True), operation="balances")
        self.assertTrue(self.component(operation="balances").suspended)

    def test_missing_component_can_be_reserved_without_restoring_erased_quota(self):
        component = self.component()
        with self.sessions.begin() as session:
            session.delete(session.get(db.BudgetComponent, (component.account_id, component.operation)))
        self.assertTrue(self.reserve())
        self.assertEqual(len(self.rows(db.BudgetAcquisition)), 1)
        self.assertIsNotNone(self.component().lease_until)

    def test_fractional_deadlines_round_forward_and_invalid_clocks_are_safe(self):
        self.now = .3
        self.assertTrue(self.reserve())
        self.assertGreaterEqual(Fraction(self.component().lease_until), Fraction(self.now) + 900)
        for clock in (None, True, "SECRET", -1, math.nan, math.inf, 10 ** 1000):
            self.now = clock
            with self.subTest(clock=repr(clock)), self.assertRaisesRegex(BudgetStoreError, "^Invalid acquisition clock$"):
                self.scheduler.can_start(self.connection)
        for clock in (sys.float_info.max, int(sys.float_info.max) + 2 ** 970 - 1):
            self.now = clock
            with self.assertRaisesRegex(BudgetStoreError, "^Invalid acquisition clock$"):
                self.scheduler.request_wait(self.connection)

    def test_migration_seeds_upper_bounds_without_rewriting_prior_state(self):
        db.reset_engine_cache()
        engine = db.make_engine(self.path)
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            command.downgrade(db._alembic_config(connection), "0009")
            connection.exec_driver_sql("UPDATE budget_connections SET last_attempt=950,next_attempt=90000,last_error='network'")
            connection.exec_driver_sql("UPDATE budget_accounts SET last_attempt=900,observed_at=800")
            connection.exec_driver_sql("UPDATE budget_components SET last_attempt=1000,last_success=800,failures=2,last_error='network' WHERE operation='transactions'")
            prior = {table: [tuple(row) for row in connection.exec_driver_sql(f"SELECT * FROM {table}")]
                     for table in ("budget_accounts", "budget_connections", "budget_account_audit")}
            old_components = [tuple(row) for row in connection.exec_driver_sql("SELECT * FROM budget_components")]
            for target in ("0010", "0009", "0010"):
                if target == "0009":
                    command.downgrade(db._alembic_config(connection), target)
                else:
                    command.upgrade(db._alembic_config(connection), target)
                    debits = connection.exec_driver_sql("SELECT operation,started_at,origin FROM budget_acquisitions ORDER BY operation").all()
                    self.assertEqual([tuple(row) for row in debits],
                                     [("balances", 950.0, "legacy_upper_bound")] * 2
                                     + [("transactions", 1000.0, "legacy_upper_bound")] * 2)
                    self.assertEqual(connection.exec_driver_sql("SELECT COUNT(*) FROM budget_request_limits").scalar_one(), 0)
                    self.assertTrue(all(tuple(row) == (None, 0, 0) for row in connection.exec_driver_sql(
                        "SELECT retry_at,suspended,backoff_until FROM budget_components")))
                for table, expected in prior.items():
                    self.assertEqual([tuple(row) for row in connection.exec_driver_sql(f"SELECT * FROM {table}")], expected)
                current = connection.exec_driver_sql("SELECT account_id,operation,last_attempt,last_success,last_error,failures,next_attempt,attempt_id,lease_until FROM budget_components").all()
                self.assertEqual([tuple(row) for row in current], old_components)
                self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_key_check").all(), [])


if __name__ == "__main__":
    unittest.main()
