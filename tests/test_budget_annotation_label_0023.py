"""Migration 0023 and one-off label normalization.

down_revision is 0019, the current master head. Re-parent after 0020, 0021 and 0022 at merge.
The label text is owner-private: these tests use synthetic phrases and never log them.
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from hestia import api_contract, db
from hestia.budget import month_budget
from hestia.budget.analytics_core import (
    AnalyticsError, active_one_off_label, clean_one_off_label, stored_one_off_label, sum_money,
)
from tests import test_budget_analytics_api as analytics_api_tests
from tests.test_budget_analytics_store import AnalyticsCase
from tests.test_budget_analytics_views import ProjectionCase
from tests.test_budget_month import _eval_charts, _tx

_INSERT = (
    "INSERT INTO budget_annotations (account_id, entry_reference, owner, mode, category_id, kind, "
    "ledger_fingerprint, version, updated_at, label) VALUES ('acct', ?, 'owner', 'manual', NULL, "
    "'expense', 'fp', 1, 1.0, ?)"
)


def _fk_off(conn):
    # foreign_keys cannot change inside a transaction. The connect listener turns them on.
    conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
    conn.commit()


class AnnotationLabelMigration0023Tests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(self.directory.name) / "m0023.db"

    def test_upgrade_downgrade_check_and_zero_drift(self):
        engine = db.make_engine(self.path)
        try:
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "0019")
            with engine.connect() as conn:
                _fk_off(conn)
                conn.execute(db.text(
                    "INSERT INTO budget_annotations (account_id, entry_reference, owner, mode, category_id, kind, "
                    "ledger_fingerprint, version, updated_at) VALUES ('acct', 'kept', 'owner', 'manual', NULL, "
                    "'expense', 'fp', 1, 1.0)"))
                conn.commit()
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "0023")

            columns = {column["name"]: column for column in inspect(engine).get_columns("budget_annotations")}
            self.assertIn("label", columns)
            self.assertTrue(columns["label"]["nullable"])
            checks = inspect(engine).get_check_constraints("budget_annotations")
            self.assertTrue(any("trim(label)" in (item.get("sqltext") or "") for item in checks))

            with engine.connect() as conn:
                self.assertIsNone(conn.exec_driver_sql(
                    "SELECT label FROM budget_annotations WHERE entry_reference='kept'").scalar())

            def insert(reference, label):
                with engine.connect() as conn:
                    _fk_off(conn)
                    conn.exec_driver_sql(_INSERT, (reference, label))
                    conn.commit()

            insert("ok", "Remont lazienki")
            insert("eighty", "a" * 80)
            insert("blank-check", None)
            # SQLite trim() strips spaces only, so a newline satisfies the CHECK. The app rejects it.
            insert("newline", "line\n")
            for reference, label in (
                ("empty", ""),
                ("spaces", "  "),
                ("lead", " padded"),
                ("trail", "padded "),
                ("long", "b" * 81),
            ):
                with self.subTest(reference=reference), self.assertRaises(IntegrityError):
                    insert(reference, label)

            with engine.begin() as conn:
                command.downgrade(db._alembic_config(conn), "0019")
            self.assertNotIn("label", {column["name"] for column in inspect(engine).get_columns("budget_annotations")})
            with engine.connect() as conn:
                self.assertEqual(conn.exec_driver_sql(
                    "SELECT COUNT(*) FROM budget_annotations WHERE entry_reference='kept'").scalar(), 1)
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "head")
            with engine.connect() as conn:
                context = MigrationContext.configure(conn)
                self.assertEqual(compare_metadata(context, db.Base.metadata), [])
            with engine.connect() as conn:
                self.assertIsNone(conn.exec_driver_sql(
                    "SELECT label FROM budget_annotations WHERE entry_reference='kept'").scalar())
                with self.assertRaises(IntegrityError):
                    _fk_off(conn)
                    conn.exec_driver_sql(_INSERT, ("after", ""))
                    conn.commit()
        finally:
            engine.dispose()


class OneOffLabelCoreTests(unittest.TestCase):
    def test_clean_trims_normalizes_and_clears_blank(self):
        self.assertIsNone(clean_one_off_label(None))
        self.assertIsNone(clean_one_off_label(""))
        self.assertIsNone(clean_one_off_label("   "))
        self.assertEqual(clean_one_off_label("  Remont łazienki  "), "Remont łazienki")
        self.assertEqual(clean_one_off_label("e\u0301"), "\u00e9")
        self.assertEqual(clean_one_off_label("a" * 80), "a" * 80)

    def test_clean_rejects_controls_length_and_non_text(self):
        for value in ("\n", "a\nb", "\t", "\u200b", "\u202e", "a" * 81, 80, b"label"):
            with self.subTest(value=value), self.assertRaises(AnalyticsError) as error:
                clean_one_off_label(value)
            self.assertEqual((error.exception.code, str(error.exception)),
                             ("budget_analytics_invalid_request", "budget_analytics_invalid_request"))
            self.assertNotIn("label", str(error.exception))

    def test_stored_accepts_only_canonical_text(self):
        self.assertIsNone(stored_one_off_label(None))
        self.assertEqual(stored_one_off_label("Remont"), "Remont")
        for value in (" padded", "e\u0301", "", "a" * 81):
            with self.subTest(value=value), self.assertRaises(AnalyticsError):
                stored_one_off_label(value)

    def test_active_label_ignores_stale_blank_and_optional_minimum(self):
        fresh = {"stale": False, "label": "Fachowiec"}
        self.assertEqual(active_one_off_label(fresh, "2000"), "Fachowiec")
        self.assertEqual(active_one_off_label(fresh, "-2000"), "Fachowiec")
        self.assertIsNone(active_one_off_label({"stale": True, "label": "Fachowiec"}, "2000"))
        self.assertIsNone(active_one_off_label({"stale": False, "label": ""}, "2000"))
        self.assertIsNone(active_one_off_label(None, "2000"))
        self.assertIsNone(active_one_off_label(fresh, "99.99", minimum="100"))
        self.assertEqual(active_one_off_label(fresh, "100", minimum="100"), "Fachowiec")
        self.assertEqual(active_one_off_label(fresh, "-100.00", minimum="100"), "Fachowiec")
        self.assertIsNone(active_one_off_label(fresh, "not-money", minimum="100"))
        self.assertIsNone(active_one_off_label(fresh, "10", minimum="not-money"))


class AnnotationLabelStoreTests(AnalyticsCase):
    def _audits(self):
        return self.rows(db.BudgetAnalyticsAudit)

    def test_label_is_stored_trimmed_and_kept_out_of_the_audit(self):
        phrase = "Remont \u0142azienki \u2013 fachowiec"
        saved = self.annotation(kind="expense", label=f"  {phrase}  ")
        self.assertEqual((saved["label"], saved["changed"], saved["version"]), (phrase, True, 1))
        self.assertEqual(self.rows(db.BudgetAnnotation)[0].label, phrase)
        again = self.annotation(kind="expense", label=phrase, expected_version=1)
        self.assertEqual((again["changed"], again["version"], again["label"]), (False, 1, phrase))
        combined = " ".join((row.before_json or "") + (row.after_json or "") for row in self._audits())
        self.assertNotIn(phrase, combined)
        self.assertNotIn("\u0142azienki", combined)
        self.assertIn('"has_label":true', self._audits()[-1].after_json)
        self.assertEqual(len(self._audits()), 1)

    def test_blank_clears_controls_do_not_write_and_auto_mode_can_keep_a_label(self):
        first = self.annotation(kind="expense", label="ATM")
        cleared = self.annotation(kind="expense", label="", expected_version=1)
        self.assertEqual((cleared["label"], cleared["changed"], cleared["version"]), (None, True, 2))
        self.assertIsNone(self.rows(db.BudgetAnnotation)[0].label)
        self.assertIn('"has_label":false', self._audits()[-1].after_json)
        self.assertNotIn("ATM", self._audits()[-1].before_json)
        noop = self.annotation(kind="expense", label="   ", expected_version=2)
        self.assertEqual((noop["changed"], noop["label"]), (False, None))
        also_null = self.annotation(kind="expense", label=None, expected_version=2)
        self.assertFalse(also_null["changed"])
        folded = self.annotation(kind="expense", label="e\u0301", expected_version=2)
        self.assertEqual(folded["label"], "\u00e9")
        before = len(self._audits())
        version = folded["version"]
        for bad in ("\n", "\u200b", "\u202e", "a" * 81, 3):
            with self.subTest(bad=type(bad).__name__), self.assertRaises(AnalyticsError):
                self.annotation(kind="expense", label=bad, expected_version=version)
        self.assertEqual(len(self._audits()), before)
        self.assertEqual(self.rows(db.BudgetAnnotation)[0].label, "\u00e9")
        auto = self.annotation(account="b", reference="credit", mode="auto", kind="auto", label="  Wyp\u0142ata  ")
        self.assertEqual((auto["mode"], auto["kind"], auto["label"], auto["version"], auto["changed"]),
                         ("auto", "auto", "Wyp\u0142ata", 1, True))
        self.assertTrue(any(row.entry_reference == "credit" and row.label == "Wyp\u0142ata"
                            for row in self.rows(db.BudgetAnnotation)))


class OneOffSeriesTests(unittest.TestCase):
    def test_labelled_rows_leave_their_category_without_changing_spent(self):
        fresh = {"version": 1, "mode": "manual", "category_id": None, "kind": "expense", "stale": False}
        food_plain = _tx("acc-a", "food", amount="10", category_id="cat-food", kind="expense", source="manual",
                         annotation={**fresh, "category_id": "cat-food", "label": None})
        food_labelled = _tx("acc-a", "food-big", amount="40", category_id="cat-food", kind="expense", source="manual",
                            annotation={**fresh, "category_id": "cat-food", "label": "Remont \u0142azienki"})
        cash = _tx("acc-a", "cash", amount="2000", kind="expense", source="manual",
                   annotation={**fresh, "label": "Remont \u0142azienki"})
        cash_same = _tx("acc-a", "cash-2", amount="15", kind="expense", source="manual",
                        annotation={**fresh, "label": None})
        stale = _tx("acc-a", "stale", amount="7", kind="expense", source="manual",
                    annotation={**fresh, "label": "Ukryte", "stale": True})
        refund = _tx("acc-a", "refund", amount="5", direction="CRDT", kind="refund", source="manual",
                     booking_date="2026-11-11",
                     annotation={**fresh, "kind": "refund", "label": "Zwrot materia\u0142u"})
        same = _tx("acc-a", "same", amount="3", kind="expense", source="manual",
                   annotation={**fresh, "label": "Remont \u0142azienki"})
        mortgage = _tx("acc-a", "mortgage", amount="500", category_id="cat-mortgage", kind="expense", source="manual",
                       annotation={**fresh, "category_id": "cat-mortgage", "label": "Nie ruszaj hipoteki"})
        settings = month_budget.MonthBudgetSettings(
            income_category_ids=("cat-salary", "cat-zus"),
            pass_through=month_budget.PassThroughSettings("cat-rent", "cat-mortgage"),
        )
        charts = _eval_charts(transactions=[food_plain, food_labelled, cash, cash_same, stale, refund, same, mortgage],
                              settings=settings)
        budget = charts["budget"]
        series = charts["series"]["months"][-1]["categories"]
        spent = budget["summary"]["spent"]
        self.assertEqual(month_budget.core.sum_money(row["net_spending"] for row in series), spent)
        self.assertEqual(month_budget.core.sum_money(row["net_spending"] for row in budget["category_spend"]), spent)
        by_spend = {row["category_id"]: row["net_spending"] for row in budget["category_spend"]}
        self.assertEqual(by_spend["cat-food"], "50")
        self.assertEqual(by_spend[None], "2020")
        self.assertEqual(by_spend["cat-mortgage"], "500")
        regular = {(row["category_id"], row["one_off"]): row["net_spending"] for row in series if not row["one_off"]}
        labelled = {(row["name"], row["category_id"]): row for row in series if row["one_off"]}
        self.assertEqual(regular[("cat-food", False)], "10")
        self.assertEqual(regular[(None, False)], "22")
        self.assertEqual(regular[("cat-mortgage", False)], "500")
        self.assertEqual(labelled[("Remont \u0142azienki", "cat-food")]["net_spending"], "40")
        self.assertEqual(labelled[("Remont \u0142azienki", None)]["net_spending"], "2003")
        self.assertEqual(labelled[("Zwrot materia\u0142u", None)]["net_spending"], "-5")
        self.assertNotIn(("Ukryte", None), labelled)
        self.assertNotIn(("Nie ruszaj hipoteki", "cat-mortgage"), labelled)
        self.assertTrue(all(row["one_off"] is True and row["name"] for row in series if row["one_off"]))


class OneOffOverviewTests(ProjectionCase):
    def test_labelled_amounts_leave_their_category_and_keep_the_currency_total(self):
        food = self.store.create_category("owner", "Synthetic Food")
        mortgage = self.store.create_category("owner", "Synthetic mortgage")
        self.add_transaction("a", "food", amount="50")
        self.annotation(reference="food", category_id=food["id"], kind="expense")
        self.add_transaction("a", "reno", amount="2000")
        self.annotation(reference="reno", category_id=food["id"], kind="expense", label="Remont lazienki")
        self.add_transaction("a", "small", amount="15")
        self.add_transaction("a", "atm", amount="100")
        self.annotation(reference="atm", kind="expense", label="ATM")
        self.add_transaction("a", "refund", amount="5", credit_debit_indicator="CRDT")
        self.annotation(reference="refund", kind="refund", label="Zwrot materialu")
        self.add_transaction("a", "stale", amount="7")
        self.annotation(reference="stale", category_id=food["id"], kind="expense", label="Ukryte")
        # The financial fingerprint covers the booking date, not the description.
        self.change_transaction("a", "stale", booking_date="2026-11-29")
        self.add_transaction("a", "mort", amount="500")
        self.annotation(reference="mort", category_id=mortgage["id"], kind="expense", label="Nie ruszaj")
        self.store.save_spending_limit("owner", category_id=food["id"], currency="PLN", monthly_limit="9000",
                                       warning_percent=80, enabled=True, expected_version=0)

        overview = self.store.overview("owner", "2026-11")
        api_contract.BudgetAnalyticsOverview.model_validate(overview)
        pln = overview["currencies"][0]
        categories = overview["categories"]
        self.assertEqual(sum_money(row["net_spending"] for row in categories), pln["net_spending"])
        seen_one_off = False
        for row in categories:
            if row["one_off"]:
                seen_one_off = True
            elif seen_one_off:
                self.fail("a regular category follows a one-off entry")
        by_key = {(row["one_off"], row["name"], row["category_id"]): row["net_spending"] for row in categories}
        # The stale manual category no longer applies, so that amount stays uncategorised.
        self.assertEqual(by_key[(False, "Synthetic Food", food["id"])], "50")
        self.assertEqual(by_key[(True, "Remont lazienki", food["id"])], "2000")
        self.assertEqual(by_key[(False, None, None)], "22")
        self.assertEqual(by_key[(True, "ATM", None)], "100")
        self.assertEqual(by_key[(True, "Zwrot materialu", None)], "-5")
        self.assertEqual(by_key[(True, "Nie ruszaj", mortgage["id"])], "500")
        self.assertNotIn((False, "Synthetic mortgage", mortgage["id"]), by_key)
        self.assertNotIn((True, "Ukryte", food["id"]), by_key)
        shown = self.output("stale", period="2026-11")
        self.assertEqual((shown["annotation"]["stale"], shown["annotation"]["label"]), (True, "Ukryte"))
        limits = self.store.spending_limits("owner", "2026-11")
        status = next(row for row in limits["statuses"] if row["category_id"] == food["id"])
        self.assertEqual((status["expenses"], status["refunds"]), ("2050", "0"))
        self.assertEqual(pln["net_spending"], "2667")

    def test_a_corrupt_stored_label_is_a_fixed_error(self):
        self.add_transaction("a", "bad", amount="4")
        self.annotation(reference="bad", kind="expense", label="Ok")
        with self.sessions.begin() as session:
            row = session.get(db.BudgetAnnotation, (self.accounts["a"], "bad"))
            row.label = "Ok\n"
        with self.assertRaises(AnalyticsError) as error:
            self.store.overview("owner", "2026-11")
        self.assertEqual(str(error.exception), "budget_analytics_invalid_request")
        self.assertNotIn("Ok", str(error.exception))


class OneOffLabelAPITests(unittest.IsolatedAsyncioTestCase):
    rows = analytics_api_tests.BudgetAnalyticsAPITests.rows
    token = analytics_api_tests.BudgetAnalyticsAPITests.token
    headers = analytics_api_tests.BudgetAnalyticsAPITests.headers

    async def asyncSetUp(self):
        await analytics_api_tests.BudgetAnalyticsAPITests.asyncSetUp(self)

    async def post(self, operation, body, *, user="alice", headers=None):
        return await analytics_api_tests.BudgetAnalyticsAPITests.post(
            self, operation, body, user=user, headers=headers)

    async def get(self, operation, *, params=None, user="alice", headers=None):
        return await analytics_api_tests.BudgetAnalyticsAPITests.get(
            self, operation, params=params, user=user, headers=headers)

    def annotation(self, **changes):
        return analytics_api_tests.BudgetAnalyticsAPITests.annotation(self, **changes)

    async def test_set_clear_and_reject_a_one_off_label_without_echoing_it(self):
        phrase = "Synthetic one-off"
        saved = await self.post("annotation", self.annotation(label=f"  {phrase}  "))
        self.assertEqual(saved.status, 200, await saved.text())
        body = await saved.json()
        api_contract.BudgetAnalyticsAnnotationResult.model_validate(body)
        self.assertEqual(body["annotation"]["label"], phrase)
        version = body["annotation"]["version"]
        audits = "".join((row.before_json or "") + (row.after_json or "") for row in self.rows(db.BudgetAnalyticsAudit))
        self.assertNotIn(phrase, audits)
        self.assertIn("has_label", audits)

        transactions = await (await self.get("transactions")).json()
        row = next(item for item in transactions["transactions"] if item["entry_reference"] == self.debit["entry_reference"])
        self.assertEqual(row["annotation"]["label"], phrase)
        overview = await (await self.get("overview")).json()
        api_contract.BudgetAnalyticsOverview.model_validate(overview)
        labelled = [item for item in overview["categories"] if item.get("one_off")]
        self.assertEqual([(item["name"], item["net_spending"]) for item in labelled], [(phrase, "10")])
        currency = next(item for item in overview["currencies"] if item["currency"] == "PLN")
        same_currency = [item for item in overview["categories"] if item["currency"] == "PLN"]
        self.assertEqual(sum_money(item["net_spending"] for item in same_currency), currency["net_spending"])

        cleared = await self.post("annotation", self.annotation(label="", expected_version=version))
        self.assertIsNone((await cleared.json())["annotation"]["label"])
        version += 1
        unchanged = await self.post("annotation", self.annotation(label=None, expected_version=version))
        again = await unchanged.json()
        self.assertEqual((again["annotation"]["changed"], again["annotation"]["label"]), (False, None))

        hidden = "Synthetic hidden"
        rejected = await self.post("annotation", self.annotation(label=hidden + "\n", expected_version=version))
        self.assertEqual(rejected.status, 400)
        self.assertEqual((await rejected.json())["error"], "budget_analytics_invalid_request")
        self.assertNotIn(hidden, await rejected.text())
        too_long = await self.post("annotation", self.annotation(label="a" * 81, expected_version=version))
        self.assertEqual(too_long.status, 400)
        self.assertEqual((await too_long.json())["error"], "budget_invalid_request")
        denied = await self.post("annotation", self.annotation(label="Viewer phrase", expected_version=version), user="viewer")
        self.assertEqual(denied.status, 403)
        self.assertNotIn("Viewer phrase", await denied.text())
        self.assertNotIn("Viewer phrase", [row.label for row in self.rows(db.BudgetAnnotation)])
