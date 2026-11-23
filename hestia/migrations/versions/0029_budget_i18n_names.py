"""Budget name and label translations, plus per-user display languages.

Revision ID: 0029
Revises: 0028

Downgrading removes translations and language preferences. Canonical names,
labels, and other settings remain available.
"""
from alembic import op
import sqlalchemy as sa

from hestia.db import annotation_label_check_sql

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

_CATEGORY_TRANSLATIONS = "budget_category_translations"
_LABEL_TRANSLATIONS = "budget_annotation_label_translations"
_CATEGORY_OWNER_INDEX = "uq_budget_categories_id_owner"
_CATEGORY_NAME_TRIGGERS = (
    ("trg_budget_categories_name_insert", """
        CREATE TRIGGER trg_budget_categories_name_insert
        BEFORE INSERT ON budget_categories
        WHEN EXISTS (
            SELECT 1 FROM budget_category_translations AS translated
            WHERE translated.owner = NEW.owner AND translated.name_key = NEW.name_key
              AND translated.category_id <> NEW.id
        )
        BEGIN
            SELECT RAISE(ABORT, 'budget_analytics_conflict');
        END
    """),
    ("trg_budget_categories_name_update", """
        CREATE TRIGGER trg_budget_categories_name_update
        BEFORE UPDATE OF owner, name_key ON budget_categories
        WHEN EXISTS (
            SELECT 1 FROM budget_category_translations AS translated
            WHERE translated.owner = NEW.owner AND translated.name_key = NEW.name_key
              AND translated.category_id <> NEW.id
        )
        BEGIN
            SELECT RAISE(ABORT, 'budget_analytics_conflict');
        END
    """),
    ("trg_budget_category_translations_name_insert", """
        CREATE TRIGGER trg_budget_category_translations_name_insert
        BEFORE INSERT ON budget_category_translations
        WHEN EXISTS (
            SELECT 1 FROM budget_categories AS category
            WHERE category.owner = NEW.owner AND category.name_key = NEW.name_key
              AND category.id <> NEW.category_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'budget_analytics_conflict');
        END
    """),
    ("trg_budget_category_translations_name_update", """
        CREATE TRIGGER trg_budget_category_translations_name_update
        BEFORE UPDATE OF owner, name_key, category_id ON budget_category_translations
        WHEN EXISTS (
            SELECT 1 FROM budget_categories AS category
            WHERE category.owner = NEW.owner AND category.name_key = NEW.name_key
              AND category.id <> NEW.category_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'budget_analytics_conflict');
        END
    """),
)


def upgrade():
    op.add_column("user_settings", sa.Column("preferred_languages", sa.JSON(), nullable=True))
    op.create_index(_CATEGORY_OWNER_INDEX, "budget_categories", ["id", "owner"], unique=True)
    op.create_table(
        _CATEGORY_TRANSLATIONS,
        sa.Column("category_id", sa.String(), primary_key=True),
        sa.Column("lang", sa.String(), primary_key=True),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("name_key", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["category_id", "owner"], ["budget_categories.id", "budget_categories.owner"],
            name="fk_budget_category_translations_category_owner", ondelete="CASCADE",
        ),
        sa.UniqueConstraint("owner", "lang", "name_key", name="uq_budget_category_translations_name"),
    )
    op.create_table(
        _LABEL_TRANSLATIONS,
        sa.Column("account_id", sa.String(), primary_key=True),
        sa.Column("entry_reference", sa.String(), primary_key=True),
        sa.Column("lang", sa.String(), primary_key=True),
        sa.Column("label", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id", "entry_reference"], ["budget_annotations.account_id", "budget_annotations.entry_reference"],
            name="fk_budget_annotation_label_translations_annotation", ondelete="CASCADE",
        ),
        sa.CheckConstraint(annotation_label_check_sql(), name="ck_budget_annotation_label_translations_label"),
    )
    for _name, statement in _CATEGORY_NAME_TRIGGERS:
        op.execute(statement)


def downgrade():
    for name, _statement in _CATEGORY_NAME_TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.drop_table(_LABEL_TRANSLATIONS)
    op.drop_table(_CATEGORY_TRANSLATIONS)
    op.drop_index(_CATEGORY_OWNER_INDEX, table_name="budget_categories")
    op.drop_column("user_settings", "preferred_languages")
