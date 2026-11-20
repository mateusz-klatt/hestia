"""Independent API component freshness without inventing endpoint history.

Revision ID: 0009
Revises: 0008
"""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("budget_components",
                    sa.Column("account_id", sa.String(), sa.ForeignKey("budget_accounts.id"), primary_key=True),
                    sa.Column("operation", sa.String(), primary_key=True),
                    sa.Column("last_attempt", sa.Float()),
                    sa.Column("last_success", sa.Float()),
                    sa.Column("last_error", sa.String()),
                    sa.Column("failures", sa.Integer(), nullable=False, server_default="0"),
                    sa.Column("next_attempt", sa.Float(), nullable=False, server_default="0"),
                    sa.Column("attempt_id", sa.String()),
                    sa.Column("lease_until", sa.Float()))
    # Legacy observed_at meant both API components completed. Neither offline
    # history nor an account-level error proves an individual endpoint attempt.
    for operation in ("transactions", "balances"):
        op.get_bind().execute(sa.text("""
            INSERT INTO budget_components (account_id, operation, last_success, next_attempt)
            SELECT a.id, :operation, a.observed_at, c.next_attempt
            FROM budget_accounts a JOIN budget_connections c ON c.id = a.connection_id
        """), {"operation": operation})


def downgrade():
    op.drop_table("budget_components")
