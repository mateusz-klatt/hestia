"""Synthetic receipt layouts for column-split OCR recovery."""

from unittest import TestCase

from hestia.budget.receipt_parser import parse_receipt


_HEADER = "SKLEP FIKCYJNY\nPARAGON FISKALNY\n"


class ReceiptColumnSplitTests(TestCase):
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

        grouped_amounts = tuple(sorted(amounts, key=lambda amount: amount.startswith("-")))
        for ordered_amounts in (amounts, grouped_amounts):
            with self.subTest(amounts=ordered_amounts):
                columns = "\n".join((*labels, *ordered_amounts))
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
            "KAKAO\nSOK\n1szt * 4,00 4,00 A\n1szt * 5,00 5,00 A\n"
            "CHLEB 1szt * 2,00 2,00 A\nSER\nMLEKO\n1szt * 2,00 2,00 A\n1szt * 2,00 2,00 A\n"
            "SUMA PLN 26,00"
        )
        self.assertEqual([line["raw_name"] for line in parsed["lines"]],
                         ["Pozycja bez nazwy", "KAWA", "HERBATA", "WODA", "KAKAO", "SOK", "CHLEB", "SER", "MLEKO"])
        self.assertEqual(parsed["lines"][3]["discount_amount"], "1")
        self.assertEqual(parsed["math_delta"], "0")

    def test_split_deposit_requires_a_matching_deposit_subtotal(self):
        parsed = parse_receipt(
            f"{_HEADER}KAWA\nHERBATA\n1szt * 2,00 2,00 A\n1szt * 3,00 3,00 A\n"
            "SUMA PLN 5,00\nOPAKOWANIA ZWROTNE WYDANIA\nDRS KAUCJA PUSZKA\n2 * 0,50 1,00\n"
            "OPAKOWANIA ZWROTNE SUMA 2,00\nDO ZAPŁATY 7,00"
        )
        self.assertEqual([line["raw_name"] for line in parsed["lines"]], ["KAWA", "HERBATA"])
        self.assertEqual((parsed["total_amount"], parsed["math_delta"]), ("7", "-2"))

    def test_mismatched_column_counts_keep_legacy_parse(self):
        for columns, subtotal, expected_names, expected_delta in (
            ("KAWA\nHERBATA\n1szt * 5,00 5,00 A", "5,00", ["HERBATA"], "0"),
            ("KAWA\nOPUST KAWA\n1szt * 5,00 5,00 A\n1szt * 2,00 2,00 A", "7,00",
             ["OPUST KAWA", "Pozycja bez nazwy"], "0"),
            ("KAWA\nHERBATA\n1szt * 5,00 5,00 A\n-1,00 A", "4,00", ["HERBATA"], "1"),
        ):
            with self.subTest(columns=columns):
                parsed = parse_receipt(f"{_HEADER}{columns}\nSUMA PLN {subtotal}")
                self.assertEqual([line["raw_name"] for line in parsed["lines"]], expected_names)
                self.assertEqual([line["discount_amount"] for line in parsed["lines"]], ["0"] * len(expected_names))
                self.assertEqual(parsed["math_delta"], expected_delta)

    def test_uses_suma_instead_of_final_payment_to_accept_a_column_split(self):
        columns = "KAWA\nHERBATA\n1szt * 2,00 2,00 A\n1szt * 3,00 3,00 A"
        parsed = parse_receipt(f"{_HEADER}{columns}\nSUMA PLN 5,02\nDO ZAPŁATY 5,00")
        self.assertEqual([line["raw_name"] for line in parsed["lines"]], ["HERBATA", "Pozycja bez nazwy"])
        self.assertEqual((parsed["total_amount"], parsed["math_delta"]), ("5", "0"))

    def test_requires_suma_to_accept_column_split(self):
        columns = "KAWA\nHERBATA\n1szt * 2,00 2,00 A\n1szt * 3,00 3,00 A"
        parsed = parse_receipt(f"{_HEADER}{columns}\nDO ZAPŁATY 5,00")
        self.assertEqual([line["raw_name"] for line in parsed["lines"]], ["HERBATA", "Pozycja bez nazwy"])
        self.assertEqual((parsed["total_amount"], parsed["math_delta"]), ("5", "0"))

    def test_single_pair_uses_plain_suma_when_legacy_cannot_recover_label(self):
        parsed = parse_receipt(f"{_HEADER}KAWA\n2 * 2,00 4,00 A\nSUMA 4,00")
        self.assertEqual([line["raw_name"] for line in parsed["lines"]], ["KAWA"])
        self.assertEqual((parsed["total_amount"], parsed["math_delta"]), ("4", "0"))

    def test_preserves_a_verified_wrapped_row(self):
        parsed = parse_receipt("SKLEP FIKCYJNY\nKAWA\n2szt * 2,00 4,00 A\nSUMA PLN 4,00")
        self.assertEqual([line["raw_name"] for line in parsed["lines"]], ["KAWA"])
        self.assertEqual((parsed["total_amount"], parsed["math_delta"]), ("4", "0"))

    def test_reconstructed_line_error_keeps_legacy_parse(self):
        parsed = parse_receipt(f"{_HEADER}KAWA\nHERBATA\n1szt * 2,00 2,00 A\n+1 * 3,00 3,00 A\nSUMA PLN 5,00")
        self.assertEqual([line["raw_name"] for line in parsed["lines"]], ["HERBATA", "+1 * 3,00"])
        self.assertEqual(parsed["math_delta"], "0")

    def test_reconciliation_includes_a_paired_discount_before_the_first_item(self):
        columns = "OPUST KAWA\nKAWA\nHERBATA\n1szt * 2,00 2,00 A\n1szt * 3,00 3,00 A\n-1,00 A"
        parsed = parse_receipt(f"{_HEADER}{columns}\nSUMA PLN 5,00")
        self.assertEqual([line["raw_name"] for line in parsed["lines"]], ["HERBATA", "Pozycja bez nazwy"])
        self.assertEqual(parsed["math_delta"], "0")

    def test_accepts_a_one_cent_subtotal_difference_in_either_direction(self):
        columns = "KAWA\nHERBATA\n1szt * 2,00 2,00 A\n1szt * 3,00 3,00 A"
        for subtotal, delta in (("4,99", "0.01"), ("5,01", "-0.01")):
            with self.subTest(subtotal=subtotal):
                parsed = parse_receipt(f"{_HEADER}{columns}\nSUMA PLN {subtotal}")
                self.assertEqual([line["raw_name"] for line in parsed["lines"]], ["KAWA", "HERBATA"])
                self.assertEqual(parsed["math_delta"], delta)

    def test_strips_only_trailing_comma_unit_suffixes_in_both_layouts(self):
        labels = ("KAWA, szt", "HERBATA, sz", "WODA,szt", "KAKAO  ,  SzT  ", "SOK,sZ", "NAPÓJ, szt XXL", "MIÓD SZT")
        expected = ["KAWA", "HERBATA", "WODA", "KAKAO", "SOK", "NAPÓJ, szt XXL", "MIÓD SZT"]
        amount = "1szt × 2,00=2,00 A"
        row_ordered = "\n".join(f"{name} {amount}" for name in labels)
        column_split = "\n".join([*labels] + [amount] * len(labels))
        for body in (row_ordered, column_split):
            with self.subTest(body=body):
                parsed = parse_receipt(f"{_HEADER}{body}\nSUMA PLN 14,00")
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
