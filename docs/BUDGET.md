# Budget: read-only Enable Banking foundation

Status: development foundation; **not enabled in the Hestia server, dashboard or
production deployment**. This first slice supplies an explicit CLI ingestion path
and SQLite persistence. It does not make payments, categorise expenses or offer
bank login in the browser. Existing public policies still describe the deployed
service; update them before enabling this connector there.

## Decisions

- Enable Banking AIS runs on the server. A phone app need not stay running.
- Only explicitly configured account `identification_hash` values are fetched.
  Authorising or linking accounts in Enable Banking does not select them in Hestia.
- Each connection belongs to one existing, enabled Hestia **admin**. Financial
  data is separate from shared calendars/device permissions. No HTTP endpoints are
  added in this stage, and CLI status prints counts and freshness, not amounts,
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

## Acquisition and durability

Each run validates the authorised session, bank and selected account mappings;
retrieves balances and **every** transaction page; validates and normalises all
records; then commits each successful account's complete data and each failed
account's error status in one SQLite transaction. A failure on one account never
publishes a partial set of that account's pages or prevents other accounts from
refreshing. Network I/O never holds a database write transaction. Hestia's
existing SQLAlchemy/Alembic engine supplies WAL, FULL durability and migrations.

The requested window covers 365 calendar dates through today, with boundaries
explicitly calculated in UTC (matching the provider API). Dates remain calendar
dates without invented times or conversions. Returned records
are filtered locally because banks may ignore the requested start date. A year
requested is not a promise that a bank supplies a complete year. Booking date is
used first, value date second; undated records are marked unresolved.

Repeated pages, malformed rows and partial/network/permission errors fail that
account's snapshot. Missing
selected accounts, changed bank identity and invalid/expired sessions fail the
connection before any account is imported. Previously committed balances,
transactions and unresolved observations for failed accounts remain intact.
The CLI returns a partial/failed state and a nonzero exit code; only a completely
successful connection advances its full-success timestamp. Absence in a later
response never deletes historical ledger entries. A newer BOOK entry cannot be
downgraded to PDNG. Canonical rows are upserted by account and entry reference;
unresolved observations represent the last complete snapshot with multiplicity.

A database-specific process lock prevents overlapping CLI runs. Successful imports
and failures persist timestamps and retry state. Restarting cannot lose committed
history. Failed transient calls are retried by a later explicit `once` invocation,
with a persisted 12-hour quota reservation made before HTTP; 429 waits at least
six hours. Authorization/data errors
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

All calls in this stage are unattended and omit **all** PSU headers. Do not forge
IP/User-Agent values or assume an interactive request has unlimited quota.
Future interactive refresh must derive real PSU context through an explicitly
trusted reverse-proxy configuration; arbitrary forwarded headers are untrusted.

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

Use a separate test DB and fake API fixtures during development. Live acquisition
uses the fixed HTTPS Enable Banking origin, validated RS256 JWTs, bounded
responses, a GET-only route allowlist and no HTTP redirects. Errors expose safe
codes only. There is no custom production API URL setting.

Migration 0006 adds four empty tables on the next startup of any upgraded Hestia
SQLite deployment, even when acquisition is disabled. Merging this foundation
therefore changes the schema at that next restart; it does not start bank access.

## Next slices (not implemented here)

1. Admin-only, owner-scoped account selection and consent renewal. Reserved
   callback: `https://hestia.klatt.ie/api/budget/enable-banking/callback`; **no route
   exists yet**. Use expiring single-use state bound to user, browser and bank;
   keep callback query secrets out of access logs, referrers and page content.
2. Reminder seven days before actual provider `valid_until`; an **Odnów** action
   that starts fresh SCA, matches accounts by primary hash, verifies the new
   session and only then closes the previous session. No fixed assumption that
   every consent is exactly 180 days. Status already reports renewal due.
3. Verify ING/BNP field shapes with owner-approved PoC sessions. Do not infer
   support for cards, pending entries or statement due dates from bank branding.
4. Year view and conservative categorisation/subscription hints with manual
   correction. The owner must explicitly choose each account's kind (current,
   savings, credit card, foreign currency); CACC or a product name is insufficient.
   Card repayments/internal transfers must not duplicate spending;
   recurring transactions do not prove unused subscriptions. Mortgage due dates
   and forecasts require explicit schedule inputs and freshness indicators.
5. Decide export, erasure and retention before broader use. Disabling a connection
   stops acquisition and retains history; provider revocation is not local erasure.
6. Full React UI, background scheduling, legal-document update, access-control UAT
   and staged deployment after review. No bank data is sent to external AI tools.
7. Independent transaction/balance freshness: retain a complete transaction fetch
   if a later balance call fails (including 429), while preserving and clearly
   marking the old balance. This needs explicit per-part status and timestamps;
   the present account snapshot remains atomic and keeps its prior committed
   data when either part fails. Do not retry immediately to compensate.

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
