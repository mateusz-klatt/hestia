// Pure unit tests for category colour generation and render tests for legend swatches & badges.
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { account } from "../fixtures";
import { CategoryBadge, CategorySwatch } from "./category_badge";
import {
  CATEGORY_PALETTE,
  NEUTRAL_CATEGORY_COLOR,
  OTHER_CATEGORY_COLOR,
  accessibleBadgeText,
  buildCategoryColorMap,
  buildCategoryColors,
  contrastRatio,
  hatchedBadgeGradient,
  hexToOklch,
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
  monthlyBudgetSeriesOf,
  mortgagesOf,
  overviewOf,
  recurringOf,
} from "./fixtures";
import type { AnalyticsCategory } from "./model";
import { MonthBudgetView, monthBudgetLabels } from "./month_budget";
import { MonthBudgetCharts } from "./month_budget_charts";
import { TransactionsPanel, transactionLabels } from "./transactions";
import { YearView, yearLabels } from "./year";

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

    // Distinct roots stay distinct up to palette size.
    const rootColors = ["transport", "food", "housing"].map((id) => forward.get(id)?.background);
    expect(new Set(rootColors).size).toBe(3);

    // Even 12 distinct roots get 12 distinct palette slots without collisions.
    const twelveRoots: CategoryColorInput[] = Array.from({ length: 12 }, (_, idx) => ({
      id: `root-${String(idx)}`,
      parent_id: null,
    }));
    const twelveMap = buildCategoryColorMap(twelveRoots);
    expect(new Set(twelveRoots.map((r) => twelveMap.get(r.id)?.background)).size).toBe(12);
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

  it("supports the optional per-root colour override hook and shades descendants from it", () => {
    const colors = buildCategoryColors(
      [
        { id: "transport", parent_id: null },
        { id: "fuel", parent_id: "transport" },
        { id: "light-root", parent_id: null },
        { id: "light-child", parent_id: "light-root" },
        { id: "bad-override", parent_id: null },
      ],
      {
        overrides: {
          transport: "#1e88e5",
          "light-root": "#eef480",
          "bad-override": "not-a-color",
        },
      },
    );
    expect(colors.get("transport").background).toBe("#1e88e5");
    expect(hueDistance(colors.get("fuel").oklch.h, hexToOklch("#1e88e5")?.h ?? 0)).toBeLessThanOrEqual(15);
    expect(relativeLuminance(colors.get("fuel").background)).toBeGreaterThan(relativeLuminance("#1e88e5"));

    // Very light root override darkens monotonically instead of clipping into white.
    expect(colors.get("light-root").background).toBe("#eef480");
    expect(colors.get("light-root").text).toBe("#000000");
    expect(relativeLuminance(colors.get("light-child").background)).toBeLessThan(relativeLuminance("#eef480"));

    // Invalid override falls back safely.
    expect(parseHexColor(colors.get("bad-override").background)).not.toBeNull();
    expect(resolveCategoryColor(colors.entries, "unlisted").background).toBeTruthy();
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
          text={monthBudgetLabels.en}
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
          }}
          accounts={[account({ id: "acc-1" })]}
          text={yearLabels.en}
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
          text={transactionLabels.en}
          locale="en"
          revision={0}
          onAccessLoss={() => false}
          onChanged={() => undefined}
        />,
      );
      await Promise.resolve();
    });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });

    const txBadges = [...host.querySelectorAll<HTMLElement>(".budget-entry .budget-cat-badge")];
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
            ],
          },
        ],
        category_labels: categories.map((item) => ({ category_id: item.id, name: item.name, archived: false })),
      },
    );

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
          text={monthBudgetLabels.pl}
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
});
