"""Synthetic Apple subscription invoices through the rendered MIME and storage paths."""
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase, mock

from sqlalchemy import select

from hestia import db
from hestia.budget.gmail_apple import apple_subscription_lines
from hestia.budget.gmail_message import GmailMessageProcessor
from hestia.budget.gmail_store import GmailStore
from hestia.budget.gmail_trust import PaymentSender
from hestia.budget.gmail_worker import WorkerUnavailable, _render, _validate_output
from tests.test_budget_gmail_message import GMAIL_HASH, MESSAGE_ID, OWNER, SECRET, _email, _metadata, _raw


BODY = (Path(__file__).parent / "fixtures/gmail/payment/app_store_invoice.txt").read_text(encoding="utf-8")
TITLE = "Przykładowa Usługa Chmurowa"
NAME = f"{TITLE} · Plan rodzinny · Roczna"
SHOP = "synthetic@email.apple.com"


def _html(body):
    return "<div>" + "</div><div>".join(map(escape, body.splitlines())) + "</div>"


def _process(message, rule=PaymentSender(SHOP, "Example App Store"), *, html_unavailable=False):
    settings = SimpleNamespace(sender_allowlist=(), payment_senders=(rule,), spouse_forward_senders=(),
                               known_merchant_domains=(), require_known_forwarded_merchant=True,
                               owner_mailbox=OWNER, hmac_secret=SECRET)
    worker = mock.Mock()
    def render(kind, payload):
        if html_unavailable and kind == "html":
            raise WorkerUnavailable
        return _validate_output(_render({"html": b"H", "text": b"T"}[kind] + payload))

    worker.parse.side_effect = render
    metadata = _metadata(message)
    metadata["internalDate"] = str(int(datetime(2026, 11, 14, tzinfo=UTC).timestamp() * 1000))
    run = mock.Mock()
    run.message.side_effect = metadata, _raw(message)
    return GmailMessageProcessor(settings, worker).process(run, MESSAGE_ID, GMAIL_HASH)


