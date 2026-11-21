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
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .rate_limit import RateScope, RetryHint, parse_retry_after, rate_scope as classify_rate_scope


API_ORIGIN = "https://api.enablebanking.com"
MAX_RESPONSE = 8 * 1024 * 1024
MAX_ERROR_RESPONSE = 64 * 1024
READ_CHUNK = 64 * 1024
TIMEOUT = 60
DIAGNOSTIC_SECONDS = 5
TOKEN_TTL = 3600
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_UUID_PATTERN = re.compile(_UUID, re.ASCII)
_READ_PATH = re.compile(rf"(?:/sessions/{_UUID}|/accounts/{_UUID}/(?:balances|transactions))",
                        re.ASCII)
_QUERY_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}", re.ASCII)


# Enable Banking ErrorResponse.error identifiers, verbatim from the ErrorCode schema:
# https://enablebanking.com/docs/api/reference/#errorcode
# The documentation does not promise the list is closed, so any other string is kept
# only as UNLISTED_PROVIDER_ERROR, never as received.
PROVIDER_ERRORS = frozenset({
    "ACCESS_DENIED", "ACCOUNT_DOES_NOT_EXIST", "ALREADY_AUTHORIZED", "ASPSP_ACCOUNT_NOT_ACCESSIBLE",
    "ASPSP_ERROR", "ASPSP_PAYMENT_NOT_ACCESSIBLE", "ASPSP_PSU_ACTION_REQUIRED", "ASPSP_RATE_LIMIT_EXCEEDED",
    "ASPSP_TIMEOUT", "AUTHORIZATION_NOT_PROVIDED", "CLOSED_SESSION", "DATE_FROM_IN_FUTURE",
    "DATE_TO_WITHOUT_DATE_FROM", "EXPIRED_AUTHORIZATION_CODE", "EXPIRED_SESSION", "INVALID_ACCOUNT_ID",
    "INVALID_HOST", "INVALID_PAYMENT", "NO_ACCOUNTS_ADDED", "PAYMENT_LIMIT_EXCEEDED", "PAYMENT_NOT_AUTHORIZED",
    "PAYMENT_NOT_FINALIZED", "PAYMENT_NOT_FOUND", "PAYMENT_SUBMISSION_NOT_DEFERRED",
    "PAYMENT_SUBMISSION_NOT_SUPPORTED", "PSU_HEADER_INVALID", "PSU_HEADER_NOT_PROVIDED", "REDIRECT_URI_NOT_ALLOWED",
    "REVOKED_SESSION", "SESSION_DOES_NOT_EXIST", "TRANSACTION_DOES_NOT_EXIST", "UNAUTHORIZED_ACCESS",
    "UNAUTHORIZED_IP", "UNTRUSTED_PAYMENT_PARTY", "WEBHOOK_URI_NOT_ALLOWED", "WRONG_ASPSP_PROVIDED",
    "WRONG_AUTHORIZATION_CODE", "WRONG_CONTINUATION_KEY", "WRONG_CREDENTIALS_PROVIDED", "WRONG_DATE_INTERVAL",
    "WRONG_REQUEST_PARAMETERS", "WRONG_SESSION_STATUS", "WRONG_TRANSACTIONS_PERIOD",
})
UNLISTED_PROVIDER_ERROR = "UNLISTED"


def safe_provider_error(value, *, any_string=False):
    """A documented identifier, UNLISTED for another string from the wire, otherwise None."""
    if not isinstance(value, str):
        return None
    if value in PROVIDER_ERRORS or value == UNLISTED_PROVIDER_ERROR:
        return value
    return UNLISTED_PROVIDER_ERROR if any_string else None


def safe_http_status(value):
    return value if type(value) is int and 100 <= value <= 599 else None


@dataclass(frozen=True)
class ProviderDiagnostic:
    """The last failed HTTP exchange of one acquisition: a status and a fixed identifier, never text."""

    http_status: int | None = None
    provider_error: str | None = None

    @classmethod
    def of(cls, error):
        """A diagnostic only when the failure really was an HTTP response; otherwise None."""
        if not isinstance(error, BudgetAPIError) or error.status is None or 200 <= error.status < 300:
            return None
        return cls(error.status, error.provider_error)


