import type { ControlOp, ControlResult, DeviceInfo } from "./api/types";
import { t } from "./i18n";
import { fmtTemp } from "./render/format";

/** The Keemple TRVs' setpoint range, in whole °C (the device reports/accepts integer Celsius). */
const THERMOSTAT_MIN_C = 4;
const THERMOSTAT_MAX_C = 28;

/**
 * Per-controls-cell "sync" hook: re-applies the live device state onto this cell's slider (blind position
 * or thermostat setpoint) when a report arrives, so the slider REFLECTS the device instead of staying at
 * the user's last pick. Registered by `renderActions` per cell; invoked by `patchControls` from the live
 * patch path (the same path that updates the "stan" text). A cell without a slider registers nothing, so
 * `patchControls` is a no-op for it. Keyed by the cell element (WeakMap → GC'd when the row is rebuilt).
 * The hook itself guards against yanking the control mid-interaction (focus / an uncommitted edit).
 */
const syncByContainer = new WeakMap<HTMLElement, (info: DeviceInfo) => void>();

/** Re-sync a controls cell's slider from a fresh DeviceInfo (a live state delta) — so the slider tracks
 *  the device's reported position/setpoint. No-op if the cell has no slider; the registered hook itself
 *  skips while the user is dragging it (focus) or, for a thermostat, mid-edit (the two-step ✓ Set). */
