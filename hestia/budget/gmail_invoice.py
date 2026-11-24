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
    r"(?P<respect>respect[ \t]+energy)\b|"
    r"(?P<orange>orange[ \t]+polska[ \t]+s[.]a[.])|"
    r"(?P<play>p4[ \t]+sp[.][ \t]+z[ \t]+o[.]o[.])|"
    r"(?P<tmobile>t[- ]mobile[ \t]+polska[ \t]+s[.]a[.])",
    re.IGNORECASE,
)
_ISSUERS = {"respect": "Respect Energy", "orange": "Orange", "play": "Play", "tmobile": "T-Mobile"}
_FORWARDED_INVOICE_NUMBERS = {
    "Respect Energy": re.compile(r"RE/(?:[0-9]+/){1,2}[0-9]+", re.ASCII),
    "Orange": re.compile(r"[0-9]{14}", re.ASCII),
    "Play": re.compile(r"F/[0-9]{8}/[0-9]{2}/[0-9]{2}", re.ASCII),
    "T-Mobile": re.compile(r"[0-9]{12}", re.ASCII),
}
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
_DATE_VALUE = r"[0-9]{2}[.][0-9]{2}[.][0-9]{4}"
_PLN_VALUE = r"(?:[0-9]{1,9}|[0-9]{1,3}(?: [0-9]{3}){1,2})(?:,[0-9]{1,2})?[ \t]*zł"
_PLN_AMOUNT = re.compile(
    r"([0-9]{1,9}|[0-9]{1,3}(?: [0-9]{3}){1,2})(,[0-9]{1,2})?[ \t]*zł\Z",
    re.IGNORECASE,
)
_SELLER_NIP = re.compile(r"^NIP:?[ \t]*((?:PL)?[0-9][0-9 -]{8,16}[0-9])[ \t]*$", re.IGNORECASE | re.MULTILINE)

