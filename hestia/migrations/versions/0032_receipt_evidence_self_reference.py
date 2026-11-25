"""Reject self-referencing receipt evidence pointers in SQLite triggers.

Revision ID: 0032
Revises: 0031
"""
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

_RECEIPTS = "budget_receipts"
_INSERT_OWNER_TRIGGER = "budget_receipts_evidence_owner_insert"
_UPDATE_OWNER_TRIGGER = "budget_receipts_evidence_owner_update"
_INVALID_OWNER_0031 = f"""NEW.evidence_for_receipt_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM {_RECEIPTS}
    WHERE id = NEW.evidence_for_receipt_id AND owner = NEW.owner
)"""
_INVALID_TARGET_0032 = f"""NEW.evidence_for_receipt_id IS NOT NULL AND (
    NEW.evidence_for_receipt_id = NEW.id OR NOT EXISTS (
        SELECT 1 FROM {_RECEIPTS}
        WHERE id = NEW.evidence_for_receipt_id AND owner = NEW.owner
    )
)"""


def upgrade():
    op.execute(f"DROP TRIGGER IF EXISTS {_UPDATE_OWNER_TRIGGER}")
    op.execute(f"DROP TRIGGER IF EXISTS {_INSERT_OWNER_TRIGGER}")
    op.execute(f"""
        CREATE TRIGGER {_INSERT_OWNER_TRIGGER}
        BEFORE INSERT ON {_RECEIPTS}
        WHEN {_INVALID_TARGET_0032}
        BEGIN
            SELECT CASE
                WHEN NEW.evidence_for_receipt_id = NEW.id
                THEN RAISE(ABORT, 'invalid receipt evidence self-reference')
                ELSE RAISE(ABORT, 'invalid receipt evidence owner')
            END;
        END
    """)
    op.execute(f"""
        CREATE TRIGGER {_UPDATE_OWNER_TRIGGER}
        BEFORE UPDATE OF id, evidence_for_receipt_id, owner ON {_RECEIPTS}
        WHEN ({_INVALID_TARGET_0032}) OR (OLD.owner <> NEW.owner AND EXISTS (
            SELECT 1 FROM {_RECEIPTS}
            WHERE evidence_for_receipt_id = OLD.id AND owner <> NEW.owner
        ))
        BEGIN
            SELECT CASE
                WHEN NEW.evidence_for_receipt_id IS NOT NULL AND NEW.evidence_for_receipt_id = NEW.id
                THEN RAISE(ABORT, 'invalid receipt evidence self-reference')
                ELSE RAISE(ABORT, 'invalid receipt evidence owner')
            END;
        END
    """)


def downgrade():
    op.execute(f"DROP TRIGGER IF EXISTS {_UPDATE_OWNER_TRIGGER}")
    op.execute(f"DROP TRIGGER IF EXISTS {_INSERT_OWNER_TRIGGER}")
    op.execute(f"""
        CREATE TRIGGER {_INSERT_OWNER_TRIGGER}
        BEFORE INSERT ON {_RECEIPTS}
        WHEN {_INVALID_OWNER_0031}
        BEGIN
            SELECT RAISE(ABORT, 'invalid receipt evidence owner');
        END
    """)
    op.execute(f"""
        CREATE TRIGGER {_UPDATE_OWNER_TRIGGER}
        BEFORE UPDATE OF evidence_for_receipt_id, owner ON {_RECEIPTS}
        WHEN ({_INVALID_OWNER_0031}) OR (OLD.owner <> NEW.owner AND EXISTS (
            SELECT 1 FROM {_RECEIPTS}
            WHERE evidence_for_receipt_id = OLD.id AND owner <> NEW.owner
        ))
        BEGIN
            SELECT RAISE(ABORT, 'invalid receipt evidence owner');
        END
    """)
