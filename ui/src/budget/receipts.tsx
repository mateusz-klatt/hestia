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
import { LinkedTransactionIndicator } from "./receipt_link_indicator";
import { ReceiptTransactionPicker, transactionPickerDescription } from "./receipt_transaction_picker";
import {
  EMPTY_RECEIPT_FILTER,
  RECEIPT_DOCUMENT_KINDS,
  RECEIPT_DOC_KIND_KEYS,
  RECEIPT_KINDS,
  RECEIPT_KIND_KEYS,
  RECEIPT_LINE_KIND_KEYS,
  RECEIPT_MAX_PAGE_LIMIT,
  RECEIPT_PAGE_LIMIT,
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
  canPickManualTransaction,
  clearStaleCandidateSelection,
  computeLinesDelta,
  createPendingReceiptStore,
  editReceiptLine,
  filterReceiptRows,
  findAdjacentReceiptId,
  findCurrentLinkedCandidate,
  findNextUnconfirmedReceiptId,
  formatBulkErrorBucket,
  hasActiveReceiptFilter,
  hasClientSideFilter,
  hasMatchingFieldsChanged,
  hasReceiptFieldOrLineEdits,
  isBulkConfirmable,
  isExcludedDraftForBulk,
  isGroupMemberEligible,
  isReceiptDraftDirty,
  isReceiptGrouped,
  isRedactedValue,
  isStaleCursorError,
  keepCandidateBinding,
  keepEvidenceMatches,
  merchantBulkGroups,
  mergeReceiptRows,
  normalizeDecimalInput,
  preselectedCandidate,
  receiptAccountLabel,
  receiptHasRedactedLines,
  receiptMerchantGroupKey,
  receiptNeedsConfirmation,
  receiptShopName,
  receiptTotalAdjustment,
  reconcileLinkChoice,
  resolveDraftFromDetailSession,
  resolvePostRefreshSelectionId,
  sameReceiptFilter,
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
  ReceiptListResult,
  ReceiptPaymentMethod,
  ReceiptSettledEvent,
  ReceiptSourceKind,
  ReceiptStatus,
  ReceiptStatusFilter,
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
  transactionSearchSupported?: boolean | undefined;
  accounts: readonly BudgetAccount[];
  connections?: readonly BudgetConnection[] | undefined;
  locale: string;
  revision: number;
  onAccessLoss: AccessBarrier;
}

type ListState =
  | { kind: "loading" }
  | {
      kind: "ready";
      rows: ReceiptSummary[];
      nextCursor: string | null;
      loadingMore: boolean;
      loadMoreError: boolean;
      refreshing: boolean;
      refreshError: boolean;
    }
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
      candidatesOnly?: boolean;
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

const SEARCH_DEBOUNCE_MS = 300;
const AUTO_ADVANCE_STORAGE_KEY = "hestia.receipts.autoAdvance";
const STATUS_FILTER_STORAGE_KEY = "hestia.receipts.statusFilter";

function readStatusFilterPreference(): ReceiptStatusFilter {
  try {
    const value = localStorage.getItem(STATUS_FILTER_STORAGE_KEY);
    if (value === "") return value;
    return RECEIPT_STATUSES.find((status) => status === value) ?? "needs_action";
  } catch {
    return "needs_action";
  }
}

function writeStatusFilterPreference(status: ReceiptStatusFilter): void {
  try {
    localStorage.setItem(STATUS_FILTER_STORAGE_KEY, status);
  } catch {
    // Keep the current filter in memory if storage is unavailable.
  }
}

function readAutoAdvancePreference(): boolean {
  try {
    return localStorage.getItem(AUTO_ADVANCE_STORAGE_KEY) !== "0";
  } catch {
    return true;
  }
}

function writeAutoAdvancePreference(enabled: boolean): void {
  try {
    localStorage.setItem(AUTO_ADVANCE_STORAGE_KEY, enabled ? "1" : "0");
  } catch {
    // Storage unavailable (private mode or blocked storage); keep in-memory state only.
  }
}

type LoadMoreOutcome =
  | { kind: "loaded"; rows: ReceiptSummary[]; nextCursor: string | null }
  | { kind: "noop" }
  | { kind: "stale_or_aborted" }
  | { kind: "error" };

type RefreshOutcome =
  | { kind: "loaded"; rows: ReceiptSummary[]; nextCursor: string | null }
  | { kind: "stale_or_aborted" }
  | { kind: "error" };

interface PendingDrawerReconcile {
  reloadDetail: boolean;
  preserveNotice: string | null;
}

type DrawerNavigationTarget =
  | {
      kind: "open";
      receiptId: string;
      trigger?: HTMLButtonElement | undefined;
      preserveNotice?: string | null | undefined;
    }
  | { kind: "close"; preserveNotice?: string | null | undefined }
  | { kind: "navigate"; direction: "prev" | "next"; preserveNotice?: string | null | undefined };

interface QuietRefreshOptions {
  /** Suppresses automatic post-refresh reconciliation only while this specific receipt remains open (caller reconciles). */
  deferReconcileForId?: string | null;
  /** Suppresses automatic switch/close while keeping a 404 probe active when this receipt disappears (e.g., mutation error). */
  probeIfMissingForId?: string | null;
  reloadDetailForId?: string | null;
  preserveNotice?: string | null;
}

interface ActiveRefreshState {
  promise: Promise<RefreshOutcome>;
  dirty: boolean;
  generation: number;
  deferredReconcileIds: Set<string>;
  probeIfMissingIds: Set<string>;
  reconcileOptionsById: Map<string, PendingDrawerReconcile>;
}

interface ReceiptScrollAnchor {
  receiptId: string;
  top: number;
}

function findRowElement(container: HTMLElement | null, receiptId: string): HTMLElement | null {
  if (container === null) return null;
  for (const el of container.querySelectorAll<HTMLElement>("li[data-receipt-id]")) {
    if (el.dataset.receiptId === receiptId) return el;
  }
  return null;
}

