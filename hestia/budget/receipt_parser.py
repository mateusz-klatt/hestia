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
MAX_MONEY_INTEGER_DIGITS = 9
MAX_MONEY_FRACTION_DIGITS = 4
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
LINE_FIELDS = frozenset({
    "line_kind", "raw_name", "ptu_code", "quantity", "raw_unit", "unit_price_raw",
    "gross_line_total", "discount_amount", "net_line_total",
})

_NUMBER = r"[+-]?[0-9]+(?:[.,][0-9]+)?"
_NUMBER_RE = re.compile(_NUMBER, re.ASCII)
_MONEY = re.compile(r"-?[0-9]+(?:\.[0-9]+)?\Z", re.ASCII)
_DATE_ISO = re.compile(r"(?<![0-9])([0-9]{4})-([0-9]{2})-([0-9]{2})(?![0-9])", re.ASCII)
_DATE_PL = re.compile(r"(?<![0-9])([0-9]{2})[-.]([0-9]{2})[-.]([0-9]{4})(?![0-9])", re.ASCII)
_TIME = re.compile(r"(?<![0-9])((?:[01][0-9]|2[0-3]):[0-5][0-9])(?![0-9])", re.ASCII)
_NIP = re.compile(r"\bNIP\s*:?\s*([0-9][0-9 -]{8,16}[0-9])\b", re.IGNORECASE | re.ASCII)
# A PAN run is digits, a mask sequence, and the separators between them.
# Whitespace covers ASCII and Unicode spaces. Dashes U+2010–U+2015, "-" and "."
# join a run. A comma does not, so "12,99" stays a price. Line breaks do not,
# so a NIP and a date on adjacent lines stay apart.
_PAN_NEWLINES = frozenset("\n\r\u2028\u2029")
_PAN_DASHES = frozenset("-‐‑‒–—―")
_PAN_MASKS = frozenset("*Xx•●#")
_CARD_LABEL = re.compile(
    r"(?i)(?:\bNR\s+KARTY\b|\bNUMER\s+KARTY\b|\bAID\b|\bPAN\b|\bCARD(?:\s+(?:NO|NUMBER))?\b|\bKARTA\b)"
)
_ITEM = re.compile(
    rf"^(?P<name>.+?)\s+(?:(?P<quantity>{_NUMBER})\s*(?P<unit>kg|g|dag|l|ml|szt|op)?"
    rf"\s*[x×*]\s*(?P<unit_price>{_NUMBER})\s*(?:=\s*)?)?"
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
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

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


def _pan_separator(char):
    if char in _PAN_NEWLINES:
        return False
    return char == "." or char in _PAN_DASHES or char.isspace()


def _pan_digit(char):
    if "0" <= char <= "9":
        return char
    if unicodedata.category(char) == "Nd":
        try:
            return str(unicodedata.digit(char))
        except (TypeError, ValueError):
            return None
    return None


def _mask_length(text, index):
    length = len(text)
    end = index
    while end < length and text[end] in _PAN_MASKS:
        end += 1
    return end - index


def _digit_length(text, index):
    length = len(text)
    end = index
    while end < length and _pan_digit(text[end]) is not None:
        end += 1
    return end - index


def _symbol_runs(text):
    """Maximal digit, mask and separator runs. One forward pass.

    A solitary mask stays an operator (``2 x 3,50``). Two or more mask
    characters form a mask sequence. The whole run is a secret when it holds
    13 or more digit and mask symbols and at least one digit, or when a mask
    sequence of two or more sits next to two or more digits (``****1234``).
    """
    found = []
    length = len(text)
    index = 0
    while index < length:
        # A comma keeps the fractional part of a price out of the next run:
        # "9999,9999 999999999" must not become one 13-symbol run.
        if index and text[index - 1] == "," and _mask_length(text, index) == 0:
            count = _digit_length(text, index)
            if count:
                index += count
                continue
        masks = _mask_length(text, index)
        if masks == 1 or (masks == 0 and _digit_length(text, index) == 0):
            index += 1
            continue
        start = index
        symbols = 0
        digits = 0
        previous = None
        previous_len = 0
        adjacent = False
        while index < length:
            masks = _mask_length(text, index)
            if masks >= 2:
                symbols += masks
                if previous == "d" and previous_len >= 2:
                    adjacent = True
                previous, previous_len = "m", masks
                index += masks
                continue
            if masks == 1:
                break
            count = _digit_length(text, index)
            if count:
                symbols += count
                digits += count
                if previous == "m" and previous_len >= 2 and count >= 2:
                    adjacent = True
                previous, previous_len = "d", count
                index += count
                continue
            if _pan_separator(text[index]):
                gap = index + 1
                while gap < length and _pan_separator(text[gap]):
                    gap += 1
                if gap < length and (_mask_length(text, gap) >= 2 or _digit_length(text, gap)):
                    index = gap
                    continue
            break
        found.append((start, index, adjacent or (digits > 0 and symbols >= 13)))
    return found


def _word_at(line, index, word):
    end = index + len(word)
    if end > len(line) or line[index:end].casefold() != word:
        return False
    before = index == 0 or not (line[index - 1].isalnum() or line[index - 1] == "_")
    after = end == len(line) or not (line[end].isalnum() or line[end] == "_")
    return before and after


def _ends_word(line, word):
    end = len(line)
    while end and line[end - 1].isspace():
        end -= 1
    return _word_at(line, end - len(word), word) if end >= len(word) else False


def _starts_word(line, word):
    index = 0
    length = len(line)
    while index < length and line[index].isspace():
        index += 1
    return _word_at(line, index, word)


def _split_card_label(line, nxt):
    """NR/NUMER and KARTY land on adjacent OCR lines. ``\\s`` in the label covers that newline."""
    return (_ends_word(line, "nr") or _ends_word(line, "numer")) and _starts_word(nxt, "karty")


def _pan_spans(text):
    """Whole-run spans plus card-label runs. No window search."""
    runs = _symbol_runs(text)
    lines = text.split("\n")
    label = [False] * len(lines)
    for number, line in enumerate(lines):
        if _CARD_LABEL.search(line) is not None:
            label[number] = True
        if number + 1 < len(lines) and _split_card_label(line, lines[number + 1]):
            label[number] = True
            label[number + 1] = True
    follow = [False] * len(lines)
    for number, flagged in enumerate(label):
        if not flagged:
            continue
        if number + 1 < len(lines):
            follow[number + 1] = True
        if number + 2 < len(lines):
            follow[number + 2] = True
    bases = []
    offset = 0
    for line in lines:
        bases.append(offset)
        offset += len(line) + 1
    spans = []
    line_no = 0
    seen = [False] * len(lines)
    for start, end, secret in runs:
        while line_no + 1 < len(bases) and start >= bases[line_no + 1]:
            line_no += 1
        first = not seen[line_no]
        seen[line_no] = True
        if secret or label[line_no] or (follow[line_no] and first):
            spans.append((start, end))
    return spans


def _redact_spans(text, spans):
    if not spans:
        return text
    parts = []
    cursor = 0
    for start, end in spans:
        parts.append(text[cursor:start])
        parts.append("[REDACTED]")
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def _receipt_text(text):
    """Reject oversized, too-long or control-bearing input before any PAN scan."""
    if not isinstance(text, str):
        raise ReceiptError()
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise ReceiptError() from None
    if size < 1 or size > MAX_TEXT_BYTES or _CONTROL.search(text):
        raise ReceiptError()
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise ReceiptError() from None
    if size > MAX_TEXT_BYTES or _CONTROL.search(text) or text.count("\n") + 1 > MAX_TEXT_LINES:
        raise ReceiptError()
    return text


def redact_pan(text):
    text = _receipt_text(text)
    return _redact_spans(text, _pan_spans(text))


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
                or len(fraction) > MAX_MONEY_FRACTION_DIGITS):
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
        return value
    return None


