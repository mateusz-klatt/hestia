"""Durable refresh jobs, owner idempotency keys and per-connection background opt-in.

Revision ID: 0013
Revises: 0012
"""
from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("budget_connections", sa.Column("background_enabled", sa.Boolean(), nullable=False,
                                                  server_default=sa.text("0")))
    op.add_column("budget_connections", sa.Column("attention", sa.String()))
    op.create_table("budget_jobs",
                    sa.Column("id", sa.String(), primary_key=True),
                    sa.Column("owner", sa.String(), sa.ForeignKey("users.username"), nullable=False),
                    sa.Column("connection_id", sa.String(), sa.ForeignKey("budget_connections.id"), nullable=False),
                    sa.Column("source", sa.String(), nullable=False),
                    sa.Column("account_id", sa.String(), sa.ForeignKey("budget_accounts.id")),
                    sa.Column("operation", sa.String()),
                    sa.Column("expected_version", sa.Integer(), nullable=False),
                    sa.Column("status", sa.String(), nullable=False),
                    sa.Column("error", sa.String()),
                    sa.Column("next_attempt", sa.Float()),
                    sa.Column("created_at", sa.Float(), nullable=False),
                    sa.Column("started_at", sa.Float()),
                    sa.Column("finished_at", sa.Float()))
    op.create_index("uq_budget_jobs_active", "budget_jobs", ["connection_id"], unique=True,
                    sqlite_where=sa.text("status IN ('queued', 'running')"))
    op.create_index("ix_budget_jobs_owner_created", "budget_jobs", ["owner", "created_at"])
    op.create_table("budget_job_requests",
                    sa.Column("owner", sa.String(), sa.ForeignKey("users.username"), primary_key=True),
                    sa.Column("idempotency_key", sa.String(), primary_key=True),
                    sa.Column("digest", sa.String(), nullable=False),
                    sa.Column("job_id", sa.String(), sa.ForeignKey("budget_jobs.id", ondelete="CASCADE"), nullable=False),
                    sa.Column("created_at", sa.Float(), nullable=False))


def downgrade():
    op.drop_table("budget_job_requests")
    op.drop_index("ix_budget_jobs_owner_created", table_name="budget_jobs")
    op.drop_index("uq_budget_jobs_active", table_name="budget_jobs")
    op.drop_table("budget_jobs")
    op.drop_column("budget_connections", "attention")
    op.drop_column("budget_connections", "background_enabled")
