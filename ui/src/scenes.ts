import type { SceneOp, SceneResult } from "./api/types";
import { t } from "./i18n";

export type PostScene = (op: SceneOp) => Promise<SceneResult | null>;

const controls = new WeakMap<HTMLElement, true>();

const BUTTONS = [
  { icon: "🌙", label: "scene.lightsOff", op: "lights_off" },
  { icon: "💡", label: "scene.lightsOn", op: "lights_on" },
  { icon: "☀", label: "scene.blindsUp", op: "blinds_up" },
  { icon: "🌑", label: "scene.blindsDown", op: "blinds_down" },
] as const satisfies readonly { icon: string; label: Parameters<typeof t>[0]; op: SceneOp }[];

/** Build the whole-home scene buttons once into their persistent rooms-view panel. */
export function renderSceneControls(container: HTMLElement, postScene: PostScene): void {
  if (controls.has(container)) return;
  controls.set(container, true);

  const title = document.createElement("h3");
  title.textContent = t("scene.title");
  container.appendChild(title);

  const buttons: HTMLButtonElement[] = [];
  const status = document.createElement("span");
  status.className = "status";
  let busy = false;

  const setDisabled = (disabled: boolean): void => {
    for (const button of buttons) button.disabled = disabled;
  };

  const send = async (op: SceneOp): Promise<void> => {
    if (busy) return;
    busy = true;
    setDisabled(true);
    status.textContent = "…";
    status.className = "status";
    try {
      const result = await postScene(op);
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
      setDisabled(false);
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
  container.appendChild(status);
}
