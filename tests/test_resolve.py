"""Unit tests for the DoH cloud resolver — fully mocked, no real network."""
from __future__ import annotations

import json
import unittest
from unittest import mock

from hestia import resolve


class FakeResp:
    """Stand-in for an `urlopen` context manager whose body `json.load` reads."""

    def __init__(self, payload, raw=None):
        self._raw = raw if raw is not None else json.dumps(payload).encode()

    def read(self, *a):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


SEED = "1.2.3.4"
GLOBAL_IP = "8.8.4.4"            # a genuinely global address (is_global → True)


def _answer(*ips, extra=()):
    ans = [{"type": 1, "data": ip} for ip in ips] + list(extra)
    return FakeResp({"Answer": ans})


class IsGlobalTests(unittest.TestCase):
    def test_global_private_and_garbage(self):
        self.assertTrue(resolve._is_global(GLOBAL_IP))
        self.assertFalse(resolve._is_global("192.0.2.1"))   # Pi-hole poison
        self.assertFalse(resolve._is_global("not-an-ip"))     # ValueError → False


class ServesTests(unittest.TestCase):
    @mock.patch("hestia.resolve.socket.create_connection")
    def test_serves_true(self, cc):
        self.assertTrue(resolve._serves("1.2.3.4", 8925, 1.0))

    @mock.patch("hestia.resolve.socket.create_connection", side_effect=OSError)
    def test_serves_false(self, cc):
        self.assertFalse(resolve._serves("1.2.3.4", 8925, 1.0))


class ResolveCloudIpTests(unittest.TestCase):
    @mock.patch("hestia.resolve.socket.create_connection")          # serves → ok
    @mock.patch("hestia.resolve.urllib.request.urlopen")
    def test_returns_global_serving_ip_and_ignores_cname(self, urlopen, cc):
        # a CNAME (type 5) is ignored; the global A record that serves :8925 wins.
        urlopen.return_value = _answer(GLOBAL_IP, extra=[{"type": 5, "data": "x.cdn"}])
        out = resolve.resolve_cloud_ip("gw.keemple.com", SEED)
        self.assertEqual(out, GLOBAL_IP)

    @mock.patch("hestia.resolve.socket.create_connection")
    @mock.patch("hestia.resolve.urllib.request.urlopen")
    def test_rejects_non_global_then_seed(self, urlopen, cc):
        urlopen.return_value = _answer("192.0.2.1")               # loop-guard rejects
        self.assertEqual(resolve.resolve_cloud_ip("gw", SEED), SEED)
        cc.assert_not_called()                                      # never even probes a private IP

    @mock.patch("hestia.resolve.socket.create_connection", side_effect=OSError)
    @mock.patch("hestia.resolve.urllib.request.urlopen")
    def test_global_but_not_serving_falls_back(self, urlopen, cc):
        urlopen.return_value = _answer(GLOBAL_IP)                   # global but :8925 closed
        self.assertEqual(resolve.resolve_cloud_ip("gw", SEED), SEED)

    @mock.patch("hestia.resolve.socket.create_connection")
    @mock.patch("hestia.resolve.urllib.request.urlopen")
    def test_first_endpoint_errors_second_succeeds(self, urlopen, cc):
        urlopen.side_effect = [OSError("boom"), _answer(GLOBAL_IP)]  # endpoint 1 fails → endpoint 2
        self.assertEqual(resolve.resolve_cloud_ip("gw", SEED), GLOBAL_IP)

    @mock.patch("hestia.resolve.urllib.request.urlopen")
    def test_bad_json_falls_back_to_seed(self, urlopen):
        urlopen.return_value = FakeResp(None, raw=b"<html>not json</html>")
        self.assertEqual(resolve.resolve_cloud_ip("gw", SEED), SEED)

    @mock.patch("hestia.resolve.urllib.request.urlopen", side_effect=OSError)
    def test_all_endpoints_fail_returns_seed(self, urlopen):
        # both DoH endpoints error → seed (global); urlopen called once per endpoint.
        self.assertEqual(resolve.resolve_cloud_ip("gw", SEED), SEED)
        self.assertEqual(urlopen.call_count, len(resolve.DOH_ENDPOINTS))

    @mock.patch("hestia.resolve.urllib.request.urlopen", side_effect=OSError)
    def test_non_global_seed_still_returned(self, urlopen):
        # a misconfigured private seed is honoured (warned, never crashes startup).
        self.assertEqual(resolve.resolve_cloud_ip("gw", "192.0.2.1"), "192.0.2.1")

    @mock.patch("hestia.resolve.socket.create_connection")
    @mock.patch("hestia.resolve.urllib.request.urlopen")
    def test_candidate_cap(self, urlopen, cc):
        # more global-but-non-serving A records than the cap → still falls back to seed,
        # and probes at most MAX_CANDIDATES of them.
        many = ["8.8.4.4"] * (resolve.MAX_CANDIDATES + 5)
        urlopen.side_effect = [_answer(*many), _answer(*many)]    # one response per endpoint
        cc.side_effect = OSError                                  # none serve
        self.assertEqual(resolve.resolve_cloud_ip("gw", SEED), SEED)
        self.assertLessEqual(cc.call_count, resolve.MAX_CANDIDATES * len(resolve.DOH_ENDPOINTS))


class DohParsingTests(unittest.TestCase):
    @mock.patch("hestia.resolve.urllib.request.urlopen")
    def test_malformed_answer_entries_dropped(self, urlopen):
        urlopen.return_value = FakeResp({"Answer": [
            "not-a-dict",                      # non-dict entry
            {"type": 1},                       # missing data
            {"type": 1, "data": 12345},        # non-str data
            {"type": 5, "data": "cname.x"},    # CNAME (type != 1)
        ]})
        self.assertEqual(resolve._doh_a_records("https://x/resolve", "gw", 1.0), [])

    @mock.patch("hestia.resolve.urllib.request.urlopen")
    def test_null_answer_yields_empty(self, urlopen):
        urlopen.return_value = FakeResp({"Answer": None})
        self.assertEqual(resolve._doh_a_records("https://x/resolve", "gw", 1.0), [])

    @mock.patch("hestia.resolve.urllib.request.urlopen")
    def test_top_level_non_dict_yields_empty(self, urlopen):
        urlopen.return_value = FakeResp(["unexpected", "list"])
        self.assertEqual(resolve._doh_a_records("https://x/resolve", "gw", 1.0), [])


if __name__ == "__main__":
    unittest.main()
