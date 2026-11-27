"""Synthetic ledger, privacy, query-plan and HTTP tests for the receipt picker."""
from __future__ import annotations

from datetime import UTC, date, datetime
from functools import partial
import json
import os
from unittest import IsolatedAsyncioTestCase, mock
from uuid import uuid4

from sqlalchemy import select

from hestia import api_contract, db
from hestia.budget import device_token, receipt_api, receipt_transaction_search as picker
from hestia.budget.receipt_parser import ReceiptError
from tests import test_budget_api as api_fixture
from tests.test_budget_analytics_store import AnalyticsCase
from tests.test_budget_receipts import confirm_values, receipt_text


def _receipt(store, owner):
    return store.parse_text(owner, receipt_text())


class ReceiptTransactionSearchTests(AnalyticsCase):
    def setUp(self):
        super().setUp()
        self.picker = picker.ReceiptTransactionSearch(self.sessions, clock=lambda: self.now, cursor_secret=b"synthetic")
        self.anchor = _receipt(self.picker, "owner")
        self.change_transaction("a", "debit", remittance_information=["Biedronka"])
        self.change_transaction("d", "debit-2", remittance_information=["Biedronka"])

    def _search(self, **filters):
        return self.picker.search_transactions("owner", {"receipt_id": self.anchor["receipt_id"], **filters})

    def _confirm(self, receipt, item, **changes):
        values = confirm_values(receipt, link={"transaction_id": item["id"]}, **changes)
        return self.picker.confirm_manual("owner", values)

    def _reserve(self, account, reference, member, evidence=None):
        group_id = str(uuid4())
        with self.sessions.begin() as session:
            session.add(db.BudgetReceiptLinkGroup(
                id=group_id, owner="owner", account_id=self.accounts[account], entry_reference=reference,
                currency="PLN", tx_amount="100", credit_debit_indicator="DBIT", evidence_receipt_id=evidence,
                status="needs_review", linked_fingerprint="synthetic", linked_evidence="{}",
                version=1, created_at=self.now, updated_at=self.now,
            ))
            session.flush()
            session.add(db.BudgetReceiptLinkMember(group_id=group_id, owner="owner", receipt_id=member))

    def test_filters_and_private_display(self):
        def search(**filters):
            return self._search(**({"wide": True, "amount_min": "0"} | filters))["items"]

        self.add_transaction("a", "lookup", amount="120.000000000000000001", booking_date="2026-12-30",
                             creditor={"name": "Fallback Shop"}, description="Synthetic title",
                             remittance_information=["Ordered Łyżki", "", 42])
        self.metadata("a", "lookup", {"creditor_name": "Łódź Market", "creditor_accounts": [
            {"scheme": "IBAN", "identification": "PL61109010140000071219812874"},
        ]})
        self.add_transaction("a", "early", amount="125", booking_date="2026-10-30")
        self.add_transaction("a", "late", amount="130", booking_date="2027-01-30")
        self.add_transaction("a", "euro", amount="120", currency="EUR", booking_date="2026-12-01")
        self.add_transaction("a", "pending", amount="120", status="PDNG", booking_date="2026-12-01")
        self.add_transaction("a", "masked", amount="115", booking_date="2026-11-28",
                             creditor_name="PL61109010140000071219812874",
                             remittance_information=["Sensitive 123456789012345678", "Domestic 87654321"])
        self.add_transaction("a", "uncertain", amount="110", booking_date="2026-12-01")
        self.change_transaction("a", "uncertain", uncertain=True)
        with self.sessions.begin() as session:
            session.add(db.BudgetAccountDisplay(
                account_id=self.accounts["a"], product="Synthetic debit card", masked_suffix="1234",
                observed_at=self.now, imported_at=self.now, digest="synthetic"
            ))
        exact, masked, lookup = search(account_id=self.accounts["a"])
        self.assertEqual([item["amount"] for item in (exact, masked, lookup)], ["100", "115", "120.000000000000000001"])
        self.assertEqual(lookup["counterparty"], "Łódź Market")
        self.assertTrue(lookup["account_label"].endswith("••1234"))
        self.assertNotIn("61109010140000071219812874", json.dumps(masked))
        self.assertNotIn("123456789012345678", json.dumps(masked))
        self.assertNotIn("87654321", json.dumps(masked))
        self.assertNotIn("61109010140000071219812874", json.dumps(lookup))
        self.assertEqual(search(query="  LODZ  "), [lookup])
        self.assertEqual(search(query="lyzki"), [lookup])
        self.assertEqual(search(query="synthetic title"), [lookup])
        self.assertEqual(search(query="' OR 1=1 --"), [])
        self.assertEqual(search(query="%"), [])
        self.assertEqual(search(query="87654321"), [])
        self.assertEqual(search(account_id=self.accounts["foreign"]), [])
        self.assertEqual(search(amount_min="120.000000000000000002"), [])
        self.assertEqual(search(amount_min=None, amount_max="120")[0]["amount"], "100")
        self.assertEqual(search(amount_min="110", date_from="2026-11-27", date_to="2026-11-29"), [masked])
        self.assertEqual(search(date_from=None, date_to=None, amount_min="121"), [])

    def test_date_defaults_are_bound_sql_parameters(self):
        today = date(2026, 12, 3)
        with self.sessions.begin() as session:
            receipt = session.get(db.BudgetReceipt, self.anchor["receipt_id"])

            def bounds(values):
                filters = picker._filters(values, receipt, today)
                params = picker._query("owner", receipt.id, filters, None, 25).compile().params
                return params["picker_start"], params["picker_end"]

            self.assertEqual(bounds({}), ("2026-10-31", "2027-01-29"))
            self.assertEqual(bounds({"date_from": None, "date_to": None}), ("2026-10-31", "2027-01-29"))
            self.assertEqual(bounds({"date_from": "2026-11-01"}), ("2026-11-01", "2027-01-29"))
            self.assertEqual(bounds({"date_from": "2025-01-01", "date_to": "2026-02-05"}), ("2025-01-01", "2026-02-05"))
            receipt.receipt_date = None
            self.assertEqual(bounds({}), ("2026-09-04", "2026-12-03"))
            self.assertEqual(bounds({"receipt_date": "2026-08-20"}), ("2026-07-21", "2026-10-19"))

    def test_merchant_and_amount_tolerance_with_wide_toggle(self):
        for reference, amount, shop in (
            ("minus", "95", "Biedronka"),
            ("plus", "105", "Biedronka"),
            ("medium", "106", "Biedronka"),
            ("different", "100.50", "Biedronkarnia"),
            ("wide-limit", "120", "Other merchant"),
            ("above-limit", "120.01", "Biedronka"),
        ):
            self.add_transaction("a", reference, amount=amount, creditor_name=shop)
        self.assertEqual([item["amount_delta"] for item in self._search()["items"]], ["0", "0", "-5", "5"])
        self.assertEqual([item["amount_delta"] for item in self._search(wide=True)["items"]],
                         ["0", "0", "0.5", "-5", "5", "6", "20"])
        self.assertEqual([item["amount"] for item in self._search(query="biedronka", amount_min="106")["items"]],
                         ["106", "120.01"])

    def test_gateway_total_plus_fee_is_included_and_labeled(self):
        with self.sessions.begin() as session:
            receipt = session.get(db.BudgetReceipt, self.anchor["receipt_id"])
            receipt.raw_shop_name, receipt.raw_nip, receipt.gateway = "Example Utilities", None, "Autopay"
            receipt.fee_amount = "6.89"
        self.add_transaction("a", "base", amount="100", creditor_name="Example Utilities")
        self.add_transaction("a", "fee", amount="106.89", creditor_name="Autopay")
        base, fee = self._search()["items"]
        self.assertEqual((base["amount_delta"], base["includes_fee"]), ("0", False))
        self.assertEqual((fee["amount_delta"], fee["includes_fee"]), ("6.89", True))
        with self.sessions.begin() as session:
            session.get(db.BudgetReceipt, self.anchor["receipt_id"]).fee_amount = "0"
        self.assertEqual(self._search()["items"], [base])

    def test_partial_forward_uses_draft_fields_and_existing_confirm(self):
        receipt_id = str(uuid4())
        with self.sessions.begin() as session:
            session.add(db.BudgetReceipt(id=receipt_id, owner="owner", status="needs_review", source_kind="email",
                                         document_kind="invoice", forwarded_manually=1, raw_shop_name="przekazane ręcznie",
                                         created_at=self.now, updated_at=self.now, version=1))
        self.now = datetime(2026, 12, 3, tzinfo=UTC).timestamp()
        broad = self.picker.search_transactions("owner", {"receipt_id": receipt_id})["items"]
        self.assertEqual(len(broad), 2)
        self.assertIsNone(broad[0]["amount_delta"])

        self.add_transaction("a", "forwarded", amount="28", currency="EUR", booking_date="2026-08-25")
        self.add_transaction("a", "wrong-date", amount="28.77", currency="EUR", booking_date="2026-10-20")
        self.add_transaction("a", "wrong-amount", amount="32", currency="EUR", booking_date="2026-08-20")
        draft = {"receipt_id": receipt_id, "receipt_date": "2026-08-20", "total_amount": "28.77", "currency": "EUR"}
        item, = self.picker.search_transactions("owner", draft)["items"]
        self.assertEqual((item["date"], item["amount_delta"], item["currency"]), ("2026-08-25", "-0.77", "EUR"))
        confirm = {
            **draft, "expected_version": 1, "receipt_kind": "purchase", "receipt_time": None,
            "payment_method": "unknown", "link": {"transaction_id": item["id"]},
            "lines": [{"line_kind": "other", "raw_name": "Synthetic invoice", "ptu_code": None, "quantity": "1",
                       "raw_unit": None, "unit_price_raw": None, "gross_line_total": "28.77", "discount_amount": "0",
                       "net_line_total": "28.77"}],
        }
        with self.assertRaises(ReceiptError) as mismatch:
            self.picker.confirm_manual("owner", {**confirm, "currency": "PLN"})
        self.assertEqual(mismatch.exception.status, 409)
        saved = self.picker.confirm_manual("owner", confirm)["receipt"]
        self.assertTrue(saved["linked"])
        self.assertEqual((saved["receipt_date"], saved["total_amount"], saved["currency"]), ("2026-08-20", "28.77", "EUR"))

    def test_pagination_and_cursor_scope(self):
        self.add_transaction("a", "same-day-a", amount="101", creditor_name="Biedronka")
        self.add_transaction("d", "same-day-d", amount="99", creditor_name="Biedronka")
        self.add_transaction("a", "later-date", amount="99", booking_date="2026-12-03", creditor_name="Biedronka")
        self.add_transaction("a", "decimal-first", amount="100.999999999999999999", booking_date="2026-12-07",
                             creditor_name="Biedronka")
        expected = self._search()["items"]
        self.assertEqual([(item["date"], item["amount"]) for item in expected], [
            ("2026-11-30", "100"), ("2026-11-29", "100"), ("2026-12-07", "100.999999999999999999"),
            ("2026-11-30", "101"), ("2026-11-30", "99"), ("2026-12-03", "99"),
        ])
        cursor, collected = None, []
        for _ in expected:
            page = self._search(limit=1, cursor=cursor)
            collected.extend(page["items"])
            cursor = page["next_cursor"]
        self.assertEqual(collected, expected)
        self.assertIsNone(cursor)
        first_cursor = self._search(limit=1)["next_cursor"]
        for invalid in ({"cursor": first_cursor, "query": "different"}, {"cursor": "broken"},
                        {"date_from": "2026-11-31"}, {"date_from": "2027-02-01"},
                        {"receipt_date": "2026-04-31"}, {"date_from": "2020-01-01"},
                        {"amount_min": "3", "amount_max": "2"}):
            with self.subTest(invalid=invalid), self.assertRaises(ReceiptError) as error:
                self._search(**invalid)
            self.assertEqual(error.exception.status, 400)

    def test_direct_and_group_reservations_rechecked_by_confirmation(self):
        other = _receipt(self.picker, "owner")
        member = _receipt(self.picker, "owner")
        self.picker.confirm("owner", confirm_values(member))
        evidence = _receipt(self.picker, "owner")
        self.picker.confirm("owner", confirm_values(evidence))
        with self.sessions.begin() as session:
            session.get(db.BudgetReceipt, evidence["receipt_id"]).gateway = "synthetic"
        first, second = self._search()["items"]
        same_first = self.picker.search_transactions("owner", {"receipt_id": other["receipt_id"]})["items"][0]
        saved = self._confirm(other, same_first)["receipt"]
        self.assertTrue(saved["linked"])
        self.assertEqual(self._search()["items"], [second])
        self.assertEqual(self.picker.search_transactions("owner", {"receipt_id": other["receipt_id"]})["items"][0], same_first)
        self._reserve("d", "debit-2", member["receipt_id"], evidence["receipt_id"])
        self.assertEqual(self._search()["items"], [])
        for own in (member, evidence):
            rows = self.picker.search_transactions("owner", {"receipt_id": own["receipt_id"]})["items"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["date"], second["date"])
        for occupied in (first, second):
            with self.assertRaises(ReceiptError) as error:
                self._confirm(self.anchor, occupied)
            self.assertEqual((error.exception.status, error.exception.code), (409, "budget_receipt_tx_already_linked"))

    def test_manual_selection_outside_suggestion_amount_and_date_then_reuses_own_link(self):
        self.add_transaction("b", "outside", amount="101.01", booking_date="2026-09-25", creditor_name="Biedronka")
        item, = self._search(account_id=self.accounts["b"], date_from="2026-09-01")["items"]
        result = self._confirm(self.anchor, item)
        self.assertEqual(result["receipt"]["linked_account_id"], self.accounts["b"])
        self.assertEqual(result["receipt"]["version"], 2)
        self.assertTrue(self._confirm(self.anchor, item, expected_version=2)["receipt"]["linked"])

    def test_transfer_leg_is_excluded_and_rejected_on_link(self):
        first, second = self._search()["items"]
        self.decide()
        self.assertEqual(self._search()["items"], [second])
        with self.assertRaises(ReceiptError) as error:
            self._confirm(self.anchor, first)
        self.assertEqual((error.exception.status, error.exception.code), (409, "budget_receipt_tx_already_linked"))

    def test_changed_or_invalid_transaction_returns_conflict(self):
        first, second = self._search()["items"]
        self.change_transaction("a", amount="101")
        self.change_transaction("d", reference="debit-2", status="PDNG")
        for item, changes in (
            (first, {}), (second, {}), (first, {"receipt_kind": "refund"}), (first, {"currency": "EUR"}),
            ({"id": "9999999999999999999." + "0" * 64}, {}),
            ({"id": "987654321." + "0" * 64}, {}),
        ):
            with self.subTest(item=item, changes=changes), self.assertRaises(ReceiptError) as error:
                self._confirm(self.anchor, item, **changes)
            self.assertEqual((error.exception.status, error.exception.code), (409, "budget_receipt_stale_source"))
        for changed in ({"currency": "USD"}, {"credit_debit_indicator": "CRDT"}):
            self.change_transaction("a", currency="PLN", credit_debit_indicator="DBIT")
            self.change_transaction("a", **changed)
            with self.assertRaises(ReceiptError) as error:
                self._confirm(self.anchor, first)
            self.assertEqual(error.exception.status, 409)
        with self.sessions.begin() as session:
            session.get(db.BudgetTransaction, (self.accounts["a"], "debit")).payload_json = "{"
        with self.assertRaises(ReceiptError) as error:
            self._confirm(self.anchor, first)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self._search()["items"], [])

    def test_fallback_date_null_anchor_metadata_and_safe_labels(self):
        self.add_transaction("a", "fallback", amount="100", booking_date=None, value_date="2026-12-02",
                             creditor="not a party object", remittance_information=None)
        self.add_transaction("a", "overlong-title", amount="100", booking_date=None, value_date=None, transaction_date="2026-11-28",
                             creditor_name="   ", title="X" * 40000)
        self.add_transaction("a", "undated", amount="100", booking_date=None, value_date=None, transaction_date=None)
        self.add_transaction("a", "invalid-fallback", amount="100", booking_date=None, value_date=None,
                             transaction_date="2026-11-31")
        self.add_transaction("a", "invalid-amount", booking_date="2026-12-02", amount="bad")
        self.change_transaction("a", "debit", creditor_name="Original Name")
        fingerprint = self.fingerprint("a", "debit")
        self.metadata("a", "debit", {"creditor_name": "Untrusted stale override"}, fingerprint="stale")
        with self.sessions.begin() as session:
            session.get(db.BudgetReceipt, self.anchor["receipt_id"]).receipt_date = None
            session.get(db.BudgetTransactionMetadata, (self.accounts["a"], "debit")).ledger_fingerprint = fingerprint
            session.get(db.BudgetTransactionMetadata, (self.accounts["a"], "debit")).payload_json = "{"
            session.add(db.BudgetAccountDisplay(
                account_id=self.accounts["a"], product="", masked_suffix="12345678",
                observed_at=self.now, imported_at=self.now, digest="synthetic"
            ))
        self.now = datetime(2026, 12, 3, tzinfo=UTC).timestamp()
        fallback, named, overlong = self._search(account_id=self.accounts["a"], wide=True)["items"]
        self.assertEqual((fallback["date"], fallback["counterparty"]), ("2026-12-02", None))
        self.assertEqual(named["counterparty"], "Original Name")
        self.assertEqual(overlong["date"], "2026-11-28")
        self.assertIsNone(overlong["title"])
        self.assertEqual(self._search(date_from="0001-01-01", date_to="0001-01-01", wide=True)["items"], [])
        self.assertNotIn("12345678", json.dumps(named))

    def test_search_plan_bounds_ledger_and_reservation_index_lookups(self):
        fixture_payload = self.transaction().payload_json
        with self.engine.begin() as connection:
            connection.exec_driver_sql("""
                WITH RECURSIVE synthetic(i) AS (VALUES (1) UNION ALL SELECT i + 1 FROM synthetic WHERE i < 12000)
                INSERT INTO budget_transactions(account_id, entry_reference, payload_json, observed_at, uncertain)
                SELECT ?, 'synthetic-' || i,
                       json_set(?, '$.booking_date', date('1980-01-01', '+' || i || ' days')), 1, 0
                FROM synthetic
            """, (self.accounts["a"], fixture_payload))
            connection.execute(db.BudgetReceipt.__table__.insert(), [
                dict(id=str(uuid4()), owner="owner", receipt_kind="purchase", receipt_date="2000-01-01",
                     total_amount="100", currency="PLN", payment_method="card", status="confirmed", source_kind="paste",
                     linked_account_id=self.accounts["a"], linked_entry_ref=f"synthetic-{number}",
                     linked_fingerprint="synthetic", linked_evidence="{}", version=1, created_at=1, updated_at=1)
                for number in range(1, 81)
            ])
            connection.exec_driver_sql("ANALYZE")
        with self.sessions() as session:
            receipt = session.get(db.BudgetReceipt, self.anchor["receipt_id"])
            filters = picker._filters({"amount_min": "100"}, receipt, date(2026, 11, 30))
            for account_id in (None, self.accounts["a"]):
                for cursor in (None, ("0010", 0, 123)):
                    statement = picker._query("owner", receipt.id, filters._replace(account_id=account_id), cursor, 10)
                    compiled = statement.compile(self.engine, compile_kwargs={"render_postcompile": True})
                    connection = session.connection()
                    connection.connection.driver_connection.create_function("hestia_receipt_tx_rank", 3,
                                                                            partial(picker._rank, filters=filters))
                    plan = connection.exec_driver_sql(
                        "EXPLAIN QUERY PLAN " + compiled.string,
                        tuple(compiled.params[key] for key in compiled.positiontup),
                    ).all()
                    details = "\n".join(row[3] for row in plan)
                    self.assertIn("SEARCH budget_transactions USING INDEX ix_budget_transactions_account_booking_date", details)
                    self.assertIn("account_id=? AND <expr>>? AND <expr><?", details)
                    self.assertIn("SEARCH budget_receipts USING INDEX uq_budget_receipts_tx_link", details)
                    self.assertNotIn("SCAN budget_transactions", details)


