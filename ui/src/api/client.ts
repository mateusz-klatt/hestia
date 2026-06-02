import type { Discovery } from "./types";

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
