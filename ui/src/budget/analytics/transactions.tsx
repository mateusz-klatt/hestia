// A period's entries with the owner's category/kind corrections (2D). Rows come from the server one bounded page
// at a time; a correction is an explicit, versioned decision, and a conflict reloads instead of overwriting.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { t, tPlural } from "../../i18n";
import type { BudgetApi } from "../api";
import type { BudgetAccount, BudgetConnection } from "../model";
import { formatAmount, transactionDate, transactionText } from "../model";
import { CategoryBadge } from "./category_badge";
import { buildCategoryColors } from "./category_colors";
import { ExportMenu } from "./export_menu";
import { exportBasename } from "./export_rows";
import { ANALYTICS_ERROR, EFFECTIVE_KINDS, NO_FILTER, RELOAD_ERRORS, TRANSACTION_KIND_KEYS,
  TRANSACTION_TRANSFER_STATUS_KEYS, UNCATEGORISED, annotationRequest, canPair, canonicalAmount,
  counterparty, counterpartyRuleCandidate, accountName as nameOf, currentChoice, daysBetween, kindChoice, kindChoicesFor, labelOf, manualPairRequest,
  pairCandidates, pairDate, pairPeriods, transactionRowKey } from "./model";
import type { AnalyticsCategory, AnalyticsTransaction, AnalyticsTransactionsByKeysRequest, CategoryChoice, KindChoice,
  RuleCandidate, TransactionFilter } from "./model";

/** Background pagination stops once this many rows are in memory; remaining pages load on demand. */
export const TRANSACTION_ROW_CAP = 5000;
const MAX_STALE_RELOADS = 2;

export type TransactionSortKey = "date" | "category" | "amount";
export type TransactionSortDirection = "asc" | "desc";
export interface TransactionSort { key: TransactionSortKey; direction: TransactionSortDirection }

const SORT_KEYS: readonly TransactionSortKey[] = ["date", "category", "amount"];

function sortLabel(key: TransactionSortKey): string {
  if (key === "date") return t("budget.transactions.date");
  if (key === "category") return t("budget.transactions.category");
  return t("budget.transactions.amount");
}

export function nextSort(current: TransactionSort | null, key: TransactionSortKey): TransactionSort {
  if (current?.key !== key) return { key, direction: "asc" };
  return { key, direction: current.direction === "asc" ? "desc" : "asc" };
}

function absoluteAmount(amount: string): string | null {
  return canonicalAmount(amount.trim().replace(/^-/, ""));
}

/** Compare canonical non-negative decimals without converting them to binary floating point. */
function compareCanonical(a: string, b: string): number {
  const [aw = "0", af = ""] = a.split(".");
  const [bw = "0", bf = ""] = b.split(".");
  if (aw.length !== bw.length) return aw.length < bw.length ? -1 : 1;
  if (aw !== bw) return aw < bw ? -1 : 1;
  const length = Math.max(af.length, bf.length);
  const as = af.padEnd(length, "0");
  const bs = bf.padEnd(length, "0");
  if (as === bs) return 0;
  return as < bs ? -1 : 1;
}

function directed(diff: number, direction: TransactionSortDirection): number {
  if (diff === 0) return 0;
  return direction === "asc" ? diff : -diff;
}

/** Missing dates and unparseable amounts stay last in both directions. Other ties keep the previous order. */
export function compareTransactions(a: AnalyticsTransaction, b: AnalyticsTransaction, sort: TransactionSort,
  categoryName: (id: string | null) => string, locale: string): number {
  if (sort.key === "date") {
    const left = transactionDate(a);
    const right = transactionDate(b);
    if (left === null && right === null) return 0;
    if (left === null) return 1;
    if (right === null) return -1;
    if (left === right) return 0;
    return directed(left < right ? -1 : 1, sort.direction);
  }
  if (sort.key === "category") {
    const diff = categoryName(a.effective.category_id).localeCompare(categoryName(b.effective.category_id), locale,
      { sensitivity: "base", numeric: true });
    return directed(diff, sort.direction);
  }
  const left = absoluteAmount(a.amount);
  const right = absoluteAmount(b.amount);
  if (left === null && right === null) return 0;
  if (left === null) return 1;
  if (right === null) return -1;
  return directed(compareCanonical(left, right), sort.direction);
}

export function sortTransactions(rows: readonly AnalyticsTransaction[], sort: TransactionSort,
  categoryName: (id: string | null) => string, locale: string): AnalyticsTransaction[] {
  return rows.map((row, index) => ({ row, index }))
    .sort((a, b) => compareTransactions(a.row, b.row, sort, categoryName, locale) || a.index - b.index)
    .map((item) => item.row);
}

