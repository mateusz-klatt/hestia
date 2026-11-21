"""Browser-bound single-use consent flows and managed credential generations.

Revision ID: 0014
Revises: 0013
"""
from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("budget_connections", sa.Column("credential_mode", sa.String(), nullable=False,
                                                  server_default=sa.text("'external'")))
    op.add_column("budget_connections", sa.Column("consent_generation", sa.Integer(), nullable=False,
                                                  server_default=sa.text("0")))
    op.add_column("budget_connections", sa.Column("active_consent_id", sa.String()))
    op.create_table("budget_consent_flows",
                    sa.Column("id", sa.String(), primary_key=True),
                    sa.Column("owner", sa.String(), sa.ForeignKey("users.username"), nullable=False),
                    sa.Column("connection_id", sa.String(), sa.ForeignKey("budget_connections.id"), nullable=False),
                    sa.Column("application_id", sa.String(), nullable=False),
                    sa.Column("bank_name", sa.String(), nullable=False),
                    sa.Column("country", sa.String(), nullable=False),
                    sa.Column("intent", sa.String(), nullable=False),
                    sa.Column("expected_connection_version", sa.Integer(), nullable=False),
                    sa.Column("expected_generation", sa.Integer(), nullable=False),
                    sa.Column("state_hash", sa.String(), nullable=False, unique=True),
                    sa.Column("nonce_hash", sa.String(), nullable=False),
                    sa.Column("auth_fingerprint", sa.String(), nullable=False),
                    sa.Column("binding_revision", sa.Integer(), nullable=False),
                    sa.Column("flow_revision", sa.Integer(), nullable=False),
                    sa.Column("created_at", sa.Float(), nullable=False),
                    sa.Column("expires_at", sa.Float(), nullable=False),
                    sa.Column("status", sa.String(), nullable=False),
                    sa.Column("error", sa.String()),
                    sa.Column("callback_at", sa.Float()),
                    sa.Column("exchange_at", sa.Float()),
                    sa.Column("exchange_binding_revision", sa.Integer()),
                    sa.Column("exchange_flow_revision", sa.Integer()),
                    sa.Column("code_ref", sa.String()),
                    sa.Column("code_expires_at", sa.Float()),
                    sa.Column("candidate_consent_id", sa.String()))
    op.create_index("ix_budget_consent_flows_owner_created", "budget_consent_flows", ["owner", "created_at"])
    op.create_table("budget_consents",
                    sa.Column("id", sa.String(), primary_key=True),
                    sa.Column("owner", sa.String(), sa.ForeignKey("users.username"), nullable=False),
                    sa.Column("connection_id", sa.String(), sa.ForeignKey("budget_connections.id"), nullable=False),
                    sa.Column("application_id", sa.String(), nullable=False),
                    sa.Column("bank_name", sa.String(), nullable=False),
                    sa.Column("country", sa.String(), nullable=False),
                    sa.Column("provenance", sa.String(), nullable=False),
                    sa.Column("status", sa.String(), nullable=False),
                    sa.Column("capsule_ref", sa.String(), nullable=False),
                    sa.Column("valid_until", sa.Float()),
                    sa.Column("generation", sa.Integer(), nullable=False),
                    sa.Column("metadata_json", sa.String(), nullable=False),
                    sa.Column("created_at", sa.Float(), nullable=False))
    op.create_table("budget_consent_audit",
                    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
                    sa.Column("owner", sa.String(), sa.ForeignKey("users.username"), nullable=False),
                    sa.Column("connection_id", sa.String(), sa.ForeignKey("budget_connections.id"), nullable=False),
                    sa.Column("flow_id", sa.String(), sa.ForeignKey("budget_consent_flows.id"), nullable=False),
                    sa.Column("actor", sa.String(), nullable=False),
                    sa.Column("origin", sa.String(), nullable=False),
                    sa.Column("action", sa.String(), nullable=False),
                    sa.Column("old_value_json", sa.String(), nullable=False),
                    sa.Column("new_value_json", sa.String(), nullable=False),
                    sa.Column("changed_at", sa.Float(), nullable=False))


def downgrade():
    op.drop_table("budget_consent_audit")
    op.drop_table("budget_consents")
    op.drop_index("ix_budget_consent_flows_owner_created", table_name="budget_consent_flows")
    op.drop_table("budget_consent_flows")
    op.drop_column("budget_connections", "active_consent_id")
    op.drop_column("budget_connections", "consent_generation")
    op.drop_column("budget_connections", "credential_mode")
