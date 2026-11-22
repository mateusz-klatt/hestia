// Synthetic analytics fixtures only; no real account, transaction or bank data.
import { component } from "../fixtures";
import type { AnalyticsCategory, AnalyticsForecast, AnalyticsForecastCurrencyTotal, AnalyticsForecastDeduplication,
  AnalyticsForecastItem, AnalyticsInbox, AnalyticsInboxConsentItem, AnalyticsInboxItem,
  AnalyticsInboxMarkSaved, AnalyticsInboxMortgageItem, AnalyticsInboxSpendingLimitItem,
  AnalyticsMonthlyBudget, AnalyticsMonthlyBudgetCategoryLimit, AnalyticsMonthlyBudgetCategorySpend,
  AnalyticsMonthlyBudgetCharts, AnalyticsMonthlyBudgetExpectedIncome, AnalyticsMonthlyBudgetPace,
  AnalyticsMonthlyBudgetPassThrough, AnalyticsMonthlyBudgetSeries, AnalyticsMortgageReminder, AnalyticsMortgages,
  AnalyticsMortgageSchedule, AnalyticsOverview, AnalyticsQuarantine, AnalyticsQuarantineAccount,
  AnalyticsQuarantineItem, AnalyticsRecurring, AnalyticsRecurringHint, AnalyticsSpendingLimit,
  AnalyticsSpendingLimitStatus, AnalyticsSpendingLimits, AnnotationSaved, MortgagePaymentSaved,
  MortgageScheduleSaved, SpendingLimitSaved, TransferDecisionResult, AnalyticsTotals, AnalyticsTransaction,
  AnalyticsTransactions, TransferLeg, TransferList, TransferPair } from "./model";

export function totals(currency: string, changes: Partial<AnalyticsTotals> = {}): AnalyticsTotals {
  return {
    currency, gross_debits: "0", gross_credits: "0", expenses: "0", income: "0", refunds: "0", unclassified_credit: "0",
    excluded_debits: "0", excluded_credits: "0", transfer_debits: "0", transfer_credits: "0", net_spending: "0", ...changes,
  };
}

const fresh = { ...component, stale: false, state: "fresh", last_success: 1 };

export function overviewOf(period: string, changes: Partial<AnalyticsOverview> = {}): AnalyticsOverview {
  return {
    period, start: `${period}-01`, end: `${period}-30`, snapshot_digest: "synthetic-digest",
    scope: { selected_accounts: 1, excluded_accounts: 0, uncertain_count: 0, unresolved_count: 0, import_unresolved_count: 0,
      freshness: [{ account_id: "acc-1", transactions: fresh, balances: fresh }] },
    currencies: [totals("PLN", { expenses: "40.5", net_spending: "40.5", income: "100" })],
    months: [], categories: [],
    transfer_summary: { suggested: 0, accepted: 0, review: 0, rejected: 0, boundary: 0, scan_limited: false },
    ...changes,
  };
}

export const staleFreshness = { ...component, stale: true, state: "stale", last_success: 1 };

export function entry(reference: string, changes: Partial<AnalyticsTransaction> = {}): AnalyticsTransaction {
  return {
    account_id: "acc-1", entry_reference: reference, fingerprint: `fp-${reference}`, transfer_fingerprint: `tfp-${reference}`,
    amount: "12.5", currency: "PLN", credit_debit_indicator: "DBIT", status: "BOOK", booking_date: "2026-11-03",
    value_date: null, transaction_date: null, remittance_information: [`Synthetic shop ${reference}`], source: "api",
    observed_at: 1, api_observed_at: 1, annotation: { version: 0, mode: "auto", category_id: null, kind: "auto", stale: false },
    metadata: null,
    effective: { kind: "expense", category_id: null, source: "default", rule_id: null },
    transfer: { status: "none", decision_id: null, candidate_count: 0, ambiguous: false }, ...changes,
  };
}

export const categoryList: AnalyticsCategory[] = [
  { id: "cat-food", name: "Synthetic Food", archived: false, version: 1, created_at: 1, updated_at: 1 },
  { id: "cat-old", name: "Synthetic Old", archived: true, version: 3, created_at: 1, updated_at: 2 },
];

export function leg(account: string, reference: string, changes: Partial<TransferLeg> = {}): TransferLeg {
  return { account_id: account, entry_reference: reference, fingerprint: `pfp-${reference}`, amount: "1200", currency: "PLN",
    date: "2026-11-10", selected: true, ...changes };
}

