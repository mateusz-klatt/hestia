"""Read the printed net charges and VAT on a Respect Energy settlement PDF."""
from decimal import Decimal, ROUND_HALF_UP
import re

from .receipt_parser import ReceiptError, validate_confirm_lines


_ISSUER = re.compile(r"Respect Energy(?: S\.?\s*A\.?| Spółka Akcyjna)?", re.I)
_MONEY = r"(?:[0-9]{1,6}|[0-9]{1,3}(?: [0-9]{3}){1,2})(?:,[0-9]{1,2})?"
_ENERGY = re.compile(rf"a\)\s*Sprzedaż energii elektrycznej\s+({_MONEY})\s+23", re.I)
_DISTRIBUTION = re.compile(rf"b\)\s*Dystrybucja energii elektrycznej\s+({_MONEY})\s+23", re.I)
_SUMMARY = re.compile(rf"(?:^|\s)({_MONEY})\s+23\s+({_MONEY})\s+({_MONEY})\Z")
_QUANTITY = re.compile(r"\bEnergia czynna\s*[-–]\s*Całodobowa\s*[0-9. –-]+\s*kWh\s+([0-9]{1,6}(?:,[0-9]{1,10})?)(?=\s|$)", re.I)
_SLIP_TOTAL = re.compile(rf"^Kwota:\s*({_MONEY})\s*zł\s*$", re.M | re.I)
_SLIP_NUMBER = re.compile(r"^Tytuł wpłaty:\s*faktura\s+([0-9]+)\s*$", re.M | re.I)
_ADJUSTMENT = re.compile(rf"^\s*(?:saldo|nadpłat\w*|niedopłat\w*|rabat|upust|zaległoś\w*|korekt\w*)\b[^\d\n:]*:?\s*[+−-]?({_MONEY})\s*zł\b", re.M | re.I)


def _decimal(value):
    return Decimal(value.replace(" ", "").replace(",", "."))


def settlement_items(lines, issuer, number, total):
    if not _ISSUER.fullmatch(issuer) or not number:
        return ()
    text = "\n".join(lines)
    if ("twoja faktura rozliczeniowa" not in text.casefold() or not re.search(r"\bRespect Energy\b", text, re.I)
            or set(_SLIP_NUMBER.findall(text)) != {number}
            or {_decimal(value) for value in _SLIP_TOTAL.findall(text)} != {Decimal(total)}
            or any(_decimal(value) for value in _ADJUSTMENT.findall(text))):
        return ()
    # Only the two consecutive subrows of the invoice summary are allocation evidence.
    energy_rows = [(i, m) for i, line in enumerate(lines) if (m := _ENERGY.fullmatch(line))]
    if len(energy_rows) != 1 or sum(bool(_DISTRIBUTION.fullmatch(line)) for line in lines) != 1:
        return ()
    i, energy = energy_rows[0]
    if i < 1 or i + 1 >= len(lines):
        return ()
    billed = _SUMMARY.search(lines[i - 1])
    distribution = _DISTRIBUTION.fullmatch(lines[i + 1])
    if billed is None or distribution is None:
        return ()
    energy_net, distribution_net = _decimal(energy[1]), _decimal(distribution[1])
    printed_net, vat, gross = map(_decimal, billed.groups())
    if energy_net + distribution_net != printed_net or printed_net + vat != gross or gross != Decimal(total):
        return ()
    if (printed_net * Decimal("0.23")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) != vat:
        return ()
    quantities = _QUANTITY.findall(text)
    kwh = _decimal(quantities[0]) if len(quantities) == 1 else None
    items = []
    for name, price, kind, quantity, unit in (
        ("Energia elektryczna (netto)", energy_net, "item", kwh or 1, "kWh" if kwh else None),
        ("Dystrybucja energii elektrycznej (netto)", distribution_net, "item", 1, None),
        ("VAT 23%", vat, "other", 1, None),
    ):
        items.append(dict(raw_name=name, line_kind=kind, quantity=str(quantity), raw_unit=unit, unit_price_raw=None,
                          ptu_code=None, gross_line_total=str(price), discount_amount="0", net_line_total=str(price)))
    try:
        return tuple(validate_confirm_lines(items, total))
    except ReceiptError:
        return ()
