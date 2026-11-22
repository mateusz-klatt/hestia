# Budget 2D: spending, categories and own-account transfers

Status as of 2026-11-22: implemented and deployed opt-in local analytics using
migrations `0015`, `0017`, `0018` and `0019` (OpenAPI `0.17.0`–`0.24.0`, PRs `#182`–`#217`).
The canonical ledger, explicit account selection and consent implementation remain the
source of truth. Suggestions require owner review before changing totals.

Account kinds and report scope are the owner's explicit choices. Respect selected
and excluded accounts without changing those choices during classification or
matching. When account numbers are unavailable, equal amount/currency and dates
within ±3 calendar days may suggest a transfer, subject to the review and manual
override contract below. Extra financial-database encryption is not a prerequisite.

## Scope and exact amounts

Reports use the owner's explicitly selected accounts. Pausing acquisition,
provider expiry or a temporary loss of availability changes freshness labels, not
historical spending. Deselecting an account removes its entries from reports and
causes relevant transfer relationships to be reassessed at the scope boundary.

Use canonical BOOK entries keyed by `(account_id, entry_reference)`. Unresolved observations (including pending entries) and uncertain observations
are counted separately from report totals. No fuzzy identity
matching or merging of bank records is introduced. Use `booking_date`, falling
back to `value_date`; an observation timestamp is not a booking date.

Aggregate exact decimal strings on the server with explicitly sufficient precision
or integer coefficients, separately for every currency. Do not use SQLite REAL,
SQL floating-point SUM, JavaScript Number, implicit Decimal precision or currency
conversion. Totals cover the whole requested period, not the current UI page.

## Transfer matching

Generate candidates before filtering the report month, so transfers crossing a
month boundary can be recognised. Every pair has exactly one DBIT leg and one
CRDT leg, on different selected owned accounts, with equal exact amount and
currency and booking/value dates at most three calendar days apart, inclusive.
Zero amounts, invalid dates and uncertain observations cannot form pairs.

When available, compare structured counterparty account identifiers with the
owner's known accounts. Keep bank-provided evidence separate from inferred
evidence. A separate structured metadata sidecar enriches API/import observations
without changing canonical ledger payloads or transaction identities. Do not infer an IBAN from a
bank's primary hash or session UID. Missing identifiers do not prevent the fallback.

Without usable account numbers, an isolated mutual one-to-one candidate can be
suggested as **possible own transfer**, with reason “equal amount/currency, dates
within 3 days”. It remains in spending totals until accepted. Multiple compatible
legs are ambiguous: show choices and never arbitrarily take the nearest date or
first row. Conflicting structured account evidence prevents heuristic auto-pairing.

The owner can accept, reject, manually pair or undo a pair. Persist that decision,
its source (`account_identity`, `heuristic`, `manual`), actor, time, rule version
and versions/fingerprints of both source entries. One entry can belong to at most
one effective pair. A rejected/undone suggestion must not reappear automatically.
Material source changes mark the old decision for review rather than silently
reapplying it. Preserve the original bank entries and the decision history.

An accepted own-transfer pair excludes both legs from income and spending. Card
purchases remain expenses; repayment of that card is an own transfer only when
its two legs have been matched. A CRDT refund is not automatically income. A
transfer with only one leg inside the current selection is a visible boundary
case, not proof that an excluded account should be added to the report.

## Categories and UI

Store category/annotation decisions separately from the bank payload. Priority is
explicit manual choice, deterministic owner rule, then uncategorised. Explicitly
choosing “uncategorised” differs from resetting to automatic classification.
Rules have a preview showing affected entries before activation; edits and bulk
actions use versions and preserve manual choices.

Start with a month view, currency selector, category totals, transaction search,
transfer-review queue and persistent manual corrections. Explain excluded,
uncertain and stale data beside totals. A year view follows the same server-side
totals. Optional monthly category limits are organisational aids.

Recurring-charge hints require at least three observed monthly occurrences and
show their evidence. They do not establish that a subscription is unwanted or
unused. Mortgage reminders require the owner's schedule, amount/currency and
chosen notification destination. Do not automatically copy private financial
details to shared calendars. No bank payment operation is introduced.

## Required synthetic verification

