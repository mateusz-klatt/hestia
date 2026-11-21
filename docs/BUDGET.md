# Budget: read-only Enable Banking foundation

Status: deployed at migration `0013` are the CLI foundation, the private HTTP API
(2B.1), durable refresh jobs with an opt-in poller (2B.jobs) and the owner budget
screens (2C). Each server feature has its own default-off flag. Browser bank consent
and renewal (2B.2/2B.3) are not implemented. This module does not make payments or
categorise expenses. The public privacy policy and terms describe the budget module;
keep them current before changing what is processed.

## Decisions

- Enable Banking AIS runs on the server. A phone app need not stay running.
- Only explicitly configured account `identification_hash` values are fetched.
  Authorising or linking accounts in Enable Banking does not select them in Hestia.
- Each connection belongs to one existing, enabled Hestia **admin**. Financial
  data is separate from shared calendars/device permissions. The private HTTP API
  requires that administrator to be the exact owner. CLI status prints counts and freshness, not amounts,
  account hashes, account numbers, descriptions, session IDs or credentials.
- Reauthorisation replaces the external session file, preserving the connection
  ID and selected account hashes. Session account UIDs are temporary routing data,
  not durable identities, and are not stored in the database.
- `entry_reference` identifies a transaction within one stable account.
  `transaction_id` is a changeable details handle and is **never** a dedup key.
  Only BOOK entries enter the canonical ledger. Other statuses, missing references
  and undated transactions are retained separately as
  unresolved observations, excluded from the canonical transaction ledger. Two
  identical unreferenced purchases remain two observations. No fuzzy matching.
- Money is stored as validated decimal strings with explicit currencies and
  credit/debit indicators. No floats, currency conversion, net-worth calculation,
  or interpretation of credit limits as available personal cash.
- Balance types stay distinct. They are observations, not proof of card statement
  debt, minimum payment or due date. Bank account type labels alone do not prove
  that an account is a credit card.

## Private stored-data API (2B.1)

Set `HESTIA_BUDGET_HTTP_ENABLED=1` only when ready to expose this feature. It also
requires `HESTIA_PERSIST=sqlite`, an authoritative SQLite users store, enabled
Hestia authentication and an existing enabled admin principal. Auth-off/dev and
legacy JSON-user fallback never grant financial access. Another admin's IDs get
the same 404 as unknown IDs. The flag defaults off; no key/session file, account
discovery, import or network acquisition happens when reading stored data.

Available admin routes are GET `/api/budget/connections`, `/accounts`, `/status`,
`/transactions`, `/balances` and `/limits` under the same `/api/budget` prefix.
Transaction and balance reads require a local `account_id`. Transaction pages use
date filters, a bounded `limit` (default 50/max 200) and an encrypted cursor bound
to the owner/account/filter. Its expiry or tampering requires a fresh first page.
Canonical entries and uncertainty counts remain separate. Large balance lists
are explicitly marked truncated; archived-import metadata is separately labeled.
Responses expose no signing keys, bank session/account UIDs or primary hashes.
`account_total` counts the account's canonical entries across all dates;
`uncertain_total` also applies to the whole account, independently of page filters.
Account responses also provide an optional private `display` object and separate
`observed_balance_currencies`. Display currency is provider-declared; currencies
observed in current API balances do not establish the account's declared currency.
Missing metadata is returned as `display: null`. See the offline metadata contract
below; reads never fetch missing labels from the bank.

Browser-only POST controls are `/api/budget/account-selection`, `/account-kind`
and `/connection-state`. They require the HttpOnly session cookie, JSON, and exact
`Origin` matching an explicitly configured HTTPS `HESTIA_BUDGET_PUBLIC_ORIGIN`
(for example `https://hestia.klatt.ie`). Any Authorization header is rejected for
these mutations; authenticated bearer reads remain supported. Unknown fields,
duplicate JSON fields, duplicate scalar query parameters and invalid types are
rejected. Supply the current `control_version` as `expected_version`; a missing
or stale conflicting version returns 409 with safe current owner metadata.

Selection/kind/pause writes audit the authenticated username with origin `web` in
the same transaction, without publishing financial details in the global audit
feed or SSE. Identical intent is a no-op. Migration 0011 starts existing control
versions at 1, preserves selections/history/quota and adds a persistent connection
pause. Config reload cannot undo pause. Pause does not deselect accounts or clear
consumed quota; an in-flight round cannot publish after it is paused.

