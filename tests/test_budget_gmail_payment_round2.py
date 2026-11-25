"""Synthetic parser regressions from the payment mail review."""

from datetime import date
from decimal import Decimal
from unittest import TestCase

from hestia.budget.gmail_payment import _HTTP_URL, _parse_amount, _shorten_url, extract_payment, inspect_payment


def _inspect(body, *subjects, gateway=False):
    return inspect_payment(body, subjects=subjects, merchant="Example Merchant", gateway=gateway)


class GmailPaymentReviewTests(TestCase):
    def test_grouped_thousands_start_at_one(self):
        for number in ("0.005", "0,005", "0 005", "01.005", "01,005", "01 005"):
            with self.subTest(number=number):
                self.assertTrue(_inspect(f"Total: {number} PLN").missing_total)
                self.assertTrue(_inspect(f"Total: €{number}").missing_total)
        self.assertEqual(_inspect("Total: 1.005 PLN").payment.total_amount, "1005")

    def test_contact_token_filter_drops_embedded_markers(self):
        self.assertEqual(_inspect(
            "Total: 1 PLN\nTytuł: Synthetic order_https://shop.example.test/ matching "
            "field_MAILTO:x@example.test www.example.test @x@y x@y@ @x x@ label",
        ).payment.description, "Synthetic matching @x x@ label")
        self.assertEqual(_inspect("Total: 1 PLN\nOdbiorca: Example x_https://pay.example.test Studio",
                                  gateway=True).payment.merchant, "Example Studio")

    def test_url_shortening_preserves_hosts_and_surrounding_brackets(self):
        text = ("[HTTP://sample:password@shop.example.test:8443/full/path?q=example "
                "https://assets.example.test/image.png] (http://[2001:db8::1]:8080/test) https://:443/no-host")
        self.assertEqual(_HTTP_URL.sub(_shorten_url, text),
                         "[https://shop.example.test/ https://assets.example.test/] "
                         "(https://[2001:db8::1]/) https://:443/no-host")

    def test_overlong_gateway_link_still_supplies_payee_host(self):
        url = "http://shop.example.test/" + "placeholder-" * 60
        for wrapped in (url, f"[{url}]", f"({url})"):
            with self.subTest(wrapper=wrapped[:1]):
                payment = _inspect(f"Potwierdzenie dla - {wrapped}\nTotal: 3 PLN", gateway=True).payment
                self.assertEqual((payment.merchant, payment.payee_host), ("Example Merchant", "shop.example.test"))

    def test_receipt_payee_ends_at_the_first_amount_or_illustration(self):
        amount_and_date = "$15.24 Paid December 8, 2026"
        illustration = "(invoice illustration [https://assets.example.test/rendering.png])"
        for suffix in (f"{amount_and_date} {illustration}", f"{illustration} {amount_and_date}"):
            with self.subTest(suffix=suffix):
                payment = _inspect(f"Receipt from Example Studio {suffix}\nCharged: 40.12 PLN", gateway=True).payment
                self.assertEqual(payment.merchant, "Example Studio")
                self.assertEqual(payment.body_dates, (date(2026, 12, 8),))

    def test_all_subjects_are_normalized_before_marker_matching(self):
        for subject in ("Ref\x00und", "Ref" + "\x00" * 512 + "und", "Re\u034ffund", "Ｒｅｆｕｎｄ",
                        "Potwierdzenie rozpoczęcia transakcji"):
            with self.subTest(subject=subject):
                result = _inspect("Total: 1 PLN", "Safe subject", subject)
                self.assertIsNone(result.payment)
                self.assertFalse(result.missing_total)
        result = _inspect("Total: 1 PLN", "\x00", "Synthetic subject")
        self.assertEqual(result.payment.description, "Synthetic subject")

    def test_title_is_not_a_payee_or_paid_date_and_opis_cannot_supply_a_date(self):
        result = _inspect("Total: 1 PLN\nTytuł: Payment to Example Payee : 1 PLN\n"
                          "Opis płatności: Paid Nov 1, 2026\nOpis zamówienia w Example Shop: Paid Nov 2, 2026\n"
                          "Paid Nov 3, 2026", gateway=True).payment
        self.assertEqual(result.merchant, "Example Shop")
        self.assertEqual(result.body_dates, (date(2026, 11, 3),))
        self.assertEqual(_inspect("Total: 1 PLN\nTytuł: płatność dla Hidden Payee", gateway=True).payment.merchant,
                         "Example Merchant")

    def test_only_multiple_labelled_payees_on_one_line_must_agree(self):
        result = _inspect("Odbiorca: Example One; Odbiorca wpłaty: Example Two\nTotal: 1 PLN", gateway=True)
        self.assertIsNone(result.payment)
        self.assertFalse(result.missing_total)
        for line, merchant, host in (
            ("dla - https://one.example.test/ dla - https://two.example.test/", "Example Merchant", "one.example.test"),
            ("dla - https://one.example.test/ Odbiorca: Example Two", "Example Two", "one.example.test"),
            ("Receipt from Example One Receipt #1234-5678 Payment to Example Two : 1 PLN", "Example One", None),
        ):
            with self.subTest(line=line):
                payment = _inspect(f"{line}\nTotal: 1 PLN", gateway=True).payment
                self.assertEqual((payment.merchant, payment.payee_host), (merchant, host))
        payment = _inspect("dla - x.example.test; dla - https://x.example.test/\nTotal: 1 PLN", gateway=True).payment
        self.assertEqual((payment.merchant, payment.payee_host), ("Example Merchant", "x.example.test"))
        payment = _inspect("Opis zamówienia w Example One without colon\nPayment to Example Two : error\n"
                           "dla - ?\nTotal: 1 PLN", gateway=True).payment
        self.assertEqual(payment.merchant, "Example Merchant")

    def test_order_description_only_takes_payee_from_order_heading(self):
        for line, merchant, fallback, description in (
            ("Opis zamówienia w Real Shop: Płatność dla szkoły", "Real Shop", False, "Płatność dla szkoły"),
            ("Opis zamówienia w Shop 1234567: Płatność dla Evil Shop", "Shop", False,
             "Płatność dla Evil Shop"),
            ("Opis zamówienia w Example One: synthetic detail Odbiorca: Example Two", "Example One", False,
             "synthetic detail Odbiorca: Example Two"),
        ):
            with self.subTest(line=line):
                payment = _inspect(f"{line}\nTotal: 1 PLN", gateway=True).payment
                self.assertEqual((payment.merchant, payment.payee_from_config, payment.description),
                                 (merchant, fallback, description))

    def test_flattened_summary_reads_independent_total_cells(self):
        for body in (
            "Receipt #ABCD Total excluding tax $1.00 Total   : $2.00 Amount paid $2.00",
            "Receipt #ABCD Total $2.00 Amount paid not a value",
            "Receipt #ABCD Amount paid $2.00",
            "Receipt #ABCD Total $2.00 ... Total $2 USD",
            "Receipt #ABCD Total $2.00 VAT $1.00 Tax $1.00 Discount $0.50",
        ):
            with self.subTest(body=body):
                payment = _inspect(body).payment
                self.assertEqual((payment.total_amount, payment.currency), ("2", "USD"))
        for body in (
            "Receipt #ABCD " + "Total " * 60 + "Amount paid not a value",
            "Receipt #ABCD Example Plan with no totals",
            "Receipt #ABCD Subtotal $1.00 Total excluding tax $2.00 VAT $3.00",
            "Receipt #ABCD Total estimate $2.00 Amount paid not a value",
        ):
            with self.subTest(body=body):
                self.assertTrue(_inspect(body).missing_total)

    def test_charged_takes_priority_and_exchange_rate_is_not_an_amount(self):
        for body in (
            "Receipt #ABCD Total $2.00 Amount paid $2.00 Charged 8.31zł using 1 USD = 4.1550 PLN",
            "Amount paid: $2.00\nCharged: PLN 8.31 using 1 USD = 4.1550 PLN. Questions?",
            "Total: $2.00\nCharged\n8.31 PLN",
        ):
            with self.subTest(body=body):
                payment = _inspect(body).payment
                self.assertEqual((payment.total_amount, payment.currency), ("8.31", "PLN"))

    def test_flattened_totals_reject_conflicts_and_truly_unlabelled_amounts(self):
        for body in (
            "Receipt #ABCD Total $2.00 ... Total $3.00 Charged 8.31 PLN",
            "Receipt #ABCD Total $2.00 / $3.00 Charged 8.31 PLN",
            "Receipt #ABCD Total $2.00 Total £2.00 Amount paid $2.00",
            "Receipt #ABCD Total $2.00 Amount paid -$2.00 Charged 8.31 PLN",
            "Receipt #ABCD Amount paid $2.00 Charged 8.31 PLN Charged 9.31 PLN",
            "Receipt #ABCD Charged 8.31 PLN using 1 USD = 4.1550 PLN including 2 PLN",
            "Charged 8.31 PLN using coupon 2 PLN",
        ):
            with self.subTest(body=body):
                result = _inspect(body)
                self.assertIsNone(result.payment)
                self.assertFalse(result.missing_total)

    def test_order_total_ignores_fee_and_table_header_without_an_amount_value(self):
        for table_entry in ("Synthetic Product -1 EUR and 2 EUR", "(16.31 PLN)", "(-16.31 PLN)"):
            with self.subTest(table_entry=table_entry):
                payment = _inspect(f"Wartość zamówienia\n16.31 PLN\nKoszt dostawy: -1 PLN\nRAZEM\n{table_entry}").payment
                self.assertEqual((payment.total_amount, payment.currency), ("16.31", "PLN"))
                self.assertTrue(_inspect(f"RAZEM\n{table_entry}").missing_total)

    def test_zero_with_symbol_and_equivalent_code_is_an_explicit_rejection(self):
        value = "0,00 € EUR"
        self.assertEqual(_parse_amount(value), (Decimal("0.00"), "EUR"))
        result = _inspect(f"Amount paid: {value}")
        self.assertIsNone(result.payment)
        self.assertFalse(result.missing_total)

    def test_conflicting_trailing_currency_code_is_an_explicit_rejection(self):
        for value in ("10,00 € USD", "10,00 zł EUR"):
            with self.subTest(value=value):
                result = _inspect(f"Amount paid: {value}")
                self.assertIsNone(result.payment)
                self.assertFalse(result.missing_total)

    def test_second_amount_on_a_labelled_total_is_an_explicit_rejection(self):
        for body in (
            "Total: 1 PLN 1 PLN",
            "Total: 1 PLN including -2 PLN",
            "Total: Estimate 1 PLN including 2 PLN",
            "Total: Estimate -1 PLN",
            "Total: $1.00 / £2.00",
            "Total:\n\n1 PLN 2 PLN",
            "Amount paid: 1 PLN\nRazem: 2 PLN and 3 PLN",
            "Total: -2 PLN",
            "Total: PLN -2",
            "Total: 1000001 PLN",
            "Total: 0 PLN",
            "Total: 1 PLN\nDo zapłaty: 2 PLN",
        ):
            with self.subTest(body=body):
                result = _inspect(body)
                self.assertIsNone(result.payment)
                self.assertFalse(result.missing_total)

    def test_unparseable_label_can_be_a_card_charge_line(self):
        payment = _inspect("Paid using card **** 0000\n£2.03").payment
        self.assertEqual((payment.total_amount, payment.currency), ("2.03", "GBP"))
        self.assertTrue(_inspect("Paid using card **** 0000\nUnknown").missing_total)

    def test_body_markers_ignore_fields_and_started_phrase_requires_notice_prefix(self):
        for line in ("Data rozpoczęcia transakcji: 05 listopada 2026 godz. 18:25",
                     "Data rozpoczęcia transakcji", "Odbiorca: Example Refund",
                     "* Opis: Payment failed text", "Wstęp:Potwierdzenie rozpoczęcia transakcji",
                     "Nr. weryfikacji: refund"):
            with self.subTest(line=line):
                self.assertTrue(_inspect(f"{line}\nTotal: 1 PLN").payment)
        for field in ("Tytuł", "Opis płatności", "Odbiorca wpłaty", "Data rozliczenia", "Nr. weryfikacji",
                      "Numer zamówienia", "Identyfikator przesyłki", "Kwota transakcji", "Termin płatności",
                      "Adres dostawy"):
            with self.subTest(field=field):
                self.assertTrue(_inspect(f"{field}: payment failed\nTotal: 1 PLN").payment)
        for notice in ("Potwierdzenie rozpoczęcia transakcji",
                       "Potwierdzenie rozpoczęcia transakcji dla - https://x.example.test/",
                       "Payment failed at 18:25 today", "Dokończ płatność: https://pay.example.test/invoice",
                       "Status płatności: nie opłacono", "Status transakcji: nie opłacono",
                       "Payment failed: please update your card", "Zwrot środków: zamówienie 123456",
                       "* Zwrot środków za zamówienie", "* Potwierdzenie rozpoczęcia transakcji",
                       "Status (próba): payment failed", "Refund issued: Example note"):
            with self.subTest(notice=notice):
                result = _inspect(f"{notice}\nTotal: 1 PLN")
                self.assertFalse(result.missing_total)
                self.assertIsNone(result.payment)

    def test_inline_tags_leave_labels_and_masked_pan_cannot_be_a_total(self):
        payment = _inspect("<p>Cena podstawowa</p>\n2,10 zł\n3,40 zł\n"
                           '<p class="line"><b>Zapłacono łącznie</b></p>\n5,50 zł\n'
                           "<p>Szczegóły płatności</p>\n000000******0000").payment
        self.assertEqual(payment.total_amount, "5.5")
        self.assertTrue(_inspect("<p>Zapłacono łącznie</p>\n000000******0000 PLN").missing_total)

    def test_gateway_receipt_payee_from_body_or_subject(self):
        for subject in ("Your receipt from Example Studio #1234-5678", "Your Example Studio receipt [#1234-5678]",
                        "Twoje pokwitowanie od Example Studio nr 1234-5678"):
            with self.subTest(subject=subject):
                self.assertEqual(_inspect("Total: 1 PLN", subject, gateway=True).payment.merchant, "Example Studio")
                self.assertEqual(_inspect("Total: 1 PLN", subject).payment.merchant, "Example Merchant")
                payment = _inspect("Total: 1 PLN\nReceipt from Example Shop", subject, gateway=True).payment
                self.assertEqual(payment.merchant, "Example Shop")
        for body in ("Receipt from Example Studio", "Receipt from Example Studio Receipt #1234-5678",
                     "Receipt from Example Studio $1.00 Paid 1 Nov 2026", "Payment to Example Studio : 1 PLN",
                     "Przekazaliśmy Twoje pieniądze\nDo Example Studio\nPłatność zakończona sukcesem",
                     "Zarejestrowaliśmy zlecenie płatności dla Example Studio (",
                     "Zarejestrowaliśmy zlecenie płatności dla Example Studio (http://studio.example.test/)."):
            with self.subTest(body=body):
                self.assertEqual(_inspect(f"{body}\nTotal: 1 PLN", gateway=True).payment.merchant, "Example Studio")
        payment = _inspect("Przekazaliśmy Twoje pieniądze\nDo zapłaty: 1 PLN", gateway=True).payment
        self.assertEqual(payment.merchant, "Example Merchant")
        self.assertTrue(payment.payee_from_config)
        payment = _inspect("Receipt from Example Merchant\nTotal: 1 PLN", gateway=True).payment
        self.assertEqual(payment.merchant, "Example Merchant")
        self.assertFalse(payment.payee_from_config)
        self.assertEqual(_inspect("Total: 1 PLN\nPotwierdzenie płatności dla Example Studio",
                                  "Your receipt from Example Shop #----", gateway=True).payment.merchant,
                         "Example Merchant")

    def test_order_references_use_labels_and_are_case_insensitive(self):
        for label in ("Zamówienie", "zamówienia nr", "zamówienie numer", "Order #", "Order no.", "nr zamówienia"):
            with self.subTest(label=label):
                payment = _inspect("Total: 1 PLN", f"Example {label} AbCdEf7").payment
                self.assertEqual(payment.order_reference, "abcdef7")
        self.assertEqual(_inspect("Total: 1 PLN\nOrder #ABCDEF", "Order received").payment.order_reference, "abcdef")
        self.assertEqual(_inspect("Total: 1 PLN", "Order #RECEIVED").payment.order_reference, "received")
        self.assertEqual(_inspect("Total: 1 PLN", "Zamówienie opłacone", "Order confirmed").payment.order_reference, None)
        self.assertEqual(_inspect("Total: 1 PLN", "Zamówienie nr ŻÓŁWIK").payment.order_reference, "żółwik")
        self.assertEqual(_inspect("Total: 1 PLN\nOrder #ABCDEF", "Order no. 00000000001").payment.order_reference,
                         "00000000001")
        self.assertIsNone(_inspect("Total: 1 PLN\nOrder #ABCDEF-GHI", "Order Z0123").payment.order_reference)
        self.assertIsNone(_inspect("Total: 1 PLN", "Order no. 00000000000").payment.order_reference)

    def test_bare_order_label_skips_prose_and_number_label_keeps_reference(self):
        self.assertIsNone(_inspect("Total: 1 PLN", "Twoje zamówienie zostało opłacone").payment.order_reference)
        for number in ("111111", "222222"):
            with self.subTest(number=number):
                payment = _inspect("Total: 1 PLN", f"Your order number: {number}").payment
                self.assertEqual(payment.order_reference, number)

    def test_description_fallback_through_generic_and_digit_heavy_candidates(self):
        for generic in ("Płatność", "Payment", "Zamówienie!", "Order", "Receipt", "Faktura"):
            with self.subTest(generic=generic):
                self.assertEqual(_inspect(f"Tytuł: {generic}\nTotal: 1 PLN", "Example confirmation").payment.description,
                                 "Example confirmation")
        body = "Total: 1 PLN\nOpis zamówienia w Example Studio: Materiały 00000000001"
        self.assertEqual(_inspect(body, "Example subject").payment.description, "Example subject")
        self.assertEqual(_inspect(body).payment.description, "Materiały")
        self.assertEqual(_inspect(body + "\nTytuł: Twoje materiały").payment.description, "Twoje materiały")
        self.assertEqual(_inspect(body + "\nTytuł: Plan 000000 Item 000000", "Example subject").payment.description,
                         "Example subject")
        self.assertEqual(_inspect("Total: 1 PLN", "Order 00000000001").payment.description, "Order")
        self.assertEqual(_inspect("Total: 1 PLN", "Order no. 00000000001").payment.description, "Order no.")
        self.assertEqual(extract_payment("Total: 1 PLN\nOpis płatności: Przykładowy opis", subject="Example subject",
                                         merchant="Example Merchant", gateway=True).description, "Przykładowy opis")

    def test_markdown_emphasis_around_labelled_values_parses_single_pair_only(self):
        for body, expected_amount, expected_currency in (
            ("- Kwota: *109,00 PLN*\n- Kwota po przewalutowaniu: *-*", "109", "PLN"),
            ("Kwota: *109,00 PLN*", "109", "PLN"),
            ("Kwota: _109,00 PLN_", "109", "PLN"),
            ("- Total: _$12.34_", "12.34", "USD"),
            ("Kwota:\n*109,00 PLN*", "109", "PLN"),
            ("Charged: *8,31 zł* using 1 USD = 4.1550 PLN", "8.31", "PLN"),
        ):
            with self.subTest(body=body):
                payment = _inspect(body, "Example payment").payment
                self.assertEqual((payment.total_amount, payment.currency), (expected_amount, expected_currency))

        gw = _inspect("- Kwota: *109,00 PLN*\n- Opłata za realizację transakcji: *1,50 PLN*\n"
                      "- Kwota po przewalutowaniu: *-*", "Potwierdzenie", gateway=True).payment
        self.assertEqual((gw.total_amount, gw.currency, gw.fee_amount), ("110.5", "PLN", "1.5"))

        for missing_body in (
            "Kwota: **109,00 PLN**",
            "Kwota: __109,00 PLN__",
            "Kwota: *109,00 PLN",
            "Kwota: 109,00 PLN*",
            "Kwota: *109,00 PLN_",
            "Kwota: 1*09,00 PLN",
            "Kwota: *1*09,00 PLN*",
            "Kwota: *-*",
            "Kwota: *",
            "Kwota: **",
        ):
            with self.subTest(missing_body=missing_body):
                result = _inspect(missing_body, "Example payment")
                self.assertTrue(result.missing_total)
                self.assertIsNone(result.payment)

        for rejected_body in (
            "Kwota: *-109,00 PLN*",
            "Kwota: *0,00 PLN*",
            "Kwota: *109,00 PLN* *50,00 PLN*",
        ):
            with self.subTest(rejected_body=rejected_body):
                result = _inspect(rejected_body, "Example payment")
                self.assertFalse(result.missing_total)
                self.assertIsNone(result.payment)
