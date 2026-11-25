// Pure helpers for owner-private receipts and documents. Exact decimals stay strings end to end
// (BigInt scaled-integer arithmetic only), and transaction references are matched via UTF-8 SHA-256
// digests matching hestia/budget/receipt_reconcile.py (`entry_reference_digest`).
import type { components } from "../api/openapi";
import type { MessageKey } from "../i18n/locales/en";
import { accountName, formatScaledBigInt, parseScaledInt, validIsoDate } from "./analytics/model";
import type { AnalyticsTransaction } from "./analytics/model";
import { transactionDate, transactionText, validRange } from "./model";
import type { BudgetAccount, BudgetConnection, BudgetTransaction } from "./model";

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

// ---- Pure synchronous SHA-256 (UTF-8) for matching linked_entry_ref digests ----

const SHA256_K = [
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
];

function rotr(value: number, bits: number): number {
  return (value >>> bits) | (value << (32 - bits));
}

/** Lowercase hex SHA-256 of UTF-8 text, matching `hestia/budget/receipt_reconcile.py:entry_reference_digest`. */
export function sha256Hex(text: string): string {
  const bytes = new TextEncoder().encode(text);
  const bitLen = bytes.length * 8;
  const paddedLen = Math.ceil((bytes.length + 9) / 64) * 64;
  const padded = new Uint8Array(paddedLen);
  padded.set(bytes);
  padded[bytes.length] = 0x80;
  const view = new DataView(padded.buffer);
  view.setUint32(paddedLen - 8, Math.floor(bitLen / 0x1_0000_0000), false);
  view.setUint32(paddedLen - 4, bitLen >>> 0, false);

  let h0 = 0x6a09e667;
  let h1 = 0xbb67ae85;
  let h2 = 0x3c6ef372;
  let h3 = 0xa54ff53a;
  let h4 = 0x510e527f;
  let h5 = 0x9b05688c;
  let h6 = 0x1f83d9ab;
  let h7 = 0x5be0cd19;
  const w = new Uint32Array(64);

  for (let offset = 0; offset < paddedLen; offset += 64) {
    for (let i = 0; i < 16; i += 1) {
      w[i] = view.getUint32(offset + i * 4, false);
    }
    for (let i = 16; i < 64; i += 1) {
      const w15 = w[i - 15] ?? 0;
      const w2 = w[i - 2] ?? 0;
      const s0 = rotr(w15, 7) ^ rotr(w15, 18) ^ (w15 >>> 3);
      const s1 = rotr(w2, 17) ^ rotr(w2, 19) ^ (w2 >>> 10);
      w[i] = ((w[i - 16] ?? 0) + s0 + (w[i - 7] ?? 0) + s1) >>> 0;
    }

    let a = h0;
    let b = h1;
    let c = h2;
    let d = h3;
    let e = h4;
    let f = h5;
    let g = h6;
    let h = h7;

    for (let i = 0; i < 64; i += 1) {
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const temp1 = (h + S1 + ch + (SHA256_K[i] ?? 0) + (w[i] ?? 0)) >>> 0;
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (S0 + maj) >>> 0;
      h = g;
      g = f;
      f = e;
      e = (d + temp1) >>> 0;
      d = c;
      c = b;
      b = a;
      a = (temp1 + temp2) >>> 0;
    }

    h0 = (h0 + a) >>> 0;
    h1 = (h1 + b) >>> 0;
    h2 = (h2 + c) >>> 0;
    h3 = (h3 + d) >>> 0;
    h4 = (h4 + e) >>> 0;
    h5 = (h5 + f) >>> 0;
    h6 = (h6 + g) >>> 0;
    h7 = (h7 + h) >>> 0;
  }

  return [h0, h1, h2, h3, h4, h5, h6, h7]
    .map((word) => word.toString(16).padStart(8, "0"))
    .join("");
}

// ---- Linked transaction & candidate metadata resolution ----

export interface ResolvedTransaction {
  accountId: string;
  entryRefDigest: string;
  date: string | null;
  amount: string;
  currency: string;
  indicator: "DBIT" | "CRDT";
  counterparty: string;
}

export function txLookupKey(accountId: string, entryRefDigest: string): string {
  return `${accountId}\0${entryRefDigest}`;
}