Cover exact large/very small decimals, mixed currencies, date boundaries and
month crossings; unique and ambiguous candidates; different account ownership;
excluded accounts; duplicate/reordered imports; uncertain/pending entries; card
purchases versus repayments; refunds; persisted reject/undo/manual override;
source correction, A→B→A review latching and stale fingerprints; concurrent accepts and restart; full
period totals across multiple UI pages; owner-only API/UI and freshness labels.
Use synthetic fixtures rather than real household transactions in CI or reviews.

## Implemented local analytics (schema 0015)

Set `HESTIA_BUDGET_ANALYTICS_ENABLED=1` together with the existing authenticated
SQLite budget HTTP surface. It is off by default. `connections.capabilities.analytics`
announces availability; older clients can ignore the additive field. This flag
exposes reports and local classification controls, without enabling acquisition,
changing selected accounts, altering request limits or contacting a bank.

The React budget page adds month/year reports, exact totals by currency,
transaction filters and classification, categories, a transfer review queue and
rule preview/save. The exact admin owner remains the only authorised principal.
Money is sent as decimal strings. The server uses scaled integers, with no FX.
`expenses` are DBIT expenses; CRDT starts as `unclassified_credit` until explicitly
classified as income or refund. `net_spending` is expenses minus refunds, not an
account balance. Gross debits/credits, excluded entries and transfer legs remain
separately visible. Uncertain/unresolved counts describe all owner accounts and
are labelled separately from the selected report scope. Freshness is shown per
selected account; pausing acquisition does not remove history.

All routes below are under `/api/budget/analytics/`, require the exact enabled
SQLite admin owner, and make no provider requests:

- GET `overview?period=YYYY-MM` or `period=YYYY`; optional `currency=PLN`.
- GET `transactions?period=...`; optional currency, category_id, kind, query,
  limit (1–200) and cursor. `category_id=uncategorised` means no effective category.
  Filtering never changes full-period overview totals.
- GET `categories` / `rules`.
- GET `transfers?period=...&status=suggested`; other statuses are accepted, review,
  boundary, rejected and undone. The graph is built before date filtering.
- POST `category-create`, `category-update`, `annotation`, `rule-preview`,
  `rule-save`, `transfer-decision`. Requests require the configured HTTPS Origin
  and a browser session, and reject duplicate/unknown JSON fields.

Annotations require the row's current `fingerprint` and local expected_version;
transfer accept/reject requires both current `transfer_fingerprint` values.
A source correction returns 409 `budget_analytics_source_changed`. Stale entity
versions return `budget_analytics_conflict`; reload and review before resubmission.
One accepted pair reserves both legs in the same SQLite transaction. Manual
pairing can override conflicting structured evidence after an explicit owner
choice, while retaining equal amount/currency, opposite directions, distinct
selected accounts and the inclusive three-day window. Reaccepting a rejected or
undone pair is an explicit manual action. Undone pairs never automatically return
as suggestions. A corrected accepted pair is persistently marked for review during ingestion,
keeps its legs reserved and affects no totals until reconfirmed. Returning to an
earlier source value does not reactivate the old decision. A pair crossing the
current account selection is a boundary case and does not hide the selected leg.

Rules use literal casefolded contains/equals on description, counterparty_name
or MCC, sorted by priority then stable ID. Preview is mandatory for creation,
editing, enabling and disabling. The 15-minute preview binds owner selection,
ledger/evidence and local classifications; changed input returns 409
`budget_analytics_stale_preview`, expiration returns `budget_analytics_preview_expired`.
Replaying a saved preview returns its saved rule without duplicate creation.
Manual annotations, including explicitly uncategorised entries, take precedence.
An archived category can be retained when editing the current classification of
that same entry. Disabling an existing rule may retain its archived category;
creating or re-enabling a rule still requires an active category.
Stale manual annotations remain marked for review and use safe default
classification until reconfirmed. Valid accepted transfers outrank rules.
Entries currently classified as excluded, manually or by an owner rule, generate
no new transfer suggestions or ambiguity edges; existing decisions and reservations remain unchanged.

Lists use encrypted cursors bound to owner, filters and the complete projection.
A changed snapshot returns 409 `budget_analytics_stale_cursor`; restart paging.

