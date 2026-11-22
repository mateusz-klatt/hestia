// Read-only owner-private monthly budget dashboard and charts (P1 & P2, OpenAPI 0.29.0).
// Evaluates in Europe/Warsaw from one snapshot on the server; all amounts stay decimal strings end to end,
// ratios divide via BigInt half-even rounding, and no raw IDs or remittance text ever enter the DOM.
import { useEffect, useRef, useState } from "react";
import type { BudgetApi } from "../api";
import { formatAmount, when } from "../model";
import type { BudgetAccount } from "../model";
import { groupCategoryTotals, hasCategoryParents, visibleCategoryRows } from "./category_tree";
import { clampPercentString, formatRatioHalfEven, isPositiveDecimal, isZero, labelOf,
  monthLabel, monthlyBudgetCurrencies, pluralPl, ratioPercentFloor, shiftMonth, validCurrency } from "./model";
import type { AnalyticsCategory, AnalyticsMonthlyBudget, AnalyticsMonthlyBudgetCharts,
  AnalyticsMonthlyBudgetExpectedIncome, AnalyticsMonthlyBudgetIncomeWindow, AnalyticsMonthlyBudgetPace,
  AnalyticsMonthlyBudgetPassThrough, AnalyticsMonthlyBudgetSeries } from "./model";
import { MonthBudgetCharts } from "./month_budget_charts";

