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
export type ReceiptDeleteRequest = Schemas["BudgetReceiptDeleteRequest"];
export type ReceiptDeleteResult = Schemas["BudgetReceiptDeleteResult"];
export type ReceiptDetailResult = Schemas["BudgetReceiptDetailResult"];
export type ReceiptListResult = Schemas["BudgetReceiptListResult"];

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

export const RECEIPT_STATUSES: readonly ReceiptStatus[] = ["imported", "needs_review", "confirmed", "draft"];
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

export interface ReceiptFilter {
  query: string;
  status: "" | ReceiptStatus;
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

export function buildReceiptListRequest(filter: ReceiptFilter, cursor: string | null = null): ReceiptListRequest {
  const month = listMonthForFilter(filter);
  const request: ReceiptListRequest = { limit: RECEIPT_PAGE_LIMIT };
  if (month !== undefined) request.month = month;
  if (filter.status !== "") request.status = filter.status;
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
      if (filter.status !== "" && row.status !== filter.status) return false;
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

export type ReceiptActionKind = "confirm" | "delete";

export interface ReceiptSettledEvent {
  receiptId: string;
  kind: ReceiptActionKind;
  outcome: "confirmed" | "deleted" | "not_found" | "conflict" | "error";
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

export interface ReceiptConfirmDraft {
  rawShopName: string;
  rawNip: string;
  receiptKind: ReceiptKind;
  receiptDate: string;
  receiptTime: string;
  totalAmount: string;
  currency: string;
  paymentMethod: ReceiptPaymentMethod;
  lines: ReceiptLineInput[];
  /** "" (explicit choice required on linked receipt without keep token) | "keep" | "unlink" | "candidate:<index>" */
  linkChoice: string;
}

export interface ReceiptLinkOptions {
  paymentAllowsLink: boolean;
  matchingFieldsChanged: boolean;
  canKeep: boolean;
  canPickCandidates: boolean;
  mustUnlinkToConfirm: boolean;
  currentCandidate: ReceiptCandidate | null;
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
  detail: Pick<ReceiptDetail, "status" | "linked" | "linked_account_id" | "linked_entry_ref">,
  candidates: readonly ReceiptCandidate[],
  paymentMethod: ReceiptPaymentMethod,
  matchingFieldsChanged = false,
): ReceiptLinkOptions {
  const paymentAllowsLink = paymentMethod !== "cash" && paymentMethod !== "mixed";
  const tokensValid = paymentAllowsLink && !matchingFieldsChanged;
  const currentCandidate = findCurrentLinkedCandidate(detail, candidates);

  if (detail.status === "needs_review") {
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

  if (detail.status === "confirmed") {
    return {
      paymentAllowsLink,
      matchingFieldsChanged,
      canKeep: false,
      canPickCandidates: false,
      mustUnlinkToConfirm: detail.linked,
      currentCandidate: null,
    };
  }

  // draft / imported rows never carry server candidates; only keep if a matching token were ever supplied.
  const canKeep = tokensValid && detail.linked && currentCandidate !== null;
  return {
    paymentAllowsLink,
    matchingFieldsChanged,
    canKeep,
    canPickCandidates: false,
    mustUnlinkToConfirm: detail.linked && !canKeep,
    currentCandidate: detail.linked ? currentCandidate : null,
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
  let linkChoice = "unlink";
  if (options.canKeep) {
    linkChoice = "keep";
  } else if (detail.linked) {
    // Never default a linked receipt without a keep token to implicit unlink.
    linkChoice = "";
  } else if (detail.status === "needs_review" && options.canPickCandidates) {
    linkChoice = "candidate:0";
  }
  return {
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
  };
}

/** Keeps link choices valid after field edits: disables stale tokens without silently unlinking linked receipts. */
export function reconcileLinkChoice(
  detail: ReceiptDetail,
  prevChoice: string,
  nextDraft: Pick<ReceiptConfirmDraft, "rawShopName" | "rawNip" | "totalAmount" | "currency" | "receiptDate" | "receiptKind" | "paymentMethod">,
  candidates: readonly ReceiptCandidate[],
): string {
  if (prevChoice === "unlink") return "unlink";
  const changed = hasMatchingFieldsChanged(detail, nextDraft);
  const nextOptions = availableLinkOptions(detail, candidates, nextDraft.paymentMethod, changed);
  if (prevChoice === "keep") {
    return nextOptions.canKeep ? "keep" : (detail.linked ? "" : "unlink");
  }
  if (prevChoice.startsWith("candidate:")) {
    return nextOptions.canPickCandidates ? prevChoice : (detail.linked ? "" : "unlink");
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

export function receiptHasRedactedLines(
  lines: readonly Pick<ReceiptLineInput, "raw_name" | "raw_unit">[],
): boolean {
  return lines.some((line) => isRedactedValue(line.raw_name) || isRedactedValue(line.raw_unit));
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

export function buildConfirmRequest(
  detail: ReceiptDetail,
  draft: ReceiptConfirmDraft,
  candidates: readonly ReceiptCandidate[],
): { ok: true; request: ReceiptConfirmRequest } | { ok: false; errorKey: MessageKey } {
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
  const total = normalizeDecimalInput(draft.totalAmount);
  if (total === null || (compareDecimalStrings(total, "0") ?? -1) <= 0) {
    return { ok: false, errorKey: "budget.receipts.invalidForm" };
  }
  if (draft.lines.length > 300) return { ok: false, errorKey: "budget.receipts.invalidForm" };

  const linesChanged = haveConfirmLinesChanged(detail.lines, draft.lines);
  if (linesChanged && receiptHasRedactedLines(detail.lines)) {
    return { ok: false, errorKey: "budget.receipts.redactedLinesEditBlocked" };
  }

  const cleanLines: ReceiptLineInput[] = [];
  for (const line of draft.lines) {
    const rawName = line.raw_name.trim();
    if (rawName === "" || rawName.length > 128) return { ok: false, errorKey: "budget.receipts.invalidForm" };
    const qty = normalizeDecimalInput(line.quantity);
    const gross = normalizeDecimalInput(line.gross_line_total);
    const discount = normalizeDecimalInput(line.discount_amount);
    const net = normalizeDecimalInput(line.net_line_total);
    if (qty === null || gross === null || discount === null || net === null) {
      return { ok: false, errorKey: "budget.receipts.invalidForm" };
    }
    const expectedNet = subtractDecimals(gross, discount);
    if (expectedNet !== net) return { ok: false, errorKey: "budget.receipts.totalMismatch" };
    const grossCmp = compareDecimalStrings(gross, "0");
    const discCmp = compareDecimalStrings(discount, "0");
    const netCmp = compareDecimalStrings(net, "0");
    if (grossCmp === null || discCmp === null || netCmp === null) {
      return { ok: false, errorKey: "budget.receipts.invalidForm" };
    }
    if (line.line_kind === "item" && (grossCmp < 0 || discCmp < 0 || netCmp < 0)) {
      return { ok: false, errorKey: "budget.receipts.totalMismatch" };
    }
    if (line.line_kind === "deposit" && (grossCmp === 0 || discount !== "0" || net !== gross)) {
      return { ok: false, errorKey: "budget.receipts.totalMismatch" };
    }
    if (line.line_kind === "basket_discount" && (grossCmp >= 0 || discount !== "0" || net !== gross)) {
      return { ok: false, errorKey: "budget.receipts.totalMismatch" };
    }
    if (line.line_kind === "other" && (discount !== "0" || net !== gross)) {
      return { ok: false, errorKey: "budget.receipts.totalMismatch" };
    }
    const unitPrice = line.unit_price_raw === null ? null : normalizeDecimalInput(line.unit_price_raw);
    if (line.unit_price_raw !== null && unitPrice === null) {
      return { ok: false, errorKey: "budget.receipts.invalidForm" };
    }
    const unit = line.raw_unit === null ? null : (line.raw_unit.trim() || null);
    if (unit !== null && unit.length > 16) return { ok: false, errorKey: "budget.receipts.invalidForm" };
    if (linesChanged && (isRedactedValue(rawName) || isRedactedValue(unit))) {
      return { ok: false, errorKey: "budget.receipts.redactedLinesEditBlocked" };
    }
    cleanLines.push({
      line_kind: line.line_kind,
      raw_name: rawName,
      ptu_code: line.ptu_code,
      quantity: qty,
      raw_unit: unit,
      unit_price_raw: unitPrice,
      gross_line_total: gross,
      discount_amount: discount,
      net_line_total: net,
    });
  }

  const netSum = addDecimals(cleanLines.map((line) => line.net_line_total));
  if (netSum !== total) return { ok: false, errorKey: "budget.receipts.totalMismatch" };

  if (draft.linkChoice === "") {
    return { ok: false, errorKey: "budget.receipts.explicitUnlinkRequired" };
  }

  const fieldsChanged = hasMatchingFieldsChanged(detail, draft);
  const options = availableLinkOptions(detail, candidates, draft.paymentMethod, fieldsChanged);
  let link: ReceiptLinkRequest | null;

  if (draft.linkChoice === "unlink") {
    link = null;
  } else if (draft.linkChoice === "keep") {
    if (!options.paymentAllowsLink || !options.canKeep || options.currentCandidate === null) {
      return { ok: false, errorKey: "budget.receipts.invalidForm" };
    }
    link = {
      account_id: options.currentCandidate.account_id,
      entry_reference: options.currentCandidate.entry_reference,
      candidate_token: options.currentCandidate.candidate_token,
    };
  } else if (draft.linkChoice.startsWith("candidate:")) {
    if (!options.paymentAllowsLink || !options.canPickCandidates) {
      return { ok: false, errorKey: "budget.receipts.invalidForm" };
    }
    const idx = Number(draft.linkChoice.slice("candidate:".length));
    const chosen = Number.isInteger(idx) ? candidates[idx] : undefined;
    if (chosen === undefined) return { ok: false, errorKey: "budget.receipts.invalidForm" };
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
  if (linesChanged) {
    request.lines = cleanLines;
  }
  return { ok: true, request };
}

// ---- Bulk confirm eligibility, merchant/NIP grouping, and plain confirm builder ----

export function isGmailImportSource(sourceKind: ReceiptSourceKind): boolean {
  return sourceKind === "email" || sourceKind === "attachment";
}

/** Only unlinked Gmail imports (`email` / `attachment` with `imported` status) support bulk confirm. */
export function isBulkConfirmable(
  receipt: Pick<ReceiptSummary, "status" | "source_kind" | "linked">,
): boolean {
  return !receipt.linked && receipt.status === "imported" && isGmailImportSource(receipt.source_kind);
}

/** True for OCR / paste / live-text drafts and non-Gmail items excluded from bulk confirm (distinct from `needs_review`). */
export function isExcludedDraftForBulk(
  receipt: Pick<ReceiptSummary, "status" | "source_kind" | "linked">,
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

/** Groups by trimmed NIP when present (avoiding merging distinct sellers with identical names), else normalized merchant name or sender domain. */
export function receiptMerchantGroupIdentity(
  receipt: Pick<ReceiptSummary, "raw_shop_name" | "raw_nip" | "forwarded_sender_domain">,
  formatWithNip: (merchant: string | null, nip: string) => string = (merchant, nip) =>
    (merchant === null ? `NIP ${nip}` : `${merchant} (NIP ${nip})`),
): MerchantBulkIdentity | null {
  const shop = receipt.raw_shop_name !== null
    && !isRedactedValue(receipt.raw_shop_name)
    && receipt.raw_shop_name.trim() !== ""
    ? receipt.raw_shop_name.trim()
    : null;
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
  receipt: Pick<ReceiptSummary, "raw_shop_name" | "raw_nip" | "forwarded_sender_domain">,
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

/** Builds a plain confirm (`link: null`) for an unlinked Gmail import; omits existing lines, synthesizing 1 summary line only for 0-line Gmail invoices. */
export function buildPlainBulkConfirmRequest(
  detail: ReceiptDetail,
  summaryFallbackName: string,
): { ok: true; request: ReceiptConfirmRequest } | { ok: false; errorCode: string } {
  if (!isBulkConfirmable(detail)) {
    return {
      ok: false,
      errorCode: detail.status === "needs_review" ? "budget_receipt_conflict" : "budget_receipt_invalid_request",
    };
  }
  const draft = draftFromDetail(detail, []);
  if (detail.lines.length === 0) {
    if (detail.document_kind !== "invoice") {
      return { ok: false, errorCode: "budget_receipt_total_mismatch" };
    }
    const normalizedTotal = detail.total_amount === null ? null : normalizeDecimalInput(detail.total_amount);
    if (normalizedTotal === null) {
      return { ok: false, errorCode: "budget_receipt_invalid_request" };
    }
    const invoiceName = detail.invoice_number !== null && !isRedactedValue(detail.invoice_number)
      ? detail.invoice_number.trim()
      : "";
    const shopName = detail.raw_shop_name !== null && !isRedactedValue(detail.raw_shop_name)
      ? detail.raw_shop_name.trim()
      : "";
    const candidateName = invoiceName || shopName || summaryFallbackName;
    const rawName = isRedactedValue(candidateName) ? "Document" : candidateName;
    draft.lines.push({
      line_kind: "other",
      raw_name: rawName,
      ptu_code: null,
      quantity: "1",
      raw_unit: null,
      unit_price_raw: null,
      gross_line_total: normalizedTotal,
      discount_amount: "0",
      net_line_total: normalizedTotal,
    });
  }
  const built = buildConfirmRequest(detail, draft, []);
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

export function formatBulkErrorBucket(status: number | undefined, error: string): string {
  return status !== undefined && status > 0 ? `${String(status)} ${error}` : error;
}

