"""Extract labelled payment totals and safe receipt fields from rendered emails."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from html import unescape
from itertools import islice
import re
from typing import Literal
import unicodedata

from .gmail_address import canonical_domain
from .gmail_invoice import _MONTHS, _SKIPPED_LINE, _line_pairs, _normalized_invoice_lines
from .gmail_trust import PaymentProvider
from .receipt_parser import (MAX_LINE_CHARS, MAX_TEXT_BYTES, ReceiptError, _normalize_receipt_text,
                             canonical_money, fold_text, redact_digit_field, redact_pan)


@dataclass(frozen=True)
class ImportedPayment:
    merchant: str
    description: str
    total_amount: str
    currency: str
    body_dates: tuple[date, ...]
    order_reference: str | None = None
    payee_from_config: bool = False
    fee_amount: str | None = None
    payee_host: str | None = None


@dataclass(frozen=True)
class PaymentInspection:
    payment: ImportedPayment | None = None
    missing_total: bool = False
    status: Literal["registered", "paid"] | None = None
    reject_reason: str | None = None
    gateway_ref: str | None = None
    gateway_ref_secondary: str | None = None
    card_last4: str | None = None
    blik_ref: str | None = None


class _RejectedPayment(ValueError):
    """An explicit rejection must not fall through to another MIME representation."""


_MARKER = re.compile(
    r"\b(?:dokończ\s+płatność|płatność\s+nie\s+została\s+(?:jeszcze\s+)?zakończona"
    r"|oczekuje\s+na\s+płatność|nie\s+opłacono"
    r"|payment\s+failed|unable\s+to\s+process|was\s+declined|nieudana\s+płatność"
    r"|płatność\s+odrzucona|nie\s+udało\s+się\s+(?:pobrać|obciążyć)"
    r"|czeka\s+na\s+odbiór"
    r"|gotowe\s+do\s+odbioru|potwierdzenie\s+odbioru|została\s+wysłana|wysłaliśmy"
    r"|shipped|out\s+for\s+delivery)\b",
    re.IGNORECASE,
)
# Only an affirmative merchant subject, optionally naming its shop or order number.
_ORDER_CANCELLATION_SUBJECT = re.compile(
    r"(?:potwierdzenie anulowania zamówienia)(?: (?:nr\.? |#)[0-9][0-9-]{0,31})?"
    r"(?: (?:w|we) (?:sklepie )?[^\W_][\w-]*(?:\.[\w-]+)*)?[.!]?", re.IGNORECASE,
)
_REFUND_SUBJECT = re.compile(r"\b(?:zwrot|refund|korekta|faktura\s++korygująca|credit\s++note)\b", re.IGNORECASE)
_REFUND_NOTICE = re.compile(
    r"(?:Zwrot\s++(?:środków|płatności|pieniędzy|za\s++zamówienie)|Twój\s++zwrot|Otrzymaliśmy\s++zwrot"
    r"|Refund\s++(?:issued|processed|of)|Your\s++refund|Faktura\s++korygująca|Korekta\s++faktury)\b",
    re.IGNORECASE,
)
_STARTED = re.compile(r"\brozpoczęci[ae]\s++transakcji\b", re.IGNORECASE)
_BODY_FIELD = re.compile(
    r"(?:Tytuł\s*+|(?:Opis|Odbiorca|Data|Nr|Numer|Identyfikator|Kwota|Termin|Adres)\b[^:]*+):"
    r"(?!//|(?<=[0-9]:)[0-9])\s*+\S",
    re.IGNORECASE,
)
_LEGAL = re.compile(r"\b(?:kapitał\w*|KRS|zakładowego)\b", re.IGNORECASE)
_URL_AUTHORITY_CHAR = r"[^\s/?#)\[\]]"
_HTTP_URL = re.compile(
    rf"(?ai:https?)://(?P<authority>{_URL_AUTHORITY_CHAR}*+\[[^\s)\[\]]++\]{_URL_AUTHORITY_CHAR}*+"
    rf"|{_URL_AUTHORITY_CHAR}++)[^\s)\]]*+",
)
_INLINE_TAG = re.compile(
    r"</?(?:p|b|strong|span|div|br|td|tr|th|table|tbody|font)(?: [^<>]*+)?/?>", re.IGNORECASE,
)
_FEE = re.compile(
    r"(?:Dodatkowe\s+opłaty|Opłata\s+za\s+(?:realizację\s+transakcji|usługę)"
    r"|Koszt\s++dostawy|Dostawa)(?=$|[:\s])",
    re.IGNORECASE,
)
_BULLET = re.compile(r"^[-*•]\s+")
_AMOUNT_DUE_LABEL = r"Kwota\s++do\s++zapłaty"
_ORDER_VALUE_LABEL = r"Wartość\s++zamówienia"
_TOTAL = re.compile(
    rf"(?:(?P<charged>Charged)|(?P<paid>Amount\s+paid|Kwota\s+zapłacona|Zapłacono(?:\s++łącznie)?|Paid)"
    rf"|(?P<total>{_AMOUNT_DUE_LABEL}|Do\s+zapłaty|Łączna\s+kwota(?:\s++(?:do\s+zapłaty|transakcji))?"
    rf"|Łącznie|Razem\s+do\s+zapłaty"
    rf"|Suma\s+do\s+zapłaty|Total|Cena\s+z\s+dostawą|{_ORDER_VALUE_LABEL})"
    r"|(?P<sum>Razem|Suma|Kwota(?:\s++(?:zamówienia|transakcji))?))(?=$|[:\s])\s*+:?\s*+",
    re.IGNORECASE,
)
# Charged labels (v1f): the amount actually debited, base + fee. A subset of the
# v1e "total" level, split out for gateway rows so the base stays visible.
_DUE = re.compile(
    rf"(?:{_AMOUNT_DUE_LABEL}|Do\s+zapłaty|Łączna\s+kwota(?:\s++(?:do\s+zapłaty|transakcji))?)(?=$|[:\s])",
    re.IGNORECASE,
)
_AMOUNT_DUE = re.compile(rf"{_AMOUNT_DUE_LABEL}(?=$|[:\s])", re.IGNORECASE)
_ORDER_VALUE = re.compile(rf"{_ORDER_VALUE_LABEL}(?=$|[:\s])", re.IGNORECASE)
_FEE_VALUE = re.compile(
    r"(?:Dodatkowe\s+opłaty(?:\s++obciążające\s++klienta)?|Opłata\s+za\s+realizację\s+transakcji)"
    r"(?=$|[:\s])\s*+:?\s*+",
    re.IGNORECASE,
)
_AUTOPAY_REGISTERED_SUBJECT = re.compile(r"Potwierdzenie\s++rozpoczęcia\s++transakcji\b", re.IGNORECASE)
_PAID_SUBJECTS = {
    "p24": re.compile(r"Potwierdzenie\b", re.IGNORECASE),
    "tpay": re.compile(r"Potwierdzenie\s++transakcji\b", re.IGNORECASE),
    "autopay": re.compile(r"(?:Przekazaliśmy\s++Twoją\s++płatność"
                          r"|Potwierdzenie\s++przekazania\s++pieniędzy)\b", re.IGNORECASE),
}
_REGISTERED_MARKERS = {
    "p24": re.compile(r"\b(?:Nowa\s++transakcja\s++płatnicza"
                      r"|zarejestrowaniu\s++nowej\s++transakcji)\b", re.IGNORECASE),
    "tpay": re.compile(r"\b(?:Nowa\s++transakcja\s++płatnicza"
                       r"|zarejestrowanie\s++nowej\s++transakcji)\b", re.IGNORECASE),
    "autopay": re.compile(r"\b(?:Zarejestrowaliśmy\s++Twoje\s++zlecenie"
                          rf"|{_AUTOPAY_REGISTERED_SUBJECT.pattern})\b", re.IGNORECASE),
    "payu": re.compile(r"\bzarejestrowaliśmy\s++zlecenie\s++płatności\b", re.IGNORECASE),
    "paynow": re.compile(r"\bzarejestrowaliśmy\s++zlecenie\s++płatności\b", re.IGNORECASE),
}
_STATUS_LABEL = r"(?:status(?:\s++(?:płatności|transakcji))?\s*+:\s*+)?"
_PAID_BODY_MARKERS = {
    "p24": re.compile(
        r"niniejsza\s++wiadomość\s++jest\s++informacją\s++o\s++potwierdzeniu\s++realizacji\s++płatności[.!]?",
        re.IGNORECASE,
    ),
    "autopay": re.compile(
        rf"{_STATUS_LABEL}(?:Płatność\s++zakończona\s++sukcesem|Przekazaliśmy\s++Twoje\s++pieniądze"
        r"|Twoja\s++wpłata\s++została\s++przekazana\s++odbiorcy)[.!]?",
        re.IGNORECASE,
    ),
    "paynow": re.compile(rf"{_STATUS_LABEL}(?:Transakcja\s++została\s++)?opłacona[.!]?", re.IGNORECASE),
}
_CARD_VERIFICATION = re.compile(r"Rejestracja\s++karty", re.IGNORECASE)
_CARD_VERIFICATION_CANCEL = re.compile(r"zostanie\s++anulowana", re.IGNORECASE)
_PAYNOW_ID = r"[A-Z0-9]{4}(?:-[A-Z0-9]{3}){3}"
_PAYNOW_FAILED = re.compile(
    rf"{_STATUS_LABEL}(?:(?:Twoja\s++)?(?:transakcja|płatność)(?:\s++{_PAYNOW_ID})?"
    r"(?:\s++została)?\s++)?(?:straciła\s++ważność|odrzucona|anulowana)[.!]?",
    re.IGNORECASE,
)
_GATEWAY_REFS = {
    "p24": re.compile(r"(?<![\w-])(P24(?:-[A-Z0-9]{3}){3})(?![\w-])", re.IGNORECASE),
    "tpay": re.compile(r"(?<![\w-])(TR-[A-Z0-9]+-[A-Z0-9]+)(?![\w-])", re.IGNORECASE),
    "autopay": re.compile(
        r"^(?:[-*•]\s+)?Identyfikator[ \t]+transakcji(?:[ \t]+Autopay)?[ \t]*"
        r"(?::[ \t]*|:?[ \t]*\n+(?=[A-Z-]*+[0-9][A-Z0-9-]*+[ \t]*+$))"
        r"([A-Z0-9]+(?:-[A-Z0-9]+)*)(?![\w-])",
        re.IGNORECASE | re.MULTILINE,
    ),
    "paynow": re.compile(rf"(?<![\w-])({_PAYNOW_ID})(?![\w-])", re.IGNORECASE),
}
_REF_AUTOPAY_PAYMENT_NO = re.compile(
    r"^(?:[-*•]\s+)?Numer[ \t]+płatności[ \t]*"
    r"(?::[ \t]*|:?[ \t]*\n+(?=[A-Z._/-]*+[0-9][A-Z0-9._/-]*+[ \t]*+$))"
    r"([A-Z0-9]++(?:[-_/.][A-Z0-9]++)*+)(?![\w-])",
    re.IGNORECASE | re.MULTILINE,
)
_REF_PAYU_SUBJECT = re.compile(r"\(([0-9]+)\)")
_REF_PAYU_LINK = re.compile(
    r"https?://[^\s?#]+\?(?:[^\s#]*?&(?:amp;)?)?transId=([A-Z0-9]+)(?=$|[&#)\]\s])",
    re.IGNORECASE,
)
_BLIK_REF = re.compile(
    r"(?:\b(?:nr|numer)\s+transakcji(?:\s+blik)?|\bidentyfikator\s+transakcji\s+blik)"
    r"\s*(?::\s*)?(?:\*{1,2}\s*|\(\s*)?(?<![\w./-])([0-9]{6,20})(?![\w./-])",
    re.IGNORECASE,
)
_TPAY_BLIK_SUFFIX = re.compile(r"(?<![\w./-])TR-[A-Z0-9]+-[A-Z0-9]+/([0-9]{7,20})(?![\w./-])", re.IGNORECASE)
_GATEWAY_ALIASES = {
    "p24": "p24",
    "przelewy24": "p24",
    "przelewy 24": "p24",
    "tpay": "tpay",
    "tpay.com": "tpay",
    "autopay": "autopay",
    "blue media": "autopay",
    "bluemedia": "autopay",
    "payu": "payu",
    "payu.com": "payu",
    "paynow": "paynow",
    "paynow.pl": "paynow",
    "blik": "blik",
    "stripe": "stripe",
}
_BANK_NIP_SPAN = re.compile(r"\bNIP\b[:\s-]*[0-9-]{8,16}", re.IGNORECASE)
_BANK_DATE_TOKEN = re.compile(r"^(?:\d{2}[-_./]\d{2}[-_./]\d{4}|\d{4}[-_./]\d{2}[-_./]\d{2})$")
_BANK_SWIFT_TAG = re.compile(
    r"/(?:OPT|TI|BI|TXT|TITL|BENM|ADR|OB|ORD|REF|NRB|KNT|DKR|ZLC|NIP)/", re.IGNORECASE,
)
_BANK_OPT_PATH = re.compile(rf"/OPT/(\S+?)(?={_BANK_SWIFT_TAG.pattern}|\s|$)", re.IGNORECASE)
_BANK_STRUCTURED_REFS = {
    "p24": re.compile(r"(?<![\w.-])(P24(?:-[A-Z0-9]{3}){3})(?![\w./-])", re.IGNORECASE),
    "tpay": re.compile(
        r"(?<![\w.-])(TR-[A-Z0-9]+-[A-Z0-9]+)(?:(?![\w./-])|(?=/[0-9]{6,20}(?![\w./-])))",
        re.IGNORECASE,
    ),
    "paynow": re.compile(rf"(?<![\w.-])({_PAYNOW_ID})(?![\w./-])", re.IGNORECASE),
}
_BANK_PAYU_NUMERIC = re.compile(r"(?<![\w./-])([0-9]{5,20})(?![\w./-])")
_BANK_PAYU_ALNUM = re.compile(
    r"(?<![\w./-])(?=[A-Z0-9]*[0-9])(?=[A-Z0-9]*[A-Z])([A-Z0-9]{6,24})(?![\w./-])",
    re.IGNORECASE,
)
_OPT_PAYU_NUMERIC = re.compile(r"^[0-9]{5,20}$")
_OPT_PAYU_ALNUM = re.compile(r"^(?=[A-Z0-9]*[0-9])(?=[A-Z0-9]*[A-Z])[A-Z0-9]{6,24}$")
_BANK_AUTOPAY_TOKEN = re.compile(
    r"(?<![\w./-])(?=[A-Z0-9_-]*[0-9])([A-Z0-9]{4,}(?:[-_][A-Z0-9]+)*)(?![\w./-])",
    re.IGNORECASE,
)
_BANK_AUTOPAY_SLASH = re.compile(r"(?<![\w./-])([A-Z0-9]+/[A-Z0-9]{2,}(?:/[A-Z0-9]+)*)(?![\w./-])", re.IGNORECASE)
_OPT_AUTOPAY_VALUE = re.compile(r"^[A-Z0-9]+(?:[-_/][A-Z0-9]+)*$")
_BANK_GATEWAY_LABELS = re.compile(
    r"\b(?:"
    r"(?P<p24>przelewy\s*24|p24(?!-[A-Z0-9])|paypro)"
    r"|(?P<tpay>tpay(?:\.com)?|krajowy\s+integrator(?:\s+p[łl]atno[śs]ci)?|transferuj(?:\.pl)?)"
    r"|(?P<autopay>autopay|blue\s*media)"
    r"|(?P<payu>payu(?:\.com)?)"
    r"|(?P<paynow>paynow(?:\.pl)?|melements)"
    r"|(?P<blik>blik)"
    r"|(?P<other>stripe|paypal|adyen|klarna|dotpay|imoje|cashbill|hotpay|polcard|eservice)"
    r")\b",
    re.IGNORECASE,
)
_GENERIC_ANCHORED_GATEWAYS = frozenset({"payu", "autopay"})
# Payee hosts: an "Adres www:" label, a parenthesized URL after the payee, or
# "dla https://host/" in the subject.
_ADDRESS_WWW = re.compile(r"Adres\s+www\s*:\s*(\S+)", re.IGNORECASE)
_PAYEE_URL = re.compile(r"\(\s*(https?://[^\s)]+)", re.IGNORECASE)
_SUBJECT_PAYEE = re.compile(r"\bdla\s++(?:-\s++)?(.+)", re.IGNORECASE)
_TRAILING_ID = re.compile(r"\s+\d{6,}$")
_CARD = re.compile(r"(?<!\w)(?:[•]{4}|[*]{4}|x{4}|(?<!\S)-)\s+[0-9]{4}$", re.IGNORECASE)
_CARD_LAST4_PATTERNS = (
    re.compile(r"(?<![./-])(?:[•·]{2,}|[*]{4,}|[xX]{4,})\s*([0-9]{4})(?![0-9-])"),
    re.compile(
        r"\b(?:visa|mastercard|maestro|amex|american\s+express|debit|credit|"
        r"karta(?:\s+p[łl]atnicza)?|card|payment\s+method|metoda\s+p[łl]atno[śs]ci)\s*"
        r"(?:[-–]|ending\s+in|ko[nń]cz[aą]c[aey]\s+si[eę]\s+na|:?\s*(?:nr\s+)?[•·*x]{2,}|:)\s*"
        r"([0-9]{4})(?![0-9-])",
        re.IGNORECASE,
    ),
)
_BLIK_PAYMENT = re.compile(
    r"^(?:(?:(?:metoda|forma|sposob) platnosci|payment method|platnosc|transakcja|payment|"
    r"paid (?:with|via|by|using)|zaplacono(?: przez| za pomoca)?|(?:nr|numer|identyfikator) transakcji)"
    r"(?:\s*[:=-]\s*|\s+))?blik\b"
)
_CURRENCY_CODE = r"(?:PLN|EUR|USD|GBP)"
_CURRENCY = rf"(?:{_CURRENCY_CODE}|zł|€|\$|£)"
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
_TRAILING_CURRENCY = re.compile(rf"[.,;]?\s++({_CURRENCY_CODE})\b", re.IGNORECASE)
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
    r"\b(?:(?P<recipient>Odbiorca(?:\s++wpłaty)?\s*+:\s*+)"
    r"|(?P<host>dla\s*+-\s*+)|płatność\s++dla\s++(?!-)"
    r"|(?P<registered>Zarejestrowaliśmy\s++zlecenie\s++płatności\s++dla\s++(?!-))"
    r"|(?P<receipt>Receipt\s++from\s++)"
    r"|(?P<payment>Payment\s++to\s++))",
    re.IGNORECASE,
)
_PAYEE_HOST = re.compile(r"(?P<scheme>https?://)?(?P<domain>[a-z0-9.-]+)(?:[/:?#]\S*)?", re.IGNORECASE)
_ON_BEHALF_OF = re.compile(r"^.{1,80}?\s+w\s+imieniu\s+(.+)$", re.IGNORECASE)
_RECEIPT_SUFFIX = re.compile(r"\s++(?:Receipt\s++#|\(\s*+invoice\s++illustration\s++\[)", re.IGNORECASE)
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
    r"|order\b(?:\s++(?P<english_suffix>no|number)\b\.?)?)"
    r"(?:(?P<separator>\s*+[:#]\s*+)|\s++)(?P<reference>[^\W_]{6,}+)(?![\w-])",
    re.IGNORECASE,
)
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


def _strip_emphasis(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in ("*", "_"):
        return stripped[1:-1]
    return value


def _parse_amount(value: str, *, labelled: bool = False, flattened_cell: bool = False) -> tuple[Decimal, str] | None:
    value = _strip_emphasis(value)
    match = _AMOUNT.search(value) if labelled else _AMOUNT.match(value)
    if match is None:
        return None
    trailing = value[match.end():]
    if match["sign"] or match["inner_sign"] or _AMOUNT.search(trailing):
        raise _RejectedPayment
    if match.start() or (trailing and not flattened_cell and not _TRAILING_WORDS.match(trailing)):
        return None
    number = match["prefix_number"] or match["suffix_number"]
    token = (match["prefix"] or match["suffix"]).casefold()
    currency = _CURRENCIES.get(token, token.upper())
    if (code := _TRAILING_CURRENCY.match(trailing)) and code[1].upper() != currency:
        raise _RejectedPayment
    digits = number.translate(_SEPARATORS)
    if _DECIMAL.search(number):
        digits = f"{digits[:-2]}.{digits[-2:]}"
    return Decimal(digits), currency


def _payment_total(lines: list[str], following: list[str], summary_rows: dict[int, int],
                   *, gateway: bool = False):
    totals: dict[str, set[tuple[Decimal, str]]] = {key: set() for key in ("charged", "paid", "total", "sum", "card")}
    due: set[tuple[Decimal, str]] = set()
    order_values: set[tuple[Decimal, str]] = set()
    has_amount_due = False
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
                if level == "total":
                    if gateway:
                        if _DUE.match(label_line):
                            due.add(parsed)
                            continue
                    elif _ORDER_VALUE.match(label_line):
                        order_values.add(parsed)
                        continue
                    elif _AMOUNT_DUE.match(label_line):
                        has_amount_due = True
                totals[level].add(parsed)
                continue
        if row is None and _CARD.search(line) and (parsed := _parse_amount(adjacent)):
            totals["card"].add(parsed)
    if gateway:
        return totals, due
    if order_values:
        if has_amount_due:
            # Polish order confirmations treat Wartość zamówienia as a merchandise subtotal
            # when an explicit Kwota do zapłaty is present in the same currency.
            order_value = _single_amount(order_values)
            if {currency for _amount, currency in totals["total"]} != {order_value[1]}:
                raise _RejectedPayment
        else:
            totals["total"].update(order_values)
    for values in totals.values():
        if values:
            if len(values) != 1:
                raise _RejectedPayment
            amount, currency = values.pop()
            if 0 < amount <= 1_000_000:
                return canonical_money(format(amount, "f")), currency
            raise _RejectedPayment
    return None


def _single_amount(values: set[tuple[Decimal, str]]) -> tuple[Decimal, str] | None:
    if len(values) > 1:
        raise _RejectedPayment
    return next(iter(values), None)


def _gateway_fee(lines: list[str], following: list[str]) -> tuple[Decimal, str] | None:
    fees = set()
    for line, next_value in zip(lines, following):
        label_line = _BULLET.sub("", line)
        if label := _FEE_VALUE.match(label_line):
            inline = label_line[label.end():]
            if parsed := _parse_amount(inline or next_value, labelled=bool(inline)):
                fees.add(parsed)
    return _single_amount(fees)


def _gateway_payment_total(lines: list[str], following: list[str], summary_rows: dict[int, int]):
    """The charged amount (base + fee) and the fee for a gateway email."""
    totals, due = _payment_total(lines, following, summary_rows, gateway=True)
    fee = _gateway_fee(lines, following)
    base = None
    for values in totals.values():
        if values:
            base = _single_amount(values)
            break
    charged = _single_amount(due)
    if base is None and charged is None:
        return None
    if len({value[1] for value in (base, charged, fee) if value is not None}) > 1:
        raise _RejectedPayment
    if charged is not None:
        total = charged
        if fee is not None and (fee[0] > charged[0] or (base is not None and charged[0] != base[0] + fee[0])):
            raise _RejectedPayment
    else:
        total = (base[0] + fee[0], base[1]) if fee is not None else base
    amount, currency = total
    if not 0 < amount <= 1_000_000:
        raise _RejectedPayment
    stored_fee = None if fee is None else canonical_money(format(fee[0], "f"))
    return canonical_money(format(amount, "f")), currency, stored_fee


def _gateway_status(provider: PaymentProvider | None, subjects: tuple[str, ...], lines: list[str]):
    if provider == "stripe":
        return "paid"
    paid_subject = _PAID_SUBJECTS.get(provider)
    if paid_subject and any(paid_subject.match(subject) for subject in subjects):
        return "paid"
    registered = _REGISTERED_MARKERS.get(provider)
    if registered and any(registered.search(line) for line in (*subjects, *lines)):
        return "registered"
    paid_body = _PAID_BODY_MARKERS.get(provider)
    if paid_body and any(paid_body.fullmatch(_BULLET.sub("", line)) for line in lines):
        return "paid"
    return "registered"


def canonical_gateway(gateway: str | None) -> str | None:
    """Normalize a receipt gateway label or provider key to a canonical gateway key."""
    if not isinstance(gateway, str):
        return None
    key = " ".join(unicodedata.normalize("NFKC", gateway).strip().lower().split())
    return _GATEWAY_ALIASES.get(key)


def _blik_ref(subjects: tuple[str, ...], lines: list[str]) -> str | None:
    joined = "\n".join((*subjects, *lines))
    match = _BLIK_REF.search(joined) or _TPAY_BLIK_SUFFIX.search(joined)
    return match[1] if match else None


def _card_suffixes(lines) -> set[str]:
    return {
        match[1]
        for line in lines
        for pattern in _CARD_LAST4_PATTERNS
        for match in pattern.finditer(line)
    }


def _card_last4(lines: list[str]) -> str | None:
    found = _card_suffixes(lines)
    return found.pop() if len(found) == 1 else None


def purchase_payment_scope(lines: list[str]) -> tuple[str | None, str | None]:
    """Use explicit payment labels and mark conflicting evidence as mixed."""
    suffixes = _card_suffixes(lines)
    blik = any(_BLIK_PAYMENT.match(fold_text(_BULLET.sub("", line.strip()))) for line in lines)
    if len(suffixes) > 1 or (suffixes and blik):
        return None, "mixed"
    return next(iter(suffixes), None), "blik" if blik else None


def _is_other_gateway_token(token: str) -> bool:
    return bool(token.startswith(("P24-", "TR-")) or _GATEWAY_REFS["paynow"].fullmatch(token))


def _gateway_labels(text: str) -> list[tuple[int, int, str]]:
    labels: list[tuple[int, int, str]] = []
    for match in _BANK_GATEWAY_LABELS.finditer(text):
        group = match.lastgroup
        labels.append((match.start(), match.end(), "_other" if group == "other" else group))
    return labels


def _token_gateway_allowed(pos: int, provider: str, labels: list[tuple[int, int, str]]) -> bool:
    if provider in _GENERIC_ANCHORED_GATEWAYS and not any(gw == provider for _, _, gw in labels):
        return False
    preceding = None
    for _, end, gw in labels:
        if end <= pos:
            preceding = gw
        else:
            break
    if preceding is not None:
        return preceding == provider
    return not (provider in _GENERIC_ANCHORED_GATEWAYS and labels and labels[0][2] != provider)


def _opt_value(raw: str) -> str:
    value = raw.rstrip("/").upper()
    if value.startswith("IU/") and len(value) > 3:
        return value[3:]
    return value


def _strip_bare_bank_refs(text: str, matched_refs: set[str], *, count: int = 0) -> str:
    for ref in sorted(matched_refs, key=len, reverse=True):
        pattern = rf"(?<![\w./-]){re.escape(ref)}(?![\w./-])"
        text = re.sub(pattern, " ", text, count=count, flags=re.IGNORECASE)
    return text


def bank_payee_texts(text: str, matched_refs: set[str]) -> tuple[str, str]:
    """Return bank text for finding and for ruling out the receipt's payee."""
    normalized = _BANK_NIP_SPAN.sub(" ", unicodedata.normalize("NFKC", text))
    if _BANK_SWIFT_TAG.search(normalized) is None:
        plain = _strip_bare_bank_refs(normalized, matched_refs)
        return plain, plain

    opt_refs = set()

    def strip_matched_opt(match: re.Match[str]) -> str:
        value = _opt_value(match[1])
        if value in matched_refs:
            opt_refs.add(value)
            return " "
        return match[0]

    # An ID cannot establish the merchant. For the conflict check, retain repeated
    # occurrences outside /OPT/ and honor the original bare-token boundaries.
    evidence = _BANK_SWIFT_TAG.sub(" ", _BANK_OPT_PATH.sub(" ", normalized))
    remaining = _BANK_OPT_PATH.sub(strip_matched_opt, normalized)
    remaining = _strip_bare_bank_refs(remaining, matched_refs - opt_refs, count=1)
    return _strip_bare_bank_refs(evidence, matched_refs), _BANK_SWIFT_TAG.sub(" ", remaining)


