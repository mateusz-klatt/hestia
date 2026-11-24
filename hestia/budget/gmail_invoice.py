"""Extract structured fields from sanitized Polish supplier invoice text."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
import re
import unicodedata

from .analytics_core import canonical_money
from .receipt_parser import MAX_LINE_CHARS, ReceiptError, redact_digit_field, redact_pan, valid_nip


@dataclass(frozen=True)
class ImportedInvoice:
    issuer: str
    nip: str | None
    invoice_number: str
    amount: str
    currency: str
    issue_date: str | None
    due_date: str


_EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", re.ASCII)
_BRAND = re.compile(
    r"(?P<respect>respect[ \t]+energy)\b|"
    r"(?P<orange>orange[ \t]+polska[ \t]+s[.]a[.])|"
    r"(?P<play>p4[ \t]+sp[.][ \t]+z[ \t]+o[.]o[.])|"
    r"(?P<tmobile>t[- ]mobile[ \t]+polska[ \t]+s[.]a[.])",
    re.IGNORECASE,
)
_ISSUERS = {"respect": "Respect Energy", "orange": "Orange", "play": "Play", "tmobile": "T-Mobile"}
# The owner-local DB keeps invoice numbers in plaintext. T-Mobile's 12 digits
# are outside PAN lengths. Orange's 14 digits must agree with each transfer
# title present. Neither 26-digit bank accounts nor 11-digit PESELs fit these formats.
_FORWARDED_INVOICE_NUMBERS = {
    "Respect Energy": re.compile(r"RE/(?:[0-9]+/){1,2}[0-9]+", re.ASCII),
    "Orange": re.compile(r"[0-9]{14}", re.ASCII),
    "Play": re.compile(r"F/[0-9]{8}/[0-9]{2}/[0-9]{2}", re.ASCII),
    "T-Mobile": re.compile(r"[0-9]{12}", re.ASCII),
}
_ACCOUNT_WORD = re.compile(r"rachunk|konto|IBAN", re.IGNORECASE)
# "Korekta" alone marks a correction only as a heading line. Regular invoices
# list prorated adjustments such as "Usługi telekomunikacyjne - korekta".
_CORRECTION = re.compile(
    r"\b(?:faktura\s+korygująca|faktury\s+korygującej)\b|^[ \t]*korekta(?:[ \t]+faktury\b[^\n]*)?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_SKIPPED_LINE = "\0"
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
_DOT_DATE = re.compile(_DATE_VALUE)
_PLN_VALUE = r"(?:[0-9]{1,9}|[0-9]{1,3}(?: [0-9]{3}){1,2})(?:,[0-9]{1,2})?[ \t]*zł"
_PLN_AMOUNT = re.compile(
    r"([0-9]{1,9}|[0-9]{1,3}(?: [0-9]{3}){1,2})(,[0-9]{1,2})?[ \t]*zł\Z",
    re.IGNORECASE,
)
_SELLER_NIP = re.compile(r"NIP:?[ \t]*((?:PL)?[0-9][0-9 -]{8,16}[0-9])", re.IGNORECASE)

_TMOBILE_NUMBER = re.compile(r"Numer faktury:[ \t]*(.*)", re.IGNORECASE)
_TMOBILE_HEADER = re.compile(r"Faktura nr:[ \t]*(\S+)[ \t]+z dnia[ \t]*(.*)", re.IGNORECASE)
_TMOBILE_DUE = re.compile(rf"Termin płatności:[ \t]*({_DATE_VALUE})", re.IGNORECASE)
_TMOBILE_ISSUE = re.compile(
    r"Data i miejsce wystawienia:[ \t]*([^,\s]+)(?:,.*)?", re.IGNORECASE,
)
# Both layouts print the page footer. The 2025 layout also prints the
# 'Faktura nr' header in place of the standalone number and issue date fields.
_TMOBILE_FOOTER = re.compile(
    r"(?:Numer Klienta:[ \t]*\S+[ \t]+)?Numer faktury:[ \t]*(\S+)[ \t]+"
    r"Okres rozliczeniowy:.*?\bData wystawienia:[ \t]*(.*)", re.IGNORECASE,
)
_TMOBILE_AMOUNT = re.compile(
    rf"A\+C[ \t]+Prosimy o wpłatę[ \t]+({_PLN_VALUE})[ \t]+brutto i", re.IGNORECASE,
)

_PLAY_NUMBER_LABEL = re.compile(r"Numer faktury", re.IGNORECASE)
_PLAY_ISSUE_LABEL = re.compile(r"Data wystawienia", re.IGNORECASE)
_PLAY_DUE_LABEL = re.compile(r"TERMIN PŁATNOŚCI:?[ \t]*(.*)", re.IGNORECASE)
_PLAY_DUE = re.compile(
    rf"({_DATE_VALUE})(?:[ \t]*(?:Numer konta bankowego:?|PROSIMY O WPŁATĘ:.*))?", re.IGNORECASE,
)
_PLAY_PAYMENT = re.compile(
    rf"(?:TERMIN PŁATNOŚCI:?[ \t]*{_DATE_VALUE}[ \t]+)?PROSIMY O WPŁATĘ:[ \t]*({_PLN_VALUE})", re.IGNORECASE,
)
_PLAY_SUMMARY = re.compile(r"PODSUMOWANIE KWOTY DO ZAPŁATY\b:?[ \t]*(.*)", re.IGNORECASE)

_ORANGE_NUMBER = re.compile(r"Do faktury VAT numer:[ \t]*(.*)", re.IGNORECASE)
_ORANGE_TRANSFER = re.compile(r"Tytuł przelewu\b:?[ \t]*(.*)", re.IGNORECASE)
_ORANGE_DUE = re.compile(rf"Zapłać do:[ \t]*({_DATE_VALUE})", re.IGNORECASE)
_ORANGE_ISSUE = re.compile(r"data wystawienia:[ \t]*([0-9]{2})[ \t]+(\S+)[ \t]+([0-9]{4})")
_ORANGE_PAYMENT_START = re.compile(r"Prosimy", re.IGNORECASE)
_ORANGE_PAYMENT_LABEL = re.compile(r"o wpłatę do:", re.IGNORECASE)
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


def _bounded_lines(text: str, *, skipped: str = "") -> list[str]:
    # Keep a placeholder in place of a dropped line to preserve field adjacency.
    bounded = []
    for line in text.splitlines():
        if len(line) <= MAX_LINE_CHARS:
            bounded.append(line.strip(" \t"))
        else:
            bounded.append("" if line.isspace() else skipped)
    return bounded


def _line_pairs(lines: list[str]) -> Iterable[tuple[str, str]]:
    return zip(lines, [*lines[1:], ""])


def _matches(pattern: re.Pattern[str], lines: Iterable[str]) -> Iterable[re.Match[str]]:
    for line in lines:
        if match := pattern.fullmatch(line):
            yield match


def _one_value(values: Iterable[str | None]) -> str | None:
    distinct = set(values)
    return next(iter(distinct)) if len(distinct) == 1 else None


def _one(pattern: re.Pattern[str], lines: Iterable[str]) -> str | None:
    return _one_value(match[1] for match in _matches(pattern, lines))


def _scoped_number(issuer: str, sources: Iterable[tuple[str, str]]) -> str | None:
    pattern = _FORWARDED_INVOICE_NUMBERS[issuer]
    numbers = set()
    for value, line in sources:
        if _ACCOUNT_WORD.search(line) or not pattern.fullmatch(value):
            return None
        numbers.add(value)
    return _one_value(numbers)


def _field_values(lines: list[str]) -> dict[str, set[str]]:
    fields: dict[str, set[str]] = {}
    for line, next_line in _line_pairs(lines):
        labels = tuple(_LABEL.finditer(line))
        for index, match in enumerate(labels):
            following = labels[index + 1] if index + 1 < len(labels) else None
            value = line[match.end():following.start() if following else len(line)].lstrip(" \t:")
            if not value and following is None:
                next_label = _LABEL.search(next_line)
                value = next_line[:next_label.start() if next_label else len(next_line)]
            if (safe := _safe_field(value.strip())) is not None:
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


def _parse_pln_amount(value: str | None) -> str | None:
    if value is None:
        return None
    value = unicodedata.normalize("NFKC", value)
    if _safe_field(value) is None:
        return None
    match = _PLN_AMOUNT.fullmatch(value)
    if match is None:
        return None
    integer, fraction = match.groups()
    return canonical_money(integer.replace(" ", "") + (fraction or "").replace(",", "."))


def _pln_invoice(
    issuer: str, number: str | None, nip: str | None, amount: str | None,
    issue_date: str | None, due_date: str | None,
) -> ImportedInvoice | None:
    if number is None or due_date is None or amount is None:
        return None
    if issue_date is not None and due_date < issue_date:
        return None
    return ImportedInvoice(issuer, _parse_nip(nip or ""), number, amount, "PLN", issue_date, due_date)


def _extract_respect(_raw: list[str], clean: list[str]) -> ImportedInvoice | None:
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


def _extract_tmobile(raw: list[str], clean: list[str]) -> ImportedInvoice | None:
    number_sources = []
    issues = {match[1] for match in _matches(_TMOBILE_ISSUE, raw)}
    for line in raw:
        if match := _TMOBILE_FOOTER.fullmatch(line) or _TMOBILE_HEADER.fullmatch(line):
            number_sources.append((match[1], line))
            issues.add(match[2])
        elif match := _TMOBILE_NUMBER.fullmatch(line):
            number_sources.append((match[1], line))
    number = _scoped_number("T-Mobile", number_sources)
    issue_value = _one_value(issues)
    if issue_value is None or not _DOT_DATE.fullmatch(issue_value):
        return None
    issue_date = _parse_date(issue_value)
    if issue_date is None:
        return None
    due_date = _parse_date(_one(_TMOBILE_DUE, clean) or "")
    amount = _parse_pln_amount(_one(_TMOBILE_AMOUNT, clean))
    return _pln_invoice(
        "T-Mobile", number, _one(_SELLER_NIP, clean), amount, issue_date, due_date
    )


def _extract_play(raw: list[str], clean: list[str]) -> ImportedInvoice | None:
    number = _scoped_number("Play", (
        (value, value) for label, value in _line_pairs(raw)
        if _PLAY_NUMBER_LABEL.fullmatch(label) and value not in ("", _SKIPPED_LINE)
    ))
    due_values = (
        match[1] or value for label, value in _line_pairs(clean) if (match := _PLAY_DUE_LABEL.fullmatch(label))
    )
    issue_values = (value for label, value in _line_pairs(clean) if _PLAY_ISSUE_LABEL.fullmatch(label))
    due_date = _parse_date(_one(_PLAY_DUE, due_values) or "")
    issue_date = _parse_date(_one_value(match[0] for match in _matches(_DOT_DATE, issue_values)) or "")
    payment = _one_value(_parse_pln_amount(match[1]) for match in _matches(_PLAY_PAYMENT, clean))
    if payment is None or any(_parse_pln_amount(match[1]) != payment for match in _matches(_PLAY_SUMMARY, raw)):
        return None
    return _pln_invoice("Play", number, _one(_SELLER_NIP, clean), payment, issue_date, due_date)


def _extract_orange(raw: list[str], clean: list[str]) -> ImportedInvoice | None:
    number = _scoped_number("Orange", ((match[1], match.string) for match in _matches(_ORANGE_NUMBER, raw)))
    if number is None or any(match[1] != number for match in _matches(_ORANGE_TRANSFER, raw)):
        return None
    payment_blocks = {
        (day, amount)
        for start, label, day, amount in zip(clean, clean[1:], clean[2:], clean[3:])
        if _ORANGE_PAYMENT_START.fullmatch(start) and _ORANGE_PAYMENT_LABEL.fullmatch(label)
    }
    if len(payment_blocks) != 1:
        return None
    payment_date, amount_value = payment_blocks.pop()
    if payment_date != _one(_ORANGE_DUE, clean):
        return None
    due_date = _parse_date(payment_date)
    issue_parts = {match.groups() for match in _matches(_ORANGE_ISSUE, (line.casefold() for line in clean))}
    issue_date = None
    if len(issue_parts) == 1:
        day, month, year = issue_parts.pop()
        if month_number := _MONTHS.get(month):
            issue_date = _parse_date(f"{day}.{month_number}.{year}")
    amount = _parse_pln_amount(amount_value)
    return _pln_invoice("Orange", number, _one(_SELLER_NIP, clean), amount, issue_date, due_date)


_EXTRACTORS = {
    "Respect Energy": _extract_respect,
    "Orange": _extract_orange,
    "Play": _extract_play,
    "T-Mobile": _extract_tmobile,
}


def extract_invoice(text: str) -> ImportedInvoice | None:
    """Recognize one supported issuer and apply only that issuer's template."""
    try:
        sanitized = _bounded_lines(redact_pan(text))
        clean = _bounded_lines(redact_pan("\n".join(_EMAIL.sub(" ", line) for line in sanitized)))
    except ReceiptError:
        return None
    # Keep skipped lines from creating a correction marker across unrelated lines.
    raw = _bounded_lines(text, skipped=_SKIPPED_LINE)
    if _CORRECTION.search("\n".join(raw)):
        return None
    issuers = {_ISSUERS[match.lastgroup] for line in clean for match in _BRAND.finditer(line)}
    if len(issuers) != 1:
        return None
    issuer = issuers.pop()
    return _EXTRACTORS[issuer](raw, clean)


def has_provider_invoice_number(text: str) -> bool:
    """Require a structured invoice with the recognized supplier's number format."""
    invoice = extract_invoice(text)
    return invoice is not None and bool(_FORWARDED_INVOICE_NUMBERS[invoice.issuer].fullmatch(invoice.invoice_number))
