"""Alembic environment — runs migrations against a connection passed in by ``hestia.db.init_db``.

hestia drives Alembic programmatically (``command.upgrade``/``downgrade`` with a live
connection in ``config.attributes['connection']``); the CLI/offline path below is kept
only so ``alembic revision --autogenerate`` works during development and is excluded
from the coverage gate.
"""
from alembic import context

from hestia.db import metadata

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
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
else:
    context.configure(connection=connectable, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()
