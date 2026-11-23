"""Allow private device alerts to be marked read in the owner inbox.

Revision ID: 0028
Revises: 0027
"""
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

_READS = "budget_owner_inbox_reads"
_KIND = "ck_budget_owner_inbox_reads_kind"
_PREVIOUS_KINDS = "'consent_renewal', 'mortgage_due', 'spending_limit'"


def _replace_kind_constraint(kinds):
    with op.batch_alter_table(_READS) as batch:
        batch.drop_constraint(_KIND, type_="check")
        batch.create_check_constraint(_KIND, f"kind IN ({kinds})")


def upgrade():
    _replace_kind_constraint(_PREVIOUS_KINDS + ", 'device_added'")


def downgrade():
    op.execute("DELETE FROM budget_owner_inbox_reads WHERE kind = 'device_added'")
    _replace_kind_constraint(_PREVIOUS_KINDS)
