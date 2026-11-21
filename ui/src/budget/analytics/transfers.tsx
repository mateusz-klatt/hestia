// Own-transfer review queue (2D). The server proposes pairs; only the owner accepts, rejects or undoes them, one
// explicit versioned decision at a time. Ambiguous pairs are shown side by side and nothing is picked for the owner.
import { useCallback, useEffect, useRef, useState } from "react";
import type { BudgetApi } from "../api";
import type { BudgetAccount } from "../model";
import { accountLabel, formatAmount } from "../model";
import { TRANSFER_QUEUES, transferActions, transferDecision } from "./model";
import type { TransferAction, TransferLeg, TransferPair, TransferQueue } from "./model";

export const transferLabels = {
  en: {
    title: "Own transfers and card repayments",
    queues: { suggested: "To review", review: "To re-check", boundary: "Excluded account", accepted: "Confirmed", rejected: "Not transfers" },
    actions: { accept: "Confirm transfer", reject: "Not a transfer", undo: "Undo" },
    sources: { account_identity: "matched by account number", heuristic: "same amount, dates within 3 days", manual: "paired by you" },
    days: (count: number) => (count === 0 ? "same day" : `${String(count)} ${count === 1 ? "day" : "days"} apart`),
    ambiguous: "Fits more than one pair: confirm the right one and mark the others as not transfers.",
    stale: "The bank changed one side after your decision; check it again.",
    boundary: "One side is on an account excluded from the budget.",
    excludedAccount: "excluded account", account: "Account",
    scanLimited: "The search stopped at its limit; some possible pairs may be missing.",
    empty: "Nothing here for this month.", loading: "Loading transfers…", error: "Transfers could not be loaded.",
    more: "Load more", conflict: "This changed elsewhere; the latest state was loaded.",
    changed: "The bank data for this pair changed; the latest state was loaded.", saveError: "Could not save. Try again.",
  },
  pl: {
    title: "Przelewy własne i spłaty kart",
    queues: { suggested: "Do przejrzenia", review: "Do ponownego sprawdzenia", boundary: "Konto wykluczone", accepted: "Potwierdzone",
      rejected: "To nie przelewy" },
    actions: { accept: "Potwierdź przelew", reject: "To nie przelew", undo: "Cofnij" },
    sources: { account_identity: "zgodny numer konta", heuristic: "ta sama kwota, daty w ciągu 3 dni", manual: "sparowane przez Ciebie" },
    days: (count: number) => (count === 0 ? "ten sam dzień" : `${String(count)} ${count === 1 ? "dzień" : "dni"} różnicy`),
    ambiguous: "Pasuje do więcej niż jednej pary: potwierdź właściwą, a pozostałe oznacz jako „to nie przelew”.",
    stale: "Bank zmienił jedną stronę po Twojej decyzji; sprawdź ją ponownie.",
    boundary: "Jedna strona jest na koncie wykluczonym z budżetu.",
    excludedAccount: "konto wykluczone", account: "Konto",
    scanLimited: "Wyszukiwanie zatrzymało się na limicie; części możliwych par może brakować.",
    empty: "Brak pozycji w tym miesiącu.", loading: "Wczytywanie przelewów…", error: "Nie udało się wczytać przelewów.",
    more: "Wczytaj więcej", conflict: "Zmieniono to w innym miejscu; wczytano aktualny stan.",
    changed: "Dane z banku dla tej pary się zmieniły; wczytano aktualny stan.", saveError: "Nie udało się zapisać. Spróbuj ponownie.",
  },
};
export type TransferLabels = typeof transferLabels.en;

interface Page { rows: TransferPair[]; cursor: string | null; loading: boolean; failed: boolean; limited: boolean }
const LOADING: Page = { rows: [], cursor: null, loading: true, failed: false, limited: false };
const RELOAD_ERRORS = new Set(["budget_version_conflict", "budget_analytics_source_changed"]);

export interface TransfersProps {
  api: Pick<BudgetApi, "transfers" | "decideTransfer">;
  period: string;
  accounts: readonly BudgetAccount[];
  text: TransferLabels;
  locale: string;
  revision: number;
  onAccessLoss: (result: { ok: boolean; status?: number; error?: string }) => boolean;
  /** A decision changes totals and entries; the owner of this panel re-reads the period. */
  onChanged: () => void;
}

const pairKey = (pair: TransferPair) =>
  `${pair.debit.account_id}/${pair.debit.entry_reference}>${pair.credit.account_id}/${pair.credit.entry_reference}`;

