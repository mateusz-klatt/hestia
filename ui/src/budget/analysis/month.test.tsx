// Synthetic data only: no real account, transaction or bank values.
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BudgetResult } from "../api";
import { account } from "../fixtures";
import type { MonthSummary } from "./model";
import { MonthView, monthLabels } from "./month";

type Answer = BudgetResult<MonthSummary>;

function summaryOf(month: string, changes: Partial<MonthSummary> = {}): MonthSummary {
  return {
    month, accounts: { selected: 2, excluded: 1 }, partial: false, stale_account_ids: [],
    currencies: [
      { currency: "EUR", income: "0", spending: "89.9", net: "-89.9", income_count: 0, spending_count: 1 },
      { currency: "PLN", income: "123456789012345678901234567890.01", spending: "0.000001", net: "123456789012345678901234567890.009999",
        income_count: 3, spending_count: 1 },
    ],
    not_counted: { pending: 2, uncertain: 0, unresolved: 1 },
    ...changes,
  };
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

function show(summary: (month: string) => Promise<Answer>, options: { revision?: number; onAccessLoss?: () => boolean } = {}) {
  const onAccessLoss = vi.fn(options.onAccessLoss ?? (() => false));
  root.render(<MonthView summary={summary} accounts={accounts} text={monthLabels.en} locale="en"
    revision={options.revision ?? 0} onAccessLoss={onAccessLoss} today={() => new Date(2026, 10, 21, 12)} />);
  return onAccessLoss;
}

function button(label: string): HTMLButtonElement {
  const found = host.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`);
  if (found === null) throw new Error(`button ${label} missing`);
  return found;
}

describe("month view", () => {
  it("shows server totals per currency without adding or converting anything", async () => {
    const summary = vi.fn((month: string) => Promise.resolve<Answer>({ ok: true, data: summaryOf(month) }));
    await act(async () => { show(summary); await Promise.resolve(); });
    await settle();
    expect(summary).toHaveBeenCalledWith("2026-11");
    expect(host.querySelector("h3")?.textContent).toBe("November 2026");
    const cards = [...host.querySelectorAll(".budget-total")];
    expect(cards.map((card) => card.getAttribute("aria-label"))).toEqual(["EUR", "PLN"]);
    expect(cards[0]?.textContent).toContain("−89.90 EUR");
    expect(cards[1]?.textContent).toContain("123,456,789,012,345,678,901,234,567,890.01 PLN");
    expect(cards[1]?.textContent).toContain("0.000001 PLN");
    expect(cards[1]?.textContent).toContain("3 entries");
    expect(host.textContent).toContain("2 accounts in the budget (1 excluded)");
    expect(host.textContent).toContain("still in progress");
    expect(host.textContent).toContain("still count as income and spending");
    expect(host.textContent).toContain("Not counted: 2 pending · 1 unrecognised");
    expect(host.textContent).not.toContain("uncertain");
    expect(button("Next month").disabled).toBe(true);
  });

  it("moves between months and never past the current one", async () => {
    const summary = vi.fn((month: string) => Promise.resolve<Answer>({ ok: true, data: summaryOf(month) }));
    await act(async () => { show(summary); await Promise.resolve(); });
    await settle();
    await act(async () => { button("Previous month").click(); await Promise.resolve(); });
    await settle();
    expect(summary).toHaveBeenLastCalledWith("2026-10");
    expect(host.querySelector("h3")?.textContent).toBe("October 2026");
    expect(host.textContent).not.toContain("still in progress");
    expect(button("Next month").disabled).toBe(false);
    await act(async () => { button("Next month").click(); await Promise.resolve(); });
    await settle();
    expect(summary).toHaveBeenLastCalledWith("2026-11");
    expect(button("Next month").disabled).toBe(true);
  });

  it("drops a late answer for a month that is no longer shown", async () => {
    let late: (value: Answer) => void = () => undefined;
    const summary = vi.fn((month: string) => (month === "2026-11"
      ? new Promise<Answer>((resolve) => { late = resolve; })
      : Promise.resolve<Answer>({ ok: true, data: summaryOf(month, { currencies: [] }) })));
    await act(async () => { show(summary); await Promise.resolve(); });
    expect(host.textContent).toContain("Loading the month");
    await act(async () => { button("Previous month").click(); await Promise.resolve(); });
    await settle();
    await act(async () => { late({ ok: true, data: summaryOf("2026-11") }); await Promise.resolve(); });
    await settle();
    expect(host.querySelector("h3")?.textContent).toBe("October 2026");
    expect(host.textContent).toContain("No booked transactions in this month.");
    expect(host.querySelector(".budget-total")).toBeNull();
  });

  it("refuses totals labelled with another month and reports a failed read", async () => {
    const summary = vi.fn((month: string) => Promise.resolve<Answer>(month === "2026-11"
      ? { ok: true, data: summaryOf("2026-10") } : { ok: false, status: 500, error: "budget_internal_error" }));
    await act(async () => { show(summary); await Promise.resolve(); });
    await settle();
    expect(host.textContent).toContain("could not be loaded");
    expect(host.querySelector(".budget-total")).toBeNull();
    await act(async () => { button("Previous month").click(); await Promise.resolve(); });
    await settle();
    expect(host.textContent).toContain("could not be loaded");
  });

  it("hands every answer to the access barrier and shows nothing it rejected", async () => {
    const summary = vi.fn(() => Promise.resolve<Answer>({ ok: false, status: 401, error: "budget_http_401" }));
    let onAccessLoss: ReturnType<typeof vi.fn> = vi.fn();
    await act(async () => { onAccessLoss = show(summary, { onAccessLoss: () => true }); await Promise.resolve(); });
    await settle();
    expect(onAccessLoss).toHaveBeenCalledWith({ ok: false, status: 401, error: "budget_http_401" });
    expect(host.textContent).toContain("Loading the month");
    expect(host.textContent).not.toContain("could not be loaded");
  });

  it("names outdated accounts and marks a partial month", async () => {
    const summary = vi.fn((month: string) => Promise.resolve<Answer>({ ok: true,
      data: summaryOf(month, { partial: true, stale_account_ids: ["acc-1", "gone"], not_counted: { pending: 0, uncertain: 0, unresolved: 0 } }) }));
    await act(async () => { show(summary); await Promise.resolve(); });
    await settle();
    expect(host.querySelector("[role=note]")?.textContent).toContain("Synthetic Card ••0002, Account");
    expect(host.querySelectorAll('[aria-label="Incomplete month"]')).toHaveLength(2);
    expect(host.textContent).not.toContain("Not counted");
  });

  it("reads the shown month again after a budget reload", async () => {
    const summary = vi.fn((month: string) => Promise.resolve<Answer>({ ok: true, data: summaryOf(month) }));
    await act(async () => { show(summary); await Promise.resolve(); });
    await settle();
    await act(async () => { show(summary, { revision: 1 }); await Promise.resolve(); });
    await settle();
    expect(summary).toHaveBeenCalledTimes(2);
    expect(summary).toHaveBeenLastCalledWith("2026-11");
  });
});
