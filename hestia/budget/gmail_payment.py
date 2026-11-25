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
from .receipt_parser import (MAX_LINE_CHARS, MAX_TEXT_BYTES, ReceiptError, _normalize_receipt_text,
                             canonical_money, redact_digit_field, redact_pan)


@dataclass(frozen=True)
class ImportedPayment:
    merchant: str
    description: str
    total_amount: str
    currency: str
    body_dates: tuple[date, ...]
    order_reference: str | None = None


@dataclass(frozen=True)
class PaymentInspection:
    payment: ImportedPayment | None = None
    missing_total: bool = False


class _RejectedPayment(ValueError):
    """An explicit rejection must not fall through to another MIME representation."""


_MARKER = re.compile(
    r"\b(?:dokończ\s+płatność|płatność\s+nie\s+została\s+(?:jeszcze\s+)?zakończona"
    r"|oczekuje\s+na\s+płatność|nie\s+opłacono"
    r"|payment\s+failed|unable\s+to\s+process|was\s+declined|nieudana\s+płatność"
    r"|płatność\s+odrzucona|nie\s+udało\s+się\s+(?:pobrać|obciążyć)"
    r"|zwrot|refund|korekta|faktura\s+korygująca|credit\s+note|czeka\s+na\s+odbiór"
    r"|gotowe\s+do\s+odbioru|potwierdzenie\s+odbioru|została\s+wysłana|wysłaliśmy"
    r"|shipped|out\s+for\s+delivery)\b",
    re.IGNORECASE,
)
_STARTED = re.compile(r"\brozpoczęci[ae]\s++transakcji\b", re.IGNORECASE)
_STARTED_NOTICE = re.compile(r"Potwierdzenie\s++rozpoczęcia\s++transakcji\b", re.IGNORECASE)
_BODY_FIELD = re.compile(r"[^:]++:(?!//|(?<=[0-9]:)[0-9])\s*+\S")
_LEGAL = re.compile(r"\b(?:kapitał\w*|KRS|zakładowego)\b", re.IGNORECASE)
_INLINE_TAG = re.compile(
    r"</?(?:p|b|strong|span|div|br|td|tr|th|table|tbody|font)(?: [^<>]*+)?/?>", re.IGNORECASE,
)
_FEE = re.compile(
    r"(?:Dodatkowe\s+opłaty|Opłata\s+za\s+(?:realizację\s+transakcji|usługę)"
    r"|Koszt\s++dostawy|Dostawa)(?=$|[:\s])",
    re.IGNORECASE,
)
_BULLET = re.compile(r"^[-*•]\s+")
_TOTAL = re.compile(
    r"(?:(?P<charged>Charged)|(?P<paid>Amount\s+paid|Kwota\s+zapłacona|Zapłacono(?:\s++łącznie)?|Paid)"
    r"|(?P<total>Do\s+zapłaty|Łącznie|Razem\s+do\s+zapłaty"
    r"|Suma\s+do\s+zapłaty|Total|Cena\s+z\s+dostawą|Wartość\s++zamówienia)"
    r"|(?P<sum>Razem|Suma|Kwota(?:\s++(?:zamówienia|transakcji))?))(?=$|[:\s])\s*+:?\s*+",
    re.IGNORECASE,
)
_CARD = re.compile(r"(?<!\w)(?:[•]{4}|[*]{4}|x{4}|(?<!\S)-)\s+[0-9]{4}$", re.IGNORECASE)
_CURRENCY = r"(?:zł|PLN|EUR|€|USD|\$|GBP|£)"
_MONEY_NUMBER = (
    r"(?:[1-9][0-9]{0,2}(?: ++[0-9]{3})++(?:[.,][0-9]{2})?"
    r"|[1-9][0-9]{0,2}(?:,[0-9]{3})++(?:\.[0-9]{2})?"
    r"|[1-9][0-9]{0,2}(?:\.[0-9]{3})++(?:,[0-9]{2})?"
    r"|[0-9]++(?:[.,][0-9]{2})?)"
)
_SIGN = r"(?:[-−]\s*+)?"
_AMOUNT_END = r"(?!\w|[.,][0-9])"
_AMOUNT = re.compile(
    rf"(?<![\w.,])(?P<sign>{_SIGN})(?:(?P<prefix>{_CURRENCY})\s*+(?P<inner_sign>{_SIGN})"
    rf"(?P<prefix_number>{_MONEY_NUMBER})"
    rf"|(?P<suffix_number>{_MONEY_NUMBER})\s*+(?P<suffix>{_CURRENCY})){_AMOUNT_END}",
    re.IGNORECASE,
)
_TRAILING_WORDS = re.compile(r"[.,;]?\s++[^\W\d_]")
_LABEL_DELIMITER = r"(?:\s*+:\s*+|\s++)"
_FX_NUMBER = r"[0-9]++(?:[.,][0-9]++)?"
_FX_NOTE = re.compile(
    rf"\s++using\s++{_FX_NUMBER}\s++{_CURRENCY}\s*+=\s*+{_FX_NUMBER}\s++{_CURRENCY}(?!\w)",
    re.IGNORECASE,
)
# Accounting labels delimit flattened cells. Only the payment totals are candidates.
_RECEIPT_HEADER = re.compile(r"Receipt\s++#\S++", re.IGNORECASE)
_SUMMARY_LABEL = re.compile(
    r"(?<!\S)(?:(?P<component>Subtotal|Total\s++excluding\s++tax|VAT|Tax|Discount)"
    r"|Total|Amount\s++paid|Charged)(?=$|[:\s])",
    re.IGNORECASE,
)
_DECIMAL = re.compile(r"[.,][0-9]{2}$")
_SEPARATORS = str.maketrans("", "", " .,")
_CURRENCIES = {"zł": "PLN", "€": "EUR", "$": "USD", "£": "GBP"}

