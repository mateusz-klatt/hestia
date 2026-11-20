"""Synthetic private views, encrypted paging and versioned controls."""
import base64
from dataclasses import replace
import json
import math
import os
import unittest
from unittest import mock
from urllib.parse import urlencode

from alembic import command
from sqlalchemy import event, select

from hestia import db
from hestia.budget import views
from hestia.budget.scheduler import BudgetDeferredError, BudgetScheduler
from hestia.budget.store import BudgetControlConflict, BudgetNotFound, BudgetPausedError, BudgetStoreError
from hestia.budget.views import BudgetAccessDenied, BudgetHTTPStore, BudgetQueryError, BudgetUsersBackendRequired
from tests import test_budget_store as fixtures


class BudgetViewsTests(unittest.TestCase):
    setUp_database = fixtures.BudgetStoreTests.setUp
    tearDown = fixtures.BudgetStoreTests.tearDown
    rows = fixtures.BudgetStoreTests.rows

    def setUp(self):
        self.setUp_database()
        self.config = fixtures.configuration()
        other = replace(self.config.connections[0], id="other-bank", owner="other")
        self.config = replace(self.config, connections=(self.config.connections[0], other))
        fixtures.prepare_selected(self.store, self.config)
        with self.sessions.begin() as session:
            session.add(db.AppMeta(key="users_authoritative", value="1"))
        self.http = BudgetHTTPStore(self.sessions, clock=lambda: self.now, cursor_secret=b"synthetic cursor secret")
        accounts = self.rows(db.BudgetAccount)
        self.account = next(row.id for row in accounts if row.owner == "owner")
        self.foreign = next(row.id for row in accounts if row.owner == "other")

    def seed(self):
        transactions = (fixtures.transaction("older", booking_date="2026-11-18"),
                        fixtures.transaction("alpha", booking_date="2026-11-20"),
                        fixtures.transaction("beta", booking_date="2026-11-20"),
                        fixtures.transaction("value-only", booking_date=None, value_date="2026-11-19"),
                        fixtures.transaction("future", booking_date="2026-11-22"),
                        fixtures.transaction("conflict"), fixtures.transaction("conflict", amount="2"))
        self.store.commit_snapshot("bank", fixtures.snapshot(*transactions))
        self.store.commit_snapshot("other-bank", fixtures.snapshot(fixtures.transaction("FOREIGN PRIVATE")))

    def dump(self):
        with self.sessions() as session:
            return {table.name: [tuple(row) for row in session.execute(select(table))]
                    for table in db.Base.metadata.sorted_tables}

    def context(self, **changes):
        return {**{"version": 1, "owner": "owner", "account": self.account,
                   "date_from": None, "date_to": None, "limit": 2}, **changes}

    def encrypted(self, payload):
        nonce = os.urandom(12)
        payload = dict(payload) if isinstance(payload, dict) else payload
        reference = payload.pop("last_reference", "beta") if isinstance(payload, dict) else "beta"
        header = json.dumps(payload).encode()
        reference = reference.encode("utf-8") if isinstance(reference, str) else b"\xff"
        data = len(header).to_bytes(2, "big") + header + reference
        return base64.urlsafe_b64encode(nonce + self.http._cipher.encrypt(nonce, data, views._CURSOR_DOMAIN)).rstrip(b"=").decode()

    def test_all_financial_reads_are_owner_scoped_and_read_only(self):
        self.seed()
        before, statements = self.dump(), []

        def capture(connection, cursor, statement, parameters, context, many):
            statements.append(statement)

        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            with mock.patch("hestia.budget.config.credentials") as credentials, \
                 mock.patch("hestia.budget.client.EnableBankingClient") as client:
                connections = self.http.connections("owner")
                accounts = self.http.accounts("owner", "bank")
                status = self.http.status("owner")
                limits = self.http.limits("owner")
                page = self.http.transactions("owner", self.account)
                balances = self.http.balances("owner", self.account)
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)
        credentials.assert_not_called()
        client.assert_not_called()
        self.assertEqual(self.dump(), before)
        self.assertTrue(all(statement.lstrip().split()[0].upper() in ("SELECT", "BEGIN") for statement in statements))
        self.assertFalse(any("IMMEDIATE" in statement for statement in statements))
        self.assertEqual([row["id"] for row in connections], ["bank"])
        self.assertEqual([row["id"] for row in accounts], [self.account])
        self.assertEqual(status[0]["transactions"], 5)
        self.assertEqual(page["uncertain_total"], 1)
        self.assertEqual(set(accounts[0]["components"]), {"balances", "transactions"})
        self.assertFalse(accounts[0]["paused"])
        self.assertEqual(balances["balances"][0]["reference_date"], None)
        self.assertEqual({row["scope"] for row in limits}, {"component", "application", "provider", "connection"})
        redacted = json.dumps([connections, accounts, status, limits])
        for private in (fixtures.APP, "synthetic-hash", "FOREIGN PRIVATE", "12345678901234567890"):
            self.assertNotIn(private, redacted)

    def test_backend_and_fresh_roles_are_required_inside_reads_and_mutations(self):
        for owner, code in (("missing", 401), ("disabled", 401), ("viewer", 403)):
            with self.subTest(owner=owner), self.assertRaises(BudgetAccessDenied) as caught:
                self.http.accounts(owner)
            self.assertEqual(caught.exception.status, code)
        with self.sessions.begin() as session:
            session.delete(session.get(db.AppMeta, "users_authoritative"))
        for operation in (lambda: self.http.status("owner"),
                          lambda: self.http.set_account_selected("owner", self.account, False, actor="owner", origin="web", expected_version=2)):
            with self.assertRaises(BudgetUsersBackendRequired):
                operation()
        with self.sessions.begin() as session:
            session.add(db.AppMeta(key="users_authoritative", value="0"))
        self.assertEqual(len(self.http.connections("owner")), 1)
        with self.sessions.begin() as session:
            session.get(db.User, "owner").role = "operator"
        with self.assertRaises(BudgetAccessDenied) as caught:
            self.http.set_account_selected("owner", self.account, False, actor="owner", origin="web", expected_version=2)
        self.assertEqual(caught.exception.status, 403)
        self.assertTrue(self.rows(db.BudgetAccount)[0].enabled)

    def test_foreign_missing_and_corrupt_parent_accounts_fail_closed(self):
        self.seed()
        for identity in (self.foreign, "missing", None, [], ""):
            for read in (self.http.balances, self.http.transactions):
                with self.subTest(identity=identity, read=read.__name__), self.assertRaises(BudgetNotFound):
                    read("owner", identity)
        for identity in ("other-bank", "missing", None, [], ""):
            with self.assertRaises(BudgetNotFound):
                self.http.set_connection_paused("owner", identity, True, actor="owner", origin="web", expected_version=1)
        with self.assertRaises(BudgetNotFound):
            self.http.accounts("owner", "other-bank")
        with self.sessions.begin() as session:
            account = session.get(db.BudgetAccount, self.account)
            account.owner, account.identification_hash = "other", "corrupt-parent-primary"
        self.assertEqual(self.http.accounts("owner"), [])
        status = self.http.status("owner")[0]
        self.assertEqual(status["accounts"], [])
        for field in ("selected_accounts", "configured_accounts", "transactions", "uncertain_transactions", "unresolved"):
            self.assertEqual(status[field], 0)
        self.assertTrue(all("account" not in row for row in self.http.limits("owner")))
        for owner in ("owner", "other"):
            with self.assertRaises(BudgetNotFound):
                self.http.balances(owner, self.account)

    def test_page_order_value_date_and_inserts_use_sql_keysets(self):
        self.seed()
        first = self.http.transactions("owner", self.account, limit=2)
        self.assertEqual([tx["entry_reference"] for tx in first["transactions"]], ["future", "beta"])
        self.assertEqual((first["account_total"], first["uncertain_total"]), (5, 1))
        raw = base64.urlsafe_b64decode(first["next_cursor"] + "=" * (-len(first["next_cursor"]) % 4))
        self.assertNotIn(b"beta", raw)
        self.store.commit_snapshot("bank", fixtures.snapshot(fixtures.transaction("new", booking_date="2026-11-23")))
        second = self.http.transactions("owner", self.account, limit=2, cursor=first["next_cursor"])
        self.assertEqual([tx["entry_reference"] for tx in second["transactions"]], ["alpha", "value-only"])
        last = self.http.transactions("owner", self.account, limit=2, cursor=second["next_cursor"])
        self.assertEqual([tx["entry_reference"] for tx in last["transactions"]], ["older"])
        self.assertIsNone(last["next_cursor"])
        self.assertEqual(second["transactions"][0]["source"], "api")
        self.assertEqual(second["transactions"][0]["api_observed_at"], self.now)

    def test_cursor_context_tampering_expiry_and_rotation_are_rejected(self):
        self.seed()
        cursor = self.http.transactions("owner", self.account, limit=2)["next_cursor"]
        cases = ({"limit": 1}, {"date_from": "2026-11-18"}, {"date_to": "2026-11-22"},
                 {"cursor": cursor[:-4] + "AAAA"}, {"cursor": "PRIVATE bad cursor"},
                 {"cursor": "x" * (views.MAX_CURSOR_CHARS + 1)}, {"cursor": []}, {"cursor": ""})
        for change in cases:
            with self.subTest(change=change.keys()), self.assertRaises(BudgetQueryError):
                self.http.transactions("owner", self.account, **{**{"limit": 2, "cursor": cursor}, **change})
        with self.assertRaises(BudgetQueryError):
            self.http.transactions("other", self.foreign, limit=2, cursor=cursor)
        with self.assertRaises(BudgetNotFound):
            self.http.transactions("owner", self.foreign, limit=2, cursor=cursor)
        rotated = BudgetHTTPStore(self.sessions, clock=lambda: self.now, cursor_secret=b"rotated")
        with self.assertRaises(BudgetQueryError):
            rotated.transactions("owner", self.account, limit=2, cursor=cursor)
        self.now += views.CURSOR_TTL
        with self.assertRaises(BudgetQueryError):
            self.http.transactions("owner", self.account, limit=2, cursor=cursor)

    def test_authenticated_cursor_payload_schema_and_time_are_bounded(self):
        self.seed()
        base = {**self.context(), "last_date": "2026-11-20", "last_reference": "beta",
                "issued_at": self.now, "expires_at": self.now + views.CURSOR_TTL}
        changes = ({"issued_at": True}, {"expires_at": False}, {"issued_at": math.inf},
                   {"expires_at": math.nan}, {"issued_at": self.now + 1},
                   {"expires_at": self.now + views.CURSOR_TTL + 1}, {"last_date": None},
                   {"last_date": "PRIVATE"}, {"last_reference": None}, {"last_reference": ""},
                   {"last_reference": "x" * 1025}, {"version": 2}, {"extra": "PRIVATE"})
        for change in changes:
            with self.subTest(change=change.keys()), self.assertRaisesRegex(BudgetQueryError, "Invalid transaction cursor"):
                self.http.transactions("owner", self.account, limit=2, cursor=self.encrypted({**base, **change}))
        with self.assertRaises(BudgetQueryError):
            self.http.transactions("owner", self.account, limit=2, cursor=self.encrypted([]))
        nonce = os.urandom(12)
        malformed = base64.urlsafe_b64encode(nonce + self.http._cipher.encrypt(nonce, b"not json", views._CURSOR_DOMAIN)).decode()
        with self.assertRaises(BudgetQueryError):
            self.http.transactions("owner", self.account, limit=2, cursor=malformed)

    def test_dates_limits_and_unicode_references_keep_money_exact(self):
        self.seed()
        for value in ("20261120", "2026-2-01", "2026-02-30", "PRIVATE", [], 1):
            with self.subTest(value=value), self.assertRaises(BudgetQueryError):
                self.http.transactions("owner", self.account, date_from=value)
        with self.assertRaises(BudgetQueryError):
            self.http.transactions("owner", self.account, date_from="2026-11-21", date_to="2026-11-20")
        for limit in (None, True, False, 0, 201, 1.5, "50"):
            with self.subTest(limit=limit), self.assertRaises(BudgetQueryError):
                self.http.transactions("owner", self.account, limit=limit)
        filtered = self.http.transactions("owner", self.account, date_from="2026-11-19", date_to="2026-11-20")
        self.assertEqual([tx["entry_reference"] for tx in filtered["transactions"]], ["beta", "alpha", "value-only"])
        self.assertEqual(filtered["account_total"], 5)
        self.assertNotIn("total", filtered)
        self.assertEqual(self.http.transactions("owner", self.account, date_from="2027-01-01")["transactions"], [])
        self.assertEqual(self.http.transactions("owner", self.account, date_to="2026-11-18")["transactions"][0]["amount"],
                         "12345678901234567890.12345")
        self.store.commit_snapshot("bank", fixtures.snapshot(fixtures.transaction("😀" * 1024), fixtures.transaction("a")))
        page = self.http.transactions("owner", self.account, limit=1)
        while page["next_cursor"]:
            self.assertLessEqual(len(page["next_cursor"]), views.MAX_CURSOR_CHARS)
            page = self.http.transactions("owner", self.account, limit=1, cursor=page["next_cursor"])

    def test_worst_reference_utf8_and_json_escaping_fit_actual_http_request_line(self):
        for ordinal, reference in enumerate(("😀" * 1024, "\x00" * 1024, "\x01" * 1024, "\\\"" * 512), 1):
            with self.subTest(reference_type=ordinal):
                stamp = f"2027-01-0{ordinal}"
                self.store.commit_snapshot("bank", fixtures.snapshot(
                    fixtures.transaction(reference, booking_date=stamp),
                    fixtures.transaction(reference[:-1], booking_date=stamp)))
                page = self.http.transactions("owner", self.account, limit=1)
                self.assertEqual(page["transactions"][0]["entry_reference"], reference)
                cursor = page["next_cursor"]
                target = "/api/budget/transactions?" + urlencode({"account_id": self.account, "limit": 1, "cursor": cursor})
                self.assertLessEqual(len(cursor), views.MAX_CURSOR_CHARS)
                self.assertLess(len(("GET " + target + " HTTP/1.1").encode("ascii")), 8190)
                following = self.http.transactions("owner", self.account, limit=1, cursor=cursor)
                self.assertEqual(following["transactions"][0]["entry_reference"], reference[:-1])
                for owner in ("😀" * 128, "\x00" * 128):
                    context = self.context(owner=owner, date_from="2026-01-01", date_to="2027-12-31", limit=200)
                    cursor = self.http._cursor(context, (stamp, reference), self.now)
                    self.assertEqual(self.http._decode_cursor(cursor, context, self.now), (stamp, reference))
                    query = {key: value for key, value in context.items() if key in ("date_from", "date_to", "limit")}
                    query.update(account_id=self.account, cursor=cursor)
                    target = "/api/budget/transactions?" + urlencode(query)
                    self.assertLessEqual(len(cursor), views.MAX_CURSOR_CHARS)
                    self.assertLess(len(("GET " + target + " HTTP/1.1").encode("ascii")), 8190)

    def test_constructor_requires_only_existing_nonempty_secret_bytes(self):
        for secret in (None, "PRIVATE", b""):
            with self.subTest(secret=secret), self.assertRaises(BudgetQueryError):
                BudgetHTTPStore(self.sessions, cursor_secret=secret)

    def test_balances_and_archived_metadata_are_separate_bounded_and_portable(self):
        self.seed()
        with self.sessions.begin() as session:
            account = session.get(db.BudgetAccount, self.account)
            balance = {"name": "Booked", "balance_type": "CLBD", "balance_amount": {"amount": "-1.2345", "currency": "PLN"},
                       "PRIVATE-extra": "never return raw JSON"}
            account.balances_json = json.dumps([balance] * 201)
            for ordinal in range(22):
                session.add(db.BudgetImportBatch(id=str(ordinal), fingerprint=str(ordinal), account_id=self.account,
                                                 fetched_at=ordinal, imported_at=self.now, report_json="{}",
                                                 balances_json=None if ordinal == 0 else json.dumps([balance])))
        value = self.http.balances("owner", self.account)
        self.assertEqual(len(value["balances"]), 200)
        self.assertTrue(value["balances_truncated"])
        self.assertEqual(value["balances_total"], 201)
        self.assertEqual(value["balances"][0]["balance_amount"]["amount"], "-1.2345")
        self.assertEqual(len(value["archived_imports"]), 20)
        self.assertTrue(value["archived_has_more"])
        self.assertEqual(value["archived_imports"][0]["fetched_at"], 21)
        self.assertEqual(value["archived_imports"][0]["balance_count"], 1)
        self.assertNotIn("PRIVATE-extra", json.dumps(value))
        self.assertNotIn("balances", value["archived_imports"][0])

    def test_web_controls_versions_noops_and_private_atomic_audit(self):
        initial = self.http.accounts("owner")[0]
        for desired in (True, False):
            with self.assertRaises(BudgetControlConflict) as caught:
                self.http.set_account_selected("owner", self.account, desired, actor="owner", origin="web")
            self.assertEqual(caught.exception.current, initial)
        selected = self.http.set_account_selected("owner", self.account, False, actor="owner", origin="web",
                                                  expected_version=initial["control_version"])
        self.assertTrue(selected["changed"])
        self.assertEqual(selected["control_version"], initial["control_version"] + 1)
        same = self.http.set_account_selected("owner", self.account, False, actor="owner", origin="web",
                                              expected_version=initial["control_version"])
        self.assertFalse(same["changed"])
        with self.assertRaises(BudgetControlConflict):
            self.http.set_account_selected("owner", self.account, True, actor="owner", origin="web",
                                            expected_version=initial["control_version"])
        for invalid in (True, 0, "1"):
            with self.assertRaises(BudgetStoreError):
                self.http.confirm_account_kind("owner", self.account, "credit_card", actor="owner", origin="web", expected_version=invalid)
        confirmed = self.http.confirm_account_kind("owner", self.account, "credit_card", actor="owner", origin="web",
                                                    expected_version=selected["control_version"])
        self.assertEqual(confirmed["control_version"], selected["control_version"] + 1)
        self.assertFalse(confirmed["selected"])
        same = self.http.confirm_account_kind("owner", self.account, "credit_card", actor="owner", origin="web", expected_version=1)
        self.assertFalse(same["changed"])
        audit = [row for row in self.rows(db.BudgetAccountAudit) if row.origin == "web"]
        self.assertEqual([(row.actor, row.field) for row in audit], [("owner", "selected"), ("owner", "kind")])
        self.assertEqual(self.rows(db.BudgetConnectionAudit), [])

    def test_foreign_controls_missing_version_and_actor_cannot_escape_owner_scope(self):
        for actor, origin in (("other", "web"), ("owner", "forged")):
            with self.assertRaises(BudgetStoreError):
                self.http.set_account_selected("owner", self.account, False, actor=actor, origin=origin, expected_version=2)
        with self.assertRaises(BudgetNotFound):
            self.http.set_account_selected("owner", self.foreign, False, actor="owner", origin="web")
        with self.assertRaises(BudgetNotFound):
            self.http.confirm_account_kind("owner", "missing", "credit_card", actor="owner", origin="web")
        for paused in (False, True):
            with self.assertRaises(BudgetControlConflict):
                self.http.set_connection_paused("owner", "bank", paused, actor="owner", origin="web")
        with self.assertRaises(BudgetStoreError):
            self.http.set_connection_paused("owner", "bank", 1, actor="owner", origin="web", expected_version=1)

    def test_audit_failure_rolls_back_version_and_pause_preserves_selection_and_quota(self):
        self.seed()
        before = self.http.accounts("owner")[0]
        with mock.patch.object(self.http, "_audit", side_effect=RuntimeError("synthetic audit failure")), self.assertRaises(RuntimeError):
            self.http.set_account_selected("owner", self.account, False, actor="owner", origin="web", expected_version=before["control_version"])
        self.assertEqual(self.http.accounts("owner")[0], before)
        connection = self.http.connections("owner")[0]
        paused = self.http.set_connection_paused("owner", "bank", True, actor="owner", origin="web", expected_version=connection["control_version"])
        self.assertTrue(paused["changed"])
        self.assertEqual(paused["control_version"], connection["control_version"] + 1)
        repeated = self.http.set_connection_paused("owner", "bank", True, actor="owner", origin="web", expected_version=1)
        self.assertFalse(repeated["changed"])
        with self.assertRaises(BudgetControlConflict):
            self.http.set_connection_paused("owner", "bank", False, actor="owner", origin="web", expected_version=1)
        self.store.configure(self.config)
        self.assertTrue(self.http.accounts("owner")[0]["selected"])
        self.assertFalse(self.http.accounts("owner")[0]["active"])
        self.assertEqual(self.store.selected_hashes("bank", "owner"), set())
        self.assertFalse(BudgetScheduler(self.store).can_start(self.config.connections[0]))
        with self.assertRaises(BudgetDeferredError):
            BudgetScheduler(self.store).request_wait(self.config.connections[0])
        for operation in (lambda: self.store.start_attempt("bank"),
                          lambda: self.store.start_component_attempt("bank", "synthetic-hash", "transactions", attempt_id="round"),
                          lambda: self.store.commit_snapshot("bank", fixtures.snapshot(fixtures.transaction("late")))):
            with self.assertRaises(BudgetPausedError):
                operation()
        self.assertEqual(self.http.transactions("owner", self.account)["account_total"], 5)
        self.assertEqual(self.rows(db.BudgetConnectionAudit)[0].field, "paused")

    def test_migration_preserves_existing_history_choices_and_limits(self):
        self.seed()
        db.reset_engine_cache()
        engine = db.make_engine(self.path)
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            command.downgrade(db._alembic_config(connection), "0010")
            tables = ("budget_accounts", "budget_connections", "budget_transactions", "budget_components",
                      "budget_account_audit", "budget_acquisitions", "budget_request_limits")
            before = {table: [tuple(row) for row in connection.exec_driver_sql(f"SELECT * FROM {table}")] for table in tables}
            command.upgrade(db._alembic_config(connection), "0011")
            self.assertTrue(all(tuple(row) == (1, 0) for row in connection.exec_driver_sql("SELECT control_version,paused FROM budget_connections")))
            self.assertTrue(all(row[0] == 1 for row in connection.exec_driver_sql("SELECT control_version FROM budget_accounts")))
            self.assertEqual(connection.exec_driver_sql("SELECT COUNT(*) FROM budget_connection_audit").scalar_one(), 0)
            command.downgrade(db._alembic_config(connection), "0010")
            for table in tables:
                self.assertEqual([tuple(row) for row in connection.exec_driver_sql(f"SELECT * FROM {table}")], before[table])
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_key_check").all(), [])
            command.upgrade(db._alembic_config(connection), "head")


if __name__ == "__main__":
    unittest.main()
