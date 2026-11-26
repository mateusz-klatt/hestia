"""Extract structured purchase documents for subsequent owner-card corroboration."""
from __future__ import annotations

from dataclasses import replace
from itertools import chain, islice
import re

from .gmail_document import ImportedDocument, extract_document
from .gmail_invoice import _FORWARDED_INVOICE_NUMBERS, extract_invoice
from .gmail_payment import _body_marker, _subject_rejected, confirms_order_cancellation
from .gmail_trust import mime_parts
from .gmail_worker import WorkerRejected

# A discovery hit may contain two body alternatives and several small PDFs.
# Limit worker calls even if a single Gmail message contains many attachments.
_MAX_PARTS = 8
_NOTICE = re.compile(
    r"\b(?:anulowan\w*|cancell?ed|cancellation|(?:faktura |invoice )?pro[ -]?forma|quotation|payment reminder"
    r"|przypomnienie o płatności)\b", re.IGNORECASE,
)


def _extract_purchase(text):
    document = extract_document(text, merchant="", allow_unnumbered_receipt=True)
    if document is not None:
        return document
    invoice = extract_invoice(text)
    if (invoice is None or invoice.issue_date is None
            or not _FORWARDED_INVOICE_NUMBERS[invoice.issuer].fullmatch(invoice.invoice_number)):
        return None
    return ImportedDocument(invoice.issuer, invoice.nip, invoice.invoice_number, invoice.amount, invoice.currency,
                            invoice.issue_date, invoice.due_date, "invoice", False, None)


def discover_document(processor, message, pending, *, inline_outer=None, attachments_only=False):
    """Return an uncommitted candidate from one original envelope, pending a unique bank match."""
    from .gmail_message import _document_parts, _document_payload, _is_pdf_part

    subjects = message.get_all("Subject", ())
    if any(confirms_order_cancellation(str(subject)) or _subject_rejected(str(subject), None)
           or _NOTICE.search(str(subject)) for subject in subjects):
        return None
    parts = _document_parts(message)
    if inline_outer is not None:
        parts = chain(parts, (("pdf", part) for part in mime_parts(inline_outer) if _is_pdf_part(part)))
    parts = list(islice(parts, _MAX_PARTS + 1))
    if len(parts) > _MAX_PARTS or (attachments_only and not any(kind == "pdf" for kind, _ in parts)):
        return None
    selected = None
    source_kind = "email"
    for kind, part in parts:
        try:
            payload = _document_payload(kind, part)
            if not payload:
                continue
            text = processor.worker.parse(kind, payload)
        except WorkerRejected:
            # A second unreadable document cannot safely corroborate the first.
            return None
        if kind == "pdf" and not text.strip():
            return None
        if any(_body_marker(line, None) or _NOTICE.match(line) or confirms_order_cancellation(line)
               for line in map(str.strip, text.splitlines())):
            return None
        if attachments_only and kind != "pdf":
            continue
        document = _extract_purchase(text)
        if document is None:
            continue
        if selected is not None and document != selected:
            return None
        selected = document
        if kind == "pdf":
            source_kind = "attachment"
    if selected is None:
        return None
    return replace(processor._document_result(message, pending, selected, source_kind), card_required=True)
