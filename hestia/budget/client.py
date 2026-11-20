"""Bounded, read-only Enable Banking HTTP client with locally signed JWTs.

Only three account-information GET routes are permitted. There is no configurable
API origin, implicit redirect, retry, or synthetic PSU context. Call this blocking
client off the server's event loop; the runtime owns durable retries and backoff.
"""
from __future__ import annotations

import base64
import http.client
import json
import math
import re
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


API_ORIGIN = "https://api.enablebanking.com"
MAX_RESPONSE = 8 * 1024 * 1024
TIMEOUT = 30
TOKEN_TTL = 3600
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_UUID_PATTERN = re.compile(_UUID, re.ASCII)
_READ_PATH = re.compile(rf"(?:/sessions/{_UUID}|/accounts/{_UUID}/(?:balances|transactions))",
                        re.ASCII)
_QUERY_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}", re.ASCII)


class BudgetAPIError(Exception):
    """Safe diagnostics: no bank bodies, URLs, identifiers, or source exceptions."""

    CODES = frozenset({"invalid_configuration", "invalid_request", "authorization",
                       "rate_limit", "unavailable", "network", "invalid_response", "provider_error"})

    def __init__(self, status: int | None = None, code: str = "provider_error", retryable: bool = False):
        self.status = status if type(status) is int and 100 <= status <= 599 else None
        self.code = code if isinstance(code, str) and code in self.CODES else "provider_error"
        self.retryable = bool(retryable)
        suffix = f" (HTTP {self.status})" if self.status is not None else ""
        super().__init__("Budget API request failed: " + self.code + suffix)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_error(status: int) -> BudgetAPIError:
    if status in {401, 403}:
        return BudgetAPIError(status, "authorization")
    if status == 429:
        return BudgetAPIError(status, "rate_limit", True)
    if status >= 500 or status in {408, 425}:
        return BudgetAPIError(status, "unavailable", True)
    return BudgetAPIError(status, "provider_error")


def _b64(value: bytes) -> bytes:
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def _reject_constant(value):
    raise ValueError("Non-finite JSON value")


class EnableBankingClient:
    def __init__(self, application_id: str, key_pem: bytes, *, opener=None, clock=time.time):
        if not isinstance(application_id, str) or not _UUID_PATTERN.fullmatch(application_id):
            raise BudgetAPIError(code="invalid_configuration")
        try:
            key = serialization.load_pem_private_key(key_pem, password=None)
        except (ValueError, TypeError, UnsupportedAlgorithm):
            raise BudgetAPIError(code="invalid_configuration") from None
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            raise BudgetAPIError(code="invalid_configuration")
        self._application_id = str(uuid.UUID(application_id))
        self._key = key
        self._opener = opener if opener is not None else build_opener(ProxyHandler({}), _NoRedirect())
        self._clock = clock
        self._token = ""
        self._issued_at = self._expires_at = 0

    def _jwt(self) -> str:
        try:
            stamp = self._clock()
            if isinstance(stamp, bool) or not math.isfinite(stamp) or stamp < 0:
                raise ValueError
            now = int(stamp)
        except (ValueError, TypeError, OverflowError):
            raise BudgetAPIError(code="invalid_configuration") from None
        if now + 60 >= self._expires_at or now < self._issued_at:
            header = {"alg": "RS256", "typ": "JWT", "kid": self._application_id}
            claims = {"iss": "enablebanking.com", "aud": "api.enablebanking.com",
                      "iat": now, "exp": now + TOKEN_TTL}
            parts = [_b64(json.dumps(value, separators=(",", ":")).encode()) for value in (header, claims)]
            message = b".".join(parts)
            try:
                signature = self._key.sign(message, padding.PKCS1v15(), hashes.SHA256())
            except (ValueError, TypeError, UnsupportedAlgorithm):
                raise BudgetAPIError(code="invalid_configuration") from None
            self._token = (message + b"." + _b64(signature)).decode("ascii")
            self._issued_at, self._expires_at = now, now + TOKEN_TTL
        return self._token

    def get(self, path: str, query: dict | None = None) -> dict:
        if not isinstance(path, str) or not _READ_PATH.fullmatch(path):
            raise BudgetAPIError(code="invalid_request")
        if query is not None and (not isinstance(query, dict) or any(
            not isinstance(key, str) or not _QUERY_KEY.fullmatch(key) or type(value) not in {str, int}
            for key, value in query.items()
        )):
            raise BudgetAPIError(code="invalid_request")
        try:
            url = API_ORIGIN + path + ("?" + urlencode(query) if query else "")
        except (ValueError, TypeError, UnicodeError):
            raise BudgetAPIError(code="invalid_request") from None
        request = Request(url, method="GET", headers={"Authorization": "Bearer " + self._jwt(),
                          "Accept": "application/json", "User-Agent": "hestia-budget/1"})
        try:
            response = self._opener.open(request, timeout=TIMEOUT)
        except HTTPError as exc:
            status = exc.code
            try:
                exc.close()
            except (OSError, http.client.HTTPException):
                pass  # Cleanup failure must not expose a bank URL or response.
            raise _http_error(status) from None
        except (OSError, URLError, ValueError, http.client.HTTPException):
            raise BudgetAPIError(code="network", retryable=True) from None
        status = None
        try:
            with response:
                status = response.status
                if type(status) is not int or not 100 <= status <= 599:
                    raise BudgetAPIError(code="invalid_response")
                if not 200 <= status < 300:
                    raise _http_error(status)
                body = response.read(MAX_RESPONSE + 1)
                if not isinstance(body, bytes) or len(body) > MAX_RESPONSE:
                    raise BudgetAPIError(status, "invalid_response")
        except (OSError, URLError, ValueError, http.client.HTTPException):
            raise BudgetAPIError(status, "network", True) from None
        try:
            data = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
            if not isinstance(data, dict):
                raise ValueError
        except (ValueError, UnicodeError, RecursionError):
            raise BudgetAPIError(status, "invalid_response") from None
        return data
