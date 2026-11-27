import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { t } from "../i18n";
import { formatScaledBigInt, parseScaledInt } from "./analytics/model";
import type { BudgetResult } from "./api";
import { formatAmount } from "./model";
import type { BudgetAccount, BudgetConnection } from "./model";
import { receiptAccountLabel } from "./receipts_model";
import type {
  ReceiptTransactionSearchItem, ReceiptTransactionSearchRequest, ReceiptTransactionSearchResult,
} from "./receipts_model";

type Search = (body: ReceiptTransactionSearchRequest) => Promise<BudgetResult<ReceiptTransactionSearchResult>>;

interface Props {
  receiptId: string;
  receiptDate: string;
  receiptTotal: string;
  accounts: readonly BudgetAccount[];
  connections: readonly BudgetConnection[];
  locale: string;
  search: Search;
  onAccessLoss: (result: BudgetResult<ReceiptTransactionSearchResult>) => boolean;
  onClose: () => void;
  onPick: (item: ReceiptTransactionSearchItem) => void;
}

function offsetDate(value: string, offset: number): string {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return "";
  const date = new Date(`${value}T00:00:00Z`);
  if (!Number.isFinite(date.getTime())) return "";
  date.setUTCDate(date.getUTCDate() + offset);
  if (date.getUTCFullYear() < 1) return "0001-01-01";
  if (date.getUTCFullYear() > 9999) return "9999-12-31";
  return date.toISOString().slice(0, 10);
}

function amountBound(total: string, multiplier: bigint): string {
  const value = parseScaledInt(total.trim().replace(",", "."));
  return value === null || value.negative ? "" : formatScaledBigInt(value.coeff * multiplier, value.scale + 1);
}

function normalizeAmount(value: string): string | null {
  const text = value.trim().replace(",", ".");
  return text.length <= 80 && /^\d+(?:\.\d+)?$/.test(text) ? text : null;
}

export function transactionPickerDescription(item: ReceiptTransactionSearchItem, locale: string): string {
  return [
    item.counterparty, item.title, item.account_label,
    item.date ?? t("budget.receipts.noDate"),
    formatAmount(item.amount, item.currency, locale, "DBIT"),
  ].filter(Boolean).join(" · ");
}

interface Page {
  request: ReceiptTransactionSearchRequest;
  items: ReceiptTransactionSearchItem[];
  cursor: string | null;
  loading: boolean;
  error: boolean;
}

