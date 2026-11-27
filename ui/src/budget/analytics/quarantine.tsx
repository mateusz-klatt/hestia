import { useEffect, useId, useRef, useState } from "react";
import { t, tPlural } from "../../i18n";
import type { BudgetApi } from "../api";
import type { BudgetAccount, BudgetConnection } from "../model";
import { QUARANTINE_BUCKET_KEYS, QUARANTINE_REASON_KEYS, QUARANTINE_STATUS_KEYS, accountName, labelOf } from "./model";
import type { AnalyticsQuarantine, AnalyticsQuarantineItem } from "./model";

type Shown = { state: "loading" } | { state: "error" } | { state: "ready"; data: AnalyticsQuarantine };

export interface QuarantineViewProps {
  api: Pick<BudgetApi, "quarantine" | "dismissQuarantineItem" | "dismissQuarantineBatch">;
  accounts: readonly BudgetAccount[];
  connections?: readonly BudgetConnection[] | undefined;
  revision: number;
  canDismiss: boolean;
  onAccessLoss: (result: { ok: boolean; status?: number; error?: string }) => boolean;
}

function reasonText(code: string): string {
  const key = QUARANTINE_REASON_KEYS[code];
  return key !== undefined ? t(key) : t("budget.quarantine.unknownReason", { code });
}

export function QuarantineView({ api, accounts, connections = [], revision, canDismiss, onAccessLoss }: QuarantineViewProps) {
  const [accountId, setAccountId] = useState("");
  const [shown, setShown] = useState<Shown>({ state: "loading" });
  const [refresh, setRefresh] = useState(0);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [dismissing, setDismissing] = useState(false);
  const [dismissError, setDismissError] = useState(false);
  const [confirmBatchId, setConfirmBatchId] = useState<string | null>(null);
  const confirmDescriptionId = useId();
  const cancelConfirmation = useRef<HTMLButtonElement>(null);
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
  }, [api, accountId, revision, refresh, onAccessLoss]);
  useEffect(() => () => { generation.current += 1; }, []);
  useEffect(() => {
    setDismissError(false);
    setConfirmBatchId(null);
  }, [accountId, revision]);
  useEffect(() => {
    if (confirmBatchId !== null) cancelConfirmation.current?.focus();
  }, [confirmBatchId]);

  async function dismiss(key: NonNullable<AnalyticsQuarantineItem["import_key"]>, wholeBatch: boolean) {
    const token = generation.current;
    setDismissError(false);
    setDismissing(true);
    const result = wholeBatch ? await api.dismissQuarantineBatch(key.batch_id) : await api.dismissQuarantineItem(key);
    setDismissing(false);
    if (onAccessLoss(result)) return;
    if (result.ok) {
      setConfirmBatchId(null);
      setRefresh((value) => value + 1);
    } else if (token === generation.current) {
      setDismissError(true);
    }
  }

  const accountTitle = t("budget.quarantine.account");
  const name = (id: string) => accountName(accounts, connections, id, accountTitle);
  const title = t("budget.quarantine.title");
  const summaryTitle = t("budget.quarantine.summary");
  const totalLabel = t("budget.quarantine.total");
  const uncertainLabel = t("budget.quarantine.buckets.uncertain");
  const unresolvedLabel = t("budget.quarantine.buckets.unresolved");
  const importUnresolvedLabel = t("budget.quarantine.buckets.import_unresolved");
  const dismissBatchLabel = t("budget.quarantine.dismissBatch");
  const dismissActionsDisabled = dismissing || confirmBatchId !== null;
  const shownBatches = new Set<string>();

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
      {dismissError ? <p className="budget-notice" role="alert">{t("budget.quarantine.dismissError")}</p> : null}
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
            <details className="budget-recurring-evidence" open={detailsOpen}
              onToggle={(event) => { setDetailsOpen(event.currentTarget.open); }}>
              <summary>{t("budget.quarantine.details", { count: shown.data.items.length })}</summary>
              <ul className="budget-entry-list">
                {shown.data.items.map((item, index) => {
                  const key = item.bucket === "import_unresolved" ? item.import_key : undefined;
                  const firstInBatch = key != null && !shownBatches.has(key.batch_id);
                  if (key != null) shownBatches.add(key.batch_id);
                  const batchCount = item.unresolved_count;
                  const rowKey = key == null
                    ? `${item.bucket}/${String(index)}`
                    : `${item.bucket}/${key.batch_id}/${String(key.ordinal)}`;
                  return (
                    <li className="budget-entry" key={rowKey}>
                      <div className="budget-entry-facts">
                        <span>{item.date ?? t("budget.quarantine.noDate")}</span>
                        <span className="budget-entry-text">{name(item.account_id)}</span>
                        <span className="budget-chip">{labelOf(QUARANTINE_BUCKET_KEYS, item.bucket)}</span>
                        <span className="budget-chip">
                          {item.status === null ? t("budget.quarantine.noStatus") : labelOf(QUARANTINE_STATUS_KEYS, item.status)}
                        </span>
                      </div>
                      <p className="budget-entry-text">{reasonText(item.reason)}</p>
                      {canDismiss && key != null ? (
                        <div className="budget-actions">
                          <button type="button" disabled={dismissActionsDisabled} onClick={() => { void dismiss(key, false); }}>
                            {t("budget.quarantine.dismissItem")}
                          </button>
                          {firstInBatch && batchCount != null ? (
                            <button type="button" disabled={dismissActionsDisabled} onClick={() => { setConfirmBatchId(key.batch_id); }}>
                              {dismissBatchLabel}
                            </button>
                          ) : null}
                        </div>
                      ) : null}
                      {canDismiss && key != null && firstInBatch && batchCount != null && confirmBatchId === key.batch_id ? (
                        <div className="budget-notice" role="alertdialog" aria-label={dismissBatchLabel}
                          aria-describedby={confirmDescriptionId}>
                          <p className="budget-entry-text" id={confirmDescriptionId}>
                            {t("budget.quarantine.dismissBatchConfirm", {
                              batch: key.batch_id, entries: tPlural("budget.quarantine.entries", batchCount),
                            })}
                          </p>
                          <div className="budget-actions">
                            <button ref={cancelConfirmation} type="button" disabled={dismissing}
                              onClick={() => { setConfirmBatchId(null); }}>
                              {t("budget.main.cancel")}
                            </button>
                            <button type="button" className="budget-primary" disabled={dismissing}
                              onClick={() => { void dismiss(key, true); }}>
                              {dismissBatchLabel}
                            </button>
                          </div>
                        </div>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
            </details>
          )}
        </>
      ) : null}
    </section>
  );
}
