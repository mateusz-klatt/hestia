// The year view (2D): the year's server totals and, per currency, one row per month from the same answer.
// Choosing a month opens it in the month view; nothing is added up in the browser.
import { useCallback, useState } from "react";
import type { BudgetApi } from "../api";
import type { BudgetAccount, BudgetConnection } from "../model";
import { formatAmount } from "../model";
import { ExportMenu } from "./export_menu";
import { exportBasename, exportLabels, monthsOfYear } from "./export_rows";
import type { ExportLabels } from "./export_rows";
import { NO_FILTER, currentMonth, isZero } from "./model";
import type { AnalyticsOverview } from "./model";
import { OverviewPanel } from "./overview";

export const yearLabels = {
  en: { previous: "Previous year", next: "Next year", months: "Months", month: "Month", spending: "Spending", income: "Income",
    toClassify: "To classify", open: (month: string) => `Open ${month}`, noData: "—",
    exportMenu: exportLabels.en },
  pl: { previous: "Poprzedni rok", next: "Następny rok", months: "Miesiące", month: "Miesiąc", spending: "Wydatki", income: "Wpływy",
    toClassify: "Do sklasyfikowania", open: (month: string) => `Otwórz ${month}`, noData: "—",
    exportMenu: exportLabels.pl },
};
export interface YearLabels {
  previous: string; next: string; months: string; month: string; spending: string; income: string; toClassify: string;
  open: (month: string) => string; noData: string; exportMenu: ExportLabels;
}

export interface YearViewProps {
  api: Pick<BudgetApi, "overview" | "analyticsTransactions" | "categories">;
  accounts: readonly BudgetAccount[];
  connections?: readonly BudgetConnection[] | undefined;
  text: YearLabels;
  locale: string;
  revision: number;
  onAccessLoss: (result: { ok: boolean; status?: number; error?: string }) => boolean;
  /** Opens one month ("YYYY-MM") in the month view. */
  onOpenMonth: (month: string) => void;
  today?: () => Date;
}

function monthName(month: string, locale: string): string {
  const [year, index] = month.split("-").map(Number);
  if (year === undefined || index === undefined || Number.isNaN(year) || Number.isNaN(index)) return month;
  return new Intl.DateTimeFormat(locale, { month: "long", timeZone: "UTC" }).format(new Date(Date.UTC(year, index - 1, 15)));
}

export function YearView({ api, accounts, connections = [], text, locale, revision, onAccessLoss, onOpenMonth,
  today = () => new Date() }: YearViewProps) {
  const latest = currentMonth(today()).slice(0, 4);
  const [year, setYear] = useState(latest);
  const overview = useCallback((period: string) => api.overview(period), [api]);
  const loadCategories = useCallback(() => api.categories(), [api]);
  const details = useCallback((data: AnalyticsOverview) => (
    data.months.length === 0 ? null : data.currencies.map((total) => {
      const unclassified = data.months.some((item) => item.currencies.some((value) =>
        value.currency === total.currency && !isZero(value.unclassified_credit)));
      return (
        <table key={total.currency} className="budget-year" aria-label={`${text.months} ${total.currency}`}>
          <caption>{text.months} · {total.currency}</caption>
          <thead>
            <tr>
              <th scope="col">{text.month}</th><th scope="col">{text.spending}</th><th scope="col">{text.income}</th>
              {unclassified ? <th scope="col">{text.toClassify}</th> : null}
            </tr>
          </thead>
          <tbody>
            {data.months.map((item) => {
              const value = item.currencies.find((entry) => entry.currency === total.currency);
              const money = (amount: string | undefined) => (amount === undefined ? text.noData : formatAmount(amount, total.currency, locale));
              const name = monthName(item.month, locale);
              return (
                <tr key={item.month}>
                  <th scope="row">
                    <button type="button" className="budget-link" aria-label={text.open(name)} onClick={() => { onOpenMonth(item.month); }}>{name}</button>
                  </th>
                  <td>{money(value?.net_spending)}</td>
                  <td>{money(value?.income)}</td>
                  {unclassified ? <td>{money(value?.unclassified_credit)}</td> : null}
                </tr>
              );
            })}
          </tbody>
        </table>
      );
    })
  ), [text, locale, onOpenMonth]);
  return (
    <section className="budget-month" aria-label={year}>
      <nav className="budget-month-nav">
        <button type="button" aria-label={text.previous} onClick={() => { setYear((value) => String(Number(value) - 1).padStart(4, "0")); }}>◀</button>
        <h3>{year}</h3>
        <button type="button" aria-label={text.next} disabled={year >= latest}
          onClick={() => { setYear((value) => (value < latest ? String(Number(value) + 1).padStart(4, "0") : value)); }}>▶</button>
      </nav>
      <ExportMenu text={text.exportMenu} filename={exportBasename(year)} periods={monthsOfYear(year)} filter={NO_FILTER}
        accounts={accounts} connections={connections} api={api} onAccessLoss={onAccessLoss} />
      <OverviewPanel overview={overview} period={year} current={year === latest} accounts={accounts} connections={connections}
        locale={locale} revision={revision} onAccessLoss={onAccessLoss} details={details} loadCategories={loadCategories} />
    </section>
  );
}