All budget responses, including errors, use `Cache-Control: no-store` and
`Referrer-Policy: no-referrer`. Verify these headers at the public reverse proxy
as well: its existing vhost header override must not replace the budget policy.
Use synthetic, separate-database records for HTTP/UAT checks; enabling the flag
does not itself authorize live import or access to bank credentials.

Connection capabilities report which of refresh jobs, background selection, background
polling and browser consent are available. Browser consent stays unavailable: no
callback/consent/public completion route exists yet. See [BUDGET_2B.md](BUDGET_2B.md)
for the later consent/renewal sequence.

## Refresh jobs and opt-in background polling (2B.jobs)

Two independent default-off flags control unattended work. Neither is implied by
`HESTIA_BUDGET_HTTP_ENABLED`, and neither imports history, copies PoC files or
creates a bank session.

| Flag | Effect when `1` |
| --- | --- |
| `HESTIA_BUDGET_JOBS_ENABLED` | One dedicated worker thread in the Hestia process executes durable refresh jobs. `POST /api/budget/refresh` then accepts work; otherwise it returns `503 budget_refresh_unavailable` and writes nothing. |
| `HESTIA_BUDGET_POLLER_ENABLED` | Requires the jobs flag. About once a minute (plus up to 10 s jitter) the worker checks stored due state and enqueues at most one intent per eligible connection. |

The executor starts only with authentication enabled, `HESTIA_PERSIST=sqlite`,
authoritative SQLite users and a readable `HESTIA_BUDGET_CONFIG`; otherwise it
logs the missing prerequisite by class name and stays disabled. It reads the full
configuration again for every poll and job, then reconciles it with `configure()`
under the per-database budget lock, so a job never narrows configuration or retires
unrelated connections. Jobs share every 2A guarantee: persisted debits before HTTP,
2 starts per rolling 24 h and 12 h spacing per selected endpoint (or stricter policy),
Retry-After and suspension, transport pacing, component leases and pause fences.
A job is a local intent, never an allowance.

`POST /api/budget/refresh` takes `{connection_id, expected_version, account_id?,
operation?}` and a required `Idempotency-Key` header (16–128 of `A-Z a-z 0-9 _ -`;
a random UUID works). It returns `202` with the job. The same owner key and target
returns the same job, even after later control changes; the same key with another
target is `409 budget_idempotency_conflict`. At most one queued or running job exists
per connection: a request for the same canonical target coalesces with it (an
omitted account/operation means the whole connection), and any other target is
`409 budget_refresh_in_progress`. A paused connection is `409 budget_connection_paused`.
`expected_version` is always required (missing is `409`), but a stale value still
replays the job its key already names. A new request never joins a queued job from
an older control generation: that job finishes `deferred/control_changed` and a new
one is queued; a running one from an older generation answers `409`.
`GET /api/budget/jobs` lists the owner's 20 newest jobs (optional `connection_id`).
Each key is kept for 7 days after it was created; a finished job is kept for 7 days
after finishing, and as long as any kept key names it. Active jobs are never pruned.
At most 32 jobs queue at once. A queued automatic (poller) intent runs only while the
poller flag is still on and the connection is still eligible; otherwise it finishes
`deferred/background_disabled` before any credential read. The reported
`next_attempt` includes leases and Retry-After, and is `null` when every requested
component (or the shared scope) is suspended and needs `limits resume`.

Job outcomes are `updated`, `partial`, `failed`, `deferred` and `interrupted`, each
with a safe error code and the next stored start time. Components skipped because
their cadence is not yet due make a whole-connection job `partial` without an
error, as in `python -m hestia.budget once`; a job with nothing due finishes
`deferred` before any secret read or HTTP. A job whose connection changed version
(pause/resume, background opt-in change) finishes `deferred/control_changed`
without a bank request. If another process (for example the CLI) holds the budget
lock, the job stays queued and is retried; after 15 minutes of contention it
finishes `deferred/busy` without consuming any debit.

`POST /api/budget/background-selection` (`{connection_id, enabled,
expected_version}`, browser-only like the other controls) persists the owner's
per-connection opt-in with a private audit row and a new control version. The
poller requires all of: the global flags, that opt-in, an enabled admin owner, a
configured connection that is not paused, at least one selected account, no active
job and a due component. Config reload never restores an opt-out. The poller reads
no key or session file and makes no HTTP request until a component is due, and it
does not replay missed ticks after downtime.

