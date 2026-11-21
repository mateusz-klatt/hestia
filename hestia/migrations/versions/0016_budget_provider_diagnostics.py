"""Persist the last HTTP status and documented Enable Banking error identifier of a failed acquisition.

Revision ID: 0016
Revises: 0015
"""
from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

_TABLES = ("budget_connections", "budget_components")


def upgrade():
    for table in _TABLES:
        op.add_column(table, sa.Column("last_http_status", sa.Integer()))
        op.add_column(table, sa.Column("last_provider_error", sa.String()))


def downgrade():
    # Native DROP COLUMN, as in 0011/0013: recreating a referenced parent table would trip its foreign keys.
    for table in _TABLES:
        op.drop_column(table, "last_provider_error")
        op.drop_column(table, "last_http_status")
