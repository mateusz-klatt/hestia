// Owner-private receipts and documents view ("Paragony i dowody"). Keeps all receipt data,
// image object URLs, and link metadata in React component memory only (never localStorage and
// never putting receipt fields into GET query strings).
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { t } from "../i18n";
import type { BudgetResult } from "./api";
import { labelOf } from "./analytics/model";
import { formatAmount } from "./model";
import type { BudgetAccount, BudgetConnection } from "./model";
import { receiptsApi as defaultReceiptsApi } from "./receipts_api";
import type { ReceiptsApi } from "./receipts_api";
import { ReceiptGroupController } from "./receipt_group_controller";
import type { ReceiptGroupControllerTarget } from "./receipt_group_controller";
import {
  EMPTY_RECEIPT_FILTER,
  RECEIPT_DOCUMENT_KINDS,
  RECEIPT_DOC_KIND_KEYS,
  RECEIPT_KINDS,
  RECEIPT_KIND_KEYS,
  RECEIPT_LINE_KIND_KEYS,
  RECEIPT_PAYMENT_METHODS,
  RECEIPT_PAYMENT_METHOD_KEYS,
  RECEIPT_SOURCE_KINDS,
  RECEIPT_SOURCE_KIND_KEYS,
  RECEIPT_STATUSES,
  RECEIPT_STATUS_KEYS,
  availableLinkOptions,
  buildBulkConfirmRequest,
  buildBulkGroupPlan,
  buildBulkSelectionPlan,
  buildCandidateSetKey,
  buildConfirmRequest,
  buildReceiptListRequest,
  candidateDisplayDate,
  candidateSelectionBinding,
  clearStaleCandidateSelection,
  computeLinesDelta,
  createPendingReceiptStore,
  editReceiptLine,
  filterReceiptRows,
  findCurrentLinkedCandidate,
  formatBulkErrorBucket,
  hasActiveReceiptFilter,
  hasClientSideFilter,
  hasMatchingFieldsChanged,
  isBulkConfirmable,
  isExcludedDraftForBulk,
  isGroupMemberEligible,
  isReceiptGrouped,
  isRedactedValue,
  isStaleCursorError,
  keepCandidateBinding,
  merchantBulkGroups,
  mergeReceiptRows,
  normalizeDecimalInput,
  preselectedCandidate,
  receiptAccountLabel,
  receiptHasRedactedLines,
  receiptMerchantGroupKey,
  reconcileLinkChoice,
  resolveDraftFromDetailSession,
  selectCandidateDraft,
  selectKeepDraft,
  suggestedCandidateDates,
  validReceiptDateRange,
} from "./receipts_model";
import type {
  PendingReceiptStore,
  ReceiptActionKind,
  ReceiptCandidate,
  ReceiptConfirmDraft,
  ReceiptDetail,
  ReceiptDetailResult,
  ReceiptDocumentKind,
  ReceiptFilter,
  ReceiptGroupCandidate,
  ReceiptGroupDetailResult,
  ReceiptLineEditField,
  ReceiptLineInput,
  ReceiptLinkExplicitChoice,
  ReceiptLinkGroupRef,
  ReceiptLinkSessionEntry,
  ReceiptPaymentMethod,
  ReceiptSettledEvent,
  ReceiptSourceKind,
  ReceiptStatus,
  ReceiptSummary,
} from "./receipts_model";

type AccessBarrier = (result: { ok: boolean; status?: number; error?: string }) => boolean;
type ReceiptAccessBarrier = (result: Parameters<AccessBarrier>[0], options?: { explicitDisabledOnly?: boolean }) => boolean;
type ReceiptGroupLink = ReceiptLinkGroupRef;
type ReceiptGroupDetail = ReceiptGroupDetailResult;

const fallbackPendingStores = new WeakMap<ReceiptsApi, PendingReceiptStore>();

function getFallbackPendingStore(api: ReceiptsApi): PendingReceiptStore {
  let store = fallbackPendingStores.get(api);
  if (store === undefined) {
    store = createPendingReceiptStore();
    fallbackPendingStores.set(api, store);
  }
  return store;
}

