// Excel and CSV download for the entries the owner is looking at. The ZIP writer loads only for XLSX.
import { useEffect, useRef, useState } from "react";
import { t } from "../../i18n";
import type { BudgetApi } from "../api";
import type { BudgetAccount, BudgetConnection } from "../model";
import { transactionRowKey } from "./model";
import type { AnalyticsTransaction, AnalyticsTransactionsByKeysRequest, TransactionFilter } from "./model";
import {
  EXPORT_PAGE_SIZE, ExportCancelled, ExportFailed, ExportSnapshotChanged, ExportStopped, buildCsv, categoryIndex,
  exportCells, exportHeaders, mapExportRow, walkTransactions,
} from "./export_rows";
import type { ExportRecord } from "./export_rows";
import type { XlsxCell } from "./xlsx_writer";

type Format = "xlsx" | "csv";
type Phase = "idle" | "running" | "error";

export interface ExportMenuProps {
  /** File name without an extension, for example `hestia-transakcje-2026-11`. */
  filename: string;
  /** Periods whose pages are walked in order. A month is one period; a year is its twelve months. */
  periods: readonly string[];
  filter: TransactionFilter;
  /** When set, export these row keys (the provisional view) instead of every entry in the period. */
  rowKeys?: readonly string[] | undefined;
  accounts: readonly BudgetAccount[];
  connections: readonly BudgetConnection[];
  api: Pick<BudgetApi, "analyticsTransactions" | "categories"> & Partial<Pick<BudgetApi, "analyticsTransactionsByKeys">>;
  onAccessLoss: (result: { ok: boolean; status?: number; error?: string }) => boolean;
  /** Test seam. The browser download is used when this is omitted. */
  save?: ((blob: Blob, filename: string) => void) | undefined;
}

function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.rel = "noopener";
  document.body.append(link);
  try {
    link.click();
  } finally {
    link.remove();
    const timer = setTimeout(() => { URL.revokeObjectURL(url); }, 30_000);
    const handle = timer as unknown as { unref?: () => void };
    handle.unref?.();
  }
}

function keyRequest(
  keys: readonly string[], filter: TransactionFilter, cursor: string | undefined,
): AnalyticsTransactionsByKeysRequest {
  return {
    row_keys: [...keys],
    limit: EXPORT_PAGE_SIZE,
    ...(cursor === undefined ? {} : { cursor }),
    ...(filter.currency === "" ? {} : { currency: filter.currency }),
    ...(filter.category === "" ? {} : { category_id: filter.category }),
    ...(filter.kind === "" ? {} : { kind: filter.kind as NonNullable<AnalyticsTransactionsByKeysRequest["kind"]> }),
    ...(filter.query === "" ? {} : { query: filter.query }),
  };
}

function sheetRows(records: readonly ExportRecord[]): XlsxCell[][] {
  return records.map((record) => exportCells(record).map((value, index): XlsxCell => {
    if (index === 0 && record.bookingIsDate) return { kind: "date", value };
    if (index === 1 && record.transactionIsDate) return { kind: "date", value };
    if (index === 5 && record.amountIsNumber) return { kind: "number", value };
    return { kind: "text", value };
  }));
}

async function fileBlob(format: Format, records: readonly ExportRecord[]): Promise<Blob> {
  const headers = exportHeaders();
  if (format === "csv") return new Blob([buildCsv(headers, records)], { type: "text/csv;charset=utf-8" });
  const writer = await import("./xlsx_writer");
  return writer.workbookBlob(headers, sheetRows(records), t("budget.export.sheet"));
}

