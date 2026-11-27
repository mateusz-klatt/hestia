"""Fetch bounded Gmail content after sender verification and emit structured provenance."""
from __future__ import annotations

import base64
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from email import policy
from email.errors import MessageError
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
import logging
from itertools import chain
import re
from zoneinfo import ZoneInfo

from .gmail_address import canonical_mailbox
from .gmail_apple import apple_document_number, inspect_apple_subscription_items
from .gmail_client import GmailError, has_spam_or_trash_label
from .gmail_document import ImportedDocument, extract_document
from .gmail_invoice import _FORWARDED_INVOICE_NUMBERS, _normalized_invoice_lines, extract_invoice
from .gmail_payment import _payee_key, confirms_order_cancellation, inspect_payment, purchase_payment_scope
from .gmail_respect import settlement_items
from .gmail_shop_items import inspect_shop_items
from .gmail_store import (DOCUMENT_NUMBER_DOMAIN, DOCUMENT_RECEIPT_DOMAIN, GATEWAY_REF_DOMAIN, ORDER_REFERENCE_DOMAIN, PAYMENT_PAYEE_DOMAIN, PAYMENT_SENDER_DOMAIN,
                          RFC_MESSAGE_DOMAIN, PendingMessage, PendingPayment, digest, normalize_parse_failed_reason)
from .gmail_trust import (_FORWARD_MARKERS, _INVALID_FORWARD, _forward_sender, _forwarded_original, _inline_forward,
                          _is_forward_marker, classify_sender, domain_matches, forward_sender_domain, inline_forward_body,
                          mime_parts, payment_sender_for, sender_address)
from .gmail_worker import MAX_HTML_BYTES, MAX_PDF_BYTES, PDFUnsupported, WorkerRejected, WorkerUnavailable


MAX_MESSAGE_BYTES = 10 * 1024 * 1024
_MAX_BASE64_CHARS = 4 * ((MAX_MESSAGE_BYTES + 2) // 3)
_INTERNAL_DATE = re.compile(r"[0-9]{1,19}\Z", re.ASCII)
_WARSAW = ZoneInfo("Europe/Warsaw")
_POLISH_FORWARD_DATE = re.compile(
    r"(?:pon|wt|śr|czw|pt|sob|niedz)\., ([0-9]{1,2}) "
    r"(sty|lut|mar|kwi|maj|cze|lip|sie|wrz|paź|lis|gru) ([0-9]{4}) o "
    r"([01]?[0-9]|2[0-3]):([0-5][0-9])",
    re.IGNORECASE,
)
_ENGLISH_FORWARD_DATE_24H = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), ([0-9]{1,2}) "
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) ([0-9]{4}) at "
    r"([01]?[0-9]|2[0-3]):([0-5][0-9])",
    re.IGNORECASE,
)
_ENGLISH_FORWARD_DATE_12H = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"([0-9]{1,2}), ([0-9]{4}) at (1[0-2]|[1-9]):([0-5][0-9]) ([AP]M)",
    re.IGNORECASE,
)
_POLISH_FORWARD_MONTHS = {
    month: number for number, month in enumerate("sty lut mar kwi maj cze lip sie wrz paź lis gru".split(), 1)
}
_ENGLISH_FORWARD_MONTHS = {
    month: number for number, month in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)
}
_ISSUER_SENDER_DOMAINS = {
    "T-Mobile": ("t-mobile.pl",),
    "Orange": ("orange.pl", "orange.com"),
    "Play": ("play.pl", "mojefinanseplay.pl"),
    "Respect Energy": ("respectenergy.pl",),
}
_PARSE_FAILED = "parse_failed"
log = logging.getLogger("hestia.budget.gmail_message")


class _PartOversized(WorkerRejected):
    """The selected MIME data exceeded its worker input limit."""


