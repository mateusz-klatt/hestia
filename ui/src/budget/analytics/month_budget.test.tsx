// Synthetic data only: no real account, category, amount or bank values.
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BudgetApi, BudgetResult } from "../api";
import { account } from "../fixtures";
import { categoryList, monthlyBudgetCategoryLimit, monthlyBudgetCategorySpend, monthlyBudgetExpectedIncome,
  monthlyBudgetOf, monthlyBudgetPace, monthlyBudgetPassThrough } from "./fixtures";
import { ANALYTICS_ERROR } from "./model";
import type { AnalyticsCategory, AnalyticsMonthlyBudget } from "./model";
import { MonthBudgetView, monthBudgetLabels } from "./month_budget";

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
    monthlyBudget: vi.fn<BudgetApi["monthlyBudget"]>().mockResolvedValue(ok(monthlyBudgetOf())),
    categories: vi.fn<BudgetApi["categories"]>().mockResolvedValue(ok({ categories: [
      ...categoryList,
      { id: "cat-salary", name: "Synthetic Salary", archived: false, version: 1, created_at: 1, updated_at: 1 },
      { id: "cat-rent", name: "Synthetic Rent", archived: false, version: 1, created_at: 1, updated_at: 1 },
      { id: "cat-mortgage", name: "Synthetic Mortgage", archived: false, version: 1, created_at: 1, updated_at: 1 },
      { id: "cat-lookup", name: "Synthetic Lookup", archived: false, version: 1, created_at: 1, updated_at: 1 },
    ] })),
  } satisfies Pick<BudgetApi, "monthlyBudget" | "categories">;
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
  api: Pick<BudgetApi, "monthlyBudget" | "categories">,
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
  });

  it("shows loading state initially and hides month navigation until server current_month arrives", async () => {
    const pending = deferred<AnalyticsMonthlyBudget>();
    const api = fakeApi();
    api.monthlyBudget.mockReturnValueOnce(pending.promise);
    await act(async () => {
      root.render(
        <MonthBudgetView api={api} accounts={accounts} text={monthBudgetLabels.en} locale="en-US"
          revision={0} onAccessLoss={() => false} onOpenMonth={() => undefined} />,
      );
      await Promise.resolve();
    });
    expect(host.querySelector('[role="status"]')?.textContent).toBe("Loading monthly budget…");
    expect(host.querySelector(".budget-month-nav")).toBeNull();
    expect(api.monthlyBudget).toHaveBeenCalledWith(undefined, "PLN");

    await act(async () => { pending.resolve(ok(monthlyBudgetOf())); await Promise.resolve(); });
    await settle();
    expect(host.querySelector('[role="status"]')).toBeNull();
    expect(host.querySelector(".budget-month-nav h3")?.textContent).toBe("November 2026");
  });

  it("renders unconfigured state without summary, pace, or category tables, and handles missing ledger timestamp", async () => {
    const api = fakeApi();
    api.monthlyBudget.mockResolvedValue(ok(monthlyBudgetOf({
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
    })));
    await show(api);

    expect(api.categories).not.toHaveBeenCalled();
    expect(host.textContent).toContain("Monthly budget is not configured yet");
    expect(host.textContent).toContain("No synced ledger data yet");
    expect(host.textContent).toContain("Data may be out of date · 1 stale account");
    expect(host.textContent).not.toContain("No stored data");
    expect(host.textContent).not.toContain("Budget received");
    expect(host.querySelector("table")).toBeNull();

    // Defensive check if ready configuration arrives with missing sections.
    api.monthlyBudget.mockResolvedValue(ok(monthlyBudgetOf({
      configuration: { status: "ready", source: "bootstrap" },
      summary: null,
      expected_income: null,
      pace: null,
    })));
    await show(api, { revision: 1 });
    expect(host.textContent).toContain("Monthly budget is not configured yet");

    api.monthlyBudget.mockResolvedValue(ok(monthlyBudgetOf({
      configuration: { status: "ready", source: "bootstrap" },
      expected_income: null,
      pace: null,
    })));
    await show(api, { revision: 2 });
    expect(host.textContent).toContain("Monthly budget is not configured yet");

    api.monthlyBudget.mockResolvedValue(ok(monthlyBudgetOf({
      configuration: { status: "ready", source: "bootstrap" },
      pace: null,
    })));
    await show(api, { revision: 3 });
    expect(host.textContent).toContain("Monthly budget is not configured yet");
  });

  it("distinguishes no stored rows from observed zero", async () => {
    const api = fakeApi();
    api.monthlyBudget.mockResolvedValue(ok(monthlyBudgetOf({
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
    })));
    await show(api);

    expect(host.textContent).toContain("No stored data");
    expect(host.textContent).not.toContain("zero net budget impact");

    api.monthlyBudget.mockResolvedValue(ok(monthlyBudgetOf({
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
    })));
    await show(api, { revision: 1 });

    expect(host.textContent).not.toContain("No stored data");
    expect(host.textContent).toContain("Transactions are stored for this month with zero net budget impact.");
    expect(host.textContent).toContain("No category spending recorded in this month.");
    expect(host.textContent).toContain("0.00 PLN");
  });

  it.each([
    { status: 400, error: ANALYTICS_ERROR.invalidRequest, alert: monthBudgetLabels.en.badRequest },
    { status: 413, error: ANALYTICS_ERROR.limit, alert: monthBudgetLabels.en.limit },
    { status: 500, error: "budget_internal_error", alert: monthBudgetLabels.en.error },
  ])("clears stale loaded data and shows fixed alert on $status", async ({ status, error, alert }) => {
    const api = fakeApi();
    api.monthlyBudget
      .mockResolvedValueOnce(ok(monthlyBudgetOf({
        available_currencies: ["PLN", "EUR"],
        summary: { start_budget: "8888", spent: "1234", remaining: "7654" },
        pass_through: monthlyBudgetPassThrough(),
      })))
      .mockResolvedValueOnce({ ok: false, status, error });
    await show(api);

    expect(host.textContent).toContain("8,888.00 PLN");
    expect(host.textContent).toContain("Synthetic Food");
    expect(host.textContent).toContain("Rent and mortgage");

    await chooseCurrency("EUR");
    expect(api.monthlyBudget).toHaveBeenLastCalledWith(undefined, "EUR");
    expect(host.querySelector('[role="alert"]')?.textContent).toBe(alert);
    expect(host.textContent).not.toContain("8,888.00");
    expect(host.textContent).not.toContain("Synthetic Food");
    expect(host.textContent).not.toContain("Rent and mortgage");
    expect(host.querySelector("table")).toBeNull();
  });

  it("recovers from 413 using manual currency code and rejects invalid manual submits", async () => {
    const api = fakeApi();
    api.monthlyBudget
      .mockResolvedValueOnce({ ok: false, status: 413, error: ANALYTICS_ERROR.limit })
      .mockResolvedValueOnce(ok(monthlyBudgetOf({ currency: "CHF", available_currencies: ["PLN", "CHF"] })));
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
    expect(api.monthlyBudget).toHaveBeenCalledTimes(1);

    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, "chf");
      input.dispatchEvent(new Event("input", { bubbles: true }));
      await Promise.resolve();
    });
    await click(button("Apply currency"));
    expect(api.monthlyBudget).toHaveBeenLastCalledWith(undefined, "CHF");
    expect(host.textContent).toContain("5,000.00 CHF");
  });

  it("renders booked vs expected income, provisional warnings, partial income warnings, delayed items and pace runs_out", async () => {
    const api = fakeApi();
    const bigSpent = "12345678901234567890.05";
    api.monthlyBudget.mockResolvedValue(ok(monthlyBudgetOf({
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
  });

  it("renders all expected income variants and all remaining pace variants", async () => {
    const api = fakeApi();
    // 1. partial with total=null and items=[], runs_out with exhaustion_day=null
    api.monthlyBudget.mockResolvedValue(ok(monthlyBudgetOf({
      summary: { start_budget: "100", spent: "200", remaining: "-100" },
      expected_income: monthlyBudgetExpectedIncome({ status: "partial", total: null, items: [] }),
      pace: monthlyBudgetPace({ status: "runs_out", exhaustion_day: null, daily_rate: null, projected_month_spend: null }),
    })));
    await show(api);
    expect(host.textContent).toContain("Partial income evidence — remaining expected income could not be estimated reliably.");
    expect(host.textContent).toContain("At this pace the budget will run out before month end.");
    expect(host.querySelector("progress")?.value).toBe(100);

    // 2. estimated + already_reached with day
    api.monthlyBudget.mockResolvedValue(ok(monthlyBudgetOf({
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

    // 3. unavailable + already_reached with day=null
    api.monthlyBudget.mockResolvedValue(ok(monthlyBudgetOf({
      expected_income: monthlyBudgetExpectedIncome({ status: "unavailable", total: null, items: [] }),
      pace: monthlyBudgetPace({ status: "already_reached", exhaustion_day: null }),
    })));
    await show(api, { revision: 2 });
    expect(host.textContent).toContain("Unavailable (no prior-month income data).");
    expect(host.textContent).toContain("Spending has already reached the budget.");

    // 4. none with total=null + within_budget + no_positive_pace
    api.monthlyBudget.mockResolvedValue(ok(monthlyBudgetOf({
      expected_income: monthlyBudgetExpectedIncome({ status: "none", total: null, items: [] }),
      pace: monthlyBudgetPace({ status: "no_positive_pace", daily_rate: null, projected_month_spend: null }),
    })));
    await show(api, { revision: 3 });
    expect(host.textContent).toContain("No additional income expected this month.");
    expect(host.textContent).toContain("No positive spending pace yet.");

    // 5. within_budget explicitly
    api.monthlyBudget.mockResolvedValue(ok(monthlyBudgetOf({
      pace: monthlyBudgetPace({ status: "within_budget" }),
    })));
    await show(api, { revision: 4 });
    expect(host.textContent).toContain("At this pace spending stays within the budget.");
  });

  it("renders closed / historical months with Wydano / Spent and historical pace and expected income", async () => {
    const api = fakeApi();
    api.monthlyBudget.mockResolvedValue(ok(monthlyBudgetOf({
      period: "2026-10",
      start: "2026-10-01",
      end: "2026-10-31",
      cutoff: "2026-10-31",
      current_month: "2026-11",
      expected_income: monthlyBudgetExpectedIncome({ status: "historical", total: null, items: [] }),
      pace: monthlyBudgetPace({ status: "historical", basis: "received", daily_rate: null, projected_month_spend: null }),
    })));
    await show(api, { locale: "pl-PL" });

    expect(host.querySelector(".budget-month-nav h3")?.textContent).toBe("październik 2026");
    expect(host.textContent).toContain("Wydano320,50 PLN");
    expect(host.textContent).not.toContain("Wydano do dziś");
    expect(host.textContent).toContain("Zamknięty miesiąc — tylko zaksięgowane wpływy.");
    expect(host.textContent).toContain("Zamknięty miesiąc — nie wylicza się prognozy tempa.");

    await show(api, { locale: "en-US", revision: 1 });
    expect(host.querySelector(".budget-month-nav h3")?.textContent).toBe("October 2026");
    expect(host.textContent).toContain("Spent320.50 PLN");
    expect(host.textContent).not.toContain("Spent to date");
    expect(host.textContent).toContain("Closed month — booked income only.");
    expect(host.textContent).toContain("Closed month — forward pace is not calculated.");
  });

  it("renders pass-through legs, uncovered mortgage, category limits table, and Unavailable card exposure in Polish and English", async () => {
    const api = fakeApi();
    api.monthlyBudget.mockResolvedValue(ok(monthlyBudgetOf({
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
    api.monthlyBudget.mockResolvedValue(ok(monthlyBudgetOf({
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

  it("navigates months using server current_month, switches currencies, and links to Month view", async () => {
    const api = fakeApi();
    api.monthlyBudget.mockImplementation((period, currency = "PLN") => {
      const active = period ?? "2026-11";
      return Promise.resolve(ok(monthlyBudgetOf({
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

    await click(labelledButton("Previous month"));
    expect(api.monthlyBudget).toHaveBeenLastCalledWith("2026-10", "PLN");
    expect(host.querySelector(".budget-month-nav h3")?.textContent).toBe("October 2026");
    expect(labelledButton("Next month").disabled).toBe(false);

    await chooseCurrency("EUR");
    expect(api.monthlyBudget).toHaveBeenLastCalledWith("2026-10", "EUR");
    // Categories only fetched once for the same revision.
    expect(api.categories).toHaveBeenCalledTimes(1);

    await click(button("View transactions"));
    expect(onOpenMonth).toHaveBeenCalledWith("2026-10", "2026-10-31");

    await click(labelledButton("Next month"));
    expect(api.monthlyBudget).toHaveBeenLastCalledWith("2026-11", "EUR");
    expect(labelledButton("Next month").disabled).toBe(true);
    expect(setItem).not.toHaveBeenCalled();
    expect(pushState).not.toHaveBeenCalled();
    expect(replaceState).not.toHaveBeenCalled();
  });

  it("opens Month view from the provisional pending-transfers review link", async () => {
    const api = fakeApi();
    const onOpenMonth = vi.fn();
    api.monthlyBudget.mockResolvedValue(ok(monthlyBudgetOf({
      period: "2026-11",
      cutoff: "2026-11-22",
      coverage: {
        ledger_as_of: 1_795_345_200,
        stale_selected_accounts: 0,
        purchase_date_fallback_count: 0,
        pending_transfer_rows: 1,
        transfer_scan_limited: false,
        provisional: true,
        has_activity: true,
      },
    })));
    await show(api, { onOpenMonth });
    const links = [...host.querySelectorAll("button")].filter((item) => item.textContent === "View transactions");
    expect(links).toHaveLength(2);
    await click(links[1] as HTMLButtonElement);
    expect(onOpenMonth).toHaveBeenCalledWith("2026-11", "2026-11-22");
  });

  it("drops stale responses and refuses mismatched period responses", async () => {
    const slow = deferred<AnalyticsMonthlyBudget>();
    const api = fakeApi();
    api.monthlyBudget
      .mockReturnValueOnce(slow.promise)
      .mockResolvedValueOnce(ok(monthlyBudgetOf({
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
      slow.resolve(ok(monthlyBudgetOf({ summary: { start_budget: "111", spent: "10", remaining: "101" } })));
      await Promise.resolve();
    });
    await settle();
    expect(host.textContent).toContain("999.00 EUR");
    expect(host.textContent).not.toContain("111.00 PLN");

    // Mismatched period response is treated as error.
    api.monthlyBudget.mockResolvedValueOnce(ok(monthlyBudgetOf({ period: "2026-11", current_month: "2026-11" })));
    await click(labelledButton("Previous month"));
    expect(host.querySelector('[role="alert"]')?.textContent).toBe(monthBudgetLabels.en.error);
    expect(host.textContent).not.toContain("999.00 EUR");
  });

  it("runs access-loss barrier on superseded monthlyBudget and categories responses", async () => {
    const slowBudget = deferred<AnalyticsMonthlyBudget>();
    const slowCats = deferred<{ categories: AnalyticsCategory[] }>();
    const api = fakeApi();
    api.monthlyBudget
      .mockReturnValueOnce(slowBudget.promise)
      .mockResolvedValueOnce(ok(monthlyBudgetOf({ currency: "EUR" })));
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

  it("never exposes category IDs or remittance text anywhere in the DOM", async () => {
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
    api.monthlyBudget.mockResolvedValue(ok(monthlyBudgetOf({
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
