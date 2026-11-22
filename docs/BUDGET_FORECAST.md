# Budget Forecast: Upcoming Commitments Estimate (Design Proposal)

> **Status:** Revised design proposal (awaiting second review approval; owner decisions Q1–Q6 decided, items (a)–(c) pending owner confirmation with recommended defaults assumed below; target: OpenAPI `0.26.0`, zero schema migration in v1).  
> **Baseline:** `master` at `146a8de` (`v1.8.0`, schema `0019`, OpenAPI `0.24.0` at `hestia/api_contract.py:74`; quarantine `0.25.0` is in review separately).

## 1. Summary & Principles

This proposal adds a read-only **Upcoming Commitments** view (`GET /api/budget/analytics/forecast`) to the opt-in Budget 2D analytics module (`HESTIA_BUDGET_ANALYTICS_ENABLED=1`, `hestia/budget/analytics_api.py:18`). It projects known scheduled mortgage instalments and active monthly recurring-charge hints over a bounded horizon of `1..12` calendar months (default `3`), strictly per currency and evaluated against the server's UTC clock.

Following `docs/BUDGET.md:21-40` and `docs/BUDGET_2D.md:14-30, 75-82`:
- **Estimate only (*szacunek*):** Output is an informational projection from stored commitments and historical patterns, never a promise, cashflow guarantee, or payment trigger.
- **Zero FX, exact decimals:** Every sum uses `analytics_core.sum_money` / `compare_money` / `canonical_money` (`hestia/budget/analytics_core.py:59-86`) as canonical decimal strings per ISO-4217 currency; never SQLite `REAL`, Python `float`, or JavaScript `Number`.
- **Zero schema migration in v1:** Computed purely on read from existing `0019` tables and pure evaluators.
- **Owner-private & zero bank I/O:** Gated by `HESTIA_BUDGET_HTTP_ENABLED=1` and `HESTIA_BUDGET_ANALYTICS_ENABLED=1`, authoritative SQLite admin owner only, `Cache-Control: no-store`, zero bank/config/credential reads, and zero amounts/labels in logs, audit, or Iris.

## 2. Decisions

### Owner Decisions (Decided)

