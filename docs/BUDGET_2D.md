# Budget 2D: spending, categories and own-account transfers

Status: implemented opt-in local analytics, using migration 0015. The canonical
ledger, explicit account selection and consent implementation remain the source
of truth. Suggestions require owner review before changing totals.

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

Use canonical BOOK entries keyed by `(account_id, entry_reference)`. Unresolved,
pending and uncertain observations stay separately counted. No fuzzy identity
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
source correction and stale fingerprints; concurrent accepts and restart; full
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
as suggestions. A corrected accepted pair stays reserved for review but affects
no totals until reconfirmed; a pair crossing the current account selection is a
boundary case and does not hide the selected leg.

Rules use literal casefolded contains/equals on description, counterparty_name
or MCC, sorted by priority then stable ID. Preview is mandatory for creation,
editing, enabling and disabling. The 15-minute preview binds owner selection,
ledger/evidence and local classifications; changed input returns 409
`budget_analytics_stale_preview`, expiration returns `budget_analytics_preview_expired`.
Replaying a saved preview returns its saved rule without duplicate creation.
Manual annotations, including explicitly uncategorised entries, take precedence.
Stale manual annotations remain marked for review and use safe default
classification until reconfirmed. Valid accepted transfers outrank rules.

Lists use encrypted cursors bound to owner, filters and the complete projection.
A changed snapshot returns 409 `budget_analytics_stale_cursor`; restart paging.
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

Recurring subscription hints, spending limits and mortgage schedules are later
extensions. This slice does not infer a subscription is unwanted, schedule a
payment or publish financial details in shared calendars.
