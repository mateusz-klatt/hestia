import type { DeviceInfo, Discovery, Globals, Summary } from "../api/types";
import { battFmt, battLow, fmtTemp, stateStr } from "./format";

/** The DOM nodes the discovery view writes into (queried once in `main.ts`). */
export interface DeviceView {
  hdrText: HTMLElement;
  crib: HTMLElement;
  outdoor: HTMLElement;
  rows: HTMLElement;
}

/** A `<td>` whose text is set via `textContent` — XSS-safe by construction. */
function cell(text: string, className?: string): HTMLTableCellElement {
  const td = document.createElement("td");
  td.textContent = text;
  if (className !== undefined) td.className = className;
  return td;
}

function typeCell(info: DeviceInfo): HTMLTableCellElement {
  const td = document.createElement("td");
  const span = document.createElement("span");
  span.textContent = `${info.type || "?"} (${info.confidence || "?"})`;
  if (info.confidence === "confirmed") span.className = "confirmed";
  td.appendChild(span);
  return td;
}

/** One device row: node / last seen / battery / inferred type / stan / name / room. */
export function deviceRow(node: string, info: DeviceInfo): HTMLTableRowElement {
  const tr = document.createElement("tr");
  tr.dataset.node = node;
  tr.dataset.type = info.type;
  tr.appendChild(cell(node));
  tr.appendChild(cell("—", "seen")); // last seen — static until SSE (PR-3) drives it
  tr.appendChild(cell(battFmt(info.battery), battLow(info.battery) ? "batt low" : "batt"));
  tr.appendChild(typeCell(info));
  tr.appendChild(cell(stateStr(info), "stan"));
  tr.appendChild(cell(info.name ?? ""));
  tr.appendChild(cell(info.room ?? ""));
  return tr;
}

/** A per-endpoint read-only sub-row of a multi-gang switch (label + on/off). */
function subRow(ep: string, on: boolean, name: string): HTMLTableRowElement {
  const tr = document.createElement("tr");
  tr.className = "subrow";
  tr.dataset.ep = ep;
  tr.appendChild(cell("")); // node
  tr.appendChild(cell("")); // last seen
  tr.appendChild(cell("")); // battery
  tr.appendChild(cell(`↳ kanał ${ep}`, "sub-label"));
  tr.appendChild(cell(on ? "on" : "off", "stan"));
  tr.appendChild(cell(name)); // name
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
        tbody.appendChild(subRow(ep, eps[ep] === true, names[ep] ?? ""));
      }
    }
  }
}

export function renderGlobals(cribEl: HTMLElement, outdoorEl: HTMLElement, g: Globals): void {
  cribEl.textContent = fmtTemp(g.crib_temp);
  outdoorEl.textContent = fmtTemp(g.outdoor_temp);
}

export function summaryText(s: Summary): string {
  return `hestia — devices (${String(s.confirmed)}/${String(s.total)} confirmed, ${String(s.unknown)} unknown)`;
}

/** Render the whole read-only discovery view (summary header, globals, table). */
export function renderDiscovery(view: DeviceView, data: Discovery): void {
  view.hdrText.textContent = summaryText(data.summary);
  renderGlobals(view.crib, view.outdoor, data.globals);
  renderDeviceRows(view.rows, data.devices);
}
