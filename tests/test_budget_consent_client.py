"""Synthetic consent transport tests; no credentials, provider or bank calls."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError
from email.message import Message
import http.client
from io import BytesIO
import json
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives.asymmetric import rsa

from hestia.budget import client as base
from hestia.budget import consent_client as module
from hestia.budget.client import BudgetAPIError
from hestia.budget.consent_client import EnableBankingConsentClient, PSUContext
from hestia.budget.rate_limit import RateScope
from tests.test_budget_client import APP_ID, ACCOUNT, Opener, PRIVATE, Response, pem


SESSION = "/sessions/" + ACCOUNT
NOW = 1_700_000_000


def authorization(**changes):
    return {"access": {"valid_until": "2027-01-01T00:00:00Z"},
            "aspsp": {"name": "Synthetic Bank Śląski", "country": "PL"},
            "state": "synthetic-state-000001", "redirect_url": "https://hestia.example/api/budget/callback",
            "psu_type": "personal", **changes}


class ConsentClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key_pem = pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))

    def client(self, *responses, clock=None, monotonic=None):
        opener = Opener(*responses)
        instance = EnableBankingConsentClient(APP_ID, self.key_pem, opener=opener,
                                             clock=clock if clock is not None else lambda: NOW,
                                             monotonic=monotonic if monotonic is not None else lambda: 100)
        return instance, opener

    def assert_safe(self, error):
        self.assertNotIn(PRIVATE, str(error))
        self.assertNotIn(PRIVATE, "".join(traceback.format_exception(error)))
        self.assertNotIn(PRIVATE, repr(error.__dict__))
        self.assertNotIn(ACCOUNT, str(error))

    def invalid(self, invoke):
        instance, opener = self.client()
        with self.assertRaises(BudgetAPIError) as caught:
            invoke(instance)
        self.assertEqual(caught.exception.code, "invalid_request")
        self.assertEqual(opener.calls, [])
        self.assert_safe(caught.exception)

    def test_exact_routes_use_fixed_origin_bounded_time_and_no_implicit_psu(self):
        instance, opener = self.client(*(Response(b'{"message":"OK"}') for _ in range(5)))
        instance.get("/aspsps")
        body = authorization(auth_method="redirect", language="pl")
        body["access"].update(balances=True, transactions=True, transactions_extended_history=False)
        before = deepcopy(body)
        instance.post("/auth", body)
        instance.post("/sessions", {"code": "synthetic-code"})
        instance.get(SESSION)
        instance.delete(SESSION)
        self.assertEqual(body, before)
        expected = [("GET", "/aspsps"), ("POST", "/auth"), ("POST", "/sessions"),
                    ("GET", SESSION), ("DELETE", SESSION)]
        for (request, options), (method, path) in zip(opener.calls, expected):
            self.assertEqual((request.full_url, request.get_method()), (base.API_ORIGIN + path, method))
            self.assertEqual(options, {"timeout": 60})
            headers = {key.lower(): value for key, value in request.header_items()}
            self.assertTrue(headers["authorization"].startswith("Bearer "))
            self.assertFalse(any(key.startswith("psu-") for key in headers))
            if method == "POST":
                self.assertEqual(headers["content-type"], "application/json")
            else:
                self.assertIsNone(request.data)
                self.assertNotIn("content-type", headers)
        self.assertEqual(json.loads(opener.calls[1][0].data), body)
        self.assertEqual(json.loads(opener.calls[2][0].data), {"code": "synthetic-code"})

    def test_psu_is_explicit_frozen_redacted_and_only_documented_headers(self):
        context = PSUContext(ip_address="2001:db8::1", user_agent="Synthetic browser", referer="https://hestia.example/",
                             accept="application/json", accept_charset="utf-8", accept_encoding="gzip",
                             accept_language="pl,en;q=0.9", geo_location="-1.2345,6.789")
        self.assertNotIn("Synthetic browser", repr(context))
        with self.assertRaises(FrozenInstanceError):
            context.user_agent = "changed"
        instance, opener = self.client(Response(), Response(), Response())
        instance.post("/auth", authorization(), psu=context)
        instance.delete(SESSION, psu=PSUContext(ip_address="192.0.2.1"))
        instance.get(SESSION, psu=PSUContext())
        headers = {key.lower(): value for key, value in opener.calls[0][0].header_items()}
        self.assertEqual({key: value for key, value in headers.items() if key.startswith("psu-")},
                         {key.lower(): value for key, value in context.headers().items()})
        self.assertEqual(len(context.headers()), 8)
        self.assertEqual(PSUContext(geo_location="90,-180").geo_location, "90,-180")
        self.invalid(lambda client: client.get(SESSION, psu={"Authorization": PRIVATE}))

    def test_invalid_psu_never_becomes_a_header_or_leaks_input(self):
        for values in ({"ip_address": "not-an-ip"}, {"ip_address": "fe80::1%eth0"},
                       {"ip_address": "192.0.2.1:80"}, {"user_agent": "x\r\nAuthorization: " + PRIVATE},
                       {"user_agent": "x" * 1025}, {"user_agent": "ż"}, {"accept": 3},
                       {"accept": ""}, {"accept": "\t"}, {"geo_location": "90.1,0"},
                       {"geo_location": "0,180.1"}, {"geo_location": "NaN,0"}, {"geo_location": "0, 0"},
                       {"referer": "https://hestia.example/?code=" + PRIVATE}):
            with self.subTest(values=tuple(values)), self.assertRaises(BudgetAPIError) as caught:
                PSUContext(**values)
            self.assert_safe(caught.exception)

    def test_method_path_allowlist_does_not_loosen_read_only_client(self):
        for path in (None, 3, "", "/auth", "/sessions", "/aspsps/", SESSION + "/", SESSION + "?a=b",
                     SESSION + "#fragment", SESSION + "\n", "/sessions/%2e%2e", "/sessions/not-uuid",
                     "/accounts/" + ACCOUNT + "/balances", "https://api.enablebanking.com" + SESSION):
            self.invalid(lambda client: client.get(path))
        for method, path, body in (("post", "/aspsps", {}), ("post", SESSION, {}),
                                   ("delete", "/auth", None), ("delete", "/sessions", None),
                                   ("delete", "/payments/" + ACCOUNT, None)):
            self.invalid(lambda client: getattr(client, method)(path, body) if method == "post"
                         else getattr(client, method)(path))
        original = base.EnableBankingClient(APP_ID, self.key_pem, opener=Opener())
        with self.assertRaises(BudgetAPIError):
            original.get("/aspsps")

    def test_capability_query_is_typed_ais_personal_and_cannot_change_origin(self):
        instance, opener = self.client(Response(), Response())
        query = {"country": "PL", "psu_type": "personal", "service": "AIS"}
        instance.get("/aspsps", query)
        instance.get(SESSION, {})
        url = urlsplit(opener.calls[0][0].full_url)
        self.assertEqual((url.netloc, url.path), ("api.enablebanking.com", "/aspsps"))
        self.assertEqual(parse_qs(url.query), {key: [value] for key, value in query.items()})
        for query in ([], "a=b", {"country": "pl"}, {"country": None}, {"country": "PL\n"},
                      {"psu_type": "business"}, {"service": "PIS"}, {"payment_type": "SEPA"},
                      {"country": True}, {"code": PRIVATE}):
            self.invalid(lambda client: client.get("/aspsps", query))
        self.invalid(lambda client: client.get(SESSION, {"country": "PL"}))

    def test_authorization_bodies_are_personal_ais_only_and_request_bytes_are_bounded(self):
        malformed = [None, [], {}, {**authorization(), "credentials": {"password": PRIVATE}},
                     authorization(psu_type="business"), authorization(aspsp={"name": "Bank", "country": "PL", "x": 1}),
                     authorization(aspsp={"name": "Bank", "country": "pl"}),
                     authorization(aspsp={"name": "ż" * 100, "country": "PL"}),
                     authorization(state="\ud800"), authorization(state="\x00"), authorization(state=""),
                     authorization(language="PL"), authorization(language=True), authorization(auth_method=None),
                     authorization(access={"valid_until": "2027-01-01T00:00:00Z", "balances": 1}),
                     authorization(access={"valid_until": "2027-01-01T00:00:00Z", "accounts": []})]
        for body in malformed:
            self.invalid(lambda client: client.post("/auth", body))
        for body in ({}, {"code": "", "x": 1}, {"code": None}, {"code": "x" * 4097}, {"code": "\ud800"},
                     {"code": "\\" * 4096}):
            self.invalid(lambda client: client.post("/sessions", body))
        instance, opener = self.client(Response())
        instance.post("/sessions", {"code": "x" * 4096})
        self.assertLessEqual(len(opener.calls[0][0].data), module.MAX_REQUEST)

    def test_registered_redirect_shape_excludes_unsafe_urls_and_callback_secrets(self):
        for url in ("http://hestia.example/callback", "https:///callback", "https://user@hestia.example/",
                    "https://user:pass@hestia.example/", "https://hestia.example/?code=" + PRIVATE,
                    "https://hestia.example/#state", "https://hestia.example:444/", "https://hestia.example:bad/",
                    "https://hestia.example/ bad", "https://hestia.example/\\bad", "https://[bad/", None):
            self.invalid(lambda client: client.post("/auth", authorization(redirect_url=url)))
        instance, _ = self.client(Response())
        instance.post("/auth", authorization(redirect_url="https://hestia.example:443/callback"))

    def test_validity_is_future_rfc3339_with_explicit_zone_not_a_local_time_guess(self):
        for value in ("2020-01-01T00:00:00Z", "2027-01-01T00:00:00", "2027-01-01 00:00:00Z",
                      "2027-02-30T00:00:00Z", "2027-01-01T00:00:00+25:00", "2027-01-01", True):
            self.invalid(lambda client: client.post("/auth", authorization(access={"valid_until": value})))
        instance, _ = self.client(Response(), Response())
        instance.post("/auth", authorization(access={"valid_until": "2027-01-01T00:00:00.123456+01:00"}))
        instance.post("/auth", authorization(access={"valid_until": "2027-01-01T00:00:00.123456789Z"}))

    def test_monotonic_deadlines_cover_whole_operation_even_when_caller_omits_one(self):
        instance, opener = self.client(Response(), Response())
        instance.get("/aspsps", deadline=105)
        instance.get("/aspsps", deadline=1000)
        self.assertEqual([options["timeout"] for _, options in opener.calls], [5, 60])
        for deadline in (-1, True, "bad", float("nan"), float("inf"), 10 ** 1000):
            self.invalid(lambda client: client.get("/aspsps", deadline=deadline))
        for deadline in (99, 100):
            instance, opener = self.client()
            with self.assertRaises(BudgetAPIError) as caught:
                instance.get("/aspsps", deadline=deadline)
            self.assertEqual(caught.exception.code, "acquisition_timeout")
            self.assertEqual(opener.calls, [])
        now = [100]
        class SlowResponse(Response):
            def read1(self, size=-1):
                now[0] += 10
                return b" "
        reply = SlowResponse()
        instance, _ = self.client(reply, monotonic=lambda: now[0])
        with self.assertRaises(BudgetAPIError) as caught:
            instance.get("/aspsps")
        self.assertEqual((caught.exception.code, now[0], reply.closed), ("acquisition_timeout", 160, True))

    def test_invalid_wall_and_monotonic_clocks_fail_before_network(self):
        for value in (None, True, -1, "bad", float("nan"), float("inf"), 1e300, 10 ** 1000):
            instance, opener = self.client(monotonic=lambda: value)
            with self.subTest(clock=type(value).__name__), self.assertRaises(BudgetAPIError) as caught:
                instance.get("/aspsps")
            self.assertEqual(caught.exception.code, "invalid_configuration")
            self.assertEqual(opener.calls, [])
        for value in (None, True, -1, "bad", float("nan"), float("inf"), 10 ** 1000):
            instance, opener = self.client(clock=lambda: value)
            with self.assertRaises(BudgetAPIError) as caught:
                instance.post("/auth", authorization())
            self.assertEqual(caught.exception.code, "invalid_configuration")
            self.assertEqual(opener.calls, [])
        for clock in ("_clock", "_monotonic"):
            instance, opener = self.client()
            setattr(instance, clock, Mock(side_effect=ValueError(PRIVATE)))
            with self.assertRaises(BudgetAPIError) as caught:
                instance.post("/auth", authorization())
            self.assert_safe(caught.exception)
            self.assertEqual(opener.calls, [])

    def test_json_success_must_be_bounded_object_without_ambiguous_or_nonfinite_fields(self):
        for body in (b"[]", b"null", b"true", b'{} trailing', b'\xff', b'{"x":NaN}',
                     b'{"x":1,"x":2}', b'[' * 2000 + b']' * 2000):
            instance, _ = self.client(Response(body))
            with self.assertRaises(BudgetAPIError) as caught:
                instance.get("/aspsps")
            self.assertEqual(caught.exception.code, "invalid_response")
            self.assert_safe(caught.exception)
        with patch.object(base, "MAX_RESPONSE", 16):
            instance, _ = self.client(Response(b'{"private":"' + PRIVATE.encode() + b'"}'))
            with self.assertRaises(BudgetAPIError) as caught:
                instance.get("/aspsps")
            self.assertEqual(caught.exception.code, "invalid_response")

    def test_transport_errors_do_not_retry_or_expose_exception_details(self):
        for error in (OSError(PRIVATE), URLError(PRIVATE), ValueError(PRIVATE), http.client.HTTPException(PRIVATE)):
            instance, opener = self.client(error, Response())
            with self.assertRaises(BudgetAPIError) as caught:
                instance.post("/sessions", {"code": "synthetic-code"})
            self.assertEqual((caught.exception.code, caught.exception.retryable), ("network", True))
            self.assert_safe(caught.exception)
            self.assertEqual(len(opener.calls), 1)

    def test_http_errors_share_existing_safe_codes_and_no_post_retries(self):
        for status, expected in ((302, "provider_error"), (400, "provider_error"), (401, "authorization"),
                                 (403, "authorization"), (408, "unavailable"), (503, "unavailable")):
            for raised in (False, True):
                response = Response(PRIVATE.encode(), status)
                reply = HTTPError("https://outside.invalid/" + PRIVATE, status, PRIVATE, {}, response) if raised else response
                instance, opener = self.client(reply, Response())
                with self.assertRaises(BudgetAPIError) as caught:
                    instance.post("/sessions", {"code": "synthetic-code"})
                self.assertEqual((caught.exception.status, caught.exception.code), (status, expected))
                self.assert_safe(caught.exception)
                self.assertEqual((len(opener.calls), response.closed), (1, True))

    def test_429_full_retry_after_survives_returned_and_raised_errors_and_bad_cleanup(self):
        for path in ("/auth", "/sessions"):
            for raised in (False, True):
                headers = Message(); headers.add_header("Retry-After", "900000"); headers.add_header("Retry-After", "600")
                response = Response(b'{"error":"ASPSP_RATE_LIMIT_EXCEEDED","message":"' + PRIVATE.encode() + b'"}', 429, headers)
                self.addCleanup(response.close)
                reply = HTTPError("https://outside.invalid/" + PRIVATE, 429, PRIVATE, headers, response) if raised else response
                if raised:
                    self.addCleanup(reply.close)
                    reply.close = Mock(side_effect=OSError(PRIVATE))
                instance, opener = self.client(reply, Response())
                with self.assertRaises(BudgetAPIError) as caught:
                    instance.post(path, authorization() if path == "/auth" else {"code": "synthetic-code"})
                error = caught.exception
                self.assertEqual((error.status, error.code, error.retry_at, error.retry_suspended, error.rate_scope),
                                 (429, "rate_limit", NOW + 900000, True, RateScope.CONNECTION))
                self.assertEqual(error.provider_code, "ASPSP_RATE_LIMIT_EXCEEDED")
                self.assertEqual(len(opener.calls), 1)
                self.assert_safe(error)

    def test_late_429_body_keeps_server_deadline_even_if_read_cannot_finish(self):
        headers = Message(); headers["Retry-After"] = "600"
        now = [100]
        response = Response(b'{}', 429, headers)
        reply = HTTPError("https://outside.invalid/" + PRIVATE, 429, PRIVATE, headers, response)
        instance, opener = self.client(monotonic=lambda: now[0])
        def late(*args, **kwargs):
            now[0] = 200
            raise reply
        opener.open = late
        with self.assertRaises(BudgetAPIError) as caught:
            instance.post("/sessions", {"code": "synthetic-code"}, deadline=105)
        self.assertEqual((caught.exception.status, caught.exception.code, caught.exception.retry_at),
                         (429, "acquisition_timeout", NOW + 600))
        self.assertTrue(response.closed)
        self.assert_safe(caught.exception)

    def test_socket_read_timeouts_decrease_with_the_operation_budget(self):
        now = [100]
        class Socket:
            def __init__(self):
                self.timeouts = []
            def settimeout(self, value):
                self.timeouts.append(value)
        socket = Socket()
        class Chunked(Response):
            def __init__(self):
                super().__init__()
                self.chunks = [b'{"ok":', b'true}', b'']
                self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=socket))
            def read1(self, size=-1):
                now[0] += 1
                return self.chunks.pop(0)
        instance, opener = self.client(monotonic=lambda: now[0])
        def opened(*args, **kwargs):
            now[0] += 2
            return Chunked()
        opener.open = opened
        self.assertEqual(instance.get("/aspsps"), {"ok": True})
        self.assertEqual(socket.timeouts, [58, 57, 56])

    def test_open_using_59_seconds_leaves_only_one_second_for_a_stalled_body(self):
        for status in (200, 429):
            for raised in ((False, True) if status == 429 else (False,)):
                now = [100]
                class Socket:
                    timeout = 60
                    def settimeout(self, value):
                        self.timeout = value
                socket = Socket()
                class Stalled(Response):
                    def read1(self, size=-1):
                        now[0] += socket.timeout
                        raise TimeoutError(PRIVATE)
                headers = Message(); headers["Retry-After"] = "600"
                response = Stalled(status=status, headers=headers)
                response.fp = SimpleNamespace(raw=SimpleNamespace(_sock=socket))
                error = HTTPError("https://outside.invalid/" + PRIVATE, status, PRIVATE, headers, response) if raised else None
                instance, opener = self.client(monotonic=lambda: now[0])
                def opened(*args, **kwargs):
                    now[0] += 59
                    if raised:
                        raise error
                    return response
                opener.open = opened
                with self.assertRaises(BudgetAPIError) as caught:
                    instance.post("/sessions", {"code": "synthetic-code"})
                self.assertEqual((caught.exception.code, socket.timeout, now[0]), ("acquisition_timeout", 1, 160))
                if status == 429:
                    self.assertEqual((caught.exception.status, caught.exception.retry_at), (429, NOW + 600))
                self.assert_safe(caught.exception)

    def test_socket_setup_failure_and_invalid_wrappers_are_bounded_safe_errors(self):
        for status in (200, 429):
            headers = Message(); headers["Retry-After"] = "600"
            response = Response(b'{"error":"ASPSP_RATE_LIMIT_EXCEEDED"}', status, headers)
            response.fp = SimpleNamespace(raw=SimpleNamespace(_sock=SimpleNamespace(settimeout=Mock(side_effect=OSError(PRIVATE)))))
            instance, _ = self.client(response)
            with self.assertRaises(BudgetAPIError) as caught:
                instance.get(SESSION)
            self.assertEqual(caught.exception.code, "network")
            if status == 429:
                self.assertEqual((caught.exception.status, caught.exception.retry_at), (429, NOW + 600))
            self.assert_safe(caught.exception)
        response = Response()
        response.fp = response
        instance, _ = self.client(response)
        with self.assertRaises(BudgetAPIError) as caught:
            instance.get(SESSION)
        self.assertEqual(caught.exception.code, "invalid_response")
        # File-like injected responses without read1 retain the bounded base reader.
        response = SimpleNamespace(read=BytesIO(b'{}').read, status=200)
        instance, _ = self.client()
        self.assertEqual(instance._read_body(response, 20, 160), b'{}')

    def test_production_opener_disables_environment_proxy_and_redirect_handlers(self):
        instance = EnableBankingConsentClient(APP_ID, self.key_pem)
        self.assertTrue(any(isinstance(handler, base._NoRedirect) for handler in instance._opener.handlers))
        # An empty proxy map registers no protocol handlers, ignoring HTTP(S)_PROXY.
        self.assertFalse(any(type(handler).__name__ == "ProxyHandler" for handler in instance._opener.handlers))
