// Month overview of the owner's spending (2D). Shows only server-computed per-currency totals for the selected
// accounts; data live in component state only and every response passes the budget's access barrier.
import { useEffect, useRef, useState } from "react";
import type { BudgetResult } from "../api";
import type { BudgetAccount } from "../model";
import { accountLabel, formatAmount } from "../model";
import { currentMonth, isMonth, isZero, monthLabel, shiftMonth } from "./model";
import type { AnalyticsCategoryTotal, AnalyticsOverview, AnalyticsTotals } from "./model";

export const overviewLabels = {
  en: {
    previous: "Previous month", next: "Next month", spending: "Spending", gross: "gross", refunds: "refunds",
    income: "Income", unclassified: "To classify", unclassifiedHelp: "incoming payments not yet marked as income or refund",
    internal: "Own transfers", internalOut: "out", internalIn: "in", internalHelp: "not counted as spending or income",
    categories: "Spending by category", uncategorised: "Uncategorised", entries: "entries",
    scope: (selected: number, excluded: number) =>
      `${String(selected)} accounts in the budget${excluded > 0 ? ` (${String(excluded)} excluded)` : ""} · change this on the Accounts tab`,
    current: "This month is still in progress.", partial: "Incomplete month",
    stale: "Incomplete data; these accounts are not up to date:",
    suggested: (count: number) => `Possible own transfers to review: ${String(count)}. Until you confirm them they count as spending and income.`,
    review: (count: number) => `Own-transfer decisions to check again after the bank data changed: ${String(count)}.`,
    notCounted: "Not counted:", pending: "pending", uncertain: "uncertain", unresolved: "unrecognised",
    empty: "No booked transactions in this month.", loading: "Loading the month…",
    error: "The month could not be loaded. Try again.", account: "Account",
  },
  pl: {
    previous: "Poprzedni miesiąc", next: "Następny miesiąc", spending: "Wydatki", gross: "brutto", refunds: "zwroty",
    income: "Wpływy", unclassified: "Do sklasyfikowania", unclassifiedHelp: "wpływy jeszcze nieoznaczone jako dochód ani zwrot",
    internal: "Przelewy własne", internalOut: "wyszło", internalIn: "weszło", internalHelp: "nie liczą się do wydatków ani wpływów",
    categories: "Wydatki według kategorii", uncategorised: "Bez kategorii", entries: "poz.",
    scope: (selected: number, excluded: number) =>
      `Kont w budżecie: ${String(selected)}${excluded > 0 ? ` (wykluczone: ${String(excluded)})` : ""} · zmienisz to na karcie Konta`,
    current: "Ten miesiąc jeszcze trwa.", partial: "Miesiąc niepełny",
    stale: "Dane niepełne; te konta nie są aktualne:",
    suggested: (count: number) => `Możliwe przelewy własne do przejrzenia: ${String(count)}. Dopóki ich nie potwierdzisz, liczą się jako wydatki i wpływy.`,
    review: (count: number) => `Decyzje o przelewach własnych do ponownego sprawdzenia po zmianie danych z banku: ${String(count)}.`,
    notCounted: "Niewliczone:", pending: "oczekujące", uncertain: "niepewne", unresolved: "nierozpoznane",
    empty: "Brak zaksięgowanych transakcji w tym miesiącu.", loading: "Wczytywanie miesiąca…",
    error: "Nie udało się wczytać miesiąca. Spróbuj ponownie.", account: "Konto",
  },
};
export type OverviewLabels = typeof overviewLabels.en;

type Shown = { state: "loading" } | { state: "error" } | { state: "ready"; data: AnalyticsOverview };

export interface OverviewProps {
  overview: (period: string) => Promise<BudgetResult<AnalyticsOverview>>;
  accounts: readonly BudgetAccount[];
  text: OverviewLabels;
  locale: string;
  /** Bumped after every completed budget reload; the shown month is read again. */
  revision: number;
  onAccessLoss: (result: { ok: boolean; status?: number; error?: string }) => boolean;
  /** Injected clock for tests. */
  today?: () => Date;
}

