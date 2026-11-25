"""Synthetic v1f gateway payment evidence: status, refs, fees, hosts, merge and evidence."""
import base64
from dataclasses import replace
from datetime import date
from email.message import EmailMessage
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from sqlalchemy import select

from hestia import db
from hestia.budget import gmail_message
from hestia.budget.gmail_invoice import extract_invoice
from hestia.budget.gmail_payment import ImportedPayment, inspect_payment
from hestia.budget.gmail_store import (DAY as SECONDS_PER_DAY, GATEWAY_REF_DOMAIN, ORDER_REFERENCE_DOMAIN,
                                       PAYMENT_SENDER_DOMAIN, PROVENANCE_TTL, RFC_MESSAGE_DOMAIN, GmailStore,
                                       PendingMessage, PendingPayment, _failed_gateway_key, digest, retain_imports)
from hestia.budget.gmail_trust import PaymentSender
from hestia.budget.receipt_parser import ReceiptError
from hestia.budget.receipt_store import BudgetReceiptStore
from tests.test_budget_gmail_sync import FakeClock


_FIXTURES = Path(__file__).parent / "fixtures" / "gmail" / "payment" / "v1f"
_MERCHANT = "Example Gateway"
_SECRET = b"synthetic-key-for-v1f-tests"


def _ref(value):
    return digest(_SECRET, GATEWAY_REF_DOMAIN, value)


def _inspect(fixture, subject, *, gateway=True, merchant=_MERCHANT, provider=None):
    body = (_FIXTURES / fixture).read_text(encoding="utf-8")
    return inspect_payment(body, subjects=(subject,), merchant=merchant, gateway=gateway, provider=provider)


class V1fFixtureTests(unittest.TestCase):
    def test_fixture_table(self):
        cases = (
            ("p24_registered.txt", "Nowa transakcja płatnicza (P24-ABC-123-DEF)", "registered", "P24-ABC-123-DEF",
             None, ImportedPayment("SKLEP PRZYKŁADOWY SP. Z O.O.", "Zamówienie 700100200", "120", "PLN", (),
                                   "700100200", fee_amount="0")),
            ("p24_paid.txt", "Potwierdzenie (P24-ABC-123-DEF)", "paid", "P24-ABC-123-DEF",
             None, ImportedPayment("SKLEP PRZYKŁADOWY SP. Z O.O.", "Zamówienie 700100200", "121.5", "PLN", (),
                                   "700100200", fee_amount="1.5")),
            ("p24_card_verification.txt", "Rejestracja karty w aplikacji mobilnej", None, None,
             "card_verification", None),
            ("tpay_registered.txt", "Nowa transakcja płatnicza TR-ABCD-1234XYZX", "registered", "TR-ABCD-1234XYZX",
             None, ImportedPayment("SKLEP PRZYKŁADOWY", "Zamówienie 500600700", "83.84", "PLN", (),
                                   "500600700", fee_amount="0")),
            ("tpay_paid.txt", "Potwierdzenie transakcji TR-ABCD-1234XYZX/9999 za zamówienie", "paid",
             "TR-ABCD-1234XYZX", None, ImportedPayment("SKLEP PRZYKŁADOWY", "Zamówienie 500600700", "83.84", "PLN",
                                                       (), "500600700", fee_amount="0")),
            ("autopay_template_a.txt", "Potwierdzenie płatności", "paid", "SYNTH0001AA", None,
             ImportedPayment("SKLEP AUTOPAY PRZYKŁAD", "Potwierdzenie płatności", "214.7", "PLN", (),
                             fee_amount="2.36")),
            ("autopay_template_b.txt", "Potwierdzenie płatności", "paid", "SYNTH0002BB", None,
             ImportedPayment("SKLEP AUTOPAY PRZYKŁAD", "Potwierdzenie płatności", "214.7", "PLN", (),
                             fee_amount="2.36")),
            ("autopay_registered.txt", "Zarejestrowaliśmy Twoje zlecenie", "registered", "SYNTH0003CC", None,
             ImportedPayment("FUNDACJA PRZYKŁADOWA", "Zarejestrowaliśmy Twoje zlecenie", "47.25", "PLN", ())),
            ("autopay_paid.txt", "Przekazaliśmy Twoją płatność", "paid", "SYNTH0003CC", None,
             ImportedPayment("FUNDACJA PRZYKŁADOWA", "Przekazaliśmy Twoją płatność", "47.25", "PLN", ())),
            ("autopay_paid_www.txt", "Potwierdzenie przekazania pieniędzy", "paid", "SYNTH0004DD", None,
             ImportedPayment("SKLEP INTERNETOWY PRZYKŁAD", "Potwierdzenie przekazania pieniędzy", "66", "PLN", (),
                             payee_host="shop.example.test")),
            ("payu_registered.txt", "Zlecenie płatności (123456789) dla FUNDACJA EXAMPLE", "registered", "123456789",
             None, ImportedPayment("FUNDACJA EXAMPLE", "Zlecenie płatności (123456789) dla FUNDACJA EXAMPLE",
                                   "25", "PLN", (), payee_host="example.test")),
            ("paynow_registered.txt", "Zlecenie płatności", "registered", "AB12-CDE-FGH-IJK", None,
             ImportedPayment("SKLEP PAYNOW PRZYKŁAD", "Zlecenie płatności", "75", "PLN", ())),
            ("paynow_failed.txt", "Transakcja AB12-CDE-FGH-IJK straciła ważność", None, "AB12-CDE-FGH-IJK",
             "failed", None),
            ("paynow_failed_solo.txt", "Transakcja ZZ99-YYY-XXX-WWV straciła ważność", None, "ZZ99-YYY-XXX-WWV",
             "failed", None),
            ("total_mismatch.txt", "Potwierdzenie płatności", None, None, None, None),
            ("p24_invoice_paid.txt", "Potwierdzenie (P24-INV-202-AAA)", "paid", "P24-INV-202-AAA", None,
             ImportedPayment("T-MOBILE POLSKA S.A.", "Potwierdzenie (P24-INV-202-AAA)", "101.5", "PLN", (),
                             fee_amount="1.5")),
            ("shop_payment_confirmation.txt", "Zamówienie nr 800900100 opłacone", None, None, None,
             ImportedPayment(_MERCHANT, "Zamówienie nr 800900100 opłacone", "66", "PLN", (), "800900100")),
            ("shop_gateway_paid.txt", "Potwierdzenie płatności", "paid", "SYNTH0006FF", None,
             ImportedPayment("SKLEP INTERNETOWY PRZYKŁAD", "Potwierdzenie płatności", "67", "PLN", (),
                             fee_amount="1")),
        )
        self.assertEqual({path.name for path in _FIXTURES.glob("*.txt")},
                         {case[0] for case in cases} | {"invoice_tmobile_style.txt"})
        for fixture, subject, status, ref, reason, payment in cases:
            with self.subTest(fixture=fixture):
                gateway = not fixture.startswith("shop_payment")
                provider = fixture.split("_", 1)[0]
                if fixture in {"shop_gateway_paid.txt", "total_mismatch.txt"}:
                    provider = "autopay"
                inspected = _inspect(fixture, subject, gateway=gateway, provider=provider if gateway else None)
                self.assertEqual((inspected.status, inspected.gateway_ref, inspected.reject_reason),
                                 (status, ref, reason))
                self.assertEqual(inspected.payment, payment)
                self.assertFalse(inspected.missing_total)

    def test_tmobile_style_invoice_parses_for_evidence(self):
        invoice = extract_invoice((_FIXTURES / "invoice_tmobile_style.txt").read_text(encoding="utf-8"))
        self.assertEqual((invoice.issuer, invoice.amount, invoice.currency, invoice.issue_date, invoice.due_date),
                         ("T-Mobile", "100", "PLN", "2026-11-10", "2026-11-25"))


