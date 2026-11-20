"""Deterministic acquisition quotas and paced HTTP without bank access or waits."""
from dataclasses import replace
from datetime import datetime, timezone
from email.message import Message
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError

from cryptography.hazmat.primitives.asymmetric import rsa

from sqlalchemy import event, select

from hestia import db
from hestia.budget import runtime
from hestia.budget.client import BudgetAPIError, EnableBankingClient
from hestia.budget.config import AcquisitionPolicy
from hestia.budget.rate_limit import RateScope
from hestia.budget.store import BudgetStore
from tests.test_budget_store import APP, configuration, prepare_selected, snapshot, transaction
from tests.test_budget_runtime import ACCOUNT_UID, FAKE_KEY, SyntheticBank
from tests.test_budget_client import Opener, Response, pem


class Clock:
    def __init__(self):
        self.wall = datetime(2026, 11, 20, 12, tzinfo=timezone.utc).timestamp()
        self.tick = 1000.0
        self.sleeps = []
        self.on_sleep = None

    def time(self):
        return self.wall

    def monotonic(self):
        return self.tick

    def advance(self, seconds):
        self.wall += seconds
        self.tick += seconds

    def sleep(self, seconds):
        if self.on_sleep is not None:
            self.on_sleep(seconds)
        self.sleeps.append(seconds)
        self.advance(seconds)


def raw_transaction(ref):
    row = transaction(ref)
    row["transaction_amount"] = {"amount": row.pop("amount"), "currency": row.pop("currency")}
    return row


class PagedBank(SyntheticBank):
    """One selected synthetic account, configurable pages and endpoint failures."""

    def __init__(self, config, clock, *, pages=3, failure=None, balance_failure=None):
        super().__init__(config)
        self.clock, self.pages = clock, pages
        self.failure, self.balance_failure = failure, balance_failure
        self.times = []
        self.after_get = None

    def get(self, path, query=None):
        self.times.append(self.clock.time())
        if path.endswith("/transactions"):
            self.calls.append((path, query))
            page = int((query or {}).get("continuation_key", "page-1").removeprefix("page-"))
            if self.failure is not None:
                self.failure(page)
            result = {"transactions": [raw_transaction("page-reference-" + str(page))]}
            if page < self.pages:
                result["continuation_key"] = "page-" + str(page + 1)
        elif path.endswith("/balances") and self.balance_failure is not None:
            self.calls.append((path, query))
            raise self.balance_failure
        else:
            result = super().get(path, query)
        if self.after_get is not None:
            self.after_get(path, query)
        return result


class BudgetQuotaRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "quota.db"
        self.engine, self.sessions = db.init_db(self.path)
        with self.sessions.begin() as session:
            session.add_all([db.User(username="owner", password_hash="unused", role="admin"),
                             db.User(username="other", password_hash="unused", role="admin")])
        self.clock = Clock()
        self.config = configuration()
        self.store = BudgetStore(self.sessions, clock=self.clock.time)
        prepare_selected(self.store, self.config)

    def tearDown(self):
        db.reset_engine_cache()
        self.directory.cleanup()

    def rows(self, model):
        with self.sessions() as session:
            return list(session.scalars(select(model)))

    def components(self):
        return {row.operation: row for row in self.rows(db.BudgetComponent)}

    def acquire(self, bank, *, config=None, factory=None):
        with mock.patch.object(runtime, "credentials", return_value=(FAKE_KEY, APP)):
            return runtime.run_once(self.config if config is None else config, path=self.path,
                                    clock=self.clock.time, monotonic=self.clock.monotonic,
                                    sleeper=self.clock.sleep,
                                    client_factory=mock.Mock(return_value=bank) if factory is None else factory)

    def test_fourteen_pages_debit_once_and_every_get_is_paced_outside_sqlite_transactions(self):
        bank = PagedBank(self.config, self.clock, pages=14)
        active = set()
        def begin(connection):
            active.add(connection)
        def end(connection):
            active.discard(connection)
        event.listen(self.engine, "begin", begin)
        event.listen(self.engine, "commit", end)
        event.listen(self.engine, "rollback", end)
        self.clock.on_sleep = lambda seconds: self.assertFalse(active, "Transport sleep held a DB transaction")
        try:
            result = self.acquire(bank)
        finally:
            event.remove(self.engine, "begin", begin)
            event.remove(self.engine, "commit", end)
            event.remove(self.engine, "rollback", end)
        self.assertEqual(result["results"], [{"connection": "bank", "state": "updated"}])
        self.assertEqual(len(bank.calls), 16)
        self.assertEqual([path.rsplit("/", 1)[-1] for path, _ in bank.calls[1:]],
                         ["transactions"] * 14 + ["balances"])
        self.assertTrue(all(right - left >= 1 for left, right in zip(bank.times, bank.times[1:])))
        self.assertEqual(self.clock.sleeps, [1] * 15)
        debits = self.rows(db.BudgetAcquisition)
        self.assertEqual(sorted(row.operation for row in debits), ["balances", "transactions"])
        self.assertEqual(len({row.attempt_id for row in debits}), 1)
        self.assertEqual({row.entry_reference for row in self.rows(db.BudgetTransaction)},
                         {"page-reference-" + str(page) for page in range(1, 15)})

    def test_immediate_repeat_is_deferred_without_reading_secrets_or_sending_session_get(self):
        bank = PagedBank(self.config, self.clock, pages=1)
        self.acquire(bank)
        calls, debits = list(bank.calls), len(self.rows(db.BudgetAcquisition))
        with mock.patch.object(runtime, "credentials") as credentials:
            result = runtime.run_once(self.config, path=self.path, clock=self.clock.time,
                                      monotonic=self.clock.monotonic, sleeper=self.clock.sleep,
                                      client_factory=mock.Mock(side_effect=AssertionError("No HTTP client expected")))
        credentials.assert_not_called()
        self.assertEqual(result["results"], [{"connection": "bank", "state": "deferred"}])
        self.assertEqual(bank.calls, calls)
        self.assertEqual(len(self.rows(db.BudgetAcquisition)), debits)

    def test_operator_transport_policy_can_require_slower_requests_without_extra_debits(self):
        connection = replace(self.config.connections[0], policy=AcquisitionPolicy(request_interval_seconds=3))
        self.config = replace(self.config, connections=(connection,))
        bank = PagedBank(self.config, self.clock, pages=2)
        result = self.acquire(bank)
        self.assertEqual(result["results"], [{"connection": "bank", "state": "updated"}])
        self.assertEqual(self.clock.sleeps, [3, 3, 3])
        self.assertTrue(all(right - left >= 3 for left, right in zip(bank.times, bank.times[1:])))
        self.assertEqual(len(self.rows(db.BudgetAcquisition)), 2)

    def test_session_pacing_deferral_does_not_record_a_failure_or_add_connection_backoff(self):
        future = self.clock.time() + 3600
        with self.sessions.begin() as session:
            session.add(db.BudgetRequestLimit(scope="application", key=APP, next_request_at=future))
            connection = session.get(db.BudgetConnection, "bank")
            connection.failures, connection.last_error = 2, "network"
            connection.next_attempt = 0
            connection.last_attempt = self.clock.time() - 86400
        bank = PagedBank(self.config, self.clock)
        result = self.acquire(bank)
        self.assertEqual(result["results"], [{"connection": "bank", "state": "deferred"}])
        connection = self.rows(db.BudgetConnection)[0]
        self.assertEqual(connection.failures, 2)
        self.assertEqual(connection.last_error, "network")
        self.assertEqual(connection.next_attempt, 0)
        self.assertEqual(connection.last_attempt, self.clock.time() - 86400)
        self.assertEqual(bank.calls, [])
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(self.rows(db.BudgetAcquisition), [])
        self.assertTrue(all(row.last_attempt is None for row in self.components().values()))
        limit = self.rows(db.BudgetRequestLimit)[0]
        self.assertEqual(limit.next_request_at, future)

    def test_late_page_error_keeps_ledger_and_quarantine_while_balances_complete(self):
        self.store.commit_snapshot("bank", snapshot(transaction("old-ledger"), transaction(None),
                                                     valid_until=self.clock.time() + 31536000))
        old_ledger = [(row.entry_reference, row.payload_json) for row in self.rows(db.BudgetTransaction)]
        old_quarantine = [row.payload_json for row in self.rows(db.BudgetUnresolved)]
        previous_success = self.components()["transactions"].last_success
        self.clock.advance(86400)
        def fail(page):
            if page == 2:
                raise BudgetAPIError(503, "unavailable", True)
        bank = PagedBank(self.config, self.clock, failure=fail)
        result = self.acquire(bank)
        self.assertEqual(result["results"], [{"connection": "bank", "state": "partial"}])
        self.assertEqual([(row.entry_reference, row.payload_json) for row in self.rows(db.BudgetTransaction)], old_ledger)
        self.assertEqual([row.payload_json for row in self.rows(db.BudgetUnresolved)], old_quarantine)
        self.assertEqual(self.components()["transactions"].last_success, previous_success)
        self.assertEqual(self.components()["transactions"].last_error, "unavailable")
        self.assertGreater(self.components()["balances"].last_success, previous_success)
        self.assertEqual(sorted(row.operation for row in self.rows(db.BudgetAcquisition)), ["balances", "transactions"])

    def test_known_endpoint_429_preserves_long_retry_deadline_and_allows_healthy_balances(self):
        retry = self.clock.time() + 3 * 86400
        def fail(page):
            raise BudgetAPIError(429, "rate_limit", True, provider_code="ASPSP_RATE_LIMIT_EXCEEDED",
                                 retry_at=retry, rate_scope=RateScope.ACCOUNT_ENDPOINT)
        bank = PagedBank(self.config, self.clock, failure=fail)
        result = self.acquire(bank)
        self.assertEqual(result["results"], [{"connection": "bank", "state": "partial"}])
        self.assertEqual([path.rsplit("/", 1)[-1] for path, _ in bank.calls[1:]], ["transactions", "balances"])
        transactions, balances = self.components()["transactions"], self.components()["balances"]
        self.assertEqual(transactions.retry_at, retry)
        self.assertGreaterEqual(transactions.next_attempt, retry)
        self.assertFalse(transactions.suspended)
        self.assertEqual(transactions.last_error, "rate_limit")
        self.assertIsNotNone(balances.last_success)
        self.assertFalse(balances.suspended)
        visible = result["status"][0]["accounts"][0]["components"]
        self.assertEqual(visible["transactions"]["retry_at"], retry)
        self.assertGreaterEqual(visible["transactions"]["next_attempt"], retry)
        self.assertFalse(visible["transactions"]["suspended"])
        self.assertIsNone(visible["balances"]["retry_at"])
        self.assertFalse(visible["balances"]["suspended"])
        self.assertIsNone(result["status"][0]["request_limits"]["retry_at"])

    def test_unknown_endpoint_429_blocks_later_http_at_connection_scope(self):
        retry = self.clock.time() + 2 * 86400
        def fail(page):
            raise BudgetAPIError(429, "rate_limit", True, retry_at=retry)
        bank = PagedBank(self.config, self.clock, failure=fail)
        result = self.acquire(bank)
        self.assertEqual(result["results"], [{"connection": "bank", "state": "failed"}])
        self.assertEqual([path.rsplit("/", 1)[-1] for path, _ in bank.calls[1:]], ["transactions"])
        limit = next(row for row in self.rows(db.BudgetRequestLimit) if row.scope == "connection" and row.key == "bank")
        self.assertEqual(limit.retry_at, retry)
        self.assertGreaterEqual(limit.next_attempt, retry)
        self.assertIsNone(self.components()["balances"].last_attempt)
        self.assertEqual(len(self.rows(db.BudgetAcquisition)), 1)

    def test_session_429_is_persisted_without_debiting_an_endpoint_or_retrying_other_gets(self):
        retry = self.clock.time() + 86400
        bank = PagedBank(self.config, self.clock)
        def session_limit(path, query=None):
            bank.calls.append((path, query))
            raise BudgetAPIError(429, "rate_limit", True, provider_code="ASPSP_RATE_LIMIT_EXCEEDED", retry_at=retry)
        bank.get = session_limit
        result = self.acquire(bank)
        self.assertEqual(result["results"], [{"connection": "bank", "state": "failed"}])
        self.assertEqual([path for path, _ in bank.calls], [f"/sessions/{APP}"])
        self.assertEqual(self.rows(db.BudgetAcquisition), [])
        self.assertTrue(all(row.last_attempt is None for row in self.components().values()))
        limit = next(row for row in self.rows(db.BudgetRequestLimit) if row.scope == "connection" and row.key == "bank")
        self.assertEqual(limit.retry_at, retry)
        self.assertGreaterEqual(limit.next_attempt, retry)

    def test_extreme_retry_is_suspended_with_its_full_deadline(self):
        retry = 1e128
        def fail(page):
            raise BudgetAPIError(429, "rate_limit", True, provider_code="ASPSP_RATE_LIMIT_EXCEEDED",
                                 retry_at=retry, retry_suspended=True, rate_scope=RateScope.ACCOUNT_ENDPOINT)
        result = self.acquire(PagedBank(self.config, self.clock, failure=fail))
        self.assertEqual(result["results"][0]["state"], "partial")
        part = self.components()["transactions"]
        self.assertTrue(part.suspended)
        self.assertEqual(part.retry_at, retry)
        self.assertEqual(part.last_error, "rate_limit")
        visible = result["status"][0]["accounts"][0]["components"]["transactions"]
        self.assertEqual(visible["state"], "blocked")
        self.assertEqual(visible["retry_at"], retry)
        self.assertTrue(visible["suspended"])
        self.assertGreaterEqual(visible["next_attempt"], retry)

    def test_retry_beyond_validated_session_suspends_without_changing_deadline(self):
        retry = self.clock.time() + 7200
        expiry = self.clock.time() + 3600
        def fail(page):
            raise BudgetAPIError(429, "rate_limit", True, provider_code="ASPSP_RATE_LIMIT_EXCEEDED",
                                 retry_at=retry, rate_scope=RateScope.ACCOUNT_ENDPOINT)
        bank = PagedBank(self.config, self.clock, failure=fail)
        get = bank.get
        def short_session(path, query=None):
            result = get(path, query)
            if path.startswith("/sessions/"):
                result["access"]["valid_until"] = datetime.fromtimestamp(expiry, timezone.utc).isoformat()
            return result
        bank.get = short_session
        result = self.acquire(bank)
        self.assertEqual(result["results"][0]["state"], "partial")
        self.assertTrue(self.components()["transactions"].suspended)
        self.assertEqual(self.components()["transactions"].retry_at, retry)

    def _change_access_after_first_page(self, *, disable_owner):
        bank = PagedBank(self.config, self.clock)
        def change(path, query):
            if path.endswith("/transactions"):
                with self.sessions.begin() as session:
                    if disable_owner:
                        session.get(db.User, "owner").disabled = True
                    else:
                        session.scalar(select(db.BudgetAccount)).enabled = False
        bank.after_get = change
        result = self.acquire(bank)
        self.assertEqual(result["results"], [{"connection": "bank", "state": "failed"}])
        self.assertEqual([path.rsplit("/", 1)[-1] for path, _ in bank.calls[1:]], ["transactions"])
        self.assertEqual(self.rows(db.BudgetTransaction), [])
        self.assertEqual(len(self.rows(db.BudgetAcquisition)), 1)
        self.assertIsNone(self.components()["balances"].last_attempt)

    def test_disabled_owner_between_pages_stops_http_and_discards_staged_data(self):
        self._change_access_after_first_page(disable_owner=True)

    def test_deselection_between_pages_stops_http_and_discards_staged_data(self):
        self._change_access_after_first_page(disable_owner=False)

    def test_replaced_live_lease_between_pages_prevents_more_page_http(self):
        bank = PagedBank(self.config, self.clock)
        def replace_marker(path, query):
            if path.endswith("/transactions"):
                with self.sessions.begin() as session:
                    row = session.scalar(select(db.BudgetComponent).where(db.BudgetComponent.operation == "transactions"))
                    row.attempt_id, row.lease_until = "foreign-current-owner", self.clock.time() + 900
        bank.after_get = replace_marker
        result = self.acquire(bank)
        self.assertEqual(result["results"][0]["state"], "failed")
        self.assertEqual(sum(path.endswith("/transactions") for path, _ in bank.calls), 1)
        self.assertEqual(self.rows(db.BudgetTransaction), [])
        self.assertEqual(self.components()["transactions"].attempt_id, "foreign-current-owner")

    def test_application_cooldown_discovered_in_first_connection_blocks_the_next_connection(self):
        second = replace(self.config.connections[0], id="other-bank", owner="other", accounts=("other-hash",))
        self.config = replace(self.config, connections=(*self.config.connections, second))
        prepare_selected(self.store, self.config)
        bank = PagedBank(self.config, self.clock)
        def apply_shared_cooldown(path, query):
            if path.startswith("/sessions/"):
                with self.sessions.begin() as session:
                    row = session.get(db.BudgetRequestLimit, ("application", APP))
                    self.assertIsNotNone(row)
                    row.next_attempt = row.retry_at = self.clock.time() + 86400
        bank.after_get = apply_shared_cooldown
        factory = mock.Mock(return_value=bank)
        with mock.patch.object(runtime, "credentials", return_value=(FAKE_KEY, APP)) as credentials:
            result = runtime.run_once(self.config, path=self.path, clock=self.clock.time,
                                      monotonic=self.clock.monotonic, sleeper=self.clock.sleep, client_factory=factory)
        self.assertEqual(result["results"], [{"connection": "bank", "state": "deferred"},
                                            {"connection": "other-bank", "state": "deferred"}])
        self.assertEqual([path for path, _ in bank.calls], [f"/sessions/{APP}"])
        credentials.assert_called_once()
        factory.assert_called_once()
        self.assertEqual(self.rows(db.BudgetAcquisition), [])
        for connection in result["status"]:
            shared = connection["request_limits"]
            self.assertEqual(shared["retry_at"], self.clock.time() + 86400)
            self.assertEqual(shared["next_attempt"], self.clock.time() + 86400)
            self.assertFalse(shared["suspended"])
            for component in connection["accounts"][0]["components"].values():
                self.assertEqual(component["next_attempt"], shared["next_attempt"])
                self.assertEqual(component["retry_at"], shared["retry_at"])
                self.assertEqual(component["state"], "deferred")

    def test_crash_keeps_its_acquisition_debit_and_restart_only_fetches_the_due_sibling(self):
        def interrupt(page):
            raise SystemExit("synthetic interrupted page")
        with self.assertRaisesRegex(SystemExit, "synthetic interrupted page"):
            self.acquire(PagedBank(self.config, self.clock, failure=interrupt))
        self.assertEqual([row.operation for row in self.rows(db.BudgetAcquisition)], ["transactions"])
        old_marker = self.components()["transactions"].attempt_id
        self.assertIsNotNone(old_marker)
        db.reset_engine_cache()
        self.engine, self.sessions = db.init_db(self.path)
        self.store = BudgetStore(self.sessions, clock=self.clock.time)
        self.clock.advance(901)
        recovery = PagedBank(self.config, self.clock, pages=1)
        result = self.acquire(recovery)
        self.assertEqual(result["results"], [{"connection": "bank", "state": "partial"}])
        self.assertEqual([path.rsplit("/", 1)[-1] for path, _ in recovery.calls[1:]], ["balances"])
        self.assertEqual(self.components()["transactions"].attempt_id, old_marker)
        self.assertIsNone(self.components()["transactions"].last_success)
        self.assertIsNotNone(self.components()["balances"].last_success)
        self.assertEqual(sorted(row.operation for row in self.rows(db.BudgetAcquisition)), ["balances", "transactions"])
        self.assertEqual(self.rows(db.BudgetTransaction), [])
        self.clock.advance(43200)
        result = self.acquire(PagedBank(self.config, self.clock, pages=1))
        self.assertEqual(result["results"], [{"connection": "bank", "state": "updated"}])
        self.assertEqual(len(self.rows(db.BudgetAcquisition)), 4)
        self.assertTrue(all(row.attempt_id is None for row in self.components().values()))

    def test_restricted_one_per_day_policy_defers_without_secrets_until_rolling_window_allows_it(self):
        connection = replace(self.config.connections[0], policy=AcquisitionPolicy(max_acquisitions_24h=1))
        self.config = replace(self.config, connections=(connection,))
        self.acquire(PagedBank(self.config, self.clock, pages=1))
        self.clock.advance(43200)
        with mock.patch.object(runtime, "credentials") as credentials:
            result = runtime.run_once(self.config, path=self.path, clock=self.clock.time,
                                      monotonic=self.clock.monotonic, sleeper=self.clock.sleep)
        credentials.assert_not_called()
        self.assertEqual(result["results"], [{"connection": "bank", "state": "deferred"}])
        self.assertEqual(len(self.rows(db.BudgetAcquisition)), 2)
        self.clock.advance(43200)
        result = self.acquire(PagedBank(self.config, self.clock, pages=1))
        self.assertEqual(result["results"], [{"connection": "bank", "state": "updated"}])
        self.assertEqual(len(self.rows(db.BudgetAcquisition)), 4)

    def test_real_http_client_metadata_and_round_deadline_reach_durable_endpoint_limits(self):
        session = SyntheticBank(self.config).get(f"/sessions/{APP}")
        headers = Message()
        headers["Retry-After"] = "259200"
        failure = HTTPError(f"https://api.enablebanking.com/accounts/{ACCOUNT_UID}/transactions", 429,
                            "Synthetic rate limit", headers, BytesIO(b'{"error":"ASPSP_RATE_LIMIT_EXCEEDED"}'))
        self.addCleanup(failure.close)
        opener = Opener(Response(json.dumps(session).encode()), failure, Response(b'{"balances":[]}'))
        key = pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))
        client = EnableBankingClient(APP, key, opener=opener, clock=self.clock.time, monotonic=self.clock.monotonic)
        deadline = self.clock.monotonic() + 900
        expected_retry = self.clock.time() + 1 + 259200
        with mock.patch.object(client, "get", wraps=client.get) as get:
            result = self.acquire(None, factory=mock.Mock(return_value=client))
        self.assertEqual(result["results"], [{"connection": "bank", "state": "partial"}])
        self.assertEqual(get.call_count, 3)
        self.assertTrue(all(call.kwargs["deadline"] == deadline for call in get.call_args_list))
        self.assertEqual(self.components()["transactions"].retry_at, expected_retry)
        self.assertGreaterEqual(self.components()["transactions"].next_attempt, expected_retry)
        self.assertIsNotNone(self.components()["balances"].last_success)
        self.assertEqual(len(self.rows(db.BudgetAcquisition)), 2)

    def test_response_after_round_deadline_is_discarded_without_charging_unvisited_balances(self):
        bank = PagedBank(self.config, self.clock)
        def slow_response(path, query):
            if path.endswith("/transactions"):
                self.clock.advance(900)
        bank.after_get = slow_response
        result = self.acquire(bank)
        self.assertEqual(result["results"], [{"connection": "bank", "state": "failed"}])
        self.assertEqual(self.rows(db.BudgetTransaction), [])
        self.assertEqual(self.components()["transactions"].last_error, "acquisition_timeout")
        self.assertIsNone(self.components()["balances"].last_attempt)
        self.assertEqual(len(self.rows(db.BudgetAcquisition)), 1)


class AcquisitionRoundDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.scheduler = mock.Mock()
        self.scheduler.request_wait.return_value = 0
        self.client = mock.Mock()
        self.client.get.return_value = {"synthetic": "response"}
        self.connection = configuration().connections[0]

    def wrapper(self, *, monotonic=None):
        return runtime.AcquisitionClient(self.client, self.scheduler, self.connection, "synthetic-round",
                                         monotonic=self.clock.monotonic if monotonic is None else monotonic,
                                         sleeper=self.clock.sleep)

    def test_invalid_monotonic_clock_fails_safely_before_network_or_reservation(self):
        for invalid in (None, True, "PRIVATE-CLOCK", -1, float("nan"), float("inf"), 10 ** 1000):
            with self.subTest(value=repr(invalid)), self.assertRaises(BudgetAPIError) as error:
                self.wrapper(monotonic=lambda: invalid)
            self.assertEqual(error.exception.code, "invalid_configuration")
            self.assertNotIn("PRIVATE", str(error.exception))
        for error in (ValueError, TypeError, OverflowError):
            with self.subTest(error=error), self.assertRaises(BudgetAPIError):
                self.wrapper(monotonic=mock.Mock(side_effect=error))
        self.client.get.assert_not_called()
        self.scheduler.request_wait.assert_not_called()
        self.scheduler.reserve_component.assert_not_called()

    def test_expired_round_never_reserves_or_sends_another_request(self):
        wrapper = self.wrapper()
        self.clock.advance(900)
        self.assertFalse(wrapper.begin_component("synthetic-hash", "transactions"))
        with self.assertRaises(BudgetAPIError) as error:
            wrapper.get(f"/sessions/{APP}")
        self.assertEqual(error.exception.code, "acquisition_timeout")
        self.scheduler.reserve_component.assert_not_called()
        self.scheduler.request_wait.assert_not_called()
        self.client.get.assert_not_called()

    def test_wait_beyond_round_budget_defers_without_sleeping_or_sending_http(self):
        from hestia.budget.scheduler import BudgetDeferredError
        wrapper = self.wrapper()
        self.scheduler.request_wait.return_value = 900
        with self.assertRaises(BudgetDeferredError):
            wrapper.get(f"/sessions/{APP}")
        self.assertEqual(self.clock.sleeps, [])
        self.client.get.assert_not_called()
