"""Deterministic Polish receipt parsing, redaction and exact confirmation checks."""
from __future__ import annotations

from datetime import date
import re
import unicodedata

from .analytics_core import AnalyticsError, canonical_money, compare_money, negate_money, sum_money

MAX_RECEIPT_LINES = 300
MAX_LINE_NAME_LEN = 128
MAX_SHOP_NAME_LEN = 128
MAX_TEXT_BYTES = 32 * 1024
MAX_TEXT_LINES = 500
MAX_LINE_CHARS = 512
_MAX_COMBINING = 8
MAX_MONEY_INTEGER_DIGITS = 9
MAX_MONEY_FRACTION_DIGITS = 4
# Joined digits of this length are a secret, with or without a Luhn check.
_SECRET_DIGITS = 12
_REMOVED = "[usunięto]"
# Spaces, dots, commas, hyphens, slashes and newlines join digits. Unicode dashes
# survive NFKC, so they join too. Any other character ends the run.
_JOIN_SEPARATORS = frozenset(" .,/-\n\t") | frozenset("‐‑‒–—―")
_PESEL_WEIGHTS = (1, 3, 7, 9, 1, 3, 7, 9, 1, 3)
_IBAN_MIN, _IBAN_MAX = 15, 34
DRAFT_TTL_SECONDS = 48 * 60 * 60
CONFIRMED_TEXT_TTL_SECONDS = 30 * 24 * 60 * 60

RECEIPT_KINDS = frozenset({"purchase", "refund"})
PAYMENT_METHODS = frozenset({"card", "blik", "cash", "mixed", "unknown"})
LINE_KINDS = frozenset({"item", "deposit", "basket_discount", "other"})
PTU_CODES = frozenset("ABCDEFG")
CONFIRM_FIELDS = frozenset({
    "receipt_id", "expected_version", "raw_shop_name", "raw_nip", "receipt_kind", "receipt_date",
    "receipt_time", "total_amount", "currency", "payment_method", "lines", "link",
})
_CONFIRM_REQUIRED = CONFIRM_FIELDS - frozenset({"lines", "raw_shop_name", "raw_nip"})
LINE_FIELDS = frozenset({
    "line_kind", "raw_name", "ptu_code", "quantity", "raw_unit", "unit_price_raw",
    "gross_line_total", "discount_amount", "net_line_total",
})

