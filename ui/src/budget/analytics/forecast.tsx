import { useEffect, useRef, useState } from "react";
import type { BudgetApi } from "../api";
import { formatAmount } from "../model";
import type { BudgetAccount, BudgetConnection } from "../model";
import { accountName, labelOf, monthLabel, mortgageCurrencies, pluralPl, validCurrency } from "./model";
import type { AnalyticsForecast, AnalyticsForecastMonth } from "./model";

type CategoryNames = ReadonlyMap<string, string> | "error" | "loading";

const horizons = [1, 3, 6, 12] as const;
type Horizon = typeof horizons[number];

const forecastLabels = {
  en: {
    title: "Forecast",
    intro: "Estimated upcoming commitments based on your mortgage schedules and monthly recurring expense patterns (at least three occurrences). This is an estimate only — not a guarantee, cashflow balance forecast, or prediction of variable spending.",
    horizon: "Forecast horizon",
    months: (count: number) => `${String(count)} ${count === 1 ? "month" : "months"}`,
    currencyFilter: "Filter forecast by currency",
    allCurrencies: "All currencies",
    currencyCode: "Currency code (ISO 4217)",
    applyCurrency: "Apply currency",
    loading: "Loading forecast…",
    error: "The forecast could not be loaded. Try again.",
    limit: "The forecast is too large. Filter by currency or choose a shorter horizon.",
    empty: "No upcoming commitments in this period.",
    emptyForCurrency: (code: string) => `No upcoming commitments in ${code} in this period.`,
    emptyMonth: "No upcoming commitments this month.",
    evaluatedFor: (date: string, timezone: string) => `Evaluated for ${date} (${timezone})`,
    ledgerAsOf: (date: string | null) => date === null ? "No synced ledger data yet" : `Ledger data as of ${date}`,
    freshnessStale: "Ledger data may be outdated.",
    staleAccounts: (count: number) => `${String(count)} ${count === 1 ? "stale account" : "stale accounts"}`,
    staleOverflow: (count: number) => `${String(count)} more ${count === 1 ? "stale account omitted" : "stale accounts omitted"} from this list.`,
    overdueSchedules: (count: number) => `${String(count)} ${count === 1 ? "mortgage schedule" : "mortgage schedules"} overdue from prior months`,
    openMortgages: "Open mortgage reminders",
    lapsedRecurring: (count: number) => `${String(count)} ${count === 1 ? "lapsed recurring hint" : "lapsed recurring hints"}`,
    staleRecurring: (count: number) => `${String(count)} ${count === 1 ? "recurring hint" : "recurring hints"} from stale accounts`,
    account: "Account",
    month: "Month",
    monthlyTotals: "Monthly totals",
    chart: (code: string) => `Monthly forecast totals in ${code}`,
    totalsTable: "View totals as a table",
    total: "Total",
    scheduledTotal: "Scheduled",
    estimatedTotal: "Estimated",
    confidence: { scheduled: "Scheduled", high: "Estimated", medium: "Estimated", estimated: "≈ estimated" },
    unmarked: "Unmarked",
    accountStale: "Account not up to date",
    merged: "Merged with instalment",
    matchedDue: (date: string) => `Instalment due ${date}`,
    lastObserved: (date: string) => `Last observed ${date}`,
    baselineConflict: (names: string) => `Skipped the estimate for: ${names} (already forecast as a fixed payment)`,
    baselineConflictUnnamed: "another category",
    baselineConflictUnknown: "Skipped an estimate for a category that is already forecast as a fixed payment",
  },
  pl: {
    title: "Prognoza",
    intro: "Szacunek nadchodzących zobowiązań na podstawie Twoich harmonogramów hipotecznych i powtarzalnych wydatków miesięcznych (co najmniej trzy wystąpienia). To wyłącznie szacunek — nie gwarancja, prognoza salda ani przewidywanie wydatków zmiennych.",
    horizon: "Horyzont prognozy",
    months: (count: number) => `${String(count)} ${pluralPl(count, "miesiąc", "miesiące", "miesięcy")}`,
    currencyFilter: "Filtruj prognozę według waluty",
    allCurrencies: "Wszystkie waluty",
    currencyCode: "Kod waluty (ISO 4217)",
    applyCurrency: "Zastosuj walutę",
    loading: "Wczytywanie prognozy…",
    error: "Nie udało się wczytać prognozy. Spróbuj ponownie.",
    limit: "Prognoza jest zbyt duża. Wybierz walutę lub skróć horyzont prognozy.",
    empty: "Brak nadchodzących zobowiązań w tym okresie.",
    emptyForCurrency: (code: string) => `Brak nadchodzących zobowiązań w walucie ${code} w tym okresie.`,
    emptyMonth: "Brak nadchodzących zobowiązań w tym miesiącu.",
    evaluatedFor: (date: string, timezone: string) => `Stan na ${date} (${timezone})`,
    ledgerAsOf: (date: string | null) => date === null ? "Brak zsynchronizowanych danych księgowych" : `Dane księgowe na dzień ${date}`,
    freshnessStale: "Dane księgowe mogą być nieaktualne.",
    staleAccounts: (count: number) => `${String(count)} ${pluralPl(count, "nieaktualne konto", "nieaktualne konta", "nieaktualnych kont")}`,
    staleOverflow: (count: number) => `Pominięto na liście: ${String(count)} ${pluralPl(count, "nieaktualne konto", "nieaktualne konta", "nieaktualnych kont")}.`,
    overdueSchedules: (count: number) => `${String(count)} ${pluralPl(count, "harmonogram z zaległą ratą", "harmonogramy z zaległą ratą", "harmonogramów z zaległą ratą")} z poprzednich miesięcy`,
    openMortgages: "Otwórz przypomnienia hipoteczne",
    lapsedRecurring: (count: number) => `${String(count)} ${pluralPl(count, "wygasła podpowiedź", "wygasłe podpowiedzi", "wygasłych podpowiedzi")} o wydatkach powtarzalnych`,
    staleRecurring: (count: number) => `${String(count)} ${pluralPl(count, "podpowiedź", "podpowiedzi", "podpowiedzi")} z nieaktualnych kont`,
    account: "Konto",
    month: "Miesiąc",
    monthlyTotals: "Sumy miesięczne",
    chart: (code: string) => `Miesięczne sumy prognozy w walucie ${code}`,
    totalsTable: "Pokaż sumy w tabeli",
    total: "Razem",
    scheduledTotal: "Zaplanowane",
    estimatedTotal: "Szacowane",
    confidence: { scheduled: "Zaplanowana", high: "Szacowana", medium: "Szacowana", estimated: "≈ szacowane" },
    unmarked: "Nieoznaczona",
    accountStale: "Konto nieaktualne",
    merged: "Połączono z ratą",
    matchedDue: (date: string) => `Termin raty: ${date}`,
    lastObserved: (date: string) => `Ostatnia obserwacja: ${date}`,
    baselineConflict: (names: string) => `Pominięto szacunek dla: ${names} (już prognozowane jako stała płatność)`,
    baselineConflictUnnamed: "inna kategoria",
    baselineConflictUnknown: "Pominięto szacunek dla kategorii, która jest już prognozowana jako stała płatność",
  },
};
type ForecastLabels = typeof forecastLabels.en;
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

