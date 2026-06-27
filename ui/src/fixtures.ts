import RULE_VOCAB from "./rule_vocab.snapshot.json";
import type { DeviceInfo, Discovery, Globals, RuleVocab } from "./api/types";

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
    motion: null,
    setpoint: null,
    thermostat_on: null,
    temperature: null,
    power_w: null,
    energy_kwh: null,
    voltage_v: null,
    endpoints: null,
    thermostat_last_cmd: null,
    last_seen: null,
    ...overrides,
  };
}

/**
 * A discovery payload wrapping the given devices, with sane defaults. The `globals` override is
 * PARTIAL (merged onto the all-null default) so a test can set just the field(s) it cares about —
 * e.g. `{ globals: { outdoor_humidity: 56 } }` leaves crib/outdoor at null.
 */
export function discovery(
  devices: Record<string, DeviceInfo>,
  overrides: Partial<Omit<Discovery, "globals">> & { globals?: Partial<Globals> } = {},
): Discovery {
  const { globals, ...rest } = overrides;
  return {
    devices,
    summary: { total: Object.keys(devices).length, confirmed: 0, unknown: 0 },
    ir_buttons: [],
    klima: {},
    klima_state: null,
    rule_vocab: ruleVocab(),
    mode: "standalone",
    target_mode: "standalone",
    env_override: null,
    ...rest,
    globals: {
      crib_temp: null, crib_temp_ts: null, outdoor_temp: null, outdoor_humidity: null,
      outdoor_temp_ts: null, outdoor_battery_ok: null, ...globals,
    },
  };
}