class ReceiptTransactionSearchHTTPTests(IsolatedAsyncioTestCase):
    rows = api_fixture.BudgetAPITests.rows
    token = api_fixture.BudgetAPITests.token
    headers = api_fixture.BudgetAPITests.headers
    get = api_fixture.BudgetAPITests.get
    post = api_fixture.BudgetAPITests.post

    async def asyncSetUp(self):
        flags = mock.patch.dict(os.environ, {receipt_api.FLAG: "1", "HESTIA_BUDGET_JOBS_ENABLED": "0",
                                            "HESTIA_BUDGET_POLLER_ENABLED": "0"})
        flags.start()
        self.addCleanup(flags.stop)
        await api_fixture.BudgetAPITests.asyncSetUp(self)
        response = await self.post("receipts/parse-text", {"text": receipt_text()})
        self.assertEqual(response.status, 200, await response.text())
        self.anchor = await response.json()

    async def _post(self, endpoint, body, *, user="alice", version="0.67.0", origin=True):
        headers = self.headers(user, origin=origin)
        if version is not None:
            headers["X-Hestia-Contract-Version"] = version
        return await self.post("receipts/" + endpoint, body, headers=headers)

    async def test_contract_owner_privacy_framing_and_advertisement(self):
        body = {"receipt_id": self.anchor["receipt_id"], "wide": True, "amount_min": "0"}
        for version in (None, "0.66.0"):
            response = await self._post("transaction-search", body, version=version)
            self.assertEqual((response.status, (await response.json())["error"]), (404, "budget_receipt_not_found"))
        for version in ("0.68.0", "garbage"):
            response = await self._post("transaction-search", body, version=version)
            self.assertEqual((response.status, (await response.json())["error"]),
                             (400, "budget_receipt_contract_version_invalid"))
        self.assertEqual((await self._post("transaction-search", body, user="bob")).status, 404)
        self.assertEqual((await self._post("transaction-search", body, origin=False)).status, 403)
        self.assertEqual((await self._post("transaction-search", {**body, "limit": 51})).status, 400)
        too_wide = await self._post("transaction-search", {**body, "date_from": "2025-01-01", "date_to": "2026-02-06"})
        self.assertEqual(too_wide.status, 400)
        self.assertEqual((await self.client.post(receipt_api.PREFIX + "/transaction-search", data="{}", headers={
            **self.headers(origin=True), "X-Hestia-Contract-Version": "0.67.0",
        })).status, 415)
        response = await self._post("transaction-search", body)
        self.assertEqual(response.status, 200, await response.text())
        api_fixture.BudgetAPITests.assert_private_headers(self, response)
        result = api_contract.BudgetReceiptTransactionSearchResult.model_validate(await response.json())
        self.assertEqual([item.title for item in result.items], ["Alice private description"])
        result = await self.get("connections")
        self.assertTrue((await result.json())["capabilities"]["receipt_transaction_search"])
        with mock.patch.dict(os.environ, {receipt_api.FLAG: "0"}):
            self.assertEqual((await self._post("transaction-search", body)).status, 404)

    async def test_existing_confirm_route_with_manual_choice_and_standard_candidate_shape(self):
        body = {"receipt_id": self.anchor["receipt_id"], "wide": True, "amount_min": "0"}
        response = await self._post("transaction-search", body)
        item, = (await response.json())["items"]
        confirm = confirm_values(self.anchor, link={"transaction_id": item["id"]})
        self.assertEqual((await self._post("confirm", confirm, version="0.66.0")).status, 400)
        self.assertEqual((await self._post("confirm", confirm, version="invalid")).status, 400)
        response = await self._post("confirm", confirm)
        self.assertEqual(response.status, 200, await response.text())
        saved = (await response.json())["receipt"]
        self.assertTrue(saved["linked"])
        response = await self._post("confirm", confirm_values(self.anchor, expected_version=saved["version"]))
        self.assertEqual(response.status, 200, await response.text())
        self.assertFalse((await response.json())["receipt"]["linked"])

    async def test_confirm_conflict_when_ledger_is_claimed_after_search(self):
        response = await self.post("receipts/parse-text", {"text": receipt_text()})
        other = await response.json()
        selections = []
        for receipt in (other, self.anchor):
            response = await self._post("transaction-search", {"receipt_id": receipt["receipt_id"],
                                                               "wide": True, "amount_min": "0"})
            item, = (await response.json())["items"]
            selections.append(confirm_values(receipt, link={"transaction_id": item["id"]}))
        self.assertEqual((await self._post("confirm", selections[0])).status, 200)
        response = await self._post("confirm", selections[1])
        self.assertEqual((response.status, (await response.json())["error"]), (409, "budget_receipt_tx_already_linked"))

    async def test_picker_and_manual_confirmation_require_browser(self):
        key_file = self.path.parent / "synthetic-rule.key"
        key_file.touch(mode=0o600)
        key_file.write_bytes(b"k" * 32)
        key = mock.patch.dict(os.environ, {"HESTIA_BUDGET_RULE_KEY_FILE": str(key_file)})
        key.start()
        self.addCleanup(key.stop)
        response = await self.client.post(device_token.PAIR_PATH, json={}, headers=self.headers(origin=True))
        self.assertEqual(response.status, 200, await response.text())
        code = (await response.json())["code"]
        response = await self.client.post(device_token.REDEEM_PATH, json={"code": code, "device_name": "Synthetic"})
        self.assertEqual(response.status, 200, await response.text())
        bearer = (await response.json())["token"]
        headers = {"Authorization": "Bearer " + bearer, "X-Hestia-Contract-Version": "0.67.0"}
        response = await self.post("receipts/transaction-search", {"receipt_id": self.anchor["receipt_id"]}, headers=headers)
        self.assertIn(response.status, (401, 403))
        confirm = confirm_values(self.anchor, link={"transaction_id": "1." + "0" * 64})
        response = await self.post("receipts/confirm", confirm, headers=headers)
        self.assertEqual((response.status, (await response.json())["error"]), (403, "budget_browser_session_required"))
