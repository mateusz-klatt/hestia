import type { ControlOp, ControlResult, DeviceInfo } from "./api/types";
import { t } from "./i18n";
import { fmtTemp } from "./render/format";

/** The Keemple TRVs' setpoint range, in whole °C (the device reports/accepts integer Celsius). */
const THERMOSTAT_MIN_C = 4;
const THERMOSTAT_MAX_C = 28;

/** Sends one control op; returns a normalised result (never rejects). */
export type PostControl = (op: ControlOp) => Promise<ControlResult>;

const LEVEL_PRESETS = [10, 25, 50, 75, 99];

/**
 * Build the "akcje" control buttons for a device into `cell`, wired to
 * `postControl`. A shared in-flight lock disables every button for the
 * round-trip (no concurrent sends) and is released in `finally`, so a failed
 * send can't wedge them; the outcome is shown in a status span.
 */
export function renderActions(
  cell: HTMLElement,
  node: number,
  info: DeviceInfo,
  postControl: PostControl,
): void {
  cell.replaceChildren();

  const buttons: HTMLButtonElement[] = [];
  const status = document.createElement("span");
  status.className = "status";
  let busy = false;

  const setDisabled = (disabled: boolean): void => {
    for (const b of buttons) b.disabled = disabled;
  };

  const send = async (op: ControlOp, pending: string): Promise<void> => {
    if (busy) return;
    busy = true;
    setDisabled(true);
    status.textContent = pending.length > 0 ? `… ${pending}` : "…";
    status.className = "status";
    try {
      const res = await postControl(op);
      status.textContent = res.ok ? t("ctl.sent") : `✗ ${res.error ?? t("ctl.failed")}`;
      status.className = res.ok ? "status" : "status err";
    } catch {
      status.textContent = t("ctl.error");
      status.className = "status err";
    } finally {
      busy = false;
      setDisabled(false);
    }
  };

  const addButton = (label: string, op: () => ControlOp, pending: () => string = () => ""): void => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = label;
    btn.style.marginRight = "0.3rem";
    btn.addEventListener("click", () => {
      void send(op(), pending());
    });
    buttons.push(btn);
    cell.appendChild(btn);
  };

  const addLevelSelect = (): void => {
    const sel = document.createElement("select");
    sel.style.marginRight = "0.3rem";
    for (const value of LEVEL_PRESETS) {
      const o = document.createElement("option");
      o.value = String(value);
      o.textContent = `${String(value)}%`;
      sel.appendChild(o);
    }
    cell.appendChild(sel);
    addButton(t("ctl.set"), () => ({ op: "level", node, value: Number(sel.value) }));
  };

  const endpointLabel = (ep: string): string => {
    const name = info.endpoint_names?.[ep]?.trim();
    return name !== undefined && name !== "" ? name : `#${ep}`;
  };

  if (info.endpoints !== null) {
    const endpoints = Object.keys(info.endpoints).sort((a, b) => Number(a) - Number(b));
    for (const ep of endpoints) {
      const label = endpointLabel(ep);
      const endpoint = Number(ep);
      addButton(`${label} ${t("ctl.on")}`, () => ({ op: "switch", node, endpoint, on: true }));
      addButton(`${label} ${t("ctl.off")}`, () => ({ op: "switch", node, endpoint, on: false }));
    }
    if (buttons.length > 0) cell.appendChild(status);
    return;
  }

  if (info.type === "light") {
    if (info.level !== null) {
      addButton(t("ctl.off"), () => ({ op: "level", node, value: 0 }));
      addButton(t("ctl.on"), () => ({ op: "level", node, value: 99 }));
      addLevelSelect();
    } else {
      addButton(t("ctl.on"), () => ({ op: "switch", node, on: true }));
      addButton(t("ctl.off"), () => ({ op: "switch", node, on: false }));
    }
  } else if (info.type === "plug") {
    addButton(t("ctl.on"), () => ({ op: "switch", node, on: true }));
    addButton(t("ctl.off"), () => ({ op: "switch", node, on: false }));
  } else if (info.type === "blind") {
    addButton(t("ctl.raise"), () => ({ op: "cover", node, value: 99 }));
    addButton(t("ctl.lower"), () => ({ op: "cover", node, value: 0 }));
  } else if (info.type === "thermostat") {
    addButton(t("ctl.off"), () => ({ op: "thermostat_power", node, on: false }));
    addButton(t("ctl.on"), () => ({ op: "thermostat_power", node, on: true }));
    // A target-temperature DROPDOWN (4–28 °C, shown in the user's C/F/K scale) + Set — ONE command per
    // change. The old − / + sent a SET per degree, so dragging from 18→25 spammed the TRV with 7 SETs in
    // a row and hung it; picking a target + Set is a single command. The option VALUE stays Celsius (the
    // device/backend speak Celsius); only the label is converted.
    const sel = document.createElement("select");
    sel.style.marginRight = "0.3rem";
    for (let c = THERMOSTAT_MIN_C; c <= THERMOSTAT_MAX_C; c++) {
      const o = document.createElement("option");
      o.value = String(c);
      o.textContent = fmtTemp(c);
      sel.appendChild(o);
    }
    const current = info.setpoint;
    const start = current !== null && Number.isFinite(current)
      ? Math.min(THERMOSTAT_MAX_C, Math.max(THERMOSTAT_MIN_C, Math.round(current)))
      : 21;
    sel.value = String(start);
    cell.appendChild(sel);
    addButton(t("ctl.set"), () => ({ op: "thermostat", node, celsius: Number(sel.value) }));
  }

  if (buttons.length > 0) cell.appendChild(status);
}
