"""Unit tests for the CMP HTTP client using an injected fake transport (zero network calls)."""
from __future__ import annotations

from datetime import date, datetime, timezone
import http.client
from io import BytesIO
import json
from pathlib import Path
from unittest import TestCase
from urllib.error import HTTPError, URLError

from hestia.appointments import (
    CMP_BASE_URL,
    CmpClient,
    CmpClientError,
    CmpCredentials,
    CmpRunLimitError,
    CmpSchedulerQuery,
    HTTPResponse,
    PollPolicy,
    TransportError,
    UrllibTransport,
    WatchCriteria,
    parse_retry_after,
)
from hestia.appointments.cmp_client import _NoRedirect

FIXTURES = Path(__file__).parent / "fixtures" / "appointments"
VALID_BODY = (FIXTURES / "cmp_scheduler_valid.json").read_bytes()


class FakeTransport:
    """Deterministic offline transport recording every call and replaying queued outcomes."""

    def __init__(self, outcomes: list[HTTPResponse | Exception]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, object]] = []

    def request(self, method: str, url: str, *, headers, body=None, timeout=15.0) -> HTTPResponse:
        self.calls.append({
            "method": method,
            "url": url,
            "headers": dict(headers),
            "body": body,
            "timeout": timeout,
        })
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _FakeHTTPResponse:
    def __init__(self, code: int, body: bytes, headers: dict[str, str] | None = None, *, read_exc: Exception | None = None):
        self.code = code
        self._body = body
        self.headers = headers or {}
        self._read_exc = read_exc

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, size: int = -1) -> bytes:
        if self._read_exc is not None:
            raise self._read_exc
        return self._body[:size]


