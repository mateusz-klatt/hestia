// Excel and CSV download for the entries the owner is looking at. The ZIP writer loads only for XLSX.
import { useEffect, useRef, useState } from "react";
import type { BudgetApi } from "../api";
import type { BudgetAccount, BudgetConnection } from "../model";
import type { TransactionFilter } from "./model";
import {
  EXPORT_PAGE_SIZE, ExportCancelled, ExportFailed, ExportStopped, buildCsv, exportCells, exportHeaders, mapExportRow,
  walkTransactions,
} from "./export_rows";
import type { ExportLabels, ExportRecord } from "./export_rows";
import type { XlsxCell } from "./xlsx_writer";

type Format = "xlsx" | "csv";
type Phase = "idle" | "running" | "error";

export interface ExportMenuProps {
  text: ExportLabels;
  /** File name without an extension, for example `hestia-transakcje-2026-11`. */
  filename: string;
  /** Periods whose pages are walked in order. A month is one period; a year is its twelve months. */
  periods: readonly string[];
  filter: TransactionFilter;
  accounts: readonly BudgetAccount[];
  connections: readonly BudgetConnection[];
  api: Pick<BudgetApi, "analyticsTransactions" | "categories">;
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
  link.click();
  link.remove();
  setTimeout(() => { URL.revokeObjectURL(url); }, 0);
}

function sheetRows(records: readonly ExportRecord[]): XlsxCell[][] {
  return records.map((record) => exportCells(record).map((value, index): XlsxCell => {
    if (index === 0 && record.bookingIsDate) return { kind: "date", value };
    if (index === 1 && record.transactionIsDate) return { kind: "date", value };
    if (index === 5 && record.amountIsNumber) return { kind: "number", value };
    return { kind: "text", value };
  }));
}

async function fileBlob(text: ExportLabels, format: Format, records: readonly ExportRecord[]): Promise<Blob> {
  const headers = exportHeaders(text);
  if (format === "csv") return new Blob([buildCsv(headers, records)], { type: "text/csv;charset=utf-8" });
  const writer = await import("./xlsx_writer");
  return writer.workbookBlob(headers, sheetRows(records), text.sheet);
}

export function ExportMenu({ text, filename, periods, filter, accounts, connections, api, onAccessLoss, save }: ExportMenuProps) {
  const [open, setOpen] = useState(false);
  const [phase, setPhase] = useState<Phase>("idle");
  const [count, setCount] = useState(0);
  const [format, setFormat] = useState<Format>("csv");
  const run = useRef(0);
  const write = save ?? downloadBlob;
  const scope = `${filename}|${periods.join(",")}|${filter.currency}|${filter.category}|${filter.kind}|${filter.query}`;
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
    const activePeriods = periods;
    setFormat(next);
    setOpen(false);
    setPhase("running");
    setCount(0);
    const loadPage = async (period: string, cursor: string | undefined) => {
      const result = cursor === undefined
        ? await api.analyticsTransactions(period, activeFilter, undefined, EXPORT_PAGE_SIZE)
        : await api.analyticsTransactions(period, activeFilter, cursor, EXPORT_PAGE_SIZE);
      if (onAccessLoss(result)) throw new ExportStopped();
      if (!result.ok) throw new ExportFailed(result.error);
      return { transactions: result.data.transactions, nextCursor: result.data.next_cursor };
    };
    void (async () => {
      try {
        const [rows, categories] = await Promise.all([
          walkTransactions(activePeriods, loadPage, {
            isCancelled: () => token !== run.current,
            onProgress: (loaded) => {
              if (token !== run.current) return;
              setCount(loaded);
            },
          }),
          api.categories(),
        ]);
        if (token !== run.current) return;
        if (onAccessLoss(categories)) throw new ExportStopped();
        if (!categories.ok) throw new ExportFailed(categories.error);
        const records = rows.map((row) => mapExportRow(row, {
          accounts, connections, categories: categories.data.categories, text,
        }));
        const blob = await fileBlob(text, next, records);
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
        {text.open}
      </button>
      {open && phase !== "running" ? (
        <div role="menu" className="budget-export-menu">
          <button type="button" role="menuitem" onClick={() => { start("xlsx"); }}>{text.xlsx}</button>
          <button type="button" role="menuitem" onClick={() => { start("csv"); }}>{text.csv}</button>
        </div>
      ) : null}
      {phase === "running" ? (
        <div className="budget-muted budget-export-progress" role="status">
          <span>{text.fetching(count)}</span>
          <button type="button" onClick={cancel}>{text.cancel}</button>
        </div>
      ) : null}
      {phase === "error" ? (
        <div className="budget-notice budget-entries-action" role="alert">
          <span>{text.error}</span>
          <button type="button" onClick={() => { start(format); }}>{text.retry}</button>
        </div>
      ) : null}
    </div>
  );
}
