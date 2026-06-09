import type {
  AuditEvent,
  ControlOp,
  ControlResult,
  DbStats,
  Discovery,
  NamePayload,
  NameResult,
  Rf433Device,
  Rule,
  RuleResult,
  SceneOp,
  SceneResult,
  UserAccount,
  UserSettings,
} from "./types";

/**
 * The API root, derived from the page URL so it works at any mount point.
 *
 * The JSON API lives at `<prefix>/api/`. The app may be served at the root
 * (`hestia.klatt.ie/` or `host/hestia/`) or — as a retired alias — at `…/ui/`.
 * So resolve relative to the page's directory, dropping a trailing `ui/`
 * segment: correct for a bare host root (`/api/`), a reverse-proxy subpath
 * (`/hestia/api/`), AND the legacy `…/ui/` mount (`…/api/`). A fixed `../api/`
 * would break at the root — it drops the proxy prefix. Pure (takes the page
 * href) so it is unit-testable.
 */
export function apiBase(pageHref: string): URL {
  const dir = new URL(".", pageHref); // the page's directory (drops any filename)
  return dir.pathname.endsWith("/ui/") ? new URL("../api/", dir) : new URL("api/", dir);
}

/** Resolve an API path (e.g. "discovery", "events") against the page-derived base. */
export function apiUrl(path: string): URL {
  return new URL(path, apiBase(document.baseURI));
}

/**
 * GET `/api/discovery`; `null` on ANY load failure — a non-2xx response, a
 * rejected fetch (offline / network error) or an invalid-JSON body. Callers
 * treat `null` as "show the failed-load status"; never letting this reject
 * keeps `void refresh()` from becoming an unhandled rejection and lets Refresh
 * recover once connectivity returns.
 */
export async function fetchDiscovery(): Promise<Discovery | null> {
  try {
    const response = await fetch(apiUrl("discovery"));
    return response.ok ? ((await response.json()) as Discovery) : null;
  } catch {
    return null;
  }
}

async function readJsonBody(response: Response): Promise<{ ok?: boolean; error?: string }> {
  try {
    return (await response.json()) as { ok?: boolean; error?: string };
  } catch {
    return {}; // non-JSON / empty body
  }
}

/**
 * POST `/api/control` with one allowlisted device op. Never rejects: a
 * non-2xx, a `{ok:false}` body, a malformed body or a network error all map to
 * `{ ok:false, error }` so the caller can surface a status without try/catch.
 */
export async function postControl(op: ControlOp): Promise<ControlResult> {
  try {
    const response = await fetch(apiUrl("control"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(op),
    });
    const body = await readJsonBody(response);
    if (response.ok && body.ok === true) return { ok: true };
    return { ok: false, error: body.error ?? `error ${String(response.status)}` };
  } catch {
    return { ok: false, error: "błąd" };
  }
}

/** POST `/api/ir` to transmit a saved Flipper signal (`{file, button}`); normalised like postControl. */
export async function postIr(file: string, button: string): Promise<ControlResult> {
  try {
    const response = await fetch(apiUrl("ir"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file, button }),
    });
    const body = await readJsonBody(response);
    if (response.ok && body.ok === true) return { ok: true };
    return { ok: false, error: body.error ?? `error ${String(response.status)}` };
  } catch {
    return { ok: false, error: "błąd" };
  }
}

/**
 * POST `/api/name` to set a node's label/room, confirm its type, or label a
 * multi-gang endpoint. Returns the raw response body so a failure can be shown
 * verbatim; never rejects (network error → `{ok:false, status:0}`). A success
 * makes the server publish `discovery_changed`, so the SSE stream re-syncs the
 * row — no manual refresh needed here.
 */
