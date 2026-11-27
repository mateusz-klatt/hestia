import { useEffect, useRef, useState } from "react";
import { t, tPlural } from "../../i18n";
import type { BudgetApi } from "../api";
import { formatAmount, formatCents, formatEstimate, roundToCents } from "../model";
import type { BudgetAccount, BudgetConnection } from "../model";
import { FORECAST_CONFIDENCE_KEYS, accountName, isScheduledForecastItem, isZero, labelOf, monthLabel, mortgageCurrencies,
  parseScaledInt, validCurrency } from "./model";
import type { AnalyticsForecast, AnalyticsForecastCurrencyTotal, AnalyticsForecastMonth } from "./model";

type CategoryNames = ReadonlyMap<string, string> | "error" | "loading";

const horizons = [1, 3, 6, 12] as const;
type Horizon = typeof horizons[number];

type Shown = { state: "loading" | "limit" | "error" } | { state: "ready"; data: AnalyticsForecast };

interface ForecastViewProps {
  api: Pick<BudgetApi, "forecast" | "mortgages" | "categories">;
  accounts: readonly BudgetAccount[];
  connections?: readonly BudgetConnection[];
  locale: string;
  revision?: number;
  onAccessLoss: (result: { ok: boolean; status?: number; error?: string }) => boolean;
  onOpenMortgages: () => void;
}

function currencyOptions(codes: readonly string[]): string[] {
  return [...new Set(codes.filter(validCurrency).map((code) => code.trim().toUpperCase()))].sort();
}

function baselineConflictNote(data: AnalyticsForecast, names: CategoryNames, locale: string): string | null {
  const conflicts = data.summary.baseline_conflicts ?? [];
  if (conflicts.length === 0 || names === "loading") return null;
  if (names === "error") return t("budget.forecast.baselineConflictUnknown");
  const seen = new Set<string>();
  const labels: string[] = [];
  let unnamed = false;
  for (const row of conflicts) {
    if (seen.has(row.category_id)) continue;
    seen.add(row.category_id);
    const name = names.get(row.category_id)?.trim() ?? "";
    if (name !== "") labels.push(name);
    else unnamed = true;
  }
  if (unnamed) labels.push(t("budget.forecast.baselineConflictUnnamed"));
  if (labels.length === 0) return t("budget.forecast.baselineConflictUnknown");
  labels.sort((left, right) => left.localeCompare(right, locale));
  return t("budget.forecast.baselineConflict", { names: labels.join(", ") });
}

function ForecastSummary({ data, accounts, connections, locale, categoryNames, onOpenMortgages }: {
  data: AnalyticsForecast;
  accounts: readonly BudgetAccount[];
  connections: readonly BudgetConnection[];
  locale: string;
  categoryNames: CategoryNames;
  onOpenMortgages: () => void;
}) {
  const { summary } = data;
  const skipped = baselineConflictNote(data, categoryNames, locale);
  const staleAccountsLabel = tPlural("budget.forecast.staleAccounts", summary.stale_accounts);
  const ledgerAsOf = data.ledger_as_of === null
    ? t("budget.forecast.ledgerAsOfEmpty")
    : t("budget.forecast.ledgerAsOfDate", { date: data.ledger_as_of });
  return (
    <>
      {skipped === null ? null : <p className="budget-notice" role="note">{skipped}</p>}
      <ul className="budget-facts">
        <li>{t("budget.forecast.evaluatedFor", { date: data.evaluation_date, timezone: data.evaluation_timezone })}</li>
        <li>{ledgerAsOf}</li>
      </ul>
      {data.freshness_stale ? <p className="budget-notice" role="note">{t("budget.forecast.freshnessStale")}</p> : null}
      <ul className="budget-facts">
        <li>{staleAccountsLabel}</li>
        <li className={summary.stale_recurring_count > 0 ? "budget-warn" : undefined}>
          {tPlural("budget.forecast.staleRecurring", summary.stale_recurring_count)}
        </li>
        <li className={summary.lapsed_recurring_count > 0 ? "budget-warn" : undefined}>
          {tPlural("budget.forecast.lapsedRecurring", summary.lapsed_recurring_count)}
        </li>
        <li className={summary.overdue_mortgage_schedule_count > 0 ? "budget-warn" : undefined}>
          {tPlural("budget.forecast.overdueSchedules", summary.overdue_mortgage_schedule_count)}
          {summary.overdue_mortgage_schedule_count > 0 ? (
            <>{" · "}<button type="button" className="budget-link" onClick={onOpenMortgages}>{t("budget.forecast.openMortgages")}</button></>
          ) : null}
        </li>
      </ul>
      {data.freshness_stale && data.stale_account_ids.length > 0 ? (
        <ul className="budget-facts" aria-label={staleAccountsLabel}>
          {data.stale_account_ids.map((id) => <li key={id}>{accountName(accounts, connections, id, t("budget.forecast.account"))}</li>)}
        </ul>
      ) : null}
      {data.stale_account_ids_truncated ? (
        <p className="budget-notice" role="note">
          {tPlural("budget.forecast.staleOverflow", summary.stale_accounts - data.stale_account_ids.length)}
        </p>
      ) : null}
    </>
  );
}

