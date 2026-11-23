// Read-only monthly budget charts (P2, OpenAPI 0.29.0): cumulative daily spend, stacked category spending,
// and income vs. spending. Uses React inline SVG with bounded viewBox coordinates derived from BigInt scaling
// plus keyboard-operable exact HTML data tables.
import { useId } from "react";
import { formatAmount } from "../model";
import { CategorySwatch } from "./category_badge";
import { OTHER_CATEGORY_COLOR, buildCategoryColors } from "./category_colors";
import type { CategoryColorInput, CategoryColorOverrides } from "./category_colors";
import { categoryParentMap, hasCategoryParents, rollupNetAmounts } from "./category_tree";
import type { CategoryLink } from "./category_tree";
import { formatRatioHalfEven, formatScaledBigInt, isZero, parseScaledInt } from "./model";
import type { AnalyticsMonthlyBudget, AnalyticsMonthlyBudgetCategoryLabel, AnalyticsMonthlyBudgetDailyPoint,
  AnalyticsMonthlyBudgetSeries, AnalyticsMonthlyBudgetSeriesMonth } from "./model";
import type { MonthPaceProjection, ScheduledPaceItem } from "./month_budget_pace";

export interface MonthBudgetChartLabels {
  chartsSectionTitle: string;
  showData: string;
  chartDailyTitle: string;
  chartCategoriesTitle: string;
  chartIncomeSpendTitle: string;
  otherCategory: string;
  dateLabel: string;
  monthColumn: string;
  cumulativeSpentLabel: string;
  bookedBudgetLine: string;
  expectedBudgetLine: string;
  paceExtensionLine: string;
  scheduledStepsColumn: string;
  overdue: string;
  upcoming: string;
  scheduledPaymentsLoading: string;
  withoutScheduledPayments: string;
  exhaustionMarker: (day: number) => string;
  incompleteMonth: string;
  currentMonth: string;
  expectedIncomeLegend: string;
  statusColumn: string;
  totalLabel: string;
  noValue: string;
  budgetReceived: string;
  netSpending: string;
  provisional: string;
  partialIncome: string;
  noStoredData: string;
  uncategorised: string;
  unknownCategory: string;
  archived: string;
  category: string;
  chartDailyDesc: (currency: string, start: string, cutoff: string) => string;
  chartCategoriesDesc: (count: number, currency: string) => string;
  chartIncomeSpendDesc: (count: number, currency: string) => string;
}

const CHART_WIDTH = 360;
const CHART_HEIGHT = 210;
const PLOT_LEFT = 64;
const PLOT_RIGHT = 346;
const PLOT_TOP = 16;
const PLOT_BOTTOM = 172;

function signedBigInt(value: string): { coeff: bigint; scale: number } {
  const parsed = parseScaledInt(value);
  if (parsed === null) return { coeff: 0n, scale: 0 };
  return { coeff: parsed.negative ? -parsed.coeff : parsed.coeff, scale: parsed.scale };
}

export function alignDecimals(values: readonly string[]): { scale: number; coeffs: bigint[] } {
  const parsed = values.map(signedBigInt);
  const scale = parsed.reduce((max, item) => (item.scale > max ? item.scale : max), 0);
  const coeffs = parsed.map((item) => item.coeff * (10n ** BigInt(scale - item.scale)));
  return { scale, coeffs };
}

export interface YScale {
  min: bigint;
  max: bigint;
  zeroY: number;
  toY: (value: bigint) => number;
}

export function buildYScale(
  values: readonly bigint[],
  plotTop: number = PLOT_TOP,
  plotBottom: number = PLOT_BOTTOM,
): YScale {
  const min = values.reduce((low, value) => (value < low ? value : low), 0n);
  const max = values.reduce((high, value) => (value > high ? value : high), 0n);
  const span = max - min;
  const height = plotBottom - plotTop;
  const toY = (value: bigint): number => {
    if (span === 0n) return Number(((plotTop + plotBottom) / 2).toFixed(2));
    const clamped = value < min ? min : value > max ? max : value;
    const offset = ((clamped - min) * BigInt(Math.round(height * 100))) / span;
    return Number((plotBottom - Number(offset) / 100).toFixed(2));
  };
  return { min, max, zeroY: toY(0n), toY };
}

function tickSeparators(locale: string): { group: string; decimal: string } {
  const parts = new Intl.NumberFormat(locale).formatToParts(11111.1);
  return {
    group: parts.find((part) => part.type === "group")?.value ?? ",",
    decimal: parts.find((part) => part.type === "decimal")?.value ?? ".",
  };
}

export function formatTickDecimal(decimalStr: string, locale: string): string {
  const negative = decimalStr.startsWith("-");
  const body = negative ? decimalStr.slice(1) : decimalStr;
  const dot = body.indexOf(".");
  const whole = dot === -1 ? body : body.slice(0, dot);
  const fraction = dot === -1 ? "" : body.slice(dot + 1);
  if (whole.length > 9) {
    const exp = whole.length - 1;
    const lead = whole.slice(0, 1);
    const next = whole.slice(1, 2);
    return `${negative ? "−" : ""}${lead}.${next}e${String(exp)}`;
  }
  const { group, decimal } = tickSeparators(locale);
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, group);
  const trimmedFrac = fraction.slice(0, 2).replace(/0+$/, "");
  const sign = negative && (whole !== "0" || trimmedFrac !== "") ? "−" : "";
  return trimmedFrac === "" ? `${sign}${grouped}` : `${sign}${grouped}${decimal}${trimmedFrac}`;
}

function shortMonthTick(period: string, locale: string, compact: boolean): string {
  const parts = period.split("-");
  const year = Number(parts[0]);
  const month = Number(parts[1]);
  if (!Number.isInteger(year) || !Number.isInteger(month)) return period;
  if (compact) return String(month).padStart(2, "0");
  return new Intl.DateTimeFormat(locale, { month: "short", timeZone: "UTC" }).format(new Date(Date.UTC(year, month - 1, 15)));
}

