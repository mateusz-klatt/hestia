"""Deterministic Polish receipt parsing, redaction and exact confirmation checks."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from itertools import groupby
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
_IBAN_SPEC_GROUP = re.compile(r"([0-9]+)!([nac])", re.ASCII)
# Official ISO 13616 BBAN specs from the SWIFT IBAN Registry (verified 2026-11-26 via
# https://www.swift.com/resource/iban-registry-pdf and
# https://www.swift.com/swift-resource/11971/download?language=en).
_IBAN_BBAN_SPECS = {
    "AD": "4!n4!n12!c",
    "AE": "3!n16!n",
    "AL": "8!n16!c",
    "AT": "5!n11!n",
    "AZ": "4!a20!c",
    "BA": "3!n3!n8!n2!n",
    "BE": "3!n7!n2!n",
    "BG": "4!a4!n2!n8!c",
    "BH": "4!a14!c",
    "BI": "5!n5!n11!n2!n",
    "BR": "8!c5!n10!n1!c1!c",
    "BY": "4!c4!n16!c",
    "CH": "5!n12!c",
    "CR": "4!n14!n",
    "CY": "3!n5!n16!c",
    "CZ": "4!n6!n10!n",
    "DE": "8!n10!n",
    "DJ": "5!n5!n11!n2!n",
    "DK": "4!n9!n1!n",
    "DO": "4!c20!n",
    "EE": "2!n2!n11!n1!n",
    "EG": "4!n4!n17!n",
    "ES": "4!n4!n1!n1!n10!n",
    "FI": "3!n11!n",
    "FK": "2!a12!n",
    "FO": "4!n9!n1!n",
    "FR": "5!n5!n11!c2!n",
    "GB": "4!a6!n8!n",
    "GE": "2!a16!n",
    "GI": "4!a15!c",
    "GL": "4!n9!n1!n",
    "GR": "3!n4!n16!c",
    "GT": "4!c20!c",
    "HN": "4!a20!n",
    "HR": "7!n10!n",
    "HU": "3!n4!n1!n15!n1!n",
    "IE": "4!a6!n8!n",
    "IL": "3!n3!n13!n",
    "IQ": "4!a3!n12!n",
    "IS": "4!n2!n6!n10!n",
    "IT": "1!a5!n5!n12!c",
    "JO": "4!a4!n18!c",
    "KW": "4!a22!c",
    "KZ": "3!n13!c",
    "LB": "4!n20!c",
    "LC": "4!a24!c",
    "LI": "5!n12!c",
    "LT": "5!n11!n",
    "LU": "3!n13!c",
    "LV": "4!a13!c",
    "LY": "3!n3!n15!n",
    "MC": "5!n5!n11!c2!n",
    "MD": "2!c18!c",
    "ME": "3!n13!n2!n",
    "MK": "3!n10!c2!n",
    "MN": "4!n12!n",
    "MR": "5!n5!n11!n2!n",
    "MT": "4!a5!n18!c",
    "MU": "4!a2!n2!n12!n3!n3!a",
    "NI": "4!a20!n",
    "NL": "4!a10!n",
    "NO": "4!n6!n1!n",
    "OM": "3!n16!c",
    "PK": "4!a16!c",
    "PL": "8!n16!n",
    "PS": "4!a21!c",
    "PT": "4!n4!n11!n2!n",
    "QA": "4!a21!c",
    "RO": "4!a16!c",
    "RS": "3!n13!n2!n",
    "RU": "9!n5!n15!c",
    "SA": "2!n18!c",
    "SC": "4!a2!n2!n16!n3!a",
    "SD": "2!n12!n",
    "SE": "3!n16!n1!n",
    "SI": "5!n8!n2!n",
    "SK": "4!n6!n10!n",
    "SM": "1!a5!n5!n12!c",
    "SO": "4!n3!n12!n",
    "ST": "4!n4!n11!n2!n",
    "SV": "4!a20!n",
    "TL": "3!n14!n2!n",
    "TN": "2!n3!n13!n2!n",
    "TR": "5!n1!n16!c",
    "UA": "6!n19!c",
    "VA": "3!n15!n",
    "VG": "4!a16!n",
    "XK": "4!n10!n2!n",
    "YE": "4!a4!n18!c",
}


def _compile_iban_specs():
    """Compile SWIFT BBAN specs into ``(total_length, character_class_mask)`` entries."""
    compiled = {}
    for country, spec in _IBAN_BBAN_SPECS.items():
        # Position 2..3 are always two check digits ('n'), followed by the BBAN mask.
        kinds = ["n", "n"]
        for count, kind in _IBAN_SPEC_GROUP.findall(spec):
            kinds.extend(kind * int(count))
        mask = "".join(kinds)
        compiled[country] = (2 + len(mask), mask)
    return compiled


_IBAN_COUNTRY_SPECS = _compile_iban_specs()
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
# OCR sometimes reads the piece count 1 as I or the multiplication sign as =.
# Only consider these alternatives when both complete, two-decimal prices remain.
_OCR_ITEM_PRICE = r"[0-9]{1,9}[.,][0-9]{2}"
_OCR_ITEM_TAIL = re.compile(
    r"(?:I\s*szt\s*[x×*=]|(?P<quantity>[1-9][0-9]{0,8})\s*szt\s*=)\s*"
    rf"(?P<unit_price>{_OCR_ITEM_PRICE})\s+(?P<gross>{_OCR_ITEM_PRICE})\s*(?:PLN\s*)?(?P<ptu>[A-G])?\Z",
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
_DISCOUNT_PREFIX = re.compile(r"^\s*(?:RABAT|OPUST)\b", re.IGNORECASE | re.ASCII)
_DISCOUNT_CURRENCY = r"(?:(?:PLN|Z[Łł])\s*)?"
# The product name may contain a size or part number. Only the price at the
# end of the line, optionally followed by the currency or PTU code, is a discount.
_DISCOUNT_AMOUNT = re.compile(
    rf"(?<![^\s:])(?P<amount>{_NUMBER})\s*{_DISCOUNT_CURRENCY}(?:[A-G])?\Z",
    re.IGNORECASE | re.ASCII,
)
_SPLIT_DISCOUNT_AMOUNT = re.compile(
    rf"(?P<amount>-{_OCR_ITEM_PRICE})\s*{_DISCOUNT_CURRENCY}(?P<ptu>[A-G])\Z", re.IGNORECASE | re.ASCII,
)
_ITEM_UNIT_SUFFIX = re.compile(r"\s*,\s*szt?\s*\Z", re.IGNORECASE)
_RECEIPT_METADATA = re.compile(r"nr\s+(?:wydr(?:uku)?|kasy|paragonu|transakcji|terminala)\b", re.IGNORECASE | re.ASCII)
_PRINT_SERIAL = re.compile(r"\s*nr\s+wydr\.?\s+[0-9]{6}/[0-9]{4}\s*\Z", re.IGNORECASE | re.ASCII)
_DECIMAL_QUANTITY = re.compile(r"[0-9]+[.,][0-9]+\Z", re.ASCII)
_UNNAMED_ITEM = "Pozycja bez nazwy"
_REFUND_WORD = re.compile(r"\bzwrot(?:u|owi|em|y|ow|om|ami|ach)?\b")
_MASTERCARD_PAYMENT = re.compile(r"^[ \t]*platnosc(?:[ \t]+|[ \t]*:[ \t]*)mastercard\b", re.MULTILINE)
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z", re.ASCII)
_CURRENCY = re.compile(r"[A-Z]{3}\Z", re.ASCII)
_TIME = re.compile(r"(?<![0-9])((?:[01][0-9]|2[0-3]):[0-5][0-9])(?![0-9])", re.ASCII)
_ARITHMETIC_AMOUNT = r"[0-9]{1,9}(?:[.,][0-9]{1,4})?"
_SHORT_AMOUNT = rf"[+-]?{_ARITHMETIC_AMOUNT}"
_BOUNDED_ARITHMETIC_AMOUNT = re.compile(_ARITHMETIC_AMOUNT, re.ASCII)
_COLUMN_TOLERANCE = Decimal("0.01")
_COLUMN_CALCULATION = re.compile(
    rf"(?P<quantity>{_SHORT_AMOUNT})\s*(?:{_UNITS})?\s*[x×*]\s*"
    rf"(?P<unit_price>{_SHORT_AMOUNT})(?:\s*=\s*|\s+)(?P<gross>{_SHORT_AMOUNT})"
    r"\s*(?:PLN\s*)?(?P<ptu>[A-G])?\Z",
    re.IGNORECASE | re.ASCII,
)
_COLUMN_SUBTOTAL = re.compile(
    rf"\s*SUMA(?:\s+PLN)?(?:\s*:\s*|\s+)(?P<amount>-?{_ARITHMETIC_AMOUNT})\s*\Z", re.IGNORECASE,
)
_COLUMN_NAME_BOUNDARY = re.compile(
    r"^(?:suma|sprzedaz|ptu|karta|gotowka|blik|platnosc|paragon|zwrot|opakowania|do zaplaty"
    r"|kasjer(?:ka)?|kasa|nr|ul|al|pl)\b"
    r"|\b(?:ul|al|pl)\."
    r"|\bnip(?:\b|(?=[0-9]))"
    r"|(?<![0-9])(?:[0-9]{2}-[0-9]{3}|[0-9]{2}[/.-][0-9]{2}[/.-][0-9]{4}"
    r"|[0-9]{4}[/.-][0-9]{2}[/.-][0-9]{2})(?![0-9])"
)
_SPLIT_DEPOSIT_SECTION = re.compile(
    r"^ *OPAKOWANIA ZWROTNE WYDANIA *\n *(?P<name>(?:DRS +)?KAUCJA\b[^\n]*)\n"
    r" *(?P<calculation>[^ \n][^\n]*)\n"
    rf" *OPAKOWANIA ZWROTNE SUMA +(?P<subtotal>{_ARITHMETIC_AMOUNT}) *$",
    re.IGNORECASE | re.MULTILINE,
)
_FISCAL_CALCULATION = re.compile(
    rf"(?P<quantity>{_ARITHMETIC_AMOUNT})\s*(?P<unit>{_UNITS})\s*[x×*]\s*"
    rf"(?P<unit_price>{_ARITHMETIC_AMOUNT})(?:\s*=\s*|\s+)(?P<gross>{_ARITHMETIC_AMOUNT})"
    r"\s*(?:PLN\s*)?(?P<ptu>[A-G])\Z",
    re.IGNORECASE | re.ASCII,
)
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
# OCR and Gmail share the buyer vocabulary. OCR also recognizes spaced headings.
BUYER_PARTY_LABELS = r"dane nabywcy|nabywca|odbiorca|kupujący|billed to|bill to|sold to|customer"
_OCR_BUYER_LABELS = BUYER_PARTY_LABELS.replace(" ", r"\s+")
_SELLER_LABELS = r"sprzedawca|merchant|seller|from|faktura\s+wystawiona\s+przez"
_NIP_PARTY = re.compile(
    # Adjacent empty column headings introduce the seller's column first.
    r"(?P<seller>^sprzedawca\s*:?\s+nabywca\s*:?$|^sprzedawca\b"
    rf"|\b(?:{_SELLER_LABELS})\s*:|^(?:{_SELLER_LABELS})(?=$|\s+NIP\b))"
    # A bare buyer heading ends at the line break or leads straight into a NIP.
    r"|(?P<buyer>\bNIP\s+(?:nabywcy|odbiorcy)\b"
    rf"|\b(?:{_OCR_BUYER_LABELS})\s*:|^(?:{_OCR_BUYER_LABELS})(?=\.?$|\s+NIP\b))",
    re.IGNORECASE,
)
_NIP_SELLER_LABEL = re.compile(r"\bNIP\s+sprzedawcy\b", re.IGNORECASE)
_NIP_SECTION_BREAK = re.compile(
    r"^(?:[-–—=_*]{3,}$|(?:paragon|faktura|pozycje|podsumowanie)\b|suma(?=\s*(?:$|:)|\s+(?:pln|brutto)\b))",
    re.IGNORECASE,
)
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
_CHAIN_ALIAS_GROUPS = {
    "biedronka": (frozenset({"biedronka"}), frozenset({"jeronimo", "martins"})),
    "carrefour": (frozenset({"carrefour"}),),
    "lidl": (frozenset({"lidl"}),),
}
_CHAIN_WORDS = {chain: frozenset().union(*groups) for chain, groups in _CHAIN_ALIAS_GROUPS.items()}
GATEWAY_TOKENS = frozenset({
    "p24", "przelewy24", "paypro", "payu", "autopay", "tpay", "paynow", "dotpay",
    "blik", "bluemedia", "stripe", "paypal", "adyen", "klarna",
    "eservice", "elavon", "fiserv", "polcard", "ecard", "cashbill", "hotpay", "imoje",
})
GATEWAY_PHRASE_TOKENS = frozenset({
    ("blue", "media"),
    ("first", "data"),
    ("krajowy", "integrator", "platnosci"),
    ("pay", "pro"),
    ("revolut", "pay"),
})
_LEGAL_FORMS_RE = re.compile(
    r"(?<![^\W_])(?:"
    r"spolka\s+z\s+ograniczona\s+odpowiedzialnoscia"
    r"|z\s+ograniczona\s+odpowiedzialnoscia"
    r"|spolka\s+akcyjna"
    r"|spolka\s+komandytowa"
    r"|spolka\s+jawna"
    r"|sp\.?[\s.]*z[\s.]*o\.?[\s.]*o\.?"
    r"|sp\.?\s*k\.?"
    r"|s\.?\s*k\.?\s*a\.?"
    r"|s\.?\s*r\.?\s*o\.?"
    r"|s\.?\s*a\.?\s*r\.?\s*l\.?"
    r"|s\.?\s*r\.?\s*l\.?"
    r"|b\.?\s*v\.?"
    r"|s\.?\s*a\.?"
    r"|(?:gmbh|limited|ltd|inc|llc|sarl|srl|ag|plc|oy|ab|as|kft|bv)\b\.?"
    r")(?![^\W_])",
    re.UNICODE,
)
_NON_MERCHANT_TOKENS = frozenset({
    "sp", "z", "o", "oo", "sa", "spolka", "zoo",
    "com", "pl", "www", "net", "eu",
    "fiskalny", "paragon",
}) | GATEWAY_TOKENS
_POLISH_CITY_TOKENS = frozenset({
    # 50+ largest Polish cities (with and without diacritics; multi-word components included).
    "warszawa",
    "krakow", "kraków",
    "wroclaw", "wrocław",
    "lodz", "łódź",
    "poznan", "poznań",
    "gdansk", "gdańsk",
    "szczecin",
    "bydgoszcz",
    "lublin",
    "bialystok", "białystok",
    "katowice",
    "gdynia",
    "czestochowa", "częstochowa",
    "radom",
    "rzeszow", "rzeszów",
    "torun", "toruń",
    "sosnowiec",
    "kielce",
    "gliwice",
    "olsztyn",
    "zabrze",
    "bielsko", "biala", "biała",
    "bytom",
    "zielona", "gora", "góra",
    "rybnik",
    "ruda", "slaska", "śląska",
    "opole",
    "tychy",
    "gorzow", "gorzów", "wielkopolski",
    "dabrowa", "dąbrowa", "gornicza", "górnicza",
    "elblag", "elbląg",
    "plock", "płock",
    "koszalin",
    "tarnow", "tarnów",
    "wloclawek", "włocławek",
    "walbrzych", "wałbrzych",
    "chorzow", "chorzów",
    "kalisz",
    "legnica",
    "grudziadz", "grudziądz",
    "jaworzno",
    "slupsk", "słupsk",
    "jastrzebie", "jastrzębie", "zdroj", "zdrój",
    "nowy", "sacz", "sącz",
    "jelenia",
    "siedlce",
    "myslowice", "mysłowice",
    "konin",
    "pila", "piła",
    "piotrkow", "piotrków", "trybunalski",
    "inowroclaw", "inowrocław",
    "lubin",
    "ostrow", "ostrów",
    "suwalki", "suwałki",
    "stargard",
    "gniezno",
    "ostrowiec", "swietokrzyski", "świętokrzyski",
    "glogow", "głogów",
    "pabianice",
})
_LOCATION_STOP_TOKENS = frozenset({
    "ul", "ulica", "al", "aleja", "plac", "centrum", "centra", "center", "centers", "centre", "centres", "galeria",
})
_SHOP_CATEGORY_TOKENS = frozenset({
    # Polish and English shop-category nouns (with/without diacritics and common inflections).
    "apteka", "apteki", "aptece", "apteke", "aptekę", "apteko", "apteką", "aptek", "aptekom", "aptekami", "aptekach",
    "apteczny", "apteczna", "apteczne", "aptecznej",
    "sklep", "sklepu", "sklepowi", "sklepem", "sklepie", "sklepy", "sklepow", "sklepów", "sklepom", "sklepami",
    "sklepach", "sklepik", "sklepiku", "sklepowy", "sklepowa",
    "piekarnia", "piekarni", "piekarnie", "piekarnię", "piekarnią", "piekarnio", "piekarn", "piekarń", "piekarniom",
    "piekarniami", "piekarniach", "piekarniczy",
    "cukiernia", "cukierni", "cukiernie", "cukiernię", "cukiernią", "cukiernio", "cukiern", "cukierń", "cukierniom",
    "cukierniami", "cukierniach", "cukierniczy",
    "restauracja", "restauracji", "restauracje", "restaurację", "restauracją", "restauracjo", "restauracyj",
    "restauracjom", "restauracjami", "restauracjach", "restauracyjny",
    "kawiarnia", "kawiarni", "kawiarnie", "kawiarnię", "kawiarnią", "kawiarnio", "kawiarn", "kawiarnią", "kawiarnom",
    "kawiarniom", "kawiarniami", "kawiarniach", "kawiarenka", "kawiarenki",
    "bar", "baru", "barowi", "barem", "barze", "bary", "barow", "barów", "barom", "barami", "barach", "bars",
    "pizzeria", "pizzerii", "pizzerie", "pizzerię", "pizzerią", "pizzerio", "pizzeriom", "pizzeriami", "pizzeriach",
    "pizzerias",
    "bistro", "bistra", "bistru", "bistrem", "bistrze", "bistrom", "bistrami", "bistrach", "bistros",
    "stacja", "stacji", "stacje", "stację", "stacją", "stacjo", "stacyj", "stacjom", "stacjami", "stacjach",
    "paliwa", "paliwo", "paliw", "paliwu", "paliwem", "paliwie", "paliwom", "paliwami", "paliwach", "paliwowy",
    "paliwowa", "paliwowe", "paliwowej",
    "hotel", "hotelu", "hotelowi", "hotelem", "hotele", "hoteli", "hotelow", "hotelów", "hotelom", "hotelami",
    "hotelach", "hotels", "hotelik", "hotelowy", "hotelowa", "hotelowe",
    "hostel", "hostelu", "hostelowi", "hostelem", "hostele", "hosteli", "hostelow", "hostelów", "hostelom",
    "hostelami", "hostelach", "hostels", "hostelowy",
    "kino", "kina", "kinu", "kinem", "kinie", "kin", "kinom", "kinami", "kinach", "kinowy", "kinowa", "kinowe",
    "salon", "salonu", "salonowi", "salonem", "salonie", "salony", "salonow", "salonów", "salonom", "salonami",
    "salonach", "salonik", "saloniku", "salons",
    "studio", "studia", "studiu", "studiem", "studiow", "studiów", "studiom", "studiami", "studiach", "studios",
    "drogeria", "drogerii", "drogerie", "drogerię", "drogerią", "drogerio", "drogeriom", "drogeriami",
    "drogeriach", "drogeryjny",
    "ksiegarnia", "księgarnia", "ksiegarni", "księgarni", "ksiegarnie", "księgarnię", "ksiegarnią", "księgarnią",
    "ksiegarnio", "księgarnio", "ksiegarn", "księgarn", "księgarń", "ksiegarniom", "księgarniom", "ksiegarniami",
    "księgarniami", "ksiegarniach", "księgarniach",
    "kwiaciarnia", "kwiaciarni", "kwiaciarnie", "kwiaciarnię", "kwiaciarnią", "kwiaciarnio", "kwiaciarn",
    "kwiaciarń", "kwiaciarniom", "kwiaciarniami", "kwiaciarniach",
    "optyk", "optyka", "optykowi", "optykiem", "optyku", "optycy", "optykow", "optyków", "optykom", "optykami",
    "optykach", "optyczny", "optyczna", "optyczne",
    "fryzjer", "fryzjera", "fryzjerowi", "fryzjerem", "fryzjerze", "fryzjerzy", "fryzjerow", "fryzjerów", "fryzjerom",
    "fryzjerami", "fryzjerach", "fryzjerski", "fryzjerska", "fryzjerskie", "fryzjerskiej", "fryzjerka", "fryzjerki",
    "fryzjerstwo",
    "kebab", "kebabu", "kebaba", "kebabowi", "kebabem", "kebabie", "kebaby", "kebabow", "kebabów", "kebabom",
    "kebabami", "kebabach", "kebabs",
    "sushi",
    "delikatesy", "delikatesow", "delikatesów", "delikatesom", "delikatesami", "delikatesach", "delikatesowy",
    "delikatesowa", "delikatesowe",
    "spozywczy", "spożywczy", "spozywcza", "spożywcza", "spozywcze", "spożywcze", "spozywczego", "spożywczego",
    "spozywczej", "spożywczej", "spozywczym", "spożywczym", "spozywczych", "spożywczych", "spozywczemu",
    "spożywczemu", "spozywcza", "spożywczą", "spozywcy", "spożywcy", "spozywczo", "spożywczo", "spozywczak",
    "spożywczak",
    "ogolnospozywczy", "ogólnospożywczy", "ogolnospozywcza", "ogólnospożywcza", "ogolnospozywcze",
    "ogólnospożywcze", "ogolnospozywczego", "ogólnospożywczego", "ogolnospozywczej", "ogólnospożywczej",
    "ogolnospozywczym", "ogólnospożywczym", "ogolnospozywczych", "ogólnospożywczych", "ogolnospozywczemu",
    "ogólnospożywczemu", "ogolnospozywczą", "ogólnospożywczą",
    "wielobranzowy", "wielobranżowy", "wielobranzowa", "wielobranżowa", "wielobranzowe", "wielobranżowe",
    "wielobranzowej", "wielobranżowej",
    "market", "marketu", "marketowi", "marketem", "markecie", "markety", "marketow", "marketów", "marketom",
    "marketami", "marketach", "markets",
    "supermarket", "supermarketu", "supermarketowi", "supermarketem", "supermarkecie", "supermarkety",
    "supermarketow", "supermarketów", "supermarketom", "supermarketami", "supermarketach", "supermarkets",
    "hipermarket", "hipermarketu", "hipermarketowi", "hipermarketem", "hipermarkecie", "hipermarkety",
    "hipermarketow", "hipermarketów", "hipermarketom", "hipermarketami", "hipermarketach", "hypermarket",
    "hypermarkets",
    "parking", "parkingu", "parkingowi", "parkingiem", "parkingi", "parkingow", "parkingów", "parkingom",
    "parkingami", "parkingach", "parkingowy", "parkingowa", "parkingowe", "parkings",
    "myjnia", "myjni", "myjnie", "myjnię", "myjnią", "myjnio", "myjniom", "myjniami", "myjniach",
    "warsztat", "warsztatu", "warsztatowi", "warsztatem", "warsztacie", "warsztaty", "warsztatow", "warsztatów",
    "warsztatom", "warsztatami", "warsztatach", "warsztatowy",
    "serwis", "serwisu", "serwisowi", "serwisem", "serwisie", "serwisy", "serwisow", "serwisów", "serwisom",
    "serwisami", "serwisach", "serwisowy", "serwisowa", "serwisowe",
    "przychodnia", "przychodni", "przychodnie", "przychodnię", "przychodnią", "przychodnio", "przychodniom",
    "przychodniami", "przychodniach",
    "gabinet", "gabinetu", "gabinetowi", "gabinetem", "gabinecie", "gabinety", "gabinetow", "gabinetów",
    "gabinetom", "gabinetami", "gabinetach", "gabinetowy",
    "klinika", "kliniki", "klinice", "klinike", "klinikę", "kliniko", "kliniką", "klinik", "klinikom", "klinikami",
    "klinikach", "kliniczny", "kliniczna",
    "pharmacy", "pharmacies", "bakery", "bakeries", "cafe", "café", "cafes", "cafés", "coffee", "coffees",
    "restaurant", "restaurants", "station", "stations", "fuel", "fuels", "cinema", "cinemas", "clinic", "clinics",
})
_ECOMMERCE_GENERIC_TOKENS = frozenset({
    # Generic Polish and English e-commerce / billing words (with/without diacritics and inflections).
    "internet", "internetowy", "internetowa", "internetowe", "internetowego", "internetowej", "internetowemu",
    "internetowym", "internetowych", "internetowymi", "internetową", "internetowo",
    "zakup", "zakupu", "zakupowi", "zakupem", "zakupie", "zakupy", "zakupow", "zakupów", "zakupom", "zakupami",
    "zakupach", "zakupowy", "zakupowa", "zakupowe",
    "zamowienie", "zamówienie", "zamowienia", "zamówienia", "zamowieniu", "zamówieniu", "zamowieniem", "zamówieniem",
    "zamowien", "zamówień", "zamowieniom", "zamówieniom", "zamowieniami", "zamówieniami", "zamowieniach",
    "zamówieniach", "order", "orders",
    "platnosc", "płatność", "platnosci", "płatności", "platnoscia", "płatnością", "platnosciom", "płatnościom",
    "platnosciami", "płatnościami", "platnosciach", "płatnościach", "platniczy", "płatniczy", "platnicza",
    "płatnicza", "platnicze", "płatnicze", "payments",
    "usluga", "usługa", "uslugi", "usługi", "usludze", "usłudze", "usluge", "usługę", "uslugo", "usługo", "uslug",
    "usług", "uslugom", "usługom", "uslugami", "usługami", "uslugach", "usługach", "uslugowy", "usługowy",
    "uslugowa", "usługowa", "uslugowe", "usługowe", "uslugowego", "usługowego", "uslugowej", "usługowej",
    "uslugowemu", "usługowemu", "uslugowym", "usługowym", "uslugowych", "usługowych", "uslugowymi", "usługowymi",
    "uslugową", "usługową", "uslugowo", "usługowo",
    "abonament", "abonamentu", "abonamentowi", "abonamentem", "abonamencie", "abonamenty", "abonamentow",
    "abonamentów", "abonamentom", "abonamentami", "abonamentach", "abonamentowy", "abonamentowa", "abonamentowe",
    "opłata", "oplaty", "opłaty", "oplacie", "opłacie", "oplate", "opłatę", "opłatą", "oplato", "opłato", "oplat",
    "opłat", "oplatom", "opłatom", "oplatami", "opłatami", "oplatach", "opłatach",
    "faktura", "faktury", "fakturze", "fakture", "fakturę", "fakturą", "fakturo", "faktur", "fakturom",
    "fakturami", "fakturach", "fakturowy", "invoice", "invoices",
    "rachunek", "rachunku", "rachunkowi", "rachunkiem", "rachunki", "rachunkow", "rachunków", "rachunkom",
    "rachunkami", "rachunkach", "rachunkowy",
    "transakcja", "transakcji", "transakcje", "transakcję", "transakcją", "transakcjo", "transakcyj", "transakcjom",
    "transakcjami", "transakcjach", "transakcyjny",
    "karta", "karty", "karcie", "karte", "kartę", "kartą", "karto", "kart", "kartom", "kartami", "kartach",
    "kartowy", "kartowa", "kartowe",
    "przelew", "przelewu", "przelewowi", "przelewem", "przelewie", "przelewy", "przelewow", "przelewów",
    "przelewom", "przelewami", "przelewach", "przelewowy",
    "transfer", "transferu", "transferowi", "transferem", "transferze", "transfery", "transferow", "transferów",
    "transferom", "transferami", "transferach", "transfers",
    "polecenie", "polecenia", "poleceniu", "poleceniem", "polecen", "poleceń", "poleceniom", "poleceniami",
    "poleceniach",
    "zaplata", "zapłata", "zaplaty", "zapłaty", "zaplacie", "zapłacie", "zaplate", "zapłatę", "zapłatą",
    "zaplato", "zapłato", "zaplat", "zapłat",
})
_CORPORATE_GENERIC_TOKENS = frozenset({
    # Generic business / corporate nouns and qualifiers (PL/EN, with/without diacritics & inflections).
    "firma", "firmy", "firmie", "firme", "firmę", "firmą", "firmo", "firm", "firmom", "firmami", "firmach",
    "handlowa", "handlowy", "handlowe", "handlowego", "handlowej", "handlowemu", "handlowym", "handlowych",
    "handlowymi", "handlową", "handlowo", "handel", "handlu",
    "company", "companies",
    "przedsiebiorstwo", "przedsiębiorstwo", "przedsiebiorstwa", "przedsiębiorstwa", "przedsiebiorstwu",
    "przedsiębiorstwu", "przedsiebiorstwem", "przedsiębiorstwem", "przedsiebiorstwie", "przedsiębiorstwie",
    "przedsiebiorstw", "przedsiębiorstw", "przedsiebiorstwom", "przedsiębiorstwom", "przedsiebiorstwami",
    "przedsiębiorstwami", "przedsiebiorstwach", "przedsiębiorstwach",
    "produkcyjne", "produkcyjny", "produkcyjna", "produkcyjnego", "produkcyjnej", "produkcyjnemu", "produkcyjnym",
    "produkcyjnych", "produkcyjnymi", "produkcyjną", "produkcyjno", "produkcja", "produkcji",
    "hurtownia", "hurtowni", "hurtownie", "hurtownię", "hurtownią", "hurtownio", "hurtowniom", "hurtowniami",
    "hurtowniach", "hurtowy", "hurtowa", "hurtowe", "hurtowego", "hurtowej", "hurtowym", "hurtowych",
    "grupa", "grupy", "grupie", "grupe", "grupę", "grupą", "grupo", "grup", "grupom", "grupami", "grupach",
    "holding", "holdingu", "holdingowi", "holdingiem", "holdingi", "holdingow", "holdingów", "holdingom",
    "holdingami", "holdingach", "holdings", "holdingowy", "holdingowa", "holdingowe",
    "international", "trading", "solutions", "solution", "systems", "system", "systemy", "systemow", "systemów",
    "consulting", "konsulting", "konsultingowy", "konsultingowa", "konsultingowe",
    "polski", "polskie", "polskiego", "polskiej", "polskiemu", "polskim", "polskich", "polskimi", "polską", "polish",
})
_GENERIC_MERCHANT_TOKENS = frozenset({
    "grocery", "groceries", "grocer", "energy", "energia", "polska", "poland", "pl", "services", "service",
    "group", "store", "shop", "sklep", "market", "online", "pay", "payment", "oplata",
}) | _POLISH_CITY_TOKENS | _LOCATION_STOP_TOKENS | _SHOP_CATEGORY_TOKENS | _ECOMMERCE_GENERIC_TOKENS | _CORPORATE_GENERIC_TOKENS
_STOP_TOKENS = _NON_MERCHANT_TOKENS | _GENERIC_MERCHANT_TOKENS


class ReceiptError(ValueError):
    CODES = frozenset({
        "budget_receipt_invalid_request", "budget_receipt_not_found", "budget_receipt_conflict",
        "budget_receipt_stale_source", "budget_receipt_tx_already_linked", "budget_receipt_total_mismatch",
        "budget_receipt_quota_exceeded", "budget_receipt_unavailable", "budget_receipt_in_group",
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


def _iban_char_matches(char, kind):
    """Return True when ``char`` matches SWIFT BBAN character class ``n``, ``a`` or ``c``."""
    if "0" <= char <= "9":
        return kind != "a"
    if "A" <= char <= "Z":
        return kind != "n"
    return False


def _iban_shape(token):
    """Polish PL plus 26 digits, or a country-structured IBAN with a valid mod-97 checksum."""
    if not token.isascii():
        return False
    token = token.upper()
    spec = _IBAN_COUNTRY_SPECS.get(token[:2])
    if spec is None:
        return False
    expected_len, mask = spec
    if len(token) != expected_len:
        return False
    if not all(_iban_char_matches(char, kind) for char, kind in zip(token[2:], mask)):
        return False
    if token[:2] == "PL":
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


def _mark_ibans(text):
    """Return IBAN line redactions and text with the IBANs masked for digit scanning.

    Separators may sit between IBAN characters. At the country's fixed SWIFT
    length, a checksum-valid candidate can end inside an ASCII token. A
    space-delimited or glued suffix stays on the final line for further checks.
    """
    redactions = {}
    kept_prefixes = {}
    retry_starts = {}
    digit_scan_parts = []
    masked_through = 0
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
        failed_start = max(retry_starts.pop(index, index), masked_through)
        at_boundary = failed_start < index or index == masked_through or not text[index - 1].isalnum()
        if not (char.isascii() and char.isalpha() and at_boundary):
            index += 1
            continue
        compact = [char.upper()]
        positions = [index]
        cursor = index + 1
        next_index = cursor
        spec = None
        matched_end = None
        while cursor < length:
            current = text[cursor]
            if current.isalnum():
                if not current.isascii():
                    break
                upper = current.upper()
                pos = len(compact)
                if pos == 1:
                    spec = _IBAN_COUNTRY_SPECS.get(compact[0] + upper)
                    if spec is None:
                        break
                elif not _iban_char_matches(upper, spec[1][pos - 2]):
                    break
                compact.append(upper)
                positions.append(cursor)
                cursor += 1
                if len(compact) == spec[0]:
                    token = "".join(compact)
                    next_char = text[cursor:cursor + 1]
                    glued = next_char.isascii() and next_char.isalnum()
                    # Only standalone PL candidates can skip the checksum.
                    if (token[:2] == "PL" and not glued) or _iban_checksum(token):
                        matched_end = cursor
                    else:
                        # Retry at the end of this candidate. Keep looking for
                        # earlier starts inside it if it crossed a separator.
                        if cursor - index == len(compact) and not retry_starts:
                            next_index = cursor
                        if glued:
                            retry_starts.setdefault(cursor, failed_start)
                    break
                continue
            if current in _JOIN_SEPARATORS:
                cursor += 1
                continue
            break
        if matched_end is None:
            index = next_index
            continue
        first_line = line_of[index]
        if failed_start < index:
            line_start = text.rfind("\n", failed_start, index) + 1
            prefix = text[max(failed_start, line_start):index]
            if first_line in redactions:
                prefix = kept_prefixes.get(first_line, "") + _REMOVED + prefix
            kept_prefixes[first_line] = prefix
        for pos in positions:
            line = line_of[pos]
            redactions[line] = kept_prefixes.get(line, "") + _REMOVED
        last_line = line_of[positions[-1]]
        if glued or next_char == " ":
            line_end = text.find("\n", matched_end)
            redactions[last_line] += text[matched_end:line_end if line_end >= 0 else length].rstrip()
        # Keep the original line indices and separate any digit runs around the IBAN.
        digit_scan_parts.extend((text[masked_through:index], "\n" * (last_line - line_of[index]), _REMOVED))
        masked_through = matched_end
        index = matched_end
    digit_scan_parts.append(text[masked_through:])
    return redactions, "".join(digit_scan_parts)


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
    lines = text.split("\n")
    ibans, digit_scan_text = _mark_ibans(text)
    masked = [_mask_structured(line) for line in digit_scan_text.split("\n")]
    for index in range(1, len(lines)):
        if _PRINT_SERIAL.fullmatch(lines[index - 1]):
            calculation = _verified_bare_calculation(lines[index].strip())
            if calculation is not None and _DECIMAL_QUANTITY.fullmatch(calculation["quantity"]):
                # The serial remains redacted. Start a separate digit run on the corroborated sale row.
                bad.add(index - 1)
                masked[index] = "x" + masked[index]
    _mark_digit_runs("\n".join(masked), bad)
    return ibans | dict.fromkeys(bad, _REMOVED)


def _redact_normalized(text):
    """Apply the redactions while keeping all other lines intact."""
    redactions = _secret_lines(text)
    if not redactions:
        return text
    return "\n".join(redactions.get(number, line) for number, line in enumerate(text.split("\n")))


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


def _seller_nip(lines, normalized_lines):
    """Find the first valid NIP outside a buyer block on the page."""
    in_buyer_block = False
    for line, original in zip(lines, normalized_lines, strict=True):
        line = _NIP_SELLER_LABEL.sub("Sprzedawca: NIP", line.strip())
        original = _NIP_SELLER_LABEL.sub("Sprzedawca: NIP", original.strip())
        if not original or _NIP_SECTION_BREAK.match(original):
            in_buyer_block = False
        parties = list(_NIP_PARTY.finditer(line))
        if line != original:
            original_roles = [party.lastgroup for party in _NIP_PARTY.finditer(original)]
            if original_roles != [party.lastgroup for party in parties]:
                # Redaction can erase a party heading. Keep its effect on following
                # lines and skip ambiguous NIPs on the partly redacted line.
                for role in original_roles:
                    in_buyer_block = role == "buyer"
                continue
        start = 0
        for party in parties:
            if not in_buyer_block and valid_nip(candidate := _nip_candidate(line[start:party.start()])):
                return candidate
            in_buyer_block = party.lastgroup == "buyer"
            start = party.end()
        if not in_buyer_block and valid_nip(candidate := _nip_candidate(line[start:])):
            return candidate
    return None


def valid_nip(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{10}", value, re.ASCII):
        return False
    checksum = sum(int(digit) * weight for digit, weight in zip(value[:9], (6, 5, 7, 2, 3, 4, 5, 6, 7))) % 11
    return checksum != 10 and checksum == int(value[9])


def chain_for(shop_name, nip):
    if nip in _CHAIN_NIPS:
        return _CHAIN_NIPS[nip]
    words = frozenset(merchant_word_tokens(shop_name))
    return next((chain for chain, groups in _CHAIN_ALIAS_GROUPS.items()
                 if any(group.issubset(words) for group in groups)), None)


def strip_legal_forms(value):
    """Strip Polish and foreign legal-form phrases (diacritic-insensitive) before tokenization."""
    if not isinstance(value, str):
        return ""
    return _LEGAL_FORMS_RE.sub(" ", fold_text(value))


def merchant_word_tokens(value):
    if not isinstance(value, str):
        return ()
    return tuple(re.findall(r"[^\W_]+", strip_legal_forms(value), re.UNICODE))


def contains_gateway_marker(text):
    """True when text contains any payment gateway token, legal institution name, or phrase."""
    words = merchant_word_tokens(text)
    if not words:
        return False
    if GATEWAY_TOKENS.intersection(words):
        return True
    limit = len(words)
    for size in (2, 3):
        for start in range(limit - size + 1):
            if words[start:start + size] in GATEWAY_PHRASE_TOKENS:
                return True
    return False


def merchant_match_groups(shop_name, nip):
    groups, _generic = merchant_score_groups(shop_name, nip)
    return tuple(alias for group in groups for alias in group)


def _distinctive_shop_words(shop_name):
    """Return distinctive shop tokens (at least 4 chars, not stop/generic/location, and >= 3 letters).

    Never falls back to generic or stop words: if every word is generic, returns an empty set.
    """
    return {
        token for token in merchant_word_tokens(shop_name)
        if len(token) >= 4 and token not in _STOP_TOKENS and sum(1 for ch in token if ch.isalpha()) >= 3
    }


def merchant_score_groups(shop_name, nip):
    words = [token for token in merchant_word_tokens(shop_name) if len(token) >= 4]
    distinctive = sorted(_distinctive_shop_words(shop_name))
    generic = tuple(sorted({token for token in words if token in _GENERIC_MERCHANT_TOKENS}))
    chain = chain_for(shop_name, nip)
    if chain is not None:
        extra = [token for token in distinctive if token not in _CHAIN_WORDS[chain]]
        return (_CHAIN_ALIAS_GROUPS[chain],) + tuple((frozenset((token,)),) for token in extra), generic
    return tuple((frozenset((token,)),) for token in distinctive), generic


def merchant_tokens(shop_name, nip):
    distinctive = _distinctive_shop_words(shop_name)
    chain = chain_for(shop_name, nip)
    if chain is not None:
        distinctive.update(_CHAIN_WORDS[chain])
    return frozenset(distinctive)


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


def _shop_header(lines):
    before_fiscal_header = True
    for index, raw in enumerate(lines[:12]):
        value = raw.strip()
        key = fold_text(value)
        if "fiskalny" in key:
            before_fiscal_header = False
        if (not value or len(value) > MAX_SHOP_NAME_LEN or _CONTROL.search(value) or _RECEIPT_METADATA.match(value)
                or any(marker in key for marker in ("paragon", "nip", "fiskalny", "nr karty", "aid"))):
            continue
        if _DATE_ISO.search(value) or _DATE_PL.search(value) or not re.search(r"[A-Za-zĄ-ż]", value):
            continue
        return redact_digit_field(value), index if before_fiscal_header else None
    return None, None


def _line_kind(name, gross):
    key = fold_text(name)
    if "kaucja" in key:
        return "deposit"
    if compare_money(gross, "0") < 0 and any(token in key for token in ("kupon", "bon ", "rabat cal", "rabat na ")):
        return "basket_discount"
    return "item"


def _make_line(name, match, line_no):
    original = " ".join(name.split())
    gross = _decimal(match["gross"])
    kind = _line_kind(original, gross)
    raw_name = redact_digit_field(_ITEM_UNIT_SUFFIX.sub("", original))
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
    match = _tail_match(value, (_ITEM_QTY_TAIL, _ITEM_GROSS_TAIL))
    if match is not None and match["quantity"] is not None:
        return match
    ocr = _tail_match(value, (_OCR_ITEM_TAIL,))
    if ocr is None:
        return match
    quantity = ocr["quantity"] or "1"
    unit_cents = int(ocr["unit_price"].replace(",", "").replace(".", ""))
    gross_cents = int(ocr["gross"].replace(",", "").replace(".", ""))
    if int(quantity) * unit_cents != gross_cents:
        return match
    return _FieldMatch({
        "name": ocr["name"], "quantity": quantity, "unit": "szt",
        "unit_price": ocr["unit_price"], "gross": ocr["gross"], "ptu": ocr["ptu"],
    })


def _reconciled_gross(match, *, tolerance=None):
    if match is None:
        return None
    amounts = (match["quantity"], match["unit_price"], match["gross"])
    if not all(value and _BOUNDED_ARITHMETIC_AMOUNT.fullmatch(value) for value in amounts):
        return None
    quantity, unit_price, gross = (Decimal(value.replace(",", ".")) for value in amounts)
    if min(quantity, unit_price, gross) <= 0:
        return None
    product = quantity * unit_price
    if tolerance is not None:
        return gross if abs(product - gross) <= tolerance else None
    rounded = product.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return gross if rounded == gross else None


def _verified_bare_calculation(value):
    match = _FISCAL_CALCULATION.fullmatch(value)
    return match if _reconciled_gross(match) is not None else None


def _can_split_discount_item(item, gross, following):
    discount = _SPLIT_DISCOUNT_AMOUNT.fullmatch(following)
    if discount is None or item["ptu"] is None or item["ptu"].upper() != discount["ptu"].upper():
        return False
    name = item["name"]
    if _DISCOUNT_PREFIX.match(name) or _line_kind(name, format(gross, "f")) != "item":
        return False
    amount = Decimal(discount["amount"].replace(",", ".")).copy_abs()
    return 0 < amount <= gross


def _item_rows(lines):
    rows = iter(" ".join(raw.split()) for raw in lines)
    value = next(rows, None)
    while value is not None:
        following = next(rows, None)
        prefix = _DISCOUNT_PREFIX.match(value)
        if prefix is not None and not any(
            marker in fold_text(value) for marker in ("kupon", "bon ", "rabat cal", "rabat na ")
        ):
            name_and_price = value[prefix.end():].strip()
            if _FISCAL_CALCULATION.fullmatch(name_and_price) or _OCR_ITEM_TAIL.fullmatch(name_and_price):
                name_and_price = f"{_UNNAMED_ITEM} {name_and_price}"
            item = _named_unit_match(name_and_price) or _item_match(name_and_price)
            has_arithmetic_tail = (item is not None and item["quantity"] is not None
                                   and item["unit_price"] is not None and not item["gross"].startswith("-"))
            if has_arithmetic_tail or _tail_match(name_and_price, (_OCR_ITEM_TAIL,)) is not None:
                gross = _reconciled_gross(item)
                if gross is not None and following is not None and _can_split_discount_item(item, gross, following):
                    yield name_and_price
                    yield "OPUST " + following
                    following = next(rows, None)
                else:
                    # A product's gross amount cannot stand in for a discount.
                    yield ""
                value = following
                continue
        yield value
        value = following


def _parse_lines(lines, shop_header_index):
    result, pending, pending_index = [], None, -2
    for index, value in enumerate(_item_rows(lines)):
        if not value or value == _REMOVED or _RECEIPT_METADATA.match(value):
            continue
        discount_prefix = _DISCOUNT_PREFIX.match(value)
        discount = _DISCOUNT_AMOUNT.search(value[discount_prefix.end():]) if discount_prefix else None
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
            match = _verified_bare_calculation(value)
            if match is not None:
                adjacent_product = pending_index == index - 1 and pending_index != shop_header_index
                name = pending if adjacent_product else _UNNAMED_ITEM
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
                pending, pending_index = value, index
            continue
        if match["ptu"] is None and match["quantity"] is None:
            key = fold_text(name)
            if "kaucja" not in key and not any(token in key for token in ("kupon", "bon ", "rabat cal", "rabat na ")):
                pending, pending_index = value, index
                continue
        if len(result) >= MAX_RECEIPT_LINES:
            raise ReceiptError()
        result.append(_make_line(name, match, len(result) + 1))
        pending = None
    return result


def _column_amount(value):
    return bool(_COLUMN_CALCULATION.fullmatch(value) or _SPLIT_DISCOUNT_AMOUNT.fullmatch(value))


def _orphan_name(value):
    if (len(value) > MAX_LINE_NAME_LEN or value.startswith(_REMOVED)
            or _COLUMN_NAME_BOUNDARY.search(fold_text(value)) or not re.search(r"[^\W\d_]", value)
            or _TIME.search(value)
            or _ITEM_GROSS_TAIL.fullmatch(value) or _BARE_UNIT.fullmatch(value)):
        return False
    if _DISCOUNT_PREFIX.match(value) and _DISCOUNT_AMOUNT.search(value):
        return False
    match = _named_unit_match(value) or _item_match(value)
    # A plain trailing integer can be part of a label, for example a pump number.
    return match is None or (match["quantity"] is None and match["ptu"] is None and match["gross"].isdigit()
                             and value.endswith(" " + match["gross"]) and "kaucja" not in fold_text(value))


def _pair_column_rows(names, amounts):
    if len(names) != len(amounts):
        return None
    joined = []
    previous_item = previous_gross = None
    for name, amount in zip(names, amounts, strict=True):
        if _DISCOUNT_PREFIX.match(name):
            if previous_item is None or not _can_split_discount_item(previous_item, previous_gross, amount):
                return None
            previous_item = None
            # Replay the verified amount as a legacy item discount.
            name = "OPUST"
        else:
            calculation = _COLUMN_CALCULATION.fullmatch(amount)
            previous_gross = _reconciled_gross(calculation, tolerance=_COLUMN_TOLERANCE)
            if previous_gross is None:
                return None
            previous_item = {"name": name, "ptu": calculation["ptu"]}
        joined.append(f"{name} {amount}")
    return joined


def _restore_item_columns(lines, shop_header_index):
    rows = [" ".join(raw.split()) for raw in lines]
    restored = None
    end = names_boundary = 0
    for is_amount, block in groupby(rows, key=_column_amount):
        amounts = list(block)
        start, end = end, end + len(amounts)
        if not is_amount:
            names_boundary = start
            continue
        name_start = start
        while (name_start > names_boundary and name_start - 1 != shop_header_index
               and _orphan_name(rows[name_start - 1])):
            name_start -= 1
        names = rows[name_start:start]
        if not names:
            continue
        joined = _pair_column_rows(names, amounts)
        if joined is None:
            return None
        if restored is None:
            restored = list(lines)
        # Preserve the positions of the surrounding rows for the legacy parser.
        restored[name_start:end] = joined + [""] * len(amounts)
    return restored


def _join_split_deposit(section):
    """Join an adjacent deposit row inside a returnable-packaging section."""
    calculation = _COLUMN_CALCULATION.fullmatch(" ".join(section["calculation"].split()))
    subtotal = Decimal(section["subtotal"].replace(",", "."))
    if _reconciled_gross(calculation) != subtotal:
        return section[0]
    return section[0].replace(f"{section['name']}\n", f"{section['name'].rstrip()} ", 1)


def _reconciles_with_subtotal(items, subtotal):
    net = _bounded_sum(line["net_line_total"] for line in items)
    return abs(Decimal(net) - Decimal(subtotal)) <= _COLUMN_TOLERANCE


def _column_split_fallback(lines, shop_header_index):
    for index, row in enumerate(lines):
        subtotal = _COLUMN_SUBTOTAL.fullmatch(row)
        if subtotal is not None:
            break
    else:
        return None
    try:
        subtotal_amount = _decimal(subtotal["amount"])
        item_rows = lines[:index]
        # SUMA excludes the packaging and payment lines that follow it.
        if _reconciles_with_subtotal(_parse_lines(item_rows, shop_header_index), subtotal_amount):
            return None
        restored = _restore_item_columns(item_rows, shop_header_index)
        if restored is None:
            return None
        items = _parse_lines(restored, shop_header_index)
        if (any(compare_money(item["net_line_total"], "0") < 0 for item in items)
                or not _reconciles_with_subtotal(items, subtotal_amount)):
            return None
        tail = _SPLIT_DEPOSIT_SECTION.sub(_join_split_deposit, "\n".join(lines[index:]))
        restored.extend(tail.split("\n"))
        parsed = _parse_lines(restored, shop_header_index)
    except ReceiptError:
        return None
    return restored, parsed, subtotal_amount


def parse_receipt(text):
    normalized = _receipt_text(text)
    text = _redact_normalized(normalized)
    # Drop a line before every parsing expression. A 32 KiB line must not reach one.
    lines = ["" if len(line) > MAX_LINE_CHARS else line for line in text.split("\n")]
    if len(lines) > MAX_TEXT_LINES:
        raise ReceiptError()
    normalized_lines = ["" if len(line) > MAX_LINE_CHARS else line for line in normalized.split("\n")]
    nip = _seller_nip(lines, normalized_lines)
    shop_name, shop_header_index = _shop_header(lines)
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
    has_card = "karta" in key or "platnicza" in key or "paypass" in key or _MASTERCARD_PAYMENT.search(key) is not None
    has_cash = "gotowka" in key
    payment = "mixed" if has_card and has_cash else "blik" if "blik" in key else "card" if has_card else "cash" if has_cash else "unknown"
    parsed_lines = _parse_lines(lines, shop_header_index)
    column_split = _column_split_fallback(lines, shop_header_index)
    if column_split is not None:
        restored, parsed_lines, subtotal = column_split
        shop_name, _ = _shop_header(restored)
        total = total or subtotal
    math_delta = None if total is None else _bounded_sum((
        _bounded_sum(line["net_line_total"] for line in parsed_lines), negate_money(total)))
    return {
        "raw_shop_name": shop_name, "raw_nip": nip, "chain_key": chain_for(shop_name, nip),
        "receipt_kind": "refund" if _REFUND_WORD.search(key) else "purchase", "receipt_date": receipt_date,
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
