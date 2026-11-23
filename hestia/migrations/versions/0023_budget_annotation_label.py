"""One-off free-text label on a budget annotation.

Revision ID: 0023
Revises: 0019

down_revision is 0019, the current master head. Re-parent this revision after
0020, 0021 and 0022 at merge: those numbers are held by other pull requests.
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

revision = "0023"
down_revision = "0019"
branch_labels = None
depends_on = None

_LABEL_CHECK = annotation_label_check_sql()


def upgrade():
    with op.batch_alter_table("budget_annotations") as batch:
        batch.add_column(sa.Column("label", sa.Text(), nullable=True))
        batch.create_check_constraint("ck_budget_annotations_label", _LABEL_CHECK)


def downgrade():
    with op.batch_alter_table("budget_annotations") as batch:
        batch.drop_constraint("ck_budget_annotations_label", type_="check")
        batch.drop_column("label")
