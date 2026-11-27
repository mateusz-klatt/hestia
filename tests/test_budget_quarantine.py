"""Pure quarantine extractor, single-snapshot read views and HTTP API tests (OpenAPI 0.25.0)."""
from __future__ import annotations

import json
import unittest

from hestia import api_contract, db
from hestia.budget import quarantine as qmod
from hestia.budget.analytics_core import AnalyticsError
from hestia.budget.analytics_views import BudgetAnalyticsViews
from hestia.budget.import_file import parse_import
from hestia.budget.normalize import AccountSnapshot, Snapshot, normalize_transaction
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
            {**base, "payload_json": json.dumps({"booking_date": "", "value_date": "2026-11-11"})},
            {**base, "payload_json": json.dumps({"booking_date": ""})},
            {**base, "payload_json": json.dumps({"booking_date": False, "value_date": "2026-11-11"})},
            {**base, "payload_json": json.dumps({"booking_date": "2026-11-10", "value_date": ""})},
            {**base, "payload_json": json.dumps({"booking_date": "2026-11-10", "value_date": "2026-02-30"})},
            {**base, "payload_json": json.dumps({"booking_date": "2026-11-10", "value_date": 123})},
            {**base, "payload_json": json.dumps({"status": "BOGUS"})},
            {**base, "bucket": qmod.BUCKET_IMPORT_UNRESOLVED, "source": "offline_import", "stored_reason": None},
            {**base, "bucket": qmod.BUCKET_IMPORT_UNRESOLVED, "source": "offline_import", "stored_reason": "unknown"},
        )
        for bad in bad_cases:
            with self.subTest(bad=bad), self.assertRaises(qmod.QuarantineDataError):
                qmod.extract_safe_item(**bad)

        # booking_date takes precedence over value_date; value_date is used only when booking_date is absent or None.
        both_dates = qmod.extract_safe_item(
            bucket=qmod.BUCKET_UNRESOLVED, account_id="acc-1", selected=True,
            payload_json=json.dumps({"entry_reference": "d-1", "booking_date": "2026-11-10",
                                     "value_date": "2026-11-11", "status": "BOOK"}),
            source="api", observed_at=100.0,
        )
        self.assertEqual(both_dates["date"], "2026-11-10")
        absent_booking = qmod.extract_safe_item(
            bucket=qmod.BUCKET_UNRESOLVED, account_id="acc-1", selected=True,
            payload_json=json.dumps({"entry_reference": "d-2", "value_date": "2026-11-12", "status": "BOOK"}),
            source="api", observed_at=100.0,
        )
        self.assertEqual(absent_booking["date"], "2026-11-12")


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

        # Corrupt DB payload fails closed with 500 budget_analytics_data_error; duplicate JSON keys fail via extract_safe_item.
        with self.sessions.begin() as session:
            session.add(db.BudgetAccount(
                id="acc-dup", connection_id="bank", owner="owner",
                identification_hash="DUP-HASH", enabled=True, configured=True,
                control_version=1, kind="current", balances_json="[]",
            ))
            session.flush()
            session.add(db.BudgetUnresolved(
                account_id="acc-excluded", ordinal=99, payload_json="{corrupt", observed_at=9999.0,
            ))
            session.add(db.BudgetUnresolved(
                account_id="acc-dup", ordinal=0,
                payload_json='{"entry_reference":"dup","booking_date":"2026-11-10","booking_date":"2026-11-11"}',
                observed_at=9999.0,
            ))
        with self.assertRaises(AnalyticsError) as corrupt:
            self.views.quarantine("owner", account_id="acc-excluded")
        self.assertEqual((corrupt.exception.code, corrupt.exception.status), ("budget_analytics_data_error", 500))
        with self.assertRaises(AnalyticsError) as dup_keys:
            self.views.quarantine("owner", account_id="acc-dup")
        self.assertEqual((dup_keys.exception.code, dup_keys.exception.status), ("budget_analytics_data_error", 500))

    def test_truncation_orders_newest_dates_first_regardless_of_ordinal_within_one_snapshot(self):
        # 230 rows per bucket sharing one timestamp; dates are NOT in ordinal/reference order.
        with self.sessions.begin() as session:
            session.add(db.BudgetAccount(
                id="acc-imp", connection_id="bank", owner="owner",
                identification_hash="IMP-SORT-HASH", enabled=True, configured=True,
                control_version=1, kind="current", balances_json="[]",
            ))
            session.add(db.BudgetAccount(
                id="acc-unc", connection_id="bank", owner="owner",
                identification_hash="UNC-SORT-HASH", enabled=True, configured=True,
                control_version=1, kind="current", balances_json="[]",
            ))
            session.add(db.BudgetImportBatch(
                id="batch-sort", fingerprint="fp-sort", account_id="acc-imp",
                fetched_at=5000.0, imported_at=5010.0, balances_json=None, report_json="{}",
            ))
            session.flush()
            expected_unres_dates = []
            expected_imp_dates = []
            expected_unc_dates = []
            for ordinal in range(230):
                if ordinal < 20:
                    booking_date, value_date, eff_date = None, None, None
                elif ordinal < 130:
                    day_idx = ordinal - 20
                    stamp = f"2026-{1 + day_idx // 28:02d}-{1 + day_idx % 28:02d}"
                    booking_date = None if ordinal % 2 == 0 else stamp
                    value_date = stamp if ordinal % 2 == 0 else None
                    eff_date = stamp
                else:
                    day_idx = ordinal - 130
                    stamp = f"2026-{6 + day_idx // 28:02d}-{1 + day_idx % 28:02d}"
                    booking_date = stamp if ordinal % 3 else None
                    value_date = None if ordinal % 3 else stamp
                    eff_date = stamp
                if eff_date is not None:
                    expected_unres_dates.append(eff_date)
                session.add(db.BudgetUnresolved(
                    account_id=self.account_id, ordinal=ordinal,
                    payload_json=_tx_json(f"ord-{ordinal:03d}", status="PDNG",
                                          booking_date=booking_date, value_date=value_date),
                    observed_at=5000.0,
                ))
                imp_idx = (ordinal * 73) % 230
                imp_date = (None if imp_idx < 20
                            else f"2025-{1 + (imp_idx - 20) // 28:02d}-{1 + (imp_idx - 20) % 28:02d}")
                if imp_date is not None:
                    expected_imp_dates.append(imp_date)
                session.add(db.BudgetImportUnresolved(
                    batch_id="batch-sort", ordinal=ordinal, reason="stale_conflict",
                    payload_json=_tx_json(f"imp-{ordinal:03d}", status="BOOK",
                                          booking_date=imp_date, value_date=None),
                ))
                unc_date = (None if imp_idx < 20
                            else f"2024-{1 + (imp_idx - 20) // 28:02d}-{1 + (imp_idx - 20) % 28:02d}")
                if unc_date is not None:
                    expected_unc_dates.append(unc_date)
                session.add(db.BudgetTransaction(
                    account_id="acc-unc", entry_reference=f"unc-{ordinal:03d}",
                    payload_json=_tx_json(f"unc-{ordinal:03d}", booking_date=unc_date, value_date=None),
                    observed_at=5000.0, uncertain=True, source="api", api_observed_at=5000.0,
                ))

        unres_report = self.views.quarantine("owner", account_id=self.account_id)
        self.assertTrue(unres_report["truncated"])
        self.assertEqual(len(unres_report["items"]), qmod.MAX_QUARANTINE_ITEMS)
        self.assertEqual([item["date"] for item in unres_report["items"]],
                         sorted(expected_unres_dates, reverse=True)[:qmod.MAX_QUARANTINE_ITEMS])
        self.assertEqual(unres_report["items"][0]["entry_reference"], "ord-229")
        self.assertEqual(unres_report["items"][-1]["entry_reference"], "ord-030")

        imp_report = self.views.quarantine("owner", account_id="acc-imp")
        self.assertTrue(imp_report["truncated"])
        self.assertEqual([item["date"] for item in imp_report["items"]],
                         sorted(expected_imp_dates, reverse=True)[:qmod.MAX_QUARANTINE_ITEMS])

        unc_report = self.views.quarantine("owner", account_id="acc-unc")
        self.assertTrue(unc_report["truncated"])
        self.assertEqual([item["date"] for item in unc_report["items"]],
                         sorted(expected_unc_dates, reverse=True)[:qmod.MAX_QUARANTINE_ITEMS])

        # Mixed unfiltered snapshot keeps newest 200 overall (all 2026 unresolved items beat 2025/2024 items).
        mixed = self.views.quarantine("owner")
        self.assertEqual(mixed["summary"]["total_count"], 690)
        self.assertEqual([item["date"] for item in mixed["items"]],
                         sorted(expected_unres_dates, reverse=True)[:qmod.MAX_QUARANTINE_ITEMS])

    def test_whole_scope_integrity_probe_fails_closed_beyond_200_rows(self):
        # 210 valid newer rows + 1 invalid row ranked beyond item 200 in each bucket -> 500 even unfiltered.
        with self.sessions.begin() as session:
            session.add(db.BudgetAccount(
                id="acc-clean", connection_id="bank", owner="owner",
                identification_hash="CLEAN-HASH", enabled=True, configured=True,
                control_version=1, kind="current", balances_json="[]",
            ))
            session.add(db.BudgetAccount(
                id="other-acc", connection_id="other-bank", owner="other",
                identification_hash="OTHER-HASH", enabled=True, configured=True,
                control_version=1, kind="current", balances_json="[]",
            ))
            session.add(db.BudgetImportBatch(
                id="batch-many", fingerprint="fp-many", account_id=self.account_id,
                fetched_at=4000.0, imported_at=4010.0, balances_json=None, report_json="{}",
            ))
            # Foreign owner's invalid row must never affect "owner".
            session.add(db.BudgetImportBatch(
                id="batch-other", fingerprint="fp-other", account_id="other-acc",
                fetched_at=4000.0, imported_at=4010.0, balances_json=None, report_json="{}",
            ))
            session.flush()
            session.add(db.BudgetImportUnresolved(
                batch_id="batch-other", ordinal=0, reason="bogus_foreign_reason",
                payload_json=_tx_json("foreign-bad", booking_date="2026-11-20"),
            ))
            # 210 valid import rows on self.account_id (newer date 2026-11-20).
            for ordinal in range(210):
                session.add(db.BudgetImportUnresolved(
                    batch_id="batch-many", ordinal=ordinal, reason="non_canonical",
                    payload_json=_tx_json(f"valid-imp-{ordinal}", booking_date="2026-11-20"),
                ))
            # Bad row #211 has older date so SQL LIMIT 200 would skip it without the whole-scope probe.
            session.add(db.BudgetImportUnresolved(
                batch_id="batch-many", ordinal=210, reason="bogus_reason",
                payload_json=_tx_json("bad-imp-211", booking_date="2020-01-01"),
            ))
            # Clean account has 1 valid row; scoping to acc-clean must succeed (200).
            session.add(db.BudgetUnresolved(
                account_id="acc-clean", ordinal=0,
                payload_json=_tx_json("clean-ref", status="PDNG", booking_date="2026-11-20"),
                observed_at=100.0,
            ))

        clean = self.views.quarantine("owner", account_id="acc-clean")
        self.assertEqual(clean["summary"]["total_count"], 1)

        with self.assertRaises(AnalyticsError) as unfiltered:
            self.views.quarantine("owner")
        self.assertEqual((unfiltered.exception.code, unfiltered.exception.status), ("budget_analytics_data_error", 500))
        with self.assertRaises(AnalyticsError) as filtered:
            self.views.quarantine("owner", account_id=self.account_id)
        self.assertEqual((filtered.exception.code, filtered.exception.status), ("budget_analytics_data_error", 500))

        # Repair import reason and verify corrupt payload beyond 200 in unresolved & invalid source in uncertain also 500.
        with self.sessions.begin() as session:
            bad_imp = session.get(db.BudgetImportUnresolved, ("batch-many", 210))
            bad_imp.reason = "non_canonical"
            for ordinal in range(210):
                session.add(db.BudgetUnresolved(
                    account_id=self.account_id, ordinal=ordinal,
                    payload_json=_tx_json(f"valid-unres-{ordinal}", status="PDNG", booking_date="2026-11-20"),
                    observed_at=5000.0,
                ))
            session.add(db.BudgetUnresolved(
                account_id=self.account_id, ordinal=210,
                payload_json=_tx_json("bad-unres-211", status="PDNG", booking_date=""),
                observed_at=1.0,
            ))
        with self.assertRaises(AnalyticsError) as bad_unres:
            self.views.quarantine("owner")
        self.assertEqual((bad_unres.exception.code, bad_unres.exception.status), ("budget_analytics_data_error", 500))

        with self.sessions.begin() as session:
            session.get(db.BudgetUnresolved, (self.account_id, 210)).payload_json = _tx_json(
                "fixed-unres-211", status="PDNG", booking_date="2020-01-01",
            )
            for idx in range(210):
                session.add(db.BudgetTransaction(
                    account_id=self.account_id, entry_reference=f"unc-ok-{idx:03d}",
                    payload_json=_tx_json(f"unc-ok-{idx:03d}", booking_date="2026-11-20"),
                    observed_at=5000.0, uncertain=True, source="api", api_observed_at=5000.0,
                ))
            session.add(db.BudgetTransaction(
                account_id=self.account_id, entry_reference="unc-bad-source",
                payload_json=_tx_json("unc-bad-source", booking_date="2020-01-01"),
                observed_at=1.0, uncertain=True, source="bank_csv", api_observed_at=1.0,
            ))
        with self.assertRaises(AnalyticsError) as bad_unc:
            self.views.quarantine("owner")
        self.assertEqual((bad_unc.exception.code, bad_unc.exception.status), ("budget_analytics_data_error", 500))

    def test_provider_nul_entry_reference_normalizes_to_unresolved_without_quarantine_500(self):
        provider_tx = {
            "entry_reference": "\x00short-ref",
            "transaction_amount": {"amount": "42.50", "currency": "PLN"},
            "credit_debit_indicator": "DBIT",
            "status": "BOOK",
            "booking_date": "2026-11-18",
            "value_date": None,
            "transaction_date": None,
            "remittance_information": ["Desc\x00with\x01c0\x7f\t\n\r"],
        }
        normalized = normalize_transaction(provider_tx)
        self.assertIsNone(normalized["entry_reference"])
        self.assertEqual(normalized["remittance_information"], ["Desc\ufffdwith\ufffdc0\ufffd\t\n\r"])
        observations = (
            AccountSnapshot("a", (), (normalized,)),
            AccountSnapshot("b", None, None),
            AccountSnapshot("c", None, None),
            AccountSnapshot("d", None, None),
        )
        self.store.commit_snapshot("bank", Snapshot(self.now + 1000, observations, "2026-11-01", "2026-11-30"))

        report = self.views.quarantine("owner", account_id=self.account_id)
        api_contract.BudgetAnalyticsQuarantine.model_validate(report)
        self.assertEqual(report["summary"], {
            "uncertain_count": 0, "unresolved_count": 1, "import_unresolved_count": 0, "total_count": 1,
        })
        self.assertEqual(len(report["items"]), 1)
        item = report["items"][0]
        self.assertEqual(item["bucket"], qmod.BUCKET_UNRESOLVED)
        self.assertIsNone(item["entry_reference"])
        self.assertEqual(item["reason"], "missing_reference")
        self.assertEqual(item["date"], "2026-11-18")
        self.assertEqual(item["status"], "BOOK")

        # Offline import with leading NUL entry_reference also stays unresolved without failing quarantine.
        imported = parse_import({
            "version": 1,
            "connection_id": "bank",
            "aspsp": {"name": "Synthetic Bank", "country": "PL"},
            "identification_hash": "a",
            "fetched_at": "1970-01-02T00:00:00Z",
            "complete": True,
            "transactions": {"transactions": [provider_tx], "continuation_key": None},
        }, self.config, now=self.now)
        import_report = self.store.import_snapshot(self.config, imported)
        self.assertEqual(import_report["unresolved"], 1)

        combined = self.views.quarantine("owner", account_id=self.account_id)
        api_contract.BudgetAnalyticsQuarantine.model_validate(combined)
        self.assertEqual(combined["summary"], {
            "uncertain_count": 0, "unresolved_count": 1, "import_unresolved_count": 1, "total_count": 2,
        })
        self.assertTrue(all(row["entry_reference"] is None for row in combined["items"]))
        self.assertEqual({(row["bucket"], row["reason"]) for row in combined["items"]}, {
            (qmod.BUCKET_UNRESOLVED, "missing_reference"),
            (qmod.BUCKET_IMPORT_UNRESOLVED, "non_canonical"),
        })


