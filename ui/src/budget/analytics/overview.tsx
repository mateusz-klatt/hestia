// Period overview of the owner's spending (2D). Currency totals stay server-computed. When categories have
// parents, each parent row adds its own amount to its descendants. Every response passes the access barrier.
import { Fragment, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { BudgetResult } from "../api";
import type { BudgetAccount, BudgetConnection } from "../model";
import { formatAmount } from "../model";
import { t, tPlural } from "../../i18n";
import { CategorySwatch } from "./category_badge";
import { buildCategoryColors, oneOffCategoryColor } from "./category_colors";
import type { CategoryColorInput } from "./category_colors";
import { groupCategoryTotals, hasCategoryParents, visibleCategoryRows } from "./category_tree";
import { accountName, isZero, oneOffRowKey, staleAccounts } from "./model";
import type { AnalyticsCategory, AnalyticsCategoryTotal, AnalyticsOverview, AnalyticsTotals } from "./model";

type Shown = { state: "loading" } | { state: "error" } | { state: "ready"; data: AnalyticsOverview };

export interface OverviewProps {
  overview: (period: string) => Promise<BudgetResult<AnalyticsOverview>>;
  period: string;
  /** The period still running (today falls inside it). */
  current: boolean;
  accounts: readonly BudgetAccount[];
  connections?: readonly BudgetConnection[] | undefined;
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

export function OverviewPanel({ overview, period, current, accounts, connections = [], locale, revision, onAccessLoss,
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

  const name = (id: string) => accountName(accounts, connections, id, t("budget.overview.account"));

  if (shown.state === "loading") return <p className="budget-muted" role="status">{t("budget.overview.loading")}</p>;
  if (shown.state === "error") return <p className="budget-notice" role="alert">{t("budget.overview.error")}</p>;
  return (
    <>
      <OverviewBody data={shown.data} current={current} name={name} locale={locale} links={links} />
      {details?.(shown.data)}
    </>
  );
}

interface BodyProps {
  data: AnalyticsOverview; current: boolean; name: (id: string) => string; locale: string;
  links: readonly AnalyticsCategory[];
}

function OverviewBody({ data, current, name, locale, links }: BodyProps) {
  const { scope, transfer_summary: transfers } = data;
  const stale = staleAccounts(scope);
  const notCounted = [
    scope.uncertain_count > 0 ? tPlural("budget.overview.uncertainCount", scope.uncertain_count) : null,
    scope.unresolved_count > 0 ? tPlural("budget.overview.unresolvedCount", scope.unresolved_count) : null,
    scope.import_unresolved_count > 0 ? tPlural("budget.overview.importUnresolvedCount", scope.import_unresolved_count) : null,
  ].filter((item): item is string => item !== null);
  const notes = [
    transfers.suggested > 0 ? t("budget.overview.suggested", { count: transfers.suggested }) : null,
    transfers.review > 0 ? t("budget.overview.review", { count: transfers.review }) : null,
    transfers.boundary > 0 ? t("budget.overview.boundary", { count: transfers.boundary }) : null,
    transfers.scan_limited ? t("budget.overview.scanLimited") : null,
  ].filter((note) => note !== null);
  const scopeText = scope.excluded_accounts > 0
    ? t("budget.overview.scopeWithExcluded", { selected: scope.selected_accounts, excluded: scope.excluded_accounts })
    : t("budget.overview.scopeAll", { selected: scope.selected_accounts });
  return (
    <>
      <p className="budget-muted">{scopeText}</p>
      {current ? <p className="budget-muted">{t("budget.overview.current")}</p> : null}
      {stale.length > 0 ? <p className="budget-notice" role="note">{t("budget.overview.stale")} {stale.map(name).join(", ")}</p> : null}
      {notes.map((note) => <p key={note} className="budget-notice" role="note">{note}</p>)}
      {data.currencies.length === 0 ? <p className="budget-status">{t("budget.overview.empty")}</p> : (
        <div className="budget-totals">
          {data.currencies.map((total) => (
            <CurrencyCard key={total.currency} total={total} partial={stale.length > 0} locale={locale}
              categories={data.categories.filter((item) => item.currency === total.currency)} links={links} />
          ))}
        </div>
      )}
      {notCounted.length > 0 ? <p className="budget-muted">{t("budget.overview.notCounted", { items: notCounted.join(" · ") })}</p> : null}
    </>
  );
}

interface CardProps {
  total: AnalyticsTotals; categories: AnalyticsCategoryTotal[]; links: readonly AnalyticsCategory[];
  partial: boolean; locale: string;
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

function CurrencyCard({ total, categories, links, partial, locale }: CardProps) {
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
      <h4>{total.currency}{partial ? <span className="budget-warn" title={t("budget.overview.partial")} aria-label={t("budget.overview.partial")}> ⚠</span> : null}</h4>
      <dl>
        <dt>{t("budget.overview.spending")}</dt>
        <dd><strong><Amount value={money(total.net_spending)} /></strong></dd>
        {isZero(total.refunds) ? null : (
          <dd className="budget-total-detail budget-total-split">
            <Compound parts={[[t("budget.overview.gross"), money(total.expenses)], [t("budget.overview.refunds"), money(total.refunds)]]} />
          </dd>
        )}
        <dt>{t("budget.overview.income")}</dt>
        <dd><Amount value={money(total.income)} /></dd>
        {isZero(total.unclassified_credit) ? null : (
          <>
            <dt title={t("budget.overview.unclassifiedHelp")}>{t("budget.overview.unclassified")}</dt>
            <dd><Amount value={money(total.unclassified_credit)} /></dd>
          </>
        )}
        {transfers ? (
          <>
            <dt title={t("budget.overview.transfersHelp")}>{t("budget.overview.transfers")}</dt>
            <dd className="budget-total-split">
              <Compound parts={[[t("budget.overview.transferOut"), money(total.transfer_debits)], [t("budget.overview.transferIn"), money(total.transfer_credits)]]} />
            </dd>
          </>
        ) : null}
        {excluded ? (
          <>
            <dt>{t("budget.overview.excluded")}</dt>
            <dd className="budget-total-split">
              <Compound parts={[[t("budget.overview.excludedOut"), money(total.excluded_debits)], [t("budget.overview.excludedIn"), money(total.excluded_credits)]]} />
            </dd>
          </>
        ) : null}
      </dl>
      {categories.length === 0 ? null : (
        <table className="budget-categories" aria-label={t("budget.overview.categoriesForCurrency", { currency: total.currency })}>
          <caption>{t("budget.overview.categories")}</caption>
          <tbody>
            {tree ? rows.map((item) => {
              const id = item.category_id;
              const label = id === null ? t("budget.overview.uncategorised") : item.name;
              const open = id === null || !collapsed.has(id);
              return (
                <tr key={id ?? "uncategorised"} data-depth={item.depth}
                  className={id === null ? "is-uncategorised" : undefined}>
                  <th scope="row">
                    {item.has_children && id !== null ? (
                      <button type="button" className="budget-tree-toggle" aria-expanded={open}
                        aria-label={open ? t("budget.overview.collapseCategory", { name: label }) : t("budget.overview.expandCategory", { name: label })}
                        onClick={() => { toggle(id); }}>
                        {open ? "▾" : "▸"}
                      </button>
                    ) : null}
                    <CategorySwatch color={categoryColors.get(id)} />
                    {label}
                    {item.archived ? <small> ({t("budget.overview.archived")})</small> : null}
                  </th>
                  <td>{money(item.net_spending)}</td>
                </tr>
              );
            }) : regular.map((item, index) => (
              <tr key={oneOffRowKey(item, index)}
                className={item.category_id === null ? "is-uncategorised" : undefined}>
                <th scope="row">
                  <CategorySwatch color={categoryColors.get(item.category_id)} />
                  {item.category_id === null ? t("budget.overview.uncategorised") : item.name ?? t("budget.overview.uncategorised")}
                  {item.archived ? <small> ({t("budget.overview.archived")})</small> : null}
                </th>
                <td>{money(item.net_spending)}</td>
              </tr>
            ))}
            {oneOffs.map((item, index) => (
              <tr key={oneOffRowKey(item, index)} className="budget-one-off">
                <th scope="row">
                  <CategorySwatch color={oneOffCategoryColor(categoryColors, item.category_id)} />
                  {item.name ?? t("budget.overview.uncategorised")}
                  <small className="budget-one-off-tag">{t("budget.overview.oneOff")}</small>
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
