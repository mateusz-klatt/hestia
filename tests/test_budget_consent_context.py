"""Synthetic peer/proxy boundaries for foreground AIS consent context."""
from __future__ import annotations

import os
from types import SimpleNamespace
import unittest
from unittest import mock

from multidict import CIMultiDict

from hestia.budget import consent_context as module
from hestia.budget.config import BudgetConfigError


def request(peer, *headers):
    return SimpleNamespace(remote=peer, headers=CIMultiDict(headers))


class ConsentContextTests(unittest.TestCase):
    def test_login_source_uses_rightmost_untrusted_hop_even_with_bad_prefix_or_duplicates(self):
        policy = {module.TRUSTED_PROXY_ENV: "127.0.0.1/32,10.0.0.0/8"}
        for headers in ((("X-Forwarded-For", "bad, 192.0.2.1, 10.0.0.1"),),
                        (("X-Forwarded-For", "bad"), ("x-forwarded-for", "192.0.2.1, 10.0.0.1"))):
            self.assertEqual(module.login_ip(request("127.0.0.1", *headers), environ=policy), "192.0.2.1")
        self.assertEqual(module.login_ip(request("192.0.2.1", ("X-Forwarded-For", "bad")),
                                         environ=policy), "192.0.2.1")
        for peer, headers in ((None, ()), ("127.0.0.1", ()),
                              ("127.0.0.1", (("X-Forwarded-For", ""),)),
                              ("127.0.0.1", (("X-Forwarded-For", "192.0.2.1, bad"),)),
                              ("127.0.0.1", (("X-Forwarded-For", "192.0.2.1"), ("X-Forwarded-For", "bad"))),
                              ("127.0.0.1", (("X-Forwarded-For", "10.0.0.1"),)),
                              ("127.0.0.1", (("X-Forwarded-For", "x" * 4097),)),
                              ("127.0.0.1", (("X-Forwarded-For", "192.0.2.1\t"),)),
                              ("127.0.0.1", (("X-Forwarded-For", "ż"),)),
                              ("127.0.0.1", (("X-Forwarded-For", "192.0.2.1," * 32 + "192.0.2.1"),))):
            with self.subTest(peer=peer, headers=headers), self.assertRaises(ValueError):
                module.login_ip(request(peer, *headers), environ=policy)

    def context(self, peer, *headers, trusted=""):
        return module.psu_context(request(peer, *headers), environ={module.TRUSTED_PROXY_ENV: trusted})

    def test_direct_public_peer_ignores_every_forwarded_identity_header(self):
        result = self.context("8.8.8.8", ("X-Forwarded-For", "1.1.1.1"),
                              ("Forwarded", "for=9.9.9.9"), ("CF-Connecting-IP", "9.9.9.9"),
                              ("Referer", "https://bank.invalid/?code=synthetic"),
                              ("User-Agent", "Synthetic browser"), ("Accept-Language", "pl,en;q=0.5"))
        self.assertEqual(result.headers(), {"Psu-Ip-Address": "8.8.8.8",
                         "Psu-User-Agent": "Synthetic browser", "Psu-Accept-Language": "pl,en;q=0.5"})
        self.assertEqual(self.context("2606:4700:4700::1111").ip_address, "2606:4700:4700::1111")

    def test_unverified_nonpublic_peers_and_invalid_transport_have_no_ip(self):
        for peer in (None, "", "invalid", "127.0.0.1", "::1", "10.0.0.1", "192.168.1.1", "172.16.0.1",
                     "100.64.0.1", "169.254.1.1", "192.0.2.1", "0.0.0.0", "224.0.0.1", "ff02::1",
                     "fe80::1%eth0", "x" * 65, 123):
            with self.subTest(peer=peer):
                self.assertIsNone(self.context(peer, ("X-Forwarded-For", "8.8.8.8")).ip_address)

    def test_explicit_chain_walks_from_right_and_stops_at_first_untrusted_peer(self):
        policy = "127.0.0.0/8, 10.0.0.0/8\n2606:4700::/32"
        value = "9.9.9.9, 8.8.8.8, 2606:4700::1111, 10.1.2.3"
        self.assertEqual(self.context("127.0.0.1", ("X-Forwarded-For", value), trusted=policy).ip_address, "8.8.8.8")
        self.assertEqual(self.context("10.1.2.3", ("X-Forwarded-For", "2606:4700:4700::1111"),
                                      trusted="10.0.0.0/8").ip_address, "2606:4700:4700::1111")
        # Explicit trust can support a genuine private/LAN PSU; the immediate
        # proxy, not an arbitrary direct client, vouches for this chain.
        self.assertEqual(self.context("127.0.0.1", ("X-Forwarded-For", "192.168.1.9"),
                                      trusted="127.0.0.1/32").ip_address, "192.168.1.9")

    def test_missing_malformed_duplicate_or_all_trusted_chain_is_unavailable(self):
        policy = "127.0.0.0/8 10.0.0.0/8"
        for value in (None, "", " ", "unknown", "8.8.8.8:443", "[::1]", "8.8.8.8,,10.0.0.1",
                      "8.8.8.8, garbage", "8.8.8.8,", "fe80::1%eth0", "8.8.8.8\r\n", "٨.٨.٨.٨",
                      "10.0.0.1, 127.0.0.2", "8.8.8.8," * 32 + "8.8.8.8", "x" * 4097):
            with self.subTest(value=value):
                headers = () if value is None else (("X-Forwarded-For", value),)
                self.assertIsNone(self.context("127.0.0.1", *headers, trusted=policy).ip_address)
        duplicate = (("X-Forwarded-For", "8.8.8.8"), ("X-Forwarded-For", "9.9.9.9"))
        self.assertIsNone(self.context("127.0.0.1", *duplicate, trusted=policy).ip_address)

    def test_all_chain_elements_are_validated_before_accepting_rightmost_client(self):
        self.assertIsNone(self.context("127.0.0.1", ("X-Forwarded-For", "bad, 8.8.8.8"),
                                      trusted="127.0.0.1/32").ip_address)
        self.assertEqual(self.context("127.0.0.1", ("X-Forwarded-For", ", ".join(["8.8.8.8"] * 32)),
                                      trusted="127.0.0.1/32").ip_address, "8.8.8.8")

    def test_optional_foreground_headers_are_bounded_ascii_and_not_arbitrary_headers(self):
        for name, maximum, field in (("User-Agent", 1024, "user_agent"), ("Accept-Language", 256, "accept_language")):
            with self.subTest(name=name):
                self.assertEqual(getattr(self.context(None, (name, "x" * maximum)), field), "x" * maximum)
                for value in ("", " ", "x" * (maximum + 1), "browser\n", "browser\t", "browser\x7f", "ż"):
                    self.assertIsNone(getattr(self.context(None, (name, value)), field))
                self.assertIsNone(getattr(self.context(None, (name, "a"), (name.lower(), "b")), field))
        result = self.context(None, ("Accept", "text/html"), ("Psu-Ip-Address", "8.8.8.8"),
                              ("Psu-User-Agent", "spoofed"))
        self.assertEqual(result.headers(), {})

    def test_operator_configuration_is_strict_bounded_and_error_is_safe(self):
        for value in (None, False, "x" * 8193, "127.0.0.1", "10.0.0.1/8", "bad/24", "127.0.0.1/33",
                      "fe80::%eth0/64", "ż/32", "127.0.0.1/32," * 129):
            with self.subTest(value=value):
                with self.assertRaises(BudgetConfigError) as raised:
                    self.context("8.8.8.8", trusted=value)
                self.assertEqual(str(raised.exception), "Budget trusted proxy configuration is invalid")
        for value in ("", " , \n ", ",".join(["127.0.0.1/32"] * 128)):
            self.assertEqual(self.context("8.8.8.8", trusted=value).ip_address, "8.8.8.8")

    def test_default_environment_is_read_without_mutation_or_other_header_sources(self):
        with mock.patch.dict(os.environ, {module.TRUSTED_PROXY_ENV: "127.0.0.1/32"}):
            result = module.psu_context(request("127.0.0.1", ("X-Forwarded-For", "8.8.8.8")))
        self.assertEqual(result.ip_address, "8.8.8.8")
        self.assertEqual(module.psu_context(request("8.8.8.8"), environ={}).ip_address, "8.8.8.8")


if __name__ == "__main__":
    unittest.main()
