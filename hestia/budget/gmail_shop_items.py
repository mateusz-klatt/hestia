"""Read the repeated, priced product blocks in a Polish shop order confirmation."""
from decimal import Decimal
import re

from .analytics_core import AnalyticsError, canonical_money, compare_money
from .gmail_invoice import _normalized_invoice_lines
from .receipt_parser import MAX_TEXT_BYTES, ReceiptError, redact_digit_field, redact_pan, validate_confirm_lines


_PRIVATE_WORDS = (
    r"adres|ulica|ul|aleja|osiedle|telefon|tel|klient\w*|konto|identyfikator|imię|nazwisko|email|e-mail|"
    r"PESEL|NIP|IBAN|kod|numer|zamówieni\w*|płatnoś\w*|odbiorc\w*|nabywc\w*|rabat\w*|zniżk\w*|dostaw\w*|wysyłk\w*"
)
_HAS_CODE = re.compile(r"\bKod\s+produktu\s*:", re.IGNORECASE)
_SECTION = re.compile(r"(?:[^\W\d_]+ )?produkty", re.IGNORECASE)
_PRODUCT = re.compile(r"([^\W\d_][\w .,'’+&()–-]{0,85}) \([1-9][0-9]{0,2} szt\.\)")
_PRIVATE = re.compile(rf"\w\.\w|\d{{4}}|_|(?:\b(?:{_PRIVATE_WORDS})\b)", re.IGNORECASE)
_CODE = re.compile(r"Kod produktu\s*:\s*[0-9]{1,20}", re.IGNORECASE)
_QUANTITY = re.compile(r"Ilość\s*:\s*([1-9][0-9]{0,3})\s*x", re.IGNORECASE)
_REPEAT = re.compile(r"([1-9][0-9]{0,3})\s*x", re.IGNORECASE)
_MONEY = r"((?:0|[1-9][0-9]{0,5})[,.][0-9]{2})\s*(?:zł|PLN)"
_PRICE = re.compile(r"Cena za sztukę\s*:\s*" + _MONEY, re.IGNORECASE)
_LINE_TOTAL = re.compile(_MONEY, re.IGNORECASE)
_EXTRA_DETAIL = re.compile(
    r"^(?:rabat\w*|zniżk\w*|dostaw\w*|wysyłk\w*|opłat\w*|zwrot\w*|korekt\w*)\b|(?:Kod produktu|Ilość|Cena za sztukę)\s*:|"
    r"\d[,.]\d{2}\s*(?:zł|PLN|EUR|USD|€|\$)", re.IGNORECASE,
)

_TABLE_HEADERS = ("produkt", "ilość", "cena", "razem")
_SUMMARY_HEADING = re.compile(r"rozliczenie zamówienia:?", re.IGNORECASE)
_SUMMARY_INLINE = re.compile(
    r"rozliczenie zamówienia\s*:\s*(?=(?:wartość zamówienia|koszt spakowania|kaucja|kwota do zapłaty)\b)", re.IGNORECASE,
)
_SUMMARY_ROW = re.compile(
    r"(?P<label>wartość zamówienia|koszt spakowania|kaucja|kwota do zapłaty)"
    r"(?:\s*:\s*(?P<colon_val>.*)|\s+(?P<space_val>.+))?\Z", re.IGNORECASE,
)
_SUMMARY_KEYS = {"wartość zamówienia": "subtotal", "koszt spakowania": "packing",
                 "kaucja": "deposit", "kwota do zapłaty": "grand_total"}
