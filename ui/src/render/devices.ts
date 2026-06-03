import type { DeviceInfo, Discovery, Globals, Summary } from "../api/types";
import { t } from "../i18n";
import { battFmt, battLow, fmtHumidity, fmtTemp, onOff, stateStr } from "./format";

/** The DOM nodes the discovery view writes into (queried once in `main.ts`). */
export interface DeviceView {
  hdrText: HTMLElement;
  crib: HTMLElement;
  outdoor: HTMLElement;
  outdoorHumidity: HTMLElement;
  rows: HTMLElement;
}

/**
 * A `<td>` whose text is set via `textContent` — XSS-safe by construction.
 * `label` becomes `data-label`, which the mobile card layout surfaces as the
 * cell's heading (the `<thead>` is hidden at narrow widths — see style.css).
 */
function cell(text: string, className?: string, label?: string): HTMLTableCellElement {
  const td = document.createElement("td");
  td.textContent = text;
  if (className !== undefined) td.className = className;
  if (label !== undefined) td.dataset.label = label;
  return td;
}

function statusSpan(): HTMLSpanElement {
  const s = document.createElement("span");
  s.className = "status";
  return s;
}

/** Inferred type + confidence, plus a "✓ confirm" button (disabled when the
 *  type is unknown or already user-confirmed). Wired by the registry binder. */
function typeCell(info: DeviceInfo): HTMLTableCellElement {
  const td = document.createElement("td");
  td.dataset.label = "type";
  const span = document.createElement("span");
  span.textContent = `${info.type || "?"} (${info.confidence || "?"})`;
  if (info.confidence === "confirmed") span.className = "confirmed";
  const confirm = document.createElement("button");
  confirm.type = "button";
  confirm.className = "confirm";
  confirm.textContent = "✓ confirm";
  confirm.disabled = info.type === "" || info.type === "unknown" || info.confidence === "confirmed";
  td.append(span, " ", confirm, statusSpan());
  return td;
}

/** An editable label cell: `<input class="name|room">` + Save + a status span. */
function editCell(field: "name" | "room", value: string): HTMLTableCellElement {
  const td = document.createElement("td");
  td.dataset.label = field;
  const input = document.createElement("input");
  input.className = field;
  input.value = value;
  const save = document.createElement("button");
  save.type = "button";
  save.className = `save-${field}`;
  save.textContent = "Save";
  td.append(input, save, statusSpan());
  return td;
}

/**
 * The "stan" cell: a `.stanval` span carrying live-state text (patched in place
 * by SSE) plus a `.scene-badge` span for transient scene-press badges. Keeping
 * them separate lets a state patch update the value without wiping the badge.
 */
function stanCell(info: DeviceInfo): HTMLTableCellElement {
  const td = document.createElement("td");
  td.className = "stan";
  td.dataset.label = "stan";
  const val = document.createElement("span");
  val.className = "stanval";
  val.textContent = stateStr(info);
  const badge = document.createElement("span");
  badge.className = "scene-badge";
  td.append(val, badge);
  return td;
}

/** One device row: node / last seen / battery / inferred type / stan / name / room. */
export function deviceRow(node: string, info: DeviceInfo): HTMLTableRowElement {
  const tr = document.createElement("tr");
  tr.dataset.node = node;
  tr.dataset.type = info.type;
  tr.appendChild(cell(node, undefined, "node"));
  tr.appendChild(cell("—", "seen", "last seen")); // last seen — static until SSE (PR-3) drives it
  tr.appendChild(cell(battFmt(info.battery), battLow(info.battery) ? "batt low" : "batt", "battery"));
  tr.appendChild(typeCell(info));
  tr.appendChild(stanCell(info));
  tr.appendChild(cell("", "actions", "akcje")); // akcje — control buttons wired by the live decorator (PR-4a)
  tr.appendChild(editCell("name", info.name ?? "")); // name + Save — wired by the registry binder (PR-4b)
  tr.appendChild(editCell("room", info.room ?? ""));
  return tr;
}

/** An editable per-endpoint label cell: `<input class="ep-name">` + Save + status. */
function epNameCell(name: string): HTMLTableCellElement {
  const td = document.createElement("td");
  td.dataset.label = "name";
  const input = document.createElement("input");
  input.className = "ep-name";
  input.value = name;
  const save = document.createElement("button");
  save.type = "button";
  save.className = "save-ep-name";
  save.textContent = "Save";
  td.append(input, save, statusSpan());
  return td;
}

