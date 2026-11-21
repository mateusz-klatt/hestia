// Synthetic data only: no real account, category, amount or bank values.
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BudgetApi, BudgetResult } from "../api";
import { account } from "../fixtures";
import { categoryList, spendingLimit, spendingLimitSaved, spendingLimitsOf, spendingLimitStatus } from "./fixtures";
import type { AnalyticsSpendingLimits } from "./model";
import { SpendingLimitsView, spendingLimitsLabels } from "./spending_limits";

type Settle<T> = (value: BudgetResult<T>) => void;

function ok<T>(data: T): Promise<BudgetResult<T>> { return Promise.resolve({ ok: true, data }); }
function fail<T>(status: number, error: string): Promise<BudgetResult<T>> { return Promise.resolve({ ok: false, status, error }); }
function deferred<T>(): { promise: Promise<BudgetResult<T>>; settle: Settle<T> } {
  let settle: Settle<T> = () => undefined;
  const promise = new Promise<BudgetResult<T>>((resolve) => { settle = resolve; });
  return { promise, settle };
}

const accounts = [account({
  id: "acc-1",
  display: { product: "Synthetic Main", currency: "PLN", masked_suffix: "0001", source: "offline_import", observed_at: 1, imported_at: 2 },
})];
const fixedToday = () => new Date(2026, 10, 21); // 2026-11-21

function fakeApi() {
  return {
    spendingLimits: vi.fn<BudgetApi["spendingLimits"]>((period) => ok(spendingLimitsOf(
      period,
      [spendingLimit({ id: "lim-1", category_id: "cat-food", currency: "PLN", monthly_limit: "500", version: 2 })],
      [spendingLimitStatus({ period, category_id: "cat-food", name: "Synthetic Food", currency: "PLN",
        monthly_limit: "500", expenses: "420.5", refunds: "20.5", net_spending: "400", remaining: "100", over_by: "0",
        percent_used_floor: 80, warning_percent: 80, status: "approaching" })],
    ))),
    saveSpendingLimit: vi.fn<BudgetApi["saveSpendingLimit"]>(() => ok({ limit: spendingLimitSaved({ version: 3 }) })),
    categories: vi.fn<BudgetApi["categories"]>(() => ok({ categories: categoryList })),
  } satisfies Pick<BudgetApi, "spendingLimits" | "saveSpendingLimit" | "categories">;
}

let host: HTMLElement;
let root: Root;

beforeEach(() => {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => { root.unmount(); await Promise.resolve(); });
  host.remove();
  vi.restoreAllMocks();
});

async function settle(): Promise<void> {
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
}
async function click(element: HTMLElement): Promise<void> {
  await act(async () => { element.click(); await Promise.resolve(); });
  await settle();
}
function button(label: string, scope: ParentNode = host): HTMLButtonElement {
  const found = [...scope.querySelectorAll("button")].find((item) => item.textContent === label);
  if (found === undefined) throw new Error(`button ${label} missing`);
  return found;
}
async function type(input: HTMLInputElement | null | undefined, value: string): Promise<void> {
  await act(async () => {
    if (input === null || input === undefined) throw new Error("input missing");
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    await Promise.resolve();
  });
}
async function choose(select: HTMLSelectElement | null | undefined, value: string): Promise<void> {
  await act(async () => {
    if (select === null || select === undefined) throw new Error("select missing");
    select.value = value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    await Promise.resolve();
  });
}

