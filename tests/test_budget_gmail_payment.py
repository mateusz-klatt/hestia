"""Synthetic payment confirmations and extractor boundaries."""
from datetime import date
from pathlib import Path
import unittest

from hestia.budget.gmail_payment import ImportedPayment, extract_payment
from hestia.budget.receipt_parser import MAX_LINE_CHARS, MAX_TEXT_BYTES, MAX_TEXT_LINES


_FIXTURES = Path(__file__).parent / "fixtures" / "gmail" / "payment"
_MERCHANT = "Example Merchant"


def _extract(text, subject="Example payment", *, gateway=False, provider=None):
    return extract_payment(text, subject=subject, merchant=_MERCHANT, gateway=gateway, provider=provider)


class PaymentFixtureTests(unittest.TestCase):
    def test_fixture_table(self):
        cases = (
            ("gateway_p24_style", "Nowa transakcja płatnicza (P24-AAA-BBB-CCC)", True, ImportedPayment(
                "PRZYKŁADOWY SKLEP SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ", "Zamówienie 100200300", "217", "PLN", (),
                "100200300", fee_amount="0",
            )),
            ("gateway_tpay_style", "Potwierdzenie transakcji TR-AAAA-0000000", True, ImportedPayment(
                "PRZYKŁADOWA SPÓŁKA AKCYJNA", "Zamówienie 0000000001", "83.84", "PLN", (), "0000000001",
                fee_amount="0",
            )),
            # v1f: the Autopay registration notice imports as a registered payment.
            ("gateway_started", "Potwierdzenie rozpoczęcia transakcji dla - https://sklep.example.test/", True,
             ImportedPayment(
                 _MERCHANT, "Potwierdzenie rozpoczęcia transakcji dla -", "146.37", "PLN", (),
                 payee_from_config=True, fee_amount="0", payee_host="sklep.example.test",
             )),
            ("gateway_unfinished", "Dokończ płatność", True, None),
            ("food_order", "Zamówienie potwierdzone", False, ImportedPayment(
                _MERCHANT, "Przykładowa Restauracja", "55.52", "PLN", (),
            )),
            ("app_store_invoice", "Twoja faktura z firmy Example", False, ImportedPayment(
                _MERCHANT, "Twoja faktura z firmy Example", "419.99", "PLN", (date(2026, 11, 14),),
            )),
            ("stripe_receipt", "Your Example Labs receipt [#0000-0000]", False, ImportedPayment(
                _MERCHANT, "Your Example Labs receipt [#0000-0000]", "399.66", "PLN", (date(2026, 11, 14),),
            )),
            ("stripe_receipt_usd", "Your receipt from Example Cloud, Inc. #0000-0000-0000", False, ImportedPayment(
                _MERCHANT, "Płatność", "6.15", "USD", (date(2026, 8, 17),),
            )),
            ("subscription_eur", "Potwierdzenie zamówienia z 13 lis 2026", False, ImportedPayment(
                _MERCHANT, "Potwierdzenie zamówienia z 13 lis 2026", "99.99", "EUR", (),
            )),
            ("payment_failed", "Important: Your Example Family subscription payment failed", False, None),
            ("pickup_notice", "Twoje zamówienie nr 00000000000000 czeka na odbiór w salonie", False, None),
            ("shop_order", "Dziękujemy za zamówienie nr 00000000001", False, ImportedPayment(
                _MERCHANT, "Dziękujemy za zamówienie nr", "83.84", "PLN", (), "00000000001",
            )),
            ("newsletter_with_prices", "Nowości w tym tygodniu", False, None),
            ("ride_inline_tags", "Przejazd opłacony", False, ImportedPayment(
                _MERCHANT, "Przejazd opłacony", "42.35", "PLN", (),
            )),
            ("gateway_fee_inclusive", "Potwierdzenie płatności", True, ImportedPayment(
                _MERCHANT, "Potwierdzenie płatności", "214.7", "PLN", (), payee_from_config=True, fee_amount="2.36",
            )),
            ("gateway_start_date_field", "Płatność zakończona", True, ImportedPayment(
                _MERCHANT, "Przykładowy zakup", "38.12", "PLN", (), payee_from_config=True,
            )),
            ("gateway_registered", "Zarejestrowaliśmy Twoje zlecenie płatnicze", True, ImportedPayment(
                "Example Studio", "Materiały do projektu", "47.25", "PLN", (), payee_host="studio.example.test",
            )),
            ("gateway_completed", "Przekazaliśmy Twoje pieniądze", True, ImportedPayment(
                "Example Studio", "Materiały do projektu", "47.25", "PLN", (),
            )),
            *(("saas_gateway_receipt", subject, True, ImportedPayment("Example Workshop", subject, "8.27", "USD", ()))
              for subject in ("Your receipt from Example Workshop #1234-5678", "Your Example Workshop receipt [#1234-5678]",
                              "Twoje pokwitowanie od Example Workshop nr 1234-5678")),
            *(("shop_order_updates", subject, False,
               ImportedPayment(_MERCHANT, "Materiały warsztatowe", "67.18", "PLN", (), reference))
              for subject, reference in (("Nowe zamówienie nr 00000000001", "00000000001"),
                                         ("Zamówienie opłacone", None),
                                         ("Zamówienie nr 00000000002", "00000000002"))),
            ("food_description", "Potwierdzenie zamówienia nr 00000000001", False, ImportedPayment(
                _MERCHANT, "Przykładowa Restauracja", "26.42", "PLN", (), "00000000001",
            )),
            ("saas_gateway_charged", "Receipt #1111-2222", True, ImportedPayment(
                "Example Studio", "Receipt #1111-2222", "96.77", "PLN", (date(2026, 11, 5),),
                payee_host="invoice.example.test",
            )),
            ("saas_gateway_long_url", "Receipt #3333-4444", True, ImportedPayment(
                "Example Render Studio", "Receipt #3333-4444", "66.19", "PLN", (date(2026, 12, 8),),
                payee_host="billing.example.test",
            )),
            ("sports_shop_html_order", "Potwierdzenie zamówienia nr 00000000003", False, ImportedPayment(
                _MERCHANT, "Potwierdzenie zamówienia nr", "248.36", "PLN", (), "00000000003",
            )),
            ("sports_shop_payment_footer", "Zaksięgowaliśmy płatność za Twoje zamówienie", False, ImportedPayment(
                _MERCHANT, "Zaksięgowaliśmy płatność za Twoje zamówienie", "369.98", "PLN", (),
            )),
            ("zero_dual_currency", "Example invoice notice", False, None),
        )
        self.assertEqual({path.stem for path in _FIXTURES.glob("*.txt")}, {case[0] for case in cases})
        for fixture, subject, gateway, expected in cases:
            with self.subTest(fixture=fixture):
                body = (_FIXTURES / (fixture + ".txt")).read_text(encoding="utf-8")
                provider = "autopay" if fixture == "gateway_started" else None
                self.assertEqual(_extract(body, subject, gateway=gateway, provider=provider), expected)


