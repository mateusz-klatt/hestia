// Synthetic data only: no real account, transaction or bank values.
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BudgetApi, BudgetResult } from "../api";
import { account } from "../fixtures";
import { annotationOf, categoryList, decisionOf, entriesOf, entry } from "./fixtures";
import type { AnalyticsTransaction, AnalyticsTransactions, AnnotationSaved, TransferDecisionResult } from "./model";
import { TRANSACTION_ROW_CAP, TransactionsPanel, transactionLabels } from "./transactions";

function ok<T>(data: T): Promise<BudgetResult<T>> { return Promise.resolve({ ok: true, data }); }
function fail<T>(status: number, error: string): Promise<BudgetResult<T>> { return Promise.resolve({ ok: false, status, error }); }
function pageOf(rows: AnalyticsTransaction[], cursor: string | null = null): AnalyticsTransactions {
  return entriesOf("2026-11", rows, cursor);
}
function pairDebit(reference = "out", changes: Partial<AnalyticsTransaction> = {}): AnalyticsTransaction {
  return entry(reference, { amount: "100.00", booking_date: "2026-11-10", ...changes });
}
function pairCredit(reference = "in", changes: Partial<AnalyticsTransaction> = {}): AnalyticsTransaction {
  return entry(reference, {
    account_id: "acc-2", amount: "0100.00", credit_debit_indicator: "CRDT", booking_date: "2026-11-11",
    metadata: { creditor_name: null, debtor_name: "Synthetic Card Repayment", mcc: null },
    effective: { kind: "income", category_id: null, source: "default", rule_id: null }, ...changes,
  });
}
function mockPairSearch(api: ReturnType<typeof fakeApi>, debit: AnalyticsTransaction, credits: AnalyticsTransaction[]): void {
  api.analyticsTransactions.mockImplementation((period, ...args) =>
    ok(args[2] === 200 ? entriesOf(period, credits) : pageOf([debit])));
}

function fakeApi(rows: AnalyticsTransaction[] = [entry("e1")]) {
  return {
    analyticsTransactions: vi.fn<BudgetApi["analyticsTransactions"]>(() => ok(pageOf(rows))),
    categories: vi.fn<BudgetApi["categories"]>(() => ok({ categories: categoryList })),
    annotate: vi.fn<BudgetApi["annotate"]>(() => ok({ annotation: annotationOf() })),
    decideTransfer: vi.fn<BudgetApi["decideTransfer"]>(() => ok({ decision: decisionOf() })),
  } satisfies Pick<BudgetApi, "analyticsTransactions" | "categories" | "annotate" | "decideTransfer">;
}

const accounts = [
  account({ id: "acc-1", display: { product: "Synthetic Current", currency: "PLN", masked_suffix: "0001",
    source: "offline_import", observed_at: 1, imported_at: 2 } }),
  account({ id: "acc-2", display: { product: "Synthetic Savings", currency: "PLN", masked_suffix: "0002",
    source: "offline_import", observed_at: 1, imported_at: 2 } }),
];

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

interface Options { period?: string; revision?: number; lost?: boolean; rowCap?: number }
async function show(api: ReturnType<typeof fakeApi>, options: Options = {}) {
  const onAccessLoss = vi.fn((result: { ok: boolean }) => options.lost === true && !result.ok);
  const onChanged = vi.fn();
  await act(async () => {
    root.render(<TransactionsPanel api={api} period={options.period ?? "2026-11"} currencies={["EUR", "PLN"]}
      accounts={accounts} text={transactionLabels.en} locale="en" revision={options.revision ?? 0}
      onAccessLoss={onAccessLoss} onChanged={onChanged} rowCap={options.rowCap} />);
    await Promise.resolve();
  });
  await settle();
  return { onAccessLoss, onChanged };
}