class V1fExtractionTests(unittest.TestCase):
    def test_status_precedence_and_provider_boundaries(self):
        base = "Kwota transakcji: 5,00 PLN\n"
        both = f"{base}Zarejestrowaliśmy Twoje zlecenie\nPłatność zakończona sukcesem"
        self.assertEqual(_inspect_text(both, "Dziękujemy", provider="autopay").status, "registered")
        self.assertEqual(_inspect_text(both, "Przekazaliśmy Twoją płatność", provider="autopay").status, "paid")
        self.assertEqual(_inspect_text(base + "Płatność zakończona sukcesem", "Potwierdzenie", provider="payu").status,
                         "registered")
        self.assertEqual(_inspect_text(base + "Płatność zakończona sukcesem", "Potwierdzenie").status, "registered")
        self.assertEqual(_inspect_text(base, "Receipt from Example", provider="stripe").status, "paid")
        self.assertIsNone(_inspect_text("Total: 5 PLN", "Receipt", gateway=False).status)

    def test_registered_language_does_not_mean_paid_or_failed(self):
        base = "Kwota transakcji: 5,00 PLN\n"
        for provider, subject, body in (
            ("p24", "Nowa transakcja płatnicza", "Jeżeli transakcja nie została opłacona"),
            ("payu", "Sprawdź status swojej płatności", "Możesz sprawdzić, czy transakcja została już opłacona"),
            ("paynow", "Zlecenie", "Możesz sprawdzić, czy transakcja została już opłacona"),
            ("paynow", "Zlecenie", "Jeśli wpłata nie została dokonana, transakcja zostanie anulowana"),
            ("p24", "Nowa transakcja płatnicza",
             "Niniejsza wiadomość jest informacją o potwierdzeniu realizacji płatności."),
            ("autopay", "Zarejestrowaliśmy Twoje zlecenie", "Czy płatność zakończona sukcesem?"),
        ):
            with self.subTest(provider=provider, body=body):
                result = _inspect_text(base + body, subject, provider=provider)
                self.assertEqual((result.status, result.reject_reason), ("registered", None))
                self.assertIsNotNone(result.payment)

    def test_explicit_paid_body_status_lines(self):
        base = "Kwota transakcji: 5,00 PLN\n"
        for provider, body in (
            ("paynow", "Transakcja została\nopłacona"),
            ("paynow", "Transakcja została opłacona"),
            ("paynow", "Status płatności: opłacona"),
            ("autopay", "Przekazaliśmy Twoje pieniądze"),
            ("autopay", "Twoja wpłata została przekazana odbiorcy."),
            ("p24", "Niniejsza wiadomość jest informacją o potwierdzeniu realizacji płatności."),
        ):
            with self.subTest(provider=provider, body=body):
                result = _inspect_text(base + body, "Dziękujemy", provider=provider)
                self.assertEqual((result.status, result.reject_reason), ("paid", None))
        self.assertEqual(_inspect_text(base + "anulowana\nopłacona", "Zlecenie", provider="paynow").reject_reason,
                         "failed")
        card = base + "Rejestracja karty\nzostanie anulowana"
        self.assertEqual(_inspect_text(card, "Potwierdzenie", provider="p24").reject_reason, "card_verification")
        self.assertEqual(_inspect_text(card, "Potwierdzenie", provider="autopay").status, "registered")

    def test_fee_labels_values_and_currency_rules(self):
        base = "Płatność zakończona sukcesem\nKwota transakcji: 10,00 PLN\n"
        self.assertEqual(_inspect_text(base + "Dodatkowe opłaty:\n1,50 PLN", "Potwierdzenie").payment.fee_amount, "1.5")
        self.assertEqual(_inspect_text(base + "Dodatkowe opłaty obciążające klienta: 0,00 PLN", "Potwierdzenie").payment.fee_amount, "0")
        self.assertEqual(_inspect_text(base + "OPŁATA ZA REALIZACJĘ TRANSAKCJI: 2,00 PLN", "Potwierdzenie").payment.fee_amount, "2")
        self.assertIsNone(_inspect_text(base, "Potwierdzenie").payment.fee_amount)
        self.assertIsNone(_inspect_text(base + "Dodatkowe opłaty: brak", "Potwierdzenie").payment.fee_amount)
        self.assertIsNone(_inspect_text(base + "Dodatkowe opłaty: -1,00 PLN", "Potwierdzenie").payment)
        self.assertIsNone(_inspect_text(base + "Dodatkowe opłaty: 1,00 EUR", "Potwierdzenie").payment)
        for amounts in (
            "Do zapłaty: 10,00 PLN\nDodatkowe opłaty: 1,00 EUR",
            "Do zapłaty: 10,00 EUR\nDodatkowe opłaty: 0,00 PLN",
            "Do zapłaty: 11,00 PLN\nDodatkowe opłaty: 1,00 PLN\nOpłata za realizację transakcji: 1,00 EUR",
        ):
            with self.subTest(amounts=amounts):
                self.assertIsNone(_inspect_text(base + amounts, "Potwierdzenie", provider="p24").payment)
        self.assertIsNone(_inspect_text("Do zapłaty: 10,00 PLN\nDodatkowe opłaty: 1,00 EUR", "Potwierdzenie",
                                        provider="p24").payment)
        # Base + fee without a charged label; a charged label without a base.
        self.assertEqual(_inspect_text(base + "Opłata za realizację transakcji: 0,50 PLN", "Potwierdzenie").payment.total_amount, "10.5")
        self.assertEqual(_inspect_text("Do zapłaty: 10,00 PLN", "Potwierdzenie").payment.total_amount, "10")
        self.assertEqual(_inspect_text("Łączna kwota do zapłaty: 10,00 PLN", "Potwierdzenie").payment.total_amount, "10")
        self.assertEqual(_inspect_text("Do zapłaty: 10,00 PLN\nDodatkowe opłaty: 1,00 PLN", "Potwierdzenie").payment.fee_amount, "1")
        # Base and charged currencies must match even without a fee; fee cannot exceed charged even without a base.
        for bad in (
            "Kwota transakcji: 10,00 PLN\nDo zapłaty: 10,00 EUR",
            "Do zapłaty: 10,00 PLN\nDodatkowe opłaty: 10,01 PLN",
        ):
            with self.subTest(bad=bad):
                rejected = _inspect_text(bad, "Potwierdzenie", provider="p24")
                self.assertIsNone(rejected.payment)
                self.assertFalse(rejected.missing_total)
        self.assertIsNone(_inspect_text("Dodatkowe opłaty: 1,00 PLN", "Potwierdzenie").payment)
        self.assertTrue(_inspect_text("Dodatkowe opłaty: 1,00 PLN", "Potwierdzenie").missing_total)
        self.assertIsNone(_inspect_text(base + "Do zapłaty: 1000001,00 PLN", "Potwierdzenie").payment)

    def test_gateway_ref_patterns(self):
        body = "Kwota transakcji: 5,00 PLN\n"
        cases = (
            ("p24", body, "Nowa transakcja płatnicza (p24-abc-123-def)", "P24-ABC-123-DEF"),
            ("tpay", body + "Tytuł płatności: TR-abcd-1234xyzx/9999", "Potwierdzenie", "TR-ABCD-1234XYZX"),
            ("autopay", body + "Identyfikator transakcji Autopay: synth0099xx", "Potwierdzenie", "SYNTH0099XX"),
            ("payu", body + "Status: https://payu.example.test/status?transId=abc987654&token=example",
             "Zlecenie", "ABC987654"),
            ("payu", body, "Zlecenie (555666777) dla Example", "555666777"),
            ("paynow", body + "Transakcja zz99-yyy-xxx-wwv", "Zlecenie", "ZZ99-YYY-XXX-WWV"),
            ("paynow", body, "Potwierdzenie TR-ZZ99-YYY-XXX-WWV", None),
            ("paynow", body, "Zwykłe potwierdzenie", None),
            ("p24", body + "Identyfikator transakcji: synth0009xx", "Potwierdzenie", None),
            (None, body, "Nowa transakcja płatnicza (P24-ABC-123-DEF)", None),
        )
        for provider, text, subject, ref in cases:
            with self.subTest(subject=subject):
                self.assertEqual(_inspect_text(text, subject, provider=provider).gateway_ref, ref)
        # The PayU token is never the reference.
        inspected = _inspect_text(body + "https://payu.example.test/status?token=abcdef&transId=112233", "Zlecenie",
                                  provider="payu")
        self.assertEqual(inspected.gateway_ref, "112233")
        self.assertIsNone(_inspect_text(body + "https://payu.example.test/status?token=987654", "Zlecenie",
                                        provider="payu").gateway_ref)
        self.assertEqual(_inspect_text(body + "Transakcja AB12-CDE-FGH-IJK", "Zlecenie (P24-ABC-123-DEF)",
                                       provider="paynow").gateway_ref, "AB12-CDE-FGH-IJK")

    def test_autopay_next_line_transaction_id_from_table_cells(self):
        base = "Kwota transakcji: 5,00 PLN\n"
        for block, expected in (
            ("Identyfikator transakcji:\nSYNTH0007GG", "SYNTH0007GG"),
            ("Identyfikator transakcji Autopay:\n\nsynth-0008-hh", "SYNTH-0008-HH"),
            ("Identyfikator transakcji\nSYNTH0009II", "SYNTH0009II"),
            ("- Identyfikator transakcji:\nSYNTH0010JJ", "SYNTH0010JJ"),
            ("Identyfikator transakcji:\nKwota transakcji: 5,00 PLN", None),
            ("Identyfikator transakcji:\nOdbiorca", None),
            ("Identyfikator transakcji:\n5,00 PLN", None),
            ("Numer płatności: ABcd_123456_PL_22_11_2026\nIdentyfikator transakcji: SYNTH0011KK",
             "ABCD_123456_PL_22_11_2026"),
            ("Numer płatności:\nABcd_123456_PL_22_11_2026\nIdentyfikator transakcji: SYNTH0012LL",
             "ABCD_123456_PL_22_11_2026"),
            ("Numer płatności:\nOdbiorca\nIdentyfikator transakcji: SYNTH0013MM", "SYNTH0013MM"),
        ):
            with self.subTest(block=block):
                self.assertEqual(_inspect_text(base + block, "Potwierdzenie", provider="autopay").gateway_ref, expected)

    def test_autopay_payment_number_and_on_behalf_of_payee_align_registered_and_paid(self):
        registered_body = (
            "Numer płatności: ABcd_123456_PL_22_11_2026\n"
            "Identyfikator transakcji: REG111111\n"
            "Odbiorca: SKLEP PRZYKŁADOWY SP. Z O.O.\n"
            "Kwota transakcji: 49,99 PLN\n"
        )
        paid_body = (
            "Przekazaliśmy Twoje pieniądze\n"
            "Numer płatności: ABcd_123456_PL_22_11_2026\n"
            "Identyfikator transakcji: PAID222222\n"
            "Odbiorca: Autopay S.A. w imieniu SKLEP PRZYKŁADOWY SP. Z O.O.\n"
            "Kwota transakcji: 49,99 PLN\n"
        )
        reg = _inspect_text(registered_body, "Potwierdzenie rozpoczęcia transakcji", provider="autopay")
        paid = _inspect_text(paid_body, "Przekazaliśmy Twoją płatność", provider="autopay")
        self.assertEqual((reg.status, paid.status), ("registered", "paid"))
        self.assertEqual(reg.gateway_ref, "ABCD_123456_PL_22_11_2026")
        self.assertEqual(paid.gateway_ref, reg.gateway_ref)
        self.assertEqual(paid.payment.merchant, "SKLEP PRZYKŁADOWY SP. Z O.O.")
        self.assertEqual(paid.payment.merchant, reg.payment.merchant)
        # Unlabelled payees keep their wording untouched; labelled host after "w imieniu" feeds payee_host.
        host_behalf = _inspect_text(
            "Odbiorca: Autopay S.A. W IMIENIU 4parents.pl.\nKwota transakcji: 10,00 PLN",
            "Przekazaliśmy Twoją płatność", provider="autopay",
        ).payment
        self.assertEqual((host_behalf.merchant, host_behalf.payee_from_config, host_behalf.payee_host),
                         (_MERCHANT, True, "4parents.pl"))
        unlabelled = _inspect_text(
            "Płatność dla Fundacja w imieniu dzieci\nKwota transakcji: 10,00 PLN",
            "Potwierdzenie", provider="autopay",
        ).payment
        self.assertEqual(unlabelled.merchant, "Fundacja w imieniu dzieci")

    def test_payee_host_sources_and_validation(self):
        base = "Kwota transakcji: 5,00 PLN\n"
        cases = (
            (base + "Adres www: https://WWW.Shop.Example.Test/path", "Potwierdzenie", "shop.example.test"),
            (base + "Adres www: localhost", "Potwierdzenie", None),
            (base + "zarejestrowaliśmy zlecenie płatności dla FUNDACJA EXAMPLE (http://www.example.test).",
             "Zlecenie", "example.test"),
            (base, "Potwierdzenie dla https://sklep.example.test/", "sklep.example.test"),
            (base, "Potwierdzenie dla Example", None),
            (base, "Potwierdzenie", None),
        )
        for text, subject, host in cases:
            with self.subTest(subject=subject):
                self.assertEqual(_inspect_text(text, subject).payment.payee_host, host)

    def test_failed_and_card_verification_shapes(self):
        base = "Kwota transakcji: 5,00 PLN\n"
        for line in ("Transakcja straciła ważność", "Płatność odrzucona", "Status płatności: anulowana"):
            with self.subTest(line=line):
                self.assertEqual(_inspect_text(base + line, "Zlecenie", provider="paynow").reject_reason, "failed")
        for line in ("Książka odrzucona 5,00 PLN", "anulowane " + "x" * 120, "Odbiorca: anulowana"):
            with self.subTest(line=line):
                self.assertIsNone(_inspect_text(base + line, "Zlecenie", provider="paynow").reject_reason)
        self.assertIsNone(_inspect_text(base + "Rejestracja karty", "Zlecenie", provider="p24").reject_reason)
        self.assertIsNone(_inspect_text(base + "zostanie anulowana", "Zlecenie", provider="p24").reject_reason)

    def test_started_subject_is_registered_for_autopay(self):
        body = "Kwota transakcji: 5,00 PLN"
        registered = _inspect_text(body, "Potwierdzenie rozpoczęcia transakcji", provider="autopay")
        self.assertEqual((registered.status, registered.reject_reason), ("registered", None))
        self.assertIsNone(_inspect_text(body, "Potwierdzenie rozpoczęcia transakcji", gateway=False).payment)
        self.assertIsNone(_inspect_text(body, "Rozpoczęcie transakcji").payment)
        self.assertIsNone(_inspect_text(body, "Rozpoczęcie transakcji", provider="autopay").payment)
        registered = _inspect_text("Potwierdzenie rozpoczęcia transakcji\n" + body, "Zlecenie", provider="autopay")
        self.assertEqual((registered.status, registered.reject_reason), ("registered", None))
        self.assertIsNotNone(registered.payment)

    def test_labeled_payee_wins_and_host_like_values_are_hosts(self):
        body = ("Przekazaliśmy Twoje pieniądze\nDo software.example.test\n"
                "Odbiorca:\n\nEXAMPLE SOFTWARE SP. Z O.O.\nKwota transakcji: 33 PLN")
        result = _inspect_text(body, "Przekazaliśmy Twoją płatność dla Example Studio", provider="autopay")
        self.assertEqual((result.status, result.payment.merchant, result.payment.payee_host),
                         ("paid", "EXAMPLE SOFTWARE SP. Z O.O.", "software.example.test"))
        body = "Transakcja została\nopłacona\nOdbiorca wpłaty:\n\nExample Shop\nDo zapłaty: 6 PLN"
        result = _inspect_text(body, "Zlecenie", provider="paynow")
        self.assertEqual((result.status, result.payment.merchant), ("paid", "Example Shop"))
        body = "Przekazaliśmy Twoje pieniądze\nDo https://shop.example.test/\nKwota transakcji: 33 PLN"
        payment = _inspect_text(body, "Przekazaliśmy Twoją płatność", provider="autopay").payment
        self.assertEqual((payment.merchant, payment.payee_from_config, payment.payee_host),
                         (_MERCHANT, True, "shop.example.test"))
        body = "Transakcja została opłacona\nOdbiorca:\nKwota transakcji: 6 PLN"
        payment = _inspect_text(body, "Zlecenie", provider="paynow").payment
        self.assertEqual((payment.merchant, payment.payee_from_config), (_MERCHANT, True))

    def test_trailing_dot_host_detection_and_empty_recipient_guards(self):
        # Trailing sentence dot is stripped before host detection: plain names stay payees, domains become hosts.
        payee_dot = _inspect_text("Płatność dla Allegro.\nKwota transakcji: 15,00 PLN", "Potwierdzenie").payment
        self.assertEqual((payee_dot.merchant, payee_dot.payee_from_config, payee_dot.payee_host),
                         ("Allegro.", False, None))
        host_dot = _inspect_text("Płatność dla 4parents.pl.\nKwota transakcji: 15,00 PLN", "Potwierdzenie").payment
        self.assertEqual((host_dot.merchant, host_dot.payee_from_config, host_dot.payee_host),
                         (_MERCHANT, True, "4parents.pl"))
        # Bulleted lines and fee lines immediately following an empty Odbiorca: must not become the payee.
        for block in (
            "- Odbiorca:\n- Kwota transakcji: 15,00 PLN",
            "- Odbiorca:\n- Status: opłacona\nKwota transakcji: 15,00 PLN",
            "Odbiorca:\nOpłata za realizację transakcji: 1,00 PLN\nKwota transakcji: 15,00 PLN",
            "Odbiorca:\nDodatkowe opłaty:\n0,00 PLN\nKwota transakcji: 15,00 PLN",
        ):
            with self.subTest(block=block):
                payment = _inspect_text(block, "Potwierdzenie", provider="autopay").payment
                self.assertEqual((payment.merchant, payment.payee_from_config), (_MERCHANT, True))

    def test_conflicting_charged_labels_reject(self):
        body = ("Płatność zakończona sukcesem\nKwota transakcji: 10,00 PLN\n"
                "Do zapłaty: 11,00 PLN\nDo zapłaty: 12,00 PLN")
        inspected = _inspect_text(body, "Potwierdzenie")
        self.assertIsNone(inspected.payment)
        self.assertFalse(inspected.missing_total)

    def test_unparseable_hosts_are_dropped(self):
        base = "Kwota transakcji: 5,00 PLN\n"
        # An "Adres www:" value that is not a host at all.
        self.assertIsNone(_inspect_text(base + "Adres www: ???", "Potwierdzenie").payment.payee_host)
        # A parenthesized payee URL without a valid domain.
        body = base + "zarejestrowaliśmy zlecenie płatności dla FUNDACJA EXAMPLE (http://localhost).\n"
        self.assertIsNone(_inspect_text(body, "Zlecenie").payment.payee_host)
        # A subject host without a valid domain.
        self.assertIsNone(_inspect_text(base, "Potwierdzenie dla https://localhost/").payment.payee_host)