Reads reuse one projection per committed state. Each GET first rechecks the exact
admin owner, then hashes everything the projection reads for that owner: every
column of its connections, accounts, selected ledger, evidence and annotations,
own identities, categories, rules and decisions, the owner-wide uncertainty
counts, and the row and edge limits. Excluded accounts' ledgers are not read; the
selected ledger is bounded by the row limit before anything else, returning the
same 413. A projection computed for that digest is reused; any committed change
gives a new digest and a fresh computation, so nothing is invalidated by hand.
Concurrent reads of one state share a single computation, and analytics reads run
one at a time, because CPU-bound reads only interleave under the GIL. The memo
lives in the server process's memory only, keeps one state per database and owner
(at most four), never reaches disk, logs or the browser, and is emptied by a
restart. Freshness, cursors and paging are still evaluated per request. Writes,
rule previews and their stale-preview check always compute a fresh projection.
There are explicit limits of 100,000 selected canonical rows, 100,000 candidate
comparisons, 100 categories/rules and 100 pending previews per owner. A row-limit
failure returns 413 without partial totals. A candidate-scan limit is visible as
`scan_limited`; suggestions are withheld and existing valid accepted decisions
remain effective. No nearest-date or first-row choice resolves ambiguity.

Migration 0015 adds separate account/transaction evidence, categories,
annotations, rules/previews, transfer decisions/reserved legs and a private
classification audit. It does not modify original ledger payloads, transaction
keys, acquisition quotas, account choices or existing consent state. Annotation
and decision audits remain owner-private, outside shared home audit and SSE.
Back up this SQLite database with the existing credential capsules consistently.

Structured evidence is optional. Existing API responses and explicit import-v1
files may provide creditor/debtor names, supported account identifiers and MCC.
There are no additional provider requests. Evidence is separate from canonical
bank records and bound to the current financial fingerprint. Import-v1 may also
include `bank_metadata: {account: {...Enable Banking account fields...},
transactions: [...Enable Banking transaction fields with entry_reference...]}`.
Repeating an already imported core batch can fill missing evidence without
adding transactions. Only IBAN, BBAN, BGNR and PGNR account schemes are retained;
personal identifiers, masked card numbers and OAuth-token schemes are skipped.
Supported identifiers are normalised conservatively; a validated Polish NRB can
supply an IBAN alias only with trusted Polish bank context. Full identifiers are
stored locally for evidence and are not exposed in analytics responses. Per-field
source/time preserves existing API values while allowing older imports to fill
missing fields. Unknown/invalid optional fields are counted and skipped.
Own identifiers require actual account fields; transaction frequency, primary
hashes and session UIDs do not establish account ownership.

Recurring-charge hints (`0.18.0`), persisted monthly spending limits (`0017` / `0.19.0`),
evaluation-only mortgage schedules (`0018` / `0.20.0`), read-only consent renewal and
reconciliation views (`0.21.0`–`0.22.0`), and the live owner inbox (`0019` / `0.23.0`–`0.24.0`)
are documented in the sections below. Hestia still never infers a subscription is
unwanted, never initiates bank payments, never copies private financial details to shared
calendars, and runs no outbound email/push delivery daemon.


When a new bank consent is adopted, structured identifiers from POST `/sessions`
are retained only for the account selection captured before that one-time request.
They are published to account evidence in the same SQLite transaction as adoption,
only after GET session verification confirms bank, personal access, expiry, primary
hash-to-UID mapping and all owner/control fences. Pending private evidence survives
restart; full identifiers of returned extra accounts are discarded. No additional
bank request is introduced. Earlier consents are not re-exchanged automatically;
missing identifiers continue to use reviewable heuristic suggestions until an
ordinary renewal or an authorised import supplies actual account fields.

A boundary indicator describes an existing accepted relationship crossing the
current selection. New candidates use selected ledger entries only; Hestia does
not scan excluded transaction history for additional heuristic partners. A selected
outflow without a known accepted relationship remains spending within that scope.

## Recurring hints and persisted category limits (schema 0017)

- `GET /api/budget/analytics/recurring` (optional `currency`) exposes bounded,
  evidence-backed monthly recurring-charge hints from selected eligible expenses,
  omitting accepted own transfers, excluded entries and internal fingerprints.
- Migration 0017 adds the owner-private `budget_category_limits` table (`(owner, category_id, currency)` unique,
  restrictive foreign keys, `category_id NOT NULL` — uncategorised limits are not supported in 0017,
  and CHECK constraints on `version`, `warning_percent`, `enabled` and `monthly_limit` length).
  Monthly limits are ongoing organisational settings: editing a limit applies to
  historical month evaluations as well (effective-dated limits are a future extension).
