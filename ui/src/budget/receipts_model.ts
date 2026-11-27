// Pure helpers for owner-private receipts and documents. Exact decimals stay strings end to end
// (BigInt scaled-integer arithmetic only), and bank transaction lookups never emit GET query URLs.
import type { components } from "../api/openapi";
import type { MessageKey } from "../i18n/locales/en";
import { accountName, formatScaledBigInt, parseScaledInt, validIsoDate } from "./analytics/model";
import { validRange } from "./model";
import type { BudgetAccount, BudgetConnection } from "./model";

type Schemas = components["schemas"];

export type ReceiptSummary = Schemas["BudgetReceiptSummary"];
export type ReceiptDetail = Schemas["BudgetReceiptDetail"];
export type ReceiptLine = Schemas["BudgetReceiptLine"];
export type ReceiptLineInput = Schemas["BudgetReceiptLineInput"];
export type ReceiptCandidate = Schemas["BudgetReceiptCandidate"];
export type ReceiptLinkRequest = Schemas["BudgetReceiptLinkRequest"];
export type ReceiptConfirmRequest = Schemas["BudgetReceiptConfirmRequest"];
export type ReceiptConfirmResult = Schemas["BudgetReceiptConfirmResult"];
export type ReceiptTransactionSearchRequest = Schemas["BudgetReceiptTransactionSearchRequest"];
export type ReceiptTransactionSearchResult = Schemas["BudgetReceiptTransactionSearchResult"];
export type ReceiptTransactionSearchItem = Schemas["BudgetReceiptTransactionSearchItem"];
export type ReceiptDetailResult = Omit<Schemas["BudgetReceiptDetailResult"], "group_candidates"> & {
  group_candidates?: ReceiptGroupCandidate[] | null;
};
export type ReceiptListResult = Schemas["BudgetReceiptListResult"];
export type ReceiptLinkGroupRef = Schemas["BudgetReceiptLinkGroupRef"];
export type ReceiptGroupDetailResult = Schemas["BudgetReceiptGroupDetailResult"];

export type ReceiptDeleteRequest = Schemas["BudgetReceiptDeleteRequest"] & {
  dissolve_group?: boolean;
  expected_group_version?: number | null;
};

export interface ReceiptGroupTransactionCandidate {
  account_id: string;
  entry_reference: string;
  amount: string;
  currency: string;
  credit_debit_indicator: "DBIT" | "CRDT";
  transaction_date: string | null;
  booking_date: string | null;
  value_date: string | null;
  merchant_token_hit: boolean;
  match_strength: ReceiptCandidate["match_strength"];
  date_distance: number;
  evidence_fingerprint: string;
  candidate_token: string;
}

export interface ReceiptGroupCandidate {
  account_id: string;
  entry_reference: string;
  amount: string;
  currency: string;
  credit_debit_indicator: "DBIT" | "CRDT";
  transaction_date: string | null;
  booking_date: string | null;
  value_date: string | null;
  match_strength: ReceiptCandidate["match_strength"];
  date_distance: number;
  evidence_fingerprint: string;
  candidate_token: string;
  fee_amount: string | null;
  evidence_receipt_id: string | null;
  members_sum: string;
  member_receipt_ids: string[];
  member_receipts: ReceiptSummary[];
  evidence?: ReceiptSummary | null;
}

export interface ReceiptGroupCandidatesRequest {
  receipt_id: string;
}

export interface ReceiptGroupCandidatesResult {
  anchor_receipt_id: string;
  transactions: ReceiptGroupTransactionCandidate[];
  proposals: ReceiptGroupCandidate[];
  eligible_receipts: ReceiptSummary[];
  eligible_evidence: ReceiptSummary[];
  no_bank_data?: boolean | null;
}

export interface ReceiptGroupMemberInput {
  receipt_id: string;
  expected_version: number;
}

export interface ReceiptGroupLinkRequest {
  link: ReceiptLinkRequest;
  members: ReceiptGroupMemberInput[];
  evidence_receipt_id?: string | null;
  evidence_expected_version?: number | null;
  fee_amount?: string | null;
}

export interface ReceiptGroupLinkResult {
  ok: true;
  group: ReceiptLinkGroupRef;
  members: ReceiptSummary[];
  evidence: ReceiptSummary | null;
  linked_account_id: string | null;
  linked_entry_ref: string | null;
}

export interface ReceiptGroupUnlinkRequest {
  group_id: string;
  expected_version: number;
}

export interface ReceiptGroupUnlinkResult {
  ok: true;
  unlinked: true;
  group_id: string;
  members: ReceiptSummary[];
  evidence: ReceiptSummary | null;
}

export type ReceiptDeleteResult = Schemas["BudgetReceiptDeleteResult"];
export type ReceiptListRequest = Partial<Schemas["BudgetReceiptListRequest"]>;
type RawReceiptSearchRequest = Schemas["BudgetReceiptSearchRequest"];
export type ReceiptSearchRequest =
  Pick<RawReceiptSearchRequest, "query"> & Partial<Omit<RawReceiptSearchRequest, "query">>;

export type ReceiptStatus = ReceiptSummary["status"];
export type ReceiptDocumentKind = NonNullable<ReceiptSummary["document_kind"]>;
export type ReceiptSourceKind = ReceiptSummary["source_kind"];
export type ReceiptPaymentMethod = ReceiptConfirmRequest["payment_method"];
export type ReceiptKind = ReceiptConfirmRequest["receipt_kind"];
export type ReceiptLineKind = ReceiptLineInput["line_kind"];

export function isReceiptGrouped(receipt: Pick<ReceiptSummary, "link_group">): boolean {
  return receipt.link_group != null;
}

export const RECEIPT_STATUSES: readonly ReceiptStatus[] = ["imported", "needs_review", "confirmed", "draft"];
const RECEIPT_NEEDS_ACTION_STATUSES: readonly ReceiptStatus[] = ["imported", "needs_review", "draft"];
export const RECEIPT_DOCUMENT_KINDS: readonly ReceiptDocumentKind[] = ["receipt", "invoice", "payment_confirmation"];
export const RECEIPT_SOURCE_KINDS: readonly ReceiptSourceKind[] = [
  "local_ocr", "live_text", "email", "attachment", "cloud_ai", "paste", "photo",
];
export const RECEIPT_PAYMENT_METHODS: readonly ReceiptPaymentMethod[] = ["card", "blik", "cash", "mixed", "unknown"];
export const RECEIPT_KINDS: readonly ReceiptKind[] = ["purchase", "refund"];
export const RECEIPT_LINE_KINDS: readonly ReceiptLineKind[] = ["item", "deposit", "basket_discount", "other"];
export const RECEIPT_PTU_CODES = ["A", "B", "C", "D", "E", "F", "G"] as const;

/** Exact server sentinel written by `redact_digit_field` / `redact_pan` for >=12-digit privacy redactions. */
export const REDACTED_VALUE = "[usunięto]";

export function isRedactedValue(value: string | null | undefined): boolean {
  return value === REDACTED_VALUE;
}

export const RECEIPT_STATUS_KEYS: Readonly<Record<string, MessageKey>> = {
  imported: "budget.receipts.statuses.imported",
  needs_review: "budget.receipts.statuses.needs_review",
  confirmed: "budget.receipts.statuses.confirmed",
  draft: "budget.receipts.statuses.draft",
};

export const RECEIPT_DOC_KIND_KEYS: Readonly<Record<string, MessageKey>> = {
  receipt: "budget.receipts.documentKind.receipt",
  invoice: "budget.receipts.documentKind.invoice",
  payment_confirmation: "budget.receipts.documentKind.payment_confirmation",
};

export const RECEIPT_SOURCE_KIND_KEYS: Readonly<Record<string, MessageKey>> = {
  local_ocr: "budget.receipts.sourceKinds.local_ocr",
  live_text: "budget.receipts.sourceKinds.live_text",
  email: "budget.receipts.sourceKinds.email",
  attachment: "budget.receipts.sourceKinds.attachment",
  cloud_ai: "budget.receipts.sourceKinds.cloud_ai",
  paste: "budget.receipts.sourceKinds.paste",
  photo: "budget.receipts.sourceKinds.photo",
};

export const RECEIPT_PAYMENT_METHOD_KEYS: Readonly<Record<string, MessageKey>> = {
  card: "budget.receipts.paymentMethods.card",
  blik: "budget.receipts.paymentMethods.blik",
  cash: "budget.receipts.paymentMethods.cash",
  mixed: "budget.receipts.paymentMethods.mixed",
  unknown: "budget.receipts.paymentMethods.unknown",
};

export const RECEIPT_KIND_KEYS: Readonly<Record<string, MessageKey>> = {
  purchase: "budget.receipts.receiptKind.purchase",
  refund: "budget.receipts.receiptKind.refund",
};

export const RECEIPT_LINE_KIND_KEYS: Readonly<Record<string, MessageKey>> = {
  item: "budget.receipts.lineKinds.item",
  deposit: "budget.receipts.lineKinds.deposit",
  basket_discount: "budget.receipts.lineKinds.basket_discount",
  other: "budget.receipts.lineKinds.other",
};

export type ReceiptStatusFilter = "" | "needs_action" | ReceiptStatus;

export interface ReceiptFilter {
  query: string;
  status: ReceiptStatusFilter;
  documentKind: "" | ReceiptDocumentKind;
  sourceKind: "" | ReceiptSourceKind;
  linked: "" | "linked" | "unlinked";
  dateFrom: string;
  dateTo: string;
}

export const EMPTY_RECEIPT_FILTER: ReceiptFilter = {
  query: "",
  status: "",
  documentKind: "",
  sourceKind: "",
  linked: "",
  dateFrom: "",
  dateTo: "",
};

export const RECEIPT_PAGE_LIMIT = 50;
export const RECEIPT_MAX_PAGE_LIMIT = 200;

export function hasActiveReceiptFilter(filter: ReceiptFilter): boolean {
  return filter.query.trim() !== ""
    || filter.status !== ""
    || filter.documentKind !== ""
    || filter.sourceKind !== ""
    || filter.linked !== ""
    || filter.dateFrom !== ""
    || filter.dateTo !== "";
}

/** True when any active filter criterion evaluates only on already-loaded cursor pages client-side. */
export function hasClientSideFilter(filter: ReceiptFilter): boolean {
  if (filter.sourceKind !== "" || filter.dateFrom !== "" || filter.dateTo !== "") return true;
  if (filter.query.trim() !== "") {
    return filter.status !== "" || filter.documentKind !== "" || filter.linked !== "";
  }
  return false;
}

export function validReceiptDateRange(filter: Pick<ReceiptFilter, "dateFrom" | "dateTo">): boolean {
  return validRange({ from: filter.dateFrom, to: filter.dateTo });
}

