// Synthetic data only: no real account, transaction or bank values.
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BudgetResult } from "../api";
import { account, connection } from "../fixtures";
import { loadLocale } from "../../i18n";
import { NEUTRAL_CATEGORY_COLOR, buildCategoryColors, parseHexColor, relativeLuminance } from "./category_colors";
import { overviewOf, staleFreshness, totals } from "./fixtures";
import { oneOffRowKey } from "./model";
import type { AnalyticsCategory, AnalyticsOverview, AnalyticsScope } from "./model";
import { OverviewPanel } from "./overview";

const fsModule = "node:fs";
const { readFileSync } = (await import(/* @vite-ignore */ fsModule)) as {
  readFileSync: (path: string, encoding: "utf8") => string;
};

type Answer = BudgetResult<AnalyticsOverview>;

const baseScope = overviewOf("2026-11").scope;
function scope(changes: Partial<AnalyticsScope>): AnalyticsScope {
  return { ...baseScope, ...changes };
}

function rich(period: string, changes: Partial<AnalyticsOverview> = {}): AnalyticsOverview {
  return overviewOf(period, {
    currencies: [
      totals("EUR", { expenses: "89.9", net_spending: "89.9" }),
      totals("PLN", { expenses: "123456789012345678901234567890.01", refunds: "0.000001",
        net_spending: "123456789012345678901234567890.009999", income: "8450", unclassified_credit: "12.3",
        transfer_debits: "1200", transfer_credits: "1200", excluded_debits: "5", excluded_credits: "0" }),
    ],
    categories: [
      { currency: "PLN", category_id: "cat-food", name: "Synthetic Food", archived: false, expenses: "310", refunds: "10", net_spending: "300" },
      { currency: "PLN", category_id: "cat-old", name: "Synthetic Old", archived: true, expenses: "7", refunds: "0", net_spending: "7" },
      { currency: "PLN", category_id: null, name: null, archived: false, expenses: "50", refunds: "0", net_spending: "50" },
      { currency: "EUR", category_id: "cat-travel", name: "Synthetic Travel", archived: false, expenses: "89.9", refunds: "0", net_spending: "89.9" },
    ],
    scope: scope({ selected_accounts: 2, excluded_accounts: 1, uncertain_count: 0, unresolved_count: 2, import_unresolved_count: 1 }),
    ...changes,
  });
}

const accounts = [account({ id: "acc-1", display: { product: "Synthetic Card", currency: "PLN", masked_suffix: "0002",
  source: "offline_import", observed_at: 1, imported_at: 2 } }), account({ id: "acc-2" })];

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