_TAIL_HEADING = re.compile(r"oszczędności w tym zamówieniu(?:\s*:?\s*(.*))?", re.IGNORECASE)
_SAVINGS_ROW = re.compile(
    r"(?P<label>kupony rabatowe|naliczone promocje|dostawa|suma oszczędności)"
    r"(?:\s*:\s*(?P<colon_val>.*)|\s+(?P<space_val>.+))?\Z", re.IGNORECASE,
)
_TABLE_MONEY = re.compile(
    r"(?:(?P<sign>[-−])\s*)?((?:0|[1-9][0-9]{0,5}|[1-9][0-9]{0,2}(?: [0-9]{3}){1,2})(?:[,.][0-9]{2})?)\s*(?:zł|PLN)",
    re.IGNORECASE,
)
_TABLE_QTY = re.compile(r"((?:0|[1-9][0-9]{0,3})(?:[,.][0-9]{1,4})?)\s*(?:(x)|(kg|g|dag|l|ml|szt\.?|op\.?))?", re.IGNORECASE)
_TABLE_NAME = re.compile(r"[^\W_][\w .,'’\"„”+&()%:/–-]{0,127}")
_TABLE_NAME_BAD = re.compile(
    rf"[^\W\d_]\.[^\W\d_]|www\.|://|@|_|\d{{5}}|\b(?:zł|PLN|EUR|USD|GBP|CHF)\b|[€$£]|"
    rf"\b(?:{_PRIVATE_WORDS}|al|os|kupon\w*|bon(?:y|ów|em|ie|u)?|bonus\w*|opłat\w*|zwrot\w*|korekt\w*|"
    rf"kaucj\w*|spakowani\w*|oszczędnoś\w*|saldo|produkt|ilość|cena|razem)\b", re.IGNORECASE,
)
_OUTSIDE_BAD = re.compile(
    r"^(?:rabat\w*|zniżk\w*|kupon\w*|bon(?:y|ów|em|ie|u)?|bonus\w*|wysyłk\w*|opłat\w*|prowizj\w*|korekt\w*|"
    r"saldo|nadpłat\w*|niedopłat\w*|kaucj\w*|oszczędnoś\w*|koszt\s+(?:dostaw\w*|wysyłk\w*|spakowani\w*|pakowani\w*)|"
    r"wartość\s+zamówienia|kwota\s+do\s+zapłaty|do\s+zapłaty|łącznie|razem)\b|"
    r"^(?:zwrot|refund)\s*[:.]?$|^(?:zwrot (?:za|środków|płatności|pieniędzy)|refund (?:for|of))\b|"
    r"\b(?:rabat\w*|zniżk\w*|kupon\w*|korekt\w*|faktura korygująca|zwrócono|zwróciliśmy|dokonano zwrotu|przyznano zwrot)\b|"
    r"\d\s*(?:zł|PLN|EUR|USD|GBP|CHF|€|\$|£)|\b(?:EUR|USD|GBP|CHF)\b|[€$£]", re.IGNORECASE,
)
_LEGAL = re.compile(r"\b(?:kapitał\w*|KRS|zakładowego)\b", re.IGNORECASE)
_SENTINEL = object()


def _amount(match):
    return Decimal(match[1].replace(",", "."))


def _table_money(value: str, *, signed: bool = False) -> Decimal | None:
    match = _TABLE_MONEY.fullmatch(value)
    if match is None or (not signed and match["sign"]):
        return None
    amount = Decimal(match[2].replace(" ", "").replace(",", "."))
    return None if amount > Decimal("1000000") else (-amount if match["sign"] else amount)


def _consume_labelled_money(lines: list[str], index: int, pattern: re.Pattern[str], *, signed: bool = False):
    match = pattern.fullmatch(lines[index])
    if match is None:
        return None
    raw = match["colon_val"] if match["colon_val"] is not None else match["space_val"]
    if raw:
        next_idx = index + 1
    elif index + 1 < len(lines):
        raw, next_idx = lines[index + 1], index + 2
    else:
        return _SENTINEL
    amount = _table_money(raw.strip(), signed=signed)
    return _SENTINEL if amount is None else (match["label"].casefold(), amount, next_idx)


def _finalize_shop_items(items: list[dict], grand_total: Decimal, total: str) -> tuple[dict, ...]:
    try:
        receipt_total = canonical_money(total)
        if compare_money(receipt_total, "0") <= 0 or canonical_money(str(grand_total)) != receipt_total:
            return ()
        return tuple(validate_confirm_lines(items, receipt_total))
    except (AnalyticsError, ReceiptError):
        return ()


def _product(lines):
    name, code, count_line, unit_line, repeated_count, total_line = lines
    product_name = _PRODUCT.fullmatch(name)
    count, repeat = _QUANTITY.fullmatch(count_line), _REPEAT.fullmatch(repeated_count)
    unit, amount = _PRICE.fullmatch(unit_line), _LINE_TOTAL.fullmatch(total_line)
    if (not product_name or _PRIVATE.search(name) or sum(c.isdigit() for c in product_name[1]) > 3
            or not _CODE.fullmatch(code) or not count or not repeat or count[1] != repeat[1] or not unit or not amount):
        return None
    unit_price, total = _amount(unit), _amount(amount)
    if unit_price * int(count[1]) != total:
        return None
    return dict(line_kind="item", raw_name=name, quantity=count[1], raw_unit="szt.", ptu_code=None,
                unit_price_raw=str(unit_price), gross_line_total=str(total), discount_amount="0", net_line_total=str(total))