An `authorization`, `invalid_session`, `expired_session`, `session_mismatch` or
`configuration` failure sets the connection's `attention` code, and takes precedence
over any other component error of the same round. Losing the authoritative SQLite
users marker stops the round at its next reservation, request or commit with
`configuration`; consumed debits stay. A targeted refresh records no error, debit or
write for components it did not request. The poller then
skips that connection until an owner refresh (or an explicit CLI `once` round)
validates the session and stores data; configuration reloads, status reads and
poller ticks never clear it. This applies equally to failures from CLI rounds.
No SCA starts in the background. Fixing a session
file or key therefore needs one explicit owner refresh, under the normal quota.

Shutdown stops taking jobs and waits up to 5 s. A running acquisition stops at
its next request boundary with outcome `interrupted`; a request already in flight
is never reported as cancelled. A job still `running` when the process dies is
marked `interrupted` by the next worker that holds the budget lock. Its debits,
lease and cooldowns stay in force, and it is not replayed.

### Activation runbook

1. Deploy with all budget flags unset; back up the SQLite database first.
2. Update the privacy policy/terms for bank data, then enable and verify
   `HESTIA_BUDGET_HTTP_ENABLED` on its own (see the 2B.1 checks above). If HTTP was
   verified before step 4, turn it off again for that step.
3. Stop every competing acquisition for the same application, bank and accounts
   (including the PoC). Hestia cannot see their requests: carry verified recent
   attempts into conservative quota state or wait a full rolling window and any
   longer server cooldown. Waiting until midnight is not a fresh allowance.
4. Mount only the owner-authorized external session files and key, read-only,
   owner-only (`0400`/`0600`, process UID). With `HESTIA_BUDGET_HTTP_ENABLED`,
   `HESTIA_BUDGET_JOBS_ENABLED` and `HESTIA_BUDGET_POLLER_ENABLED` actually off in the
   running process, import history and record the owner's initial account
   selections and per-connection opt-ins (CLI, audited). Only then enable HTTP.
   Never repeat that initial selection step once the owner can use the budget
   screens: it would overwrite later deselections made there. Later changes go
   through the versioned screen controls, or as explicit operator changes made
   only after checking the owner's current choices (the CLI sends no version).
5. Set `HESTIA_BUDGET_JOBS_ENABLED=1`, restart, and run one owner refresh for one
   connection. Check the job outcome, `/api/budget/limits` and that the logs
   contain no financial values.
6. Only after that verification set `HESTIA_BUDGET_POLLER_ENABLED=1`. The poller
   honours the opt-ins persisted at that moment; never re-apply the staged choices
   from step 4, which would undo a later opt-out made in the screens. To stop
   unattended work, opt out, pause the connection, or unset the poller flag and
   restart.

## Budget screens (2C)

Admins see a **💰 Budget** tab only after `GET /api/budget/connections` succeeds for
them: the HTTP flag is on, users are authoritative in SQLite, and the signed-in
admin owns the configured connections. Each admin sees only their own budget.

- **Import and selection are separate.** Imported history is stored whether or not
  an account is selected. *Include in budget* is the owner's persisted, audited
  choice; configuration reloads, restarts and imports never re-select an excluded
  account, and newly configured accounts are never selected automatically.
- **Other controls are independent:** the explicit account type (never inferred
  from currency or product name), pausing a connection (stops acquisition, keeps
  history) and *Automatic refresh* per connection. The opt-in takes effect only
  while the server's poller flag is on; the screen says so otherwise. Each control
  sends its current version; if it changed elsewhere, the screen reloads the
  current state instead of overwriting it.
- **Refresh from bank** appears only while the executor is enabled; otherwise the
  connection reads *Refreshing is disabled on the server*. A refresh queues a job
  under the normal quotas. The card shows queued/running and then the outcome:
  updated, partly updated, not due yet, failed or interrupted, with the next
  possible start. *Attention* means unattended refresh stopped after an
  authorization or session problem; a successful manual refresh clears it, and
  reconnecting may be needed. Reconnect and renewal are not in the screens yet.
- **Account detail** shows current balances from the bank interface. Earlier
  imported balance snapshots are listed as metadata only, never as current funds.
  Transactions are newest first, with 30/90-day, this-year and all-time presets,
  a validated date range and paging. Amounts are exact decimal strings; nothing
  is summed across currencies. Provider-declared currency and currencies seen in
  current balances are shown separately.