export function pair(changes: Partial<TransferPair> = {}): TransferPair {
  return { id: null, version: 0, status: "suggested", source: "heuristic", debit: leg("acc-1", "out-1"),
    credit: leg("acc-2", "in-1", { date: "2026-11-11" }), date_distance: 1, ambiguous: false, stale: false, boundary: false,
    ...changes };
}

export function entriesOf(period: string, rows: AnalyticsTransaction[] = [], cursor: string | null = null): AnalyticsTransactions {
  return { period, start: `${period}-01`, end: `${period}-30`, snapshot_digest: "synthetic-digest", transactions: rows,
    filtered_total: rows.length + (cursor === null ? 0 : 10), next_cursor: cursor };
}

export function transfersOf(period: string, rows: TransferPair[] = [], cursor: string | null = null, limited = false): TransferList {
  return { period, start: `${period}-01`, end: `${period}-30`, snapshot_digest: "synthetic-digest", transfers: rows,
    filtered_total: rows.length, next_cursor: cursor, scan_limited: limited };
}

export function annotationOf(changes: Partial<AnnotationSaved> = {}): AnnotationSaved {
  return { account_id: "acc-1", entry_reference: "e1", version: 1, mode: "manual", category_id: "cat-food", kind: "expense",
    ledger_fingerprint: "fp-e1", changed: true, updated_at: 1, ...changes };
}

export function decisionOf(changes: Partial<TransferDecisionResult> = {}): TransferDecisionResult {
  return { id: "dec-new", version: 1, status: "accepted", source: "heuristic", changed: true, rule_version: 1,
    debit_account_id: "acc-1", debit_ref: "out-1", debit_fingerprint: "pfp-out-1", credit_account_id: "acc-2", credit_ref: "in-1",
    credit_fingerprint: "pfp-in-1", created_at: 1, updated_at: 1, ...changes };
}

export function recurringHint(changes: Partial<AnalyticsRecurringHint> = {}): AnalyticsRecurringHint {
  return {
    label: "Synthetic Stream", label_source: "counterparty_name", amount: "49.99", currency: "PLN", cadence: "monthly",
    occurrence_count: 3, distinct_months: 3, first_date: "2026-09-05", last_date: "2026-11-05",
    min_gap_days: 30, max_gap_days: 31, account_ids: ["acc-1"],
    evidence: [
      { account_id: "acc-1", entry_reference: "rec-1", date: "2026-09-05" },
      { account_id: "acc-1", entry_reference: "rec-2", date: "2026-10-05" },
      { account_id: "acc-1", entry_reference: "rec-3", date: "2026-11-05" },
    ],
    evidence_truncated: false, ...changes,
  };
}

export function recurringOf(hints: AnalyticsRecurringHint[] = [], changes: Partial<AnalyticsRecurring> = {}): AnalyticsRecurring {
  return {
    snapshot_digest: "synthetic-digest", rule_version: 1, total_hints: hints.length, truncated: false, hints, ...changes,
  };
}

export function quarantineItem(changes: Partial<AnalyticsQuarantineItem> = {}): AnalyticsQuarantineItem {
  return {
    bucket: "uncertain", account_id: "acc-1", selected: true, entry_reference: "synthetic-private-reference",
    date: "2026-11-20", status: "BOOK", reason: "reference_conflict", source: "api", observed_at: 1_795_000_000,
    ...changes,
  };
}

export function quarantineAccount(changes: Partial<AnalyticsQuarantineAccount> = {}): AnalyticsQuarantineAccount {
  return {
    account_id: "acc-1", selected: true, uncertain_count: 0, unresolved_count: 0, import_unresolved_count: 0,
    total_count: 0, ...changes,
  };
}

export function quarantineOf(items: AnalyticsQuarantineItem[] = [], changes: Partial<AnalyticsQuarantine> = {}): AnalyticsQuarantine {
  const uncertainCount = items.filter((item) => item.bucket === "uncertain").length;
  const unresolvedCount = items.filter((item) => item.bucket === "unresolved").length;
  const importCount = items.filter((item) => item.bucket === "import_unresolved").length;
  return {
    rule_version: 1,
    summary: {
      uncertain_count: uncertainCount, unresolved_count: unresolvedCount, import_unresolved_count: importCount,
      total_count: items.length,
    },
    accounts: [quarantineAccount({
      uncertain_count: uncertainCount, unresolved_count: unresolvedCount, import_unresolved_count: importCount,
      total_count: items.length,
    })],
    items_cap: 200, truncated: false, items, ...changes,
  };
}

