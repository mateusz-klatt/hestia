// Synthetic data only: no real receipts, merchants, NIPs, or bank transactions.
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { loadLocale } from "../i18n";
import type { BudgetResult } from "./api";
import { account, connection } from "./fixtures";
import { ReceiptsView } from "./receipts";
import type { ReceiptsApi } from "./receipts_api";
import { REDACTED_VALUE } from "./receipts_model";
import type {
  ReceiptCandidate,
  ReceiptDetail,
  ReceiptDetailResult,
  ReceiptListResult,
  ReceiptSummary,
} from "./receipts_model";

function ok<T>(data: T): Promise<BudgetResult<T>> { return Promise.resolve({ ok: true, data }); }
function fail<T>(status: number, error: string): Promise<BudgetResult<T>> {
  return Promise.resolve({ ok: false, status, error });
}

interface Deferred<T> { promise: Promise<BudgetResult<T>>; resolve: (value: BudgetResult<T>) => void; }
function deferred<T>(): Deferred<T> {
  let resolve: (value: BudgetResult<T>) => void = () => undefined;
  const promise = new Promise<BudgetResult<T>>((done) => { resolve = done; });
  return { promise, resolve };
}

function receiptSummary(overrides: Partial<ReceiptSummary> = {}): ReceiptSummary {
  return {
    receipt_id: "11111111-1111-4111-8111-111111111111",
    status: "needs_review",
    document_kind: "receipt",
    source_kind: "local_ocr",
    receipt_kind: "purchase",
    receipt_date: "2026-11-14",
    receipt_time: "12:15",
    raw_shop_name: "Synthetic Grocery",
    raw_nip: "5261040828",
    shop_id: null,
    chain_key: null,
    total_amount: "42.50",
    currency: "PLN",
    payment_method: "card",
    math_delta: "0",
    linked: false,
    linked_account_id: null,
    linked_entry_ref: null,
    version: 1,
    image_available: false,
    image_expires_at: null,
    raw_text_expires_at: null,
    invoice_number: null,
    due_date: null,
    import_expires_at: null,
    forwarded_manually: false,
    forwarded_sender_domain: null,
    created_at: 1_700_000_000,
    updated_at: 1_700_000_000,
    ...overrides,
  };
}

function receiptDetail(overrides: Partial<ReceiptDetail> = {}): ReceiptDetail {
  return {
    ...receiptSummary(),
    lines: [
      {
        line_no: 1,
        line_kind: "item",
        raw_name: "Oat Milk",
        raw_name_key: "oat milk",
        ptu_code: "A",
        quantity: "1",
        raw_unit: "szt",
        unit_price_raw: "12.50",
        gross_line_total: "12.50",
        discount_amount: "0",
        net_line_total: "12.50",
      },
      {
        line_no: 2,
        line_kind: "item",
        raw_name: REDACTED_VALUE,
        raw_name_key: REDACTED_VALUE,
        ptu_code: "B",
        quantity: "1",
        raw_unit: "szt",
        unit_price_raw: "30.00",
        gross_line_total: "30.00",
        discount_amount: "0",
        net_line_total: "30.00",
      },
    ],
    ...overrides,
  };
}

function receiptCandidate(overrides: Partial<ReceiptCandidate> = {}): ReceiptCandidate {
  return {
    account_id: "local-a",
    entry_reference: "a".repeat(64),
    amount: "42.50",
    currency: "PLN",
    credit_debit_indicator: "DBIT",
    booking_date: "2026-11-14",
    value_date: "2026-11-14",
    transaction_date: null,
    date_distance: 0,
    merchant_token_hit: true,
    candidate_token: "cand-token-1",
    ...overrides,
  };
}

const accounts = [
  account({
    id: "local-a",
    display: {
      product: "Synthetic Checking",
      currency: "PLN",
      masked_suffix: "1001",
      source: "offline_import",
      observed_at: 1,
      imported_at: 2,
    },
  }),
];

function fakeClients(rows: ReceiptSummary[] = [receiptSummary()], detail?: ReceiptDetail, candidates: ReceiptCandidate[] = []) {
  const rApi: ReceiptsApi = {
    receiptsList: vi.fn<ReceiptsApi["receiptsList"]>(() => ok<ReceiptListResult>({ receipts: rows, next_cursor: null })),
    receiptsSearch: vi.fn<ReceiptsApi["receiptsSearch"]>(() => ok<ReceiptListResult>({ receipts: [], next_cursor: null })),
    receiptDetail: vi.fn<ReceiptsApi["receiptDetail"]>(() =>
      ok<ReceiptDetailResult>({ receipt: detail ?? receiptDetail(), candidates })),
    receiptImage: vi.fn<ReceiptsApi["receiptImage"]>(() => ok(new Blob(["webp-data"], { type: "image/webp" }))),
    confirmReceipt: vi.fn<ReceiptsApi["confirmReceipt"]>((req) =>
      ok({
        ok: true,
        receipt: receiptDetail({
          receipt_id: req.receipt_id,
          status: "confirmed",
          raw_shop_name: req.raw_shop_name,
          raw_nip: req.raw_nip,
          receipt_date: req.receipt_date,
          receipt_time: req.receipt_time,
          total_amount: req.total_amount,
          currency: req.currency,
          payment_method: req.payment_method,
          linked: req.link !== null,
          linked_account_id: req.link === null ? null : req.link.account_id,
          linked_entry_ref: req.link === null ? null : req.link.entry_reference,
          version: req.expected_version + 1,
        }),
      })),
    deleteReceipt: vi.fn<ReceiptsApi["deleteReceipt"]>(() => ok({ ok: true, deleted: true })),
  };
  return { rApi };
}