/** A per-endpoint sub-row of a multi-gang switch (label + on/off + editable name). */
function subRow(node: string, ep: string, on: boolean, name: string): HTMLTableRowElement {
  const tr = document.createElement("tr");
  tr.className = "subrow";
  tr.dataset.node = node; // shares its parent's node id so SSE can address it…
  tr.dataset.ep = ep; // …and data-ep makes the individual channel addressable
  tr.appendChild(cell("")); // node
  tr.appendChild(cell("")); // last seen
  tr.appendChild(cell("")); // battery
  tr.appendChild(cell(`↳ kanał ${ep}`, "sub-label"));
  tr.appendChild(cell(onOff(on), "stan ep-stan", "stan"));
  tr.appendChild(cell("")); // akcje (multi-gang channels stay read-only)
  tr.appendChild(epNameCell(name)); // per-channel label — wired by the registry binder (PR-4b)
  tr.appendChild(cell("")); // room
  return tr;
}

/** Replace the table body with one row per device, sorted by numeric node id. */
export function renderDeviceRows(tbody: HTMLElement, devices: Record<string, DeviceInfo>): void {
  tbody.replaceChildren();
  const entries = Object.entries(devices).sort(([a], [b]) => Number(a) - Number(b));
  for (const [node, info] of entries) {
    tbody.appendChild(deviceRow(node, info));
    const eps = info.endpoints;
    if (eps !== null && Object.keys(eps).length > 1) {
      const names = info.endpoint_names ?? {};
      for (const ep of Object.keys(eps).sort((a, b) => Number(a) - Number(b))) {
        tbody.appendChild(subRow(node, ep, eps[ep] === true, names[ep] ?? ""));
      }
    }
  }
}

export function renderGlobals(
  cribEl: HTMLElement,
  outdoorEl: HTMLElement,
  outdoorHumidityEl: HTMLElement,
  g: Globals,
): void {
  cribEl.textContent = fmtTemp(g.crib_temp);
  outdoorEl.textContent = fmtTemp(g.outdoor_temp);
  outdoorHumidityEl.textContent = fmtHumidity(g.outdoor_humidity);
}

/**
 * The header title. Only the noteworthy bits are shown: the `X/Y confirmed`
 * count is omitted once everything is confirmed, and `N unknown` only appears
 * when there are unknowns — so a fully-classified system simply reads
 * "hestia — devices".
 */
export function summaryText(s: Summary): string {
  const parts: string[] = [];
  if (s.confirmed < s.total) parts.push(t("header.confirmed", { confirmed: s.confirmed, total: s.total }));
  if (s.unknown > 0) parts.push(t("header.unknown", { unknown: s.unknown }));
  const title = t("header.title");
  return parts.length > 0 ? `${title} (${parts.join(", ")})` : title;
}

/**
 * The "tryb" status line: the RUNNING mode plus a note — `(cloud-free)` when standalone, an env-override
 * note when `HESTIA_MODE` pins it, or a "graduation saved, restart to apply" note when standalone is the
 * persisted target. Mirrors the legacy dashboard's text (the Phase-3 graduate *button* is intentionally
 * dropped — the appliance is standalone; a rare proxy graduation goes through `POST /api/graduate`).
 */
export function modeText(d: Pick<Discovery, "mode" | "target_mode" | "env_override">): string {
  const running = d.mode || "proxy";
  const target = d.target_mode || "proxy";
  if (d.env_override) {
    return `tryb: ${running} (HESTIA_MODE=${d.env_override} wymusza tryb; zapisany: ${target})`;
  }
  if (running === "standalone") return `tryb: ${running} (cloud-free)`;
  if (target === "standalone") return `tryb: ${running} → standalone zapisane — zrestartuj hestię`;
  return `tryb: ${running}`;
}

export function renderMode(el: HTMLElement, d: Pick<Discovery, "mode" | "target_mode" | "env_override">): void {
  el.textContent = modeText(d);
}

/** Render the whole read-only discovery view (summary header, globals, table). */
export function renderDiscovery(view: DeviceView, data: Discovery): void {
  view.hdrText.textContent = summaryText(data.summary);
  renderGlobals(view.crib, view.outdoor, view.outdoorHumidity, data.globals);
  renderDeviceRows(view.rows, data.devices);
}