export function MonthOverview({ overview, accounts, text, locale, revision, onAccessLoss, today = () => new Date() }: OverviewProps) {
  const latest = currentMonth(today());
  const [month, setMonth] = useState(latest);
  const [shown, setShown] = useState<Shown>({ state: "loading" });
  // Every month change or reload takes a new generation; an older answer can never replace a newer month.
  const generation = useRef(0);

  useEffect(() => {
    const token = ++generation.current;
    setShown({ state: "loading" });
    void overview(month).then((result) => {
      if (onAccessLoss(result) || token !== generation.current) return;
      // Never show totals labelled with a period other than the one asked for.
      setShown(result.ok && result.data.period === month ? { state: "ready", data: result.data } : { state: "error" });
    });
  }, [overview, month, revision, onAccessLoss]);
  useEffect(() => () => { generation.current += 1; }, []);

  const name = (id: string) => {
    const account = accounts.find((item) => item.id === id);
    if (account === undefined) return text.account;
    return accountLabel(account, accounts.filter((item) => item.connection === account.connection).indexOf(account), text.account);
  };

  return (
    <section className="budget-month" aria-label={monthLabel(month, locale)}>
      <nav className="budget-month-nav">
        <button type="button" aria-label={text.previous} onClick={() => { setMonth((value) => shiftMonth(value, -1)); }}>◀</button>
        <h3>{monthLabel(month, locale)}</h3>
        <button type="button" aria-label={text.next} disabled={month >= latest}
          onClick={() => { setMonth((value) => (isMonth(value) && value < latest ? shiftMonth(value, 1) : value)); }}>▶</button>
      </nav>
      {shown.state === "loading" ? <p className="budget-muted" role="status">{text.loading}</p> : null}
      {shown.state === "error" ? <p className="budget-notice" role="alert">{text.error}</p> : null}
      {shown.state === "ready" ? <OverviewBody data={shown.data} current={month === latest} name={name} text={text} locale={locale} /> : null}
    </section>
  );
}

interface BodyProps { data: AnalyticsOverview; current: boolean; name: (id: string) => string; text: OverviewLabels; locale: string }

function OverviewBody({ data, current, name, text, locale }: BodyProps) {
  const { scope, transfer_summary: transfers } = data;
  const skipped = [[scope.pending, text.pending], [scope.uncertain, text.uncertain], [scope.unresolved, text.unresolved]] as const;
  const notCounted = skipped.filter(([count]) => count > 0).map(([count, label]) => `${String(count)} ${label}`);
  return (
    <>
      <p className="budget-muted">{text.scope(scope.selected, scope.excluded)}</p>
      {current ? <p className="budget-muted">{text.current}</p> : null}
      {scope.stale_account_ids.length > 0 ? (
        <p className="budget-notice" role="note">{text.stale} {scope.stale_account_ids.map(name).join(", ")}</p>
      ) : null}
      {transfers.suggested > 0 ? <p className="budget-notice" role="note">{text.suggested(transfers.suggested)}</p> : null}
      {transfers.review > 0 ? <p className="budget-notice" role="note">{text.review(transfers.review)}</p> : null}
      {data.totals.length === 0 ? <p className="budget-status">{text.empty}</p> : (
        <div className="budget-totals">
          {data.totals.map((total) => (
            <CurrencyCard key={total.currency} total={total} partial={scope.partial} text={text} locale={locale}
              categories={data.categories.filter((item) => item.currency === total.currency)} />
          ))}
        </div>
      )}
      {notCounted.length > 0 ? <p className="budget-muted">{text.notCounted} {notCounted.join(" · ")}</p> : null}
    </>
  );
}

interface CardProps { total: AnalyticsTotals; categories: AnalyticsCategoryTotal[]; partial: boolean; text: OverviewLabels; locale: string }

function CurrencyCard({ total, categories, partial, text, locale }: CardProps) {
  const money = (amount: string) => formatAmount(amount, total.currency, locale);
  const internal = !isZero(total.internal_in) || !isZero(total.internal_out);
  return (
    <article className="budget-total" aria-label={total.currency}>
      <h4>{total.currency}{partial ? <span className="budget-warn" title={text.partial} aria-label={text.partial}> ⚠</span> : null}</h4>
      <dl>
        <dt>{text.spending}</dt>
        <dd><strong>{money(total.net_spending)}</strong></dd>
        {isZero(total.refunds) ? null : (
          <dd className="budget-total-detail">{text.gross} {money(total.spending_gross)} · {text.refunds} {money(total.refunds)}</dd>
        )}
        <dt>{text.income}</dt>
        <dd>{money(total.income)}</dd>
        {isZero(total.unclassified_credit) ? null : (
          <>
            <dt title={text.unclassifiedHelp}>{text.unclassified}</dt>
            <dd>{money(total.unclassified_credit)}</dd>
          </>
        )}
        {internal ? (
          <>
            <dt title={text.internalHelp}>{text.internal}</dt>
            <dd>{text.internalOut} {money(total.internal_out)} · {text.internalIn} {money(total.internal_in)}</dd>
          </>
        ) : null}
      </dl>
      {categories.length === 0 ? null : (
        <table className="budget-categories" aria-label={`${text.categories} ${total.currency}`}>
          <caption>{text.categories}</caption>
          <tbody>
            {categories.map((item) => (
              <tr key={item.category_id ?? "uncategorised"} className={item.category_id === null ? "is-uncategorised" : undefined}>
                <th scope="row">{item.category_id === null ? text.uncategorised : item.name ?? text.uncategorised}</th>
                <td>{money(item.amount)}</td>
                <td><small>{item.count} {text.entries}</small></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </article>
  );
}
