// Month view of the owner's spending (2D). Shows only server-computed per-currency totals for the selected
// accounts; data live in component state only and every response passes the budget's access barrier.
import { useEffect, useRef, useState } from "react";
import type { BudgetResult } from "../api";
import type { BudgetAccount } from "../model";
import { accountLabel, formatAmount } from "../model";
import { currentMonth, isMonth, monthLabel, shiftMonth } from "./model";
import type { MonthSummary } from "./model";

export const monthLabels = {
  en: {
    previous: "Previous month", next: "Next month", income: "Income", spending: "Spending", net: "Net",
    entries: "entries", scope: (selected: number, excluded: number) =>
      `${String(selected)} accounts in the budget${excluded > 0 ? ` (${String(excluded)} excluded)` : ""} · change this on the Accounts tab`,
    current: "This month is still in progress.",
    stale: "Incomplete data; these accounts are not up to date:", partial: "Incomplete month",
    transfers: "Transfers between your own accounts and card repayments still count as income and spending.",
    notCounted: "Not counted:", pending: "pending", uncertain: "uncertain", unresolved: "unrecognised",
    empty: "No booked transactions in this month.", loading: "Loading the month…",
    error: "The month could not be loaded. Try again.", account: "Account",
  },
  pl: {
    previous: "Poprzedni miesiąc", next: "Następny miesiąc", income: "Wpływy", spending: "Wydatki", net: "Netto",
    entries: "poz.", scope: (selected: number, excluded: number) =>
      `Kont w budżecie: ${String(selected)}${excluded > 0 ? ` (wykluczone: ${String(excluded)})` : ""} · zmienisz to na karcie Konta`,
    current: "Ten miesiąc jeszcze trwa.",
    stale: "Dane niepełne; te konta nie są aktualne:", partial: "Miesiąc niepełny",
    transfers: "Przelewy między Twoimi kontami i spłaty kart są na razie liczone jako wpływy i wydatki.",
    notCounted: "Niewliczone:", pending: "oczekujące", uncertain: "niepewne", unresolved: "nierozpoznane",
    empty: "Brak zaksięgowanych transakcji w tym miesiącu.", loading: "Wczytywanie miesiąca…",
    error: "Nie udało się wczytać miesiąca. Spróbuj ponownie.", account: "Konto",
  },
};
export type MonthLabels = typeof monthLabels.en;

type Summary = { state: "loading" } | { state: "error" } | { state: "ready"; data: MonthSummary };

export interface MonthViewProps {
  summary: (month: string) => Promise<BudgetResult<MonthSummary>>;
  accounts: readonly BudgetAccount[];
  text: MonthLabels;
  locale: string;
  /** Bumped after every completed budget reload; the shown month is read again. */
  revision: number;
  onAccessLoss: (result: { ok: boolean; status?: number; error?: string }) => boolean;
  /** Injected clock for tests. */
  today?: () => Date;
}

export function MonthView({ summary, accounts, text, locale, revision, onAccessLoss, today = () => new Date() }: MonthViewProps) {
  const latest = currentMonth(today());
  const [month, setMonth] = useState(latest);
  const [shown, setShown] = useState<Summary>({ state: "loading" });
  // Every month change or reload takes a new generation; an older answer can never replace a newer month.
  const generation = useRef(0);

  useEffect(() => {
    const token = ++generation.current;
    setShown({ state: "loading" });
    void summary(month).then((result) => {
      if (onAccessLoss(result) || token !== generation.current) return;
      // Never show totals labelled with a month other than the one asked for.
      setShown(result.ok && result.data.month === month ? { state: "ready", data: result.data } : { state: "error" });
    });
  }, [summary, month, revision, onAccessLoss]);
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
      {shown.state === "ready" ? <MonthBody data={shown.data} current={month === latest} name={name} text={text} locale={locale} /> : null}
    </section>
  );
}

interface BodyProps { data: MonthSummary; current: boolean; name: (id: string) => string; text: MonthLabels; locale: string }

function MonthBody({ data, current, name, text, locale }: BodyProps) {
  const skipped = [[data.not_counted.pending, text.pending], [data.not_counted.uncertain, text.uncertain],
    [data.not_counted.unresolved, text.unresolved]] as const;
  const notCounted = skipped.filter(([count]) => count > 0).map(([count, label]) => `${String(count)} ${label}`);
  return (
    <>
      <p className="budget-muted">{text.scope(data.accounts.selected, data.accounts.excluded)}</p>
      {current ? <p className="budget-muted">{text.current}</p> : null}
      {data.stale_account_ids.length > 0 ? (
        <p className="budget-notice" role="note">{text.stale} {data.stale_account_ids.map(name).join(", ")}</p>
      ) : null}
      {data.currencies.length === 0 ? <p className="budget-status">{text.empty}</p> : (
        <div className="budget-totals">
          {data.currencies.map((total) => (
            <article className="budget-total" key={total.currency} aria-label={total.currency}>
              <h4>{total.currency}{data.partial ? <span className="budget-warn" title={text.partial} aria-label={text.partial}> ⚠</span> : null}</h4>
              <dl>
                <dt>{text.income}</dt>
                <dd>{formatAmount(total.income, total.currency, locale)} <small>{total.income_count} {text.entries}</small></dd>
                <dt>{text.spending}</dt>
                <dd>{formatAmount(total.spending, total.currency, locale)} <small>{total.spending_count} {text.entries}</small></dd>
                <dt>{text.net}</dt>
                <dd><strong>{formatAmount(total.net, total.currency, locale)}</strong></dd>
              </dl>
            </article>
          ))}
        </div>
      )}
      <p className="budget-muted">{text.transfers}</p>
      {notCounted.length > 0 ? <p className="budget-muted">{text.notCounted} {notCounted.join(" · ")}</p> : null}
    </>
  );
}