function decimalsSumTo(a: string, b: string, sum: string): boolean {
  const pa = parseScaledInt(a);
  const pb = parseScaledInt(b);
  const ps = parseScaledInt(sum);
  if (pa === null || pb === null || ps === null) return false;
  const scale = Math.max(pa.scale, pb.scale, ps.scale);
  const toUnits = (parsed: NonNullable<ReturnType<typeof parseScaledInt>>) =>
    (parsed.negative ? -parsed.coeff : parsed.coeff) * (10n ** BigInt(scale - parsed.scale));
  return toUnits(pa) + toUnits(pb) === toUnits(ps);
}

/** Decimal string for a forecast currency total: purely scheduled totals stay exact. When estimates are
 *  included (`estimated_total` nonzero), round the mixed total as `round(scheduled_total) + round(estimated_total)`
 *  so chart bar widths, the totals table, and the month summary all match displayed `Scheduled + Estimated`
 *  (falling back to `round(total)` when the fields do not sum identically). */
function forecastTotalDecimal(entry: AnalyticsForecastCurrencyTotal | undefined): string {
  if (entry === undefined) return "0";
  if (isZero(entry.estimated_total)) return entry.total;
  if (decimalsSumTo(entry.scheduled_total, entry.estimated_total, entry.total)) {
    const scheduledCents = roundToCents(entry.scheduled_total);
    const estimatedCents = roundToCents(entry.estimated_total);
    if (scheduledCents !== null && estimatedCents !== null) {
      return formatCents(scheduledCents + estimatedCents);
    }
  }
  const totalCents = roundToCents(entry.total);
  return totalCents === null ? entry.total : formatCents(totalCents);
}

