"""Own-transfer candidates: exact amounts, a three-day window, identity evidence and no arbitrary picks.

Every account number below is synthetic and deliberately not a valid IBAN or card number.
"""
import hashlib
import random
import unittest

from hestia.budget.transfer_match import (MAX_DAYS, RULE_VERSION, TransferCandidate, TransferEntry,
                                          TransferInputError, TransferMatchLimitError, match_transfers)

OWNER = "owner"
IBAN_A = ("IBAN", "XX00SYNTHETIC00000000000A")
IBAN_B = ("IBAN", "XX00SYNTHETIC00000000000B")
IBAN_C = ("IBAN", "XX00SYNTHETIC00000000000C")
IBAN_SHOP = ("IBAN", "XX00SYNTHETIC0000000SHOP1")
CARD_PAN = ("MASKED_PAN", "000000******0000")
ACCOUNTS = {"acc-a": (IBAN_A,), "acc-b": (IBAN_B,), "acc-c": (IBAN_C,), "acc-plain": (), "acc-card": (CARD_PAN,)}


def entry(account, reference, direction, amount="100.00", day="2026-11-10", *, currency="PLN",
          counterparty=(), eligible=True, owner=OWNER, fingerprint=None):
    if fingerprint is None:
        fingerprint = hashlib.sha256(f"{account}/{reference}".encode()).hexdigest()
    return TransferEntry(owner, account, reference, amount, currency, direction, day, fingerprint,
                         counterparty, eligible)


def match(entries, accounts=ACCOUNTS, **options):
    return match_transfers(OWNER, entries, accounts, **options)


def pairs(candidates):
    return [(c.debit_key, c.credit_key, c.source, c.date_distance, c.ambiguous) for c in candidates]


