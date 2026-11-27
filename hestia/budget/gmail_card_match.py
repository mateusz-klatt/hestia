"""Corroborate a parsed purchase document with a single owner-scoped, booked card debit."""
import re
from dataclasses import dataclass
from datetime import date
from enum import Enum, auto

from sqlalchemy import case, func, or_

from ..db import BudgetTransaction, BudgetTransactionMetadata
from .analytics_core import AnalyticsError, canonical_money, financial_fingerprint
from .bank_metadata import exchange_evidence
from .config import BudgetConfigError
from .gmail_config import load_card_accounts
from .gmail_payment import _card_suffixes
from .receipt_parser import (
    _distinctive_shop_words,
    contains_gateway_marker,
    fold_text,
    merchant_score_groups,
    merchant_word_tokens,
)
from .receipt_reconcile import _bound_date, _json, link_evidence, resolved_party_names
from .receipt_store import BudgetReceiptStore, _nip_match, _receipt_in_group, _shift_clamped

_CARD_PURCHASE = re.compile(
    r"^(?:(?:platnosc|transakcja|zakup|operacja)(?: bezgotowkowa)? karta\b|zakup przy uzyciu karty\b"
    r"|(?:debit |credit )?card (?:purchase|payment|transaction)\b|(?:pos|ecommerce) card (?:purchase|payment)\b)"
)
_NON_PURCHASE = re.compile(
    r"^(?:przelew|polecenie zaplaty|splata karty|wyplata (?:z bankomatu|gotowki)|odsetki|prowizja"
    r"|oplata za karte|bank transfer|credit transfer|direct debit|card repayment|credit card repayment"
    r"|cash withdrawal|cash advance|chargeback)\b"
)
_BLIK_DEBIT = re.compile(r"^(?:(?:platnosc|transakcja|payment|zakup) )?blik\b")
_ORIGINAL_AMOUNT = re.compile(
    r"\b(?:kwota (?:oryginalna|w walucie (?:oryginalnej|transakcji))|original (?:transaction )?amount)"
    r"\s*[:=]?\s*(?:([A-Z]{3})\s+)?(\d{1,16}(?:[,.]\d{1,12})?)(?:\s+([A-Z]{3})\b)?(?![\d,.])",
    re.ASCII | re.IGNORECASE,
)
_DATE_KEYS = ("transaction_date", "booking_date", "value_date")
_MAX_DUE_DATE_DAYS = 45

# A Google debit can create ambiguity even if its descriptor cannot prove a purchase.
_GOOGLE_DEBIT = re.compile(r"^google\b")
_GOOGLE_BILLING = re.compile(
    r"google(?:\s*\*\s*|\s+)(?:google\s*)?(?P<product>storage|one|"
    r"workspace|play(?: store)?|youtube(?: premium| music| videos| tv)?|cloud(?:_[a-z0-9-]+)?)"
    r"(?:\s+g\.co/helppay#?)?(?:\s+[a-z]{2})?"
)
_GOOGLE_ISSUERS = {("google",), ("google", "commerce"), ("google", "ireland"), ("google", "llc"),
                   ("google", "payment"), ("google", "payments"), ("google", "poland")}
_BILLING_DESCRIPTORS = {
    ("apple", "distribution", "international"): re.compile(r"apple\.com/bill(?:\s+cork)?(?:\s+[a-z]{2})?"),
}


@dataclass(frozen=True, repr=False)
class CardPurchase:
    account_id: str
    entry_reference: str
    fingerprint: str
    evidence: dict
    receipt_date: str


class _CardCandidateBlock(Enum):
    OCCUPIED = auto()
    UNCORROBORATED_GOOGLE_DEBIT = auto()


@dataclass(frozen=True, repr=False)
class CardCandidate:
    transaction_key: tuple[str, str]
    result: CardPurchase | _CardCandidateBlock

    @property
    def available_purchase(self) -> CardPurchase | None:
        return self.result if isinstance(self.result, CardPurchase) else None

    @property
    def is_corroborated(self) -> bool:
        return self.result is not _CardCandidateBlock.UNCORROBORATED_GOOGLE_DEBIT

    @property
    def is_occupied(self) -> bool:
        return self.result is _CardCandidateBlock.OCCUPIED


