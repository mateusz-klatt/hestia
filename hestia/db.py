"""SQLite persistence foundation — SQLAlchemy 2.0 models + Alembic-managed schema.

INERT in Phase 1: nothing in the live path imports this module yet. It defines the
on-disk schema (one SQLite DB, default ``/data/hestia.db`` via ``HESTIA_DB``) that
later phases migrate the registry / automations / users / settings / audit onto. The
in-memory model stays the source of truth; this is the durability layer (see the #57
migration plan). Importing JSON state, cutting persistence over, and the cancel-safe
``_db_write_and_settle`` write path all land in later, separately-reviewed phases.

We use SYNC SQLAlchemy (driven from the event loop via ``run_in_executor`` in later
phases), NOT async/aiosqlite — it matches hestia's existing offload pattern. Durability
PRAGMAs (WAL, ``synchronous=FULL``, foreign keys, ``busy_timeout``) are set per
connection. Schema changes go through Alembic migrations (``hestia/migrations``), run
programmatically at startup (``init_db``) — never runtime autogenerate.

This module deliberately does NOT use ``from __future__ import annotations``: the
SQLAlchemy declarative mapper resolves ``Mapped[...]`` annotations at class-creation
time, and real (non-stringised) PEP 604 unions are the most robust on Python 3.14.
"""
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, ForeignKey, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

DEFAULT_DB_PATH = "/data/hestia.db"


def db_path() -> Path:
    """The SQLite file path (``HESTIA_DB`` env, else ``/data/hestia.db``)."""
    return Path(os.environ.get("HESTIA_DB", DEFAULT_DB_PATH))


class Base(DeclarativeBase):
    pass


class AppMeta(Base):
    """Key/value bookkeeping: runtime ``mode``, per-store authority markers, schema notes."""

    __tablename__ = "app_meta"
    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[str]


class Node(Base):
    """One device-registry node, stored as the exact entry dict (lossless JSON) so unknown
    / future fields survive verbatim — a normalised table would silently drop them."""

    __tablename__ = "nodes"
    key: Mapped[str] = mapped_column(primary_key=True)
    entry_json: Mapped[str]


class Automation(Base):
    """One automation rule (canonical ``Rule.to_dict`` JSON). ``position`` preserves eval order."""

    __tablename__ = "automations"
    id: Mapped[str] = mapped_column(primary_key=True)
    position: Mapped[int]
    rule_json: Mapped[str]


class User(Base):
    """An app-login account: username → scrypt password hash (see ``hestia.auth``)."""

    __tablename__ = "users"
    username: Mapped[str] = mapped_column(primary_key=True)
    password_hash: Mapped[str]


class UserSetting(Base):
    """Per-user UI preferences (#55): locale / temperature scale / theme."""

    __tablename__ = "user_settings"
    username: Mapped[str] = mapped_column(ForeignKey("users.username"), primary_key=True)
    locale: Mapped[str | None]
    temp_scale: Mapped[str | None]
    theme: Mapped[str | None]


class Audit(Base):
    """Append-only event log (#56): who/what did which action. Pruned by row/age cap later."""

    __tablename__ = "audit"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts: Mapped[float]
    actor: Mapped[str | None]
    action: Mapped[str | None]
    target: Mapped[str | None]
    detail: Mapped[str | None]
    result: Mapped[str | None]


metadata = Base.metadata

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _apply_pragmas(dbapi_conn) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=FULL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


def make_engine(path) -> Engine:
    """A SQLite engine for ``path`` with hestia's durability PRAGMAs applied per connection.
    The parent dir is created so a first run on an empty volume succeeds."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: writes run in run_in_executor worker threads (the cancel-safe write
    # path), so the connection must be usable off the creating thread. Explicit, not relying on the
    # dialect default. Single-writer serialisation is handled by the caller's save_lock + WAL.
    engine = create_engine(f"sqlite:///{p}", connect_args={"check_same_thread": False})
    event.listen(engine, "connect", lambda dbapi_conn, _record: _apply_pragmas(dbapi_conn))
    return engine


def _alembic_config(connection) -> Config:
    """An Alembic config wired to ship our migration scripts + the live connection (no .ini file)."""
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.attributes["connection"] = connection
    return cfg


# One engine + session factory per resolved DB path, created (and Alembic-upgraded) ONCE for the
# process lifetime. The hot store_sql paths (the ~30 s autosave, every audit row, each /api/db/stats)
# call init_db on every invocation; without this they each built a fresh engine AND re-ran the Alembic
# upgrade (logging "Context impl SQLiteImpl…" every time + churning a new connection pool). Keyed by the
# resolved path string so tests on distinct temp DBs stay isolated; reset_engine_cache() disposes them.
_ENGINE_CACHE: "dict[str, tuple[Engine, sessionmaker[Session]]]" = {}


def init_db(path=None) -> tuple[Engine, sessionmaker[Session]]:
    """Engine + session factory for the DB at ``path`` (default ``HESTIA_DB``), upgraded to the latest
    schema via Alembic. **Cached per resolved path**: the engine is built and the migration run ONCE per
    DB file for the process lifetime; later calls return the same pair (call ``reset_engine_cache`` to
    rebuild). Migrations run programmatically — never runtime autogenerate."""
    key = str(db_path() if path is None else path)
    cached = _ENGINE_CACHE.get(key)
    if cached is not None:
        return cached
    engine = make_engine(key)
    # The outer engine.begin() owns this connection and its commit. Alembic's env.py calls
    # context.begin_transaction() on the same connection, but for SQLite (non-transactional
    # DDL) that's a no-op context — there is no nested/second BEGIN, and the migration's DDL
    # commits with the outer transaction.
    with engine.begin() as connection:
        command.upgrade(_alembic_config(connection), "head")
    result = (engine, sessionmaker(bind=engine))
    _ENGINE_CACHE[key] = result
    return result


def reset_engine_cache() -> None:
    """Dispose every cached engine and clear the cache. For test isolation (a fresh temp DB per test)
    and the rare case of re-initialising after the on-disk file is replaced underneath us."""
    for engine, _ in _ENGINE_CACHE.values():
        engine.dispose()
    _ENGINE_CACHE.clear()


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """A transactional session: commit on success, roll back on error, always close."""
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
