"""One-off free-text label on a budget annotation.

Revision ID: 0021
Revises: 0020

No other table references ``budget_annotations``. The row itself references
``budget_transactions``, ``users`` and ``budget_categories``. ``batch_alter_table``
rebuilds this table, so upgrade and downgrade drop a leftover
``_alembic_tmp_budget_annotations`` first. ``init_db`` and ``downgrade_db`` run
that rebuild with foreign keys off, inside an explicit transaction, then
``PRAGMA foreign_key_check``.

The label is owner-private display text. It is nullable, at most 80 characters,
and stored without leading or trailing spaces. The CHECK rejects C0 controls
and DEL, including newline, carriage return and tab. The application also
rejects format characters before insert.

Downgrade drops the column. Take a SQLite backup first: saved one-off labels
are removed; the rest of each annotation row stays.
"""
from alembic import op
import sqlalchemy as sa

from hestia.db import annotation_label_check_sql

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

_TABLE = "budget_annotations"
_LABEL_CHECK = annotation_label_check_sql()


def _drop_leftover_batch_table(table: str) -> None:
    """Drop Alembic's batch-copy table if an aborted rebuild left it behind.

    ``batch_alter_table`` copies into ``_alembic_tmp_<table>`` (truncated to 50
    characters, matching Alembic). A failed rebuild outside a transaction can
    leave that empty table in place. Call this before every batch rebuild.
    """
    temp_name = ("_alembic_tmp_%s" % table)[:50]
    op.execute(f'DROP TABLE IF EXISTS "{temp_name}"')


def upgrade():
    _drop_leftover_batch_table(_TABLE)
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(sa.Column("label", sa.Text(), nullable=True))
        batch.create_check_constraint("ck_budget_annotations_label", _LABEL_CHECK)


def downgrade():
    _drop_leftover_batch_table(_TABLE)
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_constraint("ck_budget_annotations_label", type_="check")
        batch.drop_column("label")
