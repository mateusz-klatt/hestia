"""Read-only API security contract, exercised without keys or bank connections."""
from __future__ import annotations

import base64
from email.message import Message
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
    def __init__(self, body=b"{}", status=200):
        super().__init__(body)
        self.status = status
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


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

    def client(self, *responses, clock=None):
        opener = Opener(*responses)
        client = EnableBankingClient(APP_ID, self.key_pem, opener=opener,
                                     clock=clock if clock is not None else lambda: 1_700_000_000)
        return client, opener

    def assert_safe(self, exception):
        self.assertNotIn(PRIVATE, str(exception))
        self.assertNotIn(ACCOUNT, str(exception))
        self.assertNotIn(PRIVATE, "".join(traceback.format_exception(exception)))
        self.assertIn(exception.code, BudgetAPIError.CODES)

    def test_all_three_routes_are_gets_on_fixed_origin_without_psu_headers(self):
        client, opener = self.client(*(Response(b'{"ok":true}') for _ in range(3)))
        for path in (SESSION, BALANCES, TRANSACTIONS):
            self.assertEqual(client.get(path), {"ok": True})
        for (request, options), path in zip(opener.calls, (SESSION, BALANCES, TRANSACTIONS)):
            self.assertEqual(request.full_url, "https://api.enablebanking.com" + path)
            self.assertEqual(request.get_method(), "GET")
            self.assertIsNone(request.data)
            self.assertEqual(options, {"timeout": 30})
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

    def test_http_status_mapping_ignores_bodies_and_does_not_retry(self):
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
                    self.assertEqual(reply.read_sizes, [])

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
        self.assertEqual(response.read_sizes, [module.MAX_RESPONSE + 1])
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