export async function postName(payload: NamePayload): Promise<NameResult> {
  try {
    const response = await fetch(apiUrl("name"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    return { ok: response.ok, status: response.status, body: await response.text() };
  } catch {
    return { ok: false, status: 0, body: "błąd" };
  }
}

/** GET `/api/automations`; the rule list, or `null` on any load failure. */
export async function fetchAutomations(): Promise<Rule[] | null> {
  try {
    const response = await fetch(apiUrl("automations"));
    if (!response.ok) return null;
    const data = (await response.json()) as { automations?: Rule[] };
    return data.automations ?? [];
  } catch {
    return null;
  }
}

/** GET `/api/audit`; the audit event list, or `null` on any load failure. */
export async function fetchAudit(): Promise<AuditEvent[] | null> {
  try {
    const response = await fetch(apiUrl("audit"));
    if (!response.ok) return null;
    const data = (await response.json()) as { events?: AuditEvent[] };
    return data.events ?? [];
  } catch {
    return null;
  }
}

/** GET `/api/rf433`; discovered 433 MHz devices, or `null` on any load failure. */
export async function fetchRf433(): Promise<Rf433Device[] | null> {
  try {
    const response = await fetch(apiUrl("rf433"));
    if (!response.ok) return null;
    const data = (await response.json()) as { devices?: Rf433Device[] };
    return data.devices ?? [];
  } catch {
    return null;
  }
}

/** GET `/api/db/stats`; SQLite file size + table counts, or `null` on any load failure. */
export async function fetchDbStats(): Promise<DbStats | null> {
  try {
    const response = await fetch(apiUrl("db/stats"));
    return response.ok ? ((await response.json()) as DbStats) : null;
  } catch {
    return null;
  }
}

/** GET `/api/settings`; the server-persisted user settings, or `null` on any load failure. */
export async function fetchSettings(): Promise<UserSettings | null> {
  try {
    const response = await fetch(apiUrl("settings"));
    return response.ok ? ((await response.json()) as UserSettings) : null;
  } catch {
    return null;
  }
}

/** POST `/api/settings` to best-effort sync local-first user settings to the server. */
export async function saveSettings(settings: Partial<UserSettings>): Promise<boolean> {
  try {
    const response = await fetch(apiUrl("settings"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settings),
    });
    return response.ok;
  } catch {
    return false;
  }
}

/** GET `/api/rooms/icons`; the shared room→emoji map, or `null` on any load failure. */
export async function fetchRoomIcons(): Promise<Record<string, string> | null> {
  try {
    const response = await fetch(apiUrl("rooms/icons"));
    return response.ok ? ((await response.json()) as Record<string, string>) : null;
  } catch {
    return null;
  }
}

/** POST `/api/rooms/icons` to set or clear one shared room emoji. */
export async function saveRoomIcon(room: string, icon: string): Promise<boolean> {
  try {
    const response = await fetch(apiUrl("rooms/icons"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ room, icon }),
    });
    return response.ok;
  } catch {
    return false;
  }
}

/** GET `/api/whole-home`; the node ids opted out of the house-wide "all" sweeps, or `null` on failure. */
export async function fetchWholeHome(): Promise<number[] | null> {
  try {
    const response = await fetch(apiUrl("whole-home"));
    if (!response.ok) return null;
    const data = (await response.json()) as { excluded_nodes?: number[] };
    return data.excluded_nodes ?? [];
  } catch {
    return null;
  }
}

/** POST `/api/whole-home` to opt one device in (`exclude=false`) / out (`true`) of the "all" sweeps. */
export async function setWholeHomeExclude(node: number, exclude: boolean): Promise<boolean> {
  try {
    const response = await fetch(apiUrl("whole-home"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node, exclude }),
    });
    return response.ok;
  } catch {
    return false;
  }
}

/** POST `/api/scene` to run a house-wide scene; `null` on any load/send failure. */
export async function postScene(op: SceneOp): Promise<SceneResult | null> {
  try {
    const response = await fetch(apiUrl("scene"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ op }),
    });
    return response.ok ? ((await response.json()) as SceneResult) : null;
  } catch {
    return null;
  }
}