function baselineConflictNote(data: AnalyticsForecast, names: CategoryNames, text: ForecastLabels, locale: string): string | null {
  const conflicts = data.summary.baseline_conflicts ?? [];
  if (conflicts.length === 0 || names === "loading") return null;
  if (names === "error") return text.baselineConflictUnknown;
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
  if (unnamed) labels.push(text.baselineConflictUnnamed);
  if (labels.length === 0) return text.baselineConflictUnknown;
  labels.sort((left, right) => left.localeCompare(right, locale));
  return text.baselineConflict(labels.join(", "));
}

function ForecastSummary({ data, accounts, connections, text, locale, categoryNames, onOpenMortgages }: {
  data: AnalyticsForecast;
  accounts: readonly BudgetAccount[];
  connections: readonly BudgetConnection[];
  text: ForecastLabels;
  locale: string;
  categoryNames: CategoryNames;
  onOpenMortgages: () => void;
}) {
  const { summary } = data;
  const skipped = baselineConflictNote(data, categoryNames, text, locale);
  return (
    <>
      {skipped === null ? null : <p className="budget-notice" role="note">{skipped}</p>}
      <ul className="budget-facts">
        <li>{text.evaluatedFor(data.evaluation_date, data.evaluation_timezone)}</li>
        <li>{text.ledgerAsOf(data.ledger_as_of)}</li>
      </ul>
      {data.freshness_stale ? <p className="budget-notice" role="note">{text.freshnessStale}</p> : null}
      <ul className="budget-facts">
        <li>{text.staleAccounts(summary.stale_accounts)}</li>
        <li className={summary.stale_recurring_count > 0 ? "budget-warn" : undefined}>{text.staleRecurring(summary.stale_recurring_count)}</li>
        <li className={summary.lapsed_recurring_count > 0 ? "budget-warn" : undefined}>{text.lapsedRecurring(summary.lapsed_recurring_count)}</li>
        <li className={summary.overdue_mortgage_schedule_count > 0 ? "budget-warn" : undefined}>
          {text.overdueSchedules(summary.overdue_mortgage_schedule_count)}
          {summary.overdue_mortgage_schedule_count > 0 ? (
            <>{" · "}<button type="button" className="budget-link" onClick={onOpenMortgages}>{text.openMortgages}</button></>
          ) : null}
        </li>
      </ul>
      {data.freshness_stale && data.stale_account_ids.length > 0 ? (
        <ul className="budget-facts" aria-label={text.staleAccounts(summary.stale_accounts)}>
          {data.stale_account_ids.map((id) => <li key={id}>{accountName(accounts, connections, id, text.account)}</li>)}
        </ul>
      ) : null}
      {data.stale_account_ids_truncated ? (
        <p className="budget-notice" role="note">{text.staleOverflow(summary.stale_accounts - data.stale_account_ids.length)}</p>
      ) : null}
    </>
  );
}

