import type { ControlOp, ControlResult, DeviceInfo } from "./api/types";
import { t } from "./i18n";
import { fmtTemp } from "./render/format";

/** The Keemple TRVs' setpoint range, in whole °C (the device reports/accepts integer Celsius). */
const THERMOSTAT_MIN_C = 4;
const THERMOSTAT_MAX_C = 28;

/**
 * The thermostat setpoint dropdown's value, per node, kept OUTSIDE the DOM so it survives the device
 * table's periodic FULL rebuild (every refresh replaces the rows with fresh cells). The dropdown is the
 * user's input: a device report / refresh updates only the "stan" text — it must NEVER move the
 * dropdown. We seed it from the live setpoint exactly ONCE (the first render of that node this session)
 * and FREEZE it there; after that only the user moves it (and reports never do), whether or not they've
 * touched it. A hard page reload re-inits the module (empty map) so it re-seeds from the then-current
 * setpoint.
 */
const thermostatPick = new Map<number, string>();

/**
 * Same freeze contract for the blind-position slider, in whole percent 0–100 (display units; the wire
 * `cover` value is 0–99). A blind reports its OWN position, so a device report / refresh updates only the
 * "stan" text — it must never yank the slider. Seed once from the live level, freeze, only the user moves it.
 */
const blindPick = new Map<number, string>();

/** Exposed for tests to reset the cross-render pick memory between cases. */
export function __resetThermostatPicks(): void {
  thermostatPick.clear();
}

/** Exposed for tests to reset the blind-slider pick memory between cases. */
export function __resetBlindPicks(): void {
  blindPick.clear();
}

/** Wire 0–99 cover value → display 0–100 %. */
function coverPercent(value: number): number {
  return Math.round((Math.min(99, Math.max(0, value)) / 99) * 100);
}

