// Synthetic data only: no real account, transaction or bank values.
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BudgetApi, BudgetResult } from "../api";
import { categoryList } from "./fixtures";
import type { AnalyticsRule, RulePreview } from "./model";
import { CategoriesView, rulesLabels } from "./rules";

function ok<T>(data: T): Promise<BudgetResult<T>> { return Promise.resolve({ ok: true, data }); }
function fail<T>(status: number, error: string): Promise<BudgetResult<T>> { return Promise.resolve({ ok: false, status, error }); }

const rule: AnalyticsRule = { id: "rule-1", version: 2, field: "counterparty_name", operator: "contains", value: "Synthetic Grocer",
  category_id: "cat-food", kind: "expense", priority: 50, enabled: false };
const preview: RulePreview = { preview_id: "preview-1", affected_count: 23, expires_at: 2_000_000_000, examples: [
  { account_id: "acc-1", entry_reference: "e1", date: "2026-11-03", amount: "12.5", currency: "PLN", credit_debit_indicator: "DBIT",
    description: "Synthetic Grocer 1" }] };

function fakeApi() {
  return {
    categories: vi.fn<BudgetApi["categories"]>(() => ok({ categories: categoryList })),
    createCategory: vi.fn<BudgetApi["createCategory"]>(() => ok({ category: { id: "cat-new", name: "Synthetic New", archived: false, version: 1 } })),
    updateCategory: vi.fn<BudgetApi["updateCategory"]>(() => ok({ category: { id: "cat-food", name: "Synthetic Food", archived: false, version: 2 } })),
    rules: vi.fn<BudgetApi["rules"]>(() => ok({ rules: [rule] })),
    previewRule: vi.fn<BudgetApi["previewRule"]>(() => ok({ preview })),
    saveRule: vi.fn<BudgetApi["saveRule"]>(() => ok({ rule })),
  } satisfies Pick<BudgetApi, "categories" | "createCategory" | "updateCategory" | "rules" | "previewRule" | "saveRule">;
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
async function show(api: ReturnType<typeof fakeApi>, options: { revision?: number; lost?: boolean } = {}) {
  const onAccessLoss = vi.fn((result: { ok: boolean }) => options.lost === true && !result.ok);
  await act(async () => {
    root.render(<CategoriesView api={api} text={rulesLabels.en} locale="en" revision={options.revision ?? 0} onAccessLoss={onAccessLoss} />);
    await Promise.resolve();
  });
  await settle();
  return onAccessLoss;
}
function section(label: string): HTMLElement {
  const found = host.querySelector<HTMLElement>(`section[aria-label="${label}"]`);
  if (found === null) throw new Error(`section ${label} missing`);
  return found;
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
function field(scope: ParentNode, label: string): HTMLInputElement | HTMLSelectElement | null {
  return [...scope.querySelectorAll("label")].find((item) => item.textContent.startsWith(label))?.querySelector("input, select") ?? null;
}

describe("categories", () => {
  it("lists categories with archived ones marked and adds a trimmed new name", async () => {
    const api = fakeApi();
    await show(api);
    const panel = section("Categories");
    expect(panel.textContent).toContain("Synthetic Food");
    expect(panel.textContent).toContain("Synthetic Old(archived)");
    const add = [...panel.querySelectorAll("form")].at(-1);
    const name = add?.querySelector("input");
    expect(button("Add category", panel).disabled).toBe(true);
    await type(name, "  Synthetic New  ");
    await act(async () => { add?.requestSubmit(); await Promise.resolve(); });
    await settle();
    expect(api.createCategory).toHaveBeenCalledWith({ name: "Synthetic New" });
    expect(api.categories).toHaveBeenCalledTimes(2);
    expect(name?.value).toBe("");
  });

  it("renames and archives with the category id and the version the owner saw", async () => {
    const api = fakeApi();
    await show(api);
    const panel = section("Categories");
    await click(button("Rename", panel));
    const form = panel.querySelector("li form");
    await type(form?.querySelector("input"), "Synthetic Groceries");
    await act(async () => { (form as HTMLFormElement | null)?.requestSubmit(); await Promise.resolve(); });
    await settle();
    expect(api.updateCategory).toHaveBeenCalledWith({ category_id: "cat-food", name: "Synthetic Groceries", archived: false, expected_version: 1 });
    await click(button("Restore", section("Categories")));
    expect(api.updateCategory).toHaveBeenLastCalledWith({ category_id: "cat-old", name: "Synthetic Old", archived: false, expected_version: 3 });
  });

  it("reloads after a conflict and refuses an empty rename", async () => {
    const api = fakeApi();
    await show(api);
    api.updateCategory.mockImplementationOnce(() => fail(409, "budget_analytics_conflict"));
    await click(button("Archive", section("Categories")));
    expect(host.textContent).toContain("changed elsewhere");
    expect(api.categories).toHaveBeenCalledTimes(2);
    await click(button("Rename", section("Categories")));
    await type(section("Categories").querySelector("li form input"), "   ");
    expect(button("Save", section("Categories")).disabled).toBe(true);
    await click(button("Cancel", section("Categories")));
    expect(section("Categories").querySelector("li form")).toBeNull();
  });
});

describe("rules", () => {
  it("describes each rule and says manual choices win", async () => {
    await show(fakeApi());
    const panel = section("Rules");
    expect(panel.textContent).toContain("Your own choice on an entry always wins");
    expect(panel.querySelector("li")?.textContent).toContain("counterparty contains „Synthetic Grocer”→Synthetic FoodExpensePriority 50disabled");
  });

  it("saves only a fresh preview and drops it when the rule changes", async () => {
    const api = fakeApi();
    await show(api);
    await click(button("New rule"));
    const form = section("Rules").querySelector("form");
    if (form === null) throw new Error("editor missing");
    expect(button("Preview", form).disabled).toBe(true);
    await type(field(form, "text") as HTMLInputElement, " Synthetic Shop ");
    expect(host.textContent).toContain("Choose a category or a type");
    await choose(field(form, "Category") as HTMLSelectElement, "cat-food");
    await act(async () => { form.requestSubmit(); await Promise.resolve(); });
    await settle();
    expect(api.previewRule).toHaveBeenCalledWith({ rule_id: null, expected_version: 0, field: "description", operator: "contains",
      value: "Synthetic Shop", category_id: "cat-food", kind: "auto", priority: 100, enabled: true });
    const region = host.querySelector('[role="region"]');
    expect(region?.textContent).toContain("apply to 23 entries");
    expect(region?.textContent).toContain("Synthetic Grocer 1");
    expect(region?.textContent).toContain("−12.50 PLN");
    await choose(field(form, "Type") as HTMLSelectElement, "expense");
    expect(host.querySelector('[role="region"]')).toBeNull();
    await act(async () => { form.requestSubmit(); await Promise.resolve(); });
    await settle();
    await click(button("Save rule"));
    expect(api.saveRule).toHaveBeenCalledWith("preview-1");
    expect(api.rules).toHaveBeenCalledTimes(2);
    expect(section("Rules").querySelector("form")).toBeNull();
  });

  it("edits an existing rule against its version and redoes an expired preview", async () => {
    const api = fakeApi();
    await show(api);
    await click(button("Edit"));
    const form = section("Rules").querySelector("form");
    if (form === null) throw new Error("editor missing");
    expect((field(form, "text") as HTMLInputElement).value).toBe("Synthetic Grocer");
    await act(async () => { form.requestSubmit(); await Promise.resolve(); });
    await settle();
    expect(api.previewRule).toHaveBeenCalledWith(expect.objectContaining({ rule_id: "rule-1", expected_version: 2, enabled: false }));
    api.saveRule.mockImplementationOnce(() => fail(409, "budget_analytics_preview_expired"));
    await click(button("Save rule"));
    expect(host.textContent).toContain("preview the rule again");
    expect(host.querySelector('[role="region"]')).toBeNull();
    api.previewRule.mockImplementationOnce(() => fail(409, "budget_analytics_conflict"));
    await act(async () => { form.requestSubmit(); await Promise.resolve(); });
    await settle();
    expect(host.textContent).toContain("changed elsewhere");
    expect(api.rules).toHaveBeenCalledTimes(2);
  });

  it("shows empty and failed lists plainly and passes answers to the access barrier", async () => {
    const api = fakeApi();
    api.rules.mockImplementation(() => ok({ rules: [] }));
    api.categories.mockImplementation(() => fail(500, "budget_internal_error"));
    await show(api);
    expect(section("Rules").textContent).toContain("No rules yet.");
    expect(section("Categories").textContent).toContain("Could not be loaded.");
    api.rules.mockImplementation(() => fail(401, "budget_http_401"));
    const onAccessLoss = await show(api, { revision: 1, lost: true });
    expect(onAccessLoss).toHaveBeenCalledWith({ ok: false, status: 401, error: "budget_http_401" });
  });

  it("drops a save that finishes after the view reloaded", async () => {
    let late: (value: BudgetResult<{ rule: AnalyticsRule }>) => void = () => undefined;
    const api = fakeApi();
    await show(api);
    await click(button("Edit"));
    const form = section("Rules").querySelector("form");
    await act(async () => { form?.requestSubmit(); await Promise.resolve(); });
    await settle();
    api.saveRule.mockImplementationOnce(() => new Promise((resolve) => { late = resolve; }));
    await click(button("Save rule"));
    await show(api, { revision: 1 });
    await act(async () => { late({ ok: true, data: { rule } }); await Promise.resolve(); });
    await settle();
    expect(api.rules).toHaveBeenCalledTimes(2);
  });
});
