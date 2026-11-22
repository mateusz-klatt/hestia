// Strictly read-only consent health overview: bank consent renewal reminders + consent reconciliation
// classifications. Never triggers bank requests, remote closure, credential edits or local deletion.
import { useEffect, useRef, useState } from "react";
import type { BudgetApi, BudgetResult } from "./api";
import { labelOf, pluralPl } from "./analytics/model";
import { when } from "./model";
import type { BudgetConnection, BudgetConsentReconcile, BudgetConsentRenewalReminders } from "./model";

export const consentHealthLabels = {
  en: {
    title: "Consent status & renewal overview",
    intro: "Read-only summary of bank consent validity and stored authorization records. No remote closure, deletion or bank request runs here.",
    loading: "Loading consent overview…",
    error: "Consent overview could not be loaded. Try again.",
    renewalsTitle: "Bank consent validity",
    noRenewals: "No enabled bank connections to evaluate.",
    reconcileTitle: "Stored consents & authorization flows",
    externalVsManaged: (external: number, managed: number) =>
      `External consents (imported): ${String(external)} · managed by Hestia: ${String(managed)}`,
    flowsCount: (count: number) => `Authorization flows: ${String(count)}`,
    reviewCount: (count: number) => `Needs review: ${String(count)}`,
    remoteCloseInfo: (count: number) => `Eligible for remote closure (informational): ${String(count)}`,
    localRetireInfo: (count: number) => `Eligible for local cleanup (informational): ${String(count)}`,
    retentionNote: (days: number) => `Advisory only — expired records are kept for at least ${String(days)} days and never removed automatically.`,
    validUntil: (formatted: string) => `Valid until ${formatted}`,
    daysLeft: (days: number) => `${String(days)} ${days === 1 ? "day" : "days"} left`,
    expired: "Expired",
    paused: "Paused",
    attention: (code: string) => `Attention: ${code}`,
    statuses: {
      attention_required: "Attention required",
      expired: "Expired",
      due_soon: "Renewal due soon",
      healthy: "Healthy",
      unknown_expiry: "Unknown expiry",
    },
    provenances: {
      hestia_created: "Managed by Hestia",
      external_file: "External (read-only)",
      poc_retired: "Retired PoC (read-only)",
    },
    classifications: {
      external_read_only: "External / read-only",
      active_pointer_mismatch: "Active pointer mismatch",
      uncertain_flow_evidence: "Uncertain exchange (protected)",
      stale_flow_evidence: "Stale flow evidence (protected)",
      active_current: "Active",
      active_expired: "Active (expired)",
      unadopted_in_progress: "Candidate in progress",
      unadopted_orphan: "Unadopted orphan",
      unadopted_expired_retained: "Expired unadopted (retained)",
      unadopted_expired_prunable: "Expired unadopted (cleanup candidate)",
      retired_still_valid: "Retired (still valid)",
      retired_retained: "Retired (retained)",
      retired_prunable: "Retired (cleanup candidate)",
      uncertain_exchange_review: "Uncertain exchange — review",
      stale_active_flow: "Stale unfinished flow",
      flow_in_progress: "Flow in progress",
      terminal_retained: "Finished flow (retained)",
      terminal_prunable: "Finished flow (cleanup candidate)",
    },
    consentsDetails: (count: number) => `Managed consent records (${String(count)})`,
    flowsDetails: (count: number) => `Authorization flow records (${String(count)})`,
    reviewBadge: "needs review",
    unknownBank: "Bank connection",
  },
  pl: {
    title: "Stan zgód i przypomnienia o odnowieniu",
    intro: "Wyłącznie podgląd ważności zgód bankowych oraz zapisanych prób autoryzacji. Nie zamyka sesji w banku, niczego nie usuwa i nie wysyła żądań do banku.",
    loading: "Wczytywanie stanu zgód…",
    error: "Nie udało się wczytać stanu zgód. Spróbuj ponownie.",
    renewalsTitle: "Ważność zgód bankowych",
    noRenewals: "Brak włączonych połączeń bankowych do oceny.",
    reconcileTitle: "Zapisane zgody i próby autoryzacji",
    externalVsManaged: (external: number, managed: number) =>
      `Zgody zewnętrzne (import): ${String(external)} · zarządzane przez Hestię: ${String(managed)}`,
    flowsCount: (count: number) => `Próby autoryzacji: ${String(count)}`,
    reviewCount: (count: number) => `Do przeglądu: ${String(count)}`,
    remoteCloseInfo: (count: number) => `Kwalifikują się do zamknięcia w banku (informacyjnie): ${String(count)}`,
    localRetireInfo: (count: number) => `Kwalifikują się do lokalnego porządkowania (informacyjnie): ${String(count)}`,
    retentionNote: (days: number) => `Wyłącznie informacyjnie — wygasłe wpisy są przechowywane co najmniej ${String(days)} dni i nigdy nie są usuwane automatycznie.`,
    validUntil: (formatted: string) => `Ważna do ${formatted}`,
    daysLeft: (days: number) => `Pozostało ${String(days)} ${pluralPl(days, "dzień", "dni", "dni")}`,
    expired: "Wygasła",
    paused: "Wstrzymane",
    attention: (code: string) => `Wymaga uwagi: ${code}`,
    statuses: {
      attention_required: "Wymaga uwagi",
      expired: "Wygasła",
      due_soon: "Wkrótce odnowienie",
      healthy: "Aktywna",
      unknown_expiry: "Nieznana ważność",
    },
    provenances: {
      hestia_created: "Zarządzana przez Hestię",
      external_file: "Zewnętrzna (tylko odczyt)",
      poc_retired: "Archiwalna PoC (tylko odczyt)",
    },
    classifications: {
      external_read_only: "Zewnętrzna / tylko odczyt",
      active_pointer_mismatch: "Rozbieżność aktywnego wskaźnika",
      uncertain_flow_evidence: "Niepewna wymiana (chroniona)",
      stale_flow_evidence: "Przeterminowany ślad próby (chroniony)",
      active_current: "Aktywna",
      active_expired: "Aktywna (wygasła)",
      unadopted_in_progress: "Kandydat w toku",
      unadopted_orphan: "Nieprzyjęty kandydat",
      unadopted_expired_retained: "Wygasły kandydat (zachowany)",
      unadopted_expired_prunable: "Wygasły kandydat (do porządkowania)",
      retired_still_valid: "Zastąpiona (nadal ważna)",
      retired_retained: "Zastąpiona (zachowana)",
      retired_prunable: "Zastąpiona (do porządkowania)",
      uncertain_exchange_review: "Niepewna wymiana — do przeglądu",
      stale_active_flow: "Przeterminowana niedokończona próba",
      flow_in_progress: "Próba w toku",
      terminal_retained: "Zakończona próba (zachowana)",
      terminal_prunable: "Zakończona próba (do porządkowania)",
    },
    consentsDetails: (count: number) => `Zgody zarządzane przez Hestię (${String(count)})`,
    flowsDetails: (count: number) => `Historia prób autoryzacji (${String(count)})`,
    reviewBadge: "do przeglądu",
    unknownBank: "Połączenie z bankiem",
  },
};
export type ConsentHealthLabels = typeof consentHealthLabels.en;

