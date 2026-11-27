"""Synthetic privacy and reconciliation cases for the App Store invoice layout."""
from unittest import TestCase

from hestia.budget.gmail_apple import apple_subscription_lines
from tests.test_budget_gmail_apple import BODY, NAME, TITLE


class AppleItemGuardTests(TestCase):
    def test_subscription_without_net_summary(self):
        body = BODY.replace("Kwota netto", "Nieznana sekcja")
        self.assertEqual(apple_subscription_lines(body, "419.99", "PLN"), ())

    def test_device_owner_in_title_or_plan_keeps_the_summary(self):
        for field, device in (
            (TITLE, "iPhone Właściciela"),
            (TITLE, "iPhone (Właściciela)"),
            ("Roczna", "Pakiet iPhone (Właściciela) (Roczna)"),
        ):
            with self.subTest(field=field, device=device):
                self.assertEqual(apple_subscription_lines(BODY.replace(field, device), "419.99", "PLN"), ())

    def test_iphone_in_an_ordinary_product_title(self):
        body = BODY.replace(TITLE, "Przewodnik na iPhone")
        item, = apple_subscription_lines(body, "419.99", "PLN")
        self.assertEqual(item["raw_name"], "Przewodnik na iPhone · Plan rodzinny · Roczna")

    def test_unaccounted_footer_amount_or_adjustment_keeps_the_summary(self):
        for footer in (
            "Opłata za usługę 5,00 zł",
            "Dostawa\n5,00\nEUR",
            "Zastosowano kupon lojalnościowy",
            "Korekta faktury",
            "Faktura korygująca",
            "Zwrot środków",
            "Zwrot:",
            "Przyznano zwrot za subskrypcję",
        ):
            with self.subTest(footer=footer):
                self.assertEqual(apple_subscription_lines(BODY + footer, "419.99", "PLN"), ())

    def test_nonpriced_returns_help_in_the_footer_preserves_the_item(self):
        footer = ("Dowiedz się, jak poprosić o zwrot środków.\n"
                  "Zwrot lub wymiana   Informacje prawne   Polityka prywatności")
        item, = apple_subscription_lines(BODY + footer, "419.99", "PLN")
        self.assertEqual(item["raw_name"], NAME)
