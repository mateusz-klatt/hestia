import type { DeviceInfo, SceneOp, SceneResult } from "./api/types";
import { coverPercent } from "./controls";
import { t } from "./i18n";

export type PostScene = (op: SceneOp, value?: number) => Promise<SceneResult | null>;

/** A summary of the blinds the whole-home slider drives: the mean position over the INCLUDED blinds (the
 *  ones not opted out of "all"), how many are included, and how many of those have reported a level yet. */
export interface BlindAverage {
  pct: number | null; // mean position 0–100 % of the included blinds that have reported; null if none have
  included: number; // how many blinds the "all" sweep targets (not opted out)
  reported: number; // how many of those have a known position
}

/** Mean position of the blinds the whole-home sweep targets. `excluded` = whole-node opt-outs (from the
 *  admin whole-home config); when it can't be read (a non-admin) it's empty, so the average degrades to
 *  ALL blinds — the SWEEP still honours the opt-out server-side, only this displayed average widens. */
export function blindAverage(
  devices: Record<string, DeviceInfo>,
  excluded: ReadonlySet<number>,
): BlindAverage {
  let sum = 0;
  let reported = 0;
  let included = 0;
  for (const [node, info] of Object.entries(devices)) {
    if (info.type !== "blind" || excluded.has(Number(node))) continue;
    included += 1;
    if (info.level !== null && Number.isFinite(info.level)) {
      sum += coverPercent(info.level);
      reported += 1;
    }
  }
  return { pct: reported > 0 ? Math.round(sum / reported) : null, included, reported };
}

/** A handle on a rendered whole-home panel so the live layer can re-sync the blind slider to the current
 *  average as reports arrive (while the panel is open). */
export interface SceneControls {
  syncBlindAverage(): void;
}

const controls = new WeakMap<HTMLElement, SceneControls>();

const BUTTONS = [
  { icon: "🌙", label: "scene.lightsOff", op: "lights_off" },
  { icon: "💡", label: "scene.lightsOn", op: "lights_on" },
  { icon: "☀", label: "scene.blindsUp", op: "blinds_up" },
  { icon: "🌑", label: "scene.blindsDown", op: "blinds_down" },
] as const satisfies readonly { icon: string; label: Parameters<typeof t>[0]; op: SceneOp }[];

/**
 * Build the whole-home scene controls into a container (the "Cały dom" virtual-room detail body): the four
 * all-on/off buttons plus a blind-position slider that drives every (non-excluded) blind to a chosen %.
 * The slider REFLECTS the live state — seeded from + re-synced to the mean position of the targeted blinds
 * (`getBlindAvg`), so it reads like the per-blind slider in aggregate. Idempotent per container (returns the
 * same handle on a repeat call). The detail view supplies its own heading, so none is rendered here.
 */