_NUMBER = r"[+-]?[0-9]+(?:[.,][0-9]+)?"
_NUMBER_RE = re.compile(_NUMBER, re.ASCII)
_MONEY = re.compile(r"-?[0-9]+(?:\.[0-9]+)?\Z", re.ASCII)
_DATE_ISO = re.compile(r"(?<![0-9])([0-9]{4})-([0-9]{2})-([0-9]{2})(?![0-9])", re.ASCII)
_DATE_PL = re.compile(r"(?<![0-9])([0-9]{2})[-.]([0-9]{2})[-.]([0-9]{4})(?![0-9])", re.ASCII)
_UNITS = r"kg|g|dag|l|ml|szt|op"
# The price tail is anchored at the end of the line. The name is the text before it,
# so neither expression uses a leading `.+?` that overlaps the amount.
_ITEM_QTY_TAIL = re.compile(
    rf"(?P<quantity>{_NUMBER})(?:\s*(?P<unit>{_UNITS}))?\s*[x×*]\s*"
    rf"(?P<unit_price>{_NUMBER})\s*(?:=\s*)?(?P<gross>{_NUMBER})\s*(?:PLN\s*)?(?P<ptu>[A-G])?\Z",
    re.IGNORECASE | re.ASCII,
)
_ITEM_GROSS_TAIL = re.compile(
    rf"(?P<gross>{_NUMBER})\s*(?:PLN\s*)?(?P<ptu>[A-G])?\Z",
    re.IGNORECASE | re.ASCII,
)
_NAMED_UNIT_TAIL = re.compile(
    rf"(?P<quantity>{_NUMBER})\s*(?P<unit>{_UNITS})"
    rf"(?:\s*[x×*]\s*(?P<unit_price>{_NUMBER})\s*(?:=\s*)?)?"
    rf"\s+(?P<gross>{_NUMBER})\s*(?:PLN\s*)?(?P<ptu>[A-G])?\Z",
    re.IGNORECASE | re.ASCII,
)
_BARE_UNIT = re.compile(
    rf"^(?P<name>)(?P<quantity>{_NUMBER})\s*(?P<unit>kg|g|dag|l|ml|szt|op)\s+(?P<unit_price>)"
    rf"(?P<gross>{_NUMBER})\s*(?:PLN\s*)?(?P<ptu>[A-G])?$",
    re.IGNORECASE | re.ASCII,
)
_CALCULATION = re.compile(
    rf"^(?P<quantity>{_NUMBER})\s*(?P<unit>kg|g|dag|l|ml|szt|op)?\s*[x×*]\s*"
    rf"(?P<unit_price>{_NUMBER})\s*(?:=\s*)?(?P<gross>{_NUMBER})\s*(?:PLN\s*)?(?P<ptu>[A-G])?$",
    re.IGNORECASE | re.ASCII,
)
_DISCOUNT = re.compile(rf"^\s*(?:RABAT|OPUST)\b[^0-9+-]*(?P<amount>{_NUMBER})", re.IGNORECASE | re.ASCII)
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z", re.ASCII)
_CURRENCY = re.compile(r"[A-Z]{3}\Z", re.ASCII)
_TIME = re.compile(r"(?<![0-9])((?:[01][0-9]|2[0-3]):[0-5][0-9])(?![0-9])", re.ASCII)
_SHORT_AMOUNT = r"[+-]?[0-9]{1,9}(?:[.,][0-9]{1,4})?"
_CURRENCY_AMOUNT = re.compile(
    rf"(?<![A-Z0-9])(?:PLN|EUR|USD|GBP|CHF)\s*{_SHORT_AMOUNT}(?![0-9])"
    rf"|(?<![0-9]){_SHORT_AMOUNT}\s*(?:PLN|EUR|USD|GBP|CHF)(?![A-Z0-9])",
    re.IGNORECASE | re.ASCII,
)
_PTU_WORD = re.compile(r"(?<![A-Z0-9])PTU(?![A-Z0-9])", re.IGNORECASE | re.ASCII)
_PTU_AMOUNT = re.compile(rf"(?<![0-9]){_SHORT_AMOUNT}%?(?![0-9])", re.ASCII)
_NIP_LABEL = re.compile(r"\bNIP", re.IGNORECASE | re.ASCII)
_NIP_SPACE = " \t\r\f\v"
_NIP_BODY = set("0123456789 -")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ASCII_DROPPED = re.compile(r"[\x00-\x08\t\x0e-\x1f\x7f]")
_LINE_BREAK = str.maketrans({
    "\r": "\n",
    "\v": "\n",
    "\f": "\n",
    "\x1c": "\n",
    "\x1d": "\n",
    "\x1e": "\n",
    "\x85": "\n",
    "\u2028": "\n",
    "\u2029": "\n",
})
_CHAIN_NIPS = {
    "7791011327": "biedronka",
    "9370008168": "carrefour",
    "7811897358": "lidl",
}
_CHAIN_TOKENS = {
    "biedronka": ("biedronka", "jeronimo", "martins"),
    "carrefour": ("carrefour",),
    "lidl": ("lidl",),
}
_STOP_TOKENS = frozenset({"polska", "spolka", "zoo", "fiskalny", "paragon"})


class ReceiptError(ValueError):
    CODES = frozenset({
        "budget_receipt_invalid_request", "budget_receipt_not_found", "budget_receipt_conflict",
        "budget_receipt_stale_source", "budget_receipt_tx_already_linked", "budget_receipt_total_mismatch",
        "budget_receipt_quota_exceeded", "budget_receipt_unavailable",
    })

    def __init__(self, code="budget_receipt_invalid_request", status=400):
        self.code = code if code in self.CODES else "budget_receipt_invalid_request"
        self.status = status if type(status) is int and status in (400, 404, 409, 413, 503) else 400
        super().__init__(self.code)


