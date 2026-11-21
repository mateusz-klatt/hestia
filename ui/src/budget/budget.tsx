// Owner-private budget view (2C). Reads only the stored-data API; the "refresh" action queues a server job
// under the server's quotas and never talks to a bank from the browser. Data live only in component state.
import { useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { currentLocale } from "../i18n";
import { budgetApi } from "./api";
import type { BudgetApi, BudgetResult } from "./api";
import { ACTIVE_JOB, KINDS, accountLabel, formatAmount, freshness, hasActiveJob, idempotencyKey, latestJob,
  otherObservedCurrencies, presetRange, transactionDate, transactionText, validRange, when } from "./model";
import type { BudgetAccount, BudgetBalances, BudgetCapabilities, BudgetConnection, BudgetJob, BudgetKind,
  BudgetTransaction, DateRange } from "./model";
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
    lastRefresh: "Last refresh", observed: "balances seen in", badRange: "The start date must not be after the end date.",
    presets: { "30": "Last 30 days", "90": "Last 90 days", year: "This year", all: "All" },
    archivedHelp: "Earlier imported balance snapshots (metadata only, not current funds)",
    fetched: "downloaded", imported: "imported", entries: "balances",
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
    lastRefresh: "Ostatnie odświeżenie", observed: "salda widziane w", badRange: "Data początkowa nie może być późniejsza niż końcowa.",
    presets: { "30": "Ostatnie 30 dni", "90": "Ostatnie 90 dni", year: "Ten rok", all: "Wszystko" },
    archivedHelp: "Wcześniej zaimportowane migawki sald (tylko metadane, nie bieżące środki)",
    fetched: "pobrano", imported: "zaimportowano", entries: "sald",
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

function jobText(text: Labels, job: BudgetJob, locale: string): string {
  const status = (text.job as Record<string, string>)[job.status] ?? job.status;
  const next = job.next_attempt !== null && !ACTIVE_JOB.has(job.status) ? when(job.next_attempt, locale) : null;
  return [`${text.lastRefresh}: ${status}`, job.error, next === null ? null : `${text.next} ${next}`]
    .filter((part) => part !== null && part !== "").join(" · ");
}

export interface BudgetProps { active: boolean; api?: BudgetApi; pollMs?: number }

export function Budget({ active, api = budgetApi, pollMs = 2000 }: BudgetProps) {
  const { text, locale } = pick();
  const [load, setLoad] = useState<Load>("loading");
  const [connections, setConnections] = useState<BudgetConnection[]>([]);
  const [capabilities, setCapabilities] = useState<BudgetCapabilities | null>(null);
  const [accounts, setAccounts] = useState<BudgetAccount[]>([]);
  const [jobs, setJobs] = useState<BudgetJob[]>([]);
  const [detail, setDetail] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set());
  const retryKeys = useRef(new Map<string, string>());

  const reload = useCallback(async () => {
    const [connected, listed, queued] = await Promise.all([api.connections(), api.accounts(), api.jobs()]);
    if (!connected.ok) {
      const denied = connected.status === 401 || connected.status === 403;
      if (denied || connected.error === "budget_disabled") {
        // Another principal, a signed-out session or a disabled feature must not keep earlier data in memory.
        setConnections([]); setAccounts([]); setJobs([]); setDetail(null);
      }
      setLoad(connected.error === "budget_disabled" ? "disabled" : denied ? "denied" : "error");
      return;
    }
    setConnections(connected.data.connections);
    setCapabilities(connected.data.capabilities);
    if (listed.ok) setAccounts(listed.data.accounts);
    if (queued.ok) setJobs(queued.data.jobs);
    setLoad(listed.ok ? "ready" : "error");
  }, [api]);

  useEffect(() => {
    if (active) void reload();
  }, [active, reload]);

  // Poll only while a job is queued/running and the view is visible; reload stored data once it settles.
  const polling = active && hasActiveJob(jobs);
  useEffect(() => {
    if (!polling) return undefined;
    const timer = setTimeout(() => {
      void api.jobs().then((result) => {
        if (!result.ok) return;
        setJobs(result.data.jobs);
        if (!hasActiveJob(result.data.jobs)) void reload();
      });
    }, pollMs);
    return () => { clearTimeout(timer); };
  }, [polling, jobs, api, pollMs, reload]);

  const run = useCallback(async <T,>(key: string, action: () => Promise<BudgetResult<T>>): Promise<BudgetResult<T>> => {
    setBusy((current) => new Set(current).add(key));
    setNotice(null);
    const result = await action();
    setBusy((current) => { const next = new Set(current); next.delete(key); return next; });
    if (!result.ok) {
      setNotice(failureText(text, result.error));
      if (result.error === "budget_version_conflict") await reload();
    }
    return result;
  }, [reload, text]);

  const refresh = useCallback(async (connection: BudgetConnection) => {
    // Reuse the key only to retry a request whose outcome is unknown (network failure).
    const key = retryKeys.current.get(connection.id) ?? idempotencyKey();
    retryKeys.current.set(connection.id, key);
    const result = await run(`refresh:${connection.id}`, () => api.refresh(connection, {}, key));
    if (result.ok || result.status !== 0) retryKeys.current.delete(connection.id);
    if (result.ok) {
      const job = result.data.job;
      setJobs((current) => [job, ...current.filter((item) => item.id !== job.id)]);
    }
  }, [api, run]);

  const saveConnection = useCallback(async (key: string, action: () => Promise<BudgetResult<{ connection: BudgetConnection }>>) => {
    const result = await run(key, action);
    if (result.ok) {
      const saved = result.data.connection;
      setConnections((current) => current.map((item) => (item.id === saved.id ? saved : item)));
      if (key.startsWith("pause:")) {
        const listed = await api.accounts();
        if (listed.ok) setAccounts(listed.data.accounts);
      }
    }
  }, [api, run]);

  const saveAccount = useCallback(async (key: string, action: () => Promise<BudgetResult<{ account: BudgetAccount }>>) => {
    const result = await run(key, action);
    if (result.ok) {
      const saved = result.data.account;
      setAccounts((current) => current.map((item) => (item.id === saved.id ? { ...item, ...saved } : item)));
    }
  }, [run]);

  if (!active) return null;
  const header = (
    <header className="budget-header">
      <div><h2>{text.title}</h2><p>{text.subtitle}</p></div>
      <button type="button" onClick={() => { void reload(); }}>{text.reload}</button>
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
          </article>
        );
      })}
      {opened === null ? null : (
        <AccountDetail account={opened} label={accountLabel(opened, accounts.filter((a) => a.connection === opened.connection).indexOf(opened), text.account)}
          api={api} text={text} locale={locale} onClose={() => { setDetail(null); }} />
      )}
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
  account: BudgetAccount; label: string; api: BudgetApi; text: Labels; locale: string; onClose: () => void;
}

