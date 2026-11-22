# Budget Forecast: Upcoming Commitments Estimate (Design Proposal)

> **Status:** Proposed design only (target: OpenAPI `0.26.0`, zero schema migration in v1).  
> **Baseline:** `master` at `146a8de` (`v1.8.0`, schema `0019`, OpenAPI `0.24.0` at `hestia/api_contract.py:74`; quarantine `0.25.0` is in review separately).

## 1. Summary & Principles

This proposal adds a read-only **Upcoming Commitments** view (`GET /api/budget/analytics/forecast`) to the opt-in Budget 2D analytics module (`HESTIA_BUDGET_ANALYTICS_ENABLED=1`, `hestia/budget/analytics_api.py:18`). It projects known scheduled mortgage instalments and active monthly recurring-charge hints over a bounded horizon of upcoming calendar months, strictly per currency and evaluated against the server's UTC clock.

Following `docs/BUDGET.md:21-40` and `docs/BUDGET_2D.md:14-30, 75-82`:
- **Estimate only (*szacunek*):** Output is an informational projection from stored commitments and historical patterns, never a promise, cashflow guarantee, or payment trigger.
- **Zero FX, exact decimals:** Every sum uses `analytics_core.sum_money` / `compare_money` / `canonical_money` (`hestia/budget/analytics_core.py:59-86`) as canonical decimal strings per ISO-4217 currency; never SQLite `REAL`, Python `float`, or JavaScript `Number`.
- **Zero schema migration in v1:** Computed purely on read from existing `0019` tables and pure evaluators.
- **Owner-private & zero bank I/O:** Gated by `HESTIA_BUDGET_HTTP_ENABLED=1` and `HESTIA_BUDGET_ANALYTICS_ENABLED=1`, authoritative SQLite admin owner only, `Cache-Control: no-store`, zero bank/config/credential reads, and zero amounts/labels in logs, audit, or Iris.

## 2. Goals & Non-Goals

| Goals (v1) | Explicit Non-Goals |
| --- | --- |
| Read-only monthly projection of known upcoming outflows over `1..12` months (default `3`). | Cross-currency totals or FX conversion. |
| Combine enabled mortgage schedules (`budget_mortgage_schedules`) and active monthly recurring hints (`recurring.py`). | Predicting variable/discretionary spending, income, or future account balances. |
| Conservative zero-migration deduplication between mortgage schedules and recurring hints. | ML scoring, statistical seasonality models, or subscription cancellation judgments (`docs/BUDGET_2D.md:78`). |
| Explicit estimate disclaimer in EN and PL, stale-ledger visibility, and hard processing bounds. | Outbound notifications (email/push), shared calendar writes, promises, or bank payment requests. |
| Zero DB migration (`0019`) and zero audit/SSE writes. | Treating `BudgetCategoryLimit` (`hestia/budget/spending_limits.py:1`) as committed outflow. |

## 3. Data Sources & Exact Projection Rules

Evaluation uses one single server UTC clock reading `evaluation_date` (`datetime.fromtimestamp(self._now(), timezone.utc).date().isoformat()`, matching `hestia/budget/analytics_views.py:852, 1006-1007`) and one SQLite read transaction (`_read_lock` + `self._read(owner)`, `hestia/budget/views.py:80-84`). Let `M0 = evaluation_date[:7]` (`YYYY-MM`). The horizon covers `[M0, end_month]` inclusive (`horizon_months` consecutive calendar months, `1..12`).

### 3.1 Mortgage Schedules (`budget_mortgage_schedules`, `budget_mortgage_payments`)
Reuses `hestia/db.py:517-585` and `hestia/budget/mortgage_reminders.py`:
1. **Eligibility:** Load all `BudgetMortgageSchedule` rows owned by `owner`; skip rows with `enabled == False` (`mortgage_reminders.py:220-221`). Enforce `MAX_SCHEDULES = 100` and `MAX_PAID_MONTHS = 1_200` (`mortgage_reminders.py:21-22`).
2. **Per-month instantiation:** For each calendar month `M` in `[M0 .. end_month]`, call `installment_due_date(schedule.due_day, schedule.start_date, schedule.end_date, M)` (`mortgage_reminders.py:137-153`).
   - Clamps `due_day` (`1..31`) to `calendar.monthrange(year, month)[1]`, handling 28/29/30/31-day months and leap years (`2028-02-29` vs `2026-02-28`, `mortgage_reminders.py:133-134`).
   - Returns `None` if the clamped date falls outside `[start_date, end_date]` (never shifting a due date onto `start_date` or `end_date`).
