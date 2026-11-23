// Browser export of analytics entries. Category cells are the stored leaf name only: names are globally unique,
// and a parent path is never built. Amounts stay dot-decimal strings for the workbook; CSV rewrites the amount
// column with a decimal comma. A debit indicator supplies the sign. The booking column follows effective_date.
import type { BudgetAccount, BudgetConnection } from "../model";
import { transactionText } from "../model";
import { accountName, counterparty } from "./model";
import type { AnalyticsCategory, AnalyticsTransaction } from "./model";

export const exportLabels = {
  en: {
    open: "Export", xlsx: "XLSX", csv: "CSV", sheet: "Transactions",
    fetching: (count: number) => `Fetching… ${String(count)}`,
    cancel: "Cancel", retry: "Retry", error: "Transactions could not be downloaded.",
    accountFallback: "Account", uncategorised: "Uncategorised",
    columns: {
      bookingDate: "Booking date", transactionDate: "Transaction date", account: "Account", description: "Description",
      counterparty: "Counterparty", amount: "Amount", currency: "Currency", category: "Category", type: "Type",
      status: "Status",
    },
    types: {
      expense: "expense", income: "income", excluded: "excluded", refund: "refund", transfer: "own transfer",
      unclassified: "to classify",
    },
    statusBooked: "confirmed", statusPreliminary: "preliminary",
  },
  pl: {
    open: "Eksportuj", xlsx: "XLSX", csv: "CSV", sheet: "Transakcje",
    fetching: (count: number) => `Pobieranie… ${String(count)}`,
    cancel: "Anuluj", retry: "Ponów", error: "Nie udało się pobrać transakcji.",
    accountFallback: "Konto", uncategorised: "Bez kategorii",
    columns: {
      bookingDate: "Data", transactionDate: "Data transakcji", account: "Konto", description: "Opis",
      counterparty: "Kontrahent", amount: "Kwota", currency: "Waluta", category: "Kategoria", type: "Typ",
      status: "Status",
    },
    types: {
      expense: "wydatek", income: "przychód", excluded: "wykluczone", refund: "zwrot", transfer: "przelew własny",
      unclassified: "do sklasyfikowania",
    },
    statusBooked: "potwierdzone", statusPreliminary: "wstępne",
  },
};
export type ExportLabels = typeof exportLabels.en;

export const EXPORT_PAGE_SIZE = 200;

const PERIOD = /^\d{4}(?:-(?:0[1-9]|1[0-2]))?$/;
const DECIMAL = /^(-?)(\d+)(?:\.(\d+))?$/;

export function exportBasename(period: string): string {
  return PERIOD.test(period) ? `hestia-transakcje-${period}` : "hestia-transakcje";
}

/** The twelve calendar months of a year, January first. An invalid year yields nothing. */
export function monthsOfYear(year: string): string[] {
  if (!/^\d{4}$/.test(year)) return [];
  return Array.from({ length: 12 }, (_, index) => `${year}-${String(index + 1).padStart(2, "0")}`);
}

export function exportHeaders(text: ExportLabels): string[] {
  const columns = text.columns;
  return [
    columns.bookingDate, columns.transactionDate, columns.account, columns.description, columns.counterparty,
    columns.amount, columns.currency, columns.category, columns.type, columns.status,
  ];
}

/** Booked entries are confirmed; every other bank status is preliminary. */
export function exportStatus(status: string, text: ExportLabels): string {
  return status === "BOOK" ? text.statusBooked : text.statusPreliminary;
}

/** Effective kind as an export label. Unknown kinds stay as the server sent them. */
export function exportTypeLabel(kind: string, text: ExportLabels): string {
  switch (kind) {
    case "expense": return text.types.expense;
    case "income": return text.types.income;
    case "excluded": return text.types.excluded;
    case "refund": return text.types.refund;
    case "transfer": return text.types.transfer;
    case "unclassified_credit": return text.types.unclassified;
    default: return kind;
  }
}

