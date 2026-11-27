"""Owner-scoped receipt group reads and versioned HTTP projection."""
import json
import os
import unittest
from unittest import mock

from sqlalchemy import event

from hestia import api_contract, authz, db, store_sql
from hestia.budget import receipt_api
from hestia.budget.device_token import DeviceTokenError
from hestia.budget.receipt_store import BudgetReceiptStore
from tests import test_budget_api as fixture
from tests.test_budget_analytics_store import AnalyticsCase
from tests.test_budget_receipts import reference_digest


_GROUP = "10000000-0000-4000-8000-000000000001"
_MEMBER_A = "10000000-0000-4000-8000-000000000002"
_MEMBER_B = "10000000-0000-4000-8000-000000000003"
_EVIDENCE = "10000000-0000-4000-8000-000000000004"
_ATTACHED = "10000000-0000-4000-8000-000000000005"
_UNLINKED = "10000000-0000-4000-8000-000000000006"
_UNKNOWN = "10000000-0000-4000-8000-000000000099"
_BANK_REFERENCE = "private-bank-reference-for-the-synthetic-group"
_BANK_EVIDENCE = "private-evidence-must-not-be-in-the-response"
_HEADER = "X-Hestia-Contract-Version"
_VERSION = "0.59.0"
_GROUP_PATH = receipt_api.PREFIX + "/group/detail"


def _seed(session, owner, account, now, *, evidence=True):
    session.add(db.BudgetTransaction(
        account_id=account, entry_reference=_BANK_REFERENCE, payload_json="{}", observed_at=now, uncertain=False,
    ))
    group_amounts = ("44", "54") if evidence else ("46", "54")
    for receipt_id, date, amount, fields in (
        (_MEMBER_A, "2026-11-15", group_amounts[0], {"status": "needs_review"}),
        (_MEMBER_B, "2026-11-14", group_amounts[1], {"status": "needs_review"}),
        (_UNLINKED, "2026-11-17", "10", {}),
    ):
        session.add(db.BudgetReceipt(**({
            "id": receipt_id, "owner": owner, "receipt_date": date, "total_amount": amount,
            "status": "confirmed", "currency": "PLN", "payment_method": "card", "source_kind": "email",
            "raw_shop_name": "Synthetic group shop", "version": 1, "created_at": now, "updated_at": now,
        } | fields)))
    gateway = dict(owner=owner, receipt_date="2026-11-16", total_amount="100", currency="PLN",
                   payment_method="card", source_kind="email", document_kind="payment_confirmation",
                   gateway="PayU", version=1, created_at=now, updated_at=now)
    if evidence:
        session.add(db.BudgetReceipt(id=_EVIDENCE, status="needs_review", fee_amount="2", **gateway))
    session.flush()
    session.add(db.BudgetReceipt(id=_ATTACHED, status="confirmed", evidence_for_receipt_id=_MEMBER_A, **gateway))
    session.add(db.BudgetReceiptLinkGroup(
        id=_GROUP, owner=owner, account_id=account, entry_reference=_BANK_REFERENCE,
        currency="PLN", tx_amount="100", credit_debit_indicator="DBIT", status="needs_review", version=3,
        fee_amount="2" if evidence else None, evidence_receipt_id=_EVIDENCE if evidence else None,
        linked_fingerprint="fingerprint", linked_evidence=_BANK_EVIDENCE, created_at=now, updated_at=now,
    ))
    session.flush()
    session.add_all(db.BudgetReceiptLinkMember(group_id=_GROUP, receipt_id=member, owner=owner)
                    for member in (_MEMBER_A, _MEMBER_B))