export function ReceiptTransactionPicker({
  receiptId, receiptDate, receiptTotal, accounts, connections, locale, search, onAccessLoss, onClose, onPick,
}: Props) {
  const id = useId();
  const dialog = useRef<HTMLDivElement>(null);
  const searchInput = useRef<HTMLInputElement>(null);
  const generation = useRef(0);
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [dateFrom, setDateFrom] = useState(() => offsetDate(receiptDate, -14));
  const [dateTo, setDateTo] = useState(() => offsetDate(receiptDate, 30));
  const [amountFrom, setAmountFrom] = useState(() => amountBound(receiptTotal, 8n));
  const [amountTo, setAmountTo] = useState(() => amountBound(receiptTotal, 12n));
  const [accountId, setAccountId] = useState("");
  const [page, setPage] = useState<Page | null>(null);
  const [retry, setRetry] = useState(0);
  const waitingForQuery = query !== debouncedQuery;
  const badDates = dateFrom !== "" && dateTo !== "" && dateFrom > dateTo;
  const min = normalizeAmount(amountFrom);
  const max = normalizeAmount(amountTo);
  const badAmounts = (amountFrom.trim() !== "" && min === null) || (amountTo.trim() !== "" && max === null);
  const request = useMemo<ReceiptTransactionSearchRequest>(() => ({
    receipt_id: receiptId,
    amount_min: min,
    amount_max: max,
    date_from: dateFrom || null,
    date_to: dateTo || null,
    account_id: accountId || null,
    query: debouncedQuery.trim(),
  }), [receiptId, min, max, dateFrom, dateTo, accountId, debouncedQuery]);
  const visiblePage = page?.request === request && !waitingForQuery ? page : null;
  const validFilters = !badDates && !badAmounts;

  useLayoutEffect(() => { searchInput.current?.focus(); }, []);

  useEffect(() => {
    if (!waitingForQuery) return;
    const timer = window.setTimeout(() => { setDebouncedQuery(query); }, 300);
    return () => { window.clearTimeout(timer); };
  }, [query, waitingForQuery]);

  useEffect(() => {
    const started = ++generation.current;
    if (!waitingForQuery && validFilters) {
      const initial = { request, items: [], cursor: null, loading: true, error: false };
      setPage(initial);
      void search(request).then((result) => {
        if (generation.current !== started || onAccessLoss(result)) return;
        setPage(result.ok
          ? { request, items: result.data.items, cursor: result.data.next_cursor, loading: false, error: false }
          : { ...initial, loading: false, error: true });
      });
    }
    return () => { generation.current += 1; };
  }, [request, search, onAccessLoss, retry, validFilters, waitingForQuery]);

  const loadMore = (current: Page) => {
    if (current.cursor === null || current.loading) return;
    const started = generation.current;
    setPage({ ...current, loading: true, error: false });
    void search({ ...request, cursor: current.cursor }).then((result) => {
      if (generation.current !== started || onAccessLoss(result)) return;
      setPage(result.ok
        ? { request, items: [...current.items, ...result.data.items], cursor: result.data.next_cursor,
            loading: false, error: false }
        : { ...current, loading: false, error: true });
    });
  };

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        onClose();
      } else if (event.key === "Tab" && dialog.current !== null) {
        const controls = [...dialog.current.querySelectorAll<HTMLElement>("button:not(:disabled), input, select")];
        const first = controls[0];
        const last = controls.at(-1);
        const active = document.activeElement;
        if (!dialog.current.contains(active) || active === (event.shiftKey ? first : last)) {
          event.preventDefault();
          (event.shiftKey ? last : first)?.focus();
        }
      }
    };
    document.addEventListener("keydown", onKeyDown, true);
    return () => { document.removeEventListener("keydown", onKeyDown, true); };
  }, [onClose]);

  return createPortal(
    <div className="modal-overlay budget-receipt-picker-overlay">
      <div ref={dialog} className="modal-card budget-receipt-picker" role="dialog" aria-modal="true"
        aria-labelledby={`${id}-title`}>
        <header className="budget-receipt-drawer-head">
          <h3 id={`${id}-title`}>{t("budget.receipts.transactionPicker")}</h3>
          <button type="button" onClick={onClose}>{t("budget.main.close")}</button>
        </header>
        <div className="budget-receipt-form-grid">
          <label>
            {t("budget.transactions.search")}
            <input ref={searchInput} type="search" value={query} maxLength={128}
              onChange={(event) => { setQuery(event.currentTarget.value); }} />
          </label>
          <label>
            {t("budget.transactions.account")}
            <select value={accountId} onChange={(event) => { setAccountId(event.currentTarget.value); }}>
              <option value="">{t("budget.quarantine.allAccounts")}</option>
              {accounts.filter((account) => account.selected).map((account) => (
                <option key={account.id} value={account.id}>
                  {receiptAccountLabel(accounts, connections, account.id, t("budget.main.account"))}
                </option>
              ))}
            </select>
          </label>
          <fieldset className="budget-receipt-picker-range">
            <legend id={`${id}-amount`}>{t("budget.transactions.amount")}</legend>
            <label>
              <span id={`${id}-amount-from`}>{t("budget.main.from")}</span>
              <input type="text" inputMode="decimal" maxLength={80} value={amountFrom}
                aria-labelledby={`${id}-amount ${id}-amount-from`}
                onChange={(event) => { setAmountFrom(event.currentTarget.value); }} />
            </label>
            <label>
              <span id={`${id}-amount-to`}>{t("budget.main.to")}</span>
              <input type="text" inputMode="decimal" maxLength={80} value={amountTo}
                aria-labelledby={`${id}-amount ${id}-amount-to`}
                onChange={(event) => { setAmountTo(event.currentTarget.value); }} />
            </label>
          </fieldset>
          <fieldset className="budget-receipt-picker-range">
            <legend id={`${id}-date`}>{t("budget.transactions.date")}</legend>
            <label>
              <span id={`${id}-date-from`}>{t("budget.main.from")}</span>
              <input type="date" value={dateFrom} aria-labelledby={`${id}-date ${id}-date-from`}
                onChange={(event) => { setDateFrom(event.currentTarget.value); }} />
            </label>
            <label>
              <span id={`${id}-date-to`}>{t("budget.main.to")}</span>
              <input type="date" value={dateTo} aria-labelledby={`${id}-date ${id}-date-to`}
                onChange={(event) => { setDateTo(event.currentTarget.value); }} />
            </label>
          </fieldset>
        </div>
        {badDates ? <p role="alert">{t("budget.main.badRange")}</p> : null}
        {badAmounts ? <p role="alert">{t("budget.transactions.error")}</p> : null}
        {validFilters && (visiblePage === null || visiblePage.loading) ? (
          <p role="status">{t("budget.transactions.loading")}</p>
        ) : null}
        {validFilters && visiblePage?.error === true && (
          <div className="budget-actions" role="alert">
            <span>{t("budget.transactions.error")}</span>
            <button type="button" onClick={() => {
              if (visiblePage.cursor === null) setRetry((current) => current + 1);
              else loadMore(visiblePage);
            }}>{t("budget.transactions.retry")}</button>
          </div>
        )}
        {validFilters && visiblePage !== null && !visiblePage.loading && !visiblePage.error
          && visiblePage.items.length === 0 ? <p role="status">{t("budget.transactions.empty")}</p> : null}
        {validFilters && visiblePage !== null && visiblePage.items.length > 0 ? (
          <ul className="budget-receipt-picker-results">
            {visiblePage.items.map((item) => (
              <li key={item.id}>
                <button type="button" onClick={() => { onPick(item); }}>
                  {transactionPickerDescription(item, locale)}
                </button>
              </li>
            ))}
          </ul>
        ) : null}
        {validFilters && visiblePage !== null && visiblePage.cursor !== null && (
          <button type="button" aria-disabled={visiblePage.loading} onClick={() => { loadMore(visiblePage); }}>
            {t("budget.receipts.loadMore")}
          </button>
        )}
      </div>
    </div>,
    document.body,
  );
}