interface Page { rows: AnalyticsTransaction[]; cursor: string | null; total: number; loading: boolean; failed: boolean; staleError: boolean }
const LOADING: Page = { rows: [], cursor: null, total: 0, loading: true, failed: false, staleError: false };
interface PairPage { rows: AnalyticsTransaction[]; loading: boolean; failed: boolean; limited: boolean }
const PAIR_LOADING: PairPage = { rows: [], loading: true, failed: false, limited: false };

export interface TransactionsProps {
  api: Pick<BudgetApi, "analyticsTransactions" | "analyticsTransactionsByKeys" | "categories" | "annotate" | "decideTransfer">;
  period: string;
  currencies: readonly string[];
  accounts: readonly BudgetAccount[];
  connections?: readonly BudgetConnection[] | undefined;
  locale: string;
  revision: number;
  onAccessLoss: (result: { ok: boolean; status?: number; error?: string }) => boolean;
  /** A saved decision changes totals; the owner of this panel re-reads the period. */
  onChanged: () => void;
  /** Stops the background page walk. Defaults to {@link TRANSACTION_ROW_CAP}. */
  rowCap?: number | undefined;
  /** Initial currency filter when opened from another view (e.g. Monthly budget). */
  initialCurrency?: string | undefined;
  /** Server-computed `coverage.pending_row_keys` to fetch and filter entries when opened from the provisional banner. */
  initialProvisionalKeys?: readonly string[] | undefined;
  /** Server-reported total pending count (`coverage.pending_transfer_rows`) for truncation notes. */
  initialProvisionalTotal?: number | undefined;
  /** Whether `initialProvisionalKeys` was capped below `initialProvisionalTotal`. */
  initialProvisionalTruncated?: boolean | undefined;
  /** Scroll this section into view when mounted (e.g. opened from the provisional banner link). */
  focusOnMount?: boolean | undefined;
  onCreateRule?: ((candidate: RuleCandidate) => void) | undefined;
}

const rowKey = transactionRowKey;

function mergeRows(existing: readonly AnalyticsTransaction[], incoming: readonly AnalyticsTransaction[]): AnalyticsTransaction[] {
  const seen = new Set(existing.map(rowKey));
  const next = [...existing];
  for (const row of incoming) {
    const key = rowKey(row);
    if (seen.has(key)) continue;
    seen.add(key);
    next.push(row);
  }
  return next;
}

function orderRows(
  rows: readonly AnalyticsTransaction[],
  sort: TransactionSort | null,
  frozenOrder: readonly string[] | null,
  categoryName: (id: string | null) => string,
  locale: string,
): AnalyticsTransaction[] {
  if (frozenOrder !== null) {
    const byKey = new Map(rows.map((row) => [rowKey(row), row]));
    const ordered: AnalyticsTransaction[] = [];
    const used = new Set<string>();
    for (const key of frozenOrder) {
      const row = byKey.get(key);
      if (row === undefined || used.has(key)) continue;
      used.add(key);
      ordered.push(row);
    }
    for (const row of rows) {
      const key = rowKey(row);
      if (!used.has(key)) {
        used.add(key);
        ordered.push(row);
      }
    }
    return ordered;
  }
  return sort === null ? [...rows] : sortTransactions(rows, sort, categoryName, locale);
}

