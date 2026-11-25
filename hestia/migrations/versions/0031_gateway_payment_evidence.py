"""Gateway payment evidence columns on budget_receipts.

Revision ID: 0031
Revises: 0030
"""
from alembic import op
import sqlalchemy as sa

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

_RECEIPTS = "budget_receipts"
_REF_INDEX = "uq_budget_receipts_gateway_ref"
_INSERT_OWNER_TRIGGER = "budget_receipts_evidence_owner_insert"
_UPDATE_OWNER_TRIGGER = "budget_receipts_evidence_owner_update"
_COLUMNS = ("gateway", "gateway_status", "gateway_ref_hmac", "fee_amount", "payee_host",
            "evidence_for_receipt_id")
_INVALID_TARGET = f"""NEW.evidence_for_receipt_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM {_RECEIPTS}
    WHERE id = NEW.evidence_for_receipt_id AND owner = NEW.owner
)"""

# Inline CHECK/REFERENCES (the 0030 pattern): a batch rebuild would turn the inline
# REFERENCES on submitted_by into a table-level FK, which SQLite DROP COLUMN rejects,
# and a rebuild cannot DROP the parent table while child rows reference it with
# foreign keys ON. Inline constraints drop together with their column.
_PREDICATES = {
    "gateway": "gateway IS NULL OR length(gateway) BETWEEN 1 AND 64",
    "gateway_status": "gateway_status IS NULL OR (gateway IS NOT NULL "
                      "AND gateway_status IN ('registered', 'paid'))",
    "gateway_ref_hmac": "gateway_ref_hmac IS NULL OR (length(gateway_ref_hmac) = 64 "
                        "AND gateway_ref_hmac NOT GLOB '*[^0-9a-f]*')",
    "fee_amount": "fee_amount IS NULL OR (length(fee_amount) BETWEEN 1 AND 80 "
                  "AND fee_amount NOT GLOB '-*')",
    "payee_host": "payee_host IS NULL OR (length(payee_host) BETWEEN 1 AND 253 "
                  "AND instr(payee_host, '@') = 0 AND payee_host GLOB '*.*' "
                  "AND payee_host NOT GLOB '*[A-Z]*' AND payee_host NOT GLOB '*[^a-z0-9.-]*')",
}


def upgrade():
    op.execute(f"ALTER TABLE {_RECEIPTS} ADD COLUMN gateway VARCHAR(64) "
               f"CHECK ({_PREDICATES['gateway']})")
    op.execute(f"ALTER TABLE {_RECEIPTS} ADD COLUMN gateway_status VARCHAR "
               f"CHECK ({_PREDICATES['gateway_status']})")
    op.execute(f"ALTER TABLE {_RECEIPTS} ADD COLUMN gateway_ref_hmac VARCHAR(64) "
               f"CHECK ({_PREDICATES['gateway_ref_hmac']})")
    op.execute(f"ALTER TABLE {_RECEIPTS} ADD COLUMN fee_amount VARCHAR "
               f"CHECK ({_PREDICATES['fee_amount']})")
    op.execute(f"ALTER TABLE {_RECEIPTS} ADD COLUMN payee_host VARCHAR(253) "
               f"CHECK ({_PREDICATES['payee_host']})")
    # Single-column self-FK: a composite SET NULL would also null owner (NOT NULL).
    op.execute(f"ALTER TABLE {_RECEIPTS} ADD COLUMN evidence_for_receipt_id VARCHAR "
               f"REFERENCES {_RECEIPTS}(id) ON DELETE SET NULL")
    op.execute(f"""
        CREATE TRIGGER {_INSERT_OWNER_TRIGGER}
        BEFORE INSERT ON {_RECEIPTS}
        WHEN {_INVALID_TARGET}
        BEGIN
            SELECT RAISE(ABORT, 'invalid receipt evidence owner');
        END
    """)
    # Also protect existing evidence if its target's owner changes.
    op.execute(f"""
        CREATE TRIGGER {_UPDATE_OWNER_TRIGGER}
        BEFORE UPDATE OF evidence_for_receipt_id, owner ON {_RECEIPTS}
        WHEN ({_INVALID_TARGET}) OR (OLD.owner <> NEW.owner AND EXISTS (
            SELECT 1 FROM {_RECEIPTS}
            WHERE evidence_for_receipt_id = OLD.id AND owner <> NEW.owner
        ))
        BEGIN
            SELECT RAISE(ABORT, 'invalid receipt evidence owner');
        END
    """)
    op.create_index(_REF_INDEX, _RECEIPTS, ["owner", "gateway", "gateway_ref_hmac"], unique=True,
                    sqlite_where=sa.text("gateway_ref_hmac IS NOT NULL"))


def downgrade():
    op.execute(f"DROP TRIGGER IF EXISTS {_UPDATE_OWNER_TRIGGER}")
    op.execute(f"DROP TRIGGER IF EXISTS {_INSERT_OWNER_TRIGGER}")
    op.drop_index(_REF_INDEX, table_name=_RECEIPTS)
    for column in reversed(_COLUMNS):
        op.drop_column(_RECEIPTS, column)