class UniquePairTests(unittest.TestCase):
    def test_isolated_mutual_pair_is_a_heuristic_suggestion_with_both_fingerprints(self):
        debit, credit = entry("acc-a", "d1", "DBIT"), entry("acc-b", "c1", "CRDT")
        result = match([debit, credit])
        self.assertEqual(pairs(result), [(("acc-a", "d1"), ("acc-b", "c1"), "heuristic", 0, False)])
        self.assertIsInstance(result, tuple)
        self.assertIsInstance(result[0], TransferCandidate)
        self.assertEqual((result[0].debit_fingerprint, result[0].credit_fingerprint),
                         (debit.fingerprint, credit.fingerprint))
        self.assertEqual(RULE_VERSION, 1)

    def test_window_is_three_calendar_days_inclusive_in_both_directions(self):
        self.assertEqual(MAX_DAYS, 3)
        for day, expected in (("2026-11-13", [3]), ("2026-11-07", [3]), ("2026-11-14", []), ("2026-11-06", [])):
            with self.subTest(day=day):
                result = match([entry("acc-a", "d", "DBIT"), entry("acc-b", "c", "CRDT", day=day)])
                self.assertEqual([c.date_distance for c in result], expected)

    def test_month_year_and_leap_day_boundaries_use_calendar_days(self):
        for debit_day, credit_day, distance in (("2026-10-30", "2026-11-02", 3), ("2026-12-30", "2027-01-02", 3),
                                                ("2028-02-28", "2028-03-01", 2), ("2027-02-27", "2027-03-02", 3),
                                                ("2027-02-26", "2027-03-02", None)):
            with self.subTest(debit=debit_day, credit=credit_day):
                result = match([entry("acc-a", "d", "DBIT", day=debit_day), entry("acc-b", "c", "CRDT", day=credit_day)])
                self.assertEqual([c.date_distance for c in result], [] if distance is None else [distance])

    def test_amounts_compare_exactly_after_scale_normalisation_without_floats(self):
        equal = (("100", "100.00"), ("0100.0", "100"), ("0.10", "0.1"),
                 ("123456789012345678901234567890.12345678901234567890",
                  "123456789012345678901234567890.1234567890123456789000"))
        different = (("100.01", "100.1"), ("9007199254740993", "9007199254740992"),
                     ("0.00000000000000000001", "0.00000000000000000002"), ("1", "10"))
        for left, right in equal:
            with self.subTest(equal=(left, right)):
                self.assertEqual(len(match([entry("acc-a", "d", "DBIT", left), entry("acc-b", "c", "CRDT", right)])), 1)
        for left, right in different:
            with self.subTest(different=(left, right)):
                self.assertEqual(match([entry("acc-a", "d", "DBIT", left), entry("acc-b", "c", "CRDT", right)]), ())

    def test_currency_direction_and_account_must_differ_correctly(self):
        self.assertEqual(match([entry("acc-a", "d", "DBIT"), entry("acc-b", "c", "CRDT", currency="EUR")]), ())
        self.assertEqual(match([entry("acc-a", "d", "DBIT"), entry("acc-b", "c", "DBIT")]), ())
        self.assertEqual(match([entry("acc-a", "d", "CRDT"), entry("acc-b", "c", "CRDT")]), ())
        # A refund on the same account (purchase DBIT, refund CRDT) is never an own transfer.
        self.assertEqual(match([entry("acc-card", "buy", "DBIT"), entry("acc-card", "refund", "CRDT")]), ())

    def test_mixed_currencies_pair_independently(self):
        result = match([entry("acc-a", "pln-d", "DBIT"), entry("acc-b", "pln-c", "CRDT"),
                        entry("acc-a", "eur-d", "DBIT", currency="EUR"), entry("acc-c", "eur-c", "CRDT", currency="EUR")])
        self.assertEqual(sorted((c.debit_key[1], c.credit_key[1], c.ambiguous) for c in result),
                         [("eur-d", "eur-c", False), ("pln-d", "pln-c", False)])

    def test_zero_and_ineligible_entries_never_pair_and_ineligible_values_are_not_parsed(self):
        self.assertEqual(match([entry("acc-a", "d", "DBIT", "0.00"), entry("acc-b", "c", "CRDT", "0")]), ())
        garbage = TransferEntry(OWNER, "acc-unknown", "pending", None, None, None, None, None, None, False)
        result = match([entry("acc-a", "d", "DBIT"), entry("acc-b", "c", "CRDT", eligible=False), garbage])
        self.assertEqual(result, ())


class AmbiguityTests(unittest.TestCase):
    def test_one_debit_with_two_credits_marks_both_and_picks_neither(self):
        result = match([entry("acc-a", "d", "DBIT"), entry("acc-b", "near", "CRDT"),
                        entry("acc-plain", "far", "CRDT", day="2026-11-13")])
        self.assertEqual(pairs(result), [(("acc-a", "d"), ("acc-b", "near"), "heuristic", 0, True),
                                         (("acc-a", "d"), ("acc-plain", "far"), "heuristic", 3, True)])

    def test_two_by_two_offers_all_four_choices(self):
        result = match([entry("acc-a", "d1", "DBIT"), entry("acc-c", "d2", "DBIT", day="2026-11-11"),
                        entry("acc-b", "c1", "CRDT"), entry("acc-plain", "c2", "CRDT", day="2026-11-11")])
        self.assertEqual(len(result), 4)
        self.assertTrue(all(c.ambiguous for c in result))

    def test_a_chain_makes_every_edge_touching_a_shared_leg_ambiguous(self):
        result = match([entry("acc-a", "d1", "DBIT", day="2026-11-08"), entry("acc-b", "c", "CRDT"),
                        entry("acc-c", "d2", "DBIT", day="2026-11-12")])
        self.assertEqual([(c.debit_key[1], c.ambiguous) for c in result], [("d1", True), ("d2", True)])

    def test_identity_resolves_amount_ambiguity_only_against_comparable_accounts(self):
        to_b = entry("acc-a", "d", "DBIT", counterparty=(IBAN_B,))
        comparable = match([to_b, entry("acc-b", "c-b", "CRDT"), entry("acc-c", "c-c", "CRDT")])
        self.assertEqual(pairs(comparable), [(("acc-a", "d"), ("acc-b", "c-b"), "account_identity", 0, False)])
        # acc-plain has no known identifiers, so its edge stays a heuristic choice and both remain ambiguous.
        unknown = match([to_b, entry("acc-b", "c-b", "CRDT"), entry("acc-plain", "c-p", "CRDT")])
        self.assertEqual(pairs(unknown), [(("acc-a", "d"), ("acc-b", "c-b"), "account_identity", 0, True),
                                          (("acc-a", "d"), ("acc-plain", "c-p"), "heuristic", 0, True)])


