import type { ControlOp, ControlResult, Discovery, NamePayload, NameResult } from "./types";

/**
 * The API root for a UI served at `<prefix>/ui/`.
 *
 * The JSON API lives at `<prefix>/api/`, so resolving one level up from the
 * page makes every fetch work both when hestia serves the UI directly
 * (`http://host:8927/ui/`) and behind a reverse-proxy subpath
 * (`https://host/hestia/ui/`). Pure (takes the page href) so it is unit-testable.
 */
export function apiBase(pageHref: string): URL {
  return new URL("../api/", pageHref);
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
