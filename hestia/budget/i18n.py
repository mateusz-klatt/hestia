"""Budget display translations and ordered per-viewer language preferences."""
from __future__ import annotations

from sqlalchemy import select

from .. import db

LANGUAGES = (
    "ar", "bn", "bs", "cs", "da", "de", "el", "en", "es", "fa", "fi", "fil", "fr", "ga", "he",
    "hi", "hr", "hu", "hy", "id", "is", "it", "ja", "ko", "lt", "lv", "ms", "my-MM", "nl", "no",
    "pl", "pt", "ro", "ru", "sk", "sq", "sr", "sv", "sw", "th", "tr", "uk", "vi", "zh", "zh-Hant",
)
_LANGUAGE_SET = frozenset(LANGUAGES)


def validate_languages(value):
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > len(LANGUAGES):
        raise ValueError("preferred_languages must be an ordered list of supported languages")
    if any(not isinstance(lang, str) or lang not in _LANGUAGE_SET for lang in value):
        raise ValueError("preferred_languages contains an unsupported language")
    if len(set(value)) != len(value):
        raise ValueError("preferred_languages must not contain duplicates")
    return list(value)


def preferred_languages(row):
    if row is None:
        return ["en"]
    stored = row.preferred_languages
    if stored is None:
        return [row.locale] if row.locale in _LANGUAGE_SET else ["en"]
    try:
        return validate_languages(stored)
    except ValueError:
        return ["en"]


def translated(canonical, translations, preferences):
    for lang in preferences:
        value = translations.get(lang)
        if value is not None:
            return value
    return canonical


class DisplayResolver:
    def __init__(self, session, owner, viewer):
        self.preferences = preferred_languages(session.get(db.UserSetting, viewer))
        self.category_ids = set()
        self.category_names = {}
        for row in session.scalars(select(db.BudgetCategory).where(db.BudgetCategory.owner == owner)):
            self.category_ids.add(row.id)
            self.category_names[row.name] = row.id
        self.categories = {}
        for row in session.scalars(select(db.BudgetCategoryTranslation).where(
                db.BudgetCategoryTranslation.owner == owner)):
            self.categories.setdefault(row.category_id, {})[row.lang] = row.name
        self.labels = {}
        self.label_variants = {}
        label = db.BudgetAnnotationLabelTranslation
        annotation = db.BudgetAnnotation
        rows = session.execute(select(label, annotation.label).join(
            annotation, (label.account_id == annotation.account_id)
            & (label.entry_reference == annotation.entry_reference),
        ).where(annotation.owner == owner))
        for row, canonical in rows:
            key = (row.account_id, row.entry_reference)
            self.labels.setdefault(key, {})[row.lang] = row.label
            self.label_variants.setdefault(canonical, {})[key] = self.labels[key]

    def category(self, identifier, canonical):
        return translated(canonical, self.categories.get(identifier, {}), self.preferences)

    def label(self, key, canonical):
        return translated(canonical, self.labels.get(key, {}), self.preferences)

    def grouped_label(self, canonical):
        variants = self.label_variants.get(canonical, {})
        for lang in self.preferences:
            values = sorted({names[lang] for _key, names in sorted(variants.items()) if lang in names})
            if values:
                return values[0]
        return canonical

    def decorate(self, payload, *, include_category_translations=False):
        def visit(node, parent=None):
            if isinstance(node, list):
                return [visit(item, parent) for item in node]
            if not isinstance(node, dict):
                return node
            result = {key: visit(value, node) for key, value in node.items()}
            if node.get("source") == "category_baseline" and "label" in node:
                result["display_name"] = self.category(self.category_names.get(node["label"]), node["label"])
            identifier = node.get("category_id")
            category_row = node.get("id") in self.category_ids
            if identifier is None and category_row:
                identifier = node["id"]
            if "name" in node and node.get("one_off"):
                result["display_label"] = self.grouped_label(node["name"])
            elif "name" in node and ("category_id" in node or category_row):
                result["display_name"] = self.category(identifier, node["name"])
                if include_category_translations and category_row:
                    result["translations"] = dict(self.categories.get(identifier, {}))
            context = node if "account_id" in node else (parent or {})
            if "label" in node and "account_id" in context and "entry_reference" in context:
                key = (context["account_id"], context["entry_reference"])
                result["display_label"] = self.label(key, node["label"])
            return result

        return visit(payload)
