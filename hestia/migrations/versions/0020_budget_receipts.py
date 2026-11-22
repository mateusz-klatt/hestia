"""Owner-private receipt drafts, structured lines and informational bank links.

Revision ID: 0020
Revises: 0019
"""
from alembic import op
import sqlalchemy as sa

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

_SHOPS = "budget_shops"
_RECEIPTS = "budget_receipts"
_LINES = "budget_receipt_lines"
_IX_SHOPS_OWNER_CHAIN = "ix_budget_shops_owner_chain"
_IX_RECEIPTS_OWNER_DATE = "ix_budget_receipts_owner_date"
_UQ_RECEIPTS_TX_LINK = "uq_budget_receipts_tx_link"
_UQ_ACCOUNTS_ID_OWNER = "uq_budget_accounts_id_owner"


def _real_transaction():
    """Open a real SQLite transaction before the first DDL statement.

    ``env.py`` already does this for the whole revision, including Alembic's
    version update. Repeating the request here keeps upgrade and downgrade
    atomic when a caller reaches the revision without that wrapper: pysqlite
    must be out of implicit-commit mode and ``BEGIN`` must already be open.
    Assigning ``isolation_level`` commits a caller's open transaction, so that
    case is refused. A transaction this migration already opened stays open so
    the version update commits with the DDL.
    """
    connection = op.get_bind()
    raw = connection.connection.dbapi_connection
    if raw.in_transaction and raw.isolation_level is not None:
        raise RuntimeError("refusing to migrate while a transaction is open")
    if raw.isolation_level is not None:
        raw.isolation_level = None
    if not raw.in_transaction:
        connection.exec_driver_sql("BEGIN")


