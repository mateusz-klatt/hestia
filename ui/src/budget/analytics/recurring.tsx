// Read-only recurring-charge hints (2D). Shows server-computed monthly patterns with their supporting
// booked expenses. Never infers that a subscription is unwanted or unused; amounts stay exact strings.
import { useEffect, useRef, useState } from "react";
import type { BudgetApi } from "../api";
import type { BudgetAccount, BudgetConnection } from "../model";
import { formatAmount } from "../model";
import { accountName, labelOf, pluralPl, recurringCurrencies } from "./model";
import type { AnalyticsRecurring, AnalyticsRecurringHint } from "./model";

export const recurringLabels = {
  en: {
    title: "Recurring charges",
    intro: "Monthly expenses observed at least three times on selected accounts. These are informational hints only and do not imply a subscription is unwanted or unused.",
    allCurrencies: "All currencies",
    currencyFilter: "Filter recurring charges by currency",
    loading: "Loading recurring charges…",
    error: "Recurring charges could not be loaded. Try again.",
    empty: "No recurring monthly charges have been detected yet.",
    emptyForCurrency: (currency: string) => `No recurring monthly charges detected in ${currency}.`,
    truncated: (shown: number, total: number) => `Showing first ${String(shown)} of ${String(total)} recurring hints. Currency filter reflects currencies shown here.`,
    cadences: { monthly: "Monthly" },
    sources: { counterparty_name: "Counterparty", description: "Description" },
    occurrences: (count: number) => `${String(count)} ${count === 1 ? "occurrence" : "occurrences"}`,
    months: (count: number) => `${String(count)} ${count === 1 ? "month" : "months"}`,
    gap: (min: number, max: number) => (min === max
      ? `${String(min)} ${min === 1 ? "day" : "days"} apart`
      : `${String(min)}–${String(max)} days apart`),
    evidence: (count: number) => `Evidence (${String(count)})`,
    evidenceTruncated: (shown: number) => `Showing latest ${String(shown)} entries.`,
    account: "Account",
  },
  pl: {
    title: "Powtarzalne wydatki",
    intro: "Miesięczne wydatki zaobserwowane co najmniej trzy razy na wybranych kontach. To wyłącznie podpowiedzi informacyjne — nie oznaczają, że subskrypcja jest niechciana lub nieużywana.",
    allCurrencies: "Wszystkie waluty",
    currencyFilter: "Filtruj powtarzalne wydatki według waluty",
    loading: "Wczytywanie powtarzalnych wydatków…",
    error: "Nie udało się wczytać powtarzalnych wydatków. Spróbuj ponownie.",
    empty: "Nie wykryto jeszcze powtarzalnych miesięcznych wydatków.",
    emptyForCurrency: (currency: string) => `Nie wykryto powtarzalnych miesięcznych wydatków w walucie ${currency}.`,
    truncated: (shown: number, total: number) => `Pokazano pierwsze ${String(shown)} z ${String(total)} podpowiedzi. Filtr walut obejmuje waluty widoczne w tych podpowiedziach.`,
    cadences: { monthly: "Co miesiąc" },
    sources: { counterparty_name: "Kontrahent", description: "Opis" },
    occurrences: (count: number) => `${String(count)} ${pluralPl(count, "wystąpienie", "wystąpienia", "wystąpień")}`,
    months: (count: number) => `${String(count)} ${pluralPl(count, "miesiąc", "miesiące", "miesięcy")}`,
    gap: (min: number, max: number) => (min === max
      ? `co ${String(min)} ${pluralPl(min, "dzień", "dni", "dni")}`
      : `co ${String(min)}–${String(max)} dni`),
    evidence: (count: number) => `Dowody (${String(count)})`,
    evidenceTruncated: (shown: number) => `Pokazano najnowsze wpisy: ${String(shown)}.`,
    account: "Konto",
  },
};
export type RecurringLabels = typeof recurringLabels.en;

type Shown = { state: "loading" } | { state: "error" } | { state: "ready"; data: AnalyticsRecurring };

export interface RecurringViewProps {
  api: Pick<BudgetApi, "recurring">;
  accounts: readonly BudgetAccount[];
  connections?: readonly BudgetConnection[] | undefined;
  text: RecurringLabels;
  locale: string;
  revision: number;
  onAccessLoss: (result: { ok: boolean; status?: number; error?: string }) => boolean;
}

