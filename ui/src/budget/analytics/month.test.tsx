// Synthetic data only: no real account, transaction or bank values.
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BudgetApi, BudgetResult } from "../api";
import { categoryList, entriesOf, entry, overviewOf, totals, transfersOf } from "./fixtures";
import { MonthView, monthLabels } from "./month";

function ok<T>(data: T): Promise<BudgetResult<T>> { return Promise.resolve({ ok: true, data }); }

function fakeApi() {
  return {
    overview: vi.fn<BudgetApi["overview"]>((period) => ok(overviewOf(period, { currencies: [totals("EUR"), totals("PLN")] }))),
    analyticsTransactions: vi.fn<BudgetApi["analyticsTransactions"]>((period) => ok(entriesOf(period, [entry("e1")]))),
    categories: vi.fn<BudgetApi["categories"]>(() => ok({ categories: categoryList })),
    annotate: vi.fn<BudgetApi["annotate"]>(() => ok({ transaction: entry("e1") })),
    transfers: vi.fn<BudgetApi["transfers"]>((period) => ok(transfersOf(period))),
    decideTransfer: vi.fn<BudgetApi["decideTransfer"]>(),
  } satisfies Pick<BudgetApi, "overview" | "analyticsTransactions" | "categories" | "annotate" | "transfers" | "decideTransfer">;
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
async function show(api: ReturnType<typeof fakeApi>): Promise<void> {
  await act(async () => {
    root.render(<MonthView api={api} accounts={[]} text={monthLabels.en} locale="en" revision={0}
      onAccessLoss={() => false} today={() => new Date(2026, 10, 21, 12)} />);
    await Promise.resolve();
  });
  await settle();
}
function labelled(label: string): HTMLButtonElement {
  const found = host.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`);
  if (found === null) throw new Error(`button ${label} missing`);
  return found;
}
async function click(element: HTMLElement | null | undefined): Promise<void> {
  await act(async () => { element?.click(); await Promise.resolve(); });
  await settle();
}

describe("month view", () => {
  it("reads the current month's overview and entries and offers its currencies as a filter", async () => {
    const api = fakeApi();
    await show(api);
    expect(host.querySelector("h3")?.textContent).toBe("November 2026");
    expect(api.overview).toHaveBeenCalledWith("2026-11");
    expect(api.analyticsTransactions).toHaveBeenCalledWith("2026-11", expect.anything());
    const currency = [...host.querySelectorAll("label")].find((item) => item.textContent.startsWith("Currency"))?.querySelector("select");
    expect([...currency?.options ?? []].map((option) => option.value)).toEqual(["", "EUR", "PLN"]);
    expect(labelled("Next month").disabled).toBe(true);
  });

  it("moves both panels between months and never past the current one", async () => {
    const api = fakeApi();
    await show(api);
    await click(labelled("Previous month"));
    expect(host.querySelector("h3")?.textContent).toBe("October 2026");
    expect(api.overview).toHaveBeenLastCalledWith("2026-10");
    expect(api.analyticsTransactions).toHaveBeenLastCalledWith("2026-10", expect.anything());
    expect(labelled("Next month").disabled).toBe(false);
    await click(labelled("Next month"));
    expect(api.overview).toHaveBeenLastCalledWith("2026-11");
    expect(labelled("Next month").disabled).toBe(true);
  });

  it("re-reads the totals and the entries after a saved correction", async () => {
    const api = fakeApi();
    await show(api);
    expect(api.overview).toHaveBeenCalledTimes(1);
    await click([...host.querySelectorAll("button")].find((item) => item.textContent === "Change"));
    await act(async () => { host.querySelector<HTMLFormElement>(".budget-entry-editor")?.requestSubmit(); await Promise.resolve(); });
    await settle();
    expect(api.annotate).toHaveBeenCalledTimes(1);
    expect(api.overview).toHaveBeenCalledTimes(2);
    expect(api.analyticsTransactions).toHaveBeenCalledTimes(2);
    expect(api.transfers).toHaveBeenCalledTimes(2);
  });
});
