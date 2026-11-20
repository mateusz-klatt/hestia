"""Versioned private budget controls and durable connection pause.

Revision ID: 0011
Revises: 0010
"""
from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("budget_accounts", sa.Column("control_version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("budget_connections", sa.Column("control_version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("budget_connections", sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.text("0")))
    op.create_table("budget_connection_audit",
                    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
                    sa.Column("connection_id", sa.String(), sa.ForeignKey("budget_connections.id"), nullable=False),
                    sa.Column("owner", sa.String(), sa.ForeignKey("users.username"), nullable=False),
                    sa.Column("actor", sa.String(), nullable=False),
                    sa.Column("origin", sa.String(), nullable=False),
                    sa.Column("field", sa.String(), nullable=False),
                    sa.Column("old_value_json", sa.String(), nullable=False),
                    sa.Column("new_value_json", sa.String(), nullable=False),
                    sa.Column("changed_at", sa.Float(), nullable=False))


def downgrade():
    op.drop_table("budget_connection_audit")
    op.drop_column("budget_connections", "paused")
    op.drop_column("budget_connections", "control_version")
    op.drop_column("budget_accounts", "control_version")
