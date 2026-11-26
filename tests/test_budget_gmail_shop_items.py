"""Synthetic unit and integration tests for Gmail shop order itemization."""
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import select

from hestia import db
from hestia.budget import gmail_message
from hestia.budget.gmail_payment import ImportedPayment, inspect_payment
from hestia.budget.gmail_shop_items import extract_shop_items, inspect_shop_items
from hestia.budget.gmail_store import GmailStore, PendingMessage, PendingPayment
from hestia.budget.gmail_trust import PaymentSender
from hestia.budget.gmail_worker import _render, _validate_output
from hestia.budget.receipt_store import BudgetReceiptStore
from tests.test_budget_gmail_message import GMAIL_HASH, MESSAGE_ID, OWNER, SECRET, _email, _metadata, _raw


ORDER = """Podsumowanie zamówienia
Zamówione produkty
Odbiór osobisty

Kolorowe kredki (8 szt.)
Kod produktu: 31415
Ilość: 2 x
Cena za sztukę: 3,40 zł
2x
6,80 zł

Grafitowe ołówki (4 szt.)
Kod produktu: 27182
Ilość: 3 x
Cena za sztukę: 7,10 zł
3x
21,30 zł

Wartość zamówienia
28,10 zł
Łącznie
28,10 zł
Biuro obsługi
"""

TABLE_ORDER = """Potwierdzenie zamówienia
Rozliczenie zamówienia:
Wartość zamówienia
40,00 zł
Koszt spakowania
4,00 zł
Kaucja
1,00 zł
Kwota do zapłaty
45,00 zł
PRODUKT
ILOŚĆ
CENA
RAZEM
Ryż
2
15,00 zł
30,00 zł
Groch
1
10,00 zł
10,00 zł
OSZCZĘDNOŚCI W TYM ZAMÓWIENIU:
"""

TABLE_ORDER_REALISTIC = """Potwierdzenie zamówienia nr 00000000001
Rozliczenie zamówienia:
Sprzedawca:
Sklep Testowy
Wartość zamówienia
40,00 zł
Koszt spakowania
4,00 zł
Kaucja
1,00 zł
Kwota do zapłaty
45,00 zł
Sposób płatności:
Płatność online
Możliwość edycji zamówienia do 26.11.2026 20:00
Dostawa: Kurier 27.11.2026 10:00-12:00
Zamówione produkty
PRODUKT
ILOŚĆ
CENA
RAZEM
Ryż
2
15,00 zł
30,00 zł
Groch
1
10,00 zł
10,00 zł
OSZCZĘDNOŚCI W TYM ZAMÓWIENIU:
Kupony rabatowe: 0,00 zł
Naliczone promocje: - 1,00 zł
Dostawa: - 2,00 zł
Suma oszczędności: 3,00 zł
Sklep Testowy, ul. Testowa 1, tel. 123 456 789, kontakt@sklep.example.test, www.sklep.example.test
KRS 0000000000, kapitał zakładowy 100 000,00 zł
"""

TABLE_ORDER_HTML = """<!doctype html>
<html><body>
  <p>Dziękujemy za zamówienie nr 00000000001</p>
  <div>Rozliczenie zamówienia:</div>
  <table>
    <tr><td>Sprzedawca:</td><td>Sklep Testowy</td></tr>
    <tr><td>Wartość zamówienia</td><td>40,00&nbsp;zł</td></tr>
    <tr><td>Koszt spakowania</td><td>4,00&nbsp;zł</td></tr>
    <tr><td>Kaucja</td><td>1,00&nbsp;zł</td></tr>
    <tr><td>Kwota do zapłaty</td><td>45,00&nbsp;zł</td></tr>
    <tr><td>Sposób płatności:</td><td>Płatność online</td></tr>
  </table>
  <p>Możliwość edycji do 26.11.2026 20:00</p>
  <p>Dostawa: 27.11.2026 10:00-12:00</p>
  <h4>Zamówione produkty</h4>
  <table>
    <tr><th>PRODUKT</th><th>ILOŚĆ</th><th>CENA</th><th>RAZEM</th></tr>
    <tr><td>Ryż</td><td>2</td><td>15,00&nbsp;zł</td><td>30,00&nbsp;zł</td></tr>
    <tr><td>Groch</td><td>1</td><td>10,00&nbsp;zł</td><td>10,00&nbsp;zł</td></tr>
  </table>
  <div>OSZCZĘDNOŚCI W TYM ZAMÓWIENIU:</div>
  <table>
    <tr><td>Kupony rabatowe:</td><td>0,00&nbsp;zł</td></tr>
    <tr><td>Naliczone promocje:</td><td>- 1,00&nbsp;zł</td></tr>
    <tr><td>Dostawa:</td><td>- 2,00&nbsp;zł</td></tr>
    <tr><td>Suma oszczędności:</td><td>3,00&nbsp;zł</td></tr>
  </table>
  <div>Kontakt: kontakt@sklep.example.test, tel. 123 456 789</div>
  <div>Zwrot lub wymiana   Informacje prawne</div>
</body></html>"""