def _autopay_bank_tokens(text: str) -> list[tuple[str, int]]:
    candidates: list[tuple[str, int]] = [
        *((match[1].upper(), match.start()) for match in _BANK_AUTOPAY_TOKEN.finditer(text)),
        *((slash[1].upper(), slash.start()) for slash in _BANK_AUTOPAY_SLASH.finditer(text)),
    ]
    for opt in _BANK_OPT_PATH.finditer(text):
        value = _opt_value(opt[1])
        if _OPT_AUTOPAY_VALUE.fullmatch(value):
            candidates.append((value, opt.start()))
    return [
        (token, pos)
        for token, pos in candidates
        if len(token) >= 5 and any(char.isdigit() for char in token)
        and not _BANK_DATE_TOKEN.fullmatch(token) and not _is_other_gateway_token(token)
    ]


def _payu_bank_tokens(text: str) -> list[tuple[str, int]]:
    candidates: list[tuple[str, int]] = [
        *(
            (match[1].upper(), match.start())
            for match in _REF_PAYU_SUBJECT.finditer(text)
            if len(match[1]) >= 4
        ),
        *((match[1].upper(), match.start()) for match in _REF_PAYU_LINK.finditer(text)),
        *((match[1].upper(), match.start()) for match in _BANK_PAYU_NUMERIC.finditer(text)),
        *((match[1].upper(), match.start()) for match in _BANK_PAYU_ALNUM.finditer(text)),
    ]
    for opt in _BANK_OPT_PATH.finditer(text):
        value = _opt_value(opt[1])
        if _OPT_PAYU_NUMERIC.fullmatch(value) or _OPT_PAYU_ALNUM.fullmatch(value):
            candidates.append((value, opt.start()))
    return [(token, pos) for token, pos in candidates if not _is_other_gateway_token(token)]