class AppStoreItemTests(TestCase):
    def test_gross_amounts_multiple_items_and_tax(self):
        body = BODY.replace("419,99 zł", "419,98 zł", 1).replace(
            "Polska", "Syntetyczny Czytnik\nMiesięczna\nOdnowienie: 14 grudnia 2026\n0,01 zł\n"
                      "Zawiera podatek VAT w wysokości 23% (0,00 zł)\nPolska")
        items = apple_subscription_lines(body, "419.99", "PLN")
        self.assertEqual([(line["line_no"], line["raw_name"], line["net_line_total"]) for line in items],
                         [(1, NAME, "419.98"), (2, "Syntetyczny Czytnik · Miesięczna", "0.01")])

    def test_inline_vat_plan_suffix_and_private_surrounding_fields(self):
        body = BODY.replace(TITLE, "Adres: Strona Przykładowa\nprivate@example.test\nCzytnik: Edycja Plus").replace(
            "Plan rodzinny\nRoczna", "Pakiet Plus (Miesięczna)").replace(
            "23%\n78,53 zł", "23% (78,53 zł)", 1).replace("Polska", "Adres sprzedawcy\nPolska")
        items = apple_subscription_lines(body, "419.99", "PLN")
        self.assertEqual([line["raw_name"] for line in items], ["Czytnik: Edycja Plus · Pakiet Plus (Miesięczna)"])
        self.assertEqual(apple_subscription_lines(body.replace("(78,53 zł)", "78,53 zł"), "419.99", "PLN"), items)

    def test_device_label_and_fixed_month_plan(self):
        body = BODY.replace("Plan rodzinny\nRoczna", "Pakiet (3 miesiące)").replace(
            "419,99 zł", "iPhone Właściciela\n419,99 zł", 1)
        item, = apple_subscription_lines(body, "419.99", "PLN")
        self.assertEqual(item["raw_name"], TITLE + " · Pakiet (3 miesiące)")
        self.assertNotIn("Właściciela", repr(item))
        parenthesized = body.replace("iPhone Właściciela", "iPhone (Właściciela)")
        self.assertEqual(apple_subscription_lines(parenthesized, "419.99", "PLN"), (item,))

    def test_conflicting_amounts_sensitive_or_unsupported_fields_keep_summary(self):
        variants = [
            BODY.replace("419,99 zł", "419,98 zł", 1),
            BODY.replace("419,99 zł", "419,99 EUR", 1),
            BODY.replace("341,46 zł", "341,45 zł"),
            BODY.split("Naliczono podatek")[0],
            BODY[:BODY.index("419,99 zł")] + "iPhone Przykładowy\n419,99 zł\n" + BODY[BODY.index("Kwota netto"):],
            BODY.replace("23%\n78,53 zł\n\nPolska", "23%", 1),
            BODY.replace("419,99 zł", "-419,99 zł", 1),
            BODY.replace(TITLE, "klient@example.test"),
            BODY.replace(TITLE, "Adres rozliczeniowy\nJan Kowalski"),
            BODY.replace(TITLE, "Nabywca\nKwiatowa 12"),
            BODY.replace(TITLE, "Usługa 1234 5678 9012"),
            BODY.replace(TITLE, "https://example.test"),
            BODY.replace("Plan rodzinny", "Adres rozliczeniowy"),
            BODY.replace("Roczna", "Rabat"),
            BODY.replace("Odnowienie: 14 listopada 2027", "Adres: Prywatne"),
            BODY.replace("78,53 zł", "78,53 USD", 1),
            BODY.replace("78,53 zł", "0,01 zł", 1),
            BODY.replace("Plan rodzinny", "Rabat 20%"),
            BODY.replace("Polska", "Dostawa 5,00 zł\nPolska"),
            BODY.replace("Polska", "Kupon lojalnościowy\nPolska"),
            BODY.replace("Roczna", "Plan private@example.test (Roczna)"),
        ]
        for body in variants:
            with self.subTest(body=body):
                self.assertEqual(apple_subscription_lines(body, "419.99", "PLN"), ())

    def test_direct_html_import_stores_item_with_existing_line_schema(self):
        pending = _process(_email(SHOP, _html(BODY), subtype="html"))
        self.assertEqual(pending.outcome, "imported")
        self.assertEqual(pending.payment.card_last4, "0000")
        with TemporaryDirectory() as temp:
            self.addCleanup(db.reset_engine_cache)
            _, sessions = db.init_db(Path(temp) / "synthetic.db")
            with sessions.begin() as session:
                session.add(db.User(username="fixture", password_hash="synthetic", role="admin"))
                session.flush()
                receipt_id = GmailStore._payment(session, "fixture", pending.payment, 1)
            with sessions() as session:
                line, = session.scalars(select(db.BudgetReceiptLine).where(
                    db.BudgetReceiptLine.receipt_id == receipt_id))
                self.assertEqual((line.raw_name, line.line_kind, line.quantity, line.unit_price_raw,
                                  line.gross_line_total, line.discount_amount, line.net_line_total),
                                 (NAME, "item", "1", None, "419.99", "0", "419.99"))

    def test_alternative_reconciles_without_combining_duplicates_or_conflicts(self):
        for first, second, expected in (
                ("Razem: 419,99 zł", BODY, NAME), (BODY, BODY, NAME),
                (BODY, "Razem: 419,99 zł", NAME), (BODY, "Potwierdzenie bez danych", None),
                (BODY, BODY.replace(TITLE, "Inna Usługa"), None),
                (BODY, BODY.replace("419,99 zł", "418,99 zł", 1), None),
                (BODY.replace("419,99 zł", "418,99 zł", 1), BODY, None),
                (BODY, BODY.replace("000000000000", "111111111111"), None),
                (BODY, BODY.replace("14 listopada 2026", "15 listopada 2026"), None),
                ("Razem: 418,99 zł", BODY, None),
                ("Razem: 419,99 EUR", BODY, None),
                ("Mastercard •••• 1111\n419,99 zł", BODY, None)):
            with self.subTest(expected=expected):
                message = _email(SHOP, first)
                message.add_alternative(_html(second), subtype="html")
                payment = _process(message).payment
                self.assertEqual([row["raw_name"] for row in payment.items], [expected] if expected else [])

    def test_standalone_plain_and_unavailable_alternative(self):
        message = _email(SHOP, BODY)
        self.assertEqual([row["raw_name"] for row in _process(message).payment.items], [NAME])
        message.add_alternative(_html(BODY), subtype="html")
        pending = _process(message, html_unavailable=True)
        self.assertEqual((pending.outcome, pending.payment.fields.total_amount, pending.payment.items),
                         ("imported", "419.99", ()))

    def test_inline_forward_cuts_the_cover_note_from_both_mime_alternatives(self):
        header = ("---------- Forwarded message ---------\n"
                  f"From: Example App Store <{SHOP}>\nSubject: Faktura\nMessage-ID: <synthetic@apple.com>\n\n")
        for note, original, names in ((BODY, "Razem: 419,99 zł", []), ("Notatka", BODY, [NAME])):
            with self.subTest(names=names):
                message = _email(OWNER, "Notatka\n" + header + "Razem: 419,99 zł")
                message.add_alternative(_html(note + "\n" + header + original), subtype="html")
                pending = _process(message)
                self.assertEqual((pending.trust_path, pending.outcome), ("owner_forward", "imported"))
                self.assertEqual([row["raw_name"] for row in pending.payment.items], names)

    def test_lookalike_and_gateway_cannot_enable_itemization(self):
        spoof = "synthetic@apple.com.example.test"
        for address, rule in ((spoof, PaymentSender(spoof, "Apple")), (SHOP, PaymentSender(SHOP, "Apple", True))):
            with self.subTest(rule=rule):
                pending = _process(_email(address, BODY), rule)
                self.assertEqual(pending.payment.items, ())
