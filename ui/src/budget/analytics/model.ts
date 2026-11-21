// Spending analytics (2D) helpers. Every total is computed by the server per currency and arrives as a decimal
// STRING; the browser never adds, subtracts, converts or mixes amounts. Periods are "YYYY-MM" or "YYYY".
import type { BudgetCapabilities } from "../model";

// Provisional shapes until the server contract is generated into openapi.ts.
export interface AnalyticsTotals {
  currency: string;
  debits: string;
  credits: string;
  spending_gross: string;
  refunds: string;
  net_spending: string;
  income: string;
  unclassified_credit: string;
  internal_in: string;
  internal_out: string;
}

export interface AnalyticsMonth { month: string; partial: boolean; totals: AnalyticsTotals[] }

export interface AnalyticsCategoryTotal {
  currency: string;
  category_id: string | null;
  name: string | null;
  amount: string;
  count: number;
}

export interface AnalyticsScope {
  selected: number;
  excluded: number;
  partial: boolean;
  stale_account_ids: string[];
  pending: number;
  uncertain: number;
  unresolved: number;
}

export interface AnalyticsTransferSummary { suggested: number; review: number; accepted: number }

export interface AnalyticsOverview {
  period: string;
  currency: string | null;
  totals: AnalyticsTotals[];
  months: AnalyticsMonth[];
  categories: AnalyticsCategoryTotal[];
  scope: AnalyticsScope;
  transfer_summary: AnalyticsTransferSummary;
}

const MONTH = /^(\d{4})-(0[1-9]|1[0-2])$/;
const ZERO = /^-?0+(?:\.0+)?$/;

/** True only when the server explicitly offers the analytics views. */
export function hasAnalytics(capabilities: BudgetCapabilities | null): boolean {
  return capabilities !== null && (capabilities as Record<string, unknown>).analytics === true;
}

export function isMonth(value: string): boolean {
  return MONTH.test(value);
}

/** A decimal string equal to zero; a textual check, never a numeric conversion. */
export function isZero(amount: string): boolean {
  return ZERO.test(amount);
}

/** The owner's current calendar month. */
export function currentMonth(now: Date = new Date()): string {
  return `${String(now.getFullYear()).padStart(4, "0")}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

/** The month `delta` months away; an invalid key is returned unchanged. */
export function shiftMonth(month: string, delta: number): string {
  const match = MONTH.exec(month);
  if (match === null) return month;
  const index = Number(match[1]) * 12 + Number(match[2]) - 1 + delta;
  return `${String(Math.floor(index / 12)).padStart(4, "0")}-${String((index % 12) + 1).padStart(2, "0")}`;
}

/** "listopad 2026" / "November 2026" in the page locale; an invalid key is shown as it is. */
export function monthLabel(month: string, locale: string): string {
  const match = MONTH.exec(month);
  if (match === null) return month;
  const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, 15));
  return new Intl.DateTimeFormat(locale, { month: "long", year: "numeric", timeZone: "UTC" }).format(date);
}