export function TransfersPanel({ api, period, accounts, text, locale, revision, onAccessLoss, onChanged }: TransfersProps) {
  const [queue, setQueue] = useState<TransferQueue>("suggested");
  const [page, setPage] = useState<Page>(LOADING);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [reloads, setReloads] = useState(0);
  const generation = useRef(0);

  useEffect(() => {
    const token = ++generation.current;
    setPage(LOADING);
    setBusy(null);
    void api.transfers(period, queue).then((result) => {
      if (onAccessLoss(result) || token !== generation.current) return;
      setPage(result.ok
        ? { rows: result.data.transfers, cursor: result.data.next_cursor, loading: false, failed: false, limited: result.data.scan_limited }
        : { ...LOADING, loading: false, failed: true });
    });
  }, [api, period, queue, revision, reloads, onAccessLoss]);
  useEffect(() => () => { generation.current += 1; }, []);

  const more = useCallback(async () => {
    const token = generation.current;
    const cursor = page.cursor;
    if (cursor === null) return;
    setPage((current) => ({ ...current, loading: true }));
    const result = await api.transfers(period, queue, cursor);
    if (onAccessLoss(result) || token !== generation.current) return;
    setPage((current) => (result.ok
      ? { rows: [...current.rows, ...result.data.transfers], cursor: result.data.next_cursor, loading: false, failed: false,
          limited: current.limited || result.data.scan_limited }
      : { ...current, loading: false, failed: true }));
  }, [api, period, queue, page.cursor, onAccessLoss]);

  const decide = useCallback(async (pair: TransferPair, action: TransferAction) => {
    const request = transferDecision(pair, action);
    if (request === null) return;
    const token = generation.current;
    setBusy(pairKey(pair));
    setNotice(null);
    const result = await api.decideTransfer(request);
    if (onAccessLoss(result) || token !== generation.current) return;
    setBusy(null);
    if (!result.ok) {
      setNotice(result.error === "budget_analytics_source_changed" ? text.changed
        : result.error === "budget_version_conflict" ? text.conflict : text.saveError);
      if (RELOAD_ERRORS.has(result.error)) setReloads((current) => current + 1);
      return;
    }
    onChanged();
  }, [api, onAccessLoss, onChanged, text]);

  const side = (item: TransferLeg, indicator: "DBIT" | "CRDT") => {
    const account = accounts.find((value) => value.id === item.account_id);
    const name = account === undefined ? text.account
      : accountLabel(account, accounts.filter((value) => value.connection === account.connection).indexOf(account), text.account);
    return (
      <span className="budget-leg">
        <span>{item.selected ? name : `${name} (${text.excludedAccount})`}</span>
        {item.date === null ? null : <span className="budget-muted"> {item.date}</span>}
        {item.amount === null || item.currency === null ? null : (
          <strong className={indicator === "DBIT" ? "is-debit" : "is-credit"}> {formatAmount(item.amount, item.currency, locale, indicator)}</strong>
        )}
      </span>
    );
  };

  return (
    <section className="budget-transfers" aria-label={text.title}>
      <h4>{text.title}</h4>
      <div className="budget-views" role="group" aria-label={text.title}>
        {TRANSFER_QUEUES.map((item) => (
          <button type="button" key={item} aria-pressed={queue === item} onClick={() => { setQueue(item); setNotice(null); }}>
            {text.queues[item]}
          </button>
        ))}
      </div>
      {notice === null ? null : <p className="budget-notice" role="alert">{notice}</p>}
      {page.limited ? <p className="budget-notice" role="note">{text.scanLimited}</p> : null}
      {page.failed ? <p className="budget-notice" role="alert">{text.error}</p> : null}
      {page.loading && page.rows.length === 0 ? <p className="budget-muted" role="status">{text.loading}</p> : null}
      {!page.loading && !page.failed && page.rows.length === 0 ? <p className="budget-muted">{text.empty}</p> : null}
      {page.rows.length === 0 ? null : (
        <ul className="budget-entry-list">
          {page.rows.map((pair) => {
            const key = pairKey(pair);
            return (
              <li key={key} className="budget-entry budget-pair">
                <div className="budget-pair-legs">{side(pair.debit, "DBIT")}<span aria-hidden="true">→</span>{side(pair.credit, "CRDT")}</div>
                <div className="budget-entry-facts">
                  <span className="budget-chip">{text.sources[pair.source]}</span>
                  {pair.date_distance === null ? null : <span>{text.days(pair.date_distance)}</span>}
                  {pair.ambiguous ? <span className="budget-warn">{text.ambiguous}</span> : null}
                  {pair.stale ? <span className="budget-warn">{text.stale}</span> : null}
                  {pair.boundary ? <span>{text.boundary}</span> : null}
                  {transferActions(pair).map((action) => (
                    <button type="button" key={action} className={action === "accept" ? "budget-primary" : undefined}
                      disabled={busy !== null} onClick={() => { void decide(pair, action); }}>{text.actions[action]}</button>
                  ))}
                </div>
              </li>
            );
          })}
        </ul>
      )}
      {page.cursor === null ? null
        : <button type="button" disabled={page.loading} onClick={() => { void more(); }}>{text.more}</button>}
    </section>
  );
}