export function indexAnalyticsTransactions(rows: readonly AnalyticsTransaction[]): Map<string, ResolvedTransaction> {
  const map = new Map<string, ResolvedTransaction>();
  for (const row of rows) {
    const digest = sha256Hex(row.entry_reference);
    const label = row.metadata === null
      ? transactionText(row)
      : ((row.credit_debit_indicator === "DBIT" ? row.metadata.creditor_name : row.metadata.debtor_name)
        ?? transactionText(row));
    map.set(txLookupKey(row.account_id, digest), {
      accountId: row.account_id,
      entryRefDigest: digest,
      date: transactionDate(row),
      amount: row.amount,
      currency: row.currency,
      indicator: row.credit_debit_indicator,
      counterparty: label,
    });
  }
  return map;
}

export function indexAccountTransactions(
  accountId: string,
  rows: readonly BudgetTransaction[],
): Map<string, ResolvedTransaction> {
  const map = new Map<string, ResolvedTransaction>();
  for (const row of rows) {
    const digest = sha256Hex(row.entry_reference);
    map.set(txLookupKey(accountId, digest), {
      accountId,
      entryRefDigest: digest,
      date: transactionDate(row),
      amount: row.amount,
      currency: row.currency,
      indicator: row.credit_debit_indicator,
      counterparty: transactionText(row),
    });
  }
  return map;
}

/** Ordered YYYY-MM months covering +/-7 days around a valid ISO date. */
export function receiptLookupMonths(receiptDate: string | null): string[] {
  if (receiptDate === null || !validIsoDate(receiptDate)) return [];
  const time = Date.UTC(
    Number(receiptDate.slice(0, 4)),
    Number(receiptDate.slice(5, 7)) - 1,
    Number(receiptDate.slice(8, 10)),
  );
  const first = new Date(time - 7 * 86_400_000).toISOString().slice(0, 7);
  const last = new Date(time + 7 * 86_400_000).toISOString().slice(0, 7);
  return first === last ? [first] : [first, last];
}

/** Inclusive ISO date bounds (+/-7 days) for stored account transaction fallback reads. */
export function receiptLookupDateRange(receiptDate: string | null): { dateFrom?: string; dateTo?: string } {
  if (receiptDate === null || !validIsoDate(receiptDate)) return {};
  const time = Date.UTC(
    Number(receiptDate.slice(0, 4)),
    Number(receiptDate.slice(5, 7)) - 1,
    Number(receiptDate.slice(8, 10)),
  );
  return {
    dateFrom: new Date(time - 7 * 86_400_000).toISOString().slice(0, 10),
    dateTo: new Date(time + 7 * 86_400_000).toISOString().slice(0, 10),
  };
}

export function candidateDisplayDate(candidate: Pick<ReceiptCandidate, "booking_date" | "value_date" | "transaction_date">): string | null {
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

// ---- Confirm validation & exact decimal arithmetic ----

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

export function draftFromDetail(
  detail: ReceiptDetail,
  candidates: readonly ReceiptCandidate[] = [],
): ReceiptConfirmDraft {
  const paymentMethod: ReceiptPaymentMethod =
    detail.payment_method !== null && (RECEIPT_PAYMENT_METHODS as readonly string[]).includes(detail.payment_method)
      ? (detail.payment_method as ReceiptPaymentMethod)
      : "unknown";
  let linkChoice = "unlink";
  if (detail.linked) {
    linkChoice = "keep";
  } else if (candidates.length > 0 && paymentMethod !== "cash" && paymentMethod !== "mixed") {
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

export function findCurrentLinkedCandidate(
  detail: Pick<ReceiptDetail, "linked" | "linked_account_id" | "linked_entry_ref">,
  candidates: readonly ReceiptCandidate[],
): ReceiptCandidate | null {
  if (!detail.linked || detail.linked_account_id === null || detail.linked_entry_ref === null) return null;
  return candidates.find(
    (item) => item.account_id === detail.linked_account_id && item.entry_reference === detail.linked_entry_ref,
  ) ?? null;
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

  let link: ReceiptLinkRequest | null = null;
  if (draft.paymentMethod !== "cash" && draft.paymentMethod !== "mixed") {
    if (draft.linkChoice === "keep") {
      const kept = findCurrentLinkedCandidate(detail, candidates);
      if (kept === null) return { ok: false, errorKey: "budget.receipts.invalidForm" };
      link = {
        account_id: kept.account_id,
        entry_reference: kept.entry_reference,
        candidate_token: kept.candidate_token,
      };
    } else if (draft.linkChoice.startsWith("candidate:")) {
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
