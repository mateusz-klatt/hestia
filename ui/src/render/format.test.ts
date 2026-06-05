import { afterEach, describe, expect, it, vi } from "vitest";

import { device } from "../fixtures";
import { battFmt, battLow, fmtHumidity, fmtTemp, stateStr, thermostatNotResponding } from "./format";

describe("fmtTemp", () => {
  it("formats one decimal with a degree sign (Celsius default)", () => {
    expect(fmtTemp(21)).toBe("21.0°");
    expect(fmtTemp(25.2)).toBe("25.2°");
  });
  it("renders an em dash for null", () => {
    expect(fmtTemp(null)).toBe("—");
  });
});

describe("fmtTemp — temperature scale", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });
  function withScale(scale: string): void {
    const m = new Map<string, string>([["hestia.tempScale", scale]]);
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => m.get(k) ?? null,
      setItem: () => undefined,
      removeItem: () => undefined,
      clear: () => undefined,
      key: () => null,
      length: m.size,
    });
  }
  it("converts to Fahrenheit with an explicit unit", () => {
    withScale("F");
    expect(fmtTemp(0)).toBe("32.0°F");
    expect(fmtTemp(21)).toBe("69.8°F");
  });
  it("converts to Kelvin with an explicit unit", () => {
    withScale("K");
    expect(fmtTemp(26.85)).toBe("300.0 K"); // 26.85 + 273.15
  });
  it("falls back to Celsius for an unknown stored scale", () => {
    withScale("X");
    expect(fmtTemp(21)).toBe("21.0°");
  });
});

describe("fmtHumidity", () => {
  it("formats a whole percent, rounding", () => {
    expect(fmtHumidity(56)).toBe("56%");
    expect(fmtHumidity(44.6)).toBe("45%");
  });
  it("renders an em dash for null", () => {
    expect(fmtHumidity(null)).toBe("—");
  });
});

describe("battFmt / battLow", () => {
  it("formats a healthy percentage", () => {
    expect(battFmt(74)).toBe("74%");
  });
  it("renders mains (null) as an em dash", () => {
    expect(battFmt(null)).toBe("—");
  });
  it("renders the >100 sentinel as 'low' rather than a bogus percentage", () => {
    expect(battFmt(255)).toBe("low");
  });
  it("flags low for the sentinel and <20, but not null or a healthy level", () => {
    expect(battLow(255)).toBe(true);
    expect(battLow(10)).toBe(true);
    expect(battLow(50)).toBe(false);
    expect(battLow(null)).toBe(false);
  });
});

