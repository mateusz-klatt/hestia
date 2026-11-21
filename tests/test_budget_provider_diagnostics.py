"""Durable HTTP diagnostics: a status plus a documented Enable Banking identifier, never response text."""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import json
import sqlite3
import unittest
from urllib.error import HTTPError

from alembic import command
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from hestia import db
from hestia.budget import client as module
from hestia.budget.client import (BudgetAPIError, EnableBankingClient, PROVIDER_ERRORS, ProviderDiagnostic,
                                  UNLISTED_PROVIDER_ERROR, safe_http_status, safe_provider_error)
from hestia.budget.normalize import BudgetDataError
from hestia.budget.rate_limit import RateScope
from hestia.budget.store import set_diagnostic
from tests.test_budget_client import APP_ID, PRIVATE, SESSION, TRANSACTIONS, Opener, Response, pem
from tests.test_budget_components import ComponentCase
from tests.test_budget_runtime import ACCOUNT_UID, SyntheticBank
from tests.test_budget_store import APP


DETAIL = "PRIVATE-DETAIL-date_from-2025-01-01-MUST-NOT-PERSIST"


def error_body(identifier, *, message=PRIVATE, detail=DETAIL):
    return json.dumps({"message": message, "code": 400, "error": identifier, "detail": detail}).encode()


def http_error(status, body):
    return HTTPError("https://outside.invalid/" + PRIVATE, status, PRIVATE, {}, Response(body, status))


class ClientDiagnosticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key_pem = pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))

    def client(self, *responses, monotonic=None):
        opener = Opener(*responses)
        return EnableBankingClient(APP_ID, self.key_pem, opener=opener, clock=lambda: 1_700_000_000,
                                   monotonic=monotonic or (lambda: 100.0)), opener

    def fail(self, reply, *, path=TRANSACTIONS, monotonic=None, deadline=None):
        client, opener = self.client(reply, monotonic=monotonic)
        with self.assertRaises(BudgetAPIError) as caught:
            client.get(path, deadline=deadline)
        self.assertEqual(len(opener.calls), 1)
        error = caught.exception
        for text in (PRIVATE, DETAIL):
            self.assertNotIn(text, str(error))
            self.assertNotIn(text, repr(error.__dict__))
        return error

    def test_every_documented_identifier_is_kept_verbatim_and_never_its_text(self):
        self.assertEqual(len(PROVIDER_ERRORS), 43)
        self.assertLessEqual(BudgetAPIError.PROVIDER_CODES, PROVIDER_ERRORS)
        for identifier in sorted(PROVIDER_ERRORS):
            for raised in (True, False):
                body = error_body(identifier)
                reply = http_error(400, body) if raised else Response(body, 400)
                with self.subTest(identifier=identifier, raised=raised):
                    error = self.fail(reply)
                    self.assertEqual((error.status, error.code, error.retryable, error.provider_error),
                                     (400, "provider_error", False, identifier))
                    self.assertEqual(ProviderDiagnostic.of(error), ProviderDiagnostic(400, identifier))

    def test_classification_and_retryability_are_unchanged_by_the_diagnostic(self):
        cases = [(401, "authorization", False), (403, "authorization", False), (404, "provider_error", False),
                 (408, "unavailable", True), (422, "provider_error", False), (500, "unavailable", True),
                 (503, "unavailable", True)]
        for status, code, retryable in cases:
            with self.subTest(status=status):
                error = self.fail(http_error(status, error_body("ASPSP_ERROR")))
                self.assertEqual((error.status, error.code, error.retryable, error.provider_error, error.provider_code),
                                 (status, code, retryable, "ASPSP_ERROR", None))

    def test_unknown_identifiers_collapse_to_unlisted_and_malformed_bodies_leave_nothing(self):
        self.assertEqual(self.fail(http_error(400, error_body(PRIVATE))).provider_error, UNLISTED_PROVIDER_ERROR)
        self.assertEqual(self.fail(http_error(400, error_body("wrong_transactions_period"))).provider_error,
                         UNLISTED_PROVIDER_ERROR)
        malformed = [b"", b"not json " + PRIVATE.encode(), b"[]", b'{"error": 7}', b'{"error": null}',
                     b'{"message": "no identifier"}', b'{"error": "ASPSP_ERROR", "error": "ASPSP_TIMEOUT"}',
                     b'{"error": "ASPSP_ERROR", "x": NaN}', b"\xff\xfe",
                     error_body("ASPSP_ERROR", detail="x" * module.MAX_ERROR_RESPONSE)]
        for body in malformed:
            with self.subTest(body=body[:40]):
                error = self.fail(http_error(400, body))
                self.assertEqual((error.code, error.provider_error), ("provider_error", None))

    def test_rate_limit_scope_still_depends_only_on_the_rate_limit_identifier(self):
        limited = self.fail(http_error(429, error_body("ASPSP_RATE_LIMIT_EXCEEDED")))
        self.assertEqual((limited.code, limited.provider_code, limited.provider_error, limited.rate_scope),
                         ("rate_limit", "ASPSP_RATE_LIMIT_EXCEEDED", "ASPSP_RATE_LIMIT_EXCEEDED",
                          RateScope.ACCOUNT_ENDPOINT))
        other = self.fail(http_error(429, error_body("ASPSP_ERROR")))
        self.assertEqual((other.code, other.provider_code, other.provider_error, other.rate_scope),
                         ("rate_limit", None, "ASPSP_ERROR", RateScope.CONNECTION))
        session = self.fail(http_error(429, error_body("ASPSP_RATE_LIMIT_EXCEEDED")), path=SESSION)
        self.assertEqual(session.rate_scope, RateScope.CONNECTION)

    def test_a_failed_or_slow_diagnostic_read_keeps_the_original_classification(self):
        class Broken(Response):
            def read(self, size=-1):
                raise OSError(PRIVATE)
        error = self.fail(Broken(error_body("ASPSP_ERROR"), 400))
        self.assertEqual((error.status, error.code, error.provider_error), (400, "provider_error", None))

        # Each read advances the clock past the 5 s diagnostic bound, far inside the request deadline.
        ticks = iter(range(0, 10_000, 4))

        class Drip(Response):
            def read(self, size=-1):
                return super().read(1)
        drip = Drip(error_body("ASPSP_ERROR"), 503)
        error = self.fail(drip, monotonic=lambda: float(next(ticks)), deadline=5_000.0)
        self.assertEqual((error.status, error.code, error.retryable, error.provider_error),
                         (503, "unavailable", True, None))
        self.assertLess(len(drip.read_sizes), 5)

    def test_unusable_clocks_abandon_only_the_diagnostic(self):
        def broken():
            raise TypeError(PRIVATE)
        for monotonic in (broken, lambda: float("nan")):
            with self.subTest(monotonic=monotonic):
                error = self.fail(http_error(400, error_body("WRONG_TRANSACTIONS_PERIOD")), monotonic=monotonic)
                self.assertEqual((error.status, error.code, error.provider_error), (400, "provider_error", None))

    def test_a_tight_request_deadline_still_bounds_the_diagnostic_read(self):
        client, _ = self.client(Response(), monotonic=lambda: 100.0)
        self.assertEqual(client._diagnostic_deadline(102.0), 102.0)
        self.assertEqual(client._diagnostic_deadline(500.0), 100.0 + module.DIAGNOSTIC_SECONDS)
        self.assertEqual(client._diagnostic_deadline(None), 100.0 + module.DIAGNOSTIC_SECONDS)

    def test_sanitizers_and_diagnostic_factory(self):
        self.assertEqual([safe_http_status(value) for value in (200, 400, 599, 99, 600, True, 400.0, "400", None)],
                         [200, 400, 599, None, None, None, None, None, None])
        self.assertEqual(safe_provider_error("ASPSP_ERROR"), "ASPSP_ERROR")
        self.assertEqual(safe_provider_error(UNLISTED_PROVIDER_ERROR), UNLISTED_PROVIDER_ERROR)
        self.assertIsNone(safe_provider_error(PRIVATE))
        self.assertEqual(safe_provider_error(PRIVATE, any_string=True), UNLISTED_PROVIDER_ERROR)
        self.assertIsNone(safe_provider_error(["ASPSP_ERROR"], any_string=True))
        self.assertIsNone(BudgetAPIError(400, provider_error=PRIVATE).provider_error)
        self.assertIsNone(ProviderDiagnostic.of(BudgetAPIError(code="network", retryable=True)))
        self.assertIsNone(ProviderDiagnostic.of(BudgetDataError("invalid_data")))
        self.assertIsNone(ProviderDiagnostic.of(ValueError(PRIVATE)))
        self.assertEqual(ProviderDiagnostic.of(BudgetAPIError(404)), ProviderDiagnostic(404, None))