3. **Paid marks:** If `M in schedule.paid_months`, omit that month's installment from forecasted remaining outflows. Paid marks outside `[start_date, end_date]` are safely ignored (`mortgage_reminders.py:237-238`).
4. **Prior-month overdue installments:** Unpaid installments with `due_date[:7] < M0` (surfaced as `"overdue"` by `evaluate_mortgage_reminders`, `mortgage_reminders.py:178-179, 234`) are **not** rolled into `M0` totals so monthly sums stay anchored to their calendar month; instead, return `summary.overdue_mortgage_count` so the UI links to the **Mortgages** tab.
5. **Amount & confidence:** Canonical positive decimal `schedule.amount`; `source = "mortgage"`, `confidence = "scheduled"`.

### 3.2 Recurring-Charge Hints (`hestia/budget/recurring.py`)
Reuses `_recurring_hints(snapshot)` (`hestia/budget/analytics_views.py:327-350`), which wraps `detect_recurring_charges` (`hestia/budget/recurring.py:257-278`):
1. **Input scope:** Uses the owner's selected accounts (`BudgetAccount.enabled`), eligible `BOOK` `DBIT` `expense` entries (`recurring.py:196`), excluding accepted own transfers (`analytics_views.py:474-475`) and entries classified as `excluded` (`effective_kind != "expense"`).
2. **Cadence & amount:** `recurring.py` (`RULE_VERSION = 1`, `recurring.py:27-30`) detects `cadence = "monthly"` only, requiring `>= 3` occurrences across `>= 3` distinct months with consecutive gaps in `[23, 38]` days (`MIN_GAP_DAYS..MAX_GAP_DAYS`). Because `recurring.py:200, 238` groups by `(currency, label_source, normalized_label, canonical_amount)`, every v1 `RecurringHint` already has one exact `hint.amount` (no last/median/range ambiguity in v1; if a future `RULE_VERSION = 2` allows variable amounts, v2 should project `last_amount` alongside `[min, max]` metadata rather than floating-point averages).
3. **Lapsed-hint rule:** `recurring.py` splits a series whenever consecutive gaps exceed `MAX_GAP_DAYS = 38` (`recurring.py:221`). A hint is considered **lapsed** in forecast when `(evaluation_date - date.fromisoformat(hint.last_date)).days > LAPSED_AFTER_DAYS`, where `LAPSED_AFTER_DAYS = 45` (`38` days + `7` days bank-posting/sync grace). Lapsed hints are excluded from projected items and totals, and counted in `summary.lapsed_recurring_count`.
4. **Current-month (`M0`) & future-month projection:** Let `last_ym = hint.last_date[:7]` and `anchor_day = date.fromisoformat(hint.last_date).day`.
   - For each month `M` in `[M0 .. end_month]`, emit **at most one** projected item iff `M > last_ym` (so a charge already booked in `M0` is never double-counted; if `last_ym < M0` and `(evaluation_date - last_date) <= 45` days, `M0` still carries the expected unposted installment).
   - `expected_date = date(year, month, min(anchor_day, calendar.monthrange(year, month)[1])).isoformat()` (same leap-year/month-end clamping as mortgages).
5. **Confidence tiers (deterministic, no ML):**
   - `"high"`: `hint.occurrence_count >= 6` and `(hint.max_gap_days - hint.min_gap_days) <= 7`.
   - `"medium"`: all other non-lapsed hints (`occurrence_count` `3..5`, or gap spread `> 7` within `[23, 38]` days).

