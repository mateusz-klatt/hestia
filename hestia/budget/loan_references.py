"""Extract masked references from recurring bank labels for mortgage schedule links."""
from __future__ import annotations

import hashlib
import re
import unicodedata

MAX_MATCH_REFERENCES = 16
_ALNUM = r"[^\W_]"
_TOKEN = rf"(?:[0-9]{{2,}}(?:[ \u00a0][0-9]{{4}})+|{_ALNUM}+(?:[./-]{_ALNUM}+)*)"
_REFERENCES = re.compile(
    rf"\b(?:NR|NUMER|REF(?:ERENCJA|ERENCE)?|NO)[.:#\s]+(?P<numbered>{_TOKEN})(?!\w)"
    rf"|(?<!\w)(?P<token>{_TOKEN})(?!\w)", re.IGNORECASE,
)
_NON_ALNUM = re.compile(r"[^A-Z0-9]")
_TWO_DIGITS = re.compile(r"[0-9][A-Z]*[0-9]")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}|[0-9]{1,4}([./-])[0-9]{1,2}\1[0-9]{1,4}")


def normalized_loan_label(label: str) -> str:
    decomposed = unicodedata.normalize("NFKD", label.upper().replace("Ł", "L"))
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _reference_tokens(label):
    for match in _REFERENCES.finditer(label):
        kind = match.lastgroup
        token = match.group(kind)
        normalized = _NON_ALNUM.sub("", normalized_loan_label(token))
        if len(normalized) < 5 or _DATE.fullmatch(token):
            continue
        if kind == "token" and (len(normalized) < 6 or not _TWO_DIGITS.search(normalized)):
            continue
        yield match.span(kind), normalized


def _hash(normalized):
    return hashlib.sha256(normalized.encode("ascii")).hexdigest()


def extract_reference_hashes(label: str) -> set[str]:
    return {_hash(normalized) for _, normalized in _reference_tokens(label)}


def _mask(label, tokens):
    start = 0
    parts = []
    for (begin, end), normalized in tokens:
        parts.extend((label[start:begin], "••••", normalized[-4:]))
        start = end
    parts.append(label[start:])
    return "".join(parts)


def mask_loan_references(label: str) -> str:
    return _mask(label, _reference_tokens(label))


def loan_reference_candidates(label: str) -> list[dict[str, str]]:
    tokens = list(_reference_tokens(label))
    masked = _mask(label, tokens)
    return [{"hash": _hash(normalized), "last4": normalized[-4:], "label": masked}
            for _, normalized in tokens]
