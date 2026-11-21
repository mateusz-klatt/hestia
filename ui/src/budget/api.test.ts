import { afterEach, describe, expect, it, vi } from "vitest";
import { budgetApi } from "./api";
import { account, connection } from "./fixtures";

function respond(status: number, body: unknown) {
  const fetchMock = vi.fn(() => Promise.resolve(new Response(body === undefined ? "" : JSON.stringify(body), { status })));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function sent(fetchMock: ReturnType<typeof respond>, index = 0): { url: URL; init: RequestInit } {
  const [url, init] = fetchMock.mock.calls[index] as unknown as [URL, RequestInit];
  return { url, init };
}

afterEach(() => { vi.unstubAllGlobals(); });

describe("budget api", () => {
  it("reads with the same-origin cookie and builds bounded queries", async () => {
    const fetchMock = respond(200, { account_id: "a", transactions: [], account_total: 0, uncertain_total: 0, next_cursor: null });
    const result = await budgetApi.transactions("local-a", { cursor: "opaque", dateFrom: "2026-11-01", dateTo: "" });
    expect(result.ok).toBe(true);
    const { url, init } = sent(fetchMock);
    expect(url.pathname).toMatch(/\/api\/budget\/transactions$/);
    expect(Object.fromEntries(url.searchParams)).toEqual({ account_id: "local-a", cursor: "opaque", date_from: "2026-11-01" });
    expect(init).toMatchObject({ credentials: "same-origin", cache: "no-store" });
  });

  it("sends refresh with its idempotency key, JSON body and no Authorization header", async () => {
    const fetchMock = respond(202, { ok: true, job: { id: "job", created: true } });
    await budgetApi.refresh(connection, { accountId: "local-a", operation: "balances" }, "synthetic-key-0000001");
    const { url, init } = sent(fetchMock);
    expect(url.pathname).toMatch(/\/api\/budget\/refresh$/);
    const headers = init.headers as Record<string, string>;
    expect(headers["Idempotency-Key"]).toBe("synthetic-key-0000001");
    expect(headers["Content-Type"]).toBe("application/json");
    expect(Object.keys(headers)).not.toContain("Authorization");
    expect(JSON.parse(init.body as string)).toEqual({ connection_id: "bank", expected_version: 4,
      account_id: "local-a", operation: "balances" });
    await budgetApi.refresh(connection);
    expect(JSON.parse(sent(fetchMock, 1).init.body as string)).toEqual({ connection_id: "bank", expected_version: 4 });
  });

  it("sends every control with the entity's current version", async () => {
    const fetchMock = respond(200, { ok: true });
    await budgetApi.setSelected(account({ control_version: 7 }), false);
    await budgetApi.setKind(account({ control_version: 8 }), "savings");
    await budgetApi.setPaused(connection, true);
    await budgetApi.setBackground(connection, true);
    const bodies = fetchMock.mock.calls.map((_, index) => JSON.parse(sent(fetchMock, index).init.body as string) as unknown);
    expect(bodies).toEqual([
      { account_id: "local-a", selected: false, expected_version: 7 },
      { account_id: "local-a", kind: "savings", expected_version: 8 },
      { connection_id: "bank", paused: true, expected_version: 4 },
      { connection_id: "bank", enabled: true, expected_version: 4 },
    ]);
    await budgetApi.accounts("bank");
    await budgetApi.jobs();
    await budgetApi.balances("local-a");
    expect(sent(fetchMock, 4).url.searchParams.get("connection_id")).toBe("bank");
    expect(sent(fetchMock, 5).url.search).toBe("");
  });

  it("treats an unreadable or incomplete success as an unknown outcome", async () => {
    respond(202, {});
    expect(await budgetApi.refresh(connection)).toEqual({ ok: false, status: 202, error: "budget_uncertain" });
    respond(202, { ok: true, job: { id: "job" } });
    expect((await budgetApi.refresh(connection)).ok).toBe(false);
    respond(202, { ok: true, job: null });
    expect((await budgetApi.refresh(connection)).ok).toBe(false);
    respond(202, { ok: true, job: { id: "job", status: "queued", connection_id: "bank", created: true } });
    expect(await budgetApi.refresh(connection)).toMatchObject({ ok: true, data: { job: { id: "job", created: true } } });
    respond(409, { ok: false, error: "budget_refresh_in_progress" });
    expect(await budgetApi.refresh(connection)).toMatchObject({ ok: false, status: 409 });
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response("not json", { status: 202 }))));
    expect(await budgetApi.refresh(connection)).toEqual({ ok: false, status: 202, error: "budget_uncertain" });
    const fetchMock = respond(200, { user: "owner", role: "admin" });
    expect(await budgetApi.whoami()).toEqual({ ok: true, data: { user: "owner", role: "admin" } });
    expect(sent(fetchMock).url.pathname).toMatch(/\/api\/whoami$/);
  });

  it("maps refusals, empty middleware bodies and network loss without throwing", async () => {
    respond(409, { ok: false, error: "budget_version_conflict", current: { id: "bank" } });
    expect(await budgetApi.connections()).toEqual({ ok: false, status: 409, error: "budget_version_conflict", current: { id: "bank" } });
    respond(401, undefined);
    expect(await budgetApi.connections()).toEqual({ ok: false, status: 401, error: "budget_http_401", current: undefined });
    respond(200, undefined);
    expect(await budgetApi.connections()).toEqual({ ok: false, status: 200, error: "budget_uncertain" });
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("offline"))));
    expect(await budgetApi.connections()).toEqual({ ok: false, status: 0, error: "budget_network" });
  });
});