### 3.3 Deduplication: Mortgage Schedule vs. Recurring Hint
Without an explicit DB link column in v1, a mortgage paid from a selected checking account and not marked `excluded` will also appear as a `RecurringHint` after 3 monthly debits.
- **v1 Rule (zero-migration conservative 1-to-1 heuristic):** Before projecting recurring hints, match active (non-lapsed) `RecurringHint` rows against enabled `MortgageSchedule` rows that have at least one qualifying unpaid installment in `[M0 .. end_month]`. A hint matches a schedule if:
  1. `hint.currency == schedule.currency`, AND
  2. `analytics_core.compare_money(hint.amount, schedule.amount) == 0` (exact canonical decimal), AND
  3. `hint.last_date` falls within `[schedule.start_date, schedule.end_date]` (`end_date` optional) AND `abs((date.fromisoformat(hint.last_date) - due).days) <= 5` for a valid `installment_due_date(...)` `due` in the hint's month or an adjacent month (`±1` month, handling end-of-month settlement crossing into the next month, using the same `5`-day tolerance as `DEFAULT_REMIND_DAYS = 5` in `mortgage_reminders.py:19`).
  - Match **at most one** active hint per enabled schedule, resolved deterministically by smallest day distance, then `(hint.normalized_label, hint.label_source, schedule.schedule_id)`. Matched hints are suppressed from forecast totals/items and counted in `summary.deduped_recurring_count`.
- **Trade-offs:**
  - *False negative (double count):* Occurs if the owner rounded the schedule amount (e.g., `2500` vs bank debit `2498.50`) or if bank settlement shifted `> 5` days from `due_day`. Mitigated by showing itemized `Mortgage` and `Recurring` rows side by side so duplicates are obvious.
  - *False positive (over-suppression):* An unrelated fixed expense of identical currency, exact amount, and `±5` day date would be suppressed. Mitigated by 1-to-1 matching (never suppressing two hints for one mortgage) and displaying `deduped_recurring_count` in the header summary.

### 3.4 Spending Limits (`hestia/budget/spending_limits.py`)
`BudgetCategoryLimit` rows (`hestia/db.py:492-514`) represent discretionary monthly ceilings ("organisational aids only", `spending_limits.py:1`), not committed outflows. They are intentionally **excluded** from v1 forecast totals to respect the non-goal of not predicting variable spending.

## 4. Horizon, Bounds & Caps

- **Horizon (`months`):** Default `3`, allowed `1..12` (server UTC months starting at `M0`).
- **Source & output bounds (fail closed with `413 budget_analytics_limit`, zero partial totals, matching `docs/BUDGET_2D.md:173-174`):**
  - Selected ledger rows: `MAX_ROWS = 100_000` (`analytics_views.py:37, 225-226`).
  - Mortgage schedules / paid marks: `MAX_SCHEDULES = 100`, `MAX_PAID_MONTHS = 1_200` (`mortgage_reminders.py:21-22`).
  - Active (non-lapsed) recurring hints entering forecast: `MAX_FORECAST_HINTS = 200` (matching `MAX_RECURRING_HINTS = 200`, `analytics_views.py:39`).
  - Total projected line items across all months in the response: `MAX_FORECAST_ITEMS = 2_400` (e.g., `200` sources × `12` months).

## 5. API Contract Sketch (OpenAPI `0.26.0`, Strictly Additive)

Register `"forecast"` in `READS` and `_QUERY_FIELDS` (`hestia/budget/analytics_api.py:19, 33`), `("GET", "/api/budget/analytics/forecast"): "admin"` in `_ROUTE_MIN_ROLE` (`hestia/web.py:175-183`) and `_PATH_ROLES` (`hestia/api_contract.py:2229-2237`). Response schemas use `_READ` (tolerant reader, `api_contract.py:3033-3056`).

- **Endpoint:** `GET /api/budget/analytics/forecast` (`allow_head=False`)
- **Query parameters (strict, no duplicate keys, no `today` param):**
  - `months` (optional integer string `1..12`, regex `^[0-9]{1,2}$`, default `3`).
  - `currency` (optional 3-letter uppercase ASCII `^[A-Z]{3}$`; filters `totals`, `months[].currencies`, and `months[].items`).
