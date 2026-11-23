import { describe, expect, it } from "vitest";
import { groupCategoryTotals, hasCategoryParents, listCategoryTree, parentChoices, rollupNetAmounts, sumMoney,
  visibleCategoryRows } from "./category_tree";
import type { CategoryAmount, CategoryLink } from "./category_tree";

const links: CategoryLink[] = [
  { id: "transport", name: "Synthetic Transport", archived: false, parent_id: null },
  { id: "car", name: "Synthetic Car", archived: false, parent_id: "transport" },
  { id: "fuel", name: "Synthetic Fuel", archived: false, parent_id: "car" },
  { id: "taxi", name: "Synthetic Taxi", archived: false, parent_id: "transport" },
  { id: "food", name: "Synthetic Food", archived: false, parent_id: null },
];

function amount(id: string | null, expenses: string, refunds: string, net: string, name: string | null = id): CategoryAmount {
  return { category_id: id, name, archived: false, expenses, refunds, net_spending: net };
}

describe("category tree", () => {
  it("sums decimal strings and skips a token it cannot parse", () => {
    expect(sumMoney(["1.50", "2", "nope"])).toBe("3.5");
    expect(sumMoney([])).toBe("0");
  });

  it("groups spending under parents and keeps uncategorised at the end", () => {
    const rows = groupCategoryTotals([
      amount("transport", "1", "0", "1", "Synthetic Transport"),
      amount("fuel", "8", "1", "7", "Synthetic Fuel"),
      amount("taxi", "20", "0", "20", "Synthetic Taxi"),
      amount(null, "5", "0", "5", null),
    ], links, "en");
    expect(rows.map((row) => [row.name, row.depth, row.expenses, row.refunds, row.net_spending])).toEqual([
      ["Synthetic Transport", 0, "29", "1", "28"],
      ["Synthetic Car", 1, "8", "1", "7"],
      ["Synthetic Fuel", 2, "8", "1", "7"],
      ["Synthetic Taxi", 1, "20", "0", "20"],
      ["", 0, "5", "0", "5"],
    ]);
    expect(rows[0]?.has_children).toBe(true);
    expect(rows.at(-1)?.category_id).toBeNull();
  });

  it("hides a collapsed subtree, including grandchildren", () => {
    const rows = groupCategoryTotals([
      amount("fuel", "8", "0", "8"),
      amount("taxi", "2", "0", "2"),
    ], links, "en");
    const hidden = visibleCategoryRows(rows, new Set(["transport"]));
    expect(hidden.map((row) => row.category_id)).toEqual(["transport"]);
    expect(hidden[0]?.net_spending).toBe("10");
    expect(visibleCategoryRows(rows, new Set(["car"])).map((row) => row.category_id)).toEqual(["transport", "car", "taxi"]);
  });

  it("offers only parents that keep the subtree within three levels", () => {
    expect(hasCategoryParents(links)).toBe(true);
    expect(hasCategoryParents(links.map((item) => ({ ...item, parent_id: null })))).toBe(false);
    const creating = parentChoices(links, null, "en").map((item) => item.id);
    expect(creating).toContain("transport");
    expect(creating).toContain("car");
    expect(creating).not.toContain("fuel");
    const movingCar = parentChoices(links, "car", "pl").map((item) => item.id);
    expect(movingCar).not.toContain("car");
    expect(movingCar).not.toContain("fuel");
    expect(movingCar).toContain("food");
    expect(listCategoryTree(links, "en").map((row) => [row.category.name, row.depth])).toEqual([
      ["Synthetic Food", 0],
      ["Synthetic Transport", 0],
      ["Synthetic Car", 1],
      ["Synthetic Fuel", 2],
      ["Synthetic Taxi", 1],
    ]);
  });

  it("rolls chart amounts onto the top-level category", () => {
    const parents = new Map(links.map((item) => [item.id, item.parent_id ?? null]));
    expect(rollupNetAmounts([
      { category_id: "fuel", net_spending: "8" },
      { category_id: "taxi", net_spending: "2" },
      { category_id: "food", net_spending: "4" },
      { category_id: null, net_spending: "1" },
    ], parents, null)).toEqual([
      { category_id: "transport", net_spending: "10" },
      { category_id: "food", net_spending: "4" },
      { category_id: null, net_spending: "1" },
    ]);
  });
});