class _Rejected(WorkerRejected):
    """Local MIME or charset rejection carrying a fixed diagnostic reason."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class MessageParserUnavailable(GmailError):
    """A failed message exchange and the supervisor's response to a health probe."""

    def __init__(self, failed_message: PendingMessage, *, worker_ready: bool):
        super().__init__("parser_unavailable")
        self.failed_message = failed_message
        self.worker_ready = worker_ready


def _worker_reason(error):
    if isinstance(error, PDFUnsupported):
        return "pdf_unsupported"
    if isinstance(error, _Rejected):
        return error.reason
    return "worker_rejected"


def _parse_failed(pending, reason):
    return replace(pending, outcome=_PARSE_FAILED, parse_failed_reason=normalize_parse_failed_reason(reason))


def _metadata(response, message_id):
    internal_date = response.get("internalDate")
    size = response.get("sizeEstimate")
    if (response.get("id") != message_id or not isinstance(internal_date, str)
            or not _INTERNAL_DATE.fullmatch(internal_date) or type(size) is not int or size < 0):
        raise GmailError("invalid_response")
    return int(internal_date) / 1000, size


def _metadata_headers(response):
    payload = response.get("payload")
    headers = payload.get("headers") if isinstance(payload, dict) else None
    if not isinstance(headers, list):
        raise GmailError("invalid_response")
    pairs = []
    for header in headers:
        if (not isinstance(header, dict) or not isinstance(header.get("name"), str)
                or not isinstance(header.get("value"), str)):
            raise GmailError("invalid_response")
        pairs.append((header["name"], header["value"]))
    return pairs


def _raw_bytes(response, message_id):
    encoded = response.get("raw")
    if response.get("id") != message_id or not isinstance(encoded, str) or not encoded:
        raise GmailError("invalid_response")
    if len(encoded) > _MAX_BASE64_CHARS:
        raise _PartOversized
    try:
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    except ValueError:
        raise GmailError("invalid_response") from None
    if len(raw) > MAX_MESSAGE_BYTES:
        raise _PartOversized
    return raw


def _rfc_message_hmac(message, secret):
    headers = [value.strip() for name, value in message.raw_items() if name.lower() == "message-id"]
    if len(headers) != 1 or not (headers[0].startswith("<") and headers[0].endswith(">")):
        return None
    value = headers[0][1:-1]
    normalized = canonical_mailbox(value)
    if not normalized or value.strip() != value:
        return None
    # Domains compare case-insensitively; preserve the Message-ID local part.
    local = value.rpartition("@")[0]
    domain = normalized.rpartition("@")[2]
    return digest(secret, RFC_MESSAGE_DOMAIN, f"{local}@{domain}")


def _is_pdf_part(part):
    content_type = part.get_content_type()
    if content_type == "application/pdf":
        return True
    if content_type != "application/octet-stream":
        return False
    filename = part.get_filename()
    if not filename or not filename.lower().endswith(".pdf"):
        return False
    return (part.get_payload(decode=True) or b"").startswith(b"%PDF-")


def _body_parts(message):
    for subtype, kind in (("plain", "text"), ("html", "html")):
        part = message.get_body((subtype,))
        if part is not None:
            yield kind, part


def _document_parts(message):
    yield from _body_parts(message)
    for part in mime_parts(message):
        if _is_pdf_part(part):
            yield "pdf", part


def _document_payload(kind, part, *, decoded=None):
    limit = MAX_PDF_BYTES if kind == "pdf" else MAX_HTML_BYTES
    payload = (part.get_payload(decode=True) or b"") if decoded is None else decoded
    if part.defects:
        raise _Rejected("part_defects")
    if len(payload) > limit:
        raise _PartOversized
    if kind != "pdf":
        try:
            payload = payload.decode(part.get_content_charset() or "utf-8", errors="replace").encode("utf-8")
        except (LookupError, UnicodeError):
            raise _Rejected("charset_error") from None
        if len(payload) > limit:
            raise _PartOversized
    return payload


def _has_rfc822_attachment(message):
    return any(part.get_content_type() == "message/rfc822" for part in mime_parts(message))


