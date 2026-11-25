"""Synthetic Gmail API through the Unix parser and SQLite import pipeline."""
from dataclasses import replace
from datetime import UTC, date, datetime
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
from hestia.budget.gmail_payment import ImportedPayment
from hestia.budget.gmail_store import (GMAIL_MESSAGE_DOMAIN, IMPORT_TTL, PROVENANCE_TTL, GmailStore,
                                      PendingMessage, PendingPayment, _payment_sender_key, digest)
from hestia.budget.gmail_sync import GmailSync
from hestia.budget.gmail_trust import PaymentSender
from hestia.budget.gmail_worker import ReceiptParserWorker
from hestia.budget.receipt_parser import ReceiptError
from hestia.budget.receipt_store import BudgetReceiptStore
from tests.test_budget_gmail_sync import FakeClock, FakeGmailTransport, OWNER, SETTINGS, page, response


class GmailPipelineTests(TestCase):
    def test_payment_dedupe_uses_owner_and_current_config_and_prunes_sender_provenance(self):
        with TemporaryDirectory() as directory:
            self.addCleanup(db.reset_engine_cache)
            _engine, sessions = db.init_db(Path(directory) / "payment-provenance.db")
            other = "other-owner"
            with sessions.begin() as session:
                session.add_all(db.User(username=owner, password_hash="synthetic", role="admin")
                                for owner in (OWNER, other))
                session.add(db.AppMeta(key="users_authoritative", value="1"))
            clock = FakeClock()
            store = GmailStore(sessions, clock=clock.wall)
            for owner in (OWNER, other):
                store.transition(owner, 1, "active")

            fields = ImportedPayment("Example merchant", "Example purchase", "12", "PLN", ())
            day = date(2026, 11, 25)
            gateway_hmac, shop_hmac = "a" * 64, "b" * 64
            gateway = PendingPayment(fields, day, gateway_hmac, (shop_hmac,))
            shop = PendingPayment(fields, day, shop_hmac, (gateway_hmac,))

            def commit(owner, message_hmac, payment, rfc_hmac=None):
                pending = PendingMessage(message_hmac, clock.now, "direct_allowlist", "imported",
                                         rfc_message_id_hmac=rfc_hmac, payment=payment)
                return store.commit(owner, 1, {}, (pending,))

            rfc_hmac = "f" * 64
            self.assertEqual(commit(OWNER, "c" * 64, gateway, rfc_hmac), (1, 0, True))
            self.assertEqual(commit(other, "d" * 64, shop, rfc_hmac), (1, 0, True))
            self.assertEqual(commit(OWNER, "1" * 64, shop, rfc_hmac), (0, 1, True))
            # The only rule now configured for this owner is for the shop.
            self.assertEqual(commit(OWNER, "e" * 64, replace(shop, opposite_sender_hmacs=())), (1, 0, True))

            with sessions.begin() as session:
                self.assertEqual(session.get(db.BudgetGmailMessage, (OWNER, "1" * 64)).outcome, "skipped_non_receipt")
                receipt_id = session.get(db.BudgetGmailMessage, (OWNER, "c" * 64)).receipt_id
                receipt = session.get(db.BudgetReceipt, receipt_id)
                receipt.status, receipt.payment_method = "confirmed", "card"
                self.assertEqual(session.get(db.AppMeta, _payment_sender_key(receipt_id)).value, gateway_hmac)

            clock.now += PROVENANCE_TTL + 1
            store.retain(OWNER, 1)
            with sessions() as session:
                self.assertIsNotNone(session.get(db.BudgetReceipt, receipt_id))
                self.assertIsNone(session.get(db.AppMeta, _payment_sender_key(receipt_id)))
                other_receipt_id = session.get(db.BudgetGmailMessage, (other, "d" * 64)).receipt_id
                self.assertIsNotNone(session.get(db.AppMeta, _payment_sender_key(other_receipt_id)))

            store.transition(other, store.state(other).version, "revoked")
            with sessions() as session:
                self.assertIsNone(session.get(db.AppMeta, _payment_sender_key(other_receipt_id)))

    def test_direct_payments_over_socket_dedupe_only_opposite_sender_roles(self):
        shop = "orders@billing.shop.example.test"
        gateway = "orders@gateway.example.test"
        with TemporaryDirectory() as directory:
            path = Path(directory) / "gmail-payments.db"
            self.addCleanup(db.reset_engine_cache)
            engine, sessions = db.init_db(path)
            with sessions.begin() as session:
                session.add_all([db.User(username=OWNER, password_hash="synthetic", role="admin"),
                                 db.AppMeta(key="users_authoritative", value="1")])

            messages = (
                ("shop-first", shop, "15.00 PLN", 25, "imported"),
                ("shop-equal", shop, "15 PLN", 25, "imported"),
                ("gateway-match", gateway, "15 PLN", 27, "skipped_non_receipt"),
                ("gateway-first", gateway, "16 PLN", 25, "imported"),
                ("shop-match", shop, "16 PLN", 23, "skipped_non_receipt"),
                ("gateway-outside", gateway, "15 PLN", 28, "imported"),
                ("gateway-equal", gateway, "16 PLN", 25, "imported"),
                ("shop-other-currency", shop, "16 USD", 25, "imported"),
            )
            transport, clock = FakeGmailTransport(), FakeClock()
            transport.routes[("/messages", None)] = response(page(*(entry[0] for entry in messages)))
            for message_id, sender, amount, day, _outcome in messages:
                if sender == gateway:
                    body = (f"<p>Amount paid: {amount}</p><p>Odbiorca wpłaty: Example Payee</p>"
                            "<p>Tytuł: Example service</p><p>synthetic-payment-body-only-marker</p>")
                    message = self._email(sender, body, subtype="html")
                else:
                    message = self._email(sender, f"Amount paid: {amount}\nsynthetic-payment-body-only-marker")
                message["Subject"] = "Example shop purchase"
                message["Message-ID"] = f"<{message_id}@message.example.test>"
                internal_date = str(int(datetime(2026, 11, day, 1, tzinfo=UTC).timestamp() * 1000))
                transport.routes[("/messages/" + message_id, None)] = self._provider_message(
                    message_id, message, internal_date=internal_date,
                )
            settings = replace(SETTINGS, payment_senders=(
                PaymentSender("@shop.example.test", "Example Shop"),
                PaymentSender(gateway, "Example Gateway", gateway=True),
            ))
            client = GmailClient(transport, monotonic=clock.monotonic, sleep=clock.sleep)
            worker = ReceiptParserWorker(str(Path(directory) / "payment.sock"))
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
                    self.assertEqual((outcome["imported_count"], outcome["skipped_count"]), (6, 2))
                finally:
                    sidecar.terminate()

            query, = transport.requests("/messages")
            self.assertIn('from:"shop.example.test"', query["q"][0])
            self.assertIn(f'from:"{gateway}"', query["q"][0])
            with sessions() as session:
                self.assertEqual(len(session.scalars(select(db.BudgetReceipt)).all()), 6)
                self.assertEqual(len(session.scalars(select(db.BudgetReceiptLine)).all()), 6)
                for message_id, sender, amount, day, expected in messages:
                    with self.subTest(message_id=message_id):
                        key = digest(settings.hmac_secret, GMAIL_MESSAGE_DOMAIN, message_id)
                        provenance = session.get(db.BudgetGmailMessage, (OWNER, key))
                        self.assertEqual((provenance.trust_path, provenance.outcome), ("direct_allowlist", expected))
                        if expected != "imported":
                            self.assertIsNone(provenance.receipt_id)
                            continue
                        receipt = session.get(db.BudgetReceipt, provenance.receipt_id)
                        total, currency = amount.replace(".00", "").split()
                        merchant = "Example Payee" if sender == gateway else "Example Shop"
                        description = "Example service" if sender == gateway else "Example shop purchase"
                        self.assertEqual((receipt.raw_shop_name, receipt.receipt_date, receipt.total_amount,
                                          receipt.currency, receipt.status, receipt.document_kind, receipt.source_kind),
                                         (merchant, f"2026-11-{day}", total, currency, "imported",
                                          "payment_confirmation", "email"))
                        self.assertEqual(receipt.import_expires_at, clock.now + IMPORT_TTL)
                        self.assertEqual((receipt.invoice_number, receipt.due_date, receipt.raw_ocr_text),
                                         (None, None, None))
                        line = session.get(db.BudgetReceiptLine, (receipt.id, 1))
                        self.assertEqual((line.line_kind, line.raw_name, line.raw_name_key, line.quantity,
                                          line.gross_line_total, line.net_line_total),
                                         ("other", description, description.lower(), "1", total, total))
                        self.assertIsNotNone(session.get(db.AppMeta, _payment_sender_key(receipt.id)))

            receipts = BudgetReceiptStore(sessions, clock=clock.wall, cursor_secret=b"synthetic-session")
            summaries = receipts.list(OWNER, {"document_kind": "payment_confirmation"})["receipts"]
            self.assertEqual(len(summaries), 6)
            detail = receipts.detail(OWNER, summaries[0]["receipt_id"])["receipt"]
            self.assertEqual(detail["document_kind"], "payment_confirmation")
            self.assertEqual(detail["lines"][0]["raw_name"], "Example service")
            with engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
            stored = path.read_bytes()
            for private in (shop, gateway, "@shop.example.test", "synthetic-payment-body-only-marker",
                            "message.example.test"):
                self.assertNotIn(private.encode(), stored)

            receipts.delete(OWNER, {"receipt_id": detail["receipt_id"], "expected_version": 1})
            with sessions() as session:
                self.assertIsNone(session.get(db.AppMeta, _payment_sender_key(detail["receipt_id"])))
            clock.now += IMPORT_TTL
            store.retain(OWNER, store.state(OWNER).grant_generation)
            with sessions() as session:
                self.assertEqual(session.scalars(select(db.BudgetReceipt)).all(), [])
                self.assertEqual(session.scalars(select(db.BudgetReceiptLine)).all(), [])
                self.assertEqual(session.scalars(select(db.AppMeta).where(
                    db.AppMeta.key.startswith(_payment_sender_key("")),
                )).all(), [])

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

    def test_pipeline_records_malformed_pdf_and_octet_stream_pdf_reasons_over_socket(self):
        # The sidecar extracts PDFs since #278, so a malformed fixture PDF is rejected.
        with TemporaryDirectory() as directory:
            path = Path(directory) / "gmail-reasons.db"
            self.addCleanup(db.reset_engine_cache)
            _engine, sessions = db.init_db(path)
            with sessions.begin() as session:
                session.add_all([db.User(username=OWNER, password_hash="synthetic", role="admin"),
                                 db.AppMeta(key="users_authoritative", value="1")])

            pdf_mail = self._email("billing@orange.example.test", "unused")
            pdf_mail.set_content(b"%PDF-1.4 fixture", maintype="application", subtype="pdf")
            octet_mail = self._email("billing@orange.example.test", "cover")
            octet_mail.add_attachment(b"%PDF-1.4 fixture", maintype="application", subtype="octet-stream",
                                      filename="Invoice.PDF")
            bad_charset = self._email("billing@orange.example.test", "Invoice body")
            bad_charset.set_param("charset", "not-a-real-charset")
            messages = {"pipe-pdf": pdf_mail, "pipe-octet": octet_mail, "pipe-charset": bad_charset}

            transport, clock = FakeGmailTransport(), FakeClock()
            transport.routes[("/messages", None)] = response(page(*messages))
            for message_id, message in messages.items():
                transport.routes[("/messages/" + message_id, None)] = self._provider_message(message_id, message)
            settings = replace(SETTINGS, sender_allowlist=("billing@orange.example.test",))
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
                    with self.assertLogs("hestia.budget.gmail_message", level="WARNING") as logs:
                        outcome = service.sync(OWNER)
                    self.assertEqual((outcome["imported_count"], outcome["skipped_count"]), (0, 3))
                finally:
                    sidecar.terminate()
            self.assertEqual(store.status(OWNER)["parse_failed_reasons"],
                             {"charset_error": 1, "worker_rejected": 2})
            self.assertEqual(sorted(logs.output), [
                "WARNING:hestia.budget.gmail_message:gmail parse_failed reason=charset_error",
                "WARNING:hestia.budget.gmail_message:gmail parse_failed reason=worker_rejected",
                "WARNING:hestia.budget.gmail_message:gmail parse_failed reason=worker_rejected",
            ])

    @staticmethod
    def _email(sender, text, *, subtype="plain"):
        domain = sender.partition("@")[2]
        message = EmailMessage()
        message["From"] = sender
        message["Authentication-Results"] = f"mx.google.com; dkim=pass header.d={domain}; dmarc=pass header.from={domain}"
        message.set_content(text, subtype=subtype)
        return message

    @staticmethod
    def _provider_message(message_id, message, *, internal_date="1795566945123"):
        payload = message.as_bytes()
        metadata = {"id": message_id, "sizeEstimate": len(payload), "internalDate": internal_date,
                    "payload": {"headers": [{"name": name, "value": value} for name, value in message.raw_items()
                                            if name in {"From", "Sender", "Authentication-Results", "DKIM-Signature"}]}}
        raw = {"id": message_id, "raw": base64.urlsafe_b64encode(payload).decode().rstrip("=")}

        def select_response(params):
            if params["format"] == ["metadata"]:
                return response(metadata)
            assert params["format"] == ["raw"]
            return response(raw)

        return select_response
