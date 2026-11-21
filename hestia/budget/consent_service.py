"""Bounded interactive AIS authorization; local callbacks never contact the provider.

Provider schema: https://enablebanking.com/docs/api/reference/ . A known session
is retained before verification. Only verified, browser-bound candidates can
replace the local managed pointer, under the ordinary acquisition lock.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import math
import re
import secrets
import time
from urllib.parse import urlsplit

from sqlalchemy import text

from .client import BudgetAPIError
from .config import BudgetConfigError, _read_secret
from .consent_client import EnableBankingConsentClient, PSUContext
from .consent_control import BudgetConsentControlStore
from .consent_store import BudgetConsentError
from .normalize import BudgetDataError, _account_uids, _uid, _valid_until
from .runtime import BudgetBusyError, budget_lock
from .store import BudgetNotFound

_SECONDS = 120
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}\Z", re.ASCII)
_CALLBACK_ERROR = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z", re.ASCII)
_READY = ("created", "started", "staged", "exchanging", "candidate")


def _token_hash(value):
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise BudgetConsentError("budget_consent_invalid_request", 400)
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _code(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 4096
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)):
        raise BudgetConsentError("budget_consent_invalid_request", 400)
    try:
        if len(value.encode("utf-8")) > 4096:
            raise ValueError
    except (UnicodeError, ValueError):
        raise BudgetConsentError("budget_consent_invalid_request", 400) from None
    return value


def _url(value, *, authorization=False):
    try:
        if not isinstance(value, str) or not value.isascii() or len(value) > 4096:
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.port not in (None, 443)
                or parsed.username is not None or parsed.password is not None or parsed.fragment
                or any(char.isspace() or ord(char) < 32 or ord(char) == 127 or char == "\\" for char in value)
                or (authorization and parsed.hostname != "auth.enablebanking.com")
                or (not authorization and parsed.query)):
            raise ValueError
    except ValueError:
        raise BudgetConsentError("budget_consent_invalid_response" if authorization
                                 else "budget_consent_configuration", 503) from None
    return value


def _error(exc):
    if isinstance(exc, BudgetConfigError):
        return "configuration"
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and code in {"authorization", "network", "rate_limit", "invalid_response", "invalid_session",
                           "expired_session", "session_mismatch", "account_selection", "unavailable",
                           "acquisition_timeout", "deferred"} else "internal_error"


class BudgetConsentService:
    def __init__(self, config, store, files, *, path, redirect_url,
                 client_factory=EnableBankingConsentClient, clock=time.time,
                 monotonic=time.monotonic, sleeper=time.sleep, control=None):
        self.config, self.store, self.files = config, store, files
        self.path, self.redirect_url = path, _url(redirect_url)
        self.client_factory, self.clock = client_factory, clock
        self.monotonic, self.sleeper = monotonic, sleeper
        self.control = BudgetConsentControlStore(store.sessions, clock=clock) if control is None else control

    def _configured(self, flow):
        for item in self.config.connections:
            if item.id == flow["connection_id"]:
                if (item.owner, self.config.application_id, item.bank_name, item.country) != (
                        flow["owner"], flow["application_id"], flow["bank_name"], flow["country"]):
                    raise BudgetConsentError("budget_consent_configuration", 409)
                return item
        raise BudgetNotFound()

    def _guard(self, flow, nonce):
        """Fresh authority, binding and generation checks; finish SQLite work before any I/O."""
        nonce_hash = _token_hash(nonce)
        with self.store.sessions() as session:
            session.execute(text("BEGIN"))
            row = self.store._flow(session, flow["owner"], flow["id"])
            self.store._binding(row, flow["auth_fingerprint"], nonce_hash)
            if row.status not in _READY or row.binding_revision != 1 or row.expires_at <= self.clock():
                raise BudgetConsentError()
            connection = self.store._owned_connection(session, row.owner, row.connection_id)
            self.store._fence(row, connection)
            current = self.store._internal(row)
        self._configured(current)
        return current

    def _deadline(self):
        now = self.monotonic()
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise BudgetConsentError("budget_consent_configuration", 503)
        return now + _SECONDS

    def _remaining(self, deadline):
        now = self.monotonic()
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0 or now >= deadline:
            raise BudgetAPIError(code="acquisition_timeout")
        return deadline - now

    def _client(self, flow, nonce):
        self._guard(flow, nonce)
        key = _read_secret(self.config.key_file)
        return self.client_factory(self.config.application_id, key, clock=self.clock, monotonic=self.monotonic)

    def _request(self, client, flow, nonce, connection, method, path, *, deadline,
                 body=None, query=None, psu=None, before_send=None):
        while True:
            self._guard(flow, nonce)
            remaining = self._remaining(deadline)
            delay = self.control.control_wait(flow["owner"], connection, application_id=self.config.application_id)
            if delay == 0:
                break
            if delay >= remaining:
                raise BudgetAPIError(code="rate_limit")
            self.sleeper(delay)
        self._guard(flow, nonce)
        self._remaining(deadline)
        if before_send is not None:
            before_send()
        try:
            if method == "POST":
                return client.post(path, body, deadline=deadline, psu=psu)
            return client.get(path, query=query, deadline=deadline, psu=psu)
        except BudgetAPIError as exc:
            if exc.status == 429:
                self.control.record_control_rate_limit(connection, exc, application_id=self.config.application_id)
            raise

    def start(self, owner, connection_id, *, intent, expected_version, auth_fingerprint, psu=None):
        nonce, state = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        flow = self.store.create_flow(owner, connection_id, intent=intent, expected_version=expected_version,
                    state_hash=_token_hash(state), nonce_hash=_token_hash(nonce), auth_fingerprint=auth_fingerprint)
        try:
            connection = self._configured(flow)
            client, deadline = self._client(flow, nonce), self._deadline()
            response = self._request(client, flow, nonce, connection, "GET", "/aspsps", deadline=deadline,
                                     query={"country": connection.country, "psu_type": "personal", "service": "AIS"})
            bank = self._bank(response, connection, psu)
            # Use the advertised bank maximum, reduced by a small clock margin.
            until = datetime.fromtimestamp(self.clock() + bank["maximum_consent_validity"] - 30,
                                           timezone.utc).isoformat(timespec="seconds")
            response = self._request(client, flow, nonce, connection, "POST", "/auth", deadline=deadline, psu=psu,
                        body={"access": {"valid_until": until, "balances": True, "transactions": True,
                        "transactions_extended_history": True}, "aspsp": {"name": connection.bank_name,
                        "country": connection.country}, "psu_type": "personal", "state": state,
                        "redirect_url": self.redirect_url})
            authorization_url = _url(response.get("url") if isinstance(response, dict) else None, authorization=True)
            self._remaining(deadline)
            self._guard(flow, nonce)
            self.store.mark_started(owner, flow["id"])
            return {"flow_id": flow["id"], "authorization_url": authorization_url,
                    "expires_at": flow["expires_at"], "_nonce": nonce}
        except Exception as exc:
            self.store.fail_flow(owner, flow["id"], _error(exc))
            if isinstance(exc, BudgetConsentError):
                raise
            raise BudgetConsentError("budget_consent_unavailable", 503) from None

    @staticmethod
    def _bank(response, connection, psu):
        banks = response.get("aspsps") if isinstance(response, dict) else None
        if not isinstance(banks, list):
            raise BudgetAPIError(code="invalid_response")
        matches = [bank for bank in banks if isinstance(bank, dict) and bank.get("name") == connection.bank_name
                   and bank.get("country") == connection.country]
        if len(matches) != 1:
            raise BudgetAPIError(code="invalid_response")
        bank = matches[0]
        maximum = bank.get("maximum_consent_validity")
        if (not isinstance(bank.get("psu_types"), list) or "personal" not in bank["psu_types"]
                or type(maximum) is not int or maximum < 60):
            raise BudgetAPIError(code="invalid_response")
        required = bank.get("required_psu_headers", [])
        headers = {key.lower() for key in psu.headers()} if type(psu) is PSUContext else set()
        if not isinstance(required, list) or any(not isinstance(key, str) or key.lower() not in headers for key in required):
            raise BudgetConsentError("budget_consent_psu_context_required", 409)
        return bank

    def flow_for_state(self, state):
        return self.store.flow_for_state(_token_hash(state))

    def callback(self, state, nonce, *, code=None, error=None):
        state_hash, nonce_hash = _token_hash(state), _token_hash(nonce)
        if (code is None) == (error is None):
            raise BudgetConsentError("budget_consent_invalid_request", 400)
        if error is not None and (not isinstance(error, str) or not _CALLBACK_ERROR.fullmatch(error)):
            raise BudgetConsentError("budget_consent_invalid_request", 400)
        flow = self.store.flow_for_state(state_hash)
        if not hmac.compare_digest(flow["nonce_hash"], nonce_hash):
            raise BudgetConsentError("budget_consent_binding_invalid", 403)
        self._guard(flow, nonce)
        if flow["status"] not in ("created", "started"):
            raise BudgetConsentError()
        ref = self.files.put("code", {"code": _code(code)}) if code is not None else None
        try:
            result = self.store.callback(state_hash, nonce_hash, code_ref=ref,
                                         error="authorization" if error is not None else None)
        except Exception:
            if ref is not None:
                self._discard(ref)
            raise
        return {key: result[key] for key in ("id", "connection_id", "status", "error", "expires_at")}

    def _discard(self, ref):
        try:
            self.files.delete(ref)
        except Exception:
            pass  # A private orphan cannot grant another exchange claim; never hide a known session.

    @staticmethod
    def _bindings(bindings):
        return {flow_id: _token_hash(nonce) for flow_id, nonce in bindings.items()
                if isinstance(nonce, str) and _TOKEN.fullmatch(nonce)}

    def pending(self, owner, auth_fingerprint, bindings):
        return self.store.pending(owner, auth_fingerprint, self._bindings(bindings))

    def cancel(self, owner, flow_id, auth_fingerprint, nonce):
        result = self.store.cancel(owner, flow_id, auth_fingerprint, _token_hash(nonce))
        return {"flow_id": result["id"], "status": result["status"]}

    def invalidate_bindings(self, auth_fingerprint, bindings):
        return self.store.invalidate_bindings(auth_fingerprint, self._bindings(bindings))

    def status(self, owner):
        return self.store.managed(owner)

    def _verify(self, response, connection, selected):
        if not isinstance(response, dict) or response.get("status") != "AUTHORIZED":
            raise BudgetDataError("invalid_session")
        bank = response.get("aspsp")
        if (not isinstance(bank, dict) or bank.get("name") != connection.bank_name
                or bank.get("country") != connection.country or response.get("psu_type") != "personal"):
            raise BudgetDataError("session_mismatch")
        access = response.get("access")
        expiry = _valid_until(access.get("valid_until") if isinstance(access, dict) else None, self.clock())
        hashes = _account_uids(response, selected)
        accounts = response.get("accounts")
        if accounts is not None and (not isinstance(accounts, list)
                or len(accounts) != len(hashes) or {_uid(uid, "account_selection") for uid in accounts} != set(hashes.values())):
            raise BudgetDataError("account_selection")
        if not selected <= set(connection.accounts):
            raise BudgetDataError("account_selection")
        return expiry, {"account_hashes": sorted(hashes)}

    def _result(self, flow, status, candidate=None, error=None):
        result = {"flow_id": flow["id"], "status": status}
        if candidate is not None:
            result["consent"] = {key: candidate[key] for key in ("id", "connection_id", "status", "provenance",
                                                                 "valid_until", "generation", "created_at")}
        if error is not None:
            result["error"] = error
        return result

    def complete(self, owner, flow_id, *, auth_fingerprint, nonce, psu=None):
        flow = self.store.flow_for_browser(owner, flow_id, auth_fingerprint, _token_hash(nonce))
        candidate = self.store.candidate_for_flow(owner, flow_id)
        if flow["status"] == "adopted":
            return self._result(flow, "adopted", candidate)
        if flow["status"] == "exchanging":
            return self._result(flow, "in_progress", candidate)
        if flow["status"] not in ("staged", "candidate"):
            return self._result(flow, flow["status"], candidate, flow["error"])
        connection, deadline = self._configured(flow), self._deadline()
        attempted = False
        try:
            self._guard(flow, nonce)
            if candidate is None:
                client = self._client(flow, nonce)
                code = self.files.get(flow["code_ref"], "code")

                def claim():
                    nonlocal attempted
                    self.store.claim_exchange(owner, flow_id, auth_fingerprint, _token_hash(nonce))
                    attempted = True

                try:
                    response = self._request(client, flow, nonce, connection, "POST", "/sessions", deadline=deadline,
                                             body=code, psu=psu, before_send=claim)
                except Exception:
                    if attempted:
                        self._discard(flow["code_ref"])
                    raise
                self._discard(flow["code_ref"])
                session_id = _uid(response.get("session_id") if isinstance(response, dict) else None, "invalid_session")
                ref = self.files.put("session", {"session_id": session_id})
                candidate = self.store.record_candidate(owner, flow_id, capsule_ref=ref, valid_until=None,
                                                         metadata={"account_hashes": []})
            if candidate["valid_until"] is None:
                self._guard(flow, nonce)
                session = self.files.get(candidate["capsule_ref"], "session")
                client = self._client(flow, nonce)
                response = self._request(client, flow, nonce, connection, "GET", "/sessions/" + session["session_id"],
                                         deadline=deadline, psu=psu)
                self._guard(flow, nonce)
                self._remaining(deadline)
                selected = self.store.consent_selected_hashes(owner, flow["connection_id"])
                expiry, metadata = self._verify(response, connection, selected)
                candidate = self.store.record_candidate(owner, flow_id, capsule_ref=candidate["capsule_ref"],
                                                         valid_until=expiry, metadata=metadata)
            self._remaining(deadline)
            with budget_lock(self.path):
                self._guard(flow, nonce)
                selected = self.store.consent_selected_hashes(owner, flow["connection_id"])
                if not selected <= set(connection.accounts):
                    raise BudgetDataError("account_selection")
                adopted = self.store.adopt(owner, flow_id, auth_fingerprint=auth_fingerprint,
                                            nonce_hash=_token_hash(nonce), selected_hashes=selected)
            return self._result(flow, "adopted", adopted)
        except Exception as exc:
            if candidate is not None:
                return self._result(flow, "reconciliation_required", candidate,
                                    "busy" if isinstance(exc, BudgetBusyError) else _error(exc))
            if attempted:
                failed = self.store.fail_flow(owner, flow_id, "exchange_uncertain")
                return self._result(flow, failed["status"], error=failed["error"])
            return self._result(flow, "reconciliation_required", error=_error(exc))