describe("stateStr", () => {
  it("blind shows the level, em dash when unseen", () => {
    expect(stateStr(device({ type: "blind", level: 40 }))).toBe("▣ 40%");
    expect(stateStr(device({ type: "blind", level: 0 }))).toBe("▣ 0%");
    expect(stateStr(device({ type: "blind" }))).toBe("—");
  });
  it("thermostat composes temperature, setpoint (user scale) and power (icon + word)", () => {
    expect(
      stateStr(device({ type: "thermostat", temperature: 21, setpoint: 22, thermostat_on: true })),
    ).toBe("21.0° → 22.0° 🟢 On"); // Celsius default via fmtTemp
    expect(stateStr(device({ type: "thermostat", setpoint: 22 }))).toBe("→ 22.0°");
    expect(stateStr(device({ type: "thermostat", thermostat_on: false }))).toBe("⚪ Off");
    expect(stateStr(device({ type: "thermostat" }))).toBe("—");
  });
  it("light renders on/off as icon + word, keeping 'false' visible", () => {
    expect(stateStr(device({ type: "light", switch: true }))).toBe("🟢 On");
    expect(stateStr(device({ type: "light", switch: false }))).toBe("⚪ Off");
    expect(stateStr(device({ type: "light" }))).toBe("—");
  });
  it("light single endpoint flattens, multi-gang stays blank (sub-rows carry state)", () => {
    expect(stateStr(device({ type: "light", endpoints: { "1": true } }))).toBe("🟢 On");
    expect(stateStr(device({ type: "light", endpoints: { "1": false } }))).toBe("⚪ Off");
    expect(stateStr(device({ type: "light", endpoints: { "1": true, "2": false } }))).toBe("");
  });
  it("plug joins the present fields, keeping 'off' visible", () => {
    expect(
      stateStr(device({ type: "plug", switch: true, power_w: 12, energy_kwh: 3.5, voltage_v: 230 })),
    ).toBe("🟢 On · 12 W · 3.5 kWh · 230 V");
    expect(stateStr(device({ type: "plug", switch: false }))).toBe("⚪ Off");
    expect(stateStr(device({ type: "plug" }))).toBe("—");
  });
  it("door shows an icon + localised word; unexpected value falls back to raw; em dash when unseen", () => {
    expect(stateStr(device({ type: "door", door: "open" }))).toBe("🔓 open");
    expect(stateStr(device({ type: "door", door: "closed" }))).toBe("🔒 closed");
    expect(stateStr(device({ type: "door", door: "tamper" }))).toBe("tamper");   // unexpected → raw
    expect(stateStr(device({ type: "door" }))).toBe("—");
  });
  it("motion (PIR) shows an icon + localised word; em dash until it has reported", () => {
    expect(stateStr(device({ type: "motion", motion: true }))).toBe("🏃 motion");
    expect(stateStr(device({ type: "motion", motion: false }))).toBe("🧍 no motion");
    expect(stateStr(device({ type: "motion" }))).toBe("—");   // null → unseen
  });
  it("renders an em dash for a stateless / unknown type", () => {
    expect(stateStr(device({ type: "smoke" }))).toBe("—");
    expect(stateStr(device({ type: "unknown" }))).toBe("—");
  });

  it("appends ⚠ to a thermostat that was commanded but never confirmed", () => {
    // stateStr uses real Date.now(), so anchor the command 120 s ago in REAL time (past the 25 s grace).
    const longAgo = Date.now() / 1000 - 120;
    const dead = device({ type: "thermostat", setpoint: 22, thermostat_on: true, thermostat_last_cmd: longAgo });
    expect(thermostatNotResponding(dead)).toBe(true);
    expect(stateStr(dead)).toContain("⚠"); // commanded, nothing heard since → not responding
    // a normally-tracking thermostat (never commanded) has no ⚠
    expect(stateStr(device({ type: "thermostat", setpoint: 22, thermostat_on: true }))).not.toContain("⚠");
  });
});

describe("thermostatNotResponding", () => {
  const now = 1_800_000_000_000;
  const t = (secAgo: number): number => now / 1000 - secAgo;
  it("is false when never commanded", () => {
    expect(thermostatNotResponding(device({ type: "thermostat", thermostat_last_cmd: null }), now)).toBe(false);
  });
  it("is false within the confirm grace window", () => {
    expect(thermostatNotResponding(device({ type: "thermostat", thermostat_last_cmd: t(5) }), now)).toBe(false);
  });
  it("is true when commanded, grace passed, and nothing heard since", () => {
    expect(thermostatNotResponding(device({ type: "thermostat", thermostat_last_cmd: t(60) }), now)).toBe(true);
    // last_seen BEFORE the command → still silent since
    const before = new Date((t(60) - 10) * 1000).toISOString();
    expect(thermostatNotResponding(
      device({ type: "thermostat", thermostat_last_cmd: t(60), last_seen: before }), now)).toBe(true);
  });
  it("is false once a report lands AFTER the command", () => {
    const after = new Date((t(60) + 5) * 1000).toISOString();
    expect(thermostatNotResponding(
      device({ type: "thermostat", thermostat_last_cmd: t(60), last_seen: after }), now)).toBe(false);
  });
});