def upgrade():
    _real_transaction()
    op.create_index(_UQ_ACCOUNTS_ID_OWNER, "budget_accounts", ["id", "owner"], unique=True)
    op.create_table(
        _SHOPS,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("chain_key", sa.String(), nullable=True),
        sa.Column("nip", sa.String(), nullable=True),
        sa.Column("merchant_tokens", sa.Text(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["owner"], ["users.username"]),
        sa.UniqueConstraint("id", "owner", name="uq_budget_shops_id_owner"),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 128", name="ck_budget_shops_name_len"),
        sa.CheckConstraint("chain_key IS NULL OR length(chain_key) BETWEEN 1 AND 64",
                           name="ck_budget_shops_chain_key_len"),
        sa.CheckConstraint(
            "nip IS NULL OR (length(nip) = 10 AND nip GLOB "
            "'[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]')",
            name="ck_budget_shops_nip",
        ),
        sa.CheckConstraint("version >= 1", name="ck_budget_shops_version"),
    )
    op.create_index(_IX_SHOPS_OWNER_CHAIN, _SHOPS, ["owner", "chain_key"])

    op.create_table(
        _RECEIPTS,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("shop_id", sa.String(), nullable=True),
        sa.Column("raw_shop_name", sa.String(), nullable=True),
        sa.Column("raw_nip", sa.String(), nullable=True),
        sa.Column("receipt_kind", sa.String(), nullable=False, server_default=sa.text("'purchase'")),
        sa.Column("receipt_date", sa.String(), nullable=True),
        sa.Column("receipt_time", sa.String(), nullable=True),
        sa.Column("total_amount", sa.String(), nullable=True),
        sa.Column("currency", sa.String(), nullable=True),
        sa.Column("payment_method", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("source_kind", sa.String(), nullable=False),
        sa.Column("image_relpath", sa.String(), nullable=True),
        sa.Column("image_expires_at", sa.Float(), nullable=True),
        sa.Column("raw_ocr_text", sa.Text(), nullable=True),
        sa.Column("raw_text_expires_at", sa.Float(), nullable=True),
        sa.Column("linked_account_id", sa.String(), nullable=True),
        sa.Column("linked_entry_ref", sa.String(), nullable=True),
        sa.Column("linked_fingerprint", sa.String(), nullable=True),
        sa.Column("linked_evidence", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["owner"], ["users.username"]),
        sa.ForeignKeyConstraint(["shop_id", "owner"], [f"{_SHOPS}.id", f"{_SHOPS}.owner"],
                                name="fk_budget_receipts_shop_owner"),
        sa.ForeignKeyConstraint(["linked_account_id", "linked_entry_ref"],
                                ["budget_transactions.account_id", "budget_transactions.entry_reference"],
                                name="fk_budget_receipts_transaction"),
        sa.ForeignKeyConstraint(["linked_account_id", "owner"],
                                ["budget_accounts.id", "budget_accounts.owner"],
                                name="fk_budget_receipts_account_owner"),
        sa.UniqueConstraint("id", "owner", name="uq_budget_receipts_id_owner"),
        sa.CheckConstraint("raw_shop_name IS NULL OR length(raw_shop_name) <= 128",
                           name="ck_budget_receipts_raw_shop_name_len"),
        sa.CheckConstraint("receipt_kind IN ('purchase', 'refund')", name="ck_budget_receipts_kind"),
        sa.CheckConstraint(
            "receipt_date IS NULL OR (length(receipt_date) = 10 AND receipt_date GLOB "
            "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')",
            name="ck_budget_receipts_date",
        ),
        sa.CheckConstraint("total_amount IS NULL OR length(total_amount) BETWEEN 1 AND 80",
                           name="ck_budget_receipts_total_len"),
        sa.CheckConstraint("currency IS NULL OR (length(currency) = 3 AND currency GLOB '[A-Z][A-Z][A-Z]')",
                           name="ck_budget_receipts_currency"),
        sa.CheckConstraint("payment_method IS NULL OR payment_method IN ('card', 'blik', 'cash', 'mixed', 'unknown')",
                           name="ck_budget_receipts_payment"),
        sa.CheckConstraint("status IN ('draft', 'confirmed', 'needs_review')", name="ck_budget_receipts_status"),
        sa.CheckConstraint("source_kind IN ('live_text', 'local_ocr', 'cloud_ai')", name="ck_budget_receipts_source"),
        sa.CheckConstraint(
            "status = 'draft' OR (receipt_date IS NOT NULL AND total_amount IS NOT NULL "
            "AND currency IS NOT NULL AND payment_method IS NOT NULL)",
            name="ck_budget_receipts_confirmed_fields",
        ),
        sa.CheckConstraint(
            "(linked_account_id IS NULL AND linked_entry_ref IS NULL AND linked_fingerprint IS NULL "
            "AND linked_evidence IS NULL) "
            "OR (status != 'draft' AND linked_account_id IS NOT NULL AND linked_entry_ref IS NOT NULL "
            "AND linked_fingerprint IS NOT NULL AND linked_evidence IS NOT NULL)",
            name="ck_budget_receipts_link_fields",
        ),
        sa.CheckConstraint(
            "linked_evidence IS NULL OR length(linked_evidence) BETWEEN 1 AND 4096",
            name="ck_budget_receipts_evidence_len",
        ),
        sa.CheckConstraint(
            "raw_ocr_text IS NULL OR raw_text_expires_at IS NOT NULL",
            name="ck_budget_receipts_raw_text_expiry",
        ),
        sa.CheckConstraint("version >= 1", name="ck_budget_receipts_version"),
    )
    op.create_index(_IX_RECEIPTS_OWNER_DATE, _RECEIPTS, ["owner", sa.text("receipt_date DESC"), "id"])
    op.create_index(
        _UQ_RECEIPTS_TX_LINK, _RECEIPTS, ["linked_account_id", "linked_entry_ref"], unique=True,
        sqlite_where=sa.text(
            "status IN ('confirmed', 'needs_review') AND linked_account_id IS NOT NULL "
            "AND linked_entry_ref IS NOT NULL"
        ),
    )

    op.create_table(
        _LINES,
        sa.Column("receipt_id", sa.String(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("line_kind", sa.String(), nullable=False),
        sa.Column("raw_name", sa.String(), nullable=False),
        sa.Column("raw_name_key", sa.String(), nullable=False),
        sa.Column("ptu_code", sa.String(), nullable=True),
        sa.Column("quantity", sa.String(), nullable=False),
        sa.Column("raw_unit", sa.String(), nullable=True),
        sa.Column("unit_price_raw", sa.String(), nullable=True),
        sa.Column("gross_line_total", sa.String(), nullable=False),
        sa.Column("discount_amount", sa.String(), nullable=False, server_default=sa.text("'0'")),
        sa.Column("net_line_total", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("receipt_id", "line_no"),
        sa.ForeignKeyConstraint(["owner"], ["users.username"]),
        sa.ForeignKeyConstraint(["receipt_id", "owner"], [f"{_RECEIPTS}.id", f"{_RECEIPTS}.owner"],
                                name="fk_budget_receipt_lines_receipt_owner"),
        sa.CheckConstraint("line_no BETWEEN 1 AND 300", name="ck_budget_receipt_lines_no"),
        sa.CheckConstraint("line_kind IN ('item', 'deposit', 'basket_discount', 'other')",
                           name="ck_budget_receipt_lines_kind"),
        sa.CheckConstraint("length(raw_name) BETWEEN 1 AND 128", name="ck_budget_receipt_lines_name_len"),
        sa.CheckConstraint("length(raw_name_key) BETWEEN 1 AND 128", name="ck_budget_receipt_lines_name_key_len"),
        sa.CheckConstraint("ptu_code IS NULL OR ptu_code IN ('A', 'B', 'C', 'D', 'E', 'F', 'G')",
                           name="ck_budget_receipt_lines_ptu"),
    )


def downgrade():
    _real_transaction()
    op.drop_index(_UQ_RECEIPTS_TX_LINK, table_name=_RECEIPTS)
    op.drop_index(_IX_RECEIPTS_OWNER_DATE, table_name=_RECEIPTS)
    op.drop_table(_LINES)
    op.drop_table(_RECEIPTS)
    op.drop_index(_IX_SHOPS_OWNER_CHAIN, table_name=_SHOPS)
    op.drop_table(_SHOPS)
    op.drop_index(_UQ_ACCOUNTS_ID_OWNER, table_name="budget_accounts")
