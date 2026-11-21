"""Synthetic control plane: no bank access, real credentials or production database."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import uuid
from unittest import mock

from hestia import db
from hestia.budget import consent_service as module
from hestia.budget.client import BudgetAPIError
from hestia.budget.config import BudgetConfigError
from hestia.budget.consent_client import PSUContext
from hestia.budget.consent_files import ConsentFiles
from hestia.budget.consent_service import BudgetConsentService
from hestia.budget.consent_store import BudgetConsentError
from hestia.budget.job_store import BudgetUsersRequired
from hestia.budget.runtime import budget_lock
from hestia.budget.store import BudgetNotFound, BudgetStoreError
from tests.test_budget_consent_store import AUTH, ConsentCase, hashed

SESSION_ID = "bc822775-0db4-4d2f-ad3a-e5389430f214"
UID_A = "2046ec1b-fac9-43a1-9abb-d092565062ce"
UID_B = "a57ca3de-fe82-45da-8c28-c3dab2d28a4d"
UID_EXTRA = "aee236a6-ceb4-47ef-95f8-03cc079019fd"
PRIVATE = "synthetic-private-secret-do-not-print"


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


class SyntheticProvider:
    def __init__(self, case):
        self.case, self.calls, self.failures, self.hooks = case, [], {}, {}
        self.banks = {"aspsps": [{"name": "Synthetic Bank", "country": "PL", "psu_types": ["personal"],
                                  "maximum_consent_validity": 86400, "required_psu_headers": []}]}
        # The shape Enable Banking's POST /auth has actually returned (host and path; synthetic query).
        self.authorization = {"url": "https://tilisy.enablebanking.com/ais/start?sessionid=synthetic"}
        self.created = {"session_id": SESSION_ID, "never_public": PRIVATE}
        self.session = {"status": "AUTHORIZED", "aspsp": {"name": "Synthetic Bank", "country": "PL"},
                        "psu_type": "personal", "access": {"valid_until": iso(case.now + 86400)},
                        "accounts": [UID_A, UID_B, UID_EXTRA],
                        "accounts_data": [{"uid": UID_A, "identification_hash": "hash-a"},
                                          {"uid": UID_B, "identification_hash": "hash-b"},
                                          {"uid": UID_EXTRA, "identification_hash": "unselected-extra"}]}
        self.factories = []

    def factory(self, app, key, **kwargs):
        self.factories.append((app, key, kwargs))
        return self

    def request(self, method, path, payload, **kwargs):
        self.calls.append((method, path, deepcopy(payload), kwargs))
        hook = self.hooks.get((method, path))
        if hook is not None:
            hook()
        failure = self.failures.get((method, path))
        if failure is not None:
            raise failure
        if method == "GET":
            return deepcopy(self.banks if path == "/aspsps" else self.session)
        return deepcopy(self.authorization if path == "/auth" else self.created)

    def get(self, path, query=None, **kwargs):
        return self.request("GET", path, query, **kwargs)

    def post(self, path, body, **kwargs):
        return self.request("POST", path, body, **kwargs)


class ServiceCase(ConsentCase):
    def setUp(self):
        super().setUp()
        self.monotonic_now = 100.0
        self.sleeps = []
        self.key = Path(self.directory.name) / "synthetic.key"
        self.key.write_bytes(b"synthetic-fake-key")
        self.key.chmod(0o600)
        self.config = replace(self.config, key_file=str(self.key))
        self.files = ConsentFiles(Path(self.directory.name) / "capsules")
        self.provider = SyntheticProvider(self)
        self.service = self.service_factory()

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay
        self.monotonic_now += delay

    def service_factory(self, **kwargs):
        defaults = {"path": self.path, "redirect_url": "https://hestia.example/api/budget/enable-banking/callback",
                    "client_factory": self.provider.factory, "clock": lambda: self.now,
                    "monotonic": lambda: self.monotonic_now, "sleeper": self.sleep}
        return BudgetConsentService(self.config, self.store, self.files, **(defaults | kwargs))

    def start(self, **kwargs):
        defaults = {"intent": "renew", "expected_version": self.version(), "auth_fingerprint": AUTH}
        result = self.service.start("owner", "bank", **(defaults | kwargs))
        state = next(call[2]["state"] for call in reversed(self.provider.calls) if call[:2] == ("POST", "/auth"))
        return result, state

    def stage(self, **kwargs):
        result, state = self.start(**kwargs)
        self.service.callback(state, result["_nonce"], code="synthetic-once-code")
        return result, state

    def complete(self, result):
        return self.service.complete("owner", result["flow_id"], auth_fingerprint=AUTH, nonce=result["_nonce"])

    def method_calls(self, method, path):
        return [call for call in self.provider.calls if call[:2] == (method, path)]


class ServiceLifecycleTests(ServiceCase):
    def test_owner_revoked_during_post_retains_unadopted_known_session_and_stops_http(self):
        for changes in ({"disabled": True}, {"role": "operator"}):
            with self.subTest(changes=changes):
                result, _ = self.stage()

                def revoke():
                    with self.sessions.begin() as session:
                        user = session.get(db.User, "owner")
                        for key, value in changes.items():
                            setattr(user, key, value)

                self.provider.hooks[("POST", "/sessions")] = revoke
                before = len(self.method_calls("GET", "/sessions/" + SESSION_ID))
                failure = self.complete(result)
                self.assertEqual((failure["status"], failure["consent"]["status"], failure["consent"]["valid_until"]),
                                 ("reconciliation_required", "UNADOPTED", None))
                self.assertEqual(len(self.method_calls("GET", "/sessions/" + SESSION_ID)), before)
                self.assertIsNone(self.connection().active_consent_id)
                candidate = next(row for row in self.rows(db.BudgetConsent) if row.id == failure["consent"]["id"])
                self.assertEqual(self.files.get(candidate.capsule_ref, "session"), {"session_id": SESSION_ID})
                with self.assertRaises(BudgetStoreError):
                    self.complete(result)
                del self.provider.hooks[("POST", "/sessions")]
                with self.sessions.begin() as session:
                    user = session.get(db.User, "owner")
                    user.disabled, user.role = False, "admin"
                self.service.cancel("owner", result["flow_id"], AUTH, result["_nonce"])
                self.now += 61
        self.assertEqual(len(self.method_calls("POST", "/sessions")), 2)

    def test_owner_revoked_during_unknown_post_is_durably_uncertain_never_reexchanged(self):
        result, state = self.stage()

        def revoke():
            with self.sessions.begin() as session:
                session.get(db.User, "owner").disabled = True

        self.provider.hooks[("POST", "/sessions")] = revoke
        self.provider.failures[("POST", "/sessions")] = BudgetAPIError(code="network")
        self.assertEqual(self.complete(result)["status"], "exchange_uncertain")
        with self.assertRaises(BudgetStoreError):
            self.complete(result)
        with self.sessions.begin() as session:
            session.get(db.User, "owner").disabled = False
        self.assertEqual(self.service.flow_for_state(state)["status"], "exchange_uncertain")
        self.assertEqual(self.complete(result)["status"], "exchange_uncertain")
        self.assertEqual(len(self.method_calls("POST", "/sessions")), 1)
        self.assertEqual(self.rows(db.BudgetConsent), [])

    def test_authority_removed_during_known_post_cannot_adopt_or_publish_candidate(self):
        result, _ = self.stage()

        def revoke():
            with self.sessions.begin() as session:
                session.delete(session.get(db.AppMeta, "users_authoritative"))

        self.provider.hooks[("POST", "/sessions")] = revoke
        with self.assertRaises(BudgetUsersRequired):
            self.complete(result)
        self.assertIsNone(self.connection().active_consent_id)
        self.assertEqual(self.rows(db.BudgetConsent), [])
        self.assertEqual(len(self.method_calls("GET", "/sessions/" + SESSION_ID)), 0)

    def test_zero_selected_accounts_can_authorize_without_implicitly_selecting_or_acquiring(self):
        for primary in ("hash-a", "hash-b"):
            self.store.set_account_selected("owner", self.accounts[primary], False, actor="owner")
        self.provider.session.update(accounts=[], accounts_data=[])
        result, _ = self.stage()
        self.assertEqual(self.complete(result)["status"], "adopted")
        self.assertEqual(self.store.consent_selected_hashes("owner", "bank"), set())
        self.assertEqual(len(self.rows(db.BudgetAccount)), 3)
        self.assertEqual(self.rows(db.BudgetAcquisition), [])

    def test_start_callback_complete_retains_selection_limits_and_never_uses_external_session(self):
        # The configured connection need not be the first connection in the file.
        self.service.config = replace(self.config, connections=tuple(reversed(self.config.connections)))
        self.connection_changes(paused=True, next_attempt=self.now + 43200, background_enabled=True,
                                last_error="authorization", attention="authorization")
        self.store.confirm_account_kind("owner", self.accounts["hash-a"], "credit_card", actor="owner")
        before_accounts = [self.store._internal(row) for row in self.rows(db.BudgetAccount)]
        before_components = [self.store._internal(row) for row in self.rows(db.BudgetComponent)]
        with mock.patch.object(module, "_read_secret", wraps=module._read_secret) as read:
            result, state = self.start()
            self.assertEqual(len(result["_nonce"]), 43)
            flow = self.service.flow_for_state(state)
            self.assertNotIn(state, json.dumps(flow))
            self.assertNotIn(result["_nonce"], json.dumps(flow))
            auth = self.method_calls("POST", "/auth")[0]
            self.assertEqual(auth[2]["aspsp"], {"name": "Synthetic Bank", "country": "PL"})
            self.assertEqual(auth[2]["psu_type"], "personal")
            self.assertEqual(auth[2]["redirect_url"], self.service.redirect_url)
            self.assertEqual(datetime.fromisoformat(auth[2]["access"]["valid_until"]).timestamp(), 1_000_000 + 86400 - 30)
            self.assertEqual(auth[2]["access"], {"valid_until": iso(1_000_000 + 86400 - 30),
                             "balances": True, "transactions": True, "transactions_extended_history": True})
            count = len(self.provider.calls)
            callback = self.service.callback(state, result["_nonce"], code="synthetic-once-code")
            self.assertEqual(callback["status"], "staged")
            pending = self.service.pending("owner", AUTH, {result["flow_id"]: result["_nonce"], "bad": "bad-cookie"})
            self.assertEqual(pending[0]["id"], result["flow_id"])
            self.assertEqual(self.service.status("owner"), [])
            self.assertEqual(len(self.provider.calls), count, "Callback and read endpoints must stay local")
            completed = self.complete(result)
            self.assertEqual(completed["status"], "adopted")
            self.assertEqual(completed["consent"]["status"], "ACTIVE")
            self.assertEqual(self.complete(result), completed, "Repeated completion must not POST again")
            self.assertEqual({call.args[0] for call in read.call_args_list}, {str(self.key)})
        self.assertEqual(len(self.method_calls("POST", "/sessions")), 1)
        self.assertEqual(len(self.method_calls("GET", "/sessions/" + SESSION_ID)), 1)
        self.assertEqual([self.store._internal(row) for row in self.rows(db.BudgetAccount)], before_accounts)
        self.assertEqual([self.store._internal(row) for row in self.rows(db.BudgetComponent)], before_components)
        self.assertEqual(self.rows(db.BudgetAcquisition), [])
        connection = self.connection()
        self.assertEqual((connection.paused, connection.next_attempt, connection.background_enabled),
                         (True, 1_043_200, True))
        self.assertEqual(self.service.status("owner")[0]["id"], completed["consent"]["id"])
        serialized = json.dumps(completed)
        for secret in (PRIVATE, SESSION_ID, "hash-a", "hash-b", "synthetic-once-code", "capsule_ref"):
            self.assertNotIn(secret, serialized)
        self.assertTrue(self.sleeps, "Interactive requests still obey shared application pacing")
        self.assertEqual({call[3]["deadline"] for call in self.method_calls("POST", "/sessions")}, {221})

    def test_known_candidate_persisted_before_get_failure_and_retry_never_posts_code_again(self):
        result, state = self.stage()
        self.provider.failures[("GET", "/sessions/" + SESSION_ID)] = BudgetAPIError(503, "unavailable")
        failure = self.complete(result)
        self.assertEqual((failure["status"], failure["error"], failure["consent"]["valid_until"]),
                         ("reconciliation_required", "unavailable", None))
        candidate = self.store.candidate_for_flow("owner", result["flow_id"])
        self.assertEqual(self.files.get(candidate["capsule_ref"], "session"), {"session_id": SESSION_ID})
        self.assertEqual(self.service.flow_for_state(state)["status"], "candidate")
        self.assertIsNone(self.connection().active_consent_id)
        del self.provider.failures[("GET", "/sessions/" + SESSION_ID)]
        self.assertEqual(self.complete(result)["status"], "adopted")
        self.assertEqual(len(self.method_calls("POST", "/sessions")), 1)

    def test_busy_adoption_retry_uses_known_verified_candidate_without_provider_calls(self):
        result, _ = self.stage()
        with budget_lock(self.path):
            failed = self.complete(result)
        self.assertEqual((failed["status"], failed["error"]), ("reconciliation_required", "busy"))
        calls = len(self.provider.calls)
        self.assertEqual(self.complete(result)["status"], "adopted")
        self.assertEqual(len(self.provider.calls), calls)

    def test_unknown_exchange_is_durable_and_restart_never_replays_post(self):
        result, state = self.stage()
        self.provider.failures[("POST", "/sessions")] = RuntimeError(PRIVATE)
        failure = self.complete(result)
        self.assertEqual((failure["status"], failure["error"]), ("exchange_uncertain", "exchange_uncertain"))
        self.assertEqual(self.service.flow_for_state(state)["status"], "exchange_uncertain")
        self.service = self.service_factory()
        self.assertEqual(self.complete(result)["status"], "exchange_uncertain")
        self.assertEqual(len(self.method_calls("POST", "/sessions")), 1)
        self.assertNotIn(PRIVATE, json.dumps(failure))

    def test_cancellation_during_post_retains_known_session_without_get_or_adoption(self):
        result, _ = self.stage()
        self.provider.hooks[("POST", "/sessions")] = lambda: self.service.cancel(
            "owner", result["flow_id"], AUTH, result["_nonce"])
        failure = self.complete(result)
        self.assertEqual(failure["status"], "reconciliation_required")
        self.assertEqual(failure["consent"]["status"], "UNADOPTED")
        self.assertEqual(len(self.method_calls("GET", "/sessions/" + SESSION_ID)), 0)
        self.assertIsNone(self.connection().active_consent_id)
        self.assertEqual(self.complete(result)["status"], "canceled")

    def test_logout_during_get_and_pause_resume_during_verification_fence_adoption(self):
        result, _ = self.stage()
        self.provider.hooks[("GET", "/sessions/" + SESSION_ID)] = lambda: self.service.invalidate_bindings(
            AUTH, {result["flow_id"]: result["_nonce"]})
        failure = self.complete(result)
        self.assertEqual(failure["consent"]["status"], "UNADOPTED")
        self.assertIsNone(self.connection().active_consent_id)
        self.now += 61
        del self.provider.hooks[("GET", "/sessions/" + SESSION_ID)]
        result, _ = self.stage()

        def toggle():
            for paused in (True, False):
                self.store.set_connection_paused("owner", "bank", paused, actor="owner",
                                                 expected_version=self.version())

        self.provider.hooks[("GET", "/sessions/" + SESSION_ID)] = toggle
        self.assertEqual(self.complete(result)["status"], "reconciliation_required")
        self.assertIsNone(self.connection().active_consent_id)

    def test_post_get_and_sleep_never_hold_sqlite_write_transaction_or_budget_lock(self):
        result, _ = self.stage()

        def independent_writer():
            with self.store.transaction() as session:
                session.add(db.AppMeta(key=str(uuid.uuid4()), value="synthetic"))
            with budget_lock(self.path):
                pass

        self.provider.hooks[("POST", "/sessions")] = independent_writer
        self.provider.hooks[("GET", "/sessions/" + SESSION_ID)] = independent_writer
        self.assertEqual(self.complete(result)["status"], "adopted")

    def test_selected_snapshot_changes_after_get_are_rechecked_without_selecting_extras(self):
        result, _ = self.stage()
        original = self.service._verify

        def deselect_after_verify(response, connection, selected):
            verified = original(response, connection, selected)
            self.store.set_account_selected("owner", self.accounts["hash-b"], False, actor="owner")
            return verified

        with mock.patch.object(self.service, "_verify", side_effect=deselect_after_verify):
            self.assertEqual(self.complete(result)["status"], "adopted")
        self.assertEqual(self.store.consent_selected_hashes("owner", "bank"), {"hash-a"})
        self.assertEqual(len(self.rows(db.BudgetAccount)), 3, "Provider extras never expand configured account metadata")

    def test_renewal_failure_preserves_existing_active_reference_and_known_candidate(self):
        first, _ = self.stage()
        self.complete(first)
        prior = self.connection().active_consent_id
        self.now += 61
        result, _ = self.stage()
        self.provider.session["psu_type"] = "business"
        failed = self.complete(result)
        self.assertEqual((failed["status"], failed["error"]), ("reconciliation_required", "session_mismatch"))
        self.assertEqual(self.connection().active_consent_id, prior)
        self.assertEqual({row.status for row in self.rows(db.BudgetConsent)}, {"ACTIVE", "UNADOPTED"})


class ValidationTests(ServiceCase):
    def test_strict_fixed_https_redirect_and_authorization_url(self):
        for value in (None, "http://hestia.example/callback", "https://user:password@hestia.example/callback",
                      "https://hestia.example:444/callback", "https://hestia.example/callback?code=private",
                      "https://hestia.example/callback#fragment", "https://hestia.example/has space",
                      "https://hestia.example/\\evil", "https://żaba.example/callback", "x" * 4097):
            with self.subTest(value=value), self.assertRaises(BudgetConsentError) as raised:
                self.service_factory(redirect_url=value)
            self.assertNotIn(str(value), str(raised.exception))
        for value in ("https://evil.example/redirect", "javascript:alert(1)", "https://auth.enablebanking.com/#token"):
            with self.subTest(value=value):
                self.provider.authorization = {"url": value}
                with self.assertRaises(BudgetConsentError):
                    self.start()
                self.now += 3600
        for response in (None, {}):
            self.provider.authorization = response
            with self.assertRaises(BudgetConsentError):
                self.start()
            self.now += 3600

    def test_authorization_url_allows_exactly_the_two_enable_banking_hosts(self):
        for url in ("https://tilisy.enablebanking.com/ais/start?sessionid=synthetic",
                    "https://auth.enablebanking.com/ais/start?sessionid=synthetic",
                    "https://TILISY.enablebanking.com/ais/start?sessionid=synthetic",
                    "https://auth.enablebanking.com:443/ais/start?sessionid=synthetic",
                    "https://tilisy.enablebanking.com:/ais/start?sessionid=synthetic"):
            with self.subTest(url=url):
                self.provider.authorization = {"url": url}
                result, _ = self.start()
                self.assertEqual(result["authorization_url"], url)
                self.now += 3600
        for value in ("https://tilisy.enablebanking.com.evil.example/ais/start", "https://evil-tilisy.enablebanking.com/ais/start",
                      "https://eviltilisy.enablebanking.com/ais/start", "https://enablebanking.com/ais/start",
                      "https://tilisy.enablebanking.co/ais/start", "https://tilisy.enablebanking.com./ais/start",
                      "https://tilisy.enablebanking.com:8443/ais/start", "http://tilisy.enablebanking.com/ais/start",
                      "https://user@tilisy.enablebanking.com/ais/start", "https://tilisy.enablebanking.com/ais/start#x",
                      "https://tilisy.enablebanking.com\\@evil.example/", "https://auth.enablebanking.com.evil.example/",
                      "https://tilisy%2eenablebanking.com/ais/start", "https://[::1]/ais/start",
                      "https://tilisy.enablebanking.com。evil.example/ais/start", "https://tilisy.enablebanking.com:0/ais/start",
                      "https://tilisy.enablebanking.com:443@evil.example/ais/start", "https://tilisy.enablebanking.com/ais/start\n"):
            with self.subTest(value=value):
                self.provider.authorization = {"url": value}
                with self.assertRaises(BudgetConsentError) as raised:
                    self.start()
                self.assertEqual((raised.exception.code, raised.exception.status), ("budget_consent_invalid_response", 503))
                self.assertNotIn(value, str(raised.exception))
                # The flow keeps the fixed diagnostic, not a masked internal_error.
                flow = max(self.rows(db.BudgetConsentFlow), key=lambda row: row.created_at)
                self.assertEqual((flow.status, flow.error), ("failed", "invalid_response"))
                self.now += 3600

    def test_every_flow_diagnostic_the_service_records_survives_the_store(self):
        from hestia.budget import consent_store

        self.assertLessEqual(module._FLOW_ERRORS | {"configuration", "internal_error"}, consent_store._ERRORS)
        self.provider.failures[("GET", "/aspsps")] = BudgetAPIError(code="acquisition_timeout", retryable=True)
        with self.assertRaises(BudgetConsentError):
            self.start()
        flow = max(self.rows(db.BudgetConsentFlow), key=lambda row: row.created_at)
        self.assertEqual((flow.status, flow.error), ("failed", "acquisition_timeout"))

    def test_bad_callback_never_uses_provider_or_consumes_valid_binding(self):
        result, state = self.start()
        count = len(self.provider.calls)
        for values in ({"state": None}, {"state": "raw"}, {"nonce": None}, {"nonce": "short"},
                       {"code": None}, {"code": "", "error": "access_denied"}, {"code": "bad code"},
                       {"code": "bad\x7fcode"}, {"code": "x" * 4097}, {"code": "ż" * 4096},
                       {"code": "\ud800"}, {"code": 7},
                       {"code": None, "error": 3}, {"code": None, "error": "bad error"}):
            arguments = {"state": state, "nonce": result["_nonce"], "code": "synthetic-code"} | values
            with self.subTest(values=values), self.assertRaises(BudgetConsentError):
                self.service.callback(**arguments)
        with self.assertRaises(BudgetConsentError):
            self.service.callback(state, "a" * 43, code="synthetic-code")
        self.assertIsNone(self.service.flow_for_state(state)["callback_at"])
        self.assertEqual(self.service.callback(state, result["_nonce"], error="access_denied")["status"], "failed")
        self.assertEqual(len(self.provider.calls), count)
        self.assertEqual(self.complete(result)["status"], "failed")

    def test_repeated_callback_no_new_secret_file_and_cancel_shape(self):
        result, state = self.stage()
        before = set(self.files.directory.iterdir())
        with self.assertRaises(BudgetConsentError):
            self.service.callback(state, result["_nonce"], code="another-code")
        self.assertEqual(set(self.files.directory.iterdir()), before)
        self.assertEqual(self.service.cancel("owner", result["flow_id"], AUTH, result["_nonce"]),
                         {"flow_id": result["flow_id"], "status": "canceled"})
        self.assertEqual(self.service.pending("owner", AUTH, {result["flow_id"]: result["_nonce"]}), [])

    def test_wrong_owner_current_auth_and_config_identity_refused_before_key_read(self):
        result, _ = self.stage()
        with mock.patch.object(module, "_read_secret", side_effect=AssertionError("must not read")):
            with self.assertRaises(BudgetNotFound):
                self.service.complete("other", result["flow_id"], auth_fingerprint=AUTH, nonce=result["_nonce"])
            with self.assertRaises(BudgetConsentError):
                self.service.complete("owner", result["flow_id"], auth_fingerprint=hashed("new-login"), nonce=result["_nonce"])
            with self.sessions.begin() as session:
                session.delete(session.get(db.AppMeta, "users_authoritative"))
            with self.assertRaises(BudgetUsersRequired):
                self.complete(result)
        with self.sessions.begin() as session:
            session.add(db.AppMeta(key="users_authoritative", value="1"))
        self.service.config = replace(self.config, application_id=str(uuid.uuid4()))
        with mock.patch.object(module, "_read_secret", side_effect=AssertionError("must not read")):
            with self.assertRaises(BudgetConsentError):
                self.complete(result)
        self.service.config = replace(self.config, connections=())
        with self.assertRaises(BudgetNotFound):
            self.complete(result)

    def test_start_capabilities_validation_never_posts_auth_when_bank_data_invalid(self):
        original = deepcopy(self.provider.banks)
        cases = (None, {}, {"aspsps": {}}, {"aspsps": []}, {"aspsps": [None]},
                 {"aspsps": original["aspsps"] * 2})
        for response in cases:
            self.provider.banks = response
            with self.assertRaises(BudgetConsentError):
                self.start()
            self.assertEqual(len(self.method_calls("POST", "/auth")), 0)
            self.now += 3600
        for changes in ({"psu_types": None}, {"psu_types": ["business"]}, {"maximum_consent_validity": None},
                        {"maximum_consent_validity": True}, {"maximum_consent_validity": 30}):
            self.provider.banks = deepcopy(original)
            self.provider.banks["aspsps"][0].update(changes)
            with self.assertRaises(BudgetConsentError):
                self.start()
            self.now += 3600

    def test_required_foreground_psu_context_is_not_fabricated_or_replayed_on_discovery(self):
        self.provider.banks["aspsps"][0]["required_psu_headers"] = ["psu-ip-address"]
        with self.assertRaises(BudgetConsentError) as raised:
            self.start()
        self.assertEqual(raised.exception.code, "budget_consent_psu_context_required")
        self.now += 61
        context = PSUContext(ip_address="192.0.2.42", user_agent="Synthetic browser")
        self.start(psu=context)
        self.assertIsNone(self.method_calls("GET", "/aspsps")[-1][3]["psu"])
        self.assertIs(self.method_calls("POST", "/auth")[-1][3]["psu"], context)
        self.now += 3600
        self.provider.banks["aspsps"][0]["required_psu_headers"] = "Psu-Ip-Address"
        with self.assertRaises(BudgetConsentError):
            self.start(psu={"arbitrary": PRIVATE})

    def test_get_session_verification_keeps_unadopted_candidate_on_bad_bank_or_mapping(self):
        for changes in ({"status": "CLOSED"}, {"aspsp": None},
                        {"aspsp": {"name": "Wrong bank", "country": "PL"}}, {"psu_type": "business"},
                        {"access": None}, {"access": {"valid_until": iso(self.now - 1)}},
                        {"accounts_data": []}, {"accounts": []}, {"accounts": "not-list"}):
            original = deepcopy(self.provider.session)
            result, _ = self.stage()
            self.provider.session.update(changes)
            failed = self.complete(result)
            self.assertEqual(failed["status"], "reconciliation_required")
            self.assertIsNone(self.connection().active_consent_id)
            self.assertEqual(self.store.candidate_for_flow("owner", result["flow_id"])["status"], "UNADOPTED")
            self.service.cancel("owner", result["flow_id"], AUTH, result["_nonce"])
            self.provider.session = original
            self.now += 3600

    def test_ambiguous_duplicate_primary_or_uid_and_unknown_post_result_never_adopt(self):
        result, _ = self.stage()
        self.provider.session["accounts_data"].append(deepcopy(self.provider.session["accounts_data"][0]))
        self.assertEqual(self.complete(result)["error"], "account_selection")
        self.assertIsNone(self.connection().active_consent_id)
        self.service.cancel("owner", result["flow_id"], AUTH, result["_nonce"])
        self.now += 61
        self.provider.created = {"session_id": "not-a-uuid", PRIVATE: PRIVATE}
        result, _ = self.stage()
        failed = self.complete(result)
        self.assertEqual(failed["status"], "exchange_uncertain")
        self.assertNotIn(PRIVATE, json.dumps(failed))

    def test_legacy_key_failure_no_credential_replacement_or_post(self):
        result, state = self.stage()
        with mock.patch.object(module, "_read_secret", side_effect=BudgetConfigError("safe test error")):
            failure = self.complete(result)
        self.assertEqual((failure["status"], failure["error"]), ("reconciliation_required", "configuration"))
        self.assertEqual(self.service.flow_for_state(state)["status"], "staged")
        self.assertEqual(len(self.method_calls("POST", "/sessions")), 0)
        self.assertEqual(self.connection().credential_mode, "external")

    def test_allowlist_removed_before_verify_or_new_selection_before_adopt_cannot_be_expanded(self):
        result, _ = self.stage()
        original = self.service.config
        changed = replace(original.connections[0], accounts=("hash-a",))
        self.service.config = replace(original, connections=(changed, *original.connections[1:]))
        self.assertEqual(self.complete(result)["error"], "account_selection")
        self.assertIsNone(self.connection().active_consent_id)
        self.service.config = original
        original_verify = self.service._verify

        def newly_selected(response, connection, selected):
            verified = original_verify(response, connection, selected)
            with self.sessions.begin() as session:
                session.add(db.BudgetAccount(id=str(uuid.uuid4()), owner="owner", connection_id="bank",
                            identification_hash="new-not-allowlisted", enabled=True, configured=True, balances_json="[]"))
            return verified

        with mock.patch.object(self.service, "_verify", side_effect=newly_selected):
            self.assertEqual(self.complete(result)["error"], "account_selection")
        self.assertIsNone(self.connection().active_consent_id)

    def test_failed_atomic_callback_claim_removes_only_its_new_code_capsule(self):
        result, state = self.start()
        self.files.put("candidate", {"unrelated-private": PRIVATE})
        before = set(self.files.directory.iterdir())
        with mock.patch.object(self.store, "callback", side_effect=BudgetConsentError()):
            with self.assertRaises(BudgetConsentError):
                self.service.callback(state, result["_nonce"], code="synthetic-code")
        self.assertEqual(set(self.files.directory.iterdir()), before)
        # No code file is created for an error callback, even if its atomic claim fails.
        with mock.patch.object(self.store, "callback", side_effect=BudgetConsentError()):
            with self.assertRaises(BudgetConsentError):
                self.service.callback(state, result["_nonce"], error="access_denied")
        self.assertEqual(set(self.files.directory.iterdir()), before)

    def test_cleanup_failure_does_not_hide_known_post_session_and_claim_still_cannot_repeat(self):
        result, state = self.stage()
        code_ref = self.service.flow_for_state(state)["code_ref"]
        with mock.patch.object(self.files, "delete", side_effect=OSError(PRIVATE)):
            self.assertEqual(self.complete(result)["status"], "adopted")
        self.assertEqual(self.files.get(code_ref, "code"), {"code": "synthetic-once-code"})
        self.assertEqual(self.complete(result)["status"], "adopted")
        self.assertEqual(len(self.method_calls("POST", "/sessions")), 1)

    def test_successful_and_uncertain_attempts_remove_code_while_deferred_keeps_it(self):
        result, state = self.stage()
        code_ref = self.service.flow_for_state(state)["code_ref"]
        self.assertEqual(self.complete(result)["status"], "adopted")
        with self.assertRaises(BudgetConfigError):
            self.files.get(code_ref, "code")
        self.now += 61
        result, state = self.stage()
        code_ref = self.service.flow_for_state(state)["code_ref"]
        self.provider.failures[("POST", "/sessions")] = BudgetAPIError(code="network")
        self.assertEqual(self.complete(result)["status"], "exchange_uncertain")
        with self.assertRaises(BudgetConfigError):
            self.files.get(code_ref, "code")


class DeadlineAndConcurrencyTests(ServiceCase):
    def test_direct_completion_after_code_ttl_durably_expires_and_releases_connection_cap(self):
        result, state = self.stage()
        calls = len(self.provider.calls)
        self.sleep(121)
        expired = self.complete(result)
        self.assertEqual(expired, {"flow_id": result["flow_id"], "status": "expired", "error": "expired"})
        self.assertEqual(len(self.provider.calls), calls)
        self.assertEqual(self.service.flow_for_state(state)["status"], "expired")
        self.assertEqual(self.service.pending("owner", AUTH, {result["flow_id"]: result["_nonce"]}), [])
        fresh, fresh_state = self.start()
        self.assertNotEqual(fresh["flow_id"], result["flow_id"])
        self.assertEqual(self.service.flow_for_state(fresh_state)["status"], "started")
        self.assertEqual(len(self.method_calls("POST", "/sessions")), 0)
        self.assertEqual(self.rows(db.BudgetAcquisition), [])
        self.assertEqual(self.connection().control_version, 1)

    def test_suspended_or_far_cooldown_never_claims_code_or_retries_automatically(self):
        result, state = self.stage()
        with self.store.transaction() as session:
            session.get(db.BudgetRequestLimit, ("application", self.config.application_id)).next_attempt = self.now + 1000
        count = len(self.provider.calls)
        failure = self.complete(result)
        self.assertEqual((failure["status"], failure["error"]), ("reconciliation_required", "rate_limit"))
        self.assertEqual(self.service.flow_for_state(state)["status"], "staged")
        self.assertEqual(len(self.provider.calls), count)
        self.assertEqual(self.rows(db.BudgetAcquisition), [])

    def test_monotonic_deadline_after_known_post_result_retains_session_without_further_http(self):
        result, _ = self.stage()
        self.provider.hooks[("POST", "/sessions")] = lambda: setattr(self, "monotonic_now", self.monotonic_now + 121)
        failure = self.complete(result)
        self.assertEqual(failure["status"], "reconciliation_required")
        self.assertIsNone(failure["consent"]["valid_until"])
        self.assertEqual(len(self.method_calls("GET", "/sessions/" + SESSION_ID)), 0)
        self.assertEqual(self.files.get(self.store.candidate_for_flow("owner", result["flow_id"])["capsule_ref"],
                                        "session"), {"session_id": SESSION_ID})

    def test_control_rate_limit_preserved_after_cancel_without_account_debits(self):
        result, _ = self.stage()
        self.provider.hooks[("POST", "/sessions")] = lambda: self.service.cancel(
            "owner", result["flow_id"], AUTH, result["_nonce"])
        self.provider.failures[("POST", "/sessions")] = BudgetAPIError(429, "rate_limit", retry_at=self.now + 90000)
        failure = self.complete(result)
        self.assertEqual(failure["status"], "canceled")
        with self.sessions() as session:
            row = session.get(db.BudgetRequestLimit, ("connection", "bank"))
            self.assertEqual(row.retry_at, 1_090_001)
        self.assertEqual(self.rows(db.BudgetAcquisition), [])

    def test_second_complete_while_post_inflight_does_not_reexchange(self):
        result, _ = self.stage()
        entered, release = threading.Event(), threading.Event()
        self.provider.hooks[("POST", "/sessions")] = lambda: (entered.set(), release.wait(5))
        outputs = []
        worker = threading.Thread(target=lambda: outputs.append(self.complete(result)))
        worker.start()
        self.addCleanup(lambda: release.set())
        self.assertTrue(entered.wait(5))
        self.assertEqual(self.complete(result)["status"], "in_progress")
        self.assertEqual(len(self.method_calls("POST", "/sessions")), 1)
        release.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outputs[0]["status"], "adopted")

    def test_invalid_monotonic_clock_or_expired_flow_prevents_new_provider_request(self):
        for invalid in (-1, True, float("nan")):
            self.service.monotonic = lambda: invalid
            with self.assertRaises(BudgetConsentError):
                self.start()
            self.now += 3600
        self.service.monotonic = lambda: self.monotonic_now
        result, _ = self.stage()
        self.now += 901
        calls = len(self.provider.calls)
        failure = self.complete(result)
        self.assertEqual(failure["status"], "expired")
        self.assertEqual(len(self.provider.calls), calls)
