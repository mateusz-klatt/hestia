// Synthetic analytics fixtures only; no real account, transaction or bank data.
import { component } from "../fixtures";
import type { AnalyticsCategory, AnalyticsOverview, AnalyticsRecurring, AnalyticsRecurringHint, AnnotationSaved,
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
