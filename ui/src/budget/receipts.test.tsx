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
    gateway: null,
    gateway_status: null,
    fee_amount: null,
    payee_host: null,
    evidence_for_receipt_id: null,
    has_document: true,
    created_at: 1_700_000_000,
    updated_at: 1_700_000_000,
    ...overrides,
  };
}

function receiptDetail(overrides: Partial<ReceiptDetail> = {}): ReceiptDetail {
  return {
    ...receiptSummary(),
    payment_evidence: [],
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
          ...(req.raw_shop_name === undefined ? {} : { raw_shop_name: req.raw_shop_name }),
          ...(req.raw_nip === undefined ? {} : { raw_nip: req.raw_nip }),
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
    expect(d1.textContent).toContain("Explicitly select Unlink transaction before confirming");
    expect(linkRadio("unlink", d1).checked).toBe(false);
    expect(button("Confirm receipt", d1).disabled).toBe(true);
    await submit(d1.querySelector("form.budget-receipt-confirm"));
    expect(clients.rApi.confirmReceipt).not.toHaveBeenCalled();

    await click(linkRadio("unlink", d1));
    expect(button("Confirm receipt", d1).disabled).toBe(false);
    await click(button("Confirm receipt", d1));
    expect(clients.rApi.confirmReceipt).toHaveBeenLastCalledWith(expect.objectContaining({ link: null }));
    expect("lines" in (vi.mocked(clients.rApi.confirmReceipt).mock.calls.at(-1)?.[0] ?? {})).toBe(false);

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

    // 3. Linked needs_review never preselects Keep; after user chooses Keep, editing fields disables it and blocks Confirm.
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
    expect(d3.textContent).toContain("Choose whether to keep the link, change it or unlink");
    expect(d3.textContent).not.toContain("Explicitly select Unlink transaction");
    expect(linkRadio("keep", d3).checked).toBe(false);
    expect(linkRadio("unlink", d3).checked).toBe(false);
    expect(button("Confirm receipt", d3).disabled).toBe(true);
    await click(linkRadio("keep", d3));
    expect(button("Confirm receipt", d3).disabled).toBe(false);
    await typeInto(inputByName("raw_shop_name", d3), "Edited Store Before Submit");
    expect(d3.textContent).toContain("Editing merchant, NIP, amount, date, currency or type invalidates candidate tokens");
    expect(d3.textContent).toContain("Explicitly select Unlink transaction");
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

    // 4. Unlinked needs_review with candidate never preselects candidate:0 or unlink; editing NIP clears choice & disables Confirm.
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
    expect(linkRadio("candidate:0", d4).checked).toBe(false);
    expect(linkRadio("unlink", d4).checked).toBe(false);
    expect(button("Confirm receipt", d4).disabled).toBe(true);
    await click(linkRadio("candidate:0", d4));
    expect(button("Confirm receipt", d4).disabled).toBe(false);
    await typeInto(inputByName("raw_nip", d4), "5261040829");
    expect(linkRadio("candidate:0", d4).disabled).toBe(true);
    expect(linkRadio("candidate:0", d4).checked).toBe(false);
    expect(linkRadio("unlink", d4).checked).toBe(false);
    expect(button("Confirm receipt", d4).disabled).toBe(true);
    expect(d4.textContent).toContain("Editing merchant, NIP, amount, date, currency or type invalidates candidate tokens");

    // 5. Server 409 budget_receipt_stale_source (ledger update while fields untouched) reloads detail and explains.
    await click(button("Close", dialog()));
    await click(button("Details"));
    await click(linkRadio("candidate:0", dialog()));
    vi.mocked(clients.rApi.confirmReceipt).mockImplementationOnce(() => fail(409, "budget_receipt_stale_source"));
    await click(button("Confirm receipt", dialog()));
    expect(dialog().textContent).toContain("The candidate link token is no longer valid");
    expect(button("Confirm receipt", dialog()).disabled).toBe(true);
    await click(linkRadio("candidate:0", dialog()));
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

    // Reformatting net "9.00" -> "9," -> "9,00" preserves gross 10 and discount 1 and omits `lines` since decimal value is unchanged.
    await typeInto(inputByName("line_net", drawer), "9,");
    await typeInto(inputByName("line_net", drawer), "9,00");
    expect(inputByName("line_gross", drawer).value).toBe("10.00");
    expect(inputByName("line_discount", drawer).value).toBe("1.00");

    await click(button("Confirm receipt", drawer));
    const reformatCall = vi.mocked(clients.rApi.confirmReceipt).mock.calls[0]?.[0];
    expect(reformatCall).toMatchObject({ total_amount: "9", link: null });
    expect("lines" in (reformatCall ?? {})).toBe(false);

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

    // Entering the redacted placeholder into an editable line is blocked before submit.
    await click(button("Close", dialog()));
    await click(openDetailButton(0));
    const drawer3 = dialog();
    await typeInto(inputByName("line_name", drawer3), REDACTED_VALUE);
    await click(button("Confirm receipt", drawer3));
    expect(drawer3.textContent).toContain("Line editing is disabled because one or more lines contain redacted data.");
    expect(clients.rApi.confirmReceipt).toHaveBeenCalledTimes(2);
  });

  it("disables line editing with a hint and omits lines when confirming a receipt containing redacted lines", async () => {
    const clients = fakeClients(
      [receiptSummary({ receipt_id: "rec-red", status: "needs_review", total_amount: "42.50", linked: false })],
      receiptDetail({ receipt_id: "rec-red", status: "needs_review", total_amount: "42.50", linked: false }),
    );
    await renderReceipts(clients);
    await click(openDetailButton(0));
    const drawer = dialog();

    expect(drawer.textContent).toContain("Line editing is disabled because one or more lines contain redacted data.");
    expect(button("Add item", drawer).disabled).toBe(true);
    for (const field of ["line_name", "line_gross", "line_discount", "line_net"]) {
      for (const el of drawer.querySelectorAll<HTMLInputElement>(`input[name="${field}"]`)) {
        expect(el.disabled).toBe(true);
      }
    }
    for (const btn of [...drawer.querySelectorAll("button")].filter((b) => b.textContent === "Remove")) {
      expect(btn.disabled).toBe(true);
    }

    await typeInto(inputByName("raw_shop_name", drawer), "Updated Safe Shop");
    await click(button("Confirm receipt", drawer));
    const sent = vi.mocked(clients.rApi.confirmReceipt).mock.calls[0]?.[0];
    expect(sent).toMatchObject({ receipt_id: "rec-red", raw_shop_name: "Updated Safe Shop" });
    expect("lines" in (sent ?? {})).toBe(false);
    expect("raw_nip" in (sent ?? {})).toBe(false);
  });

  it("omits redacted merchant name in bulk confirm instead of echoing [usunięto]", async () => {
    const redactedImportRow = receiptSummary({
      receipt_id: "imp-redacted-shop",
      status: "imported",
      source_kind: "email",
      document_kind: "payment_confirmation",
      raw_shop_name: REDACTED_VALUE,
      raw_nip: "7791011327",
      total_amount: "30.00",
      version: 4,
    });
    const clients = fakeClients([redactedImportRow]);
    vi.mocked(clients.rApi.receiptDetail).mockImplementationOnce(() => ok({
      receipt: receiptDetail({
        receipt_id: "imp-redacted-shop",
        status: "imported",
        source_kind: "email",
        document_kind: "payment_confirmation",
        raw_shop_name: REDACTED_VALUE,
        raw_nip: "7791011327",
        payment_method: null,
        total_amount: "30.00",
        lines: [{
          line_no: 1,
          line_kind: "other",
          raw_name: REDACTED_VALUE,
          raw_name_key: "[usunieto]",
          ptu_code: null,
          quantity: "1",
          raw_unit: null,
          unit_price_raw: null,
          gross_line_total: "30.00",
          discount_amount: "0",
          net_line_total: "30.00",
        }],
        version: 4,
      }),
      candidates: [],
    }));
    await renderReceipts(clients);
    await click(button("Confirm all from: NIP 7791011327"));
    await click(button("Start bulk confirm", alertBox()));
    const payload = vi.mocked(clients.rApi.confirmReceipt).mock.calls[0]?.[0];
    expect(payload).toMatchObject({ receipt_id: "imp-redacted-shop", expected_version: 4 });
    expect("raw_shop_name" in (payload ?? {})).toBe(false);
    expect("raw_nip" in (payload ?? {})).toBe(false);
    expect("lines" in (payload ?? {})).toBe(false);
  });

  it("restricts bulk confirm to Gmail imports, separates same-name sellers by NIP, omits unedited lines, handles failures/cancel/actor aborts", async () => {
    const rows = [
      receiptSummary({ receipt_id: "imp-1", status: "imported", source_kind: "attachment", document_kind: "invoice", raw_shop_name: "Acme Power", raw_nip: "7791011327", total_amount: "50.00", version: 2 }),
      receiptSummary({ receipt_id: "imp-2", status: "imported", source_kind: "email", document_kind: "payment_confirmation", raw_shop_name: "Acme Power", raw_nip: "7791011327", total_amount: "30.00", version: 5 }),
      receiptSummary({ receipt_id: "imp-other-nip", status: "imported", source_kind: "email", document_kind: "payment_confirmation", raw_shop_name: "Acme Power", raw_nip: "5261040828", total_amount: "45.00", version: 3 }),
      receiptSummary({ receipt_id: "rev-1", status: "needs_review", source_kind: "email", document_kind: "receipt", raw_shop_name: "Acme Power", raw_nip: "7791011327", total_amount: "42.50", version: 4 }),
      receiptSummary({ receipt_id: "drf-ocr", status: "draft", source_kind: "local_ocr", document_kind: "receipt", raw_shop_name: "OCR Scan", raw_nip: null, total_amount: "15.00", version: 8 }),
      receiptSummary({ receipt_id: "drf-paste", status: "draft", source_kind: "paste", document_kind: "receipt", raw_shop_name: "Pasted Receipt", raw_nip: null, total_amount: "12.00", version: 1 }),
      receiptSummary({ receipt_id: "drf-live", status: "draft", source_kind: "live_text", document_kind: "receipt", raw_shop_name: "Live Scan", raw_nip: null, total_amount: "18.00", version: 1 }),
      receiptSummary({ receipt_id: "cnf-1", status: "confirmed", source_kind: "email", document_kind: "receipt", raw_shop_name: "Corner Cafe", raw_nip: null, total_amount: "20.00", version: 9 }),
    ];
    const clients = fakeClients(rows);

    await renderReceipts(clients);

    // needs_review, OCR/paste/live_text drafts, & confirmed checkboxes stay disabled; only 3 Gmail imports are selectable.
    expect(inputByName("select_receipt_rev-1").disabled).toBe(true);
    expect(inputByName("select_receipt_drf-ocr").disabled).toBe(true);
    expect(inputByName("select_receipt_drf-paste").disabled).toBe(true);
    expect(inputByName("select_receipt_drf-live").disabled).toBe(true);
    expect(inputByName("select_receipt_cnf-1").disabled).toBe(true);
    expect(inputByName("select_receipt_imp-1").disabled).toBe(false);

    await click(inputByName("select_all_loaded"));
    expect(inputByName("select_receipt_imp-1").checked).toBe(true);
    expect(inputByName("select_receipt_imp-2").checked).toBe(true);
    expect(inputByName("select_receipt_imp-other-nip").checked).toBe(true);
    expect(inputByName("select_receipt_drf-ocr").checked).toBe(false);
    expect(inputByName("select_receipt_drf-paste").checked).toBe(false);
    expect(inputByName("select_receipt_drf-live").checked).toBe(false);
    expect(inputByName("select_receipt_rev-1").checked).toBe(false);
    expect(host.textContent).toContain("Excluded 1 needing review (open individually to pick a bank link).");
    expect(host.textContent).toContain("Excluded 3 OCR, pasted or live-text drafts (open individually to confirm).");
    expect(host.textContent).toContain("Confirm selected (3)");
    // Two distinct groups for same merchant name with different NIPs!
    expect(host.textContent).toContain("Confirm all from: Acme Power (NIP 7791011327)");
    expect(host.textContent).toContain("Confirm all from: Acme Power (NIP 5261040828)");

    // Polish localization labels check.
    await renderReceipts(clients, { locale: "pl" });
    expect(host.textContent).toContain("Zaznacz wszystkie załadowane");
    expect(host.textContent).toContain("Zatwierdź wszystkie od: Acme Power (NIP 7791011327)");
    expect(host.textContent).toContain("Pominięto 3 szkiców OCR, wklejonych lub z aparatu (otwórz pojedynczo, aby zatwierdzić).");
    expect(host.textContent).toContain("Zatwierdź zaznaczone (3)");

    // Switch back to EN and test sequential confirms with each receipt's CAS version, omitted lines for payment confirmations, & partial failures.
    await renderReceipts(clients, { locale: "en" });
    const callOrder: string[] = [];
    const firstDeferred = deferred<{ ok: true; receipt: ReceiptDetail }>();

    vi.mocked(clients.rApi.receiptDetail).mockImplementation((id) => {
      callOrder.push(`detail:${id}`);
      if (id === "imp-1") {
        return ok({
          receipt: receiptDetail({ receipt_id: "imp-1", status: "imported", source_kind: "attachment", document_kind: "invoice", invoice_number: "FV/1", raw_shop_name: "Acme Power", raw_nip: "7791011327", payment_method: null, total_amount: "50.00", lines: [], version: 2 }),
          candidates: [receiptCandidate({ entry_reference: "9".repeat(64), candidate_token: "tok-bulk-ignored", merchant_token_hit: true })],
        });
      }
      if (id === "imp-2") {
        return ok({
          receipt: receiptDetail({ receipt_id: "imp-2", status: "imported", source_kind: "email", document_kind: "payment_confirmation", raw_shop_name: "Acme Power", raw_nip: "7791011327", payment_method: null, total_amount: "30.00", lines: [{ line_no: 1, line_kind: "other", raw_name: REDACTED_VALUE, raw_name_key: "[usunieto]", ptu_code: null, quantity: "1", raw_unit: null, unit_price_raw: null, gross_line_total: "30.00", discount_amount: "0", net_line_total: "30.00" }], version: 5 }),
          candidates: [],
        });
      }
      return ok({
        receipt: receiptDetail({ receipt_id: "imp-other-nip", status: "imported", source_kind: "email", document_kind: "payment_confirmation", raw_shop_name: "Acme Power", raw_nip: "5261040828", payment_method: null, total_amount: "45.00", lines: [{ line_no: 1, line_kind: "other", raw_name: "Hosting bill", raw_name_key: "hosting bill", ptu_code: null, quantity: "1", raw_unit: null, unit_price_raw: null, gross_line_total: "45.00", discount_amount: "0", net_line_total: "45.00" }], version: 3 }),
        candidates: [],
      });
    });

    vi.mocked(clients.rApi.confirmReceipt).mockImplementation((req) => {
      callOrder.push(`confirm:${req.receipt_id}:v${String(req.expected_version)}`);
      if (req.receipt_id === "imp-1") return firstDeferred.promise;
      if (req.receipt_id === "imp-2") return fail(409, "budget_receipt_conflict");
      return ok({ ok: true, receipt: receiptDetail({ receipt_id: req.receipt_id, status: "confirmed", version: req.expected_version + 1 }) });
    });

    await click(button("Confirm selected (3)"));
    const confirmBox = alertBox();
    expect(confirmBox.textContent).toContain("Confirm 3 documents without bank links?");
    expect(confirmBox.querySelector('input[name="bulk_auto_link"]')).toBeNull();
    expect(confirmBox.querySelector('input[name="bulk_confirm_unlinked"]')).toBeNull();
    expect(confirmBox.textContent).toContain("Merchants: Acme Power (NIP 7791011327), Acme Power (NIP 5261040828)");
    expect(confirmBox.textContent).toContain("Excluded 1 needing review");
    expect(confirmBox.textContent).toContain("Excluded 3 OCR, pasted or live-text drafts");

    await act(async () => {
      button("Start bulk confirm", confirmBox).click();
      await Promise.resolve();
    });
    await settle();
    expect(callOrder).toEqual(["detail:imp-1", "confirm:imp-1:v2"]);
    expect(host.textContent).toContain("Confirming 0 / 3…");
    expect(openDetailButton(0).disabled).toBe(true);

    await act(async () => {
      firstDeferred.resolve({ ok: true, data: { ok: true, receipt: receiptDetail({ receipt_id: "imp-1", status: "confirmed", version: 3 }) } });
      await Promise.resolve();
    });
    await settle();

    expect(callOrder).toEqual([
      "detail:imp-1",
      "confirm:imp-1:v2",
      "detail:imp-2",
      "confirm:imp-2:v5",
      "detail:imp-other-nip",
      "confirm:imp-other-nip:v3",
    ]);
    // Invoices with 0 lines synthesize 1 summary line; payment confirmations (even with redacted line) OMIT `lines`.
    expect(clients.rApi.confirmReceipt).toHaveBeenNthCalledWith(1, expect.objectContaining({
      receipt_id: "imp-1",
      expected_version: 2,
      link: null,
      lines: [expect.objectContaining({ line_kind: "other", raw_name: "FV/1", net_line_total: "50" }) as unknown],
    }));
    const secondPayload = vi.mocked(clients.rApi.confirmReceipt).mock.calls[1]?.[0];
    const thirdPayload = vi.mocked(clients.rApi.confirmReceipt).mock.calls[2]?.[0];
    expect("lines" in (secondPayload ?? {})).toBe(false);
    expect("raw_shop_name" in (secondPayload ?? {})).toBe(false);
    expect("raw_nip" in (secondPayload ?? {})).toBe(false);
    expect("lines" in (thirdPayload ?? {})).toBe(false);
    expect(host.textContent).toContain("Bulk confirm finished: 2 confirmed, 1 failed.");
    expect(host.textContent).toContain("Failure 409 budget_receipt_conflict: 1");
    expect(clients.rApi.receiptsList).toHaveBeenCalledTimes(4);

    // Clicking NIP 7791011327 group ONLY targets the 2 receipts with that NIP (not 5261040828) + cancel stops before item #2.
    const cancelDeferred = deferred<{ ok: true; receipt: ReceiptDetail }>();
    vi.mocked(clients.rApi.confirmReceipt).mockImplementationOnce(() => cancelDeferred.promise);
    await click(button("Confirm all from: Acme Power (NIP 7791011327)"));
    const acmeBox = alertBox();
    expect(acmeBox.textContent).toContain("Confirm 2 documents without bank links?");
    expect(acmeBox.textContent).toContain("Merchants: Acme Power (NIP 7791011327)");
    expect(acmeBox.textContent).toContain("Excluded 1 needing review");
    await act(async () => {
      button("Start bulk confirm", acmeBox).click();
      await Promise.resolve();
    });
    await settle();
    await act(async () => {
      button("Cancel").click();
      cancelDeferred.resolve({ ok: true, data: { ok: true, receipt: receiptDetail({ receipt_id: "imp-1", status: "confirmed", version: 3 }) } });
      await Promise.resolve();
    });
    await settle();
    expect(host.textContent).toContain("Bulk confirm cancelled after 1 confirmed (1 remaining, 0 failed).");

    // 403 error reports failure count cleanly & aborts rest, and actor switch aborts immediately.
    const abortCalls: string[] = [];
    const actorDeferred = deferred<{ ok: true; receipt: ReceiptDetail }>();
    vi.mocked(clients.rApi.confirmReceipt).mockImplementation((req) => {
      abortCalls.push(req.receipt_id);
      if (req.receipt_id === "imp-1") return actorDeferred.promise;
      return fail(403, "budget_forbidden");
    });

    await renderReceipts({ ...clients }, { actorKey: "alice", onAccessLoss: () => false });
    await click(button("Confirm all from: Acme Power (NIP 7791011327)"));
    await act(async () => {
      button("Start bulk confirm", alertBox()).click();
      await Promise.resolve();
    });
    await settle();
    expect(abortCalls).toEqual(["imp-1"]);

    await renderReceipts(clients, { actorKey: "bob", onAccessLoss: () => false });
    await act(async () => {
      actorDeferred.resolve({ ok: true, data: { ok: true, receipt: receiptDetail({ receipt_id: "imp-1", status: "confirmed", version: 3 }) } });
      await Promise.resolve();
    });
    await settle();
    expect(abortCalls).toEqual(["imp-1"]);

    vi.mocked(clients.rApi.confirmReceipt).mockImplementation(() => fail(403, "budget_forbidden"));
    await renderReceipts(clients, { actorKey: "alice", onAccessLoss: () => false });
    await click(button("Confirm all from: Acme Power (NIP 7791011327)"));
    await click(button("Start bulk confirm", alertBox()));
    expect(host.textContent).toContain("Failure 403 budget_forbidden: 1");
    expect(host.textContent).toContain("1 remaining, 1 failed");

    // Gate lock flips true after item 1 -> aborts before item 2 detail/confirm even start.
    const imp1Detail = receiptDetail({ receipt_id: "imp-1", status: "imported", source_kind: "attachment", document_kind: "invoice", invoice_number: "FV/1", raw_shop_name: "Acme Power", raw_nip: "7791011327", payment_method: null, total_amount: "50.00", lines: [], version: 2 });
    const imp2Detail = receiptDetail({ receipt_id: "imp-2", status: "imported", source_kind: "email", document_kind: "payment_confirmation", raw_shop_name: "Acme Power", raw_nip: "7791011327", payment_method: null, total_amount: "30.00", lines: [], version: 5 });

    let gateLocked = false;
    const lockedClients = fakeClients(rows.slice(0, 2));
    lockedClients.rApi.isAuthorizationLocked = () => gateLocked;
    vi.mocked(lockedClients.rApi.receiptDetail).mockImplementation((id) =>
      ok({ receipt: id === "imp-1" ? imp1Detail : imp2Detail, candidates: [] }));
    vi.mocked(lockedClients.rApi.confirmReceipt).mockImplementation((req) => {
      gateLocked = true;
      return ok({ ok: true, receipt: receiptDetail({ receipt_id: req.receipt_id, status: "confirmed", version: 3 }) });
    });
    await renderReceipts(lockedClients, { actorKey: "owner", onAccessLoss: () => false });
    await click(button("Confirm all from: Acme Power (NIP 7791011327)"));
    await click(button("Start bulk confirm", alertBox()));
    expect(lockedClients.rApi.receiptDetail).toHaveBeenCalledTimes(1);
    expect(lockedClients.rApi.confirmReceipt).toHaveBeenCalledTimes(1);
    expect(host.textContent).toContain("Bulk confirm cancelled after 1 confirmed (1 remaining, 0 failed).");

    // Gate lock flips true while item 1 detail is pending -> aborts before item 1 confirmReceipt.
    let midLocked = false;
    const midDetailDeferred = deferred<ReceiptDetailResult>();
    const midClients = fakeClients(rows.slice(0, 2));
    midClients.rApi.isAuthorizationLocked = () => midLocked;
    vi.mocked(midClients.rApi.receiptDetail).mockImplementationOnce(() => midDetailDeferred.promise);
    await renderReceipts(midClients, { actorKey: "owner", onAccessLoss: () => false });
    await click(button("Confirm all from: Acme Power (NIP 7791011327)"));
    await act(async () => {
      button("Start bulk confirm", alertBox()).click();
      await Promise.resolve();
    });
    await settle();
    midLocked = true;
    await act(async () => {
      midDetailDeferred.resolve({ ok: true, data: { receipt: imp1Detail, candidates: [] } });
      await Promise.resolve();
    });
    await settle();
    expect(midClients.rApi.confirmReceipt).not.toHaveBeenCalled();
    expect(host.textContent).toContain("Bulk confirm cancelled after 0 confirmed (2 remaining, 0 failed).");

    // 409 budget_actor_changed aborts bulk confirm immediately without attempting remaining items.
    const changedClients = fakeClients(rows.slice(0, 2));
    vi.mocked(changedClients.rApi.receiptDetail).mockImplementation((id) =>
      ok({ receipt: id === "imp-1" ? imp1Detail : imp2Detail, candidates: [] }));
    vi.mocked(changedClients.rApi.confirmReceipt).mockImplementationOnce(() => fail(409, "budget_actor_changed"));
    await renderReceipts(changedClients, { actorKey: "owner", onAccessLoss: () => false });
    await click(button("Confirm all from: Acme Power (NIP 7791011327)"));
    await click(button("Start bulk confirm", alertBox()));
    expect(changedClients.rApi.receiptDetail).toHaveBeenCalledTimes(1);
    expect(changedClients.rApi.confirmReceipt).toHaveBeenCalledTimes(1);
    expect(host.textContent).toContain("Failure 409 budget_actor_changed: 1");
    expect(host.textContent).toContain("1 remaining, 1 failed");
  });

  it("reloads the list after confirm or delete fails with a network or unknown error", async () => {
    const confirmedRow = receiptSummary({ status: "confirmed", version: 2 });
    const clients = fakeClients([receiptSummary({ status: "needs_review", version: 1 })]);
    await renderReceipts(clients);

    // Confirm commits on server, response fails with unknown error: mounted drawer shows error and list still reloads.
    await click(openDetailButton(0));
    vi.mocked(clients.rApi.receiptsList).mockImplementation(() => ok({ receipts: [confirmedRow], next_cursor: null }));
    vi.mocked(clients.rApi.confirmReceipt).mockImplementationOnce(() => fail(200, "budget_unknown_outcome"));
    const listCallsBeforeConfirm = vi.mocked(clients.rApi.receiptsList).mock.calls.length;
    await click(button("Confirm receipt", dialog()));
    expect(dialog().querySelector('[role="alert"]')).not.toBeNull();
    expect(vi.mocked(clients.rApi.receiptsList).mock.calls.length).toBeGreaterThan(listCallsBeforeConfirm);
    await click(button("Close", dialog()));
    expect(host.querySelector(".budget-receipts")?.textContent).toContain("Confirmed");

    // Delete commits on server, response fails with network error: mounted drawer shows error and list still reloads.
    vi.mocked(clients.rApi.receiptDetail).mockImplementation(() =>
      ok({ receipt: receiptDetail({ status: "confirmed", version: 2 }), candidates: [] }));
    await click(openDetailButton(0));
    vi.mocked(clients.rApi.receiptsList).mockImplementation(() => ok({ receipts: [], next_cursor: null }));
    vi.mocked(clients.rApi.deleteReceipt).mockImplementationOnce(() => fail(0, "budget_network_error"));
    const listCallsBeforeDelete = vi.mocked(clients.rApi.receiptsList).mock.calls.length;
    await click(button("Delete document", dialog()));
    await click(button("Confirm deletion", dialog()));
    expect(dialog().querySelector('[role="alert"]')).not.toBeNull();
    expect(vi.mocked(clients.rApi.receiptsList).mock.calls.length).toBeGreaterThan(listCallsBeforeDelete);
    await click(button("Close", dialog()));
    expect(host.querySelector(".budget-receipts")?.textContent).not.toContain("Synthetic Grocery");
  });

  it("shows candidates in the detail drawer for imported receipts without preselecting them", async () => {
    const candUnrelated = receiptCandidate({
      entry_reference: "b".repeat(64),
      candidate_token: "tok-imp-unrelated",
      date_distance: 25,
      merchant_token_hit: false,
    });
    const candHit = receiptCandidate({
      entry_reference: "a".repeat(64),
      candidate_token: "tok-imp-a",
      date_distance: 23,
      merchant_token_hit: true,
    });
    const impSummary = receiptSummary({
      receipt_id: "imp-drawer",
      status: "imported",
      source_kind: "email",
      document_kind: "invoice",
      raw_shop_name: "Orange",
      due_date: "2026-11-30",
      payment_method: null,
      total_amount: "88.10",
    });
    const impDetail = receiptDetail({
      receipt_id: "imp-drawer",
      status: "imported",
      source_kind: "email",
      document_kind: "invoice",
      raw_shop_name: "Orange",
      due_date: "2026-11-30",
      payment_method: null,
      total_amount: "88.10",
      lines: [{
        line_no: 1,
        line_kind: "other",
        raw_name: "Invoice summary",
        raw_name_key: "invoice summary",
        ptu_code: null,
        quantity: "1",
        raw_unit: null,
        unit_price_raw: null,
        gross_line_total: "88.10",
        discount_amount: "0",
        net_line_total: "88.10",
      }],
    });
    const clients = fakeClients([impSummary]);
    vi.mocked(clients.rApi.receiptDetail)
      .mockImplementationOnce(() => ok({ receipt: impDetail, candidates: [candUnrelated] }))
      .mockImplementationOnce(() => ok({ receipt: impDetail, candidates: [candHit, candUnrelated] }));

    // 1. Ambiguous (unlinked + candidates present): Confirm disabled until user picks "no link" or a candidate.
    await renderReceipts(clients);
    await click(openDetailButton(0));
    const drawer1 = dialog();
    expect(linkRadio("unlink", drawer1).checked).toBe(false);
    expect(linkRadio("candidate:0", drawer1).checked).toBe(false);
    expect(linkRadio("candidate:0", drawer1).disabled).toBe(false);
    expect(button("Confirm receipt", drawer1).disabled).toBe(true);
    expect(drawer1.textContent).not.toContain("Candidates are only checked on review");

    await click(linkRadio("unlink", drawer1));
    expect(button("Confirm receipt", drawer1).disabled).toBe(false);
    await click(button("Confirm receipt", drawer1));
    expect(clients.rApi.confirmReceipt).toHaveBeenLastCalledWith(expect.objectContaining({
      receipt_id: "imp-drawer",
      link: null,
    }));

    // 2. Explicitly picking a candidate in the drawer links it on confirm.
    await click(button("Close", drawer1));
    await click(openDetailButton(0));
    const drawer2 = dialog();
    expect(linkRadio("unlink", drawer2).checked).toBe(false);
    expect(linkRadio("candidate:0", drawer2).checked).toBe(false);
    expect(button("Confirm receipt", drawer2).disabled).toBe(true);
    await click(linkRadio("candidate:1", drawer2));
    expect(button("Confirm receipt", drawer2).disabled).toBe(false);
    await click(button("Confirm receipt", drawer2));
    expect(clients.rApi.confirmReceipt).toHaveBeenLastCalledWith(expect.objectContaining({
      receipt_id: "imp-drawer",
      link: { account_id: "local-a", entry_reference: "b".repeat(64), candidate_token: "tok-imp-unrelated" },
    }));
  });
});
