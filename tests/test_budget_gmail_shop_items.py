import unittest

from hestia.budget.gmail_shop_items import extract_shop_items


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