def _render_html(html: str) -> str:
    return _validate_output(_render(b"H" + html.encode("utf-8")))


class ShopItemTests(unittest.TestCase):
    def test_package_size_order_quantity_unit_price_and_total_are_distinct(self):
        lines = extract_shop_items(ORDER, "28.1", "PLN")
        self.assertEqual([(r["line_no"], r["raw_name"], r["quantity"], r["raw_unit"],
                           r["unit_price_raw"], r["net_line_total"]) for r in lines],
                         [(1, "Kolorowe kredki (8 szt.)", "2", "szt.", "3.4", "6.8"),
                          (2, "Grafitowe ołówki (4 szt.)", "3", "szt.", "7.1", "21.3")])
        self.assertTrue(all(r["line_kind"] == "item" and r["discount_amount"] == "0"
                            and r["gross_line_total"] == r["net_line_total"] for r in lines))
        self.assertNotIn("31415", str(lines))
        self.assertEqual(extract_shop_items(ORDER.replace(",", ".").replace("zł", "PLN"),
                                            "28.10", "PLN"), lines)

    def test_rejects_incomplete_contaminated_and_ambiguous_layouts(self):
        replacements = [
            ("Zamówione produkty", ""), ("Zamówione produkty", "Produkty\nProdukty"),
            ("Wartość zamówienia", "Wartość zamówienia\nWartość zamówienia"),
            ("Łącznie", "Łącznie\nŁącznie"), ("Łącznie", "Wysyłka\nŁącznie"),
            ("Łącznie\n28,10 zł\nBiuro obsługi\n", "Łącznie"),
            ("28,10 zł\nŁącznie", "28,20 zł\nŁącznie"),
            ("Łącznie\n28,10 zł", "Łącznie\n28,10 EUR"),
            ("Kod produktu:", "SKU:"), ("Kod produktu: 27182", "SKU: 27182"),
            ("Zamówione produkty", "Kod produktu: 1\nZamówione produkty"),
            ("Wartość zamówienia", "Ostatnia pozycja\nWartość zamówienia"),
            ("Odbiór osobisty", "Opłata administracyjna"), ("Biuro obsługi", "Bonus: 9,99 zł"),
            ("Grafitowe ołówki", "Adres docelowy"), ("Grafitowe ołówki", "ul. Fikcyjna"), ("Kolorowe kredki", "nazwa@example.test"),
            ("Grafitowe ołówki", "A 123 456 789"), ("Grafitowe ołówki", "www.example.test"),
            ("Grafitowe ołówki", "Jan ID123456"), ("Grafitowe ołówki", "Materiał_SKU"),
            ("(8 szt.)", "(8 kartonów)"), ("Ilość: 2 x", "Ilość: 1/2 x"),
            ("Ilość: 2 x", "Ilość: 0 x"), ("\n2x\n", "\n2 opakowania\n"),
            ("\n2x\n", "\n3x\n"), ("Cena za sztukę: 3,40 zł", "Cena za sztukę: 3,40 EUR"),
            ("Cena za sztukę: 3,40 zł", "Cena za sztukę: -3,40 zł"),
            ("6,80 zł", "6,81 zł"), ("6,80 zł", "6,80 PLN netto"),
        ]
        for before, after in replacements:
            with self.subTest(before=before, after=after):
                self.assertEqual(extract_shop_items(ORDER.replace(before, after), "28.1", "PLN"), ())

    def test_refuses_unsupported_currency_invalid_total_and_unreconciled_lines(self):
        for text, total, currency in (
                (ORDER, "28.1", "EUR"), (None, "28.1", "PLN"), ("é" * 20000, "28.1", "PLN"),
                (ORDER, "28.11", "PLN"), (ORDER, "bad", "PLN"),
                (ORDER.replace("28,10 zł", "27,10 zł"), "27.1", "PLN")):
            with self.subTest(total=total, currency=currency):
                self.assertEqual(extract_shop_items(text, total, currency), ())


