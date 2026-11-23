"""Owner-scoped Gmail grant, sync checkpoints, and receipt provenance.

Revision ID: 0024
Revises: 0023
"""
from alembic import op
import sqlalchemy as sa

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

_RECEIPTS = "budget_receipts"
_STATE = "budget_gmail_state"
_MESSAGES = "budget_gmail_messages"
_OWNER_DATE = "ix_budget_receipts_owner_date"
_MESSAGE_DATE = "ix_budget_gmail_messages_owner_date"
_MESSAGE_RFC = "ix_budget_gmail_messages_owner_rfc"
_DATE_GLOB = "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"

_STATUSES_V1A = "'draft', 'confirmed', 'needs_review'"
_SOURCES_V1A = "'live_text', 'local_ocr', 'cloud_ai'"
_ADDED_SOURCES = "'paste', 'photo', 'email', 'attachment'"
_CONFIRMED_FIELDS = ("receipt_date IS NOT NULL AND total_amount IS NOT NULL "
                     "AND currency IS NOT NULL AND payment_method IS NOT NULL")
_NO_LINK = ("linked_account_id IS NULL AND linked_entry_ref IS NULL AND linked_fingerprint IS NULL "
            "AND linked_evidence IS NULL")
_REQUIRED_LINK = ("linked_account_id IS NOT NULL AND linked_entry_ref IS NOT NULL "
                  "AND linked_fingerprint IS NOT NULL AND linked_evidence IS NOT NULL")


def _receipt_checks(batch, *, statuses, sources, unconfirmed, linked):
    checks = {
        "status": f"status IN ({statuses})",
        "source": f"source_kind IN ({sources})",
        "confirmed_fields": f"{unconfirmed} OR ({_CONFIRMED_FIELDS})",
        "link_fields": f"({_NO_LINK}) OR ({linked} AND {_REQUIRED_LINK})",
    }
    for suffix, predicate in checks.items():
        name = f"ck_budget_receipts_{suffix}"
        batch.drop_constraint(name, type_="check")
        batch.create_check_constraint(name, predicate)


def _begin_rebuild():
    op.execute('DROP TABLE IF EXISTS "_alembic_tmp_budget_receipts"')
    # SQLite reflection cannot round-trip the DESC on receipt_date.
    op.drop_index(_OWNER_DATE, table_name=_RECEIPTS)


def _end_rebuild():
    op.create_index(_OWNER_DATE, _RECEIPTS, ["owner", sa.text("receipt_date DESC"), "id"])