def bank_ref_tokens(text: str | None, gateway: str | None) -> set[str]:
    """Extract normalized per-gateway reference tokens from bank remittance or party text."""
    provider = canonical_gateway(gateway)
    if provider is None or not isinstance(text, str) or not text.strip():
        return set()
    normalized = unicodedata.normalize("NFKC", text)
    labels = _gateway_labels(normalized)
    if provider in _BANK_STRUCTURED_REFS:
        candidates = [(match[1].upper(), match.start()) for match in _BANK_STRUCTURED_REFS[provider].finditer(normalized)]
    elif provider == "blik":
        candidates = [(match[1], match.start()) for match in _BLIK_REF.finditer(normalized)]
    else:
        stripped = _BANK_NIP_SPAN.sub(lambda match: " " * len(match[0]), normalized)
        if provider == "payu":
            candidates = _payu_bank_tokens(stripped)
        elif provider == "autopay":
            candidates = _autopay_bank_tokens(stripped)
        else:
            return set()
    return {token for token, pos in candidates if _token_gateway_allowed(pos, provider, labels)}


def _gateway_ref(provider: PaymentProvider | None, subjects: tuple[str, ...], lines: list[str],
                 raw_text: str) -> str | None:
    if provider == "payu":
        match = _REF_PAYU_SUBJECT.search("\n".join(subjects)) or _REF_PAYU_LINK.search(raw_text)
    elif provider == "autopay":
        joined = "\n".join((*subjects, *lines))
        match = _REF_AUTOPAY_PAYMENT_NO.search(joined) or _GATEWAY_REFS["autopay"].search(joined)
    elif provider == "blik":
        return _blik_ref(subjects, lines)
    else:
        pattern = _GATEWAY_REFS.get(provider)
        match = pattern.search("\n".join((*subjects, *lines))) if pattern else None
    return match[1].upper() if match else None


