import { canonicalAmount, daysBetween, formatScaledBigInt, parseScaledInt } from "./model";
import type { AnalyticsForecast, AnalyticsMonthlyBudget, AnalyticsMonthlyBudgetPace, AnalyticsMonthlyBudgetRatio,
  AnalyticsMortgageSchedule, AnalyticsMortgages, AnalyticsRecurring, AnalyticsRecurringHint } from "./model";

export interface MonthPaceScheduleData {
  forecast: AnalyticsForecast;
  mortgages: AnalyticsMortgages;
  recurring: AnalyticsRecurring;
}

export interface ScheduledPaceItem {
  label: string;
  amount: string;
  expectedDate: string;
  stepDay: number;
  overdue: boolean;
}

export interface MonthPacePoint {
  day: number;
  date: string;
  before: AnalyticsMonthlyBudgetRatio;
  value: AnalyticsMonthlyBudgetRatio;
  items: readonly ScheduledPaceItem[];
}

export interface MonthPaceProjection {
  status: AnalyticsMonthlyBudgetPace["status"];
  dailyRate: AnalyticsMonthlyBudgetRatio | null;
  projectedMonthSpend: AnalyticsMonthlyBudgetRatio | null;
  projectedRemaining: AnalyticsMonthlyBudgetRatio | null;
  exhaustionDay: number | null;
  scheduledItems: readonly ScheduledPaceItem[];
  points: readonly MonthPacePoint[];
}

function highConfidence(hint: AnalyticsRecurringHint): boolean {
  return hint.occurrence_count >= 6 && hint.max_gap_days - hint.min_gap_days <= 7;
}

function sameAmount(left: string, right: string): boolean {
  const a = canonicalAmount(left);
  const b = canonicalAmount(right);
  return a !== null && a === b;
}

function dueDate(schedule: AnalyticsMortgageSchedule, period: string): string | null {
  const year = Number(period.slice(0, 4));
  const month = Number(period.slice(5, 7));
  const lastDay = new Date(Date.UTC(year, month, 0)).getUTCDate();
  const due = `${period}-${String(Math.min(schedule.due_day, lastDay)).padStart(2, "0")}`;
  if (due < schedule.start_date || (schedule.end_date !== null && due > schedule.end_date)) return null;
  return due;
}

interface Edge {
  distance: number;
  due: string;
  hintIndex: number;
  scheduleId: string;
  hintKey: string;
}

function matchEdges(edges: Edge[], matchedHints: Set<number>, matchedSchedules: Set<string>): void {
  edges.sort((a, b) => a.distance - b.distance || a.due.localeCompare(b.due)
    || a.hintKey.localeCompare(b.hintKey) || a.scheduleId.localeCompare(b.scheduleId));
  for (const edge of edges) {
    if (matchedHints.has(edge.hintIndex) || matchedSchedules.has(edge.scheduleId)) continue;
    matchedHints.add(edge.hintIndex);
    matchedSchedules.add(edge.scheduleId);
  }
}