def fold_text(value):
    normalized = unicodedata.normalize("NFC", value).translate(str.maketrans({"ł": "l", "Ł": "L"}))
    return "".join(char for char in unicodedata.normalize("NFD", normalized)
                   if unicodedata.category(char) != "Mn").casefold()


def _pesel(digits):
    """True when ``digits`` is 11 characters and the PESEL checksum matches."""
    if len(digits) != 11:
        return False
    total = sum(int(digit) * weight for digit, weight in zip(digits, _PESEL_WEIGHTS))
    return (10 - (total % 10)) % 10 == int(digits[10])


def _iban_checksum(token):
    """ISO 13616 mod-97. The check digits make the rearranged integer congruent to 1."""
    moved = token[4:] + token[:4]
    total = 0
    for char in moved:
        if char.isdigit():
            total = (total * 10 + int(char)) % 97
        else:
            total = (total * 100 + (ord(char) - 55)) % 97
    return total == 1


def _iban_shape(token):
    """Polish PL plus 26 digits, or a generic IBAN with a valid mod-97 checksum."""
    length = len(token)
    if length < _IBAN_MIN or length > _IBAN_MAX:
        return False
    if not (token[0].isalpha() and token[1].isalpha() and token[2].isdigit() and token[3].isdigit()):
        return False
    if not all(char.isalnum() for char in token):
        return False
    if token[:2] == "PL" and length == 28 and token[2:].isdigit():
        return True
    return _iban_checksum(token)


def _mark_digit_runs(text, bad):
    """Mark every line that adds a digit to a 12+ run or to a checksum-valid PESEL.

    Separators do not end the run, including a newline, so a PAN wrapped over
    several lines is one run. A letter, a colon or any other non-separator ends it.
    A price or a date stays only when its own digits are not in that run.
    """
    digits = []
    lines = set()
    line = 0

    def flush():
        if len(digits) >= _SECRET_DIGITS or _pesel("".join(digits)):
            bad.update(lines)
        digits.clear()
        lines.clear()

    for char in text:
        if char == "\n":
            line += 1
            continue
        if "0" <= char <= "9":
            digits.append(char)
            lines.add(line)
            continue
        if char in _JOIN_SEPARATORS:
            continue
        flush()
    flush()


def _mark_ibans(text, bad):
    """Mark lines that contribute a character to a bounded IBAN.

    Separators may sit between the IBAN characters. The match must end on a
    boundary, so a longer word is not a prefix hit. Each start reads at most
    ``_IBAN_MAX`` alphanumeric characters.
    """
    line_of = []
    line = 0
    for char in text:
        line_of.append(line)
        if char == "\n":
            line += 1
    length = len(text)
    index = 0
    while index < length:
        char = text[index]
        if not char.isalpha() or (index and text[index - 1].isalnum()):
            index += 1
            continue
        compact = []
        positions = []
        cursor = index
        matched_end = None
        matched_len = 0
        while cursor < length and len(compact) < _IBAN_MAX:
            current = text[cursor]
            if current.isalnum():
                compact.append(current.upper())
                positions.append(cursor)
                cursor += 1
                if _iban_shape("".join(compact)):
                    look = cursor
                    while look < length and text[look] in _JOIN_SEPARATORS:
                        look += 1
                    if look >= length or not text[look].isalnum():
                        matched_end = look
                        matched_len = len(compact)
                continue
            if current in _JOIN_SEPARATORS:
                cursor += 1
                continue
            break
        if matched_end is None:
            index += 1
            continue
        for pos in positions[:matched_len]:
            bad.add(line_of[pos])
        index = matched_end


def _mask_structured(line):
    """Hide NIP, date, currency amount and PTU amount digits from the line scanner.

    The scanner then sees only the remainder. A shop NIP on the line above a
    date no longer becomes one run, and both lines can stay.
    """
    chars = list(line)

    def mask(start, end):
        for index in range(start, end):
            chars[index] = "x"

    span = _nip_body_span(line)
    if span is not None and valid_nip(re.sub(r"[ -]", "", line[span[0]:span[1]])):
        mask(*span)
    for pattern in (_DATE_ISO, _DATE_PL):
        for match in pattern.finditer(line):
            if pattern is _DATE_ISO:
                parsed = _iso_date(*match.groups())
            else:
                day, month, year = match.groups()
                parsed = _iso_date(year, month, day)
            if parsed is not None:
                mask(*match.span())
    for match in _CURRENCY_AMOUNT.finditer(line):
        mask(*match.span())
    if _PTU_WORD.search(line):
        for match in _PTU_AMOUNT.finditer(line):
            mask(*match.span())
    return "".join(chars)