_ORDER = re.compile(r"Opis\s+zamówienia\s+w\s+([^:]+):\s*(.*)", re.IGNORECASE)
_TITLE = re.compile(r"(?:Tytuł|Opis\s++płatności)\s*+:\s*+(.*)", re.IGNORECASE)
_DESCRIPTION_LABEL = re.compile(r"(?:Tytuł|Opis)\b", re.IGNORECASE)
_VENUE = re.compile(r"Twoje\s++zamówienie\s++z\s++lokalu\s++(.+)", re.IGNORECASE)
_VENUE_SUFFIX = re.compile(r"\s++zostanie\b", re.IGNORECASE)
_PAYEE_LABEL = re.compile(
    r"\b(?:Odbiorca(?:\s++wpłaty)?\s*+:\s*+|(?P<order>Opis\s++zamówienia\s++w\s++)"
    r"|(?P<host>dla\s*+-\s*+)|płatność\s++dla\s++(?!-)"
    r"|(?P<registered>Zarejestrowaliśmy\s++zlecenie\s++płatności\s++dla\s++(?!-))"
    r"|(?P<receipt>Receipt\s++from\s++)"
    r"|(?P<payment>Payment\s++to\s++))",
    re.IGNORECASE,
)
_PAYEE_HOST = re.compile(r"(?:https?://)?([a-z0-9.-]+)(?=$|[/:?#\s])", re.IGNORECASE)
_RECEIPT_SUFFIX = re.compile(r"\s++Receipt\s++#", re.IGNORECASE)
_SUBJECT_RECEIPT_ID = r"[0-9]++(?:-[0-9]++)*+"
_SUBJECT_PAYEES = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    rf"Your\s++receipt\s++from\s++(.+?)\s++#{_SUBJECT_RECEIPT_ID}",
    rf"Your\s++(.+?)\s++receipt\s++\[#{_SUBJECT_RECEIPT_ID}\]",
    rf"Twoje\s++pokwitowanie\s++od\s++(.+?)\s++nr\s++{_SUBJECT_RECEIPT_ID}",
))
_TRANSFERRED = re.compile(r"Przekazaliśmy\s++Twoje\s++pieniądze[.!]?", re.IGNORECASE)
_TRANSFER_PAYEE = re.compile(r"Do\s++(?!zapłaty\b)(.+)", re.IGNORECASE)
_GENERIC_DESCRIPTIONS = frozenset({"płatność", "payment", "zamówienie", "order", "receipt", "faktura"})
_LONG_DIGITS = re.compile(r"\d{11,}")
_PAYEE_DIGITS = re.compile(r"\d{7,}")
_REFERENCE = re.compile(
    r"\b(?:(?P<polish_prefix>nr\s++zamówienia)|zamówieni[ae](?:\s++(?P<polish_suffix>nr|numer)\b)?"
    r"|order\b(?:\s++(?P<english_suffix>no)\b\.?)?)"
    r"(?:(?P<separator>\s*+[:#]\s*+)|\s++)(?P<reference>[^\W_]{6,}+)(?![\w-])",
    re.IGNORECASE,
)
_ORDER_STATUS = frozenset({"received", "awaiting", "preparing", "shipped", "confirmation", "confirmed",
                           "potwierdzone", "przyjęte", "opłacone", "przygotowywane", "wysłane"})
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