type Loss = (result: { ok: boolean; status?: number; error?: string }) => boolean;
type Loaded<T> = { state: "loading" } | { state: "error" } | { state: "ready"; data: T };

export interface ConsentHealthPanelProps {
  api: Pick<BudgetApi, "consentRenewalReminders" | "consentReconcile">;
  connections: readonly BudgetConnection[];
  text: ConsentHealthLabels;
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

export function ConsentHealthPanel({ api, connections, text, locale, revision, onAccessLoss }: ConsentHealthPanelProps) {
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
    connections.find((item) => item.id === connectionId)?.bank_name ?? text.unknownBank;
  const externalCount = reconcile.state === "ready"
    ? externalConsentCount(renewals.state === "ready" ? renewals.data : null, reconcile.data)
    : 0;

  return (
    <section className="budget-entries budget-consent-health" aria-label={text.title}>
      <h4>{text.title}</h4>
      <p className="budget-muted">{text.intro}</p>

      <h5>{text.renewalsTitle}</h5>
      {renewals.state === "loading" ? <p className="budget-muted" role="status">{text.loading}</p> : null}
      {renewals.state === "error" ? <p className="budget-notice" role="alert">{text.error}</p> : null}
      {renewals.state === "ready" ? (
        renewals.data.reminders.length === 0 ? <p className="budget-muted">{text.noRenewals}</p> : (
          <ul className="budget-entry-list">
            {renewals.data.reminders.map((item) => {
              const expiry = when(item.valid_until, locale);
              return (
                <li key={item.connection_id} className="budget-entry">
                  <div className="budget-limit-main">
                    <span className="budget-entry-text"><strong>{item.bank_name}</strong></span>
                    <span className="budget-chip budget-consent-status" data-status={item.status}>
                      {labelOf(text.statuses, item.status)}
                    </span>
                  </div>
                  <div className="budget-entry-facts">
                    {expiry === null ? null : <span>{text.validUntil(expiry)}</span>}
                    {item.days_remaining === null ? null : (
                      <span>{item.days_remaining < 0 || item.status === "expired"
                        ? text.expired : text.daysLeft(item.days_remaining)}</span>
                    )}
                    {item.paused ? <span className="budget-warn">{text.paused}</span> : null}
                    {item.attention === null ? null : <span className="budget-warn">{text.attention(item.attention)}</span>}
                  </div>
                </li>
              );
            })}
          </ul>
        )
      ) : null}

      <h5>{text.reconcileTitle}</h5>
      {reconcile.state === "loading" ? <p className="budget-muted" role="status">{text.loading}</p> : null}
      {reconcile.state === "error" ? <p className="budget-notice" role="alert">{text.error}</p> : null}
      {reconcile.state === "ready" ? (
        <>
          <div className="budget-entry-facts">
            <strong>{text.externalVsManaged(externalCount, reconcile.data.summary.consents_total)}</strong>
            <span>{text.flowsCount(reconcile.data.summary.flows_total)}</span>
            <span className={reconcile.data.summary.needs_owner_review > 0 ? "budget-warn" : undefined}>
              {text.reviewCount(reconcile.data.summary.needs_owner_review)}
            </span>
            {reconcile.data.summary.remote_close_eligible > 0 ? (
              <span>{text.remoteCloseInfo(reconcile.data.summary.remote_close_eligible)}</span>
            ) : null}
            {reconcile.data.summary.local_retire_eligible > 0 ? (
              <span>{text.localRetireInfo(reconcile.data.summary.local_retire_eligible)}</span>
            ) : null}
          </div>
          <p className="budget-muted">{text.retentionNote(reconcile.data.retention_days)}</p>
          {reconcile.data.consents.length > 0 ? (
            <details className="budget-recurring-evidence">
              <summary>{text.consentsDetails(reconcile.data.consents.length)}</summary>
              <ul>
                {reconcile.data.consents.map((item) => {
                  const expiry = when(item.valid_until, locale);
                  return (
                    <li key={item.consent_id}>
                      <span>{bankName(item.connection_id)}</span>
                      {" · "}
                      <span>{labelOf(text.classifications, item.classification)}</span>
                      {" · "}
                      <span>{labelOf(text.provenances, item.provenance)}</span>
                      {expiry === null ? null : <>{" · "}<span>{text.validUntil(expiry)}</span></>}
                      {item.needs_owner_review ? <>{" · "}<strong className="budget-warn">{text.reviewBadge}</strong></> : null}
                    </li>
                  );
                })}
              </ul>
            </details>
          ) : null}
          {reconcile.data.flows.length > 0 ? (
            <details className="budget-recurring-evidence">
              <summary>{text.flowsDetails(reconcile.data.flows.length)}</summary>
              <ul>
                {reconcile.data.flows.map((item) => (
                  <li key={item.flow_id}>
                    <span>{bankName(item.connection_id)}</span>
                    {" · "}
                    <span>{labelOf(text.classifications, item.classification)}</span>
                    {item.needs_owner_review ? <>{" · "}<strong className="budget-warn">{text.reviewBadge}</strong></> : null}
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
