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
  motion: boolean | null; // PIR: true = motion detected, false = idle
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

/** Node-less global fields, `null` when their poller is off. */
export interface Globals {
  crib_temp: number | null;
  outdoor_temp: number | null;
  /** %RH companion to outdoor_temp from the local-433 feeder; null when off or absent. Display-only. */
  outdoor_humidity: number | null;
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
/** A configured one-tap IR button (HESTIA_IR_BUTTONS) → transmits a saved Flipper signal. */
export interface IrButton {
  label: string;
  file: string;
  button: string;
}

/**
 * The LG A/C control map parsed from the Flipper klima.ir signal names. Empty
 * (`{}`) when no klima.ir is present, so every field is optional. `power_on`
 * maps each mode to its sorted temps; the idempotent power-on signal is
 * `on_<mode>_<temp>`. `presets` carries `off` (and any non-temp signal).
 */
export interface Klima {
  file?: string;
  modes?: Record<string, number[]>;
  power_on?: Record<string, number[]>;
  presets?: string[];
}

/**
 * The optimistic A/C state — the last one-way IR command we sent (IR has no
 * feedback, so the command IS the state). `null` until the A/C has ever been
 * commanded; `off` retains the last `mode`/`temp` for display context.
 */
export interface KlimaState {
  power: boolean;
  mode: string | null;
  temp: number | null;
}

// ---- Automations (`GET/POST /api/automations`, `POST /api/automations/delete`) ----
// Rules are authored as JSON and validated server-side by Rule.from_dict; the
// UI lists them, toggles `enabled`, deletes, and edits raw JSON. The trigger
// union is just enough to summarise a rule in the list.

export type Trigger =
  | { type: "scene"; node: number; scene_id: number }
  | { type: "state"; node?: number; field: string; op: string; value: unknown }
  | { type: "time"; at: string; days?: number[] | null }
  | { type: "sun"; event: string; offset_min?: number; days?: number[] | null }
  | { type: "presence"; mac: string; event: string }
  | { type: "cron"; expr: string };

export interface RuleAction {
  op: string;
  [field: string]: unknown;
}

/**
 * The rule grammar the guided form builds its dropdowns from (the `rule_vocab`
 * field of `GET /api/discovery`). The backend derives it from its own
 * validation constants (see `rule_vocab()`), so the form cannot drift from
 * `Rule.from_dict`. `state_fields` maps each comparable field → whether it is
 * GLOBAL (node-less → the form omits the per-node input for it).
 */
export interface RuleVocab {
  trigger_types: string[];
  state_fields: Record<string, boolean>;
  cmp_ops: string[];
  frame_action_ops: string[];
  modes: string[];
  sun_events: string[];
  presence_events: string[];
}

export interface Rule {
  id: string;
  enabled: boolean;
  modes?: string[];
  debounce?: number;
  trigger: Trigger;
  conditions: unknown[];
  actions: RuleAction[];
}

/** Result of a rule POST/DELETE: `ok` + status + the parsed body (`{ok,error,id}` or null). */
export interface RuleResult {
  ok: boolean;
  status: number;
  body: { ok?: boolean; error?: string; id?: string } | null;
}

// ---- Audit log (`GET /api/audit`) ----------------------------------------

export interface AuditEvent {
  id: number;
  ts: number;
  actor: string;
  action: string;
  target: string | null;
  detail: string | null;
  result: string | null;
}

// ---- 433 MHz device discovery (`GET /api/rf433`) -------------------------
export interface Rf433Device {
  key: string; // model + id + channel, whichever the packet carried
  count: number;
  first_seen: number;
  last_seen: number;
  fields: Record<string, string | number | boolean>; // last decoded packet's scalar fields
}

// ---- Database stats (`GET /api/db/stats`) --------------------------------

export interface DbStats {
  file_bytes: number;
  tables: Record<string, number>;
}

// ---- User settings (`GET/POST /api/settings`) ----------------------------

export interface UserSettings {
  locale: string | null;
  temp_scale: string | null;
  theme: string | null;
}

export interface Discovery {
  devices: Record<string, DeviceInfo>;
  summary: Summary;
  globals: Globals;
  ir_buttons: IrButton[];
  klima: Klima;
  klima_state: KlimaState | null; // optimistic A/C state (last IR command), null until ever commanded
  rule_vocab: RuleVocab; // dropdown grammar for the guided rule form
  mode: string;
  target_mode: string;
  env_override: string | null;
}

// ---- Server-Sent Events (`GET /api/events`) -------------------------------
// The backend pushes one of these on every decoded frame (see
// `_publish_proxy_events` / the globals poller in hestia/proxy.py).

/** A function-button scene press — transient, rendered as a brief badge. */
export interface Scene {
  id: number;
}

/** Every event flashes the row (heatmap); a button press rides a `scene`. */
export interface ActivityEvent {
  type: "activity";
  node: number;
  ts: number;
  scene?: Scene;
}

/** Live value change(s) for one node — a cheap "stan" cell patch. */
export interface StateEvent {
  type: "state";
  node: number;
  fields: Partial<DeviceInfo>;
}

/** A node-less global field change (crib_temp / outdoor_temp). */
export interface GlobalsEvent {
  type: "globals";
  fields: Partial<Globals>;
}

/** A node's discovery identity changed → the client refetches the snapshot. */
export interface DiscoveryChangedEvent {
  type: "discovery_changed";
}

/** The optimistic A/C state changed (a klima IR command was transmitted). */
export interface KlimaEvent {
  type: "klima";
  klima: KlimaState;
}

export type LiveEvent =
  | ActivityEvent
  | StateEvent
  | GlobalsEvent
  | DiscoveryChangedEvent
  | KlimaEvent;

// ---- Control ops (`POST /api/control`) ------------------------------------
// The allowlisted device commands the dashboard can send (see the Keemple
// command builders behind `_control_device_command` in hestia/proxy.py).

export type ControlOp =
  | { op: "switch"; node: number; on: boolean; endpoint?: number }
  | { op: "level"; node: number; value: number }
  | { op: "cover"; node: number; value: number }
  | { op: "thermostat"; node: number; celsius: number }
  | { op: "thermostat_power"; node: number; on: boolean };

/** Normalised result of a control POST: `ok`, plus an `error` to surface on failure. */
export interface ControlResult {
  ok: boolean;
  error?: string;
}

// ---- House-wide scenes (`POST /api/scene`) -------------------------------

export type SceneOp = "lights_off" | "lights_on" | "blinds_down" | "blinds_up";

export interface SceneResult {
  ok: boolean;
  sent: number;
  total: number;
}

// ---- Registry mutations (`POST /api/name`) --------------------------------
// Set a node's user label / room, confirm its inferred type, or label one
// endpoint of a multi-gang switch (see `_control_name` in hestia/proxy.py).

export interface NamePayload {
  node: number;
  type?: string; // confirm the inferred type
  name?: string;
  room?: string;
  ep?: number; // endpoint label for a multi-gang channel
}

/** Result of a name POST: `ok` + the HTTP status + the response body (shown verbatim on failure). */
export interface NameResult {
  ok: boolean;
  status: number;
  body: string;
}
