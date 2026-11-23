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

## Persistent Budget rule and receipt-device HMAC key

Before deploying counterparty-account rules or pairing receipt devices, **holzera must configure the dedicated key on the host**, as the same user that runs Hestia. The existing `HESTIA_BUDGET_RULE_KEY_FILE` also roots the separate receipt device-token and pairing-code keys. Rotating `HESTIA_SESSION_SECRET` leaves stored rule matching, device tokens, and pending pairing codes valid.

The container runs as UID/GID `1000:1000` (`Dockerfile` `USER 1000:1000`) and `docker-compose.yml` already mounts the host `./data` directory at `/data` read-write, so the key lives inside that existing mount and needs no new volume. On the host, from the compose directory, as the deploy user (no sudo needed when that user is UID 1000; otherwise `chown 1000:1000` afterwards):

```sh
install -d -m 0700 ./data/secrets
umask 077
python3 -c 'import os,sys; sys.stdout.buffer.write(os.urandom(32))' > ./data/secrets/budget-rule.key
chmod 0400 ./data/secrets/budget-rule.key
stat -c '%u:%g %a %s' ./data/secrets/budget-rule.key   # expect 1000:1000 400 32
```

Then add `HESTIA_BUDGET_RULE_KEY_FILE=/data/secrets/budget-rule.key` (the **container** path) to the deployment `.env` and recreate the container. Never print, copy into chat/Iris/logs, or commit the key; back it up only together with the encrypted database backups.

**Missing or unsafe key**. Hestia loads the file on demand, so startup can proceed. A missing, short (< 32 bytes), symlinked, group/world-readable or wrongly owned file makes counterparty-account rules unavailable. Creating or previewing one returns `400 budget_analytics_rule_key_unavailable`. The rules list marks affected rules with `inactive_reason` `rule_key_unavailable` (Rules UI: „Nieaktywna: brak klucza”). Other rule types and analytics reads such as overview, transactions, month budget, and forecast work during a key outage. Counterparty-account rules start working once the key is available.

Receipt pairing and redemption require a safe file with exactly 32 bytes and return `503 budget_unavailable` while it is unavailable. Device bearer requests fail closed with `401 device_token_invalid`. Browser owners can list and revoke devices during a key outage.

**Rotation:** keep the key stable. Stored values carry a key id (`cpacct:v2:<kid>:…`), so after a rotation old account rules stop matching (fail-closed, never mismatch) until they are recreated, and previews created under the old key are rejected. The rules list marks those rules with `inactive_reason` `rule_key_rotated` (Rules UI: „Nieaktywna: zmieniony klucz – utwórz regułę ponownie”). Procedure: (1) list the owner's counterparty-account rules in the Rules UI and note their category/priority; (2) replace the file (same steps as above) and recreate the container; (3) recreate each rule via "Utwórz regułę z konta odbiorcy" on a matching transaction; (4) disable the old (now inactive) rules. Rotate only on suspected key exposure.

Rotating the persistent key also invalidates every previously issued receipt device token and pending pairing code. Pair each device again after replacing the file.