class CmpClientTests(TestCase):
    def setUp(self) -> None:
        self.criteria = WatchCriteria(
            watch_id="watch-1",
            provider="cmp",
            specialty_id="spec-01",
            doctor_ids=("doc-101",),
            location_ids=("loc-01",),
            service_id="srv-01",
            date_from=date(2026, 12, 10),
            date_to=date(2026, 12, 20),
        )
        self.query = CmpSchedulerQuery.from_criteria(self.criteria)

    def test_successful_query_payload_and_bearer_auth(self) -> None:
        transport = FakeTransport([HTTPResponse(200, {"Content-Type": "application/json"}, VALID_BODY)])
        clock = FakeClock()
        client = CmpClient(
            transport=transport,
            credentials=CmpCredentials(auth_mode="bearer", bearer_token="synthetic-token"),
            policy=PollPolicy(min_request_spacing_seconds=0.0),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        run = client.new_run()
        self.assertEqual(run.remaining, 6)
        result = client.get_scheduler(self.query, run=run)
        self.assertEqual(len(result.slots), 3)
        self.assertEqual(run.requests_made, 1)
        self.assertEqual(run.remaining, 5)
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], f"{CMP_BASE_URL}/GetScheduler")
        headers = call["headers"]
        assert isinstance(headers, dict)
        self.assertEqual(headers["Authorization"], "Bearer synthetic-token")
        sent_payload = json.loads(call["body"].decode("utf-8"))
        self.assertEqual(sent_payload, {
            "dateFrom": "2026-12-10",
            "dateTo": "2026-12-20",
            "specialtyId": "spec-01",
            "doctorIds": ["doc-101"],
            "locationIds": ["loc-01"],
            "serviceId": "srv-01",
        })

        # Minimal query omits optional empty fields and auth header when auth_mode="none".
        minimal_query = CmpSchedulerQuery(date_from=date(2026, 12, 10), date_to=date(2026, 12, 11))
        self.assertEqual(minimal_query.to_payload(), {"dateFrom": "2026-12-10", "dateTo": "2026-12-11"})
        anon_transport = FakeTransport([HTTPResponse(200, {}, VALID_BODY)])
        anon_client = CmpClient(transport=anon_transport, policy=PollPolicy(min_request_spacing_seconds=0.0))
        anon_client.get_scheduler(minimal_query)
        self.assertNotIn("Authorization", anon_transport.calls[0]["headers"])

    def test_retry_exponential_backoff_retry_after_and_spacing(self) -> None:
        now = datetime(2026, 12, 10, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        # Attempt 0 -> 429 with Retry-After=5 (> exp backoff 2.0)
        # Attempt 1 -> 503 with no Retry-After -> exp backoff 4.0 * 1.0 (jitter=1.0)
        # Attempt 2 -> 200 OK
        transport = FakeTransport([
            HTTPResponse(429, {"retry-after": "5"}, b'{"error":"slow down"}'),
            HTTPResponse(503, {}, b"unavailable"),
            HTTPResponse(200, {}, VALID_BODY),
        ])
        clock = FakeClock(start=100.0)
        client = CmpClient(
            transport=transport,
            policy=PollPolicy(
                max_requests_per_run=5,
                max_retries=3,
                base_backoff_seconds=2.0,
                max_backoff_seconds=30.0,
                min_request_spacing_seconds=1.5,
            ),
            monotonic=clock.monotonic,
            wall_clock=lambda: now,
            sleep=clock.sleep,
            jitter=lambda: 1.0,
        )
        result = client.get_scheduler(self.query)
        self.assertEqual(len(result.slots), 3)
        self.assertEqual(len(transport.calls), 3)
        # Sleep sequence: 5.0 (Retry-After) -> spacing already satisfied -> 4.0 (2 * 2**1).
        self.assertEqual(clock.sleeps, [5.0, 4.0])

        # Verify min_request_spacing_seconds sleeps between back-to-back calls when monotonic barely advances.
        spacing_transport = FakeTransport([
            HTTPResponse(200, {}, VALID_BODY),
            HTTPResponse(200, {}, VALID_BODY),
        ])
        spacing_clock = FakeClock(start=50.0)
        spacing_client = CmpClient(
            transport=spacing_transport,
            policy=PollPolicy(min_request_spacing_seconds=2.0),
            monotonic=spacing_clock.monotonic,
            sleep=spacing_clock.sleep,
        )
        spacing_client.get_scheduler(self.query)
        spacing_clock.now += 0.5
        spacing_client.get_scheduler(self.query)
        self.assertEqual(spacing_clock.sleeps, [1.5])

    def test_retry_exhaustion_large_retry_after_and_transport_errors(self) -> None:
        # Retry-After exceeding max_backoff_seconds aborts immediately without sleeping.
        transport = FakeTransport([HTTPResponse(429, {"Retry-After": "120"}, b"")])
        clock = FakeClock()
        client = CmpClient(
            transport=transport,
            policy=PollPolicy(max_retries=3, base_backoff_seconds=2.0, max_backoff_seconds=30.0, min_request_spacing_seconds=0.0),
            monotonic=clock.monotonic,
            wall_clock=lambda: 1000.0,
            sleep=clock.sleep,
            jitter=lambda: 0.0,
        )
        with self.assertRaises(CmpClientError) as ctx:
            client.get_scheduler(self.query)
        self.assertEqual(ctx.exception.code, "rate_limit")
        self.assertEqual(ctx.exception.http_status, 429)
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(ctx.exception.retry_after, 120.0)
        self.assertEqual(clock.sleeps, [])

        # Exhausted 5xx retries raise provider_http with retryable=True.
        t500 = FakeTransport([
            HTTPResponse(502, {}, b"bad gateway"),
            HTTPResponse(502, {}, b"bad gateway"),
        ])
        c500 = CmpClient(
            transport=t500,
            policy=PollPolicy(max_retries=1, base_backoff_seconds=2.0, max_backoff_seconds=10.0, min_request_spacing_seconds=0.0),
            sleep=clock.sleep,
            jitter=lambda: 0.0,
        )
        with self.assertRaises(CmpClientError) as ctx500:
            c500.get_scheduler(self.query)
        self.assertEqual(ctx500.exception.code, "provider_http")
        self.assertEqual(ctx500.exception.http_status, 502)
        self.assertTrue(ctx500.exception.retryable)
        self.assertEqual(clock.sleeps, [1.0])  # equal jitter at 0.0 -> 0.5 * 2.0

        # TransportError retries then raises network error.
        clock.sleeps.clear()
        tnet = FakeTransport([TransportError("timeout"), TransportError("timeout")])
        cnet = CmpClient(
            transport=tnet,
            policy=PollPolicy(max_retries=1, base_backoff_seconds=2.0, max_backoff_seconds=10.0, min_request_spacing_seconds=0.0),
            sleep=clock.sleep,
            jitter=lambda: 0.5,
        )
        with self.assertRaises(CmpClientError) as ctxnet:
            cnet.get_scheduler(self.query)
        self.assertEqual(ctxnet.exception.code, "network")
        self.assertTrue(ctxnet.exception.retryable)
        self.assertEqual(clock.sleeps, [1.5])

    def test_run_request_cap_and_non_retryable_errors(self) -> None:
        # Cap hit mid-retry on HTTP 503 raises CmpRunLimitError without sleeping.
        clock = FakeClock()
        t_cap_http = FakeTransport([HTTPResponse(503, {}, b"")])
        c_cap_http = CmpClient(
            transport=t_cap_http,
            policy=PollPolicy(max_requests_per_run=1, max_retries=3, min_request_spacing_seconds=0.0),
            sleep=clock.sleep,
        )
        with self.assertRaises(CmpRunLimitError):
            c_cap_http.get_scheduler(self.query)
        self.assertEqual(clock.sleeps, [])

        # Cap hit mid-retry on TransportError raises CmpRunLimitError without sleeping.
        t_cap_net = FakeTransport([TransportError("reset")])
        c_cap_net = CmpClient(
            transport=t_cap_net,
            policy=PollPolicy(max_requests_per_run=1, max_retries=3, min_request_spacing_seconds=0.0),
            sleep=clock.sleep,
        )
        with self.assertRaises(CmpRunLimitError):
            c_cap_net.get_scheduler(self.query)
        self.assertEqual(clock.sleeps, [])

        # Cap already exhausted before dispatching raises CmpRunLimitError without calling transport.
        t_exhausted = FakeTransport([])
        c_exhausted = CmpClient(transport=t_exhausted, policy=PollPolicy(max_requests_per_run=1))
        exhausted_run = c_exhausted.new_run()
        exhausted_run.requests_made = 1
        with self.assertRaises(CmpRunLimitError):
            c_exhausted.get_scheduler(self.query, run=exhausted_run)
        self.assertEqual(t_exhausted.calls, [])

        # 401/403 fail fast without retry and flag authorization attention.
        for status in (401, 403):
            t_auth = FakeTransport([HTTPResponse(status, {}, b"secret-body-must-not-leak")])
            c_auth = CmpClient(transport=t_auth, policy=PollPolicy(max_retries=3))
            with self.assertRaises(CmpClientError) as ctx_auth:
                c_auth.get_scheduler(self.query)
            self.assertEqual(ctx_auth.exception.code, "authorization")
            self.assertEqual(ctx_auth.exception.http_status, status)
            self.assertEqual(ctx_auth.exception.attention, "authorization")
            self.assertFalse(ctx_auth.exception.retryable)
            self.assertNotIn("secret-body-must-not-leak", str(ctx_auth.exception))
            self.assertEqual(len(t_auth.calls), 1)

        # 400 and invalid 200 JSON body fail fast without retry.
        t_400 = FakeTransport([HTTPResponse(400, {}, b"bad")])
        with self.assertRaises(CmpClientError) as ctx_400:
            CmpClient(transport=t_400, policy=PollPolicy(max_retries=3)).get_scheduler(self.query)
        self.assertEqual(ctx_400.exception.code, "provider_http")
        self.assertFalse(ctx_400.exception.retryable)

        t_bad_json = FakeTransport([HTTPResponse(200, {}, b"not-json")])
        with self.assertRaises(CmpClientError) as ctx_json:
            CmpClient(transport=t_bad_json).get_scheduler(self.query)
        self.assertEqual(ctx_json.exception.code, "invalid_response")
        self.assertEqual(ctx_json.exception.http_status, 200)

        # Unknown error code falls back to provider_http; invalid timeout rejected; default transport initialized.
        self.assertEqual(CmpClientError("unlisted").code, "provider_http")
        self.assertIsInstance(CmpClient().transport, UrllibTransport)
        for bad_timeout in (0, -1, 121, float("nan"), True):
            with self.subTest(bad_timeout=bad_timeout):
                with self.assertRaises(ValueError):
                    CmpClient(transport=FakeTransport([]), timeout=bad_timeout)

    def test_parse_retry_after_and_response_header(self) -> None:
        now = datetime(2026, 12, 10, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertIsNone(parse_retry_after(None, now=now))
        self.assertIsNone(parse_retry_after("", now=now))
        self.assertIsNone(parse_retry_after(" " * 130, now=now))
        self.assertIsNone(parse_retry_after("9" * 129, now=now))
        self.assertEqual(parse_retry_after(" 42\t", now=now), 42.0)
        self.assertEqual(parse_retry_after("Thu, 10 Dec 2026 12:01:30 GMT", now=now), 90.0)
        # Naive HTTP date treated as UTC.
        self.assertEqual(parse_retry_after("Thu, 10 Dec 2026 12:00:30", now=now), 30.0)
        # Past HTTP date returns None.
        self.assertIsNone(parse_retry_after("Thu, 10 Dec 2026 11:59:00 GMT", now=now))
        self.assertIsNone(parse_retry_after("not-a-date", now=now))

        resp = HTTPResponse(200, {"X-Test": "value"})
        self.assertEqual(resp.header("x-test"), "value")
        self.assertIsNone(resp.header("Missing"))

    def test_urllib_transport_offline_with_mocked_opener(self) -> None:
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://example.test"))

        # Rejects non-HTTPS URLs before opening.
        t_plain = UrllibTransport(opener_factory=lambda *_args: self.fail("should not open"))
        with self.assertRaises(TransportError):
            t_plain.request("POST", "http://ic-api.cmp.med.pl/api/v1/GetScheduler", headers={})

        # Successful response read.
        class _Opener:
            def __init__(self, outcome):
                self.outcome = outcome

            def open(self, req, timeout=None):
                if isinstance(self.outcome, Exception):
                    raise self.outcome
                return self.outcome

        t_ok = UrllibTransport(opener_factory=lambda *_args: _Opener(_FakeHTTPResponse(200, b'{"days":[]}', {"X-A": "1"})))
        resp = t_ok.request("POST", "https://ic-api.cmp.med.pl/api/v1/GetScheduler", headers={"Accept": "application/json"}, body=b"{}")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body, b'{"days":[]}')
        self.assertEqual(resp.header("x-a"), "1")

        # HTTPError is returned as an HTTPResponse rather than raised as TransportError.
        http_err = HTTPError(
            "https://ic-api.cmp.med.pl/api/v1/GetScheduler",
            429,
            "Too Many Requests",
            {"Retry-After": "10"},
            BytesIO(b"rate-limited"),
        )
        t_http_err = UrllibTransport(opener_factory=lambda *_args: _Opener(http_err))
        err_resp = t_http_err.request("POST", "https://ic-api.cmp.med.pl/api/v1/GetScheduler", headers={})
        self.assertEqual(err_resp.status, 429)
        self.assertEqual(err_resp.header("Retry-After"), "10")
        self.assertEqual(err_resp.body, b"rate-limited")

        # Oversized response raises TransportError.
        t_big = UrllibTransport(
            max_response_bytes=4,
            opener_factory=lambda *_args: _Opener(_FakeHTTPResponse(200, b"12345")),
        )
        with self.assertRaises(TransportError):
            t_big.request("POST", "https://ic-api.cmp.med.pl/api/v1/GetScheduler", headers={})

        # Open error and mid-read interruption both raise TransportError.
        t_open_err = UrllibTransport(opener_factory=lambda *_args: _Opener(URLError("offline")))
        with self.assertRaises(TransportError):
            t_open_err.request("POST", "https://ic-api.cmp.med.pl/api/v1/GetScheduler", headers={})

        t_read_err = UrllibTransport(
            opener_factory=lambda *_args: _Opener(
                _FakeHTTPResponse(200, b"", read_exc=http.client.IncompleteRead(b""))
            )
        )
        with self.assertRaises(TransportError):
            t_read_err.request("POST", "https://ic-api.cmp.med.pl/api/v1/GetScheduler", headers={})
