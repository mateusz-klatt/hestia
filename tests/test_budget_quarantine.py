"""Pure quarantine extractor, single-snapshot read views and HTTP API tests (OpenAPI 0.25.0)."""
from __future__ import annotations

import json
import unittest

from hestia import api_contract, db
from hestia.budget import quarantine as qmod
from hestia.budget.analytics_core import AnalyticsError
from hestia.budget.analytics_views import BudgetAnalyticsViews
from tests import test_budget_analytics_api as analytics_fixture
from tests.test_budget_analytics_store import AnalyticsCase
from tests.test_budget_store import transaction


def _tx_json(ref="ref-1", *, status="BOOK", booking_date="2026-11-10", value_date=None,
             amount="9876.54", description="SECRET-QUARANTINE-DESC"):
    payload = transaction(ref, amount=amount, status=status, booking_date=booking_date,
                          value_date=value_date, remittance_information=[description])
    return json.dumps(payload)


class QuarantinePureTests(unittest.TestCase):
    def test_extract_all_buckets_reasons_sorting_and_fail_closed_validation(self):
        # 1. Uncertain -> reference_conflict; zero amounts/descriptions leaked.
        u = qmod.extract_safe_item(
            bucket=qmod.BUCKET_UNCERTAIN, account_id="acc-1", selected=True,
            payload_json=_tx_json("u-1"), source="api", observed_at=100,
        )
        self.assertEqual(u, {
            "bucket": "uncertain", "account_id": "acc-1", "selected": True,
            "entry_reference": "u-1", "date": "2026-11-10", "status": "BOOK",
            "reason": "reference_conflict", "source": "api", "observed_at": 100.0,
        })
        self.assertNotIn("SECRET-QUARANTINE-DESC", json.dumps(u))
        self.assertNotIn("9876.54", json.dumps(u))

        # 2. Unresolved -> all 4 derived reasons (non_booked_status, missing_reference, missing_date, reference_conflict).
        r_pdng = qmod.extract_safe_item(
            bucket=qmod.BUCKET_UNRESOLVED, account_id="acc-1", selected=False,
            payload_json=_tx_json("r-1", status="PDNG", booking_date=None, value_date="2026-11-11"),
            source="api", observed_at=200.5,
        )
        self.assertEqual((r_pdng["reason"], r_pdng["date"], r_pdng["status"]), ("non_booked_status", "2026-11-11", "PDNG"))

        r_noref = qmod.extract_safe_item(
            bucket=qmod.BUCKET_UNRESOLVED, account_id="acc-1", selected=True,
            payload_json=_tx_json(None, status="BOOK", booking_date="2026-11-12"),
            source="api", observed_at=201.0,
        )
        self.assertEqual((r_noref["reason"], r_noref["entry_reference"]), ("missing_reference", None))

        r_nodate = qmod.extract_safe_item(
            bucket=qmod.BUCKET_UNRESOLVED, account_id="acc-1", selected=True,
            payload_json=_tx_json("r-nodate", status="BOOK", booking_date=None, value_date=None),
            source="api", observed_at=202.0,
        )
        self.assertEqual((r_nodate["reason"], r_nodate["date"]), ("missing_date", None))

        r_conf = qmod.extract_safe_item(
            bucket=qmod.BUCKET_UNRESOLVED, account_id="acc-1", selected=True,
            payload_json=_tx_json("r-conf", status="BOOK", booking_date="2026-11-13"),
            source="api", observed_at=203.0,
        )
        self.assertEqual(r_conf["reason"], "reference_conflict")

        # 3. Import unresolved -> all 5 allowlisted stored reasons + status=None allowed.
        for reason in ("non_canonical", "api_preserved", "stale_conflict", "reference_conflict", "same_time_conflict"):
            item = qmod.extract_safe_item(
                bucket=qmod.BUCKET_IMPORT_UNRESOLVED, account_id="acc-1", selected=True,
                payload_json=json.dumps({"entry_reference": "imp-1", "booking_date": None, "value_date": None, "status": None}),
                source="offline_import", observed_at=300.0, stored_reason=reason,
            )
            self.assertEqual((item["reason"], item["status"], item["date"]), (reason, None, None))

        # 4. Deterministic sorting: dated newest first, undated after dated, bounded by limit.
        ordered = qmod.sort_quarantine_items([r_nodate, u, r_conf, r_pdng], limit=3)
        self.assertEqual([item["entry_reference"] for item in ordered], ["r-conf", "r-1", "u-1"])

        # 5. Fail-closed integrity checks -> QuarantineDataError.
        base = {"bucket": qmod.BUCKET_UNCERTAIN, "account_id": "acc-1", "selected": True,
                "payload_json": _tx_json(), "source": "api", "observed_at": 10.0}
        bad_cases = (
            {**base, "bucket": "other"},
            {**base, "account_id": ""},
            {**base, "account_id": "x" * 129},
            {**base, "selected": 1},
            {**base, "source": "bank_csv"},
            {**base, "observed_at": -1},
            {**base, "observed_at": float("nan")},
            {**base, "observed_at": True},
            {**base, "payload_json": 123},
            {**base, "payload_json": "{bad-json"},
            {**base, "payload_json": "[]"},
            {**base, "payload_json": json.dumps({"entry_reference": ""})},
            {**base, "payload_json": json.dumps({"entry_reference": "x" * 1025})},
            {**base, "payload_json": json.dumps({"entry_reference": 123})},
            {**base, "payload_json": json.dumps({"booking_date": 20261110})},
            {**base, "payload_json": json.dumps({"booking_date": "2026-02-30"})},
            {**base, "payload_json": json.dumps({"booking_date": "2026-1-1"})},
            {**base, "payload_json": json.dumps({"booking_date": "20261110"})},
            {**base, "payload_json": json.dumps({"status": "BOGUS"})},
            {**base, "bucket": qmod.BUCKET_IMPORT_UNRESOLVED, "source": "offline_import", "stored_reason": None},
            {**base, "bucket": qmod.BUCKET_IMPORT_UNRESOLVED, "source": "offline_import", "stored_reason": "unknown"},
        )
        for bad in bad_cases:
            with self.subTest(bad=bad), self.assertRaises(qmod.QuarantineDataError):
                qmod.extract_safe_item(**bad)


