"""Minimal appointment watch state, active HMAC slot hashes, metadata-only alerts, poller cursor, and inbox kinds.

Revision ID: 0034
Revises: 0033
"""
from alembic import op
import sqlalchemy as sa

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

_WATCH_STATE = "appointments_watch_state"
_ACTIVE_SLOTS = "appointments_active_slots"
_INBOX_ALERTS = "appointments_inbox_alerts"
_POLLER_STATE = "appointments_poller_state"
_IX_WATCH_UPDATED = "ix_appointments_watch_state_updated"
_IX_SLOTS_EXPIRY = "ix_appointments_active_slots_expiry"
_IX_ALERTS_EXPIRY = "ix_appointments_inbox_alerts_expires"
_INBOX_READS = "budget_owner_inbox_reads"
_INBOX_KIND_CHECK = "ck_budget_owner_inbox_reads_kind"
_PREVIOUS_INBOX_KINDS = "'consent_renewal', 'mortgage_due', 'spending_limit', 'device_added'"
_APPOINTMENT_INBOX_KINDS = "'appointment_slot', 'appointment_attention'"


def _replace_inbox_kind_constraint(kinds: str) -> None:
    op.execute('DROP TABLE IF EXISTS "_alembic_tmp_budget_owner_inbox_reads"')
    with op.batch_alter_table(_INBOX_READS) as batch:
        batch.drop_constraint(_INBOX_KIND_CHECK, type_="check")
        batch.create_check_constraint(_INBOX_KIND_CHECK, f"kind IN ({kinds})")


