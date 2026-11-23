// Synthetic data only: no real account, transaction or bank values.
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BudgetApi, BudgetResult } from "../api";
import { account, connection } from "../fixtures";
import { categoryList, entriesOf, entry, overviewOf } from "./fixtures";
import type { AnalyticsCategory, AnalyticsTransaction, AnalyticsTransactions } from "./model";
import { accountName } from "./model";
import { ExportMenu } from "./export_menu";
import {
  EXPORT_PAGE_SIZE, ExportCancelled, ExportSnapshotChanged, buildCsv, categoryIndex, csvAmount, csvField, exportBasename,
  exportHeaders, exportLabels, exportStatus, exportTypeLabel, mapExportRow, monthsOfYear, signedAmount, walkTransactions,
} from "./export_rows";
import type { ExportContext, ExportRecord } from "./export_rows";
import { buildWorkbook, crc32, excelSerial, sheetTitle } from "./xlsx_writer";
import type { XlsxCell } from "./xlsx_writer";
import { TransactionsPanel, transactionLabels } from "./transactions";
import { YearView, yearLabels } from "./year";

const text = exportLabels.pl;
const categories: AnalyticsCategory[] = [
  { id: "home", name: "Home", archived: false, version: 1, created_at: 1, updated_at: 1, parent_id: null },
  { id: "rent", name: "Rent", archived: false, version: 1, created_at: 1, updated_at: 1, parent_id: "home" },
  { id: "slash", name: "Food / Drink", archived: false, version: 1, created_at: 1, updated_at: 1, parent_id: null },
];
const accounts = [account({
  id: "acc-secret", connection: "bank",
  display: { product: "Synthetic Current", currency: "PLN", masked_suffix: "0001", source: "offline_import", observed_at: 1, imported_at: 2 },
})];
const connections = [connection];
const context: ExportContext = { accounts, connections, categories: categoryIndex(categories), text };

function row(changes: Partial<AnalyticsTransaction> = {}): AnalyticsTransaction {
  return entry("secret-ref", {
    account_id: "acc-secret", amount: "12.5", currency: "PLN", credit_debit_indicator: "DBIT", status: "BOOK",
    booking_date: "2026-11-03", value_date: "2026-11-02", transaction_date: "2026-11-01",
    remittance_information: ["Synthetic shop", "  ", "note"],
    metadata: { creditor_name: "Synthetic Market", debtor_name: "Synthetic Payer", mcc: null },
    effective: { kind: "expense", category_id: "rent", source: "manual", rule_id: null },
    fingerprint: "fp-secret", transfer_fingerprint: "tfp-secret", ...changes,
  });
}

function ok<T>(data: T): Promise<BudgetResult<T>> { return Promise.resolve({ ok: true, data }); }
function fail<T>(status: number, error: string): Promise<BudgetResult<T>> { return Promise.resolve({ ok: false, status, error }); }