def _inspect_text(text, subject, *, gateway=True, provider=None):
    return inspect_payment(text, subjects=(subject,), merchant=_MERCHANT, gateway=gateway, provider=provider)


INVOICE = SimpleNamespace(issuer="T-Mobile", nip="5261040567", invoice_number="501234567890", amount="100",
                          currency="PLN", issue_date="2026-11-10", due_date="2026-11-25")
DAY = date(2026, 11, 12)


def _gateway_payment(*, merchant="T-MOBILE POLSKA S.A.", total="101.5", currency="PLN", status="paid",
                     ref=_ref("ref-1"), gateway="Example Pay", fee="1.5", host=None, day=DAY,
                     description="Faktura 501234567890"):
    fields = ImportedPayment(merchant, description, total, currency, (), fee_amount=fee, payee_host=host)
    return PendingPayment(fields, day, "a" * 64, gateway=True, status=status,
                          gateway_ref_hmac=ref, gateway_name=gateway)


def _shop_payment(*, merchant="Sklep Przykładowy", total="66", currency="PLN", day=DAY):
    fields = ImportedPayment(merchant, "Zamówienie", total, currency, ())
    return PendingPayment(fields, day, "b" * 64)


class V1fStoreTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        _engine, self.sessions = db.init_db(Path(directory.name) / "v1f.db")
        with self.sessions.begin() as session:
            session.add(db.AppMeta(key="users_authoritative", value="1"))
            session.add(db.User(username="owner", password_hash="synthetic", role="admin"))
        self.clock = FakeClock()
        self.store = GmailStore(self.sessions, clock=self.clock.wall)
        self.store.transition("owner", 1, "active")

    def _commit(self, *items):
        return self.store.commit("owner", 1, {}, items)

    def _message(self, index, *, payment=None, invoice=None, outcome="imported", failed_gateway=None):
        return PendingMessage(f"{index:064x}", self.clock.now, "direct_allowlist", outcome,
                              payment=payment, invoice=invoice, failed_gateway=failed_gateway)

    def _receipts(self):
        with self.sessions() as session:
            return session.scalars(select(db.BudgetReceipt).order_by(db.BudgetReceipt.id)).all()

    def _message_row(self, index):
        with self.sessions() as session:
            return session.get(db.BudgetGmailMessage, ("owner", f"{index:064x}"))

    def _confirm(self, receipt_id, *, total=None):
        with self.sessions.begin() as session:
            row = session.get(db.BudgetReceipt, receipt_id)
            row.status = "confirmed"
            row.payment_method = "card"
            if total is not None:
                row.total_amount = total

    def test_registered_then_paid_upgrades_in_place(self):
        registered = _gateway_payment(total="120", fee="0", status="registered")
        paid = _gateway_payment(total="121.5", fee="1.5", status="paid", host="shop.example.test")
        self.assertEqual(self._commit(self._message(1, payment=registered)), (1, 0, True))
        self.assertEqual(self._commit(self._message(2, payment=paid)), (0, 1, True))
        (row,) = self._receipts()
        self.assertEqual((row.gateway, row.gateway_status, row.gateway_ref_hmac, row.total_amount,
                          row.fee_amount, row.payee_host, row.raw_shop_name, row.version),
                         ("Example Pay", "paid", _ref("ref-1"), "121.5", "1.5", "shop.example.test",
                          "T-MOBILE POLSKA S.A.", 2))
        self.assertEqual((self._message_row(2).outcome, self._message_row(2).receipt_id),
                         ("skipped_duplicate", None))
        with self.sessions() as session:
            line = session.scalar(select(db.BudgetReceiptLine))
            self.assertEqual((line.gross_line_total, line.net_line_total), ("121.5", "121.5"))
            audit = session.scalar(select(db.BudgetAnalyticsAudit).where(
                db.BudgetAnalyticsAudit.action == "gateway_upgrade"))
            self.assertEqual((audit.subject_type, audit.subject_id), ("receipt", row.id))

    def test_registered_only_stays_registered_and_repeats_skip(self):
        registered = _gateway_payment(total="120", fee="0", status="registered")
        self.assertEqual(self._commit(self._message(1, payment=registered)), (1, 0, True))
        self.assertEqual(self._commit(self._message(2, payment=registered)), (0, 1, True))
        (row,) = self._receipts()
        self.assertEqual((row.gateway_status, row.total_amount, row.version), ("registered", "120", 1))

    def test_paid_first_then_registered_keeps_one_paid_row(self):
        self.assertEqual(self._commit(self._message(1, payment=_gateway_payment())), (1, 0, True))
        self.assertEqual(self._commit(self._message(2, payment=_gateway_payment(status="registered", total="100"))),
                         (0, 1, True))
        (row,) = self._receipts()
        self.assertEqual((row.gateway_status, row.total_amount), ("paid", "101.5"))

    def test_paid_duplicate_of_paid_skips(self):
        self.assertEqual(self._commit(self._message(1, payment=_gateway_payment())), (1, 0, True))
        self.assertEqual(self._commit(self._message(2, payment=_gateway_payment())), (0, 1, True))
        self.assertEqual(len(self._receipts()), 1)

    def test_distinct_refs_and_gateways_stay_separate(self):
        self.assertEqual(self._commit(self._message(1, payment=_gateway_payment(ref=_ref("ref-1")))), (1, 0, True))
        self.assertEqual(self._commit(self._message(2, payment=_gateway_payment(ref=_ref("ref-2")))), (1, 0, True))
        self.assertEqual(self._commit(self._message(3, payment=_gateway_payment(ref=_ref("ref-1"), gateway="Other Pay"))),
                         (1, 0, True))
        self.assertEqual(len(self._receipts()), 3)

    def test_paid_upgrades_confirmed_registered_only_when_amounts_match(self):
        registered = _gateway_payment(total="120", fee="0", status="registered")
        self.assertEqual(self._commit(self._message(1, payment=registered)), (1, 0, True))
        (row,) = self._receipts()
        self._confirm(row.id)
        self.assertEqual(self._commit(self._message(2, payment=_gateway_payment(total="120"))), (0, 1, True))
        (row,) = self._receipts()
        self.assertEqual((row.gateway_status, row.total_amount, row.status, row.version), ("paid", "120", "confirmed", 1))

    def test_paid_leaves_confirmed_registered_with_different_amounts(self):
        registered = _gateway_payment(total="120", fee="0", status="registered")
        self.assertEqual(self._commit(self._message(1, payment=registered)), (1, 0, True))
        (row,) = self._receipts()
        self._confirm(row.id)
        self.assertEqual(self._commit(self._message(2, payment=_gateway_payment(total="121.5"))), (0, 1, True))
        (row,) = self._receipts()
        self.assertEqual((row.gateway_status, row.total_amount, row.version), ("registered", "120", 1))

    def test_paid_upgrade_with_a_different_currency_leaves_registration_untouched(self):
        registered = _gateway_payment(total="120", fee="0", status="registered")
        self.assertEqual(self._commit(self._message(1, payment=registered)), (1, 0, True))
        paid = _gateway_payment(merchant="Other Shop", currency="EUR", day=date(2026, 11, 13),
                                host="other.example.test")
        self.assertEqual(self._commit(self._message(2, payment=paid)), (0, 1, True))
        (row,) = self._receipts()
        self.assertEqual((row.gateway_status, row.total_amount, row.currency, row.fee_amount, row.version,
                          row.receipt_date, row.raw_shop_name, row.payee_host, row.updated_at),
                         ("registered", "120", "PLN", "0", 1, DAY.isoformat(), "T-MOBILE POLSKA S.A.", None,
                          row.created_at))
        self.assertEqual(self._message_row(2).outcome, "skipped_duplicate")
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(db.BudgetReceiptLine)).gross_line_total, "120")
            self.assertIsNone(session.scalar(select(db.BudgetAnalyticsAudit).where(
                db.BudgetAnalyticsAudit.action == "gateway_upgrade")))

    def test_failed_tombstones_unconfirmed_registered_row(self):
        registered = _gateway_payment(total="75", fee=None, status="registered", ref=_ref("ref-fail"))
        self.assertEqual(self._commit(self._message(1, payment=registered)), (1, 0, True))
        failed = ("Example Pay", _ref("ref-fail"))
        self.assertEqual(self._commit(self._message(2, outcome="skipped_non_receipt", failed_gateway=failed)),
                         (0, 1, True))
        self.assertEqual(self._receipts(), [])
        self.assertEqual(self._message_row(1).outcome, "deleted")
        self.assertEqual(self._message_row(2).outcome, "skipped_non_receipt")
        self.assertEqual(self._commit(self._message(3, payment=registered)), (0, 1, True))
        self.assertEqual(self._message_row(3).outcome, "skipped_non_receipt")
        self.assertEqual(self._receipts(), [])

    def test_failed_before_registration_blocks_it_and_then_paid_wins(self):
        reference = _ref("ref-fail")
        key = _failed_gateway_key("owner", "Example Pay", reference)
        failed = self._message(1, outcome="skipped_non_receipt", failed_gateway=("Example Pay", reference))
        self.assertEqual(self._commit(failed), (0, 1, True))
        with self.sessions() as session:
            self.assertEqual(json.loads(session.get(db.AppMeta, key).value), {
                "gateway": "Example Pay", "ref_hmac": reference,
                "expires_at": self.clock.now + PROVENANCE_TTL,
            })
        self.assertEqual(self._commit(self._message(2, payment=_gateway_payment(
            status="registered", ref=reference))), (0, 1, True))
        self.assertEqual(self._message_row(2).outcome, "skipped_non_receipt")
        self.assertEqual(self._receipts(), [])
        self.assertEqual(self._commit(self._message(3, payment=_gateway_payment(ref=reference))), (1, 0, True))
        (row,) = self._receipts()
        self.assertEqual((row.gateway_status, row.gateway_ref_hmac), ("paid", reference))
        with self.sessions() as session:
            self.assertIsNone(session.get(db.AppMeta, key))

    def test_failed_dispositions_follow_provenance_ttl_and_grant(self):
        ref = _ref("ref-fail")
        old_ref = _ref("ref-old")
        backfill_edge_ref = _ref("ref-365d")
        key = _failed_gateway_key("owner", "Example Pay", ref)
        old_key = _failed_gateway_key("owner", "Example Pay", old_ref)
        edge_key = _failed_gateway_key("owner", "Example Pay", backfill_edge_ref)
        failed = self._message(1, outcome="skipped_non_receipt", failed_gateway=("Example Pay", ref))
        self.store.commit("owner", 1, {}, (failed,))
        later = replace(failed, gmail_id_hmac=f"{2:064x}", internal_date=failed.internal_date + 10)
        earlier = replace(failed, gmail_id_hmac=f"{3:064x}", internal_date=failed.internal_date - 10)
        self.store.commit("owner", 1, {}, (later, earlier))
        expiry = later.internal_date + PROVENANCE_TTL
        # A message at the 365-day backfill edge still has 35 days left under PROVENANCE_TTL (400 days).
        edge = replace(failed, gmail_id_hmac=f"{9:064x}", failed_gateway=("Example Pay", backfill_edge_ref),
                       internal_date=failed.internal_date - 365 * SECONDS_PER_DAY)
        old = replace(earlier, gmail_id_hmac=f"{4:064x}", failed_gateway=("Example Pay", old_ref),
                      internal_date=failed.internal_date - PROVENANCE_TTL)
        self.store.commit("owner", 1, {}, (edge, old))
        with self.sessions() as session:
            self.assertEqual(json.loads(session.get(db.AppMeta, key).value)["expires_at"], expiry)
            self.assertEqual(json.loads(session.get(db.AppMeta, edge_key).value)["expires_at"],
                             edge.internal_date + PROVENANCE_TTL)
            self.assertIsNone(session.get(db.AppMeta, old_key))
        self.assertEqual(self._commit(self._message(5, payment=_gateway_payment(
            status="registered", ref=ref, gateway="Other Pay"))), (1, 0, True))
        self.clock.now = expiry
        # A due marker no longer blocks even if maintenance has not yet pruned it.
        self.assertEqual(self._commit(self._message(6, payment=_gateway_payment(
            status="registered", ref=ref))), (1, 0, True))
        self.store.retain("owner", 1)
        with self.sessions() as session:
            self.assertIsNone(session.get(db.AppMeta, key))

        self.assertEqual(self._commit(self._message(7, outcome="skipped_non_receipt",
                                                   failed_gateway=("Example Pay", old_ref))), (0, 1, True))
        with self.sessions.begin() as session:
            retain_imports(session, None, self.clock.now)
            self.assertIsNotNone(session.get(db.AppMeta, old_key))
        self.store.transition("owner", self.store.state("owner").version, "revoked")
        with self.sessions() as session:
            self.assertIsNone(session.get(db.AppMeta, old_key))

    def test_failed_leaves_confirmed_row_and_unknown_ref(self):
        registered = _gateway_payment(total="75", fee=None, status="registered", ref=_ref("ref-fail"))
        self.assertEqual(self._commit(self._message(1, payment=registered)), (1, 0, True))
        (row,) = self._receipts()
        self._confirm(row.id)
        failed = ("Example Pay", _ref("ref-fail"))
        self.assertEqual(self._commit(self._message(2, outcome="skipped_non_receipt", failed_gateway=failed)),
                         (0, 1, True))
        (row,) = self._receipts()
        self.assertEqual((row.gateway_status, row.status), ("registered", "confirmed"))
        self.assertEqual(self._commit(self._message(3, outcome="skipped_non_receipt",
                                                    failed_gateway=("Example Pay", _ref("ref-unknown")))),
                         (0, 1, True))

    def test_failed_leaves_paid_row(self):
        self.assertEqual(self._commit(self._message(1, payment=_gateway_payment(ref=_ref("ref-fail")))), (1, 0, True))
        failed = ("Example Pay", _ref("ref-fail"))
        self.assertEqual(self._commit(self._message(2, outcome="skipped_non_receipt", failed_gateway=failed)),
                         (0, 1, True))
        (row,) = self._receipts()
        self.assertEqual(row.gateway_status, "paid")

    def test_invoice_then_gateway_paid_attaches_within_due_window(self):
        self.assertEqual(self._commit(self._message(1, invoice=INVOICE)), (1, 0, True))
        self.assertEqual(self._commit(self._message(2, payment=_gateway_payment())), (1, 0, True))
        rows = self._receipts()
        invoice = [row for row in rows if row.gateway is None][0]
        gateway = [row for row in rows if row.gateway is not None][0]
        self.assertEqual(gateway.evidence_for_receipt_id, invoice.id)
        self.assertEqual((invoice.evidence_for_receipt_id, invoice.gateway), (None, None))

    def test_gateway_then_invoice_attaches_in_reverse(self):
        self.assertEqual(self._commit(self._message(1, payment=_gateway_payment())), (1, 0, True))
        (gateway,) = self._receipts()
        self.assertIsNone(gateway.evidence_for_receipt_id)
        self.assertEqual(self._commit(self._message(2, invoice=INVOICE)), (1, 0, True))
        rows = self._receipts()
        invoice = [row for row in rows if row.gateway is None][0]
        gateway = [row for row in rows if row.gateway is not None][0]
        self.assertEqual(gateway.evidence_for_receipt_id, invoice.id)

    def test_shop_document_same_day_attaches_at_reduced_total(self):
        self.assertEqual(self._commit(self._message(1, payment=_gateway_payment(
            merchant="SKLEP INTERNETOWY PRZYKŁAD", total="67", fee="1", ref=_ref("ref-shop")))), (1, 0, True))
        self.assertEqual(self._commit(self._message(2, payment=_shop_payment())), (1, 0, True))
        rows = self._receipts()
        shop = [row for row in rows if row.gateway is None][0]
        gateway = [row for row in rows if row.gateway is not None][0]
        self.assertEqual((shop.document_kind, gateway.evidence_for_receipt_id), ("payment_confirmation", shop.id))

    def test_gateway_and_shop_payments_at_the_same_total_import_in_both_orders(self):
        for index, gateway_first in enumerate((False, True), start=1):
            with self.subTest(gateway_first=gateway_first):
                total = str(50 + index)
                shop = self._message(index * 2, payment=_shop_payment(total=total))
                gateway = self._message(index * 2 + 1, payment=_gateway_payment(
                    merchant="Sklep Przykładowy", total=total, fee=None, ref=_ref(f"shop-{index}")))
                first, second = (gateway, shop) if gateway_first else (shop, gateway)
                self.assertEqual(self._commit(first), (1, 0, True))
                self.assertEqual(self._commit(second), (1, 0, True))
                shop_row, gateway_row = sorted((row for row in self._receipts() if row.total_amount == total),
                                               key=lambda row: row.gateway is not None)
                self.assertIsNone(shop_row.gateway)
                self.assertEqual(gateway_row.evidence_for_receipt_id, shop_row.id)

    def test_gateway_rows_do_not_attach_to_other_gateways(self):
        self.assertEqual(self._commit(self._message(1, payment=_gateway_payment()),
                                      self._message(2, payment=_gateway_payment(ref=_ref("distinct")))),
                         (2, 0, True))
        self.assertEqual([row.evidence_for_receipt_id for row in self._receipts()], [None, None])

    def test_ambiguous_documents_leave_gateway_standalone(self):
        self.assertEqual(self._commit(self._message(1, invoice=INVOICE)), (1, 0, True))
        second = SimpleNamespace(**{**INVOICE.__dict__, "invoice_number": "501234567891"})
        self.assertEqual(self._commit(self._message(2, invoice=second)), (1, 0, True))
        self.assertEqual(self._commit(self._message(3, payment=_gateway_payment())), (1, 0, True))
        gateway = [row for row in self._receipts() if row.gateway is not None][0]
        self.assertIsNone(gateway.evidence_for_receipt_id)

    def test_payee_key_tie_break_picks_the_matching_document(self):
        matching = SimpleNamespace(**{**INVOICE.__dict__, "issuer": "T-Mobile Polska",
                                      "invoice_number": "501234567891"})
        other = SimpleNamespace(**{**INVOICE.__dict__, "issuer": "Inny Operator", "nip": "0000000000",
                                   "invoice_number": "501234567892"})
        self.assertEqual(self._commit(self._message(1, invoice=matching), self._message(2, invoice=other)),
                         (2, 0, True))
        self.assertEqual(self._commit(self._message(3, payment=_gateway_payment())), (1, 0, True))
        rows = {row.raw_shop_name: row for row in self._receipts()}
        self.assertEqual(rows["T-MOBILE POLSKA S.A."].evidence_for_receipt_id, rows["T-Mobile Polska"].id)
        self.assertIsNone(rows["Inny Operator"].evidence_for_receipt_id)

    def test_nip_tie_break_picks_the_matching_document(self):
        first = SimpleNamespace(**{**INVOICE.__dict__, "issuer": "Inny Operator", "nip": "5261040567",
                                   "invoice_number": "501234567891"})
        second = SimpleNamespace(**{**INVOICE.__dict__, "issuer": "Drugi Operator", "nip": "0000000000",
                                    "invoice_number": "501234567892"})
        self.assertEqual(self._commit(self._message(1, invoice=first), self._message(2, invoice=second)),
                         (2, 0, True))
        self.assertEqual(self._commit(self._message(3, payment=_gateway_payment(merchant="Obcy Podmiot"))),
                         (1, 0, True))
        rows = {row.raw_shop_name: row for row in self._receipts()}
        # Without a payee or NIP match the pair is ambiguous and stays standalone.
        self.assertIsNone(rows["Obcy Podmiot"].evidence_for_receipt_id)
        with self.sessions.begin() as session:
            session.get(db.BudgetReceipt, rows["Obcy Podmiot"].id).raw_nip = "5261040567"
        with self.sessions() as session:
            gateway = session.get(db.BudgetReceipt, rows["Obcy Podmiot"].id)
            candidates = self.store._evidence_candidates(session, "owner", gateway)
            self.assertEqual([document.id for document in candidates], [rows["Inny Operator"].id])

    def test_outside_the_date_window_stays_standalone(self):
        late = _gateway_payment(day=date(2026, 12, 20))
        self.assertEqual(self._commit(self._message(1, invoice=INVOICE)), (1, 0, True))
        self.assertEqual(self._commit(self._message(2, payment=late)), (1, 0, True))
        gateway = [row for row in self._receipts() if row.gateway is not None][0]
        self.assertIsNone(gateway.evidence_for_receipt_id)

    def test_confirmed_gateway_row_is_never_repointed(self):
        self.assertEqual(self._commit(self._message(1, payment=_gateway_payment())), (1, 0, True))
        (gateway,) = self._receipts()
        self._confirm(gateway.id)
        self.assertEqual(self._commit(self._message(2, invoice=INVOICE)), (1, 0, True))
        (gateway,) = [row for row in self._receipts() if row.gateway is not None]
        self.assertIsNone(gateway.evidence_for_receipt_id)

    def test_confirmed_document_can_receive_evidence(self):
        self.assertEqual(self._commit(self._message(1, invoice=INVOICE)), (1, 0, True))
        (invoice,) = self._receipts()
        self._confirm(invoice.id)
        self.assertEqual(self._commit(self._message(2, payment=_gateway_payment())), (1, 0, True))
        gateway = [row for row in self._receipts() if row.gateway is not None][0]
        self.assertEqual(gateway.evidence_for_receipt_id, invoice.id)

    def test_upgrade_keeps_the_existing_attachment(self):
        self.assertEqual(self._commit(self._message(1, invoice=INVOICE)), (1, 0, True))
        registered = _gateway_payment(total="100", fee=None, status="registered")
        self.assertEqual(self._commit(self._message(2, payment=registered)), (1, 0, True))
        (invoice,) = [row for row in self._receipts() if row.gateway is None]
        (gateway,) = [row for row in self._receipts() if row.gateway is not None]
        self.assertEqual(gateway.evidence_for_receipt_id, invoice.id)
        self.assertEqual(self._commit(self._message(3, payment=_gateway_payment())), (0, 1, True))
        (gateway,) = [row for row in self._receipts() if row.gateway is not None]
        self.assertEqual((gateway.gateway_status, gateway.total_amount, gateway.evidence_for_receipt_id),
                         ("paid", "101.5", invoice.id))

    def test_upgrade_revalidates_an_invalid_attachment_and_can_pick_another_document(self):
        self.assertEqual(self._commit(self._message(1, payment=_shop_payment(total="100")),
                                      self._message(2, payment=_shop_payment(total="120"))), (2, 0, True))
        registration = _gateway_payment(total="100", fee=None, status="registered")
        self.assertEqual(self._commit(self._message(3, payment=registration)), (1, 0, True))
        shop_ids = {row.total_amount: row.id for row in self._receipts() if row.gateway is None}
        (gateway,) = [row for row in self._receipts() if row.gateway is not None]
        self.assertEqual(gateway.evidence_for_receipt_id, shop_ids["100"])
        self.assertEqual(self._commit(self._message(4, payment=_gateway_payment(total="121.5"))), (0, 1, True))
        (gateway,) = [row for row in self._receipts() if row.gateway is not None]
        self.assertEqual(gateway.evidence_for_receipt_id, shop_ids["120"])

    def test_paid_upgrade_removes_an_attachment_that_becomes_ambiguous(self):
        self.assertEqual(self._commit(self._message(1, payment=_shop_payment(total="100")),
                                      self._message(2, payment=_shop_payment(total="101.5"))), (2, 0, True))
        registration = _gateway_payment(total="100", fee=None, status="registered")
        self.assertEqual(self._commit(self._message(3, payment=registration)), (1, 0, True))
        (gateway,) = [row for row in self._receipts() if row.gateway is not None]
        self.assertIsNotNone(gateway.evidence_for_receipt_id)
        self.assertEqual(self._commit(self._message(4, payment=_gateway_payment())), (0, 1, True))
        (gateway,) = [row for row in self._receipts() if row.gateway is not None]
        self.assertIsNone(gateway.evidence_for_receipt_id)

    def test_paid_date_change_invalidates_an_imported_document_only(self):
        paid_day = date(2026, 11, 20)
        for index, status in enumerate(("imported", "confirmed", "needs_review"), start=1):
            with self.subTest(status=status):
                total = str(60 + index)
                ref = _ref(f"date-change-{index}")
                register = _gateway_payment(total=total, fee=None, status="registered", ref=ref)
                self.assertEqual(self._commit(self._message(index * 3, payment=_shop_payment(total=total)),
                                              self._message(index * 3 + 1, payment=register)), (2, 0, True))
                shop = next(row for row in self._receipts() if row.gateway is None and row.total_amount == total)
                if status != "imported":
                    with self.sessions.begin() as session:
                        row = session.get(db.BudgetReceipt, shop.id)
                        row.status = status
                        row.payment_method = "card"
                paid = _gateway_payment(total=total, fee=None, ref=ref, day=paid_day)
                self.assertEqual(self._commit(self._message(index * 3 + 2, payment=paid)), (0, 1, True))
                gateway = next(row for row in self._receipts() if row.gateway_ref_hmac == ref)
                self.assertEqual((gateway.receipt_date, gateway.evidence_for_receipt_id),
                                 (paid_day.isoformat(), None if status == "imported" else shop.id))

    def test_evidence_attachment_requires_matching_receipt_kind(self):
        with self.sessions.begin() as session:
            session.add(db.BudgetReceipt(
                id="refund-doc", owner="owner", raw_shop_name="Sklep Przykładowy", receipt_kind="refund",
                receipt_date=DAY.isoformat(), total_amount="66", currency="PLN", status="imported",
                document_kind="receipt", source_kind="email", version=1,
                created_at=self.clock.now, updated_at=self.clock.now,
            ))
        self.assertEqual(self._commit(self._message(1, payment=_gateway_payment(
            merchant="Sklep Przykładowy", total="66", fee=None, ref=_ref("ref-purchase")))), (1, 0, True))
        gateway = next(row for row in self._receipts() if row.gateway_ref_hmac == _ref("ref-purchase"))
        self.assertIsNone(gateway.evidence_for_receipt_id)
        with self.sessions.begin() as session:
            refund = session.get(db.BudgetReceipt, "refund-doc")
            self.store._attach_gateway_for_document(session, "owner", refund)
        gateway = next(row for row in self._receipts() if row.gateway_ref_hmac == _ref("ref-purchase"))
        self.assertIsNone(gateway.evidence_for_receipt_id)

    def test_pending_cap_ignores_evidence_rows(self):
        self.assertEqual(self._commit(self._message(1, invoice=INVOICE)), (1, 0, True))
        self.assertEqual(self._commit(self._message(2, payment=_gateway_payment())), (1, 0, True))
        with self.sessions() as session:
            self.assertEqual(self.store._pending_count(session, "owner"), 1)
        standalone = _gateway_payment(ref=_ref("ref-2"), total="500", fee=None)
        self.assertEqual(self._commit(self._message(3, payment=standalone)), (1, 0, True))
        with self.sessions() as session:
            self.assertEqual(self.store._pending_count(session, "owner"), 2)

    def test_evidence_match_requires_complete_rows(self):
        document = SimpleNamespace(currency="PLN", total_amount="100", receipt_date="2026-11-10",
                                   due_date=None, document_kind="invoice")
        row = {"currency": "PLN", "total_amount": "100", "receipt_date": "2026-11-12", "fee_amount": None}
        match = self.store._evidence_match
        self.assertFalse(match(SimpleNamespace(**{**row, "currency": None}), document))
        self.assertFalse(match(SimpleNamespace(**row), SimpleNamespace(**{**vars(document), "currency": None})))
        self.assertFalse(match(SimpleNamespace(**{**row, "total_amount": None}), document))
        self.assertFalse(match(SimpleNamespace(**row), SimpleNamespace(**{**vars(document), "total_amount": None})))
        self.assertFalse(match(SimpleNamespace(**{**row, "receipt_date": None}), document))
        self.assertFalse(match(SimpleNamespace(**row), SimpleNamespace(**{**vars(document), "receipt_date": None})))

    def test_attach_gateway_for_document_guards_and_ambiguity(self):
        self.assertEqual(self._commit(self._message(1, payment=_gateway_payment())), (1, 0, True))
        standalone = _gateway_payment(ref=_ref("ref-2"), total="500", fee=None)
        self.assertEqual(self._commit(self._message(2, payment=standalone)), (1, 0, True))
        # The reverse check attaches the matching row and skips the non-matching one.
        self.assertEqual(self._commit(self._message(3, invoice=INVOICE)), (1, 0, True))
        rows = {row.gateway_ref_hmac: row for row in self._receipts() if row.gateway is not None}
        (invoice,) = [row for row in self._receipts() if row.gateway is None]
        self.assertEqual(rows[_ref("ref-1")].evidence_for_receipt_id, invoice.id)
        self.assertIsNone(rows[_ref("ref-2")].evidence_for_receipt_id)
        with self.sessions() as session:
            attached = session.get(db.BudgetReceipt, rows[_ref("ref-1")].id)
            # A gateway row is never its own shop document.
            self.store._attach_gateway_for_document(session, "owner", attached)
            self.assertEqual(attached.evidence_for_receipt_id, invoice.id)
            # A row already carrying evidence is never re-pointed.
            document = session.get(db.BudgetReceipt, invoice.id)
            document.evidence_for_receipt_id = attached.id
            self.store._attach_gateway_for_document(session, "owner", document)
            self.assertEqual(document.evidence_for_receipt_id, attached.id)
            session.rollback()

    def test_upgrade_refreshes_a_receipt_without_lines(self):
        registered = _gateway_payment(total="100", fee=None, status="registered")
        self.assertEqual(self._commit(self._message(1, payment=registered)), (1, 0, True))
        with self.sessions.begin() as session:
            session.delete(session.scalar(select(db.BudgetReceiptLine)))
        self.assertEqual(self._commit(self._message(2, payment=_gateway_payment())), (0, 1, True))
        (row,) = self._receipts()
        self.assertEqual((row.gateway_status, row.total_amount, row.version), ("paid", "101.5", 2))
        with self.sessions() as session:
            self.assertIsNone(session.scalar(select(db.BudgetReceiptLine)))


