// Synthetic analytics fixtures only; no real account, transaction or bank data.
import type { AnalyticsOverview, AnalyticsTotals } from "./model";

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