def _inspect_coded_shop_items(lines: list[str], total: str) -> tuple[dict, ...]:
    labels = [line.casefold() for line in lines]
    sections = [i for i, line in enumerate(lines) if _SECTION.fullmatch(line)]
    if len(sections) != 1 or labels.count("wartość zamówienia") != 1 or labels.count("łącznie") != 1:
        return ()
    section, end = sections[0], labels.index("wartość zamówienia")
    if labels.index("łącznie") != end + 2 or end + 3 >= len(lines):
        return ()
    subtotal, grand = _LINE_TOTAL.fullmatch(lines[end + 1]), _LINE_TOTAL.fullmatch(lines[end + 3])
    if not subtotal or not grand or _amount(subtotal) != _amount(grand):
        return ()
    # A shop heading or pickup location can precede the first product. Never copy it.
    codes = [i for i, line in enumerate(lines) if _CODE.fullmatch(line)]
    if not codes or codes[0] < section + 2:
        return ()
    start = codes[0] - 1
    if codes != list(range(start + 1, end, 6)) or (end - start) % 6 or any(
        _EXTRA_DETAIL.search(line) for line in lines[:start] + lines[end + 4:]
    ):
        return ()
    items = [_product(lines[i:i + 6]) for i in range(start, end, 6)]
    return () if any(item is None for item in items) else _finalize_shop_items(items, _amount(grand), total)


def _has_table_order(lines: list[str]) -> bool:
    folded = [line.casefold() for line in lines]
    return (any(_SUMMARY_HEADING.fullmatch(line) or _SUMMARY_INLINE.match(line) or _TAIL_HEADING.fullmatch(line)
                for line in lines)
            or any(tuple(folded[i:i + 4]) == _TABLE_HEADERS for i in range(len(folded) - 3)))


def _parse_table_summary(summary_lines: list[str]) -> tuple[Decimal, Decimal, Decimal, Decimal] | None:
    inline = _SUMMARY_INLINE.match(summary_lines[0])
    expanded = [summary_lines[0][inline.end():], *summary_lines[1:]] if inline else summary_lines[1:]
    seen: dict[str, Decimal] = {}
    index = 0
    while index < len(expanded):
        parsed = _consume_labelled_money(expanded, index, _SUMMARY_ROW)
        if parsed is _SENTINEL:
            return None
        if parsed is not None:
            label, amount, index = parsed
            key = _SUMMARY_KEYS[label]
            if key in seen:
                return None
            seen[key] = amount
        elif _OUTSIDE_BAD.search(expanded[index]):
            return None
        else:
            index += 1
    if "subtotal" not in seen or "grand_total" not in seen:
        return None
    subtotal, grand_total = seen["subtotal"], seen["grand_total"]
    packing, deposit = seen.get("packing", Decimal(0)), seen.get("deposit", Decimal(0))
    return (subtotal, packing, deposit, grand_total) if grand_total > 0 and subtotal + packing + deposit == grand_total else None


def _parse_table_row(row: list[str]) -> tuple[dict, Decimal] | None:
    name = " ".join(row[0].split())
    if (not _TABLE_NAME.fullmatch(name) or not re.search(r"[^\W\d_]", name)
            or name[0] in "-+*/=.,:;" or name[-1] in "-+*/=:," or _TABLE_NAME_BAD.search(name)
            or sum(c.isdigit() for c in name) > 8 or redact_digit_field(name) != name or redact_pan(name) != name):
        return None
    qty_match, unit_price, line_total = _TABLE_QTY.fullmatch(row[1]), _table_money(row[2]), _table_money(row[3])
    if qty_match is None or unit_price is None or line_total is None:
        return None
    qty_norm = qty_match[1].replace(",", ".")
    if Decimal(qty_norm) <= 0 or unit_price * Decimal(qty_norm) != line_total:
        return None
    item = dict(line_kind="item", raw_name=name, quantity=canonical_money(qty_norm),
                raw_unit=qty_match[3].lower() if qty_match[3] else None, ptu_code=None,
                unit_price_raw=canonical_money(str(unit_price)), gross_line_total=canonical_money(str(line_total)),
                discount_amount="0", net_line_total=canonical_money(str(line_total)))
    return item, line_total