/** When both bounds are valid dates inside one YYYY-MM month, narrow the server `list` call with `month`. */
export function listMonthForFilter(filter: Pick<ReceiptFilter, "dateFrom" | "dateTo">): string | undefined {
  if (!validReceiptDateRange(filter)) return undefined;
  if (filter.dateFrom === "" || filter.dateTo === "") return undefined;
  const fromMonth = filter.dateFrom.slice(0, 7);
  const toMonth = filter.dateTo.slice(0, 7);
  return fromMonth === toMonth ? fromMonth : undefined;
}

export function buildReceiptListRequest(
  filter: ReceiptFilter,
  cursor: string | null = null,
  limit: number = RECEIPT_PAGE_LIMIT,
): ReceiptListRequest {
  const month = listMonthForFilter(filter);
  const request: ReceiptListRequest = { limit };
  if (month !== undefined) request.month = month;
  if (filter.status === "needs_action") {
    request.statuses = [...RECEIPT_NEEDS_ACTION_STATUSES];
  } else if (filter.status !== "") {
    request.status = filter.status;
  }
  if (filter.documentKind !== "") request.document_kind = filter.documentKind;
  if (filter.linked !== "") request.linked = filter.linked === "linked";
  if (cursor !== null) request.cursor = cursor;
  return request;
}

/** True when a paginated list/search call rejected the supplied cursor (e.g. after 15-min cursor TTL). */
export function isStaleCursorError(result: { ok: boolean; status?: number; error?: string }): boolean {
  return !result.ok
    && result.status === 400
    && (result.error === "budget_receipt_invalid_request" || result.error === "budget_http_400");
}

export function filterReceiptRows(
  rows: readonly ReceiptSummary[],
  filter: ReceiptFilter,
  searchMode: boolean,
): ReceiptSummary[] {
  return rows.filter((row) => {
    if (searchMode) {
      if (filter.status === "needs_action") {
        if (!receiptNeedsConfirmation(row)) return false;
      } else if (filter.status !== "" && row.status !== filter.status) {
        return false;
      }
      if (filter.documentKind !== "" && row.document_kind !== filter.documentKind) return false;
      if (filter.linked !== "" && row.linked !== (filter.linked === "linked")) return false;
    }
    if (filter.sourceKind !== "" && row.source_kind !== filter.sourceKind) return false;
    if (filter.dateFrom !== "" || filter.dateTo !== "") {
      if (row.receipt_date === null) return false;
      if (filter.dateFrom !== "" && row.receipt_date < filter.dateFrom) return false;
      if (filter.dateTo !== "" && row.receipt_date > filter.dateTo) return false;
    }
    return true;
  });
}

export function mergeReceiptRows(
  existing: readonly ReceiptSummary[],
  incoming: readonly ReceiptSummary[],
): ReceiptSummary[] {
  const seen = new Set(existing.map((item) => item.receipt_id));
  const merged = [...existing];
  for (const item of incoming) {
    if (seen.has(item.receipt_id)) continue;
    seen.add(item.receipt_id);
    merged.push(item);
  }
  return merged;
}

export function candidateDisplayDate(
  candidate: Pick<ReceiptCandidate, "booking_date" | "value_date" | "transaction_date">,
): string | null {
  return candidate.booking_date ?? candidate.value_date ?? candidate.transaction_date ?? null;
}

export interface SuggestedCandidateDateInfo {
  readonly primaryDate: string | null;
  readonly bookingDayMonth: string | null;
  readonly displayDistance: number;
}

const ISO_DAY_MS = 86_400_000;

function parseIsoDayNumber(value: string | null | undefined): number | null {
  if (typeof value !== "string") return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (match === null) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const check = new Date(0);
  check.setUTCFullYear(year, month - 1, day);
  check.setUTCHours(0, 0, 0, 0);
  if (check.getUTCFullYear() !== year || check.getUTCMonth() !== month - 1 || check.getUTCDate() !== day) {
    return null;
  }
  return Math.round(check.getTime() / ISO_DAY_MS);
}

function formatIsoDayMonth(isoDate: string): string {
  const parts = isoDate.split("-");
  if (parts.length === 3 && parts[1]?.length === 2 && parts[2]?.length === 2) {
    return `${parts[2]}.${parts[1]}`;
  }
  return isoDate;
}

/** Pick the exact bank date `_date_distance` scored against and derive day offset from it. */
export function suggestedCandidateDates(
  candidate: Pick<ReceiptCandidate, "booking_date" | "value_date" | "transaction_date" | "date_distance">,
  receipt?: Pick<ReceiptDetail, "receipt_date" | "document_kind" | "due_date"> | null,
): SuggestedCandidateDateInfo {
  const orderedDates = [candidate.transaction_date, candidate.booking_date, candidate.value_date] as const;
  const targetDay = parseIsoDayNumber(receipt?.receipt_date);
  let primaryDate: string | null = null;
  if (targetDay !== null) {
    let startDay = targetDay - 4;
    let endDay = targetDay + 4;
    if (receipt?.document_kind === "invoice") {
      const dueDay = parseIsoDayNumber(receipt.due_date) ?? targetDay;
      startDay = targetDay - 1;
      endDay = Math.max(dueDay, targetDay) + 14;
    }
    let bestDistance = Number.POSITIVE_INFINITY;
    for (const rawDate of orderedDates) {
      const candidateDay = parseIsoDayNumber(rawDate);
      if (candidateDay === null || candidateDay < startDay || candidateDay > endDay) continue;
      const dist = Math.abs(candidateDay - targetDay);
      if (dist < bestDistance) {
        bestDistance = dist;
        primaryDate = rawDate;
      }
    }
  }
  primaryDate ??= candidate.transaction_date ?? candidate.booking_date ?? candidate.value_date ?? null;
  const primaryDay = parseIsoDayNumber(primaryDate);
  const displayDistance = (targetDay !== null && primaryDay !== null)
    ? Math.abs(primaryDay - targetDay)
    : candidate.date_distance;
  const bookingDayMonth = (
    primaryDate !== null
    && candidate.booking_date !== null
    && primaryDate !== candidate.booking_date
  )
    ? formatIsoDayMonth(candidate.booking_date)
    : null;
  return { primaryDate, bookingDayMonth, displayDistance };
}

export function receiptAccountLabel(
  accounts: readonly BudgetAccount[],
  connections: readonly BudgetConnection[],
  accountId: string | null,
  fallback: string,
): string {
  if (accountId === null) return fallback;
  return accountName(accounts, connections, accountId, fallback);
}

// ---- Per-status link actions, confirm validation & exact decimal arithmetic ----

export type ReceiptActionKind = "confirm" | "delete" | "group_link" | "group_unlink";

export interface ReceiptSettledEvent {
  receiptId: string;
  kind: ReceiptActionKind;
  outcome: "confirmed" | "deleted" | "group_linked" | "group_unlinked" | "not_found" | "conflict" | "error";
}

export interface PendingReceiptStore {
  snapshot: () => ReadonlyMap<string, ReceiptActionKind>;
  get: (receiptId: string) => ReceiptActionKind | null;
  tryAcquire: (receiptId: string, kind: ReceiptActionKind) => symbol | null;
  release: (receiptId: string, token: symbol, outcome?: ReceiptSettledEvent["outcome"]) => void;
  subscribe: (listener: (event?: ReceiptSettledEvent) => void) => () => void;
}

