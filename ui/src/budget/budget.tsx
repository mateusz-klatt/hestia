// Owner-private budget view (2C). Reads only the stored-data API; the "refresh" action queues a server job
// under the server's quotas and never talks to a bank from the browser. Data live only in component state.
import { useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { currentLocale } from "../i18n";
import { budgetApi } from "./api";
import type { BudgetApi, BudgetResult } from "./api";
import { ACTIVE_JOB, KINDS, accountLabel, effectiveState, formatAmount, freshness, hasActiveJob, idempotencyKey,
  isAccessLoss, isComponentLimit, latestJob, limitState, newer, otherObservedCurrencies, presetRange,
  transactionDate, transactionText, validRange, when } from "./model";
import type { BudgetAccount, BudgetBalances, BudgetCapabilities, BudgetConnection, BudgetJob, BudgetKind,
  BudgetLimit, BudgetSharedLimit, BudgetTransaction, DateRange, LimitState } from "./model";
import "./budget.css";

const labels = {
  en: {
    title: "Budget", subtitle: "Your private household finances.", reload: "Reload", loading: "Loading budget…",
    disabled: "The budget is not enabled on this server.", denied: "Only the budget's owner can view it.",
    error: "The budget could not be loaded. Try again.", empty: "No bank connections are configured.",
    account: "Account", refresh: "Refresh from bank", refreshing: "Refresh queued…",
    refreshOff: "Refreshing is disabled on the server.", pause: "Pause", resume: "Resume", paused: "Paused",
    background: "Automatic refresh", backgroundOff: "automatic refresh is disabled on the server",
    attention: "Attention: unattended refresh stopped — refresh manually; reconnecting may be needed.",
    renewal: "Bank consent expires soon", validUntil: "Consent valid until", lastSuccess: "Last full refresh",
    never: "never", selected: "Include in budget", kind: "Account type", kindUnknown: "Choose type…",
    kinds: { current: "Current", savings: "Savings", credit_card: "Credit card", other: "Other" },
    freshness: { never: "not downloaded yet", fresh: "up to date", stale: "outdated", suspended: "suspended" },
    transactions: "Transactions", balances: "Balances", from: "From", to: "To", filter: "Apply",
    more: "Load more", noTransactions: "No transactions in this range.", noBalances: "No balances stored yet.",
    uncertain: "uncertain entries kept separately", archived: "archived balance imports", close: "Close",
    job: { queued: "queued", running: "running", updated: "updated", partial: "partly updated", failed: "failed",
      deferred: "not due yet", interrupted: "interrupted" },
    next: "next possible", conflict: "This changed elsewhere; the latest state was loaded.",
    busy: "A refresh is already in progress for this connection.", pausedError: "Resume the connection first.",
    unavailable: "Refresh is unavailable right now.", saveError: "Could not save. Try again.",
    limits: "Download limits", limitsHide: "Hide limits", limitsLoading: "Loading limits…",
    limitsError: "Limits could not be loaded.",
    limitsIntro: "Each account and data type is downloaded at most twice a day, at least 12 hours apart; a bank can ask for a longer pause. These are read-only.",
    scopes: { application: "All banks", provider: "This bank", connection: "This connection" },
    sharedFree: "Shared limits: no restriction", limitReady: "available now", limitFrom: "from",
    limitRunning: "download in progress", limitSuspended: "suspended — needs the operator",
    limitRetry: "bank asked for a pause until", lastTry: "last attempt", excluded: "excluded",
    lastRefresh: "Last refresh", observed: "balances seen in", badRange: "The start date must not be after the end date.",
    presets: { "30": "Last 30 days", "90": "Last 90 days", year: "This year", all: "All" },
    archivedHelp: "Earlier imported balance snapshots (metadata only, not current funds)",
    fetched: "downloaded", imported: "imported", entries: "balances",
    balanceTypes: { CLBD: "Booked (closing)", ITBD: "Booked (interim)", OPBD: "Booked (opening)",
      ITAV: "Available (interim)", CLAV: "Available (closing)", OPAV: "Available (opening)", FWAV: "Forward available",
      XPCD: "Expected", PRCD: "Previously closed" },
  },
  pl: {
    title: "Budżet", subtitle: "Prywatne finanse domowe.", reload: "Wczytaj ponownie", loading: "Wczytywanie budżetu…",
    disabled: "Budżet nie jest włączony na tym serwerze.", denied: "Budżet widzi tylko jego właściciel.",
    error: "Nie udało się wczytać budżetu. Spróbuj ponownie.", empty: "Brak skonfigurowanych połączeń z bankiem.",
    account: "Konto", refresh: "Odśwież z banku", refreshing: "Odświeżanie w kolejce…",
    refreshOff: "Odświeżanie jest wyłączone na serwerze.", pause: "Wstrzymaj", resume: "Wznów", paused: "Wstrzymane",
    background: "Automatyczne odświeżanie", backgroundOff: "automatyczne odświeżanie jest wyłączone na serwerze",
    attention: "Uwaga: automatyczne odświeżanie zatrzymane — odśwież ręcznie; może być potrzebne ponowne połączenie.",
    renewal: "Zgoda banku wkrótce wygaśnie", validUntil: "Zgoda ważna do", lastSuccess: "Ostatnie pełne odświeżenie",
    never: "nigdy", selected: "Uwzględnij w budżecie", kind: "Rodzaj konta", kindUnknown: "Wybierz rodzaj…",
    kinds: { current: "Osobiste", savings: "Oszczędnościowe", credit_card: "Karta kredytowa", other: "Inne" },
    freshness: { never: "jeszcze nie pobrano", fresh: "aktualne", stale: "nieaktualne", suspended: "wstrzymane" },
    transactions: "Transakcje", balances: "Salda", from: "Od", to: "Do", filter: "Pokaż",
    more: "Pokaż więcej", noTransactions: "Brak transakcji w tym zakresie.", noBalances: "Brak zapisanych sald.",
    uncertain: "niepewnych wpisów trzymanych osobno", archived: "zarchiwizowanych importów sald", close: "Zamknij",
    job: { queued: "w kolejce", running: "w toku", updated: "zaktualizowano", partial: "częściowo zaktualizowano",
      failed: "błąd", deferred: "jeszcze nie teraz", interrupted: "przerwane" },
    next: "następne najwcześniej", conflict: "Zmieniono to w innym miejscu; wczytano aktualny stan.",
    busy: "Dla tego połączenia trwa już odświeżanie.", pausedError: "Najpierw wznów połączenie.",
    unavailable: "Odświeżanie jest teraz niedostępne.", saveError: "Nie udało się zapisać. Spróbuj ponownie.",
    limits: "Limity pobrań", limitsHide: "Ukryj limity", limitsLoading: "Wczytywanie limitów…",
    limitsError: "Nie udało się wczytać limitów.",
    limitsIntro: "Każde konto i rodzaj danych pobieramy najwyżej 2 razy na dobę, co najmniej co 12 godzin; bank może zażądać dłuższej przerwy. To tylko podgląd.",
    scopes: { application: "Wszystkie banki", provider: "Ten bank", connection: "To połączenie" },
    sharedFree: "Wspólne limity: bez ograniczeń", limitReady: "dostępne teraz", limitFrom: "od",
    limitRunning: "pobieranie w toku", limitSuspended: "wstrzymane — wymaga decyzji operatora",
    limitRetry: "bank prosi o przerwę do", lastTry: "ostatnia próba", excluded: "wykluczone",
    lastRefresh: "Ostatnie odświeżenie", observed: "salda widziane w", badRange: "Data początkowa nie może być późniejsza niż końcowa.",
    presets: { "30": "Ostatnie 30 dni", "90": "Ostatnie 90 dni", year: "Ten rok", all: "Wszystko" },
    archivedHelp: "Wcześniej zaimportowane migawki sald (tylko metadane, nie bieżące środki)",
    fetched: "pobrano", imported: "zaimportowano", entries: "sald",
    balanceTypes: { CLBD: "Zaksięgowane (na koniec dnia)", ITBD: "Zaksięgowane (bieżące)", OPBD: "Zaksięgowane (otwarcie)",
      ITAV: "Dostępne (bieżące)", CLAV: "Dostępne (na koniec dnia)", OPAV: "Dostępne (otwarcie)", FWAV: "Dostępne w przyszłości",
      XPCD: "Oczekiwane", PRCD: "Poprzednie zamknięcie" },
  },
};
type Labels = typeof labels.en;

function pick(): { text: Labels; locale: string } {
  const locale = currentLocale();
  return { text: locale.startsWith("pl") ? labels.pl : labels.en, locale };
}

type Load = "loading" | "ready" | "disabled" | "denied" | "error";

function failureText(text: Labels, error: string): string {
  switch (error) {
    case "budget_version_conflict": return text.conflict;
    case "budget_refresh_in_progress": return text.busy;
    case "budget_connection_paused": return text.pausedError;
    case "budget_refresh_unavailable": return text.unavailable;
    default: return text.saveError;
  }
}

function balanceLabel(text: Labels, type: string, name: string): string {
  return (text.balanceTypes as Record<string, string>)[type] ?? (name || type);
}

function jobText(text: Labels, job: BudgetJob, locale: string): string {
  const status = (text.job as Record<string, string>)[job.status] ?? job.status;
  const next = job.next_attempt !== null && !ACTIVE_JOB.has(job.status) ? when(job.next_attempt, locale) : null;
  return [`${text.lastRefresh}: ${status}`, job.error, next === null ? null : `${text.next} ${next}`]
    .filter((part) => part !== null && part !== "").join(" · ");
}

export interface BudgetProps {
  active: boolean; api?: BudgetApi; pollMs?: number;
  /** The user this page signed in as; budget data are shown only while the cookie still names them. */
  principal?: string | null;
}

export function Budget({ active, api = budgetApi, pollMs = 2000, principal }: BudgetProps) {
  const { text, locale } = pick();
  const [load, setLoad] = useState<Load>("loading");
  const [connections, setConnections] = useState<BudgetConnection[]>([]);
  const [capabilities, setCapabilities] = useState<BudgetCapabilities | null>(null);
  const [accounts, setAccounts] = useState<BudgetAccount[]>([]);
  const [jobs, setJobs] = useState<BudgetJob[]>([]);
  const [showLimits, setShowLimits] = useState(false);
  const [limits, setLimits] = useState<BudgetLimit[] | null | "error">(null);
  const [detail, setDetail] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set());
  const retryKeys = useRef(new Map<string, string>());
  // Lifecycle guards: `alive` drops work finishing after unmount; every reload takes a new `generation`
  // so an older load can never overwrite a newer one; `epoch` changes only when data are cleared (signed
  // out, other principal, disabled), so no response started before that may bring the data back.
  const alive = useRef(true);
  const generation = useRef(0);
  const epoch = useRef(0);
  const [revision, setRevision] = useState(0);
  const [pollTick, setPollTick] = useState(0);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; generation.current += 1; };
  }, []);

  /** The single access barrier: any endpoint reporting a lost principal or a disabled feature clears every
   *  private value at once and invalidates all pending loads, polls and saves. */
  const invalidate = useCallback((disabled: boolean) => {
    epoch.current += 1;
    generation.current += 1;
    setConnections([]); setAccounts([]); setJobs([]); setDetail(null); setCapabilities(null); setBusy(new Set());
    setLimits(null); setShowLimits(false);
    setLoad(disabled ? "disabled" : "denied");
  }, []);
  const barrier = useCallback((result: { ok: boolean; status?: number; error?: string }): boolean => {
    if (!isAccessLoss(result)) return false;
    if (alive.current) invalidate(!result.ok && result.error === "budget_disabled");
    return true;
  }, [invalidate]);

  const reload = useCallback(async () => {
    const token = ++generation.current;
    // Each response passes the barrier as soon as it arrives; a slow sibling cannot delay clearing.
    const watch = <T,>(request: Promise<BudgetResult<T>>) => request.then((result) => {
      // A lost-access answer clears the view even when a newer load has started since.
      barrier(result);
      return result;
    });
    // A successful whoami naming someone else, or the owner without the admin role, is also a loss of access
    // and clears at once, without waiting for the other responses.
    const identity = principal === undefined ? Promise.resolve(null) : watch(api.whoami()).then((result) => {
      if (result.ok && (result.data.user !== principal || result.data.role !== "admin")
          && alive.current) invalidate(false);
      return result;
    });
    const [connected, listed, queued, me] = await Promise.all([watch(api.connections()), watch(api.accounts()),
      watch(api.jobs()), identity]);
    if (!alive.current || token !== generation.current) return;
    if (!connected.ok || !listed.ok || (me !== null && !me.ok)) {
      setLoad("error");
      return;
    }
    setConnections((current) => newer(current, connected.data.connections));
    setCapabilities(connected.data.capabilities);
    setAccounts((current) => newer(current, listed.data.accounts));
    if (queued.ok) setJobs(queued.data.jobs);
    setLoad("ready");
    setRevision((current) => current + 1);
  }, [api, principal, barrier, invalidate]);

  useEffect(() => {
    if (active) void reload();
  }, [active, reload]);

  // Poll only while a job is queued/running and the view is visible; reload stored data once it settles.
  // A failed poll re-arms the timer; an authorization failure goes through reload, which clears data.
  const polling = active && hasActiveJob(jobs);
  useEffect(() => {
    if (!polling) return undefined;
    let cancelled = false;
    const timer = setTimeout(() => {
      void api.jobs().then((result) => {
        // The access barrier runs first: even a poll whose timer was superseded must clear on lost access.
        if (!alive.current || barrier(result) || cancelled) return;
        if (!result.ok) {
          setPollTick((current) => current + 1);
          return;
        }
        setJobs(result.data.jobs);
        if (!hasActiveJob(result.data.jobs)) void reload();
      });
    }, pollMs);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [polling, jobs, pollTick, api, pollMs, reload, barrier]);

  /** Runs one mutation; null when the view was unmounted or its data cleared while it was in flight. */
  const run = useCallback(async <T,>(key: string, action: () => Promise<BudgetResult<T>>): Promise<BudgetResult<T> | null> => {
    const started = epoch.current;
    setBusy((current) => new Set(current).add(key));
    setNotice(null);
    const result = await action();
    if (!alive.current || started !== epoch.current || barrier(result)) return null;
    setBusy((current) => { const next = new Set(current); next.delete(key); return next; });
    if (!result.ok) {
      setNotice(failureText(text, result.error));
      if (result.error === "budget_version_conflict") await reload();
    }
    return result;
  }, [reload, barrier, text]);

  const refresh = useCallback(async (connection: BudgetConnection) => {
    // Reuse the key only to retry a request whose outcome is unknown (network failure).
    const key = retryKeys.current.get(connection.id) ?? idempotencyKey();
    retryKeys.current.set(connection.id, key);
    const result = await run(`refresh:${connection.id}`, () => api.refresh(connection, {}, key));
    if (result === null) return;
    // Keep the key while the outcome is unknown (network loss, unreadable 2xx, 5xx); drop it only after a
    // job was returned or the server definitively refused without creating one.
    const definitive = result.ok || (result.status >= 400 && result.status < 500)
      || result.error === "budget_refresh_unavailable";
    if (definitive) retryKeys.current.delete(connection.id);
    if (result.ok) {
      const job = result.data.job;
      setJobs((current) => [job, ...current.filter((item) => item.id !== job.id)]);
    }
  }, [api, run]);

  // A saved entity replaces the shown one only when it is not older than what a later load already shows.
  const saveConnection = useCallback(async (key: string, action: () => Promise<BudgetResult<{ connection: BudgetConnection }>>) => {
    const result = await run(key, action);
    if (result?.ok !== true) return;
    const saved = result.data.connection;
    setConnections((current) => current.map((item) =>
      (item.id === saved.id && saved.control_version >= item.control_version ? saved : item)));
    if (key.startsWith("pause:")) {
      // Pause changes every account's `active` flag; re-read accounts within the same data epoch.
      const started = epoch.current;
      const listed = await api.accounts();
      if (!alive.current || started !== epoch.current || barrier(listed)) return;
      if (listed.ok) setAccounts((current) => newer(current, listed.data.accounts));
    }
  }, [api, run, barrier]);

  const saveAccount = useCallback(async (key: string, action: () => Promise<BudgetResult<{ account: BudgetAccount }>>) => {
    const result = await run(key, action);
    if (result?.ok !== true) return;
    const saved = result.data.account;
    setAccounts((current) => current.map((item) =>
      (item.id === saved.id && saved.control_version >= item.control_version ? { ...item, ...saved } : item)));
  }, [run]);

  // Limits are read on demand, and again after every completed reload while shown.
  useEffect(() => {
    if (!active || !showLimits) return undefined;
    let cancelled = false;
    const started = epoch.current;
    void api.limits().then((result) => {
      // Barrier before the local discard: hiding the panel must not swallow a lost-access answer.
      if (!alive.current || started !== epoch.current || barrier(result) || cancelled) return;
      setLimits(result.ok ? result.data.limits : "error");
    });
    return () => { cancelled = true; };
  }, [active, showLimits, revision, api, barrier]);

  if (!active) return null;
  const header = (
    <header className="budget-header">
      <div><h2>{text.title}</h2><p>{text.subtitle}</p></div>
      <div className="budget-actions">
        {load === "ready" ? (
          <button type="button" aria-pressed={showLimits} onClick={() => { setShowLimits((current) => !current); }}>
            {showLimits ? text.limitsHide : text.limits}
          </button>
        ) : null}
        <button type="button" onClick={() => { void reload(); }}>{text.reload}</button>
      </div>
    </header>
  );
  if (load !== "ready") {
    const message = { loading: text.loading, disabled: text.disabled, denied: text.denied, error: text.error }[load];
    return <section className="budget-app">{header}<p className="budget-status" role="status">{message}</p></section>;
  }
  const opened = accounts.find((account) => account.id === detail) ?? null;
  return (
    <section className="budget-app">
      {header}
      {notice === null ? null : <p className="budget-notice" role="alert">{notice}</p>}
      {showLimits ? <p className="budget-muted">{text.limitsIntro}</p> : null}
      {connections.length === 0 ? <p className="budget-status">{text.empty}</p> : null}
      {connections.map((connection) => {
        const job = latestJob(jobs, connection.id);
        const owned = accounts.filter((account) => account.connection === connection.id);
        const refreshing = busy.has(`refresh:${connection.id}`) || (job !== null && ACTIVE_JOB.has(job.status));
        return (
          <article className="budget-connection" key={connection.id} aria-label={connection.bank_name}>
            <header>
              <h3>{connection.bank_name} <small>{connection.country}</small></h3>
              <div className="budget-actions">
                {capabilities?.refresh === true ? (
                  <button type="button" className="budget-primary" disabled={refreshing || connection.paused}
                    onClick={() => { void refresh(connection); }}>{refreshing ? text.refreshing : text.refresh}</button>
                ) : <span className="budget-muted">{text.refreshOff}</span>}
                <button type="button" disabled={busy.has(`pause:${connection.id}`)}
                  onClick={() => { void saveConnection(`pause:${connection.id}`, () => api.setPaused(connection, !connection.paused)); }}>
                  {connection.paused ? text.resume : text.pause}
                </button>
              </div>
            </header>
            <ul className="budget-facts">
              {connection.paused ? <li className="budget-warn">{text.paused}</li> : null}
              {connection.attention === null ? null : <li className="budget-warn">{text.attention}</li>}
              {connection.renewal_due ? <li className="budget-warn">{text.renewal}</li> : null}
              {connection.valid_until === null ? null : <li>{text.validUntil}: {when(connection.valid_until, locale)}</li>}
              <li>{text.lastSuccess}: {when(connection.last_success, locale) ?? text.never}</li>
              {job === null ? null : <li>{jobText(text, job, locale)}</li>}
            </ul>
            {capabilities?.background_selection === true ? (
              <label className="budget-toggle">
                <input type="checkbox" checked={connection.background_enabled} disabled={busy.has(`background:${connection.id}`)}
                  onChange={(event) => { const enabled = event.currentTarget.checked; void saveConnection(`background:${connection.id}`, () => api.setBackground(connection, enabled)); }} />
                {text.background}
                {capabilities.background_polling ? null : <span className="budget-muted"> ({text.backgroundOff})</span>}
              </label>
            ) : null}
            <ul className="budget-accounts">
              {owned.map((account, index) => (
                <AccountRow key={account.id} account={account} index={index} text={text} busy={busy}
                  onOpen={() => { setDetail(account.id); }}
                  onSelect={(selected) => { void saveAccount(`select:${account.id}`, () => api.setSelected(account, selected)); }}
                  onKind={(kind) => { void saveAccount(`kind:${account.id}`, () => api.setKind(account, kind)); }} />
              ))}
            </ul>
            {showLimits ? (
              <LimitsPanel connectionId={connection.id} accounts={owned} limits={limits} text={text} locale={locale} />
            ) : null}
          </article>
        );
      })}
      {opened === null ? null : (
        <AccountDetail account={opened} label={accountLabel(opened, accounts.filter((a) => a.connection === opened.connection).indexOf(opened), text.account)}
          api={api} text={text} locale={locale} revision={revision} onAccessLoss={barrier}
          onClose={() => { setDetail(null); }} />
      )}
    </section>
  );
}

