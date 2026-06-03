import { afterEach, describe, expect, it, vi } from "vitest";

import {
  apiBase,
  deleteRule,
  fetchAudit,
  fetchAutomations,
  fetchDbStats,
  fetchDiscovery,
  fetchSettings,
  login,
  logout,
  postIr,
  postName,
  postRule,
  saveSettings,
  whoami,
} from "./client";

describe("apiBase", () => {
  it("resolves the API root from a bare host root", () => {
    expect(apiBase("http://host:8927/").href).toBe("http://host:8927/api/");
  });
  it("resolves the API root from a dedicated subdomain root", () => {
    expect(apiBase("http://hestia.klatt.ie/").href).toBe("http://hestia.klatt.ie/api/");
  });
  it("preserves a reverse-proxy subpath at the root", () => {
    expect(apiBase("https://host/hestia/").href).toBe("https://host/hestia/api/");
  });
  it("resolves one level above the legacy /ui/ mount", () => {
    expect(apiBase("http://host:8927/ui/").href).toBe("http://host:8927/api/");
  });
  it("preserves a reverse-proxy subpath at the legacy /ui/ mount", () => {
    expect(apiBase("https://host/hestia/ui/").href).toBe("https://host/hestia/api/");
  });
  it("ignores a trailing index.html filename", () => {
    expect(apiBase("https://host/hestia/index.html").href).toBe("https://host/hestia/api/");
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

  it("maps a rejected fetch to {ok:false} without throwing (never-rejects contract)", async () => {
    vi.stubGlobal("fetch", () => Promise.reject(new Error("offline")));
    await expect(postIr("/x.ir", "off")).resolves.toEqual({ ok: false, error: "błąd" });
  });

  it("maps a non-JSON body to an error-<status> result", async () => {
    vi.stubGlobal("fetch", () =>
      Promise.resolve({ ok: false, status: 503, json: () => Promise.reject(new Error("bad json")) }),
    );
    await expect(postIr("/x.ir", "off")).resolves.toEqual({ ok: false, error: "error 503" });
  });
});

describe("fetchAutomations", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns the rule list on 2xx", async () => {
    vi.stubGlobal("fetch", () =>
      Promise.resolve({ ok: true, json: () => Promise.resolve({ automations: [{ id: "r1" }] }) }),
    );
    expect(await fetchAutomations()).toEqual([{ id: "r1" }]);
  });

  it("returns [] when the list key is absent", async () => {
    vi.stubGlobal("fetch", () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }));
    expect(await fetchAutomations()).toEqual([]);
  });

  it("returns null on a non-2xx or a rejected fetch", async () => {
    vi.stubGlobal("fetch", () => Promise.resolve({ ok: false, json: () => Promise.resolve({}) }));
    expect(await fetchAutomations()).toBeNull();
    vi.stubGlobal("fetch", () => Promise.reject(new Error("offline")));
    expect(await fetchAutomations()).toBeNull();
  });
});

describe("fetchAudit", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns the event list on 2xx", async () => {
    const event = {
      id: 1,
      ts: 1_800_000_000,
      actor: "system",
      action: "boot",
      target: null,
      detail: null,
      result: "ok",
    };
    vi.stubGlobal("fetch", () =>
      Promise.resolve({ ok: true, json: () => Promise.resolve({ events: [event] }) }),
    );
    expect(await fetchAudit()).toEqual([event]);
  });

  it("returns [] when the events key is absent", async () => {
    vi.stubGlobal("fetch", () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }));
    expect(await fetchAudit()).toEqual([]);
  });

  it("returns null on a non-2xx response", async () => {
    vi.stubGlobal("fetch", () => Promise.resolve({ ok: false, json: () => Promise.resolve({}) }));
    expect(await fetchAudit()).toBeNull();
  });

  it("returns null when fetch rejects", async () => {
    vi.stubGlobal("fetch", () => Promise.reject(new Error("offline")));
    expect(await fetchAudit()).toBeNull();
  });
});

describe("fetchDbStats", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns the parsed stats on 2xx", async () => {
    const stats = { file_bytes: 1536, tables: { nodes: 22, automations: 2 } };
    vi.stubGlobal("fetch", () => Promise.resolve({ ok: true, json: () => Promise.resolve(stats) }));
    expect(await fetchDbStats()).toEqual(stats);
  });

  it("returns null on a non-2xx response", async () => {
    vi.stubGlobal("fetch", () => Promise.resolve({ ok: false, json: () => Promise.resolve({}) }));
    expect(await fetchDbStats()).toBeNull();
  });

  it("returns null when fetch rejects or JSON parsing fails", async () => {
    vi.stubGlobal("fetch", () => Promise.reject(new Error("offline")));
    expect(await fetchDbStats()).toBeNull();

    vi.stubGlobal("fetch", () =>
      Promise.resolve({ ok: true, json: () => Promise.reject(new Error("bad json")) }),
    );
    expect(await fetchDbStats()).toBeNull();
  });
});