class PaymentExtractionTests(unittest.TestCase):
    def test_encoded_letters_in_receipt_descriptions(self):
        for encoded in ("&oacute;", "&amp;oacute;", "&#243;", "&#xf3;"):
            subject = f"Status zam{encoded}wienia"
            self.assertEqual(_extract("Total 3 PLN", subject).description, "Status zamówienia")
            self.assertEqual(_extract(f"Tytuł: {subject}\nTotal 3 PLN").description, "Status zamówienia")
        literal = "Plan A & B &lt; &amp; &unknown; &#64;"
        self.assertEqual(_extract("Total 3 PLN", literal).description, literal)

    def test_amount_currency_position_grouping_and_bounds(self):
        for value, amount, currency in (
            ("1\u00a0234,56 zł brutto", "1234.56", "PLN"),
            ("123\u202f456.75 USD", "123456.75", "USD"),
            ("1\u202f234 567 USD", None, None),
            ("EUR 1.023,04", "1023.04", "EUR"),
            ("GBP 12", "12", "GBP"),
            ("£ 1,023.04", "1023.04", "GBP"),
            ("1 023 €", "1023", "EUR"),
            ("1,023 $", "1023", "USD"),
            ("pln 1.000.000", "1000000", "PLN"),
            ("$1 000.00 miesięcznie", "1000", "USD"),
            ("0.01 USD", "0.01", "USD"),
            ("0 PLN", None, None),
            ("1 000 000,01 PLN", None, None),
            ("$1.4", None, None),
            ("$1.2345", None, None),
            ("€ 1 234 5", None, None),
            ("1,234,56 PLN", None, None),
            ("1,234.567 GBP", None, None),
            ("01.00 CAD", None, None),
            ("7.00 PLNxyz", None, None),
            ("$1 extra", "1", "USD"),
            ("$1.00 23", None, None),
        ):
            with self.subTest(value=value):
                actual = _extract(f"ToTal: {value}")
                expected = ImportedPayment(_MERCHANT, "Example payment", amount, currency, ()) if amount else None
                self.assertEqual(actual, expected)

    def test_every_total_label_and_priority(self):
        highest = ("Amount paid", "Kwota zapłacona", "Zapłacono łącznie", "Zapłacono", "Paid")
        middle = ("Do zapłaty", "Łącznie", "Razem do zapłaty", "Suma do zapłaty",
                  "Total", "Cena z dostawą")
        lowest = ("Razem", "Suma", "Kwota transakcji", "Kwota zamówienia", "Kwota")
        for labels, lower in ((highest, "Total 2 PLN"), (middle, "Razem 2 PLN"), (lowest, "xxxx 0000\n2 PLN")):
            for label in labels:
                for separator in (" ", ": ", "\n\n"):
                    with self.subTest(label=label, separator=separator):
                        actual = _extract(f"{label}{separator}PLN 3.10\n{lower}")
                        self.assertEqual(actual.total_amount, "3.1")
        self.assertEqual(_extract("Paid by card\n3 PLN\nTotal 2 PLN").total_amount, "2")
        self.assertEqual(_extract("Amount paid:\n\nEUR 2\nPaid €2.00").total_amount, "2")
        self.assertIsNone(_extract("PaidSomething 1 PLN\nCost Total 2 PLN"))
        self.assertIsNone(_extract("Offers Total 2 PLN Amount paid 2 PLN"))

    def test_disagreement_and_negative_values_reject(self):
        for body in (
            "Amount paid 2 PLN\nPaid 2 USD",
            "Total 2 PLN\nDo zapłaty 3 PLN",
            "Amount paid 5 PLN\nKwota -1 PLN",
            "Amount paid 5 PLN\nTotal EUR −1.02",
            "Amount paid 5 PLN\n- 0000\n-$3",
            "Total -0 PLN",
            "Paid 0 PLN\nTotal 2 PLN",
        ):
            with self.subTest(body=body):
                self.assertIsNone(_extract(body))

    def test_only_amount_directly_after_a_masked_card_is_a_card_charge(self):
        for card in ("•••• 0000", "**** 0000", "- 0000", "Card XXXX 0000"):
            with self.subTest(card=card):
                self.assertEqual(_extract(f"{card}\n£2.03").total_amount, "2.03")
                self.assertIsNone(_extract(f"{card}\n\n£2.03"))
        self.assertIsNone(_extract("Example 0000\n£2.03"))

    def test_every_rejection_marker(self):
        for marker in (
            "dokończ płatność", "płatność nie została zakończona", "płatność nie została jeszcze zakończona",
            "oczekuje na płatność", "nie opłacono",
            "payment failed", "unable to process", "was declined", "nieudana płatność", "płatność odrzucona",
            "nie udało się pobrać", "nie udało się obciążyć",
            "czeka na odbiór", "gotowe do odbioru", "potwierdzenie odbioru", "została wysłana",
            "wysłaliśmy", "shipped", "out for delivery",
        ):
            with self.subTest(marker=marker):
                upper = marker.upper().replace(" ", "\u00a0\t ")
                self.assertIsNone(_extract("Total 2 PLN", f"Example {upper}"))
                self.assertIsNone(_extract(f"Example {upper}!\nTotal 2 PLN"))
        for marker in ("rozpoczęcie transakcji", "rozpoczęcia transakcji"):
            self.assertIsNone(_extract("Total 2 PLN", f"Example {marker}"))
        self.assertIsNone(_extract("Potwierdzenie rozpoczęcia transakcji\nTotal 2 PLN"))

    def test_completed_order_cancellation_subject_skips_only_an_affirmative_notice(self):
        body = "Wartość zamówienia: 21,90 PLN"
        notice = "Potwierdzenie anulowania zamówienia"
        for subject in (notice, notice + " we Example", notice + " w sklepie Example.",
                        notice.upper().replace(" ", "\u00a0"), notice + " nr 12345",
                        notice.replace("ó", "&oacute;"), notice.replace("ó", "&amp;oacute;")):
            with self.subTest(subject=subject):
                self.assertIsNone(_extract(body, subject))
        for subject in ("Prośba o " + notice.lower(), "Jak uzyskać " + notice.lower() + "?",
                        notice + "?", notice.replace("ó", "&#243;") + "?", notice + " nie jest dostępne",
                        notice + " we Example nie zostało wysłane",
                        "Nie otrzymano: " + notice, notice.ljust(256) + " nie dotarło"):
            with self.subTest(subject=subject):
                self.assertEqual(_extract(body, subject).total_amount, "21.9")
        self.assertEqual(_extract(body + "\nJak anulować zamówienie?", "Potwierdzenie zamówienia").total_amount, "21.9")

    def test_refund_and_correction_markers(self):
        for marker in ("zwrot", "refund", "korekta", "faktura korygująca", "credit note"):
            with self.subTest(subject=marker):
                upper = marker.upper().replace(" ", "\u00a0\t ")
                self.assertIsNone(_extract("Total 2 PLN", f"Example {upper}"))
        for heading in (
            "Zwrot środków", "Zwrot płatności", "Zwrot pieniędzy", "Zwrot za zamówienie", "Twój zwrot",
            "Otrzymaliśmy zwrot", "Refund issued", "Refund processed", "Refund of", "Your refund",
            "Faktura korygująca", "Korekta faktury",
        ):
            with self.subTest(heading=heading):
                upper = heading.upper().replace(" ", "\u00a0\t ")
                self.assertIsNone(_extract(f"{upper} – Example\nTotal 2 PLN"))
                self.assertEqual(_extract(f"Example {heading}\nTotal 2 PLN").total_amount, "2")
        for footer in ("Zwrot lub wymiana", "Zwroty i reklamacje", "Returns & refunds"):
            with self.subTest(footer=footer):
                self.assertEqual(_extract(f"Total 2 PLN\n{footer}").total_amount, "2")

    def test_marker_does_not_reject_a_priced_or_long_item(self):
        for item in ("Book about refund 2 PLN", "Book titled Refund (USD 13.00)", "Refund issued for 2 PLN",
                     "Article ‘payment failed’ 13,20 zł", "Refund processed " + "x" * 120):
            with self.subTest(item=item):
                self.assertEqual(_extract(f"{item}\nTotal 3 PLN").total_amount, "3")
        self.assertIsNone(_extract("Really shipped\nTotal 3 PLN"))
        self.assertIsNone(_extract("Czeka na odbiór 00000000000000\nTotal 3 PLN"))
        self.assertIsNone(_extract("PAYM\u200bENT FAILED\nTotal 3 PLN"))

    def test_fees_and_legal_footers_are_not_totals(self):
        invalid_lines = (
            "Dodatkowe opłaty: -1 PLN",
            "Opłata za realizację transakcji\n€8.10",
            "Opłata za usługę: 4 PLN",
            "Dostawa: 5 PLN",
            "Total 5 PLN kapitału",
            "Amount paid 6 PLN KRS",
            "Total 7 PLN zakładowego",
            "KRS: Example zwrot",
        )
        for line in invalid_lines:
            with self.subTest(line=line):
                self.assertIsNone(_extract(line))
                self.assertEqual(_extract(f"{line}\nRazem 2 PLN").total_amount, "2")
        self.assertEqual(_extract("Total\nKRS Example footer\n2 PLN").total_amount, "2")

    def test_gateway_payee_rules(self):
        cases = (
            ("Odbiorca wpłaty: Example Payee\nOpis zamówienia w Example Payee: Plan", "Example Payee"),
            ("Odbiorca wpłaty: Example PAYEE\nPłatność dla example payee", "Example PAYEE"),
            ("Potwierdzamy płatność dla Example Payee", "Example Payee"),
            ("Płatność dla - https://SHOP.Example.Test/path?q=a", _MERCHANT),
            ("Potwierdzenie dla - shop.example.test", _MERCHANT),
            ("Potwierdzenie dla - https://example..test/", _MERCHANT),
            ("Odbiorca wpłaty: A", _MERCHANT),
            ("Odbiorca wpłaty: " + "x" * 129, _MERCHANT),
            # v1f: one trailing all-digit token of 6+ digits is a gateway id, stripped from the payee.
            ("Odbiorca wpłaty: Example 0000002", "Example"),
            ("Odbiorca wpłaty: Example 000002", "Example"),
            ("Odbiorca wpłaty: 000000 person@example.test 000000", _MERCHANT),
        )
        for line, merchant in cases:
            with self.subTest(line=line):
                body = f"{line}\nTotal 3 PLN"
                self.assertEqual(_extract(body, gateway=True).merchant, merchant)
                self.assertEqual(_extract(body).merchant, _MERCHANT)
        for body in (
            "Odbiorca wpłaty: Example A\nPłatność dla Example B\nTotal 3 PLN",
            "Odbiorca wpłaty: Example A Płatność dla Example B\nTotal 3 PLN",
            "Odbiorca wpłaty: Example A dla - https://shop.example.test\nTotal 3 PLN",
        ):
            with self.subTest(body=body):
                self.assertEqual(_extract(body, gateway=True).merchant, "Example A")
        for body, payee in (
            ("Przekazaliśmy Twoje pieniądze\nDo Example Polska S.A.\nOdbiorca: Example Polska SA\nTotal 3 PLN",
             "Example Polska SA"),
            ("Odbiorca: EXAMPLE SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ\nPłatność dla Example sp. z o.o.\nTotal 3 PLN",
             "EXAMPLE SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ"),
            ("Odbiorca: Example Ltd\nPayment to Example Limited : 3 PLN\nTotal 3 PLN", "Example Ltd"),
        ):
            with self.subTest(body=body):
                self.assertEqual(_extract(body, gateway=True).merchant, payee)
        self.assertEqual(_extract("Odbiorca: S.A.\nPłatność dla Example\nTotal 3 PLN", gateway=True).merchant, "Example")

    def test_description_selection_redaction_and_cap(self):
        self.assertEqual(_extract(
            "Opis zamówienia w Example: Ｅxample\u200b plan a@example.test "
            "https://example.test/path 00000000002 order 100200300\nTotal 3 PLN", "",
        ).description, "Example plan order 100200300")
        self.assertEqual(_extract("Opis zamówienia w Example: a@example.test\nTytuł: Other\nTotal 3 PLN").description, "Other")
        for private in ("PL00 0000 0000 0000 0000 0000 0000", "0000 0000 0000 0000", "00000000000"):
            with self.subTest(private=private):
                self.assertEqual(_extract(f"Tytuł: Plan {private}\nTotal 3 PLN").description, "Example payment")
        self.assertEqual(_extract("Tytuł: 000000 person@example.test 000000\nTotal 3 PLN").description, "Example payment")
        self.assertEqual(_extract("Tytuł: 00000000002\nTotal 3 PLN", "www.example.test").description, "Płatność")
        self.assertEqual(_extract("Total 3 PLN", "").description, "Płatność")
        self.assertEqual(_extract("Total 3 PLN", "x" * 256 + " Refund").description, "x" * 128)

    def test_payment_fields_do_not_store_an_iban_before_a_suffix(self):
        iban = "NL91 ABNA 0417 1643 00"
        payment = _extract("Total 3 PLN", f"{iban} zł")
        self.assertEqual(payment.description, "zł")

        # Contact cleanup can bring the IBAN fragments back together after the initial scan.
        body = "Odbiorca: NL91 ABNA person@example.test 0417 1643 00 zł\nTotal 3 PLN"
        payment = _extract(body, gateway=True)
        self.assertEqual(payment.merchant, _MERCHANT)
        self.assertTrue(payment.payee_from_config)

    def test_date_labels(self):
        body = ("Date paid\n\nNov 14, 2026\nData 14 listopada 2026\nData: 15/11/2026\n"
                "Paid:2026-11-16\nPaid 17 lis. 2026\nPaid 31/11/2026\nData unknown\nTotal 3 PLN")
        self.assertEqual(_extract(body).body_dates, tuple(date(2026, 11, day) for day in range(14, 18)))
        self.assertEqual(_extract("Total 3 PLN\nData: 10 listopada 2026 KRS\nData premiery 9 listopada 2026").body_dates, ())

    def test_bounded_normalization(self):
        self.assertEqual(_extract("\n \ufb03\n\n Łącz\u200bnie ： PLN １２．３０").total_amount, "12.3")
        for body in (None, "x" * (MAX_TEXT_BYTES + 1), "ą" * (MAX_TEXT_BYTES // 2 + 1),
                     "\ud800", "\n" * MAX_TEXT_LINES, "", "Total: 3 PLN" + "x" * MAX_LINE_CHARS):
            with self.subTest(text=repr(body)[:24]):
                self.assertIsNone(_extract(body))
        self.assertIsNone(_extract("Total\n" + "x" * (MAX_LINE_CHARS + 1) + "\n3 PLN"))
