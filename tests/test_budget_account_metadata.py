"""Synthetic portable labels, independent observation digest and atomic patches."""
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import math
import unittest

from alembic import command
from sqlalchemy import select

from hestia import db
from hestia.budget.account_metadata import (AccountMetadata, BudgetMetadataError, apply_account_metadata,
                                           parse_account_metadata)
from tests import test_budget_store as fixtures


NOW = datetime(2026, 11, 21, 12, tzinfo=timezone.utc).timestamp()
CONTEXT = {"application_id": fixtures.APP, "owner": "owner", "connection_id": "bank",
           "aspsp": {"name": "Synthetic Bank", "country": "PL"}, "identification_hash": "synthetic-hash"}


def document(**changes):
    return {**{"product": "Konto osobiste", "currency": "PLN", "masked_suffix": "12AB",
               "observed_at": "2026-11-21T09:00:00Z"}, **changes}


class AccountMetadataNormalizationTests(unittest.TestCase):
    def parse(self, **changes):
        return parse_account_metadata(document(**changes), now=NOW, context=CONTEXT)

    def test_portable_fields_nfc_whitespace_controls_and_raw_length_bounds(self):
        value = self.parse(product="  Konto\tZ\u0307o\u0301łw\n\x00\u200b  ", masked_suffix="0000")
        self.assertIsInstance(value, AccountMetadata)
        self.assertEqual(value.product, "Konto Żółw")
        self.assertEqual(value.masked_suffix, "0000")
        self.assertFalse(value.product_rejected)
        with self.assertRaises(FrozenInstanceError):
            value.currency = "EUR"
        for product in (None, "", " \t\n", "\x00\u200b\u202e"):
            with self.subTest(product=repr(product)):
                result = self.parse(product=product)
                self.assertIsNone(result.product)
                self.assertFalse(result.product_rejected)
        for product in ("CAFEBABE", "CAFEBABE FACADE", "Oszczednosciowe", "ↁ" * 12, "Konto 3.5",
                        "Konto 50 Premium Plus"):
            with self.subTest(product=product):
                self.assertEqual(self.parse(product=product).product, product)
        self.assertEqual(self.parse(product="ą" * 1024).product, "ą" * 64)
        self.assertEqual(self.parse(product="x" * 64).product, "x" * 64)
        self.assertEqual(self.parse(currency="ZZZ", masked_suffix="ABCD").currency, "ZZZ")
        for field in ("product", "currency", "masked_suffix"):
            self.assertIsNone(getattr(self.parse(**{field: None}), field))

    def test_identifier_like_products_are_removed_before_truncation_without_losing_other_fields(self):
        products = ("Konto2024", "VISA **** 1234", "Konto 12 34", "Konto 12-34", "Konto 12–34",
                    "Konto 12.34", "Konto１２３４", "Konto ١٢٣٤", "Konto ¹²³⁴",
                    "PL12ABCDEFGHIJKLMNOP", "ΡL12ABCDEFGHIJKLMNOP", "ＰＬ１２ABCDEFGHIJKLMNOP",
                    "Konto PL12 ABCD EFGH IJKL", "Konto PL-12-ABCD-EFGH-IJKL",
                    "Konto ΡL12 ABCD EFGH IJKL", "Konto ＰＬ１２ ABCD EFGH IJKL",
                    "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "A" * 32,
                    "ProductLong1", "1LongProductName", "x" * 64 + " PRIVATE1234")
        for product in products:
            with self.subTest(product=product):
                result = self.parse(product=product)
                self.assertIsNone(result.product)
                self.assertTrue(result.product_rejected)
                self.assertEqual((result.currency, result.masked_suffix), ("PLN", "12AB"))
        null = self.parse(product=None)
        rejected = self.parse(product="PRIVATE1234")
        self.assertEqual(null.digest, rejected.digest)
        self.assertNotEqual(null.product_rejected, rejected.product_rejected)

    def test_known_primary_is_removed_even_when_alphabetic_embedded_or_confusable(self):
        context = dict(CONTEXT, identification_hash="privatebankidentifier")
        null = parse_account_metadata(document(product=None), now=NOW, context=context)
        for product in ("privatebankidentifier", "Konto privatebankidentifier Premium",
                        "PRIVATEBANKIDENTIFIER", "prіvatebankidentifier", "ｐｒｉｖａｔｅｂａｎｋｉｄｅｎｔｉｆｉｅｒ",
                        "x" * 64 + " privatebankidentifier", " ".join("privatebankidentifier"),
                        "-".join("privatebankidentifier"), "Konto " + " / ".join("privatebankidentifier") + " Premium"):
            with self.subTest(product=product):
                result = parse_account_metadata(document(product=product), now=NOW, context=context)
                self.assertIsNone(result.product)
                self.assertTrue(result.product_rejected)
                self.assertEqual(result.digest, null.digest)
        for primary in (None, 42, "", " \x00\u200b ", "- _ /"):
            with self.subTest(primary=primary):
                result = parse_account_metadata(document(), now=NOW,
                                                context=dict(CONTEXT, identification_hash=primary))
                self.assertEqual(result.product, "Konto osobiste")
                self.assertFalse(result.product_rejected)
        self.assertEqual(parse_account_metadata(document(), now=NOW, context={}).product, "Konto osobiste")
        normalized = parse_account_metadata(document(product="Konto PRIVATEBANKIDENTIFIER"), now=NOW,
                                             context=dict(CONTEXT, identification_hash="prіvatebankidentifier\x00"))
        self.assertIsNone(normalized.product)
        self.assertTrue(normalized.product_rejected)
        punctuated = parse_account_metadata(document(product="Konto privatebankidentifier"), now=NOW,
                                             context=dict(CONTEXT, identification_hash="private-bank_identifier"))
        self.assertIsNone(punctuated.product)
        self.assertTrue(punctuated.product_rejected)

    def test_exact_block_schema_product_types_and_strict_ascii_codes(self):
        for raw in (None, [], {}, {**document(), "name": "PRIVATE"}, {**document(), "type": "PRIVATE"},
                    {**document(), "iban": "PRIVATE"}, {**document(), "uid": "PRIVATE"},
                    {**document(), "source": "api"}):
            with self.subTest(raw=type(raw).__name__), self.assertRaises(BudgetMetadataError) as caught:
                parse_account_metadata(raw, now=NOW, context=CONTEXT)
            self.assertNotIn("PRIVATE", str(caught.exception))
        for field in document():
            raw = {key: value for key, value in document().items() if key != field}
            with self.assertRaises(BudgetMetadataError):
                parse_account_metadata(raw, now=NOW, context=CONTEXT)
        for product in ([], {}, 1, False, "PRIVATE" + "x" * 1024):
            with self.subTest(product=type(product).__name__), self.assertRaises(BudgetMetadataError):
                self.parse(product=product)
        for currency in ("", "pln", "PLN ", "EU", "EURO", "１２３", 1, True, "PŁN"):
            with self.subTest(currency=currency), self.assertRaises(BudgetMetadataError):
                self.parse(currency=currency)
        for suffix in ("", "123", "12345", "abcdef", "abcd", "１２３４", "РL12", "12 3", "**12", 1234, True):
            with self.subTest(suffix=suffix), self.assertRaises(BudgetMetadataError):
                self.parse(masked_suffix=suffix)
        for code in ("PRIVATE", [], None):
            error = BudgetMetadataError(code)
            self.assertEqual(error.code, "invalid_metadata")
            self.assertNotIn("PRIVATE", str(error))

    def test_observation_time_and_trusted_context_define_independent_canonical_digest(self):
        utc = self.parse(observed_at="2026-11-21T09:00:00Z")
        offset = self.parse(observed_at="2026-11-21T10:00:00+01:00")
        self.assertEqual(utc.digest, offset.digest)
        self.assertEqual(utc.observed_at, offset.observed_at)
        reordered = {key: CONTEXT[key] for key in reversed(CONTEXT)}
        self.assertEqual(parse_account_metadata(document(), now=NOW, context=reordered).digest, utc.digest)
        for context in (dict(CONTEXT, owner="other"), dict(CONTEXT, identification_hash="other-hash"),
                        dict(CONTEXT, application_id="other-app"), dict(CONTEXT, connection_id="other-bank")):
            self.assertNotEqual(parse_account_metadata(document(), now=NOW, context=context).digest, utc.digest)
        self.assertNotEqual(self.parse(currency="EUR").digest, utc.digest)
        self.assertNotEqual(self.parse(observed_at="2026-11-21T09:00:01Z").digest, utc.digest)
        self.assertEqual(len(utc.digest), 64)
        self.assertEqual(self.parse(product="Konto Żółw").digest, self.parse(product="Konto Z\u0307o\u0301łw").digest)
        zero = parse_account_metadata(document(observed_at="1970-01-01T00:00:00Z"), now=0, context=CONTEXT)
        self.assertEqual(zero.observed_at, 0)

    def test_invalid_clocks_timestamps_and_unserializable_context_are_safe(self):
        for now in (None, True, "PRIVATE", -1, math.nan, math.inf, 10 ** 1000):
            with self.subTest(now=type(now).__name__), self.assertRaises(BudgetMetadataError):
                parse_account_metadata(document(), now=now, context=CONTEXT)
        for stamp in (None, 1, [], "PRIVATE", "x" * 65, "2026-11-21T09:00:00",
                      "2026-11-22T09:00:00Z", "2026-02-30T09:00:00Z", "1969-12-31T23:59:59Z"):
            with self.subTest(stamp=stamp), self.assertRaises(BudgetMetadataError) as caught:
                self.parse(observed_at=stamp)
            self.assertNotIn("PRIVATE", str(caught.exception))
        for context in (None, [], {"invalid": math.nan}, {"invalid": object()}, {"invalid": "\ud800"}):
            with self.subTest(context=type(context).__name__), self.assertRaises(BudgetMetadataError):
                parse_account_metadata(document(), now=NOW, context=context)


