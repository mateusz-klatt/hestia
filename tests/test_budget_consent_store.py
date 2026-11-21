"""Synthetic browser bindings, one-shot exchange claims and atomic managed adoption."""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
from unittest import mock

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import select

from hestia import db
from hestia.budget.consent_store import BudgetConsentError, BudgetConsentStore
from hestia.budget.job_store import BudgetUsersRequired
from hestia.budget.store import BudgetControlConflict, BudgetNotFound, BudgetStoreError
from tests.test_budget_job_store import JobStoreCase


def hashed(text):
    return hashlib.sha256(text.encode()).hexdigest()


AUTH, NONCE = hashed("browser-session-fingerprint"), hashed("browser-nonce")


class ConsentCase(JobStoreCase):
    def setUp(self):
        super().setUp()
        self.store = BudgetConsentStore(self.sessions, clock=lambda: self.now)

    def flow(self, owner="owner", connection_id="bank", **kwargs):
        defaults = {"intent": "connect", "expected_version": self.version(connection_id),
                    "state_hash": hashed(str(uuid.uuid4())), "nonce_hash": NONCE, "auth_fingerprint": AUTH}
        return self.store.create_flow(owner, connection_id, **(defaults | kwargs))

    def staged(self, **kwargs):
        flow = self.flow(**kwargs)
        self.store.mark_started(flow["owner"], flow["id"])
        return self.store.callback(flow["state_hash"], NONCE, code_ref=str(uuid.uuid4()))

    def exchange(self, **kwargs):
        flow = self.staged(**kwargs)
        return self.store.claim_exchange(flow["owner"], flow["id"], AUTH, NONCE)

    def candidate(self, *, hashes=("hash-a", "hash-b"), **kwargs):
        flow = self.exchange(**kwargs)
        candidate = self.store.record_candidate(flow["owner"], flow["id"], capsule_ref=str(uuid.uuid4()),
                    valid_until=self.now + 86400, metadata={"account_hashes": list(hashes)})
        return flow, candidate

    def adopt(self, flow, hashes=("hash-a", "hash-b")):
        return self.store.adopt(flow["owner"], flow["id"], auth_fingerprint=AUTH, nonce_hash=NONCE,
                                selected_hashes=hashes)

    def edit_flow(self, flow, **changes):
        with self.sessions.begin() as session:
            row = session.get(db.BudgetConsentFlow, flow["id"])
            for key, value in changes.items():
                setattr(row, key, value)

    def connection_changes(self, **changes):
        with self.sessions.begin() as session:
            row = session.get(db.BudgetConnection, "bank")
            for key, value in changes.items():
                setattr(row, key, value)


