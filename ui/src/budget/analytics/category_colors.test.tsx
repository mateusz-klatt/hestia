// Pure unit tests for category colour generation and render tests for legend swatches & badges.
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { loadLocale } from "../../i18n";
import { account } from "../fixtures";
import { CategoryBadge, CategorySwatch } from "./category_badge";
import {
  CASH_ATM_COLOR,
  CASH_ATM_NAME_KEY,
  CATEGORY_PALETTE,
  NEUTRAL_CATEGORY_COLOR,
  OTHER_CATEGORY_COLOR,
  accessibleBadgeText,
  buildCategoryColorMap,
  buildCategoryColors,
  categoryHueToHex,
  categoryNameKey,
  contrastRatio,
  hatchedBadgeGradient,
  hexToOklch,
  oklchToHex,
  oneOffCategoryColor,
  parseHexColor,
  relativeLuminance,
  resolveCategoryColor,
} from "./category_colors";
import type { CategoryColorInput } from "./category_colors";
import {
  annotationOf,
  decisionOf,
  entriesOf,
  entry,
  forecastOf,
  monthlyBudgetCategorySpend,
  monthlyBudgetChartsOf,
  monthlyBudgetExpectedIncome,
  monthlyBudgetOf,
  monthlyBudgetPace,
  monthlyBudgetSeriesOf,
  mortgagesOf,
  overviewOf,
  recurringOf,
} from "./fixtures";
import type { AnalyticsCategory } from "./model";
import { MonthBudgetView } from "./month_budget";
import { MonthBudgetCharts, buildCategoryChartModel } from "./month_budget_charts";
import { TransactionsPanel } from "./transactions";
import { YearView } from "./year";

const sampleTree: CategoryColorInput[] = [
  { id: "transport", parent_id: null },
  { id: "car", parent_id: "transport" },
  { id: "fuel", parent_id: "car" },
  { id: "service", parent_id: "car" },
  { id: "taxi", parent_id: "transport" },
  { id: "transit", parent_id: "transport" },
  { id: "food", parent_id: null },
  { id: "groceries", parent_id: "food" },
  { id: "dining", parent_id: "food" },
  { id: "housing", parent_id: null },
];

function hueDistance(a: number, b: number): number {
  const diff = Math.abs(a - b) % 360;
  return diff > 180 ? 360 - diff : diff;
}

function cssColorToHex(cssColor: string): string {
  const hex = parseHexColor(cssColor);
  if (hex !== null) return hex.hex;
  const rgb = /^rgb\(\s*(\d+),\s*(\d+),\s*(\d+)\s*\)$/i.exec(cssColor.trim());
  if (rgb === null) throw new Error(`Unexpected CSS color: ${cssColor}`);
  return `#${[rgb[1], rgb[2], rgb[3]]
    .map((part) => Number(part).toString(16).padStart(2, "0"))
    .join("")}`;
}