- `GET /api/budget/analytics/spending-limits?period=YYYY-MM` (optional `currency`) reads
  saved limits and the memoised projection in one SQLite read transaction, returning both
  `snapshot_digest` (ledger/classifications) and `limits_version` (saved limits digest).
  Disabled limits appear in `limits` only; `statuses` evaluates enabled limits without FX.
- `POST /api/budget/analytics/spending-limit-save` atomically creates or updates one
  `(category_id, currency)` limit under `BEGIN IMMEDIATE` with strict CAS (`expected_version=0`
  for create, `>=1` for update; no silent upsert). Retrying after a lost response may return
  409 `budget_analytics_conflict`: recover by reloading limits and comparing the saved state.
  Archiving a category leaves existing enabled limits active and evaluable; creating or
  re-enabling a limit requires an active owned category while disabling an existing limit on
  an archived category remains permitted. The 200-limit cap per owner counts all saved rows
  (including disabled) on create only; updates, disables and no-ops remain allowed at the cap.
  Changes are recorded only in `budget_analytics_audit`, outside shared home audit and SSE.
- **Downgrade warning (0017 → 0016):** take a SQLite backup before downgrading. Dropping
  `budget_category_limits` deletes saved limit rows while preserving historical
  `budget_analytics_audit` entries; a schema rollback cannot restore dropped settings.

## Persisted private mortgage reminder schedules (schema 0018, evaluation-only)

- **Evaluation-only scope:** migration 0018 persists owner-configured mortgage schedules and
  explicit paid-month marks and evaluates reminder statuses on read. It introduces **no**
  background notification delivery daemon, **no** outbound email/push delivery, **no** shared
  calendar writes and **no** bank payment initiation.
- **Tables (outside `_source_digest`):**
  - `budget_mortgage_schedules`: owner-private schedules (`UNIQUE(id, owner)`, restrictive
    `owner -> users.username` FK, DB-level CHECK constraints on lengths, uppercase 3-letter
    currency, `due_day` 1..31, ISO `start_date`/`end_date` with `end_date >= start_date`,
    `remind_days_before` 0..31, `destination_kind IN ('owner_inbox', 'owner_push', 'owner_email')`,
    `enabled IN (0, 1)` and `version >= 1`). Stores **only `destination_kind`** (no email
    addresses, device tokens or URLs/PII); destination resolution from the owner's profile
    belongs to a separate future delivery slice. Cap: 100 schedules per owner (including
    disabled), checked on create only.
  - `budget_mortgage_payments`: explicit `YYYY-MM` paid-month marks with composite restrictive
    FK `(schedule_id, owner) -> budget_mortgage_schedules.(id, owner)`, restrictive
    `owner -> users.username` FK, no `CASCADE` in SQLite or ORM, and DB-level `GLOB` + month
    `01..12` CHECK. Disabling a schedule never deletes payment rows. Cap: 1,200 payment marks
    per schedule on mark only; unmarking always remains permitted even at the cap.
- **Installment boundaries and overdue visibility:** an installment qualifies iff its actual
  clamped calendar due date (month-end 29/30/31 clamped to the real last day of that month,
  including leap years, never shifted onto `start_date`/`end_date`) satisfies
  `start_date <= due_date <= end_date`. A reminder lead window (`remind_days_before`) may
  become active before `start_date`. Reaching `end_date` stops new installments from being
  generated, while unpaid overdue installments remain visible until marked paid. Marking a
  month paid requires a qualifying installment; existing payment marks are preserved if
  schedule dates are later edited and may still be unmarked.
- **Routes (OpenAPI `0.20.0`, gated by `HESTIA_BUDGET_ANALYTICS_ENABLED=1`, exact admin owner,
  `Cache-Control: no-store`, zero bank requests):**
  - `GET /api/budget/analytics/mortgages`: takes no query parameters; reads the server UTC
    clock once (`evaluation_date`, `evaluation_timezone: "UTC"`), reads schedules, payments
    and `ledger_as_of` in one SQLite snapshot, and returns an opaque owner-scoped
    `schedules_version` digest (which changes on schedule or payment edits).
  - `POST /api/budget/analytics/mortgage-save`: atomic `BEGIN IMMEDIATE` create
    (`schedule_id=null`, `expected_version=0`) or update (`expected_version>=1`), strict CAS
    before no-op, canonical exact positive decimal `amount`.
  - `POST /api/budget/analytics/mortgage-payment-set`: atomic `BEGIN IMMEDIATE` insert/delete
    of one `(schedule_id, month)` mark with strict CAS on `schedule.version` (single version
    bump on change, no-op leaves version and audit unchanged).