def _secret_lines(text):
    bad = set()
    masked = "\n".join(_mask_structured(line) for line in text.split("\n"))
    _mark_digit_runs(masked, bad)
    _mark_ibans(text, bad)
    return bad


def _redact_normalized(text):
    """Replace each tainted line entirely. Other lines stay, including their digits."""
    bad = _secret_lines(text)
    if not bad:
        return text
    return "\n".join(_REMOVED if number in bad else line for number, line in enumerate(text.split("\n")))


def _contains_secret(value):
    return bool(_secret_lines(value))


def _nd_digits(value):
    """Decimal digits after NFKC. Letters and separators do not reset the count."""
    normalized = unicodedata.normalize("NFKC", value)
    return sum(unicodedata.category(char) == "Nd" for char in normalized)


def redact_digit_field(value):
    """Replace a persisted text field that carries twelve or more decimal digits."""
    if not isinstance(value, str):
        return value
    if _nd_digits(value) >= _SECRET_DIGITS:
        return _REMOVED
    return value


def _bound_raw_line(line):
    """Drop a line past the cap, then collapse a combining run to eight marks.

    Both happen before NFKC. A long run of combining marks makes CPython's
    canonical reordering quadratic, so the normaliser must not see one.
    """
    if len(line) > MAX_LINE_CHARS:
        return ""
    kept = []
    run = 0
    for char in line:
        if unicodedata.category(char)[0] == "M":
            run += 1
            if run <= _MAX_COMBINING:
                kept.append(char)
            continue
        run = 0
        kept.append(char)
    return "".join(kept)


def _normalize_receipt_text(text):
    """Allowlist after NFKC. The stored OCR text is this form.

    Line breaks are folded and each raw line is capped before NFKC, so
    compatibility digits still fold to ASCII and Polish letters still compose.
    One linear pass then keeps letters, ASCII digits, punctuation, symbols, an
    ASCII space and ``\\n``. A non-ASCII digit that NFKC left behind is mapped
    with ``unicodedata.digit``. Marks, format characters, other spaces, and
    every other category are removed.
    """
    bounded = "\n".join(
        _bound_raw_line(line)
        for line in text.replace("\r\n", "\n").translate(_LINE_BREAK).split("\n")
    )
    text = unicodedata.normalize("NFKC", bounded)
    if text.isascii() and _ASCII_DROPPED.search(text) is None:
        return text
    kept = []
    for char in text:
        if char == " " or char == "\n" or "0" <= char <= "9":
            kept.append(char)
            continue
        if ord(char) < 128:
            if char.isalpha() or unicodedata.category(char)[0] in "PS":
                kept.append(char)
            continue
        category = unicodedata.category(char)
        if category[0] in "LPS":
            kept.append(char)
            continue
        if category == "Nd":
            try:
                kept.append(str(unicodedata.digit(char)))
            except (TypeError, ValueError):
                continue
    return "".join(kept)


def _receipt_text(text):
    """Normalise, then reject oversized, too-long or control-bearing input before any PAN scan."""
    if not isinstance(text, str):
        raise ReceiptError()
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise ReceiptError() from None
    if size < 1 or size > MAX_TEXT_BYTES:
        raise ReceiptError()
    text = _normalize_receipt_text(text)
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise ReceiptError() from None
    # A line past the cap is dropped before NFKC, so a single oversized line can
    # normalise to empty. The raw byte check above already rejected an empty input.
    if size > MAX_TEXT_BYTES or _CONTROL.search(text) or text.count("\n") + 1 > MAX_TEXT_LINES:
        raise ReceiptError()
    return text


def redact_pan(text):
    return _redact_normalized(_receipt_text(text))


def _ascii_word(char):
    return char.isascii() and (char.isalnum() or char == "_")


