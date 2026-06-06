// The UI's view of the hestia HTTP / SSE contract.
//
// The contract is OWNED BY THE BACKEND: `hestia/api_contract.py` (Pydantic v2) generates
// `docs/api/openapi.json`, from which `src/api/openapi.ts` is generated (`npm run gen:types`).
// This module is a thin, STABLE ALIAS LAYER over those generated component schemas — so the
// names the app imports never change, while the *shapes* track the contract: a backend change
// that regenerates `openapi.ts` surfaces here (and across every consumer) at `tsc` time. The
// handful of purely client-side shapes that have no wire schema (fetch-result wrappers) are
// defined directly below.
import type { components } from "./openapi";

type Schemas = components["schemas"];

// ---- generated-contract aliases (shapes owned by hestia/api_contract.py) ----

/** Per-endpoint on/off of a multi-gang switch (`{ "1": true, "2": false }`). Inlined in the
 *  contract (not a named schema), so aliased locally. */
export type DeviceEndpoints = Record<string, boolean>;

export type DeviceInfo = Schemas["DeviceInfo"];
export type Globals = Schemas["Globals"];
export type Summary = Schemas["Summary"];
export type IrButton = Schemas["IrButton"];
export type Klima = Schemas["Klima"];
export type KlimaState = Schemas["KlimaState"];

/** A rule trigger — the discriminated union the backend inlines into a rule's `trigger` field. */
export type Trigger =
  | Schemas["TriggerScene"]
  | Schemas["TriggerState"]
  | Schemas["TriggerTime"]
  | Schemas["TriggerSun"]
  | Schemas["TriggerPresence"]
  | Schemas["TriggerCron"];
export type RuleAction = Schemas["RuleAction"];
export type RuleVocab = Schemas["RuleVocab"];
export type Rule = Schemas["Rule"];

export type AuditEvent = Schemas["AuditEvent"];
export type Rf433Device = Schemas["Rf433Device"];
export type DbStats = Schemas["DbStats"];
export type UserSettings = Schemas["Settings"];
export type UserAccount = Schemas["UserRow"];
export type Discovery = Schemas["Discovery"];

// ---- Server-Sent Events (`GET /api/events`) -------------------------------
export type Scene = Schemas["Scene"];
export type ActivityEvent = Schemas["ActivityEvent"];
export type StateEvent = Schemas["StateEvent"];
export type GlobalsEvent = Schemas["GlobalsEvent"];
export type DiscoveryChangedEvent = Schemas["DiscoveryChangedEvent"];
export type KlimaEvent = Schemas["KlimaEvent"];
export type LiveEvent = Schemas["LiveEvent"];

// ---- Control / scenes / registry mutations --------------------------------
export type ControlOp = Schemas["ControlRequest"];
export type SceneOp = Schemas["SceneRequest"]["op"];
export type SceneResult = Schemas["SceneResult"];
export type NamePayload = Schemas["NameRequest"];

// ---- client-only shapes (no wire schema — these wrap fetch() results) ------

/** Result of a rule POST/DELETE: `ok` + HTTP status + the parsed body (`{ok,error,id}` or null). */
export interface RuleResult {
  ok: boolean;
  status: number;
  body: { ok?: boolean; error?: string; id?: string } | null;
}

/** Normalised result of a control POST: `ok`, plus an `error` to surface on failure. */
export interface ControlResult {
  ok: boolean;
  error?: string;
}

/** Result of a name POST: `ok` + the HTTP status + the response body (shown verbatim on failure). */
export interface NameResult {
  ok: boolean;
  status: number;
  body: string;
}
