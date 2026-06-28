import type { SceneOp, SceneResult } from "./api/types";
import { t } from "./i18n";

export type PostScene = (op: SceneOp, value?: number) => Promise<SceneResult | null>;

const controls = new WeakMap<HTMLElement, true>();

const BUTTONS = [
  { icon: "🌙", label: "scene.lightsOff", op: "lights_off" },
  { icon: "💡", label: "scene.lightsOn", op: "lights_on" },
  { icon: "☀", label: "scene.blindsUp", op: "blinds_up" },
  { icon: "🌑", label: "scene.blindsDown", op: "blinds_down" },
] as const satisfies readonly { icon: string; label: Parameters<typeof t>[0]; op: SceneOp }[];

/** Build the whole-home scene buttons into a container (the "Cały dom" virtual-room detail body).
 *  The detail view supplies its own heading, so no title is rendered here. */
export function renderSceneControls(container: HTMLElement, postScene: PostScene): void {
  if (controls.has(container)) return;
  controls.set(container, true);

  const buttons: HTMLButtonElement[] = [];
  const status = document.createElement("span");
  status.className = "status";
  let busy = false;

  const setDisabled = (disabled: boolean): void => {
    for (const button of buttons) button.disabled = disabled;
  };

  // A blind-position range shares the buttons' in-flight lock, so a drag-release while a scene POST is
  // pending can't fire a second overlapping sweep.
  const sliders: HTMLInputElement[] = [];

  const setDisabledAll = (disabled: boolean): void => {
    setDisabled(disabled);
    for (const s of sliders) s.disabled = disabled;
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

  for (const item of BUTTONS) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `${item.icon} ${t(item.label)}`;
    button.addEventListener("click", () => {
      void send(item.op);
    });
    buttons.push(button);
    container.appendChild(button);
  }

  // A whole-home blind-position slider on its own line under the up/down buttons: drag to any %, commit
  // ON RELEASE (the `change` event — NOT `input`, so dragging doesn't spam /api/scene) as one `blinds_set`
  // sweep that drives EVERY non-excluded blind to that position (e.g. all to half open). It mirrors the
  // per-blind slider in controls.ts; seeded at 50 % (the panel renders once, so no freeze-across-rebuild
  // dance is needed). The Raise/Lower buttons remain for the two extremes.
  const sliderRow = document.createElement("div");
  sliderRow.className = "scene-slider";
  const slider = document.createElement("input");
  slider.type = "range";
  slider.min = "0";
  slider.max = "100";
  slider.step = "1";
  slider.value = "50";
  slider.setAttribute("aria-label", t("ctl.position"));
  slider.style.marginRight = "0.3rem";
  const readout = document.createElement("span");
  readout.className = "slider-val";
  readout.textContent = "▣ 50%";
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
}
