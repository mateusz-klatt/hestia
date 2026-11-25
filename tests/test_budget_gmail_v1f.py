"""Synthetic v1f gateway payment evidence: status, refs, fees, hosts, merge and evidence."""
import base64
from dataclasses import replace
from datetime import date, datetime, UTC
from email.message import EmailMessage
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
from hestia.budget.gmail_store import (GATEWAY_REF_DOMAIN, GMAIL_MESSAGE_DOMAIN, PAYMENT_SENDER_DOMAIN, GmailStore,
                                       PendingMessage, PendingPayment, digest)
from hestia.budget.gmail_trust import PaymentSender
from hestia.budget.receipt_parser import ReceiptError
from hestia.budget.receipt_store import BudgetReceiptStore
from tests.test_budget_gmail_sync import FakeClock


_FIXTURES = Path(__file__).parent / "fixtures" / "gmail" / "payment" / "v1f"
_MERCHANT = "Example Gateway"
_SECRET = b"synthetic-key-for-v1f-tests"


def _ref(value):
    return digest(_SECRET, GATEWAY_REF_DOMAIN, value)


def _inspect(fixture, subject, *, gateway=True, merchant=_MERCHANT):
    body = (_FIXTURES / fixture).read_text(encoding="utf-8")
    return inspect_payment(body, subjects=(subject,), merchant=merchant, gateway=gateway)


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
                inspected = _inspect(fixture, subject, gateway=gateway)
                self.assertEqual((inspected.status, inspected.gateway_ref, inspected.reject_reason),
                                 (status, ref, reason))
                self.assertEqual(inspected.payment, payment)
                self.assertFalse(inspected.missing_total)

    def test_tmobile_style_invoice_parses_for_evidence(self):
        invoice = extract_invoice((_FIXTURES / "invoice_tmobile_style.txt").read_text(encoding="utf-8"))
        self.assertEqual((invoice.issuer, invoice.amount, invoice.currency, invoice.issue_date, invoice.due_date),
                         ("T-Mobile", "100", "PLN", "2026-11-10", "2026-11-25"))


