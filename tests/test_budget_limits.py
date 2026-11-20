"""Operator suspension recovery preserves server deadlines, quota and ownership."""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from sqlalchemy import delete, select

from hestia import db
from hestia.budget import __main__ as cli, runtime
from hestia.budget.client import BudgetAPIError
from hestia.budget.limits import BudgetLimits, LIMIT_SCOPES
from hestia.budget.rate_limit import RateScope
from hestia.budget.scheduler import BudgetScheduler, provider_key
from hestia.budget.store import BudgetStore, BudgetStoreError
from tests.test_budget_store import APP, configuration, prepare_selected, snapshot, transaction


class BudgetLimitTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "limits.db"
        self.config_path = Path(self.directory.name) / "PRIVATE-config.json"
        _, self.sessions = db.init_db(self.path)
        with self.sessions.begin() as session:
            session.add_all([db.User(username="owner", password_hash="unused", role="admin"),
                             db.User(username="other", password_hash="unused", role="admin"),
                             db.User(username="viewer", password_hash="unused", role="viewer"),
                             db.User(username="disabled", password_hash="unused", role="admin", disabled=True)])
        self.now = 1795176000.0
        self.config = configuration(accounts=("PRIVATE-account-hash",))
        foreign = replace(self.config.connections[0], id="foreign-bank", owner="other", bank_name="Other Bank",
                          accounts=("PRIVATE-foreign-hash",))
        self.config = replace(self.config, connections=(*self.config.connections, foreign))
        self.config_path.write_text(json.dumps({"version": 1, **asdict(self.config)}), encoding="utf-8")
        self.store = BudgetStore(self.sessions, clock=lambda: self.now)
        prepare_selected(self.store, self.config)
        self.limits = BudgetLimits(self.store, self.config)
        self.scheduler = BudgetScheduler(self.store, application_id=APP)
        self.connection = self.config.connections[0]
        accounts = self.rows(db.BudgetAccount)
        self.account_id = next(row.id for row in accounts if row.owner == "owner")
        self.foreign_id = next(row.id for row in accounts if row.owner == "other")

    def tearDown(self):
        db.reset_engine_cache()
        self.directory.cleanup()

    def rows(self, model):
        with self.sessions() as session:
            return list(session.scalars(select(model)))

    def stable_state(self):
        models = (db.BudgetComponent, db.BudgetRequestLimit, db.BudgetAccount, db.BudgetConnection,
                  db.BudgetAcquisition, db.BudgetTransaction, db.BudgetUnresolved, db.BudgetAccountAudit)
        return {model.__tablename__: sorted(
            [tuple(getattr(row, column.name) for column in model.__table__.columns if column.name != "suspended")
             for row in self.rows(model)], key=repr) for model in models}

    def shared_key(self, scope):
        return {"connection": "bank", "application": APP, "provider": provider_key("Synthetic Bank", "PL")}[scope]

    def seed(self, scope):
        with self.sessions.begin() as session:
            if scope == "component":
                row = session.get(db.BudgetComponent, (self.account_id, "transactions"))
                row.backoff_until = self.now + 21600
                row.attempt_id, row.lease_until = "synthetic-lease", self.now + 900
            else:
                row = db.BudgetRequestLimit(scope=scope, key=self.shared_key(scope),
                                            next_request_at=self.now + 1, last_request_at=self.now - 1)
                session.add(row)
            row.next_attempt, row.retry_at, row.suspended = self.now + 43200, self.now + 691200, True

    def resume(self, scope, **kwargs):
        selectors = {"account_id": self.account_id, "operation": "transactions"} if scope == "component" else {}
        return self.limits.resume("owner", "bank", scope, actor="operator:1000", **selectors, **kwargs)

    def invoke(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = cli.main(["--config", str(self.config_path), "--db", str(self.path), "limits", *arguments])
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue(), stderr.getvalue()

    def assert_redacted(self, output):
        for value in ("PRIVATE", APP, provider_key("Synthetic Bank", "PL"), "12345678901234567890", "Retry-After"):
            self.assertNotIn(value, output)

    def test_list_is_owner_scoped_sanitized_and_never_creates_missing_limits(self):
        self.seed("component")
        self.seed("provider")
        before = self.stable_state()
        listed = self.limits.list("owner")
        self.assertEqual(len(listed), 5)
        self.assertEqual({item["connection"] for item in listed}, {"bank"})
        self.assertEqual({item["scope"] for item in listed}, LIMIT_SCOPES)
        components = [item for item in listed if item["scope"] == "component"]
        self.assertEqual({item["account"] for item in components}, {self.account_id})
        self.assertEqual({item["operation"] for item in components}, {"transactions", "balances"})
        self.assertTrue(next(item for item in components if item["operation"] == "transactions")["suspended"])
        self.assert_redacted(json.dumps(listed))
        self.assertEqual(self.stable_state(), before)
        self.assertEqual(len(self.rows(db.BudgetRequestLimit)), 1)
        self.assertEqual({item["connection"] for item in self.limits.list("other")}, {"foreign-bank"})
        self.assertEqual(BudgetLimits(self.store, replace(self.config, connections=())).list("owner"), [])

    def test_all_scopes_clear_only_suspension_and_append_atomic_boolean_audit(self):
        self.store.commit_snapshot("bank", snapshot(transaction(), account_hash="PRIVATE-account-hash",
                                                    valid_until=self.now + 31536000))
        self.store.set_account_selected("owner", self.account_id, False, actor="operator")
        for scope in sorted(LIMIT_SCOPES):
            self.seed(scope)
        before = self.stable_state()
        for scope in sorted(LIMIT_SCOPES):
            result = self.resume(scope)
            self.assertTrue(result["changed"])
            self.assertFalse(result["suspended"])
            self.assertEqual(result["retry_at"], self.now + 691200)
            self.assertEqual(self.stable_state(), before)
            self.assertFalse(self.resume(scope)["changed"])
        audit = self.rows(db.BudgetLimitAudit)
        self.assertEqual(len(audit), 4)
        self.assertEqual({row.scope for row in audit}, LIMIT_SCOPES)
        self.assertEqual({row.owner for row in audit}, {"owner"})
        self.assertEqual({row.actor for row in audit}, {"operator:1000"})
        self.assertEqual({row.origin for row in audit}, {"operator"})
        self.assertEqual({row.field for row in audit}, {"suspended"})
        self.assertTrue(all(json.loads(row.old_value_json) is True and json.loads(row.new_value_json) is False for row in audit))
        self.assertEqual(next(row.target_key for row in audit if row.scope == "component"), self.account_id + ":transactions")
        self.assertFalse(next(row.enabled for row in self.rows(db.BudgetAccount) if row.id == self.account_id))

    def test_oversized_hint_resume_after_six_hours_does_not_bypass_endpoint_quota(self):
        self.assertTrue(self.scheduler.reserve_component(self.connection, "PRIVATE-account-hash", "transactions",
                                                         attempt_id="original", valid_until=self.now + 31536000))
        self.scheduler.record_rate_limit(self.connection, primary="PRIVATE-account-hash", operation="transactions",
                                         error=BudgetAPIError(429, "rate_limit", True, retry_suspended=True,
                                                              rate_scope=RateScope.ACCOUNT_ENDPOINT))
        original = self.stable_state()
        self.now += 21600
        result = self.resume("component")
        self.assertTrue(result["changed"])
        self.assertEqual(self.stable_state(), original)
        self.assertFalse(self.scheduler.reserve_component(self.connection, "PRIVATE-account-hash", "transactions",
                                                          attempt_id="too-soon", valid_until=self.now + 31536000))
        self.now += 21600
        self.assertTrue(self.scheduler.reserve_component(self.connection, "PRIVATE-account-hash", "transactions",
                                                         attempt_id="after-quota", valid_until=self.now + 31536000))
        self.assertEqual(len(self.rows(db.BudgetAcquisition)), 2)

    def test_resuming_long_server_deadline_keeps_it_in_force_until_exact_deadline(self):
        deadline = self.now + 691200
        self.scheduler.record_rate_limit(self.connection, primary="PRIVATE-account-hash", operation="transactions",
                                         error=BudgetAPIError(429, "rate_limit", True, retry_at=deadline,
                                                              rate_scope=RateScope.ACCOUNT_ENDPOINT))
        result = self.resume("component")
        self.assertEqual(result["retry_at"], deadline)
        self.assertFalse(result["suspended"])
        self.now = deadline - 1
        self.assertFalse(self.scheduler.reserve_component(self.connection, "PRIVATE-account-hash", "transactions",
                                                          attempt_id="early", valid_until=deadline + 86400))
        self.now = deadline
        self.assertTrue(self.scheduler.reserve_component(self.connection, "PRIVATE-account-hash", "transactions",
                                                         attempt_id="due", valid_until=deadline + 86400))

    def test_missing_or_already_resumed_limits_are_noop_without_fake_rows_or_audit(self):
        with self.sessions.begin() as session:
            session.execute(delete(db.BudgetComponent).where(db.BudgetComponent.account_id == self.account_id))
            session.get(db.BudgetConnection, "bank").next_attempt = self.now + 600
        before = self.stable_state()
        for scope in LIMIT_SCOPES:
            result = self.resume(scope)
            self.assertFalse(result["changed"])
            self.assertFalse(result["suspended"])
            if scope == "connection":
                self.assertEqual(result["next_attempt"], self.now + 600)
        self.assertEqual(self.stable_state(), before)
        self.assertEqual(self.rows(db.BudgetLimitAudit), [])
        self.limits.list("owner")
        self.assertEqual(self.stable_state(), before)

    def test_invalid_owner_foreign_connection_and_account_never_reveal_target(self):
        for owner in ("missing", "viewer", "disabled"):
            with self.assertRaises(BudgetStoreError):
                self.limits.list(owner)
            with self.assertRaises(BudgetStoreError):
                self.limits.resume(owner, "bank", "connection", actor="operator")
        for owner, connection in (("owner", "foreign-bank"), ("other", "bank"), ("owner", "unknown"), ("owner", None)):
            with self.assertRaisesRegex(BudgetStoreError, "Unknown bank connection"):
                self.limits.resume(owner, connection, "connection", actor="operator")
        for account in (self.foreign_id, "unknown"):
            with self.assertRaisesRegex(BudgetStoreError, "Unknown bank account"):
                self.limits.resume("owner", "bank", "component", account_id=account, operation="balances", actor="operator")
        with self.sessions.begin() as session:
            session.add(db.BudgetAccount(id="wrong-parent", owner="owner", connection_id="foreign-bank",
                                         identification_hash="PRIVATE-invalid-parent", enabled=False, configured=False,
                                         balances_json="[]"))
        with self.assertRaisesRegex(BudgetStoreError, "Unknown bank account"):
            self.limits.resume("owner", "bank", "component", account_id="wrong-parent", operation="balances", actor="operator")
        self.assertEqual(self.rows(db.BudgetLimitAudit), [])

    def test_config_and_stored_immutable_identity_must_match_for_list_and_resume(self):
        missing = replace(self.connection, id="missing-bank")
        limits = BudgetLimits(self.store, replace(self.config, connections=(missing,)))
        with self.assertRaises(BudgetStoreError):
            limits.list("owner")
        with self.assertRaises(BudgetStoreError):
            limits.resume("owner", "missing-bank", "connection", actor="operator")
        for field, value in (("owner", "other"), ("application_id", "different-app"), ("bank_name", "Different Bank"), ("country", "IE")):
            with self.sessions.begin() as session:
                row = session.get(db.BudgetConnection, "bank")
                old = getattr(row, field)
                setattr(row, field, value)
            with self.subTest(field=field), self.assertRaisesRegex(BudgetStoreError, "Unknown bank connection"):
                self.resume("connection")
            with self.sessions.begin() as session:
                setattr(session.get(db.BudgetConnection, "bank"), field, old)

    def test_invalid_scope_selectors_actor_and_origin_are_safe(self):
        cases = [(None, {}), ([], {}), ("unknown", {}), ("component", {}),
                 ("component", {"account_id": [], "operation": "transactions"}),
                 ("component", {"account_id": self.account_id, "operation": []}),
                 ("component", {"account_id": self.account_id, "operation": "details"}),
                 ("connection", {"account_id": self.account_id}), ("connection", {"operation": "balances"})]
        for scope, selectors in cases:
            with self.subTest(scope=scope), self.assertRaises(BudgetStoreError):
                self.limits.resume("owner", "bank", scope, actor="operator", **selectors)
        with self.assertRaises(BudgetStoreError):
            self.limits.resume("owner", "bank", "connection", actor="PRIVATE\x00")
        with self.assertRaises(BudgetStoreError):
            self.limits.resume("owner", "bank", "connection", actor="operator", origin="remote")

    def test_audit_failure_rolls_back_suspension_and_all_related_state(self):
        self.seed("component")
        before = self.stable_state()
        audit = self.limits._audit
        def fail_after_flush(session, *args):
            audit(session, *args)
            session.flush()
            raise RuntimeError("synthetic audit failure")
        with mock.patch.object(self.limits, "_audit", side_effect=fail_after_flush):
            with self.assertRaises(RuntimeError):
                self.resume("component")
        self.assertEqual(self.stable_state(), before)
        self.assertTrue(next(row for row in self.rows(db.BudgetComponent) if row.account_id == self.account_id and row.operation == "transactions").suspended)
        self.assertEqual(self.rows(db.BudgetLimitAudit), [])
        self.assertTrue(self.resume("component")["changed"])

    def test_cli_local_actor_sanitized_output_no_credentials_network_and_lock(self):
        self.seed("component")
        with mock.patch.object(runtime, "credentials") as credentials, \
             mock.patch("urllib.request.OpenerDirector.open") as http, mock.patch.object(cli.os, "geteuid", return_value=2468):
            code, output, diagnostic = self.invoke(["list", "--owner", "owner"])
            self.assertEqual((code, diagnostic), (0, ""))
            self.assert_redacted(output)
            code, output, diagnostic = self.invoke(["resume", "--owner", "owner", "--connection", "bank", "--scope", "component",
                                                    "--account", self.account_id, "--operation", "transactions"])
        credentials.assert_not_called()
        http.assert_not_called()
        self.assertEqual((code, diagnostic), (0, ""))
        self.assertTrue(json.loads(output)["limit"]["changed"])
        self.assert_redacted(output)
        self.assertEqual(self.rows(db.BudgetLimitAudit)[0].actor, "operator:2468")
        with runtime.budget_lock(self.path):
            code, output, diagnostic = self.invoke(["list", "--owner", "owner"])
        self.assertEqual((code, output), (2, ""))
        self.assertIn("already running", diagnostic)

    def test_cli_rejects_missing_or_extra_selectors_and_caller_supplied_actor_or_scope_key(self):
        base = ["resume", "--owner", "owner", "--connection", "bank"]
        for arguments in ([], ["list"], base, base + ["--scope", "component"],
                          base + ["--scope", "connection", "--account", self.account_id],
                          base + ["--scope", "connection", "--actor", "spoofed"],
                          base + ["--scope", "connection", "--origin", "spoofed"],
                          base + ["--scope", "connection", "--key", "raw-key"]):
            code, output, diagnostic = self.invoke(arguments)
            self.assertEqual((code, output), (2, ""))
            self.assertTrue(diagnostic)
        self.assertEqual(self.rows(db.BudgetLimitAudit), [])
