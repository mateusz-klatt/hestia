import { afterEach, describe, expect, it, vi } from "vitest";

import { apiBase, fetchDiscovery } from "./client";

describe("apiBase", () => {
  it("resolves the API root one level above the /ui/ page", () => {
    expect(apiBase("http://host:8927/ui/").href).toBe("http://host:8927/api/");
  });
  it("preserves a reverse-proxy subpath", () => {
    expect(apiBase("https://host/hestia/ui/").href).toBe("https://host/hestia/api/");
  });
});

describe("fetchDiscovery", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns the parsed payload on a 2xx response", async () => {
    vi.stubGlobal("fetch", () =>
      Promise.resolve({ ok: true, json: () => Promise.resolve({ devices: {} }) }),
    );
    expect(await fetchDiscovery()).toEqual({ devices: {} });
  });

  it("returns null on a non-2xx response", async () => {
    vi.stubGlobal("fetch", () =>
      Promise.resolve({ ok: false, json: () => Promise.resolve({}) }),
    );
    expect(await fetchDiscovery()).toBeNull();
  });

  it("returns null when fetch rejects (offline / network error)", async () => {
    vi.stubGlobal("fetch", () => Promise.reject(new Error("offline")));
    expect(await fetchDiscovery()).toBeNull();
  });

  it("returns null when the response body is not valid JSON", async () => {
    vi.stubGlobal("fetch", () =>
      Promise.resolve({ ok: true, json: () => Promise.reject(new Error("bad json")) }),
    );
    expect(await fetchDiscovery()).toBeNull();
  });
});