export function patchControls(container: HTMLElement, info: DeviceInfo): void {
  syncByContainer.get(container)?.(info);
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
  syncByContainer.delete(cell); // drop any slider sync from a prior render of this reused cell

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
  // Resolves to whether every op succeeded, so a caller can react to the outcome (e.g. the thermostat
  // clears its "mid-edit" flag once the send settles, so the slider resumes tracking the device).
  const send = async (ops: ControlOp | ControlOp[], pending: string): Promise<boolean> => {
    if (busy) return false;
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
      return failed === undefined;
    } catch {
      status.textContent = t("ctl.error");
      status.className = "status err";
      return false;
    } finally {
      busy = false;
      setDisabled(false);
    }
  };

  // `onClick` runs synchronously on press (e.g. snap a slider to its extreme for instant feedback);
  // `onDone` runs after the send settles (e.g. clear the thermostat mid-edit flag).
  const addButton = (
    label: string,
    op: () => ControlOp | ControlOp[],
    pending: () => string = () => "",
    title?: string,
    onClick?: () => void,
    onDone?: () => void,
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
      if (busy) return; // in-flight lock: drop the whole click (snap + send + onDone) — `onDone` must signal
      onClick?.();      // a STARTED send settled (clearing thermostat `dirty`), never a send dropped as busy
      void send(op(), pending()).then(() => onDone?.());
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
  // dragging doesn't spam /api/control) as one absolute `cover` value 0–99. The slider REFLECTS the live
  // position: seeded from the reported level and re-synced on every device report (via `patchControls`),
  // EXCEPT while the user is actively dragging it (focus guard) so a report can't yank it mid-drag. The
  // Raise/Lower buttons snap it to the extreme on press for instant feedback. Returns a snap setter for them.
  const addBlindSlider = (): ((pct: number) => void) => {
    const slider = document.createElement("input");
    slider.type = "range";
    slider.min = "0";
    slider.max = "100";
    slider.step = "1";
    slider.setAttribute("aria-label", t("ctl.position"));
    slider.style.marginRight = "0.3rem";
    const readout = document.createElement("span");
    readout.className = "slider-val";
    readout.style.marginRight = "0.3rem";
    const setPct = (pct: number): void => {
      slider.value = String(pct);
      readout.textContent = `${String(pct)}%`;
    };
    const level = info.level;
    setPct(level !== null && Number.isFinite(level) ? coverPercent(level) : 50); // seed from the live level
    slider.addEventListener("input", () => {
      readout.textContent = `${slider.value}%`;
    });
    slider.addEventListener("change", () => {
      if (busy) return; // a send is in flight; ignore this release so we can't post past the in-flight value
      void send({ op: "cover", node, value: coverValue(Number(slider.value)) }, "");
    });
    // Track the device: a report moves the slider to the new position — unless the user is dragging it now.
    syncByContainer.set(cell, (fresh) => {
      if (document.activeElement === slider) return; // mid-drag — don't yank it
      const lvl = fresh.level;
      if (lvl !== null && Number.isFinite(lvl)) setPct(coverPercent(lvl));
    });
    sliders.push(slider); // share the in-flight disable with the buttons
    cell.appendChild(slider);
    cell.appendChild(readout);
    return setPct;
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
    // Forward-declared so Raise/Lower can snap the slider to its extreme on press for instant feedback;
    // the slider is appended AFTER the buttons (DOM order) but `snapBlind` is wired before any click fires.
    let snapBlind: (pct: number) => void = () => undefined;
    addButton(t("ctl.raise"), () => ({ op: "cover", node, value: 99 }), () => "", undefined, () => { snapBlind(100); });
    addButton(t("ctl.lower"), () => ({ op: "cover", node, value: 0 }), () => "", undefined, () => { snapBlind(0); });
    snapBlind = addBlindSlider();
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
    const clampC = (c: number): number => Math.min(THERMOSTAT_MAX_C, Math.max(THERMOSTAT_MIN_C, c));
    const slider = document.createElement("input");
    slider.type = "range";
    slider.min = String(THERMOSTAT_MIN_C);
    slider.max = String(THERMOSTAT_MAX_C);
    slider.step = "1";
    slider.setAttribute("aria-label", t("user.temperature")); // name the setpoint slider for screen readers
    slider.style.marginRight = "0.3rem";
    const readout = document.createElement("span");
    readout.className = "slider-val";
    readout.style.marginRight = "0.3rem";
    const setC = (c: number): void => {
      slider.value = String(c);
      readout.textContent = fmtTemp(c);
    };
    const current = info.setpoint;
    setC(current !== null && Number.isFinite(current) ? clampC(Math.round(current)) : 21); // seed from the live setpoint
    // The thermostat is a TWO-STEP commit (drag, THEN ✓ Set), so unlike the blind there's a window where a
    // dragged-but-unsent target would be clobbered by a device report. `dirty` protects it: the slider tracks
    // the reported setpoint by default, but once the user moves it we hold their value until the send settles
    // (✓ Set / ⏻ Off) — then tracking resumes and the next report wins. A full rebuild (new cell) re-seeds
    // from the then-current setpoint, so an abandoned drag self-corrects within the 45 s refresh.
    let dirty = false;
    slider.addEventListener("input", () => {
      dirty = true; // user is editing → don't let a report move it until they commit
      readout.textContent = fmtTemp(Number(slider.value)); // live label while dragging — no API call
    });
    syncByContainer.set(cell, (fresh) => {
      if (dirty || document.activeElement === slider) return; // mid-edit / dragging — don't yank it
      const sp = fresh.setpoint;
      if (sp !== null && Number.isFinite(sp)) setC(clampC(Math.round(sp)));
    });
    sliders.push(slider); // share the in-flight disable with the ✓/⏻ buttons
    cell.appendChild(slider);
    cell.appendChild(readout);
    // ✓ Set = power-ON then the setpoint (a setpoint alone is ignored while off — two commands, in that order,
    // like the Keemple app). Once the send settles, clear `dirty` so the slider resumes tracking the device
    // (the confirming report will read back the value we just set). One Set per tap — no per-degree SET burst
    // that spammed and hung the TRV; the slider does not send while dragging.
    addButton("✓", () => [
      { op: "thermostat_power", node, on: true },
      { op: "thermostat", node, celsius: Number(slider.value) },
    ], () => "", t("ctl.set"), undefined, () => { dirty = false; });
    // Off = drop to the frost-protection minimum FIRST, then power off. Sending 4 °C before the off means
    // that even if the power-off frame is lost, the TRV is left at its lowest setpoint (won't heat) rather
    // than stuck at the old high target — "on the safe side". The slider snaps to 4 °C on press (the value Off
    // sets) for instant feedback; `dirty` clears once it settles so the device's report (4 °C on success, the
    // old setpoint if the send failed) re-syncs the slider. Partial failure (power-on ok, setpoint fails on ✓)
    // deliberately leaves the device ON at its old setpoint and surfaces ✗ — we never auto-revert to OFF.
    addButton("⏻", () => [
      { op: "thermostat", node, celsius: THERMOSTAT_MIN_C },
      { op: "thermostat_power", node, on: false },
    ], () => "", t("ctl.turnOff"), () => { setC(THERMOSTAT_MIN_C); }, () => { dirty = false; });
  }

  if (buttons.length > 0) cell.appendChild(status);
}