class V1fExtractionTests(unittest.TestCase):
    def test_paid_wins_and_unknown_gateway_is_registered(self):
        both = "Nowa transakcja płatnicza\nPłatność zakończona sukcesem\nKwota transakcji: 5,00 PLN"
        self.assertEqual(_inspect_text(both, "Nowa transakcja płatnicza").status, "paid")
        unknown = "Dziękujemy.\nKwota transakcji: 5,00 PLN"
        self.assertEqual(_inspect_text(unknown, "Podsumowanie").status, "registered")
        self.assertIsNone(_inspect_text("Total: 5 PLN", "Receipt", gateway=False).status)

    def test_fee_labels_values_and_currency_rules(self):
        base = "Płatność zakończona sukcesem\nKwota transakcji: 10,00 PLN\n"
        self.assertEqual(_inspect_text(base + "Dodatkowe opłaty:\n1,50 PLN", "Potwierdzenie").payment.fee_amount, "1.5")
        self.assertEqual(_inspect_text(base + "Dodatkowe opłaty obciążające klienta: 0,00 PLN", "Potwierdzenie").payment.fee_amount, "0")
        self.assertEqual(_inspect_text(base + "OPŁATA ZA REALIZACJĘ TRANSAKCJI: 2,00 PLN", "Potwierdzenie").payment.fee_amount, "2")
        self.assertIsNone(_inspect_text(base, "Potwierdzenie").payment.fee_amount)
        self.assertIsNone(_inspect_text(base + "Dodatkowe opłaty: brak", "Potwierdzenie").payment.fee_amount)
        self.assertIsNone(_inspect_text(base + "Dodatkowe opłaty: -1,00 PLN", "Potwierdzenie").payment)
        self.assertIsNone(_inspect_text(base + "Dodatkowe opłaty: 1,00 EUR", "Potwierdzenie").payment)
        # Base + fee without a charged label; a charged label without a base.
        self.assertEqual(_inspect_text(base + "Opłata za realizację transakcji: 0,50 PLN", "Potwierdzenie").payment.total_amount, "10.5")
        self.assertEqual(_inspect_text("Do zapłaty: 10,00 PLN", "Potwierdzenie").payment.total_amount, "10")
        self.assertEqual(_inspect_text("Łączna kwota do zapłaty: 10,00 PLN", "Potwierdzenie").payment.total_amount, "10")
        self.assertIsNone(_inspect_text("Dodatkowe opłaty: 1,00 PLN", "Potwierdzenie").payment)
        self.assertTrue(_inspect_text("Dodatkowe opłaty: 1,00 PLN", "Potwierdzenie").missing_total)
        self.assertIsNone(_inspect_text(base + "Do zapłaty: 1000001,00 PLN", "Potwierdzenie").payment)

    def test_gateway_ref_patterns(self):
        body = "Kwota transakcji: 5,00 PLN\n"
        cases = (
            (body, "Nowa transakcja płatnicza (P24-ABC-123-DEF)", "P24-ABC-123-DEF"),
            (body + "Tytuł płatności: TR-ABCD-1234XYZX/9999", "Potwierdzenie", "TR-ABCD-1234XYZX"),
            (body + "Identyfikator transakcji Autopay: synth0099xx", "Potwierdzenie", "SYNTH0099XX"),
            (body + "Status: https://payu.example.test/status?transId=987654&token=secret", "Zlecenie", "987654"),
            (body, "Zlecenie (555666777) dla Example", "555666777"),
            (body + "Transakcja ZZ99-YYY-XXX-WWV", "Zlecenie", "ZZ99-YYY-XXX-WWV"),
            (body, "Zwykłe potwierdzenie", None),
        )
        for text, subject, ref in cases:
            with self.subTest(subject=subject):
                self.assertEqual(_inspect_text(text, subject).gateway_ref, ref)
        # The PayU token is never the reference.
        inspected = _inspect_text(body + "https://payu.example.test/status?token=abcdef&transId=112233", "Zlecenie")
        self.assertEqual(inspected.gateway_ref, "112233")

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
        self.assertEqual(_inspect_text(base + "Transakcja straciła ważność", "Zlecenie").reject_reason, "failed")
        self.assertEqual(_inspect_text(base + "Zlecenie anulowane", "Zlecenie").reject_reason, "failed")
        self.assertEqual(_inspect_text(base + "Płatność odrzucona", "Zlecenie").reject_reason, "failed")
        # A priced or overlong line is an item, not a failure notice.
        self.assertIsNone(_inspect_text(base + "Książka odrzucona 5,00 PLN", "Zlecenie").reject_reason)
        self.assertIsNone(_inspect_text(base + "anulowane " + "x" * 120, "Zlecenie").reject_reason)
        self.assertIsNone(_inspect_text(base + "Odbiorca: anulowana", "Zlecenie").reject_reason)
        self.assertEqual(_inspect_text(base + "Status płatności: anulowana", "Zlecenie").reject_reason, "failed")
        self.assertEqual(_inspect_text(base + "Rejestracja karty\nzostanie anulowana", "Zlecenie").reject_reason,
                         "card_verification")
        self.assertIsNone(_inspect_text(base + "Rejestracja karty", "Zlecenie").reject_reason)

    def test_started_subject_is_registered_only_for_gateways(self):
        body = "Kwota transakcji: 5,00 PLN"
        registered = _inspect_text(body, "Potwierdzenie rozpoczęcia transakcji")
        self.assertEqual((registered.status, registered.reject_reason), ("registered", None))
        self.assertIsNone(_inspect_text(body, "Potwierdzenie rozpoczęcia transakcji", gateway=False).payment)
        self.assertIsNone(_inspect_text(body, "Rozpoczęcie transakcji").payment)
        registered = _inspect_text("Potwierdzenie rozpoczęcia transakcji\n" + body, "Zlecenie")
        self.assertEqual((registered.status, registered.reject_reason), ("registered", None))
        self.assertIsNotNone(registered.payment)


