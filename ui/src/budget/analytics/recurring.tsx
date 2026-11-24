// Read-only recurring-charge hints (2D). Shows server-computed monthly patterns with their supporting
// booked expenses. Never infers that a subscription is unwanted or unused; amounts stay exact strings.
import { useEffect, useRef, useState } from "react";
import { t, tPlural } from "../../i18n";
import type { BudgetApi } from "../api";
import type { BudgetAccount, BudgetConnection } from "../model";
import { formatAmount } from "../model";
import { RECURRING_CADENCE_KEYS, RECURRING_LABEL_SOURCE_KEYS, accountName, labelOf, recurringCurrencies } from "./model";
import type { AnalyticsRecurring, AnalyticsRecurringHint } from "./model";

type Shown = { state: "loading" } | { state: "error" } | { state: "ready"; data: AnalyticsRecurring };

export interface RecurringViewProps {
  api: Pick<BudgetApi, "recurring">;
  accounts: readonly BudgetAccount[];
  connections?: readonly BudgetConnection[] | undefined;
  locale: string;
  revision: number;
  onAccessLoss: (result: { ok: boolean; status?: number; error?: string }) => boolean;
}

function hintKey(hint: AnalyticsRecurringHint): string {
  const firstRef = hint.evidence[0]?.entry_reference ?? "";
  return `${hint.currency}/${hint.label_source}/${hint.label}/${hint.amount}/${hint.first_date}/${hint.last_date}/${firstRef}`;
}

export function RecurringView({ api, accounts, connections = [], locale, revision, onAccessLoss }: RecurringViewProps) {
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

  const name = (id: string) => accountName(accounts, connections, id, t("budget.recurring.account"));
  const showCurrencies = currencies.length > 1 || currency !== "";
  const title = t("budget.recurring.title");

  return (
    <section className="budget-entries" aria-label={title}>
      <h4>{title}</h4>
      <p className="budget-muted">{t("budget.recurring.intro")}</p>
      {showCurrencies ? (
        <div className="budget-views" role="group" aria-label={t("budget.recurring.currencyFilter")}>
          <button type="button" aria-pressed={currency === ""} onClick={() => { setCurrency(""); }}>
            {t("budget.recurring.allCurrencies")}
          </button>
          {currencies.map((code) => (
            <button type="button" key={code} aria-pressed={currency === code} onClick={() => { setCurrency(code); }}>
              {code}
            </button>
          ))}
        </div>
      ) : null}
      {shown.state === "loading" ? <p className="budget-muted" role="status">{t("budget.recurring.loading")}</p> : null}
      {shown.state === "error" ? <p className="budget-notice" role="alert">{t("budget.recurring.error")}</p> : null}
      {shown.state === "ready" ? (
        <>
          {shown.data.truncated ? (
            <p className="budget-notice" role="note">
              {t("budget.recurring.truncated", { shown: shown.data.hints.length, total: shown.data.total_hints })}
            </p>
          ) : null}
          {shown.data.hints.length === 0 ? (
            <p className="budget-muted">
              {currency === "" ? t("budget.recurring.empty") : t("budget.recurring.emptyForCurrency", { currency })}
            </p>
          ) : (
            <ul className="budget-entry-list">
              {shown.data.hints.map((hint) => (
                <li key={hintKey(hint)} className="budget-entry">
                  <div className="budget-recurring-main">
                    <span className="budget-entry-text">{hint.label}</span>
                    <strong className="budget-recurring-amount">{formatAmount(hint.amount, hint.currency, locale, "DBIT")}</strong>
                  </div>
                  <div className="budget-entry-facts">
                    <span className="budget-chip">{labelOf(RECURRING_CADENCE_KEYS, hint.cadence)}</span>
                    <span className="budget-chip">{labelOf(RECURRING_LABEL_SOURCE_KEYS, hint.label_source)}</span>
                    <span>{tPlural("budget.recurring.occurrences", hint.occurrence_count)}</span>
                    <span>{tPlural("budget.recurring.months", hint.distinct_months)}</span>
                    <span>{hint.first_date} – {hint.last_date}</span>
                    <span>
                      {hint.min_gap_days === hint.max_gap_days
                        ? tPlural("budget.recurring.gapSingle", hint.min_gap_days)
                        : t("budget.recurring.gapRange", { min: hint.min_gap_days, max: hint.max_gap_days })}
                    </span>
                    {hint.account_ids.map((id) => <span key={id}>{name(id)}</span>)}
                  </div>
                  <details className="budget-recurring-evidence">
                    <summary>{t("budget.recurring.evidence", { count: hint.evidence.length })}</summary>
                    {hint.evidence_truncated ? (
                      <p className="budget-muted">{t("budget.recurring.evidenceTruncated", { shown: hint.evidence.length })}</p>
                    ) : null}
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
