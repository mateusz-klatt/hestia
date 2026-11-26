"""Itemize the Polish App Store subscription invoice after its payment is accepted."""
from __future__ import annotations

from decimal import Decimal
import re

from .gmail_invoice import _normalized_invoice_lines
from .gmail_payment import _AMOUNT, _CARD, _parse_amount, _parse_date
from .receipt_parser import ReceiptError, redact_digit_field, redact_pan, validate_confirm_lines


_DOCUMENT = re.compile(r"^(?:dokument|numer dokumentu)[ \t]*:?[ \t]*(?:\n[ \t]*)?([0-9]{6,20})[ \t]*$",
                       re.IGNORECASE | re.MULTILINE)
_INCLUDED_VAT = re.compile(r"Zawiera podatek VAT w wysokości [0-9]{1,2}%\s*:?\s*(.*)", re.IGNORECASE)
_CHARGED_VAT = re.compile(r"Naliczono podatek VAT w wysokości [0-9]{1,2}%", re.IGNORECASE)
_RENEWAL = re.compile(r"Odnowienie:\s*([0-9]{1,2} [^\W\d_]+ [0-9]{4})", re.IGNORECASE)
_CYCLE = re.compile(
    r"\b(?:(?:tygodniow|miesięczn|kwartaln|roczn)(?:[aey]|ie)|co (?:tydzień|miesiąc|kwartał|rok)"
    r"|[1-9][0-9]? (?:miesiąc(?:e)?|miesięcy))\)?$", re.IGNORECASE,
)
_DEVICE_OWNER = r"[^\W\d_](?:[^\W\d_]|['’-]){0,63}"
_DEVICE = re.compile(rf"iPhone (?:{_DEVICE_OWNER}|\({_DEVICE_OWNER}\))", re.IGNORECASE)
_PLAN = re.compile(r"Plan (?:rodzinny|indywidualny|studencki)", re.IGNORECASE)
_PRODUCT_TEXT = re.compile(r"[^\W\d_](?:[^\W_]|[ +&'’():–-]){1,95}")
_PRECEDING_ID = re.compile(r"(?:[A-Z0-9-]*[0-9]{5,}[A-Z0-9-]*|[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+)", re.IGNORECASE)
_PRIVATE_LABEL = re.compile(
    r"\b(?:netto|brutto|VAT|adres|konto|identyfikator|telefon|klient|klienta|imię|nazwisko|email|"
    r"odbiorca|nabywca|PESEL|płatność|zamówieni[ae]|faktura|dokument|mastercard|visa)\b", re.IGNORECASE,
)
_ADJUSTMENT = re.compile(r"\b(?:rabat\w*|zniżk\w*|saldo|zwrot\w*|discount|credit|kupon\w*)\b", re.IGNORECASE)


def apple_document_number(text):
    matches = _DOCUMENT.findall(text)
    return matches[0] if len(matches) == 1 else None


def _price(line, currency):
    if len(line) > 32 or not (match := _AMOUNT.fullmatch(line)) or match["sign"] or match["inner_sign"]:
        return None
    amount, code = _parse_amount(line)
    return amount if code == currency and amount <= 1_000_000 else None


def _safe_product_text(line):
    return (_PRODUCT_TEXT.fullmatch(line) and not _PRIVATE_LABEL.search(line)
            and sum(char.isdigit() for char in line) <= 4 and redact_digit_field(line) == line
            and redact_pan(line) == line)


def _product_start(detail, index, consumed):
    renewal = _RENEWAL.fullmatch(detail[index])
    start = index - 2
    if (start < 1 or index + 2 >= len(detail) or not renewal or not _parse_date(renewal[1])
            or not _CYCLE.search(detail[index - 1])):
        return None
    if _PLAN.fullmatch(detail[start]):
        start -= 1
    names = detail[start:index]
    if (start < 1 or not all(_safe_product_text(name) for name in names)
            or (start - 1 not in consumed and not _PRECEDING_ID.fullmatch(detail[start - 1]))):
        return None
    return start


def _item_prices(detail, index, currency):
    # An optional device line can contain a person's name. Never store its text.
    price_row = index + 1
    if _DEVICE.fullmatch(detail[price_row]):
        price_row += 1
    if price_row + 1 >= len(detail):
        return None
    price = _price(detail[price_row], currency)
    included = _INCLUDED_VAT.fullmatch(detail[price_row + 1])
    if price is None or not included:
        return None
    tax_text = included[1].strip()
    end = price_row + 1
    if not tax_text:
        end += 1
        if end >= len(detail):
            return None
        tax_text = detail[end]
    elif tax_text.startswith("(") and tax_text.endswith(")"):
        tax_text = tax_text[1:-1].strip()
    tax = _price(tax_text, currency)
    if tax is None or tax > price:
        return None
    return price, tax, end


def apple_subscription_lines(text, total, currency):
    """Return only a complete set of safe, reconciled lines; unfamiliar layouts keep their summary."""
    lines = [line for line in _normalized_invoice_lines(text) if line]
    labels = [line.casefold().removesuffix(":").rstrip() for line in lines]
    if labels.count("kwota netto") != 1:
        return ()
    summary = labels.index("kwota netto")
    if summary + 5 >= len(lines):
        return ()
    net = _price(lines[summary + 1], currency)
    vat = _price(lines[summary + 3], currency)
    charged = _price(lines[summary + 5], currency)
    if (not _CHARGED_VAT.fullmatch(lines[summary + 2]) or not _CARD.search(lines[summary + 4])
            or charged != Decimal(total) or net is None or vat is None or net + vat != charged):
        return ()

    # The billing address and invoice identifiers can flank the products. Read only
    # the title and plan immediately adjacent to a recognized cadence and renewal date.
    items = []
    item_vat = Decimal(0)
    consumed = set()
    detail = lines[:summary]
    for index in range(len(detail)):
        if not labels[index].startswith("odnowienie"):
            continue
        start = _product_start(detail, index, consumed)
        if start is None:
            return ()
        prices = _item_prices(detail, index, currency)
        if prices is None:
            return ()
        price, tax, end = prices
        item_vat += tax
        amount = format(price, "f")
        items.append(dict(line_kind="item", raw_name=" · ".join(detail[start:index]), quantity="1", raw_unit=None, ptu_code=None,
                          unit_price_raw=None, gross_line_total=amount, discount_amount="0", net_line_total=amount))
        consumed.update(range(start, end + 1))

    # An unaccounted price or discount might belong to another purchase.
    if not items or item_vat != vat:
        return ()
    if any(_ADJUSTMENT.search(line) or (i not in consumed and _AMOUNT.search(line)) for i, line in enumerate(detail)):
        return ()
    try:
        return tuple(validate_confirm_lines(items, total))
    except ReceiptError:
        return ()