export function TransactionsPanel({ api, period, currencies, accounts, connections = [], locale, revision, onAccessLoss,
  onChanged, rowCap = TRANSACTION_ROW_CAP, initialCurrency, initialProvisionalKeys, initialProvisionalTotal,
  initialProvisionalTruncated, focusOnMount = false, onCreateRule }: TransactionsProps) {
  const startingFilter: TransactionFilter = initialCurrency === undefined ? NO_FILTER : { ...NO_FILTER, currency: initialCurrency };
  const [filter, setFilter] = useState<TransactionFilter>(startingFilter);
  const [draft, setDraft] = useState<TransactionFilter>(startingFilter);
  const [provisionalFilter, setProvisionalFilter] = useState<{
    period: string;
    keys: readonly string[] | undefined;
    checked: boolean;
  }>(() => ({ period, keys: initialProvisionalKeys, checked: initialProvisionalKeys !== undefined }));
  const provisionalChecked = provisionalFilter.period === period && provisionalFilter.keys === initialProvisionalKeys
    ? provisionalFilter.checked
    : true;
  const activeProvisionalKeys = provisionalChecked && initialProvisionalKeys !== undefined ? initialProvisionalKeys : undefined;
  const [page, setPage] = useState<Page>(LOADING);
  const [categories, setCategories] = useState<AnalyticsCategory[]>([]);
  const [editing, setEditing] = useState<string | null>(null);
  const [frozenOrder, setFrozenOrder] = useState<readonly string[] | null>(null);
  const [pairing, setPairing] = useState<string | null>(null);
  const [pairPage, setPairPage] = useState<PairPage>(PAIR_LOADING);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [reloads, setReloads] = useState(0);
  const [sort, setSort] = useState<TransactionSort | null>(null);
  const sectionRef = useRef<HTMLElement>(null);
  const didScrollOnLoad = useRef(false);
  // Every period, filter or reload change takes a new generation; late pages and saves of an older one are dropped.
  const generation = useRef(0);
  const walkToken = useRef(0);
  const staleReloads = useRef(0);
  const picker = useRef(0);

  const walkPages = useCallback(async (
    genToken: number,
    runToken: number,
    startCursor: string | undefined,
    initialRows: readonly AnalyticsTransaction[],
    stopAfterOne: boolean,
    rowKeys: readonly string[] | undefined,
  ) => {
    let cursor = startCursor;
    let rows = [...initialRows];
    let first = rows.length === 0;
    let fetchedAny = false;
    while (!fetchedAny || (!stopAfterOne && rows.length < rowCap)) {
      const result = rowKeys !== undefined
        ? await api.analyticsTransactionsByKeys({
          row_keys: [...rowKeys],
          ...(cursor === undefined ? {} : { cursor }),
          ...(filter.currency === "" ? {} : { currency: filter.currency }),
          ...(filter.category === "" ? {} : { category_id: filter.category }),
          ...(filter.kind === "" ? {} : { kind: filter.kind as NonNullable<AnalyticsTransactionsByKeysRequest["kind"]> }),
          ...(filter.query === "" ? {} : { query: filter.query }),
        })
        : cursor === undefined
          ? await api.analyticsTransactions(period, filter)
          : await api.analyticsTransactions(period, filter, cursor);
      const accessLost = onAccessLoss(result);
      if (genToken !== generation.current || runToken !== walkToken.current) return;
      if (accessLost) {
        if (!first) setPage((current) => ({ ...current, loading: false }));
        return;
      }
      if (!result.ok) {
        if (cursor !== undefined && result.error === ANALYTICS_ERROR.staleCursor && staleReloads.current < MAX_STALE_RELOADS) {
          staleReloads.current += 1;
          setReloads((current) => current + 1);
          return;
        }
        const staleError = cursor !== undefined && result.error === ANALYTICS_ERROR.staleCursor;
        setPage((current) => (first
          ? { ...LOADING, loading: false, failed: true, staleError }
          : { ...current, cursor: cursor ?? null, loading: false, failed: true, staleError }));
        return;
      }
      fetchedAny = true;
      const merged = mergeRows(rows, result.data.transactions);
      const added = merged.length - rows.length;
      rows = merged;
      const total = result.data.filtered_total;
      const next = result.data.next_cursor;
      const exhausted = next === null || added === 0 || next === cursor;
      const nextCursor = exhausted ? null : next;
      const stopHere = exhausted || stopAfterOne || rows.length >= rowCap;
      setPage({ rows, cursor: nextCursor, total, loading: !stopHere, failed: false, staleError: false });
      if (stopHere) return;
      cursor = next;
      first = false;
    }
  }, [api, period, filter, onAccessLoss, rowCap]);

  useEffect(() => {
    staleReloads.current = 0;
  }, [api, period, filter, revision, activeProvisionalKeys]);

  useEffect(() => {
    const token = ++generation.current;
    const runToken = ++walkToken.current;
    picker.current += 1;
    setPage(LOADING);
    setEditing(null);
    setFrozenOrder(null);
    setPairing(null);
    setBusy(null);
    void api.categories().then((result) => {
      if (onAccessLoss(result) || token !== generation.current) return;
      if (result.ok) setCategories(result.data.categories);
    });
    if (activeProvisionalKeys !== undefined && activeProvisionalKeys.length === 0) {
      setPage({ rows: [], cursor: null, total: 0, loading: false, failed: false, staleError: false });
      return;
    }
    // The first page paints immediately. Later pages follow the cursor until the cap, the period changes,
    // or the access barrier rejects an answer. A stale cursor restarts from the first page (up to the retry cap).
    void walkPages(token, runToken, undefined, [], false, activeProvisionalKeys);
  }, [api, period, filter, revision, reloads, onAccessLoss, walkPages, activeProvisionalKeys]);
  useEffect(() => () => { generation.current += 1; walkToken.current += 1; picker.current += 1; }, []);

  const more = useCallback(() => {
    if (page.loading || page.cursor === null) return;
    if (activeProvisionalKeys !== undefined && activeProvisionalKeys.length === 0) return;
    const runToken = ++walkToken.current;
    const cursor = page.cursor;
    const rows = page.rows;
    setPage((current) => ({ ...current, loading: true, failed: false, staleError: false }));
    void walkPages(generation.current, runToken, cursor, rows, true, activeProvisionalKeys);
  }, [page.cursor, page.loading, page.rows, walkPages, activeProvisionalKeys]);

  const retry = useCallback(() => {
    if (page.loading) return;
    if (page.staleError) {
      staleReloads.current = 0;
      setReloads((current) => current + 1);
      return;
    }
    if (activeProvisionalKeys !== undefined && activeProvisionalKeys.length === 0) return;
    const runToken = ++walkToken.current;
    const cursor = page.cursor ?? undefined;
    const rows = page.rows;
    const stopAfterOne = rows.length >= rowCap;
    setPage((current) => ({ ...current, loading: true, failed: false, staleError: false }));
    void walkPages(generation.current, runToken, cursor, rows, stopAfterOne, activeProvisionalKeys);
  }, [page.cursor, page.loading, page.rows, page.staleError, rowCap, walkPages, activeProvisionalKeys]);

  const loadPairs = useCallback(async (debit: AnalyticsTransaction, pickerToken: number, generationToken: number) => {
    const date = pairDate(debit);
    if (date === null) return;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      if (generationToken !== generation.current || pickerToken !== picker.current) return;
      setPairPage(PAIR_LOADING);
      const rows: AnalyticsTransaction[] = [];
      let limited = false;
      let restart = false;
      for (const month of pairPeriods(date)) {
        let cursor: string | undefined;
        for (let pageNumber = 0; pageNumber < 10; pageNumber += 1) {
          const result = await api.analyticsTransactions(month,
            { currency: debit.currency, category: "", kind: "", query: "" }, cursor, 200);
          if (onAccessLoss(result) || generationToken !== generation.current || pickerToken !== picker.current) return;
          if (!result.ok) {
            if (result.error === ANALYTICS_ERROR.staleCursor && attempt === 0) { restart = true; break; }
            setPairPage({ rows: [], loading: false, failed: true, limited: false });
            return;
          }
          rows.push(...result.data.transactions);
          cursor = result.data.next_cursor ?? undefined;
          if (cursor === undefined) break;
          if (pageNumber === 9) limited = true;
        }
        if (restart) break;
      }
      if (restart) continue;
      setPairPage({ rows, loading: false, failed: false, limited });
      return;
    }
  }, [api, onAccessLoss]);

  const provisionalKeySet = useMemo(
    () => (activeProvisionalKeys === undefined ? null : new Set(activeProvisionalKeys)),
    [activeProvisionalKeys],
  );
  const provisionalOnly = provisionalKeySet !== null;
  const currencyOptions = useMemo(() => {
    const extras = [draft.currency, filter.currency].filter((code) => code !== "" && !currencies.includes(code));
    return extras.length === 0 ? currencies : [...currencies, ...extras].sort((a, b) => a.localeCompare(b));
  }, [currencies, draft.currency, filter.currency]);
  const categoryNames = useMemo(() => new Map(categories.map((item) => [item.id, item.name])), [categories]);
  const categoryColors = useMemo(() => buildCategoryColors(categories), [categories]);
  const categoryName = useCallback(
    (id: string | null) => (id === null ? t("budget.transactions.uncategorised") : (categoryNames.get(id) ?? t("budget.transactions.uncategorised"))),
    [categoryNames],
  );
  const accountName = (id: string) => nameOf(accounts, connections, id, t("budget.transactions.account"));
  const kindText = (kind: string) => labelOf(TRANSACTION_KIND_KEYS, kind);
  const matchingRows = useMemo(() => {
    if (provisionalKeySet === null) return page.rows;
    return page.rows.filter((row) => provisionalKeySet.has(rowKey(row)));
  }, [page.rows, provisionalKeySet]);
  const shownRows = useMemo(
    () => orderRows(matchingRows, sort, frozenOrder, categoryName, locale),
    [matchingRows, sort, frozenOrder, categoryName, locale],
  );
  const shownTotal = provisionalOnly ? matchingRows.length : page.total;

  const closePairs = useCallback(() => {
    picker.current += 1;
    setPairing(null);
    setFrozenOrder(null);
  }, []);

  const openPairs = useCallback((row: AnalyticsTransaction) => {
    if (!canPair(row)) return;
    const pickerToken = ++picker.current;
    setPairing(rowKey(row));
    setPairPage(PAIR_LOADING);
    setEditing(null);
    setFrozenOrder(shownRows.map(rowKey));
    setNotice(null);
    void loadPairs(row, pickerToken, generation.current);
  }, [loadPairs, shownRows]);

  const save = useCallback(async (row: AnalyticsTransaction, category: CategoryChoice, kind: KindChoice, label: string) => {
    const token = generation.current;
    setBusy(rowKey(row));
    setNotice(null);
    const result = await api.annotate(annotationRequest(row, category, kind, label));
    // The access barrier runs first, so a lost session clears even when the list moved on meanwhile.
    const accessLost = onAccessLoss(result);
    if (!accessLost && result.ok) onChanged();
    if (token !== generation.current) return;
    setBusy(null);
    if (accessLost) return;
    if (!result.ok) {
      setNotice(result.error === ANALYTICS_ERROR.sourceChanged ? t("budget.transactions.changed")
        : RELOAD_ERRORS.has(result.error) ? t("budget.transactions.conflict") : t("budget.transactions.saveError"));
      if (RELOAD_ERRORS.has(result.error)) {
        staleReloads.current = 0;
        setReloads((current) => current + 1);
      }
      return;
    }
    setEditing(null);
    setFrozenOrder(null);
  }, [api, onAccessLoss, onChanged]);

  const confirmPair = useCallback(async (debit: AnalyticsTransaction, credit: AnalyticsTransaction) => {
    const generationToken = generation.current;
    const pickerToken = picker.current;
    setBusy(`${rowKey(debit)}>${rowKey(credit)}`);
    setNotice(null);
    const result = await api.decideTransfer(manualPairRequest(debit, credit));
    const accessLost = onAccessLoss(result);
    if (!accessLost && result.ok) onChanged();
    if (generationToken !== generation.current || pickerToken !== picker.current) return;
    setBusy(null);
    if (accessLost) return;
    if (!result.ok) {
      const reserved = result.error === ANALYTICS_ERROR.legReserved;
      const existing = result.error === ANALYTICS_ERROR.conflict;
      setNotice(result.error === ANALYTICS_ERROR.sourceChanged ? t("budget.transactions.changed")
        : reserved ? t("budget.transactions.pairReserved") : existing ? t("budget.transactions.pairExisting")
          : result.error === ANALYTICS_ERROR.invalidRequest ? t("budget.transactions.pairInvalid")
            : RELOAD_ERRORS.has(result.error) ? t("budget.transactions.conflict") : t("budget.transactions.saveError"));
      if (RELOAD_ERRORS.has(result.error)) {
        staleReloads.current = 0;
        setReloads((current) => current + 1);
      }
      return;
    }
    picker.current += 1;
    setPairing(null);
    setFrozenOrder(null);
  }, [api, onAccessLoss, onChanged]);

  useEffect(() => {
    if (!focusOnMount) return;
    const target: { scrollIntoView?: (options?: ScrollIntoViewOptions) => void } | null = sectionRef.current;
    target?.scrollIntoView?.({ behavior: "smooth", block: "start" });
  }, [focusOnMount]);

  useEffect(() => {
    if (!focusOnMount || didScrollOnLoad.current || page.loading) return;
    didScrollOnLoad.current = true;
    const target: { scrollIntoView?: (options?: ScrollIntoViewOptions) => void } | null = sectionRef.current;
    target?.scrollIntoView?.({ behavior: "smooth", block: "start" });
  }, [focusOnMount, page.loading]);

  const startEditing = (key: string) => {
    closePairs();
    setFrozenOrder(shownRows.map(rowKey));
    setEditing(key);
    setNotice(null);
  };
  const stopEditing = () => {
    setEditing(null);
    setFrozenOrder(null);
  };
  const changeSort = (key: TransactionSortKey) => {
    const next = nextSort(sort, key);
    setSort(next);
    if (editing !== null || pairing !== null) {
      setFrozenOrder(sortTransactions(matchingRows, next, categoryName, locale).map(rowKey));
    } else {
      setFrozenOrder(null);
    }
  };
  const truncatedNotice = !page.loading && !page.failed && page.cursor !== null ? (
    <div className="budget-notice budget-entries-action" role="note">
      <span>{t("budget.transactions.truncated", { shown: page.rows.length, total: page.total })}</span>
      <button type="button" onClick={more}>{t("budget.transactions.more")}</button>
    </div>
  ) : null;

  return (
    <section className="budget-entries" aria-label={t("budget.transactions.title")} ref={sectionRef}>
      <header className="budget-entries-head">
        <h4>{t("budget.transactions.title")}</h4>
        <ExportMenu filename={exportBasename(period)} periods={[period]} filter={filter}
          rowKeys={activeProvisionalKeys} accounts={accounts} connections={connections} api={api} onAccessLoss={onAccessLoss} />
      </header>
      <form className="budget-range" onSubmit={(event) => { event.preventDefault(); setFilter(draft); }}>
        <label>{t("budget.transactions.currency")}
          <select name="currency" value={draft.currency} onChange={(event) => { setDraft({ ...draft, currency: event.currentTarget.value }); }}>
            <option value="">{t("budget.transactions.all")}</option>
            {currencyOptions.map((currency) => <option key={currency} value={currency}>{currency}</option>)}
          </select>
        </label>
        <label>{t("budget.transactions.category")}
          <select name="category" value={draft.category} onChange={(event) => { setDraft({ ...draft, category: event.currentTarget.value }); }}>
            <option value="">{t("budget.transactions.all")}</option>
            <option value={UNCATEGORISED}>{t("budget.transactions.uncategorised")}</option>
            {categories.filter((item) => !item.archived).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select>
        </label>
        <label>{t("budget.transactions.kind")}
          <select name="kind" value={draft.kind} onChange={(event) => { setDraft({ ...draft, kind: event.currentTarget.value }); }}>
            <option value="">{t("budget.transactions.all")}</option>
            {EFFECTIVE_KINDS.map((kind) => <option key={kind} value={kind}>{kindText(kind)}</option>)}
          </select>
        </label>
        <label>{t("budget.transactions.search")}
          <input name="query" type="search" maxLength={100} value={draft.query}
            onChange={(event) => { setDraft({ ...draft, query: event.currentTarget.value }); }} />
        </label>
        {initialProvisionalKeys === undefined ? null : (
          <label className="budget-toggle">
            <input name="provisional" type="checkbox" checked={provisionalOnly}
              onChange={(event) => {
                setProvisionalFilter({ period, keys: initialProvisionalKeys, checked: event.currentTarget.checked });
                setEditing(null);
                setFrozenOrder(null);
                setPairing(null);
              }} />
            {t("budget.transactions.provisionalOnly")}
          </label>
        )}
        <button type="submit">{t("budget.transactions.apply")}</button>
      </form>
      {notice === null ? null : <p className="budget-notice" role="alert">{notice}</p>}
      {activeProvisionalKeys !== undefined && initialProvisionalTruncated === true ? (
        <div className="budget-notice budget-entries-action" role="note">
          <span>{t("budget.transactions.provisionalTruncated", {
            shown: activeProvisionalKeys.length,
            total: initialProvisionalTotal ?? activeProvisionalKeys.length,
          })}</span>
        </div>
      ) : null}
      {page.failed ? (
        <div className="budget-notice budget-entries-action" role="alert">
          <span>{t("budget.transactions.error")}</span>
          <button type="button" onClick={retry}>{t("budget.transactions.retry")}</button>
        </div>
      ) : null}
      {page.loading && shownRows.length === 0 ? <p className="budget-muted" role="status">{t("budget.transactions.loading")}</p> : null}
      {!page.loading && !page.failed && shownRows.length === 0 ? (
        <>
          <p className="budget-muted">{t("budget.transactions.empty")}</p>
          {truncatedNotice}
        </>
      ) : null}
      {shownRows.length === 0 ? null : (
        <>
          <p className="budget-muted">{t("budget.transactions.total", { count: shownTotal })}</p>
          {page.loading ? <p className="budget-muted" role="status">{t("budget.transactions.loadingMore", { count: page.rows.length })}</p> : null}
          {truncatedNotice}
          <div className="budget-views budget-sort" role="group" aria-label={t("budget.transactions.sort")}>
            {SORT_KEYS.map((key) => {
              const active = sort?.key === key;
              const direction = active ? sort.direction : undefined;
              const label = sortLabel(key);
              const directionText = direction === "asc"
                ? t("budget.transactions.sortAsc")
                : direction === "desc" ? t("budget.transactions.sortDesc") : undefined;
              return (
                <button
                  type="button"
                  key={key}
                  aria-pressed={active}
                  data-sort={direction}
                  aria-label={directionText === undefined ? label : `${label}, ${directionText}`}
                  onClick={() => { changeSort(key); }}
                >
                  {label}
                </button>
              );
            })}
          </div>
          <ul className="budget-entry-list" aria-label={t("budget.transactions.title")}>
            {shownRows.map((row) => {
              const key = rowKey(row);
              const transferKey = TRANSACTION_TRANSFER_STATUS_KEYS[row.transfer.status];
              const transfer = transferKey === undefined ? "" : t(transferKey);
              const accountCandidate = counterpartyRuleCandidate(row);
              return (
                <li key={key} className="budget-entry">
                  <div className="budget-entry-main">
                    <span className="budget-muted">{transactionDate(row)}</span>
                    <span className="budget-entry-text">{counterparty(row) ?? transactionText(row)}</span>
                    <strong className={row.credit_debit_indicator === "DBIT" ? "is-debit" : "is-credit"}>
                      {formatAmount(row.amount, row.currency, locale, row.credit_debit_indicator)}
                    </strong>
                  </div>
                  <div className="budget-entry-facts">
                    <span>{accountName(row.account_id)}</span>
                    <CategoryBadge
                      color={categoryColors.get(
                        row.effective.category_id !== null && categoryNames.has(row.effective.category_id)
                          ? row.effective.category_id
                          : null,
                      )}
                      label={categoryName(row.effective.category_id)}
                    />
                    {row.effective.kind === "expense" || row.effective.kind === "income" ? null
                      : <span className="budget-chip">{kindText(row.effective.kind)}</span>}
                    {row.effective.source === "manual" ? <span>{t("budget.transactions.manual")}</span> : null}
                    {row.effective.source === "rule" ? <span>{t("budget.transactions.rule")}</span> : null}
                    {transfer === "" ? null : <span className="budget-chip">{transfer}</span>}
                    {row.transfer.ambiguous ? <span className="budget-warn">{t("budget.transactions.ambiguous")}</span> : null}
                    {oneOffLabel(row) === "" ? null : <span className="budget-chip budget-one-off">{oneOffLabel(row)}</span>}
                    {row.annotation.stale ? <span className="budget-warn">{t("budget.transactions.stale")}</span> : null}
                    {editing === key ? null : (
                      <button type="button" disabled={busy !== null} onClick={() => { startEditing(key); }}>{t("budget.transactions.edit")}</button>
                    )}
                    {canPair(row) && pairing !== key ? (
                      <button type="button" disabled={busy !== null} onClick={() => { openPairs(row); }}>{t("budget.transactions.pair")}</button>
                    ) : null}
                    {editing === key || onCreateRule === undefined || accountCandidate === null ? null : (
                      <button type="button" disabled={busy !== null} title={row.counterparty_account ?? undefined}
                        onClick={() => { onCreateRule(accountCandidate); }}>{t("budget.transactions.accountRule")}</button>
                    )}
                  </div>
                  {editing === key ? (
                    <Editor row={row} categories={categories} kindText={kindText} saving={busy === key}
                      onSave={(category, kind, label) => { void save(row, category, kind, label); }} onCancel={stopEditing}
                      onCreateRule={onCreateRule} />
                  ) : null}
                  {pairing === key ? (
                    <PairPicker debit={row} page={pairPage} accounts={accounts} connections={connections} locale={locale}
                      disabled={busy !== null} onConfirm={(credit) => { void confirmPair(row, credit); }} onCancel={closePairs} />
                  ) : null}
                </li>
              );
            })}
          </ul>
        </>
      )}
    </section>
  );
}