/** Display 0–100 % → wire 0–99 cover value. */
function coverValue(percent: number): number {
  return Math.round((Math.min(100, Math.max(0, percent)) / 100) * 99);
}

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
  // Range sliders that commit through `send` (blind) or feed it (thermostat) join the in-flight lock too,
  // so a second release while a POST is pending can't fire a dropped `send` and strand the frozen pick.
  const sliders: HTMLInputElement[] = [];
  const status = document.createElement("span");
  status.className = "status";
  let busy = false;

  const setDisabled = (disabled: boolean): void => {
    for (const b of buttons) b.disabled = disabled;
    for (const s of sliders) s.disabled = disabled;
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

  // A position slider for a blind: drag to any %, commit ON RELEASE (the `change` event — NOT `input`, so
  // dragging doesn't spam /api/control) as one absolute `cover` value 0–99. Seeded once from the live level
  // and FROZEN per node via `blindPick` (mirrors the thermostat): the table's 45 s full rebuild and device
  // reports update only the "stan" text, never the slider. The live position stays visible in that text; the
  // Raise/Lower buttons remain for the two extremes. A value readout tracks the drag.
  const addBlindSlider = (): void => {
    const slider = document.createElement("input");
    slider.type = "range";
    slider.min = "0";
    slider.max = "100";
    slider.step = "1";
    slider.setAttribute("aria-label", t("ctl.position"));
    slider.style.marginRight = "0.3rem";
    const level = info.level;
    const startPct = level !== null && Number.isFinite(level) ? coverPercent(level) : 50;
    let pick = blindPick.get(node);
    if (pick === undefined) {
      pick = String(startPct);
      blindPick.set(node, pick);
    }
    slider.value = pick;
    const readout = document.createElement("span");
    readout.className = "slider-val";
    readout.style.marginRight = "0.3rem";
    readout.textContent = `${pick}%`;
    slider.addEventListener("input", () => {
      readout.textContent = `${slider.value}%`;
    });
    slider.addEventListener("change", () => {
      if (busy) return; // a send is in flight; ignore this release so blindPick can't drift past what was sent
      blindPick.set(node, slider.value); // the user's release wins, kept across the table's full rebuilds
      void send({ op: "cover", node, value: coverValue(Number(slider.value)) }, "");
    });
    sliders.push(slider); // share the in-flight disable with the buttons
    cell.appendChild(slider);
    cell.appendChild(readout);
  };

  const endpointLabel = (ep: string): string => {
    const name = info.endpoint_names?.[ep]?.trim();
    return name !== undefined && name !== "" ? name : `#${ep}`;
  };

  if (info.endpoints !== null) {
    const endpoints = Object.keys(info.endpoints).sort((a, b) => Number(a) - Number(b));
    // Only prefix the channel name when this cell shows MORE THAN ONE channel (the room card renders all
    // gangs together, so the name disambiguates the 4 buttons). The engineer table renders one channel
    // per sub-row — already labelled — so a single-channel cell drops the redundant name: the On/Off pair
    // then fits on one line instead of wrapping under a long channel name.
    const prefix = endpoints.length > 1;
    for (const ep of endpoints) {
      const label = prefix ? `${endpointLabel(ep)} ` : "";
      const endpoint = Number(ep);
      addButton(`${label}${t("ctl.on")}`, () => ({ op: "switch", node, endpoint, on: true }));
      addButton(`${label}${t("ctl.off")}`, () => ({ op: "switch", node, endpoint, on: false }));
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
    addBlindSlider();
  } else if (info.type === "thermostat") {
    // Mirrors the klima panel: a target-temperature SLIDER (4–28 °C, whole degrees; the readout shows the
    // user's C/F/K scale) + ✓ Set + ⏻ Off. A setpoint SET while the thermostat is OFF doesn't take, so Set is
    // power-ON then the setpoint (two commands, in that order — like the Keemple app) so the user just drags to
    // a temperature and taps Set; there is no separate On. The slider VALUE stays Celsius (device/backend speak
    // Celsius); only the readout label is C/F/K-converted. One Set command per tap — no per-degree − / + SET
    // burst that spammed and hung the TRV; the slider does not send while dragging.
    // Partial failure (power-on ok, setpoint fails) deliberately leaves the device ON at its old setpoint
    // and surfaces the ✗ error — we do NOT auto-revert to OFF: that would countermand the user's "on"
    // intent and add a third TRV command. The optimistic echo only fires per successful inject, so the UI
    // stays consistent with the device; the user just re-taps Set.
    const slider = document.createElement("input");
    slider.type = "range";
    slider.min = String(THERMOSTAT_MIN_C);
    slider.max = String(THERMOSTAT_MAX_C);
    slider.step = "1";
    slider.setAttribute("aria-label", t("user.temperature")); // name the setpoint slider for screen readers
    slider.style.marginRight = "0.3rem";
    const current = info.setpoint;
    const start = current !== null && Number.isFinite(current)
      ? Math.min(THERMOSTAT_MAX_C, Math.max(THERMOSTAT_MIN_C, Math.round(current)))
      : 21;
    // Seed from the live setpoint exactly ONCE per node, then FREEZE: a device report / 45 s refresh
    // updates the "stan" text but never moves the slider — touched or not. Only the user moves it.
    let pick = thermostatPick.get(node);
    if (pick === undefined) {
      pick = String(start);
      thermostatPick.set(node, pick);
    }
    slider.value = pick;
    const readout = document.createElement("span");
    readout.className = "slider-val";
    readout.style.marginRight = "0.3rem";
    readout.textContent = fmtTemp(Number(slider.value));
    slider.addEventListener("input", () => {
      readout.textContent = fmtTemp(Number(slider.value)); // live label while dragging — no API call
    });
    slider.addEventListener("change", () => {
      thermostatPick.set(node, slider.value); // the user's choice wins, kept across the table's full rebuilds
    });
    sliders.push(slider); // share the in-flight disable with the ✓/⏻ buttons
    cell.appendChild(slider);
    cell.appendChild(readout);
    addButton("✓", () => [
      { op: "thermostat_power", node, on: true }, // turn on, THEN set — a setpoint alone is ignored while off
      { op: "thermostat", node, celsius: Number(slider.value) },
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
