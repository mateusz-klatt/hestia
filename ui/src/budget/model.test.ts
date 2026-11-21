import { describe, expect, it } from "vitest";
import { account, component } from "./fixtures";
import type { BudgetAccount, BudgetJob, BudgetTransaction } from "./model";
import { accountLabel, formatAmount, freshness, hasActiveJob, idempotencyKey, latestJob, otherObservedCurrencies,
  presetRange, transactionDate, transactionText, validRange, when } from "./model";

function job(changes: Partial<BudgetJob> = {}): BudgetJob {
  return { id: "job", connection_id: "bank", account_id: null, operation: null, source: "manual", status: "queued",
    error: null, next_attempt: null, created_at: 1, started_at: null, finished_at: null, ...changes };
}

describe("exact decimal presentation", () => {
  it("keeps every digit of large amounts without float rounding", () => {
    expect(formatAmount("12345678901234567890.12345", "PLN", "en")).toBe("12,345,678,901,234,567,890.12345 PLN");
    expect(formatAmount("12345678901234567890.12345", "PLN", "pl").replace(/\s/g, " ")).toBe("12 345 678 901 234 567 890,12345 PLN");
    expect(formatAmount("0.1", "EUR", "en")).toBe("0.10 EUR");
    expect(formatAmount("7", "EUR", "en")).toBe("7.00 EUR");
  });
  it("applies the debit indicator and never shows a negative zero", () => {
    expect(formatAmount("12.30", "PLN", "en", "DBIT")).toBe("−12.30 PLN");
    expect(formatAmount("12.30", "PLN", "en", "CRDT")).toBe("12.30 PLN");
    expect(formatAmount("-12.30", "PLN", "en")).toBe("−12.30 PLN");
    expect(formatAmount("-12.30", "PLN", "en", "DBIT")).toBe("12.30 PLN");
    expect(formatAmount("0.00", "PLN", "en", "DBIT")).toBe("0.00 PLN");
  });
  it("shows an unparseable amount verbatim instead of guessing", () => {
    expect(formatAmount("1e5", "PLN", "en")).toBe("1e5 PLN");
  });
});

describe("labels, dates and ranges", () => {
  it("prefers product and masked suffix and never falls back to an identifier", () => {
    expect(accountLabel(account(), 1, "Account")).toBe("Account 2");
    const display = { product: "Konto Plus", currency: "PLN", masked_suffix: "1234", source: "offline_import",
      observed_at: 1, imported_at: 2 };
    expect(accountLabel(account({ display }), 0, "Account")).toBe("Konto Plus ••1234");
    expect(accountLabel(account({ display: { ...display, product: null } }), 0, "Account")).toBe("Account 1 ••1234");
    expect(accountLabel(account({ display: { ...display, masked_suffix: null } }), 0, "Account")).toBe("Konto Plus");
  });
  it("separates declared and observed balance currencies", () => {
    const display = { product: null, currency: "PLN", masked_suffix: null, source: "offline_import", observed_at: 1, imported_at: 2 };
    expect(otherObservedCurrencies(account({ display, observed_balance_currencies: ["PLN", "EUR"] }))).toEqual(["EUR"]);
    expect(otherObservedCurrencies(account({ observed_balance_currencies: ["PLN"] }))).toEqual(["PLN"]);
    const legacy: Partial<BudgetAccount> = account();
    delete legacy.observed_balance_currencies;
    expect(otherObservedCurrencies(legacy as BudgetAccount)).toEqual([]);
  });
  it("uses booking, then value, then transaction date and joins non-empty description lines", () => {
    const base: BudgetTransaction = { entry_reference: "r", amount: "1", currency: "PLN", credit_debit_indicator: "DBIT",
      status: "BOOK", booking_date: null, value_date: null, transaction_date: "2026-11-01",
      remittance_information: ["Shop", " ", "Card"], source: "api", observed_at: 1, api_observed_at: 1 };
    expect(transactionDate(base)).toBe("2026-11-01");
    expect(transactionDate({ ...base, value_date: "2026-11-02" })).toBe("2026-11-02");
    expect(transactionDate({ ...base, booking_date: "2026-11-03", value_date: "2026-11-02" })).toBe("2026-11-03");
    expect(transactionDate({ ...base, transaction_date: null })).toBeNull();
    expect(transactionText(base)).toBe("Shop · Card");
  });
  it("builds inclusive local presets and validates ranges", () => {
    const today = new Date(2026, 10, 21);
    expect(presetRange("30", today)).toEqual({ from: "2026-10-23", to: "2026-11-21" });
    expect(presetRange("90", today)).toEqual({ from: "2026-08-24", to: "2026-11-21" });
    expect(presetRange("year", today)).toEqual({ from: "2026-01-01", to: "2026-11-21" });
    expect(presetRange("all", today)).toEqual({ from: "", to: "" });
    expect(validRange({ from: "2026-11-01", to: "2026-11-01" })).toBe(true);
    expect(validRange({ from: "2026-11-02", to: "2026-11-01" })).toBe(false);
    expect(validRange({ from: "", to: "2026-11-01" })).toBe(true);
    expect(validRange({ from: "nonsense", to: "" })).toBe(false);
    expect(validRange({ from: "", to: "2026-1-1" })).toBe(false);
  });
  it("formats timestamps and reports freshness honestly", () => {
    expect(when(null, "en")).toBeNull();
    expect(when(Number.NaN, "en")).toBeNull();
    expect(when(1_795_000_000, "en")).toMatch(/2026/);
    expect(freshness(component)).toBe("never");
    expect(freshness({ ...component, last_success: 5, stale: false })).toBe("fresh");
    expect(freshness({ ...component, last_success: 5, stale: true })).toBe("stale");
    expect(freshness({ ...component, suspended: true })).toBe("suspended");
  });
});

describe("jobs and keys", () => {
  it("finds the newest job per connection and detects active work", () => {
    const jobs = [job({ id: "new", status: "running" }), job({ id: "old", status: "updated" }),
      job({ id: "other", connection_id: "other" })];
    expect(latestJob(jobs, "bank")?.id).toBe("new");
    expect(latestJob(jobs, "missing")).toBeNull();
    expect(hasActiveJob(jobs)).toBe(true);
    expect(hasActiveJob([job({ status: "deferred" })])).toBe(false);
  });
  it("creates a server-valid key per intent", () => {
    expect(idempotencyKey()).toMatch(/^[A-Za-z0-9_-]{16,128}$/);
    expect(idempotencyKey()).not.toBe(idempotencyKey());
    expect(idempotencyKey(() => "fixed-key-for-test-0001")).toBe("fixed-key-for-test-0001");
  });
});