describe("transaction export rows", () => {
  it("maps the leaf category, the signed amount and the type and status labels", () => {
    const mapped = mapExportRow(row(), context);
    expect(mapped.category).toBe("Rent");
    expect(mapped.category).not.toContain("Home");
    expect(mapExportRow(row({ effective: { kind: "expense", category_id: "slash", source: "manual", rule_id: null } }), context).category)
      .toBe("Food / Drink");
    expect(mapped.type).toBe("wydatek");
    expect(mapExportRow(row({ effective: { kind: "income", category_id: null, source: "default", rule_id: null } }), context).type).toBe("przychód");
    expect(mapExportRow(row({ effective: { kind: "excluded", category_id: null, source: "manual", rule_id: null } }), context).type).toBe("wykluczone");
    expect(exportTypeLabel("refund", text)).toBe("zwrot");
    expect(exportTypeLabel("transfer", text)).toBe("przelew własny");
    expect(exportTypeLabel("unclassified_credit", text)).toBe("do sklasyfikowania");
    expect(mapped.status).toBe("potwierdzone");
    expect(exportStatus("PDNG", text)).toBe("wstępne");
    expect(exportStatus("BOOK", exportLabels.en)).toBe("confirmed");
    expect(mapped.amount).toBe("-12.5");
    expect(mapped.amountIsNumber).toBe(true);
    expect(signedAmount("0.00", "DBIT")).toEqual({ text: "0.00", numeric: true });
    expect(signedAmount("100.00", "CRDT").text).toBe("100.00");
    expect(mapped.bookingDate).toBe("2026-11-03");
    expect(mapped.bookingIsDate).toBe(true);
    expect(mapped.transactionDate).toBe("2026-11-01");
    expect(mapExportRow(row({ booking_date: null }), context).bookingDate).toBe("2026-11-02");
    expect(mapExportRow(row({ booking_date: null, value_date: null, transaction_date: "2026-11-01" }), context).bookingDate).toBe("");
    const early = mapExportRow(row({ booking_date: "1899-12-31", transaction_date: "1900-02-28" }), context);
    expect(early.bookingDate).toBe("1899-12-31");
    expect(early.bookingIsDate).toBe(false);
    expect(early.transactionIsDate).toBe(false);
    expect(mapExportRow(row({ booking_date: "1900-03-01" }), context).bookingIsDate).toBe(true);
    expect(mapped.description).toBe("Synthetic shop · note");
    expect(mapped.counterparty).toBe("Synthetic Market");
    expect(mapExportRow(row({ credit_debit_indicator: "CRDT" }), context).counterparty).toBe("Synthetic Payer");
    expect(mapped.account).toBe(accountName(accounts, connections, "acc-secret", text.accountFallback));
    expect(mapped.currency).toBe("PLN");
    const encoded = JSON.stringify(mapped);
    expect(encoded).not.toContain("acc-secret");
    expect(encoded).not.toContain("secret-ref");
    expect(encoded).not.toContain("fp-secret");
    expect(encoded).not.toContain("tfp-secret");
    expect(mapExportRow(row({ effective: { kind: "expense", category_id: null, source: "default", rule_id: null } }), context).category)
      .toBe("Bez kategorii");
  });

  it("names a month file and a year file, and lists every month of the year", () => {
    expect(exportBasename("2026-11")).toBe("hestia-transakcje-2026-11");
    expect(exportBasename("2026")).toBe("hestia-transakcje-2026");
    expect(exportLabels.pl.fetching(4)).toBe("Pobieranie… 4");
    expect(exportHeaders(text)).toEqual([
      "Data", "Data transakcji", "Konto", "Opis", "Kontrahent", "Kwota", "Waluta", "Kategoria", "Typ", "Status",
    ]);
    expect(monthsOfYear("2026")).toEqual([
      "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06",
      "2026-07", "2026-08", "2026-09", "2026-10", "2026-11", "2026-12",
    ]);
  });
});

describe("transaction export csv", () => {
  it("writes a UTF-8 BOM, semicolons, CRLF and RFC 4180 quotes", () => {
    expect(csvField("plain")).toBe("plain");
    expect(csvField("a;b")).toBe("\"a;b\"");
    expect(csvField("say \"hi\"")).toBe("\"say \"\"hi\"\"\"");
    expect(csvField("line\r\nnext")).toBe("\"line\r\nnext\"");
    expect(csvField("=cmd")).toBe("'=cmd");
    expect(csvField("+cmd")).toBe("'+cmd");
    expect(csvField("@cmd")).toBe("'@cmd");
    expect(csvField("-12.5")).toBe("'-12.5");
    expect(csvField(" =SUM(1)")).toBe("' =SUM(1)");
    expect(csvField("\t=cmd")).toBe("'\t=cmd");
    expect(csvField("\r=cmd")).toBe("\"'\r=cmd\"");
    expect(csvAmount("-12.5")).toBe("-12,5");
    expect(csvAmount("0.00")).toBe("0,00");
    expect(csvAmount("100")).toBe("100");
    expect(csvAmount("=12")).toBe("'=12");
    const record: ExportRecord = {
      ...mapExportRow(row(), context),
      description: "say \"hi\";\nnext",
      counterparty: "A;B",
    };
    const csv = buildCsv(exportHeaders(text), [record]);
    const bytes = new TextEncoder().encode(csv);
    expect([...bytes.slice(0, 3)]).toEqual([0xef, 0xbb, 0xbf]);
    expect(csv.startsWith("\uFEFFData;Data transakcji;")).toBe(true);
    expect(csv).toContain("\"say \"\"hi\"\";\nnext\"");
    expect(csv).toContain("\"A;B\"");
    expect(csv).toContain(";-12,5;PLN;");
    expect(csv).not.toContain("-12.5");
    expect(csv.endsWith("\r\n")).toBe(true);
    expect(csv.split("\r\n").filter((line) => line !== "")).toHaveLength(2);
  });
});