- **Sanitized audit:** `budget_analytics_audit` logs only a strict metadata allowlist
  (`schedule_id`, versions, `enabled`, `destination_kind`, `month`, `paid`) — never `label`,
  `amount`, destination targets or raw request bodies, and never writes to shared home audit
  or SSE.
- **Backup and downgrade (`0018 → 0017`):** back up SQLite (including schedules and payment
  marks) before downgrading. Downgrade drops `budget_mortgage_payments` before
  `budget_mortgage_schedules`, preserving sanitized `budget_analytics_audit` rows.

## Read-only bank consent renewal reminders (OpenAPI `0.21.0`, zero migration)

- `GET /api/budget/consent/renewal-reminders` (gated by `HESTIA_BUDGET_CONSENT_ENABLED=1`,
  exact SQLite admin owner with signed browser session cookie, `allow_head=False`,
  `Cache-Control: no-store`, zero query parameters, zero bank requests) evaluates the
  owner's enabled `budget_connections` on demand via `renewal_reminders.evaluate_renewal_reminders`.
- Uses only each connection's actual stored `valid_until` UTC epoch timestamp, `paused` flag
  and explicit `attention` code — never assuming a fixed 180-day lifetime — with lead tiers
  `(14, 7, 3, 1)` days.
- Strictly **evaluation-only**: returns statuses, milestones, `valid_until`, remaining time
  and `destination_kind` only (fixed to `"owner_inbox"` until a future delivery-configuration
  slice is introduced; never session IDs, URLs, codes or PII destination targets), triggers no
  notification delivery and never initiates consent renewal or modifies credentials. Stored DB
  integrity violations fail closed with HTTP 500 (`budget_consent_data_error`) rather than 400.

## Read-only bank consent reconciliation (OpenAPI `0.22.0`, zero migration)

- `GET /api/budget/consent/reconcile` (gated by `HESTIA_BUDGET_CONSENT_ENABLED=1`, exact
  SQLite admin owner with signed browser session cookie, `allow_head=False`,
  `Cache-Control: no-store`, zero query parameters, zero bank requests) classifies the owner's
  stored `budget_consents` and `budget_consent_flows` via `consent_reconcile.classify_consents`
  with defense-in-depth `active_consent_ids` pointers from `budget_connections`.
- Strictly **read-only / informational**: introduces no close, retire or delete endpoints;
  `remote_close_eligible` and `local_retire_eligible` are advisory flags only. External and
  retired PoC sessions (`external_file`, `poc_retired`) always classify as `external_read_only`
  with both eligibility flags `false`.
- **Opaque identifiers (keyed HMAC-SHA256):** `consent_id`, `flow_id` and `candidate_consent_id`
  are returned exclusively as domain-separated HMAC-SHA256 digests keyed with a key derived from
  the server session secret (never raw Enable Banking session IDs, DB UUIDs, capsule refs, URLs,
  codes, `aspsp_id` or `psu_type`). These digests are **not stable across restarts or secret
  rotations**; the UI uses them solely as ephemeral React keys and never displays or persists them.
  Stored DB integrity or missing-secret errors fail closed with HTTP 500
  (`budget_consent_data_error`) and exceeding evaluation caps returns 413 (`budget_consent_limit`).

## Owner-private live active alerts inbox (schema 0019, Variant D)

