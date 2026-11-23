"""Persist a stable hue for each active top-level budget category.

Revision ID: 0027
Revises: 0025

Numbered 0027 because another open migration already reserves 0026. Re-parent
this revision when the earlier migrations merge. Existing active roots are
assigned once, independently per owner, in deterministic name_key order.
"""
from alembic import op
import sqlalchemy as sa

revision = "0027"
down_revision = "0025"
branch_labels = None
depends_on = None

_TABLE = "budget_categories"
_RANGE_CHECK = "ck_budget_categories_color_hue_range"
_ROOT_CHECK = "ck_budget_categories_color_hue_root"
_OFFSET = 20


def _drop_leftover_batch_table() -> None:
    temp_name = ("_alembic_tmp_%s" % _TABLE)[:50]
    op.execute(f'DROP TABLE IF EXISTS "{temp_name}"')


def upgrade():
    _drop_leftover_batch_table()
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(sa.Column("color_hue", sa.Integer(), nullable=True))
        batch.create_check_constraint(
            _RANGE_CHECK, "color_hue IS NULL OR (color_hue >= 0 AND color_hue <= 359)")
        batch.create_check_constraint(_ROOT_CHECK, "parent_id IS NULL OR color_hue IS NULL")

    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT id, owner FROM budget_categories "
        "WHERE parent_id IS NULL AND archived = 0 ORDER BY owner, name_key, id"
    )).all()
    grouped = {}
    for identifier, owner in rows:
        grouped.setdefault(owner, []).append(identifier)
    for identifiers in grouped.values():
        count = len(identifiers)
        for index, identifier in enumerate(identifiers):
            hue = round(index * 360 / count + _OFFSET) % 360
            bind.execute(sa.text("UPDATE budget_categories SET color_hue = :hue WHERE id = :identifier"),
                         {"hue": hue, "identifier": identifier})


def downgrade():
    _drop_leftover_batch_table()
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_constraint(_ROOT_CHECK, type_="check")
        batch.drop_constraint(_RANGE_CHECK, type_="check")
        batch.drop_column("color_hue")
