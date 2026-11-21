// Synthetic analytics fixtures only; no real account, transaction or bank data.
import { component } from "../fixtures";
import type { AnalyticsCategory, AnalyticsOverview, AnalyticsTotals, AnalyticsTransaction, AnalyticsTransactions, TransferLeg, TransferList,
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
    observed_at: 1, api_observed_at: 1, annotation: null, metadata: null,
    effective: { kind: "expense", category_id: null, source: "default", rule_id: null },
    transfer: { status: "none", decision_id: null, candidate_count: 0, ambiguous: false }, ...changes,
  };
}

export const categoryList: AnalyticsCategory[] = [
  { id: "cat-food", name: "Synthetic Food", archived: false, version: 1 },
  { id: "cat-old", name: "Synthetic Old", archived: true, version: 3 },
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
