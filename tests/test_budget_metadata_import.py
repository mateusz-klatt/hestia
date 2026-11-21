"""Optional account labels enrich offline batches without touching the ledger."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import io
import json
from pathlib import Path
from unittest import mock
import unittest

from sqlalchemy import select

from hestia import db
from hestia.budget import __main__ as cli, config, import_file, runtime, store
from hestia.budget.import_file import BudgetImportError, ImportSnapshot, parse_import
from hestia.budget.store import BudgetStoreError
from tests import test_budget_store as fixtures
from tests.test_budget_import_store import quota_state, seed_retry_state


NOW = datetime(2026, 11, 20, 12, tzinfo=timezone.utc).timestamp()


def document(configuration):
    connection = configuration.connections[0]
    transaction = fixtures.transaction()
    money = {key: transaction.pop(key) for key in ("amount", "currency")}
    return {"version": 1, "connection_id": connection.id,
            "aspsp": {"name": connection.bank_name, "country": connection.country},
            "identification_hash": connection.accounts[0], "fetched_at": "2026-11-20T11:30:00Z",
            "complete": True, "transactions": {"transactions": [{**transaction, "transaction_amount": money}]}}


def metadata(**changes):
    return {"product": "PRIVATE Synthetic Current Account", "currency": "ZZZ", "masked_suffix": "A1B2",
            "observed_at": "2026-11-20T11:00:00Z", **changes}


class BudgetMetadataImportTests(unittest.TestCase):
    setUp_database = fixtures.BudgetStoreTests.setUp
    tearDown = fixtures.BudgetStoreTests.tearDown
    rows = fixtures.BudgetStoreTests.rows

    def setUp(self):
        self.setUp_database()
        self.now = NOW
        self.config = fixtures.configuration()
        self.raw = document(self.config)

    def parse(self, account=None, *, raw=None, configuration=None):
        value = deepcopy(self.raw if raw is None else raw)
        if account is not None:
            value["account"] = account
        return parse_import(value, self.config if configuration is None else configuration, now=self.now)

    def tables(self, *, include_display=True):
        with self.sessions() as session:
            return {table.name: sorted([tuple(row) for row in session.execute(select(table))], key=repr)
                    for table in db.Base.metadata.sorted_tables
                    if include_display or table.name != db.BudgetAccountDisplay.__tablename__}

    def invoke(self, raw, *, database=None):
        root = Path(self.directory.name)
        config_path, snapshot_path = root / "PRIVATE-config.json", root / "PRIVATE-snapshot.json"
        config_path.write_text(json.dumps(fixtures.configuration_document(self.config)), encoding="utf-8")
        snapshot_path.write_text(json.dumps(raw), encoding="utf-8")
        config_path.chmod(0o600)
        snapshot_path.chmod(0o600)
        stdout, stderr = io.StringIO(), io.StringIO()
        target = self.path if database is None else database
        with mock.patch.object(cli.time, "time", return_value=self.now), redirect_stdout(stdout), redirect_stderr(stderr):
            result = cli.main(["--config", str(config_path), "--db", str(target),
                               "import-snapshot", "--file", str(snapshot_path)])
        return result, stdout.getvalue(), stderr.getvalue()

    def test_optional_metadata_keeps_legacy_constructor_fingerprint_and_exact_report(self):
        plain = self.parse()
        legacy = ImportSnapshot(plain.connection_id, plain.identification_hash, plain.fetched_at,
                                plain.transactions, plain.balances, plain.digest)
        self.assertEqual(legacy, plain)
        self.assertIsNone(plain.metadata)
        self.assertEqual(plain.digest, "0404a53061566f9bc73900df3ad525824e425b0bf7ef0c1498efc261a8188f00")
        enriched = self.parse(metadata())
        self.assertEqual(enriched.digest, plain.digest)
        self.assertEqual((enriched.transactions, enriched.balances), (plain.transactions, plain.balances))
        report = self.store.import_snapshot(self.config, plain)
        self.assertEqual(report, {"batch_id": self.rows(db.BudgetImportBatch)[0].id, "duplicate": False,
                                  "inserted": 1, "updated": 0, "preserved_api": 0, "stale": 0,
                                  "unresolved": 0, "unchanged": 0})
        self.assertEqual(json.loads(self.rows(db.BudgetImportBatch)[0].report_json), report)
        self.assertEqual(self.rows(db.BudgetAccountDisplay), [])
        with mock.patch.object(store, "apply_account_metadata") as apply:
            replay = self.store.import_snapshot(self.config, plain)
        apply.assert_not_called()
        self.assertNotIn("metadata", replay)

    def test_metadata_digest_uses_trusted_account_context_and_not_ledger_payload(self):
        from hestia.budget.account_metadata import parse_account_metadata

        with mock.patch.object(import_file, "parse_account_metadata", wraps=parse_account_metadata) as parse:
            first = self.parse(metadata())
        connection = self.config.connections[0]
        parse.assert_called_once_with(metadata(), now=self.now, context={
            "application_id": self.config.application_id, "owner": connection.owner,
            "connection_id": connection.id, "aspsp": {"name": connection.bank_name, "country": connection.country},
            "identification_hash": connection.accounts[0]})
        changed_ledger = deepcopy(self.raw)
        changed_ledger["fetched_at"] = "2026-11-20T11:45:00Z"
        changed_ledger["transactions"]["transactions"][0]["remittance_information"] = ["Different ledger"]
        changed = self.parse(metadata(), raw=changed_ledger)
        self.assertNotEqual(changed.digest, first.digest)
        self.assertEqual(changed.metadata, first.metadata)
        other_config = replace(self.config, application_id="00000000-0000-4000-8000-000000000999")
        other = self.parse(metadata(), configuration=other_config)
        self.assertNotEqual(other.metadata.digest, first.metadata.digest)

    def test_previously_imported_batch_can_be_enriched_without_reprocessing_or_report_changes(self):
        plain = self.parse()
        original = self.store.import_snapshot(self.config, plain)
        before = self.tables(include_display=False)
        with mock.patch.object(self.store, "_offline_transaction") as ledger, \
             mock.patch.object(self.store, "_restore_api_conflicts") as conflicts:
            report = self.store.import_snapshot(self.config, self.parse(metadata()))
            repeated = self.store.import_snapshot(self.config, self.parse(metadata()))
        ledger.assert_not_called()
        conflicts.assert_not_called()
        self.assertEqual(report["metadata"], {"state": "updated", "product_rejected": 0})
        self.assertEqual(repeated["metadata"], {"state": "unchanged", "product_rejected": 0})
        for result in (report, repeated):
            self.assertTrue(result["duplicate"])
            self.assertEqual(result["batch_id"], original["batch_id"])
            self.assertTrue(all(result[key] == 0 for key in ("inserted", "updated", "preserved_api", "stale", "unresolved", "unchanged")))
        self.assertEqual(self.tables(include_display=False), before)
        display = self.rows(db.BudgetAccountDisplay)[0]
        self.assertEqual((display.product, display.currency, display.masked_suffix),
                         (metadata()["product"], "ZZZ", "A1B2"))
        self.assertEqual(display.observed_at, NOW - 3600)
        self.assertEqual(display.imported_at, NOW)

    def test_new_enriched_batch_saves_both_and_does_not_persist_metadata_in_ledger_report(self):
        report = self.store.import_snapshot(self.config, self.parse(metadata()))
        self.assertEqual((report["duplicate"], report["inserted"], report["metadata"]),
                         (False, 1, {"state": "updated", "product_rejected": 0}))
        self.assertNotIn("metadata", json.loads(self.rows(db.BudgetImportBatch)[0].report_json))
        self.assertFalse(self.rows(db.BudgetAccount)[0].enabled)
        self.assertFalse(self.rows(db.BudgetAccount)[0].configured)
        self.assertEqual(self.rows(db.BudgetComponent), [])

    def test_metadata_observation_order_empty_values_and_equal_time_conflict_are_atomic(self):
        self.store.import_snapshot(self.config, self.parse(metadata()))
        before = self.tables()
        older = self.store.import_snapshot(self.config, self.parse(metadata(product="Older", observed_at="2026-11-20T10:00:00Z")))
        self.assertEqual(older["metadata"], {"state": "older", "product_rejected": 0})
        empty = self.store.import_snapshot(self.config, self.parse(metadata(product=None, currency=None, masked_suffix=None,
                                                                           observed_at="2026-11-20T11:30:00Z")))
        self.assertEqual(empty["metadata"], {"state": "empty", "product_rejected": 0})
        self.assertEqual(self.tables(), before)
        with self.assertRaises(BudgetStoreError) as caught:
            self.store.import_snapshot(self.config, self.parse(metadata(product="PRIVATE Conflict")))
        self.assertEqual(str(caught.exception), "Invalid account display metadata: metadata_conflict")
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertEqual(self.tables(), before)
        newer = self.store.import_snapshot(self.config, self.parse(metadata(product="New label", currency="EUR",
                                                                           observed_at="2026-11-20T11:30:00Z")))
        self.assertEqual(newer["metadata"], {"state": "updated", "product_rejected": 0})
        display = self.rows(db.BudgetAccountDisplay)[0]
        self.assertEqual((display.product, display.currency, display.observed_at), ("New label", "EUR", NOW - 1800))
        self.assertEqual(self.tables(include_display=False), {key: value for key, value in before.items()
                                                             if key != db.BudgetAccountDisplay.__tablename__})

    def test_enrichment_preserves_api_history_selection_kind_versions_quota_and_pause(self):
        self.store.import_snapshot(self.config, self.parse())
        fixtures.prepare_selected(self.store, self.config)
        account_id = self.store.accounts("owner")[0]["id"]
        self.store.confirm_account_kind("owner", account_id, "credit_card", actor="operator:123")
        self.store.commit_snapshot("bank", fixtures.snapshot(fixtures.transaction(amount="99"), valid_until=NOW + 31536000))
        self.store.set_connection_paused("owner", "bank", True, actor="operator:123")
        seed_retry_state(self.sessions, account_id, self.now)
        before, quotas = self.tables(include_display=False), quota_state(self.rows)
        with mock.patch.object(config, "credentials") as credentials, mock.patch.object(runtime, "credentials") as runtime_credentials, \
             mock.patch("urllib.request.OpenerDirector.open") as network:
            result = self.store.import_snapshot(self.config, self.parse(metadata()))
        for operation in (credentials, runtime_credentials, network):
            operation.assert_not_called()
        self.assertEqual(result["metadata"], {"state": "updated", "product_rejected": 0})
        self.assertEqual(self.tables(include_display=False), before)
        self.assertEqual(quota_state(self.rows), quotas)

    def test_owner_and_immutable_context_are_rechecked_before_duplicate_enrichment(self):
        self.store.import_snapshot(self.config, self.parse())
        before = self.tables()
        other = replace(self.config, connections=(replace(self.config.connections[0], owner="other"),))
        with self.assertRaises(BudgetStoreError):
            self.store.import_snapshot(other, self.parse(metadata(), configuration=other))
        self.assertEqual(self.tables(), before)
        with self.sessions.begin() as session:
            session.get(db.User, "owner").disabled = True
        before = self.tables()
        with self.assertRaises(BudgetStoreError):
            self.store.import_snapshot(self.config, self.parse(metadata()))
        self.assertEqual(self.tables(), before)

    def test_write_failure_rolls_back_display_account_batch_and_ledger_together(self):
        enriched = self.parse(metadata())
        before = self.tables()
        with mock.patch.object(self.store, "_offline_transaction", side_effect=RuntimeError("PRIVATE storage failure")), \
             self.assertRaises(RuntimeError):
            self.store.import_snapshot(self.config, enriched)
        self.assertEqual(self.tables(), before)
        self.assertEqual(self.store.import_snapshot(self.config, enriched)["metadata"], {"state": "updated", "product_rejected": 0})
        updated = self.parse(metadata(product="Updated", observed_at="2026-11-20T11:30:00Z"))
        before = self.tables()
        with mock.patch.object(store, "apply_account_metadata", side_effect=store.BudgetMetadataError("invalid_metadata")), \
             self.assertRaises(BudgetStoreError) as caught:
            self.store.import_snapshot(self.config, updated)
        self.assertEqual(str(caught.exception), "Invalid account display metadata: invalid_metadata")
        self.assertEqual(self.tables(), before)

    def test_malformed_metadata_is_fully_rejected_before_database_initialization(self):
        invalid = [None, [], {}, {**metadata(), "owner": "PRIVATE forged owner"},
                   metadata(currency="pln"), metadata(currency="EUR1"), metadata(masked_suffix="12345"),
                   metadata(masked_suffix="ąBCD"), metadata(product=123),
                   metadata(observed_at="2026-11-21T12:00:00Z"), metadata(observed_at="2026-11-20T11:00:00")]
        missing_observation = metadata(); del missing_observation["observed_at"]
        invalid.append(missing_observation)
        target = Path(self.directory.name) / "must-not-be-created.db"
        for account in invalid:
            with self.subTest(account_type=type(account).__name__):
                raw = {**self.raw, "account": account}
                before = self.tables()
                with mock.patch.object(db, "init_db") as initialize, mock.patch.object(cli, "budget_lock") as lock:
                    code, output, diagnostic = self.invoke(raw, database=target)
                self.assertEqual((code, output), (2, ""))
                self.assertNotIn("PRIVATE", diagnostic)
                initialize.assert_not_called()
                lock.assert_not_called()
                self.assertFalse(target.exists())
                self.assertEqual(self.tables(), before)
                with self.assertRaises(BudgetImportError) as caught:
                    parse_import(raw, self.config, now=self.now)
                self.assertEqual(caught.exception.code, "invalid_metadata")

    def test_cli_returns_only_safe_metadata_state_and_keeps_duplicate_batch_unchanged(self):
        raw = {**self.raw, "account": metadata()}
        with mock.patch.object(config, "credentials") as credentials, mock.patch.object(runtime, "credentials") as runtime_credentials, \
             mock.patch("urllib.request.OpenerDirector.open") as network, mock.patch.object(store.BudgetStore, "configure") as configure:
            code, output, diagnostic = self.invoke(raw)
            again_code, again, again_diagnostic = self.invoke(raw)
        self.assertEqual((code, diagnostic, again_code, again_diagnostic), (0, "", 0, ""))
        for operation in (credentials, runtime_credentials, network, configure):
            operation.assert_not_called()
        first, second = json.loads(output), json.loads(again)
        self.assertEqual(first["import"]["metadata"], {"state": "updated", "product_rejected": 0})
        self.assertEqual(second["import"]["metadata"], {"state": "unchanged", "product_rejected": 0})
        self.assertEqual(first["import"]["batch_id"], second["import"]["batch_id"])
        self.assertEqual(len(self.rows(db.BudgetImportBatch)), 1)
        self.assertEqual(len(self.rows(db.BudgetTransaction)), 1)
        for private in (metadata()["product"], "ZZZ", "A1B2", self.config.connections[0].accounts[0],
                        "12345678901234567890", "Synthetic purchase"):
            self.assertNotIn(private, output + again)
        conflict_code, conflict_output, conflict_error = self.invoke({**raw, "account": metadata(product="PRIVATE conflict")})
        self.assertEqual((conflict_code, conflict_output), (2, ""))
        self.assertEqual(conflict_error.strip(), "Invalid account display metadata: metadata_conflict")
        self.assertNotIn("PRIVATE", conflict_error)

    def test_identifier_like_product_is_omitted_without_blocking_ledger_or_leaking_raw_text(self):
        unsafe = "PRIVATE Account 12345678"
        raw = {**self.raw, "account": metadata(product=unsafe)}
        parsed = parse_import(raw, self.config, now=self.now)
        normalized = self.parse(metadata(product=None))
        self.assertIsNone(parsed.metadata.product)
        self.assertTrue(parsed.metadata.product_rejected)
        self.assertEqual(parsed.metadata.digest, normalized.metadata.digest)
        code, output, diagnostic = self.invoke(raw)
        self.assertEqual((code, diagnostic), (0, ""))
        self.assertEqual(json.loads(output)["import"]["metadata"], {"state": "updated", "product_rejected": 1})
        self.assertEqual(len(self.rows(db.BudgetTransaction)), 1)
        self.assertIsNone(self.rows(db.BudgetAccountDisplay)[0].product)
        self.assertNotIn(unsafe, output + repr(self.tables()))
        repeated = self.store.import_snapshot(self.config, self.parse(metadata(product="Konto2024")))
        self.assertEqual(repeated["metadata"], {"state": "unchanged", "product_rejected": 1})

    def test_metadata_conflict_rejects_new_ledger_batch_without_any_partial_writes(self):
        self.store.import_snapshot(self.config, self.parse(metadata()))
        before = self.tables()
        raw = deepcopy(self.raw)
        raw["transactions"]["transactions"][0]["entry_reference"] = "new-transaction-must-not-appear"
        raw["account"] = metadata(currency="EUR")
        parsed = parse_import(raw, self.config, now=self.now)
        self.assertNotEqual(parsed.digest, self.parse().digest)
        with self.assertRaises(BudgetStoreError):
            self.store.import_snapshot(self.config, parsed)
        self.assertEqual(self.tables(), before)
        code, output, diagnostic = self.invoke(raw)
        self.assertEqual((code, output), (2, ""))
        self.assertEqual(diagnostic.strip(), "Invalid account display metadata: metadata_conflict")
        self.assertNotIn("PRIVATE", diagnostic)
        self.assertEqual(self.tables(), before)

    def test_cli_metadata_error_codes_remain_allowlisted_and_never_include_source_details(self):
        raw = {**self.raw, "account": metadata()}
        secret_values = [metadata()["product"], self.config.connections[0].accounts[0], "https://PRIVATE-provider.test/account"]
        for supplied_code, expected in (("metadata_conflict", "metadata_conflict"),
                                        ("invalid_metadata", "invalid_metadata"),
                                        (" ".join(secret_values), "invalid_metadata")):
            with self.subTest(expected=expected), \
                 mock.patch.object(store, "apply_account_metadata", side_effect=store.BudgetMetadataError(supplied_code)):
                before = self.tables()
                code, output, diagnostic = self.invoke(raw)
            self.assertEqual((code, output), (2, ""))
            self.assertEqual(diagnostic.strip(), "Invalid account display metadata: " + expected)
            for value in secret_values:
                self.assertNotIn(value, diagnostic)
            self.assertEqual(self.tables(), before)
