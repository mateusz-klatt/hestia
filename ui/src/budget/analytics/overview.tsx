// Period overview of the owner's spending (2D). Currency totals stay server-computed. When categories have
// parents, each parent row adds its own amount to its descendants. Every response passes the access barrier.
import { Fragment, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { BudgetResult } from "../api";
import type { BudgetAccount, BudgetConnection } from "../model";
import { formatAmount } from "../model";
import { CategorySwatch } from "./category_badge";
import { buildCategoryColors, oneOffCategoryColor } from "./category_colors";
import type { CategoryColorInput } from "./category_colors";
import { groupCategoryTotals, hasCategoryParents, visibleCategoryRows } from "./category_tree";
import { accountName, isZero, oneOffRowKey, staleAccounts } from "./model";
import type { AnalyticsCategory, AnalyticsCategoryTotal, AnalyticsOverview, AnalyticsTotals } from "./model";

export const overviewLabels = {
  en: {
    spending: "Spending", gross: "before refunds", refunds: "refunds",
    income: "Income", unclassified: "To classify", unclassifiedHelp: "incoming payments not yet marked as income or refund",
    transfers: "Own transfers", transferOut: "out", transferIn: "in", transfersHelp: "not counted as spending or income",
    excluded: "Left out by you", excludedOut: "out", excludedIn: "in",
    categories: "Spending by category", uncategorised: "Uncategorised", archived: "archived", oneOff: "one-off",
    collapseCategory: (name: string) => `Collapse ${name}`, expandCategory: (name: string) => `Expand ${name}`,
    scope: (selected: number, excluded: number) =>
      `${String(selected)} accounts in the budget${excluded > 0 ? ` (${String(excluded)} excluded)` : ""} · change this on the Accounts tab`,
    current: "This period is still in progress.", partial: "Incomplete data",
    stale: "Incomplete data; these accounts are not up to date:",
    suggested: (count: number) => `Possible own transfers to review: ${String(count)}. Until you confirm them they count as spending and income.`,
    review: (count: number) => `Own-transfer decisions to check again after the bank data changed: ${String(count)}.`,
    boundary: (count: number) => `Transfers with one side on an excluded account: ${String(count)}. They count as ordinary spending or income.`,
    scanLimited: "The search for own transfers stopped at its limit; some possible pairs may be missing.",
    notCounted: "Not counted:", uncertain: "uncertain", unresolved: "unrecognised", importUnresolved: "unrecognised from import",
    empty: "No booked transactions in this period.", loading: "Loading totals…",
    error: "The totals could not be loaded. Try again.", account: "Account",
  },
  pl: {
    spending: "Wydatki", gross: "przed zwrotami", refunds: "zwroty",
    income: "Wpływy", unclassified: "Do sklasyfikowania", unclassifiedHelp: "wpływy jeszcze nieoznaczone jako dochód ani zwrot",
    transfers: "Przelewy własne", transferOut: "wyszło", transferIn: "weszło", transfersHelp: "nie liczą się do wydatków ani wpływów",
    excluded: "Pominięte przez Ciebie", excludedOut: "wyszło", excludedIn: "weszło",
    categories: "Wydatki według kategorii", uncategorised: "Bez kategorii", archived: "archiwalna", oneOff: "jednorazowe",
    collapseCategory: (name: string) => `Zwiń ${name}`, expandCategory: (name: string) => `Rozwiń ${name}`,
    scope: (selected: number, excluded: number) =>
      `Kont w budżecie: ${String(selected)}${excluded > 0 ? ` (wykluczone: ${String(excluded)})` : ""} · zmienisz to na karcie Konta`,
    current: "Ten okres jeszcze trwa.", partial: "Dane niepełne",
    stale: "Dane niepełne; te konta nie są aktualne:",
    suggested: (count: number) => `Możliwe przelewy własne do przejrzenia: ${String(count)}. Dopóki ich nie potwierdzisz, liczą się jako wydatki i wpływy.`,
    review: (count: number) => `Decyzje o przelewach własnych do ponownego sprawdzenia po zmianie danych z banku: ${String(count)}.`,
    boundary: (count: number) => `Przelewy z jedną stroną na koncie wykluczonym: ${String(count)}. Liczą się jako zwykłe wydatki lub wpływy.`,
    scanLimited: "Wyszukiwanie przelewów własnych zatrzymało się na limicie; części możliwych par może brakować.",
    notCounted: "Niewliczone:", uncertain: "niepewne", unresolved: "nierozpoznane", importUnresolved: "nierozpoznane z importu",
    empty: "Brak zaksięgowanych transakcji w tym okresie.", loading: "Wczytywanie sum…",
    error: "Nie udało się wczytać sum. Spróbuj ponownie.", account: "Konto",
  },
};
export type OverviewLabels = typeof overviewLabels.en;

type Shown = { state: "loading" } | { state: "error" } | { state: "ready"; data: AnalyticsOverview };

export interface OverviewProps {
  overview: (period: string) => Promise<BudgetResult<AnalyticsOverview>>;
  period: string;
  /** The period still running (today falls inside it). */
  current: boolean;
  accounts: readonly BudgetAccount[];
  connections?: readonly BudgetConnection[] | undefined;
  text: OverviewLabels;
  locale: string;
  /** Bumped after every completed budget reload or saved decision; the period is read again. */
  revision: number;
  onAccessLoss: (result: { ok: boolean; status?: number; error?: string }) => boolean;
  /** Currencies present in the shown period, for filters elsewhere. */
  onCurrencies?: ((currencies: string[]) => void) | undefined;
  /** Extra content drawn from the same answer (the year's month table). */
  details?: ((data: AnalyticsOverview) => ReactNode) | undefined;
  /** Owner categories. When any has a parent, the spending list becomes a collapsible tree. */
  loadCategories?: (() => Promise<BudgetResult<{ categories: AnalyticsCategory[] }>>) | undefined;
}

export function OverviewPanel({ overview, period, current, accounts, connections = [], text, locale, revision, onAccessLoss,
  onCurrencies, details, loadCategories }: OverviewProps) {
  const [shown, setShown] = useState<Shown>({ state: "loading" });
  const [links, setLinks] = useState<readonly AnalyticsCategory[]>([]);
  // Every period change or reload takes a new generation; an older answer can never replace a newer period.
  const generation = useRef(0);

  useEffect(() => {
    const token = ++generation.current;
    setShown({ state: "loading" });
    setLinks([]);
    void overview(period).then((result) => {
      if (onAccessLoss(result) || token !== generation.current) return;
      // Never show totals labelled with a period other than the one asked for.
      if (result.ok && result.data.period === period) {
        setShown({ state: "ready", data: result.data });
        onCurrencies?.(result.data.currencies.map((total) => total.currency));
      } else {
        setShown({ state: "error" });
      }
    });
    if (loadCategories !== undefined) {
      void loadCategories().then((result) => {
        if (onAccessLoss(result) || token !== generation.current) return;
        setLinks(result.ok ? result.data.categories : []);
      });
    }
  }, [overview, period, revision, onAccessLoss, onCurrencies, loadCategories]);
  useEffect(() => () => { generation.current += 1; }, []);

  const name = (id: string) => accountName(accounts, connections, id, text.account);

  if (shown.state === "loading") return <p className="budget-muted" role="status">{text.loading}</p>;
  if (shown.state === "error") return <p className="budget-notice" role="alert">{text.error}</p>;
  return (
    <>
      <OverviewBody data={shown.data} current={current} name={name} text={text} locale={locale} links={links} />
      {details?.(shown.data)}
    </>
  );
}

interface BodyProps {
  data: AnalyticsOverview; current: boolean; name: (id: string) => string; text: OverviewLabels; locale: string;
  links: readonly AnalyticsCategory[];
}

function OverviewBody({ data, current, name, text, locale, links }: BodyProps) {
  const { scope, transfer_summary: transfers } = data;
  const stale = staleAccounts(scope);
  const skipped = [[scope.uncertain_count, text.uncertain], [scope.unresolved_count, text.unresolved],
    [scope.import_unresolved_count, text.importUnresolved]] as const;
  const notCounted = skipped.filter(([count]) => count > 0).map(([count, label]) => `${String(count)} ${label}`);
  const notes = [
    transfers.suggested > 0 ? text.suggested(transfers.suggested) : null,
    transfers.review > 0 ? text.review(transfers.review) : null,
    transfers.boundary > 0 ? text.boundary(transfers.boundary) : null,
    transfers.scan_limited ? text.scanLimited : null,
  ].filter((note) => note !== null);
  return (
    <>
      <p className="budget-muted">{text.scope(scope.selected_accounts, scope.excluded_accounts)}</p>
      {current ? <p className="budget-muted">{text.current}</p> : null}
      {stale.length > 0 ? <p className="budget-notice" role="note">{text.stale} {stale.map(name).join(", ")}</p> : null}
      {notes.map((note) => <p key={note} className="budget-notice" role="note">{note}</p>)}
      {data.currencies.length === 0 ? <p className="budget-status">{text.empty}</p> : (
        <div className="budget-totals">
          {data.currencies.map((total) => (
            <CurrencyCard key={total.currency} total={total} partial={stale.length > 0} text={text} locale={locale}
              categories={data.categories.filter((item) => item.currency === total.currency)} links={links} />
          ))}
        </div>
      )}
      {notCounted.length > 0 ? <p className="budget-muted">{text.notCounted} {notCounted.join(" · ")}</p> : null}
    </>
  );
}

interface CardProps {
  total: AnalyticsTotals; categories: AnalyticsCategoryTotal[]; links: readonly AnalyticsCategory[];
  partial: boolean; text: OverviewLabels; locale: string;
}

function Amount({ value }: { value: string }) {
  return <span className="budget-amount">{value}</span>;
}

/** Label plus amount, repeated. Each pair stays together when it fits; a line break only happens at the separator. */
function Compound({ parts }: { parts: readonly (readonly [string, string])[] }) {
  return parts.map(([label, amount], index) => (
    <Fragment key={`${label}-${String(index)}`}>
      {index > 0 ? " · " : null}
      <span className="budget-total-part">{label} <Amount value={amount} /></span>
    </Fragment>
  ));
}

function CurrencyCard({ total, categories, links, partial, text, locale }: CardProps) {
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(() => new Set());
  const money = (amount: string) => formatAmount(amount, total.currency, locale);
  const tree = hasCategoryParents(links);
  const regular = categories.filter((item) => item.one_off !== true);
  const oneOffs = categories.filter((item) => item.one_off === true);
  const rows = tree ? visibleCategoryRows(groupCategoryTotals(regular, links, locale), collapsed) : [];
  const colorInputs: CategoryColorInput[] = [...links];
  const seenColorIds = new Set(links.map((item) => item.id));
  for (const item of categories) {
    if (item.category_id !== null && !seenColorIds.has(item.category_id)) {
      seenColorIds.add(item.category_id);
      colorInputs.push({ id: item.category_id, parent_id: null });
    }
  }
  const categoryColors = buildCategoryColors(colorInputs);
  const toggle = (id: string) => {
    setCollapsed((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };
  const transfers = !isZero(total.transfer_debits) || !isZero(total.transfer_credits);
  const excluded = !isZero(total.excluded_debits) || !isZero(total.excluded_credits);
  return (
    <article className="budget-total budget-total-currency" aria-label={total.currency}>
      <h4>{total.currency}{partial ? <span className="budget-warn" title={text.partial} aria-label={text.partial}> ⚠</span> : null}</h4>
      <dl>
        <dt>{text.spending}</dt>
        <dd><strong><Amount value={money(total.net_spending)} /></strong></dd>
        {isZero(total.refunds) ? null : (
          <dd className="budget-total-detail budget-total-split">
            <Compound parts={[[text.gross, money(total.expenses)], [text.refunds, money(total.refunds)]]} />
          </dd>
        )}
        <dt>{text.income}</dt>
        <dd><Amount value={money(total.income)} /></dd>
        {isZero(total.unclassified_credit) ? null : (
          <>
            <dt title={text.unclassifiedHelp}>{text.unclassified}</dt>
            <dd><Amount value={money(total.unclassified_credit)} /></dd>
          </>
        )}
        {transfers ? (
          <>
            <dt title={text.transfersHelp}>{text.transfers}</dt>
            <dd className="budget-total-split">
              <Compound parts={[[text.transferOut, money(total.transfer_debits)], [text.transferIn, money(total.transfer_credits)]]} />
            </dd>
          </>
        ) : null}
        {excluded ? (
          <>
            <dt>{text.excluded}</dt>
            <dd className="budget-total-split">
              <Compound parts={[[text.excludedOut, money(total.excluded_debits)], [text.excludedIn, money(total.excluded_credits)]]} />
            </dd>
          </>
        ) : null}
      </dl>
      {categories.length === 0 ? null : (
        <table className="budget-categories" aria-label={`${text.categories} ${total.currency}`}>
          <caption>{text.categories}</caption>
          <tbody>
            {tree ? rows.map((item) => {
              const id = item.category_id;
              const label = id === null ? text.uncategorised : item.name;
              const open = id === null || !collapsed.has(id);
              return (
                <tr key={id ?? "uncategorised"} data-depth={item.depth}
                  className={id === null ? "is-uncategorised" : undefined}>
                  <th scope="row">
                    {item.has_children && id !== null ? (
                      <button type="button" className="budget-tree-toggle" aria-expanded={open}
                        aria-label={open ? text.collapseCategory(label) : text.expandCategory(label)}
                        onClick={() => { toggle(id); }}>
                        {open ? "▾" : "▸"}
                      </button>
                    ) : null}
                    <CategorySwatch color={categoryColors.get(id)} />
                    {label}
                    {item.archived ? <small> ({text.archived})</small> : null}
                  </th>
                  <td>{money(item.net_spending)}</td>
                </tr>
              );
            }) : regular.map((item, index) => (
              <tr key={oneOffRowKey(item, index)}
                className={item.category_id === null ? "is-uncategorised" : undefined}>
                <th scope="row">
                  <CategorySwatch color={categoryColors.get(item.category_id)} />
                  {item.category_id === null ? text.uncategorised : item.name ?? text.uncategorised}
                  {item.archived ? <small> ({text.archived})</small> : null}
                </th>
                <td>{money(item.net_spending)}</td>
              </tr>
            ))}
            {oneOffs.map((item, index) => (
              <tr key={oneOffRowKey(item, index)} className="budget-one-off">
                <th scope="row">
                  <CategorySwatch color={oneOffCategoryColor(categoryColors, item.category_id)} />
                  {item.name ?? text.uncategorised}
                  <small className="budget-one-off-tag">{text.oneOff}</small>
                </th>
                <td>{money(item.net_spending)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </article>
  );
}
