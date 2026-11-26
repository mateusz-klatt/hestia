"""ID-first receipt<->bank matching, card_last4 filter, HESTIA_BUDGET_CARD_ACCOUNTS, and no_bank_data tests."""
from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from aiohttp import web
from sqlalchemy import select

from hestia import api_contract, db, web as hestia_web
from hestia.budget import config, gmail_config, receipt_api
from hestia.budget.gmail_store import GATEWAY_REF_DOMAIN, digest
from hestia.budget.receipt_parser import ReceiptError
from hestia.budget.receipt_store import BudgetReceiptStore, _fee_adjusted_total
from tests.test_budget_receipts import AnalyticsCase, ReceiptHTTPTests, confirm_values, reference_digest

_REF_SECRET = b"synthetic-receipt-id-match-secret-32b"


def _assert_no_hmac(test: unittest.TestCase, payload: object, forbidden_values: set[str]) -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            test.assertNotIn("ref_hmac", str(key))
            test.assertNotIn(str(key), forbidden_values)
            _assert_no_hmac(test, value, forbidden_values)
        return
    if isinstance(payload, (list, tuple)):
        for item in payload:
            _assert_no_hmac(test, item, forbidden_values)
        return
    if isinstance(payload, str):
        for secret_val in forbidden_values:
            test.assertNotIn(secret_val, payload)


class CardAccountsConfigTests(unittest.TestCase):
    def test_load_card_accounts_and_load_settings_validation(self):
        self.assertEqual(dict(gmail_config.load_card_accounts(environ={})), {})
        self.assertEqual(dict(gmail_config.load_card_accounts(environ={gmail_config.CARD_ACCOUNTS_ENV: ""})), {})
        valid_env = {
            gmail_config.CARD_ACCOUNTS_ENV: json.dumps({"1234": ["acc-1", "acc_2"], "0000": ["acc-3"]}),
        }
        loaded = gmail_config.load_card_accounts(environ=valid_env)
        self.assertEqual(dict(loaded), {"1234": ("acc-1", "acc_2"), "0000": ("acc-3",)})
        self.assertIsNone(gmail_config.load_settings(environ=valid_env))

        invalid_payloads = [
            " ",
            "not-json",
            "[]",
            "null",
            '{"1234": NaN}',
            '{"1234": ["acc-1"], "1234": ["acc-2"]}',
            json.dumps({"123": ["acc-1"]}),
            json.dumps({"12345": ["acc-1"]}),
            json.dumps({"12ab": ["acc-1"]}),
            json.dumps({"1234": "acc-1"}),
            json.dumps({"1234": []}),
            json.dumps({"1234": [""]}),
            json.dumps({"1234": ["acc 1"]}),
            json.dumps({"1234": [123]}),
            json.dumps({"1234": ["acc-1", "acc-1"]}),
            json.dumps({f"{i:04d}": ["acc-1"] for i in range(65)}),
            json.dumps({"1234": [f"acc-{i}" for i in range(33)]}),
            "x" * (16 * 1024 + 1),
        ]
        for raw in invalid_payloads:
            with self.subTest(raw=raw[:40]):
                env = {gmail_config.CARD_ACCOUNTS_ENV: raw}
                with self.assertRaises(config.BudgetConfigError):
                    gmail_config.load_card_accounts(environ=env)
                with self.assertRaises(config.BudgetConfigError):
                    gmail_config.load_settings(environ=env)

        with self.assertRaises(config.BudgetConfigError):
            gmail_config.normalize_card_accounts(None)

    def test_load_ref_hmac_secret(self):
        self.assertIsNone(gmail_config.load_ref_hmac_secret(environ={}))
        with TemporaryDirectory() as directory:
            secret_file = Path(directory) / "hmac.key"
            secret_file.write_bytes(b"a" * 32)
            os.chmod(secret_file, 0o600)
            self.assertEqual(
                gmail_config.load_ref_hmac_secret(
                    environ={gmail_config.HMAC_FILE: str(secret_file)},
                    session_secret=b"b" * 32,
                ),
                b"a" * 32,
            )
            with self.assertRaises(config.BudgetConfigError):
                gmail_config.load_ref_hmac_secret(
                    environ={gmail_config.HMAC_FILE: str(secret_file)},
                    session_secret=b"a" * 32,
                )
            short_file = Path(directory) / "short.key"
            short_file.write_bytes(b"short")
            os.chmod(short_file, 0o600)
            with self.assertRaises(config.BudgetConfigError):
                gmail_config.load_ref_hmac_secret(environ={gmail_config.HMAC_FILE: str(short_file)})