class QuarantineViewsTests(AnalyticsCase):
    def setUp(self):
        super().setUp()
        self.views = BudgetAnalyticsViews(self.sessions, clock=lambda: self.now, cursor_secret=b"synthetic-only")
        self.account_id = self.accounts["a"]

    def test_counts_primary_all_buckets_account_filter_isolation_truncation_and_500_integrity(self):
        # Empty owner (0 accounts) returns zero counts cleanly without empty-IN SQL issues.
        with self.sessions.begin() as session:
            session.add(db.User(username="emptyadmin", password_hash="unused", role="admin"))
        empty = self.views.quarantine("emptyadmin")
        api_contract.BudgetAnalyticsQuarantine.model_validate(empty)
        self.assertEqual(empty["summary"], {"uncertain_count": 0, "unresolved_count": 0,
                                            "import_unresolved_count": 0, "total_count": 0})
        self.assertEqual((empty["accounts"], empty["items"], empty["truncated"]), ([], [], False))

        # Seed all 3 quarantine buckets across selected + excluded owned accounts and a foreign owner's account.
        with self.sessions.begin() as session:
            session.add(db.BudgetAccount(
                id="acc-excluded", connection_id="bank", owner="owner",
                identification_hash="EXCLUDED-HASH", enabled=False, configured=True,
                control_version=1, kind="current", balances_json="[]",
            ))
            session.add(db.BudgetAccount(
                id="other-acc", connection_id="other-bank", owner="other",
                identification_hash="OTHER-HASH", enabled=True, configured=True,
                control_version=1, kind="current", balances_json="[]",
            ))
            session.flush()
            # 1. Uncertain canonical row on owner's selected account (self.account_id).
            session.add(db.BudgetTransaction(
                account_id=self.account_id, entry_reference="unc-1",
                payload_json=_tx_json("unc-1", booking_date="2026-11-18", amount="4321.00",
                                      description="PRIVATE-UNCERTAIN-PROSE"),
                observed_at=1000.0, uncertain=True, source="api", api_observed_at=1000.0,
            ))
            # 2. API Unresolved rows on selected + excluded owned accounts.
            session.add(db.BudgetUnresolved(
                account_id=self.account_id, ordinal=0,
                payload_json=_tx_json("unres-pdng", status="PDNG", booking_date="2026-11-20",
                                      amount="777.77", description="PRIVATE-PENDING-PROSE"),
                observed_at=1100.0,
            ))
            session.add(db.BudgetUnresolved(
                account_id="acc-excluded", ordinal=0,
                payload_json=_tx_json(None, status="BOOK", booking_date="2026-11-19",
                                      amount="888.88", description="PRIVATE-EXCLUDED-PROSE"),
                observed_at=1050.0,
            ))
            # 3. Offline import unresolved on owner's selected account.
            session.add(db.BudgetImportBatch(
                id="batch-1", fingerprint="fp-batch-1", account_id=self.account_id,
                fetched_at=1080.0, imported_at=1090.0, balances_json=None, report_json="{}",
            ))
            session.add(db.BudgetImportUnresolved(
                batch_id="batch-1", ordinal=0, reason="api_preserved",
                payload_json=_tx_json("imp-ref-1", booking_date="2026-11-17",
                                      amount="999.99", description="PRIVATE-IMPORT-PROSE"),
            ))
            # 4. Foreign owner's unresolved row (must never leak to "owner").
            session.add(db.BudgetUnresolved(
                account_id="other-acc", ordinal=0,
                payload_json=_tx_json("foreign-ref", status="PDNG", booking_date="2026-11-21",
                                      description="FOREIGN-SECRET"),
                observed_at=1200.0,
            ))

        report = self.views.quarantine("owner")
        api_contract.BudgetAnalyticsQuarantine.model_validate(report)
        self.assertEqual(report["summary"], {
            "uncertain_count": 1, "unresolved_count": 2, "import_unresolved_count": 1, "total_count": 4,
        })
        self.assertFalse(report["truncated"])
        self.assertEqual(len(report["items"]), 4)
        # Matches overview's owner-wide scope counts identically.
        overview_scope = self.views.overview("owner", "2026-11")["scope"]
        self.assertEqual(
            (report["summary"]["uncertain_count"], report["summary"]["unresolved_count"],
             report["summary"]["import_unresolved_count"]),
            (overview_scope["uncertain_count"], overview_scope["unresolved_count"],
             overview_scope["import_unresolved_count"]),
        )
        # Strict privacy: zero descriptions, amounts or foreign records in serialized wire JSON.
        serialized = json.dumps(report)
        for secret in ("PRIVATE-UNCERTAIN-PROSE", "PRIVATE-PENDING-PROSE", "PRIVATE-EXCLUDED-PROSE",
                       "PRIVATE-IMPORT-PROSE", "FOREIGN-SECRET", "foreign-ref", "4321.00", "777.77",
                       "888.88", "999.99", "EXCLUDED-HASH", "OTHER-HASH"):
            self.assertNotIn(secret, serialized)
        # Account filter works for owned accounts and returns 404 for unknown/foreign accounts.
        filtered = self.views.quarantine("owner", account_id="acc-excluded")
        self.assertEqual(filtered["summary"]["total_count"], 1)
        self.assertEqual([row["account_id"] for row in filtered["accounts"]], ["acc-excluded"])
        self.assertEqual(filtered["items"][0]["reason"], "missing_reference")
        self.assertFalse(filtered["items"][0]["selected"])
        for bad_id in ("other-acc", "no-such-account"):
            with self.assertRaises(AnalyticsError) as nf:
                self.views.quarantine("owner", account_id=bad_id)
            self.assertEqual((nf.exception.code, nf.exception.status), ("budget_analytics_not_found", 404))
        for invalid_id in ("", "x" * 129, 123):
            with self.assertRaises(AnalyticsError) as bad:
                self.views.quarantine("owner", account_id=invalid_id)
            self.assertEqual(bad.exception.status, 400)

        # Truncation at items_cap=200 preserves exact counts while capping returned items cleanly.
        with self.sessions.begin() as session:
            for ordinal in range(1, 205):
                session.add(db.BudgetUnresolved(
                    account_id=self.account_id, ordinal=ordinal,
                    payload_json=_tx_json(f"bulk-{ordinal}", status="PDNG", booking_date="2026-11-15"),
                    observed_at=1000.0 + ordinal,
                ))
        capped = self.views.quarantine("owner")
        self.assertEqual(capped["summary"]["unresolved_count"], 206)
        self.assertEqual(capped["summary"]["total_count"], 208)
        self.assertEqual(len(capped["items"]), qmod.MAX_QUARANTINE_ITEMS)
        self.assertTrue(capped["truncated"])

        # Corrupt DB payload fails closed with 500 budget_analytics_data_error.
        with self.sessions.begin() as session:
            session.add(db.BudgetUnresolved(
                account_id="acc-excluded", ordinal=99, payload_json="{corrupt", observed_at=9999.0,
            ))
        with self.assertRaises(AnalyticsError) as corrupt:
            self.views.quarantine("owner", account_id="acc-excluded")
        self.assertEqual((corrupt.exception.code, corrupt.exception.status), ("budget_analytics_data_error", 500))


