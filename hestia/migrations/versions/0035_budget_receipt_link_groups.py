"""Add receipt link groups and mutually exclusive transaction reservations.

Revision ID: 0035
Revises: 0034
"""
from alembic import op
import sqlalchemy as sa

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

_GROUPS = "budget_receipt_link_groups"
_MEMBERS = "budget_receipt_link_members"
_EVIDENCE_INDEX = "uq_budget_receipt_link_groups_evidence"
_OWNER_STATUS_INDEX = "ix_budget_receipt_link_groups_owner_status"
_MEMBER_GROUP_INDEX = "ix_budget_receipt_link_members_group"

# Both active states reserve the transaction. Check each relation from both
# sides so an INSERT, UPDATE, or same-key replacement cannot bypass it.
_GUARDS = (
    (_GROUPS, "owner, account_id, entry_reference, evidence_receipt_id, status", """
        SELECT RAISE(ABORT, 'group transaction already has a receipt link') WHERE EXISTS (
            SELECT 1 FROM budget_receipts
            WHERE linked_account_id = NEW.account_id AND linked_entry_ref = NEW.entry_reference
              AND status IN ('confirmed', 'needs_review')
        );
        SELECT RAISE(ABORT, 'group transaction is a transfer leg') WHERE EXISTS (
            SELECT 1 FROM budget_transfer_legs
            WHERE account_id = NEW.account_id AND entry_reference = NEW.entry_reference
        );
        SELECT RAISE(ABORT, 'receipt is not eligible for group evidence')
        WHERE NEW.evidence_receipt_id IS NOT NULL AND (
            EXISTS (SELECT 1 FROM budget_receipt_link_members WHERE receipt_id = NEW.evidence_receipt_id)
            OR NOT EXISTS (
                SELECT 1 FROM budget_receipts
                WHERE id = NEW.evidence_receipt_id AND owner = NEW.owner
                  AND (status = 'confirmed' OR (status = 'needs_review' AND NEW.status = 'needs_review'))
                  AND gateway IS NOT NULL AND linked_account_id IS NULL AND evidence_for_receipt_id IS NULL
            )
        );
    """),
    (_MEMBERS, "receipt_id, owner", """
        SELECT RAISE(ABORT, 'receipt is not eligible for group membership') WHERE (
            EXISTS (SELECT 1 FROM budget_receipt_link_groups WHERE evidence_receipt_id = NEW.receipt_id)
            OR NOT EXISTS (
                SELECT 1 FROM budget_receipts
                WHERE id = NEW.receipt_id AND owner = NEW.owner AND status IN ('confirmed', 'needs_review')
                  AND gateway IS NULL AND linked_account_id IS NULL AND evidence_for_receipt_id IS NULL
            )
        );
    """),
    ("budget_receipts", "id, status, gateway, linked_account_id, linked_entry_ref, evidence_for_receipt_id", """
        SELECT RAISE(ABORT, 'receipt transaction is reserved by a group')
        WHERE NEW.status IN ('confirmed', 'needs_review') AND EXISTS (
            SELECT 1 FROM budget_receipt_link_groups
            WHERE account_id = NEW.linked_account_id AND entry_reference = NEW.linked_entry_ref
        );
        SELECT RAISE(ABORT, 'receipt is not eligible for group membership')
        WHERE (NEW.status NOT IN ('confirmed', 'needs_review') OR NEW.gateway IS NOT NULL
               OR NEW.linked_account_id IS NOT NULL OR NEW.evidence_for_receipt_id IS NOT NULL)
          AND EXISTS (SELECT 1 FROM budget_receipt_link_members WHERE receipt_id = NEW.id);
        SELECT RAISE(ABORT, 'receipt is not eligible for group evidence') WHERE EXISTS (
            SELECT 1 FROM budget_receipt_link_groups
            WHERE evidence_receipt_id = NEW.id AND (
                NEW.status NOT IN ('confirmed', 'needs_review')
                OR (NEW.status = 'needs_review' AND status = 'confirmed')
                OR NEW.gateway IS NULL OR NEW.linked_account_id IS NOT NULL OR NEW.evidence_for_receipt_id IS NOT NULL
            )
        );
    """),
    ("budget_transfer_legs", "account_id, entry_reference", """
        SELECT RAISE(ABORT, 'transfer transaction is reserved by a group') WHERE EXISTS (
            SELECT 1 FROM budget_receipt_link_groups
            WHERE account_id = NEW.account_id AND entry_reference = NEW.entry_reference
        );
    """),
)


