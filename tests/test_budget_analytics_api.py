"""Real aiohttp/SQLite analytics boundaries, with synthetic owner-only records."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from types import SimpleNamespace
import unittest
from unittest import mock

from multidict import MultiDict

from hestia import api_contract, db, web
from hestia.budget import analytics_api, analytics_views, config, forecast, runtime
from hestia.budget.analytics_core import financial_fingerprint
from hestia.budget.normalize import AccountSnapshot, Snapshot
from hestia.budget.recurring import RecurringHint, RecurringOccurrence
from tests import test_budget_api as fixture
from tests.test_budget_store import prepare_selected, transaction

PREFIX = analytics_api.PREFIX
PERIOD = {"period": "2026-11"}


class BudgetAnalyticsAPITests(unittest.IsolatedAsyncioTestCase):
    rows = fixture.BudgetAPITests.rows
    token = fixture.BudgetAPITests.token
    headers = fixture.BudgetAPITests.headers
    assert_private_headers = fixture.BudgetAPITests.assert_private_headers

    async def asyncSetUp(self):
        patch = mock.patch.dict(os.environ, {analytics_api.FLAG: "1", "HESTIA_BUDGET_JOBS_ENABLED": "0",
                               "HESTIA_BUDGET_POLLER_ENABLED": "0", "HESTIA_BUDGET_CONSENT_ENABLED": "0"})
        patch.start()
        self.addCleanup(patch.stop)
        # Reuse fixture construction, without inheriting or re-running its tests.
        await fixture.BudgetAPITests.asyncSetUp(self)
        first = replace(self.config.connections[0], accounts=(fixture.PRIVATE_A, "ALICE-SYNTHETIC-CREDIT"))
        self.config = replace(self.config, connections=(first, self.config.connections[1]))
        prepare_selected(self.store, self.config)
        self.credit_id = next(a.id for a in self.rows(db.BudgetAccount) if a.identification_hash == "ALICE-SYNTHETIC-CREDIT")
        self.debit = transaction("alice-pair-debit", amount="10", remittance_information=["Synthetic transfer"])
        self.credit = transaction("alice-pair-credit", amount="10", credit_debit_indicator="CRDT",
                                  booking_date="2026-11-21", remittance_information=["Synthetic transfer"])
        self.store.commit_snapshot("bank", Snapshot(web._now() + 31536000,
            (AccountSnapshot(fixture.PRIVATE_A, (), (self.debit,)),
             AccountSnapshot("ALICE-SYNTHETIC-CREDIT", (), (self.credit,))), "2026-01-01", "2026-12-31"))
        for patch in (mock.patch.object(config, "load_config", side_effect=AssertionError("No configuration reads")),
                      mock.patch.object(runtime, "credentials", side_effect=AssertionError("No credentials")),
                      mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("No provider requests"))):
            guard = patch.start()
            self.addCleanup(patch.stop)
            self.addCleanup(guard.assert_not_called)

    async def get(self, operation, *, params=None, user="alice", headers=None):
        if params is None:
            params = PERIOD if operation in ("overview", "transactions", "transfers", "spending-limits") else None
        return await self.client.get(PREFIX + "/" + operation, params=params,
                                     headers=self.headers(user) if headers is None else headers)

    async def post(self, operation, body, *, user="alice", headers=None):
        headers = self.headers(user, origin=True) if headers is None else headers
        return await self.client.post(PREFIX + "/" + operation, data=json.dumps(body),
                                     headers={**headers, "Content-Type": "application/json"})

    def annotation(self, **changes):
        return {"account_id": self.alice_id, "entry_reference": self.debit["entry_reference"], "mode": "manual",
                "category_id": None, "kind": "expense", "expected_version": 0,
                "expected_fingerprint": financial_fingerprint(self.debit), **changes}

    def candidate(self, **changes):
        return {"rule_id": None, "expected_version": 0, "field": "description", "operator": "contains",
                "value": "Synthetic transfer", "category_id": None, "kind": "expense", "priority": 10,
                "enabled": True, **changes}

    def bodies(self):
        return {"category-create": {"name": "Synthetic groceries"},
                "category-update": {"category_id": "unknown", "name": "Synthetic", "archived": False, "expected_version": 0},
                "annotation": self.annotation(), "rule-preview": self.candidate(), "rule-save": {"preview_id": "unknown"},
                "transfer-decision": {"action": "undo", "decision_id": "unknown", "expected_version": 1},
                "spending-limit-save": {"category_id": "unknown", "currency": "PLN", "monthly_limit": "100",
                                        "warning_percent": 80, "enabled": True, "expected_version": 0},
                "mortgage-save": {"schedule_id": None, "label": "Synthetic mortgage", "amount": "2000",
                                  "currency": "PLN", "due_day": 15, "start_date": "2026-01-01", "end_date": None,
                                  "remind_days_before": 5, "destination_kind": "owner_inbox",
                                  "enabled": True, "expected_version": 0},
                "mortgage-payment-set": {"schedule_id": "unknown", "month": "2026-01", "paid": True,
                                         "expected_version": 1},
                "inbox-mark": {"items": [{"kind": "consent_renewal", "subject_id": "a" * 64, "milestone": "g0:exp0:expired"}],
                               "read": True, "expected_reads_version": "0" * 64}}

    async def test_all_eleven_reads_are_private_local_bounded_and_match_wire_models(self):
        models = {"overview": api_contract.BudgetAnalyticsOverview, "transactions": api_contract.BudgetAnalyticsTransactions,
                  "categories": api_contract.BudgetAnalyticsCategories, "rules": api_contract.BudgetAnalyticsRules,
                  "transfers": api_contract.BudgetAnalyticsTransfers, "recurring": api_contract.BudgetAnalyticsRecurring,
                  "spending-limits": api_contract.BudgetAnalyticsSpendingLimits,
                  "mortgages": api_contract.BudgetAnalyticsMortgages,
                  "inbox": api_contract.BudgetAnalyticsInbox,
                  "quarantine": api_contract.BudgetAnalyticsQuarantine,
                  "forecast": api_contract.BudgetAnalyticsForecast}
        self.assertEqual(set(models), set(analytics_api.READS))
        before = len(self.rows(db.BudgetAcquisition))
        with mock.patch.object(config, "load_config", side_effect=AssertionError("No configuration read")), \
             mock.patch.object(runtime, "credentials") as credentials, \
             mock.patch("urllib.request.OpenerDirector.open") as http, mock.patch.object(web, "_audit") as audit:
            for operation, model in models.items():
                response = await self.get(operation)
                self.assertEqual(response.status, 200, await response.text())
                self.assert_private_headers(response)
                body = await response.json()
                model.model_validate(body)
                serialized = json.dumps(body)
                for private in (fixture.PRIVATE_A, fixture.PRIVATE_B, self.bob_id, "Bob private description"):
                    self.assertNotIn(private, serialized)
                head = await self.client.head(PREFIX + "/" + operation, headers=self.headers())
                self.assertEqual(head.status, 405)
            for operation in ("overview", "forecast"):
                bearer = await self.get(operation, headers=self.headers(bearer=True))
                self.assertEqual(bearer.status, 200)
        credentials.assert_not_called()
        http.assert_not_called()
        audit.assert_not_called()
        self.assertEqual(len(self.rows(db.BudgetAcquisition)), before)
        self.assertEqual(self.rows(db.BudgetAnalyticsAudit), [])

    async def test_all_routes_require_current_admin_and_auth_feature_flags(self):
        for user, expected in (("viewer", 403), ("operator", 403), ("disabled", 401)):
            for operation in analytics_api.READS:
                response = await self.get(operation, user=user)
                self.assertEqual(response.status, expected)
            for operation, body in self.bodies().items():
                response = await self.post(operation, body, user=user)
                self.assertEqual(response.status, expected)
        anonymous = await self.get("overview", headers={})
        self.assertEqual(anonymous.status, 401)
        anonymous_forecast = await self.get("forecast", headers={})
        self.assertEqual(anonymous_forecast.status, 401)
        for environment, expected in (({analytics_api.FLAG: "0"}, 404), ({"HESTIA_BUDGET_HTTP_ENABLED": "0"}, 404),
                                      ({"HESTIA_PERSIST": "json"}, 409)):
            with mock.patch.dict(os.environ, environment):
                response = await self.get("overview")
                self.assertEqual(response.status, expected)
                forecast_response = await self.get("forecast")
                self.assertEqual(forecast_response.status, expected)
                self.assertEqual(await forecast_response.json(), await response.json())
        with mock.patch.object(web, "_AUTH_ENABLED", False):
            self.assertEqual((await self.get("overview")).status, 401)

    async def test_all_writes_require_cookie_and_exact_origin(self):
        for operation, body in self.bodies().items():
            for headers in (self.headers(bearer=True, origin=True), self.headers(),
                            {**self.headers(), "Origin": "https://foreign.invalid"}):
                response = await self.post(operation, body, headers=headers)
                self.assertEqual(response.status, 403, operation)
            response = await self.post(operation, body, headers={"Origin": fixture.ORIGIN})
            self.assertEqual(response.status, 401)
        self.assertEqual(self.rows(db.BudgetAnalyticsAudit), [])

    async def test_queries_and_json_are_strict_and_missing_fingerprints_cannot_write(self):
        for operation in analytics_api.READS:
            for params in ({"unexpected": "x", **(PERIOD if operation in analytics_api._QUERY_FIELDS else {})},
                           MultiDict([("period", "2026-11"), ("period", "2026-10")])):
                response = await self.get(operation, params=params)
                self.assertEqual(response.status, 400)
        for params in ({}, {"period": ""}, {"period": "x" * 129}, {**PERIOD, "limit": "0"},
                       {**PERIOD, "limit": "201"}, {**PERIOD, "limit": "-1"},
                       {**PERIOD, "query": "x" * 257}):
            self.assertEqual((await self.get("transactions", params=params)).status, 400)
        for params in ({"period": "2026-11"}, {"currency": "pln"},
                       MultiDict([("currency", "PLN"), ("currency", "EUR")])):
            self.assertEqual((await self.get("recurring", params=params)).status, 400)
        self.assertEqual((await self.get("recurring", params={"currency": "PLN"})).status, 200)
        self.assertEqual((await self.get("spending-limits", params={"period": "2026"})).status, 400)
        self.assertEqual((await self.get("spending-limits", params={"period": "2026-11", "currency": "PLN"})).status, 200)
        for value in ('{"name":"a","name":"b"}', '{', '[]', '{"name":"x","extra":true}'):
            response = await self.client.post(PREFIX + "/category-create", data=value,
                       headers={**self.headers(origin=True), "Content-Type": "application/json"})
            self.assertEqual(response.status, 400)
        missing = self.annotation(); del missing["expected_fingerprint"]
        self.assertEqual((await self.post("annotation", missing)).status, 400)
        self.assertEqual(self.rows(db.BudgetAnnotation), [])
        # This bound is larger than aiohttp's request-line bound; exercise the
        # query parser directly without asking the transport to log a huge URL.
        with self.assertRaises(analytics_api.AnalyticsError):
            analytics_api._query(SimpleNamespace(query=MultiDict({**PERIOD, "cursor": "x" * 8193})), "transactions")

    async def test_forecast_strict_queries_and_stored_json_error_mapping(self):
        valid = await self.get("forecast", params={"months": "01", "currency": "PLN"})
        self.assertEqual(valid.status, 200)
        self.assertEqual((await valid.json())["horizon_months"], 1)
        with mock.patch.object(analytics_api, "_store", side_effect=AssertionError("invalid forecast query reached store")):
            for params in ({"months": "0"}, {"months": "13"}, {"months": "001"}, {"months": "1.0"},
                           {"months": "１"}, {"months": " 1"}, {"today": "2026-11-22"}, {"currency": "pln"},
                           MultiDict([("months", "1"), ("months", "2")]),
                           MultiDict([("currency", "PLN"), ("currency", "EUR")])):
                with self.subTest(params=params):
                    response = await self.get("forecast", params=params)
                    self.assertEqual(response.status, 400)
                    self.assertEqual(await response.json(), {"ok": False, "error": "budget_analytics_invalid_request"})
                    self.assert_private_headers(response)

        with self.sessions.begin() as session:
            entry = session.get(db.BudgetTransaction, (self.alice_id, self.debit["entry_reference"]))
            entry.payload_json = '{"Synthetic private data":'
        with mock.patch.object(analytics_api.api.log, "error") as log:
            bad_query = await self.get("forecast", params={"months": "13"})
            self.assertEqual(bad_query.status, 400)
            self.assertEqual((await bad_query.json())["error"], "budget_analytics_invalid_request")
            corrupt = await self.get("forecast")
            self.assertEqual(corrupt.status, 500)
            self.assertEqual(await corrupt.json(), {"ok": False, "error": "budget_analytics_data_error"})
            self.assert_private_headers(corrupt)
            legacy = await self.get("recurring")
            self.assertEqual(legacy.status, 400)
            self.assertEqual((await legacy.json())["error"], "budget_analytics_invalid_request")
        log.assert_not_called()

    async def test_forecast_unencodable_stored_label_returns_private_data_error(self):
        with self.sessions.begin() as session:
            for month in (8, 9, 10):
                ref = f"synthetic-charge-{month}"
                entry = transaction(ref, amount="7.42", booking_date=f"2026-{month:02d}-15",
                                    remittance_information=["\ud800"])
                session.add(db.BudgetTransaction(account_id=self.alice_id, entry_reference=ref,
                            payload_json=json.dumps(entry), observed_at=1, source="api", uncertain=False))
        now = datetime(2026, 11, 22, tzinfo=timezone.utc).timestamp()
        with mock.patch.object(analytics_views.BudgetAnalyticsViews, "_now", return_value=now), \
             mock.patch.object(analytics_api.api.log, "error") as log:
            response = await self.get("forecast")
            self.assertEqual(response.status, 500)
            self.assertEqual(await response.json(), {"ok": False, "error": "budget_analytics_data_error"})
            self.assert_private_headers(response)
        log.assert_not_called()

    async def test_forecast_schedules_and_freshness_are_owner_private(self):
        schedules = {}
        for owner, label in (("alice", "Synthetic Alice mortgage"), ("bob", "Synthetic Bob mortgage")):
            response = await self.post("mortgage-save", self.bodies()["mortgage-save"] | {"label": label}, user=owner)
            self.assertEqual(response.status, 200)
            schedules[owner] = (await response.json())["schedule"]["id"]

        for owner, foreign in (("alice", "bob"), ("bob", "alice")):
            response = await self.get("forecast", params={"months": "2"}, user=owner)
            self.assertEqual(response.status, 200)
            result = await response.json()
            api_contract.BudgetAnalyticsForecast.model_validate(result)
            self.assertEqual({item["schedule_id"] for month in result["months"] for item in month["items"]},
                             {schedules[owner]})
            self.assertEqual(result["summary"]["selected_accounts"], 2 if owner == "alice" else 1)
            self.assertNotIn(schedules[foreign], json.dumps(result))
            self.assertNotIn("Synthetic " + foreign.title() + " mortgage", json.dumps(result))

        with self.sessions.begin() as session:
            session.get(db.BudgetConnection, "bank").owner = "bob"
        alice = await (await self.get("forecast", params={"months": "1"})).json()
        self.assertEqual(alice["summary"]["selected_accounts"], 0)
        self.assertEqual(alice["stale_account_ids"], [])
        self.assertIsNone(alice["ledger_as_of"])
        self.assertTrue(alice["freshness_stale"])

    def insert_forecast_schedules(self, count, **changes):
        with self.sessions.begin() as session:
            session.add_all([
                db.BudgetMortgageSchedule(**({"id": f"synthetic-mortgage-{index:03d}", "owner": "alice",
                    "label": "Synthetic mortgage", "amount": "2100", "currency": "PLN", "due_day": 15,
                    "start_date": "2026-11-01", "remind_days_before": 5, "destination_kind": "owner_inbox",
                    "enabled": True, "version": 1, "created_at": 1, "updated_at": 1} | changes))
                for index in range(count)
            ])

    async def assert_forecast_limit(self, *, params=None):
        response = await self.get("forecast", params=params)
        self.assertEqual(response.status, 413)
        self.assert_private_headers(response)
        self.assertEqual(await response.json(), {"ok": False, "error": "budget_analytics_limit"})

    async def test_forecast_source_schedule_payment_and_projected_item_caps(self):
        with mock.patch.object(analytics_views, "MAX_ROWS", 0):
            await self.assert_forecast_limit(params={"currency": "EUR"})

        self.insert_forecast_schedules(1, start_date="1900-01-01")
        with mock.patch.object(forecast, "MAX_FORECAST_ITEMS", 1):
            await self.assert_forecast_limit(params={"months": "2"})
        with self.sessions.begin() as session:
            session.add_all([
                db.BudgetMortgagePayment(schedule_id="synthetic-mortgage-000", owner="alice",
                                         month=f"{1900 + index // 12:04d}-{index % 12 + 1:02d}", marked_at=1)
                for index in range(1201)
            ])
        await self.assert_forecast_limit(params={"currency": "EUR"})

        with self.sessions.begin() as session:
            session.delete(session.get(db.BudgetMortgagePayment, ("synthetic-mortgage-000", "2000-01")))
        result = await self.get("forecast", params={"currency": "EUR"})
        self.assertEqual(result.status, 200)
        with self.sessions.begin() as session:
            session.query(db.BudgetMortgagePayment).delete()
            session.query(db.BudgetMortgageSchedule).delete()
        self.insert_forecast_schedules(101, enabled=False)
        await self.assert_forecast_limit(params={"currency": "EUR"})

    async def test_forecast_capacity_and_bounded_utf8_wire_response(self):
        label = "🦉" * 256
        self.insert_forecast_schedules(100, amount="1" * 77)
        hints = tuple(
            RecurringHint(label=label + str(index), normalized_label=label + str(index), label_source="description",
                          amount="2" * 77, currency="PLN", cadence="monthly", occurrence_count=3, distinct_months=3,
                          first_date="2026-08-15", last_date="2026-10-15", min_gap_days=30, max_gap_days=31,
                          account_ids=(self.alice_id,), evidence=tuple(
                              RecurringOccurrence(self.alice_id, f"charge-{index}-{month}", f"2026-{month:02d}-15", "a" * 64)
                              for month in (8, 9, 10)))
            for index in range(200)
        )
        short_hints = tuple(replace(hint, label=f"Synthetic charge {index}", normalized_label=f"synthetic charge {index}")
                            for index, hint in enumerate(hints))
        test_case = self
        buffers = []
        cap = analytics_api.MAX_FORECAST_RESPONSE_BYTES
        self.assertEqual(cap, 4 * 1024 * 1024)

        class CheckedBuffer(bytearray):
            def __init__(self):
                super().__init__()
                buffers.append(self)

            def extend(self, chunk):
                test_case.assertLessEqual(len(self) + len(chunk), analytics_api.MAX_FORECAST_RESPONSE_BYTES)
                super().extend(chunk)

        now = datetime(2026, 11, 22, tzinfo=timezone.utc).timestamp()
        params = {"months": "12"}
        with mock.patch.object(analytics_views.BudgetAnalyticsViews, "_now", return_value=now), \
             mock.patch.object(analytics_views, "_snapshot_recurring",
                               return_value=short_hints + (replace(short_hints[-1], currency="EUR"),)) as recurring, \
             mock.patch.object(analytics_api, "bytearray", CheckedBuffer, create=True):
            await self.assert_forecast_limit(params=params)
            self.assertEqual(buffers, [])
            recovered = await self.get("forecast", params=params | {"currency": "PLN"})
            self.assertEqual(recovered.status, 200)
            recovered_body = await recovered.read()
            self.assertLess(len(recovered_body), cap)
            self.assertEqual(sum(len(month["items"]) for month in json.loads(recovered_body)["months"]), 3600)

            with self.sessions.begin() as session:
                session.query(db.BudgetMortgageSchedule).filter_by(owner="alice").update({"label": label})
            recurring.return_value = hints
            buffers.clear()
            await self.assert_forecast_limit(params=params)
            self.assertEqual(len(buffers), 1)
            self.assertLessEqual(len(buffers[0]), cap)
            self.assertGreater(len(buffers[0]), cap - 4096)
            with mock.patch.object(analytics_api, "MAX_FORECAST_RESPONSE_BYTES", cap * 2), \
                 mock.patch.object(analytics_api.web, "Response", wraps=analytics_api.web.Response) as response_factory:
                response = await self.get("forecast", params=params)
                self.assertEqual(response.status, 200)
                self.assertIs(response_factory.call_args.kwargs["body"], buffers[1])
                wire = await response.read()
        self.assert_private_headers(response)
        self.assertEqual(response.headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(int(response.headers["Content-Length"]), len(wire))
        self.assertEqual(wire, buffers[1])
        self.assertGreater(len(wire), cap)
        self.assertLess(len(wire.decode("utf-8")), cap)
        result = json.loads(wire)
        self.assertEqual(sum(len(month["items"]) for month in result["months"]), 3600)
        self.assertEqual({item["label"] for item in result["months"][0]["items"]}, {label, label[:255] + "…"})

    async def test_role_and_authoritative_backend_are_rechecked_inside_store_operations(self):
        original = analytics_api._read
        for change, expected in (({"role": "viewer"}, 403), ({"disabled": True}, 401)):
            with self.sessions.begin() as session:
                user = session.get(db.User, "alice")
                user.role, user.disabled = "admin", False
            def revoke(*args):
                with self.sessions.begin() as session:
                    user = session.get(db.User, "alice")
                    for key, value in change.items():
                        setattr(user, key, value)
                return original(*args)
            with mock.patch.object(analytics_api, "_read", side_effect=revoke):
                response = await self.get("transactions")
                self.assertEqual(response.status, expected)
                self.assertNotIn("alice-pair-debit", await response.text())
        with self.sessions.begin() as session:
            user = session.get(db.User, "alice")
            user.role, user.disabled = "admin", False
        original_write = analytics_api._write
        def revoke_write(*args):
            with self.sessions.begin() as session:
                session.get(db.User, "alice").disabled = True
            return original_write(*args)
        with mock.patch.object(analytics_api, "_write", side_effect=revoke_write):
            self.assertEqual((await self.post("category-create", {"name": "Denied"})).status, 401)
        self.assertEqual(self.rows(db.BudgetCategory), [])
        self.assertEqual(self.rows(db.BudgetAnalyticsAudit), [])

    async def test_missing_authority_and_corrupt_parent_ownership_fail_closed(self):
        with self.sessions.begin() as session:
            session.get(db.BudgetConnection, "bank").owner = "bob"
        response = await self.get("transactions")
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["transactions"], [])
        original = analytics_api._read
        def remove_authority(*args):
            with self.sessions.begin() as session:
                session.delete(session.get(db.AppMeta, "users_authoritative"))
            return original(*args)
        with mock.patch.object(analytics_api, "_read", side_effect=remove_authority):
            response = await self.get("overview")
            self.assertEqual(response.status, 409)
            self.assertEqual((await response.json())["error"], "budget_requires_sqlite_users")

    async def test_pagination_is_owner_bound_and_source_change_returns_conflict(self):
        params = {**PERIOD, "limit": "1"}
        response = await self.get("transactions", params=params)
        self.assertEqual(response.status, 200)
        first = await response.json()
        self.assertIsNotNone(first["next_cursor"])
        second_response = await self.get("transactions", params={**params, "cursor": first["next_cursor"]})
        self.assertEqual(second_response.status, 200)
        second = await second_response.json()
        self.assertNotEqual(first["transactions"][0]["entry_reference"], second["transactions"][0]["entry_reference"])
        foreign = await self.get("transactions", params={**params, "cursor": first["next_cursor"]}, user="bob")
        self.assertEqual(foreign.status, 400)
        self.assertEqual((await self.post("annotation", self.annotation())).status, 200)
        stale = await self.get("transactions", params={**params, "cursor": first["next_cursor"]})
        self.assertEqual(stale.status, 409)

    async def test_flag_is_rechecked_after_json_and_unexpected_errors_are_sanitized(self):
        with mock.patch.object(analytics_api, "enabled", side_effect=(True, False)):
            response = await self.post("category-create", {"name": "Not written"})
            self.assertEqual(response.status, 404)
        self.assertEqual(self.rows(db.BudgetCategory), [])
        with mock.patch.object(analytics_api, "_read", side_effect=RuntimeError("SYNTHETIC-PRIVATE-BODY")), \
             mock.patch.object(analytics_api.api.log, "error") as log:
            response = await self.get("overview")
            self.assertEqual(response.status, 500)
            self.assertNotIn("SYNTHETIC-PRIVATE", await response.text())
            self.assertNotIn("SYNTHETIC-PRIVATE", repr(log.call_args))

    async def test_category_annotation_controls_and_source_conflicts_match_contract(self):
        response = await self.post("category-create", {"name": "Synthetic groceries"})
        self.assertEqual(response.status, 200, await response.text())
        saved = await response.json()
        api_contract.BudgetAnalyticsCategoryResult.model_validate(saved)
        category = saved["category"]
        repeated = await (await self.post("category-create", {"name": "Synthetic groceries"})).json()
        self.assertEqual((repeated["category"]["id"], repeated["category"]["changed"]), (category["id"], False))
        changed = await self.post("category-update", {"category_id": category["id"], "name": "Renamed",
                                "archived": False, "expected_version": category["version"]})
        self.assertEqual(changed.status, 200)
        conflict = await self.post("category-update", {"category_id": category["id"], "name": "Stale",
                                 "archived": False, "expected_version": category["version"]})
        self.assertEqual(conflict.status, 409)
        manual = await self.post("annotation", self.annotation(category_id=category["id"]))
        self.assertEqual(manual.status, 200, await manual.text())
        api_contract.BudgetAnalyticsAnnotationResult.model_validate(await manual.json())
        self.assertEqual((await self.post("annotation", self.annotation())).status, 409)
        bad_source = await self.post("annotation", self.annotation(expected_version=1, expected_fingerprint="0" * 64))
        self.assertEqual(bad_source.status, 409)
        foreign = await self.post("annotation", self.annotation(account_id=self.bob_id, entry_reference="bob-entry"))
        self.assertEqual(foreign.status, 404)
        virtual = await self.post("annotation", self.annotation(entry_reference="alice-older", mode="auto", kind="auto",
                                 expected_fingerprint=financial_fingerprint(transaction("alice-older", booking_date="2026-10-01"))))
        self.assertEqual(virtual.status, 200)
        api_contract.BudgetAnalyticsAnnotationResult.model_validate(await virtual.json())
        self.assertTrue(self.rows(db.BudgetAnalyticsAudit))
        self.assertEqual(self.rows(db.Audit), [])

    async def test_rule_preview_save_replay_expiry_and_selection_change_are_fenced(self):
        response = await self.post("rule-preview", self.candidate())
        self.assertEqual(response.status, 200, await response.text())
        preview = (await response.json())["preview"]
        api_contract.BudgetAnalyticsPreview.model_validate(preview)
        response = await self.post("rule-save", {"preview_id": preview["preview_id"]})
        self.assertEqual(response.status, 200, await response.text())
        saved = (await response.json())["rule"]
        api_contract.BudgetAnalyticsRuleSaved.model_validate(saved)
        again = await (await self.post("rule-save", {"preview_id": preview["preview_id"]})).json()
        self.assertEqual((again["rule"]["id"], again["rule"]["changed"]), (saved["id"], False))
        expired = (await (await self.post("rule-preview", self.candidate(value="expired"))).json())["preview"]
        with self.sessions.begin() as session:
            session.get(db.BudgetRulePreview, expired["preview_id"]).expires_at = web._now() - 1
        self.assertEqual((await self.post("rule-save", {"preview_id": expired["preview_id"]})).status, 409)
        stale = (await (await self.post("rule-preview", self.candidate(value="stale"))).json())["preview"]
        with self.sessions.begin() as session:
            session.get(db.BudgetAccount, self.credit_id).enabled = False
        self.assertEqual((await self.post("rule-save", {"preview_id": stale["preview_id"]})).status, 409)

    async def test_transfer_accept_reject_undo_and_fingerprints_are_atomic(self):
        response = await self.get("transfers")
        self.assertEqual(response.status, 200, await response.text())
        pairs = (await response.json())["transfers"]
        pair = next(p for p in pairs if p["debit"]["entry_reference"] == self.debit["entry_reference"])
        body = {"debit": {"account_id": self.alice_id, "entry_reference": self.debit["entry_reference"]},
                "credit": {"account_id": self.credit_id, "entry_reference": self.credit["entry_reference"]},
                "action": "accept", "expected_version": 0, "source": "manual",
                "expected_debit_fingerprint": pair["debit"]["fingerprint"],
                "expected_credit_fingerprint": pair["credit"]["fingerprint"]}
        missing = dict(body); del missing["expected_debit_fingerprint"]
        self.assertEqual((await self.post("transfer-decision", missing)).status, 400)
        self.assertEqual(self.rows(db.BudgetTransferLeg), [])
        response = await self.post("transfer-decision", body)
        self.assertEqual(response.status, 200, await response.text())
        accepted = (await response.json())["decision"]
        api_contract.BudgetAnalyticsDecisionSaved.model_validate(accepted)
        self.assertEqual(len(self.rows(db.BudgetTransferLeg)), 2)
        reject = {"decision_id": accepted["id"], "action": "reject", "expected_version": accepted["version"],
                  "expected_debit_fingerprint": pair["debit"]["fingerprint"],
                  "expected_credit_fingerprint": pair["credit"]["fingerprint"]}
        response = await self.post("transfer-decision", reject)
        self.assertEqual(response.status, 200, await response.text())
        rejected = (await response.json())["decision"]
        self.assertEqual((rejected["status"], self.rows(db.BudgetTransferLeg)), ("rejected", []))
        response = await self.post("transfer-decision", {"decision_id": accepted["id"], "action": "undo",
                                                        "expected_version": rejected["version"]})
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await response.json())["decision"]["status"], "undone")

    async def test_recurring_endpoint_returns_validated_hints_and_filters_by_currency(self):
        charges = tuple(
            transaction(f"alice-rec-{day}", amount="29.99", currency="PLN", booking_date=day,
                        remittance_information=["Synthetic Streaming"])
            for day in ("2026-09-10", "2026-10-10", "2026-11-10")
        )
        self.store.commit_snapshot("bank", Snapshot(web._now() + 31536000,
            (AccountSnapshot(fixture.PRIVATE_A, (), charges),
             AccountSnapshot("ALICE-SYNTHETIC-CREDIT", (), ())), "2026-01-01", "2026-12-31"))
        response = await self.get("recurring")
        self.assertEqual(response.status, 200, await response.text())
        self.assert_private_headers(response)
        body = await response.json()
        api_contract.BudgetAnalyticsRecurring.model_validate(body)
        self.assertEqual((body["total_hints"], body["truncated"], len(body["hints"])), (1, False, 1))
        self.assertEqual((body["hints"][0]["label"], body["hints"][0]["amount"], body["hints"][0]["currency"]),
                         ("Synthetic Streaming", "29.99", "PLN"))
        self.assertNotIn("fingerprint", json.dumps(body))
        eur = await self.get("recurring", params={"currency": "EUR"})
        self.assertEqual(eur.status, 200)
        self.assertEqual((await eur.json())["total_hints"], 0)

    async def test_spending_limit_save_and_monthly_evaluation_match_contract(self):
        category = (await (await self.post("category-create", {"name": "Groceries"})).json())["category"]
        self.assertEqual((await self.post("annotation", self.annotation(category_id=category["id"]))).status, 200)
        body = {"category_id": category["id"], "currency": "PLN", "monthly_limit": "12.00",
                "warning_percent": 80, "enabled": True, "expected_version": 0}
        # Strict JSON rejects bool-as-int on warning_percent and expected_version.
        self.assertEqual((await self.post("spending-limit-save", {**body, "warning_percent": True})).status, 400)
        self.assertEqual((await self.post("spending-limit-save", {**body, "expected_version": False})).status, 400)
        saved_resp = await self.post("spending-limit-save", body)
        self.assertEqual(saved_resp.status, 200, await saved_resp.text())
        saved = await saved_resp.json()
        api_contract.BudgetAnalyticsSpendingLimitResult.model_validate(saved)
        self.assertEqual((saved["limit"]["monthly_limit"], saved["limit"]["version"], saved["limit"]["changed"]),
                         ("12", 1, True))
        # Retrying create with expected_version=0 returns 409 conflict (no silent upsert).
        self.assertEqual((await self.post("spending-limit-save", body)).status, 409)
        view_resp = await self.get("spending-limits", params={"period": "2026-11"})
        self.assertEqual(view_resp.status, 200, await view_resp.text())
        self.assert_private_headers(view_resp)
        view = await view_resp.json()
        api_contract.BudgetAnalyticsSpendingLimits.model_validate(view)
        self.assertEqual((len(view["limits"]), len(view["statuses"]), view["statuses"][0]["status"],
                          view["statuses"][0]["net_spending"], view["statuses"][0]["remaining"]),
                         (1, 1, "approaching", "10", "2"))
        # Disable works with current expected_version and removes from statuses while keeping in limits.
        disabled = await self.post("spending-limit-save", {**body, "enabled": False, "expected_version": 1})
        self.assertEqual(disabled.status, 200)
        after = await (await self.get("spending-limits", params={"period": "2026-11"})).json()
        self.assertEqual((len(after["limits"]), after["statuses"]), (1, []))
        self.assertNotEqual(after["limits_version"], view["limits_version"])
        self.assertEqual(after["snapshot_digest"], view["snapshot_digest"])


if __name__ == "__main__":
    unittest.main()
