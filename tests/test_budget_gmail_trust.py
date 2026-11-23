"""Synthetic Gmail authentication and spouse-forward fixtures."""
import base64
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
import quopri
import unittest
from unittest.mock import Mock

from hestia.budget.gmail_address import canonical_mailbox
from hestia.budget.gmail_trust import classify_sender, normalize_sender_rules, unwrap_forward


MERCHANT = "billing@merchant.example"
SPOUSE = "spouse@gmail.com"
DIRECT = (MERCHANT, "@provider.example")
SPOUSES = (SPOUSE,)
KNOWN = ("merchant.example", "provider.example")
INVOICE = "Numer dokumentu: FV/2026/42\nKwota: 123,45 PLN\nTermin płatności: 2026-12-01\n"


def _headers(address=MERCHANT, dkim=None, other=None, *, authserv="mx.google.com"):
    domain = address.rpartition("@")[2]
    dkim = dkim if dkim is not None else f"dkim=pass header.d={domain}"
    other = other if other is not None else f"spf=pass smtp.mailfrom={address}"
    return [("From", f"Fixture Sender <{address}>"), ("Authentication-Results", f"{authserv}; {dkim}; {other}")]


def _classify(headers):
    return classify_sender(headers, DIRECT, SPOUSES)


def _message(sender=MERCHANT, body=INVOICE):
    message = EmailMessage()
    message["From"] = sender
    message.set_content(body)
    return message


def _forward(sender, original):
    outer = _message(sender, "Przesyłam fakturę.")
    outer.add_attachment(original)
    return outer


def _inline(marker="---------- Forwarded message ---------", author_field="From", sender=MERCHANT):
    return (f"Earlier text.\n{marker}\n\n{author_field}: Merchant <{sender}>\n"
            f"Message-ID: <fixture-42@merchant.example>\nSubject: Invoice\n\n{INVOICE}")


