// Synthetic chart rows only. A labelled amount stays out of Other even when it is small.
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { monthlyBudgetOf, monthlyBudgetSeriesOf } from "./fixtures";
import type { AnalyticsMonthlyBudgetSeriesMonth } from "./model";
import { monthBudgetLabels } from "./month_budget";
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
      oneOff("Remont łazienki", "5000"),
    ];
    const model = buildCategoryChartModel(
      [{
        period: "2026-11", has_activity: true, is_current_month: true, income_estimate_partial: false,
        provisional: false, start_budget: "0", spent: "5060", categories,
      }],
      [],
      options,
    );
    expect(model.displayed.map((item) => item.id)).toEqual(["c1", "c2", "c3", "c4", "c5"]);
    expect(model.oneOffs.map((item) => item.name)).toEqual(["Remont łazienki"]);
    expect(model.displayed.some((item) => item.oneOff)).toBe(false);
    const slices = model.stacks[0]?.slices ?? [];
    expect(slices.find((slice) => slice.key === "__other_pos__")?.value).toBe(10n);
    expect(slices.find((slice) => slice.key === ONE_OFF_KEY)).toMatchObject({
      label: "Remont łazienki", value: 5000n,
    });
    expect(slices.some((slice) => slice.key.includes("Remont"))).toBe(false);
    expect(model.stacks[0]?.posTotal).toBe(5060n);
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
  });

  it("marks the labelled bar in the legend and the data table", async () => {
    const series = monthlyBudgetSeriesOf({
      months: [{
        period: "2026-11", has_activity: true, is_current_month: true, income_estimate_partial: false,
        provisional: false, start_budget: "0", spent: "61",
        categories: [
          category("c1", "10"), category("c2", "10"), category("c3", "10"),
          category("c4", "10"), category("c5", "10"), category("c6", "10"),
          oneOff("Remont łazienki", "1"),
        ],
      }],
    });
    await act(async () => {
      root.render(<MonthBudgetCharts budget={monthlyBudgetOf()} series={series} locale="pl" text={monthBudgetLabels.pl} />);
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
