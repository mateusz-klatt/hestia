import type { DeviceInfo } from "../api/types";

/** Global temperature (°C) → one decimal + degree sign; `—` when null. */
export function fmtTemp(value: number | null): string {
  return value === null ? "—" : `${value.toFixed(1)}°`;
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

/**
 * Type-aware live-state ("stan") text. Uses `!== null` throughout so a blind at
 * 0 % or a switch that is `false` still renders — only an unseen field is `—`.
 */
export function stateStr(info: DeviceInfo): string {
  switch (info.type) {
    case "blind":
      return info.level === null ? "—" : `▣ ${String(info.level)}%`;
    case "thermostat": {
      let s = "";
      if (info.temperature !== null) s += `${String(info.temperature)}°`;
      if (info.setpoint !== null) s += `${s.length > 0 ? " → " : "→ "}${String(info.setpoint)}°`;
      if (info.thermostat_on !== null) s += `${s.length > 0 ? " " : ""}${info.thermostat_on ? "⏻on" : "off"}`;
      return s.length > 0 ? s : "—";
    }
    case "light": {
      if (info.endpoints !== null) {
        const eps = Object.keys(info.endpoints);
        if (eps.length > 1) return ""; // multi-gang: each channel renders as its own sub-row
        const first = eps[0];
        if (first === undefined) return "—";
        return info.endpoints[first] === true ? "on" : "off";
      }
      return info.switch === null ? "—" : info.switch ? "on" : "off";
    }
    case "plug": {
      const parts: string[] = [];
      if (info.switch !== null) parts.push(info.switch ? "on" : "off"); // most important first
      if (info.power_w !== null) parts.push(`${String(info.power_w)} W`);
      if (info.energy_kwh !== null) parts.push(`${String(info.energy_kwh)} kWh`);
      if (info.voltage_v !== null) parts.push(`${String(info.voltage_v)} V`);
      return parts.length > 0 ? parts.join(" · ") : "—";
    }
    case "door":
      // string-origin, but rendered via textContent → no escaping needed
      return info.door === null ? "—" : info.door;
    default:
      // motion / smoke / water / unknown — no numeric state yet
      return "—";
  }
}
