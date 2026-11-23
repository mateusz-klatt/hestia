"""Fetch bounded Gmail content after sender verification and emit structured provenance."""
from __future__ import annotations

import base64
from dataclasses import replace
from email import policy
from email.errors import MessageError
from email.parser import BytesParser
import re

from .gmail_address import canonical_mailbox
from .gmail_client import GmailError
from .gmail_invoice import _FORWARDED_INVOICE_NUMBERS, extract_invoice
from .gmail_store import RFC_MESSAGE_DOMAIN, PendingMessage, digest
from .gmail_trust import classify_sender, forward_sender_domain, mime_parts, unwrap_trusted_forward
from .gmail_worker import MAX_HTML_BYTES, MAX_PDF_BYTES, WorkerRejected, WorkerUnavailable


MAX_MESSAGE_BYTES = 10 * 1024 * 1024
_MAX_BASE64_CHARS = 4 * ((MAX_MESSAGE_BYTES + 2) // 3)
_INTERNAL_DATE = re.compile(r"[0-9]{1,19}\Z", re.ASCII)


class _PartOversized(WorkerRejected):
    """The selected MIME data exceeded its worker input limit."""


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


def _document_parts(message):
    for subtype, kind in (("plain", "text"), ("html", "html")):
        part = message.get_body((subtype,))
        if part is not None:
            yield kind, part
    for part in mime_parts(message):
        if part.get_content_type() == "application/pdf":
            yield "pdf", part


def _document_payload(kind, part):
    limit = MAX_PDF_BYTES if kind == "pdf" else MAX_HTML_BYTES
    payload = part.get_payload(decode=True) or b""
    if part.defects:
        raise WorkerRejected
    if len(payload) > limit:
        raise _PartOversized
    if kind != "pdf":
        try:
            payload = payload.decode(part.get_content_charset() or "utf-8", errors="replace").encode("utf-8")
        except (LookupError, UnicodeError):
            raise WorkerRejected from None
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
                               getattr(self.settings, "owner_mailbox", None))

    def _manual_forward(self, outer, inner, pending):
        domain = forward_sender_domain(inner)
        if domain is None:
            return replace(pending, outcome="skipped_non_receipt")
        attached = any(part.get_content_type() == "message/rfc822" for part in mime_parts(outer))
        return replace(pending, outcome="imported", invoice=None, forwarded_manually=True, sender_domain=domain,
                       rfc_message_id_hmac=_rfc_message_hmac(inner, self.settings.hmac_secret),
                       source_kind="attachment" if attached else "email")

    def _invoice(self, message, pending):
        for kind, part in _document_parts(message):
            try:
                payload = _document_payload(kind, part)
                if not payload:
                    continue
                text = self.worker.parse(kind, payload)
            except _PartOversized:
                pending = replace(pending, outcome="skipped_oversized")
                continue
            except WorkerRejected:
                pending = replace(pending, outcome="parse_failed")
                continue
            invoice = extract_invoice(text)
            if invoice is not None:
                return replace(pending, outcome="imported", invoice=invoice,
                               rfc_message_id_hmac=_rfc_message_hmac(message, self.settings.hmac_secret),
                               source_kind="attachment" if kind == "pdf" else "email")
        return pending

    def _parse(self, raw, pending):
        message = BytesParser(policy=policy.default).parsebytes(raw)
        if self._trust(message.raw_items()) != pending.trust_path:
            return replace(pending, trust_path="untrusted", outcome="skipped_untrusted_sender")
        if message.defects:
            return replace(pending, outcome="parse_failed")
        if pending.trust_path in ("spouse_forward", "owner_forward"):
            selected = unwrap_trusted_forward(
                message, self.settings.known_merchant_domains,
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
            return self._parse(raw, pending)
        except GmailError as error:
            if error.http_status == 404:
                return replace(pending, outcome="deleted")
            raise
        except _PartOversized:
            return replace(pending, outcome="skipped_oversized")
        except (MessageError, RecursionError, UnicodeError, WorkerRejected):
            return replace(pending, outcome="parse_failed")
        except WorkerUnavailable:
            raise GmailError("parser_unavailable") from None
