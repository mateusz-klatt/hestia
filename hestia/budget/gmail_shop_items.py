"""Read the repeated, priced product blocks in a Polish shop order confirmation."""
from decimal import Decimal
import re

from .analytics_core import AnalyticsError, canonical_money
from .gmail_invoice import _normalized_invoice_lines
from .receipt_parser import MAX_TEXT_BYTES, ReceiptError, validate_confirm_lines


_HAS_CODE = re.compile(r"\bKod\s+produktu\s*:", re.IGNORECASE)
_SECTION = re.compile(r"(?:[^\W\d_]+ )?produkty", re.IGNORECASE)
_PRODUCT = re.compile(r"([^\W\d_][\w .,'’+&()–-]{0,85}) \([1-9][0-9]{0,2} szt\.\)")
_PRIVATE = re.compile(
    r"\w\.\w|\d{4}|_|\b(?:adres|ulica|ul|aleja|osiedle|telefon|tel|klient\w*|konto|identyfikator|"
    r"imię|nazwisko|email|e-mail|PESEL|NIP|IBAN|kod|numer|zamówieni\w*|płatnoś\w*|"
    r"odbiorc\w*|nabywc\w*|rabat\w*|zniżk\w*|dostaw\w*|wysyłk\w*)\b", re.IGNORECASE,
)
_CODE = re.compile(r"Kod produktu\s*:\s*[0-9]{1,20}", re.IGNORECASE)
_QUANTITY = re.compile(r"Ilość\s*:\s*([1-9][0-9]{0,3})\s*x", re.IGNORECASE)
_REPEAT = re.compile(r"([1-9][0-9]{0,3})\s*x", re.IGNORECASE)
_MONEY = r"((?:0|[1-9][0-9]{0,5})[,.][0-9]{2})\s*(?:zł|PLN)"
_PRICE = re.compile(r"Cena za sztukę\s*:\s*" + _MONEY, re.IGNORECASE)
_LINE_TOTAL = re.compile(_MONEY, re.IGNORECASE)
_EXTRA_DETAIL = re.compile(
    r"^(?:rabat\w*|zniżk\w*|dostaw\w*|wysyłk\w*|opłat\w*|zwrot\w*|korekt\w*)\b|(?:Kod produktu|Ilość|Cena za sztukę)\s*:|"
    r"\d[,.]\d{2}\s*(?:zł|PLN|EUR|USD|€|\$)",
    re.IGNORECASE,
)


def _amount(match):
    return Decimal(match[1].replace(",", "."))


def _product(lines):
    name, code, count_line, unit_line, repeated_count, total_line = lines
    product_name = _PRODUCT.fullmatch(name)
    count = _QUANTITY.fullmatch(count_line)
    repeat = _REPEAT.fullmatch(repeated_count)
    unit = _PRICE.fullmatch(unit_line)
    amount = _LINE_TOTAL.fullmatch(total_line)
    if (not product_name or _PRIVATE.search(name) or sum(c.isdigit() for c in product_name[1]) > 3
            or not _CODE.fullmatch(code)
            or not count or not repeat or count[1] != repeat[1] or not unit or not amount):
        return None
    unit_price, total = _amount(unit), _amount(amount)
    if unit_price * int(count[1]) != total:
        return None
    return dict(line_kind="item", raw_name=name, quantity=count[1], raw_unit="szt.", ptu_code=None,
                unit_price_raw=str(unit_price), gross_line_total=str(total),
                discount_amount="0", net_line_total=str(total))


def inspect_shop_items(text: str, total: str, currency: str) -> tuple[dict, ...] | None:
    """None indicates no recognized block. An empty tuple indicates a block that failed validation."""
    if not isinstance(text, str) or len(text) > MAX_TEXT_BYTES or not _HAS_CODE.search(text):
        return None
    if currency != "PLN" or len(text.encode("utf-8", "replace")) > MAX_TEXT_BYTES:
        return ()
    lines = [line for line in _normalized_invoice_lines(text) if line]
    labels = [line.casefold() for line in lines]
    sections = [i for i, line in enumerate(lines) if _SECTION.fullmatch(line)]
    if len(sections) != 1 or labels.count("wartość zamówienia") != 1 or labels.count("łącznie") != 1:
        return ()
    section = sections[0]
    end = labels.index("wartość zamówienia")
    if labels.index("łącznie") != end + 2 or end + 3 >= len(lines):
        return ()
    subtotal = _LINE_TOTAL.fullmatch(lines[end + 1])
    grand = _LINE_TOTAL.fullmatch(lines[end + 3])
    if not subtotal or not grand or _amount(subtotal) != _amount(grand):
        return ()

    # A shop heading or pickup location can precede the first product. Never copy it.
    codes = [i for i, line in enumerate(lines) if _CODE.fullmatch(line)]
    if not codes or codes[0] < section + 2:
        return ()
    start = codes[0] - 1
    if codes != list(range(start + 1, end, 6)) or (end - start) % 6:
        return ()
    if any(_EXTRA_DETAIL.search(line) for line in lines[:start] + lines[end + 4:]):
        return ()
    items = []
    for i in range(start, end, 6):
        item = _product(lines[i:i + 6])
        if item is None:
            return ()
        items.append(item)
    try:
        receipt_total = canonical_money(total)
        if canonical_money(str(_amount(grand))) != receipt_total:
            return ()
        return tuple(validate_confirm_lines(items, receipt_total))
    except (AnalyticsError, ReceiptError):
        return ()


def extract_shop_items(text: str, total: str, currency: str) -> tuple[dict, ...]:
    return inspect_shop_items(text, total, currency) or ()