def _parse_forward_date(value):
    value = value.replace("\u202f", " ").replace("\u00a0", " ")
    if match := _POLISH_FORWARD_DATE.fullmatch(value):
        day, month, year, hour, minute = match.groups()
        return datetime(int(year), _POLISH_FORWARD_MONTHS[month.casefold()], int(day),
                        int(hour), int(minute), tzinfo=_WARSAW)
    if match := _ENGLISH_FORWARD_DATE_24H.fullmatch(value):
        day, month, year, hour, minute = match.groups()
        return datetime(int(year), _ENGLISH_FORWARD_MONTHS[month.casefold()], int(day),
                        int(hour), int(minute), tzinfo=_WARSAW)
    if match := _ENGLISH_FORWARD_DATE_12H.fullmatch(value):
        month, day, year, hour, minute, meridiem = match.groups()
        hour = int(hour) % 12 + (12 if meridiem.upper() == "PM" else 0)
        return datetime(int(year), _ENGLISH_FORWARD_MONTHS[month.casefold()], int(day),
                        hour, int(minute), tzinfo=_WARSAW)
    return parsedate_to_datetime(value)


def _payment_receipt_date(message, pending):
    try:
        outer = datetime.fromtimestamp(pending.internal_date, UTC)
        fallback = outer.astimezone(_WARSAW).date()
    except (OverflowError, OSError, ValueError):
        return None
    if pending.trust_path not in ("spouse_forward", "owner_forward"):
        return fallback
    headers = message.get_all("Date", ())
    if len(headers) != 1:
        return fallback
    try:
        inner = _parse_forward_date(str(headers[0]))
        if inner.tzinfo is None:
            return fallback
        inner = inner.astimezone(UTC)
        if inner < outer - timedelta(days=400) or inner > outer + timedelta(days=3):
            return fallback
        return inner.astimezone(_WARSAW).date()
    except (TypeError, ValueError, OverflowError, OSError):
        return fallback


