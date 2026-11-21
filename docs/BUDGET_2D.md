# Budget 2D: spending, categories and own-account transfers

Status: implementation contract for the next analytics slice. The current ledger,
account selection and consent implementation remain the source of truth. No
analytics endpoint or automatic transaction pairing is claimed by this document.

The operator confirmed all account kinds in the UI and chose 11 of 15 configured
accounts. Respect the four exclusions. The operator also explicitly authorised
matching by equal amount and dates within three days when account numbers are
unavailable. Extra financial-database encryption is not a prerequisite.

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
evidence. The current canonical normaliser does not retain structured creditor
or debtor account fields; a metadata sidecar and import/API enrichment must be
implemented before claiming account-number matching. Do not infer an IBAN from a
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