class BudgetAPIError(Exception):
    """Safe diagnostics: no bank bodies, URLs, identifiers, or source exceptions."""

    CODES = frozenset({"invalid_configuration", "invalid_request", "authorization",
                       "rate_limit", "unavailable", "network", "invalid_response", "provider_error",
                       "acquisition_timeout"})
    PROVIDER_CODES = frozenset({"ASPSP_RATE_LIMIT_EXCEEDED"})

    def __init__(self, status: int | None = None, code: str = "provider_error", retryable: bool = False, *,
                 provider_code: str | None = None, retry_at: float | None = None, retry_suspended: bool = False,
                 rate_scope: RateScope = RateScope.CONNECTION, provider_error: str | None = None):
        self.status = safe_http_status(status)
        self.code = code if isinstance(code, str) and code in self.CODES else "provider_error"
        self.retryable = bool(retryable)
        self.provider_code = (provider_code if isinstance(provider_code, str) and provider_code in self.PROVIDER_CODES
                              else None)
        # Diagnostic only: never drives classification, retries or rate scope.
        self.provider_error = safe_provider_error(provider_error)
        self.retry_at = _safe_deadline(retry_at)
        self.retry_suspended = bool(retry_suspended)
        self.rate_scope = (RateScope.ACCOUNT_ENDPOINT if rate_scope == RateScope.ACCOUNT_ENDPOINT
                           else RateScope.CONNECTION)
        suffix = f" (HTTP {self.status})" if self.status is not None else ""
        super().__init__("Budget API request failed: " + self.code + suffix)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _safe_deadline(value):
    try:
        if type(value) in (int, float) and math.isfinite(value) and value >= 0:
            return float(value)
    except OverflowError:
        pass
    return None


def _unique_error_fields(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError
        data[key] = value
    return data


def _provider_error(response, read_body):
    # ErrorResponse.error is documented; message/detail are deliberately ignored.
    # https://enablebanking.com/docs/api/reference/#errorresponse
    try:
        body = read_body(response, MAX_ERROR_RESPONSE)
        if not isinstance(body, bytes) or len(body) > MAX_ERROR_RESPONSE:
            return None
        data = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_error_fields,
                          parse_constant=_reject_constant)
        return safe_provider_error(data.get("error"), any_string=True) if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException, RecursionError):
        return None


def _diagnostic_error(response, diagnostic_body):
    # A failed or slow diagnostic read must never change how the HTTP error is classified.
    if diagnostic_body is None:
        return None
    try:
        return _provider_error(response, diagnostic_body)
    except BudgetAPIError:
        return None


def _retry_hint(response, clock):
    headers = getattr(response, "headers", None)
    if headers is None:
        return RetryHint(None, False), False
    try:
        values = (headers.get_all("Retry-After") if hasattr(headers, "get_all")
                  else [headers.get("Retry-After")])
    except (AttributeError, TypeError, ValueError, OSError, http.client.HTTPException):
        return RetryHint(None, True), False
    try:
        now = clock()
        hints = [parse_retry_after(value, now=now) for value in values or [None]]
    except (TypeError, ValueError, OverflowError):
        return RetryHint(None, True), True
    deadline = max((hint.deadline for hint in hints if hint.deadline is not None), default=None)
    return RetryHint(deadline, any(hint.suspended for hint in hints)), False