class GmailMessageProcessor:
    def __init__(self, settings, worker, *, discover_documents=False):
        self.require_auth = getattr(settings, "require_sender_authentication", True)
        self.discover_documents = discover_documents and not self.require_auth
        self.settings = settings
        self.worker = worker

    def ready(self):
        return self.worker.ready()

    def _trust(self, headers):
        return classify_sender(headers, self.settings.sender_allowlist, self.settings.spouse_forward_senders,
                               getattr(self.settings, "owner_mailbox", None),
                               payment_senders=getattr(self.settings, "payment_senders", ()),
                               document_senders=getattr(self.settings, "document_senders", ()),
                               require_authentication=self.require_auth)

    def _manual_forward(self, outer, inner, pending):
        attached = _has_rfc822_attachment(outer)
        return replace(pending, outcome="imported", invoice=None, forwarded_manually=True,
                       sender_domain=forward_sender_domain(inner), source_kind="attachment" if attached else "email")

    def _nested_inline_original(self, message):
        """Recognize two valid inline envelopes only to create a manual placeholder."""
        for kind, part in _body_parts(message):
            try:
                payload = _document_payload(kind, part)
                text = self.worker.parse("html", payload) if kind == "html" and payload else payload.decode("utf-8")
            except (WorkerRejected, UnicodeError):
                continue
            lines = text.splitlines()
            markers = [i for i, line in enumerate(lines) if line.strip(" \t-:").casefold() in _FORWARD_MARKERS]
            if len(markers) != 2:
                continue
            first = _inline_forward("\n".join(lines[:markers[1]]) + "\n\n")
            second = _inline_forward("\n".join(lines[markers[1]:]))
            if (first is not None and first is not _INVALID_FORWARD and _forward_sender(first)
                    and second is not None and second is not _INVALID_FORWARD and _forward_sender(second)):
                return first
        return None

    def _respect_items(self, message, invoice, kind, text):
        if (invoice.issuer != "Respect Energy" or invoice.currency != "PLN"
                or not invoice.invoice_number.isdecimal()):
            return ()
        if kind != "pdf":
            # The trusted email summary can identify the invoice before its detailed PDF.
            pdfs = (part for part in mime_parts(message) if _is_pdf_part(part))
            attachment = next(pdfs, None)
            if attachment is None or next(pdfs, None) is not None:
                return ()
            try:
                text = self.worker.parse("pdf", _document_payload("pdf", attachment))
            except (WorkerRejected, WorkerUnavailable):
                return ()
        return settlement_items(_normalized_invoice_lines(text), invoice.issuer, invoice.invoice_number, invoice.amount)

    def _invoice(self, message, pending):
        # This is the authenticated direct envelope or the original selected by the forward trust gate.
        sender_domain = forward_sender_domain(message) or ""
        for kind, part in _document_parts(message):
            try:
                payload = _document_payload(kind, part)
                if not payload:
                    continue
                text = self.worker.parse(kind, payload)
            except _PartOversized:
                pending = replace(pending, outcome="skipped_oversized", parse_failed_reason=None)
                continue
            except WorkerRejected as error:
                pending = _parse_failed(pending, _worker_reason(error))
                continue
            invoice = extract_invoice(text)
            if invoice is not None and any(
                domain_matches(sender_domain, domain) for domain in _ISSUER_SENDER_DOMAINS.get(invoice.issuer, ())
            ):
                if items := self._respect_items(message, invoice, kind, text):
                    invoice = replace(invoice, items=items)
                return replace(pending, outcome="imported", invoice=invoice,
                               rfc_message_id_hmac=_rfc_message_hmac(message, self.settings.hmac_secret),
                               source_kind="attachment" if kind == "pdf" else "email",
                               parse_failed_reason=None)
        return pending

    def _document(self, message, pending, sender, *, inline_outer=None, forwarded=False, manual=False,
                  allow_undated_template=False):
        """Read trusted PDFs once, rejecting multiple distinct document numbers."""
        selected = None
        selected_template = None
        failure = pending
        no_text = False
        issue_dates: dict[str, set[str]] = {}
        sender_domain = forward_sender_domain(message) or ""
        outer_parts = mime_parts(inline_outer) if inline_outer is not None else ()
        for part in chain(mime_parts(message), outer_parts):
            if not _is_pdf_part(part):
                continue
            try:
                payload = _document_payload("pdf", part)
                if not payload:
                    continue
                text = self.worker.parse("pdf", payload)
            except _PartOversized:
                failure = replace(pending, outcome="skipped_oversized", parse_failed_reason=None)
                continue
            except WorkerRejected as error:
                failure = _parse_failed(pending, _worker_reason(error))
                continue
            if not text.strip():
                failure = _parse_failed(pending, "pdf_no_text")
                no_text = True
                continue
            invoice = extract_invoice(text)
            if invoice is not None:
                if not any(domain_matches(sender_domain, domain)
                           for domain in _ISSUER_SENDER_DOMAINS.get(invoice.issuer, ())):
                    continue
                pattern = _FORWARDED_INVOICE_NUMBERS.get(invoice.issuer)
                if forwarded and (manual or pattern is None or not pattern.fullmatch(invoice.invoice_number)):
                    continue
                document = ImportedDocument(invoice.issuer, invoice.nip, invoice.invoice_number, invoice.amount,
                                            invoice.currency, invoice.issue_date, invoice.due_date, "invoice", False, None,
                                            self._respect_items(message, invoice, "pdf", text))
            else:
                document = extract_document(text, merchant=sender.merchant, pdf=True)
            if document is None:
                continue
            if document.issue_date is None:
                if invoice is None or not allow_undated_template:
                    continue
            else:
                try:
                    email_date = datetime.fromtimestamp(pending.internal_date, _WARSAW).date()
                    distance = (date.fromisoformat(document.issue_date) - email_date).days
                except (OverflowError, OSError, ValueError):
                    continue
                if not -400 <= distance <= 7:
                    continue
            known_dates = issue_dates.setdefault(document.document_kind, set())
            if document.issue_date is not None:
                known_dates.add(document.issue_date)
                if len(known_dates) > 1:
                    return _parse_failed(pending, "multiple_documents")
            if selected is None:
                selected = document
                selected_template = invoice if document.issue_date is None else None
            elif (selected.invoice_number.casefold() != document.invoice_number.casefold()
                  or _payee_key(selected.issuer) != _payee_key(document.issuer)
                  or selected.currency != document.currency or Decimal(selected.amount) != Decimal(document.amount)):
                return _parse_failed(pending, "multiple_documents")
            else:
                same_kind = selected.document_kind == document.document_kind
                use_document = ((selected.document_kind == "receipt" and document.document_kind == "invoice")
                                or (same_kind and selected.issue_date is None and document.issue_date is not None))
                preferred = document if use_document else selected
                if use_document:
                    selected_template = invoice if document.issue_date is None else None
                selected = replace(preferred, items=selected.items if selected.items == document.items else (),
                                   paid=selected.paid or document.paid,
                                   receipt_number=selected.receipt_number or document.receipt_number)
        if selected is None:
            return replace(failure, diagnostic_reason="pdf_no_text") if no_text else failure
        secret = self.settings.hmac_secret
        if selected.issue_date is None and selected_template is not None and len(issue_dates) == 1:
            return replace(pending, outcome="imported", invoice=selected_template, source_kind="attachment",
                           rfc_message_id_hmac=_rfc_message_hmac(message, secret), parse_failed_reason=None)
        return self._document_result(message, pending, selected, "attachment")

    def _document_result(self, message, pending, selected, source_kind):
        secret = self.settings.hmac_secret
        number_hmac = (digest(secret, DOCUMENT_NUMBER_DOMAIN, selected.invoice_number.casefold())
                       if selected.invoice_number else None)
        provenance = {"number_hmac": number_hmac,
                      "seller_key": _payee_key(selected.issuer), "paid": selected.paid,
                      "receipt_number_hmac": (digest(secret, DOCUMENT_RECEIPT_DOMAIN, selected.receipt_number.casefold())
                                              if selected.receipt_number else None)}
        return replace(pending, outcome="imported", invoice=selected, source_kind=source_kind,
                       rfc_message_id_hmac=_rfc_message_hmac(message, secret),
                       document_provenance=provenance, parse_failed_reason=None)

    def _payment_from_part(self, kind, part, subjects, pending, sender, message, *, cut_inline=False, apple=False):
        try:
            payload = _document_payload(kind, part)
            if not payload:
                return pending, True, True
            text = self.worker.parse(kind, payload)
        except _PartOversized:
            return replace(pending, outcome="skipped_oversized"), True, False
        except WorkerRejected as error:
            return _parse_failed(pending, _worker_reason(error)), True, False
        if cut_inline:
            if sum(_is_forward_marker(line) for line in text.splitlines()) != 1:
                return pending, False, False
            cut = inline_forward_body(text)
            if cut is text:
                return pending, False, False
            text = cut
        if not text.strip():
            return pending, True, True
        inspected = inspect_payment(text, subjects=subjects, merchant=sender.merchant, gateway=sender.gateway,
                                    provider=sender.provider)
        if inspected.missing_total:
            return pending, True, True
        secret = self.settings.hmac_secret
        if inspected.reject_reason == "failed":
            ref_hmacs = tuple(digest(secret, GATEWAY_REF_DOMAIN, ref) for ref in (
                inspected.gateway_ref, inspected.gateway_ref_secondary,
            ) if ref is not None)
            if sender.gateway and ref_hmacs:
                return replace(pending, outcome="skipped_non_receipt",
                               failed_gateway=(sender.merchant, *ref_hmacs)), False, False
            return pending, False, False
        payment = inspected.payment
        if payment is None:
            return pending, False, False
        receipt_date = _payment_receipt_date(message, pending)
        if receipt_date is None:
            return pending, False, False
        if any(abs((body_date - receipt_date).days) > 3 for body_date in payment.body_dates):
            return pending, False, False
        reference = payment.order_reference
        apple_number = apple_document_number(text) if apple else None
        item_block = None
        payment_method = None
        if not sender.gateway:
            inspect_items = inspect_apple_subscription_items if apple else inspect_shop_items
            item_block = inspect_items(text, payment.total_amount, payment.currency)
            _, payment_method = purchase_payment_scope(text.splitlines())
        provenance = PendingPayment(
            replace(payment, order_reference=None), receipt_date,
            digest(secret, PAYMENT_SENDER_DOMAIN, sender.sender),
            order_reference_hmac=digest(secret, ORDER_REFERENCE_DOMAIN, reference) if reference else None,
            gateway=sender.gateway,
            payee_hmac=(digest(secret, PAYMENT_PAYEE_DOMAIN, _payee_key(payment.merchant))
                        if sender.gateway and not payment.payee_from_config else None),
            status=inspected.status,
            gateway_ref_hmac=(digest(secret, GATEWAY_REF_DOMAIN, inspected.gateway_ref)
                              if inspected.gateway_ref is not None else None),
            gateway_ref_secondary_hmac=(digest(secret, GATEWAY_REF_DOMAIN, inspected.gateway_ref_secondary)
                                        if inspected.gateway_ref_secondary is not None else None),
            gateway_name=sender.merchant if sender.gateway else None,
            blik_ref_hmac=(digest(secret, GATEWAY_REF_DOMAIN, inspected.blik_ref)
                           if inspected.blik_ref is not None else None),
            card_last4=inspected.card_last4,
            payment_method=payment_method,
            items=item_block or (), item_block_rejected=item_block == (),
            apple_document_hmac=digest(secret, DOCUMENT_NUMBER_DOMAIN, apple_number) if apple_number else None,
        )
        return replace(pending, outcome="imported", payment=provenance,
                       rfc_message_id_hmac=_rfc_message_hmac(message, secret)), False, False

    def _reconcile_alternative_items(self, result, message, pending, sender, subjects, inline_outer, apple):
        # Only compare representations from the same multipart/alternative. For inline
        # forwards the trust gate has already compared the two original From/Message-ID headers.
        envelope = message if inline_outer is None else inline_outer
        plain = envelope.get_body(("plain",))
        html = next((part.get_body(("html",)) for part in mime_parts(envelope)
                     if part.get_content_type() == "multipart/alternative"
                     and part.get_body(("plain",)) is plain and plain is not None), None)
        if html is None:
            return result
        try:
            alternate, _, _ = self._payment_from_part(
                "html", html, subjects, pending, sender, message, cut_inline=inline_outer is not None, apple=apple,
            )
        except WorkerUnavailable:
            alternate = pending
        first, second = result.payment, alternate.payment
        items = ()
        if (second is not None and not (first.item_block_rejected or second.item_block_rejected)
                and first.fields.total_amount == second.fields.total_amount
                and first.fields.currency == second.fields.currency):
            evidence = ((first.card_last4, second.card_last4), (first.order_reference_hmac, second.order_reference_hmac),
                        (first.apple_document_hmac, second.apple_document_hmac),
                        (first.fields.body_dates, second.fields.body_dates), (first.items, second.items))
            if all(not left or not right or left == right for left, right in evidence):
                items = first.items or second.items
        return replace(result, payment=replace(first, items=items))

    def _payment(self, message, pending, sender, *, inline_outer=None):
        subjects = tuple(map(str, message.get_all("Subject", ())))
        failure = pending
        missing_total = False
        apple = not sender.gateway and domain_matches(forward_sender_domain(message) or "", "apple.com")
        for kind, part in _body_parts(message):
            result, should_continue, part_missing = self._payment_from_part(
                kind, part, subjects, pending, sender, message, apple=apple,
            )
            if not should_continue:
                # Render a second shop representation only after the selected one yielded a complete table.
                if kind == "text" and result.payment is not None and (apple or result.payment.items):
                    return self._reconcile_alternative_items(
                        result, message, pending, sender, subjects, inline_outer, apple)
                return result
            failure = result if result is not pending else failure
            missing_total |= part_missing
        if missing_total and inline_outer is not None and inline_outer.get_body(("plain",)) is not None:
            html_part = inline_outer.get_body(("html",))
            if html_part is not None:
                result, _, _ = self._payment_from_part(
                    "html", html_part, subjects, pending, sender, message, cut_inline=True, apple=apple,
                )
                return result
        return failure

    def _cancelled_order(self, message, pending, sender, *, inline_outer=None):
        if not any(confirms_order_cancellation(str(subject)) for subject in message.get_all("Subject", ())):
            return None
        if sender is not None and sender.gateway:
            # A gateway can still report a correlated terminal failure for an earlier registration.
            return self._payment(message, pending, sender, inline_outer=inline_outer)
        return pending

    def _parse(self, raw, pending, metadata_author):
        from .gmail_discovery import discover_document

        message = BytesParser(policy=policy.default).parsebytes(raw)
        if (sender_address(message.raw_items()) != metadata_author
                or self._trust(message.raw_items()) != pending.trust_path):
            return replace(pending, trust_path="untrusted", outcome="skipped_untrusted_sender")
        if message.defects:
            return _parse_failed(pending, "message_defects")
        if pending.trust_path == "untrusted":
            return discover_document(self, message, pending) or pending
        payment_senders = getattr(self.settings, "payment_senders", ())
        document_senders = getattr(self.settings, "document_senders", ())
        if pending.trust_path in ("spouse_forward", "owner_forward"):
            render_html = lambda payload: self.worker.parse("html", payload)
            inner = _forwarded_original(message, render_html)
            if inner is _INVALID_FORWARD:
                nested_inline = self._nested_inline_original(message)
                if nested_inline is not None:
                    return self._manual_forward(message, nested_inline, pending)
            if inner in (None, _INVALID_FORWARD) and self.discover_documents and not _has_rfc822_attachment(message):
                return discover_document(self, message, pending, attachments_only=True) or pending
            if inner is _INVALID_FORWARD:
                return pending
            if inner is None or _forward_sender(inner) is None:
                return replace(pending, outcome="skipped_non_receipt")
            nested = _forwarded_original(inner, render_html)
            if nested is _INVALID_FORWARD:
                return replace(pending, outcome="skipped_non_receipt")
            if nested is not None:
                if _forwarded_original(nested, render_html) is not None:
                    return replace(pending, outcome="skipped_non_receipt")
                return self._manual_forward(message, inner, pending)
            sender = payment_sender_for(inner.raw_items(), payment_senders)
            inline_outer = None if _has_rfc822_attachment(message) else message
            if cancelled := self._cancelled_order(inner, pending, sender, inline_outer=inline_outer):
                return cancelled
            manual = _forward_sender(inner) not in self.settings.sender_allowlist
            document_failure = pending
            document_sender = payment_sender_for(inner.raw_items(), document_senders)
            if document_sender is not None:
                imported = self._document(inner, pending, document_sender, inline_outer=inline_outer,
                                          forwarded=True, manual=manual, allow_undated_template=not manual)
                if imported.outcome == "imported" or imported.parse_failed_reason == "multiple_documents":
                    return imported
                document_failure = imported
            if not manual:
                imported = self._invoice(inner, pending)
                pattern = None if imported.invoice is None else _FORWARDED_INVOICE_NUMBERS.get(imported.invoice.issuer)
                if (imported.outcome == "imported" and pattern is not None
                        and pattern.fullmatch(imported.invoice.invoice_number)):
                    return imported
            if sender is not None:
                imported = self._payment(inner, pending, sender, inline_outer=inline_outer)
                if document_failure.diagnostic_reason == "pdf_no_text":
                    imported = replace(imported, diagnostic_reason="pdf_no_text")
                if imported.outcome == "imported":
                    return imported
                if imported.diagnostic_reason is not None:
                    return replace(self._manual_forward(message, inner, pending),
                                   diagnostic_reason=imported.diagnostic_reason)
            if self.discover_documents:
                discovered = discover_document(self, inner, pending, inline_outer=inline_outer)
                if discovered is not None:
                    return discovered
            if sender is None and document_failure.outcome != pending.outcome:
                return document_failure
            return self._manual_forward(message, inner, pending)
        sender = payment_sender_for(message.raw_items(), payment_senders)
        if cancelled := self._cancelled_order(message, pending, sender):
            return cancelled
        document_sender = payment_sender_for(message.raw_items(), document_senders)
        parsed = self._direct_document_or_payment(message, pending, sender, document_sender)
        if self.discover_documents and parsed.outcome == "skipped_non_receipt" and parsed.failed_gateway is None:
            return discover_document(self, message, pending) or parsed
        return parsed

    def _direct_document_or_payment(self, message, pending, sender, document_sender):
        if document_sender is not None:
            issuer_sender = classify_sender(message.raw_items(), self.settings.sender_allowlist, (),
                                            require_authentication=self.require_auth) == "direct_allowlist"
            imported = self._document(message, pending, document_sender, allow_undated_template=issuer_sender)
            if imported.outcome == "imported" or imported.parse_failed_reason == "multiple_documents":
                return imported
            if sender is None:
                if not issuer_sender:
                    return imported
                template = self._invoice(message, pending)
                return imported if template.outcome == pending.outcome else template
        if sender is not None:
            payment = self._payment(message, pending, sender)
            if document_sender is not None and imported.diagnostic_reason == "pdf_no_text":
                payment = replace(payment, diagnostic_reason="pdf_no_text")
            return payment
        return self._invoice(message, pending)

    def process(self, run, message_id, gmail_hmac):
        try:
            meta = run.message(message_id)
        except GmailError as error:
            # A message can be deleted between the scoped list and metadata GET.
            if error.http_status == 404:
                return None
            raise
        internal_date, size = _metadata(meta, message_id)
        if has_spam_or_trash_label(meta):
            return None
        pending = PendingMessage(gmail_hmac, internal_date, "untrusted", "skipped_non_receipt")
        if size > MAX_MESSAGE_BYTES:
            return replace(pending, outcome="skipped_oversized")
        headers = _metadata_headers(meta)
        author = sender_address(headers)
        trust_path = self._trust(headers)
        if trust_path == "untrusted" and not (self.discover_documents and author):
            return replace(pending, outcome="skipped_untrusted_sender")
        pending = replace(pending, trust_path=trust_path)
        try:
            response = run.message(message_id, raw=True)
            if has_spam_or_trash_label(response):
                return None
            raw = _raw_bytes(response, message_id)
            result = self._parse(raw, pending, author)
        except GmailError as error:
            if error.http_status == 404:
                return replace(pending, outcome="deleted")
            raise
        except _PartOversized:
            return replace(pending, outcome="skipped_oversized")
        except WorkerRejected as error:
            result = _parse_failed(pending, _worker_reason(error))
        except MessageError:
            result = _parse_failed(pending, "mime_error")
        except RecursionError:
            result = _parse_failed(pending, "recursion")
        except UnicodeError:
            result = _parse_failed(pending, "unicode_error")
        except WorkerUnavailable:
            raise MessageParserUnavailable(
                _parse_failed(pending, "worker_rejected"), worker_ready=self.ready(),
            ) from None
        if result.outcome == _PARSE_FAILED:
            log.warning("gmail parse_failed reason=%s", result.parse_failed_reason)
        return result