class ReceiptIdMatchingTests(AnalyticsCase):
    def setUp(self):
        super().setUp()
        self.receipts = BudgetReceiptStore(
            self.sessions,
            clock=lambda: self.now,
            cursor_secret=b"receipts-only",
            ref_hmac_secret=_REF_SECRET,
            card_accounts={
                "2177": [self.accounts["b"]],
                "0000": [self.accounts["b"], self.accounts["a"]],
                "8888": ["nonexistent-account"],
            },
        )

    def _insert_receipt(
        self,
        *,
        receipt_id: str,
        gateway: str | None = None,
        raw_ref: str | None = None,
        blik_raw_ref: str | None = None,
        total_amount: str = "120",
        fee_amount: str | None = None,
        receipt_date: str = "2026-11-15",
        card_last4: str | None = None,
        shop_name: str = "SKLEP PRZYKŁADOWY",
        payment_method: str = "card",
        status: str = "imported",
    ) -> None:
        gw_hmac = digest(_REF_SECRET, GATEWAY_REF_DOMAIN, raw_ref) if raw_ref is not None else None
        blik_hmac = digest(_REF_SECRET, GATEWAY_REF_DOMAIN, blik_raw_ref) if blik_raw_ref is not None else None
        with self.sessions.begin() as session:
            session.add(db.BudgetReceipt(
                id=receipt_id,
                owner="owner",
                raw_shop_name=shop_name,
                receipt_kind="purchase",
                receipt_date=receipt_date,
                total_amount=total_amount,
                currency="PLN",
                payment_method=payment_method,
                status=status,
                source_kind="email",
                document_kind="payment_confirmation",
                gateway=gateway,
                gateway_status="paid" if gateway else None,
                gateway_ref_hmac=gw_hmac,
                blik_ref_hmac=blik_hmac,
                card_last4=card_last4,
                fee_amount=fee_amount,
                version=1,
                created_at=self.now,
                updated_at=self.now,
            ))
            session.add(db.BudgetReceiptLine(
                receipt_id=receipt_id,
                owner="owner",
                line_no=1,
                line_kind="other",
                raw_name="Zakup",
                raw_name_key="zakup",
                quantity="1",
                gross_line_total=total_amount,
                net_line_total=total_amount,
            ))

    def test_p24_id_outranks_nip_and_hides_hmacs(self):
        # Add malformed / non-BOOK rows to verify resilience of _unique_id_matches and _earliest_payload_date.
        with self.sessions.begin() as session:
            session.add_all([
                db.BudgetTransaction(
                    account_id=self.accounts["a"], entry_reference="tx-list-json",
                    payload_json="[]", observed_at=1.0, uncertain=False,
                ),
                db.BudgetTransaction(
                    account_id=self.accounts["a"], entry_reference="tx-pdng",
                    payload_json=json.dumps({
                        "transaction_date": "2026-11-15", "amount": "120.00", "currency": "PLN",
                        "credit_debit_indicator": "DBIT", "status": "PDNG",
                        "remittance_information": ["P24-T5C-123-456"],
                    }),
                    observed_at=1.0, uncertain=False,
                ),
                db.BudgetTransaction(
                    account_id=self.accounts["a"], entry_reference="tx-bad-fp",
                    payload_json=json.dumps({"transaction_date": "2026-11-15", "status": "BOOK"}),
                    observed_at=1.0, uncertain=False,
                ),
            ])
        ref = "P24-T5C-123-456"
        self.add_transaction(
            "a", "tx-nip", amount="120.00", booking_date="2026-11-15", value_date="2026-11-15",
            creditor={"name": "SKLEP"}, remittance_information=["NIP 7791011327 Zamowienie"],
        )
        self.add_transaction(
            "a", "tx-p24", amount="120.00", booking_date="2026-11-15", value_date="2026-11-15",
            creditor={"name": "PayPro SA P24-T5C-123-456"}, remittance_information=["SKLEP"],
        )
        self._insert_receipt(receipt_id="10000000-0000-4000-8000-000000000001", gateway="p24", raw_ref=ref)
        with self.sessions.begin() as session:
            row = session.get(db.BudgetReceipt, "10000000-0000-4000-8000-000000000001")
            row.raw_nip = "7791011327"

        detail = self.receipts.detail("owner", "10000000-0000-4000-8000-000000000001")
        self.assertEqual([c["entry_reference"] for c in detail["candidates"]],
                         [reference_digest("tx-p24"), reference_digest("tx-nip")])
        self.assertEqual(detail["candidates"][0]["match_strength"], "id")
        self.assertEqual(detail["candidates"][1]["match_strength"], "none")
        self.assertFalse(detail["no_bank_data"])

        forbidden = {digest(_REF_SECRET, GATEWAY_REF_DOMAIN, ref), ref}
        _assert_no_hmac(self, detail, forbidden)
        _assert_no_hmac(self, self.receipts.list("owner", {"limit": 10}), forbidden)
        _assert_no_hmac(self, self.receipts.search("owner", {"query": "SKLEP", "limit": 10}), forbidden)

    def test_duplicate_p24_id_in_window_kills_id_strength(self):
        ref = "P24-DUP-111-222"
        self.add_transaction("a", "tx-1", amount="120.00", booking_date="2026-11-15", value_date="2026-11-15",
                             remittance_information=["P24-DUP-111-222"])
        self.add_transaction("a", "tx-2", amount="10.00", booking_date="2026-11-16", value_date="2026-11-16",
                             remittance_information=["P24-DUP-111-222"])
        self._insert_receipt(receipt_id="10000000-0000-4000-8000-000000000002", gateway="Przelewy24", raw_ref=ref)

        detail = self.receipts.detail("owner", "10000000-0000-4000-8000-000000000002")
        self.assertEqual(len(detail["candidates"]), 1)
        self.assertEqual(detail["candidates"][0]["match_strength"], "none")

        # Duplicate outside date window does not collide with the in-window row.
        self.add_transaction("a", "tx-outside", amount="120.00", booking_date="2026-11-01", value_date="2026-11-01",
                             remittance_information=["P24-UNQ-111-222"])
        self.add_transaction("a", "tx-inside", amount="120.00", booking_date="2026-11-15", value_date="2026-11-15",
                             remittance_information=["P24-UNQ-111-222"])
        self._insert_receipt(
            receipt_id="10000000-0000-4000-8000-000000000003",
            gateway="Przelewy24",
            raw_ref="P24-UNQ-111-222",
        )
        detail_unq = self.receipts.detail("owner", "10000000-0000-4000-8000-000000000003")
        self.assertEqual(detail_unq["candidates"][0]["match_strength"], "id")

    def test_grouped_transaction_is_hidden_and_an_issued_id_cannot_take_it(self):
        tables = [model.__table__ for model in (
            db.BudgetReceipt, db.BudgetReceiptLine, db.BudgetReceiptLinkGroup,
            db.BudgetReceiptLinkMember, db.BudgetAnalyticsAudit,
        )]

        def stored_state():
            with self.sessions() as session:
                return [session.execute(select(table).order_by(*table.primary_key.columns)).all() for table in tables]

        for index, status in enumerate(("confirmed", "needs_review")):
            with self.subTest(status=status):
                identifier = f"10000000-0000-4000-8000-{80 + index:012d}"
                entry = f"tx-group-{status}"
                reference = f"P24-GRP-111-22{index}"
                amount = str(120 + index)
                bank_fields = dict(amount=amount, booking_date="2026-11-15", value_date="2026-11-15",
                                   remittance_information=[reference])
                self.add_transaction("a", entry, **bank_fields)
                self._insert_receipt(receipt_id=identifier, gateway="Przelewy24", raw_ref=reference, total_amount=amount)
                issued = self.receipts.detail("owner", identifier)
                candidate, = issued["candidates"]
                self.assertEqual((candidate["match_strength"], candidate["entry_reference"]),
                                 ("id", reference_digest(entry)))
                values = confirm_values({"receipt_id": identifier, "version": issued["receipt"]["version"],
                                         "draft": issued["receipt"], "candidates": issued["candidates"]},
                                        link={"account_id": self.accounts["a"], "entry_reference": entry})

                group_id = f"group-{status}"
                members = [f"10000000-0000-4000-8000-{90 + 2 * index + offset:012d}" for offset in range(2)]
                for member, total in zip(members, ("60", str(60 + index))):
                    self._insert_receipt(receipt_id=member, status=status, total_amount=total)
                with self.sessions.begin() as session:
                    session.add(db.BudgetReceiptLinkGroup(
                        id=group_id, owner="owner", account_id=self.accounts["a"], entry_reference=entry,
                        currency="PLN", tx_amount=amount, credit_debit_indicator="DBIT", status=status,
                        linked_fingerprint=self.fingerprint("a", entry), linked_evidence="{}", version=1,
                        created_at=self.now, updated_at=self.now,
                    ))
                    session.flush()
                    session.add_all(db.BudgetReceiptLinkMember(group_id=group_id, receipt_id=member, owner="owner")
                                    for member in members)

                self.assertEqual(self.receipts.detail("owner", identifier)["candidates"], [])
                before = stored_state()
                with mock.patch.object(self.receipts, "_shop_id", wraps=self.receipts._shop_id) as shop:
                    with self.assertRaises(ReceiptError) as reserved:
                        self.receipts.confirm("owner", values)
                    shop.assert_not_called()
                self.assertEqual((reserved.exception.code, reserved.exception.status),
                                 ("budget_receipt_tx_already_linked", 409))
                self.assertEqual(stored_state(), before)

                self.add_transaction("b", entry, **bank_fields)
                visible, = self.receipts.detail("owner", identifier)["candidates"]
                self.assertEqual((visible["account_id"], visible["entry_reference"], visible["match_strength"]),
                                 (self.accounts["b"], reference_digest(entry), "none"))

    def test_grouped_members_and_evidence_reject_legacy_link_edits_and_delete(self):
        for index, status in enumerate(("confirmed", "needs_review")):
            group_id = f"protected-group-{status}"
            group_entry = f"tx-protected-group-{status}"
            member, other_member, evidence = (
                f"10000000-0000-4000-8000-{100 + 3 * index + offset:012d}" for offset in range(3)
            )
            self.add_transaction("a", group_entry, amount="122", booking_date="2026-11-15", value_date="2026-11-15")
            self.add_transaction("a", f"tx-unreserved-member-{status}", amount="60", booking_date="2026-11-15",
                                 value_date="2026-11-15")
            self.add_transaction("a", f"tx-unreserved-evidence-{status}", amount="122", booking_date="2026-11-15",
                                 value_date="2026-11-15")
            for receipt_id in (member, other_member):
                self._insert_receipt(receipt_id=receipt_id, status=status, total_amount="60")
            self._insert_receipt(receipt_id=evidence, status=status, total_amount="122", fee_amount="2", gateway="PayU")
            issued = {}
            for role, receipt_id in (("member", member), ("evidence", evidence)):
                with self.sessions() as session:
                    row = session.get(db.BudgetReceipt, receipt_id)
                    candidates = self.receipts._candidates(session, "owner", row, {})
                reference = f"tx-unreserved-{role}-{status}"
                issued[role] = next(candidate for candidate in candidates
                                    if candidate["entry_reference"] == reference_digest(reference))
            with self.sessions.begin() as session:
                session.add(db.BudgetReceiptLinkGroup(
                    id=group_id, owner="owner", account_id=self.accounts["a"], entry_reference=group_entry,
                    currency="PLN", tx_amount="122", credit_debit_indicator="DBIT", status=status,
                    fee_amount="2", evidence_receipt_id=evidence,
                    linked_fingerprint=self.fingerprint("a", group_entry), linked_evidence="{}", version=1,
                    created_at=self.now, updated_at=self.now,
                ))
                session.flush()
                session.add_all(db.BudgetReceiptLinkMember(group_id=group_id, receipt_id=receipt_id, owner="owner")
                                for receipt_id in (member, other_member))

            for role, receipt_id in (("member", member), ("evidence", evidence)):
                with self.subTest(status=status, role=role):
                    detail = self.receipts.detail("owner", receipt_id)
                    self.assertEqual(detail.get("candidates", []), [])
                    with self.sessions() as session:
                        row = session.get(db.BudgetReceipt, receipt_id)
                        self.assertEqual(self.receipts._candidates(session, "owner", row, {}), [])
                    parsed = {"receipt_id": receipt_id, "version": detail["receipt"]["version"],
                              "draft": detail["receipt"]}
                    confirm = confirm_values(parsed, lines=None)
                    delete = {"receipt_id": receipt_id, "expected_version": confirm["expected_version"]}
                    for operation, values in ((self.receipts.confirm, confirm), (self.receipts.delete, delete)):
                        with self.assertRaises(ReceiptError) as stale:
                            operation("owner", values | {"expected_version": 2})
                        self.assertEqual((stale.exception.code, stale.exception.status), ("budget_receipt_conflict", 409))

                    link = {key: issued[role][key] for key in ("account_id", "entry_reference", "candidate_token")}
                    for field, value in (
                        ("raw_shop_name", "OTHER SHOP"), ("raw_nip", "7791011327"), ("receipt_kind", "refund"),
                        ("receipt_date", "2026-11-16"), ("total_amount", "61"), ("currency", "EUR"),
                        ("payment_method", "blik"), ("link", link),
                    ):
                        with self.subTest(field=field):
                            with self.assertRaises(ReceiptError) as in_group:
                                self.receipts.confirm("owner", confirm | {field: value})
                            self.assertEqual((in_group.exception.code, in_group.exception.status),
                                             ("budget_receipt_in_group", 409))
                    with self.assertRaises(ReceiptError) as cannot_delete:
                        self.receipts.delete("owner", delete)
                    self.assertEqual((cannot_delete.exception.code, cannot_delete.exception.status),
                                     ("budget_receipt_in_group", 409))
                    self.assertEqual(self.receipts.detail("owner", receipt_id)["receipt"], detail["receipt"])

                    # Null link and omitted merchant fields preserve the group. Normalize money on input as usual.
                    confirm.pop("raw_shop_name")
                    confirm.pop("raw_nip")
                    updated = self.receipts.confirm("owner", confirm | {
                        "receipt_time": "12:30", "total_amount": confirm["total_amount"] + ".00",
                    })["receipt"]
                    self.assertEqual((updated["status"], updated["version"], updated["receipt_time"]), (status, 2, "12:30"))
                    with self.sessions() as session:
                        group = session.get(db.BudgetReceiptLinkGroup, group_id)
                        self.assertEqual((group.version, group.status, group.evidence_receipt_id), (1, status, evidence))
                        self.assertEqual(set(session.scalars(select(db.BudgetReceiptLinkMember.receipt_id).where(
                            db.BudgetReceiptLinkMember.group_id == group_id))), {member, other_member})
                        audit, = session.scalars(select(db.BudgetAnalyticsAudit).where(
                            db.BudgetAnalyticsAudit.subject_type == "receipt",
                            db.BudgetAnalyticsAudit.subject_id == receipt_id,
                        ))
                        self.assertEqual(audit.action, "confirm")
                        for payload in (audit.before_json, audit.after_json):
                            self.assertTrue(json.loads(payload)["linked"])
                            self.assertNotIn(group_entry, payload)
                            self.assertNotIn(detail["receipt"]["raw_shop_name"], payload)

    def test_cross_gateway_never_matches(self):
        self.add_transaction("a", "tx-p24", amount="120.00", booking_date="2026-11-15", value_date="2026-11-15",
                             remittance_information=["/OPT/IU/P24-T5C-123-456"])
        self._insert_receipt(
            receipt_id="10000000-0000-4000-8000-000000000004",
            gateway="Tpay",
            raw_ref="P24-T5C-123-456",
        )
        detail = self.receipts.detail("owner", "10000000-0000-4000-8000-000000000004")
        self.assertEqual(detail["candidates"][0]["match_strength"], "none")

    def test_wrong_amount_rejected_and_amount_with_fee_matches_and_confirms(self):
        self.add_transaction("a", "tx-wrong", amount="99.00", booking_date="2026-11-15", value_date="2026-11-15",
                             remittance_information=["P24-WRG-111-222"])
        self._insert_receipt(
            receipt_id="10000000-0000-4000-8000-000000000009",
            gateway="Przelewy24",
            raw_ref="P24-WRG-111-222",
            total_amount="121.5",
            fee_amount="1.5",
        )
        self.assertEqual(self.receipts.detail("owner", "10000000-0000-4000-8000-000000000009")["candidates"], [])

        self._insert_receipt(
            receipt_id="10000000-0000-4000-8000-000000000005",
            gateway="Przelewy24",
            raw_ref="P24-FEE-111-222",
            total_amount="121.5",
            fee_amount="1.5",
        )
        self.add_transaction("a", "tx-base", amount="120.00", booking_date="2026-11-15", value_date="2026-11-15",
                             remittance_information=["P24-FEE-111-222"])
        detail = self.receipts.detail("owner", "10000000-0000-4000-8000-000000000005")
        self.assertEqual(len(detail["candidates"]), 1)
        self.assertEqual(detail["candidates"][0]["match_strength"], "id")
        confirmed = self.receipts.confirm("owner", confirm_values({
            "receipt_id": detail["receipt"]["receipt_id"],
            "version": detail["receipt"]["version"],
            "draft": detail["receipt"],
            "candidates": detail["candidates"],
        }, link={"account_id": self.accounts["a"], "entry_reference": detail["candidates"][0]["entry_reference"]}))
        self.assertTrue(confirmed["receipt"]["linked"])

        # Zero / oversized fees return None from _fee_adjusted_total, and non-id cannot confirm total - fee.
        self.assertIsNone(_fee_adjusted_total("100", "0"))
        self.assertIsNone(_fee_adjusted_total("10", "20"))
        self.add_transaction("a", "tx-too-low", amount="90.00", booking_date="2026-11-15",
                             value_date="2026-11-15", remittance_information=["SKLEP"])
        self.add_transaction("a", "tx-nofee-match", amount="98.00", booking_date="2026-11-15",
                             value_date="2026-11-15", remittance_information=["SKLEP"])
        self._insert_receipt(
            receipt_id="10000000-0000-4000-8000-000000000006",
            gateway="Przelewy24",
            raw_ref="P24-NO-SUCH-REF",
            total_amount="100",
            fee_amount="2",
        )
        detail2 = self.receipts.detail("owner", "10000000-0000-4000-8000-000000000006")
        self.assertEqual(detail2["candidates"], [])
        with self.sessions() as session:
            row = session.get(db.BudgetReceipt, "10000000-0000-4000-8000-000000000006")
            cleaned = {
                "raw_shop_name": "SKLEP", "raw_nip": None, "receipt_kind": "purchase",
                "receipt_date": "2026-11-15", "total_amount": "100", "currency": "PLN",
                "payment_method": "card", "document_kind": "payment_confirmation", "due_date": None,
            }
            for ref_name in ("tx-too-low", "tx-nofee-match"):
                with self.subTest(ref_name=ref_name), self.assertRaises(ReceiptError):
                    self.receipts._verify_link(
                        session, "owner", cleaned,
                        {"account_id": self.accounts["a"], "entry_reference": ref_name},
                        row.id, row=row,
                    )

    def test_tpay_autopay_payu_paynow_blik_and_opt_cases(self):
        cases = [
            ("10000000-0000-4000-8000-000000000010", "Tpay", "TR-ABCD-1234XYZX", None,
             "/OPT/IU/TR-ABCD-1234XYZX/95069274474", "Krajowy Integrator"),
            ("10000000-0000-4000-8000-000000000011", "Tpay", "TR-9999-AAAA", "95069274474",
             "Płatność BLIK 15.11.2026 Nr transakcji 95069274474", "SKLEP"),
            ("10000000-0000-4000-8000-000000000012", "Autopay", "ABCD_123456_PL_22_11_2026", None,
             "/OPT/IU/ABCD_123456_PL_22_11_2026", "Autopay SA"),
            ("10000000-0000-4000-8000-000000000013", "PayU", "987654321", None,
             "/OPT/IU/987654321 Sklep", "PayU SA"),
            ("10000000-0000-4000-8000-000000000014", "Paynow", "AB12-CDE-FGH-IJK", None,
             "PRZELEW /OPT/AB12-CDE-FGH-IJK", "mElements SA"),
            ("10000000-0000-4000-8000-000000000015", None, None, "77788899900",
             "Płatność BLIK Nr transakcji 77788899900", "SKLEP BLIK"),
        ]
        for idx, (rid, gw, raw_ref, blik_ref, remit, cred) in enumerate(cases):
            with self.subTest(gateway=gw, rid=rid):
                self.add_transaction("a", f"tx-case-{idx}", amount=f"{10 + idx}.00", booking_date="2026-11-15",
                                     value_date="2026-11-15", remittance_information=[remit], creditor={"name": cred})
                self._insert_receipt(
                    receipt_id=rid, gateway=gw, raw_ref=raw_ref, blik_raw_ref=blik_ref,
                    total_amount=str(10 + idx), payment_method="blik" if blik_ref else "card",
                )
                detail = self.receipts.detail("owner", rid)
                self.assertEqual(len(detail["candidates"]), 1)
                self.assertEqual(detail["candidates"][0]["match_strength"], "id")

    def test_secondary_appmeta_gateway_ref_payee_guard_and_opt_exact_match(self):
        # Issue 2 (positive): secondary ref matches as "id" only when bank text names the receipt's merchant cleanly.
        rid_pos = "10000000-0000-4000-8000-000000000016"
        secondary_ref = "AP/2026/998877"
        self.add_transaction(
            "a", "tx-sec-pos", amount="64.00", booking_date="2026-11-15", value_date="2026-11-15",
            remittance_information=[f"/OPT/IU/{secondary_ref} MERCHANT A nr 42"], creditor={"name": "Autopay SA"},
        )
        self._insert_receipt(
            receipt_id=rid_pos, shop_name="MERCHANT A", gateway="Autopay",
            raw_ref="SYNTH0003CC", total_amount="64",
        )
        with self.sessions.begin() as session:
            session.add(db.AppMeta(
                key=f"budget_gmail_gateway_ref:{rid_pos}",
                value=json.dumps([digest(_REF_SECRET, GATEWAY_REF_DOMAIN, secondary_ref)]),
            ))
        detail = self.receipts.detail("owner", rid_pos)
        self.assertEqual(len(detail["candidates"]), 1)
        self.assertEqual(detail["candidates"][0]["match_strength"], "id")

        # Duplicate secondary ref still makes the issued "id" candidate stale on confirm.
        self.add_transaction(
            "a", "tx-sec-dup", amount="64.00", booking_date="2026-11-16", value_date="2026-11-16",
            remittance_information=[f"/OPT/IU/{secondary_ref} MERCHANT A"], creditor={"name": "Autopay SA"},
        )
        with self.assertRaises(ReceiptError) as raised:
            self.receipts.confirm("owner", confirm_values({
                "receipt_id": detail["receipt"]["receipt_id"],
                "version": detail["receipt"]["version"],
                "draft": detail["receipt"],
                "candidates": detail["candidates"],
            }, link={"account_id": self.accounts["a"], "entry_reference": detail["candidates"][0]["entry_reference"]}))
        self.assertEqual(raised.exception.code, "budget_receipt_stale_source")

        # Issue 2 (negatives): cross-merchant, missing-merchant, extra-merchant, and generic-only receipts must NOT get "id".
        neg_cases = [
            ("10000000-0000-4000-8000-000000000017", "MERCHANT A", "AP/2026/111117",
             "/OPT/IU/AP/2026/111117 MERCHANT B", "71"),
            ("10000000-0000-4000-8000-000000000018", "MERCHANT A", "AP/2026/111118",
             "/OPT/IU/AP/2026/111118", "72"),
            ("10000000-0000-4000-8000-000000000019", "MERCHANT A", "AP/2026/111119",
             "/OPT/IU/AP/2026/111119 MERCHANT A OTHER", "73"),
            ("10000000-0000-4000-8000-00000000002a", "MERCHANT A", "AP/2026/111120",
             "/OPT/IU/AP/2026/111120 MERCHANT A B", "74"),
            ("10000000-0000-4000-8000-00000000002b", "Sklep", "AP/2026/111121",
             "/OPT/IU/AP/2026/111121 Sklep", "75"),
            # Issue 1 end-to-end: longer /OPT/ path with /EXTRA suffix must NOT match AP/2025/111111.
            ("10000000-0000-4000-8000-00000000002c", "MERCHANT A", "AP/2025/111111",
             "/OPT/IU/AP/2025/111111/EXTRA MERCHANT A", "76"),
            # Delta review follow-up: trailing SWIFT fields after /OPT/<id> must still trigger different-merchant guard.
            ("10000000-0000-4000-8000-00000000002e", "ALPHA", "AP/2026/555555",
             "/OPT/AP/2026/555555/TXT/BETA ALPHA", "78"),
            ("10000000-0000-4000-8000-00000000002f", "ALPHA", "AP/2026/555556",
             "/OPT/AP/2026/555556/TITL/BETA ALPHA", "79"),
            ("10000000-0000-4000-8000-000000000030", "ALPHA", "AP/2026/555557",
             "/OPT/AP/2026/555557/BENM/BETA ALPHA", "80"),
            ("10000000-0000-4000-8000-000000000032", "ALPHA", "BETA5555",
             "/OPT/BETA5555/TXT/BETA5555 ALPHA", "82"),
            ("10000000-0000-4000-8000-000000000033", "ALPHA", "ALPHA/2026/555559",
             "/OPT/ALPHA/2026/555559", "83"),
            ("10000000-0000-4000-8000-000000000036", "ALPHA", "ALPHA/2026/555562",
             "/TI/12345 ALPHA/2026/555562", "86"),
            ("10000000-0000-4000-8000-000000000037", "ALPHA", "BETA5556",
             "/OPT/BETA5556/OPT/ALPHA", "87"),
            ("10000000-0000-4000-8000-00000000003a", "ALPHA", "BETA5557",
             "/OPT/BETA5557/TXT/ALPHA BETA5557", "90"),
            ("10000000-0000-4000-8000-00000000003b", "ALPHA", "BETA5558",
             "BETA5558 /TXT/ALPHA BETA5558", "91"),
        ]
        for rid, shop, sec_ref, remit, amt in neg_cases:
            with self.subTest(rid=rid, remit=remit):
                self.add_transaction(
                    "a", f"tx-{rid[-2:]}", amount=f"{amt}.00", booking_date="2026-11-15", value_date="2026-11-15",
                    remittance_information=[remit], creditor={"name": "Autopay SA"},
                )
                self._insert_receipt(
                    receipt_id=rid, shop_name=shop, gateway="Autopay",
                    raw_ref=f"PRIM-{rid[-4:]}", total_amount=amt,
                )
                with self.sessions.begin() as session:
                    session.add(db.AppMeta(
                        key=f"budget_gmail_gateway_ref:{rid}",
                        value=json.dumps([digest(_REF_SECRET, GATEWAY_REF_DOMAIN, sec_ref)]),
                    ))
                res = self.receipts.detail("owner", rid)
                self.assertEqual(len(res["candidates"]), 1)
                self.assertEqual(res["candidates"][0]["match_strength"], "none")

        positive_cases = [
            ("10000000-0000-4000-8000-000000000031", "AP/2026/555558", "/OPT/AP/2026/555558/TXT/ALPHA", "81"),
            ("10000000-0000-4000-8000-000000000034", "AP/2026/555560", "/opt/ap/2026/５５５５６０/txt/ALPHA", "84"),
            ("10000000-0000-4000-8000-000000000035", "AP/2026/555561", "AP/2026/555561 ALPHA", "85"),
            ("10000000-0000-4000-8000-000000000038", "AP/2026/555563", "AP/2026/555563 /TXT/ALPHA", "88"),
            ("10000000-0000-4000-8000-000000000039", "REF555564", "/TXT/ALPHA REF555564", "89"),
        ]
        for rid, sec_ref, remit, amt in positive_cases:
            with self.subTest(rid=rid, remit=remit):
                self.add_transaction(
                    "a", f"tx-{rid[-2:]}", amount=f"{amt}.00", booking_date="2026-11-15", value_date="2026-11-15",
                    remittance_information=[remit], creditor={"name": "Autopay SA"},
                )
                self._insert_receipt(
                    receipt_id=rid, shop_name="ALPHA", gateway="Autopay",
                    raw_ref=f"PRIM-{rid[-4:]}", total_amount=amt,
                )
                with self.sessions.begin() as session:
                    session.add(db.AppMeta(
                        key=f"budget_gmail_gateway_ref:{rid}",
                        value=json.dumps([digest(_REF_SECRET, GATEWAY_REF_DOMAIN, sec_ref)]),
                    ))
                detail = self.receipts.detail("owner", rid)
                self.assertEqual(len(detail["candidates"]), 1)
                self.assertEqual(detail["candidates"][0]["match_strength"], "id")

        # Primary ref keeps its existing rule even without merchant tokens in remittance.
        rid_prim = "10000000-0000-4000-8000-00000000002d"
        self.add_transaction(
            "a", "tx-prim-only", amount="77.00", booking_date="2026-11-15", value_date="2026-11-15",
            remittance_information=["/OPT/IU/PRIM999000 UNRELATED"], creditor={"name": "Autopay SA"},
        )
        self._insert_receipt(
            receipt_id=rid_prim, shop_name="MERCHANT A", gateway="Autopay",
            raw_ref="PRIM999000", total_amount="77",
        )
        prim_detail = self.receipts.detail("owner", rid_prim)
        self.assertEqual(prim_detail["candidates"][0]["match_strength"], "id")

    def test_card_last4_filter_unmapped_fallback_and_no_bank_data(self):
        # Add transactions starting 2026-08-01 on account "a", while account "b" (card 2177) has 2026-09-15.
        self.add_transaction("a", "tx-aug-a", amount="49.99", booking_date="2026-08-01",
                             value_date="2026-08-01", remittance_information=["BIEDRONKA"])
        self.add_transaction("a", "tx-sep-main", amount="49.99", booking_date="2026-09-15",
                             value_date="2026-09-15", remittance_information=["BIEDRONKA"])
        self.add_transaction("b", "tx-sep-card", amount="49.99", booking_date="2026-09-15",
                             value_date="2026-09-15", remittance_information=["BIEDRONKA"])

        # Mapped card_last4 ("2177" -> [account b]) restricts candidates to account b only.
        self._insert_receipt(
            receipt_id="10000000-0000-4000-8000-000000000020",
            total_amount="49.99",
            receipt_date="2026-09-15",
            card_last4="2177",
            shop_name="BIEDRONKA",
        )
        detail_mapped = self.receipts.detail("owner", "10000000-0000-4000-8000-000000000020")
        self.assertEqual([c["account_id"] for c in detail_mapped["candidates"]], [self.accounts["b"]])
        self.assertEqual(detail_mapped["candidates"][0]["match_strength"], "distinctive")
        self.assertEqual(detail_mapped["receipt"]["card_last4"], "2177")
        self.assertFalse(detail_mapped["no_bank_data"])

        # Unmapped card_last4 ("9999") applies no filter -> candidates from both accounts a & b.
        self._insert_receipt(
            receipt_id="10000000-0000-4000-8000-000000000021",
            total_amount="49.99",
            receipt_date="2026-09-15",
            card_last4="9999",
            shop_name="Apple",
        )
        detail_unmapped = self.receipts.detail("owner", "10000000-0000-4000-8000-000000000021")
        self.assertEqual(
            {c["account_id"] for c in detail_unmapped["candidates"]},
            {self.accounts["a"], self.accounts["b"]},
        )

        # Pre-Sep receipt with card_last4="2177" (mapped to account b whose earliest tx is 2026-09-15) -> no_bank_data=True,
        # while unmapped receipt on same date is covered by account a (earliest 2026-08-01) -> no_bank_data=False.
        with self.sessions.begin() as session:
            session.add(db.BudgetTransaction(
                account_id=self.accounts["b"], entry_reference="tx-b-non-dict",
                payload_json="[]", observed_at=1.0, uncertain=False,
            ))
        self._insert_receipt(
            receipt_id="10000000-0000-4000-8000-000000000022",
            total_amount="49.99",
            receipt_date="2026-08-15",
            card_last4="2177",
            shop_name="Apple",
        )
        pre_sep_card = self.receipts.detail("owner", "10000000-0000-4000-8000-000000000022")
        self.assertEqual(pre_sep_card["candidates"], [])
        self.assertTrue(pre_sep_card["no_bank_data"])

        self._insert_receipt(
            receipt_id="10000000-0000-4000-8000-000000000023",
            total_amount="49.99",
            receipt_date="2026-08-15",
            card_last4="9999",
            shop_name="Apple",
        )
        self.assertFalse(self.receipts.detail("owner", "10000000-0000-4000-8000-000000000023")["no_bank_data"])

        # Receipt earlier than every enabled account -> no_bank_data=True.
        self._insert_receipt(
            receipt_id="10000000-0000-4000-8000-000000000024",
            total_amount="49.99",
            receipt_date="2026-07-15",
            card_last4=None,
            shop_name="Apple",
        )
        self.assertTrue(self.receipts.detail("owner", "10000000-0000-4000-8000-000000000024")["no_bank_data"])

        # Mapped to non-existent account -> empty candidates and no_bank_data=False.
        self._insert_receipt(
            receipt_id="10000000-0000-4000-8000-000000000025",
            total_amount="49.99",
            receipt_date="2026-09-15",
            card_last4="8888",
            shop_name="Apple",
        )
        missing_acc = self.receipts.detail("owner", "10000000-0000-4000-8000-000000000025")
        self.assertEqual(missing_acc["candidates"], [])
        self.assertFalse(missing_acc["no_bank_data"])

        # Cash receipt is non-matchable -> no_bank_data=False even before earliest date.
        self._insert_receipt(
            receipt_id="10000000-0000-4000-8000-000000000026",
            total_amount="49.99",
            receipt_date="2026-07-15",
            payment_method="cash",
            shop_name="Apple",
        )
        self.assertFalse(self.receipts.detail("owner", "10000000-0000-4000-8000-000000000026")["no_bank_data"])

        # Card mapped to an enabled account with 0 transactions -> no_bank_data=True.
        with self.sessions.begin() as session:
            conn_id = session.get(db.BudgetAccount, self.accounts["a"]).connection_id
            session.add(db.BudgetAccount(
                id="acc-empty", connection_id=conn_id, owner="owner",
                identification_hash="empty-hash", enabled=True, balances_json="[]",
            ))
        store_empty_card = BudgetReceiptStore(
            self.sessions, clock=lambda: self.now, cursor_secret=b"receipts-only",
            card_accounts={"5555": ["acc-empty"]},
        )
        self._insert_receipt(
            receipt_id="10000000-0000-4000-8000-000000000027",
            total_amount="49.99",
            receipt_date="2026-09-15",
            card_last4="5555",
            shop_name="Apple",
        )
        self.assertTrue(store_empty_card.detail("owner", "10000000-0000-4000-8000-000000000027")["no_bank_data"])

    def test_kimi_review_detail_and_stale_id_confirm_regressions(self):
        # 1. BLIK "Nr transakcji 123456ABC" must NOT match ref 123456 through detail().
        self.add_transaction("a", "tx-blik-abc", amount="33.00", booking_date="2026-11-15",
                             value_date="2026-11-15", remittance_information=["Nr transakcji 123456ABC"])
        self._insert_receipt(
            receipt_id="10000000-0000-4000-8000-000000000030",
            gateway="BLIK",
            raw_ref="123456",
            blik_raw_ref="123456",
            total_amount="33",
            payment_method="blik",
        )
        self.assertEqual(self.receipts.detail("owner", "10000000-0000-4000-8000-000000000030")["candidates"][0]["match_strength"], "none")

        # 2. PayU "ORDER.987654" must NOT match ref 987654 through detail().
        self.add_transaction("a", "tx-payu-dot", amount="44.00", booking_date="2026-11-15",
                             value_date="2026-11-15", creditor={"name": "PayU S.A."},
                             remittance_information=["ORDER.987654"])
        self._insert_receipt(
            receipt_id="10000000-0000-4000-8000-000000000031",
            gateway="PayU",
            raw_ref="987654",
            total_amount="44",
        )
        self.assertEqual(self.receipts.detail("owner", "10000000-0000-4000-8000-000000000031")["candidates"][0]["match_strength"], "none")

        # 3. Autopay receipt 456789 must NOT match bank text "PayU order 456789" through detail().
        self.add_transaction("a", "tx-autopay-cross", amount="55.00", booking_date="2026-11-15",
                             value_date="2026-11-15", creditor={"name": "Sklep"},
                             remittance_information=["PayU order 456789"])
        self._insert_receipt(
            receipt_id="10000000-0000-4000-8000-000000000032",
            gateway="Autopay",
            raw_ref="456789",
            total_amount="55",
        )
        self.assertEqual(self.receipts.detail("owner", "10000000-0000-4000-8000-000000000032")["candidates"][0]["match_strength"], "none")

        # 4. Stale exact-amount "id" token is rejected with 409 budget_receipt_stale_source once a duplicate ref appears.
        self.add_transaction("a", "tx-p24-first", amount="66.00", booking_date="2026-11-15",
                             value_date="2026-11-15", remittance_information=["P24-STL-111-222"])
        self._insert_receipt(
            receipt_id="10000000-0000-4000-8000-000000000033",
            gateway="Przelewy24",
            raw_ref="P24-STL-111-222",
            total_amount="66",
        )
        issued = self.receipts.detail("owner", "10000000-0000-4000-8000-000000000033")
        self.assertEqual(issued["candidates"][0]["match_strength"], "id")

        self.add_transaction("a", "tx-p24-second", amount="66.00", booking_date="2026-11-16",
                             value_date="2026-11-16", remittance_information=["P24-STL-111-222"])
        refreshed = self.receipts.detail("owner", "10000000-0000-4000-8000-000000000033")
        self.assertTrue(all(c["match_strength"] == "none" for c in refreshed["candidates"]))

        with self.assertRaises(ReceiptError) as caught:
            self.receipts.confirm(
                "owner",
                confirm_values(
                    {"receipt_id": issued["receipt"]["receipt_id"], "version": 1,
                     "draft": issued["receipt"], "candidates": issued["candidates"]},
                    link={"account_id": self.accounts["a"], "entry_reference": issued["candidates"][0]["entry_reference"]},
                ),
            )
        self.assertEqual((caught.exception.code, caught.exception.status), ("budget_receipt_stale_source", 409))

        # Direct _verify_link with issued_as_id=True rejects wrong tx amount even when unique ID holds.
        self.add_transaction("a", "tx-p24-bad-amt", amount="77.00", booking_date="2026-11-15",
                             value_date="2026-11-15", remittance_information=["P24-BAD-111-222"])
        self._insert_receipt(
            receipt_id="10000000-0000-4000-8000-000000000034",
            gateway="Przelewy24",
            raw_ref="P24-BAD-111-222",
            total_amount="88",
        )
        with self.sessions() as session:
            row = session.get(db.BudgetReceipt, "10000000-0000-4000-8000-000000000034")
            with self.assertRaises(ReceiptError):
                self.receipts._verify_link(
                    session,
                    "owner",
                    {
                        "raw_shop_name": "SKLEP PRZYKŁADOWY", "raw_nip": None, "receipt_kind": "purchase",
                        "receipt_date": "2026-11-15", "total_amount": "88", "currency": "PLN",
                        "payment_method": "card", "document_kind": "payment_confirmation", "due_date": None,
                    },
                    {"account_id": self.accounts["a"], "entry_reference": "tx-p24-bad-amt"},
                    row.id,
                    row=row,
                    issued_as_id=True,
                )


