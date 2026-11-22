# Release Notes Draft: v1.8.0 (Budget 2D & Consent Observability)

> **Status:** Draft only. Git tag `v1.8.0` and the GitHub Release will be created only
> after explicit owner approval. Covers PRs `#182`–`#217` (commit `1e699a1`, schema `0019`,
> OpenAPI `0.24.0`).

## Summary

`v1.8.0` delivers the complete opt-in **Budget 2D** local spending analytics suite,
provider acquisition hardening, read-only bank consent renewal/reconciliation
observability, and an owner-private live active alerts inbox. Every Budget 2D feature
remains gated behind default-off server flags, executes strictly against local SQLite
snapshots without extra bank API requests, and preserves exact decimal arithmetic per
currency.

## Highlights by Area (PRs `#182`–`#217`)

### Local Spending Analytics, Transfers & Performance (`#182`–`#187`, `#189`, `#194`)
- **#182, #183, #184:** Pure own-account transfer matcher, migration `0015`, opt-in
  `/api/budget/analytics/*` backend (OpenAPI `0.17.0`), and React screens for Month, Year,
  Categories, preview-before-save Rules, and Own-Transfer Review.
- **#185, #194:** Excluded entries no longer generate transfer suggestions; owners can
  explicitly pair a debit and compatible credit manually.
- **#186, #187, #189:** In-memory per-database/owner projection memoisation keyed by full
  source digest (bounded before reading), plus accessible form control naming in the UI.

### Acquisition & Consent Hardening (`#188`, `#190`–`#192`, `#201`, `#204`, `#207`)
- **#188, #201:** Migration `0016` persists allowlisted Enable Banking HTTP failure
  diagnostics (`last_http_status`, `last_provider_error`) within a bounded non-429 body
  read budget (never exposed over HTTP; SQLite operator diagnostics only).
- **#190, #191, #192:** Accepted Enable Banking's `tilisy` authorization host with fixed
  flow codes, fenced mid-round account selection changes as `deferred/control_changed`,
  and pinned authorization URL host edge cases.
- **#204, #207:** Per-connection `max_history_days` (`1..364`) for banks enforcing shorter
  history windows, single-`today` UTC boundary calculation, coverage-aware unresolved
  quarantine retention, and UTC midnight paging/boundary tests.

### Recurring-Charge Hints (`#193`, `#195`, `#198`, `#208`)
- **#193, #195, #198, #208:** Pure monthly recurring-charge hint detector (minimum 3
  occurrences, evidence-backed, no subscription judgment), read-only
  `GET /api/budget/analytics/recurring` (OpenAPI `0.18.0`), and read-only **Recurring**
  UI tab.

### Monthly Category Spending Limits (`#196`, `#199`, `#202`, `#205`, `#209`)
- **#196, #199, #202, #205, #209:** Pure scaled-integer spending-limit evaluator,
  migration `0017` (`budget_category_limits`, cap 200, restrictive FKs),
  `GET /api/budget/analytics/spending-limits` and `POST /api/budget/analytics/spending-limit-save`
  (OpenAPI `0.19.0`, strict CAS), FK/roundtrip/CAS race tests, and **Spending limits** UI tab.

### Private Mortgage Reminder Schedules (`#197`, `#199`, `#206`, `#210`)
- **#197, #199, #206, #210:** Pure mortgage reminder evaluator (month-end/leap-year
  clamping, overdue visibility, ledger freshness flag), migration `0018`
  (`budget_mortgage_schedules` + `budget_mortgage_payments`, evaluation-only, zero PII
  targets, sanitized audit), `GET /api/budget/analytics/mortgages`,
  `POST /api/budget/analytics/mortgage-save`, `POST /api/budget/analytics/mortgage-payment-set`
  (OpenAPI `0.20.0`), and **Mortgages** UI tab.

### Read-Only Consent Renewal & Reconciliation (`#200`, `#203`, `#211`–`#214`)
- **#200, #203, #211, #212, #213, #214:** Pure renewal reminder evaluator (uses actual
  provider `valid_until`, never assumes 180 days) and consent/flow reconciliation
  classifier (`external_read_only` for external/PoC sessions, defense-in-depth
  `active_consent_ids`). Exposed read-only `GET /api/budget/consent/renewal-reminders`
  (`0.21.0`) and `GET /api/budget/consent/reconcile` (`0.22.0`) with keyed domain-separated
  HMAC-SHA256 opaque IDs, fail-closed `500` on DB integrity errors, and the on-demand
  read-only **Consent status** UI panel with localized EN/PL attention messages.

