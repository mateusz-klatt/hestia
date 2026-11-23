"""Budget name and label translations, plus per-user display languages.

Revision ID: 0026
Revises: 0023

Downgrading removes translations and language preferences. Canonical names,
labels, and other settings remain available.
"""
from alembic import op
import sqlalchemy as sa

from hestia.db import annotation_label_check_sql

revision = "0026"
down_revision = "0023"
branch_labels = None
depends_on = None

_CATEGORY_TRANSLATIONS = "budget_category_translations"
_LABEL_TRANSLATIONS = "budget_annotation_label_translations"
_CATEGORY_OWNER_INDEX = "uq_budget_categories_id_owner"


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


def downgrade():
    op.drop_table(_LABEL_TRANSLATIONS)
    op.drop_table(_CATEGORY_TRANSLATIONS)
    op.drop_index(_CATEGORY_OWNER_INDEX, table_name="budget_categories")
    op.drop_column("user_settings", "preferred_languages")
