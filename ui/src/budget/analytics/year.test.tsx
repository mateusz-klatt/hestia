// Synthetic data only: no real account, transaction or bank values.
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BudgetApi, BudgetResult } from "../api";
import { overviewOf, totals } from "./fixtures";
import type { AnalyticsOverview } from "./model";
import { YearView, yearLabels } from "./year";

function yearOf(period: string, unclassified = false): AnalyticsOverview {
  return overviewOf(period, {
    currencies: [totals("EUR", { net_spending: "89.9" }), totals("PLN", { net_spending: "6120.35", income: "8450" })],
    months: [
      { month: `${period}-01`, currencies: [totals("PLN", { net_spending: "100", income: "2000", unclassified_credit: unclassified ? "5" : "0" })] },
      { month: `${period}-02`, currencies: [totals("PLN", { net_spending: "200.5" }), totals("EUR", { net_spending: "89.9" })] },
    ],
  });
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
});

async function settle(): Promise<void> {
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
}
async function show(overview: BudgetApi["overview"]) {
  const onOpenMonth = vi.fn();
  await act(async () => {
    root.render(<YearView api={{ overview }} accounts={[]} text={yearLabels.en} locale="en" revision={0} onAccessLoss={() => false}
      onOpenMonth={onOpenMonth} today={() => new Date(2026, 10, 21, 12)} />);
    await Promise.resolve();
  });
  await settle();
  return onOpenMonth;
}
function labelled(label: string): HTMLButtonElement {
  const found = host.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`);
  if (found === null) throw new Error(`button ${label} missing`);
  return found;
}
async function click(element: HTMLElement): Promise<void> {
  await act(async () => { element.click(); await Promise.resolve(); });
  await settle();
}
function rows(currency: string): string[] {
  return [...host.querySelectorAll(`table[aria-label="Months ${currency}"] tbody tr`)].map((row) => row.textContent);
}

describe("year view", () => {
  it("shows the year's totals and each month's server totals per currency", async () => {
    const overview = vi.fn((period: string) => Promise.resolve<BudgetResult<AnalyticsOverview>>({ ok: true, data: yearOf(period) }));
    await show(overview);
    expect(overview).toHaveBeenCalledWith("2026");
    expect(host.querySelector("h3")?.textContent).toBe("2026");
    expect(host.textContent).toContain("This period is still in progress.");
    expect(host.querySelector('.budget-total[aria-label="PLN"]')?.textContent).toContain("6,120.35 PLN");
    expect(rows("PLN")).toEqual(["January100.00 PLN2,000.00 PLN", "February200.50 PLN0.00 PLN"]);
    expect(rows("EUR")).toEqual(["January——", "February89.90 EUR0.00 EUR"]);
    expect(host.textContent).not.toContain("To classify");
    expect(labelled("Next year").disabled).toBe(true);
  });

  it("adds the to-classify column only for a currency where some month has such credits", async () => {
    await show((period) => Promise.resolve({ ok: true, data: yearOf(period, true) }));
    expect(rows("PLN")[0]).toBe("January100.00 PLN2,000.00 PLN5.00 PLN");
    expect(host.querySelector('table[aria-label="Months PLN"] thead')?.textContent).toContain("To classify");
    expect(rows("EUR")[0]).toBe("January——");
    expect(host.querySelector('table[aria-label="Months EUR"] thead')?.textContent).not.toContain("To classify");
  });

  it("opens a month and moves between years without passing the current one", async () => {
    const overview = vi.fn((period: string) => Promise.resolve<BudgetResult<AnalyticsOverview>>({ ok: true, data: yearOf(period) }));
    const onOpenMonth = await show(overview);
    await click(labelled("Open February"));
    expect(onOpenMonth).toHaveBeenCalledWith("2026-02");
    await click(labelled("Previous year"));
    expect(overview).toHaveBeenLastCalledWith("2025");
    expect(host.textContent).not.toContain("still in progress");
    await click(labelled("Next year"));
    expect(overview).toHaveBeenLastCalledWith("2026");
  });

  it("draws no month table for a year without months", async () => {
    await show((period) => Promise.resolve({ ok: true, data: overviewOf(period) }));
    expect(host.querySelector(".budget-year")).toBeNull();
  });
});
