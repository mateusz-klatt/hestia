"""Receipt device tokens and one-time pairing codes. HMAC columns only.

Revision ID: 0024
Revises: 0023

Downgrade drops these tables and device-token audit entries. Receipt and household rows stay.
"""
from alembic import op
import sqlalchemy as sa

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

_TOKENS = "budget_receipt_device_tokens"
_CODES = "budget_receipt_pair_codes"
_IX_TOKENS = "ix_budget_receipt_device_tokens_owner"
_IX_CODES = "ix_budget_receipt_pair_codes_owner"
_HEX64 = "length({name}) = 64 AND {name} NOT GLOB '*[^0-9a-f]*'"


def upgrade():
    op.create_table(
        _TOKENS,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("device_name", sa.String(), nullable=False),
        sa.Column("token_hmac", sa.String(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("hard_expires_at", sa.Float(), nullable=False),
        sa.Column("sliding_written_at", sa.Float(), nullable=False),
        sa.Column("last_used_at", sa.Float(), nullable=True),
        sa.Column("revoked_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["owner"], ["users.username"]),
        sa.UniqueConstraint("token_hmac", name="uq_budget_receipt_device_tokens_hmac"),
        sa.CheckConstraint("length(device_name) BETWEEN 1 AND 64",
                           name="ck_budget_receipt_device_tokens_name"),
        sa.CheckConstraint(_HEX64.format(name="token_hmac"), name="ck_budget_receipt_device_tokens_hmac"),
        sa.CheckConstraint("expires_at <= hard_expires_at", name="ck_budget_receipt_device_tokens_expiry"),
    )
    op.create_index(_IX_TOKENS, _TOKENS, ["owner"])
    op.create_table(
        _CODES,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("code_hmac", sa.String(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("consumed_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["owner"], ["users.username"]),
        sa.UniqueConstraint("code_hmac", name="uq_budget_receipt_pair_codes_hmac"),
        sa.CheckConstraint(_HEX64.format(name="code_hmac"), name="ck_budget_receipt_pair_codes_hmac"),
    )
    op.create_index(_IX_CODES, _CODES, ["owner"])


def downgrade():
    op.execute("DELETE FROM budget_analytics_audit WHERE subject_type = 'receipt_device_token'")
    op.drop_index(_IX_CODES, table_name=_CODES)
    op.drop_index(_IX_TOKENS, table_name=_TOKENS)
    op.drop_table(_CODES)
    op.drop_table(_TOKENS)
