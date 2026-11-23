"""Extract structured fields from sanitized Polish supplier invoice text."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re

from .analytics_core import canonical_money
from .receipt_parser import ReceiptError, redact_digit_field, redact_pan, valid_nip


@dataclass(frozen=True)
class ImportedInvoice:
    issuer: str
    nip: str | None
    invoice_number: str
    amount: str
    currency: str
    issue_date: str | None
    due_date: str


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", re.ASCII)
_BRAND = re.compile(
    r"^(?:(?:wystawca|sprzedawca)[ \t]*:[ \t]*)?"
    r"(?:(?P<respect>respect energy)|(?P<orange>orange)|"
    r"(?P<play>play|p4\s+sp)|(?P<tmobile>t[- ]mobile))\b",
    re.IGNORECASE | re.MULTILINE,
)
_ISSUERS = {"respect": "Respect Energy", "orange": "Orange", "play": "Play", "tmobile": "T-Mobile"}
_LABEL = re.compile(
    r"(?<!\w)(?:"
    r"(?P<invoice_number>numer (?:dokumentu|faktury)|nr[.]? (?:faktury|dokumentu)|faktura nr[.]?)"
    r"|(?P<amount>kwota(?: (?:do zap[łl]aty|brutto))?|do zap[łl]aty|razem brutto)"
    r"|(?P<issue_date>data (?:wystawienia|faktury|dokumentu))"
    r"|(?P<due_date>termin (?:p[łl]atno[śs]ci|zap[łl]aty)|p[łl]atne do)"
    r"|(?P<seller_nip>nip (?:sprzedawcy|wystawcy))"
    r"|(?P<nip>nip(?![ \t]+(?:nabywcy|odbiorcy|klienta)\b))"
    r")(?!\w)",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"[A-Za-z0-9][A-Za-z0-9./_-]{0,127}\Z", re.ASCII)
_AMOUNT = re.compile(
    r"([0-9]{1,9}|[0-9]{1,3}(?: [0-9]{3}){1,2})([,.][0-9]{1,2})?\s*(PLN|EUR|USD|GBP|CHF|zł)\Z",
    re.IGNORECASE,
)
_DATE_ISO = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z", re.ASCII)
_DATE_PL = re.compile(r"([0-9]{2})([-./])([0-9]{2})\2([0-9]{4})\Z", re.ASCII)


def _safe_field(value: str) -> str | None:
    """Apply v1a's per-field digit gate as well as its PAN/IBAN/PESEL redactor."""
    if not value or _EMAIL.search(value):
        return None
    if redact_digit_field(value) != value or redact_pan(value) != value:
        return None
    # v1a recognizes date and price tokens in complete lines. A single invoice
    # value must also fail closed when those delimiters split a PESEL.
    digits = "".join(char for char in value if char.isdecimal())
    if len(digits) == 11 and redact_pan(digits) != digits:
        return None
    return value


def _field_values(text: str) -> dict[str, set[str]]:
    labels = tuple(_LABEL.finditer(text))
    fields: dict[str, set[str]] = {}
    for index, match in enumerate(labels):
        end = labels[index + 1].start() if index + 1 < len(labels) else len(text)
        # Cells can be on separate lines. An empty value line must not let an
        # unrelated line become the value, including after v1a drops long lines.
        section = text[match.end():end].lstrip(" \t:").removeprefix("\n")
        value = section.split("\n", 1)[0].strip()
        if (safe := _safe_field(value)) is not None:
            fields.setdefault(match.lastgroup, set()).add(safe)
    return fields


def _unique(fields: dict[str, set[str]], key: str) -> str:
    values = fields.get(key, ())
    return next(iter(values)) if len(values) == 1 else ""


def _parse_date(value: str) -> str | None:
    if match := _DATE_PL.fullmatch(value):
        day, _, month, year = match.groups()
        value = f"{year}-{month}-{day}"
    if not _DATE_ISO.fullmatch(value):
        return None
    try:
        return _safe_field(date.fromisoformat(value).isoformat())
    except ValueError:
        return None


def _parse_nip(value: str) -> str | None:
    digits = value.removeprefix("PL").replace("-", "").replace(" ", "")
    return _safe_field(digits) if valid_nip(digits) else None


def extract_invoice(text: str) -> ImportedInvoice | None:
    """Recognize the four supported invoice templates after the sender trust gate."""
    try:
        clean = redact_pan(_EMAIL.sub(" ", redact_pan(text)))
    except ReceiptError:
        return None
    issuers = {_ISSUERS[match.lastgroup] for match in _BRAND.finditer(clean)}
    if len(issuers) != 1:
        return None
    issuer = issuers.pop()  # Fixed, digit-free product vocabulary.
    fields = _field_values(clean)
    invoice_number = _unique(fields, "invoice_number")
    due_date = _parse_date(_unique(fields, "due_date"))
    amount_match = _AMOUNT.fullmatch(_unique(fields, "amount"))
    if not _NUMBER.fullmatch(invoice_number) or due_date is None or amount_match is None:
        return None
    integer, fraction, currency = amount_match.groups()
    # The complete amount field already passed _safe_field, and canonicalizing
    # a decimal can only remove digits.
    amount = canonical_money(integer.replace(" ", "") + (fraction or "").replace(",", "."))
    currency = "PLN" if currency.casefold() == "zł" else currency.upper()  # Regex-closed vocabulary.
    nip = _parse_nip(_unique(fields, "seller_nip" if "seller_nip" in fields else "nip"))
    issue_date = _parse_date(_unique(fields, "issue_date"))
    if issue_date is not None and due_date < issue_date:
        return None
    return ImportedInvoice(issuer, nip, invoice_number, amount, currency, issue_date, due_date)