class ReceiptIdMatchingHTTPTests(ReceiptHTTPTests):
    async def test_http_contract_056_gating_card_accounts_and_id_confirm(self):
        now = hestia_web._now()
        acc_main, acc_card = "acc-http-main", "acc-http-card"
        with self.sessions.begin() as session:
            session.add(db.BudgetConnection(
                id="conn-http-id", owner="alice", application_id="app", bank_name="Bank", country="PL",
                enabled=True, next_attempt=0, failures=0,
            ))
            session.flush()
            session.add_all([
                db.BudgetAccount(
                    id=acc_main, connection_id="conn-http-id", owner="alice",
                    identification_hash="hash-http-main", enabled=True, balances_json="{}",
                ),
                db.BudgetAccount(
                    id=acc_card, connection_id="conn-http-id", owner="alice",
                    identification_hash="hash-http-card", enabled=True, balances_json="{}",
                ),
            ])
        with TemporaryDirectory() as directory:
            secret_file = Path(directory) / "hmac.key"
            secret_file.write_bytes(_REF_SECRET)
            os.chmod(secret_file, 0o600)
            with mock.patch.dict(os.environ, {
                gmail_config.HMAC_FILE: str(secret_file),
                gmail_config.CARD_ACCOUNTS_ENV: json.dumps({"2177": [acc_card]}),
            }):
                with self.sessions.begin() as session:
                    session.add_all([
                        db.BudgetTransaction(
                            account_id=acc_main, entry_reference="http-p24",
                            payload_json=json.dumps({
                                "transaction_date": "2026-11-15", "booking_date": "2026-11-15",
                                "value_date": "2026-11-15", "amount": "120.00", "currency": "PLN",
                                "credit_debit_indicator": "DBIT", "status": "BOOK",
                                "remittance_information": ["/OPT/IU/P24-HTP-111-222"],
                                "creditor": {"name": "Przelewy24"},
                            }),
                            observed_at=now, uncertain=False,
                        ),
                        db.BudgetTransaction(
                            account_id=acc_card, entry_reference="http-card",
                            payload_json=json.dumps({
                                "transaction_date": "2026-11-15", "booking_date": "2026-11-15",
                                "value_date": "2026-11-15", "amount": "50.00", "currency": "PLN",
                                "credit_debit_indicator": "DBIT", "status": "BOOK",
                                "remittance_information": ["BIEDRONKA"],
                            }),
                            observed_at=now, uncertain=False,
                        ),
                        db.BudgetReceipt(
                            id="20000000-0000-4000-8000-000000000001", owner="alice",
                            raw_shop_name="SKLEP HTTP", receipt_kind="purchase", receipt_date="2026-11-15",
                            total_amount="121.5", fee_amount="1.5", currency="PLN", payment_method="card",
                            status="imported", source_kind="email", document_kind="payment_confirmation",
                            gateway="Przelewy24", gateway_status="paid",
                            gateway_ref_hmac=digest(_REF_SECRET, GATEWAY_REF_DOMAIN, "P24-HTP-111-222"),
                            card_last4="9999", version=1, created_at=now, updated_at=now,
                        ),
                        db.BudgetReceiptLine(
                            receipt_id="20000000-0000-4000-8000-000000000001", owner="alice",
                            line_no=1, line_kind="other", raw_name="Zamowienie", raw_name_key="zamowienie",
                            quantity="1", gross_line_total="121.5", net_line_total="121.5",
                        ),
                        db.BudgetReceipt(
                            id="20000000-0000-4000-8000-000000000002", owner="alice",
                            raw_shop_name="Apple", receipt_kind="purchase", receipt_date="2026-08-01",
                            total_amount="50", currency="PLN", payment_method="card",
                            status="imported", source_kind="email", document_kind="payment_confirmation",
                            card_last4="2177", version=1, created_at=now, updated_at=now,
                        ),
                    ])

                headers_057 = {**self.headers("alice", origin=True), "X-Hestia-Contract-Version": "0.57.0"}
                headers_056 = {**self.headers("alice", origin=True), "X-Hestia-Contract-Version": "0.56.0"}
                headers_055 = {**self.headers("alice", origin=True), "X-Hestia-Contract-Version": "0.55.0"}

                res_057 = await self.get("detail", "20000000-0000-4000-8000-000000000001", headers=headers_057)
                self.assertEqual(res_057.status, 200)
                body_057 = await res_057.json()
                api_contract.BudgetReceiptDetailResult.model_validate(body_057)
                self.assertEqual(body_057["candidates"][0]["match_strength"], "id")
                self.assertEqual(body_057["receipt"]["card_last4"], "9999")
                self.assertFalse(body_057["no_bank_data"])
                _assert_no_hmac(self, body_057, {digest(_REF_SECRET, GATEWAY_REF_DOMAIN, "P24-HTP-111-222")})

                # Below 0.57.0 (both 0.56.0 and 0.55.0): hide fee-adjusted candidates and strip 0.57.0 fields.
                for old_headers in (headers_056, headers_055):
                    res_old = await self.get("detail", "20000000-0000-4000-8000-000000000001", headers=old_headers)
                    self.assertEqual(res_old.status, 200)
                    body_old = await res_old.json()
                    self.assertEqual(body_old["candidates"], [])
                    self.assertNotIn("card_last4", body_old["receipt"])
                    self.assertNotIn("no_bank_data", body_old)

                # Exact-amount "id" match on 0.55.0 is mapped to "none" (never "nip"), so old clients never preselect it.
                with self.sessions.begin() as session:
                    session.add_all([
                        db.BudgetTransaction(
                            account_id=acc_main, entry_reference="http-p24-exact",
                            payload_json=json.dumps({
                                "transaction_date": "2026-11-15", "booking_date": "2026-11-15",
                                "value_date": "2026-11-15", "amount": "200.00", "currency": "PLN",
                                "credit_debit_indicator": "DBIT", "status": "BOOK",
                                "remittance_information": ["P24-EXA-111-222"],
                                "creditor": {"name": "Przelewy24"},
                            }),
                            observed_at=now, uncertain=False,
                        ),
                        db.BudgetReceipt(
                            id="20000000-0000-4000-8000-000000000003", owner="alice",
                            raw_shop_name="SKLEP EXACT", receipt_kind="purchase", receipt_date="2026-11-15",
                            total_amount="200", currency="PLN", payment_method="card",
                            status="imported", source_kind="email", document_kind="payment_confirmation",
                            gateway="Przelewy24", gateway_status="paid",
                            gateway_ref_hmac=digest(_REF_SECRET, GATEWAY_REF_DOMAIN, "P24-EXA-111-222"),
                            version=1, created_at=now, updated_at=now,
                        ),
                        db.BudgetReceiptLine(
                            receipt_id="20000000-0000-4000-8000-000000000003", owner="alice",
                            line_no=1, line_kind="other", raw_name="Zamowienie", raw_name_key="zamowienie",
                            quantity="1", gross_line_total="200", net_line_total="200",
                        ),
                    ])
                exact_057 = await (await self.get(
                    "detail", "20000000-0000-4000-8000-000000000003", headers=headers_057,
                )).json()
                self.assertEqual(exact_057["candidates"][0]["match_strength"], "id")
                exact_055 = await (await self.get(
                    "detail", "20000000-0000-4000-8000-000000000003", headers=headers_055,
                )).json()
                self.assertEqual(exact_055["candidates"][0]["match_strength"], "none")

                # Confirm fee-adjusted ID match via HTTP on 0.57.0.
                cand = body_057["candidates"][0]
                confirmed = await self.post(
                    "confirm",
                    confirm_values(
                        {"receipt_id": body_057["receipt"]["receipt_id"], "version": 1,
                         "draft": body_057["receipt"], "candidates": body_057["candidates"]},
                        link={"account_id": cand["account_id"], "entry_reference": cand["entry_reference"]},
                    ),
                    headers=headers_057,
                )
                self.assertEqual(confirmed.status, 200, await confirmed.text())
                conf_body = await confirmed.json()
                api_contract.BudgetReceiptConfirmResult.model_validate(conf_body)
                self.assertTrue(conf_body["receipt"]["linked"])

                # Card-mapped pre-Sep gap returns no_bank_data=True on 0.57.0 and is stripped on 0.56.0/0.55.0.
                gap_057 = await (await self.get(
                    "detail", "20000000-0000-4000-8000-000000000002", headers=headers_057,
                )).json()
                self.assertTrue(gap_057["no_bank_data"])
                self.assertEqual(gap_057["candidates"], [])
                gap_056 = await (await self.get(
                    "detail", "20000000-0000-4000-8000-000000000002", headers=headers_056,
                )).json()
                self.assertNotIn("no_bank_data", gap_056)

                # Stale exact-amount ID token must return 409 budget_receipt_stale_source once a duplicate ref appears.
                with self.sessions.begin() as session:
                    session.add(db.BudgetTransaction(
                        account_id=acc_main, entry_reference="http-p24-dup",
                        payload_json=json.dumps({
                            "transaction_date": "2026-11-16", "booking_date": "2026-11-16",
                            "value_date": "2026-11-16", "amount": "200.00", "currency": "PLN",
                            "credit_debit_indicator": "DBIT", "status": "BOOK",
                            "remittance_information": ["P24-EXA-111-222"],
                            "creditor": {"name": "Przelewy24"},
                        }),
                        observed_at=now, uncertain=False,
                    ))
                stale_res = await self.post(
                    "confirm",
                    confirm_values(
                        {"receipt_id": exact_057["receipt"]["receipt_id"], "version": 1,
                         "draft": exact_057["receipt"], "candidates": exact_057["candidates"]},
                        link={"account_id": acc_main, "entry_reference": exact_057["candidates"][0]["entry_reference"]},
                    ),
                    headers=headers_057,
                )
                self.assertEqual(stale_res.status, 409)
                self.assertEqual((await stale_res.json())["error"], "budget_receipt_stale_source")


if __name__ == "__main__":
    unittest.main()
