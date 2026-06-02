import { afterEach, describe, expect, it, vi } from "vitest";

import { apiBase, fetchDiscovery, postIr, postName } from "./client";

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

describe("postName", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns ok + status + body on a 2xx response", async () => {
    vi.stubGlobal("fetch", () =>
      Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve("") }),
    );
    expect(await postName({ node: 7, name: "x" })).toEqual({ ok: true, status: 200, body: "" });
  });

  it("returns the error body verbatim on a non-2xx response", async () => {
    vi.stubGlobal("fetch", () =>
      Promise.resolve({ ok: false, status: 400, text: () => Promise.resolve("invalid name") }),
    );
    expect(await postName({ node: 7 })).toEqual({ ok: false, status: 400, body: "invalid name" });
  });

  it("maps a rejected fetch to {ok:false, status:0} without throwing", async () => {
    vi.stubGlobal("fetch", () => Promise.reject(new Error("offline")));
    await expect(postName({ node: 7 })).resolves.toEqual({ ok: false, status: 0, body: "błąd" });
  });
});

describe("postIr", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns ok on a 2xx {ok:true}", async () => {
    vi.stubGlobal("fetch", () =>
      Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true }) }),
    );
    expect(await postIr("/ext/infrared/klima.ir", "off")).toEqual({ ok: true });
  });

  it("returns the error on a failed transmit", async () => {
    vi.stubGlobal("fetch", () =>
      Promise.resolve({
        ok: false,
        status: 503,
        json: () => Promise.resolve({ ok: false, error: "flipper IR is disabled" }),
      }),
    );
    expect(await postIr("/x.ir", "off")).toEqual({ ok: false, error: "flipper IR is disabled" });
  });
});