def _nip_body_span(line):
    """Start and end of the first NIP body. The window is 18 characters and does not backtrack."""
    found = _NIP_LABEL.search(line)
    if found is None:
        return None
    index = found.end()
    limit = len(line)
    while index < limit and line[index] in _NIP_SPACE:
        index += 1
    if index < limit and line[index] == ":":
        index += 1
        while index < limit and line[index] in _NIP_SPACE:
            index += 1
    start = index
    if start >= limit or line[start] not in "0123456789":
        return None
    end = start
    cap = min(limit, start + 18)
    while end < cap and line[end] in _NIP_BODY:
        end += 1
    while end - start >= 10:
        if line[end - 1] in "0123456789" and (end == limit or not _ascii_word(line[end])):
            return start, end
        end -= 1
    return None


def _nip_candidate(line):
    """Digits from the first NIP body. The span is at most 18 characters and does not backtrack."""
    span = _nip_body_span(line)
    if span is None:
        return None
    return re.sub(r"[ -]", "", line[span[0]:span[1]])


def valid_nip(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{10}", value, re.ASCII):
        return False
    checksum = sum(int(digit) * weight for digit, weight in zip(value[:9], (6, 5, 7, 2, 3, 4, 5, 6, 7))) % 11
    return checksum != 10 and checksum == int(value[9])


def chain_for(shop_name, nip):
    if nip in _CHAIN_NIPS:
        return _CHAIN_NIPS[nip]
    key = fold_text(shop_name or "")
    return next((chain for chain, tokens in _CHAIN_TOKENS.items()
                 if any(token in key for token in tokens)), None)


def merchant_tokens(shop_name, nip):
    chain = chain_for(shop_name, nip)
    tokens = set(_CHAIN_TOKENS.get(chain, ()))
    for token in re.findall(r"[\w%]+", fold_text(shop_name or ""), re.UNICODE):
        if len(token) >= 4 and token not in _STOP_TOKENS:
            tokens.add(token)
    return frozenset(tokens)


def _bounded_money(value, *, mismatch=False):
    try:
        if not isinstance(value, str):
            raise ValueError
        normalized = value.replace(",", ".")
        if len(normalized) > 80 or not _MONEY.fullmatch(normalized):
            raise ValueError
        integer, _, fraction = normalized.partition(".")
        if (len(integer.removeprefix("-")) > MAX_MONEY_INTEGER_DIGITS
                or len(fraction) > MAX_MONEY_FRACTION_DIGITS
                or _contains_secret(normalized)):
            raise ValueError
        return canonical_money(normalized)
    except (ValueError, AnalyticsError):
        if mismatch:
            raise ReceiptError("budget_receipt_total_mismatch") from None
        raise ReceiptError() from None


def _decimal(value):
    return _bounded_money(value)


def _bounded_sum(values):
    """Reject an accumulated total that has left the receipt magnitude limit."""
    return _bounded_money(sum_money(values))


def _iso_date(year, month, day):
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def _shop_name(lines):
    for raw in lines[:12]:
        value = raw.strip()
        key = fold_text(value)
        if (not value or len(value) > MAX_SHOP_NAME_LEN or _CONTROL.search(value)
                or any(marker in key for marker in ("paragon", "nip", "fiskalny", "nr karty", "aid"))):
            continue
        if _DATE_ISO.search(value) or _DATE_PL.search(value) or not re.search(r"[A-Za-zĄ-ż]", value):
            continue
        return redact_digit_field(value)
    return None


def _line_kind(name, gross):
    key = fold_text(name)
    if "kaucja" in key:
        return "deposit"
    if compare_money(gross, "0") < 0 and any(token in key for token in ("kupon", "bon ", "rabat cal", "rabat na ")):
        return "basket_discount"
    return "item"


def _make_line(name, match, line_no):
    original = " ".join(name.strip().split())
    gross = _decimal(match["gross"])
    kind = _line_kind(original, gross)
    raw_name = redact_digit_field(original)
    raw_name_key = fold_text(raw_name)
    if (not raw_name or len(raw_name) > MAX_LINE_NAME_LEN or not raw_name_key
            or len(raw_name_key) > MAX_LINE_NAME_LEN or _CONTROL.search(raw_name)):
        raise ReceiptError()
    quantity = _decimal(match["quantity"] or "1")
    unit_price = _decimal(match["unit_price"] or gross)
    return {
        "line_no": line_no, "line_kind": kind, "raw_name": raw_name, "raw_name_key": raw_name_key,
        "ptu_code": match["ptu"].upper() if match["ptu"] else None,
        "quantity": quantity, "raw_unit": match["unit"].lower() if match["unit"] else None,
        "unit_price_raw": unit_price, "gross_line_total": gross, "discount_amount": "0",
        "net_line_total": gross,
    }


class _FieldMatch:
    def __init__(self, groups):
        self._groups = groups

    def __getitem__(self, key):
        return self._groups[key]


def _tail_match(value, tails):
    """Shortest non-empty name whose remainder matches a price tail.

    The caller has already collapsed whitespace. Each suffix starts at a single
    space and is anchored, so the name and the amount do not share a quantifier.
    """
    start = 0
    while True:
        space = value.find(" ", start)
        if space < 0:
            return None
        suffix = value[space + 1:]
        for tail in tails:
            match = tail.fullmatch(suffix)
            if match is not None:
                groups = {"name": value[:space], "quantity": None, "unit": None, "unit_price": None}
                groups.update(match.groupdict())
                return _FieldMatch(groups)
        start = space + 1


def _named_unit_match(value):
    return _tail_match(value, (_NAMED_UNIT_TAIL,))


def _item_match(value):
    return _tail_match(value, (_ITEM_QTY_TAIL, _ITEM_GROSS_TAIL))


def _parse_lines(lines):
    result, pending = [], None
    for raw in lines:
        value = " ".join(raw.split())
        if not value:
            continue
        discount = _DISCOUNT.match(value)
        basket = any(marker in fold_text(value) for marker in ("kupon", "bon ", "rabat cal", "rabat na "))
        if discount and not basket and result and result[-1]["line_kind"] == "item":
            amount = _decimal(discount["amount"].replace("+", ""))
            if compare_money(amount, "0") < 0:
                amount = negate_money(amount)
            line = result[-1]
            line["discount_amount"] = _bounded_sum((line["discount_amount"], amount))
            line["net_line_total"] = _bounded_sum((line["gross_line_total"], negate_money(line["discount_amount"])))
            pending = None
            continue
        match = _named_unit_match(value)
        bare_match = None if match is not None else _BARE_UNIT.fullmatch(value)
        if bare_match is not None:
            match = bare_match
            name = match["unit"]
        elif match is not None:
            name = (match["name"] or "").strip() or pending or match["unit"]
        else:
            match = _item_match(value)
            name = None
            if match is not None:
                name = match["name"]
            else:
                calculation = _CALCULATION.fullmatch(value)
                if calculation is not None and pending:
                    match, name = calculation, pending
        if match is None or name is None:
            key = fold_text(value)
            if (re.search(r"[A-Za-zĄ-ż]", value) and len(value) <= MAX_LINE_NAME_LEN
                    and not any(marker in key for marker in (
                        "suma", "sprzedaz", "ptu", "karta", "gotowka", "blik", "paragon", "nip", "zwrot",
                    ))):
                pending = value
            continue
        if match["ptu"] is None and match["quantity"] is None:
            key = fold_text(name)
            if "kaucja" not in key and not any(token in key for token in ("kupon", "bon ", "rabat cal", "rabat na ")):
                pending = value
                continue
        if len(result) >= MAX_RECEIPT_LINES:
            raise ReceiptError()
        result.append(_make_line(name, match, len(result) + 1))
        pending = None
    return result


def parse_receipt(text):
    text = redact_pan(text)
    # Drop a line before every parsing expression. A 32 KiB line must not reach one.
    lines = ["" if len(line) > MAX_LINE_CHARS else line for line in text.split("\n")]
    if len(lines) > MAX_TEXT_LINES:
        raise ReceiptError()
    nip = None
    for line in lines:
        candidate = _nip_candidate(line)
        if candidate and valid_nip(candidate):
            nip = candidate
            break
    shop_name = _shop_name(lines)
    receipt_date = None
    receipt_time = None
    total = None
    for line in lines:
        if receipt_date is None:
            match = _DATE_ISO.search(line)
            if match:
                receipt_date = _iso_date(*match.groups())
            else:
                match = _DATE_PL.search(line)
                if match:
                    day, month, year = match.groups()
                    receipt_date = _iso_date(year, month, day)
        if receipt_time is None:
            match = _TIME.search(line)
            if match:
                receipt_time = match[1]
        key = fold_text(line)
        if "suma pln" in key or "do zaplaty" in key:
            amounts = _NUMBER_RE.findall(line)
            if amounts:
                total = _decimal(amounts[-1])
    key = fold_text(text)
    has_card = "karta" in key or "platnicza" in key or "paypass" in key
    has_cash = "gotowka" in key
    payment = "mixed" if has_card and has_cash else "blik" if "blik" in key else "card" if has_card else "cash" if has_cash else "unknown"
    parsed_lines = _parse_lines(lines)
    math_delta = None if total is None else _bounded_sum((
        _bounded_sum(line["net_line_total"] for line in parsed_lines), negate_money(total)))
    return {
        "raw_shop_name": shop_name, "raw_nip": nip, "chain_key": chain_for(shop_name, nip),
        "receipt_kind": "refund" if "zwrot" in key else "purchase", "receipt_date": receipt_date,
        "receipt_time": receipt_time, "total_amount": total, "currency": "PLN" if total is not None else None,
        "payment_method": payment, "lines": parsed_lines,
        "math_delta": math_delta,
    }


def normalize_shop_name(value):
    """NFC plus collapsed whitespace. Candidate signatures and confirm both use this."""
    if not isinstance(value, str):
        return None
    return " ".join(unicodedata.normalize("NFC", value).split())


def _clean_text(value, *, limit, optional=False):
    if value is None or (optional and value == ""):
        if optional:
            return None
        raise ReceiptError()
    if not isinstance(value, str) or _CONTROL.search(value) or any(
            unicodedata.category(char) in ("Cf", "Cs") for char in value):
        raise ReceiptError()
    value = normalize_shop_name(value)
    value = redact_digit_field(value)
    if not value or len(value) > limit:
        raise ReceiptError()
    value = redact_pan(value)
    if not value or len(value) > limit:
        raise ReceiptError()
    return value


def _confirm_date(value):
    if not isinstance(value, str):
        raise ReceiptError()
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        raise ReceiptError() from None
    return value


def _confirm_money(value, *, mismatch=False):
    return _bounded_money(value, mismatch=mismatch)


def _validate_line_totals(value):
    kind = value["line_kind"]
    gross = _confirm_money(value["gross_line_total"], mismatch=True)
    discount = _confirm_money(value["discount_amount"], mismatch=True)
    net = _confirm_money(value["net_line_total"], mismatch=True)
    expected = sum_money((gross, negate_money(discount)))
    valid = net == expected
    if kind == "item":
        valid = valid and compare_money(gross, "0") >= 0 and compare_money(discount, "0") >= 0 and compare_money(net, "0") >= 0
    elif kind == "deposit":
        valid = valid and compare_money(gross, "0") != 0 and discount == "0" and net == gross
    elif kind == "basket_discount":
        valid = valid and compare_money(gross, "0") < 0 and discount == "0" and net == gross
    else:
        valid = valid and kind == "other" and discount == "0" and net == gross
    if not valid:
        raise ReceiptError("budget_receipt_total_mismatch")
    return gross, discount, net


def _validate_line(value, line_no):
    if not isinstance(value, dict) or set(value) != LINE_FIELDS:
        raise ReceiptError()
    kind = value["line_kind"]
    if kind not in LINE_KINDS:
        raise ReceiptError()
    name = _clean_text(value["raw_name"], limit=MAX_LINE_NAME_LEN)
    name_key = fold_text(name)
    if not name_key or len(name_key) > MAX_LINE_NAME_LEN:
        raise ReceiptError()
    ptu = value["ptu_code"]
    if ptu is not None and ptu not in PTU_CODES:
        raise ReceiptError()
    unit = _clean_text(value["raw_unit"], limit=16, optional=True)
    if _nd_digits(name + (unit or "")) >= _SECRET_DIGITS:
        raise ReceiptError()
    unit_price = value["unit_price_raw"]
    if unit_price is not None:
        unit_price = _confirm_money(unit_price)
    quantity = _confirm_money(value["quantity"])
    gross, discount, net = _validate_line_totals(value)
    return {
        "line_no": line_no, "line_kind": kind, "raw_name": name, "raw_name_key": name_key, "ptu_code": ptu,
        "quantity": quantity, "raw_unit": unit, "unit_price_raw": unit_price, "gross_line_total": gross,
        "discount_amount": discount, "net_line_total": net,
    }


def validate_confirm_line_totals(lines, total):
    nets = [_validate_line_totals(line)[2] for line in lines]
    if sum_money(nets) != total:
        raise ReceiptError("budget_receipt_total_mismatch")


def validate_confirm_lines(lines, total):
    if not isinstance(lines, list) or len(lines) > MAX_RECEIPT_LINES:
        raise ReceiptError()
    clean_lines = [_validate_line(line, number) for number, line in enumerate(lines, 1)]
    if sum_money(line["net_line_total"] for line in clean_lines) != total:
        raise ReceiptError("budget_receipt_total_mismatch")
    return clean_lines


def validate_confirm(values):
    if not isinstance(values, dict) or not _CONFIRM_REQUIRED.issubset(values) or set(values) - CONFIRM_FIELDS:
        raise ReceiptError()
    if (not isinstance(values["receipt_id"], str) or not _UUID.fullmatch(values["receipt_id"])
            or type(values["expected_version"]) is not int or values["expected_version"] < 1):
        raise ReceiptError()
    kind = values["receipt_kind"]
    payment = values["payment_method"]
    currency = values["currency"]
    if kind not in RECEIPT_KINDS or payment not in PAYMENT_METHODS or not isinstance(currency, str) or not _CURRENCY.fullmatch(currency):
        raise ReceiptError()
    cleaned = {
        "receipt_kind": kind, "currency": currency, "payment_method": payment,
    }
    if "raw_shop_name" in values:
        cleaned["raw_shop_name"] = _clean_text(values["raw_shop_name"], limit=MAX_SHOP_NAME_LEN, optional=True)
    if "raw_nip" in values:
        nip = values["raw_nip"]
        if nip == "":
            nip = None
        if nip is not None and not valid_nip(nip):
            raise ReceiptError()
        cleaned["raw_nip"] = nip
    if "raw_shop_name" in cleaned or "raw_nip" in cleaned:
        cleaned["chain_key"] = chain_for(cleaned.get("raw_shop_name"), cleaned.get("raw_nip"))
    receipt_time = values["receipt_time"]
    if receipt_time == "":
        receipt_time = None
    if receipt_time is not None and (not isinstance(receipt_time, str) or not _TIME.fullmatch(receipt_time)):
        raise ReceiptError()
    cleaned["receipt_time"] = receipt_time
    total = _confirm_money(values["total_amount"], mismatch=True)
    if compare_money(total, "0") <= 0:
        raise ReceiptError("budget_receipt_total_mismatch")
    cleaned["total_amount"] = total
    lines = values.get("lines")
    cleaned["lines"] = None if lines is None else validate_confirm_lines(lines, total)
    link = values["link"]
    if link is not None:
        token = None if not isinstance(link, dict) else link.get("candidate_token")
        if (payment in ("cash", "mixed") or not isinstance(link, dict)
                or set(link) != {"account_id", "entry_reference", "candidate_token"}
                or not isinstance(link["account_id"], str) or not 1 <= len(link["account_id"]) <= 128
                or not isinstance(link["entry_reference"], str) or not 1 <= len(link["entry_reference"]) <= 1024
                or not isinstance(token, str) or not 1 <= len(token) <= 4096 or "\n" in token or "\r" in token):
            raise ReceiptError()
    cleaned["link"] = link
    cleaned["receipt_date"] = _confirm_date(values["receipt_date"])
    return cleaned