export function renderSceneControls(
  container: HTMLElement,
  postScene: PostScene,
  getBlindAvg?: () => BlindAverage,
): SceneControls {
  const existing = controls.get(container);
  if (existing) return existing;

  const buttons: HTMLButtonElement[] = [];
  const status = document.createElement("span");
  status.className = "status";
  let busy = false;
  // The blind slider is disabled when EITHER a send is in flight (the shared lock) OR there are no blinds
  // to drive (included === 0). Kept as its own flag so releasing the in-flight lock doesn't re-enable a
  // slider that has nothing to control.
  let noBlinds = false;

  const setDisabled = (disabled: boolean): void => {
    for (const button of buttons) button.disabled = disabled;
  };

  // A blind-position range shares the buttons' in-flight lock, so a drag-release while a scene POST is
  // pending can't fire a second overlapping sweep.
  const sliders: HTMLInputElement[] = [];

  const setDisabledAll = (disabled: boolean): void => {
    setDisabled(disabled);
    for (const s of sliders) s.disabled = disabled || noBlinds; // keep a no-blinds slider disabled
  };

  const send = async (op: SceneOp, value?: number): Promise<void> => {
    if (busy) return;
    busy = true;
    setDisabledAll(true);
    status.textContent = "…";
    status.className = "status";
    try {
      const result = await postScene(op, value);
      if (result === null || !result.ok) {
        status.textContent = `✗ ${t("ctl.failed")}`;
        status.className = "status err";
      } else {
        status.textContent = `✓ ${String(result.sent)}/${String(result.total)}`;
      }
    } catch {
      status.textContent = t("ctl.error");
      status.className = "status err";
    } finally {
      busy = false;
      setDisabledAll(false);
    }
  };

  // Forward-declared so the Raise-all / Lower-all buttons can snap the slider to its extreme on press
  // (instant feedback); the slider is built after the buttons (DOM order) but wired before any click fires.
  let setBlindPct: (pct: number) => void = () => undefined;

  for (const item of BUTTONS) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `${item.icon} ${t(item.label)}`;
    button.addEventListener("click", () => {
      if (busy) return; // in-flight lock: drop the whole click (snap + send) while a sweep is pending
      if (item.op === "blinds_up") setBlindPct(100);
      else if (item.op === "blinds_down") setBlindPct(0);
      void send(item.op);
    });
    buttons.push(button);
    container.appendChild(button);
  }

  // A whole-home blind-position slider on its own line under the up/down buttons: drag to any %, commit
  // ON RELEASE (the `change` event — NOT `input`, so dragging doesn't spam /api/scene) as one `blinds_set`
  // sweep that drives EVERY non-excluded blind to that position (e.g. all to half open). It mirrors the
  // per-blind slider in controls.ts and REFLECTS the live aggregate: seeded from + re-synced to the mean
  // position of the targeted blinds, except while the user is dragging it. The Raise/Lower buttons stay.
  const sliderRow = document.createElement("div");
  sliderRow.className = "scene-slider";
  const slider = document.createElement("input");
  slider.type = "range";
  slider.min = "0";
  slider.max = "100";
  slider.step = "1";
  slider.setAttribute("aria-label", t("ctl.position"));
  slider.style.marginRight = "0.3rem";
  const readout = document.createElement("span");
  readout.className = "slider-val";
  // Paint the slider from a fresh average: the mean position when known; "—" when no included blind has
  // reported yet (never a fake 50 %); disabled when there are NO included blinds (nothing to drive).
  const paintAverage = (stats: BlindAverage | undefined): void => {
    noBlinds = stats !== undefined && stats.included === 0;
    slider.disabled = noBlinds || busy; // also stays disabled while a send holds the lock
    if (noBlinds) {
      readout.textContent = "▣ —"; // nothing to drive
      return;
    }
    if (stats === undefined || stats.pct === null) {
      slider.value = "50"; // neutral start — still usable to set a position before any report lands
      readout.textContent = "▣ —";
      return;
    }
    slider.value = String(stats.pct);
    readout.textContent = `▣ ${String(stats.pct)}%`;
  };
  setBlindPct = (pct: number): void => {
    slider.value = String(pct);
    readout.textContent = `▣ ${String(pct)}%`;
  };
  paintAverage(getBlindAvg?.()); // seed from the current average
  slider.addEventListener("input", () => {
    readout.textContent = `▣ ${slider.value}%`;
  });
  slider.addEventListener("change", () => {
    if (busy) return; // a sweep is in flight; ignore this release
    void send("blinds_set", Number(slider.value));
  });
  sliders.push(slider);
  sliderRow.appendChild(slider);
  sliderRow.appendChild(readout);
  container.appendChild(sliderRow);

  container.appendChild(status);

  const handle: SceneControls = {
    // Re-sync the slider to the current average — unless the user is dragging it (focus), the panel is
    // detached (navigated away — a fresh handle paints on re-entry), or a send is in flight. The `busy`
    // skip is deliberate and needs no replay: repainting mid-sweep would flash the PRE-report average over
    // the value the user just committed, and the blinds' own reports re-sync the slider once the sweep lands.
    syncBlindAverage(): void {
      if (busy || !slider.isConnected || document.activeElement === slider) return;
      paintAverage(getBlindAvg?.());
    },
  };
  controls.set(container, handle);
  return handle;
}