export interface ExportRecord {
  bookingDate: string;
  transactionDate: string;
  account: string;
  description: string;
  counterparty: string;
  amount: string;
  currency: string;
  category: string;
  type: string;
  status: string;
  bookingIsDate: boolean;
  transactionIsDate: boolean;
  amountIsNumber: boolean;
}

export interface ExportContext {
  accounts: readonly BudgetAccount[];
  connections: readonly BudgetConnection[];
  /** Leaf names keyed by category id. Built once per export. */
  categories: ReadonlyMap<string, string>;
  text: ExportLabels;
}

const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;

export function isIsoDate(value: string): boolean {
  const match = ISO_DATE.exec(value);
  if (match === null) return false;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const stamp = new Date(Date.UTC(year, month - 1, day));
  return stamp.getUTCFullYear() === year && stamp.getUTCMonth() === month - 1 && stamp.getUTCDate() === day;
}

/** Excel's 1900 leap-year bug makes serials before 1 March 1900 disagree with the civil date. Those stay text. */
const EXCEL_DATE_MIN = "1900-03-01";

export function isExcelDate(value: string): boolean {
  return value >= EXCEL_DATE_MIN && isIsoDate(value);
}

export function categoryIndex(categories: readonly AnalyticsCategory[]): ReadonlyMap<string, string> {
  return new Map(categories.map((item) => [item.id, item.name]));
}

/** A signed decimal token. Zero stays unsigned. Anything that is not a decimal is returned unchanged. */
export function signedAmount(amount: string, indicator: string): { text: string; numeric: boolean } {
  const match = DECIMAL.exec(amount.trim());
  if (match === null) return { text: amount.trim(), numeric: false };
  const whole = (match[2] ?? "0").replace(/^0+(?=\d)/, "");
  const fraction = match[3];
  const body = fraction === undefined ? whole : `${whole}.${fraction}`;
  const zero = /^0+$/.test(whole) && (fraction === undefined || /^0+$/.test(fraction));
  const negative = !zero && ((match[1] === "-") !== (indicator === "DBIT"));
  return { text: `${negative ? "-" : ""}${body}`, numeric: true };
}

function leafCategory(categories: ReadonlyMap<string, string>, id: string | null, fallback: string): string {
  if (id === null) return fallback;
  return categories.get(id) ?? fallback;
}

export function mapExportRow(row: AnalyticsTransaction, context: ExportContext): ExportRecord {
  const bookingDate = row.booking_date ?? row.value_date ?? "";
  const transactionDate = row.transaction_date ?? "";
  const amount = signedAmount(row.amount, row.credit_debit_indicator);
  return {
    bookingDate, transactionDate,
    account: accountName(context.accounts, context.connections, row.account_id, context.text.accountFallback),
    description: transactionText(row),
    counterparty: counterparty(row) ?? "",
    amount: amount.text,
    currency: row.currency,
    category: leafCategory(context.categories, row.effective.category_id, context.text.uncategorised),
    type: exportTypeLabel(row.effective.kind, context.text),
    status: exportStatus(row.status, context.text),
    bookingIsDate: isExcelDate(bookingDate),
    transactionIsDate: isExcelDate(transactionDate),
    amountIsNumber: amount.numeric,
  };
}

export function exportCells(record: ExportRecord): string[] {
  return [
    record.bookingDate, record.transactionDate, record.account, record.description, record.counterparty,
    record.amount, record.currency, record.category, record.type, record.status,
  ];
}

const FORMULA_LEAD = new Set(["=", "+", "-", "@", "\t", "\r"]);

/**
 * Text that a spreadsheet may treat as a formula: a leading tab or CR, or `= + - @` as the first
 * character or the first character after spaces. The amount column is the only numeric exception.
 */
export function formulaRisk(value: string): boolean {
  const first = value[0];
  if (first !== undefined && FORMULA_LEAD.has(first)) return true;
  const skipped = value.trimStart()[0];
  return skipped !== undefined && FORMULA_LEAD.has(skipped);
}

