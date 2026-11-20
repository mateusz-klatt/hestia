"""Read-only API security contract, exercised without keys or bank connections."""
from __future__ import annotations

import base64
from email.message import Message
from email.utils import format_datetime
from datetime import datetime, timezone
import http.client
from io import BytesIO
import json
import traceback
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPSHandler, build_opener
from urllib.response import addinfourl

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from hestia.budget import client as module
from hestia.budget.client import BudgetAPIError, EnableBankingClient
from hestia.budget.rate_limit import RateScope, MAX_AUTOMATIC_DELAY, MAX_HEADER_CHARS


APP_ID = "a1111111-b222-4333-8444-c55555555555"
ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
SESSION = "/sessions/" + ACCOUNT
BALANCES = "/accounts/" + ACCOUNT + "/balances"
TRANSACTIONS = "/accounts/" + ACCOUNT + "/transactions"
PRIVATE = "PRIVATE-BANK-DETAILS-MUST-NOT-APPEAR"


def pem(key, *, encryption=None, format=serialization.PrivateFormat.PKCS8):
    return key.private_bytes(serialization.Encoding.PEM, format,
                             encryption or serialization.NoEncryption())


def decode_part(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class Response(BytesIO):
    def __init__(self, body=b"{}", status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)

    def read1(self, size=-1):
        return self.read(size)


class Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def open(self, request, **kwargs):
        self.calls.append((request, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class BudgetClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.key_pem = pem(cls.key)

    def client(self, *responses, clock=None, monotonic=None):
        opener = Opener(*responses)
        client = EnableBankingClient(APP_ID, self.key_pem, opener=opener,
                                     clock=clock if clock is not None else lambda: 1_700_000_000,
                                     monotonic=monotonic if monotonic is not None else lambda: 100.0)
        return client, opener

    def assert_safe(self, exception):
        self.assertNotIn(PRIVATE, str(exception))
        self.assertNotIn(ACCOUNT, str(exception))
        self.assertNotIn(PRIVATE, "".join(traceback.format_exception(exception)))
        self.assertIn(exception.code, BudgetAPIError.CODES)
        self.assertNotIn(PRIVATE, repr(exception.__dict__))
        self.assertNotIn(ACCOUNT, repr(exception.__dict__))

    def rate_error(self, body, *, headers=None, path=TRANSACTIONS, raised=False, clock=None):
        response = Response(body, 429, headers)
        reply = HTTPError("https://outside.invalid/" + PRIVATE, 429, PRIVATE, headers, response) if raised else response
        client, opener = self.client(reply, Response(), clock=clock)
        with self.assertRaises(BudgetAPIError) as caught:
            client.get(path)
        self.assert_safe(caught.exception)
        self.assertEqual((caught.exception.status, caught.exception.code, caught.exception.retryable), (429, "rate_limit", True))
        self.assertEqual(len(opener.calls), 1)
        self.assertTrue(response.closed)
        self.assertTrue(response.read_sizes)
        self.assertTrue(all(0 < size <= module.READ_CHUNK for size in response.read_sizes))
        return caught.exception

    def test_all_three_routes_are_gets_on_fixed_origin_without_psu_headers(self):
        client, opener = self.client(*(Response(b'{"ok":true}') for _ in range(3)))
        for path in (SESSION, BALANCES, TRANSACTIONS):
            self.assertEqual(client.get(path), {"ok": True})
        for (request, options), path in zip(opener.calls, (SESSION, BALANCES, TRANSACTIONS)):
            self.assertEqual(request.full_url, "https://api.enablebanking.com" + path)
            self.assertEqual(request.get_method(), "GET")
            self.assertIsNone(request.data)
            self.assertEqual(options, {"timeout": 60})
            headers = {name.lower(): value for name, value in request.header_items()}
            self.assertEqual(set(headers), {"authorization", "accept", "user-agent"})
            self.assertTrue(headers["authorization"].startswith("Bearer "))
            self.assertEqual(headers["accept"], "application/json")

    def test_jwt_signature_and_claims_are_valid_rs256(self):
        client = EnableBankingClient(APP_ID.upper(), self.key_pem, opener=Opener(Response()), clock=lambda: 123456)
        client.get(SESSION)
        token = client._opener.calls[0][0].get_header("Authorization").removeprefix("Bearer ")
        encoded_header, encoded_claims, encoded_signature = token.split(".")
        self.assertEqual(json.loads(decode_part(encoded_header)), {"alg": "RS256", "typ": "JWT", "kid": APP_ID})
        claims = json.loads(decode_part(encoded_claims))
        self.assertEqual(claims, {"iss": "enablebanking.com", "aud": "api.enablebanking.com",
                                  "iat": 123456, "exp": 127056})
        self.key.public_key().verify(decode_part(encoded_signature),
                                     (encoded_header + "." + encoded_claims).encode(),
                                     padding.PKCS1v15(), hashes.SHA256())

    def test_jwt_cache_refreshes_before_expiry_and_after_clock_moves_backwards(self):
        now = [10000]
        client, opener = self.client(*(Response() for _ in range(4)), clock=lambda: now[0])
        client.get(SESSION)
        now[0] += 3539
        client.get(SESSION)
        now[0] += 1
        client.get(SESSION)
        now[0] = 9000
        client.get(SESSION)
        tokens = [request.get_header("Authorization") for request, _ in opener.calls]
        self.assertEqual(tokens[0], tokens[1])
        self.assertNotEqual(tokens[1], tokens[2])
        self.assertNotEqual(tokens[2], tokens[3])
        self.assertEqual(json.loads(decode_part(tokens[3].split(".")[1]))["iat"], 9000)

    def test_opaque_query_is_encoded_without_mutating_input_or_allowing_another_origin(self):
        query = {"date_from": "2026-11-01", "continuation_key": "opaque+/=&?https://outside.invalid/", "limit": 25}
        before = dict(query)
        client, opener = self.client(Response(), Response())
        client.get(TRANSACTIONS, query)
        client.get(BALANCES, {})
        url = urlsplit(opener.calls[0][0].full_url)
        self.assertEqual((url.scheme, url.netloc, url.path), ("https", "api.enablebanking.com", TRANSACTIONS))
        self.assertEqual(parse_qs(url.query), {key: [str(value)] for key, value in query.items()})
        self.assertEqual(query, before)
        self.assertEqual(opener.calls[1][0].full_url, module.API_ORIGIN + BALANCES)

    def test_invalid_paths_and_query_shapes_never_reach_transport(self):
        paths = (None, 5, "", "/application", "/auth", "/sessions", SESSION + "/", SESSION + "?x=y",
                 SESSION + "#fragment", SESSION + "\n", "/SESSIONS/" + ACCOUNT,
                 "/accounts/" + ACCOUNT + "/details", "/accounts/" + ACCOUNT + "/transactions/anything",
                 "https://api.enablebanking.com" + SESSION, "https://outside.invalid" + SESSION,
                 "//outside.invalid/" + ACCOUNT, "/accounts/../balances", "/sessions/%2e%2e", "/sessions/not-uuid")
        for path in paths:
            client, opener = self.client()
            with self.subTest(path=path), self.assertRaises(BudgetAPIError) as error:
                client.get(path)
            self.assertEqual(error.exception.code, "invalid_request")
            self.assertEqual(opener.calls, [])
        for query in ([], "x=y", {3: "x"}, {"bad-key": "x"}, {"PSU": "x"}, {"x": None},
                      {"x": []}, {"x": {}}, {"x": True}, {"x": 1.25}, {"x": "\ud800"}):
            client, opener = self.client()
            with self.subTest(query=query), self.assertRaises(BudgetAPIError) as error:
                client.get(SESSION, query)
            self.assertEqual(error.exception.code, "invalid_request")
            self.assertEqual(opener.calls, [])

    def test_configuration_rejects_non_uuid_ids_invalid_encrypted_weak_and_non_rsa_keys(self):
        for identifier in (None, 12, "", APP_ID + "\n", "urn:uuid:" + APP_ID, "{" + APP_ID + "}", "not-uuid"):
            with self.subTest(identifier=identifier), self.assertRaises(BudgetAPIError) as error:
                EnableBankingClient(identifier, self.key_pem)
            self.assertEqual(error.exception.code, "invalid_configuration")
        other_keys = (b"", PRIVATE.encode(), PRIVATE, None,
                      self.key.public_key().public_bytes(serialization.Encoding.PEM,
                                                         serialization.PublicFormat.SubjectPublicKeyInfo),
                      pem(self.key, encryption=serialization.BestAvailableEncryption(b"synthetic-test-only")),
                      pem(rsa.generate_private_key(public_exponent=65537, key_size=1024)),
                      pem(ec.generate_private_key(ec.SECP256R1())))
        for key in other_keys:
            with self.subTest(key_type=type(key).__name__), self.assertRaises(BudgetAPIError) as error:
                EnableBankingClient(APP_ID, key)
            self.assertEqual(error.exception.code, "invalid_configuration")
            self.assert_safe(error.exception)
        with patch.object(module.serialization, "load_pem_private_key", side_effect=UnsupportedAlgorithm(PRIVATE)):
            with self.assertRaises(BudgetAPIError) as error:
                EnableBankingClient(APP_ID, self.key_pem)
            self.assert_safe(error.exception)
        client = EnableBankingClient(APP_ID, pem(self.key, format=serialization.PrivateFormat.TraditionalOpenSSL),
                                     opener=Opener(Response()))
        self.assertEqual(client.get(SESSION), {})

    def test_invalid_clocks_and_signing_failures_are_safe_configuration_errors(self):
        for now in (None, "bad", True, -1, float("nan"), float("inf"), 10 ** 1000):
            client, opener = self.client(clock=lambda: now)
            with self.subTest(value_type=type(now).__name__), self.assertRaises(BudgetAPIError) as error:
                client.get(SESSION)
            self.assertEqual(error.exception.code, "invalid_configuration")
            self.assertEqual(opener.calls, [])
        client, opener = self.client()
        client._key = Mock()
        client._key.sign.side_effect = ValueError(PRIVATE)
        with self.assertRaises(BudgetAPIError) as error:
            client.get(SESSION)
        self.assert_safe(error.exception)
        self.assertEqual(opener.calls, [])

    def test_http_status_mapping_reads_only_bounded_429_metadata_and_does_not_retry(self):
        cases = [(status, "provider_error", False) for status in (301, 302, 303, 307, 308, 400, 404)]
        cases += [(401, "authorization", False), (403, "authorization", False), (429, "rate_limit", True)]
        cases += [(status, "unavailable", True) for status in (408, 425, 500, 503, 599)]
        for status, code, retryable in cases:
            for raised in (False, True):
                body = BytesIO(PRIVATE.encode())
                reply = HTTPError("https://outside.invalid/" + PRIVATE, status, PRIVATE, {}, body) if raised else Response(PRIVATE.encode(), status)
                client, opener = self.client(reply, Response())
                with self.subTest(status=status, raised=raised), self.assertRaises(BudgetAPIError) as error:
                    client.get(SESSION)
                self.assertEqual((error.exception.status, error.exception.code, error.exception.retryable),
                                 (status, code, retryable))
                self.assert_safe(error.exception)
                self.assertEqual(len(opener.calls), 1)
                self.assertTrue(body.closed if raised else reply.closed)
                if not raised:
                    self.assertEqual(bool(reply.read_sizes), status == 429)
                    self.assertTrue(all(0 < size <= module.READ_CHUNK for size in reply.read_sizes))

    def test_network_errors_before_and_during_read_are_safe_and_close_responses(self):
        for failure in (OSError(PRIVATE), URLError(PRIVATE), TimeoutError(PRIVATE), ValueError(PRIVATE),
                        http.client.BadStatusLine(PRIVATE), http.client.IncompleteRead(PRIVATE.encode())):
            client, opener = self.client(failure)
            with self.subTest(failure=type(failure).__name__), self.assertRaises(BudgetAPIError) as error:
                client.get(SESSION)
            self.assertEqual((error.exception.status, error.exception.code, error.exception.retryable), (None, "network", True))
            self.assert_safe(error.exception)
            self.assertEqual(len(opener.calls), 1)
            response = Response()
            response.read = Mock(side_effect=failure)
            client, _ = self.client(response)
            with self.assertRaises(BudgetAPIError) as error:
                client.get(SESSION)
            self.assertEqual((error.exception.status, error.exception.code, error.exception.retryable), (200, "network", True))
            self.assert_safe(error.exception)
            self.assertTrue(response.closed)

    def test_error_response_cleanup_failure_preserves_safe_http_diagnostic(self):
        reply = HTTPError("https://outside.invalid/" + PRIVATE, 403, PRIVATE, {}, BytesIO(PRIVATE.encode()))
        # The client deliberately tolerates this close failure. Retain the real
        # cleanup so the test does not leave HTTPError's wrapper unclosed.
        self.addCleanup(reply.close)
        reply.close = Mock(side_effect=OSError(PRIVATE))
        client, opener = self.client(reply)
        with self.assertRaises(BudgetAPIError) as error:
            client.get(SESSION)
        self.assertEqual((error.exception.status, error.exception.code), (403, "authorization"))
        self.assert_safe(error.exception)
        self.assertEqual(len(opener.calls), 1)

    def test_invalid_json_non_objects_and_unbounded_or_invalid_responses_are_rejected(self):
        for body in (b"", PRIVATE.encode(), b"\xff", b"[]", b"null", b"42", b'"string"',
                     b'{"amount":NaN}', b'{"amount":Infinity}', b'{"amount":-Infinity}', b"[" * 2000):
            response = Response(body)
            client, _ = self.client(response)
            with self.subTest(body_prefix=body[:20]), self.assertRaises(BudgetAPIError) as error:
                client.get(SESSION)
            self.assertEqual((error.exception.status, error.exception.code, error.exception.retryable), (200, "invalid_response", False))
            self.assert_safe(error.exception)
            self.assertTrue(response.closed)
        for status in (None, True, "200", 99, 600):
            client, _ = self.client(Response(status=status))
            with self.subTest(status=status), self.assertRaises(BudgetAPIError) as error:
                client.get(SESSION)
            self.assertEqual((error.exception.status, error.exception.code), (None, "invalid_response"))
        response = Response()
        response.read = Mock(return_value=None)
        client, _ = self.client(response)
        with self.assertRaises(BudgetAPIError):
            client.get(SESSION)
        response = Response(b"x" * (module.MAX_RESPONSE + 1))
        client, _ = self.client(response)
        with self.assertRaises(BudgetAPIError) as error:
            client.get(SESSION)
        self.assertEqual(error.exception.code, "invalid_response")
        self.assertEqual(sum(response.read_sizes), module.MAX_RESPONSE + 1)
        self.assertTrue(all(size <= module.READ_CHUNK for size in response.read_sizes))
        self.assertTrue(response.closed)

    def test_exact_limit_json_object_is_accepted(self):
        body = b'{"x":"' + b"a" * (module.MAX_RESPONSE - 8) + b'"}'
        self.assertEqual(len(body), module.MAX_RESPONSE)
        client, _ = self.client(Response(body))
        self.assertEqual(len(client.get(SESSION)["x"]), module.MAX_RESPONSE - 8)

    def test_default_opener_blocks_real_redirect_processing_and_ignores_proxy_environment(self):
        calls = []

        class FakeHTTPS(HTTPSHandler):
            def https_open(self, request):
                calls.append(request)
                headers = Message()
                headers["Location"] = "https://outside.invalid/" + PRIVATE
                reply = addinfourl(BytesIO(PRIVATE.encode()), headers, request.full_url, 302)
                reply.msg = "Found"
                return reply

        def offline_opener(*handlers):
            self.assertEqual(handlers[0].proxies, {})
            self.assertIsInstance(handlers[1], module._NoRedirect)
            return build_opener(*handlers, FakeHTTPS())

        with patch.object(module, "build_opener", side_effect=offline_opener):
            client = EnableBankingClient(APP_ID, self.key_pem)
            with self.assertRaises(BudgetAPIError) as error:
                client.get(SESSION)
        self.assertEqual((error.exception.status, error.exception.code), (302, "provider_error"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].full_url, module.API_ORIGIN + SESSION)
        self.assert_safe(error.exception)

    def test_error_constructor_cannot_expose_untrusted_diagnostics(self):
        for status, code in ((PRIVATE, PRIVATE), (True, []), (None, None), (99, "unknown"), (600, 3)):
            error = BudgetAPIError(status, code)
            self.assertEqual((error.status, error.code, error.retryable), (None, "provider_error", False))
            self.assert_safe(error)

    def test_structured_error_metadata_preserves_legacy_arguments_and_sanitizes_new_fields(self):
        legacy = BudgetAPIError(429, "rate_limit", True)
        self.assertEqual((legacy.provider_code, legacy.retry_at, legacy.retry_suspended, legacy.rate_scope),
                         (None, None, False, RateScope.CONNECTION))
        self.assertEqual(str(legacy), "Budget API request failed: rate_limit (HTTP 429)")
        for deadline in (0, 123.5):
            error = BudgetAPIError(429, "rate_limit", True, provider_code="ASPSP_RATE_LIMIT_EXCEEDED",
                                   retry_at=deadline, retry_suspended=True, rate_scope="account_endpoint")
            self.assertEqual((error.provider_code, error.retry_at, error.retry_suspended, error.rate_scope),
                             ("ASPSP_RATE_LIMIT_EXCEEDED", float(deadline), True, RateScope.ACCOUNT_ENDPOINT))
        for deadline in (None, True, PRIVATE, [], -1, float("nan"), float("inf"), 10 ** 1000):
            error = BudgetAPIError(429, "rate_limit", provider_code=PRIVATE, retry_at=deadline, rate_scope=PRIVATE)
            self.assertEqual((error.provider_code, error.retry_at, error.rate_scope), (None, None, RateScope.CONNECTION))
            self.assert_safe(error)
        for provider in (None, [], {"error": PRIVATE}):
            self.assertIsNone(BudgetAPIError(provider_code=provider).provider_code)
        self.assertEqual(BudgetAPIError(rate_scope=RateScope.APPLICATION).rate_scope, RateScope.CONNECTION)

    def test_documented_top_level_error_is_the_only_trusted_provider_field(self):
        body = json.dumps({"error": "ASPSP_RATE_LIMIT_EXCEEDED", "message": PRIVATE,
                           "detail": {"account_id": ACCOUNT}, "retry_at": PRIVATE,
                           "rate_scope": "application"}).encode()
        for path in (TRANSACTIONS, BALANCES, SESSION):
            for raised in (False, True):
                with self.subTest(path=path, raised=raised):
                    error = self.rate_error(body, path=path, raised=raised)
                    self.assertEqual(error.provider_code, "ASPSP_RATE_LIMIT_EXCEEDED")
                    self.assertEqual(error.rate_scope, RateScope.CONNECTION if path == SESSION else RateScope.ACCOUNT_ENDPOINT)
                    self.assertIsNone(error.retry_at)
                    self.assertFalse(error.retry_suspended)
        for data in ({"error": "APPLICATION_RATE_LIMIT_EXCEEDED"}, {"error": PRIVATE}, {"error": []},
                     {"error": None}, {"detail": {"error": "ASPSP_RATE_LIMIT_EXCEEDED"}},
                     {"message": "ASPSP_RATE_LIMIT_EXCEEDED"}, []):
            error = self.rate_error(json.dumps(data).encode(), headers={"Retry-After": "3600"})
            self.assertIsNone(error.provider_code)
            self.assertEqual(error.rate_scope, RateScope.CONNECTION)
            self.assertEqual(error.retry_at, 1_700_003_600)

    def test_retry_after_seconds_and_http_date_use_injected_clock_on_both_transport_paths(self):
        now = 1_700_000_000
        future = now + 7200
        date_header = format_datetime(datetime.fromtimestamp(future, timezone.utc), usegmt=True)
        body = b'{"error":"ASPSP_RATE_LIMIT_EXCEEDED"}'
        for value, expected in (("120", now + 120), ("0", now), (" \t3600\t ", now + 3600), (date_header, future)):
            for raised in (False, True):
                with self.subTest(value=value, raised=raised):
                    error = self.rate_error(body, headers={"Retry-After": value}, raised=raised)
                    self.assertEqual(error.retry_at, expected)
                    self.assertFalse(error.retry_suspended)
                    self.assertEqual(error.rate_scope, RateScope.ACCOUNT_ENDPOINT)

    def test_long_and_oversized_hints_suspend_classified_scope_without_shortening_server_delay(self):
        body = b'{"error":"ASPSP_RATE_LIMIT_EXCEEDED"}'
        delay = MAX_AUTOMATIC_DELAY + 1
        for value in (str(delay), "9" * MAX_HEADER_CHARS):
            error = self.rate_error(body, headers={"Retry-After": value})
            self.assertTrue(error.retry_suspended)
            self.assertGreaterEqual(error.retry_at, 1_700_000_000 + delay)
            self.assertEqual(error.rate_scope, RateScope.ACCOUNT_ENDPOINT)
        error = self.rate_error(body, headers={"Retry-After": "9" * (MAX_HEADER_CHARS + 1)})
        self.assertTrue(error.retry_suspended)
        self.assertIsNone(error.retry_at)
        self.assertEqual(error.rate_scope, RateScope.ACCOUNT_ENDPOINT)

    def test_duplicate_hints_keep_strictest_deadline_and_suspension_even_with_malformed_body(self):
        headers = Message()
        for value in ("120", str(MAX_AUTOMATIC_DELAY + 1), PRIVATE, "9" * (MAX_HEADER_CHARS + 1), "3600"):
            headers["Retry-After"] = value
        for body, scope in ((b'{"error":"ASPSP_RATE_LIMIT_EXCEEDED"}', RateScope.ACCOUNT_ENDPOINT),
                            (PRIVATE.encode(), RateScope.CONNECTION)):
            error = self.rate_error(body, headers=headers, raised=True)
            self.assertEqual(error.retry_at, 1_700_000_000 + MAX_AUTOMATIC_DELAY + 1)
            self.assertTrue(error.retry_suspended)
            self.assertEqual(error.rate_scope, scope)

    def test_malformed_and_past_hints_do_not_invent_an_application_wide_scope(self):
        body = b'{"error":"ASPSP_RATE_LIMIT_EXCEEDED"}'
        for value in (None, True, [], {}, "", "-1", "1.5", PRIVATE, "Wed, 01 Jan 2020 00:00:00 GMT"):
            error = self.rate_error(body, headers={"Retry-After": value})
            self.assertIsNone(error.retry_at)
            self.assertFalse(error.retry_suspended)
            self.assertEqual(error.rate_scope, RateScope.ACCOUNT_ENDPOINT)
        error = self.rate_error(body, headers=Message())
        self.assertIsNone(error.retry_at)
        self.assertFalse(error.retry_suspended)
        bad_headers = Mock()
        bad_headers.get_all.side_effect = OSError(PRIVATE)
        error = self.rate_error(body, headers=bad_headers)
        self.assertIsNone(error.retry_at)
        self.assertTrue(error.retry_suspended)
        self.assertEqual(error.rate_scope, RateScope.ACCOUNT_ENDPOINT)

    def test_invalid_clock_during_rate_metadata_processing_preserves_safe_429_and_suspends_connection(self):
        body = b'{"error":"ASPSP_RATE_LIMIT_EXCEEDED"}'
        for value in (None, True, float("nan"), float("inf"), 10 ** 1000, ValueError(PRIVATE), OverflowError(PRIVATE)):
            clock = Mock(side_effect=[1_700_000_000, value])
            error = self.rate_error(body, headers={"Retry-After": "120"}, clock=clock)
            self.assertIsNone(error.retry_at)
            self.assertTrue(error.retry_suspended)
            self.assertEqual(error.rate_scope, RateScope.CONNECTION)
            self.assertEqual(error.provider_code, "ASPSP_RATE_LIMIT_EXCEEDED")

    def test_malformed_ambiguous_or_oversized_error_bodies_preserve_valid_header_deadline(self):
        bodies = (b"", PRIVATE.encode(), b"\xff", b"null", b'{"error":NaN}', b"[" * 2000,
                  b'{"error":"PRIVATE","error":"ASPSP_RATE_LIMIT_EXCEEDED"}',
                  b"x" * (module.MAX_ERROR_RESPONSE + 1))
        for body in bodies:
            with self.subTest(body_prefix=body[:20]):
                error = self.rate_error(body, headers={"Retry-After": "900"})
                self.assertIsNone(error.provider_code)
                self.assertEqual(error.retry_at, 1_700_000_900)
                self.assertEqual(error.rate_scope, RateScope.CONNECTION)
        exact = b'{"error":"ASPSP_RATE_LIMIT_EXCEEDED"}'
        exact += b" " * (module.MAX_ERROR_RESPONSE - len(exact))
        self.assertEqual(self.rate_error(exact).provider_code, "ASPSP_RATE_LIMIT_EXCEEDED")

    def test_error_body_read_failure_or_nonbytes_does_not_replace_429_or_drop_header(self):
        for raised in (False, True):
            for failure in (OSError(PRIVATE), http.client.IncompleteRead(PRIVATE.encode()), None, PRIVATE):
                response = Response(status=429, headers={"Retry-After": "1200"})
                response.read = Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=failure)
                reply = HTTPError("https://outside.invalid/" + PRIVATE, 429, PRIVATE, response.headers, response) if raised else response
                client, opener = self.client(reply)
                with self.assertRaises(BudgetAPIError) as caught:
                    client.get(BALANCES)
                error = caught.exception
                self.assertEqual((error.status, error.code, error.retry_at), (429, "rate_limit", 1_700_001_200))
                self.assertIsNone(error.provider_code)
                self.assertEqual(error.rate_scope, RateScope.CONNECTION)
                self.assert_safe(error)
                self.assertTrue(response.closed)
                self.assertEqual(len(opener.calls), 1)

    def test_429_cleanup_failure_keeps_trusted_metadata_without_exposing_private_error(self):
        reply = HTTPError("https://outside.invalid/" + PRIVATE, 429, PRIVATE, {"Retry-After": "1800"},
                          BytesIO(b'{"error":"ASPSP_RATE_LIMIT_EXCEEDED"}'))
        self.addCleanup(reply.close)
        reply.close = Mock(side_effect=OSError(PRIVATE))
        client, _ = self.client(reply)
        with self.assertRaises(BudgetAPIError) as caught:
            client.get(TRANSACTIONS)
        self.assertEqual(caught.exception.provider_code, "ASPSP_RATE_LIMIT_EXCEEDED")
        self.assertEqual(caught.exception.retry_at, 1_700_001_800)
        self.assert_safe(caught.exception)

    def test_non2xx_response_cleanup_failure_preserves_known_status_and_retry_deadline(self):
        class CleanupFailure(Response):
            def __exit__(self, *args):
                super().__exit__(*args)
                raise OSError(PRIVATE)

        for status, code in ((429, "rate_limit"), (404, "provider_error")):
            response = CleanupFailure(b'{"error":"ASPSP_RATE_LIMIT_EXCEEDED"}', status,
                                      {"Retry-After": "99999"})
            client, opener = self.client(response)
            with self.assertRaises(BudgetAPIError) as caught:
                client.get(TRANSACTIONS)
            self.assertEqual((caught.exception.status, caught.exception.code), (status, code))
            self.assertEqual(caught.exception.retry_at, 1_700_099_999 if status == 429 else None)
            self.assertTrue(response.closed)
            self.assertEqual(len(opener.calls), 1)
            self.assert_safe(caught.exception)

    def test_deadline_caps_transport_timeout_and_elapsed_deadline_never_opens_http(self):
        for deadline, timeout in ((100.25, 0.25), (1000, 60)):
            client, opener = self.client(Response())
            self.assertEqual(client.get(SESSION, deadline=deadline), {})
            self.assertEqual(opener.calls[0][1], {"timeout": timeout})
        for deadline in (100, 99):
            client, opener = self.client(Response())
            with self.assertRaises(BudgetAPIError) as caught:
                client.get(SESSION, deadline=deadline)
            self.assertEqual(caught.exception.code, "acquisition_timeout")
            self.assertTrue(caught.exception.retryable)
            self.assertEqual(opener.calls, [])

    def test_invalid_deadline_or_monotonic_clock_is_safe_before_http(self):
        for deadline in (True, -1, PRIVATE, [], float("nan"), float("inf"), 10 ** 1000):
            client, opener = self.client(Response())
            with self.assertRaises(BudgetAPIError) as caught:
                client.get(SESSION, deadline=deadline)
            self.assertEqual(caught.exception.code, "invalid_request")
            self.assertEqual(opener.calls, [])
            self.assert_safe(caught.exception)
        for value in (None, True, -1, PRIVATE, [], float("nan"), float("inf"), 10 ** 1000):
            client, opener = self.client(Response(), monotonic=lambda: value)
            with self.assertRaises(BudgetAPIError) as caught:
                client.get(SESSION, deadline=200)
            self.assertEqual(caught.exception.code, "invalid_configuration")
            self.assertEqual(opener.calls, [])
            self.assert_safe(caught.exception)
        client, opener = self.client(Response(), monotonic=Mock(side_effect=ValueError(PRIVATE)))
        with self.assertRaises(BudgetAPIError) as caught:
            client.get(SESSION, deadline=200)
        self.assertEqual(caught.exception.code, "invalid_configuration")
        self.assertEqual(opener.calls, [])
        self.assert_safe(caught.exception)

    def test_deadline_is_checked_again_after_signing_before_http(self):
        now = [100.0]
        client, opener = self.client(Response(), monotonic=lambda: now[0])
        def slow_sign(*args):
            now[0] = 102.0
            return b"synthetic signature"
        client._key = Mock()
        client._key.sign.side_effect = slow_sign
        with self.assertRaises(BudgetAPIError) as caught:
            client.get(SESSION, deadline=101)
        self.assertEqual(caught.exception.code, "acquisition_timeout")
        self.assertEqual(opener.calls, [])

    def test_late_open_or_transport_failure_never_publishes_data_and_closes_responses(self):
        for failure in (None, TimeoutError(PRIVATE), HTTPError("https://outside.invalid/" + PRIVATE, 403, PRIVATE, {}, BytesIO())):
            now = [100.0]
            response = Response()
            client, opener = self.client(monotonic=lambda: now[0])
            def slow_open(*args, **kwargs):
                now[0] = 102.0
                if failure is not None:
                    raise failure
                return response
            opener.open = Mock(side_effect=slow_open)
            with self.assertRaises(BudgetAPIError) as caught:
                client.get(SESSION, deadline=101)
            self.assertEqual(caught.exception.code, "acquisition_timeout")
            self.assert_safe(caught.exception)
            if failure is None:
                self.assertTrue(response.closed)
                self.assertEqual(response.read_sizes, [])
            elif isinstance(failure, HTTPError):
                self.assertTrue(failure.fp.closed)
            else:
                response.close()

    def test_slow_drip_read1_stops_between_chunks_without_using_filling_read(self):
        now = [100.0]
        response = Response(b'{"ok":true}')
        def drip(size):
            now[0] += 0.4
            return BytesIO.read(response, 1)
        response.read1 = Mock(side_effect=drip)
        response.read = Mock(side_effect=AssertionError("filling read must not be used"))
        client, opener = self.client(response, monotonic=lambda: now[0])
        with self.assertRaises(BudgetAPIError) as caught:
            client.get(SESSION, deadline=101)
        self.assertEqual(caught.exception.code, "acquisition_timeout")
        self.assertEqual(response.read1.call_count, 3)
        response.read.assert_not_called()
        self.assertTrue(response.closed)
        self.assertEqual(opener.calls[0][1], {"timeout": 1.0})

    def test_deadline_after_eof_or_read_error_or_json_parsing_does_not_return_late_data(self):
        for timing in ("eof", "read_error", "json"):
            now = [100.0]
            response = Response()
            original_read = response.read1
            original_json = json.loads
            def delayed_read(size):
                if timing == "read_error":
                    now[0] = 102.0
                    raise TimeoutError(PRIVATE)
                chunk = original_read(size)
                if not chunk:
                    now[0] = 102.0
                return chunk
            def delayed_json(*args, **kwargs):
                result = original_json(*args, **kwargs)
                now[0] = 102.0
                return result
            client, _ = self.client(response, monotonic=lambda: now[0])
            if timing != "json":
                response.read1 = delayed_read
            with patch.object(module.json, "loads", side_effect=delayed_json if timing == "json" else original_json):
                with self.assertRaises(BudgetAPIError) as caught:
                    client.get(SESSION, deadline=101)
            self.assertEqual(caught.exception.code, "acquisition_timeout")
            self.assertTrue(response.closed)
            self.assert_safe(caught.exception)

    def test_typed_synthetic_response_without_read1_uses_bounded_fallback_read(self):
        response = Response(b'{"ok":true}')
        response.read1 = None
        client, _ = self.client(response)
        self.assertEqual(client.get(SESSION, deadline=200), {"ok": True})
        self.assertTrue(response.read_sizes)
        self.assertTrue(all(size <= module.READ_CHUNK for size in response.read_sizes))
        self.assertTrue(response.closed)

    def test_deadline_during_429_body_preserves_received_server_hint_and_closes_both_paths(self):
        for raised in (False, True):
            now = [100.0]
            response = Response(b'{"error":"ASPSP_RATE_LIMIT_EXCEEDED"}', 429, {"Retry-After": "86400"})
            def late_read(size):
                now[0] = 102.0
                return BytesIO.read(response, size)
            response.read1 = late_read
            reply = HTTPError("https://outside.invalid/" + PRIVATE, 429, PRIVATE, response.headers, response) if raised else response
            client, opener = self.client(reply, monotonic=lambda: now[0])
            with self.assertRaises(BudgetAPIError) as caught:
                client.get(TRANSACTIONS, deadline=101)
            self.assertEqual((caught.exception.status, caught.exception.code), (429, "acquisition_timeout"))
            self.assertEqual(caught.exception.retry_at, 1_700_086_400)
            self.assertEqual(caught.exception.rate_scope, RateScope.CONNECTION)
            self.assertIsNone(caught.exception.provider_code)
            self.assertTrue(response.closed)
            self.assertEqual(len(opener.calls), 1)
            self.assert_safe(caught.exception)

    def test_late_429_body_and_cleanup_failure_cannot_erase_received_retry_deadline(self):
        now = [100.0]

        class LateCleanupFailure(Response):
            def read1(self, size):
                now[0] = 102.0
                return super().read1(size)

            def __exit__(self, *args):
                super().__exit__(*args)
                raise OSError(PRIVATE)

        response = LateCleanupFailure(b'{"error":"ASPSP_RATE_LIMIT_EXCEEDED"}', 429,
                                      {"Retry-After": "86400"})
        client, opener = self.client(response, monotonic=lambda: now[0])
        with self.assertRaises(BudgetAPIError) as caught:
            client.get(TRANSACTIONS, deadline=101)
        self.assertEqual((caught.exception.status, caught.exception.code), (429, "acquisition_timeout"))
        self.assertEqual(caught.exception.retry_at, 1_700_086_400)
        self.assertEqual(caught.exception.rate_scope, RateScope.CONNECTION)
        self.assertTrue(response.closed)
        self.assertEqual(len(opener.calls), 1)
        self.assert_safe(caught.exception)