class V1fApiTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        _engine, self.sessions = db.init_db(Path(directory.name) / "v1f-api.db")
        with self.sessions.begin() as session:
            session.add(db.AppMeta(key="users_authoritative", value="1"))
            session.add(db.User(username="owner", password_hash="synthetic", role="admin"))
        self.clock = FakeClock()
        self.store = GmailStore(self.sessions, clock=self.clock.wall)
        self.store.transition("owner", 1, "active")
        self.receipts = BudgetReceiptStore(self.sessions, clock=self.clock.wall, cursor_secret=b"receipts-only")
        self.store.commit("owner", 1, {}, (
            PendingMessage("1" * 64, self.clock.now, "direct_allowlist", "imported", invoice=INVOICE),
            PendingMessage("2" * 64, self.clock.now, "direct_allowlist", "imported", payment=_gateway_payment()),
            PendingMessage("3" * 64, self.clock.now, "direct_allowlist", "imported",
                           payment=_gateway_payment(ref=_ref("ref-standalone"), total="500", fee=None,
                                                    merchant="SKLEP BEZ DOKUMENTU")),
        ))

    def _rows(self):
        with self.sessions() as session:
            return {row.raw_shop_name: row for row in session.scalars(select(db.BudgetReceipt))}

    def test_list_default_excludes_evidence_and_filter_selects(self):
        rows = self._rows()
        evidence_id = rows["T-MOBILE POLSKA S.A."].id
        default = self.receipts.list("owner", {})
        identifiers = {item["receipt_id"] for item in default["receipts"]}
        self.assertNotIn(evidence_id, identifiers)
        self.assertEqual(len(identifiers), 2)
        included = self.receipts.list("owner", {"evidence": "include"})
        self.assertEqual({item["receipt_id"] for item in included["receipts"]},
                         {row.id for row in rows.values()})
        only = self.receipts.list("owner", {"evidence": "only"})
        self.assertEqual([item["receipt_id"] for item in only["receipts"]], [evidence_id])
        with self.assertRaises(ReceiptError):
            self.receipts.list("owner", {"evidence": "bogus"})
        with self.assertRaises(ReceiptError):
            self.receipts.search("owner", {"query": "sklep", "evidence": "only"})

    def test_summary_and_detail_expose_gateway_fields(self):
        rows = self._rows()
        attached = self.receipts.detail("owner", rows["T-MOBILE POLSKA S.A."].id)["receipt"]
        self.assertEqual((attached["gateway"], attached["gateway_status"], attached["fee_amount"],
                          attached["payee_host"], attached["has_document"]),
                         ("Example Pay", "paid", "1.5", None, True))
        self.assertEqual(attached["evidence_for_receipt_id"], rows["T-Mobile"].id)
        standalone = self.receipts.detail("owner", rows["SKLEP BEZ DOKUMENTU"].id)["receipt"]
        self.assertEqual((standalone["has_document"], standalone["evidence_for_receipt_id"],
                          standalone["payment_evidence"]), (False, None, []))
        document = self.receipts.detail("owner", rows["T-Mobile"].id)["receipt"]
        self.assertEqual(document["payment_evidence"], [{
            "receipt_id": rows["T-MOBILE POLSKA S.A."].id, "gateway": "Example Pay", "gateway_status": "paid",
            "total_amount": "101.5", "fee_amount": "1.5", "receipt_date": "2026-11-12",
        }])
        self.assertTrue(document["has_document"])
        listed = {item["receipt_id"]: item for item in self.receipts.list("owner", {})["receipts"]}
        self.assertEqual(listed[rows["SKLEP BEZ DOKUMENTU"].id]["has_document"], False)
        self.assertEqual(listed[rows["T-Mobile"].id]["gateway"], None)


