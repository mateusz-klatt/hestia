// Synthetic data only: no real account, transaction or bank values.
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BudgetApi, BudgetResult } from "../api";
import { account } from "../fixtures";
import { categoryList, entry } from "./fixtures";
import type { AnalyticsTransaction, AnalyticsTransactions } from "./model";
import { TransactionsPanel, transactionLabels } from "./transactions";

function ok<T>(data: T): Promise<BudgetResult<T>> { return Promise.resolve({ ok: true, data }); }
function fail<T>(status: number, error: string): Promise<BudgetResult<T>> { return Promise.resolve({ ok: false, status, error }); }
function pageOf(rows: AnalyticsTransaction[], cursor: string | null = null): AnalyticsTransactions {
  return { transactions: rows, total: rows.length + (cursor === null ? 0 : 10), next_cursor: cursor };
}

function fakeApi(rows: AnalyticsTransaction[] = [entry("e1")]) {
  return {
    analyticsTransactions: vi.fn<BudgetApi["analyticsTransactions"]>(() => ok(pageOf(rows))),
    categories: vi.fn<BudgetApi["categories"]>(() => ok({ categories: categoryList })),
    annotate: vi.fn<BudgetApi["annotate"]>(() => ok({ transaction: rows[0] ?? entry("x") })),
  } satisfies Pick<BudgetApi, "analyticsTransactions" | "categories" | "annotate">;
}

const accounts = [account({ id: "acc-1", display: { product: "Synthetic Current", currency: "PLN", masked_suffix: "0001",
  source: "offline_import", observed_at: 1, imported_at: 2 } })];

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

interface Options { period?: string; revision?: number; lost?: boolean }
async function show(api: ReturnType<typeof fakeApi>, options: Options = {}) {
  const onAccessLoss = vi.fn((result: { ok: boolean }) => options.lost === true && !result.ok);
  const onChanged = vi.fn();
  await act(async () => {
    root.render(<TransactionsPanel api={api} period={options.period ?? "2026-11"} currencies={["EUR", "PLN"]}
      accounts={accounts} text={transactionLabels.en} locale="en" revision={options.revision ?? 0}
      onAccessLoss={onAccessLoss} onChanged={onChanged} />);
    await Promise.resolve();
  });
  await settle();
  return { onAccessLoss, onChanged };
}

function button(text: string, scope: ParentNode = host): HTMLButtonElement {
  const found = [...scope.querySelectorAll("button")].find((item) => item.textContent === text);
  if (found === undefined) throw new Error(`button ${text} missing`);
  return found;
}
async function click(element: HTMLElement): Promise<void> {
  await act(async () => { element.click(); await Promise.resolve(); });
  await settle();
}
function select(label: string, scope: ParentNode = host): HTMLSelectElement {
  const found = [...scope.querySelectorAll("label")].find((item) => item.textContent.startsWith(label))?.querySelector("select");
  if (found === null || found === undefined) throw new Error(`select ${label} missing`);
  return found;
}
async function choose(element: HTMLSelectElement, value: string): Promise<void> {
  await act(async () => {
    element.value = value;
    element.dispatchEvent(new Event("change", { bubbles: true }));
    await Promise.resolve();
  });
}
async function submit(form: HTMLFormElement | null): Promise<void> {
  await act(async () => { form?.requestSubmit(); await Promise.resolve(); });
  await settle();
}