interface Ledger { rows: BudgetTransaction[]; cursor: string | null; uncertain: number; failed: boolean; loading: boolean }

const EMPTY_LEDGER: Ledger = { rows: [], cursor: null, uncertain: 0, failed: false, loading: true };

function AccountDetail({ account, label, api, text, locale, onClose }: DetailProps) {
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

  useEffect(() => {
    const token = ++generation.current;
    setLedger(EMPTY_LEDGER);
    setBalances(null);
    void api.balances(account.id).then((result) => {
      if (token === generation.current) setBalances(result.ok ? result.data : null);
    });
    void api.transactions(account.id, { dateFrom: range.from, dateTo: range.to }).then((result) => {
      if (token !== generation.current) return;
      setLedger(result.ok
        ? { rows: result.data.transactions, cursor: result.data.next_cursor, uncertain: result.data.uncertain_total,
            failed: false, loading: false }
        : { ...EMPTY_LEDGER, failed: true, loading: false });
    });
  }, [api, account.id, range]);

  const more = useCallback(async () => {
    const token = generation.current;
    const cursor = ledger.cursor;
    if (cursor === null) return;
    setLedger((current) => ({ ...current, loading: true }));
    const result = await api.transactions(account.id, { cursor, dateFrom: range.from, dateTo: range.to });
    if (token !== generation.current) return;
    setLedger((current) => (result.ok
      ? { rows: [...current.rows, ...result.data.transactions], cursor: result.data.next_cursor,
          uncertain: result.data.uncertain_total, failed: false, loading: false }
      : { ...current, failed: true, loading: false }));
  }, [api, account.id, range, ledger.cursor]);

  const apply = (next: DateRange) => { setDraft(next); if (validRange(next)) setRange(next); };
  return (
    <aside className="budget-detail" aria-label={label} ref={panel}>
      <header><h3>{label}</h3><button type="button" onClick={onClose}>{text.close}</button></header>
      <h4>{text.balances}</h4>
      {balances === null || balances.balances.length === 0 ? <p className="budget-muted">{text.noBalances}</p> : (
        <ul className="budget-balances">
          {balances.balances.map((balance, index) => (
            <li key={`${balance.balance_type}-${String(index)}`}>
              <span>{balance.name || balance.balance_type}</span>
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

export function mountBudget(element: HTMLElement): { setActive: (active: boolean) => void; destroy: () => void } {
  const root = createRoot(element);
  return { setActive: (active) => { root.render(<Budget active={active} />); }, destroy: () => { root.unmount(); } };
}
