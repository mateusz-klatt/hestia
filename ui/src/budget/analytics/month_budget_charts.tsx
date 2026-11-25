// Read-only monthly budget charts (P2, OpenAPI 0.29.0): cumulative daily spend, stacked category spending,
// and income vs. spending. Uses React inline SVG with bounded viewBox coordinates derived from BigInt scaling
// plus keyboard-operable exact HTML data tables.
import { useId } from "react";
import { t, tPlural } from "../../i18n";
import { formatAmount, formatEstimate } from "../model";
import { CategorySwatch } from "./category_badge";
import { OTHER_CATEGORY_COLOR, buildCategoryColors, oneOffCategoryColor } from "./category_colors";
import type { CategoryColorInput, CategoryColorOverrides } from "./category_colors";
import { categoryParentMap, hasCategoryParents, rollupNetAmounts } from "./category_tree";
import type { CategoryLink } from "./category_tree";
import { formatRatioHalfEven, formatScaledBigInt, isZero, parseScaledInt } from "./model";
import type { AnalyticsMonthlyBudget, AnalyticsMonthlyBudgetCategoryLabel, AnalyticsMonthlyBudgetDailyPoint,
  AnalyticsMonthlyBudgetSeries, AnalyticsMonthlyBudgetSeriesMonth } from "./model";
import type { MonthPaceProjection, ScheduledPaceItem } from "./month_budget_pace";

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
): string {
  const date = formatShortDate(item.expectedDate, locale);
  const status = item.overdue ? t("budget.monthBudget.overdue") : t("budget.monthBudget.upcoming");
  const formatted = item.estimated
    ? formatEstimate(item.amount, currency, locale)
    : formatAmount(item.amount, currency, locale);
  return `${date} · ${item.label} · ${formatted} · ${status}`;
}

