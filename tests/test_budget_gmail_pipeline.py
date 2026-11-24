"""Synthetic Gmail API through the Unix parser and SQLite import pipeline."""
from dataclasses import replace
from email.message import EmailMessage
from pathlib import Path
from tempfile import TemporaryDirectory
import base64
import subprocess
import sys
import time
from unittest import TestCase

from sqlalchemy import select

from hestia import db
from hestia.budget.gmail_client import GmailClient
from hestia.budget.gmail_message import GmailMessageProcessor
from hestia.budget.gmail_store import GMAIL_MESSAGE_DOMAIN, GmailStore, digest
from hestia.budget.gmail_sync import GmailSync
from hestia.budget.gmail_worker import ReceiptParserWorker
from hestia.budget.receipt_parser import ReceiptError
from hestia.budget.receipt_store import BudgetReceiptStore
from tests.test_budget_gmail_sync import FakeClock, FakeGmailTransport, OWNER, SETTINGS, page, response


class GmailPipelineTests(TestCase):
    def test_scoped_forward_and_direct_invoice_dedupe_without_body_storage(self):
        merchant_sender = "synthetic-billing@pl.orange.com"
        with TemporaryDirectory() as directory:
            path = Path(directory) / "gmail-test.db"
            self.addCleanup(db.reset_engine_cache)
            engine, sessions = db.init_db(path)
            with sessions.begin() as session:
                session.add_all([db.User(username=OWNER, password_hash="synthetic", role="admin"),
                                 db.AppMeta(key="users_authoritative", value="1")])
            body = (Path(__file__).parent / "fixtures" / "gmail" / "pdf_text" / "orange_invoice.txt").read_text(
                encoding="utf-8")
            body += "<p>synthetic-body-only-marker</p><p>PESEL 44051401458</p><p>4111 1111 1111 1111</p>"
            direct = self._email(merchant_sender, body)
            direct["Message-ID"] = "<synthetic-invoice-key@orange.example.test>"
            forward = self._email("spouse@example.test", "Synthetic cover note")
            forward.add_attachment(direct)
            spoof = self._email("intruder@untrusted.example.test", body)
            spoof.replace_header("From", merchant_sender)
            untrusted_forward = self._email("intruder@untrusted.example.test", "Synthetic cover note")
            untrusted_forward.add_attachment(direct)
            review = self._email("spouse@example.test", "Synthetic cover note")
            review.add_attachment(self._email("synthetic-other@pl.orange.com", body))
            messages = {"mock-forward": forward, "mock-direct": direct, "mock-spoof": spoof,
                        "mock-untrusted-forward": untrusted_forward, "mock-review": review}

            transport, clock = FakeGmailTransport(), FakeClock()
            transport.routes[("/messages", None)] = response(page(*messages))
            for message_id, message in messages.items():
                transport.routes[("/messages/" + message_id, None)] = self._provider_message(message_id, message)
            settings = replace(SETTINGS, sender_allowlist=(merchant_sender,),
                               known_merchant_domains=("pl.orange.com",))
            client = GmailClient(transport, monotonic=clock.monotonic, sleep=clock.sleep)
            worker = ReceiptParserWorker(str(Path(directory) / "parse.sock"))
            store = GmailStore(sessions, clock=clock.wall)
            service = GmailSync(settings, store, client, GmailMessageProcessor(settings, worker), path)
            with subprocess.Popen((sys.executable, "-B", "-c",
                                       "from hestia.budget.gmail_worker import serve; import sys; serve(sys.argv[1])",
                                       worker.socket_path), env={}, stderr=subprocess.DEVNULL) as sidecar:
                try:
                    for _ in range(50):
                        if worker.ready():
                            break
                        time.sleep(0.01)
                    self.assertTrue(worker.ready())
                    service.transition(OWNER, 1, "active")
                    outcome = service.sync(OWNER)
                    self.assertEqual((outcome["imported_count"], outcome["skipped_count"]), (2, 3))
                finally:
                    sidecar.terminate()

            with sessions() as session:
                invoice, = list(session.scalars(select(db.BudgetReceipt).where(db.BudgetReceipt.status == "imported")))
                review, = list(session.scalars(select(db.BudgetReceipt).where(db.BudgetReceipt.status == "needs_review")))
                self.assertEqual((review.forwarded_manually, review.forwarded_sender_domain, review.invoice_number),
                                 (1, "pl.orange.com", None))
                self.assertEqual((invoice.raw_shop_name, invoice.raw_nip, invoice.invoice_number, invoice.total_amount,
                                  invoice.currency, invoice.receipt_date, invoice.due_date),
                                 ("Orange", "5260250995", "26110200000001", "88.1", "PLN", "2026-11-14", "2026-11-30"))
                self.assertEqual((invoice.status, invoice.source_kind, invoice.document_kind), ("imported", "email", "invoice"))
                self.assertIsNone(invoice.raw_ocr_text)
                self.assertIsNone(invoice.image_relpath)
                rows = {row.gmail_id_hmac: row for row in session.scalars(select(db.BudgetGmailMessage))}
                imported = rows[digest(settings.hmac_secret, GMAIL_MESSAGE_DOMAIN, "mock-forward")]
                self.assertEqual((imported.trust_path, imported.outcome, imported.receipt_id),
                                 ("spouse_forward", "imported", invoice.id))
                self.assertEqual({row.outcome for row in rows.values()},
                                 {"imported", "skipped_duplicate", "skipped_untrusted_sender"})
            for denied in ("mock-spoof", "mock-untrusted-forward"):
                self.assertEqual([item["format"] for item in transport.requests("/messages/" + denied)], [["metadata"]])
            receipts = BudgetReceiptStore(sessions, clock=clock.wall, cursor_secret=b"synthetic-session")
            summary, = receipts.list(OWNER, {"document_kind": "invoice", "status": "imported"})["receipts"]
            self.assertEqual((summary["invoice_number"], summary["due_date"], summary["document_kind"]),
                             ("[usunięto]", "2026-11-30", "invoice"))
            self.assertIsNone(summary["math_delta"])
            self.assertEqual(receipts.list(OWNER, {"document_kind": "receipt"})["receipts"], [])
            with self.assertRaises(ReceiptError):
                receipts.list(OWNER, {"document_kind": "unknown"})
            with engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
            stored = path.read_bytes()
            for private in (*messages, "synthetic-body-only-marker", "synthetic-invoice-key", "spouse@example.test",
                            "44051401458", "4111 1111 1111 1111", SETTINGS.owner_mailbox):
                self.assertNotIn(private.encode(), stored)

    @staticmethod
    def _email(sender, text, *, subtype="plain"):
        domain = sender.partition("@")[2]
        message = EmailMessage()
        message["From"] = sender
        message["Authentication-Results"] = f"mx.google.com; dkim=pass header.d={domain}; dmarc=pass header.from={domain}"
        message.set_content(text, subtype=subtype)
        return message

    @staticmethod
    def _provider_message(message_id, message):
        payload = message.as_bytes()
        metadata = {"id": message_id, "sizeEstimate": len(payload), "internalDate": "1795566945123",
                    "payload": {"headers": [{"name": name, "value": value} for name, value in message.raw_items()
                                            if name in {"From", "Sender", "Authentication-Results", "DKIM-Signature"}]}}
        raw = {"id": message_id, "raw": base64.urlsafe_b64encode(payload).decode().rstrip("=")}

        def select_response(params):
            if params["format"] == ["metadata"]:
                return response(metadata)
            assert params["format"] == ["raw"]
            return response(raw)

        return select_response
