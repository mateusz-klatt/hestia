import { afterEach, describe, expect, it, vi } from "vitest";
import { RECEIPT_CONTRACT_HEADERS, createReceiptsApi, receiptsApi, withActorHeader } from "./receipts_api";

function respond(status: number, body: unknown, headers: HeadersInit = {}) {
  const fetchMock = vi.fn(() => Promise.resolve(new Response(
    body === undefined ? "" : JSON.stringify(body),
    { status, headers },
  )));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function sent(fetchMock: ReturnType<typeof respond>, index = 0): { url: URL; init: RequestInit } {
  const [url, init] = fetchMock.mock.calls[index] as unknown as [URL, RequestInit];
  return { url, init };
}

afterEach(() => { vi.unstubAllGlobals(); });

describe("receipts api", () => {
  it("lists receipts via POST JSON with browser cookie and v0.40+ contract header", async () => {
    const fetchMock = respond(200, { receipts: [], next_cursor: null });
    const result = await receiptsApi.receiptsList({ status: "needs_review", document_kind: "invoice", limit: 25 });
    expect(result.ok).toBe(true);
    const { url, init } = sent(fetchMock);
    expect(url.pathname).toMatch(/\/api\/budget\/receipts\/list$/);
    expect(url.search).toBe("");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("same-origin");
    expect(init.cache).toBe("no-store");
    expect(init.headers).toEqual({
      "Content-Type": "application/json",
      ...RECEIPT_CONTRACT_HEADERS,
    });
    expect(JSON.parse(init.body as string)).toEqual({
      status: "needs_review",
      document_kind: "invoice",
      limit: 25,
    });
  });

  it("searches receipts via POST JSON rather than query strings", async () => {
    const fetchMock = respond(200, { receipts: [], next_cursor: "cursor-2" });
    const result = await receiptsApi.receiptsSearch({ query: "Lidl", limit: 50, cursor: "cursor-1" });
    expect(result).toEqual({ ok: true, data: { receipts: [], next_cursor: "cursor-2" } });
    const { url, init } = sent(fetchMock);
    expect(url.pathname).toMatch(/\/api\/budget\/receipts\/search$/);
    expect(url.search).toBe("");
    expect(init.headers).toEqual({
      "Content-Type": "application/json",
      ...RECEIPT_CONTRACT_HEADERS,
    });
    expect(JSON.parse(init.body as string)).toEqual({ query: "Lidl", limit: 50, cursor: "cursor-1" });
  });

  it("reads receipt details and sends confirm/delete CAS mutations with contract header where needed", async () => {
    const fetchMock = respond(200, { ok: true });
    await receiptsApi.receiptDetail("rec-42");
    await receiptsApi.confirmReceipt({
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
    await receiptsApi.deleteReceipt({ receipt_id: "rec-42", expected_version: 3 });

    const detail = sent(fetchMock, 0);
    expect(detail.url.pathname).toMatch(/\/api\/budget\/receipts\/detail$/);
    expect(Object.fromEntries(detail.url.searchParams)).toEqual({ receipt_id: "rec-42" });
    expect(detail.init.headers).toEqual(RECEIPT_CONTRACT_HEADERS);
    expect(detail.init.credentials).toBe("same-origin");

    const confirm = sent(fetchMock, 1);
    expect(confirm.url.pathname).toMatch(/\/api\/budget\/receipts\/confirm$/);
    expect(confirm.init.headers).toEqual({ "Content-Type": "application/json", ...RECEIPT_CONTRACT_HEADERS });
    expect(JSON.parse(confirm.init.body as string)).toMatchObject({
      receipt_id: "rec-42",
      expected_version: 2,
      link: null,
    });

    const del = sent(fetchMock, 2);
    expect(del.url.pathname).toMatch(/\/api\/budget\/receipts\/delete$/);
    expect(del.init.headers).toEqual({ "Content-Type": "application/json" });
    expect(JSON.parse(del.init.body as string)).toEqual({ receipt_id: "rec-42", expected_version: 3 });
  });

  it("loads WebP receipt images as Blob and parses structured errors cleanly", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(new Blob(["webp-bytes"], { type: "image/webp" }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ error: "budget_receipts_disabled" }), { status: 404 }))
      .mockRejectedValueOnce(new TypeError("offline"));
    vi.stubGlobal("fetch", fetchMock);

    const okRes = await receiptsApi.receiptImage("rec-img");
    expect(okRes.ok).toBe(true);
    if (okRes.ok) expect(okRes.data).toBeInstanceOf(Blob);
    const [imgUrl, imgInit] = fetchMock.mock.calls[0] as unknown as [URL, RequestInit];
    expect(imgUrl.pathname).toMatch(/\/api\/budget\/receipts\/image$/);
    expect(Object.fromEntries(imgUrl.searchParams)).toEqual({ receipt_id: "rec-img" });
    expect(imgInit).toEqual({ credentials: "same-origin", cache: "no-store" });

    expect(await receiptsApi.receiptImage("rec-off")).toEqual({
      ok: false,
      status: 404,
      error: "budget_receipts_disabled",
      current: undefined,
    });
    expect(await receiptsApi.receiptImage("rec-net")).toEqual({
      ok: false,
      status: 0,
      error: "budget_network",
    });
  });

  it("exposes withActorHeader and createReceiptsApi without mutating default request headers", () => {
    expect(withActorHeader({ ...RECEIPT_CONTRACT_HEADERS }, "owner")).toEqual(RECEIPT_CONTRACT_HEADERS);
    const scoped = createReceiptsApi("owner");
    expect(typeof scoped.receiptsSearch).toBe("function");
  });
});