class ReceiptGroupStoreReadsTests(AnalyticsCase):
    def setUp(self):
        super().setUp()
        self.receipts = BudgetReceiptStore(self.sessions, clock=lambda: self.now, cursor_secret=b"synthetic-only")

    def test_group_detail_forbids_device_tokens_at_the_store(self):
        device_id = "synthetic-device"
        with self.sessions.begin() as session:
            _seed(session, "owner", self.accounts["a"], self.now)
            session.add(db.BudgetReceiptDeviceToken(
                id=device_id, owner="owner", device_name="Synthetic device", token_hmac="1" * 64,
                created_at=self.now, expires_at=self.now + 30, hard_expires_at=self.now + 60,
                sliding_written_at=self.now,
            ))
        store = BudgetReceiptStore(self.sessions, clock=lambda: self.now, cursor_secret=b"synthetic-only",
                                   device_id=device_id)
        with self.assertRaises(DeviceTokenError) as denied:
            store.group_detail("owner", _GROUP)
        self.assertEqual((denied.exception.status, denied.exception.code), (403, "budget_forbidden"))

    def test_grouped_needs_review_detail_skips_one_to_one_candidates(self):
        with self.sessions.begin() as session:
            _seed(session, "owner", self.accounts["a"], self.now)
        for receipt_id in (_MEMBER_A, _EVIDENCE):
            with self.subTest(receipt_id=receipt_id), mock.patch.object(self.receipts, "_candidates") as scan:
                detail = self.receipts.detail("owner", receipt_id)
                scan.assert_not_called()
                self.assertNotIn("candidates", detail)
                self.assertNotIn("no_bank_data", detail)
                self.assertEqual(detail["receipt"]["link_group"]["group_status"], "needs_review")

    def test_linked_keyset_filter_and_page_group_lookup(self):
        with self.sessions.begin() as session:
            _seed(session, "owner", self.accounts["a"], self.now)

        batch_queries = []

        def before_cursor_execute(_conn, _cursor, statement, _params, *_args):
            if "budget_receipt_link_members" in statement and "UNION ALL" in statement:
                batch_queries.append(statement)

        event.listen(self.engine, "before_cursor_execute", before_cursor_execute)
        self.addCleanup(event.remove, self.engine, "before_cursor_execute", before_cursor_execute)
        linked_page = {"linked": True, "limit": 2, "evidence": "include"}
        first = self.receipts.list("owner", linked_page)
        rows = first["receipts"]
        self.assertEqual(len(batch_queries), 1)
        self.assertEqual([item["receipt_id"] for item in rows], [_EVIDENCE, _MEMBER_A])
        self.assertIsNotNone(first["next_cursor"])
        for item in rows:
            self.assertTrue(item["linked"])
            self.assertTrue(item["has_document"])
            self.assertEqual(item["linked_account_id"], self.accounts["a"])
            self.assertEqual(item["linked_entry_ref"], reference_digest(_BANK_REFERENCE))
            self.assertEqual(item["link_group"], {
                "group_id": _GROUP, "group_status": "needs_review", "group_version": 3,
                "member_count": 2, "fee_amount": "2", "evidence_receipt_id": _EVIDENCE,
            })
        second = self.receipts.list("owner", {**linked_page, "cursor": first["next_cursor"]})
        self.assertEqual(len(batch_queries), 2)
        self.assertEqual([item["receipt_id"] for item in second["receipts"]], [_MEMBER_B])
        self.assertIsNone(second["next_cursor"])
        unlinked = self.receipts.list("owner", {"linked": False})["receipts"]
        self.assertEqual([item["receipt_id"] for item in unlinked], [_UNLINKED])
        self.assertNotIn("link_group", unlinked[0])
        only = self.receipts.list("owner", {"evidence": "only"})["receipts"]
        self.assertEqual([item["receipt_id"] for item in only], [_EVIDENCE, _ATTACHED])
        self.assertEqual([item["linked"] for item in only], [True, False])
        self.assertTrue(all(item["has_document"] for item in only))
        visible = self.receipts.list("owner", {"linked": True})["receipts"]
        self.assertEqual([item["receipt_id"] for item in visible], [_MEMBER_A, _MEMBER_B])
        self.assertEqual(self.receipts.list("other", {"linked": True}), {"receipts": [], "next_cursor": None})
        self.assertEqual(len(batch_queries), 5)

    def test_group_detail_without_evidence_and_grouped_search(self):
        with self.sessions.begin() as session:
            _seed(session, "owner", self.accounts["a"], self.now, evidence=False)
        group = self.receipts.group_detail("owner", _GROUP)
        self.assertIsNone(group["evidence"])
        self.assertIsNone(group["group"]["fee_amount"])
        self.assertEqual(group["group"]["member_count"], 2)
        self.assertEqual([item["receipt_id"] for item in group["members"]], [_MEMBER_A, _MEMBER_B])
        api_contract.BudgetReceiptGroupDetailResult.model_validate(group)
        search = self.receipts.search("owner", {"query": "Synthetic"})
        self.assertEqual([item["receipt_id"] for item in search["receipts"] if item["linked"]], [_MEMBER_A, _MEMBER_B])


