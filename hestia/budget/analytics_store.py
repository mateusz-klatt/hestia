"""Owner-private classifications and transfer decisions, separate from bank records."""
from __future__ import annotations

from datetime import date
import hmac
import json
import math
import os
import re
import unicodedata
import uuid

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.exc import IntegrityError

from .. import api_contract
from ..db import BudgetAccount as Account, BudgetAccountIdentity as Identity
from ..db import BudgetAnnotation as Annotation, BudgetAnalyticsAudit as Audit, BudgetCategory as Category
from ..db import BudgetAnnotationLabelTranslation as LabelTranslation
from ..db import BudgetCategoryTranslation as CategoryTranslation
from ..db import BudgetCategoryLimit as CategoryLimitRow
from ..db import BudgetMortgagePayment as MortgagePaymentRow, BudgetMortgageSchedule as MortgageScheduleRow
from ..db import BudgetConnection as Connection, BudgetRule as Rule, BudgetRulePreview as Preview
from ..db import BudgetReceiptLinkGroup as ReceiptLinkGroup
from ..db import BudgetTransaction as Transaction, BudgetTransactionMetadata as Metadata
from ..db import BudgetTransferDecision as Decision, BudgetTransferLeg as Leg
from .analytics_core import (AnalyticsError, KINDS, COUNTERPARTY_RULE_PREFIX, clean_one_off_label, compare_money,
                             compatible_kind, compile_rules, counterparty_key_id, counterparty_rule_from_identification, counterparty_rule_parts,
                             counterparty_rule_value, default_effective_kind, effective_date, financial_fingerprint,
                             first_matching_rule, match_rule, match_texts, pair_fingerprint)
from .client import _reject_constant, _unique_error_fields
from .config import BudgetConfigError, _read_secret
from .i18n import LANGUAGES
from .receipt_reconcile import reconcile_receipt_links
from .mortgage_reminders import (MAX_PAID_MONTHS as MAX_MORTGAGE_PAID_MONTHS, MAX_SCHEDULES as MAX_MORTGAGE_SCHEDULES,
                                 PRIVATE_DESTINATIONS as MORTGAGE_DESTINATIONS, MortgageInputError,
                                 _parse_amount as _parse_mortgage_amount, _parse_date as _parse_mortgage_date,
                                 _parse_month as _parse_mortgage_month, installment_due_date)
from .spending_limits import SpendingLimitInputError, _format_scaled, _parse_amount
from .views import BudgetHTTPStore

MAX_CATEGORY_LIMITS = 200
_TRANSLATION_ENTRY_ADAPTER = TypeAdapter(
    api_contract.BudgetCategoryTranslationImportEntry | api_contract.BudgetLabelTranslationImportEntry)
_LIMIT_ERROR = "budget_analytics_limit"
MAX_RULES = 5000
MAX_BATCH_RULES = 100
_HASH = re.compile(r"[a-f0-9]{64}\Z", re.ASCII)
_CURRENCY = re.compile(r"[A-Z]{3}\Z", re.ASCII)
_RULE_FIELDS = frozenset({"rule_id", "expected_version", "field", "operator", "value", "category_id", "kind", "priority", "enabled"})
_CATEGORY_FIELDS = ("id", "name", "archived", "version", "created_at", "updated_at", "parent_id", "color_hue")
_CATEGORY_HUE_OFFSET = 20
_MAX_CATEGORY_DEPTH = 3
_UNSET = object()
_RULE_KEY_UNCHECKED = object()
_CATEGORY_LIMIT_FIELDS = ("id", "category_id", "currency", "monthly_limit", "warning_percent", "enabled",
                          "version", "created_at", "updated_at")
_MORTGAGE_SCHEDULE_FIELDS = ("id", "label", "amount", "currency", "due_day", "start_date", "end_date",
                             "remind_days_before", "destination_kind", "enabled", "version", "created_at", "updated_at")
_RULE_OUTPUT = ("id", "field", "operator", "value", "category_id", "kind", "priority", "enabled", "version", "created_at", "updated_at")
_ANNOTATION_FIELDS = ("account_id", "entry_reference", "mode", "category_id", "kind", "ledger_fingerprint", "version", "updated_at")
_DECISION_FIELDS = ("id", "debit_account_id", "debit_ref", "credit_account_id", "credit_ref", "source", "status",
                    "debit_fingerprint", "credit_fingerprint", "rule_version", "version", "created_at", "updated_at")


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def _dict(row, fields):
    return {key: getattr(row, key) for key in fields}


def _same_account_digest(row, key):
    """True when an edit resubmits the digest already stored on this account rule."""
    if row is None or row.field != "counterparty_account" or not isinstance(key, str):
        return False
    stored_key = counterparty_rule_parts(row.value)[1]
    return hmac.compare_digest(key, stored_key)


def _account_rule_inactive_reason(stored_key_id, key_context):
    """``key_context`` is ``None`` when the rule key is missing or unsafe, else ``(secret, key id)``."""
    if key_context is None:
        return "rule_key_unavailable"
    if key_context[1] != stored_key_id:
        return "rule_key_rotated"
    return None


def _rule_wire(row, *, key_context=_RULE_KEY_UNCHECKED):
    result = _dict(row, _RULE_OUTPUT)
    if result["field"] == "counterparty_account":
        try:
            key_id, key, masked = counterparty_rule_parts(result["value"])
        except AnalyticsError:
            raise AnalyticsError("budget_analytics_data_error", 500) from None
        result["value"] = masked
        result["counterparty_account_key"] = key
        if key_context is not _RULE_KEY_UNCHECKED:
            reason = _account_rule_inactive_reason(key_id, key_context)
            if reason is not None:
                result["inactive_reason"] = reason
    return result


def _annotation_wire(row):
    """API shape. Includes the owner-private label and never a log line."""
    return {**_dict(row, _ANNOTATION_FIELDS), "label": row.label}


def _annotation_audit(row):
    """Audit allowlist. Records whether a label is set and never the text."""
    return {**_dict(row, _ANNOTATION_FIELDS), "has_label": row.label is not None}


_KEEP_LABEL = object()
_TRANSLATION_JOINERS = frozenset("\u200c\u200d")


def _text(value, limit=80, *, translation=False):
    if not isinstance(value, str) or len(value) > limit:
        raise AnalyticsError()
    for index, char in enumerate(value):
        if unicodedata.category(char) not in ("Cc", "Cf", "Cs"):
            continue
        if (not translation or char not in _TRANSLATION_JOINERS or index == 0 or index + 1 == len(value)
                or unicodedata.category(value[index - 1])[0] not in ("L", "M")
                or unicodedata.category(value[index + 1])[0] not in ("L", "M")):
            raise AnalyticsError()
    normalized = unicodedata.normalize("NFC", value.strip())
    if not normalized or len(normalized) > limit:
        raise AnalyticsError()
    return normalized


def _name_key(name):
    return unicodedata.normalize("NFC", name.replace("\u200c", "").replace("\u200d", "")).casefold()


def _version(current, expected):
    if type(expected) is not int or expected < 0:
        raise AnalyticsError()
    if expected != current:
        raise AnalyticsError("budget_analytics_conflict", 409)