class QuarantineHTTPAPITests(unittest.IsolatedAsyncioTestCase):
    rows = analytics_fixture.BudgetAnalyticsAPITests.rows
    token = analytics_fixture.BudgetAnalyticsAPITests.token
    headers = analytics_fixture.BudgetAnalyticsAPITests.headers
    assert_private_headers = analytics_fixture.BudgetAnalyticsAPITests.assert_private_headers
    get = analytics_fixture.BudgetAnalyticsAPITests.get
    post = analytics_fixture.BudgetAnalyticsAPITests.post

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

    async def test_quarantine_http_invalid_import_reason_fails_closed_within_and_beyond_200(self):
        with self.sessions.begin() as session:
            session.add(db.BudgetImportBatch(
                id="http-batch", fingerprint="fp-http", account_id=self.alice_id,
                fetched_at=3000.0, imported_at=3010.0, balances_json=None, report_json="{}",
            ))
            session.flush()
            session.add(db.BudgetImportUnresolved(
                batch_id="http-batch", ordinal=0, reason="bogus_reason",
                payload_json=_tx_json("http-bad-0", booking_date="2020-01-01"),
            ))
        # Invalid reason within the first 200 rows -> 500 budget_analytics_data_error + private headers.
        resp_small = await self.get("quarantine")
        self.assertEqual(resp_small.status, 500)
        self.assert_private_headers(resp_small)
        self.assertEqual(await resp_small.json(), {"ok": False, "error": "budget_analytics_data_error"})

        # Add 205 newer valid rows so ordinal 0 sits beyond item 200 -> still 500 + private headers.
        with self.sessions.begin() as session:
            for ordinal in range(1, 206):
                session.add(db.BudgetImportUnresolved(
                    batch_id="http-batch", ordinal=ordinal, reason="non_canonical",
                    payload_json=_tx_json(f"http-valid-{ordinal}", booking_date="2026-11-20"),
                ))
        resp_beyond = await self.get("quarantine")
        self.assertEqual(resp_beyond.status, 500)
        self.assert_private_headers(resp_beyond)
        self.assertEqual(await resp_beyond.json(), {"ok": False, "error": "budget_analytics_data_error"})
        resp_scoped = await self.get("quarantine", params={"account_id": self.alice_id})
        self.assertEqual(resp_scoped.status, 500)
        self.assert_private_headers(resp_scoped)
        self.assertEqual(await resp_scoped.json(), {"ok": False, "error": "budget_analytics_data_error"})

    async def test_owner_can_dismiss_one_import_item_then_the_rest_of_its_batch(self):
        original_report = '{"unresolved":201}'
        with self.sessions.begin() as session:
            for batch_id, account_id in (("alice-import", self.alice_id), ("alice-other", self.alice_id),
                                         ("bob-import", self.bob_id)):
                session.add(db.BudgetImportBatch(
                    id=batch_id, fingerprint=batch_id, account_id=account_id, fetched_at=3000,
                    imported_at=3010, balances_json=None, report_json=original_report,
                ))
            session.flush()
            for batch_id, size in (("alice-import", 201), ("alice-other", 1), ("bob-import", 1)):
                session.add_all(db.BudgetImportUnresolved(
                    batch_id=batch_id, ordinal=i, reason="api_preserved",
                    payload_json=_tx_json("same-reference", amount="25.12", description="Synthetic offline description"),
                ) for i in range(size))
            session.add(db.BudgetUnresolved(
                account_id=self.alice_id, ordinal=0, payload_json=_tx_json("api-unresolved", status="PDNG"),
                observed_at=3000,
            ))
        for version in (None, "0.61.0", "0.62.0"):
            headers = self.headers() if version is None else self.headers() | {"X-Hestia-Contract-Version": version}
            response = await self.get("quarantine", headers=headers)
            self.assertEqual(response.status, 200)
            body = await response.json()
            self.assertTrue(body["items"])
            self.assertTrue(all({"import_key", "unresolved_count"}.isdisjoint(item) for item in body["items"]))

        version = {"X-Hestia-Contract-Version": "0.63.0"}
        headers = self.headers(origin=True) | version
        read = await self.get("quarantine", headers=headers)
        self.assertEqual(read.status, 200)
        report = await read.json()
        for later_version in ("0.64.0", "0.65.0"):
            with self.subTest(version=later_version):
                current_headers = headers | {"X-Hestia-Contract-Version": later_version}
                current = await self.get("quarantine", headers=current_headers)
                self.assertEqual(current.status, 200)
                self.assertEqual(await current.json(), report)
        self.assertTrue(report["truncated"])
        self.assertEqual(report["summary"]["import_unresolved_count"], 202)
        api_contract.BudgetAnalyticsQuarantine.model_validate(report)
        self.assertNotIn("Synthetic offline description", json.dumps(report))
        self.assertNotIn("25.12", json.dumps(report))
        api_item = next(item for item in report["items"] if item["bucket"] == "unresolved")
        self.assertTrue({"import_key", "unresolved_count"}.isdisjoint(api_item))
        import_items = [item for item in report["items"] if item["bucket"] == "import_unresolved"]
        self.assertLess(len(import_items), 201)
        keys = [item["import_key"] for item in import_items]
        self.assertEqual({(item["import_key"]["batch_id"], item["unresolved_count"]) for item in import_items},
                         {("alice-import", 201)})
        self.assertEqual(len(keys), len({(key["batch_id"], key["ordinal"]) for key in keys}))
        key = next(key for key in keys if key["batch_id"] == "alice-import")

        for operation, payload in (("quarantine-dismiss", key), ("quarantine-dismiss-batch", {"batch_id": "alice-import"})):
            with self.subTest(operation=operation):
                reply = await self.post(operation, payload, headers=self.headers("bob", origin=True) | version)
                self.assertEqual(reply.status, 404)
                self.assert_private_headers(reply)
                self.assertEqual((await self.post(operation, {**payload, "batch_id": "missing"}, headers=headers)).status, 404)
                self.assertEqual((await self.post(operation, payload, headers=self.headers() | version)).status, 403)
                bearer = self.headers(origin=True, bearer=True) | version
                self.assertEqual((await self.post(operation, payload, headers=bearer)).status, 403)
                self.assertEqual((await self.post(operation, payload)).status, 404)
                legacy = self.headers(origin=True) | {"X-Hestia-Contract-Version": "0.61.0"}
                self.assertEqual((await self.post(operation, payload, headers=legacy)).status, 404)
        for bad_key in ({"batch_id": "alice-import"}, {**key, "ordinal": -1}, {**key, "ordinal": True},
                        {**key, "ordinal": 2**53}):
            self.assertEqual((await self.post("quarantine-dismiss", bad_key, headers=headers)).status, 400)
        bad_version = self.headers(origin=True) | {"X-Hestia-Contract-Version": "latest"}
        self.assertEqual((await self.post("quarantine-dismiss", key, headers=bad_version)).status, 400)
        self.assertEqual(self.rows(db.BudgetAnalyticsAudit), [])

        async def dismiss(operation, payload, expected_count):
            response = await self.post(operation, payload, headers=headers)
            self.assertEqual(response.status, 200, await response.text())
            self.assert_private_headers(response)
            body = await response.json()
            api_contract.BudgetAnalyticsQuarantineDismissResult.model_validate(body)
            self.assertEqual(body, {"ok": True, "dismissal": {"dismissed_count": expected_count}})

        await dismiss("quarantine-dismiss", key, 1)
        await dismiss("quarantine-dismiss", key, 0)
        read = await self.get("quarantine", headers=headers)
        report = await read.json()
        self.assertEqual(report["summary"]["import_unresolved_count"], 201)
        self.assertNotIn(key, [item.get("import_key") for item in report["items"]])
        self.assertEqual({item["unresolved_count"] for item in report["items"] if item["bucket"] == "import_unresolved"},
                         {200})
        await dismiss("quarantine-dismiss-batch", {"batch_id": key["batch_id"]}, 200)
        await dismiss("quarantine-dismiss-batch", {"batch_id": key["batch_id"]}, 0)
        read = await self.get("quarantine", headers=headers)
        report = await read.json()
        self.assertEqual(report["summary"]["import_unresolved_count"], 1)
        self.assertEqual(report["summary"]["unresolved_count"], 1)
        item = next(item for item in report["items"] if item["bucket"] == "import_unresolved")
        self.assertEqual((item["import_key"]["batch_id"], item["unresolved_count"]), ("alice-other", 1))
        empty_scope = await self.get("quarantine", params={"account_id": self.credit_id}, headers=headers)
        self.assertEqual(empty_scope.status, 200)
        self.assertEqual((await empty_scope.json())["items"], [])
        self.assertEqual({row.batch_id for row in self.rows(db.BudgetImportUnresolved)}, {"alice-other", "bob-import"})
        self.assertEqual({row.report_json for row in self.rows(db.BudgetImportBatch)}, {original_report})
        audits = sorted(self.rows(db.BudgetAnalyticsAudit), key=lambda row: row.id)
        self.assertEqual(len(audits), 2)
        for row, ordinal, count in zip(audits, (key["ordinal"], None), (1, 200), strict=True):
            self.assertEqual((row.owner, row.subject_type, row.subject_id, row.action),
                             ("alice", "import_unresolved", "alice-import", "dismiss"))
            self.assertEqual(json.loads(row.before_json), {"ordinal": ordinal, "count": count})
            self.assertEqual(json.loads(row.after_json), {"ordinal": ordinal, "count": 0})


if __name__ == "__main__":
    unittest.main()
