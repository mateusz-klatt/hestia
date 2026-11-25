import { afterEach, describe, expect, it, vi } from "vitest";
import { forecastOf, monthlyBudgetChartsOf, monthlyBudgetOf } from "./analytics/fixtures";
import { budgetApi, createBudgetApi } from "./api";
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
  it("binds only owner-budget requests to the mounted actor", async () => {
    const fetchMock = respond(200, {});
    const scoped = createBudgetApi("owner");
    await scoped.capabilities();
    await scoped.accounts();
    await scoped.receiptDevices();
    expect(new Headers(sent(fetchMock, 0).init.headers).has("X-Hestia-Expected-Actor")).toBe(false);
    for (const index of [1, 2]) {
      expect(new Headers(sent(fetchMock, index).init.headers).get("X-Hestia-Expected-Actor")).toBe("b3duZXI");
    }
    for (const [index, actor, encoded] of [[3, "Ági", "w4FnaQ"], [4, "Łukasz", "xYF1a2Fzeg"]] as const) {
      await createBudgetApi(actor).accounts();
      expect(new Headers(sent(fetchMock, index).init.headers).get("X-Hestia-Expected-Actor")).toBe(encoded);
    }
  });

  it("reads with the same-origin cookie and builds bounded queries", async () => {
    const fetchMock = respond(200, { account_id: "a", transactions: [], account_total: 0, uncertain_total: 0, next_cursor: null });
    const result = await budgetApi.transactions("local-a", { cursor: "opaque", dateFrom: "2026-11-01", dateTo: "" });
    expect(result.ok).toBe(true);
    const { url, init } = sent(fetchMock);
    expect(url.pathname).toMatch(/\/api\/budget\/transactions$/);
    expect(Object.fromEntries(url.searchParams)).toEqual({ account_id: "local-a", cursor: "opaque", date_from: "2026-11-01" });
    expect(init).toMatchObject({ credentials: "same-origin", cache: "no-store" });
  });

  it("posts receipt device management requests with the browser session", async () => {
    const fetchMock = respond(200, {});
    await budgetApi.receiptDevices();
    await budgetApi.pairReceiptDevice();
    await budgetApi.cancelReceiptPairCode("pair-code-id");
    await budgetApi.revokeReceiptDevice("paired-device");

    const requests = [
      { path: "list", body: {} },
      { path: "pair-code", body: {} },
      { path: "pair-code-cancel", body: { code_id: "pair-code-id" } },
      { path: "revoke", body: { token_id: "paired-device" } },
    ];
    requests.forEach(({ path, body }, index) => {
      const { url, init } = sent(fetchMock, index);
      expect(url.pathname).toBe(`/api/budget/receipts/device-token/${path}`);
      expect(init).toEqual({
        credentials: "same-origin", cache: "no-store", method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
    });
  });

  it("posts the explicit category colour spread mutation", async () => {
    const fetchMock = respond(200, { ok: true, categories: [] });
    await budgetApi.spreadCategoryColors();
    const { url, init } = sent(fetchMock);
    expect(url.pathname).toBe("/api/budget/analytics/category-colors-spread");
    expect(init).toEqual({
      credentials: "same-origin", cache: "no-store", method: "POST",
      headers: { "Content-Type": "application/json" }, body: "{}",
    });
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
    await budgetApi.inbox();
    expect(sent(fetchMock, 10).url.pathname).toMatch(/\/api\/budget\/analytics\/inbox$/);
    expect(sent(fetchMock, 10).url.search).toBe("");
    await budgetApi.markInbox({
      items: [{ kind: "consent_renewal", subject_id: "a".repeat(64), milestone: "g1:exp1800000000:lead:7" }],
      read: true,
      expected_reads_version: "0".repeat(64),
    });
    expect(sent(fetchMock, 11).url.pathname).toMatch(/\/api\/budget\/analytics\/inbox-mark$/);
    expect(sent(fetchMock, 11).init.method).toBe("POST");
    expect(Object.keys(sent(fetchMock, 11).init.headers as Record<string, string>)).not.toContain("Authorization");
    expect(JSON.parse(sent(fetchMock, 11).init.body as string)).toEqual({
      items: [{ kind: "consent_renewal", subject_id: "a".repeat(64), milestone: "g1:exp1800000000:lead:7" }],
      read: true,
      expected_reads_version: "0".repeat(64),
    });
  });

  it("sends row_keys via JSON POST without Authorization header for transactions-by-keys", async () => {
    const fetchMock = respond(200, { period: "", start: "", end: "", snapshot_digest: "d", transactions: [], filtered_total: 0, next_cursor: null });
    await budgetApi.analyticsTransactionsByKeys({ row_keys: ["acc-1/r1", "acc-2/r2"], currency: "PLN" });
    const { url, init } = sent(fetchMock);
    expect(url.pathname).toMatch(/\/api\/budget\/analytics\/transactions-by-keys$/);
    expect(url.search).toBe("");
    expect(init.method).toBe("POST");
    expect(Object.keys(init.headers as Record<string, string>)).not.toContain("Authorization");
    expect(JSON.parse(init.body as string)).toEqual({ row_keys: ["acc-1/r1", "acc-2/r2"], currency: "PLN" });
  });

  it("builds quarantine reads with and without an account filter", async () => {
    const fetchMock = respond(200, {});
    await budgetApi.quarantine();
    expect(sent(fetchMock).url.pathname).toMatch(/\/api\/budget\/analytics\/quarantine$/);
    expect(sent(fetchMock).url.search).toBe("");
    expect(sent(fetchMock).init.method).toBeUndefined();
    await budgetApi.quarantine("acc-1");
    expect(sent(fetchMock, 1).url.pathname).toMatch(/\/api\/budget\/analytics\/quarantine$/);
    expect(Object.fromEntries(sent(fetchMock, 1).url.searchParams)).toEqual({ account_id: "acc-1" });
    expect(sent(fetchMock, 1).init.method).toBeUndefined();
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

  it("reads the forecast with optional horizon and currency query parameters", async () => {
    const data = forecastOf();
    const fetchMock = respond(200, data);
    expect(await budgetApi.forecast()).toEqual({ ok: true, data });
    const { url, init } = sent(fetchMock);
    expect(url.pathname).toMatch(/\/api\/budget\/analytics\/forecast$/);
    expect(url.search).toBe("");
    expect(init).toEqual({ credentials: "same-origin", cache: "no-store" });

    await budgetApi.forecast(6, "EUR");
    expect(Object.fromEntries(sent(fetchMock, 1).url.searchParams)).toEqual({ months: "6", currency: "EUR" });
    await budgetApi.forecast(12, "");
    expect(Object.fromEntries(sent(fetchMock, 2).url.searchParams)).toEqual({ months: "12" });
    await budgetApi.forecast(undefined, "PLN");
    expect(Object.fromEntries(sent(fetchMock, 3).url.searchParams)).toEqual({ currency: "PLN" });
  });

  it("reads the monthly budget with optional period and currency query parameters", async () => {
    const data = monthlyBudgetOf();
    const fetchMock = respond(200, data);
    expect(await budgetApi.monthlyBudget()).toEqual({ ok: true, data });
    const { url, init } = sent(fetchMock);
    expect(url.pathname).toMatch(/\/api\/budget\/analytics\/monthly-budget$/);
    expect(url.search).toBe("");
    expect(init).toEqual({ credentials: "same-origin", cache: "no-store" });

    await budgetApi.monthlyBudget("2026-10", "EUR");
    expect(Object.fromEntries(sent(fetchMock, 1).url.searchParams)).toEqual({ period: "2026-10", currency: "EUR" });
    await budgetApi.monthlyBudget("2026-10", "");
    expect(Object.fromEntries(sent(fetchMock, 2).url.searchParams)).toEqual({ period: "2026-10" });
    await budgetApi.monthlyBudget(undefined, "PLN");
    expect(Object.fromEntries(sent(fetchMock, 3).url.searchParams)).toEqual({ currency: "PLN" });
  });

  it("reads monthly budget charts with optional period, currency, and history query parameters", async () => {
    const data = monthlyBudgetChartsOf();
    const fetchMock = respond(200, data);
    expect(await budgetApi.monthlyBudgetCharts()).toEqual({ ok: true, data });
    const { url, init } = sent(fetchMock);
    expect(url.pathname).toMatch(/\/api\/budget\/analytics\/monthly-budget\/charts$/);
    expect(url.search).toBe("");
    expect(init).toEqual({ credentials: "same-origin", cache: "no-store" });

    await budgetApi.monthlyBudgetCharts("2026-10", "EUR", 12);
    expect(Object.fromEntries(sent(fetchMock, 1).url.searchParams)).toEqual({
      period: "2026-10", currency: "EUR", history: "12",
    });
    await budgetApi.monthlyBudgetCharts(undefined, "PLN", 6);
    expect(Object.fromEntries(sent(fetchMock, 2).url.searchParams)).toEqual({ currency: "PLN", history: "6" });
  });

  it("sends every control with the entity's current version", async () => {
    const fetchMock = respond(200, { ok: true });
    await budgetApi.setSelected(account({ control_version: 7 }), false);
    await budgetApi.setKind(account({ control_version: 8 }), "savings");
    await budgetApi.setCreditLimit(account({ control_version: 9 }), { amount: "10000", currency: "PLN" });
    await budgetApi.setCreditLimit(account({ control_version: 10 }), null);
    await budgetApi.setPaused(connection, true);
    await budgetApi.setBackground(connection, true);
    const bodies = fetchMock.mock.calls.map((_, index) => JSON.parse(sent(fetchMock, index).init.body as string) as unknown);
    expect(bodies).toEqual([
      { account_id: "local-a", selected: false, expected_version: 7 },
      { account_id: "local-a", kind: "savings", expected_version: 8 },
      { account_id: "local-a", credit_limit: { amount: "10000", currency: "PLN" }, expected_version: 9 },
      { account_id: "local-a", credit_limit: null, expected_version: 10 },
      { connection_id: "bank", paused: true, expected_version: 4 },
      { connection_id: "bank", enabled: true, expected_version: 4 },
    ]);
    await budgetApi.accounts("bank");
    await budgetApi.jobs();
    await budgetApi.balances("local-a");
    await budgetApi.limits();
    expect(sent(fetchMock, 9).url.pathname).toMatch(/\/api\/budget\/limits$/);
    await budgetApi.consentStatus();
    await budgetApi.consentPending();
    await budgetApi.consentRenewalReminders();
    await budgetApi.consentReconcile();
    await budgetApi.consentStart(connection, "renew");
    await budgetApi.consentCancel("flow-1");
    expect([10, 11, 12, 13, 14, 15].map((index) => sent(fetchMock, index).url.pathname.replace(/.*\/api\/budget\//, "")))
      .toEqual(["consent/status", "consent/pending", "consent/renewal-reminders", "consent/reconcile", "consent/start", "consent/cancel"]);
    expect(sent(fetchMock, 12).url.search).toBe("");
    expect(sent(fetchMock, 13).url.search).toBe("");
    expect(JSON.parse(sent(fetchMock, 14).init.body as string)).toEqual({ connection_id: "bank", expected_version: 4, intent: "renew" });
    expect(JSON.parse(sent(fetchMock, 15).init.body as string)).toEqual({ flow_id: "flow-1" });
    expect(sent(fetchMock, 6).url.searchParams.get("connection_id")).toBe("bank");
    expect(sent(fetchMock, 7).url.search).toBe("");
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
    const fetchMock = respond(200, {
      user: "owner", installation_permissions: [], own_budget: {
        permissions: ["budget.view"], can_view: true, can_use_receipts: true, can_manage_devices: true,
      }, delegated_receipts: null, household_available: false,
    });
    expect((await budgetApi.capabilities()).ok).toBe(true);
    expect(sent(fetchMock).url.pathname).toMatch(/\/api\/me\/capabilities$/);
  });

  it("parses nested actor errors and makes legacy mode read-only", async () => {
    respond(409, { error: { code: "budget_actor_changed" } });
    expect(await budgetApi.connections()).toEqual({
      ok: false, status: 409, error: "budget_actor_changed", current: undefined,
    });

    const scoped = createBudgetApi("owner");
    scoped.setLegacyReadOnly(true);
    const fetchMock = respond(200, {});
    expect(await scoped.setPaused(connection, true)).toEqual({
      ok: false, status: 409, error: "budget_legacy_read_only",
    });
    expect(fetchMock).not.toHaveBeenCalled();
    expect((await scoped.receiptDevices()).ok).toBe(true);
    expect((await scoped.analyticsTransactionsByKeys({ row_keys: [], currency: "PLN" })).ok).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(2);
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