function independentCrc(data: Uint8Array): number {
  let crc = 0xffffffff;
  for (const byte of data) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1));
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function unzip(bytes: Uint8Array): Map<string, string> {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const files = new Map<string, string>();
  const locals: { name: string; crc: number; size: number; offset: number }[] = [];
  let offset = 0;
  while (offset + 30 <= bytes.length && view.getUint32(offset, true) === 0x04034b50) {
    const method = view.getUint16(offset + 8, true);
    const stored = view.getUint32(offset + 14, true);
    const size = view.getUint32(offset + 18, true);
    const nameLength = view.getUint16(offset + 26, true);
    const extraLength = view.getUint16(offset + 28, true);
    const nameStart = offset + 30;
    const dataStart = nameStart + nameLength + extraLength;
    const dataEnd = dataStart + size;
    if (dataEnd > bytes.length) throw new Error("truncated zip");
    if (method !== 0) throw new Error(`compressed entry ${String(method)}`);
    const data = bytes.subarray(dataStart, dataEnd);
    if (independentCrc(data) !== stored) throw new Error("crc mismatch");
    const name = new TextDecoder().decode(bytes.subarray(nameStart, nameStart + nameLength));
    files.set(name, new TextDecoder().decode(data));
    locals.push({ name, crc: stored, size, offset });
    offset = dataEnd;
  }
  if (locals.length === 0) throw new Error("zip had no files");
  const centralStart = offset;
  const central: { name: string; crc: number; size: number; local: number }[] = [];
  while (offset + 46 <= bytes.length && view.getUint32(offset, true) === 0x02014b50) {
    const method = view.getUint16(offset + 10, true);
    const crc = view.getUint32(offset + 16, true);
    const size = view.getUint32(offset + 24, true);
    const nameLength = view.getUint16(offset + 28, true);
    const extraLength = view.getUint16(offset + 30, true);
    const commentLength = view.getUint16(offset + 32, true);
    const local = view.getUint32(offset + 42, true);
    const nameStart = offset + 46;
    const nameEnd = nameStart + nameLength + extraLength + commentLength;
    if (nameEnd > bytes.length) throw new Error("truncated central directory");
    if (method !== 0) throw new Error(`compressed central entry ${String(method)}`);
    const name = new TextDecoder().decode(bytes.subarray(nameStart, nameStart + nameLength));
    central.push({ name, crc, size, local });
    offset = nameEnd;
  }
  if (offset + 22 > bytes.length || view.getUint32(offset, true) !== 0x06054b50) throw new Error("missing eocd");
  const entries = view.getUint16(offset + 10, true);
  const directorySize = view.getUint32(offset + 12, true);
  const directoryOffset = view.getUint32(offset + 16, true);
  if (entries !== locals.length || entries !== central.length) throw new Error("entry count");
  if (directoryOffset !== centralStart || directorySize !== offset - centralStart) throw new Error("directory range");
  for (let index = 0; index < central.length; index += 1) {
    const item = central[index];
    const local = locals[index];
    if (item === undefined || local === undefined || item.name !== local.name || item.crc !== local.crc
      || item.size !== local.size || item.local !== local.offset) throw new Error("central mismatch");
  }
  return files;
}