class GmailTrustTests(unittest.TestCase):
    def test_normalize_rules_and_merge_domains(self):
        self.assertEqual(canonical_mailbox("  Billing+Demo@Merchant.Example\t"), "billing+demo@merchant.example")
        for value in (None, "local", "á@merchant.example", "a@merchant.example\n", "a..b@merchant.example",
                      "a@provider..example", "a@" + "sub." * 64 + "example", "a" * 65 + "@merchant.example"):
            with self.subTest(value=value):
                self.assertIsNone(canonical_mailbox(value))
        direct, spouses, known = normalize_sender_rules(
            [" @Provider.Example ", "billing@Merchant.Example", "BILLING@MERCHANT.EXAMPLE", "vendor@gmail.com"],
            ["Spouse@Gmail.COM"], [" @Supplier.Example "])
        self.assertEqual(direct, ("@provider.example", "billing@merchant.example", "vendor@gmail.com"))
        self.assertEqual(spouses, SPOUSES)
        self.assertEqual(known, ("merchant.example", "provider.example", "supplier.example"))
        self.assertEqual(normalize_sender_rules([], SPOUSES, []), ((), SPOUSES, ()))
        self.assertEqual(normalize_sender_rules(DIRECT, [], []), (DIRECT[::-1], (), KNOWN))

    def test_invalid_rules_fail_closed(self):
        for direct, spouses, known in (
            ([], [], []), ("@merchant.example", [], []), ([False], [], []), (["@@merchant.example"], [], []),
            ([], ["@gmail.com"], []), ([], ["@provider.example"], []), (DIRECT, [], ["gmail.com"]),
            *(([f"@{domain}"], [], []) for domain in (
                "gmail.com", "icloud.com", "wp.pl", "onet.pl", "o2.pl", "interia.pl", "outlook.com", "yahoo.com")),
        ):
            with self.subTest(direct=direct, spouses=spouses, known=known):
                with self.assertRaisesRegex(ValueError, "^budget_receipt_gmail_sender_config_invalid$"):
                    normalize_sender_rules(direct, spouses, known)

    def test_direct_spouse_and_domain_rules(self):
        for address, expected in (
            (MERCHANT, "direct_allowlist"), ("Billing@Merchant.Example", "direct_allowlist"),
            ("a@provider.example", "direct_allowlist"), ("a@billing.provider.example", "direct_allowlist"),
            ("a@fakeprovider.example", "untrusted"), ("a@provider.example.untrusted.example", "untrusted"),
            ("other@merchant.example", "untrusted"), (SPOUSE, "spouse_forward"),
            ("spouse+other@gmail.com", "untrusted"),
        ):
            with self.subTest(address=address):
                self.assertEqual(_classify(_headers(address)), expected)
        self.assertEqual(classify_sender(_headers(SPOUSE), (SPOUSE,), SPOUSES), "spouse_forward")

    def test_spoofed_from_and_sender_mismatch(self):
        self.assertEqual(_classify(_headers() + [("Sender", "relay@merchant.example")]), "direct_allowlist")
        for headers in (
            _headers()[1:], _headers() + [("fRoM", MERCHANT)],
            [("From", f"{MERCHANT}, {SPOUSE}")] + _headers()[1:],
            [("From", "no-mailbox")] + _headers()[1:],
            _headers() + [("Sender", "relay@evil.example")], _headers() + [("Sender", "no-mailbox")],
            _headers() + [("Sender", MERCHANT)] * 2,
            _headers(dkim="dkim=pass header.d=relay.evil.example"),
            _headers(dkim="dkim=pass header.d=relay.merchant.example"),
            _headers(other="spf=pass smtp.mailfrom=sender@evil.example"),
            _headers(other="dmarc=pass header.from=merchant.example.evil.example"),
            _headers(other="spf=pass smtp.helo=merchant.example"),
        ):
            with self.subTest(headers=headers):
                self.assertEqual(_classify(headers), "untrusted")

    def test_only_topmost_google_auth_results_are_trusted(self):
        good = _headers()
        bad = _headers(authserv="relay.example")[-1]
        forged_lower = good[-1]
        self.assertEqual(_classify([good[0], bad, forged_lower]), "untrusted")
        self.assertEqual(_classify([good[0], _headers(dkim="dkim=fail")[-1], forged_lower]), "untrusted")
        self.assertEqual(_classify(good[:1]), "untrusted")
        for headers in ([("Received", "from fixture.example")] + good + [bad],
                        good + [("Received", "from fixture.example"), bad]):
            with self.subTest(headers=headers):
                self.assertEqual(_classify(headers), "direct_allowlist")

    def test_comments_and_quotes_keep_auth_results_delimited(self):
        self.assertEqual(_classify(_headers(
            dkim='DKIM/1=PASS header.d="Merchant.Example"',
            other="spf=fail; dmarc=pass (policy unknown (fixture \\) comment)) header.from=merchant.example",
            authserv="MX.GOOGLE.COM ")), "direct_allowlist")
        self.assertEqual(_classify(_headers(other="spf=pass smtp.mailfrom=merchant.example")), "direct_allowlist")
        for dkim in (
            'dkim=fail reason="x; dkim=pass header.d=merchant.example"',
            "dkim=fail (x; dkim=pass header.d=merchant.example)",
            "dkim=pass (header.d=merchant.example)",
            "dkim=paſſ header.d=merchant.example",
            "dkim=pass header.d=merchant.example (unclosed comment",
            "dkim=pass header.d=merchant.example )",
            'dkim=pass header.d="merchant.example',
            "dkim=pass header.d=merchant.example \\g",
            "dkim=pass header.d=merchant.example trailing-garbage",
            "dkim=pass header.d=merchant.example header.d=merchant.example",
            " " * (16 * 1024),
        ):
            with self.subTest(dkim=dkim[:100]):
                self.assertEqual(_classify(_headers(dkim=dkim)), "untrusted")

    def test_pass_without_header_d_correlates_actual_signatures(self):
        for evidence, signatures in (
            ("header.i=@merchant.example header.s=fixture header.b=abcd", (
                "v=1; d=merchant.example; b=ab\r\n cdEFGH; s=fixture;",
                "v=1; d=merchant.example; b=abcdEFGH; s=unrelated; i=@merchant.example",
            )),
            ("header.i=sender@merchant.example", (
                "v=1; d=merchant.example; i=sender@merchant.example; b=1234; s=fixture",)),
            ("header.b=abcd", ("d=merchant.example; b=abcdEFGH; s=fixture",)),
            ("header.s=fixture", ("d=merchant.example; b=abcdEFGH; s=fixture",)),
        ):
            with self.subTest(evidence=evidence, signatures=signatures):
                self.assertEqual(_classify(_headers(dkim=f"dkim=pass {evidence}")
                                           + [("DKIM-Signature", signature) for signature in signatures]),
                                 "direct_allowlist")

    def test_signature_correlation_fails_closed(self):
        matching = "d=merchant.example; b=abcdEFGH; s=fixture"
        for evidence, signatures in (
            ("", (matching,)), ("header.b=\"\" header.i=@merchant.example", (matching,)),
            ("header.i=@merchant.example", ()), ("header.i=@merchant.example", (matching, matching)),
            ("header.i=@merchant.example", ("d=evil.example; i=@merchant.example; b=abcdEFGH",)),
            ("header.i=other@merchant.example header.s=fixture header.b=abcd", (matching,)),
            ("header.s=fixture header.b=ABCD", (matching,)),
            ("header.s=fixture", ("d=bad domain; s=fixture",)),
            ("header.s=fixture", ("s=fixture",)),
            ("header.s=fixture", ("d=merchant.example; s=fixture; d=evil.example",)),
            ("header.s=fixture", ("d=merchant.example; s=fixture; malformed",)),
            ("header.d=evil.example header.i=@merchant.example", (matching,)),
        ):
            with self.subTest(evidence=evidence, signatures=signatures):
                self.assertEqual(_classify(_headers(dkim=f"dkim=pass {evidence}")
                                           + [("DKIM-Signature", value) for value in signatures]), "untrusted")

    def test_mime_unwrap_with_one_or_two_forward_levels(self):
        original = _message()
        original["Message-ID"] = "<synthetic@merchant.example>"
        root_rfc822 = EmailMessage()
        root_rfc822["From"] = SPOUSE
        root_rfc822.set_content(original)
        for outer in (root_rfc822, _forward(SPOUSE, original),
                      _forward(SPOUSE, _forward("relay@relay.example", original))):
            # Parse serialized fixture bytes as the Gmail import would.
            outer = BytesParser(policy=policy.default).parsebytes(outer.as_bytes())
            selected = unwrap_forward(outer, KNOWN)
            self.assertEqual(selected["Message-ID"], original["Message-ID"])
            self.assertEqual(selected.get_body().get_content(), INVOICE)
        self.assertIsNone(unwrap_forward(_forward(SPOUSE, _forward(
            "relay@relay.example", _forward("relay2@relay.example", original))), KNOWN))

    def test_rfc822_wrapper_and_inner_body_have_independent_transfer_encodings(self):
        prefix = b"From: spouse@gmail.com\r\nContent-Type: message/rfc822\r\n"
        for wrapper_cte in (b"", b"Content-Transfer-Encoding: 7bit\r\n",
                            b"Content-Transfer-Encoding: 8BIT\r\n", b"Content-Transfer-Encoding: binary\r\n"):
            for body_cte in ("base64", "quoted-printable"):
                with self.subTest(wrapper_cte=wrapper_cte, body_cte=body_cte):
                    original = _message()
                    original.set_content(INVOICE, cte=body_cte)
                    outer = BytesParser(policy=policy.default).parsebytes(prefix + wrapper_cte + b"\r\n"
                                                                           + original.as_bytes())
                    self.assertEqual(unwrap_forward(outer, KNOWN).get_content(), INVOICE)

    def test_malformed_rfc822_wrappers_fail_closed(self):
        prefix = b"From: spouse@gmail.com\r\nContent-Type: message/rfc822\r\n"
        original = _message().as_bytes(policy=policy.SMTP)
        for cte, data in (
            (b"Content-Transfer-Encoding: base64\r\n", base64.encodebytes(original)),
            (b"Content-Transfer-Encoding: quoted-printable\r\n", quopri.encodestring(original)),
            (b"Content-Transfer-Encoding: 8bit\r\nContent-Transfer-Encoding: 8bit\r\n", original),
            (b"Content-Transfer-Encoding: 8bit\r\n", b"missing original headers\r\n"),
        ):
            with self.subTest(cte=cte):
                outer = BytesParser(policy=policy.default).parsebytes(prefix + cte + b"\r\n" + data)
                self.assertIsNone(unwrap_forward(outer, KNOWN))
        empty = EmailMessage()
        empty["Content-Type"] = "message/rfc822"
        outer = _message(SPOUSE, _inline())
        outer.make_mixed()
        outer.attach(empty)
        self.assertIsNone(unwrap_forward(outer, KNOWN))

    def test_ambiguous_and_unknown_originals(self):
        outer = _forward(SPOUSE, _message())
        outer.add_attachment(_message("bills@provider.example"))
        self.assertIsNone(unwrap_forward(outer, KNOWN))
        unknown = _message("fake@notmerchant.example")
        self.assertIsNone(unwrap_forward(_forward(SPOUSE, unknown), KNOWN))
        self.assertIs(unwrap_forward(_forward(SPOUSE, unknown), KNOWN, require_known=False), unknown)
        self.assertIsNone(unwrap_forward(_forward(SPOUSE, EmailMessage()), KNOWN))
        direct_public = ("vendor@gmail.com",)
        public_merchant = _message("vendor@gmail.com")
        self.assertIs(unwrap_forward(_forward(SPOUSE, public_merchant), KNOWN, sender_allowlist=direct_public),
                      public_merchant)
        self.assertIsNone(unwrap_forward(_forward(SPOUSE, _message("another@gmail.com")), KNOWN,
                                         sender_allowlist=direct_public))

    def test_untrusted_forward_chain_is_never_unwrapped(self):
        merchant = _message()
        merchant["Authentication-Results"] = _headers()[-1][1]
        outer = _forward("attacker@evil.example", _forward(SPOUSE, merchant))
        outer["Authentication-Results"] = _headers("attacker@evil.example")[-1][1]
        self.assertEqual(_classify(outer.raw_items()), "untrusted")
        outer.replace_header("From", SPOUSE)  # Spoof the trusted spouse's From.
        self.assertEqual(_classify(outer.raw_items()), "untrusted")

    def test_inline_forward_from_bounded_english_and_polish_headers(self):
        for marker, sender_header in (("---------- Forwarded message ---------", "From"),
                                      ("Begin forwarded message:", "From"), ("Przekazana wiadomość", "Od")):
            with self.subTest(marker=marker):
                outer = _message(SPOUSE, _inline(marker, sender_header))
                original = unwrap_forward(outer, KNOWN)
                self.assertEqual(original["From"], MERCHANT)
                self.assertEqual(original["Message-ID"], "<fixture-42@merchant.example>")
                self.assertEqual(original.get_content().lstrip("\n"), INVOICE)
        original = unwrap_forward(_message(SPOUSE, f"Przekazana wiadomość:\nOd: {MERCHANT}\n{INVOICE}"), KNOWN)
        self.assertEqual(original.get_content(), INVOICE)

    def test_malformed_inline_headers_and_overlong_input_fail_closed(self):
        for text in (
            "No forward at all", "Begin forwarded message:\n", "Begin forwarded message:\n\nBody",
            f"Begin forwarded message:\nFrom: {MERCHANT}\nOd: {SPOUSE}\n\n{INVOICE}",
            f"Begin forwarded message:\nFrom: {MERCHANT}\nDate: " + "x" * 4096 + f"\n\n{INVOICE}",
            "Begin forwarded message:\n" + "\n" * 32 + f"From: {MERCHANT}\n{INVOICE}",
            _inline(sender="notmerchant@evil.example"), "x" * (1024 * 1024 + 1),
        ):
            with self.subTest(text=text[:100]):
                self.assertIsNone(unwrap_forward(_message(SPOUSE, text), KNOWN))
        invalid_charset = _message(SPOUSE, _inline())
        invalid_charset.set_param("charset", "not-a-real-charset")
        self.assertIsNone(unwrap_forward(invalid_charset, KNOWN))
        self.assertIsNone(unwrap_forward(EmailMessage(), KNOWN))

    def test_html_can_only_pass_through_isolated_renderer_interface(self):
        outer = EmailMessage()
        outer["From"] = SPOUSE
        outer.set_content("<html><body>Synthetic HTML</body></html>", subtype="html")
        self.assertIsNone(unwrap_forward(outer, KNOWN))
        renderer = Mock(return_value=_inline())
        self.assertEqual(unwrap_forward(outer, KNOWN, render_html=renderer)["From"], MERCHANT)
        renderer.assert_called_once_with(b"<html><body>Synthetic HTML</body></html>\n")
        renderer.reset_mock()
        outer.set_content("é" * (1024 * 1024 // 2 + 1), charset="iso-8859-1", subtype="html")
        self.assertIsNone(unwrap_forward(outer, KNOWN, render_html=renderer))
        renderer.assert_not_called()