class V1fMessageTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            sender_allowlist=(), spouse_forward_senders=(), hmac_secret=_SECRET,
            owner_mailbox="owner@gmail.com",
            payment_senders=(PaymentSender("@gateway.example.test", "Example Pay", gateway=True, provider="autopay"),
                             PaymentSender("@paynow.example.test", "Example Pay", gateway=True, provider="paynow"),
                             PaymentSender("@p24.example.test", "Example Pay", gateway=True, provider="p24"),
                             PaymentSender("@shop.example.test", "Example Shop")),
        )
        self.worker = Mock()
        self.worker.ready.return_value = True
        self.worker.parse.side_effect = lambda _kind, source: source.decode("utf-8")
        self.processor = gmail_message.GmailMessageProcessor(self.settings, self.worker)

    def _process(self, body, subject, sender="receipts@gateway.example.test"):
        message = EmailMessage()
        message["From"] = sender
        message["Subject"] = subject
        domain = sender.rpartition("@")[2]
        message["Authentication-Results"] = (
            f"mx.google.com; dkim=pass header.d={domain}; dmarc=pass header.from={domain}"
        )
        message.set_content(body)
        metadata = {"id": "msg", "internalDate": "1795566945123", "sizeEstimate": 200,
                    "payload": {"headers": [{"name": name, "value": value}
                                            for name, value in message.raw_items()]}}
        run = Mock()
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
        run.message.side_effect = [metadata, {"id": "msg", "raw": raw}]
        return self.processor.process(run, "msg", "a" * 64)

    def test_paid_payment_carries_status_ref_fee_and_host(self):
        body = ("Płatność zakończona sukcesem\n\nIdentyfikator transakcji: SYNTH0004DD\n"
                "Odbiorca: SKLEP INTERNETOWY PRZYKŁAD\nAdres www: shop.example.test\n"
                "Kwota transakcji: 66,00 PLN\nOpłata za realizację transakcji: 1,00 PLN\nDo zapłaty: 67,00 PLN")
        pending = self._process(body, "Potwierdzenie płatności")
        self.assertEqual(pending.outcome, "imported")
        payment = pending.payment
        self.assertEqual((payment.status, payment.gateway_name, payment.gateway_ref_hmac),
                         ("paid", "Example Pay", digest(_SECRET, GATEWAY_REF_DOMAIN, "SYNTH0004DD")))
        self.assertEqual((payment.fields.fee_amount, payment.fields.payee_host), ("1", "shop.example.test"))
        self.assertEqual(payment.sender_hmac, digest(_SECRET, PAYMENT_SENDER_DOMAIN, "@gateway.example.test"))

    def test_failed_payment_marks_the_gateway_reference(self):
        body = "Status transakcji: straciła ważność\nIdentyfikator transakcji: AB12-CDE-FGH-IJK\nKwota transakcji: 75 PLN"
        pending = self._process(body, "Transakcja straciła ważność", sender="receipts@paynow.example.test")
        self.assertEqual((pending.outcome, pending.failed_gateway),
                         ("skipped_non_receipt", ("Example Pay", digest(_SECRET, GATEWAY_REF_DOMAIN, "AB12-CDE-FGH-IJK"))))
        self.assertIsNone(pending.payment)

    def test_failed_payment_without_a_reference_stays_plain(self):
        body = "Status transakcji: straciła ważność\nKwota transakcji: 75 PLN"
        pending = self._process(body, "Transakcja straciła ważność", sender="receipts@paynow.example.test")
        self.assertEqual((pending.outcome, pending.failed_gateway), ("skipped_non_receipt", None))

    def test_card_verification_is_a_plain_rejection(self):
        body = "Rejestracja karty w aplikacji mobilnej.\nTransakacja zostanie anulowana.\n\nKwota transakcji: 0,01 PLN"
        pending = self._process(body, "Rejestracja karty", sender="receipts@p24.example.test")
        self.assertEqual((pending.outcome, pending.failed_gateway, pending.payment),
                         ("skipped_non_receipt", None, None))

    def test_non_gateway_payment_has_no_gateway_fields(self):
        pending = self._process("Łącznie: 55,52 PLN", "Zamówienie potwierdzone", sender="orders@shop.example.test")
        self.assertEqual(pending.outcome, "imported")
        self.assertEqual((pending.payment.status, pending.payment.gateway_name, pending.payment.gateway_ref_hmac),
                         (None, None, None))

    def test_autopay_registered_and_paid_merge_via_payment_number_and_on_behalf_of_payee(self):
        registered = self._process(
            "Numer płatności: ABcd_123456_PL_22_11_2026\nIdentyfikator transakcji: REG111111\n"
            "Odbiorca: SKLEP PRZYKŁADOWY SP. Z O.O.\nKwota transakcji: 49,99 PLN\n",
            "Potwierdzenie rozpoczęcia transakcji",
        )
        paid = self._process(
            "Przekazaliśmy Twoje pieniądze\nNumer płatności: ABcd_123456_PL_22_11_2026\n"
            "Identyfikator transakcji: PAID222222\n"
            "Odbiorca: Autopay S.A. w imieniu SKLEP PRZYKŁADOWY SP. Z O.O.\nKwota transakcji: 49,99 PLN\n",
            "Przekazaliśmy Twoją płatność",
        )
        self.assertEqual((registered.payment.status, paid.payment.status), ("registered", "paid"))
        self.assertEqual(registered.payment.gateway_ref_hmac, paid.payment.gateway_ref_hmac)
        self.assertEqual(registered.payment.payee_hmac, paid.payment.payee_hmac)
        with TemporaryDirectory() as directory:
            self.addCleanup(db.reset_engine_cache)
            _engine, sessions = db.init_db(Path(directory) / "autopay-merge.db")
            with sessions.begin() as session:
                session.add(db.AppMeta(key="users_authoritative", value="1"))
                session.add(db.User(username="owner", password_hash="synthetic", role="admin"))
            store = GmailStore(sessions, clock=FakeClock().wall)
            store.transition("owner", 1, "active")
            self.assertEqual(store.commit("owner", 1, {}, (
                replace(registered, gmail_id_hmac="1" * 64),
                replace(paid, gmail_id_hmac="2" * 64),
            )), (1, 1, True))
            with sessions() as session:
                (row,) = session.scalars(select(db.BudgetReceipt)).all()
                self.assertEqual((row.gateway_status, row.raw_shop_name, row.total_amount),
                                 ("paid", "SKLEP PRZYKŁADOWY SP. Z O.O.", "49.99"))