def _gateway_ref_secondary(provider: PaymentProvider | None, subjects: tuple[str, ...], lines: list[str]):
    if provider != "autopay":
        return None
    joined = "\n".join((*subjects, *lines))
    payment_number = _REF_AUTOPAY_PAYMENT_NO.search(joined)
    transaction = _GATEWAY_REFS["autopay"].search(joined)
    if not payment_number or not transaction:
        return None
    primary, secondary = payment_number[1].upper(), transaction[1].upper()
    return secondary if secondary != primary else None


def _candidate_host(value: str) -> tuple[bool, str | None]:
    match = _PAYEE_HOST.fullmatch(value.strip(" ()[]").removesuffix("."))
    if match and (match["scheme"] or "." in match["domain"]):
        domain = match["domain"].lower().removeprefix("www.")
        return True, canonical_domain(domain)
    return False, None


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


_POLISH_LETTER_ENTITY = re.compile(r"&(?:amp;)?([A-Za-z][A-Za-z0-9]{1,30}|#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6});")
_POLISH_DIACRITICS = frozenset("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ")


def _polish_letter(match: re.Match) -> str:
    # Some mail subjects and plain-text titles use HTML-encoded Polish letters.
    # Leave markup, punctuation and arbitrary character references as literal text.
    decoded = unescape(f"&{match[1]};")
    return decoded if decoded in _POLISH_DIACRITICS else match[0]


