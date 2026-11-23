// Own-transfer review queue (2D). The server proposes pairs; only the owner accepts, rejects or undoes them, one
// explicit versioned decision at a time. Ambiguous pairs are shown side by side and nothing is picked for the owner.
import { useCallback, useEffect, useRef, useState } from "react";
import type { BudgetApi } from "../api";
import type { BudgetAccount, BudgetConnection } from "../model";
import { formatAmount } from "../model";
import { t, tPlural } from "../../i18n";
import { ANALYTICS_ERROR, RELOAD_ERRORS, TRANSFER_ACTION_KEYS, TRANSFER_QUEUES, TRANSFER_QUEUE_KEYS,
  TRANSFER_SOURCE_KEYS, accountName, labelOf, legsMatch, transferActions, transferDecision } from "./model";
import type { TransferAction, TransferLeg, TransferPair, TransferQueue } from "./model";

interface Page { rows: TransferPair[]; cursor: string | null; loading: boolean; failed: boolean; limited: boolean }
const LOADING: Page = { rows: [], cursor: null, loading: true, failed: false, limited: false };

export interface TransfersProps {
  api: Pick<BudgetApi, "transfers" | "decideTransfer">;
  period: string;
  accounts: readonly BudgetAccount[];
  connections?: readonly BudgetConnection[] | undefined;
  locale: string;
  revision: number;
  onAccessLoss: (result: { ok: boolean; status?: number; error?: string }) => boolean;
  /** A decision changes totals and entries; the owner of this panel re-reads the period. */
  onChanged: () => void;
}

const pairKey = (pair: TransferPair) =>
  `${pair.debit.account_id}/${pair.debit.entry_reference}>${pair.credit.account_id}/${pair.credit.entry_reference}`;

export function TransfersPanel({ api, period, accounts, connections = [], locale, revision, onAccessLoss, onChanged }: TransfersProps) {
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
    if (!result.ok && result.error === ANALYTICS_ERROR.staleCursor) { setReloads((current) => current + 1); return; }
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
      setNotice(result.error === ANALYTICS_ERROR.sourceChanged ? t("budget.transfers.changed")
        : result.error === ANALYTICS_ERROR.legReserved ? t("budget.transfers.reserved")
          : RELOAD_ERRORS.has(result.error) ? t("budget.transfers.conflict") : t("budget.transfers.saveError"));
      if (RELOAD_ERRORS.has(result.error)) setReloads((current) => current + 1);
      return;
    }
    onChanged();
  }, [api, onAccessLoss, onChanged]);

  const side = (item: TransferLeg, indicator: "DBIT" | "CRDT") => {
    const name = accountName(accounts, connections, item.account_id, t("budget.transfers.account"));
    return (
      <span className="budget-leg">
        <span>{item.selected ? name : `${name} (${t("budget.transfers.excludedAccount")})`}</span>
        {item.date === null ? null : <span className="budget-muted"> {item.date}</span>}
        {item.amount === null || item.currency === null ? null : (
          <strong className={indicator === "DBIT" ? "is-debit" : "is-credit"}> {formatAmount(item.amount, item.currency, locale, indicator)}</strong>
        )}
      </span>
    );
  };

  return (
    <section className="budget-transfers" aria-label={t("budget.transfers.title")}>
      <h4>{t("budget.transfers.title")}</h4>
      <div className="budget-views" role="group" aria-label={t("budget.transfers.title")}>
        {TRANSFER_QUEUES.map((item) => (
          <button type="button" key={item} aria-pressed={queue === item} onClick={() => { setQueue(item); setNotice(null); }}>
            {labelOf(TRANSFER_QUEUE_KEYS, item)}
          </button>
        ))}
      </div>
      {notice === null ? null : <p className="budget-notice" role="alert">{notice}</p>}
      {page.limited ? <p className="budget-notice" role="note">{t("budget.transfers.scanLimited")}</p> : null}
      {page.failed ? <p className="budget-notice" role="alert">{t("budget.transfers.error")}</p> : null}
      {page.loading && page.rows.length === 0 ? <p className="budget-muted" role="status">{t("budget.transfers.loading")}</p> : null}
      {!page.loading && !page.failed && page.rows.length === 0 ? <p className="budget-muted">{t("budget.transfers.empty")}</p> : null}
      {page.rows.length === 0 ? null : (
        <ul className="budget-entry-list">
          {page.rows.map((pair) => {
            const key = pairKey(pair);
            return (
              <li key={key} className="budget-entry budget-pair">
                <div className="budget-pair-legs">{side(pair.debit, "DBIT")}<span aria-hidden="true">→</span>{side(pair.credit, "CRDT")}</div>
                <div className="budget-entry-facts">
                  <span className="budget-chip">{labelOf(TRANSFER_SOURCE_KEYS, pair.source)}</span>
                  {pair.date_distance === null ? null : (
                    <span>{pair.date_distance === 0 ? t("budget.transfers.daysSame") : tPlural("budget.transfers.days", pair.date_distance)}</span>
                  )}
                  {pair.ambiguous ? <span className="budget-warn">{t("budget.transfers.ambiguous")}</span> : null}
                  {pair.stale ? <span className="budget-warn">{t("budget.transfers.stale")}</span> : null}
                  {pair.boundary ? <span>{t("budget.transfers.boundary")}</span> : null}
                  {!pair.boundary && !legsMatch(pair) ? <span className="budget-warn">{t("budget.transfers.mismatch")}</span> : null}
                  {transferActions(pair).map((action) => (
                    <button type="button" key={action} className={action === "accept" ? "budget-primary" : undefined}
                      disabled={busy !== null} onClick={() => { void decide(pair, action); }}>{labelOf(TRANSFER_ACTION_KEYS, action)}</button>
                  ))}
                </div>
              </li>
            );
          })}
        </ul>
      )}
      {page.cursor === null ? null
        : <button type="button" disabled={page.loading} onClick={() => { void more(); }}>{t("budget.transfers.more")}</button>}
    </section>
  );
}
