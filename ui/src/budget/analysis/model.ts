// Spending analysis (2D) helpers. Every total is computed by the server per currency and arrives as a decimal
// STRING; the browser never adds, converts or mixes amounts. Month keys are "YYYY-MM" in the owner's calendar.
import type { BudgetCapabilities } from "../model";

// Provisional shape until the server contract is generated into openapi.ts.
export interface CurrencySummary {
  currency: string;
  income: string;
  spending: string;
  net: string;
  income_count: number;
  spending_count: number;
}

export interface MonthSummary {
  month: string;
  accounts: { selected: number; excluded: number };
  currencies: CurrencySummary[];
  not_counted: { pending: number; uncertain: number; unresolved: number };
  partial: boolean;
  stale_account_ids: string[];
}

const MONTH = /^(\d{4})-(0[1-9]|1[0-2])$/;

/** True only when the server explicitly offers the analysis views. */
export function hasAnalysis(capabilities: BudgetCapabilities | null): boolean {
  return capabilities !== null && (capabilities as Record<string, unknown>).analysis === true;
}

export function isMonth(value: string): boolean {
  return MONTH.test(value);
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
