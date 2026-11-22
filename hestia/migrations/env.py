"""Alembic environment — runs migrations against a connection passed in by ``hestia.db.init_db``.

hestia drives Alembic programmatically (``command.upgrade``/``downgrade`` with a live
connection in ``config.attributes['connection']``); the CLI/offline path below is kept
only so ``alembic revision --autogenerate`` works during development and is excluded
from the coverage gate.
"""
from alembic import context

from hestia.db import metadata, open_sqlite_ddl_transaction, restore_sqlite_isolation

target_metadata = metadata
connectable = context.config.attributes.get("connection")

if connectable is None:  # pragma: no cover - dev-only CLI path; init_db always passes a connection
    from sqlalchemy import engine_from_config, pool

    connectable = engine_from_config(
        context.config.get_section(context.config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        previous = open_sqlite_ddl_transaction(connection)
        try:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
            if connection.connection.dbapi_connection.in_transaction:
                connection.exec_driver_sql("COMMIT")
        except Exception:
            raw = connection.connection.dbapi_connection
            if raw.in_transaction:
                connection.exec_driver_sql("ROLLBACK")
            raise
        finally:
            restore_sqlite_isolation(connection, previous)
else:
    # Hold one real transaction across every statement in the revision, including
    # the alembic_version update Alembic performs before run_migrations returns.
    # The caller (init_db's engine.begin, or a test's) commits or rolls it back.
    previous = open_sqlite_ddl_transaction(connectable)
    try:
        context.configure(connection=connectable, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    finally:
        restore_sqlite_isolation(connectable, previous)
