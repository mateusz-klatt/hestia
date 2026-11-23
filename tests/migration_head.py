"""The single Alembic head, so migration tests do not pin a revision that the next migration moves."""
from alembic.config import Config
from alembic.script import ScriptDirectory

from hestia import db


def head_revision() -> str:
    cfg = Config()
    cfg.set_main_option("script_location", str(db._MIGRATIONS_DIR))
    (head,) = ScriptDirectory.from_config(cfg).get_heads()
    return head


HEAD = head_revision()
