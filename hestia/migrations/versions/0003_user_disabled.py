"""add users.disabled (temporary account disable)

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-05

Adds the per-user ``disabled`` flag (#PR-D) — a reversible "switch the account off" that
revokes access without deleting the row (the role is kept for re-enable). Existing rows
default to ``false`` (enabled), so no one is locked out by the migration. Mirrors the
``disabled`` column on ``hestia.db.User`` exactly (incl. ``server_default``) so the
model↔migration drift guard sees no diff.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # NOT NULL with a constant default → SQLite backfills existing rows with 0 (enabled) as it adds it.
    op.add_column("users", sa.Column("disabled", sa.Boolean(), nullable=False, server_default=sa.text("0")))


def downgrade() -> None:
    # batch_alter_table recreates the table without the column, so the drop works on every SQLite build.
    with op.batch_alter_table("users") as batch:
        batch.drop_column("disabled")
