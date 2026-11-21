// Synthetic data only: no real account, transaction or bank values.
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BudgetResult } from "../api";
import { account } from "../fixtures";
import { overviewOf, totals } from "./fixtures";
import type { AnalyticsOverview } from "./model";
import { OverviewPanel, overviewLabels } from "./overview";

type Answer = BudgetResult<AnalyticsOverview>;

function rich(period: string, changes: Partial<AnalyticsOverview> = {}): AnalyticsOverview {
  return overviewOf(period, {
    totals: [
      totals("EUR", { spending_gross: "89.9", net_spending: "89.9" }),
      totals("PLN", { spending_gross: "123456789012345678901234567890.01", refunds: "0.000001",
        net_spending: "123456789012345678901234567890.009999", income: "8450", unclassified_credit: "12.3",
        internal_out: "1200", internal_in: "1200" }),
    ],
    categories: [
      { currency: "PLN", category_id: "cat-food", name: "Synthetic Food", amount: "300", count: 4 },
      { currency: "PLN", category_id: null, name: null, amount: "50", count: 2 },
      { currency: "EUR", category_id: "cat-travel", name: "Synthetic Travel", amount: "89.9", count: 1 },
    ],
    scope: { selected: 2, excluded: 1, partial: false, stale_account_ids: [], pending: 2, uncertain: 0, unresolved: 1 },
    ...changes,
  });
}

const accounts = [account({ id: "acc-1", display: { product: "Synthetic Card", currency: "PLN", masked_suffix: "0002",
  source: "offline_import", observed_at: 1, imported_at: 2 } }), account({ id: "acc-2" })];

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
});

async function settle(): Promise<void> {
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
}

async function show(overview: (period: string) => Promise<Answer>,
  options: { period?: string; current?: boolean; revision?: number; lost?: boolean; onCurrencies?: (currencies: string[]) => void } = {}) {
  const onAccessLoss = vi.fn(() => options.lost === true);
  await act(async () => {
    root.render(<OverviewPanel overview={overview} period={options.period ?? "2026-11"} current={options.current ?? true}
      accounts={accounts} text={overviewLabels.en} locale="en" revision={options.revision ?? 0} onAccessLoss={onAccessLoss}
      onCurrencies={options.onCurrencies} />);
    await Promise.resolve();
  });
  await settle();
  return onAccessLoss;
}

