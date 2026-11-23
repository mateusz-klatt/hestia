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

## Receipt device pairing behind Apache and Cloudflare

Set `HESTIA_BUDGET_TRUSTED_PROXY_CIDRS=127.0.0.1/32,::1/128` so Hestia trusts the loopback Apache proxy's forwarded client address. Pairing backoff and its per-client limit use an IPv4 address or an IPv6 /64 prefix. Only well-formed redemption attempts consume the shared limit of 300 per minute.

Enable Apache's `mod_remoteip` and configure `CF-Connecting-IP` as the client-address header. Restrict trusted senders to Cloudflare's published IPv4 and IPv6 ranges, maintained in `/etc/apache2/cloudflare-proxies.lst`, one CIDR per line. Populate and maintain that file from the official range lists. [Cloudflare instructions](https://developers.cloudflare.com/support/troubleshooting/restoring-visitor-ips/restoring-original-visitor-ips/), [Cloudflare IP ranges](https://www.cloudflare.com/ips/)

In the Hestia virtual host, use the following directives with `mod_headers` and `mod_proxy` enabled.

```apache
RemoteIPHeader CF-Connecting-IP
RemoteIPTrustedProxyList /etc/apache2/cloudflare-proxies.lst
RequestHeader unset X-Forwarded-For
ProxyAddHeaders On
```

`mod_remoteip` restores the client address before proxying. The `RequestHeader` directive strips incoming `X-Forwarded-For`, and `ProxyAddHeaders` builds the forwarded header from Apache's restored address. Use the normal request-header phase for this removal so it follows address restoration. Without the trusted loopback setting and client-address restoration, pairing traffic collapses into one per-IP bucket. [Apache remote-IP documentation](https://httpd.apache.org/docs/2.4/mod/mod_remoteip.html), [Apache proxy headers](https://httpd.apache.org/docs/2.4/mod/mod_proxy.html#proxyaddheaders)

## Gmail invoice import (receipts v1d-a)

Default off (`HESTIA_BUDGET_GMAIL_IMPORT_ENABLED` unset). Invoice HTML/plain text is parsed only by the isolated `receipt-parser` service (`network_mode: none`, read-only root, `cap_drop: ALL`, `no-new-privileges`, UID 1000, 256 MiB / 32 pids / 0.5 CPU, noexec tmpfs). It is behind the compose profile `receipt-parser`, so an ordinary Hestia deploy does not start it. Hestia never falls back to in-process parsing: with the flag on and the service down, the Gmail routes return `503 budget_receipt_gmail_parser_unavailable` and no Gmail call is made.

The service reads all three files through `config._read_secret`. Each must be a regular file owned by container UID 1000 with mode `0400` or `0600`. Symlinks are rejected.

- **Dedupe key** `HESTIA_BUDGET_RECEIPTS_HMAC_SECRET_FILE`: 32 random bytes, created like the rule key above, e.g. `./data/secrets/receipts-gmail-hmac.key` → `/data/secrets/receipts-gmail-hmac.key`. Keep it stable; rotating it makes previously imported messages look new.
- **Gmail refresh token**: reuse the existing `gmail.readonly` token (no new grant). Write it into a `0400` file under `./data/secrets/` and point the config's `refresh_token_env` at the env var holding that container path.
- **Config** `HESTIA_BUDGET_RECEIPTS_GMAIL_CONFIG`: path to the JSON config, up to 16 KiB.

`sender_allowlist` and `spouse_forward_senders` must both appear, and at least one must contain an entry. Environment variable names must match `[A-Z_][A-Z0-9_]{0,127}` and be set inside the container. The examples below are placeholders.

| JSON key | Type | Meaning | Default or allowed values |
| --- | --- | --- | --- |
| `owner` | String | Hestia account that owns the Gmail import, such as `example-owner`. | Required. Nonblank UTF-8, up to 128 characters. |
| `owner_mailbox` | String | Gmail mailbox the OAuth profile must match, such as `owner@example.test`. | Required. Bare ASCII mailbox address, compared case-insensitively. |
| `client_id_env` | String | Name of the environment variable holding the OAuth client ID, such as `EXAMPLE_GMAIL_CLIENT_ID`. | Required. Environment variable name. |
| `client_secret_env` | String | Name of the environment variable holding the OAuth client secret, such as `EXAMPLE_GMAIL_CLIENT_SECRET`. | Required. Environment variable name. |
| `refresh_token_env` | String | Name of the environment variable holding the absolute container path to the refresh-token file, such as `EXAMPLE_GMAIL_REFRESH_TOKEN_FILE`. | Required. Environment variable name. |
| `sender_allowlist` | Array of strings | Supplier rules for direct imports. Forwarded invoices require exact mailbox entries. | Required. Bare ASCII addresses or domain rules such as `billing@supplier.example.test` and `@supplier.example.test`. Domain rules include subdomains and exclude public webmail domains. |
| `spouse_forward_senders` | Array of strings | Mailboxes allowed to send spouse forwards. | Required. Exact bare ASCII addresses such as `forwarder@example.test`. |
| `known_merchant_domains` | Array of strings | Legacy domain setting. Automatic forward imports depend on exact mailbox entries in `sender_allowlist`. | Defaults to `[]`. ASCII domains with an optional leading `@`, such as `supplier.example.test`. Public webmail domains are excluded. |
| `require_known_forwarded_merchant` | Boolean | Legacy setting. The importer enforces exact mailbox checks for either value. | Defaults to `true`. |
| `label_name` | String or `null` | Exact Gmail label used to scope imports, such as `Example Invoices`. | Defaults to `null` for no label filter. A string must be nonblank UTF-8, up to 225 characters. |
| `backfill_days` | Integer | Number of days to look back during backfill. | Defaults to `365`. Range 1–365. |
| `backfill_max_messages` | Integer | Number of messages the backfill processes before pausing to resume later. | Defaults to `500`. Range 1–500. |
| `sync_interval_seconds` | Integer | Interval in seconds between scheduled syncs. | Defaults to `3600`. Range 3,600–9,007,199,254,740,992 (`2^53`). |

Enable, in this order:

1. Create the secret files above and add their container paths to `.env`.
2. `docker compose --profile receipt-parser up -d --build receipt-parser`
3. Wait until `docker compose ps receipt-parser` reports `healthy`.
4. Set `HESTIA_BUDGET_GMAIL_IMPORT_ENABLED=1` in `.env` and recreate the `hestia` container.
5. Smoke: the Gmail status route reports `configured=true`; no import has run yet unless triggered.

To roll back, remove the flag from `.env` and recreate `hestia` (Gmail routes return 404 and the poller stops). Then run `docker compose --profile receipt-parser stop receipt-parser`. Disabling Gmail leaves migration `0027` applied. To roll back the Gmail follow-up schema, stop the app and run `downgrade_db(path, "0026")` (this keeps the `0026` category hues). Migration `0027` discards queued Gmail syncs on downgrade and requires any manual-forward receipts and `owner_forward` provenance rows to be removed first. Downgrading below `0026` also drops the category hues, and below `0025` deletes device pairing codes, tokens, and their audit entries.