- **Privacy in the browser:** financial data stay in page memory only. The screen
  does not watch the session continuously: it clears everything immediately when
  any budget response reports lost access or a disabled budget, or when the
  identity check on reload shows another user or a lost admin role. Signing out
  in the same tab reloads the page. Browser storage holds only general interface
  preferences, such as the last chosen tab, never budget data.

## Acquisition and durability

Each run validates the authorised session, bank and selected account mappings;
then retrieves transactions and balances independently. Transactions publish only
after **every** page validates; balances publish only after their complete response
validates. An endpoint failure preserves that endpoint's previous data while its
successful sibling can refresh. Successful components and safe errors commit in
one SQLite transaction. Network I/O never holds a database write transaction. Hestia's
existing SQLAlchemy/Alembic engine supplies WAL, FULL durability and migrations.

The requested window covers 365 calendar dates through today, with boundaries
explicitly calculated in UTC (matching the provider API). Dates remain calendar
dates without invented times or conversions. Returned records
are filtered locally because banks may ignore the requested start date. A year
requested is not a promise that a bank supplies a complete year. Booking date is
used first, value date second; undated records are marked unresolved.

Repeated pages, malformed rows and partial/network errors fail that component.
Missing
selected accounts, changed bank identity and invalid/expired sessions fail the
connection before any component is imported; a late HTTP 401 also discards all
staged results. Previously committed data for failed components remain intact.
The CLI returns a partial/failed state and a nonzero exit code; only a completely
successful connection advances its full-success timestamp. Absence in a later
response never deletes historical ledger entries. A newer BOOK entry cannot be
downgraded to PDNG. Canonical rows are upserted by account and entry reference;
unresolved observations represent the last complete snapshot with multiplicity.

A database-specific process lock prevents overlapping CLI runs. Successful imports
and failures persist timestamps and retry state. Restarting cannot lose committed
history. Failed transient calls are retried by a later explicit `once` invocation,
with persisted endpoint quota/cadence reservations made before HTTP. Authorization/data errors
require correction before useful progress; CLI never loops in a tight retry.
Distinct observations using the same reference are retained in unresolved
quarantine. Any previous canonical row using that reference is marked uncertain
and excluded from canonical counts, without deleting its history. A subsequent
complete unambiguous observation can resolve it. Other records still import.
Uncertain historical rows stay excluded even if the reference is absent later;
the unresolved count describes the latest complete observations, not an erasure
of earlier uncertainty. A reconciliation UI remains a later slice.
There is no server poller or cron installation in this stage. A production poller
should default to two runs per day, with bank-specific limits and manual refresh
rules verified before enabling it.

Migration 0009 adds separate transaction/balance attempt, success, safe-error and
retry metadata. Only a complete API account observation backfills both success
times; offline imports cannot advance them. Each endpoint persists a round marker
and a 15-minute running lease before HTTP. Restart retains attempts and cooldowns;
an expired running marker is shown as interrupted, without inventing a success.
Migration 0010 adds independent endpoint scheduling, retaining existing connection
deadlines as a shared floor. A failing card endpoint can no longer consume the
eligibility of a healthy account or its other endpoint.

Status adds `accounts[].components.transactions` and `.balances`, with states,
age, stale flag and explicit freshness thresholds (36 hours for transactions,
24 hours for balances). These are acquisition ages; absent bank observation times
remain absent. A completed fetch does not prove the bank returned its full history.
`running` means an unexpired request lease; `error` also covers an interrupted
lease. `never` has no complete API observation, `deferred` is waiting for its
deadline, `blocked` cannot currently acquire, `ok` is fresh and `ready` is stale
and eligible for a later refresh. Errors and their deadlines remain visible.
Account/connection `last_success` still means both components completed for the
entire selected scope. Partial success does not advance this combined timestamp.

All calls in this stage are unattended and omit **all** PSU headers. Do not forge
IP/User-Agent values or assume an interactive request has unlimited quota.
Future interactive refresh must derive real PSU context through an explicitly
trusted reverse-proxy configuration; arbitrary forwarded headers are untrusted.

### Endpoint quotas and transport pacing