function prefixFormula(value: string): string {
  return formulaRisk(value) ? `'${value}` : value;
}

/** One RFC 4180 field. The separator is semicolon, so a comma needs no quotes. Text is never left numeric. */
export function csvField(value: string): string {
  const safe = prefixFormula(value);
  if (/[;"\r\n]/.test(safe)) return `"${safe.replaceAll("\"", "\"\"")}"`;
  return safe;
}

const DECIMAL_AMOUNT = /^-?\d+(?:\.\d+)?$/;

/** Amount column only: a plain decimal keeps its sign and uses a Polish decimal comma. Other text is neutralised. */
export function csvAmount(value: string): string {
  if (DECIMAL_AMOUNT.test(value)) return value.replace(".", ",");
  return csvField(value);
}

/** UTF-8 CSV with a leading BOM, semicolon separators and CRLF records. */
export function buildCsv(headers: readonly string[], records: readonly ExportRecord[]): string {
  const header = headers.map(csvField).join(";");
  const body = records.map((record) => exportCells(record).map((value, index) => (
    index === 5 ? csvAmount(value) : csvField(value)
  )).join(";"));
  return `\uFEFF${[header, ...body].join("\r\n")}\r\n`;
}

export interface ExportPage {
  transactions: readonly AnalyticsTransaction[];
  nextCursor: string | null;
  /** Present on analytics pages. A later page with a different digest aborts the walk. */
  snapshotDigest?: string;
}

export class ExportCancelled extends Error {
  constructor() {
    super("export_cancelled");
    this.name = "ExportCancelled";
  }
}

export class ExportFailed extends Error {
  readonly code: string;
  constructor(code: string) {
    super(code);
    this.name = "ExportFailed";
    this.code = code;
  }
}

/** The projection changed between pages. The caller retries the walk once, then surfaces the error. */
export class ExportSnapshotChanged extends Error {
  constructor() {
    super("export_snapshot_changed");
    this.name = "ExportSnapshotChanged";
  }
}

/** The session barrier already cleared the screen; the export stops without a retry prompt. */
export class ExportStopped extends Error {
  constructor() {
    super("export_stopped");
    this.name = "ExportStopped";
  }
}

export interface WalkHooks {
  isCancelled: () => boolean;
  onProgress: (loaded: number) => void;
}

function rowIdentity(row: AnalyticsTransaction): string {
  return `${row.account_id}\u0000${row.entry_reference}`;
}

/**
 * Every page of each period, in order. A repeated cursor or a page that adds nothing ends that period.
 * Cancel rejects; it does not return a partial list. Errors from `load` propagate.
 */
export async function walkTransactions(
  periods: readonly string[],
  load: (period: string, cursor: string | undefined) => ExportPage | Promise<ExportPage>,
  hooks: WalkHooks,
): Promise<AnalyticsTransaction[]> {
  const rows: AnalyticsTransaction[] = [];
  const seen = new Set<string>();
  let digest: string | undefined;
  for (const period of periods) {
    let cursor: string | undefined;
    const cursors = new Set<string>();
    for (;;) {
      if (hooks.isCancelled()) throw new ExportCancelled();
      const page = await load(period, cursor);
      if (hooks.isCancelled()) throw new ExportCancelled();
      if (page.snapshotDigest !== undefined) {
        if (digest === undefined) digest = page.snapshotDigest;
        else if (page.snapshotDigest !== digest) throw new ExportSnapshotChanged();
      }
      let added = 0;
      for (const row of page.transactions) {
        const key = rowIdentity(row);
        if (seen.has(key)) continue;
        seen.add(key);
        rows.push(row);
        added += 1;
      }
      hooks.onProgress(rows.length);
      if (hooks.isCancelled()) throw new ExportCancelled();
      const next = page.nextCursor;
      if (next === null || next === "" || added === 0 || next === cursor || cursors.has(next)) break;
      cursors.add(next);
      cursor = next;
    }
  }
  return rows;
}
