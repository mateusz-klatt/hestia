"""Queue fields for a Gmail import, plus owner/spouse manual forwards.

Revision ID: 0025
Revises: 0024

``batch_alter_table`` rebuilds ``budget_gmail_state``, ``budget_receipts`` and
``budget_gmail_messages``. Upgrade and downgrade drop a leftover
``_alembic_tmp_*`` table first. ``init_db`` and ``downgrade_db`` run those
rebuilds with foreign keys off, inside an explicit transaction, then
``PRAGMA foreign_key_check``.

Downgrade drops the scheduling columns and the manual-forward columns. A queued
run does not survive. Downgrade refuses while a manual forward or an
``owner_forward`` provenance row remains.
"""
from alembic import op
import sqlalchemy as sa

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

_STATE = "budget_gmail_state"
_RECEIPTS = "budget_receipts"
_MESSAGES = "budget_gmail_messages"
_OWNER_DATE = "ix_budget_receipts_owner_date"
_CONFIRMED = ("receipt_date IS NOT NULL AND total_amount IS NOT NULL "
              "AND currency IS NOT NULL AND payment_method IS NOT NULL")
_MANUAL_CONFIRMED = ("status IN ('draft', 'imported') OR (forwarded_manually = 1 AND status = 'needs_review') "
                     f"OR ({_CONFIRMED})")
_IMPORTED_CONFIRMED = f"status IN ('draft', 'imported') OR ({_CONFIRMED})"
_DOMAIN = ("forwarded_sender_domain IS NULL OR (length(forwarded_sender_domain) BETWEEN 1 AND 253 "
           "AND instr(forwarded_sender_domain, '@') = 0 AND forwarded_sender_domain GLOB '*.*' "
           "AND forwarded_sender_domain NOT GLOB '*[A-Z]*' "
           "AND forwarded_sender_domain NOT GLOB '*[^a-z0-9.-]*')")
_TRUST_NEW = "trust_path IN ('direct_allowlist', 'spouse_forward', 'owner_forward', 'untrusted')"
_TRUST_OLD = "trust_path IN ('direct_allowlist', 'spouse_forward', 'untrusted')"


def _drop_leftover_batch_table(table):
    temp_name = ("_alembic_tmp_%s" % table)[:50]
    op.execute(f'DROP TABLE IF EXISTS "{temp_name}"')


def _begin_receipts():
    _drop_leftover_batch_table(_RECEIPTS)
    op.drop_index(_OWNER_DATE, table_name=_RECEIPTS)


def _end_receipts():
    op.create_index(_OWNER_DATE, _RECEIPTS, ["owner", sa.text("receipt_date DESC"), "id"])


def upgrade():
    _drop_leftover_batch_table(_STATE)
    with op.batch_alter_table(_STATE) as batch:
        batch.add_column(sa.Column("run_requested", sa.Integer(), nullable=False, server_default=sa.text("0")))
        batch.add_column(sa.Column("force_full_resync", sa.Integer(), nullable=False, server_default=sa.text("0")))
        batch.create_check_constraint("ck_budget_gmail_state_run_requested", "run_requested IN (0, 1)")
        batch.create_check_constraint("ck_budget_gmail_state_force_full_resync", "force_full_resync IN (0, 1)")

    _begin_receipts()
    with op.batch_alter_table(_RECEIPTS) as batch:
        batch.add_column(sa.Column("forwarded_manually", sa.Integer(), nullable=False, server_default=sa.text("0")))
        batch.add_column(sa.Column("forwarded_sender_domain", sa.String(), nullable=True))
        batch.drop_constraint("ck_budget_receipts_confirmed_fields", type_="check")
        batch.create_check_constraint("ck_budget_receipts_confirmed_fields", _MANUAL_CONFIRMED)
        batch.create_check_constraint("ck_budget_receipts_forwarded_manually", "forwarded_manually IN (0, 1)")
        batch.create_check_constraint("ck_budget_receipts_forwarded_sender_domain", _DOMAIN)
    _end_receipts()

    _drop_leftover_batch_table(_MESSAGES)
    with op.batch_alter_table(_MESSAGES) as batch:
        batch.drop_constraint("ck_budget_gmail_messages_trust_path", type_="check")
        batch.create_check_constraint("ck_budget_gmail_messages_trust_path", _TRUST_NEW)


def downgrade():
    connection = op.get_bind()
    manual, owner_forwards = connection.exec_driver_sql(
        "SELECT (SELECT COUNT(*) FROM budget_receipts WHERE forwarded_manually != 0 "
        "OR forwarded_sender_domain IS NOT NULL), "
        "(SELECT COUNT(*) FROM budget_gmail_messages WHERE trust_path = 'owner_forward')"
    ).one()
    if manual or owner_forwards:
        raise RuntimeError(
            f"Cannot downgrade Gmail follow-ups with {manual} manual forwards and "
            f"{owner_forwards} owner-forward provenance rows. Delete those rows first."
        )
    _drop_leftover_batch_table(_MESSAGES)
    with op.batch_alter_table(_MESSAGES) as batch:
        batch.drop_constraint("ck_budget_gmail_messages_trust_path", type_="check")
        batch.create_check_constraint("ck_budget_gmail_messages_trust_path", _TRUST_OLD)

    _begin_receipts()
    with op.batch_alter_table(_RECEIPTS) as batch:
        batch.drop_constraint("ck_budget_receipts_forwarded_sender_domain", type_="check")
        batch.drop_constraint("ck_budget_receipts_forwarded_manually", type_="check")
        batch.drop_constraint("ck_budget_receipts_confirmed_fields", type_="check")
        batch.create_check_constraint("ck_budget_receipts_confirmed_fields", _IMPORTED_CONFIRMED)
        batch.drop_column("forwarded_sender_domain")
        batch.drop_column("forwarded_manually")
    _end_receipts()

    _drop_leftover_batch_table(_STATE)
    with op.batch_alter_table(_STATE) as batch:
        batch.drop_constraint("ck_budget_gmail_state_force_full_resync", type_="check")
        batch.drop_constraint("ck_budget_gmail_state_run_requested", type_="check")
        batch.drop_column("force_full_resync")
        batch.drop_column("run_requested")