- **200 Response Key Sets (`BudgetAnalyticsForecast`):**
  - Top-level: `evaluation_date`, `evaluation_timezone` (`"UTC"`), `horizon_months`, `start_month`, `end_month`, `ledger_as_of`, `freshness_stale`, `snapshot_digest`, `schedules_version`, `rule_version` (`1`), `summary`, `totals`, `months`.
  - `summary` (`BudgetAnalyticsForecastSummary`): `{selected_accounts, excluded_accounts, overdue_mortgage_count, lapsed_recurring_count, deduped_recurring_count}` (integers only).
  - `totals[]` & `months[].currencies[]` (`BudgetAnalyticsForecastCurrencyTotal`): `{currency, total, scheduled_total, estimated_total}` (all exact canonical decimal strings, sorted by `currency`).
  - `months[]` (`BudgetAnalyticsForecastMonth`): `{month, currencies, items}` (always `horizon_months` ordered entries; zero-item months carry empty lists).
  - `months[].items[]` (`BudgetAnalyticsForecastItem`): `{source ("mortgage" | "recurring"), schedule_id (str | null), label, amount, currency, expected_date, confidence ("scheduled" | "high" | "medium"), label_source ("counterparty_name" | "description" | null), occurrence_count (int | null), last_observed_date (str | null), account_ids (list[str])}`, sorted by `(expected_date, currency, source, label, schedule_id or "")`.

```json
{
  "evaluation_date": "2026-11-22",
  "evaluation_timezone": "UTC",
  "horizon_months": 3,
  "start_month": "2026-11",
  "end_month": "2027-01",
  "ledger_as_of": "2026-11-21",
  "freshness_stale": false,
  "snapshot_digest": "<64-hex>",
  "schedules_version": "<64-hex>",
  "rule_version": 1,
  "summary": {
    "selected_accounts": 2,
    "excluded_accounts": 0,
    "overdue_mortgage_count": 0,
    "lapsed_recurring_count": 1,
    "deduped_recurring_count": 1
  },
  "totals": [
    {"currency": "PLN", "total": "7549.97", "scheduled_total": "7400", "estimated_total": "149.97"}
  ],
  "months": [
    {
      "month": "2026-11",
      "currencies": [
        {"currency": "PLN", "total": "2449.99", "scheduled_total": "2400", "estimated_total": "49.99"}
      ],
      "items": [
        {
          "source": "mortgage",
          "schedule_id": "mort-1",
          "label": "Synthetic home loan",
          "amount": "2400",
          "currency": "PLN",
          "expected_date": "2026-11-25",
          "confidence": "scheduled",
          "label_source": null,
          "occurrence_count": null,
          "last_observed_date": null,
          "account_ids": []
        },
        {
          "source": "recurring",
          "schedule_id": null,
          "label": "Synthetic Stream",
          "amount": "49.99",
          "currency": "PLN",
          "expected_date": "2026-11-28",
          "confidence": "high",
          "label_source": "counterparty_name",
          "occurrence_count": 8,
          "last_observed_date": "2026-10-28",
          "account_ids": ["acc-1"]
        }
      ]
    }
  ]
}
```
- **Error Responses (`BudgetError`, `api_contract.py:1514-1517`, `PRIVATE_HEADERS`):**
  - `400 budget_analytics_invalid_request` (bad/out-of-range `months`, bad `currency`, unknown/duplicate params, e.g. client `today`).
  - `401 budget_not_signed_in`, `403 budget_forbidden`, `404 budget_disabled` / `budget_analytics_disabled`, `409 budget_requires_sqlite_users` (`api.py:53-63`, `analytics_api.py:100-103`).
  - `413 budget_analytics_limit` (any source/item cap exceeded; no partial totals).
  - `500 budget_analytics_data_error` / `budget_internal_error` (stored invariant failure, `analytics_views.py:844-845`), `503 budget_analytics_unavailable` / `budget_unavailable`.

## 6. UI Sketch & Estimate Disclaimer

- **Tab placement:** Add a 9th button `"forecast"` to `Budget` in `ui/src/budget/budget.tsx:28, 74, 135, 449`, gated by `hasAnalytics(capabilities)`:
  - Order: `Accounts`, `Inbox`, `Month`, `Year`, `Spending limits`, `Recurring`, `Mortgages`, **`Forecast`**, `Categories and rules` (`Konta`, `Skrzynka`, `Miesiąc`, `Rok`, `Limity wydatków`, `Cykliczne`, `Hipoteka`, **`Prognoza`**, `Kategorie i reguły`).
  - Implemented in `ui/src/budget/analytics/forecast.tsx` (`ForecastView`), wired via `budgetApi.forecast(months, currency)` in `ui/src/budget/api.ts` and types in `ui/src/budget/analytics/model.ts`. Uses `formatAmount(amount, currency, locale)` (`ui/src/budget/model.ts:68-79`) only; never computes sums in JS.
