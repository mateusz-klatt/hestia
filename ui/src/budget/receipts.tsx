// Owner-private receipts and documents view ("Paragony i dowody"). Keeps all receipt data,
// image object URLs, and matched bank details in React component memory only (never localStorage).
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { t } from "../i18n";
import type { BudgetApi, BudgetResult } from "./api";
import { receiptsApi as defaultReceiptsApi } from "./receipts_api";
import type { ReceiptsApi } from "./receipts_api";
import { currentMonth, labelOf } from "./analytics/model";
import { formatAmount } from "./model";
import type { BudgetAccount, BudgetConnection } from "./model";
import { EMPTY_RECEIPT_FILTER, RECEIPT_DOCUMENT_KINDS, RECEIPT_DOC_KIND_KEYS, RECEIPT_KINDS,
  RECEIPT_KIND_KEYS, RECEIPT_LINE_KIND_KEYS, RECEIPT_PAYMENT_METHODS, RECEIPT_PAYMENT_METHOD_KEYS,
  RECEIPT_SOURCE_KINDS, RECEIPT_SOURCE_KIND_KEYS, RECEIPT_STATUSES, RECEIPT_STATUS_KEYS, buildConfirmRequest,
  buildReceiptListRequest, candidateDisplayDate, computeLinesDelta, draftFromDetail, filterReceiptRows,
  findCurrentLinkedCandidate, hasActiveReceiptFilter, indexAccountTransactions, indexAnalyticsTransactions,
  isRedactedValue, mergeReceiptRows, normalizeDecimalInput, receiptAccountLabel, receiptLookupDateRange,
  receiptLookupMonths, subtractDecimals, txLookupKey, validReceiptDateRange } from "./receipts_model";
import type { ReceiptCandidate, ReceiptConfirmDraft, ReceiptDetail, ReceiptDetailResult, ReceiptDocumentKind,
  ReceiptFilter, ReceiptLineInput, ReceiptPaymentMethod, ReceiptSourceKind, ReceiptStatus, ReceiptSummary,
  ResolvedTransaction } from "./receipts_model";

type AccessBarrier = (result: { ok: boolean; status?: number; error?: string }) => boolean;

export interface ReceiptsViewProps {
  api: Pick<BudgetApi, "analyticsTransactions" | "transactions">;
  receiptsApi?: ReceiptsApi | undefined;
  accounts: readonly BudgetAccount[];
  connections?: readonly BudgetConnection[] | undefined;
  analyticsEnabled?: boolean | undefined;
  locale: string;
  revision: number;
  onAccessLoss: AccessBarrier;
}

type ListState =
  | { kind: "loading" }
  | { kind: "ready"; rows: ReceiptSummary[]; nextCursor: string | null; loadingMore: boolean; loadMoreError: boolean }
  | { kind: "disabled" }
  | { kind: "error" };

type DetailState =
  | { kind: "loading" }
  | { kind: "ready"; detail: ReceiptDetail; candidates: ReceiptCandidate[] }
  | { kind: "error" };

type ImageState =
  | { kind: "none" }
  | { kind: "loading" }
  | { kind: "ready"; url: string }
  | { kind: "unavailable" };

function isDisabledStatus(result: { ok: boolean; status?: number; error?: string }): boolean {
  return !result.ok && result.status === 404 && result.error !== "budget_receipt_not_found";
}

function revokeObjectUrl(url: string | null): void {
  if (url !== null && typeof URL.revokeObjectURL === "function") {
    URL.revokeObjectURL(url);
  }
}

function createObjectUrl(blob: Blob): string | null {
  return typeof URL.createObjectURL === "function" ? URL.createObjectURL(blob) : null;
}

function RedactedOrText({ value, fallback = "" }: { value: string | null | undefined; fallback?: string }) {
  if (value === null || value === undefined || value === "") {
    return <>{fallback}</>;
  }
  if (isRedactedValue(value)) {
    return (
      <span className="budget-redacted" title={t("budget.receipts.redactedTooltip")}>
        {t("budget.receipts.redactedLabel")}
      </span>
    );
  }
  return <>{value}</>;
}

function SourceIcon({ kind }: { kind: ReceiptSourceKind }) {
  if (kind === "email") {
    return (
      <svg className="budget-receipt-icon" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
        <rect x="1.5" y="3" width="13" height="10" rx="1.5" fill="none" stroke="currentColor" strokeWidth="1.4" />
        <path d="M2 4l6 4.5L14 4" fill="none" stroke="currentColor" strokeWidth="1.4" />
      </svg>
    );
  }
  if (kind === "attachment") {
    return (
      <svg className="budget-receipt-icon" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
        <path d="M4 1.5h5l3.5 3.5v9.5h-8.5z" fill="none" stroke="currentColor" strokeWidth="1.4" />
        <path d="M9 1.5V5h3.5" fill="none" stroke="currentColor" strokeWidth="1.4" />
      </svg>
    );
  }
  if (kind === "local_ocr" || kind === "photo") {
    return (
      <svg className="budget-receipt-icon" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
        <rect x="1.5" y="4" width="13" height="9" rx="1.5" fill="none" stroke="currentColor" strokeWidth="1.4" />
        <circle cx="8" cy="8.5" r="2.4" fill="none" stroke="currentColor" strokeWidth="1.4" />
        <path d="M5.5 4l1-1.5h3L10.5 4" fill="none" stroke="currentColor" strokeWidth="1.4" />
      </svg>
    );
  }
  if (kind === "cloud_ai") {
    return (
      <svg className="budget-receipt-icon" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
        <path d="M8 2l1.4 3.6L13 7l-3.6 1.4L8 12l-1.4-3.6L3 7l3.6-1.4z" fill="none" stroke="currentColor" strokeWidth="1.4" />
      </svg>
    );
  }
  return (
    <svg className="budget-receipt-icon" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
      <rect x="3" y="2" width="10" height="12" rx="1.2" fill="none" stroke="currentColor" strokeWidth="1.4" />
      <path d="M5.5 5.5h5M5.5 8h5M5.5 10.5h3.5" fill="none" stroke="currentColor" strokeWidth="1.4" />
    </svg>
  );
}

function receiptTotalDisplay(receipt: Pick<ReceiptSummary, "total_amount" | "currency">, locale: string): string {
  if (receipt.total_amount === null || receipt.currency === null) {
    return t("budget.receipts.noAmount");
  }
  return formatAmount(receipt.total_amount, receipt.currency, locale);
}

