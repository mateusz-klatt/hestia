// Minimal SpreadsheetML workbook packed in a store-only ZIP (method 0, CRC-32). No shared strings:
// text is inline, dates are Excel serials, amounts are decimal tokens with a Polish number format.
import { formulaRisk, isExcelDate } from "./export_rows";

export type XlsxCellKind = "text" | "number" | "date";
export interface XlsxCell { kind: XlsxCellKind; value: string }

const XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
/** LCID 0415 is pl-PL. Excel then shows the grouping and decimal marks for Polish. At least two fraction digits. */
const POLISH_NUMBER = "[$-415]#,##0.00##########";
const DATE_FORMAT = "yyyy-mm-dd";
const WIDTHS = [12, 18, 32, 44, 28, 14, 10, 24, 18, 16, 28] as const;
const encoder = new TextEncoder();

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let index = 0; index < 256; index += 1) {
    let value = index;
    for (let bit = 0; bit < 8; bit += 1) value = (value & 1) === 1 ? (0xedb88320 ^ (value >>> 1)) : (value >>> 1);
    table[index] = value >>> 0;
  }
  return table;
})();

export function crc32(data: Uint8Array): number {
  let crc = 0xffffffff;
  for (const byte of data) {
    const entry = CRC_TABLE[(crc ^ byte) & 0xff];
    crc = ((entry ?? 0) ^ (crc >>> 8)) >>> 0;
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function concat(parts: readonly Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((sum, part) => sum + part.length, 0));
  let offset = 0;
  for (const part of parts) {
    out.set(part, offset);
    offset += part.length;
  }
  return out;
}

function bytesOf(length: number, write: (view: DataView) => void): Uint8Array {
  const bytes = new Uint8Array(length);
  write(new DataView(bytes.buffer));
  return bytes;
}

function u16(value: number): Uint8Array {
  return bytesOf(2, (view) => { view.setUint16(0, value, true); });
}

function u32(value: number): Uint8Array {
  return bytesOf(4, (view) => { view.setUint32(0, value, true); });
}

interface ZipEntry { name: string; data: Uint8Array }

/** DOS timestamp frozen so two builds of the same sheet match. 2026-01-01 00:00. */
const DOS_TIME = 0;
const DOS_DATE = ((2026 - 1980) << 9) | (1 << 5) | 1;
const UTF8_FLAG = 0x0800;

export function zipStore(entries: readonly ZipEntry[]): Uint8Array {
  const locals: Uint8Array[] = [];
  const central: Uint8Array[] = [];
  let offset = 0;
  for (const entry of entries) {
    const name = encoder.encode(entry.name);
    const crc = crc32(entry.data);
    const local = concat([
      u32(0x04034b50), u16(20), u16(UTF8_FLAG), u16(0), u16(DOS_TIME), u16(DOS_DATE), u32(crc),
      u32(entry.data.length), u32(entry.data.length), u16(name.length), u16(0), name, entry.data,
    ]);
    locals.push(local);
    central.push(concat([
      u32(0x02014b50), u16(20), u16(20), u16(UTF8_FLAG), u16(0), u16(DOS_TIME), u16(DOS_DATE), u32(crc),
      u32(entry.data.length), u32(entry.data.length), u16(name.length), u16(0), u16(0), u16(0), u16(0),
      u32(0), u32(offset), name,
    ]));
    offset += local.length;
  }
  const directory = concat(central);
  const end = concat([
    u32(0x06054b50), u16(0), u16(0), u16(entries.length), u16(entries.length), u32(directory.length), u32(offset), u16(0),
  ]);
  return concat([...locals, directory, end]);
}

/** XML 1.0 Char: tab, LF, CR, and the legal planes. Excludes C0 controls, surrogates, U+FFFE and U+FFFF. */
function isXmlChar(code: number): boolean {
  return code === 0x9 || code === 0xa || code === 0xd
    || (code >= 0x20 && code <= 0xd7ff)
    || (code >= 0xe000 && code <= 0xfffd)
    || (code >= 0x10000 && code <= 0x10ffff);
}

function xml(value: string): string {
  let out = "";
  for (const char of value) {
    const code = char.codePointAt(0) ?? 0;
    if (!isXmlChar(code)) continue;
    if (char === "&") out += "&amp;";
    else if (char === "<") out += "&lt;";
    else if (char === ">") out += "&gt;";
    else if (char === "\"") out += "&quot;";
    else out += char;
  }
  return out;
}

function needsPreserve(value: string): boolean {
  return value !== value.trim() || /[\n\r\t]/.test(value);
}

function columnName(index: number): string {
  let value = index + 1;
  let name = "";
  while (value > 0) {
    const digit = (value - 1) % 26;
    name = String.fromCharCode(65 + digit) + name;
    value = Math.floor((value - 1) / 26);
  }
  return name;
}

/** Days since 1899-12-30. Dates before 1900-03-01 are not serialised; Excel's false leap day shifts them. */
export function excelSerial(iso: string): number | null {
  if (!isExcelDate(iso)) return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  if (match === null) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const stamp = Date.UTC(year, month - 1, day);
  const check = new Date(stamp);
  if (check.getUTCFullYear() !== year || check.getUTCMonth() !== month - 1 || check.getUTCDate() !== day) return null;
  return Math.round((stamp - Date.UTC(1899, 11, 30)) / 86_400_000);
}

const SHEET_FALLBACK = "Transactions";

function trimSheet(value: string): string {
  let current = value;
  for (let guard = 0; guard < 6; guard += 1) {
    const next = current.trim().replace(/^'+/, "").replace(/'+$/, "");
    if (next === current) return current;
    current = next;
  }
  return current.trim();
}

/** At most 31 UTF-16 code units, never ending on a high surrogate. */
function truncateUnits(value: string, max: number): string {
  if (value.length <= max) return value;
  const cut = value.charCodeAt(max - 1);
  const end = cut >= 0xd800 && cut <= 0xdbff ? max - 1 : max;
  return value.slice(0, end);
}

/** Excel forbids : \\ / ? * [ ], wrapping apostrophes, a trailing space, an empty name, and "History". */
export function sheetTitle(name: string): string {
  const cleaned = trimSheet(truncateUnits(trimSheet(name.replace(/[:\\/?*[\]]/g, " ")), 31));
  if (cleaned === "" || cleaned.toLowerCase() === "history") return SHEET_FALLBACK;
  return cleaned;
}

function cellXml(cell: XlsxCell, ref: string): string {
  if (cell.value === "") return "";
  if (cell.kind === "number" && /^-?\d+(\.\d+)?$/.test(cell.value)) return `<c r="${ref}" s="2"><v>${cell.value}</v></c>`;
  if (cell.kind === "date") {
    const serial = excelSerial(cell.value);
    if (serial !== null) return `<c r="${ref}" s="1"><v>${String(serial)}</v></c>`;
  }
  const text = xml(cell.value);
  const space = needsPreserve(cell.value) ? " xml:space=\"preserve\"" : "";
  const quoted = formulaRisk(cell.value) ? " s=\"3\"" : "";
  return `<c r="${ref}"${quoted} t="inlineStr"><is><t${space}>${text}</t></is></c>`;
}

function stylesXml(): string {
  return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <numFmts count="2">
    <numFmt numFmtId="164" formatCode="${DATE_FORMAT}"/>
    <numFmt numFmtId="165" formatCode="${POLISH_NUMBER}"/>
  </numFmts>
  <fonts count="1"><font><sz val="11"/><name val="Calibri"/><family val="2"/></font></fonts>
  <fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
  <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="4">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
    <xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" quotePrefix="1"/>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>`;
}

function worksheetXml(headers: readonly string[], rows: readonly (readonly XlsxCell[])[]): string {
  const lastRow = rows.length + 1;
  const lastColumn = columnName(Math.max(headers.length, 1) - 1);
  const width = WIDTHS.map((value, index) => `<col min="${String(index + 1)}" max="${String(index + 1)}" width="${String(value)}" customWidth="1"/>`).join("");
  const header = headers.map((value, index) => cellXml({ kind: "text", value }, `${columnName(index)}1`)).join("");
  const body = rows.map((row, rowIndex) => {
    const cells = row.map((cell, index) => cellXml(cell, `${columnName(index)}${String(rowIndex + 2)}`)).join("");
    return `<row r="${String(rowIndex + 2)}">${cells}</row>`;
  }).join("");
  const ref = `A1:${lastColumn}${String(lastRow)}`;
  return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <dimension ref="${ref}"/>
  <sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/><selection pane="bottomLeft" activeCell="A2" sqref="A2"/></sheetView></sheetViews>
  <cols>${width}</cols>
  <sheetData><row r="1">${header}</row>${body}</sheetData>
  <autoFilter ref="${ref}"/>
</worksheet>`;
}

function workbookParts(headers: readonly string[], rows: readonly (readonly XlsxCell[])[], sheetName: string): ZipEntry[] {
  const title = sheetTitle(sheetName);
  const files: [string, string][] = [
    ["[Content_Types].xml", `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>`],
    ["_rels/.rels", `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>`],
    ["xl/workbook.xml", `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="${xml(title)}" sheetId="1" r:id="rId1"/></sheets>
</workbook>`],
    ["xl/_rels/workbook.xml.rels", `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>`],
    ["xl/worksheets/sheet1.xml", worksheetXml(headers, rows)],
    ["xl/styles.xml", stylesXml()],
  ];
  return files.map(([name, xmlText]) => ({ name, data: encoder.encode(xmlText) }));
}

export function buildWorkbook(headers: readonly string[], rows: readonly (readonly XlsxCell[])[], sheetName: string): Uint8Array {
  if (rows.length > 1_048_575) throw new Error("export_too_many_rows");
  return zipStore(workbookParts(headers, rows, sheetName));
}

export function workbookBlob(headers: readonly string[], rows: readonly (readonly XlsxCell[])[], sheetName: string): Blob {
  const bytes = buildWorkbook(headers, rows, sheetName);
  const copy = new ArrayBuffer(bytes.byteLength);
  new Uint8Array(copy).set(bytes);
  return new Blob([copy], { type: XLSX_MIME });
}
