"""Synthetic receipt layouts for column-split OCR recovery."""

from time import perf_counter
from unittest import TestCase

from hestia.budget.receipt_parser import _SPLIT_DEPOSIT_SECTION, _orphan_name, parse_receipt


_HEADER = "SKLEP FIKCYJNY\nPARAGON FISKALNY\n"


class ReceiptColumnSplitTests(TestCase):
    def assert_keeps_legacy_parse(self, body, subtotal):
        # This label still supplies the legacy total without offering a fiscal subtotal to the fallback.
        legacy = parse_receipt(f"{_HEADER}{body}\nSUMA PLN RAZEM {subtotal}")
        self.assertEqual(parse_receipt(f"{_HEADER}{body}\nSUMA PLN {subtotal}"), legacy)

    def test_column_split_matches_row_order_with_duplicate_items_and_deposit(self):
        labels = (
            "PALIWO TESTOWE # 3",
            "PŁYN TESTOWY, szt",
            "SOK TESTOWY 250ml, sz",
            "OPUST SOK TESTOWY 250ml, sz",
            "SOK TESTOWY 250ml, sz",
            "RABAT SOK TESTOWY 250ml, sz",
        )
        amounts = (
            "10,00L * 6,00 60,00 B",
            "1szt * 30,00 30,00 A",
            "1szt * 9,00 9,00 A",
            "-1,50 A",
            "1szt * 10,00 10,00 B",
            "-2,50 B",
        )
        subtotal = "Suma Opustów -4,00\nSUMA PLN 105,00\nOPAKOWANIA ZWROTNE WYDANIA\n"
        payment = "OPAKOWANIA ZWROTNE SUMA 1,00\nDO ZAPŁATY 106,00\nPłatność Mastercard-C 106,00"
        row_ordered = "\n".join(f"{name}    {amount}" for name, amount in zip(labels, amounts, strict=True))
        expected = parse_receipt(
            f"{_HEADER}{row_ordered}\n{subtotal}DRS KAUCJA PUSZKA 2 * 0,50 1,00\n{payment}"
        )
        self.assertEqual((expected["total_amount"], expected["math_delta"]), ("106", "0"))
        self.assertEqual(
            [(line["raw_name"], line["line_kind"], line["gross_line_total"], line["discount_amount"])
             for line in expected["lines"]],
            [
                ("PALIWO TESTOWE # 3", "item", "60", "0"),
                ("PŁYN TESTOWY", "item", "30", "0"),
                ("SOK TESTOWY 250ml", "item", "9", "1.5"),
                ("SOK TESTOWY 250ml", "item", "10", "2.5"),
                ("DRS KAUCJA PUSZKA", "deposit", "1", "0"),
            ],
        )

        columns = "\n".join((*labels, *amounts))
        split = f"{_HEADER}{columns}\n{subtotal}DRS KAUCJA PUSZKA\n2 * 0,50 1,00\n{payment}"
        self.assertEqual(parse_receipt(split), expected)

    def test_headerless_fuel_example_keeps_the_same_result(self):
        row_ordered = """PARAGON FISKALNY
FUEL95 ACT. Pompa # 3              10,00L * 6,00 60,00 B
WASHER FLUID 3.5L EKO, szt          1szt * 30,00 30,00 A
DRINK XY 250ml, sz                  1szt * 9,00 9,00 A
OPUST DRINK XY 250ml, sz                        -1,50 A
DRINK XY 250ml, sz                  1szt * 9,00 9,00 A
OPUST DRINK XY 250ml, sz                        -1,50 A
-----
Suma Opustów -3,00
SUMA PLN 105,00
-----
OPAKOWANIA ZWROTNE WYDANIA
DRS KAUCJA PUSZKA                    2 * 0,50 1,00
OPAKOWANIA ZWROTNE SUMA 1,00
DO ZAPŁATY 106,00
Płatność Mastercard-C 106,00
"""
        column_split = """PARAGON FISKALNY
FUEL95 ACT. Pompa # 3
WASHER FLUID 3.5L EKO, szt
DRINK XY 250ml, sz
OPUST DRINK XY 250ml, sz
DRINK XY 250ml, sz
OPUST DRINK XY 250ml, sz
10,00L * 6,00 60,00 B
1szt * 30,00 30,00 A
1szt * 9,00 9,00 A
-1,50 A
1szt * 9,00 9,00 A
-1,50 A
Suma Opustów -3,00
SUMA PLN 105,00
OPAKOWANIA ZWROTNE WYDANIA
DRS KAUCJA PUSZKA
2 * 0,50 1,00
OPAKOWANIA ZWROTNE SUMA 1,00
DO ZAPŁATY 106,00
Płatność Mastercard-C 106,00
"""
        expected = parse_receipt(row_ordered)
        self.assertEqual((expected["total_amount"], expected["math_delta"]), ("106", "0"))
        self.assertEqual(parse_receipt(column_split), expected)

    def test_recovers_multiple_runs_across_metadata_and_complete_rows(self):
        parsed = parse_receipt(
            f"{_HEADER}1szt * 3,00 3,00 A\nNr kasy 7\n"
            "KAWA\nHERBATA\n1szt * 2,00 2,00 A\n1szt * 3,00 3,00 A\n"
            "WODA 1szt * 4,00 4,00 A\nOPUST WODA -1,00 A\n"
            "KAKAO\nSOK\nOPUST SOK\n1szt * 4,00 4,00 A\n1szt * 5,00 5,00 A\n-1,00 A\n"
            "CHLEB 1szt * 2,00 2,00 A\nSER\nMLEKO\n1szt * 2,00 2,00 A\n1szt * 2,00 2,00 A\n"
            "SUMA PLN 25,00"
        )
        self.assertEqual([line["raw_name"] for line in parsed["lines"]],
                         ["Pozycja bez nazwy", "KAWA", "HERBATA", "WODA", "KAKAO", "SOK", "CHLEB", "SER", "MLEKO"])
        self.assertEqual(parsed["lines"][3]["discount_amount"], "1")
        self.assertEqual(parsed["math_delta"], "0")

    def test_split_deposit_requires_a_matching_deposit_subtotal(self):
        parsed = parse_receipt(
            f"{_HEADER}KAWA\nHERBATA\nOPUST HERBATA\n1szt * 2,00 2,00 A\n1szt * 3,00 3,00 A\n-1,00 A\n"
            "SUMA PLN 4,00\nOPAKOWANIA ZWROTNE WYDANIA\nDRS KAUCJA PUSZKA\n2 * 0,50 1,00\n"
            "OPAKOWANIA ZWROTNE SUMA 2,00\nDO ZAPŁATY 6,00"
        )
        self.assertEqual([line["raw_name"] for line in parsed["lines"]], ["KAWA", "HERBATA"])
        self.assertEqual((parsed["total_amount"], parsed["math_delta"]), ("6", "-2"))

    def test_split_deposit_search_finishes_quickly_on_blank_calculation(self):
        malformed = (
            "OPAKOWANIA ZWROTNE WYDANIA\nDRS KAUCJA PUSZKA\n"
            + " " * 64_000 + "\nOPAKOWANIA ZWROTNE BRAK 1,00"
        )
        started = perf_counter()
        self.assertIsNone(_SPLIT_DEPOSIT_SECTION.search(malformed))
        self.assertLess(perf_counter() - started, 1.0)

    def test_previous_calculation_run_cannot_become_name_rows(self):
        parsed = parse_receipt(
            f"{_HEADER}KAWA\nHERBATA\n1szt * 2 2\n1szt * 3 3\n"
            "SOK\nMLEKO\nOPUST MLEKO\n1szt * 4 4\n1szt * 5 5 A\n-1,00 A\nSUMA PLN 13,00"
        )
        self.assertEqual([line["raw_name"] for line in parsed["lines"]], ["KAWA", "HERBATA", "SOK", "MLEKO"])
        self.assertEqual(parsed["math_delta"], "0")

    def test_mismatched_column_counts_keep_legacy_parse(self):
        for columns, subtotal in (
            ("KAWA\nHERBATA\nOPUST HERBATA\n1szt * 5,00 5,00 A\n-1,00 A", "4,00"),
            ("KAWA\nOPUST KAWA\n1szt * 5,00 5,00 A\n-1,00 A\n1szt * 2,00 2,00 A", "6,00"),
        ):
            with self.subTest(columns=columns):
                self.assert_keeps_legacy_parse(columns, subtotal)

    def test_rejects_type_mismatches_without_reordering_amounts(self):
        for columns in (
            "KAWA\nOPUST KAWA\nHERBATA\n1szt * 2,00 2,00 A\n1szt * 3,00 3,00 B\n-1,00 A",
            "KAWA\nHERBATA\nOPUST HERBATA\n1szt * 2,00 2,00 A\n-1,00 B\n1szt * 3,00 3,00 B",
        ):
            with self.subTest(columns=columns):
                self.assert_keeps_legacy_parse(columns, "4,00")

    def test_rejects_discounts_without_an_immediately_preceding_item(self):
        for columns, subtotal in (
            ("OPUST KAWA\nKAWA\n-1,00 A\n1szt * 5,00 5,00 A", "4,00"),
            ("KAWA\nOPUST KAWA\nRABAT KAWA\n1szt * 5,00 5,00 A\n-1,00 A\n-1,00 A", "3,00"),
        ):
            with self.subTest(columns=columns):
                self.assert_keeps_legacy_parse(columns, subtotal)

    def test_rejects_discount_when_ptu_does_not_match(self):
        for ptu in ("B", ""):
            with self.subTest(ptu=ptu):
                self.assert_keeps_legacy_parse(f"KAWA\nOPUST KAWA\n1szt * 5,00 5,00 {ptu}\n-1,00 A", "4,00")

    def test_rejects_discount_that_makes_the_item_net_negative(self):
        self.assert_keeps_legacy_parse("KAWA\nOPUST KAWA\n1szt * 2,00 2,00 A\n-3,00 A", "-1,00")

    def test_rejects_negative_net_after_an_additional_inline_discount(self):
        columns = (
            "KAWA\nHERBATA\nOPUST HERBATA\n1szt * 5,00 5,00 A\n1szt * 2,00 2,00 A\n-1,00 A\n"
            "OPUST HERBATA -2,00 A"
        )
        self.assert_keeps_legacy_parse(columns, "4,00")

    def test_accepts_discount_equal_to_gross(self):
        parsed = parse_receipt(f"{_HEADER}KAWA\nOPUST KAWA\n1szt * 2,00 2,00 A\n-2,00 A\nSUMA PLN 0,00")
        [item] = parsed["lines"]
        self.assertEqual((item["raw_name"], item["discount_amount"], item["net_line_total"]), ("KAWA", "2", "0"))

    def test_uses_suma_instead_of_final_payment_to_accept_a_column_split(self):
        columns = "KAWA\nHERBATA\n1szt * 2,00 2,00 A\n1szt * 3,00 3,00 A"
        parsed = parse_receipt(f"{_HEADER}{columns}\nSUMA PLN 5,02\nDO ZAPŁATY 5,00")
        self.assertEqual([line["raw_name"] for line in parsed["lines"]], ["HERBATA", "Pozycja bez nazwy"])
        self.assertEqual((parsed["total_amount"], parsed["math_delta"]), ("5", "0"))

    def test_nonreconciling_row_ordered_receipt_has_no_candidate(self):
        self.assert_keeps_legacy_parse("KAWA 1szt * 2,00 2,00 A", "3,00")

    def test_requires_suma_to_accept_column_split(self):
        columns = "KAWA\nHERBATA\n1szt * 2,00 2,00 A\n1szt * 3,00 3,00 A"
        parsed = parse_receipt(f"{_HEADER}{columns}\nDO ZAPŁATY 5,00")
        self.assertEqual([line["raw_name"] for line in parsed["lines"]], ["HERBATA", "Pozycja bez nazwy"])
        self.assertEqual((parsed["total_amount"], parsed["math_delta"]), ("5", "0"))

    def test_single_pair_uses_plain_suma_when_legacy_cannot_recover_label(self):
        parsed = parse_receipt(f"{_HEADER}KAWA\n2 * 2,00 4,00\nSUMA 4,00")
        self.assertEqual([line["raw_name"] for line in parsed["lines"]], ["KAWA"])
        self.assertEqual((parsed["total_amount"], parsed["math_delta"]), ("4", "0"))

    def test_reconciled_wrapped_item_with_address_preserves_full_legacy_result(self):
        wrapped = "UL. FIKCYJNA 10\nMIÓD\n2szt * 4,00 8,00 A\n1szt * 1,00 1,00 A"
        self.assert_keeps_legacy_parse(wrapped, "9,00")
        parsed = parse_receipt(f"{_HEADER}{wrapped}\nSUMA PLN 9,00")
        self.assertEqual([line["raw_name"] for line in parsed["lines"]], ["MIÓD", "Pozycja bez nazwy"])

    def test_reconciled_legacy_subtotal_wins_over_a_different_final_payment(self):
        columns = "KAWA\nHERBATA\n1szt * 2,00 2,00 A\n1szt * 3,00 3,00 A"
        parsed = parse_receipt(f"{_HEADER}{columns}\nSUMA PLN 5,00\nDO ZAPŁATY 6,00")
        self.assertEqual([line["raw_name"] for line in parsed["lines"]], ["HERBATA", "Pozycja bez nazwy"])
        self.assertEqual((parsed["total_amount"], parsed["math_delta"]), ("6", "-1"))

    def test_legacy_subtotal_within_one_cent_needs_no_fallback(self):
        columns = "KAWA\nHERBATA\n1szt * 2,00 2,00 A\n1szt * 3,00 3,00 A"
        for subtotal in ("4,99", "5,01"):
            with self.subTest(subtotal=subtotal):
                self.assert_keeps_legacy_parse(columns, subtotal)

    def test_preserves_a_verified_wrapped_row_after_shop_header(self):
        parsed = parse_receipt("SKLEP FIKCYJNY\nKAWA\n2szt * 2,00 4,00 A\nSUMA PLN 4,00")
        self.assertEqual([line["raw_name"] for line in parsed["lines"]], ["KAWA"])
        self.assertEqual((parsed["total_amount"], parsed["math_delta"]), ("4", "0"))

    def test_fallback_excludes_the_shop_header_without_a_fiscal_marker(self):
        parsed = parse_receipt("SKLEP FIKCYJNY\nKAWA\nOPUST KAWA\n1szt * 5,00 5,00 A\n-1,00 A\nSUMA PLN 4,00")
        self.assertEqual(parsed["raw_shop_name"], "SKLEP FIKCYJNY")
        [item] = parsed["lines"]
        self.assertEqual((item["raw_name"], item["discount_amount"], parsed["math_delta"]), ("KAWA", "1", "0"))

    def test_excludes_the_shop_header_from_a_split_run_without_fiscal_header(self):
        parsed = parse_receipt("SKLEP FIKCYJNY\nKAWA\nOPUST KAWA\n2szt * 2,00 4,00 A\n-1,00 A\nSUMA PLN 3,00")
        self.assertEqual(parsed["raw_shop_name"], "SKLEP FIKCYJNY")
        self.assertEqual([line["raw_name"] for line in parsed["lines"]], ["KAWA"])

    def test_reconstructed_line_error_keeps_legacy_parse(self):
        columns = ", szt\nHERBATA\nOPUST HERBATA\n1szt * 2,00 2,00 A\n1szt * 3,00 3,00 A\n-1,00 A"
        self.assert_keeps_legacy_parse(columns, "4,00")

    def test_unverified_wrapped_arithmetic_preserves_legacy_result(self):
        columns = "MIÓD\n2szt * 4,00 9,00 A"
        self.assert_keeps_legacy_parse(columns, "9,00")
        parsed = parse_receipt(f"{_HEADER}{columns}\nSUMA PLN 9,00")
        [item] = parsed["lines"]
        self.assertEqual((item["raw_name"], item["quantity"], item["gross_line_total"], parsed["math_delta"]),
                         ("2szt * 4,00", "1", "9", "0"))

    def test_rejects_unverified_arithmetic_even_when_columns_would_reconcile(self):
        columns = "MIÓD\nOPUST MIÓD\n2szt * 4,00 9,00 A\n-1,00 A"
        self.assert_keeps_legacy_parse(columns, "8,00")

    def test_accepts_arithmetic_within_one_cent(self):
        parsed = parse_receipt(f"{_HEADER}MIÓD\nOPUST MIÓD\n2szt * 4,00 8,01 A\n-1,00 A\nSUMA PLN 7,01")
        [item] = parsed["lines"]
        self.assertEqual((item["raw_name"], item["quantity"], item["net_line_total"], parsed["math_delta"]),
                         ("MIÓD", "2", "7.01", "0"))

    def test_metadata_and_nonname_lines_are_not_orphans(self):
        for line in (
            "UL. FIKCYJNA 10", "al. TESTOWA 3", "Pl. PRÓBNY 9", "ADRES UL. FIKCYJNA 10", "00-000 FIKCYJNE",
            "NIP: 0000000000", "SPRZEDAWCA NIP0000000000", "Kasjer TESTOWY", "KASA 3", "Nr 10", "2026-01-02",
            "03.01.2026", "03/01/2026", "12:34", "PARAGON FISKALNY", "12345", "---", "(?.!)",
        ):
            with self.subTest(line=line):
                self.assertFalse(_orphan_name(line))

    def test_address_breaks_an_orphan_run_even_when_pairing_would_reconcile(self):
        columns = "UL. FIKCYJNA 10\nKAWA\nOPUST KAWA\n1szt * 2,00 2,00 A\n1szt * 3,00 3,00 A\n-1,00 A"
        self.assert_keeps_legacy_parse(columns, "4,00")

    def test_orphans_do_not_cross_gaps_before_amounts(self):
        for labels in ("KAWA\n-----\nHERBATA\nOPUST HERBATA", "KAWA\nHERBATA\nOPUST HERBATA\n"):
            with self.subTest(labels=labels):
                self.assert_keeps_legacy_parse(f"{labels}\n1szt * 2,00 2,00 A\n1szt * 3,00 3,00 A\n-1,00 A", "4,00")

    def test_accepts_a_one_cent_subtotal_difference_in_either_direction(self):
        columns = "KAWA\nHERBATA\nOPUST HERBATA\n1szt * 2,00 2,00 A\n1szt * 3,00 3,00 A\n-1,00 A"
        for subtotal, delta in (("3,99", "0.01"), ("4,01", "-0.01")):
            with self.subTest(subtotal=subtotal):
                parsed = parse_receipt(f"{_HEADER}{columns}\nSUMA PLN {subtotal}")
                self.assertEqual([line["raw_name"] for line in parsed["lines"]], ["KAWA", "HERBATA"])
                self.assertEqual(parsed["math_delta"], delta)

    def test_strips_only_trailing_comma_unit_suffixes_in_both_layouts(self):
        labels = ("KAWA, szt", "HERBATA, sz", "WODA,szt", "KAKAO  ,  SzT  ", "SOK,sZ", "NAPÓJ, szt XXL", "MIÓD SZT")
        expected = ["KAWA", "HERBATA", "WODA", "KAKAO", "SOK", "NAPÓJ, szt XXL", "MIÓD SZT"]
        amount = "1szt × 2,00=2,00 A"
        row_ordered = "\n".join(f"{name} {amount}" for name in labels) + "\nOPUST MIÓD SZT -1,00 A"
        column_split = "\n".join([*labels, "OPUST MIÓD SZT"] + [amount] * len(labels) + ["-1,00 A"])
        for body in (row_ordered, column_split):
            with self.subTest(body=body):
                parsed = parse_receipt(f"{_HEADER}{body}\nSUMA PLN 13,00")
                self.assertEqual([line["raw_name"] for line in parsed["lines"]], expected)
                self.assertEqual([line["raw_unit"] for line in parsed["lines"]], ["szt"] * len(expected))
                self.assertEqual(parsed["math_delta"], "0")

    def test_preserves_row_ordered_parsing_without_unit_suffixes(self):
        parsed = parse_receipt(
            f"{_HEADER}CHLEB 2 szt x 4,50=9,00 A\nRABAT CHLEB -1,00 A\n"
            "SER 0,5kg * 10,00 5,00 B\nSUMA PLN 13,00\nGOTÓWKA"
        )
        self.assertEqual(
            (parsed["raw_shop_name"], parsed["payment_method"], parsed["total_amount"], parsed["math_delta"]),
            ("SKLEP FIKCYJNY", "cash", "13", "0"),
        )
        self.assertEqual(
            [(line["line_no"], line["raw_name"], line["quantity"], line["ptu_code"], line["net_line_total"])
             for line in parsed["lines"]],
            [(1, "CHLEB", "2", "A", "8"), (2, "SER", "0.5", "B", "5")],
        )
