"""Optional account display observations without changing acquisition state.

Revision ID: 0012
Revises: 0011
"""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("budget_account_display",
                    sa.Column("account_id", sa.String(), sa.ForeignKey("budget_accounts.id"), primary_key=True),
                    sa.Column("product", sa.String()),
                    sa.Column("currency", sa.String()),
                    sa.Column("masked_suffix", sa.String()),
                    sa.Column("source", sa.String(), nullable=False, server_default="offline_import"),
                    sa.Column("observed_at", sa.Float(), nullable=False),
                    sa.Column("imported_at", sa.Float(), nullable=False),
                    sa.Column("digest", sa.String(), nullable=False))


def downgrade():
    op.drop_table("budget_account_display")