def _fingerprint(current, expected):
    if not isinstance(expected, str) or not _HASH.fullmatch(expected):
        raise AnalyticsError()
    if not hmac.compare_digest(current, expected):
        raise AnalyticsError("budget_analytics_source_changed", 409)


def _even_hues(count):
    return [round(index * 360 / count + _CATEGORY_HUE_OFFSET) % 360 for index in range(count)]


def _largest_gap_hue(hues):
    ordered = sorted(set(hues))
    if not ordered:
        return _CATEGORY_HUE_OFFSET
    gaps = []
    for index, start in enumerate(ordered):
        end = ordered[(index + 1) % len(ordered)]
        if index == len(ordered) - 1:
            end += 360
        gaps.append((end - start, start, end))
    _width, start, end = min(gaps, key=lambda gap: (-gap[0], gap[1]))
    return (start + (end - start) // 2) % 360


class BudgetAnalyticsStore(BudgetHTTPStore):
    def __init__(self, sessions, *, rule_key_file=None, **kwargs):
        super().__init__(sessions, **kwargs)
        self._rule_key_file = rule_key_file

    def _rule_key_context(self, *, required=True):
        path = self._rule_key_file or os.environ.get("HESTIA_BUDGET_RULE_KEY_FILE")
        try:
            if not isinstance(path, str) or not path:
                raise ValueError
            secret = _read_secret(path, max_bytes=1024)
            if len(secret) < 32:
                raise ValueError
            return secret, counterparty_key_id(secret)
        except (BudgetConfigError, TypeError, ValueError):
            if required:
                raise AnalyticsError("budget_analytics_rule_key_unavailable", 400) from None
            return None

    def _now(self):
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise AnalyticsError("budget_analytics_unavailable", 503)
        return now

    @staticmethod
    def _owned(session, model, owner, identifier):
        if not isinstance(identifier, str) or not identifier:
            raise AnalyticsError()
        row = session.get(model, identifier)
        if row is None or row.owner != owner:
            raise AnalyticsError("budget_analytics_not_found", 404)
        return row

    def _category(self, session, owner, identifier, *, retain_key=None):
        if identifier is None:
            return None
        row = self._owned(session, Category, owner, identifier)
        if row.archived:
            if retain_key is None:
                raise AnalyticsError()
            snapshot = self._snapshot(session, owner)
            if not any((entry["account_id"], entry["entry_reference"]) == retain_key
                       and entry.get("base_effective", entry["effective"]).get("category_id") == identifier
                       for entry in snapshot["transactions"]):
                raise AnalyticsError()
        return row

    def _transaction(self, session, owner, account_id, reference, *, selected=True):
        if not isinstance(reference, str) or not 1 <= len(reference) <= 1024:
            raise AnalyticsError()
        account, _ = self._owned_account(session, owner, account_id)
        row = session.get(Transaction, (account_id, reference))
        if row is None:
            raise AnalyticsError("budget_analytics_not_found", 404)
        try:
            payload = json.loads(row.payload_json, object_pairs_hook=_unique_error_fields,
                                 parse_constant=_reject_constant)
        except (ValueError, TypeError):
            raise AnalyticsError() from None
        fingerprint = financial_fingerprint(payload)
        if selected and (not account.enabled or row.uncertain or payload["status"] != "BOOK"
                         or effective_date(payload) is None):
            raise AnalyticsError()
        return row, payload, fingerprint

    def _analytics_audit(self, session, owner, subject_type, subject_id, action, before, after):
        session.add(Audit(owner=owner, subject_type=subject_type, subject_id=subject_id, action=action,
                          before_json=_json(before), after_json=_json(after), changed_at=self._now()))

    def categories(self, owner):
        with self._read(owner) as session:
            return [_dict(row, _CATEGORY_FIELDS) for row in session.scalars(select(Category).where(Category.owner == owner)
                                                                           .order_by(Category.name_key, Category.id))]

    @staticmethod
    def _next_category_hue(session, owner, *, exclude_id=None):
        query = select(Category.color_hue).where(Category.owner == owner, Category.parent_id.is_(None),
                                                 Category.color_hue.is_not(None))
        if exclude_id is not None:
            query = query.where(Category.id != exclude_id)
        return _largest_gap_hue(session.scalars(query))

    @staticmethod
    def _validate_category_hue(value):
        if value is not None and (type(value) is not int or not 0 <= value <= 359):
            raise AnalyticsError()

    def _checked_parent(self, session, owner, parent_id, subject_id):
        """Same-owner active parent, no cycle, and the moved subtree stays within depth 3."""
        if parent_id is None:
            return None
        if not isinstance(parent_id, str) or not parent_id or parent_id == subject_id:
            raise AnalyticsError()
        parent = self._owned(session, Category, owner, parent_id)
        if parent.archived:
            raise AnalyticsError()
        depth = 1
        seen = {parent.id}
        cursor = parent
        while cursor.parent_id is not None:
            if cursor.parent_id == subject_id or cursor.parent_id in seen:
                raise AnalyticsError()
            nxt = session.get(Category, cursor.parent_id)
            if nxt is None or nxt.owner != owner:
                raise AnalyticsError()
            seen.add(nxt.id)
            depth += 1
            if depth > _MAX_CATEGORY_DEPTH:
                raise AnalyticsError()
            cursor = nxt
        height = 0 if subject_id is None else self._subtree_height(session, owner, subject_id)
        if depth + height > _MAX_CATEGORY_DEPTH - 1:
            raise AnalyticsError()
        return parent.id

    @staticmethod
    def _subtree_height(session, owner, category_id):
        rows = session.execute(select(Category.id, Category.parent_id).where(Category.owner == owner)).all()
        children = {}
        for identifier, parent in rows:
            if parent:
                children.setdefault(parent, []).append(identifier)
        height = 0
        stack = [(child, 1) for child in children.get(category_id, ())]
        seen = {category_id}
        while stack:
            node, dist = stack.pop()
            if node in seen:
                raise AnalyticsError()
            seen.add(node)
            if dist > height:
                height = dist
            for child in children.get(node, ()):
                stack.append((child, dist + 1))
        return height

    def _has_children(self, session, owner, category_id):
        child = session.scalar(select(Category.id).where(Category.owner == owner, Category.parent_id == category_id).limit(1))
        return child is not None

    def create_category(self, owner, name, parent_id=None):
        name = " ".join(_text(name).split())
        key = _name_key(name)
        with self.transaction(owner, "budget.edit") as session:
            self._owner(session, owner)
            prior = session.scalar(select(Category).where(Category.owner == owner, Category.name_key == key))
            if prior is not None:
                # The same name and parent is an idempotent retry. A different parent must not
                # look like success: the existing row would keep its parent and the form would clear.
                if prior.name != name or prior.parent_id != parent_id:
                    raise AnalyticsError("budget_analytics_conflict", 409)
                return {**_dict(prior, _CATEGORY_FIELDS), "changed": False}
            translated = session.scalar(select(CategoryTranslation.category_id).where(
                CategoryTranslation.owner == owner, CategoryTranslation.name_key == key).limit(1))
            if translated is not None:
                raise AnalyticsError("budget_analytics_conflict", 409)
            if session.scalar(select(func.count()).select_from(Category).where(Category.owner == owner)) >= 100:
                raise AnalyticsError("budget_analytics_limit", 413)
            parent_id = self._checked_parent(session, owner, parent_id, None)
            now = self._now()
            hue = None if parent_id is not None else self._next_category_hue(session, owner)
            row = Category(id=str(uuid.uuid4()), owner=owner, name=name, name_key=key, archived=False, version=1,
                           created_at=now, updated_at=now, parent_id=parent_id, color_hue=hue)
            session.add(row)
            try:
                session.flush()
            except IntegrityError:
                raise AnalyticsError("budget_analytics_conflict", 409) from None
            result = _dict(row, _CATEGORY_FIELDS)
            self._analytics_audit(session, owner, "category", row.id, "create", None, result)
            return {**result, "changed": True}

    def update_category(self, owner, identifier, *, name, archived, expected_version, parent_id=_UNSET, color_hue=_UNSET):
        name = " ".join(_text(name).split())
        key = _name_key(name)
        if type(archived) is not bool:
            raise AnalyticsError()
        if color_hue is not _UNSET:
            self._validate_category_hue(color_hue)
        with self.transaction(owner, "budget.edit") as session:
            self._owner(session, owner)
            row = self._owned(session, Category, owner, identifier)
            _version(row.version, expected_version)
            other = session.scalar(select(Category).where(Category.owner == owner, Category.name_key == key,
                                                          Category.id != row.id))
            if other is not None:
                raise AnalyticsError("budget_analytics_conflict", 409)
            translated = session.scalar(select(CategoryTranslation.category_id).where(
                CategoryTranslation.owner == owner, CategoryTranslation.name_key == key,
                CategoryTranslation.category_id != row.id).limit(1))
            if translated is not None:
                raise AnalyticsError("budget_analytics_conflict", 409)
            new_parent = row.parent_id if parent_id is _UNSET else self._checked_parent(session, owner, parent_id, row.id)
            if new_parent is not None and color_hue is not _UNSET:
                raise AnalyticsError()
            before = _dict(row, _CATEGORY_FIELDS)
            new_hue = row.color_hue
            if new_parent is not None:
                new_hue = None
            elif color_hue is not _UNSET:
                new_hue = (self._next_category_hue(session, owner, exclude_id=row.id)
                           if color_hue is None else color_hue)
            elif row.parent_id is not None or (row.color_hue is None and row.archived and not archived):
                new_hue = self._next_category_hue(session, owner, exclude_id=row.id)
            if (row.name, row.archived, row.parent_id, row.color_hue) == (name, archived, new_parent, new_hue):
                return {**before, "changed": False}
            if archived and self._has_children(session, owner, row.id):
                raise AnalyticsError()
            row.name, row.name_key, row.archived, row.parent_id = name, key, archived, new_parent
            row.color_hue = new_hue
            row.version += 1
            row.updated_at = self._now()
            try:
                session.flush()
            except IntegrityError:
                raise AnalyticsError("budget_analytics_conflict", 409) from None
            result = _dict(row, _CATEGORY_FIELDS)
            self._analytics_audit(session, owner, "category", row.id, "update", before, result)
            return {**result, "changed": True}

    def spread_category_colors(self, owner):
        with self.transaction(owner, "budget.edit") as session:
            self._owner(session, owner)
            rows = list(session.scalars(select(Category).where(
                Category.owner == owner, Category.parent_id.is_(None), Category.archived.is_(False))
                .order_by(Category.name_key, Category.id)))
            result = []
            for row, hue in zip(rows, _even_hues(len(rows))):
                before = _dict(row, _CATEGORY_FIELDS)
                if row.color_hue == hue:
                    result.append(before)
                    continue
                row.color_hue = hue
                row.version += 1
                row.updated_at = self._now()
                after = _dict(row, _CATEGORY_FIELDS)
                self._analytics_audit(session, owner, "category", row.id, "color-spread", before, after)
                result.append(after)
            return result

    @staticmethod
    def _translation_language(lang):
        if not isinstance(lang, str) or lang not in LANGUAGES:
            raise AnalyticsError()
        return lang

    def _set_category_translation(self, session, owner, category_id, lang, name):
        lang = self._translation_language(lang)
        name = " ".join(_text(name, translation=True).split())
        self._owned(session, Category, owner, category_id)
        key = _name_key(name)
        canonical = session.scalar(select(Category.id).where(
            Category.owner == owner, Category.name_key == key, Category.id != category_id).limit(1))
        if canonical is not None:
            raise AnalyticsError("budget_analytics_conflict", 409)
        other = session.scalar(select(CategoryTranslation).where(
            CategoryTranslation.owner == owner, CategoryTranslation.name_key == key,
            CategoryTranslation.category_id != category_id,
        ))
        if other is not None:
            raise AnalyticsError("budget_analytics_conflict", 409)
        row = session.get(CategoryTranslation, (category_id, lang))
        changed = row is None or row.name != name
        if row is None:
            row = CategoryTranslation(category_id=category_id, owner=owner, lang=lang, name=name, name_key=key)
            session.add(row)
        else:
            row.name, row.name_key = name, key
        try:
            session.flush()
        except IntegrityError:
            raise AnalyticsError("budget_analytics_conflict", 409) from None
        return {"category_id": category_id, "lang": lang, "name": name, "changed": changed}

    def set_category_translation(self, owner, *, category_id, lang, name):
        with self.transaction(owner, "budget.edit") as session:
            self._owner(session, owner)
            return self._set_category_translation(session, owner, category_id, lang, name)

    def delete_category_translation(self, owner, *, category_id, lang):
        lang = self._translation_language(lang)
        with self.transaction(owner, "budget.edit") as session:
            self._owner(session, owner)
            self._owned(session, Category, owner, category_id)
            row = session.get(CategoryTranslation, (category_id, lang))
            if row is not None:
                session.delete(row)
            return {"category_id": category_id, "lang": lang, "changed": row is not None}

    def _owned_label(self, session, owner, account_id, entry_reference, *, require_label):
        row = session.get(Annotation, (account_id, entry_reference))
        if row is None or row.owner != owner or (require_label and row.label is None):
            raise AnalyticsError("budget_analytics_not_found", 404)
        return row

    def _set_label_translation(self, session, owner, account_id, entry_reference, lang, label):
        lang = self._translation_language(lang)
        label = _text(label, translation=True)
        self._owned_label(session, owner, account_id, entry_reference, require_label=True)
        key = (account_id, entry_reference, lang)
        row = session.get(LabelTranslation, key)
        changed = row is None or row.label != label
        if row is None:
            row = LabelTranslation(account_id=account_id, entry_reference=entry_reference, lang=lang, label=label)
            session.add(row)
        else:
            row.label = label
        session.flush()
        return {"account_id": account_id, "entry_reference": entry_reference, "lang": lang,
                "label": label, "changed": changed}

    def set_label_translation(self, owner, *, account_id, entry_reference, lang, label):
        with self.transaction(owner, "budget.edit") as session:
            self._owner(session, owner)
            return self._set_label_translation(session, owner, account_id, entry_reference, lang, label)

    def delete_label_translation(self, owner, *, account_id, entry_reference, lang):
        lang = self._translation_language(lang)
        with self.transaction(owner, "budget.edit") as session:
            self._owner(session, owner)
            self._owned_label(session, owner, account_id, entry_reference, require_label=False)
            row = session.get(LabelTranslation, (account_id, entry_reference, lang))
            if row is not None:
                session.delete(row)
            return {"account_id": account_id, "entry_reference": entry_reference, "lang": lang,
                    "changed": row is not None}

    def import_translations(self, owner, *, entries):
        if not isinstance(entries, list) or not 1 <= len(entries) <= 500:
            raise AnalyticsError()
        results = []
        with self.transaction(owner, "budget.edit") as session:
            self._owner(session, owner)
            for index, entry in enumerate(entries):
                try:
                    with session.begin_nested():
                        try:
                            values = _TRANSLATION_ENTRY_ADAPTER.validate_python(entry).model_dump()
                        except ValidationError:
                            raise AnalyticsError() from None
                        if values["kind"] == "category":
                            saved = self._set_category_translation(
                                session, owner, values["category_id"], values["lang"], values["name"])
                        else:
                            saved = self._set_label_translation(
                                session, owner, values["account_id"], values["entry_reference"],
                                values["lang"], values["label"])
                    results.append({"index": index, "ok": True, "changed": saved["changed"]})
                except AnalyticsError as exc:
                    results.append({"index": index, "ok": False, "error": exc.code})
        return results

    @staticmethod
    def _validate_limit_write(category_id, currency, monthly_limit, warning_percent, enabled, expected_version):
        if (not isinstance(category_id, str) or not 1 <= len(category_id) <= 128
                or not category_id.strip() or category_id == "uncategorised"
                or not isinstance(currency, str) or not _CURRENCY.fullmatch(currency)
                or type(warning_percent) is not int or not 1 <= warning_percent <= 99
                or type(enabled) is not bool or type(expected_version) is not int or expected_version < 0):
            raise AnalyticsError()
        try:
            canonical = _format_scaled(*_parse_amount(monthly_limit, positive=True))
        except SpendingLimitInputError:
            raise AnalyticsError() from None
        return canonical

    def save_spending_limit(self, owner, *, category_id, currency, monthly_limit, warning_percent, enabled,
                            expected_version):
        """Atomically create or update one owner-private monthly category spending limit."""
        canonical = self._validate_limit_write(category_id, currency, monthly_limit, warning_percent,
                                               enabled, expected_version)
        with self.transaction(owner, "budget.edit") as session:
            self._owner(session, owner)
            category = self._owned(session, Category, owner, category_id)
            row = session.scalar(select(CategoryLimitRow).where(
                CategoryLimitRow.owner == owner, CategoryLimitRow.category_id == category_id,
                CategoryLimitRow.currency == currency))
            _version(0 if row is None else row.version, expected_version)
            if category.archived and (row is None or enabled):
                raise AnalyticsError()
            if row is not None and (row.monthly_limit, row.warning_percent, row.enabled) == (
                    canonical, warning_percent, enabled):
                return {**_dict(row, _CATEGORY_LIMIT_FIELDS), "changed": False}
            now = self._now()
            if row is None:
                count = session.scalar(select(func.count()).select_from(CategoryLimitRow).where(
                    CategoryLimitRow.owner == owner))
                if count >= MAX_CATEGORY_LIMITS:
                    raise AnalyticsError("budget_analytics_limit", 413)
                row = CategoryLimitRow(id=str(uuid.uuid4()), owner=owner, category_id=category_id, currency=currency,
                                       monthly_limit=canonical, warning_percent=warning_percent, enabled=enabled,
                                       version=1, created_at=now, updated_at=now)
                session.add(row)
                session.flush()
                result = _dict(row, _CATEGORY_LIMIT_FIELDS)
                self._analytics_audit(session, owner, "category_limit", row.id, "create", None, result)
                return {**result, "changed": True}
            before = _dict(row, _CATEGORY_LIMIT_FIELDS)
            row.monthly_limit, row.warning_percent, row.enabled = canonical, warning_percent, enabled
            row.version += 1
            row.updated_at = now
            session.flush()
            result = _dict(row, _CATEGORY_LIMIT_FIELDS)
            self._analytics_audit(session, owner, "category_limit", row.id, "update", before, result)
            return {**result, "changed": True}

    @staticmethod
    def _mortgage_schedule_dict(row, paid_months):
        return {**_dict(row, _MORTGAGE_SCHEDULE_FIELDS), "paid_months": list(paid_months)}

    @staticmethod
    def _mortgage_audit_payload(row):
        """Strict allowlist: never logs label, amount or any destination target/PII."""
        return {"schedule_id": row.id, "version": row.version, "enabled": row.enabled,
                "destination_kind": row.destination_kind}

    @staticmethod
    def _mortgage_payment_audit(schedule_id, version, month, paid):
        return {"schedule_id": schedule_id, "version": version, "month": month, "paid": paid}

    @staticmethod
    def _mortgage_payment_result(row, month, paid, paid_months, changed):
        return {**BudgetAnalyticsStore._mortgage_payment_audit(row.id, row.version, month, paid),
                "paid_months": paid_months, "changed": changed}

    @staticmethod
    def _mortgage_paid_months(session, owner, schedule_id):
        return list(session.scalars(
            select(MortgagePaymentRow.month).where(
                MortgagePaymentRow.owner == owner, MortgagePaymentRow.schedule_id == schedule_id)
            .order_by(MortgagePaymentRow.month)))

    @staticmethod
    def _validate_mortgage_write(schedule_id, label, amount, currency, due_day, start_date, end_date,
                                 remind_days_before, destination_kind, enabled, expected_version):
        if schedule_id is not None and (
                not isinstance(schedule_id, str) or not 1 <= len(schedule_id) <= 128 or not schedule_id.strip()):
            raise AnalyticsError()
        clean_label = _text(label, limit=256)
        if (not isinstance(currency, str) or not _CURRENCY.fullmatch(currency)
                or type(due_day) is not int or not 1 <= due_day <= 31
                or type(remind_days_before) is not int or not 0 <= remind_days_before <= 31
                or not isinstance(destination_kind, str) or destination_kind not in MORTGAGE_DESTINATIONS
                or type(enabled) is not bool or type(expected_version) is not int or expected_version < 0):
            raise AnalyticsError()
        try:
            canonical_amount = _parse_mortgage_amount(amount)
            start = _parse_mortgage_date(start_date)
            end = None if end_date is None else _parse_mortgage_date(end_date)
        except MortgageInputError:
            raise AnalyticsError() from None
        if end is not None and end < start:
            raise AnalyticsError()
        return clean_label, canonical_amount, start.isoformat(), (None if end is None else end.isoformat())

    def save_mortgage_schedule(self, owner, *, schedule_id=None, label, amount, currency, due_day, start_date,
                               end_date=None, remind_days_before, destination_kind, enabled, expected_version):
        """Atomically create or update one owner-private mortgage reminder schedule (evaluation-only)."""
        clean_label, canonical_amount, start_iso, end_iso = self._validate_mortgage_write(
            schedule_id, label, amount, currency, due_day, start_date, end_date,
            remind_days_before, destination_kind, enabled, expected_version)
        with self.transaction(owner, "budget.edit") as session:
            self._owner(session, owner)
            if schedule_id is None:
                _version(0, expected_version)
                count = session.scalar(select(func.count()).select_from(MortgageScheduleRow).where(
                    MortgageScheduleRow.owner == owner))
                if count >= MAX_MORTGAGE_SCHEDULES:
                    raise AnalyticsError(_LIMIT_ERROR, 413)
                now = self._now()
                row = MortgageScheduleRow(
                    id=str(uuid.uuid4()), owner=owner, label=clean_label, amount=canonical_amount,
                    currency=currency, due_day=due_day, start_date=start_iso, end_date=end_iso,
                    remind_days_before=remind_days_before, destination_kind=destination_kind,
                    enabled=enabled, version=1, created_at=now, updated_at=now,
                )
                session.add(row)
                session.flush()
                self._analytics_audit(session, owner, "mortgage_schedule", row.id, "create",
                                      None, self._mortgage_audit_payload(row))
                return {**self._mortgage_schedule_dict(row, ()), "changed": True}
            row = self._owned(session, MortgageScheduleRow, owner, schedule_id)
            _version(row.version, expected_version)
            paid_months = self._mortgage_paid_months(session, owner, row.id)
            desired = (clean_label, canonical_amount, currency, due_day, start_iso, end_iso,
                       remind_days_before, destination_kind, enabled)
            current = (row.label, row.amount, row.currency, row.due_day, row.start_date, row.end_date,
                       row.remind_days_before, row.destination_kind, row.enabled)
            if current == desired:
                return {**self._mortgage_schedule_dict(row, paid_months), "changed": False}
            before_audit = self._mortgage_audit_payload(row)
            (row.label, row.amount, row.currency, row.due_day, row.start_date, row.end_date,
             row.remind_days_before, row.destination_kind, row.enabled) = desired
            row.version += 1
            row.updated_at = self._now()
            session.flush()
            self._analytics_audit(session, owner, "mortgage_schedule", row.id, "update",
                                  before_audit, self._mortgage_audit_payload(row))
            return {**self._mortgage_schedule_dict(row, paid_months), "changed": True}

    def set_mortgage_payment(self, owner, *, schedule_id, month, paid, expected_version):
        """Atomically mark or unmark one YYYY-MM installment paid, bumping schedule.version once."""
        if (not isinstance(schedule_id, str) or not 1 <= len(schedule_id) <= 128 or not schedule_id.strip()
                or type(paid) is not bool or type(expected_version) is not int or expected_version < 0):
            raise AnalyticsError()
        try:
            clean_month = _parse_mortgage_month(month)
        except MortgageInputError:
            raise AnalyticsError() from None
        with self.transaction(owner, "budget.edit") as session:
            self._owner(session, owner)
            row = self._owned(session, MortgageScheduleRow, owner, schedule_id)
            _version(row.version, expected_version)
            if paid and installment_due_date(row.due_day, row.start_date, row.end_date, clean_month) is None:
                raise AnalyticsError()
            existing = session.get(MortgagePaymentRow, (row.id, clean_month))
            already_paid = existing is not None
            if already_paid == paid:
                return self._mortgage_payment_result(
                    row, clean_month, paid, self._mortgage_paid_months(session, owner, row.id), False)
            now = self._now()
            if paid:
                count = session.scalar(select(func.count()).select_from(MortgagePaymentRow).where(
                    MortgagePaymentRow.owner == owner, MortgagePaymentRow.schedule_id == row.id))
                if count >= MAX_MORTGAGE_PAID_MONTHS:
                    raise AnalyticsError(_LIMIT_ERROR, 413)
                session.add(MortgagePaymentRow(schedule_id=row.id, month=clean_month, owner=owner, marked_at=now))
            else:
                session.delete(existing)
            old_version = row.version
            row.version += 1
            row.updated_at = now
            session.flush()
            self._analytics_audit(
                session, owner, "mortgage_payment", row.id, "mark_paid" if paid else "unmark_paid",
                self._mortgage_payment_audit(row.id, old_version, clean_month, already_paid),
                self._mortgage_payment_audit(row.id, row.version, clean_month, paid),
            )
            return self._mortgage_payment_result(
                row, clean_month, paid, self._mortgage_paid_months(session, owner, row.id), True)

    def annotate(self, owner, account_id, entry_reference, *, mode, category_id, kind, expected_version,
                 expected_fingerprint, label=_KEEP_LABEL):
        if (mode not in ("auto", "manual") or not isinstance(kind, str) or kind not in KINDS
                or (mode == "auto" and (category_id is not None or kind != "auto"))):
            raise AnalyticsError()
        # An omitted label is left alone. Null and blank clear. Failures use a fixed code and never echo the text.
        clean_label = _KEEP_LABEL if label is _KEEP_LABEL else clean_one_off_label(label)
        with self.transaction(owner, "budget.edit") as session:
            self._owner(session, owner)
            _, payload, fingerprint = self._transaction(session, owner, account_id, entry_reference)
            _fingerprint(fingerprint, expected_fingerprint)
            self._category(session, owner, category_id, retain_key=(account_id, entry_reference))
            if not compatible_kind(kind, payload):
                raise AnalyticsError()
            row = session.get(Annotation, (account_id, entry_reference))
            if row is not None and row.owner != owner:
                raise AnalyticsError("budget_analytics_not_found", 404)
            _version(0 if row is None else row.version, expected_version)
            before = None if row is None else _annotation_audit(row)
            next_label = (None if row is None else row.label) if clean_label is _KEEP_LABEL else clean_label
            desired = mode, category_id, kind, fingerprint, next_label
            if row is None and mode == "auto" and next_label is None:
                return {"account_id": account_id, "entry_reference": entry_reference, "mode": "auto",
                        "category_id": None, "kind": "auto", "ledger_fingerprint": fingerprint, "version": 0,
                        "updated_at": None, "label": None, "changed": False}
            if row is not None and (row.mode, row.category_id, row.kind, row.ledger_fingerprint, row.label) == desired:
                return {**_annotation_wire(row), "changed": False}
            if row is None:
                row = Annotation(account_id=account_id, entry_reference=entry_reference, owner=owner, version=0,
                                 label=next_label)
                session.add(row)
            row.mode, row.category_id, row.kind, row.ledger_fingerprint = desired[:4]
            if row.label != next_label:
                session.execute(delete(LabelTranslation).where(
                    LabelTranslation.account_id == account_id,
                    LabelTranslation.entry_reference == entry_reference,
                ))
            row.label = next_label
            row.version += 1
            row.updated_at = self._now()
            session.flush()
            result = _annotation_wire(row)
            self._analytics_audit(session, owner, "annotation", _json([account_id, entry_reference]), "set",
                                  before, _annotation_audit(row))
            return {**result, "changed": True}

    def rules(self, owner):
        key_context = self._rule_key_context(required=False)
        with self._read(owner) as session:
            return [_rule_wire(row, key_context=key_context)
                    for row in session.scalars(select(Rule).where(Rule.owner == owner)
                                              .order_by(Rule.priority, Rule.id))]

    def _counterparty_mask(self, session, owner, key):
        if not isinstance(key, str) or _HASH.fullmatch(key) is None:
            raise AnalyticsError()
        cache_key = "hestia_counterparty_account_snapshot"
        snapshot = session.info.get(cache_key)
        if snapshot is None:
            snapshot = self._snapshot(session, owner)
            session.info[cache_key] = snapshot
        for row in snapshot["transactions"]:
            for evidence_key, masked in row["_counterparty_accounts"]:
                if hmac.compare_digest(evidence_key, key):
                    return masked
        raise AnalyticsError()

    def _rule_candidate(self, session, owner, value, *, allow_stored=False):
        # The contract's value is optional and defaults to empty. A direct caller may omit it.
        if isinstance(value, dict) and "value" not in value:
            value = {**value, "value": ""}
        if not isinstance(value, dict) or set(value) not in (_RULE_FIELDS, _RULE_FIELDS | {"counterparty_account_key"}):
            raise AnalyticsError()
        value = dict(value)
        if (value["field"] not in ("description", "counterparty_name", "counterparty_account", "mcc")
                or value["operator"] not in ("contains", "equals") or not isinstance(value["kind"], str) or value["kind"] not in KINDS
                or type(value["priority"]) is not int or not 0 <= value["priority"] <= 10000
                or type(value["enabled"]) is not bool
                or (value["field"] == "counterparty_account" and value["operator"] != "equals")):
            raise AnalyticsError()
        if value["field"] == "counterparty_account":
            key = value.pop("counterparty_account_key", None)
            raw = value["value"]
            if not isinstance(raw, str) or len(raw) > 256:
                raise AnalyticsError()
            row = None if value["rule_id"] is None else self._owned(session, Rule, owner, value["rule_id"])
            if _same_account_digest(row, key):
                # Same digest keeps the stored kid and mask. The submitted text may only repeat that
                # mask, or be empty. Any other account text is a different account and needs a new key.
                stored_kid, stored_key, stored_mask = counterparty_rule_parts(row.value)
                if raw not in ("", stored_mask):
                    raise AnalyticsError()
                value["value"] = counterparty_rule_value(stored_kid, stored_key, stored_mask)
            elif key is not None:
                if raw.lstrip().startswith("cpacct:"):
                    raise AnalyticsError()
                masked = self._counterparty_mask(session, owner, key)
                _secret, key_id = self._rule_key_context()
                value["value"] = counterparty_rule_value(key_id, key, masked)
            elif raw.lstrip().startswith("cpacct:"):
                if not allow_stored:
                    raise AnalyticsError()
                raw = _text(raw, 256)
                key_id, stored_key, masked = counterparty_rule_parts(raw)
                value["value"] = counterparty_rule_value(key_id, stored_key, masked)
            else:
                raw = _text(raw, 256)
                secret, _key_id = self._rule_key_context()
                value["value"] = counterparty_rule_from_identification(raw, secret, owner=owner)
        else:
            if value.pop("counterparty_account_key", None) is not None:
                raise AnalyticsError()
            value["value"] = _text(value["value"], 256)
            row = None if value["rule_id"] is None else self._owned(session, Rule, owner, value["rule_id"])
        _version(0 if row is None else row.version, value["expected_version"])
        if row is not None and not value["enabled"] and row.category_id == value["category_id"]:
            if value["category_id"] is not None:
                self._owned(session, Category, owner, value["category_id"])
        else:
            self._category(session, owner, value["category_id"])
        return value, row

    def _account_projection(self, session, owner, *, classify=True):
        # Import after module initialization: the read projection subclasses this store.
        from .analytics_views import projection_snapshot
        context = self._rule_key_context(required=False)
        secret, key_id = context if context is not None else (None, None)
        return projection_snapshot(session, owner, classify=classify, account_secret=secret, account_key_id=key_id)

    def _snapshot(self, session, owner):
        return self._account_projection(session, owner)

    @staticmethod
    def _proposed(rows, rules, *, watch=None, account_secret=None, account_key_id=None, owner=None):
        """Count rows whose rule result changes. ``watch`` is the candidate subset: only rows that
        already use one of those rules, or whose text can match one, are evaluated."""
        compiled, safe = compile_rules(rules)
        watch_compiled, replaced = None, set()
        if watch is not None and safe:
            watch_compiled, watch_safe = compile_rules(watch)
            if watch_safe:
                replaced = {rule["id"] for rule in watch}
            else:
                watch_compiled = None
        affected, examples = 0, []
        for row in rows:
            if row["annotation"]["mode"] == "manual" or row["effective"].get("source") == "transfer":
                continue
            texts = row.get("_match_texts")
            if texts is None:
                texts = match_texts(row["payload"], row["metadata"], account_secret=account_secret,
                                    account_key_id=account_key_id, owner=owner)
            if watch_compiled is not None:
                current_id = row.get("base_effective", row["effective"]).get("rule_id")
                if current_id not in replaced and first_matching_rule(watch_compiled, row["payload"], texts) is None:
                    continue
            default_kind = row.get("_default_kind")
            if default_kind is None:
                default_kind = default_effective_kind(row["payload"], row["metadata"])
            rule, kind = match_rule(compiled, row["payload"], texts, default_kind=default_kind)
            effective = {
                "kind": kind,
                "category_id": None if rule is None else rule["category_id"],
                "rule_id": None if rule is None else rule["id"],
            }
            current = row.get("base_effective", row["effective"])
            if any(current.get(key) != effective[key] for key in effective):
                affected += 1
                if len(examples) < 20:
                    examples.append({"account_id": row["account_id"], "entry_reference": row["entry_reference"]})
        return affected, examples

    @staticmethod
    def _merged_rules(existing, proposed):
        proposed_ids = {rule["id"] for rule in proposed}
        rules = [rule for rule in existing if rule["id"] not in proposed_ids]
        rules.extend(proposed)
        rules.sort(key=lambda row: (row["priority"], row["id"]))
        return rules

    def _reserve_preview(self, session, owner, now):
        session.execute(delete(Preview).where(Preview.owner == owner, Preview.saved_rule_id.is_(None),
                                              Preview.expires_at <= now))
        count = session.scalar(select(func.count()).select_from(Preview).where(Preview.owner == owner,
                                                                                Preview.saved_rule_id.is_(None)))
        if count >= 100:
            raise AnalyticsError("budget_analytics_limit", 413)

    def _stage_candidates(self, session, owner, candidates):
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= MAX_BATCH_RULES:
            raise AnalyticsError()
        seen, staged = set(), []
        for candidate in candidates:
            candidate, row = self._rule_candidate(session, owner, candidate)
            if candidate["rule_id"] is not None:
                if candidate["rule_id"] in seen:
                    raise AnalyticsError()
                seen.add(candidate["rule_id"])
            candidate = dict(candidate)
            candidate["_proposed_id"] = candidate["rule_id"] or str(uuid.uuid4())
            staged.append(candidate)
        return staged

    def _preview_candidates(self, session, owner, staged):
        now = self._now()
        self._reserve_preview(session, owner, now)
        snapshot = self._snapshot(session, owner)
        proposed = [{**candidate, "id": candidate["_proposed_id"]} for candidate in staged]
        rules = self._merged_rules(snapshot["rules"], proposed)
        context = self._rule_key_context(required=False)
        secret, key_id = context if context is not None else (None, None)
        affected, examples = self._proposed(snapshot["transactions"], rules, watch=proposed,
                                            account_secret=secret, account_key_id=key_id, owner=owner)
        return snapshot, affected, examples, now

    def preview_rule(self, owner, candidate):
        with self.transaction(owner, "budget.edit") as session:
            self._owner(session, owner)
            staged = self._stage_candidates(session, owner, [candidate])
            snapshot, affected, examples, now = self._preview_candidates(session, owner, staged)
            preview = Preview(id=str(uuid.uuid4()), owner=owner, candidate_json=_json(staged[0]),
                              snapshot_digest=snapshot["digest"], created_at=now, expires_at=now + 900)
            session.add(preview)
            return {"preview_id": preview.id, "expires_at": preview.expires_at,
                    "affected_count": affected, "examples": examples}

    def preview_rules(self, owner, candidates):
        """One snapshot and one digest for up to 100 rules. Nothing is stored if any candidate is invalid."""
        with self.transaction(owner, "budget.edit") as session:
            self._owner(session, owner)
            staged = self._stage_candidates(session, owner, candidates)
            snapshot, affected, examples, now = self._preview_candidates(session, owner, staged)
            preview = Preview(id=str(uuid.uuid4()), owner=owner,
                              candidate_json=_json({"_batch": True, "candidates": staged}),
                              snapshot_digest=snapshot["digest"], created_at=now, expires_at=now + 900)
            session.add(preview)
            return {"preview_id": preview.id, "expires_at": preview.expires_at,
                    "affected_count": affected, "examples": examples, "rule_count": len(staged)}

    def _snapshot_digest(self, session, owner):
        preview = self._account_projection(session, owner, classify=False)
        _, safe = compile_rules(preview["rules"])
        if safe:
            return preview["digest"]
        return self._account_projection(session, owner)["digest"]

    def _apply_candidate(self, session, owner, candidate):
        proposed_id = candidate.pop("_proposed_id", None)
        if not isinstance(proposed_id, str) or not proposed_id:
            raise AnalyticsError()
        candidate, row = self._rule_candidate(session, owner, candidate, allow_stored=True)
        before = None if row is None else _dict(row, _RULE_OUTPUT)
        fields = ("field", "operator", "value", "category_id", "kind", "priority", "enabled")
        changed = row is None or any(getattr(row, key) != candidate[key] for key in fields)
        if row is None:
            if session.scalar(select(func.count()).select_from(Rule).where(Rule.owner == owner)) >= MAX_RULES:
                raise AnalyticsError("budget_analytics_limit", 413)
            now = self._now()
            row = Rule(id=proposed_id, owner=owner, version=0, created_at=now)
            session.add(row)
        if changed:
            for key in fields:
                setattr(row, key, candidate[key])
            row.version += 1
            row.updated_at = self._now()
        session.flush()
        result = _dict(row, _RULE_OUTPUT)
        if changed:
            self._analytics_audit(session, owner, "rule", row.id, "save", before, result)
        return {**_rule_wire(row), "changed": changed}

    def _saved_batch(self, session, owner, payload):
        rules = []
        for candidate in payload["candidates"]:
            proposed_id = candidate.get("_proposed_id") if isinstance(candidate, dict) else None
            if not isinstance(proposed_id, str) or not proposed_id:
                raise AnalyticsError()
            row = self._owned(session, Rule, owner, proposed_id)
            rules.append({**_rule_wire(row), "changed": False})
        return {"rules": rules, "changed": False}

    def save_rule(self, owner, preview_id):
        with self.transaction(owner, "budget.edit") as session:
            self._owner(session, owner)
            preview = self._owned(session, Preview, owner, preview_id)
            if preview.saved_rule_id is not None:
                # A saved batch is not a single-rule replay. Corrupt JSON on an ordinary
                # saved preview still replays: only a well-formed batch object is rejected.
                try:
                    saved = json.loads(preview.candidate_json)
                except json.JSONDecodeError:
                    saved = None
                if isinstance(saved, dict) and saved.get("_batch"):
                    raise AnalyticsError()
                row = self._owned(session, Rule, owner, preview.saved_rule_id)
                return {**_rule_wire(row), "changed": False}
            if preview.expires_at <= self._now():
                raise AnalyticsError("budget_analytics_preview_expired", 409)
            if preview.snapshot_digest != self._snapshot_digest(session, owner):
                raise AnalyticsError("budget_analytics_stale_preview", 409)
            candidate = json.loads(preview.candidate_json)
            if not isinstance(candidate, dict) or candidate.get("_batch"):
                raise AnalyticsError()
            result = self._apply_candidate(session, owner, candidate)
            preview.saved_rule_id, preview.saved_version = result["id"], result["version"]
            return result

    def save_rules(self, owner, preview_id):
        """Apply every candidate in one transaction or leave the rule set unchanged."""
        with self.transaction(owner, "budget.edit") as session:
            self._owner(session, owner)
            preview = self._owned(session, Preview, owner, preview_id)
            payload = json.loads(preview.candidate_json)
            candidates = payload.get("candidates") if isinstance(payload, dict) else None
            if (not isinstance(payload, dict) or not payload.get("_batch") or not isinstance(candidates, list)
                    or not 1 <= len(candidates) <= MAX_BATCH_RULES):
                raise AnalyticsError()
            if preview.saved_rule_id is not None:
                return self._saved_batch(session, owner, payload)
            if preview.expires_at <= self._now():
                raise AnalyticsError("budget_analytics_preview_expired", 409)
            if preview.snapshot_digest != self._snapshot_digest(session, owner):
                raise AnalyticsError("budget_analytics_stale_preview", 409)
            # Reject a tampered candidate before the cap or any write. A missing id used to
            # escape as KeyError and a 500.
            checked, creating = [], 0
            for candidate in payload["candidates"]:
                proposed_id = candidate.get("_proposed_id") if isinstance(candidate, dict) else None
                if not isinstance(proposed_id, str) or not proposed_id:
                    raise AnalyticsError()
                creating += candidate.get("rule_id") is None
                checked.append(dict(candidate))
            existing = session.scalar(select(func.count()).select_from(Rule).where(Rule.owner == owner))
            if existing + creating > MAX_RULES:
                raise AnalyticsError("budget_analytics_limit", 413)
            # Re-validate every candidate before the first write so a later conflict cannot land.
            for candidate in checked:
                probe = dict(candidate)
                probe.pop("_proposed_id", None)
                self._rule_candidate(session, owner, probe, allow_stored=True)
            results = [self._apply_candidate(session, owner, candidate) for candidate in checked]
            preview.saved_rule_id, preview.saved_version = results[0]["id"], results[0]["version"]
            return {"rules": results, "changed": any(row["changed"] for row in results)}

    @staticmethod
    def _evidence(session, owner, row, payload, fingerprint):
        metadata = session.get(Metadata, (row.account_id, row.entry_reference))
        data = (json.loads(metadata.payload_json) if metadata is not None and metadata.owner == owner
                and metadata.ledger_fingerprint == fingerprint else None)
        accounts = session.scalars(select(Account.id).join(Connection).where(Account.owner == owner, Connection.owner == owner))
        owned = {account: [] for account in accounts}
        identities = session.execute(select(Identity).join(Account).join(Connection).where(
            Identity.owner == owner, Account.owner == owner, Connection.owner == owner))
        owned.update({identity.account_id: json.loads(identity.identities_json) for identity, in identities})
        return pair_fingerprint(row.account_id, row.entry_reference, payload, data, owned), data, owned

    @staticmethod
    def _verify_source(owner, debit_row, debit_payload, debit_meta, credit_row, credit_payload, credit_meta, owned, source):
        from .transfer_match import TransferEntry, TransferInputError, TransferMatchLimitError, match_transfers
        entries = []
        for row, payload, metadata in ((debit_row, debit_payload, debit_meta), (credit_row, credit_payload, credit_meta)):
            field = "creditor_accounts" if payload["credit_debit_indicator"] == "DBIT" else "debtor_accounts"
            counterparties = () if metadata is None else tuple((value["scheme"], value["identification"])
                                                              for value in metadata.get(field, []))
            entries.append(TransferEntry(owner=owner, account_id=row.account_id, entry_reference=row.entry_reference,
                           amount=payload["amount"], currency=payload["currency"], direction=payload["credit_debit_indicator"],
                           date=effective_date(payload), fingerprint=pair_fingerprint(row.account_id, row.entry_reference,
                           payload, metadata, owned), counterparty_accounts=counterparties))
        identities = {account: tuple((value["scheme"], value["identification"]) for value in values)
                      for account, values in owned.items()}
        try:
            candidates = match_transfers(owner, entries, identities)
        except (TransferInputError, TransferMatchLimitError):
            raise AnalyticsError() from None
        if len(candidates) != 1 or candidates[0].source != source:
            raise AnalyticsError()

    def decide_transfer(self, owner, *, decision_id=None, debit=None, credit=None, action, expected_version,
                        expected_debit_fingerprint=None, expected_credit_fingerprint=None, source="manual"):
        if action not in ("accept", "reject", "undo") or source not in ("account_identity", "heuristic", "manual"):
            raise AnalyticsError()
        with self.transaction(owner, "budget.edit") as session:
            self._owner(session, owner)
            row = None if decision_id is None else self._owned(session, Decision, owner, decision_id)
            if row is None:
                if not isinstance(debit, dict) or not isinstance(credit, dict) or any(
                        set(leg) != {"account_id", "entry_reference"} for leg in (debit, credit)):
                    raise AnalyticsError()
                row = session.scalar(select(Decision).where(Decision.owner == owner,
                    Decision.debit_account_id == debit["account_id"], Decision.debit_ref == debit["entry_reference"],
                    Decision.credit_account_id == credit["account_id"], Decision.credit_ref == credit["entry_reference"]))
            else:
                stored = ({"account_id": row.debit_account_id, "entry_reference": row.debit_ref},
                          {"account_id": row.credit_account_id, "entry_reference": row.credit_ref})
                if (debit is not None and debit != stored[0]) or (credit is not None and credit != stored[1]):
                    raise AnalyticsError("budget_analytics_conflict", 409)
                debit, credit = stored
            _version(0 if row is None else row.version, expected_version)
            if action == "undo" and row is None:
                raise AnalyticsError("budget_analytics_not_found", 404)
            debit_row, debit_payload, debit_financial = self._transaction(session, owner, debit["account_id"], debit["entry_reference"], selected=action != "undo")
            credit_row, credit_payload, credit_financial = self._transaction(session, owner, credit["account_id"], credit["entry_reference"], selected=action != "undo")
            if action != "undo":
                debit_fp, debit_meta, owned = self._evidence(session, owner, debit_row, debit_payload, debit_financial)
                credit_fp, credit_meta, _ = self._evidence(session, owner, credit_row, credit_payload, credit_financial)
                _fingerprint(debit_fp, expected_debit_fingerprint)
                _fingerprint(credit_fp, expected_credit_fingerprint)
                if (debit_row.account_id == credit_row.account_id or debit_payload["credit_debit_indicator"] != "DBIT"
                        or credit_payload["credit_debit_indicator"] != "CRDT"
                        or compare_money(debit_payload["amount"], "0") <= 0
                        or compare_money(debit_payload["amount"], credit_payload["amount"]) != 0
                        or debit_payload["currency"] != credit_payload["currency"]
                        or abs((date.fromisoformat(effective_date(debit_payload)) - date.fromisoformat(effective_date(credit_payload))).days) > 3):
                    raise AnalyticsError()
                if source != "manual":
                    self._verify_source(owner, debit_row, debit_payload, debit_meta, credit_row, credit_payload, credit_meta, owned, source)
            else:
                debit_fp, credit_fp = row.debit_fingerprint, row.credit_fingerprint
                source = row.source
            if action == "accept":
                legs = (
                    (debit_row.account_id, debit_row.entry_reference),
                    (credit_row.account_id, credit_row.entry_reference),
                )
                # Both group statuses reserve the transaction.
                reservation = select(ReceiptLinkGroup.id).where(
                    ReceiptLinkGroup.owner == owner,
                    tuple_(ReceiptLinkGroup.account_id, ReceiptLinkGroup.entry_reference).in_(legs),
                ).limit(1)
                if session.scalar(reservation) is not None:
                    raise AnalyticsError("budget_analytics_leg_reserved", 409)
            before = None if row is None else _dict(row, _DECISION_FIELDS)
            status = {"accept": "accepted", "reject": "rejected", "undo": "undone"}[action]
            if row is not None and (row.status, row.debit_fingerprint, row.credit_fingerprint, row.source) == (status, debit_fp, credit_fp, source):
                return {**before, "changed": False}
            if row is None:
                now = self._now()
                row = Decision(id=str(uuid.uuid4()), owner=owner, debit_account_id=debit_row.account_id,
                      debit_ref=debit_row.entry_reference, credit_account_id=credit_row.account_id,
                      credit_ref=credit_row.entry_reference, source=source, rule_version=1, version=0,
                      status=status, debit_fingerprint=debit_fp, credit_fingerprint=credit_fp,
                      created_at=now, updated_at=now)
                session.add(row)
                session.flush()
            session.execute(delete(Leg).where(Leg.decision_id == row.id))
            if status == "accepted":
                for leg in (debit_row, credit_row):
                    prior = session.get(Leg, (leg.account_id, leg.entry_reference))
                    if prior is not None:
                        raise AnalyticsError("budget_analytics_leg_reserved", 409)
                    session.add(Leg(account_id=leg.account_id, entry_reference=leg.entry_reference, decision_id=row.id))
            row.status, row.source, row.debit_fingerprint, row.credit_fingerprint = status, source, debit_fp, credit_fp
            row.version += 1
            row.updated_at = self._now()
            session.flush()
            reconcile_receipt_links(session, owner, row.updated_at)
            result = _dict(row, _DECISION_FIELDS)
            self._analytics_audit(session, owner, "transfer", row.id, action, before, result)
            return {**result, "changed": True}
