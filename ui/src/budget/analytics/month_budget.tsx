// Read-only owner-private monthly budget dashboard and charts (P1 & P2, OpenAPI 0.29.0).
// Evaluates in Europe/Warsaw from one snapshot on the server; all amounts stay decimal strings end to end,
// ratios divide via BigInt half-even rounding, and no raw IDs or remittance text ever enter the DOM.
import { useEffect, useRef, useState } from "react";
import { t, tPlural } from "../../i18n";
import type { BudgetApi, BudgetResult } from "../api";
import { formatAmount, when } from "../model";
import type { BudgetAccount } from "../model";
import { CategorySwatch } from "./category_badge";
import { buildCategoryColors, oneOffCategoryColor } from "./category_colors";
import type { CategoryColorResolver } from "./category_colors";
import { groupCategoryTotals, hasCategoryParents, visibleCategoryRows } from "./category_tree";
import { MONTH_BUDGET_CARD_STATUS_KEYS, MONTH_BUDGET_LIMIT_STATUS_KEYS, clampPercentString, formatRatioHalfEven,
  isPositiveDecimal, isZero, labelOf, monthLabel, monthlyBudgetCurrencies, oneOffRowKey, ratioPercentFloor,
  shiftMonth, validCurrency } from "./model";
import type { AnalyticsCategory, AnalyticsMonthlyBudget, AnalyticsMonthlyBudgetCardDebt,
  AnalyticsMonthlyBudgetCharts, AnalyticsMonthlyBudgetExpectedIncome, AnalyticsMonthlyBudgetIncomeWindow,
  AnalyticsMonthlyBudgetPace, AnalyticsMonthlyBudgetPassThrough, AnalyticsMonthlyBudgetSeries } from "./model";
import { MonthBudgetCharts } from "./month_budget_charts";
import { buildMonthPaceProjection } from "./month_budget_pace";
import type { MonthPaceProjection, MonthPaceScheduleData } from "./month_budget_pace";

type Shown =
  | { state: "loading" | "badRequest" | "limit" | "error" }
  | { state: "ready"; data: AnalyticsMonthlyBudgetCharts };

export interface OpenMonthOptions {
  currency?: string;
  provisionalKeys?: readonly string[];
  provisionalTotal?: number;
  provisionalTruncated?: boolean;
}

type PaceScheduleState =
  | { state: "idle" }
  | { state: "loading" }
  | { state: "error" }
  | { state: "ready"; data: MonthPaceScheduleData };


export interface MonthBudgetViewProps {
  api: Pick<BudgetApi, "monthlyBudgetCharts" | "categories" | "forecast" | "mortgages" | "recurring">;
  accounts: readonly BudgetAccount[];
  locale: string;
  revision: number;
  onAccessLoss: (result: { ok: boolean; status?: number; error?: string }) => boolean;
  onOpenMonth: (month: string, serverCurrentMonth: string, options?: OpenMonthOptions) => void;
}

function incomeWindowLabel(incomeWindow: AnalyticsMonthlyBudgetIncomeWindow, locale: string): string {
  const monthFormatter = new Intl.DateTimeFormat(locale, { month: "short", timeZone: "UTC" });
  const formatDay = (value: string) => {
    const parsed = new Date(`${value}T00:00:00Z`);
    return `${String(parsed.getUTCDate())} ${monthFormatter.format(parsed)}`;
  };
  return `${formatDay(incomeWindow.start)} – ${formatDay(incomeWindow.end)}`;
}

function paceSentence(pace: AnalyticsMonthlyBudgetPace, projection: MonthPaceProjection | null): string {
  const status = projection?.status ?? pace.status;
  const exhaustionDay = projection === null ? pace.exhaustion_day : projection.exhaustionDay;
  switch (status) {
    case "runs_out": return exhaustionDay === null
      ? t("budget.monthBudget.paceRunsOutBeforeEnd")
      : t("budget.monthBudget.paceRunsOutDay", { day: exhaustionDay });
    case "within_budget": return t("budget.monthBudget.paceWithinBudget");
    case "already_reached": return exhaustionDay === null
      ? t("budget.monthBudget.paceAlreadyReached")
      : t("budget.monthBudget.paceAlreadyReachedDay", { day: exhaustionDay });
    case "no_budget": return t("budget.monthBudget.paceNoBudget");
    case "no_positive_pace": return t("budget.monthBudget.paceNoPositivePace");
    case "historical": return t("budget.monthBudget.paceHistorical");
  }
}

