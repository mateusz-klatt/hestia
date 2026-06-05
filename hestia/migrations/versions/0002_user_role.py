"""add users.role (admin/operator/viewer)

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-05

Adds the per-user access role for RBAC (#73). The column default is ``viewer`` (least
privilege) so any row created without an explicit role is NOT silently an admin. Existing
rows predate roles, so they are backfilled to ``admin`` — the operator's accounts keep full
access (no lockout). Mirrors the ``role`` column on ``hestia.db.User`` exactly, including the
``server_default``, so the model↔migration drift guard sees no diff.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # NOT NULL with a constant default → SQLite backfills existing rows with 'viewer' as it adds the
    # column; the UPDATE then promotes those pre-existing (pre-role) accounts to 'admin'. New rows
    # inserted after this default to 'viewer' (or the explicit role the app/CLI supplies).
    op.add_column("users", sa.Column("role", sa.String(), nullable=False, server_default=sa.text("'viewer'")))
    op.execute("UPDATE users SET role = 'admin'")   # every account that existed before roles → admin


def downgrade() -> None:
    # batch_alter_table recreates the table without the column, so the drop works on every SQLite build.
    with op.batch_alter_table("users") as batch:
        batch.drop_column("role")
