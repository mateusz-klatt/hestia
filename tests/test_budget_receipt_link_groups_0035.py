"""Exercise the receipt-group migration and the reciprocal SQLite guards."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from hestia import db
from tests.migration_head import HEAD


_GROUPS = db.BudgetReceiptLinkGroup.__table__
_MEMBERS = db.BudgetReceiptLinkMember.__table__
_RECEIPTS = db.BudgetReceipt.__table__
_LEGS = db.BudgetTransferLeg.__table__
_LINK = {"linked_account_id": "account", "linked_fingerprint": "fp", "linked_evidence": "{}"}


def _insert_group(conn, group_id, reference, **changes):
    values = dict(id=group_id, owner="alice", account_id="account", entry_reference=reference,
                  currency="PLN", tx_amount="120", credit_debit_indicator="DBIT", status="confirmed",
                  linked_fingerprint="fp", linked_evidence="{}", version=1, created_at=1, updated_at=1)
    conn.execute(_GROUPS.insert().values(**(values | changes)))


def _insert_receipt(conn, receipt_id, **changes):
    values = dict(id=receipt_id, owner="alice", status="confirmed", source_kind="attachment",
                  receipt_date="2026-11-20", currency="PLN", total_amount="60", payment_method="card",
                  version=1, created_at=1, updated_at=1)
    conn.execute(_RECEIPTS.insert().values(**(values | changes)))


def _member(conn, group_id, receipt_id, owner="alice"):
    conn.execute(_MEMBERS.insert().values(group_id=group_id, receipt_id=receipt_id, owner=owner))


def _transfer_leg(conn, reference):
    conn.execute(_LEGS.insert().values(account_id="account", entry_reference=reference, decision_id="decision"))


def _schema(conn):
    return conn.exec_driver_sql(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY name"
    ).all()


class ReceiptLinkGroupsMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(directory.name) / "groups.db"
        prior = db.make_engine(self.path)
        try:
            db._run_sqlite_migration(prior, lambda conn: command.upgrade(db._alembic_config(conn), "0034"))
            with prior.begin() as conn:
                self._seed(conn)
                self.before_schema = _schema(conn)
                self.before_receipts = conn.execute(select(_RECEIPTS).order_by(_RECEIPTS.c.id)).all()
            db._run_sqlite_migration(prior, lambda conn: command.upgrade(db._alembic_config(conn), "0035"))
            with prior.connect() as conn:
                self.receipt_group_schema = _schema(conn)
        finally:
            prior.dispose()
        self.engine, _ = db.init_db(self.path)

    @staticmethod
    def _seed(conn):
        conn.execute(db.User.__table__.insert(), [
            dict(username=owner, password_hash="synthetic") for owner in ("alice", "bob")
        ])
        conn.execute(db.BudgetConnection.__table__.insert(), [
            dict(id=owner, owner=owner, application_id="app", bank_name="Bank", country="PL",
                 enabled=True, next_attempt=0, failures=0) for owner in ("alice", "bob")
        ])
        conn.execute(db.BudgetAccount.__table__.insert(), [
            dict(id=account, owner=owner, connection_id=owner, identification_hash=account, enabled=True,
                 balances_json="[]") for account, owner in (("account", "alice"), ("peer", "alice"), ("other", "bob"))
        ])
        conn.execute(db.BudgetTransaction.__table__.insert(), [
            dict(account_id=account, entry_reference=entry, payload_json="{}", observed_at=1, uncertain=False)
            for account, entry in (("account", "legacy"), ("account", "group"), ("account", "spare"),
                                   ("account", "transfer"), ("peer", "transfer-peer"), ("other", "group"))
        ])
        conn.execute(db.BudgetTransferDecision.__table__.insert().values(
            id="decision", owner="alice", debit_account_id="account", debit_ref="transfer",
            credit_account_id="peer", credit_ref="transfer-peer", source="manual", status="accepted",
            debit_fingerprint="fp", credit_fingerprint="fp", rule_version=1, version=1, created_at=1, updated_at=1,
        ))
        _insert_receipt(conn, "legacy", linked_entry_ref="legacy", **_LINK)
        _insert_receipt(conn, "member-a")
        _insert_receipt(conn, "member-b")
        _insert_receipt(conn, "evidence", document_kind="payment_confirmation", gateway="PayU")
        _insert_receipt(conn, "attached", document_kind="payment_confirmation", gateway="PayU",
                        evidence_for_receipt_id="member-a")
        _insert_receipt(conn, "other-member", owner="bob")

    def test_upgrade_and_empty_downgrade_preserve_0034(self):
        with self.engine.connect() as conn:
            self.assertEqual(conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one(), HEAD)
            upgraded_schema = _schema(conn)
            self.assertTrue(set(self.before_schema).issubset(self.receipt_group_schema))
            self.assertEqual(conn.execute(select(_RECEIPTS).order_by(_RECEIPTS.c.id)).all(), self.before_receipts)
            self.assertEqual(compare_metadata(MigrationContext.configure(conn), db.metadata), [])
            self.assertEqual(len([row for row in upgraded_schema if row.name.endswith(("_guard_insert", "_guard_update"))
                                  and "link_group_guard" in row.name]), 8)

        db.downgrade_db(self.path, "0034")
        prior = db.make_engine(self.path)
        try:
            with prior.connect() as conn:
                self.assertEqual(conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one(), "0034")
                self.assertEqual(_schema(conn), self.before_schema)
                self.assertEqual(conn.execute(select(_RECEIPTS).order_by(_RECEIPTS.c.id)).all(), self.before_receipts)
                self.assertEqual(conn.exec_driver_sql("PRAGMA foreign_key_check").all(), [])
        finally:
            prior.dispose()

        current, _ = db.init_db(self.path)
        with current.connect() as conn:
            self.assertEqual(_schema(conn), upgraded_schema)
            self.assertEqual(conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one(), 1)
            self.assertEqual(conn.exec_driver_sql("PRAGMA integrity_check").all(), [("ok",)])
            self.assertEqual(conn.exec_driver_sql("PRAGMA foreign_key_check").all(), [])

    def test_populated_downgrade_refuses_without_changes(self):
        with self.engine.begin() as conn:
            _insert_group(conn, "g", "group", evidence_receipt_id="evidence")
            _member(conn, "g", "member-a")
            _member(conn, "g", "member-b")
            schema = _schema(conn)
        with self.assertRaisesRegex(RuntimeError, "Unlink all groups first"):
            db.downgrade_db(self.path, "0034")
        reopened = db.make_engine(self.path)
        self.addCleanup(reopened.dispose)
        with reopened.connect() as conn:
            self.assertEqual(conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one(), HEAD)
            self.assertEqual(_schema(conn), schema)
            self.assertEqual(conn.exec_driver_sql("PRAGMA foreign_key_check").all(), [])
        _, sessions = db.init_db(self.path)
        with sessions() as session:
            self.assertEqual(session.get(db.BudgetReceiptLinkGroup, "g").evidence_receipt_id, "evidence")
            self.assertEqual(session.scalars(select(db.BudgetReceiptLinkMember.receipt_id).order_by(
                db.BudgetReceiptLinkMember.receipt_id)).all(), ["member-a", "member-b"])

    def test_legacy_link_reservations_reject_both_write_orders(self):
        with self.engine.begin() as conn:
            _insert_group(conn, "g", "group")
            for status in ("confirmed", "needs_review"):
                with self.subTest(status=status):
                    conn.execute(_RECEIPTS.update().where(_RECEIPTS.c.id == "legacy").values(status=status))
                    with self.assertRaisesRegex(IntegrityError, "already has a receipt link"):
                        _insert_group(conn, "conflict", "legacy")
                    with self.assertRaisesRegex(IntegrityError, "already has a receipt link"):
                        conn.execute(_GROUPS.update().where(_GROUPS.c.id == "g").values(entry_reference="legacy"))

                    conn.execute(_GROUPS.update().where(_GROUPS.c.id == "g").values(status=status))
                    with self.assertRaisesRegex(IntegrityError, "receipt transaction is reserved by a group"):
                        _insert_receipt(conn, "conflict", status=status, linked_entry_ref="group", **_LINK)
                    with self.assertRaisesRegex(IntegrityError, "receipt transaction is reserved by a group"):
                        conn.execute(_RECEIPTS.update().where(_RECEIPTS.c.id == "member-a").values(
                            status=status, linked_entry_ref="group", **_LINK))
            conn.execute(_GROUPS.delete().where(_GROUPS.c.id == "g"))
            _insert_receipt(conn, "released", linked_entry_ref="group", **_LINK)

    def test_transfer_reservations_reject_both_write_orders_and_allow_release(self):
        with self.engine.begin() as conn:
            _transfer_leg(conn, "transfer")
            _insert_group(conn, "g", "group")
            for status in ("confirmed", "needs_review"):
                with self.subTest(status=status):
                    with self.assertRaisesRegex(IntegrityError, "transaction is a transfer leg"):
                        _insert_group(conn, "conflict", "transfer", status=status)
                    with self.assertRaisesRegex(IntegrityError, "transaction is a transfer leg"):
                        conn.execute(_GROUPS.update().where(_GROUPS.c.id == "g").values(
                            entry_reference="transfer", status=status))
                    conn.execute(_GROUPS.update().where(_GROUPS.c.id == "g").values(status=status))
                    with self.assertRaisesRegex(IntegrityError, "transfer transaction is reserved by a group"):
                        _transfer_leg(conn, "group")
                    with self.assertRaisesRegex(IntegrityError, "transfer transaction is reserved by a group"):
                        conn.execute(_LEGS.update().where(_LEGS.c.entry_reference == "transfer").values(
                            entry_reference="group"))
            conn.execute(_GROUPS.delete().where(_GROUPS.c.id == "g"))
            _transfer_leg(conn, "group")

    def test_membership_and_evidence_eligibility(self):
        with self.engine.begin() as conn:
            _insert_receipt(conn, "draft", status="draft", gateway="PayU")
            _insert_receipt(conn, "imported", status="imported")
            _insert_receipt(conn, "forwarding", evidence_for_receipt_id="member-a")
            for receipt_id in ("draft", "legacy", "attached", "member-a"):
                with self.subTest(evidence=receipt_id), self.assertRaisesRegex(IntegrityError, "eligible for group evidence"):
                    _insert_group(conn, "g", "group", evidence_receipt_id=receipt_id)
            _insert_group(conn, "g", "group", evidence_receipt_id="evidence", fee_amount="1.50")
            for receipt_id in ("draft", "imported", "legacy", "attached", "forwarding", "evidence"):
                with self.subTest(member=receipt_id), self.assertRaisesRegex(IntegrityError, "eligible for group membership"):
                    _member(conn, "g", receipt_id)
            # A receipt is allowed to be the target of a separate v1f gateway attachment.
            _member(conn, "g", "member-a")
            _member(conn, "g", "member-b")
            _insert_receipt(conn, "new-attached", gateway="PayU", evidence_for_receipt_id="member-b")
            conn.execute(_RECEIPTS.update().where(_RECEIPTS.c.id == "member-b").values(status="needs_review"))
            with self.assertRaisesRegex(IntegrityError, "eligible for group membership"):
                conn.execute(_MEMBERS.update().where(_MEMBERS.c.receipt_id == "member-a").values(receipt_id="imported"))
            with self.assertRaisesRegex(IntegrityError, "eligible for group evidence"):
                conn.execute(_GROUPS.update().where(_GROUPS.c.id == "g").values(evidence_receipt_id="attached"))

    def test_receipt_updates_and_replacements_preserve_assigned_role(self):
        with self.engine.begin() as conn:
            _insert_group(conn, "g", "group", evidence_receipt_id="evidence")
            _member(conn, "g", "member-a")
            for receipt_id, role, changes in (
                ("member-a", "membership", {"gateway": "PayU"}),
                ("member-a", "membership", {"evidence_for_receipt_id": "member-b"}),
                ("member-a", "membership", {"status": "imported"}),
                ("member-a", "membership", {"linked_entry_ref": "spare", **_LINK}),
                ("evidence", "evidence", {"gateway": None}),
                ("evidence", "evidence", {"evidence_for_receipt_id": "member-a"}),
                ("evidence", "evidence", {"status": "draft"}),
                ("evidence", "evidence", {"status": "needs_review"}),
                ("evidence", "evidence", {"linked_entry_ref": "spare", **_LINK}),
            ):
                with self.subTest(receipt=receipt_id, changes=changes), self.assertRaisesRegex(
                        IntegrityError, f"eligible for group {role}"):
                    conn.execute(_RECEIPTS.update().where(_RECEIPTS.c.id == receipt_id).values(**changes))

            with self.assertRaisesRegex(IntegrityError, "eligible for group membership"):
                conn.exec_driver_sql("INSERT OR REPLACE INTO budget_receipts "
                                     "(id, owner, status, source_kind, version, created_at, updated_at) "
                                     "VALUES ('member-a', 'alice', 'imported', 'email', 1, 1, 1)")
            conn.execute(_GROUPS.update().where(_GROUPS.c.id == "g").values(status="needs_review"))
            conn.execute(_RECEIPTS.update().where(_RECEIPTS.c.id == "evidence").values(status="needs_review"))
            with self.assertRaisesRegex(IntegrityError, "eligible for group evidence"):
                conn.execute(_GROUPS.update().where(_GROUPS.c.id == "g").values(status="confirmed"))

    def test_group_uniqueness_composite_foreign_keys_checks_and_indexes(self):
        with self.engine.begin() as conn:
            for field, value in (("currency", "pln"), ("tx_amount", ""), ("tx_amount", "1" * 81),
                                 ("credit_debit_indicator", "DEBIT"), ("fee_amount", "-1"), ("status", "draft"),
                                 ("linked_evidence", ""), ("linked_evidence", "1" * 4097), ("version", 0)):
                with self.subTest(field=field, value=str(value)[:12]), self.assertRaisesRegex(IntegrityError, "CHECK"):
                    _insert_group(conn, "g", "group", **{field: value})
            with self.assertRaisesRegex(IntegrityError, "FOREIGN KEY"):
                _insert_group(conn, "g", "not-a-transaction")
            with self.assertRaisesRegex(IntegrityError, "FOREIGN KEY"):
                _insert_group(conn, "g", "group", account_id="other")
            with self.assertRaisesRegex(IntegrityError, "eligible for group evidence"):
                _insert_group(conn, "g", "group", account_id="other", owner="bob", evidence_receipt_id="evidence")
            _insert_group(conn, "g", "group", evidence_receipt_id="evidence")
            with self.assertRaisesRegex(IntegrityError, "UNIQUE"):
                _insert_group(conn, "duplicate-tx", "group", status="needs_review")
            with self.assertRaisesRegex(IntegrityError, "UNIQUE"):
                _insert_group(conn, "duplicate-evidence", "spare", evidence_receipt_id="evidence")
            _insert_group(conn, "g2", "spare")
            _insert_group(conn, "g3", "group", account_id="other", owner="bob", credit_debit_indicator="CRDT")
            _member(conn, "g", "member-a")
            with self.assertRaisesRegex(IntegrityError, "UNIQUE"):
                _member(conn, "g2", "member-a")
            with self.assertRaisesRegex(IntegrityError, "FOREIGN KEY"):
                _member(conn, "g", "other-member", owner="bob")
            with self.assertRaisesRegex(IntegrityError, "eligible for group membership"):
                _member(conn, "g3", "member-b", owner="bob")
            with self.assertRaisesRegex(IntegrityError, "FOREIGN KEY"):
                conn.execute(_GROUPS.delete().where(_GROUPS.c.id == "g"))
            with self.assertRaisesRegex(IntegrityError, "FOREIGN KEY"):
                conn.execute(_RECEIPTS.delete().where(_RECEIPTS.c.id.in_(("evidence", "member-a"))))

            indexes = {item["name"]: item for item in inspect(conn).get_indexes(_GROUPS.name)}
            evidence = indexes["uq_budget_receipt_link_groups_evidence"]
            self.assertTrue(evidence["unique"])
            self.assertEqual(str(evidence["dialect_options"]["sqlite_where"]), "evidence_receipt_id IS NOT NULL")
            self.assertEqual(indexes["ix_budget_receipt_link_groups_owner_status"]["column_names"],
                             ["owner", "status", "id"])
            self.assertEqual(inspect(conn).get_indexes(_MEMBERS.name)[0]["column_names"], ["group_id"])