describe("SpendingLimitsView", () => {
  it("renders exact decimal statuses, progress, exceeded warning and Polish labels without browser storage", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const exactLimit = "12345678901234567890.05";
    const api = fakeApi();
    api.spendingLimits.mockImplementation((period) => ok(spendingLimitsOf(
      period,
      [spendingLimit({ id: "lim-big", monthly_limit: exactLimit, currency: "PLN" })],
      [spendingLimitStatus({
        period, monthly_limit: exactLimit, expenses: "12345678901234567900.05", refunds: "5",
        net_spending: "12345678901234567895.05", remaining: "0", over_by: "5",
        percent_used_floor: 100, warning_percent: 80, status: "exceeded",
      })],
    )));

    await act(async () => {
      root.render(<SpendingLimitsView api={api} accounts={accounts} text={spendingLimitsLabels.en}
        locale="en-US" revision={1} onAccessLoss={() => false} today={fixedToday} />);
      await Promise.resolve();
    });
    await settle();

    expect(api.spendingLimits).toHaveBeenCalledWith("2026-11", undefined);
    expect(host.textContent).toContain("organisational aids only");
    expect(host.textContent).toContain("12,345,678,901,234,567,895.05 PLN of 12,345,678,901,234,567,890.05 PLN (100%)");
    expect(host.textContent).toContain("Exceeded");
    expect(host.textContent).toContain("Over by: 5.00 PLN");
    expect(host.textContent).toContain("Refunds: 5.00 PLN");
    expect(host.querySelector("progress")?.value).toBe(100);
    expect(setItem).not.toHaveBeenCalled();
    expect(window.location.search).toBe("");

    await act(async () => {
      root.render(<SpendingLimitsView api={api} accounts={accounts} text={spendingLimitsLabels.pl}
        locale="pl-PL" revision={2} onAccessLoss={() => false} today={fixedToday} />);
      await Promise.resolve();
    });
    await settle();
    expect(host.textContent).toContain("wyłącznie pomoc organizacyjna");
    expect(host.textContent).toContain("Przekroczony");
    expect(host.textContent).toContain("Przekroczono o:");
  });

  it("creates, edits, toggles with strict CAS expected_version, disables buttons in-flight and reloads on 409/413", async () => {
    const api = fakeApi();
    await act(async () => {
      root.render(<SpendingLimitsView api={api} accounts={accounts} text={spendingLimitsLabels.en}
        locale="en-US" revision={1} onAccessLoss={() => false} today={fixedToday} />);
      await Promise.resolve();
    });
    await settle();

    // Creating a duplicate (cat-food, PLN) is blocked inline before submit; changing currency to EUR succeeds with expected_version: 0.
    await click(button("New limit"));
    const form = host.querySelector("form");
    if (form === null) throw new Error("create form missing");
    expect(host.textContent).toContain("already exists");
    expect(button("Save limit", form).disabled).toBe(true);
    await type(form.querySelector('input[name="limit_currency"]'), "eur");
    await type(form.querySelector('input[name="monthly_limit"]'), "250.50");
    await type(form.querySelector('input[name="warning_percent"]'), "75");
    expect(button("Save limit", form).disabled).toBe(false);

    const inflight = deferred<{ limit: ReturnType<typeof spendingLimitSaved> }>();
    api.saveSpendingLimit.mockImplementationOnce(() => inflight.promise);
    await act(async () => { form.requestSubmit(); await Promise.resolve(); });
    expect(button("Save limit", form).disabled).toBe(true);
    expect(api.saveSpendingLimit).toHaveBeenCalledWith({
      category_id: "cat-food", currency: "EUR", monthly_limit: "250.50", warning_percent: 75, enabled: true, expected_version: 0,
    });
    await act(async () => { inflight.settle({ ok: true, data: { limit: spendingLimitSaved({ currency: "EUR" }) } }); await Promise.resolve(); });
    await settle();
    expect(host.querySelector("form")).toBeNull();
    expect(api.spendingLimits).toHaveBeenCalledTimes(2);

    // Edit existing limit sends expected_version: 2; 409 reloads & shows conflict banner.
    await click(button("Edit"));
    const editForm = host.querySelector("form");
    if (editForm === null) throw new Error("edit form missing");
    await type(editForm.querySelector('input[name="monthly_limit"]'), "600");
    api.saveSpendingLimit.mockImplementationOnce(() => fail(409, "budget_analytics_conflict"));
    await act(async () => { editForm.requestSubmit(); await Promise.resolve(); });
    await settle();
    expect(api.saveSpendingLimit).toHaveBeenLastCalledWith({
      category_id: "cat-food", currency: "PLN", monthly_limit: "600", warning_percent: 80, enabled: true, expected_version: 2,
    });
    expect(host.textContent).toContain("changed elsewhere");
    expect(host.querySelector("form")).toBeNull();
    expect(api.spendingLimits).toHaveBeenCalledTimes(3);

    // Quick disable toggle sends enabled: false with expected_version: 2; 413 shows cap message.
    api.saveSpendingLimit.mockImplementationOnce(() => fail(413, "budget_analytics_limit"));
    await click(button("Disable"));
    expect(api.saveSpendingLimit).toHaveBeenLastCalledWith({
      category_id: "cat-food", currency: "PLN", monthly_limit: "500", warning_percent: 80, enabled: false, expected_version: 2,
    });
    expect(host.textContent).toContain("Maximum number of spending limits reached.");
  });

  it("prevents enabling a limit on an archived category and guards currency/month switching against stale responses", async () => {
    const first = deferred<AnalyticsSpendingLimits>();
    const second = deferred<AnalyticsSpendingLimits>();
    const api = fakeApi();
    api.spendingLimits
      .mockImplementationOnce((period) => ok(spendingLimitsOf(
        period,
        [
          spendingLimit({ id: "lim-pln", category_id: "cat-old", currency: "PLN", enabled: false, version: 4 }),
          spendingLimit({ id: "lim-eur", category_id: "cat-food", currency: "EUR", enabled: true, version: 1 }),
        ],
        [spendingLimitStatus({ period, category_id: "cat-food", currency: "EUR" })],
      )))
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise);
    const onAccessLoss = vi.fn((result: { ok: boolean; status?: number; error?: string }) => !result.ok && result.status === 401);

    await act(async () => {
      root.render(<SpendingLimitsView api={api} accounts={accounts} text={spendingLimitsLabels.en}
        locale="en-US" revision={1} onAccessLoss={onAccessLoss} today={fixedToday} />);
      await Promise.resolve();
    });
    await settle();

    // Archived disabled limit cannot be quick-enabled.
    expect(button("Enable").disabled).toBe(true);

    // Next month button disabled on current month; previous month shifts to 2026-10.
    const nextBtn = host.querySelector<HTMLButtonElement>('button[aria-label="Next month"]');
    const prevBtn = host.querySelector<HTMLButtonElement>('button[aria-label="Previous month"]');
    expect(nextBtn?.disabled).toBe(true);

    // Switch currency PLN -> then previous month (superseding PLN read).
    await click(button("PLN"));
    expect(api.spendingLimits).toHaveBeenLastCalledWith("2026-11", "PLN");
    if (prevBtn === null) throw new Error("prev month missing");
    await click(prevBtn);
    expect(api.spendingLimits).toHaveBeenLastCalledWith("2026-10", "PLN");

    // Newer 2026-10 response settles first.
    await act(async () => {
      second.settle({ ok: true, data: spendingLimitsOf("2026-10", [], []) });
      await Promise.resolve();
    });
    expect(host.textContent).toContain("No spending limits configured in PLN.");

    // Stale 2026-11 response arrives late with 401 -> still trips access barrier!
    await act(async () => {
      first.settle({ ok: false, status: 401, error: "budget_http_401" });
      await Promise.resolve();
    });
    expect(onAccessLoss).toHaveBeenCalledWith({ ok: false, status: 401, error: "budget_http_401" });
  });

  it("handles empty categories, error state and selecting an active category cleanly", async () => {
    const api = fakeApi();
    api.categories.mockImplementationOnce(() => ok({ categories: [] }));
    api.spendingLimits.mockImplementationOnce((period) => ok(spendingLimitsOf(period, [], [])));

    await act(async () => {
      root.render(<SpendingLimitsView api={api} accounts={accounts} text={spendingLimitsLabels.en}
        locale="en-US" revision={1} onAccessLoss={() => false} today={fixedToday} />);
      await Promise.resolve();
    });
    await settle();
    expect(host.textContent).toContain("Create at least one active category");
    expect(host.textContent).toContain("No monthly spending limits configured yet.");

    // Error state
    api.spendingLimits.mockImplementationOnce(() => fail(500, "budget_internal_error"));
    await act(async () => {
      root.render(<SpendingLimitsView api={api} accounts={accounts} text={spendingLimitsLabels.en}
        locale="en-US" revision={2} onAccessLoss={() => false} today={fixedToday} />);
      await Promise.resolve();
    });
    await settle();
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("could not be loaded");

    // Category select change inside create form
    api.categories.mockImplementationOnce(() => ok({ categories: [
      ...categoryList,
      { id: "cat-home", name: "Synthetic Home", archived: false, version: 1, created_at: 1, updated_at: 1 },
    ] }));
    api.spendingLimits.mockImplementationOnce((period) => ok(spendingLimitsOf(period, [], [])));
    await act(async () => {
      root.render(<SpendingLimitsView api={api} accounts={accounts} text={spendingLimitsLabels.en}
        locale="en-US" revision={3} onAccessLoss={() => false} today={fixedToday} />);
      await Promise.resolve();
    });
    await settle();
    await click(button("New limit"));
    const select = host.querySelector<HTMLSelectElement>('select[name="limit_category"]');
    await choose(select, "cat-home");
    expect(select?.value).toBe("cat-home");
    await click(button("Cancel"));
    expect(host.querySelector("form")).toBeNull();
  });
});
