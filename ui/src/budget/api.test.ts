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

  it("passes an analytics page limit only when one is given", async () => {
    const fetchMock = respond(200, { period: "2026-11", transactions: [], filtered_total: 0, next_cursor: null });
    const filter = { currency: "PLN", category: "", kind: "", query: "" };
    await budgetApi.analyticsTransactions("2026-11", filter, "opaque", 200);
    expect(Object.fromEntries(sent(fetchMock).url.searchParams)).toEqual({ period: "2026-11", currency: "PLN", cursor: "opaque", limit: "200" });
    await budgetApi.analyticsTransactions("2026-11", filter);
    expect(Object.fromEntries(sent(fetchMock, 1).url.searchParams)).toEqual({ period: "2026-11", currency: "PLN" });
    await budgetApi.recurring();
    expect(sent(fetchMock, 2).url.pathname).toMatch(/\/api\/budget\/analytics\/recurring$/);
    expect(sent(fetchMock, 2).url.search).toBe("");
    await budgetApi.recurring("EUR");
    expect(Object.fromEntries(sent(fetchMock, 3).url.searchParams)).toEqual({ currency: "EUR" });
    await budgetApi.spendingLimits("2026-11");
    expect(sent(fetchMock, 4).url.pathname).toMatch(/\/api\/budget\/analytics\/spending-limits$/);
    expect(Object.fromEntries(sent(fetchMock, 4).url.searchParams)).toEqual({ period: "2026-11" });
    await budgetApi.spendingLimits("2026-11", "PLN");
    expect(Object.fromEntries(sent(fetchMock, 5).url.searchParams)).toEqual({ period: "2026-11", currency: "PLN" });
    await budgetApi.saveSpendingLimit({
      category_id: "cat-food", currency: "PLN", monthly_limit: "500.5", warning_percent: 80, enabled: true, expected_version: 0,
    });
    expect(sent(fetchMock, 6).url.pathname).toMatch(/\/api\/budget\/analytics\/spending-limit-save$/);
    expect(sent(fetchMock, 6).init.method).toBe("POST");
    expect(JSON.parse(sent(fetchMock, 6).init.body as string)).toEqual({
      category_id: "cat-food", currency: "PLN", monthly_limit: "500.5", warning_percent: 80, enabled: true, expected_version: 0,
    });
    await budgetApi.mortgages();
    expect(sent(fetchMock, 7).url.pathname).toMatch(/\/api\/budget\/analytics\/mortgages$/);
    expect(sent(fetchMock, 7).url.search).toBe("");
    await budgetApi.saveMortgage({
      schedule_id: null, label: "Synthetic Loan", amount: "2450.5", currency: "PLN", due_day: 10,
      start_date: "2026-01-01", end_date: null, remind_days_before: 5, destination_kind: "owner_inbox",
      enabled: true, expected_version: 0,
    });
    expect(sent(fetchMock, 8).url.pathname).toMatch(/\/api\/budget\/analytics\/mortgage-save$/);
    expect(sent(fetchMock, 8).init.method).toBe("POST");
    expect(JSON.parse(sent(fetchMock, 8).init.body as string)).toMatchObject({
      schedule_id: null, label: "Synthetic Loan", destination_kind: "owner_inbox", expected_version: 0,
    });
    await budgetApi.setMortgagePayment({ schedule_id: "mtg-1", month: "2026-11", paid: true, expected_version: 1 });
    expect(sent(fetchMock, 9).url.pathname).toMatch(/\/api\/budget\/analytics\/mortgage-payment-set$/);
    expect(JSON.parse(sent(fetchMock, 9).init.body as string)).toEqual({
      schedule_id: "mtg-1", month: "2026-11", paid: true, expected_version: 1,
    });
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
    await budgetApi.limits();
    expect(sent(fetchMock, 7).url.pathname).toMatch(/\/api\/budget\/limits$/);
    await budgetApi.consentStatus();
    await budgetApi.consentPending();
    await budgetApi.consentStart(connection, "renew");
    await budgetApi.consentCancel("flow-1");
    expect([8, 9, 10, 11].map((index) => sent(fetchMock, index).url.pathname.replace(/.*\/api\/budget\//, "")))
      .toEqual(["consent/status", "consent/pending", "consent/start", "consent/cancel"]);
    expect(JSON.parse(sent(fetchMock, 10).init.body as string)).toEqual({ connection_id: "bank", expected_version: 4, intent: "renew" });
    expect(JSON.parse(sent(fetchMock, 11).init.body as string)).toEqual({ flow_id: "flow-1" });
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
