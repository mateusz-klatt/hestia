"""Add card_last4 and blik_ref_hmac columns on budget_receipts."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

_RECEIPTS = "budget_receipts"
_BLIK_INDEX = "ix_budget_receipts_owner_blik_ref"
_COLS = (
    ("blik_ref_hmac", sa.CheckConstraint(
        "blik_ref_hmac IS NULL OR (length(blik_ref_hmac) = 64 AND blik_ref_hmac NOT GLOB '*[^0-9a-f]*')",
        name="ck_budget_receipts_blik_ref_hmac",
    )),
    ("card_last4", sa.CheckConstraint(
        "card_last4 IS NULL OR (length(card_last4) = 4 AND card_last4 NOT GLOB '*[^0-9]*')",
        name="ck_budget_receipts_card_last4",
    )),
)


def upgrade() -> None:
    for name, check in _COLS:
        op.add_column(_RECEIPTS, sa.Column(name, sa.String(), check, nullable=True))
    op.create_index(
        _BLIK_INDEX,
        _RECEIPTS,
        ["owner", "blik_ref_hmac"],
        sqlite_where=sa.text("blik_ref_hmac IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_BLIK_INDEX, table_name=_RECEIPTS)
    for name, _check in reversed(_COLS):
        op.drop_column(_RECEIPTS, name)
