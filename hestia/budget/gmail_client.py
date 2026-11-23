"""Fenced, quota-bounded Google read-only transport for receipt imports."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import json
import threading
import time
from urllib.parse import quote, urlencode

from ..calendar_sync.providers import HTTPTransport, ProviderError
from .client import _reject_constant, _unique_error_fields

TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
MAX_QUOTA_UNITS_PER_RUN = 1000
_MESSAGE_COST = 5
_QUOTA_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "dailyLimitExceeded"}


class GmailError(Exception):
    """Only fixed labels and statuses leave the transport boundary."""

    def __init__(self, code, status=503, *, http_status=None, retry_after=None, attention=None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.http_status = http_status
        self.retry_after = retry_after
        self.attention = attention


class RunLimit(Exception):
    """Continue from the persisted checkpoint in the next round."""


@dataclass(frozen=True, repr=False)
class GmailCredentials:
    client_id: str
    client_secret: str
    refresh_token: str


@dataclass(frozen=True, repr=False)
class _AccessToken:
    value: str
    refresh_at: float


def _json_body(response):
    try:
        body = json.loads(response.body, object_pairs_hook=_unique_error_fields, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return None
    return body if isinstance(body, dict) else None


def _http_error(response):
    status = response.status
    error = (_json_body(response) or {}).get("error")
    if error == "invalid_grant":
        return GmailError("authorization", 403, http_status=status, attention="authorization")
    reasons = set()
    if isinstance(error, dict):
        for key in ("errors", "details"):
            nodes = error.get(key)
            if isinstance(nodes, list):
                reasons.update(item["reason"] for item in nodes if isinstance(item, dict)
                               and isinstance(item.get("reason"), str))
    if status == 429 or (status == 403 and reasons & _QUOTA_REASONS):
        return GmailError("daily_quota" if "dailyLimitExceeded" in reasons else "quota", 429,
                          http_status=status, retry_after=response.header("Retry-After"))
    if status in (401, 403):
        return GmailError("authorization", 403, http_status=status, attention="authorization")
    return GmailError("provider_http", http_status=status)


class GmailClient:
    def __init__(self, transport=None, *, monotonic=time.monotonic, sleep=time.sleep):
        self.transport = HTTPTransport() if transport is None else transport
        self.monotonic = monotonic
        self.sleep = sleep
        self._locks = {}
        self._locks_guard = threading.Lock()
        self._tokens = {}
        self._dispatched_at = {}

    def lock(self, owner):
        with self._locks_guard:
            return self._locks.setdefault(owner, threading.RLock())

    def forget(self, owner):
        with self.lock(owner):
            self._tokens.pop(owner, None)

    def run(self, owner, generation, credentials, fence):
        return GmailRun(self, owner, generation, credentials, fence)

    def _check(self, run):
        try:
            run.fence()
        except GmailError:
            self.forget(run.owner)
            raise

    def _dispatch(self, run, method, url, headers, *, body=None, cost=0):
        if run.units + cost > MAX_QUOTA_UNITS_PER_RUN:
            raise RunLimit()
        last = self._dispatched_at.get(run.owner)
        if last is not None:
            delay = last + 0.05 - self.monotonic()
            if delay > 0:
                self.sleep(delay)
        self._check(run)
        self._dispatched_at[run.owner] = self.monotonic()
        run.units += cost
        try:
            response = self.transport.request(method, url, headers=headers, body=body)
        except ProviderError:
            self._check(run)
            raise GmailError("network") from None
        self._check(run)
        return response

    def _token(self, run):
        cached = self._tokens.get(run.owner)
        if cached is not None and cached[0] == run.generation and self.monotonic() < cached[1].refresh_at:
            return cached[1].value
        self.forget(run.owner)
        credentials = run.credentials
        body = urlencode({"grant_type": "refresh_token", "client_id": credentials.client_id,
                          "client_secret": credentials.client_secret, "refresh_token": credentials.refresh_token})
        response = self._dispatch(run, "POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"},
                                  body=body.encode("ascii"))
        if response.status != 200:
            raise _http_error(response)
        data = _json_body(response) or {}
        token = data.get("access_token")
        expires = data.get("expires_in")
        if (not isinstance(token, str) or not 0 < len(token) <= 8192 or not token.isascii()
                or any(not 33 <= ord(char) <= 126 for char in token)
                or type(expires) is not int or not 0 < expires <= 86400):
            raise GmailError("invalid_token_response")
        self._tokens[run.owner] = (run.generation, _AccessToken(token, self.monotonic() + max(0, expires - 60)))
        return token

    def _get(self, run, path, cost, params):
        url = GMAIL_API + path
        if params:
            url += "?" + urlencode(params, doseq=True)
        with self.lock(run.owner):
            self._check(run)
            for _ in range(2):
                if run.units + cost > MAX_QUOTA_UNITS_PER_RUN:
                    raise RunLimit()
                token = self._token(run)
                response = self._dispatch(run, "GET", url, {"Authorization": "Bearer " + token}, cost=cost)
                if response.status != 401:
                    break
                self.forget(run.owner)
            if not 200 <= response.status < 300:
                error = _http_error(response)
                if error.attention == "authorization":
                    self.forget(run.owner)
                raise error
            data = _json_body(response)
            if data is None:
                raise GmailError("invalid_response")
            return data


@dataclass(repr=False)
class GmailRun:
    client: GmailClient
    owner: str
    generation: int
    credentials: GmailCredentials
    fence: Callable[[], None]
    units: int = field(default=0, init=False)

    def profile(self):
        return self.client._get(self, "/profile", 1, {})

    def labels(self):
        return self.client._get(self, "/labels", 1, {})

    def history(self, baseline, page_token, label_id):
        params = {"startHistoryId": baseline, "historyTypes": ("messageAdded", "labelAdded"), "maxResults": 100}
        if label_id is not None:
            params["labelId"] = label_id
        if page_token is not None:
            params["pageToken"] = page_token
        return self.client._get(self, "/history", 2, params)

    def messages(self, query, page_token):
        params = {"q": query, "maxResults": 100}
        if page_token is not None:
            params["pageToken"] = page_token
        return self.client._get(self, "/messages", 5, params)

    def require_message_budget(self):
        if self.units + 2 * _MESSAGE_COST > MAX_QUOTA_UNITS_PER_RUN:
            raise RunLimit()

    def message(self, message_id, *, raw=False):
        params = {"format": "raw", "fields": "id,raw"} if raw else {
            "format": "metadata", "metadataHeaders": ("From", "Sender", "Authentication-Results", "DKIM-Signature"),
            "fields": "id,internalDate,sizeEstimate,payload(headers)",
        }
        return self.client._get(self, "/messages/" + quote(message_id, safe=""), _MESSAGE_COST, params)