describe("period entries", () => {
  it("lists the server's rows with category, source, kind, transfer and stale facts", async () => {
    const api = fakeApi([
      entry("e1", { counterparty_name: "Synthetic Grocer", effective: { category_id: "cat-food", kind: "expense", source: "rule" } }),
      entry("e2", { credit_debit_indicator: "CRDT", amount: "40", effective: { category_id: null, kind: "unclassified_credit", source: "uncategorised" },
        transfer_state: "suggested" }),
      entry("e3", { annotation: { mode: "manual", kind: "refund", category_id: "cat-old", version: 2, stale: true },
        effective: { category_id: "cat-old", kind: "refund", source: "manual" }, transfer_state: "review" }),
    ]);
    await show(api);
    expect(api.analyticsTransactions).toHaveBeenCalledWith("2026-11", { currency: "", category: "", kind: "", query: "" });
    const rows = [...host.querySelectorAll(".budget-entry")].map((row) => row.textContent);
    expect(rows[0]).toContain("Synthetic Grocer");
    expect(rows[0]).toContain("−12.50 PLN");
    expect(rows[0]).toContain("Synthetic Current ••0001");
    expect(rows[0]).toContain("Synthetic Food");
    expect(rows[0]).toContain("by rule");
    expect(rows[1]).toContain("Synthetic shop e2");
    expect(rows[1]).toContain("40.00 PLN");
    expect(rows[1]).toContain("Uncategorised");
    expect(rows[1]).toContain("To classify");
    expect(rows[1]).toContain("possible own transfer");
    expect(rows[2]).toContain("Synthetic Old");
    expect(rows[2]).toContain("Refund");
    expect(rows[2]).toContain("set by you");
    expect(rows[2]).toContain("own transfer to re-check");
    expect(rows[2]).toContain("the bank changed this entry");
    expect(host.textContent).toContain("3 entries");
  });

  it("applies filters only on submit and offers active categories plus uncategorised", async () => {
    const api = fakeApi();
    await show(api);
    const options = [...select("Category").options].map((option) => option.value);
    expect(options).toEqual(["", "uncategorised", "cat-food"]);
    await choose(select("Currency"), "EUR");
    await choose(select("Category"), "uncategorised");
    await choose(select("Type"), "refund");
    const search = host.querySelector<HTMLInputElement>('input[type="search"]');
    await act(async () => {
      if (search === null) throw new Error("search missing");
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(search, "shop");
      search.dispatchEvent(new Event("input", { bubbles: true }));
      await Promise.resolve();
    });
    expect(api.analyticsTransactions).toHaveBeenCalledTimes(1);
    await submit(host.querySelector(".budget-range"));
    expect(api.analyticsTransactions).toHaveBeenLastCalledWith("2026-11", { currency: "EUR", category: "uncategorised", kind: "refund", query: "shop" });
  });

  it("loads further pages with the server cursor and keeps the filter", async () => {
    const api = fakeApi();
    api.analyticsTransactions.mockImplementationOnce(() => ok(pageOf([entry("e1")], "cursor-1")))
      .mockImplementationOnce(() => ok(pageOf([entry("e2")])));
    await show(api);
    await click(button("Load more"));
    expect(api.analyticsTransactions).toHaveBeenLastCalledWith("2026-11", { currency: "", category: "", kind: "", query: "" }, "cursor-1");
    expect(host.querySelectorAll(".budget-entry")).toHaveLength(2);
    expect(host.textContent).not.toContain("Load more");
  });

  it("saves a first manual category with version 0 and tells the view to re-read", async () => {
    const api = fakeApi();
    const { onChanged } = await show(api);
    await click(button("Change"));
    const editor = host.querySelector(".budget-entry-editor");
    if (editor === null) throw new Error("editor missing");
    expect([...select("Category", editor).options].map((option) => option.value)).toEqual(["auto", "none", "cat:cat-food"]);
    await choose(select("Category", editor), "cat:cat-food");
    await choose(select("Type", editor), "expense");
    await submit(host.querySelector(".budget-entry-editor"));
    expect(api.annotate).toHaveBeenCalledWith({ account_id: "acc-1", entry_reference: "e1", mode: "manual",
      category_id: "cat-food", kind: "expense", expected_version: 0 });
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(host.querySelector(".budget-entry-editor")).toBeNull();
  });

  it("distinguishes explicit uncategorised from a reset to automatic, with the seen version", async () => {
    const annotated = entry("e1", { annotation: { mode: "manual", kind: "income", category_id: "cat-old", version: 7, stale: false } });
    const api = fakeApi([annotated]);
    await show(api);
    await click(button("Change"));
    const editor = host.querySelector(".budget-entry-editor");
    if (editor === null) throw new Error("editor missing");
    // The archived category the entry already uses stays selectable for it.
    expect(select("Category", editor).value).toBe("cat:cat-old");
    expect(select("Type", editor).value).toBe("income");
    await choose(select("Category", editor), "none");
    await submit(host.querySelector(".budget-entry-editor"));
    expect(api.annotate).toHaveBeenLastCalledWith(expect.objectContaining({ mode: "manual", category_id: null, kind: "income", expected_version: 7 }));
    await click(button("Change"));
    await choose(select("Category", host.querySelector(".budget-entry-editor") ?? host), "auto");
    await choose(select("Type", host.querySelector(".budget-entry-editor") ?? host), "auto");
    await submit(host.querySelector(".budget-entry-editor"));
    expect(api.annotate).toHaveBeenLastCalledWith(expect.objectContaining({ mode: "auto", category_id: null, kind: "auto" }));
  });

  it("reloads instead of overwriting after a version conflict and explains other failures", async () => {
    const api = fakeApi();
    const { onChanged } = await show(api);
    api.annotate.mockImplementationOnce(() => fail(409, "budget_version_conflict"));
    await click(button("Change"));
    await submit(host.querySelector(".budget-entry-editor"));
    expect(host.textContent).toContain("changed elsewhere");
    expect(api.analyticsTransactions).toHaveBeenCalledTimes(2);
    expect(onChanged).not.toHaveBeenCalled();
    api.annotate.mockImplementationOnce(() => fail(400, "budget_invalid_request"));
    await click(button("Change"));
    await submit(host.querySelector(".budget-entry-editor"));
    expect(host.textContent).toContain("Could not save");
    expect(host.querySelector(".budget-entry-editor")).not.toBeNull();
    await click(button("Cancel"));
    expect(host.querySelector(".budget-entry-editor")).toBeNull();
  });

  it("hands list and save answers to the access barrier first", async () => {
    const api = fakeApi();
    api.analyticsTransactions.mockImplementation(() => fail(401, "budget_http_401"));
    const { onAccessLoss } = await show(api, { lost: true });
    expect(onAccessLoss).toHaveBeenCalledWith({ ok: false, status: 401, error: "budget_http_401" });
    expect(host.textContent).not.toContain("could not be loaded");
  });

  it("drops a late page from a previous period", async () => {
    let late: (value: BudgetResult<AnalyticsTransactions>) => void = () => undefined;
    const api = fakeApi([entry("october")]);
    api.analyticsTransactions.mockImplementationOnce(() => new Promise((resolve) => { late = resolve; }));
    await show(api);
    expect(host.textContent).toContain("Loading entries");
    await show(api, { period: "2026-10" });
    await act(async () => { late({ ok: true, data: pageOf([entry("november")]) }); await Promise.resolve(); });
    await settle();
    expect(host.textContent).toContain("Synthetic shop october");
    expect(host.textContent).not.toContain("november");
  });

  it("drops a late save once the list has moved to another period", async () => {
    let saved: (value: BudgetResult<{ transaction: AnalyticsTransaction }>) => void = () => undefined;
    const api = fakeApi();
    const first = await show(api);
    api.annotate.mockImplementationOnce(() => new Promise((resolve) => { saved = resolve; }));
    await click(button("Change"));
    await submit(host.querySelector(".budget-entry-editor"));
    expect(button("Save").disabled).toBe(true);
    await show(api, { period: "2026-10" });
    await act(async () => { saved({ ok: true, data: { transaction: entry("e1") } }); await Promise.resolve(); });
    await settle();
    expect(first.onChanged).not.toHaveBeenCalled();
    expect(host.querySelector(".budget-entry-editor")).toBeNull();
  });

  it("reports failed and empty reads plainly", async () => {
    const api = fakeApi([]);
    await show(api);
    expect(host.textContent).toContain("No entries match.");
    api.analyticsTransactions.mockImplementation(() => fail(500, "budget_internal_error"));
    api.categories.mockImplementation(() => fail(500, "budget_internal_error"));
    await show(api, { revision: 1 });
    expect(host.textContent).toContain("Entries could not be loaded.");
    expect(host.textContent).not.toContain("No entries match.");
  });
});
