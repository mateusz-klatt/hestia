// Strictly read-only consent health overview: bank consent renewal reminders + consent reconciliation
// classifications. Never triggers bank requests, remote closure, credential edits or local deletion.
import { useEffect, useRef, useState } from "react";
import { t, tPlural } from "../i18n";
import type { BudgetApi, BudgetResult } from "./api";
import { CONSENT_ATTENTION_KEYS, CONSENT_CLASSIFICATION_KEYS, CONSENT_PROVENANCE_KEYS, CONSENT_STATUS_KEYS,
  labelOf } from "./analytics/model";
import { when } from "./model";
import type { BudgetConnection, BudgetConsentReconcile, BudgetConsentRenewalReminders } from "./model";

type Loss = (result: { ok: boolean; status?: number; error?: string }) => boolean;
type Loaded<T> = { state: "loading" } | { state: "error" } | { state: "ready"; data: T };

export interface ConsentHealthPanelProps {
  api: Pick<BudgetApi, "consentRenewalReminders" | "consentReconcile">;
  connections: readonly BudgetConnection[];
  locale: string;
  revision: number;
  onAccessLoss: Loss;
}

function externalConsentCount(renewals: BudgetConsentRenewalReminders | null,
  reconcile: BudgetConsentReconcile): number {
  const managedActive = new Set(
    reconcile.consents
      .filter((item) => item.provenance === "hestia_created" && item.status === "ACTIVE")
      .map((item) => item.connection_id),
  );
  const externalConnections = new Set<string>();
  for (const item of reconcile.consents) {
    if (item.provenance !== "hestia_created") externalConnections.add(item.connection_id);
  }
  if (renewals !== null) {
    for (const reminder of renewals.reminders) {
      if (reminder.valid_until !== null && !managedActive.has(reminder.connection_id)) {
        externalConnections.add(reminder.connection_id);
      }
    }
  }
  return externalConnections.size;
}

function attentionText(code: string): string {
  const key = CONSENT_ATTENTION_KEYS[code];
  return key !== undefined ? t(key) : t("budget.consent.attentionFallback");
}

