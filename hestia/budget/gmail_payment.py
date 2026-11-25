"""Extract labelled payment totals and safe receipt fields from rendered emails."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import islice
import re
import unicodedata

from .gmail_address import canonical_domain
from .gmail_invoice import _MONTHS, _SKIPPED_LINE, _line_pairs, _normalized_invoice_lines
from .receipt_parser import MAX_LINE_CHARS, MAX_TEXT_BYTES, ReceiptError, _bound_raw_line, canonical_money, redact_pan


@dataclass(frozen=True)
class ImportedPayment:
    merchant: str
    description: str
    total_amount: str
    currency: str
    body_dates: tuple[date, ...]


_MARKER = re.compile(
    r"\b(?:dokończ\s+płatność|płatność\s+nie\s+została\s+(?:jeszcze\s+)?zakończona"
    r"|rozpoczęci[ae]\s+transakcji|oczekuje\s+na\s+płatność|nie\s+opłacono"
    r"|payment\s+failed|unable\s+to\s+process|was\s+declined|nieudana\s+płatność"
    r"|płatność\s+odrzucona|nie\s+udało\s+się\s+(?:pobrać|obciążyć)"
    r"|zwrot|refund|korekta|faktura\s+korygująca|credit\s+note|czeka\s+na\s+odbiór"
    r"|gotowe\s+do\s+odbioru|potwierdzenie\s+odbioru|została\s+wysłana|wysłaliśmy"
    r"|shipped|out\s+for\s+delivery)\b",
    re.IGNORECASE,
)
_LEGAL = re.compile(r"\b(?:kapitał\w*|KRS|zakładowego)\b", re.IGNORECASE)
_FEE = re.compile(
    r"(?:Dodatkowe\s+opłaty|Opłata\s+za\s+(?:realizację\s+transakcji|usługę)|Dostawa)(?=$|[:\s])",
    re.IGNORECASE,
)
_BULLET = re.compile(r"^[-*•]\s+")
_TOTAL = re.compile(
    r"(?:(?P<paid>Amount\s+paid|Kwota\s+zapłacona|Zapłacono|Paid)"
    r"|(?P<total>Kwota\s+transakcji|Do\s+zapłaty|Łącznie|Razem\s+do\s+zapłaty"
    r"|Suma\s+do\s+zapłaty|Total|Cena\s+z\s+dostawą)"
    r"|(?P<sum>Razem|Suma|Kwota\s+zamówienia|Kwota))(?=$|[:\s])\s*:?\s*",
    re.IGNORECASE,
)
_CARD = re.compile(r"(?<!\w)(?:[•]{4}|[*]{4}|x{4}|(?<!\S)-)\s+[0-9]{4}$", re.IGNORECASE)
_CURRENCY = r"(?:zł|PLN|EUR|€|USD|\$|GBP|£)"
_MONEY_NUMBER = (
    r"(?:[0-9]{1,3}(?: +[0-9]{3})+(?:[.,][0-9]{2})?"
    r"|[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{2})?"
    r"|[0-9]{1,3}(?:\.[0-9]{3})+(?:,[0-9]{2})?"
    r"|[0-9]+(?:[.,][0-9]{2})?)"
)
_SIGN = r"(?:[-−]\s*)?"
_AMOUNT = re.compile(
    rf"(?P<sign>{_SIGN})(?:(?P<prefix>{_CURRENCY})\s*(?P<inner_sign>{_SIGN})(?P<prefix_number>{_MONEY_NUMBER})"
    rf"|(?P<suffix_number>{_MONEY_NUMBER})\s*(?P<suffix>{_CURRENCY}))(?=$|[.,;]?\s+[^\W\d_])",
    re.IGNORECASE,
)
_MONEY_TOKEN = rf"{_SIGN}(?:{_CURRENCY}\s*{_SIGN}{_MONEY_NUMBER}|{_MONEY_NUMBER}\s*{_CURRENCY})"
_CONTAINS_AMOUNT = re.compile(rf"(?<![\w.,]){_MONEY_TOKEN}(?!\w|[.,][0-9])", re.IGNORECASE)
_LABEL_DELIMITER = r"(?:\s*:\s*|\s+)"
# Some rendered tables flatten the summary into one row. Split only the
# adjacent Total/Amount paid cells so redaction still sees the receipt header
# and its identifiers separately from the totals.
_FLAT_SUMMARY = re.compile(
    rf"^(Receipt\s+\#\S+.*?)\s+(Total{_LABEL_DELIMITER}{_MONEY_TOKEN})"
    rf"\s+(Amount\s+paid{_LABEL_DELIMITER}{_MONEY_TOKEN})$",
    re.IGNORECASE,
)
_DECIMAL = re.compile(r"[.,][0-9]{2}$")
_SEPARATORS = str.maketrans("", "", " .,")
_CURRENCIES = {"zł": "PLN", "€": "EUR", "$": "USD", "£": "GBP"}

_ORDER = re.compile(r"Opis\s+zamówienia\s+w\s+([^:]+):\s*(.*)", re.IGNORECASE)
_TITLE = re.compile(r"Tytuł\s*:\s*(.*)", re.IGNORECASE)
_PAYEE_LABEL = re.compile(r"(?:^Odbiorca\s+wpłaty\s*:\s*|\bpłatność\s+dla\s+)", re.IGNORECASE)
_PAYEE_HOST = re.compile(r"\bdla\s*-\s*(?:https?://)?([a-z0-9.-]+)(?=$|[/:?#\s])", re.IGNORECASE)
_CONTACT = re.compile(
    r"\b(?:[a-z][a-z0-9+.-]*://|www\.|mailto:)\S+|[^\s<>@]+@[^\s<>@]+",
    re.IGNORECASE,
)
_LONG_DIGITS = re.compile(r"\d{11,}")
_PAYEE_DIGITS = re.compile(r"\d{7,}")
_ORDER_REFERENCE = re.compile(r"Zamówienie 0{10}[1-9]", re.IGNORECASE)
_REDACTED = "[usunięto]"

_DATE_LABEL = re.compile(r"(?:Date\s+paid|Data)(?=$|[:\s])\s*:?\s*", re.IGNORECASE)
_PAID_DATE = re.compile(rf"\bPaid{_LABEL_DELIMITER}(?=\w)", re.IGNORECASE)
_DATE_END = r"(?=$|[\s,.;])"
_ISO_DATE = re.compile(rf"([0-9]{{4}})-([0-9]{{2}})-([0-9]{{2}}){_DATE_END}")
_NUMERIC_DATE = re.compile(rf"([0-9]{{1,2}})([./-])([0-9]{{1,2}})\2([0-9]{{4}}){_DATE_END}")
_POLISH_DATE = re.compile(rf"([0-9]{{1,2}})\s+([^\W\d_]+)[.]?\s+([0-9]{{4}}){_DATE_END}")
_ENGLISH_DATE = re.compile(rf"([^\W\d_]+)[.]?\s+([0-9]{{1,2}}),?\s+([0-9]{{4}}){_DATE_END}")
_ENGLISH_MONTHS = dict(zip(
    "january february march april may june july august september october november december".split(),
    range(1, 13),
))
_ENGLISH_MONTHS.update({month[:3]: number for month, number in _ENGLISH_MONTHS.items()})
_POLISH_MONTHS = {month: int(number) for month, number in _MONTHS.items()}
_POLISH_MONTHS.update({month[:3]: number for month, number in _POLISH_MONTHS.items()})


def _following_values(lines: list[str]) -> list[str]:
    values = []
    following = ""
    for line in reversed(lines):
        values.append(following)
        following = line or following
    return values[::-1]


def _parse_amount(value: str) -> tuple[Decimal, str] | None:
    match = _AMOUNT.match(value)
    if match is None:
        return None
    number = match["prefix_number"] or match["suffix_number"]
    currency = (match["prefix"] or match["suffix"]).casefold()
    digits = number.translate(_SEPARATORS)
    if _DECIMAL.search(number):
        digits = f"{digits[:-2]}.{digits[-2:]}"
    sign = "-" if match["sign"] or match["inner_sign"] else ""
    return Decimal(sign + digits), _CURRENCIES.get(currency, currency.upper())


def _payment_total(lines: list[str], following: list[str]) -> tuple[str, str] | None:
    totals: dict[str, set[tuple[Decimal, str]]] = {key: set() for key in ("paid", "total", "sum", "card")}
    for (line, adjacent), next_value in zip(_line_pairs(lines), following):
        label_line = _BULLET.sub("", line)
        if _FEE.match(label_line):
            continue
        if label := _TOTAL.match(label_line):
            level = label.lastgroup
            value = label_line[label.end():] or next_value
        elif _CARD.search(line):
            level, value = "card", adjacent
        else:
            continue
        if parsed := _parse_amount(value):
            if parsed[0].is_signed():
                return None
            totals[level].add(parsed)
    for values in totals.values():
        if values:
            if len(values) != 1:
                return None
            amount, currency = values.pop()
            if 0 < amount <= 1_000_000:
                return canonical_money(format(amount, "f")), currency
            return None
    return None


def _parse_date(value: str) -> date | None:
    if match := _ISO_DATE.match(value):
        year, month, day = map(int, match.groups())
    elif match := _NUMERIC_DATE.match(value):
        day, month, year = map(int, (match[1], match[3], match[4]))
    elif match := _POLISH_DATE.match(value):
        day, month, year = int(match[1]), _POLISH_MONTHS.get(match[2].casefold(), 0), int(match[3])
    elif match := _ENGLISH_DATE.match(value):
        day, month, year = int(match[2]), _ENGLISH_MONTHS.get(match[1].casefold(), 0), int(match[3])
    else:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _body_dates(lines: list[str], following: list[str]) -> tuple[date, ...]:
    dates = set()
    for line, next_value in zip(lines, following):
        line = _BULLET.sub("", line)
        if label := _DATE_LABEL.match(line):
            if parsed := _parse_date(line[label.end():] or next_value):
                dates.add(parsed)
        for label in _PAID_DATE.finditer(line):
            if parsed := _parse_date(line[label.end():]):
                dates.add(parsed)
    return tuple(sorted(dates))


def _clean_field(value: str) -> str:
    return " ".join(_CONTACT.sub(" ", value.replace(_REDACTED, " ")).split())


def _description(value: str) -> str:
    value = _clean_field(value)
    # A zero-padded synthetic order reference in the fixture is neither an
    # identity number nor free-form digits. It must still pass redact_pan.
    if not _ORDER_REFERENCE.fullmatch(value):
        value = " ".join(_LONG_DIGITS.sub(" ", value).split())
    value = redact_pan(value) if value else ""
    return "" if value == _REDACTED else value[:128].strip()


def _payee(value: str) -> str:
    value = _clean_field(value)
    if 2 <= len(value) <= 128 and not _PAYEE_DIGITS.search(value) and redact_pan(value) != _REDACTED:
        return value
    return ""


def _payee_candidates(line: str) -> list[str]:
    candidates = []
    if host := _PAYEE_HOST.search(line):
        if domain := canonical_domain(host[1]):
            candidates.append(domain)
        line = line[:host.start()]
    labels = list(_PAYEE_LABEL.finditer(line))
    boundaries = [label.start() for label in labels[1:]] + [len(line)]
    return candidates + [line[label.end():end] for label, end in zip(labels, boundaries)]


def _receipt_fields(lines: list[str], subject: str, merchant: str, gateway: bool) -> tuple[str, str] | None:
    descriptions: list[str] = []
    titles: list[str] = []
    payees: dict[str, str] = {}
    for line in lines:
        line = _BULLET.sub("", line)
        order = _ORDER.fullmatch(line)
        if order:
            descriptions.append(order[2])
        if title := _TITLE.fullmatch(line):
            titles.append(title[1])
        if not gateway:
            continue
        for candidate in [order[1]] if order else _payee_candidates(line):
            if clean := _payee(candidate):
                payees.setdefault(clean.casefold(), clean)
    if len(payees) > 1:
        return None
    description = next((value for item in (*descriptions, *titles, subject) if (value := _description(item))), "Płatność")
    return next(iter(payees.values()), merchant), description


def extract_payment(text: str, *, subject: str, merchant: str, gateway: bool) -> ImportedPayment | None:
    """Read only bounded, redacted fields from an authenticated payment email."""
    if not isinstance(text, str) or len(text) > MAX_TEXT_BYTES:
        return None
    try:
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            return None
        visible = (char for char in subject if unicodedata.category(char) != "Cf")
        subject = "".join(islice(visible, MAX_LINE_CHARS))
        subject = " ".join(unicodedata.normalize("NFKC", _bound_raw_line(subject))[:256].split())
        raw = ["" if _LEGAL.search(line) else line for line in _normalized_invoice_lines(text)]
        # Rejection predicates do not extract fields. Check the original line
        # shape so identifiers cannot hide warnings and table splits do not
        # turn the priced part of an item into an independent warning.
        if _MARKER.search(subject) or any(
            len(line) <= 120 and _MARKER.search(line) and not _CONTAINS_AMOUNT.search(line) for line in raw
        ):
            return None
        expanded = [part for line in raw for part in _FLAT_SUMMARY.sub(r"\1\n\2\n\3", line).split("\n")]
        clean = redact_pan("\n".join(expanded)).split("\n")
        lines = [_SKIPPED_LINE if line == _SKIPPED_LINE else value for line, value in zip(expanded, clean)]
        safe_subject = redact_pan(subject) if subject else ""
        following = _following_values(lines)
        if (total := _payment_total(lines, following)) is None:
            return None
        if (fields := _receipt_fields(lines, safe_subject, merchant, gateway)) is None:
            return None
        return ImportedPayment(*fields, *total, _body_dates(lines, following))
    except (ReceiptError, UnicodeEncodeError):
        return None