function hintKey(hint: AnalyticsRecurringHint): string {
  const firstRef = hint.evidence[0]?.entry_reference ?? "";
  return `${hint.currency}/${hint.label_source}/${hint.label}/${hint.amount}/${hint.first_date}/${hint.last_date}/${firstRef}`;
}

export function RecurringView({ api, accounts, connections = [], text, locale, revision, onAccessLoss }: RecurringViewProps) {
  const [currency, setCurrency] = useState("");
  const [currencies, setCurrencies] = useState<string[]>([]);
  const [shown, setShown] = useState<Shown>({ state: "loading" });
  // Every currency change, reload or unmount invalidates older in-flight reads so stale rows never render.
  const generation = useRef(0);

  useEffect(() => {
    const token = ++generation.current;
    setShown({ state: "loading" });
    void api.recurring(currency === "" ? undefined : currency).then((result) => {
      // Run the access barrier before dropping superseded answers so a 401/403/404 always clears the view.
      if (onAccessLoss(result) || token !== generation.current) return;
      if (!result.ok) {
        setShown({ state: "error" });
        return;
      }
      setShown({ state: "ready", data: result.data });
      if (currency === "") {
        setCurrencies(recurringCurrencies(result.data.hints));
      } else {
        setCurrencies((current) => (current.includes(currency) ? current : [...current, currency].sort((a, b) => a.localeCompare(b))));
      }
    });
  }, [api, currency, revision, onAccessLoss]);
  useEffect(() => () => { generation.current += 1; }, []);

  const name = (id: string) => accountName(accounts, connections, id, text.account);
  const showCurrencies = currencies.length > 1 || currency !== "";

  return (
    <section className="budget-entries" aria-label={text.title}>
      <h4>{text.title}</h4>
      <p className="budget-muted">{text.intro}</p>
      {showCurrencies ? (
        <div className="budget-views" role="group" aria-label={text.currencyFilter}>
          <button type="button" aria-pressed={currency === ""} onClick={() => { setCurrency(""); }}>
            {text.allCurrencies}
          </button>
          {currencies.map((code) => (
            <button type="button" key={code} aria-pressed={currency === code} onClick={() => { setCurrency(code); }}>
              {code}
            </button>
          ))}
        </div>
      ) : null}
      {shown.state === "loading" ? <p className="budget-muted" role="status">{text.loading}</p> : null}
      {shown.state === "error" ? <p className="budget-notice" role="alert">{text.error}</p> : null}
      {shown.state === "ready" ? (
        <>
          {shown.data.truncated ? (
            <p className="budget-notice" role="note">{text.truncated(shown.data.hints.length, shown.data.total_hints)}</p>
          ) : null}
          {shown.data.hints.length === 0 ? (
            <p className="budget-muted">{currency === "" ? text.empty : text.emptyForCurrency(currency)}</p>
          ) : (
            <ul className="budget-entry-list">
              {shown.data.hints.map((hint) => (
                <li key={hintKey(hint)} className="budget-entry">
                  <div className="budget-recurring-main">
                    <span className="budget-entry-text">{hint.label}</span>
                    <strong className="budget-recurring-amount">{formatAmount(hint.amount, hint.currency, locale, "DBIT")}</strong>
                  </div>
                  <div className="budget-entry-facts">
                    <span className="budget-chip">{labelOf(text.cadences, hint.cadence)}</span>
                    <span className="budget-chip">{labelOf(text.sources, hint.label_source)}</span>
                    <span>{text.occurrences(hint.occurrence_count)}</span>
                    <span>{text.months(hint.distinct_months)}</span>
                    <span>{hint.first_date} – {hint.last_date}</span>
                    <span>{text.gap(hint.min_gap_days, hint.max_gap_days)}</span>
                    {hint.account_ids.map((id) => <span key={id}>{name(id)}</span>)}
                  </div>
                  <details className="budget-recurring-evidence">
                    <summary>{text.evidence(hint.evidence.length)}</summary>
                    {hint.evidence_truncated ? <p className="budget-muted">{text.evidenceTruncated(hint.evidence.length)}</p> : null}
                    <ul>
                      {hint.evidence.map((item) => (
                        <li key={`${item.account_id}/${item.entry_reference}/${item.date}`}>
                          <span>{item.date}</span>
                          {" · "}
                          <span>{name(item.account_id)}</span>
                        </li>
                      ))}
                    </ul>
                  </details>
                </li>
              ))}
            </ul>
          )}
        </>
      ) : null}
    </section>
  );
}
