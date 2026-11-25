"""Offline tests of the operator reset and its exact deletion scope."""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

from sqlalchemy import select

from hestia import db
from hestia.budget import __main__ as cli, gmail_config
from hestia.budget.gmail_client import GmailClient
from hestia.budget.gmail_store import (DAY, GMAIL_MESSAGE_DOMAIN, GmailStore, _failed_gateway_key, _payment_order_key,
                                       _payment_payee_key, _payment_sender_key, _retry_window_key, digest)
from hestia.budget.gmail_sync import GmailSync, _sync_lock
from tests.test_budget_gmail_sync import (NOW, OWNER, SETTINGS, FakeClock, FakeGmailTransport, SyntheticProcessor, page,
                                          response)


class GmailResetTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(directory.name) / "reset.db"
        self.engine, self.sessions = db.init_db(self.path)
        self.clock = FakeClock()
        self.settings = replace(SETTINGS, backfill_days=90)
        self.store = GmailStore(self.sessions, clock=self.clock.wall)
        self.transport = FakeGmailTransport()
        client = GmailClient(self.transport, monotonic=self.clock.monotonic, sleep=self.clock.sleep)
        self.processor = SyntheticProcessor(self.clock)
        self.service = GmailSync(self.settings, self.store, client, self.processor, self.path)
        with self.sessions.begin() as session:
            session.add_all([
                db.User(username=OWNER, password_hash="synthetic", role="admin"),
                db.User(username="other", password_hash="synthetic", role="admin"),
                db.AppMeta(key="users_authoritative", value="1"),
            ])

    def _invoke(self, *options, path=None):
        out, err = StringIO(), StringIO()
        with mock.patch.object(gmail_config, "load_settings", return_value=self.settings), \
                mock.patch.object(cli.time, "time", self.clock.wall), redirect_stdout(out), redirect_stderr(err):
            status = cli.main(["--db", str(self.path if path is None else path), "gmail", "reset-unconfirmed-payments",
                               *options])
        return status, out.getvalue(), err.getvalue()

    @staticmethod
    def _mail_key(name):
        return digest(SETTINGS.hmac_secret, GMAIL_MESSAGE_DOMAIN, name)

    @staticmethod
    def _payment_keys(receipt_id):
        return {_payment_sender_key(receipt_id), _payment_order_key(receipt_id), _payment_payee_key(receipt_id)}

    def _seed(self):
        self.service.transition(OWNER, 1, "active")
        with self.sessions.begin() as session:
            state = session.get(db.BudgetGmailState, OWNER)
            state.history_id, state.last_sync_at = "17", NOW - DAY
            state.last_attempt_at, state.backoff_until = NOW - 1, NOW + 120
            session.add(db.BudgetConnection(id="bank", owner=OWNER, application_id="fixture", bank_name="Fixture",
                                            country="PL", enabled=True, next_attempt=0, failures=0))
            session.flush()
            session.add(db.BudgetAccount(id="account", connection_id="bank", owner=OWNER,
                                         identification_hash="fixture", enabled=True, balances_json="{}"))
            session.flush()
            session.add(db.BudgetTransaction(account_id="account", entry_reference="entry", payload_json="{}",
                                             observed_at=NOW, uncertain=False))
            session.flush()
            receipts = {
                "payment": {},
                "orphan": {},
                "invoice": {"document_kind": "invoice"},
                "receipt": {"document_kind": "receipt"},
                "confirmed": {"status": "confirmed", "payment_method": "card"},
                "review": {"status": "needs_review", "payment_method": "card"},
                "linked": {"status": "needs_review", "payment_method": "card", "linked_account_id": "account",
                           "linked_entry_ref": "entry", "linked_fingerprint": "fixture", "linked_evidence": "{}"},
                "forwarded": {"forwarded_manually": 1},
                "attachment": {"source_kind": "attachment"},
                "foreign": {"owner": "other"},
            }
            for receipt_id, values in receipts.items():
                fields = dict(owner=OWNER, status="imported", document_kind="payment_confirmation", source_kind="email",
                              receipt_date="2026-11-20", total_amount="9", currency="PLN", version=1,
                              created_at=NOW, updated_at=NOW, raw_shop_name="Synthetic shop")
                session.add(db.BudgetReceipt(id=receipt_id, **(fields | values)))
                for key in self._payment_keys(receipt_id):
                    session.add(db.AppMeta(key=key, value="synthetic"))
            session.flush()
            attached = session.get(db.BudgetReceipt, "confirmed")
            attached.gateway = "Example Pay"
            attached.evidence_for_receipt_id = "payment"
            for receipt_id in receipts.keys() - {"orphan"}:
                owner = "other" if receipt_id == "foreign" else OWNER
                session.add(self._message(receipt_id, receipt_id=receipt_id, owner=owner))
            self.removed_messages = {
                self._mail_key(name) for name in ("payment", "bound", "duplicate", "duplicate-forward", "non-receipt")
            }
            session.add_all([
                self._message("bound", "skipped_oversized", receipt_id="payment"),
                self._message("duplicate", "skipped_duplicate"),
                self._message("duplicate-forward", "skipped_duplicate", trust="owner_forward"),
                self._message("non-receipt", "skipped_non_receipt"),
                self._message("non-receipt-forward", "skipped_non_receipt", trust="spouse_forward"),
                self._message("failed", "parse_failed"),
                self._message("tombstone", "deleted"),
                self._message("foreign-duplicate", "skipped_duplicate", owner="other"),
                self._message("foreign-non-receipt", "skipped_non_receipt", owner="other"),
                db.BudgetReceiptLine(receipt_id="payment", owner=OWNER, line_no=1, line_kind="other",
                                     raw_name="Synthetic payment", raw_name_key="synthetic payment",
                                     quantity="1", gross_line_total="9", net_line_total="9"),
                db.BudgetAnalyticsAudit(owner=OWNER, subject_type="receipt", subject_id="payment", action="seed",
                                        before_json="{}", after_json="{}", changed_at=NOW),
            ])
            for owner in (OWNER, "other"):
                session.add(db.AppMeta(
                    key=_failed_gateway_key(owner, "Example Pay", "a" * 64),
                    value=json.dumps({"gateway": "Example Pay", "ref_hmac": "a" * 64, "expires_at": NOW + DAY}),
                ))

    def _message(self, name, outcome="imported", *, receipt_id=None, trust="direct_allowlist", owner=OWNER):
        return db.BudgetGmailMessage(owner=owner, gmail_id_hmac=self._mail_key(name), trust_path=trust,
                                     outcome=outcome, receipt_id=receipt_id, internal_date=NOW, created_at=NOW)

    def _snapshot(self):
        with self.sessions() as session:
            models = (db.BudgetReceipt, db.BudgetReceiptLine, db.BudgetGmailMessage, db.BudgetGmailState,
                      db.AppMeta, db.BudgetAnalyticsAudit)
            return {model: session.execute(select(model.__table__).order_by(*model.__table__.primary_key)).all()
                    for model in models}

    def test_dry_run_and_real_reset_preserve_protected_rows_and_rescan_full_window(self):
        self._seed()
        before = self._snapshot()
        expected = {"receipts": 2, "gmail_messages": 5}
        code, out, err = self._invoke("--dry-run")
        self.assertEqual((code, json.loads(out), err), (0, expected, ""))
        self.assertEqual(self._snapshot(), before)

        code, out, err = self._invoke()
        self.assertEqual((code, json.loads(out), err), (0, expected, ""))
        self.assertEqual(len(self.transport.requests("/messages")), 0)
        after = self._snapshot()
        self.assertEqual(after[db.BudgetGmailMessage],
                         [row for row in before[db.BudgetGmailMessage] if row.gmail_id_hmac not in self.removed_messages])
        self.assertEqual(after[db.BudgetReceiptLine], [])
        self.assertEqual(after[db.BudgetAnalyticsAudit],
                         [row for row in before[db.BudgetAnalyticsAudit] if row.subject_type != "receipt"])
        receipts = {row.id: row for row in after[db.BudgetReceipt]}
        self.assertEqual(set(receipts), {row.id for row in before[db.BudgetReceipt]} - {"payment", "orphan"})
        for row in before[db.BudgetReceipt]:
            if row.id in receipts and row.id != "confirmed":
                self.assertEqual(receipts[row.id], row)
        self.assertEqual((receipts["confirmed"].status, receipts["confirmed"].evidence_for_receipt_id,
                          receipts["confirmed"].owner, receipts["confirmed"].version),
                         ("confirmed", None, OWNER, 1))
        meta = {row.key for row in after[db.AppMeta]}
        self.assertEqual(meta, ({row.key for row in before[db.AppMeta]} - self._payment_keys("payment")
                                - self._payment_keys("orphan") - {_failed_gateway_key(OWNER, "Example Pay", "a" * 64)})
                         | {_retry_window_key(OWNER)})
        state = self.store.state(OWNER)
        self.assertEqual((state.run_requested, state.force_full_resync, state.next_sync_at, state.backoff_until,
                          state.grant_generation), (1, 1, NOW, NOW + 120, 1))
        code, out, err = self._invoke()
        self.assertEqual((code, json.loads(out), err), (0, {"receipts": 0, "gmail_messages": 0}, ""))
        self.assertEqual(self._snapshot(), after)

        self.clock.now += 120
        self.transport.routes[("/messages", None)] = response(page("invoice", "payment", "non-receipt"))
        self.service.sync(OWNER, poll=True)
        self.assertIn(f"after:{int(NOW) - 90 * DAY}", self.transport.requests("/messages")[0]["q"][0])
        self.assertEqual(self.processor.processed, ["payment", "non-receipt"])
        self.assertFalse(self.store.retry_full_window(OWNER))

    def test_disabled_grant_and_stale_generation_refuse_untouched(self):
        before = self._snapshot()
        self.assertEqual(self._invoke("--dry-run"), (2, "", "budget_receipt_gmail_disabled\n"))
        self.assertEqual(self._snapshot(), before)

        self._seed()
        stale = self.store.state(OWNER)
        self.store.transition(OWNER, stale.version, "disabled")
        before = self._snapshot()
        self.assertEqual(self._invoke(), (2, "", "budget_receipt_gmail_disabled\n"))
        self.assertEqual(self._snapshot(), before)
        self.store.transition(OWNER, stale.version + 1, "active")
        before = self._snapshot()
        with mock.patch.object(GmailStore, "state", return_value=stale):
            self.assertEqual(self._invoke(), (2, "", "budget_receipt_gmail_disabled\n"))
        self.assertEqual(self._snapshot(), before)

    def test_in_flight_sync_and_mailbox_mismatch_refuse_untouched(self):
        self._seed()
        before = self._snapshot()
        with _sync_lock(self.path):
            self.assertEqual(self._invoke(), (2, "", "budget_receipt_gmail_sync_busy\n"))
        self.assertEqual(self._snapshot(), before)
        self.store.opt_in_error(OWNER, self.store.state(OWNER).version, attention="identity_mismatch",
                                code="mailbox_mismatch", http_status=403)
        before = self._snapshot()
        self.assertEqual(self._invoke(), (2, "", "mailbox_mismatch\n"))
        self.assertEqual(self._snapshot(), before)

    def test_database_error_rolls_back_and_output_contains_no_payload(self):
        self._seed()
        before = self._snapshot()
        with self.engine.begin() as connection:
            connection.exec_driver_sql("""
                CREATE TRIGGER synthetic_abort BEFORE UPDATE ON budget_gmail_state
                WHEN NEW.force_full_resync = 1
                BEGIN SELECT RAISE(ABORT, 'synthetic sensitive value'); END
            """)
        self.assertEqual(self._invoke(), (1, "", "Gmail payment reset failed\n"))
        self.assertEqual(self._snapshot(), before)

    def test_requires_an_existing_migrated_database(self):
        self._seed()
        missing = self.path.with_name("missing.db")
        status, out, err = self._invoke(path=missing)
        self.assertEqual((status, out, err), (2, "", "Gmail payment reset requires an existing database\n"))
        self.assertFalse(missing.exists())
        db.downgrade_db(self.path, "0030")
        self.assertEqual(self._invoke("--dry-run"), (1, "", "Gmail payment reset failed\n"))
        engine = db.make_engine(self.path)
        self.addCleanup(engine.dispose)
        with engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one(), "0030")
            self.assertEqual(connection.exec_driver_sql("SELECT count(*) FROM budget_receipts").scalar_one(), 10)
