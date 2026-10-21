"""Durable calendar groups, canonical events, replicas, tombstones and outbox.

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("calendar_groups",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False))
    op.create_table("calendar_endpoints",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("group_id", sa.String(), sa.ForeignKey("calendar_groups.id"), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("account", sa.String(), nullable=False),
        sa.Column("calendar_id", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("cursor", sa.String()),
        sa.Column("last_success", sa.Float()),
        sa.Column("last_error", sa.String()))
    op.create_table("calendar_events",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("group_id", sa.String(), sa.ForeignKey("calendar_groups.id"), nullable=False),
        sa.Column("uid", sa.String(), nullable=False),
        sa.Column("payload_json", sa.String(), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.Column("deleted_at", sa.Float()),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("modified_at", sa.Float(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.UniqueConstraint("group_id", "uid"))
    op.create_table("calendar_replicas",
        sa.Column("endpoint_id", sa.String(), sa.ForeignKey("calendar_endpoints.id"), primary_key=True),
        sa.Column("event_id", sa.String(), sa.ForeignKey("calendar_events.id"), primary_key=True),
        sa.Column("remote_id", sa.String(), nullable=False),
        sa.Column("etag", sa.String()),
        sa.Column("observed_hash", sa.String()),
        sa.Column("applied_revision", sa.Integer(), nullable=False),
        sa.Column("seen", sa.Boolean(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.Column("outbound_hash", sa.String()),
        sa.Column("outbound_revision", sa.Integer()),
        sa.Column("uncertain", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("endpoint_id", "remote_id"))
    op.create_table("calendar_outbox",
        sa.Column("endpoint_id", sa.String(), sa.ForeignKey("calendar_endpoints.id"), primary_key=True),
        sa.Column("event_id", sa.String(), sa.ForeignKey("calendar_events.id"), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt", sa.Float(), nullable=False),
        sa.Column("last_error", sa.String()))


def downgrade():
    for name in ("calendar_outbox", "calendar_replicas", "calendar_events", "calendar_endpoints", "calendar_groups"):
        op.drop_table(name)