export function spendingLimit(changes: Partial<AnalyticsSpendingLimit> = {}): AnalyticsSpendingLimit {
  return {
    id: "lim-1", category_id: "cat-food", currency: "PLN", monthly_limit: "500", warning_percent: 80,
    enabled: true, version: 1, created_at: 1, updated_at: 1, ...changes,
  };
}

export function spendingLimitStatus(changes: Partial<AnalyticsSpendingLimitStatus> = {}): AnalyticsSpendingLimitStatus {
  return {
    period: "2026-11", category_id: "cat-food", name: "Synthetic Food", currency: "PLN",
    monthly_limit: "500", expenses: "320.5", refunds: "0", net_spending: "320.5", remaining: "179.5", over_by: "0",
    percent_used_floor: 64, warning_percent: 80, status: "within", archived: false, ...changes,
  };
}

export function spendingLimitsOf(period: string, limits: AnalyticsSpendingLimit[] = [],
  statuses: AnalyticsSpendingLimitStatus[] = [],
  changes: Partial<AnalyticsSpendingLimits> = {}): AnalyticsSpendingLimits {
  return {
    period, start: `${period}-01`, end: `${period}-30`, snapshot_digest: "synthetic-digest",
    limits_version: "synthetic-limits-v1", rule_version: 1, limits, statuses, ...changes,
  };
}

export function spendingLimitSaved(changes: Partial<SpendingLimitSaved> = {}): SpendingLimitSaved {
  return { ...spendingLimit(), changed: true, ...changes };
}

export function mortgageSchedule(changes: Partial<AnalyticsMortgageSchedule> = {}): AnalyticsMortgageSchedule {
  return {
    id: "mtg-1", label: "Synthetic Home Loan", amount: "2450.5", currency: "PLN", due_day: 10,
    start_date: "2026-01-01", end_date: null, remind_days_before: 5, destination_kind: "owner_inbox",
    enabled: true, version: 1, paid_months: ["2026-10"], created_at: 1, updated_at: 1, ...changes,
  };
}

export function mortgageReminder(changes: Partial<AnalyticsMortgageReminder> = {}): AnalyticsMortgageReminder {
  return {
    schedule_id: "mtg-1", label: "Synthetic Home Loan", amount: "2450.5", currency: "PLN",
    due_date: "2026-11-10", days_until_due: 3, status: "due_soon", reminder_active: true,
    destination_kind: "owner_inbox", freshness_stale: false, ledger_as_of: "2026-11-06", rule_version: 1,
    ...changes,
  };
}

export function mortgagesOf(schedules: AnalyticsMortgageSchedule[] = [],
  reminders: AnalyticsMortgageReminder[] = [],
  changes: Partial<AnalyticsMortgages> = {}): AnalyticsMortgages {
  return {
    evaluation_date: "2026-11-07", evaluation_timezone: "UTC", ledger_as_of: "2026-11-06",
    schedules_version: "synthetic-schedules-v1", rule_version: 1, schedules, reminders, ...changes,
  };
}

export function mortgageSaved(changes: Partial<MortgageScheduleSaved> = {}): MortgageScheduleSaved {
  return { ...mortgageSchedule(), changed: true, ...changes };
}

export function mortgagePaymentSaved(changes: Partial<MortgagePaymentSaved> = {}): MortgagePaymentSaved {
  return {
    schedule_id: "mtg-1", version: 2, month: "2026-11", paid: true, paid_months: ["2026-10", "2026-11"],
    changed: true, ...changes,
  };
}

export function forecastOf(changes: Partial<AnalyticsForecast> = {}): AnalyticsForecast {
  return {
    evaluation_date: "2026-11-22", evaluation_timezone: "UTC", horizon_months: 3,
    start_month: "2026-11", end_month: "2027-01", ledger_as_of: "2026-11-21", freshness_stale: false,
    stale_account_ids: [], stale_account_ids_truncated: false, snapshot_digest: "synthetic-digest",
    schedules_version: "synthetic-schedules-v1", rule_version: 1,
    summary: {
      selected_accounts: 1, excluded_accounts: 0, stale_accounts: 0, overdue_mortgage_schedule_count: 0,
      lapsed_recurring_count: 0, deduped_recurring_count: 0, stale_recurring_count: 0,
    },
    totals: [], deduplications: [],
    months: ["2026-11", "2026-12", "2027-01"].map((month) => ({ month, currencies: [], items: [] })),
    ...changes,
  };
}

export function forecastItem(changes: Partial<AnalyticsForecastItem> = {}): AnalyticsForecastItem {
  return {
    source: "mortgage", schedule_id: "mtg-1", label: "Synthetic Home Loan", amount: "2450.5", currency: "PLN",
    expected_date: "2026-11-10", confidence: "scheduled", past_due: true, account_stale: false, ...changes,
  };
}