- **Controls & layout:**
  - Estimate disclaimer banner right below the view heading (styled consistently with `recurring.tsx:106` / `mortgages.tsx:496`).
  - Horizon switcher (`1`, `3`, `6`, `12` months, default `3`) and currency filter pills (`All currencies` + observed currencies), matching `budget-views`.
  - Metadata row (`Evaluated for <date> (UTC)`, `Ledger data as of <date>` + stale warning if `freshness_stale`) and informational notes when `overdue_mortgage_count > 0` (button jumping to `Mortgages`), `deduped_recurring_count > 0`, or `lapsed_recurring_count > 0`.
  - Horizon summary totals per currency (`Total` = `Scheduled` + `Estimated`), followed by chronological month sections with itemized obligations (`expected_date`, `label`, `source` + `confidence` badges, `amount`).
- **Mandatory estimate disclaimer wording:**
  - **EN:** `"Estimated upcoming commitments based on your mortgage schedules and monthly recurring expense patterns (at least three occurrences). This is an estimate only — not a guarantee, cashflow balance forecast, or prediction of variable spending."`
  - **PL:** `"Szacunek nadchodzących zobowiązań na podstawie Twoich harmonogramów hipotecznych i powtarzalnych wydatków miesięcznych (co najmniej trzy wystąpienia). To wyłącznie szacunek — nie gwarancja, prognoza salda ani przewidywanie wydatków zmiennych."`

## 7. Privacy, Security & Performance

- **Auth & isolation:** Reuses `api._principal`, `BudgetHTTPStore._owner`, and strict `owner == ...` SQL filters (`views.py:70-85`). Both cookie and bearer auth allowed on `GET` (`docs/BUDGET.md:70-71`).
- **No leakage:** Pure module `hestia/budget/forecast.py` uses `@dataclass(frozen=True, repr=False)` (like `mortgage_reminders.py:52, 69`) and fixed-string errors (`ForecastInputError`, `ForecastLimitError`). `analytics_api.py:124` delegates unexpected errors to `api._failure` (`api.py:208`), logging only `operation` and `type(exc).__name__`. No amounts, labels, or descriptions enter logs, `budget_analytics_audit`, `/api/audit`, SSE, or Iris.
- **Browser hygiene:** All forecast state stays strictly in React component state, passes `onAccessLoss` (`budget.tsx:214-225`), and never enters `localStorage` or URL query parameters (`docs/BUDGET.md:247-252`).
- **Performance & memoisation:** Executes inside `_read_lock` and `self._read(owner)` (`analytics_views.py:54, 765`). Reuses `self._projection(session, owner)` and its memoised `snapshot["_recurring"]` (`analytics_views.py:635-646, 767-770`) so repeated reads or switching between `Recurring` and `Forecast` avoid rescanning `budget_transactions`. Schedule queries (`<= 100` rows, `<= 1_200` payment marks each) and horizon projection (`<= 100` schedules + `<= 200` active hints across `<= 12` months, hard-capped at `2_400` projected items) run as bounded `O((S + H) * M)` in-memory work on top of the memoised projection.

## 8. Testing Strategy

1. **Pure evaluator (`tests/test_budget_forecast.py`):**
   - **Leap years & month-end clamping:** `due_day` `29`/`30`/`31` and recurring `last_date` day `29`/`30`/`31` across `2026-02` (non-leap), `2028-02` (leap), `2100-02`, and `30`-day months.
   - **Mortgage `end_date` & `paid_months`:** Mid-month `end_date < due_date` stops generation; future `start_date`; paid months excluded from monthly sums; prior-month unpaid installments counted in `overdue_mortgage_count` only.
   - **Currency mixing:** Multi-currency (`PLN`, `EUR`, `USD`) mortgages and recurring hints summed strictly per currency with extreme 70+ digit decimals (`test_budget_mortgage_reminders.py:197-198`).
   - **Lapsed & same-month hints:** `last_date` age `45` days (active) vs `46` days (lapsed, omitted, counted); `last_date[:7] == M0` omitted in `M0` (no double-count) and projected from `M0 + 1`.
   - **Deduplication:** Exact `(currency, amount, in-window, ±5d of due_day)` 1-to-1 suppression; two equal-amount hints keep the second; rounded mortgage amount does not suppress; disabled schedule does not suppress.
   - **Safety:** Permutation invariance, `repr()` redaction of amounts/labels/IDs, fixed error messages, and `413` caps.