The default operational ceiling is two acquisitions per **rolling 24 hours**, at
least 12 hours apart, for each stable local account and operation. This is not a
claim that every bank grants that allowance. Configuration may make the ceiling
stricter; verify the bank's actual limits before enabling unattended acquisition.
For example, a connection may add this optional policy object:

```json
"policy": {
  "max_acquisitions_24h": 1,
  "minimum_interval_seconds": 86400,
  "request_interval_seconds": 2
}
```

Omitted policy fields default to `2`, `43200` and `1`. Allowed values are 1–2
acquisitions, 43200–604800 seconds between acquisition starts, and 1–60 seconds
between application HTTP requests. Existing version-1 configurations still work.
Each transaction collection, including all continuation pages, consumes one
durable acquisition. Errors, process termination and expired running leases do not
refund it. If cancellation, expiry or a newly discovered shared block occurs
after reservation but before HTTP, that reservation also remains consumed. This
conservative cost prevents an uncertain interrupted request from gaining a refund.
New session IDs, configuration reloads and manual `once` invocations
cannot reset quota history. Migration keeps conservative legacy attempt debits
and the old shared connection deadline, so upgrade cannot grant a fresh allowance.

Every GET, including session metadata and continuation pages, separately reserves
an application transport slot. Short pacing waits happen outside database write
transactions; long cooldowns defer work. Owner, selection, current round lease and
shared limits are checked again before network work. When no endpoint is due,
`once` defers before reading credentials or requesting session metadata.

An allowlisted `ASPSP_RATE_LIMIT_EXCEEDED` on an account endpoint delays only that
account/operation. Unclassified and session-level 429s delay the connection.
Shared application/provider floors are always consulted, but no wider scope is
guessed from an arbitrary error message. The current API supplies no distinct
application-wide error code. All rate errors retain at least a six-hour floor
plus consumed cadence/quota and any longer valid `Retry-After` deadline.
Seconds and HTTP dates are supported; malformed/past hints use the fallback.
Oversized hints, delays over seven days or reaching beyond session validity
suspend automatic acquisition without truncating the deadline. Status exposes
the deadline/suspension; no automatic reset occurs.
After checking the provider response or confirming an unknown hint was invalid,
the local operator can list limits and record an explicit review:

```sh
python -m hestia.budget --config /secure/budget.json --db /secure/test.db limits list --owner existing-admin
python -m hestia.budget --config /secure/budget.json --db /secure/test.db limits resume --owner existing-admin --connection personal-bank --scope component --account LOCAL_ACCOUNT_ID --operation transactions
```

Resume clears only the suspension flag and appends an atomic operator audit. It
does not shorten any retained server deadline, backoff, cadence or rolling quota,
and does not select an account. Valid future deadlines still defer requests after
review. Other explicit scopes are `connection`, `application` and `provider`;
application/provider reviews can affect other connections and are privileged
local operator actions. These commands read neither keys nor session files and
have no HTTP equivalent. Repeating a completed review is a no-op.

Requests use at most a 60-second socket timeout and a 15-minute monotonic round
deadline, checked between bounded response chunks and before/after HTTP. An
already pending socket read may finish within its assigned timeout; data arriving
after the round deadline is discarded. An incomplete transaction page sequence
preserves the previous ledger/quarantine; independently complete components may
still publish. No immediate HTTP retries occur.

## Secrets and local storage

Use the project's deployment environment and external mounted files. Set
`HESTIA_BUDGET_CONFIG` to the JSON configuration path. Key and session files must
be regular, owner-only files owned by the process user, with no symlink at the
final path component. They are never read from browser storage, logged or copied
into SQLite. For bind-mounted secrets, host file ownership must match the
container process UID; changing mode alone does not change ownership.
Protect their directories and the SQLite DB, WAL files and backups
with filesystem permissions. SQLite financial data is **not encrypted at rest**
by this module. Do not commit bank exports, raw responses, signing keys or
session files to git. This foundation does not read or migrate the existing PoC
cache, and does not share a PoC session implicitly.

Example configuration (all values below are placeholders):

```json
{
  "version": 1,
  "application_id": "00000000-0000-4000-8000-000000000001",
  "key_file": "/run/secrets/enable-banking.pem",
  "connections": [{
    "id": "personal-bank",
    "owner": "existing-admin",
    "bank_name": "Bank name returned by Enable Banking",
    "country": "PL",
    "session_file": "/run/secrets/personal-bank-session.json",
    "accounts": ["primary-identification-hash-selected-by-owner"]
  }]
}
```