export function ConsentHealthPanel({ api, connections, locale, revision, onAccessLoss }: ConsentHealthPanelProps) {
  const [renewals, setRenewals] = useState<Loaded<BudgetConsentRenewalReminders>>({ state: "loading" });
  const [reconcile, setReconcile] = useState<Loaded<BudgetConsentReconcile>>({ state: "loading" });
  const generation = useRef(0);

  useEffect(() => {
    const token = ++generation.current;
    setRenewals({ state: "loading" });
    setReconcile({ state: "loading" });
    const handle = <T,>(request: Promise<BudgetResult<T>>, apply: (value: Loaded<T>) => void) => {
      void request.then((result) => {
        // Barrier runs before generation check so hiding or reloading never swallows a 401/403/404.
        if (onAccessLoss(result) || token !== generation.current) return;
        apply(result.ok ? { state: "ready", data: result.data } : { state: "error" });
      });
    };
    handle(api.consentRenewalReminders(), setRenewals);
    handle(api.consentReconcile(), setReconcile);
  }, [api, revision, onAccessLoss]);
  useEffect(() => () => { generation.current += 1; }, []);

  const bankName = (connectionId: string) =>
    connections.find((item) => item.id === connectionId)?.bank_name ?? t("budget.consent.unknownBank");
  const externalCount = reconcile.state === "ready"
    ? externalConsentCount(renewals.state === "ready" ? renewals.data : null, reconcile.data)
    : 0;
  const title = t("budget.consent.title");
  const loadingText = t("budget.consent.loading");
  const errorText = t("budget.consent.error");
  const reviewBadge = t("budget.consent.reviewBadge");

  return (
    <section className="budget-entries budget-consent-health" aria-label={title}>
      <h4>{title}</h4>
      <p className="budget-muted">{t("budget.consent.intro")}</p>

      <h5>{t("budget.consent.renewalsTitle")}</h5>
      {renewals.state === "loading" ? <p className="budget-muted" role="status">{loadingText}</p> : null}
      {renewals.state === "error" ? <p className="budget-notice" role="alert">{errorText}</p> : null}
      {renewals.state === "ready" ? (
        renewals.data.reminders.length === 0 ? <p className="budget-muted">{t("budget.consent.noRenewals")}</p> : (
          <ul className="budget-entry-list">
            {renewals.data.reminders.map((item) => {
              const expiry = when(item.valid_until, locale);
              return (
                <li key={item.connection_id} className="budget-entry">
                  <div className="budget-limit-main">
                    <span className="budget-entry-text"><strong>{item.bank_name}</strong></span>
                    <span className="budget-chip budget-consent-status" data-status={item.status}>
                      {labelOf(CONSENT_STATUS_KEYS, item.status)}
                    </span>
                  </div>
                  <div className="budget-entry-facts">
                    {expiry === null ? null : <span>{t("budget.consent.validUntil", { date: expiry })}</span>}
                    {item.days_remaining === null ? null : (
                      <span>{item.days_remaining < 0 || item.status === "expired"
                        ? t("budget.consent.expired") : tPlural("budget.consent.daysLeft", item.days_remaining)}</span>
                    )}
                    {item.paused ? <span className="budget-warn">{t("budget.consent.paused")}</span> : null}
                    {item.attention === null ? null : <span className="budget-warn">{attentionText(item.attention)}</span>}
                  </div>
                </li>
              );
            })}
          </ul>
        )
      ) : null}

      <h5>{t("budget.consent.reconcileTitle")}</h5>
      {reconcile.state === "loading" ? <p className="budget-muted" role="status">{loadingText}</p> : null}
      {reconcile.state === "error" ? <p className="budget-notice" role="alert">{errorText}</p> : null}
      {reconcile.state === "ready" ? (
        <>
          <div className="budget-entry-facts">
            <strong>
              {t("budget.consent.externalVsManaged", { external: externalCount, managed: reconcile.data.summary.consents_total })}
            </strong>
            <span>{t("budget.consent.flowsCount", { count: reconcile.data.summary.flows_total })}</span>
            <span className={reconcile.data.summary.needs_owner_review > 0 ? "budget-warn" : undefined}>
              {t("budget.consent.reviewCount", { count: reconcile.data.summary.needs_owner_review })}
            </span>
            {reconcile.data.summary.remote_close_eligible > 0 ? (
              <span>{t("budget.consent.remoteCloseInfo", { count: reconcile.data.summary.remote_close_eligible })}</span>
            ) : null}
            {reconcile.data.summary.local_retire_eligible > 0 ? (
              <span>{t("budget.consent.localRetireInfo", { count: reconcile.data.summary.local_retire_eligible })}</span>
            ) : null}
          </div>
          <p className="budget-muted">{t("budget.consent.retentionNote", { days: reconcile.data.retention_days })}</p>
          {reconcile.data.consents.length > 0 ? (
            <details className="budget-recurring-evidence">
              <summary>{t("budget.consent.consentsDetails", { count: reconcile.data.consents.length })}</summary>
              <ul>
                {reconcile.data.consents.map((item) => {
                  const expiry = when(item.valid_until, locale);
                  return (
                    <li key={item.consent_id}>
                      <span>{bankName(item.connection_id)}</span>
                      {" · "}
                      <span>{labelOf(CONSENT_CLASSIFICATION_KEYS, item.classification)}</span>
                      {" · "}
                      <span>{labelOf(CONSENT_PROVENANCE_KEYS, item.provenance)}</span>
                      {expiry === null ? null : <>{" · "}<span>{t("budget.consent.validUntil", { date: expiry })}</span></>}
                      {item.needs_owner_review ? <>{" · "}<strong className="budget-warn">{reviewBadge}</strong></> : null}
                    </li>
                  );
                })}
              </ul>
            </details>
          ) : null}
          {reconcile.data.flows.length > 0 ? (
            <details className="budget-recurring-evidence">
              <summary>{t("budget.consent.flowsDetails", { count: reconcile.data.flows.length })}</summary>
              <ul>
                {reconcile.data.flows.map((item) => (
                  <li key={item.flow_id}>
                    <span>{bankName(item.connection_id)}</span>
                    {" · "}
                    <span>{labelOf(CONSENT_CLASSIFICATION_KEYS, item.classification)}</span>
                    {item.needs_owner_review ? <>{" · "}<strong className="budget-warn">{reviewBadge}</strong></> : null}
                  </li>
                ))}
              </ul>
            </details>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
