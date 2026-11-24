// Browser export of analytics entries. Category cells are the stored leaf name only: names are globally unique,
// and a parent path is never built. Amounts stay dot-decimal strings for the workbook; CSV rewrites the amount
// column with a decimal comma. A debit indicator supplies the sign. The booking column follows effective_date.
// The last column is the annotation's one-off label, blank when it is unset.
import { t } from "../../i18n";
import type { BudgetAccount, BudgetConnection } from "../model";
import { transactionText } from "../model";
import { EXPORT_TYPE_KEYS, accountName, counterparty, labelOf } from "./model";
import type { AnalyticsCategory, AnalyticsTransaction } from "./model";

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

export function exportHeaders(): string[] {
  return [
    t("budget.export.colBookingDate"), t("budget.export.colTransactionDate"), t("budget.export.colAccount"),
    t("budget.export.colDescription"), t("budget.export.colCounterparty"), t("budget.export.colAmount"),
    t("budget.export.colCurrency"), t("budget.export.colCategory"), t("budget.export.colType"),
    t("budget.export.colStatus"), t("budget.export.colOneOffLabel"),
  ];
}

/** Booked entries are confirmed; every other bank status is preliminary. */
export function exportStatus(status: string): string {
  return status === "BOOK" ? t("budget.export.statusBooked") : t("budget.export.statusPreliminary");
}

/** Effective kind as an export label. Unknown kinds stay as the server sent them. */
export function exportTypeLabel(kind: string): string {
  if (kind === "unclassified_credit") return t("budget.export.types.unclassified");
  return labelOf(EXPORT_TYPE_KEYS, kind);
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
  /** Annotation label, or "" when the entry has none. */
  oneOffLabel: string;
  bookingIsDate: boolean;
  transactionIsDate: boolean;
  amountIsNumber: boolean;
}

export interface ExportContext {
  accounts: readonly BudgetAccount[];
  connections: readonly BudgetConnection[];
  /** Leaf names keyed by category id. Built once per export. */
  categories: ReadonlyMap<string, string>;
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

/** The stored one-off label. Null and a missing field both export as a blank cell. */
function exportOneOffLabel(row: AnalyticsTransaction): string {
  const value = row.annotation.label;
  return typeof value === "string" ? value : "";
}

export function mapExportRow(row: AnalyticsTransaction, context: ExportContext): ExportRecord {
  const bookingDate = row.booking_date ?? row.value_date ?? "";
  const transactionDate = row.transaction_date ?? "";
  const amount = signedAmount(row.amount, row.credit_debit_indicator);
  return {
    bookingDate, transactionDate,
    account: accountName(context.accounts, context.connections, row.account_id, t("budget.export.accountFallback")),
    description: transactionText(row),
    counterparty: counterparty(row) ?? "",
    amount: amount.text,
    currency: row.currency,
    category: leafCategory(context.categories, row.effective.category_id, t("budget.export.uncategorised")),
    type: exportTypeLabel(row.effective.kind),
    status: exportStatus(row.status),
    oneOffLabel: exportOneOffLabel(row),
    bookingIsDate: isExcelDate(bookingDate),
    transactionIsDate: isExcelDate(transactionDate),
    amountIsNumber: amount.numeric,
  };
}

export function exportCells(record: ExportRecord): string[] {
  return [
    record.bookingDate, record.transactionDate, record.account, record.description, record.counterparty,
    record.amount, record.currency, record.category, record.type, record.status, record.oneOffLabel,
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