def _parse_amount(value: str, *, labelled: bool = False, flattened_cell: bool = False) -> tuple[Decimal, str] | None:
    match = _AMOUNT.search(value) if labelled else _AMOUNT.match(value)
    if match is None:
        return None
    trailing = value[match.end():]
    if match["sign"] or match["inner_sign"] or _AMOUNT.search(trailing):
        raise _RejectedPayment
    if match.start() or (trailing and not flattened_cell and not _TRAILING_WORDS.match(trailing)):
        return None
    number = match["prefix_number"] or match["suffix_number"]
    currency = (match["prefix"] or match["suffix"]).casefold()
    digits = number.translate(_SEPARATORS)
    if _DECIMAL.search(number):
        digits = f"{digits[:-2]}.{digits[-2:]}"
    return Decimal(digits), _CURRENCIES.get(currency, currency.upper())


def _payment_total(lines: list[str], following: list[str], summary_rows: dict[int, int]) -> tuple[str, str] | None:
    totals: dict[str, set[tuple[Decimal, str]]] = {key: set() for key in ("charged", "paid", "total", "sum", "card")}
    summary_totals: dict[tuple[int, str], tuple[Decimal, str]] = {}
    for index, ((line, adjacent), next_value) in enumerate(zip(_line_pairs(lines), following)):
        label_line = _BULLET.sub("", line)
        if _FEE.match(label_line):
            continue
        row = summary_rows.get(index)
        if label := _TOTAL.match(label_line):
            level = label.lastgroup
            inline = label_line[label.end():]
            value = inline if row is not None else inline or next_value
            if level == "charged":
                value = _FX_NOTE.sub("", value).strip()
            if parsed := _parse_amount(value, labelled=bool(inline), flattened_cell=row is not None):
                if row is not None and summary_totals.setdefault((row, level), parsed) != parsed:
                    raise _RejectedPayment
                totals[level].add(parsed)
                continue
        if row is None and _CARD.search(line) and (parsed := _parse_amount(adjacent)):
            totals["card"].add(parsed)
    for values in totals.values():
        if values:
            if len(values) != 1:
                raise _RejectedPayment
            amount, currency = values.pop()
            if 0 < amount <= 1_000_000:
                return canonical_money(format(amount, "f")), currency
            raise _RejectedPayment
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
        if _DESCRIPTION_LABEL.match(line):
            continue
        if label := _DATE_LABEL.match(line):
            if parsed := _parse_date(line[label.end():] or next_value):
                dates.add(parsed)
        for label in _PAID_DATE.finditer(line):
            if parsed := _parse_date(line[label.end():]):
                dates.add(parsed)
    return tuple(sorted(dates))


def _contact_token(token: str) -> bool:
    folded = token.casefold()
    return "://" in folded or "mailto:" in folded or "www." in folded or "@" in token[1:-1]


def _clean_field(value: str) -> str:
    return " ".join(token for token in value.replace(_REDACTED, " ").split() if not _contact_token(token))


