"""Add optional hashed bank references to mortgage schedules.

Revision ID: 0036
Revises: 0035
"""
from alembic import op
import sqlalchemy as sa

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

_SCHEDULES = "budget_mortgage_schedules"


def upgrade() -> None:
    op.add_column(_SCHEDULES, sa.Column("match_references", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column(_SCHEDULES, "match_references")
