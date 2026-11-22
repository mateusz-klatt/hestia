import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BudgetApi, BudgetResult } from "../api";
import { account } from "../fixtures";
import { forecastCurrencyTotal, forecastDeduplication, forecastItem, forecastOf, mortgageSchedule, mortgagesOf } from "./fixtures";
import { ForecastView } from "./forecast";
import { ANALYTICS_ERROR } from "./model";
import type { AnalyticsForecast, AnalyticsMortgages } from "./model";

const accounts = [account({
  id: "acc-1",
  display: { product: "Synthetic Main", currency: "PLN", masked_suffix: "0001", source: "offline_import", observed_at: 1, imported_at: 2 },
  observed_balance_currencies: ["EUR"],
})];

const limit = { ok: false, status: 413, error: ANALYTICS_ERROR.limit } as const;
const exact = "12345678901234567890.05";

type AccessResult = { ok: boolean; status?: number; error?: string };

function ok<T>(data: T): BudgetResult<T> { return { ok: true, data }; }

function deferred<T>(): { promise: Promise<BudgetResult<T>>; resolve: (result: BudgetResult<T>) => void } {
  let resolve: (result: BudgetResult<T>) => void = () => undefined;
  const promise = new Promise<BudgetResult<T>>((settle) => { resolve = settle; });
  return { promise, resolve };
}

function fakeApi() {
  return {
    forecast: vi.fn<BudgetApi["forecast"]>().mockResolvedValue(ok(forecastOf())),
    mortgages: vi.fn<BudgetApi["mortgages"]>().mockResolvedValue(ok(mortgagesOf())),
  };
}

function withItem(label: string, currency: string): AnalyticsForecast {
  const base = forecastOf();
  const total = forecastCurrencyTotal({ currency });
  return {
    ...base,
    totals: [total],
    months: base.months.map((month) => month.month === base.start_month
      ? { ...month, items: [forecastItem({ label, currency })], currencies: [total] }
      : month),
  };
}

function populatedForecast(): AnalyticsForecast {
  const base = forecastOf();
  const loan = forecastItem({ amount: exact, expected_date: "2026-11-15" });
  const recurring = forecastItem({ source: "recurring", confidence: "high", schedule_id: null, label: "Synthetic Stream",
    currency: "EUR", amount: "49.99", expected_date: "2026-11-25", past_due: false, account_stale: true });
  const pln = forecastCurrencyTotal({ currency: "PLN", total: exact, scheduled_total: exact, estimated_total: "0" });
  const eur = forecastCurrencyTotal({ currency: "EUR", total: "49.99", scheduled_total: "0", estimated_total: "49.99" });
  return {
    ...base,
    ledger_as_of: "2026-11-08",
    freshness_stale: true,
    stale_account_ids: ["acc-1"],
    stale_account_ids_truncated: true,
    summary: { ...base.summary, selected_accounts: 3, stale_accounts: 3, overdue_mortgage_schedule_count: 1,
      lapsed_recurring_count: 1, stale_recurring_count: 2, deduped_recurring_count: 1 },
    totals: [eur, pln],
    deduplications: [forecastDeduplication({ amount: exact, matched_due_date: "2026-10-15", last_observed_date: "2026-10-16" })],
    months: base.months.map((month) => month.month === base.start_month
      ? { ...month, items: [loan, recurring], currencies: [eur, pln] }
      : month),
  };
}

