// Synthetic data only: no real account, transaction or bank values.
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createBudgetApi } from "../api";
import type { BudgetApi, BudgetResult } from "../api";
import { buildCategoryColors, contrastRatio, parseHexColor } from "./category_colors";
import { categoryList } from "./fixtures";
import type { AnalyticsRule, RuleCandidate, RulePreview, RuleSaved } from "./model";
import { loadLocale, t } from "../../i18n";
import { account } from "../fixtures";
import { CategoriesView } from "./rules";

function ok<T>(data: T): Promise<BudgetResult<T>> { return Promise.resolve({ ok: true, data }); }
function fail<T>(status: number, error: string): Promise<BudgetResult<T>> { return Promise.resolve({ ok: false, status, error }); }

const rule: AnalyticsRule = { id: "rule-1", version: 2, field: "counterparty_name", operator: "contains", value: "Synthetic Grocer",
  category_id: "cat-food", kind: "expense", priority: 50, enabled: false, created_at: 1, updated_at: 2 };
const saved: RuleSaved = { ...rule, changed: true };
const preview: RulePreview = { preview_id: "preview-1", affected_count: 23, expires_at: 2_000_000_000, examples: [
  { account_id: "acc-1", entry_reference: "synthetic-ref-1" }, { account_id: "gone", entry_reference: "synthetic-ref-2" }] };
const accounts = [account({ id: "acc-1", display: { product: "Synthetic Current", currency: "PLN", masked_suffix: "0001",
  source: "offline_import", observed_at: 1, imported_at: 2 } })];
const category = { id: "cat-new", name: "Synthetic New", archived: false, version: 1, created_at: 1, updated_at: 1, parent_id: null, changed: true };

function fakeApi() {
  return {
    categories: vi.fn<BudgetApi["categories"]>(() => ok({ categories: categoryList })),
    createCategory: vi.fn<BudgetApi["createCategory"]>(() => ok({ category })),
    updateCategory: vi.fn<BudgetApi["updateCategory"]>(() => ok({ category: { ...category, id: "cat-food", version: 2 } })),
    spreadCategoryColors: vi.fn<BudgetApi["spreadCategoryColors"]>(() => ok({ categories: categoryList })),
    rules: vi.fn<BudgetApi["rules"]>(() => ok({ rules: [rule] })),
    previewRule: vi.fn<BudgetApi["previewRule"]>(() => ok({ preview })),
    saveRule: vi.fn<BudgetApi["saveRule"]>(() => ok({ rule: saved })),
  } satisfies Pick<BudgetApi, "categories" | "createCategory" | "updateCategory" | "spreadCategoryColors" | "rules" | "previewRule" | "saveRule">;
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
  vi.unstubAllGlobals();
  await loadLocale("en");
});