class DiagnosticBank(SyntheticBank):
    """Synthetic bank whose chosen routes answer through the real client and its HTTP error parser."""

    def __init__(self, config, key_pem, *, transactions=None, session=None):
        super().__init__(config)
        self.key_pem, self.replies = key_pem, {"transactions": transactions, "session": session}

    def _real(self, reply, path, query):
        client = EnableBankingClient(APP_ID, self.key_pem, opener=Opener(reply), clock=lambda: 1_700_000_000,
                                     monotonic=lambda: 100.0)
        return client.get(path, query=query)

    def get(self, path, query=None):
        if self.replies["session"] is not None and path.startswith("/sessions/"):
            self.calls.append((path, query))
            return self._real(self.replies["session"], path, query)
        if self.replies["transactions"] is not None and path == f"/accounts/{ACCOUNT_UID}/transactions":
            self.calls.append((path, query))
            return self._real(self.replies["transactions"], path, query)
        return super().get(path, query)


class DurableDiagnosticTests(ComponentCase):
    @classmethod
    def setUpClass(cls):
        cls.key_pem = pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))

    def diagnostics(self, model, **where):
        with self.sessions() as session:
            return [(row.last_error, row.last_http_status, row.last_provider_error)
                    for row in session.scalars(select(model).filter_by(**where))]

    def component(self, operation):
        return self.diagnostics(db.BudgetComponent, account_id=self.account().id, operation=operation)[0]

    def connection(self):
        return self.diagnostics(db.BudgetConnection, id="bank")[0]

    def assert_no_response_text(self):
        db.reset_engine_cache()
        with closing(sqlite3.connect(self.path)) as connection:
            dump = "\n".join(connection.iterdump())
        for text in (PRIVATE, DETAIL, "outside.invalid"):
            self.assertNotIn(text, dump)
        _, self.sessions = db.init_db(self.path)

    def restart(self):
        db.reset_engine_cache()
        _, self.sessions = db.init_db(self.path)

    def test_component_http_failure_survives_restart_and_clears_after_success(self):
        bank = DiagnosticBank(self.config, self.key_pem,
                              transactions=http_error(400, error_body("WRONG_TRANSACTIONS_PERIOD")))
        self.acquire(bank)
        self.assertEqual(self.component("transactions"), ("provider_error", 400, "WRONG_TRANSACTIONS_PERIOD"))
        self.assertEqual(self.component("balances"), (None, None, None))
        # The connection summary is aggregate; the per-request detail lives on the component.
        self.assertEqual(self.connection(), ("partial_data", None, None))
        self.assert_no_response_text()
        self.restart()
        self.assertEqual(self.component("transactions"), ("provider_error", 400, "WRONG_TRANSACTIONS_PERIOD"))

        self.now += 2 * 86400
        self.acquire(SyntheticBank(self.config))
        self.assertEqual(self.component("transactions"), (None, None, None))
        self.assertEqual(self.connection(), (None, None, None))

    def test_a_later_non_http_failure_does_not_keep_a_stale_diagnostic(self):
        self.reserve("transactions", "balances")
        diagnostic = ProviderDiagnostic(503, "ASPSP_TIMEOUT")
        observation = replace(self.observation(balances=(), transactions_error="unavailable"),
                              transactions_diagnostic=diagnostic)
        self.store.commit_snapshot("bank", self.batch(observation), attempt_id="round")
        self.assertEqual(self.component("transactions"), ("unavailable", 503, "ASPSP_TIMEOUT"))
        self.now += 60
        self.reserve("transactions", "balances", attempt_id="next")
        self.store.commit_snapshot("bank", self.batch(self.observation(
            balances=(), transactions_error="invalid_data")), attempt_id="next")
        self.assertEqual(self.component("transactions"), ("invalid_data", None, None))

    def test_round_failure_records_the_connection_and_reserved_components(self):
        bank = DiagnosticBank(self.config, self.key_pem, session=http_error(422, error_body("EXPIRED_SESSION")))
        self.acquire(bank)
        self.assertEqual(self.connection(), ("provider_error", 422, "EXPIRED_SESSION"))
        # The session failed before any reservation, so no component was touched.
        self.assertEqual(self.component("transactions"), (None, None, None))
        self.assert_no_response_text()

        self.now += 86400
        bank = DiagnosticBank(self.config, self.key_pem,
                              transactions=http_error(401, error_body("UNAUTHORIZED_ACCESS")))
        self.acquire(bank)
        self.assertEqual(self.connection(), ("authorization", 401, "UNAUTHORIZED_ACCESS"))
        self.assertEqual(self.component("transactions"), ("authorization", 401, "UNAUTHORIZED_ACCESS"))
        self.assert_no_response_text()

    def test_non_http_round_failure_clears_the_connection_diagnostic(self):
        self.store.record_failure("bank", "provider_error", diagnostic=ProviderDiagnostic(404, "ACCOUNT_DOES_NOT_EXIST"))
        self.assertEqual(self.connection(), ("provider_error", 404, "ACCOUNT_DOES_NOT_EXIST"))
        self.store.record_failure("bank", "network")
        self.assertEqual(self.connection(), ("network", None, None))

    def test_store_revalidates_whatever_it_is_given(self):
        class Row:
            last_http_status = last_provider_error = "unset"
        row = Row()
        set_diagnostic(row, ProviderDiagnostic(999, PRIVATE))
        self.assertEqual((row.last_http_status, row.last_provider_error), (None, None))
        set_diagnostic(row, ProviderDiagnostic(400, UNLISTED_PROVIDER_ERROR))
        self.assertEqual((row.last_http_status, row.last_provider_error), (400, UNLISTED_PROVIDER_ERROR))
        set_diagnostic(row, object())
        self.assertEqual((row.last_http_status, row.last_provider_error), (None, None))

    def test_0016_roundtrip_keeps_history_and_drops_only_the_diagnostics(self):
        bank = DiagnosticBank(self.config, self.key_pem, transactions=http_error(400, error_body("ASPSP_ERROR")))
        self.acquire(bank)
        engine, _ = db.init_db(self.path)
        with engine.begin() as connection:
            before = connection.exec_driver_sql("SELECT count(*) FROM budget_transactions").scalar_one()
            command.downgrade(db._alembic_config(connection), "0015")
            for table in ("budget_components", "budget_connections"):
                columns = {row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")}
                self.assertFalse({"last_http_status", "last_provider_error"} & columns)
            command.upgrade(db._alembic_config(connection), "head")
            self.assertEqual(connection.exec_driver_sql("SELECT count(*) FROM budget_transactions").scalar_one(), before)
            self.assertEqual(connection.exec_driver_sql(
                "SELECT count(*) FROM budget_components WHERE last_http_status IS NOT NULL").scalar_one(), 0)



if __name__ == "__main__":
    unittest.main()