def _inspect_text(text, subject, *, gateway=True):
    return inspect_payment(text, subjects=(subject,), merchant=_MERCHANT, gateway=gateway)


INVOICE = SimpleNamespace(issuer="T-Mobile", nip="5261040567", invoice_number="501234567890", amount="100",
                          currency="PLN", issue_date="2026-11-10", due_date="2026-11-25")
DAY = date(2026, 11, 12)


def _gateway_payment(*, merchant="T-MOBILE POLSKA S.A.", total="101.5", currency="PLN", status="paid",
                     ref=_ref("ref-1"), gateway="Example Pay", fee="1.5", host=None, day=DAY,
                     description="Faktura 501234567890"):
    fields = ImportedPayment(merchant, description, total, currency, (), fee_amount=fee, payee_host=host)
    return PendingPayment(fields, day, "a" * 64, (), gateway=True, status=status,
                          gateway_ref_hmac=ref, gateway_name=gateway)


def _shop_payment(*, merchant="Sklep Przykładowy", total="66", currency="PLN", day=DAY):
    fields = ImportedPayment(merchant, "Zamówienie", total, currency, ())
    return PendingPayment(fields, day, "b" * 64, (), gateway=False)


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

    def test_failed_tombstones_unconfirmed_registered_row(self):
        registered = _gateway_payment(total="75", fee=None, status="registered", ref=_ref("ref-fail"))
        self.assertEqual(self._commit(self._message(1, payment=registered)), (1, 0, True))
        failed = ("Example Pay", _ref("ref-fail"))
        self.assertEqual(self._commit(self._message(2, outcome="skipped_non_receipt", failed_gateway=failed)),
                         (0, 1, True))
        self.assertEqual(self._receipts(), [])
        self.assertEqual(self._message_row(1).outcome, "deleted")
        self.assertEqual(self._message_row(2).outcome, "skipped_non_receipt")

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

    def test_pending_cap_ignores_evidence_rows(self):
        self.assertEqual(self._commit(self._message(1, invoice=INVOICE)), (1, 0, True))
        self.assertEqual(self._commit(self._message(2, payment=_gateway_payment())), (1, 0, True))
        with self.sessions() as session:
            self.assertEqual(self.store._pending_count(session, "owner"), 1)
        standalone = _gateway_payment(ref=_ref("ref-2"), total="500", fee=None)
        self.assertEqual(self._commit(self._message(3, payment=standalone)), (1, 0, True))
        with self.sessions() as session:
            self.assertEqual(self.store._pending_count(session, "owner"), 2)


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


