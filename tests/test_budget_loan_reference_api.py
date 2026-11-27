"""Mortgage reference picker, save, and forecast over real HTTP and SQLite."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import unittest
from unittest import mock

from hestia import api_contract, db
from hestia.budget import analytics_views
from hestia.budget.analytics_core import AnalyticsError
from tests import test_budget_analytics_api as fixture

_CONTRACT_HEADER = "X-Hestia-Contract-Version"
_CANDIDATES = "mortgage-reference-candidates"
_LOAN = "TESTLOAN4101"
_POLICY = "TESTPOLICY4102"
_FOREIGN = "TESTFOREIGN4103"
_NOW = datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()


def _reference(token):
    return {"hash": sha256(token.encode()).hexdigest(), "last4": token[-4:]}


def _schedule(**changes):
    return {
        "label": "Synthetic home loan and insurance", "amount": "100", "currency": "PLN", "due_day": 5,
        "start_date": "2026-01-01", "remind_days_before": 5, "destination_kind": "owner_inbox",
        "enabled": True, "expected_version": 0, **changes,
    }


class MortgageReferencesAPITests(unittest.IsolatedAsyncioTestCase):
    rows = fixture.BudgetAnalyticsAPITests.rows
    token = fixture.BudgetAnalyticsAPITests.token
    headers = fixture.BudgetAnalyticsAPITests.headers
    assert_private_headers = fixture.BudgetAnalyticsAPITests.assert_private_headers
    get = fixture.BudgetAnalyticsAPITests.get
    post = fixture.BudgetAnalyticsAPITests.post

    async def asyncSetUp(self):
        await fixture.BudgetAnalyticsAPITests.asyncSetUp(self)

    def _headers(self, version="0.64.0", *, user="alice", origin=False):
        headers = self.headers(user, origin=origin)
        if version is not None:
            headers[_CONTRACT_HEADER] = version
        return headers

    def _recurring(self, account_id, key, label, amounts):
        with self.sessions.begin() as session:
            for month, amount in zip((8, 9, 10), amounts, strict=True):
                entry_reference = f"synthetic-{key}-{month}"
                entry = fixture.transaction(
                    entry_reference, amount=amount, booking_date=f"2026-{month:02d}-05",
                    remittance_information=[label],
                )
                session.add(db.BudgetTransaction(account_id=account_id, entry_reference=entry_reference,
                            payload_json=json.dumps(entry), observed_at=_NOW, source="api", uncertain=False))

    async def _success(self, response):
        self.assertEqual(response.status, 200, await response.text())
        self.assert_private_headers(response)
        return await response.json()

    async def _save(self, body, version="0.64.0"):
        response = await self.post("mortgage-save", body, headers=self._headers(version, origin=True))
        result = await self._success(response)
        api_contract.BudgetAnalyticsMortgageScheduleResult.model_validate(result)
        return result["schedule"]

    async def test_picker_requires_contract_and_returns_masked_owner_candidates(self):
        self._recurring(self.alice_id, "loan", f"RATA KREDYTU NR {_LOAN}", ("100", "99.5", "100.5"))
        self._recurring(self.alice_id, "loan-alias", f"KREDYT NR {_LOAN}", ("4", "4", "4"))
        self._recurring(self.alice_id, "insurance", f"Ubezpieczenie polisy nr {_POLICY}", ("7", "7", "7"))
        self._recurring(self.alice_id, "utility", "Synthetic monthly utilities", ("3", "3", "3"))
        self._recurring(self.bob_id, "foreign", f"RATA KREDYTU NR {_FOREIGN}", ("17", "17", "17"))
        for version in (None, "0.62.0", "0.63.0", "0.999.0", "0.064.0"):
            with self.subTest(version=version):
                response = await self.get(_CANDIDATES, headers=self._headers(version))
                self.assertEqual(response.status, 400)
                self.assertEqual((await response.json())["error"], "budget_analytics_contract_version_invalid")
                self.assert_private_headers(response)

        for user, expected in (("viewer", 403), ("disabled", 401)):
            response = await self.get(_CANDIDATES, headers=self._headers(user=user))
            self.assertEqual(response.status, expected)
        self.assertEqual((await self.get(_CANDIDATES, headers={_CONTRACT_HEADER: "0.64.0"})).status, 401)

        for user, tokens in (("alice", (_LOAN, _POLICY)), ("bob", (_FOREIGN,))):
            with self.subTest(user=user):
                body = await self._success(await self.get(_CANDIDATES, headers=self._headers(user=user)))
                api_contract.BudgetAnalyticsMortgageReferenceCandidates.model_validate(body)
                candidates = body["candidates"]
                self.assertCountEqual(
                    [{key: item[key] for key in ("hash", "last4")} for item in candidates],
                    [_reference(token) for token in tokens],
                )
                for item in candidates:
                    self.assertIn(item["last4"], item["label"])
                for token in (_LOAN, _POLICY, _FOREIGN):
                    self.assertNotIn(token, json.dumps(body))
                self.assertNotIn("utilities", json.dumps(body))
        with mock.patch.object(analytics_views, "MAX_RECURRING_HINTS", 1):
            capped = await self._success(await self.get(_CANDIDATES, headers=self._headers()))
        self.assertEqual(len(capped["candidates"]), 1)

    async def test_versioned_writes_preserve_links_and_clear_only_when_requested(self):
        references = [_reference(_LOAN), _reference(_POLICY)]
        created = await self._save(_schedule(match_references=references))
        schedule_id = created["id"]
        self.assertEqual((created["version"], created["changed"]), (1, True))
        self.assertCountEqual(created["match_references"], references)

        newest = await self._success(await self.get("mortgages", headers=self._headers()))
        api_contract.BudgetAnalyticsMortgages.model_validate(newest)
        self.assertIs(newest["match_references_supported"], True)
        self.assertCountEqual(newest["schedules"][0]["match_references"], references)
        for version in ("0.62.0", "0.63.0", None):
            old = await self._success(await self.get("mortgages", headers=self._headers(version)))
            self.assertNotIn("match_references_supported", old)
            self.assertNotIn("match_references", old["schedules"][0])

        no_op = await self._save(_schedule(
            schedule_id=schedule_id, expected_version=1, match_references=references[::-1],
        ))
        self.assertEqual((no_op["version"], no_op["changed"]), (1, False))

        other = {"schedule_id": schedule_id, "label": "Edited synthetic loan", "expected_version": 1}
        old = await self._save(_schedule(**other), version="0.62.0")
        self.assertEqual((old["version"], old["changed"]), (2, True))
        self.assertNotIn("match_references", old)
        other["expected_version"] = 2
        unversioned = await self._save(_schedule(**other), version=None)
        self.assertEqual((unversioned["version"], unversioned["changed"]), (2, False))
        self.assertNotIn("match_references", unversioned)
        preserved = await self._save(_schedule(**other))
        self.assertCountEqual(preserved["match_references"], references)

        replaced = await self._save(_schedule(**other, match_references=references[:1]))
        self.assertEqual((replaced["version"], replaced["changed"]), (3, True))
        self.assertEqual(replaced["match_references"], references[:1])
        other["expected_version"] = 3
        cleared = await self._save(_schedule(**other, match_references=[]))
        self.assertEqual((cleared["version"], cleared["changed"], cleared["match_references"]), (4, True, []))
        self.assertEqual(len(self.rows(db.BudgetAnalyticsAudit)), 4)

    async def test_version_and_validation_reject_invalid_link_writes(self):
        reference = _reference(_LOAN)
        for version in (None, "0.62.0", "0.63.0"):
            response = await self.post("mortgage-save", _schedule(match_references=[reference]),
                                       headers=self._headers(version, origin=True))
            self.assertEqual(response.status, 400)
            self.assertEqual((await response.json())["error"], "budget_analytics_invalid_request")
        for malformed in ([_LOAN], [{**reference, "raw": _LOAN}], [{"hash": _LOAN, "last4": "4101"}],
                          [{**reference, "last4": _LOAN}]):
            with self.subTest(malformed=malformed):
                response = await self.post("mortgage-save", _schedule(match_references=malformed),
                                           headers=self._headers(origin=True))
                self.assertEqual(response.status, 400)
                self.assertEqual((await response.json())["error"], "budget_invalid_request")

        duplicates = [reference, {**reference, "last4": "DEMO"}]
        response = await self.post("mortgage-save", _schedule(match_references=duplicates),
                                   headers=self._headers(origin=True))
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"], "budget_analytics_invalid_request")
        store = analytics_views.BudgetAnalyticsViews(self.sessions, cursor_secret=b"synthetic-test")
        with self.assertRaises(AnalyticsError):
            store.save_mortgage_schedule("alice", **_schedule(match_references=[{"raw": _LOAN}]))
        self.assertEqual(self.rows(db.BudgetMortgageSchedule), [])
        self.assertEqual(self.rows(db.BudgetAnalyticsAudit), [])

    async def test_forecast_keeps_schedule_and_gates_linked_audit_fields(self):
        self._recurring(self.alice_id, "loan", f"RATA KREDYTU NR {_LOAN}", ("100", "100.5", "99.5"))
        self._recurring(self.alice_id, "insurance", f"Ubezpieczenie nr {_POLICY}", ("7", "7", "7"))
        created = await self._save(_schedule(match_references=[_reference(_LOAN), _reference(_POLICY)]))

        with mock.patch.object(analytics_views.BudgetAnalyticsViews, "_now", return_value=_NOW):
            for version in ("0.64.0", "0.63.0", "0.62.0", None):
                with self.subTest(version=version):
                    result = await self._success(await self.get(
                        "forecast", params={"months": "1"}, headers=self._headers(version),
                    ))
                    api_contract.BudgetAnalyticsForecast.model_validate(result)
                    items = result["months"][0]["items"]
                    self.assertEqual([(row["schedule_id"], row["source"], row["amount"]) for row in items],
                                     [(created["id"], "mortgage", "100")])
                    audits = result["deduplications"]
                    self.assertEqual(len(audits), 2)
                    self.assertEqual({row["matched_due_date"] for row in audits}, {"2026-10-05"})
                    if version == "0.64.0":
                        self.assertEqual({row["match_type"] for row in audits}, {"linked"})
                        self.assertEqual({row["amount_delta"] for row in audits}, {"-0.5", "-93"})
                    else:
                        for row in audits:
                            self.assertNotIn("match_type", row)
                            self.assertNotIn("amount_delta", row)
                    for token in (_LOAN, _POLICY):
                        self.assertNotIn(token, json.dumps(result))
