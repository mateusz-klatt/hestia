import type { DeviceInfo, Discovery, RuleVocab } from "./api/types";

/**
 * A representative rule grammar mirroring the backend `rule_vocab()`. Includes
 * both GLOBAL fields (`crib_temp` / `outdoor_temp` → node-less) and per-node
 * fields so the predicate editor's node-visibility logic can be exercised.
 */
export function ruleVocab(overrides: Partial<RuleVocab> = {}): RuleVocab {
  return {
    trigger_types: ["scene", "state", "time", "sun", "presence", "cron"],
    state_fields: {
      crib_temp: true,
      outdoor_temp: true,
      door: false,
      level: false,
      switch: false,
      temperature: false,
    },
    cmp_ops: ["!=", "<", "<=", "==", ">", ">="],
    frame_action_ops: ["cover", "level", "switch", "thermostat", "thermostat_power"],
    modes: ["proxy", "standalone"],
    sun_events: ["sunrise", "sunset"],
    presence_events: ["arrive", "leave"],
    ...overrides,
  };
}

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
    klima: {},
    rule_vocab: ruleVocab(),
    mode: "standalone",
    target_mode: "standalone",
    env_override: null,
    ...overrides,
  };
}
