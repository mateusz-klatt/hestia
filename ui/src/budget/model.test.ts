import { describe, expect, it } from "vitest";
import { account, component, componentLimit, connection, sharedLimit } from "./fixtures";
import type { BudgetAccount, BudgetJob, BudgetTransaction } from "./model";
import { OPEN_FLOW, READY_FLOW, accountLabel, consentIntent, effectiveState, safeAuthorizationUrl, formatAmount, formatEstimate, freshness, hasActiveJob, idempotencyKey, isAccessLoss,
  isComponentLimit, latestJob, limitState, newer, otherObservedCurrencies, presetRange, transactionDate,
  transactionText, validRange, when } from "./model";

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
  it("rounds exact decimal estimates to 2 fraction digits half away from zero without float math", () => {
    const pl = (amount: string) => formatEstimate(amount, "PLN", "pl").replace(/\s/g, " ");
    expect(pl("54.335")).toBe("54,34 PLN");
    expect(pl("14079.7625")).toBe("14 079,76 PLN");
    expect(pl("3724.0225")).toBe("3 724,02 PLN");
    expect(pl("-1.005")).toBe("−1,01 PLN");
    expect(pl("0.004")).toBe("0,00 PLN");
    expect(pl("-0.004")).toBe("0,00 PLN");
    expect(pl("-0.005")).toBe("−0,01 PLN");
    expect(pl("0.005")).toBe("0,01 PLN");
    expect(pl("10")).toBe("10,00 PLN");
    expect(pl("9.995")).toBe("10,00 PLN");
    expect(pl("-9.995")).toBe("−10,00 PLN");
    expect(pl("12345678901234567890.125")).toBe("12 345 678 901 234 567 890,13 PLN");

    expect(formatEstimate("54.335", "PLN", "en")).toBe("54.34 PLN");
    expect(formatEstimate("14079.7625", "PLN", "en")).toBe("14,079.76 PLN");
    expect(formatEstimate("3724.0225", "PLN", "en")).toBe("3,724.02 PLN");
    expect(formatEstimate("-1.005", "PLN", "en")).toBe("−1.01 PLN");
    expect(formatEstimate("0.004", "PLN", "en")).toBe("0.00 PLN");
    expect(formatEstimate("-0.004", "PLN", "en")).toBe("0.00 PLN");
    expect(formatEstimate("10", "PLN", "en")).toBe("10.00 PLN");
    expect(formatEstimate("12345678901234567890.125", "PLN", "en")).toBe("12,345,678,901,234,567,890.13 PLN");
    expect(formatEstimate("1e5", "PLN", "en")).toBe("1e5 PLN");
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

describe("access loss and version-aware merges", () => {
  it("recognises lost principal and disabled feature only", () => {
    expect(isAccessLoss({ ok: false, status: 401, error: "x" })).toBe(true);
    expect(isAccessLoss({ ok: false, status: 403, error: "x" })).toBe(true);
    expect(isAccessLoss({ ok: false, status: 404, error: "budget_disabled" })).toBe(true);
    expect(isAccessLoss({ ok: false, status: 404, error: "budget_not_found" })).toBe(false);
    expect(isAccessLoss({ ok: false, status: 500, error: "x" })).toBe(false);
    expect(isAccessLoss({ ok: true })).toBe(false);
  });
  it("keeps a newer shown version and follows the incoming membership", () => {
    const shown = [account({ id: "a", control_version: 3, selected: false }), account({ id: "gone", control_version: 9 })];
    const incoming = [account({ id: "a", control_version: 2, selected: true }), account({ id: "b", control_version: 1 })];
    const merged = newer(shown, incoming);
    expect(merged.map((item) => [item.id, item.control_version, item.selected])).toEqual([["a", 3, false], ["b", 1, true]]);
    expect(newer([account({ id: "a", control_version: 1 })], [account({ id: "a", control_version: 4 })])[0]?.control_version).toBe(4);
  });
});

describe("consent helpers", () => {
  it("chooses renew only for a connection that has worked before", () => {
    expect(consentIntent(connection)).toBe("connect");
    expect(consentIntent({ ...connection, last_success: 5 })).toBe("renew");
    expect(consentIntent({ ...connection, valid_until: 5 })).toBe("renew");
    expect(consentIntent({ ...connection, attention: "expired_session" })).toBe("renew");
    expect(consentIntent({ ...connection, renewal_due: true })).toBe("renew");
  });
  it("accepts only plain HTTPS authorization URLs", () => {
    expect(safeAuthorizationUrl("https://bank.example.test/a?b=c")).toBe("https://bank.example.test/a?b=c");
    for (const bad of ["http://bank.example.test/", "javascript:alert(1)", "https://user:pw@bank.example.test/", "not a url", 5, null]) {
      expect(safeAuthorizationUrl(bad)).toBeNull();
    }
  });
  it("mirrors the server's active and ready flow states", () => {
    expect([...OPEN_FLOW].sort()).toEqual(["candidate", "created", "exchanging", "staged", "started"]);
    expect([...READY_FLOW].every((status) => OPEN_FLOW.has(status))).toBe(true);
    for (const closed of ["adopted", "canceled", "expired", "failed"]) expect(OPEN_FLOW.has(closed)).toBe(false);
  });
});

describe("stored limits", () => {
  const now = 1_000_000;
  it("describes suspension, in-flight lease, deadlines and free state without lifting anything", () => {
    expect(limitState(componentLimit("a", "transactions"), now)).toEqual({ kind: "ready" });
    expect(limitState(componentLimit("a", "transactions", { suspended: true, lease_until: now + 50 }), now)).toEqual({ kind: "suspended" });
    expect(limitState(componentLimit("a", "transactions", { lease_until: now + 50, next_attempt: now + 900 }), now))
      .toEqual({ kind: "running", until: now + 50 });
    expect(limitState(componentLimit("a", "transactions", { lease_until: now - 1, next_attempt: now + 900 }), now))
      .toEqual({ kind: "waiting", until: now + 900, retry: false });
    expect(limitState(componentLimit("a", "transactions", { next_attempt: now + 10, retry_at: now + 500, backoff_until: now + 100 }), now))
      .toEqual({ kind: "waiting", until: now + 500, retry: true });
    expect(limitState(componentLimit("a", "transactions", { retry_at: now - 5, next_attempt: now - 1 }), now)).toEqual({ kind: "ready" });
  });
  it("combines a component with its connection's shared scopes, most restrictive first", () => {
    const free = componentLimit("a", "balances");
    expect(effectiveState(free, [sharedLimit("application"), sharedLimit("provider")], now)).toEqual({ kind: "ready" });
    expect(effectiveState(free, [sharedLimit("provider", { retry_at: now + 300 })], now)).toEqual({ kind: "waiting", until: now + 300, retry: true });
    expect(effectiveState(componentLimit("a", "balances", { next_attempt: now + 900 }), [sharedLimit("connection", { next_attempt: now + 100 })], now))
      .toEqual({ kind: "waiting", until: now + 900, retry: false });
    expect(effectiveState(componentLimit("a", "balances", { lease_until: now + 10 }), [sharedLimit("connection", { next_attempt: now + 100 })], now))
      .toEqual({ kind: "running", until: now + 10 });
    expect(effectiveState(componentLimit("a", "balances", { lease_until: now + 10 }), [sharedLimit("application", { suspended: true })], now))
      .toEqual({ kind: "suspended" });
    expect(isComponentLimit(free)).toBe(true);
    expect(isComponentLimit(sharedLimit("connection"))).toBe(false);
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