_V1F2_FIXTURES = Path(__file__).parent / "fixtures" / "gmail" / "payment" / "v1f2"


class V1f2ForwardedPaymentTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            sender_allowlist=("billing@respectenergy.pl",),
            spouse_forward_senders=("spouse@gmail.com",),
            owner_mailbox="owner@gmail.com",
            hmac_secret=_SECRET,
            payment_senders=(
                PaymentSender("@przelewy24.pl", "Przelewy24", gateway=True),
                PaymentSender("@payu.com", "PayU", gateway=True),
                PaymentSender("@paynow.pl", "Paynow", gateway=True),
                PaymentSender("@shop.example.test", "Example Shop"),
            ),
        )
        self.worker = Mock()
        self.worker.ready.return_value = True
        self.worker.parse.side_effect = lambda _kind, source: source.decode("utf-8")
        self.processor = gmail_message.GmailMessageProcessor(self.settings, self.worker)

    @staticmethod
    def _email(sender, body, *, subject="Fwd: payment", rfc_id="<outer@gmail.com>"):
        domain = sender.rpartition("@")[2]
        message = EmailMessage()
        message["From"] = sender
        message["Subject"] = subject
        if rfc_id is not None:
            message["Message-ID"] = rfc_id
        message["Authentication-Results"] = (
            f"mx.google.com; dkim=pass header.d={domain}; dmarc=pass header.from={domain}"
        )
        message.set_content(body)
        return message

    def _process(self, message, gmail_id="msg-1", internal_date="1795566945123"):
        metadata = {"id": gmail_id, "internalDate": internal_date, "sizeEstimate": 512,
                    "payload": {"headers": [{"name": name, "value": value}
                                            for name, value in message.raw_items()]}}
        run = Mock()
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
        run.message.side_effect = [metadata, {"id": gmail_id, "raw": raw}]
        return self.processor.process(run, gmail_id, digest(_SECRET, b"test", gmail_id))

    def _new_store(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        _engine, sessions = db.init_db(Path(directory.name) / "v1f2.db")
        with sessions.begin() as session:
            session.add(db.AppMeta(key="users_authoritative", value="1"))
            session.add(db.User(username="owner", password_hash="synthetic", role="admin"))
        clock = FakeClock()
        store = GmailStore(sessions, clock=clock.wall)
        store.transition("owner", 1, "active")
        return store, sessions

    def test_forwarded_p24_registered_fixture_parses_and_merges_with_paid(self):
        body = (_V1F2_FIXTURES / "forwarded_p24_registered.txt").read_text(encoding="utf-8")
        forwarded = self._email("spouse@gmail.com", body, rfc_id="<spouse-fwd-p24@gmail.com>")
        pending = self._process(forwarded, gmail_id="fwd-p24")
        self.assertEqual((pending.trust_path, pending.outcome, pending.forwarded_manually, pending.source_kind),
                         ("spouse_forward", "imported", False, "email"))
        self.assertEqual(pending.rfc_message_id_hmac,
                         digest(_SECRET, RFC_MESSAGE_DOMAIN, "p24-fwd-1@przelewy24.example.test"))
        self.assertEqual((pending.payment.gateway, pending.payment.gateway_name, pending.payment.status,
                          pending.payment.gateway_ref_hmac),
                         (True, "Przelewy24", "registered", digest(_SECRET, GATEWAY_REF_DOMAIN, "P24-FWD-123-XYZ")))
        self.assertEqual((pending.payment.fields.merchant, pending.payment.fields.total_amount,
                          pending.payment.fields.currency, pending.payment.fields.fee_amount),
                         ("SKLEP PRZYKŁADOWY SP. Z O.O.", "120", "PLN", "0"))
        # A subsequent paid P24 email with the same gateway ref upgrades the forwarded registration in place.
        paid_msg = self._email(
            "noreply@przelewy24.pl",
            "Potwierdzeniu realizacji płatności\nOdbiorca wpłaty: SKLEP PRZYKŁADOWY SP. Z O.O. 4500000000\n"
            "Kwota transakcji: 120,00 PLN\nDodatkowe opłaty: 1,50 PLN\nDo zapłaty: 121,50 PLN",
            subject="Potwierdzenie (P24-FWD-123-XYZ)", rfc_id="<p24-paid-1@przelewy24.example.test>",
        )
        paid_pending = self._process(paid_msg, gmail_id="direct-p24-paid")
        store, sessions = self._new_store()
        self.assertEqual(store.commit("owner", 1, {}, (pending, paid_pending)), (1, 1, True))
        with sessions() as session:
            (row,) = session.scalars(select(db.BudgetReceipt)).all()
            self.assertEqual((row.gateway, row.gateway_status, row.total_amount, row.fee_amount,
                              row.forwarded_manually),
                             ("Przelewy24", "paid", "121.5", "1.5", 0))
            prov = session.get(db.BudgetGmailMessage, ("owner", pending.gmail_id_hmac))
            self.assertEqual((prov.trust_path, prov.outcome), ("spouse_forward", "imported"))

    def test_forwarded_payu_fixture_parses_under_owner_forward(self):
        body = (_V1F2_FIXTURES / "forwarded_payu_registered.txt").read_text(encoding="utf-8")
        forwarded = self._email("owner@gmail.com", body, rfc_id="<owner-fwd-payu@gmail.com>")
        pending = self._process(forwarded, gmail_id="fwd-payu")
        self.assertEqual((pending.trust_path, pending.outcome, pending.forwarded_manually),
                         ("owner_forward", "imported", False))
        self.assertEqual(pending.rfc_message_id_hmac,
                         digest(_SECRET, RFC_MESSAGE_DOMAIN, "payu-fwd-1@payu.example.test"))
        self.assertEqual((pending.payment.gateway_name, pending.payment.status, pending.payment.gateway_ref_hmac),
                         ("PayU", "registered", digest(_SECRET, GATEWAY_REF_DOMAIN, "987654321")))
        self.assertEqual((pending.payment.fields.merchant, pending.payment.fields.total_amount,
                          pending.payment.fields.payee_host),
                         ("FUNDACJA EXAMPLE", "45", "example.test"))

    def test_forwarded_shop_order_fixture_parses_and_attaches_gateway_evidence(self):
        body = (_V1F2_FIXTURES / "forwarded_shop_order.txt").read_text(encoding="utf-8")
        forwarded = self._email("spouse@gmail.com", body, rfc_id="<spouse-fwd-shop@gmail.com>")
        shop_pending = self._process(forwarded, gmail_id="fwd-shop")
        self.assertEqual((shop_pending.trust_path, shop_pending.outcome, shop_pending.forwarded_manually),
                         ("spouse_forward", "imported", False))
        self.assertEqual((shop_pending.payment.gateway, shop_pending.payment.gateway_name, shop_pending.payment.status),
                         (False, None, None))
        self.assertEqual((shop_pending.payment.fields.merchant, shop_pending.payment.fields.total_amount,
                          shop_pending.payment.order_reference_hmac),
                         ("Example Shop", "89.5", digest(_SECRET, ORDER_REFERENCE_DOMAIN, "880900100")))
        self.assertEqual(shop_pending.rfc_message_id_hmac,
                         digest(_SECRET, RFC_MESSAGE_DOMAIN, "shop-fwd-1@shop.example.test"))
        # Matching gateway payment attaches to the forwarded shop receipt as evidence.
        gw_msg = self._email(
            "noreply@przelewy24.pl",
            "Odbiorca wpłaty: Example Shop\nKwota transakcji: 89,50 PLN\nDodatkowe opłaty: 0,50 PLN\nDo zapłaty: 90,00 PLN",
            subject="Potwierdzenie (P24-SHP-880-AAA)", rfc_id="<p24-shop@przelewy24.example.test>",
        )
        gw_pending = self._process(gw_msg, gmail_id="gw-for-shop")
        store, sessions = self._new_store()
        self.assertEqual(store.commit("owner", 1, {}, (gw_pending, shop_pending)), (2, 0, True))
        with sessions() as session:
            rows = session.scalars(select(db.BudgetReceipt)).all()
            shop_row = next(row for row in rows if row.gateway is None)
            gw_row = next(row for row in rows if row.gateway is not None)
            self.assertEqual(gw_row.evidence_for_receipt_id, shop_row.id)

    def test_forwarded_unknown_sender_and_rejected_payment_fall_back_to_manual(self):
        unknown_body = (_V1F2_FIXTURES / "forwarded_unknown_sender.txt").read_text(encoding="utf-8")
        unknown_msg = self._email("spouse@gmail.com", unknown_body, rfc_id="<spouse-fwd-unknown@gmail.com>")
        pending = self._process(unknown_msg, gmail_id="fwd-unknown")
        self.assertEqual((pending.trust_path, pending.outcome, pending.forwarded_manually,
                          pending.sender_domain, pending.payment, pending.rfc_message_id_hmac),
                         ("spouse_forward", "imported", True, "unknown-shop.example", None, None))
        store, sessions = self._new_store()
        self.assertEqual(store.commit("owner", 1, {}, (pending,)), (1, 0, True))
        with sessions() as session:
            (row,) = session.scalars(select(db.BudgetReceipt)).all()
            self.assertEqual((row.status, row.forwarded_manually, row.forwarded_sender_domain, row.total_amount),
                             ("needs_review", 1, "unknown-shop.example", None))

        # Matched gateway sender whose body rejects (card verification / failed / no total) also falls back to manual.
        for label, sender, subject, inner_body in (
            ("card_verification", "noreply@przelewy24.pl", "Rejestracja karty",
             "Rejestracja karty w aplikacji mobilnej.\nTransakacja zostanie anulowana.\nKwota transakcji: 0,01 PLN"),
            ("failed", "powiadomienia@paynow.pl", "Transakcja straciła ważność",
             "Status transakcji: straciła ważność\nIdentyfikator transakcji: AB12-CDE-FGH-IJK\nKwota transakcji: 75 PLN"),
            ("no_total", "orders@shop.example.test", "Informacja o koncie", "Witaj w naszym sklepie."),
        ):
            with self.subTest(case=label):
                inner = self._email(sender, inner_body, subject=subject, rfc_id=f"<{label}@example.test>")
                outer = self._email("owner@gmail.com", "Forwarding", rfc_id=f"<outer-{label}@gmail.com>")
                outer.add_attachment(inner)
                fallback = self._process(outer, gmail_id=f"fwd-{label}")
                self.assertEqual((fallback.trust_path, fallback.outcome, fallback.forwarded_manually,
                                  fallback.payment, fallback.failed_gateway, fallback.rfc_message_id_hmac),
                                 ("owner_forward", "imported", True, None, None, None))

    def test_forwarded_and_direct_copy_dedupe_in_both_orders_via_inner_rfc_message_id(self):
        inner_rfc_id = "<orig-p24-dedupe@przelewy24.example.test>"
        direct_body = (
            "Potwierdzamy zarejestrowaniu nowej transakcji.\n"
            "Odbiorca wpłaty: SKLEP PRZYKŁADOWY SP. Z O.O. 4500000000\n"
            "Tytuł: Zamówienie 710200300\n"
            "Kwota transakcji: 120,00 PLN\n"
            "Dodatkowe opłaty: 0,00 PLN\n"
        )
        direct_msg = self._email("noreply@przelewy24.pl", direct_body,
                                 subject="Nowa transakcja płatnicza (P24-DED-111-222)", rfc_id=inner_rfc_id)
        attached_fwd = self._email("spouse@gmail.com", "Przesyłam", rfc_id="<spouse-cover-1@gmail.com>")
        attached_fwd.add_attachment(direct_msg)
        inline_fwd = self._email(
            "owner@gmail.com",
            "---------- Forwarded message ---------\n"
            "From: Przelewy24 <noreply@przelewy24.pl>\n"
            "Subject: Nowa transakcja płatnicza (P24-DED-111-222)\n"
            f"Message-ID: {inner_rfc_id}\n\n"
            f"{direct_body}",
            rfc_id="<owner-cover-2@gmail.com>",
        )
        direct_shop = self._email("orders@shop.example.test", "Łącznie: 89,50 PLN\n",
                                  subject="Potwierdzenie zamówienia nr 880900100",
                                  rfc_id="<orig-shop-dedupe@shop.example.test>")
        attached_shop_fwd = self._email("spouse@gmail.com", "Przesyłam sklep", rfc_id="<spouse-shop-cover@gmail.com>")
        attached_shop_fwd.add_attachment(direct_shop)

        for label, direct_pair, fwd_pair in (
            ("p24-attached", ("direct-p24", direct_msg), ("fwd-p24-att", attached_fwd)),
            ("p24-inline", ("direct-p24", direct_msg), ("fwd-p24-inl", inline_fwd)),
            ("shop-attached", ("direct-shop", direct_shop), ("fwd-shop-att", attached_shop_fwd)),
        ):
            direct_pending = self._process(direct_pair[1], gmail_id=direct_pair[0])
            fwd_pending = self._process(fwd_pair[1], gmail_id=fwd_pair[0])
            self.assertIsNotNone(direct_pending.rfc_message_id_hmac)
            self.assertEqual(fwd_pending.rfc_message_id_hmac, direct_pending.rfc_message_id_hmac)
            for order_name, pair in (("direct_then_forward", (direct_pending, fwd_pending)),
                                     ("forward_then_direct", (fwd_pending, direct_pending))):
                with self.subTest(case=label, order=order_name):
                    store, sessions = self._new_store()
                    self.assertEqual(store.commit("owner", 1, {}, (pair[0],)), (1, 0, True))
                    self.assertEqual(store.commit("owner", 1, {}, (pair[1],)), (0, 1, True))
                    with sessions() as session:
                        receipts = session.scalars(select(db.BudgetReceipt)).all()
                        self.assertEqual(len(receipts), 1)
                        self.assertEqual(receipts[0].forwarded_manually, 0)
                        first_row = session.get(db.BudgetGmailMessage, ("owner", pair[0].gmail_id_hmac))
                        second_row = session.get(db.BudgetGmailMessage, ("owner", pair[1].gmail_id_hmac))
                        self.assertEqual((first_row.outcome, second_row.outcome), ("imported", "skipped_duplicate"))
                        self.assertEqual(first_row.receipt_id, receipts[0].id)
                        self.assertIsNone(second_row.receipt_id)
