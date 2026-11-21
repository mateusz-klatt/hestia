"""POST-session account numbers remain private until independently verified atomic adoption."""
import json
from unittest import mock

from hestia import db
from hestia.budget import consent_service, consent_store
from hestia.budget.analytics_views import BudgetAnalyticsViews
from hestia.budget.client import BudgetAPIError
from hestia.budget.consent_store import BudgetConsentError, BudgetConsentStore
from tests.test_budget_analytics_core import payload
from tests.test_budget_consent_service import ServiceCase, SESSION_ID, UID_A, UID_B, UID_EXTRA
from tests.test_budget_consent_store import AUTH


def account(uid, primary, number):
    return {"uid": uid, "identification_hash": primary,
            "account_id": {"other": {"scheme_name": "BBAN", "identification": number}},
            "name": "must-not-retain-holder", "postal_address": {"private": "must-not-retain-address"}}


class ConsentEvidenceTests(ServiceCase):
    def evidence(self):
        self.provider.created["accounts"] = [account(UID_A, "hash-a", "synthetic-a"),
            account(UID_B, "hash-b", "synthetic-b"), account(UID_EXTRA, "unselected-extra", "synthetic-extra")]

    def candidate_metadata(self, result):
        return json.loads(self.store.candidate_for_flow("owner", result["flow_id"])["metadata_json"])

    def test_post_numbers_survive_hash_only_get_without_extra_requests_or_public_data(self):
        self.evidence()
        result, _ = self.stage()
        during_get = []
        def verify_before_publish():
            self.assertEqual(self.rows(db.BudgetAccountIdentity), [])
            during_get.append(self.candidate_metadata(result))
            self.now += 20
        self.provider.hooks[("GET", "/sessions/" + SESSION_ID)] = verify_before_publish
        response = self.complete(result)
        self.assertEqual(response["status"], "adopted")
        identities = {row.account_id: row for row in self.rows(db.BudgetAccountIdentity)}
        self.assertEqual(set(identities), {self.accounts["hash-a"], self.accounts["hash-b"]})
        self.assertEqual(json.loads(identities[self.accounts["hash-a"]].identities_json),
                         [{"scheme": "BBAN", "identification": "synthetic-a"}])
        self.assertEqual(identities[self.accounts["hash-a"]].observed_at,
                         during_get[0]["account_identity_evidence"][UID_A]["observed_at"])
        self.assertLess(identities[self.accounts["hash-a"]].observed_at, self.now)
        self.assertEqual(identities[self.accounts["hash-a"]].source, "api")
        self.assertEqual([call[:2] for call in self.provider.calls],
                         [("GET", "/aspsps"), ("POST", "/auth"), ("POST", "/sessions"), ("GET", "/sessions/" + SESSION_ID)])
        private = self.candidate_metadata(result)
        self.assertEqual(private["account_uids"]["hash-a"], UID_A)
        self.assertNotIn(UID_EXTRA, json.dumps(private))
        self.assertNotIn("synthetic-extra", json.dumps(private))
        self.assertNotIn("unselected-extra", private["account_uids"])
        self.assertNotIn("must-not-retain", json.dumps(private))
        public = json.dumps([response, self.service.status("owner"), self.service.pending("owner", AUTH, {})])
        for value in ("synthetic-a", "hash-a", UID_A, "account_identity_evidence", "account_uids"):
            self.assertNotIn(value, public)
        self.assertEqual(self.rows(db.Audit), [])
        self.assertEqual(self.rows(db.BudgetAcquisition), [])

    def test_pending_post_evidence_survives_get_failure_restart_and_exact_verified_retry(self):
        self.evidence()
        result, _ = self.stage()
        self.provider.failures[("GET", "/sessions/" + SESSION_ID)] = BudgetAPIError(code="network")
        self.assertEqual(self.complete(result)["status"], "reconciliation_required")
        pending = self.store.candidate_for_flow("owner", result["flow_id"])
        self.assertIsNone(pending["valid_until"])
        self.assertEqual(json.loads(pending["metadata_json"])["account_hashes"], [])
        self.assertEqual(self.rows(db.BudgetAccountIdentity), [])
        self.store = BudgetConsentStore(self.sessions, clock=lambda: self.now)
        self.service = self.service_factory()
        del self.provider.failures[("GET", "/sessions/" + SESSION_ID)]
        self.assertEqual(self.complete(result)["status"], "adopted")
        self.assertEqual(len(self.method_calls("POST", "/sessions")), 1)
        self.assertEqual(len(self.rows(db.BudgetAccountIdentity)), 2)
        verified = self.store.candidate_for_flow("owner", result["flow_id"])
        repeated = self.store.record_candidate("owner", result["flow_id"], capsule_ref=verified["capsule_ref"],
            valid_until=verified["valid_until"], metadata={"account_hashes": json.loads(verified["metadata_json"])["account_hashes"]})
        self.assertEqual(repeated["metadata_json"], verified["metadata_json"])
        altered = json.loads(verified["metadata_json"])
        altered["account_identity_evidence"][UID_A]["identities"][0]["identification"] = "different"
        with self.assertRaises(BudgetConsentError):
            self.store.record_candidate("owner", result["flow_id"], capsule_ref=verified["capsule_ref"],
                                        valid_until=verified["valid_until"], metadata=altered)

    def test_old_candidate_shape_is_exactly_unchanged_without_optional_evidence(self):
        result, _ = self.stage()
        self.assertEqual(self.complete(result)["status"], "adopted")
        self.assertEqual(set(self.candidate_metadata(result)), {"account_hashes"})
        self.assertEqual(self.rows(db.BudgetAccountIdentity), [])

    def test_uid_or_primary_hash_mismatch_never_supplies_identity_evidence(self):
        self.evidence()
        self.provider.created["accounts"][0]["uid"] = UID_EXTRA
        self.provider.created["accounts"][2]["uid"] = UID_A
        self.provider.created["accounts"][1]["identification_hash"] = "different-primary"
        result, _ = self.stage()
        self.assertEqual(self.complete(result)["status"], "adopted")
        self.assertEqual(self.rows(db.BudgetAccountIdentity), [])

    def test_unselected_and_foreign_evidence_never_changes_local_accounts(self):
        self.evidence()
        self.store.set_account_selected("owner", self.accounts["hash-b"], False, actor="owner")
        self.provider.created["accounts"].append(account("08684c57-86e8-4e2d-b351-d35bad6124dd", "hash-c", "foreign"))
        result, _ = self.stage()
        self.assertEqual(self.complete(result)["status"], "adopted")
        self.assertEqual([row.account_id for row in self.rows(db.BudgetAccountIdentity)], [self.accounts["hash-a"]])
        self.assertEqual(len(self.rows(db.BudgetAccount)), 3)
        metadata = self.candidate_metadata(result)
        self.assertEqual(set(metadata["account_identity_evidence"]), {UID_A})
        self.assertEqual(metadata["account_uids"], {"hash-a": UID_A})
        self.assertNotIn("synthetic-b", json.dumps(metadata))
        self.assertNotIn("synthetic-extra", json.dumps(metadata))
        self.assertNotIn("foreign", json.dumps(metadata))

    def test_cancelled_or_changed_generation_retains_private_candidate_without_identity_publication(self):
        for failure in ("cancel", "generation", "bank", "psu", "expiry"):
            with self.subTest(failure=failure):
                self.evidence()
                result, _ = self.stage()
                if failure in ("cancel", "generation"):
                    def interfere():
                        if failure == "cancel":
                            self.service.cancel("owner", result["flow_id"], AUTH, result["_nonce"])
                        else:
                            self.connection_changes(consent_generation=self.connection().consent_generation + 1)
                    self.provider.hooks[("GET", "/sessions/" + SESSION_ID)] = interfere
                elif failure == "bank":
                    self.provider.session["aspsp"]["country"] = "FI"
                elif failure == "psu":
                    self.provider.session["psu_type"] = "business"
                else:
                    self.provider.session["access"]["valid_until"] = "1970-01-01T00:00:00+00:00"
                self.assertEqual(self.complete(result)["status"], "reconciliation_required")
                self.assertEqual(self.rows(db.BudgetAccountIdentity), [])
                self.assertIn("account_identity_evidence", self.candidate_metadata(result))
                # Keep each synthetic case independent without creating another production flow.
                if failure != "cancel":
                    self.provider.hooks.clear()
                    self.provider.session["aspsp"]["country"] = "PL"
                    self.provider.session["psu_type"] = "personal"
                    from tests.test_budget_consent_service import iso
                    self.provider.session["access"]["valid_until"] = iso(self.now + 86400)
                    self.store.cancel("owner", result["flow_id"], AUTH, consent_service._token_hash(result["_nonce"]))
                self.now += 3601

    def test_optional_bad_fields_and_duplicate_source_rows_do_not_break_consent(self):
        self.evidence()
        rows = self.provider.created["accounts"]
        rows += [None, {"uid": "invalid", "identification_hash": "hash-a"}, account(UID_A, "hash-a", "conflicting")]
        rows[1]["account_id"] = {"iban": "invalid"}
        result, _ = self.stage()
        self.assertEqual(self.complete(result)["status"], "adopted")
        self.assertEqual(self.rows(db.BudgetAccountIdentity), [])

    def test_adoption_identity_change_latches_accepted_transfer_in_the_same_transaction(self):
        self.evidence()
        with self.sessions.begin() as session:
            for account_id, reference, direction in ((self.accounts["hash-a"], "debit", "DBIT"),
                                                     (self.accounts["hash-b"], "credit", "CRDT")):
                session.add(db.BudgetTransaction(account_id=account_id, entry_reference=reference,
                    payload_json=json.dumps(payload(amount="100", credit_debit_indicator=direction)),
                    source="api", observed_at=self.now, api_observed_at=self.now, uncertain=False))
        analytics = BudgetAnalyticsViews(self.sessions, clock=lambda: self.now, cursor_secret=b"synthetic")
        candidate = analytics.transfers("owner", "2026")["transfers"][0]
        saved = analytics.decide_transfer("owner", debit={key: candidate["debit"][key] for key in ("account_id", "entry_reference")},
            credit={key: candidate["credit"][key] for key in ("account_id", "entry_reference")}, action="accept", expected_version=0,
            expected_debit_fingerprint=candidate["debit"]["fingerprint"], expected_credit_fingerprint=candidate["credit"]["fingerprint"])
        result, _ = self.stage()
        self.assertEqual(self.complete(result)["status"], "adopted")
        self.assertEqual(analytics.transfers("owner", "2026", status="review")["transfers"][0]["id"], saved["id"])
        self.assertEqual(len(self.rows(db.BudgetTransferLeg)), 2)
        self.assertEqual(analytics.overview("owner", "2026")["currencies"][0]["expenses"], "100")

    def test_identity_and_adoption_roll_back_together_then_verified_candidate_retries_without_http(self):
        self.evidence()
        result, _ = self.stage()
        with mock.patch("hestia.budget.analytics_reconcile.reconcile_transfers", side_effect=RuntimeError("synthetic")):
            self.assertEqual(self.complete(result)["status"], "reconciliation_required")
        self.assertEqual(self.rows(db.BudgetAccountIdentity), [])
        self.assertEqual(self.connection().credential_mode, "external")
        self.assertIsNotNone(self.store.candidate_for_flow("owner", result["flow_id"])["valid_until"])
        count = len(self.provider.calls)
        self.assertEqual(self.complete(result)["status"], "adopted")
        self.assertEqual(len(self.provider.calls), count)
        self.assertEqual(len(self.rows(db.BudgetAccountIdentity)), 2)

    def test_late_owner_or_generation_fence_prevents_all_identity_writes(self):
        self.evidence()
        result, _ = self.stage()
        original = self.store.adopt
        def changed(*args, **kwargs):
            self.connection_changes(consent_generation=99)
            return original(*args, **kwargs)
        with mock.patch.object(self.store, "adopt", side_effect=changed):
            self.assertEqual(self.complete(result)["status"], "reconciliation_required")
        self.assertEqual(self.rows(db.BudgetAccountIdentity), [])
        self.assertEqual(self.connection().credential_mode, "external")

    def test_older_post_evidence_never_overwrites_a_newer_observation(self):
        self.evidence()
        with self.sessions.begin() as session:
            session.add(db.BudgetAccountIdentity(account_id=self.accounts["hash-a"], owner="owner",
                identities_json=json.dumps([{"scheme": "BBAN", "identification": "newer"}]),
                source="api", observed_at=self.now + 100, version=7))
        result, _ = self.stage()
        self.assertEqual(self.complete(result)["status"], "adopted")
        first = next(row for row in self.rows(db.BudgetAccountIdentity) if row.account_id == self.accounts["hash-a"])
        self.assertEqual((json.loads(first.identities_json)[0]["identification"], first.version), ("newer", 7))

    def test_optional_evidence_validation_bounds_and_exact_allowlist(self):
        observed = self.now
        good = {"identification_hash": "hash-a", "identities": [{"scheme": "BBAN", "identification": "synthetic"}],
                "observed_at": observed}
        for bad in (None, [], {"x": 1}, good | {"extra": "not-retained"}, good | {"identification_hash": ""},
                    good | {"identities": []}, good | {"observed_at": True}, good | {"observed_at": float("nan")},
                    good | {"observed_at": observed + 1}, good | {"observed_at": -1}, good | {"observed_at": 10 ** 1000}):
            self.assertEqual(consent_store._identity_evidence({UID_A: bad}, self.now), {})
        self.assertEqual(consent_store._identity_evidence({"bad-uid": good}, self.now), {})
        self.assertEqual(consent_store._identity_evidence([], self.now), {})
        with mock.patch.object(consent_store, "_IDENTITY_LIMIT", 0):
            self.assertEqual(consent_store._identity_evidence({UID_A: good}, self.now), {})
            self.assertEqual(consent_store._account_uid_map({"hash-a": UID_A}, {"hash-a"}), {})
        with mock.patch.object(consent_store, "_IDENTITY_BYTES", 0):
            self.assertEqual(consent_store._identity_evidence({UID_A: good}, self.now), {})
        self.assertEqual(consent_store._account_uid_map({"foreign": UID_A, "hash-a": "bad"}, {"hash-a"}), {})
        self.assertEqual(consent_store._account_uid_map([], {"hash-a"}), {})
        self.assertEqual(consent_service._post_identity_evidence({"accounts": []}, "PL", self.now, allowed_hashes={"hash-a"}), {})
        self.assertEqual(consent_service._post_identity_evidence({}, "PL", self.now, allowed_hashes={"hash-a"}), {})
        with mock.patch.object(consent_service, "_IDENTITY_LIMIT", 0):
            self.assertEqual(consent_service._post_identity_evidence({"accounts": [account(UID_A, "hash-a", "x")]}, "PL", self.now,
                                                                    allowed_hashes={"hash-a"}), {})