export function ExportMenu({ filename, periods, filter, rowKeys, accounts, connections, api, onAccessLoss, save }: ExportMenuProps) {
  const [open, setOpen] = useState(false);
  const [phase, setPhase] = useState<Phase>("idle");
  const [count, setCount] = useState(0);
  const [format, setFormat] = useState<Format>("csv");
  const run = useRef(0);
  const write = save ?? downloadBlob;
  const scope = `${filename}|${periods.join(",")}|${filter.currency}|${filter.category}|${filter.kind}|${filter.query}|${rowKeys === undefined ? "" : rowKeys.join("\u0000")}`;
  const [scopeKey, setScopeKey] = useState(scope);
  if (scopeKey !== scope) {
    setScopeKey(scope);
    run.current += 1;
    setPhase("idle");
    setOpen(false);
  }
  useEffect(() => () => { run.current += 1; }, []);

  const start = (next: Format) => {
    const token = ++run.current;
    const activeFilter = filter;
    const activeKeys = rowKeys;
    const activePeriods = activeKeys === undefined ? periods : [periods[0] ?? "keys"];
    setFormat(next);
    setOpen(false);
    setPhase("running");
    setCount(0);
    const loadPage = async (period: string, cursor: string | undefined) => {
      if (activeKeys !== undefined) {
        const byKeys = api.analyticsTransactionsByKeys;
        if (byKeys === undefined) throw new ExportFailed("export_unavailable");
        const result = await byKeys(keyRequest(activeKeys, activeFilter, cursor));
        if (onAccessLoss(result)) throw new ExportStopped();
        if (!result.ok) throw new ExportFailed(result.error);
        const allowed = new Set(activeKeys);
        return {
          transactions: result.data.transactions.filter((item) => allowed.has(transactionRowKey(item))),
          nextCursor: result.data.next_cursor,
          snapshotDigest: result.data.snapshot_digest,
        };
      }
      const result = cursor === undefined
        ? await api.analyticsTransactions(period, activeFilter, undefined, EXPORT_PAGE_SIZE)
        : await api.analyticsTransactions(period, activeFilter, cursor, EXPORT_PAGE_SIZE);
      if (onAccessLoss(result)) throw new ExportStopped();
      if (!result.ok) throw new ExportFailed(result.error);
      return { transactions: result.data.transactions, nextCursor: result.data.next_cursor, snapshotDigest: result.data.snapshot_digest };
    };
    const hooks = {
      isCancelled: () => token !== run.current,
      onProgress: (loaded: number) => {
        if (token !== run.current) return;
        setCount(loaded);
      },
    };
    void (async () => {
      try {
        const categories = await api.categories();
        if (token !== run.current) return;
        if (onAccessLoss(categories)) throw new ExportStopped();
        if (!categories.ok) throw new ExportFailed(categories.error);
        const names = categoryIndex(categories.data.categories);
        const loadRows = () => (activeKeys !== undefined && activeKeys.length === 0
          ? Promise.resolve([])
          : walkTransactions(activePeriods, loadPage, hooks));
        let rows: AnalyticsTransaction[];
        try {
          rows = await loadRows();
        } catch (error) {
          if (!(error instanceof ExportSnapshotChanged) || token !== run.current) throw error;
          setCount(0);
          rows = await loadRows();
        }
        if (token !== run.current) return;
        const records = rows.map((row) => mapExportRow(row, { accounts, connections, categories: names }));
        const blob = await fileBlob(next, records);
        if (token !== run.current) return;
        write(blob, `${filename}.${next}`);
        setPhase("idle");
      } catch (error) {
        if (token !== run.current) return;
        if (error instanceof ExportCancelled || error instanceof ExportStopped) {
          setPhase("idle");
          return;
        }
        setPhase("error");
      }
    })();
  };

  const cancel = () => {
    run.current += 1;
    setPhase("idle");
    setOpen(false);
  };

  return (
    <div className="budget-export">
      <button type="button" aria-haspopup="menu" aria-expanded={open} disabled={phase === "running"}
        onClick={() => { setOpen((current) => !current); }}>
        {t("budget.export.open")}
      </button>
      {open && phase !== "running" ? (
        <div role="menu" className="budget-export-menu">
          <button type="button" role="menuitem" onClick={() => { start("xlsx"); }}>{t("budget.export.xlsx")}</button>
          <button type="button" role="menuitem" onClick={() => { start("csv"); }}>{t("budget.export.csv")}</button>
        </div>
      ) : null}
      {phase === "running" ? (
        <div className="budget-muted budget-export-progress" role="status">
          <span>{t("budget.export.fetching", { count })}</span>
          <button type="button" onClick={cancel}>{t("budget.export.cancel")}</button>
        </div>
      ) : null}
      {phase === "error" ? (
        <div className="budget-notice budget-entries-action" role="alert">
          <span>{t("budget.export.error")}</span>
          <button type="button" onClick={() => { start(format); }}>{t("budget.export.retry")}</button>
        </div>
      ) : null}
    </div>
  );
}