export const monthBudgetLabels = {
  en: {
    title: "Monthly budget",
    readOnly: "Read only",
    month: "Month",
    previousMonth: "Previous month",
    nextMonth: "Next month",
    currency: "Currency",
    currencyCode: "Currency code (ISO 4217)",
    applyCurrency: "Apply currency",
    loading: "Loading monthly budget…",
    categoriesLoading: "Loading categories before spending and charts…",
    error: "Monthly budget could not be loaded. Try again.",
    categoriesError: "Categories could not be loaded, so spending and charts are not shown.",
    badRequest: "Invalid month or currency selection. Choose another month or currency.",
    limit: "Monthly budget data exceed the response size limit. Choose another currency or month.",
    unconfigured: "Monthly budget is not configured yet on this server. Income categories must be configured before monthly totals can be shown.",
    noStoredData: "No stored data",
    noStoredDataDetail: "No stored transactions for this month and currency.",
    observedZero: "Transactions are stored for this month with zero net budget impact.",
    evaluatedFor: (date: string, timezone: string) => `Evaluated for ${date} (${timezone})`,
    ledgerAsOf: (formatted: string | null) =>
      formatted === null ? "No synced ledger data yet" : `Ledger data as of ${formatted}`,
    staleData: "Data may be out of date",
    staleAccounts: (count: number) => `${String(count)} ${count === 1 ? "stale account" : "stale accounts"}`,
    provisional: "Provisional totals",
    pendingTransfers: (count: number) =>
      `Provisional totals: ${String(count)} unverified transfer or classification ${count === 1 ? "candidate still affects" : "candidates still affect"} totals.`,
    transferScanLimited: "Own-transfer detection reached its scan limit; some transfer pairs may be missing.",
    purchaseDateBasis: (cutoff: string) => `Through ${cutoff} · expenses by purchase date`,
    purchaseDateFallback: (count: number) =>
      `${String(count)} ${count === 1 ? "debit used" : "debits used"} posting date because purchase date was absent.`,
    viewTransactions: "View transactions",
    summaryTitle: "Monthly summary",
    budgetReceived: "Budget received",
    spentToDate: "Spent to date",
    spentClosed: "Spent",
    remaining: "Remaining",
    spentVsBudget: "Spent compared to budget received",
    expectedIncomeTitle: "Expected income",
    expectedIncomeTotal: "Total expected",
    partialIncome: "Partial income evidence",
    partialIncomeMissing: "Partial income evidence — remaining expected income could not be estimated reliably.",
    expectedIncomeUnavailable: "Unavailable (no prior-month income data).",
    expectedIncomeNone: "No additional income expected this month.",
    expectedIncomeHistorical: "Closed month — booked income only.",
    expectedDate: (date: string | null) =>
      date === null ? "Combined estimate (no expected date)" : `Expected ${date}`,
    expectedDelayed: "Delayed",
    expectedSourcePreviousMonth: "Previous month",
    incomeCategoryFallback: "Income category",
    paceTitle: "Spending pace",
    paceBasisReceived: "Based on budget received",
    paceBasisReceivedPlusExpected: "Based on budget received + expected",
    paceBudget: "Pace budget",
    dailyRate: "Daily rate",
    perDay: (formatted: string) => `${formatted} / day`,
    projectedSpend: "Projected month spend",
    paceRunsOut: (day: number | null) =>
      day === null
        ? "At this pace the budget will run out before month end."
        : `At this pace the budget runs out around day ${String(day)}.`,
    paceWithinBudget: "At this pace spending stays within the budget.",
    paceAlreadyReached: (day: number | null) =>
      day === null
        ? "Spending has already reached the budget."
        : `Spending has already reached the budget (day ${String(day)}).`,
    paceNoBudget: "No funded budget yet to calculate pace.",
    paceNoPositivePace: "No positive spending pace yet.",
    paceHistorical: "Closed month — forward pace is not calculated.",
    passThroughTitle: "Rent and mortgage",
    rentLabel: "Rent",
    mortgageLabel: "Mortgage",
    rentReceived: "Rent received",
    mortgageDebits: "Mortgage payments",
    mortgageRefunds: "Mortgage refunds",
    matchedRent: "Covered by rent",
    unappliedRent: "Reserved excess rent",
    unappliedMortgageRefunds: "Unused mortgage refunds",
    uncoveredMortgage: "Uncovered mortgage",
    passThroughNote: "Uncovered mortgage is included in spending below; excess rent stays reserved.",
    spendingAndLimits: "Spending and limits",
    category: "Category",
    expenses: "Expenses",
    refunds: "Refunds",
    netSpending: "Net spending",
    limitColumn: "Limit",
    noLimit: "—",
    uncategorised: "Uncategorised",
    unknownCategory: "Category",
    archived: "archived",
    collapseCategory: (name: string) => `Collapse ${name}`,
    expandCategory: (name: string) => `Expand ${name}`,
    noExpenses: "No category spending recorded in this month.",
    limitStatuses: {
      within: "Within limit",
      approaching: "Approaching limit",
      at_limit: "At limit",
      exceeded: "Exceeded",
    },
    limitUsed: (limitFormatted: string, percent: string) => `of ${limitFormatted} (${percent}%)`,
    limitRemaining: (amount: string) => `Remaining: ${amount}`,
    limitOverBy: (amount: string) => `Over by: ${amount}`,
    warningAt: (percent: number) => `Warning at ${String(percent)}%`,
    cardExposureTitle: "Unpaid card spending",
    cardStatuses: { unavailable: "Unavailable" },
    unavailable: "Unavailable",
    cardExposureNote: "Card exposure stays unavailable until bank sign and full transaction-window coverage are verified.",
    chartsSectionTitle: "Charts",
    chartHistory: "Chart history",
    historyMonths: (count: number) => `${String(count)} months`,
    showData: "Show data",
    chartDailyTitle: "Cumulative spending",
    chartCategoriesTitle: "Spending by category",
    chartIncomeSpendTitle: "Income and spending",
    otherCategory: "Other",
    dateLabel: "Date",
    monthColumn: "Month",
    cumulativeSpentLabel: "Cumulative spending",
    bookedBudgetLine: "Booked budget",
    expectedBudgetLine: "Budget + expected",
    paceExtensionLine: "Pace projection",
    exhaustionMarker: (day: number) => `Exhaustion ~day ${String(day)}`,
    incompleteMonth: "Incomplete month",
    currentMonth: "Current month",
    expectedIncomeLegend: "Expected income",
    statusColumn: "Status",
    totalLabel: "Total",
    noValue: "—",
    chartDailyDesc: (currency: string, start: string, cutoff: string) =>
      `Cumulative net spending in ${currency} from ${start} through ${cutoff} compared against budget.`,
    chartCategoriesDesc: (count: number, currency: string) =>
      `Monthly stacked category net spending in ${currency} across ${String(count)} months.`,
    chartIncomeSpendDesc: (count: number, currency: string) =>
      `Monthly booked income and net spending in ${currency} across ${String(count)} months.`,
  },
  pl: {
    title: "Budżet miesiąca",
    readOnly: "Tylko do odczytu",
    month: "Miesiąc",
    previousMonth: "Poprzedni miesiąc",
    nextMonth: "Następny miesiąc",
    currency: "Waluta",
    currencyCode: "Kod waluty (ISO 4217)",
    applyCurrency: "Zastosuj walutę",
    loading: "Wczytywanie budżetu miesiąca…",
    categoriesLoading: "Wczytywanie kategorii przed wydatkami i wykresami…",
    error: "Nie udało się wczytać budżetu miesiąca. Spróbuj ponownie.",
    categoriesError: "Nie udało się wczytać kategorii, więc wydatki i wykresy nie są pokazane.",
    badRequest: "Nieprawidłowy wybór miesiąca lub waluty. Wybierz inny miesiąc lub walutę.",
    limit: "Dane budżetu miesiąca przekraczają limit rozmiaru odpowiedzi. Wybierz inną walutę lub miesiąc.",
    unconfigured: "Budżet miesiąca nie jest jeszcze skonfigurowany na tym serwerze. Najpierw trzeba skonfigurować kategorie wpływów.",
    noStoredData: "Brak zapisanych danych",
    noStoredDataDetail: "Brak zapisanych transakcji dla tego miesiąca i waluty.",
    observedZero: "W tym miesiącu zapisano transakcje o zerowym saldzie budżetowym.",
    evaluatedFor: (date: string, timezone: string) => `Stan na ${date} (${timezone})`,
    ledgerAsOf: (formatted: string | null) =>
      formatted === null ? "Brak zsynchronizowanych danych księgowych" : `Dane księgowe na ${formatted}`,
    staleData: "Dane mogą być nieaktualne",
    staleAccounts: (count: number) =>
      `${String(count)} ${pluralPl(count, "nieaktualne konto", "nieaktualne konta", "nieaktualnych kont")}`,
    provisional: "Wynik wstępny",
    pendingTransfers: (count: number) =>
      `Wynik wstępny: ${String(count)} ${pluralPl(count, "niepotwierdzony wpis lub przelew nadal wpływa", "niepotwierdzone wpisy lub przelewy nadal wpływają", "niepotwierdzonych wpisów lub przelewów nadal wpływa")} na sumy.`,
    transferScanLimited: "Wyszukiwanie przelewów własnych osiągnęło limit; części par może brakować.",
    purchaseDateBasis: (cutoff: string) => `Do ${cutoff} · wydatki według daty zakupu`,
    purchaseDateFallback: (count: number) =>
      `Dla ${String(count)} ${pluralPl(count, "wydatku", "wydatków", "wydatków")} użyto daty księgowania z powodu braku daty zakupu.`,
    viewTransactions: "Zobacz transakcje",
    summaryTitle: "Podsumowanie miesiąca",
    budgetReceived: "Budżet z wpływów",
    spentToDate: "Wydano do dziś",
    spentClosed: "Wydano",
    remaining: "Pozostało",
    spentVsBudget: "Wydano względem budżetu z wpływów",
    expectedIncomeTitle: "Oczekiwane wpływy",
    expectedIncomeTotal: "Łącznie oczekiwane",
    partialIncome: "Niepełne dane o wpływach",
    partialIncomeMissing: "Niepełne dane o wpływach — nie można wiarygodnie oszacować pozostałych wpływów.",
    expectedIncomeUnavailable: "Niedostępne (brak wpływów z poprzedniego miesiąca).",
    expectedIncomeNone: "Brak dalszych oczekiwanych wpływów w tym miesiącu.",
    expectedIncomeHistorical: "Zamknięty miesiąc — tylko zaksięgowane wpływy.",
    expectedDate: (date: string | null) =>
      date === null ? "Szacunek łączny (brak przewidywanej daty)" : `Oczekiwano ${date}`,
    expectedDelayed: "Opóźniony",
    expectedSourcePreviousMonth: "Poprzedni miesiąc",
    incomeCategoryFallback: "Kategoria wpływów",
    paceTitle: "Tempo wydatków",
    paceBasisReceived: "Podstawa: budżet z wpływów",
    paceBasisReceivedPlusExpected: "Podstawa: budżet z wpływów + oczekiwane",
    paceBudget: "Budżet do tempa",
    dailyRate: "Tempo dzienne",
    perDay: (formatted: string) => `${formatted} / dzień`,
    projectedSpend: "Prognozowane wydatki miesiąca",
    paceRunsOut: (day: number | null) =>
      day === null
        ? "Według obecnego tempa budżet wyczerpie się przed końcem miesiąca."
        : `Według obecnego tempa budżet wyczerpie się około dnia ${String(day)}.`,
    paceWithinBudget: "Według obecnego tempa wydatki zmieszczą się w budżecie.",
    paceAlreadyReached: (day: number | null) =>
      day === null
        ? "Wydatki już osiągnęły budżet."
        : `Wydatki już osiągnęły budżet (dzień ${String(day)}).`,
    paceNoBudget: "Brak jeszcze budżetu z wpływów do oceny tempa.",
    paceNoPositivePace: "Brak dodatniego tempa wydatków.",
    paceHistorical: "Zamknięty miesiąc — nie wylicza się prognozy tempa.",
    passThroughTitle: "Najem i rata",
    rentLabel: "Najem",
    mortgageLabel: "Rata hipoteczna",
    rentReceived: "Wpływy z najmu",
    mortgageDebits: "Płatności raty",
    mortgageRefunds: "Zwroty raty",
    matchedRent: "Pokryte najmem",
    unappliedRent: "Niewykorzystany najem (rezerwa)",
    unappliedMortgageRefunds: "Niewykorzystane zwroty raty",
    uncoveredMortgage: "Niepokryta część raty",
    passThroughNote: "Niepokryta część raty wlicza się do wydatków poniżej; nadwyżka najmu pozostaje zarezerwowana.",
    spendingAndLimits: "Wydatki i limity",
    category: "Kategoria",
    expenses: "Wydatki",
    refunds: "Zwroty",
    netSpending: "Wydatki netto",
    limitColumn: "Limit",
    noLimit: "—",
    uncategorised: "Bez kategorii",
    unknownCategory: "Kategoria",
    archived: "archiwalna",
    collapseCategory: (name: string) => `Zwiń ${name}`,
    expandCategory: (name: string) => `Rozwiń ${name}`,
    noExpenses: "Brak wydatków w kategoriach w tym miesiącu.",
    limitStatuses: {
      within: "W limicie",
      approaching: "Blisko limitu",
      at_limit: "Na limicie",
      exceeded: "Przekroczony",
    },
    limitUsed: (limitFormatted: string, percent: string) => `z ${limitFormatted} (${percent}%)`,
    limitRemaining: (amount: string) => `Pozostało: ${amount}`,
    limitOverBy: (amount: string) => `Przekroczono o: ${amount}`,
    warningAt: (percent: number) => `Ostrzeżenie od ${String(percent)}%`,
    cardExposureTitle: "Niespłacone wydatki kartą",
    cardStatuses: { unavailable: "Niedostępne" },
    unavailable: "Niedostępne",
    cardExposureNote: "Ekspozycja kartowa pozostaje niedostępna do czasu potwierdzenia znaku salda i pełnego okna historii transakcji.",
    chartsSectionTitle: "Wykresy",
    chartHistory: "Zakres wykresów",
    historyMonths: (count: number) => `${String(count)} ${pluralPl(count, "miesiąc", "miesiące", "miesięcy")}`,
    showData: "Pokaż dane",
    chartDailyTitle: "Wydatki narastająco",
    chartCategoriesTitle: "Wydatki według kategorii",
    chartIncomeSpendTitle: "Wpływy i wydatki",
    otherCategory: "Pozostałe",
    dateLabel: "Data",
    monthColumn: "Miesiąc",
    cumulativeSpentLabel: "Wydatki narastająco",
    bookedBudgetLine: "Budżet z wpływów",
    expectedBudgetLine: "Budżet + oczekiwane",
    paceExtensionLine: "Prognoza tempa",
    exhaustionMarker: (day: number) => `Wyczerpanie ~dzień ${String(day)}`,
    incompleteMonth: "Niepełny miesiąc",
    currentMonth: "Bieżący miesiąc",
    expectedIncomeLegend: "Oczekiwane wpływy",
    statusColumn: "Status",
    totalLabel: "Razem",
    noValue: "—",
    chartDailyDesc: (currency: string, start: string, cutoff: string) =>
      `Skumulowane wydatki netto w walucie ${currency} od ${start} do ${cutoff} na tle budżetu.`,
    chartCategoriesDesc: (count: number, currency: string) =>
      `Miesięczne wydatki netto według kategorii w walucie ${currency} dla ${String(count)} ${pluralPl(count, "miesiąca", "miesięcy", "miesięcy")}.`,
    chartIncomeSpendDesc: (count: number, currency: string) =>
      `Miesięczne wpływy do budżetu i wydatki netto w walucie ${currency} dla ${String(count)} ${pluralPl(count, "miesiąca", "miesięcy", "miesięcy")}.`,
  },
};
export type MonthBudgetLabels = typeof monthBudgetLabels.en;