_TMOBILE_CORRECTION = re.compile(r"FAKTURA KORYGUJĄCA|Numer faktury korygującej", re.IGNORECASE)
_TMOBILE_NUMBER = re.compile(r"^Numer faktury:[ \t]*(\S+)[ \t]*$", re.IGNORECASE | re.MULTILINE)
_TMOBILE_DUE = re.compile(rf"^Termin płatności:[ \t]*({_DATE_VALUE})[ \t]*$", re.IGNORECASE | re.MULTILINE)
_TMOBILE_ISSUE = re.compile(
    rf"^Data i miejsce wystawienia:[ \t]*({_DATE_VALUE})(?:,[ \t]*[^\n]*)?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
# Page footer present in both the 2025 and 2026 layouts. The 2025 layout has no
# standalone "Numer faktury:" or "Data i miejsce wystawienia:" line.
_TMOBILE_FOOTER = re.compile(
    rf"Numer faktury:[ \t]*([0-9]{{12}})[ \t]+Okres rozliczeniowy:[^\n]*?"
    rf"Data wystawienia:[ \t]*({_DATE_VALUE})[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_TMOBILE_AMOUNT = re.compile(
    rf"^A\+C[ \t]+Prosimy o wpłatę[ \t]+({_PLN_VALUE})[ \t]+brutto i[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

_PLAY_NUMBER = re.compile(r"^Numer faktury[ \t]*\n[ \t]*(\S+)[ \t]*$", re.IGNORECASE | re.MULTILINE)
_PLAY_ISSUE = re.compile(
    rf"^Data wystawienia[ \t]*\n[ \t]*({_DATE_VALUE})[ \t]*$", re.IGNORECASE | re.MULTILINE
)
_PLAY_DUE = re.compile(
    rf"^TERMIN PŁATNOŚCI:?[ \t]*(?:\n[ \t]*)?({_DATE_VALUE})(?![0-9])",
    re.IGNORECASE | re.MULTILINE,
)
_PLAY_PAYMENT = re.compile(rf"PROSIMY O WPŁATĘ:[ \t]*({_PLN_VALUE})[ \t]*$", re.IGNORECASE | re.MULTILINE)
_PLAY_SUMMARY = re.compile(
    rf"^PODSUMOWANIE KWOTY DO ZAPŁATY:[ \t]*({_PLN_VALUE})[ \t]*$", re.IGNORECASE | re.MULTILINE
)

_ORANGE_NUMBER = re.compile(r"^Do faktury VAT numer:[ \t]*(\S+)[ \t]*$", re.IGNORECASE | re.MULTILINE)
_ORANGE_TRANSFER = re.compile(r"^Tytuł przelewu:[ \t]*(\S+)[ \t]*$", re.IGNORECASE | re.MULTILINE)
_ORANGE_DUE = re.compile(rf"^Zapłać do:[ \t]*({_DATE_VALUE})[ \t]*$", re.IGNORECASE | re.MULTILINE)
_ORANGE_ISSUE = re.compile(
    r"^Data wystawienia:[ \t]*([0-9]{2})[ \t]+"
    r"(stycznia|lutego|marca|kwietnia|maja|czerwca|lipca|sierpnia|września|października|listopada|grudnia)"
    r"[ \t]+([0-9]{4})[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_ORANGE_AMOUNT = re.compile(
    rf"^Prosimy[ \t]*\no wpłatę do:[ \t]*\n{_DATE_VALUE}[ \t]*\n({_PLN_VALUE})[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_MONTHS = {
    month: f"{number:02}"
    for number, month in enumerate(
        ("stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca", "sierpnia", "września",
         "października", "listopada", "grudnia"),
        1,
    )
}


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


def _safe_invoice_number(value: str, pattern: re.Pattern[str]) -> str | None:
    # These fixed issuer formats are the scoped exception to the generic digit
    # gate. Anchored labels ensure bank-account lines never supply a value.
    return value if pattern.fullmatch(value) else None


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


def _one(pattern: re.Pattern[str], text: str):
    values = set(pattern.findall(text))
    return next(iter(values)) if len(values) == 1 else None


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


def _parse_pln_amount(value: str) -> str | None:
    if _safe_field(value) is None:
        return None
    match = _PLN_AMOUNT.fullmatch(value)
    if match is None:
        return None
    integer, fraction = match.groups()
    return canonical_money(integer.replace(" ", "") + (fraction or "").replace(",", "."))


def _pln_invoice(issuer, number, nip, amount_value, issue_date, due_date):
    amount = _parse_pln_amount(amount_value or "")
    if number is None or due_date is None or amount is None:
        return None
    if issue_date is not None and due_date < issue_date:
        return None
    return ImportedInvoice(issuer, _parse_nip(nip or ""), number, amount, "PLN", issue_date, due_date)


def _extract_respect(_raw: str, clean: str) -> ImportedInvoice | None:
    fields = _field_values(clean)
    invoice_number = _unique(fields, "invoice_number")
    due_date = _parse_date(_unique(fields, "due_date"))
    amount_match = _AMOUNT.fullmatch(_unique(fields, "amount"))
    if not _NUMBER.fullmatch(invoice_number) or due_date is None or amount_match is None:
        return None
    integer, fraction, currency = amount_match.groups()
    amount = canonical_money(integer.replace(" ", "") + (fraction or "").replace(",", "."))
    currency = "PLN" if currency.casefold() == "zł" else currency.upper()
    nip = _parse_nip(_unique(fields, "seller_nip" if "seller_nip" in fields else "nip"))
    issue_date = _parse_date(_unique(fields, "issue_date"))
    if issue_date is not None and due_date < issue_date:
        return None
    return ImportedInvoice("Respect Energy", nip, invoice_number, amount, currency, issue_date, due_date)


def _extract_tmobile(raw: str, clean: str) -> ImportedInvoice | None:
    if _TMOBILE_CORRECTION.search(raw):
        return None
    footer = _TMOBILE_FOOTER.findall(raw)
    numbers = set(_TMOBILE_NUMBER.findall(raw)) | {number for number, _ in footer}
    issues = set(_TMOBILE_ISSUE.findall(clean)) | {issued for _, issued in footer}
    number = _safe_invoice_number(
        next(iter(numbers)) if len(numbers) == 1 else "", _FORWARDED_INVOICE_NUMBERS["T-Mobile"]
    )
    due_date = _parse_date(_one(_TMOBILE_DUE, clean) or "")
    issue_date = _parse_date(next(iter(issues)) if len(issues) == 1 else "")
    return _pln_invoice(
        "T-Mobile", number, _one(_SELLER_NIP, clean), _one(_TMOBILE_AMOUNT, clean), issue_date, due_date
    )


def _extract_play(raw: str, clean: str) -> ImportedInvoice | None:
    number = _safe_invoice_number(_one(_PLAY_NUMBER, raw) or "", _FORWARDED_INVOICE_NUMBERS["Play"])
    due_date = _parse_date(_one(_PLAY_DUE, clean) or "")
    issue_date = _parse_date(_one(_PLAY_ISSUE, clean) or "")
    payments = set(_PLAY_PAYMENT.findall(clean))
    summaries = set(_PLAY_SUMMARY.findall(clean))
    if len(payments) != 1 or summaries - payments:
        return None
    payment = next(iter(payments))
    return _pln_invoice("Play", number, _one(_SELLER_NIP, clean), payment, issue_date, due_date)


def _extract_orange(raw: str, clean: str) -> ImportedInvoice | None:
    numbers = set(_ORANGE_NUMBER.findall(raw)) | set(_ORANGE_TRANSFER.findall(raw))
    number = _safe_invoice_number(next(iter(numbers)) if len(numbers) == 1 else "",
                                  _FORWARDED_INVOICE_NUMBERS["Orange"])
    due_date = _parse_date(_one(_ORANGE_DUE, clean) or "")
    issue_parts = _one(_ORANGE_ISSUE, clean)
    issue_date = None
    if issue_parts is not None:
        day, month, year = issue_parts
        issue_date = _parse_date(f"{day}.{_MONTHS[month.casefold()]}.{year}")
    return _pln_invoice(
        "Orange", number, _one(_SELLER_NIP, clean), _one(_ORANGE_AMOUNT, clean), issue_date, due_date
    )


_EXTRACTORS = {
    "Respect Energy": _extract_respect,
    "Orange": _extract_orange,
    "Play": _extract_play,
    "T-Mobile": _extract_tmobile,
}


def extract_invoice(text: str) -> ImportedInvoice | None:
    """Recognize one supported issuer and apply only that issuer's template."""
    try:
        clean = redact_pan(_EMAIL.sub(" ", redact_pan(text)))
    except ReceiptError:
        return None
    issuers = {_ISSUERS[match.lastgroup] for match in _BRAND.finditer(clean)}
    if len(issuers) != 1:
        return None
    issuer = issuers.pop()
    raw = text.replace("\r\n", "\n").replace("\r", "\n")
    return _EXTRACTORS[issuer](raw, clean)


def has_provider_invoice_number(text: str) -> bool:
    """Require a structured invoice with the recognized supplier's number format."""
    invoice = extract_invoice(text)
    return invoice is not None and bool(_FORWARDED_INVOICE_NUMBERS[invoice.issuer].fullmatch(invoice.invoice_number))