Session file: `{"session_id":"00000000-0000-4000-8000-000000000002"}`.
The account hash must come from the authenticated account-selection flow or a
secure operator workflow; hashes are sensitive identifiers and should not be
published. Empty `accounts` selects nothing. The actual provider bank name must
match; example names and these UUIDs cannot grant access.

```sh
python -m hestia.budget --config /secure/budget.json --db /secure/test.db status
python -m hestia.budget --config /secure/budget.json --db /secure/test.db once
```

Since migration 0008 (selection policy 2), the version-1 configuration's account
list is a hard **allowlist**, separate from persisted owner selection. `status`
reconciles that allowlist without contacting a bank. It never selects an account
or reverses an explicit opt-out. `status --stored-only` also skips allowlist
reconciliation. Neither command reads private keys/session files.

### Explicit account controls

Existing selected accounts keep their selection during upgrade. Newly configured
accounts start unselected, with kind `unknown`; configuration, bank consent and
offline import do not select them or confirm a kind. Acquisition requires both
the allowlist and persisted owner selection. Removing an account from configuration
stops acquisition without erasing its identity, history or prior selection; an
explicit deselection persists after removal/readdition, status and restart.
Changing the selected scope during acquisition aborts staged publication and can
consume that round's reservation; the next run respects the existing cooldown.

```sh
python -m hestia.budget --config /secure/budget.json --db /secure/test.db accounts --owner existing-admin
python -m hestia.budget --config /secure/budget.json --db /secure/test.db account select LOCAL_ACCOUNT_ID --owner existing-admin
python -m hestia.budget --config /secure/budget.json --db /secure/test.db account confirm-kind LOCAL_ACCOUNT_ID --owner existing-admin --kind credit_card
python -m hestia.budget --config /secure/budget.json --db /secure/test.db account deselect LOCAL_ACCOUNT_ID --owner existing-admin
```

These privileged local CLI actions act on behalf of the named, existing enabled
admin owner. They do not establish an authenticated browser principal. Listing
and mutation are owner-scoped: another owner's ID is rejected without returning
that account's data. The operator actor is recorded as `operator:<process uid>`;
there are no caller-supplied actor/origin flags. Web controls use the separate
guarded HTTP surface described above and audit the authenticated owner.

Confirmable kinds are `current`, `savings`, `credit_card` and `other`. Kind does not
infer currency or select an account; selecting does not confirm kind. Different
currencies and balance types remain distinct observations. No product-name/CACC
heuristic confirms the type. Repeating a selection or an already-confirmed kind
is a no-op. Changed choices commit with an append-only audit containing actor,
owner, local account ID, time, field, enum/boolean changes and origin, without
amounts, IBANs or descriptions. Audit/confirmation survives restart and does not
change ledger records, balances, consent validity or quota reservations.

`once` fetches only the intersection of configured and owner-selected identities,
honoring persistent pause. It uses the independent balance/transaction freshness
and account/endpoint retry budgets implemented in 2A.2/2A.3. Reading the private
HTTP API does not run it or change those reservations.

Use a separate test DB and fake API fixtures during development. Live acquisition
uses the fixed HTTPS Enable Banking origin, validated RS256 JWTs, bounded
responses, a GET-only route allowlist and no HTTP redirects. Errors expose safe
codes only. There is no custom production API URL setting.

Migration 0006 adds four empty tables on the next startup of any upgraded Hestia
SQLite deployment, even when acquisition is disabled. Merging this foundation
therefore changes the schema at that next restart; it does not start bank access.

## Offline JSON history import

The operator can import a complete local export of Enable Banking responses:

```sh
umask 077
python -m hestia.budget --config /secure/budget.json --db /secure/test.db import-snapshot --file /secure/account-history.json
python -m hestia.budget --config /secure/budget.json --db /secure/test.db status --stored-only
```

This is a server/operator CLI feature. There is no HTTP upload, browser importer,
bank request, credential resolution or automatic file discovery. The command is
generic for the **Enable Banking response schema**; arbitrary JSON, bank-specific
CSV and MT940 are not supported formats. The existing configuration still
explicitly selects the account hash and identifies its enabled admin owner;
credential file references may remain unavailable for a purely offline import.

One file describes one selected account:

