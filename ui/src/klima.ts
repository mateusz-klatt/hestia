import type { ControlResult, IrButton, Klima, KlimaState } from "./api/types";
import { t } from "./i18n";

/** Transmits a saved Flipper signal (`{file, button}`); normalised result (never rejects). */
export type PostIr = (file: string, button: string) => Promise<ControlResult>;

/** Pictogram per A/C mode; an unknown mode falls back to the snowflake (the panel's identity). */
const MODE_ICON: Record<string, string> = { cool: "❄️", heat: "🔥", auto: "🔄", dry: "💨", fan: "💨" };

/**
 * The current A/C state as a compact, language-neutral pictogram line for the
 * klima panel: `❄️ 22°` (cooling), `🔥 24°` (heating), `🔄 20°` (auto), `⏻` (off),
 * or just `❄️` when never commanded (the panel's identity, no known state).
 * Pure — no DOM, no i18n (icons + a temperature number) — so it stays trivially testable.
 */
export function formatKlimaState(state: KlimaState | null): string {
  if (state === null) return "❄️"; // unknown → A/C identity only
  if (!state.power) return "⏻"; // off (the backend retains mode/temp for future "resume" UX)
  const icon = state.mode !== null ? (MODE_ICON[state.mode] ?? "❄️") : "❄️";
  return state.temp !== null ? `${icon} ${String(state.temp)}°` : icon;
}

/** Update every built klima panel's status pictogram (both the rooms + admin panels). */
export function applyKlimaState(boxes: Iterable<HTMLElement>, state: KlimaState | null): void {
  const text = formatKlimaState(state);
  for (const box of boxes) {
    const el = box.querySelector<HTMLElement>(".klima-state");
    if (el !== null) el.textContent = text;
  }
}

/**
 * Build the configured one-tap IR buttons into `box` (static config → built
 * once, guarded by `dataset.built`). Each button transmits its saved signal and
 * shows a shared ✓/✗ status. No buttons when none are configured.
 */
export function renderIrButtons(box: HTMLElement, buttons: IrButton[], postIr: PostIr): void {
  if (buttons.length === 0 || box.dataset.built !== undefined) return;
  box.dataset.built = "1";
  const status = document.createElement("span");
  status.className = "status";
  status.style.marginLeft = "0.5rem";
  // One transmit at a time: a shared lock disables ALL IR buttons for the
  // round-trip (the Flipper transmit is single-owner anyway), so quick taps on
  // different buttons can't overlap or race the shared status span.
  const btns: HTMLButtonElement[] = [];
  let busy = false;
  const send = async (b: IrButton): Promise<void> => {
    if (busy) return;
    busy = true;
    for (const x of btns) x.disabled = true;
    status.textContent = "…";
    try {
      const res = await postIr(b.file, b.button);
      status.textContent = res.ok ? `✓ ${b.label}` : `✗ ${res.error ?? t("ctl.failed")}`;
    } finally {
      busy = false;
      for (const x of btns) x.disabled = false;
    }
  };
  for (const b of buttons) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = b.label;
    btn.style.marginRight = "0.4rem";
    btn.addEventListener("click", () => {
      void send(b);
    });
    btns.push(btn);
    box.appendChild(btn);
  }
  box.appendChild(status);
}

/**
 * Build the LG A/C panel into `box` from the parsed klima signal map (built
 * once). Data-driven, no hard-coded modes: a [mode ▾][temp ▾] + "Ustaw" sends
 * the idempotent power-on signal `on_<mode>_<temp>`, and "Wyłącz" sends `off`.
 * A shared in-flight lock disables every button for the round-trip. Nothing is
 * built when the map is empty / has no usable signals.
 */
export function renderKlima(box: HTMLElement, klima: Klima, postIr: PostIr): void {
  if (box.dataset.built !== undefined) return;
  const programs = klima.power_on ?? {};
  const file = klima.file;
  const modeNames = Object.keys(programs).sort();
  const canOff = (klima.presets ?? []).includes("off");
  if (file === undefined || (modeNames.length === 0 && !canOff)) return;
  box.dataset.built = "1";

  // Leading status pictogram — updated live by `applyKlimaState` (replaces the old static ❄️ label;
  // defaults to ❄️ = the A/C identity until the first command / discovery snapshot fills it in).
  const stateLabel = document.createElement("span");
  stateLabel.className = "klima-state";
  stateLabel.textContent = formatKlimaState(null);
  box.appendChild(stateLabel);
  const status = document.createElement("span");
  status.className = "status";
  status.style.marginLeft = "0.5rem";

  const buttons: HTMLButtonElement[] = [];
  let busy = false;
  const send = async (button: string, tag: string): Promise<void> => {
    if (busy) return;
    busy = true;
    for (const b of buttons) b.disabled = true;
    status.textContent = "…";
    try {
      const res = await postIr(file, button);
      status.textContent = res.ok ? `✓ ${tag}` : `✗ ${res.error ?? t("ctl.failed")}`;
    } catch {
      status.textContent = t("ctl.error");
    } finally {
      busy = false;
      for (const b of buttons) b.disabled = false;
    }
  };

  if (modeNames.length > 0) {
    const mode = document.createElement("select");
    mode.style.marginRight = "0.3rem";
    for (const m of modeNames) {
      const o = document.createElement("option");
      o.value = m;
      o.textContent = m;
      mode.appendChild(o);
    }
    const temp = document.createElement("select");
    temp.style.marginRight = "0.3rem";
    const fillTemps = (): void => {
      temp.replaceChildren();
      for (const t of programs[mode.value] ?? []) {
        const o = document.createElement("option");
        o.value = String(t);
        o.textContent = `${String(t)}°`;
        temp.appendChild(o);
      }
    };
    mode.addEventListener("change", fillTemps);
    fillTemps();
    const set = document.createElement("button");
    set.type = "button";
    set.textContent = "✓"; // compact icon so the klima row fits one line; label via title/aria
    set.title = t("ctl.set");
    set.setAttribute("aria-label", t("ctl.set"));
    set.style.marginRight = "0.4rem";
    set.addEventListener("click", () => {
      if (mode.value === "" || temp.value === "") return;
      void send(`on_${mode.value}_${temp.value}`, `${mode.value} ${temp.value}°`);
    });
    buttons.push(set);
    box.append(mode, temp, set);
  }

  if (canOff) {
    const off = document.createElement("button");
    off.type = "button";
    off.textContent = "⏻"; // power-off icon (compact); label via title/aria
    off.title = t("ctl.turnOff");
    off.setAttribute("aria-label", t("ctl.turnOff"));
    off.style.marginRight = "0.4rem";
    off.addEventListener("click", () => {
      void send("off", t("ctl.turnOff"));
    });
    buttons.push(off);
    box.appendChild(off);
  }

  box.appendChild(status);
}
