"""Owner-private monthly category spending limits (Budget 2D).

Revision ID: 0017
Revises: 0016
"""
from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "budget_category_limits",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("category_id", sa.String(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("monthly_limit", sa.String(), nullable=False),
        sa.Column("warning_percent", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["owner"], ["users.username"]),
        sa.ForeignKeyConstraint(["category_id"], ["budget_categories.id"]),
        sa.UniqueConstraint("owner", "category_id", "currency"),
        sa.CheckConstraint("version >= 1", name="ck_budget_category_limits_version"),
        sa.CheckConstraint("warning_percent >= 1 AND warning_percent <= 99",
                           name="ck_budget_category_limits_warning_percent"),
        sa.CheckConstraint("enabled IN (0, 1)", name="ck_budget_category_limits_enabled"),
        sa.CheckConstraint("length(monthly_limit) >= 1 AND length(monthly_limit) <= 80",
                           name="ck_budget_category_limits_monthly_limit_len"),
    )


def downgrade():
    # Take a backup before downgrading: dropping the table removes saved limit rows
    # while leaving historical budget_analytics_audit entries intact.
    op.drop_table("budget_category_limits")