function CumulativeDailyChart({ budget, daily, paceProjection, schedulePending, scheduleError, locale }: {
  budget: AnalyticsMonthlyBudget;
  daily: readonly AnalyticsMonthlyBudgetDailyPoint[];
  paceProjection: MonthPaceProjection | null;
  schedulePending: boolean;
  scheduleError: boolean;
  locale: string;
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
  const chartAria = t("budget.monthBudget.chartDailyAria", { currency });
  const cumulativeLabel = t("budget.monthBudget.cumulativeSpentLabel");
  const paceLabel = t("budget.monthBudget.paceExtensionLine");
  const withoutScheduledText = t("budget.monthBudget.withoutScheduledPayments");
  const noValueText = t("budget.monthBudget.noValue");

  return (
    <article className="budget-total budget-month-chart" aria-label={chartAria}>
      <h4>{chartAria}</h4>
      <svg role="img" aria-labelledby={`${id}-title ${id}-desc`} viewBox={`0 0 ${String(CHART_WIDTH)} ${String(CHART_HEIGHT)}`}>
        <title id={`${id}-title`}>{t("budget.monthBudget.chartDailySvgTitle", { currency })}</title>
        <desc id={`${id}-desc`}>{t("budget.monthBudget.chartDailyDesc", { currency, start: budget.start, cutoff: budget.cutoff })}</desc>
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
              <title>{point.items.map((item) => scheduledItemLabel(item, currency, locale)).join("\n")}</title>
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
          <span>{cumulativeLabel}: <strong>{money(lastCumulative)}</strong></span>
        </li>
        <li>
          <span className="budget-chart-line-swatch" style={{ color: "var(--chart-budget)", borderTopStyle: "dashed" }} aria-hidden="true" />
          <span>{t("budget.monthBudget.bookedBudgetLine")}: <strong>{money(startBudget)}</strong></span>
        </li>
        {hasExpectedBudget ? (
          <li>
            <span className="budget-chart-line-swatch" style={{ color: "var(--chart-expected)", borderTopStyle: "dotted" }} aria-hidden="true" />
            <span>{t("budget.monthBudget.expectedBudgetLine")}: <strong>{money(expectedBudget)}</strong></span>
          </li>
        ) : null}
        {showProjectedSummary && projected !== null ? (
          <li>
            <span className="budget-chart-line-swatch" style={{ color: "var(--chart-spend)", borderTopStyle: "dotted" }} aria-hidden="true" />
            <span>
              {paceLabel}: <strong>{formatRatioHalfEven(projected, currency, locale)}</strong>
              {scheduleError ? ` (${withoutScheduledText})` : ""}
            </span>
          </li>
        ) : null}
        {schedulePending ? (
          <li role="status">
            <span>{t("budget.monthBudget.scheduledPaymentsLoading")}</span>
          </li>
        ) : null}
        {scheduleError && (!showProjectedSummary || projected === null) ? (
          <li className="budget-warn">
            <span>{withoutScheduledText}</span>
          </li>
        ) : null}
        {projection?.scheduledItems.map((item, index) => (
          <li key={`${item.expectedDate}-${String(index)}`} className={item.overdue ? "budget-warn" : undefined}>
            <span className="budget-chart-step-swatch" data-overdue={item.overdue}
              style={{ color: item.overdue ? "var(--err)" : "var(--chart-expected)" }} aria-hidden="true">
              {item.overdue ? "!" : null}
            </span>
            <span>{scheduledItemLabel(item, currency, locale)}</span>
          </li>
        ))}
        {exhaustionDay !== null ? (
          <li className="budget-warn">
            <span>{t("budget.monthBudget.exhaustionMarker", { day: exhaustionDay })}</span>
          </li>
        ) : null}
      </ul>
      <details className="budget-recurring-evidence">
        <summary>{t("budget.monthBudget.showData")}</summary>
        <div className="budget-table-wrap" role="region" aria-label={chartAria} tabIndex={0}>
          <table className="budget-month-dashboard-table">
            <caption>{chartAria}</caption>
            <thead>
              <tr>
                <th scope="col">{t("budget.monthBudget.dateLabel")}</th>
                <th scope="col">{cumulativeLabel}</th>
                {showProjectedColumn ? <th scope="col">{paceLabel}</th> : null}
                {showStepsColumn ? <th scope="col">{t("budget.monthBudget.scheduledStepsColumn")}</th> : null}
              </tr>
            </thead>
            <tbody>
              {allDates.map((date) => {
                const actual = dailyByDate.get(date);
                const projPoint = projectionByDate.get(date);
                return (
                  <tr key={date}>
                    <th scope="row">{date}</th>
                    <td>{actual !== undefined ? money(actual) : noValueText}</td>
                    {showProjectedColumn ? (
                      <td>{projPoint !== undefined ? formatRatioHalfEven(projPoint.value, currency, locale) : noValueText}</td>
                    ) : null}
                    {showStepsColumn ? (
                      <td>
                        {projPoint !== undefined && projPoint.items.length > 0 ? (
                          <div className="budget-month-steps-cell">
                            {projPoint.items.map((item, index) => (
                              <div key={`${item.expectedDate}-${String(index)}`} className={item.overdue ? "budget-warn" : undefined}>
                                {scheduledItemLabel(item, currency, locale)}
                              </div>
                            ))}
                          </div>
                        ) : noValueText}
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
// Chart 2: Category spending by month (stacked around zero, top 8 + Other)
// ---------------------------------------------------------------------------

/** Roots drawn on the stacked chart. Everything past this rank is hatched "Pozostałe". */
export const TOP_CATEGORY_ROOTS = 8;

export interface RankedCategory {
  id: string | null;
  /** Chart identity. Regular rows are `c:<id>`; a one-off uses its annotation hash. */
  seriesKey: string;
  name: string;
  archived: boolean;
  absTotal: bigint;
  color: string;
  hatched: boolean;
  oneOff: boolean;
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
  oneOffs: RankedCategory[];
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
    months: months.map((month) => {
      // A labelled one-off stays beside its category. Only the regular rows roll up to the parent.
      const regular = month.categories.filter((entry) => entry.one_off !== true);
      const labelled = month.categories.filter((entry) => entry.one_off === true);
      const rolled = rollupNetAmounts(regular, parents, null).map((entry) => ({
        category_id: entry.category_id,
        net_spending: entry.net_spending,
        name: null,
        one_off: false,
        one_off_key: null,
      }));
      return { ...month, categories: [...rolled, ...labelled] };
    }),
  };
}

function chartPoint(entry: AnalyticsMonthlyBudgetSeriesMonth["categories"][number]): {
  category_id: string | null;
  net_spending: string;
  one_off: boolean;
  name: string | null;
  one_off_key: string | null;
} {
  const name = typeof entry.name === "string" && entry.name !== "" ? entry.name : null;
  const rawKey = entry.one_off_key;
  const oneOffKey = typeof rawKey === "string" && rawKey !== "" ? rawKey : null;
  const oneOff = entry.one_off === true && name !== null;
  return {
    category_id: entry.category_id,
    net_spending: entry.net_spending,
    one_off: oneOff,
    name,
    one_off_key: oneOff ? oneOffKey : null,
  };
}

function seriesKeyOf(point: {
  category_id: string | null;
  one_off: boolean;
  one_off_key: string | null;
}): string {
  // Regular rows stay `c:<id>` so the ranking can recover the category id. A one-off uses its hash.
  if (point.one_off) return point.one_off_key ?? `one-off:${point.category_id ?? ""}`;
  return `c:${point.category_id ?? ""}`;
}

function categoryIdFromSeriesKey(seriesKey: string): string | null {
  // Regular series keys are always `c:` plus the category id. `c:` itself is uncategorised.
  const id = seriesKey.slice(2);
  return id === "" ? null : id;
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

  const perMonthBig = new Map<string, Map<string, bigint>>();
  const byMonthAndCategory = new Map<string, Map<string | null, string>>();
  const absTotals = new Map<string, bigint>();
  const oneOffAbs = new Map<string, bigint>();
  const oneOffNames = new Map<string, { id: string | null; name: string }>();

  for (const month of chartMonths) {
    const bigMap = new Map<string, bigint>();
    const strMap = new Map<string | null, string>();
    for (const entry of month.categories) {
      const point = chartPoint(entry);
      const key = seriesKeyOf(point);
      const parsed = signedBigInt(point.net_spending);
      const scaled = parsed.coeff * (10n ** BigInt(scale - parsed.scale));
      bigMap.set(key, (bigMap.get(key) ?? 0n) + scaled);
      // Regular amounts stay under the category id, which is how the tree roll-up is read.
      // A one-off keeps its hash so it does not replace that category.
      strMap.set(point.one_off ? key : point.category_id, point.net_spending);
      const absVal = scaled < 0n ? -scaled : scaled;
      if (point.one_off) {
        oneOffAbs.set(key, (oneOffAbs.get(key) ?? 0n) + absVal);
        oneOffNames.set(key, { id: point.category_id, name: point.name ?? options.unknownCategory });
      } else {
        absTotals.set(key, (absTotals.get(key) ?? 0n) + absVal);
      }
    }
    perMonthBig.set(month.period, bigMap);
    byMonthAndCategory.set(month.period, strMap);
  }

  const colorInputs: CategoryColorInput[] = [...(categories ?? [])];
  const knownColorIds = new Set(colorInputs.map((item) => item.id));
  const rememberColorId = (id: string | null, name?: string) => {
    if (id === null || knownColorIds.has(id)) return;
    knownColorIds.add(id);
    colorInputs.push(name === undefined || name === ""
      ? { id, parent_id: null }
      : { id, parent_id: null, name });
  };
  for (const item of prepared.labels) rememberColorId(item.category_id, item.name);
  for (const seriesKey of absTotals.keys()) rememberColorId(categoryIdFromSeriesKey(seriesKey));
  for (const meta of oneOffNames.values()) rememberColorId(meta.id);
  const categoryColors = buildCategoryColors(colorInputs, overrides === undefined ? undefined : { overrides });

  const allCategories: RankedCategory[] = [...absTotals.entries()].map(([seriesKey, absTotal]) => {
    const id = categoryIdFromSeriesKey(seriesKey);
    const resolved = categoryColors.get(id);
    if (id === null) {
      return {
        id: null, seriesKey, name: options.uncategorised, archived: false, absTotal, oneOff: false,
        color: resolved.background, hatched: resolved.hatched,
      };
    }
    const meta = labelLookup.get(id);
    return {
      id,
      seriesKey,
      name: meta?.name ?? options.unknownCategory,
      archived: meta?.archived ?? false,
      absTotal,
      oneOff: false,
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

  const oneOffs: RankedCategory[] = [...oneOffAbs.entries()].map(([seriesKey, absTotal]) => {
    const meta = oneOffNames.get(seriesKey);
    const id = meta?.id ?? null;
    const resolved = oneOffCategoryColor(categoryColors, id);
    return {
      id,
      seriesKey,
      name: meta?.name ?? options.unknownCategory,
      archived: false,
      absTotal,
      oneOff: true,
      color: resolved.background,
      hatched: resolved.hatched,
    };
  });
  oneOffs.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : a.seriesKey < b.seriesKey ? -1 : 1));

  const nonzeroCategories = allCategories.filter((item) => item.absTotal > 0n);
  const displayed = nonzeroCategories.slice(0, TOP_CATEGORY_ROOTS);
  const restKeys = new Set(nonzeroCategories.slice(TOP_CATEGORY_ROOTS).map((item) => item.seriesKey));

  let hasOtherPositive = false;
  let hasOtherNegative = false;
  const stacks: MonthCategoryStack[] = chartMonths.map((month) => {
    const bigMap = perMonthBig.get(month.period) ?? new Map<string, bigint>();
    let posCursor = 0n;
    let negCursor = 0n;
    const slices: CategorySlice[] = [];

    const pushSlice = (cat: RankedCategory, val: bigint) => {
      if (val === 0n) return;
      const sliceKey = cat.oneOff ? cat.seriesKey : cat.id === null ? "__uncategorised__" : cat.id;
      if (val > 0n) {
        slices.push({
          key: sliceKey, label: cat.name, color: cat.color, hatched: cat.hatched,
          value: val, y0: posCursor, y1: posCursor + val,
        });
        posCursor += val;
      } else {
        slices.push({
          key: sliceKey, label: cat.name, color: cat.color, hatched: cat.hatched,
          value: val, y0: negCursor, y1: negCursor + val,
        });
        negCursor += val;
      }
    };

    for (const cat of displayed) pushSlice(cat, bigMap.get(cat.seriesKey) ?? 0n);

    let otherPos = 0n;
    let otherNeg = 0n;
    for (const key of restKeys) {
      const val = bigMap.get(key) ?? 0n;
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
    // One-offs are drawn after Other and never folded into it, even when the amount is small.
    for (const cat of oneOffs) pushSlice(cat, bigMap.get(cat.seriesKey) ?? 0n);

    return { month, slices, posTotal: posCursor, negTotal: negCursor };
  });

  return {
    scale, displayed, hasOtherPositive, hasOtherNegative, allCategories, oneOffs, byMonthAndCategory, stacks,
  };
}

function monthTickSuffix(month: AnalyticsMonthlyBudgetSeriesMonth): string {
  return `${month.is_current_month ? "*" : ""}${month.provisional ? "~" : ""}`;
}

function CategorySpendingChart({ series, currency, locale, categories }: {
  series: AnalyticsMonthlyBudgetSeries;
  currency: string;
  locale: string;
  categories?: readonly CategoryLink[] | undefined;
}) {
  const id = useId();
  const money = (amount: string) => formatAmount(amount, currency, locale);
  const otherCategoryLabel = t("budget.monthBudget.otherCategory");
  const model = buildCategoryChartModel(series.months, series.category_labels, {
    uncategorised: t("budget.monthBudget.uncategorised"),
    unknownCategory: t("budget.monthBudget.unknownCategory"),
    otherCategory: otherCategoryLabel,
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
  const chartAria = t("budget.monthBudget.chartCategoriesAria", { currency });
  const archivedText = t("budget.monthBudget.archived");
  const oneOffTagText = t("budget.monthBudget.oneOffTag");
  const currentMonthText = t("budget.monthBudget.currentMonth");
  const provisionalText = t("budget.monthBudget.provisional");

  return (
    <article className="budget-total budget-month-chart" aria-label={chartAria}>
      <h4>{chartAria}</h4>
      <svg role="img" aria-labelledby={`${id}-title ${id}-desc`} viewBox={`0 0 ${String(CHART_WIDTH)} ${String(CHART_HEIGHT)}`}>
        <title id={`${id}-title`}>{t("budget.monthBudget.chartCategoriesSvgTitle", { currency })}</title>
        <desc id={`${id}-desc`}>{tPlural("budget.monthBudget.chartCategoriesDesc", series.months.length, { currency })}</desc>
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
          <li key={cat.seriesKey}>
            <CategorySwatch color={{ background: cat.color, hatched: cat.hatched }} className="budget-chart-swatch" />
            <span>
              {cat.name}
              {cat.archived ? <small className="budget-muted"> ({archivedText})</small> : null}
            </span>
          </li>
        ))}
        {model.oneOffs.map((cat) => (
          <li key={cat.seriesKey} className="budget-one-off">
            <CategorySwatch color={{ background: cat.color, hatched: cat.hatched }} className="budget-chart-swatch" />
            <span>
              {cat.name}
              <small className="budget-one-off-tag">{oneOffTagText}</small>
            </span>
          </li>
        ))}
        {hasOther ? (
          <li>
            <CategorySwatch color={OTHER_CATEGORY_COLOR} className="budget-chart-swatch" />
            <span>{otherCategoryLabel}</span>
          </li>
        ) : null}
        {hasCurrent ? <li><span>* {currentMonthText}</span></li> : null}
        {hasProvisional ? <li><span>~ {provisionalText}</span></li> : null}
      </ul>
      <details className="budget-recurring-evidence">
        <summary>{t("budget.monthBudget.showData")}</summary>
        <div className="budget-table-wrap" role="region" aria-label={chartAria} tabIndex={0}>
          <table className="budget-month-dashboard-table">
            <caption>{chartAria}</caption>
            <thead>
              <tr>
                <th scope="col">{t("budget.monthBudget.category")}</th>
                {series.months.map((month) => (
                  <th key={month.period} scope="col">
                    <div>{shortMonthTick(month.period, locale, false)} ({month.period})</div>
                    {month.is_current_month ? <div><small className="budget-muted">{currentMonthText}</small></div> : null}
                    {month.provisional ? <div><small className="budget-warn">{provisionalText}</small></div> : null}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {[...model.allCategories, ...model.oneOffs].map((cat) => (
                <tr key={cat.seriesKey} className={cat.oneOff ? "budget-one-off" : cat.id === null ? "is-uncategorised" : undefined}>
                  <th scope="row">
                    <CategorySwatch color={{ background: cat.color, hatched: cat.hatched }} />
                    {cat.name}
                    {cat.oneOff ? <small className="budget-one-off-tag">{oneOffTagText}</small> : null}
                    {cat.archived ? <small className="budget-muted"> ({archivedText})</small> : null}
                  </th>
                  {series.months.map((month) => {
                    const value = model.byMonthAndCategory.get(month.period)?.get(cat.oneOff ? cat.seriesKey : cat.id);
                    return <td key={month.period}>{value === undefined ? t("budget.monthBudget.noValue") : money(value)}</td>;
                  })}
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr>
                <th scope="row"><strong>{t("budget.monthBudget.totalLabel")}</strong></th>
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

function IncomeVsSpendChart({ budget, months, locale }: {
  budget: AnalyticsMonthlyBudget;
  months: readonly AnalyticsMonthlyBudgetSeriesMonth[];
  locale: string;
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
  const chartAria = t("budget.monthBudget.chartIncomeSpendAria", { currency });
  const budgetReceivedText = t("budget.monthBudget.budgetReceived");
  const expectedIncomeLegendText = t("budget.monthBudget.expectedIncomeLegend");
  const netSpendingText = t("budget.monthBudget.netSpending");
  const incompleteMonthText = t("budget.monthBudget.incompleteMonth");
  const provisionalText = t("budget.monthBudget.provisional");
  const partialIncomeText = t("budget.monthBudget.partialIncome");
  const noStoredDataText = t("budget.monthBudget.noStoredData");
  const noValueText = t("budget.monthBudget.noValue");

  return (
    <article className="budget-total budget-month-chart" aria-label={chartAria}>
      <h4>{chartAria}</h4>
      <svg role="img" aria-labelledby={`${id}-title ${id}-desc`} viewBox={`0 0 ${String(CHART_WIDTH)} ${String(CHART_HEIGHT)}`}>
        <title id={`${id}-title`}>{t("budget.monthBudget.chartIncomeSpendSvgTitle", { currency })}</title>
        <desc id={`${id}-desc`}>{tPlural("budget.monthBudget.chartIncomeSpendDesc", months.length, { currency })}</desc>
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
          <span>{budgetReceivedText}</span>
        </li>
        {currentExpectedTotal !== null ? (
          <li>
            <span className="budget-chart-swatch" style={{
              background: `repeating-linear-gradient(-45deg, var(--chart-expected), var(--chart-expected) 2px, var(--surface) 2px, var(--surface) 4px)`,
              borderColor: "var(--chart-expected)",
            }} aria-hidden="true" />
            <span>{expectedIncomeLegendText}: <strong>{money(currentExpectedTotal)}</strong></span>
          </li>
        ) : null}
        <li>
          <span className="budget-chart-swatch" style={{
            background: `repeating-linear-gradient(45deg, var(--chart-spend), var(--chart-spend) 2px, var(--surface) 2px, var(--surface) 4px)`,
            borderColor: "var(--chart-spend)",
          }} aria-hidden="true" />
          <span>{netSpendingText}</span>
        </li>
        {hasIncomplete ? <li><span>* {incompleteMonthText}</span></li> : null}
        {hasProvisional ? <li><span>~ {provisionalText}</span></li> : null}
        {hasPartialIncome ? <li><span>{partialIncomeText}</span></li> : null}
        {hasGaps ? <li><span>{noStoredDataText}</span></li> : null}
      </ul>
      <details className="budget-recurring-evidence">
        <summary>{t("budget.monthBudget.showData")}</summary>
        <div className="budget-table-wrap" role="region" aria-label={chartAria} tabIndex={0}>
          <table className="budget-month-dashboard-table">
            <caption>{chartAria}</caption>
            <thead>
              <tr>
                <th scope="col">{t("budget.monthBudget.monthColumn")}</th>
                <th scope="col">{budgetReceivedText}</th>
                {currentExpectedTotal !== null ? <th scope="col">{expectedIncomeLegendText}</th> : null}
                <th scope="col">{netSpendingText}</th>
                <th scope="col">{t("budget.monthBudget.statusColumn")}</th>
              </tr>
            </thead>
            <tbody>
              {monthBars.map(({ month, gap }) => {
                const markers: string[] = [];
                if (gap) markers.push(noStoredDataText);
                if (month.is_current_month) markers.push(incompleteMonthText);
                if (month.provisional) markers.push(provisionalText);
                if (month.income_estimate_partial) markers.push(partialIncomeText);
                return (
                  <tr key={month.period}>
                    <th scope="row">{fullMonthLabel(month.period, locale)}</th>
                    <td>{gap ? noValueText : money(month.start_budget)}</td>
                    {currentExpectedTotal !== null ? (
                      <td>{month.is_current_month ? money(currentExpectedTotal) : noValueText}</td>
                    ) : null}
                    <td>{gap ? noValueText : money(month.spent)}</td>
                    <td>{markers.length === 0 ? noValueText : markers.join(" · ")}</td>
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
  budget, series, paceProjection, schedulePending = false, scheduleError = false, locale, categories,
}: {
  budget: AnalyticsMonthlyBudget;
  series: AnalyticsMonthlyBudgetSeries;
  paceProjection: MonthPaceProjection | null;
  schedulePending?: boolean;
  scheduleError?: boolean;
  locale: string;
  categories?: readonly CategoryLink[] | undefined;
}) {
  return (
    <section className="budget-month-charts" aria-label={t("budget.monthBudget.chartsSectionTitle")}>
      <CumulativeDailyChart budget={budget} daily={series.daily} paceProjection={paceProjection}
        schedulePending={schedulePending} scheduleError={scheduleError} locale={locale} />
      <CategorySpendingChart series={series} currency={budget.currency} locale={locale} categories={categories} />
      <IncomeVsSpendChart budget={budget} months={series.months} locale={locale} />
    </section>
  );
}
