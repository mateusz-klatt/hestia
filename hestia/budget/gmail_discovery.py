"""Extract structured purchase documents for subsequent owner-card corroboration."""
from __future__ import annotations

import re
from dataclasses import replace
from itertools import chain, islice

from .gmail_document import ImportedDocument, extract_document
from .gmail_invoice import _FORWARDED_INVOICE_NUMBERS, extract_invoice
from .gmail_payment import _CURRENCY, _body_marker, _subject_rejected, confirms_order_cancellation, purchase_payment_scope
from .gmail_trust import classify_sender, mime_parts
from .gmail_worker import WorkerRejected
from .receipt_parser import fold_text

# A discovery hit may contain two body alternatives and several small PDFs.
# Limit worker calls even if a single Gmail message contains many attachments.
_MAX_PARTS = 8
_MAX_DISCOVERY_PDF_BYTES = 2 * 1024 * 1024
_NOTICE = re.compile(
    r"\b(?:(?:faktura |invoice )?pro[ -]?forma|quotation|payment reminder"
    r"|przypomnienie o płatności)\b", re.IGNORECASE,
)
_REVERSAL_SUBJECT = re.compile(
    r"\b(?:anulowan\w*|cancell?ed|cancellation|returned|"
    r"return (?:confirmation|notice|approved|received|completed)|"
    r"corrected invoice|invoice correction|credit memo)\b"
)
_REVERSAL_HEADING = re.compile(
    r"^(?:credit (?:note|memo)|corrected invoice|invoice correction|"
    r"(?:potwierdzenie )?anulowanie (?:zamowienia|platnosci|transakcji)|"
    r"(?:order )?cancell?ation (?:confirmation|notice)|"
    r"return (?:confirmation|notice|approved|received|completed)|"
    r"(?:zwrot (?:srodkow|platnosci|pieniedzy|za zamowienie|towaru)|"
    r"zwrot|dokonano zwrotu|refund|return|korekta|anulowanie|anulowan[aeoy]|cancell?ed|returned|(?:order )?cancell?ation)"
    r"(?=\s*[:#–-]|\s+\d|\s*[.!]?$))\b"
)
_REFUND_AMOUNT = re.compile(
    r"^(?:(?:zwrot|dokonano zwrotu)(?: srodkow| platnosci| pieniedzy)? w kwocie"
    r"|(?:your )?refund of|refund amount)\b"
    rf" *[:=]? *(?:(?:{_CURRENCY}|zl) *)?[-−]? *[0-9]",
    re.IGNORECASE,
)
_REVERSAL_STATUS = re.compile(
    r"\b(?:(?:zamowienie|platnosc|transakcja) (?:zostal[ao] )?anulowan[aeo]|"
    r"(?:order|payment|transaction) (?:(?:has been|was|is now) )?cancell?ed|"
    r"(?:anulowalismy|anulowano) (?:zamowienie|platnosc|transakcje)|"
    r"(?:zwrot|refund|return) (?:(?:has been|was|zostal) )?"
    r"(?:issued|processed|approved|received|completed|zlecony|wykonany|przyjety|zaakceptowany)|"
    r"(?:otrzymalismy|przyjelismy|zaakceptowalismy|zlecilismy) zwrot|"
    r"(?:we (?:issued|processed|approved) (?:a |your )?(?:refund|return))|"
    r"(?:we (?:have )?refunded|zwrocilismy) (?:your|the|ci|twoje|platnosc|srodki)|"
    r"(?:faktura korygujaca|korekta faktury|credit note|credit memo)\b)"
)
_CONDITIONAL = re.compile(r"^(?:if\b|jesli\b|jezeli\b|w przypadku\b)")


def _body_reversal(line):
    line = " ".join(fold_text(line).split()).lstrip("-*• ")
    return bool(_REVERSAL_HEADING.match(line) or _REFUND_AMOUNT.match(line)
                or (not _CONDITIONAL.match(line) and _REVERSAL_STATUS.search(line)))


def _extract_purchase(text, *, pdf=False):
    document = extract_document(text, merchant="", allow_unnumbered_receipt=True, pdf=pdf)
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
    from .gmail_message import _document_parts, _document_payload, _is_pdf_part, _parse_failed

    subjects = message.get_all("Subject", ())
    if any(confirms_order_cancellation(str(subject)) or _subject_rejected(str(subject), None)
           or _NOTICE.search(str(subject)) or _REVERSAL_SUBJECT.search(fold_text(str(subject)))
           or _body_reversal(str(subject))
           for subject in subjects):
        return replace(pending, outcome="skipped_non_receipt")
    parts = ((kind, part, message) for kind, part in _document_parts(message))
    if inline_outer is not None:
        parts = chain(parts, (("pdf", part, inline_outer) for part in mime_parts(inline_outer) if _is_pdf_part(part)))
    parts = list(islice(parts, _MAX_PARTS + 1))
    if len(parts) > _MAX_PARTS:
        return None
    # Check the decoded PDFs before sending any part of an untrusted discovery hit to the worker.
    pdf_payloads = {part: part.get_payload(decode=True) or b"" for kind, part, _ in parts if kind == "pdf"}
    if attachments_only and not pdf_payloads:
        return None
    oversized_envelopes = {envelope for kind, part, envelope in parts
                           if kind == "pdf" and len(pdf_payloads[part]) > _MAX_DISCOVERY_PDF_BYTES}
    settings = processor.settings
    for envelope in oversized_envelopes:
        if attachments_only or classify_sender(
            envelope.raw_items(), settings.sender_allowlist, settings.spouse_forward_senders,
            getattr(settings, "owner_mailbox", None),
            document_senders=getattr(settings, "document_senders", ()),
            require_authentication=processor.require_auth,
        ) != "direct_allowlist":
            return _parse_failed(pending, "other")
    selected = None
    source_kind = "email"
    payment_lines = []
    for kind, part, _ in parts:
        try:
            payload = _document_payload(kind, part, decoded=pdf_payloads.get(part))
            if not payload:
                continue
            text = processor.worker.parse(kind, payload)
        except WorkerRejected:
            # A second unreadable document cannot safely corroborate the first.
            return None
        if kind == "pdf" and not text.strip():
            return None
        lines = [line.strip() for line in text.splitlines()]
        if any(_body_marker(line, None) or _NOTICE.match(line) or confirms_order_cancellation(line)
               or _body_reversal(line) for line in lines):
            return replace(pending, outcome="skipped_non_receipt")
        payment_lines.extend(lines)
        if attachments_only and kind != "pdf":
            continue
        document = _extract_purchase(text, pdf=kind == "pdf")
        if document is None:
            continue
        if selected is not None and document != selected:
            return None
        if selected is None or kind == "pdf" and source_kind != "attachment":
            selected = document
        elif kind == "pdf" and selected.items != document.items:
            selected = replace(selected, items=())
        if kind == "pdf":
            source_kind = "attachment"
    if selected is None:
        return None
    last4, payment_method = purchase_payment_scope(payment_lines)
    selected = replace(selected, card_last4=last4, payment_method=payment_method)
    return replace(processor._document_result(message, pending, selected, source_kind), card_required=True)
