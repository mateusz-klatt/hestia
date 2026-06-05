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
  // The thermostat setpoint dropdown is the USER's input. A background state report / 45 s refresh
  // re-renders the row, but must NOT yank the user's pick back to the (possibly stale) State setpoint
  // — "I set 18 °C and it jumps to 28". Preserve the current selection across re-renders; only the
  // first build seeds it from info.setpoint.
  const priorSetpoint = info.type === "thermostat"
    ? cell.querySelector<HTMLSelectElement>("select")?.value
    : undefined;
  cell.replaceChildren();

  const buttons: HTMLButtonElement[] = [];
  const status = document.createElement("span");
  status.className = "status";
  let busy = false;

  const setDisabled = (disabled: boolean): void => {
    for (const b of buttons) b.disabled = disabled;
  };

  // Sends one OR a sequence of ops under a single in-flight lock (a multi-command action like the
  // thermostat "Set" = power-on then setpoint sends both before re-enabling; it stops on the first error).
  const send = async (ops: ControlOp | ControlOp[], pending: string): Promise<void> => {
    if (busy) return;
    busy = true;
    setDisabled(true);
    status.textContent = pending.length > 0 ? `… ${pending}` : "…";
    status.className = "status";
    try {
      let failed: string | undefined;
      for (const op of Array.isArray(ops) ? ops : [ops]) {
        const res = await postControl(op);
        if (!res.ok) {
          failed = res.error ?? t("ctl.failed");
          break;
        }
      }
      status.textContent = failed === undefined ? t("ctl.sent") : `✗ ${failed}`;
      status.className = failed === undefined ? "status" : "status err";
    } catch {
      status.textContent = t("ctl.error");
      status.className = "status err";
    } finally {
      busy = false;
      setDisabled(false);
    }
  };

  const addButton = (
    label: string,
    op: () => ControlOp | ControlOp[],
    pending: () => string = () => "",
    title?: string,
  ): void => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = label;
    btn.style.marginRight = "0.3rem";
    if (title !== undefined) {
      btn.title = title; // icon-only buttons (thermostat ✓/⏻) get an accessible name, like klima
      btn.setAttribute("aria-label", title);
    }
    btn.addEventListener("click", () => {
      void send(op(), pending());
    });
    buttons.push(btn);
    cell.appendChild(btn);
  };

  const addLevelSelect = (): void => {
    const sel = document.createElement("select");
    sel.setAttribute("aria-label", t("ctl.brightness")); // icon-only panel → name the dropdown for screen readers
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
    // Mirrors the klima panel: a target-temperature DROPDOWN (4–28 °C, shown in the user's C/F/K scale) +
    // ✓ Set + ⏻ Off. A setpoint SET while the thermostat is OFF doesn't take, so Set is power-ON then the
    // setpoint (two commands, in that order — like the Keemple app) so the user just picks a temperature
    // and taps Set; there is no separate On. The option VALUE stays Celsius (device/backend speak Celsius);
    // only the label is C/F/K-converted. One Set command per change — no per-degree − / + SET burst that
    // spammed and hung the TRV.
    // Partial failure (power-on ok, setpoint fails) deliberately leaves the device ON at its old setpoint
    // and surfaces the ✗ error — we do NOT auto-revert to OFF: that would countermand the user's "on"
    // intent and add a third TRV command. The optimistic echo only fires per successful inject, so the UI
    // stays consistent with the device; the user just re-taps Set.
    const sel = document.createElement("select");
    sel.setAttribute("aria-label", t("user.temperature")); // name the setpoint dropdown for screen readers
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
    sel.value = priorSetpoint ?? String(start); // keep the user's pick across re-renders; seed once from setpoint
    cell.appendChild(sel);
    addButton("✓", () => [
      { op: "thermostat_power", node, on: true }, // turn on, THEN set — a setpoint alone is ignored while off
      { op: "thermostat", node, celsius: Number(sel.value) },
    ], () => "", t("ctl.set"));
    // Off = drop to the frost-protection minimum FIRST, then power off. Sending 4 °C before the off means
    // that even if the power-off frame is lost, the TRV is left at its lowest setpoint (won't heat) rather
    // than stuck at the old high target — "on the safe side". We always supply a temperature on Set anyway,
    // so losing the prior setpoint here costs nothing. (Keemple's own off keeps the setpoint; this is safer.)
    addButton("⏻", () => [
      { op: "thermostat", node, celsius: THERMOSTAT_MIN_C },
      { op: "thermostat_power", node, on: false },
    ], () => "", t("ctl.turnOff"));
  }

  if (buttons.length > 0) cell.appendChild(status);
}