let host: HTMLElement;
let root: Root;

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(async () => {
  await update(() => { root.unmount(); });
  host.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function update(change: () => void): Promise<void> {
  await act(async () => {
    change();
    await Promise.resolve();
  });
}

async function show(api: ReturnType<typeof fakeApi>, locale = "en-US", onAccessLoss: (result: AccessResult) => boolean = () => false,
  onOpenMortgages: () => void = () => undefined): Promise<void> {
  await update(() => {
    root.render(<ForecastView api={api} accounts={accounts} locale={locale} revision={0}
      onAccessLoss={onAccessLoss} onOpenMortgages={onOpenMortgages} />);
  });
}

function requireElement<T extends Element>(element: T | null, name: string): T {
  if (element === null) throw new Error(`Missing ${name}`);
  return element;
}

function select(label: string): HTMLSelectElement {
  const element = [...host.querySelectorAll("select")].find((control) =>
    [...control.labels].some((associated) => associated.textContent.startsWith(label)));
  if (element === undefined) throw new Error(`Missing select ${label}`);
  return element;
}

const horizon = () => select("Forecast horizon");
const currency = () => select("Filter forecast by currency");
const currencyOptions = () => [...currency().options].map((option) => option.value);

async function choose(select: HTMLSelectElement, value: string): Promise<void> {
  await update(() => {
    select.value = value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

function button(label: string): HTMLButtonElement {
  const found = [...host.querySelectorAll("button")].find((item) => item.textContent === label);
  if (found === undefined) throw new Error(`Missing button ${label}`);
  return found;
}

describe("ForecastView", () => {
  it.each([
    {
      locale: "en-US",
      disclaimer: "Estimated upcoming commitments based on your mortgage schedules and monthly recurring expense patterns (at least three occurrences). This is an estimate only — not a guarantee, cashflow balance forecast, or prediction of variable spending.",
      scheduled: "Scheduled", estimated: "Estimated", unmarked: "Unmarked", stale: "Account not up to date",
      merged: "Merged with instalment",
    },
    {
      locale: "pl-PL",
      disclaimer: "Szacunek nadchodzących zobowiązań na podstawie Twoich harmonogramów hipotecznych i powtarzalnych wydatków miesięcznych (co najmniej trzy wystąpienia). To wyłącznie szacunek — nie gwarancja, prognoza salda ani przewidywanie wydatków zmiennych.",
      scheduled: "Zaplanowana", estimated: "Szacowana", unmarked: "Nieoznaczona", stale: "Konto nieaktualne",
      merged: "Połączono z ratą",
    },
  ])("renders the estimate disclaimer and each status chip in $locale", async ({ locale, disclaimer, scheduled, estimated, unmarked, stale, merged }) => {
    const api = fakeApi();
    api.forecast.mockResolvedValue(ok(populatedForecast()));
    await show(api, locale);

    expect(host.textContent).toContain(disclaimer);
    expect(host.textContent).toContain("Synthetic Home Loan");
    expect(host.textContent).toContain("Synthetic Stream");
    const details = [...host.querySelectorAll("details")].find((item) => item.querySelector("summary")?.textContent === merged);
    if (details === undefined) throw new Error(`Missing details ${merged}`);
    const itemChips = [...host.querySelectorAll(".budget-chip")].filter((chip) => !details.contains(chip));
    expect(itemChips.map((chip) => chip.textContent)).toEqual(expect.arrayContaining([scheduled, estimated, unmarked, stale]));

    expect(details.open).toBe(false);
    await update(() => { requireElement(details.querySelector("summary"), merged).click(); });
    expect(details.open).toBe(true);
    expect(details.textContent).toContain("Synthetic Bank Mortgage");
    expect(details.textContent).toContain("Synthetic Home Loan");
    expect(details.textContent).toContain("2026-10-15");
    expect(details.textContent).toContain("2026-10-16");
    expect(details.querySelector(".budget-chip")?.textContent).toBe(stale);
    expect(details.querySelector("button, input, select")).toBeNull();
  });

  it("labels each currency chart and shows exact monthly totals in text tables", async () => {
    const api = fakeApi();
    api.forecast.mockResolvedValue(ok(populatedForecast()));
    await show(api);

    for (const code of ["PLN", "EUR"]) {
      const chart = host.querySelector(`svg[role="img"][aria-label="Monthly forecast totals in ${code}"]`);
      expect(chart?.querySelector("rect")).toBeInstanceOf(SVGElement);
    }
    const tables = [...host.querySelectorAll("table")];
    const pln = tables.find((table) => table.caption?.textContent === "Monthly totals · PLN");
    const eur = tables.find((table) => table.caption?.textContent === "Monthly totals · EUR");
    expect(pln?.textContent).toContain("12,345,678,901,234,567,890.05 PLN");
    expect(eur?.textContent).toContain("49.99 EUR");
    expect(pln?.textContent).not.toContain("EUR");
    expect(eur?.textContent).not.toContain("PLN");
  });

  it("refetches for horizons and currencies without writing storage or the URL", async () => {
    const api = fakeApi();
    api.forecast.mockResolvedValueOnce(ok({
      ...withItem("Synthetic GBP loan", "GBP"), deduplications: [forecastDeduplication({ currency: "CAD" })],
    }));
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const pushState = vi.spyOn(History.prototype, "pushState");
    const replaceState = vi.spyOn(History.prototype, "replaceState");
    await show(api);

    expect(horizon().value).toBe("3");
    expect(api.forecast).toHaveBeenLastCalledWith(3, undefined);
    expect(currencyOptions()).toEqual(["", "CAD", "EUR", "GBP", "PLN"]);
    await choose(horizon(), "6");
    expect(api.forecast).toHaveBeenLastCalledWith(6, undefined);
    await choose(currency(), "PLN");
    expect(api.forecast).toHaveBeenLastCalledWith(6, "PLN");
    await choose(horizon(), "12");
    expect(api.forecast).toHaveBeenLastCalledWith(12, "PLN");
    await choose(currency(), "");
    expect(api.forecast).toHaveBeenLastCalledWith(12, undefined);
    expect(setItem).not.toHaveBeenCalled();
    expect(pushState).not.toHaveBeenCalled();
    expect(replaceState).not.toHaveBeenCalled();
  });

  it("discards a successful forecast after a newer request has rendered", async () => {
    const old = deferred<AnalyticsForecast>();
    const api = fakeApi();
    api.forecast.mockReturnValueOnce(old.promise).mockResolvedValueOnce(ok(withItem("Fresh EUR", "EUR")));
    await show(api);

    await choose(currency(), "EUR");
    expect(host.textContent).toContain("Fresh EUR");
    await update(() => { old.resolve(ok(withItem("Stale PLN", "PLN"))); });
    expect(host.textContent).toContain("Fresh EUR");
    expect(host.textContent).not.toContain("Stale PLN");
  });

  it.each([401, 403])("passes a superseded %i to the access barrier before dropping it", async (status) => {
    const old = deferred<AnalyticsForecast>();
    const api = fakeApi();
    api.forecast.mockReturnValueOnce(old.promise).mockResolvedValueOnce(ok(withItem("Fresh EUR", "EUR")));
    const barrier = vi.fn((result: AccessResult) => !result.ok && result.status === status);
    await show(api, "en-US", barrier);
    await choose(currency(), "EUR");
    expect(host.textContent).toContain("Fresh EUR");

    const refusal = { ok: false, status, error: status === 401 ? "budget_not_signed_in" : "budget_forbidden" } as const;
    await update(() => { old.resolve(refusal); });
    expect(barrier).toHaveBeenLastCalledWith(refusal);
    expect(host.textContent).toContain("Fresh EUR");
  });

  it("recovers from an initial 413 with account currencies, mortgage currencies, and a manual code", async () => {
    const schedules = deferred<AnalyticsMortgages>();
    const api = fakeApi();
    api.forecast.mockResolvedValueOnce(limit).mockResolvedValueOnce(ok(withItem("Synthetic CHF loan", "CHF")))
      .mockResolvedValueOnce(ok(withItem("Synthetic CAD loan", "CAD")));
    api.mortgages.mockReturnValueOnce(schedules.promise);
    await show(api);

    expect(host.querySelector('[role="alert"]')?.textContent).toMatch(/currency.*horizon/i);
    expect(currency().disabled).toBe(false);
    expect(currencyOptions()).toEqual(["", "EUR", "PLN"]);
    expect(api.mortgages).toHaveBeenCalledTimes(1);
    await update(() => { schedules.resolve(ok(mortgagesOf([mortgageSchedule({ currency: "CHF" })]))); });
    expect(currencyOptions()).toEqual(["", "CHF", "EUR", "PLN"]);

    await choose(currency(), "CHF");
    expect(api.forecast).toHaveBeenLastCalledWith(3, "CHF");
    expect(host.textContent).toContain("Synthetic CHF loan");

    const input = requireElement(host.querySelector<HTMLInputElement>("form input"), "currency code field");
    await update(() => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, "cad");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await update(() => { button("Apply currency").click(); });
    expect(api.forecast).toHaveBeenLastCalledWith(3, "CAD");
    expect(host.textContent).toContain("Synthetic CAD loan");
    expect(currency().value).toBe("CAD");
  });

  it("checks access loss from a superseded mortgage fallback", async () => {
    const schedules = deferred<AnalyticsMortgages>();
    const api = fakeApi();
    api.forecast.mockResolvedValueOnce(limit).mockResolvedValueOnce(ok(withItem("Fresh EUR", "EUR")));
    api.mortgages.mockReturnValueOnce(schedules.promise);
    const barrier = vi.fn((result: AccessResult) => !result.ok && result.status === 403);
    await show(api, "en-US", barrier);
    await choose(currency(), "EUR");

    const refusal = { ok: false, status: 403, error: "budget_forbidden" } as const;
    await update(() => { schedules.resolve(refusal); });
    expect(barrier).toHaveBeenLastCalledWith(refusal);
    expect(host.textContent).toContain("Fresh EUR");
  });

  it("offers mortgage reminders and shows dates and stale account names", async () => {
    const api = fakeApi();
    const openMortgages = vi.fn();
    api.forecast.mockResolvedValue(ok(populatedForecast()));
    await show(api, "en-US", () => false, openMortgages);

    expect(host.textContent).toContain("Evaluated for 2026-11-22 (UTC)");
    expect(host.textContent).toContain("Ledger data as of 2026-11-08");
    expect(host.textContent).toContain("1 mortgage schedule overdue from prior months");
    expect(host.textContent).toContain("1 lapsed recurring hint");
    expect(host.textContent).toContain("2 recurring hints from stale accounts");
    expect(host.textContent).toContain("3 stale accounts");
    expect(host.textContent).toContain("2 more stale accounts omitted from this list.");
    expect(host.textContent).toContain("Synthetic Main ••0001");
    expect(host.textContent).not.toContain("acc-1");
    await update(() => { button("Open mortgage reminders").click(); });
    expect(openMortgages).toHaveBeenCalledOnce();
  });

  it("shows an empty state for both all-currency and filtered responses", async () => {
    const api = fakeApi();
    await show(api);

    expect(host.textContent).toContain("No upcoming commitments in this period.");
    expect(host.querySelector('svg[role="img"]')).toBeNull();
    await choose(currency(), "EUR");
    expect(host.textContent).toContain("No upcoming commitments in EUR in this period.");
  });
});