class IdentityEvidenceTests(unittest.TestCase):
    def test_debit_creditor_or_credit_debtor_naming_the_other_account_is_identity_evidence(self):
        for debit, credit in ((entry("acc-a", "d", "DBIT", counterparty=(IBAN_B,)), entry("acc-b", "c", "CRDT")),
                              (entry("acc-a", "d", "DBIT"), entry("acc-b", "c", "CRDT", counterparty=(IBAN_A,))),
                              (entry("acc-a", "d", "DBIT", counterparty=(IBAN_B, ("BBAN", "1"))),
                               entry("acc-b", "c", "CRDT", counterparty=(IBAN_A,)))):
            with self.subTest():
                self.assertEqual([c.source for c in match([debit, credit])], ["account_identity"])

    def test_comparable_identifier_naming_someone_else_blocks_the_heuristic(self):
        to_shop = entry("acc-a", "d", "DBIT", counterparty=(IBAN_SHOP,))
        self.assertEqual(match([to_shop, entry("acc-b", "c", "CRDT")]), ())
        from_shop = entry("acc-b", "c", "CRDT", counterparty=(IBAN_SHOP,))
        self.assertEqual(match([entry("acc-a", "d", "DBIT"), from_shop]), ())

    def test_contradiction_on_either_leg_wins_over_a_match_on_the_other(self):
        debit = entry("acc-a", "d", "DBIT", counterparty=(IBAN_B,))
        credit = entry("acc-b", "c", "CRDT", counterparty=(IBAN_SHOP,))
        self.assertEqual(match([debit, credit]), ())

    def test_non_comparable_schemes_and_unknown_accounts_fall_back_to_the_heuristic(self):
        debit = entry("acc-a", "d", "DBIT", counterparty=(("BBAN", "SYNTHETIC-1"),))
        self.assertEqual([c.source for c in match([debit, entry("acc-b", "c", "CRDT")])], ["heuristic"])
        to_plain = entry("acc-a", "d", "DBIT", counterparty=(IBAN_SHOP,))
        self.assertEqual([c.source for c in match([to_plain, entry("acc-plain", "c", "CRDT")])], ["heuristic"])

    def test_card_purchase_stays_alone_and_matched_repayment_is_a_candidate(self):
        purchase = entry("acc-card", "buy", "DBIT", "49.99", counterparty=(IBAN_SHOP,))
        self.assertEqual(match([purchase]), ())
        repayment = entry("acc-a", "repay", "DBIT", "49.99", counterparty=(("IBAN", "XX00SYNTHETICREPAY000000"),))
        settled = entry("acc-card", "settle", "CRDT", "49.99", day="2026-11-11")
        result = match([purchase, repayment, settled])
        self.assertEqual(pairs(result), [(("acc-a", "repay"), ("acc-card", "settle"), "heuristic", 1, False)])


