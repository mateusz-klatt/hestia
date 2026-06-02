// Hand-written mirror of the `GET /api/discovery` JSON contract (see
// `_discovery` / `_discovery_entry` / `globals_snapshot` / `_summary` in the
// Python backend). The frontend is a generic HTTP client: it knows the shape
// of the JSON, not the protocol that produces it.

/** Per-endpoint on/off state of a multi-gang switch — `{ "1": true, "2": false }`. */
export type DeviceEndpoints = Record<string, boolean>;

/**
 * One device as merged from the classifier + the user registry.
 *
 * Live-state fields (`level`..`endpoints`) are ALWAYS present, `null` when the
 * node hasn't been seen for that field — a stable contract where `0` / `false`
 * are never lost. Registry labels (`name` / `room` / `endpoint_names`) are
 * optional: only present once the operator has set them.
 */
export interface DeviceInfo {
  power: string | null;
  type: string;
  confidence: string;
  battery: number | null;
  // live state — always present, null when unseen
  level: number | null;
  switch: boolean | null;
  door: string | null;
  setpoint: number | null;
  thermostat_on: boolean | null;
  temperature: number | null;
  power_w: number | null;
  energy_kwh: number | null;
  voltage_v: number | null;
  endpoints: DeviceEndpoints | null;
  // registry labels — optional (absent until set)
  name?: string;
  room?: string;
  endpoint_names?: Record<string, string>;
}

/** Node-less global automation fields (°C), `null` when their poller is off. */
export interface Globals {
  crib_temp: number | null;
  outdoor_temp: number | null;
}

/** Aggregate counters for the header. */
export interface Summary {
  total: number;
  confirmed: number;
  unknown: number;
}

/**
 * The full `/api/discovery` payload. PR-2 renders `devices` / `summary` /
 * `globals`; the remaining fields are part of the contract but typed loosely
 * (`unknown`) until the PRs that render them tighten the shape.
 */
export interface Discovery {
  devices: Record<string, DeviceInfo>;
  summary: Summary;
  globals: Globals;
  ir_buttons: unknown;
  klima: unknown;
  rule_vocab: unknown;
  mode: string;
  target_mode: string;
  env_override: string | null;
}