describe("period overview", () => {
  it("shows server totals per currency without adding, subtracting or converting anything", async () => {
    const overview = vi.fn((period: string) => Promise.resolve<Answer>({ ok: true, data: rich(period) }));
    await show(overview);
    expect(overview).toHaveBeenCalledWith("2026-11");
    const cards = [...host.querySelectorAll<HTMLElement>(".budget-total")];
    expect(cards.map((card) => card.getAttribute("aria-label"))).toEqual(["EUR", "PLN"]);
    const [eur, pln] = cards;
    expect(eur?.textContent).toContain("89.90 EUR");
    expect(eur?.textContent).not.toContain("gross");
    expect(eur?.textContent).not.toContain("To classify");
    expect(eur?.textContent).not.toContain("Own transfers");
    expect(pln?.textContent).toContain("123,456,789,012,345,678,901,234,567,890.009999 PLN");
    expect(pln?.textContent).toContain("gross 123,456,789,012,345,678,901,234,567,890.01 PLN · refunds 0.000001 PLN");
    expect(pln?.textContent).toContain("To classify12.30 PLN");
    expect(pln?.textContent).toContain("out 1,200.00 PLN · in 1,200.00 PLN");
    expect(host.textContent).toContain("2 accounts in the budget (1 excluded)");
    expect(host.textContent).toContain("still in progress");
    expect(host.textContent).toContain("Not counted: 2 pending · 1 unrecognised");
    expect(host.textContent).not.toContain("uncertain");
  });

  it("lists categories under their own currency with an explicit uncategorised row", async () => {
    await show((period) => Promise.resolve<Answer>({ ok: true, data: rich(period) }));
    const pln = host.querySelector('[aria-label="Spending by category PLN"]');
    expect([...pln?.querySelectorAll("tr") ?? []].map((row) => row.textContent)).toEqual([
      "Synthetic Food300.00 PLN4 entries", "Uncategorised50.00 PLN2 entries"]);
    expect(pln?.querySelector(".is-uncategorised")).not.toBeNull();
    const eur = host.querySelector('[aria-label="Spending by category EUR"]');
    expect(eur?.textContent).toContain("Synthetic Travel");
    expect(eur?.textContent).not.toContain("Synthetic Food");
  });

  it("says how many own transfers wait for review and that they still count", async () => {
    await show((period) => Promise.resolve<Answer>({ ok: true,
      data: rich(period, { transfer_summary: { suggested: 3, review: 1, accepted: 5 } }) }));
    const notes = [...host.querySelectorAll("[role=note]")].map((note) => note.textContent);
    expect(notes).toEqual([
      "Possible own transfers to review: 3. Until you confirm them they count as spending and income.",
      "Own-transfer decisions to check again after the bank data changed: 1."]);
  });

  it("reports the shown currencies and says nothing about progress for a closed month", async () => {
    const onCurrencies = vi.fn();
    await show((period) => Promise.resolve<Answer>({ ok: true, data: rich(period) }), { current: false, onCurrencies });
    expect(onCurrencies).toHaveBeenCalledWith(["EUR", "PLN"]);
    expect(host.textContent).not.toContain("still in progress");
  });

  it("drops a late answer for a period that is no longer shown", async () => {
    let late: (value: Answer) => void = () => undefined;
    const overview = vi.fn((period: string) => (period === "2026-11"
      ? new Promise<Answer>((resolve) => { late = resolve; })
      : Promise.resolve<Answer>({ ok: true, data: overviewOf(period, { totals: [] }) })));
    await show(overview);
    expect(host.textContent).toContain("Loading the month");
    await show(overview, { period: "2026-10", current: false });
    await act(async () => { late({ ok: true, data: rich("2026-11") }); await Promise.resolve(); });
    await settle();
    expect(host.textContent).toContain("No booked transactions in this month.");
    expect(host.querySelector(".budget-total")).toBeNull();
  });

  it("refuses totals labelled with another period and reports a failed read", async () => {
    const overview = vi.fn((period: string) => Promise.resolve<Answer>(period === "2026-11"
      ? { ok: true, data: rich("2026-10") } : { ok: false, status: 500, error: "budget_internal_error" }));
    await show(overview);
    expect(host.textContent).toContain("could not be loaded");
    expect(host.querySelector(".budget-total")).toBeNull();
    await show(overview, { period: "2026-10" });
    expect(host.textContent).toContain("could not be loaded");
  });

  it("hands every answer to the access barrier and shows nothing it rejected", async () => {
    const onAccessLoss = await show(() => Promise.resolve<Answer>({ ok: false, status: 401, error: "budget_http_401" }), { lost: true });
    expect(onAccessLoss).toHaveBeenCalledWith({ ok: false, status: 401, error: "budget_http_401" });
    expect(host.textContent).toContain("Loading the month");
    expect(host.textContent).not.toContain("could not be loaded");
  });

  it("names outdated accounts and marks a partial month", async () => {
    await show((period) => Promise.resolve<Answer>({ ok: true, data: rich(period, {
      scope: { selected: 2, excluded: 0, partial: true, stale_account_ids: ["acc-1", "gone"], pending: 0, uncertain: 0, unresolved: 0 } }) }));
    expect(host.querySelector("[role=note]")?.textContent).toContain("Synthetic Card ••0002, Account");
    expect(host.querySelectorAll('[aria-label="Incomplete month"]')).toHaveLength(2);
    expect(host.textContent).not.toContain("Not counted");
    expect(host.textContent).toContain("2 accounts in the budget · change");
  });

  it("reads the shown month again after a budget reload", async () => {
    const overview = vi.fn((period: string) => Promise.resolve<Answer>({ ok: true, data: rich(period) }));
    await show(overview);
    await show(overview, { revision: 1 });
    expect(overview).toHaveBeenCalledTimes(2);
    expect(overview).toHaveBeenLastCalledWith("2026-11");
  });
});
