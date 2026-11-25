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
  /** "keep" | "unlink" | "candidate:<index>" */
  linkChoice: string;
}

export interface ReceiptLinkOptions {
  paymentAllowsLink: boolean;
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

/** Models the exact link transitions `receipt_store.confirm` and `receipt_store.detail` support per status. */
export function availableLinkOptions(
  detail: Pick<ReceiptDetail, "status" | "linked" | "linked_account_id" | "linked_entry_ref">,
  candidates: readonly ReceiptCandidate[],
  paymentMethod: ReceiptPaymentMethod,
): ReceiptLinkOptions {
  const paymentAllowsLink = paymentMethod !== "cash" && paymentMethod !== "mixed";
  const currentCandidate = findCurrentLinkedCandidate(detail, candidates);

  if (detail.status === "needs_review") {
    return {
      paymentAllowsLink,
      canKeep: paymentAllowsLink && detail.linked && currentCandidate !== null,
      canPickCandidates: paymentAllowsLink && candidates.length > 0,
      mustUnlinkToConfirm: false,
      currentCandidate,
    };
  }

  if (detail.status === "confirmed") {
    return {
      paymentAllowsLink,
      canKeep: false,
      canPickCandidates: false,
      mustUnlinkToConfirm: detail.linked,
      currentCandidate: null,
    };
  }

  // draft / imported rows never carry server candidates; only keep if a matching token were ever supplied.
  return {
    paymentAllowsLink,
    canKeep: paymentAllowsLink && detail.linked && currentCandidate !== null,
    canPickCandidates: false,
    mustUnlinkToConfirm: false,
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
  const options = availableLinkOptions(detail, candidates, paymentMethod);
  let linkChoice = "unlink";
  if (options.canKeep) {
    linkChoice = "keep";
  } else if (detail.status === "needs_review" && !detail.linked && options.canPickCandidates) {
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

export function computeLinesDelta(lines: readonly ReceiptLineInput[], totalAmount: string): string | null {
  const total = normalizeDecimalInput(totalAmount);
  if (total === null) return null;
  const netSum = addDecimals(lines.map((line) => line.net_line_total));
  if (netSum === null) return null;
  return subtractDecimals(netSum, total);
}

export function buildConfirmRequest(
  detail: ReceiptDetail,
  draft: ReceiptConfirmDraft,
  candidates: readonly ReceiptCandidate[],
): { ok: true; request: ReceiptConfirmRequest } | { ok: false; errorKey: MessageKey } {
  const shopName = draft.rawShopName.trim();
  if (shopName.length > 128) return { ok: false, errorKey: "budget.receipts.invalidForm" };
  const nip = draft.rawNip.trim();
  if (nip !== "" && !validPolishNip(nip)) return { ok: false, errorKey: "budget.receipts.invalidForm" };
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

  const options = availableLinkOptions(detail, candidates, draft.paymentMethod);
  let link: ReceiptLinkRequest | null = null;
  if (options.paymentAllowsLink) {
    if (draft.linkChoice === "keep") {
      if (!options.canKeep || options.currentCandidate === null) {
        return { ok: false, errorKey: "budget.receipts.invalidForm" };
      }
      link = {
        account_id: options.currentCandidate.account_id,
        entry_reference: options.currentCandidate.entry_reference,
        candidate_token: options.currentCandidate.candidate_token,
      };
    } else if (draft.linkChoice.startsWith("candidate:")) {
      if (!options.canPickCandidates) {
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
    }
  }

  return {
    ok: true,
    request: {
      receipt_id: detail.receipt_id,
      expected_version: detail.version,
      raw_shop_name: shopName === "" ? null : shopName,
      raw_nip: nip === "" ? null : nip,
      receipt_kind: draft.receiptKind,
      receipt_date: draft.receiptDate,
      receipt_time: time === "" ? null : time,
      total_amount: total,
      currency,
      payment_method: draft.paymentMethod,
      lines: cleanLines,
      link,
    },
  };
}
