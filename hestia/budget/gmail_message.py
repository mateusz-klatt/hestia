"""Fetch bounded Gmail content after sender verification and emit structured provenance."""
from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime
from email import policy
from email.errors import MessageError
from email.parser import BytesParser
import logging
import re
from zoneinfo import ZoneInfo

from .gmail_address import canonical_mailbox
from .gmail_client import GmailError
from .gmail_invoice import _FORWARDED_INVOICE_NUMBERS, extract_invoice
from .gmail_payment import _payee_key, inspect_payment
from .gmail_store import (GATEWAY_REF_DOMAIN, ORDER_REFERENCE_DOMAIN, PAYMENT_PAYEE_DOMAIN, PAYMENT_SENDER_DOMAIN,
                          RFC_MESSAGE_DOMAIN, PendingMessage, PendingPayment, digest, normalize_parse_failed_reason)
from .gmail_trust import (classify_sender, domain_matches, forward_sender_domain, mime_parts, payment_sender_for,
                          unwrap_trusted_forward)
from .gmail_worker import MAX_HTML_BYTES, MAX_PDF_BYTES, PDFUnsupported, WorkerRejected, WorkerUnavailable


MAX_MESSAGE_BYTES = 10 * 1024 * 1024
_MAX_BASE64_CHARS = 4 * ((MAX_MESSAGE_BYTES + 2) // 3)
_INTERNAL_DATE = re.compile(r"[0-9]{1,19}\Z", re.ASCII)
_WARSAW = ZoneInfo("Europe/Warsaw")
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


def _document_payload(kind, part):
    limit = MAX_PDF_BYTES if kind == "pdf" else MAX_HTML_BYTES
    payload = part.get_payload(decode=True) or b""
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


class GmailMessageProcessor:
    def __init__(self, settings, worker):
        self.settings = settings
        self.worker = worker

    def ready(self):
        return self.worker.ready()

    def _trust(self, headers):
        return classify_sender(headers, self.settings.sender_allowlist, self.settings.spouse_forward_senders,
                               getattr(self.settings, "owner_mailbox", None),
                               payment_senders=getattr(self.settings, "payment_senders", ()))

    def _manual_forward(self, outer, inner, pending):
        attached = any(part.get_content_type() == "message/rfc822" for part in mime_parts(outer))
        return replace(pending, outcome="imported", invoice=None, forwarded_manually=True,
                       sender_domain=forward_sender_domain(inner), source_kind="attachment" if attached else "email")

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
                return replace(pending, outcome="imported", invoice=invoice,
                               rfc_message_id_hmac=_rfc_message_hmac(message, self.settings.hmac_secret),
                               source_kind="attachment" if kind == "pdf" else "email",
                               parse_failed_reason=None)
        return pending

    def _payment(self, message, pending, sender):
        subjects = tuple(map(str, message.get_all("Subject", ())))
        failure = pending
        for kind, part in _body_parts(message):
            try:
                payload = _document_payload(kind, part)
                if not payload:
                    continue
                text = self.worker.parse(kind, payload)
            except _PartOversized:
                failure = replace(pending, outcome="skipped_oversized")
                continue
            except WorkerRejected as error:
                failure = _parse_failed(pending, _worker_reason(error))
                continue
            if not text.strip():
                continue
            inspected = inspect_payment(text, subjects=subjects, merchant=sender.merchant, gateway=sender.gateway,
                                        provider=sender.provider)
            if inspected.missing_total:
                continue
            secret = self.settings.hmac_secret
            if inspected.reject_reason == "failed":
                if sender.gateway and inspected.gateway_ref is not None:
                    ref_hmac = digest(secret, GATEWAY_REF_DOMAIN, inspected.gateway_ref)
                    return replace(pending, outcome="skipped_non_receipt",
                                   failed_gateway=(sender.merchant, ref_hmac))
                return pending
            payment = inspected.payment
            if payment is None:
                return pending
            try:
                receipt_date = datetime.fromtimestamp(pending.internal_date, _WARSAW).date()
            except (OverflowError, OSError, ValueError):
                return pending
            if any(abs((body_date - receipt_date).days) > 3 for body_date in payment.body_dates):
                return pending
            reference = payment.order_reference
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
                gateway_name=sender.merchant if sender.gateway else None,
            )
            return replace(pending, outcome="imported", payment=provenance,
                           rfc_message_id_hmac=_rfc_message_hmac(message, secret))
        return failure

    def _parse(self, raw, pending):
        message = BytesParser(policy=policy.default).parsebytes(raw)
        if self._trust(message.raw_items()) != pending.trust_path:
            return replace(pending, trust_path="untrusted", outcome="skipped_untrusted_sender")
        if message.defects:
            return _parse_failed(pending, "message_defects")
        if pending.trust_path in ("spouse_forward", "owner_forward"):
            selected = unwrap_trusted_forward(
                message,
                sender_allowlist=self.settings.sender_allowlist,
                render_html=lambda payload: self.worker.parse("html", payload),
            )
            if selected is None:
                return replace(pending, outcome="skipped_non_receipt")
            inner, manual = selected
            if not manual:
                imported = self._invoice(inner, pending)
                pattern = None if imported.invoice is None else _FORWARDED_INVOICE_NUMBERS.get(imported.invoice.issuer)
                if (imported.outcome == "imported" and pattern is not None
                        and pattern.fullmatch(imported.invoice.invoice_number)):
                    return imported
            return self._manual_forward(message, inner, pending)
        payment_senders = getattr(self.settings, "payment_senders", ())
        sender = payment_sender_for(message.raw_items(), payment_senders)
        if sender is not None:
            return self._payment(message, pending, sender)
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
        pending = PendingMessage(gmail_hmac, internal_date, "untrusted", "skipped_non_receipt")
        if size > MAX_MESSAGE_BYTES:
            return replace(pending, outcome="skipped_oversized")
        trust_path = self._trust(_metadata_headers(meta))
        if trust_path == "untrusted":
            return replace(pending, outcome="skipped_untrusted_sender")
        pending = replace(pending, trust_path=trust_path)
        try:
            raw = _raw_bytes(run.message(message_id, raw=True), message_id)
            result = self._parse(raw, pending)
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
            raise GmailError("parser_unavailable") from None
        if result.outcome == _PARSE_FAILED:
            log.warning("gmail parse_failed reason=%s", result.parse_failed_reason)
        return result
