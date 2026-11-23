"""Household viewer grants (Budget Month P3).

Revision ID: 0022
Revises: 0021

Creates ``budget_household_members`` only. It does not batch-alter a table
other tables reference. Downgrade drops this table and returns to 0021.
Category parents, one-off labels, the owner ledger, limits, mortgages, inbox,
consent and acquisition rows stay in place.
"""
from alembic import op
import sqlalchemy as sa

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

_TABLE = "budget_household_members"
_IX_OWNER = "ix_budget_household_members_owner"


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
            "length(grant_id) = 64 AND grant_id NOT GLOB '*[^0-9a-f]*'",
            name="ck_budget_household_members_grant_id",
        ),
    )
    op.create_index(_IX_OWNER, _TABLE, ["owner"])


def downgrade():
    # Take a SQLite backup before downgrading. This drops household grants only.
    # Reapplying 0022 starts from an empty grant table; grants are not restored.
    op.drop_index(_IX_OWNER, table_name=_TABLE)
    op.drop_table(_TABLE)
