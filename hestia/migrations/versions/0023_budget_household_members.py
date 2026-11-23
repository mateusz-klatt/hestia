"""Household viewer grants (Budget Month P3).

Revision ID: 0023
Revises: 0022

Creates ``budget_household_members`` only. It does not batch-alter a table
other tables reference. It also indexes the household audit lookup. Downgrade
drops that index and this table and returns to 0022. Receipt rows, category
parents, one-off labels, the owner ledger, limits, mortgages, inbox, consent
and acquisition rows stay in place.
"""
from alembic import op
import sqlalchemy as sa

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

_TABLE = "budget_household_members"
_IX_OWNER = "ix_budget_household_members_owner"
_IX_AUDIT = "ix_budget_analytics_audit_subject_action_at"


def upgrade():
    op.create_table(
        _TABLE,
        sa.Column("member_username", sa.String(), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("grant_id", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("member_username"),
        sa.ForeignKeyConstraint(
            ["member_username"], ["users.username"],
            name="fk_budget_household_members_member",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner"], ["users.username"],
            name="fk_budget_household_members_owner",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("grant_id", name="uq_budget_household_members_grant_id"),
        sa.CheckConstraint("member_username <> owner", name="ck_budget_household_members_distinct"),
        sa.CheckConstraint(
            "typeof(grant_id) = 'text' AND length(grant_id) = 64 AND grant_id NOT GLOB '*[^0-9a-f]*' "
            "AND instr(grant_id, char(0)) = 0",
            name="ck_budget_household_members_grant_id",
        ),
    )
    op.create_index(_IX_OWNER, _TABLE, ["owner"])
    op.create_index(_IX_AUDIT, "budget_analytics_audit", ["subject_id", "action", "changed_at"])


def downgrade():
    # Take a SQLite backup before downgrading. This drops household grants only.
    # Reapplying 0023 starts from an empty grant table; grants are not restored.
    op.drop_index(_IX_AUDIT, table_name="budget_analytics_audit")
    op.drop_index(_IX_OWNER, table_name=_TABLE)
    op.drop_table(_TABLE)