function ForecastChart({ months, currency, locale }: {
  months: readonly AnalyticsForecastMonth[];
  currency: string;
  locale: string;
}) {
  const rows = months.map(({ month, currencies }) => ({
    month,
    amount: forecastTotalDecimal(currencies.find((total) => total.currency === currency)),
  }));
  const parsed = rows.map(({ amount }) => parseScaledInt(amount));
  const scale = Math.max(0, ...parsed.map((item) => item?.scale ?? 0));
  const units = parsed.map((item) => (
    item === null ? 0n : (item.negative ? -item.coeff : item.coeff) * (10n ** BigInt(scale - item.scale))
  ));
  const max = units.reduce((largest, value) => value > largest ? value : largest, 0n);
  const chartWidth = 320;
  const labelWidth = 72;
  const barWidth = chartWidth - labelWidth;
  const rowHeight = 22;
  const monthlyTotalsTitle = t("budget.forecast.monthlyTotals");

  return (
    <div className="budget-total">
      <h4>{monthlyTotalsTitle} · {currency}</h4>
      <svg role="img" aria-label={t("budget.forecast.chart", { code: currency })}
        viewBox={`0 0 ${String(chartWidth)} ${String(rows.length * rowHeight)}`} width={chartWidth}
        style={{ display: "block", maxWidth: "100%" }}>
        {rows.map(({ month }, index) => {
          const y = index * rowHeight;
          // Convert only the scaled SVG length to Number. The money and the ratio use exact integers.
          const width = max === 0n ? 0 : Number((units[index] ?? 0n) * BigInt(barWidth * 10) / max) / 10;
          return (
            <g key={month}>
              <text x="0" y={y + 15} fill="currentColor" fontSize="13">{month}</text>
              <rect x={labelWidth} y={y + 4} width={barWidth} height="14" fill="var(--accent-soft)" />
              <rect x={labelWidth} y={y + 4} width={width} height="14" fill="var(--accent)" />
            </g>
          );
        })}
      </svg>
      <details className="budget-recurring-evidence">
        <summary>{t("budget.forecast.totalsTable")}</summary>
        <div className="budget-table-wrap budget-chart-data-wrap" role="region" aria-label={`${monthlyTotalsTitle} · ${currency}`} tabIndex={0}>
          <table className="budget-limits-table" style={{ tableLayout: "fixed" }}>
            <caption>{monthlyTotalsTitle} · {currency}</caption>
            <thead><tr><th scope="col">{t("budget.forecast.month")}</th><th scope="col">{t("budget.forecast.total")}</th></tr></thead>
            <tbody>
              {rows.map(({ month, amount }) => (
                <tr key={month}>
                  <th scope="row">{monthLabel(month, locale)}</th>
                  <td>{formatAmount(amount, currency, locale)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}

function ForecastMonth({ month, locale }: { month: AnalyticsForecastMonth; locale: string }) {
  const label = monthLabel(month.month, locale);
  return (
    <section className="budget-total budget-month" aria-label={label}>
      <h4>{label}</h4>
      {month.items.length === 0 ? <p className="budget-muted">{t("budget.forecast.emptyMonth")}</p> : (
        <>
          <div className="budget-totals">
            {month.currencies.map((entry) => (
              <dl key={entry.currency} aria-label={entry.currency}>
                <dt>{t("budget.forecast.total")}</dt><dd><strong>{formatAmount(forecastTotalDecimal(entry), entry.currency, locale)}</strong></dd>
                <dt>{t("budget.forecast.scheduledTotal")}</dt>
                <dd>{isZero(entry.estimated_total) ? formatAmount(entry.scheduled_total, entry.currency, locale) : formatEstimate(entry.scheduled_total, entry.currency, locale)}</dd>
                <dt>{t("budget.forecast.estimatedTotal")}</dt><dd>{formatEstimate(entry.estimated_total, entry.currency, locale)}</dd>
              </dl>
            ))}
          </div>
          <ul className="budget-entry-list">
            {month.items.map((item, index) => (
              // The API orders this read-only list and can return identical labels and dates without item IDs.
              <li key={index} className="budget-entry">
                <div className="budget-recurring-main">
                  <span className="budget-entry-text">{item.label}</span>
                  <strong className="budget-recurring-amount">
                    {isScheduledForecastItem(item)
                      ? formatAmount(item.amount, item.currency, locale)
                      : formatEstimate(item.amount, item.currency, locale)}
                  </strong>
                </div>
                <div className="budget-entry-facts">
                  <time dateTime={item.expected_date}>{item.expected_date}</time>
                  <span className="budget-chip">{labelOf(FORECAST_CONFIDENCE_KEYS, item.confidence)}</span>
                  {item.past_due ? <span className="budget-chip">{t("budget.forecast.unmarked")}</span> : null}
                  {item.account_stale ? <span className="budget-chip">{t("budget.forecast.accountStale")}</span> : null}
                </div>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

export function ForecastView({ api, accounts, connections = [], locale, revision = 0, onAccessLoss, onOpenMortgages }: ForecastViewProps) {
  const [horizon, setHorizon] = useState<Horizon>(3);
  const [currency, setCurrency] = useState("");
  const [discovered, setDiscovered] = useState<string[]>([]);
  const [manualEnabled, setManualEnabled] = useState(false);
  const [manualCode, setManualCode] = useState("");
  const [shown, setShown] = useState<Shown>({ state: "loading" });
  const [categoryNames, setCategoryNames] = useState<CategoryNames>(new Map());
  const generation = useRef(0);

  useEffect(() => {
    const token = ++generation.current;
    setShown({ state: "loading" });
    setCategoryNames(new Map());
    void api.forecast(horizon, currency || undefined).then((result) => {
      // Superseded responses can still report access loss, so run the barrier first on both reads.
      if (onAccessLoss(result) || token !== generation.current) return;
      if (result.ok) {
        const { data } = result;
        setDiscovered((previous) => currencyOptions([
          ...previous, ...data.totals.map((total) => total.currency), ...data.deduplications.map((item) => item.currency),
        ]));
        const conflicts = data.summary.baseline_conflicts ?? [];
        setCategoryNames(conflicts.length > 0 ? "loading" : new Map());
        setShown({ state: "ready", data });
        if (conflicts.length > 0) {
          void api.categories().then((categoriesResult) => {
            if (onAccessLoss(categoriesResult) || token !== generation.current) return;
            if (!categoriesResult.ok) {
              setCategoryNames("error");
              return;
            }
            setCategoryNames(new Map(categoriesResult.data.categories.map((item) => [item.id, item.name])));
          });
        }
        return;
      }
      if (result.status !== 413) {
        setShown({ state: "error" });
        return;
      }
      setShown({ state: "limit" });
      setManualEnabled(true);
      if (currency === "") {
        void api.mortgages().then((fallback) => {
          if (onAccessLoss(fallback) || token !== generation.current) return;
          if (fallback.ok) {
            setDiscovered((previous) => currencyOptions([...previous, ...mortgageCurrencies(fallback.data.schedules)]));
          }
        });
      }
    });
    return () => { generation.current += 1; };
  }, [api, currency, horizon, revision, onAccessLoss]);

  const currencies = currencyOptions([
    ...discovered, currency,
    ...accounts.flatMap((account) => [account.display?.currency ?? "", ...(account.observed_balance_currencies ?? [])]),
  ]);
  const data = shown.state === "ready" ? shown.data : null;

  const title = t("budget.forecast.title");
  return (
    <section className="budget-entries" aria-label={title}>
      <h4>{title}</h4>
      <p className="budget-notice" role="note">{t("budget.forecast.intro")}</p>
      <div className="budget-range">
        <label>{t("budget.forecast.horizon")}
          <select value={horizon} onChange={(event) => {
            const selected = horizons.find((value) => String(value) === event.currentTarget.value);
            if (selected !== undefined) setHorizon(selected);
          }}>
            {horizons.map((value) => <option key={value} value={value}>{tPlural("budget.forecast.months", value)}</option>)}
          </select>
        </label>
        <label>{t("budget.forecast.currencyFilter")}
          <select value={currency} onChange={(event) => { setCurrency(event.currentTarget.value); }}>
            <option value="">{t("budget.forecast.allCurrencies")}</option>
            {currencies.map((code) => <option key={code} value={code}>{code}</option>)}
          </select>
        </label>
      </div>
      {shown.state === "loading" ? <p className="budget-muted" role="status">{t("budget.forecast.loading")}</p> : null}
      {shown.state === "error" ? <p className="budget-notice" role="alert">{t("budget.forecast.error")}</p> : null}
      {shown.state === "limit" ? <p className="budget-notice" role="alert">{t("budget.forecast.limit")}</p> : null}
      {manualEnabled ? (
        <form className="budget-rule-editor" onSubmit={(event) => {
          event.preventDefault();
          if (validCurrency(manualCode)) setCurrency(manualCode.trim().toUpperCase());
        }}>
          <label>{t("budget.forecast.currencyCode")}
            <input type="text" name="currency" autoComplete="off" maxLength={3} value={manualCode}
              onChange={(event) => { setManualCode(event.currentTarget.value); }} />
          </label>
          <button type="submit" disabled={!validCurrency(manualCode)}>{t("budget.forecast.applyCurrency")}</button>
        </form>
      ) : null}
      {data === null ? null : (
        <>
          <ForecastSummary data={data} accounts={accounts} connections={connections} locale={locale}
            categoryNames={categoryNames} onOpenMortgages={onOpenMortgages} />
          {data.totals.length === 0 ? (
            <p className="budget-muted">
              {currency === "" ? t("budget.forecast.empty") : t("budget.forecast.emptyForCurrency", { code: currency })}
            </p>
          ) : (
            <>
              <div className="budget-totals">
                {data.totals.map(({ currency: code }) => (
                  <ForecastChart key={code} months={data.months} currency={code} locale={locale} />
                ))}
              </div>
              {data.months.map((month) => <ForecastMonth key={month.month} month={month} locale={locale} />)}
            </>
          )}
          {data.deduplications.length > 0 ? (
            <details className="budget-recurring-evidence">
              <summary>{t("budget.forecast.merged")}</summary>
              <ul className="budget-entry-list">
                {data.deduplications.map((item) => (
                  <li key={`${item.schedule_id}/${item.matched_due_date}`} className="budget-entry">
                    <div className="budget-recurring-main">
                      <span className="budget-entry-text">{item.recurring_label} → {item.schedule_label}</span>
                      <strong className="budget-recurring-amount">{formatAmount(item.amount, item.currency, locale)}</strong>
                    </div>
                    <div className="budget-entry-facts">
                      <span>{t("budget.forecast.matchedDue", { date: item.matched_due_date })}</span>
                      <span>{t("budget.forecast.lastObserved", { date: item.last_observed_date })}</span>
                      {item.account_stale ? <span className="budget-chip">{t("budget.forecast.accountStale")}</span> : null}
                    </div>
                  </li>
                ))}
              </ul>
            </details>
          ) : null}
        </>
      )}
    </section>
  );
}
