"""Read-only CMP ``/GetScheduler`` HTTP client with injected transport, cap, and backoff.

No live network calls occur in tests: callers inject a ``Transport`` implementation.
Exceptions emit only fixed safe error codes and never include response bodies, URLs,
or credentials. Wire payload fields reflect Slice 1 assumptions in ``docs/APPOINTMENTS.md``.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, timezone
from email.utils import parsedate_to_datetime
import http.client
import json
import math
import random
import time
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import CMP_BASE_URL, CmpCredentials, PollPolicy, WatchCriteria, _check_https_url
from .parser import CmpParseError, ParseResult, parse_cmp_scheduler_bytes

SCHEDULER_PATH = "/GetScheduler"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 15.0
USER_AGENT = "Hestia-AppointmentsMonitor/1.0"
_MAX_RETRY_AFTER_CHARS = 128


class TransportError(Exception):
    """Fixed safe transport failure (timeout, DNS/TLS/socket, or response size limit)."""


class CmpClientError(Exception):
    """Fixed safe diagnostic leaving the CMP client boundary."""

    CODES = frozenset({
        "network",
        "rate_limit",
        "provider_http",
        "authorization",
        "invalid_response",
        "run_limit",
    })

    def __init__(
        self,
        code: str,
        *,
        http_status: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
        attention: str | None = None,
    ) -> None:
        self.code = code if code in self.CODES else "provider_http"
        self.http_status = http_status
        self.retryable = retryable
        self.retry_after = retry_after
        self.attention = attention
        suffix = f" (HTTP {http_status})" if http_status is not None else ""
        super().__init__(f"CMP API error: {self.code}{suffix}")


class CmpRunLimitError(CmpClientError):
    """Raised when a poll run reaches its per-run request cap."""

    def __init__(
        self,
        *,
        attempted: bool = False,
        cause_code: str = "run_limit",
        http_status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        self.attempted = attempted
        self.cause_code = cause_code
        super().__init__(
            "run_limit",
            http_status=http_status,
            retryable=False,
            retry_after=retry_after,
        )


@dataclass(frozen=True)
class HTTPResponse:
    """Minimal HTTP response returned by an injected ``Transport``."""

    status: int
    headers: Mapping[str, str]
    body: bytes = b""

    def header(self, name: str) -> str | None:
        target = name.lower()
        for key, value in self.headers.items():
            if key.lower() == target:
                return value
        return None


class Transport(Protocol):
    """Injectable HTTP transport contract for CMP requests."""

    request: Callable[..., HTTPResponse]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibTransport:
    """Blocking HTTPS-only stdlib transport with redirects disabled and bounded reads."""

    def __init__(
        self,
        *,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        opener_factory: Callable[..., object] = build_opener,
    ) -> None:
        self.max_response_bytes = max_response_bytes
        self._opener_factory = opener_factory

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> HTTPResponse:
        if urlsplit(url).scheme != "https":
            raise TransportError("CMP transport requires HTTPS")
        req = Request(url, data=body, headers=dict(headers), method=method)
        try:
            response = self._opener_factory(_NoRedirect()).open(req, timeout=timeout)
        except HTTPError as exc:
            response = exc
        except (OSError, URLError, ValueError, http.client.HTTPException):
            raise TransportError("CMP network request failed") from None
        try:
            with response:
                data = response.read(self.max_response_bytes + 1)
                if len(data) > self.max_response_bytes:
                    raise TransportError("CMP response exceeds safe size")
                return HTTPResponse(response.code, dict(response.headers.items()), data)
        except (OSError, URLError, http.client.HTTPException):
            raise TransportError("CMP network response interrupted") from None


@dataclass(frozen=True)
class CmpSchedulerQuery:
    """Typed CMP ``POST /GetScheduler`` request (synthetic wire-field assumptions)."""

    date_from: date
    date_to: date
    specialty_id: str | None = None
    doctor_ids: tuple[str, ...] = ()
    location_ids: tuple[str, ...] = ()
    service_id: str | None = None

    @classmethod
    def from_criteria(cls, criteria: WatchCriteria) -> CmpSchedulerQuery:
        return cls(
            date_from=criteria.date_from,
            date_to=criteria.date_to,
            specialty_id=criteria.specialty_id,
            doctor_ids=criteria.doctor_ids,
            location_ids=criteria.location_ids,
            service_id=criteria.service_id,
        )

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "dateFrom": self.date_from.isoformat(),
            "dateTo": self.date_to.isoformat(),
        }
        if self.specialty_id is not None:
            payload["specialtyId"] = self.specialty_id
        if self.doctor_ids:
            payload["doctorIds"] = list(self.doctor_ids)
        if self.location_ids:
            payload["locationIds"] = list(self.location_ids)
        if self.service_id is not None:
            payload["serviceId"] = self.service_id
        return payload


@dataclass
class CmpRun:
    """Per-run request budget tracker shared across watches in a single poll cycle."""

    max_requests: int
    requests_made: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.max_requests - self.requests_made)


def parse_retry_after(raw: str | None, *, now: float) -> float | None:
    """Parse an RFC 9110 ``Retry-After`` delta-seconds or HTTP-date into non-negative seconds."""
    if not isinstance(raw, str):
        return None
    text = raw.strip(" \t")
    if not text or len(text) > _MAX_RETRY_AFTER_CHARS:
        return None
    if text.isascii() and text.isdigit():
        return float(int(text))
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        delta = parsed.timestamp() - now
    except (TypeError, ValueError, OverflowError):
        return None
    return delta if delta >= 0 else None


class CmpClient:
    """Read-only CMP availability client with injected transport, request cap, and backoff."""

    def __init__(
        self,
        *,
        transport: Transport | None = None,
        base_url: str = CMP_BASE_URL,
        credentials: CmpCredentials = CmpCredentials(),
        policy: PollPolicy = PollPolicy(),
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if (type(timeout) is int and 0 < timeout <= 120) or (
            type(timeout) is float and math.isfinite(timeout) and 0 < timeout <= 120
        ):
            self.timeout = float(timeout)
        else:
            raise ValueError("Invalid CMP request timeout")
        self.base_url = _check_https_url(base_url)
        self.transport: Transport = UrllibTransport() if transport is None else transport
        self.credentials = credentials
        self.policy = policy
        self.monotonic = monotonic
        self.wall_clock = wall_clock
        self.sleep = sleep
        self.jitter = jitter
        self._last_dispatch: float | None = None

    def new_run(self) -> CmpRun:
        """Create a fresh per-run request budget bounded by ``policy.max_requests_per_run``."""
        return CmpRun(max_requests=self.policy.max_requests_per_run)

    def _backoff_delay(self, attempt: int) -> float:
        exp = min(self.policy.max_backoff_seconds, self.policy.base_backoff_seconds * (2 ** attempt))
        factor = 0.5 + 0.5 * max(0.0, min(1.0, float(self.jitter())))
        return min(self.policy.max_backoff_seconds, exp * factor)

    def _wait_spacing(self) -> None:
        if self._last_dispatch is not None and self.policy.min_request_spacing_seconds > 0:
            delay = self._last_dispatch + self.policy.min_request_spacing_seconds - self.monotonic()
            if delay > 0:
                self.sleep(delay)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        if self.credentials.auth_mode == "bearer":
            headers["Authorization"] = f"Bearer {self.credentials.bearer_token}"
        return headers

    def _handle_retryable_status(
        self,
        response: HTTPResponse,
        *,
        attempt: int,
        active_run: CmpRun,
    ) -> float:
        code = "rate_limit" if response.status == 429 else "provider_http"
        retry_after = parse_retry_after(response.header("Retry-After"), now=self.wall_clock())
        delay = self._backoff_delay(attempt)
        if retry_after is not None:
            if retry_after > self.policy.max_backoff_seconds:
                raise CmpClientError(
                    code,
                    http_status=response.status,
                    retryable=True,
                    retry_after=retry_after,
                )
            delay = max(delay, retry_after)
        if attempt >= self.policy.max_retries:
            raise CmpClientError(
                code,
                http_status=response.status,
                retryable=True,
                retry_after=retry_after,
            )
        if active_run.requests_made >= active_run.max_requests:
            raise CmpRunLimitError(
                attempted=True,
                cause_code=code,
                http_status=response.status,
                retry_after=retry_after,
            )
        return delay

    def get_scheduler(
        self,
        query: CmpSchedulerQuery,
        *,
        run: CmpRun | None = None,
    ) -> ParseResult:
        """Query ``POST /GetScheduler`` and return parsed normalized ``Europe/Warsaw`` slots."""
        active_run = self.new_run() if run is None else run
        url = self.base_url + SCHEDULER_PATH
        body = json.dumps(query.to_payload(), separators=(",", ":")).encode("utf-8")
        headers = self._headers()

        attempt = 0
        while True:
            if active_run.requests_made >= active_run.max_requests:
                raise CmpRunLimitError(attempted=False)
            self._wait_spacing()
            self._last_dispatch = self.monotonic()
            active_run.requests_made += 1
            try:
                response = self.transport.request(
                    "POST",
                    url,
                    headers=headers,
                    body=body,
                    timeout=self.timeout,
                )
            except TransportError:
                if attempt >= self.policy.max_retries:
                    raise CmpClientError("network", retryable=True) from None
                if active_run.requests_made >= active_run.max_requests:
                    raise CmpRunLimitError(attempted=True, cause_code="network") from None
                self.sleep(self._backoff_delay(attempt))
                attempt += 1
                continue

            if response.status == 200:
                try:
                    return parse_cmp_scheduler_bytes(
                        response.body,
                        default_specialty_id=query.specialty_id,
                    )
                except CmpParseError:
                    raise CmpClientError("invalid_response", http_status=200) from None

            if response.status in (401, 403):
                raise CmpClientError(
                    "authorization",
                    http_status=response.status,
                    retryable=False,
                    attention="authorization",
                )

            if response.status == 429 or 500 <= response.status <= 599:
                delay = self._handle_retryable_status(
                    response,
                    attempt=attempt,
                    active_run=active_run,
                )
                self.sleep(delay)
                attempt += 1
                continue

            raise CmpClientError("provider_http", http_status=response.status, retryable=False)