class ReceiptGroupHTTPReadsTests(unittest.IsolatedAsyncioTestCase):
    rows = fixture.BudgetAPITests.rows
    token = fixture.BudgetAPITests.token
    headers = fixture.BudgetAPITests.headers
    assert_private_headers = fixture.BudgetAPITests.assert_private_headers

    async def asyncSetUp(self):
        patch = mock.patch.dict(os.environ, {
            receipt_api.FLAG: "1", "HESTIA_BUDGET_JOBS_ENABLED": "0",
            "HESTIA_BUDGET_POLLER_ENABLED": "0", "HESTIA_BUDGET_CONSENT_ENABLED": "0",
        })
        patch.start()
        self.addCleanup(patch.stop)
        await fixture.BudgetAPITests.asyncSetUp(self)
        store_sql.activate_catalog_compat(path=self.path)
        with self.sessions.begin() as session:
            session.get(db.AppMeta, authz.POLICY_PHASE_KEY).value = authz.POLICY_PHASE_CUSTOM
            session.add_all((
                db.PermissionGrant(username="viewer", permission_id="budget.receipts.view", target_owner="alice"),
                db.PermissionGrant(username="operator", permission_id="budget.receipts.add", target_owner="alice"),
            ))
            _seed(session, "alice", self.alice_id, 1.0)

    async def group(self, user="alice", *, version=_VERSION, group_id=_GROUP):
        headers = self.headers(user)
        if version is not None:
            headers[_HEADER] = version
        return await self.client.get(_GROUP_PATH, params={"group_id": group_id}, headers=headers)

    async def test_group_detail_is_owner_scoped_and_redacts_bank_fields_for_delegate(self):
        for user, can_match in (("alice", True), ("viewer", False)):
            with self.subTest(user=user):
                response = await self.group(user)
                self.assertEqual(response.status, 200, await response.text())
                self.assert_private_headers(response)
                body = await response.json()
                api_contract.BudgetReceiptGroupDetailResult.model_validate(body)
                self.assertEqual([row["receipt_id"] for row in body["members"]], [_MEMBER_A, _MEMBER_B])
                self.assertEqual(body["evidence"]["receipt_id"], _EVIDENCE)
                self.assertTrue(body["evidence"]["has_document"])
                self.assertEqual(body["group"]["member_count"], 2)
                for item in (body, body["evidence"], *body["members"]):
                    self.assertEqual(item["linked_account_id"], self.alice_id if can_match else None)
                    digest = reference_digest(_BANK_REFERENCE) if can_match else None
                    self.assertEqual(item["linked_entry_ref"], digest)
                    self.assertNotIn("candidate_token", item)
                wire = json.dumps(body)
                self.assertNotIn(_BANK_REFERENCE, wire)
                self.assertNotIn(_BANK_EVIDENCE, wire)
        self.assertEqual((await self.group("bob")).status, 404)
        self.assertEqual((await self.group("viewer", group_id=_UNKNOWN)).status, 404)
        self.assertEqual((await self.group("alice", group_id="invalid")).status, 404)
        self.assertEqual((await self.group("operator")).status, 403)

    async def test_group_route_requires_exact_query_browser_cookie_and_contract_opt_in(self):
        for version in (None, "0.57.0", "0.58.0"):
            with self.subTest(version=version):
                self.assertEqual((await self.group(version=version)).status, 404)
        major, minor, _patch = (int(part) for part in api_contract.CONTRACT_VERSION.split("."))
        for version in (f"{major}.{minor + 1}.0", "0.59x"):
            response = await self.group(version=version)
            self.assertEqual(response.status, 400)
            self.assertEqual((await response.json())["error"], "budget_receipt_contract_version_invalid")
        headers = self.headers() | {_HEADER: _VERSION}
        for query in ({}, {"receipt_id": _GROUP}, [("group_id", _GROUP), ("group_id", _GROUP)]):
            response = await self.client.get(_GROUP_PATH, params=query, headers=headers)
            self.assertEqual(response.status, 400)
        response = await self.client.head(_GROUP_PATH, params={"group_id": _GROUP}, headers=headers)
        self.assertEqual(response.status, 405, await response.text())
        response = await self.client.get(_GROUP_PATH, params={"group_id": _GROUP}, headers={_HEADER: _VERSION})
        self.assertEqual(response.status, 401)
        for bearer in (self.token(), "A" * 43):
            headers = {_HEADER: _VERSION, "Authorization": "Bearer " + bearer}
            response = await self.client.get(_GROUP_PATH, params={"group_id": _GROUP}, headers=headers)
            self.assertEqual(response.status, 403)

    async def test_old_receipt_list_and_detail_keep_linked_digest_without_group_field(self):
        for version in (None, "0.57.0", "0.58.0", _VERSION):
            with self.subTest(version=version):
                headers = {} if version is None else {_HEADER: version}
                default_listing = await self.client.post(receipt_api.PREFIX + "/list", json={"linked": True},
                                                         headers=self.headers(origin=True) | headers)
                self.assertEqual(default_listing.status, 200, await default_listing.text())
                visible = (await default_listing.json())["receipts"]
                self.assertEqual([row["receipt_id"] for row in visible], [_MEMBER_A, _MEMBER_B])
                listing = await self.client.post(receipt_api.PREFIX + "/list",
                                                 json={"linked": True, "evidence": "include"},
                                                 headers=self.headers(origin=True) | headers)
                self.assertEqual(listing.status, 200, await listing.text())
                receipts = (await listing.json())["receipts"]
                self.assertEqual([row["receipt_id"] for row in receipts], [_EVIDENCE, _MEMBER_A, _MEMBER_B])
                detail = await self.client.get(receipt_api.PREFIX + "/detail", params={"receipt_id": _MEMBER_A},
                                               headers=self.headers() | headers)
                self.assertEqual(detail.status, 200, await detail.text())
                body = await detail.json()
                api_contract.BudgetReceiptDetailResult.model_validate(body)
                for row in (*receipts, body["receipt"]):
                    self.assertTrue(row["linked"])
                    self.assertEqual(row["linked_account_id"], self.alice_id)
                    self.assertEqual(row["linked_entry_ref"], reference_digest(_BANK_REFERENCE))
                    self.assertEqual("link_group" in row, version == _VERSION)
                self.assertFalse(body.get("candidates"))