def _description(candidates: list[str]) -> str:
    fallback = "Płatność"
    for candidate in candidates:
        value = _clean_field(candidate)
        if value.strip(" .,:;!?").casefold() in _GENERIC_DESCRIPTIONS:
            continue
        digit_heavy = bool(_LONG_DIGITS.search(value)) or redact_digit_field(value) != value
        value = " ".join(_LONG_DIGITS.sub(" ", value).split())
        value = redact_pan(value) if value else ""
        value = value[:128].strip()
        if not value or value == _REDACTED:
            continue
        if not digit_heavy:
            return value
        if fallback == "Płatność":
            fallback = value
    return fallback


def _payee(value: str) -> str:
    value = _clean_field(value)
    if 2 <= len(value) <= 128 and not _PAYEE_DIGITS.search(value) and redact_pan(value) != _REDACTED:
        return value
    return ""


# Gateways spell one payee several ways ("Do Orange Polska S.A." beside
# "Odbiorca: Orange Polska SA"). Legal-form suffixes and punctuation do not
# make a second payee.
_LEGAL_FORM = re.compile(
    r"(?:\s+(?:s\.?\s*a\.?|sp\.?\s*z\s*o\.?\s*o\.?|sp\.?\s*k\.?|sp\.?\s*j\.?"
    r"|spółka\s+(?:akcyjna|komandytowa|jawna|z\s+ograniczoną\s+odpowiedzialnością)"
    r"|ltd\.?|limited|inc\.?|llc|gmbh|pbc))+$",
    re.IGNORECASE,
)
_PAYEE_PUNCT = re.compile(r"[\s.,\-]+")


def _payee_key(value: str) -> str:
    stem = _LEGAL_FORM.sub("", value.strip())
    return _PAYEE_PUNCT.sub(" ", stem or value).strip().casefold()


def _payee_candidates(line: str) -> list[str]:
    candidates = []
    labels = list(_PAYEE_LABEL.finditer(line))
    boundaries = [label.start() for label in labels[1:]] + [len(line)]
    for label, end in zip(labels, boundaries):
        value = line[label.end():end].rstrip(" ;|")
        kind = label.lastgroup
        if kind == "host":
            if (host := _PAYEE_HOST.match(value)) and (domain := canonical_domain(host[1])):
                candidates.append(domain)
        elif kind == "order":
            name, separator, _ = value.partition(":")
            if separator:
                candidates.append(name)
        elif kind == "payment":
            name, _, amount = value.rpartition(":")
            if _AMOUNT.fullmatch(amount.strip()):
                candidates.append(name)
        elif kind == "receipt":
            suffix = _RECEIPT_SUFFIX.search(value) or _AMOUNT.search(value)
            candidates.append(value[:suffix.start()] if suffix else value)
        elif kind == "registered":
            # The HTML worker can leave an opening parenthesis after removing a URL.
            candidates.append(_clean_field(value).rstrip(" .("))
        else:
            candidates.append(value)
    return candidates


def _receipt_fields(lines: list[str], subjects: tuple[str, ...], merchant: str, gateway: bool) -> tuple[str, str]:
    descriptions: list[str] = []
    titles: list[str] = []
    venues: list[str] = []
    candidates: list[str] = []
    payees: dict[str, str] = {}
    for line, next_line in zip(lines, _following_values(lines)):
        line = _BULLET.sub("", line)
        if order := _ORDER.fullmatch(line):
            descriptions.append(order[2])
        if title := _TITLE.fullmatch(line):
            titles.append(title[1])
            continue
        if venue := _VENUE.fullmatch(line):
            venues.append(_VENUE_SUFFIX.split(venue[1], maxsplit=1)[0])
        if not gateway:
            continue
        candidates.extend(_payee_candidates(line))
        if _TRANSFERRED.fullmatch(line) and (payee := _TRANSFER_PAYEE.fullmatch(next_line)):
            candidates.append(payee[1])
    if gateway:
        for subject in subjects:
            candidates.extend(match[1] for pattern in _SUBJECT_PAYEES if (match := pattern.fullmatch(subject)))
        for candidate in candidates:
            if clean := _payee(candidate):
                payees.setdefault(_payee_key(clean), clean)
    if len(payees) > 1:
        raise _RejectedPayment
    return next(iter(payees.values()), merchant), _description([*descriptions, *titles, *venues, *subjects])