def _decode_polish_letters(value: str) -> str:
    return _POLISH_LETTER_ENTITY.sub(_polish_letter, value)


def _description(candidates: list[str]) -> str:
    fallback = "Płatność"
    for candidate in candidates:
        value = _clean_field(_decode_polish_letters(candidate))
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
    # One trailing whitespace-separated all-digit token of 6+ digits is a gateway
    # internal id, not part of the payee name ("SHOP SP. Z O.O. 4500000000").
    # The stem must keep a letter, so an all-digit identifier is never a payee.
    stripped = _TRAILING_ID.sub("", value)
    if stripped != value and re.search(r"[^\W\d_]", stripped):
        value = stripped
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


def _payee_candidates(line: str, next_line: str):
    labels = list(_PAYEE_LABEL.finditer(line))
    boundaries = [label.start() for label in labels[1:]] + [len(line)]
    for label, end in zip(labels, boundaries):
        value = line[label.end():end].rstrip(" ;|")
        kind = label.lastgroup
        if kind == "recipient" and not value:
            if end == len(line) and not (
                _BULLET.match(next_line) or _BODY_FIELD.match(next_line)
                or _TOTAL.match(next_line) or _FEE.match(next_line)
            ):
                yield kind, next_line
        elif kind == "payment":
            name, _, amount = value.rpartition(":")
            if _AMOUNT.fullmatch(amount.strip()):
                yield kind, name
        elif kind == "receipt":
            suffixes = (_RECEIPT_SUFFIX.search(value), _AMOUNT.search(value))
            end = min((suffix.start() for suffix in suffixes if suffix), default=len(value))
            yield kind, value[:end].rstrip()
            if link := _PAYEE_URL.search(value[end:]):
                yield "host", link[1]
        elif kind == "registered":
            # The HTML worker can leave an opening parenthesis after removing a URL.
            yield kind, value.rstrip(" .(")
        else:
            yield kind, value


