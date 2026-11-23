# Deploy runbook

Standing procedure for shipping a migration, plus the deploy 31 incident that set it.

## Incident note, 2026-11-23, deploy 31

Migration `0020` failed with `FOREIGN KEY constraint failed`. Rollback to deploy 30 took about 1.5 min. The backup was clean.

Root cause: `batch_alter_table` on SQLite combined with `PRAGMA foreign_keys=ON` inside the migration transaction, plus a shadow verify that used a bare engine without the production pragmas.

Fix: this PR. `hestia.db.init_db` and `hestia.db.downgrade_db` share one dedicated connection: foreign keys off (the pragma must read back `0`), a real `BEGIN`, Alembic, then `PRAGMA foreign_key_check` before commit. A violation rolls the migration back. `0020` also drops a leftover `_alembic_tmp_*` table before its batch rebuild. Other connections still open with the production pragmas, including `PRAGMA foreign_keys=ON`.

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

7. Run `downgrade_db` to the previous revision, then `PRAGMA integrity_check` again. It must return `ok`.

8. Run `init_db` again. `downgrade_db` drops the cached engine for that path, so this call upgrades back to head in the same process. Repeat the checks from steps 4–6.

9. Delete the copy.

```python
from hestia.db import downgrade_db, init_db

init_db("/tmp/hestia-shadow.db")
# integrity_check, foreign_key_check, no _alembic_tmp_%, row counts against the copy
downgrade_db("/tmp/hestia-shadow.db", "0019")  # the previous revision
# integrity_check again
init_db("/tmp/hestia-shadow.db")
```

## Batch migrations on referenced tables

SQLite `batch_alter_table` rebuilds a table by copying it and dropping the original. Child foreign keys reject that `DROP` while `PRAGMA foreign_keys=ON`, and SQLite ignores a change to that pragma inside a transaction. A batch migration on a table that other tables reference has to go through this path (`init_db` / `downgrade_db`), not a bare engine.

## Known same-class risks

Follow-ups only. This PR does not change those migrations. The dedicated-connection runner now covers them, but they still need tests when they are written.

- `0002` and `0003` downgrades batch-alter `users` while `user_settings` references it.
- The planned v1d `0023` / `0024` migrations will batch-alter `budget_receipts`, which lines reference.

## Persistent budget-rule HMAC key

Before deploying counterparty-account rules, **holzera must create the dedicated rule key on the host**, as the same user that runs Hestia. Do not reuse `HESTIA_SESSION_SECRET`: session rotation must not change persisted rule matching.

```sh
install -d -m 0700 /var/lib/hestia/secrets
umask 077
head -c32 /dev/urandom > /var/lib/hestia/secrets/budget-rule.key
chmod 0400 /var/lib/hestia/secrets/budget-rule.key
```

Set `HESTIA_BUDGET_RULE_KEY_FILE=/var/lib/hestia/secrets/budget-rule.key` in Hestia's deployment environment and mount the file read-only at that path. Back up the key securely with the database. The file must remain owned by the Hestia user with mode `0400` or `0600`; missing, short, symlinked, or otherwise unsafe files make account-rule creation and matching fail with `budget_analytics_rule_key_unavailable` while other rule types remain available.

Keep this key stable. Changing it changes the stored key-ID context, rejects previews created under the previous key, and prevents older counterparty-account rules from matching until they are deliberately recreated.
