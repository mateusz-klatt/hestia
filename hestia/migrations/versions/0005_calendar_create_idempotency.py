"""Persist create retry identities and index reconciliation lookups.

Revision ID: 0005
Revises: 0004

Existing events keep NULL retry identities. Tombstones retain their create key
and original fingerprint, so a lost HTTP response cannot cause resurrection.
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("calendar_events", sa.Column("create_key", sa.String()))
    op.add_column("calendar_events", sa.Column("create_fingerprint", sa.String()))
    op.create_index("uq_calendar_events_create_key", "calendar_events", ["group_id", "create_key"], unique=True)
    op.create_index("ix_calendar_replicas_event_id", "calendar_replicas", ["event_id"])
    op.create_index("ix_calendar_outbox_event_id", "calendar_outbox", ["event_id"])


def downgrade():
    op.drop_index("ix_calendar_outbox_event_id", table_name="calendar_outbox")
    op.drop_index("ix_calendar_replicas_event_id", table_name="calendar_replicas")
    op.drop_index("uq_calendar_events_create_key", table_name="calendar_events")
    op.drop_column("calendar_events", "create_fingerprint")
    op.drop_column("calendar_events", "create_key")