def _resolve_payee(candidates, address_host):
    labelled = {}
    fallback = host = None
    for kind, value in candidates:
        if kind == "recipient" and (behalf := _ON_BEHALF_OF.match(value.strip())):
            value = behalf[1]
        host_like, candidate_host = _candidate_host(value)
        if host_like or kind == "host":
            host = host or candidate_host
            continue
        if link := _PAYEE_URL.search(value):
            host = host or _candidate_host(link[1])[1]
            value = value[:link.start()].rstrip()
        if payee := _payee(value):
            if kind == "recipient":
                labelled.setdefault(_payee_key(payee), payee)
            else:
                fallback = fallback or payee
    if len(labelled) > 1:
        raise _RejectedPayment
    return next(iter(labelled.values()), fallback), address_host or host


def _receipt_fields(lines: list[str], subjects: tuple[str, ...], gateway: bool):
    descriptions: list[str] = []
    titles: list[str] = []
    venues: list[str] = []
    candidates: list[tuple[str | None, str]] = []
    address_host = None
    for line, next_line in zip(lines, _following_values(lines)):
        line = _BULLET.sub("", line)
        if order := _ORDER.fullmatch(line):
            descriptions.append(order[2])
            if gateway:
                candidates.append((None, order[1]))
            continue
        if title := _TITLE.fullmatch(line):
            titles.append(title[1])
            continue
        if venue := _VENUE.fullmatch(line):
            venues.append(_VENUE_SUFFIX.split(venue[1], maxsplit=1)[0])
        if not gateway:
            continue
        if label := _ADDRESS_WWW.search(line):
            address_host = address_host or _candidate_host(label[1])[1]
        candidates.extend(_payee_candidates(line, next_line))
        if _TRANSFERRED.fullmatch(line) and (payee := _TRANSFER_PAYEE.fullmatch(next_line)):
            candidates.append((None, payee[1]))
    if gateway:
        for subject in subjects:
            candidates.extend((None, match[1]) for pattern in _SUBJECT_PAYEES if (match := pattern.fullmatch(subject)))
            if match := _SUBJECT_PAYEE.search(subject):
                candidates.append((None, match[1]))
    payee, host = _resolve_payee(candidates, address_host)
    return payee, _description([*descriptions, *titles, *venues, *subjects]), host


