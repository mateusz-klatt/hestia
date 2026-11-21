"""Migration 0018, atomic store/CAS, views and HTTP API tests for private mortgage schedules."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import uuid

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from hestia import api_contract, db, web
from hestia.budget import analytics_api
from hestia.budget.analytics_core import AnalyticsError
from hestia.budget.analytics_store import BudgetAnalyticsStore
from hestia.budget.analytics_views import BudgetAnalyticsViews
from tests import test_budget_analytics_api as analytics_fixture
from tests.test_budget_analytics_store import AnalyticsCase

PREFIX = analytics_api.PREFIX


def _schedule_payload(**overrides):
    data = {
        "schedule_id": None,
        "label": "Synthetic home mortgage",
        "amount": "02450.500",
        "currency": "PLN",
        "due_day": 15,
        "start_date": "2026-01-01",
        "end_date": "2026-12-31",
        "remind_days_before": 5,
        "destination_kind": "owner_inbox",
        "enabled": True,
        "expected_version": 0,
    }
    data.update(overrides)
    return data


class MortgageMigration0018Tests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(self.directory.name) / "m0018.db"

    def test_migration_0018_schema_drift_constraints_composite_fk_and_roundtrip(self):
        engine = db.make_engine(self.path)
        try:
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "0017")
            with engine.begin() as conn:
                conn.exec_driver_sql("INSERT INTO users (username, password_hash, role) VALUES ('alice', 'h', 'admin')")
                conn.exec_driver_sql("INSERT INTO users (username, password_hash, role) VALUES ('bob', 'h', 'admin')")
                conn.exec_driver_sql(
                    "INSERT INTO budget_analytics_audit "
                    "(owner, subject_type, subject_id, action, before_json, after_json, changed_at) "
                    "VALUES ('alice', 'mortgage_schedule', 's-1', 'create', 'null', '{\"version\":1}', 100.0)")
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "0018")
            tables = set(inspect(engine).get_table_names())
            self.assertTrue({"budget_mortgage_schedules", "budget_mortgage_payments"} <= tables)

            # Zero ORM vs migration drift at head.
            with engine.connect() as conn:
                ctx = MigrationContext.configure(conn)
                self.assertEqual(compare_metadata(ctx, db.Base.metadata), [])

            # Valid schedule + payment insert.
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "INSERT INTO budget_mortgage_schedules "
                    "(id, owner, label, amount, currency, due_day, start_date, end_date, remind_days_before, "
                    "destination_kind, enabled, version, created_at, updated_at) "
                    "VALUES ('s-1', 'alice', 'Loan', '2500', 'PLN', 15, '2026-01-01', '2026-12-31', 5, "
                    "'owner_inbox', 1, 1, 100.0, 100.0)")
                conn.exec_driver_sql(
                    "INSERT INTO budget_mortgage_payments (schedule_id, month, owner, marked_at) "
                    "VALUES ('s-1', '2026-01', 'alice', 100.0)")

            # All schedule CHECK constraints + FK checks.
            bad_schedules = (
                ("empty-label", "alice", "", "100", "PLN", 15, "2026-01-01", None, 5, "owner_inbox", 1, 1),
                ("long-label", "alice", "x" * 257, "100", "PLN", 15, "2026-01-01", None, 5, "owner_inbox", 1, 1),
                ("empty-amt", "alice", "Loan", "", "PLN", 15, "2026-01-01", None, 5, "owner_inbox", 1, 1),
                ("long-amt", "alice", "Loan", "1" * 81, "PLN", 15, "2026-01-01", None, 5, "owner_inbox", 1, 1),
                ("bad-cur-low", "alice", "Loan", "100", "pln", 15, "2026-01-01", None, 5, "owner_inbox", 1, 1),
                ("bad-cur-len", "alice", "Loan", "100", "PL", 15, "2026-01-01", None, 5, "owner_inbox", 1, 1),
                ("due-0", "alice", "Loan", "100", "PLN", 0, "2026-01-01", None, 5, "owner_inbox", 1, 1),
                ("due-32", "alice", "Loan", "100", "PLN", 32, "2026-01-01", None, 5, "owner_inbox", 1, 1),
                ("bad-start", "alice", "Loan", "100", "PLN", 15, "2026-1-1", None, 5, "owner_inbox", 1, 1),
                ("bad-end", "alice", "Loan", "100", "PLN", 15, "2026-01-01", "2026-12-3", 5, "owner_inbox", 1, 1),
                ("rev-end", "alice", "Loan", "100", "PLN", 15, "2026-06-01", "2026-05-31", 5, "owner_inbox", 1, 1),
                ("rem-neg", "alice", "Loan", "100", "PLN", 15, "2026-01-01", None, -1, "owner_inbox", 1, 1),
                ("rem-32", "alice", "Loan", "100", "PLN", 15, "2026-01-01", None, 32, "owner_inbox", 1, 1),
                ("shared-cal", "alice", "Loan", "100", "PLN", 15, "2026-01-01", None, 5, "shared_calendar", 1, 1),
                ("en-2", "alice", "Loan", "100", "PLN", 15, "2026-01-01", None, 5, "owner_inbox", 2, 1),
                ("ver-0", "alice", "Loan", "100", "PLN", 15, "2026-01-01", None, 5, "owner_inbox", 1, 0),
                ("bad-owner", "ghost", "Loan", "100", "PLN", 15, "2026-01-01", None, 5, "owner_inbox", 1, 1),
            )
            for row in bad_schedules:
                with self.subTest(bad_schedule=row[0]), self.assertRaises(IntegrityError):
                    with engine.begin() as conn:
                        conn.exec_driver_sql(
                            "INSERT INTO budget_mortgage_schedules "
                            "(id, owner, label, amount, currency, due_day, start_date, end_date, remind_days_before, "
                            "destination_kind, enabled, version, created_at, updated_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 100.0, 100.0)", row)

            # Composite FK blocks cross-owner payment insertion; CHECK blocks invalid month formats.
            bad_payments = (
                ("s-1", "2026-02", "bob"),      # wrong owner for s-1
                ("s-missing", "2026-02", "alice"),
                ("s-1", "2026-00", "alice"),
                ("s-1", "2026-13", "alice"),
                ("s-1", "2026-2", "alice"),
                ("s-1", "abcd-01", "alice"),
            )
            for prow in bad_payments:
                with self.subTest(bad_payment=prow), self.assertRaises(IntegrityError):
                    with engine.begin() as conn:
                        conn.exec_driver_sql(
                            "INSERT INTO budget_mortgage_payments (schedule_id, month, owner, marked_at) "
                            "VALUES (?, ?, ?, 100.0)", prow)

            # Restrictive FKs (no CASCADE): deleting referenced schedule or user is blocked until dependents removed.
            with self.assertRaises(IntegrityError):
                with engine.begin() as conn:
                    conn.exec_driver_sql("DELETE FROM budget_mortgage_schedules WHERE id='s-1'")
            with self.assertRaises(IntegrityError):
                with engine.begin() as conn:
                    conn.exec_driver_sql("DELETE FROM users WHERE username='alice'")
            with engine.begin() as conn:
                conn.exec_driver_sql("DELETE FROM budget_mortgage_payments WHERE schedule_id='s-1'")
                conn.exec_driver_sql("DELETE FROM budget_mortgage_schedules WHERE id='s-1'")

            # Full 0017 -> 0018 -> 0017 -> 0018 roundtrip drops payments before schedules and keeps audit.
            with engine.begin() as conn:
                command.downgrade(db._alembic_config(conn), "0017")
            after_down = set(inspect(engine).get_table_names())
            self.assertNotIn("budget_mortgage_payments", after_down)
            self.assertNotIn("budget_mortgage_schedules", after_down)
            with engine.connect() as conn:
                self.assertEqual(conn.exec_driver_sql("SELECT COUNT(*) FROM budget_analytics_audit").scalar(), 1)
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "0018")
            self.assertTrue({"budget_mortgage_schedules", "budget_mortgage_payments"}
                            <= set(inspect(engine).get_table_names()))
            with self.assertRaises(IntegrityError):
                with engine.begin() as conn:
                    conn.exec_driver_sql(
                        "INSERT INTO budget_mortgage_schedules "
                        "(id, owner, label, amount, currency, due_day, start_date, end_date, remind_days_before, "
                        "destination_kind, enabled, version, created_at, updated_at) "
                        "VALUES ('s-bad', 'alice', 'Loan', '100', 'PLN', 15, '2026-01-01', NULL, 5, "
                        "'shared_calendar', 1, 1, 100.0, 100.0)")
        finally:
            engine.dispose()


class MortgageStore0018Tests(AnalyticsCase):
    def test_schedule_and_payment_crud_cas_sanitized_audit_and_edited_dates(self):
        created = self.store.save_mortgage_schedule(
            "owner", **_schedule_payload(label="  SECRET-LOAN-42  ", amount="09876.540",
                                         start_date="2026-01-20", end_date="2026-03-10", due_day=15))
        self.assertEqual((created["label"], created["amount"], created["version"], created["changed"],
                          created["paid_months"]),
                         ("SECRET-LOAN-42", "9876.54", 1, True, []))

        # Strict CAS checked BEFORE no-op on both create and update.
        with self.assertRaises(AnalyticsError) as bad_create:
            self.store.save_mortgage_schedule("owner", **_schedule_payload(expected_version=1))
        self.assertEqual((bad_create.exception.code, bad_create.exception.status), ("budget_analytics_conflict", 409))
        for bad_ver in (0, 2):
            with self.assertRaises(AnalyticsError) as bad_upd:
                self.store.save_mortgage_schedule(
                    "owner", **_schedule_payload(schedule_id=created["id"], label="SECRET-LOAN-42",
                                                 amount="9876.54", start_date="2026-01-20", end_date="2026-03-10",
                                                 due_day=15, expected_version=bad_ver))
            self.assertEqual((bad_upd.exception.code, bad_upd.exception.status), ("budget_analytics_conflict", 409))

        # Exact no-op leaves version, updated_at and audit unchanged.
        self.now += 60
        noop = self.store.save_mortgage_schedule(
            "owner", **_schedule_payload(schedule_id=created["id"], label="SECRET-LOAN-42",
                                         amount="09876.5400", start_date="2026-01-20", end_date="2026-03-10",
                                         due_day=15, expected_version=1))
        self.assertEqual((noop["changed"], noop["version"], noop["updated_at"]), (False, 1, created["updated_at"]))
        self.assertEqual(len(self.rows(db.BudgetAnalyticsAudit)), 1)

        # Installment eligibility on payment mark (due_day=15 in [2026-01-20, 2026-03-10] -> only 2026-02 qualifies).
        for ineligible in ("2026-01", "2026-03"):
            with self.assertRaises(AnalyticsError) as exc:
                self.store.set_mortgage_payment("owner", schedule_id=created["id"], month=ineligible,
                                                paid=True, expected_version=1)
            self.assertEqual(exc.exception.status, 400)

        # Mark qualifying month 2026-02 paid -> bumps schedule.version to 2.
        paid = self.store.set_mortgage_payment("owner", schedule_id=created["id"], month="2026-02",
                                               paid=True, expected_version=1)
        self.assertEqual((paid["changed"], paid["version"], paid["paid"], paid["paid_months"]),
                         (True, 2, True, ["2026-02"]))

        # Repeat mark paid at current version -> no-op, version stays 2, no extra audit.
        audit_count = len(self.rows(db.BudgetAnalyticsAudit))
        paid_noop = self.store.set_mortgage_payment("owner", schedule_id=created["id"], month="2026-02",
                                                    paid=True, expected_version=2)
        self.assertEqual((paid_noop["changed"], paid_noop["version"]), (False, 2))
        self.assertEqual(len(self.rows(db.BudgetAnalyticsAudit)), audit_count)

        # Stale version on payment set -> 409 BEFORE no-op.
        with self.assertRaises(AnalyticsError) as stale_pay:
            self.store.set_mortgage_payment("owner", schedule_id=created["id"], month="2026-02",
                                            paid=True, expected_version=1)
        self.assertEqual((stale_pay.exception.code, stale_pay.exception.status), ("budget_analytics_conflict", 409))

        # Narrowing schedule dates and disabling schedule NEVER deletes existing payment marks!
        narrowed = self.store.save_mortgage_schedule(
            "owner", **_schedule_payload(schedule_id=created["id"], label="SECRET-LOAN-42",
                                         amount="9876.54", start_date="2026-04-01", end_date="2026-12-31",
                                         due_day=15, enabled=False, expected_version=2))
        self.assertEqual((narrowed["changed"], narrowed["version"], narrowed["enabled"], narrowed["paid_months"]),
                         (True, 3, False, ["2026-02"]))
        self.assertEqual(len(self.rows(db.BudgetMortgagePayment)), 1)

        # Unmarking a preserved historical payment mark outside the edited date window still succeeds.
        unmarked = self.store.set_mortgage_payment("owner", schedule_id=created["id"], month="2026-02",
                                                   paid=False, expected_version=3)
        self.assertEqual((unmarked["changed"], unmarked["version"], unmarked["paid"], unmarked["paid_months"]),
                         (True, 4, False, []))
        self.assertEqual(self.rows(db.BudgetMortgagePayment), [])

        # Foreign owner / unknown schedule -> 404.
        for bad_owner, bad_id in (("other", created["id"]), ("owner", "missing-schedule")):
            with self.assertRaises(AnalyticsError) as nf:
                self.store.save_mortgage_schedule(bad_owner, **_schedule_payload(schedule_id=bad_id, expected_version=4))
            self.assertEqual((nf.exception.code, nf.exception.status), ("budget_analytics_not_found", 404))
            with self.assertRaises(AnalyticsError) as nf2:
                self.store.set_mortgage_payment(bad_owner, schedule_id=bad_id, month="2026-05",
                                                paid=True, expected_version=4)
            self.assertEqual((nf2.exception.code, nf2.exception.status), ("budget_analytics_not_found", 404))

        # Condition 4: Sanitized allowlist audit never retains label, amount or PII; shared Audit stays empty.
        self.assertEqual(self.rows(db.Audit), [])
        for audit_row in self.rows(db.BudgetAnalyticsAudit):
            combined = audit_row.before_json + audit_row.after_json
            self.assertNotIn("SECRET-LOAN-42", combined)
            self.assertNotIn("9876.54", combined)
            self.assertNotIn("destination_target", combined)

    def test_validation_caps_rollback_and_concurrent_cas_races(self):
        bad_schedules = (
            {"schedule_id": " "}, {"schedule_id": "x" * 129}, {"label": " "}, {"label": "x" * 257},
            {"amount": "0"}, {"amount": "-5"}, {"amount": 2500}, {"currency": "pln"}, {"due_day": True},
            {"due_day": 0}, {"due_day": 32}, {"start_date": "2026-02-30"}, {"end_date": "2026-02-30"},
            {"start_date": "2026-06-01", "end_date": "2026-05-31"}, {"remind_days_before": True},
            {"remind_days_before": -1}, {"remind_days_before": 32}, {"destination_kind": "shared_calendar"},
            {"enabled": 1}, {"expected_version": True}, {"expected_version": -1},
        )
        for bad in bad_schedules:
            with self.subTest(bad_schedule=bad), self.assertRaises(AnalyticsError) as exc:
                self.store.save_mortgage_schedule("owner", **_schedule_payload(**bad))
            self.assertEqual(exc.exception.status, 400)

        bad_payments = (
            {"schedule_id": " "}, {"schedule_id": "x" * 129}, {"month": "2026-13"}, {"month": "2026"},
            {"paid": 1}, {"expected_version": True}, {"expected_version": -1},
        )
        for bad in bad_payments:
            values = {"schedule_id": "s-1", "month": "2026-01", "paid": True, "expected_version": 1, **bad}
            with self.subTest(bad_payment=bad), self.assertRaises(AnalyticsError) as exc:
                self.store.set_mortgage_payment("owner", **values)
            self.assertEqual(exc.exception.status, 400)

        # Rollback on audit failure leaves no saved schedule.
        with mock.patch.object(self.store, "_analytics_audit", side_effect=RuntimeError("synthetic-disk-error")):
            with self.assertRaises(RuntimeError):
                self.store.save_mortgage_schedule("owner", **_schedule_payload())
        self.assertEqual(self.rows(db.BudgetMortgageSchedule), [])

        # Schedule cap 100 counts disabled rows on create only; update/disable succeeds at cap.
        first = self.store.save_mortgage_schedule("owner", **_schedule_payload(end_date=None))
        with self.sessions.begin() as session:
            for index in range(99):
                session.add(db.BudgetMortgageSchedule(
                    id=str(uuid.uuid4()), owner="owner", label=f"S-{index}", amount="100", currency="PLN",
                    due_day=10, start_date="2026-01-01", end_date=None, remind_days_before=3,
                    destination_kind="owner_push", enabled=bool(index % 2), version=1,
                    created_at=self.now, updated_at=self.now,
                ))
        with self.assertRaises(AnalyticsError) as cap:
            self.store.save_mortgage_schedule("owner", **_schedule_payload())
        self.assertEqual((cap.exception.code, cap.exception.status), ("budget_analytics_limit", 413))
        updated = self.store.save_mortgage_schedule(
            "owner", **_schedule_payload(schedule_id=first["id"], end_date=None, enabled=False, expected_version=1))
        self.assertEqual(updated["version"], 2)

        # Payment cap 1200 blocks 1201st mark (413), while unmarking still succeeds at cap!
        with self.sessions.begin() as session:
            for index in range(1200):
                ym = f"{2026 + index // 12:04d}-{(index % 12) + 1:02d}"
                session.add(db.BudgetMortgagePayment(
                    schedule_id=first["id"], month=ym, owner="owner", marked_at=self.now,
                ))
        with self.assertRaises(AnalyticsError) as pay_cap:
            self.store.set_mortgage_payment("owner", schedule_id=first["id"], month="2126-01",
                                            paid=True, expected_version=2)
        self.assertEqual((pay_cap.exception.code, pay_cap.exception.status), ("budget_analytics_limit", 413))
        unmarked = self.store.set_mortgage_payment("owner", schedule_id=first["id"], month="2026-01",
                                                   paid=False, expected_version=2)
        self.assertEqual((unmarked["changed"], unmarked["version"]), (True, 3))

        # Concurrent two-writer CAS race: schedule update vs payment mark at expected_version=3.
        barrier = threading.Barrier(2)
        outcomes = []
        lock = threading.Lock()

        def do_schedule_update():
            writer = BudgetAnalyticsStore(self.sessions, clock=lambda: self.now, cursor_secret=b"synthetic-only")
            barrier.wait(timeout=5)
            try:
                res = writer.save_mortgage_schedule(
                    "owner", **_schedule_payload(schedule_id=first["id"], end_date=None, enabled=True,
                                                 expected_version=3))
                out = ("ok", "schedule", res["version"])
            except AnalyticsError as exc:
                out = ("err", exc.code, exc.status)
            with lock:
                outcomes.append(out)

        def do_payment_mark():
            writer = BudgetAnalyticsStore(self.sessions, clock=lambda: self.now, cursor_secret=b"synthetic-only")
            barrier.wait(timeout=5)
            try:
                res = writer.set_mortgage_payment("owner", schedule_id=first["id"], month="2026-01",
                                                  paid=True, expected_version=3)
                out = ("ok", "payment", res["version"])
            except AnalyticsError as exc:
                out = ("err", exc.code, exc.status)
            with lock:
                outcomes.append(out)

        threads = [threading.Thread(target=do_schedule_update), threading.Thread(target=do_payment_mark)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len([o for o in outcomes if o[0] == "ok"]), 1)
        self.assertEqual([o for o in outcomes if o[0] == "err"], [("err", "budget_analytics_conflict", 409)])


class MortgageViewsAndAPI0018Tests(unittest.IsolatedAsyncioTestCase):
    rows = analytics_fixture.BudgetAnalyticsAPITests.rows
    token = analytics_fixture.BudgetAnalyticsAPITests.token
    headers = analytics_fixture.BudgetAnalyticsAPITests.headers
    assert_private_headers = analytics_fixture.BudgetAnalyticsAPITests.assert_private_headers
    get = analytics_fixture.BudgetAnalyticsAPITests.get
    post = analytics_fixture.BudgetAnalyticsAPITests.post

    async def asyncSetUp(self):
        await analytics_fixture.BudgetAnalyticsAPITests.asyncSetUp(self)

    async def test_mortgages_views_server_utc_clock_digest_isolation_and_http_roundtrip(self):
        # 1. Server UTC clock read once, schedules_version changes on schedule and payment edits,
        #    and overview snapshot_digest is completely unaffected (tables outside _source_digest).
        overview_before = await (await self.get("overview")).json()
        before_digest = overview_before["snapshot_digest"]

        # Reject any client query parameter (including ?today=...) on GET /mortgages.
        bad_query = await self.get("mortgages", params={"today": "2026-01-01"})
        self.assertEqual(bad_query.status, 400)
        self.assert_private_headers(bad_query)

        fixed_now = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc).timestamp()
        with mock.patch.object(web, "_now", return_value=fixed_now):
            # Empty initial read works with Cache-Control: no-store.
            r0 = await self.get("mortgages")
            self.assertEqual(r0.status, 200)
            self.assert_private_headers(r0)
            d0 = await r0.json()
            api_contract.BudgetAnalyticsMortgages.model_validate(d0)
            self.assertEqual(d0["evaluation_timezone"], "UTC")
            self.assertEqual((d0["schedules"], d0["reminders"]), ([], []))

            # Create schedule via HTTP POST /mortgage-save.
            create_resp = await self.post("mortgage-save", _schedule_payload(
                label="HTTP Mortgage", amount="3100.50", start_date="2026-10-01",
                end_date="2026-11-30", due_day=15, destination_kind="owner_email",
            ))
            self.assertEqual(create_resp.status, 200, await create_resp.text())
            self.assert_private_headers(create_resp)
            created_body = await create_resp.json()
            api_contract.BudgetAnalyticsMortgageScheduleResult.model_validate(created_body)
            sched_id = created_body["schedule"]["id"]

            r1 = await self.get("mortgages")
            d1 = await r1.json()
            api_contract.BudgetAnalyticsMortgages.model_validate(d1)
            self.assertNotEqual(d1["schedules_version"], d0["schedules_version"])
            self.assertEqual(d1["reminders"][0]["due_date"], "2026-10-15")
            self.assertEqual(d1["reminders"][0]["status"], "overdue")
            self.assertNotIn("destination_target", json.dumps(d1))

            # Mark 2026-10 paid via HTTP POST /mortgage-payment-set -> advances due date & changes schedules_version.
            pay_resp = await self.post("mortgage-payment-set", {
                "schedule_id": sched_id, "month": "2026-10", "paid": True, "expected_version": 1,
            })
            self.assertEqual(pay_resp.status, 200, await pay_resp.text())
            self.assert_private_headers(pay_resp)
            pay_body = await pay_resp.json()
            api_contract.BudgetAnalyticsMortgagePaymentResult.model_validate(pay_body)
            self.assertEqual(pay_body["payment"]["version"], 2)

            r2 = await self.get("mortgages")
            d2 = await r2.json()
            api_contract.BudgetAnalyticsMortgages.model_validate(d2)
            self.assertNotEqual(d2["schedules_version"], d1["schedules_version"])
            self.assertEqual(d2["reminders"][0]["due_date"], "2026-11-15")

            # Strict DTO rejects destination_target PII, shared_calendar, float/int amount, bool due_day.
            for bad_body in (
                {**_schedule_payload(), "destination_target": "owner@example.com"},
                _schedule_payload(destination_kind="shared_calendar"),
                _schedule_payload(amount=2500),
                _schedule_payload(due_day=True),
            ):
                bad_resp = await self.post("mortgage-save", bad_body)
                self.assertEqual(bad_resp.status, 400)
                self.assert_private_headers(bad_resp)

        # Overview snapshot_digest is untouched by mortgage schedules & payments.
        overview_after = await (await self.get("overview")).json()
        self.assertEqual(overview_after["snapshot_digest"], before_digest)

        # Direct views test when owner has no ledger observations at all -> ledger_as_of=None, freshness_stale=True.
        views = BudgetAnalyticsViews(self.sessions, clock=lambda: fixed_now, cursor_secret=b"synthetic-only")
        bob_view = views.mortgages("bob")
        self.assertEqual(bob_view["evaluation_date"], "2026-11-21")
        self.assertIsNotNone(bob_view["ledger_as_of"])
        with self.sessions.begin() as session:
            session.add(db.User(username="emptyadmin", password_hash="unused", role="admin"))
        views.save_mortgage_schedule("emptyadmin", **_schedule_payload(start_date="2026-11-01"))
        empty_view = views.mortgages("emptyadmin")
        self.assertIsNone(empty_view["ledger_as_of"])
        self.assertTrue(empty_view["reminders"][0]["freshness_stale"])


if __name__ == "__main__":
    unittest.main()