function ExpectedIncomeCard({ income, currency, evaluationDate, categoryMap, categoryColors, locale }: {
  income: AnalyticsMonthlyBudgetExpectedIncome;
  currency: string;
  evaluationDate: string;
  categoryMap: ReadonlyMap<string, string>;
  categoryColors: CategoryColorResolver;
  locale: string;
}) {
  const money = (amount: string) => formatAmount(amount, currency, locale);
  const title = t("budget.monthBudget.expectedIncomeTitle");
  const totalLabel = t("budget.monthBudget.expectedIncomeTotal");
  const fallbackCategory = t("budget.monthBudget.incomeCategoryFallback");
  const previousMonthLabel = t("budget.monthBudget.expectedSourcePreviousMonth");
  const delayedLabel = t("budget.monthBudget.expectedDelayed");

  return (
    <article className="budget-total" aria-label={title}>
      <div className="budget-limit-main">
        <h4>{title}</h4>
        {income.status === "partial" ? <span className="budget-chip budget-warn">{t("budget.monthBudget.partialIncome")}</span> : null}
      </div>
      {income.status === "historical" ? (
        <p className="budget-muted">{t("budget.monthBudget.expectedIncomeHistorical")}</p>
      ) : income.status === "unavailable" ? (
        <p className="budget-muted">{t("budget.monthBudget.expectedIncomeUnavailable")}</p>
      ) : income.status === "none" ? (
        <>
          <dl>
            <dt>{totalLabel}</dt>
            <dd>{money(income.total ?? "0")}</dd>
          </dl>
          <p className="budget-muted">{t("budget.monthBudget.expectedIncomeNone")}</p>
        </>
      ) : (
        <>
          {income.total !== null ? (
            <dl>
              <dt>{totalLabel}</dt>
              <dd><strong>{money(income.total)}</strong></dd>
            </dl>
          ) : (
            <p className="budget-muted">{t("budget.monthBudget.partialIncomeMissing")}</p>
          )}
          {income.items.length === 0 ? null : (
            <ul className="budget-entry-list">
              {income.items.map((item, index) => {
                const delayed = item.expected_date !== null && item.expected_date < evaluationDate;
                return (
                  <li key={`${String(index)}-${item.expected_date ?? "none"}`} className="budget-entry">
                    <div className="budget-recurring-main">
                      <span className="budget-entry-text">
                        <CategorySwatch color={categoryColors.get(item.category_id)} />
                        {categoryMap.get(item.category_id) ?? fallbackCategory}
                      </span>
                      <strong className="budget-recurring-amount">{money(item.amount)}</strong>
                    </div>
                    <div className="budget-entry-facts">
                      <span>
                        {item.expected_date === null
                          ? t("budget.monthBudget.expectedDateEmpty")
                          : t("budget.monthBudget.expectedDateOn", { date: item.expected_date })}
                      </span>
                      <span className="budget-chip">{previousMonthLabel}</span>
                      {delayed ? <span className="budget-chip budget-warn">{delayedLabel}</span> : null}
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </>
      )}
    </article>
  );
}

function PaceCard({ pace, projection, schedulePending, scheduleError, currency, provisional, partialIncome, locale }: {
  pace: AnalyticsMonthlyBudgetPace;
  projection: MonthPaceProjection | null;
  schedulePending: boolean;
  scheduleError: boolean;
  currency: string;
  provisional: boolean;
  partialIncome: boolean;
  locale: string;
}) {
  const dailyRate = projection === null ? pace.daily_rate : projection.dailyRate;
  const projectedSpend = projection === null ? pace.projected_month_spend : projection.projectedMonthSpend;
  const title = t("budget.monthBudget.paceTitle");
  const paceBudgetLabel = t("budget.monthBudget.paceBudget");

  return (
    <article className="budget-total" aria-label={title}>
      <div className="budget-limit-main">
        <h4>{title}</h4>
        <div className="budget-actions">
          {provisional ? <span className="budget-chip budget-warn">{t("budget.monthBudget.provisional")}</span> : null}
          {partialIncome ? <span className="budget-chip budget-warn">{t("budget.monthBudget.partialIncome")}</span> : null}
        </div>
      </div>
      <p className="budget-muted">
        {pace.basis === "received_plus_expected"
          ? t("budget.monthBudget.paceBasisReceivedPlusExpected")
          : t("budget.monthBudget.paceBasisReceived")}
      </p>
      {schedulePending ? (
        <>
          <p className="budget-muted" role="status">{t("budget.monthBudget.scheduledPaymentsLoading")}</p>
          <dl>
            <dt>{paceBudgetLabel}</dt>
            <dd>{formatAmount(pace.budget, currency, locale)}</dd>
          </dl>
        </>
      ) : (
        <>
          <p>{paceSentence(pace, projection)}</p>
          {scheduleError ? <p className="budget-notice" role="note">{t("budget.monthBudget.scheduledPaymentsError")}</p> : null}
          <dl>
            <dt>{paceBudgetLabel}</dt>
            <dd>{formatAmount(pace.budget, currency, locale)}</dd>
            {dailyRate === null ? null : (
              <>
                <dt>{t("budget.monthBudget.dailyRate")}</dt>
                <dd>{t("budget.monthBudget.perDay", { amount: formatRatioHalfEven(dailyRate, currency, locale) })}</dd>
              </>
            )}
            {projectedSpend === null ? null : (
              <>
                <dt>{t("budget.monthBudget.projectedSpend")}</dt>
                <dd>{formatRatioHalfEven(projectedSpend, currency, locale)}</dd>
              </>
            )}
          </dl>
        </>
      )}
    </article>
  );
}

function PassThroughCard({ passThrough, currency, categoryMap, locale }: {
  passThrough: AnalyticsMonthlyBudgetPassThrough;
  currency: string;
  categoryMap: ReadonlyMap<string, string>;
  locale: string;
}) {
  const money = (amount: string) => formatAmount(amount, currency, locale);
  const rentName = categoryMap.get(passThrough.rent_category_id) ?? t("budget.monthBudget.rentLabel");
  const mortgageName = categoryMap.get(passThrough.mortgage_category_id) ?? t("budget.monthBudget.mortgageLabel");
  const title = t("budget.monthBudget.passThroughTitle");

  return (
    <article className="budget-total" aria-label={title}>
      <h4>{title}</h4>
      <p className="budget-muted">{rentName} → {mortgageName}</p>
      <dl>
        <dt>{t("budget.monthBudget.rentReceived")}</dt>
        <dd>{money(passThrough.rent_received)}</dd>
        <dt>{t("budget.monthBudget.mortgageDebits")}</dt>
        <dd>{money(passThrough.mortgage_debits)}</dd>
        {isZero(passThrough.mortgage_refunds) ? null : (
          <>
            <dt>{t("budget.monthBudget.mortgageRefunds")}</dt>
            <dd>{money(passThrough.mortgage_refunds)}</dd>
          </>
        )}
        <dt>{t("budget.monthBudget.matchedRent")}</dt>
        <dd>{money(passThrough.matched)}</dd>
        <dt>{t("budget.monthBudget.unappliedRent")}</dt>
        <dd>{money(passThrough.unapplied_rent)}</dd>
        {isZero(passThrough.unapplied_mortgage_refunds) ? null : (
          <>
            <dt>{t("budget.monthBudget.unappliedMortgageRefunds")}</dt>
            <dd>{money(passThrough.unapplied_mortgage_refunds)}</dd>
          </>
        )}
        <dt><strong>{t("budget.monthBudget.uncoveredMortgage")}</strong></dt>
        <dd><strong>{money(passThrough.uncovered_mortgage)}</strong></dd>
      </dl>
      <p className="budget-muted">{t("budget.monthBudget.passThroughNote")}</p>
    </article>
  );
}

type CategorySpend = AnalyticsMonthlyBudget["category_spend"][number];
type LoadedCategories = "pending" | "error" | readonly AnalyticsCategory[];

function isCategoryList(value: LoadedCategories): value is readonly AnalyticsCategory[] {
  return Array.isArray(value);
}

function shownCategorySpend(
  rows: readonly CategorySpend[],
  categories: readonly AnalyticsCategory[],
  locale: string,
  collapsed: ReadonlySet<string>,
): {
  key: string; category_id: string | null; name: string | null; archived: boolean; depth: number;
  has_children: boolean; expenses: string; refunds: string; net_spending: string;
  limit: CategorySpend["limit"]; tree: boolean; one_off: boolean;
}[] {
  const oneOffs = rows.filter((row) => row.one_off === true).map((row, index) => ({
    key: oneOffRowKey(row, index),
    category_id: row.category_id, name: row.name, archived: row.archived, depth: 0, has_children: false,
    expenses: row.expenses, refunds: row.refunds, net_spending: row.net_spending, limit: null,
    tree: false, one_off: true,
  }));
  const regular = rows.filter((row) => row.one_off !== true);
  if (!hasCategoryParents(categories)) {
    return [
      ...regular.map((row, index) => ({
        key: oneOffRowKey(row, index),
        category_id: row.category_id, name: row.name, archived: row.archived, depth: 0, has_children: false,
        expenses: row.expenses, refunds: row.refunds, net_spending: row.net_spending, limit: row.limit,
        tree: false, one_off: false,
      })),
      ...oneOffs,
    ];
  }
  const limits = new Map(regular.map((row) => [row.category_id, row.limit]));
  const treeRows = visibleCategoryRows(groupCategoryTotals(regular, categories, locale), collapsed).map((row) => ({
    key: row.category_id ?? "uncategorised",
    category_id: row.category_id, name: row.name, archived: row.archived, depth: row.depth,
    has_children: row.has_children, expenses: row.expenses, refunds: row.refunds, net_spending: row.net_spending,
    limit: limits.get(row.category_id) ?? null, tree: true, one_off: false,
  }));
  return [...treeRows, ...oneOffs];
}

export function MonthBudgetView({ api, accounts, locale, revision, onAccessLoss, onOpenMonth }: MonthBudgetViewProps) {
  const [period, setPeriod] = useState<string | undefined>(undefined);
  const [currency, setCurrency] = useState("PLN");
  const [history, setHistory] = useState<6 | 12>(6);
  const [discoveredCurrencies, setDiscoveredCurrencies] = useState<string[]>([]);
  const [serverCurrentMonth, setServerCurrentMonth] = useState<string | null>(null);
  const [manualCode, setManualCode] = useState("");
  const [shown, setShown] = useState<Shown>({ state: "loading" });
  const [paceSchedule, setPaceSchedule] = useState<PaceScheduleState>({ state: "idle" });
  const [categories, setCategories] = useState<LoadedCategories>("pending");
  const mounted = useRef(true);
  const generation = useRef(0);
  const revisionRef = useRef(revision);
  const categoriesLoadedRev = useRef(-1);
  const categoriesRequest = useRef(0);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    const token = ++generation.current;
    const prevRevision = revisionRef.current;
    revisionRef.current = revision;
    if (prevRevision !== revision) {
      categoriesLoadedRev.current = -1;
      setCategories("pending");
    }
    setShown({ state: "loading" });
    setPaceSchedule({ state: "idle" });
    void api.monthlyBudgetCharts(period, currency, history).then((result) => {
      // Run the access barrier before dropping superseded answers so a 401/403/disabled always clears the view.
      if (onAccessLoss(result) || token !== generation.current) return;
      if (!result.ok) {
        const state = result.status === 400 ? "badRequest" : result.status === 413 ? "limit" : "error";
        setShown({ state });
        return;
      }
      const { budget } = result.data;
      if (period !== undefined && budget.period !== period) {
        setShown({ state: "error" });
        return;
      }
      setServerCurrentMonth(budget.current_month);
      setDiscoveredCurrencies((previous) => monthlyBudgetCurrencies([...budget.available_currencies, ...previous]));
      setShown({ state: "ready", data: result.data });
      if (budget.period === budget.current_month && budget.summary !== null && budget.pace !== null
        && budget.pace.status !== "historical") {
        setPaceSchedule({ state: "loading" });
        let accessLost = false;
        // Pass each response through the access barrier immediately so a slow sibling cannot delay clearing.
        const watch = <T,>(request: Promise<BudgetResult<T>>) => request.then((response) => {
          if (onAccessLoss(response)) {
            accessLost = true;
          } else if (!response.ok && token === generation.current && mounted.current && !accessLost) {
            setPaceSchedule({ state: "error" });
          }
          return response;
        });
        void Promise.all([
          watch(api.forecast(2, budget.currency)),
          watch(api.mortgages()),
          watch(api.recurring(budget.currency)),
        ]).then(([forecastResult, mortgagesResult, recurringResult]) => {
          if (accessLost || token !== generation.current || !mounted.current) return;
          if (forecastResult.ok && mortgagesResult.ok && recurringResult.ok) {
            setPaceSchedule({
              state: "ready",
              data: {
                forecast: forecastResult.data,
                mortgages: mortgagesResult.data,
                recurring: recurringResult.data,
              },
            });
          } else {
            setPaceSchedule({ state: "error" });
          }
        });
      }
      // Parent limits already include the subtree. Drawing the table or the charts before the
      // category list arrives would pair that rolled limit with direct spend, or plot leaves as roots.
      if (budget.configuration.status === "ready" && categoriesLoadedRev.current !== revision) {
        const request = ++categoriesRequest.current;
        setCategories("pending");
        void api.categories().then((catResult) => {
          if (onAccessLoss(catResult) || !mounted.current || revision !== revisionRef.current) return;
          if (request !== categoriesRequest.current) return;
          if (!catResult.ok) {
            setCategories("error");
            return;
          }
          categoriesLoadedRev.current = revision;
          setCategories(catResult.data.categories);
        });
      }
    });
    return () => { generation.current += 1; };
  }, [api, period, currency, history, revision, onAccessLoss]);

  const readyPayload = shown.state === "ready" ? shown.data : null;
  const data = readyPayload?.budget ?? null;
  const series = readyPayload?.series ?? null;
  const nav = serverCurrentMonth === null
    ? null
    : { current: serverCurrentMonth, active: data === null ? (period ?? serverCurrentMonth) : data.period };
  const currencies = monthlyBudgetCurrencies(
    [...(data?.available_currencies ?? []), ...discoveredCurrencies, currency],
    accounts,
  );
  const categoryMap = new Map(isCategoryList(categories) ? categories.map((item) => [item.id, item.name]) : []);

  const title = t("budget.monthBudget.title");
  return (
    <section className="budget-month budget-month-dashboard" aria-label={title}>
      <div className="budget-limit-main">
        <h4>{title}</h4>
        <span className="budget-chip">{t("budget.monthBudget.readOnly")}</span>
      </div>
      <div className="budget-month-dashboard-controls">
        {nav === null ? null : (
          <nav className="budget-month-nav" aria-label={t("budget.monthBudget.month")}>
            <button type="button" aria-label={t("budget.monthBudget.previousMonth")}
              onClick={() => { setPeriod(shiftMonth(nav.active, -1)); }}>◀</button>
            <h3>{monthLabel(nav.active, locale)}</h3>
            <button type="button" aria-label={t("budget.monthBudget.nextMonth")} disabled={nav.active >= nav.current}
              onClick={() => { setPeriod(shiftMonth(nav.active, 1)); }}>▶</button>
          </nav>
        )}
        <div className="budget-range">
          <label>{t("budget.monthBudget.currency")}
            <select name="currency" value={currency} onChange={(event) => { setCurrency(event.currentTarget.value); }}>
              {currencies.map((code) => <option key={code} value={code}>{code}</option>)}
            </select>
          </label>
          <label>{t("budget.monthBudget.chartHistory")}
            <select name="history" value={String(history)} onChange={(event) => {
              setHistory(event.currentTarget.value === "12" ? 12 : 6);
            }}>
              <option value="6">{tPlural("budget.monthBudget.historyMonths", 6)}</option>
              <option value="12">{tPlural("budget.monthBudget.historyMonths", 12)}</option>
            </select>
          </label>
        </div>
      </div>

      {shown.state === "loading" ? <p className="budget-muted" role="status">{t("budget.monthBudget.loading")}</p> : null}
      {shown.state === "badRequest" ? <p className="budget-notice" role="alert">{t("budget.monthBudget.badRequest")}</p> : null}
      {shown.state === "limit" ? (
        <>
          <p className="budget-notice" role="alert">{t("budget.monthBudget.limit")}</p>
          <form className="budget-rule-editor" onSubmit={(event) => {
            event.preventDefault();
            if (validCurrency(manualCode)) setCurrency(manualCode.trim().toUpperCase());
          }}>
            <label>{t("budget.monthBudget.currencyCode")}
              <input type="text" name="manual_currency" autoComplete="off" maxLength={3} value={manualCode}
                onChange={(event) => { setManualCode(event.currentTarget.value); }} />
            </label>
            <button type="submit" disabled={!validCurrency(manualCode)}>{t("budget.monthBudget.applyCurrency")}</button>
          </form>
        </>
      ) : null}
      {shown.state === "error" ? <p className="budget-notice" role="alert">{t("budget.monthBudget.error")}</p> : null}

      {data === null ? null : (
        <ReadyDashboard data={data} series={series} paceSchedule={paceSchedule} categoryMap={categoryMap}
          categories={categories} locale={locale} onOpenMonth={onOpenMonth} />
      )}
    </section>
  );
}

function SummaryMeter({ spent, budget, label }: { spent: string; budget: string; label: string }) {
  const percentFloor = ratioPercentFloor(spent, budget);
  return (
    <div className="budget-limit-meter">
      <progress value={clampPercentString(percentFloor)} max={100} aria-label={label} />
      <span className="budget-muted">{label}: {percentFloor}%</span>
    </div>
  );
}

function CardDebtCard({ cardDebt, locale }: {
  cardDebt: AnalyticsMonthlyBudgetCardDebt;
  locale: string;
}) {
  const cardExposureTitle = t("budget.monthBudget.cardExposureTitle");
  return (
    <article className="budget-total" aria-label={cardExposureTitle}>
      <h4>{cardExposureTitle}</h4>
      <dl>
        <dt>{cardExposureTitle}</dt>
        <dd>
          <strong>
            {cardDebt.amount === null
              ? labelOf(MONTH_BUDGET_CARD_STATUS_KEYS, cardDebt.status)
              : formatAmount(cardDebt.amount, cardDebt.currency, locale)}
          </strong>
          {cardDebt.amount !== null && cardDebt.status === "partial" ? (
            <span className="budget-muted">
              {" · "}
              {labelOf(MONTH_BUDGET_CARD_STATUS_KEYS, cardDebt.status)}
            </span>
          ) : null}
        </dd>
        {cardDebt.as_of_date !== null ? (
          <>
            <dt>{t("budget.monthBudget.cardExposureAsOfLabel")}</dt>
            <dd>{cardDebt.as_of_date}</dd>
          </>
        ) : null}
      </dl>
      <p className="budget-muted">
        {cardDebt.status === "available"
          ? t("budget.monthBudget.cardExposureCalculatedNote")
          : cardDebt.status === "partial"
            ? t("budget.monthBudget.cardExposurePartialNote")
            : t("budget.monthBudget.cardExposureNote")}
      </p>
    </article>
  );
}

function ReadyDashboard({ data, series, paceSchedule, categoryMap, categories, locale, onOpenMonth }: {
  data: AnalyticsMonthlyBudget;
  series: AnalyticsMonthlyBudgetSeries | null;
  paceSchedule: PaceScheduleState;
  categoryMap: ReadonlyMap<string, string>;
  categories: LoadedCategories;
  locale: string;
  onOpenMonth: (month: string, serverCurrentMonth: string, options?: OpenMonthOptions) => void;
}) {
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(() => new Set());
  const toggleCategory = (id: string) => {
    setCollapsed((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };
  const money = (amount: string) => formatAmount(amount, data.currency, locale);
  const ledgerStamp = when(data.coverage.ledger_as_of, locale);
  const isCurrent = data.period === data.current_month;
  const schedulePending = isCurrent && paceSchedule.state === "loading";
  const scheduleError = isCurrent && paceSchedule.state === "error";
  const paceProjection = schedulePending
    ? null
    : buildMonthPaceProjection(data, paceSchedule.state === "ready" ? paceSchedule.data : undefined);
  const projectedRemaining = isCurrent ? paceProjection?.projectedRemaining ?? null : null;
  const categoryColors = buildCategoryColors(isCategoryList(categories) ? categories : []);
  const viewTransactionsText = t("budget.monthBudget.viewTransactions");
  const provisionalText = t("budget.monthBudget.provisional");
  const summaryTitle = t("budget.monthBudget.summaryTitle");
  const spendingAndLimitsTitle = t("budget.monthBudget.spendingAndLimits");
  const cardDebt: AnalyticsMonthlyBudgetCardDebt = data.card_debt ?? {
    status: data.card_exposure.status,
    amount: data.card_exposure.amount,
    currency: data.currency,
    as_of_date: data.card_exposure.as_of_date,
  };
  const uncategorisedLabel = t("budget.monthBudget.uncategorised");
  const unknownCategoryLabel = t("budget.monthBudget.unknownCategory");
  const archivedText = t("budget.monthBudget.archived");
  const oneOffTagText = t("budget.monthBudget.oneOffTag");
  const noLimitText = t("budget.monthBudget.noLimit");

  return (
    <>
      <ul className="budget-facts">
        <li>{t("budget.monthBudget.evaluatedFor", { date: data.evaluation_date, timezone: data.evaluation_timezone })}</li>
        <li>
          {ledgerStamp === null
            ? t("budget.monthBudget.ledgerAsOfEmpty")
            : t("budget.monthBudget.ledgerAsOfDate", { date: ledgerStamp })}
        </li>
      </ul>
      {data.coverage.stale_selected_accounts > 0 ? (
        <p className="budget-notice" role="note">
          {t("budget.monthBudget.staleData")} · {tPlural("budget.monthBudget.staleAccounts", data.coverage.stale_selected_accounts)}
        </p>
      ) : null}
      <div className="budget-month-basis">
        <span>{t("budget.monthBudget.purchaseDateBasis", { cutoff: data.cutoff })}</span>
        {" · "}
        <button type="button" className="budget-link" onClick={() => { onOpenMonth(data.period, data.current_month); }}>
          {viewTransactionsText}
        </button>
        {data.coverage.purchase_date_fallback_count > 0 ? (
          <span className="budget-muted">
            {" · "}
            {tPlural("budget.monthBudget.purchaseDateFallback", data.coverage.purchase_date_fallback_count)}
          </span>
        ) : null}
      </div>

      {data.configuration.status === "unconfigured" || data.summary === null || data.expected_income === null || data.pace === null ? (
        <>
          <p className="budget-notice" role="status">{t("budget.monthBudget.unconfigured")}</p>
          <div className="budget-totals">
            <CardDebtCard cardDebt={cardDebt} locale={locale} />
          </div>
        </>
      ) : (
        <>
          {!data.coverage.has_activity ? (
            <p className="budget-notice" role="status">
              <strong>{t("budget.monthBudget.noStoredData")}</strong> · {t("budget.monthBudget.noStoredDataDetail")}
            </p>
          ) : isZero(data.summary.start_budget) && isZero(data.summary.spent) && isZero(data.summary.remaining) ? (
            <p className="budget-muted" role="status">{t("budget.monthBudget.observedZero")}</p>
          ) : null}

          {data.coverage.pending_transfer_rows > 0 ? (
            <p className="budget-notice" role="note">
              {tPlural("budget.monthBudget.pendingTransfers", data.coverage.pending_transfer_rows)}
              {data.coverage.pending_row_keys_truncated === true
                ? ` ${t("budget.monthBudget.provisionalTruncated", {
                    shown: data.coverage.pending_row_keys?.length ?? 500,
                    total: data.coverage.pending_transfer_rows,
                  })}`
                : ""}
              {" "}
              <button type="button" className="budget-link"
                onClick={() => {
                  onOpenMonth(data.period, data.current_month, {
                    currency: data.currency,
                    provisionalKeys: data.coverage.pending_row_keys ?? [],
                    provisionalTotal: data.coverage.pending_transfer_rows,
                    provisionalTruncated: data.coverage.pending_row_keys_truncated === true,
                  });
                }}>
                {viewTransactionsText}
              </button>
            </p>
          ) : null}
          {data.coverage.transfer_scan_limited ? (
            <p className="budget-notice" role="note">{t("budget.monthBudget.transferScanLimited")}</p>
          ) : null}

          <div className="budget-totals">
            <article className="budget-total" aria-label={summaryTitle}>
              <div className="budget-limit-main">
                <h4>{summaryTitle}</h4>
                {data.coverage.provisional ? <span className="budget-chip budget-warn">{provisionalText}</span> : null}
              </div>
              <dl>
                <dt>
                  {t("budget.monthBudget.budgetReceived")}
                  {data.configuration.income_window ? (
                    <small className="budget-muted">
                      {" "}({incomeWindowLabel(data.configuration.income_window, locale)})
                    </small>
                  ) : null}
                </dt>
                <dd><strong>{money(data.summary.start_budget)}</strong></dd>
                <dt>
                  {isCurrent ? t("budget.monthBudget.spentToDate") : t("budget.monthBudget.spentClosed")}
                  {data.coverage.provisional ? <small className="budget-warn"> ({provisionalText})</small> : null}
                </dt>
                <dd><strong>{money(data.summary.spent)}</strong></dd>
                <dt>
                  {schedulePending || projectedRemaining !== null ? t("budget.monthBudget.remainingAtMonthEnd") : t("budget.monthBudget.remaining")}
                  {data.coverage.provisional ? <small className="budget-warn"> ({provisionalText})</small> : null}
                  {scheduleError && projectedRemaining !== null ? (
                    <small className="budget-warn"> ({t("budget.monthBudget.withoutScheduledPayments")})</small>
                  ) : null}
                </dt>
                <dd>
                  {schedulePending ? (
                    <span className="budget-muted" role="status">{t("budget.monthBudget.scheduledPaymentsLoading")}</span>
                  ) : (
                    <strong>
                      {projectedRemaining === null
                        ? money(data.summary.remaining)
                        : formatRatioHalfEven(projectedRemaining, data.currency, locale)}
                    </strong>
                  )}
                </dd>
              </dl>
              {scheduleError && projectedRemaining === null ? (
                <p className="budget-muted" role="note">{t("budget.monthBudget.scheduledPaymentsError")}</p>
              ) : null}
              {isPositiveDecimal(data.summary.start_budget) ? (
                <SummaryMeter spent={data.summary.spent} budget={data.summary.start_budget} label={t("budget.monthBudget.spentVsBudget")} />
              ) : null}
            </article>

            <ExpectedIncomeCard income={data.expected_income} currency={data.currency}
              evaluationDate={data.evaluation_date} categoryMap={categoryMap} categoryColors={categoryColors}
              locale={locale} />

            <PaceCard pace={data.pace} projection={paceProjection}
              schedulePending={schedulePending} scheduleError={scheduleError} currency={data.currency}
              provisional={data.coverage.provisional} partialIncome={data.expected_income.status === "partial"}
              locale={locale} />

            {data.pass_through === null ? null : (
              <PassThroughCard passThrough={data.pass_through} currency={data.currency}
                categoryMap={categoryMap} locale={locale} />
            )}

            <CardDebtCard cardDebt={cardDebt} locale={locale} />
          </div>

          <section className="budget-entries" aria-label={spendingAndLimitsTitle}>
            <h4>{spendingAndLimitsTitle}</h4>
            {categories === "pending" ? <p className="budget-muted" role="status">{t("budget.monthBudget.categoriesLoading")}</p> : null}
            {categories === "error" ? <p className="budget-notice" role="alert">{t("budget.monthBudget.categoriesError")}</p> : null}
            {isCategoryList(categories) && data.category_spend.length === 0 ? (
              <p className="budget-muted">{t("budget.monthBudget.noExpenses")}</p>
            ) : null}
            {isCategoryList(categories) && data.category_spend.length > 0 ? (
              <div className="budget-table-wrap" role="region" aria-label={spendingAndLimitsTitle} tabIndex={0}>
                <table className="budget-month-dashboard-table">
                  <thead>
                    <tr>
                      <th scope="col">{t("budget.monthBudget.category")}</th>
                      <th scope="col">{t("budget.monthBudget.expenses")}</th>
                      <th scope="col">{t("budget.monthBudget.refunds")}</th>
                      <th scope="col">{t("budget.monthBudget.netSpending")}</th>
                      <th scope="col">{t("budget.monthBudget.limitColumn")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {shownCategorySpend(data.category_spend, categories, locale, collapsed).map((row) => {
                      const oneOff = row.one_off;
                      const label = oneOff
                        ? (row.name ?? uncategorisedLabel)
                        : row.category_id === null
                          ? uncategorisedLabel
                          : (row.name ?? categoryMap.get(row.category_id) ?? unknownCategoryLabel);
                      const categoryId = row.category_id;
                      const closed = categoryId !== null && collapsed.has(categoryId);
                      const rowClass = [
                        row.tree ? `budget-tree-depth-${String(row.depth)}` : "",
                        oneOff ? "budget-one-off" : categoryId === null ? "is-uncategorised" : "",
                      ].filter(Boolean).join(" ") || undefined;
                      return (
                        <tr key={row.key} data-depth={row.tree ? row.depth : undefined} className={rowClass}>
                          <th scope="row">
                            <span className="budget-tree-cell">
                              {row.tree ? (
                                row.has_children && categoryId !== null ? (
                                  <button type="button" className="budget-tree-toggle" aria-expanded={!closed}
                                    aria-label={closed
                                      ? t("budget.monthBudget.expandCategory", { name: label })
                                      : t("budget.monthBudget.collapseCategory", { name: label })}
                                    onClick={() => { toggleCategory(categoryId); }}>
                                    {closed ? "▸" : "▾"}
                                  </button>
                                ) : (
                                  <span className="budget-tree-spacer" aria-hidden="true" />
                                )
                              ) : null}
                              <CategorySwatch color={oneOff ? oneOffCategoryColor(categoryColors, categoryId) : categoryColors.get(categoryId)} />
                              <span className="budget-tree-label">
                                {label}
                                {oneOff ? <small className="budget-one-off-tag">{oneOffTagText}</small> : null}
                                {row.archived ? <small className="budget-muted"> ({archivedText})</small> : null}
                              </span>
                            </span>
                          </th>
                          <td>{money(row.expenses)}</td>
                          <td>{money(row.refunds)}</td>
                          <td><strong>{money(row.net_spending)}</strong></td>
                          <td>
                            {row.limit === null ? noLimitText : (
                              <div className="budget-month-limit-cell">
                                <span className="budget-chip budget-limit-status" data-status={row.limit.status}>
                                  {labelOf(MONTH_BUDGET_LIMIT_STATUS_KEYS, row.limit.status)}
                                </span>
                                <div className="budget-limit-meter">
                                  <progress value={clampPercentString(row.limit.percent_used_floor)} max={100} aria-label={label} />
                                  <span>
                                    {t("budget.monthBudget.limitUsed", {
                                      limit: money(row.limit.monthly_limit),
                                      percent: row.limit.percent_used_floor,
                                    })}
                                  </span>
                                </div>
                                <div className="budget-entry-facts">
                                  {isZero(row.limit.over_by) ? (
                                    <span>{t("budget.monthBudget.limitRemaining", { amount: money(row.limit.remaining) })}</span>
                                  ) : (
                                    <span className="budget-warn">
                                      {t("budget.monthBudget.limitOverBy", { amount: money(row.limit.over_by) })}
                                    </span>
                                  )}
                                  <span>{t("budget.monthBudget.warningAt", { percent: row.limit.warning_percent })}</span>
                                </div>
                              </div>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : null}
          </section>

          {series === null || !isCategoryList(categories) ? null : (
            <MonthBudgetCharts budget={data} series={series} paceProjection={paceProjection}
              schedulePending={schedulePending} scheduleError={scheduleError} categories={categories}
              locale={locale} />
          )}
        </>
      )}
    </>
  );
}