def upgrade():
    _begin_rebuild()
    with op.batch_alter_table(_RECEIPTS) as batch:
        _receipt_checks(batch, statuses=f"{_STATUSES_V1A}, 'imported'", sources=f"{_SOURCES_V1A}, {_ADDED_SOURCES}",
                        unconfirmed="status IN ('draft', 'imported')", linked="status IN ('confirmed', 'needs_review')")
        batch.add_column(sa.Column("document_kind", sa.String(), nullable=False, server_default=sa.text("'receipt'")))
        batch.add_column(sa.Column("import_expires_at", sa.Float(), nullable=True))
        batch.add_column(sa.Column("invoice_number", sa.String(), nullable=True))
        batch.add_column(sa.Column("due_date", sa.String(), nullable=True))
        batch.create_check_constraint("ck_budget_receipts_document_kind",
                                      "document_kind IN ('receipt', 'invoice', 'payment_confirmation')")
        batch.create_check_constraint("ck_budget_receipts_import_expiry",
                                      "import_expires_at IS NULL OR import_expires_at >= 0")
        batch.create_check_constraint("ck_budget_receipts_invoice_number",
                                      "invoice_number IS NULL OR length(invoice_number) BETWEEN 1 AND 128")
        batch.create_check_constraint("ck_budget_receipts_due_date", f"due_date IS NULL OR due_date GLOB {_DATE_GLOB}")
    _end_rebuild()

    op.create_table(
        _STATE,
        sa.Column("owner", sa.String(), sa.ForeignKey("users.username"), primary_key=True),
        sa.Column("grant_status", sa.String(), nullable=False, server_default=sa.text("'disabled'")),
        sa.Column("grant_generation", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("granted_at", sa.Float(), nullable=True),
        sa.Column("revoked_at", sa.Float(), nullable=True),
        sa.Column("mailbox_hmac", sa.String(), nullable=True),
        sa.Column("sync_phase", sa.String(), nullable=False, server_default=sa.text("'idle'")),
        sa.Column("history_id", sa.String(), nullable=True),
        sa.Column("pending_history_id", sa.String(), nullable=True),
        sa.Column("resume_page_token", sa.String(), nullable=True),
        sa.Column("history_page_token", sa.String(), nullable=True),
        sa.Column("scope_scan_page_token", sa.String(), nullable=True),
        sa.Column("scope_scan_query_hmac", sa.String(), nullable=True),
        sa.Column("resume_cutoff_ts", sa.Float(), nullable=True),
        sa.Column("batch_processed_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_sync_at", sa.Float(), nullable=True),
        sa.Column("last_attempt_at", sa.Float(), nullable=True),
        sa.Column("next_sync_at", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("backoff_until", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("failures", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("attention", sa.String(), nullable=True),
        sa.Column("last_http_status", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("backfill_completed_at", sa.Float(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.CheckConstraint("grant_status IN ('disabled', 'active', 'revoked')", name="ck_budget_gmail_state_grant_status"),
        sa.CheckConstraint("grant_generation >= 0", name="ck_budget_gmail_state_grant_generation"),
        sa.CheckConstraint("granted_at IS NULL OR granted_at >= 0", name="ck_budget_gmail_state_granted_at"),
        sa.CheckConstraint("revoked_at IS NULL OR revoked_at >= 0", name="ck_budget_gmail_state_revoked_at"),
        sa.CheckConstraint("mailbox_hmac IS NULL OR (length(mailbox_hmac) = 64 AND mailbox_hmac NOT GLOB '*[^0-9a-f]*')",
                           name="ck_budget_gmail_state_mailbox_hmac"),
        sa.CheckConstraint("sync_phase IN ('idle', 'backfill', 'incremental', 'full_resync')",
                           name="ck_budget_gmail_state_sync_phase"),
        sa.CheckConstraint("history_id IS NULL OR length(history_id) BETWEEN 1 AND 64", name="ck_budget_gmail_state_history_id"),
        sa.CheckConstraint("pending_history_id IS NULL OR length(pending_history_id) BETWEEN 1 AND 64",
                           name="ck_budget_gmail_state_pending_history_id"),
        sa.CheckConstraint("resume_page_token IS NULL OR length(resume_page_token) BETWEEN 1 AND 512",
                           name="ck_budget_gmail_state_resume_page_token"),
        sa.CheckConstraint("history_page_token IS NULL OR length(history_page_token) BETWEEN 1 AND 512",
                           name="ck_budget_gmail_state_history_page_token"),
        sa.CheckConstraint("scope_scan_page_token IS NULL OR length(scope_scan_page_token) BETWEEN 1 AND 512",
                           name="ck_budget_gmail_state_scope_scan_page_token"),
        sa.CheckConstraint("scope_scan_query_hmac IS NULL OR (length(scope_scan_query_hmac) = 64 "
                           "AND scope_scan_query_hmac NOT GLOB '*[^0-9a-f]*')", name="ck_budget_gmail_state_scope_scan_query_hmac"),
        sa.CheckConstraint("resume_cutoff_ts IS NULL OR resume_cutoff_ts >= 0", name="ck_budget_gmail_state_resume_cutoff_ts"),
        sa.CheckConstraint("batch_processed_count >= 0", name="ck_budget_gmail_state_batch_processed_count"),
        sa.CheckConstraint("last_sync_at IS NULL OR last_sync_at >= 0", name="ck_budget_gmail_state_last_sync_at"),
        sa.CheckConstraint("last_attempt_at IS NULL OR last_attempt_at >= 0", name="ck_budget_gmail_state_last_attempt_at"),
        sa.CheckConstraint("next_sync_at >= 0", name="ck_budget_gmail_state_next_sync_at"),
        sa.CheckConstraint("backoff_until >= 0", name="ck_budget_gmail_state_backoff_until"),
        sa.CheckConstraint("failures >= 0", name="ck_budget_gmail_state_failures"),
        sa.CheckConstraint("attention IS NULL OR attention IN ('authorization', 'configuration', 'identity_mismatch', 'quota')",
                           name="ck_budget_gmail_state_attention"),
        sa.CheckConstraint("last_http_status IS NULL OR last_http_status BETWEEN 100 AND 599",
                           name="ck_budget_gmail_state_last_http_status"),
        sa.CheckConstraint("last_error IS NULL OR length(last_error) <= 64", name="ck_budget_gmail_state_last_error"),
        sa.CheckConstraint("backfill_completed_at IS NULL OR backfill_completed_at >= 0",
                           name="ck_budget_gmail_state_backfill_completed_at"),
        sa.CheckConstraint("version >= 1", name="ck_budget_gmail_state_version"),
    )

    op.create_table(
        _MESSAGES,
        sa.Column("owner", sa.String(), sa.ForeignKey("users.username"), primary_key=True),
        sa.Column("gmail_id_hmac", sa.String(), primary_key=True),
        sa.Column("rfc_message_id_hmac", sa.String(), nullable=True),
        sa.Column("internal_date", sa.Float(), nullable=False),
        sa.Column("trust_path", sa.String(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("receipt_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["receipt_id", "owner"], ["budget_receipts.id", "budget_receipts.owner"],
                                name="fk_budget_gmail_messages_receipt_owner"),
        sa.CheckConstraint("length(gmail_id_hmac) = 64 AND gmail_id_hmac NOT GLOB '*[^0-9a-f]*'",
                           name="ck_budget_gmail_messages_gmail_id_hmac"),
        sa.CheckConstraint("rfc_message_id_hmac IS NULL OR (length(rfc_message_id_hmac) = 64 "
                           "AND rfc_message_id_hmac NOT GLOB '*[^0-9a-f]*')", name="ck_budget_gmail_messages_rfc_message_id_hmac"),
        sa.CheckConstraint("internal_date >= 0", name="ck_budget_gmail_messages_internal_date"),
        sa.CheckConstraint("trust_path IN ('direct_allowlist', 'spouse_forward', 'untrusted')",
                           name="ck_budget_gmail_messages_trust_path"),
        sa.CheckConstraint("outcome IN ('imported', 'skipped_non_receipt', 'skipped_untrusted_sender', "
                           "'skipped_oversized', 'skipped_duplicate', 'parse_failed', 'deleted')",
                           name="ck_budget_gmail_messages_outcome"),
        sa.CheckConstraint("created_at >= 0", name="ck_budget_gmail_messages_created_at"),
    )
    op.create_index(_MESSAGE_DATE, _MESSAGES, ["owner", "internal_date"])
    op.create_index(_MESSAGE_RFC, _MESSAGES, ["owner", "rfc_message_id_hmac"],
                    sqlite_where=sa.text("rfc_message_id_hmac IS NOT NULL"))


def downgrade():
    connection = op.get_bind()
    receipts, messages, grants = connection.exec_driver_sql(
        "SELECT (SELECT COUNT(*) FROM budget_receipts WHERE status = 'imported' "
        f"OR source_kind IN ({_ADDED_SOURCES}) OR document_kind != 'receipt' "
        "OR import_expires_at IS NOT NULL OR invoice_number IS NOT NULL OR due_date IS NOT NULL), "
        "(SELECT COUNT(*) FROM budget_gmail_messages), "
        "(SELECT COUNT(*) FROM budget_gmail_state WHERE grant_status = 'active')"
    ).one()
    if receipts or messages or grants:
        raise RuntimeError(
            f"Cannot downgrade receipts v1d-a with {receipts} incompatible receipts, "
            f"{messages} Gmail provenance rows and {grants} active grants. "
            "Revoke Gmail and explicitly delete remaining v1d receipts first."
        )
    op.drop_index(_MESSAGE_RFC, table_name=_MESSAGES)
    op.drop_index(_MESSAGE_DATE, table_name=_MESSAGES)
    op.drop_table(_MESSAGES)
    op.drop_table(_STATE)

    _begin_rebuild()
    with op.batch_alter_table(_RECEIPTS) as batch:
        _receipt_checks(batch, statuses=_STATUSES_V1A, sources=_SOURCES_V1A,
                        unconfirmed="status = 'draft'", linked="status != 'draft'")
        for constraint in ("document_kind", "import_expiry", "invoice_number", "due_date"):
            batch.drop_constraint(f"ck_budget_receipts_{constraint}", type_="check")
        for column in ("document_kind", "import_expires_at", "invoice_number", "due_date"):
            batch.drop_column(column)
    _end_rebuild()
