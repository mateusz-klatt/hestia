"""Unit tests for bank_ref_tokens, BLIK Nr transakcji, card_last4, and canonical_gateway."""
from __future__ import annotations

import base64
from email.message import EmailMessage
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from sqlalchemy import select

from hestia import db
from hestia.budget import gmail_message
from hestia.budget.gmail_payment import bank_ref_tokens, canonical_gateway, inspect_payment
from hestia.budget.gmail_store import GATEWAY_REF_DOMAIN, GmailStore, digest
from hestia.budget.gmail_trust import PaymentSender

_SECRET = b"synthetic-id-tokens-hmac-secret-32b"


class FakeClock:
    def __init__(self, wall=1_795_600_000.0):
        self.now = wall

    def wall(self):
        return self.now


class BankRefTokenTests(unittest.TestCase):
    def test_canonical_gateway_aliases(self):
        self.assertIsNone(canonical_gateway(None))
        self.assertIsNone(canonical_gateway(123))
        self.assertIsNone(canonical_gateway("unknown-gw"))
        for raw, expected in [
            ("p24", "p24"),
            ("Przelewy24", "p24"),
            ("Przelewy 24", "p24"),
            ("tpay", "tpay"),
            ("Tpay.com", "tpay"),
            ("autopay", "autopay"),
            ("Blue Media", "autopay"),
            ("payu", "payu"),
            ("paynow", "paynow"),
            ("BLIK", "blik"),
            ("Stripe", "stripe"),
        ]:
            self.assertEqual(canonical_gateway(raw), expected)

    def test_p24_and_opt_extraction_no_cross_match(self):
        self.assertEqual(bank_ref_tokens("", "p24"), set())
        self.assertEqual(bank_ref_tokens("   ", "p24"), set())
        self.assertEqual(bank_ref_tokens(None, "p24"), set())
        self.assertEqual(bank_ref_tokens("P24-T5C-123-456", None), set())
        self.assertEqual(bank_ref_tokens("P24-T5C-123-456", "stripe"), set())

        self.assertEqual(
            bank_ref_tokens("PRZELEW /OPT/IU/p24-t5c-123-456 SKLEP", "Przelewy24"),
            {"P24-T5C-123-456"},
        )
        self.assertEqual(bank_ref_tokens("PRZELEW /OPT/IU/P24-T5C-123-456", "tpay"), set())
        self.assertEqual(bank_ref_tokens("PRZELEW /OPT/IU/P24-T5C-123-456", "autopay"), set())
        self.assertEqual(bank_ref_tokens("PRZELEW /OPT/IU/P24-T5C-123-456", "payu"), set())
        self.assertEqual(bank_ref_tokens("PRZELEW /OPT/IU/P24-T5C-123-456", "paynow"), set())
        self.assertEqual(bank_ref_tokens("PRZELEW /OPT/IU/P24-T5C-123-456", "blik"), set())

    def test_tpay_paynow_payu_autopay_and_blik_tokens(self):
        self.assertEqual(
            bank_ref_tokens("/OPT/IU/tr-abcd-1234xyzx/95069274474 SKLEP", "Tpay"),
            {"TR-ABCD-1234XYZX"},
        )
        self.assertEqual(bank_ref_tokens("TR-ABCD-1234XYZX/95069274474", "p24"), set())

        self.assertEqual(
            bank_ref_tokens("/OPT/IU/ab12-cde-fgh-ijk", "Paynow"),
            {"AB12-CDE-FGH-IJK"},
        )

        payu_tokens = bank_ref_tokens(
            "PayU S.A. (987654321) transId=abc123def NIP 7791011327 /OPT/IU/246813579 TR-SKIP-123",
            "PayU",
        )
        self.assertTrue({"987654321", "ABC123DEF", "246813579"}.issubset(payu_tokens))
        self.assertNotIn("7791011327", payu_tokens)
        self.assertNotIn("TR-SKIP-123", payu_tokens)

        autopay_tokens = bank_ref_tokens(
            "Autopay /OPT/IU/abcd_123456_pl_22_11_2026 /OPT/00001234/2026 AP/2026/001234 "
            "22/11/2026 NIP 779-101-13-27 P24-AAA-BBB-CCC",
            "Autopay",
        )
        self.assertTrue(
            {"ABCD_123456_PL_22_11_2026", "00001234/2026", "AP/2026/001234"}.issubset(autopay_tokens)
        )
        self.assertNotIn("22/11/2026", autopay_tokens)
        self.assertNotIn("P24-AAA-BBB-CCC", autopay_tokens)

        blik_tokens = bank_ref_tokens("Płatność BLIK 12.11.2026 Nr transakcji\n95069274474 shop.example.test", "blik")
        self.assertEqual(blik_tokens, {"95069274474"})

    def test_kimi_review_strict_boundaries_and_gateway_anchoring(self):
        # 1. BLIK trailing/leading non-separator characters rejected cleanly.
        for bad_blik in (
            "Nr transakcji 123456ABC",
            "Nr transakcji ABC123456",
            "Nr transakcji 123456.78",
            "Nr transakcji 123456-78",
            "Nr transakcji 123456/78",
            "Nr transakcji 123456_78",
            "PayU Nr transakcji 123456",
        ):
            with self.subTest(bad_blik=bad_blik):
                self.assertEqual(bank_ref_tokens(bad_blik, "blik"), set())
        self.assertEqual(bank_ref_tokens("Nr transakcji 123456", "blik"), {"123456"})
        self.assertEqual(bank_ref_tokens("Płatność BLIK Nr transakcji: (123456), sklep", "blik"), {"123456"})

        # 2. PayU strict numeric/token boundaries and required PayU gateway anchoring.
        for bad_payu in (
            "PayU ORDER.987654",
            "PayU 987654.12",
            "PayU ORDER-987654",
            "PayU 987654_12",
            "PayU ORDER/987654",
            "PayU /OPT/IU/ORDER.987654",
            "ORDER 987654",
            "Autopay 987654 PayU",
            "987654 Autopay 123456 PayU",
        ):
            with self.subTest(bad_payu=bad_payu):
                self.assertEqual(bank_ref_tokens(bad_payu, "payu"), set())
        self.assertEqual(bank_ref_tokens("PayU S.A. 987654", "payu"), {"987654"})
        self.assertEqual(bank_ref_tokens("PayU (987654)", "payu"), {"987654"})
        self.assertEqual(bank_ref_tokens("PayU /OPT/IU/987654", "payu"), {"987654"})
        self.assertEqual(bank_ref_tokens("Zamówienie 987654 PayU", "payu"), {"987654"})

        # 3. Autopay rejects PayU-labelled or unanchored numbers, and honors Autopay/Blue Media names.
        for bad_autopay in (
            "PayU order 456789",
            "Autopay SA PayU order 456789",
            "456789",
            "Autopay ORDER.456789",
            "Autopay /OPT/IU/ORDER.456789",
            "Stripe P24-T5C-123-456",
        ):
            with self.subTest(bad_autopay=bad_autopay):
                self.assertEqual(bank_ref_tokens(bad_autopay, "autopay"), set())
        self.assertEqual(bank_ref_tokens("Autopay order 456789", "autopay"), {"456789"})
        self.assertEqual(bank_ref_tokens("Blue Media S.A. /OPT/IU/456789", "autopay"), {"456789"})
        # Kimi delta review (issue 1): exact /OPT/ value only, never sliding sub-windows.
        self.assertEqual(bank_ref_tokens("Autopay SA /OPT/IU/ap/2025/111111", "autopay"), {"AP/2025/111111"})
        self.assertEqual(bank_ref_tokens("Autopay SA /OPT/ap/2025/111111/TXT/Sklep", "autopay"), {"AP/2025/111111"})
        self.assertEqual(bank_ref_tokens("Autopay SA /OPT/ap/2026/555555/TITL/ALPHA", "autopay"), {"AP/2026/555555"})
        self.assertEqual(bank_ref_tokens("Autopay SA /OPT/ap/2026/555555/BENM/ALPHA", "autopay"), {"AP/2026/555555"})
        self.assertEqual(
            bank_ref_tokens("Autopay SA /OPT/IU/AP/2025/111111/EXTRA", "autopay"),
            {"AP/2025/111111/EXTRA"},
        )
        self.assertNotIn("AP/2025/111111", bank_ref_tokens("Autopay SA /OPT/IU/AP/2025/111111/EXTRA", "autopay"))
        self.assertNotIn(
            "AP/2025/111111",
            bank_ref_tokens("Autopay SA /OPT/IU/UNRELATED/AP/2025/111111", "autopay"),
        )
        self.assertEqual(bank_ref_tokens("Autopay AP/2025/111111/EXTRA", "autopay"), {"AP/2025/111111/EXTRA"})
        self.assertEqual(bank_ref_tokens("PayU /OPT/IU/987654/TI/Sklep", "payu"), {"987654"})
        self.assertEqual(bank_ref_tokens("PayU /OPT/IU/987654/EXTRA", "payu"), set())
        self.assertEqual(bank_ref_tokens("PayU P24-T5C-123-456", "p24"), set())
        self.assertEqual(bank_ref_tokens("Stripe P24-T5C-123-456", "p24"), set())
        self.assertEqual(bank_ref_tokens("P24-T5C-123-456.78", "p24"), set())