def upgrade() -> None:
    op.create_table(
        _WATCH_STATE,
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("watch_id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False, server_default=sa.text("'cmp'")),
        sa.Column("last_status", sa.String(), nullable=False, server_default=sa.text("'ok'")),
        sa.Column("last_error_code", sa.String(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_run_at", sa.Float(), nullable=True),
        sa.Column("next_poll_at", sa.Float(), nullable=True),
        sa.Column("cooldown_until", sa.Float(), nullable=True),
        sa.Column("attention_notified_at", sa.Float(), nullable=True),
        sa.Column("notify_window_start_at", sa.Float(), nullable=True),
        sa.Column("notify_window_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("owner", "watch_id"),
        sa.ForeignKeyConstraint(["owner"], ["users.username"]),
        sa.CheckConstraint("length(watch_id) BETWEEN 1 AND 64", name="ck_appointments_watch_state_watch_id"),
        sa.CheckConstraint("provider IN ('cmp')", name="ck_appointments_watch_state_provider"),
        sa.CheckConstraint(
            "last_status IN ('ok', 'backoff', 'auth_attention', 'circuit_open', 'error')",
            name="ck_appointments_watch_state_last_status",
        ),
        sa.CheckConstraint(
            "last_error_code IS NULL OR last_error_code IN "
            "('network', 'rate_limit', 'provider_http', 'authorization', 'invalid_response', 'run_limit')",
            name="ck_appointments_watch_state_last_error_code",
        ),
        sa.CheckConstraint("consecutive_failures >= 0", name="ck_appointments_watch_state_failures"),
        sa.CheckConstraint(
            "last_run_at IS NULL OR last_run_at >= 0",
            name="ck_appointments_watch_state_last_run_at",
        ),
        sa.CheckConstraint(
            "next_poll_at IS NULL OR next_poll_at >= 0",
            name="ck_appointments_watch_state_next_poll_at",
        ),
        sa.CheckConstraint(
            "cooldown_until IS NULL OR cooldown_until >= 0",
            name="ck_appointments_watch_state_cooldown_until",
        ),
        sa.CheckConstraint(
            "attention_notified_at IS NULL OR attention_notified_at >= 0",
            name="ck_appointments_watch_state_attention_notified_at",
        ),
        sa.CheckConstraint(
            "notify_window_start_at IS NULL OR notify_window_start_at >= 0",
            name="ck_appointments_watch_state_notify_window_start",
        ),
        sa.CheckConstraint("notify_window_count >= 0", name="ck_appointments_watch_state_notify_window_count"),
        sa.CheckConstraint("updated_at >= 0", name="ck_appointments_watch_state_updated_at"),
    )
    op.create_index(_IX_WATCH_UPDATED, _WATCH_STATE, ["updated_at"])

    op.create_table(
        _ACTIVE_SLOTS,
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("watch_id", sa.String(), nullable=False),
        sa.Column("slot_hash", sa.String(), nullable=False),
        sa.Column("slot_date", sa.String(), nullable=False),
        sa.Column("better_than_booked", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("pending_notify", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("first_seen_at", sa.Float(), nullable=False),
        sa.Column("last_seen_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("notified_at", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("owner", "watch_id", "slot_hash"),
        sa.ForeignKeyConstraint(
            ["owner", "watch_id"],
            [f"{_WATCH_STATE}.owner", f"{_WATCH_STATE}.watch_id"],
            name="fk_appointments_active_slots_watch",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "length(slot_hash) = 64 AND slot_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_appointments_active_slots_hash",
        ),
        sa.CheckConstraint(
            "length(slot_date) = 10 AND slot_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'",
            name="ck_appointments_active_slots_date",
        ),
        sa.CheckConstraint("better_than_booked IN (0, 1)", name="ck_appointments_active_slots_better"),
        sa.CheckConstraint("pending_notify IN (0, 1)", name="ck_appointments_active_slots_pending"),
        sa.CheckConstraint("first_seen_at >= 0", name="ck_appointments_active_slots_first_seen"),
        sa.CheckConstraint("last_seen_at >= 0", name="ck_appointments_active_slots_last_seen"),
        sa.CheckConstraint("expires_at >= last_seen_at", name="ck_appointments_active_slots_expires"),
        sa.CheckConstraint(
            "notified_at IS NULL OR notified_at >= 0",
            name="ck_appointments_active_slots_notified",
        ),
    )
    op.create_index(_IX_SLOTS_EXPIRY, _ACTIVE_SLOTS, ["expires_at", "slot_date"])

    op.create_table(
        _INBOX_ALERTS,
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("watch_id", sa.String(), nullable=False),
        sa.Column("slot_hash", sa.String(), nullable=False),
        sa.Column("slot_date", sa.String(), nullable=False),
        sa.Column("alert_kind", sa.String(), nullable=False, server_default=sa.text("'new_slot'")),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("owner", "watch_id", "slot_hash"),
        sa.ForeignKeyConstraint(
            ["owner", "watch_id", "slot_hash"],
            [f"{_ACTIVE_SLOTS}.owner", f"{_ACTIVE_SLOTS}.watch_id", f"{_ACTIVE_SLOTS}.slot_hash"],
            name="fk_appointments_inbox_alerts_slot",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "length(slot_date) = 10 AND slot_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'",
            name="ck_appointments_inbox_alerts_date",
        ),
        sa.CheckConstraint(
            "alert_kind IN ('better_slot', 'new_slot')",
            name="ck_appointments_inbox_alerts_kind",
        ),
        sa.CheckConstraint("created_at >= 0", name="ck_appointments_inbox_alerts_created"),
        sa.CheckConstraint("expires_at >= created_at", name="ck_appointments_inbox_alerts_expires"),
    )
    op.create_index(_IX_ALERTS_EXPIRY, _INBOX_ALERTS, ["expires_at"])

    op.create_table(
        _POLLER_STATE,
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("next_watch_index", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("owner"),
        sa.ForeignKeyConstraint(["owner"], ["users.username"]),
        sa.CheckConstraint("next_watch_index >= 0", name="ck_appointments_poller_state_index"),
        sa.CheckConstraint("updated_at >= 0", name="ck_appointments_poller_state_updated"),
    )

    _replace_inbox_kind_constraint(_PREVIOUS_INBOX_KINDS + ", " + _APPOINTMENT_INBOX_KINDS)


def downgrade() -> None:
    op.execute(f"DELETE FROM {_INBOX_READS} WHERE kind IN ({_APPOINTMENT_INBOX_KINDS})")
    op.execute("DELETE FROM app_meta WHERE key IN ('appointments_generation', 'appointments_disabled_at')")
    _replace_inbox_kind_constraint(_PREVIOUS_INBOX_KINDS)
    op.drop_table(_POLLER_STATE)
    op.drop_index(_IX_ALERTS_EXPIRY, table_name=_INBOX_ALERTS)
    op.drop_table(_INBOX_ALERTS)
    op.drop_index(_IX_SLOTS_EXPIRY, table_name=_ACTIVE_SLOTS)
    op.drop_table(_ACTIVE_SLOTS)
    op.drop_index(_IX_WATCH_UPDATED, table_name=_WATCH_STATE)
    op.drop_table(_WATCH_STATE)
