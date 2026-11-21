// Synthetic analytics fixtures only; no real account, transaction or bank data.
import type { AnalyticsCategory, AnalyticsOverview, AnalyticsTotals, AnalyticsTransaction } from "./model";

export function totals(currency: string, changes: Partial<AnalyticsTotals> = {}): AnalyticsTotals {
  return {
    currency, debits: "0", credits: "0", spending_gross: "0", refunds: "0", net_spending: "0", income: "0",
    unclassified_credit: "0", internal_in: "0", internal_out: "0", ...changes,
  };
}

export function overviewOf(period: string, changes: Partial<AnalyticsOverview> = {}): AnalyticsOverview {
  return {
    period, currency: null,
    totals: [totals("PLN", { spending_gross: "40.5", net_spending: "40.5", income: "100" })],
    months: [], categories: [],
    scope: { selected: 1, excluded: 0, partial: false, stale_account_ids: [], pending: 0, uncertain: 0, unresolved: 0 },
    transfer_summary: { suggested: 0, review: 0, accepted: 0 },
    ...changes,
  };
}

export function entry(reference: string, changes: Partial<AnalyticsTransaction> = {}): AnalyticsTransaction {
  return {
    account_id: "acc-1", entry_reference: reference, fingerprint: `fp-${reference}`, amount: "12.5", currency: "PLN",
    credit_debit_indicator: "DBIT", status: "BOOK", booking_date: "2026-11-03", value_date: null, transaction_date: null,
    remittance_information: [`Synthetic shop ${reference}`], counterparty_name: null, annotation: null,
    effective: { category_id: null, kind: "expense", source: "uncategorised" }, transfer_state: "none", ...changes,
  };
}

export const categoryList: AnalyticsCategory[] = [
  { id: "cat-food", name: "Synthetic Food", archived: false, version: 1 },
  { id: "cat-old", name: "Synthetic Old", archived: true, version: 3 },
];
