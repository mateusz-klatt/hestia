// Synthetic data only: no real receipts, merchants, NIPs, or bank transactions.
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { loadLocale } from "../i18n";
import type { BudgetApi, BudgetResult } from "./api";
import { entriesOf, entry } from "./analytics/fixtures";
import { account, connection } from "./fixtures";
import type { BudgetTransaction, BudgetTransactions } from "./model";
import { ReceiptsView } from "./receipts";
import type { ReceiptsApi } from "./receipts_api";
import { REDACTED_VALUE, sha256Hex } from "./receipts_model";
import type { ReceiptCandidate, ReceiptDetail, ReceiptDetailResult, ReceiptListResult, ReceiptSummary } from "./receipts_model";

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
    entry_reference: sha256Hex("tx-entry-001"),
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
  const analyticsApi = {
    analyticsTransactions: vi.fn<BudgetApi["analyticsTransactions"]>((period) => ok(entriesOf(period))),
    transactions: vi.fn<BudgetApi["transactions"]>((accountId) =>
      ok<BudgetTransactions>({ account_id: accountId, transactions: [], account_total: 0, uncertain_total: 0, next_cursor: null })),
  };
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
  return { analyticsApi, rApi };
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
  analyticsEnabled?: boolean;
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
        api={clients.analyticsApi}
        receiptsApi={clients.rApi}
        accounts={accounts}
        connections={[connection]}
        analyticsEnabled={options.analyticsEnabled ?? true}
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
  it("lists receipts and documents with merchant, total, currency, source icon, status and link badge", async () => {
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

    // Polish locale renders "Paragony i dowody" cleanly.
    await renderReceipts(clients, { locale: "pl" });
    expect(host.textContent).toContain("Paragony i dowody");
    expect(host.textContent).toContain("Do przeglądu");
    expect(host.textContent).toContain("Potwierdzenie płatności");
    expect(host.textContent).toContain("Powiązany");
  });

  it("applies server list filters, client-side source/date filters, search API, and cursor pagination", async () => {
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
        next_cursor: null,
      }))
      .mockImplementationOnce(() => ok<ReceiptListResult>({ receipts: [receiptSummary()], next_cursor: null }));

    vi.mocked(clients.rApi.receiptsSearch).mockImplementationOnce(() => ok<ReceiptListResult>({
      receipts: [
        receiptSummary({ receipt_id: "s-1", status: "confirmed", source_kind: "photo", raw_shop_name: "Lidl Warszawa" }),
        receiptSummary({ receipt_id: "s-2", status: "needs_review", source_kind: "attachment", raw_shop_name: "Lidl Faktura", receipt_date: "2026-11-11", linked: true, document_kind: "invoice" }),
      ],
      next_cursor: null,
    }));

    await renderReceipts(clients);

    // Filter same-month date range -> sends month + status + document_kind + linked.
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
    // Only p1-a survives source_kind=attachment + date_to=2026-11-20.
    expect(host.querySelectorAll(".budget-receipt-row")).toHaveLength(1);
    expect(host.textContent).toContain("Load more");

    // Load cursor page 2.
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
    expect(host.querySelectorAll(".budget-receipt-row")).toHaveLength(2);

    // Text search uses receiptsSearch POST JSON and still honors dropdown filters.
    await typeInto(inputByName("query"), "Lidl");
    await submit(host.querySelector(".budget-range"));
    expect(clients.rApi.receiptsSearch).toHaveBeenCalledWith({ query: "Lidl", limit: 50 });
    expect(host.textContent).toContain("Lidl Faktura");
    expect(host.textContent).not.toContain("Lidl Warszawa");

    // Reset filters returns to default list.
    await click(button("Reset filters"));
    expect(clients.rApi.receiptsList).toHaveBeenLastCalledWith({ limit: 50 });
  });

  it("shows detail header, lines, redacted markers, linked transaction via SHA-256 digest, and gates image fetch strictly on image_available", async () => {
    const createUrlSpy = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:synthetic-receipt");
    const revokeUrlSpy = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);

    const clients = fakeClients([
      receiptSummary({ receipt_id: "rec-no-img", image_available: false, linked: true }),
      receiptSummary({ receipt_id: "rec-with-img", image_available: true, linked: true, raw_shop_name: "Image Market" }),
    ]);

    const txRef = "bank-tx-digest-source";
    const linkedDigest = sha256Hex(txRef);
    const matchedAnalytics = entry(txRef, {
      account_id: "local-a",
      amount: "88.00",
      currency: "PLN",
      booking_date: "2026-11-14",
      metadata: { creditor_name: "Matched Synthetic Store", debtor_name: null, mcc: "5411" },
    });
    vi.mocked(clients.analyticsApi.analyticsTransactions).mockImplementation((period) =>
      ok(entriesOf(period, [matchedAnalytics])));

    const storageGetSpy = vi.spyOn(Storage.prototype, "getItem");
    const storageSetSpy = vi.spyOn(Storage.prototype, "setItem");

    // 1. Open receipt with image_available=false -> no receiptImage call.
    vi.mocked(clients.rApi.receiptDetail).mockImplementationOnce(() => ok({
      receipt: receiptDetail({
        receipt_id: "rec-no-img",
        image_available: false,
        linked: true,
        linked_account_id: "local-a",
        linked_entry_ref: linkedDigest,
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
    expect(drawer.textContent).toContain("Matched Synthetic Store");
    expect(drawer.textContent).toContain("88.00 PLN");
    expect(drawer.querySelector(".budget-redacted")?.textContent).toBe("Removed");
    expect(drawer.textContent).not.toContain(REDACTED_VALUE);
    await click(button("Close", drawer));

    // 2. Open receipt with image_available=true -> fetches WebP blob, renders img, revokes on close.
    vi.mocked(clients.rApi.receiptDetail).mockImplementationOnce(() => ok({
      receipt: receiptDetail({
        receipt_id: "rec-with-img",
        image_available: true,
        linked: true,
        linked_account_id: "local-a",
        linked_entry_ref: sha256Hex("raw-tx-ref"),
      }),
      candidates: [],
    }));
    // Verify fallback to api.transactions when analyticsTransactions misses or is disabled.
    const fallbackTx: BudgetTransaction = {
      entry_reference: "raw-tx-ref",
      amount: "42.50",
      currency: "PLN",
      credit_debit_indicator: "DBIT",
      status: "BOOK",
      booking_date: "2026-11-14",
      value_date: null,
      transaction_date: null,
      remittance_information: ["Fallback Counterparty"],
      source: "api",
      observed_at: 1,
      api_observed_at: 1,
    };
    vi.mocked(clients.analyticsApi.analyticsTransactions).mockImplementation((period) => ok(entriesOf(period, [])));
    vi.mocked(clients.analyticsApi.transactions).mockImplementation((accountId) =>
      ok<BudgetTransactions>({ account_id: accountId, transactions: [fallbackTx], account_total: 1, uncertain_total: 0, next_cursor: null }));

    await click(openDetailButton(1));

    expect(clients.rApi.receiptImage).toHaveBeenCalledWith("rec-with-img");
    expect(createUrlSpy).toHaveBeenCalledTimes(1);
    const img = host.querySelector<HTMLImageElement>("img.budget-receipt-image");
    expect(img?.getAttribute("src")).toBe("blob:synthetic-receipt");
    const openDrawer = dialog();
    expect(openDrawer.textContent).toContain("Fallback Counterparty");

    await click(button("Close", openDrawer));
    expect(revokeUrlSpy).toHaveBeenCalledWith("blob:synthetic-receipt");
    expect(storageGetSpy).not.toHaveBeenCalled();
    expect(storageSetSpy).not.toHaveBeenCalled();
  });

  it("handles confirm with candidate link, keep vs unlink choices, summary line addition, and CAS errors", async () => {
    const candKeep = receiptCandidate({ entry_reference: sha256Hex("kept-tx"), candidate_token: "tok-keep" });
    const candAlt = receiptCandidate({
      entry_reference: sha256Hex("alt-tx"),
      candidate_token: "tok-alt",
      date_distance: 2,
      booking_date: "2026-11-16",
      merchant_token_hit: false,
    });
    const clients = fakeClients([
      receiptSummary({ receipt_id: "rec-confirm", document_kind: "invoice", status: "imported" }),
    ]);

    // Case 1: unlinked imported invoice with 0 lines -> add summary line + pick candidate + confirm.
    vi.mocked(clients.rApi.receiptDetail).mockImplementationOnce(() => ok({
      receipt: receiptDetail({
        receipt_id: "rec-confirm",
        document_kind: "invoice",
        status: "imported",
        payment_method: null,
        total_amount: "42.50",
        lines: [],
        linked: false,
        version: 3,
      }),
      candidates: [candAlt],
    }));

    await renderReceipts(clients);
    await click(button("Details"));
    const drawer = dialog();
    await choose(selectByName("payment_method", drawer), "card");
    await click(button("Add summary line", drawer));
    await click(button("Confirm receipt", drawer));

    expect(clients.rApi.confirmReceipt).toHaveBeenCalledWith(expect.objectContaining({
      receipt_id: "rec-confirm",
      expected_version: 3,
      payment_method: "card",
      total_amount: "42.5",
      link: {
        account_id: candAlt.account_id,
        entry_reference: candAlt.entry_reference,
        candidate_token: "tok-alt",
      },
    }));
    expect(host.textContent).toContain("Receipt confirmed and saved.");

    // Case 2: linked needs_review with matching current candidate allows keep, or switch to unlink (link: null).
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
    await click(button("Details"));
    const drawer2 = dialog();
    await click(linkRadio("unlink", drawer2));
    vi.mocked(clients.rApi.confirmReceipt).mockImplementationOnce(() => fail(409, "budget_receipt_conflict"));
    await click(button("Confirm receipt", drawer2));
    expect(drawer2.textContent).toContain("This document was modified elsewhere.");
    expect(clients.rApi.confirmReceipt).toHaveBeenLastCalledWith(expect.objectContaining({ link: null }));

    // Case 3: linked confirmed without candidates blocks "keep" submit until "unlink" is chosen.
    vi.mocked(clients.rApi.receiptDetail).mockImplementation(() => ok({
      receipt: receiptDetail({
        receipt_id: "rec-confirm",
        status: "confirmed",
        linked: true,
        linked_account_id: "local-a",
        linked_entry_ref: sha256Hex("done-tx"),
        version: 5,
      }),
      candidates: [],
    }));
    await click(button("Close", drawer2));
    await click(button("Details"));
    const drawer3 = dialog();
    expect(button("Confirm receipt", drawer3).disabled).toBe(true);
    expect(drawer3.textContent).toContain("re-linking requires review candidates");
    await click(linkRadio("unlink", drawer3));
    expect(button("Confirm receipt", drawer3).disabled).toBe(false);
  });

  it("requires confirmation dialog before deleting a receipt and supports cancellation", async () => {
    const clients = fakeClients([receiptSummary({ receipt_id: "rec-del", version: 7 })]);
    vi.mocked(clients.rApi.receiptDetail).mockImplementation(() => ok({
      receipt: receiptDetail({ receipt_id: "rec-del", version: 7 }),
      candidates: [],
    }));

    await renderReceipts(clients);
    await click(button("Details"));
    const drawer = dialog();

    await click(button("Delete document", drawer));
    expect(clients.rApi.deleteReceipt).not.toHaveBeenCalled();
    const confirmBox = alertBox(drawer);
    expect(confirmBox.textContent).toContain("Delete this document?");

    await click(button("Cancel", confirmBox));
    expect(drawer.querySelector('[role="alertdialog"]')).toBeNull();
    expect(clients.rApi.deleteReceipt).not.toHaveBeenCalled();

    await click(button("Delete document", drawer));
    await click(button("Confirm deletion", drawer));
    expect(clients.rApi.deleteReceipt).toHaveBeenCalledWith({ receipt_id: "rec-del", expected_version: 7 });
    expect(host.querySelector('[role="dialog"]')).toBeNull();
  });

  it("clears all receipts state on 401/403 access loss, 404 disabled flag, and stale epochs", async () => {
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

    // 403 during confirm invokes onAccessLoss and wipes detail + image URL.
    vi.mocked(clients.rApi.confirmReceipt).mockImplementationOnce(() => fail(403, "budget_forbidden"));
    await click(button("Confirm receipt"));
    expect(onAccessLoss).toHaveBeenCalled();
    expect(host.querySelector('[role="dialog"]')).toBeNull();
    expect(revokeUrlSpy).toHaveBeenCalledWith("blob:temp-img");

    // Stale in-flight list read from an older revision is discarded once a newer revision settles.
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

    // 404 budget_receipts_disabled clears view cleanly into disabled state.
    vi.mocked(clients.rApi.receiptsList).mockImplementationOnce(() => fail(404, "budget_receipts_disabled"));
    await renderReceipts(clients, { revision: 3 });
    expect(host.textContent).toContain("Receipts are disabled on this server");
    expect(host.querySelectorAll(".budget-receipt-row")).toHaveLength(0);
  });
});
