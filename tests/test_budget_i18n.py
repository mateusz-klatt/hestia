"""Budget category/label translations, viewer preferences, migration and HTTP boundaries."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from hestia import api_contract, db
from hestia.budget import analytics_api, household
from hestia.budget.analytics_core import AnalyticsError, financial_fingerprint
from hestia.budget.analytics_store import BudgetAnalyticsStore
from hestia.budget.i18n import DisplayResolver, LANGUAGES, preferred_languages, translated, validate_languages
from tests import test_budget_analytics_api as analytics_api_tests
from tests.test_budget_analytics_store import AnalyticsCase
from tests import test_budget_household as household_tests
from tests.migration_head import HEAD

GROCERIES = household_tests.GROCERIES
SECRET = household_tests.SECRET


class LanguageTests(unittest.TestCase):
    def test_backend_languages_are_exactly_the_ui_locale_files(self):
        expected = {path.stem for path in (Path(__file__).parents[1] / "ui" / "src" / "i18n" / "locales").glob("*.ts")}
        self.assertEqual(len(LANGUAGES), 45)
        self.assertEqual(set(LANGUAGES), expected)
        self.assertIn("en", LANGUAGES)

    def test_validation_and_locale_derived_defaults(self):
        self.assertIsNone(validate_languages(None))
        self.assertEqual(validate_languages(["zh-Hant", "my-MM", "en"]), ["zh-Hant", "my-MM", "en"])
        self.assertEqual(validate_languages([]), [])
        for value in (None, [], ["de", "fr"]):
            api_contract.SettingsUpdate.model_validate({"preferred_languages": value})
        for bad in ("de", ["de", "de"], ["xx"], [1], list(LANGUAGES) + ["en"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_languages(bad)
            with self.assertRaises(ValueError):
                api_contract.SettingsUpdate.model_validate({"preferred_languages": bad})
        self.assertEqual(preferred_languages(None), ["en"])
        self.assertEqual(preferred_languages(SimpleNamespace(preferred_languages=None, locale="pl")), ["pl"])
        self.assertEqual(preferred_languages(SimpleNamespace(preferred_languages=None, locale="pl-PL")), ["en"])
        self.assertEqual(preferred_languages(SimpleNamespace(preferred_languages=[], locale="pl")), [])
        self.assertEqual(preferred_languages(SimpleNamespace(preferred_languages=["bad"], locale="pl")), ["en"])
        self.assertEqual(translated("Canonical", {"de": "Kanoniczny", "fr": "Canonique"}, ["es", "fr"]), "Canonique")
        self.assertEqual(translated("Canonical", {}, ["de"]), "Canonical")
        resolver = DisplayResolver.__new__(DisplayResolver)
        resolver.preferences = ["de", "fr"]
        resolver.label_variants = {"ATM": {("account", "entry"): {"en": "Cash machine"}}}
        self.assertEqual(resolver.grouped_label("ATM"), "ATM")
        resolver.category_names = {"Canonical": "category"}
        resolver.categories = {"category": {"de": "Kanoniczny"}}
        resolver.category_ids = {"category"}
        decorated = resolver.decorate({"source": "category_baseline", "label": "Canonical"})
        self.assertEqual(decorated["display_name"], "Kanoniczny")


class TranslationMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(self.directory.name) / "i18n.db"

    def test_schema_constraints_cascade_and_downgrade_roundtrip(self):
        cfg = Config()
        cfg.set_main_option("script_location", str(db._MIGRATIONS_DIR))
        revision = ScriptDirectory.from_config(cfg).get_revision(HEAD)
        self.assertEqual(revision.down_revision, "0028")
        engine, sessions = db.init_db(self.path)
        with sessions.begin() as session:
            session.add(db.User(username="owner", password_hash="unused", role="admin"))
            session.flush()
            session.add(db.UserSetting(username="owner", locale="pl", temp_scale="C", theme=None,
                                       preferred_languages=["de", "en"]))
            session.add(db.BudgetCategory(id="food", owner="owner", name="Food", name_key="food", archived=False,
                                          version=1, created_at=1, updated_at=1, parent_id=None))
            session.flush()
            session.add(db.BudgetCategoryTranslation(category_id="food", owner="owner", lang="de",
                                                     name="Essen", name_key="essen"))
            session.add(db.BudgetCategory(id="other", owner="owner", name="Other", name_key="other", archived=False,
                                          version=1, created_at=1, updated_at=1, parent_id=None))
            session.add(db.BudgetCategoryTranslation(category_id="other", owner="owner", lang="de",
                                                     name="Anderes", name_key="anderes"))
        with self.assertRaises(IntegrityError):
            with sessions.begin() as session:
                session.add(db.BudgetCategoryTranslation(category_id="other", owner="owner", lang="de",
                                                         name="ESSEN", name_key="essen"))
        with self.assertRaises(IntegrityError):
            with sessions.begin() as session:
                session.add(db.BudgetCategoryTranslation(category_id="food", owner="owner", lang="de",
                                                         name="Duplicate language", name_key="duplicate"))
        with sessions.begin() as session:
            session.delete(session.get(db.BudgetCategory, "food"))
        with sessions() as session:
            self.assertEqual(session.get(db.BudgetCategory, "food"), None)
            self.assertEqual(session.get(db.BudgetCategoryTranslation, ("food", "de")), None)
            self.assertEqual(session.get(db.BudgetCategory, "other").name, "Other")
        engine.dispose()

        db.downgrade_db(self.path, "0027")
        with closing(sqlite3.connect(self.path)) as raw:
            tables = {row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("budget_category_translations", tables)
            self.assertNotIn("budget_annotation_label_translations", tables)
            settings_columns = {row[1] for row in raw.execute("PRAGMA table_info(user_settings)")}
            self.assertNotIn("preferred_languages", settings_columns)
            self.assertEqual(raw.execute("SELECT name FROM budget_categories WHERE id='other'").fetchone()[0], "Other")
            self.assertEqual(raw.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            self.assertEqual(raw.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(raw.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '!_alembic!_tmp!_%' ESCAPE '!'"
            ).fetchall(), [])
        engine, _sessions = db.init_db(self.path)
        with engine.connect() as connection:
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), db.metadata), [])
        engine.dispose()


class TranslationStoreTests(AnalyticsCase):
    def setUp(self):
        super().setUp()
        self.food = self.store.create_category("owner", "Synthetic Food")
        self.other = self.store.create_category("owner", "Synthetic Other")
        self.saved = self.annotation(kind="expense", label="ATM")

    def test_category_upsert_folded_collision_delete_and_cascade(self):
        first = self.store.set_category_translation("owner", category_id=self.food["id"], lang="de", name="  Lebens mittel  ")
        self.assertEqual((first["name"], first["changed"]), ("Lebens mittel", True))
        repeated = self.store.set_category_translation(
            "owner", category_id=self.food["id"], lang="de", name="Lebens mittel")
        self.assertFalse(repeated["changed"])
        renamed = self.store.set_category_translation("owner", category_id=self.food["id"], lang="de", name="Essen")
        self.assertTrue(renamed["changed"])
        with self.assertRaises(AnalyticsError) as collision:
            self.store.set_category_translation("owner", category_id=self.other["id"], lang="de", name=" essen ")
        self.assertEqual(collision.exception.status, 409)
        with self.assertRaises(AnalyticsError) as unknown:
            self.store.set_category_translation("owner", category_id="missing", lang="de", name="Essen")
        self.assertEqual(unknown.exception.status, 404)
        foreign = self.store.create_category("other", "Foreign Food")
        self.assertTrue(self.store.set_category_translation(
            "other", category_id=foreign["id"], lang="de", name="Essen")["changed"])
        with self.assertRaises(AnalyticsError):
            self.store.set_category_translation("owner", category_id=foreign["id"], lang="de", name="Fremd")
        for lang in ("xx", None):
            with self.subTest(lang=lang), self.assertRaises(AnalyticsError):
                self.store.set_category_translation("owner", category_id=self.food["id"], lang=lang, name="Essen")
        with self.assertRaises(AnalyticsError):
            self.store.set_category_translation("owner", category_id=self.food["id"], lang="fr", name=" ")
        self.assertFalse(self.store.delete_category_translation(
            "owner", category_id=self.food["id"], lang="fr")["changed"])
        self.assertTrue(self.store.delete_category_translation(
            "owner", category_id=self.food["id"], lang="de")["changed"])
        self.assertFalse(self.store.delete_category_translation(
            "owner", category_id=self.food["id"], lang="de")["changed"])
        self.store.set_category_translation("owner", category_id=self.food["id"], lang="de", name="Essen")
        with self.sessions() as session:
            self.assertEqual(session.get(db.BudgetCategory, self.food["id"]).name, "Synthetic Food")
        with self.sessions.begin() as session:
            session.delete(session.get(db.BudgetCategory, self.food["id"]))
        self.assertEqual([row for row in self.rows(db.BudgetCategoryTranslation) if row.category_id == self.food["id"]], [])

    def test_integrity_race_is_reported_as_a_collision(self):
        real_flush = Session.flush

        def flaky_flush(session, *args, **kwargs):
            if any(isinstance(row, db.BudgetCategoryTranslation) for row in session.new):
                raise IntegrityError("INSERT", {}, Exception("unique"))
            return real_flush(session, *args, **kwargs)

        with mock.patch.object(Session, "flush", flaky_flush):
            with self.assertRaises(AnalyticsError) as collision:
                self.store.set_category_translation("owner", category_id=self.food["id"], lang="it", name="Cibo")
        self.assertEqual(collision.exception.status, 409)

    def test_label_upsert_validation_delete_and_canonical_lifecycle(self):
        key = {"account_id": self.accounts["a"], "entry_reference": "debit"}
        first = self.store.set_label_translation("owner", **key, lang="de", label=" Geldautomat ")
        self.assertEqual((first["label"], first["changed"]), ("Geldautomat", True))
        self.assertFalse(self.store.set_label_translation("owner", **key, lang="de", label="Geldautomat")["changed"])
        self.assertTrue(self.store.set_label_translation("owner", **key, lang="de", label="Bankomat")["changed"])
        for bad in (None, "", " ", "line\n", "a" * 81):
            with self.subTest(bad=bad), self.assertRaises(AnalyticsError):
                self.store.set_label_translation("owner", **key, lang="fr", label=bad)
        with self.assertRaises(AnalyticsError) as missing:
            self.store.set_label_translation("owner", account_id=self.accounts["a"], entry_reference="missing",
                                             lang="de", label="Fehlt")
        self.assertEqual(missing.exception.status, 404)
        self.annotation(account="b", reference="credit", kind="auto")
        with self.assertRaises(AnalyticsError):
            self.store.set_label_translation("owner", account_id=self.accounts["b"], entry_reference="credit",
                                             lang="de", label="Kein Label")
        self.assertFalse(self.store.delete_label_translation("owner", **key, lang="fr")["changed"])
        self.assertTrue(self.store.delete_label_translation("owner", **key, lang="de")["changed"])
        self.assertFalse(self.store.delete_label_translation("owner", **key, lang="de")["changed"])
        self.store.set_label_translation("owner", **key, lang="de", label="Bankomat")
        changed = self.annotation(kind="expense", label="Cash", expected_version=1)
        self.assertEqual(changed["version"], 2)
        self.assertEqual(self.rows(db.BudgetAnnotationLabelTranslation), [])
        self.store.set_label_translation("owner", **key, lang="de", label="Bargeld")
        cleared = self.annotation(kind="expense", label=None, expected_version=2)
        self.assertIsNone(cleared["label"])
        self.assertEqual(self.rows(db.BudgetAnnotationLabelTranslation), [])
        with self.assertRaises(IntegrityError):
            with self.sessions.begin() as session:
                session.add(db.BudgetAnnotationLabelTranslation(**key, lang="de", label=" padded"))

    def test_label_translation_cascades_with_annotation_delete(self):
        key = {"account_id": self.accounts["a"], "entry_reference": "debit"}
        self.store.set_label_translation("owner", **key, lang="de", label="Bankomat")
        with self.sessions.begin() as session:
            session.delete(session.get(db.BudgetAnnotation, (self.accounts["a"], "debit")))
        self.assertEqual(self.rows(db.BudgetAnnotationLabelTranslation), [])

    def test_mixed_bulk_import_is_idempotent_and_reports_each_error(self):
        account = self.accounts["a"]
        entries = [
            {"kind": "category", "category_id": self.food["id"], "lang": "de", "name": "Essen"},
            {"kind": "category", "category_id": self.other["id"], "lang": "de", "name": "essen"},
            {"kind": "category", "category_id": "missing", "lang": "de", "name": "Fehlt"},
            {"kind": "label", "account_id": account, "entry_reference": "debit", "lang": "de", "label": "Bankomat"},
            {"kind": "label", "account_id": account, "entry_reference": "missing", "lang": "de", "label": "Fehlt"},
            {"kind": "other"},
            {"kind": "category", "category_id": self.food["id"], "lang": "fr", "name": "Nourriture"},
        ]
        results = self.store.import_translations("owner", entries=entries)
        self.assertEqual([row["ok"] for row in results], [True, False, False, True, False, False, True])
        self.assertEqual([row["index"] for row in results], list(range(7)))
        self.assertEqual(results[1]["error"], "budget_analytics_conflict")
        self.assertEqual(results[2]["error"], "budget_analytics_not_found")
        self.assertEqual(results[4]["error"], "budget_analytics_not_found")
        replay = self.store.import_translations("owner", entries=[entries[0], entries[3], entries[6]])
        self.assertEqual([(row["ok"], row["changed"]) for row in replay], [(True, False)] * 3)
        self.assertEqual(len(self.store.import_translations("owner", entries=[entries[0]] * 500)), 500)
        for bad in ([], [entries[0]] * 501):
            with self.subTest(size=len(bad)), self.assertRaises(AnalyticsError):
                self.store.import_translations("owner", entries=bad)


class TranslationAPITests(unittest.IsolatedAsyncioTestCase):
    rows = analytics_api_tests.BudgetAnalyticsAPITests.rows
    token = analytics_api_tests.BudgetAnalyticsAPITests.token
    headers = analytics_api_tests.BudgetAnalyticsAPITests.headers
    get = analytics_api_tests.BudgetAnalyticsAPITests.get
    post = analytics_api_tests.BudgetAnalyticsAPITests.post
    annotation = analytics_api_tests.BudgetAnalyticsAPITests.annotation

    async def asyncSetUp(self):
        await analytics_api_tests.BudgetAnalyticsAPITests.asyncSetUp(self)
        self.analytics = BudgetAnalyticsStore(self.sessions, cursor_secret=SECRET)

    def preferences(self, languages, *, locale="de"):
        with self.sessions.begin() as session:
            row = session.get(db.UserSetting, "alice")
            if row is None:
                row = db.UserSetting(username="alice", locale=locale, temp_scale=None, theme=None)
                session.add(row)
            row.preferred_languages = languages

    async def test_category_translation_routes_maps_and_fallback_order(self):
        food = self.analytics.create_category("alice", "Canonical Food")
        empty = self.analytics.create_category("alice", "No Translation")
        for lang, name in (("en", "Food"), ("fr", "Nourriture"), ("de", "Essen")):
            response = await self.post("category-translation-set", {"category_id": food["id"], "lang": lang, "name": name})
            self.assertEqual(response.status, 200, await response.text())
            api_contract.BudgetCategoryTranslationSetResult.model_validate(await response.json())
        repeated = await self.post("category-translation-set", {"category_id": food["id"], "lang": "de", "name": "Essen"})
        self.assertFalse((await repeated.json())["translation"]["changed"])
        collision = await self.post("category-translation-set", {"category_id": empty["id"], "lang": "de", "name": "essen"})
        self.assertEqual(collision.status, 409)
        unknown = await self.post("category-translation-set", {"category_id": "missing", "lang": "de", "name": "Fehlt"})
        self.assertEqual(unknown.status, 404)

        self.preferences(["fr", "de"])
        body = await (await self.get("categories")).json()
        api_contract.BudgetAnalyticsCategories.model_validate(body)
        rows = {row["id"]: row for row in body["categories"]}
        self.assertEqual(rows[food["id"]]["display_name"], "Nourriture")
        self.assertEqual(rows[food["id"]]["translations"], {"en": "Food", "fr": "Nourriture", "de": "Essen"})
        self.assertEqual(rows[food["id"]]["name"], "Canonical Food")
        self.assertEqual(rows[food["id"]]["color_hue"], food["color_hue"])
        self.assertEqual((rows[empty["id"]]["display_name"], rows[empty["id"]]["translations"]),
                         ("No Translation", {}))
        self.preferences(["de", "fr"])
        rows = {row["id"]: row for row in (await (await self.get("categories")).json())["categories"]}
        self.assertEqual(rows[food["id"]]["display_name"], "Essen")
        self.preferences([])
        rows = {row["id"]: row for row in (await (await self.get("categories")).json())["categories"]}
        self.assertEqual(rows[food["id"]]["display_name"], "Canonical Food")
        self.preferences(None)
        rows = {row["id"]: row for row in (await (await self.get("categories")).json())["categories"]}
        self.assertEqual(rows[food["id"]]["display_name"], "Essen")

        deleted = await self.post("category-translation-delete", {"category_id": food["id"], "lang": "de"})
        self.assertEqual(deleted.status, 200)
        api_contract.BudgetCategoryTranslationDeleteResult.model_validate(await deleted.json())
        repeated = await self.post("category-translation-delete", {"category_id": food["id"], "lang": "de"})
        self.assertFalse((await repeated.json())["translation"]["changed"])

    async def test_label_display_bulk_import_and_delete_routes(self):
        response = await self.post("annotation", self.annotation(label="ATM"))
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await response.json())["annotation"]["display_label"], "ATM")
        set_body = {"account_id": self.alice_id, "entry_reference": self.debit["entry_reference"],
                    "lang": "de", "label": "Bankomat"}
        response = await self.post("label-translation-set", set_body)
        self.assertEqual(response.status, 200, await response.text())
        api_contract.BudgetLabelTranslationSetResult.model_validate(await response.json())
        self.preferences(["de"])
        transactions = await (await self.get("transactions")).json()
        row = next(row for row in transactions["transactions"] if row["entry_reference"] == self.debit["entry_reference"])
        self.assertEqual((row["annotation"]["label"], row["annotation"]["display_label"]), ("ATM", "Bankomat"))
        overview = await (await self.get("overview")).json()
        one_off = next(row for row in overview["categories"] if row.get("one_off"))
        self.assertEqual((one_off["name"], one_off["display_label"]), ("ATM", "Bankomat"))

        food = self.analytics.create_category("alice", "Bulk Food")
        entries = [
            {"kind": "category", "category_id": food["id"], "lang": "de", "name": "Essen"},
            {"kind": "category", "category_id": "missing", "lang": "de", "name": "Fehlt"},
            {"kind": "label", "account_id": self.alice_id, "entry_reference": self.debit["entry_reference"],
             "lang": "fr", "label": "Distributeur"},
        ]
        imported = await self.post("translation-import", {"entries": entries})
        self.assertEqual(imported.status, 200, await imported.text())
        body = await imported.json()
        api_contract.BudgetTranslationImportResult.model_validate(body)
        self.assertEqual([row["ok"] for row in body["results"]], [True, False, True])
        replay = await (await self.post("translation-import", {"entries": [entries[0], entries[2]]})).json()
        self.assertEqual([(row["ok"], row["changed"]) for row in replay["results"]], [(True, False), (True, False)])
        self.assertEqual((await self.post("translation-import", {"entries": []})).status, 400)
        self.assertEqual((await self.post("translation-import", {"entries": [entries[0]] * 501})).status, 400)

        delete_body = {key: value for key, value in set_body.items() if key != "label"}
        deleted = await self.post("label-translation-delete", delete_body)
        self.assertEqual(deleted.status, 200)
        api_contract.BudgetLabelTranslationDeleteResult.model_validate(await deleted.json())
        repeated = await self.post("label-translation-delete", delete_body)
        self.assertFalse((await repeated.json())["translation"]["changed"])
        unknown = await self.post("label-translation-set", set_body | {"entry_reference": "missing"})
        self.assertEqual(unknown.status, 404)

    async def test_bulk_import_validates_rows_independently(self):
        food = self.analytics.create_category("alice", "Row Validation Food")
        valid = {"kind": "category", "category_id": food["id"], "lang": "de", "name": "Essen"}
        entries = [
            valid,
            valid | {"lang": "unsupported"},
            {"kind": "other"},
            valid | {"name": "x" * 81},
            "not-an-object",
            valid | {"lang": "fr", "name": "Nourriture"},
        ]
        response = await self.post("translation-import", {"entries": entries})
        self.assertEqual(response.status, 200, await response.text())
        body = await response.json()
        api_contract.BudgetTranslationImportResult.model_validate(body)
        self.assertEqual([row["index"] for row in body["results"]], list(range(len(entries))))
        self.assertEqual([row["ok"] for row in body["results"]], [True, False, False, False, False, True])
        self.assertEqual([row["error"] for row in body["results"] if not row["ok"]],
                         ["budget_analytics_invalid_request"] * 4)
        categories = await (await self.get("categories")).json()
        row = next(row for row in categories["categories"] if row["id"] == food["id"])
        self.assertEqual(row["translations"], {"de": "Essen", "fr": "Nourriture"})

        for envelope in ({"entries": []}, {"entries": [valid] * 501}, {"entries": {}},
                         {"entries": [valid], "extra": True}):
            with self.subTest(envelope=type(envelope["entries"]).__name__, size=len(envelope["entries"])):
                self.assertEqual((await self.post("translation-import", envelope)).status, 400)
        oversized = await self.post("translation-import", {
            "entries": [valid], "padding": "x" * analytics_api.MAX_TRANSACTIONS_BY_KEYS_BYTES})
        self.assertEqual(oversized.status, 413)


class ViewerPreferenceHouseholdTests(unittest.IsolatedAsyncioTestCase):
    rows = household_tests.HouseholdAPITests.rows
    token = household_tests.HouseholdAPITests.token
    headers = household_tests.HouseholdAPITests.headers
    grant_headers = household_tests.HouseholdAPITests.grant_headers
    fetch = household_tests.HouseholdAPITests.fetch

    async def asyncSetUp(self):
        await household_tests.HouseholdAPITests.asyncSetUp(self)

    async def test_household_never_uses_owner_names_or_translations(self):
        store = BudgetAnalyticsStore(self.sessions, cursor_secret=SECRET)
        store.set_category_translation("alice", category_id=self.groceries["id"], lang="de", name="Lebensmittel")
        store.set_category_translation("alice", category_id=self.groceries["id"], lang="fr", name="Courses")
        with self.sessions() as session:
            payload = json.loads(session.get(db.BudgetTransaction, (self.alice_id, "alice-entry")).payload_json)
        store.annotate("alice", self.alice_id, "alice-entry", mode="manual", category_id=self.groceries["id"],
                       kind="expense", expected_version=0, expected_fingerprint=financial_fingerprint(payload))
        with self.sessions.begin() as session:
            session.add(db.UserSetting(username="alice", locale="en", temp_scale=None, theme=None,
                                       preferred_languages=["de"]))
            session.add(db.UserSetting(username="viewer", locale="en", temp_scale=None, theme=None,
                                       preferred_languages=["fr"]))
        alias = household.entity_alias(SECRET, self.grant_id, "category", self.groceries["id"])
        categories = await (await self.fetch("categories")).json()
        row = next(row for row in categories["categories"] if row["id"] == alias)
        safe_row = dict(row)
        self.assertEqual(row["color_hue"], self.groceries["color_hue"])
        for operation in ("categories", "overview", "monthly-budget"):
            body = await (await self.fetch(operation)).json()
            text = json.dumps(body, ensure_ascii=False)
            for secret in (GROCERIES, "Lebensmittel", "Courses"):
                self.assertNotIn(secret, text, operation)
            self.assertNotIn("display_name", text, operation)
            self.assertNotIn("display_label", text, operation)
        overview = await (await self.fetch("overview")).json()
        row = next(row for row in overview["categories"] if row["category_id"] == alias)
        self.assertEqual(row["color_hue"], self.groceries["color_hue"])
        for languages in (["de"], []):
            with self.sessions.begin() as session:
                session.get(db.UserSetting, "viewer").preferred_languages = languages
            categories = await (await self.fetch("categories")).json()
            row = next(row for row in categories["categories"] if row["id"] == alias)
            self.assertEqual(row, safe_row)
        api_contract.BudgetHouseholdCategories.model_validate(categories)


if __name__ == "__main__":
    unittest.main()