def _card_amount_matches(payload, currency, amount, lines, metadata):
    if payload["currency"] == currency:
        return canonical_money(payload["amount"]) == amount
    if "exchange_rate" in metadata and metadata["exchange_rate"] is None:
        return False
    original = exchange_evidence(metadata.get("exchange_rate")).get("instructed_amount")
    amounts = {(original["currency"], canonical_money(original["amount"]))} if original else set()
    for line in lines:
        for match in _ORIGINAL_AMOUNT.finditer(line):
            before, quantity, after = match.groups()
            if (before is None) != (after is None):
                amounts.add(((before or after).upper(), canonical_money(quantity.replace(",", "."))))
    return amounts == {(currency, amount)}


def _bank_descriptor(line):
    return _CARD_PURCHASE.sub("", fold_text(line).strip()).lstrip(" :-")


def _google_billing_matches(issuer, descriptor):
    billing = _GOOGLE_BILLING.fullmatch(descriptor)
    # Only known billing entities corroborate a product they do not name.
    return billing is not None and (
        issuer in _GOOGLE_ISSUERS or issuer == ("google", *billing["product"].split())
    )


def _merchant_matches(invoice, lines, creditor):
    name = invoice.issuer
    if contains_gateway_marker(name):
        return False
    text = " ".join(filter(None, (creditor, *lines)))
    if _nip_match(invoice.nip, text.casefold())[1]:
        return True
    descriptor = _BILLING_DESCRIPTORS.get(merchant_word_tokens(name))
    if descriptor and any(descriptor.fullmatch(_bank_descriptor(line)) for line in filter(None, (creditor, *lines))):
        return True
    groups, _ = merchant_score_groups(name, invoice.nip)
    words = frozenset(merchant_word_tokens(text))
    return bool(groups) and all(any(alias <= words for alias in group) for group in groups)


def _payment_matches(invoice, transaction, account_kind, lines, mapped_accounts):
    descriptions = [" ".join(merchant_word_tokens(line)) for line in lines]
    if any(_NON_PURCHASE.match(line) for line in descriptions):
        return False
    is_blik = any(_BLIK_DEBIT.match(line) for line in descriptions)
    is_card = any(_CARD_PURCHASE.match(line) for line in descriptions)
    if getattr(invoice, "payment_method", None) == "blik":
        return is_blik and not is_card and account_kind != "credit_card" and not _card_suffixes(lines)
    if is_blik or (account_kind != "credit_card" and not is_card):
        return False
    last4 = getattr(invoice, "card_last4", None)
    if last4 is None:
        return True
    if mapped_accounts is not None and transaction.account_id not in mapped_accounts:
        return False
    suffixes = _card_suffixes(lines)
    if suffixes:
        return suffixes == {last4}
    return mapped_accounts is not None


def _card_row(session, owner, invoice, transaction, account_kind, amount, receipt_date, start, end,
              mapped_accounts, google_issuer):
    payload = _json(transaction.payload_json)
    fingerprint = financial_fingerprint(payload)
    if canonical_money(payload["amount"]) == "0" or not any(
        start <= day <= end for key in _DATE_KEYS if (day := _bound_date(payload.get(key)))
    ):
        return None
    lines = payload.get("remittance_information", [])
    if not isinstance(lines, list) or any(not isinstance(value, str) for value in lines):
        return None
    if not _payment_matches(invoice, transaction, account_kind, lines, mapped_accounts):
        return None
    metadata = session.get(BudgetTransactionMetadata, (transaction.account_id, transaction.entry_reference))
    data = {}
    if metadata and metadata.owner == owner and metadata.ledger_fingerprint == fingerprint:
        data = _json(metadata.payload_json)
    amount_matches = isinstance(data, dict) and _card_amount_matches(payload, invoice.currency, amount, lines, data)
    if google_issuer is None and not amount_matches:
        return None
    creditor, debtor = resolved_party_names(session, owner, transaction.account_id, transaction.entry_reference,
                                           payload, fingerprint)
    if google_issuer is not None:
        descriptors = [_bank_descriptor(line) for line in filter(None, (creditor, *lines))]
        if not any(_GOOGLE_DEBIT.match(line) for line in descriptors):
            return None
        if not amount_matches or not any(_google_billing_matches(google_issuer, line) for line in descriptors):
            # An uncorroborated Google debit still prevents a unique match in this window.
            return _CardCandidateBlock.UNCORROBORATED_GOOGLE_DEBIT
    elif not _merchant_matches(invoice, lines, creditor):
        return None
    evidence = link_evidence(owner, transaction.account_id, transaction.entry_reference, payload,
                             invoice.issuer, invoice.nip, creditor, fingerprint, debtor=debtor)
    evidence["gmail_card"] = True
    return CardPurchase(transaction.account_id, transaction.entry_reference, fingerprint, evidence, receipt_date)


