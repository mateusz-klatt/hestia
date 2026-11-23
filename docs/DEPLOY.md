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

The container runs as UID/GID `1000:1000` (`Dockerfile` `USER 1000:1000`) and `docker-compose.yml` already mounts the host `./data` directory at `/data` read-write, so the key lives inside that existing mount and needs no new volume. On the host, from the compose directory, as the deploy user (no sudo needed when that user is UID 1000; otherwise `chown 1000:1000` afterwards):

```sh
install -d -m 0700 ./data/secrets
umask 077
python3 -c 'import os,sys; sys.stdout.buffer.write(os.urandom(32))' > ./data/secrets/budget-rule.key
chmod 0400 ./data/secrets/budget-rule.key
stat -c '%u:%g %a %s' ./data/secrets/budget-rule.key   # expect 1000:1000 400 32
```

Then add `HESTIA_BUDGET_RULE_KEY_FILE=/data/secrets/budget-rule.key` (the **container** path) to the deployment `.env` and recreate the container. Never print, copy into chat/Iris/logs, or commit the key; back it up only together with the encrypted database backups.

**Missing or unsafe key:** startup is not affected. The key is loaded lazily; a missing, short (< 32 bytes), symlinked, group/world-readable or wrongly owned file makes only counterparty-account rules unavailable. Creating or previewing one returns `400 budget_analytics_rule_key_unavailable`. An existing one does not match, and the rules list marks it with `inactive_reason` `rule_key_unavailable` (Rules UI: „Nieaktywna: brak klucza”). Every other rule type keeps working, so overview, transactions, month budget, forecast and the other analytics reads stay successful. The key can be created after the deploy, but account rules only start working once it exists.

**Rotation:** keep the key stable. Stored values carry a key id (`cpacct:v2:<kid>:…`), so after a rotation old account rules stop matching (fail-closed, never mismatch) until they are recreated, and previews created under the old key are rejected. The rules list marks those rules with `inactive_reason` `rule_key_rotated` (Rules UI: „Nieaktywna: zmieniony klucz – utwórz regułę ponownie”). Procedure: (1) list the owner's counterparty-account rules in the Rules UI and note their category/priority; (2) replace the file (same steps as above) and recreate the container; (3) recreate each rule via "Utwórz regułę z konta odbiorcy" on a matching transaction; (4) disable the old (now inactive) rules. Rotate only on suspected key exposure.