function paidScheduledAmounts(budget: AnalyticsMonthlyBudget, data?: MonthPaceScheduleData): {
  mortgageAmounts: string[];
  recurringAmounts: string[];
  paidScheduleIds: ReadonlySet<string>;
} {
  if (data === undefined) return { mortgageAmounts: [], recurringAmounts: [], paidScheduleIds: new Set() };
  const period = budget.period;
  const currency = budget.currency;
  const schedules = data.mortgages.schedules
    .map((schedule) => ({
      schedule,
      due: schedule.enabled && schedule.currency === currency ? dueDate(schedule, period) : null,
    }))
    .filter((entry): entry is { schedule: AnalyticsMortgageSchedule; due: string } => entry.due !== null);
  const hints = data.recurring.hints.filter((hint) => hint.currency === currency
    && hint.last_date.startsWith(`${period}-`)
    && hint.last_date <= budget.evaluation_date
    && highConfidence(hint));
  const edges: Edge[] = [];
  hints.forEach((hint, hintIndex) => {
    for (const { schedule, due } of schedules) {
      if (!sameAmount(hint.amount, schedule.amount)) continue;
      const distance = daysBetween(hint.last_date, due);
      if (distance <= 5) {
        edges.push({
          distance, due, hintIndex, scheduleId: schedule.id,
          hintKey: `${hint.label}|${hint.label_source}|${hint.first_date}|${hint.last_date}`,
        });
      }
    }
  });
  const matchedHints = new Set<number>();
  const matchedSchedules = new Set<string>();
  matchEdges(edges, matchedHints, matchedSchedules);
  const paidScheduleIds = new Set(matchedSchedules);
  for (const { schedule } of schedules) {
    if (schedule.paid_months.includes(period)) paidScheduleIds.add(schedule.id);
  }
  // A mortgage explicitly marked paid in paid_months may have booked > 5 days from its due date;
  // pair it 1:1 with a same-amount unmatched hint in this period so it is not subtracted twice.
  const latePaidEdges: Edge[] = [];
  hints.forEach((hint, hintIndex) => {
    if (matchedHints.has(hintIndex)) return;
    for (const { schedule, due } of schedules) {
      if (!paidScheduleIds.has(schedule.id) || matchedSchedules.has(schedule.id)) continue;
      if (!sameAmount(hint.amount, schedule.amount)) continue;
      latePaidEdges.push({
        distance: daysBetween(hint.last_date, due), due, hintIndex, scheduleId: schedule.id,
        hintKey: `${hint.label}|${hint.label_source}|${hint.first_date}|${hint.last_date}`,
      });
    }
  });
  matchEdges(latePaidEdges, matchedHints, matchedSchedules);
  return {
    mortgageAmounts: schedules.filter(({ schedule }) => paidScheduleIds.has(schedule.id))
      .map(({ schedule }) => schedule.amount),
    recurringAmounts: hints.filter((_, index) => !matchedHints.has(index)).map((hint) => hint.amount),
    paidScheduleIds,
  };
}

function hasKnownDueDate(source: string, confidence: string): boolean {
  return source === "mortgage" || (source === "recurring" && confidence === "high");
}

function scheduledItems(budget: AnalyticsMonthlyBudget, data: MonthPaceScheduleData | undefined,
  paidScheduleIds: ReadonlySet<string>): ScheduledPaceItem[] {
  const month = data?.forecast.months.find((item) => item.month === budget.period);
  const cutoffDay = Number(budget.evaluation_date.slice(8, 10));
  return (month?.items ?? []).filter((item) => item.currency === budget.currency
    && hasKnownDueDate(item.source, item.confidence)
    && !(item.source === "mortgage" && item.schedule_id !== null && paidScheduleIds.has(item.schedule_id)))
    .map((item) => {
      const overdue = item.expected_date < budget.evaluation_date;
      return {
        label: item.label,
        amount: item.amount,
        expectedDate: item.expected_date,
        stepDay: overdue ? cutoffDay : Number(item.expected_date.slice(8, 10)),
        overdue,
      };
    })
    .sort((a, b) => a.stepDay - b.stepDay || a.expectedDate.localeCompare(b.expectedDate)
      || a.label.localeCompare(b.label) || a.amount.localeCompare(b.amount));
}

function decimalScale(values: readonly string[]): number {
  return values.reduce((scale, value) => Math.max(scale, parseScaledInt(value)?.scale ?? 0), 0);
}

function decimalUnits(value: string, scale: number): bigint {
  const parsed = parseScaledInt(value);
  if (parsed === null) return 0n;
  return (parsed.negative ? -parsed.coeff : parsed.coeff) * (10n ** BigInt(scale - parsed.scale));
}

function ratio(units: bigint, scale: number, denominator: number): AnalyticsMonthlyBudgetRatio {
  return { numerator: formatScaledBigInt(units, scale), denominator };
}

function clampBigInt(value: bigint, min: bigint, max: bigint): bigint {
  return value < min ? min : value > max ? max : value;
}