class LifecycleTests(ConsentCase):
    def test_full_flow_atomic_adoption_preserves_controls_quota_and_private_payloads(self):
        self.connection_changes(paused=True, background_enabled=True, next_attempt=self.now + 7000,
                                failures=2, attention="authorization", last_error="authorization")
        with self.sessions.begin() as session:
            account = session.get(db.BudgetAccount, self.accounts["hash-a"])
            account.kind, account.kind_confirmed_by, account.kind_confirmed_at = "credit_card", "owner", self.now
            component = session.get(db.BudgetComponent, (account.id, "transactions"))
            component.next_attempt, component.retry_at, component.suspended = self.now + 9000, self.now + 8000, True
            session.add(db.BudgetAcquisition(id="prior-debit", account_id=account.id, operation="transactions", attempt_id="prior",
                                             started_at=self.now - 1, origin="operator"))
            session.add(db.BudgetTransaction(account_id=account.id, entry_reference="prior-entry", payload_json='{}',
                                             source="api", observed_at=self.now, api_observed_at=self.now, uncertain=False))
        with self.sessions() as session:
            tables = (db.BudgetAccount, db.BudgetComponent, db.BudgetAcquisition, db.BudgetTransaction,
                      db.BudgetAccountAudit, db.BudgetRequestLimit)
            before = {table: [self.store._internal(row) for row in session.scalars(select(table))] for table in tables}
        flow, candidate = self.candidate(hashes=("hash-a", "hash-b", "unconfigured-extra"))
        self.assertEqual(flow["status"], "exchanging")
        self.assertEqual(self.store.consent_selected_hashes("owner", "bank"), {"hash-a", "hash-b"})
        adopted = self.adopt(flow, self.store.consent_selected_hashes("owner", "bank"))
        self.assertEqual((adopted["id"], adopted["status"], adopted["generation"]), (candidate["id"], "ACTIVE", 1))
        connection = self.connection()
        self.assertEqual((connection.credential_mode, connection.consent_generation, connection.active_consent_id,
                          connection.control_version), ("managed", 1, candidate["id"], 2))
        self.assertEqual((connection.paused, connection.background_enabled, connection.next_attempt, connection.failures,
                          connection.attention, connection.last_error),
                         (True, True, self.now + 7000, 2, "authorization", "authorization"))
        with self.sessions() as session:
            after = {table: [self.store._internal(row) for row in session.scalars(select(table))] for table in tables}
        self.assertEqual(after, before)
        managed = self.store.managed("owner")
        self.assertEqual(managed[0]["status"], "ACTIVE")
        self.assertNotIn("capsule_ref", managed[0])
        self.assertNotIn("metadata_json", managed[0])
        self.assertEqual(self.store.managed("other"), [])
        self.assertEqual(self.store.pending("owner", AUTH, {flow["id"]: NONCE}), [])
        self.assertEqual(self.store.flow_for_state(flow["state_hash"])["status"], "adopted")
        audits = self.rows(db.BudgetConsentAudit)
        self.assertEqual([row.action for row in audits],
                         ["created", "started", "staged", "exchanging", "candidate_recorded", "adopted"])
        encoded = json.dumps([self.store._internal(row) for row in audits])
        for secret in (AUTH, NONCE, flow["state_hash"], flow["code_ref"], candidate["capsule_ref"], "hash-a"):
            self.assertNotIn(secret, encoded)
        self.assertEqual({(row.actor, row.origin) for row in audits}, {("owner", "web")})

    def test_renew_retires_only_prior_managed_session_and_never_reads_files(self):
        first, prior = self.candidate()
        self.adopt(first)
        self.now += 61
        second, fresh = self.candidate(intent="renew")
        self.adopt(second)
        states = {row.id: row.status for row in self.rows(db.BudgetConsent)}
        self.assertEqual(states, {prior["id"]: "RETIRED", fresh["id"]: "ACTIVE"})
        self.assertEqual((self.connection().consent_generation, self.connection().control_version), (2, 3))
        with self.assertRaises(BudgetConsentError):
            self.adopt(second)

    def test_cancel_during_exchange_retains_known_candidate_but_cannot_adopt(self):
        flow = self.exchange()
        canceled = self.store.cancel("owner", flow["id"], AUTH, NONCE)
        self.assertEqual(canceled["status"], "canceled")
        self.assertEqual(self.store.cancel("owner", flow["id"], AUTH, NONCE), canceled)
        candidate = self.store.record_candidate("owner", flow["id"], capsule_ref=str(uuid.uuid4()),
                       valid_until=self.now + 5000, metadata={"account_hashes": ["hash-a", "hash-b"]})
        self.assertEqual((candidate["status"], self.store.managed("owner")[0]["status"]), ("UNADOPTED", "UNADOPTED"))
        with self.assertRaises(BudgetConsentError):
            self.adopt(flow)
        self.assertIsNone(self.connection().active_consent_id)
        self.assertEqual(self.store.flow_for_state(flow["state_hash"])["status"], "canceled")

    def test_restart_marks_only_interrupted_exchange_uncertain_never_reexchanges(self):
        flow = self.exchange()
        restarted = BudgetConsentStore(self.sessions, clock=lambda: self.now)
        self.assertEqual(restarted.recover_interrupted(), 1)
        self.assertEqual(restarted.recover_interrupted(), 0)
        self.assertEqual(restarted.flow_for_state(flow["state_hash"])["status"], "exchange_uncertain")
        with self.assertRaises(BudgetConsentError):
            restarted.claim_exchange("owner", flow["id"], AUTH, NONCE)
        candidate = restarted.record_candidate("owner", flow["id"], capsule_ref=str(uuid.uuid4()),
                    valid_until=self.now + 5000, metadata={"account_hashes": ["hash-a", "hash-b"]})
        self.assertEqual(candidate["status"], "UNADOPTED")
        with self.assertRaises(BudgetConsentError):
            self.adopt(flow)

    def test_one_callback_one_exchange_and_safe_pending_binding_filters(self):
        flow = self.flow()
        self.assertEqual(self.store.pending("owner", AUTH, {}), [])
        self.assertEqual(self.store.pending("owner", hashed("another-login"), {flow["id"]: NONCE}), [])
        pending = self.store.pending("owner", AUTH, {flow["id"]: NONCE})
        self.assertEqual(pending[0]["id"], flow["id"])
        self.assertFalse(set(pending[0]) & {"state_hash", "nonce_hash", "auth_fingerprint", "code_ref"})
        started = self.store.mark_started("owner", flow["id"])
        self.assertEqual(self.store.mark_started("owner", flow["id"]), started)
        code_ref = str(uuid.uuid4())
        staged = self.store.callback(flow["state_hash"], NONCE, code_ref=code_ref)
        self.assertEqual((staged["code_ref"], staged["code_expires_at"]), (code_ref, self.now + 120))
        with self.assertRaises(BudgetConsentError):
            self.store.callback(flow["state_hash"], NONCE, code_ref=code_ref)
        claim = self.store.claim_exchange("owner", flow["id"], AUTH, NONCE)
        self.assertEqual((claim["exchange_binding_revision"], claim["exchange_flow_revision"]), (1, 1))
        with self.assertRaises(BudgetConsentError):
            self.store.claim_exchange("owner", flow["id"], AUTH, NONCE)
        with self.assertRaises(BudgetConsentError):
            self.store.mark_started("owner", flow["id"])

    def test_wrong_browser_does_not_consume_callback_or_exchange(self):
        flow = self.flow()
        with self.assertRaises(BudgetConsentError) as error:
            self.store.callback(flow["state_hash"], hashed("foreign-browser"), code_ref=str(uuid.uuid4()))
        self.assertEqual((error.exception.status, error.exception.code), (403, "budget_consent_binding_invalid"))
        staged = self.store.callback(flow["state_hash"], NONCE, code_ref=str(uuid.uuid4()))
        for auth, nonce in ((hashed("old-login"), NONCE), (AUTH, hashed("foreign-browser"))):
            with self.subTest(auth=auth), self.assertRaises(BudgetConsentError):
                self.store.claim_exchange("owner", flow["id"], auth, nonce)
        self.assertEqual(self.store.flow_for_state(flow["state_hash"])["status"], staged["status"])

    def test_login_logout_invalidates_only_matching_browser_fences_inflight_adoption(self):
        flow = self.exchange()
        for auth, bindings in ((hashed("other-session"), {flow["id"]: NONCE}),
                               (AUTH, {flow["id"]: hashed("other-browser")}), (AUTH, {"unknown": NONCE})):
            self.assertEqual(self.store.invalidate_bindings(auth, bindings), 0)
        self.assertEqual(self.store.invalidate_bindings(AUTH, {flow["id"]: NONCE}), 1)
        self.assertEqual(self.store.invalidate_bindings(AUTH, {flow["id"]: NONCE}), 0)
        row = self.store.flow_for_state(flow["state_hash"])
        self.assertEqual((row["binding_revision"], row["flow_revision"], row["error"]), (2, 2, "binding_invalid"))
        self.assertEqual(self.store.pending("owner", AUTH, {flow["id"]: NONCE}), [])

    def test_expiry_and_code_ttl_are_independent_bounded_and_pending_persists_expiry(self):
        flow = self.flow()
        self.now += 850
        staged = self.store.callback(flow["state_hash"], NONCE, code_ref=str(uuid.uuid4()))
        self.assertEqual(staged["code_expires_at"], flow["expires_at"])
        self.now += 50
        with self.assertRaises(BudgetConsentError):
            self.store.claim_exchange("owner", flow["id"], AUTH, NONCE)
        self.assertEqual(self.store.pending("owner", AUTH, {flow["id"]: NONCE}), [])
        self.assertEqual(self.store.flow_for_state(flow["state_hash"])["status"], "expired")
        fresh = self.staged()
        self.now += 120
        with self.assertRaises(BudgetConsentError):
            self.store.claim_exchange("owner", fresh["id"], AUTH, NONCE)
        self.assertEqual(self.store.flow_for_state(fresh["state_hash"])["status"], "staged")
        self.assertEqual(self.store.pending("owner", AUTH, {fresh["id"]: NONCE}), [])
        self.assertEqual(self.store.flow_for_state(fresh["state_hash"])["status"], "expired")
        self.assertEqual(self.flow()["status"], "created", "Expired code releases the one-active-connection cap")

    def test_revocation_and_restart_cleanup_work_after_original_owner_disabled_or_demoted(self):
        flow = self.exchange()
        with self.sessions.begin() as session:
            session.get(db.User, "owner").disabled = True
        self.assertEqual(self.store.invalidate_bindings(AUTH, {flow["id"]: NONCE}), 1)
        with self.sessions() as session:
            self.assertEqual(session.get(db.BudgetConsentFlow, flow["id"]).status, "canceled")
        with self.assertRaises(BudgetStoreError):
            self.store.managed("owner")
        with self.assertRaises(BudgetStoreError):
            self.adopt(flow)
        with self.sessions.begin() as session:
            session.get(db.User, "owner").disabled = False
        self.now += 61
        interrupted = self.exchange()
        with self.sessions.begin() as session:
            session.get(db.User, "owner").role = "viewer"
        self.assertEqual(self.store.recover_interrupted(), 1)
        with self.sessions() as session:
            row = session.get(db.BudgetConsentFlow, interrupted["id"])
            self.assertEqual((row.status, row.error), ("exchange_uncertain", "exchange_uncertain"))
        self.assertEqual(self.store.managed("other"), [], "Other enabled owner remains usable")
        with self.assertRaises(BudgetStoreError):
            self.flow()

    def test_expired_exchange_is_not_silently_pruned_or_retried(self):
        flow = self.exchange()
        self.now += 901
        self.assertEqual(self.store.pending("owner", AUTH, {flow["id"]: NONCE})[0]["status"], "exchanging")
        candidate = self.store.record_candidate("owner", flow["id"], capsule_ref=str(uuid.uuid4()),
                    valid_until=self.now + 5000, metadata={"account_hashes": ["hash-a", "hash-b"]})
        with self.assertRaises(BudgetConsentError):
            self.adopt(flow)
        self.assertEqual(candidate["status"], "UNADOPTED")
        self.assertEqual(self.store.pending("owner", AUTH, {flow["id"]: NONCE}), [])

    def test_callback_provider_refusal_and_safe_failure_mapping(self):
        flow = self.flow()
        refused = self.store.callback(flow["state_hash"], NONCE, error="secret-provider-url")
        self.assertEqual((refused["status"], refused["error"]), ("failed", "authorization"))
        self.assertEqual(self.store.fail_flow("owner", flow["id"], "network")["status"], "failed")
        self.now += 61
        flow = self.flow()
        self.assertEqual(self.store.callback(flow["state_hash"], NONCE, error="authorization")["error"], "authorization")
        self.now += 61
        flow = self.flow()
        self.assertEqual(self.store.fail_flow("owner", flow["id"], "secret-provider-url")["error"], "internal_error")
        self.now += 3600
        flow = self.exchange()
        failed = self.store.fail_flow("owner", flow["id"], "network")
        self.assertEqual((failed["status"], failed["error"]), ("exchange_uncertain", "network"))


