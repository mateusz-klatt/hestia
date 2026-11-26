"""Extract labelled purchase documents from trusted senders' rendered PDFs."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import re

from .analytics_core import canonical_money
from .gmail_invoice import _normalized_invoice_lines, _parse_nip, _safe_field
from .gmail_respect import settlement_items
from .gmail_payment import _following_values, _parse_amount, _parse_date, _RejectedPayment
from .receipt_parser import MAX_TEXT_BYTES, ReceiptError


@dataclass(frozen=True, repr=False)
class ImportedDocument:
    issuer: str
    nip: str | None
    invoice_number: str | None
    amount: str
    currency: str
    issue_date: str | None
    due_date: str | None
    document_kind: str
    paid: bool
    receipt_number: str | None
    items: tuple[dict, ...] = field(default=(), compare=False)


_REJECT = re.compile(r"wyciąg|zestawienie operacji|statement of account|historia rachunku|skierowanie|recepta|e-zwolnienie|wynik badania|newsletter|korekta|korygując|credit note", re.I)
_OUT_OF_SCOPE = re.compile(r"^\s*(?:bilet(?:y|u|ów)?|tickets?|booking|rezerwacja|rezerwacji)\b", re.I | re.M)
_DOCUMENT = re.compile(r"faktura|invoice|receipt|rachunek|paragon|\bbilling\s+number\b", re.I)
_GENERIC_SELLER = re.compile(r"^(?:billing|(?:summary|invoice|receipt|statement|faktura|rachunek|paragon)\b)", re.I)
_LABELS = {
    "invoice": r"faktura(?:\s+vat)?\s+nr\.?|(?:faktura\s+)?numer faktury|invoice number|billing number|invoice(?=\s*#)",
    "receipt": r"receipt number|receipt(?=\s*#)|paragon(?:\s+nr\.?)?",
    "issue": r"data wystawienia(?: faktury)?|data faktury|date of issue|date issued|document date|purchase date|transaction date|date(?=\s*:)|data zakupu",
    "paid_date": r"date paid",
    "due": r"termin płatności|date due|payment due on",
    "seller": r"sprzedawca|merchant(?=\s*:)|seller(?=\s*:)|from(?=\s*:|\s*$)",
    "issuer": r"faktura wystawiona przez",
    "buyer": r"dane nabywcy|nabywca|odbiorca|kupujący|billed to|bill to|sold to|customer",
    "nip": r"nip",
    "total0": r"do zapłaty razem|razem do zapłaty",
    "total1": r"kwota do zapłaty|do zapłaty|amount due|total due",
    "total2": r"suma brutto|suma(?=\s*:|\s+w\s+(?:PLN|EUR|USD|GBP)\b)|wartość brutto|kwota brutto|total amount|grand total|total",
    "total3": r"zapłacono|amount paid",
}
_LABEL = re.compile(r"^\s*(?:" + "|".join(f"(?P<{key}>{value})" for key, value in _LABELS.items()) + r")(?!\w)\s*[:.]?\s*(?P<value>.*)$", re.I)
_PARAGON = re.compile(r"^\s*paragon\b", re.I | re.M)
_RECEIPT_HEADING = re.compile(r"^\s*(?:payment\s+|sales\s+)?receipt\s*:?\s*$", re.I | re.M)
_PAID = re.compile(r"zapłacono|amount paid|payment history|płatność kartą", re.I)
_EXCLUDED = re.compile(r"netto|\bnet\b|subtotal|excluding tax|podatek|\bvat\b", re.I)
_RECEIPT_NUMBER = re.compile(r"[0-9A-Z-]{4,64}\Z", re.ASCII)
_TABLE_OTHER = re.compile(r"^identyfikator płatności\s*:?\s*$", re.I)
_KNOWN_HEADER = re.compile(r"^(?:identyfikator płatności|tax|vat id|nr vat|nip ue|netto|brutto|vat)\b", re.I)
_CURRENCY_LABEL = re.compile(r"^w\s+(PLN|EUR|USD|GBP)\b\s*:?\s*(.*)$", re.I)
_SETTLEMENT = re.compile(r"\bfaktura rozliczeniowa\b", re.I)
_SETTLEMENT_NUMBER = re.compile(r"^nr\.\s*(\S+)\s+z\s+dnia\s*:\s*(.*)$", re.I)
_LEADING_DATE = re.compile(r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{1,2}([./])[0-9]{1,2}\1[0-9]{4})(?![0-9])")
_SELLER_BUYER = re.compile(r"^sprzedawca\s*:\s*nabywca\s*:?\s*$", re.I)
_INLINE_NIP = re.compile(r"\bNIP\s*:\s*((?:PL)?[0-9][0-9 -]{8,16}[0-9])", re.I)
_EU_VAT_LABEL = re.compile(r"^(?:(?:nip\s+ue|vat\s*id|nr\s+vat)\b|ue\s*:)", re.I)
_EU_VAT_VALUE = re.compile(
    r"^(?:AT|BE|BG|HR|CY|CZ|DK|EE|FI|FR|DE|EL|GR|HU|IE|IT|LV|LT|LU|MT|NL|PL|PT|RO|SK|SI|ES|SE|XI|CH|GB)"
    r"[\s-]*(?=[A-Z0-9 .-]*[0-9])[A-Z0-9 .-]+$", re.I | re.ASCII,
)
_SELLER_ADDRESS = re.compile(r"\s+ul\.|,\s*[0-9]{2}-[0-9]{3}\b", re.I)
_BUYER_INLINE = re.compile(r"\b(?:dane nabywcy|nabywca|odbiorca|kupujący|billed to|bill to|sold to|customer)\s*:", re.I)
_VAT_SUMMARY = re.compile(r"^(?:suma\s*:|wartość brutto\s*:?)\s*$", re.I)
_VAT_COLUMN = re.compile(r"^(?:wartość\s+|kwota\s+)?(netto|podatek(?:\s+vat)?|vat|brutto)\s*:?\s*$", re.I)


def _document_date(value: str):
    match = _LEADING_DATE.match(value)
    return _parse_date(match.group() if match else value)


def _table_fields(lines: list[str], fields: dict[str, list[str]]) -> set[int]:
    """Read the invoice/date columns only when a complete header precedes them."""
    nonempty = [(index, line) for index, line in enumerate(lines) if line]
    headers = set()
    for index, (line_index, line) in enumerate(nonempty):
        first = _LABEL.fullmatch(line)
        if not first or first.group("invoice") is None or first.group("value"):
            continue
        following = nonempty[index + 1:]
        if not following:
            continue
        second = _LABEL.fullmatch(following[0][1])
        if not second or second.group("issue") is None or second.group("value"):
            continue
        values = following[1:]
        if values and _TABLE_OTHER.fullmatch(values[0][1]):
            values = values[1:]
        if len(values) < 2 or any(_LABEL.match(value) or _KNOWN_HEADER.match(value)
                                  or _VAT_COLUMN.fullmatch(value) for _, value in values[:2]):
            continue
        fields.setdefault("invoice", []).append(values[0][1])
        fields.setdefault("issue", []).append(values[1][1])
        headers.update((line_index, following[0][0]))
    return headers


def _vat_totals(lines: list[str]) -> tuple[dict[int, str], set[int]]:
    """Select the gross column of a vertical VAT summary, never its net column."""
    nonempty = [(index, line) for index, line in enumerate(lines) if line]
    totals, headers = {}, set()
    for offset, (index, line) in enumerate(nonempty):
        if not _VAT_SUMMARY.fullmatch(line):
            continue
        following = nonempty[offset + 1:]
        columns = []
        while following and (column := _VAT_COLUMN.fullmatch(following[0][1])):
            columns.append((following.pop(0)[0], column[1].casefold()))
        if columns:
            headers.update(column_index for column_index, _ in columns)
            gross = [position for position, (_, name) in enumerate(columns) if name == "brutto"]
            if len(gross) != 1 or not any(name == "netto" for _, name in columns) or len(following) < len(columns):
                continue
            candidates = [value for _, value in following[:len(columns)]]
            selected = gross[0]
        else:
            candidates = []
            for _, value in following:
                if _EXCLUDED.search(value):
                    break
                try:
                    parsed = _parse_amount(value)
                except _RejectedPayment:
                    break
                if parsed is None:
                    break
                candidates.append(value)
            if len(candidates) < 2:
                continue
            selected = len(candidates) - 1
        totals[index] = candidates[selected]
    return totals, headers


def extract_document(text: str, *, merchant: str, allow_unnumbered_receipt: bool = False,
                     pdf: bool = False) -> ImportedDocument | None:
    """Return a document only if the labelled values are safe and unambiguous."""
    if not isinstance(text, str) or len(text) > MAX_TEXT_BYTES:
        return None
    try:
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            return None
        lines = _normalized_invoice_lines(text)
    except (ReceiptError, UnicodeEncodeError):
        return None
    joined = "\n".join(lines)
    if _REJECT.search(joined) or _OUT_OF_SCOPE.search(joined) or not _DOCUMENT.search(joined):
        return None
    fields: dict[str, list[str]] = {}
    table_headers = _table_fields(lines, fields)
    vat_totals, vat_headers = _vat_totals(lines)
    seller_block = False
    buyer_seen = False
    following_values = _following_values(lines)
    explicit_seller = any(
        (match := _LABEL.match(line)) is not None and (match.group("seller") or "").casefold() == "sprzedawca"
        for line in lines
    )
    previous = ""
    for index, (line, following) in enumerate(zip(lines, following_values)):
        prior = previous
        if line:
            previous = line
        if index in table_headers or index in vat_headers:
            continue
        if _SETTLEMENT.search(prior) and (settlement := _SETTLEMENT_NUMBER.fullmatch(line)):
            fields.setdefault("invoice", []).append(settlement[1])
            fields.setdefault("issue", []).append(settlement[2])
            continue
        if _SELLER_BUYER.fullmatch(line):
            seller_block = False
            buyer_seen = True
            if not following or _LABEL.match(following):
                return None
            fields.setdefault("seller", []).append(following)
            seller_index = next(i for i in range(index + 1, len(lines)) if lines[i])
            for nip_index in range(seller_index + 1, len(lines)):
                nip_line = lines[nip_index]
                if not nip_line or _EU_VAT_LABEL.match(nip_line) or _EU_VAT_VALUE.fullmatch(nip_line):
                    continue
                nip_label = _LABEL.match(nip_line)
                if not nip_label or nip_label.group("nip") is None:
                    break
                value = nip_label.group("value") or following_values[nip_index]
                if _parse_nip(value) is None and (_EU_VAT_LABEL.match(value) or _EU_VAT_VALUE.fullmatch(value)):
                    continue
                fields.setdefault("nip", []).append(value)
                break
            continue
        if _EU_VAT_LABEL.match(line):
            continue
        match = _LABEL.match(line)
        if match is None:
            continue
        key = next(key for key in _LABELS if match.group(key) is not None)
        if key == "receipt" and match.group("receipt").casefold().startswith("paragon"):
            if match.group("receipt").casefold() == "paragon" and not match.group("value"):
                continue
            if not any(char.isdecimal() for char in match.group("value")):
                continue
            key = "paragon"
        if key == "buyer":
            seller_block = False
            buyer_seen = True
            continue
        if key == "issuer" and (explicit_seller or buyer_seen):
            seller_block = False
            continue
        if key == "seller" and match.group("seller").casefold() == "from" and buyer_seen:
            continue
        if key in ("seller", "issuer"):
            seller_block = True
        if key == "nip" and not seller_block:
            continue
        if not match.group("value") and index not in vat_totals and (
            not following or _LABEL.match(following) or _KNOWN_HEADER.match(following)
            or _VAT_COLUMN.fullmatch(following)
        ):
            continue
        value = (vat_totals.get(index) or match.group("value") or following).strip()
        if key == "seller":
            # A buyer label on the same line terminates the seller block.
            buyer = _BUYER_INLINE.search(value)
            if buyer:
                value = value[:buyer.start()]
                seller_block = False
                buyer_seen = True
            nip = _INLINE_NIP.search(value)
            if nip:
                fields.setdefault("nip", []).append(nip[1])
            value = re.split(r"\bNIP\s*:", value, maxsplit=1, flags=re.I)[0]
            value = _SELLER_ADDRESS.split(value, maxsplit=1)[0].strip()
        if key.startswith("total") and (_EXCLUDED.search(line) or _EXCLUDED.search(value)):
            continue
        if key.startswith("total") and (currency := _CURRENCY_LABEL.fullmatch(value)):
            value = f"{currency[1]} {currency[2] or following}"
        fields.setdefault(key, []).append(value)

    def one_number(key):
        values = [value.removeprefix("#").strip() for value in fields.get(key, [])]
        if not values:
            return None
        if any(not any(char.isdecimal() for char in value) for value in values):
            return None
        if key == "receipt":
            if any(not _RECEIPT_NUMBER.fullmatch(value) for value in values):
                return None
        elif any(not 1 <= len(value) <= 64 or not value.isprintable()
                 or _LABEL.match(value) or _safe_field(value) is None for value in values):
            return None
        return values[0] if len(set(values)) == 1 else None

    number = one_number("invoice")
    receipt_number = one_number("receipt")
    paragon_number = one_number("paragon")
    if ((fields.get("invoice") and number is None) or (fields.get("receipt") and receipt_number is None)
            or (fields.get("paragon") and paragon_number is None)
            or (receipt_number and paragon_number and receipt_number != paragon_number)):
        return None
    receipt_number = receipt_number or paragon_number
    number = number or receipt_number
    kind = "receipt" if receipt_number or _RECEIPT_HEADING.search(joined) or (
        not fields.get("invoice") and _PARAGON.search(joined)) else "invoice"
    if number is None and (kind != "receipt" or not allow_unnumbered_receipt):
        return None
    dates = {}
    for key in ("issue", "due", "paid_date"):
        values = fields.get(key, [])
        parsed = {_document_date(value) for value in values}
        if values and (None in parsed or len(parsed) != 1):
            return None
        dates[key] = next(iter(parsed)).isoformat() if parsed else None
    if dates["issue"] is None and kind == "receipt":
        dates["issue"] = dates["paid_date"]
    if dates["issue"] is None:
        return None
    total = None
    zero_due = False
    for key in ("total0", "total1", "total2", "total3"):
        if key == "total1" and "total0" in fields:
            continue
        values = fields.get(key, [])
        if values:
            try:
                parsed = {_parse_amount(value, labelled=True) for value in values}
            except _RejectedPayment:
                return None
            if None in parsed or len(parsed) != 1:
                return None
            total = parsed.pop()
            if total[0] == 0 and key in ("total0", "total1"):
                zero_due = True
                total = None
                continue
            if not Decimal(0) < total[0] <= Decimal("1000000"):
                return None
            break
    if total is None:
        return None
    sellers = fields.get("seller", []) or fields.get("issuer", [])
    if len(set(sellers)) > 1:
        return None
    seller = sellers[0] if sellers else merchant
    # Stripe documents often have a seller heading before their labelled fields.
    if not sellers and re.search(r"invoice number|receipt number|billing number", joined, re.I):
        first = next((line for line in lines if line), "")
        if first and not _GENERIC_SELLER.match(first) and not _DOCUMENT.search(first) and not _LABEL.match(first):
            safe_first = _safe_field(first) if len(first) <= 128 else None
            if safe_first:
                seller = safe_first
    seller = _safe_field(seller.strip()) if isinstance(seller, str) else None
    if not seller or len(seller) > 128:
        return None
    nips = set()
    for value in fields.get("nip", []):
        parsed = _parse_nip(value)
        if parsed is None and (_EU_VAT_LABEL.match(value) or _EU_VAT_VALUE.fullmatch(value)):
            continue
        nips.add(parsed)
    nip = next(iter(nips)) if len(nips) == 1 and None not in nips else None
    paid = bool(receipt_number or zero_due or dates["paid_date"] or _PAID.search(joined))
    amount = canonical_money(str(total[0]))
    items = settlement_items(lines, seller, number, amount) if pdf and kind == "invoice" and total[1] == "PLN" else ()
    return ImportedDocument(seller, nip, number, amount, total[1], dates["issue"], dates["due"],
                            kind, paid, receipt_number, items)