class MerchantProductTableTests(unittest.TestCase):
    def test_extracts_rows_with_packing_and_deposit_without_leaking_outside_text(self):
        expected = [
            (1, "item", "Ryż", "2", None, "15", "30"),
            (2, "item", "Groch", "1", None, "10", "10"),
            (3, "other", "Koszt spakowania", "1", None, None, "4"),
            (4, "deposit", "Kaucja", "1", None, None, "1"),
        ]
        for source_name, text in (
                ("plain", TABLE_ORDER),
                ("realistic_plain", TABLE_ORDER_REALISTIC),
                ("html", _render_html(TABLE_ORDER_HTML)),
                ("pln_dot", TABLE_ORDER.replace(",", ".").replace("zł", "PLN")),
                ("inline_summary", (
                    "Rozliczenie zamówienia: Wartość zamówienia: 40,00 zł\n"
                    "Koszt spakowania 4,00 zł\nKaucja: 1,00 zł\nKwota do zapłaty: 45,00 zł\n"
                    "PRODUKT\nILOŚĆ\nCENA\nRAZEM\n"
                    "Ryż\n2\n15,00 zł\n30,00 zł\nGroch\n1\n10,00 zł\n10,00 zł\n"
                    "OSZCZĘDNOŚCI W TYM ZAMÓWIENIU: 0,00 zł\nBiuro obsługi"
                )),
                ("bare_savings_amount", TABLE_ORDER + "0,00 zł\n"),
        ):
            with self.subTest(source=source_name):
                lines = inspect_shop_items(text, "45.00", "PLN")
                self.assertIsNotNone(lines)
                self.assertEqual(
                    [(r["line_no"], r["line_kind"], r["raw_name"], r["quantity"],
                      r["raw_unit"], r["unit_price_raw"], r["net_line_total"]) for r in lines],
                    expected,
                )
                self.assertTrue(all(r["discount_amount"] == "0"
                                    and r["gross_line_total"] == r["net_line_total"] for r in lines))
                serialized = repr(lines)
                for outside in ("Sklep Testowy", "Płatność online", "26.11.2026", "Kurier",
                                "kontakt@", "123 456 789", "www.", "Kupony", "Naliczone", "Dostawa"):
                    self.assertNotIn(outside, serialized)

    def test_package_digit_names_units_thousands_and_optional_fees(self):
        package_names = (
            "Rozliczenie zamówienia:\nWartość zamówienia 40,00 zł\nKwota do zapłaty 40,00 zł\n"
            "PRODUKT\nILOŚĆ\nCENA\nRAZEM\n"
            "Sok 1800 ml 2 x 25 g\n2 x\n15,00 zł\n30,00 zł\n"
            "Kawa 2 x 100 g 12 szt.\n1 szt.\n10,00 zł\n10,00 zł\n"
            "OSZCZĘDNOŚCI W TYM ZAMÓWIENIU:\n"
        )
        lines = extract_shop_items(package_names, "40", "PLN")
        self.assertEqual(
            [(r["line_kind"], r["raw_name"], r["quantity"], r["raw_unit"], r["net_line_total"]) for r in lines],
            [("item", "Sok 1800 ml 2 x 25 g", "2", None, "30"),
             ("item", "Kawa 2 x 100 g 12 szt.", "1", "szt.", "10")],
        )

        zero_fees = (
            "Rozliczenie zamówienia:\nWartość zamówienia 40,00 zł\nKoszt spakowania 0,00 zł\n"
            "Kaucja 0 zł\nKwota do zapłaty 40,00 zł\n"
            "PRODUKT\nILOŚĆ\nCENA\nRAZEM\n"
            "Ryż\n2\n15,00 zł\n30,00 zł\n"
            "Groch\n1\n10,00 zł\n10,00 zł\n"
            "OSZCZĘDNOŚCI W TYM ZAMÓWIENIU:\n"
        )
        self.assertEqual(
            [(r["line_kind"], r["raw_name"], r["net_line_total"]) for r in extract_shop_items(zero_fees, "40", "PLN")],
            [("item", "Ryż", "30"), ("item", "Groch", "10")],
        )

        packing_only = TABLE_ORDER.replace("Kaucja\n1,00 zł\n", "").replace("45,00 zł", "44,00 zł")
        self.assertEqual(
            [(r["line_kind"], r["raw_name"], r["net_line_total"]) for r in extract_shop_items(packing_only, "44", "PLN")],
            [("item", "Ryż", "30"), ("item", "Groch", "10"), ("other", "Koszt spakowania", "4")],
        )

        deposit_only = TABLE_ORDER.replace("Koszt spakowania\n4,00 zł\n", "").replace("45,00 zł", "41,00 zł")
        self.assertEqual(
            [(r["line_kind"], r["raw_name"], r["net_line_total"]) for r in extract_shop_items(deposit_only, "41", "PLN")],
            [("item", "Ryż", "30"), ("item", "Groch", "10"), ("deposit", "Kaucja", "1")],
        )

        decimal_and_thousands = (
            "Rozliczenie zamówienia:\nWartość zamówienia 1 005,00 zł\nKwota do zapłaty 1 005,00 zł\n"
            "PRODUKT\nILOŚĆ\nCENA\nRAZEM\n"
            "Bonbonierka 500 g\n0,5 kg\n10,00 zł\n5,00 zł\n"
            "Sok 100% 1 l\n1 op.\n1 000,00 zł\n1 000,00 zł\n"
            "OSZCZĘDNOŚCI W TYM ZAMÓWIENIU:\n"
        )
        self.assertEqual(
            [(r["raw_name"], r["quantity"], r["raw_unit"], r["unit_price_raw"], r["net_line_total"])
             for r in extract_shop_items(decimal_and_thousands, "1005", "PLN")],
            [("Bonbonierka 500 g", "0.5", "kg", "10", "5"),
             ("Sok 100% 1 l", "1", "op.", "1000", "1000")],
        )

    def test_unrelated_formats_return_none(self):
        fixtures = Path(__file__).parent / "fixtures" / "gmail" / "payment"
        for text in (
                "Łącznie: 45,00 zł",
                (fixtures / "shop_order.txt").read_text(encoding="utf-8"),
                (fixtures / "sports_shop_html_order.txt").read_text(encoding="utf-8"),
                (fixtures / "subscription_eur.txt").read_text(encoding="utf-8"),
                "x" * 40000,
                "é" * 20000,
                None,
        ):
            with self.subTest(text=str(text)[:40]):
                self.assertIsNone(inspect_shop_items(text, "45", "PLN"))

    def test_rejects_accounting_currency_structure_and_privacy_failures(self):
        cases = [
            # Subtotal and grand-total accounting disagreements (never invent balancing rows)
            (TABLE_ORDER.replace("30,00 zł", "31,00 zł"), "45"),
            (TABLE_ORDER.replace("10,00 zł\n10,00 zł", "10,00 zł\n11,00 zł"), "45"),
            (TABLE_ORDER.replace("40,00 zł", "41,00 zł").replace("4,00 zł", "3,00 zł"), "45"),
            (TABLE_ORDER.replace("40,00 zł", "39,00 zł", 1), "45"),
            (TABLE_ORDER.replace("45,00 zł", "44,00 zł"), "44"),
            (TABLE_ORDER.replace("Kaucja\n1,00 zł\n", ""), "45"),
            (TABLE_ORDER, "40"),
            (TABLE_ORDER, "46"),
            (TABLE_ORDER, "0"),
            (TABLE_ORDER, "bad"),
            # Currency ambiguity and negative / invalid amounts
            (TABLE_ORDER.replace("1,00 zł", "1,00 EUR"), "45"),
            (TABLE_ORDER.replace("15,00 zł", "15,00 EUR"), "45"),
            (TABLE_ORDER.replace("30,00 zł", "30,00 PLN EUR"), "45"),
            (TABLE_ORDER.replace("40,00 zł", "40,00"), "45"),
            (TABLE_ORDER.replace("40,00 zł", "40,00 zł netto"), "45"),
            (TABLE_ORDER.replace("4,00 zł", "-4,00 zł"), "45"),
            (TABLE_ORDER.replace("15,00 zł", "-15,00 zł"), "45"),
            (TABLE_ORDER.replace("45,00 zł", "1 000 001,00 zł"), "1000001"),
            (TABLE_ORDER + "5,00 EUR\n", "45"),
            (TABLE_ORDER + "-5,00 zł\n", "45"),
            (TABLE_ORDER_REALISTIC.replace("3,00 zł", "3,00 EUR"), "45"),
            # Unaccounted components, savings inconsistencies, and outside-table contamination
            (TABLE_ORDER.replace("Kwota do zapłaty", "Koszt dostawy\n5,00 zł\nKwota do zapłaty"), "45"),
            (TABLE_ORDER.replace("Kwota do zapłaty", "Rabat\n5,00 zł\nKwota do zapłaty"), "45"),
            (TABLE_ORDER.replace("Kwota do zapłaty", "Dostawa\n5,00 zł\nKwota do zapłaty"), "45"),
            ("Opłata administracyjna\n" + TABLE_ORDER, "45"),
            ("Bonus: 9,99 zł\n" + TABLE_ORDER, "45"),
            (TABLE_ORDER + "Bonus: 9,99 zł\n", "45"),
            (TABLE_ORDER + "Dostawa: 5,00 zł\n", "45"),
            (TABLE_ORDER + "Naliczone promocje: - 1,00 zł\n", "45"),
            (TABLE_ORDER_REALISTIC.replace("Suma oszczędności: 3,00 zł", "Suma oszczędności: 4,00 zł"), "45"),
            (TABLE_ORDER_REALISTIC.replace("Suma oszczędności: 3,00 zł", "Suma oszczędności:"), "45"),
            (TABLE_ORDER_REALISTIC.replace("Dostawa: - 2,00 zł", "Dostawa: - 2,00 zł\nDostawa: - 2,00 zł"), "45"),
            (TABLE_ORDER + "0,00 zł\n1,00 zł\n", "45"),
            (TABLE_ORDER + "Rabat lojalnościowy\n", "45"),
            (TABLE_ORDER + "Zwrot środków\n", "45"),
            (TABLE_ORDER + "Chleb\n1\n5,00 zł\n5,00 zł\n", "50"),
            ("Rozliczenie zamówienia:\nWartość zamówienia 40,00 zł\nKwota do zapłaty 40,00 zł\n" + TABLE_ORDER, "45"),
            (TABLE_ORDER + "OSZCZĘDNOŚCI W TYM ZAMÓWIENIU:\n", "45"),
            # Structural errors and summary/table ordering
            (TABLE_ORDER.replace("Rozliczenie zamówienia:\n", ""), "45"),
            (TABLE_ORDER.replace("Rozliczenie zamówienia:", "Rozliczenie zamówienia: bzdura"), "45"),
            (TABLE_ORDER.replace("Wartość zamówienia\n40,00 zł\n", ""), "45"),
            (TABLE_ORDER.replace("Kwota do zapłaty\n45,00 zł\n", ""), "45"),
            (TABLE_ORDER.replace("Kwota do zapłaty\n45,00 zł", "Kwota do zapłaty"), "45"),
            (TABLE_ORDER.replace("Kaucja\n1,00 zł", "Kaucja\nKaucja\n1,00 zł"), "45"),
            (TABLE_ORDER.replace("Kaucja\n1,00 zł", "Kaucja\n1,00 zł\nKaucja\n1,00 zł"), "45"),
            (TABLE_ORDER.replace("PRODUKT\nILOŚĆ\nCENA\nRAZEM", "PRODUKT\nCENA\nILOŚĆ\nRAZEM"), "45"),
            (TABLE_ORDER.replace("PRODUKT\nILOŚĆ\nCENA\nRAZEM", "PRODUKT\nILOŚĆ\nCENA"), "45"),
            (TABLE_ORDER.replace("OSZCZĘDNOŚCI W TYM ZAMÓWIENIU:\n", ""), "45"),
            (TABLE_ORDER.replace("OSZCZĘDNOŚCI W TYM ZAMÓWIENIU:", "OSZCZĘDNOŚCI W TYM ZAMÓWIENIU: tekst"), "45"),
            (TABLE_ORDER.replace("Ryż\n2\n15,00 zł\n30,00 zł\nGroch\n1\n10,00 zł\n10,00 zł\n", ""), "45"),
            (TABLE_ORDER.replace("Groch\n1\n10,00 zł\n10,00 zł\n", "Groch\n1\n10,00 zł\n"), "45"),
            ("PRODUKT\nILOŚĆ\nCENA\nRAZEM\nRyż\n2\n15,00 zł\n30,00 zł\n"
             "OSZCZĘDNOŚCI W TYM ZAMÓWIENIU:\nRozliczenie zamówienia:\n"
             "Wartość zamówienia\n30,00 zł\nKwota do zapłaty\n30,00 zł\n", "30"),
            (TABLE_ORDER.replace("Groch", "Kod produktu: 1\nGroch"), "45"),
            # Row cell validation and PII/privacy protection
            (TABLE_ORDER.replace("Ryż", "Adres docelowy"), "45"),
            (TABLE_ORDER.replace("Ryż", "ul. Fikcyjna"), "45"),
            (TABLE_ORDER.replace("Ryż", "nazwa@example.test"), "45"),
            (TABLE_ORDER.replace("Ryż", "www.example.test"), "45"),
            (TABLE_ORDER.replace("Ryż", "A 123 456 789"), "45"),
            (TABLE_ORDER.replace("Ryż", "Jan ID123456"), "45"),
            (TABLE_ORDER.replace("Ryż", "Materiał_SKU"), "45"),
            (TABLE_ORDER.replace("Ryż", "Kaucja"), "45"),
            (TABLE_ORDER.replace("Ryż", "1234"), "45"),
            (TABLE_ORDER.replace("Ryż", "Produkt 4111 1111 1111 1111"), "45"),
            (TABLE_ORDER.replace("\n2\n", "\n0\n"), "45"),
            (TABLE_ORDER.replace("\n2\n", "\n1/2\n"), "45"),
            (TABLE_ORDER.replace("\n2\n", "\n2 kartony\n"), "45"),
        ]
        for text, total in cases:
            with self.subTest(text=text[:60], total=total):
                self.assertEqual(inspect_shop_items(text, total, "PLN"), ())
                self.assertEqual(extract_shop_items(text, total, "PLN"), ())
        self.assertEqual(inspect_shop_items(TABLE_ORDER, "45", "EUR"), ())


class MerchantProductTableIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(db.reset_engine_cache)
        _, self.sessions = db.init_db(Path(self.tmp.name) / "shop-table.db")
        with self.sessions.begin() as session:
            session.add(db.User(username=OWNER, password_hash="synthetic", role="admin"))
            session.add(db.AppMeta(key="users_authoritative", value="1"))
        self.store = GmailStore(self.sessions, clock=lambda: 1795566945.0)
        self.store.transition(OWNER, 1, "active")

    def _processor(self):
        settings = SimpleNamespace(
            sender_allowlist=(),
            payment_senders=(PaymentSender("@shop.example.test", "Example Shop"),),
            spouse_forward_senders=(),
            known_merchant_domains=("shop.example.test",),
            require_known_forwarded_merchant=True,
            owner_mailbox=OWNER,
            hmac_secret=SECRET,
        )
        worker = Mock()
        worker.ready.return_value = True
        worker.parse.side_effect = lambda kind, payload: _validate_output(
            _render({"html": b"H", "text": b"T"}[kind] + payload)
        )
        return gmail_message.GmailMessageProcessor(settings, worker), worker

    def test_persists_item_packing_and_deposit_rows_in_sqlite_without_raw_body(self):
        lines = extract_shop_items(_render_html(TABLE_ORDER_HTML), "45", "PLN")
        fields = ImportedPayment("Example Shop", "Zamówienie 00000000001", "45", "PLN", ())
        payment = PendingPayment(fields, date(2026, 11, 26), "b" * 64, items=lines)
        pending = PendingMessage("c" * 64, 1795566945.0, "direct_allowlist", "imported", payment=payment)
        self.assertEqual(self.store.commit(OWNER, 1, {}, (pending,)), (1, 0, True))

        receipts = BudgetReceiptStore(self.sessions, clock=lambda: 1795566945.0, cursor_secret=b"synthetic")
        summary, = receipts.list(OWNER, {"document_kind": "payment_confirmation"})["receipts"]
        detail = receipts.detail(OWNER, summary["receipt_id"])["receipt"]
        self.assertEqual(detail["total_amount"], "45")
        self.assertEqual(
            [(line["line_no"], line["line_kind"], line["raw_name"], line["quantity"],
              line["unit_price_raw"], line["net_line_total"]) for line in detail["lines"]],
            [
                (1, "item", "Ryż", "2", "15", "30"),
                (2, "item", "Groch", "1", "10", "10"),
                (3, "other", "Koszt spakowania", "1", None, "4"),
                (4, "deposit", "Kaucja", "1", None, "1"),
            ],
        )
        with self.sessions() as session:
            stored = session.scalars(select(db.BudgetReceipt)).one()
            self.assertIsNone(stored.raw_ocr_text)

    def test_message_processor_with_explicit_expected_total_and_safe_fallback_before_payment_fix(self):
        processor, _worker = self._processor()

        def process(message):
            run = Mock()
            metadata = _metadata(message)
            metadata["internalDate"] = str(int(datetime(2026, 11, 26, tzinfo=UTC).timestamp() * 1000))
            run.message.side_effect = metadata, _raw(message)
            return processor.process(run, MESSAGE_ID, GMAIL_HASH)

        # Before gmail_payment.py picks Kwota do zapłaty (45), total=40 fails shop reconciliation cleanly.
        unpatched = _email("orders@shop.example.test", TABLE_ORDER_HTML, subtype="html")
        pending_unpatched = process(unpatched)
        self.assertEqual((pending_unpatched.outcome, pending_unpatched.payment.fields.total_amount,
                          pending_unpatched.payment.items), ("imported", "40", ()))

        # With expected grand total 45 supplied explicitly by payment extraction, HTML and alternatives reconcile.
        def fake_inspect_payment(text, *, subjects, merchant, gateway, provider=None):
            inspected = inspect_payment(text, subjects=subjects, merchant=merchant, gateway=gateway, provider=provider)
            if inspected.payment is None:
                return inspected
            return replace(inspected, payment=replace(inspected.payment, total_amount="45"))

        plain_and_html = _email("orders@shop.example.test", TABLE_ORDER_REALISTIC)
        plain_and_html.add_alternative(TABLE_ORDER_HTML, subtype="html")
        plain_and_summary_html = _email("orders@shop.example.test", TABLE_ORDER_REALISTIC)
        plain_and_summary_html.add_alternative("<div>Łącznie: 45,00 zł</div>", subtype="html")
        plain_and_conflicting_html = _email("orders@shop.example.test", TABLE_ORDER_REALISTIC)
        plain_and_conflicting_html.add_alternative(TABLE_ORDER_HTML.replace("Groch", "Fasola"), subtype="html")
        forwarded = _email(OWNER, "Notatka")
        forwarded.add_attachment(_email("orders@shop.example.test", TABLE_ORDER_HTML, subtype="html"))

        with patch.object(gmail_message, "inspect_payment", side_effect=fake_inspect_payment):
            for message, expected_count in (
                    (unpatched, 4),
                    (plain_and_html, 4),
                    (plain_and_summary_html, 4),
                    (plain_and_conflicting_html, 0),
                    (forwarded, 4),
            ):
                with self.subTest(expected_count=expected_count, multipart=message.is_multipart()):
                    pending = process(message)
                    self.assertEqual((pending.outcome, pending.payment.fields.total_amount,
                                      len(pending.payment.items)), ("imported", "45", expected_count))