export function forecastCurrencyTotal(changes: Partial<AnalyticsForecastCurrencyTotal> = {}): AnalyticsForecastCurrencyTotal {
  return { currency: "PLN", total: "2450.5", scheduled_total: "2450.5", estimated_total: "0", ...changes };
}

export function forecastDeduplication(changes: Partial<AnalyticsForecastDeduplication> = {}): AnalyticsForecastDeduplication {
  return {
    schedule_id: "mtg-1", schedule_label: "Synthetic Home Loan", recurring_label: "Synthetic Bank Mortgage",
    amount: "2450.5", currency: "PLN", matched_due_date: "2026-10-10", last_observed_date: "2026-10-11",
    day_distance: 1, account_stale: true, ...changes,
  };
}

export function inboxConsentItem(changes: Partial<AnalyticsInboxConsentItem> = {}): AnalyticsInboxConsentItem {
  return {
    kind: "consent_renewal",
    subject_id: "a".repeat(64),
    milestone: "g1:exp1800000000:lead:7",
    read: false,
    read_at: null,
    ref: { connection_id: "bank" },
    params: { status: "due_soon", valid_until: 1_800_000_000, days_remaining: 5, attention: null, paused: false },
    ...changes,
  };
}

export function inboxMortgageItem(changes: Partial<AnalyticsInboxMortgageItem> = {}): AnalyticsInboxMortgageItem {
  return {
    kind: "mortgage_due",
    subject_id: "b".repeat(64),
    milestone: "2026-11:overdue",
    read: false,
    read_at: null,
    ref: { schedule_id: "mtg-1" },
    params: { status: "overdue", due_date: "2026-11-10", days_until_due: -3, currency: "PLN", freshness_stale: false },
    ...changes,
  };
}

export function inboxSpendingLimitItem(
  changes: Partial<AnalyticsInboxSpendingLimitItem> = {},
): AnalyticsInboxSpendingLimitItem {
  return {
    kind: "spending_limit",
    subject_id: "c".repeat(64),
    milestone: "2026-11:exceeded",
    read: false,
    read_at: null,
    ref: { category_id: "cat-food", currency: "PLN" },
    params: { status: "exceeded", period: "2026-11", currency: "PLN", warning_percent: 80, percent_used_floor: 112 },
    ...changes,
  };
}

export function inboxOf(items: AnalyticsInboxItem[] = [], changes: Partial<AnalyticsInbox> = {}): AnalyticsInbox {
  const unread = items.filter((item) => !item.read).length;
  const readCount = items.filter((item) => item.read).length;
  return {
    evaluation_date: "2026-11-21",
    evaluation_timezone: "UTC",
    evaluated_at: 1_795_176_000,
    reads_revision: 0,
    reads_version: "0".repeat(64),
    reads_count: readCount,
    reads_cap: 500,
    unread_count: unread,
    rule_version: 1,
    items,
    ...changes,
  };
}

export function inboxMarkSaved(changes: Partial<AnalyticsInboxMarkSaved> = {}): AnalyticsInboxMarkSaved {
  return {
    read: true,
    changed: true,
    changed_count: 1,
    reads_revision: 1,
    reads_version: "1".repeat(64),
    reads_count: 1,
    reads_cap: 500,
    ...changes,
  };
}

export function monthlyBudgetCategoryLimit(
  changes: Partial<AnalyticsMonthlyBudgetCategoryLimit> = {},
): AnalyticsMonthlyBudgetCategoryLimit {
  return {
    monthly_limit: "500",
    remaining: "179.5",
    over_by: "0",
    percent_used_floor: "64",
    warning_percent: 80,
    status: "within",
    ...changes,
  };
}

export function monthlyBudgetCategorySpend(
  changes: Partial<AnalyticsMonthlyBudgetCategorySpend> = {},
): AnalyticsMonthlyBudgetCategorySpend {
  return {
    category_id: "cat-food",
    name: "Synthetic Food",
    archived: false,
    expenses: "320.5",
    refunds: "0",
    net_spending: "320.5",
    limit: null,
    ...changes,
  };
}

export function monthlyBudgetPassThrough(
  changes: Partial<AnalyticsMonthlyBudgetPassThrough> = {},
): AnalyticsMonthlyBudgetPassThrough {
  return {
    rent_category_id: "cat-rent",
    mortgage_category_id: "cat-mortgage",
    rent_received: "2000",
    mortgage_debits: "3200",
    mortgage_refunds: "0",
    unapplied_mortgage_refunds: "0",
    matched: "2000",
    unapplied_rent: "0",
    uncovered_mortgage: "1200",
    ...changes,
  };
}