type Shown =
  | { state: "loading" | "badRequest" | "limit" | "error" }
  | { state: "ready"; data: AnalyticsMonthlyBudgetCharts };

export interface MonthBudgetViewProps {
  api: Pick<BudgetApi, "monthlyBudgetCharts" | "categories">;
  accounts: readonly BudgetAccount[];
  text: MonthBudgetLabels;
  locale: string;
  revision: number;
  onAccessLoss: (result: { ok: boolean; status?: number; error?: string }) => boolean;
  onOpenMonth: (month: string, serverCurrentMonth: string) => void;
}

function incomeWindowLabel(incomeWindow: AnalyticsMonthlyBudgetIncomeWindow, locale: string): string {
  const monthFormatter = new Intl.DateTimeFormat(locale, { month: "short", timeZone: "UTC" });
  const formatDay = (value: string) => {
    const parsed = new Date(`${value}T00:00:00Z`);
    return `${String(parsed.getUTCDate())} ${monthFormatter.format(parsed)}`;
  };
  return `${formatDay(incomeWindow.start)} – ${formatDay(incomeWindow.end)}`;
}

function paceSentence(pace: AnalyticsMonthlyBudgetPace, text: MonthBudgetLabels): string {
  switch (pace.status) {
    case "runs_out": return text.paceRunsOut(pace.exhaustion_day);
    case "within_budget": return text.paceWithinBudget;
    case "already_reached": return text.paceAlreadyReached(pace.exhaustion_day);
    case "no_budget": return text.paceNoBudget;
    case "no_positive_pace": return text.paceNoPositivePace;
    case "historical": return text.paceHistorical;
  }
}