- **Live active alerts semantics (`GET /api/budget/analytics/inbox`):**
  - Strictly **pure read-only** (zero SQLite writes, zero bank I/O, zero query parameters,
    gated by `HESTIA_BUDGET_ANALYTICS_ENABLED=1`, exact SQLite admin owner, `allow_head=False`,
    `Cache-Control: no-store`). Consistent with all other `/api/budget/analytics/*` reads,
    authenticated owner bearer tokens and browser sessions are both accepted on `GET /inbox`,
    while `POST /inbox-mark` strictly requires a signed browser session cookie and HTTPS Origin.
  - Represents **currently active alerts only**: once an underlying condition resolves (for
    example a mortgage installment is marked paid, a consent is renewed or attention clears,
    or spending drops below the warning threshold), that alert disappears automatically from
    `GET /inbox` even if it was still unread. Active mortgage reminders are surfaced regardless
    of configured `destination_kind` (`owner_inbox`, `owner_push`, `owner_email`) until a
    separate outbound delivery slice is introduced.
  - Evaluates all three live sources (`consent_renewal`, `mortgage_due`, `spending_limit` for
    the current server UTC month) and persisted read markers inside **one single SQLite read
    snapshot** against one server UTC clock reading (`evaluation_date`, `evaluation_timezone: "UTC"`,
    `evaluated_at`), reusing the memoised spending projection when enabled spending limits exist.
  - Items are sorted deterministically without `created_at` (unread before read, then kind,
    severity, `subject_id` and `milestone`). `items`, `unread_count`, `reads_count`, `reads_cap`,
    `reads_revision` and `reads_version` all reflect that single snapshot.
- **Tables (strictly outside `_source_digest`):**
  - `budget_owner_inbox_state`: per-owner monotonic `reads_revision` (`CHECK >= 0`) and
    `updated_at` (`CHECK >= 0`) with restrictive `owner -> users.username` FK (no `CASCADE`).
    Starts at `0` when absent; increments `+1` atomically only on actual mark/unmark changes.
  - `budget_owner_inbox_reads`: composite PK `(owner, kind, subject_id, milestone)`, restrictive
    `owner -> users.username` FK (no `CASCADE`), `read_at >= 0`, DB-level CHECK constraints on
    allowed `kind`, 64-lowercase-hex `subject_id` and `1..128` `milestone` length, plus an index
    on `owner`.
- **Durable dedupe receipts (no automatic pruning in v1) and cap 500:**
  - Read markers are **never pruned automatically in v1** (temporary inactivity does not prove
    permanent disappearance; deleting a marker could resurrect a returning alert as unread).
  - Cap: `MAX_INBOX_READS = 500` persisted read markers per owner, enforced on **create only**.
    Exceeding the cap fails explicitly with HTTP `413` (`budget_analytics_limit`) without partial
    writes; unmarking (`read=false`) and no-ops on existing markers always remain permitted even
    at the cap. `reads_count` and `reads_cap` expose capacity directly in responses.
- **Anti-ABA CAS and atomic batch marking (`POST /api/budget/analytics/inbox-mark`):**
  - Accepts `{items: [{kind, subject_id, milestone}, ...], read: bool, expected_reads_version: str}`
    (strict `1..100` items, order-preserving deduplication, HTTPS Origin + signed browser cookie).
  - `reads_version` hashes only `owner`, monotonic `reads_revision` and persisted read markers
    (never live active alert keys or ledger rows), so background refreshes do not break CAS while
    `mark -> unmark` always produces a distinct `reads_version` (preventing ABA).
  - Runs inside one atomic `BEGIN IMMEDIATE` transaction: verifies enabled admin owner, enforces
    strict CAS BEFORE no-op (`409 budget_analytics_conflict`), re-evaluates live activity for any
    unmarked keys when `read=true`, and rejects the **entire batch** (`409 budget_analytics_source_changed`)
    if any unmarked key is no longer currently active (already-marked keys are no-ops).
- **Privacy, live `ref`, milestones and sanitized audit:**
  - `subject_id` is a stable domain-separated local SHA-256 digest (`owner + kind + local_id`)
    and remains the durable read-marker key in `budget_owner_inbox_reads`.
  - Each live item also carries an additive `ref` object with the owner-known local ID
    (`consent_renewal` -> `{connection_id}`, `mortgage_due` -> `{schedule_id}`,
    `spending_limit` -> `{category_id, currency}`), computed live from existing owner endpoints
    so the UI can resolve display names locally without storing or logging `ref` in SQLite or
    audit (never provider/session IDs, bank names, labels, amounts or PII).
  - `consent_renewal` milestones include both `consent_generation` and `valid_until` expiry tag;
    `mortgage_due` and `spending_limit` milestones key once per `(YYYY-MM, status)`.
  - Live `params` are strictly allowlisted per kind (zero bank names, labels, amounts, account
    IDs, error prose, destination targets or PII).
  - Changes write sanitized metadata only to `budget_analytics_audit` (`owner_inbox_read`, opaque
    SHA-256 `key_token`, `kind`, `reads_revision`, `read`), outside shared home audit and SSE.
    No outbound email/push delivery or shared calendar channel is introduced.