describe("category_colors", () => {
  it("curates 10-12 distinct accessible root hues", () => {
    expect(CATEGORY_PALETTE.length).toBeGreaterThanOrEqual(10);
    expect(CATEGORY_PALETTE.length).toBeLessThanOrEqual(12);
    expect(new Set(CATEGORY_PALETTE.map((item) => item.background)).size).toBe(CATEGORY_PALETTE.length);
  });

  it("assigns the exact same colour to every root and descendant regardless of input list order", () => {
    const forward = buildCategoryColorMap(sampleTree);
    const reversed = buildCategoryColorMap([...sampleTree].reverse());
    const rotated = buildCategoryColorMap([...sampleTree.slice(4), ...sampleTree.slice(0, 4)]);
    const childrenFirst = buildCategoryColorMap([
      ...sampleTree.filter((item) => item.parent_id !== null),
      ...sampleTree.filter((item) => item.parent_id === null),
    ]);

    for (const item of sampleTree) {
      const base = forward.get(item.id);
      expect(base).toBeDefined();
      expect(reversed.get(item.id)).toEqual(base);
      expect(rotated.get(item.id)).toEqual(base);
      expect(childrenFirst.get(item.id)).toEqual(base);
    }

    // Roots that hash to distinct palette slots receive distinct colours; adding or removing roots never shifts other roots.
    const baselineCategories: CategoryColorInput[] = [
      ...Array.from({ length: 30 }, (_, idx) => ({ id: `cat-${String(idx)}`, parent_id: null }))
        .filter((item) => item.id !== "cat-11"),
      { id: "cat-24-child", parent_id: "cat-24" },
      { id: "cat-24-grand", parent_id: "cat-24-child" },
    ];
    const withCat11 = [
      ...baselineCategories,
      { id: "cat-11", parent_id: null },
      { id: "extra-root", parent_id: null },
    ];
    const baseMap = buildCategoryColorMap(baselineCategories);
    const expandedMap = buildCategoryColorMap(withCat11);
    expect(new Set([...baseMap.values()].filter((c) => c.depth === 0).map((c) => c.background)).size).toBeGreaterThanOrEqual(8);
    for (const item of baselineCategories) {
      expect(expandedMap.get(item.id)).toEqual(baseMap.get(item.id));
    }

    // Removing an arbitrary root does not alter any remaining root's colour.
    const reducedMap = buildCategoryColorMap(baselineCategories.filter((item) => item.id !== "cat-0" && item.id !== "cat-15"));
    for (const item of baselineCategories) {
      if (item.id === "cat-0" || item.id === "cat-15") continue;
      expect(reducedMap.get(item.id)).toEqual(baseMap.get(item.id));
    }
  });

  it("uses each persisted server hue without moving other roots or changing descendant shading", () => {
    const items: CategoryColorInput[] = [
      { id: "server", parent_id: null, color_hue: 137 },
      { id: "server-child", parent_id: "server" },
      { id: "fallback", parent_id: null, color_hue: null },
      { id: "cash", parent_id: null, name: "Gotówka (bankomat)", color_hue: 222 },
      { id: "cash-null", parent_id: null, name: "Gotówka (bankomat)", color_hue: null },
    ];
    const colors = buildCategoryColors(items);
    expect(colors.get("server").background).toBe(categoryHueToHex(137));
    expect(colors.get("server").oklch).toEqual({ l: 0.55, c: 0.16, h: 137 });
    expect(hueDistance(colors.get("server-child").oklch.h, 137)).toBeLessThanOrEqual(15);
    expect(colors.get("server-child").oklch.l).toBeGreaterThan(colors.get("server").oklch.l);
    expect(colors.get("fallback")).toEqual(buildCategoryColors([{ id: "fallback", parent_id: null }]).get("fallback"));
    expect(colors.get("cash").background).toBe(categoryHueToHex(222));
    expect(colors.get("cash-null").background).toBe(CASH_ATM_COLOR);
    expect(buildCategoryColors([...items, { id: "added", parent_id: null, color_hue: 300 }]).get("server"))
      .toEqual(colors.get("server"));
    expect(buildCategoryColors(items, { overrides: { server: "#1e88e5" } }).get("server").background).toBe("#1e88e5");
  });

  it("keeps badge text AA-compliant at the server anchor for every hue", () => {
    for (let hue = 0; hue < 360; hue += 1) {
      const background = categoryHueToHex(hue);
      const text = accessibleBadgeText(background);
      expect(contrastRatio(background, text), `hue ${String(hue)}`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("shades children from their parent's hue with monotonic lightness across depth", () => {
    const colors = buildCategoryColors(sampleTree);
    const transport = colors.get("transport");
    const car = colors.get("car");
    const fuel = colors.get("fuel");
    const service = colors.get("service");
    const taxi = colors.get("taxi");

    expect(transport.depth).toBe(0);
    expect(car.depth).toBe(1);
    expect(fuel.depth).toBe(2);
    expect(car.rootId).toBe("transport");
    expect(fuel.rootId).toBe("transport");

    // Monotonic lightness in both perceptual OKLCH L and sRGB relative luminance.
    expect(car.oklch.l).toBeGreaterThan(transport.oklch.l);
    expect(fuel.oklch.l).toBeGreaterThan(car.oklch.l);
    expect(service.oklch.l).toBeGreaterThan(car.oklch.l);
    expect(taxi.oklch.l).toBeGreaterThan(transport.oklch.l);

    expect(relativeLuminance(car.background)).toBeGreaterThan(relativeLuminance(transport.background));
    expect(relativeLuminance(fuel.background)).toBeGreaterThan(relativeLuminance(car.background));
    expect(relativeLuminance(service.background)).toBeGreaterThan(relativeLuminance(car.background));

    // Step size sits around 10-12% OKLCH lightness per level.
    expect(car.oklch.l - transport.oklch.l).toBeGreaterThan(0.08);
    expect(car.oklch.l - transport.oklch.l).toBeLessThan(0.14);
    expect(fuel.oklch.l - car.oklch.l).toBeGreaterThan(0.07);
    expect(fuel.oklch.l - car.oklch.l).toBeLessThan(0.15);

    // Descendants remain in their parent's hue neighbourhood.
    expect(hueDistance(car.oklch.h, transport.oklch.h)).toBeLessThanOrEqual(15);
    expect(hueDistance(fuel.oklch.h, car.oklch.h)).toBeLessThanOrEqual(20);
  });

  it("keeps siblings at the same depth distinct under the same parent", () => {
    const manySiblings: CategoryColorInput[] = [
      { id: "root", parent_id: null },
      ...Array.from({ length: 8 }, (_, idx) => ({ id: `child-${String(idx)}`, parent_id: "root" })),
      ...Array.from({ length: 6 }, (_, idx) => ({ id: `grand-${String(idx)}`, parent_id: "child-0" })),
    ];
    const colors = buildCategoryColors(manySiblings);
    const rootColor = colors.get("root");
    const childHexes = Array.from({ length: 8 }, (_, idx) => colors.get(`child-${String(idx)}`).background);
    const grandHexes = Array.from({ length: 6 }, (_, idx) => colors.get(`grand-${String(idx)}`).background);

    expect(new Set(childHexes).size).toBe(8);
    expect(new Set(grandHexes).size).toBe(6);

    // Every child stays strictly lighter than root, and every grandchild strictly lighter than child-0.
    const child0Lum = relativeLuminance(colors.get("child-0").background);
    const rootLum = relativeLuminance(rootColor.background);
    for (let idx = 0; idx < 8; idx += 1) {
      expect(relativeLuminance(colors.get(`child-${String(idx)}`).background)).toBeGreaterThan(rootLum);
    }
    for (let idx = 0; idx < 6; idx += 1) {
      expect(relativeLuminance(colors.get(`grand-${String(idx)}`).background)).toBeGreaterThan(child0Lum);
    }
  });

  it("uses neutral grey for Uncategorised and Other, and hatches one-off entries", () => {
    const colors = buildCategoryColors(sampleTree);
    const uncategorised = colors.get(null);
    expect(uncategorised).toEqual(NEUTRAL_CATEGORY_COLOR);
    expect(uncategorised.neutral).toBe(true);
    expect(uncategorised.hatched).toBe(false);
    expect(uncategorised.oklch.c).toBeLessThan(0.03);

    expect(OTHER_CATEGORY_COLOR.neutral).toBe(true);
    expect(OTHER_CATEGORY_COLOR.hatched).toBe(true);
    expect(OTHER_CATEGORY_COLOR.oklch.c).toBeLessThan(0.03);

    const oneOffFood = oneOffCategoryColor(colors, "food");
    expect(oneOffFood.background).toBe(colors.get("food").background);
    expect(oneOffFood.hatched).toBe(true);
    expect(oneOffFood.neutral).toBe(false);

    const oneOffUncat = oneOffCategoryColor(colors, null);
    expect(oneOffUncat.background).toBe(NEUTRAL_CATEGORY_COLOR.background);
    expect(oneOffUncat.hatched).toBe(true);
    expect(oneOffUncat.neutral).toBe(true);
  });

  it("meets WCAG AA (>= 4.5:1) contrast for badge text across palette, shades, neutral, and overrides", () => {
    // Check all 12 curated roots and their depth 1 & depth 2 shades.
    const fullTree: CategoryColorInput[] = [];
    for (let i = 0; i < CATEGORY_PALETTE.length; i += 1) {
      const rootId = `r-${String(i)}`;
      const childId = `c-${String(i)}`;
      const grandId = `g-${String(i)}`;
      fullTree.push({ id: rootId, parent_id: null }, { id: childId, parent_id: rootId }, { id: grandId, parent_id: childId });
    }
    const colors = buildCategoryColors(fullTree);
    for (const color of colors.entries.values()) {
      expect(["#000000", "#ffffff"]).toContain(color.text);
      expect(contrastRatio(color.background, color.text)).toBeGreaterThanOrEqual(4.5);
    }

    // Neutral colors meet WCAG AA.
    expect(contrastRatio(NEUTRAL_CATEGORY_COLOR.background, NEUTRAL_CATEGORY_COLOR.text)).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(OTHER_CATEGORY_COLOR.background, OTHER_CATEGORY_COLOR.text)).toBeGreaterThanOrEqual(4.5);

    // Extreme light, mid, and dark backgrounds pick black or white automatically and clear 4.5:1.
    expect(accessibleBadgeText("#000000")).toBe("#ffffff");
    expect(accessibleBadgeText("#ffffff")).toBe("#000000");
    expect(accessibleBadgeText("#f5e642")).toBe("#000000");
    expect(accessibleBadgeText("#1b3a6b")).toBe("#ffffff");
    for (const hex of ["#000000", "#1b3a6b", "#777777", "#808080", "#d2691e", "#f5e642", "#b8e986", "#ffffff"]) {
      const text = accessibleBadgeText(hex);
      expect(contrastRatio(hex, text)).toBeGreaterThanOrEqual(4.5);
    }

    // Hatched badge stripe stays AA-compliant against badge text.
    const hatched = oneOffCategoryColor(colors, "r-0");
    const gradient = hatchedBadgeGradient(hatched);
    const hexMatches = gradient.match(/#[0-9a-f]{6}/gi) ?? [];
    expect(hexMatches.length).toBeGreaterThanOrEqual(2);
    for (const stripeHex of hexMatches) {
      expect(contrastRatio(stripeHex, hatched.text)).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("supports the optional per-root colour override hook without changing other roots", () => {
    const items: CategoryColorInput[] = [
      { id: "transport", parent_id: null },
      { id: "fuel", parent_id: "transport" },
      { id: "light-root", parent_id: null },
      { id: "light-child", parent_id: "light-root" },
      { id: "bad-override", parent_id: null },
      ...Array.from({ length: 15 }, (_, idx) => ({ id: `cat-${String(idx)}`, parent_id: null })),
      { id: "cat-6-child", parent_id: "cat-6" },
    ];
    const withoutOverrides = buildCategoryColors(items);
    const colors = buildCategoryColors(items, {
      overrides: {
        transport: "#1e88e5",
        "light-root": "#eef480",
        "bad-override": "not-a-color",
      },
    });
    expect(colors.get("transport").background).toBe("#1e88e5");
    expect(hueDistance(colors.get("fuel").oklch.h, hexToOklch("#1e88e5")?.h ?? 0)).toBeLessThanOrEqual(15);
    expect(relativeLuminance(colors.get("fuel").background)).toBeGreaterThan(relativeLuminance("#1e88e5"));

    // Very light root override darkens monotonically instead of clipping into white.
    expect(colors.get("light-root").background).toBe("#eef480");
    expect(colors.get("light-root").text).toBe("#000000");
    expect(relativeLuminance(colors.get("light-child").background)).toBeLessThan(relativeLuminance("#eef480"));

    // Invalid override falls back safely, and all unrelated roots/descendants stay unchanged.
    expect(parseHexColor(colors.get("bad-override").background)).not.toBeNull();
    expect(resolveCategoryColor(colors.entries, "unlisted").background).toBeTruthy();
    for (const item of items) {
      if (item.id === "transport" || item.id === "fuel" || item.id === "light-root" || item.id === "light-child") continue;
      expect(colors.get(item.id)).toEqual(withoutOverrides.get(item.id));
    }
  });

  it("pins Gotówka (bankomat) to a fixed slate for the root and its descendants only", () => {
    expect(categoryNameKey("Gotówka (bankomat)")).toBe(CASH_ATM_NAME_KEY);
    expect(categoryNameKey("  GOTÓWKA   (bankomat) ")).toBe(CASH_ATM_NAME_KEY);
    expect(categoryNameKey("Goto\u0301wka (bankomat)")).toBe(CASH_ATM_NAME_KEY);
    expect(categoryNameKey("Gotowka (bankomat)")).not.toBe(CASH_ATM_NAME_KEY);

    const dom = { id: "dom", parent_id: null, name: "Dom" };
    const food = { id: "food", parent_id: null, name: "Jedzenie" };
    const cash = { id: "cash-atm", parent_id: null, name: "Gotówka (bankomat)" };
    const child = { id: "cash-child", parent_id: "cash-atm", name: "Wypłata" };
    const plain = buildCategoryColors([
      dom,
      food,
      { ...cash, name: "Gotowka (bankomat)" },
      child,
    ]);
    const colored = buildCategoryColors([dom, food, cash, child]);

    expect(colored.get("cash-atm").background).toBe(CASH_ATM_COLOR);
    expect(colored.get("cash-atm").background).not.toBe(plain.get("cash-atm").background);
    expect(colored.get("cash-atm").background).not.toBe(NEUTRAL_CATEGORY_COLOR.background);
    expect(CATEGORY_PALETTE.some((swatch) => swatch.background === CASH_ATM_COLOR)).toBe(false);
    expect(colored.get("cash-atm").oklch.c).toBeGreaterThan(0.02);
    expect(colored.get("cash-atm").oklch.c).toBeLessThan(0.08);
    for (const name of ["crimson", "terracotta", "magenta"] as const) {
      const swatch = CATEGORY_PALETTE.find((item) => item.name === name);
      expect(swatch).toBeDefined();
      expect(hueDistance(colored.get("cash-atm").oklch.h, swatch?.oklch.h ?? 0)).toBeGreaterThan(40);
    }
    expect(contrastRatio(colored.get("cash-atm").background, colored.get("cash-atm").text)).toBeGreaterThanOrEqual(4.5);

    expect(colored.get("cash-child").rootId).toBe("cash-atm");
    expect(colored.get("cash-child").background).not.toBe(plain.get("cash-child").background);
    expect(colored.get("cash-child").background).not.toBe(CASH_ATM_COLOR);
    expect(hueDistance(colored.get("cash-child").oklch.h, colored.get("cash-atm").oklch.h)).toBeLessThanOrEqual(15);
    expect(contrastRatio(colored.get("cash-child").background, colored.get("cash-child").text)).toBeGreaterThanOrEqual(4.5);

    // Other roots keep the id hash. A nested category with the cash name does not recolour its parent.
    expect(colored.get("dom")).toEqual(plain.get("dom"));
    expect(colored.get("food")).toEqual(plain.get("food"));
    const nested = { id: "nested-cash", parent_id: "dom", name: "Gotówka (bankomat)" };
    const nestedBase = buildCategoryColors([dom, { ...nested, name: "Inne" }]);
    const nestedNamed = buildCategoryColors([dom, nested]);
    expect(nestedNamed.get("dom")).toEqual(nestedBase.get("dom"));
    expect(nestedNamed.get("nested-cash")).toEqual(nestedBase.get("nested-cash"));

    // The existing override hook still wins, and still shades only that root's descendants.
    const hooked = buildCategoryColors([dom, food, cash, child], { overrides: { "cash-atm": "#1e88e5" } });
    expect(hooked.get("cash-atm").background).toBe("#1e88e5");
    expect(hooked.get("dom")).toEqual(colored.get("dom"));
    expect(hooked.get("food")).toEqual(colored.get("food"));
    expect(hueDistance(hooked.get("cash-child").oklch.h, hexToOklch("#1e88e5")?.h ?? 0)).toBeLessThanOrEqual(15);

    const chartOptions = { uncategorised: "Uncategorised", unknownCategory: "Category", otherCategory: "Other" };
    const month = {
      period: "2026-10", has_activity: true, is_current_month: false, income_estimate_partial: false,
      provisional: false, start_budget: "0", spent: "50",
      categories: [
        { category_id: "cash-atm", net_spending: "40" },
        { category_id: "dom", net_spending: "10" },
      ],
    };
    const labels = [
      { category_id: "cash-atm", name: "Gotówka (bankomat)", archived: false },
      { category_id: "dom", name: "Dom", archived: false },
    ];
    const fromLabels = buildCategoryChartModel([month], labels, chartOptions);
    expect(fromLabels.displayed.find((item) => item.id === "cash-atm")?.color).toBe(CASH_ATM_COLOR);
    expect(fromLabels.displayed.find((item) => item.id === "dom")?.color).toBe(colored.get("dom").background);

    const fromTree = buildCategoryChartModel([month], labels, chartOptions, [
      { id: "cash-atm", name: "Gotówka (bankomat)", archived: false, parent_id: null },
      { id: "cash-child", name: "Wypłata", archived: false, parent_id: "cash-atm" },
      { id: "dom", name: "Dom", archived: false, parent_id: null },
    ]);
    expect(fromTree.displayed.map((item) => item.id)).toEqual(["cash-atm", "dom"]);
    expect(fromTree.displayed.find((item) => item.id === "cash-atm")?.color).toBe(CASH_ATM_COLOR);
    expect(fromTree.displayed.find((item) => item.id === "dom")?.color).toBe(colored.get("dom").background);
  });

  it("handles OKLCH anchors and clamps out-of-gamut inputs accurately", () => {
    expect(oklchToHex(0, 0, 0)).toBe("#000000");
    expect(oklchToHex(1, 0, 0)).toBe("#ffffff");
    expect(oklchToHex(-0.25, 0.2, 40)).toBe("#000000");
    expect(oklchToHex(1.3, 0.2, 40)).toBe("#ffffff");

    // Achromatic anchor stays neutral grey with matching L.
    const neutralHex = oklchToHex(0.5, 0, 120);
    const neutralRgb = parseHexColor(neutralHex);
    expect(neutralRgb).not.toBeNull();
    expect(neutralRgb?.r).toBe(neutralRgb?.g);
    expect(neutralRgb?.g).toBe(neutralRgb?.b);
    const neutralOklch = hexToOklch(neutralHex);
    expect(neutralOklch?.l ?? 0).toBeCloseTo(0.5, 2);
    expect(neutralOklch?.c ?? 1).toBeLessThan(0.005);

    // Negative chroma clamps cleanly and hues wrap modulo 360.
    expect(oklchToHex(0.5, -0.3, 40)).toBe(oklchToHex(0.5, 0, 40));
    expect(oklchToHex(0.6, 0.12, -30)).toBe(oklchToHex(0.6, 0.12, 330));
    expect(oklchToHex(0.6, 0.12, 690)).toBe(oklchToHex(0.6, 0.12, 330));

    // Curated anchors and sRGB primaries stay consistent through OKLCH round-tripping.
    for (const swatch of CATEGORY_PALETTE) {
      const roundTrip = hexToOklch(swatch.background);
      expect(roundTrip).not.toBeNull();
      expect(Math.abs((roundTrip?.l ?? 0) - swatch.oklch.l)).toBeLessThan(0.01);
      expect(hueDistance(roundTrip?.h ?? 0, swatch.oklch.h)).toBeLessThan(2);
    }
    expect(hueDistance(hexToOklch("#ff0000")?.h ?? 0, 29.2)).toBeLessThan(2);
    expect(hueDistance(hexToOklch("#00ff00")?.h ?? 0, 142.5)).toBeLessThan(2);
    expect(hueDistance(hexToOklch("#0000ff")?.h ?? 0, 264.1)).toBeLessThan(2);

    // Out-of-gamut chroma reduces chroma in-gamut while preserving lightness and hue.
    for (const [targetL, targetH] of [[0.7, 145], [0.55, 260], [0.85, 95]] as const) {
      const hex = oklchToHex(targetL, 0.5, targetH);
      expect(parseHexColor(hex)).not.toBeNull();
      const result = hexToOklch(hex);
      expect(result).not.toBeNull();
      expect(Math.abs((result?.l ?? 0) - targetL)).toBeLessThan(0.015);
      expect(hueDistance(result?.h ?? 0, targetH)).toBeLessThan(3);
    }
  });

  it("handles deep trees beyond depth 3 without identical clamping, 5000-level chains, cycles, and ~50 categories", () => {
    // Depths 0..7 stay distinguishable even near the upper lightness anchor (including amber L=0.60 and light override).
    const deepLevels: CategoryColorInput[] = Array.from({ length: 8 }, (_, idx) => ({
      id: `d-${String(idx)}`,
      parent_id: idx === 0 ? null : `d-${String(idx - 1)}`,
    }));
    const amberSlotIdx = CATEGORY_PALETTE.findIndex((swatch) => swatch.name === "amber");
    for (const overrideHex of [undefined, CATEGORY_PALETTE[amberSlotIdx]?.background, "#eef480"]) {
      const overrides = overrideHex === undefined ? undefined : { "d-0": overrideHex };
      const deepColors = buildCategoryColors(deepLevels, overrides === undefined ? undefined : { overrides });
      const hexes = deepLevels.map((item) => deepColors.get(item.id).background);
      for (let idx = 1; idx < hexes.length; idx += 1) {
        expect(hexes[idx]).not.toBe(hexes[idx - 1]);
      }
      for (const item of deepLevels) {
        const entryColor = deepColors.get(item.id);
        expect(contrastRatio(entryColor.background, entryColor.text)).toBeGreaterThanOrEqual(4.5);
      }
    }

    // 5000-level deep tree does not throw RangeError and resolves every node cleanly.
    const chain5000: CategoryColorInput[] = Array.from({ length: 5000 }, (_, idx) => ({
      id: `lvl-${String(idx)}`,
      parent_id: idx === 0 ? null : `lvl-${String(idx - 1)}`,
    }));
    const chainMap = buildCategoryColorMap(chain5000);
    expect(chainMap.size).toBe(5000);
    expect(chainMap.get("lvl-4999")?.rootId).toBe("lvl-0");
    expect(chainMap.get("lvl-4999")?.depth).toBe(4999);
    expect(contrastRatio(
      chainMap.get("lvl-4999")?.background ?? "#000000",
      chainMap.get("lvl-4999")?.text ?? "#ffffff",
    )).toBeGreaterThanOrEqual(4.5);

    // Cyclic components terminate cleanly in any input order and respect overrides/AA contrast.
    const cyclicInput: CategoryColorInput[] = [
      { id: "cy-a", parent_id: "cy-c" },
      { id: "cy-b", parent_id: "cy-a" },
      { id: "cy-c", parent_id: "cy-b" },
      { id: "cy-leaf", parent_id: "cy-b" },
      { id: "self-loop", parent_id: "self-loop" },
    ];
    const cyForward = buildCategoryColorMap(cyclicInput);
    const cyReversed = buildCategoryColorMap([...cyclicInput].reverse());
    expect(cyForward.size).toBe(cyclicInput.length);
    for (const item of cyclicInput) {
      const color = cyForward.get(item.id);
      expect(color).toBeDefined();
      expect(cyReversed.get(item.id)).toEqual(color);
      expect(contrastRatio(color?.background ?? "#000000", color?.text ?? "#ffffff")).toBeGreaterThanOrEqual(4.5);
    }

    // Realistic ~50-category 3-level tree stays order-independent, monotonic by depth, and sibling-distinct.
    const fiftyCategories: CategoryColorInput[] = [];
    for (let r = 0; r < 10; r += 1) {
      const rootId = `grp-${String(r)}`;
      fiftyCategories.push({ id: rootId, parent_id: null });
      const child0 = `${rootId}-c0`;
      const child1 = `${rootId}-c1`;
      fiftyCategories.push(
        { id: child0, parent_id: rootId },
        { id: child1, parent_id: rootId },
        { id: `${child0}-g0`, parent_id: child0 },
        { id: `${child0}-g1`, parent_id: child0 },
      );
    }
    expect(fiftyCategories).toHaveLength(50);
    const fiftyMap = buildCategoryColorMap(fiftyCategories);
    const fiftyRevMap = buildCategoryColorMap([...fiftyCategories].reverse());
    expect(fiftyMap.size).toBe(50);
    for (const item of fiftyCategories) {
      const c = fiftyMap.get(item.id);
      expect(c).toBeDefined();
      expect(fiftyRevMap.get(item.id)).toEqual(c);
      expect(contrastRatio(c?.background ?? "#000000", c?.text ?? "#ffffff")).toBeGreaterThanOrEqual(4.5);
    }
    for (let r = 0; r < 10; r += 1) {
      const rootId = `grp-${String(r)}`;
      const c0 = `${rootId}-c0`;
      const c1 = `${rootId}-c1`;
      expect(fiftyMap.get(c0)?.background).not.toBe(fiftyMap.get(c1)?.background);
      expect(fiftyMap.get(`${c0}-g0`)?.background).not.toBe(fiftyMap.get(`${c0}-g1`)?.background);
      expect(relativeLuminance(fiftyMap.get(c0)?.background ?? "#000000"))
        .toBeGreaterThan(relativeLuminance(fiftyMap.get(rootId)?.background ?? "#ffffff"));
      expect(relativeLuminance(fiftyMap.get(`${c0}-g0`)?.background ?? "#000000"))
        .toBeGreaterThan(relativeLuminance(fiftyMap.get(c0)?.background ?? "#ffffff"));
    }
  });
});

describe("CategorySwatch and CategoryBadge render", () => {
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

  it("renders a legend swatch with category colour and hatched pattern for Other/one-off", async () => {
    const colors = buildCategoryColors(sampleTree);
    const food = colors.get("food");
    const oneOffFood = oneOffCategoryColor(colors, "food");

    await act(async () => {
      root.render(
        <ul className="budget-chart-legend">
          <li>
            <CategorySwatch color={food} className="budget-chart-swatch" />
            <span>Food</span>
          </li>
          <li>
            <CategorySwatch color={OTHER_CATEGORY_COLOR} className="budget-chart-swatch" />
            <span>Other</span>
          </li>
          <li>
            <CategorySwatch color={oneOffFood} className="budget-chart-swatch" />
            <span>One-off food</span>
          </li>
        </ul>,
      );
      await Promise.resolve();
    });

    const swatches = [...host.querySelectorAll<HTMLElement>(".budget-chart-swatch")];
    expect(swatches).toHaveLength(3);
    expect(swatches[0]?.getAttribute("aria-hidden")).toBe("true");
    expect(cssColorToHex(swatches[0]?.style.backgroundColor ?? "")).toBe(food.background);
    expect(swatches[0]?.getAttribute("data-hatched")).toBeNull();

    expect(cssColorToHex(swatches[1]?.style.backgroundColor ?? "")).toBe(OTHER_CATEGORY_COLOR.background);
    expect(swatches[1]?.getAttribute("data-hatched")).toBe("true");
    expect(swatches[1]?.style.backgroundImage).toContain("repeating-linear-gradient");

    expect(cssColorToHex(swatches[2]?.style.backgroundColor ?? "")).toBe(food.background);
    expect(swatches[2]?.getAttribute("data-hatched")).toBe("true");
    expect(swatches[2]?.style.backgroundImage).toContain("repeating-linear-gradient");
  });

  it("renders a category badge with WCAG AA contrast text for light, dark, and neutral categories", async () => {
    const colors = buildCategoryColors(
      [
        { id: "dark-cat", parent_id: null },
        { id: "light-cat", parent_id: null },
      ],
      { overrides: { "dark-cat": "#1b3a6b", "light-cat": "#f5e642" } },
    );

    await act(async () => {
      root.render(
        <div>
          <CategoryBadge color={colors.get("dark-cat")} label="Dark" />
          <CategoryBadge color={colors.get("light-cat")} label="Light" />
          <CategoryBadge color={colors.get(null)} label="Uncategorised" />
          <CategoryBadge color={oneOffCategoryColor(colors, "dark-cat")} label="One-off dark" />
        </div>,
      );
      await Promise.resolve();
    });

    const badges = [...host.querySelectorAll<HTMLElement>(".budget-cat-badge")];
    expect(badges).toHaveLength(4);
    expect(badges.map((el) => el.textContent)).toEqual(["Dark", "Light", "Uncategorised", "One-off dark"]);

    for (const badge of badges) {
      const bgHex = cssColorToHex(badge.style.backgroundColor);
      const fgHex = cssColorToHex(badge.style.color);
      expect(["#000000", "#ffffff"]).toContain(fgHex);
      expect(contrastRatio(bgHex, fgHex)).toBeGreaterThanOrEqual(4.5);
    }

    expect(cssColorToHex(badges[0]?.style.color ?? "")).toBe("#ffffff");
    expect(cssColorToHex(badges[1]?.style.color ?? "")).toBe("#000000");
    expect(cssColorToHex(badges[2]?.style.backgroundColor ?? "")).toBe(NEUTRAL_CATEGORY_COLOR.background);
    expect(badges[3]?.getAttribute("data-hatched")).toBe("true");
    expect(badges[3]?.style.backgroundImage).toContain("repeating-linear-gradient");
  });

  it("renders chart series, legend swatches, Year tree swatches, and transaction badges consistently", async () => {
    const categories: AnalyticsCategory[] = [
      { id: "transport", name: "Transport", archived: false, version: 1, created_at: 1, updated_at: 1, parent_id: null },
      { id: "fuel", name: "Fuel", archived: false, version: 1, created_at: 1, updated_at: 1, parent_id: "transport" },
      { id: "food", name: "Food", archived: false, version: 1, created_at: 1, updated_at: 1, parent_id: null },
    ];
    const expected = buildCategoryColors(categories);
    const budget = monthlyBudgetChartsOf({
      category_spend: [
        monthlyBudgetCategorySpend({ category_id: "fuel", name: "Fuel", net_spending: "200" }),
        monthlyBudgetCategorySpend({ category_id: "food", name: "Food", net_spending: "100" }),
      ],
    }).budget;
    const series = monthlyBudgetSeriesOf({
      months: [
        {
          period: "2026-11", has_activity: true, is_current_month: true, income_estimate_partial: false,
          provisional: false, start_budget: "5000", spent: "300",
          categories: [
            { category_id: "fuel", net_spending: "200" },
            { category_id: "food", net_spending: "100" },
          ],
        },
      ],
      category_labels: [
        { category_id: "fuel", name: "Fuel", archived: false },
        { category_id: "food", name: "Food", archived: false },
      ],
    });

    await act(async () => {
      root.render(
        <MonthBudgetCharts
          budget={budget}
          series={series}
          paceProjection={null}
          locale="en-US"
          categories={categories}
        />,
      );
      await Promise.resolve();
    });

    const chartCard = host.querySelector('[aria-label="Spending by category · PLN"]');
    const legendSwatches = [...chartCard?.querySelectorAll<HTMLElement>(".budget-chart-legend .budget-chart-swatch") ?? []];
    expect(legendSwatches).toHaveLength(2);
    expect(cssColorToHex(legendSwatches[0]?.style.backgroundColor ?? "")).toBe(expected.get("transport").background);
    expect(cssColorToHex(legendSwatches[1]?.style.backgroundColor ?? "")).toBe(expected.get("food").background);
    const chartRects = [...chartCard?.querySelectorAll("svg rect") ?? []].map((rect) => rect.getAttribute("fill"));
    expect(chartRects).toContain(expected.get("transport").background);
    expect(chartRects).toContain(expected.get("food").background);

    // YearView shows tree row swatches matching the exact same palette and lighter descendant shades.
    await act(async () => {
      root.render(
        <YearView
          api={{
            overview: (period) => Promise.resolve({
              ok: true as const,
              data: overviewOf(period, {
                categories: [
                  { currency: "PLN", category_id: "fuel", name: "Fuel", archived: false, expenses: "200", refunds: "0", net_spending: "200" },
                  { currency: "PLN", category_id: null, name: null, archived: false, expenses: "25", refunds: "0", net_spending: "25" },
                ],
              }),
            }),
            categories: () => Promise.resolve({ ok: true as const, data: { categories } }),
            analyticsTransactions: vi.fn(() => Promise.resolve({ ok: true as const, data: entriesOf("2026-11", []) })),
          }}
          accounts={[account({ id: "acc-1" })]}
          locale="en"
          revision={0}
          onAccessLoss={() => false}
          onOpenMonth={() => undefined}
          today={() => new Date(2026, 10, 21, 12)}
        />,
      );
      await Promise.resolve();
    });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });

    const yearSwatches = [...host.querySelectorAll<HTMLElement>(".budget-categories .budget-cat-swatch")];
    expect(yearSwatches.map((el) => cssColorToHex(el.style.backgroundColor))).toEqual([
      expected.get("transport").background,
      expected.get("fuel").background,
      NEUTRAL_CATEGORY_COLOR.background,
    ]);

    // TransactionsPanel renders WCAG AA badges for categorised and uncategorised rows.
    const txRows = [
      entry("t1", { effective: { category_id: "fuel", kind: "expense", source: "rule", rule_id: "r1" } }),
      entry("t2", { effective: { category_id: null, kind: "expense", source: "default", rule_id: null } }),
    ];
    await act(async () => {
      root.render(
        <TransactionsPanel
          api={{
            analyticsTransactions: vi.fn(() => Promise.resolve({ ok: true as const, data: entriesOf("2026-11", txRows) })),
            analyticsTransactionsByKeys: vi.fn(() => Promise.resolve({ ok: true as const, data: entriesOf("", txRows) })),
            categories: vi.fn(() => Promise.resolve({ ok: true as const, data: { categories } })),
            annotate: vi.fn(() => Promise.resolve({ ok: true as const, data: { annotation: annotationOf() } })),
            decideTransfer: vi.fn(() => Promise.resolve({ ok: true as const, data: { decision: decisionOf() } })),
          }}
          period="2026-11"
          currencies={["PLN"]}
          accounts={[account({ id: "acc-1" })]}
          locale="en"
          revision={0}
          onAccessLoss={() => false}
          onChanged={() => undefined}
        />,
      );
      await Promise.resolve();
    });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });

    const txBadges = [...host.querySelectorAll<HTMLElement>(".budget-transaction-table tbody .budget-cat-badge")];
    expect(txBadges.map((el) => el.textContent)).toEqual(["Fuel", "Uncategorised"]);
    expect(cssColorToHex(txBadges[0]?.style.backgroundColor ?? "")).toBe(expected.get("fuel").background);
    expect(cssColorToHex(txBadges[1]?.style.backgroundColor ?? "")).toBe(NEUTRAL_CATEGORY_COLOR.background);
    for (const badge of txBadges) {
      const bgHex = cssColorToHex(badge.style.backgroundColor);
      const fgHex = cssColorToHex(badge.style.color);
      expect(["#000000", "#ffffff"]).toContain(fgHex);
      expect(contrastRatio(bgHex, fgHex)).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("renders tree swatches across Budżet miesiąca 3-level rows and hatched Other legend swatch", async () => {
    const categories: AnalyticsCategory[] = [
      { id: "transport", name: "Transport", archived: false, version: 1, created_at: 1, updated_at: 1, parent_id: null },
      { id: "car", name: "Car", archived: false, version: 1, created_at: 1, updated_at: 1, parent_id: "transport" },
      { id: "fuel", name: "Fuel", archived: false, version: 1, created_at: 1, updated_at: 1, parent_id: "car" },
      { id: "food", name: "Food", archived: false, version: 1, created_at: 1, updated_at: 1, parent_id: null },
      { id: "cat-4", name: "Health", archived: false, version: 1, created_at: 1, updated_at: 1, parent_id: null },
      { id: "cat-5", name: "Home", archived: false, version: 1, created_at: 1, updated_at: 1, parent_id: null },
      { id: "cat-6", name: "Leisure", archived: false, version: 1, created_at: 1, updated_at: 1, parent_id: null },
      { id: "cat-7", name: "Misc", archived: false, version: 1, created_at: 1, updated_at: 1, parent_id: null },
      { id: "cat-8", name: "Gifts", archived: false, version: 1, created_at: 1, updated_at: 1, parent_id: null },
      { id: "cat-9", name: "Pets", archived: false, version: 1, created_at: 1, updated_at: 1, parent_id: null },
      { id: "cat-10", name: "Travel", archived: false, version: 1, created_at: 1, updated_at: 1, parent_id: null },
    ];
    const expected = buildCategoryColors(categories);
    const charts = monthlyBudgetChartsOf(
      {
        category_spend: [
          monthlyBudgetCategorySpend({ category_id: "fuel", name: "Fuel", expenses: "100", net_spending: "100" }),
          monthlyBudgetCategorySpend({ category_id: null, name: null, expenses: "20", net_spending: "20" }),
        ],
      },
      {
        months: [
          {
            period: "2026-11", has_activity: true, is_current_month: true, income_estimate_partial: false,
            provisional: false, start_budget: "5000", spent: "300",
            categories: [
              { category_id: "fuel", net_spending: "100" },
              { category_id: "food", net_spending: "80" },
              { category_id: "cat-4", net_spending: "50" },
              { category_id: "cat-5", net_spending: "30" },
              { category_id: "cat-6", net_spending: "20" },
              { category_id: "cat-7", net_spending: "10" },
              { category_id: "cat-8", net_spending: "8" },
              { category_id: "cat-9", net_spending: "6" },
              { category_id: "cat-10", net_spending: "4" },
            ],
          },
        ],
        category_labels: categories.map((item) => ({ category_id: item.id, name: item.name, archived: false })),
      },
    );

    await loadLocale("pl");
    await act(async () => {
      root.render(
        <MonthBudgetView
          api={{
            monthlyBudgetCharts: vi.fn(() => Promise.resolve({ ok: true as const, data: charts })),
            categories: vi.fn(() => Promise.resolve({ ok: true as const, data: { categories } })),
            forecast: vi.fn(() => Promise.resolve({ ok: true as const, data: forecastOf() })),
            mortgages: vi.fn(() => Promise.resolve({ ok: true as const, data: mortgagesOf() })),
            recurring: vi.fn(() => Promise.resolve({ ok: true as const, data: recurringOf() })),
          }}
          accounts={[account({ id: "acc-1" })]}
          locale="pl-PL"
          revision={0}
          onAccessLoss={() => false}
          onOpenMonth={() => undefined}
        />,
      );
      await Promise.resolve();
    });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });

    const treeSwatches = [...host.querySelectorAll<HTMLElement>('section[aria-label="Wydatki i limity"] tbody .budget-cat-swatch')];
    expect(treeSwatches.map((el) => cssColorToHex(el.style.backgroundColor))).toEqual([
      expected.get("transport").background,
      expected.get("car").background,
      expected.get("fuel").background,
      NEUTRAL_CATEGORY_COLOR.background,
    ]);
    expect(relativeLuminance(cssColorToHex(treeSwatches[1]?.style.backgroundColor ?? "")))
      .toBeGreaterThan(relativeLuminance(cssColorToHex(treeSwatches[0]?.style.backgroundColor ?? "")));
    expect(relativeLuminance(cssColorToHex(treeSwatches[2]?.style.backgroundColor ?? "")))
      .toBeGreaterThan(relativeLuminance(cssColorToHex(treeSwatches[1]?.style.backgroundColor ?? "")));

    const otherLegendSwatch = host.querySelector<HTMLElement>('.budget-chart-legend .budget-chart-swatch[data-hatched="true"]');
    expect(otherLegendSwatch).not.toBeNull();
    expect(cssColorToHex(otherLegendSwatch?.style.backgroundColor ?? "")).toBe(OTHER_CATEGORY_COLOR.background);
  });

  it("renders several charts on the page with globally unique pattern IDs", async () => {
    const categories: AnalyticsCategory[] = Array.from({ length: 7 }, (_, idx) => ({
      id: `pcat-${String(idx)}`,
      name: `Category ${String(idx)}`,
      archived: false,
      version: 1,
      created_at: 1,
      updated_at: 1,
      parent_id: null,
    }));
    const makeProps = (currency: string) => ({
      budget: monthlyBudgetOf({
        currency,
        expected_income: monthlyBudgetExpectedIncome({ status: "estimated", total: "1000" }),
        pace: monthlyBudgetPace({ basis: "received_plus_expected", budget: "6000" }),
      }),
      series: monthlyBudgetSeriesOf({
        months: [
          {
            period: "2026-11", has_activity: true, is_current_month: true, income_estimate_partial: false,
            provisional: false, start_budget: "5000", spent: "300",
            categories: categories.map((cat, idx) => ({ category_id: cat.id, net_spending: String(100 - idx * 10) })),
          },
        ],
        category_labels: categories.map((cat) => ({ category_id: cat.id, name: cat.name, archived: false })),
      }),
    });
    const pln = makeProps("PLN");
    const eur = makeProps("EUR");

    await loadLocale("en");
    await act(async () => {
      root.render(
        <div>
          <MonthBudgetCharts
            budget={pln.budget}
            series={pln.series}
            paceProjection={null}
            locale="en-US"
            categories={categories}
          />
          <MonthBudgetCharts
            budget={eur.budget}
            series={eur.series}
            paceProjection={null}
            locale="en-US"
            categories={categories}
          />
        </div>,
      );
      await Promise.resolve();
    });

    const svgs = [...host.querySelectorAll("svg")];
    expect(svgs.length).toBe(6);

    const patterns = [...host.querySelectorAll("svg pattern")];
    const patternIds = patterns.map((el) => el.getAttribute("id") ?? "");
    expect(patternIds.length).toBe(6);
    expect(patternIds.every((value) => value !== "")).toBe(true);
    expect(new Set(patternIds).size).toBe(patternIds.length);

    for (const cat of categories) {
      for (const value of patternIds) {
        expect(value).not.toContain(cat.id);
      }
    }

    // Every url(#...) fill inside each SVG resolves to a <pattern> defined in that same SVG.
    let urlFillCount = 0;
    for (const svg of svgs) {
      const localIds = new Set([...svg.querySelectorAll("pattern")].map((el) => el.getAttribute("id") ?? ""));
      for (const el of svg.querySelectorAll("[fill]")) {
        const fill = el.getAttribute("fill") ?? "";
        const match = /^url\(#(.+)\)$/.exec(fill);
        if (match !== null) {
          urlFillCount += 1;
          expect(localIds.has(match[1] ?? "")).toBe(true);
        }
      }
    }
    expect(urlFillCount).toBeGreaterThanOrEqual(4);
  });
});
