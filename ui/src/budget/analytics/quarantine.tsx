import { useEffect, useRef, useState } from "react";
import { t, tPlural } from "../../i18n";
import type { BudgetApi } from "../api";
import type { BudgetAccount, BudgetConnection } from "../model";
import { QUARANTINE_BUCKET_KEYS, QUARANTINE_REASON_KEYS, QUARANTINE_STATUS_KEYS, accountName, labelOf } from "./model";
import type { AnalyticsQuarantine } from "./model";

type Shown = { state: "loading" } | { state: "error" } | { state: "ready"; data: AnalyticsQuarantine };

export interface QuarantineViewProps {
  api: Pick<BudgetApi, "quarantine">;
  accounts: readonly BudgetAccount[];
  connections?: readonly BudgetConnection[] | undefined;
  revision: number;
  onAccessLoss: (result: { ok: boolean; status?: number; error?: string }) => boolean;
}

function reasonText(code: string): string {
  const key = QUARANTINE_REASON_KEYS[code];
  return key !== undefined ? t(key) : t("budget.quarantine.unknownReason", { code });
}

export function QuarantineView({ api, accounts, connections = [], revision, onAccessLoss }: QuarantineViewProps) {
  const [accountId, setAccountId] = useState("");
  const [shown, setShown] = useState<Shown>({ state: "loading" });
  const generation = useRef(0);

  useEffect(() => {
    const token = ++generation.current;
    setShown({ state: "loading" });
    void api.quarantine(accountId === "" ? undefined : accountId).then((result) => {
      if (onAccessLoss(result) || token !== generation.current) return;
      if (!result.ok) {
        setShown({ state: "error" });
        return;
      }
      setShown({ state: "ready", data: result.data });
    });
  }, [api, accountId, revision, onAccessLoss]);
  useEffect(() => () => { generation.current += 1; }, []);

  const accountTitle = t("budget.quarantine.account");
  const name = (id: string) => accountName(accounts, connections, id, accountTitle);
  const title = t("budget.quarantine.title");
  const summaryTitle = t("budget.quarantine.summary");
  const totalLabel = t("budget.quarantine.total");
  const uncertainLabel = t("budget.quarantine.buckets.uncertain");
  const unresolvedLabel = t("budget.quarantine.buckets.unresolved");
  const importUnresolvedLabel = t("budget.quarantine.buckets.import_unresolved");

  return (
    <section className="budget-entries" aria-label={title}>
      <h4>{title}</h4>
      <p className="budget-muted">{t("budget.quarantine.intro")}</p>
      <div className="budget-views" role="group" aria-label={t("budget.quarantine.accountFilter")}>
        <button type="button" aria-pressed={accountId === ""} onClick={() => { setAccountId(""); }}>
          {t("budget.quarantine.allAccounts")}
        </button>
        {accounts.map((item) => (
          <button type="button" key={item.id} aria-pressed={accountId === item.id} onClick={() => { setAccountId(item.id); }}>
            <span className="budget-entry-text">{name(item.id)}</span>
          </button>
        ))}
      </div>
      {shown.state === "loading" ? <p className="budget-muted" role="status">{t("budget.quarantine.loading")}</p> : null}
      {shown.state === "error" ? <p className="budget-notice" role="alert">{t("budget.quarantine.error")}</p> : null}
      {shown.state === "ready" ? (
        <>
          <section className="budget-total" aria-label={summaryTitle}>
            <h4>{summaryTitle}</h4>
            <dl>
              <dt>{uncertainLabel}</dt><dd>{tPlural("budget.quarantine.entries", shown.data.summary.uncertain_count)}</dd>
              <dt>{unresolvedLabel}</dt><dd>{tPlural("budget.quarantine.entries", shown.data.summary.unresolved_count)}</dd>
              <dt>{importUnresolvedLabel}</dt>
              <dd>{tPlural("budget.quarantine.entries", shown.data.summary.import_unresolved_count)}</dd>
              <dt><strong>{totalLabel}</strong></dt>
              <dd><strong>{tPlural("budget.quarantine.entries", shown.data.summary.total_count)}</strong></dd>
            </dl>
          </section>
          <table className="budget-limits-table">
            <caption>{t("budget.quarantine.accounts")}</caption>
            <thead>
              <tr>
                <th scope="col">{accountTitle}</th>
                <th scope="col">{uncertainLabel}</th>
                <th scope="col">{unresolvedLabel}</th>
                <th scope="col">{importUnresolvedLabel}</th>
                <th scope="col">{totalLabel}</th>
              </tr>
            </thead>
            <tbody>
              {shown.data.accounts.map((item) => (
                <tr key={item.account_id} className={item.selected ? undefined : "is-excluded"}>
                  <th scope="row">
                    {name(item.account_id)}
                    {item.selected ? null : <small> ({t("budget.quarantine.excluded")})</small>}
                  </th>
                  <td>{item.uncertain_count}</td>
                  <td>{item.unresolved_count}</td>
                  <td>{item.import_unresolved_count}</td>
                  <td>{item.total_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {shown.data.summary.total_count === 0 ? <p className="budget-muted">{t("budget.quarantine.empty")}</p> : null}
          {shown.data.truncated ? (
            <p className="budget-notice" role="note">
              {tPlural("budget.quarantine.truncatedShown", shown.data.items.length, {
                total: tPlural("budget.quarantine.ofTotal", shown.data.summary.total_count),
              })}
            </p>
          ) : null}
          {shown.data.items.length === 0 ? null : (
            <details className="budget-recurring-evidence">
              <summary>{t("budget.quarantine.details", { count: shown.data.items.length })}</summary>
              <ul className="budget-entry-list">
                {shown.data.items.map((item, index) => (
                  <li className="budget-entry" key={`${item.account_id}/${item.entry_reference ?? ""}/${String(index)}`}>
                    <div className="budget-entry-facts">
                      <span>{item.date ?? t("budget.quarantine.noDate")}</span>
                      <span className="budget-entry-text">{name(item.account_id)}</span>
                      <span className="budget-chip">{labelOf(QUARANTINE_BUCKET_KEYS, item.bucket)}</span>
                      <span className="budget-chip">
                        {item.status === null ? t("budget.quarantine.noStatus") : labelOf(QUARANTINE_STATUS_KEYS, item.status)}
                      </span>
                    </div>
                    <p className="budget-entry-text">{reasonText(item.reason)}</p>
                  </li>
                ))}
              </ul>
            </details>
          )}
        </>
      ) : null}
    </section>
  );
}
