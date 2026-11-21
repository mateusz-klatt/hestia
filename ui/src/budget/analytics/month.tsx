// The month view (2D): one calendar month's overview and its entries. A saved correction re-reads both, so the
// totals shown always come from the server after the change.
import { useCallback, useState } from "react";
import type { BudgetApi } from "../api";
import type { BudgetAccount } from "../model";
import { currentMonth, isMonth, monthLabel, shiftMonth } from "./model";
import { OverviewPanel, overviewLabels } from "./overview";
import type { OverviewLabels } from "./overview";
import { TransactionsPanel, transactionLabels } from "./transactions";
import type { TransactionLabels } from "./transactions";
import { TransfersPanel, transferLabels } from "./transfers";
import type { TransferLabels } from "./transfers";

export const monthLabels = {
  en: { previous: "Previous month", next: "Next month", overview: overviewLabels.en, transfers: transferLabels.en,
    entries: transactionLabels.en },
  pl: { previous: "Poprzedni miesiąc", next: "Następny miesiąc", overview: overviewLabels.pl, transfers: transferLabels.pl,
    entries: transactionLabels.pl },
};
export interface MonthLabels {
  previous: string; next: string; overview: OverviewLabels; transfers: TransferLabels; entries: TransactionLabels;
}

export interface MonthViewProps {
  api: Pick<BudgetApi, "overview" | "analyticsTransactions" | "categories" | "annotate" | "transfers" | "decideTransfer">;
  accounts: readonly BudgetAccount[];
  text: MonthLabels;
  locale: string;
  /** Bumped after every completed budget reload. */
  revision: number;
  onAccessLoss: (result: { ok: boolean; status?: number; error?: string }) => boolean;
  /** The month to open first ("YYYY-MM"); the current month when absent or in the future. */
  initialMonth?: string | undefined;
  /** Injected clock for tests. */
  today?: () => Date;
}

export function MonthView({ api, accounts, text, locale, revision, onAccessLoss, initialMonth,
  today = () => new Date() }: MonthViewProps) {
  const latest = currentMonth(today());
  const [month, setMonth] = useState(initialMonth !== undefined && isMonth(initialMonth) && initialMonth < latest ? initialMonth : latest);
  const [changes, setChanges] = useState(0);
  const [currencies, setCurrencies] = useState<string[]>([]);
  const changed = useCallback(() => { setChanges((current) => current + 1); }, []);
  const overview = useCallback((period: string) => api.overview(period), [api]);
  return (
    <section className="budget-month" aria-label={monthLabel(month, locale)}>
      <nav className="budget-month-nav">
        <button type="button" aria-label={text.previous} onClick={() => { setMonth((value) => shiftMonth(value, -1)); }}>◀</button>
        <h3>{monthLabel(month, locale)}</h3>
        <button type="button" aria-label={text.next} disabled={month >= latest}
          onClick={() => { setMonth((value) => (isMonth(value) && value < latest ? shiftMonth(value, 1) : value)); }}>▶</button>
      </nav>
      <OverviewPanel overview={overview} period={month} current={month === latest} accounts={accounts} text={text.overview}
        locale={locale} revision={revision + changes} onAccessLoss={onAccessLoss} onCurrencies={setCurrencies} />
      <TransfersPanel api={api} period={month} accounts={accounts} text={text.transfers} locale={locale}
        revision={revision + changes} onAccessLoss={onAccessLoss} onChanged={changed} />
      <TransactionsPanel api={api} period={month} currencies={currencies} accounts={accounts} text={text.entries}
        locale={locale} revision={revision + changes} onAccessLoss={onAccessLoss} onChanged={changed} />
    </section>
  );
}