async function show(overview: (period: string) => Promise<Answer>,
  options: { period?: string; current?: boolean; revision?: number; lost?: boolean;
    locale?: string; onCurrencies?: (currencies: string[]) => void;
    loadCategories?: () => Promise<BudgetResult<{ categories: AnalyticsCategory[] }>> } = {}) {
  const onAccessLoss = vi.fn(() => options.lost === true);
  const categories = options.loadCategories === undefined ? {} : { loadCategories: options.loadCategories };
  await act(async () => {
    root.render(<OverviewPanel overview={overview} period={options.period ?? "2026-11"} current={options.current ?? true}
      accounts={accounts} locale={options.locale ?? "en"} revision={options.revision ?? 0}
      onAccessLoss={onAccessLoss} onCurrencies={options.onCurrencies} {...categories} />);
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
    expect(eur?.textContent).not.toContain("refunds");
    expect(eur?.textContent).not.toContain("To classify");
    expect(eur?.textContent).not.toContain("Own transfers");
    expect(eur?.textContent).not.toContain("Left out");
    expect(pln?.textContent).toContain("123,456,789,012,345,678,901,234,567,890.009999 PLN");
    expect(pln?.textContent).toContain("before refunds 123,456,789,012,345,678,901,234,567,890.01 PLN · refunds 0.000001 PLN");
    expect(pln?.textContent).toContain("To classify12.30 PLN");
    expect(pln?.textContent).toContain("out 1,200.00 PLN · in 1,200.00 PLN");
    expect(pln?.textContent).toContain("Left out by youout 5.00 PLN · in 0.00 PLN");
    expect(host.textContent).toContain("2 accounts in the budget (1 excluded)");
    expect(host.textContent).toContain("still in progress");
    expect(host.textContent).toContain("Not counted: 2 unrecognised · 1 unrecognised from import");
    expect(host.textContent).not.toContain("uncertain");
    expect(host.querySelector('[aria-label="Incomplete data"]')).toBeNull();
  });

  it("wraps each amount of a compound total so the line can break between them", async () => {
    await loadLocale("pl");
    await show((period) => Promise.resolve<Answer>({ ok: true, data: rich(period) }), { locale: "pl" });
    const card = host.querySelector('.budget-total[aria-label="PLN"]');
    const ddAfter = (label: string) => [...card?.querySelectorAll("dt") ?? []].find((item) => item.textContent === label)?.nextElementSibling;
    const transfer = ddAfter("Przelewy własne");
    const excluded = ddAfter("Pominięte przez Ciebie");
    const spending = ddAfter("Wydatki");
    expect(transfer?.classList.contains("budget-total-split")).toBe(true);
    expect(excluded?.classList.contains("budget-total-split")).toBe(true);
    expect(spending?.classList.contains("budget-total-split")).toBe(false);
    expect([...(transfer?.querySelectorAll(".budget-amount") ?? [])].map((item) => item.textContent.replace(/\s/g, " "))).toEqual([
      "1 200,00 PLN", "1 200,00 PLN",
    ]);
    expect([...(excluded?.querySelectorAll(".budget-amount") ?? [])].map((item) => item.textContent.replace(/\s/g, " "))).toEqual([
      "5,00 PLN", "0,00 PLN",
    ]);
    expect(transfer?.textContent.replace(/\s/g, " ")).toBe("wyszło 1 200,00 PLN · weszło 1 200,00 PLN");
    expect([...(transfer?.querySelectorAll(".budget-total-part") ?? [])].map((item) => item.textContent.replace(/\s/g, " "))).toEqual([
      "wyszło 1 200,00 PLN", "weszło 1 200,00 PLN",
    ]);
    expect(excluded?.textContent.replace(/\s/g, " ")).toBe("wyszło 5,00 PLN · weszło 0,00 PLN");
    expect([...(excluded?.querySelectorAll(".budget-total-part") ?? [])].map((item) => item.textContent.replace(/\s/g, " "))).toEqual([
      "wyszło 5,00 PLN", "weszło 0,00 PLN",
    ]);
    expect(spending?.querySelector(".budget-amount")?.textContent).toContain("PLN");
    const detail = card?.querySelector(".budget-total-detail");
    expect(detail?.classList.contains("budget-total-split")).toBe(true);
    expect(detail?.querySelectorAll(".budget-amount")).toHaveLength(2);
    expect(detail?.querySelectorAll(".budget-total-part")).toHaveLength(2);
    for (const part of card?.querySelectorAll(".budget-total-part") ?? []) {
      expect(part.textContent).not.toContain(" · ");
      expect(part.querySelectorAll(".budget-amount")).toHaveLength(1);
    }
  });

  it("keeps label+amount pairs atomic and category table captions full-width on narrow screens in budget.css", () => {
    const css = readFileSync("src/budget/budget.css", "utf8");
    expect(css).toMatch(/\.budget-total-part\s*\{[^}]*display:\s*inline-block/);
    const mobileBlock = css.slice(css.indexOf("@media (max-width: 640px)"));
    expect(mobileBlock).toMatch(/\.budget-categories\s*\{\s*display:\s*table;\s*width:\s*100%;\s*\}/);
    expect(mobileBlock).toMatch(/\.budget-categories caption\s*\{\s*display:\s*table-caption;\s*\}/);
    expect(mobileBlock).toMatch(/\.budget-categories tbody\s*\{\s*display:\s*table-row-group;\s*\}/);
    expect(mobileBlock).toMatch(/\.budget-categories tbody tr\s*\{[^}]*display:\s*table-row;/);
    expect(mobileBlock).toMatch(/\.budget-categories tbody td\s*\{[^}]*display:\s*table-cell;/);
  });

  it("lists categories under their own currency, marking archived ones and the uncategorised row", async () => {
    await show((period) => Promise.resolve<Answer>({ ok: true, data: rich(period) }));
    const pln = host.querySelector('[aria-label="Spending by category PLN"]');
    expect([...pln?.querySelectorAll("tr") ?? []].map((row) => row.textContent)).toEqual([
      "Synthetic Food300.00 PLN", "Synthetic Old (archived)7.00 PLN", "Uncategorised50.00 PLN"]);
    expect(pln?.querySelector(".is-uncategorised")).not.toBeNull();
    const uncatSwatch = pln?.querySelector<HTMLElement>(".is-uncategorised .budget-cat-swatch");
    expect(uncatSwatch).not.toBeNull();
    const toHex = (css: string) => {
      const m = /^rgb\(\s*(\d+),\s*(\d+),\s*(\d+)\s*\)$/i.exec(css.trim());
      if (m !== null) return `#${[m[1], m[2], m[3]].map((n) => Number(n).toString(16).padStart(2, "0")).join("")}`;
      return parseHexColor(css)?.hex ?? css;
    };
    expect(toHex(uncatSwatch?.style.backgroundColor ?? "")).toBe(NEUTRAL_CATEGORY_COLOR.background);
    const eur = host.querySelector('[aria-label="Spending by category EUR"]');
    expect(eur?.textContent).toContain("Synthetic Travel");
    expect(eur?.textContent).not.toContain("Synthetic Food");
  });

  it("keys a one-off row by one_off_key and keeps the label out of the key", () => {
    const key = "cd".repeat(32);
    const labelled = { category_id: "cat-food", one_off: true, one_off_key: key };
    expect(oneOffRowKey(labelled, 0)).toBe(key);
    expect(oneOffRowKey(labelled, 0).includes("Remont")).toBe(false);
    expect(oneOffRowKey({ category_id: "cat-food", one_off: false, one_off_key: null }, 1)).toBe("cat-food");
    expect(oneOffRowKey({ category_id: null, one_off: false, one_off_key: null }, 2)).toBe("uncategorised-2");
    expect(oneOffRowKey({ category_id: "cat-food", one_off: true, one_off_key: null }, 3)).toBe("one-off:cat-food:3");
  });

  it("shows a labelled amount on its own row with a one-off tag", async () => {
    const data = rich("2026-11");
    data.categories = [...data.categories, {
      currency: "PLN", category_id: null, name: "Remont łazienki", archived: false,
      expenses: "2000", refunds: "0", net_spending: "2000", one_off: true,
      one_off_key: "ab".repeat(32),
    }];
    await show((period) => Promise.resolve<Answer>({ ok: true, data: { ...data, period } }));
    const row = host.querySelector(".budget-one-off");
    expect(row?.textContent).toContain("Remont łazienki");
    expect(row?.textContent).toContain("one-off");
    expect(row?.textContent).toContain("2,000.00 PLN");
    expect(row?.classList.contains("is-uncategorised")).toBe(false);
    await loadLocale("pl");
    await show((period) => Promise.resolve<Answer>({ ok: true, data: { ...data, period } }), { locale: "pl" });
    expect(host.querySelector(".budget-one-off")?.textContent).toContain("jednorazowe");
  });

  it("rolls a parent up from its children and collapses the subtree", async () => {
    const links: AnalyticsCategory[] = [
      { id: "cat-transport", name: "Synthetic Transport", archived: false, version: 1, created_at: 1, updated_at: 1, parent_id: null },
      { id: "cat-fuel", name: "Synthetic Fuel", archived: false, version: 1, created_at: 1, updated_at: 1, parent_id: "cat-transport" },
    ];
    const data = rich("2026-11", {
      categories: [
        { currency: "PLN", category_id: "cat-transport", name: "Synthetic Transport", archived: false, expenses: "10", refunds: "0", net_spending: "10" },
        { currency: "PLN", category_id: "cat-fuel", name: "Synthetic Fuel", archived: false, expenses: "40", refunds: "0", net_spending: "40" },
        { currency: "PLN", category_id: null, name: null, archived: false, expenses: "5", refunds: "0", net_spending: "5" },
      ],
    });
    await show(() => Promise.resolve<Answer>({ ok: true, data }), {
      loadCategories: () => Promise.resolve({ ok: true, data: { categories: links } }),
    });
    const table = host.querySelector('[aria-label="Spending by category PLN"]');
    const rows = [...table?.querySelectorAll("tbody tr") ?? []];
    expect(rows.map((row) => row.getAttribute("data-depth"))).toEqual(["0", "1", "0"]);
    expect(rows.map((row) => [...row.classList].filter((cls) => cls.startsWith("budget-tree-depth-")))).toEqual([
      ["budget-tree-depth-0"],
      ["budget-tree-depth-1"],
      ["budget-tree-depth-0"],
    ]);
    const swatches = rows.map((row) => row.querySelector<HTMLElement>(".budget-cat-swatch")?.style.backgroundColor ?? "");
    const expectedColors = buildCategoryColors(links);
    const toHex = (css: string) => {
      const m = /^rgb\(\s*(\d+),\s*(\d+),\s*(\d+)\s*\)$/i.exec(css.trim());
      if (m !== null) return `#${[m[1], m[2], m[3]].map((n) => Number(n).toString(16).padStart(2, "0")).join("")}`;
      return parseHexColor(css)?.hex ?? css;
    };
    expect(toHex(swatches[0] ?? "")).toBe(expectedColors.get("cat-transport").background);
    expect(toHex(swatches[1] ?? "")).toBe(expectedColors.get("cat-fuel").background);
    expect(toHex(swatches[2] ?? "")).toBe(NEUTRAL_CATEGORY_COLOR.background);
    expect(relativeLuminance(toHex(swatches[1] ?? ""))).toBeGreaterThan(relativeLuminance(toHex(swatches[0] ?? "")));
    expect(rows[0]?.textContent).toContain("Synthetic Transport");
    expect(rows[0]?.textContent).toContain("50.00 PLN");
    expect(rows[1]?.textContent).toContain("Synthetic Fuel");
    expect(rows[1]?.textContent).toContain("40.00 PLN");
    expect(rows[2]?.textContent).toContain("Uncategorised");
    const toggle = rows[0]?.querySelector("button");
    if (toggle === null || toggle === undefined) throw new Error("collapse missing");
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    await act(async () => { toggle.click(); await Promise.resolve(); });
    const after = [...host.querySelectorAll('[aria-label="Spending by category PLN"] tbody tr')];
    expect(after).toHaveLength(2);
    expect(after[0]?.textContent).toContain("50.00 PLN");
    expect(after.map((row) => row.textContent).join(" ")).not.toContain("Synthetic Fuel");
    expect(after[0]?.querySelector("button")?.getAttribute("aria-expanded")).toBe("false");
  });

  it("explains transfers to review, re-check, boundary cases and a limited scan", async () => {
    await show((period) => Promise.resolve<Answer>({ ok: true, data: rich(period, {
      transfer_summary: { suggested: 3, review: 1, accepted: 5, rejected: 2, boundary: 4, scan_limited: true } }) }));
    const notes = [...host.querySelectorAll("[role=note]")].map((note) => note.textContent);
    expect(notes).toEqual([
      "Possible own transfers to review: 3. Until you confirm them they count as spending and income.",
      "Own-transfer decisions to check again after the bank data changed: 1.",
      "Transfers with one side on an excluded account: 4. They count as ordinary spending or income.",
      "The search for own transfers stopped at its limit; some possible pairs may be missing."]);
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
      : Promise.resolve<Answer>({ ok: true, data: overviewOf(period, { currencies: [] }) })));
    await show(overview);
    expect(host.textContent).toContain("Loading totals");
    await show(overview, { period: "2026-10", current: false });
    await act(async () => { late({ ok: true, data: rich("2026-11") }); await Promise.resolve(); });
    await settle();
    expect(host.textContent).toContain("No booked transactions in this period.");
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
    expect(host.textContent).toContain("Loading totals");
    expect(host.textContent).not.toContain("could not be loaded");
  });

  it("names outdated or suspended accounts and marks the totals as incomplete", async () => {
    const suspended = { ...staleFreshness, stale: false, suspended: true };
    await show((period) => Promise.resolve<Answer>({ ok: true, data: rich(period, { scope: scope({
      selected_accounts: 3, excluded_accounts: 0, freshness: [
        { account_id: "acc-1", transactions: staleFreshness, balances: staleFreshness },
        { account_id: "acc-2", transactions: { ...staleFreshness, stale: false }, balances: staleFreshness },
        { account_id: "gone", transactions: suspended, balances: suspended }] }) }) }));
    expect(host.querySelector("[role=note]")?.textContent).toContain("Synthetic Card ••0002, Account");
    expect(host.querySelector("[role=note]")?.textContent).not.toContain("Account 2");
    expect(host.querySelectorAll('[aria-label="Incomplete data"]')).toHaveLength(2);
    expect(host.textContent).not.toContain("Not counted");
    expect(host.textContent).toContain("3 accounts in the budget · change");
  });

  it("names outdated accounts with their bank", async () => {
    const overview = (period: string) => Promise.resolve<Answer>({ ok: true, data: rich(period, { scope: scope({
      freshness: [{ account_id: "acc-2", transactions: staleFreshness, balances: staleFreshness }] }) }) });
    await act(async () => {
      root.render(<OverviewPanel overview={overview} period="2026-11" current accounts={accounts} connections={[connection]}
        locale="en" revision={0} onAccessLoss={() => false} />);
      await Promise.resolve();
    });
    await settle();
    expect(host.querySelector("[role=note]")?.textContent).toContain("Synthetic Bank · Account 2");
  });

  it("reads the shown period again after a reload", async () => {
    const overview = vi.fn((period: string) => Promise.resolve<Answer>({ ok: true, data: rich(period) }));
    await show(overview);
    await show(overview, { revision: 1 });
    expect(overview).toHaveBeenCalledTimes(2);
    expect(overview).toHaveBeenLastCalledWith("2026-11");
  });
});