async function drain(times = 12): Promise<void> {
  for (let i = 0; i < times; i += 1) await settle();
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
async function applyCurrencyFilter(): Promise<void> {
  await choose(select("Currency"), "EUR");
  await submit(host.querySelector(".budget-range"));
}

describe("period entries", () => {
  it("lists the server's rows with category, source, kind, transfer and stale facts", async () => {
    const api = fakeApi([
      entry("e1", { metadata: { creditor_name: "Synthetic Grocer", debtor_name: "Synthetic Owner", mcc: "5411" },
        effective: { category_id: "cat-food", kind: "expense", source: "rule", rule_id: "rule-1" } }),
      entry("e2", { credit_debit_indicator: "CRDT", amount: "40", metadata: { creditor_name: "Synthetic Owner", debtor_name: null, mcc: null },
        effective: { category_id: null, kind: "unclassified_credit", source: "default", rule_id: null },
        transfer: { status: "suggested", decision_id: null, candidate_count: 2, ambiguous: true } }),
      entry("e3", { annotation: { mode: "manual", kind: "refund", category_id: "cat-old", version: 2, stale: true },
        effective: { category_id: "cat-old", kind: "refund", source: "manual", rule_id: null },
        transfer: { status: "review", decision_id: "dec-1", candidate_count: 1, ambiguous: false } }),
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
    expect(rows[1]).toContain("fits several pairs");
    expect(rows[1]).not.toContain("Synthetic Owner");
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
    await drain();
    expect(api.analyticsTransactions).toHaveBeenLastCalledWith("2026-11", { currency: "", category: "", kind: "", query: "" }, "cursor-1");
    expect(host.querySelectorAll(".budget-entry")).toHaveLength(2);
    expect(host.textContent).not.toContain("Loading…");
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
      category_id: "cat-food", kind: "expense", expected_fingerprint: "fp-e1", expected_version: 0 });
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(host.querySelector(".budget-entry-editor")).toBeNull();
  });

  it("distinguishes explicit uncategorised from a reset to automatic, with the seen version", async () => {
    const annotated = entry("e1", { credit_debit_indicator: "CRDT",
      annotation: { mode: "manual", kind: "income", category_id: "cat-old", version: 7, stale: false } });
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

  it("makes a type choice a manual decision that keeps the entry's current category visibly", async () => {
    const api = fakeApi([entry("e1", { effective: { category_id: "cat-old", kind: "expense", source: "rule", rule_id: "rule-1" } }),
      entry("e2")]);
    await show(api);
    const [first, second] = [...host.querySelectorAll<HTMLElement>(".budget-entry")];
    if (first === undefined || second === undefined) throw new Error("rows missing");
    await click(button("Change", first));
    let editor = first.querySelector(".budget-entry-editor") ?? host;
    expect(select("Category", editor).value).toBe("auto");
    // The archived category the entry currently shows stays available for it, marked as archived.
    expect([...select("Category", editor).options].map((option) => option.value)).toContain("cat:cat-old");
    expect([...select("Category", editor).options].find((option) => option.value === "cat:cat-old")?.textContent).toBe("Synthetic Old (archived)");
    await choose(select("Type", editor), "excluded");
    expect(select("Category", editor).value).toBe("cat:cat-old");
    await submit(first.querySelector(".budget-entry-editor"));
    expect(api.annotate).toHaveBeenLastCalledWith(expect.objectContaining({ entry_reference: "e1", mode: "manual",
      category_id: "cat-old", kind: "excluded" }));
    await click(button("Change", host.querySelectorAll<HTMLElement>(".budget-entry")[1] ?? host));
    editor = host.querySelector(".budget-entry-editor") ?? host;
    await choose(select("Type", editor), "expense");
    expect(select("Category", editor).value).toBe("none");
    // Going back to automatic hands the type back to the rules as well.
    await choose(select("Category", editor), "auto");
    expect(select("Type", editor).value).toBe("auto");
    await submit(host.querySelector(".budget-entry-editor"));
    expect(api.annotate).toHaveBeenLastCalledWith(expect.objectContaining({ entry_reference: "e2", mode: "auto", kind: "auto" }));
  });

  it("never offers an archived category from a stale manual choice that the entry no longer shows", async () => {
    const api = fakeApi([entry("e1", { annotation: { version: 4, mode: "manual", category_id: "cat-old", kind: "expense", stale: true },
      effective: { category_id: "cat-food", kind: "expense", source: "rule", rule_id: "rule-1" } })]);
    await show(api);
    await click(button("Change"));
    const options = [...select("Category", host.querySelector(".budget-entry-editor") ?? host).options].map((option) => option.value);
    expect(options).toEqual(["auto", "none", "cat:cat-food"]);
  });

  it("offers only types that fit the entry's direction and drops an incompatible stored one to automatic", async () => {
    const api = fakeApi([entry("out"), entry("in", { credit_debit_indicator: "CRDT",
      annotation: { version: 2, mode: "manual", category_id: null, kind: "expense", stale: false } })]);
    await show(api);
    const rows = [...host.querySelectorAll<HTMLElement>(".budget-entry")];
    await click(button("Change", rows[0] ?? host));
    let editor = host.querySelector(".budget-entry-editor") ?? host;
    expect([...select("Type", editor).options].map((option) => option.value)).toEqual(["auto", "expense", "excluded"]);
    await click(button("Cancel", editor));
    await click(button("Change", host.querySelectorAll<HTMLElement>(".budget-entry")[1] ?? host));
    editor = host.querySelector(".budget-entry-editor") ?? host;
    expect([...select("Type", editor).options].map((option) => option.value)).toEqual(["auto", "income", "refund", "excluded"]);
    expect(select("Type", editor).value).toBe("auto");
  });

  it("locks the editor while a save is in flight", async () => {
    let saved: (value: BudgetResult<{ annotation: AnnotationSaved }>) => void = () => undefined;
    const api = fakeApi();
    await show(api);
    api.annotate.mockImplementationOnce(() => new Promise((resolve) => { saved = resolve; }));
    await click(button("Change"));
    await submit(host.querySelector(".budget-entry-editor"));
    const editor = host.querySelector(".budget-entry-editor") ?? host;
    expect(select("Category", editor).disabled).toBe(true);
    expect(select("Type", editor).disabled).toBe(true);
    await act(async () => { saved({ ok: true, data: { annotation: annotationOf() } }); await Promise.resolve(); });
    await settle();
  });

  it("reloads instead of overwriting after a version conflict and explains other failures", async () => {
    const api = fakeApi();
    const { onChanged } = await show(api);
    api.annotate.mockImplementationOnce(() => fail(409, "budget_analytics_conflict"));
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

  it("reloads with a clear message when the bank changed the entry, and restarts on a stale cursor", async () => {
    const api = fakeApi();
    await show(api);
    api.annotate.mockImplementationOnce(() => fail(409, "budget_analytics_source_changed"));
    await click(button("Change"));
    await submit(host.querySelector(".budget-entry-editor"));
    expect(host.textContent).toContain("The bank changed this entry");
    expect(api.analyticsTransactions).toHaveBeenCalledTimes(2);
    api.analyticsTransactions.mockImplementationOnce(() => ok(pageOf([entry("e1")], "cursor-1")))
      .mockImplementationOnce(() => fail(409, "budget_analytics_stale_cursor"));
    await show(api, { revision: 1 });
    await drain();
    expect(api.analyticsTransactions).toHaveBeenLastCalledWith("2026-11", { currency: "", category: "", kind: "", query: "" });
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

  it("reports a late save without restoring an editor from another period", async () => {
    let saved: (value: BudgetResult<{ annotation: AnnotationSaved }>) => void = () => undefined;
    const api = fakeApi();
    const first = await show(api);
    api.annotate.mockImplementationOnce(() => new Promise((resolve) => { saved = resolve; }));
    await click(button("Change"));
    await submit(host.querySelector(".budget-entry-editor"));
    expect(button("Save").disabled).toBe(true);
    await show(api, { period: "2026-10" });
    await act(async () => { saved({ ok: true, data: { annotation: annotationOf() } }); await Promise.resolve(); });
    await settle();
    expect(first.onChanged).toHaveBeenCalledTimes(1);
    expect(host.querySelector(".budget-entry-editor")).toBeNull();
  });

  it("reports a successful save after a filter change without restoring stale UI", async () => {
    let saved: (value: BudgetResult<{ annotation: AnnotationSaved }>) => void = () => undefined;
    const api = fakeApi();
    const { onChanged } = await show(api);
    api.annotate.mockImplementationOnce(() => new Promise((resolve) => { saved = resolve; }));
    await click(button("Change"));
    await submit(host.querySelector(".budget-entry-editor"));
    await applyCurrencyFilter();
    await act(async () => { saved({ ok: true, data: { annotation: annotationOf() } }); await Promise.resolve(); });
    await settle();
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(host.querySelector(".budget-entry-editor")).toBeNull();
    expect(host.querySelector(".budget-notice")).toBeNull();
  });

  it("drops a failed save after a filter change without notifying or showing a notice", async () => {
    let saved: (value: BudgetResult<{ annotation: AnnotationSaved }>) => void = () => undefined;
    const api = fakeApi();
    const { onChanged } = await show(api);
    api.annotate.mockImplementationOnce(() => new Promise((resolve) => { saved = resolve; }));
    await click(button("Change"));
    await submit(host.querySelector(".budget-entry-editor"));
    await applyCurrencyFilter();
    await act(async () => { saved({ ok: false, status: 500, error: "budget_internal_error" }); await Promise.resolve(); });
    await settle();
    expect(onChanged).not.toHaveBeenCalled();
    expect(host.querySelector(".budget-notice")).toBeNull();
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

  function names(): string[] {
    return [...host.querySelectorAll(".budget-entry-text")].map((item) => item.textContent);
  }
  function header(label: string): HTMLTableCellElement {
    const found = [...host.querySelectorAll<HTMLTableCellElement>("thead th")].find((item) => item.querySelector("button")?.textContent === label);
    if (found === undefined) throw new Error(`header ${label} missing`);
    return found;
  }
  function classified(reference: string, changes: Partial<AnalyticsTransaction>): AnalyticsTransaction {
    return entry(reference, changes);
  }

  it("sorts by date, category and absolute amount and toggles direction", async () => {
    const rows = [
      classified("mid", { booking_date: "2026-11-02", amount: "10.00",
        effective: { kind: "expense", category_id: "cat-food", source: "default", rule_id: null } }),
      classified("early", { booking_date: "2026-11-01", amount: "-30.00",
        effective: { kind: "expense", category_id: null, source: "default", rule_id: null } }),
      classified("late", { booking_date: null, value_date: "2026-11-03", amount: "2.50", credit_debit_indicator: "CRDT",
        effective: { kind: "income", category_id: "cat-old", source: "default", rule_id: null } }),
    ];
    const api = fakeApi(rows);
    await show(api);
    expect(names()).toEqual(["Synthetic shop mid", "Synthetic shop early", "Synthetic shop late"]);
    expect(header("Date").getAttribute("aria-sort")).toBe("none");
    expect(header("Category").getAttribute("aria-sort")).toBe("none");
    expect(header("Amount").getAttribute("aria-sort")).toBe("none");

    await click(button("Date"));
    expect(header("Date").getAttribute("aria-sort")).toBe("ascending");
    expect(names()).toEqual(["Synthetic shop early", "Synthetic shop mid", "Synthetic shop late"]);
    await click(button("Date"));
    expect(header("Date").getAttribute("aria-sort")).toBe("descending");
    expect(names()).toEqual(["Synthetic shop late", "Synthetic shop mid", "Synthetic shop early"]);

    await click(button("Category"));
    expect(header("Date").getAttribute("aria-sort")).toBe("none");
    expect(header("Category").getAttribute("aria-sort")).toBe("ascending");
    expect(names()).toEqual(["Synthetic shop mid", "Synthetic shop late", "Synthetic shop early"]);
    await click(button("Category"));
    expect(header("Category").getAttribute("aria-sort")).toBe("descending");
    expect(names()).toEqual(["Synthetic shop early", "Synthetic shop late", "Synthetic shop mid"]);

    await click(button("Amount"));
    expect(header("Amount").getAttribute("aria-sort")).toBe("ascending");
    expect(header("Category").getAttribute("aria-sort")).toBe("none");
    // Absolute values: 2.50, 10.00, 30.00. The negative amount is not treated as smaller.
    expect(names()).toEqual(["Synthetic shop late", "Synthetic shop mid", "Synthetic shop early"]);
    await click(button("Amount"));
    expect(header("Amount").getAttribute("aria-sort")).toBe("descending");
    expect(names()).toEqual(["Synthetic shop early", "Synthetic shop mid", "Synthetic shop late"]);
  });

  it("orders equal absolute amounts by their previous order and keeps a missing date last", async () => {
    const rows = [
      classified("debit", { booking_date: "2026-11-04", amount: "40.00", credit_debit_indicator: "DBIT" }),
      classified("none", { booking_date: null, value_date: null, transaction_date: null, amount: "1.00" }),
      classified("credit", { booking_date: "2026-11-02", amount: "-40.00", credit_debit_indicator: "CRDT" }),
    ];
    const api = fakeApi(rows);
    await show(api);
    await click(button("Amount"));
    expect(names()).toEqual(["Synthetic shop none", "Synthetic shop debit", "Synthetic shop credit"]);
    await click(button("Date"));
    expect(names()).toEqual(["Synthetic shop credit", "Synthetic shop debit", "Synthetic shop none"]);
    await click(button("Date"));
    expect(names()).toEqual(["Synthetic shop debit", "Synthetic shop credit", "Synthetic shop none"]);
  });

  it("keeps fetching pages in the background and shows how many rows are in", async () => {
    const cursors: Array<string | undefined> = [];
    let release: (value: BudgetResult<AnalyticsTransactions>) => void = () => undefined;
    const api = fakeApi();
    api.analyticsTransactions.mockImplementation((_period, _filter, cursor) => {
      cursors.push(cursor);
      if (cursor === undefined) return ok(pageOf([entry("p1")], "c1"));
      if (cursor === "c1") return new Promise((resolve) => { release = resolve; });
      return ok(pageOf([entry("p3")]));
    });
    await show(api);
    expect(host.textContent).toContain("Synthetic shop p1");
    expect(host.textContent).toContain(transactionLabels.en.loadingMore(1));
    expect(transactionLabels.pl.loadingMore(4)).toBe("Wczytywanie… 4");
    await act(async () => { release({ ok: true, data: pageOf([entry("p2")], "c2") }); await Promise.resolve(); });
    await drain();
    expect(cursors).toEqual([undefined, "c1", "c2"]);
    expect(names()).toEqual(["Synthetic shop p1", "Synthetic shop p2", "Synthetic shop p3"]);
    expect(host.textContent).not.toContain("Loading…");
  });

  it("drops a background page that resolves after the period changes", async () => {
    let late: (value: BudgetResult<AnalyticsTransactions>) => void = () => undefined;
    const api = fakeApi();
    api.analyticsTransactions.mockImplementation((period, _filter, cursor) => {
      if (period === "2026-11" && cursor === undefined) return ok(pageOf([entry("first")], "c1"));
      if (period === "2026-11") return new Promise((resolve) => { late = resolve; });
      return ok(pageOf([entry("october")]));
    });
    await show(api);
    expect(host.textContent).toContain(transactionLabels.en.loadingMore(1));
    await show(api, { period: "2026-10" });
    expect(names()).toEqual(["Synthetic shop october"]);
    await act(async () => { late({ ok: true, data: pageOf([entry("late")]) }); await Promise.resolve(); });
    await drain();
    expect(names()).toEqual(["Synthetic shop october"]);
    expect(host.textContent).not.toContain("late");
  });

  it("stops the background walk when access is lost", async () => {
    const calls: Array<string | undefined> = [];
    const api = fakeApi();
    api.analyticsTransactions.mockImplementation((_period, _filter, cursor) => {
      calls.push(cursor);
      if (cursor === undefined) return ok(pageOf([entry("kept")], "c1"));
      return fail(401, "budget_http_401");
    });
    const { onAccessLoss } = await show(api, { lost: true });
    await drain();
    expect(calls).toEqual([undefined, "c1"]);
    expect(onAccessLoss).toHaveBeenCalledWith({ ok: false, status: 401, error: "budget_http_401" });
    expect(names()).toEqual(["Synthetic shop kept"]);
    expect(host.textContent).not.toContain("could not be loaded");
    expect(host.textContent).not.toContain("Loading…");
  });

  it("stops background loading at the row cap", async () => {
    expect(TRANSACTION_ROW_CAP).toBe(5000);
    const calls: Array<string | undefined> = [];
    const api = fakeApi();
    api.analyticsTransactions.mockImplementation((_period, _filter, cursor) => {
      calls.push(cursor);
      const page = calls.length;
      return ok(pageOf([entry(`p${String(page)}a`), entry(`p${String(page)}b`)], `c${String(page)}`));
    });
    await show(api, { rowCap: 3 });
    await drain();
    expect(calls).toEqual([undefined, "c1"]);
    expect(names()).toEqual(["Synthetic shop p1a", "Synthetic shop p1b", "Synthetic shop p2a"]);
    expect(host.textContent).not.toContain("p2b");
    expect(host.textContent).not.toContain("Loading…");
  });
});

describe("manual own-transfer pairing", () => {
  it("localizes same-day, singular and plural day distances", () => {
    expect([0, 1, 2].map(transactionLabels.en.pairDays)).toEqual(["same day", "1 day apart", "2 days apart"]);
    expect([0, 1, 2].map(transactionLabels.pl.pairDays)).toEqual(["ten sam dzień", "1 dzień różnicy", "2 dni różnicy"]);
  });

  it("shows the action only on eligible debit rows", async () => {
    const rows = [
      pairDebit("eligible"),
      pairDebit("credit", { credit_debit_indicator: "CRDT" }),
      pairDebit("zero", { amount: "0.00" }),
      pairDebit("dateless", { booking_date: null, value_date: null }),
      pairDebit("kind", { effective: { kind: "transfer", category_id: null, source: "default", rule_id: null } }),
      pairDebit("accepted", { transfer: { status: "accepted", decision_id: "dec-1", candidate_count: 0, ambiguous: false } }),
    ];
    const api = fakeApi(rows);
    await show(api);
    const rendered = [...host.querySelectorAll<HTMLElement>(".budget-entry")];
    expect(rendered).toHaveLength(rows.length);
    for (const [index, row] of rendered.entries()) {
      expect([...row.querySelectorAll("button")].some((item) => item.textContent === transactionLabels.en.pair)).toBe(index === 0);
    }
  });

  it("searches both adjacent months with currency and limit, following every cursor", async () => {
    const debit = pairDebit("out", { booking_date: "2026-11-02" });
    const credit = pairCredit("in", { booking_date: "2026-10-31" });
    const api = fakeApi([debit]);
    await show(api);
    api.analyticsTransactions.mockImplementation((period, ...args) => {
      const [, cursor, limit] = args;
      if (limit !== 200) return ok(pageOf([debit]));
      if (period === "2026-10" && cursor === undefined) return ok(entriesOf(period, [], "october-next"));
      return ok(entriesOf(period, period === "2026-10" ? [credit] : []));
    });
    await click(button(transactionLabels.en.pair));
    const filter = { currency: "PLN", category: "", kind: "", query: "" };
    expect(api.analyticsTransactions.mock.calls.slice(1)).toEqual([
      ["2026-10", filter, undefined, 200], ["2026-10", filter, "october-next", 200], ["2026-11", filter, undefined, 200],
    ]);
    expect(host.textContent).toContain(transactionLabels.en.pairTitle);
    expect(host.textContent).toContain("Synthetic Card Repayment");
    expect(host.textContent).toContain("Synthetic Savings ••0002");
    expect(host.textContent).toContain("100.00 PLN");
    expect(host.textContent).toContain("2 days apart");
    expect(host.querySelectorAll(".budget-pair-picker .budget-entry")).toHaveLength(1);
  });

  it("stops each month at ten pages and warns that the search was partial", async () => {
    const debit = pairDebit();
    const api = fakeApi([debit]);
    await show(api);
    let searchPage = 0;
    api.analyticsTransactions.mockImplementation((period, ...args) => {
      if (args[2] !== 200) return ok(pageOf([debit]));
      searchPage += 1;
      return ok(entriesOf(period, [], `search-${String(searchPage)}`));
    });
    await click(button(transactionLabels.en.pair));
    expect(api.analyticsTransactions.mock.calls.filter((args) => args[3] === 200)).toHaveLength(10);
    expect(host.textContent).toContain(transactionLabels.en.pairLimited);
    expect(host.textContent).toContain(transactionLabels.en.pairEmpty);
  });

  it("restarts a stale search once, then reports a second stale cursor as a failure", async () => {
    const debit = pairDebit();
    const api = fakeApi([debit]);
    await show(api);
    let searchPage = 0;
    api.analyticsTransactions.mockImplementation((period, ...args) => {
      if (args[2] !== 200) return ok(pageOf([debit]));
      searchPage += 1;
      return searchPage % 2 === 1 ? ok(entriesOf(period, [], "stale")) : fail(409, "budget_analytics_stale_cursor");
    });
    await click(button(transactionLabels.en.pair));
    expect(api.analyticsTransactions.mock.calls.filter((args) => args[3] === 200)).toHaveLength(4);
    expect(host.textContent).toContain(transactionLabels.en.pairError);
    expect(host.textContent).not.toContain(transactionLabels.en.pairLoading);
  });

  it("renders an explicit empty state when the search has no acceptable credit", async () => {
    const debit = pairDebit();
    const api = fakeApi([debit]);
    await show(api);
    mockPairSearch(api, debit, [pairCredit("wrong", { amount: "100.01" })]);
    await click(button(transactionLabels.en.pair));
    expect(host.textContent).toContain(transactionLabels.en.pairEmpty);
    expect(host.querySelectorAll(".budget-pair-picker .budget-entry")).toHaveLength(0);
  });

  it("sends the exact manual decision and reloads the view after confirmation", async () => {
    const debit = pairDebit();
    const credit = pairCredit();
    const api = fakeApi([debit]);
    const { onChanged } = await show(api);
    mockPairSearch(api, debit, [credit]);
    await click(button(transactionLabels.en.pair));
    await click(button(transactionLabels.en.pairConfirm));
    expect(api.decideTransfer).toHaveBeenCalledWith({
      action: "accept", source: "manual", decision_id: null, expected_version: 0,
      debit: { account_id: "acc-1", entry_reference: "out" },
      credit: { account_id: "acc-2", entry_reference: "in" },
      expected_debit_fingerprint: "tfp-out", expected_credit_fingerprint: "tfp-in",
    });
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(host.querySelector(".budget-pair-picker")).toBeNull();
  });

  it.each([
    { code: "budget_analytics_source_changed", notice: transactionLabels.en.changed, reload: true },
    { code: "budget_analytics_leg_reserved", notice: transactionLabels.en.pairReserved, reload: true },
    { code: "budget_analytics_conflict", notice: transactionLabels.en.pairExisting, reload: true },
    { code: "budget_analytics_invalid_request", notice: transactionLabels.en.pairInvalid, reload: false },
    { code: "budget_analytics_stale_cursor", notice: transactionLabels.en.conflict, reload: true },
    { code: "budget_internal_error", notice: transactionLabels.en.saveError, reload: false },
  ])("maps $code to its pairing notice and reload behavior", async ({ code, notice, reload }) => {
    const debit = pairDebit();
    const credit = pairCredit();
    const api = fakeApi([debit]);
    const { onChanged } = await show(api);
    mockPairSearch(api, debit, [credit]);
    api.decideTransfer.mockImplementationOnce(() => fail(409, code));
    await click(button(transactionLabels.en.pair));
    await click(button(transactionLabels.en.pairConfirm));
    expect(host.textContent).toContain(notice);
    expect(api.analyticsTransactions.mock.calls.filter((args) => args[3] !== 200)).toHaveLength(reload ? 2 : 1);
    expect(host.querySelector(".budget-pair-picker") === null).toBe(reload);
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("hands a failed search to the access barrier before doing anything else", async () => {
    const debit = pairDebit();
    const api = fakeApi([debit]);
    const { onAccessLoss } = await show(api, { lost: true });
    api.analyticsTransactions.mockImplementation((...args) =>
      args[3] === 200 ? fail(401, "budget_http_401") : ok(pageOf([debit])));
    await click(button(transactionLabels.en.pair));
    expect(onAccessLoss).toHaveBeenCalledWith({ ok: false, status: 401, error: "budget_http_401" });
    expect(api.analyticsTransactions.mock.calls.filter((args) => args[3] === 200)).toHaveLength(1);
    expect(host.textContent).not.toContain(transactionLabels.en.pairError);
  });

  it("hands confirmation to the access barrier first and releases its busy state", async () => {
    const debit = pairDebit();
    const api = fakeApi([debit]);
    const { onAccessLoss, onChanged } = await show(api, { lost: true });
    mockPairSearch(api, debit, [pairCredit()]);
    api.decideTransfer.mockImplementationOnce(() => fail(401, "budget_http_401"));
    await click(button(transactionLabels.en.pair));
    await click(button(transactionLabels.en.pairConfirm));
    expect(onAccessLoss).toHaveBeenCalledWith({ ok: false, status: 401, error: "budget_http_401" });
    expect(onChanged).not.toHaveBeenCalled();
    expect(host.textContent).not.toContain(transactionLabels.en.saveError);
    expect(button(transactionLabels.en.pairConfirm).disabled).toBe(false);
  });

  it("ignores a search answer that arrives after cancellation", async () => {
    const debit = pairDebit();
    const api = fakeApi([debit]);
    await show(api);
    let loaded: (value: BudgetResult<AnalyticsTransactions>) => void = () => undefined;
    api.analyticsTransactions.mockImplementation((...args) => {
      if (args[3] !== 200) return ok(pageOf([debit]));
      return new Promise((resolve) => { loaded = resolve; });
    });
    await click(button(transactionLabels.en.pair));
    expect(host.textContent).toContain(transactionLabels.en.pairLoading);
    await click(button(transactionLabels.en.cancel));
    await act(async () => { loaded({ ok: true, data: entriesOf("2026-11", [pairCredit()]) }); await Promise.resolve(); });
    await settle();
    expect(host.querySelector(".budget-pair-picker")).toBeNull();
    expect(host.textContent).not.toContain("Synthetic Card Repayment");
  });

  it("closes the picker and drops its pending answer when the editor opens", async () => {
    const debit = pairDebit();
    const api = fakeApi([debit]);
    await show(api);
    let loaded: (value: BudgetResult<AnalyticsTransactions>) => void = () => undefined;
    api.analyticsTransactions.mockImplementation((...args) => {
      if (args[3] !== 200) return ok(pageOf([debit]));
      return new Promise((resolve) => { loaded = resolve; });
    });
    await click(button(transactionLabels.en.pair));
    expect(host.textContent).toContain(transactionLabels.en.pairLoading);
    await click(button(transactionLabels.en.edit));
    expect(host.querySelector(".budget-pair-picker")).toBeNull();
    expect(host.querySelector(".budget-entry-editor")).not.toBeNull();
    await act(async () => { loaded({ ok: true, data: entriesOf("2026-11", [pairCredit()]) }); await Promise.resolve(); });
    await settle();
    expect(host.querySelector(".budget-pair-picker")).toBeNull();
    expect(host.querySelector(".budget-entry-editor")).not.toBeNull();
    expect(host.textContent).not.toContain("Synthetic Card Repayment");
  });

  it("drops a late search answer after the panel reloads", async () => {
    const debit = pairDebit();
    const api = fakeApi([debit]);
    await show(api);
    let loaded: (value: BudgetResult<AnalyticsTransactions>) => void = () => undefined;
    api.analyticsTransactions.mockImplementationOnce(() => new Promise((resolve) => { loaded = resolve; }));
    await click(button(transactionLabels.en.pair));
    api.analyticsTransactions.mockImplementation(() => ok(pageOf([debit])));
    await show(api, { revision: 1 });
    await act(async () => { loaded({ ok: true, data: entriesOf("2026-11", [pairCredit()]) }); await Promise.resolve(); });
    await settle();
    expect(host.querySelector(".budget-pair-picker")).toBeNull();
    expect(host.textContent).not.toContain("Synthetic Card Repayment");
  });

  it("reports a late confirmation without restoring a picker after the panel reloads", async () => {
    const debit = pairDebit();
    const api = fakeApi([debit]);
    const { onChanged } = await show(api);
    mockPairSearch(api, debit, [pairCredit()]);
    let decided: (value: BudgetResult<{ decision: TransferDecisionResult }>) => void = () => undefined;
    api.decideTransfer.mockImplementationOnce(() => new Promise((resolve) => { decided = resolve; }));
    await click(button(transactionLabels.en.pair));
    await click(button(transactionLabels.en.pairConfirm));
    await show(api, { revision: 1 });
    await act(async () => { decided({ ok: true, data: { decision: decisionOf() } }); await Promise.resolve(); });
    await settle();
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(host.querySelector(".budget-pair-picker")).toBeNull();
  });

  it("reports a successful confirmation after a filter change without restoring stale UI", async () => {
    const debit = pairDebit();
    const api = fakeApi([debit]);
    const { onChanged } = await show(api);
    mockPairSearch(api, debit, [pairCredit()]);
    let decided: (value: BudgetResult<{ decision: TransferDecisionResult }>) => void = () => undefined;
    api.decideTransfer.mockImplementationOnce(() => new Promise((resolve) => { decided = resolve; }));
    await click(button(transactionLabels.en.pair));
    await click(button(transactionLabels.en.pairConfirm));
    await applyCurrencyFilter();
    await act(async () => { decided({ ok: true, data: { decision: decisionOf() } }); await Promise.resolve(); });
    await settle();
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(host.querySelector(".budget-pair-picker")).toBeNull();
    expect(host.querySelector(".budget-notice")).toBeNull();
  });

  it("drops a failed confirmation after a filter change without notifying or showing a notice", async () => {
    const debit = pairDebit();
    const api = fakeApi([debit]);
    const { onChanged } = await show(api);
    mockPairSearch(api, debit, [pairCredit()]);
    let decided: (value: BudgetResult<{ decision: TransferDecisionResult }>) => void = () => undefined;
    api.decideTransfer.mockImplementationOnce(() => new Promise((resolve) => { decided = resolve; }));
    await click(button(transactionLabels.en.pair));
    await click(button(transactionLabels.en.pairConfirm));
    await applyCurrencyFilter();
    await act(async () => { decided({ ok: false, status: 500, error: "budget_internal_error" }); await Promise.resolve(); });
    await settle();
    expect(onChanged).not.toHaveBeenCalled();
    expect(host.querySelector(".budget-notice")).toBeNull();
  });

  it("disables row and picker actions while a pair is being confirmed", async () => {
    const debit = pairDebit();
    const api = fakeApi([debit]);
    const { onChanged } = await show(api);
    mockPairSearch(api, debit, [pairCredit()]);
    let decided: (value: BudgetResult<{ decision: TransferDecisionResult }>) => void = () => undefined;
    api.decideTransfer.mockImplementationOnce(() => new Promise((resolve) => { decided = resolve; }));
    await click(button(transactionLabels.en.pair));
    await click(button(transactionLabels.en.pairConfirm));
    expect(button(transactionLabels.en.pairConfirm).disabled).toBe(true);
    expect(button(transactionLabels.en.cancel).disabled).toBe(true);
    expect([...host.querySelectorAll("button")].some((item) => item.textContent === transactionLabels.en.pair)).toBe(false);
    expect(button(transactionLabels.en.edit).disabled).toBe(true);
    await act(async () => { decided({ ok: true, data: { decision: decisionOf() } }); await Promise.resolve(); });
    await settle();
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(button(transactionLabels.en.pair).disabled).toBe(false);
  });

  it("cancels an open picker without changing anything", async () => {
    const debit = pairDebit();
    const api = fakeApi([debit]);
    const { onChanged } = await show(api);
    mockPairSearch(api, debit, [pairCredit()]);
    await click(button(transactionLabels.en.pair));
    expect([...host.querySelectorAll("button")].some((item) => item.textContent === transactionLabels.en.pair)).toBe(false);
    await click(button(transactionLabels.en.cancel));
    expect(host.querySelector(".budget-pair-picker")).toBeNull();
    expect(button(transactionLabels.en.pair)).toBeDefined();
    expect(api.decideTransfer).not.toHaveBeenCalled();
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("closes the category editor when the picker opens", async () => {
    const debit = pairDebit();
    const api = fakeApi([debit]);
    await show(api);
    mockPairSearch(api, debit, [pairCredit()]);
    await click(button(transactionLabels.en.edit));
    expect(host.querySelector(".budget-entry-editor")).not.toBeNull();
    await click(button(transactionLabels.en.pair));
    expect(host.querySelector(".budget-entry-editor")).toBeNull();
    expect(host.querySelector(".budget-pair-picker")).not.toBeNull();
  });

  it("keeps one picker open and drops a late answer from the previous picker", async () => {
    const rows = [pairDebit("first"), pairDebit("second")];
    const api = fakeApi(rows);
    await show(api);
    let loaded: (value: BudgetResult<AnalyticsTransactions>) => void = () => undefined;
    api.analyticsTransactions.mockImplementationOnce(() => new Promise((resolve) => { loaded = resolve; }));
    const rendered = [...host.querySelectorAll<HTMLElement>(".budget-entry")];
    await click(button(transactionLabels.en.pair, rendered[0] ?? host));
    api.analyticsTransactions.mockImplementation(() => ok(entriesOf("2026-11", [pairCredit()])));
    await click(button(transactionLabels.en.pair, rendered[1] ?? host));
    await act(async () => { loaded({ ok: true, data: entriesOf("2026-11", [pairCredit("late")]) }); await Promise.resolve(); });
    await settle();
    expect(host.querySelectorAll(".budget-pair-picker")).toHaveLength(1);
    expect(rendered[0]?.querySelector(".budget-pair-picker")).toBeNull();
    expect(rendered[1]?.textContent).toContain("Synthetic Card Repayment");
    expect(rendered[1]?.textContent).not.toContain("Synthetic shop late");
  });
});
