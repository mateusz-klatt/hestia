"""Durable acquisition debits, shared cooldowns and request pacing.

Revision ID: 0010
Revises: 0009
"""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("budget_components", sa.Column("retry_at", sa.Float()))
    op.add_column("budget_components", sa.Column("suspended", sa.Boolean(), nullable=False, server_default=sa.text("0")))
    op.add_column("budget_components", sa.Column("backoff_until", sa.Float(), nullable=False, server_default="0"))
    op.create_table("budget_acquisitions",
                    sa.Column("id", sa.String(), primary_key=True),
                    sa.Column("account_id", sa.String(), sa.ForeignKey("budget_accounts.id"), nullable=False),
                    sa.Column("operation", sa.String(), nullable=False),
                    sa.Column("attempt_id", sa.String(), nullable=False),
                    sa.Column("started_at", sa.Float(), nullable=False),
                    sa.Column("origin", sa.String(), nullable=False),
                    sa.UniqueConstraint("account_id", "operation", "attempt_id"))
    op.create_index("ix_budget_acquisition_window", "budget_acquisitions", ["account_id", "operation", "started_at"])
    op.create_table("budget_request_limits",
                    sa.Column("scope", sa.String(), primary_key=True),
                    sa.Column("key", sa.String(), primary_key=True),
                    sa.Column("next_attempt", sa.Float(), nullable=False, server_default="0"),
                    sa.Column("next_request_at", sa.Float(), nullable=False, server_default="0"),
                    sa.Column("last_request_at", sa.Float()),
                    sa.Column("retry_at", sa.Float()),
                    sa.Column("suspended", sa.Boolean(), nullable=False, server_default=sa.text("0")))
    op.create_table("budget_limit_audit",
                    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
                    sa.Column("scope", sa.String(), nullable=False),
                    sa.Column("target_key", sa.String(), nullable=False),
                    sa.Column("owner", sa.String(), sa.ForeignKey("users.username"), nullable=False),
                    sa.Column("actor", sa.String(), nullable=False),
                    sa.Column("origin", sa.String(), nullable=False),
                    sa.Column("field", sa.String(), nullable=False),
                    sa.Column("old_value_json", sa.String(), nullable=False),
                    sa.Column("new_value_json", sa.String(), nullable=False),
                    sa.Column("changed_at", sa.Float(), nullable=False))
    # Old account/connection attempts do not identify the endpoint. Conservatively
    # debit both components at the latest possible attempt, without rewriting history.
    op.execute("""
        INSERT INTO budget_acquisitions (id, account_id, operation, attempt_id, started_at, origin)
        SELECT 'legacy:' || b.account_id || ':' || b.operation, b.account_id, b.operation,
               'legacy_upper_bound', MAX(COALESCE(b.last_attempt, -1),
                                        COALESCE(a.last_attempt, -1), COALESCE(c.last_attempt, -1)),
               'legacy_upper_bound'
        FROM budget_components b JOIN budget_accounts a ON a.id = b.account_id
        JOIN budget_connections c ON c.id = a.connection_id
        WHERE b.last_attempt IS NOT NULL OR a.last_attempt IS NOT NULL OR c.last_attempt IS NOT NULL
    """)


def downgrade():
    op.drop_table("budget_limit_audit")
    op.drop_table("budget_request_limits")
    op.drop_index("ix_budget_acquisition_window", table_name="budget_acquisitions")
    op.drop_table("budget_acquisitions")
    op.drop_column("budget_components", "backoff_until")
    op.drop_column("budget_components", "suspended")
    op.drop_column("budget_components", "retry_at")
