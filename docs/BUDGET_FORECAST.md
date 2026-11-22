# Budget Forecast: Upcoming Commitments Estimate (Design Proposal)

> **Status:** Revised design proposal v4 (Round-3 review fixes applied; owner decisions **Q1–Q6** and **(a)–(c)** all decided; target: OpenAPI `0.26.0`, zero schema migration in v1).  
> **Baseline:** `master` at `dc03790` (schema `0019`, OpenAPI `0.25.0` at `hestia/api_contract.py:76` with `GET /api/budget/analytics/quarantine`).

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
| **Q1** | Lapsed recurring cutoff | A recurring hint lapses when `(evaluation_date - last_date).days > 45` (`LAPSED_AFTER_DAYS = 45`), provided its source account(s) are not stale (see **(b)**). | [§5.3](#53-recurring-charge-hints-hestiabudgetrecurringpy) |
| **Q2** | Passed hint inside `M0` | If `last_date` was before `M0` and the expected day in `M0` already passed, the hint stays in `M0` until it lapses (never marked `past_due`). | [§5.3](#53-recurring-charge-hints-hestiabudgetrecurringpy) |
| **Q3** | Prior-month overdue mortgages | Overdue mortgage instalments from before `M0` (`due_date[:7] < M0`) are **not** added to `M0` totals and appear only as a schedule count. | [§5.2](#52-mortgage-schedules-budget_mortgage_schedules-budget_mortgage_payments) |
| **Q4** | Projection horizon | Default `months = 3`, maximum `12` (`1..12`). | [§6](#6-horizon-bounds-processing-order--caps) |
| **Q5** | Deduplication matching | Conservative 1:1 instalment↔hint heuristic on same `currency`, exact canonical `amount`, and `±5` days from due date; explicit DB linking is deferred to v2. | [§5.4](#54-deduplication-mortgage-schedule-vs-recurring-hint) |
| **Q6** | Spending limits | `BudgetCategoryLimit` rows are completely excluded from v1 Forecast totals and items. | [§5.5](#55-spending-limits-hestiabudgetspending_limitspy) |
| **(a)** | Unmarked `M0` mortgages & overdue inside `M0` | **Yes:** An `M0` mortgage instalment counts in full until manually marked paid, and an overdue instalment inside `M0` (`M0-01 <= due_date < evaluation_date`) stays in `M0` totals/items labelled `"unmarked"` (*nieoznaczona*). | [§5.2](#52-mortgage-schedules-budget_mortgage_schedules-budget_mortgage_payments), [§8](#8-ui-sketch-estimate-disclaimer--legal-review) |
| **(b)** | Stale-account recurring hints | **Yes:** Recurring hints from stale accounts do not lapse at `> 45` days; they remain visible/dedup-eligible and are flagged `"account not up to date"` (*konto nieaktualne*). | [§5.3](#53-recurring-charge-hints-hestiabudgetrecurringpy), [§8](#8-ui-sketch-estimate-disclaimer--legal-review) |
| **(c)** | Visibility of hidden duplicates | **Yes:** Hidden duplicates suppressed by 1:1 mortgage deduplication appear in the UI as a read-only `"merged with instalment"` (*połączono z ratą*) list (`deduplications[]`); link/unmerge controls defer to v2. | [§5.4](#54-deduplication-mortgage-schedule-vs-recurring-hint), [§8](#8-ui-sketch-estimate-disclaimer--legal-review) |

## 3. Review Resolution

### Round 1

| Review Finding | Summary of Fix | Resolving Section(s) |
| --- | --- | --- |
| **BLOCKER 1** (paid-off / ended loan resurrection) | Separate schedule↔hint identity matching (all `enabled` schedules, even with 0 unpaid horizon items or `end_date < M0`) from instalment qualification; exclude disabled schedules from both projection and dedup; add paid/mix/ended/disabled tests. | [§5.2](#52-mortgage-schedules-budget_mortgage_schedules-budget_mortgage_payments), [§5.4](#54-deduplication-mortgage-schedule-vs-recurring-hint), [§10](#10-testing-strategy) |
| **HIGH 2** (partially paid / unmarked `M0`) | Document v1 `"unmarked scheduled commitments"` (full instalment until manual mark; ledger debits never mark paid); keep `M0` past-due instalments in `M0` labelled `"unmarked"` (*nieoznaczona*) per **(a)**. | [§2(a)](#owner-decisions-decided), [§5.2](#52-mortgage-schedules-budget_mortgage_schedules-budget_mortgage_payments), [§8](#8-ui-sketch-estimate-disclaimer--legal-review) |
| **HIGH 3** (size bounds & truncation reconciliation) | Raise `MAX_FORECAST_ITEMS = 3_600` (`(100 + 200) × 12`); bound item/dedup `label` to `256` code points and serialized UTF-8 JSON to `4 MiB` (`413`, zero partial totals); define cap/filter/dedup order; reconcile `MAX_RECURRING_HINTS` truncation vs `413`. | [§5.3](#53-recurring-charge-hints-hestiabudgetrecurringpy), [§6](#6-horizon-bounds-processing-order--caps), [§10](#10-testing-strategy) |
| **HIGH 4** (freshness masking & lapse confusion) | Compute per-account UTC freshness (`stale_account_ids`) so one fresh account never masks a stale one; define `freshness_stale` normatively (`None` -> stale, `> 7` days UTC); keep stale-account hints active with `account_stale: true`. | [§2(b)](#owner-decisions-decided), [§5.1](#51-utc-clock-transaction-scope--normative-freshness), [§5.3](#53-recurring-charge-hints-hestiabudgetrecurringpy), [§10](#10-testing-strategy) |
| **HIGH 5** (schedule vs instalment count) | Rename `overdue_mortgage_count` to `overdue_mortgage_schedule_count`; define it on pre-`M0` earliest unpaid schedule reminders; test multi-overdue months in one schedule. | [§5.2](#52-mortgage-schedules-budget_mortgage_schedules-budget_mortgage_payments), [§7](#7-api-contract-sketch-openapi-0260-strictly-additive), [§10](#10-testing-strategy) |
| **HIGH 6** (incomplete 1:1 algorithm & opacity) | Specify total-order candidate sort key, greedy unused-pair loop, `deduplications[]` response array, and tie/reversed-order tests. | [§2(c)](#owner-decisions-decided), [§5.4](#54-deduplication-mortgage-schedule-vs-recurring-hint), [§7](#7-api-contract-sketch-openapi-0260-strictly-additive), [§10](#10-testing-strategy) |
| **MEDIUM 7** (API sketch defects) | Provide complete 3-month summing JSON example; filter `summary` warnings and `deduplications[]` by `currency`; add `forecast.RULE_VERSION`; give total-order item tie-break; drop unused repeated per-month evidence fields. | [§6](#6-horizon-bounds-processing-order--caps), [§7](#7-api-contract-sketch-openapi-0260-strictly-additive) |
| **MEDIUM 8** (test plan gates) | Require 100% line + branch Python coverage and full UI CI gate (`typecheck`, `lint`, `test`, `build`), plus an explicit test that existing OpenAPI paths/schemas are unchanged apart from `info.version`. | [§10](#10-testing-strategy) |
| **MEDIUM 9** (delivery checklist) | Require `_ANALYTICS_MODELS` & `_analytics_paths` registration (`api_contract.py`), `npm run gen:types` (`ui/src/api/openapi.ts`), updates to `BUDGET.md` / `BUDGET_2D.md` / this doc's status, and review of `privacy-policy.html` & `terms-of-service.html`. | [§11](#11-effort-estimate--delivery-checklist) |

### Round 2

| Review Finding | Summary of Fix | Resolving Section(s) |
| --- | --- | --- |
| **R2-1** (instalment-level dedup matching & `hint_key`) | Define `hint_key(h)` explicitly; key greedy matching by instalment `(schedule_id, matched_due_date)` via `used_installments` instead of `used_schedules` so multi-cluster stale loans match distinct historical instalments without resurrecting BLOCKER 1 while competing hints yield one match; take audit dates from accepted edge and `last_observed_date = h.last_date`; test both cases. | [§5.4](#54-deduplication-mortgage-schedule-vs-recurring-hint), [§10](#10-testing-strategy) |
| **R2-2** (item/audit flags & digest definitions) | Add `account_stale` to `deduplications[]` with UI chip; enforce `past_due` true only on overdue mortgage instalments and `account_stale = false` on all instalments; define `snapshot_digest` (from `_projection`) and `schedules_version` (all owner schedules before currency filter); test passed `M0` recurring item and matched 46-day hint over mixed fresh/stale accounts. | [§5.1](#51-utc-clock-transaction-scope--normative-freshness), [§5.2](#52-mortgage-schedules-budget_mortgage_schedules-budget_mortgage_payments), [§5.3](#53-recurring-charge-hints-hestiabudgetrecurringpy), [§7](#7-api-contract-sketch-openapi-0260-strictly-additive), [§8](#8-ui-sketch-estimate-disclaimer--legal-review), [§10](#10-testing-strategy) |
| **R2-3** (bounds, buffered send, cache claim & 413 UI recovery) | Encode once into a private bounded UTF-8 byte buffer (`<= 4 MiB`) and send that exact measured buffer; add tests for 3,600 short items passing and Unicode `> 4 MiB` returning `413`; cap `stale_account_ids` at `200` (`stale_account_ids_truncated`, `summary.stale_accounts`) while checking full internal set; narrow cache claim to reusing computed results; keep UI currency selector usable on initial `413` using account/schedule fallbacks (+ 3-letter input) and test filtered retry. | [§5.1](#51-utc-clock-transaction-scope--normative-freshness), [§6](#6-horizon-bounds-processing-order--caps), [§7](#7-api-contract-sketch-openapi-0260-strictly-additive), [§8](#8-ui-sketch-estimate-disclaimer--legal-review), [§9](#9-privacy-security--performance), [§10](#10-testing-strategy) |
| **R2-4** (OpenAPI `0.26.0` additivity vs `0.25.0` at `dc03790`) | Rebase baseline to `dc03790` (`0.25.0`); add forecast-specific `400`/`413`/`500` descriptions on `GET /api/budget/analytics/forecast`; compare `0.26.0` directly against `0.25.0` and update `tests/test_budget_contract.py` (`~180–215`) route and version assertions. | [§7](#7-api-contract-sketch-openapi-0260-strictly-additive), [§10](#10-testing-strategy), [§11](#11-effort-estimate--delivery-checklist) |
| **Owner `(a)–(c)`** (pending confirmations) | Record all three owner confirmations as **Decided (yes)** in `§2`. | [§2](#owner-decisions-decided) |

### Round 3

| Review Finding | Summary of Fix | Resolving Section(s) |
| --- | --- | --- |
| **R3-1** (a losing hint stays eligible for later edges) | A hint that loses one edge stays unmatched, so the greedy loop can still match it to another instalment. §5.3 projects only hints still unmatched after the whole loop. Replace the example that said the loser projects, and add a fixture where two hints compete for one instalment and the loser matches the second. | [§5.4](#54-deduplication-mortgage-schedule-vs-recurring-hint), [§10](#10-testing-strategy) |
| **R3-2** (narrow the memory guarantee) | The output caps cover only the returned `stale_account_ids` list and the encoded response. The full freshness set grows with the number of selected accounts. Check the UTF-8 length before extending the buffer. Test the `> 3_600` item guard by injecting a lower limit, because that branch is unreachable under the production caps. | [§5.1](#51-utc-clock-transaction-scope--normative-freshness), [§6](#6-horizon-bounds-processing-order--caps), [§9](#9-privacy-security--performance), [§10](#10-testing-strategy) |
| **R3-3** (forecast `409` and stored-data status) | Add a forecast-specific `409` description (SQLite backend required) on the forecast operation, with a contract-test assertion. After query validation, map stored-data validation errors from helpers (`_json` at `analytics_views.py:60-66`, a bare `AnalyticsError()` that is status `400` today) to `500 budget_analytics_data_error`. Leave limit and authorization errors as they are. Regression: a bad query returns `400` and bad stored data returns `500`. | [§7](#7-api-contract-sketch-openapi-0260-strictly-additive), [§10](#10-testing-strategy) |

## 4. Goals & Non-Goals

| Goals (v1) | Explicit Non-Goals |
| --- | --- |
| Read-only monthly projection of known upcoming outflows over `1..12` months (default `3`). | Cross-currency totals or FX conversion. |
| Combine enabled mortgage schedules (`budget_mortgage_schedules`) and active monthly recurring hints (`recurring.py`). | Predicting variable/discretionary spending, income, or future account balances. |
| Conservative zero-migration 1:1 instalment↔hint deduplication with transparent `deduplications[]` inspection. | ML scoring, statistical seasonality, or subscription cancellation judgments (`docs/BUDGET_2D.md:78`). |
| Per-account UTC stale visibility, explicit EN/PL estimate disclaimer, and fail-closed bounds. | Outbound notifications (email/push), shared calendar writes, or bank payment requests. |
| Zero DB migration (`0019`) and zero audit/SSE writes. | Treating `BudgetCategoryLimit` (`hestia/budget/spending_limits.py:1`) as committed outflow. |

## 5. Data Sources & Exact Projection Rules

### 5.1 UTC Clock, Transaction Scope & Normative Freshness
Evaluation uses one server UTC clock reading `evaluation_date` (`datetime.fromtimestamp(self._now(), timezone.utc).date().isoformat()`, matching `hestia/budget/analytics_views.py:853, 1008`) inside one SQLite read transaction (`_read_lock` at `analytics_views.py:55` + `self._read(owner)` at `hestia/budget/views.py:80-84`). Let `M0 = evaluation_date[:7]` (`YYYY-MM`). The horizon covers `[M0, end_month]` inclusive (`horizon_months` consecutive calendar months in `1900-01..9999-12`).

- **`snapshot_digest`:** Taken directly from `snapshot["digest"]` produced by `self._projection(session, owner)` (`analytics_views.py:250, 636-647`), which hashes the owner's full selected-account projection inputs (accounts, identities, selected ledger, metadata, annotations, categories, rules, decisions) and is invariant to `months` and `currency`.

Existing `_ledger_as_of` (`analytics_views.py:804-815`) takes `max` across all selected accounts, allowing one fresh account to mask a stale one (`mortgages` remains unchanged). Forecast instead computes **per-account UTC freshness** across every selected account `a` (`BudgetAccount.enabled == True` joined with `BudgetConnection.owner == owner`, `hestia/db.py:278-297`):
1. For each selected account `a`, let `comp_ts` be `BudgetComponent.last_success` (`operation == "transactions"`, `hestia/db.py:314-321`) and `tx_ts` be `max(BudgetTransaction.observed_at)` where `uncertain == False` (`hestia/db.py:441-451`). Let `account_ts(a) = max(ts for ts in (comp_ts, tx_ts) if ts is not None)`, or `None` when both are absent.
2. When `account_ts(a)` is present, `account_as_of(a) = min(datetime.fromtimestamp(account_ts(a), timezone.utc).date().isoformat(), evaluation_date)` (clamping clock skew to `evaluation_date`, matching `analytics_views.py:814-815`); otherwise `account_as_of(a) = None`.
3. **Normative staleness rule (UTC, 7-day boundary matching `DEFAULT_STALE_AFTER_DAYS = 7`, `mortgage_reminders.py:20, 196`):**
   - `account_stale(a) = True` iff `account_as_of(a) is None` OR `(date.fromisoformat(evaluation_date) - date.fromisoformat(account_as_of(a))).days > 7`. Age `<= 7` days is fresh (`False`); age `>= 8` days is stale (`True`); `None` (never synced and no non-uncertain transactions) is always stale (`True`).
   - `stale_account_ids_set`: full set of selected `account.id` values with `account_stale(a) == True`; `summary.stale_accounts = len(stale_account_ids_set)`.
   - `stale_account_ids`: sorted list `sorted(stale_account_ids_set)[:MAX_STALE_ACCOUNT_IDS]` where `MAX_STALE_ACCOUNT_IDS = 200`, with `stale_account_ids_truncated = len(stale_account_ids_set) > MAX_STALE_ACCOUNT_IDS`. **Internal freshness checks (`§5.3`) and `freshness_stale` always use the full `stale_account_ids_set`**, never the capped display slice. `MAX_STALE_ACCOUNT_IDS` covers only this returned list. The encoded response has its own cap (§6), and that path checks the byte count before extending the buffer. The full freshness set grows with the number of selected accounts.
   - `ledger_as_of`: latest `account_as_of(a)` across selected accounts, or `None` if all selected accounts have `account_as_of(a) is None` (or `selected_accounts == 0`).
   - **Normative `freshness_stale`:** `True` iff `selected_accounts == 0` OR `len(stale_account_ids_set) > 0` (thus `ledger_as_of is None` always implies `freshness_stale == True`, and one fresh account never masks a stale selected account).

### 5.2 Mortgage Schedules (`budget_mortgage_schedules`, `budget_mortgage_payments`)
Reuses `hestia/db.py:517-585` and `hestia/budget/mortgage_reminders.py`:
1. **Eligibility, `schedules_version` & disabled schedules:** Load **all** `BudgetMortgageSchedule` rows owned by `owner` ordered by `BudgetMortgageSchedule.id` with all `BudgetMortgagePayment` `paid_months` ordered by `(schedule_id, month)` (`analytics_views.py:821-829`). Enforce `MAX_SCHEDULES = 100` and `MAX_PAID_MONTHS = 1_200` (`mortgage_reminders.py:21-22`). Compute `schedules_version = _schedules_version(owner, all_schedules)` (`analytics_views.py:385-392, 860`) over **all** owner schedules and paid marks **before** applying any `?currency` filter (or `enabled` filter), matching `GET /mortgages`. **Disabled schedules (`enabled == False`) are excluded from both forecast projection and deduplication** (consistent with `mortgage_reminders.py:220-221`; keeping a finished loan as a dedup anchor requires `enabled = True` with `end_date` and/or `paid_months`).
2. **Unmarked scheduled commitments:** `budget_mortgage_payments` stores only `(schedule_id, month)` markers with no amount or transaction link (`hestia/db.py:565-585`, `analytics_store.py:327-366`). In v1, unpaid instalments are **unmarked scheduled commitments**: each unpaid month projects the full `schedule.amount` until marked paid manually via `mortgage-payment-set`, and a ledger `DBIT` **never** marks a mortgage instalment as paid or reduces its amount.
3. **Per-month instantiation (`M` in `[M0 .. end_month]`):** Call `installment_due_date(schedule.due_day, schedule.start_date, schedule.end_date, M)` (`mortgage_reminders.py:137-153`), which clamps `due_day` to `calendar.monthrange(year, month)[1]` and returns `None` outside `[start_date, end_date]`. Omit `M` if `M in schedule.paid_months` (`analytics_store.py:340` permits future paid marks within `[start_date, end_date]`).
4. **Overdue inside `M0` vs. prior to `M0`:**
   - **Inside `M0` (`M0-01 <= due_date < evaluation_date`, unmarked):** Per **§2(a)**, stays in `M0` items and totals with full `schedule.amount` and `past_due = True` (UI renders `"unmarked"` / *nieoznaczona*). All non-overdue instalments (`due_date >= evaluation_date`) have `past_due = False`.
   - **Before `M0` (`due_date[:7] < M0`):** Per **Q3**, **never** rolled into `M0` totals. Because `evaluate_mortgage_reminders` returns only each enabled schedule's **earliest** unpaid qualifying instalment (`mortgage_reminders.py:162-172, 232-235, 253`), `summary.overdue_mortgage_schedule_count` counts **enabled schedules** (currency-filtered when `?currency` is set, not instalments) whose earliest unpaid `due_date` satisfies `due_date is not None and due_date[:7] < M0`. (A schedule with 3 overdue prior months counts as `1`; a schedule overdue only inside `M0` counts as `0` here because that instalment is projected in `M0`.)
5. **Amount & item flags:** Canonical positive decimal `schedule.amount`; `source = "mortgage"`, `confidence = "scheduled"`, and **`account_stale = False` always** on mortgage instalment items (stale account sync is surfaced via `freshness_stale` / `stale_account_ids` and on recurring/dedup rows, never on mortgage instalments).

### 5.3 Recurring-Charge Hints (`hestia/budget/recurring.py`)
Reuses the complete, untruncated `snapshot["_recurring"]` produced by `_recurring_hints(snapshot)` (`hestia/budget/analytics_views.py:328-351`) wrapping `detect_recurring_charges` (`hestia/budget/recurring.py:257-278`):
1. **Input scope & grouping:** Selected accounts (`BudgetAccount.enabled`), eligible `BOOK` `DBIT` `expense` entries (`recurring.py:196`), excluding accepted own transfers (`analytics_views.py:475-476`) and `excluded` entries. Requires `>= 3` occurrences across `>= 3` distinct months with gaps in `[23, 38]` days (`recurring.py:27-30`). Grouping by `(currency, label_source, normalized_label, canonical_amount)` (`recurring.py:133-139, 200, 238`) yields one exact `hint.amount` per `RecurringHint` (`RULE_VERSION = 1`).
2. **Lapse vs. stale-account rule (Q1 + §2(b)):** For each hint `h`, let `hint_stale = any(aid in stale_account_ids_set for aid in h.account_ids)` (using the full `stale_account_ids_set`) and `age_days = (date.fromisoformat(evaluation_date) - date.fromisoformat(h.last_date)).days`.
   - If `hint_stale == False` and `age_days > 45` (`LAPSED_AFTER_DAYS = 45`): `h` is **lapsed** (excluded from dedup and projection; `summary.lapsed_recurring_count += 1`).
   - Otherwise `h` is **active**: if `hint_stale == True`, `h` **does not lapse** even when `age_days > 45` (`summary.stale_recurring_count += 1`), remains eligible for dedup (carrying `account_stale = True` into `deduplications[]` if matched), and any surviving projected items carry `account_stale = True`.
3. **Current & future-month projection:** Let `last_ym = hint.last_date[:7]` and `anchor_day = date.fromisoformat(hint.last_date).day`. For each `M` in `[M0 .. end_month]`, emit at most one item iff `M > last_ym` (preventing double-count in `last_ym`, while under **Q2** a passed hint with `last_ym < M0` stays in `M0` until it lapses), with `expected_date = date(year, month, min(anchor_day, calendar.monthrange(year, month)[1])).isoformat()`. Recurring items **always** have `past_due = False` (even in `M0` when `expected_date < evaluation_date`; `past_due` applies solely to overdue mortgage instalments).
4. **Confidence tiers:** `"high"` when `hint.occurrence_count >= 6` and `(hint.max_gap_days - hint.min_gap_days) <= 7`; `"medium"` otherwise.
5. **Reconciliation with `MAX_RECURRING_HINTS`:** `GET /recurring` is a non-summing list view (`analytics_views.py:774-777`) that truncates at `MAX_RECURRING_HINTS = 200` and sets `truncated: true`. `GET /forecast` computes **monetary totals**, so silently truncating after 200 hints would emit incomplete totals (`docs/BUDGET_2D.md:173-174`). Therefore `forecast` reads the untruncated `snapshot["_recurring"]`, filters by `currency` (if given), partitions lapsed vs. active hints, and fails closed with `413 budget_analytics_limit` (zero partial totals) if active hints exceed `MAX_FORECAST_HINTS = 200`.

### 5.4 Deduplication: Mortgage Schedule vs. Recurring Hint
To prevent paid-off or ended loans from being resurrected by lingering bank history (**BLOCKER 1**), **identity matching is strictly separated from horizon instalment qualification and operates at instalment granularity `(schedule_id, matched_due_date)`**:
1. **Hint identity `hint_key(h)`:** Define
   `hint_key(h) = (h.currency, h.normalized_label, h.label_source, analytics_core.canonical_money(h.amount), h.first_date, h.last_date)`.
   (Note: `RecurringHint.amount` from `detect_recurring_charges` is already canonical via `_amount` at `recurring.py:133-139`, and `detect_recurring_charges` guarantees `hint_key(h)` uniquely identifies each hint `h` per `recurring.py:276-277`.)
2. **Candidate sets:** Every **enabled** `MortgageSchedule` `s` (`s.enabled == True`, even if all months in `[M0 .. end_month]` are in `s.paid_months` or `s.end_date < evaluation_date`) and every **active** (non-lapsed) `RecurringHint` `h` (both already currency-filtered when `?currency` is set). Disabled schedules never participate.
3. **Instalment-level candidate edges:** A schedule instalment `(s.schedule_id, matched_due_date)` and a hint `h` form a candidate edge `((s.schedule_id, matched_due_date), hint_key(h))` iff:
   - `h.currency == s.currency`, AND
   - `analytics_core.compare_money(h.amount, s.amount) == 0`, AND
   - Checking candidate months `M_due` in `[shift_month(h.last_date[:7], -1), h.last_date[:7], shift_month(h.last_date[:7], +1)]` (within `1900-01..9999-12`), `matched_due_date = installment_due_date(s.due_day, s.start_date, s.end_date, M_due)` is not `None` and satisfies `distance = abs((date.fromisoformat(h.last_date) - date.fromisoformat(matched_due_date)).days) <= 5`. (At most one `M_due` can satisfy `distance <= 5` for a given `(s, h)` because consecutive monthly due dates are `>= 28` days apart.)
4. **Total-order sort key & greedy 1:1 instalment↔hint selection (HIGH 6 + R2-1):** Sort all candidate edges ascending by:
   `(distance, matched_due_date, *hint_key(h), s.schedule_id)`
   (Because `(hint_key(h), s.schedule_id)` is unique per candidate `(s, h)` pair and `matched_due_date` is unique for that pair, this sort key is a strict total order invariant to input order.)
   Iterate in sorted order with `used_installments: set[tuple[str, str]] = set()` and `used_hints: set = set()`:
   - Let `edge_installment = (s.schedule_id, matched_due_date)`.
   - Skip if `edge_installment in used_installments` or `hint_key(h) in used_hints`.
   - Otherwise add `edge_installment` to `used_installments`, add `hint_key(h)` to `used_hints`, and append a `BudgetAnalyticsForecastDeduplication` audit record with `matched_due_date` and `day_distance = distance` from the accepted edge, `last_observed_date = h.last_date`, and `account_stale = any(aid in stale_account_ids_set for aid in h.account_ids)`.
   When the skip is `edge_installment in used_installments`, `hint_key(h)` stays unused. A hint that loses one edge stays eligible for later edges, and the loop can match it to another instalment. §5.3 projects only hints that are still unmatched when the loop ends.
5. **Counterexample handling (R2-1, R3-1):**
   - *Multi-cluster stale loan:* A Jan–Aug loan with debits in Jan–Mar and Jun–Aug produces two clusters in `recurring.py:217-225`, both remaining active on a stale account. Because `used_installments` keys by `(s.schedule_id, matched_due_date)`, the clusters match **distinct** historical instalments (`2026-03-15` and `2026-08-15`), so **both** are suppressed and neither projects in October onwards.
   - *Competing hints for one instalment:* Two hints within `±5` days of the same `(s.schedule_id, matched_due_date)` share that `edge_installment`, so the total order accepts at most one edge for it. The hint on the skipped edge stays eligible. When a later edge pairs that hint with another instalment, the loop accepts the later edge and both hints are suppressed. A competing hint is projected under §5.3 only when it is still unmatched after the whole loop.
6. **Horizon governance:** Any matched hint `h` is suppressed for the **entire** horizon `[M0 .. end_month]` even when its matched schedule `s` emits `0` unpaid instalments in `[M0 .. end_month]`. Final `deduplications` is sorted by `(currency, matched_due_date, schedule_id)` (unique because each `(schedule_id, matched_due_date)` matches at most once) and `summary.deduped_recurring_count = len(deduplications)`.

### 5.5 Spending Limits (`hestia/budget/spending_limits.py`)
Per **Q6**, `BudgetCategoryLimit` rows (`hestia/db.py:492-514`, `"organisational aids only"`, `spending_limits.py:1`) are completely excluded from v1 Forecast totals and items.

## 6. Horizon, Bounds, Processing Order & Caps

- **Horizon:** `months` query parameter, default `3`, allowed `1..12` (UTC calendar months from `M0`).
- **Normative processing order (cap → filter → dedup → project → bounded encode):**
  1. Validate query params (`months`, `currency`); fail invalid with `400 budget_analytics_invalid_request`. After this validation, the forecast read maps stored-data helper errors to `500 budget_analytics_data_error` (§7). Limit errors stay `413`. Authorization errors stay `401`, `403`, `404`, and `409`.
  2. Read `_projection(session, owner)` (enforces `MAX_ROWS = 100_000`, `analytics_views.py:38, 223-227` -> `413`) and all owner schedules/paid marks (enforces `MAX_SCHEDULES = 100`, `MAX_PAID_MONTHS = 1_200`, `mortgage_reminders.py:21-22` -> `413`). Compute `snapshot_digest = snapshot["digest"]` and `schedules_version = _schedules_version(owner, all_schedules)` **before** currency filtering.
  3. Compute per-account UTC freshness across all selected accounts (`stale_account_ids_set`, `summary.stale_accounts`, capped `stale_account_ids` at `MAX_STALE_ACCOUNT_IDS = 200` with `stale_account_ids_truncated`, `ledger_as_of`, `freshness_stale`) and compute/reuse untruncated `snapshot["_recurring"]`. `MAX_STALE_ACCOUNT_IDS` covers only the returned list. The full freshness set grows with the number of selected accounts (§5.1).
  4. Apply `currency` filter (if provided) to enabled `schedules` and detected `hints`.
  5. Partition filtered `hints` into lapsed vs. active (`§5.3`) using full `stale_account_ids_set`, computing `lapsed_recurring_count` and `stale_recurring_count`. Enforce `len(active_hints) <= MAX_FORECAST_HINTS` (`200`) -> else `413 budget_analytics_limit`.
  6. Run instalment-level 1:1 deduplication (`§5.4`) on filtered enabled `schedules` and filtered `active_hints`.
  7. Instantiate all items in `[M0 .. end_month]` for filtered enabled `schedules` (`past_due = expected_date < evaluation_date`, `account_stale = False`) and surviving `active_hints` (`past_due = False`, `account_stale = hint_stale`); compute `overdue_mortgage_schedule_count`. Enforce `len(projected_items) <= MAX_FORECAST_ITEMS` (`3_600` = `(100 + 200) × 12`) -> else `413 budget_analytics_limit`. With `MAX_SCHEDULES = 100`, `MAX_FORECAST_HINTS = 200`, and `months <= 12` already applied, `len(projected_items) > 3_600` cannot occur on this path; §10 tests the guard by injecting a lower limit.
  8. Bound display labels (`MAX_FORECAST_LABEL = 256` code points: recurring `remittance_information` in `normalize.py:124-133` + `analytics_views.py:345` can reach `409_699` chars and `MAX_DESCRIPTION = 524_288` in `recurring.py:33, 157`; any forecast `label` / `schedule_label` / `recurring_label` longer than `256` code points is truncated as `value[:255].rstrip() + "…"` while `schedule.label` is already `<= 256` at `mortgage_reminders.py:25`), sort `items` and `deduplications`, sum totals via `sum_money`, and **encode once into a private bounded UTF-8 byte buffer** (`json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":")).iterencode(payload)`). Check the next chunk's UTF-8 length **before** extending the private `bytearray`. When `len(buffer) + len(chunk) > MAX_FORECAST_RESPONSE_BYTES` (`4_194_304` = `4 MiB`), raise `413 budget_analytics_limit` and leave that chunk unappended, so the buffer stays within `4 MiB` (**zero partial totals**). On HTTP, send **that exact measured byte buffer** with `application/json; charset=utf-8` and `api.PRIVATE_HEADERS` (rather than re-encoding a dict via `web.json_response` at `analytics_api.py:121`).

## 7. API Contract Sketch (OpenAPI `0.26.0`, Strictly Additive)

Compare strictly against direct predecessor `0.25.0` at `dc03790`: register `"forecast"` in `READS` and `_QUERY_FIELDS` (`hestia/budget/analytics_api.py:19-20, 33-38`), `("GET", "/api/budget/analytics/forecast"): "admin"` in `_ROUTE_MIN_ROLE` (`hestia/web.py:175-184`) and `_PATH_ROLES` (`hestia/api_contract.py:2286-2306`), and models in `_ANALYTICS_MODELS` (`api_contract.py:2254-2266`) and `_analytics_paths()` (`api_contract.py:2915-2988`). Response schemas use `_READ` (`api_contract.py:115, 3106-3128`). Bump `CONTRACT_VERSION` from `"0.25.0"` to `"0.26.0"` (`api_contract.py:76`).

- **Endpoint:** `GET /api/budget/analytics/forecast` (`allow_head=False`)
- **Query parameters (strict, no duplicate keys, no `today` param):**
  - `months` (optional integer string `1..12`, regex `^[0-9]{1,2}$`, default `3`).
  - `currency` (optional `^[A-Z]{3}$`). When set, filters `totals`, `months[].currencies`, `months[].items`, `deduplications`, **and** currency-scoped `summary` counts (`overdue_mortgage_schedule_count`, `lapsed_recurring_count`, `deduped_recurring_count`, `stale_recurring_count`) so warnings from filtered-out currencies never leak (`MEDIUM 7`). `summary.selected_accounts`, `summary.excluded_accounts`, `summary.stale_accounts`, `stale_account_ids`, `stale_account_ids_truncated`, `ledger_as_of`, `freshness_stale`, `snapshot_digest`, and `schedules_version` remain owner-wide (before currency filtering).
- **New `0.26.0` Response Schemas (all fields below are new):**
  - `BudgetAnalyticsForecast`: `{evaluation_date, evaluation_timezone ("UTC"), horizon_months, start_month, end_month, ledger_as_of (str | null), freshness_stale (bool), stale_account_ids (list[str], <= 200), stale_account_ids_truncated (bool), snapshot_digest, schedules_version, rule_version (forecast.RULE_VERSION = 1), summary, totals, deduplications, months}`.
  - `BudgetAnalyticsForecastSummary`: `{selected_accounts, excluded_accounts, stale_accounts, overdue_mortgage_schedule_count, lapsed_recurring_count, deduped_recurring_count, stale_recurring_count}` (integers).
  - `BudgetAnalyticsForecastCurrencyTotal`: `{currency, total, scheduled_total, estimated_total}` (exact decimal strings, sorted by `currency`).
  - `BudgetAnalyticsForecastDeduplication`: `{schedule_id, schedule_label, recurring_label, amount, currency, matched_due_date, last_observed_date, day_distance, account_stale}` (sorted by `(currency, matched_due_date, schedule_id)`).
  - `BudgetAnalyticsForecastMonth`: `{month, currencies, items}` (always `horizon_months` ordered entries; empty months carry `[]`).
  - `BudgetAnalyticsForecastItem`: `{source ("mortgage" | "recurring"), schedule_id (str | null), label (<=256 chars), amount, currency, expected_date, confidence ("scheduled" | "high" | "medium"), past_due (bool), account_stale (bool)}`. `past_due` is `true` **only** on overdue mortgage instalments (`M0-01 <= expected_date < evaluation_date`) and always `false` on recurring items; `account_stale` is always `false` on mortgage instalments and reflects hint source staleness on recurring items. Repeated per-month evidence fields (`account_ids`, `label_source`, `occurrence_count`, `last_observed_date`) are omitted; the Recurring tab owns full evidence. Sorted by strict total order `(expected_date, currency, source, label, schedule_id or "", amount, tie_breaker)` where `tie_breaker` is `""` for `"mortgage"` and `(hint.normalized_label, hint.label_source, hint.first_date, hint.last_date)` for `"recurring"`.

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
  "stale_account_ids_truncated": false,
  "snapshot_digest": "<64-hex>",
  "schedules_version": "<64-hex>",
  "rule_version": 1,
  "summary": {
    "selected_accounts": 2,
    "excluded_accounts": 0,
    "stale_accounts": 0,
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
      "day_distance": 1,
      "account_stale": false
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

- **Forecast-Specific OpenAPI Operation & Error Descriptions (`api_contract.py:2915-2996`, `BudgetError` at `1516-1519`):**
  Register a forecast override in `_analytics_paths()` (leaving all `0.25.0` routes untouched) with explicit `summary`, `description`, and forecast-specific error descriptions:
  - `400 budget_analytics_invalid_request`: `"Invalid forecast query: months must be an integer 1..12, currency must match ^[A-Z]{3}$, and unknown or duplicate parameters (including today) are rejected."` This status is the forecast query-validation result.
  - `401 budget_not_signed_in`, `403 budget_forbidden`, and `404 budget_disabled` / `budget_analytics_disabled` remain the results `_principal` and the analytics enabled-check already return (`api.py:54-60`, `analytics_api.py:103-104`).
  - `409 budget_requires_sqlite_users`: `"SQLite backend required: forecast requires HESTIA_PERSIST=sqlite."` `_principal` raises `BudgetUsersBackendRequired` when persist is not SQLite (`api.py:61-62`). The analytics handler forwards that exception to `api._failure` (`analytics_api.py:124-125`), which returns `409 budget_requires_sqlite_users` (`api.py:198-199`). This description is on the forecast operation. Shared analytics responses keep the `409` text at `api_contract.py:2993`.
  - `413 budget_analytics_limit`: `"Forecast limit exceeded (>100,000 selected rows, >100 schedules, >1,200 paid marks per schedule, >200 active currency-filtered hints, >3,600 projected items, or >4 MiB UTF-8 JSON payload); zero partial totals returned."` Explicit limit errors stay `413`, including `AnalyticsError("budget_analytics_limit", 413)` at `analytics_views.py:227`.
  - **Stored-data mapping (forecast read, after query validation):** Helpers that reject stored rows raise a bare `AnalyticsError()`, which today is status `400` / `budget_analytics_invalid_request` (`analytics_core.py:24`). `_json` does so at `hestia/budget/analytics_views.py:60-66` (the `raise` is line 66) when stored JSON fails to parse as the expected value. Map those stored-data validation errors to `500 budget_analytics_data_error` at the forecast read boundary. A bad query still returns `400`. Bad stored data returns `500`. Limit errors (`413`) and authorization errors (`401`, `403`, `404`, `409`) keep the status and code they already have. Other analytics routes continue to see the helper's default `400`.
  - `500 budget_analytics_data_error` / `budget_internal_error`: `"Stored mortgage schedule, ledger, or metadata integrity check failed (`budget_analytics_data_error`), including stored-data helper errors mapped to 500 after forecast query validation, or unexpected internal failure (`budget_internal_error`)."`, `503 budget_analytics_unavailable` / `budget_unavailable`.

## 8. UI Sketch, Estimate Disclaimer & Legal Review

- **Tab placement:** Add a 9th button `"forecast"` (`Forecast` / `Prognoza`) after `"mortgages"` in `ui/src/budget/budget.tsx:28, 74, 135, 449`, gated by `hasAnalytics(capabilities)`: `Accounts`, `Inbox`, `Month`, `Year`, `Spending limits`, `Recurring`, `Mortgages`, **`Forecast`**, `Categories and rules`. Implemented in `ui/src/budget/analytics/forecast.tsx` (`ForecastView`), wired via `budgetApi.forecast(months, currency)` in `ui/src/budget/api.ts` and generated types in `ui/src/budget/analytics/model.ts`; formats money strictly via `formatAmount` (`ui/src/budget/model.ts:68-79`).
- **Controls, `413` currency recovery & badges:**
  - Estimate disclaimer right below the heading (matching `recurring.tsx:106` / `mortgages.tsx:496`).
  - Horizon switcher (`1`, `3`, `6`, `12`, default `3`) and currency filter controls rendered **outside** the `ready` block so they stay interactive on `413`.
  - **Fallback currency discovery (`R2-3`):** Unlike `recurring.tsx:91-95` (which populates `currencies` only after a successful unfiltered call), `ForecastView` seeds `currencies` synchronously from `accounts` (`account.display?.currency` and `account.observed_balance_currencies`, matching `api_contract.py:1261-1263` and `analytics/model.ts:467-476`). When an unfiltered `forecast` call (`currency === ""`) fails with `413` (`ANALYTICS_ERROR.limit`), `ForecastView` keeps the currency selector enabled, fetches `api.mortgages()` (passing through `onAccessLoss` and generation guards) to merge `schedules[].currency`, displays a clear EN/PL `413` recovery notice instructing the user to filter by currency or shorten the horizon, and also renders a 3-letter ISO-4217 currency filter input (`validCurrency`) on `413` so recovery still works even when a ledger currency is absent from account metadata and schedules. On any successful response, observed currencies from `totals[].currency` and `deduplications[].currency` are merged into `currencies`.
  - Metadata & warnings: `Evaluated for <date> (UTC)`, `Ledger data as of <date>` + stale-account names resolved via `accountName` (`analytics/model.ts:341-348`) when `freshness_stale` is true (plus explicit overflow count `summary.stale_accounts - stale_account_ids.length` when `stale_account_ids_truncated` is true); banners for `overdue_mortgage_schedule_count > 0` (jump to `Mortgages`), `lapsed_recurring_count > 0`, and `stale_recurring_count > 0`.
  - Per-item status chips: `"Unmarked"` (*"Nieoznaczona"*) when `item.past_due` is true (`§2(a)` — overdue mortgage instalments only), and `"Account not up to date"` (*"Konto nieaktualne"*) when `item.account_stale` is true (`§2(b)` — recurring items only).
  - Read-only `"Merged with instalment"` (*"Połączono z ratą"*) section (`§2(c)`) listing `deduplications[]` (`recurring_label` → `schedule_label`, `amount`, `matched_due_date`, `last_observed_date`, plus the `"Account not up to date"` / *"Konto nieaktualne"* chip whenever `dedup.account_stale` is true) when non-empty.
- **Mandatory estimate disclaimer wording:**
  - **EN:** `"Estimated upcoming commitments based on your mortgage schedules and monthly recurring expense patterns (at least three occurrences). This is an estimate only — not a guarantee, cashflow balance forecast, or prediction of variable spending."`
  - **PL:** `"Szacunek nadchodzących zobowiązań na podstawie Twoich harmonogramów hipotecznych i powtarzalnych wydatków miesięcznych (co najmniej trzy wystąpienia). To wyłącznie szacunek — nie gwarancja, prognoza salda ani przewidywanie wydatków zmiennych."`
- **Public legal pages:** Review `ui/public/privacy-policy.html:199-205, 297-299` and `ui/public/terms-of-service.html:113-117, 133-136` (`docs/BUDGET.md:13-14`) to ensure on-read upcoming-commitment estimates and heuristic duplicate suppression are accurately disclosed.

## 9. Privacy, Security & Performance

- **Auth & isolation:** Reuses `api._principal`, `BudgetHTTPStore._owner`, and strict `owner == ...` SQL filters (`views.py:70-85`). Both cookie and bearer auth allowed on `GET` (`docs/BUDGET.md:70-71`).
- **No leakage:** Pure module `hestia/budget/forecast.py` uses `@dataclass(frozen=True, repr=False)` (like `mortgage_reminders.py:52, 69`) and fixed-string errors (`ForecastInputError`, `ForecastDataError`, `ForecastLimitError`). `analytics_api.py:124-125` delegates unexpected errors to `api._failure` (`api.py:208`), logging only `operation` and `type(exc).__name__`. Zero amounts, labels, or descriptions enter logs, `budget_analytics_audit`, `/api/audit`, SSE, or Iris.
- **Browser hygiene:** All forecast state lives strictly in React component state, passes `onAccessLoss` (`budget.tsx:214-225`), and never enters `localStorage` or URL query strings (`docs/BUDGET.md:247-252`).
- **Performance, memoisation & bounded buffering:** Executes inside `_read_lock` and `self._read(owner)` (`analytics_views.py:55, 766`). Reuses computed results from `self._projection(session, owner)` and `snapshot["_recurring"]` (`analytics_views.py:636-647, 767-771`) so switching between `Recurring` and `Forecast` on an unchanged snapshot avoids recomputing the projection, transfer matching, and recurring-hint detection (while `_source_digest` still reads and hashes the selected ledger rows per request to validate the cache key, `analytics_views.py:562-610, 639`). Schedule queries stay within `<= 100` rows and `<= 1_200` paid marks each, and projection stays within `<= 100` schedules, `<= 200` active hints, and `<= 12` months (at most `3_600` items). The `<= 200` cap applies to the returned `stale_account_ids` list, and the `4 MiB` cap applies to the encoded response: the encoder checks the next chunk's UTF-8 length before extending the private buffer. The full freshness set grows with the number of selected accounts.

## 10. Testing Strategy

Implementation PRs must pass **both** repository CI gates (`.github/workflows/ci.yml:15-67`, `.coveragerc:1-7`): **100% Python line + branch coverage** (`python -m coverage run -m unittest discover -s tests && python -m coverage report --fail-under=100`) and the **UI gate** (`cd ui && npm run typecheck && npm run lint && npm run test && npm run build`).

1. **Pure evaluator (`tests/test_budget_forecast.py`):**
   - **Calendar & decimals:** Leap/month-end clamping (`2026-02-28`, `2028-02-29`, `2100-02-28`, `30`-day months); mixed currencies with 77-digit decimals (`test_budget_mortgage_reminders.py:197-198`).
   - **BLOCKER 1 & HIGH 2 (schedules, paid marks, `M0`, flags):** Whole horizon paid (future paid marks allowed, `analytics_store.py:340`), mixed paid/unpaid months, ended schedule (`end_date < M0` and settlement `±5d` past `end_date`), and **disabled schedule** (never projects and never suppresses a matching hint); unmarked `M0` mortgage stays in `M0` with full amount (`past_due=true` when `< evaluation_date`, `account_stale=false` always on mortgage items); **recurring item in a passed `M0`** (`last_date` before `M0`, expected day `< evaluation_date`) stays in `M0` with **`past_due=false`**.
   - **HIGH 5 (`overdue_mortgage_schedule_count`):** 3 overdue months before `M0` in 1 schedule yields count `1`; overdue only inside `M0` yields count `0` while projecting in `M0`.
   - **HIGH 4 (freshness, lapse & mixed fresh/stale matching):** `ledger_as_of=None` -> `freshness_stale=True`; 7-day UTC boundary (`7d` fresh vs `8d` stale); one fresh account does not mask a stale selected account (`stale_account_ids`); `45d` active vs `46d` lapsed (fresh account) vs `46d` non-lapsing (`account_stale=True`) on stale account; **`matched 46-day hint over a mix of fresh and stale accounts`** stays active (`account_stale=True`), matches its schedule into `deduplications[]` with `account_stale=True`, suppresses projection, increments `stale_recurring_count` and `deduped_recurring_count`, and leaves mortgage item `account_stale=False`; `last_date[:7] == M0` vs `last_date[:7] < M0`; **`stale_account_ids` cap** (`201` stale selected accounts caps `stale_account_ids` to `200` with `stale_account_ids_truncated=True`, `summary.stale_accounts=201`, and keeps a 46-day hint on the 201st account active with `account_stale=True`).
   - **HIGH 6 & R2-1 (instalment-level 1:1 dedup & `hint_key`):** Tie-breaks (same distance, same label/amount), reversed input order and random permutations yield identical `deduplications[]` and `months[].items`; **`Jan–Aug` loan with `Jan–Mar` + `Jun–Aug` hint clusters on a stale account** matches two distinct historical instalments (`2026-03-15`, `2026-08-15`) and suppresses both from `Oct` onwards; **two hints competing for the same instalment** accept exactly one edge for that instalment (audit `matched_due_date`/`day_distance` from the accepted edge, `last_observed_date = h.last_date`); the loser stays eligible and is projected only when it is still unmatched after the whole loop; **two hints compete for the same instalment and the loser matches a second instalment on another enabled schedule** (the shared instalment sorts first; one hint has at most one due month per schedule, so the second match is a different schedule; both hints end matched and neither is projected); 1 hint vs 2 equal schedules yields one match.
   - **HIGH 3 & R2-3 (caps, bounded UTF-8 buffer & labels):** `100` schedules + `200` hints × `12` months (`3_600` short items) **passes** (`200`, `< 4 MiB`); `201` active hints -> `ForecastLimitError` (`413`); **`> 3_600` items, injected limit:** with production caps `MAX_SCHEDULES = 100`, `MAX_FORECAST_HINTS = 200`, and `months <= 12`, the `> 3_600` branch is unreachable, so the test injects a lower `MAX_FORECAST_ITEMS` and asserts `413` with zero partial totals; `410k`-char `remittance_information` hint label truncated to `256` code points; **`<= 3_600` items with multi-byte Unicode labels exceeding `4 MiB` UTF-8 bytes -> `413` with zero partial totals** (check the next chunk's UTF-8 length before extending the buffer, so the private buffer stays within `4 MiB`; assert on UTF-8 bytes rather than `str` length, and send that single measured buffer).
   - **Safety:** `repr()` redaction and fixed error strings.
2. **Views, API & backward compatibility (`tests/test_budget_analytics_views.py`, `tests/test_budget_analytics_api.py`, `tests/test_api_contract.py`, `tests/test_budget_contract.py`):**
   - Computed-result memo reuse (`_recurring_hints` / `projection_snapshot` not re-invoked across `recurring` -> `forecast` on unchanged snapshot, per `test_budget_analytics_views.py:640-642`), single read transaction, `snapshot_digest` (from `_projection`) and `schedules_version` (over all owner schedules/payments before currency filter, invariant to `?currency`/`?months`), `?currency` scoping of `summary` and `deduplications[]`, RBAC (`401`/`403`/cross-owner), flags (`404`), `HEAD -> 405`, `PRIVATE_HEADERS`, zero `load_config`/`credentials`/`urllib`/`_audit` calls, strict query validation (`months=0/13`, `today=...`, `currency=pln`), **R3-3 regression** (a bad query returns `400 budget_analytics_invalid_request`; bad stored data rejected by `_json` at `hestia/budget/analytics_views.py:60-66` returns `500 budget_analytics_data_error`), `413` HTTP responses (checking buffered response send does not emit partial JSON on overflow), and extending `test_all_ten_reads_are_private_local_bounded_and_match_wire_models` (`tests/test_budget_analytics_api.py:91-98`) to all 11 analytics reads.
   - **MEDIUM 8 & R2-4 OpenAPI compatibility tests:** Compare generated `0.26.0` OpenAPI against direct predecessor **`0.25.0`** (`dc03790`), asserting every pre-existing path/operation/component schema from `0.25.0` (including `quarantine`) is structurally unchanged apart from `info.version` (`0.26.0`), alongside `test_checked_in_openapi_matches_models` (`test_api_contract.py:47-53`). Update `tests/test_budget_contract.py:201-215` (`analytics_reads` includes `"forecast"`, `info.version == "0.26.0"`, and assert forecast-specific `400`/`409`/`413`/`500` response descriptions on `GET /api/budget/analytics/forecast`, including `409` description `"SQLite backend required: forecast requires HESTIA_PERSIST=sqlite."`).
3. **UI (`ui/src/budget/analytics/forecast.test.tsx`, `ui/src/budget/budget.test.tsx`):**
   - Synthetic fixtures only; tab gating, EN/PL disclaimer, horizon/currency switches, **`413` on initial unfiltered load (`101 PLN + 100 EUR`) keeps currency selector usable via account/schedule fallbacks and retries with `?currency=EUR` -> `200`**, `"Unmarked"` / `"Nieoznaczona"` M0 badge (only on overdue mortgage instalments), `"Account not up to date"` / `"Konto nieaktualne"` stale flag on recurring items **and `deduplications[]` rows**, `"Merged with instalment"` / `"Połączono z ratą"` dedup list, stale-account truncation notice, overdue schedule jump to `Mortgages`, and immediate state wipe on access loss.

## 11. Effort Estimate & Delivery Checklist

- **Effort estimate:**
  - **Backend PR (`Short`, zero migration):** Pure `hestia/budget/forecast.py` (`RULE_VERSION = 1`), `BudgetAnalyticsViews.forecast`, bounded UTF-8 buffer encoder in `analytics_api.py` / `web.py` routes, OpenAPI contract (`0.26.0`), and unit/views/API/contract tests.
  - **UI PR (`Short`):** OpenAPI TS typegen, `ui/src/budget/api.ts`, `ui/src/budget/analytics/model.ts`, `ui/src/budget/analytics/forecast.tsx`, 9th tab in `ui/src/budget/budget.tsx` + `budget.css`, and Vitest specs.
- **Delivery checklist (MEDIUM 9 + R2-4):**
  1. Register new models in `_ANALYTICS_MODELS` (`hestia/api_contract.py:2254-2266`), route and forecast-specific overrides in `_analytics_paths` (`api_contract.py:2915-2988`) and `_PATH_ROLES` (`api_contract.py:2286-2306`), bump `CONTRACT_VERSION` to `"0.26.0"` (`api_contract.py:76`), update `tests/test_budget_contract.py:201-215` & `tests/test_budget_analytics_api.py:91-98`, and regenerate `docs/api/openapi.json` (`python -m hestia.api_contract`).
  2. Run `cd ui && npm run gen:types` (`ui/package.json:16`) to regenerate `ui/src/api/openapi.ts` (`ui/src/api/types.ts:4`).
  3. Update `docs/BUDGET.md` (`docs/BUDGET.md:3-15, 238-240, 768-780`), `docs/BUDGET_2D.md` (`docs/BUDGET_2D.md:3-5, 400-447`), and update this document's `Status` banner to **Implemented** upon shipping.
  4. Review and update `ui/public/privacy-policy.html` and `ui/public/terms-of-service.html` to cover on-read upcoming-commitment estimates and heuristic duplicate hiding (`deduplications[]`) per `docs/BUDGET.md:13-14`.

## 12. v2 Option (Requires Schema Migration — Out of Scope for v1)

If future feedback shows exact-amount deduplication misses variable-rate mortgages or the owner wants manual overrides:
- **Explicit link/suppression table (migration `0020`):** Persist owner-managed links between `budget_mortgage_schedules` and recurring keys (plus reversible unmerge/snooze/dismiss flags supporting `§2(c)`) under CAS, replacing the `±5`-day exact-amount heuristic where explicitly linked.
- **Recurring amount bands (`recurring.py` `RULE_VERSION = 2`):** Group minor merchant price drift within an explicit band and project `last_amount` with an optional `[min, max]` range.
- **Discretionary envelope:** Optionally render enabled `BudgetCategoryLimit` ceilings in a separate non-summed section alongside committed totals.