def _line_kind(name, gross):
    key = fold_text(name)
    if "kaucja" in key:
        return "deposit"
    if compare_money(gross, "0") < 0 and any(token in key for token in ("kupon", "bon ", "rabat cal", "rabat na ")):
        return "basket_discount"
    return "item"


def _make_line(name, match, line_no):
    raw_name = " ".join(name.strip().split())
    raw_name_key = fold_text(raw_name)
    if (not raw_name or len(raw_name) > MAX_LINE_NAME_LEN or not raw_name_key
            or len(raw_name_key) > MAX_LINE_NAME_LEN or _CONTROL.search(raw_name)):
        raise ReceiptError()
    gross = _decimal(match["gross"])
    quantity = _decimal(match["quantity"] or "1")
    unit_price = _decimal(match["unit_price"] or gross)
    kind = _line_kind(raw_name, gross)
    return {
        "line_no": line_no, "line_kind": kind, "raw_name": raw_name, "raw_name_key": raw_name_key,
        "ptu_code": match["ptu"].upper() if match["ptu"] else None,
        "quantity": quantity, "raw_unit": match["unit"].lower() if match["unit"] else None,
        "unit_price_raw": unit_price, "gross_line_total": gross, "discount_amount": "0",
        "net_line_total": gross,
    }