describe("transaction export xlsx", () => {
  it("packs a stored workbook whose sheet has numeric amounts, dates and a frozen header", () => {
    expect(crc32(new TextEncoder().encode("123456789"))).toBe(0xcbf43926);
    const mapped = mapExportRow(row({ remittance_information: ["Fish & chips"] }), context);
    const headers = exportHeaders(text);
    const cells: XlsxCell[] = [
      { kind: "date", value: mapped.bookingDate },
      { kind: "date", value: mapped.transactionDate },
      { kind: "text", value: mapped.account },
      { kind: "text", value: mapped.description },
      { kind: "text", value: mapped.counterparty },
      { kind: "number", value: mapped.amount },
      { kind: "text", value: mapped.currency },
      { kind: "text", value: mapped.category },
      { kind: "text", value: mapped.type },
      { kind: "text", value: mapped.status },
    ];
    const packed = buildWorkbook(headers, [cells], text.sheet);
    const files = unzip(packed);
    const types = files.get("[Content_Types].xml") ?? "";
    const workbook = files.get("xl/workbook.xml") ?? "";
    const sheet = files.get("xl/worksheets/sheet1.xml") ?? "";
    const styles = files.get("xl/styles.xml") ?? "";
    expect(types).toContain("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml");
    expect(types).toContain("application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml");
    expect(types).toContain("application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml");
    expect(workbook).toContain("name=\"Transakcje\"");
    expect(styles).toContain("formatCode=\"[$-415]#,##0.00##########\"");
    expect(styles).toContain("formatCode=\"yyyy-mm-dd\"");
    const booking = excelSerial("2026-11-03");
    const traded = excelSerial("2026-11-01");
    expect(booking).toBe(Math.round((Date.UTC(2026, 10, 3) - Date.UTC(1899, 11, 30)) / 86_400_000));
    expect(sheet).toContain(`<c r="A2" s="1"><v>${String(booking)}</v></c>`);
    expect(sheet).toContain(`<c r="B2" s="1"><v>${String(traded)}</v></c>`);
    expect(sheet).toContain("<c r=\"F2\" s=\"2\"><v>-12.5</v></c>");
    expect(sheet).toContain("Fish &amp; chips");
    expect(sheet).toContain("ySplit=\"1\"");
    expect(sheet).toContain("state=\"frozen\"");
    expect(sheet).toContain("autoFilter ref=\"A1:J2\"");
    expect(sheet).toContain("width=\"44\"");
    expect(sheet).not.toContain("acc-secret");
    expect(sheet).not.toContain("secret-ref");
    expect(sheet).not.toContain("fp-secret");
    expect(excelSerial("1900-02-28")).toBeNull();
    expect(excelSerial("1900-03-01")).not.toBeNull();
  });

  it("quotes formula text, strips XML-illegal characters, and sanitises the sheet title", () => {
    const risky = buildWorkbook(["Opis"], [[{ kind: "text", value: "\t=cmd\uFFFE\uFFFF" }]], "Sheet");
    const sheet = unzip(risky).get("xl/worksheets/sheet1.xml") ?? "";
    const styles = unzip(risky).get("xl/styles.xml") ?? "";
    expect(styles).toContain("quotePrefix=\"1\"");
    expect(sheet).toContain("s=\"3\"");
    expect(sheet).toContain("xml:space=\"preserve\"");
    expect(sheet).toContain("\t=cmd");
    expect(sheet).not.toContain("\uFFFE");
    expect(sheet).not.toContain("\uFFFF");
    const early = buildWorkbook(["Data"], [[{ kind: "date", value: "1899-12-31" }]], "Dates");
    expect(unzip(early).get("xl/worksheets/sheet1.xml")).toContain(">1899-12-31</t>");
    expect(sheetTitle("'Sales'")).toBe("Sales");
    expect(sheetTitle("Sales ")).toBe("Sales");
    expect(sheetTitle("'")).toBe("Transactions");
    expect(sheetTitle("History")).toBe("Transactions");
    expect(sheetTitle("history")).toBe("Transactions");
    expect(sheetTitle("  'History'  ")).toBe("Transactions");
    const emoji = "\uD83D\uDE00";
    expect(sheetTitle("a".repeat(30) + emoji)).toBe("a".repeat(30));
    expect(sheetTitle("b".repeat(29) + emoji)).toBe("b".repeat(29) + emoji);
    expect(/[\uD800-\uDFFF]/.test(sheetTitle("a".repeat(30) + emoji))).toBe(false);
    const reserved = unzip(buildWorkbook(["A"], [], "History")).get("xl/workbook.xml") ?? "";
    expect(reserved).toContain("name=\"Transactions\"");
    expect(() => unzip(risky.subarray(0, risky.length - 22))).toThrow("missing eocd");
  });
});

const idle = { isCancelled: () => false, onProgress: () => {} };

