"""HTTP API, security, contract gating, device-token and delegation tests for M:1 receipt groups."""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock
import uuid

from sqlalchemy import select

from hestia import api_contract, authz, db, store_sql
from hestia.budget import device_token, gmail_config, receipt_api
from hestia.budget.gmail_store import GATEWAY_REF_DOMAIN, digest
from tests import test_budget_api as api_fixture


def _uuid():
    return str(uuid.uuid4())


class ReceiptGroupAPITests(api_fixture.unittest.IsolatedAsyncioTestCase):
    _HMAC_SECRET = b"receipt-group-http-hmac-secret-32"
    rows = api_fixture.BudgetAPITests.rows
    token = api_fixture.BudgetAPITests.token
    headers = api_fixture.BudgetAPITests.headers

    async def asyncSetUp(self):
        patch = mock.patch.dict(os.environ, {
            receipt_api.FLAG: "1",
            "HESTIA_BUDGET_HTTP_ENABLED": "1",
            "HESTIA_BUDGET_JOBS_ENABLED": "0",
            "HESTIA_BUDGET_POLLER_ENABLED": "0",
            "HESTIA_BUDGET_CONSENT_ENABLED": "0",
        })
        patch.start()
        self.addCleanup(patch.stop)
        hmac_patch = mock.patch.object(gmail_config, "load_ref_hmac_secret", return_value=self._HMAC_SECRET)
        hmac_patch.start()
        self.addCleanup(hmac_patch.stop)
        await api_fixture.BudgetAPITests.asyncSetUp(self)
        rule_key = self.path.parent / "budget-rule.key"
        rule_key.touch(mode=0o600)
        rule_key.write_bytes(os.urandom(32))
        key_patch = mock.patch.dict(os.environ, {"HESTIA_BUDGET_RULE_KEY_FILE": str(rule_key)})
        key_patch.start()
        self.addCleanup(key_patch.stop)

    def _add_tx(self, ref, *, owner="alice", amount="100.00", booking_date="2026-11-20",
                remittance=None, creditor="Allegro PayU"):
        account_id = self.alice_id if owner == "alice" else self.bob_id
        payload = api_fixture.transaction(
            ref, amount=amount, booking_date=booking_date,
            remittance_information=["PAYU ALLEGRO REF 77665544"] if remittance is None else remittance,
        )
        payload["creditor"] = {"name": creditor}
        with self.sessions.begin() as session:
            session.add(db.BudgetTransaction(
                account_id=account_id, entry_reference=ref, payload_json=json.dumps(payload),
                observed_at=2_000_000.0, uncertain=False,
            ))

    def _add_receipt(self, *, owner="alice", status="confirmed", shop_name="Seller", receipt_date="2026-11-20",
                     total_amount="50.00", currency="PLN", gateway=None, fee_amount=None, gateway_ref=None,
                     forwarded_sender_domain="allegro.pl", payment_method="card"):
        rid = _uuid()
        now = 2_000_000.0
        with self.sessions.begin() as session:
            session.add(db.BudgetReceipt(
                id=rid, owner=owner, submitted_by=owner,
                raw_shop_name=shop_name, receipt_kind="purchase", receipt_date=receipt_date,
                receipt_time="12:00", total_amount=total_amount, currency=currency,
                payment_method=payment_method, status=status, source_kind="email",
                gateway=gateway, gateway_status="paid" if gateway else None, fee_amount=fee_amount,
                forwarded_sender_domain=forwarded_sender_domain,
                gateway_ref_hmac=None if gateway_ref is None else digest(self._HMAC_SECRET, GATEWAY_REF_DOMAIN, gateway_ref),
                version=1, created_at=now, updated_at=now,
            ))
            if status == "confirmed":
                session.flush()
                session.add(db.BudgetReceiptLine(
                    receipt_id=rid, owner=owner, line_no=1, line_kind="item",
                    raw_name="Item", raw_name_key="item", ptu_code="A",
                    quantity="1", raw_unit="szt", unit_price_raw=total_amount,
                    gross_line_total=total_amount, discount_amount="0", net_line_total=total_amount,
                ))
        return rid

    def _receipt_headers(self, user="alice", *, version="0.59.0", origin=True, bearer=False,
                         device_token_secret=None):
        headers = {"Content-Type": "application/json"}
        if version is not None:
            headers["X-Hestia-Contract-Version"] = version
        if device_token_secret is not None:
            headers["Authorization"] = f"Bearer {device_token_secret}"
        else:
            headers.update(self.headers(user, origin=origin, bearer=bearer))
        return headers

    async def _post_receipt(self, operation, body, *, user="alice", version="0.59.0", origin=True,
                            bearer=False, device_token_secret=None):
        return await self.client.post(
            receipt_api.PREFIX + "/" + operation,
            json=body,
            headers=self._receipt_headers(
                user, version=version, origin=origin, bearer=bearer,
                device_token_secret=device_token_secret,
            ),
        )

    async def test_version_gating_and_detail_stripping_below_0_59_0(self):
        self._add_tx("alice-v59-tx", amount="80.00")
        r1 = self._add_receipt(shop_name="Split Store", receipt_date="2026-11-20", total_amount="30.00")
        r2 = self._add_receipt(shop_name="Split Store", receipt_date="2026-11-20", total_amount="50.00")

        for bad_version in (None, "0.58.0"):
            hdrs = self._receipt_headers("alice", version=bad_version)
            self.assertEqual((await self._post_receipt("group/candidates", {"receipt_id": r1}, version=bad_version)).status, 404)
            self.assertEqual((await self._post_receipt("group/link", {
                "link": {"account_id": self.alice_id, "entry_reference": "0" * 64, "candidate_token": "tok"},
                "members": [{"receipt_id": r1, "expected_version": 1}, {"receipt_id": r2, "expected_version": 1}],
            }, version=bad_version)).status, 404)
            self.assertEqual((await self._post_receipt("group/unlink", {
                "group_id": _uuid(), "expected_version": 1,
            }, version=bad_version)).status, 404)
            det_old = await self.client.get(receipt_api.PREFIX + "/detail", params={"receipt_id": r1}, headers=hdrs)
            self.assertEqual(det_old.status, 200)
            old_body = await det_old.json()
            self.assertNotIn("group_candidates", old_body)
            self.assertNotIn("link_group", old_body["receipt"])

        bad_ver_resp = await self._post_receipt("group/candidates", {"receipt_id": r1}, version="0.99.0")
        self.assertEqual(bad_ver_resp.status, 400)
        self.assertEqual((await bad_ver_resp.json())["error"], "budget_receipt_contract_version_invalid")

        det_new = await self.client.get(
            receipt_api.PREFIX + "/detail", params={"receipt_id": r1},
            headers=self._receipt_headers("alice", version="0.59.0"),
        )
        self.assertEqual(det_new.status, 200)
        new_body = await det_new.json()
        api_contract.BudgetReceiptDetailResult.model_validate(new_body)
        self.assertEqual(len(new_body["group_candidates"]), 1)

    async def test_http_roundtrip_candidates_link_unlink_delete_dissolve_and_validation(self):
        self._add_tx("alice-allegro-100", amount="100.00", remittance=["PAYU 77665544"])
        r1 = self._add_receipt(shop_name="Sklep Jedyny", total_amount="40.00")
        r2 = self._add_receipt(shop_name="Sklep Drugi", total_amount="58.50")
        ev = self._add_receipt(shop_name="PayU", total_amount="100.00", gateway="payu",
                               fee_amount="1.50", gateway_ref="77665544")

        cands_resp = await self._post_receipt("group/candidates", {"receipt_id": r1})
        self.assertEqual(cands_resp.status, 200, await cands_resp.text())
        cands = await cands_resp.json()
        api_contract.BudgetReceiptGroupCandidatesResult.model_validate(cands)
        prop = next(p for p in cands["proposals"] if p["evidence_receipt_id"] == ev)
        self.assertEqual((prop["match_strength"], prop["fee_amount"]), ("id", "1.5"))

        # Schema & sum error HTTP checks covering BudgetReceiptGroupLinkRequest model validator branches.
        valid_link = {"account_id": prop["account_id"], "entry_reference": prop["entry_reference"],
                      "candidate_token": prop["candidate_token"]}
        for bad_link_body in (
            {"link": valid_link, "members": [{"receipt_id": r1, "expected_version": 1}]},
            {"link": valid_link, "members": [{"receipt_id": r1, "expected_version": 1},
                                             {"receipt_id": r1, "expected_version": 1}]},
            {"link": valid_link, "members": [{"receipt_id": r1, "expected_version": 1},
                                             {"receipt_id": r2, "expected_version": 1}],
             "evidence_receipt_id": ev},
            {"link": valid_link, "members": [{"receipt_id": r1, "expected_version": 1},
                                             {"receipt_id": r2, "expected_version": 1}],
             "evidence_receipt_id": r1, "evidence_expected_version": 1},
            {"link": valid_link, "members": [{"receipt_id": r1, "expected_version": 1},
                                             {"receipt_id": r2, "expected_version": 1}],
             "fee_amount": "-1.00"},
            {"link": valid_link, "members": [{"receipt_id": r1, "expected_version": 1},
                                             {"receipt_id": r2, "expected_version": 1}],
             "fee_amount": "1.00"},
        ):
            bad_schema = await self._post_receipt("group/link", bad_link_body)
            self.assertEqual(bad_schema.status, 400)
        bad_sum = await self._post_receipt("group/link", {
            "link": valid_link,
            "members": [{"receipt_id": r1, "expected_version": 1}, {"receipt_id": r2, "expected_version": 1}],
        })
        self.assertEqual(bad_sum.status, 400)
        self.assertEqual((await bad_sum.json())["error"], "budget_receipt_group_sum_mismatch")

        # Valid group/link -> 200.
        link_resp = await self._post_receipt("group/link", {
            "link": {"account_id": prop["account_id"], "entry_reference": prop["entry_reference"],
                     "candidate_token": prop["candidate_token"]},
            "members": [{"receipt_id": r1, "expected_version": 1}, {"receipt_id": r2, "expected_version": 1}],
            "evidence_receipt_id": ev, "evidence_expected_version": 1, "fee_amount": "1.50",
        })
        self.assertEqual(link_resp.status, 200, await link_resp.text())
        link_body = await link_resp.json()
        api_contract.BudgetReceiptGroupLinkResult.model_validate(link_body)
        group_id = link_body["group"]["group_id"]
        self.assertNotIn("alice-allegro-100", json.dumps(link_body))
        self.assertNotIn("PAYU", json.dumps(link_body))

        # Audit table contains no raw provider entry_reference or raw merchant names.
        with self.sessions() as session:
            audits = list(session.scalars(select(db.BudgetAnalyticsAudit).where(
                db.BudgetAnalyticsAudit.subject_type == "receipt_group",
                db.BudgetAnalyticsAudit.subject_id == group_id,
            )))
            self.assertEqual(len(audits), 1)
            self.assertNotIn("alice-allegro-100", audits[0].after_json)
            self.assertNotIn("Sklep", audits[0].after_json)

        # GET detail on old contract strips link_group while 0.59.0 returns link_group & GET group/detail works.
        det_old = await self.client.get(
            receipt_api.PREFIX + "/detail", params={"receipt_id": r1},
            headers=self._receipt_headers("alice", version="0.58.0"),
        )
        self.assertNotIn("link_group", (await det_old.json())["receipt"])

        gdetail = await self.client.get(
            receipt_api.PREFIX + "/group/detail", params={"group_id": group_id},
            headers=self._receipt_headers("alice", version="0.59.0"),
        )
        self.assertEqual(gdetail.status, 200)
        api_contract.BudgetReceiptGroupDetailResult.model_validate(await gdetail.json())

        # Stale group/unlink -> 409, then valid group/unlink -> 200.
        stale_unlink = await self._post_receipt("group/unlink", {"group_id": group_id, "expected_version": 99})
        self.assertEqual(stale_unlink.status, 409)
        self.assertEqual((await stale_unlink.json())["error"], "budget_receipt_conflict")

        unlink_resp = await self._post_receipt("group/unlink", {"group_id": group_id, "expected_version": 1})
        self.assertEqual(unlink_resp.status, 200)
        unlink_body = await unlink_resp.json()
        api_contract.BudgetReceiptGroupUnlinkResult.model_validate(unlink_body)
        self.assertEqual({m["version"] for m in unlink_body["members"]}, {3})

        # Re-link, test delete(dissolve_group) HTTP contract & delete(dissolve_group=True) on member.
        cands2 = await (await self._post_receipt("group/candidates", {"receipt_id": r1})).json()
        prop2 = next(p for p in cands2["proposals"] if p["evidence_receipt_id"] == ev)
        link2 = await (await self._post_receipt("group/link", {
            "link": {"account_id": prop2["account_id"], "entry_reference": prop2["entry_reference"],
                     "candidate_token": prop2["candidate_token"]},
            "members": [{"receipt_id": r1, "expected_version": 3}, {"receipt_id": r2, "expected_version": 3}],
            "evidence_receipt_id": ev, "evidence_expected_version": 3, "fee_amount": "1.50",
        })).json()
        group2_id = link2["group"]["group_id"]

        bad_del_pair = await self._post_receipt("delete", {
            "receipt_id": r1, "expected_version": 4, "dissolve_group": True,
        })
        self.assertEqual(bad_del_pair.status, 400)
        bad_del_pair2 = await self._post_receipt("delete", {
            "receipt_id": r1, "expected_version": 4, "dissolve_group": False, "expected_group_version": 1,
        })
        self.assertEqual(bad_del_pair2.status, 400)

        blocked_del = await self._post_receipt("delete", {"receipt_id": r1, "expected_version": 4})
        self.assertEqual(blocked_del.status, 409)
        self.assertEqual((await blocked_del.json())["error"], "budget_receipt_in_group")

        dissolved_del = await self._post_receipt("delete", {
            "receipt_id": r1, "expected_version": 4, "dissolve_group": True, "expected_group_version": 1,
        })
        self.assertEqual(dissolved_del.status, 200)
        api_contract.BudgetReceiptDeleteResult.model_validate(await dissolved_del.json())
        self.assertEqual((await self.client.get(
            receipt_api.PREFIX + "/group/detail", params={"group_id": group2_id},
            headers=self._receipt_headers("alice", version="0.59.0"),
        )).status, 404)

    async def test_device_token_grant_withdrawal_delegation_roles_and_cross_owner_tokens(self):
        self._add_tx("alice-sec-tx", amount="70.00")
        r1 = self._add_receipt(shop_name="Sec Shop", total_amount="30.00")
        r2 = self._add_receipt(shop_name="Sec Shop", total_amount="40.00")
        bob_r1 = self._add_receipt(owner="bob", shop_name="Bob Shop", total_amount="30.00")

        # 1. Non-admin roles, missing Origin, and session Bearer are blocked.
        for bad_hdrs in (
            self._receipt_headers("viewer"),
            self._receipt_headers("operator"),
            self._receipt_headers("alice", origin=False),
            self._receipt_headers("alice", origin=False, bearer=True),
        ):
            r = await self.client.post(
                receipt_api.PREFIX + "/group/candidates", json={"receipt_id": r1}, headers=bad_hdrs,
            )
            self.assertEqual(r.status, 403)

        # 2. Device token: pair for alice, verify group/candidates allowed while group/link, group/unlink, group/detail blocked.
        store_sql.activate_catalog_compat(path=self.path)
        with self.sessions.begin() as session:
            session.get(db.AppMeta, authz.POLICY_PHASE_KEY).value = authz.POLICY_PHASE_CUSTOM
        pair_resp = await self.client.post(
            device_token.PAIR_PATH, json={}, headers=self.headers("alice", origin=True),
        )
        self.assertEqual(pair_resp.status, 200, await pair_resp.text())
        code = (await pair_resp.json())["code"]
        redeem_resp = await self.client.post(
            device_token.REDEEM_PATH, json={"code": code, "device_name": "iphone"},
        )
        self.assertEqual(redeem_resp.status, 200, await redeem_resp.text())
        dev_secret = (await redeem_resp.json())["token"]

        dev_cands = await self._post_receipt("group/candidates", {"receipt_id": r1}, device_token_secret=dev_secret)
        self.assertEqual(dev_cands.status, 200, await dev_cands.text())
        alice_prop = (await dev_cands.json())["proposals"][0]

        for blocked_op in ("group/link", "group/unlink"):
            resp = await self._post_receipt(blocked_op, {}, device_token_secret=dev_secret)
            self.assertEqual(resp.status, 403)
        self.assertEqual((await self.client.get(
            receipt_api.PREFIX + "/group/detail", params={"group_id": _uuid()},
            headers=self._receipt_headers("alice", device_token_secret=dev_secret),
        )).status, 403)

        # 3. Cross-owner isolation: bob gets 404 on alice receipt and cannot replay alice's preview token.
        self.assertEqual((await self._post_receipt(
            "group/candidates", {"receipt_id": r1}, user="bob",
        )).status, 404)
        self._add_tx("bob-tx", owner="bob", amount="70.00")
        bob_r2 = self._add_receipt(owner="bob", shop_name="Bob Shop", total_amount="40.00")
        cross_tok = await self._post_receipt("group/link", {
            "link": {"account_id": self.bob_id, "entry_reference": alice_prop["entry_reference"],
                     "candidate_token": alice_prop["candidate_token"]},
            "members": [{"receipt_id": bob_r1, "expected_version": 1}, {"receipt_id": bob_r2, "expected_version": 1}],
        }, user="bob")
        self.assertEqual(cross_tok.status, 400)
        cross_tok2 = await self._post_receipt("group/link", {
            "link": {"account_id": alice_prop["account_id"], "entry_reference": alice_prop["entry_reference"],
                     "candidate_token": alice_prop["candidate_token"]},
            "members": [{"receipt_id": bob_r1, "expected_version": 1}, {"receipt_id": bob_r2, "expected_version": 1}],
        }, user="bob")
        self.assertEqual(cross_tok2.status, 400)

        # 4. Grant withdrawal invalidates preview use and device-token group/candidates.
        with self.sessions.begin() as session:
            session.query(db.PermissionGrant).filter(
                db.PermissionGrant.username == "alice",
                db.PermissionGrant.permission_id == "budget.receipts.match",
            ).delete()
        withdrawn_link = await self._post_receipt("group/link", {
            "link": {"account_id": alice_prop["account_id"], "entry_reference": alice_prop["entry_reference"],
                     "candidate_token": alice_prop["candidate_token"]},
            "members": [{"receipt_id": r1, "expected_version": 1}, {"receipt_id": r2, "expected_version": 1}],
        })
        self.assertEqual(withdrawn_link.status, 403)
        self.assertEqual((await self._post_receipt(
            "group/candidates", {"receipt_id": r1}, device_token_secret=dev_secret,
        )).status, 403)
        self.assertEqual((await self._post_receipt("delete", {
            "receipt_id": r1, "expected_version": 1, "dissolve_group": True, "expected_group_version": 1,
        })).status, 403)


if __name__ == "__main__":
    unittest.main()
