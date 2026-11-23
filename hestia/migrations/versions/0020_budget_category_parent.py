"""Nullable parent on owner budget categories so spending can roll up a tree.

Revision ID: 0020
Revises: 0019

#248 merges before #238, so this revision is 0020. Open PRs #238 and #239
re-parent onto it when they land.
Depth, cycles and "archive only after children move" are enforced in the store;
the database keeps the self-link, forbids a category parenting itself, and
restricts deletes of a category that still has children.
"""
from alembic import op
import sqlalchemy as sa

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

_TABLE = "budget_categories"
_FK = "fk_budget_categories_parent_id"
_CHECK = "ck_budget_categories_parent_not_self"
_INDEX = "ix_budget_categories_parent_id"


def _drop_leftover_batch_table(table: str) -> None:
    """Drop Alembic's batch-copy table if an aborted rebuild left it behind.

    ``batch_alter_table`` copies into ``_alembic_tmp_<table>`` (truncated to 50
    characters, matching Alembic). SQLite can autocommit that CREATE when no
    explicit transaction is open, so a failed DROP leaves the empty temp table
    in place and the next attempt dies with "table already exists". Call this
    before every batch rebuild; future batch migrations should do the same.
    """
    temp_name = ("_alembic_tmp_%s" % table)[:50]
    op.execute(f'DROP TABLE IF EXISTS "{temp_name}"')


def upgrade():
    _drop_leftover_batch_table(_TABLE)
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(sa.Column("parent_id", sa.String(), nullable=True))
        batch.create_foreign_key(_FK, _TABLE, ["parent_id"], ["id"])
        batch.create_check_constraint(_CHECK, "parent_id IS NULL OR parent_id != id")
        batch.create_index(_INDEX, ["parent_id"])


def downgrade():
    # Take a SQLite backup before downgrading: dropping parent_id forgets which
    # category sat under which, while the category rows themselves stay.
    _drop_leftover_batch_table(_TABLE)
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_index(_INDEX)
        batch.drop_constraint(_CHECK, type_="check")
        batch.drop_constraint(_FK, type_="foreignkey")
        batch.drop_column("parent_id")
