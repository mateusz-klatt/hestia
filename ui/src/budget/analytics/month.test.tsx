// Synthetic data only: no real account, transaction or bank values.
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { loadLocale } from "../../i18n";
import type { BudgetApi, BudgetResult } from "../api";
import { annotationOf, categoryList, entriesOf, entry, overviewOf, totals, transfersOf } from "./fixtures";
import { MonthView } from "./month";

function ok<T>(data: T): Promise<BudgetResult<T>> { return Promise.resolve({ ok: true, data }); }

function fakeApi() {
  return {
    overview: vi.fn<BudgetApi["overview"]>((period) => ok(overviewOf(period, { currencies: [totals("EUR"), totals("PLN")] }))),
    analyticsTransactions: vi.fn<BudgetApi["analyticsTransactions"]>((period) => ok(entriesOf(period, [entry("e1")]))),
    analyticsTransactionsByKeys: vi.fn<BudgetApi["analyticsTransactionsByKeys"]>(() => ok(entriesOf("", [entry("e1")]))),
    categories: vi.fn<BudgetApi["categories"]>(() => ok({ categories: categoryList })),
    annotate: vi.fn<BudgetApi["annotate"]>(() => ok({ annotation: annotationOf() })),
    transfers: vi.fn<BudgetApi["transfers"]>((period) => ok(transfersOf(period))),
    decideTransfer: vi.fn<BudgetApi["decideTransfer"]>(),
  } satisfies Pick<BudgetApi,
    "overview" | "analyticsTransactions" | "analyticsTransactionsByKeys" | "categories" | "annotate" | "transfers"
    | "decideTransfer">;
}

let host: HTMLElement;
let root: Root;
beforeEach(async () => {
  await loadLocale("en");
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => { root.unmount(); await Promise.resolve(); });
  host.remove();
  await loadLocale("en");
});

async function settle(): Promise<void> {
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
}
async function show(api: ReturnType<typeof fakeApi>, initialMonth?: string): Promise<void> {
  await act(async () => {
    root.render(<MonthView api={api} accounts={[]} locale="en" revision={0}
      onAccessLoss={() => false} initialMonth={initialMonth} today={() => new Date(2026, 10, 21, 12)} />);
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

  it("opens on a month chosen elsewhere, but never on a future or invalid one", async () => {
    const api = fakeApi();
    await show(api, "2026-03");
    expect(api.overview).toHaveBeenLastCalledWith("2026-03");
    expect(host.querySelector("h3")?.textContent).toBe("March 2026");
    await act(async () => { root.unmount(); await Promise.resolve(); });
    root = createRoot(host);
    await show(api, "2027-01");
    expect(api.overview).toHaveBeenLastCalledWith("2026-11");
    await act(async () => { root.unmount(); await Promise.resolve(); });
    root = createRoot(host);
    await show(api, "nope");
    expect(api.overview).toHaveBeenLastCalledWith("2026-11");
  });

    it("re-reads the totals and the entries after a saved correction", async () => {
    const api = fakeApi();
    await show(api);
    expect(api.overview).toHaveBeenCalledTimes(1);
    await click(host.querySelector<HTMLButtonElement>(".budget-entry-category"));
    await act(async () => { host.querySelector<HTMLFormElement>(".budget-entry-editor")?.requestSubmit(); await Promise.resolve(); });
    await settle();
    expect(api.annotate).toHaveBeenCalledTimes(1);
    expect(api.overview).toHaveBeenCalledTimes(2);
    expect(api.analyticsTransactions).toHaveBeenCalledTimes(2);
    expect(api.transfers).toHaveBeenCalledTimes(2);
  });

  it("forwards initial currency and provisional keys to Transactions, scrolls into view, and drops them on month navigation", async () => {
    const scrollIntoView = vi.fn();
    const prevScroll = (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
    Element.prototype.scrollIntoView = scrollIntoView;
    try {
      const api = fakeApi();
      const pendingRow = entry("pending-1", { remittance_information: ["Synthetic Pending Item"] });
      const rows = [
        pendingRow,
        entry("explicit-1", {
          remittance_information: ["Synthetic Explicit Item"],
          annotation: { version: 1, mode: "manual", category_id: "cat-food", kind: "expense", stale: false },
          effective: { kind: "expense", category_id: "cat-food", source: "manual", rule_id: null },
        }),
      ];
      api.analyticsTransactions.mockImplementation((period) => ok(entriesOf(period, rows)));
      api.analyticsTransactionsByKeys.mockImplementation(() => ok(entriesOf("", [pendingRow])));
      await act(async () => {
        root.render(<MonthView api={api} accounts={[]} locale="en" revision={0}
          onAccessLoss={() => false} initialMonth="2026-03" initialCurrency="EUR"
          initialProvisionalKeys={["acc-1/pending-1"]} initialProvisionalTotal={505}
          initialProvisionalTruncated={true} today={() => new Date(2026, 10, 21, 12)} />);
        await Promise.resolve();
      });
      await settle();

      expect(api.analyticsTransactionsByKeys).toHaveBeenCalledWith({
        row_keys: ["acc-1/pending-1"], currency: "EUR",
      });
      const provisionalToggle = host.querySelector<HTMLInputElement>('input[name="provisional"]');
      expect(provisionalToggle?.checked).toBe(true);
      const currencySelect = [...host.querySelectorAll("label")].find((item) => item.textContent.startsWith("Currency"))?.querySelector("select");
      expect(currencySelect?.value).toBe("EUR");
      expect([...host.querySelectorAll(".budget-entry-text")].map((item) => item.textContent)).toEqual(["Synthetic Pending Item"]);
      expect(host.textContent).toContain("1 entries");
      expect(host.textContent).toContain("Showing first 1 of 505.");
      expect(scrollIntoView).toHaveBeenCalled();

      // Navigating to another month does not apply the originating month's provisional keys or truncation note.
      await click(host.querySelector<HTMLButtonElement>('button[aria-label="Previous month"]') ?? undefined);
      expect(api.analyticsTransactionsByKeys).toHaveBeenCalledTimes(1);
      expect(api.analyticsTransactions).toHaveBeenLastCalledWith("2026-02", { currency: "EUR", category: "", kind: "", query: "" });
      expect(host.querySelector<HTMLInputElement>('input[name="provisional"]')).toBeNull();
      expect(host.textContent).not.toContain("Showing first");
      expect([...host.querySelectorAll(".budget-entry-text")].map((item) => item.textContent)).toEqual([
        "Synthetic Pending Item",
        "Synthetic Explicit Item",
      ]);
    } finally {
      if (prevScroll === undefined) {
        delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
      } else {
        Element.prototype.scrollIntoView = prevScroll as typeof Element.prototype.scrollIntoView;
      }
    }
  });
});
