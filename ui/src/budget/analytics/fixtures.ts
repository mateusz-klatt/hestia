// Synthetic analytics fixtures only; no real account, transaction or bank data.
import { component } from "../fixtures";
import type { AnalyticsCategory, AnalyticsInbox, AnalyticsInboxConsentItem, AnalyticsInboxItem,
  AnalyticsInboxMarkSaved, AnalyticsInboxMortgageItem, AnalyticsInboxSpendingLimitItem,
  AnalyticsMortgageReminder, AnalyticsMortgages, AnalyticsMortgageSchedule,
  AnalyticsOverview, AnalyticsRecurring, AnalyticsRecurringHint, AnalyticsSpendingLimit, AnalyticsSpendingLimitStatus,
  AnalyticsSpendingLimits, AnnotationSaved, MortgagePaymentSaved, MortgageScheduleSaved, SpendingLimitSaved,
  TransferDecisionResult, AnalyticsTotals, AnalyticsTransaction, AnalyticsTransactions, TransferLeg, TransferList,
  TransferPair } from "./model";

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
