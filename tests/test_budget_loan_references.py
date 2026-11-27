"""Synthetic reference extraction and masking shared by the picker and forecast."""
from hashlib import sha256
import unittest

from hestia.budget.loan_references import (
    extract_reference_hashes, loan_reference_candidates, mask_loan_references, normalized_loan_label,
)


class LoanReferenceTests(unittest.TestCase):
    def test_reference_forms_share_a_hash_and_leave_only_the_last_four_visible(self):
        digest = sha256(b"SYNTH123456").hexdigest()
        for label, masked in (
            ("Rata kredytu nr: Synth/1234-56", "Rata kredytu nr: ••••3456"),
            ("Ubezpieczenie synth123456", "Ubezpieczenie ••••3456"),
            ("Polisa REF. ＳＹＮＴＨ123456", "Polisa REF. ••••3456"),
        ):
            with self.subTest(label=label):
                self.assertEqual(extract_reference_hashes(label), {digest})
                self.assertEqual(mask_loan_references(label), masked)
                self.assertEqual(loan_reference_candidates(label), [
                    {"hash": digest, "last4": "3456", "label": masked},
                ])
        self.assertEqual(normalized_loan_label("Pożyczka, współfinansowanie"), "POZYCZKA, WSPOLFINANSOWANIE")

    def test_grouped_number_and_explicit_letter_reference_are_independent_tokens(self):
        label = "Polisa nr 77 9012 3456, reference SYNTHETIC"
        masked = "Polisa nr ••••3456, reference ••••ETIC"
        candidates = loan_reference_candidates(label)
        self.assertEqual(candidates, [
            {"hash": sha256(b"7790123456").hexdigest(), "last4": "3456", "label": masked},
            {"hash": sha256(b"SYNTHETIC").hexdigest(), "last4": "ETIC", "label": masked},
        ])
        self.assertNotIn(sha256(b"90123456").hexdigest(), extract_reference_hashes(label))

    def test_short_tokens_plain_words_and_dates_are_not_references(self):
        label = "November rent on 2026-11-05, reference 2026-11, nr 1234, m0-only, 12345"
        self.assertEqual(extract_reference_hashes(label), set())
        self.assertEqual(loan_reference_candidates(label), [])
        self.assertEqual(mask_loan_references(label), label)
