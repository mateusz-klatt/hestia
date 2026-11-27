// The month view (2D): one calendar month's overview and its entries. A saved correction re-reads both, so the
// totals shown always come from the server after the change.
import { useCallback, useState } from "react";
import { t } from "../../i18n";
import type { BudgetApi } from "../api";
import type { BudgetAccount, BudgetConnection } from "../model";
import type { ReceiptsApi } from "../receipts_api";
import { currentMonth, isMonth, monthLabel, shiftMonth } from "./model";
import type { RuleCandidate } from "./model";
import { OverviewPanel } from "./overview";
import { TransactionsPanel } from "./transactions";
import { TransfersPanel } from "./transfers";

export interface MonthViewProps {
  api: Pick<BudgetApi,
    "overview" | "analyticsTransactions" | "analyticsTransactionsByKeys" | "categories" | "annotate" | "transfers"
    | "decideTransfer">;
  receiptsList?: ReceiptsApi["receiptsList"] | undefined;
  accounts: readonly BudgetAccount[];
  connections?: readonly BudgetConnection[] | undefined;
  locale: string;
  /** Bumped after every completed budget reload. */
  revision: number;
  onAccessLoss: (result: { ok: boolean; status?: number; error?: string }) => boolean;
  /** The month to open first ("YYYY-MM"); the current month when absent or in the future. */
  initialMonth?: string | undefined;
  /** Initial currency filter when opened from Monthly budget. */
  initialCurrency?: string | undefined;
  /** Exact server-computed `coverage.pending_row_keys` to filter Transactions on arrival and scroll into view. */
  initialProvisionalKeys?: readonly string[] | undefined;
  /** Server-reported total pending count (`coverage.pending_transfer_rows`) for truncation notes. */
  initialProvisionalTotal?: number | undefined;
  /** Whether `initialProvisionalKeys` was capped below `initialProvisionalTotal`. */
  initialProvisionalTruncated?: boolean | undefined;
  /** Injected clock for tests. */
  today?: () => Date;
  onCreateRule?: ((candidate: RuleCandidate) => void) | undefined;
}

export function MonthView({ api, receiptsList, accounts, connections = [], locale, revision, onAccessLoss, initialMonth,
  initialCurrency, initialProvisionalKeys, initialProvisionalTotal, initialProvisionalTruncated,
  today = () => new Date(), onCreateRule }: MonthViewProps) {
  const latest = currentMonth(today());
  const initialPeriod = initialMonth !== undefined && isMonth(initialMonth) && initialMonth < latest ? initialMonth : latest;
  const [month, setMonth] = useState(initialPeriod);
  const [changes, setChanges] = useState(0);
  const [currencies, setCurrencies] = useState<string[]>([]);
  const changed = useCallback(() => { setChanges((current) => current + 1); }, []);
  const overview = useCallback((period: string) => api.overview(period), [api]);
  const loadCategories = useCallback(() => api.categories(), [api]);
  const sameInitialPeriod = month === initialPeriod;
  const provisionalKeys = sameInitialPeriod ? initialProvisionalKeys : undefined;
  const provisionalTotal = sameInitialPeriod ? initialProvisionalTotal : undefined;
  const provisionalTruncated = sameInitialPeriod ? initialProvisionalTruncated : undefined;
  return (
    <section className="budget-month" aria-label={monthLabel(month, locale)}>
      <nav className="budget-month-nav">
        <button type="button" aria-label={t("budget.month.previous")} onClick={() => { setMonth((value) => shiftMonth(value, -1)); }}>◀</button>
        <h3>{monthLabel(month, locale)}</h3>
        <button type="button" aria-label={t("budget.month.next")} disabled={month >= latest}
          onClick={() => { setMonth((value) => (isMonth(value) && value < latest ? shiftMonth(value, 1) : value)); }}>▶</button>
      </nav>
      <OverviewPanel overview={overview} period={month} current={month === latest} accounts={accounts} connections={connections}
        locale={locale} revision={revision + changes} onAccessLoss={onAccessLoss} onCurrencies={setCurrencies} loadCategories={loadCategories} />
      <TransfersPanel api={api} period={month} accounts={accounts} connections={connections} locale={locale}
        revision={revision + changes} onAccessLoss={onAccessLoss} onChanged={changed} />
      <TransactionsPanel api={api} receiptsList={receiptsList} period={month} currencies={currencies} accounts={accounts} connections={connections}
        locale={locale} revision={revision + changes} onAccessLoss={onAccessLoss} onChanged={changed}
        initialCurrency={initialCurrency} initialProvisionalKeys={provisionalKeys}
        initialProvisionalTotal={provisionalTotal} initialProvisionalTruncated={provisionalTruncated}
        focusOnMount={initialProvisionalKeys !== undefined} onCreateRule={onCreateRule} />
    </section>
  );
}
