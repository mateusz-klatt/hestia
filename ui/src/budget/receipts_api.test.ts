import { afterEach, describe, expect, it, vi } from "vitest";
import { createBudgetApi } from "./api";
import { RECEIPT_CONTRACT_HEADERS, createReceiptsApi, receiptsApi } from "./receipts_api";
import type { ReceiptsApi } from "./receipts_api";

const OWNER_TAG = "ZYtoux-yHqo-dVxLUe_uXkgvKgk2LWgJCV6VODecrlo";
const CONTRACT_HEADER = "X-Hestia-Contract-Version";
const VERSION_ERROR = "budget_receipt_contract_version_invalid";

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
    expect(new Headers(init.headers).get(CONTRACT_HEADER)).toBe("0.59.0");
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
    expect(new Headers(init.headers).get(CONTRACT_HEADER)).toBe("0.59.0");
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
    expect(new Headers(detail.init.headers).get(CONTRACT_HEADER)).toBe("0.59.0");
    expect(new Headers(detail.init.headers).get("X-Hestia-Expected-Actor")).toBe(OWNER_TAG);
    expect(detail.init.credentials).toBe("same-origin");

    const confirm = sent(fetchMock, 2);
    expect(confirm.url.pathname).toMatch(/\/api\/budget\/receipts\/confirm$/);
    expect(new Headers(confirm.init.headers).get("Content-Type")).toBe("application/json");
    expect(new Headers(confirm.init.headers).get(CONTRACT_HEADER)).toBe(RECEIPT_CONTRACT_HEADERS[CONTRACT_HEADER]);
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

  it.each([
    { path: "list", read: (client: ReceiptsApi) => client.receiptsList({ limit: 25 }) },
    { path: "search", read: (client: ReceiptsApi) => client.receiptsSearch({ query: "Sklep" }) },
    { path: "detail", read: (client: ReceiptsApi) => client.receiptDetail("rec-42") },
  ])("retries receipt $path once at the previous version on the explicit version error", async ({ path, read }) => {
    const payload = path === "detail"
      ? { receipt: { receipt_id: "rec-42" }, candidates: [] }
      : { receipts: [], next_cursor: null };
    const fetchMock = respond(200, payload);
    const gate = createBudgetApi("owner");
    await gate.capabilities();
    fetchMock.mockImplementationOnce(() => Promise.resolve(
      new Response(JSON.stringify({ error: { code: VERSION_ERROR } }), { status: 400 }),
    ));

    expect(await read(createReceiptsApi(gate))).toEqual({ ok: true, data: payload });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    const first = sent(fetchMock, 1);
    const retry = sent(fetchMock, 2);
    expect(first.url.pathname).toBe(`/api/budget/receipts/${path}`);
    expect(retry.url.href).toBe(first.url.href);
    expect(retry.init.method).toBe(first.init.method);
    expect(retry.init.body).toBe(first.init.body);
    expect(retry.init.signal).toBe(first.init.signal);
    expect(retry.init.credentials).toBe("same-origin");
    expect(retry.init.cache).toBe("no-store");
    expect(new Headers(first.init.headers).get(CONTRACT_HEADER)).toBe("0.59.0");
    const headers = new Headers(first.init.headers);
    headers.set(CONTRACT_HEADER, "0.57.0");
    expect(Object.fromEntries(new Headers(retry.init.headers))).toEqual(Object.fromEntries(headers));
    expect(new Headers(retry.init.headers).get("X-Hestia-Expected-Actor")).toBe(OWNER_TAG);
  });

  it("preserves unrelated receipt read errors and never retries unrelated reads", async () => {
    const fetchMock = respond(400, { error: VERSION_ERROR });
    const gate = createBudgetApi("owner");
    await gate.capabilities();
    fetchMock.mockImplementationOnce(() => Promise.resolve(
      new Response(JSON.stringify({ error: "budget_receipt_invalid_request" }), { status: 400 }),
    ));
    expect(await gate.receiptsList()).toMatchObject({
      ok: false, status: 400, error: "budget_receipt_invalid_request",
    });
    expect(await gate.inbox()).toMatchObject({ ok: false, status: 400, error: VERSION_ERROR });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(new Headers(sent(fetchMock, 2).init.headers).has(CONTRACT_HEADER)).toBe(false);
  });

  it("does not retry a receipt read invalidated by an actor change", async () => {
    const fetchMock = respond(200, {});
    const gate = createBudgetApi("owner");
    await gate.capabilities();
    let resolveRead!: (response: Response) => void;
    fetchMock.mockImplementationOnce(() => new Promise<Response>((resolve) => { resolveRead = resolve; }));
    const pending = gate.receiptDetail("rec-42");
    fetchMock.mockImplementationOnce(() => Promise.resolve(
      new Response(JSON.stringify(capabilitiesFor("another-owner", "B".repeat(43))), { status: 200 }),
    ));
    expect(await gate.capabilities()).toMatchObject({ ok: false, status: 409, error: "budget_actor_changed" });
    resolveRead(new Response(JSON.stringify({ error: VERSION_ERROR }), { status: 400 }));

    expect(await pending).toMatchObject({ ok: false, status: 409, error: "budget_actor_changed" });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(sent(fetchMock, 1).init.signal?.aborted).toBe(true);
  });

  it("reads group detail via a gated GET without retrying unsupported or unknown groups", async () => {
    const payload = {
      group: { group_id: "group-42", group_status: "confirmed", group_version: 3,
        member_count: 2, fee_amount: "1.25", evidence_receipt_id: null },
      members: [],
      evidence: null,
      linked_account_id: null,
      linked_entry_ref: null,
    };
    const fetchMock = respond(200, payload);
    const gate = createBudgetApi("owner");
    await gate.capabilities();
    const client = createReceiptsApi(gate);

    expect(await client.fetchReceiptLinkGroupDetail?.("group-42")).toEqual({ ok: true, data: payload });
    const { url, init } = sent(fetchMock, 1);
    expect(url.pathname).toBe("/api/budget/receipts/group/detail");
    expect(Object.fromEntries(url.searchParams)).toEqual({ group_id: "group-42" });
    expect(init.method ?? "GET").toBe("GET");
    expect(init.body).toBeUndefined();
    expect(init.credentials).toBe("same-origin");
    expect(init.cache).toBe("no-store");
    expect(new Headers(init.headers).get(CONTRACT_HEADER)).toBe("0.59.0");
    expect(new Headers(init.headers).get("X-Hestia-Expected-Actor")).toBe(OWNER_TAG);

    fetchMock.mockImplementationOnce(() => Promise.resolve(new Response(
      JSON.stringify({ error: VERSION_ERROR }), { status: 400 },
    )));
    fetchMock.mockImplementationOnce(() => Promise.resolve(new Response(
      JSON.stringify({ error: { code: "budget_receipt_not_found" } }), { status: 404 },
    )));
    expect(await client.fetchReceiptLinkGroupDetail?.("group-42")).toMatchObject({
      ok: false, status: 400, error: VERSION_ERROR,
    });
    expect(await client.fetchReceiptLinkGroupDetail?.("missing")).toMatchObject({
      ok: false, status: 404, error: "budget_receipt_not_found",
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
    expect(await receiptsApi.fetchReceiptLinkGroupDetail?.("group-42")).toEqual({
      ok: false,
      status: 503,
      error: "budget_server_changed",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