let host: HTMLElement;
let root: Root;

beforeEach(async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  await loadLocale("en");
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(async () => {
  await act(async () => {
    root.unmount();
    await Promise.resolve();
  });
  host.remove();
  await loadLocale("en");
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function settle(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

interface RenderOpts {
  locale?: string;
  revision?: number;
  actorKey?: string;
  onAccessLoss?: (result: { ok: boolean; status?: number; error?: string }) => boolean;
}

async function renderReceipts(
  clients: ReturnType<typeof fakeClients>,
  options: RenderOpts = {},
) {
  const loc = options.locale ?? "en";
  await loadLocale(loc.startsWith("pl") ? "pl" : "en");
  const onAccessLoss = options.onAccessLoss
    ?? vi.fn((result: { ok: boolean; status?: number; error?: string }) =>
      !result.ok && (result.status === 401 || result.status === 403 || result.error === "budget_disabled"));
  await act(async () => {
    root.render(
      <ReceiptsView
        receiptsApi={clients.rApi}
        actorKey={options.actorKey ?? "owner"}
        accounts={accounts}
        connections={[connection]}
        locale={loc}
        revision={options.revision ?? 0}
        onAccessLoss={onAccessLoss}
      />,
    );
    await Promise.resolve();
  });
  await settle();
  return { onAccessLoss };
}

function button(text: string, scope: ParentNode = host): HTMLButtonElement {
  const found = [...scope.querySelectorAll("button")].find((item) => item.textContent === text);
  if (found === undefined) throw new Error(`button ${text} missing`);
  return found;
}

function dialog(scope: ParentNode = host): HTMLElement {
  const found = scope.querySelector<HTMLElement>('[role="dialog"]');
  if (found === null) throw new Error("dialog missing");
  return found;
}

function alertBox(scope: ParentNode = host): HTMLElement {
  const found = scope.querySelector<HTMLElement>('[role="alertdialog"]');
  if (found === null) throw new Error("alertdialog missing");
  return found;
}

function openDetailButton(index: number): HTMLButtonElement {
  const found = [...host.querySelectorAll("button")].filter((btn) => btn.textContent === "Details")[index];
  if (found === undefined) throw new Error(`Details button #${String(index)} missing`);
  return found;
}

function linkRadio(value: string, scope: ParentNode): HTMLInputElement {
  const found = scope.querySelector<HTMLInputElement>(`input[name="link_choice"][value="${value}"]`);
  if (found === null) throw new Error(`link radio ${value} missing`);
  return found;
}

function inputByName(name: string, scope: ParentNode = host): HTMLInputElement {
  const found = scope.querySelector<HTMLInputElement>(`input[name="${name}"]`);
  if (found === null) throw new Error(`input ${name} missing`);
  return found;
}

function selectByName(name: string, scope: ParentNode = host): HTMLSelectElement {
  const found = scope.querySelector<HTMLSelectElement>(`select[name="${name}"]`);
  if (found === null) throw new Error(`select ${name} missing`);
  return found;
}

async function click(element: HTMLElement): Promise<void> {
  await act(async () => {
    element.click();
    await Promise.resolve();
  });
  await settle();
}

async function typeInto(input: HTMLInputElement, value: string): Promise<void> {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    await Promise.resolve();
  });
}

async function choose(element: HTMLSelectElement, value: string): Promise<void> {
  await act(async () => {
    element.value = value;
    element.dispatchEvent(new Event("change", { bubbles: true }));
    await Promise.resolve();
  });
}

async function submit(form: HTMLFormElement | null): Promise<void> {
  await act(async () => {
    form?.requestSubmit();
    await Promise.resolve();
  });
  await settle();
}

describe("ReceiptsView", () => {
  it("lists receipts and documents, labels actual searchable fields, and localizes in Polish", async () => {
    const clients = fakeClients([
      receiptSummary({
        receipt_id: "rec-1",
        status: "needs_review",
        document_kind: "receipt",
        source_kind: "local_ocr",
        raw_shop_name: "Synthetic Grocery",
        total_amount: "42.50",
        currency: "PLN",
        linked: false,
      }),
      receiptSummary({
        receipt_id: "rec-2",
        status: "imported",
        document_kind: "invoice",
        source_kind: "attachment",
        raw_shop_name: null,
        forwarded_manually: true,
        forwarded_sender_domain: "billing.example.test",
        total_amount: "199.00",
        currency: "EUR",
        linked: true,
      }),
      receiptSummary({
        receipt_id: "rec-3",
        status: "confirmed",
        document_kind: "payment_confirmation",
        source_kind: "email",
        raw_shop_name: "Synthetic Energy",
        total_amount: "310.25",
        currency: "PLN",
        linked: true,
      }),
    ]);

    await renderReceipts(clients);
    expect(clients.rApi.receiptsList).toHaveBeenCalledWith({ limit: 50 });
    expect(inputByName("query").placeholder).toBe("Merchant, NIP or item");

    const renderedRows = [...host.querySelectorAll(".budget-receipt-row")].map((item) => item.textContent);
    expect(renderedRows).toHaveLength(3);
    expect(renderedRows[0]).toContain("Synthetic Grocery");
    expect(renderedRows[0]).toContain("42.50 PLN");
    expect(renderedRows[0]).toContain("Needs review");
    expect(renderedRows[0]).toContain("Unlinked");
    expect(renderedRows[1]).toContain("billing.example.test");
    expect(renderedRows[1]).toContain("199.00 EUR");
    expect(renderedRows[1]).toContain("Invoice");
    expect(renderedRows[1]).toContain("Imported");
    expect(renderedRows[1]).toContain("Linked");
    expect(renderedRows[2]).toContain("Payment confirmation");
    expect(host.querySelector('[data-source-kind="local_ocr"] svg')).not.toBeNull();
    expect(host.querySelector('[data-source-kind="attachment"] svg')).not.toBeNull();
    expect(host.querySelector('[data-source-kind="email"] svg')).not.toBeNull();

    await renderReceipts(clients, { locale: "pl" });
    expect(host.textContent).toContain("Paragony i dowody");
    expect(inputByName("query").placeholder).toBe("Sklep, NIP lub pozycja");
    expect(host.textContent).toContain("Do przeglądu");
    expect(host.textContent).toContain("Potwierdzenie płatności");
    expect(host.textContent).toContain("Powiązany");
  });

  it("applies server filters, client-side hints, search API, cursor pagination, and recovers once from expired cursors", async () => {
    const clients = fakeClients([
      receiptSummary({ receipt_id: "p1-a", status: "needs_review", document_kind: "invoice", source_kind: "attachment", receipt_date: "2026-11-10", linked: true }),
      receiptSummary({ receipt_id: "p1-b", status: "needs_review", document_kind: "invoice", source_kind: "email", receipt_date: "2026-11-10", linked: true }),
      receiptSummary({ receipt_id: "p1-c", status: "needs_review", document_kind: "invoice", source_kind: "attachment", receipt_date: "2026-11-28", linked: true }),
    ]);
    vi.mocked(clients.rApi.receiptsList)
      .mockImplementationOnce(() => ok<ReceiptListResult>({ receipts: [receiptSummary()], next_cursor: null }))
      .mockImplementationOnce(() => ok<ReceiptListResult>({
        receipts: [
          receiptSummary({ receipt_id: "p1-a", status: "needs_review", document_kind: "invoice", source_kind: "attachment", receipt_date: "2026-11-10", linked: true }),
          receiptSummary({ receipt_id: "p1-b", status: "needs_review", document_kind: "invoice", source_kind: "email", receipt_date: "2026-11-10", linked: true }),
          receiptSummary({ receipt_id: "p1-c", status: "needs_review", document_kind: "invoice", source_kind: "attachment", receipt_date: "2026-11-28", linked: true }),
        ],
        next_cursor: "cursor-page-2",
      }))
      .mockImplementationOnce(() => ok<ReceiptListResult>({
        receipts: [
          receiptSummary({ receipt_id: "p2-a", status: "needs_review", document_kind: "invoice", source_kind: "attachment", receipt_date: "2026-11-12", raw_shop_name: "Second Page Vendor", linked: true }),
        ],
        next_cursor: "cursor-expired",
      }))
      .mockImplementationOnce(() => fail(400, "budget_receipt_invalid_request"))
      .mockImplementationOnce(() => ok<ReceiptListResult>({
        receipts: [
          receiptSummary({ receipt_id: "reloaded-p1", status: "needs_review", document_kind: "invoice", source_kind: "attachment", receipt_date: "2026-11-10", raw_shop_name: "Fresh Page 1 Vendor", linked: true }),
        ],
        next_cursor: "cursor-second-bad",
      }))
      .mockImplementationOnce(() => fail(400, "budget_receipt_invalid_request"))
      .mockImplementationOnce(() => ok<ReceiptListResult>({ receipts: [receiptSummary()], next_cursor: null }));

    vi.mocked(clients.rApi.receiptsSearch).mockImplementationOnce(() => ok<ReceiptListResult>({
      receipts: [
        receiptSummary({ receipt_id: "s-1", status: "confirmed", source_kind: "photo", raw_shop_name: "Lidl Warszawa" }),
        receiptSummary({ receipt_id: "s-2", status: "needs_review", source_kind: "attachment", raw_shop_name: "Lidl Faktura", receipt_date: "2026-11-11", linked: true, document_kind: "invoice" }),
      ],
      next_cursor: null,
    }));

    await renderReceipts(clients);
    expect(host.textContent).not.toContain("Filtering loaded pages only");

    await choose(selectByName("status"), "needs_review");
    await choose(selectByName("document_kind"), "invoice");
    await choose(selectByName("source_kind"), "attachment");
    await choose(selectByName("linked"), "linked");
    await typeInto(inputByName("date_from"), "2026-11-05");
    await typeInto(inputByName("date_to"), "2026-11-20");
    await submit(host.querySelector(".budget-range"));

    expect(clients.rApi.receiptsList).toHaveBeenNthCalledWith(2, {
      limit: 50,
      month: "2026-11",
      status: "needs_review",
      document_kind: "invoice",
      linked: true,
    });
    expect(host.querySelectorAll(".budget-receipt-row")).toHaveLength(1);
    expect(host.textContent).toContain("Filtering loaded pages only — load more to check older entries.");

    await click(button("Load more"));
    expect(clients.rApi.receiptsList).toHaveBeenNthCalledWith(3, {
      limit: 50,
      month: "2026-11",
      status: "needs_review",
      document_kind: "invoice",
      linked: true,
      cursor: "cursor-page-2",
    });
    expect(host.textContent).toContain("Second Page Vendor");

    await click(button("Load more"));
    expect(host.textContent).toContain("Page cursor expired — reloaded from page 1.");
    expect(host.textContent).toContain("Fresh Page 1 Vendor");

    await click(button("Load more"));
    expect(host.textContent).toContain("Receipts could not be loaded.");
    expect([...host.querySelectorAll("button")].some((b) => b.textContent === "Load more")).toBe(false);

    await typeInto(inputByName("query"), "Lidl");
    await submit(host.querySelector(".budget-range"));
    expect(clients.rApi.receiptsSearch).toHaveBeenCalledWith({ query: "Lidl", limit: 50 });
    expect(host.textContent).toContain("Lidl Faktura");
    expect(host.textContent).not.toContain("Lidl Warszawa");

    await click(button("Reset filters"));
    expect(host.textContent).not.toContain("Filtering loaded pages only");
    expect(clients.rApi.receiptsList).toHaveBeenLastCalledWith({ limit: 50 });
  });

  it("shows link state without GET lookups, hides image preview cleanly on 404, and revokes valid image blobs", async () => {
    const createUrlSpy = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:synthetic-receipt");
    const revokeUrlSpy = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);

    const clients = fakeClients([
      receiptSummary({ receipt_id: "rec-no-img", image_available: false, linked: true }),
      receiptSummary({ receipt_id: "rec-img-404", image_available: true, linked: true, raw_shop_name: "Missing Scan Market" }),
      receiptSummary({ receipt_id: "rec-with-img", image_available: true, linked: true, raw_shop_name: "Image Market" }),
    ]);

    const storageGetSpy = vi.spyOn(Storage.prototype, "getItem");
    const storageSetSpy = vi.spyOn(Storage.prototype, "setItem");

    vi.mocked(clients.rApi.receiptDetail).mockImplementationOnce(() => ok({
      receipt: receiptDetail({
        receipt_id: "rec-no-img",
        image_available: false,
        linked: true,
        linked_account_id: "local-a",
        linked_entry_ref: "a".repeat(64),
        payment_method: "blik",
        invoice_number: "FV/2026/11",
        due_date: "2026-11-30",
        math_delta: "1.00",
      }),
      candidates: [],
    }));

    await renderReceipts(clients);
    await click(openDetailButton(0));

    expect(clients.rApi.receiptImage).not.toHaveBeenCalled();
    const drawer = dialog();
    expect(drawer.textContent).toContain("FV/2026/11");
    expect(drawer.textContent).toContain("BLIK");
    expect(drawer.textContent).toContain("Oat Milk");
    expect(drawer.textContent).toContain("Linked to this account; bank counterparty details stay on the ledger.");
    expect(drawer.querySelector(".budget-redacted")?.textContent).toBe("Removed");
    expect(drawer.textContent).not.toContain(REDACTED_VALUE);
    await click(button("Close", drawer));

    vi.mocked(clients.rApi.receiptDetail).mockImplementationOnce(() => ok({
      receipt: receiptDetail({ receipt_id: "rec-img-404", raw_shop_name: "Missing Scan Market", image_available: true }),
      candidates: [],
    }));
    vi.mocked(clients.rApi.receiptImage).mockImplementationOnce(() => fail(404, "budget_receipt_not_found"));

    await click(openDetailButton(1));
    const drawer404 = dialog();
    expect(drawer404.textContent).toContain("Missing Scan Market");
    expect(drawer404.querySelector("img.budget-receipt-image")).toBeNull();
    expect(drawer404.textContent).not.toContain("Image preview is unavailable.");
    expect(drawer404.textContent).not.toContain("Receipt image");
    await click(button("Close", drawer404));

    vi.mocked(clients.rApi.receiptDetail).mockImplementationOnce(() => ok({
      receipt: receiptDetail({
        receipt_id: "rec-with-img",
        image_available: true,
        linked: true,
        linked_account_id: "local-a",
        linked_entry_ref: "b".repeat(64),
      }),
      candidates: [],
    }));

    await click(openDetailButton(2));
    expect(clients.rApi.receiptImage).toHaveBeenCalledWith("rec-with-img");
    expect(createUrlSpy).toHaveBeenCalledTimes(1);
    const img = host.querySelector<HTMLImageElement>("img.budget-receipt-image");
    expect(img?.getAttribute("src")).toBe("blob:synthetic-receipt");
    await click(button("Close", dialog()));
    expect(revokeUrlSpy).toHaveBeenCalledWith("blob:synthetic-receipt");
    expect(storageGetSpy).not.toHaveBeenCalled();
    expect(storageSetSpy).not.toHaveBeenCalled();
  });

  it("never implicitly unlinks linked receipts, disables stale tokens on field edits before submit, and handles server 409 stale_source", async () => {
    const candKeep = receiptCandidate({ entry_reference: "a".repeat(64), candidate_token: "tok-keep" });
    const candAlt = receiptCandidate({
      entry_reference: "b".repeat(64),
      candidate_token: "tok-alt",
      date_distance: 2,
      booking_date: "2026-11-16",
      merchant_token_hit: false,
    });
    const clients = fakeClients([
      receiptSummary({ receipt_id: "rec-confirm", status: "needs_review" }),
    ]);

    // 1. Linked needs_review WITHOUT keep token -> Confirm is disabled until user explicitly selects Unlink.
    vi.mocked(clients.rApi.receiptDetail).mockImplementationOnce(() => ok({
      receipt: receiptDetail({
        receipt_id: "rec-confirm",
        status: "needs_review",
        linked: true,
        linked_account_id: "local-a",
        linked_entry_ref: "c".repeat(64),
        version: 2,
      }),
      candidates: [],
    }));

    await renderReceipts(clients);
    await click(button("Details"));
    const d1 = dialog();
    expect(d1.textContent).toContain("No matching keep token is available for this link");
    expect(linkRadio("unlink", d1).checked).toBe(false);
    expect(button("Confirm receipt", d1).disabled).toBe(true);
    await submit(d1.querySelector("form.budget-receipt-confirm"));
    expect(clients.rApi.confirmReceipt).not.toHaveBeenCalled();

    await click(linkRadio("unlink", d1));
    expect(button("Confirm receipt", d1).disabled).toBe(false);
    await click(button("Confirm receipt", d1));
    expect(clients.rApi.confirmReceipt).toHaveBeenLastCalledWith(expect.objectContaining({ link: null }));

    // 2. Confirmed linked receipt also requires explicit Unlink before Confirm becomes active.
    vi.mocked(clients.rApi.receiptDetail).mockImplementationOnce(() => ok({
      receipt: receiptDetail({
        receipt_id: "rec-confirm",
        status: "confirmed",
        linked: true,
        linked_account_id: "local-a",
        linked_entry_ref: candKeep.entry_reference,
        version: 3,
      }),
      candidates: [],
    }));
    await click(button("Close", dialog()));
    await click(button("Details"));
    const d2 = dialog();
    expect(d2.textContent).toContain("Confirmed receipts cannot keep or choose candidates on edit");
    expect(linkRadio("unlink", d2).checked).toBe(false);
    expect(button("Confirm receipt", d2).disabled).toBe(true);
    await click(linkRadio("unlink", d2));
    expect(button("Confirm receipt", d2).disabled).toBe(false);

    // 3. Editing matching fields on linked needs_review disables Keep BEFORE submit, never sends stale token or implicit unlink.
    vi.mocked(clients.rApi.receiptDetail).mockImplementationOnce(() => ok({
      receipt: receiptDetail({
        receipt_id: "rec-confirm",
        status: "needs_review",
        linked: true,
        linked_account_id: candKeep.account_id,
        linked_entry_ref: candKeep.entry_reference,
        version: 4,
      }),
      candidates: [candKeep, candAlt],
    }));
    await click(button("Close", dialog()));
    await click(button("Details"));
    const d3 = dialog();
    expect(linkRadio("keep", d3).checked).toBe(true);
    await typeInto(inputByName("raw_shop_name", d3), "Edited Store Before Submit");
    expect(d3.textContent).toContain("Editing merchant, NIP, amount, date, currency or type invalidates candidate tokens");
    expect(linkRadio("keep", d3).disabled).toBe(true);
    expect(linkRadio("keep", d3).checked).toBe(false);
    expect(linkRadio("unlink", d3).checked).toBe(false);
    expect(button("Confirm receipt", d3).disabled).toBe(true);

    // Reverting merchant restores Keep; editing NIP or amount also disables tokens immediately.
    await typeInto(inputByName("raw_shop_name", d3), "Synthetic Grocery");
    expect(linkRadio("keep", d3).disabled).toBe(false);
    await click(linkRadio("keep", d3));
    await typeInto(inputByName("total_amount", d3), "99.99");
    expect(linkRadio("keep", d3).disabled).toBe(true);
    expect(linkRadio("candidate:1", d3).disabled).toBe(true);
    expect(button("Confirm receipt", d3).disabled).toBe(true);
    expect(clients.rApi.confirmReceipt).toHaveBeenCalledTimes(1);

    // 4. Unlinked needs_review with candidate: editing NIP disables candidate and sets linkChoice to confirm without link.
    vi.mocked(clients.rApi.receiptDetail)
      .mockImplementationOnce(() => ok({
        receipt: receiptDetail({ receipt_id: "rec-confirm", status: "needs_review", linked: false, version: 5 }),
        candidates: [candAlt],
      }))
      // Subsequent concurrent ledger 409 stale_source test reloads detail cleanly.
      .mockImplementationOnce(() => ok({
        receipt: receiptDetail({ receipt_id: "rec-confirm", status: "needs_review", linked: false, version: 5 }),
        candidates: [candAlt],
      }))
      .mockImplementationOnce(() => ok({
        receipt: receiptDetail({ receipt_id: "rec-confirm", status: "needs_review", linked: false, version: 5 }),
        candidates: [receiptCandidate({ entry_reference: "b".repeat(64), candidate_token: "tok-refreshed" })],
      }));

    await click(button("Close", dialog()));
    await click(button("Details"));
    const d4 = dialog();
    expect(linkRadio("candidate:0", d4).checked).toBe(true);
    await typeInto(inputByName("raw_nip", d4), "5261040829");
    expect(linkRadio("candidate:0", d4).disabled).toBe(true);
    expect(linkRadio("unlink", d4).checked).toBe(true);
    expect(d4.textContent).toContain("Editing merchant, NIP, amount, date, currency or type invalidates candidate tokens");

    // 5. Server 409 budget_receipt_stale_source (ledger update while fields untouched) reloads detail and explains.
    await click(button("Close", dialog()));
    await click(button("Details"));
    vi.mocked(clients.rApi.confirmReceipt).mockImplementationOnce(() => fail(409, "budget_receipt_stale_source"));
    await click(button("Confirm receipt", dialog()));
    expect(dialog().textContent).toContain("The candidate link token is no longer valid");
    await click(button("Confirm receipt", dialog()));
    expect(clients.rApi.confirmReceipt).toHaveBeenLastCalledWith(expect.objectContaining({
      link: expect.objectContaining({ candidate_token: "tok-refreshed" }) as unknown,
    }));
  });

  it("ignores stale detail responses older than the latest Confirm version and clears detailState when closed after 404", async () => {
    const clients = fakeClients([
      receiptSummary({ receipt_id: "rec-a", raw_shop_name: "Secret Store A", version: 5 }),
      receiptSummary({ receipt_id: "rec-b", raw_shop_name: "Receipt B", version: 1 }),
    ]);

    const slowDetailReload = deferred<ReceiptDetailResult>();
    vi.mocked(clients.rApi.receiptDetail)
      .mockImplementationOnce(() => ok({ receipt: receiptDetail({ receipt_id: "rec-a", raw_shop_name: "Secret Store A", version: 5 }), candidates: [] }))
      .mockImplementationOnce(() => slowDetailReload.promise);

    await renderReceipts(clients, { revision: 1 });
    await click(openDetailButton(0));
    expect(dialog().textContent).toContain("Version 5");

    // Simulate a Budget reload bumping revision while Confirm starts: slowDetailReload (v5) is in flight.
    await renderReceipts(clients, { revision: 2 });
    await click(button("Confirm receipt", dialog()));
    expect(dialog().textContent).toContain("Version 6");

    // Now the stale reload finishes with Version 5 — it MUST NOT overwrite saved Version 6.
    await act(async () => {
      slowDetailReload.resolve({
        ok: true,
        data: { receipt: receiptDetail({ receipt_id: "rec-a", raw_shop_name: "Secret Store A", version: 5 }), candidates: [] },
      });
      await Promise.resolve();
    });
    await settle();
    expect(dialog().textContent).toContain("Version 6");
    expect(dialog().textContent).not.toContain("Version 5");

    await click(button("Confirm receipt", dialog()));
    expect(clients.rApi.confirmReceipt).toHaveBeenLastCalledWith(expect.objectContaining({ expected_version: 6 }));

    // Close via 404 on Confirm -> clears detailState cleanly so opening Receipt B never flashes Secret Store A.
    vi.mocked(clients.rApi.confirmReceipt).mockImplementationOnce(() => fail(404, "budget_receipt_not_found"));
    await click(button("Confirm receipt", dialog()));
    expect(host.querySelector('[role="dialog"]')).toBeNull();

    const loadingB = deferred<ReceiptDetailResult>();
    vi.mocked(clients.rApi.receiptDetail).mockImplementationOnce(() => loadingB.promise);
    await click(openDetailButton(1));
    const drawerB = dialog();
    expect(drawerB.textContent).toContain("Loading document details");
    expect(drawerB.textContent).not.toContain("Secret Store A");

    await act(async () => {
      loadingB.resolve({
        ok: true,
        data: { receipt: receiptDetail({ receipt_id: "rec-b", raw_shop_name: "Receipt B", version: 1 }), candidates: [] },
      });
      await Promise.resolve();
    });
    await settle();
    expect(dialog().textContent).toContain("Receipt B");
  });

  it("keeps mutation locks across panel unmount/remount, prevents double-submit, and respects confirmation dialog", async () => {
    const clients = fakeClients([
      receiptSummary({ receipt_id: "rec-a", raw_shop_name: "Receipt A", version: 7 }),
      receiptSummary({ receipt_id: "rec-b", raw_shop_name: "Receipt B", version: 2 }),
    ]);
    vi.mocked(clients.rApi.receiptDetail).mockImplementation((id) => ok({
      receipt: receiptDetail({ receipt_id: id, raw_shop_name: id === "rec-a" ? "Receipt A" : "Receipt B", version: 7 }),
      candidates: [],
    }));

    const pendingConfirm = deferred<{ ok: true; receipt: ReceiptDetail }>();
    vi.mocked(clients.rApi.confirmReceipt).mockImplementationOnce(() => pendingConfirm.promise);

    await renderReceipts(clients);
    await click(openDetailButton(0));
    const drawer = dialog();

    const form = drawer.querySelector<HTMLFormElement>("form.budget-receipt-confirm");
    await act(async () => {
      form?.requestSubmit();
      form?.requestSubmit();
      await Promise.resolve();
    });
    expect(clients.rApi.confirmReceipt).toHaveBeenCalledTimes(1);
    expect(openDetailButton(1).disabled).toBe(true);

    // Unmount and remount ReceiptsView with the same rApi -> lock on rec-a survives remount until settled!
    await act(async () => {
      root.unmount();
      root = createRoot(host);
      await Promise.resolve();
    });
    await renderReceipts(clients);
    expect(host.textContent).toContain("Confirming…");
    await click(openDetailButton(0));
    expect(button("Confirming…", dialog()).disabled).toBe(true);
    await submit(dialog().querySelector<HTMLFormElement>("form.budget-receipt-confirm"));
    expect(clients.rApi.confirmReceipt).toHaveBeenCalledTimes(1);

    await act(async () => {
      pendingConfirm.resolve({
        ok: true,
        data: { ok: true, receipt: receiptDetail({ receipt_id: "rec-a", status: "confirmed", version: 8 }) },
      });
      await Promise.resolve();
    });
    await settle();
    expect(openDetailButton(1).disabled).toBe(false);

    // Delete with confirmation dialog & double-click guard.
    const pendingDelete = deferred<{ ok: true; deleted: true }>();
    vi.mocked(clients.rApi.deleteReceipt).mockImplementationOnce(() => pendingDelete.promise);

    await click(button("Delete document", dialog()));
    const confirmBox = alertBox(dialog());
    await click(button("Cancel", confirmBox));
    expect(dialog().querySelector('[role="alertdialog"]')).toBeNull();

    await click(button("Delete document", dialog()));
    const delConfirmBtn = button("Confirm deletion", dialog());
    await act(async () => {
      delConfirmBtn.click();
      delConfirmBtn.click();
      await Promise.resolve();
    });
    expect(clients.rApi.deleteReceipt).toHaveBeenCalledTimes(1);

    await act(async () => {
      pendingDelete.resolve({ ok: true, data: { ok: true, deleted: true } });
      await Promise.resolve();
    });
    await settle();
    expect(host.querySelector('[role="dialog"]')).toBeNull();
  });

  it("propagates 401/403 even after unmounting, clears search text on 404 disable, and discards stale epochs", async () => {
    const revokeUrlSpy = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:temp-img");

    const clients = fakeClients([receiptSummary({ receipt_id: "rec-auth", image_available: true })]);
    const pendingList = deferred<ReceiptListResult>();

    vi.mocked(clients.rApi.receiptDetail).mockImplementationOnce(() => ok({
      receipt: receiptDetail({ receipt_id: "rec-auth", image_available: true }),
      candidates: [],
    }));

    const { onAccessLoss } = await renderReceipts(clients);
    await click(button("Details"));
    expect(host.querySelector("img.budget-receipt-image")).not.toBeNull();

    vi.mocked(clients.rApi.confirmReceipt).mockImplementationOnce(() => fail(403, "budget_forbidden"));
    await click(button("Confirm receipt"));
    expect(onAccessLoss).toHaveBeenCalled();
    expect(host.querySelector('[role="dialog"]')).toBeNull();
    expect(revokeUrlSpy).toHaveBeenCalledWith("blob:temp-img");

    vi.mocked(clients.rApi.receiptsList)
      .mockImplementationOnce(() => pendingList.promise)
      .mockImplementationOnce(() => ok<ReceiptListResult>({
        receipts: [receiptSummary({ receipt_id: "rec-fresh", raw_shop_name: "Fresh Epoch Store" })],
        next_cursor: null,
      }));
    await renderReceipts(clients, { revision: 1 });
    await renderReceipts(clients, { revision: 2 });
    expect(host.textContent).toContain("Fresh Epoch Store");
    await act(async () => {
      pendingList.resolve({
        ok: true,
        data: { receipts: [receiptSummary({ receipt_id: "rec-stale", raw_shop_name: "Stale Epoch Store" })], next_cursor: null },
      });
      await Promise.resolve();
    });
    await settle();
    expect(host.textContent).toContain("Fresh Epoch Store");
    expect(host.textContent).not.toContain("Stale Epoch Store");

    vi.mocked(clients.rApi.receiptsSearch).mockImplementationOnce(() => fail(404, "budget_receipts_disabled"));
    await typeInto(inputByName("query"), "Sensitive Search");
    await submit(host.querySelector(".budget-range"));
    expect(host.textContent).toContain("Receipts are disabled on this server");
    expect(host.querySelectorAll(".budget-receipt-row")).toHaveLength(0);

    vi.mocked(clients.rApi.receiptsList).mockImplementationOnce(() => ok<ReceiptListResult>({ receipts: [], next_cursor: null }));
    await renderReceipts(clients, { revision: 3 });
    expect(inputByName("query").value).toBe("");
    expect(clients.rApi.receiptsList).toHaveBeenLastCalledWith({ limit: 50 });

    const unmountedRead = deferred<ReceiptListResult>();
    const accessLossUnmount = vi.fn(() => true);
    vi.mocked(clients.rApi.receiptsList).mockImplementationOnce(() => unmountedRead.promise);
    await renderReceipts(clients, { revision: 4, onAccessLoss: accessLossUnmount });
    await act(async () => {
      root.unmount();
      root = createRoot(host);
      unmountedRead.resolve({ ok: false, status: 401, error: "budget_http_401" });
      await Promise.resolve();
    });
    expect(accessLossUnmount).toHaveBeenCalledWith({ ok: false, status: 401, error: "budget_http_401" });
  });

  it("preserves discount when net line amount is reformatted or edited, and supports gross/discount/net editing", async () => {
    const clients = fakeClients([
      receiptSummary({ receipt_id: "rec-disc", status: "needs_review", total_amount: "9.00", linked: false }),
    ]);
    vi.mocked(clients.rApi.receiptDetail).mockImplementation(() => ok({
      receipt: receiptDetail({
        receipt_id: "rec-disc",
        status: "needs_review",
        total_amount: "9.00",
        linked: false,
        lines: [{
          line_no: 1,
          line_kind: "item",
          raw_name: "Roasted Coffee",
          raw_name_key: "roasted coffee",
          ptu_code: "A",
          quantity: "1",
          raw_unit: "szt",
          unit_price_raw: "10.00",
          gross_line_total: "10.00",
          discount_amount: "1.00",
          net_line_total: "9.00",
        }],
      }),
      candidates: [],
    }));

    await renderReceipts(clients);
    await click(openDetailButton(0));
    const drawer = dialog();

    // Reformatting net "9.00" -> "9," -> "9,00" must preserve gross 10 and discount 1 on save.
    await typeInto(inputByName("line_net", drawer), "9,");
    await typeInto(inputByName("line_net", drawer), "9,00");
    expect(inputByName("line_gross", drawer).value).toBe("10.00");
    expect(inputByName("line_discount", drawer).value).toBe("1.00");

    await click(button("Confirm receipt", drawer));
    expect(clients.rApi.confirmReceipt).toHaveBeenCalledWith(expect.objectContaining({
      lines: [expect.objectContaining({
        gross_line_total: "10",
        discount_amount: "1",
        net_line_total: "9",
      }) as unknown],
    }));

    // Reopen and edit net -> discount stays 1, gross updates cleanly; then edit discount -> net updates cleanly.
    await click(button("Close", dialog()));
    await click(openDetailButton(0));
    const drawer2 = dialog();
    await typeInto(inputByName("line_net", drawer2), "8.00");
    expect(inputByName("line_gross", drawer2).value).toBe("9");
    expect(inputByName("line_discount", drawer2).value).toBe("1.00");

    await typeInto(inputByName("line_gross", drawer2), "12.00");
    expect(inputByName("line_discount", drawer2).value).toBe("1.00");
    expect(inputByName("line_net", drawer2).value).toBe("11");

    await typeInto(inputByName("line_discount", drawer2), "2.50");
    expect(inputByName("line_gross", drawer2).value).toBe("12.00");
    expect(inputByName("line_net", drawer2).value).toBe("9.5");

    await typeInto(inputByName("total_amount", drawer2), "9.50");
    await click(button("Confirm receipt", drawer2));
    expect(clients.rApi.confirmReceipt).toHaveBeenLastCalledWith(expect.objectContaining({
      total_amount: "9.5",
      lines: [expect.objectContaining({
        gross_line_total: "12",
        discount_amount: "2.5",
        net_line_total: "9.5",
      }) as unknown],
    }));
  });
});