class GuardTests(ConsentCase):
    def test_stale_callback_generation_is_refused_without_consuming_state(self):
        flow = self.flow()
        self.connection_changes(control_version=2)
        with self.assertRaises(BudgetConsentError) as raised:
            self.store.callback(flow["state_hash"], NONCE, code_ref=str(uuid.uuid4()))
        self.assertEqual(raised.exception.code, "budget_consent_control_changed")
        self.assertIsNone(self.store.flow_for_state(flow["state_hash"])["callback_at"])
        self.assertEqual(self.store.flow_for_state(flow["state_hash"])["status"], "created")

    def test_invalid_inputs_are_fixed_and_never_write(self):
        invalids = ({"intent": "delete"}, {"state_hash": "raw-code"}, {"state_hash": None},
                    {"nonce_hash": 12}, {"auth_fingerprint": "A" * 64})
        for values in invalids:
            with self.subTest(values=values), self.assertRaises(BudgetConsentError) as raised:
                self.flow(**values)
            self.assertEqual(str(raised.exception), "budget_consent_invalid_request")
        for expected in (None, 0, True, 2):
            with self.subTest(version=expected), self.assertRaises(BudgetStoreError):
                self.flow(expected_version=expected)
        self.assertEqual(self.rows(db.BudgetConsentFlow), [])
        for code_ref, error in ((None, None), (str(uuid.uuid4()), "authorization"), ("not-uuid", None), (1, None)):
            with self.subTest(ref=code_ref), self.assertRaises(BudgetConsentError):
                self.store.callback(hashed("unknown"), NONCE, code_ref=code_ref, error=error)
        for action in (lambda: self.store.callback(hashed("unknown"), NONCE, error="authorization"),
                       lambda: self.store.flow_for_state(hashed("unknown")),
                       lambda: self.store.mark_started("owner", "unknown")):
            with self.assertRaises(BudgetNotFound):
                action()

    def test_owner_authority_roles_and_disabled_account_rechecked_at_every_write(self):
        flow = self.exchange()
        for operation in (lambda: self.store.mark_started("other", flow["id"]),
                          lambda: self.store.consent_selected_hashes("other", "bank"),
                          lambda: self.store.cancel("other", flow["id"], AUTH, NONCE)):
            with self.assertRaises(BudgetNotFound):
                operation()
        with self.sessions.begin() as session:
            session.delete(session.get(db.AppMeta, "users_authoritative"))
        for operation in (lambda: self.store.flow_for_state(flow["state_hash"]),
                          lambda: self.store.managed("owner"), lambda: self.store.recover_interrupted(),
                          lambda: self.store.invalidate_bindings(AUTH, {flow["id"]: NONCE})):
            with self.assertRaises(BudgetUsersRequired):
                operation()
        with self.sessions.begin() as session:
            session.add(db.AppMeta(key="users_authoritative", value="1"))
            session.get(db.User, "owner").disabled = True
        with self.assertRaises(BudgetStoreError):
            self.store.record_candidate("owner", flow["id"], capsule_ref=str(uuid.uuid4()),
                                        valid_until=self.now + 5000, metadata={"account_hashes": []})
        with self.sessions.begin() as session:
            user = session.get(db.User, "owner")
            user.disabled, user.role = False, "viewer"
        with self.assertRaises(BudgetStoreError):
            self.flow()

    def test_state_unique_rollback_and_connection_disabled(self):
        flow = self.flow()
        self.store.cancel("owner", flow["id"], AUTH, NONCE)
        self.now += 61
        with self.assertRaises(BudgetConsentError):
            self.flow(state_hash=flow["state_hash"])
        self.assertEqual(len(self.rows(db.BudgetConsentFlow)), 1)
        self.assertEqual(len(self.rows(db.BudgetConsentAudit)), 2)
        self.connection_changes(enabled=False)
        with self.assertRaises(BudgetNotFound):
            self.flow()

    def test_throttles_survive_restart_and_cancel_does_not_reset_limits(self):
        first = self.flow()
        self.store.cancel("owner", first["id"], AUTH, NONCE)
        self.store = BudgetConsentStore(self.sessions, clock=lambda: self.now)
        with self.assertRaises(BudgetConsentError) as limited:
            self.flow()
        self.assertEqual((limited.exception.code, limited.exception.status), ("budget_consent_rate_limited", 429))
        for _ in range(2):
            self.now += 60
            flow = self.flow()
            self.store.cancel("owner", flow["id"], AUTH, NONCE)
        self.now += 60
        with self.assertRaises(BudgetConsentError):
            self.flow()
        self.now += 3600
        self.assertEqual(self.flow()["status"], "created")

    def test_active_caps_owner_and_connection_and_expires_before_counting(self):
        first = self.flow()
        self.now += 61
        with self.assertRaises(BudgetConsentError) as busy:
            self.flow()
        self.assertEqual(busy.exception.code, "budget_consent_in_progress")
        for key in ("second", "third"):
            with self.sessions.begin() as session:
                session.add(db.BudgetConnection(id=key, owner="owner", application_id=self.config.application_id,
                            bank_name=key, country="PL", enabled=True, next_attempt=0, failures=0))
        self.flow(connection_id="second")
        with self.assertRaises(BudgetConsentError):
            self.flow(connection_id="third")
        self.now += 900
        self.assertEqual(self.flow(connection_id="third")["status"], "created")
        self.assertEqual(self.store.flow_for_state(first["state_hash"])["status"], "expired")

    def test_exchange_checks_all_generation_identity_code_and_flow_fences(self):
        flow = self.staged()
        checks = ({"binding_revision": 2}, {"expires_at": self.now}, {"code_expires_at": self.now})
        for change in checks:
            with self.subTest(change=change):
                self.edit_flow(flow, **change)
                with self.assertRaises(BudgetConsentError):
                    self.store.claim_exchange("owner", flow["id"], AUTH, NONCE)
                self.edit_flow(flow, **{key: flow[key] for key in change})
        original = self.connection()
        for change in ({"enabled": False}, {"bank_name": "Changed"}, {"consent_generation": 9},
                       {"control_version": 9}):
            with self.subTest(change=change):
                self.connection_changes(**change)
                with self.assertRaises(BudgetConsentError) as fenced:
                    self.store.claim_exchange("owner", flow["id"], AUTH, NONCE)
                self.assertEqual(fenced.exception.code, "budget_consent_control_changed")
                self.connection_changes(**{key: getattr(original, key) for key in change})
        self.assertEqual(self.store.claim_exchange("owner", flow["id"], AUTH, NONCE)["status"], "exchanging")

    def test_expired_unstarted_flow_cannot_start_or_callback(self):
        flow = self.flow()
        self.now += 900
        with self.assertRaises(BudgetConsentError):
            self.store.mark_started("owner", flow["id"])
        with self.assertRaises(BudgetConsentError):
            self.store.callback(flow["state_hash"], NONCE, code_ref=str(uuid.uuid4()))


