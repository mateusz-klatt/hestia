import { describe, expect, it } from "vitest";
import { forecastItem, forecastOf, monthlyBudgetOf, monthlyBudgetPace, mortgageSchedule, mortgagesOf,
  recurringHint, recurringOf } from "./fixtures";
import type { AnalyticsForecastItem, AnalyticsMonthlyBudget } from "./model";
import { buildMonthPaceProjection } from "./month_budget_pace";
import type { MonthPaceScheduleData } from "./month_budget_pace";

function budget(changes: Parameters<typeof monthlyBudgetOf>[0] = {}): AnalyticsMonthlyBudget {
  return monthlyBudgetOf({
    period: "2026-11",
    current_month: "2026-11",
    start: "2026-11-01",
    end: "2026-11-30",
    cutoff: "2026-11-10",
    evaluation_date: "2026-11-10",
    summary: { start_budget: "10000", spent: "100", remaining: "9900" },
    pace: monthlyBudgetPace({ budget: "10000" }),
    ...changes,
  });
}

function scheduleData(changes: {
  forecast?: Parameters<typeof forecastOf>[0];
  mortgages?: Parameters<typeof mortgagesOf>[2];
  schedules?: Parameters<typeof mortgagesOf>[0];
  recurring?: Parameters<typeof recurringOf>[1];
  hints?: Parameters<typeof recurringOf>[0];
} = {}): MonthPaceScheduleData {
  return {
    forecast: forecastOf(changes.forecast),
    mortgages: mortgagesOf(changes.schedules ?? [], [], changes.mortgages),
    recurring: recurringOf(changes.hints ?? [], changes.recurring),
  };
}

function forecastWith(items: AnalyticsForecastItem[], month = "2026-11"): MonthPaceScheduleData["forecast"] {
  return forecastOf({
    months: [{ month, currencies: [], items }],
  });
}