async function postRuleJson(path: string, payload: unknown): Promise<RuleResult> {
  try {
    const response = await fetch(apiUrl(path), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    let body: RuleResult["body"] = null;
    try {
      body = (await response.json()) as RuleResult["body"];
    } catch {
      /* empty / non-JSON body (e.g. 503/504) */
    }
    return { ok: response.ok, status: response.status, body };
  } catch {
    return { ok: false, status: 0, body: null };
  }
}

/** POST `/api/automations` to save a rule (server-side `Rule.from_dict` validates). */
export function postRule(payload: unknown): Promise<RuleResult> {
  return postRuleJson("automations", payload);
}

/** POST `/api/automations/delete` to remove a rule by id. */
export function deleteRule(id: string): Promise<RuleResult> {
  return postRuleJson("automations/delete", { id });
}

export interface WhoAmI {
  user: string | null; // the logged-in username, or null when auth is disabled
  role: string | null; // the RBAC role (admin|operator|viewer), or null when auth is disabled
}

/**
 * GET `/api/whoami`: `{user, role}` on 200 (both null when auth is off), or `null` on 401 / any failure.
 * The app uses `null` as "not logged in → show the login form"; a null role means "auth off → full access".
 */
export async function whoami(): Promise<WhoAmI | null> {
  try {
    const response = await fetch(apiUrl("whoami"));
    return response.ok ? ((await response.json()) as WhoAmI) : null;
  } catch {
    return null;
  }
}

/** POST `/api/login`; `true` on success (the response sets the session cookie). Never rejects. */
export async function login(user: string, password: string): Promise<boolean> {
  try {
    const response = await fetch(apiUrl("login"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user, password }),
    });
    return response.ok;
  } catch {
    return false;
  }
}

/** POST `/api/logout` (clears the session cookie). Best-effort — the caller reloads regardless. */
export async function logout(): Promise<void> {
  try {
    await fetch(apiUrl("logout"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
  } catch {
    return;
  }
}

// ---- user management (#PR-D) ---------------------------------------------

/** The outcome of a user-management mutation: success, or a server-supplied error string (English) we
 *  surface only as a fallback — the UI validates the common cases client-side with localized text. */
export interface MutationResult {
  ok: boolean;
  error: string | null;
}

async function postUser(path: string, payload: unknown): Promise<MutationResult> {
  try {
    const response = await fetch(apiUrl(path), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (response.ok) return { ok: true, error: null };
    let error: string | null = null;
    try {
      const body = (await response.json()) as { error?: unknown };
      if (typeof body.error === "string") error = body.error;
    } catch {
      /* non-JSON error body */
    }
    return { ok: false, error };
  } catch {
    return { ok: false, error: null };
  }
}

/** POST `/api/me/password` — the signed-in user changes their OWN password (server verifies current). */
export function changeOwnPassword(current: string, newPassword: string): Promise<MutationResult> {
  return postUser("me/password", { current, new: newPassword });
}

/** GET `/api/users` (admin) — every account's metadata (no hashes), or `null` on 401 / any failure. */
export async function fetchUsers(): Promise<UserAccount[] | null> {
  try {
    const response = await fetch(apiUrl("users"));
    if (!response.ok) return null;
    const body = (await response.json()) as { users?: unknown };
    return Array.isArray(body.users) ? (body.users as UserAccount[]) : null;
  } catch {
    return null;
  }
}

/** POST `/api/users` (admin) — create a new account. */
export function addUser(username: string, password: string, role: string): Promise<MutationResult> {
  return postUser("users", { username, password, role });
}

/** POST `/api/users/role` (admin) — change another user's role. */
export function setUserRole(username: string, role: string): Promise<MutationResult> {
  return postUser("users/role", { username, role });
}

/** POST `/api/users/disabled` (admin) — enable/disable another account. */
export function setUserDisabled(username: string, disabled: boolean): Promise<MutationResult> {
  return postUser("users/disabled", { username, disabled });
}

/** POST `/api/users/reset-password` (admin) — set a new password for another user. */
export function resetUserPassword(username: string, newPassword: string): Promise<MutationResult> {
  return postUser("users/reset-password", { username, new: newPassword });
}