class CandidateTests(ConsentCase):
    def test_revoked_exchange_owner_can_only_retain_minimal_known_session_or_terminalize_unknown(self):
        for changes in ({"disabled": True}, {"role": "viewer"}):
            with self.subTest(changes=changes):
                flow = self.exchange()
                with self.sessions.begin() as session:
                    user = session.get(db.User, "owner")
                    for key, value in changes.items():
                        setattr(user, key, value)
                candidate = self.store.record_candidate("owner", flow["id"], capsule_ref=str(uuid.uuid4()),
                                  valid_until=None, metadata={"account_hashes": []})
                self.assertEqual((candidate["status"], candidate["valid_until"]), ("UNADOPTED", None))
                with self.assertRaises(BudgetStoreError):
                    self.store.record_candidate("owner", flow["id"], capsule_ref=candidate["capsule_ref"],
                          valid_until=self.now + 1000, metadata={"account_hashes": ["hash-a", "hash-b"]})
                with self.assertRaises(BudgetStoreError):
                    self.adopt(flow)
                with self.assertRaises(BudgetStoreError):
                    self.store.managed("owner")
                self.assertIsNone(self.connection().active_consent_id)
                with self.sessions.begin() as session:
                    user = session.get(db.User, "owner")
                    user.disabled, user.role = False, "admin"
                self.store.cancel("owner", flow["id"], AUTH, NONCE)
                self.now += 61

    def test_cleanup_requires_exact_owner_claim_immutable_context_and_authoritative_backend(self):
        unclaimed = self.flow()
        with self.sessions.begin() as session:
            session.get(db.User, "owner").disabled = True
        for operation in (lambda: self.store.fail_flow("owner", unclaimed["id"], "network"),
                          lambda: self.store.record_candidate("owner", unclaimed["id"], capsule_ref=str(uuid.uuid4()),
                                  valid_until=None, metadata={"account_hashes": []})):
            with self.assertRaises(BudgetStoreError):
                operation()
        with self.sessions.begin() as session:
            session.get(db.User, "owner").disabled = False
        self.store.cancel("owner", unclaimed["id"], AUTH, NONCE)
        self.now += 61
        flow = self.exchange()
        with self.assertRaises(BudgetNotFound):
            self.store.fail_flow("other", flow["id"], "network")
        with self.assertRaises(BudgetNotFound):
            self.store.fail_flow("owner", "unknown", "network")
        self.connection_changes(bank_name="changed")
        with self.assertRaises(BudgetConsentError):
            self.store.fail_flow("owner", flow["id"], "network")
        self.connection_changes(bank_name="Synthetic Bank")
        with self.sessions.begin() as session:
            session.get(db.User, "owner").disabled = True
            session.delete(session.get(db.AppMeta, "users_authoritative"))
        with self.assertRaises(BudgetUsersRequired):
            self.store.record_candidate("owner", flow["id"], capsule_ref=str(uuid.uuid4()),
                                          valid_until=None, metadata={"account_hashes": []})
        with self.assertRaises(BudgetUsersRequired):
            self.store.fail_flow("owner", flow["id"], "network")
        with self.sessions.begin() as session:
            session.add(db.AppMeta(key="users_authoritative", value="1"))
        failed = self.store.fail_flow("owner", flow["id"], "network")
        self.assertEqual((failed["status"], failed["error"]), ("exchange_uncertain", "network"))
        self.assertEqual(self.rows(db.BudgetConsent), [])

    def test_known_session_is_retained_before_verification_and_upgraded_only_once(self):
        flow = self.exchange()
        self.assertIsNone(self.store.candidate_for_flow("owner", flow["id"]))
        self.assertEqual(self.store.flow_for_browser("owner", flow["id"], AUTH, NONCE)["id"], flow["id"])
        with self.assertRaises(BudgetConsentError):
            self.store.flow_for_browser("owner", flow["id"], AUTH, hashed("different-browser"))
        ref = str(uuid.uuid4())
        known = self.store.record_candidate("owner", flow["id"], capsule_ref=ref, valid_until=None,
                                            metadata={"account_hashes": []})
        self.assertEqual(self.store.candidate_for_flow("owner", flow["id"]), known)
        self.assertIsNone(known["valid_until"])
        # An empty selection does not make an unverified session eligible for adoption.
        with self.sessions.begin() as session:
            for account in session.scalars(select(db.BudgetAccount).where(db.BudgetAccount.owner == "owner")):
                account.enabled = False
        with self.assertRaises(BudgetConsentError):
            self.adopt(flow, ())
        self.assertEqual(self.store.record_candidate("owner", flow["id"], capsule_ref=ref, valid_until=None,
                         metadata={"account_hashes": []}), known)
        with self.assertRaises(BudgetConsentError):
            self.store.record_candidate("owner", flow["id"], capsule_ref=ref, valid_until=None,
                                        metadata={"account_hashes": ["hash-a"]})
        verified = self.store.record_candidate("owner", flow["id"], capsule_ref=ref, valid_until=self.now + 5000,
                                               metadata={"account_hashes": ["hash-a"]})
        self.assertEqual((verified["id"], verified["valid_until"]), (known["id"], self.now + 5000))
        self.assertEqual(self.store.record_candidate("owner", flow["id"], capsule_ref=ref,
                         valid_until=self.now + 5000, metadata={"account_hashes": ["hash-a"]}), verified)
        with self.assertRaises(BudgetConsentError):
            self.store.record_candidate("owner", flow["id"], capsule_ref=ref, valid_until=self.now + 6000,
                                        metadata={"account_hashes": ["hash-a"]})
        self.adopt(flow, ())

    def test_candidate_validation_and_repeat_never_reprocesses_or_replaces_session(self):
        flow = self.exchange()
        ref = str(uuid.uuid4())
        invalid = ({"capsule_ref": "secret"}, {"capsule_ref": None}, {"valid_until": float("nan")},
                   {"valid_until": float("inf")}, {"valid_until": True}, {"valid_until": "tomorrow"},
                   {"valid_until": self.now}, {"metadata": []}, {"metadata": {"secret": "raw-session"}},
                   {"metadata": {"account_hashes": "hash-a"}}, {"metadata": {"account_hashes": [1]}},
                   {"metadata": {"account_hashes": [""]}}, {"metadata": {"account_hashes": ["x" * 1025]}},
                   {"metadata": {"account_hashes": ["a\x00b"]}}, {"metadata": {"account_hashes": ["a\x7fb"]}},
                   {"metadata": {"account_hashes": ["a", "a"]}})
        defaults = {"capsule_ref": ref, "valid_until": self.now + 5000,
                    "metadata": {"account_hashes": ["hash-b", "hash-a"]}}
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(BudgetConsentError):
                self.store.record_candidate("owner", flow["id"], **(defaults | values))
        self.assertEqual(self.rows(db.BudgetConsent), [])
        candidate = self.store.record_candidate("owner", flow["id"], **defaults)
        self.assertEqual(self.store.record_candidate("owner", flow["id"], **defaults), candidate)
        with self.assertRaises(BudgetConsentError):
            self.store.record_candidate("owner", flow["id"], **(defaults | {"capsule_ref": str(uuid.uuid4())}))
        self.assertEqual(len(self.rows(db.BudgetConsent)), 1)
        self.now += 3600
        unclaimed = self.flow()
        with self.assertRaises(BudgetConsentError):
            self.store.record_candidate("owner", unclaimed["id"], **defaults)

    def test_adoption_requires_same_binding_revision_connection_and_selected_coverage(self):
        flow, candidate = self.candidate()
        for auth, nonce in ((hashed("new-login"), NONCE), (AUTH, hashed("new-browser"))):
            with self.assertRaises(BudgetConsentError):
                self.store.adopt("owner", flow["id"], auth_fingerprint=auth, nonce_hash=nonce,
                                 selected_hashes=["hash-a", "hash-b"])
        for change in ({"binding_revision": 2}, {"flow_revision": 2}):
            self.edit_flow(flow, **change)
            with self.assertRaises(BudgetConsentError):
                self.adopt(flow)
            self.edit_flow(flow, **{key: flow[key] for key in change})
        with self.assertRaises(BudgetConsentError):
            self.adopt(flow, ("hash-a",))
        with self.sessions.begin() as session:
            row = session.get(db.BudgetAccount, self.accounts["hash-b"])
            row.enabled = False
        with self.assertRaises(BudgetConsentError):
            self.adopt(flow)
        self.adopt(flow, ("hash-a",))
        self.assertEqual(self.connection().active_consent_id, candidate["id"])

    def test_candidate_coverage_expiry_identity_and_status_fail_without_changes(self):
        flow, candidate = self.candidate(hashes=("hash-a",))
        with self.assertRaises(BudgetConsentError):
            self.adopt(flow)
        with self.sessions.begin() as session:
            row = session.get(db.BudgetConsent, candidate["id"])
            row.metadata_json = json.dumps({"account_hashes": ["hash-a", "hash-b"]})
        for change in ({"valid_until": self.now}, {"status": "RETIRED"}, {"bank_name": "Other bank"}):
            with self.subTest(change=change):
                with self.sessions.begin() as session:
                    row = session.get(db.BudgetConsent, candidate["id"])
                    for key, value in change.items():
                        setattr(row, key, value)
                with self.assertRaises(BudgetConsentError):
                    self.adopt(flow)
                with self.sessions.begin() as session:
                    row = session.get(db.BudgetConsent, candidate["id"])
                    for key in change:
                        setattr(row, key, candidate[key])
        self.assertIsNone(self.connection().active_consent_id)
        self.assertEqual(self.connection().control_version, 1)
        self.adopt(flow)

    def test_prior_managed_pointer_must_be_owned_active_and_matching_generation(self):
        first, prior = self.candidate()
        self.adopt(first)
        self.now += 61
        flow, _ = self.candidate(intent="renew")
        for change in ({"status": "UNADOPTED"}, {"country": "DE"}, {"generation": 99}):
            with self.subTest(change=change):
                with self.sessions.begin() as session:
                    row = session.get(db.BudgetConsent, prior["id"])
                    for key, value in change.items():
                        setattr(row, key, value)
                with self.assertRaises(BudgetConsentError):
                    self.adopt(flow)
                with self.sessions.begin() as session:
                    row = session.get(db.BudgetConsent, prior["id"])
                    for key in change:
                        setattr(row, key, "ACTIVE" if key == "status" else prior[key])
        self.connection_changes(active_consent_id="missing")
        with self.assertRaises(BudgetConsentError):
            self.adopt(flow)
        self.connection_changes(active_consent_id=prior["id"])
        self.adopt(flow)

    def test_transaction_failure_rolls_back_pointer_candidate_and_audit(self):
        flow, candidate = self.candidate()
        before = len(self.rows(db.BudgetConsentAudit))
        with mock.patch.object(self.store, "_transition", side_effect=RuntimeError("synthetic-disk-error")):
            with self.assertRaises(RuntimeError):
                self.adopt(flow)
        self.assertEqual(self.connection().credential_mode, "external")
        self.assertEqual(self.connection().consent_generation, 0)
        self.assertIsNone(self.connection().active_consent_id)
        self.assertEqual(self.rows(db.BudgetConsent)[0].status, candidate["status"])
        self.assertEqual(len(self.rows(db.BudgetConsentAudit)), before)

    def test_invalid_local_references_never_expose_other_owner_or_retire_external_credentials(self):
        flow, candidate = self.candidate()
        for pointer in ("missing", None):
            self.edit_flow(flow, candidate_consent_id=pointer)
            if pointer is None:
                self.assertIsNone(self.store.candidate_for_flow("owner", flow["id"]))
                with self.assertRaises(BudgetConsentError):
                    self.adopt(flow)
            else:
                with self.assertRaises(BudgetConsentError):
                    self.store.candidate_for_flow("owner", flow["id"])
        self.edit_flow(flow, candidate_consent_id=candidate["id"])
        with self.sessions.begin() as session:
            row = session.get(db.BudgetConsent, candidate["id"])
            row.owner = "other"
        with self.assertRaises(BudgetConsentError):
            self.store.candidate_for_flow("owner", flow["id"])
        with self.sessions.begin() as session:
            session.get(db.BudgetConsent, candidate["id"]).owner = "owner"
        self.connection_changes(credential_mode="invalid-mode")
        with self.assertRaises(BudgetConsentError):
            self.adopt(flow)
        self.connection_changes(credential_mode="external", active_consent_id=candidate["id"])
        with self.assertRaises(BudgetConsentError):
            self.adopt(flow)
        self.assertEqual(self.rows(db.BudgetConsent)[0].status, "UNADOPTED")

    def test_concurrent_callbacks_and_exchanges_claim_only_once(self):
        flow = self.flow()
        barrier = threading.Barrier(2)

        def run(action):
            barrier.wait()
            try:
                action()
                return "claimed"
            except BudgetConsentError:
                return "refused"

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(run, lambda: self.store.callback(flow["state_hash"], NONCE,
                       code_ref=str(uuid.uuid4()))) for _ in range(2)]
            self.assertCountEqual([future.result() for future in futures], ["claimed", "refused"])
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(run, lambda: self.store.claim_exchange("owner", flow["id"], AUTH, NONCE))
                       for _ in range(2)]
            self.assertCountEqual([future.result() for future in futures], ["claimed", "refused"])
        self.assertEqual([row.action for row in self.rows(db.BudgetConsentAudit)], ["created", "staged", "exchanging"])

    def test_schema_matches_and_existing_external_connection_backfills_without_history_loss(self):
        engine = self.sessions.kw["bind"]
        with engine.connect() as connection:
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), db.metadata), [])
        with engine.begin() as connection:
            command.downgrade(db._alembic_config(connection), "0013")
            self.assertEqual(connection.exec_driver_sql("SELECT count(*) FROM budget_accounts").scalar(), 3)
            command.upgrade(db._alembic_config(connection), "head")
        self.assertEqual((self.connection().credential_mode, self.connection().consent_generation,
                          self.connection().active_consent_id), ("external", 0, None))
        self.assertEqual(len(self.rows(db.BudgetAccount)), 3)