class V1fReprocessTests(unittest.TestCase):
    def test_reprocess_merges_upgrades_and_deletes_idempotently(self):
        from datetime import UTC, datetime
        import subprocess
        import sys
        import time

        from hestia.budget.gmail_client import GmailClient
        from hestia.budget.gmail_sync import GmailSync
        from hestia.budget.gmail_worker import ReceiptParserWorker
        from tests.test_budget_gmail_sync import FakeGmailTransport, SETTINGS, page, response

        owner = "owner"
        gateway = "@gateway.example.test"
        with TemporaryDirectory() as directory:
            path = Path(directory) / "reprocess.db"
            self.addCleanup(db.reset_engine_cache)
            _engine, sessions = db.init_db(path)
            with sessions.begin() as session:
                session.add_all([db.User(username=owner, password_hash="synthetic", role="admin"),
                                 db.AppMeta(key="users_authoritative", value="1")])
            clock = FakeClock()
            store = GmailStore(sessions, clock=clock.wall)
            settings = replace(SETTINGS, payment_senders=(PaymentSender(gateway, "Example Pay", gateway=True),))

            bodies = {
                "p24-reg": ("p24_registered.txt", "Nowa transakcja płatnicza (P24-ABC-123-DEF)", 10),
                "p24-paid": ("p24_paid.txt", "Potwierdzenie (P24-ABC-123-DEF)", 12),
                "paynow-reg": ("paynow_registered.txt", "Zlecenie płatności", 14),
                "paynow-failed": ("paynow_failed.txt", "Transakcja AB12-CDE-FGH-IJK straciła ważność", 16),
                "paynow-solo": ("paynow_failed_solo.txt", "Transakcja ZZ99-YYY-XXX-WWV straciła ważność", 18),
            }
            transport = FakeGmailTransport()
            transport.routes[("/messages", None)] = response(page(*reversed(list(bodies))))
            for message_id, (fixture, subject, day) in bodies.items():
                body = (_FIXTURES / fixture).read_text(encoding="utf-8")
                message = EmailMessage()
                message["From"] = "receipts@gateway.example.test"
                message["Subject"] = subject
                message["Authentication-Results"] = (
                    "mx.google.com; dkim=pass header.d=gateway.example.test; "
                    "dmarc=pass header.from=gateway.example.test"
                )
                message.set_content(body)
                internal_date = str(int(datetime(2026, 11, day, 1, tzinfo=UTC).timestamp() * 1000))
                payload = message.as_bytes()
                metadata = {"id": message_id, "sizeEstimate": len(payload), "internalDate": internal_date,
                            "payload": {"headers": [{"name": name, "value": value}
                                                    for name, value in message.raw_items()]}}
                raw = {"id": message_id, "raw": base64.urlsafe_b64encode(payload).decode().rstrip("=")}

                def select_response(params, metadata=metadata, raw=raw):
                    return response(metadata if params["format"] == ["metadata"] else raw)

                transport.routes[("/messages/" + message_id, None)] = select_response

            client = GmailClient(transport, monotonic=clock.monotonic, sleep=clock.sleep)
            worker = ReceiptParserWorker(str(Path(directory) / "reprocess.sock"))
            service = GmailSync(settings, store, client, gmail_message.GmailMessageProcessor(settings, worker), path)

            sender_hmac = digest(settings.hmac_secret, PAYMENT_SENDER_DOMAIN, gateway)

            def v1e_payment(total):
                fields = ImportedPayment("SKLEP PRZYKŁADOWY", "Zamówienie", total, "PLN", ())
                return PendingPayment(fields, date(2026, 11, 20), sender_hmac, (), gateway=True)

            with subprocess.Popen((sys.executable, "-B", "-c",
                                   "from hestia.budget.gmail_worker import serve; import sys; serve(sys.argv[1])",
                                   worker.socket_path), env={}, stderr=subprocess.DEVNULL) as sidecar:
                try:
                    for _ in range(50):
                        if worker.ready():
                            break
                        time.sleep(0.01)
                    self.assertTrue(worker.ready())
                    service.transition(owner, 1, "active")
                    # Simulate the v1e imports: same messages, no gateway columns.
                    legacy = {
                        "p24-reg": ("120", "imported"), "p24-paid": ("121.5", "imported"),
                        "paynow-reg": ("75", "imported"), "paynow-failed": ("75", "skipped_duplicate"),
                        "paynow-solo": ("80", "imported"),
                    }
                    items = tuple(
                        PendingMessage(digest(settings.hmac_secret, GMAIL_MESSAGE_DOMAIN, message_id),
                                       clock.now, "direct_allowlist", outcome,
                                       payment=None if outcome == "skipped_duplicate" else v1e_payment(total))
                        for message_id, (total, outcome) in legacy.items()
                    )
                    self.assertEqual(store.commit(owner, 1, {}, items), (4, 1, True))
                    service.schedule_reprocess(owner)
                    outcome = service.sync(owner)
                    self.assertEqual(outcome["sync_phase"], "idle")
                finally:
                    sidecar.terminate()

            with sessions() as session:
                rows = session.scalars(select(db.BudgetReceipt)).all()
                self.assertEqual(len(rows), 1)
                (paid,) = rows
                self.assertEqual((paid.gateway, paid.gateway_status, paid.total_amount, paid.fee_amount,
                                  paid.raw_shop_name),
                                 ("Example Pay", "paid", "121.5", "1.5", "SKLEP PRZYKŁADOWY SP. Z O.O."))
                self.assertIsNotNone(paid.gateway_ref_hmac)
                outcomes = {row.outcome for row in session.scalars(select(db.BudgetGmailMessage))}
                self.assertEqual(outcomes, {"imported", "skipped_duplicate", "deleted"})
                self.assertIsNone(session.get(db.AppMeta, "budget_gmail_reprocess_payments:" + owner))
                audits = session.scalars(select(db.BudgetAnalyticsAudit).where(
                    db.BudgetAnalyticsAudit.action == "gateway_reprocess")).all()
                self.assertEqual(len(audits), 2)

            # A second scheduled run changes nothing.
            clock.now += 61
            with subprocess.Popen((sys.executable, "-B", "-c",
                                   "from hestia.budget.gmail_worker import serve; import sys; serve(sys.argv[1])",
                                   worker.socket_path), env={}, stderr=subprocess.DEVNULL) as sidecar:
                try:
                    for _ in range(50):
                        if worker.ready():
                            break
                        time.sleep(0.01)
                    self.assertTrue(worker.ready())
                    service.schedule_reprocess(owner)
                    service.sync(owner)
                finally:
                    sidecar.terminate()
            with sessions() as session:
                (paid_again,) = session.scalars(select(db.BudgetReceipt)).all()
                self.assertEqual((paid_again.id, paid_again.gateway_status, paid_again.total_amount,
                                  paid_again.version), (paid.id, "paid", "121.5", paid.version))