def _order_reference(fields: list[str]) -> str | None:
    for field in fields:
        for match in _REFERENCE.finditer(_clean_field(field)):
            reference = match["reference"].casefold()
            # Bare order labels also introduce prose like "Order received".
            if reference not in _ORDER_STATUS or any(match.group(
                "polish_prefix", "polish_suffix", "english_suffix", "separator",
            )):
                return reference
    return None


def _flatten_summary(line: str) -> tuple[str, ...]:
    if header := _RECEIPT_HEADER.match(line):
        labels = list(_SUMMARY_LABEL.finditer(line, header.end()))
        if labels:
            ends = [label.start() for label in labels[1:]] + [len(line)]
            cells = (line[label.start():end].rstrip() for label, end in zip(labels, ends) if not label["component"])
            # Separate the receipt identifier from the totals before redacting.
            return (line[:labels[0].start()].rstrip(), *cells)
    return (line,)


def _subject_text(subject: str) -> str:
    visible = (" " if char.isspace() else char for char in subject
               if char.isspace() or unicodedata.category(char) not in ("Cc", "Cf"))
    bounded = "".join(islice(visible, MAX_LINE_CHARS))
    return " ".join(_normalize_receipt_text(bounded)[:256].split())


def _body_marker(line: str) -> bool:
    return (len(line) <= 120 and not _BODY_FIELD.match(_BULLET.sub("", line))
            and bool(_MARKER.search(line) or _STARTED_NOTICE.match(line)) and not _AMOUNT.search(line))


def inspect_payment(text: str, *, subjects: tuple[str, ...], merchant: str, gateway: bool) -> PaymentInspection:
    """Distinguish an absent total from a rejection in one rendered MIME body."""
    if not isinstance(text, str) or len(text) > MAX_TEXT_BYTES:
        return PaymentInspection()
    try:
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            return PaymentInspection()
        subjects = tuple(_subject_text(subject) for subject in subjects)
        normalized = (_INLINE_TAG.sub("", line).strip() for line in _normalized_invoice_lines(text))
        raw = ["" if _LEGAL.search(line) else line for line in normalized]
        # Rejection predicates do not extract fields. Check the original line
        # shape so identifiers cannot hide warnings and table splits do not
        # turn the priced part of an item into an independent warning.
        if any(_MARKER.search(subject) or _STARTED.search(subject) for subject in subjects) or any(
            _body_marker(line) for line in raw
        ):
            return PaymentInspection()
        expanded = []
        summary_rows = {}
        for line in raw:
            header, *cells = _flatten_summary(line)
            row = len(expanded)
            expanded.append(header)
            for cell in cells:
                summary_rows[len(expanded)] = row
                expanded.append(cell)
        joined = "\n".join(expanded)
        clean = redact_pan(joined).split("\n") if joined else expanded
        lines = [_SKIPPED_LINE if line == _SKIPPED_LINE else value for line, value in zip(expanded, clean)]
        following = _following_values(lines)
        if (total := _payment_total(lines, following, summary_rows)) is None:
            return PaymentInspection(missing_total=True)
        safe_subjects = tuple(redact_pan(subject) if subject else "" for subject in subjects)
        fields = _receipt_fields(lines, safe_subjects, merchant, gateway)
        reference = _order_reference([*safe_subjects, *lines])
        return PaymentInspection(ImportedPayment(*fields, *total, _body_dates(lines, following), reference))
    except (_RejectedPayment, ReceiptError, UnicodeEncodeError):
        return PaymentInspection()


def extract_payment(text: str, *, subject: str, merchant: str, gateway: bool) -> ImportedPayment | None:
    """Read only bounded, redacted fields from an authenticated payment email."""
    return inspect_payment(text, subjects=(subject,), merchant=merchant, gateway=gateway).payment
