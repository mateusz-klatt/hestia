// Synthetic chart rows only. A labelled amount stays out of Other even when it is small.
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { loadLocale } from "../../i18n";
import { monthlyBudgetOf, monthlyBudgetSeriesOf } from "./fixtures";
import type { AnalyticsMonthlyBudgetSeriesMonth } from "./model";
import { MonthBudgetCharts, buildCategoryChartModel } from "./month_budget_charts";

type SeriesCategory = AnalyticsMonthlyBudgetSeriesMonth["categories"][number];

function category(id: string, net: string): SeriesCategory {
  return { category_id: id, net_spending: net };
}

const ONE_OFF_KEY = "ab".repeat(32);

function oneOff(name: string, net: string, key = ONE_OFF_KEY): SeriesCategory {
  return { category_id: null, net_spending: net, one_off: true, name, one_off_key: key };
}

const options = { uncategorised: "Uncategorised", unknownCategory: "Category", otherCategory: "Other" };

describe("one-off chart entries", () => {
  it("draws a labelled amount beside Other and never folds it in", () => {
    const categories = [
      category("c1", "10"), category("c2", "10"), category("c3", "10"),
      category("c4", "10"), category("c5", "10"), category("c6", "10"),
      category("c7", "10"), category("c8", "10"), category("c9", "10"),
      oneOff("Remont łazienki", "5000"),
    ];
    const model = buildCategoryChartModel(
      [{
        period: "2026-11", has_activity: true, is_current_month: true, income_estimate_partial: false,
        provisional: false, start_budget: "0", spent: "5090", categories,
      }],
      [],
      options,
    );
    expect(model.displayed.map((item) => item.id)).toEqual(["c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8"]);
    expect(model.oneOffs.map((item) => item.name)).toEqual(["Remont łazienki"]);
    expect(model.displayed.some((item) => item.oneOff)).toBe(false);
    const slices = model.stacks[0]?.slices ?? [];
    expect(slices.find((slice) => slice.key === "__other_pos__")?.value).toBe(10n);
    expect(slices.find((slice) => slice.key === ONE_OFF_KEY)).toMatchObject({
      label: "Remont łazienki", value: 5000n,
    });
    expect(slices.some((slice) => slice.key.includes("Remont"))).toBe(false);
    expect(model.stacks[0]?.posTotal).toBe(5090n);
  });

  it("keeps a categorised one-off beside its category and falls back when the hash is missing", () => {
    const model = buildCategoryChartModel(
      [{
        period: "2026-11", has_activity: true, is_current_month: true, income_estimate_partial: false,
        provisional: false, start_budget: "0", spent: "8",
        categories: [
          { category_id: "food", net_spending: "10" },
          { category_id: "food", net_spending: "-4", one_off: true, name: "Zwrot kaucji", one_off_key: null },
          { category_id: null, net_spending: "2", one_off: true, name: "Alpha", one_off_key: "" },
          { category_id: "food", net_spending: "1", one_off: true, name: "Alpha", one_off_key: "ff".repeat(32) },
          { category_id: "misc", net_spending: "3", one_off: true, name: "", one_off_key: null },
        ],
      }],
      [{ category_id: "food", name: "Food", archived: false }],
      options,
    );
    const hash = "ff".repeat(32);
    expect(model.oneOffs.map((item) => [item.name, item.seriesKey])).toEqual([
      ["Alpha", hash],
      ["Alpha", "one-off:"],
      ["Zwrot kaucji", "one-off:food"],
    ]);
    const food = model.allCategories.find((item) => item.id === "food");
    const labelled = model.oneOffs.find((item) => item.seriesKey === "one-off:food");
    expect(labelled).toMatchObject({ id: "food", hatched: true, color: food?.color });
    expect(food?.hatched).toBe(false);
    expect(model.byMonthAndCategory.get("2026-11")?.get("food")).toBe("10");
    expect(model.byMonthAndCategory.get("2026-11")?.get("one-off:food")).toBe("-4");
    expect(model.displayed.some((item) => item.oneOff)).toBe(false);
    expect(model.oneOffs.some((item) => item.id === "misc")).toBe(false);
    const slices = model.stacks[0]?.slices ?? [];
    expect(slices.find((slice) => slice.key === "one-off:food")).toMatchObject({ value: -4n, hatched: true });
    expect(slices.find((slice) => slice.key === "one-off:")).toMatchObject({ value: 2n, hatched: true });
  });

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
    await loadLocale("en");
  });

  it("marks the labelled bar in the legend and the data table", async () => {
    const series = monthlyBudgetSeriesOf({
      months: [{
        period: "2026-11", has_activity: true, is_current_month: true, income_estimate_partial: false,
        provisional: false, start_budget: "0", spent: "61",
        categories: [
          category("c1", "10"), category("c2", "10"), category("c3", "10"),
          category("c4", "10"), category("c5", "10"), category("c6", "10"),
          category("c7", "10"), category("c8", "10"), category("c9", "10"),
          oneOff("Remont łazienki", "1"),
        ],
      }],
    });
    await loadLocale("pl");
    await act(async () => {
      root.render(<MonthBudgetCharts budget={monthlyBudgetOf()} series={series} paceProjection={null} locale="pl" />);
      await Promise.resolve();
    });
    const legend = host.querySelector("li.budget-one-off");
    expect(legend?.textContent).toContain("Remont łazienki");
    expect(legend?.textContent).toContain("jednorazowe");
    const tableRow = host.querySelector("tr.budget-one-off");
    expect(tableRow?.textContent).toContain("Remont łazienki");
    expect(tableRow?.textContent).toContain("jednorazowe");
    expect(host.textContent).toContain("Pozostałe");
    expect(legend?.textContent).not.toContain("Pozostałe");
  });
});
