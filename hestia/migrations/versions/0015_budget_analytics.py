"""Owner-private budget analytics annotations, rules and transfer decisions.

Revision ID: 0015
Revises: 0014
"""
from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('budget_account_identity',
        sa.Column('account_id', sa.String(), primary_key=True),
        sa.Column('owner', sa.String(), nullable=False),
        sa.Column('identities_json', sa.String(), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('observed_at', sa.Float(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['budget_accounts.id']),
        sa.ForeignKeyConstraint(['owner'], ['users.username']))
    op.create_table('budget_transaction_metadata',
        sa.Column('account_id', sa.String(), primary_key=True),
        sa.Column('entry_reference', sa.String(), primary_key=True),
        sa.Column('owner', sa.String(), nullable=False),
        sa.Column('ledger_fingerprint', sa.String(), nullable=False),
        sa.Column('payload_json', sa.String(), nullable=False),
        sa.Column('provenance_json', sa.Text(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('observed_at', sa.Float(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['account_id', 'entry_reference'], ['budget_transactions.account_id', 'budget_transactions.entry_reference']),
        sa.ForeignKeyConstraint(['owner'], ['users.username']))
    op.create_table('budget_categories',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('owner', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('name_key', sa.String(), nullable=False),
        sa.Column('archived', sa.Boolean(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(['owner'], ['users.username']),
        sa.UniqueConstraint('owner', 'name_key'))
    op.create_table('budget_annotations',
        sa.Column('account_id', sa.String(), primary_key=True),
        sa.Column('entry_reference', sa.String(), primary_key=True),
        sa.Column('owner', sa.String(), nullable=False),
        sa.Column('mode', sa.String(), nullable=False),
        sa.Column('category_id', sa.String(), nullable=True),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('ledger_fingerprint', sa.String(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(['account_id', 'entry_reference'], ['budget_transactions.account_id', 'budget_transactions.entry_reference']),
        sa.ForeignKeyConstraint(['category_id'], ['budget_categories.id']),
        sa.ForeignKeyConstraint(['owner'], ['users.username']))
    op.create_table('budget_rules',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('owner', sa.String(), nullable=False),
        sa.Column('field', sa.String(), nullable=False),
        sa.Column('operator', sa.String(), nullable=False),
        sa.Column('value', sa.String(), nullable=False),
        sa.Column('category_id', sa.String(), nullable=True),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('priority', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(['category_id'], ['budget_categories.id']),
        sa.ForeignKeyConstraint(['owner'], ['users.username']))
    op.create_table('budget_rule_previews',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('owner', sa.String(), nullable=False),
        sa.Column('candidate_json', sa.String(), nullable=False),
        sa.Column('snapshot_digest', sa.String(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('expires_at', sa.Float(), nullable=False),
        sa.Column('saved_rule_id', sa.String(), nullable=True),
        sa.Column('saved_version', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['owner'], ['users.username']),
        sa.ForeignKeyConstraint(['saved_rule_id'], ['budget_rules.id']))
    op.create_table('budget_transfer_decisions',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('owner', sa.String(), nullable=False),
        sa.Column('debit_account_id', sa.String(), nullable=False),
        sa.Column('debit_ref', sa.String(), nullable=False),
        sa.Column('credit_account_id', sa.String(), nullable=False),
        sa.Column('credit_ref', sa.String(), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('debit_fingerprint', sa.String(), nullable=False),
        sa.Column('credit_fingerprint', sa.String(), nullable=False),
        sa.Column('rule_version', sa.Integer(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(['credit_account_id', 'credit_ref'], ['budget_transactions.account_id', 'budget_transactions.entry_reference']),
        sa.ForeignKeyConstraint(['debit_account_id', 'debit_ref'], ['budget_transactions.account_id', 'budget_transactions.entry_reference']),
        sa.ForeignKeyConstraint(['owner'], ['users.username']),
        sa.UniqueConstraint('owner', 'debit_account_id', 'debit_ref', 'credit_account_id', 'credit_ref'))
    op.create_table('budget_transfer_legs',
        sa.Column('account_id', sa.String(), primary_key=True),
        sa.Column('entry_reference', sa.String(), primary_key=True),
        sa.Column('decision_id', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['account_id', 'entry_reference'], ['budget_transactions.account_id', 'budget_transactions.entry_reference']),
        sa.ForeignKeyConstraint(['decision_id'], ['budget_transfer_decisions.id']))
    op.create_table('budget_analytics_audit',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('owner', sa.String(), nullable=False),
        sa.Column('subject_type', sa.String(), nullable=False),
        sa.Column('subject_id', sa.String(), nullable=False),
        sa.Column('action', sa.String(), nullable=False),
        sa.Column('before_json', sa.String(), nullable=False),
        sa.Column('after_json', sa.String(), nullable=False),
        sa.Column('changed_at', sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(['owner'], ['users.username']))


def downgrade():
    op.drop_table('budget_analytics_audit')
    op.drop_table('budget_transfer_legs')
    op.drop_table('budget_transfer_decisions')
    op.drop_table('budget_rule_previews')
    op.drop_table('budget_rules')
    op.drop_table('budget_annotations')
    op.drop_table('budget_categories')
    op.drop_table('budget_transaction_metadata')
    op.drop_table('budget_account_identity')
