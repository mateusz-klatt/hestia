# Budget 2A: account controls and independent acquisition

**DRAFT / PROPOSED / UNIMPLEMENTED.** Baseline: master `02f03bc`.
This document proposes backend work; it does not enable bank acquisition.
Existing behavior and import guarantees remain documented in [BUDGET.md](BUDGET.md).

## Scope and sequence

2A comprises three dependent pieces: explicit account controls, independent
component freshness, then durable request limits. Agree their schema/contracts
before parallel implementation; merge and verify each piece before its dependent.
Keep the operator CLI and fake transports as the initial execution boundary.
No live callbacks, bank-panel changes, automatic poller or production activation.
2B will add authenticated, owner-scoped financial endpoints and consent flows.
2C will add React account selection, confirmation and freshness displays after 2B.

## 2A.1 — explicit account selection and kind

Keep stable identity `(owner, identification_hash)` and the existing local account ID.
Session account UIDs remain temporary routes; currency, display name and kind are
not identity keys. Reauthorisation must not duplicate or silently reclassify accounts.
Proposed kinds: `unknown`, `current`, `savings`, `credit_card`, `other`.
Store explicit owner confirmation separately: confirmed kind, confirmation time
and confirmation origin. Existing accounts migrate to unconfirmed `unknown`.
Provider metadata or a prefill may suggest a kind, but never confirms it.
Secondary identification hashes are reconciliation hints, not automatic merge
keys: some are not unique. A changed primary hash or ambiguous alias requires
explicit reconciliation without reassigning existing history.
See [AccountResource](https://enablebanking.com/docs/api/reference/#accountresource)
for the distinction between the primary hash and non-unique secondary hashes.
Missing `balance_after_transaction`, a product name or an account-type code does
not prove a credit card. An imported file cannot supply trusted confirmation.
Account kind and currencies are independent: a credit card may be multicurrency,
and foreign currency does not make an account a separate kind or a new account.
Preserve currency and provider balance type on every balance; do not sum unlike
currencies/types, interpret a credit limit as cash, or infer statement repayment.
Selection remains explicit per account. Changing kind must not select an account;
selecting it must not count as confirming kind or granting access to another owner.
Separate the configured identity allowlist from the owner's persisted selection.
Both must permit acquisition. Configuration reconciliation and status commands
must not re-enable an account the owner deselected. Preserve existing enabled
choices during migration; newly introduced identities start disabled until an
explicit selection action. Version/document this change to legacy CLI selection
behavior before shipping it, and test it independently of kind confirmation.
An operator workflow must explicitly record owner confirmation rather than treating
existing configuration, an admin row or successful bank consent as that confirmation.
Proposed CLI actions: `accounts`, `account select <local-id>`,
`account deselect <local-id>` and `account confirm-kind <local-id> --kind <kind>`.
These privileged local actions act on behalf of the configured owner, not as an
authenticated browser principal. Append an audit record containing actor, owner,
local account ID, time, field, old/new enum or boolean and origin; omit amounts,
IBANs and bank descriptions.
2A adds no financial HTTP access. The 2B contract must reject access when auth is
off, require an enabled authenticated principal and enforce exact owner scope.
An admin role alone must not grant another household member's financial data.
Later HTTP access returns 401 for an invalid/missing session (including a disabled
user), 403 for a valid non-admin, and 404 for another owner's resource.

## 2A.2 — independent transactions and balances

Introduce a component key `(local_account_id, operation)`, where operation is
`transactions` or `balances`. Each has separate attempt, complete-success and
error state; connection session validity and identity checks remain shared.
Proposed component fields: `last_attempt_at`, `last_success_at`, `last_error`,
`consecutive_failures`, `next_attempt_at` and a bounded acquisition reservation.
Distinguish acquisition time from any bank-supplied source observation timestamp;
an absent bank timestamp stays absent. Transaction row `observed_at` and
`api_observed_at` retain their existing meanings, independent of component freshness.
Expose states `never`, `ready`, `deferred`, `running`, `ok`, `error`, `blocked`;
derive stale age from complete-success time and an explicit freshness policy.
Proposed configurable defaults: transactions stale after 36 hours and balances
after 24 hours. These describe acquisition freshness, not invented bank source
timestamps or a guarantee of current bank data.
State `ok` describes a completed request, not guaranteed bank history completeness.
Session expiry/revocation blocks new work without erasing previously stored data.
Split normalizer output into component successes/failures. Validate the session,
bank and selected account mapping before any component may publish its result.
Transactions publish only after every page validates, with the same fixed date
window across pages. A failed page leaves the previous ledger and API quarantine.
Balances publish only after their complete response validates. A balances error
must not discard independently complete transactions, and the reverse also holds.
Commit successful components and safe error state under selection/owner rechecks;
perform all HTTP outside SQLite write transactions. An invalid session/identity
or HTTP401 remains fatal to that acquisition before its staged data is published.
Aggregate connection status must show partial success rather than pretending both
components refreshed. Keep an explicit all-selected-components success timestamp.

## 2A.3 — durable limits and Retry-After

Depends on independent component state; it must not use a connection timestamp as
evidence that both endpoint budgets are available. Persist limits by stable local
account and operation, with applicable shared bank/session/application cooldowns.
An `ASPSP_RATE_LIMIT_EXCEEDED` response on an account endpoint defaults to that
account and operation, preserving healthy sibling accounts. Session-level 429
blocks the connection. Explicit application/provider-wide errors use that wider
scope; genuinely unclassified responses use a conservative connection floor.
Do not infer a global limit from one known account-level rate error.
Use verified bank policy/configuration; no universal provider quota is assumed.
Default background cadence is two acquisitions per day per selected endpoint,
subject to stricter verified bank quotas. This is an operational ceiling, not a
promise of bank allowance or spare manual requests.
Reserve an account/endpoint **acquisition** in a short transaction before its first
HTTP call. Correct continuation pages share that acquisition; do not charge a
daily refresh allowance again for each page. ING's refresh series limit must not
be confused with a generic requests-per-second limit. Separately gate individual
HTTP requests, including session metadata and pages, against applicable transport
pacing/cooldowns. Recheck selection, owner validity and deadlines before network
work. A restarted acquisition may consume another allowance; an uncertain request
after a crash conservatively retains its reservation across restart, configuration
changes and session renewal. Expiring a running lease must not erase consumed quota.
The HTTP client returns safe structured retry metadata without raw response bodies,
URLs or account identifiers. Parse `Retry-After` delay-seconds and HTTP-date using
an injected clock, and preserve recognized provider error codes through an
allowlist so account-level ASPSP errors can be distinguished from provider limits.
Parse retry metadata using
an injected clock; malformed/past values use a conservative fallback policy.
Compute eligibility from the latest applicable quota, backoff and valid server
deadline. Never retry earlier than a valid `Retry-After`; an extreme delay suspends
automatic acquisition for operator review instead of truncating the delay early.
For this proposal, a valid delay exceeding seven days or extending beyond session
validity suspends automatic acquisition. Keep the original deadline; suspension
does not make a retry eligible at the seven-day boundary.
Persist failure/backoff before returning. No immediate HTTP retries; manual refresh
uses the same reservation path. Background calls continue to omit all PSU headers.
Legitimate interactive PSU context is a separate 2B decision; never forge it to
claim an online allowance. Proposed transport timeout is 60 seconds per request
with a 15-minute whole-acquisition deadline, in addition to row/page/size bounds.
Reaching that deadline reports incomplete/deferred and retains previous data.
If pagination cannot finish within permitted acquisition/transport constraints, retain committed history and
report deferred/incomplete work; do not publish a partial transaction collection.

## Migration and compatibility

Add nullable confirmation metadata and component/request-limit state via Alembic.
Keep existing account IDs, references, enabled flags and owner relationships.
Backfill component success only from known complete API acquisition timestamps,
never offline `fetched_at`, import time or transaction observation times.
Carry the existing connection `next_attempt` reservation forward as a shared floor,
so upgrading cannot create a fresh allowance. New state alone must not enable work.
Preserve the transaction key, BOOK/quarantine rules and decimal/currency contracts.
Offline import remains atomic, idempotent, credential-free and network-free.
It preserves enabled flags, archives balances, and cannot refresh API component
success/quota/session state or change any API-authoritative payload/uncertainty.
Keep `source`, `observed_at`, `api_observed_at` and batch `imported_at` distinct.
`status --stored-only` remains read-only. Document additive status fields and the
versioned transition from legacy config-driven enabling to the separate allowlist
and persisted owner selection; no status command may defeat an explicit opt-out.

## Acceptance matrix and allocation

| Piece / owner | Required synthetic checks before merge |
| --- | --- |
| 2A.1 schema/store/config | Existing accounts stay unconfirmed; explicit confirmation survives restart; kind/currency changes preserve identity; selection and confirmation remain independent; opt-outs survive configure/status/once and new identities remain disabled. |
| 2A.1 access contract, verified in 2B | Auth off, disabled owner and different-owner requests fail closed; admin status does not bypass owner scope. No HTTP routes ship in 2A. |
| 2A.2 normalizer + store | Transactions succeed/balances429 and the reverse update only the successful component; later-page failure preserves the previous ledger/quarantine. |
| 2A.2 session + concurrency | HTTP401, expired/mismatched session, selection changes or disabled owner prevent staged publication; network work holds no write transaction. |
| 2A.3 client + runtime/store | Seconds/date Retry-After, invalid/past/extreme values, shared limits and manual refresh obey persisted deadlines; 14 continuation pages consume one refresh-series allowance and follow separate transport pacing. |
| 2A.3 restart + contention | Crash before/after HTTP and concurrent processes cannot duplicate allowance; restart, config changes and session renewal cannot reset reservations. |
| Migration + import regression | Existing data/flags/uncertainty survive upgrade; current cooldown is retained; offline repeat/older/conflicting imports preserve API authority and never advance API freshness. |

Store/schema owns durable state and atomic contracts; normalizer owns component
completeness; client/runtime owns safe retry metadata and pre-HTTP reservations.
Use independent review plus the repository's full coverage gate before considering
any deployment. Live consent, credentials and production tests require a separate,
explicit activation decision; this design is not that decision.
