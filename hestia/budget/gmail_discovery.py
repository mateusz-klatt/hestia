"""Extract structured purchase documents for subsequent owner-card corroboration."""
from __future__ import annotations

import re
from dataclasses import replace
from itertools import chain, islice

from .gmail_document import ImportedDocument, extract_document
from .gmail_invoice import _FORWARDED_INVOICE_NUMBERS, extract_invoice
from .gmail_payment import _body_marker, _subject_rejected, confirms_order_cancellation, purchase_payment_scope
from .gmail_trust import mime_parts
from .gmail_worker import WorkerRejected
from .receipt_parser import fold_text

# A discovery hit may contain two body alternatives and several small PDFs.
# Limit worker calls even if a single Gmail message contains many attachments.
_MAX_PARTS = 8
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
    r"zwrot|refund|return|korekta|anulowanie|anulowan[aeoy]|cancell?ed|returned|(?:order )?cancell?ation)"
    r"(?=\s*[:#–-]|\s+\d|\s*[.!]?$))\b"
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
    return bool(_REVERSAL_HEADING.match(line)
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
    from .gmail_message import _document_parts, _document_payload, _is_pdf_part

    subjects = message.get_all("Subject", ())
    if any(confirms_order_cancellation(str(subject)) or _subject_rejected(str(subject), None)
           or _NOTICE.search(str(subject)) or _REVERSAL_SUBJECT.search(fold_text(str(subject)))
           or _body_reversal(str(subject))
           for subject in subjects):
        return replace(pending, outcome="skipped_non_receipt")
    parts = _document_parts(message)
    if inline_outer is not None:
        parts = chain(parts, (("pdf", part) for part in mime_parts(inline_outer) if _is_pdf_part(part)))
    parts = list(islice(parts, _MAX_PARTS + 1))
    if len(parts) > _MAX_PARTS or (attachments_only and not any(kind == "pdf" for kind, _ in parts)):
        return None
    selected = None
    source_kind = "email"
    payment_lines = []
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
