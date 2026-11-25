"""Synthetic parser regressions from the payment mail review."""

from datetime import date
from unittest import TestCase

from hestia.budget.gmail_payment import extract_payment, inspect_payment


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

    def test_multiple_payees_on_one_line_must_agree(self):
        for line in (
            "Odbiorca: Example One; Odbiorca wpłaty: Example Two",
            "dla - https://one.example.test/ dla - https://two.example.test/",
            "dla - https://one.example.test/ Odbiorca: Example Two",
            "Receipt from Example One Receipt #1234-5678 Payment to Example Two : 1 PLN",
            "Opis zamówienia w Example One: synthetic detail Odbiorca: Example Two",
        ):
            with self.subTest(line=line):
                result = _inspect(f"{line}\nTotal: 1 PLN", gateway=True)
                self.assertIsNone(result.payment)
                self.assertFalse(result.missing_total)
        payment = _inspect("dla - x.example.test; dla - https://x.example.test/\nTotal: 1 PLN", gateway=True).payment
        self.assertEqual(payment.merchant, "x.example.test")
        payment = _inspect("Opis zamówienia w Example One without colon\nPayment to Example Two : error\n"
                           "dla - ?\nTotal: 1 PLN", gateway=True).payment
        self.assertEqual(payment.merchant, "Example Merchant")

    def test_flattened_summary_uses_only_the_final_total_and_paid_cells(self):
        payment = _inspect("Receipt #ABCD Total excluding tax $1.00 Total   : $2.00 Amount paid $2.00").payment
        self.assertEqual(payment.total_amount, "2")
        for body in (
            "Receipt #ABCD " + "Total " * 60 + "Amount paid not a value",
            "Receipt #ABCD Total $2.00 Amount paid not a value",
            "Receipt #ABCD Amount paid $2.00",
        ):
            with self.subTest(body=body):
                self.assertTrue(_inspect(body).missing_total)

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
                     "Status (próba): payment failed", "Nr. weryfikacji: refund"):
            with self.subTest(line=line):
                self.assertTrue(_inspect(f"{line}\nTotal: 1 PLN").payment)
        for notice in ("Potwierdzenie rozpoczęcia transakcji",
                       "Potwierdzenie rozpoczęcia transakcji dla - https://x.example.test/",
                       "Payment failed at 18:25 today"):
            with self.subTest(notice=notice):
                self.assertFalse(_inspect(f"{notice}\nTotal: 1 PLN").missing_total)
                self.assertIsNone(_inspect(f"{notice}\nTotal: 1 PLN").payment)

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
                self.assertIsNone(_inspect("Total: 1 PLN\nReceipt from Example Shop", subject, gateway=True).payment)
        for body in ("Receipt from Example Studio", "Receipt from Example Studio Receipt #1234-5678",
                     "Receipt from Example Studio $1.00 Paid 1 Nov 2026", "Payment to Example Studio : 1 PLN",
                     "Przekazaliśmy Twoje pieniądze\nDo Example Studio\nPłatność zakończona sukcesem",
                     "Zarejestrowaliśmy zlecenie płatności dla Example Studio (",
                     "Zarejestrowaliśmy zlecenie płatności dla Example Studio (http://studio.example.test/)."):
            with self.subTest(body=body):
                self.assertEqual(_inspect(f"{body}\nTotal: 1 PLN", gateway=True).payment.merchant, "Example Studio")
        payment = _inspect("Przekazaliśmy Twoje pieniądze\nDo zapłaty: 1 PLN", gateway=True).payment
        self.assertEqual(payment.merchant, "Example Merchant")
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