def _http_error(status: int, *, response, path, clock, read_body, diagnostic_body=None) -> BudgetAPIError:
    if status == 429:
        hint, unsafe_clock = _retry_hint(response, clock)
        try:
            provider_error = _provider_error(response, read_body)
        except BudgetAPIError as exc:
            # Keep an already received server deadline if the body arrives late.
            raise BudgetAPIError(status, exc.code, exc.retryable, retry_at=hint.deadline,
                                 retry_suspended=hint.suspended) from None
        provider_code = provider_error if provider_error in BudgetAPIError.PROVIDER_CODES else None
        scope = RateScope.CONNECTION if unsafe_clock else classify_rate_scope(path, provider_code)
        return BudgetAPIError(status, "rate_limit", True, provider_code=provider_code, retry_at=hint.deadline,
                              retry_suspended=hint.suspended, rate_scope=scope, provider_error=provider_error)
    provider_error = _diagnostic_error(response, diagnostic_body)
    if status in {401, 403}:
        return BudgetAPIError(status, "authorization", provider_error=provider_error)
    if status >= 500 or status in {408, 425}:
        return BudgetAPIError(status, "unavailable", True, provider_error=provider_error)
    return BudgetAPIError(status, "provider_error", provider_error=provider_error)


def _b64(value: bytes) -> bytes:
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def _reject_constant(value):
    raise ValueError("Non-finite JSON value")


def _response_socket(response):
    # urllib HTTPResponse -> fp/raw/_sock; HTTPError adds one fp wrapper.
    # Injected in-memory responses need no socket. Bound traversal even for an
    # invalid/cyclic wrapper, and never inspect or expose its URL or headers.
    node = response
    for _ in range(5):
        if callable(getattr(node, "settimeout", None)):
            return node
        following = getattr(node, "fp", None)
        if following is None:
            following = getattr(node, "raw", None)
        if following is None:
            following = getattr(node, "_sock", None)
        if following is None:
            return None
        node = following
    raise BudgetAPIError(code="invalid_response")