class QuarantineHTTPAPITests(unittest.IsolatedAsyncioTestCase):
    rows = analytics_fixture.BudgetAnalyticsAPITests.rows
    token = analytics_fixture.BudgetAnalyticsAPITests.token
    headers = analytics_fixture.BudgetAnalyticsAPITests.headers
    assert_private_headers = analytics_fixture.BudgetAnalyticsAPITests.assert_private_headers
    get = analytics_fixture.BudgetAnalyticsAPITests.get

    async def asyncSetUp(self):
        await analytics_fixture.BudgetAnalyticsAPITests.asyncSetUp(self)

    async def test_quarantine_http_read_filters_headers_and_isolation(self):
        with self.sessions.begin() as session:
            session.add(db.BudgetUnresolved(
                account_id=self.alice_id, ordinal=0,
                payload_json=_tx_json("http-unres", status="PDNG", booking_date="2026-11-20",
                                      amount="555.55", description="HTTP-SECRET-DESC"),
                observed_at=1500.0,
            ))
        resp = await self.get("quarantine")
        self.assertEqual(resp.status, 200, await resp.text())
        self.assert_private_headers(resp)
        body = await resp.json()
        api_contract.BudgetAnalyticsQuarantine.model_validate(body)
        self.assertEqual(set(body), {"rule_version", "summary", "accounts", "items_cap", "truncated", "items"})
        self.assertEqual(set(body["summary"]),
                         {"uncertain_count", "unresolved_count", "import_unresolved_count", "total_count"})
        for account in body["accounts"]:
            self.assertEqual(set(account), {"account_id", "selected", "uncertain_count", "unresolved_count",
                                            "import_unresolved_count", "total_count"})
        for item in body["items"]:
            self.assertEqual(set(item), {"bucket", "account_id", "selected", "entry_reference", "date", "status",
                                         "reason", "source", "observed_at"})
        self.assertEqual(body["summary"]["unresolved_count"], 1)
        self.assertNotIn("HTTP-SECRET-DESC", json.dumps(body))
        self.assertNotIn("555.55", json.dumps(body))

        # Filtered owned account -> 200; foreign Bob account -> 404; unknown query key -> 400; HEAD -> 405.
        scoped = await self.get("quarantine", params={"account_id": self.alice_id})
        self.assertEqual(scoped.status, 200)
        self.assertEqual((await scoped.json())["summary"]["total_count"], 1)

        foreign = await self.get("quarantine", params={"account_id": self.bob_id})
        self.assertEqual(foreign.status, 404)
        self.assert_private_headers(foreign)

        bad_q = await self.get("quarantine", params={"period": "2026-11"})
        self.assertEqual(bad_q.status, 400)
        self.assert_private_headers(bad_q)

        head = await self.client.head(analytics_fixture.PREFIX + "/quarantine", headers=self.headers())
        self.assertEqual(head.status, 405)


if __name__ == "__main__":
    unittest.main()