describe("transaction export pagination", () => {
  it("walks every page of each period and resets the cursor between months", async () => {
    const calls: [string, string | undefined][] = [];
    const rows = await walkTransactions(["2026-01", "2026-02"], (period, cursor) => {
      calls.push([period, cursor]);
      if (period === "2026-01" && cursor === undefined) return { transactions: [entry("a")], nextCursor: "p2" };
      if (period === "2026-01") return { transactions: [entry("a"), entry("b")], nextCursor: null };
      return { transactions: [entry("c")], nextCursor: null };
    }, idle);
    expect(calls).toEqual([["2026-01", undefined], ["2026-01", "p2"], ["2026-02", undefined]]);
    expect(rows.map((item) => item.entry_reference)).toEqual(["a", "b", "c"]);
  });

  it("walks all twelve months of a year", async () => {
    const seen: string[] = [];
    await walkTransactions(monthsOfYear("2026"), (period) => {
      seen.push(period);
      return { transactions: [], nextCursor: null };
    }, idle);
    expect(seen).toHaveLength(12);
    expect(seen[0]).toBe("2026-01");
    expect(seen[11]).toBe("2026-12");
  });

  it("stops on a repeated cursor instead of following it forever", async () => {
    let calls = 0;
    const rows = await walkTransactions(["2026-11"], () => {
      calls += 1;
      if (calls > 4) throw new Error("did not stop");
      return { transactions: [entry(`r${String(calls)}`)], nextCursor: "loop" };
    }, idle);
    expect(calls).toBe(2);
    expect(rows).toHaveLength(2);
  });

  it("rejects when cancelled and does not return a partial list", async () => {
    let calls = 0;
    let cancelled = false;
    const progress: number[] = [];
    const task = walkTransactions(["2026-11"], () => {
      calls += 1;
      return { transactions: [entry("a")], nextCursor: "next" };
    }, {
      isCancelled: () => cancelled,
      onProgress: (loaded) => {
        progress.push(loaded);
        cancelled = true;
      },
    });
    await expect(task).rejects.toBeInstanceOf(ExportCancelled);
    expect(calls).toBe(1);
    expect(progress).toEqual([1]);
  });

  it("rejects when a later page fails and keeps the progress from the pages already read", async () => {
    const progress: number[] = [];
    const load = vi.fn((_period: string, cursor: string | undefined) => {
      if (cursor === undefined) return { transactions: [entry("a"), entry("b")], nextCursor: "next" };
      throw new Error("budget_network");
    });
    await expect(walkTransactions(["2026-11"], load, {
      isCancelled: () => false,
      onProgress: (loaded) => { progress.push(loaded); },
    })).rejects.toThrow("budget_network");
    expect(load).toHaveBeenCalledTimes(2);
    expect(progress).toEqual([2]);
  });

  it("rejects when a later page belongs to a different snapshot", async () => {
    const task = walkTransactions(["2026-01", "2026-02"], (period) => ({
      transactions: [entry(period)], nextCursor: null, snapshotDigest: period === "2026-01" ? "one" : "two",
    }), idle);
    await expect(task).rejects.toBeInstanceOf(ExportSnapshotChanged);
  });
});

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve: (value: T) => void = () => {};
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

async function flush(): Promise<void> {
  for (let attempt = 0; attempt < 8; attempt += 1) {
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
  }
}

