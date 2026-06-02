import RULE_VOCAB from "./rule_vocab.snapshot.json";
import type { DeviceInfo, Discovery, RuleVocab } from "./api/types";

/**
 * The real rule grammar, a VERBATIM capture of the backend `rule_vocab()`
 * (committed as `rule_vocab.snapshot.json`). Regenerate with:
 *   python3 -c "import json,hestia.automations as a; print(json.dumps(a.rule_vocab(), indent=2))"
 *
 * Sourcing the fixture from a real capture (rather than hand-transcribing it)
 * keeps the tests honest: `cmp_ops` are the backend's word-tokens
 * (`eq/ge/gt/le/lt/ne`), NOT symbolic operators, and `state_fields` carries
 * both GLOBAL fields (`crib_temp` / `outdoor_temp` → node-less) and per-node
 * fields so the predicate editor's node-visibility logic is exercised against
 * the grammar the live server actually validates.
 */
export function ruleVocab(overrides: Partial<RuleVocab> = {}): RuleVocab {
  return { ...(RULE_VOCAB as RuleVocab), ...overrides };
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