export interface ReceiptsViewProps {
  receiptsApi?: ReceiptsApi | undefined;
  pendingStore?: PendingReceiptStore | undefined;
  actorKey?: string | undefined;
  accounts: readonly BudgetAccount[];
  connections?: readonly BudgetConnection[] | undefined;
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
  | {
      kind: "ready";
      detail: ReceiptDetail;
      candidates: ReceiptCandidate[];
      groupCandidates: ReceiptGroupCandidate[];
      noBankData: boolean;
    }
  | { kind: "error" };

type ImageState =
  | { kind: "none" }
  | { kind: "loading" }
  | { kind: "ready"; url: string }
  | { kind: "unavailable" };

function isDisabledStatus(result: Parameters<AccessBarrier>[0], explicitDisabledOnly = false): boolean {
  if (result.ok || result.status !== 404) return false;
  if (explicitDisabledOnly) return result.error === "budget_receipts_disabled";
  return result.error !== "budget_receipt_not_found";
}

function revokeObjectUrl(url: string | null): void {
  if (url !== null && typeof URL.revokeObjectURL === "function") {
    URL.revokeObjectURL(url);
  }
}

function createObjectUrl(blob: Blob): string | null {
  return typeof URL.createObjectURL === "function" ? URL.createObjectURL(blob) : null;
}

function RedactedOrText({ value, fallback }: { value: string | null; fallback: string }) {
  if (value === null || value === "") return <>{fallback}</>;
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

function gatewayChips(receipt: Pick<ReceiptSummary, "gateway_status" | "has_document" | "payee_host">) {
  return (
    <>
      {receipt.gateway_status === "registered" ? (
        <span className="budget-chip budget-receipt-gateway" data-gateway-status="registered">
          {t("budget.receipts.gateway.registered")}
        </span>
      ) : null}
      {receipt.has_document ? null : (
        <span className="budget-chip budget-receipt-gateway" data-gateway-status="no_document">
          {receipt.payee_host === null
            ? t("budget.receipts.gateway.noDocumentNoHost")
            : t("budget.receipts.gateway.noDocument", { host: receipt.payee_host })}
        </span>
      )}
    </>
  );
}

function ReceiptGroupBadge({ group }: { group: ReceiptGroupLink }) {
  return (
    <span
      className="budget-chip budget-receipt-status"
      data-status={group.group_status}
      title={group.group_status === "needs_review" ? t("budget.receipts.statuses.needs_review") : undefined}
    >
      {t("budget.receipts.groupedBadge", { count: group.member_count })}
    </span>
  );
}

function confirmErrorText(error: string): string {
  switch (error) {
    case "budget_receipt_conflict": return t("budget.receipts.conflict");
    case "budget_receipt_tx_already_linked": return t("budget.receipts.txAlreadyLinked");
    case "budget_receipt_stale_source": return t("budget.receipts.staleSource");
    case "budget_receipt_total_mismatch": return t("budget.receipts.totalMismatch");
    case "budget_receipt_in_group": return t("budget.receipts.inGroupConflict");
    case "budget_receipt_not_found": return t("budget.receipts.notFound");
    default: return t("budget.receipts.confirmError");
  }
}

function pendingActionLabel(kind: ReceiptActionKind): string {
  switch (kind) {
    case "confirm": return t("budget.receipts.confirming");
    case "delete": return t("budget.receipts.deleting");
    case "group_link": return t("budget.receipts.linkingGroup");
    case "group_unlink": return t("budget.receipts.unlinkingGroup");
  }
}

function isConflictOrStaleError(result: { status?: number; error?: string }): boolean {
  return result.status === 409
    || result.error === "budget_receipt_conflict"
    || result.error === "budget_receipt_tx_already_linked"
    || result.error === "budget_receipt_stale_source";
}

function settledOutcome(
  result: { ok: boolean; status?: number; error?: string },
  successOutcome: "confirmed" | "deleted" | "group_unlinked",
): ReceiptSettledEvent["outcome"] {
  if (result.ok) return successOutcome;
  if (result.status === 404) return "not_found";
  if (isConflictOrStaleError(result)) return "conflict";
  return "error";
}

function acquireGroupParticipantLocks(
  store: PendingReceiptStore,
  action: "group_unlink" | "delete",
  receiptId: string,
  loadedGroup: ReceiptGroupDetail | null,
  expectedGroupId: string,
): Array<{ id: string; token: symbol }> | null {
  const ids = new Set<string>([receiptId]);
  if (loadedGroup !== null && loadedGroup.group.group_id === expectedGroupId) {
    for (const member of loadedGroup.members) ids.add(member.receipt_id);
    if (loadedGroup.evidence !== null) ids.add(loadedGroup.evidence.receipt_id);
  }
  const acquired: Array<{ id: string; token: symbol }> = [];
  for (const id of ids) {
    const token = store.tryAcquire(id, action);
    if (token === null) {
      for (const prev of acquired) store.release(prev.id, prev.token);
      return null;
    }
    acquired.push({ id, token });
  }
  return acquired;
}

function releaseGroupParticipantLocks(
  store: PendingReceiptStore,
  acquired: ReadonlyArray<{ id: string; token: symbol }>,
  outcome?: ReceiptSettledEvent["outcome"],
): void {
  for (const { id, token } of acquired) {
    store.release(id, token, outcome);
  }
}

export function ReceiptsView({
  receiptsApi = defaultReceiptsApi,
  pendingStore,
  actorKey = "",
  accounts,
  connections = [],
  locale,
  revision,
  onAccessLoss,
}: ReceiptsViewProps) {
  const store = pendingStore ?? getFallbackPendingStore(receiptsApi);

  const [draft, setDraft] = useState<ReceiptFilter>(EMPTY_RECEIPT_FILTER);
  const [filter, setFilter] = useState<ReceiptFilter>(EMPTY_RECEIPT_FILTER);
  const [listState, setListState] = useState<ListState>({ kind: "loading" });
  const [listRefresh, setListRefresh] = useState(0);
  const [disabledAtRevision, setDisabledAtRevision] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detailState, setDetailState] = useState<DetailState>({ kind: "loading" });
  const [detailRefresh, setDetailRefresh] = useState(0);
  const [imageState, setImageState] = useState<ImageState>({ kind: "none" });
  const [pendingMap, setPendingMap] = useState<ReadonlyMap<string, ReceiptActionKind>>(() => store.snapshot());
  const [selectedForBulk, setSelectedForBulk] = useState<ReadonlySet<string>>(() => new Set());
  const [groupControllerTarget, setGroupControllerTarget] = useState<ReceiptGroupControllerTarget | null>(null);
  const [groupControllerSession, setGroupControllerSession] = useState(0);
  const [bulkConfirmPlan, setBulkConfirmPlan] = useState<{
    targets: ReceiptSummary[];
    excludedNeedsReview: number;
    excludedDrafts: number;
    merchants: string[];
    linkById: boolean;
  } | null>(null);
  const [bulkProgress, setBulkProgress] = useState<{ total: number; done: number } | null>(null);
  const [bulkReport, setBulkReport] = useState<{
    confirmed: number;
    failed: number;
    remaining: number;
    cancelled: boolean;
    excludedNeedsReview: number;
    excludedDrafts: number;
    errors: [string, number][];
  } | null>(null);

  const mounted = useRef(true);
  const listGeneration = useRef(0);
  const detailGeneration = useRef(0);
  const imageGeneration = useRef(0);
  const receiptFloorGenRef = useRef<Map<string, number>>(new Map());
  const receiptLinkMemoryRef = useRef<Map<string, ReceiptLinkSessionEntry>>(new Map());
  const openReceiptIdRef = useRef<string | null>(null);
  const receiptDrawerTriggerRef = useRef<HTMLButtonElement | null>(null);
  const loadedReceiptIdRef = useRef<string | null>(null);
  const latestReadyCandidatesRef = useRef<{ receiptId: string; candidates: readonly ReceiptCandidate[] } | null>(null);
  const staleCursorReloadedRef = useRef(false);
  const activeImageUrl = useRef<string | null>(null);
  const bulkCancelledRef = useRef(false);
  const bulkRunGenRef = useRef(0);
  const actorKeyRef = useRef(actorKey);
  const groupReturnFocusRef = useRef<HTMLButtonElement | null>(null);

  useLayoutEffect(() => {
    openReceiptIdRef.current = selectedId;
  }, [selectedId]);

  const clearImage = useCallback(() => {
    imageGeneration.current += 1;
    revokeObjectUrl(activeImageUrl.current);
    activeImageUrl.current = null;
    setImageState({ kind: "none" });
  }, []);

  const resetDrawer = useCallback(() => {
    detailGeneration.current += 1;
    openReceiptIdRef.current = null;
    receiptDrawerTriggerRef.current = null;
    loadedReceiptIdRef.current = null;
    latestReadyCandidatesRef.current = null;
    clearImage();
    setSelectedId(null);
    setDetailState({ kind: "loading" });
  }, [clearImage]);

  const reloadList = useCallback(() => {
    staleCursorReloadedRef.current = false;
    setListRefresh((current) => current + 1);
  }, []);

  const clearAllReceiptsState = useCallback((disabled: boolean, disabledRevision: number | null = null) => {
    listGeneration.current += 1;
    bulkCancelledRef.current = true;
    bulkRunGenRef.current += 1;
    receiptFloorGenRef.current.clear();
    receiptLinkMemoryRef.current.clear();
    staleCursorReloadedRef.current = false;
    resetDrawer();
    setGroupControllerTarget(null);
    setGroupControllerSession((current) => current + 1);
    groupReturnFocusRef.current = null;
    setSelectedForBulk(new Set());
    setBulkConfirmPlan(null);
    setBulkProgress(null);
    setDraft(EMPTY_RECEIPT_FILTER);
    setFilter(EMPTY_RECEIPT_FILTER);
    setNotice(null);
    setDisabledAtRevision(disabled ? disabledRevision : null);
    setListState(disabled ? { kind: "disabled" } : { kind: "error" });
  }, [resetDrawer]);

  useLayoutEffect(() => {
    if (actorKeyRef.current !== actorKey) {
      actorKeyRef.current = actorKey;
      clearAllReceiptsState(false);
      setBulkReport(null);
    }
  }, [actorKey, clearAllReceiptsState]);

  const checkBarrier = useCallback<ReceiptAccessBarrier>((result, options = {}) => {
    // Propagate lost access BEFORE checking !mounted.current so unmounting onto Accounts never drops 401/403.
    if (onAccessLoss(result)) {
      if (mounted.current) clearAllReceiptsState(false);
      return true;
    }
    if (isDisabledStatus(result, options.explicitDisabledOnly === true)) {
      if (mounted.current) clearAllReceiptsState(true, revision);
      return true;
    }
    return false;
  }, [onAccessLoss, clearAllReceiptsState, revision]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      listGeneration.current += 1;
      detailGeneration.current += 1;
      imageGeneration.current += 1;
      bulkCancelledRef.current = true;
      bulkRunGenRef.current += 1;
      openReceiptIdRef.current = null;
      revokeObjectUrl(activeImageUrl.current);
      activeImageUrl.current = null;
    };
  }, []);

  const runBulkConfirm = useCallback(async (plan: {
    targets: ReceiptSummary[];
    excludedNeedsReview: number;
    excludedDrafts: number;
    merchants: string[];
    linkById: boolean;
  }) => {
    const runToken = ++bulkRunGenRef.current;
    const expectedActor = actorKey;
    const isBulkCancelled = () => bulkCancelledRef.current;
    const isGateLocked = () => receiptsApi.isAuthorizationLocked?.() === true;
    const isSessionAlive = () =>
      mounted.current && bulkRunGenRef.current === runToken && actorKeyRef.current === expectedActor;
    bulkCancelledRef.current = false;
    setBulkConfirmPlan(null);
    setBulkReport(null);
    setNotice(null);
    setBulkProgress({ total: plan.targets.length, done: 0 });

    let confirmed = 0;
    let failed = 0;
    let processed = 0;
    let cancelled = false;
    let aborted = false;
    const errors = new Map<string, number>();

    const recordError = (bucket: string) => {
      failed += 1;
      errors.set(bucket, (errors.get(bucket) ?? 0) + 1);
    };
    const isAuthAbort = (res: { ok: boolean; status?: number; error?: string }): boolean => {
      if (checkBarrier(res)) return true;
      return !res.ok && (
        res.status === 401
        || res.status === 403
        || res.error === "budget_forbidden"
        || res.error === "budget_stale_epoch"
        || res.error === "budget_actor_changed"
        || res.error === "budget_server_changed"
      );
    };

    for (const target of plan.targets) {
      if (!isSessionAlive() || isGateLocked()) {
        aborted = true;
        break;
      }
      if (isBulkCancelled()) {
        cancelled = true;
        break;
      }
      const lockToken = store.tryAcquire(target.receipt_id, "confirm");
      if (lockToken === null) {
        recordError(formatBulkErrorBucket(409, "budget_receipt_in_flight"));
        processed += 1;
        setBulkProgress({ total: plan.targets.length, done: processed });
        continue;
      }
      let bulkOutcome: ReceiptSettledEvent["outcome"] | undefined;
      try {
        const detailRes = await receiptsApi.receiptDetail(target.receipt_id);
        if (isAuthAbort(detailRes)) {
          if (!detailRes.ok) {
            recordError(formatBulkErrorBucket(detailRes.status, detailRes.error));
            processed += 1;
          }
          aborted = true;
          break;
        }
        if (!isSessionAlive() || isGateLocked()) {
          aborted = true;
          break;
        }
        if (isBulkCancelled()) {
          cancelled = true;
          break;
        }
        if (!detailRes.ok) {
          recordError(formatBulkErrorBucket(detailRes.status, detailRes.error));
          processed += 1;
          setBulkProgress({ total: plan.targets.length, done: processed });
          continue;
        }
        const built = detailRes.data.receipt.receipt_id !== target.receipt_id
          ? { ok: false as const, errorCode: "budget_receipt_conflict" }
          : buildBulkConfirmRequest(
              detailRes.data.receipt,
              detailRes.data.candidates ?? [],
              t("budget.receipts.summaryLineDefault"),
              { linkById: plan.linkById },
            );
        if (!built.ok) {
          recordError(formatBulkErrorBucket(undefined, built.errorCode));
          processed += 1;
          setBulkProgress({ total: plan.targets.length, done: processed });
          if (isBulkCancelled()) {
            cancelled = true;
            break;
          }
          continue;
        }
        if (!isSessionAlive() || isGateLocked()) {
          aborted = true;
          break;
        }
        if (isBulkCancelled()) {
          cancelled = true;
          break;
        }
        const confirmRes = await receiptsApi.confirmReceipt(built.request);
        bulkOutcome = settledOutcome(confirmRes, "confirmed");
        if (isAuthAbort(confirmRes)) {
          if (!confirmRes.ok) {
            recordError(formatBulkErrorBucket(confirmRes.status, confirmRes.error));
            processed += 1;
          }
          aborted = true;
          break;
        }
        if (!isSessionAlive()) {
          aborted = true;
          break;
        }
        if (confirmRes.ok) {
          confirmed += 1;
          setSelectedForBulk((current) => {
            if (!current.has(target.receipt_id)) return current;
            const next = new Set(current);
            next.delete(target.receipt_id);
            return next;
          });
        } else {
          recordError(formatBulkErrorBucket(confirmRes.status, confirmRes.error));
        }
        processed += 1;
        setBulkProgress({ total: plan.targets.length, done: processed });
        if (isBulkCancelled()) {
          cancelled = true;
          break;
        }
      } finally {
        store.release(target.receipt_id, lockToken, isSessionAlive() ? undefined : bulkOutcome);
      }
    }

    if (!isSessionAlive()) return;
    setBulkProgress(null);
    setBulkReport({
      confirmed,
      failed,
      remaining: Math.max(0, plan.targets.length - processed),
      cancelled: cancelled || aborted,
      excludedNeedsReview: plan.excludedNeedsReview,
      excludedDrafts: plan.excludedDrafts,
      errors: [...errors.entries()],
    });
    if (!aborted) {
      reloadList();
    }
  }, [actorKey, checkBarrier, receiptsApi, reloadList, store]);

  // Subscribe to persistent store so locks survive unmount/remount and late settlements refresh the panel.
  useEffect(() => {
    setPendingMap(store.snapshot());
    return store.subscribe((event?: ReceiptSettledEvent) => {
      if (!mounted.current) return;
      setPendingMap(store.snapshot());
      if (event !== undefined) {
        // Reload list after every settled mutation (including error) in case a server write committed before response loss.
        reloadList();
        if (openReceiptIdRef.current === event.receiptId) {
          receiptFloorGenRef.current.set(event.receiptId, ++detailGeneration.current);
          if (event.outcome === "deleted" || event.outcome === "not_found") {
            resetDrawer();
            if (event.outcome === "not_found") setNotice(t("budget.receipts.notFound"));
          } else {
            setDetailRefresh((current) => current + 1);
          }
        }
      }
    });
  }, [store, reloadList, resetDrawer]);

  const searchMode = filter.query.trim() !== "";

  useEffect(() => {
    if (disabledAtRevision === revision) {
      setListState({ kind: "disabled" });
      return;
    }
    const token = ++listGeneration.current;
    setListState({ kind: "loading" });
    const request = searchMode
      ? receiptsApi.receiptsSearch({ query: filter.query.trim(), limit: 50 })
      : receiptsApi.receiptsList(buildReceiptListRequest(filter));
    void request.then((result) => {
      if (checkBarrier(result) || !mounted.current || token !== listGeneration.current) return;
      if (!result.ok) {
        setListState({ kind: "error" });
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
  }, [receiptsApi, filter, searchMode, revision, listRefresh, disabledAtRevision, checkBarrier]);

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
      if (checkBarrier(result) || !mounted.current || token !== listGeneration.current) return;
      if (!result.ok) {
        if (isStaleCursorError(result) && !staleCursorReloadedRef.current) {
          staleCursorReloadedRef.current = true;
          setNotice(t("budget.receipts.cursorExpiredNotice"));
          setListRefresh((current) => current + 1);
          return;
        }
        setListState((current) => (current.kind === "ready"
          ? {
              ...current,
              loadingMore: false,
              loadMoreError: true,
              nextCursor: isStaleCursorError(result) ? null : current.nextCursor,
            }
          : current));
        return;
      }
      staleCursorReloadedRef.current = false;
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

  useEffect(() => {
    if (selectedId === null) {
      detailGeneration.current += 1;
      loadedReceiptIdRef.current = null;
      latestReadyCandidatesRef.current = null;
      clearImage();
      setDetailState({ kind: "loading" });
      return;
    }
    const targetId = selectedId;
    const token = ++detailGeneration.current;
    if (loadedReceiptIdRef.current !== targetId) {
      latestReadyCandidatesRef.current = null;
      setDetailState({ kind: "loading" });
      clearImage();
    }
    void receiptsApi.receiptDetail(targetId).then((result: BudgetResult<ReceiptDetailResult>) => {
      const floorToken = receiptFloorGenRef.current.get(targetId) ?? 0;
      if (checkBarrier(result) || !mounted.current || token !== detailGeneration.current
        || token <= floorToken || openReceiptIdRef.current !== targetId) return;
      if (!result.ok) {
        if (result.status === 404) {
          resetDrawer();
          setNotice(t("budget.receipts.notFound"));
          staleCursorReloadedRef.current = false;
          setListRefresh((current) => current + 1);
          return;
        }
        loadedReceiptIdRef.current = null;
        latestReadyCandidatesRef.current = null;
        setDetailState({ kind: "error" });
        return;
      }
      const receipt = result.data.receipt;
      const candidates = result.data.candidates ?? [];
      const groupCandidates = result.data.group_candidates ?? [];
      loadedReceiptIdRef.current = receipt.receipt_id;
      latestReadyCandidatesRef.current = { receiptId: receipt.receipt_id, candidates };
      setDetailState({
        kind: "ready",
        detail: receipt,
        candidates,
        groupCandidates,
        noBankData: result.data.no_bank_data === true,
      });
      if (receipt.image_available) {
        const imgToken = ++imageGeneration.current;
        setImageState({ kind: "loading" });
        void receiptsApi.receiptImage(receipt.receipt_id).then((imgResult) => {
          const latestFloor = receiptFloorGenRef.current.get(targetId) ?? 0;
          if (checkBarrier(imgResult, { explicitDisabledOnly: true }) || !mounted.current
            || imgToken !== imageGeneration.current || token !== detailGeneration.current
            || token <= latestFloor || openReceiptIdRef.current !== targetId) return;
          if (!imgResult.ok) {
            setImageState(imgResult.status === 404 ? { kind: "none" } : { kind: "unavailable" });
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
  }, [receiptsApi, selectedId, revision, detailRefresh, checkBarrier, clearImage, resetDrawer]);

  const drawerActionBusy: ReceiptActionKind | null =
    selectedId === null ? null : (pendingMap.get(selectedId) ?? null);

  const getLatestCandidates = useCallback((receiptId: string): readonly ReceiptCandidate[] | null => (
    latestReadyCandidatesRef.current?.receiptId === receiptId ? latestReadyCandidatesRef.current.candidates : null
  ), []);

  const openReceipt = useCallback((receiptId: string, trigger?: HTMLButtonElement) => {
    if (bulkProgress !== null
      || groupControllerTarget !== null
      || (openReceiptIdRef.current !== null && store.get(openReceiptIdRef.current) !== null)
      || openReceiptIdRef.current === receiptId) return;
    if (openReceiptIdRef.current === null) receiptDrawerTriggerRef.current = trigger ?? null;
    openReceiptIdRef.current = receiptId;
    loadedReceiptIdRef.current = null;
    latestReadyCandidatesRef.current = null;
    setDetailState({ kind: "loading" });
    setNotice(null);
    setSelectedId(receiptId);
  }, [store, bulkProgress, groupControllerTarget]);

  const closeDetail = useCallback(() => {
    if (groupControllerTarget !== null
      || (openReceiptIdRef.current !== null && store.get(openReceiptIdRef.current) !== null)) return;
    const trigger = receiptDrawerTriggerRef.current;
    resetDrawer();
    trigger?.focus();
  }, [store, resetDrawer, groupControllerTarget]);

  const resolveDetailDraft = useCallback((
    detail: ReceiptDetail,
    candidates: readonly ReceiptCandidate[],
  ): { draft: ReceiptConfirmDraft; showSuggestedBanner: boolean; keepEvidenceInvalidated: boolean } => {
    const key = `${actorKey}:${detail.receipt_id}`;
    const resolved = resolveDraftFromDetailSession(
      detail,
      candidates,
      receiptLinkMemoryRef.current.get(key),
    );
    receiptLinkMemoryRef.current.set(key, resolved.nextSessionEntry);
    return {
      draft: resolved.draft,
      showSuggestedBanner: resolved.showSuggestedBanner,
      keepEvidenceInvalidated: resolved.keepEvidenceInvalidated,
    };
  }, [actorKey]);

  const recordLinkChoiceTouched = useCallback((
    receiptId: string,
    choice: ReceiptLinkExplicitChoice,
    detail?: Pick<ReceiptDetail, "version" | "status" | "linked">,
  ) => {
    const key = `${actorKey}:${receiptId}`;
    const prev = receiptLinkMemoryRef.current.get(key);
    receiptLinkMemoryRef.current.set(key, {
      touched: true,
      choice,
      baselineVersion: prev?.baselineVersion ?? detail?.version ?? 0,
      baselineStatus: prev?.baselineStatus ?? detail?.status ?? "draft",
      baselineLinked: prev?.baselineLinked ?? detail?.linked ?? false,
      baselineCandidateKeys: prev?.baselineCandidateKeys ?? [],
      candidateSetChanged: prev?.candidateSetChanged ?? false,
    });
  }, [actorKey]);

  const applyFilter = (next: ReceiptFilter) => {
    if (bulkProgress !== null || !validReceiptDateRange(next)) return;
    staleCursorReloadedRef.current = false;
    setNotice(null);
    setFilter(next);
  };

  const clearFilters = () => {
    if (bulkProgress !== null) return;
    staleCursorReloadedRef.current = false;
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

  const shownRows = listState.kind === "ready" ? filterReceiptRows(listState.rows, filter, searchMode) : [];
  const clientFiltered = hasClientSideFilter(filter);
  const formatWithNip = (merchant: string | null, nip: string): string => (
    merchant === null
      ? `NIP ${nip}`
      : t("budget.receipts.merchantWithNip", { merchant, nip })
  );
  const confirmableRows = shownRows.filter(isBulkConfirmable);
  const shownNeedsReviewCount = shownRows.filter((row) => row.status === "needs_review").length;
  const shownExcludedDraftsCount = shownRows.filter(isExcludedDraftForBulk).length;
  const allConfirmableSelected = confirmableRows.length > 0
    && confirmableRows.every((row) => selectedForBulk.has(row.receipt_id));
  const selectionPlan = buildBulkSelectionPlan(
    shownRows,
    selectedForBulk,
    t("budget.receipts.unknownMerchant"),
    formatWithNip,
  );
  const merchantGroups = merchantBulkGroups(shownRows, formatWithNip);
  const visibleMerchantActions = merchantGroups.length === 1
    ? merchantGroups
    : merchantGroups.filter((group) => group.confirmableIds.length >= 2 || merchantGroups.length <= 3);
  const groupWritesAvailable = receiptsApi.receiptGroupCandidates !== undefined && receiptsApi.linkReceiptGroup !== undefined;
  const bulkGroupPlan = buildBulkGroupPlan(shownRows, selectedForBulk);
  const anyPending = pendingMap.size > 0;
  const bulkBusy = bulkProgress !== null || drawerActionBusy !== null || groupControllerTarget !== null;
  const bulkControlsDisabled = bulkBusy || anyPending;

  const formatMerchantsLine = (merchants: readonly string[]): string => {
    const shown = merchants.slice(0, 5);
    const rest = merchants.length - shown.length;
    const base = shown.join(", ");
    return rest > 0 ? `${base} (${t("budget.receipts.bulkConfirmMoreMerchants", { count: rest })})` : base;
  };

  const toggleSelectAllLoaded = () => {
    setSelectedForBulk((current) => {
      const next = new Set(current);
      if (allConfirmableSelected) {
        for (const row of confirmableRows) next.delete(row.receipt_id);
      } else {
        for (const row of confirmableRows) next.add(row.receipt_id);
      }
      return next;
    });
  };

  const toggleRowSelection = (receiptId: string) => {
    setSelectedForBulk((current) => {
      const next = new Set(current);
      if (next.has(receiptId)) {
        next.delete(receiptId);
      } else {
        next.add(receiptId);
      }
      return next;
    });
  };

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

      {bulkProgress === null ? null : (
        <div className="budget-notice budget-entries-action" role="status">
          <span>{t("budget.receipts.bulkProgress", { done: bulkProgress.done, total: bulkProgress.total })}</span>
          <button
            type="button"
            onClick={() => { bulkCancelledRef.current = true; }}
          >
            {t("budget.main.cancel")}
          </button>
        </div>
      )}

      {bulkReport === null ? null : (
        <div
          className="budget-notice budget-receipt-bulk-dialog"
          role={bulkReport.failed > 0 ? "alert" : "status"}
        >
          <p>
            {bulkReport.cancelled
              ? t("budget.receipts.bulkCancelledSummary", {
                  confirmed: bulkReport.confirmed,
                  remaining: bulkReport.remaining,
                  failed: bulkReport.failed,
                })
              : t("budget.receipts.bulkDoneSummary", {
                  confirmed: bulkReport.confirmed,
                  failed: bulkReport.failed,
                })}
          </p>
          {bulkReport.excludedNeedsReview > 0 ? (
            <p className="budget-muted">{t("budget.receipts.bulkExcludedReview", { count: bulkReport.excludedNeedsReview })}</p>
          ) : null}
          {bulkReport.excludedDrafts > 0 ? (
            <p className="budget-muted">{t("budget.receipts.bulkExcludedDrafts", { count: bulkReport.excludedDrafts })}</p>
          ) : null}
          {bulkReport.errors.length === 0 ? null : (
            <ul className="budget-entry-facts">
              {bulkReport.errors.map(([code, count]) => (
                <li key={code}>{t("budget.receipts.bulkErrorEntry", { code, count })}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {bulkConfirmPlan === null ? null : (
        <div
          className="budget-notice budget-receipt-bulk-dialog"
          role="alertdialog"
          aria-label={t(
            bulkConfirmPlan.linkById ? "budget.receipts.bulkConfirmWithIdTitle" : "budget.receipts.bulkConfirmTitle",
            { count: bulkConfirmPlan.targets.length },
          )}
        >
          <p>
            <strong>
              {t(
                bulkConfirmPlan.linkById ? "budget.receipts.bulkConfirmWithIdTitle" : "budget.receipts.bulkConfirmTitle",
                { count: bulkConfirmPlan.targets.length },
              )}
            </strong>
          </p>
          <p className="budget-muted">
            {t("budget.receipts.bulkConfirmMerchants", { merchants: formatMerchantsLine(bulkConfirmPlan.merchants) })}
          </p>
          {bulkConfirmPlan.excludedNeedsReview > 0 ? (
            <p className="budget-warn" role="note">
              {t("budget.receipts.bulkExcludedReview", { count: bulkConfirmPlan.excludedNeedsReview })}
            </p>
          ) : null}
          {bulkConfirmPlan.excludedDrafts > 0 ? (
            <p className="budget-warn" role="note">
              {t("budget.receipts.bulkExcludedDrafts", { count: bulkConfirmPlan.excludedDrafts })}
            </p>
          ) : null}
          <label className="budget-toggle">
            <input
              type="checkbox"
              name="bulk_link_by_id"
              checked={bulkConfirmPlan.linkById}
              disabled={bulkBusy}
              onChange={(event) => {
                const checked = event.currentTarget.checked;
                setBulkConfirmPlan((current) => current === null ? null : { ...current, linkById: checked });
              }}
            />
            <span>{t("budget.receipts.bulkConfirmWithIdLinkOption")}</span>
          </label>
          {bulkConfirmPlan.linkById ? null : (
            <p className="budget-muted">{t("budget.receipts.linkChoiceNone")}</p>
          )}
          <div className="budget-actions">
            <button
              type="button"
              className="budget-primary"
              disabled={bulkBusy}
              onClick={() => { void runBulkConfirm(bulkConfirmPlan); }}
            >
              {t("budget.receipts.bulkConfirmAction")}
            </button>
            <button
              type="button"
              onClick={() => { setBulkConfirmPlan(null); }}
            >
              {t("budget.main.cancel")}
            </button>
          </div>
        </div>
      )}

      {listState.kind === "loading" ? <p className="budget-muted" role="status">{t("budget.receipts.loading")}</p> : null}

      {listState.kind === "error" ? (
        <div className="budget-notice budget-entries-action" role="alert">
          <span>{t("budget.receipts.loadError")}</span>
          <button
            type="button"
            onClick={() => {
              staleCursorReloadedRef.current = false;
              setListRefresh((current) => current + 1);
            }}
          >
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
              <div className="budget-receipt-bulk-bar" role="group" aria-label={t("budget.receipts.confirmSelectedZero")}>
                <label className="budget-toggle">
                  <input
                    type="checkbox"
                    name="select_all_loaded"
                    checked={allConfirmableSelected}
                    disabled={bulkControlsDisabled || confirmableRows.length === 0}
                    onChange={toggleSelectAllLoaded}
                  />
                  <span>{t("budget.receipts.selectAllLoaded")}</span>
                </label>
                <button
                  type="button"
                  className="budget-primary"
                  disabled={bulkControlsDisabled || selectionPlan.confirmable.length === 0}
                  onClick={() => {
                    setBulkConfirmPlan({
                      targets: selectionPlan.confirmable,
                      excludedNeedsReview: selectionPlan.excludedNeedsReview,
                      excludedDrafts: selectionPlan.excludedDrafts,
                      merchants: selectionPlan.merchants,
                      linkById: false,
                    });
                  }}
                >
                  {selectionPlan.confirmable.length > 0
                    ? t("budget.receipts.confirmSelected", { count: selectionPlan.confirmable.length })
                    : t("budget.receipts.confirmSelectedZero")}
                </button>
                {selectedForBulk.size > 0 ? (
                  <button
                    type="button"
                    disabled={bulkControlsDisabled}
                    onClick={() => { setSelectedForBulk(new Set()); }}
                  >
                    {t("budget.receipts.clearSelection")}
                  </button>
                ) : null}
                {groupWritesAvailable ? (
                  <button
                    type="button"
                    disabled={bulkControlsDisabled || !bulkGroupPlan.eligible}
                    onClick={(event) => {
                      const anchor = bulkGroupPlan.selected[0];
                      if (!bulkGroupPlan.eligible || anchor === undefined) return;
                      groupReturnFocusRef.current = event.currentTarget;
                      setGroupControllerSession((current) => current + 1);
                      setGroupControllerTarget({ kind: "bulk", anchor, selectedRows: bulkGroupPlan.selected });
                    }}
                  >
                    {t("budget.receipts.bulkLinkGroupAction")}
                  </button>
                ) : null}
                {visibleMerchantActions.map((group) => (
                  <button
                    key={group.key}
                    type="button"
                    disabled={bulkControlsDisabled}
                    onClick={() => {
                      const groupSet = new Set(group.confirmableIds);
                      setBulkConfirmPlan({
                        targets: shownRows.filter((row) => groupSet.has(row.receipt_id)),
                        excludedNeedsReview: group.excludedNeedsReview,
                        excludedDrafts: group.excludedDrafts,
                        merchants: [group.merchant],
                        linkById: false,
                      });
                    }}
                  >
                    {t("budget.receipts.confirmAllMerchant", { merchant: group.merchant })}
                  </button>
                ))}
              </div>
              {groupWritesAvailable && selectedForBulk.size >= 2 && !bulkGroupPlan.eligible ? (
                <p className="budget-muted" role="note">{t("budget.receipts.bulkLinkGroupHint")}</p>
              ) : null}
              {shownNeedsReviewCount > 0 ? (
                <p className="budget-muted" role="note">
                  {t("budget.receipts.bulkExcludedReview", { count: shownNeedsReviewCount })}
                </p>
              ) : null}
              {shownExcludedDraftsCount > 0 ? (
                <p className="budget-muted" role="note">
                  {t("budget.receipts.bulkExcludedDrafts", { count: shownExcludedDraftsCount })}
                </p>
              ) : null}
              <p className="budget-muted">{t("budget.receipts.shownCount", { count: shownRows.length })}</p>
              <ul className="budget-entry-list" aria-label={title}>
                {shownRows.map((row) => {
                  const rowPending = pendingMap.get(row.receipt_id) ?? null;
                  const rowConfirmable = isBulkConfirmable(row);
                  const rowSelectable = rowConfirmable || (groupWritesAvailable && isGroupMemberEligible(row));
                  const rowMerchantText = receiptMerchantGroupKey(row, formatWithNip) ?? t("budget.receipts.unknownMerchant");
                  return (
                    <li key={row.receipt_id} className="budget-entry budget-receipt-row">
                      <div className="budget-receipt-row-main">
                        <input
                          type="checkbox"
                          className="budget-receipt-row-check"
                          name={`select_receipt_${row.receipt_id}`}
                          aria-label={t("budget.receipts.selectRow", { merchant: rowMerchantText })}
                          checked={rowSelectable && selectedForBulk.has(row.receipt_id)}
                          disabled={bulkControlsDisabled || !rowSelectable}
                          onChange={() => { toggleRowSelection(row.receipt_id); }}
                        />
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
                        {gatewayChips(row)}
                        {row.receipt_kind === "refund" ? (
                          <span className="budget-chip">{t("budget.receipts.receiptKind.refund")}</span>
                        ) : null}
                        <span className="budget-chip budget-receipt-status" data-status={row.status}>
                          {labelOf(RECEIPT_STATUS_KEYS, row.status)}
                        </span>
                        {row.link_group == null ? (
                          <span className="budget-chip budget-receipt-link-badge" data-linked={row.linked ? "true" : "false"}>
                            {row.linked ? t("budget.receipts.linkedBadge") : t("budget.receipts.unlinkedBadge")}
                          </span>
                        ) : <ReceiptGroupBadge group={row.link_group} />}
                        {rowPending === null ? null : (
                          <span className="budget-chip budget-muted" role="status">
                            {pendingActionLabel(rowPending)}
                          </span>
                        )}
                        <button
                          type="button"
                          disabled={bulkBusy || (anyPending && rowPending === null)}
                          onClick={(event) => { openReceipt(row.receipt_id, event.currentTarget); }}
                        >
                          {t("budget.receipts.open")}
                        </button>
                      </div>
                    </li>
                  );
                })}
              </ul>
            </>
          )}
          {clientFiltered ? (
            <p className="budget-muted" role="note">{t("budget.receipts.clientFilterHint")}</p>
          ) : null}
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
          key={`${actorKey}:${selectedId}`}
          api={receiptsApi}
          store={store}
          accounts={accounts}
          connections={connections}
          locale={locale}
          detailState={detailState}
          imageState={imageState}
          actionBusy={drawerActionBusy}
          groupControllerOpen={groupControllerTarget !== null}
          isOpenReceipt={(receiptId) => openReceiptIdRef.current === receiptId}
          getLatestCandidates={getLatestCandidates}
          resolveDetailDraft={resolveDetailDraft}
          onLinkChoiceTouched={recordLinkChoiceTouched}
          onAccessLoss={checkBarrier}
          onClose={closeDetail}
          onOpenReceipt={openReceipt}
          onOpenGroupCandidates={(detail, trigger) => {
            if (bulkBusy) return;
            groupReturnFocusRef.current = trigger;
            setGroupControllerSession((current) => current + 1);
            setGroupControllerTarget({ kind: "candidates", anchor: detail });
          }}
          onReviewGroupProposal={(detail, proposal, trigger) => {
            if (bulkBusy) return;
            groupReturnFocusRef.current = trigger;
            setGroupControllerSession((current) => current + 1);
            setGroupControllerTarget({ kind: "proposal", anchor: detail, proposal });
          }}
          onRetryDetail={() => { setDetailRefresh((current) => current + 1); }}
          onConfirmed={(updated) => {
            receiptLinkMemoryRef.current.delete(`${actorKey}:${updated.receipt_id}`);
            const floorToken = ++detailGeneration.current;
            receiptFloorGenRef.current.set(updated.receipt_id, floorToken);
            latestReadyCandidatesRef.current = { receiptId: updated.receipt_id, candidates: [] };
            setDetailState({ kind: "ready", detail: updated, candidates: [], groupCandidates: [], noBankData: false });
            setNotice(t("budget.receipts.confirmSuccess"));
            reloadList();
          }}
          onGroupUnlinked={(receiptId) => {
            receiptLinkMemoryRef.current.delete(`${actorKey}:${receiptId}`);
            const floorToken = ++detailGeneration.current;
            receiptFloorGenRef.current.set(receiptId, floorToken);
            setNotice(t("budget.receipts.groupUnlinkSuccess"));
            setDetailRefresh((current) => current + 1);
            reloadList();
          }}
          onConflictOrStale={(receiptId) => {
            recordLinkChoiceTouched(receiptId, { kind: "empty" });
            const floorToken = ++detailGeneration.current;
            receiptFloorGenRef.current.set(receiptId, floorToken);
            setDetailRefresh((current) => current + 1);
            reloadList();
          }}
          onNotFound={(receiptId) => {
            receiptLinkMemoryRef.current.delete(`${actorKey}:${receiptId}`);
            const floorToken = ++detailGeneration.current;
            receiptFloorGenRef.current.set(receiptId, floorToken);
            resetDrawer();
            setNotice(t("budget.receipts.notFound"));
            reloadList();
          }}
          onDeleted={(receiptId, dissolvedGroup = false) => {
            receiptLinkMemoryRef.current.delete(`${actorKey}:${receiptId}`);
            const floorToken = ++detailGeneration.current;
            receiptFloorGenRef.current.set(receiptId, floorToken);
            resetDrawer();
            if (dissolvedGroup) setNotice(t("budget.receipts.dissolveDeleteSuccess"));
            reloadList();
          }}
          onMutationError={reloadList}
        />
      )}

      {groupControllerTarget === null ? null : (
        <ReceiptGroupController
          key={`${actorKey}:${String(groupControllerSession)}`}
          api={receiptsApi}
          store={store}
          target={groupControllerTarget}
          actorKey={actorKey}
          accounts={accounts}
          connections={connections}
          locale={locale}
          revision={revision}
          onAccessLoss={checkBarrier}
          onClose={() => {
            setGroupControllerTarget(null);
            const opener = groupReturnFocusRef.current;
            groupReturnFocusRef.current = null;
            if (opener?.isConnected === true) opener.focus();
          }}
          onPartialConfirm={(affectedIds) => {
            reloadList();
            const openId = openReceiptIdRef.current;
            if (openId !== null && affectedIds.includes(openId)) {
              receiptFloorGenRef.current.set(openId, ++detailGeneration.current);
              setDetailRefresh((current) => current + 1);
            }
          }}
          onGroupLinked={(_result, affectedIds) => {
            setGroupControllerTarget(null);
            setSelectedForBulk((current) => {
              const next = new Set(current);
              for (const id of affectedIds) next.delete(id);
              return next;
            });
            setNotice(t("budget.receipts.groupLinkSuccess"));
            reloadList();
            const openId = openReceiptIdRef.current;
            if (openId !== null && affectedIds.includes(openId)) {
              receiptLinkMemoryRef.current.delete(`${actorKey}:${openId}`);
              receiptFloorGenRef.current.set(openId, ++detailGeneration.current);
              setDetailRefresh((current) => current + 1);
            }
            const opener = groupReturnFocusRef.current;
            groupReturnFocusRef.current = null;
            if (opener?.isConnected === true) opener.focus();
          }}
        />
      )}
    </section>
  );
}

interface DrawerProps {
  api: Pick<
    ReceiptsApi,
    | "confirmReceipt"
    | "deleteReceipt"
    | "fetchReceiptLinkGroupDetail"
    | "receiptGroupCandidates"
    | "linkReceiptGroup"
    | "unlinkReceiptGroup"
    | "isAuthorizationLocked"
  >;
  store: PendingReceiptStore;
  accounts: readonly BudgetAccount[];
  connections: readonly BudgetConnection[];
  locale: string;
  detailState: DetailState;
  imageState: ImageState;
  actionBusy: ReceiptActionKind | null;
  groupControllerOpen: boolean;
  isOpenReceipt: (receiptId: string) => boolean;
  getLatestCandidates: (receiptId: string) => readonly ReceiptCandidate[] | null;
  resolveDetailDraft: (
    detail: ReceiptDetail,
    candidates: readonly ReceiptCandidate[],
  ) => { draft: ReceiptConfirmDraft; showSuggestedBanner: boolean; keepEvidenceInvalidated: boolean };
  onLinkChoiceTouched: (receiptId: string, choice: ReceiptLinkExplicitChoice) => void;
  onAccessLoss: ReceiptAccessBarrier;
  onClose: () => void;
  onOpenReceipt: (receiptId: string) => void;
  onOpenGroupCandidates: (detail: ReceiptDetail, trigger: HTMLButtonElement | null) => void;
  onReviewGroupProposal: (detail: ReceiptDetail, proposal: ReceiptGroupCandidate, trigger: HTMLButtonElement | null) => void;
  onRetryDetail: () => void;
  onConfirmed: (receipt: ReceiptDetail) => void;
  onGroupUnlinked: (receiptId: string) => void;
  onConflictOrStale: (receiptId: string) => void;
  onNotFound: (receiptId: string) => void;
  onDeleted: (receiptId: string, dissolvedGroup?: boolean) => void;
  onMutationError: () => void;
}

function ReceiptGroupDocumentRow({ receipt, current, locale, onOpenReceipt }: {
  receipt: ReceiptSummary;
  current: boolean;
  locale: string;
  onOpenReceipt: (receiptId: string) => void;
}) {
  const name = (
    <RedactedOrText
      value={receipt.raw_shop_name}
      fallback={receipt.gateway ?? receipt.forwarded_sender_domain ?? t("budget.receipts.unknownMerchant")}
    />
  );
  return (
    <li className="budget-entry" aria-current={current ? "true" : undefined}>
      <div className="budget-entry-main">
        <span className="budget-muted">{receipt.receipt_date ?? t("budget.receipts.noDate")}</span>
        <span className="budget-entry-text">
          {current ? name : (
            <button type="button" className="budget-link" onClick={() => { onOpenReceipt(receipt.receipt_id); }}>
              {name}
            </button>
          )}
        </span>
        <strong>{receiptTotalDisplay(receipt, locale)}</strong>
      </div>
      {receipt.gateway !== null && receipt.raw_shop_name && receipt.raw_shop_name !== receipt.gateway ? (
        <span className="budget-muted">{receipt.gateway}</span>
      ) : null}
    </li>
  );
}

function ReceiptGroupSection({
  api,
  group,
  receipt,
  accounts,
  connections,
  locale,
  actionBusy,
  confirmingDissolve,
  actionError,
  onOpenReceipt,
  onAccessLoss,
  onGroupDetailLoaded,
  onUnlinkGroup,
  onStartDissolveDelete,
  onConfirmDissolveDelete,
  onCancelDissolveDelete,
}: {
  api: Pick<ReceiptsApi, "fetchReceiptLinkGroupDetail" | "unlinkReceiptGroup">;
  group: ReceiptGroupLink;
  receipt: ReceiptDetail;
  accounts: readonly BudgetAccount[];
  connections: readonly BudgetConnection[];
  locale: string;
  actionBusy: ReceiptActionKind | null;
  confirmingDissolve: boolean;
  actionError: string | null;
  onOpenReceipt: (receiptId: string) => void;
  onAccessLoss: ReceiptAccessBarrier;
  onGroupDetailLoaded: (detail: ReceiptGroupDetail) => void;
  onUnlinkGroup: (groupVersion: number) => void;
  onStartDissolveDelete: () => void;
  onConfirmDissolveDelete: (groupVersion: number) => void;
  onCancelDissolveDelete: () => void;
}) {
  const { group_id: groupId } = group;
  const [state, setState] = useState<
    { kind: "loading" } | { kind: "error" } | { kind: "ready"; detail: ReceiptGroupDetail }
  >({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (api.fetchReceiptLinkGroupDetail === undefined) return;
    let active = true;
    setState({ kind: "loading" });
    void api.fetchReceiptLinkGroupDetail(groupId).then((result) => {
      if (onAccessLoss(result, { explicitDisabledOnly: true }) || !active) return;
      if (result.ok && result.data.group.group_id === groupId) {
        onGroupDetailLoaded(result.data);
        setState({ kind: "ready", detail: result.data });
      } else {
        setState({ kind: "error" });
      }
    });
    return () => { active = false; };
  }, [api, groupId, onAccessLoss, onGroupDetailLoaded, attempt]);

  const title = t("budget.receipts.linkedGroupSection");
  const groupDetail = state.kind === "ready" ? state.detail : null;
  const currentGroup = groupDetail?.group ?? group;
  const accountId = groupDetail === null ? receipt.linked_account_id : groupDetail.linked_account_id;
  const currency = receipt.currency ?? groupDetail?.members[0]?.currency ?? groupDetail?.evidence?.currency ?? null;
  const itemProps = { locale, onOpenReceipt };

  return (
    <section className="budget-receipt-section" aria-label={title}>
      <h5>{title}</h5>
      <div className="budget-entry-facts">
        <ReceiptGroupBadge group={currentGroup} />
        {currentGroup.group_status === "needs_review" ? (
          <span className="budget-warn">{t("budget.receipts.statuses.needs_review")}</span>
        ) : null}
        {accountId === null ? null : (
          <span>{t("budget.receipts.linkedAccount", {
            account: receiptAccountLabel(accounts, connections, accountId, t("budget.main.account")),
          })}</span>
        )}
        {currentGroup.fee_amount === null ? null : (
          <span>{t("budget.receipts.paymentEvidenceFee", {
            amount: currency === null ? currentGroup.fee_amount : formatAmount(currentGroup.fee_amount, currency, locale),
          })}</span>
        )}
      </div>
      {currentGroup.group_status === "needs_review" ? (
        <p className="budget-notice" role="note">{t("budget.receipts.groupNeedsReviewNote")}</p>
      ) : null}
      {api.fetchReceiptLinkGroupDetail === undefined ? (
        <p className="budget-muted">{t("budget.receipts.detailError")}</p>
      ) : state.kind === "loading" ? (
        <p className="budget-muted" role="status">{t("budget.receipts.detailLoading")}</p>
      ) : state.kind === "error" ? (
        <div className="budget-notice budget-entries-action" role="alert">
          <span>{t("budget.receipts.detailError")}</span>
          <button type="button" onClick={() => { setAttempt((current) => current + 1); }}>
            {t("budget.receipts.retry")}
          </button>
        </div>
      ) : (
        <>
          <ul className="budget-entry-list" aria-label={t("budget.receipts.groupMembers")}>
            {state.detail.members.map((member) => (
              <ReceiptGroupDocumentRow key={member.receipt_id} receipt={member}
                current={member.receipt_id === receipt.receipt_id} {...itemProps} />
            ))}
          </ul>
          {state.detail.evidence === null ? null : (
            <>
              <strong>{t("budget.receipts.paymentEvidence")}</strong>
              <ul className="budget-entry-list" aria-label={t("budget.receipts.paymentEvidence")}>
                <ReceiptGroupDocumentRow receipt={state.detail.evidence}
                  current={state.detail.evidence.receipt_id === receipt.receipt_id} {...itemProps} />
              </ul>
            </>
          )}
        </>
      )}

      {actionError === null ? null : <p className="budget-notice" role="alert">{actionError}</p>}

      <div className="budget-actions">
        {api.unlinkReceiptGroup === undefined ? null : (
          <button
            type="button"
            disabled={actionBusy !== null}
            onClick={() => { onUnlinkGroup(currentGroup.group_version); }}
          >
            {actionBusy === "group_unlink"
              ? t("budget.receipts.unlinkingGroup")
              : t("budget.receipts.unlinkGroupAction")}
          </button>
        )}
        <button
          type="button"
          disabled={actionBusy !== null}
          onClick={onStartDissolveDelete}
        >
          {t("budget.receipts.dissolveDeleteAction")}
        </button>
      </div>

      {confirmingDissolve ? (
        <div className="budget-notice budget-receipt-delete-dialog" role="alertdialog" aria-label={t("budget.receipts.dissolveDeleteTitle")}>
          <p><strong>{t("budget.receipts.dissolveDeleteTitle")}</strong></p>
          <p className="budget-muted">{t("budget.receipts.dissolveDeleteBody")}</p>
          <div className="budget-actions">
            <button
              type="button"
              className="budget-primary"
              disabled={actionBusy !== null}
              onClick={() => { onConfirmDissolveDelete(currentGroup.group_version); }}
            >
              {actionBusy === "delete"
                ? t("budget.receipts.deleting")
                : t("budget.receipts.dissolveDeleteConfirm")}
            </button>
            <button
              type="button"
              disabled={actionBusy !== null}
              onClick={onCancelDissolveDelete}
            >
              {t("budget.main.cancel")}
            </button>
          </div>
        </div>
      ) : null}
    </section>
  );
}

function ReceiptDetailDrawer({
  api,
  store,
  accounts,
  connections,
  locale,
  detailState,
  imageState,
  actionBusy,
  groupControllerOpen,
  isOpenReceipt,
  getLatestCandidates,
  resolveDetailDraft,
  onLinkChoiceTouched,
  onAccessLoss,
  onClose,
  onOpenReceipt,
  onOpenGroupCandidates,
  onReviewGroupProposal,
  onRetryDetail,
  onConfirmed,
  onGroupUnlinked,
  onConflictOrStale,
  onNotFound,
  onDeleted,
  onMutationError,
}: DrawerProps) {
  const [confirmDraft, setConfirmDraft] = useState<ReceiptConfirmDraft | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [confirmingDissolve, setConfirmingDissolve] = useState(false);
  const [suggestionDismissed, setSuggestionDismissed] = useState(false);
  const keepActionErrorRef = useRef(false);
  const drawerMountedRef = useRef(true);
  const groupDetailRef = useRef<ReceiptGroupDetail | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const linkChoicesRef = useRef<HTMLFieldSetElement | null>(null);

  const handleGroupDetailLoaded = useCallback((loaded: ReceiptGroupDetail) => {
    groupDetailRef.current = loaded;
  }, []);

  useLayoutEffect(() => { closeButtonRef.current?.focus(); }, []);

  useEffect(() => {
    drawerMountedRef.current = true;
    return () => { drawerMountedRef.current = false; };
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && actionBusy === null && !groupControllerOpen) onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => { document.removeEventListener("keydown", onKeyDown); };
  }, [onClose, actionBusy, groupControllerOpen]);

  useEffect(() => {
    if (detailState.kind === "ready" && !isReceiptGrouped(detailState.detail)) {
      groupDetailRef.current = null;
      const resolved = resolveDetailDraft(detailState.detail, detailState.candidates);
      setConfirmDraft(resolved.draft);
      setSuggestionDismissed(!resolved.showSuggestedBanner);
      if (keepActionErrorRef.current) {
        keepActionErrorRef.current = false;
      } else if (resolved.keepEvidenceInvalidated) {
        setActionError(t("budget.receipts.bankEvidenceChanged"));
      } else {
        setActionError(null);
      }
      setConfirmingDelete(false);
      setConfirmingDissolve(false);
    } else if (detailState.kind === "ready") {
      if (groupDetailRef.current?.group.group_id !== detailState.detail.link_group?.group_id) {
        groupDetailRef.current = null;
      }
      setSuggestionDismissed(false);
      setConfirmDraft(null);
      if (keepActionErrorRef.current) {
        keepActionErrorRef.current = false;
      } else {
        setActionError(null);
      }
      setConfirmingDelete(false);
      setConfirmingDissolve(false);
    } else if (detailState.kind === "loading") {
      setSuggestionDismissed(false);
      setConfirmDraft(null);
      if (!keepActionErrorRef.current) setActionError(null);
      setConfirmingDelete(false);
      setConfirmingDissolve(false);
    } else {
      groupDetailRef.current = null;
      setSuggestionDismissed(false);
      keepActionErrorRef.current = false;
      setConfirmDraft(null);
      setActionError(null);
      setConfirmingDelete(false);
      setConfirmingDissolve(false);
    }
  }, [detailState, resolveDetailDraft]);

  const title = t("budget.receipts.detailTitle");

  const updateDraftFields = (
    detail: ReceiptDetail,
    candidates: readonly ReceiptCandidate[],
    patch: Partial<ReceiptConfirmDraft>,
  ) => {
    setConfirmDraft((current) => {
      if (current === null) return null;
      const next = { ...current, ...patch };
      const nextLinkChoice = reconcileLinkChoice(detail, current.linkChoice, next, candidates);
      return {
        ...next,
        linkChoice: nextLinkChoice,
        selectedCandidate: nextLinkChoice.startsWith("candidate:") ? current.selectedCandidate : null,
        keepBinding: nextLinkChoice === "keep" ? current.keepBinding : null,
      };
    });
  };

  const submitConfirm = async (detail: ReceiptDetail, candidates: readonly ReceiptCandidate[], draft: ReceiptConfirmDraft) => {
    if (isReceiptGrouped(detail)) return;
    const currentCandidates = getLatestCandidates(detail.receipt_id) ?? candidates;
    const built = buildConfirmRequest(detail, draft, currentCandidates, t("budget.receipts.summaryLineDefault"));
    if (!built.ok) {
      if (built.errorKey === "budget.receipts.candidateListChanged" || built.errorKey === "budget.receipts.bankEvidenceChanged") {
        keepActionErrorRef.current = true;
        if (built.errorKey === "budget.receipts.candidateListChanged") {
          setSuggestionDismissed(true);
        } else {
          onLinkChoiceTouched(detail.receipt_id, { kind: "empty" });
        }
        setConfirmDraft((current) => (current === null
          ? null
          : { ...current, linkChoice: "", selectedCandidate: null, keepBinding: null }));
      }
      setActionError(t(built.errorKey));
      return;
    }
    if (!isOpenReceipt(detail.receipt_id)) return;
    const lockToken = store.tryAcquire(detail.receipt_id, "confirm");
    if (lockToken === null) return;
    keepActionErrorRef.current = false;
    setActionError(null);
    let outcome: ReceiptSettledEvent["outcome"] = "error";
    let handledInMountedDrawer = false;
    try {
      const result = await api.confirmReceipt(built.request);
      outcome = settledOutcome(result, "confirmed");
      const stillOpen = drawerMountedRef.current && isOpenReceipt(detail.receipt_id);
      if (onAccessLoss(result) || !stillOpen) return;
      handledInMountedDrawer = true;
      if (!result.ok) {
        if (result.status === 404) {
          onNotFound(detail.receipt_id);
          return;
        }
        setActionError(confirmErrorText(result.error));
        if (isConflictOrStaleError(result)) {
          keepActionErrorRef.current = true;
          onConflictOrStale(detail.receipt_id);
        } else {
          // Reload list even on network/unknown errors in case the write committed server-side.
          onMutationError();
        }
        return;
      }
      onConfirmed(result.data.receipt);
    } finally {
      store.release(detail.receipt_id, lockToken, handledInMountedDrawer ? undefined : outcome);
    }
  };

  const submitDelete = async (detail: ReceiptDetail) => {
    if (isReceiptGrouped(detail) || !isOpenReceipt(detail.receipt_id)) return;
    const lockToken = store.tryAcquire(detail.receipt_id, "delete");
    if (lockToken === null) return;
    keepActionErrorRef.current = false;
    setActionError(null);
    let outcome: ReceiptSettledEvent["outcome"] = "error";
    let handledInMountedDrawer = false;
    try {
      const result = await api.deleteReceipt({ receipt_id: detail.receipt_id, expected_version: detail.version });
      outcome = settledOutcome(result, "deleted");
      const stillOpen = drawerMountedRef.current && isOpenReceipt(detail.receipt_id);
      if (onAccessLoss(result) || !stillOpen) return;
      handledInMountedDrawer = true;
      if (!result.ok) {
        if (result.status === 404) {
          onNotFound(detail.receipt_id);
          return;
        }
        setActionError(result.error === "budget_receipt_in_group"
          ? t("budget.receipts.inGroupConflict")
          : isConflictOrStaleError(result)
            ? t("budget.receipts.conflict")
            : t("budget.receipts.deleteError"));
        if (isConflictOrStaleError(result)) {
          keepActionErrorRef.current = true;
          onConflictOrStale(detail.receipt_id);
        } else {
          // Reload list even on network/unknown errors in case the delete committed server-side.
          onMutationError();
        }
        return;
      }
      onDeleted(detail.receipt_id, false);
    } finally {
      store.release(detail.receipt_id, lockToken, handledInMountedDrawer ? undefined : outcome);
    }
  };

  const submitUnlinkGroup = async (detail: ReceiptDetail, groupVersion: number) => {
    const group = detail.link_group;
    if (group == null || api.unlinkReceiptGroup === undefined || !isOpenReceipt(detail.receipt_id)) return;
    if (api.isAuthorizationLocked?.() === true) {
      setActionError(t("budget.main.serverChanged"));
      return;
    }
    const acquired = acquireGroupParticipantLocks(
      store,
      "group_unlink",
      detail.receipt_id,
      groupDetailRef.current,
      group.group_id,
    );
    if (acquired === null) {
      setActionError(t("budget.receipts.groupBusyError"));
      return;
    }
    keepActionErrorRef.current = false;
    setActionError(null);
    let outcome: ReceiptSettledEvent["outcome"] = "error";
    let handledInMountedDrawer = false;
    try {
      const result = await api.unlinkReceiptGroup({
        group_id: group.group_id,
        expected_version: groupVersion,
      });
      outcome = settledOutcome(result, "group_unlinked");
      const stillOpen = drawerMountedRef.current && isOpenReceipt(detail.receipt_id);
      if (onAccessLoss(result, { explicitDisabledOnly: true }) || !stillOpen) return;
      handledInMountedDrawer = true;
      if (!result.ok) {
        if (result.status === 404 || isConflictOrStaleError(result)) {
          setActionError(t("budget.receipts.conflict"));
          keepActionErrorRef.current = true;
          onConflictOrStale(detail.receipt_id);
          return;
        }
        setActionError(t("budget.receipts.groupUnlinkError"));
        onMutationError();
        return;
      }
      setConfirmingDissolve(false);
      onGroupUnlinked(detail.receipt_id);
    } finally {
      releaseGroupParticipantLocks(store, acquired, handledInMountedDrawer ? undefined : outcome);
    }
  };

  const submitDissolveDelete = async (detail: ReceiptDetail, groupVersion: number) => {
    const group = detail.link_group;
    if (group == null || !isOpenReceipt(detail.receipt_id)) return;
    if (api.isAuthorizationLocked?.() === true) {
      setActionError(t("budget.main.serverChanged"));
      return;
    }
    const acquired = acquireGroupParticipantLocks(
      store,
      "delete",
      detail.receipt_id,
      groupDetailRef.current,
      group.group_id,
    );
    if (acquired === null) {
      setActionError(t("budget.receipts.groupBusyError"));
      return;
    }
    keepActionErrorRef.current = false;
    setActionError(null);
    let outcome: ReceiptSettledEvent["outcome"] = "error";
    let handledInMountedDrawer = false;
    try {
      const result = await api.deleteReceipt({
        receipt_id: detail.receipt_id,
        expected_version: detail.version,
        dissolve_group: true,
        expected_group_version: groupVersion,
      });
      outcome = settledOutcome(result, "deleted");
      const stillOpen = drawerMountedRef.current && isOpenReceipt(detail.receipt_id);
      if (onAccessLoss(result) || !stillOpen) return;
      handledInMountedDrawer = true;
      if (!result.ok) {
        if (result.status === 404) {
          onNotFound(detail.receipt_id);
          return;
        }
        setActionError(isConflictOrStaleError(result)
          ? t("budget.receipts.conflict")
          : t("budget.receipts.deleteError"));
        if (isConflictOrStaleError(result)) {
          setConfirmingDissolve(false);
          keepActionErrorRef.current = true;
          onConflictOrStale(detail.receipt_id);
        } else {
          onMutationError();
        }
        return;
      }
      onDeleted(detail.receipt_id, true);
    } finally {
      releaseGroupParticipantLocks(store, acquired, handledInMountedDrawer ? undefined : outcome);
    }
  };

  const addSummaryLine = (detail: ReceiptDetail, draft: ReceiptConfirmDraft) => {
    if (receiptHasRedactedLines(detail.lines)) return;
    const normalizedTotal = normalizeDecimalInput(draft.totalAmount) ?? draft.totalAmount.trim();
    const defaultName = detail.invoice_number !== null && !isRedactedValue(detail.invoice_number)
      ? detail.invoice_number
      : (draft.rawShopName.trim() !== "" && !isRedactedValue(draft.rawShopName.trim())
        ? draft.rawShopName.trim()
        : t("budget.receipts.summaryLineDefault"));
    const safeName = isRedactedValue(defaultName) ? "Document" : defaultName;
    const nextLine: ReceiptLineInput = {
      line_kind: "other",
      raw_name: safeName,
      ptu_code: null,
      quantity: "1",
      raw_unit: null,
      unit_price_raw: null,
      gross_line_total: normalizedTotal === "" ? "0" : normalizedTotal,
      discount_amount: "0",
      net_line_total: normalizedTotal === "" ? "0" : normalizedTotal,
    };
    setConfirmDraft({ ...draft, lines: [...draft.lines, nextLine] });
  };

  const updateLine = (detail: ReceiptDetail, index: number, field: ReceiptLineEditField, value: string) => {
    if (receiptHasRedactedLines(detail.lines)) return;
    setConfirmDraft((current) => {
      if (current === null) return null;
      return {
        ...current,
        lines: current.lines.map((line, idx) => (idx === index ? editReceiptLine(line, field, value) : line)),
      };
    });
  };

  const removeLine = (detail: ReceiptDetail, index: number) => {
    if (receiptHasRedactedLines(detail.lines)) return;
    setConfirmDraft((current) => (current === null
      ? null
      : { ...current, lines: current.lines.filter((_, idx) => idx !== index) }));
  };

  return (
    <div
      className="modal-overlay budget-receipt-overlay"
      onClick={(event) => {
        if (event.target === event.currentTarget && actionBusy === null && !groupControllerOpen) onClose();
      }}
    >
      <div className="modal-card budget-receipt-drawer" role="dialog" aria-modal="true" aria-label={title}>
        <header className="budget-receipt-drawer-head">
          <h3>{title}</h3>
          <button ref={closeButtonRef} type="button" disabled={actionBusy !== null} onClick={onClose}>
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
          const { detail, candidates, groupCandidates } = detailState;
          const group = detail.link_group;
          const currentCandidateSetKey = buildCandidateSetKey(detail, candidates);
          const storedKeepCandidate = findCurrentLinkedCandidate(detail, candidates);
          const activeDraft = group != null || confirmDraft === null
            ? null
            : clearStaleCandidateSelection(confirmDraft, detail.receipt_id, currentCandidateSetKey, storedKeepCandidate);
          const hasRedactedLines = receiptHasRedactedLines(detail.lines);
          const draftMethod: ReceiptPaymentMethod = activeDraft?.paymentMethod ?? "unknown";
          const matchingChanged = activeDraft !== null && hasMatchingFieldsChanged(detail, activeDraft);
          const linkOptions = availableLinkOptions(detail, candidates, draftMethod, matchingChanged);
          const { currentCandidate } = linkOptions;
          const hasStoredKeepCandidate = storedKeepCandidate !== null;
          const lineDelta = activeDraft === null
            || (detail.document_kind === "invoice" && detail.lines.length === 0 && activeDraft.lines.length === 0)
            ? null
            : computeLinesDelta(activeDraft.lines, activeDraft.totalAmount);
          const deltaCurrency = (activeDraft?.currency.trim() || detail.currency) ?? "PLN";
          const lineDeltaDisplay = lineDelta !== null && lineDelta !== "0"
            ? formatAmount(lineDelta, deltaCurrency, locale)
            : null;
          const showKeepRadio = linkOptions.canKeep || (hasStoredKeepCandidate && detail.status !== "confirmed");
          const showCandidateRadios = detail.status !== "confirmed" && candidates.length > 0;
          const tokensDisabledByEdit = matchingChanged && (showKeepRadio || showCandidateRadios || detail.linked);
          const disableTokens = actionBusy !== null || !linkOptions.paymentAllowsLink || matchingChanged;
          const strongSingleCandidate = preselectedCandidate(detail, candidates, draftMethod);
          const suggestedCandidate = activeDraft === null || suggestionDismissed || !linkOptions.canPickCandidates
            ? null
            : strongSingleCandidate;
          const bannerCandidate = activeDraft?.linkChoice === "candidate:0" ? suggestedCandidate : null;
          const canGroupWrites = api.receiptGroupCandidates !== undefined && api.linkReceiptGroup !== undefined;
          const canGroupReceipt = group == null && canGroupWrites && isGroupMemberEligible(detail);
          const groupProposal = canGroupReceipt && linkOptions.paymentAllowsLink && strongSingleCandidate === null
            ? (groupCandidates[0] ?? null)
            : null;

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
                    {gatewayChips(detail)}
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

              {bannerCandidate === null ? null : ((candidate) => {
                const accountText = receiptAccountLabel(
                  accounts,
                  connections,
                  candidate.account_id,
                  t("budget.main.account"),
                );
                const { primaryDate, bookingDayMonth, displayDistance } = suggestedCandidateDates(candidate, detail);
                const baseDate = primaryDate ?? t("budget.receipts.noDate");
                const dateText = bookingDayMonth === null
                  ? baseDate
                  : `${baseDate} ${t("budget.receipts.bookingDateNote", { date: bookingDayMonth })}`;
                const daysText = displayDistance === 0
                  ? t("budget.receipts.candidateSameDay")
                  : t("budget.receipts.candidateDays", { days: displayDistance });
                const amountText = formatAmount(
                  candidate.amount,
                  candidate.currency,
                  locale,
                  candidate.credit_debit_indicator,
                );
                const strengthBadge = candidate.match_strength === "id"
                  ? t("budget.receipts.matchBadgeId")
                  : candidate.match_strength === "nip"
                    ? t("budget.receipts.matchBadgeNip")
                    : t("budget.receipts.matchBadgeMerchant");
                return (
                  <section
                    className="budget-notice budget-receipt-suggested"
                    aria-label={t("budget.receipts.suggestedMatchHeading")}
                  >
                    <div className="budget-receipt-suggested-head">
                      <strong>{t("budget.receipts.suggestedMatchHeading")}</strong>
                      <span
                        className="budget-chip budget-receipt-match-badge"
                        data-match-strength={candidate.match_strength}
                      >
                        {strengthBadge}
                      </span>
                    </div>
                    <div className="budget-entry-main">
                      <span className="budget-muted">{dateText}</span>
                      <span className="budget-entry-text">
                        {accountText}
                        {` (${daysText})`}
                      </span>
                      <strong className={candidate.credit_debit_indicator === "DBIT" ? "is-debit" : "is-credit"}>
                        {amountText}
                      </strong>
                    </div>
                    <div className="budget-actions">
                      <button
                        type="button"
                        className="budget-link"
                        disabled={actionBusy !== null}
                        onClick={() => {
                          onLinkChoiceTouched(detail.receipt_id, { kind: "empty" });
                          setSuggestionDismissed(true);
                          setConfirmDraft((current) => (
                            current === null
                              ? null
                              : { ...current, linkChoice: "", selectedCandidate: null, keepBinding: null }
                          ));
                          linkChoicesRef.current?.focus();
                          const target: { scrollIntoView?: (options?: ScrollIntoViewOptions) => void } | null =
                            linkChoicesRef.current;
                          target?.scrollIntoView?.({ block: "nearest" });
                        }}
                      >
                        {t("budget.receipts.suggestedMatchChange")}
                      </button>
                      <button
                        type="button"
                        disabled={actionBusy !== null}
                        onClick={() => {
                          onLinkChoiceTouched(detail.receipt_id, { kind: "unlink" });
                          setSuggestionDismissed(true);
                          setConfirmDraft((current) => (
                            current === null
                              ? null
                              : { ...current, linkChoice: "unlink", selectedCandidate: null, keepBinding: null }
                          ));
                        }}
                      >
                        {t("budget.receipts.suggestedMatchUnlink")}
                      </button>
                    </div>
                  </section>
                );
              })(bannerCandidate)}

              {groupProposal === null ? null : ((proposal) => {
                const accountText = receiptAccountLabel(
                  accounts,
                  connections,
                  proposal.account_id,
                  t("budget.main.account"),
                );
                const { primaryDate, bookingDayMonth, displayDistance } = suggestedCandidateDates(proposal, detail);
                const baseDate = primaryDate ?? t("budget.receipts.noDate");
                const dateText = bookingDayMonth === null
                  ? baseDate
                  : `${baseDate} ${t("budget.receipts.bookingDateNote", { date: bookingDayMonth })}`;
                const daysText = displayDistance === 0
                  ? t("budget.receipts.candidateSameDay")
                  : t("budget.receipts.candidateDays", { days: displayDistance });
                const amountText = formatAmount(
                  proposal.amount,
                  proposal.currency,
                  locale,
                  proposal.credit_debit_indicator,
                );
                const memberCount = Math.max(proposal.member_receipt_ids.length, proposal.member_receipts.length);
                return (
                  <section
                    className="budget-notice budget-receipt-group-proposal"
                    aria-label={t("budget.receipts.groupProposalHeading")}
                  >
                    <p>
                      <strong>
                        {t("budget.receipts.groupProposalSummary", {
                          count: memberCount,
                          amount: amountText,
                        })}
                      </strong>
                    </p>
                    <div className="budget-entry-main">
                      <span className="budget-muted">{dateText}</span>
                      <span className="budget-entry-text">{accountText} ({daysText})</span>
                      <strong className={proposal.credit_debit_indicator === "DBIT" ? "is-debit" : "is-credit"}>
                        {amountText}
                      </strong>
                    </div>
                    <div className="budget-actions">
                      <button
                        type="button"
                        disabled={actionBusy !== null || groupControllerOpen}
                        onClick={(event) => { onReviewGroupProposal(detail, proposal, event.currentTarget); }}
                      >
                        {t("budget.receipts.reviewGroupAction")}
                      </button>
                    </div>
                  </section>
                );
              })(groupProposal)}

              <dl className="budget-receipt-meta">
                <dt>{t("budget.receipts.date")}</dt>
                <dd>{detail.receipt_date ?? t("budget.receipts.noDate")}</dd>
                {detail.receipt_time === null || detail.receipt_time === "" ? null : (
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
                    <dd><RedactedOrText value={detail.raw_nip} fallback="—" /></dd>
                  </>
                )}
                {detail.invoice_number === null ? null : (
                  <>
                    <dt>{t("budget.receipts.invoiceNumber")}</dt>
                    <dd><RedactedOrText value={detail.invoice_number} fallback="—" /></dd>
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

              {detail.payment_evidence.length === 0 ? null : (
                <section className="budget-receipt-section" aria-label={t("budget.receipts.paymentEvidence")}>
                  <h5>{t("budget.receipts.paymentEvidence")}</h5>
                  <ul className="budget-receipt-evidence">
                    {detail.payment_evidence.map((evidence) => (
                      <li key={evidence.receipt_id}>
                        <span className="budget-muted">{evidence.receipt_date ?? t("budget.receipts.noDate")}</span>
                        <span>{evidence.gateway ?? t("budget.receipts.unknownMerchant")}</span>
                        {evidence.gateway_status === "registered" ? (
                          <span className="budget-chip budget-receipt-gateway" data-gateway-status="registered">
                            {t("budget.receipts.gateway.registered")}
                          </span>
                        ) : null}
                        <strong>
                          {evidence.total_amount === null || detail.currency === null
                            ? t("budget.receipts.noAmount")
                            : formatAmount(evidence.total_amount, detail.currency, locale)}
                        </strong>
                        {evidence.fee_amount === null ? null : (
                          <span className="budget-muted">
                            {t("budget.receipts.paymentEvidenceFee", {
                              amount: detail.currency === null
                                ? evidence.fee_amount
                                : formatAmount(evidence.fee_amount, detail.currency, locale),
                            })}
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                </section>
              )}

              {group != null ? (
                <ReceiptGroupSection
                  key={`${group.group_id}:${String(group.group_version)}`}
                  api={api}
                  group={group}
                  receipt={detail}
                  accounts={accounts}
                  connections={connections}
                  locale={locale}
                  actionBusy={actionBusy}
                  confirmingDissolve={confirmingDissolve}
                  actionError={actionError}
                  onOpenReceipt={onOpenReceipt}
                  onAccessLoss={onAccessLoss}
                  onGroupDetailLoaded={handleGroupDetailLoaded}
                  onUnlinkGroup={(groupVersion) => { void submitUnlinkGroup(detail, groupVersion); }}
                  onStartDissolveDelete={() => { setConfirmingDissolve(true); setActionError(null); }}
                  onConfirmDissolveDelete={(groupVersion) => { void submitDissolveDelete(detail, groupVersion); }}
                  onCancelDissolveDelete={() => { setConfirmingDissolve(false); }}
                />
              ) : <section className="budget-receipt-section" aria-label={t("budget.receipts.linkSection")}>
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
                    {currentCandidate !== null ? (
                      <div className="budget-entry-main">
                        <span className="budget-muted">
                          {candidateDisplayDate(currentCandidate) ?? t("budget.receipts.noDate")}
                        </span>
                        <span className="budget-entry-text">{t("budget.receipts.linkedSummaryNote")}</span>
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
                      <div className="budget-entry-main">
                        <span className="budget-muted">{detail.receipt_date ?? t("budget.receipts.noDate")}</span>
                        <span className="budget-entry-text">{t("budget.receipts.linkedSummaryNote")}</span>
                        <strong>{receiptTotalDisplay(detail, locale)}</strong>
                      </div>
                    )}
                  </div>
                ) : (
                  <>
                    <p className="budget-muted">{t("budget.receipts.unlinkedState")}</p>
                    {canGroupReceipt ? (
                      <div className="budget-actions">
                        <button
                          type="button"
                          disabled={actionBusy !== null || groupControllerOpen}
                          onClick={(event) => { onOpenGroupCandidates(detail, event.currentTarget); }}
                        >
                          {t("budget.receipts.groupWithOthersAction")}
                        </button>
                      </div>
                    ) : null}
                  </>
                )}
              </section>}

              {detail.image_available && imageState.kind !== "none" ? (
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
                      const fallbackName = labelOf(RECEIPT_LINE_KIND_KEYS, line.line_kind);
                      const currency = detail.currency ?? "PLN";
                      return (
                        <li key={line.line_no} className="budget-entry">
                          <div className="budget-entry-main">
                            <span className="budget-entry-text">
                              <RedactedOrText value={line.raw_name} fallback={fallbackName} />
                            </span>
                            <strong>{formatAmount(line.net_line_total, currency, locale)}</strong>
                          </div>
                          <div className="budget-entry-facts">
                            <span>{line.quantity}{line.raw_unit ? ` ${line.raw_unit}` : ""}</span>
                            {line.unit_price_raw !== null ? <span>× {line.unit_price_raw}</span> : null}
                            {line.ptu_code !== null ? (
                              <span>{t("budget.receipts.ptu", { code: line.ptu_code })}</span>
                            ) : null}
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

              {activeDraft === null ? null : (
                <form
                  className="budget-receipt-section budget-receipt-confirm"
                  onSubmit={(event) => {
                    event.preventDefault();
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
                        value={activeDraft.rawShopName}
                        onChange={(event) => {
                          updateDraftFields(detail, candidates, { rawShopName: event.currentTarget.value });
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
                        value={activeDraft.rawNip}
                        onChange={(event) => {
                          updateDraftFields(detail, candidates, { rawNip: event.currentTarget.value });
                        }}
                      />
                    </label>
                    <label>
                      {t("budget.receipts.receiptKindLabel")}
                      <select
                        name="receipt_kind"
                        disabled={actionBusy !== null}
                        value={activeDraft.receiptKind}
                        onChange={(event) => {
                          updateDraftFields(detail, candidates, {
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
                        value={activeDraft.receiptDate}
                        onChange={(event) => {
                          updateDraftFields(detail, candidates, { receiptDate: event.currentTarget.value });
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
                        value={activeDraft.receiptTime}
                        onChange={(event) => {
                          setConfirmDraft({ ...activeDraft, receiptTime: event.currentTarget.value });
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
                        value={activeDraft.totalAmount}
                        onChange={(event) => {
                          updateDraftFields(detail, candidates, { totalAmount: event.currentTarget.value });
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
                        value={activeDraft.currency}
                        onChange={(event) => {
                          updateDraftFields(detail, candidates, { currency: event.currentTarget.value.toUpperCase() });
                        }}
                      />
                    </label>
                    <label>
                      {t("budget.receipts.paymentMethod")}
                      <select
                        name="payment_method"
                        disabled={actionBusy !== null}
                        value={activeDraft.paymentMethod}
                        onChange={(event) => {
                          updateDraftFields(detail, candidates, {
                            paymentMethod: event.currentTarget.value as ReceiptPaymentMethod,
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
                        {t("budget.receipts.linesHeading", { count: activeDraft.lines.length })}
                      </span>
                      <div className="budget-actions">
                        {activeDraft.lines.length === 0 ? (
                          <button
                            type="button"
                            disabled={actionBusy !== null || hasRedactedLines}
                            onClick={() => { addSummaryLine(detail, activeDraft); }}
                          >
                            {t("budget.receipts.addSummaryLine")}
                          </button>
                        ) : (
                          <button
                            type="button"
                            disabled={actionBusy !== null || hasRedactedLines}
                            onClick={() => { addSummaryLine(detail, activeDraft); }}
                          >
                            {t("budget.receipts.addLine")}
                          </button>
                        )}
                      </div>
                    </div>
                    {hasRedactedLines ? (
                      <p className="budget-warn" role="note">{t("budget.receipts.redactedLinesEditBlocked")}</p>
                    ) : null}
                    {lineDeltaDisplay === null ? null : (
                      <p className="budget-warn">
                        {t("budget.receipts.mathDelta")}: {lineDeltaDisplay}
                      </p>
                    )}
                    {activeDraft.lines.map((line, idx) => (
                      <div key={idx} className="budget-receipt-line-edit">
                        <label>
                          {t("budget.receipts.lineName")}
                          <input
                            name="line_name"
                            type="text"
                            maxLength={128}
                            disabled={actionBusy !== null || hasRedactedLines}
                            value={line.raw_name}
                            onChange={(event) => { updateLine(detail, idx, "raw_name", event.currentTarget.value); }}
                          />
                        </label>
                        <label>
                          {t("budget.receipts.lineGross")}
                          <input
                            name="line_gross"
                            type="text"
                            inputMode="decimal"
                            disabled={actionBusy !== null || hasRedactedLines}
                            value={line.gross_line_total}
                            onChange={(event) => { updateLine(detail, idx, "gross_line_total", event.currentTarget.value); }}
                          />
                        </label>
                        {line.line_kind === "item" ? (
                          <label>
                            {t("budget.receipts.lineDiscount")}
                            <input
                              name="line_discount"
                              type="text"
                              inputMode="decimal"
                              disabled={actionBusy !== null || hasRedactedLines}
                              value={line.discount_amount}
                              onChange={(event) => { updateLine(detail, idx, "discount_amount", event.currentTarget.value); }}
                            />
                          </label>
                        ) : null}
                        <label>
                          {t("budget.receipts.lineAmount")}
                          <input
                            name="line_net"
                            type="text"
                            inputMode="decimal"
                            disabled={actionBusy !== null || hasRedactedLines}
                            value={line.net_line_total}
                            onChange={(event) => { updateLine(detail, idx, "net_line_total", event.currentTarget.value); }}
                          />
                        </label>
                        <button
                          type="button"
                          disabled={actionBusy !== null || hasRedactedLines}
                          onClick={() => { removeLine(detail, idx); }}
                        >
                          {t("budget.receipts.removeLine")}
                        </button>
                      </div>
                    ))}
                  </div>

                  <fieldset ref={linkChoicesRef} tabIndex={-1} className="budget-receipt-link-choices">
                    <legend>{t("budget.receipts.linkChoiceHeading")}</legend>
                    {!linkOptions.paymentAllowsLink ? (
                      <p className="budget-muted">{t("budget.receipts.cashNoLinkNote")}</p>
                    ) : null}
                    {linkOptions.paymentAllowsLink && detail.status !== "confirmed"
                      && candidates.length === 0 && detailState.noBankData ? (
                      <p className="budget-muted budget-receipt-no-bank-data" role="note">
                        {t("budget.receipts.noBankDataPeriod")}
                      </p>
                    ) : null}
                    {linkOptions.mustUnlinkToConfirm && detail.status === "confirmed" ? (
                      <p className="budget-warn" role="note">{t("budget.receipts.confirmedUnlinkNote")}</p>
                    ) : null}
                    {detail.status === "needs_review" && detail.linked && currentCandidate === null && !matchingChanged ? (
                      <p className="budget-warn" role="note">{t("budget.receipts.needsReviewNoKeepNote")}</p>
                    ) : null}
                    {tokensDisabledByEdit ? (
                      <p className="budget-warn" role="note">{t("budget.receipts.staleSourceEditHint")}</p>
                    ) : null}
                    {activeDraft.linkChoice === "" && detail.linked ? (
                      <p className="budget-warn" role="note">
                        {linkOptions.canKeep
                          ? t("budget.receipts.reviewLinkChoiceRequired")
                          : t("budget.receipts.explicitUnlinkRequired")}
                      </p>
                    ) : null}

                    {showKeepRadio ? (
                      <label className="budget-toggle">
                        <input
                          type="radio"
                          name="link_choice"
                          value="keep"
                          disabled={disableTokens}
                          checked={activeDraft.linkChoice === "keep" && linkOptions.canKeep}
                          onChange={() => {
                            onLinkChoiceTouched(detail.receipt_id, {
                              kind: "keep",
                              keepBinding: currentCandidate === null ? null : keepCandidateBinding(currentCandidate),
                            });
                            setConfirmDraft(selectKeepDraft(activeDraft, currentCandidate));
                          }}
                        />
                        <span>{t("budget.receipts.linkChoiceKeep")}</span>
                      </label>
                    ) : null}

                    <label className="budget-toggle">
                      <input
                        type="radio"
                        name="link_choice"
                        value="unlink"
                        disabled={actionBusy !== null}
                        checked={activeDraft.linkChoice === "unlink"}
                        onChange={() => {
                          onLinkChoiceTouched(detail.receipt_id, { kind: "unlink" });
                          setConfirmDraft({ ...activeDraft, linkChoice: "unlink", selectedCandidate: null, keepBinding: null });
                        }}
                      />
                      <span>
                        {detail.linked ? t("budget.receipts.linkChoiceUnlink") : t("budget.receipts.linkChoiceNone")}
                      </span>
                    </label>

                    {showCandidateRadios ? candidates.map((candidate, index) => {
                      if (currentCandidate !== null
                        && candidate.account_id === currentCandidate.account_id
                        && candidate.entry_reference === currentCandidate.entry_reference) {
                        return null;
                      }
                      const choiceValue = `candidate:${String(index)}`;
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
                            disabled={disableTokens}
                            checked={activeDraft.linkChoice === choiceValue && linkOptions.canPickCandidates}
                            onChange={() => {
                              onLinkChoiceTouched(detail.receipt_id, {
                                kind: "candidate",
                                ...candidateSelectionBinding(candidate),
                              });
                              setConfirmDraft(selectCandidateDraft(activeDraft, candidate, index));
                            }}
                          />
                          <span>
                            {accountText} · {dateText} · {amountText}
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
                      type="button"
                      className="budget-primary"
                      disabled={actionBusy !== null || activeDraft.linkChoice === ""}
                      onClick={() => {
                        void submitConfirm(detail, candidates, activeDraft);
                      }}
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

              {group == null && confirmingDelete ? (
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