def _parse_lines(lines):
    result, pending = [], None
    for raw in lines:
        value = raw.strip()
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
        match = _ITEM.fullmatch(value)
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
    lines = text.split("\n")
    if len(lines) > MAX_TEXT_LINES:
        raise ReceiptError()
    nip = None
    for line in lines:
        match = _NIP.search(line)
        if match:
            candidate = re.sub(r"[ -]", "", match[1])
            if valid_nip(candidate):
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
        "raw_ocr_text": text,
    }


def _clean_text(value, *, limit, optional=False):
    if value is None or (optional and value == ""):
        if optional:
            return None
        raise ReceiptError()
    if not isinstance(value, str) or _CONTROL.search(value) or any(
            unicodedata.category(char) in ("Cf", "Cs") for char in value):
        raise ReceiptError()
    value = " ".join(unicodedata.normalize("NFC", value).split())
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
    unit_price = value["unit_price_raw"]
    if unit_price is not None:
        unit_price = _confirm_money(unit_price)
    quantity = _confirm_money(value["quantity"])
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
        valid = valid and discount == "0" and net == gross
    if not valid:
        raise ReceiptError("budget_receipt_total_mismatch")
    return {
        "line_no": line_no, "line_kind": kind, "raw_name": name, "raw_name_key": name_key, "ptu_code": ptu,
        "quantity": quantity, "raw_unit": unit, "unit_price_raw": unit_price, "gross_line_total": gross,
        "discount_amount": discount, "net_line_total": net,
    }


def validate_confirm(values):
    if not isinstance(values, dict) or set(values) != CONFIRM_FIELDS:
        raise ReceiptError()
    if (not isinstance(values["receipt_id"], str) or not _UUID.fullmatch(values["receipt_id"])
            or type(values["expected_version"]) is not int or values["expected_version"] < 1):
        raise ReceiptError()
    kind = values["receipt_kind"]
    payment = values["payment_method"]
    currency = values["currency"]
    if kind not in RECEIPT_KINDS or payment not in PAYMENT_METHODS or not isinstance(currency, str) or not _CURRENCY.fullmatch(currency):
        raise ReceiptError()
    shop = _clean_text(values["raw_shop_name"], limit=MAX_SHOP_NAME_LEN, optional=True)
    nip = values["raw_nip"]
    if nip == "":
        nip = None
    if nip is not None and not valid_nip(nip):
        raise ReceiptError()
    receipt_time = values["receipt_time"]
    if receipt_time == "":
        receipt_time = None
    if receipt_time is not None and (not isinstance(receipt_time, str) or not _TIME.fullmatch(receipt_time)):
        raise ReceiptError()
    total = _confirm_money(values["total_amount"], mismatch=True)
    if compare_money(total, "0") <= 0:
        raise ReceiptError("budget_receipt_total_mismatch")
    lines = values["lines"]
    if not isinstance(lines, list) or len(lines) > MAX_RECEIPT_LINES:
        raise ReceiptError()
    clean_lines = [_validate_line(line, number) for number, line in enumerate(lines, 1)]
    if sum_money(line["net_line_total"] for line in clean_lines) != total:
        raise ReceiptError("budget_receipt_total_mismatch")
    link = values["link"]
    if link is not None:
        token = None if not isinstance(link, dict) else link.get("candidate_token")
        if (payment in ("cash", "mixed") or not isinstance(link, dict)
                or set(link) != {"account_id", "entry_reference", "candidate_token"}
                or not isinstance(link["account_id"], str) or not 1 <= len(link["account_id"]) <= 128
                or not isinstance(link["entry_reference"], str) or not 1 <= len(link["entry_reference"]) <= 1024
                or not isinstance(token, str) or not 1 <= len(token) <= 4096 or "\n" in token or "\r" in token):
            raise ReceiptError()
    return {
        "raw_shop_name": shop, "raw_nip": nip, "receipt_kind": kind, "receipt_date": _confirm_date(values["receipt_date"]),
        "receipt_time": receipt_time, "total_amount": total, "currency": currency, "payment_method": payment,
        "lines": clean_lines, "link": link, "chain_key": chain_for(shop, nip),
    }