class DecisionInputTests(unittest.TestCase):
    def test_blocked_pairs_are_skipped_and_no_longer_count_towards_ambiguity(self):
        entries = [entry("acc-a", "d", "DBIT"), entry("acc-b", "c1", "CRDT"), entry("acc-plain", "c2", "CRDT")]
        result = match(entries, blocked_pairs=frozenset({(("acc-a", "d"), ("acc-b", "c1"))}))
        self.assertEqual(pairs(result), [(("acc-a", "d"), ("acc-plain", "c2"), "heuristic", 0, False)])
        self.assertEqual(match(entries[:2], blocked_pairs={(("acc-a", "d"), ("acc-b", "c1"))}), ())

    def test_reserved_entries_are_skipped_entirely(self):
        entries = [entry("acc-a", "d1", "DBIT"), entry("acc-c", "d2", "DBIT"), entry("acc-b", "c", "CRDT")]
        result = match(entries, reserved_entries=frozenset({("acc-a", "d1")}))
        self.assertEqual(pairs(result), [(("acc-c", "d2"), ("acc-b", "c"), "heuristic", 0, False)])
        self.assertEqual(match(entries, reserved_entries={("acc-b", "c")}), ())


class DeterminismAndLimitTests(unittest.TestCase):
    def test_input_order_does_not_change_the_result(self):
        entries = [entry(account, f"{direction}-{index}", direction, f"{amount}.00", f"2026-11-{day:02d}")
                   for index, (account, direction, amount, day) in enumerate(
                       [("acc-a", "DBIT", 10, 1), ("acc-b", "CRDT", 10, 2), ("acc-c", "CRDT", 10, 4),
                        ("acc-b", "DBIT", 20, 5), ("acc-a", "CRDT", 20, 5), ("acc-plain", "DBIT", 30, 9),
                        ("acc-card", "CRDT", 30, 12), ("acc-c", "DBIT", 10, 3)])]
        expected = match(entries)
        self.assertEqual(len(expected), 5)
        for seed in range(5):
            shuffled = list(entries)
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(match(iter(shuffled)), expected)
        self.assertEqual([c.debit_key[1] for c in expected],
                         ["DBIT-0", "DBIT-0", "DBIT-7", "DBIT-3", "DBIT-5"])

    def test_edge_limit_counts_compatible_pairs_and_never_returns_a_partial_graph(self):
        entries = [entry("acc-a", "d1", "DBIT"), entry("acc-a", "d2", "DBIT"),
                   entry("acc-b", "c1", "CRDT"), entry("acc-a", "same-account", "CRDT")]
        self.assertEqual(len(match(entries, max_edges=4)), 2)
        with self.assertRaises(TransferMatchLimitError) as raised:
            match(entries, max_edges=3)
        self.assertEqual(str(raised.exception), "Transfer matching limit exceeded")
        self.assertEqual(match([entry("acc-a", "d", "DBIT")], max_edges=0), ())