def _order_reference(fields: list[str]) -> str | None:
    for field in fields:
        for match in _REFERENCE.finditer(_clean_field(field)):
            reference = match["reference"].casefold()
            # Bare order labels also introduce prose like "Order received".
            if any(char.isdigit() for char in reference) or any(match.group(
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


def confirms_order_cancellation(subject: str) -> bool:
    # Do not treat a truncated question, qualification or negation as a definitive notice.
    if len(subject) > 256:
        return False
    normalized = _subject_text(_decode_polish_letters(subject))
    return _ORDER_CANCELLATION_SUBJECT.fullmatch(normalized) is not None


def _shorten_url(match: re.Match[str]) -> str:
    authority = match["authority"].rpartition("@")[2]
    if authority.startswith("["):
        host, close, _ = authority.partition("]")
        host += close
    else:
        host = authority.partition(":")[0]
    return f"https://{host}/" if host else match[0]


def _body_marker(line: str, provider: PaymentProvider | None) -> bool:
    if len(line) > 120:
        return False
    line = _BULLET.sub("", line)
    started = provider != "autopay" and _AUTOPAY_REGISTERED_SUBJECT.match(line)
    return (not _BODY_FIELD.match(line)
            and bool(_MARKER.search(line) or started or _REFUND_NOTICE.match(line))
            and not _AMOUNT.search(line))


def _subject_rejected(subject: str, provider: PaymentProvider | None) -> bool:
    if _MARKER.search(subject) or _REFUND_SUBJECT.search(subject):
        return True
    return bool(_STARTED.search(subject)) and not (provider == "autopay" and _AUTOPAY_REGISTERED_SUBJECT.match(subject))


def inspect_payment(text: str, *, subjects: tuple[str, ...], merchant: str, gateway: bool,
                    provider: PaymentProvider | None = None) -> PaymentInspection:
    """Distinguish an absent total from a rejection in one rendered MIME body."""
    if not isinstance(text, str) or len(text) > MAX_TEXT_BYTES:
        return PaymentInspection()
    try:
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            return PaymentInspection()
        provider = provider if gateway else None
        cancellation = any(confirms_order_cancellation(subject) for subject in subjects)
        subjects = tuple(_subject_text(subject) for subject in subjects)
        # Shorten link tokens before invoice normalization applies its per-line cap.
        shortened = _HTTP_URL.sub(_shorten_url, text)
        normalized = (_INLINE_TAG.sub("", line).strip() for line in _normalized_invoice_lines(shortened))
        raw = ["" if _LEGAL.search(line) else line for line in normalized]
        # Rejection predicates do not extract fields. Check the original line
        # shape so identifiers cannot hide warnings and table splits do not
        # turn the priced part of an item into an independent warning.
        if provider == "paynow" and any(_PAYNOW_FAILED.fullmatch(_BULLET.sub("", line)) for line in raw):
            return PaymentInspection(
                reject_reason="failed", gateway_ref=_gateway_ref(provider, subjects, raw, text),
                gateway_ref_secondary=_gateway_ref_secondary(provider, subjects, raw),
            )
        if provider == "p24":
            body = "\n".join(raw)
            if _CARD_VERIFICATION.search(body) and _CARD_VERIFICATION_CANCEL.search(body):
                return PaymentInspection(reject_reason="card_verification")
        if cancellation or any(_subject_rejected(subject, provider) for subject in subjects):
            return PaymentInspection()
        if any(_body_marker(line, provider) for line in raw):
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
        fee = status = gateway_ref = gateway_ref_secondary = None
        if gateway:
            if (result := _gateway_payment_total(lines, following, summary_rows)) is None:
                return PaymentInspection(missing_total=True)
            total, fee = result[:2], result[2]
            status = _gateway_status(provider, subjects, lines)
            gateway_ref = _gateway_ref(provider, subjects, raw, text)
            gateway_ref_secondary = _gateway_ref_secondary(provider, subjects, raw)
        elif (total := _payment_total(lines, following, summary_rows)) is None:
            return PaymentInspection(missing_total=True)
        safe_subjects = tuple(redact_pan(subject) if subject else "" for subject in subjects)
        payee, description, host = _receipt_fields(lines, safe_subjects, gateway)
        reference = _order_reference([*safe_subjects, *lines])
        blik_ref = _blik_ref(safe_subjects, [*raw, *lines])
        card_last4 = _card_last4(lines)
        payment = ImportedPayment(payee or merchant, description, *total, _body_dates(lines, following), reference,
                                  payee_from_config=gateway and payee is None,
                                  fee_amount=fee, payee_host=host)
        return PaymentInspection(payment, status=status, gateway_ref=gateway_ref,
                                 gateway_ref_secondary=gateway_ref_secondary,
                                 card_last4=card_last4, blik_ref=blik_ref)
    except (_RejectedPayment, ReceiptError, UnicodeEncodeError):
        return PaymentInspection()


def extract_payment(text: str, *, subject: str, merchant: str, gateway: bool,
                    provider: PaymentProvider | None = None) -> ImportedPayment | None:
    """Read only bounded, redacted fields from an authenticated payment email."""
    return inspect_payment(text, subjects=(subject,), merchant=merchant, gateway=gateway, provider=provider).payment
