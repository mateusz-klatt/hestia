"""Owner-private live inbox read markers and monotonic CAS revision (Budget 2D, Variant D).

Revision ID: 0019
Revises: 0018
"""
from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

_STATE = "budget_owner_inbox_state"
_READS = "budget_owner_inbox_reads"
_IX_READS_OWNER = "ix_budget_owner_inbox_reads_owner"


def upgrade():
    op.create_table(
        _STATE,
        sa.Column("owner", sa.String(), primary_key=True),
        sa.Column("reads_revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["owner"], ["users.username"]),
        sa.CheckConstraint("reads_revision >= 0", name="ck_budget_owner_inbox_state_reads_revision"),
        sa.CheckConstraint("updated_at >= 0", name="ck_budget_owner_inbox_state_updated_at"),
    )

    op.create_table(
        _READS,
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("subject_id", sa.String(), nullable=False),
        sa.Column("milestone", sa.String(), nullable=False),
        sa.Column("read_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("owner", "kind", "subject_id", "milestone"),
        sa.ForeignKeyConstraint(["owner"], ["users.username"]),
        sa.CheckConstraint(
            "kind IN ('consent_renewal', 'mortgage_due', 'spending_limit')",
            name="ck_budget_owner_inbox_reads_kind",
        ),
        sa.CheckConstraint(
            "length(subject_id) = 64 AND subject_id NOT GLOB '*[^0-9a-f]*'",
            name="ck_budget_owner_inbox_reads_subject_id",
        ),
        sa.CheckConstraint(
            "length(milestone) >= 1 AND length(milestone) <= 128",
            name="ck_budget_owner_inbox_reads_milestone",
        ),
        sa.CheckConstraint("read_at >= 0", name="ck_budget_owner_inbox_reads_read_at"),
    )
    op.create_index(_IX_READS_OWNER, _READS, ["owner"])


def downgrade():
    # Take a SQLite backup before downgrading: dropping these tables deletes saved owner
    # inbox read markers and monotonic reads_revision while keeping sanitized
    # budget_analytics_audit rows intact.
    op.drop_index(_IX_READS_OWNER, table_name=_READS)
    op.drop_table(_READS)
    op.drop_table(_STATE)