async function settle(): Promise<void> {
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
}
async function show(api: ReturnType<typeof fakeApi>, options: { revision?: number; lost?: boolean;
  locale?: string; initialRule?: RuleCandidate | null } = {}) {
  const onAccessLoss = vi.fn((result: { ok: boolean }) => options.lost === true && !result.ok);
  const locale = options.locale ?? "en";
  await loadLocale(locale.startsWith("pl") ? "pl" : "en");
  await act(async () => {
    root.render(<CategoriesView api={api} accounts={accounts} locale={locale}
      revision={options.revision ?? 0} onAccessLoss={onAccessLoss} initialRule={options.initialRule} />);
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
function categoryRow(name: string, scope: ParentNode = section("Categories")): HTMLButtonElement {
  const found = [...scope.querySelectorAll<HTMLButtonElement>("button.budget-category-row")]
    .find((item) => item.textContent.includes(name));
  if (found === undefined) throw new Error(`category row ${name} missing`);
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
  it("lists categories with swatches without repeated row actions and adds a trimmed new name", async () => {
    const api = fakeApi();
    await show(api);
    const panel = section("Categories");
    expect(panel.textContent).toContain("Synthetic Food");
    expect(panel.textContent).toContain("Synthetic Old (archived)");
    const rows = [...panel.querySelectorAll<HTMLButtonElement>("li button.budget-category-row")];
    expect(rows).toHaveLength(2);
    expect(rows.every((row) => row.type === "button")).toBe(true);
    expect([...panel.querySelectorAll("li button")].map((item) => item.textContent)).not.toContain("Rename");
    expect([...panel.querySelectorAll("li button")].map((item) => item.textContent)).not.toContain("Archive");
    expect([...panel.querySelectorAll("li button")].map((item) => item.textContent)).not.toContain("Restore");
    const add = [...panel.querySelectorAll("form")].at(-1);
    const name = add?.querySelector("input");
    expect(button("Add category", panel).disabled).toBe(true);
    await type(name, "  Synthetic New  ");
    await act(async () => { add?.requestSubmit(); await Promise.resolve(); });
    await settle();
    expect(api.createCategory).toHaveBeenCalledWith({ name: "Synthetic New", parent_id: null });
    expect(api.categories).toHaveBeenCalledTimes(2);
    expect(name?.value).toBe("");
  });

  it("opens the editor from a row with keyboard support, renames, archives, and restores inside it", async () => {
    const api = fakeApi();
    await show(api);
    const panel = section("Categories");
    await click(categoryRow("Synthetic Food", panel));
    const form = panel.querySelector("li form");
    const nameInput = form?.querySelector<HTMLInputElement>('input[name="category_name"]');
    expect(document.activeElement).toBe(nameInput);
    await act(async () => {
      form?.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      await Promise.resolve();
    });
    expect(panel.querySelector("li form")).toBeNull();
    expect(document.activeElement).toBe(categoryRow("Synthetic Food", panel));

    await click(categoryRow("Synthetic Food", panel));
    const renameForm = panel.querySelector("li form");
    await type(renameForm?.querySelector('input[name="category_name"]'), "Synthetic Groceries");
    await act(async () => { (renameForm as HTMLFormElement | null)?.requestSubmit(); await Promise.resolve(); });
    await settle();
    expect(api.updateCategory).toHaveBeenCalledWith({ category_id: "cat-food", name: "Synthetic Groceries", archived: false, expected_version: 1, parent_id: null });
    expect(panel.querySelector("li form")).toBeNull();

    await click(categoryRow("Synthetic Food", panel));
    await click(button("Archive", panel.querySelector("li form") as HTMLElement));
    expect(api.updateCategory).toHaveBeenCalledWith({ category_id: "cat-food", name: "Synthetic Food", archived: true, expected_version: 1, parent_id: null });
    expect(panel.querySelector("li form")).toBeNull();

    await click(categoryRow("Synthetic Old", panel));
    await click(button("Restore", panel.querySelector("li form") as HTMLElement));
    expect(api.updateCategory).toHaveBeenLastCalledWith({ category_id: "cat-old", name: "Synthetic Old", archived: false, expected_version: 3, parent_id: null });
    expect(panel.querySelector("li form")).toBeNull();

    // Switching directly from one open row to another moves focus to the newly opened editor.
    await click(categoryRow("Synthetic Food", panel));
    await click(categoryRow("Synthetic Old", panel));
    const switchedForm = panel.querySelector("li form");
    expect(document.activeElement).toBe(switchedForm?.querySelector('input[name="category_name"]'));
    expect((switchedForm?.querySelector('input[name="category_name"]') as HTMLInputElement | null)?.value).toBe("Synthetic Old");
  });

  it("sets a root hue with a live preview and clears it back to automatic", async () => {
    const api = fakeApi();
    await show(api);
    const panel = section("Categories");
    await click(categoryRow("Synthetic Food", panel));
    const editor = panel.querySelector("li form");
    const hue = field(editor as HTMLElement, "Colour hue") as HTMLInputElement;
    expect(hue.type).toBe("range");
    await type(hue, "123");
    expect(hue.value).toBe("123");
    expect(editor?.textContent).toContain("123°");
    await act(async () => { (editor as HTMLFormElement).requestSubmit(); await Promise.resolve(); });
    await settle();
    expect(api.updateCategory).toHaveBeenCalledWith({
      category_id: "cat-food", name: "Synthetic Food", archived: false, expected_version: 1, parent_id: null, color_hue: 123,
    });

    await click(categoryRow("Synthetic Food", panel));
    const clearing = panel.querySelector("li form");
    await click(button("Automatic colour", clearing as HTMLElement));
    expect(button("Automatic colour", clearing as HTMLElement).disabled).toBe(true);
    await act(async () => { (clearing as HTMLFormElement).requestSubmit(); await Promise.resolve(); });
    await settle();
    expect(api.updateCategory).toHaveBeenLastCalledWith({
      category_id: "cat-food", name: "Synthetic Food", archived: false, expected_version: 1, parent_id: null, color_hue: null,
    });
  });

  it("hides spread colours under More disclosure, explains its impact, and reloads on request", async () => {
    const api = fakeApi();
    await show(api);
    const panel = section("Categories");
    const details = panel.querySelector("details");
    expect(details).not.toBeNull();
    expect(details?.open).toBe(false);
    expect(details?.querySelector("summary")?.textContent).toBe("More");
    await click(details?.querySelector("summary") as HTMLElement);
    expect(details?.open).toBe(true);
    expect(details?.textContent).toContain(t("budget.rules.spreadColorsHint"));
    expect(details?.textContent).toContain("Synthetic Food");
    expect(details?.textContent).not.toContain("Synthetic Old");
    const spread = button("Spread colours evenly", details as ParentNode);
    expect(spread.disabled).toBe(false);
    await click(spread);
    expect(api.spreadCategoryColors).toHaveBeenCalledTimes(1);
    expect(api.categories).toHaveBeenCalledTimes(2);
  });

  it("reloads after a conflict and refuses an empty rename", async () => {
    const api = fakeApi();
    await show(api);
    const panel = section("Categories");
    await click(categoryRow("Synthetic Food", panel));
    api.updateCategory.mockImplementationOnce(() => fail(409, "budget_analytics_conflict"));
    await click(button("Archive", panel.querySelector("li form") as HTMLElement));
    expect(host.textContent).toContain("changed elsewhere");
    expect(api.categories).toHaveBeenCalledTimes(2);
    await type(panel.querySelector("li form input"), "   ");
    expect(button("Save", panel).disabled).toBe(true);
    await click(button("Cancel", panel));
    expect(panel.querySelector("li form")).toBeNull();
    expect(document.activeElement).toBe(categoryRow("Synthetic Food", panel));
  });

  it("indents a subcategory, keeps hue top-level only, and enforces archived-parent protections", async () => {
    const transport = { id: "cat-transport", name: "Synthetic Transport", archived: false, version: 1, created_at: 1, updated_at: 1, parent_id: null };
    const fuel = { id: "cat-fuel", name: "Synthetic Fuel", archived: false, version: 1, created_at: 1, updated_at: 1, parent_id: "cat-transport" };
    const archived = { id: "cat-old", name: "Synthetic Old", archived: true, version: 3, created_at: 1, updated_at: 2, parent_id: null };
    const api = fakeApi();
    api.categories.mockImplementation(() => ok({ categories: [fuel, transport, archived] }));
    await show(api);
    const panel = section("Categories");
    const items = [...panel.querySelectorAll("ul.budget-category-list > li")];
    const oldRow = items[0];
    const parent = items[1];
    const child = items[2];
    if (oldRow === undefined || parent === undefined || child === undefined) throw new Error("category rows missing");
    expect(items.map((item) => item.getAttribute("data-depth"))).toEqual(["0", "0", "1"]);
    expect(parent.textContent).toContain("Synthetic Transport");
    const expectedColors = buildCategoryColors([fuel, transport, archived]);
    const parentSwatch = parent.querySelector<HTMLElement>(".budget-cat-swatch");
    const childSwatch = child.querySelector<HTMLElement>(".budget-cat-swatch");
    expect(parentSwatch?.style.backgroundColor).toBeTruthy();
    expect(childSwatch?.style.backgroundColor).toBeTruthy();
    expect(parentSwatch?.style.backgroundColor).not.toBe(childSwatch?.style.backgroundColor);
    expect(expectedColors.get("cat-fuel").oklch.l).toBeGreaterThan(expectedColors.get("cat-transport").oklch.l);

    await click(categoryRow("Synthetic Transport", panel));
    const parentEditor = panel.querySelector("li form") as HTMLElement;
    expect(button("Archive", parentEditor).disabled).toBe(true);
    expect(button("Archive", parentEditor).title).toBe(t("budget.rules.archiveBlocked"));
    expect(parentEditor.textContent).toContain(t("budget.rules.archiveBlocked"));
    expect(field(parentEditor, "Colour hue")).not.toBeNull();
    await click(button("Cancel", parentEditor));

    const add = [...panel.querySelectorAll("form")].at(-1);
    if (add === undefined) throw new Error("form missing");
    const addParentSelect = field(add, "Parent category") as HTMLSelectElement;
    expect([...addParentSelect.options].map((option) => option.value)).toEqual(["", "cat-fuel", "cat-transport"]);
    await choose(addParentSelect, "cat-transport");
    await type(add.querySelector("input"), "Synthetic Wash");
    await act(async () => { add.requestSubmit(); await Promise.resolve(); });
    await settle();
    expect(api.createCategory).toHaveBeenCalledWith({ name: "Synthetic Wash", parent_id: "cat-transport" });

    await click(categoryRow("Synthetic Fuel", panel));
    const editor = section("Categories").querySelector("li form");
    if (!(editor instanceof HTMLFormElement)) throw new Error("editor missing");
    expect(button("Archive", editor).disabled).toBe(false);
    expect(field(editor, "Colour hue")).toBeNull();
    const editParentSelect = field(editor, "Parent category") as HTMLSelectElement;
    expect([...editParentSelect.options].map((option) => option.value)).toEqual(["", "cat-transport"]);
    await choose(editParentSelect, "");
    expect(field(editor, "Colour hue")).not.toBeNull();
    await act(async () => { editor.requestSubmit(); await Promise.resolve(); });
    await settle();
    expect(api.updateCategory).toHaveBeenCalledWith({
      category_id: "cat-fuel", name: "Synthetic Fuel", archived: false, expected_version: 1, parent_id: null,
    });
  });

  it("labels the parent field, disclosure, and spread hint in Polish", async () => {
    await show(fakeApi(), { locale: "pl" });
    const panel = section("Kategorie");
    expect(panel.textContent).toContain("Kategoria nadrzędna");
    expect(panel.textContent).toContain("Najwyższy poziom");
    expect(panel.querySelector("details summary")?.textContent).toBe("Więcej");
    expect(panel.textContent).toContain("Rozłóż kolory równomiernie");
    expect(panel.textContent).toContain(t("budget.rules.spreadColorsHint"));
    expect(t("budget.rules.archiveBlocked")).toBe("Najpierw przenieś jej podkategorie. Zarchiwizowane też trzeba przenieść.");
  });
});

describe("rules", () => {
  it("shows a Polish note when an account rule key is missing or rotated", async () => {
    const api = fakeApi();
    api.rules.mockImplementation(() => ok({ rules: [
      { ...rule, id: "plain", field: "description", operator: "contains", value: "Synthetic Shop", enabled: true },
      { ...rule, id: "missing", field: "counterparty_account", operator: "equals", value: "PL** … 1234", enabled: true,
        inactive_reason: "rule_key_unavailable" },
      { ...rule, id: "rotated", field: "counterparty_account", operator: "equals", value: "PL** … 5678", enabled: true,
        inactive_reason: "rule_key_rotated" },
    ] }));
    await show(api, { locale: "pl" });
    const panel = section("Reguły");
    expect(panel.textContent).toContain("Nieaktywna: brak klucza");
    expect(panel.textContent).toContain("Nieaktywna: zmieniony klucz – utwórz regułę ponownie");
    expect(panel.textContent.match(/Nieaktywna/g)).toHaveLength(2);
    await loadLocale("en");
    expect(t("budget.rules.inactiveKeyMissing")).toBe("Inactive: missing key");
    expect(t("budget.rules.inactiveKeyRotated")).toBe("Inactive: key changed – create the rule again");
  });

  it("describes each rule and says manual choices win", async () => {
    await show(fakeApi());
    const panel = section("Rules");
    expect(panel.textContent).toContain("Your own choice on an entry always wins");
    expect(panel.querySelector("li")?.textContent).toContain("counterparty contains “Synthetic Grocer”→Synthetic FoodExpensePriority 50disabled");
    const badge = panel.querySelector<HTMLElement>(".budget-cat-badge");
    expect(badge?.textContent).toBe("Synthetic Food");
    const rgb = (value: string) => {
      const m = /^rgb\(\s*(\d+),\s*(\d+),\s*(\d+)\s*\)$/i.exec(value.trim());
      if (m !== null) return `#${[m[1], m[2], m[3]].map((n) => Number(n).toString(16).padStart(2, "0")).join("")}`;
      return parseHexColor(value)?.hex ?? value;
    };
    expect(contrastRatio(rgb(badge?.style.backgroundColor ?? ""), rgb(badge?.style.color ?? ""))).toBeGreaterThanOrEqual(4.5);
  });

  it("offers equals only for a manually typed counterparty account", async () => {
    const api = fakeApi();
    await show(api);
    await click(button("New rule"));
    const form = section("Rules").querySelector("form");
    if (form === null) throw new Error("editor missing");
    await choose(field(form, "When") as HTMLSelectElement, "counterparty_account");
    const operator = form.querySelector<HTMLSelectElement>('select[name="operator"]');
    expect(operator?.disabled).toBe(true);
    expect([...operator?.options ?? []].map((option) => option.value)).toEqual(["equals"]);
    const syntheticIban = ` pl ${"0 ".repeat(22)}1234 `;
    await type(form.querySelector('input[name="value"]'), syntheticIban);
    await choose(field(form, "Category") as HTMLSelectElement, "cat-food");
    await act(async () => { form.requestSubmit(); await Promise.resolve(); });
    await settle();
    expect(api.previewRule).toHaveBeenCalledWith({
      rule_id: null, expected_version: 0, field: "counterparty_account", operator: "equals", value: syntheticIban.trim(),
      category_id: "cat-food", kind: "auto", priority: 100, enabled: true,
    });
  });

  it("keeps an opaque account key until the masked value is edited", async () => {
    const api = fakeApi();
    const key = "b".repeat(64);
    const initialRule: RuleCandidate = {
      rule_id: null, expected_version: 0, field: "counterparty_account", operator: "equals", value: "PL** … 1234",
      counterparty_account_key: key, category_id: "cat-food", kind: "auto", priority: 100, enabled: true,
    };
    await show(api, { initialRule });
    const form = section("Rules").querySelector("form");
    if (form === null) throw new Error("editor missing");
    await act(async () => { form.requestSubmit(); await Promise.resolve(); });
    await settle();
    expect(api.previewRule).toHaveBeenCalledWith(initialRule);
    const replacement = `PL${"0".repeat(22)}5678`;
    await type(form.querySelector('input[name="value"]'), replacement);
    await act(async () => { form.requestSubmit(); await Promise.resolve(); });
    await settle();
    const edited = api.previewRule.mock.calls[1]?.[0];
    expect(edited?.value).toBe(replacement);
    expect(edited).not.toHaveProperty("counterparty_account_key");
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
    expect([...region?.querySelectorAll("li") ?? []].map((item) => item.textContent)).toEqual([
      "Synthetic Current ••0001 · entry synthetic-ref-1", "Account · entry synthetic-ref-2"]);
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
    const okApi = fakeApi();
    okApi.updateCategory.mockImplementationOnce(() => fail(403, "budget_forbidden"));
    const onSaveAccessLoss = await show(okApi, { lost: true });
    await click(categoryRow("Synthetic Food"));
    await click(button("Archive", section("Categories").querySelector("li form") as HTMLElement));
    expect(onSaveAccessLoss).toHaveBeenCalledWith({ ok: false, status: 403, error: "budget_forbidden" });
    api.rules.mockImplementation(() => fail(401, "budget_http_401"));
    const onAccessLoss = await show(api, { revision: 1, lost: true });
    expect(onAccessLoss).toHaveBeenCalledWith({ ok: false, status: 401, error: "budget_http_401" });
  });

  it("lets a rule on an archived category only be turned off until another category is chosen", async () => {
    const api = fakeApi();
    api.rules.mockImplementation(() => ok({ rules: [{ ...rule, category_id: "cat-old", enabled: true }] }));
    await show(api);
    await click(button("Edit"));
    const form = section("Rules").querySelector("form");
    if (form === null) throw new Error("editor missing");
    expect((field(form, "Category") as HTMLSelectElement).selectedOptions[0]?.textContent).toBe("Synthetic Old (archived)");
    expect(form.querySelector('[role="alert"]')?.textContent).toContain("can only be turned off");
    expect(button("Preview", form).disabled).toBe(true);
    // Turning the rule off keeps its archived category and may be previewed and saved.
    const enabled = [...form.querySelectorAll("label")].find((item) => item.textContent.includes("Enabled"))?.querySelector("input");
    await act(async () => { enabled?.click(); await Promise.resolve(); });
    expect(button("Preview", form).disabled).toBe(false);
    expect(form.querySelector('[role="alert"]')).toBeNull();
    await act(async () => { form.requestSubmit(); await Promise.resolve(); });
    await settle();
    expect(api.previewRule).toHaveBeenLastCalledWith(expect.objectContaining({ category_id: "cat-old", enabled: false }));
    // Turning it back on needs an active category.
    await act(async () => { enabled?.click(); await Promise.resolve(); });
    expect(button("Preview", form).disabled).toBe(true);
    await choose(field(form, "Category") as HTMLSelectElement, "cat-food");
    expect(form.querySelector('[role="alert"]')).toBeNull();
    expect(button("Preview", form).disabled).toBe(false);
    expect([...(field(form, "Category") as HTMLSelectElement).options].map((option) => option.value)).toEqual(["", "cat-food"]);
  });

  it("locks the rule form while a preview is asked for and drops an answer that arrives after a reload", async () => {
    let late: (value: BudgetResult<{ preview: RulePreview }>) => void = () => undefined;
    const api = fakeApi();
    await show(api);
    await click(button("Edit"));
    const form = section("Rules").querySelector("form");
    if (form === null) throw new Error("editor missing");
    api.previewRule.mockImplementationOnce(() => new Promise((resolve) => { late = resolve; }));
    await act(async () => { form.requestSubmit(); await Promise.resolve(); });
    await settle();
    // While the preview is pending the rule cannot be edited, so the answer always matches the form.
    expect((field(form, "text") as HTMLInputElement).disabled).toBe(true);
    expect((field(form, "Category") as HTMLSelectElement).disabled).toBe(true);
    expect(button("Cancel", form).disabled).toBe(true);
    await show(api, { revision: 1 });
    await act(async () => { late({ ok: true, data: { preview } }); await Promise.resolve(); });
    await settle();
    expect(host.querySelector('[role="region"]')).toBeNull();
    expect(button("Preview").disabled).toBe(false);
    expect((field(section("Rules"), "text") as HTMLInputElement).disabled).toBe(false);
  });

  it("releases a category save that was pending when the view reloaded", async () => {
    let late: (value: BudgetResult<{ category: typeof category }>) => void = () => undefined;
    const api = fakeApi();
    await show(api);
    api.createCategory.mockImplementationOnce(() => new Promise((resolve) => { late = resolve; }));
    const add = [...section("Categories").querySelectorAll("form")].at(-1);
    await type(add?.querySelector("input"), "Synthetic Pending");
    await act(async () => { add?.requestSubmit(); await Promise.resolve(); });
    await settle();
    expect(categoryRow("Synthetic Food").disabled).toBe(true);
    await show(api, { revision: 1 });
    await act(async () => { late({ ok: true, data: { category } }); await Promise.resolve(); });
    await settle();
    expect(categoryRow("Synthetic Food").disabled).toBe(false);
    expect(button("Add category", section("Categories")).disabled).toBe(false);
    expect(api.categories).toHaveBeenCalledTimes(2);
  });

  it("drops a save that finishes after the view reloaded", async () => {
    let late: (value: BudgetResult<{ rule: RuleSaved }>) => void = () => undefined;
    const api = fakeApi();
    await show(api);
    await click(button("Edit"));
    const form = section("Rules").querySelector("form");
    await act(async () => { form?.requestSubmit(); await Promise.resolve(); });
    await settle();
    api.saveRule.mockImplementationOnce(() => new Promise((resolve) => { late = resolve; }));
    await click(button("Save rule"));
    await show(api, { revision: 1 });
    await act(async () => { late({ ok: true, data: { rule: saved } }); await Promise.resolve(); });
    await settle();
    expect(api.rules).toHaveBeenCalledTimes(2);
    // The reload discarded the preview the save belonged to; the form is usable again and nothing was reloaded twice.
    expect(host.querySelector('[role="region"]')).toBeNull();
    expect(button("Preview").disabled).toBe(false);
  });

  it("aborts pending reads and discards another actor's late categories and rules", async () => {
    const tags = { alice: "A".repeat(43), bob: "B".repeat(43) };
    let actor: "alice" | "bob" = "alice";
    const pending = new Map<string, { resolve: (response: Response) => void; signal: AbortSignal }>();
    const fetchMock = vi.fn((input: unknown, init?: RequestInit) => {
      const path = (input as URL).pathname;
      if (path.endsWith("/api/me/capabilities")) {
        return Promise.resolve(new Response(JSON.stringify({
          user: actor, actor_tag: tags[actor], installation_permissions: [], own_budget: {
            permissions: ["budget.view"], can_view: true, can_use_receipts: true, can_manage_devices: true,
          }, delegated_receipts: null, household_available: false,
        }), { status: 200 }));
      }
      if (actor === "bob") {
        return new Promise<Response>((resolve) => {
          pending.set(path, { resolve, signal: init?.signal as AbortSignal });
        });
      }
      if (path.endsWith("/categories")) return Promise.resolve(new Response(JSON.stringify({ categories: categoryList }), { status: 200 }));
      if (path.endsWith("/rules")) return Promise.resolve(new Response(JSON.stringify({ rules: [rule] }), { status: 200 }));
      throw new Error(`unexpected request ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const api = createBudgetApi("alice");
    expect((await api.capabilities()).ok).toBe(true);
    const onAccessLoss = vi.fn((result: { ok: boolean }) => !result.ok);
    const render = async (revision: number) => {
      await act(async () => {
        root.render(<CategoriesView api={api} accounts={accounts} revision={revision} onAccessLoss={onAccessLoss} />);
        await Promise.resolve();
      });
    };
    await render(0);
    await settle();
    expect(host.textContent).toContain("Synthetic Food");
    expect(host.textContent).toContain("Synthetic Grocer");

    actor = "bob";
    await render(1);
    await settle();
    expect(pending.size).toBe(2);
    expect(await api.capabilities()).toEqual({ ok: false, status: 409, error: "budget_actor_changed" });
    for (const [path, request] of pending) {
      const body = path.endsWith("/categories")
        ? { categories: [{ ...category, id: "cat-bob", name: "Bob Private Category" }] }
        : { rules: [{ ...rule, id: "rule-bob", value: "Bob Private Rule" }] };
      request.resolve(new Response(JSON.stringify(body), { status: 200 }));
    }
    await settle();
    expect([...pending.values()].every(({ signal }) => signal.aborted)).toBe(true);
    expect(host.textContent).not.toContain("Bob Private");
    expect(host.textContent).not.toContain("Synthetic Food");
    const calls = fetchMock.mock.calls.length;
    expect(await api.rules()).toEqual({ ok: false, status: 409, error: "budget_actor_changed" });
    expect(fetchMock).toHaveBeenCalledTimes(calls);
  });
});
