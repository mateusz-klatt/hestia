import type { ControlOp, ControlResult, DeviceInfo } from "./api/types";
import { t } from "./i18n";

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

  // Clamp a setpoint nudge to the thermostat's 4–28 °C range (the Keemple TRVs' actual limits;
  // default 21 when unseen / non-finite).
  const clampSetpoint = (delta: number): number => {
    const current = info.setpoint ?? 21;
    const base = Number.isFinite(current) ? current : 21;
    return Math.min(28, Math.max(4, base + delta));
  };

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
    // 1 °C step: the device REPORTS its setpoint as a whole integer °C, so a half-degree nudge
    // wouldn't survive the next report (21.5 comes back as 21/22). Whole degrees match it.
    addButton(
      "−",
      () => ({ op: "thermostat", node, celsius: clampSetpoint(-1) }),
      () => `${clampSetpoint(-1).toFixed(1)}°`,
    );
    addButton(
      "+",
      () => ({ op: "thermostat", node, celsius: clampSetpoint(1) }),
      () => `${clampSetpoint(1).toFixed(1)}°`,
    );
  }

  if (buttons.length > 0) cell.appendChild(status);
}
