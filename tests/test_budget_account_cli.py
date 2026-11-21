"""Owner-scoped local account controls never discover accounts or call a bank."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
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
from hestia.budget.store import ACCOUNT_KINDS, BudgetStore
from tests.test_budget_import_cli import import_document
from tests.test_budget_store import configuration, configuration_document


PRIVATE_HASH = "PRIVATE-owned-account-hash"
FOREIGN_HASH = "PRIVATE-foreign-account-hash"


class BudgetAccountCLITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.path = self.root / "accounts.db"
        self.config_path = self.root / "PRIVATE-config.json"
        self.config = configuration(accounts=(PRIVATE_HASH,))
        foreign = replace(self.config.connections[0], id="foreign-bank", owner="other", accounts=(FOREIGN_HASH,))
        self.config = replace(self.config, connections=(*self.config.connections, foreign))
        self.write_config(self.config)
        _, self.sessions = db.init_db(self.path)
        with self.sessions.begin() as session:
            session.add_all([db.User(username="owner", password_hash="unused", role="admin"),
                             db.User(username="other", password_hash="unused", role="admin"),
                             db.User(username="viewer", password_hash="unused", role="viewer"),
                             db.User(username="disabled", password_hash="unused", role="admin", disabled=True)])
        self.store = BudgetStore(self.sessions)

    def tearDown(self):
        db.reset_engine_cache()
        self.directory.cleanup()

    def write_config(self, config):
        self.config_path.write_text(json.dumps(configuration_document(config)), encoding="utf-8")
        self.config_path.chmod(0o600)

    def rows(self, model):
        with self.sessions() as session:
            return list(session.scalars(select(model)))

    def invoke(self, arguments, *, environment=False):
        stdout, stderr = io.StringIO(), io.StringIO()
        prefix = [] if environment else ["--config", str(self.config_path), "--db", str(self.path)]
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = cli.main(prefix + arguments)
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue(), stderr.getvalue()

    def result(self, arguments):
        code, output, diagnostic = self.invoke(arguments)
        self.assertEqual((code, diagnostic), (0, ""))
        self.assert_redacted(output)
        return json.loads(output)

    def account(self, owner="owner"):
        return self.result(["accounts", "--owner", owner])["accounts"][0]

    def action(self, action, account_id, *, owner="owner", kind=None):
        arguments = ["account", action, account_id, "--owner", owner]
        if kind is not None:
            arguments += ["--kind", kind]
        return self.result(arguments)["account"]

    def assert_redacted(self, output):
        for private in (PRIVATE_HASH, FOREIGN_HASH, str(self.config_path), "PRIVATE financial description",
                        "12345678901234567890", "private-transaction-reference", "-999.99"):
            self.assertNotIn(private, output)

    def test_accounts_apply_metadata_and_list_only_one_owner_without_credentials_or_http(self):
        with mock.patch.object(runtime, "credentials") as credentials, \
             mock.patch.object(cli, "run_once") as acquisition, \
             mock.patch("urllib.request.OpenerDirector.open") as http:
            account = self.account()
            foreign = self.account("other")
        for operation in (credentials, acquisition, http):
            operation.assert_not_called()
        self.assertNotEqual(account["id"], foreign["id"])
        self.assertEqual(account["connection"], "bank")
        self.assertEqual(foreign["connection"], "foreign-bank")
        self.assertTrue(account["configured"])
        self.assertFalse(account["selected"])
        self.assertFalse(account["active"])
        self.assertEqual(account["kind"], "unknown")
        for field in ("kind_confirmed_at", "kind_confirmed_by", "kind_confirmation_origin"):
            self.assertIsNone(account[field])
        self.assertEqual(len(self.rows(db.BudgetAccount)), 2)
        self.assertEqual(self.rows(db.BudgetAccountAudit), [])
        with mock.patch.dict(os.environ, {"HESTIA_BUDGET_CONFIG": str(self.config_path), "HESTIA_DB": str(self.path)}):
            code, output, diagnostic = self.invoke(["accounts", "--owner", "owner"], environment=True)
        self.assertEqual((code, diagnostic), (0, ""))
        self.assertEqual(json.loads(output)["accounts"], [account])
        self.assert_redacted(output)

    def test_selection_is_persistent_idempotent_and_audited_with_local_actor(self):
        account_id = self.account()["id"]
        with mock.patch.object(cli.os, "geteuid", return_value=2468), \
             mock.patch.object(runtime, "credentials") as credentials, \
             mock.patch("urllib.request.OpenerDirector.open") as http:
            selected = self.action("select", account_id)
            repeated = self.action("select", account_id)
            listed = self.account()
            deselected = self.action("deselect", account_id)
            repeated_deselection = self.action("deselect", account_id)
        credentials.assert_not_called()
        http.assert_not_called()
        self.assertTrue(selected["changed"])
        self.assertTrue(selected["selected"])
        self.assertTrue(selected["active"])
        self.assertFalse(repeated["changed"])
        self.assertTrue(listed["selected"])
        self.assertTrue(deselected["changed"])
        self.assertFalse(deselected["selected"])
        self.assertFalse(deselected["active"])
        self.assertFalse(repeated_deselection["changed"])
        audit = self.rows(db.BudgetAccountAudit)
        self.assertEqual(len(audit), 2)
        self.assertEqual({row.account_id for row in audit}, {account_id})
        self.assertEqual({row.owner for row in audit}, {"owner"})
        self.assertEqual({row.actor for row in audit}, {"operator:2468"})
        self.assertEqual({row.origin for row in audit}, {"operator"})
        self.assertEqual([(json.loads(row.old_value_json), json.loads(row.new_value_json)) for row in audit],
                         [(False, True), (True, False)])

    def test_kind_requires_explicit_confirmation_is_currency_independent_and_does_not_select(self):
        account_id = self.account()["id"]
        with self.sessions.begin() as session:
            session.get(db.BudgetAccount, account_id).balances_json = json.dumps([
                {"balance_amount": {"amount": "-999.99", "currency": "USD"}},
                {"balance_amount": {"amount": "0", "currency": "EUR"}}])
        with mock.patch.object(cli.os, "geteuid", return_value=1357):
            for kind in sorted(ACCOUNT_KINDS):
                confirmed = self.action("confirm-kind", account_id, kind=kind)
                self.assertTrue(confirmed["changed"])
                self.assertEqual(confirmed["kind"], kind)
                self.assertIsNotNone(confirmed["kind_confirmed_at"])
                self.assertEqual(confirmed["kind_confirmed_by"], "operator:1357")
                self.assertEqual(confirmed["kind_confirmation_origin"], "operator")
                self.assertFalse(confirmed["selected"])
                self.assertFalse(confirmed["active"])
                repeated = self.action("confirm-kind", account_id, kind=kind)
                self.assertFalse(repeated["changed"])
                self.assertEqual(repeated["kind_confirmed_at"], confirmed["kind_confirmed_at"])
        audit = self.rows(db.BudgetAccountAudit)
        self.assertEqual(len(audit), 4)
        self.assertEqual({json.loads(row.new_value_json)["kind"] for row in audit}, ACCOUNT_KINDS)
        self.assertTrue(all(json.loads(row.new_value_json)["confirmed"] for row in audit))
        self.assertEqual(json.loads(audit[0].old_value_json), {"kind": "unknown", "confirmed": False})
        self.assertEqual({row.field for row in audit}, {"kind"})
        self.assertEqual({row.actor for row in audit}, {"operator:1357"})

    def test_missing_viewer_disabled_owner_and_foreign_account_ids_are_rejected(self):
        account_id = self.account()["id"]
        for owner in ("missing", "viewer", "disabled"):
            for arguments in (["accounts", "--owner", owner],
                              ["account", "select", account_id, "--owner", owner],
                              ["account", "confirm-kind", account_id, "--owner", owner, "--kind", "current"]):
                with self.subTest(owner=owner, command=arguments[0]):
                    code, output, diagnostic = self.invoke(arguments)
                    self.assertEqual((code, output), (2, ""))
                    self.assertIn("enabled admin owner", diagnostic)
                    self.assert_redacted(diagnostic)
        for owner, identifier in (("other", account_id), ("owner", "PRIVATE-unknown-local-id")):
            for action in ("select", "deselect", "confirm-kind"):
                arguments = ["account", action, identifier, "--owner", owner]
                if action == "confirm-kind":
                    arguments += ["--kind", "other"]
                code, output, diagnostic = self.invoke(arguments)
                self.assertEqual((code, output), (2, ""))
                self.assertEqual(diagnostic, "Unknown bank account\n")
        self.assertFalse(self.account()["selected"])
        self.assertEqual(self.rows(db.BudgetAccountAudit), [])

    def test_retired_account_cannot_be_selected_but_owner_can_deselect_it(self):
        account_id = self.account()["id"]
        self.action("select", account_id)
        self.write_config(replace(self.config, connections=()))
        retired = self.account()
        self.assertFalse(retired["configured"])
        self.assertTrue(retired["selected"])
        self.assertFalse(retired["active"])
        code, output, diagnostic = self.invoke(["account", "select", account_id, "--owner", "owner"])
        self.assertEqual((code, output), (2, ""))
        self.assert_redacted(diagnostic)
        deselected = self.action("deselect", account_id)
        self.assertTrue(deselected["changed"])
        self.assertFalse(deselected["selected"])
        self.assertEqual(len(self.rows(db.BudgetAccountAudit)), 2)

    def test_offline_import_and_listing_never_confirm_kind_or_select_account(self):
        document = import_document(self.config)
        document["transactions"]["transactions"][0]["entry_reference"] = "private-transaction-reference"
        document["transactions"]["transactions"][0]["remittance_information"] = ["PRIVATE financial description"]
        path = self.root / "PRIVATE-import.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        path.chmod(0o600)
        with mock.patch.object(cli.time, "time", return_value=1795176000), \
             mock.patch.object(runtime, "credentials") as credentials, \
             mock.patch("urllib.request.OpenerDirector.open") as http:
            self.result(["import-snapshot", "--file", str(path)])
            account = self.account()
        credentials.assert_not_called()
        http.assert_not_called()
        self.assertEqual(account["kind"], "unknown")
        self.assertIsNone(account["kind_confirmed_at"])
        self.assertFalse(account["selected"])
        self.assertFalse(account["active"])
        self.assertEqual(len(self.rows(db.BudgetTransaction)), 1)
        self.assertEqual(self.rows(db.BudgetAccountAudit), [])

    def test_lock_contention_blocks_local_controls_and_releases_afterwards(self):
        account_id = self.account()["id"]
        with runtime.budget_lock(self.path):
            for arguments in (["accounts", "--owner", "owner"],
                              ["account", "select", account_id, "--owner", "owner"]):
                code, output, diagnostic = self.invoke(arguments)
                self.assertEqual((code, output), (2, ""))
                self.assertIn("already running", diagnostic)
        self.assertTrue(self.action("select", account_id)["changed"])
        self.assertEqual(len(self.rows(db.BudgetAccountAudit)), 1)

    def test_owner_kind_and_action_are_required_and_actor_cannot_be_supplied(self):
        account_id = self.account()["id"]
        invalid = [["accounts"], ["account"], ["account", "select", account_id],
                   ["account", "confirm-kind", account_id, "--owner", "owner"],
                   ["account", "confirm-kind", account_id, "--owner", "owner", "--kind", "unknown"],
                   ["account", "select", account_id, "--owner", "owner", "--actor", "spoofed"],
                   ["account", "select", account_id, "--owner", "owner", "--origin", "spoofed"]]
        for arguments in invalid:
            with self.subTest(arguments=arguments), mock.patch.object(cli, "execute") as execute:
                code, output, diagnostic = self.invoke(arguments)
                self.assertEqual((code, output), (2, ""))
                self.assertIn("error:", diagnostic)
                execute.assert_not_called()
        self.assertEqual(self.rows(db.BudgetAccountAudit), [])


if __name__ == "__main__":
    unittest.main()