function fullMonthLabel(period: string, locale: string): string {
  const parts = period.split("-");
  const year = Number(parts[0]);
  const month = Number(parts[1]);
  if (!Number.isInteger(year) || !Number.isInteger(month)) return period;
  return new Intl.DateTimeFormat(locale, { month: "long", year: "numeric", timeZone: "UTC" }).format(new Date(Date.UTC(year, month - 1, 15)));
}

function formatAxisLabel(value: bigint, scale: number, denom: bigint, locale: string): string {
  if (value === 0n) return "0";
  const rounded = value / denom;
  return formatTickDecimal(formatScaledBigInt(rounded, scale), locale);
}

function YAxisGrid({ scaleObj, scale, denom, locale }: {
  scaleObj: YScale;
  scale: number;
  denom: bigint;
  locale: string;
}) {
  const zeroLabel = "0";
  const showMax = scaleObj.max > 0n;
  const showMin = scaleObj.min < 0n;
  const maxY = scaleObj.toY(scaleObj.max);
  const minY = scaleObj.toY(scaleObj.min);
  const zeroNearTop = showMax && Math.abs(scaleObj.zeroY - maxY) < 12;
  const zeroNearBottom = showMin && Math.abs(scaleObj.zeroY - minY) < 12;

  return (
    <g aria-hidden="true">
      <line x1={PLOT_LEFT} y1={PLOT_TOP} x2={PLOT_LEFT} y2={PLOT_BOTTOM} stroke="var(--border-strong)" strokeWidth="1" />
      <line x1={PLOT_LEFT} y1={scaleObj.zeroY} x2={PLOT_RIGHT} y2={scaleObj.zeroY} stroke="var(--border-strong)" strokeWidth="1.2" />
      {showMax ? (
        <>
          <line x1={PLOT_LEFT} y1={maxY} x2={PLOT_RIGHT} y2={maxY} stroke="var(--border)" strokeWidth="1" />
          <text x={PLOT_LEFT - 5} y={maxY + 3} textAnchor="end" fontSize="9.5" fill="var(--fg-soft)">
            {formatAxisLabel(scaleObj.max, scale, denom, locale)}
          </text>
        </>
      ) : null}
      {showMin ? (
        <>
          <line x1={PLOT_LEFT} y1={minY} x2={PLOT_RIGHT} y2={minY} stroke="var(--border)" strokeWidth="1" />
          <text x={PLOT_LEFT - 5} y={minY + 3} textAnchor="end" fontSize="9.5" fill="var(--fg-soft)">
            {formatAxisLabel(scaleObj.min, scale, denom, locale)}
          </text>
        </>
      ) : null}
      {!zeroNearTop && !zeroNearBottom ? (
        <text x={PLOT_LEFT - 5} y={scaleObj.zeroY + 3} textAnchor="end" fontSize="9.5" fill="var(--fg-soft)">
          {zeroLabel}
        </text>
      ) : null}
    </g>
  );
}

// ---------------------------------------------------------------------------
// Chart 1: Cumulative daily spend
// ---------------------------------------------------------------------------

function formatShortDate(isoDate: string, locale: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(isoDate);
  if (match === null) return isoDate;
  const parsed = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
  if (Number.isNaN(parsed.getTime())) return isoDate;
  return new Intl.DateTimeFormat(locale, { day: "numeric", month: "short", timeZone: "UTC" }).format(parsed);
}

function scheduledItemLabel(
  item: ScheduledPaceItem,
  currency: string,
  locale: string,
  text: Pick<MonthBudgetChartLabels, "overdue" | "upcoming">,
): string {
  const date = formatShortDate(item.expectedDate, locale);
  const status = item.overdue ? text.overdue : text.upcoming;
  return `${date} · ${item.label} · ${formatAmount(item.amount, currency, locale)} · ${status}`;
}

