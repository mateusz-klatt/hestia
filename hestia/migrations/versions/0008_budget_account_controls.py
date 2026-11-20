"""Separate configured identities from persisted owner selection and kind.

Revision ID: 0008
Revises: 0007

Legacy selected accounts stay selected. New identities are configured separately
and remain unselected until an explicit operator action on behalf of their owner.
"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("budget_accounts", sa.Column("configured", sa.Boolean(), nullable=False, server_default=sa.text("0")))
    op.add_column("budget_accounts", sa.Column("kind", sa.String(), nullable=False, server_default="unknown"))
    op.add_column("budget_accounts", sa.Column("kind_confirmed_at", sa.Float()))
    op.add_column("budget_accounts", sa.Column("kind_confirmed_by", sa.String()))
    op.add_column("budget_accounts", sa.Column("kind_confirmation_origin", sa.String()))
    op.execute("UPDATE budget_accounts SET configured = enabled")
    op.create_table("budget_account_audit",
                    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
                    sa.Column("account_id", sa.String(), sa.ForeignKey("budget_accounts.id"), nullable=False),
                    sa.Column("owner", sa.String(), sa.ForeignKey("users.username"), nullable=False),
                    sa.Column("actor", sa.String(), nullable=False),
                    sa.Column("origin", sa.String(), nullable=False),
                    sa.Column("field", sa.String(), nullable=False),
                    sa.Column("old_value_json", sa.String(), nullable=False),
                    sa.Column("new_value_json", sa.String(), nullable=False),
                    sa.Column("changed_at", sa.Float(), nullable=False))


def downgrade():
    op.drop_table("budget_account_audit")
    op.drop_column("budget_accounts", "kind_confirmation_origin")
    op.drop_column("budget_accounts", "kind_confirmed_by")
    op.drop_column("budget_accounts", "kind_confirmed_at")
    op.drop_column("budget_accounts", "kind")
    op.drop_column("budget_accounts", "configured")
