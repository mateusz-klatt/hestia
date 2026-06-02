import { describe, expect, it } from "vitest";

import { device } from "../fixtures";
import { battFmt, battLow, fmtHumidity, fmtTemp, stateStr } from "./format";

describe("fmtTemp", () => {
  it("formats one decimal with a degree sign", () => {
    expect(fmtTemp(21)).toBe("21.0°");
    expect(fmtTemp(25.2)).toBe("25.2°");
  });
  it("renders an em dash for null", () => {
    expect(fmtTemp(null)).toBe("—");
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
  it("thermostat composes temperature, setpoint and power", () => {
    expect(
      stateStr(device({ type: "thermostat", temperature: 21, setpoint: 22, thermostat_on: true })),
    ).toBe("21° → 22° ⏻on");
    expect(stateStr(device({ type: "thermostat", setpoint: 22 }))).toBe("→ 22°");
    expect(stateStr(device({ type: "thermostat", thermostat_on: false }))).toBe("off");
    expect(stateStr(device({ type: "thermostat" }))).toBe("—");
  });
  it("light renders on/off, keeping 'false' visible", () => {
    expect(stateStr(device({ type: "light", switch: true }))).toBe("on");
    expect(stateStr(device({ type: "light", switch: false }))).toBe("off");
    expect(stateStr(device({ type: "light" }))).toBe("—");
  });
  it("light single endpoint flattens, multi-gang stays blank (sub-rows carry state)", () => {
    expect(stateStr(device({ type: "light", endpoints: { "1": true } }))).toBe("on");
    expect(stateStr(device({ type: "light", endpoints: { "1": false } }))).toBe("off");
    expect(stateStr(device({ type: "light", endpoints: { "1": true, "2": false } }))).toBe("");
  });
  it("plug joins the present fields, keeping 'off' visible", () => {
    expect(
      stateStr(device({ type: "plug", switch: true, power_w: 12, energy_kwh: 3.5, voltage_v: 230 })),
    ).toBe("on · 12 W · 3.5 kWh · 230 V");
    expect(stateStr(device({ type: "plug", switch: false }))).toBe("off");
    expect(stateStr(device({ type: "plug" }))).toBe("—");
  });
  it("door shows its string, em dash when unseen", () => {
    expect(stateStr(device({ type: "door", door: "open" }))).toBe("open");
    expect(stateStr(device({ type: "door" }))).toBe("—");
  });
  it("renders an em dash for a stateless / unknown type", () => {
    expect(stateStr(device({ type: "motion" }))).toBe("—");
    expect(stateStr(device({ type: "unknown" }))).toBe("—");
  });
});