```json
{
  "version": 1,
  "connection_id": "personal-bank",
  "aspsp": {"name": "Bank name returned by Enable Banking", "country": "PL"},
  "identification_hash": "primary-identification-hash-selected-by-owner",
  "fetched_at": "2026-11-19T12:00:00+00:00",
  "complete": true,
  "transactions": {
    "transactions": [],
    "continuation_key": null
  },
  "balances": {"balances": []},
  "account": {
    "product": "VISA PREMIUM",
    "currency": "PLN",
    "masked_suffix": "1234",
    "observed_at": "2026-11-18T09:00:00+00:00"
  }
}
```

`transactions.transactions` contains raw transaction objects from the API;
`balances.balances` optionally contains raw balance objects. Combine all pages
from the completed export before importing. A remaining continuation key,
explicit error, missing completeness declaration, duplicate JSON key or malformed
row rejects the **entire file before opening the database**. `complete: true` is
the exporter's declaration of a completed acquisition, not proof that the bank
provided every historic transaction. `fetched_at` is the actual source observation
time, must include a UTC offset and cannot be in the future. No session UID or
session ID is needed; primary account hashes match across reauthorisations.

### Optional private account display metadata

The optional `account` block above contains exactly four required fields when
present. `product`, `currency` and `masked_suffix` may each be null; `observed_at`
must be the actual account-metadata observation time, with a UTC offset and no
future timestamp. It is independent of transaction `fetched_at` and the export
time. Omit the whole block when the observation time or metadata is unavailable.
Old version-1 exports without this block remain valid and do not erase labels.

Products accept up to 1024 input characters. Hestia normalizes Unicode, removes
control/format characters, collapses whitespace and limits the display to 64
characters. It replaces identifier-like products with null before truncating,
including long digit sequences and IBAN/UUID-like values, and reports a safe
`product_rejected` count. This does not block valid history import. An empty
product also becomes null. Currency
is syntactically three uppercase ASCII letters, consistent with transaction
normalization; no current ISO-code membership list is enforced. The suffix is
exactly four uppercase ASCII letters/digits, calculated by the private exporter.
Never place a full account number, card number, holder name, account type, provider
UID or session identifier in this block. Unknown fields are rejected. The synthetic
example above does not identify a real account.

Migration 0012 adds a separate display row per account; existing accounts have no
display row. Metadata has its own context-bound digest and never changes the
transaction batch fingerprint. Within the same import transaction, metadata is
applied before the duplicate-batch return. Thus adding a description to an already
imported export can update only its display metadata; ledger rows, original batch
counts and quarantines remain unchanged. A metadata conflict rolls the entire
import back. Repeated identical metadata is a no-op, older observations are
ignored, and different metadata at the same observation time is rejected with
the fixed diagnostic `metadata_conflict`. Null
fields preserve existing values; an all-null patch does nothing. The displayed
observation time describes the last accepted patch, so retained fields can be
older. Hestia assigns source `offline_import` and its own ingestion time.

The CLI reports only metadata outcome state and the product-rejection count, never
its values or digest. Only the
existing owner-private HTTP account response exposes `display` with product,
declared currency, suffix, source and observation/ingestion times. Metadata does
not enter shared audit, SSE or logs, and does not change selection, confirmed kind,
control versions, API freshness, balances, quota or backoff. Currencies observed
in current API balances are a separate sorted list; archived import balances are
excluded. These labels are not evidence of credit-card debt or repayment dates.

No additional bank request, account discovery or consent exchange is introduced.
Use a separate private test database for exports and migration/import pilots.

### History validation and storage

Files must be regular, owned by the process user, mode 0400 or 0600 and not a
symlink at the final component. Limits are 32 MiB input, 32 MiB normalized data
and 100,000 total transaction/balance observations before deduplication. All
records use the same amount/date/status normalization as API acquisition. There
is **no 365-day filter** in the importer, so older history is preserved. Unknown
fields inside a transaction are not persisted, including volatile detail IDs.

The whole file, its provenance record and any newly required metadata commit
atomically. New offline-only connections/accounts are stored disabled; existing
selection flags remain unchanged. The enabled admin owner and selected hash are
checked again inside the database transaction, including on repeated imports.
Bad identity or a write failure rolls the entire file back.

Matching is by local account plus stable `entry_reference`. Only dated BOOK
records enter canonical counts. Missing references/dates, other statuses and
conflicts go to a **separate offline quarantine**, preserving multiplicity without
replacing the current API quarantine. Any prior API observation or API conflict
decision takes precedence over every offline file, even a newer file. An offline
import cannot clear uncertainty established by the API.