class AccountMetadataPersistenceTests(unittest.TestCase):
    setUp_database = fixtures.BudgetStoreTests.setUp
    tearDown = fixtures.BudgetStoreTests.tearDown
    rows = fixtures.BudgetStoreTests.rows

    def setUp(self):
        self.setUp_database()
        fixtures.prepare_selected(self.store, fixtures.configuration(accounts=("synthetic-hash", "second")))
        accounts = self.rows(db.BudgetAccount)
        self.account = accounts[0].id
        self.second = accounts[1].id

    def metadata(self, when=900, context=None, **changes):
        stamp = datetime.fromtimestamp(when, timezone.utc).isoformat()
        return parse_account_metadata(document(observed_at=stamp, **changes), now=self.now,
                                      context=CONTEXT if context is None else context)

    def apply(self, metadata, account=None):
        with self.store.transaction() as session:
            return apply_account_metadata(session, self.account if account is None else account, metadata, now=self.now)

    def snapshot(self):
        with self.sessions() as session:
            return {table.name: [tuple(row) for row in session.execute(select(table))]
                    for table in db.Base.metadata.sorted_tables if table.name != "budget_account_display"}

    def test_display_updates_only_own_table_and_two_accounts_can_share_suffix(self):
        before = self.snapshot()
        self.assertEqual(self.apply(self.metadata()), "updated")
        self.assertEqual(self.apply(self.metadata(context=dict(CONTEXT, identification_hash="second")), account=self.second), "updated")
        self.assertEqual(self.snapshot(), before)
        rows = self.rows(db.BudgetAccountDisplay)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row.masked_suffix for row in rows}, {"12AB"})
        self.assertEqual({row.source for row in rows}, {"offline_import"})
        self.assertNotEqual(rows[0].digest, rows[1].digest)

    def test_exact_repeat_older_and_newer_null_patch_preserve_monotonic_values(self):
        first = self.metadata()
        self.assertEqual(self.apply(first), "updated")
        original = self.rows(db.BudgetAccountDisplay)[0]
        self.now += 100
        self.assertEqual(self.apply(first), "unchanged")
        self.assertEqual(self.rows(db.BudgetAccountDisplay)[0].imported_at, original.imported_at)
        self.assertEqual(self.apply(self.metadata(when=800, currency="USD")), "older")
        patch = self.metadata(when=1000, product=None, currency="EUR", masked_suffix=None)
        self.assertEqual(self.apply(patch), "updated")
        current = self.rows(db.BudgetAccountDisplay)[0]
        self.assertEqual((current.product, current.currency, current.masked_suffix), ("Konto osobiste", "EUR", "12AB"))
        self.assertEqual((current.observed_at, current.imported_at, current.digest), (1000, 1100, patch.digest))
        self.assertEqual(self.apply(first), "older")
        self.assertEqual(self.apply(patch), "unchanged")

    def test_empty_patch_is_noop_even_for_missing_display_and_does_not_advance_time(self):
        empty = self.metadata(product=None, currency=None, masked_suffix=None)
        self.assertEqual(self.apply(empty), "empty")
        self.assertEqual(self.rows(db.BudgetAccountDisplay), [])
        self.assertEqual(self.apply(self.metadata()), "updated")
        before = self.rows(db.BudgetAccountDisplay)[0].imported_at
        self.now += 100
        unsafe = self.metadata(when=1000, product="PRIVATE1234", currency=None, masked_suffix=None)
        self.assertTrue(unsafe.product_rejected)
        self.assertEqual(self.apply(unsafe), "empty")
        row = self.rows(db.BudgetAccountDisplay)[0]
        self.assertEqual((row.observed_at, row.imported_at), (900, before))

    def test_same_time_different_digest_rolls_back_entire_caller_transaction(self):
        self.apply(self.metadata())
        before = self.snapshot()
        with self.assertRaises(BudgetMetadataError) as caught:
            with self.store.transaction() as session:
                session.get(db.BudgetConnection, "bank").paused = True
                apply_account_metadata(session, self.account, self.metadata(currency="EUR"), now=self.now)
        self.assertEqual(caught.exception.code, "metadata_conflict")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.rows(db.BudgetAccountDisplay)[0].currency, "PLN")

    def test_apply_rechecks_clock_and_cannot_create_account_metadata_from_future_or_wrong_type(self):
        first = self.metadata()
        for value in (None, replace(first, observed_at=math.nan), replace(first, observed_at=self.now + 1)):
            with self.subTest(metadata=type(value).__name__), self.assertRaises(BudgetMetadataError):
                self.apply(value)
        with self.assertRaises(BudgetMetadataError):
            with self.store.transaction() as session:
                apply_account_metadata(session, self.account, first, now=math.inf)
        self.assertEqual(self.rows(db.BudgetAccountDisplay), [])

    def test_migration_upgrade_downgrade_preserves_every_existing_table(self):
        db.reset_engine_cache()
        engine = db.make_engine(self.path)
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            command.downgrade(db._alembic_config(connection), "0011")
            tables = [name for name, in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
                      if name != "alembic_version"]
            before = {name: [tuple(row) for row in connection.exec_driver_sql(f'SELECT * FROM "{name}"')] for name in tables}
            command.upgrade(db._alembic_config(connection), "0012")
            self.assertEqual(connection.exec_driver_sql("SELECT COUNT(*) FROM budget_account_display").scalar_one(), 0)
            connection.exec_driver_sql(
                "INSERT INTO budget_account_display (account_id,product,currency,masked_suffix,observed_at,imported_at,digest) "
                "VALUES (?,?,?,?,?,?,?)", (self.account, "Synthetic", "EUR", "0000", 900, 1000, "digest"))
            self.assertEqual(connection.exec_driver_sql("SELECT source FROM budget_account_display").scalar_one(), "offline_import")
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_key_check").all(), [])
            command.downgrade(db._alembic_config(connection), "0011")
            for name in tables:
                self.assertEqual([tuple(row) for row in connection.exec_driver_sql(f'SELECT * FROM "{name}"')], before[name])
            command.upgrade(db._alembic_config(connection), "head")


if __name__ == "__main__":
    unittest.main()