describe("fetchSettings / saveSettings", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("fetchSettings returns the parsed settings on 2xx", async () => {
    const settings = { locale: "pl", temp_scale: "F", theme: null };
    vi.stubGlobal("fetch", () => Promise.resolve({ ok: true, json: () => Promise.resolve(settings) }));
    expect(await fetchSettings()).toEqual(settings);
  });

  it("fetchSettings returns null on non-2xx, rejected fetch, or bad JSON", async () => {
    vi.stubGlobal("fetch", () => Promise.resolve({ ok: false, json: () => Promise.resolve({}) }));
    expect(await fetchSettings()).toBeNull();

    vi.stubGlobal("fetch", () => Promise.reject(new Error("offline")));
    expect(await fetchSettings()).toBeNull();

    vi.stubGlobal("fetch", () =>
      Promise.resolve({ ok: true, json: () => Promise.reject(new Error("bad json")) }),
    );
    expect(await fetchSettings()).toBeNull();
  });

  it("saveSettings POSTs JSON and returns response.ok", async () => {
    const seen: { url: string; init: RequestInit }[] = [];
    vi.stubGlobal("fetch", (url: URL, init: RequestInit) => {
      seen.push({ url: url.href, init });
      return Promise.resolve({ ok: true });
    });

    expect(await saveSettings({ locale: "pl" })).toBe(true);
    expect(seen[0]?.url).toContain("/api/settings");
    expect(seen[0]?.init.method).toBe("POST");
    expect(seen[0]?.init.headers).toEqual({ "Content-Type": "application/json" });
    expect(seen[0]?.init.body).toBe(JSON.stringify({ locale: "pl" }));
  });

  it("saveSettings returns false on non-2xx or rejected fetch", async () => {
    vi.stubGlobal("fetch", () => Promise.resolve({ ok: false }));
    expect(await saveSettings({ temp_scale: "K" })).toBe(false);

    vi.stubGlobal("fetch", () => Promise.reject(new Error("offline")));
    expect(await saveSettings({ temp_scale: "K" })).toBe(false);
  });
});

describe("postRule / deleteRule", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("postRule returns ok + the parsed body on success", async () => {
    vi.stubGlobal("fetch", () =>
      Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true, id: "r1" }) }),
    );
    expect(await postRule({ id: "r1" })).toEqual({ ok: true, status: 200, body: { ok: true, id: "r1" } });
  });

  it("postRule surfaces the parsed error body on failure", async () => {
    vi.stubGlobal("fetch", () =>
      Promise.resolve({ ok: false, status: 400, json: () => Promise.resolve({ ok: false, error: "bad" }) }),
    );
    expect(await postRule({})).toEqual({ ok: false, status: 400, body: { ok: false, error: "bad" } });
  });

  it("postRule tolerates an empty / non-JSON body (body: null)", async () => {
    vi.stubGlobal("fetch", () =>
      Promise.resolve({ ok: false, status: 503, json: () => Promise.reject(new Error("empty")) }),
    );
    expect(await postRule({})).toEqual({ ok: false, status: 503, body: null });
  });

  it("deleteRule POSTs the id", async () => {
    const seen: { body: unknown }[] = [];
    vi.stubGlobal("fetch", (_url: unknown, init: { body?: unknown } = {}) => {
      seen.push({ body: init.body });
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true }) });
    });
    const res = await deleteRule("r1");
    expect(res.ok).toBe(true);
    expect(seen[0]?.body).toBe(JSON.stringify({ id: "r1" }));
  });
});

describe("whoami", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns the {user} payload on 200 (auth on)", async () => {
    vi.stubGlobal("fetch", () =>
      Promise.resolve({ ok: true, json: () => Promise.resolve({ user: "tata" }) }),
    );
    expect(await whoami()).toEqual({ user: "tata" });
  });

  it("returns {user:null} on 200 when auth is off", async () => {
    vi.stubGlobal("fetch", () =>
      Promise.resolve({ ok: true, json: () => Promise.resolve({ user: null }) }),
    );
    expect(await whoami()).toEqual({ user: null });
  });

  it("returns null on 401 (not logged in)", async () => {
    vi.stubGlobal("fetch", () => Promise.resolve({ ok: false, json: () => Promise.resolve({}) }));
    expect(await whoami()).toBeNull();
  });

  it("returns null when fetch rejects", async () => {
    vi.stubGlobal("fetch", () => Promise.reject(new Error("offline")));
    expect(await whoami()).toBeNull();
  });
});

describe("login", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns true on a 2xx response and sends the credentials", async () => {
    const seen: { url: string; body: unknown }[] = [];
    vi.stubGlobal("fetch", (url: URL, init: RequestInit) => {
      seen.push({ url: url.href, body: init.body });
      return Promise.resolve({ ok: true });
    });
    expect(await login("tata", "s3cret")).toBe(true);
    expect(seen[0]?.url).toContain("/api/login");
    expect(seen[0]?.body).toBe(JSON.stringify({ user: "tata", password: "s3cret" }));
  });

  it("returns false on a non-2xx response", async () => {
    vi.stubGlobal("fetch", () => Promise.resolve({ ok: false }));
    expect(await login("tata", "nope")).toBe(false);
  });

  it("returns false when fetch rejects", async () => {
    vi.stubGlobal("fetch", () => Promise.reject(new Error("offline")));
    expect(await login("tata", "x")).toBe(false);
  });
});

describe("logout", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("POSTs /api/logout", async () => {
    const seen: string[] = [];
    vi.stubGlobal("fetch", (url: URL) => {
      seen.push(url.href);
      return Promise.resolve({ ok: true });
    });
    await logout();
    expect(seen[0]).toContain("/api/logout");
  });

  it("swallows a rejected fetch (best-effort)", async () => {
    vi.stubGlobal("fetch", () => Promise.reject(new Error("offline")));
    await expect(logout()).resolves.toBeUndefined();
  });
});