def card_purchase_candidates(session, owner, invoice, *, received_date=None, include_receipt_id=None):
    """Enumerate plausible debits, including reservations and uncorroborated Google debits."""
    if isinstance(received_date, date):
        received_date = received_date.isoformat()
    receipt_date = _bound_date(invoice.issue_date or received_date)
    if (not receipt_date or getattr(invoice, "document_kind", "invoice") not in ("invoice", "receipt")
            or getattr(invoice, "payment_method", None) in ("cash", "mixed")):
        return []
    if include_receipt_id is not None and _receipt_in_group(session, owner, include_receipt_id):
        return []
    try:
        amount = canonical_money(invoice.amount)
    except AnalyticsError:
        return []
    if amount == "0" or amount.startswith("-"):
        return []
    last4 = getattr(invoice, "card_last4", None)
    try:
        mapped_accounts = load_card_accounts().get(last4) if last4 is not None else None
    except BudgetConfigError:
        return []
    issuer = merchant_word_tokens(invoice.issuer)
    google_issuer = issuer if issuer in _GOOGLE_ISSUERS or _distinctive_shop_words(invoice.issuer) == {"google"} else None
    target = date.fromisoformat(receipt_date)
    due = _bound_date(invoice.due_date)
    last = date.fromisoformat(due) if due else target
    if not 0 <= (last - target).days <= _MAX_DUE_DATE_DAYS:
        last = target
    start, end = _shift_clamped(target, -4).isoformat(), _shift_clamped(last, 4).isoformat()
    raw = BudgetTransaction.payload_json
    valid_json = case((func.json_valid(raw), raw), else_="{}")
    filters = (func.json_extract(valid_json, "$.status") == "BOOK",
               func.json_extract(valid_json, "$.credit_debit_indicator") == "DBIT",
               or_(*(func.json_extract(valid_json, "$." + key).between(start, end) for key in _DATE_KEYS)))
    candidates = []
    for transaction, account_kind, occupied in BudgetReceiptStore._candidate_rows(
        session, owner, include_receipt_id=include_receipt_id, card_filters=filters
    ):
        try:
            candidate = _card_row(session, owner, invoice, transaction, account_kind, amount, receipt_date, start,
                                  end, mapped_accounts, google_issuer)
        except (AnalyticsError, KeyError, ValueError, TypeError, RecursionError):
            continue
        if candidate is not None:
            if occupied and candidate is not _CardCandidateBlock.UNCORROBORATED_GOOGLE_DEBIT:
                candidate = _CardCandidateBlock.OCCUPIED
            candidates.append(CardCandidate((transaction.account_id, transaction.entry_reference), candidate))
    return candidates


def match_card_purchase(session, owner, invoice, *, received_date=None, include_receipt_id=None):
    """Return a purchase only when its sole plausible debit is available."""
    candidates = card_purchase_candidates(
        session, owner, invoice, received_date=received_date, include_receipt_id=include_receipt_id,
    )
    return candidates[0].available_purchase if len(candidates) == 1 else None


def still_matches_card(session, row):
    """Only a receipt auto-confirmed by this matcher needs uniqueness/FX revalidation."""
    from .gmail_card_import import receipt_document

    match = match_card_purchase(session, row.owner, receipt_document(row), include_receipt_id=row.id)
    return match is not None and (match.account_id, match.entry_reference) == (row.linked_account_id, row.linked_entry_ref)