2. **Views, cache & API (`tests/test_budget_analytics_views.py`, `tests/test_budget_analytics_api.py`, `tests/test_api_contract.py`):**
   - Memo reuse (`_projection` + `snapshot["_recurring"]`), single read transaction, stale ledger (`freshness_stale`), excluded accounts/transfers ignored, and `413` limits.
   - RBAC (`401`/`403`/cross-owner), flags (`404`), `HEAD -> 405`, `PRIVATE_HEADERS`, zero `load_config`/`credentials`/`urllib`/`_audit` calls, strict query rejections (`months=0/13`, `today=...`, `currency=pln`), and OpenAPI `0.26.0` binding checks.
3. **UI (`ui/src/budget/analytics/forecast.test.tsx`, `ui/src/budget/budget.test.tsx`):**
   - Synthetic fixtures only; verifies tab gating, EN/PL disclaimer, horizon/currency switches, overdue/dedup/lapsed banners, and immediate state wipe on access loss.

## 9. Open Questions for the Owner

1. **Lapsed recurring cutoff:** Should `LAPSED_AFTER_DAYS` be `45` days (`MAX_GAP_DAYS + 7` days posting grace) or longer (e.g., `60` days)? *Recommended default: `45` days from `evaluation_date`, paired with `freshness_stale` warning when `ledger_as_of` is `> 7` days old.*
2. **Pending current-month (`M0`) hint:** If `last_date` was in `M0 - 1` and its expected day in `M0` already passed within the `45`-day active window (e.g., bank sync lagged), keep it in `M0` or start at `M0 + 1`? *Recommended default: keep it in `M0` until lapsed at `46` days so delayed syncs do not hide current-month commitments.*
3. **Prior-month unpaid mortgages:** Should overdue mortgages from before `M0` stay excluded from `M0` totals and only show in `summary.overdue_mortgage_count`? *Recommended default: yes, report separately so each month reflects its own schedule.*
4. **Default horizon:** Should the default `months` value be `3` or `6` (max `12`)? *Recommended default: `3` months (max `12`), keeping initial estimates concise and lower-drift.*
5. **v1 mortgage-vs-recurring deduplication:** Is the zero-migration 1-to-1 conservative match (`currency + exact amount + ±5 days of clamped due_day`) acceptable for v1? *Recommended default: yes, with `deduped_recurring_count` displayed visibly and explicit linking deferred to v2.*
6. **Spending-limit overlay:** Should `BudgetCategoryLimit` rows stay completely out of the v1 Forecast tab? *Recommended default: yes, keep them on **Spending limits** to avoid mixing fixed commitments with variable caps.*

## 10. Effort Estimate

- **Backend PR (`Short`, zero migration):** Pure evaluator `hestia/budget/forecast.py`, `BudgetAnalyticsViews.forecast`, `analytics_api.py` / `web.py` / `api_contract.py` (`0.26.0`) + `docs/api/openapi.json`, and unit/views/API/contract tests.
- **UI PR (`Short`):** `ui/src/budget/api.ts`, `ui/src/budget/analytics/model.ts`, `ui/src/budget/analytics/forecast.tsx`, `forecast` tab in `ui/src/budget/budget.tsx` + `budget.css`, and Vitest specs.

## 11. v2 Option (Requires Schema Migration — Out of Scope for v1)

If future feedback shows exact-amount deduplication misses variable-rate mortgages or the owner wants manual control over recurring projections:
- **Explicit link/suppression table (migration `0020`):** Persist owner-managed links between `budget_mortgage_schedules` and recurring keys (plus optional recurring-hint snooze/dismiss flags) under CAS, replacing the `±5`-day exact-amount heuristic where explicitly linked.
- **Recurring amount bands (`recurring.py` `RULE_VERSION = 2`):** Group minor merchant price drift within an explicit band and project `last_amount` with an optional range indicator.
- **Discretionary envelope:** Optionally render enabled `BudgetCategoryLimit` ceilings in a separate non-summed section alongside committed totals.