describe("buildMonthPaceProjection", () => {
  it("steps on a known due date independently of the discretionary trend", () => {
    const current = budget({
      period: "2026-12",
      current_month: "2026-12",
      start: "2026-12-01",
      end: "2026-12-31",
      cutoff: "2026-12-01",
      evaluation_date: "2026-12-01",
    });
    const data = scheduleData({
      forecast: forecastWith([forecastItem({
        label: "Kredyt hipoteczny – mieszkanie",
        amount: "6286.90",
        expected_date: "2026-12-05",
        past_due: false,
      })], "2026-12"),
    });
    const projection = buildMonthPaceProjection(current, data);

    expect(projection?.dailyRate).toEqual({ numerator: "100", denominator: 1 });
    expect(projection?.points.find((point) => point.day === 4)?.value).toEqual({ numerator: "400", denominator: 1 });
    expect(projection?.points.find((point) => point.day === 5)?.before).toEqual({ numerator: "500", denominator: 1 });
    expect(projection?.points.find((point) => point.day === 5)?.value).toEqual({ numerator: "6786.9", denominator: 1 });
    expect(projection?.points.find((point) => point.day === 5)?.items[0]).toMatchObject({
      label: "Kredyt hipoteczny – mieszkanie", expectedDate: "2026-12-05", stepDay: 5, overdue: false,
    });
    expect(projection?.projectedMonthSpend).toEqual({ numerator: "9386.9", denominator: 1 });
    expect(projection?.projectedRemaining).toEqual({ numerator: "613.1", denominator: 1 });
  });

  it("excludes paid scheduled items from the trend and does not step them again", () => {
    const current = budget({
      cutoff: "2026-11-06",
      evaluation_date: "2026-11-06",
      summary: { start_budget: "10000", spent: "6500", remaining: "3500" },
    });
    const schedule = mortgageSchedule({
      id: "mtg-paid", amount: "6000", due_day: 5, paid_months: ["2026-11"],
    });
    const data: MonthPaceScheduleData = {
      forecast: forecastWith([forecastItem({
        schedule_id: "mtg-paid", amount: "6000", expected_date: "2026-11-05",
      })]),
      mortgages: mortgagesOf([schedule]),
      recurring: recurringOf([recurringHint({
        label: "Synthetic mortgage observation", amount: "6000", occurrence_count: 6,
        first_date: "2026-06-05", last_date: "2026-11-05", min_gap_days: 30, max_gap_days: 31,
      })]),
    };
    const projection = buildMonthPaceProjection(current, data);

    expect(projection?.dailyRate).toEqual({ numerator: "500", denominator: 6 });
    expect(projection?.scheduledItems).toEqual([]);
    expect(projection?.projectedMonthSpend).toEqual({ numerator: "51000", denominator: 6 });
    expect(projection?.projectedRemaining).toEqual({ numerator: "9000", denominator: 6 });
  });

  it("subtracts a paid high-confidence recurring item once", () => {
    const current = budget({
      cutoff: "2026-11-06",
      evaluation_date: "2026-11-06",
      summary: { start_budget: "10000", spent: "650", remaining: "9350" },
    });
    const data = scheduleData({
      hints: [recurringHint({
        amount: "600", occurrence_count: 6, first_date: "2026-06-05", last_date: "2026-11-05",
        min_gap_days: 30, max_gap_days: 31,
      })],
    });
    const projection = buildMonthPaceProjection(current, data);

    expect(projection?.dailyRate).toEqual({ numerator: "50", denominator: 6 });
    expect(projection?.projectedMonthSpend).toEqual({ numerator: "5100", denominator: 6 });
  });

  it("steps an overdue item today and marks it overdue", () => {
    const current = budget({
      cutoff: "2026-11-22",
      evaluation_date: "2026-11-22",
    });
    const data = scheduleData({
      forecast: forecastWith([forecastItem({
        source: "recurring", schedule_id: null, confidence: "high", label: "Synthetic subscription",
        amount: "50", expected_date: "2026-11-05", past_due: false,
      })]),
    });
    const projection = buildMonthPaceProjection(current, data);

    expect(projection?.points[0]?.day).toBe(22);
    expect(projection?.points[0]?.before).toEqual({ numerator: "2200", denominator: 22 });
    expect(projection?.points[0]?.value).toEqual({ numerator: "3300", denominator: 22 });
    expect(projection?.points[0]?.items[0]).toMatchObject({ expectedDate: "2026-11-05", stepDay: 22, overdue: true });
    expect(projection?.projectedMonthSpend).toEqual({ numerator: "4100", denominator: 22 });
  });

  it("falls back to the pure calendar-day trend for an empty schedule", () => {
    const projection = buildMonthPaceProjection(budget(), scheduleData());

    expect(projection?.dailyRate).toEqual({ numerator: "100", denominator: 10 });
    expect(projection?.points[0]?.value).toEqual({ numerator: "1000", denominator: 10 });
    expect(projection?.points.at(-1)?.value).toEqual({ numerator: "3000", denominator: 10 });
    expect(projection?.projectedMonthSpend).toEqual({ numerator: "3000", denominator: 10 });
    expect(projection?.projectedRemaining).toEqual({ numerator: "97000", denominator: 10 });
    expect(projection?.scheduledItems).toEqual([]);
  });

  it("keeps PLN and EUR schedules and paid items separate", () => {
    const data: MonthPaceScheduleData = {
      forecast: forecastWith([
        forecastItem({ label: "PLN step", amount: "100", expected_date: "2026-11-20" }),
        forecastItem({ label: "EUR step", amount: "500", currency: "EUR", expected_date: "2026-11-20" }),
      ]),
      mortgages: mortgagesOf([mortgageSchedule({
        id: "eur-paid", currency: "EUR", amount: "9000", paid_months: ["2026-11"],
      })]),
      recurring: recurringOf([recurringHint({
        currency: "EUR", amount: "7000", occurrence_count: 6, last_date: "2026-11-05",
        min_gap_days: 30, max_gap_days: 31,
      })]),
    };
    const pln = buildMonthPaceProjection(budget(), data);
    const eur = buildMonthPaceProjection(budget({
      currency: "EUR",
      summary: { start_budget: "10000", spent: "20000", remaining: "-10000" },
    }), data);

    expect(pln?.scheduledItems.map((item) => item.label)).toEqual(["PLN step"]);
    expect(pln?.dailyRate).toEqual({ numerator: "100", denominator: 10 });
    expect(eur?.scheduledItems.map((item) => item.label)).toEqual(["EUR step"]);
    expect(eur?.dailyRate).toEqual({ numerator: "4000", denominator: 10 });
  });

  it("leaves medium recurring and category-baseline estimates in the trend", () => {
    const medium = forecastItem({
      source: "recurring", schedule_id: null, confidence: "medium", amount: "200", expected_date: "2026-11-20",
    });
    const baseline = {
      ...forecastItem({ amount: "300", expected_date: "2026-11-21" }),
      source: "category_baseline",
    } as unknown as AnalyticsForecastItem;
    const projection = buildMonthPaceProjection(budget(), scheduleData({
      forecast: forecastWith([medium, baseline]),
    }));

    expect(projection?.scheduledItems).toEqual([]);
    expect(projection?.projectedMonthSpend).toEqual({ numerator: "3000", denominator: 10 });
  });
});
