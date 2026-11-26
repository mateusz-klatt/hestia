import unittest
from pathlib import Path

from hestia.budget.gmail_document import extract_document


_FIXTURES = Path(__file__).parent / "fixtures" / "gmail" / "documents"


def fixture(name):
    return (_FIXTURES / f"{name}.txt").read_text(encoding="utf-8")


def fake_nip(prefix):
    check = sum(int(digit) * weight for digit, weight in zip(prefix, (6, 5, 7, 2, 3, 4, 5, 6, 7))) % 11
    assert check < 10
    return prefix + str(check)


class DocumentExtractionTest(unittest.TestCase):
    def test_invoice_and_receipt_fields(self):
        seller_nip = fake_nip("856734621")
        buyer_nip = fake_nip("123456321")
        text = f"""Faktura VAT nr F/1
Sprzedawca: Sklep Testowy
NIP: {seller_nip}
Nabywca: Klient Testowy
NIP: {buyer_nip}
Data wystawienia: 03.11.2026
Termin płatności: 10.11.2026
Wartość netto 10 zł
Suma: 12,30 zł
Zapłacono: 12,30 zł
"""
        document = extract_document(text, merchant="Sklep")
        self.assertIsNotNone(document)
        self.assertEqual((document.invoice_number, document.amount, document.currency), ("F/1", "12.3", "PLN"))
        self.assertEqual((document.issuer, document.nip), ("Sklep Testowy", seller_nip))
        self.assertEqual((document.issue_date, document.due_date, document.document_kind, document.paid),
                         ("2026-11-03", "2026-11-10", "invoice", True))

    def test_priority_and_dates(self):
        samples = [
            ("Invoice number: X1\nDate of issue: November 3, 2026\nSubtotal €8.00\nTotal €10.00", "10", "EUR"),
            ("Invoice number X2\nDate of issue 2026-11-03\nAmount due $11.00 USD\nTotal $12.00", "11", "USD"),
            ("Invoice\nBilling Number X3\nDocument Date 03/11/2026\nTotal Amount EUR 13.00", "13", "EUR"),
        ]
        for text, amount, currency in samples:
            with self.subTest(text=text):
                document = extract_document(text, merchant="Vendor")
                self.assertEqual((document.amount, document.currency, document.issue_date),
                                 (amount, currency, "2026-11-03"))

    def test_rejects(self):
        base = "Invoice number X1\nDate of issue 2026-11-03\nTotal €10.00"
        for text in (base + "\nStatement of account", base + "\nwynik badania", base + "\nnewsletter",
                     base + "\ncredit note", base + "\nTotal €11.00", "Prices €10.00", "Invoice"):
            with self.subTest(text=text):
                self.assertIsNone(extract_document(text, merchant="Vendor"))

    def test_bounded_and_ambiguous_values_fail_closed(self):
        from unittest.mock import patch
        from hestia.budget.receipt_parser import MAX_TEXT_BYTES, ReceiptError
        base = 'Invoice number X1\nDate of issue 2026-11-03\nTotal €10.00'
        for text in (None, 'x' * (MAX_TEXT_BYTES + 1), 'ż' * (MAX_TEXT_BYTES // 2 + 1), '\ud800',
                     base.replace('X1', 'x' * 65), base.replace('X1', '1234567890123456'),
                     base + '\nInvoice number X2', base + '\nReceipt number person@example.test',
                     base.replace('2026-11-03', 'invalid'), base + '\nDate of issue 2026-11-04',
                     base.replace('Date of issue 2026-11-03\n', ''),
                     base.replace('Total €10.00', 'Total -€10.00'),
                     base.replace('Total €10.00', 'Total nonsense'),
                     base.replace('Total €10.00', 'Subtotal €10.00'),
                     base + '\nSprzedawca: sensitive@example.test'):
            with self.subTest(text=str(text)[:80]):
                self.assertIsNone(extract_document(text, merchant='Vendor'))
        with patch('hestia.budget.gmail_document._normalized_invoice_lines', side_effect=ReceiptError('invalid')):
            self.assertIsNone(extract_document(base, merchant='Vendor'))

    def test_wrapped_labels_nip_scope_and_receipt(self):
        text = ('Receipt number:\n\nRC-42\nDate of issue:\n03/11/2026\n'
                'Amount paid:\n€10.00\nNIP: 8567346215\nBill to: Customer\nNIP: 1234563218')
        doc = extract_document(text, merchant='Vendor')
        self.assertEqual((doc.invoice_number, doc.nip, doc.document_kind, doc.paid), ('RC-42', None, 'receipt', True))
        self.assertEqual(doc.issuer, 'Vendor')
        seller = text + '\nFrom: Vendor\nNIP: 8567346215\nNIP: 1234563218'
        self.assertIsNone(extract_document(seller, merchant='Vendor').nip)
        self.assertIsNone(extract_document(text, merchant=''))

    def test_seller_bounds_and_fallbacks(self):
        base = 'Invoice number X1\nDate of issue 2026-11-03\nTotal €10.00'
        self.assertIsNone(extract_document(base + '\nSprzedawca: One\nSprzedawca: Two', merchant='Vendor'))
        self.assertIsNone(extract_document(base + '\nSprzedawca: ' + 'A' * 129, merchant='Vendor'))
        polish = 'Faktura nr X1\nData faktury 03.11.2026\nKwota brutto 10 zł'
        self.assertEqual(extract_document(polish, merchant='Vendor').issuer, 'Vendor')
        self.assertEqual(extract_document('Example Studio\n' + base + '\nTotal excluding tax €8.00',
                                          merchant='Vendor').issuer, 'Example Studio')
        date_first = 'Date of issue 2026-11-03\nInvoice number X1\nTotal €10.00'
        self.assertEqual(extract_document(date_first, merchant='Vendor').issuer, 'Vendor')
        self.assertEqual(extract_document(base.replace('X1', 'F/Ż 42'), merchant='Vendor').invoice_number, 'F/Ż 42')
        self.assertIsNone(extract_document(base.replace('X1', 'Date due'), merchant='Vendor'))

    def test_paragon_heading_classifies_receipt_without_inventing_number(self):
        text = 'Paragon\nFaktura nr X1\nData faktury 03.11.2026\nKwota do zapłaty 10 zł'
        document = extract_document(text, merchant='Vendor')
        self.assertEqual((document.document_kind, document.invoice_number, document.receipt_number),
                         ('invoice', 'X1', None))
        self.assertEqual(extract_document(text.replace('Paragon\n', 'Paragon nr P1\n'),
                                          merchant='Vendor').receipt_number, 'P1')

    def test_g1_grand_total_precedes_page_totals(self):
        text = fixture('grocery_grand_total')
        doc = extract_document(text, merchant='Vendor')
        self.assertEqual((doc.invoice_number, doc.amount, doc.currency, doc.nip),
                         ('A123456789', '16.8', 'PLN', fake_nip('856734621')))
        self.assertEqual(extract_document(text.replace('Do zapłaty razem:', 'Razem do zapłaty:'),
                                          merchant='Vendor').amount, '16.8')
        self.assertIsNone(extract_document(text.replace('Do zapłaty razem:', 'Do zapłaty:'), merchant='Vendor'))
        self.assertIsNone(extract_document(text + '\nRazem do zapłaty: 17 PLN', merchant='Vendor'))
        self.assertIsNone(extract_document(text + '\nFaktura VAT nr A123456780', merchant='Vendor'))

    def test_g2_receipt_number_and_paid_date(self):
        text = fixture('stripe_numeric_receipt')
        doc = extract_document(text, merchant='Vendor Test')
        self.assertEqual((doc.invoice_number, doc.receipt_number, doc.issue_date, doc.amount,
                          doc.currency, doc.document_kind, doc.paid),
                         ('ABCD1234-0007', '123456789012', '2026-08-03', '11.07', 'USD', 'receipt', True))
        self.assertEqual(extract_document(text.replace('Date paid', 'Date issued'),
                                          merchant='Vendor Test').issue_date, '2026-08-03')
        for invalid in ('abc123', 'AB!', '123', 'A' * 65):
            with self.subTest(invalid=invalid):
                self.assertIsNone(extract_document(text.replace('Receipt number 123456789012',
                                                                'Receipt number ' + invalid), merchant='Vendor Test'))
        receipt_only = extract_document(text.replace('Invoice number ABCD1234-0007\n', ''),
                                        merchant='Vendor Test')
        self.assertEqual(receipt_only.receipt_number, '123456789012')
        self.assertIsNone(extract_document(text.replace('ABCD1234-0007', '123456789012'), merchant='Vendor Test'))
        self.assertEqual(extract_document(text.replace('Credited total $2.00', 'Credited total $999.00'),
                                          merchant='Vendor Test').amount, '11.07')

    def test_g3_cloud_office_headers_and_label_currency(self):
        text = fixture('cloud_office_pl')
        self.assertGreater(text.index('Do zapłaty w EUR'), 3000)
        doc = extract_document(text, merchant='Vendor')
        self.assertEqual((doc.invoice_number, doc.issue_date, doc.due_date, doc.amount, doc.currency, doc.nip),
                         ('1234567890', '2026-01-02', '2026-01-15', '12.3', 'EUR', fake_nip('856734621')))
        self.assertEqual(extract_document(text.replace('Do zapłaty w EUR 12,30',
                                                        'Do zapłaty w EUR\n12,30'), merchant='Vendor').amount, '12.3')
        summary_only = text.replace('Do zapłaty w EUR 12,30\n', '')
        self.assertEqual(extract_document(summary_only, merchant='Vendor').amount, '12.3')
        self.assertEqual(extract_document(summary_only.replace('Suma w EUR 12,30', 'Suma w EUR\n12,30'),
                                          merchant='Vendor').amount, '12.3')
        self.assertIsNone(extract_document(text.replace('Do zapłaty w EUR 12,30',
                                                        'Do zapłaty w EUR 12,30 USD'), merchant='Vendor'))
        self.assertIsNone(extract_document(text.replace('Data faktury\nIdentyfikator płatności\n1234567890\n02.01.2026',
                                                        'Data faktury\nIdentyfikator płatności'), merchant='Vendor'))

    def test_g4_settlement_and_two_column_seller(self):
        text = fixture('utility_settlement')
        doc = extract_document(text, merchant='Vendor')
        self.assertEqual((doc.invoice_number, doc.issue_date, doc.due_date, doc.issuer, doc.nip, doc.amount),
                         ('1234567890', '2026-01-02', '2026-01-15', 'Utility Test S.A.',
                          fake_nip('856734621'), '245.1'))
        no_seller_nip = text.replace('NIP: 8567346215\n', '')
        self.assertIsNone(extract_document(no_seller_nip, merchant='Vendor').nip)
        self.assertIsNone(extract_document(text.replace('15.01.2026 245', '35.01.2026 245'), merchant='Vendor'))
        self.assertIsNone(extract_document('Faktura nr X1\nData faktury 03.11.2026\n'
                                           'Kwota brutto 10 zł\nSprzedawca: Nabywca:', merchant='Vendor'))

    def test_g5_inline_seller_wins_and_is_trimmed(self):
        text = fixture('ride_inline_seller')
        doc = extract_document(text, merchant='Vendor')
        self.assertEqual((doc.issuer, doc.nip, doc.amount, doc.currency, doc.paid),
                         ('Taxi Test Sp. z o.o.', fake_nip('856734621'), '45.5', 'PLN', True))
        postcode = text.replace(' ul. Testowa 1, 00-001 Warszawa', ', 00-001 Warszawa')
        self.assertEqual(extract_document(postcode, merchant='Vendor').issuer, 'Taxi Test Sp. z o.o.')
        buyer_inline = text.replace('NIP: 856-734-62-15', 'NIP: 856-734-62-15 Nabywca: Klient Test')
        self.assertEqual(extract_document(buyer_inline, merchant='Vendor').nip, fake_nip('856734621'))

    def test_g6_ticket_and_booking_stay_out_of_scope(self):
        text = fixture('ticket_booking')
        for marker in ('Ticket booking', 'Bilet', 'Rezerwacja', 'Rezerwacji'):
            with self.subTest(marker=marker):
                self.assertIsNone(extract_document(text.replace('Ticket booking', marker), merchant='Vendor'))
        invoice = 'Invoice number X1\nDate issued 2026-11-03\nTotal €10\nSupport ticket information'
        self.assertEqual(extract_document(invoice, merchant='Vendor').invoice_number, 'X1')

    def test_r1_due_zero_and_amount_bounds(self):
        base = 'Invoice number X1\nDate issued 2026-11-03\n'
        for due in ('Amount due €0.00', 'Do zapłaty 0,00 EUR', 'Do zapłaty razem 0,00 EUR'):
            with self.subTest(due=due):
                text = base + due + '\nTotal €12.00'
                doc = extract_document(text, merchant='Vendor')
                self.assertEqual((doc.amount, doc.paid, doc.document_kind), ('12', True, 'invoice'))
        paid_only = extract_document(base + 'Amount due €0.00\nAmount paid €12.00', merchant='Vendor')
        self.assertEqual((paid_only.amount, paid_only.paid, paid_only.document_kind), ('12', True, 'invoice'))
        self.assertIsNone(extract_document(base + 'Amount due €0.00\nAmount due €1.00\nTotal €1.00',
                                           merchant='Vendor'))
        text = base + 'Do zapłaty razem 0 EUR\nDo zapłaty 1 EUR\nDo zapłaty 2 EUR\nTotal 3 EUR'
        self.assertEqual(extract_document(text, merchant='Vendor').amount, '3')
        for amount in ('0', '1000000.01', '9' * 90, '9' * 300):
            with self.subTest(amount=amount):
                self.assertIsNone(extract_document(base + 'Total €' + amount, merchant='Vendor'))
        self.assertEqual(extract_document(base + 'Total €1000000.00', merchant='Vendor').amount, '1000000')
        self.assertIsNone(extract_document(base + 'Amount due €0.00', merchant='Vendor'))

    def test_r2_wrapped_exclusions_and_vat_tables(self):
        for name in ('vat_amounts', 'vat_columns'):
            with self.subTest(name=name):
                self.assertEqual(extract_document(fixture(name), merchant='Vendor').amount, '123')
        reordered = fixture('vat_columns').replace('Netto\nVAT\nBrutto\n100,00 PLN\n23,00 PLN\n123,00 PLN',
                                                    'Brutto\nVAT\nNetto\n123,00 PLN\n23,00 PLN\n100,00 PLN')
        self.assertEqual(extract_document(reordered, merchant='Vendor').amount, '123')
        base = 'Invoice number X1\nDate issued 2026-11-03\n'
        for value in ('100 PLN netto', '100 EUR excluding tax', '100 PLN VAT'):
            with self.subTest(value=value):
                self.assertIsNone(extract_document(base + 'Total\n' + value, merchant='Vendor'))
                self.assertEqual(extract_document(base + 'Total\n' + value + '\nAmount paid 123 PLN',
                                                  merchant='Vendor').amount, '123')
        # An incomplete vertical header must not reclassify its net column as gross.
        text = fixture('vat_columns').replace('Brutto\n', '')
        self.assertIsNone(extract_document(text, merchant='Vendor'))

    def test_r3_paragon_heading_never_becomes_number(self):
        body = 'Paragon fiskalny\nData faktury 03.11.2026\nKwota brutto 10 PLN'
        self.assertIsNone(extract_document(body, merchant='Vendor'))
        doc = extract_document(body + '\nFaktura nr X1', merchant='Vendor')
        self.assertEqual((doc.invoice_number, doc.receipt_number, doc.document_kind), ('X1', None, 'invoice'))
        self.assertIsNone(extract_document(body.replace('fiskalny', 'ABC'), merchant='Vendor'))

    def test_r4_buyer_markers_and_from_context(self):
        start = ('Faktura nr X1\nSprzedawca: Sklep Test\nNIP: 8567346215\n'
                 'Data faktury 03.11.2026\nKwota brutto 10 PLN\n')
        for marker in ('Nabywca', 'Odbiorca', 'Dane nabywcy', 'Kupujący', 'Bill to', 'Billed to', 'Customer'):
            with self.subTest(marker=marker):
                text = start + marker + ': Klient Test\nNIP: 1234563218\nFrom billing period\nFrom: Wrong Seller'
                doc = extract_document(text, merchant='Vendor')
                self.assertEqual((doc.issuer, doc.nip), ('Sklep Test', fake_nip('856734621')))
        bare = 'Invoice number X1\nDate issued 2026-11-03\nTotal EUR 10\nFrom\nSeller Test'
        self.assertEqual(extract_document(bare, merchant='Vendor').issuer, 'Seller Test')
        prose = bare.replace('From\nSeller Test', 'From Vendor billing period')
        self.assertEqual(extract_document(prose, merchant='Vendor').issuer, 'Vendor')
        text = start.replace('Sprzedawca: Sklep Test\n', 'Sprzedawca: Sklep Test Odbiorca: Klient Test\n')
        self.assertIsNone(extract_document(text, merchant='Vendor').nip)

    def test_n3_paid_invoice_and_real_receipt_kinds(self):
        invoice = 'Faktura nr X1\nData faktury 03.11.2026\nSuma: 12 PLN\nZapłacono: 12 PLN'
        doc = extract_document(invoice, merchant='Vendor')
        self.assertEqual((doc.document_kind, doc.paid), ('invoice', True))
        self.assertEqual(extract_document('Receipt\nInvoice number X1\nDate issued 2026-11-03\n'
                                          'Amount paid €12', merchant='Vendor').document_kind, 'receipt')
        self.assertEqual(extract_document('Paragon nr P1\nData faktury 03.11.2026\nSuma: 12 PLN',
                                          merchant='Vendor').document_kind, 'receipt')

    def test_n4_amount_in_words_ignored(self):
        text = fixture('grocery_grand_total') + '\nSłownie: szesnaście złotych osiemdziesiąt groszy'
        self.assertEqual(extract_document(text, merchant='Vendor').amount, '16.8')

    def test_table_headers_fail_closed_when_incomplete_or_ambiguous(self):
        from hestia.budget.gmail_document import _table_fields
        for lines in (['Numer faktury'], ['Numer faktury', 'Nabywca'],
                      ['Numer faktury', 'Data faktury'],
                      ['Numer faktury', 'Data faktury', 'Nabywca', 'value'],
                      ['Numer faktury', 'Data faktury', 'X1', 'NIP:']):
            with self.subTest(lines=lines):
                fields = {}
                self.assertEqual(_table_fields(lines, fields), set())
                self.assertEqual(fields, {})
        fields = {}
        self.assertEqual(_table_fields(['Numer faktury', 'Data faktury', 'X1', '2026-11-03'], fields), {0, 1})
        self.assertEqual(fields, {'invoice': ['X1'], 'issue': ['2026-11-03']})
        for values in (['Tax 1.00', '2026-11-03'], ['X1', 'VAT ID: IE1234567WA'],
                       ['Wartość netto', '2026-11-03']):
            with self.subTest(values=values):
                fields = {}
                self.assertEqual(_table_fields(['Numer faktury', 'Data faktury'] + values, fields), set())
                self.assertEqual(fields, {})
        text = 'Invoice number X1\nDate issued 2026-11-03\nTotal €10\nNumer faktury'
        self.assertEqual(extract_document(text, merchant='Vendor').invoice_number, 'X1')

    def test_vat_summary_rejects_excluded_invalid_and_partial_values(self):
        base = 'Faktura nr X1\nData faktury 03.11.2026\nSuma:\n'
        for value in ('100 PLN netto', '-1 PLN', 'unknown'):
            with self.subTest(value=value):
                self.assertIsNone(extract_document(base + value, merchant='Vendor'))
        self.assertEqual(extract_document(base + '100 PLN', merchant='Vendor').amount, '100')

    def test_bare_from_after_buyer_does_not_reopen_seller(self):
        text = ('Faktura nr X1\nSprzedawca: Sklep Test\nNIP: 8567346215\n'
                'Odbiorca: Klient Test\nFrom\nWrong Seller\nNIP: 1234563218\n'
                'Data faktury 03.11.2026\nSuma: 10 PLN')
        doc = extract_document(text, merchant='Vendor')
        self.assertEqual((doc.issuer, doc.nip), ('Sklep Test', fake_nip('856734621')))

    def test_extra_eu_vat_ids_do_not_discard_seller_nip(self):
        seller_nip = fake_nip('856734621')
        base = ('Faktura nr X1\nSprzedawca: Sklep Test\nNIP: ' + seller_nip + '\n'
                'Data faktury 03.11.2026\nSuma: 10 PLN\n')
        for extra in ('NIP UE: PL' + seller_nip, 'VAT ID: IE1234567WA', 'Nr VAT: DE123456789',
                      'NIP: PL123456789012', 'NIP: IE1234567WA', 'NIP: DE123456789',
                      'NIP: CHE123456789', 'NIP: UE: PL123456789012', 'NIP:\nVAT ID: IE1234567WA'):
            with self.subTest(extra=extra):
                self.assertEqual(extract_document(base + extra, merchant='Vendor').nip, seller_nip)
        self.assertIsNone(extract_document(base + 'NIP: 1234567890', merchant='Vendor').nip)
        self.assertEqual(extract_document(base.replace('NIP: ' + seller_nip, 'NIP: PL' + seller_nip),
                                          merchant='Vendor').nip, seller_nip)

    def test_extra_eu_vat_ids_in_two_column_seller_block(self):
        seller_nip = fake_nip('856734621')
        buyer_nip = fake_nip('123456321')
        base = ('Faktura nr X1\nSprzedawca: Nabywca:\nSklep Test\n{ids}\n'
                'Klient Test\nNIP: ' + buyer_nip + '\nData faktury 03.11.2026\nSuma: 10 PLN')
        for ids in ('NIP UE: PL123456789012\nNIP: ' + seller_nip,
                    'NIP: IE1234567WA\nNIP: ' + seller_nip,
                    'NIP:\nIE1234567WA\n\nNIP: ' + seller_nip,
                    'NIP UE:\nPL123456789012\nNIP: ' + seller_nip,
                    'NIP: ' + seller_nip + '\nNIP UE: PL123456789012'):
            with self.subTest(ids=ids):
                self.assertEqual(extract_document(base.format(ids=ids), merchant='Vendor').nip, seller_nip)
        self.assertIsNone(extract_document(base.format(ids='NIP UE: IE1234567WA'), merchant='Vendor').nip)
        last = 'Faktura nr X1\nData faktury 03.11.2026\nSuma: 10 PLN\nSprzedawca: Nabywca:\nSklep Test'
        self.assertIsNone(extract_document(last, merchant='Vendor').nip)

    def test_unsafe_or_overlong_stripe_seller_falls_back_to_config(self):
        body = '\nInvoice number X1\nDate issued 2026-11-03\nTotal EUR 10'
        for first in ('billing@example.test', 'A' * 129):
            with self.subTest(first=first):
                doc = extract_document(first + body, merchant='Configured Vendor')
                self.assertEqual(doc.issuer, 'Configured Vendor')

    def test_h1_full_page_skips_header_only_labels(self):
        text = fixture('h1_header_only')
        self.assertGreater(text.index('Do zapłaty w EUR'), 3000)
        doc = extract_document(text, merchant='Vendor')
        self.assertEqual((doc.invoice_number, doc.issue_date, doc.due_date, doc.amount, doc.currency),
                         ('1234567890', '2026-01-02', '2026-01-15', '12.3', 'EUR'))
        base = 'Invoice number X1\nDate issued 2026-11-03\nTotal EUR 10\n'
        for header in ('Invoice number\nDate due 2026-11-10', 'Date due\nTax 1.00',
                       'Sprzedawca\nVAT ID: IE1234567WA', 'Amount due\nWartość netto',
                       'Invoice number\nNr VAT: DE123456789'):
            with self.subTest(header=header):
                result = extract_document(base + header, merchant='Vendor')
                self.assertIsNotNone(result)
                self.assertEqual((result.invoice_number, result.amount), ('X1', '10'))

    def test_h2_billing_summary_falls_back_and_identical_totals_collapse(self):
        text = fixture('h2_billing_summary')
        doc = extract_document(text, merchant='Configured Vendor')
        self.assertEqual((doc.document_kind, doc.issuer, doc.invoice_number, doc.issue_date, doc.amount, doc.currency),
                         ('invoice', 'Configured Vendor', 'G123456789', '2026-09-11', '5', 'EUR'))
        self.assertIsNone(extract_document(text.replace('Total EUR 5.00', 'Total EUR 6.00'),
                                           merchant='Configured Vendor'))
        for heading in ('Summary', 'INVOICE', 'Receipt', 'statement', 'FAKTURA', 'Rachunek',
                        'PARAGON', 'billing service'):
            with self.subTest(heading=heading):
                result = extract_document(text.replace('Billing Summary', heading), merchant='Configured Vendor')
                self.assertEqual(result.issuer, 'Configured Vendor')
        self.assertIsNone(extract_document(text + '\nStatement of account', merchant='Configured Vendor'))