export function ReceiptsView({
  receiptsApi = defaultReceiptsApi,
  pendingStore,
  actorKey = "",
  transactionSearchSupported = false,
  accounts,
  connections = [],
  locale,
  revision,
  onAccessLoss,
}: ReceiptsViewProps) {
  const store = pendingStore ?? getFallbackPendingStore(receiptsApi);

  const [draft, setDraft] = useState<ReceiptFilter>(() => ({
    ...EMPTY_RECEIPT_FILTER,
    status: readStatusFilterPreference(),
  }));
  const [filter, setFilter] = useState<ReceiptFilter>(draft);
  const [listState, setListState] = useState<ListState>({ kind: "loading" });
  const [listRefresh, setListRefresh] = useState(0);
  const [disabledAtRevision, setDisabledAtRevision] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [autoAdvance, setAutoAdvance] = useState<boolean>(readAutoAdvancePreference);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [drawerOpenSession, setDrawerOpenSession] = useState(0);
  const [detailState, setDetailState] = useState<DetailState>({ kind: "loading" });
  const [detailRefresh, setDetailRefresh] = useState(0);
  const [imageState, setImageState] = useState<ImageState>({ kind: "none" });
  const [pendingNavPrompt, setPendingNavPrompt] = useState<{
    receiptId: string;
    target: DrawerNavigationTarget;
  } | null>(null);
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
  const revisionRef = useRef(revision);
  const groupReturnFocusRef = useRef<HTMLButtonElement | null>(null);
  const listStateRef = useRef<ListState>(listState);
  const draftRef = useRef<ReceiptFilter>(draft);
  const filterRef = useRef<ReceiptFilter>(filter);
  const searchMode = filter.query.trim() !== "";
  const searchModeRef = useRef(searchMode);
  const bulkProgressRef = useRef(bulkProgress);
  const autoAdvanceRef = useRef(autoAdvance);
  const groupControllerOpenRef = useRef(groupControllerTarget !== null);
  const groupCommittedWhileOpenRef = useRef(false);
  const searchDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const loadMorePromiseRef = useRef<Promise<LoadMoreOutcome> | null>(null);
  const activeRefreshRef = useRef<ActiveRefreshState | null>(null);
  const listContainerRef = useRef<HTMLElement | null>(null);
  const pendingScrollRestoreRef = useRef<{
    scrollX: number;
    scrollY: number;
    anchors: ReceiptScrollAnchor[];
  } | null>(null);
  const latestDetailVersionRef = useRef<{ receiptId: string; version: number } | null>(null);
  const openReceiptIndexRef = useRef<number>(0);
  const navGenerationRef = useRef(0);
  const drawerSessionRef = useRef(0);
  const detailProbeGenRef = useRef(0);
  const dirtyDraftReceiptIdRef = useRef<string | null>(null);
  const pendingNavRef = useRef<DrawerNavigationTarget | null>(null);
  const pendingDrawerReconcileRef = useRef<Map<string, PendingDrawerReconcile>>(new Map());
  const autoAdvanceVisitedRef = useRef<Map<string, ReceiptStatus>>(new Map());

  useLayoutEffect(() => {
    openReceiptIdRef.current = selectedId;
  }, [selectedId]);

  useLayoutEffect(() => {
    filterRef.current = filter;
    searchModeRef.current = searchMode;
    bulkProgressRef.current = bulkProgress;
    autoAdvanceRef.current = autoAdvance;
    groupControllerOpenRef.current = groupControllerTarget !== null;
  }, [filter, searchMode, bulkProgress, autoAdvance, groupControllerTarget]);

  useLayoutEffect(() => {
    if (selectedId === null || listState.kind !== "ready") return;
    const shown = filterReceiptRows(listState.rows, filter, searchMode);
    const idx = shown.findIndex((row) => row.receipt_id === selectedId);
    if (idx >= 0) {
      openReceiptIndexRef.current = idx;
    }
  }, [selectedId, listState, filter, searchMode]);

  useLayoutEffect(() => {
    if (revisionRef.current !== revision) {
      revisionRef.current = revision;
    }
  }, [revision]);

  useLayoutEffect(() => {
    const pending = pendingScrollRestoreRef.current;
    if (pending === null || listState.kind !== "ready") return;
    pendingScrollRestoreRef.current = null;
    for (const anchor of pending.anchors) {
      const anchorEl = findRowElement(listContainerRef.current, anchor.receiptId);
      if (anchorEl === null) continue;
      const delta = anchorEl.getBoundingClientRect().top - anchor.top;
      if (Math.abs(delta) >= 1) {
        const targetY = Math.max(0, window.scrollY + delta);
        try {
          window.scrollTo(window.scrollX, targetY);
        } catch {
          // Scroll API unavailable in minimal DOM environments.
        }
      }
      return;
    }
    if (window.scrollY !== pending.scrollY) {
      try {
        window.scrollTo(pending.scrollX, pending.scrollY);
      } catch {
        // Scroll API unavailable in minimal DOM environments.
      }
    }
  }, [listState]);

  const updateListState = useCallback((updater: (current: ListState) => ListState): ListState => {
    const next = updater(listStateRef.current);
    listStateRef.current = next;
    setListState(next);
    return next;
  }, []);

  const cancelSearchDebounce = useCallback(() => {
    if (searchDebounceRef.current !== null) {
      clearTimeout(searchDebounceRef.current);
      searchDebounceRef.current = null;
    }
  }, []);

  const updateAutoAdvance = useCallback((enabled: boolean) => {
    autoAdvanceRef.current = enabled;
    setAutoAdvance(enabled);
    writeAutoAdvancePreference(enabled);
  }, []);

  const updateGroupControllerTarget = useCallback((next: ReceiptGroupControllerTarget | null) => {
    groupControllerOpenRef.current = next !== null;
    groupCommittedWhileOpenRef.current = false;
    setGroupControllerTarget(next);
  }, []);

  const clearImage = useCallback(() => {
    imageGeneration.current += 1;
    revokeObjectUrl(activeImageUrl.current);
    activeImageUrl.current = null;
    setImageState({ kind: "none" });
  }, []);

  const isMounted = useCallback((): boolean => mounted.current, []);

  const isStillOpen = useCallback((receiptId: string): boolean => (
    openReceiptIdRef.current === receiptId
  ), []);

  const isDraftDirtyFor = useCallback((receiptId: string): boolean => (
    dirtyDraftReceiptIdRef.current === receiptId
  ), []);

  const linkSessionKey = useCallback((receiptId: string, openSession = drawerSessionRef.current): string => (
    `${actorKey}:${receiptId}:${String(openSession)}`
  ), [actorKey]);

  const bumpDrawerSession = useCallback((): number => {
    const next = drawerSessionRef.current + 1;
    drawerSessionRef.current = next;
    setDrawerOpenSession(next);
    return next;
  }, []);

  const deferDrawerReconcile = useCallback((receiptId: string, pending: PendingDrawerReconcile) => {
    const prev = pendingDrawerReconcileRef.current.get(receiptId);
    pendingDrawerReconcileRef.current.set(receiptId, {
      reloadDetail: (prev?.reloadDetail ?? false) || pending.reloadDetail,
      preserveNotice: pending.preserveNotice ?? prev?.preserveNotice ?? null,
    });
  }, []);

  const resetDrawer = useCallback(() => {
    const prevId = openReceiptIdRef.current;
    const prevSession = drawerSessionRef.current;
    if (prevId !== null) {
      receiptLinkMemoryRef.current.delete(linkSessionKey(prevId, prevSession));
      pendingDrawerReconcileRef.current.delete(prevId);
      activeRefreshRef.current?.deferredReconcileIds.delete(prevId);
      activeRefreshRef.current?.probeIfMissingIds.delete(prevId);
    }
    dirtyDraftReceiptIdRef.current = null;
    pendingNavRef.current = null;
    setPendingNavPrompt(null);
    bumpDrawerSession();
    detailProbeGenRef.current += 1;
    detailGeneration.current += 1;
    navGenerationRef.current += 1;
    openReceiptIdRef.current = null;
    openReceiptIndexRef.current = 0;
    receiptDrawerTriggerRef.current = null;
    loadedReceiptIdRef.current = null;
    latestReadyCandidatesRef.current = null;
    latestDetailVersionRef.current = null;
    autoAdvanceVisitedRef.current.clear();
    clearImage();
    setSelectedId(null);
    setDetailState({ kind: "loading" });
  }, [bumpDrawerSession, clearImage, linkSessionKey]);

  const clearAllReceiptsState = useCallback((disabled: boolean, disabledRevision: number | null = null) => {
    cancelSearchDebounce();
    listGeneration.current += 1;
    navGenerationRef.current += 1;
    loadMorePromiseRef.current = null;
    activeRefreshRef.current = null;
    pendingScrollRestoreRef.current = null;
    pendingDrawerReconcileRef.current.clear();
    bulkCancelledRef.current = true;
    bulkRunGenRef.current += 1;
    receiptFloorGenRef.current.clear();
    receiptLinkMemoryRef.current.clear();
    staleCursorReloadedRef.current = false;
    resetDrawer();
    updateGroupControllerTarget(null);
    setGroupControllerSession((current) => current + 1);
    groupReturnFocusRef.current = null;
    setSelectedForBulk(new Set());
    setBulkConfirmPlan(null);
    setBulkProgress(null);
    const resetFilter = { ...EMPTY_RECEIPT_FILTER, status: draftRef.current.status };
    draftRef.current = resetFilter;
    filterRef.current = resetFilter;
    searchModeRef.current = false;
    setDraft(resetFilter);
    setFilter((current) => (sameReceiptFilter(current, resetFilter) ? current : resetFilter));
    setNotice(null);
    setDisabledAtRevision(disabled ? disabledRevision : null);
    updateListState(() => (disabled ? { kind: "disabled" } : { kind: "error" }));
  }, [cancelSearchDebounce, resetDrawer, updateGroupControllerTarget, updateListState]);

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

  const closeDrawerAsDeleted = useCallback((receiptId: string) => {
    if (openReceiptIdRef.current !== receiptId) return;
    receiptLinkMemoryRef.current.delete(linkSessionKey(receiptId));
    receiptFloorGenRef.current.set(receiptId, ++detailGeneration.current);
    setSelectedForBulk((current) => {
      if (!current.has(receiptId)) return current;
      const next = new Set(current);
      next.delete(receiptId);
      return next;
    });
    const trigger = receiptDrawerTriggerRef.current;
    resetDrawer();
    setNotice(t("budget.receipts.deletedNotice"));
    if (trigger?.isConnected === true) trigger.focus({ preventScroll: true });
  }, [linkSessionKey, resetDrawer]);

  const probeMissingOpenReceipt = useCallback((receiptId: string) => {
    const session = drawerSessionRef.current;
    const probeGen = ++detailProbeGenRef.current;
    void receiptsApi.receiptDetail(receiptId).then((result: BudgetResult<ReceiptDetailResult>) => {
      if (
        checkBarrier(result)
        || !isMounted()
        || detailProbeGenRef.current !== probeGen
        || openReceiptIdRef.current !== receiptId
        || drawerSessionRef.current !== session
      ) {
        return;
      }
      if (!result.ok && result.status === 404) {
        closeDrawerAsDeleted(receiptId);
      }
    });
  }, [checkBarrier, closeDrawerAsDeleted, isMounted, receiptsApi]);

  const fetchNextPage = useCallback((): Promise<LoadMoreOutcome> => {
    if (loadMorePromiseRef.current !== null) return loadMorePromiseRef.current;
    const cur = listStateRef.current;
    if (cur.kind !== "ready" || cur.loadingMore || cur.nextCursor === null) {
      return Promise.resolve({ kind: "noop" });
    }
    const cursor = cur.nextCursor;
    const token = listGeneration.current;
    const activeFilter = filterRef.current;
    const activeSearchMode = searchModeRef.current;
    updateListState((current) => (current.kind === "ready"
      ? { ...current, loadingMore: true, loadMoreError: false }
      : current));
    const request = activeSearchMode
      ? receiptsApi.receiptsSearch({ query: activeFilter.query.trim(), limit: RECEIPT_PAGE_LIMIT, cursor })
      : receiptsApi.receiptsList(buildReceiptListRequest(activeFilter, cursor));
    const pending: Promise<LoadMoreOutcome> = request.then((result): LoadMoreOutcome => {
      if (loadMorePromiseRef.current === pending) loadMorePromiseRef.current = null;
      if (checkBarrier(result) || !isMounted() || token !== listGeneration.current) {
        return { kind: "stale_or_aborted" };
      }
      if (!result.ok) {
        if (isStaleCursorError(result) && !staleCursorReloadedRef.current) {
          staleCursorReloadedRef.current = true;
          setNotice(t("budget.receipts.cursorExpiredNotice"));
          setListRefresh((current) => current + 1);
          return { kind: "error" };
        }
        updateListState((current) => (current.kind === "ready"
          ? {
              ...current,
              loadingMore: false,
              loadMoreError: true,
              nextCursor: isStaleCursorError(result) ? null : current.nextCursor,
            }
          : current));
        return { kind: "error" };
      }
      staleCursorReloadedRef.current = false;
      const nextState = updateListState((current) => {
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
          refreshing: false,
          refreshError: false,
        };
      });
      if (nextState.kind !== "ready") return { kind: "stale_or_aborted" };
      return { kind: "loaded", rows: nextState.rows, nextCursor: nextState.nextCursor };
    });
    loadMorePromiseRef.current = pending;
    return pending;
  }, [receiptsApi, checkBarrier, isMounted, updateListState]);

  /** Single unified guard for ALL drawer navigation paths (sync/async Prev/Next, auto-advance, list selection, close). */
  const requestDrawerNavigation = useCallback((
    target: DrawerNavigationTarget,
    options?: { force?: boolean },
  ): boolean => {
    const runGuarded = (navTarget: DrawerNavigationTarget, force: boolean): boolean => {
      const currentId = openReceiptIdRef.current;
      if (bulkProgressRef.current !== null || groupControllerOpenRef.current) return false;
      if (currentId !== null && store.get(currentId) !== null) return false;
      if (navTarget.kind === "open" && currentId === navTarget.receiptId) return false;

      let resolvedTarget: DrawerNavigationTarget = navTarget;
      if (navTarget.kind === "navigate") {
        if (currentId === null) return false;
        const curList = listStateRef.current;
        if (curList.kind !== "ready") return false;
        const shown = filterReceiptRows(curList.rows, filterRef.current, searchModeRef.current);
        const directTarget = findAdjacentReceiptId(shown, currentId, navTarget.direction);
        if (directTarget !== null) {
          resolvedTarget = {
            kind: "open",
            receiptId: directTarget,
            preserveNotice: navTarget.preserveNotice,
          };
        } else if (
          navTarget.direction === "prev"
          || curList.nextCursor === null
          || !shown.some((row) => row.receipt_id === currentId)
        ) {
          return false;
        }
      }

      if (!force && currentId !== null && isDraftDirtyFor(currentId)) {
        pendingNavRef.current = resolvedTarget;
        setPendingNavPrompt({ receiptId: currentId, target: resolvedTarget });
        return false;
      }

      pendingNavRef.current = null;
      setPendingNavPrompt(null);

      if (resolvedTarget.kind === "close") {
        const trigger = receiptDrawerTriggerRef.current;
        resetDrawer();
        if (resolvedTarget.preserveNotice != null) {
          setNotice(resolvedTarget.preserveNotice);
        }
        if (trigger?.isConnected === true) trigger.focus({ preventScroll: true });
        return true;
      }

      if (resolvedTarget.kind === "open") {
        const prevSession = drawerSessionRef.current;
        if (currentId !== null) {
          receiptLinkMemoryRef.current.delete(linkSessionKey(currentId, prevSession));
          pendingDrawerReconcileRef.current.delete(currentId);
          activeRefreshRef.current?.deferredReconcileIds.delete(currentId);
          activeRefreshRef.current?.probeIfMissingIds.delete(currentId);
        } else {
          receiptDrawerTriggerRef.current = resolvedTarget.trigger ?? null;
          autoAdvanceVisitedRef.current.clear();
        }
        dirtyDraftReceiptIdRef.current = null;
        pendingDrawerReconcileRef.current.delete(resolvedTarget.receiptId);
        const nextSession = bumpDrawerSession();
        receiptLinkMemoryRef.current.delete(linkSessionKey(resolvedTarget.receiptId, nextSession));
        detailProbeGenRef.current += 1;
        if (listStateRef.current.kind === "ready") {
          const shown = filterReceiptRows(listStateRef.current.rows, filterRef.current, searchModeRef.current);
          const idx = shown.findIndex((row) => row.receipt_id === resolvedTarget.receiptId);
          if (idx >= 0) openReceiptIndexRef.current = idx;
        }
        navGenerationRef.current += 1;
        openReceiptIdRef.current = resolvedTarget.receiptId;
        loadedReceiptIdRef.current = null;
        latestReadyCandidatesRef.current = null;
        latestDetailVersionRef.current = null;
        setDetailState({ kind: "loading" });
        setNotice(resolvedTarget.preserveNotice ?? null);
        setSelectedId(resolvedTarget.receiptId);
        return true;
      }

      if (currentId === null) return false;
      const curList = listStateRef.current;
      if (curList.kind !== "ready" || curList.nextCursor === null) return false;
      const startListGen = listGeneration.current;
      const startNavGen = ++navGenerationRef.current;
      const startSession = drawerSessionRef.current;
      const isNavStillActive = (): boolean => (
        isMounted()
        && listGeneration.current === startListGen
        && navGenerationRef.current === startNavGen
        && openReceiptIdRef.current === currentId
        && drawerSessionRef.current === startSession
      );
      void (async () => {
        let nextCursor: string | null = curList.nextCursor;
        while (nextCursor !== null) {
          const outcome = await fetchNextPage();
          if (!isNavStillActive()) return;
          if (outcome.kind !== "loaded") return;
          nextCursor = outcome.nextCursor;
          const nextShown = filterReceiptRows(outcome.rows, filterRef.current, searchModeRef.current);
          const nextTarget = findAdjacentReceiptId(nextShown, currentId, "next");
          if (nextTarget !== null) {
            runGuarded(
              { kind: "open", receiptId: nextTarget, preserveNotice: resolvedTarget.preserveNotice },
              false,
            );
            return;
          }
        }
      })();
      return true;
    };

    return runGuarded(target, options?.force === true);
  }, [bumpDrawerSession, fetchNextPage, isDraftDirtyFor, isMounted, linkSessionKey, resetDrawer, store]);

  const discardPendingNavigation = useCallback(() => {
    const currentId = openReceiptIdRef.current;
    const pending = pendingNavRef.current;
    pendingNavRef.current = null;
    setPendingNavPrompt(null);
    if (currentId !== null && dirtyDraftReceiptIdRef.current === currentId) {
      dirtyDraftReceiptIdRef.current = null;
    }
    if (pending !== null) {
      requestDrawerNavigation(pending, { force: true });
    }
  }, [requestDrawerNavigation]);

  const reconcileDrawerAfterRefresh = useCallback((
    refreshedRows: readonly ReceiptSummary[],
    options?: {
      reloadDetailForId?: string | null;
      preserveNotice?: string | null;
    },
  ): "none" | "kept" | "deferred" | "switched" | "closed" => {
    const currentId = openReceiptIdRef.current;
    if (currentId === null) return "none";
    if (isDraftDirtyFor(currentId)) {
      deferDrawerReconcile(currentId, {
        reloadDetail: true,
        preserveNotice: options?.preserveNotice ?? null,
      });
      return "deferred";
    }
    pendingDrawerReconcileRef.current.delete(currentId);
    const refreshedShown = filterReceiptRows(refreshedRows, filterRef.current, searchModeRef.current);
    const nextIndex = refreshedShown.findIndex((row) => row.receipt_id === currentId);
    if (nextIndex >= 0) {
      openReceiptIndexRef.current = nextIndex;
      // INV-C: Clean drawer always follows the server after any list refresh via the detail generation guard.
      receiptFloorGenRef.current.set(currentId, ++detailGeneration.current);
      setDetailRefresh((current) => current + 1);
      return "kept";
    }
    const fallbackId = resolvePostRefreshSelectionId(
      refreshedShown,
      currentId,
      openReceiptIndexRef.current,
    );
    if (fallbackId !== null) {
      return requestDrawerNavigation({
        kind: "open",
        receiptId: fallbackId,
        preserveNotice: options?.preserveNotice,
      }) ? "switched" : "deferred";
    }
    return requestDrawerNavigation({
      kind: "close",
      preserveNotice: options?.preserveNotice,
    }) ? "closed" : "deferred";
  }, [deferDrawerReconcile, isDraftDirtyFor, requestDrawerNavigation]);

  const handleDraftDirtyChange = useCallback((receiptId: string, dirty: boolean) => {
    if (openReceiptIdRef.current !== receiptId) return;
    if (dirty) {
      dirtyDraftReceiptIdRef.current = receiptId;
      return;
    }
    if (dirtyDraftReceiptIdRef.current !== receiptId) return;
    dirtyDraftReceiptIdRef.current = null;
    pendingNavRef.current = null;
    setPendingNavPrompt(null);
    const pending = pendingDrawerReconcileRef.current.get(receiptId);
    if (pending === undefined) return;
    pendingDrawerReconcileRef.current.delete(receiptId);
    if (store.get(receiptId) !== null) return;
    const curList = listStateRef.current;
    if (curList.kind !== "ready") {
      if (pending.reloadDetail) {
        receiptFloorGenRef.current.set(receiptId, ++detailGeneration.current);
        setDetailRefresh((current) => current + 1);
      }
      return;
    }
    reconcileDrawerAfterRefresh(curList.rows, {
      reloadDetailForId: pending.reloadDetail ? receiptId : null,
      preserveNotice: pending.preserveNotice,
    });
  }, [reconcileDrawerAfterRefresh, store]);

  const quietRefetchLoadedRange = useCallback((options: QuietRefreshOptions = {}): Promise<RefreshOutcome> => {
    if (disabledAtRevision === revision || listStateRef.current.kind === "disabled") {
      updateListState(() => ({ kind: "disabled" }));
      return Promise.resolve({ kind: "stale_or_aborted" });
    }
    const mergeRefreshOptions = (target: ActiveRefreshState, opts: QuietRefreshOptions): void => {
      if (opts.deferReconcileForId != null) {
        target.deferredReconcileIds.add(opts.deferReconcileForId);
      }
      if (opts.probeIfMissingForId != null) {
        target.probeIfMissingIds.add(opts.probeIfMissingForId);
      }
      if (opts.reloadDetailForId != null) {
        target.deferredReconcileIds.delete(opts.reloadDetailForId);
        target.probeIfMissingIds.delete(opts.reloadDetailForId);
      }
      const targetId = opts.reloadDetailForId ?? openReceiptIdRef.current;
      if (targetId !== null && (opts.reloadDetailForId === targetId || opts.preserveNotice != null)) {
        const prev = target.reconcileOptionsById.get(targetId);
        target.reconcileOptionsById.set(targetId, {
          reloadDetail: (prev?.reloadDetail ?? false) || opts.reloadDetailForId === targetId,
          preserveNotice: opts.preserveNotice ?? prev?.preserveNotice ?? null,
        });
      }
    };

    const existing = activeRefreshRef.current;
    if (existing !== null) {
      existing.dirty = true;
      mergeRefreshOptions(existing, options);
      return existing.promise;
    }

    const state: ActiveRefreshState = {
      promise: Promise.resolve({ kind: "stale_or_aborted" }),
      dirty: false,
      generation: ++listGeneration.current,
      deferredReconcileIds: new Set<string>(),
      probeIfMissingIds: new Set<string>(),
      reconcileOptionsById: new Map<string, PendingDrawerReconcile>(),
    };
    mergeRefreshOptions(state, options);

    const run = async (): Promise<RefreshOutcome> => {
      const isRefreshDirty = (): boolean => state.dirty;
      const clearActiveRefresh = (): void => {
        if (activeRefreshRef.current === state) {
          activeRefreshRef.current = null;
        }
      };
      const runPass = async (token: number): Promise<RefreshOutcome> => {
        loadMorePromiseRef.current = null;
        navGenerationRef.current += 1;
        staleCursorReloadedRef.current = false;

        const cur = listStateRef.current;
        const activeFilter = filterRef.current;
        const activeSearchMode = searchModeRef.current;
        const targetCount = cur.kind === "ready" ? cur.rows.length : 0;

        const savedScrollX = window.scrollX;
        const savedScrollY = window.scrollY;
        const openIdAtStart = openReceiptIdRef.current;
        const anchors: ReceiptScrollAnchor[] = [];
        if (cur.kind === "ready") {
          if (openIdAtStart !== null) {
            const anchorEl = findRowElement(listContainerRef.current, openIdAtStart);
            if (anchorEl !== null) {
              anchors.push({ receiptId: openIdAtStart, top: anchorEl.getBoundingClientRect().top });
            }
          }
          // Confirmation can remove the open receipt from the filtered list.
          if (listContainerRef.current !== null
            && (openIdAtStart === null || state.deferredReconcileIds.has(openIdAtStart))) {
            for (const el of listContainerRef.current.querySelectorAll<HTMLElement>("li[data-receipt-id]")) {
              const receiptId = el.dataset.receiptId;
              if (receiptId === undefined || receiptId === openIdAtStart) continue;
              const rect = el.getBoundingClientRect();
              if (rect.bottom > 0) {
                anchors.push({ receiptId, top: rect.top });
                break;
              }
            }
          }
          updateListState((current) => (current.kind === "ready"
            ? { ...current, loadingMore: false, loadMoreError: false, refreshing: true, refreshError: false }
            : current));
        }

        const collected: ReceiptSummary[] = [];
        const seenIds = new Set<string>();
        const seenCursors = new Set<string>();
        let cursor: string | null = null;
        let finalNextCursor: string | null = null;
        let keepFetching = true;
        let passFailed = false;

        while (keepFetching) {
          const pageLimit: number = cursor === null
            ? Math.min(RECEIPT_MAX_PAGE_LIMIT, Math.max(RECEIPT_PAGE_LIMIT, targetCount))
            : Math.min(RECEIPT_MAX_PAGE_LIMIT, Math.max(1, targetCount - collected.length));
          const request: Promise<BudgetResult<ReceiptListResult>> = activeSearchMode
            ? receiptsApi.receiptsSearch({
                query: activeFilter.query.trim(),
                limit: pageLimit,
                ...(cursor === null ? {} : { cursor }),
              })
            : receiptsApi.receiptsList(buildReceiptListRequest(activeFilter, cursor, pageLimit));
          const result: BudgetResult<ReceiptListResult> = await request;
          if (checkBarrier(result) || !isMounted() || token !== listGeneration.current) {
            return { kind: "stale_or_aborted" };
          }
          if (!result.ok) {
            passFailed = true;
            keepFetching = false;
            continue;
          }
          let addedNew = false;
          for (const row of result.data.receipts) {
            if (seenIds.has(row.receipt_id)) continue;
            seenIds.add(row.receipt_id);
            collected.push(row);
            addedNew = true;
          }
          const next: string | null = result.data.next_cursor;
          const cursorStuck: boolean = next !== null && (next === cursor || seenCursors.has(next));
          finalNextCursor = next === null || cursorStuck || !addedNew ? null : next;
          if (finalNextCursor === null || collected.length >= targetCount) {
            keepFetching = false;
          } else {
            seenCursors.add(finalNextCursor);
            cursor = finalNextCursor;
          }
        }

        if (isRefreshDirty()) {
          return { kind: "loaded", rows: collected, nextCursor: finalNextCursor };
        }

        if (passFailed) {
          updateListState((current) => (current.kind === "ready"
            ? { ...current, refreshing: false, refreshError: true }
            : { kind: "error" }));
          const openId = openReceiptIdRef.current;
          if (openId !== null && !state.deferredReconcileIds.has(openId)) {
            const opts = state.reconcileOptionsById.get(openId);
            const shouldReload = opts?.reloadDetail === true || state.probeIfMissingIds.has(openId);
            if (shouldReload) {
              if (isDraftDirtyFor(openId)) {
                deferDrawerReconcile(openId, opts ?? { reloadDetail: true, preserveNotice: null });
              } else {
                receiptFloorGenRef.current.set(openId, ++detailGeneration.current);
                setDetailRefresh((current) => current + 1);
              }
            }
          }
          return { kind: "error" };
        }

        if (cur.kind === "ready") {
          pendingScrollRestoreRef.current = {
            scrollX: savedScrollX,
            scrollY: savedScrollY,
            anchors,
          };
        }
        updateListState(() => ({
          kind: "ready",
          rows: collected,
          nextCursor: finalNextCursor,
          loadingMore: false,
          loadMoreError: false,
          refreshing: false,
          refreshError: false,
        }));
        const openId = openReceiptIdRef.current;
        if (openId !== null && store.get(openId) === null && !state.deferredReconcileIds.has(openId)) {
          const opts = state.reconcileOptionsById.get(openId);
          const presentInList = collected.some((row) => row.receipt_id === openId);
          if (state.probeIfMissingIds.has(openId)) {
            if (!isDraftDirtyFor(openId)) {
              // INV-C: Clean drawer follows the server after refresh (covers lost-response confirm/save or delete 404).
              receiptFloorGenRef.current.set(openId, ++detailGeneration.current);
              setDetailRefresh((current) => current + 1);
            } else if (!presentInList) {
              probeMissingOpenReceipt(openId);
            }
          } else if (!presentInList && opts?.reloadDetail !== true && !isDraftDirtyFor(openId)) {
            // INV-C: Clean open receipt vanished on a background refresh under the same filter — reload via the
            // detail generation guard so a 404 closes with "Dokument został usunięty" and 200 shows latest state.
            receiptFloorGenRef.current.set(openId, ++detailGeneration.current);
            setDetailRefresh((current) => current + 1);
          } else {
            reconcileDrawerAfterRefresh(collected, {
              reloadDetailForId: opts?.reloadDetail === true ? openId : null,
              preserveNotice: opts?.preserveNotice ?? null,
            });
            if (isStillOpen(openId) && !presentInList) {
              probeMissingOpenReceipt(openId);
            }
          }
        }
        return { kind: "loaded", rows: collected, nextCursor: finalNextCursor };
      };

      try {
        let keepRunning = true;
        let lastOutcome: RefreshOutcome = { kind: "stale_or_aborted" };
        let token = state.generation;
        while (keepRunning) {
          state.dirty = false;
          state.generation = token;
          lastOutcome = await runPass(token);
          if (lastOutcome.kind === "stale_or_aborted" || !isMounted() || !isRefreshDirty()) {
            clearActiveRefresh();
            keepRunning = false;
          } else {
            token = ++listGeneration.current;
          }
        }
        return lastOutcome;
      } finally {
        clearActiveRefresh();
      }
    };

    state.promise = run();
    activeRefreshRef.current = state;
    return state.promise;
  }, [
    disabledAtRevision,
    revision,
    receiptsApi,
    checkBarrier,
    deferDrawerReconcile,
    isDraftDirtyFor,
    isMounted,
    isStillOpen,
    probeMissingOpenReceipt,
    updateListState,
    reconcileDrawerAfterRefresh,
    store,
  ]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      cancelSearchDebounce();
      listGeneration.current += 1;
      detailGeneration.current += 1;
      detailProbeGenRef.current += 1;
      imageGeneration.current += 1;
      navGenerationRef.current += 1;
      loadMorePromiseRef.current = null;
      activeRefreshRef.current = null;
      dirtyDraftReceiptIdRef.current = null;
      pendingNavRef.current = null;
      pendingDrawerReconcileRef.current.clear();
      bulkCancelledRef.current = true;
      bulkRunGenRef.current += 1;
      openReceiptIdRef.current = null;
      revokeObjectUrl(activeImageUrl.current);
      activeImageUrl.current = null;
    };
  }, [cancelSearchDebounce]);

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
      void quietRefetchLoadedRange();
    }
  }, [actorKey, checkBarrier, quietRefetchLoadedRange, receiptsApi, store]);

  // Subscribe to persistent store so locks survive unmount/remount and late settlements refresh the panel.
  useEffect(() => {
    setPendingMap(store.snapshot());
    return store.subscribe((event?: ReceiptSettledEvent) => {
      if (!mounted.current) return;
      setPendingMap(store.snapshot());
      if (event !== undefined) {
        // Reload list after every settled mutation (including error) in case a server write committed before response loss.
        void quietRefetchLoadedRange();
        if (openReceiptIdRef.current === event.receiptId) {
          if (event.outcome === "deleted" || event.outcome === "not_found") {
            closeDrawerAsDeleted(event.receiptId);
          } else if (isDraftDirtyFor(event.receiptId)) {
            deferDrawerReconcile(event.receiptId, { reloadDetail: true, preserveNotice: null });
          } else {
            receiptFloorGenRef.current.set(event.receiptId, ++detailGeneration.current);
            setDetailRefresh((current) => current + 1);
          }
        }
      }
    });
  }, [store, quietRefetchLoadedRange, closeDrawerAsDeleted, deferDrawerReconcile, isDraftDirtyFor]);

  useEffect(() => {
    if (disabledAtRevision === revision) {
      updateListState(() => ({ kind: "disabled" }));
      return;
    }
    const token = ++listGeneration.current;
    loadMorePromiseRef.current = null;
    activeRefreshRef.current = null;
    pendingScrollRestoreRef.current = null;
    navGenerationRef.current += 1;
    autoAdvanceVisitedRef.current.clear();
    updateListState(() => ({ kind: "loading" }));
    const request = searchMode
      ? receiptsApi.receiptsSearch({ query: filter.query.trim(), limit: RECEIPT_PAGE_LIMIT })
      : receiptsApi.receiptsList(buildReceiptListRequest(filter));
    void request.then((result) => {
      if (checkBarrier(result) || !mounted.current || token !== listGeneration.current) return;
      if (!result.ok) {
        updateListState(() => ({ kind: "error" }));
        return;
      }
      updateListState(() => ({
        kind: "ready",
        rows: result.data.receipts,
        nextCursor: result.data.next_cursor,
        loadingMore: false,
        loadMoreError: false,
        refreshing: false,
        refreshError: false,
      }));
      const openId = openReceiptIdRef.current;
      if (openId !== null && store.get(openId) === null) {
        reconcileDrawerAfterRefresh(result.data.receipts);
        if (isStillOpen(openId) && !result.data.receipts.some((row) => row.receipt_id === openId)) {
          probeMissingOpenReceipt(openId);
        }
      }
    });
    return () => {
      if (listGeneration.current === token) {
        listGeneration.current += 1;
      }
      loadMorePromiseRef.current = null;
    };
  }, [
    receiptsApi,
    filter,
    searchMode,
    revision,
    listRefresh,
    disabledAtRevision,
    checkBarrier,
    isStillOpen,
    probeMissingOpenReceipt,
    updateListState,
    reconcileDrawerAfterRefresh,
    store,
  ]);

  const loadMore = useCallback(() => {
    void fetchNextPage();
  }, [fetchNextPage]);

  useEffect(() => {
    if (selectedId === null) {
      detailGeneration.current += 1;
      loadedReceiptIdRef.current = null;
      latestReadyCandidatesRef.current = null;
      latestDetailVersionRef.current = null;
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
      if (checkBarrier(result) || !isMounted() || token !== detailGeneration.current
        || token <= floorToken || openReceiptIdRef.current !== targetId) return;
      if (!result.ok && result.status === 404) {
        closeDrawerAsDeleted(targetId);
        void quietRefetchLoadedRange();
        return;
      }
      if (isDraftDirtyFor(targetId)) {
        deferDrawerReconcile(targetId, { reloadDetail: true, preserveNotice: null });
        if (result.ok) {
          const freshCandidates = result.data.candidates ?? [];
          latestReadyCandidatesRef.current = { receiptId: targetId, candidates: freshCandidates };
          setDetailState((current) => (
            current.kind === "ready" && current.detail.receipt_id === targetId
              ? { ...current, candidates: freshCandidates, candidatesOnly: true }
              : current
          ));
        }
        return;
      }
      if (!result.ok) {
        loadedReceiptIdRef.current = null;
        latestReadyCandidatesRef.current = null;
        setDetailState({ kind: "error" });
        return;
      }
      const receipt = result.data.receipt;
      const latestKnown = latestDetailVersionRef.current;
      if (
        latestKnown !== null
        && (latestKnown.receiptId === targetId || latestKnown.receiptId === receipt.receipt_id)
        && receipt.version < latestKnown.version
      ) {
        return;
      }
      latestDetailVersionRef.current = { receiptId: receipt.receipt_id, version: receipt.version };
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
          if (checkBarrier(imgResult, { explicitDisabledOnly: true }) || !isMounted()
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
  }, [
    receiptsApi,
    selectedId,
    revision,
    detailRefresh,
    checkBarrier,
    clearImage,
    closeDrawerAsDeleted,
    deferDrawerReconcile,
    isDraftDirtyFor,
    isMounted,
    quietRefetchLoadedRange,
  ]);

  const drawerActionBusy: ReceiptActionKind | null =
    selectedId === null ? null : (pendingMap.get(selectedId) ?? null);

  const getLatestCandidates = useCallback((receiptId: string): readonly ReceiptCandidate[] | null => (
    latestReadyCandidatesRef.current?.receiptId === receiptId ? latestReadyCandidatesRef.current.candidates : null
  ), []);

  const handleConfirmed = useCallback(async (
    updated: ReceiptDetail,
    previous: Pick<ReceiptDetail, "status" | "version">,
    options?: { continuePendingNav?: boolean },
  ) => {
    const session = drawerSessionRef.current;
    const pendingNav = options?.continuePendingNav === true ? pendingNavRef.current : null;
    pendingNavRef.current = null;
    setPendingNavPrompt(null);
    const isSameDrawerSession = (): boolean => (
      isMounted()
      && openReceiptIdRef.current === updated.receipt_id
      && drawerSessionRef.current === session
    );
    receiptLinkMemoryRef.current.delete(linkSessionKey(updated.receipt_id, session));
    if (dirtyDraftReceiptIdRef.current === updated.receipt_id) {
      dirtyDraftReceiptIdRef.current = null;
    }
    pendingDrawerReconcileRef.current.delete(updated.receipt_id);
    const floorToken = ++detailGeneration.current;
    receiptFloorGenRef.current.set(updated.receipt_id, floorToken);
    latestReadyCandidatesRef.current = { receiptId: updated.receipt_id, candidates: [] };
    latestDetailVersionRef.current = { receiptId: updated.receipt_id, version: updated.version };
    setSelectedForBulk((current) => {
      if (!current.has(updated.receipt_id)) return current;
      const next = new Set(current);
      next.delete(updated.receipt_id);
      return next;
    });

    if (listStateRef.current.kind === "ready") {
      const idx = filterReceiptRows(listStateRef.current.rows, filterRef.current, searchModeRef.current)
        .findIndex((row) => row.receipt_id === updated.receipt_id);
      if (idx >= 0) openReceiptIndexRef.current = idx;
    }
    const previousIndex = openReceiptIndexRef.current;

    setDetailState({ kind: "ready", detail: updated, candidates: [], groupCandidates: [], noBankData: false });

    const refreshOutcome = await quietRefetchLoadedRange({ deferReconcileForId: updated.receipt_id });
    if (!isSameDrawerSession()) return;

    const serverChanged = updated.status !== previous.status || updated.version !== previous.version;
    const confirmNotice = serverChanged ? t("budget.receipts.confirmSuccess") : null;
    if (refreshOutcome.kind === "stale_or_aborted") return;
    if (refreshOutcome.kind === "error") {
      if (pendingNav !== null) {
        if (requestDrawerNavigation({ ...pendingNav, preserveNotice: confirmNotice })) return;
      }
      if (isDraftDirtyFor(updated.receipt_id)) {
        deferDrawerReconcile(updated.receipt_id, { reloadDetail: true, preserveNotice: confirmNotice });
      } else {
        setDetailRefresh((current) => current + 1);
      }
      if (confirmNotice !== null) setNotice(confirmNotice);
      return;
    }

    let latestRows = refreshOutcome.rows;
    let latestCursor: string | null = refreshOutcome.nextCursor;
    let latestShown = filterReceiptRows(latestRows, filterRef.current, searchModeRef.current);

    if (pendingNav !== null) {
      if (requestDrawerNavigation({ ...pendingNav, preserveNotice: confirmNotice })) return;
      if (isDraftDirtyFor(updated.receipt_id)) {
        deferDrawerReconcile(updated.receipt_id, { reloadDetail: true, preserveNotice: confirmNotice });
        if (!latestRows.some((row) => row.receipt_id === updated.receipt_id)) {
          probeMissingOpenReceipt(updated.receipt_id);
        }
        if (confirmNotice !== null) setNotice(confirmNotice);
        return;
      }
      const selection = reconcileDrawerAfterRefresh(latestRows, {
        reloadDetailForId: updated.receipt_id,
        preserveNotice: confirmNotice,
      });
      if ((selection === "kept" || selection === "deferred") && confirmNotice !== null) {
        setNotice(confirmNotice);
      }
      return;
    }

    if (isDraftDirtyFor(updated.receipt_id)) {
      deferDrawerReconcile(updated.receipt_id, { reloadDetail: true, preserveNotice: confirmNotice });
      if (!latestRows.some((row) => row.receipt_id === updated.receipt_id)) {
        probeMissingOpenReceipt(updated.receipt_id);
      }
      if (confirmNotice !== null) setNotice(confirmNotice);
      return;
    }

    if (!autoAdvanceRef.current) {
      const selection = reconcileDrawerAfterRefresh(latestRows, {
        reloadDetailForId: updated.receipt_id,
        preserveNotice: confirmNotice,
      });
      if ((selection === "kept" || selection === "deferred") && confirmNotice !== null) {
        setNotice(confirmNotice);
      }
      return;
    }

    autoAdvanceVisitedRef.current.set(updated.receipt_id, updated.status);

    const findVisibleNextUnconfirmed = (shown: readonly ReceiptSummary[], wrap: boolean): string | null => (
      findNextUnconfirmedReceiptId(shown, updated.receipt_id, {
        wrap,
        startIndex: previousIndex,
        predicate: (row) => autoAdvanceVisitedRef.current.get(row.receipt_id) !== row.status,
      })
    );

    const forwardTarget = findVisibleNextUnconfirmed(latestShown, false);
    if (forwardTarget !== null) {
      requestDrawerNavigation({ kind: "open", receiptId: forwardTarget, preserveNotice: confirmNotice });
      return;
    }

    if (latestCursor !== null) {
      const startListGen = listGeneration.current;
      const startNavGen = ++navGenerationRef.current;
      do {
        const outcome = await fetchNextPage();
        if (!isSameDrawerSession() || listGeneration.current !== startListGen
          || navGenerationRef.current !== startNavGen) {
          return;
        }
        if (isDraftDirtyFor(updated.receipt_id)) {
          deferDrawerReconcile(updated.receipt_id, { reloadDetail: true, preserveNotice: confirmNotice });
          if (confirmNotice !== null) setNotice(confirmNotice);
          return;
        }
        if (outcome.kind !== "loaded") return;
        latestRows = outcome.rows;
        latestCursor = outcome.nextCursor;
        latestShown = filterReceiptRows(latestRows, filterRef.current, searchModeRef.current);
        const nextForward = findVisibleNextUnconfirmed(latestShown, false);
        if (nextForward !== null) {
          requestDrawerNavigation({ kind: "open", receiptId: nextForward, preserveNotice: confirmNotice });
          return;
        }
      } while (latestCursor !== null);
    }

    const wrappedTarget = findVisibleNextUnconfirmed(latestShown, true);
    if (wrappedTarget !== null) {
      requestDrawerNavigation({ kind: "open", receiptId: wrappedTarget, preserveNotice: confirmNotice });
      return;
    }

    const anyStillUnconfirmed = receiptNeedsConfirmation(updated)
      || latestShown.some((row) => row.receipt_id !== updated.receipt_id && receiptNeedsConfirmation(row));
    if (anyStillUnconfirmed) {
      const selection = reconcileDrawerAfterRefresh(latestRows, {
        reloadDetailForId: updated.receipt_id,
        preserveNotice: confirmNotice,
      });
      if ((selection === "kept" || selection === "deferred") && confirmNotice !== null) {
        setNotice(confirmNotice);
      }
      return;
    }

    requestDrawerNavigation({ kind: "close", preserveNotice: t("budget.receipts.allConfirmed") });
  }, [
    deferDrawerReconcile,
    fetchNextPage,
    isDraftDirtyFor,
    isMounted,
    linkSessionKey,
    probeMissingOpenReceipt,
    quietRefetchLoadedRange,
    reconcileDrawerAfterRefresh,
    requestDrawerNavigation,
  ]);

  const resolveDetailDraft = useCallback((
    detail: ReceiptDetail,
    candidates: readonly ReceiptCandidate[],
  ): { draft: ReceiptConfirmDraft; showSuggestedBanner: boolean; keepEvidenceInvalidated: boolean } => {
    const key = linkSessionKey(detail.receipt_id);
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
  }, [linkSessionKey]);

  const recordLinkChoiceTouched = useCallback((
    receiptId: string,
    choice: ReceiptLinkExplicitChoice,
    detail?: Pick<ReceiptDetail, "version" | "status" | "linked">,
  ) => {
    const key = linkSessionKey(receiptId);
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
  }, [linkSessionKey]);

  const clearLinkChoiceMemory = useCallback((receiptId: string) => {
    receiptLinkMemoryRef.current.delete(linkSessionKey(receiptId));
  }, [linkSessionKey]);

  const commitFilter = useCallback((next: ReceiptFilter) => {
    cancelSearchDebounce();
    if (bulkProgressRef.current !== null || !validReceiptDateRange(next)) return;
    staleCursorReloadedRef.current = false;
    setNotice(null);
    filterRef.current = next;
    searchModeRef.current = next.query.trim() !== "";
    setFilter((current) => (sameReceiptFilter(current, next) ? current : next));
  }, [cancelSearchDebounce]);

  const updateImmediateFilter = useCallback((patch: Partial<ReceiptFilter>) => {
    const nextDraft: ReceiptFilter = { ...draftRef.current, ...patch };
    draftRef.current = nextDraft;
    setDraft(nextDraft);
    commitFilter(nextDraft);
  }, [commitFilter]);

  const updateStatusFilter = (status: ReceiptStatusFilter): void => {
    writeStatusFilterPreference(status);
    updateImmediateFilter({ status });
  };

  const updateSearchQuery = useCallback((query: string) => {
    const nextDraft: ReceiptFilter = { ...draftRef.current, query };
    draftRef.current = nextDraft;
    setDraft(nextDraft);
    cancelSearchDebounce();
    if (bulkProgressRef.current !== null || !validReceiptDateRange(nextDraft)) return;
    searchDebounceRef.current = setTimeout(() => {
      searchDebounceRef.current = null;
      if (!mounted.current || bulkProgressRef.current !== null) return;
      const latest = draftRef.current;
      if (!validReceiptDateRange(latest)) return;
      staleCursorReloadedRef.current = false;
      setNotice(null);
      filterRef.current = latest;
      searchModeRef.current = latest.query.trim() !== "";
      setFilter((current) => (sameReceiptFilter(current, latest) ? current : latest));
    }, SEARCH_DEBOUNCE_MS);
  }, [cancelSearchDebounce]);

  const clearFilters = useCallback(() => {
    if (bulkProgressRef.current !== null) return;
    cancelSearchDebounce();
    staleCursorReloadedRef.current = false;
    draftRef.current = EMPTY_RECEIPT_FILTER;
    filterRef.current = EMPTY_RECEIPT_FILTER;
    searchModeRef.current = false;
    setDraft(EMPTY_RECEIPT_FILTER);
    setFilter(EMPTY_RECEIPT_FILTER);
    setNotice(null);
    writeStatusFilterPreference(EMPTY_RECEIPT_FILTER.status);
  }, [cancelSearchDebounce]);

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
    <section ref={listContainerRef} className="budget-entries budget-receipts" aria-label={title}>
      <header className="budget-entries-head">
        <div>
          <h4>{title}</h4>
          <p className="budget-muted">{t("budget.receipts.intro")}</p>
        </div>
      </header>

      <form className="budget-range" onSubmit={(event) => { event.preventDefault(); commitFilter(draftRef.current); }}>
        <label>
          {t("budget.receipts.search")}
          <input
            name="query"
            type="search"
            maxLength={128}
            placeholder={t("budget.receipts.searchPlaceholder")}
            value={draft.query}
            onChange={(event) => { updateSearchQuery(event.currentTarget.value); }}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                commitFilter(draftRef.current);
              }
            }}
          />
        </label>
        <label>
          {t("budget.receipts.status")}
          <select
            name="status"
            value={draft.status}
            onChange={(event) => { updateStatusFilter(event.currentTarget.value as ReceiptStatusFilter); }}
          >
            <option value="needs_action">{t("budget.receipts.statusFilter.needsAction")}</option>
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
              updateImmediateFilter({ documentKind: event.currentTarget.value as "" | ReceiptDocumentKind });
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
              updateImmediateFilter({ sourceKind: event.currentTarget.value as "" | ReceiptSourceKind });
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
              updateImmediateFilter({ linked: event.currentTarget.value as "" | "linked" | "unlinked" });
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
            onChange={(event) => { updateImmediateFilter({ dateFrom: event.currentTarget.value }); }}
          />
        </label>
        <label>
          {t("budget.main.to")}
          <input
            name="date_to"
            type="date"
            value={draft.dateTo}
            min={draft.dateFrom || undefined}
            onChange={(event) => { updateImmediateFilter({ dateTo: event.currentTarget.value }); }}
          />
        </label>
        {hasActiveReceiptFilter(draft) || hasActiveReceiptFilter(filter) ? (
          <button type="button" onClick={clearFilters}>{t("budget.receipts.clearFilters")}</button>
        ) : null}
      </form>

      {filter.status !== "" && filter.status !== "confirmed" ? (
        <p className="budget-muted">
          {t("budget.receipts.statusFilter.confirmedHidden")}{" · "}
          <button type="button" className="budget-link" onClick={() => { updateStatusFilter(""); }}>
            {t("budget.receipts.statusFilter.showAll")}
          </button>
        </p>
      ) : null}
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
                      updateGroupControllerTarget({ kind: "bulk", anchor, selectedRows: bulkGroupPlan.selected });
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
                    <li key={row.receipt_id} className="budget-entry budget-receipt-row" data-receipt-id={row.receipt_id}>
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
                            value={receiptShopName(row)}
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
                        {row.linked_transaction == null ? null : (
                          <LinkedTransactionIndicator transaction={row.linked_transaction} locale={locale} />
                        )}
                        {rowPending === null ? null : (
                          <span className="budget-chip budget-muted" role="status">
                            {pendingActionLabel(rowPending)}
                          </span>
                        )}
                        <button
                          type="button"
                          disabled={bulkBusy || (anyPending && rowPending === null)}
                          onClick={(event) => {
                            requestDrawerNavigation({
                              kind: "open",
                              receiptId: row.receipt_id,
                              trigger: event.currentTarget,
                            });
                          }}
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
          {listState.refreshing ? (
            <p className="budget-muted" role="status">{t("budget.receipts.refreshing")}</p>
          ) : null}
          {listState.refreshError ? (
            <div className="budget-notice budget-entries-action" role="alert">
              <span>{t("budget.receipts.loadError")}</span>
              <button type="button" onClick={() => { void quietRefetchLoadedRange(); }}>
                {t("budget.receipts.retry")}
              </button>
            </div>
          ) : null}
          {clientFiltered ? (
            <p className="budget-muted" role="note">{t("budget.receipts.clientFilterHint")}</p>
          ) : null}
          {listState.loadMoreError ? (
            <p className="budget-notice" role="alert">{t("budget.receipts.loadError")}</p>
          ) : null}
          {listState.nextCursor === null ? null : (
            <div className="budget-actions">
              <button type="button" disabled={listState.loadingMore || listState.refreshing} onClick={loadMore}>
                {t("budget.receipts.loadMore")}
              </button>
            </div>
          )}
        </>
      ) : null}

      {selectedId === null ? null : (() => {
        const selectedIndex = shownRows.findIndex((row) => row.receipt_id === selectedId);
        const canPrev = selectedIndex > 0;
        const canNext = selectedIndex >= 0 && (
          selectedIndex < shownRows.length - 1
          || (listState.kind === "ready" && listState.nextCursor !== null)
        );
        const loadingMoreList = listState.kind === "ready" && (listState.loadingMore || listState.refreshing);
        return (
          <ReceiptDetailDrawer
            key={`${actorKey}:${selectedId}:${String(drawerOpenSession)}`}
            api={receiptsApi}
            transactionSearchSupported={transactionSearchSupported}
            store={store}
            accounts={accounts}
            connections={connections}
            locale={locale}
            detailState={detailState}
            imageState={imageState}
            actionBusy={drawerActionBusy}
            groupControllerOpen={groupControllerTarget !== null}
            canPrev={canPrev}
            canNext={canNext}
            loadingMore={loadingMoreList}
            autoAdvance={autoAdvance}
            onAutoAdvanceChange={updateAutoAdvance}
            onPrev={() => { requestDrawerNavigation({ kind: "navigate", direction: "prev" }); }}
            onNext={() => { requestDrawerNavigation({ kind: "navigate", direction: "next" }); }}
            unsavedPromptVisible={pendingNavPrompt?.receiptId === selectedId}
            onDiscardPendingNav={discardPendingNavigation}
            isOpenReceipt={(receiptId) => openReceiptIdRef.current === receiptId}
            getLatestCandidates={getLatestCandidates}
            resolveDetailDraft={resolveDetailDraft}
            onClearLinkChoiceMemory={clearLinkChoiceMemory}
            onDraftDirtyChange={handleDraftDirtyChange}
            onLinkChoiceTouched={recordLinkChoiceTouched}
            onAccessLoss={checkBarrier}
            onClose={() => { requestDrawerNavigation({ kind: "close" }); }}
            onOpenReceipt={(receiptId) => { requestDrawerNavigation({ kind: "open", receiptId }); }}
            onOpenGroupCandidates={(detail, trigger) => {
              if (bulkBusy) return;
              groupReturnFocusRef.current = trigger;
              setGroupControllerSession((current) => current + 1);
              updateGroupControllerTarget({ kind: "candidates", anchor: detail });
            }}
            onReviewGroupProposal={(detail, proposal, trigger) => {
              if (bulkBusy) return;
              groupReturnFocusRef.current = trigger;
              setGroupControllerSession((current) => current + 1);
              updateGroupControllerTarget({ kind: "proposal", anchor: detail, proposal });
            }}
            onRetryDetail={() => { setDetailRefresh((current) => current + 1); }}
            onConfirmed={(updated, previous, opts) => { void handleConfirmed(updated, previous, opts); }}
            onGroupUnlinked={(receiptId) => {
              updateGroupControllerTarget(null);
              clearLinkChoiceMemory(receiptId);
              const floorToken = ++detailGeneration.current;
              receiptFloorGenRef.current.set(receiptId, floorToken);
              const unlinkNotice = t("budget.receipts.groupUnlinkSuccess");
              setNotice(unlinkNotice);
              void quietRefetchLoadedRange({
                reloadDetailForId: receiptId,
                preserveNotice: unlinkNotice,
              });
            }}
            onConflictOrStale={(receiptId) => {
              if (dirtyDraftReceiptIdRef.current === receiptId) {
                dirtyDraftReceiptIdRef.current = null;
              }
              pendingNavRef.current = null;
              setPendingNavPrompt(null);
              pendingDrawerReconcileRef.current.delete(receiptId);
              recordLinkChoiceTouched(receiptId, { kind: "empty" });
              const floorToken = ++detailGeneration.current;
              receiptFloorGenRef.current.set(receiptId, floorToken);
              setDetailRefresh((current) => current + 1);
            }}
            onReloadLatestVersion={(receiptId) => {
              if (dirtyDraftReceiptIdRef.current === receiptId) {
                dirtyDraftReceiptIdRef.current = null;
              }
              pendingNavRef.current = null;
              setPendingNavPrompt(null);
              pendingDrawerReconcileRef.current.delete(receiptId);
              clearLinkChoiceMemory(receiptId);
              const floorToken = ++detailGeneration.current;
              receiptFloorGenRef.current.set(receiptId, floorToken);
              setDetailRefresh((current) => current + 1);
            }}
            onNotFound={(receiptId) => {
              closeDrawerAsDeleted(receiptId);
              void quietRefetchLoadedRange();
            }}
            onDeleted={(receiptId, dissolvedGroup = false) => {
              clearLinkChoiceMemory(receiptId);
              const floorToken = ++detailGeneration.current;
              receiptFloorGenRef.current.set(receiptId, floorToken);
              setSelectedForBulk((current) => {
                if (!current.has(receiptId)) return current;
                const next = new Set(current);
                next.delete(receiptId);
                return next;
              });
              const trigger = receiptDrawerTriggerRef.current;
              resetDrawer();
              if (dissolvedGroup) setNotice(t("budget.receipts.dissolveDeleteSuccess"));
              void quietRefetchLoadedRange();
              if (trigger?.isConnected === true) trigger.focus({ preventScroll: true });
            }}
            onMutationError={() => {
              void quietRefetchLoadedRange({ probeIfMissingForId: openReceiptIdRef.current });
            }}
          />
        );
      })()}

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
            const hadCommittedStep = groupCommittedWhileOpenRef.current;
            updateGroupControllerTarget(null);
            const opener = groupReturnFocusRef.current;
            groupReturnFocusRef.current = null;
            if (opener?.isConnected === true) opener.focus({ preventScroll: true });
            if (hadCommittedStep) {
              const openId = openReceiptIdRef.current;
              if (openId !== null && !isDraftDirtyFor(openId)) {
                clearLinkChoiceMemory(openId);
                receiptFloorGenRef.current.set(openId, ++detailGeneration.current);
                setDetailRefresh((current) => current + 1);
              }
              void quietRefetchLoadedRange({ probeIfMissingForId: openId });
            }
          }}
          onPartialConfirm={(affectedIds) => {
            groupCommittedWhileOpenRef.current = true;
            const openId = openReceiptIdRef.current;
            if (openId !== null && affectedIds.includes(openId) && !isDraftDirtyFor(openId)) {
              clearLinkChoiceMemory(openId);
            }
            void quietRefetchLoadedRange({ probeIfMissingForId: openId });
          }}
          onGroupLinked={(_, affectedIds) => {
            updateGroupControllerTarget(null);
            setSelectedForBulk((current) => {
              const next = new Set(current);
              for (const id of affectedIds) next.delete(id);
              return next;
            });
            const openId = openReceiptIdRef.current;
            if (openId !== null && affectedIds.includes(openId)) {
              clearLinkChoiceMemory(openId);
            }
            const linkNotice = t("budget.receipts.groupLinkSuccess");
            setNotice(linkNotice);
            const opener = groupReturnFocusRef.current;
            groupReturnFocusRef.current = null;
            if (opener?.isConnected === true) opener.focus({ preventScroll: true });
            void quietRefetchLoadedRange({
              reloadDetailForId: openId !== null && affectedIds.includes(openId) ? openId : null,
              preserveNotice: linkNotice,
            });
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
    | "searchReceiptTransactions"
    | "isAuthorizationLocked"
  >;
  transactionSearchSupported: boolean;
  store: PendingReceiptStore;
  accounts: readonly BudgetAccount[];
  connections: readonly BudgetConnection[];
  locale: string;
  detailState: DetailState;
  imageState: ImageState;
  actionBusy: ReceiptActionKind | null;
  groupControllerOpen: boolean;
  canPrev: boolean;
  canNext: boolean;
  loadingMore: boolean;
  autoAdvance: boolean;
  onAutoAdvanceChange: (enabled: boolean) => void;
  onPrev: () => void;
  onNext: () => void;
  unsavedPromptVisible: boolean;
  onDiscardPendingNav: () => void;
  isOpenReceipt: (receiptId: string) => boolean;
  getLatestCandidates: (receiptId: string) => readonly ReceiptCandidate[] | null;
  resolveDetailDraft: (
    detail: ReceiptDetail,
    candidates: readonly ReceiptCandidate[],
  ) => { draft: ReceiptConfirmDraft; showSuggestedBanner: boolean; keepEvidenceInvalidated: boolean };
  onClearLinkChoiceMemory: (receiptId: string) => void;
  onDraftDirtyChange: (receiptId: string, dirty: boolean) => void;
  onLinkChoiceTouched: (receiptId: string, choice: ReceiptLinkExplicitChoice) => void;
  onAccessLoss: ReceiptAccessBarrier;
  onClose: () => void;
  onOpenReceipt: (receiptId: string) => void;
  onOpenGroupCandidates: (detail: ReceiptDetail, trigger: HTMLButtonElement | null) => void;
  onReviewGroupProposal: (detail: ReceiptDetail, proposal: ReceiptGroupCandidate, trigger: HTMLButtonElement | null) => void;
  onRetryDetail: () => void;
  onConfirmed: (
    receipt: ReceiptDetail,
    previous: Pick<ReceiptDetail, "status" | "version">,
    options?: { continuePendingNav?: boolean },
  ) => void;
  onGroupUnlinked: (receiptId: string) => void;
  onConflictOrStale: (receiptId: string) => void;
  onReloadLatestVersion: (receiptId: string) => void;
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
      value={receiptShopName(receipt)}
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
  transactionSearchSupported,
  store,
  accounts,
  connections,
  locale,
  detailState,
  imageState,
  actionBusy,
  groupControllerOpen,
  canPrev,
  canNext,
  loadingMore,
  autoAdvance,
  onAutoAdvanceChange,
  onPrev,
  onNext,
  unsavedPromptVisible,
  onDiscardPendingNav,
  isOpenReceipt,
  getLatestCandidates,
  resolveDetailDraft,
  onClearLinkChoiceMemory,
  onDraftDirtyChange,
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
  onReloadLatestVersion,
  onNotFound,
  onDeleted,
  onMutationError,
}: DrawerProps) {
  const [confirmDraft, setConfirmDraft] = useState<ReceiptConfirmDraft | null>(null);
  const [manualPickerOpen, setManualPickerOpen] = useState(false);
  const confirmDraftRef = useRef<ReceiptConfirmDraft | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [conflictReloadPending, setConflictReloadPending] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [confirmingDissolve, setConfirmingDissolve] = useState(false);
  const [suggestionDismissed, setSuggestionDismissed] = useState(false);
  const keepActionErrorRef = useRef<
    | null
    | { mode: "always" }
    | { mode: "if_unchanged"; receiptId: string; version: number; status: ReceiptStatus }
  >(null);
  const drawerMountedRef = useRef(true);
  const groupDetailRef = useRef<ReceiptGroupDetail | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const manualPickerTriggerRef = useRef<HTMLButtonElement | null>(null);
  const linkChoicesRef = useRef<HTMLFieldSetElement | null>(null);
  const searchTransactions = transactionSearchSupported ? api.searchReceiptTransactions : undefined;
  const manualPickerVisible = manualPickerOpen && searchTransactions !== undefined
    && detailState.kind === "ready" && confirmDraft !== null
    && canPickManualTransaction(detailState.detail, confirmDraft);

  const closeManualPicker = useCallback(() => { setManualPickerOpen(false); }, []);

  const handleGroupDetailLoaded = useCallback((loaded: ReceiptGroupDetail) => {
    groupDetailRef.current = loaded;
  }, []);

  useLayoutEffect(() => { closeButtonRef.current?.focus({ preventScroll: true }); }, []);
  useLayoutEffect(() => {
    if (manualPickerVisible || manualPickerTriggerRef.current === null) return;
    manualPickerTriggerRef.current.focus({ preventScroll: true });
    manualPickerTriggerRef.current = null;
    closeManualPicker();
  }, [closeManualPicker, manualPickerVisible]);

  useEffect(() => {
    drawerMountedRef.current = true;
    return () => { drawerMountedRef.current = false; };
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && actionBusy === null && !groupControllerOpen && !manualPickerVisible) onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => { document.removeEventListener("keydown", onKeyDown); };
  }, [onClose, actionBusy, groupControllerOpen, manualPickerVisible]);

  useEffect(() => {
    const shouldKeepExistingError = (detail: ReceiptDetail): boolean => {
      const keepErr = keepActionErrorRef.current;
      keepActionErrorRef.current = null;
      if (keepErr === null) return false;
      if (keepErr.mode === "always") return true;
      return (
        keepErr.receiptId === detail.receipt_id
        && keepErr.version === detail.version
        && keepErr.status === detail.status
      );
    };

    if (detailState.kind === "ready" && !isReceiptGrouped(detailState.detail)) {
      if (
        detailState.candidatesOnly === true
        && confirmDraftRef.current !== null
        && confirmDraftRef.current.receiptId === detailState.detail.receipt_id
      ) {
        // Candidates-only refresh while the user holds a dirty draft: keep user edits intact and only check bank drift.
        const currentDraft = confirmDraftRef.current;
        const freshKey = buildCandidateSetKey(detailState.detail, detailState.candidates);
        const freshKeep = findCurrentLinkedCandidate(detailState.detail, detailState.candidates);
        if (currentDraft.linkChoice === "keep" && !keepEvidenceMatches(freshKeep, currentDraft.keepBinding)) {
          keepActionErrorRef.current = { mode: "always" };
          onLinkChoiceTouched(detailState.detail.receipt_id, { kind: "empty" });
          const nextDraft: ReceiptConfirmDraft = {
            ...currentDraft,
            linkChoice: "",
            selectedCandidate: null,
            keepBinding: null,
            candidateSetKey: freshKey,
          };
          confirmDraftRef.current = nextDraft;
          setConfirmDraft(nextDraft);
          setActionError(t("budget.receipts.bankEvidenceChanged"));
          onDraftDirtyChange(
            detailState.detail.receipt_id,
            isReceiptDraftDirty(detailState.detail, nextDraft, detailState.candidates),
          );
        } else if (
          currentDraft.linkChoice.startsWith("candidate:")
          && currentDraft.candidateSetKey !== freshKey
        ) {
          onLinkChoiceTouched(detailState.detail.receipt_id, { kind: "empty" });
          setSuggestionDismissed(true);
          const nextDraft: ReceiptConfirmDraft = {
            ...currentDraft,
            linkChoice: "",
            selectedCandidate: null,
            keepBinding: null,
            candidateSetKey: freshKey,
          };
          confirmDraftRef.current = nextDraft;
          setConfirmDraft(nextDraft);
          onDraftDirtyChange(
            detailState.detail.receipt_id,
            isReceiptDraftDirty(detailState.detail, nextDraft, detailState.candidates),
          );
        } else if (currentDraft.candidateSetKey !== freshKey) {
          const nextDraft: ReceiptConfirmDraft = { ...currentDraft, candidateSetKey: freshKey };
          confirmDraftRef.current = nextDraft;
          setConfirmDraft(nextDraft);
        }
        return;
      }
      groupDetailRef.current = null;
      const resolved = resolveDetailDraft(detailState.detail, detailState.candidates);
      confirmDraftRef.current = resolved.draft;
      setConfirmDraft(resolved.draft);
      setSuggestionDismissed(!resolved.showSuggestedBanner);
      if (shouldKeepExistingError(detailState.detail)) {
        // Keep the existing alert when explicitly preserved or when server version/status did not advance.
      } else if (resolved.keepEvidenceInvalidated) {
        setActionError(t("budget.receipts.bankEvidenceChanged"));
      } else {
        setActionError(null);
      }
      setConflictReloadPending(false);
      setConfirmingDelete(false);
      setConfirmingDissolve(false);
    } else if (detailState.kind === "ready") {
      if (groupDetailRef.current?.group.group_id !== detailState.detail.link_group?.group_id) {
        groupDetailRef.current = null;
      }
      setSuggestionDismissed(false);
      confirmDraftRef.current = null;
      setConfirmDraft(null);
      if (!shouldKeepExistingError(detailState.detail)) {
        setActionError(null);
      }
      setConflictReloadPending(false);
      setConfirmingDelete(false);
      setConfirmingDissolve(false);
    } else if (detailState.kind === "loading") {
      setSuggestionDismissed(false);
      confirmDraftRef.current = null;
      setConfirmDraft(null);
      if (keepActionErrorRef.current === null) setActionError(null);
      setConflictReloadPending(false);
      setConfirmingDelete(false);
      setConfirmingDissolve(false);
    } else {
      groupDetailRef.current = null;
      setSuggestionDismissed(false);
      keepActionErrorRef.current = null;
      confirmDraftRef.current = null;
      setConfirmDraft(null);
      setActionError(null);
      setConflictReloadPending(false);
      setConfirmingDelete(false);
      setConfirmingDissolve(false);
    }
  }, [detailState, onDraftDirtyChange, onLinkChoiceTouched, resolveDetailDraft]);

  const title = t("budget.receipts.detailTitle");

  const resetDraftSynchronously = (
    detail: ReceiptDetail,
    candidates: readonly ReceiptCandidate[],
  ): void => {
    onClearLinkChoiceMemory(detail.receipt_id);
    const resolved = resolveDetailDraft(detail, candidates);
    confirmDraftRef.current = resolved.draft;
    setConfirmDraft(resolved.draft);
    setSuggestionDismissed(!resolved.showSuggestedBanner);
    keepActionErrorRef.current = null;
    setActionError(null);
    setConflictReloadPending(false);
    setConfirmingDelete(false);
    setConfirmingDissolve(false);
  };

  const commitDraft = (
    detail: ReceiptDetail,
    candidates: readonly ReceiptCandidate[],
    next: ReceiptConfirmDraft,
  ) => {
    confirmDraftRef.current = next;
    setConfirmDraft(next);
    onDraftDirtyChange(detail.receipt_id, isReceiptDraftDirty(detail, next, candidates));
  };

  const mutateDraft = (
    detail: ReceiptDetail,
    candidates: readonly ReceiptCandidate[],
    fallback: ReceiptConfirmDraft,
    updater: (current: ReceiptConfirmDraft) => ReceiptConfirmDraft,
  ) => {
    const base = confirmDraftRef.current ?? fallback;
    commitDraft(detail, candidates, updater(base));
  };

  const updateDraftFields = (
    detail: ReceiptDetail,
    candidates: readonly ReceiptCandidate[],
    fallback: ReceiptConfirmDraft,
    patch: Partial<ReceiptConfirmDraft>,
  ) => {
    mutateDraft(detail, candidates, fallback, (current) => {
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

  const submitConfirm = async (
    detail: ReceiptDetail,
    candidates: readonly ReceiptCandidate[],
    draft: ReceiptConfirmDraft,
    options?: { continuePendingNav?: boolean },
  ) => {
    if (isReceiptGrouped(detail)) return;
    const currentCandidates = getLatestCandidates(detail.receipt_id) ?? candidates;
    const built = buildConfirmRequest(detail, draft, currentCandidates, t("budget.receipts.summaryLineDefault"));
    if (!built.ok) {
      if (built.errorKey === "budget.receipts.candidateListChanged" || built.errorKey === "budget.receipts.bankEvidenceChanged") {
        keepActionErrorRef.current = { mode: "always" };
        if (built.errorKey === "budget.receipts.candidateListChanged") {
          setSuggestionDismissed(true);
        } else {
          onLinkChoiceTouched(detail.receipt_id, { kind: "empty" });
        }
        const currentDraft = confirmDraftRef.current;
        if (currentDraft !== null) {
          commitDraft(detail, currentCandidates, { ...currentDraft, linkChoice: "", selectedCandidate: null, keepBinding: null });
        }
      }
      setActionError(t(built.errorKey));
      return;
    }
    if (!isOpenReceipt(detail.receipt_id)) return;
    const lockToken = store.tryAcquire(detail.receipt_id, "confirm");
    if (lockToken === null) return;
    keepActionErrorRef.current = null;
    setActionError(null);
    setConflictReloadPending(false);
    let outcome: ReceiptSettledEvent["outcome"] = "error";
    let handledInMountedDrawer = false;
    let lockReleased = false;
    const releaseLock = (finalOutcome?: ReceiptSettledEvent["outcome"]) => {
      if (lockReleased) return;
      lockReleased = true;
      store.release(detail.receipt_id, lockToken, finalOutcome);
    };
    try {
      const result = await api.confirmReceipt(built.request);
      outcome = settledOutcome(result, "confirmed");
      const stillOpen = drawerMountedRef.current && isOpenReceipt(detail.receipt_id);
      if (onAccessLoss(result) || !stillOpen) return;
      handledInMountedDrawer = true;
      if (!result.ok) {
        if (result.status === 404) {
          releaseLock();
          onNotFound(detail.receipt_id);
          return;
        }
        if (isConflictOrStaleError(result)) {
          const cleanStaleTokenOnly =
            result.error === "budget_receipt_stale_source" && !hasReceiptFieldOrLineEdits(detail, draft);
          if (cleanStaleTokenOnly) {
            setActionError(confirmErrorText(result.error));
            keepActionErrorRef.current = { mode: "always" };
            onConflictOrStale(detail.receipt_id);
          } else {
            setActionError(
              result.error === "budget_receipt_tx_already_linked"
                ? t("budget.receipts.txAlreadyLinked")
                : t("budget.receipts.conflictDraftNotice"),
            );
            setConflictReloadPending(true);
          }
        } else {
          setActionError(confirmErrorText(result.error));
          keepActionErrorRef.current = {
            mode: "if_unchanged",
            receiptId: detail.receipt_id,
            version: detail.version,
            status: detail.status,
          };
          // Reload list even on network/unknown errors in case the write committed server-side.
          onMutationError();
        }
        return;
      }
      releaseLock();
      onConfirmed(result.data.receipt, { status: detail.status, version: detail.version }, options);
    } finally {
      releaseLock(handledInMountedDrawer ? undefined : outcome);
    }
  };

  const submitDelete = async (detail: ReceiptDetail, candidates: readonly ReceiptCandidate[]) => {
    if (isReceiptGrouped(detail) || !isOpenReceipt(detail.receipt_id)) return;
    const lockToken = store.tryAcquire(detail.receipt_id, "delete");
    if (lockToken === null) return;
    keepActionErrorRef.current = null;
    setActionError(null);
    setConflictReloadPending(false);
    let outcome: ReceiptSettledEvent["outcome"] = "error";
    let handledInMountedDrawer = false;
    let lockReleased = false;
    const releaseLock = (finalOutcome?: ReceiptSettledEvent["outcome"]) => {
      if (lockReleased) return;
      lockReleased = true;
      store.release(detail.receipt_id, lockToken, finalOutcome);
    };
    try {
      const result = await api.deleteReceipt({ receipt_id: detail.receipt_id, expected_version: detail.version });
      outcome = settledOutcome(result, "deleted");
      const stillOpen = drawerMountedRef.current && isOpenReceipt(detail.receipt_id);
      if (onAccessLoss(result) || !stillOpen) return;
      handledInMountedDrawer = true;
      if (!result.ok) {
        if (result.status === 404) {
          releaseLock();
          onNotFound(detail.receipt_id);
          return;
        }
        if (isConflictOrStaleError(result)) {
          const hasDirtyDraft =
            confirmDraftRef.current !== null && isReceiptDraftDirty(detail, confirmDraftRef.current, candidates);
          if (hasDirtyDraft) {
            setConfirmingDelete(false);
            setActionError(t("budget.receipts.conflictDraftNotice"));
            setConflictReloadPending(true);
          } else {
            setActionError(result.error === "budget_receipt_in_group"
              ? t("budget.receipts.inGroupConflict")
              : t("budget.receipts.conflict"));
            keepActionErrorRef.current = { mode: "always" };
            onConflictOrStale(detail.receipt_id);
          }
        } else {
          setActionError(result.error === "budget_receipt_in_group"
            ? t("budget.receipts.inGroupConflict")
            : t("budget.receipts.deleteError"));
          keepActionErrorRef.current = {
            mode: "if_unchanged",
            receiptId: detail.receipt_id,
            version: detail.version,
            status: detail.status,
          };
          // Reload list even on network/unknown errors in case the delete committed server-side.
          onMutationError();
        }
        return;
      }
      releaseLock();
      onDeleted(detail.receipt_id, false);
    } finally {
      releaseLock(handledInMountedDrawer ? undefined : outcome);
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
    keepActionErrorRef.current = null;
    setActionError(null);
    let outcome: ReceiptSettledEvent["outcome"] = "error";
    let handledInMountedDrawer = false;
    let locksReleased = false;
    const releaseLocks = (finalOutcome?: ReceiptSettledEvent["outcome"]) => {
      if (locksReleased) return;
      locksReleased = true;
      releaseGroupParticipantLocks(store, acquired, finalOutcome);
    };
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
          keepActionErrorRef.current = { mode: "always" };
          onConflictOrStale(detail.receipt_id);
          return;
        }
        setActionError(t("budget.receipts.groupUnlinkError"));
        keepActionErrorRef.current = {
          mode: "if_unchanged",
          receiptId: detail.receipt_id,
          version: detail.version,
          status: detail.status,
        };
        onMutationError();
        return;
      }
      setConfirmingDissolve(false);
      releaseLocks();
      onGroupUnlinked(detail.receipt_id);
    } finally {
      releaseLocks(handledInMountedDrawer ? undefined : outcome);
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
    keepActionErrorRef.current = null;
    setActionError(null);
    let outcome: ReceiptSettledEvent["outcome"] = "error";
    let handledInMountedDrawer = false;
    let locksReleased = false;
    const releaseLocks = (finalOutcome?: ReceiptSettledEvent["outcome"]) => {
      if (locksReleased) return;
      locksReleased = true;
      releaseGroupParticipantLocks(store, acquired, finalOutcome);
    };
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
          releaseLocks();
          onNotFound(detail.receipt_id);
          return;
        }
        setActionError(isConflictOrStaleError(result)
          ? t("budget.receipts.conflict")
          : t("budget.receipts.deleteError"));
        if (isConflictOrStaleError(result)) {
          setConfirmingDissolve(false);
          keepActionErrorRef.current = { mode: "always" };
          onConflictOrStale(detail.receipt_id);
        } else {
          keepActionErrorRef.current = {
            mode: "if_unchanged",
            receiptId: detail.receipt_id,
            version: detail.version,
            status: detail.status,
          };
          onMutationError();
        }
        return;
      }
      releaseLocks();
      onDeleted(detail.receipt_id, true);
    } finally {
      releaseLocks(handledInMountedDrawer ? undefined : outcome);
    }
  };

  const addSummaryLine = (
    detail: ReceiptDetail,
    candidates: readonly ReceiptCandidate[],
    draft: ReceiptConfirmDraft,
  ) => {
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
    mutateDraft(detail, candidates, draft, (current) => ({ ...current, lines: [...current.lines, nextLine] }));
  };

  const updateLine = (
    detail: ReceiptDetail,
    candidates: readonly ReceiptCandidate[],
    draft: ReceiptConfirmDraft,
    index: number,
    field: ReceiptLineEditField,
    value: string,
  ) => {
    if (receiptHasRedactedLines(detail.lines)) return;
    mutateDraft(detail, candidates, draft, (current) => ({
      ...current,
      lines: current.lines.map((line, idx) => (idx === index ? editReceiptLine(line, field, value) : line)),
    }));
  };

  const removeLine = (
    detail: ReceiptDetail,
    candidates: readonly ReceiptCandidate[],
    draft: ReceiptConfirmDraft,
    index: number,
  ) => {
    if (receiptHasRedactedLines(detail.lines)) return;
    mutateDraft(detail, candidates, draft, (current) => ({
      ...current,
      lines: current.lines.filter((_, idx) => idx !== index),
    }));
  };

  return (
    <div
      className="modal-overlay budget-receipt-overlay"
      onClick={(event) => {
        if (event.target === event.currentTarget && actionBusy === null && !groupControllerOpen && !manualPickerVisible) onClose();
      }}
    >
      <div className="modal-card budget-receipt-drawer" role="dialog" aria-modal="true" aria-label={title}
        inert={manualPickerVisible}>
        <header className="budget-receipt-drawer-head">
          <h3>{title}</h3>
          <div className="budget-receipt-drawer-head-actions">
            <button
              type="button"
              disabled={actionBusy !== null || groupControllerOpen || loadingMore || !canPrev}
              onClick={onPrev}
            >
              {t("budget.receipts.prevAction")}
            </button>
            <button
              type="button"
              disabled={actionBusy !== null || groupControllerOpen || loadingMore || !canNext}
              onClick={onNext}
            >
              {t("budget.receipts.nextAction")}
            </button>
            <button ref={closeButtonRef} type="button" disabled={actionBusy !== null} onClick={onClose}>
              {t("budget.main.close")}
            </button>
          </div>
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
          const canPickManually = searchTransactions !== undefined && activeDraft !== null
            && canPickManualTransaction(detail, activeDraft);
          const hasRedactedLines = receiptHasRedactedLines(detail.lines);
          const draftMethod: ReceiptPaymentMethod = activeDraft?.paymentMethod ?? "unknown";
          const matchingChanged = activeDraft !== null && hasMatchingFieldsChanged(detail, activeDraft);
          const linkOptions = availableLinkOptions(detail, candidates, draftMethod, matchingChanged);
          const { currentCandidate } = linkOptions;
          const hasStoredKeepCandidate = storedKeepCandidate !== null;
          const lineAdjustment = activeDraft === null ? null : receiptTotalAdjustment(detail, activeDraft);
          const lineDelta = activeDraft === null || lineAdjustment !== null
            || (detail.document_kind === "invoice" && detail.lines.length === 0 && activeDraft.lines.length === 0)
            ? null
            : computeLinesDelta(activeDraft.lines, activeDraft.totalAmount);
          const deltaCurrency = (activeDraft?.currency.trim() || detail.currency) ?? "PLN";
          const adjustmentCurrency = lineAdjustment?.kind === "remainder" && /^[A-Z]{3}$/i.test(deltaCurrency)
            ? new Intl.NumberFormat(locale, { style: "currency", currency: deltaCurrency })
                .formatToParts(0).find((part) => part.type === "currency")?.value ?? deltaCurrency
            : deltaCurrency;
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
                      value={receiptShopName(detail)}
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
                          const currentDraft = confirmDraftRef.current;
                          if (currentDraft !== null) {
                            commitDraft(detail, candidates, { ...currentDraft, linkChoice: "", selectedCandidate: null, keepBinding: null });
                          }
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
                          const currentDraft = confirmDraftRef.current;
                          if (currentDraft !== null) {
                            commitDraft(detail, candidates, { ...currentDraft, linkChoice: "unlink", selectedCandidate: null, keepBinding: null });
                          }
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
                {lineAdjustment === null && detail.math_delta !== null && detail.math_delta !== "0"
                  && detail.currency !== null ? (
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
                {canPickManually && activeDraft !== null && searchTransactions !== undefined ? (
                  <>
                    <div className="budget-actions">
                      <button type="button" disabled={actionBusy !== null || groupControllerOpen}
                        onClick={(event) => {
                          manualPickerTriggerRef.current = event.currentTarget;
                          setManualPickerOpen(true);
                        }}>
                        {t("budget.receipts.transactionPicker")}
                      </button>
                    </div>
                    {activeDraft.linkChoice === "manual" && activeDraft.manualTransaction !== undefined ? (
                      <label className="budget-toggle budget-receipt-candidate">
                        <input type="radio" name="link_choice" value="manual" checked readOnly />
                        <span>{transactionPickerDescription(activeDraft.manualTransaction, locale)}</span>
                      </label>
                    ) : null}
                    {manualPickerVisible ? (
                      <ReceiptTransactionPicker receiptId={detail.receipt_id} receiptDate={activeDraft.receiptDate}
                        receiptTotal={activeDraft.totalAmount} accounts={accounts} connections={connections} locale={locale}
                        search={searchTransactions} onAccessLoss={onAccessLoss} onClose={closeManualPicker}
                        onPick={(item) => {
                          onLinkChoiceTouched(detail.receipt_id, { kind: "empty" });
                          commitDraft(detail, candidates, {
                            ...activeDraft, linkChoice: "manual", manualTransaction: item,
                            selectedCandidate: null, keepBinding: null,
                          });
                          closeManualPicker();
                        }} />
                    ) : null}
                  </>
                ) : null}
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
                          updateDraftFields(detail, candidates, activeDraft, { rawShopName: event.currentTarget.value });
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
                          updateDraftFields(detail, candidates, activeDraft, { rawNip: event.currentTarget.value });
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
                          updateDraftFields(detail, candidates, activeDraft, {
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
                          updateDraftFields(detail, candidates, activeDraft, { receiptDate: event.currentTarget.value });
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
                          const nextTime = event.currentTarget.value;
                          mutateDraft(detail, candidates, activeDraft, (current) => ({ ...current, receiptTime: nextTime }));
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
                          updateDraftFields(detail, candidates, activeDraft, { totalAmount: event.currentTarget.value });
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
                          updateDraftFields(detail, candidates, activeDraft, { currency: event.currentTarget.value.toUpperCase() });
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
                          updateDraftFields(detail, candidates, activeDraft, {
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
                            onClick={() => { addSummaryLine(detail, candidates, activeDraft); }}
                          >
                            {t("budget.receipts.addSummaryLine")}
                          </button>
                        ) : (
                          <button
                            type="button"
                            disabled={actionBusy !== null || hasRedactedLines}
                            onClick={() => { addSummaryLine(detail, candidates, activeDraft); }}
                          >
                            {t("budget.receipts.addLine")}
                          </button>
                        )}
                      </div>
                    </div>
                    {hasRedactedLines && lineAdjustment?.kind !== "summary" ? (
                      <p className="budget-warn" role="note">{t("budget.receipts.redactedLinesEditBlocked")}</p>
                    ) : null}
                    {lineAdjustment === null ? null : (
                      <p className="budget-muted" role="note">
                        {lineAdjustment.kind === "remainder"
                          ? t("budget.receipts.partialLinesRemainder", {
                              amount: formatAmount(lineAdjustment.amount, adjustmentCurrency, locale),
                            })
                          : t("budget.receipts.partialLinesSummary")}
                      </p>
                    )}
                    {lineDeltaDisplay === null ? null : (
                      <p className="budget-warn">
                        {t("budget.receipts.mathDelta")}: {lineDeltaDisplay}
                      </p>
                    )}
                    {activeDraft.lines.map((line, idx) => (
                      <div
                        key={idx}
                        className="budget-receipt-line-edit"
                        data-has-discount={line.line_kind === "item" ? "true" : "false"}
                      >
                        <label className="budget-receipt-line-field-name">
                          <span className="budget-receipt-line-label">{t("budget.receipts.lineName")}</span>
                          <input
                            name="line_name"
                            type="text"
                            maxLength={128}
                            disabled={actionBusy !== null || hasRedactedLines}
                            value={line.raw_name}
                            onChange={(event) => { updateLine(detail, candidates, activeDraft, idx, "raw_name", event.currentTarget.value); }}
                          />
                        </label>
                        <label className="budget-receipt-line-field-amount">
                          <span className="budget-receipt-line-label">{t("budget.receipts.lineGross")}</span>
                          <input
                            name="line_gross"
                            type="text"
                            inputMode="decimal"
                            disabled={actionBusy !== null || hasRedactedLines}
                            value={line.gross_line_total}
                            onChange={(event) => { updateLine(detail, candidates, activeDraft, idx, "gross_line_total", event.currentTarget.value); }}
                          />
                        </label>
                        {line.line_kind === "item" ? (
                          <label className="budget-receipt-line-field-amount">
                            <span className="budget-receipt-line-label">{t("budget.receipts.lineDiscount")}</span>
                            <input
                              name="line_discount"
                              type="text"
                              inputMode="decimal"
                              disabled={actionBusy !== null || hasRedactedLines}
                              value={line.discount_amount}
                              onChange={(event) => { updateLine(detail, candidates, activeDraft, idx, "discount_amount", event.currentTarget.value); }}
                            />
                          </label>
                        ) : null}
                        <label className="budget-receipt-line-field-amount">
                          <span className="budget-receipt-line-label">{t("budget.receipts.lineAmount")}</span>
                          <input
                            name="line_net"
                            type="text"
                            inputMode="decimal"
                            disabled={actionBusy !== null || hasRedactedLines}
                            value={line.net_line_total}
                            onChange={(event) => { updateLine(detail, candidates, activeDraft, idx, "net_line_total", event.currentTarget.value); }}
                          />
                        </label>
                        <button
                          type="button"
                          className="budget-receipt-line-remove"
                          disabled={actionBusy !== null || hasRedactedLines}
                          onClick={() => { removeLine(detail, candidates, activeDraft, idx); }}
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
                    {linkOptions.mustUnlinkToConfirm && detail.status === "confirmed" && !canPickManually ? (
                      <p className="budget-warn" role="note">{t("budget.receipts.confirmedUnlinkNote")}</p>
                    ) : null}
                    {detail.status === "needs_review" && detail.linked && currentCandidate === null
                      && !matchingChanged && !canPickManually ? (
                      <p className="budget-warn" role="note">{t("budget.receipts.needsReviewNoKeepNote")}</p>
                    ) : null}
                    {tokensDisabledByEdit ? (
                      <p className="budget-warn" role="note">{t("budget.receipts.staleSourceEditHint")}</p>
                    ) : null}
                    {activeDraft.linkChoice === "" && detail.linked ? (
                      <p className="budget-warn" role="note">
                        {linkOptions.canKeep || canPickManually
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
                            commitDraft(detail, candidates, selectKeepDraft(activeDraft, currentCandidate));
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
                          commitDraft(detail, candidates, { ...activeDraft, linkChoice: "unlink", selectedCandidate: null, keepBinding: null });
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
                              commitDraft(detail, candidates, selectCandidateDraft(activeDraft, candidate, index));
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

                  {actionError === null ? null : conflictReloadPending ? (
                    <div className="budget-notice budget-entries-action" role="alert">
                      <span>{actionError}</span>
                      <button
                        type="button"
                        disabled={actionBusy !== null}
                        onClick={() => {
                          setConflictReloadPending(false);
                          setActionError(null);
                          onReloadLatestVersion(detail.receipt_id);
                        }}
                      >
                        {t("budget.receipts.loadLatestVersion")}
                      </button>
                    </div>
                  ) : (
                    <p className="budget-notice" role="alert">{actionError}</p>
                  )}

                  {unsavedPromptVisible ? (
                    <div className="budget-notice budget-entries-action budget-receipt-unsaved-prompt" role="alert">
                      <span>{t("budget.receipts.unsavedChangesPrompt")}</span>
                      <div className="budget-actions">
                        <button
                          type="button"
                          className="budget-primary"
                          disabled={actionBusy !== null || activeDraft.linkChoice === ""}
                          onClick={() => {
                            void submitConfirm(detail, candidates, activeDraft, { continuePendingNav: true });
                          }}
                        >
                          {t("budget.receipts.unsavedChangesSave")}
                        </button>
                        <button
                          type="button"
                          disabled={actionBusy !== null}
                          onClick={() => {
                            resetDraftSynchronously(detail, candidates);
                            onDiscardPendingNav();
                          }}
                        >
                          {t("budget.receipts.unsavedChangesDiscard")}
                        </button>
                      </div>
                    </div>
                  ) : null}

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
                  <label className="budget-toggle budget-receipt-auto-advance">
                    <input
                      type="checkbox"
                      name="auto_advance_on_confirm"
                      checked={autoAdvance}
                      disabled={actionBusy !== null}
                      onChange={(event) => { onAutoAdvanceChange(event.currentTarget.checked); }}
                    />
                    <span>{t("budget.receipts.autoAdvanceLabel")}</span>
                  </label>
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
                      onClick={() => { void submitDelete(detail, candidates); }}
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
