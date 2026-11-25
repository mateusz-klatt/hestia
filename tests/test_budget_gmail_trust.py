"""Synthetic Gmail authentication and spouse-forward fixtures."""
import base64
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
import quopri
import unittest
from unittest.mock import Mock

from hestia.budget.gmail_address import canonical_mailbox
from hestia.budget.gmail_trust import (
    PaymentSender, classify_sender, forward_sender_domain, inline_forward_body, normalize_payment_senders,
    normalize_sender_rules, payment_sender_for, unwrap_trusted_forward,
)


MERCHANT = "billing@merchant.example"
SPOUSE = "spouse@gmail.com"
DIRECT = (MERCHANT, "@provider.example")
SPOUSES = (SPOUSE,)
INVOICE = "Orange\nNumer dokumentu: FV/2026/11/42\nKwota: 123,45 PLN\nTermin płatności: 2026-12-01\n"


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
    def test_normalize_rules_without_deriving_domains_from_mailboxes(self):
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
        self.assertEqual(known, ("provider.example", "supplier.example"))
        self.assertEqual(normalize_sender_rules([], SPOUSES, []), ((), SPOUSES, ()))
        self.assertEqual(normalize_sender_rules(DIRECT, [], []), (DIRECT[::-1], (), ("provider.example",)))

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

    def test_payment_merchant_must_contain_a_nonblank_character(self):
        entry = {"sender": "@pay.example.test", "merchant": "   "}
        with self.assertRaisesRegex(ValueError, "^budget_receipt_gmail_sender_config_invalid$"):
            normalize_payment_senders([entry])
        self.assertEqual(normalize_payment_senders([entry | {"merchant": "Example Pay"}]),
                         (PaymentSender("@pay.example.test", "Example Pay"),))

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

    def test_payment_rules_require_aligned_auth_and_match_direct_only(self):
        payments = (PaymentSender("@pay.example.test", "Example Pay", True),
                    PaymentSender("bill@shop.example.test", "Example Shop"))
        gateway = "bill@sub.pay.example.test"
        relaxed = _headers(gateway, dkim="dkim=pass header.d=pay.example.test",
                           other="spf=pass smtp.mailfrom=pay.example.test")
        for headers, expected in (
            (relaxed, "direct_allowlist"),
            (_headers("bill@shop.example.test"), "direct_allowlist"),
            (_headers("other@shop.example.test"), "untrusted"),
            (_headers("bill@notpay.example.test"), "untrusted"),
            (_headers(gateway, dkim="dkim=fail"), "untrusted"),
            (_headers(gateway, other="dmarc=pass header.from=pay.example.test"), "untrusted"),
            (_headers(gateway, other="dmarc=pass header.from=sub.pay.example.test"), "direct_allowlist"),
        ):
            with self.subTest(expected=expected, headers=headers):
                self.assertEqual(classify_sender(headers, DIRECT, SPOUSES, payment_senders=payments), expected)
        self.assertEqual(classify_sender(relaxed, DIRECT, (gateway,), payment_senders=payments), "untrusted")
        self.assertEqual(classify_sender(relaxed, DIRECT, (), gateway, payment_senders=payments), "untrusted")
        self.assertEqual(classify_sender(_headers(gateway), DIRECT, (gateway,), payment_senders=payments),
                         "spouse_forward")
        self.assertEqual(classify_sender(_headers(gateway), DIRECT, (), gateway, payment_senders=payments),
                         "owner_forward")
        self.assertEqual(classify_sender(relaxed, (gateway,), (), gateway), "owner_forward")

    def test_specific_payment_sender_wins(self):
        generic = PaymentSender("@pay.example.test", "Example Pay", True)
        subdomain = PaymentSender("@sub.pay.example.test", "Example Branch")
        exact = PaymentSender("bill@sub.pay.example.test", "Example Shop")
        payments = (generic, exact, subdomain)
        for headers, expected in (
            (_headers("Bill@Sub.Pay.Example.Test"), exact),
            (_headers("another@sub.pay.example.test"), subdomain),
            (_headers("bill@alternate.pay.example.test"), generic),
            (_headers("bill@other.example.test"), None),
            (_headers("bill@pay.example.test") + [("from", exact.sender)], None),
            ([("From", "no-address")], None),
            ([], None),
        ):
            with self.subTest(headers=headers):
                self.assertEqual(payment_sender_for(headers, payments), expected)

    def test_forward_sender_domain_requires_a_single_author(self):
        self.assertEqual(forward_sender_domain(_message("bill@shop.example.test")), "shop.example.test")
        self.assertIsNone(forward_sender_domain(EmailMessage()))

    def test_allowlisted_senders_accept_parent_dkim_and_spf_domains(self):
        accepted = (
            ("twoja_f@ktura.t-mobile.pl", "@t-mobile.pl", "t-mobile.pl", "twoja_f@ktura.t-mobile.pl"),
            ("e-faktura@pl.orange.com", "@orange.com", "orange.com", "bounce@orange.com"),
        )
        for address, rule, signing, mailfrom in accepted:
            with self.subTest(address=address, signing=signing):
                headers = _headers(address, dkim=f"dkim=pass header.d={signing}",
                                   other=f"spf=pass smtp.mailfrom={mailfrom}")
                self.assertEqual(classify_sender(headers, (rule,), ()), "direct_allowlist")
        rejected = (
            ("billing@evil-t-mobile.pl", "billing@evil-t-mobile.pl", "t-mobile.pl"),
            ("billing@t-mobile.pl.evil.com", "@t-mobile.pl.evil.com", "t-mobile.pl"),
            ("billing@shop.pl", "@shop.pl", "pl"),
        )
        for address, rule, signing in rejected:
            with self.subTest(address=address, signing=signing):
                headers = _headers(address, dkim=f"dkim=pass header.d={signing}",
                                   other=f"spf=pass smtp.mailfrom={address}")
                self.assertEqual(classify_sender(headers, (rule,), ()), "untrusted")
        spouse = "forwarder@ktura.t-mobile.pl"
        strict = _headers(spouse, dkim="dkim=pass header.d=t-mobile.pl",
                          other=f"spf=pass smtp.mailfrom={spouse}")
        self.assertEqual(classify_sender(strict, ("billing@merchant.example",), (spouse,)), "untrusted")
        dmarc_parent = _headers(
            "twoja_f@ktura.t-mobile.pl", dkim="dkim=pass header.d=evil.example",
            other="dmarc=pass header.from=t-mobile.pl")
        self.assertEqual(classify_sender(dmarc_parent, ("@t-mobile.pl",), ()), "untrusted")

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
        exact_senders = (MERCHANT, "relay@provider.example", "relay2@provider.example")
        for outer in (root_rfc822, _forward(SPOUSE, original),
                      _forward(SPOUSE, _forward("relay@provider.example", original))):
            # Parse serialized fixture bytes as the Gmail import would.
            outer = BytesParser(policy=policy.default).parsebytes(outer.as_bytes())
            selected, manual = unwrap_trusted_forward(outer, sender_allowlist=exact_senders)
            self.assertFalse(manual)
            self.assertEqual(selected["Message-ID"], original["Message-ID"])
            self.assertEqual(selected.get_body().get_content(), INVOICE)
        self.assertIsNone(unwrap_trusted_forward(_forward(SPOUSE, _forward(
            "relay@provider.example", _forward("relay2@provider.example", original))), sender_allowlist=exact_senders))
        self.assertEqual(unwrap_trusted_forward(_forward(SPOUSE, _forward("relay@evil.example", original)),
                                                sender_allowlist=exact_senders), (original, True))

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
                    original, manual = unwrap_trusted_forward(outer, sender_allowlist=DIRECT)
                    self.assertFalse(manual)
                    self.assertEqual(original.get_content(), INVOICE)

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
                self.assertIsNone(unwrap_trusted_forward(outer))
        empty = EmailMessage()
        empty["Content-Type"] = "message/rfc822"
        outer = _message(SPOUSE, _inline())
        outer.make_mixed()
        outer.attach(empty)
        self.assertIsNone(unwrap_trusted_forward(outer))

    def test_ambiguous_and_unknown_originals(self):
        outer = _forward(SPOUSE, _message())
        outer.add_attachment(_message("bills@provider.example"))
        self.assertIsNone(unwrap_trusted_forward(outer))
        unknown = _message("fake@notmerchant.example")
        self.assertEqual(unwrap_trusted_forward(_forward(SPOUSE, unknown), sender_allowlist=DIRECT), (unknown, True))
        self.assertIsNone(unwrap_trusted_forward(_forward(SPOUSE, EmailMessage())))
        direct_public = ("vendor@gmail.com",)
        public_merchant = _message("vendor@gmail.com")
        self.assertEqual(unwrap_trusted_forward(_forward(SPOUSE, public_merchant), sender_allowlist=direct_public),
                         (public_merchant, False))
        other = _message("another@gmail.com")
        self.assertEqual(unwrap_trusted_forward(_forward(SPOUSE, other), sender_allowlist=direct_public),
                         (other, True))

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
                                      ("---------- Wiadomość przekazana dalej ----------", "Od"),
                                      ("Begin forwarded message:", "From"), ("Przekazana wiadomość", "Od")):
            with self.subTest(marker=marker):
                outer = _message(SPOUSE, _inline(marker, sender_header))
                original, manual = unwrap_trusted_forward(outer, sender_allowlist=DIRECT)
                self.assertFalse(manual)
                self.assertEqual(original["From"], MERCHANT)
                self.assertEqual(original["Subject"], "Invoice")
                self.assertEqual(original["Message-ID"], "<fixture-42@merchant.example>")
                self.assertEqual(original.get_content().lstrip("\n"), INVOICE)
        polish, manual = unwrap_trusted_forward(
            _message(SPOUSE, f"Przekazana wiadomość:\nOd: {MERCHANT}\nTemat: Faktura 42\n\n{INVOICE}"),
            sender_allowlist=DIRECT,
        )
        self.assertFalse(manual)
        self.assertEqual((polish["From"], polish["Subject"]), (MERCHANT, "Faktura 42"))
        original, manual = unwrap_trusted_forward(
            _message(SPOUSE, f"Przekazana wiadomość:\nOd: {MERCHANT}\n{INVOICE}"), sender_allowlist=DIRECT)
        self.assertFalse(manual)
        self.assertIsNone(original["Subject"])
        self.assertEqual(original.get_content(), INVOICE)

    def test_malformed_inline_headers_and_overlong_input_fail_closed(self):
        for text in (
            "No forward at all", "Begin forwarded message:\n", "Begin forwarded message:\n\nBody",
            f"Begin forwarded message:\nFrom: {MERCHANT}\nOd: {SPOUSE}\n\n{INVOICE}",
            f"Begin forwarded message:\nFrom: {MERCHANT}\nSubject: A\nTemat: B\n\n{INVOICE}",
            f"Begin forwarded message:\nFrom: {MERCHANT}\nDate: " + "x" * 4096 + f"\n\n{INVOICE}",
            "Begin forwarded message:\n" + "\n" * 32 + f"From: {MERCHANT}\n{INVOICE}",
            _inline() + _inline(), "x" * (1024 * 1024 + 1),
        ):
            with self.subTest(text=text[:100]):
                self.assertIsNone(unwrap_trusted_forward(_message(SPOUSE, text), sender_allowlist=DIRECT))
        invalid_charset = _message(SPOUSE, _inline())
        invalid_charset.set_param("charset", "not-a-real-charset")
        self.assertIsNone(unwrap_trusted_forward(invalid_charset))
        self.assertIsNone(unwrap_trusted_forward(EmailMessage()))

    def test_html_can_only_pass_through_isolated_renderer_interface(self):
        outer = EmailMessage()
        outer["From"] = SPOUSE
        outer.set_content("<html><body>Synthetic HTML</body></html>", subtype="html")
        self.assertIsNone(unwrap_trusted_forward(outer))
        renderer = Mock(return_value=_inline())
        original, manual = unwrap_trusted_forward(outer, sender_allowlist=DIRECT, render_html=renderer)
        self.assertFalse(manual)
        self.assertEqual(original["From"], MERCHANT)
        renderer.assert_called_once_with(b"<html><body>Synthetic HTML</body></html>\n")
        renderer.reset_mock()
        outer.set_content(" \n", subtype="html")
        self.assertIsNone(unwrap_trusted_forward(outer, sender_allowlist=DIRECT, render_html=renderer))
        renderer.assert_not_called()

        outer.set_content("é" * (1024 * 1024 // 2 + 1), charset="iso-8859-1", subtype="html")
        self.assertIsNone(unwrap_trusted_forward(outer, render_html=renderer))
        renderer.assert_not_called()

    def test_inline_and_attached_originals_at_the_same_level_are_ambiguous(self):
        outer = _message(SPOUSE, _inline())
        outer.add_attachment(_message())
        self.assertIsNone(unwrap_trusted_forward(outer, sender_allowlist=DIRECT))

    def test_alternative_inline_bodies_must_agree_on_the_original(self):
        outer = _message(SPOUSE, _inline())
        outer.add_alternative("<html>Synthetic fixture</html>", subtype="html")
        original, manual = unwrap_trusted_forward(outer, sender_allowlist=DIRECT,
                                                  render_html=lambda _payload: _inline())
        self.assertFalse(manual)
        self.assertEqual(original["From"], MERCHANT)
        self.assertIsNone(unwrap_trusted_forward(outer, sender_allowlist=DIRECT,
                                                 render_html=lambda _payload: _inline(sender="other@merchant.example")))

    def test_inline_forward_body_cuts_after_marker_and_headers_only_when_present(self):
        self.assertEqual(
            inline_forward_body("Zapłacono: 999 PLN\n" + _inline("---------- Wiadomość przekazana dalej ----------", "Od")),
            f"\n{INVOICE.rstrip()}",
        )
        self.assertEqual(
            inline_forward_body("Note: 999 PLN\n---------- Forwarded message ---------\nRazem: 10 PLN"),
            "Razem: 10 PLN",
        )
        self.assertEqual(inline_forward_body("Razem: 10 PLN"), "Razem: 10 PLN")
        self.assertEqual(inline_forward_body("Note: 999 PLN\n" + _inline() + _inline()), "")