export function monthlyBudgetExpectedIncome(
  changes: Partial<AnalyticsMonthlyBudgetExpectedIncome> = {},
): AnalyticsMonthlyBudgetExpectedIncome {
  return {
    status: "none",
    total: "0",
    items: [],
    ...changes,
  };
}

export function monthlyBudgetPace(
  changes: Partial<AnalyticsMonthlyBudgetPace> = {},
): AnalyticsMonthlyBudgetPace {
  return {
    basis: "received",
    budget: "5000",
    status: "within_budget",
    daily_rate: { numerator: "320.5", denominator: 22 },
    projected_month_spend: { numerator: "9615", denominator: 22 },
    exhaustion_day: null,
    ...changes,
  };
}

export function monthlyBudgetOf(changes: Partial<AnalyticsMonthlyBudget> = {}): AnalyticsMonthlyBudget {
  return {
    period: "2026-11",
    start: "2026-11-01",
    end: "2026-11-30",
    cutoff: "2026-11-22",
    current_month: "2026-11",
    evaluated_at: 1_795_348_800,
    evaluation_date: "2026-11-22",
    evaluation_timezone: "Europe/Warsaw",
    currency: "PLN",
    available_currencies: ["PLN"],
    configuration: { status: "ready", source: "bootstrap" },
    coverage: {
      ledger_as_of: 1_795_345_200,
      stale_selected_accounts: 0,
      purchase_date_fallback_count: 0,
      pending_transfer_rows: 0,
      transfer_scan_limited: false,
      provisional: false,
      has_activity: true,
    },
    summary: {
      start_budget: "5000",
      spent: "320.5",
      remaining: "4679.5",
    },
    expected_income: monthlyBudgetExpectedIncome(),
    pace: monthlyBudgetPace(),
    category_spend: [monthlyBudgetCategorySpend()],
    pass_through: null,
    card_exposure: { status: "unavailable", amount: null, as_of_date: null },
    ...changes,
  };
}

export function monthlyBudgetSeriesOf(
  changes: Partial<AnalyticsMonthlyBudgetSeries> = {},
): AnalyticsMonthlyBudgetSeries {
  return {
    daily: [
      { date: "2026-11-01", cumulative_spent: "0" },
      { date: "2026-11-05", cumulative_spent: "120.5" },
      { date: "2026-11-10", cumulative_spent: "320.5" },
    ],
    months: [
      {
        period: "2026-06", has_activity: false, is_current_month: false, income_estimate_partial: false,
        provisional: false, start_budget: "0", spent: "0", categories: [],
      },
      {
        period: "2026-07", has_activity: true, is_current_month: false, income_estimate_partial: false,
        provisional: false, start_budget: "4500", spent: "2100",
        categories: [{ category_id: "cat-food", net_spending: "2100" }],
      },
      {
        period: "2026-08", has_activity: true, is_current_month: false, income_estimate_partial: false,
        provisional: false, start_budget: "4800", spent: "2500",
        categories: [{ category_id: "cat-food", net_spending: "2500" }],
      },
      {
        period: "2026-09", has_activity: true, is_current_month: false, income_estimate_partial: false,
        provisional: false, start_budget: "4900", spent: "2600",
        categories: [{ category_id: "cat-food", net_spending: "2600" }],
      },
      {
        period: "2026-10", has_activity: true, is_current_month: false, income_estimate_partial: false,
        provisional: false, start_budget: "5000", spent: "2400",
        categories: [{ category_id: "cat-food", net_spending: "2400" }],
      },
      {
        period: "2026-11", has_activity: true, is_current_month: true, income_estimate_partial: false,
        provisional: false, start_budget: "5000", spent: "320.5",
        categories: [{ category_id: "cat-food", net_spending: "320.5" }],
      },
    ],
    category_labels: [{ category_id: "cat-food", name: "Synthetic Food", archived: false }],
    ...changes,
  };
}

export function monthlyBudgetChartsOf(
  budgetChanges: Partial<AnalyticsMonthlyBudget> = {},
  seriesChanges?: Partial<AnalyticsMonthlyBudgetSeries> | null,
): AnalyticsMonthlyBudgetCharts {
  const budget = monthlyBudgetOf(budgetChanges);
  if (seriesChanges === null || budget.configuration.status === "unconfigured") {
    return { budget, series: null };
  }
  return { budget, series: monthlyBudgetSeriesOf(seriesChanges) };
}
