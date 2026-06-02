import type { DeviceInfo, Discovery } from "./api/types";

/** A fully-defaulted device (all live state null / unseen); override per test. */
export function device(overrides: Partial<DeviceInfo> = {}): DeviceInfo {
  return {
    power: null,
    type: "unknown",
    confidence: "unknown",
    battery: null,
    level: null,
    switch: null,
    door: null,
    setpoint: null,
    thermostat_on: null,
    temperature: null,
    power_w: null,
    energy_kwh: null,
    voltage_v: null,
    endpoints: null,
    ...overrides,
  };
}

/** A discovery payload wrapping the given devices, with sane defaults. */
export function discovery(
  devices: Record<string, DeviceInfo>,
  overrides: Partial<Discovery> = {},
): Discovery {
  return {
    devices,
    summary: { total: Object.keys(devices).length, confirmed: 0, unknown: 0 },
    globals: { crib_temp: null, outdoor_temp: null },
    ir_buttons: [],
    klima: null,
    rule_vocab: {},
    mode: "standalone",
    target_mode: "standalone",
    env_override: null,
    ...overrides,
  };
}