class V1fMessageTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            sender_allowlist=(), spouse_forward_senders=(), hmac_secret=_SECRET,
            owner_mailbox="owner@gmail.com",
            payment_senders=(PaymentSender("@gateway.example.test", "Example Pay", gateway=True),
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

    def test_registered_payment_carries_status_ref_fee_and_host(self):
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
        body = "Twoja transakcja AB12-CDE-FGH-IJK straciła ważność.\n\nKwota transakcji: 75,00 PLN"
        pending = self._process(body, "Transakcja straciła ważność")
        self.assertEqual((pending.outcome, pending.failed_gateway),
                         ("skipped_non_receipt", ("Example Pay", digest(_SECRET, GATEWAY_REF_DOMAIN, "AB12-CDE-FGH-IJK"))))
        self.assertIsNone(pending.payment)

    def test_failed_payment_without_a_reference_stays_plain(self):
        body = "Twoja transakcja straciła ważność.\n\nKwota transakcji: 75,00 PLN"
        pending = self._process(body, "Transakcja straciła ważność")
        self.assertEqual((pending.outcome, pending.failed_gateway), ("skipped_non_receipt", None))

    def test_card_verification_is_a_plain_rejection(self):
        body = "Rejestracja karty w aplikacji mobilnej.\nTransakacja zostanie anulowana.\n\nKwota transakcji: 0,01 PLN"
        pending = self._process(body, "Rejestracja karty")
        self.assertEqual((pending.outcome, pending.failed_gateway, pending.payment),
                         ("skipped_non_receipt", None, None))

    def test_non_gateway_payment_has_no_gateway_fields(self):
        pending = self._process("Łącznie: 55,52 PLN", "Zamówienie potwierdzone", sender="orders@shop.example.test")
        self.assertEqual(pending.outcome, "imported")
        self.assertEqual((pending.payment.status, pending.payment.gateway_name, pending.payment.gateway_ref_hmac),
                         (None, None, None))