describe("export menu", () => {
  let host: HTMLElement;
  let root: Root;
  beforeEach(() => {
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
  });
  afterEach(async () => {
    await act(async () => { root.unmount(); await Promise.resolve(); });
    host.remove();
  });

  function button(label: string): HTMLButtonElement {
    const found = [...host.querySelectorAll("button")].find((item) => item.textContent === label);
    if (found === undefined) throw new Error(`button ${label} missing`);
    return found;
  }
  async function click(label: string): Promise<void> {
    await act(async () => { button(label).click(); await Promise.resolve(); });
  }

  it("shows progress, then downloads CSV for the active filter, and retries after an error", async () => {
    const saved: { name: string; bytes: Promise<ArrayBuffer> }[] = [];
    const save = (blob: Blob, name: string) => { saved.push({ name, bytes: blob.arrayBuffer() }); };
    let failFirst = true;
    const analyticsTransactions = vi.fn<BudgetApi["analyticsTransactions"]>((_period, _filter, cursor) => {
      if (failFirst) {
        failFirst = false;
        return fail(500, "budget_network");
      }
      const page: AnalyticsTransactions = cursor === undefined
        ? entriesOf("2026-11", [row()], "next")
        : entriesOf("2026-11", [row({ entry_reference: "second-ref", remittance_information: ["Second"] })], null);
      return ok(page);
    });
    const api = {
      analyticsTransactions,
      categories: vi.fn<BudgetApi["categories"]>(() => ok({ categories })),
    };
    await act(async () => {
      root.render(<ExportMenu text={text} filename="hestia-transakcje-2026-11" periods={["2026-11"]}
        filter={{ currency: "PLN", category: "rent", kind: "expense", query: "shop" }}
        accounts={accounts} connections={connections} api={api} onAccessLoss={() => false} save={save} />);
      await Promise.resolve();
    });
    await click("Eksportuj");
    expect(button("XLSX")).toBeDefined();
    await click("CSV");
    await flush();
    expect(host.textContent).toContain("Nie udało się pobrać transakcji.");
    expect(analyticsTransactions).toHaveBeenCalledWith("2026-11",
      { currency: "PLN", category: "rent", kind: "expense", query: "shop" }, undefined, EXPORT_PAGE_SIZE);
    await click("Ponów");
    await flush();
    expect(saved).toHaveLength(1);
    expect(saved[0]?.name).toBe("hestia-transakcje-2026-11.csv");
    const bytes = new Uint8Array(await saved[0]?.bytes ?? new ArrayBuffer(0));
    expect([...bytes.slice(0, 3)]).toEqual([0xef, 0xbb, 0xbf]);
    const body = new TextDecoder("utf-8", { ignoreBOM: true }).decode(bytes);
    expect(body.startsWith("\uFEFF")).toBe(true);
    expect(body).toContain("Synthetic shop · note");
    expect(body).toContain("Second");
    expect(body).not.toContain("acc-secret");
    expect(analyticsTransactions.mock.calls.filter((call) => call[3] === EXPORT_PAGE_SIZE).length).toBeGreaterThanOrEqual(2);
  });

  it("cancels an in-flight download and does not save the file", async () => {
    const pending = deferred<BudgetResult<AnalyticsTransactions>>();
    const analyticsTransactions = vi.fn<BudgetApi["analyticsTransactions"]>(() => pending.promise);
    const save = vi.fn();
    await act(async () => {
      root.render(<ExportMenu text={exportLabels.en} filename="hestia-transakcje-2026-11" periods={["2026-11"]}
        filter={{ currency: "", category: "", kind: "", query: "" }} accounts={accounts} connections={connections}
        api={{ analyticsTransactions, categories: vi.fn<BudgetApi["categories"]>(() => ok({ categories: categoryList })) }}
        onAccessLoss={() => false} save={save} />);
      await Promise.resolve();
    });
    await click("Export");
    await click("CSV");
    await flush();
    expect(host.textContent).toContain("Fetching… 0");
    await click("Cancel");
    await act(async () => { pending.resolve({ ok: true, data: entriesOf("2026-11", [row()], "next") }); await Promise.resolve(); });
    await flush();
    expect(save).not.toHaveBeenCalled();
    expect(analyticsTransactions).toHaveBeenCalledTimes(1);
    expect(host.textContent).not.toContain("Fetching…");
  });

  it("builds an xlsx blob from the lazy writer", async () => {
    const saved: Blob[] = [];
    const analyticsTransactions = vi.fn<BudgetApi["analyticsTransactions"]>(() => ok(entriesOf("2026-11", [row()])));
    await act(async () => {
      root.render(<ExportMenu text={text} filename="hestia-transakcje-2026-11" periods={["2026-11"]}
        filter={{ currency: "", category: "", kind: "", query: "" }} accounts={accounts} connections={connections}
        api={{ analyticsTransactions, categories: vi.fn<BudgetApi["categories"]>(() => ok({ categories })) }}
        onAccessLoss={() => false} save={(blob) => { saved.push(blob); }} />);
      await Promise.resolve();
    });
    await click("Eksportuj");
    await click("XLSX");
    await flush();
    const blob = saved[0];
    if (blob === undefined) throw new Error("xlsx was not saved");
    const bytes = new Uint8Array(await blob.arrayBuffer());
    expect([...bytes.slice(0, 2)]).toEqual([0x50, 0x4b]);
    expect(unzip(bytes).get("xl/worksheets/sheet1.xml")).toContain("<v>-12.5</v>");
  });

  it("loads categories before the months and retries once when the snapshot changes", async () => {
    const saved: string[] = [];
    const order: string[] = [];
    const digests = ["one", "two", "one", "one"];
    const analyticsTransactions = vi.fn<BudgetApi["analyticsTransactions"]>((period) => {
      order.push(period);
      const snapshot_digest = digests.shift() ?? "one";
      return ok({ ...entriesOf(period, [row()]), snapshot_digest });
    });
    const loadCategories = vi.fn<BudgetApi["categories"]>(() => {
      order.push("categories");
      return ok({ categories });
    });
    await act(async () => {
      root.render(<ExportMenu text={text} filename="hestia-transakcje-2026" periods={["2026-01", "2026-02"]}
        filter={{ currency: "", category: "", kind: "", query: "" }} accounts={accounts} connections={connections}
        api={{ analyticsTransactions, categories: loadCategories }} onAccessLoss={() => false}
        save={(blob, name) => { saved.push(`${name}:${String(blob.size)}`); }} />);
      await Promise.resolve();
    });
    await click("Eksportuj");
    await click("CSV");
    await flush();
    expect(saved[0]).toMatch(/^hestia-transakcje-2026\.csv:/);
    expect(order).toEqual(["categories", "2026-01", "2026-02", "2026-01", "2026-02"]);
  });

  it("does not request months when the category read reports an ended session", async () => {
    const analyticsTransactions = vi.fn<BudgetApi["analyticsTransactions"]>(() => ok(entriesOf("2026-01", [])));
    const save = vi.fn();
    const onAccessLoss = vi.fn(() => true);
    await act(async () => {
      root.render(<ExportMenu text={text} filename="hestia-transakcje-2026" periods={monthsOfYear("2026")}
        filter={{ currency: "", category: "", kind: "", query: "" }} accounts={accounts} connections={connections}
        api={{ analyticsTransactions, categories: vi.fn<BudgetApi["categories"]>(() => fail(401, "budget_http_401")) }}
        onAccessLoss={onAccessLoss} save={save} />);
      await Promise.resolve();
    });
    await click("Eksportuj");
    await click("CSV");
    await flush();
    expect(onAccessLoss).toHaveBeenCalled();
    expect(analyticsTransactions).not.toHaveBeenCalled();
    expect(save).not.toHaveBeenCalled();
    expect(host.textContent).not.toContain("Ponów");
  });

  it("writes only the header when the provisional key list is empty", async () => {
    const analyticsTransactions = vi.fn<BudgetApi["analyticsTransactions"]>(() => ok(entriesOf("2026-11", [row()])));
    const analyticsTransactionsByKeys = vi.fn<BudgetApi["analyticsTransactionsByKeys"]>(() => ok(entriesOf("", [row()])));
    const saved: Promise<ArrayBuffer>[] = [];
    await act(async () => {
      root.render(<ExportMenu text={text} filename="hestia-transakcje-2026-11" periods={["2026-11"]} rowKeys={[]}
        filter={{ currency: "PLN", category: "", kind: "", query: "" }} accounts={accounts} connections={connections}
        api={{ analyticsTransactions, analyticsTransactionsByKeys, categories: vi.fn<BudgetApi["categories"]>(() => ok({ categories })) }}
        onAccessLoss={() => false} save={(blob) => { saved.push(blob.arrayBuffer()); }} />);
      await Promise.resolve();
    });
    await click("Eksportuj");
    await click("CSV");
    await flush();
    expect(analyticsTransactions).not.toHaveBeenCalled();
    expect(analyticsTransactionsByKeys).not.toHaveBeenCalled();
    const raw = await saved[0];
    if (raw === undefined) throw new Error("csv was not saved");
    const body = new TextDecoder("utf-8", { ignoreBOM: true }).decode(new Uint8Array(raw));
    expect(body.startsWith("\uFEFFData;")).toBe(true);
    expect(body).not.toContain("Synthetic");
  });
});

