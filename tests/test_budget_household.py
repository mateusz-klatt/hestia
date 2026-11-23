"""Household grants, the member read gate, and the household API."""
from __future__ import annotations

import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from alembic import command
from multidict import MultiDict
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from hestia import auth, db, store_sql, web
from hestia.budget import analytics_api, household, household_api, household_views, month_budget
from hestia.proxy import EventBus
from hestia.budget.analytics_core import AnalyticsError, counterparty_rule_value
from hestia.budget.analytics_store import BudgetAnalyticsStore
from hestia.budget.household import (
    HouseholdAuthError, entity_alias, require_household_data_grant, run_household_command,
)
from hestia.budget.household_views import _encode
from tests import test_budget_api as fixture

SECRET = fixture.SECRET
PREFIX = household_api.PREFIX
PERIOD = "2026-11"
AMOUNT = "12345678901234567890.12345"
REMITTANCE = "Alice private description"
IBAN = "PL00123456789012345678901234"
RULE_VALUE = "SECRET-RULE-OPERAND"
GROCERIES = "Groceries SECRET NAME"
SALARY_NAME = "Wynagrodzenie"
ZUS_NAME = "Świadczenia ZUS"
PYTHON = None  # tests invoke library functions directly; the fleet command is recorded in the module doc.


def _versions(path):
    with sqlite3.connect(path) as conn:
        return [row[0] for row in conn.execute("SELECT version_num FROM alembic_version")]


