"""Index account-scoped date ranges for manual receipt transaction search.

Revision ID: 0037
Revises: 0036
"""
from alembic import op
import sqlalchemy as sa

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

_INDEX = "ix_budget_transactions_account_booking_date"


def upgrade() -> None:
    # Keep this expression aligned with the search predicate. Damaged legacy
    # payloads must not prevent upgrading the rest of the ledger.
    op.create_index(_INDEX, "budget_transactions", ["account_id", sa.text("""
        (CASE WHEN json_valid(payload_json) THEN coalesce(
            json_extract(payload_json, '$.booking_date'),
            json_extract(payload_json, '$.value_date'),
            json_extract(payload_json, '$.transaction_date'),
            '0001-01-01'
        ) END) DESC
    """)])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="budget_transactions")
