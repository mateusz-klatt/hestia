"""Opt-in read-only bank ingestion, durable identities and uncertain observations.

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("budget_connections",
                    sa.Column("id", sa.String(), primary_key=True),
                    sa.Column("owner", sa.String(), sa.ForeignKey("users.username"), nullable=False),
                    sa.Column("application_id", sa.String(), nullable=False),
                    sa.Column("bank_name", sa.String(), nullable=False),
                    sa.Column("country", sa.String(), nullable=False),
                    sa.Column("enabled", sa.Boolean(), nullable=False),
                    sa.Column("valid_until", sa.Float()),
                    sa.Column("last_attempt", sa.Float()),
                    sa.Column("last_success", sa.Float()),
                    sa.Column("next_attempt", sa.Float(), nullable=False),
                    sa.Column("failures", sa.Integer(), nullable=False),
                    sa.Column("last_error", sa.String()))
    op.create_table("budget_accounts",
                    sa.Column("id", sa.String(), primary_key=True),
                    sa.Column("connection_id", sa.String(), sa.ForeignKey("budget_connections.id"), nullable=False),
                    sa.Column("owner", sa.String(), sa.ForeignKey("users.username"), nullable=False),
                    sa.Column("identification_hash", sa.String(), nullable=False),
                    sa.Column("enabled", sa.Boolean(), nullable=False),
                    sa.Column("balances_json", sa.String(), nullable=False),
                    sa.Column("observed_at", sa.Float()),
                    sa.Column("last_attempt", sa.Float()),
                    sa.Column("last_error", sa.String()),
                    sa.UniqueConstraint("owner", "identification_hash"))
    op.create_table("budget_transactions",
                    sa.Column("account_id", sa.String(), sa.ForeignKey("budget_accounts.id"), primary_key=True),
                    sa.Column("entry_reference", sa.String(), primary_key=True),
                    sa.Column("payload_json", sa.String(), nullable=False),
                    sa.Column("observed_at", sa.Float(), nullable=False),
                    sa.Column("uncertain", sa.Boolean(), nullable=False))
    op.create_table("budget_unresolved",
                    sa.Column("account_id", sa.String(), sa.ForeignKey("budget_accounts.id"), primary_key=True),
                    sa.Column("ordinal", sa.Integer(), primary_key=True),
                    sa.Column("payload_json", sa.String(), nullable=False),
                    sa.Column("observed_at", sa.Float(), nullable=False))


def downgrade():
    op.drop_table("budget_unresolved")
    op.drop_table("budget_transactions")
    op.drop_table("budget_accounts")
    op.drop_table("budget_connections")
