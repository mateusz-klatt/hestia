// Synthetic data only: no real account, category, amount or bank values.
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BudgetApi, BudgetResult } from "../api";
import { account } from "../fixtures";
import { categoryList, monthlyBudgetCategoryLimit, monthlyBudgetCategorySpend, monthlyBudgetChartsOf,
  monthlyBudgetExpectedIncome, monthlyBudgetPace, monthlyBudgetPassThrough,
  monthlyBudgetSeriesOf } from "./fixtures";
import { ANALYTICS_ERROR } from "./model";
import type { AnalyticsCategory, AnalyticsMonthlyBudgetCharts } from "./model";
import { MonthBudgetView, monthBudgetLabels } from "./month_budget";
import { alignDecimals, buildCategoryChartModel, buildYScale, formatTickDecimal } from "./month_budget_charts";

const accounts = [account({
  id: "acc-1",
  display: { product: "Synthetic Main", currency: "PLN", masked_suffix: "0001", source: "offline_import", observed_at: 1, imported_at: 2 },
  observed_balance_currencies: ["EUR"],
})];

type AccessResult = { ok: boolean; status?: number; error?: string };

function ok<T>(data: T): BudgetResult<T> { return { ok: true, data }; }
function fail<T>(status: number, error: string): BudgetResult<T> { return { ok: false, status, error }; }
function deferred<T>(): { promise: Promise<BudgetResult<T>>; resolve: (result: BudgetResult<T>) => void } {
  let resolve: (result: BudgetResult<T>) => void = () => undefined;
  const promise = new Promise<BudgetResult<T>>((settle) => { resolve = settle; });
  return { promise, resolve };
}