export function buildMonthPaceProjection(
  budget: AnalyticsMonthlyBudget,
  data?: MonthPaceScheduleData,
): MonthPaceProjection | null {
  const { summary, pace } = budget;
  if (summary === null || pace === null || budget.period !== budget.current_month || pace.status === "historical") {
    return null;
  }
  const cutoffDay = Number(budget.evaluation_date.slice(8, 10));
  const daysInMonth = Number(budget.end.slice(8, 10));
  const paid = paidScheduledAmounts(budget, data);
  const scheduled = scheduledItems(budget, data, paid.paidScheduleIds);
  const passThrough = budget.pass_through;
  const scale = decimalScale([
    summary.spent,
    pace.budget,
    ...(passThrough === null ? [] : [passThrough.mortgage_debits, passThrough.uncovered_mortgage]),
    ...paid.mortgageAmounts,
    ...paid.recurringAmounts,
    ...scheduled.map((item) => item.amount),
  ]);
  const spentUnits = decimalUnits(summary.spent, scale);
  const budgetUnits = decimalUnits(pace.budget, scale);
  const paidMortgageUnits = paid.mortgageAmounts.reduce((total, amount) => total + decimalUnits(amount, scale), 0n);
  let paidScheduledSubtraction = paidMortgageUnits;
  if (passThrough !== null) {
    // mortgage_debits stands in for the pass-through schedule's share; uncovered_mortgage is the part in spent.
    const debitsUnits = clampBigInt(decimalUnits(passThrough.mortgage_debits, scale), 0n, paidMortgageUnits);
    const uncoveredUnits = clampBigInt(decimalUnits(passThrough.uncovered_mortgage, scale), 0n, paidMortgageUnits);
    paidScheduledSubtraction = clampBigInt(paidMortgageUnits - debitsUnits + uncoveredUnits, 0n, paidMortgageUnits);
  }
  const paidRecurringUnits = paid.recurringAmounts.reduce((total, amount) => total + decimalUnits(amount, scale), 0n);
  const paidUnits = paidScheduledSubtraction + paidRecurringUnits;
  const discretionaryUnits = spentUnits > paidUnits ? spentUnits - paidUnits : 0n;
  const denominator = BigInt(cutoffDay);
  const itemsByDay = new Map<number, ScheduledPaceItem[]>();
  for (const item of scheduled) {
    const items = itemsByDay.get(item.stepDay) ?? [];
    items.push(item);
    itemsByDay.set(item.stepDay, items);
  }

  const points: MonthPacePoint[] = [];
  let scheduledUnits = 0n;
  if (spentUnits > 0n || scheduled.length > 0) {
    for (let day = cutoffDay; day <= daysInMonth; day += 1) {
      const items = itemsByDay.get(day) ?? [];
      const beforeUnits = spentUnits * denominator + discretionaryUnits * BigInt(day - cutoffDay)
        + scheduledUnits * denominator;
      for (const item of items) scheduledUnits += decimalUnits(item.amount, scale);
      const valueUnits = beforeUnits + items.reduce((total, item) => total + decimalUnits(item.amount, scale), 0n)
        * denominator;
      points.push({
        day,
        date: `${budget.period}-${String(day).padStart(2, "0")}`,
        before: ratio(beforeUnits, scale, cutoffDay),
        value: ratio(valueUnits, scale, cutoffDay),
        items,
      });
    }
  }

  const projected = points.at(-1)?.value ?? null;
  const projectedUnits = points.length === 0 ? null : decimalUnits(points.at(-1)?.value.numerator ?? "0", scale);
  const projectedRemaining = projectedUnits === null ? null
    : ratio(budgetUnits * denominator - projectedUnits, scale, cutoffDay);
  const dailyRate = points.length === 0 ? null : ratio(discretionaryUnits, scale, cutoffDay);
  let status: AnalyticsMonthlyBudgetPace["status"];
  let exhaustionDay: number | null = null;
  if (paidUnits === 0n && scheduled.length === 0) {
    status = pace.status;
    exhaustionDay = pace.exhaustion_day;
  } else if (budgetUnits <= 0n) {
    status = "no_budget";
  } else if (spentUnits >= budgetUnits) {
    status = "already_reached";
    exhaustionDay = cutoffDay;
  } else if (points.length === 0) {
    status = "no_positive_pace";
  } else {
    const exhausted = points.find((point) => decimalUnits(point.value.numerator, scale) >= budgetUnits * denominator);
    status = exhausted === undefined ? "within_budget" : "runs_out";
    exhaustionDay = exhausted?.day ?? null;
  }
  return {
    status,
    dailyRate,
    projectedMonthSpend: projected,
    projectedRemaining,
    exhaustionDay,
    scheduledItems: scheduled,
    points,
  };
}