def _mapping_keys(value):
    found = set()
    if isinstance(value, dict):
        found.update(value)
        for item in value.values():
            found.update(_mapping_keys(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_mapping_keys(item))
    return found


def _tables(path):
    with sqlite3.connect(path) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


class HouseholdCommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "household.db"
        self.addCleanup(db.reset_engine_cache)

    def _ready(self):
        engine, sessions = db.init_db(self.path)
        store_sql.cutover_users(engine, {"alice": "unused"})
        with sessions.begin() as session:
            session.add_all([
                db.User(username="viewer", password_hash="unused", role="viewer"),
                db.User(username="bob", password_hash="unused", role="admin"),
            ])
        return engine, sessions

    def _run(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), self.assertLogs("hestia.budget.household", level="INFO") as logs:
            code = run_household_command(mock.Mock(
                action=args[0], member=args[1], owner=args[2],
                grant_id=args[3] if len(args) > 3 else None, db=str(self.path)))
        return code, stdout.getvalue(), stderr.getvalue(), "\n".join(logs.output)

    def test_grant_is_idempotent_conflict_revoke_and_regrant_are_exact(self):
        self._ready()
        statements = []
        real = household._immediate_engine

        def wrapped(path):
            engine = real(path)
            event.listen(engine, "before_cursor_execute", lambda *_args: statements.append(_args[2]))
            return engine

        with mock.patch.object(household, "_immediate_engine", wrapped), \
             mock.patch("hestia.db.init_db", side_effect=AssertionError("init_db")), \
             mock.patch("hestia.budget.__main__.execute", side_effect=AssertionError("execute")), \
             mock.patch("hestia.budget.__main__.load_config", side_effect=AssertionError("config")), \
             mock.patch("hestia.budget.__main__.budget_lock", side_effect=AssertionError("lock")):
            code, out, err, logged = self._run("grant", "viewer", "alice")
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        first = json.loads(out)
        self.assertEqual(first["status"], "granted")
        self.assertEqual(len(first["grant_id"]), 64)
        self.assertIn("BEGIN IMMEDIATE", statements)
        self.assertNotIn("viewer", logged)
        self.assertNotIn("alice", logged)
        self.assertNotIn(first["grant_id"], logged)
        self.assertNotIn("viewer", out)
        self.assertNotIn("alice", out)

        code, out, err, logged = self._run("grant", "viewer", "alice")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["status"], "unchanged")
        self.assertEqual(json.loads(out)["grant_id"], first["grant_id"])
        self.assertNotIn(first["grant_id"], logged)

        code, out, err, _logged = self._run("grant", "viewer", "bob")
        self.assertEqual(code, 2)
        self.assertEqual(err.strip(), "household_grant_conflict")
        self.assertEqual(out, "")

        code, _out, err, _logged = self._run("revoke", "viewer", "alice", "0" * 64)
        self.assertEqual(code, 2)
        self.assertEqual(err.strip(), "household_grant_mismatch")
        code, _out, err, _logged = self._run("revoke", "viewer", "bob", first["grant_id"])
        self.assertEqual(err.strip(), "household_grant_mismatch")
        with Session(db.make_engine(self.path)) as session:
            self.assertEqual(session.get(db.BudgetHouseholdMember, "viewer").grant_id, first["grant_id"])

        code, out, err, _logged = self._run("revoke", "viewer", "alice", first["grant_id"])
        self.assertEqual((code, err.strip(), json.loads(out)["status"]), (0, "", "revoked"))
        code, out, err, _logged = self._run("grant", "viewer", "alice")
        second = json.loads(out)
        self.assertEqual(second["status"], "granted")
        self.assertNotEqual(second["grant_id"], first["grant_id"])
        self.assertNotIn(second["grant_id"], _logged)

    def test_invalid_member_and_owner_do_not_write(self):
        _engine, sessions = self._ready()
        code, out, err, _logged = self._run("grant", "alice", "alice")
        self.assertEqual((code, err.strip(), out), (2, "household_member_invalid", ""))
        code, out, err, _logged = self._run("grant", "bob", "alice")
        self.assertEqual((code, err.strip(), out), (2, "household_member_invalid", ""))
        code, _out, err, _logged = self._run("grant", "viewer", "missing")
        self.assertEqual(err.strip(), "household_owner_invalid")
        granted = household.grant_member("viewer", "alice", path=str(self.path))
        with sessions.begin() as session:
            member = session.get(db.User, "viewer")
            member.role = "operator"
            member.disabled = True
        code, _out, err, _logged = self._run("grant", "viewer", "alice")
        self.assertEqual(err.strip(), "household_member_invalid")
        with sessions.begin() as session:
            session.get(db.User, "viewer").disabled = False
            session.get(db.User, "alice").disabled = True
        code, _out, err, _logged = self._run("grant", "viewer", "alice")
        self.assertEqual(err.strip(), "household_owner_invalid")
        with Session(db.make_engine(self.path)) as session:
            self.assertEqual(session.get(db.BudgetHouseholdMember, "viewer").grant_id, granted["grant_id"])

    def test_missing_database_is_not_created_and_old_schema_is_not_upgraded(self):
        missing = self.path
        with mock.patch("hestia.budget.household.make_engine", side_effect=AssertionError("created")):
            code, _out, err, _logged = self._run("grant", "viewer", "alice")
        self.assertEqual((code, err.strip()), (2, "household_db_required"))
        self.assertFalse(missing.exists())

        engine = db.make_engine(self.path)
        try:
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "0019")
        finally:
            engine.dispose()
        with mock.patch("hestia.db.init_db", side_effect=AssertionError("init_db")):
            code, _out, err, _logged = self._run("grant", "viewer", "alice")
        self.assertEqual((code, err.strip()), (2, "household_schema_required"))
        self.assertEqual(_versions(self.path), ["0019"])
        self.assertNotIn("budget_household_members", _tables(self.path))

    def test_head_without_users_marker_does_not_grant(self):
        db.init_db(self.path)
        code, _out, err, _logged = self._run("grant", "viewer", "alice")
        self.assertEqual((code, err.strip()), (2, "household_users_required"))
        self.assertEqual(_versions(self.path), ["0024"])
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM budget_household_members").fetchone()[0], 0)

    def _assert_migration_clean(self):
        with sqlite3.connect(self.path) as raw:
            self.assertEqual([row[0] for row in raw.execute("PRAGMA integrity_check")], ["ok"])
            self.assertEqual(raw.execute("PRAGMA foreign_key_check").fetchall(), [])
            leftovers = [row[0] for row in raw.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name LIKE '!_alembic!_tmp!_%' ESCAPE '!'")]
            self.assertEqual(leftovers, [])

    def test_downgrade_roundtrip_keeps_owner_rows_and_empties_grants(self):
        """0021 → init_db (0024) → downgrade_db (0022) → init_db (0024).

        Each step goes through the production runner. Receipt rows, category parents,
        the one-off label column, and owner rows survive. Grants do not.
        """
        engine = db.make_engine(self.path)
        try:
            db._run_sqlite_migration(
                engine, lambda connection: command.upgrade(db._alembic_config(connection), "0021"))
            with Session(engine) as session, session.begin():
                session.add_all([
                    db.User(username="alice", password_hash="unused", role="admin"),
                    db.User(username="viewer", password_hash="unused", role="viewer"),
                ])
                session.flush()
                session.add_all([
                    db.BudgetCategory(id="cat-salary", owner="alice", name=SALARY_NAME, name_key="wynagrodzenie",
                                      archived=False, version=1, created_at=1, updated_at=1),
                    db.BudgetOwnerInboxRead(owner="alice", kind="mortgage_due", subject_id="ab" * 32,
                                            milestone="due", read_at=1),
                ])
        finally:
            engine.dispose()

        upgraded, sessions = db.init_db(self.path)
        self._assert_migration_clean()
        with upgraded.connect() as conn:
            self.assertEqual(conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar(), "0024")
        with sessions.begin() as session:
            session.add(db.BudgetHouseholdMember(member_username="viewer", owner="alice", grant_id="cd" * 32))
        with self.assertRaises(IntegrityError):
            with sessions.begin() as session:
                session.get(db.BudgetHouseholdMember, "viewer").grant_id = "CD" * 32
        with sessions.begin() as session:
            self.assertEqual(session.get(db.BudgetHouseholdMember, "viewer").grant_id, "cd" * 32)
        with self.assertRaises(IntegrityError):
            with sessions.begin() as session:
                session.get(db.BudgetHouseholdMember, "viewer").grant_id = ("ab" * 32) + "\x00" + "ZZ"
        with sessions.begin() as session:
            self.assertEqual(session.get(db.BudgetHouseholdMember, "viewer").grant_id, "cd" * 32)
        self._assert_migration_clean()

        db.downgrade_db(self.path, "0022")
        self._assert_migration_clean()
        with sqlite3.connect(self.path) as raw:
            names = {row[0] for row in raw.execute("SELECT name FROM sqlite_master")}
            self.assertEqual(raw.execute("SELECT version_num FROM alembic_version").fetchone()[0], "0022")
            self.assertNotIn("budget_household_members", names)
            self.assertIn("budget_receipts", names)
            self.assertNotIn("ix_budget_analytics_audit_subject_action_at", names)
            self.assertEqual(raw.execute("SELECT name FROM budget_categories WHERE id='cat-salary'").fetchone()[0],
                             SALARY_NAME)
            self.assertEqual(raw.execute("SELECT milestone FROM budget_owner_inbox_reads").fetchone()[0], "due")
            category_columns = {row[1] for row in raw.execute("PRAGMA table_info(budget_categories)")}
            annotation_columns = {row[1] for row in raw.execute("PRAGMA table_info(budget_annotations)")}
            self.assertIn("parent_id", category_columns)
            self.assertIn("label", annotation_columns)

        again, _sessions = db.init_db(self.path)
        self._assert_migration_clean()
        with again.connect() as conn:
            self.assertEqual(conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar(), "0024")
            self.assertEqual(conn.exec_driver_sql("SELECT COUNT(*) FROM budget_household_members").scalar(), 0)
            self.assertEqual(conn.exec_driver_sql(
                "SELECT name FROM budget_categories WHERE id='cat-salary'").scalar(), SALARY_NAME)
            self.assertEqual(conn.exec_driver_sql("SELECT COUNT(*) FROM budget_owner_inbox_reads").scalar(), 1)

    def test_unexpected_failure_logs_the_class_only(self):
        self._ready()
        with mock.patch("hestia.budget.household._grant", side_effect=RuntimeError("viewer " + AMOUNT)):
            code, _out, err, logged = self._run("grant", "viewer", "alice")
        self.assertEqual(code, 1)
        self.assertEqual(err.strip(), "household_internal")
        self.assertIn("RuntimeError", logged)
        self.assertNotIn("viewer", logged)
        self.assertNotIn(AMOUNT, logged)

    def test_unknown_action_is_a_fixed_code(self):
        code = run_household_command(mock.Mock(action="nope", member="viewer", owner="alice", grant_id=None, db=None))
        self.assertEqual(code, 2)


class HouseholdAPITests(unittest.IsolatedAsyncioTestCase):
    # Compose the owner fixture. Subclassing would re-run its tests against the extra household row.
    rows = fixture.BudgetAPITests.rows
    token = fixture.BudgetAPITests.token
    headers = fixture.BudgetAPITests.headers
    assert_private_headers = fixture.BudgetAPITests.assert_private_headers

    async def asyncSetUp(self):
        patch = mock.patch.dict(os.environ, {analytics_api.FLAG: "1", "HESTIA_BUDGET_JOBS_ENABLED": "0",
                                              "HESTIA_BUDGET_POLLER_ENABLED": "0",
                                              "HESTIA_BUDGET_CONSENT_ENABLED": "0"})
        patch.start()
        self.addCleanup(patch.stop)
        await fixture.BudgetAPITests.asyncSetUp(self)
        analytics = BudgetAnalyticsStore(self.sessions, cursor_secret=SECRET)
        self.salary = analytics.create_category("alice", SALARY_NAME)
        self.zus = analytics.create_category("alice", ZUS_NAME)
        self.groceries = analytics.create_category("alice", GROCERIES)
        self.coded_salary = analytics.create_category("alice", "salary")
        self.benefit = analytics.create_category("alice", "child benefit")
        self.benefit_code = analytics.create_category("alice", "child_benefit")
        with self.sessions.begin() as session:
            session.add(db.BudgetRule(
                id="rule-secret", owner="alice", field="description", operator="contains", value=RULE_VALUE,
                category_id=self.groceries["id"], kind="expense", priority=10, enabled=True, version=1,
                created_at=1, updated_at=1))
            row = session.get(db.BudgetTransaction, (self.alice_id, "alice-entry"))
            payload = json.loads(row.payload_json)
            payload["remittance_information"] = [REMITTANCE, IBAN]
            row.payload_json = json.dumps(payload)
            clone = dict(payload)
            clone["entry_reference"] = "alice-entry-2"
            session.add(db.BudgetTransaction(
                account_id=self.alice_id, entry_reference="alice-entry-2", payload_json=json.dumps(clone),
                observed_at=row.observed_at, uncertain=False, source=row.source, api_observed_at=row.api_observed_at))
        granted = household.grant_member("viewer", "alice", path=str(self.path))
        self.grant_id = granted["grant_id"]

    def grant_headers(self, user="viewer", *, grant=None, extra=None):
        headers = self.headers(user)
        if grant is not False:
            headers[household_api.GRANT_HEADER] = self.grant_id if grant is None else grant
        if extra:
            headers.update(extra)
        return headers

    async def fetch(self, operation, *, user="viewer", params=None, headers=None, grant=None):
        if headers is None:
            headers = self.grant_headers(user, grant=False if operation == "access" else grant)
        if params is None and operation in ("overview", "transactions", "transfers", "spending-limits", "monthly-budget"):
            params = {"period": PERIOD}
        return await self.client.get(PREFIX + "/" + operation, params=params, headers=headers)

    def test_utf8_cap_counts_multibyte_characters(self):
        payload = {"label": "ł"}
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertEqual(len("ł".encode("utf-8")), 2)
        self.assertEqual(bytes(_encode(payload)), encoded)
        from hestia.budget.analytics_api import _encode_bounded
        self.assertEqual(bytes(_encode_bounded(payload, len(encoded))), encoded)
        with self.assertRaises(AnalyticsError) as caught:
            _encode_bounded(payload, len(encoded) - 1)
        self.assertEqual((caught.exception.status, caught.exception.code), (413, "budget_analytics_limit"))

    async def test_reads_are_allowlisted_private_and_match_the_owner_decimals(self):
        models = {
            "access": __import__("hestia.api_contract", fromlist=["BudgetHouseholdAccess"]).BudgetHouseholdAccess,
        }
        from hestia import api_contract
        models = {
            "access": api_contract.BudgetHouseholdAccess,
            "report-accounts": api_contract.BudgetHouseholdAccounts,
            "monthly-budget": api_contract.BudgetHouseholdMonthlyBudget,
            "overview": api_contract.BudgetHouseholdOverview,
            "transactions": api_contract.BudgetHouseholdTransactions,
            "categories": api_contract.BudgetHouseholdCategories,
            "rules": api_contract.BudgetHouseholdRules,
            "transfers": api_contract.BudgetHouseholdTransfers,
            "spending-limits": api_contract.BudgetHouseholdSpendingLimits,
            "recurring": api_contract.BudgetHouseholdRecurring,
            "mortgages": api_contract.BudgetHouseholdMortgages,
            "inbox": api_contract.BudgetHouseholdInbox,
            "quarantine": api_contract.BudgetHouseholdQuarantine,
            "monthly-budget-settings": api_contract.BudgetHouseholdSettings,
        }
        self.assertEqual(tuple(models), household_api.READS)
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.DEBUG)
        logger = logging.getLogger("hestia.budget")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        bodies = {}
        for operation, model in models.items():
            response = await self.fetch(operation)
            self.assertEqual(response.status, 200, await response.text())
            self.assert_private_headers(response)
            body = await response.json()
            model.model_validate(body)
            bodies[operation] = body
            head = await self.client.head(PREFIX + "/" + operation, headers=self.grant_headers())
            self.assertEqual(head.status, 403, operation)
            self.assertEqual(await head.read(), b"")
            owner_head = await self.client.head(PREFIX + "/" + operation, headers=self.headers("alice"))
            self.assertEqual(owner_head.status, 405, operation)
        text = json.dumps(bodies)
        for secret in (SALARY_NAME, ZUS_NAME, GROCERIES, "child benefit", REMITTANCE, IBAN, RULE_VALUE,
                       self.alice_id, self.bob_id, fixture.PRIVATE_A, fixture.PRIVATE_B, "alice-entry"):
            self.assertNotIn(secret, text, secret)
        logged = stream.getvalue()
        for secret in (SALARY_NAME, GROCERIES, REMITTANCE, IBAN, AMOUNT, self.alice_id, self.grant_id):
            self.assertNotIn(secret, logged)
        access = bodies["access"]
        self.assertEqual(set(access), {"grant_id"})
        self.assertEqual(access["grant_id"], self.grant_id)
        labels = {row["label"] for row in bodies["categories"]["categories"]}
        self.assertIn("salary", labels)
        self.assertIn("child_benefit", labels)
        self.assertTrue(any(label.isdigit() for label in labels))
        self.assertEqual(bodies["rules"]["rules"][0]["field"], "description")
        self.assertNotIn("value", bodies["rules"]["rules"][0])
        accounts = bodies["report-accounts"]["accounts"]
        self.assertEqual([row["ordinal"] for row in accounts], list(range(1, len(accounts) + 1)))
        alias = accounts[0]["account_id"]
        self.assertEqual(alias, entity_alias(SECRET, self.grant_id, "account", self.alice_id))
        again = await (await self.fetch("report-accounts")).json()
        self.assertEqual(again["accounts"], accounts)
        home_tx = bodies["transactions"]
        self.assertGreaterEqual(home_tx["filtered_total"], 2)
        self.assertIn(AMOUNT, [row["amount"] for row in home_tx["transactions"]])
        owner = await self.client.get("/api/budget/analytics/transactions", params={"period": PERIOD},
                                       headers=self.headers("alice"))
        self.assertEqual(owner.status, 200)
        owner_body = await owner.json()
        self.assertIn(REMITTANCE, json.dumps(owner_body))
        owner_amounts = sorted(row["amount"] for row in owner_body["transactions"])
        self.assertEqual(sorted(row["amount"] for row in home_tx["transactions"]), owner_amounts)
        owner_month = await self.client.get("/api/budget/analytics/monthly-budget", params={"period": PERIOD},
                                             headers=self.headers("alice"))
        owner_month_body = await owner_month.json()
        self.assertNotIn("series", owner_month_body)
        self.assertNotIn("_series_daily", owner_month_body)
        home_month = bodies["monthly-budget"]
        self.assertEqual(home_month["budget"]["summary"], owner_month_body["summary"])
        spent = home_month["budget"]["summary"]["spent"] if home_month["budget"]["summary"] else None
        if spent is None:
            self.assertEqual(home_month["series"]["daily"][-1]["cumulative_spent"], "0")
        else:
            self.assertEqual(home_month["series"]["daily"][-1]["cumulative_spent"], spent)
        self.assertLessEqual(len(home_month["series"]["daily"]), 31)
        self.assertLessEqual(len(home_month["series"]["months"]), 12)
        self.assertEqual(home_month["series"]["months"][-1]["period"], home_month["budget"]["period"])
        self.assertTrue(all(row["income_estimate_partial"] is False or row["is_current_month"]
                            for row in home_month["series"]["months"]))
        settings = bodies["monthly-budget-settings"]
        self.assertEqual(settings["income_category_ids"], [])
        self.assertIsNone(settings["pass_through"])

    async def test_spouse_views_omit_counterparty_account_fields(self):
        mask = "PL** … 1234"
        digest = "cd" * 32
        stored = counterparty_rule_value("ab" * 8, digest, mask)
        with self.sessions.begin() as session:
            session.add(db.BudgetRule(
                id="rule-account", owner="alice", field="counterparty_account", operator="equals",
                value=stored, category_id=None, kind="expense", priority=1, enabled=True, version=1,
                created_at=1, updated_at=1))
        rules = await self.fetch("rules")
        self.assertEqual(rules.status, 200, await rules.text())
        rules_body = await rules.json()
        transactions = await self.fetch("transactions")
        self.assertEqual(transactions.status, 200, await transactions.text())
        tx_body = await transactions.json()
        with mock.patch.dict(os.environ):
            os.environ.pop("HESTIA_BUDGET_RULE_KEY_FILE", None)
            owner_rules = await self.client.get("/api/budget/analytics/rules", headers=self.headers("alice"))
            self.assertEqual(owner_rules.status, 200, await owner_rules.text())
            owner_rules_body = await owner_rules.json()
            owner_tx = await self.client.get("/api/budget/analytics/transactions", params={"period": PERIOD},
                                              headers=self.headers("alice"))
        self.assertEqual(owner_tx.status, 200, await owner_tx.text())
        owner_tx_body = await owner_tx.json()
        account_rule = next(row for row in owner_rules_body["rules"] if row["field"] == "counterparty_account")
        self.assertEqual(account_rule["value"], mask)
        self.assertEqual(account_rule["counterparty_account_key"], digest)
        self.assertEqual(account_rule["inactive_reason"], "rule_key_unavailable")
        self.assertIn("counterparty_account_key", owner_tx_body["transactions"][0])
        self.assertIn("counterparty_account", owner_tx_body["transactions"][0])
        forbidden = {"counterparty_account_key", "counterparty_account", "inactive_reason"}
        for body in (rules_body, tx_body):
            self.assertTrue(forbidden.isdisjoint(_mapping_keys(body)))
            text = json.dumps(body)
            for secret in (mask, digest, stored, "cpacct:", "rule_key_unavailable", "rule_key_rotated"):
                self.assertNotIn(secret, text)
        self.assertEqual([row["field"] for row in rules_body["rules"]], ["description"])
        self.assertGreaterEqual(len(tx_body["transactions"]), 1)

    async def test_aliases_follow_the_grant_and_hidden_text_does_not_move_the_cursor(self):
        first = await (await self.fetch("report-accounts")).json()
        second = await (await self.fetch("report-accounts")).json()
        self.assertEqual(first["accounts"][0]["account_id"], second["accounts"][0]["account_id"])
        page = await (await self.fetch("transactions", params={"period": PERIOD, "limit": "1"})).json()
        self.assertEqual(page["filtered_total"], 2)
        self.assertIsNotNone(page["next_cursor"])
        with self.sessions.begin() as session:
            for reference in ("alice-entry", "alice-entry-2"):
                row = session.get(db.BudgetTransaction, (self.alice_id, reference))
                payload = json.loads(row.payload_json)
                payload["remittance_information"] = ["hidden remittance changed " + IBAN]
                row.payload_json = json.dumps(payload)
        replay = await self.fetch("transactions", params={"period": PERIOD, "limit": "1", "cursor": page["next_cursor"]})
        self.assertEqual(replay.status, 200, await replay.text())
        replay_body = await replay.json()
        self.assertEqual(replay_body["filtered_total"], page["filtered_total"])
        self.assertNotEqual(replay_body["transactions"][0]["id"], page["transactions"][0]["id"])
        with self.sessions.begin() as session:
            row = session.get(db.BudgetTransaction, (self.alice_id, "alice-entry"))
            payload = json.loads(row.payload_json)
            payload["amount"] = "1.00"
            row.payload_json = json.dumps(payload)
        stale = await self.fetch("transactions", params={"period": PERIOD, "limit": "1", "cursor": page["next_cursor"]})
        self.assertEqual(stale.status, 409)
        household.revoke_grant("viewer", "alice", self.grant_id, path=str(self.path))
        renewed = household.grant_member("viewer", "alice", path=str(self.path))
        self.assertNotEqual(renewed["grant_id"], self.grant_id)
        old = await self.fetch("report-accounts")
        self.assertEqual(old.status, 403)
        new = await (await self.fetch("report-accounts", grant=renewed["grant_id"])).json()
        self.assertNotEqual(new["accounts"][0]["account_id"], first["accounts"][0]["account_id"])
        self.assertEqual(new["accounts"][0]["account_id"],
                         entity_alias(SECRET, renewed["grant_id"], "account", self.alice_id))
        foreign = await self.fetch("transactions", params={"period": PERIOD, "category_id": "uncategorised"},
                                    grant=renewed["grant_id"])
        self.assertEqual(foreign.status, 404)
        self.assertEqual((await foreign.json())["error"], "budget_analytics_not_found")
        raw = await self.fetch("transactions", params={"period": PERIOD, "account_id": self.alice_id},
                                grant=renewed["grant_id"])
        self.assertEqual(raw.status, 404)

    async def test_every_denial_is_fixed_and_private(self):
        missing = await self.fetch("transactions", grant=False)
        self.assertEqual(missing.status, 403)
        self.assert_private_headers(missing)
        missing_text = await missing.text()
        self.assertEqual(json.loads(missing_text)["error"], "budget_forbidden")
        self.assertNotIn(AMOUNT, missing_text)
        missing_body = await self.client.get(PREFIX + "/transactions", params={"period": PERIOD},
                                              headers=self.headers("viewer"))
        self.assertEqual(missing_body.status, 403)
        self.assertNotIn(AMOUNT, await missing_body.text())
        token = self.token("viewer")
        duplicate = await self.client.get(
            PREFIX + "/overview", params={"period": PERIOD},
            headers=[("Cookie", "hestia_session=" + token),
                     (household_api.GRANT_HEADER, self.grant_id),
                     (household_api.GRANT_HEADER, self.grant_id)])
        self.assertEqual(duplicate.status, 403)
        self.assert_private_headers(duplicate)
        mismatched = await self.fetch("overview", grant=("a" if self.grant_id[:1] != "a" else "b") + self.grant_id[1:])
        self.assertEqual(mismatched.status, 403)
        self.assertEqual((await mismatched.json())["error"], "budget_forbidden")

        with self.sessions.begin() as session:
            session.add(db.BudgetHouseholdMember(member_username="alice", owner="bob", grant_id="cd" * 32))
        operator = await self.fetch("access", user="operator")
        self.assertEqual(operator.status, 403)
        self.assert_private_headers(operator)
        admin = await self.fetch("monthly-budget", user="alice", headers=self.grant_headers("alice", grant="cd" * 32))
        self.assertEqual(admin.status, 403)
        disabled = await self.fetch("access", user="disabled")
        self.assertEqual(disabled.status, 401)
        self.assert_private_headers(disabled)

        with self.sessions.begin() as session:
            session.get(db.User, "alice").disabled = True
        inactive_owner = await self.fetch("categories")
        self.assertEqual(inactive_owner.status, 403)
        self.assertEqual((await inactive_owner.json())["error"], "budget_forbidden")
        with self.sessions.begin() as session:
            session.get(db.User, "alice").disabled = False
            session.get(db.User, "viewer").disabled = True
        with self.sessions() as session:
            with self.assertRaises(HouseholdAuthError) as caught:
                require_household_data_grant(session, "viewer", header=self.grant_id, require_header=True)
        self.assertEqual((caught.exception.status, caught.exception.code), (403, "budget_forbidden"))
        inactive_viewer = await self.fetch("access")
        self.assertEqual(inactive_viewer.status, 401)
        with self.sessions.begin() as session:
            session.get(db.User, "viewer").disabled = False

        with self.sessions.begin() as session:
            session.delete(session.get(db.BudgetHouseholdMember, "viewer"))
        revoked = await self.fetch("inbox")
        self.assertEqual(revoked.status, 403)
        self.assertNotIn(self.grant_id, await revoked.text())

        household.grant_member("viewer", "alice", path=str(self.path))
        with mock.patch.dict(os.environ, {"HESTIA_BUDGET_HTTP_ENABLED": "0"}):
            flags = await self.fetch("access")
        self.assertEqual(flags.status, 404)
        self.assertEqual((await flags.json())["error"], "budget_disabled")
        self.assert_private_headers(flags)
        with mock.patch.dict(os.environ, {analytics_api.FLAG: "0"}):
            analytics = await self.fetch("access")
        self.assertEqual((analytics.status, (await analytics.json())["error"]), (404, "budget_analytics_disabled"))
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "json"}), \
             mock.patch.object(auth, "load_users", return_value={"viewer": "x", "alice": "x"}):
            backend = await self.fetch("access")
        self.assertEqual(backend.status, 409)
        self.assertEqual((await backend.json())["error"], "budget_requires_sqlite_users")
        self.assert_private_headers(backend)
        with mock.patch.object(web, "_AUTH_ENABLED", False):
            signed_out = await self.fetch("access", headers={})
        self.assertEqual(signed_out.status, 401)

        with self.sessions() as session:
            with mock.patch.dict(os.environ, {analytics_api.FLAG: "0"}):
                with self.assertRaises(HouseholdAuthError) as caught:
                    require_household_data_grant(session, "viewer", header=self.grant_id, require_header=False)
            self.assertEqual(caught.exception.code, "budget_analytics_disabled")
            from hestia.budget.views import BudgetUsersBackendRequired
            with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "json"}):
                with self.assertRaises(BudgetUsersBackendRequired):
                    require_household_data_grant(session, "viewer", header=None, require_header=False)
            with self.assertRaises(HouseholdAuthError) as caught:
                require_household_data_grant(session, "", header=None, require_header=False)
            self.assertEqual(caught.exception.status, 401)
            with self.assertRaises(HouseholdAuthError):
                require_household_data_grant(session, "operator", header="ab" * 32, require_header=True)
            with self.assertRaises(HouseholdAuthError):
                require_household_data_grant(session, "alice", header="cd" * 32, require_header=True)

    async def test_changes_between_eligible_roles_keep_the_grant(self):
        for role, operation in (("operator", "transactions"), ("viewer", "overview")):
            with self.subTest(role=role):
                with self.sessions.begin() as session:
                    session.get(db.User, "viewer").role = role
                response = await self.fetch(operation)
                self.assertEqual(response.status, 200, await response.text())
                self.assertEqual((await response.json())["grant_id"], self.grant_id)
        self.assertCountEqual(self._audit_split()[0], [
            ("alice", "viewer", "transactions", "null", "null"),
            ("alice", "viewer", "overview", "null", "null"),
        ])

    async def test_member_ineligibility_during_final_recheck_discards_the_body(self):
        grant = household.grant_member("operator", "alice", path=str(self.path))["grant_id"]

        def update_member(role, disabled):
            with self.sessions.begin() as session:
                member = session.get(db.User, "operator")
                member.role = role
                member.disabled = disabled

        for role, disabled, discovery_status in (("admin", False, 403), ("operator", True, 401)):
            with self.subTest(role=role, disabled=disabled):
                update_member("operator", False)
                with mock.patch.object(household_api, "before_grant_recheck",
                                       side_effect=lambda: update_member(role, disabled)) as recheck:
                    response = await self.fetch("transactions", user="operator", grant=grant)
                recheck.assert_called_once_with()
                self.assertEqual(response.status, 403)
                self.assert_private_headers(response)
                body = await response.text()
                self.assertEqual(json.loads(body)["error"], "budget_forbidden")
                self.assertNotIn(AMOUNT, body)
                self.assertNotIn(grant, body)
                self.assertEqual(self._audit_split()[0], [])
                discovery = await self.fetch("access", user="operator")
                self.assertEqual(discovery.status, discovery_status)
                with self.sessions() as session:
                    self.assertEqual(session.get(db.BudgetHouseholdMember, "operator").grant_id, grant)

    async def test_revoke_during_the_final_recheck_discards_the_body(self):
        def revoke():
            household.revoke_grant("viewer", "alice", self.grant_id, path=str(self.path))
            self.renewed = household.grant_member("viewer", "bob", path=str(self.path))["grant_id"]

        with mock.patch.object(household_api, "before_grant_recheck", revoke):
            response = await self.fetch("transactions")
        self.assertEqual(response.status, 403)
        self.assert_private_headers(response)
        text = await response.text()
        self.assertNotIn(AMOUNT, text)
        self.assertNotIn(self.grant_id, text)
        self.assertNotIn(SALARY_NAME, text)
        self.assertEqual(json.loads(text)["error"], "budget_forbidden")
        self.assertEqual(self._audit_split()[0], [])
        old = await self.fetch("transactions")
        self.assertEqual(old.status, 403)
        renewed = await self.fetch("transactions", grant=self.renewed)
        self.assertEqual(renewed.status, 200, await renewed.text())

    async def test_queries_settings_and_admin_routes_stay_closed(self):
        for params in ({"query": "secret"}, {"unexpected": "1"}, MultiDict([("period", PERIOD), ("period", "2026-10")])):
            response = await self.fetch("transactions", params=params)
            self.assertEqual(response.status, 400, params)
        self.assertEqual((await self.fetch("access", params={"period": PERIOD})).status, 400)
        self.assertEqual((await self.fetch("transactions", params={})).status, 400)
        config = json.dumps({"alice": {"income_category_ids": [self.salary["id"]], "pass_through": {
            "rent_category_id": self.zus["id"], "mortgage_category_id": self.groceries["id"]}}})
        with mock.patch.dict(os.environ, {month_budget.MONTH_CONFIG_ENV: config}):
            response = await self.fetch("monthly-budget-settings")
            self.assertEqual(response.status, 200, await response.text())
            body = await response.json()
            self.assertEqual(body["income_category_ids"], [entity_alias(SECRET, self.grant_id, "category", self.salary["id"])])
            self.assertEqual(body["pass_through"]["rent_category_id"],
                             entity_alias(SECRET, self.grant_id, "category", self.zus["id"]))
            self.assertNotIn(self.salary["id"], json.dumps(body))
            self.assertNotIn(SALARY_NAME, json.dumps(body))
            month = await (await self.fetch("monthly-budget")).json()
            self.assertEqual(month["series"]["daily"][-1]["cumulative_spent"], month["budget"]["summary"]["spent"])
        missing = json.dumps({"alice": {"income_category_ids": ["missing-category"]}})
        with mock.patch.dict(os.environ, {month_budget.MONTH_CONFIG_ENV: missing}):
            response = await self.fetch("monthly-budget-settings")
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())["error"], "budget_unavailable")
        self.assert_private_headers(response)
        with mock.patch.object(household_views, "MAX_RESPONSE_BYTES", 8):
            limited = await self.fetch("access")
        self.assertEqual(limited.status, 413)
        self.assert_private_headers(limited)
        limited_text = await limited.text()
        self.assertNotIn(self.grant_id, limited_text)
        self.assertEqual(json.loads(limited_text)["error"], "budget_analytics_limit")
        for path, method in (("/api/budget/accounts", "get"), ("/api/budget/transactions", "get"),
                             ("/api/budget/analytics/overview", "get"), ("/api/budget/analytics/monthly-budget", "get"),
                             ("/api/budget/consent/status", "get"), ("/api/budget/consent/start", "post"),
                             ("/api/budget/refresh", "post")):
            response = await (self.client.get if method == "get" else self.client.post)(
                path, headers=self.headers("viewer", origin=method == "post"))
            self.assertEqual(response.status, 403, path)
            self.assertNotIn(AMOUNT, await response.text())
        callback = await self.client.get(
            "/api/budget/enable-banking/callback", headers=self.grant_headers(), allow_redirects=False)
        self.assertEqual(callback.status, 303)
        callback_text = await callback.text()
        self.assertNotIn(AMOUNT, callback_text)
        self.assertNotIn(self.grant_id, callback_text)
        from hestia import api_contract
        for operation in household_api.READS:
            path = PREFIX + "/" + operation
            self.assertEqual(web._ROUTE_MIN_ROLE[("GET", path)], "viewer")
            self.assertEqual(api_contract._PATH_ROLES[("GET", path)], "viewer")
        for (method, path), role in web._ROUTE_MIN_ROLE.items():
            if path.startswith("/api/budget/") and "/household/" not in path:
                self.assertEqual(role, "admin", path)
        for (method, path), role in api_contract._PATH_ROLES.items():
            if path.startswith("/api/budget/") and "/household/" not in path and not path.endswith("/callback"):
                self.assertEqual(role, "admin", path)

    async def test_owner_series_flag_stays_off_the_owner_response(self):
        response = await self.client.get("/api/budget/analytics/monthly-budget", headers=self.headers("alice"))
        self.assertEqual(response.status, 200, await response.text())
        body = await response.json()
        self.assertNotIn("_series_daily", body)
        self.assertNotIn("series", body)

    async def test_recheck_exception_and_oversized_body_stay_fixed(self):
        with mock.patch.object(household_api, "_commit", side_effect=RuntimeError("boom")):
            failed = await self.fetch("access")
        self.assertEqual(failed.status, 500)
        self.assertEqual((await failed.json())["error"], "budget_internal_error")
        self.assert_private_headers(failed)
        with mock.patch.object(household_api, "MAX_RESPONSE_BYTES", 0):
            limited = await self.fetch("access")
        self.assertEqual(limited.status, 413)
        self.assertEqual((await limited.json())["error"], "budget_analytics_limit")
        self.assert_private_headers(limited)

    def _audit_split(self):
        with self.sessions() as session:
            rows = session.scalars(select(db.BudgetAnalyticsAudit).order_by(db.BudgetAnalyticsAudit.id)).all()
            household_rows = []
            other = 0
            for row in rows:
                if row.subject_type == "household_read":
                    household_rows.append((row.owner, row.subject_id, row.action, row.before_json, row.after_json))
                else:
                    other += 1
            return household_rows, other

    def _home_audit(self):
        with self.sessions() as session:
            rows = session.scalars(select(db.Audit).order_by(db.Audit.id)).all()
            return [(row.actor, row.action, row.target, row.detail, row.result) for row in rows]

    async def test_successful_viewer_read_writes_one_sanitised_audit_row(self):
        """One sanitised analytics-audit row for a successful spouse read, and none when it is refused.

        Repeats inside 60 seconds do not add another row, and discovery writes none. The row names
        the member, the grant owner and the view. It does not carry amounts, aliases, the grant id
        or query values, and it does not appear on /api/audit or SSE.
        """
        household_rows, other = self._audit_split()
        self.assertEqual(household_rows, [])
        home_before = self._home_audit()
        engine, _sessions = db.init_db()
        runtime = self.client.server.app[web._RT_KEY]
        runtime.audit_engine = engine
        bus = EventBus()
        runtime.event_bus = bus
        subscription = await bus.try_subscribe()
        self.addCleanup(subscription.close)
        reader = household_api._reader(SECRET)
        with self.assertRaises(AnalyticsError):
            reader.commit_authorized_read("viewer", "alice", self.grant_id, self.grant_id, "period=" + PERIOD)
        with self.assertRaises(AnalyticsError):
            reader.commit_authorized_read(
                "viewer", "alice", self.grant_id, self.grant_id, PREFIX + "/transactions?period=" + PERIOD)
        with self.assertRaises(HouseholdAuthError):
            reader.commit_authorized_read("viewer", "bob", self.grant_id, self.grant_id, "overview")
        with self.assertRaises(HouseholdAuthError):
            reader.commit_authorized_read("viewer", "alice", "ab" * 32, self.grant_id, "overview")
        self.assertEqual(self._audit_split()[0], [])

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.DEBUG)
        logger = logging.getLogger("hestia.budget")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        with mock.patch.object(web, "_audit") as home_audit:
            first = await self.fetch("transactions", params={"period": PERIOD, "limit": "1"})
            self.assertEqual(first.status, 200, await first.text())
            page = await first.json()
            self.assertIn(AMOUNT, [row["amount"] for row in page["transactions"]])
            alias = page["transactions"][0]["account_id"]
            cursor = page["next_cursor"]
            self.assertIsNotNone(cursor)
            self.assertEqual(page["grant_id"], self.grant_id)
            second = await self.fetch("transactions", params={"period": PERIOD, "limit": "1", "cursor": cursor})
            self.assertEqual(second.status, 200, await second.text())
            discovered = await self.fetch("access")
            self.assertEqual(discovered.status, 200, await discovered.text())
            self.assertEqual((await discovered.json())["grant_id"], self.grant_id)

            denied = await self.fetch("overview", grant=False)
            self.assertEqual(denied.status, 403)
            mismatched = await self.fetch("rules", grant="ab" * 32)
            self.assertEqual(mismatched.status, 403)
            missing_category = await self.fetch(
                "transactions", params={"period": PERIOD, "category_id": "uncategorised"})
            self.assertEqual(missing_category.status, 404)
            operator = await self.fetch("categories", user="operator")
            self.assertEqual(operator.status, 403)
            with mock.patch.dict(os.environ, {"HESTIA_BUDGET_HTTP_ENABLED": "0"}):
                disabled = await self.fetch("inbox")
            self.assertEqual(disabled.status, 404)
            self.assertEqual((await disabled.json())["error"], "budget_disabled")
            with mock.patch.object(household_api, "MAX_RESPONSE_BYTES", 0):
                oversized = await self.fetch("mortgages")
            self.assertEqual(oversized.status, 413)
            with mock.patch.object(household_views.HouseholdReader, "commit_authorized_read",
                                    side_effect=RuntimeError(AMOUNT)):
                broken = await self.fetch("quarantine")
            self.assertEqual(broken.status, 500)
            self.assertEqual((await broken.json())["error"], "budget_internal_error")
            self.assertNotIn(AMOUNT, await broken.text())

            def revoke():
                household.revoke_grant("viewer", "alice", self.grant_id, path=str(self.path))

            with mock.patch.object(household_api, "before_grant_recheck", revoke):
                raced = await self.fetch("spending-limits")
            self.assertEqual(raced.status, 403)
            self.assertNotIn(AMOUNT, await raced.text())
            self.assertNotIn(self.grant_id, await raced.text())
        home_audit.assert_not_called()
        rows, other_after = self._audit_split()
        self.assertEqual(other_after, other)
        self.assertEqual(rows, [
            ("alice", "viewer", "transactions", "null", "null"),
        ])
        recorded = json.dumps(rows)
        for secret in (AMOUNT, self.grant_id, alias, cursor, PERIOD, REMITTANCE, IBAN, SALARY_NAME,
                       RULE_VALUE, GROCERIES, self.alice_id):
            self.assertNotIn(secret, recorded, secret)
        self.assertEqual(self._home_audit(), home_before)
        feed = await self.client.get("/api/audit", headers=self.headers("viewer"))
        self.assertEqual(feed.status, 200)
        feed_text = await feed.text()
        self.assertNotIn("household_read", feed_text)
        self.assertNotIn(AMOUNT, feed_text)
        self.assertNotIn(self.grant_id, feed_text)
        self.assertNotIn(alias, feed_text)
        self.assertNotIn(cursor, feed_text)
        await asyncio.sleep(0)
        self.assertTrue(subscription.queue.empty())
        logged = stream.getvalue()
        self.assertNotIn(AMOUNT, logged)
        self.assertNotIn(self.grant_id, logged)
        self.assertNotIn(alias, logged)
        self.assertNotIn(cursor, logged)

    async def test_revoke_after_the_commit_returns_the_body_and_keeps_the_row(self):
        engine, _sessions = db.init_db()
        statements = []

        def capture(*args):
            statements.append(args[2])

        event.listen(engine, "before_cursor_execute", capture)

        def revoke():
            household.revoke_grant("viewer", "alice", self.grant_id, path=str(self.path))

        try:
            with mock.patch.object(household_api, "after_grant_commit", revoke):
                response = await self.fetch("overview")
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        self.assertEqual(response.status, 200, await response.text())
        body = await response.json()
        self.assertEqual(body["grant_id"], self.grant_id)
        self.assertEqual(body["period"], PERIOD)
        spent = body["currencies"][0]["expenses"]
        self.assertTrue(spent)
        self.assertIn("BEGIN IMMEDIATE", statements)
        rows, _other = self._audit_split()
        self.assertEqual(rows, [("alice", "viewer", "overview", "null", "null")])
        follow = await self.fetch("overview")
        self.assertEqual(follow.status, 403)
        follow_text = await follow.text()
        self.assertNotIn(spent, follow_text)
        self.assertNotIn(self.grant_id, follow_text)
        self.assertEqual(self._audit_split()[0], rows)

    async def test_audit_dedup_retention_and_access_do_not_write_a_row(self):
        stamp = {"now": 1_700_000_000.0}
        cutoff = stamp["now"] - 180 * 86400
        with self.sessions.begin() as session:
            session.add_all([
                db.BudgetAnalyticsAudit(
                    owner="alice", subject_type="household_read", subject_id="viewer", action="categories",
                    before_json="null", after_json="null", changed_at=cutoff - 1),
                db.BudgetAnalyticsAudit(
                    owner="alice", subject_type="household_read", subject_id="viewer", action="rules",
                    before_json="null", after_json="null", changed_at=cutoff),
                db.BudgetAnalyticsAudit(
                    owner="alice", subject_type="household_read", subject_id="viewer", action="mortgages",
                    before_json="null", after_json="null", changed_at=stamp["now"] - 179 * 86400),
                db.BudgetAnalyticsAudit(
                    owner="alice", subject_type="category", subject_id="cat-kept", action="update",
                    before_json="null", after_json="{}", changed_at=1),
            ])

        def frozen(self):
            return stamp["now"]

        with mock.patch.object(BudgetAnalyticsStore, "_now", frozen):
            first = await self.fetch("transactions", params={"period": PERIOD, "limit": "1"})
            self.assertEqual(first.status, 200, await first.text())
            second = await self.fetch("transactions", params={"period": PERIOD, "limit": "1"})
            self.assertEqual(second.status, 200, await second.text())
            discovered = await self.fetch("access")
            self.assertEqual(discovered.status, 200, await discovered.text())
            self.assertEqual((await discovered.json())["grant_id"], self.grant_id)
            self.assertEqual(
                [row[2] for row in self._audit_split()[0]], ["rules", "mortgages", "transactions"])
            stamp["now"] += 60
            boundary = await self.fetch("transactions", params={"period": PERIOD, "limit": "1"})
            self.assertEqual(boundary.status, 200, await boundary.text())
            self.assertEqual(
                [row[2] for row in self._audit_split()[0]], ["rules", "mortgages", "transactions"])
            stamp["now"] += 1
            later = await self.fetch("transactions", params={"period": PERIOD, "limit": "1"})
            self.assertEqual(later.status, 200, await later.text())
            overview = await self.fetch("overview")
            self.assertEqual(overview.status, 200, await overview.text())
            self.assertEqual(
                [row[2] for row in self._audit_split()[0]],
                ["mortgages", "transactions", "transactions", "overview"])
            with self.sessions() as session:
                kept = session.scalars(select(db.BudgetAnalyticsAudit).where(
                    db.BudgetAnalyticsAudit.subject_type == "category")).all()
            self.assertIn(("cat-kept", "update"), [(row.subject_id, row.action) for row in kept])
            self.assertGreater(len(kept), 1)

            def revoke():
                household.revoke_grant("viewer", "alice", self.grant_id, path=str(self.path))

            before = self._audit_split()[0]
            with mock.patch.object(household_api, "before_grant_recheck", revoke):
                denied = await self.fetch("overview")
            self.assertEqual(denied.status, 403)
            self.assertNotIn(AMOUNT, await denied.text())
            self.assertEqual(self._audit_split()[0], before)

    async def test_grant_header_is_rejected_before_lookup_and_the_analytics_lock(self):
        entered = []
        real = household_views._read_lock

        class Recording:
            def __enter__(self):
                entered.append(True)
                return real.__enter__()

            def __exit__(self, exc_type, exc, tb):
                return real.__exit__(exc_type, exc, tb)

        for header in ("é" + "a" * 63, "AB" * 32, "g" * 64, "ab" * 31, "ab" * 32 + "c"):
            entered.clear()
            with mock.patch.object(household_views, "_read_lock", Recording()):
                response = await self.fetch("transactions", grant=header)
            self.assertEqual(response.status, 403, header)
            self.assertEqual((await response.json())["error"], "budget_forbidden")
            self.assertEqual(entered, [], header)
            self.assertNotIn(AMOUNT, await response.text())
        self.assertEqual(self._audit_split()[0], [])
        session = mock.Mock()
        with self.assertRaises(HouseholdAuthError) as caught:
            household.authorize_household(session, "viewer", header="é" + "a" * 63, require_header=True)
        self.assertEqual(caught.exception.status, 403)
        session.get.assert_not_called()
        self.assertFalse(household.valid_grant_id(None))
        self.assertFalse(household.valid_grant_id(b"ab" * 32))
        self.assertFalse(household.valid_grant_id("AB" * 32))
        self.assertTrue(household.valid_grant_id(self.grant_id))
        entered.clear()
        with mock.patch.object(household_views, "_read_lock", Recording()):
            mismatched = await self.fetch("overview", grant="ab" * 32)
        self.assertEqual(mismatched.status, 403)
        self.assertEqual(entered, [])
        self.assertEqual(self._audit_split()[0], [])