class ValidationTests(unittest.TestCase):
    def assertRejected(self, *args, **kwargs):
        with self.assertRaises(TransferInputError) as raised:
            match_transfers(*args, **kwargs)
        self.assertEqual(str(raised.exception), "Invalid transfer matching input")

    def test_scope_arguments_are_validated(self):
        valid = [entry("acc-a", "d", "DBIT")]
        for owner in ("", " ", "x" * 1025, "own\ner", None):
            self.assertRejected(owner, valid, ACCOUNTS)
        for accounts in (None, [("acc-a", ())], {"": ()}, {"acc-a": [IBAN_A]}, {"acc-a": (("IBAN",),)},
                         {"acc-a": ("IBAN",)}, {"acc-a": (("I" * 33, "x"),)}, {"acc-a": (("IBAN", "x" * 129),)},
                         {"acc-a": tuple(("IBAN", str(n)) for n in range(17))}):
            self.assertRejected(OWNER, valid, accounts)
        for blocked in ([], {("acc-a", "d")}, {(("acc-a", "d"),)}, {(("acc-a",), ("acc-b", "c"))},
                        {(("acc-a", "d"), "acc-b")}):
            self.assertRejected(OWNER, valid, ACCOUNTS, blocked_pairs=blocked)
        for reserved in ([("acc-a", "d")], {"acc-a"}, {("acc-a", 1)}):
            self.assertRejected(OWNER, valid, ACCOUNTS, reserved_entries=reserved)
        for limit in (True, -1, 1.5, "10"):
            self.assertRejected(OWNER, valid, ACCOUNTS, max_edges=limit)
        for entries in (None, "acc-a", b"x", {"acc-a": 1}):
            self.assertRejected(OWNER, entries, ACCOUNTS)

    def test_entries_are_validated_without_echoing_values(self):
        base = dict(account="acc-a", reference="d", direction="DBIT")
        bad = [entry(**base, owner="intruder"), entry(**base, amount="-1.00"), entry(**base, amount="1e3"),
               entry(**base, amount=""), entry(**base, amount="1."), entry(**base, amount=".5"),
               entry(**base, amount="1" * 81), entry(**base, amount=100), entry(**base, currency="pln"),
               entry(**base, currency="PLNX"), entry(**base, currency=None), entry(**base, day="2026-02-30"),
               entry(**base, day="2026-1-01"), entry(**base, day=None), entry(**base, day="２０２６-11-10"),
               entry(**base, fingerprint="A" * 64), entry(**base, fingerprint="0" * 63), entry(**base, fingerprint=12345),
               entry(**base, counterparty=[IBAN_B]), entry(**base, counterparty=(("IBAN", ""),)),
               entry(**base, eligible=1), entry("acc-unknown", "d", "DBIT"), entry("acc-a", "d", "XFER"),
               entry("acc-a", "", "DBIT"), entry("acc-a", "r" * 1025, "DBIT"), entry("acc-a", 7, "DBIT"),
               entry("acc\x00", "d", "DBIT"), entry(None, "d", "DBIT"),
               ("acc-a", "d")]
        for item in bad:
            with self.subTest(item=getattr(item, "__dict__", item)):
                self.assertRejected(OWNER, [item], ACCOUNTS)

    def test_entry_references_are_opaque_and_kept_verbatim(self):
        opaque = ["\x00", " ", "ref\x7f\n", "\t" * 1024, "r" * 1024]
        entries = [entry("acc-a", ref, "DBIT", f"{index + 1}.00") for index, ref in enumerate(opaque)]
        entries += [entry("acc-b", ref, "CRDT", f"{index + 1}.00") for index, ref in enumerate(opaque)]
        result = match(entries, blocked_pairs=frozenset({(("acc-a", "\x00"), ("acc-b", " "))}),
                       reserved_entries=frozenset({("acc-c", "\x01")}))
        self.assertEqual([(c.debit_key[1], c.credit_key[1], c.ambiguous) for c in result],
                         [(ref, ref, False) for ref in sorted(opaque)])
        self.assertEqual(match(list(reversed(entries))), result)
        self.assertEqual(match(entries[:1] + entries[5:6], reserved_entries={("acc-a", "\x00")}), ())

    def test_duplicate_keys_are_rejected_even_when_one_copy_is_ineligible(self):
        self.assertRejected(OWNER, [entry("acc-a", "d", "DBIT"), entry("acc-a", "d", "CRDT", eligible=False)], ACCOUNTS)

    def test_values_never_appear_in_repr_or_errors(self):
        secret = entry("acc-a", "SECRET-REF", "DBIT", "4242.42", counterparty=(IBAN_SHOP,))
        self.assertNotIn("SECRET-REF", repr(secret))
        self.assertNotIn("4242", repr(secret))
        candidate = match([entry("acc-a", "SECRET-REF", "DBIT"), entry("acc-b", "c", "CRDT")])[0]
        self.assertNotIn("SECRET-REF", repr(candidate))
        with self.assertRaises(TransferInputError) as raised:
            match([entry("acc-a", "SECRET-REF", "DBIT", "4242.4x")])
        self.assertNotIn("SECRET", str(raised.exception) + repr(raised.exception))
        self.assertIsNone(raised.exception.__context__)


if __name__ == "__main__":
    unittest.main()