class PaymentExtractionExtensionTests(unittest.TestCase):
    def test_blik_nr_transakcji_and_tpay_suffix(self):
        tpay_res = inspect_payment(
            "* Tytuł płatności w systemie: TR-ABCD-1234XYZX/95069274474\n* Kwota transakcji: 83,84 PLN",
            subjects=("Potwierdzenie TR-ABCD-1234XYZX",),
            merchant="Tpay",
            gateway=True,
            provider="tpay",
        )
        self.assertEqual(tpay_res.gateway_ref, "TR-ABCD-1234XYZX")
        self.assertEqual(tpay_res.blik_ref, "95069274474")

        blik_gw = inspect_payment(
            "Potwierdzenie płatności BLIK\nNr transakcji: 95069274474\nKwota transakcji: 40,00 PLN",
            subjects=("BLIK",),
            merchant="BLIK",
            gateway=True,
            provider="blik",
        )
        self.assertEqual(blik_gw.gateway_ref, "95069274474")
        self.assertEqual(blik_gw.blik_ref, "95069274474")

        shop_blik = inspect_payment(
            "Zamówienie potwierdzone\nZapłacono: BLIK\nNumer transakcji BLIK: 95069274474\nŁącznie: 55,00 zł",
            subjects=("Zamówienie",),
            merchant="Example Shop",
            gateway=False,
        )
        self.assertIsNone(shop_blik.gateway_ref)
        self.assertEqual(shop_blik.blik_ref, "95069274474")

    def test_card_last4_extraction_and_conflict_guard(self):
        for sample, expected in [
            ("Receipt\nMastercard •••• 1234\nRazem: 49,99 zł", "1234"),
            ("Receipt\nVisa ••2177\nTotal: $19.00", "2177"),
            ("Google Play\nVisa-3290\nTotal: 29,99 zł", "3290"),
            ("Stripe\nPayment method - 0000\nAmount paid: $8.27", "0000"),
            ("Karta kończąca się na 9876\nSuma: 15,00 PLN", "9876"),
            ("Receipt\nMastercard •••• 1234\nCard ending in 9999\nRazem: 10,00 zł", None),
            ("Przelew 14 listopada 2026\nRazem: 10,00 zł", None),
        ]:
            with self.subTest(expected=expected):
                inspected = inspect_payment(sample, subjects=("Receipt",), merchant="Example", gateway=False)
                self.assertEqual(inspected.card_last4, expected)

    def test_gmail_pipeline_persists_card_last4_and_blik_ref_hmac(self):
        settings = SimpleNamespace(
            sender_allowlist=(),
            spouse_forward_senders=(),
            owner_mailbox="owner@gmail.com",
            hmac_secret=_SECRET,
            payment_senders=(
                PaymentSender("@tpay.com", "Tpay", gateway=True),
                PaymentSender("@apple.example.test", "Apple"),
            ),
        )
        worker = Mock()
        worker.ready.return_value = True
        worker.parse.side_effect = lambda _kind, source: source.decode("utf-8")
        processor = gmail_message.GmailMessageProcessor(settings, worker)

        def _proc(sender, subject, body, gid):
            msg = EmailMessage()
            msg["From"] = sender
            msg["Subject"] = subject
            dom = sender.rpartition("@")[2]
            msg["Authentication-Results"] = f"mx.google.com; dkim=pass header.d={dom}; dmarc=pass header.from={dom}"
            msg.set_content(body)
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii").rstrip("=")
            meta = {"id": gid, "internalDate": "1795566945123", "sizeEstimate": 200,
                    "payload": {"headers": [{"name": k, "value": v} for k, v in msg.raw_items()]}}
            run = Mock()
            run.message.side_effect = [meta, {"id": gid, "raw": raw}]
            return processor.process(run, gid, digest(_SECRET, b"test", gid))

        reg = _proc(
            "noreply@tpay.com",
            "Rejestracja TR-1111-2222",
            "Nowa transakcja płatnicza\nTR-1111-2222\nKwota transakcji: 40,00 PLN",
            "m-reg",
        )
        paid = _proc(
            "noreply@tpay.com",
            "Potwierdzenie transakcji TR-1111-2222",
            "Dziękujemy za dokonanie płatności.\n* Tytuł płatności w systemie: TR-1111-2222/95069274474\n"
            "* Kwota transakcji: 40,00 PLN\n* Mastercard •••• 4321",
            "m-paid",
        )
        apple = _proc(
            "billing@apple.example.test",
            "Faktura Apple",
            "Subskrypcja\nMastercard ••2177\nRazem: 419,99 zł",
            "m-apple",
        )

        with TemporaryDirectory() as directory:
            self.addCleanup(db.reset_engine_cache)
            _engine, sessions = db.init_db(Path(directory) / "pipe.db")
            try:
                with sessions.begin() as session:
                    session.add(db.AppMeta(key="users_authoritative", value="1"))
                    session.add(db.User(username="owner", password_hash="s", role="admin"))
                store = GmailStore(sessions, clock=FakeClock().wall)
                store.transition("owner", 1, "active")
                self.assertEqual(store.commit("owner", 1, {}, (reg, paid, apple)), (2, 1, True))
                with sessions() as session:
                    rows = {row.raw_shop_name: row for row in session.scalars(select(db.BudgetReceipt)).all()}
                    tpay_row = rows["Tpay"]
                    self.assertEqual(tpay_row.gateway_status, "paid")
                    self.assertEqual(tpay_row.gateway_ref_hmac, digest(_SECRET, GATEWAY_REF_DOMAIN, "TR-1111-2222"))
                    self.assertEqual(tpay_row.blik_ref_hmac, digest(_SECRET, GATEWAY_REF_DOMAIN, "95069274474"))
                    self.assertEqual(tpay_row.card_last4, "4321")
                    apple_row = rows["Apple"]
                    self.assertEqual(apple_row.card_last4, "2177")
                    self.assertIsNone(apple_row.blik_ref_hmac)
            finally:
                _engine.dispose()


if __name__ == "__main__":
    unittest.main()
