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
# Stream breaks use one separator and a real calendar day. Mixed separators and
# spaces are not dates. A two-digit year is only the dotted ``DD.MM.YY`` form.
_STREAM_DATE = re.compile(
    r"(?<![0-9])(?:"
    r"(?P<iso_y>[0-9]{4})-(?P<iso_m>[0-9]{2})-(?P<iso_d>[0-9]{2})"
    r"|(?P<dmy_d>[0-9]{2})-(?P<dmy_m>[0-9]{2})-(?P<dmy_y>[0-9]{4})"
    r"|(?P<slash_d>[0-9]{2})/(?P<slash_m>[0-9]{2})/(?P<slash_y>[0-9]{4})"
    r"|(?P<dot_d>[0-9]{2})\.(?P<dot_m>[0-9]{2})\.(?P<dot_y>[0-9]{4}|[0-9]{2})"
    r")(?![0-9])",
    re.ASCII,
)
_TIME = re.compile(r"(?<![0-9])((?:[01][0-9]|2[0-3]):[0-5][0-9])(?![0-9])", re.ASCII)
_NIP = re.compile(r"\bNIP\s*:?\s*([0-9][0-9 -]{8,16}[0-9])\b", re.IGNORECASE | re.ASCII)
# A candidate is digits and mask symbols on one line. Spaces, hyphens, dots and
# commas stay inside it. A letter breaks it, as does a gap of two or more
# characters that are not symbols or those separators. Near a card label every
# 13–19 Luhn window is a PAN, prices included. Elsewhere a PAN uses only spaces
# and hyphens, so a window that crosses a decimal price is left intact.
# On the label line and the next two, every digit run of two or more is removed
# except a quantity written ``\d+ ?(szt|kg|g|l|x)``. Those three lines also share
# one digit stream, so a PAN wrapped across them is removed with every fragment
# that contributed a digit or a mask.
# Everywhere, each window of up to three lines joins digit runs that are
# separated only by spaces, hyphens or the newline. Windows of 13–19 symbols
# on that stream are tested for Luhn and for a mask beside digits. A price
# token breaks the stream. A date breaks it only when it is a real calendar
# day in 2000–2099 written with one canonical separator. Beside a card label
# a date does not break the stream.
_PAN_NEWLINES = frozenset("\n\r\u2028\u2029")
_PAN_DASHES = frozenset("-‐‑‒–—―")
_PAN_MASKS = frozenset("*Xx•●#")
_CARD_LABEL = re.compile(
    r"(?i)(?:\bNR\s+KARTY\b|\bNUMER\s+KARTY\b|\bAID\b|\bPAN\b|\bCARD(?:\s+(?:NO|NUMBER))?\b|\bKARTA\b)"
)
_LABEL_QTY = re.compile(r"(?<![0-9])([0-9]+) ?(?:szt|kg|g|l|x)\b", re.IGNORECASE)
_ITEM = re.compile(
    rf"^(?P<name>.+?)\s+(?:(?P<quantity>{_NUMBER})\s*(?P<unit>kg|g|dag|l|ml|szt|op)?"
    rf"\s*[x×*]\s*(?P<unit_price>{_NUMBER})\s*(?:=\s*)?)?"
    rf"(?P<gross>{_NUMBER})\s*(?:PLN\s*)?(?P<ptu>[A-G])?$",
    re.IGNORECASE | re.ASCII,
)
# ``1000 szt 3,49`` has no multiplication sign. The quantity is still multi-digit.
_NAMED_UNIT = re.compile(
    rf"^(?P<name>.+?)\s+(?P<quantity>{_NUMBER})\s*(?P<unit>kg|g|dag|l|ml|szt|op)"
    rf"(?:\s*[x×*]\s*(?P<unit_price>{_NUMBER})\s*(?:=\s*)?)?"
    rf"\s+(?P<gross>{_NUMBER})\s*(?:PLN\s*)?(?P<ptu>[A-G])?$",
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
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# ASCII controls still present after line terminators have become ``\n``.
_ASCII_DROPPED = re.compile(r"[\x00-\x08\t\x0e-\x1f\x7f]")
# Line terminators become ``\n`` before the control check, so a PAN split by
# U+2028 or a vertical tab is one receipt, not rejected or left in one line.
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


def _pan_separator(char):
    """Space, hyphen, dot or comma. Letters and other gaps are not separators."""
    if char in _PAN_NEWLINES:
        return False
    return char in ".," or char in _PAN_DASHES or char.isspace()


def _pan_digit(char):
    if "0" <= char <= "9":
        return char
    if unicodedata.category(char) == "Nd":
        try:
            return str(unicodedata.digit(char))
        except (TypeError, ValueError):
            return None
    return None


def _is_letter(char):
    return unicodedata.category(char).startswith("L")


def _luhn(digits):
    """True when ``digits`` is 13–19 characters and passes the Luhn check."""
    length = len(digits)
    if length < 13 or length > 19:
        return False
    total = 0
    double = False
    for char in reversed(digits):
        value = ord(char) - 48
        if double:
            value *= 2
            if value > 9:
                value -= 9
        total += value
        double = not double
    return total % 10 == 0


def _money_ranges(text):
    """ASCII ``\\d{1,6}[,.]\\d{2,4}`` spans. Neighbouring digits stay visible."""
    found = []
    length = len(text)
    index = 0
    while index < length:
        if not ("0" <= text[index] <= "9") or (index and "0" <= text[index - 1] <= "9"):
            index += 1
            continue
        cursor = index
        digits = 0
        while cursor < length and "0" <= text[cursor] <= "9":
            digits += 1
            cursor += 1
        if 1 <= digits <= 6 and cursor < length and text[cursor] in ",.":
            fraction = cursor + 1
            frac_digits = 0
            while fraction < length and "0" <= text[fraction] <= "9":
                frac_digits += 1
                fraction += 1
            if 2 <= frac_digits <= 4 and (fraction == length or not ("0" <= text[fraction] <= "9")):
                found.append((index, fraction))
                index = fraction
                continue
        index = cursor if cursor > index else index + 1
    return found


def _line_candidates(line):
    """Digit and mask symbols grouped into candidates. One forward pass.

    Each symbol is ``(index, kind, value)`` with kind ``d`` or ``m``. A dot,
    a comma, a space or a hyphen does not break the candidate. A letter does,
    and so does a run of two or more other non-symbol characters.
    """
    symbols = []
    candidates = []
    length = len(line)
    index = 0
    start = None
    while index < length:
        digit = _pan_digit(line[index])
        if digit is not None or line[index] in _PAN_MASKS:
            if start is None:
                start = len(symbols)
            symbols.append((index, "d" if digit is not None else "m", digit if digit is not None else line[index]))
            index += 1
            continue
        if start is None:
            index += 1
            continue
        if _is_letter(line[index]):
            candidates.append((start, len(symbols)))
            start = None
            index += 1
            continue
        gap = index
        while (gap < length and _pan_digit(line[gap]) is None and line[gap] not in _PAN_MASKS
                and not _is_letter(line[gap])):
            gap += 1
        other = sum(1 for cursor in range(index, gap) if not _pan_separator(line[cursor]))
        if other >= 2:
            candidates.append((start, len(symbols)))
            start = None
        index = gap
    if start is not None:
        candidates.append((start, len(symbols)))
    return symbols, candidates


def _mask_next_to_digits(symbols):
    """A mask sequence of two or more sits beside two or more digits."""
    runs = []
    for _, kind, _ in symbols:
        if runs and runs[-1][0] == kind:
            runs[-1][1] += 1
        else:
            runs.append([kind, 1])
    for index, (kind, length) in enumerate(runs):
        if kind != "m" or length < 2:
            continue
        if index and runs[index - 1][0] == "d" and runs[index - 1][1] >= 2:
            return True
        if index + 1 < len(runs) and runs[index + 1][0] == "d" and runs[index + 1][1] >= 2:
            return True
    return False


def _window_is_plain(line, symbols):
    """True when every separator in the window is a space or a hyphen."""
    start = symbols[0][0] + 1
    end = symbols[-1][0]
    for index in range(start, end):
        char = line[index]
        if _pan_digit(char) is not None or char in _PAN_MASKS:
            continue
        if char in _PAN_DASHES or (char.isspace() and char not in _PAN_NEWLINES):
            continue
        return False
    return True


def _digits_are_prices(symbols, cover):
    saw = False
    for index, kind, _ in symbols:
        if kind != "d":
            continue
        saw = True
        if not cover[index]:
            return False
    return saw


def _flag_candidate(line, symbols, cover, label_context):
    """Mark PAN symbols. Lengths 13–19 only, so the scan stays linear.

    A mask sequence beside digits always flags the candidate. A Luhn window
    does too. Away from a card label the window may contain only spaces and
    hyphens: a decimal inside ``\\d{1,6}[,.]\\d{2,4}`` means the window is a
    price. Beside a card label that exemption is off, so price-shaped groups
    are still a PAN. The whole candidate is the secret, which keeps a ``.00``
    suffix from leaving the last group visible.
    """
    count = len(symbols)
    if not count:
        return bytearray()
    if _mask_next_to_digits(symbols):
        return bytearray(b"\x01" * count)
    if count < 13 or not any(kind == "d" for _, kind, _ in symbols):
        return bytearray(count)
    # Each decimal price holds at most 10 digits, so a 13-symbol window in an
    # all-price run always crosses a decimal. Skip the Luhn scan.
    if not label_context and _digits_are_prices(symbols, cover):
        return bytearray(count)
    for length in range(13, min(19, count) + 1):
        for start in range(count - length + 1):
            window = symbols[start:start + length]
            if not label_context and not _window_is_plain(line, window):
                continue
            digits = "".join(value for _, kind, value in window if kind == "d")
            if _luhn(digits):
                return bytearray(b"\x01" * count)
    return bytearray(count)


def _candidate_spans(symbols, flagged, money_ranges):
    spans = []
    index = 0
    count = len(symbols)
    while index < count:
        if not flagged[index]:
            index += 1
            continue
        end = index + 1
        while end < count and flagged[end]:
            end += 1
        start_at = symbols[index][0]
        end_at = symbols[end - 1][0] + 1
        for money_start, money_end in money_ranges:
            if money_start < end_at and money_end > start_at:
                start_at = min(start_at, money_start)
                end_at = max(end_at, money_end)
        spans.append((start_at, end_at))
        index = end
    return spans


def _label_quantity_spans(line):
    """Digit runs of ``\\d+ ?(szt|kg|g|l|x)``. The unit itself stays visible."""
    return [(match.start(1), match.end(1)) for match in _LABEL_QTY.finditer(line)]


def _label_digit_spans(line):
    """Every digit run of 2 or more beside a card label.

    A price is removed too: the card section is not an item line. The only
    digits that stay are a quantity directly followed by a unit.
    """
    protected = bytearray(len(line))
    for start, end in _label_quantity_spans(line):
        protected[start:end] = b"\x01" * (end - start)
    spans = []
    index = 0
    length = len(line)
    while index < length:
        if _pan_digit(line[index]) is None:
            index += 1
            continue
        start = index
        while index < length and _pan_digit(line[index]) is not None:
            index += 1
        if index - start >= 2 and not any(protected[cursor] for cursor in range(start, index)):
            spans.append((start, index))
    return spans


def _symbol_fragments(line):
    """Maximal digit or mask runs, each with ``(kind, value)`` symbols."""
    fragments = []
    length = len(line)
    index = 0
    while index < length:
        if _pan_digit(line[index]) is None and line[index] not in _PAN_MASKS:
            index += 1
            continue
        start = index
        symbols = []
        while index < length and (_pan_digit(line[index]) is not None or line[index] in _PAN_MASKS):
            digit = _pan_digit(line[index])
            if digit is not None:
                symbols.append(("d", digit))
            else:
                symbols.append(("m", line[index]))
            index += 1
        fragments.append((start, index, symbols))
    return fragments


def _label_stream_flags(symbols):
    """Luhn and mask flags for the digits of one label and the next two lines.

    Luhn sees the digit stream only, so a mask or a dropped separator cannot
    shift the window. A mask sequence beside digits flags that sequence and
    the neighbouring digit run. Symbols outside a hit stay clear.
    """
    flagged = bytearray(len(symbols))
    runs = []
    for index, (kind, _) in enumerate(symbols):
        if runs and runs[-1][0] == kind:
            runs[-1][2] = index + 1
        else:
            runs.append([kind, index, index + 1])
    for index, (kind, start, end) in enumerate(runs):
        if kind != "m" or end - start < 2:
            continue
        if index and runs[index - 1][0] == "d" and runs[index - 1][2] - runs[index - 1][1] >= 2:
            left = runs[index - 1][1]
            flagged[left:end] = b"\x01" * (end - left)
        if index + 1 < len(runs) and runs[index + 1][0] == "d" and runs[index + 1][2] - runs[index + 1][1] >= 2:
            right = runs[index + 1][2]
            flagged[start:right] = b"\x01" * (right - start)
    digit_index = [index for index, (kind, _) in enumerate(symbols) if kind == "d"]
    digit_count = len(digit_index)
    if digit_count < 13:
        return flagged
    digits = "".join(symbols[index][1] for index in digit_index)
    for length in range(13, min(19, digit_count) + 1):
        for start in range(digit_count - length + 1):
            if not _luhn(digits[start:start + length]):
                continue
            for offset in range(length):
                flagged[digit_index[start + offset]] = 1
    return flagged


def _label_stream_spans(fragments, first, limit, offsets):
    """Redact each fragment that contributed a symbol to a flagged window."""
    symbols = []
    owners = []
    for number in range(first, limit):
        for start, end, frag_symbols in fragments[number]:
            for kind, value in frag_symbols:
                symbols.append((kind, value))
                owners.append((number, start, end))
    if not symbols:
        return []
    flagged = _label_stream_flags(symbols)
    spans = []
    seen = set()
    for index, mark in enumerate(flagged):
        if not mark:
            continue
        number, start, end = owners[index]
        key = (number, start, end)
        if key in seen:
            continue
        seen.add(key)
        spans.append((offsets[number] + start, offsets[number] + end))
    return spans


def _line_secret_spans(line, label_context):
    symbols, candidates = _line_candidates(line)
    if not symbols:
        return []
    ranges = _money_ranges(line)
    cover = bytearray(len(line))
    for start, end in ranges:
        cover[start:end] = b"\x01" * (end - start)
    spans = []
    for start, end in candidates:
        window = symbols[start:end]
        flagged = _flag_candidate(line, window, cover, label_context)
        spans.extend(_candidate_spans(window, flagged, ranges))
    return spans


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


def _merge_spans(spans):
    if not spans:
        return []
    ordered = sorted(spans)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _stream_date_parts(match):
    if match.group("iso_y") is not None:
        return match.group("iso_y"), match.group("iso_m"), match.group("iso_d")
    if match.group("dmy_y") is not None:
        return match.group("dmy_y"), match.group("dmy_m"), match.group("dmy_d")
    if match.group("slash_y") is not None:
        return match.group("slash_y"), match.group("slash_m"), match.group("slash_d")
    return match.group("dot_y"), match.group("dot_m"), match.group("dot_d")


def _real_calendar_date(year, month, day):
    """True when the parts are a real day and the year is 2000–2099.

    A two-digit year is ``20xx``.
    """
    if len(year) == 2:
        year = "20" + year
    if len(year) != 4:
        return False
    try:
        parsed = date(int(year), int(month), int(day))
    except ValueError:
        return False
    return 2000 <= parsed.year <= 2099


def _span_overlaps(span, others):
    start, end = span
    return any(start < other_end and end > other_start for other_start, other_end in others)


def _covered_by(span, covers):
    start, end = span
    return any(cover_start <= start and end <= cover_end for cover_start, cover_end in covers)


def _date_break_and_joiner_spans(line):
    """Real canonical dates break the stream. Invalid date shapes do not.

    A dotted invalid day such as ``31.13.0000`` would otherwise be dropped as
    a date and as a price. Its separators join the digits instead, so a PAN
    wrapped across the newline still meets the scanner.
    """
    real = []
    joiners = []
    for match in _STREAM_DATE.finditer(line):
        year, month, day = _stream_date_parts(match)
        span = (match.start(), match.end())
        if _real_calendar_date(year, month, day):
            real.append(span)
        else:
            joiners.append(span)
    for match in _DATE_PL.finditer(line):
        span = (match.start(), match.end())
        if _covered_by(span, real):
            continue
        day, month, year = match.groups()
        separators = {char for char in match.group(0) if char in ".-"}
        if len(separators) == 1 and _real_calendar_date(year, month, day):
            continue
        joiners.append(span)
    return real, joiners


def _stream_break_ranges(line, *, break_dates=True):
    """Price tokens, and receipt dates that are real calendar days.

    A hyphenated date such as ``2026-11-20`` would otherwise join a NIP across
    the newline, and that combined digit string can pass Luhn. An invalid
    month or day is not a date, so those digits stay in the stream. Inside a
    card-label window ``break_dates`` is false and a date exempts nothing.
    """
    real, joiners = _date_break_and_joiner_spans(line) if break_dates else ([], [])
    ranges = [span for span in _money_ranges(line) if not _span_overlaps(span, joiners)]
    ranges.extend(real)
    return _merge_spans(ranges), _merge_spans(joiners)


def _plain_line_tokens(line, *, break_dates=True):
    """Digit and mask runs, with ``None`` where the plain stream must break.

    Spaces and hyphens are skipped, so they join the runs on either side.
    A dot, a comma, a letter or any other character breaks the stream.
    A dot or slash inside an invalid date shape joins, same as a hyphen.
    """
    breaks, joiners = _stream_break_ranges(line, break_dates=break_dates)
    tokens = []
    length = len(line)
    index = 0
    break_at = 0
    joiner_at = 0
    joiner_count = len(joiners)

    def in_joiner(at):
        nonlocal joiner_at
        while joiner_at < joiner_count and joiners[joiner_at][1] <= at:
            joiner_at += 1
        return joiner_at < joiner_count and joiners[joiner_at][0] <= at

    while index < length:
        if break_at < len(breaks) and breaks[break_at][1] <= index:
            break_at += 1
        if break_at < len(breaks) and breaks[break_at][0] <= index < breaks[break_at][1]:
            tokens.append(None)
            index = breaks[break_at][1]
            break_at += 1
            continue
        digit = _pan_digit(line[index])
        if digit is not None or line[index] in _PAN_MASKS:
            start = index
            kind = "d" if digit is not None else "m"
            symbols = []
            while index < length:
                if break_at < len(breaks) and breaks[break_at][0] <= index < breaks[break_at][1]:
                    break
                digit = _pan_digit(line[index])
                if digit is not None:
                    this, value = "d", digit
                elif line[index] in _PAN_MASKS:
                    this, value = "m", line[index]
                else:
                    break
                if this != kind:
                    break
                symbols.append((this, value))
                index += 1
            tokens.append((start, index, symbols))
            continue
        if line[index] in _PAN_DASHES or (line[index].isspace() and line[index] not in _PAN_NEWLINES):
            index += 1
            continue
        if line[index] in "./" and in_joiner(index):
            index += 1
            continue
        tokens.append(None)
        index += 1
    return tokens


def _plain_window_flags(symbols):
    """Flag symbols inside 13–19 windows that pass Luhn or sit beside a mask."""
    count = len(symbols)
    flagged = bytearray(count)
    # A mask run with no digits is not a PAN. Skip the 13–19 windows.
    if count < 13 or not any(kind == "d" for kind, _ in symbols):
        return flagged
    for length in range(13, min(19, count) + 1):
        last = count - length + 1
        for start in range(last):
            window = symbols[start:start + length]
            runs = []
            for kind, _ in window:
                if runs and runs[-1][0] == kind:
                    runs[-1][1] += 1
                else:
                    runs.append([kind, 1])
            hit = False
            for index, (kind, run) in enumerate(runs):
                if kind != "m" or run < 2:
                    continue
                left = index and runs[index - 1][0] == "d" and runs[index - 1][1] >= 2
                right = (index + 1 < len(runs) and runs[index + 1][0] == "d"
                         and runs[index + 1][1] >= 2)
                if left or right:
                    hit = True
                    break
            if not hit:
                digits = "".join(value for kind, value in window if kind == "d")
                hit = _luhn(digits)
            if not hit:
                continue
            flagged[start:start + length] = b"\x01" * length
    return flagged


def _flagged_fragment_spans(fragments):
    """Redact each fragment that contributed a symbol to a flagged window."""
    symbols = []
    owners = []
    for index, (_, _, frag_symbols) in enumerate(fragments):
        for kind, value in frag_symbols:
            symbols.append((kind, value))
            owners.append(index)
    flagged = _plain_window_flags(symbols)
    spans = []
    seen = set()
    for index, mark in enumerate(flagged):
        if not mark:
            continue
        owner = owners[index]
        if owner in seen:
            continue
        seen.add(owner)
        start, end, _ = fragments[owner]
        spans.append((start, end))
    return spans


def _cross_line_plain_spans(lines, offsets, break_dates):
    """Join plain groups across each window of up to three lines.

    Each line is visited at most three times, and only windows of 13–19 symbols
    are tested, so the scan stays linear. ``break_dates`` is false on a card
    label and the next two lines, where a date exempts nothing.
    """
    tokens = [
        _plain_line_tokens(line, break_dates=break_dates[index])
        for index, line in enumerate(lines)
    ]
    spans = []
    total = len(lines)
    for start in range(total):
        limit = min(start + 3, total)
        stream = []

        def flush():
            if stream:
                spans.extend(_flagged_fragment_spans(stream))
                stream.clear()

        for number in range(start, limit):
            for token in tokens[number]:
                if token is None:
                    flush()
                    continue
                local_start, local_end, symbols = token
                stream.append((offsets[number] + local_start, offsets[number] + local_end, symbols))
        flush()
    return spans


def _pan_spans(text):
    """PAN windows per line, then label fragments, then plain groups across three lines.

    A label line and the next two share one digit stream. Separately, every
    window of up to three lines joins groups that use only spaces and hyphens.
    A Luhn or mask hit removes each fragment that contributed.
    """
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
    spans = []
    offsets = []
    offset = 0
    for number, line in enumerate(lines):
        offsets.append(offset)
        context = label[number] or follow[number]
        for start, end in _line_secret_spans(line, context):
            spans.append((offset + start, offset + end))
        if context:
            for start, end in _label_digit_spans(line):
                spans.append((offset + start, offset + end))
        offset += len(line) + 1
    if any(label):
        fragments = [None] * len(lines)
        for number, flagged in enumerate(label):
            if not flagged:
                continue
            limit = min(number + 3, len(lines))
            for index in range(number, limit):
                if fragments[index] is None:
                    fragments[index] = _symbol_fragments(lines[index])
            spans.extend(_label_stream_spans(fragments, number, limit, offsets))
    date_breaks = [not (label[number] or follow[number]) for number in range(len(lines))]
    spans.extend(_cross_line_plain_spans(lines, offsets, date_breaks))
    return _merge_spans(spans)


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


def _normalize_receipt_text(text):
    """Allowlist after NFKC. The stored OCR text is this form.

    NFKC first, so compatibility digits fold to ASCII and Polish letters
    compose. Line terminators are already ``\\n``. One linear pass then keeps
    letters, ASCII digits, punctuation, symbols, an ASCII space and ``\\n``.
    A non-ASCII digit that NFKC left behind is mapped with ``unicodedata.digit``.
    Marks, format characters, other spaces, and every other category are removed.
    """
    text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").translate(_LINE_BREAK)
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
    if size < 1 or size > MAX_TEXT_BYTES or _CONTROL.search(text) or text.count("\n") + 1 > MAX_TEXT_LINES:
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
        match = _NAMED_UNIT.fullmatch(value)
        bare_match = None if match is not None else _BARE_UNIT.fullmatch(value)
        if bare_match is not None:
            match = bare_match
            name = match["unit"]
        elif match is not None:
            name = (match["name"] or "").strip() or pending or match["unit"]
        else:
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