function ForecastChart({ months, currency, text, locale }: {
  months: readonly AnalyticsForecastMonth[];
  currency: string;
  text: ForecastLabels;
  locale: string;
}) {
  const rows = months.map(({ month, currencies }) => ({
    month, amount: currencies.find((total) => total.currency === currency)?.total ?? "0",
  }));
  const decimals = rows.map(({ amount }) => amount.split("."));
  const scale = Math.max(0, ...decimals.map(([, fraction]) => fraction?.length ?? 0));
  const units = decimals.map(([whole, fraction = ""]) => BigInt(`${whole ?? "0"}${fraction.padEnd(scale, "0")}`));
  const max = units.reduce((largest, value) => value > largest ? value : largest, 0n);
  const chartWidth = 320;
  const labelWidth = 72;
  const barWidth = chartWidth - labelWidth;
  const rowHeight = 22;

  return (
    <div className="budget-total">
      <h4>{text.monthlyTotals} · {currency}</h4>
      <svg role="img" aria-label={text.chart(currency)} viewBox={`0 0 ${String(chartWidth)} ${String(rows.length * rowHeight)}`} width={chartWidth}
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
        <summary>{text.totalsTable}</summary>
        <table className="budget-limits-table" style={{ tableLayout: "fixed" }}>
          <caption>{text.monthlyTotals} · {currency}</caption>
          <thead><tr><th scope="col">{text.month}</th><th scope="col">{text.total}</th></tr></thead>
          <tbody>
            {rows.map(({ month, amount }) => (
              <tr key={month}><th scope="row">{monthLabel(month, locale)}</th><td>{formatAmount(amount, currency, locale)}</td></tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  );
}

function ForecastMonth({ month, text, locale }: { month: AnalyticsForecastMonth; text: ForecastLabels; locale: string }) {
  const label = monthLabel(month.month, locale);
  return (
    <section className="budget-total budget-month" aria-label={label}>
      <h4>{label}</h4>
      {month.items.length === 0 ? <p className="budget-muted">{text.emptyMonth}</p> : (
        <>
          <div className="budget-totals">
            {month.currencies.map(({ currency, total, scheduled_total, estimated_total }) => (
              <dl key={currency} aria-label={currency}>
                <dt>{text.total}</dt><dd><strong>{formatAmount(total, currency, locale)}</strong></dd>
                <dt>{text.scheduledTotal}</dt><dd>{formatAmount(scheduled_total, currency, locale)}</dd>
                <dt>{text.estimatedTotal}</dt><dd>{formatAmount(estimated_total, currency, locale)}</dd>
              </dl>
            ))}
          </div>
          <ul className="budget-entry-list">
            {month.items.map((item, index) => (
              // The API orders this read-only list and can return identical labels and dates without item IDs.
              <li key={index} className="budget-entry">
                <div className="budget-recurring-main">
                  <span className="budget-entry-text">{item.label}</span>
                  <strong className="budget-recurring-amount">{formatAmount(item.amount, item.currency, locale)}</strong>
                </div>
                <div className="budget-entry-facts">
                  <time dateTime={item.expected_date}>{item.expected_date}</time>
                  <span className="budget-chip">{labelOf(text.confidence, item.confidence)}</span>
                  {item.past_due ? <span className="budget-chip">{text.unmarked}</span> : null}
                  {item.account_stale ? <span className="budget-chip">{text.accountStale}</span> : null}
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
  const text = locale.toLowerCase().startsWith("pl") ? forecastLabels.pl : forecastLabels.en;
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

  return (
    <section className="budget-entries" aria-label={text.title}>
      <h4>{text.title}</h4>
      <p className="budget-notice" role="note">{text.intro}</p>
      <div className="budget-range">
        <label>{text.horizon}
          <select value={horizon} onChange={(event) => {
            const selected = horizons.find((value) => String(value) === event.currentTarget.value);
            if (selected !== undefined) setHorizon(selected);
          }}>
            {horizons.map((value) => <option key={value} value={value}>{text.months(value)}</option>)}
          </select>
        </label>
        <label>{text.currencyFilter}
          <select value={currency} onChange={(event) => { setCurrency(event.currentTarget.value); }}>
            <option value="">{text.allCurrencies}</option>
            {currencies.map((code) => <option key={code} value={code}>{code}</option>)}
          </select>
        </label>
      </div>
      {shown.state === "loading" ? <p className="budget-muted" role="status">{text.loading}</p> : null}
      {shown.state === "error" ? <p className="budget-notice" role="alert">{text.error}</p> : null}
      {shown.state === "limit" ? <p className="budget-notice" role="alert">{text.limit}</p> : null}
      {manualEnabled ? (
        <form className="budget-rule-editor" onSubmit={(event) => {
          event.preventDefault();
          if (validCurrency(manualCode)) setCurrency(manualCode.trim().toUpperCase());
        }}>
          <label>{text.currencyCode}
            <input type="text" name="currency" autoComplete="off" maxLength={3} value={manualCode}
              onChange={(event) => { setManualCode(event.currentTarget.value); }} />
          </label>
          <button type="submit" disabled={!validCurrency(manualCode)}>{text.applyCurrency}</button>
        </form>
      ) : null}
      {data === null ? null : (
        <>
          <ForecastSummary data={data} accounts={accounts} connections={connections} text={text} locale={locale}
            categoryNames={categoryNames} onOpenMortgages={onOpenMortgages} />
          {data.totals.length === 0 ? (
            <p className="budget-muted">{currency === "" ? text.empty : text.emptyForCurrency(currency)}</p>
          ) : (
            <>
              <div className="budget-totals">
                {data.totals.map(({ currency: code }) => (
                  <ForecastChart key={code} months={data.months} currency={code} text={text} locale={locale} />
                ))}
              </div>
              {data.months.map((month) => <ForecastMonth key={month.month} month={month} text={text} locale={locale} />)}
            </>
          )}
          {data.deduplications.length > 0 ? (
            <details className="budget-recurring-evidence">
              <summary>{text.merged}</summary>
              <ul className="budget-entry-list">
                {data.deduplications.map((item) => (
                  <li key={`${item.schedule_id}/${item.matched_due_date}`} className="budget-entry">
                    <div className="budget-recurring-main">
                      <span className="budget-entry-text">{item.recurring_label} → {item.schedule_label}</span>
                      <strong className="budget-recurring-amount">{formatAmount(item.amount, item.currency, locale)}</strong>
                    </div>
                    <div className="budget-entry-facts">
                      <span>{text.matchedDue(item.matched_due_date)}</span>
                      <span>{text.lastObserved(item.last_observed_date)}</span>
                      {item.account_stale ? <span className="budget-chip">{text.accountStale}</span> : null}
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