function fakeApi() {
  return {
    monthlyBudgetCharts: vi.fn<BudgetApi["monthlyBudgetCharts"]>().mockResolvedValue(ok(monthlyBudgetChartsOf())),
    categories: vi.fn<BudgetApi["categories"]>().mockResolvedValue(ok({ categories: [
      ...categoryList,
      { id: "cat-salary", name: "Synthetic Salary", archived: false, version: 1, created_at: 1, updated_at: 1 },
      { id: "cat-rent", name: "Synthetic Rent", archived: false, version: 1, created_at: 1, updated_at: 1 },
      { id: "cat-mortgage", name: "Synthetic Mortgage", archived: false, version: 1, created_at: 1, updated_at: 1 },
      { id: "cat-lookup", name: "Synthetic Lookup", archived: false, version: 1, created_at: 1, updated_at: 1 },
    ] })),
  } satisfies Pick<BudgetApi, "monthlyBudgetCharts" | "categories">;
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
  await act(async () => { root.unmount(); await Promise.resolve(); });
  host.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function settle(): Promise<void> {
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
}

async function show(
  api: Pick<BudgetApi, "monthlyBudgetCharts" | "categories">,
  options: {
    locale?: string;
    revision?: number;
    onAccessLoss?: (result: AccessResult) => boolean;
    onOpenMonth?: (month: string, cutoff?: string) => void;
  } = {},
): Promise<void> {
  const locale = options.locale ?? "en-US";
  const text = locale.toLowerCase().startsWith("pl") ? monthBudgetLabels.pl : monthBudgetLabels.en;
  await act(async () => {
    root.render(
      <MonthBudgetView
        api={api}
        accounts={accounts}
        text={text}
        locale={locale}
        revision={options.revision ?? 0}
        onAccessLoss={options.onAccessLoss ?? (() => false)}
        onOpenMonth={options.onOpenMonth ?? (() => undefined)}
      />,
    );
    await Promise.resolve();
  });
  await settle();
}

function currencySelect(): HTMLSelectElement {
  const element = host.querySelector<HTMLSelectElement>('select[name="currency"]');
  if (element === null) throw new Error("Missing currency select");
  return element;
}

function historySelect(): HTMLSelectElement {
  const element = host.querySelector<HTMLSelectElement>('select[name="history"]');
  if (element === null) throw new Error("Missing history select");
  return element;
}

function button(label: string): HTMLButtonElement {
  const found = [...host.querySelectorAll("button")].find((item) => item.textContent === label);
  if (found === undefined) throw new Error(`Missing button ${label}`);
  return found;
}

function labelledButton(label: string): HTMLButtonElement {
  const found = host.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`);
  if (found === null) throw new Error(`Missing button aria-label=${label}`);
  return found;
}

async function click(element: HTMLElement): Promise<void> {
  await act(async () => { element.click(); await Promise.resolve(); });
  await settle();
}

async function chooseCurrency(code: string): Promise<void> {
  const select = currencySelect();
  await act(async () => {
    select.value = code;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    await Promise.resolve();
  });
  await settle();
}

async function chooseHistory(value: "6" | "12"): Promise<void> {
  const select = historySelect();
  await act(async () => {
    select.value = value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    await Promise.resolve();
  });
  await settle();
}

function assertBoundedSvgCoordinates(svg: SVGSVGElement): void {
  const viewBox = svg.getAttribute("viewBox")?.split(" ").map(Number) ?? [];
  expect(viewBox).toHaveLength(4);
  const maxX = viewBox[2] ?? 0;
  const maxY = viewBox[3] ?? 0;
  for (const el of svg.querySelectorAll("rect, line, circle")) {
    for (const attr of ["x", "x1", "x2", "cx", "width"]) {
      const raw = el.getAttribute(attr);
      if (raw !== null) {
        const val = Number(raw);
        expect(Number.isFinite(val)).toBe(true);
        expect(val).toBeGreaterThanOrEqual(0);
        expect(val).toBeLessThanOrEqual(maxX);
      }
    }
    for (const attr of ["y", "y1", "y2", "cy", "height"]) {
      const raw = el.getAttribute(attr);
      if (raw !== null) {
        const val = Number(raw);
        expect(Number.isFinite(val)).toBe(true);
        expect(val).toBeGreaterThanOrEqual(0);
        expect(val).toBeLessThanOrEqual(maxY);
      }
    }
  }
  for (const poly of svg.querySelectorAll("polyline")) {
    const pts = (poly.getAttribute("points") ?? "").trim().split(/\s+/);
    for (const pair of pts) {
      const [xs, ys] = pair.split(",").map(Number);
      expect(Number.isFinite(xs)).toBe(true);
      expect(Number.isFinite(ys)).toBe(true);
      expect(xs).toBeGreaterThanOrEqual(0);
      expect(xs).toBeLessThanOrEqual(maxX);
      expect(ys).toBeGreaterThanOrEqual(0);
      expect(ys).toBeLessThanOrEqual(maxY);
    }
  }
}

describe("MonthBudgetView", () => {
  it("keeps identical i18n keys in English and Polish and pluralizes Polish counts", () => {
    expect(Object.keys(monthBudgetLabels.pl).sort()).toEqual(Object.keys(monthBudgetLabels.en).sort());
    expect(Object.keys(monthBudgetLabels.pl.limitStatuses).sort()).toEqual(Object.keys(monthBudgetLabels.en.limitStatuses).sort());
    expect(Object.keys(monthBudgetLabels.pl.cardStatuses).sort()).toEqual(Object.keys(monthBudgetLabels.en.cardStatuses).sort());

    expect(monthBudgetLabels.en.staleAccounts(1)).toBe("1 stale account");
    expect(monthBudgetLabels.en.staleAccounts(2)).toBe("2 stale accounts");
    expect(monthBudgetLabels.en.pendingTransfers(1)).toContain("1 unverified transfer or classification candidate still affects");
    expect(monthBudgetLabels.en.pendingTransfers(2)).toContain("2 unverified transfer or classification candidates still affect");
    expect(monthBudgetLabels.en.purchaseDateFallback(1)).toBe("1 debit used posting date because purchase date was absent.");
    expect(monthBudgetLabels.en.purchaseDateFallback(3)).toBe("3 debits used posting date because purchase date was absent.");
    expect(monthBudgetLabels.en.historyMonths(6)).toBe("6 months");

    expect(monthBudgetLabels.pl.staleAccounts(1)).toBe("1 nieaktualne konto");
    expect(monthBudgetLabels.pl.staleAccounts(2)).toBe("2 nieaktualne konta");
    expect(monthBudgetLabels.pl.staleAccounts(5)).toBe("5 nieaktualnych kont");
    expect(monthBudgetLabels.pl.staleAccounts(12)).toBe("12 nieaktualnych kont");
    expect(monthBudgetLabels.pl.staleAccounts(22)).toBe("22 nieaktualne konta");
    expect(monthBudgetLabels.pl.pendingTransfers(1)).toContain("1 niepotwierdzony wpis lub przelew nadal wpływa");
    expect(monthBudgetLabels.pl.pendingTransfers(2)).toContain("2 niepotwierdzone wpisy lub przelewy nadal wpływają");
    expect(monthBudgetLabels.pl.pendingTransfers(5)).toContain("5 niepotwierdzonych wpisów lub przelewów nadal wpływa");
    expect(monthBudgetLabels.pl.purchaseDateFallback(1)).toContain("1 wydatku");
    expect(monthBudgetLabels.pl.purchaseDateFallback(2)).toContain("2 wydatków");
    expect(monthBudgetLabels.pl.historyMonths(6)).toBe("6 miesięcy");
    expect(monthBudgetLabels.pl.historyMonths(12)).toBe("12 miesięcy");
  });

  it.each([
    { locale: "pl-PL", label: "Budżet z wpływów", window: "20 paź – 19 lis" },
    { locale: "en-US", label: "Budget received", window: "20 Oct – 19 Nov" },
  ])("shows the configured income window in the $locale budget label", async ({ locale, label, window }) => {
    const api = fakeApi();
    api.monthlyBudgetCharts.mockResolvedValue(ok(monthlyBudgetChartsOf({
      configuration: {
        status: "ready", source: "bootstrap",
        income_window: { start: "2026-10-20", end: "2026-11-19", start_day: 20 },
      },
    })));
    await show(api, { locale });

    const term = [...host.querySelectorAll("dt")].find((item) => item.textContent.startsWith(label));
    expect(term?.textContent).toBe(`${label} (${window})`);
  });

  it("omits the income window from the budget label when it is not configured", async () => {
    const api = fakeApi();
    await show(api, { locale: "pl-PL" });

    const term = [...host.querySelectorAll("dt")].find((item) => item.textContent.startsWith("Budżet z wpływów"));
    expect(term?.textContent).toBe("Budżet z wpływów");
    expect(term?.querySelector("small")).toBeNull();
  });

  it("shows loading state initially and hides month navigation until server current_month arrives", async () => {
    const pending = deferred<AnalyticsMonthlyBudgetCharts>();
    const api = fakeApi();
    api.monthlyBudgetCharts.mockReturnValueOnce(pending.promise);
    await act(async () => {
      root.render(
        <MonthBudgetView api={api} accounts={accounts} text={monthBudgetLabels.en} locale="en-US"
          revision={0} onAccessLoss={() => false} onOpenMonth={() => undefined} />,
      );
      await Promise.resolve();
    });
    expect(host.querySelector('[role="status"]')?.textContent).toBe("Loading monthly budget…");
    expect(host.querySelector(".budget-month-nav")).toBeNull();
    expect(api.monthlyBudgetCharts).toHaveBeenCalledWith(undefined, "PLN", 6);

    await act(async () => { pending.resolve(ok(monthlyBudgetChartsOf())); await Promise.resolve(); });
    await settle();
    expect(host.querySelector('[role="status"]')).toBeNull();
    expect(host.querySelector(".budget-month-nav h3")?.textContent).toBe("November 2026");
  });

  it("renders unconfigured state without summary, pace, category tables, or charts, and handles missing ledger timestamp", async () => {
    const api = fakeApi();
    api.monthlyBudgetCharts.mockResolvedValue(ok(monthlyBudgetChartsOf({
      configuration: { status: "unconfigured", source: "none" },
      coverage: {
        ledger_as_of: null,
        stale_selected_accounts: 1,
        purchase_date_fallback_count: 0,
        pending_transfer_rows: 0,
        transfer_scan_limited: false,
        provisional: false,
        has_activity: false,
      },
      summary: null,
      expected_income: null,
      pace: null,
      category_spend: [],
      pass_through: null,
    }, null)));
    await show(api);

    expect(api.categories).not.toHaveBeenCalled();
    expect(host.textContent).toContain("Monthly budget is not configured yet");
    expect(host.textContent).toContain("No synced ledger data yet");
    expect(host.textContent).toContain("Data may be out of date · 1 stale account");
    expect(host.textContent).not.toContain("No stored data");
    expect(host.textContent).not.toContain("Budget received");
    expect(host.querySelector("table")).toBeNull();
    expect(host.querySelector("svg")).toBeNull();

    // Defensive check if ready configuration arrives with missing sections or null series.
    api.monthlyBudgetCharts.mockResolvedValue(ok(monthlyBudgetChartsOf({
      configuration: { status: "ready", source: "bootstrap" },
      summary: null,
      expected_income: null,
      pace: null,
    }, null)));
    await show(api, { revision: 1 });
    expect(host.textContent).toContain("Monthly budget is not configured yet");

    api.monthlyBudgetCharts.mockResolvedValue(ok(monthlyBudgetChartsOf({
      configuration: { status: "ready", source: "bootstrap" },
      expected_income: null,
      pace: null,
    }, null)));
    await show(api, { revision: 2 });
    expect(host.textContent).toContain("Monthly budget is not configured yet");

    api.monthlyBudgetCharts.mockResolvedValue(ok(monthlyBudgetChartsOf({
      configuration: { status: "ready", source: "bootstrap" },
      pace: null,
    }, null)));
    await show(api, { revision: 3 });
    expect(host.textContent).toContain("Monthly budget is not configured yet");

    api.monthlyBudgetCharts.mockResolvedValue(ok(monthlyBudgetChartsOf({}, null)));
    await show(api, { revision: 4 });
    expect(host.querySelector(".budget-month-charts")).toBeNull();
  });

  it("distinguishes no stored rows from observed zero", async () => {
    const api = fakeApi();
    api.monthlyBudgetCharts.mockResolvedValue(ok(monthlyBudgetChartsOf({
      coverage: {
        ledger_as_of: 1_795_345_200,
        stale_selected_accounts: 0,
        purchase_date_fallback_count: 0,
        pending_transfer_rows: 0,
        transfer_scan_limited: false,
        provisional: false,
        has_activity: false,
      },
      summary: { start_budget: "0", spent: "0", remaining: "0" },
      pace: monthlyBudgetPace({ status: "no_budget", budget: "0", daily_rate: null, projected_month_spend: null }),
      category_spend: [],
    }, {
      daily: [{ date: "2026-11-01", cumulative_spent: "0" }],
      months: [
        { period: "2026-11", has_activity: false, is_current_month: true, income_estimate_partial: false,
          provisional: false, start_budget: "0", spent: "0", categories: [] },
      ],
      category_labels: [],
    })));
    await show(api);

    expect(host.textContent).toContain("No stored data");
    expect(host.textContent).not.toContain("zero net budget impact");
    expect(host.querySelectorAll("svg")).toHaveLength(3);
    for (const svg of host.querySelectorAll("svg")) assertBoundedSvgCoordinates(svg);

    api.monthlyBudgetCharts.mockResolvedValue(ok(monthlyBudgetChartsOf({
      coverage: {
        ledger_as_of: 1_795_345_200,
        stale_selected_accounts: 0,
        purchase_date_fallback_count: 0,
        pending_transfer_rows: 0,
        transfer_scan_limited: false,
        provisional: false,
        has_activity: true,
      },
      summary: { start_budget: "0", spent: "0", remaining: "0" },
      pace: monthlyBudgetPace({ status: "no_budget", budget: "0", daily_rate: null, projected_month_spend: null }),
      category_spend: [],
    }, {
      daily: [{ date: "2026-11-01", cumulative_spent: "0" }],
      months: [
        { period: "2026-11", has_activity: true, is_current_month: true, income_estimate_partial: false,
          provisional: false, start_budget: "0", spent: "0", categories: [] },
      ],
      category_labels: [],
    })));
    await show(api, { revision: 1 });

    expect(host.textContent).toContain("Transactions are stored for this month with zero net budget impact.");
    expect(host.textContent).toContain("No category spending recorded in this month.");
    expect(host.textContent).toContain("0.00 PLN");
  });

  it("does not show no stored data when early window income funds a month with no category rows", async () => {
    const api = fakeApi();
    api.monthlyBudgetCharts.mockResolvedValue(ok(monthlyBudgetChartsOf({
      configuration: {
        status: "ready", source: "bootstrap",
        income_window: { start: "2026-10-20", end: "2026-11-19", start_day: 20 },
      },
      coverage: {
        ledger_as_of: 1_795_345_200,
        stale_selected_accounts: 0,
        purchase_date_fallback_count: 0,
        pending_transfer_rows: 0,
        transfer_scan_limited: false,
        provisional: false,
        has_activity: true,
      },
      summary: { start_budget: "5000", spent: "0", remaining: "5000" },
      pace: monthlyBudgetPace({
        status: "no_positive_pace", budget: "5000", daily_rate: null, projected_month_spend: null,
      }),
      category_spend: [],
    }, {
      daily: [{ date: "2026-11-01", cumulative_spent: "0" }],
      months: [
        { period: "2026-11", has_activity: true, is_current_month: true, income_estimate_partial: false,
          provisional: false, start_budget: "5000", spent: "0", categories: [] },
      ],
      category_labels: [],
    })));
    await show(api);

    expect(host.textContent).not.toContain("No stored data");
    expect(host.textContent).toContain("5,000.00 PLN");
    expect(host.textContent).toContain("No category spending recorded in this month.");
  });

  it.each([
    { status: 400, error: ANALYTICS_ERROR.invalidRequest, alert: monthBudgetLabels.en.badRequest },
    { status: 404, error: "budget_analytics_disabled", alert: monthBudgetLabels.en.error },
    { status: 409, error: "budget_requires_sqlite_users", alert: monthBudgetLabels.en.error },
    { status: 413, error: ANALYTICS_ERROR.limit, alert: monthBudgetLabels.en.limit },
    { status: 500, error: "budget_internal_error", alert: monthBudgetLabels.en.error },
    { status: 503, error: "budget_unavailable", alert: monthBudgetLabels.en.error },
  ])("clears stale loaded data and shows fixed alert on $status ($error)", async ({ status, error, alert }) => {
    const api = fakeApi();
    api.monthlyBudgetCharts
      .mockResolvedValueOnce(ok(monthlyBudgetChartsOf({
        available_currencies: ["PLN", "EUR"],
        summary: { start_budget: "8888", spent: "1234", remaining: "7654" },
        pass_through: monthlyBudgetPassThrough(),
      })))
      .mockResolvedValueOnce({ ok: false, status, error });
    await show(api);

    expect(host.textContent).toContain("8,888.00 PLN");
    expect(host.textContent).toContain("Synthetic Food");
    expect(host.textContent).toContain("Rent and mortgage");
    expect(host.querySelectorAll("svg")).toHaveLength(3);

    await chooseCurrency("EUR");
    expect(api.monthlyBudgetCharts).toHaveBeenLastCalledWith(undefined, "EUR", 6);
    expect(host.querySelector('[role="alert"]')?.textContent).toBe(alert);
    expect(host.textContent).not.toContain(error);
    expect(host.textContent).not.toContain("8,888.00");
    expect(host.textContent).not.toContain("Synthetic Food");
    expect(host.textContent).not.toContain("Rent and mortgage");
    expect(host.querySelector("table")).toBeNull();
    expect(host.querySelector("svg")).toBeNull();
  });

  it("handles 404 budget_analytics_disabled, 409 conflict, and 503 unavailable on initial load", async () => {
    for (const [status, error] of [
      [404, "budget_analytics_disabled"],
      [409, "budget_requires_sqlite_users"],
      [503, "budget_analytics_unavailable"],
    ] as const) {
      const api = fakeApi();
      api.monthlyBudgetCharts.mockResolvedValueOnce({ ok: false, status, error });
      await show(api);
      expect(host.querySelector('[role="alert"]')?.textContent).toBe(monthBudgetLabels.en.error);
      expect(host.textContent).not.toContain(error);
      expect(host.querySelector("table")).toBeNull();
    }
  });

  it("recovers from 413 using manual currency code or history toggle and rejects invalid manual submits", async () => {
    const api = fakeApi();
    api.monthlyBudgetCharts
      .mockResolvedValueOnce({ ok: false, status: 413, error: ANALYTICS_ERROR.limit })
      .mockResolvedValueOnce(ok(monthlyBudgetChartsOf({ currency: "CHF", available_currencies: ["PLN", "CHF"] })))
      .mockResolvedValueOnce({ ok: false, status: 413, error: ANALYTICS_ERROR.limit })
      .mockResolvedValueOnce(ok(monthlyBudgetChartsOf({ currency: "CHF", available_currencies: ["PLN", "CHF"] })));
    await show(api);

    expect(host.querySelector('[role="alert"]')?.textContent).toBe(monthBudgetLabels.en.limit);
    const form = host.querySelector<HTMLFormElement>("form.budget-rule-editor");
    const input = host.querySelector<HTMLInputElement>('input[name="manual_currency"]');
    if (form === null || input === null) throw new Error("Missing manual currency form");

    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, "12");
      input.dispatchEvent(new Event("input", { bubbles: true }));
      await Promise.resolve();
    });
    await act(async () => { form.requestSubmit(); await Promise.resolve(); });
    expect(api.monthlyBudgetCharts).toHaveBeenCalledTimes(1);

    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, "chf");
      input.dispatchEvent(new Event("input", { bubbles: true }));
      await Promise.resolve();
    });
    await click(button("Apply currency"));
    expect(api.monthlyBudgetCharts).toHaveBeenLastCalledWith(undefined, "CHF", 6);
    expect(host.textContent).toContain("5,000.00 CHF");

    // Switching to 12 months hits 413 -> switching back to 6 recovers.
    await chooseHistory("12");
    expect(host.querySelector('[role="alert"]')?.textContent).toBe(monthBudgetLabels.en.limit);
    await chooseHistory("6");
    expect(api.monthlyBudgetCharts).toHaveBeenLastCalledWith(undefined, "CHF", 6);
    expect(host.querySelector('[role="alert"]')).toBeNull();
  });

  it("renders booked vs expected income, provisional warnings, partial income warnings, pace runs_out, and charts", async () => {
    const api = fakeApi();
    const bigSpent = "12345678901234567890.05";
    api.monthlyBudgetCharts.mockResolvedValue(ok(monthlyBudgetChartsOf({
      evaluation_date: "2026-11-22",
      available_currencies: ["PLN", "EUR"],
      coverage: {
        ledger_as_of: 1_795_345_200,
        stale_selected_accounts: 2,
        purchase_date_fallback_count: 1,
        pending_transfer_rows: 2,
        transfer_scan_limited: true,
        provisional: true,
        has_activity: true,
      },
      summary: { start_budget: "5000", spent: "3500", remaining: "1500" },
      expected_income: monthlyBudgetExpectedIncome({
        status: "partial",
        total: "2500",
        items: [
          { category_id: "cat-salary", amount: "1500", expected_date: "2026-11-15", source: "previous_month" },
          { category_id: "cat-unknown-inc", amount: "1000", expected_date: null, source: "previous_month" },
        ],
      }),
      pace: monthlyBudgetPace({
        basis: "received_plus_expected",
        budget: "7500",
        status: "runs_out",
        daily_rate: { numerator: bigSpent, denominator: 2 },
        projected_month_spend: { numerator: "6000", denominator: 1 },
        exhaustion_day: 27,
      }),
    }, {
      daily: Array.from({ length: 22 }, (_, i) => ({
        date: `2026-11-${String(i + 1).padStart(2, "0")}`,
        cumulative_spent: i === 2 ? "-50" : i === 21 ? "3500" : String(i * 100),
      })),
    })));
    await show(api);

    expect(host.textContent).toContain("Data may be out of date · 2 stale accounts");
    expect(host.textContent).toContain("1 debit used posting date because purchase date was absent.");
    expect(host.textContent).toContain("Provisional totals: 2 unverified transfer or classification candidates still affect totals.");
    expect(host.textContent).toContain("Own-transfer detection reached its scan limit");
    expect(host.textContent).toContain("Budget received");
    expect(host.textContent).toContain("5,000.00 PLN");
    expect(host.textContent).toContain("Spent to date (Provisional totals)");
    expect(host.textContent).toContain("Remaining (Provisional totals)");
    expect(host.querySelector("progress")?.value).toBe(70);

    const expectedCard = host.querySelector('[aria-label="Expected income"]');
    expect(expectedCard?.textContent).toContain("Partial income evidence");
    expect(expectedCard?.textContent).toContain("2,500.00 PLN");
    expect(expectedCard?.textContent).toContain("Synthetic Salary");
    expect(expectedCard?.textContent).toContain("Income category");
    expect(expectedCard?.textContent).toContain("Expected 2026-11-15");
    expect(expectedCard?.textContent).toContain("Delayed");
    expect(expectedCard?.textContent).toContain("Combined estimate (no expected date)");

    const paceCard = host.querySelector('[aria-label="Spending pace"]');
    expect(paceCard?.textContent).toContain("Provisional totals");
    expect(paceCard?.textContent).toContain("Partial income evidence");
    expect(paceCard?.textContent).toContain("Based on budget received + expected");
    expect(paceCard?.textContent).toContain("7,500.00 PLN");
    expect(paceCard?.textContent).toContain("At this pace the budget runs out around day 27.");
    expect(paceCard?.textContent).toContain("6,172,839,450,617,283,945.02 PLN / day");
    expect(paceCard?.textContent).toContain("6,000.00 PLN");

    // Chart 1 renders booked budget line, expected-inclusive budget line, pace extension, exhaustion marker, and refund dip!
    const dailyChart = host.querySelector('[aria-label="Cumulative spending · PLN"]');
    expect(dailyChart?.querySelector(".budget-chart-booked-line")).not.toBeNull();
    expect(dailyChart?.querySelector(".budget-chart-expected-line")).not.toBeNull();
    expect(dailyChart?.querySelector(".budget-chart-pace-line")).not.toBeNull();
    expect(dailyChart?.querySelector(".budget-chart-exhaustion")?.textContent).toContain("27");
    expect(dailyChart?.textContent).toContain("Budget + expected: 7,500.00 PLN");
    expect(dailyChart?.textContent).toContain("Exhaustion ~day 27");
    expect(dailyChart?.textContent).toContain("-50.00 PLN".replace("-", "−"));
    for (const svg of host.querySelectorAll("svg")) assertBoundedSvgCoordinates(svg);

    // Chart 3 renders expected income bar segment on current month!
    const incomeChart = host.querySelector('[aria-label="Income and spending · PLN"]');
    expect(incomeChart?.querySelector(".budget-chart-expected-bar")).not.toBeNull();
    expect(incomeChart?.textContent).toContain("Expected income: 2,500.00 PLN");
  });

  it("renders all expected income variants and all remaining pace variants", async () => {
    const api = fakeApi();
    // 1. partial with total=null and items=[], runs_out with exhaustion_day=null
    api.monthlyBudgetCharts.mockResolvedValue(ok(monthlyBudgetChartsOf({
      summary: { start_budget: "100", spent: "200", remaining: "-100" },
      expected_income: monthlyBudgetExpectedIncome({ status: "partial", total: null, items: [] }),
      pace: monthlyBudgetPace({ status: "runs_out", exhaustion_day: null, daily_rate: null, projected_month_spend: null }),
    })));
    await show(api);
    expect(host.textContent).toContain("Partial income evidence — remaining expected income could not be estimated reliably.");
    expect(host.textContent).toContain("At this pace the budget will run out before month end.");
    expect(host.querySelector("progress")?.value).toBe(100);
    expect(host.textContent).toContain("Spent compared to budget received: 200%");

    // 2. estimated + already_reached with day (marks exhaustion on current day)
    api.monthlyBudgetCharts.mockResolvedValue(ok(monthlyBudgetChartsOf({
      expected_income: monthlyBudgetExpectedIncome({
        status: "estimated", total: "1000",
        items: [{ category_id: "cat-salary", amount: "1000", expected_date: "2026-11-28", source: "previous_month" }],
      }),
      pace: monthlyBudgetPace({ status: "already_reached", exhaustion_day: 22 }),
    })));
    await show(api, { revision: 1 });
    expect(host.textContent).toContain("1,000.00 PLN");
    expect(host.textContent).not.toContain("Delayed");
    expect(host.textContent).toContain("Spending has already reached the budget (day 22).");
    expect(host.querySelector(".budget-chart-exhaustion")?.textContent).toContain("22");

    // 3. unavailable + already_reached with day=null
    api.monthlyBudgetCharts.mockResolvedValue(ok(monthlyBudgetChartsOf({
      expected_income: monthlyBudgetExpectedIncome({ status: "unavailable", total: null, items: [] }),
      pace: monthlyBudgetPace({ status: "already_reached", exhaustion_day: null }),
    })));
    await show(api, { revision: 2 });
    expect(host.textContent).toContain("Unavailable (no prior-month income data).");
    expect(host.textContent).toContain("Spending has already reached the budget.");

    // 4. none with total=null + within_budget + no_positive_pace
    api.monthlyBudgetCharts.mockResolvedValue(ok(monthlyBudgetChartsOf({
      expected_income: monthlyBudgetExpectedIncome({ status: "none", total: null, items: [] }),
      pace: monthlyBudgetPace({ status: "no_positive_pace", daily_rate: null, projected_month_spend: null }),
    })));
    await show(api, { revision: 3 });
    expect(host.textContent).toContain("No additional income expected this month.");
    expect(host.textContent).toContain("No positive spending pace yet.");

    // 5. within_budget explicitly
    api.monthlyBudgetCharts.mockResolvedValue(ok(monthlyBudgetChartsOf({
      pace: monthlyBudgetPace({ status: "within_budget" }),
    })));
    await show(api, { revision: 4 });
    expect(host.textContent).toContain("At this pace spending stays within the budget.");
  });

  it("renders closed / historical months with Wydano / Spent and omits pace extension and exhaustion on Chart 1", async () => {
    const api = fakeApi();
    const closedSeries = monthlyBudgetSeriesOf({
      daily: Array.from({ length: 31 }, (_, i) => ({
        date: `2026-10-${String(i + 1).padStart(2, "0")}`,
        cumulative_spent: "320.5",
      })),
      months: [
        {
          period: "2026-10", has_activity: true, is_current_month: false, income_estimate_partial: false,
          provisional: false, start_budget: "5000", spent: "320.5",
          categories: [{ category_id: "cat-food", net_spending: "320.5" }],
        },
      ],
    });
    api.monthlyBudgetCharts.mockResolvedValue(ok(monthlyBudgetChartsOf({
      period: "2026-10",
      start: "2026-10-01",
      end: "2026-10-31",
      cutoff: "2026-10-31",
      current_month: "2026-11",
      expected_income: monthlyBudgetExpectedIncome({ status: "historical", total: null, items: [] }),
      pace: monthlyBudgetPace({ status: "historical", basis: "received", daily_rate: null, projected_month_spend: null }),
    }, closedSeries)));
    await show(api, { locale: "pl-PL" });

    expect(host.querySelector(".budget-month-nav h3")?.textContent).toBe("październik 2026");
    expect(host.textContent).toContain("Wydano320,50 PLN");
    expect(host.textContent).not.toContain("Wydano do dziś");
    expect(host.textContent).toContain("Zamknięty miesiąc — tylko zaksięgowane wpływy.");
    expect(host.textContent).toContain("Zamknięty miesiąc — nie wylicza się prognozy tempa.");
    expect(host.querySelector(".budget-chart-pace-line")).toBeNull();
    expect(host.querySelector(".budget-chart-exhaustion")).toBeNull();
    expect(host.querySelector(".budget-chart-expected-line")).toBeNull();
    expect(host.textContent).toContain("Wydatki narastająco · PLN");
    expect(host.textContent).toContain("Wydatki według kategorii · PLN");
    expect(host.textContent).toContain("Wpływy i wydatki · PLN");

    await show(api, { locale: "en-US", revision: 1 });
    expect(host.querySelector(".budget-month-nav h3")?.textContent).toBe("October 2026");
    expect(host.textContent).toContain("Spent320.50 PLN");
    expect(host.textContent).not.toContain("Spent to date");
    expect(host.textContent).toContain("Closed month — booked income only.");
    expect(host.textContent).toContain("Closed month — forward pace is not calculated.");
    expect(host.querySelector(".budget-chart-pace-line")).toBeNull();
    expect(host.querySelector(".budget-chart-exhaustion")).toBeNull();
  });

  it("ranks top-5 categories with stable ID ties, separates positive and negative Other slices, and marks provisional/current/gap buckets across 12 months", async () => {
    const series12 = monthlyBudgetSeriesOf({
      daily: [{ date: "2026-11-01", cumulative_spent: "99999999999999999999" }],
      months: [
        // 12 months oldest -> newest
        ...["2025-12", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07"].map((p, idx) => ({
          period: p, has_activity: idx > 0, is_current_month: false, income_estimate_partial: false,
          provisional: idx === 2, start_budget: idx === 0 ? "0" : "4000", spent: idx === 0 ? "0" : "100",
          categories: idx === 0 ? [] : [{ category_id: "cat-top1", net_spending: "100" }],
        })),
        {
          period: "2026-08", has_activity: true, is_current_month: false, income_estimate_partial: false,
          provisional: false, start_budget: "5000", spent: "200",
          categories: [
            { category_id: "cat-top1", net_spending: "100" },
            { category_id: "cat-signed", net_spending: "100" }, // +100 in Aug and -100 in Sep => abs sum = 200!
          ],
        },
        {
          period: "2026-09", has_activity: true, is_current_month: false, income_estimate_partial: false,
          provisional: false, start_budget: "5000", spent: "200",
          categories: [
            { category_id: "cat-top1", net_spending: "300" },
            { category_id: "cat-signed", net_spending: "-100" },
          ],
        },
        {
          period: "2026-10", has_activity: true, is_current_month: false, income_estimate_partial: false,
          provisional: true, start_budget: "5000", spent: "830",
          categories: [
            { category_id: "cat-tie-b", net_spending: "200" }, // tie 200 with cat-tie-a & cat-signed
            { category_id: "cat-tie-a", net_spending: "200" }, // tie 200, ID a < b
            { category_id: "cat-5th", net_spending: "150" },   // rank 5 (150)
            { category_id: "cat-other-pos", net_spending: "80" },
            { category_id: "cat-other-neg", net_spending: "-50" }, // separate negative Other slice!
            { category_id: null, net_spending: "250" }, // uncategorised = 250 -> ranks #2!
          ],
        },
        {
          period: "2026-11", has_activity: true, is_current_month: true, income_estimate_partial: true,
          provisional: true, start_budget: "0", spent: "-50",
          categories: [{ category_id: "cat-other-neg", net_spending: "-50" }],
        },
      ],
      category_labels: [
        { category_id: "cat-top1", name: "Top One", archived: false },
        { category_id: "cat-signed", name: "Signed Swings", archived: false },
        { category_id: "cat-tie-a", name: "Tie Alpha", archived: true },
        { category_id: "cat-tie-b", name: "Tie Beta", archived: false },
        { category_id: "cat-5th", name: "Fifth Rank", archived: false },
        { category_id: "cat-other-pos", name: "Other Pos", archived: false },
        { category_id: "cat-other-neg", name: "Other Neg", archived: false },
      ],
    });
    const huge = "9".repeat(80);
    const tiny = "0." + "0".repeat(77) + "1";
    const api = fakeApi();
    api.monthlyBudgetCharts.mockResolvedValue(ok(monthlyBudgetChartsOf({
      summary: { start_budget: huge, spent: "-50", remaining: huge },
      expected_income: monthlyBudgetExpectedIncome({ status: "partial", total: tiny, items: [] }),
      pace: monthlyBudgetPace({ basis: "received_plus_expected", budget: huge, status: "no_positive_pace", daily_rate: null, projected_month_spend: null }),
    }, series12)));

    await show(api, { locale: "pl-PL" });
    await chooseHistory("12");
    expect(api.monthlyBudgetCharts).toHaveBeenLastCalledWith(undefined, "PLN", 12);

    const catChart = host.querySelector('[aria-label="Wydatki według kategorii · PLN"]');
    expect(catChart?.textContent).toContain("Pozostałe");
    expect(catChart?.textContent).toContain("Tie Alpha (archiwalna)");
    // Ranking abs totals:
    // cat-top1: 7*100 + 100 + 300 = 1100 (#1)
    // null (Uncategorised): 250 (#2)
    // cat-signed: 100 + 100 = 200 (#3, "cat-signed" < "cat-tie-a")
    // cat-tie-a: 200 (#4, "cat-tie-a" < "cat-tie-b")
    // cat-tie-b: 200 (#5)
    // cat-5th: 150 -> Other!
    // cat-other-neg: |-50| + |-50| = 100 -> Other!
    // cat-other-pos: 80 -> Other!
    const catLegendText = catChart?.querySelector(".budget-chart-legend")?.textContent ?? "";
    expect(catLegendText).toContain("Top One");
    expect(catLegendText).toContain("Bez kategorii");
    expect(catLegendText).toContain("Signed Swings");
    expect(catLegendText).toContain("Tie Alpha (archiwalna)");
    expect(catLegendText).toContain("Tie Beta");
    expect(catLegendText).toContain("Pozostałe");
    expect(catLegendText).not.toContain("Fifth Rank");
    expect(catLegendText).not.toContain("Other Pos");
    expect(catLegendText).not.toContain("Other Neg");

    // Exact HTML table in Chart 2 includes ALL categories + current/provisional bucket markers!
    const catTable = catChart?.querySelector("table");
    expect(catTable?.textContent).toContain("Fifth Rank");
    expect(catTable?.textContent).toContain("Other Pos");
    expect(catTable?.textContent).toContain("Other Neg");
    expect(catTable?.textContent).toContain("Bieżący miesiąc");
    expect(catTable?.textContent).toContain("Wynik wstępny");
    expect(catTable?.textContent).toContain("100,00 PLN");

    // Chart 3 shows gap for 2025-12, incomplete + provisional + partial markers, and negative spend!
    const incChart = host.querySelector('[aria-label="Wpływy i wydatki · PLN"]');
    expect(incChart?.querySelector(".budget-chart-gap")).not.toBeNull();
    expect(incChart?.textContent).toContain("Brak zapisanych danych");
    expect(incChart?.textContent).toContain("Niepełny miesiąc");
    expect(incChart?.textContent).toContain("Niepełne dane o wpływach");
    expect(incChart?.textContent).toContain("Wynik wstępny");

    // All SVG coordinates are strictly finite & bounded inside viewBox even with 80-digit BigInt values!
    for (const svg of host.querySelectorAll("svg")) assertBoundedSvgCoordinates(svg);
    expect(host.querySelector('[aria-label="Wydatki narastająco · PLN"] svg')?.textContent).toContain("9.9e79");
  });

  it("tests pure BigInt chart math helpers for ranking ties, separate Other +/-, and zero-span scaling", () => {
    expect(alignDecimals(["10", "bad", "-2.55"])).toEqual({ scale: 2, coeffs: [1000n, 0n, -255n] });
    const zeroScale = buildYScale([0n, 0n], 16, 172);
    expect(zeroScale.toY(0n)).toBe(94);
    expect(formatTickDecimal("0", "en-US")).toBe("0");
    expect(formatTickDecimal("-123456.789", "en-US")).toBe("−123,456.78");
    expect(formatTickDecimal("9876543210123", "en-US")).toBe("9.8e12");

    const catModel = buildCategoryChartModel(
      [
        {
          period: "2026-10", has_activity: true, is_current_month: false, income_estimate_partial: false,
          provisional: false, start_budget: "0", spent: "30",
          categories: [
            { category_id: "c1", net_spending: "100" },
            { category_id: "c2", net_spending: "100" },
            { category_id: "c3", net_spending: "100" },
            { category_id: "c4", net_spending: "100" },
            { category_id: "c5", net_spending: "100" },
            { category_id: "c6", net_spending: "50" },
            { category_id: "c7", net_spending: "-70" },
            { category_id: "c0-zero", net_spending: "0" },
          ],
        },
      ],
      [
        { category_id: "c1", name: "C1", archived: false },
        { category_id: "c2", name: "C2", archived: false },
      ],
      { uncategorised: "Uncategorised", unknownCategory: "Category", otherCategory: "Other" },
    );
    expect(catModel.displayed.map((c) => c.id)).toEqual(["c1", "c2", "c3", "c4", "c5"]);
    expect(catModel.hasOtherPositive).toBe(true);
    expect(catModel.hasOtherNegative).toBe(true);
    expect(catModel.stacks[0]?.posTotal).toBe(550n);
    expect(catModel.stacks[0]?.negTotal).toBe(-70n);
    expect(catModel.allCategories.map((c) => c.id)).toContain("c0-zero");
  });

  it("renders pass-through legs, uncovered mortgage, category limits table, and Unavailable card exposure in Polish and English", async () => {
    const api = fakeApi();
    api.monthlyBudgetCharts.mockResolvedValue(ok(monthlyBudgetChartsOf({
      pass_through: monthlyBudgetPassThrough({
        rent_received: "2500",
        mortgage_debits: "3200",
        mortgage_refunds: "150",
        unapplied_mortgage_refunds: "50",
        matched: "2500",
        unapplied_rent: "0",
        uncovered_mortgage: "600",
      }),
      category_spend: [
        monthlyBudgetCategorySpend({
          category_id: "cat-mortgage",
          name: "Synthetic Mortgage",
          expenses: "700",
          refunds: "100",
          net_spending: "600",
          limit: monthlyBudgetCategoryLimit({
            monthly_limit: "500",
            remaining: "0",
            over_by: "100",
            percent_used_floor: "99999999999999999999999",
            warning_percent: 80,
            status: "exceeded",
          }),
        }),
        monthlyBudgetCategorySpend({
          category_id: "cat-lookup",
          name: null,
          archived: true,
          expenses: "200",
          refunds: "0",
          net_spending: "200",
          limit: monthlyBudgetCategoryLimit({
            monthly_limit: "500",
            remaining: "300",
            over_by: "0",
            percent_used_floor: "40",
            warning_percent: 80,
            status: "within",
          }),
        }),
        monthlyBudgetCategorySpend({
          category_id: "cat-missing",
          name: null,
          expenses: "10",
          refunds: "0",
          net_spending: "10",
          limit: monthlyBudgetCategoryLimit({
            monthly_limit: "12",
            remaining: "2",
            over_by: "0",
            percent_used_floor: "83",
            warning_percent: 80,
            status: "approaching",
          }),
        }),
        monthlyBudgetCategorySpend({
          category_id: "cat-exact",
          name: "Synthetic Exact",
          expenses: "100",
          refunds: "0",
          net_spending: "100",
          limit: monthlyBudgetCategoryLimit({
            monthly_limit: "100",
            remaining: "0",
            over_by: "0",
            percent_used_floor: "100",
            warning_percent: 80,
            status: "at_limit",
          }),
        }),
        monthlyBudgetCategorySpend({
          category_id: null,
          name: null,
          expenses: "50",
          refunds: "0",
          net_spending: "50",
          limit: null,
        }),
      ],
      card_exposure: { status: "unavailable", amount: null, as_of_date: null },
    })));
    await show(api, { locale: "pl-PL" });

    const passThroughPl = host.querySelector('[aria-label="Najem i rata"]');
    expect(passThroughPl?.textContent).toContain("Synthetic Rent → Synthetic Mortgage");
    expect(passThroughPl?.textContent).toContain("2\u00a0500,00 PLN");
    expect(passThroughPl?.textContent).toContain("3\u00a0200,00 PLN");
    expect(passThroughPl?.textContent).toContain("150,00 PLN");
    expect(passThroughPl?.textContent).toContain("50,00 PLN");
    expect(passThroughPl?.textContent).toContain("Niepokryta część raty600,00 PLN");

    const cardPl = host.querySelector('[aria-label="Niespłacone wydatki kartą"]');
    expect(cardPl?.textContent).toContain("Niedostępne");

    const tableRegion = host.querySelector('[role="region"][aria-label="Wydatki i limity"]');
    expect(tableRegion?.getAttribute("tabindex")).toBe("0");
    expect(tableRegion?.textContent).toContain("Synthetic Mortgage");
    expect(tableRegion?.textContent).toContain("99999999999999999999999%");
    expect(tableRegion?.textContent).toContain("Przekroczony");
    expect(tableRegion?.textContent).toContain("Synthetic Lookup (archiwalna)");
    expect(tableRegion?.textContent).toContain("Kategoria");
    expect(tableRegion?.textContent).toContain("Bez kategorii");
    const progressBars = [...(tableRegion?.querySelectorAll("progress") ?? [])];
    expect(progressBars[0]?.value).toBe(100);
    expect(progressBars[1]?.value).toBe(40);

    // Also test pass-through when refunds=0 and categories fallback when categories() fails.
    api.categories.mockResolvedValue(fail(500, "budget_internal_error"));
    api.monthlyBudgetCharts.mockResolvedValue(ok(monthlyBudgetChartsOf({
      pass_through: monthlyBudgetPassThrough(),
      category_spend: [monthlyBudgetCategorySpend()],
    })));
    await show(api, { locale: "en-US", revision: 1 });
    const passThroughEn = host.querySelector('[aria-label="Rent and mortgage"]');
    expect(passThroughEn?.textContent).toContain("Rent → Mortgage");
    expect(passThroughEn?.textContent).toContain("2,000.00 PLN");
    expect(passThroughEn?.textContent).toContain("3,200.00 PLN");
    expect(passThroughEn?.textContent).toContain("Uncovered mortgage1,200.00 PLN");
    expect(passThroughEn?.textContent).not.toContain("Mortgage refunds");
    expect(host.querySelector('[aria-label="Unpaid card spending"]')?.textContent).toContain("Unavailable");
  });

  it("navigates months using server current_month, switches currencies and history, and links to Month view", async () => {
    const api = fakeApi();
    api.monthlyBudgetCharts.mockImplementation((period, currency = "PLN") => {
      const active = period ?? "2026-11";
      return Promise.resolve(ok(monthlyBudgetChartsOf({
        period: active,
        current_month: "2026-11",
        cutoff: active === "2026-11" ? "2026-11-22" : "2026-10-31",
        currency,
        available_currencies: ["PLN", "EUR"],
      })));
    });
    const onOpenMonth = vi.fn();
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const pushState = vi.spyOn(History.prototype, "pushState");
    const replaceState = vi.spyOn(History.prototype, "replaceState");

    await show(api, { onOpenMonth });
    expect(labelledButton("Next month").disabled).toBe(true);
    expect([...currencySelect().options].map((option) => option.value)).toEqual(["PLN", "EUR"]);
    expect(historySelect().value).toBe("6");

    await click(labelledButton("Previous month"));
    expect(api.monthlyBudgetCharts).toHaveBeenLastCalledWith("2026-10", "PLN", 6);
    expect(host.querySelector(".budget-month-nav h3")?.textContent).toBe("October 2026");
    expect(labelledButton("Next month").disabled).toBe(false);

    await chooseCurrency("EUR");
    expect(api.monthlyBudgetCharts).toHaveBeenLastCalledWith("2026-10", "EUR", 6);
    // Categories only fetched once for the same revision.
    expect(api.categories).toHaveBeenCalledTimes(1);

    await chooseHistory("12");
    expect(api.monthlyBudgetCharts).toHaveBeenLastCalledWith("2026-10", "EUR", 12);

    await click(button("View transactions"));
    expect(onOpenMonth).toHaveBeenCalledWith("2026-10", "2026-11");

    await click(labelledButton("Next month"));
    expect(api.monthlyBudgetCharts).toHaveBeenLastCalledWith("2026-11", "EUR", 12);
    expect(labelledButton("Next month").disabled).toBe(true);
    expect(setItem).not.toHaveBeenCalled();
    expect(pushState).not.toHaveBeenCalled();
    expect(replaceState).not.toHaveBeenCalled();
  });

  it("opens Month view filtered to provisional entries from the provisional pending-transfers review link", async () => {
    const api = fakeApi();
    const onOpenMonth = vi.fn();
    api.monthlyBudgetCharts.mockResolvedValue(ok(monthlyBudgetChartsOf({
      period: "2026-10",
      current_month: "2026-11",
      cutoff: "2026-10-31",
      currency: "EUR",
      coverage: {
        ledger_as_of: 1_795_345_200,
        stale_selected_accounts: 0,
        purchase_date_fallback_count: 0,
        pending_transfer_rows: 4,
        pending_row_keys: ["acc-1/p1", "acc-1/p2", "acc-1/p3", "acc-1/p4"],
        transfer_scan_limited: false,
        provisional: true,
        has_activity: true,
      },
    })));
    await show(api, { onOpenMonth });
    const links = [...host.querySelectorAll("button")].filter((item) => item.textContent === "View transactions");
    expect(links).toHaveLength(2);
    await click(links[0] as HTMLButtonElement);
    expect(onOpenMonth).toHaveBeenNthCalledWith(1, "2026-10", "2026-11");
    await click(links[1] as HTMLButtonElement);
    expect(onOpenMonth).toHaveBeenNthCalledWith(2, "2026-10", "2026-11", {
      currency: "EUR",
      provisionalKeys: ["acc-1/p1", "acc-1/p2", "acc-1/p3", "acc-1/p4"],
      provisionalTotal: 4,
      provisionalTruncated: false,
    });
    expect(host.textContent).not.toContain("Showing first");
  });

  it("shows a truncation note without leaking row keys when pending_row_keys is capped at 500", async () => {
    const keys = Array.from({ length: 500 }, (_, idx) => `acc-secret-id/ref-secret-${String(idx)}`);
    const api = fakeApi();
    const onOpenMonth = vi.fn();
    api.monthlyBudgetCharts.mockResolvedValue(ok(monthlyBudgetChartsOf({
      period: "2026-11",
      current_month: "2026-11",
      currency: "PLN",
      coverage: {
        ledger_as_of: 1_795_345_200,
        stale_selected_accounts: 0,
        purchase_date_fallback_count: 0,
        pending_transfer_rows: 505,
        pending_row_keys: keys,
        pending_row_keys_truncated: true,
        transfer_scan_limited: false,
        provisional: true,
        has_activity: true,
      },
    })));
    await show(api, { locale: "pl-PL", onOpenMonth });
    expect(host.textContent).toContain("Pokazano pierwsze 500 z 505.");
    expect(host.textContent).not.toContain("acc-secret-id");
    expect(host.textContent).not.toContain("ref-secret-0");

    const links = [...host.querySelectorAll("button")].filter((item) => item.textContent === "Zobacz transakcje");
    await click(links[1] as HTMLButtonElement);
    expect(onOpenMonth).toHaveBeenNthCalledWith(1, "2026-11", "2026-11", {
      currency: "PLN",
      provisionalKeys: keys,
      provisionalTotal: 505,
      provisionalTruncated: true,
    });
  });

  it("drops stale responses and refuses mismatched period responses", async () => {
    const slow = deferred<AnalyticsMonthlyBudgetCharts>();
    const api = fakeApi();
    api.monthlyBudgetCharts
      .mockReturnValueOnce(slow.promise)
      .mockResolvedValueOnce(ok(monthlyBudgetChartsOf({
        currency: "EUR",
        summary: { start_budget: "999", spent: "100", remaining: "899" },
      })));
    await act(async () => {
      root.render(
        <MonthBudgetView api={api} accounts={accounts} text={monthBudgetLabels.en} locale="en-US"
          revision={0} onAccessLoss={() => false} onOpenMonth={() => undefined} />,
      );
      await Promise.resolve();
    });
    await chooseCurrency("EUR");
    expect(host.textContent).toContain("999.00 EUR");

    await act(async () => {
      slow.resolve(ok(monthlyBudgetChartsOf({ summary: { start_budget: "111", spent: "10", remaining: "101" } })));
      await Promise.resolve();
    });
    await settle();
    expect(host.textContent).toContain("999.00 EUR");
    expect(host.textContent).not.toContain("111.00 PLN");

    // Mismatched period response is treated as error.
    api.monthlyBudgetCharts.mockResolvedValueOnce(ok(monthlyBudgetChartsOf({ period: "2026-11", current_month: "2026-11" })));
    await click(labelledButton("Previous month"));
    expect(host.querySelector('[role="alert"]')?.textContent).toBe(monthBudgetLabels.en.error);
    expect(host.textContent).not.toContain("999.00 EUR");
  });

  it("runs access-loss barrier on superseded monthlyBudgetCharts and categories responses", async () => {
    const slowBudget = deferred<AnalyticsMonthlyBudgetCharts>();
    const slowCats = deferred<{ categories: AnalyticsCategory[] }>();
    const api = fakeApi();
    api.monthlyBudgetCharts
      .mockReturnValueOnce(slowBudget.promise)
      .mockResolvedValueOnce(ok(monthlyBudgetChartsOf({ currency: "EUR" })));
    api.categories.mockReturnValueOnce(slowCats.promise);

    const barrier = vi.fn((result: AccessResult) => !result.ok && result.status === 401);
    await act(async () => {
      root.render(
        <MonthBudgetView api={api} accounts={accounts} text={monthBudgetLabels.en} locale="en-US"
          revision={0} onAccessLoss={barrier} onOpenMonth={() => undefined} />,
      );
      await Promise.resolve();
    });
    await chooseCurrency("EUR");

    const denied = { ok: false as const, status: 401, error: "budget_http_401" };
    await act(async () => { slowBudget.resolve(denied); await Promise.resolve(); });
    await settle();
    expect(barrier).toHaveBeenCalledWith(denied);

    // Revision bump supersedes in-flight categories read, yet 401 still passes through barrier.
    await show(api, { revision: 1, onAccessLoss: barrier });
    await act(async () => { slowCats.resolve(denied); await Promise.resolve(); });
    await settle();
    expect(barrier).toHaveBeenCalledWith(denied);

    // Unmounting while categories is in-flight ignores late non-access-loss response.
    const lateCats = deferred<{ categories: AnalyticsCategory[] }>();
    api.categories.mockReturnValueOnce(lateCats.promise);
    await show(api, { revision: 2, onAccessLoss: barrier });
    await act(async () => { root.unmount(); await Promise.resolve(); });
    root = createRoot(host);
    await act(async () => { lateCats.resolve(ok({ categories: categoryList })); await Promise.resolve(); });
    await settle();
  });

  it("never exposes category IDs or remittance text anywhere in the DOM or chart SVG", async () => {
    const secretRentId = "cat-secret-rent-id-9a1b";
    const secretMortgageId = "cat-secret-mortgage-id-8c2d";
    const secretSalaryId = "cat-secret-salary-id-7e3f";
    const secretSpendId = "cat-secret-spend-id-6g4h";
    const api = fakeApi();
    api.categories.mockResolvedValue(ok({
      categories: [
        { id: secretRentId, name: "Synthetic Rent Name", archived: false, version: 1, created_at: 1, updated_at: 1 },
        { id: secretMortgageId, name: "Synthetic Mortgage Name", archived: false, version: 1, created_at: 1, updated_at: 1 },
        { id: secretSalaryId, name: "Synthetic Salary Name", archived: false, version: 1, created_at: 1, updated_at: 1 },
        { id: secretSpendId, name: "Synthetic Spend Name", archived: false, version: 1, created_at: 1, updated_at: 1 },
      ],
    }));
    api.monthlyBudgetCharts.mockResolvedValue(ok(monthlyBudgetChartsOf({
      expected_income: monthlyBudgetExpectedIncome({
        status: "estimated",
        total: "5000",
        items: [{ category_id: secretSalaryId, amount: "5000", expected_date: "2026-11-28", source: "previous_month" }],
      }),
      pass_through: monthlyBudgetPassThrough({
        rent_category_id: secretRentId,
        mortgage_category_id: secretMortgageId,
      }),
      category_spend: [
        monthlyBudgetCategorySpend({ category_id: secretSpendId, name: "Synthetic Spend Name" }),
      ],
    }, {
      months: [
        {
          period: "2026-11", has_activity: true, is_current_month: true, income_estimate_partial: false,
          provisional: false, start_budget: "5000", spent: "320.5",
          categories: [{ category_id: secretSpendId, net_spending: "320.5" }],
        },
      ],
      category_labels: [{ category_id: secretSpendId, name: "Synthetic Spend Name", archived: false }],
    })));
    await show(api);

    for (const secret of [secretRentId, secretMortgageId, secretSalaryId, secretSpendId]) {
      expect(host.textContent).not.toContain(secret);
      expect(host.innerHTML).not.toContain(secret);
    }
    expect(host.textContent).toContain("Synthetic Rent Name");
    expect(host.textContent).toContain("Synthetic Mortgage Name");
    expect(host.textContent).toContain("Synthetic Salary Name");
    expect(host.textContent).toContain("Synthetic Spend Name");
  });
});
