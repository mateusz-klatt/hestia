import { afterEach, describe, expect, it, vi } from "vitest";
import { createBudgetApi } from "./api";
import { RECEIPT_CONTRACT_HEADERS, createReceiptsApi, receiptsApi } from "./receipts_api";

const OWNER_TAG = "ZYtoux-yHqo-dVxLUe_uXkgvKgk2LWgJCV6VODecrlo";

function capabilitiesFor(user: string | null = "owner", tag = OWNER_TAG) {
  return {
    user,
    actor_tag: user === null ? null : tag,
    installation_permissions: [],
    own_budget: {
      permissions: ["budget.view"],
      can_view: true,
      can_use_receipts: true,
      can_manage_devices: true,
    },
    delegated_receipts: null,
    household_available: false,
  };
}

function respond(status: number, body: unknown, headers: HeadersInit = {}) {
  const fetchMock = vi.fn((input: unknown) => {
    if ((input as URL).pathname.endsWith("/api/me/capabilities")) {
      return Promise.resolve(new Response(JSON.stringify(capabilitiesFor()), { status: 200 }));
    }
    return Promise.resolve(new Response(body === undefined ? "" : JSON.stringify(body), { status, headers }));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function sent(fetchMock: ReturnType<typeof respond>, index = 0): { url: URL; init: RequestInit } {
  const [url, init] = fetchMock.mock.calls[index] as unknown as [URL, RequestInit];
  return { url, init };
}

afterEach(() => { vi.unstubAllGlobals(); });

describe("receipts api", () => {
  it("lists receipts via POST JSON through the verified gate with X-Hestia-Expected-Actor and contract header", async () => {
    const fetchMock = respond(200, { receipts: [], next_cursor: null });
    const gate = createBudgetApi("owner");
    await gate.capabilities();
    const client = createReceiptsApi(gate);

    const result = await client.receiptsList({ status: "needs_review", document_kind: "invoice", limit: 25 });
    expect(result.ok).toBe(true);
    const { url, init } = sent(fetchMock, 1);
    expect(url.pathname).toMatch(/\/api\/budget\/receipts\/list$/);
    expect(url.search).toBe("");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("same-origin");
    expect(init.cache).toBe("no-store");
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
    expect(new Headers(init.headers).get("X-Hestia-Contract-Version")).toBe(RECEIPT_CONTRACT_HEADERS["X-Hestia-Contract-Version"]);
    expect(new Headers(init.headers).get("X-Hestia-Expected-Actor")).toBe(OWNER_TAG);
    expect(JSON.parse(init.body as string)).toEqual({
      status: "needs_review",
      document_kind: "invoice",
      limit: 25,
    });
  });

  it("searches receipts via POST JSON rather than query strings", async () => {
    const fetchMock = respond(200, { receipts: [], next_cursor: "cursor-2" });
    const gate = createBudgetApi("owner");
    await gate.capabilities();
    const client = createReceiptsApi(gate);

    const result = await client.receiptsSearch({ query: "Lidl", limit: 50, cursor: "cursor-1" });
    expect(result).toEqual({ ok: true, data: { receipts: [], next_cursor: "cursor-2" } });
    const { url, init } = sent(fetchMock, 1);
    expect(url.pathname).toMatch(/\/api\/budget\/receipts\/search$/);
    expect(url.search).toBe("");
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
    expect(new Headers(init.headers).get("X-Hestia-Expected-Actor")).toBe(OWNER_TAG);
    expect(JSON.parse(init.body as string)).toEqual({ query: "Lidl", limit: 50, cursor: "cursor-1" });
  });

  it("reads receipt details and sends confirm/delete CAS mutations with expected-actor and lock enforcement", async () => {
    const fetchMock = respond(200, { ok: true });
    const gate = createBudgetApi("owner");
    await gate.capabilities();
    const client = createReceiptsApi(gate);

    await client.receiptDetail("rec-42");
    await client.confirmReceipt({
      receipt_id: "rec-42",
      expected_version: 2,
      raw_shop_name: "Sklep ABC",
      raw_nip: null,
      receipt_date: "2026-11-12",
      receipt_time: null,
      total_amount: "45.00",
      currency: "PLN",
      receipt_kind: "purchase",
      payment_method: "card",
      lines: [{
        raw_name: "Pozycja",
        quantity: "1",
        raw_unit: null,
        unit_price_raw: null,
        discount_amount: "0",
        gross_line_total: "45.00",
        net_line_total: "45.00",
        line_kind: "other",
        ptu_code: null,
      }],
      link: null,
    });
    await client.deleteReceipt({ receipt_id: "rec-42", expected_version: 3 });

    const detail = sent(fetchMock, 1);
    expect(detail.url.pathname).toMatch(/\/api\/budget\/receipts\/detail$/);
    expect(Object.fromEntries(detail.url.searchParams)).toEqual({ receipt_id: "rec-42" });
    expect(new Headers(detail.init.headers).get("X-Hestia-Contract-Version")).toBe(RECEIPT_CONTRACT_HEADERS["X-Hestia-Contract-Version"]);
    expect(new Headers(detail.init.headers).get("X-Hestia-Expected-Actor")).toBe(OWNER_TAG);
    expect(detail.init.credentials).toBe("same-origin");

    const confirm = sent(fetchMock, 2);
    expect(confirm.url.pathname).toMatch(/\/api\/budget\/receipts\/confirm$/);
    expect(new Headers(confirm.init.headers).get("Content-Type")).toBe("application/json");
    expect(new Headers(confirm.init.headers).get("X-Hestia-Contract-Version")).toBe(RECEIPT_CONTRACT_HEADERS["X-Hestia-Contract-Version"]);
    expect(new Headers(confirm.init.headers).get("X-Hestia-Expected-Actor")).toBe(OWNER_TAG);
    expect(JSON.parse(confirm.init.body as string)).toMatchObject({
      receipt_id: "rec-42",
      expected_version: 2,
      link: null,
    });

    const del = sent(fetchMock, 3);
    expect(del.url.pathname).toMatch(/\/api\/budget\/receipts\/delete$/);
    expect(new Headers(del.init.headers).get("Content-Type")).toBe("application/json");
    expect(new Headers(del.init.headers).get("X-Hestia-Expected-Actor")).toBe(OWNER_TAG);
    expect(JSON.parse(del.init.body as string)).toEqual({ receipt_id: "rec-42", expected_version: 3 });

    expect(client.isAuthorizationLocked?.()).toBe(false);
    gate.setAuthorizationLocked(true);
    expect(client.isAuthorizationLocked?.()).toBe(true);
    expect(await client.deleteReceipt({ receipt_id: "rec-42", expected_version: 4 })).toEqual({
      ok: false,
      status: 503,
      error: "budget_server_changed",
    });
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it("loads WebP receipt images as Blob without Blob.stream and parses structured errors cleanly", async () => {
    const webpBytes = new TextEncoder().encode("webp-bytes");
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(capabilitiesFor()), { status: 200 }))
      .mockResolvedValueOnce(new Response(webpBytes, {
        status: 200,
        headers: { "Content-Type": "image/webp" },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ error: "budget_receipts_disabled" }), { status: 404 }))
      .mockRejectedValueOnce(new TypeError("offline"));
    vi.stubGlobal("fetch", fetchMock);

    const gate = createBudgetApi("owner");
    await gate.capabilities();
    const client = createReceiptsApi(gate);

    const okRes = await client.receiptImage("rec-img");
    expect(okRes.ok).toBe(true);
    if (okRes.ok) {
      expect(okRes.data).toBeInstanceOf(Blob);
      expect(okRes.data.size).toBe(webpBytes.byteLength);
      expect(okRes.data.type).toBe("image/webp");
    }
    const [imgUrl, imgInit] = fetchMock.mock.calls[1] as unknown as [URL, RequestInit];
    expect(imgUrl.pathname).toMatch(/\/api\/budget\/receipts\/image$/);
    expect(Object.fromEntries(imgUrl.searchParams)).toEqual({ receipt_id: "rec-img" });
    expect(imgInit.credentials).toBe("same-origin");
    expect(imgInit.cache).toBe("no-store");
    expect(new Headers(imgInit.headers).get("X-Hestia-Expected-Actor")).toBe(OWNER_TAG);

    expect(await client.receiptImage("rec-off")).toEqual({
      ok: false,
      status: 404,
      error: "budget_receipts_disabled",
      current: undefined,
    });
    expect(await client.receiptImage("rec-net")).toEqual({
      ok: false,
      status: 0,
      error: "budget_network",
    });
  });

  it("refuses unverified calls on the default receiptsApi gate", async () => {
    const fetchMock = respond(200, {});
    expect(await receiptsApi.receiptsList({ limit: 10 })).toEqual({
      ok: false,
      status: 503,
      error: "budget_server_changed",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
