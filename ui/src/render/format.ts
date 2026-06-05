import type { DeviceInfo } from "../api/types";
import { t } from "../i18n";
import { tempScale } from "../prefs";

/** A binary on/off live state as an icon + the localised word (🟢 On / ⚪ Off) — language-neutral
 *  glyph for the wife-friendly view, plus the translated word so it reads in the chosen language. */
export function onOff(on: boolean): string {
  return on ? `🟢 ${t("ctl.on")}` : `⚪ ${t("ctl.off")}`;
}

/**
 * A temperature (sensors report °C) in the user's chosen scale; `—` when null. Celsius keeps the
 * bare "°" (the default convention, so existing displays are unchanged); Fahrenheit / Kelvin show
 * an explicit unit since they're an opt-in.
 */
export function fmtTemp(value: number | null): string {
  if (value === null) return "—";
  switch (tempScale()) {
    case "F":
      return `${((value * 9) / 5 + 32).toFixed(1)}°F`;
    case "K":
      return `${(value + 273.15).toFixed(1)} K`;
    default:
      return `${value.toFixed(1)}°`;
  }
}

/** Relative humidity (%RH) → whole percent; `—` when null. */
export function fmtHumidity(value: number | null): string {
  return value === null ? "—" : `${String(Math.round(value))}%`;
}

/**
 * Battery level (%) for nodes that report one; `—` for mains (no report).
 * A value above 100 is the Z-Wave low-battery sentinel (e.g. 0xff) — show
 * "low" rather than a bogus "255%".
 */
export function battFmt(pct: number | null): string {
  if (pct === null) return "—";
  if (pct > 100) return "low";
  return `${String(pct)}%`;
}

/** True when a reported battery is low (<20 %) or the >100 sentinel. */
export function battLow(pct: number | null): boolean {
  return pct !== null && (pct > 100 || pct < 20);
}

/** Grace after a thermostat SET before its silence counts as "not responding" — must clear the
 *  confirm-debounce (~40 s, Keemple-aligned) + the GET poll round-trip + margin, else ⚠ would flash
 *  in the gap before the confirm poll lands. */
const THERMOSTAT_CONFIRM_GRACE_MS = 50_000;

/**
 * A thermostat is "not responding" when we sent it a command but no device frame has arrived SINCE
 * (the confirm poll went unanswered) and the grace window has passed. In confirm-only mode a healthy
 * idle TRV and a dead one are both silent, so time-alone can't tell them apart — this keys off
 * "commanded, but never heard back", which only a genuinely unreachable device sustains.
 */
export function thermostatNotResponding(info: DeviceInfo, now: number = Date.now()): boolean {
  const cmd = info.thermostat_last_cmd;
  if (cmd === null) return false; // never commanded → nothing to confirm
  const cmdMs = cmd * 1000;
  if (now - cmdMs < THERMOSTAT_CONFIRM_GRACE_MS) return false; // still inside the confirm window
  const seen = info.last_seen === null ? 0 : Date.parse(info.last_seen);
  return !(seen > cmdMs); // a report landed AFTER the command → it responded; otherwise it's silent
}

/**
 * Type-aware live-state ("stan") text. Uses `!== null` throughout so a blind at
 * 0 % or a switch that is `false` still renders — only an unseen field is `—`.
 */
export function stateStr(info: DeviceInfo): string {
  switch (info.type) {
    case "blind":
      return info.level === null ? "—" : `▣ ${String(info.level)}%`;
    case "thermostat": {
      // measured temp + setpoint in the user's scale (C/F/K) — the device/backend stay Celsius.
      let s = "";
      if (info.temperature !== null) s += fmtTemp(info.temperature);
      if (info.setpoint !== null) s += `${s.length > 0 ? " → " : "→ "}${fmtTemp(info.setpoint)}`;
      if (info.thermostat_on !== null) s += `${s.length > 0 ? " " : ""}${onOff(info.thermostat_on)}`;
      if (thermostatNotResponding(info)) s += `${s.length > 0 ? " " : ""}⚠`; // commanded but never confirmed
      return s.length > 0 ? s : "—";
    }
    case "light": {
      if (info.endpoints !== null) {
        const eps = Object.keys(info.endpoints);
        if (eps.length > 1) return ""; // multi-gang: each channel renders as its own sub-row
        const first = eps[0];
        if (first === undefined) return "—";
        return onOff(info.endpoints[first] === true);
      }
      return info.switch === null ? "—" : onOff(info.switch);
    }
    case "plug": {
      const parts: string[] = [];
      if (info.switch !== null) parts.push(onOff(info.switch)); // most important first
      if (info.power_w !== null) parts.push(`${String(info.power_w)} W`);
      if (info.energy_kwh !== null) parts.push(`${String(info.energy_kwh)} kWh`);
      if (info.voltage_v !== null) parts.push(`${String(info.voltage_v)} V`);
      return parts.length > 0 ? parts.join(" · ") : "—";
    }
    case "door":
      // icon + localised word; an unexpected value falls back to the raw string (textContent-safe).
      if (info.door === null) return "—";
      if (info.door === "open") return `🔓 ${t("state.open")}`;
      if (info.door === "closed") return `🔒 ${t("state.closed")}`;
      return info.door;
    case "motion":
      // PIR: icon + localised word, null until it has reported.
      if (info.motion === null) return "—";
      return info.motion ? `🏃 ${t("state.motion")}` : `🧍 ${t("state.noMotion")}`;
    default:
      // smoke / water / unknown — no numeric state yet
      return "—";
  }
}
