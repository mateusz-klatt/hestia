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

  it("reads the Warsaw month from a 2-month forecast when UTC is still on the previous month", () => {
    const current = budget({
      period: "2026-12",
      current_month: "2026-12",
      start: "2026-12-01",
      end: "2026-12-31",
      cutoff: "2026-12-01",
      evaluation_date: "2026-12-01",
    });
    const data = scheduleData({
      forecast: {
        evaluation_date: "2026-11-30",
        start_month: "2026-11",
        end_month: "2026-12",
        horizon_months: 2,
        months: [
          { month: "2026-11", currencies: [], items: [] },
          {
            month: "2026-12",
            currencies: [],
            items: [forecastItem({ label: "December step", amount: "2000", expected_date: "2026-12-05", past_due: false })],
          },
        ],
      },
    });
    const projection = buildMonthPaceProjection(current, data);

    expect(projection?.scheduledItems.map((item) => item.label)).toEqual(["December step"]);
    expect(projection?.projectedMonthSpend).toEqual({ numerator: "5100", denominator: 1 });
  });

  it("ignores disabled and out-of-window mortgage schedules in paid amounts and hint matching", () => {
    const current = budget({
      cutoff: "2026-11-10",
      evaluation_date: "2026-11-10",
      summary: { start_budget: "10000", spent: "1000", remaining: "9000" },
    });
    const disabledPaid = mortgageSchedule({
      id: "mtg-disabled", amount: "500", due_day: 5, enabled: false, paid_months: ["2026-11"],
    });
    const futurePaid = mortgageSchedule({
      id: "mtg-future", amount: "300", due_day: 5, start_date: "2026-12-01", paid_months: ["2026-11"],
    });
    const endedPaid = mortgageSchedule({
      id: "mtg-ended", amount: "200", due_day: 5, end_date: "2026-10-31", paid_months: ["2026-11"],
    });
    const partialMonthSkipped = mortgageSchedule({
      id: "mtg-midstart", amount: "150", due_day: 10, start_date: "2026-11-15", paid_months: ["2026-11"],
    });
    const data = scheduleData({
      schedules: [disabledPaid, futurePaid, endedPaid, partialMonthSkipped],
    });
    expect(buildMonthPaceProjection(current, data)?.dailyRate).toEqual({ numerator: "1000", denominator: 10 });

    // Neither a disabled schedule nor an out-of-window schedule may capture a high-confidence recurring hint.
    const hintCapturedCheck = scheduleData({
      schedules: [
        mortgageSchedule({ id: "mtg-disabled-cap", amount: "400", due_day: 5, enabled: false }),
        mortgageSchedule({ id: "mtg-ended-cap", amount: "250", due_day: 5, end_date: "2026-10-31" }),
      ],
      hints: [
        recurringHint({
          label: "Streaming A", amount: "400", occurrence_count: 6,
          first_date: "2026-06-05", last_date: "2026-11-05", min_gap_days: 30, max_gap_days: 31,
        }),
        recurringHint({
          label: "Streaming B", amount: "250", occurrence_count: 6,
          first_date: "2026-06-05", last_date: "2026-11-05", min_gap_days: 30, max_gap_days: 31,
        }),
      ],
    });
    expect(buildMonthPaceProjection(current, hintCapturedCheck)?.dailyRate).toEqual({
      numerator: "350",
      denominator: 10,
    });
  });

  it("does not subtract the rent-covered pass_through.matched share from paid mortgage installments", () => {
    const current = budget({
      cutoff: "2026-11-10",
      evaluation_date: "2026-11-10",
      summary: { start_budget: "10000", spent: "4550", remaining: "5450" },
      pass_through: {
        rent_category_id: "cat-rent",
        mortgage_category_id: "cat-mortgage",
        rent_received: "2000",
        mortgage_debits: "6000",
        mortgage_refunds: "0",
        unapplied_mortgage_refunds: "0",
        matched: "2000",
        unapplied_rent: "0",
        uncovered_mortgage: "4000",
      },
    });
    const data = scheduleData({
      schedules: [mortgageSchedule({ id: "mtg-pt", amount: "6000", due_day: 5, paid_months: ["2026-11"] })],
      hints: [
        recurringHint({
          label: "Bank Mortgage", amount: "6000", occurrence_count: 6,
          first_date: "2026-06-05", last_date: "2026-11-05", min_gap_days: 30, max_gap_days: 31,
        }),
        recurringHint({
          label: "Fiber", amount: "50", occurrence_count: 6,
          first_date: "2026-06-05", last_date: "2026-11-05", min_gap_days: 30, max_gap_days: 31,
        }),
      ],
    });
    const projection = buildMonthPaceProjection(current, data);

    // spent = 4000 (uncovered mortgage) + 50 (Fiber) + 500 (discretionary) -> discretionary = 500.
    // projected = 4550 + 500/10 * 20 = 5550 = 55500 / 10.
    expect(projection?.dailyRate).toEqual({ numerator: "500", denominator: 10 });
    expect(projection?.projectedMonthSpend).toEqual({ numerator: "55500", denominator: 10 });

    // Fully rent-covered paid mortgage (matched === amount) still subtracts paid recurring items cleanly.
    const fullyCovered = budget({
      cutoff: "2026-11-10",
      evaluation_date: "2026-11-10",
      summary: { start_budget: "10000", spent: "550", remaining: "9450" },
      pass_through: {
        rent_category_id: "cat-rent",
        mortgage_category_id: "cat-mortgage",
        rent_received: "2000",
        mortgage_debits: "2000",
        mortgage_refunds: "0",
        unapplied_mortgage_refunds: "0",
        matched: "2000",
        unapplied_rent: "0",
        uncovered_mortgage: "0",
      },
    });
    const fullData = scheduleData({
      schedules: [mortgageSchedule({ id: "mtg-full", amount: "2000", due_day: 5, paid_months: ["2026-11"] })],
      hints: [
        recurringHint({
          label: "Fiber", amount: "50", occurrence_count: 6,
          first_date: "2026-06-05", last_date: "2026-11-05", min_gap_days: 30, max_gap_days: 31,
        }),
      ],
    });
    expect(buildMonthPaceProjection(fullyCovered, fullData)?.dailyRate).toEqual({ numerator: "500", denominator: 10 });

    // Unpaid mortgage with an unrelated pass_through.matched still preserves paid recurring subtraction.
    const unpaidWithMatch = budget({
      cutoff: "2026-11-10",
      evaluation_date: "2026-11-10",
      summary: { start_budget: "10000", spent: "550", remaining: "9450" },
      pass_through: {
        rent_category_id: "cat-rent",
        mortgage_category_id: "cat-mortgage",
        rent_received: "1000",
        mortgage_debits: "1000",
        mortgage_refunds: "0",
        unapplied_mortgage_refunds: "0",
        matched: "1000",
        unapplied_rent: "0",
        uncovered_mortgage: "0",
      },
    });
    const unpaidData = scheduleData({
      forecast: forecastWith([forecastItem({
        schedule_id: "mtg-unpaid", amount: "2000", expected_date: "2026-11-25", past_due: false,
      })]),
      schedules: [mortgageSchedule({ id: "mtg-unpaid", amount: "2000", due_day: 25, paid_months: [] })],
      hints: [
        recurringHint({
          label: "Fiber", amount: "50", occurrence_count: 6,
          first_date: "2026-06-05", last_date: "2026-11-05", min_gap_days: 30, max_gap_days: 31,
        }),
      ],
    });
    const unpaidProj = buildMonthPaceProjection(unpaidWithMatch, unpaidData);
    expect(unpaidProj?.dailyRate).toEqual({ numerator: "500", denominator: 10 });
    expect(unpaidProj?.scheduledItems).toHaveLength(1);
  });

  it("subtracts pass_through.uncovered_mortgage instead of installment minus matched when a mortgage refund is recognized", () => {
    const current = budget({
      cutoff: "2026-11-10",
      evaluation_date: "2026-11-10",
      summary: { start_budget: "10000", spent: "3500", remaining: "6500" },
      pass_through: {
        rent_category_id: "cat-rent",
        mortgage_category_id: "cat-mortgage",
        rent_received: "2000",
        mortgage_debits: "6000",
        mortgage_refunds: "1000",
        unapplied_mortgage_refunds: "0",
        matched: "2000",
        unapplied_rent: "0",
        uncovered_mortgage: "3000",
      },
    });
    const data = scheduleData({
      schedules: [mortgageSchedule({ id: "mtg-refund", amount: "6000", due_day: 5, paid_months: ["2026-11"] })],
      hints: [
        recurringHint({
          label: "Bank Mortgage", amount: "6000", occurrence_count: 6,
          first_date: "2026-06-05", last_date: "2026-11-05", min_gap_days: 30, max_gap_days: 31,
        }),
      ],
    });
    const projection = buildMonthPaceProjection(current, data);

    // spent = 3000 (uncovered_mortgage = 6000 - 2000 matched - 1000 refund) + 500 discretionary.
    // projected = 3500 + 500/10 * 20 = 4500 = 45000 / 10.
    expect(projection?.dailyRate).toEqual({ numerator: "500", denominator: 10 });
    expect(projection?.projectedMonthSpend).toEqual({ numerator: "45000", denominator: 10 });

    // Negative uncovered_mortgage is clamped at 0.
    const clamped = budget({
      cutoff: "2026-11-10",
      evaluation_date: "2026-11-10",
      summary: { start_budget: "10000", spent: "500", remaining: "9500" },
      pass_through: {
        rent_category_id: "cat-rent",
        mortgage_category_id: "cat-mortgage",
        rent_received: "2000",
        mortgage_debits: "2000",
        mortgage_refunds: "0",
        unapplied_mortgage_refunds: "0",
        matched: "2000",
        unapplied_rent: "0",
        uncovered_mortgage: "-100",
      },
    });
    const clampedProj = buildMonthPaceProjection(clamped, data);
    expect(clampedProj?.dailyRate).toEqual({ numerator: "500", denominator: 10 });
  });

  it("does not double-subtract a paid mortgage booked more than 5 days from its due date", () => {
    const current = budget({
      cutoff: "2026-11-15",
      evaluation_date: "2026-11-15",
      summary: { start_budget: "10000", spent: "3500", remaining: "6500" },
    });
    const data = scheduleData({
      forecast: forecastWith([forecastItem({
        schedule_id: "mtg-late-book", amount: "3000", expected_date: "2026-11-05", past_due: true,
      })]),
      schedules: [mortgageSchedule({ id: "mtg-late-book", amount: "3000", due_day: 5, paid_months: ["2026-11"] })],
      hints: [recurringHint({
        label: "Bank Mortgage", amount: "3000", occurrence_count: 6,
        first_date: "2026-06-12", last_date: "2026-11-12", min_gap_days: 30, max_gap_days: 31,
      })],
    });
    const projection = buildMonthPaceProjection(current, data);

    expect(projection?.scheduledItems).toEqual([]);
    expect(projection?.dailyRate).toEqual({ numerator: "500", denominator: 15 });
    expect(projection?.projectedMonthSpend).toEqual({ numerator: "60000", denominator: 15 });

    // A second distinct recurring hint of the same amount stays subtracted once.
    const twoHints = scheduleData({
      schedules: [mortgageSchedule({ id: "mtg-late-book", amount: "3000", due_day: 5, paid_months: ["2026-11"] })],
      hints: [
        recurringHint({
          label: "Bank Mortgage", amount: "3000", occurrence_count: 6,
          first_date: "2026-06-12", last_date: "2026-11-12", min_gap_days: 30, max_gap_days: 31,
        }),
        recurringHint({
          label: "Other Monthly", amount: "3000", occurrence_count: 6,
          first_date: "2026-06-14", last_date: "2026-11-14", min_gap_days: 30, max_gap_days: 31,
        }),
      ],
    });
    const twoHintsProj = buildMonthPaceProjection(
      budget({ cutoff: "2026-11-15", evaluation_date: "2026-11-15", summary: { start_budget: "10000", spent: "6500", remaining: "3500" } }),
      twoHints,
    );
    expect(twoHintsProj?.dailyRate).toEqual({ numerator: "500", denominator: 15 });
  });

  it("ignores recurring hints whose last_date is after evaluation_date", () => {
    const current = budget({
      cutoff: "2026-11-10",
      evaluation_date: "2026-11-10",
      summary: { start_budget: "10000", spent: "1000", remaining: "9000" },
    });
    const data = scheduleData({
      forecast: forecastWith([forecastItem({
        schedule_id: "mtg-upcoming", label: "Upcoming Mortgage", amount: "3000", expected_date: "2026-11-15", past_due: false,
      })]),
      schedules: [mortgageSchedule({ id: "mtg-upcoming", amount: "3000", due_day: 15, paid_months: [] })],
      hints: [
        recurringHint({
          label: "Future Mortgage Hint", amount: "3000", occurrence_count: 6,
          first_date: "2026-06-15", last_date: "2026-11-15", min_gap_days: 30, max_gap_days: 31,
        }),
        recurringHint({
          label: "Today Hint", amount: "200", occurrence_count: 6,
          first_date: "2026-06-10", last_date: "2026-11-10", min_gap_days: 30, max_gap_days: 31,
        }),
      ],
    });
    const projection = buildMonthPaceProjection(current, data);

    expect(projection?.scheduledItems.map((item) => item.label)).toEqual(["Upcoming Mortgage"]);
    expect(projection?.dailyRate).toEqual({ numerator: "800", denominator: 10 });
  });
});
