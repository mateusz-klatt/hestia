"""Additive authorization bridge schema: ownership, grants, actor binding, and native login codes.

Revision ID: 0030
Revises: 0029
"""
from alembic import op
import sqlalchemy as sa

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("budget_owner", sa.Boolean(), nullable=False, server_default=sa.text("0")))
    op.add_column(
        "users",
        sa.Column("credential_generation", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )

    op.create_table(
        "permission_grants",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(), sa.ForeignKey("users.username"), nullable=False),
        sa.Column("permission_id", sa.String(), nullable=False),
        sa.Column("target_owner", sa.String(), sa.ForeignKey("users.username"), nullable=True),
    )
    op.create_index(
        "uq_permission_grants_installation",
        "permission_grants",
        ["username", "permission_id"],
        unique=True,
        sqlite_where=sa.text("target_owner IS NULL"),
    )
    op.create_index(
        "uq_permission_grants_owner",
        "permission_grants",
        ["username", "permission_id", "target_owner"],
        unique=True,
        sqlite_where=sa.text("target_owner IS NOT NULL"),
    )

    op.execute("ALTER TABLE budget_receipts ADD COLUMN submitted_by VARCHAR REFERENCES users(username)")
    op.execute("ALTER TABLE budget_receipt_device_tokens ADD COLUMN actor VARCHAR REFERENCES users(username)")
    op.execute("ALTER TABLE budget_receipt_device_tokens ADD COLUMN issued_permissions TEXT")

    op.create_index(
        "ix_budget_receipt_device_tokens_owner_actor",
        "budget_receipt_device_tokens",
        ["owner", "actor"],
    )

    op.create_table(
        "native_login_codes",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("username", sa.String(), sa.ForeignKey("users.username"), nullable=False),
        sa.Column("code_hmac", sa.String(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("consumed_at", sa.Float(), nullable=True),
        sa.UniqueConstraint("code_hmac", name="uq_native_login_codes_code_hmac"),
        sa.CheckConstraint(
            "length(code_hmac) = 64 AND code_hmac NOT GLOB '*[^0-9a-f]*'",
            name="ck_native_login_codes_hmac",
        ),
        sa.CheckConstraint("generation >= 0", name="ck_native_login_codes_generation"),
        sa.CheckConstraint("expires_at > created_at", name="ck_native_login_codes_expiry"),
    )
    op.create_index(
        "ix_native_login_codes_active_user",
        "native_login_codes",
        ["username", "expires_at"],
        sqlite_where=sa.text("consumed_at IS NULL"),
    )


def downgrade():
    op.execute(
        "DELETE FROM app_meta "
        "WHERE key IN ('auth_session_phase', 'authz_policy_phase')"
    )
    op.drop_table("native_login_codes")
    op.drop_table("permission_grants")
    op.drop_index(
        "ix_budget_receipt_device_tokens_owner_actor",
        table_name="budget_receipt_device_tokens",
    )
    op.drop_column("budget_receipt_device_tokens", "issued_permissions")
    op.drop_column("budget_receipt_device_tokens", "actor")
    op.drop_column("budget_receipts", "submitted_by")
    op.drop_column("users", "credential_generation")
    op.drop_column("users", "budget_owner")