interface PairPickerProps {
  debit: AnalyticsTransaction;
  page: PairPage;
  accounts: readonly BudgetAccount[];
  connections: readonly BudgetConnection[];
  locale: string;
  disabled: boolean;
  onConfirm: (credit: AnalyticsTransaction) => void;
  onCancel: () => void;
}

function PairPicker({ debit, page, accounts, connections, locale, disabled, onConfirm, onCancel }: PairPickerProps) {
  const candidates = pairCandidates(debit, page.rows);
  const debitDate = pairDate(debit);
  return (
    <div className="budget-pair-picker" aria-label={t("budget.transactions.pairTitle")}>
      <p className="budget-muted">{t("budget.transactions.pairTitle")}</p>
      {page.limited ? <p className="budget-notice" role="note">{t("budget.transactions.pairLimited")}</p> : null}
      {page.loading ? <p className="budget-muted" role="status">{t("budget.transactions.pairLoading")}</p> : null}
      {page.failed ? <p className="budget-notice" role="alert">{t("budget.transactions.pairError")}</p> : null}
      {!page.loading && !page.failed && candidates.length === 0 ? <p className="budget-muted">{t("budget.transactions.pairEmpty")}</p> : null}
      {!page.loading && !page.failed && candidates.length > 0 ? (
        <ul className="budget-entry-list">
          {candidates.map((credit) => {
            const creditDate = pairDate(credit);
            if (debitDate === null || creditDate === null) return null;
            const days = daysBetween(debitDate, creditDate);
            return (
              <li key={rowKey(credit)} className="budget-entry">
                <div className="budget-entry-main">
                  <span className="budget-muted">{creditDate}</span>
                  <span className="budget-entry-text">{counterparty(credit) ?? transactionText(credit)}</span>
                  <strong className="is-credit">{formatAmount(credit.amount, credit.currency, locale, credit.credit_debit_indicator)}</strong>
                </div>
                <div className="budget-entry-facts">
                  <span>{nameOf(accounts, connections, credit.account_id, t("budget.transactions.account"))}</span>
                  <span>{days === 0 ? t("budget.transactions.pairDaysSame") : tPlural("budget.transactions.pairDays", days)}</span>
                  <button type="button" className="budget-primary" disabled={disabled} onClick={() => { onConfirm(credit); }}>{t("budget.transactions.pairConfirm")}</button>
                </div>
              </li>
            );
          })}
        </ul>
      ) : null}
      <div><button type="button" disabled={disabled} onClick={onCancel}>{t("budget.transactions.cancel")}</button></div>
    </div>
  );
}

