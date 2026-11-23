# Deploy runbook

Standing procedure for shipping a migration, plus the deploy 31 incident that set it.

## Incident note, 2026-11-23, deploy 31

Migration `0020` failed with `FOREIGN KEY constraint failed`. Rollback to deploy 30 took about 1.5 min. The backup was clean.

Root cause: `batch_alter_table` on SQLite combined with `PRAGMA foreign_keys=ON` inside the migration transaction, plus a shadow verify that used a bare engine without the production pragmas.

The runner fix: `hestia.db.init_db` and `hestia.db.downgrade_db` share one dedicated connection: foreign keys off (the pragma must read back `0`), a real `BEGIN IMMEDIATE`, Alembic, then `PRAGMA foreign_key_check` before commit. A violation rolls the migration back. `0020` also drops a leftover `_alembic_tmp_*` table before its batch rebuild. Other connections still open with the production pragmas, including `PRAGMA foreign_keys=ON`.

## Standing shadow-verify procedure

Run this for every migration, against a copy, before the deploy. Leave the production backup itself untouched.

1. Copy the production backup on the host. Mode `0600`. No network: do not send the file off the machine, and run the verify process with network disabled.

   ```sh
   umask 077
   cp /path/to/hestia-backup.db /tmp/hestia-shadow.db
   chmod 0600 /tmp/hestia-shadow.db
   ```

2. Record `COUNT(*)` for every application table on that copy (skip `sqlite_%`).

3. Run `hestia.db.init_db` on the copy. That applies the real pragmas (`journal_mode=WAL`, `synchronous=FULL`, `foreign_keys=ON`, `busy_timeout=5000`). Do not open a bare engine.

4. Check `PRAGMA integrity_check` returns `ok`, and `PRAGMA foreign_key_check` is empty.

5. Check `sqlite_master` has no table named `_alembic_tmp_%`:

   ```sql
   SELECT name FROM sqlite_master
    WHERE type = 'table' AND name LIKE '!_alembic!_tmp!_%' ESCAPE '!';
   ```

6. Compare row counts against the copy (the counts from step 2). Tables the migration does not add or drop must match.

7. Run `downgrade_db` to the previous revision using the explicit copy path. Repeat the integrity, foreign-key, and leftover-table checks from steps 4–5, and check surviving rows against the counts from step 2. Tables or columns intentionally removed by the downgrade lose their data.

8. Run `init_db` again. `downgrade_db` drops the cached engine for that path, so this call upgrades back to head in the same process. Repeat the checks from steps 4–6.

9. Delete the copy.

```python
from hestia.db import downgrade_db, init_db

init_db("/tmp/hestia-shadow.db")
# integrity_check, foreign_key_check, no _alembic_tmp_%, row counts against the copy
downgrade_db("/tmp/hestia-shadow.db", "0021")  # previous revision when head is 0022
# integrity_check, foreign_key_check, no _alembic_tmp_%, surviving rows
init_db("/tmp/hestia-shadow.db")
```

`init_db(path=None)` uses `HESTIA_DB` (or its configured default). Downgrades require an explicit path: `downgrade_db(None, "0019")`, an empty path, or a whitespace-only path raises `ValueError` before resolving a target or touching the engine cache. `downgrade_db(path, None)` and empty or invalid revisions also raise `ValueError`; omitting the required revision argument raises `TypeError`. Always pass both the copy path and target revision.

## Batch migrations on referenced tables

SQLite `batch_alter_table` rebuilds a table by copying it and dropping the original. Child foreign keys reject that `DROP` while `PRAGMA foreign_keys=ON`, and SQLite ignores a change to that pragma inside a transaction. A batch migration on a table that other tables reference has to go through this path (`init_db` / `downgrade_db`), not a bare engine.

## Verified batch migrations and future risks

`tests/test_migration_downgrade.py` runs a populated database from head through every revision down to `0001`, then upgrades again. Each step checks integrity, foreign keys, leftover batch tables, and the expected surviving rows. The `0003` and `0002` downgrades rebuild `users` while `user_settings` still references it; the runner's FK-off transaction preserves both. Budget rows survive until the downgrade that intentionally drops their tables. Upgrading restores schema, not deleted budget data, roles, disabled flags, labels, or category parent links; `0002` backfills surviving users to `admin` and `0003` defaults them to enabled.

Within `0019`–`0022`, only `0020` (categories) and `0021` (annotations) use `batch_alter_table`. Both already drop leftover batch tables before upgrade and downgrade; the same regression test covers all four guards with populated referencing rows. `0019` and `0022` create/drop tables and indexes without batch rebuilds.

Future migrations that batch-alter `budget_receipts`, which lines reference, need the same guards and populated round-trip checks.