def upgrade() -> None:
    op.create_table(
        _GROUPS,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("entry_reference", sa.String(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("tx_amount", sa.String(), nullable=False),
        sa.Column("credit_debit_indicator", sa.String(), nullable=False),
        sa.Column("fee_amount", sa.String()),
        sa.Column("evidence_receipt_id", sa.String()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("linked_fingerprint", sa.String(), nullable=False),
        sa.Column("linked_evidence", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("id", "owner", name="uq_budget_receipt_link_groups_id_owner"),
        sa.UniqueConstraint("account_id", "entry_reference", name="uq_budget_receipt_link_groups_tx"),
        sa.ForeignKeyConstraint(["owner"], ["users.username"]),
        sa.ForeignKeyConstraint(["account_id", "entry_reference"],
                                ["budget_transactions.account_id", "budget_transactions.entry_reference"],
                                name="fk_budget_receipt_link_groups_tx"),
        sa.ForeignKeyConstraint(["account_id", "owner"], ["budget_accounts.id", "budget_accounts.owner"],
                                name="fk_budget_receipt_link_groups_account_owner"),
        sa.ForeignKeyConstraint(["evidence_receipt_id", "owner"], ["budget_receipts.id", "budget_receipts.owner"],
                                name="fk_budget_receipt_link_groups_evidence_owner"),
        sa.CheckConstraint("length(currency) = 3 AND currency GLOB '[A-Z][A-Z][A-Z]'",
                           name="ck_budget_receipt_link_groups_currency"),
        sa.CheckConstraint("length(tx_amount) BETWEEN 1 AND 80", name="ck_budget_receipt_link_groups_amount"),
        sa.CheckConstraint("credit_debit_indicator IN ('DBIT', 'CRDT')", name="ck_budget_receipt_link_groups_direction"),
        sa.CheckConstraint("fee_amount IS NULL OR (length(fee_amount) BETWEEN 1 AND 80 AND fee_amount NOT GLOB '-*')",
                           name="ck_budget_receipt_link_groups_fee"),
        sa.CheckConstraint("status IN ('confirmed', 'needs_review')", name="ck_budget_receipt_link_groups_status"),
        sa.CheckConstraint("length(linked_evidence) BETWEEN 1 AND 4096", name="ck_budget_receipt_link_groups_evidence"),
        sa.CheckConstraint("version >= 1", name="ck_budget_receipt_link_groups_version"),
    )
    op.create_index(_EVIDENCE_INDEX, _GROUPS, ["evidence_receipt_id"], unique=True,
                    sqlite_where=sa.text("evidence_receipt_id IS NOT NULL"))
    op.create_index(_OWNER_STATUS_INDEX, _GROUPS, ["owner", "status", "id"])
    op.create_table(
        _MEMBERS,
        sa.Column("group_id", sa.String(), nullable=False),
        sa.Column("receipt_id", sa.String(), primary_key=True),
        sa.Column("owner", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["group_id", "owner"], [f"{_GROUPS}.id", f"{_GROUPS}.owner"],
                                name="fk_budget_receipt_link_members_group_owner"),
        sa.ForeignKeyConstraint(["receipt_id", "owner"], ["budget_receipts.id", "budget_receipts.owner"],
                                name="fk_budget_receipt_link_members_receipt_owner"),
    )
    op.create_index(_MEMBER_GROUP_INDEX, _MEMBERS, ["group_id"])
    for table, columns, body in _GUARDS:
        for suffix, operation in (("insert", "INSERT"), ("update", f"UPDATE OF {columns}")):
            op.execute(f"""
                CREATE TRIGGER {table}_link_group_guard_{suffix}
                BEFORE {operation} ON {table}
                BEGIN
                    {body}
                END
            """)


def downgrade() -> None:
    populated = op.get_bind().exec_driver_sql(f"SELECT 1 FROM {_GROUPS} UNION ALL SELECT 1 FROM {_MEMBERS} LIMIT 1")
    if populated.first() is not None:
        raise RuntimeError("Cannot downgrade 0035 with receipt link groups. Unlink all groups first.")
    for table, _, _ in _GUARDS:
        for suffix in ("insert", "update"):
            op.execute(f"DROP TRIGGER {table}_link_group_guard_{suffix}")
    op.drop_index(_MEMBER_GROUP_INDEX, table_name=_MEMBERS)
    op.drop_table(_MEMBERS)
    op.drop_index(_OWNER_STATUS_INDEX, table_name=_GROUPS)
    op.drop_index(_EVIDENCE_INDEX, table_name=_GROUPS)
    op.drop_table(_GROUPS)