### Owner-Private Live Active Alerts Inbox (`#215`–`#217`)
- **#215, #216, #217:** Migration `0019` (`budget_owner_inbox_state` +
  `budget_owner_inbox_reads`, strictly outside `_source_digest`), pure read-only
  single-snapshot `GET /api/budget/analytics/inbox` across consent renewal, mortgage due
  and spending limit alerts with live owner-known `ref` (`0.23.0`–`0.24.0`), atomic
  anti-ABA CAS `POST /api/budget/analytics/inbox-mark` (`1..100` items, cap 500 on create
  only, no pruning in v1, sanitized `key_token` audit), and the **Inbox (`Skrzynka`)** UI tab.

## Schema Migrations (`0015`–`0019`)

| Migration | Tables / Columns Added | In `_source_digest`? | Downgrade / Backup Note |
| --- | --- | --- | --- |
| `0015` | `budget_account_identity`, `budget_transaction_metadata`, `budget_categories`, `budget_annotations`, `budget_rules`, `budget_rule_previews`, `budget_transfer_decisions`, `budget_transfer_legs`, `budget_analytics_audit` | Yes (except previews/audit) | Back up SQLite before downgrade; drops all 2D classifications and decisions. |
| `0016` | `last_http_status`, `last_provider_error` on `budget_connections` and `budget_components` | Yes (connections) | Drops diagnostic columns only; preserves ledger and quotas. |
| `0017` | `budget_category_limits` | No | Back up SQLite before downgrade; drops saved spending limits, keeps `budget_analytics_audit`. |
| `0018` | `budget_mortgage_schedules`, `budget_mortgage_payments` | No | Back up SQLite before downgrade; drops payment marks then schedules, keeps `budget_analytics_audit`. |
| `0019` | `budget_owner_inbox_state`, `budget_owner_inbox_reads` | No | Back up SQLite before downgrade; drops inbox read markers and monotonic revision, keeps `budget_analytics_audit`. |

## OpenAPI Contract Progression (`0.17.0` → `0.24.0`, strictly additive)

- `0.17.0`: Core `/api/budget/analytics/` reads (`overview`, `transactions`, `categories`, `rules`, `transfers`) and writes (`category-create`, `category-update`, `annotation`, `rule-preview`, `rule-save`, `transfer-decision`).
- `0.18.0`: `GET /api/budget/analytics/recurring`.
- `0.19.0`: `GET /api/budget/analytics/spending-limits`, `POST /api/budget/analytics/spending-limit-save`.
- `0.20.0`: `GET /api/budget/analytics/mortgages`, `POST /api/budget/analytics/mortgage-save`, `POST /api/budget/analytics/mortgage-payment-set`.
- `0.21.0`: `GET /api/budget/consent/renewal-reminders`.
- `0.22.0`: `GET /api/budget/consent/reconcile`.
- `0.23.0`: `GET /api/budget/analytics/inbox`, `POST /api/budget/analytics/inbox-mark`.
- `0.24.0`: Additive live `ref` object on `GET /api/budget/analytics/inbox` items.

## Known Limitations & Explicit Non-Goals

- **No outbound notification delivery:** Mortgage `destination_kind` (`owner_inbox`, `owner_push`, `owner_email`) is stored for future delivery slices; no email/push daemon runs in `v1.8.0`, and all active mortgage reminders surface in the in-app Inbox regardless of `destination_kind`.
- **No shared-calendar sync & no bank payments:** Financial reminders never write to shared calendars and never initiate bank transfers.
- **No remote session closure or automated consent cleanup:** `GET /api/budget/consent/reconcile` is strictly read-only / advisory; external and retired PoC sessions are always `external_read_only`.
- **Live active inbox semantics & cap 500:** Resolved alerts disappear automatically from `GET /api/budget/analytics/inbox` even if unread; read markers are never pruned automatically in v1 (cap `500` persisted markers per owner on create; unmarking always succeeds to free slots).
- **No currency conversion (FX) and no effective-dated spending limits:** All totals and limits evaluate strictly per currency using exact decimal strings; editing a spending limit applies to historical month queries as well.
- **No uncertain-entry reconciliation UI, automated erasure, or HTTP limit-resume action:** Operator limit resume and offline JSON imports remain local CLI actions; SQLite financial data is not encrypted at rest by this module.

## Upgrade Checklist

1. Back up the SQLite database (`hestia.db` + WAL) consistently with the private consent capsule directory before stopping the container.
2. Run an offline shadow-migration test (`0014 -> 0019`) against a copy of production SQLite with `--network none` and verify zero foreign-key violations and zero Alembic/ORM drift.
3. Keep default-off flags explicit in deployment configuration (`HESTIA_BUDGET_ANALYTICS_ENABLED=1` for Budget 2D tabs; `HESTIA_BUDGET_CONSENT_ENABLED=1` for consent status/renewal; `HESTIA_BUDGET_POLLER_ENABLED` remains independently opt-in).
