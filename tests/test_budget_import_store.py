"""Synthetic offline history, API authority, replay and migration invariants."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import unittest
from unittest import mock

from alembic import command
from sqlalchemy import select

from hestia import db
from hestia.budget.import_file import parse_import
from hestia.budget.store import BudgetStoreError
from tests import test_budget_store as fixtures


def quota_state(rows):
    """Exact durable endpoint debits, retry hints and shared transport limits."""
    return {model.__tablename__: sorted(
        [tuple(getattr(row, column.name) for column in model.__table__.columns) for row in rows(model)],
        key=repr) for model in (db.BudgetComponent, db.BudgetAcquisition, db.BudgetRequestLimit)}


def seed_retry_state(sessions, account_id, now, *, application_id=fixtures.APP):
    with sessions.begin() as session:
        for operation in ("transactions", "balances"):
            row = session.get(db.BudgetComponent, (account_id, operation))
            row.retry_at, row.suspended = now + 604800, True
            session.add(db.BudgetAcquisition(id="synthetic-debit-" + operation, account_id=account_id,
                                             operation=operation, attempt_id="synthetic-previous-round",
                                             started_at=now - 300, origin="api"))
        session.add_all([
            db.BudgetRequestLimit(scope="application", key=application_id, next_attempt=now + 600,
                                  next_request_at=now + 1, last_request_at=now - 2,
                                  retry_at=now + 900, suspended=True),
            db.BudgetRequestLimit(scope="connection", key="bank", next_attempt=now + 1200,
                                  next_request_at=0, last_request_at=None,
                                  retry_at=now + 1800, suspended=False)])


class BudgetImportStoreTests(unittest.TestCase):
    setUp = fixtures.BudgetStoreTests.setUp
    tearDown = fixtures.BudgetStoreTests.tearDown
    rows = fixtures.BudgetStoreTests.rows

    def imported(self, *transactions, fetched_at=None, config=None, balances=True):
        config = fixtures.configuration() if config is None else config
        when = self.now - 100 if fetched_at is None else fetched_at
        raw = []
        for value in transactions:
            value = dict(value)
            money = {key: value.pop(key) for key in ("amount", "currency")}
            raw.append({**value, "transaction_amount": money})
        item = config.connections[0]
        document = {"version": 1, "connection_id": item.id, "aspsp": {"name": item.bank_name, "country": item.country},
                    "identification_hash": item.accounts[0],
                    "fetched_at": datetime.fromtimestamp(when, timezone.utc).isoformat(), "complete": True,
                    "transactions": {"transactions": raw}}
        if balances:
            document["balances"] = {"balances": [{"name": "Historical", "balance_type": "ITBD",
                                                    "balance_amount": {"amount": "-98.765", "currency": "PLN"}}]}
        return parse_import(document, config, now=self.now)

    def test_disabled_metadata_full_history_and_exact_replay(self):
        config = fixtures.configuration()
        first = fixtures.transaction(booking_date="2001-01-01")
        pending = fixtures.transaction("pending", status="PDNG")
        missing = fixtures.transaction(None)
        imported = self.imported(first, pending, missing, missing)
        report = self.store.import_snapshot(config, imported)
        self.assertEqual(report["inserted"], 1)
        self.assertEqual(report["unresolved"], 3)
        self.assertFalse(report["duplicate"])
        state = self.store.status()[0]
        self.assertFalse(state["enabled"])
        self.assertEqual(state["selected_accounts"], 0)
        self.assertEqual(state["offline_imports"], 1)
        self.assertEqual(state["offline_transactions"], 1)
        self.assertEqual(state["offline_unresolved"], 3)
        self.assertIsNone(state["last_success"])
        self.assertEqual(state["next_attempt"], 0)
        account = self.rows(db.BudgetAccount)[0]
        self.assertFalse(account.enabled)
        self.assertFalse(account.configured)
        self.assertEqual(self.rows(db.BudgetComponent), [])
        self.assertEqual(account.balances_json, "[]")
        self.assertIsNone(account.observed_at)
        batch = self.rows(db.BudgetImportBatch)[0]
        self.assertEqual(json.loads(batch.balances_json)[0]["balance_amount"]["amount"], "-98.765")
        tx = self.rows(db.BudgetTransaction)[0]
        self.assertEqual(tx.source, "offline_import")
        self.assertIsNone(tx.api_observed_at)
        self.assertEqual(tx.observed_at, imported.fetched_at)
        self.assertEqual(json.loads(tx.payload_json)["booking_date"], "2001-01-01")
        replay = self.store.import_snapshot(config, imported)
        self.assertTrue(replay["duplicate"])
        for field in ("inserted", "updated", "preserved_api", "duplicates_of_api", "stale", "unresolved", "unchanged"):
            self.assertEqual(replay[field], 0)
        original_report = json.loads(self.rows(db.BudgetImportBatch)[0].report_json)
        self.assertEqual(original_report["inserted"], 1)
        self.assertEqual(original_report["unresolved"], 3)
        self.assertEqual(len(self.rows(db.BudgetImportBatch)), 1)
        self.assertEqual(len(self.rows(db.BudgetImportUnresolved)), 3)
        self.assertEqual(self.store.status()[0], state)

    def test_api_precedence_and_api_state_unaffected(self):
        config = fixtures.configuration()
        fixtures.prepare_selected(self.store, config)
        self.store.start_attempt("bank")
        self.store.commit_snapshot("bank", fixtures.snapshot(fixtures.transaction(), fixtures.transaction(None)))
        self.store.record_failure("bank", "rate_limit")
        seed_retry_state(self.sessions, self.rows(db.BudgetAccount)[0].id, self.now)
        state = self.store.status()[0]
        quota_before = quota_state(self.rows)
        component_rows = self.rows(db.BudgetComponent)
        self.assertEqual(len(component_rows), 2)
        component_before = [(row.operation, row.last_attempt, row.last_success, row.last_error,
                             row.failures, row.next_attempt, row.attempt_id, row.lease_until)
                            for row in component_rows]
        api_tx = self.rows(db.BudgetTransaction)[0]
        observation = self.imported(fixtures.transaction(), fixtures.transaction(amount="99"),
                                    fixtures.transaction("historical"), fetched_at=self.now - 10)
        report = self.store.import_snapshot(config, observation)
        self.assertEqual(report["inserted"], 1)
        self.assertEqual(report["preserved_api"], 2)
        self.assertEqual(report["duplicates_of_api"], 0)
        self.assertEqual(report["unresolved"], 2)
        old_ref = next(row for row in self.rows(db.BudgetTransaction) if row.entry_reference == "ref-1")
        self.assertEqual(old_ref.payload_json, api_tx.payload_json)
        self.assertFalse(old_ref.uncertain)
        self.assertEqual(old_ref.api_observed_at, self.now)
        after = self.store.status()[0]
        for field in ("enabled", "last_attempt", "last_success", "next_attempt", "failures", "last_error", "valid_until", "accounts"):
            self.assertEqual(after[field], state[field])
        self.assertEqual(after["unresolved"], state["unresolved"])
        self.assertEqual([(row.operation, row.last_attempt, row.last_success, row.last_error,
                           row.failures, row.next_attempt, row.attempt_id, row.lease_until)
                          for row in self.rows(db.BudgetComponent)], component_before)
        self.assertEqual(quota_state(self.rows), quota_before)

    def test_api_duplicate_with_different_remittance_keeps_api_payload_and_counts_in_report(self):
        config = fixtures.configuration()
        fixtures.prepare_selected(self.store, config)
        api = fixtures.transaction(value_date="2026-11-21")
        self.store.commit_snapshot("bank", fixtures.snapshot(api))
        duplicate = self.imported({**api, "remittance_information": ["Offline display"]})
        report = self.store.import_snapshot(config, duplicate)
        self.assertEqual((report["preserved_api"], report["duplicates_of_api"], report["unresolved"]), (1, 1, 0))
        self.assertEqual(self.rows(db.BudgetImportUnresolved), [])
        self.assertEqual(json.loads(self.rows(db.BudgetTransaction)[0].payload_json), api)
        batch = self.rows(db.BudgetImportBatch)[0]
        self.assertEqual(json.loads(batch.report_json)["duplicates_of_api"], 1)
        self.assertEqual(self.store.import_snapshot(config, duplicate)["duplicates_of_api"], 0)

    def test_api_financial_differences_remain_quarantined(self):
        config = fixtures.configuration()
        fixtures.prepare_selected(self.store, config)
        api = fixtures.transaction(value_date="2026-11-21")
        self.store.commit_snapshot("bank", fixtures.snapshot(api))
        for change in ({"amount": "9"}, {"currency": "EUR"}, {"booking_date": "2026-11-21"},
                       {"value_date": "2026-11-20"}, {"value_date": None},
                       {"credit_debit_indicator": "CRDT"}, {"transaction_date": "2026-11-20"}):
            with self.subTest(change=change):
                imported = self.imported({**api, **change, "remittance_information": ["Offline display"]})
                report = self.store.import_snapshot(config, imported)
                self.assertEqual((report["preserved_api"], report["duplicates_of_api"], report["unresolved"]), (1, 0, 1))
                self.assertEqual({row.reason for row in self.rows(db.BudgetImportUnresolved)}, {"api_preserved"})
        self.assertEqual(json.loads(self.rows(db.BudgetTransaction)[0].payload_json), api)

    def test_api_duplicate_requires_the_same_account(self):
        config = fixtures.configuration(accounts=("synthetic-hash", "synthetic-other-hash"))
        fixtures.prepare_selected(self.store, config)
        observation = fixtures.snapshot()
        observation.accounts += fixtures.snapshot(fixtures.transaction(), account_hash="synthetic-other-hash").accounts
        self.store.commit_snapshot("bank", observation)
        report = self.store.import_snapshot(config, self.imported(fixtures.transaction(), config=config))
        self.assertEqual((report["inserted"], report["duplicates_of_api"], report["unresolved"]), (1, 0, 0))

    def test_api_duplicate_comparison_fails_closed_on_invalid_stored_payload(self):
        from hestia.budget.store import _matches_api_transaction

        tx = fixtures.transaction()
        for invalid in ("{", "[]", '{"currency":"PLN","currency":"PLN"}'):
            with self.subTest(invalid=invalid):
                self.assertFalse(_matches_api_transaction(invalid, tx))

    def test_strict_observation_order_and_equal_time_conflicts(self):
        config = fixtures.configuration()
        self.store.import_snapshot(config, self.imported(fixtures.transaction(), fetched_at=700, balances=False))
        self.assertIsNone(self.rows(db.BudgetImportBatch)[0].balances_json)
        same = self.imported(fixtures.transaction(), fetched_at=700, balances=True)
        self.assertEqual(self.store.import_snapshot(config, same)["unchanged"], 1)
        self.assertEqual(self.store.import_snapshot(config, self.imported(fixtures.transaction(amount="3"), fetched_at=600))["stale"], 1)
        conflict = self.imported(fixtures.transaction(amount="2"), fetched_at=700)
        self.assertEqual(self.store.import_snapshot(config, conflict)["unresolved"], 1)
        self.assertTrue(self.rows(db.BudgetTransaction)[0].uncertain)
        # Equal-time matching data in a distinct batch cannot silently clear the conflict.
        match = self.imported(fixtures.transaction(), fetched_at=700, balances=False)
        match = replace(match, digest="distinct-synthetic-batch")
        self.assertEqual(self.store.import_snapshot(config, match)["unresolved"], 1)
        newer = self.imported(fixtures.transaction(amount="4"), fetched_at=800)
        self.assertEqual(self.store.import_snapshot(config, newer)["updated"], 1)
        self.assertFalse(self.rows(db.BudgetTransaction)[0].uncertain)
        self.assertEqual(json.loads(self.rows(db.BudgetTransaction)[0].payload_json)["amount"], "4")

    def test_new_conflict_keeps_durable_uncertain_head_against_old_file(self):
        config = fixtures.configuration()
        conflict = self.imported(fixtures.transaction(), fixtures.transaction(amount="2"), fetched_at=800)
        report = self.store.import_snapshot(config, conflict)
        self.assertEqual(report["unresolved"], 2)
        self.assertEqual(report["inserted"], 0)
        self.assertTrue(self.rows(db.BudgetTransaction)[0].uncertain)
        self.assertEqual(self.store.import_snapshot(config, self.imported(fixtures.transaction(), fetched_at=700))["stale"], 1)
        older_conflict = self.imported(fixtures.transaction(), fixtures.transaction(amount="3"), fetched_at=600)
        report = self.store.import_snapshot(config, older_conflict)
        self.assertEqual((report["stale"], report["unresolved"]), (2, 2))
        self.assertEqual(self.rows(db.BudgetTransaction)[0].observed_at, 800)
        self.assertEqual(self.store.status()[0]["transactions"], 0)

    def test_api_conflict_promotes_authority_without_falsifying_payload_source(self):
        config = fixtures.configuration()
        self.store.import_snapshot(config, self.imported(fixtures.transaction(), fetched_at=700))
        fixtures.prepare_selected(self.store, config)
        self.store.commit_snapshot("bank", fixtures.snapshot(fixtures.transaction(), fixtures.transaction(amount="2")))
        canonical = self.rows(db.BudgetTransaction)[0]
        self.assertEqual(canonical.source, "offline_import")
        self.assertEqual(canonical.api_observed_at, self.now)
        self.assertTrue(canonical.uncertain)
        self.now += 100
        report = self.store.import_snapshot(config, self.imported(fixtures.transaction(amount="3"), fetched_at=self.now))
        self.assertEqual((report["preserved_api"], report["unresolved"]), (1, 1))
        self.assertTrue(self.rows(db.BudgetTransaction)[0].uncertain)
        self.store.commit_snapshot("bank", fixtures.snapshot(fixtures.transaction(amount="4")))
        final = self.rows(db.BudgetTransaction)[0]
        self.assertEqual(final.source, "api")
        self.assertFalse(final.uncertain)
        self.assertEqual(final.api_observed_at, self.now)

    def test_legacy_api_quarantine_is_protected_even_without_existing_canonical(self):
        config = fixtures.configuration()
        fixtures.prepare_selected(self.store, config)
        account = self.rows(db.BudgetAccount)[0]
        with self.sessions.begin() as session:
            for i, tx in enumerate((fixtures.transaction(), fixtures.transaction(amount="2"), fixtures.transaction(None))):
                session.add(db.BudgetUnresolved(account_id=account.id, ordinal=i, payload_json=json.dumps(tx), observed_at=800))
        report = self.store.import_snapshot(config, self.imported(fixtures.transaction(), fetched_at=700))
        self.assertEqual((report["preserved_api"], report["unresolved"]), (1, 1))
        self.assertEqual(report["duplicates_of_api"], 0)
        prior = self.rows(db.BudgetTransaction)[0]
        self.assertEqual(prior.source, "api")
        self.assertTrue(prior.uncertain)
        self.assertEqual(prior.api_observed_at, 800)

    def test_owner_selection_and_identity_checked_even_for_duplicate(self):
        config = fixtures.configuration()
        observation = self.imported(fixtures.transaction())
        self.store.import_snapshot(config, observation)
        for invalid in (replace(config, connections=()), fixtures.configuration(accounts=())):
            with self.assertRaises(BudgetStoreError):
                self.store.import_snapshot(invalid, observation)
        for user_change in ("viewer", "disabled", "missing"):
            invalid = fixtures.configuration(owner=user_change)
            with self.assertRaises(BudgetStoreError):
                self.store.import_snapshot(invalid, observation)
        with self.assertRaises(BudgetStoreError):
            self.store.import_snapshot(replace(config, application_id="different-app"), observation)
        other = fixtures.configuration(id="other-bank")
        with self.assertRaises(BudgetStoreError):
            self.store.import_snapshot(other, replace(observation, connection_id="other-bank"))
        # A digest cannot be reassigned to another selected account.
        other = fixtures.configuration(accounts=("another-hash",))
        with self.assertRaises(BudgetStoreError):
            self.store.import_snapshot(other, replace(observation, identification_hash="another-hash"))
        with self.sessions.begin() as session:
            session.get(db.User, "owner").disabled = True
        with self.assertRaises(BudgetStoreError):
            self.store.import_snapshot(config, observation)
        self.assertEqual(len(self.rows(db.BudgetImportBatch)), 1)

    def test_failure_rolls_back_metadata_batch_and_entries_and_allows_restart(self):
        config = fixtures.configuration()
        observation = self.imported(fixtures.transaction("first"), fixtures.transaction("second"))
        actual = self.store._offline_transaction
        calls = 0

        def interrupted(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("synthetic crash")
            return actual(*args)

        with mock.patch.object(self.store, "_offline_transaction", side_effect=interrupted), self.assertRaises(RuntimeError):
            self.store.import_snapshot(config, observation)
        for model in (db.BudgetConnection, db.BudgetAccount, db.BudgetTransaction, db.BudgetImportBatch, db.BudgetImportUnresolved):
            self.assertEqual(self.rows(model), [])
        db.reset_engine_cache()
        self.engine, self.sessions = db.init_db(self.path)
        from hestia.budget.store import BudgetStore
        self.store = BudgetStore(self.sessions, clock=lambda: self.now)
        self.assertEqual(self.store.import_snapshot(config, observation)["inserted"], 2)

    def test_migration_0007_preserves_existing_api_data_and_downgrades(self):
        db.reset_engine_cache()
        engine = db.make_engine(self.path)
        with engine.begin() as connection:
            command.downgrade(db._alembic_config(connection), "0006")
            connection.exec_driver_sql("INSERT INTO budget_connections (id,owner,application_id,bank_name,country,enabled,next_attempt,failures) VALUES ('bank','owner','app','Bank','PL',1,900,1)")
            connection.exec_driver_sql("INSERT INTO budget_accounts (id,connection_id,owner,identification_hash,enabled,balances_json) VALUES ('account','bank','owner','hash',1,'[]')")
            connection.exec_driver_sql("INSERT INTO budget_transactions (account_id,entry_reference,payload_json,observed_at,uncertain) VALUES ('account','ref','{}',800,0)")
            command.upgrade(db._alembic_config(connection), "head")
            row = connection.exec_driver_sql("SELECT source, api_observed_at, observed_at, payload_json FROM budget_transactions").one()
            self.assertEqual(tuple(row), ("api", 800.0, 800.0, "{}"))
            command.downgrade(db._alembic_config(connection), "0006")
            self.assertEqual(connection.exec_driver_sql("SELECT payload_json FROM budget_transactions").scalar_one(), "{}")
            command.upgrade(db._alembic_config(connection), "head")
        engine.dispose()

    def test_migration_0008_preserves_selection_history_provenance_and_quota(self):
        db.reset_engine_cache()
        engine = db.make_engine(self.path)
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            command.downgrade(db._alembic_config(connection), "0007")
            connection.exec_driver_sql(
                "INSERT INTO budget_connections "
                "(id,owner,application_id,bank_name,country,enabled,next_attempt,failures,last_success,last_error) "
                "VALUES ('bank','owner','app','Synthetic Bank','PL',1,90000,2,800,'rate_limit')")
            connection.exec_driver_sql(
                "INSERT INTO budget_accounts "
                "(id,connection_id,owner,identification_hash,enabled,balances_json,observed_at) "
                "VALUES (?,?,?,?,?,?,?)",
                [("selected", "bank", "owner", "api-hash", 1, '[{"synthetic":true}]', 800),
                 ("retired", "bank", "owner", "offline-hash", 0, "[]", None)])
            connection.exec_driver_sql(
                "INSERT INTO budget_transactions "
                "(account_id,entry_reference,payload_json,observed_at,uncertain,source,api_observed_at) "
                "VALUES (?,?,?,?,?,?,?)",
                [("selected", "api-ref", '{"amount":"12.345"}', 800, 1, "api", 850),
                 ("retired", "offline-ref", '{"amount":"99.01"}', 700, 0, "offline_import", None)])
            connection.exec_driver_sql(
                "INSERT INTO budget_unresolved (account_id,ordinal,payload_json,observed_at) "
                "VALUES (?,?,?,?)", ("selected", 0, '{"synthetic":"api-conflict"}', 850))
            connection.exec_driver_sql(
                "INSERT INTO budget_import_batches "
                "(id,fingerprint,account_id,fetched_at,imported_at,balances_json,report_json) "
                "VALUES ('batch','digest','retired',700,900,'[]','{}')")
            connection.exec_driver_sql(
                "INSERT INTO budget_import_unresolved (batch_id,ordinal,reason,payload_json) "
                "VALUES ('batch',0,'missing_reference','{}')")

            def historical_rows():
                return {table: [tuple(row) for row in connection.exec_driver_sql(f"SELECT * FROM {table}")]
                        for table in ("budget_connections", "budget_transactions", "budget_unresolved",
                                      "budget_import_batches", "budget_import_unresolved")}

            before = historical_rows()
            for revision in ("0008", "0007", "0008"):
                if revision == "0007":
                    command.downgrade(db._alembic_config(connection), revision)
                else:
                    command.upgrade(db._alembic_config(connection), revision)
                    rows = connection.exec_driver_sql(
                        "SELECT id,configured,enabled,kind,kind_confirmed_at,kind_confirmed_by,"
                        "kind_confirmation_origin FROM budget_accounts ORDER BY id").all()
                    self.assertEqual([tuple(row) for row in rows],
                                     [("retired", 0, 0, "unknown", None, None, None),
                                      ("selected", 1, 1, "unknown", None, None, None)])
                self.assertEqual(historical_rows(), before)
                accounts = connection.exec_driver_sql(
                    "SELECT id,identification_hash,enabled,balances_json,observed_at "
                    "FROM budget_accounts ORDER BY id").all()
                self.assertEqual([tuple(row) for row in accounts],
                                 [("retired", "offline-hash", 0, "[]", None),
                                  ("selected", "api-hash", 1, '[{"synthetic":true}]', 800.0)])
                self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_key_check").all(), [])

    def test_migration_0009_backfills_only_known_api_freshness_and_preserves_history(self):
        db.reset_engine_cache()
        engine = db.make_engine(self.path)
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            command.downgrade(db._alembic_config(connection), "0008")
            connection.exec_driver_sql(
                "INSERT INTO budget_connections "
                "(id,owner,application_id,bank_name,country,enabled,next_attempt,failures,last_success,last_error) "
                "VALUES ('bank','owner','app','Synthetic Bank','PL',1,90000,2,900,'rate_limit')")
            connection.exec_driver_sql(
                "INSERT INTO budget_accounts "
                "(id,connection_id,owner,identification_hash,enabled,configured,kind,kind_confirmed_at,"
                "kind_confirmed_by,kind_confirmation_origin,balances_json,observed_at,last_attempt,last_error) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [("api", "bank", "owner", "api-hash", 1, 1, "credit_card", 500,
                  "operator:1000", "operator", '[{"synthetic":true}]', 800, 850, "network"),
                 ("offline", "bank", "owner", "offline-hash", 0, 0, "unknown", None,
                  None, None, "[]", None, None, None)])
            connection.exec_driver_sql(
                "INSERT INTO budget_transactions "
                "(account_id,entry_reference,payload_json,observed_at,uncertain,source,api_observed_at) "
                "VALUES (?,?,?,?,?,?,?)",
                [("api", "api-ref", '{"amount":"12.345"}', 800, 1, "api", 850),
                 ("offline", "offline-ref", '{"amount":"99.01"}', 700, 0, "offline_import", None)])
            connection.exec_driver_sql(
                "INSERT INTO budget_unresolved (account_id,ordinal,payload_json,observed_at) "
                "VALUES ('api',0,'{}',850)")
            connection.exec_driver_sql(
                "INSERT INTO budget_import_batches "
                "(id,fingerprint,account_id,fetched_at,imported_at,balances_json,report_json) "
                "VALUES ('batch','digest','offline',700,900,'[]','{}')")
            connection.exec_driver_sql(
                "INSERT INTO budget_import_unresolved (batch_id,ordinal,reason,payload_json) "
                "VALUES ('batch',0,'missing_reference','{}')")
            connection.exec_driver_sql(
                "INSERT INTO budget_account_audit "
                "(account_id,owner,actor,origin,field,old_value_json,new_value_json,changed_at) "
                "VALUES ('api','owner','operator:1000','operator','kind','{}','{}',500)")

            def historical_rows():
                tables = ("budget_connections", "budget_accounts", "budget_transactions", "budget_unresolved",
                          "budget_import_batches", "budget_import_unresolved", "budget_account_audit")
                return {table: [tuple(row) for row in connection.exec_driver_sql(f"SELECT * FROM {table}")]
                        for table in tables}

            before = historical_rows()
            for revision in ("0009", "0008", "0009"):
                if revision == "0008":
                    command.downgrade(db._alembic_config(connection), revision)
                else:
                    command.upgrade(db._alembic_config(connection), revision)
                    rows = connection.exec_driver_sql(
                        "SELECT account_id,operation,last_attempt,last_success,last_error,failures,"
                        "next_attempt,attempt_id,lease_until FROM budget_components ORDER BY account_id,operation").all()
                    self.assertEqual([tuple(row) for row in rows],
                                     [("api", "balances", None, 800.0, None, 0, 90000.0, None, None),
                                      ("api", "transactions", None, 800.0, None, 0, 90000.0, None, None),
                                      ("offline", "balances", None, None, None, 0, 90000.0, None, None),
                                      ("offline", "transactions", None, None, None, 0, 90000.0, None, None)])
                self.assertEqual(historical_rows(), before)
                self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_key_check").all(), [])


if __name__ == "__main__":
    unittest.main()