def _valid_table_tail(tail_lines: list[str]) -> bool:
    inline = (_TAIL_HEADING.fullmatch(tail_lines[0])[1] or "").strip()
    rest, index = tail_lines[1:], 0
    if inline:
        if _table_money(inline) is None:
            return False
    elif rest and _table_money(rest[0]) is not None:
        index = 1
    else:
        seen: dict[str, Decimal] = {}
        while index < len(rest):
            parsed = _consume_labelled_money(rest, index, _SAVINGS_ROW, signed=True)
            if parsed is _SENTINEL:
                return False
            if parsed is None:
                break
            label, amount, index = parsed
            if label in seen or (label != "suma oszczędności" and amount > 0):
                return False
            seen[label] = amount
        if seen:
            components = [val for key, val in seen.items() if key != "suma oszczędności"]
            total_saved = seen.get("suma oszczędności")
            if (total_saved is not None and (total_saved < 0 or (components and total_saved != -sum(components)))) or (
                total_saved is None and any(val != 0 for val in components)
            ):
                return False
    return not any(_OUTSIDE_BAD.search(line) and not _LEGAL.search(line) for line in rest[index:])


def _inspect_table_shop_items(lines: list[str], total: str) -> tuple[dict, ...]:
    folded = [line.casefold() for line in lines]
    summary_starts = [i for i, line in enumerate(lines)
                      if _SUMMARY_HEADING.fullmatch(line) or _SUMMARY_INLINE.match(line)]
    headers = [i for i in range(len(folded) - 3) if tuple(folded[i:i + 4]) == _TABLE_HEADERS]
    tails = [i for i, line in enumerate(lines) if _TAIL_HEADING.fullmatch(line)]
    if (len(summary_starts) != 1 or len(headers) != 1 or len(tails) != 1
            or sum(1 for line in folded if line in _TABLE_HEADERS) != 4):
        return ()
    summary_idx, header_idx, tail_idx = summary_starts[0], headers[0], tails[0]
    table_start = header_idx + 4
    if (not (summary_idx < header_idx and table_start < tail_idx) or (tail_idx - table_start) % 4
            or any(_OUTSIDE_BAD.search(line) for line in lines[:summary_idx])):
        return ()
    summary = _parse_table_summary(lines[summary_idx:header_idx])
    if summary is None or not _valid_table_tail(lines[tail_idx:]):
        return ()
    subtotal, packing, deposit, grand_total = summary
    rows = [_parse_table_row(lines[i:i + 4]) for i in range(table_start, tail_idx, 4)]
    if any(row is None for row in rows) or sum((row[1] for row in rows), Decimal(0)) != subtotal:
        return ()
    items = [row[0] for row in rows]
    for amount, name, kind in ((packing, "Koszt spakowania", "other"), (deposit, "Kaucja", "deposit")):
        if amount > 0:
            canonical = canonical_money(str(amount))
            items.append(dict(line_kind=kind, raw_name=name, quantity="1", raw_unit=None, ptu_code=None,
                              unit_price_raw=None, gross_line_total=canonical, discount_amount="0", net_line_total=canonical))
    return _finalize_shop_items(items, grand_total, total)


def inspect_shop_items(text: str, total: str, currency: str) -> tuple[dict, ...] | None:
    """None indicates no recognized block. An empty tuple indicates a block that failed validation."""
    if not isinstance(text, str) or len(text) > MAX_TEXT_BYTES or len(text.encode("utf-8", "replace")) > MAX_TEXT_BYTES:
        return None
    lines = [line for line in _normalized_invoice_lines(text) if line]
    has_code, has_table = bool(_HAS_CODE.search(text)), _has_table_order(lines)
    if not has_code and not has_table:
        return None
    if (has_code and has_table) or currency != "PLN":
        return ()
    return _inspect_coded_shop_items(lines, total) if has_code else _inspect_table_shop_items(lines, total)


def extract_shop_items(text: str, total: str, currency: str) -> tuple[dict, ...]:
    return inspect_shop_items(text, total, currency) or ()
