"""Owner-private mortgage reminder schedules and payment marks (Budget 2D).

Revision ID: 0018
Revises: 0017
"""
from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

_SCHEDULES = "budget_mortgage_schedules"
_PAYMENTS = "budget_mortgage_payments"
_IX_SCHEDULES_OWNER = "ix_budget_mortgage_schedules_owner"
_IX_PAYMENTS_OWNER = "ix_budget_mortgage_payments_owner"


def upgrade():
    op.create_table(
        _SCHEDULES,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("amount", sa.String(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("due_day", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.String(), nullable=False),
        sa.Column("end_date", sa.String(), nullable=True),
        sa.Column("remind_days_before", sa.Integer(), nullable=False),
        sa.Column("destination_kind", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["owner"], ["users.username"]),
        sa.UniqueConstraint("id", "owner", name="uq_budget_mortgage_schedules_id_owner"),
        sa.CheckConstraint("length(label) >= 1 AND length(label) <= 256",
                           name="ck_budget_mortgage_schedules_label_len"),
        sa.CheckConstraint("length(amount) >= 1 AND length(amount) <= 80",
                           name="ck_budget_mortgage_schedules_amount_len"),
        sa.CheckConstraint("length(currency) = 3 AND currency GLOB '[A-Z][A-Z][A-Z]'",
                           name="ck_budget_mortgage_schedules_currency"),
        sa.CheckConstraint("due_day >= 1 AND due_day <= 31",
                           name="ck_budget_mortgage_schedules_due_day"),
        sa.CheckConstraint(
            "length(start_date) = 10 AND start_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'",
            name="ck_budget_mortgage_schedules_start_date",
        ),
        sa.CheckConstraint(
            "end_date IS NULL OR (length(end_date) = 10 "
            "AND end_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' "
            "AND end_date >= start_date)",
            name="ck_budget_mortgage_schedules_end_date",
        ),
        sa.CheckConstraint("remind_days_before >= 0 AND remind_days_before <= 31",
                           name="ck_budget_mortgage_schedules_remind_days"),
        sa.CheckConstraint("destination_kind IN ('owner_inbox', 'owner_push', 'owner_email')",
                           name="ck_budget_mortgage_schedules_destination_kind"),
        sa.CheckConstraint("enabled IN (0, 1)", name="ck_budget_mortgage_schedules_enabled"),
        sa.CheckConstraint("version >= 1", name="ck_budget_mortgage_schedules_version"),
    )
    op.create_index(_IX_SCHEDULES_OWNER, _SCHEDULES, ["owner"])

    op.create_table(
        _PAYMENTS,
        sa.Column("schedule_id", sa.String(), nullable=False),
        sa.Column("month", sa.String(), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("marked_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("schedule_id", "month"),
        sa.ForeignKeyConstraint(["owner"], ["users.username"]),
        sa.ForeignKeyConstraint(
            ["schedule_id", "owner"],
            [f"{_SCHEDULES}.id", f"{_SCHEDULES}.owner"],
            name="fk_budget_mortgage_payments_schedule_owner",
        ),
        sa.CheckConstraint(
            "length(month) = 7 AND month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]' "
            "AND substr(month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_budget_mortgage_payments_month",
        ),
    )
    op.create_index(_IX_PAYMENTS_OWNER, _PAYMENTS, ["owner"])


def downgrade():
    # Take a backup before downgrading: dropping these tables removes saved mortgage
    # schedules and payment marks while leaving sanitized budget_analytics_audit rows intact.
    op.drop_index(_IX_PAYMENTS_OWNER, table_name=_PAYMENTS)
    op.drop_table(_PAYMENTS)
    op.drop_index(_IX_SCHEDULES_OWNER, table_name=_SCHEDULES)
    op.drop_table(_SCHEDULES)