function CumulativeDailyChart({ budget, daily, paceProjection, schedulePending, scheduleError, locale, text }: {
  budget: AnalyticsMonthlyBudget;
  daily: readonly AnalyticsMonthlyBudgetDailyPoint[];
  paceProjection: MonthPaceProjection | null;
  schedulePending: boolean;
  scheduleError: boolean;
  locale: string;
  text: MonthBudgetChartLabels;
}) {
  const id = useId();
  const currency = budget.currency;
  const money = (amount: string) => formatAmount(amount, currency, locale);
  const startBudget = budget.summary?.start_budget ?? "0";
  const pace = budget.pace;
  const activePace = budget.period === budget.current_month && pace !== null && pace.status !== "historical" ? pace : null;
  const projection = activePace === null ? null : paceProjection;
  const hasExpectedBudget = pace !== null && pace.basis === "received_plus_expected";
  const expectedBudget = pace?.budget ?? startBudget;
  const projected = projection?.projectedMonthSpend ?? null;
  const exhaustionDay = projection?.exhaustionDay ?? null;
  const cutoffDay = Number(budget.cutoff.slice(8, 10)) || daily.length;
  const daysInMonth = Math.max(daily.length, Number(budget.end.slice(8, 10)) || 30, cutoffDay);
  const lastPoint = daily.at(-1);
  const lastCumulative = lastPoint?.cumulative_spent ?? "0";
  const projectionNumerators = projection?.points.flatMap((point) => [point.before.numerator, point.value.numerator]) ?? [];

  const rawMoney = [
    ...daily.map((point) => point.cumulative_spent),
    startBudget,
    ...(hasExpectedBudget ? [expectedBudget] : []),
    ...projectionNumerators,
  ];
  const { scale, coeffs } = alignDecimals(rawMoney);
  const denom = BigInt(projected?.denominator ?? 1);
  const dailyCoeffs = coeffs.slice(0, daily.length).map((coeff) => coeff * denom);
  const bookedCoeff = (coeffs[daily.length] ?? 0n) * denom;
  const expectedIndex = daily.length + 1;
  const expectedCoeff = hasExpectedBudget ? (coeffs[expectedIndex] ?? 0n) * denom : bookedCoeff;
  const projectionIndex = daily.length + 1 + (hasExpectedBudget ? 1 : 0);
  const projectionCoeffs = projectionNumerators.map((_, index) => coeffs[projectionIndex + index] ?? 0n);

  const yScale = buildYScale([
    ...dailyCoeffs,
    bookedCoeff,
    ...(hasExpectedBudget ? [expectedCoeff] : []),
    ...projectionCoeffs,
  ]);

  const toX = (day: number): number => {
    if (daysInMonth <= 1) return Number(((PLOT_LEFT + PLOT_RIGHT) / 2).toFixed(2));
    return Number((PLOT_LEFT + ((day - 1) / (daysInMonth - 1)) * (PLOT_RIGHT - PLOT_LEFT)).toFixed(2));
  };

  const polylinePoints = dailyCoeffs
    .map((coeff, index) => `${String(toX(index + 1))},${String(yScale.toY(coeff))}`)
    .join(" ");
  const cutoffX = toX(cutoffDay);
  const cutoffY = yScale.toY(dailyCoeffs.at(-1) ?? 0n);
  const bookedY = yScale.toY(bookedCoeff);
  const expectedY = yScale.toY(expectedCoeff);
  const showPaceExtension = projection !== null && projection.points.length > 0;
  const pacePoints = projection?.points.flatMap((point, index) => {
    const x = toX(point.day);
    const before = projectionCoeffs[index * 2] ?? 0n;
    const value = projectionCoeffs[index * 2 + 1] ?? 0n;
    const points = [`${String(x)},${String(yScale.toY(before))}`];
    if (before !== value) points.push(`${String(x)},${String(yScale.toY(value))}`);
    return points;
  }) ?? [];
  const startCoord = `${String(cutoffX)},${String(cutoffY)}`;
  const pacePolylineCoords = [startCoord, ...pacePoints];
  const pacePolylinePoints = pacePolylineCoords.join(" ");
  const hasPaceLine = showPaceExtension && pacePolylineCoords.some((coord) => coord !== startCoord);
  const showProjectedSummary = showPaceExtension && (cutoffDay < daysInMonth || projection.scheduledItems.length > 0);
  const exhaustionX = exhaustionDay !== null ? toX(exhaustionDay) : null;
  const exhaustionY = exhaustionDay !== null
    ? (projection?.status === "already_reached" ? cutoffY : expectedY)
    : null;
  const dailyByDate = new Map(daily.map((point) => [point.date, point.cumulative_spent]));
  const projectionByDate = new Map(projection?.points.map((point) => [point.date, point]) ?? []);
  const allDates = [...new Set([...dailyByDate.keys(), ...projectionByDate.keys()])].sort((a, b) => a.localeCompare(b));
  const showProjectedColumn = showProjectedSummary && projected !== null;
  const showStepsColumn = (projection?.scheduledItems.length ?? 0) > 0;

  const ticks = [...new Set([1, 10, 20, daysInMonth].filter((day) => day <= daysInMonth && (day === daysInMonth || daysInMonth - day >= 3)))];

  return (
    <article className="budget-total budget-month-chart" aria-label={`${text.chartDailyTitle} · ${currency}`}>
      <h4>{text.chartDailyTitle} · {currency}</h4>
      <svg role="img" aria-labelledby={`${id}-title ${id}-desc`} viewBox={`0 0 ${String(CHART_WIDTH)} ${String(CHART_HEIGHT)}`}>
        <title id={`${id}-title`}>{`${text.chartDailyTitle} (${currency})`}</title>
        <desc id={`${id}-desc`}>{text.chartDailyDesc(currency, budget.start, budget.cutoff)}</desc>
        <YAxisGrid scaleObj={yScale} scale={scale} denom={denom} locale={locale} />
        <line className="budget-chart-booked-line" x1={PLOT_LEFT} y1={bookedY} x2={PLOT_RIGHT} y2={bookedY}
          stroke="var(--chart-budget)" strokeWidth="1.6" strokeDasharray="6 3" />
        {hasExpectedBudget ? (
          <line className="budget-chart-expected-line" x1={PLOT_LEFT} y1={expectedY} x2={PLOT_RIGHT} y2={expectedY}
            stroke="var(--chart-expected)" strokeWidth="1.6" strokeDasharray="2 3" />
        ) : null}
        {daily.length > 1 ? (
          <polyline className="budget-chart-actual-line" fill="none" stroke="var(--chart-spend)" strokeWidth="2.2"
            strokeLinejoin="round" strokeLinecap="round" points={polylinePoints} />
        ) : null}
        <circle cx={cutoffX} cy={cutoffY} r="3" fill="var(--chart-spend)" />
        {hasPaceLine ? (
          <polyline className="budget-chart-pace-line" fill="none" stroke="var(--chart-spend)" strokeWidth="1.8"
            strokeDasharray="2 4" strokeLinejoin="round" strokeLinecap="round" points={pacePolylinePoints} />
        ) : null}
        {projection?.points.map((point, index) => {
          if (point.items.length === 0) return null;
          const isOverdue = point.items.some((item) => item.overdue);
          const sx = toX(point.day);
          const sy = yScale.toY(projectionCoeffs[index * 2 + 1] ?? 0n);
          return (
            <g key={point.date} className="budget-chart-scheduled-step" data-overdue={isOverdue}>
              <title>{point.items.map((item) => scheduledItemLabel(item, currency, locale, text)).join("\n")}</title>
              <circle cx={sx} cy={sy} r={isOverdue ? "5" : "4"} fill="var(--surface)"
                stroke={isOverdue ? "var(--err)" : "var(--chart-expected)"}
                strokeDasharray={isOverdue ? "2 1.5" : undefined} strokeWidth="2" />
              {isOverdue ? (
                <text x={sx} y={sy + 3} textAnchor="middle" fontSize="8" fontWeight="700"
                  fill="var(--err)" aria-hidden="true">!</text>
              ) : null}
            </g>
          );
        })}
        {exhaustionDay !== null && exhaustionX !== null && exhaustionY !== null ? (
          <g className="budget-chart-exhaustion">
            <line x1={exhaustionX} y1={PLOT_TOP} x2={exhaustionX} y2={PLOT_BOTTOM} stroke="var(--err)" strokeWidth="1.2" strokeDasharray="3 2" />
            <circle cx={exhaustionX} cy={exhaustionY} r="3.5" fill="var(--surface)" stroke="var(--err)" strokeWidth="2" />
            <text x={Math.min(PLOT_RIGHT - 4, exhaustionX + 4)} y={PLOT_TOP + 10}
              textAnchor={exhaustionX > PLOT_RIGHT - 44 ? "end" : "start"} fontSize="9.5" fontWeight="600" fill="var(--err)">
              {String(exhaustionDay)}
            </text>
          </g>
        ) : null}
        <g aria-hidden="true">
          {ticks.map((day) => (
            <text key={day} x={toX(day)} y={PLOT_BOTTOM + 16} textAnchor="middle" fontSize="10" fill="var(--fg-soft)">
              {String(day)}
            </text>
          ))}
        </g>
      </svg>
      <ul className="budget-chart-legend">
        <li>
          <span className="budget-chart-line-swatch" style={{ color: "var(--chart-spend)" }} aria-hidden="true" />
          <span>{text.cumulativeSpentLabel}: <strong>{money(lastCumulative)}</strong></span>
        </li>
        <li>
          <span className="budget-chart-line-swatch" style={{ color: "var(--chart-budget)", borderTopStyle: "dashed" }} aria-hidden="true" />
          <span>{text.bookedBudgetLine}: <strong>{money(startBudget)}</strong></span>
        </li>
        {hasExpectedBudget ? (
          <li>
            <span className="budget-chart-line-swatch" style={{ color: "var(--chart-expected)", borderTopStyle: "dotted" }} aria-hidden="true" />
            <span>{text.expectedBudgetLine}: <strong>{money(expectedBudget)}</strong></span>
          </li>
        ) : null}
        {showProjectedSummary && projected !== null ? (
          <li>
            <span className="budget-chart-line-swatch" style={{ color: "var(--chart-spend)", borderTopStyle: "dotted" }} aria-hidden="true" />
            <span>
              {text.paceExtensionLine}: <strong>{formatRatioHalfEven(projected, currency, locale)}</strong>
              {scheduleError ? ` (${text.withoutScheduledPayments})` : ""}
            </span>
          </li>
        ) : null}
        {schedulePending ? (
          <li role="status">
            <span>{text.scheduledPaymentsLoading}</span>
          </li>
        ) : null}
        {scheduleError && (!showProjectedSummary || projected === null) ? (
          <li className="budget-warn">
            <span>{text.withoutScheduledPayments}</span>
          </li>
        ) : null}
        {projection?.scheduledItems.map((item, index) => (
          <li key={`${item.expectedDate}-${String(index)}`} className={item.overdue ? "budget-warn" : undefined}>
            <span className="budget-chart-step-swatch" data-overdue={item.overdue}
              style={{ color: item.overdue ? "var(--err)" : "var(--chart-expected)" }} aria-hidden="true">
              {item.overdue ? "!" : null}
            </span>
            <span>{scheduledItemLabel(item, currency, locale, text)}</span>
          </li>
        ))}
        {exhaustionDay !== null ? (
          <li className="budget-warn">
            <span>{text.exhaustionMarker(exhaustionDay)}</span>
          </li>
        ) : null}
      </ul>
      <details className="budget-recurring-evidence">
        <summary>{text.showData}</summary>
        <div className="budget-table-wrap" role="region" aria-label={`${text.chartDailyTitle} · ${currency}`} tabIndex={0}>
          <table className="budget-month-dashboard-table">
            <caption>{text.chartDailyTitle} · {currency}</caption>
            <thead>
              <tr>
                <th scope="col">{text.dateLabel}</th>
                <th scope="col">{text.cumulativeSpentLabel}</th>
                {showProjectedColumn ? <th scope="col">{text.paceExtensionLine}</th> : null}
                {showStepsColumn ? <th scope="col">{text.scheduledStepsColumn}</th> : null}
              </tr>
            </thead>
            <tbody>
              {allDates.map((date) => {
                const actual = dailyByDate.get(date);
                const projPoint = projectionByDate.get(date);
                return (
                  <tr key={date}>
                    <th scope="row">{date}</th>
                    <td>{actual !== undefined ? money(actual) : text.noValue}</td>
                    {showProjectedColumn ? (
                      <td>{projPoint !== undefined ? formatRatioHalfEven(projPoint.value, currency, locale) : text.noValue}</td>
                    ) : null}
                    {showStepsColumn ? (
                      <td>
                        {projPoint !== undefined && projPoint.items.length > 0 ? (
                          <div className="budget-month-steps-cell">
                            {projPoint.items.map((item, index) => (
                              <div key={`${item.expectedDate}-${String(index)}`} className={item.overdue ? "budget-warn" : undefined}>
                                {scheduledItemLabel(item, currency, locale, text)}
                              </div>
                            ))}
                          </div>
                        ) : text.noValue}
                      </td>
                    ) : null}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </details>
    </article>
  );
}

// ---------------------------------------------------------------------------
// Chart 2: Category spending by month (stacked around zero, top 5 + Other)
// ---------------------------------------------------------------------------

export interface RankedCategory {
  id: string | null;
  name: string;
  archived: boolean;
  absTotal: bigint;
  color: string;
  hatched: boolean;
}

export interface CategorySlice {
  key: string;
  label: string;
  color: string;
  hatched: boolean;
  value: bigint;
  y0: bigint;
  y1: bigint;
}

export interface MonthCategoryStack {
  month: AnalyticsMonthlyBudgetSeriesMonth;
  slices: CategorySlice[];
  posTotal: bigint;
  negTotal: bigint;
}

export interface CategoryChartModel {
  scale: number;
  displayed: RankedCategory[];
  hasOtherPositive: boolean;
  hasOtherNegative: boolean;
  allCategories: RankedCategory[];
  byMonthAndCategory: Map<string, Map<string | null, string>>;
  stacks: MonthCategoryStack[];
}

function topLevelMonths(
  months: readonly AnalyticsMonthlyBudgetSeriesMonth[],
  labels: readonly AnalyticsMonthlyBudgetCategoryLabel[],
  categories: readonly CategoryLink[] | undefined,
): { months: readonly AnalyticsMonthlyBudgetSeriesMonth[]; labels: readonly AnalyticsMonthlyBudgetCategoryLabel[] } {
  if (categories === undefined || !hasCategoryParents(categories)) return { months, labels };
  const parents = categoryParentMap(categories);
  const merged = new Map(labels.map((item) => [item.category_id, item]));
  for (const item of categories) {
    if (!merged.has(item.id)) merged.set(item.id, { category_id: item.id, name: item.name, archived: item.archived });
  }
  return {
    labels: [...merged.values()],
    months: months.map((month) => ({ ...month, categories: rollupNetAmounts(month.categories, parents, null) })),
  };
}

export function buildCategoryChartModel(
  months: readonly AnalyticsMonthlyBudgetSeriesMonth[],
  categoryLabels: readonly AnalyticsMonthlyBudgetCategoryLabel[],
  options: { uncategorised: string; unknownCategory: string; otherCategory: string },
  categories?: readonly CategoryLink[],
  overrides?: CategoryColorOverrides,
): CategoryChartModel {
  const prepared = topLevelMonths(months, categoryLabels, categories);
  const chartMonths = prepared.months;
  const labelLookup = new Map(prepared.labels.map((item) => [item.category_id, item]));
  const allNetStrings: string[] = [];
  for (const month of chartMonths) {
    for (const cat of month.categories) allNetStrings.push(cat.net_spending);
  }
  const { scale } = alignDecimals(allNetStrings);

  const perMonthBig = new Map<string, Map<string | null, bigint>>();
  const byMonthAndCategory = new Map<string, Map<string | null, string>>();
  const absTotals = new Map<string | null, bigint>();

  for (const month of chartMonths) {
    const bigMap = new Map<string | null, bigint>();
    const strMap = new Map<string | null, string>();
    for (const entry of month.categories) {
      const parsed = signedBigInt(entry.net_spending);
      const scaled = parsed.coeff * (10n ** BigInt(scale - parsed.scale));
      bigMap.set(entry.category_id, scaled);
      strMap.set(entry.category_id, entry.net_spending);
      const absVal = scaled < 0n ? -scaled : scaled;
      absTotals.set(entry.category_id, (absTotals.get(entry.category_id) ?? 0n) + absVal);
    }
    perMonthBig.set(month.period, bigMap);
    byMonthAndCategory.set(month.period, strMap);
  }

  const colorInputs: CategoryColorInput[] = [...(categories ?? [])];
  const knownColorIds = new Set(colorInputs.map((item) => item.id));
  for (const item of prepared.labels) {
    if (!knownColorIds.has(item.category_id)) {
      knownColorIds.add(item.category_id);
      colorInputs.push({ id: item.category_id, parent_id: null });
    }
  }
  for (const id of absTotals.keys()) {
    if (id !== null && !knownColorIds.has(id)) {
      knownColorIds.add(id);
      colorInputs.push({ id, parent_id: null });
    }
  }
  const categoryColors = buildCategoryColors(colorInputs, overrides === undefined ? undefined : { overrides });

  const allCategories: RankedCategory[] = [...absTotals.entries()].map(([id, absTotal]) => {
    const resolved = categoryColors.get(id);
    if (id === null) {
      return {
        id: null, name: options.uncategorised, archived: false, absTotal,
        color: resolved.background, hatched: resolved.hatched,
      };
    }
    const meta = labelLookup.get(id);
    return {
      id,
      name: meta?.name ?? options.unknownCategory,
      archived: meta?.archived ?? false,
      absTotal,
      color: resolved.background,
      hatched: resolved.hatched,
    };
  });

  allCategories.sort((a, b) => {
    if (a.absTotal !== b.absTotal) return a.absTotal > b.absTotal ? -1 : 1;
    if ((a.id === null) !== (b.id === null)) return a.id === null ? 1 : -1;
    const idA = a.id ?? "";
    const idB = b.id ?? "";
    return idA < idB ? -1 : idA > idB ? 1 : 0;
  });

  const nonzeroCategories = allCategories.filter((item) => item.absTotal > 0n);
  const displayed = nonzeroCategories.slice(0, 5);
  const restIds = new Set(nonzeroCategories.slice(5).map((item) => item.id));

  let hasOtherPositive = false;
  let hasOtherNegative = false;
  const stacks: MonthCategoryStack[] = chartMonths.map((month) => {
    const bigMap = perMonthBig.get(month.period) ?? new Map<string | null, bigint>();
    let posCursor = 0n;
    let negCursor = 0n;
    const slices: CategorySlice[] = [];

    for (const cat of displayed) {
      const val = bigMap.get(cat.id) ?? 0n;
      const sliceKey = cat.id === null ? "__uncategorised__" : cat.id;
      if (val > 0n) {
        slices.push({
          key: sliceKey, label: cat.name, color: cat.color, hatched: cat.hatched,
          value: val, y0: posCursor, y1: posCursor + val,
        });
        posCursor += val;
      } else if (val < 0n) {
        slices.push({
          key: sliceKey, label: cat.name, color: cat.color, hatched: cat.hatched,
          value: val, y0: negCursor, y1: negCursor + val,
        });
        negCursor += val;
      }
    }

    let otherPos = 0n;
    let otherNeg = 0n;
    for (const id of restIds) {
      const val = bigMap.get(id) ?? 0n;
      if (val > 0n) otherPos += val;
      else if (val < 0n) otherNeg += val;
    }
    if (otherPos > 0n) {
      hasOtherPositive = true;
      slices.push({
        key: "__other_pos__", label: options.otherCategory, color: OTHER_CATEGORY_COLOR.background, hatched: true,
        value: otherPos, y0: posCursor, y1: posCursor + otherPos,
      });
      posCursor += otherPos;
    }
    if (otherNeg < 0n) {
      hasOtherNegative = true;
      slices.push({
        key: "__other_neg__", label: options.otherCategory, color: OTHER_CATEGORY_COLOR.background, hatched: true,
        value: otherNeg, y0: negCursor, y1: negCursor + otherNeg,
      });
      negCursor += otherNeg;
    }

    return { month, slices, posTotal: posCursor, negTotal: negCursor };
  });

  return { scale, displayed, hasOtherPositive, hasOtherNegative, allCategories, byMonthAndCategory, stacks };
}

function monthTickSuffix(month: AnalyticsMonthlyBudgetSeriesMonth): string {
  return `${month.is_current_month ? "*" : ""}${month.provisional ? "~" : ""}`;
}

function CategorySpendingChart({ series, currency, locale, text, categories }: {
  series: AnalyticsMonthlyBudgetSeries;
  currency: string;
  locale: string;
  text: MonthBudgetChartLabels;
  categories?: readonly CategoryLink[] | undefined;
}) {
  const id = useId();
  const money = (amount: string) => formatAmount(amount, currency, locale);
  const model = buildCategoryChartModel(series.months, series.category_labels, {
    uncategorised: text.uncategorised,
    unknownCategory: text.unknownCategory,
    otherCategory: text.otherCategory,
  }, categories);

  const yScale = buildYScale(model.stacks.flatMap((stack) => [stack.posTotal, stack.negTotal]));
  const count = Math.max(1, series.months.length);
  const colWidth = (PLOT_RIGHT - PLOT_LEFT) / count;
  const barWidth = Math.min(28, Math.max(8, colWidth * 0.62));
  const compactTicks = count > 6;
  const hasCurrent = series.months.some((month) => month.is_current_month);
  const hasProvisional = series.months.some((month) => month.provisional);
  const hasOther = model.hasOtherPositive || model.hasOtherNegative;
  const customHatchColors = [...new Set(
    model.stacks.flatMap((stack) => stack.slices)
      .filter((slice) => slice.hatched && !slice.key.startsWith("__other_"))
      .map((slice) => slice.color),
  )];

  return (
    <article className="budget-total budget-month-chart" aria-label={`${text.chartCategoriesTitle} · ${currency}`}>
      <h4>{text.chartCategoriesTitle} · {currency}</h4>
      <svg role="img" aria-labelledby={`${id}-title ${id}-desc`} viewBox={`0 0 ${String(CHART_WIDTH)} ${String(CHART_HEIGHT)}`}>
        <title id={`${id}-title`}>{`${text.chartCategoriesTitle} (${currency})`}</title>
        <desc id={`${id}-desc`}>{text.chartCategoriesDesc(series.months.length, currency)}</desc>
        <defs>
          <pattern id={`${id}-other-pattern`} width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <rect width="5" height="5" fill={OTHER_CATEGORY_COLOR.background} fillOpacity="0.25" />
            <line x1="0" y1="0" x2="0" y2="5" stroke={OTHER_CATEGORY_COLOR.background} strokeWidth="2" />
          </pattern>
          {customHatchColors.map((hatchColor, idx) => (
            <pattern key={hatchColor} id={`${id}-hatch-${String(idx)}`} width="5" height="5"
              patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
              <rect width="5" height="5" fill={hatchColor} fillOpacity="0.25" />
              <line x1="0" y1="0" x2="0" y2="5" stroke={hatchColor} strokeWidth="2" />
            </pattern>
          ))}
        </defs>
        <YAxisGrid scaleObj={yScale} scale={model.scale} denom={1n} locale={locale} />
        {model.stacks.map((stack, index) => {
          const cx = PLOT_LEFT + (index + 0.5) * colWidth;
          const x = Number((cx - barWidth / 2).toFixed(2));
          const w = Number(barWidth.toFixed(2));
          const showLabel = !compactTicks || index % 2 === 0 || index === count - 1;
          const tickText = `${shortMonthTick(stack.month.period, locale, compactTicks)}${monthTickSuffix(stack.month)}`;
          return (
            <g key={stack.month.period}>
              {stack.slices.map((slice) => {
                const yA = yScale.toY(slice.y0);
                const yB = yScale.toY(slice.y1);
                const top = Math.min(yA, yB);
                const rectHeight = Math.max(0.5, Number(Math.abs(yA - yB).toFixed(2)));
                const isOther = slice.key.startsWith("__other_");
                const hatchIdx = !isOther && slice.hatched ? customHatchColors.indexOf(slice.color) : -1;
                const fill = isOther
                  ? `url(#${id}-other-pattern)`
                  : hatchIdx >= 0
                    ? `url(#${id}-hatch-${String(hatchIdx)})`
                    : slice.color;
                const stroke = isOther || slice.hatched ? slice.color : "var(--surface)";
                return (
                  <rect key={`${stack.month.period}-${slice.key}`} x={x} y={top} width={w} height={rectHeight}
                    fill={fill} stroke={stroke} strokeWidth="0.6" />
                );
              })}
              {showLabel ? (
                <text x={Number(cx.toFixed(2))} y={PLOT_BOTTOM + 16} textAnchor="middle" fontSize="9.5" fill="var(--fg-soft)">
                  {tickText}
                </text>
              ) : null}
            </g>
          );
        })}
      </svg>
      <ul className="budget-chart-legend">
        {model.displayed.map((cat) => (
          <li key={cat.id ?? "__uncategorised__"}>
            <CategorySwatch color={{ background: cat.color, hatched: cat.hatched }} className="budget-chart-swatch" />
            <span>
              {cat.name}
              {cat.archived ? <small className="budget-muted"> ({text.archived})</small> : null}
            </span>
          </li>
        ))}
        {hasOther ? (
          <li>
            <CategorySwatch color={OTHER_CATEGORY_COLOR} className="budget-chart-swatch" />
            <span>{text.otherCategory}</span>
          </li>
        ) : null}
        {hasCurrent ? <li><span>* {text.currentMonth}</span></li> : null}
        {hasProvisional ? <li><span>~ {text.provisional}</span></li> : null}
      </ul>
      <details className="budget-recurring-evidence">
        <summary>{text.showData}</summary>
        <div className="budget-table-wrap" role="region" aria-label={`${text.chartCategoriesTitle} · ${currency}`} tabIndex={0}>
          <table className="budget-month-dashboard-table">
            <caption>{text.chartCategoriesTitle} · {currency}</caption>
            <thead>
              <tr>
                <th scope="col">{text.category}</th>
                {series.months.map((month) => (
                  <th key={month.period} scope="col">
                    <div>{shortMonthTick(month.period, locale, false)} ({month.period})</div>
                    {month.is_current_month ? <div><small className="budget-muted">{text.currentMonth}</small></div> : null}
                    {month.provisional ? <div><small className="budget-warn">{text.provisional}</small></div> : null}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {model.allCategories.map((cat) => (
                <tr key={cat.id ?? "__uncategorised__"} className={cat.id === null ? "is-uncategorised" : undefined}>
                  <th scope="row">
                    <CategorySwatch color={{ background: cat.color, hatched: cat.hatched }} />
                    {cat.name}
                    {cat.archived ? <small className="budget-muted"> ({text.archived})</small> : null}
                  </th>
                  {series.months.map((month) => {
                    const value = model.byMonthAndCategory.get(month.period)?.get(cat.id);
                    return <td key={month.period}>{value === undefined ? text.noValue : money(value)}</td>;
                  })}
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr>
                <th scope="row"><strong>{text.totalLabel}</strong></th>
                {series.months.map((month) => (
                  <td key={month.period}><strong>{money(month.spent)}</strong></td>
                ))}
              </tr>
            </tfoot>
          </table>
        </div>
      </details>
    </article>
  );
}

// ---------------------------------------------------------------------------
// Chart 3: Income versus spend per month
// ---------------------------------------------------------------------------

function IncomeVsSpendChart({ budget, months, locale, text }: {
  budget: AnalyticsMonthlyBudget;
  months: readonly AnalyticsMonthlyBudgetSeriesMonth[];
  locale: string;
  text: MonthBudgetChartLabels;
}) {
  const id = useId();
  const currency = budget.currency;
  const money = (amount: string) => formatAmount(amount, currency, locale);
  const hasCurrentExpected = budget.expected_income !== null
    && (budget.expected_income.status === "estimated" || budget.expected_income.status === "partial")
    && budget.expected_income.total !== null
    && !isZero(budget.expected_income.total);
  const currentExpectedTotal = hasCurrentExpected ? budget.expected_income?.total ?? null : null;
  const currentExpectedBudget = hasCurrentExpected ? budget.pace?.budget ?? null : null;

  const isGapMonth = (month: AnalyticsMonthlyBudgetSeriesMonth) =>
    !month.has_activity && isZero(month.start_budget) && isZero(month.spent);

  const valuesToScale: string[] = [];
  for (const month of months) {
    if (isGapMonth(month)) continue;
    valuesToScale.push(month.start_budget, month.spent);
    if (month.is_current_month && currentExpectedBudget !== null) valuesToScale.push(currentExpectedBudget);
  }
  const { scale } = alignDecimals(valuesToScale);
  const toAligned = (val: string): bigint => {
    const parsed = signedBigInt(val);
    return parsed.coeff * (10n ** BigInt(scale - parsed.scale));
  };

  const monthBars = months.map((month) => {
    const gap = isGapMonth(month);
    const booked = toAligned(month.start_budget);
    const spent = toAligned(month.spent);
    const expectedTop = month.is_current_month && currentExpectedBudget !== null ? toAligned(currentExpectedBudget) : booked;
    return { month, gap, booked, spent, expectedTop };
  });

  const yScale = buildYScale(
    monthBars.flatMap((bar) => (bar.gap ? [0n] : [bar.booked, bar.spent, bar.expectedTop])),
  );
  const count = Math.max(1, months.length);
  const colWidth = (PLOT_RIGHT - PLOT_LEFT) / count;
  const pairWidth = Math.min(28, Math.max(10, colWidth * 0.72));
  const barW = Number((pairWidth / 2 - 1).toFixed(2));
  const compactTicks = count > 6;
  const hasGaps = monthBars.some((bar) => bar.gap);
  const hasIncomplete = months.some((month) => month.is_current_month);
  const hasProvisional = months.some((month) => month.provisional);
  const hasPartialIncome = months.some((month) => month.income_estimate_partial);

  return (
    <article className="budget-total budget-month-chart" aria-label={`${text.chartIncomeSpendTitle} · ${currency}`}>
      <h4>{text.chartIncomeSpendTitle} · {currency}</h4>
      <svg role="img" aria-labelledby={`${id}-title ${id}-desc`} viewBox={`0 0 ${String(CHART_WIDTH)} ${String(CHART_HEIGHT)}`}>
        <title id={`${id}-title`}>{`${text.chartIncomeSpendTitle} (${currency})`}</title>
        <desc id={`${id}-desc`}>{text.chartIncomeSpendDesc(months.length, currency)}</desc>
        <defs>
          <pattern id={`${id}-spend-pattern`} width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <rect width="5" height="5" fill="var(--chart-spend)" fillOpacity="0.22" />
            <line x1="0" y1="0" x2="0" y2="5" stroke="var(--chart-spend)" strokeWidth="2.2" />
          </pattern>
          <pattern id={`${id}-expected-pattern`} width="4" height="4" patternUnits="userSpaceOnUse" patternTransform="rotate(-45)">
            <rect width="4" height="4" fill="var(--chart-expected)" fillOpacity="0.18" />
            <line x1="0" y1="0" x2="0" y2="4" stroke="var(--chart-expected)" strokeWidth="1.5" />
          </pattern>
        </defs>
        <YAxisGrid scaleObj={yScale} scale={scale} denom={1n} locale={locale} />
        {monthBars.map((bar, index) => {
          const cx = PLOT_LEFT + (index + 0.5) * colWidth;
          const leftX = Number((cx - barW - 1).toFixed(2));
          const rightX = Number((cx + 1).toFixed(2));
          const showLabel = !compactTicks || index % 2 === 0 || index === count - 1;
          const tickText = `${shortMonthTick(bar.month.period, locale, compactTicks)}${monthTickSuffix(bar.month)}`;

          const incY = yScale.toY(bar.booked);
          const incTop = Math.min(incY, yScale.zeroY);
          const incHeight = Math.abs(yScale.zeroY - incY);
          const expY = yScale.toY(bar.expectedTop);
          const expTop = Math.min(expY, incY);
          const expHeight = Math.abs(incY - expY);

          const spendY = yScale.toY(bar.spent);
          const spendTop = Math.min(spendY, yScale.zeroY);
          const spendHeight = Math.abs(yScale.zeroY - spendY);

          return (
            <g key={bar.month.period}>
              {bar.gap ? (
                <line className="budget-chart-gap" x1={leftX} y1={yScale.zeroY} x2={rightX + barW} y2={yScale.zeroY}
                  stroke="var(--muted)" strokeWidth="2" strokeDasharray="3 3" />
              ) : (
                <>
                  {incHeight > 0 ? (
                    <rect className="budget-chart-income-bar" x={leftX} y={incTop} width={barW}
                      height={Math.max(0.5, Number(incHeight.toFixed(2)))} fill="var(--chart-income)" />
                  ) : (
                    <line x1={leftX} y1={yScale.zeroY} x2={leftX + barW} y2={yScale.zeroY} stroke="var(--chart-income)" strokeWidth="1.5" />
                  )}
                  {expHeight > 0 ? (
                    <rect className="budget-chart-expected-bar" x={leftX} y={expTop} width={barW}
                      height={Math.max(0.5, Number(expHeight.toFixed(2)))} fill={`url(#${id}-expected-pattern)`}
                      stroke="var(--chart-expected)" strokeWidth="1" strokeDasharray="2 2" />
                  ) : null}
                  {spendHeight > 0 ? (
                    <rect className="budget-chart-spend-bar" x={rightX} y={spendTop} width={barW}
                      height={Math.max(0.5, Number(spendHeight.toFixed(2)))} fill={`url(#${id}-spend-pattern)`}
                      stroke="var(--chart-spend)" strokeWidth="1" />
                  ) : (
                    <line x1={rightX} y1={yScale.zeroY} x2={rightX + barW} y2={yScale.zeroY} stroke="var(--chart-spend)" strokeWidth="1.5" />
                  )}
                </>
              )}
              {showLabel ? (
                <text x={Number(cx.toFixed(2))} y={PLOT_BOTTOM + 16} textAnchor="middle" fontSize="9.5" fill="var(--fg-soft)">
                  {tickText}
                </text>
              ) : null}
            </g>
          );
        })}
      </svg>
      <ul className="budget-chart-legend">
        <li>
          <span className="budget-chart-swatch" style={{ background: "var(--chart-income)", borderColor: "transparent" }} aria-hidden="true" />
          <span>{text.budgetReceived}</span>
        </li>
        {currentExpectedTotal !== null ? (
          <li>
            <span className="budget-chart-swatch" style={{
              background: `repeating-linear-gradient(-45deg, var(--chart-expected), var(--chart-expected) 2px, var(--surface) 2px, var(--surface) 4px)`,
              borderColor: "var(--chart-expected)",
            }} aria-hidden="true" />
            <span>{text.expectedIncomeLegend}: <strong>{money(currentExpectedTotal)}</strong></span>
          </li>
        ) : null}
        <li>
          <span className="budget-chart-swatch" style={{
            background: `repeating-linear-gradient(45deg, var(--chart-spend), var(--chart-spend) 2px, var(--surface) 2px, var(--surface) 4px)`,
            borderColor: "var(--chart-spend)",
          }} aria-hidden="true" />
          <span>{text.netSpending}</span>
        </li>
        {hasIncomplete ? <li><span>* {text.incompleteMonth}</span></li> : null}
        {hasProvisional ? <li><span>~ {text.provisional}</span></li> : null}
        {hasPartialIncome ? <li><span>{text.partialIncome}</span></li> : null}
        {hasGaps ? <li><span>{text.noStoredData}</span></li> : null}
      </ul>
      <details className="budget-recurring-evidence">
        <summary>{text.showData}</summary>
        <div className="budget-table-wrap" role="region" aria-label={`${text.chartIncomeSpendTitle} · ${currency}`} tabIndex={0}>
          <table className="budget-month-dashboard-table">
            <caption>{text.chartIncomeSpendTitle} · {currency}</caption>
            <thead>
              <tr>
                <th scope="col">{text.monthColumn}</th>
                <th scope="col">{text.budgetReceived}</th>
                {currentExpectedTotal !== null ? <th scope="col">{text.expectedIncomeLegend}</th> : null}
                <th scope="col">{text.netSpending}</th>
                <th scope="col">{text.statusColumn}</th>
              </tr>
            </thead>
            <tbody>
              {monthBars.map(({ month, gap }) => {
                const markers: string[] = [];
                if (gap) markers.push(text.noStoredData);
                if (month.is_current_month) markers.push(text.incompleteMonth);
                if (month.provisional) markers.push(text.provisional);
                if (month.income_estimate_partial) markers.push(text.partialIncome);
                return (
                  <tr key={month.period}>
                    <th scope="row">{fullMonthLabel(month.period, locale)}</th>
                    <td>{gap ? text.noValue : money(month.start_budget)}</td>
                    {currentExpectedTotal !== null ? (
                      <td>{month.is_current_month ? money(currentExpectedTotal) : text.noValue}</td>
                    ) : null}
                    <td>{gap ? text.noValue : money(month.spent)}</td>
                    <td>{markers.length === 0 ? text.noValue : markers.join(" · ")}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </details>
    </article>
  );
}

export function MonthBudgetCharts({
  budget, series, paceProjection, schedulePending = false, scheduleError = false, locale, text, categories,
}: {
  budget: AnalyticsMonthlyBudget;
  series: AnalyticsMonthlyBudgetSeries;
  paceProjection: MonthPaceProjection | null;
  schedulePending?: boolean;
  scheduleError?: boolean;
  locale: string;
  text: MonthBudgetChartLabels;
  categories?: readonly CategoryLink[] | undefined;
}) {
  return (
    <section className="budget-month-charts" aria-label={text.chartsSectionTitle}>
      <CumulativeDailyChart budget={budget} daily={series.daily} paceProjection={paceProjection}
        schedulePending={schedulePending} scheduleError={scheduleError} locale={locale} text={text} />
      <CategorySpendingChart series={series} currency={budget.currency} locale={locale} text={text} categories={categories} />
      <IncomeVsSpendChart budget={budget} months={series.months} locale={locale} text={text} />
    </section>
  );
}