function confirmErrorText(error: string): string {
  switch (error) {
    case "budget_receipt_conflict": return t("budget.receipts.conflict");
    case "budget_receipt_tx_already_linked": return t("budget.receipts.txAlreadyLinked");
    case "budget_receipt_stale_source": return t("budget.receipts.staleSource");
    case "budget_receipt_total_mismatch": return t("budget.receipts.totalMismatch");
    case "budget_receipt_not_found": return t("budget.receipts.notFound");
    default: return t("budget.receipts.confirmError");
  }
}

export function ReceiptsView({
  api,
  receiptsApi = defaultReceiptsApi,
  accounts,
  connections = [],
  analyticsEnabled = true,
  locale,
  revision,
  onAccessLoss,
}: ReceiptsViewProps) {
  const [draft, setDraft] = useState<ReceiptFilter>(EMPTY_RECEIPT_FILTER);
  const [filter, setFilter] = useState<ReceiptFilter>(EMPTY_RECEIPT_FILTER);
  const [listState, setListState] = useState<ListState>({ kind: "loading" });
  const [listRefresh, setListRefresh] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detailState, setDetailState] = useState<DetailState>({ kind: "loading" });
  const [detailRefresh, setDetailRefresh] = useState(0);
  const [imageState, setImageState] = useState<ImageState>({ kind: "none" });
  const [txMap, setTxMap] = useState<ReadonlyMap<string, ResolvedTransaction>>(() => new Map());
  const [txLoading, setTxLoading] = useState(false);

  const mounted = useRef(true);
  const listGeneration = useRef(0);
  const detailGeneration = useRef(0);
  const imageGeneration = useRef(0);
  const txGeneration = useRef(0);
  const activeImageUrl = useRef<string | null>(null);

  const clearImage = useCallback(() => {
    imageGeneration.current += 1;
    revokeObjectUrl(activeImageUrl.current);
    activeImageUrl.current = null;
    setImageState({ kind: "none" });
  }, []);

  const clearAllReceiptsState = useCallback((disabled: boolean) => {
    listGeneration.current += 1;
    detailGeneration.current += 1;
    txGeneration.current += 1;
    clearImage();
    setSelectedId(null);
    setDetailState({ kind: "loading" });
    setTxMap(new Map());
    setTxLoading(false);
    setNotice(null);
    setListState(disabled ? { kind: "disabled" } : { kind: "error" });
  }, [clearImage]);

  const checkBarrier = useCallback((result: { ok: boolean; status?: number; error?: string }): boolean => {
    if (onAccessLoss(result)) {
      if (mounted.current) clearAllReceiptsState(false);
      return true;
    }
    if (!result.ok && isDisabledStatus(result)) {
      if (mounted.current) clearAllReceiptsState(true);
      return true;
    }
    return false;
  }, [onAccessLoss, clearAllReceiptsState]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      listGeneration.current += 1;
      detailGeneration.current += 1;
      imageGeneration.current += 1;
      txGeneration.current += 1;
      revokeObjectUrl(activeImageUrl.current);
      activeImageUrl.current = null;
    };
  }, []);

  const searchMode = filter.query.trim() !== "";

  useEffect(() => {
    const token = ++listGeneration.current;
    setListState({ kind: "loading" });
    const request = searchMode
      ? receiptsApi.receiptsSearch({ query: filter.query.trim(), limit: 50 })
      : receiptsApi.receiptsList(buildReceiptListRequest(filter));
    void request.then((result) => {
      if (!mounted.current || checkBarrier(result) || token !== listGeneration.current) return;
      if (!result.ok) {
        setListState(result.status === 404 ? { kind: "disabled" } : { kind: "error" });
        return;
      }
      setListState({
        kind: "ready",
        rows: result.data.receipts,
        nextCursor: result.data.next_cursor,
        loadingMore: false,
        loadMoreError: false,
      });
    });
  }, [receiptsApi, filter, searchMode, revision, listRefresh, checkBarrier]);

  const loadMore = useCallback(() => {
    if (listState.kind !== "ready" || listState.loadingMore || listState.nextCursor === null) return;
    const cursor = listState.nextCursor;
    const token = listGeneration.current;
    setListState((current) => (current.kind === "ready"
      ? { ...current, loadingMore: true, loadMoreError: false }
      : current));
    const request = searchMode
      ? receiptsApi.receiptsSearch({ query: filter.query.trim(), limit: 50, cursor })
      : receiptsApi.receiptsList(buildReceiptListRequest(filter, cursor));
    void request.then((result) => {
      if (!mounted.current || checkBarrier(result) || token !== listGeneration.current) return;
      if (!result.ok) {
        setListState((current) => (current.kind === "ready"
          ? { ...current, loadingMore: false, loadMoreError: true }
          : current));
        return;
      }
      setListState((current) => {
        if (current.kind !== "ready") return current;
        const merged = mergeReceiptRows(current.rows, result.data.receipts);
        const next = result.data.next_cursor;
        const exhausted = next === null || next === cursor || merged.length === current.rows.length;
        return {
          kind: "ready",
          rows: merged,
          nextCursor: exhausted ? null : next,
          loadingMore: false,
          loadMoreError: false,
        };
      });
    });
  }, [receiptsApi, filter, listState, searchMode, checkBarrier]);

  const resolveLinkedTransactions = useCallback(async (detail: ReceiptDetail, candidates: readonly ReceiptCandidate[]) => {
    const token = ++txGeneration.current;
    const targets: { accountId: string; entryRefDigest: string; date: string | null }[] = [];
    if (detail.linked && detail.linked_account_id !== null && detail.linked_entry_ref !== null) {
      targets.push({
        accountId: detail.linked_account_id,
        entryRefDigest: detail.linked_entry_ref,
        date: detail.receipt_date,
      });
    }
    for (const candidate of candidates) {
      targets.push({
        accountId: candidate.account_id,
        entryRefDigest: candidate.entry_reference,
        date: candidateDisplayDate(candidate) ?? detail.receipt_date,
      });
    }
    if (targets.length === 0) {
      setTxMap(new Map());
      setTxLoading(false);
      return;
    }

    setTxLoading(true);
    setTxMap(new Map());
    const collected = new Map<string, ResolvedTransaction>();
    const hasAllTargets = () => targets.every((item) => collected.has(txLookupKey(item.accountId, item.entryRefDigest)));

    const months = new Set<string>();
    for (const target of targets) {
      for (const month of receiptLookupMonths(target.date)) months.add(month);
    }
    if (months.size === 0 && detail.linked) {
      months.add(currentMonth());
    }

    if (analyticsEnabled && months.size > 0) {
      for (const month of months) {
        let cursor: string | undefined;
        for (let pageNumber = 0; pageNumber < 5; pageNumber += 1) {
          const result = await api.analyticsTransactions(
            month,
            { currency: detail.currency ?? "", category: "", kind: "", query: "" },
            cursor,
            200,
          );
          if (!mounted.current || checkBarrier(result) || token !== txGeneration.current) return;
          if (!result.ok) break;
          const pageMap = indexAnalyticsTransactions(result.data.transactions);
          for (const [k, v] of pageMap) collected.set(k, v);
          if (hasAllTargets()) break;
          cursor = result.data.next_cursor ?? undefined;
          if (cursor === undefined) break;
        }
        if (hasAllTargets()) break;
      }
    }

    if (!hasAllTargets()) {
      const missingAccountIds = [...new Set(
        targets.filter((item) => !collected.has(txLookupKey(item.accountId, item.entryRefDigest)))
          .map((item) => item.accountId),
      )];
      const range = receiptLookupDateRange(detail.receipt_date);
      for (const accountId of missingAccountIds) {
        let cursor: string | undefined;
        for (let pageNumber = 0; pageNumber < 3; pageNumber += 1) {
          const result = await api.transactions(accountId, { ...range, cursor, limit: 200 });
          if (!mounted.current || checkBarrier(result) || token !== txGeneration.current) return;
          if (!result.ok) break;
          const pageMap = indexAccountTransactions(accountId, result.data.transactions);
          for (const [k, v] of pageMap) collected.set(k, v);
          if (hasAllTargets()) break;
          cursor = result.data.next_cursor ?? undefined;
          if (cursor === undefined) break;
        }
      }
    }

    if (!mounted.current || token !== txGeneration.current) return;
    setTxMap(collected);
    setTxLoading(false);
  }, [api, analyticsEnabled, checkBarrier]);

  useEffect(() => {
    if (selectedId === null) {
      detailGeneration.current += 1;
      txGeneration.current += 1;
      clearImage();
      setTxMap(new Map());
      setTxLoading(false);
      return;
    }
    const token = ++detailGeneration.current;
    setDetailState({ kind: "loading" });
    clearImage();
    void receiptsApi.receiptDetail(selectedId).then((result: BudgetResult<ReceiptDetailResult>) => {
      if (!mounted.current || checkBarrier(result) || token !== detailGeneration.current) return;
      if (!result.ok) {
        if (result.status === 404) {
          setSelectedId(null);
          setNotice(t("budget.receipts.notFound"));
          setListRefresh((current) => current + 1);
          return;
        }
        setDetailState({ kind: "error" });
        return;
      }
      const receipt = result.data.receipt;
      const candidates = result.data.candidates ?? [];
      setDetailState({ kind: "ready", detail: receipt, candidates });
      void resolveLinkedTransactions(receipt, candidates);
      if (receipt.image_available) {
        const imgToken = ++imageGeneration.current;
        setImageState({ kind: "loading" });
        void receiptsApi.receiptImage(receipt.receipt_id).then((imgResult) => {
          if (!mounted.current || checkBarrier(imgResult) || imgToken !== imageGeneration.current
            || token !== detailGeneration.current) return;
          if (!imgResult.ok) {
            setImageState({ kind: "unavailable" });
            return;
          }
          const objectUrl = createObjectUrl(imgResult.data);
          if (objectUrl === null) {
            setImageState({ kind: "unavailable" });
            return;
          }
          revokeObjectUrl(activeImageUrl.current);
          activeImageUrl.current = objectUrl;
          setImageState({ kind: "ready", url: objectUrl });
        });
      }
    });
  }, [receiptsApi, selectedId, revision, detailRefresh, checkBarrier, clearImage, resolveLinkedTransactions]);

  const closeDetail = useCallback(() => {
    detailGeneration.current += 1;
    txGeneration.current += 1;
    clearImage();
    setSelectedId(null);
    setTxMap(new Map());
    setTxLoading(false);
  }, [clearImage]);

  const shownRows = useMemo(() => {
    if (listState.kind !== "ready") return [];
    return filterReceiptRows(listState.rows, filter, searchMode);
  }, [listState, filter, searchMode]);

  const applyFilter = (next: ReceiptFilter) => {
    if (!validReceiptDateRange(next)) return;
    setNotice(null);
    setFilter(next);
  };

  const clearFilters = () => {
    setDraft(EMPTY_RECEIPT_FILTER);
    setFilter(EMPTY_RECEIPT_FILTER);
    setNotice(null);
  };

  const title = t("budget.receipts.title");

  if (listState.kind === "disabled") {
    return (
      <section className="budget-entries budget-receipts" aria-label={title}>
        <h4>{title}</h4>
        <p className="budget-status" role="status">{t("budget.receipts.disabled")}</p>
      </section>
    );
  }

  return (
    <section className="budget-entries budget-receipts" aria-label={title}>
      <header className="budget-entries-head">
        <div>
          <h4>{title}</h4>
          <p className="budget-muted">{t("budget.receipts.intro")}</p>
        </div>
      </header>

      <form className="budget-range" onSubmit={(event) => { event.preventDefault(); applyFilter(draft); }}>
        <label>
          {t("budget.receipts.search")}
          <input
            name="query"
            type="search"
            maxLength={128}
            placeholder={t("budget.receipts.searchPlaceholder")}
            value={draft.query}
            onChange={(event) => { setDraft({ ...draft, query: event.currentTarget.value }); }}
          />
        </label>
        <label>
          {t("budget.receipts.status")}
          <select
            name="status"
            value={draft.status}
            onChange={(event) => { setDraft({ ...draft, status: event.currentTarget.value as "" | ReceiptStatus }); }}
          >
            <option value="">{t("budget.receipts.all")}</option>
            {RECEIPT_STATUSES.map((item) => (
              <option key={item} value={item}>{labelOf(RECEIPT_STATUS_KEYS, item)}</option>
            ))}
          </select>
        </label>
        <label>
          {t("budget.receipts.documentKindLabel")}
          <select
            name="document_kind"
            value={draft.documentKind}
            onChange={(event) => {
              setDraft({ ...draft, documentKind: event.currentTarget.value as "" | ReceiptDocumentKind });
            }}
          >
            <option value="">{t("budget.receipts.all")}</option>
            {RECEIPT_DOCUMENT_KINDS.map((item) => (
              <option key={item} value={item}>{labelOf(RECEIPT_DOC_KIND_KEYS, item)}</option>
            ))}
          </select>
        </label>
        <label>
          {t("budget.receipts.sourceKindLabel")}
          <select
            name="source_kind"
            value={draft.sourceKind}
            onChange={(event) => {
              setDraft({ ...draft, sourceKind: event.currentTarget.value as "" | ReceiptSourceKind });
            }}
          >
            <option value="">{t("budget.receipts.all")}</option>
            {RECEIPT_SOURCE_KINDS.map((item) => (
              <option key={item} value={item}>{labelOf(RECEIPT_SOURCE_KIND_KEYS, item)}</option>
            ))}
          </select>
        </label>
        <label>
          {t("budget.receipts.linkFilterLabel")}
          <select
            name="linked"
            value={draft.linked}
            onChange={(event) => {
              setDraft({ ...draft, linked: event.currentTarget.value as "" | "linked" | "unlinked" });
            }}
          >
            <option value="">{t("budget.receipts.all")}</option>
            <option value="linked">{t("budget.receipts.linkedBadge")}</option>
            <option value="unlinked">{t("budget.receipts.unlinkedBadge")}</option>
          </select>
        </label>
        <label>
          {t("budget.main.from")}
          <input
            name="date_from"
            type="date"
            value={draft.dateFrom}
            max={draft.dateTo || undefined}
            onChange={(event) => { setDraft({ ...draft, dateFrom: event.currentTarget.value }); }}
          />
        </label>
        <label>
          {t("budget.main.to")}
          <input
            name="date_to"
            type="date"
            value={draft.dateTo}
            min={draft.dateFrom || undefined}
            onChange={(event) => { setDraft({ ...draft, dateTo: event.currentTarget.value }); }}
          />
        </label>
        <button type="submit">{t("budget.receipts.apply")}</button>
        {hasActiveReceiptFilter(draft) || hasActiveReceiptFilter(filter) ? (
          <button type="button" onClick={clearFilters}>{t("budget.receipts.clearFilters")}</button>
        ) : null}
      </form>

      {validReceiptDateRange(draft) ? null : <p className="budget-notice" role="alert">{t("budget.main.badRange")}</p>}
      {notice === null ? null : <p className="budget-notice" role="status">{notice}</p>}

      {listState.kind === "loading" ? <p className="budget-muted" role="status">{t("budget.receipts.loading")}</p> : null}

      {listState.kind === "error" ? (
        <div className="budget-notice budget-entries-action" role="alert">
          <span>{t("budget.receipts.loadError")}</span>
          <button type="button" onClick={() => { setListRefresh((current) => current + 1); }}>
            {t("budget.receipts.retry")}
          </button>
        </div>
      ) : null}

      {listState.kind === "ready" ? (
        <>
          {shownRows.length === 0 ? (
            <p className="budget-muted">
              {listState.nextCursor !== null
                ? t("budget.receipts.emptyPageMore")
                : t("budget.receipts.empty")}
            </p>
          ) : (
            <>
              <p className="budget-muted">{t("budget.receipts.shownCount", { count: shownRows.length })}</p>
              <ul className="budget-entry-list" aria-label={title}>
                {shownRows.map((row) => (
                  <li key={row.receipt_id} className="budget-entry budget-receipt-row">
                    <div className="budget-receipt-row-main">
                      <span className="budget-muted">
                        {row.receipt_date ?? t("budget.receipts.noDate")}
                        {row.receipt_time !== null && row.receipt_time !== "" ? ` ${row.receipt_time}` : ""}
                      </span>
                      <strong className="budget-receipt-merchant">
                        <RedactedOrText
                          value={row.raw_shop_name}
                          fallback={row.forwarded_sender_domain ?? t("budget.receipts.unknownMerchant")}
                        />
                      </strong>
                      <strong className="budget-receipt-total">{receiptTotalDisplay(row, locale)}</strong>
                    </div>
                    <div className="budget-entry-facts">
                      <span className="budget-receipt-source" data-source-kind={row.source_kind}>
                        <SourceIcon kind={row.source_kind} />
                        <span>{labelOf(RECEIPT_SOURCE_KIND_KEYS, row.source_kind)}</span>
                      </span>
                      {row.document_kind === null ? null : (
                        <span className="budget-chip">{labelOf(RECEIPT_DOC_KIND_KEYS, row.document_kind)}</span>
                      )}
                      {row.receipt_kind === "refund" ? (
                        <span className="budget-chip">{t("budget.receipts.receiptKind.refund")}</span>
                      ) : null}
                      <span className="budget-chip budget-receipt-status" data-status={row.status}>
                        {labelOf(RECEIPT_STATUS_KEYS, row.status)}
                      </span>
                      <span className="budget-chip budget-receipt-link-badge" data-linked={row.linked ? "true" : "false"}>
                        {row.linked ? t("budget.receipts.linkedBadge") : t("budget.receipts.unlinkedBadge")}
                      </span>
                      <button type="button" onClick={() => { setNotice(null); setSelectedId(row.receipt_id); }}>
                        {t("budget.receipts.open")}
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            </>
          )}
          {listState.loadMoreError ? (
            <p className="budget-notice" role="alert">{t("budget.receipts.loadError")}</p>
          ) : null}
          {listState.nextCursor === null ? null : (
            <div className="budget-actions">
              <button type="button" disabled={listState.loadingMore} onClick={loadMore}>
                {t("budget.receipts.loadMore")}
              </button>
            </div>
          )}
        </>
      ) : null}

      {selectedId === null ? null : (
        <ReceiptDetailDrawer
          api={receiptsApi}
          accounts={accounts}
          connections={connections}
          locale={locale}
          detailState={detailState}
          imageState={imageState}
          txMap={txMap}
          txLoading={txLoading}
          onAccessLoss={checkBarrier}
          onClose={closeDetail}
          onRetryDetail={() => { setDetailRefresh((current) => current + 1); }}
          onConfirmed={(updated) => {
            setDetailState({ kind: "ready", detail: updated, candidates: [] });
            setNotice(t("budget.receipts.confirmSuccess"));
            setListRefresh((current) => current + 1);
          }}
          onConflictOrStale={() => {
            setDetailRefresh((current) => current + 1);
            setListRefresh((current) => current + 1);
          }}
          onNotFound={() => {
            closeDetail();
            setNotice(t("budget.receipts.notFound"));
            setListRefresh((current) => current + 1);
          }}
          onDeleted={() => {
            closeDetail();
            setListRefresh((current) => current + 1);
          }}
        />
      )}
    </section>
  );
}

interface DrawerProps {
  api: Pick<ReceiptsApi, "confirmReceipt" | "deleteReceipt">;
  accounts: readonly BudgetAccount[];
  connections: readonly BudgetConnection[];
  locale: string;
  detailState: DetailState;
  imageState: ImageState;
  txMap: ReadonlyMap<string, ResolvedTransaction>;
  txLoading: boolean;
  onAccessLoss: (result: { ok: boolean; status?: number; error?: string }) => boolean;
  onClose: () => void;
  onRetryDetail: () => void;
  onConfirmed: (receipt: ReceiptDetail) => void;
  onConflictOrStale: () => void;
  onNotFound: () => void;
  onDeleted: () => void;
}

function ReceiptDetailDrawer({
  api,
  accounts,
  connections,
  locale,
  detailState,
  imageState,
  txMap,
  txLoading,
  onAccessLoss,
  onClose,
  onRetryDetail,
  onConfirmed,
  onConflictOrStale,
  onNotFound,
  onDeleted,
}: DrawerProps) {
  const [confirmDraft, setConfirmDraft] = useState<ReceiptConfirmDraft | null>(null);
  const [actionBusy, setActionBusy] = useState<"confirm" | "delete" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const actionGeneration = useRef(0);
  const keepActionErrorRef = useRef(false);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && actionBusy === null) onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => { document.removeEventListener("keydown", onKeyDown); };
  }, [onClose, actionBusy]);

  useEffect(() => () => { actionGeneration.current += 1; }, []);

  useEffect(() => {
    if (detailState.kind === "ready") {
      setConfirmDraft(draftFromDetail(detailState.detail, detailState.candidates));
      if (keepActionErrorRef.current) {
        keepActionErrorRef.current = false;
      } else {
        setActionError(null);
      }
      setConfirmingDelete(false);
    } else if (detailState.kind === "loading") {
      setConfirmDraft(null);
      if (!keepActionErrorRef.current) setActionError(null);
      setConfirmingDelete(false);
    } else {
      keepActionErrorRef.current = false;
      setConfirmDraft(null);
      setActionError(null);
      setConfirmingDelete(false);
    }
  }, [detailState]);

  const title = t("budget.receipts.detailTitle");

  const submitConfirm = async (detail: ReceiptDetail, candidates: readonly ReceiptCandidate[], draft: ReceiptConfirmDraft) => {
    const built = buildConfirmRequest(detail, draft, candidates);
    if (!built.ok) {
      setActionError(t(built.errorKey));
      return;
    }
    const token = ++actionGeneration.current;
    setActionBusy("confirm");
    keepActionErrorRef.current = false;
    setActionError(null);
    const result = await api.confirmReceipt(built.request);
    if (onAccessLoss(result) || token !== actionGeneration.current) return;
    setActionBusy(null);
    if (!result.ok) {
      if (result.status === 404) {
        onNotFound();
        return;
      }
      setActionError(confirmErrorText(result.error));
      if (result.error === "budget_receipt_conflict"
        || result.error === "budget_receipt_tx_already_linked"
        || result.error === "budget_receipt_stale_source") {
        keepActionErrorRef.current = true;
        onConflictOrStale();
      }
      return;
    }
    onConfirmed(result.data.receipt);
  };

  const submitDelete = async (detail: ReceiptDetail) => {
    const token = ++actionGeneration.current;
    setActionBusy("delete");
    keepActionErrorRef.current = false;
    setActionError(null);
    const result = await api.deleteReceipt({ receipt_id: detail.receipt_id, expected_version: detail.version });
    if (onAccessLoss(result) || token !== actionGeneration.current) return;
    setActionBusy(null);
    if (!result.ok) {
      if (result.status === 404) {
        onNotFound();
        return;
      }
      setActionError(result.error === "budget_receipt_conflict"
        ? t("budget.receipts.conflict")
        : t("budget.receipts.deleteError"));
      if (result.error === "budget_receipt_conflict") {
        keepActionErrorRef.current = true;
        onConflictOrStale();
      }
      return;
    }
    onDeleted();
  };

  const addSummaryLine = (detail: ReceiptDetail, draft: ReceiptConfirmDraft) => {
    const normalizedTotal = normalizeDecimalInput(draft.totalAmount) ?? draft.totalAmount.trim();
    const defaultName = detail.invoice_number !== null && !isRedactedValue(detail.invoice_number)
      ? detail.invoice_number
      : (draft.rawShopName.trim() !== "" && !isRedactedValue(draft.rawShopName.trim())
        ? draft.rawShopName.trim()
        : t("budget.receipts.summaryLineDefault"));
    const summaryLine: ReceiptLineInput = {
      line_kind: "other",
      raw_name: defaultName,
      ptu_code: null,
      quantity: "1",
      raw_unit: null,
      unit_price_raw: normalizedTotal,
      gross_line_total: normalizedTotal,
      discount_amount: "0",
      net_line_total: normalizedTotal,
    };
    setConfirmDraft({ ...draft, lines: [...draft.lines, summaryLine] });
  };

  const updateLine = (index: number, patch: Partial<ReceiptLineInput>) => {
    setConfirmDraft((current) => {
      if (current === null) return null;
      const lines = current.lines.map((line, idx) => {
        if (idx !== index) return line;
        const next = { ...line, ...patch };
        if ("gross_line_total" in patch || "discount_amount" in patch) {
          const computed = subtractDecimals(next.gross_line_total, next.discount_amount);
          if (computed !== null) next.net_line_total = computed;
        }
        return next;
      });
      return { ...current, lines };
    });
  };

  const removeLine = (index: number) => {
    setConfirmDraft((current) => (current === null
      ? null
      : { ...current, lines: current.lines.filter((_, idx) => idx !== index) }));
  };

  return (
    <div
      className="modal-overlay budget-receipt-overlay"
      onClick={(event) => {
        if (event.target === event.currentTarget && actionBusy === null) onClose();
      }}
    >
      <div className="modal-card budget-receipt-drawer" role="dialog" aria-modal="true" aria-label={title}>
        <header className="budget-receipt-drawer-head">
          <h3>{title}</h3>
          <button type="button" disabled={actionBusy !== null} onClick={onClose}>
            {t("budget.main.close")}
          </button>
        </header>

        {detailState.kind === "loading" ? (
          <p className="budget-muted" role="status">{t("budget.receipts.detailLoading")}</p>
        ) : detailState.kind === "error" ? (
          <div className="budget-notice budget-entries-action" role="alert">
            <span>{t("budget.receipts.detailError")}</span>
            <button type="button" onClick={onRetryDetail}>{t("budget.receipts.retry")}</button>
          </div>
        ) : (() => {
          const { detail, candidates } = detailState;
          const currentCandidate = findCurrentLinkedCandidate(detail, candidates);
          const linkedTx = detail.linked && detail.linked_account_id !== null && detail.linked_entry_ref !== null
            ? txMap.get(txLookupKey(detail.linked_account_id, detail.linked_entry_ref)) ?? null
            : null;
          const keepBlocked = confirmDraft !== null
            && confirmDraft.linkChoice === "keep"
            && detail.linked
            && currentCandidate === null;
          const paymentAllowsLink = confirmDraft !== null
            && confirmDraft.paymentMethod !== "cash"
            && confirmDraft.paymentMethod !== "mixed";
          const lineDelta = confirmDraft === null ? null : computeLinesDelta(confirmDraft.lines, confirmDraft.totalAmount);

          return (
            <div className="budget-receipt-drawer-body">
              <div className="budget-receipt-hero">
                <div>
                  <h4 className="budget-receipt-hero-merchant">
                    <RedactedOrText
                      value={detail.raw_shop_name}
                      fallback={detail.forwarded_sender_domain ?? t("budget.receipts.unknownMerchant")}
                    />
                  </h4>
                  <div className="budget-entry-facts">
                    <span className="budget-receipt-source" data-source-kind={detail.source_kind}>
                      <SourceIcon kind={detail.source_kind} />
                      <span>{labelOf(RECEIPT_SOURCE_KIND_KEYS, detail.source_kind)}</span>
                    </span>
                    {detail.document_kind === null ? null : (
                      <span className="budget-chip">{labelOf(RECEIPT_DOC_KIND_KEYS, detail.document_kind)}</span>
                    )}
                    <span className="budget-chip budget-receipt-status" data-status={detail.status}>
                      {labelOf(RECEIPT_STATUS_KEYS, detail.status)}
                    </span>
                    <span className="budget-muted">{t("budget.receipts.version", { version: detail.version })}</span>
                  </div>
                </div>
                <strong className="budget-receipt-hero-total">{receiptTotalDisplay(detail, locale)}</strong>
              </div>

              {detail.forwarded_manually && detail.forwarded_sender_domain !== null ? (
                <p className="budget-muted">
                  {t("budget.receipts.forwardedManually", { domain: detail.forwarded_sender_domain })}
                </p>
              ) : null}

              <dl className="budget-receipt-meta">
                <dt>{t("budget.receipts.date")}</dt>
                <dd>{detail.receipt_date ?? t("budget.receipts.noDate")}</dd>
                {detail.receipt_time === null ? null : (
                  <>
                    <dt>{t("budget.receipts.time")}</dt>
                    <dd>{detail.receipt_time}</dd>
                  </>
                )}
                <dt>{t("budget.receipts.receiptKindLabel")}</dt>
                <dd>{labelOf(RECEIPT_KIND_KEYS, detail.receipt_kind)}</dd>
                <dt>{t("budget.receipts.paymentMethod")}</dt>
                <dd>
                  {detail.payment_method === null
                    ? t("budget.receipts.noPaymentMethod")
                    : labelOf(RECEIPT_PAYMENT_METHOD_KEYS, detail.payment_method)}
                </dd>
                {detail.raw_nip === null ? null : (
                  <>
                    <dt>{t("budget.receipts.nip")}</dt>
                    <dd>{detail.raw_nip}</dd>
                  </>
                )}
                {detail.invoice_number === null ? null : (
                  <>
                    <dt>{t("budget.receipts.invoiceNumber")}</dt>
                    <dd><RedactedOrText value={detail.invoice_number} /></dd>
                  </>
                )}
                {detail.due_date === null ? null : (
                  <>
                    <dt>{t("budget.receipts.dueDate")}</dt>
                    <dd>{detail.due_date}</dd>
                  </>
                )}
                {detail.math_delta !== null && detail.math_delta !== "0" && detail.currency !== null ? (
                  <>
                    <dt>{t("budget.receipts.mathDelta")}</dt>
                    <dd className="budget-warn">{formatAmount(detail.math_delta, detail.currency, locale)}</dd>
                  </>
                ) : null}
              </dl>

              <section className="budget-receipt-section" aria-label={t("budget.receipts.linkSection")}>
                <h5>{t("budget.receipts.linkSection")}</h5>
                {detail.linked ? (
                  <div className="budget-receipt-linked-tx">
                    <div className="budget-entry-facts">
                      <span className="budget-chip budget-receipt-link-badge" data-linked="true">
                        {t("budget.receipts.linkedBadge")}
                      </span>
                      <span>
                        {t("budget.receipts.linkedAccount", {
                          account: receiptAccountLabel(
                            accounts,
                            connections,
                            detail.linked_account_id,
                            t("budget.main.account"),
                          ),
                        })}
                      </span>
                    </div>
                    {txLoading ? (
                      <p className="budget-muted" role="status">{t("budget.receipts.loadingLinkedTx")}</p>
                    ) : linkedTx !== null ? (
                      <div className="budget-entry-main">
                        <span className="budget-muted">{linkedTx.date ?? t("budget.receipts.noDate")}</span>
                        <span className="budget-entry-text">
                          {linkedTx.counterparty || t("budget.receipts.unknownMerchant")}
                        </span>
                        <strong className={linkedTx.indicator === "DBIT" ? "is-debit" : "is-credit"}>
                          {formatAmount(linkedTx.amount, linkedTx.currency, locale, linkedTx.indicator)}
                        </strong>
                      </div>
                    ) : currentCandidate !== null ? (
                      <div className="budget-entry-main">
                        <span className="budget-muted">
                          {candidateDisplayDate(currentCandidate) ?? t("budget.receipts.noDate")}
                        </span>
                        <span className="budget-entry-text">{t("budget.receipts.linkedTxUnavailable")}</span>
                        <strong className={currentCandidate.credit_debit_indicator === "DBIT" ? "is-debit" : "is-credit"}>
                          {formatAmount(
                            currentCandidate.amount,
                            currentCandidate.currency,
                            locale,
                            currentCandidate.credit_debit_indicator,
                          )}
                        </strong>
                      </div>
                    ) : (
                      <p className="budget-muted">{t("budget.receipts.linkedTxUnavailable")}</p>
                    )}
                  </div>
                ) : (
                  <p className="budget-muted">{t("budget.receipts.unlinkedState")}</p>
                )}
              </section>

              {detail.image_available ? (
                <section className="budget-receipt-section" aria-label={t("budget.receipts.imageHeading")}>
                  <h5>{t("budget.receipts.imageHeading")}</h5>
                  {imageState.kind === "loading" ? (
                    <p className="budget-muted" role="status">{t("budget.receipts.imageLoading")}</p>
                  ) : imageState.kind === "ready" ? (
                    <img className="budget-receipt-image" src={imageState.url} alt={t("budget.receipts.imageAlt")} />
                  ) : (
                    <p className="budget-muted">{t("budget.receipts.imageUnavailable")}</p>
                  )}
                </section>
              ) : null}

              <section className="budget-receipt-section budget-receipt-lines" aria-label={t("budget.receipts.linesHeading", { count: detail.lines.length })}>
                <h5>{t("budget.receipts.linesHeading", { count: detail.lines.length })}</h5>
                {detail.lines.length === 0 ? (
                  <p className="budget-muted">{t("budget.receipts.noLines")}</p>
                ) : (
                  <ul className="budget-entry-list">
                    {detail.lines.map((line) => {
                      const lineAmount = detail.currency !== null
                        ? formatAmount(line.net_line_total, detail.currency, locale)
                        : line.net_line_total;
                      return (
                        <li key={line.line_no} className="budget-entry budget-receipt-line-row">
                          <div className="budget-receipt-line-main">
                            <span className="budget-entry-text budget-receipt-line-desc">
                              <RedactedOrText value={line.raw_name} />
                            </span>
                            <span className="budget-receipt-line-amount">{lineAmount}</span>
                          </div>
                          <div className="budget-entry-facts">
                            {line.line_kind === "item" ? null : (
                              <span className="budget-chip">{labelOf(RECEIPT_LINE_KIND_KEYS, line.line_kind)}</span>
                            )}
                            <span>
                              {line.quantity}
                              {line.raw_unit === null ? "" : (
                                <>{" "}<RedactedOrText value={line.raw_unit} /></>
                              )}
                              {line.unit_price_raw === null ? "" : ` × ${line.unit_price_raw}`}
                            </span>
                            {line.ptu_code === null ? null : (
                              <span>{t("budget.receipts.ptu", { code: line.ptu_code })}</span>
                            )}
                            {line.discount_amount !== "0" && detail.currency !== null ? (
                              <span>
                                {t("budget.receipts.discount", {
                                  amount: formatAmount(line.discount_amount, detail.currency, locale),
                                })}
                              </span>
                            ) : null}
                          </div>
                        </li>
                      );
                    })}
                  </ul>
                )}
              </section>

              {confirmDraft === null ? null : (
                <form
                  className="budget-receipt-section budget-receipt-confirm"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void submitConfirm(detail, candidates, confirmDraft);
                  }}
                >
                  <h5>{t("budget.receipts.confirmHeading")}</h5>
                  <div className="budget-receipt-form-grid">
                    <label>
                      {t("budget.receipts.merchant")}
                      <input
                        name="raw_shop_name"
                        type="text"
                        maxLength={128}
                        disabled={actionBusy !== null}
                        value={confirmDraft.rawShopName}
                        onChange={(event) => {
                          setConfirmDraft({ ...confirmDraft, rawShopName: event.currentTarget.value });
                        }}
                      />
                    </label>
                    <label>
                      {t("budget.receipts.nip")}
                      <input
                        name="raw_nip"
                        type="text"
                        maxLength={10}
                        disabled={actionBusy !== null}
                        value={confirmDraft.rawNip}
                        onChange={(event) => {
                          setConfirmDraft({ ...confirmDraft, rawNip: event.currentTarget.value });
                        }}
                      />
                    </label>
                    <label>
                      {t("budget.receipts.receiptKindLabel")}
                      <select
                        name="receipt_kind"
                        disabled={actionBusy !== null}
                        value={confirmDraft.receiptKind}
                        onChange={(event) => {
                          setConfirmDraft({
                            ...confirmDraft,
                            receiptKind: event.currentTarget.value as ReceiptConfirmDraft["receiptKind"],
                          });
                        }}
                      >
                        {RECEIPT_KINDS.map((item) => (
                          <option key={item} value={item}>{labelOf(RECEIPT_KIND_KEYS, item)}</option>
                        ))}
                      </select>
                    </label>
                    <label>
                      {t("budget.receipts.date")}
                      <input
                        name="receipt_date"
                        type="date"
                        required
                        disabled={actionBusy !== null}
                        value={confirmDraft.receiptDate}
                        onChange={(event) => {
                          setConfirmDraft({ ...confirmDraft, receiptDate: event.currentTarget.value });
                        }}
                      />
                    </label>
                    <label>
                      {t("budget.receipts.time")}
                      <input
                        name="receipt_time"
                        type="text"
                        placeholder={t("rule.phWindowStart")}
                        maxLength={5}
                        disabled={actionBusy !== null}
                        value={confirmDraft.receiptTime}
                        onChange={(event) => {
                          setConfirmDraft({ ...confirmDraft, receiptTime: event.currentTarget.value });
                        }}
                      />
                    </label>
                    <label>
                      {t("budget.receipts.total")}
                      <input
                        name="total_amount"
                        type="text"
                        inputMode="decimal"
                        required
                        disabled={actionBusy !== null}
                        value={confirmDraft.totalAmount}
                        onChange={(event) => {
                          setConfirmDraft({ ...confirmDraft, totalAmount: event.currentTarget.value });
                        }}
                      />
                    </label>
                    <label>
                      {t("budget.receipts.currency")}
                      <input
                        name="currency"
                        type="text"
                        maxLength={3}
                        required
                        disabled={actionBusy !== null}
                        value={confirmDraft.currency}
                        onChange={(event) => {
                          setConfirmDraft({ ...confirmDraft, currency: event.currentTarget.value.toUpperCase() });
                        }}
                      />
                    </label>
                    <label>
                      {t("budget.receipts.paymentMethod")}
                      <select
                        name="payment_method"
                        disabled={actionBusy !== null}
                        value={confirmDraft.paymentMethod}
                        onChange={(event) => {
                          const nextMethod = event.currentTarget.value as ReceiptPaymentMethod;
                          const disallowed = nextMethod === "cash" || nextMethod === "mixed";
                          setConfirmDraft({
                            ...confirmDraft,
                            paymentMethod: nextMethod,
                            linkChoice: disallowed ? "unlink" : confirmDraft.linkChoice,
                          });
                        }}
                      >
                        {RECEIPT_PAYMENT_METHODS.map((item) => (
                          <option key={item} value={item}>{labelOf(RECEIPT_PAYMENT_METHOD_KEYS, item)}</option>
                        ))}
                      </select>
                    </label>
                  </div>

                  <div className="budget-receipt-edit-lines">
                    <div className="budget-entries-head">
                      <span className="budget-muted">
                        {t("budget.receipts.linesHeading", { count: confirmDraft.lines.length })}
                      </span>
                      <div className="budget-actions">
                        {confirmDraft.lines.length === 0 ? (
                          <button
                            type="button"
                            disabled={actionBusy !== null}
                            onClick={() => { addSummaryLine(detail, confirmDraft); }}
                          >
                            {t("budget.receipts.addSummaryLine")}
                          </button>
                        ) : (
                          <button
                            type="button"
                            disabled={actionBusy !== null}
                            onClick={() => { addSummaryLine(detail, confirmDraft); }}
                          >
                            {t("budget.receipts.addLine")}
                          </button>
                        )}
                      </div>
                    </div>
                    {lineDelta !== null && lineDelta !== "0" ? (
                      <p className="budget-warn" role="note">{t("budget.receipts.totalMismatch")}</p>
                    ) : null}
                    {confirmDraft.lines.map((line, idx) => (
                      <div key={idx} className="budget-receipt-edit-line">
                        <label>
                          {t("budget.receipts.lineName")}
                          <input
                            type="text"
                            maxLength={128}
                            disabled={actionBusy !== null}
                            value={line.raw_name}
                            onChange={(event) => { updateLine(idx, { raw_name: event.currentTarget.value }); }}
                          />
                        </label>
                        <label>
                          {t("budget.receipts.lineAmount")}
                          <input
                            type="text"
                            inputMode="decimal"
                            disabled={actionBusy !== null}
                            value={line.net_line_total}
                            onChange={(event) => {
                              const value = event.currentTarget.value;
                              updateLine(idx, {
                                gross_line_total: value,
                                discount_amount: "0",
                                net_line_total: value,
                              });
                            }}
                          />
                        </label>
                        <button type="button" disabled={actionBusy !== null} onClick={() => { removeLine(idx); }}>
                          {t("budget.receipts.removeLine")}
                        </button>
                      </div>
                    ))}
                  </div>

                  <fieldset className="budget-receipt-link-choices">
                    <legend>{t("budget.receipts.linkChoiceHeading")}</legend>
                    {!paymentAllowsLink ? (
                      <p className="budget-muted">{t("budget.receipts.cashNoLinkNote")}</p>
                    ) : null}

                    {detail.linked ? (
                      <label className="budget-toggle">
                        <input
                          type="radio"
                          name="link_choice"
                          value="keep"
                          disabled={actionBusy !== null || !paymentAllowsLink}
                          checked={confirmDraft.linkChoice === "keep"}
                          onChange={() => { setConfirmDraft({ ...confirmDraft, linkChoice: "keep" }); }}
                        />
                        <span>
                          {currentCandidate !== null
                            ? t("budget.receipts.linkChoiceKeep")
                            : t("budget.receipts.linkChoiceKeepUnavailable")}
                        </span>
                      </label>
                    ) : null}

                    <label className="budget-toggle">
                      <input
                        type="radio"
                        name="link_choice"
                        value="unlink"
                        disabled={actionBusy !== null}
                        checked={confirmDraft.linkChoice === "unlink"}
                        onChange={() => { setConfirmDraft({ ...confirmDraft, linkChoice: "unlink" }); }}
                      />
                      <span>
                        {detail.linked ? t("budget.receipts.linkChoiceUnlink") : t("budget.receipts.linkChoiceNone")}
                      </span>
                    </label>

                    {paymentAllowsLink ? candidates.map((candidate, index) => {
                      if (currentCandidate !== null
                        && candidate.account_id === currentCandidate.account_id
                        && candidate.entry_reference === currentCandidate.entry_reference) {
                        return null;
                      }
                      const choiceValue = `candidate:${String(index)}`;
                      const resolved = txMap.get(txLookupKey(candidate.account_id, candidate.entry_reference)) ?? null;
                      const accountText = receiptAccountLabel(accounts, connections, candidate.account_id, t("budget.main.account"));
                      const dateText = candidateDisplayDate(candidate) ?? t("budget.receipts.noDate");
                      const amountText = formatAmount(
                        candidate.amount,
                        candidate.currency,
                        locale,
                        candidate.credit_debit_indicator,
                      );
                      const daysText = candidate.date_distance === 0
                        ? t("budget.receipts.candidateSameDay")
                        : t("budget.receipts.candidateDays", { days: candidate.date_distance });
                      return (
                        <label key={choiceValue} className="budget-toggle budget-receipt-candidate">
                          <input
                            type="radio"
                            name="link_choice"
                            value={choiceValue}
                            disabled={actionBusy !== null}
                            checked={confirmDraft.linkChoice === choiceValue}
                            onChange={() => { setConfirmDraft({ ...confirmDraft, linkChoice: choiceValue }); }}
                          />
                          <span>
                            {accountText} · {dateText} · {amountText}
                            {resolved !== null && resolved.counterparty !== "" ? ` · ${resolved.counterparty}` : ""}
                            {` (${daysText})`}
                            {candidate.merchant_token_hit ? ` · ${t("budget.receipts.candidateMerchantMatch")}` : ""}
                          </span>
                        </label>
                      );
                    }) : null}
                  </fieldset>

                  {actionError === null ? null : <p className="budget-notice" role="alert">{actionError}</p>}

                  <div className="budget-actions">
                    <button
                      type="submit"
                      className="budget-primary"
                      disabled={actionBusy !== null || keepBlocked}
                    >
                      {actionBusy === "confirm"
                        ? t("budget.receipts.confirming")
                        : t("budget.receipts.confirmAction")}
                    </button>
                    <button
                      type="button"
                      disabled={actionBusy !== null}
                      onClick={() => { setConfirmingDelete(true); setActionError(null); }}
                    >
                      {t("budget.receipts.deleteAction")}
                    </button>
                  </div>
                </form>
              )}

              {confirmingDelete ? (
                <div className="budget-notice budget-receipt-delete-dialog" role="alertdialog" aria-label={t("budget.receipts.deleteConfirmTitle")}>
                  <p><strong>{t("budget.receipts.deleteConfirmTitle")}</strong></p>
                  <p className="budget-muted">{t("budget.receipts.deleteConfirmBody")}</p>
                  <div className="budget-actions">
                    <button
                      type="button"
                      className="budget-primary"
                      disabled={actionBusy !== null}
                      onClick={() => { void submitDelete(detail); }}
                    >
                      {actionBusy === "delete"
                        ? t("budget.receipts.deleting")
                        : t("budget.receipts.deleteConfirmAction")}
                    </button>
                    <button
                      type="button"
                      disabled={actionBusy !== null}
                      onClick={() => { setConfirmingDelete(false); }}
                    >
                      {t("budget.main.cancel")}
                    </button>
                  </div>
                </div>
              ) : null}
            </div>
          );
        })()}
      </div>
    </div>
  );
}