Among offline-only observations, strictly newer source timestamps may update a
record; older files cannot overwrite it or introduce a newer uncertainty flag.
Equal-time conflicting records remain uncertain. A durable uncertain placeholder
also protects a reference that has only ever had conflicting observations, so an
older file cannot later publish it as a safe transaction. A newer unambiguous
offline observation may resolve an offline-only conflict; the API can resolve it
through normal acquisition.

Each canonical entry identifies its source (`api` or `offline_import`) and source
observation time. Import batches separately record source time, ingestion time,
normalized content digest and outcome counts. A normalized repeat (including JSON
formatting/key-order changes, reordered rows or changed volatile detail handles)
is idempotent. Unreferenced identical transactions retain their real multiplicity.
The report on a repeat identifies the previous batch and returns zero per-run
outcome counts. The original batch counts remain stored for provenance.
The CLI batch identifier is an opaque UUID; the content fingerprint stays in the
database. API BOOK conflicts now also retain an uncertain placeholder when no
canonical entry existed before, so the uncertainty count can increase for that
case even without an offline import.

Imported balances are archived with their batch. They never replace a live balance
or imply current available funds. Import does not change API last-success times,
errors, consent validity, quota reservations or backoff. Status reports offline
batch/transaction/quarantine counts separately and prints no financial payloads,
account hashes, bank references or filenames.

Original files remain with the operator; import does not remove them. Protect and
retain/delete them according to the operator's storage policy. Imported SQLite
history and quarantine persist until explicitly erased by the operator; this
module has no automatic financial retention/erasure job. Protect the database,
WAL files and backups too; none of this storage is encrypted by the module.

Migration 0007 preserves existing records as API observations and adds provenance
and offline batch/quarantine storage. It does not import any file or enable bank
access at startup. Run pilots against a separate private database. Before storing
bank records in the live service, update the published privacy/terms documents to
describe the actual enabled feature and retention practices.
Keep exporter/importer clocks synchronized; a source timestamp ahead of the
importer's clock is rejected rather than silently adjusted.

## Next slices (not implemented here)

1. Browser consent and renewal. Reserved
   callback: `https://hestia.klatt.ie/api/budget/enable-banking/callback`; **no route
   exists yet**. Use expiring single-use state bound to user, browser and bank;
   keep callback query secrets out of access logs, referrers and page content.
2. Reminder seven days before actual provider `valid_until`; an **Odnów** action
   verifies and adopts a new session without automatically closing external/PoC
   sessions. Some banks invalidate the old consent during new SCA; retaining the
   old local reference does not guarantee it remains usable. No universal 180-day
   assumption. Status already reports renewal due.
3. Verify ING/BNP field shapes with owner-approved PoC sessions. Do not infer
   support for cards, pending entries or statement due dates from bank branding.
4. Year view and conservative categorisation/subscription hints with manual
   correction. The owner must explicitly choose each account's kind (current,
   savings, credit card or other); CACC or a product name is insufficient.
   Card repayments/internal transfers must not duplicate spending;
   recurring transactions do not prove unused subscriptions. Mortgage due dates
   and forecasts require explicit schedule inputs and freshness indicators.
5. Decide export, erasure and retention before broader use. Disabling a connection
   stops acquisition and retains history; provider revocation is not local erasure.
6. Staged live activation follows the runbook above. No bank data is sent to
   external AI tools.
7. Authenticated UI for limit inspection and operator review, preserving the CLI's
   deadlines and audit. Do not erase quota history to compensate for a partial result.

## Sources

- [Enable Banking API reference](https://enablebanking.com/docs/api/reference/)
  (SessionAccount/GetSessionResponse, Transaction, balances, pagination).
- [Enable Banking FAQ](https://enablebanking.com/docs/faq/) (stable identity,
  transaction references, PSU headers, limits and reauthorisation).
- [Quick start](https://enablebanking.com/docs/api/quick-start/) (JWT and AIS flow).
- [Linked accounts](https://enablebanking.com/docs/api/linked-accounts/) and
  [terms](https://enablebanking.com/terms/) (restricted own-account access).

Verified against the public documentation on 2026-11-20. Tests use synthetic
records; production account data and credentials are not test fixtures.