describe("export buttons on the month and year views", () => {
  let host: HTMLElement;
  let root: Root;
  const downloads: { name: string; body: Promise<string> }[] = [];
  beforeEach(() => {
    downloads.length = 0;
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
    vi.spyOn(URL, "createObjectURL").mockImplementation((blob) => {
      if (blob instanceof Blob) downloads.push({ name: "", body: blob.text() });
      return "blob:hestia";
    });
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      const last = downloads.at(-1);
      if (last !== undefined) last.name = this.download;
    });
  });
  afterEach(async () => {
    await act(async () => { root.unmount(); await Promise.resolve(); });
    host.remove();
    vi.restoreAllMocks();
  });
  async function flushView(): Promise<void> {
    for (let attempt = 0; attempt < 10; attempt += 1) {
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    }
  }
  function button(label: string): HTMLButtonElement {
    const found = [...host.querySelectorAll("button")].find((item) => item.textContent === label);
    if (found === undefined) throw new Error(`button ${label} missing`);
    return found;
  }

  it("exports the month view with the applied currency filter", async () => {
    const analyticsTransactions = vi.fn<BudgetApi["analyticsTransactions"]>(() => ok(entriesOf("2026-11", [row()])));
    const api = {
      analyticsTransactions,
      analyticsTransactionsByKeys: vi.fn<BudgetApi["analyticsTransactionsByKeys"]>(() => ok(entriesOf("", [row()]))),
      categories: vi.fn<BudgetApi["categories"]>(() => ok({ categories })),
      annotate: vi.fn<BudgetApi["annotate"]>(),
      decideTransfer: vi.fn<BudgetApi["decideTransfer"]>(),
    };
    await act(async () => {
      root.render(<TransactionsPanel api={api} period="2026-11" currencies={["PLN"]} accounts={accounts} connections={connections}
        text={transactionLabels.en} locale="en" revision={0} onAccessLoss={() => false} onChanged={() => {}} />);
      await Promise.resolve();
    });
    await flushView();
    const currency = host.querySelector<HTMLSelectElement>("select[name=\"currency\"]");
    if (currency === null) throw new Error("currency filter missing");
    await act(async () => {
      currency.value = "PLN";
      currency.dispatchEvent(new Event("change", { bubbles: true }));
      await Promise.resolve();
    });
    await flushView();
    await act(async () => { button("Apply").click(); await Promise.resolve(); });
    await flushView();
    await act(async () => { button("Export").click(); await Promise.resolve(); });
    await act(async () => { button("CSV").click(); await Promise.resolve(); });
    await flushView();
    const exportCall = analyticsTransactions.mock.calls.find((call) => call[3] === EXPORT_PAGE_SIZE);
    expect(exportCall?.[0]).toBe("2026-11");
    expect(exportCall?.[1]).toMatchObject({ currency: "PLN" });
    expect(downloads.at(-1)?.name).toBe("hestia-transakcje-2026-11.csv");
    expect(await downloads.at(-1)?.body).toContain("Rent");
  });

  it("exports the provisional keys the month view is showing", async () => {
    const keys = ["acc-secret/secret-ref"];
    const analyticsTransactions = vi.fn<BudgetApi["analyticsTransactions"]>(() => ok(entriesOf("2026-11", [
      row({ entry_reference: "whole-month", remittance_information: ["Whole month"] }),
    ])));
    const analyticsTransactionsByKeys = vi.fn<BudgetApi["analyticsTransactionsByKeys"]>(() => ok(entriesOf("", [row()])));
    const api = {
      analyticsTransactions,
      analyticsTransactionsByKeys,
      categories: vi.fn<BudgetApi["categories"]>(() => ok({ categories })),
      annotate: vi.fn<BudgetApi["annotate"]>(),
      decideTransfer: vi.fn<BudgetApi["decideTransfer"]>(),
    };
    await act(async () => {
      root.render(<TransactionsPanel api={api} period="2026-11" currencies={["PLN"]} accounts={accounts} connections={connections}
        text={transactionLabels.en} locale="en" revision={0} onAccessLoss={() => false} onChanged={() => {}}
        initialCurrency="PLN" initialProvisionalKeys={keys} />);
      await Promise.resolve();
    });
    await flushView();
    expect(host.querySelector<HTMLInputElement>("input[name=\"provisional\"]")?.checked).toBe(true);
    await act(async () => { button("Export").click(); await Promise.resolve(); });
    await act(async () => { button("CSV").click(); await Promise.resolve(); });
    await flushView();
    expect(analyticsTransactions).not.toHaveBeenCalled();
    const exportCall = analyticsTransactionsByKeys.mock.calls.find((call) => call[0].limit === EXPORT_PAGE_SIZE);
    expect(exportCall?.[0]).toMatchObject({ row_keys: keys, currency: "PLN" });
    expect(downloads.at(-1)?.name).toBe("hestia-transakcje-2026-11.csv");
    const body = await downloads.at(-1)?.body;
    expect(body).toContain("Synthetic shop");
    expect(body).not.toContain("Whole month");
  });

  it("exports all twelve months from the year view", async () => {
    const periods: string[] = [];
    const analyticsTransactions = vi.fn<BudgetApi["analyticsTransactions"]>((period) => {
      periods.push(period);
      return ok(entriesOf(period, []));
    });
    const overview = vi.fn<BudgetApi["overview"]>((period) => ok(overviewOf(period, { currencies: [], months: [] })));
    await act(async () => {
      root.render(<YearView api={{ overview, analyticsTransactions, categories: vi.fn<BudgetApi["categories"]>(() => ok({ categories: [] })) }}
        accounts={[]} text={yearLabels.en} locale="en" revision={0} onAccessLoss={() => false} onOpenMonth={() => {}}
        today={() => new Date(2026, 10, 21, 12)} />);
      await Promise.resolve();
    });
    await flushView();
    await act(async () => { button("Export").click(); await Promise.resolve(); });
    await act(async () => { button("CSV").click(); await Promise.resolve(); });
    await flushView();
    expect(periods).toEqual(monthsOfYear("2026"));
    expect(downloads.at(-1)?.name).toBe("hestia-transakcje-2026.csv");
    expect(analyticsTransactions.mock.calls.every((call) => call[3] === EXPORT_PAGE_SIZE)).toBe(true);
  });
});