def _shutdown_socket(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class _DeadlineReader:
    def __init__(self, client, response, deadline):
        self.client, self.deadline = client, deadline
        self.response = response
        self.socket = _response_socket(response)
        reader = getattr(response, "read1", None)
        self.reader = reader if callable(reader) else response.read

    def read1(self, size):
        # Content-Length completion closes HTTPResponse.fp and its socket before
        # the base reader's final EOF probe. Never reuse that closed socket.
        self.socket = _response_socket(self.response)
        if self.socket is not None:
            try:
                self.socket.settimeout(self.client._remaining(self.deadline))
            except (OSError, ValueError, TypeError):
                raise BudgetAPIError(code="network", retryable=True) from None
        return self.reader(size)


class EnableBankingClient:
    def __init__(self, application_id: str, key_pem: bytes, *, opener=None, clock=time.time,
                 monotonic=time.monotonic):
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
        self._monotonic = monotonic
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

    def _remaining(self, deadline):
        if deadline is None:
            return TIMEOUT
        if _safe_deadline(deadline) is None:
            raise BudgetAPIError(code="invalid_request") from None
        try:
            now = self._monotonic()
        except (TypeError, ValueError, OverflowError):
            raise BudgetAPIError(code="invalid_configuration") from None
        if _safe_deadline(now) is None:
            raise BudgetAPIError(code="invalid_configuration") from None
        remaining = deadline - now
        if remaining <= 0:
            raise BudgetAPIError(code="acquisition_timeout", retryable=True) from None
        return min(TIMEOUT, remaining)

    # Persisted acquisition diagnostics; the consent control plane keeps its own error handling.
    _provider_diagnostics = True

    def _diagnostic_reader(self, deadline):
        if not self._provider_diagnostics:
            return None

        def read(response, maximum):
            # The bound starts when the read does and caps every blocking socket read, not only the gaps.
            bound = self._diagnostic_deadline(deadline)
            reader = _DeadlineReader(self, response, bound)
            watchdog = None
            if reader.socket is not None:
                watchdog = threading.Timer(self._remaining(bound), _shutdown_socket, args=(reader.socket,))
                watchdog.daemon = True
                watchdog.start()
            try:
                return self._read_body(reader, maximum, bound)
            finally:
                if watchdog is not None:
                    watchdog.cancel()
        return read

    def _diagnostic_deadline(self, deadline):
        # Reading a non-429 error body is diagnostic only, so it gets a short bound of its own.
        try:
            bound = self._monotonic() + DIAGNOSTIC_SECONDS
        except (TypeError, ValueError, OverflowError):
            return 0.0  # Unusable clock: the read is abandoned and the diagnostic stays empty.
        if _safe_deadline(bound) is None:
            return 0.0
        return bound if _safe_deadline(deadline) is None else min(deadline, bound)

    def _read_body(self, response, maximum, deadline):
        reader = getattr(response, "read1", None)
        reader = reader if callable(reader) else response.read
        body = bytearray()
        while len(body) <= maximum:
            self._remaining(deadline)
            try:
                chunk = reader(min(READ_CHUNK, maximum + 1 - len(body)))
            except (OSError, ValueError, http.client.HTTPException):
                self._remaining(deadline)
                raise
            self._remaining(deadline)
            if not isinstance(chunk, bytes):
                return None
            body.extend(chunk)
            if not chunk:
                break
        return bytes(body)

    def _response_body(self, response, path, deadline):
        status, http_error = None, None
        try:
            with response:
                status = response.status
                if status != 429:
                    self._remaining(deadline)
                if type(status) is not int or not 100 <= status <= 599:
                    raise BudgetAPIError(code="invalid_response")
                if not 200 <= status < 300:
                    try:
                        http_error = _http_error(
                            status, response=response, path=path, clock=self._clock,
                            read_body=lambda response, maximum: self._read_body(response, maximum, deadline),
                            diagnostic_body=self._diagnostic_reader(deadline))
                    except BudgetAPIError as exc:
                        http_error = exc
                    raise http_error
                body = self._read_body(response, MAX_RESPONSE, deadline)
                if not isinstance(body, bytes) or len(body) > MAX_RESPONSE:
                    raise BudgetAPIError(status, "invalid_response")
        except (OSError, URLError, ValueError, http.client.HTTPException):
            if http_error is not None:
                raise http_error from None
            self._remaining(deadline)
            raise BudgetAPIError(status, "network", True) from None
        return body, status

    def get(self, path: str, query: dict | None = None, *, deadline=None) -> dict:
        if not isinstance(path, str) or not _READ_PATH.fullmatch(path):
            raise BudgetAPIError(code="invalid_request")
        if query is not None and (not isinstance(query, dict) or any(
            not isinstance(key, str) or not _QUERY_KEY.fullmatch(key) or type(value) not in {str, int}
            for key, value in query.items()
        )):
            raise BudgetAPIError(code="invalid_request")
        self._remaining(deadline)
        try:
            url = API_ORIGIN + path + ("?" + urlencode(query) if query else "")
        except (ValueError, TypeError, UnicodeError):
            raise BudgetAPIError(code="invalid_request") from None
        request = Request(url, method="GET", headers={"Authorization": "Bearer " + self._jwt(),
                          "Accept": "application/json", "User-Agent": "hestia-budget/1"})
        try:
            response = self._opener.open(request, timeout=self._remaining(deadline))
        except HTTPError as exc:
            status = exc.code
            try:
                if status != 429:
                    self._remaining(deadline)
                error = _http_error(status, response=exc, path=path, clock=self._clock,
                                    read_body=lambda response, maximum: self._read_body(response, maximum, deadline),
                                    diagnostic_body=self._diagnostic_reader(deadline))
            finally:
                try:
                    exc.close()
                except (OSError, http.client.HTTPException):
                    pass  # Cleanup failure must not expose a bank URL or response.
            raise error from None
        except (OSError, URLError, ValueError, http.client.HTTPException):
            self._remaining(deadline)
            raise BudgetAPIError(code="network", retryable=True) from None
        body, status = self._response_body(response, path, deadline)
        try:
            data = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
            if not isinstance(data, dict):
                raise ValueError
        except (ValueError, UnicodeError, RecursionError):
            raise BudgetAPIError(status, "invalid_response") from None
        self._remaining(deadline)
        return data