| ID | Topic | Decided Rule (v1) | Spec Section |
| --- | --- | --- | --- |
| **Q1** | Lapsed recurring cutoff | A recurring hint lapses when `(evaluation_date - last_date).days > 45` (`LAPSED_AFTER_DAYS = 45`), provided its source account(s) are not stale (see `(b)`). | [§5.3](#53-recurring-charge-hints-hestiabudgetrecurringpy) |
| **Q2** | Passed hint inside `M0` | If `last_date` was before `M0` and the expected day in `M0` already passed, the hint stays in `M0` until it lapses. | [§5.3](#53-recurring-charge-hints-hestiabudgetrecurringpy) |
| **Q3** | Prior-month overdue mortgages | Overdue mortgage instalments from before `M0` (`due_date[:7] < M0`) are **not** added to `M0` totals and appear only as a schedule count. | [§5.2](#52-mortgage-schedules-budget_mortgage_schedules-budget_mortgage_payments) |
| **Q4** | Projection horizon | Default `months = 3`, maximum `12` (`1..12`). | [§6](#6-horizon-bounds-processing-order--caps) |
| **Q5** | Deduplication matching | Conservative 1:1 heuristic on same `currency`, exact canonical `amount`, and `±5` days from due date; explicit DB linking is deferred to v2. | [§5.4](#54-deduplication-mortgage-schedule-vs-recurring-hint) |
| **Q6** | Spending limits | `BudgetCategoryLimit` rows are completely excluded from v1 Forecast totals and items. | [§5.5](#55-spending-limits-hestiabudgetspending_limitspy) |

### Pending Owner Decisions

The normative rules in this specification assume the recommended defaults below; final owner confirmation is required before implementation merges:

- **(a) Unmarked `M0` mortgage instalments & overdue within `M0`:** An `M0` mortgage instalment is shown in full until manually marked paid, and an overdue instalment whose due date falls within `M0` (`M0-01 <= due_date < evaluation_date`) stays in `M0` totals/items labelled `"unmarked"` (*nieoznaczona*). **Recommended default: yes.**
- **(b) Stale-account recurring hints:** Recurring hints observed on accounts with stale ledger data do not lapse at `> 45` days; instead they stay visible and are flagged `"account not up to date"` (*konto nieaktualne*). **Recommended default: yes.**
- **(c) Reversibility / visibility of hidden duplicates:** Hidden duplicates suppressed by 1:1 mortgage deduplication are shown in the UI as a read-only `"merged with instalment"` (*połączono z ratą*) list in v1, with explicit link/unmerge controls deferred to v2. **Recommended default: yes.**

## 3. Review Resolution

| Review Finding | Summary of Fix | Resolving Section(s) |
| --- | --- | --- |
| **BLOCKER 1** (paid-off / ended loan resurrection) | Separate schedule↔hint identity matching (all `enabled` schedules, even with 0 unpaid horizon items or `end_date < M0`) from instalment qualification; define disabled schedules as excluded from both projection and dedup; add paid/mix/ended/disabled tests. | [§5.2](#52-mortgage-schedules-budget_mortgage_schedules-budget_mortgage_payments), [§5.4](#54-deduplication-mortgage-schedule-vs-recurring-hint), [§10](#10-testing-strategy) |
| **HIGH 2** (partially paid / unmarked `M0`) | Document v1 `"unmarked scheduled commitments"` (full instalment until manual mark; ledger debits never mark paid); keep `M0` past-due instalments in `M0` labelled `"unmarked"` (*nieoznaczona*) under pending `(a)`. | [§2(a)](#pending-owner-decisions), [§5.2](#52-mortgage-schedules-budget_mortgage_schedules-budget_mortgage_payments), [§8](#8-ui-sketch-estimate-disclaimer--legal-review) |
| **HIGH 3** (size bounds & truncation reconciliation) | Raise `MAX_FORECAST_ITEMS = 3_600` (`(100 + 200) × 12`); bound item/dedup `label` to `256` chars and serialized response JSON to `4 MiB` (`413`, zero partial totals); define exact cap/filter/dedup order; reconcile `MAX_RECURRING_HINTS` truncation vs `413`. | [§5.3](#53-recurring-charge-hints-hestiabudgetrecurringpy), [§6](#6-horizon-bounds-processing-order--caps), [§10](#10-testing-strategy) |
| **HIGH 4** (freshness masking & lapse confusion) | Compute per-account UTC freshness (`stale_account_ids`) so one fresh account cannot mask a stale one; define `freshness_stale` normatively (`None` -> stale, `> 7` days UTC); keep stale-account hints active with `account_stale: true`. | [§2(b)](#pending-owner-decisions), [§5.1](#51-utc-clock-transaction-scope--normative-freshness), [§5.3](#53-recurring-charge-hints-hestiabudgetrecurringpy), [§10](#10-testing-strategy) |
| **HIGH 5** (schedule vs instalment count) | Rename `overdue_mortgage_count` to `overdue_mortgage_schedule_count`; define it on pre-`M0` earliest unpaid schedule reminders; test multi-overdue months in one schedule. | [§5.2](#52-mortgage-schedules-budget_mortgage_schedules-budget_mortgage_payments), [§7](#7-api-contract-sketch-openapi-0260-strictly-additive), [§10](#10-testing-strategy) |
| **HIGH 6** (incomplete 1:1 algorithm & opacity) | Specify total-order candidate sort key, greedy unused-pair loop, `deduplications[]` response array, and tie/reversed-order tests. | [§2(c)](#pending-owner-decisions), [§5.4](#54-deduplication-mortgage-schedule-vs-recurring-hint), [§7](#7-api-contract-sketch-openapi-0260-strictly-additive), [§10](#10-testing-strategy) |
| **MEDIUM 7** (API sketch defects) | Provide complete 3-month summing JSON example; filter `summary` warnings and `deduplications[]` by `currency`; add `forecast.RULE_VERSION`; give total-order item tie-break; drop unused repeated per-month evidence fields. | [§6](#6-horizon-bounds-processing-order--caps), [§7](#7-api-contract-sketch-openapi-0260-strictly-additive) |
| **MEDIUM 8** (test plan gates) | Require 100% line + branch Python coverage and full UI CI gate (`typecheck`, `lint`, `test`, `build`), plus an explicit test that existing OpenAPI paths/schemas are unchanged apart from `info.version`. | [§10](#10-testing-strategy) |
| **MEDIUM 9** (delivery checklist) | Require `_ANALYTICS_MODELS` & `_analytics_paths` registration (`api_contract.py`), `npm run gen:types` (`ui/src/api/openapi.ts`), updates to `BUDGET.md` / `BUDGET_2D.md` / this doc's status, and review of `privacy-policy.html` & `terms-of-service.html`. | [§11](#11-effort-estimate--delivery-checklist) |

## 4. Goals & Non-Goals

| Goals (v1) | Explicit Non-Goals |
| --- | --- |
| Read-only monthly projection of known upcoming outflows over `1..12` months (default `3`). | Cross-currency totals or FX conversion. |
| Combine enabled mortgage schedules (`budget_mortgage_schedules`) and active monthly recurring hints (`recurring.py`). | Predicting variable/discretionary spending, income, or future account balances. |
| Conservative zero-migration 1:1 deduplication with transparent `deduplications[]` inspection. | ML scoring, statistical seasonality, or subscription cancellation judgments (`docs/BUDGET_2D.md:78`). |
| Per-account UTC stale visibility, explicit EN/PL estimate disclaimer, and fail-closed bounds. | Outbound notifications (email/push), shared calendar writes, or bank payment requests. |
| Zero DB migration (`0019`) and zero audit/SSE writes. | Treating `BudgetCategoryLimit` (`hestia/budget/spending_limits.py:1`) as committed outflow. |

## 5. Data Sources & Exact Projection Rules

### 5.1 UTC Clock, Transaction Scope & Normative Freshness
Evaluation uses one server UTC clock reading `evaluation_date` (`datetime.fromtimestamp(self._now(), timezone.utc).date().isoformat()`, matching `hestia/budget/analytics_views.py:852, 1006-1007`) inside one SQLite read transaction (`_read_lock` + `self._read(owner)`, `hestia/budget/views.py:80-84`). Let `M0 = evaluation_date[:7]` (`YYYY-MM`). The horizon covers `[M0, end_month]` inclusive (`horizon_months` consecutive calendar months in `1900-01..9999-12`).

Existing `_ledger_as_of` (`analytics_views.py:803-815`) takes `max` across all selected accounts, allowing one fresh account to mask a stale one (`mortgages` remains unchanged). Forecast instead computes **per-account UTC freshness** across every selected account `a` (`BudgetAccount.enabled == True` joined with `BudgetConnection.owner == owner`, `hestia/db.py:278-297`):
1. For each selected account `a`, let `comp_ts` be `BudgetComponent.last_success` (`operation == "transactions"`, `hestia/db.py:314-321`) and `tx_ts` be `max(BudgetTransaction.observed_at)` where `uncertain == False` (`hestia/db.py:441-451`). Let `account_ts(a) = max(ts for ts in (comp_ts, tx_ts) if ts is not None)`, or `None` when both are absent.
2. When `account_ts(a)` is present, `account_as_of(a) = min(datetime.fromtimestamp(account_ts(a), timezone.utc).date().isoformat(), evaluation_date)` (clamping clock skew to `evaluation_date`, matching `analytics_views.py:813-814`); otherwise `account_as_of(a) = None`.
3. **Normative staleness rule (UTC, 7-day boundary matching `DEFAULT_STALE_AFTER_DAYS = 7`, `mortgage_reminders.py:20, 196`):**
   - `account_stale(a) = True` iff `account_as_of(a) is None` OR `(date.fromisoformat(evaluation_date) - date.fromisoformat(account_as_of(a))).days > 7`. Age `<= 7` days is fresh (`False`); age `>= 8` days is stale (`True`); `None` (never synced and no non-uncertain transactions) is always stale (`True`).
   - `stale_account_ids`: sorted list of selected `account.id` values with `account_stale(a) == True`.
   - `ledger_as_of`: latest `account_as_of(a)` across selected accounts, or `None` if all selected accounts have `account_as_of(a) is None` (or `selected_accounts == 0`).
   - **Normative `freshness_stale`:** `True` iff `selected_accounts == 0` OR `len(stale_account_ids) > 0` (thus `ledger_as_of is None` always implies `freshness_stale == True`, and one fresh account never masks a stale selected account).

### 5.2 Mortgage Schedules (`budget_mortgage_schedules`, `budget_mortgage_payments`)
Reuses `hestia/db.py:517-585` and `hestia/budget/mortgage_reminders.py`:
1. **Eligibility & disabled schedules:** Load all `BudgetMortgageSchedule` rows owned by `owner`. Enforce `MAX_SCHEDULES = 100` and `MAX_PAID_MONTHS = 1_200` (`mortgage_reminders.py:21-22`). **Disabled schedules (`enabled == False`) are excluded from both forecast projection and deduplication** (consistent with `mortgage_reminders.py:220-221`; keeping a finished loan as a dedup anchor requires `enabled = True` with `end_date` and/or `paid_months`).
2. **Unmarked scheduled commitments:** `budget_mortgage_payments` stores only `(schedule_id, month)` markers with no amount or transaction link (`hestia/db.py:565-585`, `analytics_store.py:327-366`). In v1, unpaid instalments are **unmarked scheduled commitments**: each unpaid month projects the full `schedule.amount` until marked paid manually via `mortgage-payment-set`, and a ledger `DBIT` **never** marks a mortgage instalment as paid or reduces its amount.
3. **Per-month instantiation (`M` in `[M0 .. end_month]`):** Call `installment_due_date(schedule.due_day, schedule.start_date, schedule.end_date, M)` (`mortgage_reminders.py:137-153`), which clamps `due_day` to `calendar.monthrange(year, month)[1]` and returns `None` outside `[start_date, end_date]`. Omit `M` if `M in schedule.paid_months` (`analytics_store.py:340` permits future paid marks within `[start_date, end_date]`).
4. **Overdue inside `M0` vs. prior to `M0`:**
   - **Inside `M0` (`M0-01 <= due_date < evaluation_date`, unmarked):** Under `(a)` (`yes`), stays in `M0` items and totals with full `schedule.amount` and `past_due = true` (UI renders `"unmarked"` / *nieoznaczona*).
   - **Before `M0` (`due_date[:7] < M0`):** Under **Q3**, **never** rolled into `M0` totals. Because `evaluate_mortgage_reminders` returns only each enabled schedule's **earliest** unpaid qualifying instalment (`mortgage_reminders.py:162-172, 232-235, 253`), `summary.overdue_mortgage_schedule_count` counts **enabled schedules** (not instalments) whose earliest unpaid `due_date` satisfies `due_date is not None and due_date[:7] < M0`. (A schedule with 3 overdue prior months counts as `1`; a schedule overdue only inside `M0` counts as `0` here because that instalment is projected in `M0`.)
5. **Amount & confidence:** Canonical positive decimal `schedule.amount`; `source = "mortgage"`, `confidence = "scheduled"`.

### 5.3 Recurring-Charge Hints (`hestia/budget/recurring.py`)
Reuses the complete, untruncated `snapshot["_recurring"]` produced by `_recurring_hints(snapshot)` (`hestia/budget/analytics_views.py:327-350`) wrapping `detect_recurring_charges` (`hestia/budget/recurring.py:257-278`):
1. **Input scope & grouping:** Selected accounts (`BudgetAccount.enabled`), eligible `BOOK` `DBIT` `expense` entries (`recurring.py:196`), excluding accepted own transfers (`analytics_views.py:474-475`) and `excluded` entries. Requires `>= 3` occurrences across `>= 3` distinct months with gaps in `[23, 38]` days (`recurring.py:27-30`). Grouping by `(currency, label_source, normalized_label, canonical_amount)` (`recurring.py:200, 238`) yields one exact `hint.amount` per `RecurringHint` (`RULE_VERSION = 1`).
2. **Lapse vs. stale-account rule (Q1 + Pending `(b)`):** For each hint `h`, let `account_stale = any(aid in stale_account_ids_set for aid in h.account_ids)` and `age_days = (date.fromisoformat(evaluation_date) - date.fromisoformat(h.last_date)).days`.
   - If `account_stale == False` and `age_days > 45` (`LAPSED_AFTER_DAYS = 45`): `h` is **lapsed** (excluded from dedup and projection; `summary.lapsed_recurring_count += 1`).
   - Otherwise `h` is **active**: if `account_stale == True`, `h` **does not lapse** even when `age_days > 45` (`summary.stale_recurring_count += 1`), remains eligible for dedup, and any projected items carry `account_stale: true`.
3. **Current & future-month projection:** Let `last_ym = hint.last_date[:7]` and `anchor_day = date.fromisoformat(hint.last_date).day`. For each `M` in `[M0 .. end_month]`, emit at most one item iff `M > last_ym` (preventing double-count in `last_ym`, while under **Q2** a passed hint with `last_ym < M0` stays in `M0` until it lapses), with `expected_date = date(year, month, min(anchor_day, calendar.monthrange(year, month)[1])).isoformat()`.
4. **Confidence tiers:** `"high"` when `hint.occurrence_count >= 6` and `(hint.max_gap_days - hint.min_gap_days) <= 7`; `"medium"` otherwise.
5. **Reconciliation with `MAX_RECURRING_HINTS`:** `GET /recurring` is a non-summing list view (`analytics_views.py:773-775`) that truncates at `MAX_RECURRING_HINTS = 200` and sets `truncated: true`. `GET /forecast` computes **monetary totals**, so silently truncating after 200 hints would emit incomplete totals (`docs/BUDGET_2D.md:173-174`). Therefore `forecast` reads the untruncated `snapshot["_recurring"]`, filters by `currency` (if given), partitions lapsed vs. active hints, and fails closed with `413 budget_analytics_limit` (zero partial totals) if active hints exceed `MAX_FORECAST_HINTS = 200`.

### 5.4 Deduplication: Mortgage Schedule vs. Recurring Hint
To prevent paid-off or ended loans from being resurrected by lingering bank history (**BLOCKER 1**), **identity matching is strictly separated from horizon instalment qualification**:
1. **Candidate sets:** Every **enabled** `MortgageSchedule` `s` (`s.enabled == True`, even if all months in `[M0 .. end_month]` are in `s.paid_months` or `s.end_date < evaluation_date`) and every **active** (non-lapsed) `RecurringHint` `h` (both already currency-filtered when `?currency` is set). Disabled schedules never participate.
2. **Pair eligibility:** `(s, h)` forms a candidate edge iff:
   - `h.currency == s.currency`, AND
   - `analytics_core.compare_money(h.amount, s.amount) == 0`, AND
   - Checking candidate months `M_due` in `[shift_month(h.last_date[:7], -1), h.last_date[:7], shift_month(h.last_date[:7], +1)]` (within `1900-01..9999-12`), at least one `due_iso = installment_due_date(s.due_day, s.start_date, s.end_date, M_due)` is not `None` and satisfies `distance = abs((date.fromisoformat(h.last_date) - date.fromisoformat(due_iso)).days) <= 5`. Let `(distance, due_iso)` be the minimum `(distance, due_iso)` across those months (unique because consecutive monthly instalments are `>= 28` days apart).
3. **Total-order sort key & greedy 1:1 selection (HIGH 6):** Sort all candidate edges ascending by:
   `(distance, due_iso, h.currency, h.normalized_label, h.label_source, h.first_date, h.last_date, s.schedule_id)`
   (Note: `detect_recurring_charges` guarantees `(h.currency, h.normalized_label, h.label_source, h.amount, h.first_date, h.last_date)` is unique per hint, and `s.schedule_id` is unique per schedule, so the sort key is a strict total order invariant to input order.)
   Iterate in sorted order with `used_schedules = set()`, `used_hints = set()`:
   - Skip if `s.schedule_id in used_schedules` or `hint_key(h) in used_hints`.
   - Otherwise mark both used and append a `BudgetAnalyticsForecastDeduplication` entry to `deduplications`.
4. **Horizon governance:** Any matched hint `h` is suppressed for the **entire** horizon `[M0 .. end_month]` even when its matched schedule `s` emits `0` unpaid instalments in `[M0 .. end_month]`. Final `deduplications` is sorted by `(currency, matched_due_date, schedule_id)` and `summary.deduped_recurring_count = len(deduplications)`.

### 5.5 Spending Limits (`hestia/budget/spending_limits.py`)
Per **Q6**, `BudgetCategoryLimit` rows (`hestia/db.py:492-514`, `"organisational aids only"`, `spending_limits.py:1`) are completely excluded from v1 Forecast totals and items.

## 6. Horizon, Bounds, Processing Order & Caps

- **Horizon:** `months` query parameter, default `3`, allowed `1..12` (UTC calendar months from `M0`).
- **Normative processing order (cap → filter → dedup → project → serialize):**
  1. Validate query params (`months`, `currency`); fail invalid with `400 budget_analytics_invalid_request`.
  2. Read `_projection(session, owner)` (enforces `MAX_ROWS = 100_000`, `analytics_views.py:37, 224-226` -> `413`) and all owner schedules/paid marks (enforces `MAX_SCHEDULES = 100`, `MAX_PAID_MONTHS = 1_200`, `mortgage_reminders.py:21-22` -> `413`).
  3. Compute per-account UTC freshness (`stale_account_ids`, `ledger_as_of`, `freshness_stale`) and compute/reuse untruncated `snapshot["_recurring"]`.
  4. Apply `currency` filter (if provided) to enabled `schedules` and detected `hints`.
  5. Partition filtered `hints` into lapsed vs. active (`§5.3`). Enforce `len(active_hints) <= MAX_FORECAST_HINTS` (`200`) -> else `413 budget_analytics_limit`.
  6. Run 1:1 deduplication (`§5.4`) on filtered enabled `schedules` and filtered `active_hints`.
  7. Instantiate all items in `[M0 .. end_month]` for filtered enabled `schedules` and surviving `active_hints`; compute `overdue_mortgage_schedule_count`. Enforce `len(projected_items) <= MAX_FORECAST_ITEMS` (`3_600` = `(100 + 200) × 12`) -> else `413 budget_analytics_limit`.
  8. Bound display labels (`MAX_FORECAST_LABEL = 256` code points: recurring `remittance_information` in `normalize.py:124-133` + `analytics_views.py:344` can reach `409_699` chars and `MAX_DESCRIPTION = 524_288` in `recurring.py:33, 157`; any forecast `label` / `recurring_label` longer than `256` code points is truncated as `value[:255].rstrip() + "…"` while `schedule.label` is already `<= 256` at `mortgage_reminders.py:25`), sort items, sum totals via `sum_money`, and verify serialized JSON byte length (`len(json.dumps(payload).encode("utf-8"))`) `<= MAX_FORECAST_RESPONSE_BYTES` (`4 * 1024 * 1024` bytes) -> else `413 budget_analytics_limit` with **zero partial totals**.

## 7. API Contract Sketch (OpenAPI `0.26.0`, Strictly Additive)

Register `"forecast"` in `READS` and `_QUERY_FIELDS` (`hestia/budget/analytics_api.py:19, 33`), `("GET", "/api/budget/analytics/forecast"): "admin"` in `_ROUTE_MIN_ROLE` (`hestia/web.py:175-183`) and `_PATH_ROLES` (`hestia/api_contract.py:2228-2237`), and models in `_ANALYTICS_MODELS` (`api_contract.py:2196-2208`) and `_analytics_paths()` (`api_contract.py:2856-2883`). Response schemas use `_READ` (`api_contract.py:113, 3033-3056`).

- **Endpoint:** `GET /api/budget/analytics/forecast` (`allow_head=False`)
- **Query parameters (strict, no duplicate keys, no `today` param):**
  - `months` (optional integer string `1..12`, regex `^[0-9]{1,2}$`, default `3`).
  - `currency` (optional `^[A-Z]{3}$`). When set, filters `totals`, `months[].currencies`, `months[].items`, `deduplications`, **and** currency-scoped `summary` counts (`overdue_mortgage_schedule_count`, `lapsed_recurring_count`, `deduped_recurring_count`, `stale_recurring_count`) so warnings from filtered-out currencies never leak (`MEDIUM 7`). `summary.selected_accounts`, `summary.excluded_accounts`, `stale_account_ids`, `ledger_as_of`, and `freshness_stale` remain owner-selected-account-wide.
- **New `0.26.0` Response Schemas (all fields below are new):**
  - `BudgetAnalyticsForecast`: `{evaluation_date, evaluation_timezone ("UTC"), horizon_months, start_month, end_month, ledger_as_of (str | null), freshness_stale (bool), stale_account_ids (list[str]), snapshot_digest, schedules_version, rule_version (forecast.RULE_VERSION = 1), summary, totals, deduplications, months}`.
  - `BudgetAnalyticsForecastSummary`: `{selected_accounts, excluded_accounts, overdue_mortgage_schedule_count, lapsed_recurring_count, deduped_recurring_count, stale_recurring_count}` (integers).
  - `BudgetAnalyticsForecastCurrencyTotal`: `{currency, total, scheduled_total, estimated_total}` (exact decimal strings, sorted by `currency`).
  - `BudgetAnalyticsForecastDeduplication`: `{schedule_id, schedule_label, recurring_label, amount, currency, matched_due_date, last_observed_date, day_distance}` (sorted by `(currency, matched_due_date, schedule_id)`).
  - `BudgetAnalyticsForecastMonth`: `{month, currencies, items}` (always `horizon_months` ordered entries; empty months carry `[]`).
  - `BudgetAnalyticsForecastItem`: `{source ("mortgage" | "recurring"), schedule_id (str | null), label (<=256 chars), amount, currency, expected_date, confidence ("scheduled" | "high" | "medium"), past_due (bool), account_stale (bool)}`. Repeated per-month evidence fields (`account_ids`, `label_source`, `occurrence_count`, `last_observed_date`) are intentionally omitted; the Recurring tab owns full evidence. Sorted by strict total order `(expected_date, currency, source, label, schedule_id or "", amount, tie_breaker)` where `tie_breaker` is `""` for `"mortgage"` and `(hint.normalized_label, hint.label_source, hint.first_date, hint.last_date)` for `"recurring"`.

```json
{
  "evaluation_date": "2026-11-22",
  "evaluation_timezone": "UTC",
  "horizon_months": 3,
  "start_month": "2026-11",
  "end_month": "2027-01",
  "ledger_as_of": "2026-11-21",
  "freshness_stale": false,
  "stale_account_ids": [],
  "snapshot_digest": "<64-hex>",
  "schedules_version": "<64-hex>",
  "rule_version": 1,
  "summary": {
    "selected_accounts": 2,
    "excluded_accounts": 0,
    "overdue_mortgage_schedule_count": 0,
    "lapsed_recurring_count": 1,
    "deduped_recurring_count": 1,
    "stale_recurring_count": 0
  },
  "totals": [
    {"currency": "PLN", "total": "7349.97", "scheduled_total": "7200", "estimated_total": "149.97"}
  ],
  "deduplications": [
    {
      "schedule_id": "mort-1",
      "schedule_label": "Synthetic home loan",
      "recurring_label": "SYNTHETIC BANK HIPOTEKA",
      "amount": "2400",
      "currency": "PLN",
      "matched_due_date": "2026-11-15",
      "last_observed_date": "2026-11-16",
      "day_distance": 1
    }
  ],
  "months": [
    {
      "month": "2026-11",
      "currencies": [{"currency": "PLN", "total": "2449.99", "scheduled_total": "2400", "estimated_total": "49.99"}],
      "items": [
        {"source": "mortgage", "schedule_id": "mort-1", "label": "Synthetic home loan", "amount": "2400", "currency": "PLN", "expected_date": "2026-11-15", "confidence": "scheduled", "past_due": true, "account_stale": false},
        {"source": "recurring", "schedule_id": null, "label": "Synthetic Stream", "amount": "49.99", "currency": "PLN", "expected_date": "2026-11-28", "confidence": "high", "past_due": false, "account_stale": false}
      ]
    },
    {
      "month": "2026-12",
      "currencies": [{"currency": "PLN", "total": "2449.99", "scheduled_total": "2400", "estimated_total": "49.99"}],
      "items": [
        {"source": "mortgage", "schedule_id": "mort-1", "label": "Synthetic home loan", "amount": "2400", "currency": "PLN", "expected_date": "2026-12-15", "confidence": "scheduled", "past_due": false, "account_stale": false},
        {"source": "recurring", "schedule_id": null, "label": "Synthetic Stream", "amount": "49.99", "currency": "PLN", "expected_date": "2026-12-28", "confidence": "high", "past_due": false, "account_stale": false}
      ]
    },
    {
      "month": "2027-01",
      "currencies": [{"currency": "PLN", "total": "2449.99", "scheduled_total": "2400", "estimated_total": "49.99"}],
      "items": [
        {"source": "mortgage", "schedule_id": "mort-1", "label": "Synthetic home loan", "amount": "2400", "currency": "PLN", "expected_date": "2027-01-15", "confidence": "scheduled", "past_due": false, "account_stale": false},
        {"source": "recurring", "schedule_id": null, "label": "Synthetic Stream", "amount": "49.99", "currency": "PLN", "expected_date": "2027-01-28", "confidence": "high", "past_due": false, "account_stale": false}
      ]
    }
  ]
}
```

- **Error Responses (`BudgetError`, `api_contract.py:1514-1517`, `PRIVATE_HEADERS`):**
  - `400 budget_analytics_invalid_request` (`months` out of range, bad `currency`, unknown/duplicate query keys, e.g. `today`).
  - `401 budget_not_signed_in`, `403 budget_forbidden`, `404 budget_disabled` / `budget_analytics_disabled`, `409 budget_requires_sqlite_users` (`api.py:53-63`, `analytics_api.py:100-103`).
  - `413 budget_analytics_limit` (any source, active-hint `> 200`, item `> 3_600`, or `> 4 MiB` response cap exceeded; zero partial totals).
  - `500 budget_analytics_data_error` / `budget_internal_error` (stored DB invariant failure, `analytics_views.py:844-845`), `503 budget_analytics_unavailable` / `budget_unavailable`.

## 8. UI Sketch, Estimate Disclaimer & Legal Review

- **Tab placement:** Add a 9th button `"forecast"` (`Forecast` / `Prognoza`) after `"mortgages"` in `ui/src/budget/budget.tsx:28, 74, 135, 449`, gated by `hasAnalytics(capabilities)`: `Accounts`, `Inbox`, `Month`, `Year`, `Spending limits`, `Recurring`, `Mortgages`, **`Forecast`**, `Categories and rules`. Implemented in `ui/src/budget/analytics/forecast.tsx` (`ForecastView`), wired via `budgetApi.forecast(months, currency)` in `ui/src/budget/api.ts` and generated types in `ui/src/budget/analytics/model.ts`; formats money strictly via `formatAmount` (`ui/src/budget/model.ts:68-79`).
- **Controls & badges:**
  - Estimate disclaimer right below the heading (matching `recurring.tsx:106` / `mortgages.tsx:496`).
  - Horizon switcher (`1`, `3`, `6`, `12`, default `3`) and currency filter pills (`All currencies` + observed currencies, matching `recurring.tsx:91-95`).
  - Metadata & warnings: `Evaluated for <date> (UTC)`, `Ledger data as of <date>` + stale-account names resolved via `accountName` (`analytics/model.ts:341-348`) when `freshness_stale` is true; banners for `overdue_mortgage_schedule_count > 0` (jump to `Mortgages`), `lapsed_recurring_count > 0`, and `stale_recurring_count > 0`.
  - Per-item status chips: `"Unmarked"` (*"Nieoznaczona"*) when `item.past_due` is true (`(a)`), and `"Account not up to date"` (*"Konto nieaktualne"*) when `item.account_stale` is true (`(b)`).
  - Read-only `"Merged with instalment"` (*"Połączono z ratą"*) section (`(c)`) listing `deduplications[]` (`recurring_label` → `schedule_label`, `amount`, `matched_due_date`, `last_observed_date`) when non-empty.
- **Mandatory estimate disclaimer wording:**
  - **EN:** `"Estimated upcoming commitments based on your mortgage schedules and monthly recurring expense patterns (at least three occurrences). This is an estimate only — not a guarantee, cashflow balance forecast, or prediction of variable spending."`
  - **PL:** `"Szacunek nadchodzących zobowiązań na podstawie Twoich harmonogramów hipotecznych i powtarzalnych wydatków miesięcznych (co najmniej trzy wystąpienia). To wyłącznie szacunek — nie gwarancja, prognoza salda ani przewidywanie wydatków zmiennych."`
- **Public legal pages:** Review `ui/public/privacy-policy.html:199-205, 297-299` and `ui/public/terms-of-service.html:113-117, 133-136` (`docs/BUDGET.md:13-14`) to ensure on-read upcoming-commitment estimates and heuristic duplicate suppression are accurately disclosed.

## 9. Privacy, Security & Performance

- **Auth & isolation:** Reuses `api._principal`, `BudgetHTTPStore._owner`, and strict `owner == ...` SQL filters (`views.py:70-85`). Both cookie and bearer auth allowed on `GET` (`docs/BUDGET.md:70-71`).
- **No leakage:** Pure module `hestia/budget/forecast.py` uses `@dataclass(frozen=True, repr=False)` (like `mortgage_reminders.py:52, 69`) and fixed-string errors (`ForecastInputError`, `ForecastDataError`, `ForecastLimitError`). `analytics_api.py:124` delegates unexpected errors to `api._failure` (`api.py:208`), logging only `operation` and `type(exc).__name__`. Zero amounts, labels, or descriptions enter logs, `budget_analytics_audit`, `/api/audit`, SSE, or Iris.
- **Browser hygiene:** All forecast state lives strictly in React component state, passes `onAccessLoss` (`budget.tsx:214-225`), and never enters `localStorage` or URL query strings (`docs/BUDGET.md:247-252`).
- **Performance & memoisation:** Executes inside `_read_lock` and `self._read(owner)` (`analytics_views.py:54, 765`). Reuses `self._projection(session, owner)` and `snapshot["_recurring"]` (`analytics_views.py:635-646, 767-770`) so switching between `Recurring` and `Forecast` avoids rescanning `budget_transactions`. Schedule queries (`<= 100` rows, `<= 1_200` paid marks each) and projection (`<= 100` schedules + `<= 200` active hints × `<= 12` months, capped at `3_600` items and `4 MiB` JSON) run in bounded memory.

## 10. Testing Strategy

Implementation PRs must pass **both** repository CI gates (`.github/workflows/ci.yml:15-67`, `.coveragerc:1-7`): **100% Python line + branch coverage** (`python -m coverage run -m unittest discover -s tests && python -m coverage report --fail-under=100`) and the **UI gate** (`cd ui && npm run typecheck && npm run lint && npm run test && npm run build`).

1. **Pure evaluator (`tests/test_budget_forecast.py`):**
   - **Calendar & decimals:** Leap/month-end clamping (`2026-02-28`, `2028-02-29`, `2100-02-28`, `30`-day months); mixed currencies with 77-digit decimals (`test_budget_mortgage_reminders.py:197-198`).
   - **BLOCKER 1 & HIGH 2 (schedules, paid marks, `M0`):** Whole horizon paid (future paid marks allowed, `analytics_store.py:340`), mixed paid/unpaid months, ended schedule (`end_date < M0` and settlement `±5d` past `end_date`), and **disabled schedule** (never projects and never suppresses a matching hint); unmarked `M0` stays in `M0` with full amount (`past_due=true` when `< evaluation_date`).
   - **HIGH 5 (`overdue_mortgage_schedule_count`):** 3 overdue months before `M0` in 1 schedule yields count `1`; overdue only inside `M0` yields count `0` while projecting in `M0`.
   - **HIGH 4 (freshness & lapse):** `ledger_as_of=None` -> `freshness_stale=True`; 7-day UTC boundary (`7d` fresh vs `8d` stale); one fresh account does not mask a stale selected account (`stale_account_ids`); `45d` active vs `46d` lapsed (fresh account) vs `46d` non-lapsing (`account_stale=True`) on stale account; `last_date[:7] == M0` vs `last_date[:7] < M0`.
   - **HIGH 6 (1:1 dedup & total order):** Tie-breaks (same distance, same label/amount), reversed input order and random permutations yield identical `deduplications[]` and `months[].items`; 2 equal hints keep second; 1 hint vs 2 equal schedules keeps single match.
   - **HIGH 3 (caps & bounds):** `100` schedules + `200` hints × `12` months = `3_600` items succeeds; `201` active hints -> `ForecastLimitError` (`413`); `> 3_600` items -> `413`; `410k`-char `remittance_information` hint label truncated to `256` chars; `> 4 MiB` serialized JSON -> `413` with zero partial totals.
   - **Safety:** `repr()` redaction and fixed error strings.
2. **Views, API & backward compatibility (`tests/test_budget_analytics_views.py`, `tests/test_budget_analytics_api.py`, `tests/test_api_contract.py`):**
   - Memo reuse (`_projection` + `snapshot["_recurring"]`), single read transaction, `?currency` filtering scoping `summary` counts and `deduplications[]` without leaking other currencies, RBAC (`401`/`403`/cross-owner), flags (`404`), `HEAD -> 405`, `PRIVATE_HEADERS`, zero `load_config`/`credentials`/`urllib`/`_audit` calls, strict query validation (`months=0/13`, `today=...`, `currency=pln`), `413` HTTP responses.
   - **MEDIUM 8 compatibility test:** Assert in `tests/test_api_contract.py` that every pre-existing OpenAPI path and component schema from `0.24.0` is byte-for-byte / structurally unchanged apart from `info.version` (`0.26.0`), alongside `test_checked_in_openapi_matches_models` (`test_api_contract.py:47-53`).
3. **UI (`ui/src/budget/analytics/forecast.test.tsx`, `ui/src/budget/budget.test.tsx`):**
   - Synthetic fixtures only; tab gating, EN/PL disclaimer, horizon/currency switches, `"Unmarked"` / `"Nieoznaczona"` M0 badge, `"Account not up to date"` / `"Konto nieaktualne"` stale flag, `"Merged with instalment"` / `"Połączono z ratą"` dedup list, overdue schedule jump to `Mortgages`, and immediate state wipe on access loss.

## 11. Effort Estimate & Delivery Checklist

- **Effort estimate:**
  - **Backend PR (`Short`, zero migration):** Pure `hestia/budget/forecast.py` (`RULE_VERSION = 1`), `BudgetAnalyticsViews.forecast`, `analytics_api.py` / `web.py` routes, OpenAPI contract (`0.26.0`), and unit/views/API/contract tests.
  - **UI PR (`Short`):** OpenAPI TS typegen, `ui/src/budget/api.ts`, `ui/src/budget/analytics/model.ts`, `ui/src/budget/analytics/forecast.tsx`, 9th tab in `ui/src/budget/budget.tsx` + `budget.css`, and Vitest specs.
- **Delivery checklist (MEDIUM 9):**
  1. Register new models in `_ANALYTICS_MODELS` (`hestia/api_contract.py:2196-2208`), route in `_analytics_paths` (`api_contract.py:2856-2883`) and `_PATH_ROLES` (`api_contract.py:2228-2237`), bump `CONTRACT_VERSION` to `"0.26.0"`, and regenerate `docs/api/openapi.json` (`python -m hestia.api_contract`).
  2. Run `cd ui && npm run gen:types` (`ui/package.json:16`) to regenerate `ui/src/api/openapi.ts` (`ui/src/api/types.ts:4`).
  3. Update `docs/BUDGET.md` (`docs/BUDGET.md:3-15, 238-240, 768-780`), `docs/BUDGET_2D.md` (`docs/BUDGET_2D.md:3-5, 400-421`), and update this document's `Status` banner to **Implemented** upon shipping.
  4. Review and update `ui/public/privacy-policy.html` and `ui/public/terms-of-service.html` to cover on-read upcoming-commitment estimates and heuristic duplicate hiding (`deduplications[]`) per `docs/BUDGET.md:13-14`.

## 12. v2 Option (Requires Schema Migration — Out of Scope for v1)

If future feedback shows exact-amount deduplication misses variable-rate mortgages or the owner wants manual overrides:
- **Explicit link/suppression table (migration `0020`):** Persist owner-managed links between `budget_mortgage_schedules` and recurring keys (plus reversible unmerge/snooze/dismiss flags supporting `(c)`) under CAS, replacing the `±5`-day exact-amount heuristic where explicitly linked.
- **Recurring amount bands (`recurring.py` `RULE_VERSION = 2`):** Group minor merchant price drift within an explicit band and project `last_amount` with an optional `[min, max]` range.
- **Discretionary envelope:** Optionally render enabled `BudgetCategoryLimit` ceilings in a separate non-summed section alongside committed totals.