/** Shared in-memory mutation lock registry keyed by receipt_id so locks survive panel unmount/remount. */
export function createPendingReceiptStore(): PendingReceiptStore {
  const active = new Map<string, { kind: ReceiptActionKind; token: symbol }>();
  const listeners = new Set<(event?: ReceiptSettledEvent) => void>();
  const notify = (event?: ReceiptSettledEvent) => {
    for (const listener of [...listeners]) listener(event);
  };
  return {
    snapshot: () => {
      const copy = new Map<string, ReceiptActionKind>();
      for (const [id, entry] of active.entries()) copy.set(id, entry.kind);
      return copy;
    },
    get: (receiptId) => active.get(receiptId)?.kind ?? null,
    tryAcquire: (receiptId, kind) => {
      if (active.has(receiptId)) return null;
      const token = Symbol(receiptId);
      active.set(receiptId, { kind, token });
      notify();
      return token;
    },
    release: (receiptId, token, outcome) => {
      const current = active.get(receiptId);
      if (current?.token !== token) return;
      active.delete(receiptId);
      notify(outcome === undefined ? undefined : { receiptId, kind: current.kind, outcome });
    },
    subscribe: (listener) => {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
  };
}

export interface SelectedCandidateBinding {
  readonly account_id: string;
  readonly entry_reference: string;
  readonly match_strength: ReceiptCandidate["match_strength"];
  readonly evidence_fingerprint: string | null;
}

export interface KeepCandidateBinding {
  readonly account_id: string;
  readonly entry_reference: string;
  readonly match_strength: ReceiptCandidate["match_strength"];
  readonly evidence_fingerprint: string | null;
  readonly evidence_digest: string;
  readonly reviewed_token: string;
}

export interface ReceiptConfirmDraft {
  readonly receiptId: string;
  rawShopName: string;
  rawNip: string;
  receiptKind: ReceiptKind;
  receiptDate: string;
  receiptTime: string;
  totalAmount: string;
  currency: string;
  paymentMethod: ReceiptPaymentMethod;
  lines: ReceiptLineInput[];
  /** "" (explicit choice required) | "keep" | "unlink" | "manual" | "candidate:<index>" */
  linkChoice: string;
  selectedCandidate: SelectedCandidateBinding | null;
  keepBinding: KeepCandidateBinding | null;
  manualTransaction?: ReceiptTransactionSearchItem;
  readonly candidateSetKey: string;
}

function candidateEvidenceFingerprint(candidate: ReceiptCandidate): string | null {
  const extra = candidate as ReceiptCandidate & { evidence_fingerprint?: unknown };
  return typeof extra.evidence_fingerprint === "string" && extra.evidence_fingerprint !== ""
    ? extra.evidence_fingerprint
    : null;
}

export function candidateEvidenceDigest(candidate: ReceiptCandidate): string {
  return [
    candidate.account_id,
    candidate.entry_reference,
    candidate.amount,
    candidate.currency,
    candidate.credit_debit_indicator,
    candidate.transaction_date ?? "",
    candidate.booking_date ?? "",
    candidate.value_date ?? "",
    candidate.merchant_token_hit ? "1" : "0",
    candidate.match_strength,
    String(candidate.date_distance),
    candidateEvidenceFingerprint(candidate) ?? "",
  ].join("\0");
}

export function candidateSelectionBinding(candidate: ReceiptCandidate): SelectedCandidateBinding {
  return {
    account_id: candidate.account_id,
    entry_reference: candidate.entry_reference,
    match_strength: candidate.match_strength,
    evidence_fingerprint: candidateEvidenceFingerprint(candidate),
  };
}

export function candidateMatchesBinding(
  candidate: ReceiptCandidate,
  binding: SelectedCandidateBinding | null,
): boolean {
  return binding !== null
    && candidate.account_id === binding.account_id
    && candidate.entry_reference === binding.entry_reference
    && candidate.match_strength === binding.match_strength
    && candidateEvidenceFingerprint(candidate) === binding.evidence_fingerprint;
}

export function keepCandidateBinding(candidate: ReceiptCandidate): KeepCandidateBinding {
  return {
    account_id: candidate.account_id,
    entry_reference: candidate.entry_reference,
    match_strength: candidate.match_strength,
    evidence_fingerprint: candidateEvidenceFingerprint(candidate),
    evidence_digest: candidateEvidenceDigest(candidate),
    reviewed_token: candidate.candidate_token,
  };
}

export function keepEvidenceMatches(
  candidate: ReceiptCandidate | null,
  binding: KeepCandidateBinding | null,
): boolean {
  return candidate !== null
    && binding !== null
    && candidateEvidenceDigest(candidate) === binding.evidence_digest;
}

export function buildCandidateSetKey(
  detail: Pick<ReceiptDetail, "receipt_id" | "version" | "status" | "linked">,
  candidates: readonly ReceiptCandidate[],
): string {
  const parts = candidates.map(
    (cand) => `${cand.account_id}\0${cand.entry_reference}\0${cand.match_strength}\0${candidateEvidenceFingerprint(cand) ?? ""}`,
  );
  return `${detail.receipt_id}|${String(detail.version)}|${detail.status}|${detail.linked ? "1" : "0"}|${parts.join("\x1f")}`;
}

export function selectCandidateDraft(
  draft: ReceiptConfirmDraft,
  candidate: ReceiptCandidate,
  index: number,
): ReceiptConfirmDraft {
  return {
    ...draft,
    linkChoice: `candidate:${String(index)}`,
    selectedCandidate: candidateSelectionBinding(candidate),
    keepBinding: null,
  };
}

export function selectKeepDraft(
  draft: ReceiptConfirmDraft,
  currentCandidate: ReceiptCandidate | null,
): ReceiptConfirmDraft {
  return {
    ...draft,
    linkChoice: "keep",
    selectedCandidate: null,
    keepBinding: currentCandidate === null ? null : keepCandidateBinding(currentCandidate),
  };
}

export function clearStaleCandidateSelection(
  draft: ReceiptConfirmDraft,
  currentReceiptId: string,
  currentCandidateSetKey: string,
  currentKeepCandidate?: ReceiptCandidate | null,
): ReceiptConfirmDraft | null {
  if (draft.receiptId !== currentReceiptId) return null;
  if (draft.linkChoice === "keep" && currentKeepCandidate !== undefined && !keepEvidenceMatches(currentKeepCandidate, draft.keepBinding)) {
    return {
      ...draft,
      linkChoice: "",
      selectedCandidate: null,
      keepBinding: null,
    };
  }
  if (draft.candidateSetKey === currentCandidateSetKey) return draft;
  if (!draft.linkChoice.startsWith("candidate:") && draft.selectedCandidate === null && draft.linkChoice !== "keep") {
    return draft;
  }
  return {
    ...draft,
    linkChoice: "",
    selectedCandidate: null,
    keepBinding: null,
  };
}

export interface ReceiptLinkOptions {
  paymentAllowsLink: boolean;
  matchingFieldsChanged: boolean;
  canKeep: boolean;
  canPickCandidates: boolean;
  mustUnlinkToConfirm: boolean;
  currentCandidate: ReceiptCandidate | null;
}

export function canPickManualTransaction(
  detail: Pick<ReceiptDetail, "currency" | "evidence_for_receipt_id" | "link_group">,
  draft: Pick<ReceiptConfirmDraft, "currency" | "receiptKind" | "paymentMethod">,
): boolean {
  const currency = draft.currency.trim().toUpperCase();
  return !isReceiptGrouped(detail) && detail.evidence_for_receipt_id === null
    && draft.receiptKind === "purchase" && /^[A-Z]{3}$/.test(currency)
    && (detail.currency === null || currency === detail.currency)
    && draft.paymentMethod !== "cash" && draft.paymentMethod !== "mixed";
}

export function findCurrentLinkedCandidate(
  detail: Pick<ReceiptDetail, "linked" | "linked_account_id" | "linked_entry_ref">,
  candidates: readonly ReceiptCandidate[],
): ReceiptCandidate | null {
  if (!detail.linked || detail.linked_account_id === null || detail.linked_entry_ref === null) return null;
  return candidates.find(
    (item) => item.account_id === detail.linked_account_id && item.entry_reference === detail.linked_entry_ref,
  ) ?? null;
}

/** True when any link-matching receipt header field (merchant, NIP, amount, date, currency, kind) was edited. */
export function hasMatchingFieldsChanged(
  detail: Pick<ReceiptDetail, "raw_shop_name" | "raw_nip" | "total_amount" | "currency" | "receipt_date" | "receipt_kind">,
  draft: Pick<ReceiptConfirmDraft, "rawShopName" | "rawNip" | "totalAmount" | "currency" | "receiptDate" | "receiptKind">,
): boolean {
  if (draft.rawShopName.trim() !== (detail.raw_shop_name ?? "").trim()) return true;
  if (draft.rawNip.trim() !== (detail.raw_nip ?? "").trim()) return true;
  if (draft.receiptDate !== (detail.receipt_date ?? "")) return true;
  if (draft.currency.trim().toUpperCase() !== (detail.currency ?? "").toUpperCase()) return true;
  if (draft.receiptKind !== detail.receipt_kind) return true;
  const origNorm = detail.total_amount === null ? null : normalizeDecimalInput(detail.total_amount);
  const draftNorm = normalizeDecimalInput(draft.totalAmount);
  if (origNorm !== null && draftNorm !== null) return origNorm !== draftNorm;
  return draft.totalAmount.trim() !== (detail.total_amount ?? "").trim();
}

/** Models the exact link transitions `receipt_store.confirm` and `receipt_store.detail` support per status. */
export function availableLinkOptions(
  detail: Pick<ReceiptDetail, "status" | "linked" | "linked_account_id" | "linked_entry_ref" | "link_group">,
  candidates: readonly ReceiptCandidate[],
  paymentMethod: ReceiptPaymentMethod,
  matchingFieldsChanged = false,
): ReceiptLinkOptions {
  const grouped = isReceiptGrouped(detail);
  const paymentAllowsLink = !grouped && paymentMethod !== "cash" && paymentMethod !== "mixed";
  if (grouped || detail.status === "confirmed") {
    return {
      paymentAllowsLink,
      matchingFieldsChanged,
      canKeep: false,
      canPickCandidates: false,
      mustUnlinkToConfirm: !grouped && detail.linked,
      currentCandidate: null,
    };
  }

  const tokensValid = paymentAllowsLink && !matchingFieldsChanged;
  const currentCandidate = findCurrentLinkedCandidate(detail, candidates);

  const canKeep = tokensValid && detail.linked && currentCandidate !== null;
  return {
    paymentAllowsLink,
    matchingFieldsChanged,
    canKeep,
    canPickCandidates: tokensValid && candidates.length > 0,
    mustUnlinkToConfirm: detail.linked && !canKeep,
    currentCandidate,
  };
}

export function isStrongMatchStrength(strength: ReceiptCandidate["match_strength"]): boolean {
  return strength === "id" || strength === "nip" || strength === "distinctive";
}

/**
 * Preselects the single candidate only when the receipt is unlinked (`imported` / `draft` / `needs_review`
 * with no stored link), payment allows linking, there is exactly one candidate, and `match_strength` is
 * `"nip"` or `"distinctive"`.
 */
export function preselectedCandidate(
  detail: Pick<ReceiptDetail, "status" | "linked" | "linked_account_id" | "linked_entry_ref" | "link_group">,
  candidates: readonly ReceiptCandidate[],
  paymentMethod: ReceiptPaymentMethod,
): ReceiptCandidate | null {
  if (detail.status !== "imported" && detail.status !== "draft" && detail.status !== "needs_review") {
    return null;
  }
  if (isReceiptGrouped(detail) || detail.linked || detail.linked_account_id !== null || detail.linked_entry_ref !== null) {
    return null;
  }
  if (paymentMethod === "cash" || paymentMethod === "mixed") {
    return null;
  }
  if (candidates.length !== 1) {
    return null;
  }
  const sole = candidates[0];
  if (sole === undefined || !isStrongMatchStrength(sole.match_strength)) {
    return null;
  }
  return sole;
}

export type ReceiptLinkExplicitChoice =
  | { kind: "empty" }
  | { kind: "unlink" }
  | { kind: "keep"; keepBinding: KeepCandidateBinding | null }
  | {
      kind: "candidate";
      account_id: string;
      entry_reference: string;
      match_strength: ReceiptCandidate["match_strength"];
      evidence_fingerprint: string | null;
    };

export interface ReceiptLinkSessionEntry {
  touched: boolean;
  choice: ReceiptLinkExplicitChoice | null;
  baselineVersion: number;
  baselineStatus: ReceiptDetail["status"];
  baselineLinked: boolean;
  baselineCandidateKeys: readonly string[];
  candidateSetChanged: boolean;
}

export function candidateIdentityKeys(candidates: readonly ReceiptCandidate[]): string[] {
  return candidates.map(
    (cand) => `${cand.account_id}:${cand.entry_reference}:${cand.match_strength}:${candidateEvidenceFingerprint(cand) ?? ""}`,
  );
}

export function sameCandidateIdentitySet(a: readonly string[], b: readonly string[]): boolean {
  if (a.length !== b.length) return false;
  const sortedA = [...a].sort();
  const sortedB = [...b].sort();
  for (let i = 0; i < sortedA.length; i += 1) {
    if (sortedA[i] !== sortedB[i]) return false;
  }
  return true;
}

function defaultAmbiguousOrUnlink(
  detail: Pick<ReceiptDetail, "linked">,
  options: Pick<ReceiptLinkOptions, "canPickCandidates">,
): string {
  return detail.linked || options.canPickCandidates ? "" : "unlink";
}

function resolveTouchedLinkChoice(
  choice: ReceiptLinkExplicitChoice | null,
  detail: Pick<ReceiptDetail, "status" | "linked">,
  options: ReceiptLinkOptions,
  candidates: readonly ReceiptCandidate[],
): {
  linkChoice: string;
  selectedCandidate: SelectedCandidateBinding | null;
  keepBinding: KeepCandidateBinding | null;
  keepEvidenceInvalidated: boolean;
} {
  if (choice === null) {
    return {
      linkChoice: defaultAmbiguousOrUnlink(detail, options),
      selectedCandidate: null,
      keepBinding: null,
      keepEvidenceInvalidated: false,
    };
  }
  if (choice.kind === "unlink") {
    return { linkChoice: "unlink", selectedCandidate: null, keepBinding: null, keepEvidenceInvalidated: false };
  }
  if (choice.kind === "empty") {
    return {
      linkChoice: !options.paymentAllowsLink && !detail.linked ? "unlink" : "",
      selectedCandidate: null,
      keepBinding: null,
      keepEvidenceInvalidated: false,
    };
  }
  if (choice.kind === "keep") {
    if (options.canKeep && keepEvidenceMatches(options.currentCandidate, choice.keepBinding)) {
      return { linkChoice: "keep", selectedCandidate: null, keepBinding: choice.keepBinding, keepEvidenceInvalidated: false };
    }
    const fallbackChoice = !options.paymentAllowsLink && !detail.linked ? "unlink" : "";
    const invalidated = choice.keepBinding !== null && detail.status === "needs_review" && detail.linked;
    return { linkChoice: fallbackChoice, selectedCandidate: null, keepBinding: null, keepEvidenceInvalidated: invalidated };
  }
  const idx = candidates.findIndex((cand) => candidateMatchesBinding(cand, choice));
  const matched = idx >= 0 ? candidates[idx] : undefined;
  if (options.canPickCandidates && matched !== undefined) {
    return {
      linkChoice: `candidate:${String(idx)}`,
      selectedCandidate: candidateSelectionBinding(matched),
      keepBinding: null,
      keepEvidenceInvalidated: false,
    };
  }
  if ((!options.paymentAllowsLink || detail.status === "confirmed") && !detail.linked) {
    return { linkChoice: "unlink", selectedCandidate: null, keepBinding: null, keepEvidenceInvalidated: false };
  }
  return { linkChoice: "", selectedCandidate: null, keepBinding: null, keepEvidenceInvalidated: false };
}

/**
 * Builds the drawer draft for a receipt while preserving the user's explicit link choice across
 * reloads/refreshes in this session and never re-applying preselection after interaction or
 * candidate-set changes.
 */
export function resolveDraftFromDetailSession(
  detail: ReceiptDetail,
  candidates: readonly ReceiptCandidate[],
  previous: ReceiptLinkSessionEntry | undefined,
): {
  draft: ReceiptConfirmDraft;
  nextSessionEntry: ReceiptLinkSessionEntry;
  showSuggestedBanner: boolean;
  keepEvidenceInvalidated: boolean;
} {
  const currentKeys = candidateIdentityKeys(candidates);
  const serverStateChanged = previous !== undefined && (
    previous.baselineVersion !== detail.version
    || previous.baselineStatus !== detail.status
    || previous.baselineLinked !== detail.linked
  );
  const nextSessionEntry: ReceiptLinkSessionEntry = previous === undefined
    ? {
        touched: false,
        choice: null,
        baselineVersion: detail.version,
        baselineStatus: detail.status,
        baselineLinked: detail.linked,
        baselineCandidateKeys: currentKeys,
        candidateSetChanged: false,
      }
    : {
        ...previous,
        choice: serverStateChanged ? null : previous.choice,
        baselineVersion: detail.version,
        baselineStatus: detail.status,
        baselineLinked: detail.linked,
        baselineCandidateKeys: serverStateChanged ? currentKeys : previous.baselineCandidateKeys,
        candidateSetChanged:
          previous.candidateSetChanged
          || !sameCandidateIdentitySet(previous.baselineCandidateKeys, currentKeys),
      };
  const baseDraft = draftFromDetail(detail, candidates);
  if (nextSessionEntry.touched) {
    const paymentMethod = baseDraft.paymentMethod;
    const options = availableLinkOptions(detail, candidates, paymentMethod, false);
    const touched = resolveTouchedLinkChoice(nextSessionEntry.choice, detail, options, candidates);
    const sanitizedEntry: ReceiptLinkSessionEntry = touched.keepEvidenceInvalidated
      ? { ...nextSessionEntry, choice: { kind: "empty" } }
      : nextSessionEntry;
    return {
      draft: {
        ...baseDraft,
        linkChoice: touched.linkChoice,
        selectedCandidate: touched.selectedCandidate,
        keepBinding: touched.keepBinding,
      },
      nextSessionEntry: sanitizedEntry,
      showSuggestedBanner: false,
      keepEvidenceInvalidated: touched.keepEvidenceInvalidated,
    };
  }
  if (nextSessionEntry.candidateSetChanged) {
    const options = availableLinkOptions(detail, candidates, baseDraft.paymentMethod, false);
    return {
      draft: {
        ...baseDraft,
        linkChoice: defaultAmbiguousOrUnlink(detail, options),
        selectedCandidate: null,
        keepBinding: null,
      },
      nextSessionEntry,
      showSuggestedBanner: false,
      keepEvidenceInvalidated: false,
    };
  }
  const canPreselect = baseDraft.linkChoice === "candidate:0"
    && preselectedCandidate(detail, candidates, baseDraft.paymentMethod) !== null;
  return {
    draft: baseDraft,
    nextSessionEntry,
    showSuggestedBanner: canPreselect,
    keepEvidenceInvalidated: false,
  };
}

export function draftFromDetail(
  detail: ReceiptDetail,
  candidates: readonly ReceiptCandidate[] = [],
): ReceiptConfirmDraft {
  const paymentMethod: ReceiptPaymentMethod =
    detail.payment_method !== null && (RECEIPT_PAYMENT_METHODS as readonly string[]).includes(detail.payment_method)
      ? (detail.payment_method as ReceiptPaymentMethod)
      : "unknown";
  const options = availableLinkOptions(detail, candidates, paymentMethod, false);
  const preselected = preselectedCandidate(detail, candidates, paymentMethod);
  let linkChoice = "unlink";
  let selectedCandidate: SelectedCandidateBinding | null = null;
  if (preselected !== null && options.canPickCandidates) {
    linkChoice = "candidate:0";
    selectedCandidate = candidateSelectionBinding(preselected);
  } else if (detail.linked || options.canPickCandidates) {
    // Ambiguous link state (changed/review link, confirmed re-edit, multiple or weak candidates):
    // leave linkChoice empty so Confirm stays disabled until the user explicitly chooses.
    linkChoice = "";
  }
  return {
    receiptId: detail.receipt_id,
    rawShopName: detail.raw_shop_name ?? "",
    rawNip: detail.raw_nip ?? "",
    receiptKind: detail.receipt_kind,
    receiptDate: detail.receipt_date ?? "",
    receiptTime: detail.receipt_time ?? "",
    totalAmount: detail.total_amount ?? "",
    currency: detail.currency ?? "PLN",
    paymentMethod,
    lines: detail.lines.map((line) => ({
      line_kind: line.line_kind,
      raw_name: line.raw_name,
      ptu_code: line.ptu_code,
      quantity: line.quantity,
      raw_unit: line.raw_unit,
      unit_price_raw: line.unit_price_raw,
      gross_line_total: line.gross_line_total,
      discount_amount: line.discount_amount,
      net_line_total: line.net_line_total,
    })),
    linkChoice,
    selectedCandidate,
    keepBinding: null,
    candidateSetKey: buildCandidateSetKey(detail, candidates),
  };
}

function canonicalDraftSnapshot(draft: ReceiptConfirmDraft): string {
  return JSON.stringify({
    receiptId: draft.receiptId,
    rawShopName: draft.rawShopName,
    rawNip: draft.rawNip,
    receiptKind: draft.receiptKind,
    receiptDate: draft.receiptDate,
    receiptTime: draft.receiptTime,
    totalAmount: draft.totalAmount,
    currency: draft.currency,
    paymentMethod: draft.paymentMethod,
    lines: draft.lines.map((line) => ({
      line_kind: line.line_kind,
      raw_name: line.raw_name,
      ptu_code: line.ptu_code,
      quantity: line.quantity,
      raw_unit: line.raw_unit,
      unit_price_raw: line.unit_price_raw,
      gross_line_total: line.gross_line_total,
      discount_amount: line.discount_amount,
      net_line_total: line.net_line_total,
    })),
    linkChoice: draft.linkChoice,
    manualTransactionId: draft.linkChoice === "manual" ? draft.manualTransaction?.id ?? null : null,
    selectedCandidate: draft.selectedCandidate === null
      ? null
      : {
          account_id: draft.selectedCandidate.account_id,
          entry_reference: draft.selectedCandidate.entry_reference,
          match_strength: draft.selectedCandidate.match_strength,
          evidence_fingerprint: draft.selectedCandidate.evidence_fingerprint,
        },
    keepBinding: draft.keepBinding === null
      ? null
      : {
          account_id: draft.keepBinding.account_id,
          entry_reference: draft.keepBinding.entry_reference,
          match_strength: draft.keepBinding.match_strength,
          evidence_fingerprint: draft.keepBinding.evidence_fingerprint,
          evidence_digest: draft.keepBinding.evidence_digest,
        },
  });
}

/**
 * True when the full editable draft (header fields, lines, link choice, candidate selection,
 * and keep binding) differs from the clean server snapshot derived from `detail` and `candidates`.
 */
export function isReceiptDraftDirty(
  detail: ReceiptDetail,
  draft: ReceiptConfirmDraft,
  candidates: readonly ReceiptCandidate[] = [],
): boolean {
  if (draft.receiptId !== detail.receipt_id) return false;
  return canonicalDraftSnapshot(draft) !== canonicalDraftSnapshot(draftFromDetail(detail, candidates));
}

/**
 * True when header fields or lines differ from `detail`, ignoring link/candidate selection changes
 * (used only to distinguish stale candidate token 409s from unsaved receipt field/line edits).
 */
export function hasReceiptFieldOrLineEdits(
  detail: ReceiptDetail,
  draft: ReceiptConfirmDraft,
): boolean {
  if (draft.receiptId !== detail.receipt_id) return false;
  const baseline = draftFromDetail(detail, []);
  return canonicalDraftSnapshot({
    ...draft,
    linkChoice: baseline.linkChoice,
    selectedCandidate: baseline.selectedCandidate,
    keepBinding: baseline.keepBinding,
  }) !== canonicalDraftSnapshot(baseline);
}

/** Keeps link choices valid after field edits: disables stale tokens without silently picking unlink. */
export function reconcileLinkChoice(
  detail: ReceiptDetail,
  prevChoice: string,
  nextDraft: Pick<ReceiptConfirmDraft, "rawShopName" | "rawNip" | "totalAmount" | "currency" | "receiptDate" | "receiptKind" | "paymentMethod">,
  candidates: readonly ReceiptCandidate[],
): string {
  if (prevChoice === "unlink") return "unlink";
  if (prevChoice === "manual") return canPickManualTransaction(detail, nextDraft) ? "manual" : "";
  const changed = hasMatchingFieldsChanged(detail, nextDraft);
  const nextOptions = availableLinkOptions(detail, candidates, nextDraft.paymentMethod, changed);
  if (prevChoice === "keep") {
    return nextOptions.canKeep ? "keep" : "";
  }
  if (prevChoice.startsWith("candidate:")) {
    return nextOptions.canPickCandidates ? prevChoice : "";
  }
  return "";
}

export function validPolishNip(nip: string): boolean {
  if (!/^[0-9]{10}$/.test(nip)) return false;
  const weights = [6, 5, 7, 2, 3, 4, 5, 6, 7];
  let sum = 0;
  for (let i = 0; i < 9; i += 1) {
    sum += Number(nip[i]) * (weights[i] ?? 0);
  }
  return sum % 11 === Number(nip[9]);
}

export function normalizeDecimalInput(raw: string): string | null {
  const trimmed = raw.trim().replace(",", ".");
  if (trimmed === "" || trimmed.length > 80 || !/^-?\d+(?:\.\d+)?$/.test(trimmed)) return null;
  const parsed = parseScaledInt(trimmed);
  if (parsed === null || parsed.scale > 4) return null;
  const body = parsed.coeff.toString();
  const intDigits = Math.max(1, body.length - parsed.scale);
  if (intDigits > 9) return null;
  const signed = parsed.negative ? -parsed.coeff : parsed.coeff;
  return formatScaledBigInt(signed, parsed.scale);
}

export function addDecimals(values: readonly string[]): string | null {
  let maxScale = 0;
  const parsedList: { neg: boolean; coeff: bigint; scale: number }[] = [];
  for (const raw of values) {
    const parsed = parseScaledInt(raw.trim());
    if (parsed === null) return null;
    if (parsed.scale > maxScale) maxScale = parsed.scale;
    parsedList.push({ neg: parsed.negative, coeff: parsed.coeff, scale: parsed.scale });
  }
  let sum = 0n;
  for (const item of parsedList) {
    const factor = 10n ** BigInt(maxScale - item.scale);
    const aligned = (item.neg ? -item.coeff : item.coeff) * factor;
    sum += aligned;
  }
  return formatScaledBigInt(sum, maxScale);
}

export function subtractDecimals(a: string, b: string): string | null {
  const left = parseScaledInt(a.trim());
  const right = parseScaledInt(b.trim());
  if (left === null || right === null) return null;
  const scale = Math.max(left.scale, right.scale);
  const leftVal = (left.negative ? -left.coeff : left.coeff) * (10n ** BigInt(scale - left.scale));
  const rightVal = (right.negative ? -right.coeff : right.coeff) * (10n ** BigInt(scale - right.scale));
  return formatScaledBigInt(leftVal - rightVal, scale);
}

function compareDecimalStrings(a: string, b: string): number | null {
  const left = parseScaledInt(a);
  const right = parseScaledInt(b);
  if (left === null || right === null) return null;
  const scale = Math.max(left.scale, right.scale);
  const lv = (left.negative ? -left.coeff : left.coeff) * (10n ** BigInt(scale - left.scale));
  const rv = (right.negative ? -right.coeff : right.coeff) * (10n ** BigInt(scale - right.scale));
  return lv < rv ? -1 : lv > rv ? 1 : 0;
}

export type ReceiptLineEditField = "raw_name" | "gross_line_total" | "discount_amount" | "net_line_total";

/**
 * Edits one receipt line field while keeping `net = gross - discount` consistent with `receipt_parser`.
 * Reformatting a field with an unchanged decimal value leaves the other fields untouched.
 */
export function editReceiptLine(
  line: ReceiptLineInput,
  field: ReceiptLineEditField,
  value: string,
): ReceiptLineInput {
  if (field === "raw_name") return { ...line, raw_name: value };

  if (field === "gross_line_total") {
    const nextNorm = normalizeDecimalInput(value);
    if (nextNorm === null) return { ...line, gross_line_total: value };
    const discNorm = normalizeDecimalInput(line.discount_amount) ?? "0";
    const netNorm = normalizeDecimalInput(line.net_line_total);
    const impliedGross = netNorm === null ? null : addDecimals([netNorm, discNorm]);
    if (normalizeDecimalInput(line.gross_line_total) === nextNorm || impliedGross === nextNorm) {
      return { ...line, gross_line_total: value };
    }
    if (line.line_kind !== "item") {
      return { ...line, gross_line_total: value, discount_amount: "0", net_line_total: nextNorm };
    }
    const newNet = subtractDecimals(nextNorm, discNorm);
    return { ...line, gross_line_total: value, net_line_total: newNet ?? line.net_line_total };
  }

  if (field === "discount_amount") {
    if (line.line_kind !== "item") return { ...line, discount_amount: "0" };
    const nextNorm = normalizeDecimalInput(value);
    if (nextNorm === null) return { ...line, discount_amount: value };
    const grossNorm = normalizeDecimalInput(line.gross_line_total);
    const netNorm = normalizeDecimalInput(line.net_line_total);
    const impliedDisc = grossNorm === null || netNorm === null ? null : subtractDecimals(grossNorm, netNorm);
    if (normalizeDecimalInput(line.discount_amount) === nextNorm || impliedDisc === nextNorm) {
      return { ...line, discount_amount: value };
    }
    const newNet = grossNorm === null ? null : subtractDecimals(grossNorm, nextNorm);
    return { ...line, discount_amount: value, net_line_total: newNet ?? line.net_line_total };
  }

  // Editing net_line_total: never wipe discount; adjust gross = net + discount when the value actually changes.
  const nextNorm = normalizeDecimalInput(value);
  if (nextNorm === null) return { ...line, net_line_total: value };
  const grossNorm = normalizeDecimalInput(line.gross_line_total);
  const discNorm = normalizeDecimalInput(line.discount_amount) ?? "0";
  const impliedNet = grossNorm === null ? null : subtractDecimals(grossNorm, discNorm);
  if (normalizeDecimalInput(line.net_line_total) === nextNorm || impliedNet === nextNorm) {
    return { ...line, net_line_total: value };
  }
  if (line.line_kind !== "item") {
    return { ...line, gross_line_total: nextNorm, discount_amount: "0", net_line_total: value };
  }
  const newGross = addDecimals([nextNorm, discNorm]);
  return { ...line, gross_line_total: newGross ?? line.gross_line_total, net_line_total: value };
}

export function computeLinesDelta(lines: readonly ReceiptLineInput[], totalAmount: string): string | null {
  const total = normalizeDecimalInput(totalAmount);
  if (total === null) return null;
  const normalizedNets: string[] = [];
  for (const line of lines) {
    const net = normalizeDecimalInput(line.net_line_total);
    if (net === null) return null;
    normalizedNets.push(net);
  }
  const netSum = addDecimals(normalizedNets);
  if (netSum === null) return null;
  return subtractDecimals(netSum, total);
}

const MAX_RECEIPT_LINES = 300;

type ReceiptTotalAdjustment = { kind: "remainder"; amount: string } | { kind: "summary" };

type ConfirmLineError = "budget.receipts.invalidForm" | "budget.receipts.totalMismatch";
type ConfirmLinePlan =
  | { ok: true; lines: ReceiptLineInput[] | undefined; adjustment: ReceiptTotalAdjustment | null }
  | { ok: false; errorKey: ConfirmLineError | "budget.receipts.redactedLinesEditBlocked" };

function normalizeConfirmMoney(raw: string): string | null {
  const amount = normalizeDecimalInput(raw);
  if (amount === null) return null;
  // The server rejects money fields that look like private identifiers, including a valid PESEL.
  const digits = Array.from(amount.replace(/[-.]/g, ""), Number);
  if (digits.length >= 12) return null;
  if (digits.length === 11) {
    const weights = [1, 3, 7, 9, 1, 3, 7, 9, 1, 3];
    const checksum = weights.reduce((sum, weight, index) => sum + weight * (digits[index] ?? 0), 0);
    if ((10 - checksum % 10) % 10 === digits[10]) return null;
  }
  return amount;
}

function validConfirmLineText(text: string): boolean {
  if (/[\p{Cf}\p{Cs}]/u.test(text)) return false;
  for (const char of text) {
    const code = char.charCodeAt(0);
    if ((code < 32 && char !== "\t" && char !== "\n" && char !== "\r") || code === 127) return false;
  }
  return true;
}

function cleanConfirmLine(input: ReceiptLineInput): ReceiptLineInput | ConfirmLineError {
  const rawName = input.raw_name.normalize("NFC").trim().replace(/\s+/g, " ");
  const rawUnit = input.raw_unit?.normalize("NFC").trim().replace(/\s+/g, " ") || null;
  const nameKey = rawName.normalize("NFD").replace(/\p{Mn}/gu, "").trim();
  const quantity = normalizeConfirmMoney(input.quantity);
  const unitPrice = input.unit_price_raw === null ? null : normalizeConfirmMoney(input.unit_price_raw);
  if (nameKey === "" || Array.from(rawName).length > 128 || Array.from(nameKey).length > 128
    || !validConfirmLineText(input.raw_name) || (rawUnit !== null && Array.from(rawUnit).length > 16)
    || (input.raw_unit !== null && !validConfirmLineText(input.raw_unit))
    || (input.ptu_code !== null && !RECEIPT_PTU_CODES.includes(input.ptu_code))
    || quantity === null || (input.unit_price_raw !== null && unitPrice === null)) {
    return "budget.receipts.invalidForm";
  }

  // The server redacts fields carrying 12 digits individually and rejects split identifiers.
  const nameDigits = rawName.normalize("NFKC").match(/\p{Nd}/gu)?.length ?? 0;
  const unitDigits = rawUnit?.normalize("NFKC").match(/\p{Nd}/gu)?.length ?? 0;
  if (nameDigits < 12 && unitDigits < 12 && nameDigits + unitDigits >= 12) {
    return "budget.receipts.invalidForm";
  }

  const gross = normalizeConfirmMoney(input.gross_line_total);
  const discount = normalizeConfirmMoney(input.discount_amount);
  const net = normalizeConfirmMoney(input.net_line_total);
  if (gross === null || discount === null || net === null) return "budget.receipts.invalidForm";
  if (subtractDecimals(gross, discount) !== net) return "budget.receipts.totalMismatch";

  // receipt_parser._validate_line_totals applies these same rules to purchases and refunds.
  switch (input.line_kind) {
    case "item":
      if ([gross, discount, net].some((amount) => amount.startsWith("-"))) return "budget.receipts.totalMismatch";
      break;
    case "deposit":
      if (gross === "0" || discount !== "0") return "budget.receipts.totalMismatch";
      break;
    case "basket_discount":
      if (!gross.startsWith("-") || discount !== "0") return "budget.receipts.totalMismatch";
      break;
    case "other":
      if (discount !== "0") return "budget.receipts.totalMismatch";
      break;
    default:
      return "budget.receipts.invalidForm";
  }
  return {
    line_kind: input.line_kind,
    raw_name: rawName,
    ptu_code: input.ptu_code,
    quantity,
    raw_unit: rawUnit,
    unit_price_raw: unitPrice,
    gross_line_total: gross,
    discount_amount: discount,
    net_line_total: net,
  };
}

export function receiptTotalAdjustment(
  detail: Pick<ReceiptDetail, "document_kind" | "lines">,
  draft: Pick<ReceiptConfirmDraft, "totalAmount" | "lines">,
): ReceiptTotalAdjustment | null {
  const total = normalizeConfirmMoney(draft.totalAmount);
  if (total === null || total === "0" || total.startsWith("-")) return null;
  const plan = planConfirmLines(detail, draft.lines, total);
  return plan.ok ? plan.adjustment : null;
}

export function receiptHasRedactedLines(
  lines: readonly Pick<ReceiptLineInput, "raw_name" | "raw_unit">[],
): boolean {
  return lines.some((line) => isRedactedValue(line.raw_name.trim().normalize("NFC"))
    || isRedactedValue(line.raw_unit?.trim().normalize("NFC")));
}

/** True when normalized confirmable line values differ from stored detail lines (pure decimal reformatting counts as unchanged). */
export function haveConfirmLinesChanged(
  detailLines: readonly ReceiptLine[],
  draftLines: readonly ReceiptLineInput[],
): boolean {
  if (detailLines.length !== draftLines.length) return true;
  for (let i = 0; i < detailLines.length; i += 1) {
    const orig = detailLines[i];
    const next = draftLines[i];
    if (orig === undefined || next === undefined) return true;
    if (orig.line_kind !== next.line_kind) return true;
    if (orig.raw_name.trim() !== next.raw_name.trim()) return true;
    if (orig.ptu_code !== next.ptu_code) return true;
    const origUnit = orig.raw_unit === null ? null : (orig.raw_unit.trim() || null);
    const nextUnit = next.raw_unit === null ? null : (next.raw_unit.trim() || null);
    if (origUnit !== nextUnit) return true;
    if (normalizeDecimalInput(orig.quantity) !== normalizeDecimalInput(next.quantity)) return true;
    const origUnitPrice = orig.unit_price_raw === null ? null : normalizeDecimalInput(orig.unit_price_raw);
    const nextUnitPrice = next.unit_price_raw === null ? null : normalizeDecimalInput(next.unit_price_raw);
    if (origUnitPrice !== nextUnitPrice) return true;
    if (normalizeDecimalInput(orig.gross_line_total) !== normalizeDecimalInput(next.gross_line_total)) return true;
    if (normalizeDecimalInput(orig.discount_amount) !== normalizeDecimalInput(next.discount_amount)) return true;
    if (normalizeDecimalInput(orig.net_line_total) !== normalizeDecimalInput(next.net_line_total)) return true;
  }
  return false;
}

function synthesizeTotalLine(rawName: string, amount: string, lineKind: "item" | "other"): ReceiptLineInput {
  return {
    line_kind: lineKind,
    raw_name: rawName,
    ptu_code: null,
    quantity: "1",
    raw_unit: null,
    unit_price_raw: null,
    gross_line_total: amount,
    discount_amount: "0",
    net_line_total: amount,
  };
}

function planConfirmLines(
  detail: Pick<ReceiptDetail, "document_kind" | "lines">,
  draftLines: readonly ReceiptLineInput[],
  total: string,
): ConfirmLinePlan {
  const allowsFallback = detail.document_kind !== "payment_confirmation";
  const summary: ConfirmLinePlan = {
    ok: true,
    lines: [synthesizeTotalLine("Tylko suma", total, "other")],
    adjustment: { kind: "summary" },
  };
  const hasRedactedLines = receiptHasRedactedLines(detail.lines) || receiptHasRedactedLines(draftLines);
  if (hasRedactedLines && haveConfirmLinesChanged(detail.lines, draftLines)) {
    return { ok: false, errorKey: "budget.receipts.redactedLinesEditBlocked" };
  }
  if (draftLines.length > MAX_RECEIPT_LINES) {
    return allowsFallback ? summary : { ok: false, errorKey: "budget.receipts.invalidForm" };
  }

  const cleanLines: ReceiptLineInput[] = [];
  for (const line of draftLines) {
    const clean = cleanConfirmLine(line);
    if (typeof clean === "string") return allowsFallback ? summary : { ok: false, errorKey: clean };
    cleanLines.push(clean);
  }

  if (detail.document_kind === "invoice" && detail.lines.length === 0 && cleanLines.length === 0) {
    return { ok: true, lines: undefined, adjustment: null };
  }
  const delta = computeLinesDelta(cleanLines, total);
  if (delta === "0") {
    const sendLines = !hasRedactedLines && haveConfirmLinesChanged(detail.lines, draftLines);
    return { ok: true, lines: sendLines ? cleanLines : undefined, adjustment: null };
  }
  if (!allowsFallback) return { ok: false, errorKey: "budget.receipts.totalMismatch" };

  if (!hasRedactedLines && delta?.startsWith("-") && cleanLines.length < MAX_RECEIPT_LINES) {
    const amount = normalizeConfirmMoney(delta.slice(1));
    if (amount !== null) {
      return {
        ok: true,
        lines: [...cleanLines, synthesizeTotalLine("Pozostałe / nierozpoznane pozycje", amount, "item")],
        adjustment: { kind: "remainder", amount },
      };
    }
  }
  return summary;
}

function synthesizeInvoiceSummaryLine(
  detail: Pick<ReceiptDetail, "invoice_number" | "raw_shop_name">,
  draftShopName: string,
  normalizedTotal: string,
  summaryFallbackName: string,
): ReceiptLineInput {
  const invoiceName = detail.invoice_number !== null && !isRedactedValue(detail.invoice_number)
    ? detail.invoice_number.trim()
    : "";
  const shopTrimmed = draftShopName.trim();
  const rawShop = shopTrimmed !== "" && !isRedactedValue(shopTrimmed)
    ? shopTrimmed
    : (detail.raw_shop_name !== null && !isRedactedValue(detail.raw_shop_name)
      ? detail.raw_shop_name.trim()
      : "");
  const candidateName = invoiceName || rawShop || summaryFallbackName;
  const rawName = candidateName === "" || isRedactedValue(candidateName) ? "Document" : candidateName;
  return synthesizeTotalLine(rawName, normalizedTotal, "other");
}

export function buildConfirmRequest(
  detail: ReceiptDetail,
  draft: ReceiptConfirmDraft,
  candidates: readonly ReceiptCandidate[],
  summaryFallbackName = "Document",
): { ok: true; request: ReceiptConfirmRequest } | { ok: false; errorKey: MessageKey } {
  if (isReceiptGrouped(detail)) return { ok: false, errorKey: "budget.receipts.invalidForm" };
  const origShop = (detail.raw_shop_name ?? "").trim();
  const shopName = draft.rawShopName.trim();
  const shopEdited = shopName !== origShop;
  if (shopName.length > 128) return { ok: false, errorKey: "budget.receipts.invalidForm" };
  if (shopEdited && isRedactedValue(shopName)) return { ok: false, errorKey: "budget.receipts.invalidForm" };
  const origNip = (detail.raw_nip ?? "").trim();
  const nip = draft.rawNip.trim();
  const nipEdited = nip !== origNip;
  if (nipEdited && isRedactedValue(nip)) return { ok: false, errorKey: "budget.receipts.invalidForm" };
  if (nipEdited && nip !== "" && !validPolishNip(nip)) return { ok: false, errorKey: "budget.receipts.invalidForm" };
  if (!validIsoDate(draft.receiptDate)) return { ok: false, errorKey: "budget.receipts.invalidForm" };
  const time = draft.receiptTime.trim();
  if (time !== "" && !/^(?:[01][0-9]|2[0-3]):[0-5][0-9]$/.test(time)) {
    return { ok: false, errorKey: "budget.receipts.invalidForm" };
  }
  const currency = draft.currency.trim().toUpperCase();
  if (!/^[A-Z]{3}$/.test(currency)) return { ok: false, errorKey: "budget.receipts.invalidForm" };
  const total = normalizeConfirmMoney(draft.totalAmount);
  if (total === null || total === "0" || total.startsWith("-")) {
    return { ok: false, errorKey: "budget.receipts.invalidForm" };
  }

  const linePlan = planConfirmLines(detail, draft.lines, total);
  if (!linePlan.ok) return linePlan;

  if (draft.linkChoice === "") {
    return { ok: false, errorKey: "budget.receipts.explicitUnlinkRequired" };
  }

  const fieldsChanged = hasMatchingFieldsChanged(detail, draft);
  const options = availableLinkOptions(detail, candidates, draft.paymentMethod, fieldsChanged);
  let link: ReceiptConfirmRequest["link"];

  if (draft.linkChoice === "unlink") {
    link = null;
  } else if (draft.linkChoice === "manual") {
    if (draft.manualTransaction?.currency !== currency || !canPickManualTransaction(detail, draft)) {
      return { ok: false, errorKey: "budget.receipts.invalidForm" };
    }
    link = { transaction_id: draft.manualTransaction.id };
  } else if (draft.linkChoice === "keep") {
    if (!options.paymentAllowsLink || options.matchingFieldsChanged || detail.status === "confirmed") {
      return { ok: false, errorKey: "budget.receipts.invalidForm" };
    }
    if (
      !options.canKeep
      || options.currentCandidate === null
      || draft.keepBinding === null
      || !keepEvidenceMatches(options.currentCandidate, draft.keepBinding)
    ) {
      return { ok: false, errorKey: "budget.receipts.bankEvidenceChanged" };
    }
    link = {
      account_id: draft.keepBinding.account_id,
      entry_reference: draft.keepBinding.entry_reference,
      candidate_token: draft.keepBinding.reviewed_token,
    };
  } else if (draft.linkChoice.startsWith("candidate:")) {
    if (!options.paymentAllowsLink || options.matchingFieldsChanged) {
      return { ok: false, errorKey: "budget.receipts.invalidForm" };
    }
    const binding = draft.selectedCandidate;
    if (binding === null) {
      return { ok: false, errorKey: "budget.receipts.candidateListChanged" };
    }
    const idx = Number(draft.linkChoice.slice("candidate:".length));
    const chosen = Number.isInteger(idx) ? candidates[idx] : undefined;
    if (!options.canPickCandidates || chosen === undefined || !candidateMatchesBinding(chosen, binding)) {
      return { ok: false, errorKey: "budget.receipts.candidateListChanged" };
    }
    link = {
      account_id: chosen.account_id,
      entry_reference: chosen.entry_reference,
      candidate_token: chosen.candidate_token,
    };
  } else {
    return { ok: false, errorKey: "budget.receipts.invalidForm" };
  }

  const request: ReceiptConfirmRequest = {
    receipt_id: detail.receipt_id,
    expected_version: detail.version,
    receipt_kind: draft.receiptKind,
    receipt_date: draft.receiptDate,
    receipt_time: time === "" ? null : time,
    total_amount: total,
    currency,
    payment_method: draft.paymentMethod,
    link,
  };
  if (shopEdited) {
    request.raw_shop_name = shopName === "" ? null : shopName;
  }
  if (nipEdited) {
    request.raw_nip = nip === "" ? null : nip;
  }
  if (linePlan.lines !== undefined) {
    request.lines = linePlan.lines;
  } else if (detail.document_kind === "invoice" && detail.lines.length === 0 && draft.lines.length === 0) {
    request.lines = [synthesizeInvoiceSummaryLine(detail, shopName, total, summaryFallbackName)];
  }
  return { ok: true, request };
}

// ---- Bulk confirm eligibility, merchant/NIP grouping, and plain confirm builder ----

export function isGmailImportSource(sourceKind: ReceiptSourceKind): boolean {
  return sourceKind === "email" || sourceKind === "attachment";
}

/** Only unlinked Gmail imports (`email` / `attachment` with `imported` status) support bulk confirm. */
export function isBulkConfirmable(
  receipt: Pick<ReceiptSummary, "status" | "source_kind" | "linked" | "link_group">,
): boolean {
  return !isReceiptGrouped(receipt) && !receipt.linked && receipt.status === "imported" && isGmailImportSource(receipt.source_kind);
}

/** True for OCR / paste / live-text drafts and non-Gmail items excluded from bulk confirm (distinct from `needs_review`). */
export function isExcludedDraftForBulk(
  receipt: Pick<ReceiptSummary, "status" | "source_kind" | "linked" | "link_group">,
): boolean {
  return !isBulkConfirmable(receipt) && receipt.status !== "needs_review" && receipt.status !== "confirmed";
}

export interface MerchantBulkIdentity {
  key: string;
  label: string;
}

export function normalizeMerchantGroupName(name: string): string {
  return name.normalize("NFC").trim().replace(/\s+/g, " ").toLowerCase();
}

export function receiptShopName(receipt: Pick<ReceiptSummary, "display_shop_name" | "raw_shop_name">): string | null {
  return receipt.display_shop_name ?? receipt.raw_shop_name;
}

/** Groups by trimmed NIP when present (avoiding merging distinct sellers with identical names), else normalized merchant name or sender domain. */
export function receiptMerchantGroupIdentity(
  receipt: Pick<ReceiptSummary, "display_shop_name" | "raw_shop_name" | "raw_nip" | "forwarded_sender_domain">,
  formatWithNip: (merchant: string | null, nip: string) => string = (merchant, nip) =>
    (merchant === null ? `NIP ${nip}` : `${merchant} (NIP ${nip})`),
): MerchantBulkIdentity | null {
  const name = receiptShopName(receipt)?.trim() ?? "";
  const shop = name !== "" && !isRedactedValue(name) ? name : null;
  const domain = receipt.forwarded_sender_domain !== null && receipt.forwarded_sender_domain.trim() !== ""
    ? receipt.forwarded_sender_domain.trim()
    : null;
  const merchant = shop ?? domain;
  const rawNip = receipt.raw_nip?.trim() ?? "";
  const nip = rawNip !== "" && !isRedactedValue(rawNip) ? rawNip : null;
  if (nip !== null) {
    return { key: `nip:${nip}`, label: formatWithNip(merchant, nip) };
  }
  if (shop !== null) {
    return { key: `name:${normalizeMerchantGroupName(shop)}`, label: shop };
  }
  if (domain !== null) {
    return { key: `domain:${domain.toLowerCase()}`, label: domain };
  }
  return null;
}

/** Safe display merchant label (including NIP when present); never surfaces privacy redaction sentinels. */
export function receiptMerchantGroupKey(
  receipt: Pick<ReceiptSummary, "display_shop_name" | "raw_shop_name" | "raw_nip" | "forwarded_sender_domain">,
  formatWithNip?: (merchant: string | null, nip: string) => string,
): string | null {
  return receiptMerchantGroupIdentity(receipt, formatWithNip)?.label ?? null;
}

export interface MerchantBulkGroup {
  key: string;
  merchant: string;
  confirmableIds: string[];
  excludedNeedsReview: number;
  excludedDrafts: number;
}

export function merchantBulkGroups(
  rows: readonly ReceiptSummary[],
  formatWithNip?: (merchant: string | null, nip: string) => string,
): MerchantBulkGroup[] {
  const byGroup = new Map<string, MerchantBulkGroup>();
  for (const row of rows) {
    const identity = receiptMerchantGroupIdentity(row, formatWithNip);
    if (identity === null) continue;
    let group = byGroup.get(identity.key);
    if (group === undefined) {
      group = {
        key: identity.key,
        merchant: identity.label,
        confirmableIds: [],
        excludedNeedsReview: 0,
        excludedDrafts: 0,
      };
      byGroup.set(identity.key, group);
    }
    if (isBulkConfirmable(row)) {
      group.confirmableIds.push(row.receipt_id);
    } else if (row.status === "needs_review") {
      group.excludedNeedsReview += 1;
    } else if (isExcludedDraftForBulk(row)) {
      group.excludedDrafts += 1;
    }
  }
  return [...byGroup.values()].filter((group) => group.confirmableIds.length > 0);
}

export interface BulkSelectionPlan {
  confirmable: ReceiptSummary[];
  excludedNeedsReview: number;
  excludedDrafts: number;
  merchants: string[];
}

export function buildBulkSelectionPlan(
  rows: readonly ReceiptSummary[],
  selectedIds: ReadonlySet<string>,
  unknownMerchantLabel: string,
  formatWithNip?: (merchant: string | null, nip: string) => string,
): BulkSelectionPlan {
  const confirmable: ReceiptSummary[] = [];
  const merchants: string[] = [];
  const seenKeys = new Set<string>();
  let excludedNeedsReview = 0;
  let excludedDrafts = 0;

  for (const row of rows) {
    if (selectedIds.has(row.receipt_id) && isBulkConfirmable(row)) {
      confirmable.push(row);
      const identity = receiptMerchantGroupIdentity(row, formatWithNip);
      const label = identity?.label ?? unknownMerchantLabel;
      const seenKey = identity?.key ?? `unknown:${unknownMerchantLabel}`;
      if (!seenKeys.has(seenKey)) {
        seenKeys.add(seenKey);
        merchants.push(label);
      }
    } else if (row.status === "needs_review") {
      excludedNeedsReview += 1;
    } else if (isExcludedDraftForBulk(row)) {
      excludedDrafts += 1;
    }
  }
  return { confirmable, excludedNeedsReview, excludedDrafts, merchants };
}

export function uniqueIdCandidate(
  detail: Pick<
    ReceiptDetail,
    "status" | "linked" | "linked_account_id" | "linked_entry_ref" | "payment_method" | "evidence_for_receipt_id" | "link_group"
  >,
  candidates: readonly ReceiptCandidate[],
): { candidate: ReceiptCandidate; index: number } | null {
  if (detail.status !== "imported" || detail.evidence_for_receipt_id !== null) {
    return null;
  }
  if (isReceiptGrouped(detail) || detail.linked || detail.linked_account_id !== null || detail.linked_entry_ref !== null) {
    return null;
  }
  if (detail.payment_method === "cash" || detail.payment_method === "mixed") {
    return null;
  }
  let found: { candidate: ReceiptCandidate; index: number } | null = null;
  for (let index = 0; index < candidates.length; index += 1) {
    const candidate = candidates[index];
    if (candidate?.match_strength === "id") {
      if (found !== null) {
        return null;
      }
      found = { candidate, index };
    }
  }
  if (found === null || found.candidate.candidate_token.trim() === "") {
    return null;
  }
  return found;
}

/** Builds a bulk confirm request; links only when `linkById` is opted in AND a single `"id"` candidate exists. */
export function buildBulkConfirmRequest(
  detail: ReceiptDetail,
  candidates: readonly ReceiptCandidate[],
  summaryFallbackName: string,
  options?: { linkById?: boolean },
): { ok: true; request: ReceiptConfirmRequest; linkedById: boolean } | { ok: false; errorCode: string } {
  if (!isBulkConfirmable(detail)) {
    return {
      ok: false,
      errorCode: detail.status === "needs_review" ? "budget_receipt_conflict" : "budget_receipt_invalid_request",
    };
  }
  const idMatch = options?.linkById === true ? uniqueIdCandidate(detail, candidates) : null;
  const effectiveCandidates = idMatch === null ? [] : candidates;
  const draft = draftFromDetail(detail, []);
  if (idMatch !== null) {
    draft.linkChoice = `candidate:${String(idMatch.index)}`;
    draft.selectedCandidate = candidateSelectionBinding(idMatch.candidate);
  } else {
    draft.linkChoice = "unlink";
    draft.selectedCandidate = null;
  }
  const built = buildConfirmRequest(detail, draft, effectiveCandidates, summaryFallbackName);
  if (!built.ok) {
    return {
      ok: false,
      errorCode: built.errorKey === "budget.receipts.totalMismatch"
        ? "budget_receipt_total_mismatch"
        : "budget_receipt_invalid_request",
    };
  }
  return { ok: true, request: built.request, linkedById: idMatch !== null };
}

/** Builds a plain confirm (`link: null`) for an unlinked Gmail import. */
export function buildPlainBulkConfirmRequest(
  detail: ReceiptDetail,
  summaryFallbackName: string,
): { ok: true; request: ReceiptConfirmRequest } | { ok: false; errorCode: string } {
  const built = buildBulkConfirmRequest(detail, [], summaryFallbackName, { linkById: false });
  return built.ok ? { ok: true, request: built.request } : built;
}

export function formatBulkErrorBucket(status: number | undefined, error: string): string {
  return status !== undefined && status > 0 ? `${String(status)} ${error}` : error;
}

// ---- M:1 receipt grouping eligibility, selection plans & safe write builders ----

export const GROUP_MIN_MEMBERS = 2;
export const GROUP_MAX_MEMBERS = 8;
export const GROUP_MAX_SPAN_DAYS = 14;

/** True when a receipt can participate as a member of an M:1 group (confirmed or imported). */
export function isGroupMemberEligible(
  receipt: Pick<
    ReceiptSummary,
    | "status"
    | "linked"
    | "linked_account_id"
    | "linked_entry_ref"
    | "link_group"
    | "gateway"
    | "evidence_for_receipt_id"
    | "payment_method"
    | "receipt_kind"
    | "currency"
    | "receipt_date"
    | "total_amount"
  >,
): boolean {
  if (isReceiptGrouped(receipt)) return false;
  if (receipt.linked || receipt.linked_account_id !== null || receipt.linked_entry_ref !== null) return false;
  if (receipt.status !== "confirmed" && receipt.status !== "imported") return false;
  if (receipt.gateway != null || receipt.evidence_for_receipt_id != null) return false;
  if (receipt.payment_method === "cash" || receipt.payment_method === "mixed") return false;
  if (receipt.currency === null || !/^[A-Z]{3}$/.test(receipt.currency)) return false;
  if (receipt.receipt_date === null || !validIsoDate(receipt.receipt_date)) return false;
  if (receipt.total_amount === null) return false;
  const normalized = normalizeDecimalInput(receipt.total_amount);
  return normalized !== null && (compareDecimalStrings(normalized, "0") ?? -1) > 0;
}

export interface BulkGroupSelectionPlan {
  readonly selected: ReceiptSummary[];
  readonly eligible: boolean;
  readonly reason: "none" | "count" | "ineligible" | "mismatch";
  readonly currency: string | null;
  readonly receiptKind: ReceiptKind | null;
}

/** Validates whether 2..8 list-selected receipts share one currency and kind for M:1 group review. */
export function buildBulkGroupPlan(
  rows: readonly ReceiptSummary[],
  selectedIds: ReadonlySet<string>,
): BulkGroupSelectionPlan {
  const selected = rows.filter((row) => selectedIds.has(row.receipt_id));
  if (selected.length === 0) {
    return { selected, eligible: false, reason: "none", currency: null, receiptKind: null };
  }
  if (selected.length < GROUP_MIN_MEMBERS || selected.length > GROUP_MAX_MEMBERS) {
    return { selected, eligible: false, reason: "count", currency: null, receiptKind: null };
  }
  if (selected.some((row) => !isGroupMemberEligible(row))) {
    return { selected, eligible: false, reason: "ineligible", currency: null, receiptKind: null };
  }
  const first = selected[0];
  if (first === undefined || first.currency === null) {
    return { selected, eligible: false, reason: "ineligible", currency: null, receiptKind: null };
  }
  const sameScope = selected.every(
    (row) => row.currency === first.currency && row.receipt_kind === first.receipt_kind,
  );
  if (!sameScope) {
    return { selected, eligible: false, reason: "mismatch", currency: null, receiptKind: null };
  }
  return {
    selected,
    eligible: true,
    reason: "none",
    currency: first.currency,
    receiptKind: first.receipt_kind,
  };
}

/** True when all supplied ISO dates are valid and span at most `maxDays` calendar days. */
export function isGroupDateSpanValid(
  dates: readonly (string | null | undefined)[],
  maxDays = GROUP_MAX_SPAN_DAYS,
): boolean {
  if (dates.length === 0) return false;
  let minDay = Number.POSITIVE_INFINITY;
  let maxDay = Number.NEGATIVE_INFINITY;
  for (const raw of dates) {
    const day = parseIsoDayNumber(raw);
    if (day === null) return false;
    if (day < minDay) minDay = day;
    if (day > maxDay) maxDay = day;
  }
  return maxDay - minDay <= maxDays;
}

export function groupTransactionFromProposal(
  proposal: ReceiptGroupCandidate,
): ReceiptGroupTransactionCandidate {
  return {
    account_id: proposal.account_id,
    entry_reference: proposal.entry_reference,
    amount: proposal.amount,
    currency: proposal.currency,
    credit_debit_indicator: proposal.credit_debit_indicator,
    transaction_date: proposal.transaction_date,
    booking_date: proposal.booking_date,
    value_date: proposal.value_date,
    merchant_token_hit: proposal.match_strength !== "none",
    match_strength: proposal.match_strength,
    date_distance: proposal.date_distance,
    evidence_fingerprint: proposal.evidence_fingerprint,
    candidate_token: proposal.candidate_token,
  };
}

/** Merges anchor, server eligible receipts, and optional bulk-selected rows, keeping fresher versions. */
export function mergeGroupPoolMembers(
  anchor: ReceiptSummary | null,
  eligibleReceipts: readonly ReceiptSummary[],
  extraMembers: readonly ReceiptSummary[] = [],
): ReceiptSummary[] {
  const byId = new Map<string, ReceiptSummary>();
  if (anchor !== null) byId.set(anchor.receipt_id, anchor);
  for (const row of eligibleReceipts) {
    const prev = byId.get(row.receipt_id);
    if (prev === undefined || row.version >= prev.version) byId.set(row.receipt_id, row);
  }
  for (const row of extraMembers) {
    const prev = byId.get(row.receipt_id);
    if (prev === undefined || row.version > prev.version) byId.set(row.receipt_id, row);
  }
  return [...byId.values()];
}

/** Builds a sequential unlinked pre-confirm (`link: null`) payload for an imported group member or evidence row. */
export function buildGroupPreConfirmRequest(
  detail: ReceiptDetail,
  expectedVersion: number,
  summaryFallbackName: string,
): { ok: true; request: ReceiptConfirmRequest } | { ok: false; errorCode: string } {
  if (detail.version !== expectedVersion || detail.status !== "imported") {
    return { ok: false, errorCode: "budget_receipt_conflict" };
  }
  if (
    isReceiptGrouped(detail)
    || detail.linked
    || detail.linked_account_id !== null
    || detail.linked_entry_ref !== null
    || detail.evidence_for_receipt_id !== null
  ) {
    return { ok: false, errorCode: "budget_receipt_tx_already_linked" };
  }
  if (detail.payment_method === "cash" || detail.payment_method === "mixed") {
    return { ok: false, errorCode: "budget_receipt_invalid_request" };
  }
  const draft = draftFromDetail(detail, []);
  draft.linkChoice = "unlink";
  draft.selectedCandidate = null;
  draft.keepBinding = null;
  const built = buildConfirmRequest(detail, draft, [], summaryFallbackName);
  if (!built.ok) {
    return {
      ok: false,
      errorCode: built.errorKey === "budget.receipts.totalMismatch"
        ? "budget_receipt_total_mismatch"
        : "budget_receipt_invalid_request",
    };
  }
  return { ok: true, request: built.request };
}

export function positiveGroupFee(amount: string | null | undefined): string | null {
  if (amount == null) return null;
  const normalized = normalizeDecimalInput(amount);
  return normalized !== null && (compareDecimalStrings(normalized, "0") ?? -1) > 0 ? normalized : null;
}

export function buildGroupLinkRequest(params: {
  transaction: Pick<ReceiptGroupTransactionCandidate, "account_id" | "entry_reference" | "candidate_token">;
  members: readonly ReceiptGroupMemberInput[];
  evidence?: (ReceiptGroupMemberInput & { fee_amount?: string | null }) | null;
}): ReceiptGroupLinkRequest {
  const fee = positiveGroupFee(params.evidence?.fee_amount);
  return {
    link: {
      account_id: params.transaction.account_id,
      entry_reference: params.transaction.entry_reference,
      candidate_token: params.transaction.candidate_token,
    },
    members: params.members.map((member) => ({
      receipt_id: member.receipt_id,
      expected_version: member.expected_version,
    })),
    ...(params.evidence == null ? {} : {
      evidence_receipt_id: params.evidence.receipt_id,
      evidence_expected_version: params.evidence.expected_version,
      ...(fee === null ? {} : { fee_amount: fee }),
    }),
  };
}

// ---- Filter equality & detail navigation ----

export function sameReceiptFilter(a: ReceiptFilter, b: ReceiptFilter): boolean {
  return a.query.trim() === b.query.trim()
    && a.status === b.status
    && a.documentKind === b.documentKind
    && a.sourceKind === b.sourceKind
    && a.linked === b.linked
    && a.dateFrom === b.dateFrom
    && a.dateTo === b.dateTo;
}

export function receiptNeedsConfirmation(
  receipt: Pick<ReceiptSummary, "status">,
): boolean {
  return RECEIPT_NEEDS_ACTION_STATUSES.includes(receipt.status);
}

export function findAdjacentReceiptId(
  rows: readonly ReceiptSummary[],
  currentId: string,
  direction: "prev" | "next",
): string | null {
  const idx = rows.findIndex((row) => row.receipt_id === currentId);
  if (idx < 0) return null;
  const target = direction === "prev" ? rows[idx - 1] : rows[idx + 1];
  return target?.receipt_id ?? null;
}

/**
 * Resolves which receipt should remain selected after a list refresh:
 * keeps `currentId` if it is still present, otherwise selects the row now at the same index
 * (clamped to the last row), or `null` if the list is empty.
 */
export function resolvePostRefreshSelectionId(
  rows: readonly ReceiptSummary[],
  currentId: string,
  previousIndex: number,
): string | null {
  if (rows.some((row) => row.receipt_id === currentId)) return currentId;
  if (rows.length === 0) return null;
  const clamped = Math.max(0, Math.min(previousIndex, rows.length - 1));
  return rows[clamped]?.receipt_id ?? null;
}

export function findNextUnconfirmedReceiptId(
  rows: readonly ReceiptSummary[],
  currentId: string,
  options: {
    wrap?: boolean;
    startIndex?: number;
    predicate?: (row: ReceiptSummary) => boolean;
  } = {},
): string | null {
  const matches = (row: ReceiptSummary): boolean =>
    row.receipt_id !== currentId
    && receiptNeedsConfirmation(row)
    && (options.predicate === undefined || options.predicate(row));

  const idx = rows.findIndex((row) => row.receipt_id === currentId);
  if (idx >= 0) {
    for (let i = idx + 1; i < rows.length; i += 1) {
      const row = rows[i];
      if (row !== undefined && matches(row)) return row.receipt_id;
    }
    if (options.wrap === true) {
      for (let i = 0; i < idx; i += 1) {
        const row = rows[i];
        if (row !== undefined && matches(row)) return row.receipt_id;
      }
    }
    return null;
  }

  const start = options.startIndex === undefined
    ? 0
    : Math.max(0, Math.min(options.startIndex, rows.length));
  for (let i = start; i < rows.length; i += 1) {
    const row = rows[i];
    if (row !== undefined && matches(row)) return row.receipt_id;
  }
  if (options.wrap === true) {
    for (let i = 0; i < start; i += 1) {
      const row = rows[i];
      if (row !== undefined && matches(row)) return row.receipt_id;
    }
  }
  return null;
}