- **Backup and downgrade (`0019 → 0018`):** back up SQLite before downgrading. Downgrade drops
  `budget_owner_inbox_reads` and `budget_owner_inbox_state` while preserving sanitized
  `budget_analytics_audit` rows.

## Deployed Budget 2D UI tabs (OpenAPI `0.24.0`)

When `connections.capabilities.analytics` is `true`, the owner's **Budget** screen exposes
eight on-demand tabs in memory only (zero financial persistence in `localStorage` or URL
query strings; any `401`/`403`/`404` access loss clears all budget state immediately):

- **Accounts (`Konta`):** stored connections, account selection/kind, refresh jobs,
  on-demand read-only **Download limits**, and (when `capabilities.consent` is `true`)
  the on-demand read-only **Consent status (`Stan zgód`)** panel.
- **Inbox (`Skrzynka`):** live active alerts across `consent_renewal`, `mortgage_due` and
  `spending_limit`, resolving display names locally via `item.ref`, with unread/capacity
  indicators (`reads_count / reads_cap`), All / Unread filter, single & batch (up to 100)
  anti-ABA CAS read/unread controls, and deep-links to Consent status, Mortgages and
  Spending limits.
- **Month (`Miesiąc`)** and **Year (`Rok`):** exact server-side per-currency totals,
  category breakdowns, transaction filters/manual annotations, and own-transfer review queue.
- **Spending limits (`Limity wydatków`):** monthly category spending limits with exact
  decimal progress bars, strict CAS editing, and direct link to Categories and rules.
- **Recurring (`Cykliczne`):** read-only evidence-backed monthly recurring-charge hints.
- **Mortgages (`Hipoteka`):** evaluation-only private mortgage schedules and `YYYY-MM`
  payment marks evaluated against server UTC today (`evaluation_date`), with zero PII targets.
- **Categories and rules (`Kategorie i reguły`):** owner categories and mandatory
  preview-before-save deterministic classification rules.

## Read-only quarantine inspection (OpenAPI `0.25.0`, zero migration)

- `GET /api/budget/analytics/quarantine` (optional `account_id`, gated by
  `HESTIA_BUDGET_ANALYTICS_ENABLED=1`, exact SQLite admin owner, `allow_head=False`,
  `Cache-Control: no-store`, zero DB writes, zero bank requests) explains why observations
  are excluded from canonical report totals without introducing any mutation or resolution
  actions.
- **Counts primary:** returns exact whole-scope and per-account counts across all owned
  accounts (selected and excluded alike) for `uncertain_count`, `unresolved_count`, and
  `import_unresolved_count`, matching `_owner_counts` when unfiltered. Passing an unknown
  or foreign `account_id` returns `404 budget_analytics_not_found`.
- **Safe non-prose detail allowlist:** returns at most `200` deterministically ordered
  detail entries (`items_cap: 200`, `truncated` flag), ordered newest effective `date`
  first (`NULLS LAST`), then `observed_at` descending, then bucket/account/reference/reason
  ascending (matched in SQL before per-bucket `200`-row capping so newer dates in a single
  snapshot or import batch are never dropped behind older-dated rows). Entries contain
  exclusively `bucket`, `account_id`, `selected`, `entry_reference`, effective ISO `date`,
  fixed bank `status`, fixed exclusion `reason` (`non_booked_status`, `missing_reference`,
  `missing_date`, `reference_conflict`, `non_canonical`, `api_preserved`, `stale_conflict`,
  `same_time_conflict`), `source`, and `observed_at` — never transaction descriptions
  (`remittance_information`), amounts, currencies, or counterparty names. Whole-scope
  bounded integrity probes fail closed with HTTP `500` (`budget_analytics_data_error`) on
  invalid `reason`, `source`, `observed_at`/`fetched_at`, or SQL-verifiable `payload_json`
  fields anywhere in scope (including beyond row `200`); duplicate-key JSON rejection
  applies to the extracted candidate items.
