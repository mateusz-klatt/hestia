"""Synthetic rendered PDF, trust and storage coverage for generic documents."""
from dataclasses import replace
from datetime import date
from email.message import EmailMessage
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import select

from hestia import db
from hestia.budget.gmail_document import extract_document
from hestia.budget.gmail_message import GmailMessageProcessor
from hestia.budget.gmail_payment import ImportedPayment
from hestia.budget.gmail_store import GmailStore, PendingMessage, PendingPayment, _document_number_key, tombstone_receipt
from hestia.budget.gmail_sync import _terms
from hestia.budget.gmail_trust import PaymentSender, normalize_document_senders, normalize_sender_rules
from tests.test_budget_gmail_message import _email, _metadata, _raw, MESSAGE_ID
from tests.test_budget_gmail_sync import FakeClock, OWNER, SETTINGS

FIXTURES = Path(__file__).parent / 'fixtures' / 'gmail' / 'documents'


def fixture(name):
    return (FIXTURES / (name + '.txt')).read_text(encoding='utf-8')


class DocumentFixtures(unittest.TestCase):
    def test_specified_documents(self):
        expected = {
            'polish_vat': ('F/2026/1', '123', 'PLN', '2026-11-10'),
            'ride': ('RIDE/1', '45.5', 'PLN', None),
            'utility': ('UTIL/1', '159.9', 'PLN', '2026-11-20'),
            'stripe_invoice': ('INV-42', '123', 'EUR', None),
            'stripe_receipt': ('INV-42', '123', 'EUR', None),
            'stripe_usd': ('INV-43', '25', 'USD', '2026-11-20'),
            'cloud': ('CLOUD-1', '17', 'USD', None),
            'office': ('OFFICE-1', '55', 'EUR', None),
        }
        for name, wanted in expected.items():
            with self.subTest(name=name):
                doc = extract_document(fixture(name), merchant='Vendor')
                self.assertEqual((doc.invoice_number, doc.amount, doc.currency, doc.due_date), wanted)
                self.assertEqual(doc.issue_date, '2026-11-03')
        doc = extract_document(fixture('polish_vat'), merchant='Vendor')
        self.assertEqual(doc.nip, '8567346215')
        self.assertTrue(doc.paid)
        self.assertTrue(extract_document(fixture('ride'), merchant='Vendor').paid)
        self.assertEqual(extract_document(fixture('stripe_receipt'), merchant='Vendor').receipt_number, 'REC-42')
        for name in ('statement', 'medical', 'newsletter', 'credit_note', 'conflict'):
            self.assertIsNone(extract_document(fixture(name), merchant='Vendor'))

    def test_document_sender_validation_and_query(self):
        entry = {'sender': '@Shop.example.test', 'merchant': 'Shop'}
        docs = normalize_document_senders([entry])
        self.assertEqual(docs, (PaymentSender('@shop.example.test', 'Shop'),))
        self.assertEqual(normalize_sender_rules([], [], [], document_senders=docs), ((), (), ()))
        self.assertIn('from:"shop.example.test"', _terms(replace(SETTINGS, document_senders=docs)))
        self.assertEqual(len(normalize_document_senders([
            {'sender': f'person{i}@shop.example.test', 'merchant': 'Shop'} for i in range(200)])), 200)
        for bad in (None, {}, [None], [entry, entry], [entry | {'gateway': True}],
                    [entry | {'sender': '@gmail.com'}], [entry | {'merchant': ''}],
                    [{'sender': f'person{i}@shop.example.test', 'merchant': 'Shop'} for i in range(201)]):
            with self.subTest(bad=str(bad)[:80]), self.assertRaises(ValueError):
                normalize_document_senders(bad)
        normalize_sender_rules(['@shop.example.test'], [], [], document_senders=docs)
        normalize_sender_rules([], [], [], payment_senders=docs, document_senders=docs)


class DocumentPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(db.reset_engine_cache)
        _, self.sessions = db.init_db(Path(self.tmp.name) / 'documents.db')
        with self.sessions.begin() as session:
            session.add(db.User(username=OWNER, password_hash='synthetic', role='admin'))
            session.add(db.AppMeta(key='users_authoritative', value='1'))
        self.clock = FakeClock()
        self.store = GmailStore(self.sessions, clock=self.clock.wall)
        self.store.transition(OWNER, 1, 'active')
        self.settings = SimpleNamespace(sender_allowlist=(), spouse_forward_senders=('spouse@example.test',),
            owner_mailbox='owner@example.test', hmac_secret=b'synthetic-document-hmac-key-012345',
            payment_senders=(PaymentSender('@stripe.example.test', 'Gateway', gateway=True, provider='stripe'),),
            document_senders=(PaymentSender('@stripe.example.test', 'Przykład Studio'),))
        self.worker = Mock()
        self.worker.parse.side_effect = lambda kind, payload: payload.decode('utf-8')
        self.processor = GmailMessageProcessor(self.settings, self.worker)
        self.counter = 0

    def message(self, texts, body='Amount paid: 123 EUR', sender='billing@stripe.example.test'):
        msg = _email(sender, body, rfc_id=None)
        msg['Subject'] = 'Receipt from Example Vendor'
        for text in texts:
            msg.add_attachment(text.encode(), maintype='application', subtype='pdf', filename='invoice.pdf')
        return msg

    def process(self, msg):
        self.counter += 1
        run = Mock()
        run.message.side_effect = [_metadata(msg), _raw(msg)]
        return self.processor.process(run, MESSAGE_ID, f'{self.counter:064x}')

    def commit(self, item):
        return self.store.commit(OWNER, 1, {}, (item,))

    def test_gateway_sender_document_takes_precedence_and_falls_back(self):
        doc = self.process(self.message([fixture('stripe_invoice')]))
        self.assertEqual((doc.outcome, doc.invoice.document_kind, doc.payment), ('imported', 'invoice', None))
        self.assertEqual(self.commit(doc), (1, 0, True))
        with self.sessions() as session:
            rows = session.scalars(select(db.BudgetReceipt)).all()
            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0].gateway)
        fallback = self.process(self.message(['newsletter with prices']))
        self.assertEqual(fallback.outcome, 'imported')
        self.assertIsNotNone(fallback.payment)
        self.assertIsNone(fallback.invoice)
        self.assertEqual(self.commit(fallback), (1, 0, True))

    def test_invoice_receipt_both_orders_and_multi_pdf(self):
        for i, order in enumerate((('stripe_invoice', 'stripe_receipt'), ('stripe_receipt', 'stripe_invoice'))):
            # Use a separate seller so both orderings exercise insertion.
            self.settings.document_senders = (PaymentSender('@stripe.example.test', f'Vendor {i}'),)
            first = fixture(order[0]).replace('Przykład Studio', f'Vendor {i}').replace('INV-42', f'INV-{i}-42')
            second = fixture(order[1]).replace('Przykład Studio', f'Vendor {i}').replace('INV-42', f'INV-{i}-42')
            self.assertEqual(self.commit(self.process(self.message([first]))), (1, 0, True))
            self.assertEqual(self.commit(self.process(self.message([second]))), (0, 1, True))
        self.settings.document_senders = (PaymentSender('@stripe.example.test', 'Przykład Studio'),)
        combined = self.process(self.message([fixture('stripe_invoice'), fixture('stripe_receipt')]))
        self.assertTrue(combined.document_provenance['paid'])
        self.assertIsNotNone(combined.document_provenance['receipt_number_hmac'])
        conflict = self.process(self.message([fixture('stripe_invoice'), fixture('stripe_usd')]))
        self.assertEqual((conflict.outcome, conflict.parse_failed_reason), ('parse_failed', 'multiple_documents'))
        with self.sessions() as session:
            receipts = session.scalars(select(db.BudgetReceipt)).all()
            self.assertEqual(len(receipts), 2)
            for receipt in receipts:
                provenance = json.loads(session.get(db.AppMeta, _document_number_key(receipt.id)).value)
                self.assertTrue(provenance['paid'])
                self.assertIsNotNone(provenance['receipt_number_hmac'])
                self.assertNotIn('INV-42', json.dumps(provenance))

    def test_reverse_evidence_both_orders_and_provenance_cleanup(self):
        for first_gateway in (True, False):
            total = '123' if first_gateway else '223'
            fields = ImportedPayment('Przykład Studio', 'Synthetic payment', str(int(total) + 2),
                                     'EUR', (), fee_amount='2')
            payment = PendingPayment(fields, date(2026, 11, 3), 'a' * 64, gateway=True,
                                     gateway_name='Synthetic Gateway', status='paid', gateway_ref_hmac='b' * 64)
            with self.subTest(first_gateway=first_gateway):
                document = self.process(self.message([fixture('stripe_invoice').replace('INV-42',
                                                  'INV-' + str(self.counter + 1)).replace('€123.00', '€' + total + '.00')]))
                gateway = PendingMessage(f'{self.counter + 100:064x}', self.clock.now, 'direct_allowlist', 'imported',
                                         payment=replace(payment, gateway_ref_hmac=str(self.counter + 1) * 64))
                order = (gateway, document) if first_gateway else (document, gateway)
                for item in order:
                    self.assertEqual(self.commit(item), (1, 0, True))
                with self.sessions.begin() as session:
                    doc = session.get(db.BudgetGmailMessage, (OWNER, document.gmail_id_hmac))
                    pay = session.get(db.BudgetGmailMessage, (OWNER, gateway.gmail_id_hmac))
                    self.assertEqual(session.get(db.BudgetReceipt, pay.receipt_id).evidence_for_receipt_id, doc.receipt_id)
                    doc_id = doc.receipt_id
                    tombstone_receipt(session, OWNER, doc_id)
                    self.assertIsNone(session.get(db.AppMeta, _document_number_key(doc_id)))

    def test_forwarded_document_and_untrusted_sender(self):
        inner = self.message([fixture('stripe_invoice')])
        outer = _email('spouse@example.test', 'forwarded', rfc_id=None)
        outer.add_attachment(inner)
        result = self.process(outer)
        self.assertEqual((result.trust_path, result.outcome, result.source_kind),
                         ('spouse_forward', 'imported', 'attachment'))
        before = self.worker.parse.call_count
        unknown = self.message([fixture('stripe_invoice')], sender='unknown@other.example.test')
        self.assertEqual(self.process(unknown).outcome, 'skipped_untrusted_sender')
        self.assertEqual(self.worker.parse.call_count, before)

    def test_pdf_failures_date_window_and_template_precedence(self):
        from hestia.budget import gmail_message
        from hestia.budget.gmail_worker import PDFUnsupported
        from tests.test_budget_gmail_message import INVOICE
        self.settings.payment_senders = ()
        self.assertEqual(self.process(self.message([''])).outcome, 'skipped_non_receipt')
        with patch.object(gmail_message, '_document_payload', side_effect=gmail_message._PartOversized()):
            self.assertEqual(self.process(self.message(['x'])).outcome, 'skipped_oversized')
        with patch.object(self.worker, 'parse', side_effect=PDFUnsupported()):
            result = self.process(self.message(['x']))
            self.assertEqual((result.outcome, result.parse_failed_reason), ('parse_failed', 'pdf_unsupported'))
        with patch.object(self.worker, 'parse', return_value=''):
            result = self.process(self.message(['x']))
            self.assertEqual((result.outcome, result.parse_failed_reason), ('parse_failed', 'pdf_no_text'))
        self.assertEqual(self.process(self.message([fixture('stripe_invoice').replace('November 3, 2026',
                                             'November 3, 2020')])).outcome, 'skipped_non_receipt')
        # A recognized template from the wrong sender must not fall through to the generic extractor.
        self.assertEqual(self.process(self.message([INVOICE])).outcome, 'skipped_non_receipt')
        self.settings.document_senders = (PaymentSender('@respectenergy.pl', 'Vendor'),)
        template = self.process(self.message([INVOICE], sender='billing@respectenergy.pl'))
        self.assertEqual((template.outcome, template.invoice.issuer), ('imported', 'Respect Energy'))
        # A malformed or unrepresentable message timestamp fails closed.
        pending = PendingMessage('f' * 64, 10**100, 'direct_allowlist', 'skipped_non_receipt')
        self.assertEqual(self.processor._document(self.message([INVOICE], sender='billing@respectenergy.pl'),
                                                 pending, self.settings.document_senders[0]).outcome,
                         'skipped_non_receipt')
        document = extract_document(fixture('stripe_invoice'), merchant='Vendor')
        with patch.object(gmail_message, 'extract_invoice', return_value=None), \
             patch.object(gmail_message, 'extract_document', return_value=replace(document, issue_date='bad')):
            self.assertEqual(self.process(self.message(['bad'], sender='billing@respectenergy.pl')).outcome,
                             'skipped_non_receipt')

    def test_cap_rfc_and_forward_fallback(self):
        doc = self.process(self.message([fixture('stripe_invoice')]))
        with patch.object(self.store, '_pending_count', return_value=200):
            self.assertEqual(self.commit(doc), (0, 0, False))
        self.assertEqual(self.commit(doc), (1, 0, True))
        inner = self.message(['unrelated newsletter'])
        outer = _email('spouse@example.test', 'forwarded', rfc_id=None)
        outer.add_attachment(inner)
        result = self.process(outer)
        self.assertEqual(result.outcome, 'imported')
        self.assertIsNotNone(result.payment)
        # Same RFC Message-ID wins before the document-number check.
        first = self.message([fixture('stripe_invoice')])
        first['Message-ID'] = '<synthetic-one@example.test>'
        second = self.message([fixture('stripe_usd')])
        second['Message-ID'] = '<synthetic-one@example.test>'
        self.assertEqual(self.commit(self.process(first)), (0, 1, True))
        # Use a fresh RFC id and a different number to establish the dedupe row.
        first = self.message([fixture('stripe_invoice').replace('INV-42', 'INV-NEW')])
        first['Message-ID'] = '<synthetic-two@example.test>'
        first_result = self.process(first)
        self.assertEqual(self.commit(first_result), (1, 0, True))
        second.replace_header('Message-ID', '<synthetic-two@example.test>')
        self.assertEqual(self.commit(self.process(second)), (0, 1, True))

    def test_actual_p24_paid_email_attaches_in_both_orders(self):
        body = (FIXTURES.parent / 'payment' / 'v1f' / 'p24_paid.txt').read_text(encoding='utf-8')
        self.settings.payment_senders += (PaymentSender('@p24.example.test', 'P24', gateway=True, provider='p24'),)
        pdf = ('Faktura VAT nr SYNTH/120\nSprzedawca: SKLEP PRZYKŁADOWY SP. Z O.O.\n'
               'Data wystawienia: 03.11.2026\nTermin płatności: 25.11.2026\nKwota do zapłaty 120,00 PLN')
        for index, gateway_first in enumerate((True, False)):
            owner = f'owner-{index}'
            with self.sessions.begin() as session:
                session.add(db.User(username=owner, password_hash='synthetic', role='admin'))
            self.store.transition(owner, 1, 'active')
            mail = _email('receipts@p24.example.test', body, rfc_id=None)
            mail['Subject'] = 'Potwierdzenie (P24-ABC-123-DEF)'
            gateway = self.process(mail)
            document = self.process(self.message([pdf]))
            self.assertEqual((gateway.payment.status, gateway.payment.fields.total_amount,
                              gateway.payment.fields.fee_amount), ('paid', '121.5', '1.5'))
            for item in ((gateway, document) if gateway_first else (document, gateway)):
                self.assertEqual(self.store.commit(owner, 1, {}, (item,)), (1, 0, True))
            with self.sessions() as session:
                gateway_row = session.get(db.BudgetGmailMessage, (owner, gateway.gmail_id_hmac))
                document_row = session.get(db.BudgetGmailMessage, (owner, document.gmail_id_hmac))
                self.assertEqual(session.get(db.BudgetReceipt, gateway_row.receipt_id).evidence_for_receipt_id,
                                 document_row.receipt_id)

    def test_no_text_reason_for_issuer_overlap_and_forward(self):
        self.settings.payment_senders = ()
        self.settings.sender_allowlist = ('@stripe.example.test',)
        message = self.message(['  '], body='unrelated')
        result = self.process(message)
        self.assertEqual((result.outcome, result.parse_failed_reason), ('parse_failed', 'pdf_no_text'))
        self.settings.sender_allowlist = ()
        outer = _email('spouse@example.test', 'forwarded', rfc_id=None)
        outer.add_attachment(message)
        result = self.process(outer)
        self.assertEqual((result.outcome, result.parse_failed_reason), ('parse_failed', 'pdf_no_text'))

    def test_issuer_body_fallback_for_overlapping_sender(self):
        from tests.test_budget_gmail_message import INVOICE
        self.settings.payment_senders = ()
        self.settings.sender_allowlist = ('@respectenergy.pl',)
        self.settings.document_senders = (PaymentSender('@respectenergy.pl', 'Vendor'),)
        message = self.message(['newsletter'], body=INVOICE, sender='billing@respectenergy.pl')
        result = self.process(message)
        self.assertEqual((result.outcome, result.invoice.issuer, result.source_kind),
                         ('imported', 'Respect Energy', 'email'))

    def test_issue_date_window_boundaries(self):
        from datetime import timedelta
        self.settings.payment_senders = ()
        mail_day = date(2026, 11, 25)
        for offset, outcome in ((-401, 'skipped_non_receipt'), (-400, 'imported'),
                                (7, 'imported'), (8, 'skipped_non_receipt')):
            with self.subTest(offset=offset):
                issue = (mail_day + timedelta(days=offset)).isoformat()
                text = fixture('stripe_invoice').replace('November 3, 2026', issue)
                self.assertEqual(self.process(self.message([text])).outcome, outcome)

    def test_r5_nested_forward_requires_manual_and_does_not_parse_pdf(self):
        original = self.message([fixture('stripe_invoice')])
        relay = _email('relay@untrusted.example.test', 'forward', rfc_id=None)
        relay.add_attachment(original)
        outer = _email('spouse@example.test', 'forward', rfc_id=None)
        outer.add_attachment(relay)
        result = self.process(outer)
        self.assertEqual((result.outcome, result.forwarded_manually, result.sender_domain),
                         ('imported', True, 'untrusted.example.test'))
        self.assertIsNone(result.invoice)
        self.assertFalse(any(call.args[0] == 'pdf' for call in self.worker.parse.call_args_list))

    def test_r5_forwarded_template_requires_exact_sender_and_provider_number(self):
        from tests.test_budget_gmail_message import INVOICE
        sender = 'billing@respectenergy.pl'
        self.settings.payment_senders = ()
        self.settings.document_senders = (PaymentSender('@respectenergy.pl', 'Vendor'),)
        self.settings.sender_allowlist = (sender,)
        for actual_sender, text, expected in (
            (sender, INVOICE, False),
            ('other@respectenergy.pl', INVOICE, True),
            (sender, INVOICE.replace('RE/2026/00042', 'FV/2026/11/42'), True),
        ):
            with self.subTest(sender=actual_sender, text=text):
                original = self.message([text], sender=actual_sender)
                outer = _email('spouse@example.test', 'forward', rfc_id=None)
                outer.add_attachment(original)
                result = self.process(outer)
                self.assertEqual((result.outcome, result.forwarded_manually), ('imported', expected))
                if expected:
                    self.assertIsNone(result.invoice)
                else:
                    self.assertEqual(result.invoice.issuer, 'Respect Energy')

    def test_r5_outer_inline_pdf_requires_trusted_original_sender(self):
        for sender, expected in (('billing@stripe.example.test', False), ('unknown@other.example.test', True)):
            with self.subTest(sender=sender):
                body = f'Forwarded message\nFrom: {sender}\nSubject: Receipt\n\nSee attached.'
                outer = _email('spouse@example.test', body, rfc_id=None)
                outer.add_attachment(fixture('stripe_invoice').encode(), maintype='application', subtype='pdf',
                                     filename='invoice.pdf')
                self.worker.reset_mock()
                result = self.process(outer)
                self.assertEqual((result.outcome, result.forwarded_manually), ('imported', expected))
                self.assertEqual(any(call.args[0] == 'pdf' for call in self.worker.parse.call_args_list), not expected)
                if not expected:
                    self.assertEqual(result.invoice.invoice_number, 'INV-42')

    def test_r6_same_number_pdf_consistency_and_invoice_fields(self):
        invoice = fixture('stripe_invoice').replace('Przykład Studio\n',
            'Sprzedawca: Przykład Studio\nNIP: 8567346215\n') + '\nDate due November 20, 2026\n'
        receipt = fixture('stripe_receipt')
        for pdfs in ((invoice, receipt), (receipt, invoice)):
            result = self.process(self.message(pdfs))
            self.assertEqual((result.outcome, result.invoice.document_kind, result.invoice.due_date, result.invoice.nip),
                             ('imported', 'invoice', '2026-11-20', '8567346215'))
            self.assertTrue(result.invoice.paid)
            self.assertEqual(result.invoice.receipt_number, 'REC-42')
        for altered in (receipt.replace('Receipt\n', 'Other Seller\nReceipt\n'),
                        receipt.replace('€123.00', '€124.00'), receipt.replace('€123.00', '$123.00')):
            for pdfs in ((invoice, altered), (altered, invoice)):
                with self.subTest(pdfs=pdfs[0][:30]):
                    result = self.process(self.message(pdfs))
                    self.assertEqual((result.outcome, result.parse_failed_reason),
                                     ('parse_failed', 'multiple_documents'))

    def test_r6_cross_message_invoice_enriches_unconfirmed_receipt(self):
        invoice = fixture('stripe_invoice').replace('Przykład Studio\n',
            'Sprzedawca: Przykład Studio\nNIP: 8567346215\n') + '\nDate due November 20, 2026\n'
        receipt = fixture('stripe_receipt')
        for index, texts in enumerate(((receipt, invoice), (invoice, receipt))):
            owner = f'order-{index}'
            with self.sessions.begin() as session:
                session.add(db.User(username=owner, password_hash='synthetic', role='admin'))
            self.store.transition(owner, 1, 'active')
            first, second = (self.process(self.message([text])) for text in texts)
            self.assertEqual(self.store.commit(owner, 1, {}, (first,)), (1, 0, True))
            self.assertEqual(self.store.commit(owner, 1, {}, (second,)), (0, 1, True))
            with self.sessions() as session:
                message = session.get(db.BudgetGmailMessage, (owner, first.gmail_id_hmac))
                row = session.get(db.BudgetReceipt, message.receipt_id)
                provenance = json.loads(session.get(db.AppMeta, _document_number_key(row.id)).value)
                self.assertEqual((row.document_kind, row.due_date, row.raw_nip),
                                 ('invoice', '2026-11-20', '8567346215'))
                self.assertTrue(provenance['paid'])
                self.assertIsNotNone(provenance['receipt_number_hmac'])

    def test_r6_cross_message_conflicts_do_not_merge(self):
        invoice = fixture('stripe_invoice')
        receipt = fixture('stripe_receipt')
        for index, altered in enumerate((receipt.replace('Receipt\n', 'Other Seller\nReceipt\n'),
                                        receipt.replace('€123.00', '€124.00'),
                                        receipt.replace('€123.00', '$123.00'))):
            for reverse in (False, True):
                owner = f'conflict-{index}-{int(reverse)}'
                with self.sessions.begin() as session:
                    session.add(db.User(username=owner, password_hash='synthetic', role='admin'))
                self.store.transition(owner, 1, 'active')
                texts = (altered, invoice) if reverse else (invoice, altered)
                items = [self.process(self.message([text])) for text in texts]
                self.assertEqual(self.store.commit(owner, 1, {}, (items[0],)), (1, 0, True))
                self.assertEqual(self.store.commit(owner, 1, {}, (items[1],)), (0, 1, True))
                with self.sessions() as session:
                    message = session.get(db.BudgetGmailMessage, (owner, items[1].gmail_id_hmac))
                    self.assertEqual(message.outcome, 'parse_failed')
                self.assertEqual(self.store.status(owner)['parse_failed_reasons'], {'multiple_documents': 1})

    def test_n2_empty_pdf_records_diagnostic_on_payment_fallback(self):
        self.worker.parse.side_effect = lambda kind, payload: '' if kind == 'pdf' else payload.decode()
        result = self.process(self.message(['empty scanned document']))
        self.assertEqual((result.outcome, result.diagnostic_reason), ('imported', 'pdf_no_text'))
        self.assertIsNotNone(result.payment)
        self.assertEqual(self.commit(result), (1, 0, True))
        self.assertEqual(self.store.status(OWNER)['parse_failed_reasons'], {'pdf_no_text': 1})

    def test_r5_forward_without_original_is_skipped(self):
        outer = _email('spouse@example.test', 'No forwarded message', rfc_id=None)
        self.assertEqual(self.process(outer).outcome, 'skipped_non_receipt')

    def test_n2_forwarded_payment_and_manual_fallback_keep_empty_pdf_diagnostic(self):
        self.worker.parse.side_effect = lambda kind, payload: '' if kind == 'pdf' else payload.decode()
        for body, manual in (('Amount paid: 123 EUR', False), ('No payment amount', True)):
            with self.subTest(manual=manual):
                inner = self.message(['synthetic empty PDF'], body=body)
                outer = _email('spouse@example.test', 'forward', rfc_id=None)
                outer.add_attachment(inner)
                result = self.process(outer)
                self.assertEqual((result.outcome, result.forwarded_manually, result.diagnostic_reason),
                                 ('imported', manual, 'pdf_no_text'))
                self.assertEqual(self.commit(result), (1, 0, True))
        self.assertEqual(self.store.status(OWNER)['parse_failed_reasons'], {'pdf_no_text': 2})

    def test_n2_empty_diagnostic_recorded_once_without_payment_sender(self):
        self.settings.payment_senders = ()
        self.worker.parse.side_effect = lambda kind, payload: '' if kind == 'pdf' else payload.decode()
        result = self.process(self.message(['empty PDF']))
        self.assertEqual((result.outcome, result.parse_failed_reason), ('parse_failed', 'pdf_no_text'))
        self.assertEqual(self.commit(result), (0, 1, True))
        self.assertEqual(self.store.status(OWNER)['parse_failed_reasons'], {'pdf_no_text': 1})

    def test_n2_empty_pdf_diagnostic_survives_another_pdf_failure(self):
        from hestia.budget.gmail_worker import PDFUnsupported
        def parse(kind, payload):
            if kind != 'pdf':
                return payload.decode()
            if payload == b'empty':
                return ''
            raise PDFUnsupported()
        self.worker.parse.side_effect = parse
        result = self.process(self.message(['empty', 'unsupported']))
        self.assertEqual((result.outcome, result.diagnostic_reason), ('imported', 'pdf_no_text'))
        self.assertEqual(self.commit(result), (1, 0, True))
        self.assertEqual(self.store.status(OWNER)['parse_failed_reasons'], {'pdf_no_text': 1})

    def test_r6_confirmed_receipt_is_not_overwritten_by_later_invoice(self):
        receipt = self.process(self.message([fixture('stripe_receipt')]))
        self.assertEqual(self.commit(receipt), (1, 0, True))
        with self.sessions.begin() as session:
            message = session.get(db.BudgetGmailMessage, (OWNER, receipt.gmail_id_hmac))
            row = session.get(db.BudgetReceipt, message.receipt_id)
            row.status, row.payment_method = 'confirmed', 'card'
        invoice = self.process(self.message([fixture('stripe_invoice') + '\nDate due November 20, 2026']))
        self.assertEqual(self.commit(invoice), (0, 1, True))
        with self.sessions() as session:
            message = session.get(db.BudgetGmailMessage, (OWNER, receipt.gmail_id_hmac))
            row = session.get(db.BudgetReceipt, message.receipt_id)
            self.assertEqual((row.status, row.document_kind, row.due_date), ('confirmed', 'receipt', None))

    def test_g2_receipt_only_identifiers_are_hmac_only_at_rest(self):
        text = fixture('stripe_numeric_receipt').replace('Invoice number ABCD1234-0007\n', '')
        for identifier in ('123456789012', '123456789013'):
            item = self.process(self.message([text.replace('123456789012', identifier)]))
            self.assertEqual((item.outcome, item.invoice.document_kind), ('imported', 'receipt'))
            self.assertNotIn(identifier, repr(item.invoice))
            self.assertEqual(self.commit(item), (1, 0, True))
        with self.sessions() as session:
            rows = session.scalars(select(db.BudgetReceipt)).all()
            self.assertEqual(len(rows), 2)
            for row in rows:
                self.assertIsNone(row.invoice_number)
                provenance = session.get(db.AppMeta, _document_number_key(row.id)).value
                self.assertNotIn('123456789012', provenance)
                self.assertNotIn('123456789013', provenance)
                self.assertTrue(json.loads(provenance)['receipt_number_hmac'])

    def test_r5_nested_inline_forward_is_manual_without_reading_pdf(self):
        single = _email('spouse@example.test', 'Forwarded message\nFrom: relay@untrusted.example.test\n\nBody',
                        rfc_id=None)
        self.assertIsNone(self.processor._nested_inline_original(single))
        body = ('Forwarded message\nFrom: relay@untrusted.example.test\n\n'
                'Forwarded message\nFrom: billing@stripe.example.test\n\nInvoice attached')
        outer = _email('spouse@example.test', body, rfc_id=None)
        outer.add_attachment(fixture('stripe_invoice').encode(), maintype='application', subtype='pdf',
                             filename='invoice.pdf')
        result = self.process(outer)
        self.assertEqual((result.outcome, result.forwarded_manually, result.sender_domain),
                         ('imported', True, 'untrusted.example.test'))
        self.assertFalse(any(call.args[0] == 'pdf' for call in self.worker.parse.call_args_list))
        invalid = _email('spouse@example.test', body.replace('From: billing@stripe.example.test', 'From: invalid'),
                         rfc_id=None)
        self.assertEqual(self.process(invalid).outcome, 'skipped_non_receipt')

    def test_r5_nested_html_is_manual_and_bad_render_is_ignored(self):
        from hestia.budget.gmail_worker import WorkerRejected
        body = ('Forwarded message\nFrom: relay@untrusted.example.test\n\n'
                'Forwarded message\nFrom: billing@stripe.example.test\n\nInvoice attached')
        outer = _email('spouse@example.test', '<html>synthetic</html>', subtype='html', rfc_id=None)
        self.worker.parse.return_value = body
        self.worker.parse.side_effect = None
        result = self.process(outer)
        self.assertEqual((result.outcome, result.forwarded_manually), ('imported', True))
        self.worker.parse.side_effect = WorkerRejected()
        self.assertIsNone(self.processor._nested_inline_original(outer))

    def test_extra_undated_issuer_template_is_imported_without_second_parse(self):
        from hestia.budget import gmail_message
        from hestia.budget.gmail_invoice import ImportedInvoice
        invoice = ImportedInvoice('Respect Energy', None, 'RE/2026/00042', '123.45',
                                  'PLN', None, '2026-12-05')
        self.settings.payment_senders = ()
        self.settings.sender_allowlist = ('@respectenergy.pl',)
        self.settings.document_senders = (PaymentSender('@respectenergy.pl', 'Vendor'),)
        message = self.message(['synthetic PDF'], sender='billing@respectenergy.pl')
        with patch.object(gmail_message, 'extract_invoice', return_value=invoice) as extract, \
             patch.object(self.processor, '_invoice', wraps=self.processor._invoice) as invoice_path:
            result = self.process(message)
        self.assertEqual((result.outcome, result.source_kind, result.invoice), ('imported', 'attachment', invoice))
        self.assertIsNone(result.document_provenance)
        extract.assert_called_once()
        self.worker.parse.assert_called_once()
        invoice_path.assert_not_called()
        self.assertEqual(self.commit(result), (1, 0, True))

    def test_extra_undated_templates_obey_trust_and_multi_pdf_checks(self):
        from hestia.budget import gmail_message
        from hestia.budget.gmail_invoice import ImportedInvoice
        invoice = ImportedInvoice('Respect Energy', None, 'RE/2026/00042', '123.45',
                                  'PLN', None, '2026-12-05')
        self.settings.payment_senders = ()
        self.settings.document_senders = (PaymentSender('@respectenergy.pl', 'Vendor'),)
        message = self.message(['one'], sender='billing@respectenergy.pl')
        with patch.object(gmail_message, 'extract_invoice', return_value=invoice):
            self.assertEqual(self.process(message).outcome, 'skipped_non_receipt')
        self.settings.sender_allowlist = ('@respectenergy.pl',)
        message = self.message(['one', 'two'], sender='billing@respectenergy.pl')
        with patch.object(gmail_message, 'extract_invoice', side_effect=[invoice, replace(invoice, invoice_number='RE/2026/00043')]):
            result = self.process(message)
        self.assertEqual((result.outcome, result.parse_failed_reason), ('parse_failed', 'multiple_documents'))
        dated = ('Faktura nr RE/2026/00042\nSprzedawca: Respect Energy\nData faktury 21.11.2026\n'
                 'Kwota brutto 123,45 PLN')
        message = self.message(['one', dated], sender='billing@respectenergy.pl')
        with patch.object(gmail_message, 'extract_invoice', side_effect=[invoice, None]):
            result = self.process(message)
        self.assertEqual((result.outcome, result.invoice.issue_date), ('imported', '2026-11-21'))
        self.assertIsNotNone(result.document_provenance)