interface EditorProps {
  row: AnalyticsTransaction;
  categories: readonly AnalyticsCategory[];
  kindText: (kind: string) => string;
  saving: boolean;
  onSave: (category: CategoryChoice, kind: KindChoice, label: string) => void;
  onCancel: () => void;
  onCreateRule?: ((candidate: RuleCandidate) => void) | undefined;
}

function oneOffLabel(row: AnalyticsTransaction): string {
  const value = (row.annotation as { label?: string | null }).label;
  return typeof value === "string" ? value : "";
}

/** Select values: "auto" (rules decide), "none" (explicitly uncategorised) or "cat:<id>". */
function encode(choice: CategoryChoice): string {
  if (choice.mode === "auto") return "auto";
  return choice.category_id === null ? "none" : `cat:${choice.category_id}`;
}
function decode(value: string): CategoryChoice {
  if (value === "auto") return { mode: "auto" };
  return { mode: "manual", category_id: value.startsWith("cat:") ? value.slice(4) : null };
}

function Editor({ row, categories, kindText, saving, onSave, onCancel, onCreateRule }: EditorProps) {
  const [category, setCategory] = useState(encode(currentChoice(row)));
  const kinds = kindChoicesFor(row.credit_debit_indicator);
  const [kind, setKind] = useState<KindChoice>(row.annotation.mode === "manual"
    ? kindChoice(row.annotation.kind, row.credit_debit_indicator) : "auto");
  const [label, setLabel] = useState(oneOffLabel(row));
  const accountCandidate = counterpartyRuleCandidate(row);
  // An archived category stays selectable only as this entry's current classification: the category it shows now,
  // or its own manual choice unless the bank changed the entry since. Assigning any other archived category is refused.
  const manual = row.annotation.mode === "manual" && !row.annotation.stale ? row.annotation.category_id : null;
  const kept = new Set([manual, row.effective.category_id]);
  const offered = categories.filter((item) => !item.archived || kept.has(item.id));
  // "Automatic" hands both category and type back to rules. Choosing a type is a manual decision, so the category
  // visibly becomes the one the entry shows now (or explicitly none) instead of a hidden automatic state.
  const chooseKind = (value: KindChoice) => {
    setKind(value);
    if (value !== "auto" && category === "auto") setCategory(encode({ mode: "manual", category_id: row.effective.category_id }));
  };
  const chooseCategory = (value: string) => {
    setCategory(value);
    if (value === "auto") setKind("auto");
  };
  return (
    <form className="budget-entry-editor" onSubmit={(event) => { event.preventDefault(); onSave(decode(category), kind, label); }}>
      <label>{t("budget.transactions.category")}
        <select name="category_id" value={category} disabled={saving} onChange={(event) => { chooseCategory(event.currentTarget.value); }}>
          <option value="auto">{t("budget.transactions.automatic")}</option>
          <option value="none">{t("budget.transactions.uncategorised")}</option>
          {offered.map((item) => (
            <option key={item.id} value={`cat:${item.id}`}>{item.archived ? `${item.name} (${t("budget.transactions.archived")})` : item.name}</option>
          ))}
        </select>
      </label>
      <label>{t("budget.transactions.kind")}
        <select name="kind" value={kind} disabled={saving}
          onChange={(event) => { chooseKind(kindChoice(event.currentTarget.value, row.credit_debit_indicator)); }}>
          {kinds.map((item) => <option key={item} value={item}>{kindText(item)}</option>)}
        </select>
      </label>
      <label>{t("budget.transactions.oneOffLabel")}
        <input name="one_off_label" type="text" maxLength={80} value={label} disabled={saving}
          onChange={(event) => { setLabel(event.currentTarget.value); }} />
      </label>
      <p className="budget-muted">{t("budget.transactions.oneOffHint")}</p>
      {onCreateRule === undefined || accountCandidate === null ? null : (
        <button type="button" disabled={saving} title={row.counterparty_account ?? undefined}
          onClick={() => { onCreateRule(accountCandidate); }}>{t("budget.transactions.accountRule")}</button>
      )}
      <button type="submit" className="budget-primary" disabled={saving}>{t("budget.transactions.save")}</button>
      <button type="button" disabled={saving} onClick={onCancel}>{t("budget.transactions.cancel")}</button>
    </form>
  );
}
