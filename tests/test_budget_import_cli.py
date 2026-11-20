"""Private synthetic files reach SQLite without credentials, HTTP or quota changes."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from sqlalchemy import select

from hestia import db
from hestia.budget import __main__ as cli
from hestia.budget import runtime
from hestia.budget.store import BudgetStore
from tests.test_budget_store import configuration, prepare_selected, snapshot, transaction


PRIVATE_HASH = "PRIVATE-account-hash-never-log"
PRIVATE_DESCRIPTION = "PRIVATE synthetic financial description"


def import_document(config):
    connection = config.connections[0]
    tx = transaction("PRIVATE-entry-reference", remittance_information=[PRIVATE_DESCRIPTION])
    amount = {"amount": tx.pop("amount"), "currency": tx.pop("currency")}
    return {"version": 1, "connection_id": connection.id,
            "aspsp": {"name": connection.bank_name, "country": connection.country},
            "identification_hash": connection.accounts[0], "fetched_at": "2026-11-20T11:00:00+00:00",
            "complete": True, "transactions": {"transactions": [{**tx, "transaction_amount": amount}]},
            "balances": {"balances": [{"name": "Booked", "balance_type": "CLBD",
                                       "balance_amount": {"amount": "-12.34500", "currency": "PLN"}}]}}


class BudgetImportCLITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.path = self.root / "synthetic.db"
        self.config_path = self.root / "PRIVATE-config.json"
        self.file = self.root / "PRIVATE-snapshot.json"
        self.config = configuration(accounts=(PRIVATE_HASH,))
        self.now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc).timestamp()
        self.write_config(self.config)
        self.write_import(import_document(self.config))

    def tearDown(self):
        db.reset_engine_cache()
        self.directory.cleanup()

    def write_config(self, config):
        self.config_path.write_text(json.dumps({"version": 1, **asdict(config)}), encoding="utf-8")
        self.config_path.chmod(0o600)

    def write_import(self, document):
        self.file.write_text(json.dumps(document), encoding="utf-8")
        self.file.chmod(0o600)

    def initialize(self):
        _, self.sessions = db.init_db(self.path)
        with self.sessions.begin() as session:
            session.add_all([db.User(username="owner", password_hash="unused", role="admin"),
                             db.User(username="viewer", password_hash="unused", role="viewer")])

    def rows(self, model):
        with self.sessions() as session:
            return list(session.scalars(select(model)))

    def invoke(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(cli.time, "time", return_value=self.now), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = cli.main(arguments)
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue(), stderr.getvalue()

    def arguments(self, *, path=None):
        return ["--config", str(self.config_path), "--db", str(self.path if path is None else path),
                "import-snapshot", "--file", str(self.file)]

    def assert_redacted(self, output):
        for private in (PRIVATE_HASH, PRIVATE_DESCRIPTION, self.file.name, str(self.file),
                        "12345678901234567890", "PRIVATE-entry-reference", "-12.345"):
            self.assertNotIn(private, output)

    def test_real_parser_import_and_idempotent_repeat_need_no_credentials_http_or_configure(self):
        self.initialize()
        with mock.patch.object(runtime, "credentials") as credentials, \
             mock.patch.object(cli, "run_once") as acquisition, \
             mock.patch.object(BudgetStore, "configure") as configure, \
             mock.patch("urllib.request.OpenerDirector.open") as http:
            code, output, diagnostic = self.invoke(self.arguments())
            self.assertEqual((code, diagnostic), (0, ""))
            first = json.loads(output)
            self.assertFalse(first["import"]["duplicate"])
            self.assertEqual(first["import"]["inserted"], 1)
            code, repeated, diagnostic = self.invoke(self.arguments())
        for operation in (credentials, acquisition, configure, http):
            operation.assert_not_called()
        self.assertEqual((code, diagnostic), (0, ""))
        second = json.loads(repeated)
        self.assertTrue(second["import"]["duplicate"])
        self.assertEqual(second["import"]["batch_id"], first["import"]["batch_id"])
        self.assertEqual(len(self.rows(db.BudgetTransaction)), 1)
        tx = json.loads(self.rows(db.BudgetTransaction)[0].payload_json)
        self.assertEqual(tx["amount"], "12345678901234567890.12345")
        self.assertEqual(tx["remittance_information"], [PRIVATE_DESCRIPTION])
        account = self.rows(db.BudgetAccount)[0]
        self.assertEqual(json.loads(account.balances_json), [])
        imported_balance = json.loads(self.rows(db.BudgetImportBatch)[0].balances_json)
        self.assertEqual(imported_balance[0]["balance_amount"]["amount"], "-12.345")
        self.assertFalse(self.rows(db.BudgetConnection)[0].enabled)
        self.assertFalse(account.enabled)
        self.assertFalse(account.configured)
        self.assertEqual(self.rows(db.BudgetComponent), [])
        self.assertIsNone(self.rows(db.BudgetConnection)[0].last_attempt)
        self.assertEqual(self.rows(db.BudgetConnection)[0].next_attempt, 0)
        self.assert_redacted(output + repeated)

    def test_existing_connection_selection_quota_errors_and_other_accounts_are_untouched(self):
        self.initialize()
        original = self.config.connections[0]
        other = replace(original, id="other-bank", accounts=("other-private-hash",))
        live_config = replace(self.config, connections=(original, other))
        store = BudgetStore(self.sessions, clock=lambda: self.now)
        prepare_selected(store, live_config)
        store.start_attempt("bank")
        store.record_failure("bank", "rate_limit")
        store.start_attempt("other-bank")
        store.commit_snapshot("other-bank", snapshot(transaction("other-reference"),
                                                     account_hash="other-private-hash", valid_until=self.now + 31536000))
        before = store.status()
        code, output, diagnostic = self.invoke(self.arguments())
        self.assertEqual((code, diagnostic), (0, ""))
        after = store.status()
        for field in ("enabled", "last_attempt", "last_success", "next_attempt", "last_error", "failures"):
            self.assertEqual(after[0][field], before[0][field])
        self.assertEqual(after[0]["accounts"], before[0]["accounts"])
        self.assertEqual(after[1], before[1])
        self.assertEqual(len(self.rows(db.BudgetAccount)), 2)
        self.assertTrue(all(account.enabled for account in self.rows(db.BudgetAccount)))
        self.assertEqual({row.entry_reference for row in self.rows(db.BudgetTransaction)},
                         {"other-reference", "PRIVATE-entry-reference"})
        self.assert_redacted(output)

    def test_default_environment_database_and_configuration_are_supported(self):
        self.initialize()
        with mock.patch.dict(os.environ, {"HESTIA_DB": str(self.path), "HESTIA_BUDGET_CONFIG": str(self.config_path)}):
            code, output, diagnostic = self.invoke(["import-snapshot", "--file", str(self.file)])
        self.assertEqual((code, diagnostic), (0, ""))
        self.assertEqual(json.loads(output)["import"]["inserted"], 1)
        self.assertEqual(len(self.rows(db.BudgetTransaction)), 1)
        self.assert_redacted(output)

    def test_stored_only_status_preserves_disabled_import_metadata_and_existing_quota(self):
        self.initialize()
        code, _, diagnostic = self.invoke(self.arguments())
        self.assertEqual((code, diagnostic), (0, ""))
        store = BudgetStore(self.sessions, clock=lambda: self.now)
        store.record_failure("bank", "rate_limit")
        before = store.status()
        self.assertFalse(before[0]["enabled"])
        self.assertFalse(before[0]["accounts"][0]["enabled"])
        self.assertGreater(before[0]["next_attempt"], self.now)
        with mock.patch.object(runtime, "credentials") as credentials, \
             mock.patch.object(cli, "run_once") as acquisition, \
             mock.patch.object(BudgetStore, "configure") as configure, \
             mock.patch("urllib.request.OpenerDirector.open") as http:
            code, output, diagnostic = self.invoke(["--config", str(self.config_path), "--db", str(self.path),
                                                    "status", "--stored-only"])
        self.assertEqual((code, diagnostic), (0, ""))
        for operation in (credentials, acquisition, configure, http):
            operation.assert_not_called()
        self.assertEqual(json.loads(output)["status"], before)
        self.assertEqual(store.status(), before)
        self.assert_redacted(output)

    def test_entire_input_is_validated_before_database_or_lock_creation(self):
        original = import_document(self.config)
        documents = [dict(original, connection_id="PRIVATE-foreign-connection"),
                     dict(original, identification_hash="PRIVATE-unselected-account"),
                     dict(original, aspsp={"name": "PRIVATE-wrong-bank", "country": "PL"}),
                     dict(original, complete=False)]
        invalid_late_row = deepcopy(original)
        invalid_late_row["transactions"]["transactions"].append({"PRIVATE": "invalid late record"})
        documents.append(invalid_late_row)
        for document in documents:
            with self.subTest(document=document.keys()):
                self.write_import(document)
                with mock.patch.object(db, "init_db") as initialize, \
                     mock.patch.object(cli, "budget_lock") as lock:
                    code, output, diagnostic = self.invoke(self.arguments())
                self.assertEqual((code, output), (2, ""))
                self.assert_redacted(diagnostic)
                self.assertNotIn("PRIVATE", diagnostic)
                initialize.assert_not_called()
                lock.assert_not_called()
                self.assertFalse(self.path.exists())
                self.assertFalse(Path(str(self.path) + ".budget-sync.lock").exists())

    def test_unreadable_malformed_and_nonprivate_files_are_safe_exit_two_before_db_creation(self):
        for content in (b"PRIVATE malformed JSON", b"\xff"):
            self.file.write_bytes(content)
            code, output, diagnostic = self.invoke(self.arguments())
            self.assertEqual((code, output), (2, ""))
            self.assert_redacted(diagnostic)
            self.assertNotIn("PRIVATE", diagnostic)
            self.assertFalse(self.path.exists())
        self.write_import(import_document(self.config))
        self.file.chmod(0o644)
        code, output, diagnostic = self.invoke(self.arguments())
        self.assertEqual((code, output), (2, ""))
        self.assert_redacted(diagnostic)
        self.assertFalse(self.path.exists())
        self.file.unlink()
        code, output, diagnostic = self.invoke(self.arguments())
        self.assertEqual((code, output), (2, ""))
        self.assert_redacted(diagnostic)
        self.assertFalse(self.path.exists())

    def test_invalid_config_precedes_import_read_and_database_creation(self):
        self.config_path.write_text('{"PRIVATE":"invalid configuration"}', encoding="utf-8")
        with mock.patch.object(cli, "load_import") as load, mock.patch.object(db, "init_db") as initialize:
            code, output, diagnostic = self.invoke(self.arguments())
        self.assertEqual((code, output), (2, ""))
        self.assertNotIn("PRIVATE", diagnostic)
        self.assertFalse(self.path.exists())
        load.assert_not_called()
        initialize.assert_not_called()

    def test_non_admin_owner_is_rejected_atomically_without_new_metadata(self):
        self.initialize()
        self.write_config(configuration(accounts=(PRIVATE_HASH,), owner="viewer"))
        code, output, diagnostic = self.invoke(self.arguments())
        self.assertEqual((code, output), (2, ""))
        self.assertIn("enabled admin owner", diagnostic)
        self.assertEqual(self.rows(db.BudgetConnection), [])
        self.assertEqual(self.rows(db.BudgetAccount), [])
        self.assertEqual(self.rows(db.BudgetTransaction), [])
        self.assert_redacted(diagnostic)

    def test_lock_contention_prevents_import_and_releases_afterwards(self):
        self.initialize()
        with runtime.budget_lock(self.path):
            code, output, diagnostic = self.invoke(self.arguments())
        self.assertEqual((code, output), (2, ""))
        self.assertIn("already running", diagnostic)
        self.assertEqual(self.rows(db.BudgetTransaction), [])
        code, output, diagnostic = self.invoke(self.arguments())
        self.assertEqual((code, diagnostic), (0, ""))
        self.assertEqual(json.loads(output)["import"]["inserted"], 1)

    def test_storage_failure_is_redacted_and_existing_data_survives(self):
        self.initialize()
        store = BudgetStore(self.sessions, clock=lambda: self.now)
        prepare_selected(store, self.config)
        store.commit_snapshot("bank", snapshot(transaction("committed"), account_hash=PRIVATE_HASH,
                                              valid_until=self.now + 31536000))
        before = store.status()
        with mock.patch.object(BudgetStore, "import_snapshot", side_effect=RuntimeError("PRIVATE SQL and bank payload")):
            code, output, diagnostic = self.invoke(self.arguments())
        self.assertEqual((code, output), (1, ""))
        self.assertEqual(diagnostic, "Bank acquisition failed; previously committed data is retained\n")
        self.assertEqual(store.status(), before)
        self.assertEqual(self.rows(db.BudgetTransaction)[0].entry_reference, "committed")

    def test_import_requires_file_and_other_commands_reject_it_before_execution(self):
        for arguments in (["import-snapshot"], ["status", "--file", "unused"], ["once", "--file", "unused"]):
            with self.subTest(arguments=arguments), mock.patch.object(cli, "execute") as execute:
                code, output, diagnostic = self.invoke(arguments)
            self.assertEqual((code, output), (2, ""))
            self.assertIn("error:", diagnostic)
            execute.assert_not_called()
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
