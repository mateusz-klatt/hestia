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

/** GET `/api/discovery`; `null` on a non-2xx response (mirrors the legacy UI). */
export async function fetchDiscovery(): Promise<Discovery | null> {
  const response = await fetch(new URL("discovery", apiBase(document.baseURI)));
  return response.ok ? ((await response.json()) as Discovery) : null;
}