function stateText(text: Labels, state: LimitState, locale: string): string {
  switch (state.kind) {
    case "suspended": return text.limitSuspended;
    case "running": return text.limitRunning;
    case "waiting": return `${state.retry ? text.limitRetry : text.limitFrom} ${when(state.until, locale) ?? ""}`;
    default: return text.limitReady;
  }
}

interface LimitsProps {
  connectionId: string; accounts: BudgetAccount[]; limits: BudgetLimit[] | null | "error"; text: Labels; locale: string;
}

/** Read-only view of stored limits for one connection; there is deliberately no way to lift any of them here. */
function LimitsPanel({ connectionId, accounts, limits, text, locale }: LimitsProps) {
  if (limits === null) return <p className="budget-muted" role="status">{text.limitsLoading}</p>;
  if (limits === "error") return <p className="budget-notice" role="alert">{text.limitsError}</p>;
  const now = Date.now() / 1000;
  const own = limits.filter((limit) => limit.connection === connectionId);
  const shared = own.filter((limit): limit is BudgetSharedLimit => !isComponentLimit(limit));
  const blocked = shared.map((limit) => [limit, limitState(limit, now)] as const).filter(([, state]) => state.kind !== "ready");
  const component = (accountId: string, operation: "transactions" | "balances") =>
    own.find((limit) => isComponentLimit(limit) && limit.account === accountId && limit.operation === operation);
  return (
    <section className="budget-limits" aria-label={text.limits}>
      <ul className="budget-facts">
        {blocked.length === 0 ? <li>{text.sharedFree}</li> : blocked.map(([limit, state]) => (
          <li key={limit.scope} className="budget-warn">
            {(text.scopes as Record<string, string>)[limit.scope] ?? limit.scope}: {stateText(text, state, locale)}
          </li>
        ))}
      </ul>
      <table className="budget-limits-table">
        <thead><tr><th scope="col">{text.account}</th><th scope="col">{text.transactions}</th><th scope="col">{text.balances}</th></tr></thead>
        <tbody>
          {accounts.map((account, index) => (
            <tr key={account.id} className={account.selected ? undefined : "is-excluded"}>
              <th scope="row">
                {accountLabel(account, index, text.account)}
                {account.selected ? null : <small> ({text.excluded})</small>}
              </th>
              {(["transactions", "balances"] as const).map((operation) => {
                const limit = component(account.id, operation);
                if (limit === undefined || !isComponentLimit(limit)) return <td key={operation}>—</td>;
                const state = effectiveState(limit, shared, now);
                const last = when(limit.last_attempt, locale);
                return (
                  <td key={operation} data-state={state.kind}>
                    {stateText(text, state, locale)}
                    {last === null ? null : <small className="budget-muted"><br />{text.lastTry}: {last}</small>}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

interface RowProps {
  account: BudgetAccount; index: number; text: Labels; busy: ReadonlySet<string>;
  onOpen: () => void; onSelect: (selected: boolean) => void; onKind: (kind: BudgetKind) => void;
}

function AccountRow({ account, index, text, busy, onOpen, onSelect, onKind }: RowProps) {
  const kind = account.kind_confirmed_at === null ? "" : account.kind;
  const state = (name: "transactions" | "balances") => text.freshness[freshness(account.components[name])];
  return (
    <li className={account.selected ? "budget-account" : "budget-account is-excluded"}>
      <button type="button" className="budget-account-name" onClick={onOpen}>
        {accountLabel(account, index, text.account)}
        {account.display?.currency ? <small> {account.display.currency}</small> : null}
      </button>
      {otherObservedCurrencies(account).length === 0 ? null
        : <span className="budget-muted">{text.observed}: {otherObservedCurrencies(account).join(", ")}</span>}
      <span className="budget-muted">{text.transactions}: {state("transactions")} · {text.balances}: {state("balances")}</span>
      <label className="budget-toggle">
        <input type="checkbox" checked={account.selected} disabled={!account.configured || busy.has(`select:${account.id}`)}
          onChange={(event) => { onSelect(event.currentTarget.checked); }} />
        {text.selected}
      </label>
      <label className="budget-kind">
        <span>{text.kind}</span>
        <select value={kind} disabled={busy.has(`kind:${account.id}`)}
          onChange={(event) => { if (event.currentTarget.value !== "") onKind(event.currentTarget.value as BudgetKind); }}>
          <option value="" disabled>{text.kindUnknown}</option>
          {KINDS.map((item) => <option key={item} value={item}>{text.kinds[item]}</option>)}
        </select>
      </label>
    </li>
  );
}

interface DetailProps {
  account: BudgetAccount; label: string; api: BudgetApi; text: Labels; locale: string; revision: number;
  /** Reports a lost principal/disabled response so the whole view clears, not just this panel. */
  onAccessLoss: (result: { ok: boolean; status?: number; error?: string }) => boolean;
  onClose: () => void;
}

interface Ledger { rows: BudgetTransaction[]; cursor: string | null; uncertain: number; failed: boolean; loading: boolean }

const EMPTY_LEDGER: Ledger = { rows: [], cursor: null, uncertain: 0, failed: false, loading: true };

function AccountDetail({ account, label, api, text, locale, revision, onAccessLoss, onClose }: DetailProps) {
  const panel = useRef<HTMLElement>(null);
  useEffect(() => {
    // The detail renders below every connection; bring it into view when another account is opened.
    panel.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [account.id]);
  const [balances, setBalances] = useState<BudgetBalances | null>(null);
  const [ledger, setLedger] = useState<Ledger>(EMPTY_LEDGER);
  const [range, setRange] = useState<DateRange>({ from: "", to: "" });
  const [draft, setDraft] = useState<DateRange>({ from: "", to: "" });
  // Every account/filter change starts a new generation; a late response from an older one is dropped,
  // so a previous account's or filter's rows can never replace the current ones.
  const generation = useRef(0);

  // A completed reload (for example after a refresh job settles) re-reads the open account as well.
  useEffect(() => {
    const token = ++generation.current;
    setLedger(EMPTY_LEDGER);
    setBalances(null);
    void api.balances(account.id).then((result) => {
      if (onAccessLoss(result) || token !== generation.current) return;
      setBalances(result.ok ? result.data : null);
    });
    void api.transactions(account.id, { dateFrom: range.from, dateTo: range.to }).then((result) => {
      if (onAccessLoss(result) || token !== generation.current) return;
      setLedger(result.ok
        ? { rows: result.data.transactions, cursor: result.data.next_cursor, uncertain: result.data.uncertain_total,
            failed: false, loading: false }
        : { ...EMPTY_LEDGER, failed: true, loading: false });
    });
  }, [api, account.id, range, revision, onAccessLoss]);
  useEffect(() => () => { generation.current += 1; }, []);

  const more = useCallback(async () => {
    const token = generation.current;
    const cursor = ledger.cursor;
    if (cursor === null) return;
    setLedger((current) => ({ ...current, loading: true }));
    const result = await api.transactions(account.id, { cursor, dateFrom: range.from, dateTo: range.to });
    if (onAccessLoss(result) || token !== generation.current) return;
    setLedger((current) => (result.ok
      ? { rows: [...current.rows, ...result.data.transactions], cursor: result.data.next_cursor,
          uncertain: result.data.uncertain_total, failed: false, loading: false }
      : { ...current, failed: true, loading: false }));
  }, [api, account.id, range, ledger.cursor, onAccessLoss]);

  const apply = (next: DateRange) => { setDraft(next); if (validRange(next)) setRange(next); };
  return (
    <aside className="budget-detail" aria-label={label} ref={panel}>
      <header><h3>{label}</h3><button type="button" onClick={onClose}>{text.close}</button></header>
      <h4>{text.balances}</h4>
      {balances === null || balances.balances.length === 0 ? <p className="budget-muted">{text.noBalances}</p> : (
        <ul className="budget-balances">
          {balances.balances.map((balance, index) => (
            <li key={`${balance.balance_type}-${String(index)}`}>
              <span title={balance.name || balance.balance_type}>{balanceLabel(text, balance.balance_type, balance.name)}</span>
              <strong>{formatAmount(balance.balance_amount.amount, balance.balance_amount.currency, locale)}</strong>
            </li>
          ))}
        </ul>
      )}
      {balances !== null && balances.archived_imports.length > 0 ? (
        <details className="budget-archived">
          <summary>{text.archivedHelp}</summary>
          <ul>
            {balances.archived_imports.map((item) => (
              <li key={item.id}>
                {text.fetched} {when(item.fetched_at, locale)} · {text.imported} {when(item.imported_at, locale)}
                {" · "}{item.balance_count} {text.entries}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
      <h4>{text.transactions}</h4>
      <div className="budget-presets" role="group" aria-label={text.transactions}>
        {(["30", "90", "year", "all"] as const).map((preset) => (
          <button type="button" key={preset} onClick={() => { apply(presetRange(preset)); }}>{text.presets[preset]}</button>
        ))}
      </div>
      <form className="budget-range" onSubmit={(event) => { event.preventDefault(); apply(draft); }}>
        <label>{text.from}<input type="date" value={draft.from} max={draft.to || undefined}
          onChange={(event) => { setDraft({ ...draft, from: event.currentTarget.value }); }} /></label>
        <label>{text.to}<input type="date" value={draft.to} min={draft.from || undefined}
          onChange={(event) => { setDraft({ ...draft, to: event.currentTarget.value }); }} /></label>
        <button type="submit">{text.filter}</button>
      </form>
      {validRange(draft) ? null : <p className="budget-notice" role="alert">{text.badRange}</p>}
      {ledger.failed ? <p className="budget-notice" role="alert">{text.error}</p> : null}
      {ledger.loading && ledger.rows.length === 0 ? <p className="budget-muted" role="status">{text.loading}</p> : null}
      {!ledger.loading && ledger.rows.length === 0 && !ledger.failed ? <p className="budget-muted">{text.noTransactions}</p> : null}
      {ledger.rows.length === 0 ? null : (
        <table className="budget-transactions">
          <tbody>
            {ledger.rows.map((row) => (
              <tr key={row.entry_reference}>
                <td>{transactionDate(row)}</td>
                <td>{transactionText(row)}</td>
                <td className={row.credit_debit_indicator === "DBIT" ? "is-debit" : "is-credit"}>
                  {formatAmount(row.amount, row.currency, locale, row.credit_debit_indicator)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {ledger.uncertain > 0 ? <p className="budget-muted">{ledger.uncertain} {text.uncertain}</p> : null}
      {ledger.cursor === null ? null
        : <button type="button" disabled={ledger.loading} onClick={() => { void more(); }}>{text.more}</button>}
    </aside>
  );
}

export function mountBudget(element: HTMLElement, principal: string | null): { setActive: (active: boolean) => void; destroy: () => void } {
  const root = createRoot(element);
  return {
    setActive: (active) => { root.render(<Budget active={active} principal={principal} />); },
    destroy: () => { root.unmount(); },
  };
}