function ExpectedIncomeCard({ income, currency, evaluationDate, categoryMap, text, locale }: {
  income: AnalyticsMonthlyBudgetExpectedIncome;
  currency: string;
  evaluationDate: string;
  categoryMap: ReadonlyMap<string, string>;
  text: MonthBudgetLabels;
  locale: string;
}) {
  const money = (amount: string) => formatAmount(amount, currency, locale);
  return (
    <article className="budget-total" aria-label={text.expectedIncomeTitle}>
      <div className="budget-limit-main">
        <h4>{text.expectedIncomeTitle}</h4>
        {income.status === "partial" ? <span className="budget-chip budget-warn">{text.partialIncome}</span> : null}
      </div>
      {income.status === "historical" ? (
        <p className="budget-muted">{text.expectedIncomeHistorical}</p>
      ) : income.status === "unavailable" ? (
        <p className="budget-muted">{text.expectedIncomeUnavailable}</p>
      ) : income.status === "none" ? (
        <>
          <dl>
            <dt>{text.expectedIncomeTotal}</dt>
            <dd>{money(income.total ?? "0")}</dd>
          </dl>
          <p className="budget-muted">{text.expectedIncomeNone}</p>
        </>
      ) : (
        <>
          {income.total !== null ? (
            <dl>
              <dt>{text.expectedIncomeTotal}</dt>
              <dd><strong>{money(income.total)}</strong></dd>
            </dl>
          ) : (
            <p className="budget-muted">{text.partialIncomeMissing}</p>
          )}
          {income.items.length === 0 ? null : (
            <ul className="budget-entry-list">
              {income.items.map((item, index) => {
                const delayed = item.expected_date !== null && item.expected_date < evaluationDate;
                return (
                  <li key={`${String(index)}-${item.expected_date ?? "none"}`} className="budget-entry">
                    <div className="budget-recurring-main">
                      <span className="budget-entry-text">
                        {categoryMap.get(item.category_id) ?? text.incomeCategoryFallback}
                      </span>
                      <strong className="budget-recurring-amount">{money(item.amount)}</strong>
                    </div>
                    <div className="budget-entry-facts">
                      <span>{text.expectedDate(item.expected_date)}</span>
                      <span className="budget-chip">{text.expectedSourcePreviousMonth}</span>
                      {delayed ? <span className="budget-chip budget-warn">{text.expectedDelayed}</span> : null}
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

function PaceCard({ pace, currency, provisional, partialIncome, text, locale }: {
  pace: AnalyticsMonthlyBudgetPace;
  currency: string;
  provisional: boolean;
  partialIncome: boolean;
  text: MonthBudgetLabels;
  locale: string;
}) {
  return (
    <article className="budget-total" aria-label={text.paceTitle}>
      <div className="budget-limit-main">
        <h4>{text.paceTitle}</h4>
        <div className="budget-actions">
          {provisional ? <span className="budget-chip budget-warn">{text.provisional}</span> : null}
          {partialIncome ? <span className="budget-chip budget-warn">{text.partialIncome}</span> : null}
        </div>
      </div>
      <p className="budget-muted">
        {pace.basis === "received_plus_expected" ? text.paceBasisReceivedPlusExpected : text.paceBasisReceived}
      </p>
      <p>{paceSentence(pace, text)}</p>
      <dl>
        <dt>{text.paceBudget}</dt>
        <dd>{formatAmount(pace.budget, currency, locale)}</dd>
        {pace.daily_rate === null ? null : (
          <>
            <dt>{text.dailyRate}</dt>
            <dd>{text.perDay(formatRatioHalfEven(pace.daily_rate, currency, locale))}</dd>
          </>
        )}
        {pace.projected_month_spend === null ? null : (
          <>
            <dt>{text.projectedSpend}</dt>
            <dd>{formatRatioHalfEven(pace.projected_month_spend, currency, locale)}</dd>
          </>
        )}
      </dl>
    </article>
  );
}

function PassThroughCard({ passThrough, currency, categoryMap, text, locale }: {
  passThrough: AnalyticsMonthlyBudgetPassThrough;
  currency: string;
  categoryMap: ReadonlyMap<string, string>;
  text: MonthBudgetLabels;
  locale: string;
}) {
  const money = (amount: string) => formatAmount(amount, currency, locale);
  const rentName = categoryMap.get(passThrough.rent_category_id) ?? text.rentLabel;
  const mortgageName = categoryMap.get(passThrough.mortgage_category_id) ?? text.mortgageLabel;
  return (
    <article className="budget-total" aria-label={text.passThroughTitle}>
      <h4>{text.passThroughTitle}</h4>
      <p className="budget-muted">{rentName} → {mortgageName}</p>
      <dl>
        <dt>{text.rentReceived}</dt>
        <dd>{money(passThrough.rent_received)}</dd>
        <dt>{text.mortgageDebits}</dt>
        <dd>{money(passThrough.mortgage_debits)}</dd>
        {isZero(passThrough.mortgage_refunds) ? null : (
          <>
            <dt>{text.mortgageRefunds}</dt>
            <dd>{money(passThrough.mortgage_refunds)}</dd>
          </>
        )}
        <dt>{text.matchedRent}</dt>
        <dd>{money(passThrough.matched)}</dd>
        <dt>{text.unappliedRent}</dt>
        <dd>{money(passThrough.unapplied_rent)}</dd>
        {isZero(passThrough.unapplied_mortgage_refunds) ? null : (
          <>
            <dt>{text.unappliedMortgageRefunds}</dt>
            <dd>{money(passThrough.unapplied_mortgage_refunds)}</dd>
          </>
        )}
        <dt><strong>{text.uncoveredMortgage}</strong></dt>
        <dd><strong>{money(passThrough.uncovered_mortgage)}</strong></dd>
      </dl>
      <p className="budget-muted">{text.passThroughNote}</p>
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
  limit: CategorySpend["limit"]; tree: boolean;
}[] {
  if (!hasCategoryParents(categories)) {
    return rows.map((row, index) => ({
      key: row.category_id ?? `uncategorised-${String(index)}`,
      category_id: row.category_id, name: row.name, archived: row.archived, depth: 0, has_children: false,
      expenses: row.expenses, refunds: row.refunds, net_spending: row.net_spending, limit: row.limit, tree: false,
    }));
  }
  const limits = new Map(rows.map((row) => [row.category_id, row.limit]));
  return visibleCategoryRows(groupCategoryTotals(rows, categories, locale), collapsed).map((row) => ({
    key: row.category_id ?? "uncategorised",
    category_id: row.category_id, name: row.name, archived: row.archived, depth: row.depth,
    has_children: row.has_children, expenses: row.expenses, refunds: row.refunds, net_spending: row.net_spending,
    limit: limits.get(row.category_id) ?? null, tree: true,
  }));
}

export function MonthBudgetView({ api, accounts, text, locale, revision, onAccessLoss, onOpenMonth }: MonthBudgetViewProps) {
  const [period, setPeriod] = useState<string | undefined>(undefined);
  const [currency, setCurrency] = useState("PLN");
  const [history, setHistory] = useState<6 | 12>(6);
  const [discoveredCurrencies, setDiscoveredCurrencies] = useState<string[]>([]);
  const [serverCurrentMonth, setServerCurrentMonth] = useState<string | null>(null);
  const [manualCode, setManualCode] = useState("");
  const [shown, setShown] = useState<Shown>({ state: "loading" });
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

  return (
    <section className="budget-month budget-month-dashboard" aria-label={text.title}>
      <div className="budget-limit-main">
        <h4>{text.title}</h4>
        <span className="budget-chip">{text.readOnly}</span>
      </div>
      <div className="budget-month-dashboard-controls">
        {nav === null ? null : (
          <nav className="budget-month-nav" aria-label={text.month}>
            <button type="button" aria-label={text.previousMonth}
              onClick={() => { setPeriod(shiftMonth(nav.active, -1)); }}>◀</button>
            <h3>{monthLabel(nav.active, locale)}</h3>
            <button type="button" aria-label={text.nextMonth} disabled={nav.active >= nav.current}
              onClick={() => { setPeriod(shiftMonth(nav.active, 1)); }}>▶</button>
          </nav>
        )}
        <div className="budget-range">
          <label>{text.currency}
            <select name="currency" value={currency} onChange={(event) => { setCurrency(event.currentTarget.value); }}>
              {currencies.map((code) => <option key={code} value={code}>{code}</option>)}
            </select>
          </label>
          <label>{text.chartHistory}
            <select name="history" value={String(history)} onChange={(event) => {
              setHistory(event.currentTarget.value === "12" ? 12 : 6);
            }}>
              <option value="6">{text.historyMonths(6)}</option>
              <option value="12">{text.historyMonths(12)}</option>
            </select>
          </label>
        </div>
      </div>

      {shown.state === "loading" ? <p className="budget-muted" role="status">{text.loading}</p> : null}
      {shown.state === "badRequest" ? <p className="budget-notice" role="alert">{text.badRequest}</p> : null}
      {shown.state === "limit" ? (
        <>
          <p className="budget-notice" role="alert">{text.limit}</p>
          <form className="budget-rule-editor" onSubmit={(event) => {
            event.preventDefault();
            if (validCurrency(manualCode)) setCurrency(manualCode.trim().toUpperCase());
          }}>
            <label>{text.currencyCode}
              <input type="text" name="manual_currency" autoComplete="off" maxLength={3} value={manualCode}
                onChange={(event) => { setManualCode(event.currentTarget.value); }} />
            </label>
            <button type="submit" disabled={!validCurrency(manualCode)}>{text.applyCurrency}</button>
          </form>
        </>
      ) : null}
      {shown.state === "error" ? <p className="budget-notice" role="alert">{text.error}</p> : null}

      {data === null ? null : (
        <ReadyDashboard data={data} series={series} categoryMap={categoryMap} categories={categories} text={text}
          locale={locale} onOpenMonth={onOpenMonth} />
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

function ReadyDashboard({ data, series, categoryMap, categories, text, locale, onOpenMonth }: {
  data: AnalyticsMonthlyBudget;
  series: AnalyticsMonthlyBudgetSeries | null;
  categoryMap: ReadonlyMap<string, string>;
  categories: LoadedCategories;
  text: MonthBudgetLabels;
  locale: string;
  onOpenMonth: (month: string, serverCurrentMonth: string) => void;
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

  return (
    <>
      <ul className="budget-facts">
        <li>{text.evaluatedFor(data.evaluation_date, data.evaluation_timezone)}</li>
        <li>{text.ledgerAsOf(ledgerStamp)}</li>
      </ul>
      {data.coverage.stale_selected_accounts > 0 ? (
        <p className="budget-notice" role="note">
          {text.staleData} · {text.staleAccounts(data.coverage.stale_selected_accounts)}
        </p>
      ) : null}
      <div className="budget-month-basis">
        <span>{text.purchaseDateBasis(data.cutoff)}</span>
        {" · "}
        <button type="button" className="budget-link" onClick={() => { onOpenMonth(data.period, data.current_month); }}>
          {text.viewTransactions}
        </button>
        {data.coverage.purchase_date_fallback_count > 0 ? (
          <span className="budget-muted"> · {text.purchaseDateFallback(data.coverage.purchase_date_fallback_count)}</span>
        ) : null}
      </div>

      {data.configuration.status === "unconfigured" || data.summary === null || data.expected_income === null || data.pace === null ? (
        <p className="budget-notice" role="status">{text.unconfigured}</p>
      ) : (
        <>
          {!data.coverage.has_activity ? (
            <p className="budget-notice" role="status">
              <strong>{text.noStoredData}</strong> · {text.noStoredDataDetail}
            </p>
          ) : isZero(data.summary.start_budget) && isZero(data.summary.spent) && isZero(data.summary.remaining) ? (
            <p className="budget-muted" role="status">{text.observedZero}</p>
          ) : null}

          {data.coverage.pending_transfer_rows > 0 ? (
            <p className="budget-notice" role="note">
              {text.pendingTransfers(data.coverage.pending_transfer_rows)}
              {" "}
              <button type="button" className="budget-link" onClick={() => { onOpenMonth(data.period, data.current_month); }}>
                {text.viewTransactions}
              </button>
            </p>
          ) : null}
          {data.coverage.transfer_scan_limited ? (
            <p className="budget-notice" role="note">{text.transferScanLimited}</p>
          ) : null}

          <div className="budget-totals">
            <article className="budget-total" aria-label={text.summaryTitle}>
              <div className="budget-limit-main">
                <h4>{text.summaryTitle}</h4>
                {data.coverage.provisional ? <span className="budget-chip budget-warn">{text.provisional}</span> : null}
              </div>
              <dl>
                <dt>
                  {text.budgetReceived}
                  {data.configuration.income_window ? (
                    <small className="budget-muted">
                      {" "}({incomeWindowLabel(data.configuration.income_window, locale)})
                    </small>
                  ) : null}
                </dt>
                <dd><strong>{money(data.summary.start_budget)}</strong></dd>
                <dt>
                  {isCurrent ? text.spentToDate : text.spentClosed}
                  {data.coverage.provisional ? <small className="budget-warn"> ({text.provisional})</small> : null}
                </dt>
                <dd><strong>{money(data.summary.spent)}</strong></dd>
                <dt>
                  {text.remaining}
                  {data.coverage.provisional ? <small className="budget-warn"> ({text.provisional})</small> : null}
                </dt>
                <dd><strong>{money(data.summary.remaining)}</strong></dd>
              </dl>
              {isPositiveDecimal(data.summary.start_budget) ? (
                <SummaryMeter spent={data.summary.spent} budget={data.summary.start_budget} label={text.spentVsBudget} />
              ) : null}
            </article>

            <ExpectedIncomeCard income={data.expected_income} currency={data.currency}
              evaluationDate={data.evaluation_date} categoryMap={categoryMap} text={text} locale={locale} />

            <PaceCard pace={data.pace} currency={data.currency} provisional={data.coverage.provisional}
              partialIncome={data.expected_income.status === "partial"} text={text} locale={locale} />

            {data.pass_through === null ? null : (
              <PassThroughCard passThrough={data.pass_through} currency={data.currency}
                categoryMap={categoryMap} text={text} locale={locale} />
            )}

            <article className="budget-total" aria-label={text.cardExposureTitle}>
              <h4>{text.cardExposureTitle}</h4>
              <dl>
                <dt>{text.cardExposureTitle}</dt>
                <dd><strong>{labelOf(text.cardStatuses, data.card_exposure.status)}</strong></dd>
              </dl>
              <p className="budget-muted">{text.cardExposureNote}</p>
            </article>
          </div>

          <section className="budget-entries" aria-label={text.spendingAndLimits}>
            <h4>{text.spendingAndLimits}</h4>
            {categories === "pending" ? <p className="budget-muted" role="status">{text.categoriesLoading}</p> : null}
            {categories === "error" ? <p className="budget-notice" role="alert">{text.categoriesError}</p> : null}
            {isCategoryList(categories) && data.category_spend.length === 0 ? (
              <p className="budget-muted">{text.noExpenses}</p>
            ) : null}
            {isCategoryList(categories) && data.category_spend.length > 0 ? (
              <div className="budget-table-wrap" role="region" aria-label={text.spendingAndLimits} tabIndex={0}>
                <table className="budget-month-dashboard-table">
                  <thead>
                    <tr>
                      <th scope="col">{text.category}</th>
                      <th scope="col">{text.expenses}</th>
                      <th scope="col">{text.refunds}</th>
                      <th scope="col">{text.netSpending}</th>
                      <th scope="col">{text.limitColumn}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {shownCategorySpend(data.category_spend, categories, locale, collapsed).map((row) => {
                      const label = row.category_id === null
                        ? text.uncategorised
                        : (row.name ?? categoryMap.get(row.category_id) ?? text.unknownCategory);
                      const categoryId = row.category_id;
                      const closed = categoryId !== null && collapsed.has(categoryId);
                      return (
                        <tr key={row.key} data-depth={row.tree ? row.depth : undefined}
                          className={categoryId === null ? "is-uncategorised" : undefined}>
                          <th scope="row">
                            {row.tree && row.has_children && categoryId !== null ? (
                              <button type="button" className="budget-tree-toggle" aria-expanded={!closed}
                                aria-label={closed ? text.expandCategory(label) : text.collapseCategory(label)}
                                onClick={() => { toggleCategory(categoryId); }}>
                                {closed ? "▸" : "▾"}
                              </button>
                            ) : null}
                            {label}
                            {row.archived ? <small className="budget-muted"> ({text.archived})</small> : null}
                          </th>
                          <td>{money(row.expenses)}</td>
                          <td>{money(row.refunds)}</td>
                          <td><strong>{money(row.net_spending)}</strong></td>
                          <td>
                            {row.limit === null ? text.noLimit : (
                              <div className="budget-month-limit-cell">
                                <span className="budget-chip budget-limit-status" data-status={row.limit.status}>
                                  {labelOf(text.limitStatuses, row.limit.status)}
                                </span>
                                <div className="budget-limit-meter">
                                  <progress value={clampPercentString(row.limit.percent_used_floor)} max={100} aria-label={label} />
                                  <span>{text.limitUsed(money(row.limit.monthly_limit), row.limit.percent_used_floor)}</span>
                                </div>
                                <div className="budget-entry-facts">
                                  {isZero(row.limit.over_by) ? (
                                    <span>{text.limitRemaining(money(row.limit.remaining))}</span>
                                  ) : (
                                    <span className="budget-warn">{text.limitOverBy(money(row.limit.over_by))}</span>
                                  )}
                                  <span>{text.warningAt(row.limit.warning_percent)}</span>
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
            <MonthBudgetCharts budget={data} series={series} categories={categories} locale={locale} text={text} />
          )}
        </>
      )}
    </>
  );
}
