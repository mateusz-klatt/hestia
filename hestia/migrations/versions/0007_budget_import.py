"""Offline import provenance and separate uncertainty, preserving API authority.

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("budget_transactions", sa.Column("source", sa.String(), nullable=False, server_default="api"))
    op.add_column("budget_transactions", sa.Column("api_observed_at", sa.Float()))
    op.execute("UPDATE budget_transactions SET api_observed_at = observed_at")
    op.create_table("budget_import_batches",
                    sa.Column("id", sa.String(), primary_key=True),
                    sa.Column("fingerprint", sa.String(), nullable=False, unique=True),
                    sa.Column("account_id", sa.String(), sa.ForeignKey("budget_accounts.id"), nullable=False),
                    sa.Column("fetched_at", sa.Float(), nullable=False),
                    sa.Column("imported_at", sa.Float(), nullable=False),
                    sa.Column("balances_json", sa.String()),
                    sa.Column("report_json", sa.String(), nullable=False))
    op.create_table("budget_import_unresolved",
                    sa.Column("batch_id", sa.String(), sa.ForeignKey("budget_import_batches.id"), primary_key=True),
                    sa.Column("ordinal", sa.Integer(), primary_key=True),
                    sa.Column("reason", sa.String(), nullable=False),
                    sa.Column("payload_json", sa.String(), nullable=False))


def downgrade():
    op.drop_table("budget_import_unresolved")
    op.drop_table("budget_import_batches")
    op.drop_column("budget_transactions", "api_observed_at")
    op.drop_column("budget_transactions", "source")
