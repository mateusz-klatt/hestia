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

/** The owner's explicit kind choice; "auto" returns the entry to rules and defaults. */
export type KindChoice = "auto" | "expense" | "income" | "refund" | "excluded";
export const KIND_CHOICES: readonly KindChoice[] = ["auto", "expense", "income", "refund", "excluded"];

export interface AnalyticsAnnotation {
  mode: "auto" | "manual";
  kind: KindChoice;
  category_id: string | null;
  version: number;
  /** The bank entry changed after this decision was made; it needs the owner's look. */
  stale: boolean;
}

export interface AnalyticsTransaction {
  account_id: string;
  entry_reference: string;
  fingerprint: string;
  amount: string;
  currency: string;
  credit_debit_indicator: "CRDT" | "DBIT";
  status: string;
  booking_date: string | null;
  value_date: string | null;
  transaction_date: string | null;
  remittance_information: string[];
  counterparty_name: string | null;
  annotation: AnalyticsAnnotation | null;
  effective: { category_id: string | null; kind: string; source: "manual" | "rule" | "uncategorised" };
  transfer_state: "none" | "suggested" | "accepted" | "review" | "rejected";
}

export interface AnalyticsTransactions { transactions: AnalyticsTransaction[]; total: number; next_cursor: string | null }

export interface AnalyticsCategory { id: string; name: string; archived: boolean; version: number }

export interface TransactionFilter { currency: string; category: string; kind: string; query: string }
export const NO_FILTER: TransactionFilter = { currency: "", category: "", kind: "", query: "" };
/** Filter value for entries without a category (the server's explicit uncategorised bucket). */
export const UNCATEGORISED = "uncategorised";

export interface AnnotationRequest {
  account_id: string;
  entry_reference: string;
  mode: "auto" | "manual";
  category_id: string | null;
  kind: KindChoice;
  /** 0 creates the first annotation; otherwise the version the owner saw. */
  expected_version: number;
}

/** The owner's category choice in the editor: automatic (rules), explicitly none, or a category id. */
export type CategoryChoice = { mode: "auto" } | { mode: "manual"; category_id: string | null };

export function annotationRequest(row: AnalyticsTransaction, category: CategoryChoice, kind: KindChoice): AnnotationRequest {
  return {
    account_id: row.account_id, entry_reference: row.entry_reference, mode: category.mode,
    category_id: category.mode === "manual" ? category.category_id : null, kind,
    expected_version: row.annotation?.version ?? 0,
  };
}

/** The editor's starting choice: what the owner set before, or automatic. */
export function currentChoice(row: AnalyticsTransaction): CategoryChoice {
  const annotation = row.annotation;
  return annotation === null || annotation.mode === "auto" ? { mode: "auto" } : { mode: "manual", category_id: annotation.category_id };
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
