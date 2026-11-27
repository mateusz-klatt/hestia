// Synthetic data only: no real receipts, merchants, NIPs, or bank transactions.
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { components } from "../api/openapi";
import { loadLocale } from "../i18n";
import type { BudgetResult } from "./api";
import { account, connection } from "./fixtures";
import { ReceiptsView } from "./receipts";
import type { ReceiptsApi } from "./receipts_api";
import { REDACTED_VALUE, createPendingReceiptStore } from "./receipts_model";
import type {
  ReceiptCandidate,
  ReceiptDetail,
  ReceiptDetailResult,
  ReceiptGroupCandidate,
  ReceiptGroupCandidatesResult,
  ReceiptGroupLinkResult,
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
    card_last4: null,
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
    match_strength: "none",
    candidate_token: "cand-token-1",
    ...overrides,
  };
}

type ReceiptGroupDetail = components["schemas"]["BudgetReceiptGroupDetailResult"];

function receiptGroupDetail(): ReceiptGroupDetail {
  const group: ReceiptGroupDetail["group"] = {
    group_id: "44444444-4444-4444-8444-444444444444",
    group_version: 3,
    group_status: "needs_review",
    member_count: 2,
    evidence_receipt_id: "33333333-3333-4333-8333-333333333333",
    fee_amount: "0.50",
  };
  return {
    group,
    members: [
      receiptSummary({ link_group: group, linked: true, raw_shop_name: "Synthetic first shop", total_amount: "25.00" }),
      receiptSummary({ link_group: group, linked: true, receipt_id: "22222222-2222-4222-8222-222222222222",
        raw_shop_name: "Synthetic second shop", total_amount: "17.00" }),
    ],
    evidence: receiptSummary({
      link_group: group,
      linked: true,
      receipt_id: group.evidence_receipt_id ?? "",
      document_kind: "payment_confirmation",
      gateway: "Synthetic gateway",
      raw_shop_name: null,
      total_amount: "42.50",
    }),
    linked_account_id: null,
    linked_entry_ref: null,
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
  try {
    localStorage.removeItem("hestia.receipts.autoAdvance");
  } catch {
    // Ignore when localStorage is unavailable in this test environment.
  }
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
  try {
    localStorage.removeItem("hestia.receipts.autoAdvance");
  } catch {
    // Ignore when localStorage is unavailable in this test environment.
  }
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

function openDetailButton(index: number, label = "Details"): HTMLButtonElement {
  const found = [...host.querySelectorAll("button")].filter((btn) => btn.textContent === label)[index];
  if (found === undefined) throw new Error(`${label} button #${String(index)} missing`);
  return found;
}

function openDetailById(receiptId: string, label = "Details"): HTMLButtonElement {
  const row = host.querySelector<HTMLElement>(`li[data-receipt-id="${receiptId}"]`);
  if (row === null) throw new Error(`receipt row ${receiptId} missing`);
  return button(label, row);
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
  it("shows grouped members and gateway evidence in the same drawer without legacy write actions", async () => {
    const groupDetail = receiptGroupDetail();
    const rows = [...groupDetail.members, ...(groupDetail.evidence === null ? [] : [groupDetail.evidence])];
    const clients = fakeClients(rows);
    vi.mocked(clients.rApi.receiptDetail).mockImplementation((id) => ok({
      receipt: receiptDetail(rows.find((row) => row.receipt_id === id)),
      candidates: [receiptCandidate()],
    }));
    const loadGroup = vi.fn(() => ok(groupDetail));
    clients.rApi.fetchReceiptLinkGroupDetail = loadGroup;

    await renderReceipts(clients);
    expect([...host.querySelectorAll(".budget-receipt-row")].map((row) => row.textContent)).toEqual([
      expect.stringContaining("Grouped (2)"),
      expect.stringContaining("Grouped (2)"),
      expect.stringContaining("Grouped (2)"),
    ]);
    const opener = openDetailButton(0);
    await click(opener);
    expect(loadGroup).toHaveBeenCalledWith(groupDetail.group.group_id);
    const group = dialog().querySelector<HTMLElement>('[aria-label="Linked group"]');
    if (group === null) throw new Error("group missing");
    expect(group.querySelector('[aria-label="Documents in group"]')?.textContent).toContain("Synthetic first shop");
    expect(group.querySelector('[aria-current="true"]')?.textContent).toContain("25.00 PLN");
    expect(group.querySelector('[aria-label="Payment evidence"]')?.textContent).toContain("Synthetic gateway");
    expect(group.textContent).toContain("fee 0.50 PLN");
    expect(group.textContent).toContain("Needs review");
    expect(group.textContent).not.toContain("Account:");
    expect(dialog().querySelector('[aria-label="Linked bank transaction"]')).toBeNull();
    expect(dialog().querySelector('[name="link_choice"]')).toBeNull();
    expect(dialog().textContent).not.toContain("Confirm receipt");
    expect(dialog().textContent).not.toContain("Delete document");

    await click(button("Synthetic second shop", group));
    expect(clients.rApi.receiptDetail).toHaveBeenLastCalledWith(rows[1]?.receipt_id);
    expect(dialog().querySelector('[aria-current="true"]')?.textContent).toContain("Synthetic second shop");
    expect(document.activeElement).toBe(button("Close", dialog()));
    await click(button("Close", dialog()));
    expect(document.activeElement).toBe(opener);
    expect(clients.rApi.confirmReceipt).not.toHaveBeenCalled();
    expect(clients.rApi.deleteReceipt).not.toHaveBeenCalled();
  });

  it("keeps the receipt open when a group read is unavailable and retries the group alone", async () => {
    const groupDetail = receiptGroupDetail();
    const firstRow = groupDetail.members[0];
    if (firstRow === undefined) throw new Error("member missing");
    const clients = fakeClients([firstRow], receiptDetail(firstRow));
    clients.rApi.fetchReceiptLinkGroupDetail = vi.fn<NonNullable<ReceiptsApi["fetchReceiptLinkGroupDetail"]>>()
      .mockResolvedValueOnce({ ok: false, status: 404, error: "budget_http_404" })
      .mockImplementation(() => ok(groupDetail));

    await renderReceipts(clients);
    await click(openDetailButton(0));
    expect(dialog().querySelector('[role="alert"]')?.textContent).toContain("Document details could not be loaded.");
    expect(host.querySelectorAll(".budget-receipt-row")).toHaveLength(1);
    await click(button("Retry", dialog()));
    expect(dialog().textContent).toContain("Synthetic second shop");
    expect(clients.rApi.receiptDetail).toHaveBeenCalledTimes(1);
    expect(clients.rApi.fetchReceiptLinkGroupDetail).toHaveBeenCalledTimes(2);
  });

  it("drops an unfinished group response when the active receipt session changes", async () => {
    const groupDetail = receiptGroupDetail();
    const firstRow = groupDetail.members[0];
    if (firstRow === undefined) throw new Error("member missing");
    const clients = fakeClients([firstRow], receiptDetail(firstRow));
    const response = deferred<ReceiptGroupDetail>();
    clients.rApi.fetchReceiptLinkGroupDetail = vi.fn(() => response.promise);

    await renderReceipts(clients);
    await click(openDetailButton(0));
    expect(dialog().textContent).toContain("Loading document details…");
    await renderReceipts(fakeClients(), { actorKey: "new-owner" });
    await click(openDetailButton(0));
    await act(async () => {
      response.resolve({ ok: true, data: groupDetail });
      await response.promise;
    });
    expect(dialog().querySelector('[aria-label="Linked group"]')).toBeNull();
    expect(dialog().textContent).not.toContain("Synthetic second shop");
    expect(dialog().textContent).toContain("Confirm receipt");
  });

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
    let filteredPage1Loads = 0;
    vi.mocked(clients.rApi.receiptsList).mockImplementation((req) => {
      if (req?.cursor === "cursor-page-2") {
        return ok<ReceiptListResult>({
          receipts: [
            receiptSummary({ receipt_id: "p2-a", status: "needs_review", document_kind: "invoice", source_kind: "attachment", receipt_date: "2026-11-12", raw_shop_name: "Second Page Vendor", linked: true }),
          ],
          next_cursor: "cursor-expired",
        });
      }
      if (req?.cursor === "cursor-expired" || req?.cursor === "cursor-second-bad") {
        return fail(400, "budget_receipt_invalid_request");
      }
      if (req?.month === "2026-11" && req.status === "needs_review" && req.document_kind === "invoice" && req.linked === true) {
        filteredPage1Loads += 1;
        if (filteredPage1Loads === 1) {
          return ok<ReceiptListResult>({
            receipts: [
              receiptSummary({ receipt_id: "p1-a", status: "needs_review", document_kind: "invoice", source_kind: "attachment", receipt_date: "2026-11-10", linked: true }),
              receiptSummary({ receipt_id: "p1-b", status: "needs_review", document_kind: "invoice", source_kind: "email", receipt_date: "2026-11-10", linked: true }),
              receiptSummary({ receipt_id: "p1-c", status: "needs_review", document_kind: "invoice", source_kind: "attachment", receipt_date: "2026-11-28", linked: true }),
            ],
            next_cursor: "cursor-page-2",
          });
        }
        return ok<ReceiptListResult>({
          receipts: [
            receiptSummary({ receipt_id: "reloaded-p1", status: "needs_review", document_kind: "invoice", source_kind: "attachment", receipt_date: "2026-11-10", raw_shop_name: "Fresh Page 1 Vendor", linked: true }),
          ],
          next_cursor: "cursor-second-bad",
        });
      }
      return ok<ReceiptListResult>({ receipts: [receiptSummary()], next_cursor: null });
    });

    vi.mocked(clients.rApi.receiptsSearch).mockImplementationOnce(() => ok<ReceiptListResult>({
      receipts: [
        receiptSummary({ receipt_id: "s-1", status: "confirmed", source_kind: "photo", raw_shop_name: "Lidl Warszawa" }),
        receiptSummary({ receipt_id: "s-2", status: "needs_review", source_kind: "attachment", raw_shop_name: "Lidl Faktura", receipt_date: "2026-11-11", linked: true, document_kind: "invoice" }),
      ],
      next_cursor: null,
    }));

    await renderReceipts(clients);
    expect([...host.querySelectorAll("button")].some((b) => b.textContent === "Apply filters")).toBe(false);
    expect(host.textContent).not.toContain("Filtering loaded pages only");

    await choose(selectByName("status"), "needs_review");
    await choose(selectByName("document_kind"), "invoice");
    await choose(selectByName("source_kind"), "attachment");
    await choose(selectByName("linked"), "linked");
    await typeInto(inputByName("date_from"), "2026-11-05");
    await typeInto(inputByName("date_to"), "2026-11-20");
    await settle();

    expect(clients.rApi.receiptsList).toHaveBeenLastCalledWith({
      limit: 50,
      month: "2026-11",
      status: "needs_review",
      document_kind: "invoice",
      linked: true,
    });
    expect(host.querySelectorAll(".budget-receipt-row")).toHaveLength(1);
    expect(host.textContent).toContain("Filtering loaded pages only — load more to check older entries.");

    await click(button("Load more"));
    expect(clients.rApi.receiptsList).toHaveBeenLastCalledWith({
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
    expect(storageGetSpy.mock.calls.every(([key]) => key === "hestia.receipts.autoAdvance")).toBe(true);
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
    expect(host.querySelector('[role="dialog"]')).toBeNull();

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
    await click(inputByName("auto_advance_on_confirm", dialog()));

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
    await click(openDetailById("rec-b"));
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

    const confirmBtn = button("Confirm receipt", drawer);
    await act(async () => {
      confirmBtn.click();
      confirmBtn.click();
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
    expect(host.querySelector('[role="dialog"]')).toBeNull();
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
    expect(host.querySelector('[role="dialog"]')).toBeNull();
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

  it("shows and submits only the total when a redacted receipt would need a remainder", async () => {
    const detail = receiptDetail({ status: "draft", total_amount: "50.00", math_delta: "-7.50" });
    const candidate = receiptCandidate({ amount: "50.00", match_strength: "nip" });
    const clients = fakeClients([detail], detail, [candidate]);

    await renderReceipts(clients, { locale: "pl" });
    await click(openDetailButton(0, "Szczegóły"));
    const drawer = dialog();

    expect(drawer.textContent).toContain("Pozycje odczytane częściowo — zostanie zapisana tylko suma.");
    expect(drawer.textContent).not.toContain("Przy zatwierdzaniu zachowane zostaną zapisane pozycje.");
    expect(drawer.textContent).not.toContain("różnica 7,50 zł jako pozostałe");
    expect(inputByName("line_name", drawer).disabled).toBe(true);
    expect(linkRadio("candidate:0", drawer).checked).toBe(true);
    const confirm = button("Zatwierdź dokument", drawer);
    expect(confirm.disabled).toBe(false);
    await click(confirm);

    expect(clients.rApi.confirmReceipt).toHaveBeenCalledTimes(1);
    const request = vi.mocked(clients.rApi.confirmReceipt).mock.calls[0]?.[0];
    expect(request).toMatchObject({
      total_amount: "50",
      link: {
        account_id: candidate.account_id,
        entry_reference: candidate.entry_reference,
        candidate_token: candidate.candidate_token,
      },
    });
    expect(request?.lines).toEqual([{
      line_kind: "other",
      raw_name: "Tylko suma",
      ptu_code: null,
      quantity: "1",
      raw_unit: null,
      unit_price_raw: null,
      gross_line_total: "50",
      discount_amount: "0",
      net_line_total: "50",
    }]);
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
          candidates: [receiptCandidate({ entry_reference: "9".repeat(64), candidate_token: "tok-bulk-ignored", merchant_token_hit: true, match_strength: "nip" })],
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
      match_strength: "distinctive",
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
    expect(host.querySelector('[role="dialog"]')).toBeNull();
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

  it("preselects a single strong match in the detail drawer and supports Zmień and Odłącz flows", async () => {
    const candNip = receiptCandidate({
      entry_reference: "1".repeat(64),
      candidate_token: "tok-nip-1",
      date_distance: 0,
      merchant_token_hit: true,
      match_strength: "nip",
    });
    const candDist = receiptCandidate({
      entry_reference: "2".repeat(64),
      candidate_token: "tok-dist-1",
      date_distance: 2,
      booking_date: "2026-11-16",
      merchant_token_hit: true,
      match_strength: "distinctive",
    });
    const candGeneric = receiptCandidate({
      entry_reference: "3".repeat(64),
      candidate_token: "tok-gen-1",
      merchant_token_hit: true,
      match_strength: "generic",
    });
    const candNone = receiptCandidate({
      entry_reference: "4".repeat(64),
      candidate_token: "tok-none-1",
      merchant_token_hit: false,
      match_strength: "none",
    });

    const rows = [
      receiptSummary({ receipt_id: "rec-nip", status: "imported", raw_shop_name: "Biedronka" }),
      receiptSummary({ receipt_id: "rec-dist", status: "draft", raw_shop_name: "Biedronka" }),
      receiptSummary({ receipt_id: "rec-review", status: "needs_review", raw_shop_name: "Biedronka" }),
      receiptSummary({ receipt_id: "rec-gen", status: "imported", raw_shop_name: "Biedronka" }),
      receiptSummary({ receipt_id: "rec-none", status: "imported", raw_shop_name: "Biedronka" }),
      receiptSummary({ receipt_id: "rec-two", status: "imported", raw_shop_name: "Biedronka" }),
      receiptSummary({ receipt_id: "rec-conf-u", status: "confirmed", raw_shop_name: "Biedronka" }),
      receiptSummary({ receipt_id: "rec-conf-l", status: "confirmed", raw_shop_name: "Biedronka" }),
      receiptSummary({ receipt_id: "rec-cash", status: "imported", raw_shop_name: "Biedronka" }),
    ];
    const baseDetail = (overrides: Partial<ReceiptDetail> = {}) => receiptDetail({
      status: "imported",
      linked: false,
      linked_account_id: null,
      linked_entry_ref: null,
      payment_method: "card",
      ...overrides,
    });

    const clients = fakeClients(rows);
    vi.mocked(clients.rApi.receiptDetail)
      // 1. PL imported single "nip" -> banner + preselect + Zmień
      .mockImplementationOnce(() => ok({ receipt: baseDetail({ receipt_id: "rec-nip", status: "imported" }), candidates: [candNip] }))
      // 2. PL draft single "distinctive" -> banner + Odłącz / bez powiązania -> sends link: null (+ post-confirm detail reload)
      .mockImplementationOnce(() => ok({ receipt: baseDetail({ receipt_id: "rec-dist", status: "draft" }), candidates: [candDist] }))
      .mockImplementationOnce(() => ok({ receipt: baseDetail({ receipt_id: "rec-dist", status: "confirmed", version: 2 }), candidates: [] }))
      // 3. EN needs_review single "nip" -> 1-click Confirm sends candNip link (+ post-confirm detail reload)
      .mockImplementationOnce(() => ok({ receipt: baseDetail({ receipt_id: "rec-review", status: "needs_review" }), candidates: [candNip] }))
      .mockImplementationOnce(() => ok({ receipt: baseDetail({ receipt_id: "rec-review", status: "confirmed", linked: true, version: 2 }), candidates: [] }))
      // 4. EN single "generic" -> NO preselect
      .mockImplementationOnce(() => ok({ receipt: baseDetail({ receipt_id: "rec-gen", status: "imported" }), candidates: [candGeneric] }))
      // 5. EN single "none" -> NO preselect
      .mockImplementationOnce(() => ok({ receipt: baseDetail({ receipt_id: "rec-none", status: "imported" }), candidates: [candNone] }))
      // 6. EN two candidates even when first is "nip" -> NO preselect
      .mockImplementationOnce(() => ok({ receipt: baseDetail({ receipt_id: "rec-two", status: "imported" }), candidates: [candNip, candNone] }))
      // 7. EN linked / stored link changed -> NO preselect
      .mockImplementationOnce(() => ok({
        receipt: baseDetail({
          receipt_id: "rec-review",
          status: "needs_review",
          linked: false,
          linked_account_id: "local-a",
          linked_entry_ref: "9".repeat(64),
        }),
        candidates: [candNip],
      }))
      // 8. EN confirmed unlinked & confirmed linked re-edit with single "nip" -> NO preselect
      .mockImplementationOnce(() => ok({ receipt: baseDetail({ receipt_id: "rec-conf-u", status: "confirmed", linked: false }), candidates: [candNip] }))
      .mockImplementationOnce(() => ok({
        receipt: baseDetail({
          receipt_id: "rec-conf-l",
          status: "confirmed",
          linked: true,
          linked_account_id: "local-a",
          linked_entry_ref: "1".repeat(64),
        }),
        candidates: [candNip],
      }))
      // 9. EN cash payment unlinked with single "nip" -> NO banner, unlink selected
      .mockImplementationOnce(() => ok({
        receipt: baseDetail({ receipt_id: "rec-cash", status: "imported", payment_method: "cash" }),
        candidates: [candNip],
      }));

    // PL flow: banner content, Zmień focus & clear, then next receipt Odłącz flow.
    await renderReceipts(clients, { locale: "pl" });
    await click(openDetailButton(0, "Szczegóły"));
    const dPl1 = dialog();
    const bannerPl1 = dPl1.querySelector<HTMLElement>(".budget-receipt-suggested");
    expect(bannerPl1).not.toBeNull();
    expect(bannerPl1?.textContent).toContain("Sugerowane dopasowanie");
    expect(bannerPl1?.textContent).toContain("Synthetic Checking");
    expect(bannerPl1?.textContent).toContain("2026-11-14");
    expect(bannerPl1?.textContent).toContain("42,50");
    expect(bannerPl1?.querySelector('[data-match-strength="nip"]')?.textContent).toBe("NIP");
    expect(linkRadio("candidate:0", dPl1).checked).toBe(true);
    const submitPl1 = dPl1.querySelector<HTMLButtonElement>("form.budget-receipt-confirm button.budget-primary");
    expect(submitPl1?.disabled).toBe(false);

    await click(button("Zmień", dPl1));
    expect(dPl1.querySelector(".budget-receipt-suggested")).toBeNull();
    expect(linkRadio("candidate:0", dPl1).checked).toBe(false);
    expect(linkRadio("unlink", dPl1).checked).toBe(false);
    expect(submitPl1?.disabled).toBe(true);
    expect(document.activeElement).toBe(linkRadio("candidate:0", dPl1).closest("fieldset"));

    await click(linkRadio("candidate:0", dPl1));
    expect(submitPl1?.disabled).toBe(false);
    await click(button("Zamknij", dPl1));

    await click(openDetailButton(1, "Szczegóły"));
    const dPl2 = dialog();
    const bannerPl2 = dPl2.querySelector<HTMLElement>(".budget-receipt-suggested");
    expect(bannerPl2).not.toBeNull();
    expect(bannerPl2?.querySelector('[data-match-strength="distinctive"]')?.textContent).toBe("Sprzedawca");
    expect(linkRadio("candidate:0", dPl2).checked).toBe(true);
    await click(button("Odłącz / bez powiązania", dPl2));
    expect(dPl2.querySelector(".budget-receipt-suggested")).toBeNull();
    expect(linkRadio("unlink", dPl2).checked).toBe(true);
    await click(inputByName("auto_advance_on_confirm", dPl2));
    const submitPl2 = dPl2.querySelector<HTMLButtonElement>("form.budget-receipt-confirm button.budget-primary");
    if (submitPl2 === null) throw new Error("expected submit button");
    expect(submitPl2.disabled).toBe(false);
    await click(submitPl2);
    expect(clients.rApi.confirmReceipt).toHaveBeenLastCalledWith(expect.objectContaining({
      receipt_id: "rec-dist",
      link: null,
    }));
    await click(button("Zamknij", dPl2));

    // EN flow: one-click confirm on needs_review + all non-preselection cases.
    await renderReceipts(clients, { locale: "en" });
    await click(openDetailButton(2));
    const dEn1 = dialog();
    const bannerEn1 = dEn1.querySelector<HTMLElement>(".budget-receipt-suggested");
    expect(bannerEn1).not.toBeNull();
    expect(bannerEn1?.textContent).toContain("Suggested match");
    expect(bannerEn1?.querySelector('[data-match-strength="nip"]')?.textContent).toBe("NIP match");
    expect(linkRadio("candidate:0", dEn1).checked).toBe(true);
    expect(button("Confirm receipt", dEn1).disabled).toBe(false);
    await click(button("Confirm receipt", dEn1));
    expect(clients.rApi.confirmReceipt).toHaveBeenLastCalledWith(expect.objectContaining({
      receipt_id: "rec-review",
      link: { account_id: "local-a", entry_reference: "1".repeat(64), candidate_token: "tok-nip-1" },
    }));
    await click(button("Close", dEn1));

    // generic, none, 2+ candidates, stored link changed -> no banner, empty choice, Confirm disabled.
    for (const rowIdx of [3, 4, 5, 2]) {
      await click(openDetailButton(rowIdx));
      const d = dialog();
      expect(d.querySelector(".budget-receipt-suggested")).toBeNull();
      expect(linkRadio("candidate:0", d).checked).toBe(false);
      expect(linkRadio("unlink", d).checked).toBe(false);
      expect(button("Confirm receipt", d).disabled).toBe(true);
      await click(button("Close", d));
    }

    // confirmed unlinked -> no banner, unlink auto-selected, no candidate offered.
    await click(openDetailButton(6));
    const dConfirmedUnlinked = dialog();
    expect(dConfirmedUnlinked.querySelector(".budget-receipt-suggested")).toBeNull();
    expect(dConfirmedUnlinked.querySelector('input[name="link_choice"][value="candidate:0"]')).toBeNull();
    expect(linkRadio("unlink", dConfirmedUnlinked).checked).toBe(true);
    expect(button("Confirm receipt", dConfirmedUnlinked).disabled).toBe(false);
    await click(button("Close", dConfirmedUnlinked));

    // confirmed linked (re-edit) -> no banner, empty choice, Confirm disabled until explicit Unlink.
    await click(openDetailButton(7));
    const dConfirmedLinked = dialog();
    expect(dConfirmedLinked.querySelector(".budget-receipt-suggested")).toBeNull();
    expect(dConfirmedLinked.querySelector('input[name="link_choice"][value="candidate:0"]')).toBeNull();
    expect(linkRadio("unlink", dConfirmedLinked).checked).toBe(false);
    expect(button("Confirm receipt", dConfirmedLinked).disabled).toBe(true);
    await click(button("Close", dConfirmedLinked));

    // cash payment -> no banner, no candidate preselected (unlink auto-selected for cash).
    await click(openDetailButton(8));
    const dCash = dialog();
    expect(dCash.querySelector(".budget-receipt-suggested")).toBeNull();
    expect(linkRadio("unlink", dCash).checked).toBe(true);
    expect(button("Confirm receipt", dCash).disabled).toBe(false);
    await click(button("Close", dCash));
  });

  it("preselects the bank candidate and confirms a partially read receipt on one click", async () => {
    const detail = receiptDetail({
      status: "draft",
      total_amount: "67.01",
      math_delta: "-7.01",
      lines: ["10.00", "20.00", "30.00"].map<ReceiptDetail["lines"][number]>((amount, index) => ({
        line_no: index + 1,
        line_kind: "item",
        raw_name: `Synthetic item ${String(index + 1)}`,
        raw_name_key: `synthetic item ${String(index + 1)}`,
        ptu_code: null,
        quantity: "1",
        raw_unit: null,
        unit_price_raw: null,
        gross_line_total: amount,
        discount_amount: "0",
        net_line_total: amount,
      })),
    });
    const candidate = receiptCandidate({ amount: "67.01", match_strength: "nip" });
    const clients = fakeClients([detail], detail, [candidate]);

    await renderReceipts(clients, { locale: "pl" });
    await click(openDetailButton(0, "Szczegóły"));
    const drawer = dialog();

    expect(clients.rApi.receiptDetail).toHaveBeenCalledWith(detail.receipt_id);
    expect(drawer.textContent).toContain("Sugerowane dopasowanie");
    expect(linkRadio("candidate:0", drawer).checked).toBe(true);
    expect(drawer.textContent).toContain("Pozycje odczytane częściowo — różnica 7,01 zł jako pozostałe.");
    expect(drawer.textContent).not.toContain("Suma pozycji musi być równa kwocie całkowitej.");
    const confirm = button("Zatwierdź dokument", drawer);
    expect(confirm.disabled).toBe(false);
    await click(confirm);

    expect(clients.rApi.confirmReceipt).toHaveBeenCalledTimes(1);
    const request = vi.mocked(clients.rApi.confirmReceipt).mock.calls[0]?.[0];
    expect(request).toMatchObject({
      total_amount: "67.01",
      link: {
        account_id: candidate.account_id,
        entry_reference: candidate.entry_reference,
        candidate_token: candidate.candidate_token,
      },
    });
    expect(request?.lines?.map((line) => line.net_line_total)).toEqual(["10", "20", "30", "7.01"]);
  });

  it("never confirms or links on implicit Enter or form submit with a preselected candidate", async () => {
    const candNip = receiptCandidate({
      entry_reference: "1".repeat(64),
      candidate_token: "tok-enter-nip",
      match_strength: "nip",
    });
    const clients = fakeClients(
      [receiptSummary({ receipt_id: "rec-enter", status: "imported" })],
      receiptDetail({ receipt_id: "rec-enter", status: "imported", linked: false }),
      [candNip],
    );

    await renderReceipts(clients);
    await click(openDetailButton(0));
    const d = dialog();
    expect(linkRadio("candidate:0", d).checked).toBe(true);
    const shopInput = inputByName("raw_shop_name", d);
    const form = d.querySelector<HTMLFormElement>("form.budget-receipt-confirm");
    await act(async () => {
      shopInput.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
      form?.requestSubmit();
      await Promise.resolve();
    });
    expect(clients.rApi.confirmReceipt).not.toHaveBeenCalled();

    await click(button("Confirm receipt", d));
    expect(clients.rApi.confirmReceipt).toHaveBeenCalledTimes(1);
    expect(clients.rApi.confirmReceipt).toHaveBeenLastCalledWith(expect.objectContaining({
      receipt_id: "rec-enter",
      link: { account_id: "local-a", entry_reference: "1".repeat(64), candidate_token: "tok-enter-nip" },
    }));
  });

  it("keeps Odłącz across background refreshes and drawer reopen, and clears preselection if candidates change", async () => {
    const candNipA = receiptCandidate({
      entry_reference: "a".repeat(64),
      candidate_token: "tok-a",
      match_strength: "nip",
    });
    const candNipB = receiptCandidate({
      entry_reference: "b".repeat(64),
      candidate_token: "tok-b",
      match_strength: "nip",
    });
    const rows = [
      receiptSummary({ receipt_id: "rec-stay-unlink", status: "imported" }),
      receiptSummary({ receipt_id: "rec-cand-shift", status: "imported" }),
    ];
    const clients = fakeClients(rows);
    vi.mocked(clients.rApi.receiptDetail).mockImplementation((id) => {
      if (id === "rec-stay-unlink") {
        return ok({ receipt: receiptDetail({ receipt_id: id, status: "imported", linked: false }), candidates: [candNipA] });
      }
      return ok({ receipt: receiptDetail({ receipt_id: id, status: "imported", linked: false }), candidates: [candNipA] });
    });

    await renderReceipts(clients, { locale: "pl", revision: 1 });
    await click(openDetailButton(0, "Szczegóły"));
    expect(linkRadio("candidate:0", dialog()).checked).toBe(true);
    await click(button("Odłącz / bez powiązania", dialog()));
    expect(linkRadio("unlink", dialog()).checked).toBe(true);

    // Background refresh returns the exact same strong candidate -> unlink must stay selected, banner gone.
    await renderReceipts(clients, { locale: "pl", revision: 2 });
    expect(linkRadio("unlink", dialog()).checked).toBe(true);
    expect(dialog().querySelector(".budget-receipt-suggested")).toBeNull();

    // Close and re-open same receipt -> still unlink and no banner.
    await click(button("Zamknij", dialog()));
    await click(openDetailButton(0, "Szczegóły"));
    expect(linkRadio("unlink", dialog()).checked).toBe(true);
    expect(dialog().querySelector(".budget-receipt-suggested")).toBeNull();
    await click(button("Zamknij", dialog()));

    // Untouched receipt where background refresh changes candidate set -> preselection must not re-apply to candNipB.
    await renderReceipts(clients, { locale: "en", revision: 3 });
    await click(openDetailButton(1));
    expect(linkRadio("candidate:0", dialog()).checked).toBe(true);
    vi.mocked(clients.rApi.receiptDetail).mockImplementation(() =>
      ok({ receipt: receiptDetail({ receipt_id: "rec-cand-shift", status: "imported", linked: false }), candidates: [candNipB] }));
    await renderReceipts(clients, { locale: "en", revision: 4 });
    expect(dialog().querySelector(".budget-receipt-suggested")).toBeNull();
    expect(linkRadio("candidate:0", dialog()).checked).toBe(false);
    expect(linkRadio("unlink", dialog()).checked).toBe(false);
    expect(button("Confirm receipt", dialog()).disabled).toBe(true);
  });

  it("confirms a zero-line Gmail invoice with a preselected strong candidate on single click", async () => {
    const candNip = receiptCandidate({
      entry_reference: "5".repeat(64),
      candidate_token: "tok-zero-inv",
      match_strength: "nip",
    });
    const invSummary = receiptSummary({
      receipt_id: "rec-zero-inv",
      status: "imported",
      source_kind: "attachment",
      document_kind: "invoice",
      invoice_number: "FV/2026/05",
      total_amount: "88.00",
      payment_method: null,
      math_delta: null,
    });
    const invDetail = receiptDetail({
      receipt_id: "rec-zero-inv",
      status: "imported",
      source_kind: "attachment",
      document_kind: "invoice",
      invoice_number: "FV/2026/05",
      total_amount: "88.00",
      payment_method: null,
      math_delta: null,
      lines: [],
      linked: false,
    });
    const clients = fakeClients([invSummary], invDetail, [candNip]);
    await renderReceipts(clients);
    await click(openDetailButton(0));
    const d = dialog();
    expect(d.textContent).not.toContain("Line math discrepancy");
    expect(d.querySelector(".budget-receipt-suggested")).not.toBeNull();
    expect(linkRadio("candidate:0", d).checked).toBe(true);
    expect(button("Confirm receipt", d).disabled).toBe(false);
    await click(button("Confirm receipt", d));
    expect(clients.rApi.confirmReceipt).toHaveBeenCalledWith(expect.objectContaining({
      receipt_id: "rec-zero-inv",
      link: { account_id: "local-a", entry_reference: "5".repeat(64), candidate_token: "tok-zero-inv" },
      lines: [expect.objectContaining({ line_kind: "other", raw_name: "FV/2026/05", net_line_total: "88" }) as unknown],
    }));
  });

  it("shows suggested banner exact server distance date with optional booking note instead of booking_date", async () => {
    const candDiff = receiptCandidate({
      entry_reference: "7".repeat(64),
      candidate_token: "tok-date-diff",
      transaction_date: "2026-11-12",
      booking_date: "2026-11-14",
      value_date: "2026-11-14",
      date_distance: 0,
      match_strength: "nip",
    });
    const candValueDate = receiptCandidate({
      entry_reference: "6".repeat(64),
      candidate_token: "tok-val-date",
      transaction_date: null,
      booking_date: "2026-11-16",
      value_date: "2026-11-14",
      date_distance: 0,
      match_strength: "nip",
    });
    const candSame = receiptCandidate({
      entry_reference: "8".repeat(64),
      candidate_token: "tok-date-same",
      transaction_date: "2026-11-14",
      booking_date: "2026-11-14",
      date_distance: 0,
      match_strength: "nip",
    });
    const candNullTx = receiptCandidate({
      entry_reference: "9".repeat(64),
      candidate_token: "tok-date-null",
      transaction_date: null,
      booking_date: "2026-11-14",
      date_distance: 0,
      match_strength: "nip",
    });
    const rows = [
      receiptSummary({ receipt_id: "rec-diff", receipt_date: "2026-11-12", status: "imported" }),
      receiptSummary({ receipt_id: "rec-same", status: "imported" }),
      receiptSummary({ receipt_id: "rec-null", status: "imported" }),
      receiptSummary({ receipt_id: "rec-val", receipt_date: "2026-11-14", status: "imported" }),
    ];
    const clients = fakeClients(rows);
    vi.mocked(clients.rApi.receiptDetail)
      .mockImplementationOnce(() => ok({ receipt: receiptDetail({ receipt_id: "rec-diff", receipt_date: "2026-11-12", status: "imported", linked: false }), candidates: [candDiff] }))
      .mockImplementationOnce(() => ok({ receipt: receiptDetail({ receipt_id: "rec-val", receipt_date: "2026-11-14", status: "imported", linked: false }), candidates: [candValueDate] }))
      .mockImplementationOnce(() => ok({ receipt: receiptDetail({ receipt_id: "rec-diff", receipt_date: "2026-11-12", status: "imported", linked: false }), candidates: [candDiff] }))
      .mockImplementationOnce(() => ok({ receipt: receiptDetail({ receipt_id: "rec-same", status: "imported", linked: false }), candidates: [candSame] }))
      .mockImplementationOnce(() => ok({ receipt: receiptDetail({ receipt_id: "rec-null", status: "imported", linked: false }), candidates: [candNullTx] }))
      .mockImplementationOnce(() => ok({ receipt: receiptDetail({ receipt_id: "rec-val", receipt_date: "2026-11-14", status: "imported", linked: false }), candidates: [candValueDate] }));

    await renderReceipts(clients, { locale: "pl" });
    await click(openDetailButton(0, "Szczegóły"));
    const bannerPlDiff = dialog().querySelector<HTMLElement>(".budget-receipt-suggested");
    expect(bannerPlDiff?.textContent).toContain("2026-11-12 (księgowanie 14.11)");
    await click(button("Zamknij", dialog()));

    await click(openDetailButton(3, "Szczegóły"));
    const bannerPlVal = dialog().querySelector<HTMLElement>(".budget-receipt-suggested");
    expect(bannerPlVal?.textContent).toContain("2026-11-14 (księgowanie 16.11)");
    expect(bannerPlVal?.textContent).toContain("Ten sam dzień");
    await click(button("Zamknij", dialog()));

    await renderReceipts(clients, { locale: "en" });
    await click(openDetailButton(0));
    const bannerEnDiff = dialog().querySelector<HTMLElement>(".budget-receipt-suggested");
    expect(bannerEnDiff?.textContent).toContain("2026-11-12 (booked 14.11)");
    await click(button("Close", dialog()));

    await click(openDetailButton(1));
    const bannerSame = dialog().querySelector<HTMLElement>(".budget-receipt-suggested");
    expect(bannerSame?.textContent).toContain("2026-11-14");
    expect(bannerSame?.textContent).not.toContain("(booked");
    await click(button("Close", dialog()));

    await click(openDetailButton(2));
    const bannerNullTx = dialog().querySelector<HTMLElement>(".budget-receipt-suggested");
    expect(bannerNullTx?.textContent).toContain("2026-11-14");
    expect(bannerNullTx?.textContent).not.toContain("(booked");
    await click(button("Close", dialog()));

    await click(openDetailButton(3));
    const bannerEnVal = dialog().querySelector<HTMLElement>(".budget-receipt-suggested");
    expect(bannerEnVal?.textContent).toContain("2026-11-14 (booked 16.11)");
    expect(bannerEnVal?.textContent).toContain("Same day");
  });

  it("synchronously clears preselection before rendering swapped candidates and blocks stale Confirm click with Lista dopasowań się zmieniła", async () => {
    const candStrong = receiptCandidate({
      entry_reference: "1".repeat(64),
      candidate_token: "tok-strong",
      amount: "42.50",
      match_strength: "nip",
    });
    const candWeak = receiptCandidate({
      entry_reference: "2".repeat(64),
      candidate_token: "tok-weak",
      amount: "99.00",
      match_strength: "generic",
    });
    const detail = receiptDetail({ receipt_id: "rec-race", status: "imported", linked: false });
    const refreshDeferred = deferred<ReceiptDetailResult>();
    const clients = fakeClients([receiptSummary({ receipt_id: "rec-race", status: "imported" })]);
    let calls = 0;
    vi.mocked(clients.rApi.receiptDetail).mockImplementation(() => {
      calls += 1;
      if (calls === 1) return ok({ receipt: detail, candidates: [candStrong] });
      return refreshDeferred.promise;
    });

    await renderReceipts(clients, { locale: "pl", revision: 1 });
    await click(openDetailButton(0, "Szczegóły"));
    const confirmBtn = button("Zatwierdź dokument", dialog());
    expect(linkRadio("candidate:0", dialog()).checked).toBe(true);
    expect(confirmBtn.disabled).toBe(false);

    // Start refresh; verify Confirm never sees enabled state alongside replacement candWeak DOM,
    // and clicking Confirm in the microtask gap after detail resolves fails closed with the Polish message.
    const observedWeakWithEnabledConfirm: boolean[] = [];
    const observer = new MutationObserver(() => {
      if (dialog().textContent.includes("99,00")) {
        const btn = dialog().querySelector<HTMLButtonElement>("form.budget-receipt-confirm button.budget-primary");
        observedWeakWithEnabledConfirm.push(btn !== null && !btn.disabled);
      }
    });
    observer.observe(dialog(), { subtree: true, childList: true, attributes: true, characterData: true });

    await renderReceipts(clients, { locale: "pl", revision: 2 });
    await act(async () => {
      refreshDeferred.resolve({ ok: true, data: { receipt: detail, candidates: [candWeak] } });
      await Promise.resolve();
      // Click Confirm right after receiptDetail settled in memory before React renders candWeak.
      confirmBtn.click();
    });
    observer.disconnect();

    expect(clients.rApi.confirmReceipt).not.toHaveBeenCalled();
    expect(observedWeakWithEnabledConfirm.every((enabled) => !enabled)).toBe(true);
    expect(dialog().querySelector('[role="alert"]')?.textContent).toContain("Lista dopasowań się zmieniła");
    expect(linkRadio("candidate:0", dialog()).checked).toBe(false);
    expect(button("Zatwierdź dokument", dialog()).disabled).toBe(true);
    expect(dialog().querySelector(".budget-receipt-suggested")).toBeNull();
  });

  it("preserves preselection across reopen when only randomized candidate_token rotates and submits the fresh token", async () => {
    const entryRef = "a".repeat(64);
    const candFirst = receiptCandidate({
      entry_reference: entryRef,
      candidate_token: "tok-rand-1",
      match_strength: "nip",
    });
    const candReopen = receiptCandidate({
      entry_reference: entryRef,
      candidate_token: "tok-rand-2",
      match_strength: "nip",
    });
    const detail = receiptDetail({ receipt_id: "rec-reopen", status: "imported", linked: false });
    const clients = fakeClients([receiptSummary({ receipt_id: "rec-reopen", status: "imported" })]);
    vi.mocked(clients.rApi.receiptDetail)
      .mockImplementationOnce(() => ok({ receipt: detail, candidates: [candFirst] }))
      .mockImplementationOnce(() => ok({ receipt: detail, candidates: [candReopen] }));

    await renderReceipts(clients, { locale: "en", revision: 1 });
    await click(openDetailButton(0));
    expect(dialog().querySelector(".budget-receipt-suggested")).not.toBeNull();
    expect(linkRadio("candidate:0", dialog()).checked).toBe(true);
    await click(button("Close", dialog()));

    // Reopening the unchanged receipt returns a newly randomized candidate_token ("tok-rand-2"),
    // yet preselection stays intact and Confirm submits the fresh token from CURRENT candidates.
    await click(openDetailButton(0));
    expect(dialog().querySelector(".budget-receipt-suggested")).not.toBeNull();
    expect(linkRadio("candidate:0", dialog()).checked).toBe(true);
    const confirmBtn = button("Confirm receipt", dialog());
    expect(confirmBtn.disabled).toBe(false);
    await click(confirmBtn);
    expect(clients.rApi.confirmReceipt).toHaveBeenCalledWith(expect.objectContaining({
      receipt_id: "rec-reopen",
      link: { account_id: candReopen.account_id, entry_reference: entryRef, candidate_token: "tok-rand-2" },
    }));
  });

  it("submits reviewed_token for Keep on needs_review and fails closed with Dane z banku się zmieniły when bank evidence changes", async () => {
    const entryRef = "b".repeat(64);
    const candReviewed = receiptCandidate({
      entry_reference: entryRef,
      candidate_token: "tok-t1",
      amount: "42.50",
      match_strength: "nip",
    });
    const candRotatedOnly = receiptCandidate({
      entry_reference: entryRef,
      candidate_token: "tok-t2",
      amount: "42.50",
      match_strength: "nip",
    });
    const candEvidenceDrifted = receiptCandidate({
      entry_reference: entryRef,
      candidate_token: "tok-drifted",
      amount: "42.51",
      match_strength: "nip",
    });
    const detailSame = receiptDetail({
      receipt_id: "rec-keep-same",
      status: "needs_review",
      linked: true,
      linked_account_id: candReviewed.account_id,
      linked_entry_ref: entryRef,
      version: 2,
    });
    const detailDrifted = receiptDetail({
      receipt_id: "rec-keep-drift",
      status: "needs_review",
      linked: true,
      linked_account_id: candReviewed.account_id,
      linked_entry_ref: entryRef,
      version: 2,
    });

    const driftRefresh = deferred<ReceiptDetailResult>();
    const clients = fakeClients([
      receiptSummary({ receipt_id: "rec-keep-same", status: "needs_review", linked: true }),
      receiptSummary({ receipt_id: "rec-keep-drift", status: "needs_review", linked: true }),
    ]);
    let sameCalls = 0;
    let driftCalls = 0;
    vi.mocked(clients.rApi.receiptDetail).mockImplementation((id) => {
      if (id === "rec-keep-same") {
        sameCalls += 1;
        return ok({ receipt: detailSame, candidates: [sameCalls === 1 ? candReviewed : candRotatedOnly] });
      }
      driftCalls += 1;
      if (driftCalls === 1) return ok({ receipt: detailDrifted, candidates: [candReviewed] });
      return driftRefresh.promise;
    });

    await renderReceipts(clients, { locale: "pl", revision: 1 });

    // Part 1: Keep survives nonce-only refresh and submits the originally reviewed tok-t1 (never swaps in tok-t2).
    await click(openDetailButton(0, "Szczegóły"));
    await click(inputByName("auto_advance_on_confirm", dialog()));
    const keepRadioSame = linkRadio("keep", dialog());
    await click(keepRadioSame);
    expect(keepRadioSame.checked).toBe(true);
    await renderReceipts(clients, { locale: "pl", revision: 2 });
    await click(button("Zatwierdź dokument", dialog()));
    expect(clients.rApi.confirmReceipt).toHaveBeenCalledTimes(1);
    expect(clients.rApi.confirmReceipt).toHaveBeenLastCalledWith(expect.objectContaining({
      receipt_id: "rec-keep-same",
      link: { account_id: candReviewed.account_id, entry_reference: entryRef, candidate_token: "tok-t1" },
    }));
    await click(button("Zamknij", dialog()));

    // Part 2: When bank evidence drifts (amount changes 42.50 -> 42.51), Confirm fails closed even in microtask gap.
    vi.mocked(clients.rApi.confirmReceipt).mockClear();
    await click(openDetailButton(1, "Szczegóły"));
    const keepRadioDrift = linkRadio("keep", dialog());
    await click(keepRadioDrift);
    const confirmDriftBtn = button("Zatwierdź dokument", dialog());
    expect(confirmDriftBtn.disabled).toBe(false);

    await renderReceipts(clients, { locale: "pl", revision: 3 });
    await act(async () => {
      driftRefresh.resolve({ ok: true, data: { receipt: detailDrifted, candidates: [candEvidenceDrifted] } });
      await Promise.resolve();
      confirmDriftBtn.click();
    });

    expect(clients.rApi.confirmReceipt).not.toHaveBeenCalled();
    expect(dialog().querySelector('[role="alert"]')?.textContent).toContain("Dane z banku się zmieniły — wybierz ponownie");
    expect(linkRadio("keep", dialog()).checked).toBe(false);
    expect(button("Zatwierdź dokument", dialog()).disabled).toBe(true);
  });

  it("shows ID preselection badge, no-bank-data period note, and opt-in bulk confirm linking only exact ID matches", async () => {
    const p24Id = "id-p24-1";
    const nipId = "id-nip-1";
    const gapId = "id-gap-1";
    const idCand = receiptCandidate({
      account_id: "local-a",
      entry_reference: "e".repeat(64),
      match_strength: "id",
      merchant_token_hit: false,
      candidate_token: "tok-p24-unique",
    });
    const nipCand = receiptCandidate({
      account_id: "local-a",
      entry_reference: "f".repeat(64),
      match_strength: "nip",
      merchant_token_hit: true,
      candidate_token: "tok-nip-strong",
    });

    const rows: ReceiptSummary[] = [
      receiptSummary({
        receipt_id: p24Id,
        status: "imported",
        source_kind: "email",
        document_kind: "payment_confirmation",
        gateway: "Przelewy24",
        gateway_status: "paid",
        has_document: false,
        raw_shop_name: "Sklep P24",
        payment_method: "card",
      }),
      receiptSummary({
        receipt_id: nipId,
        status: "imported",
        source_kind: "email",
        document_kind: "invoice",
        raw_shop_name: "Sklep NIP",
        raw_nip: "7791011327",
        payment_method: null,
      }),
      receiptSummary({
        receipt_id: gapId,
        status: "imported",
        source_kind: "email",
        document_kind: "payment_confirmation",
        raw_shop_name: "Apple PreSep",
        receipt_date: "2026-08-01",
        payment_method: "card",
        card_last4: "2177",
      }),
    ];
    const clients = fakeClients(rows);
    vi.mocked(clients.rApi.receiptDetail).mockImplementation((receiptId) => {
      if (receiptId === p24Id) {
        return ok({
          receipt: receiptDetail({ ...rows[0], card_last4: null, payment_evidence: [] }),
          candidates: [idCand],
          no_bank_data: false,
        });
      }
      if (receiptId === nipId) {
        return ok({
          receipt: receiptDetail({ ...rows[1], card_last4: null, lines: [], payment_evidence: [] }),
          candidates: [nipCand],
          no_bank_data: false,
        });
      }
      return ok({
        receipt: receiptDetail({ ...rows[2], payment_evidence: [] }),
        candidates: [],
        no_bank_data: true,
      });
    });

    await renderReceipts(clients, { locale: "pl" });

    // 1) Gateway P24 receipt with unique "id" candidate preselects it with ID badge.
    await click(openDetailButton(0, "Szczegóły"));
    const d0 = dialog();
    expect(d0.querySelector('[data-match-strength="id"]')?.textContent).toBe("Identyfikator");
    expect(linkRadio("candidate:0", d0).checked).toBe(true);
    expect(d0.textContent).not.toContain("Brak danych z banku za ten okres");
    await click(button("Zamknij", d0));

    // 2) Pre-Sep receipt with no_bank_data: true shows "Brak danych z banku za ten okres." instead of empty list.
    await click(openDetailButton(2, "Szczegóły"));
    const d2 = dialog();
    expect(d2.textContent).toContain("Brak danych z banku za ten okres.");
    await click(button("Zamknij", d2));

    // 3) Opt-in bulk confirm links ONLY p24Id (exact "id" candidate) and leaves nipId & gapId unlinked.
    await click(inputByName("select_all_loaded"));
    await click(button("Zatwierdź zaznaczone (3)"));
    const bulkBox = alertBox();
    const bulkOpt = bulkBox.querySelector<HTMLInputElement>('input[name="bulk_link_by_id"]');
    if (bulkOpt === null) throw new Error("Expected bulk_link_by_id checkbox");
    expect(bulkOpt.checked).toBe(false);
    expect(bulkBox.textContent).toContain("Zatwierdź z powiązaniem (tylko pewne — po identyfikatorze)");
    await click(bulkOpt);
    await click(button("Rozpocznij zatwierdzanie", bulkBox));

    expect(clients.rApi.confirmReceipt).toHaveBeenCalledTimes(3);
    expect(clients.rApi.confirmReceipt).toHaveBeenNthCalledWith(1, expect.objectContaining({
      receipt_id: p24Id,
      link: { account_id: idCand.account_id, entry_reference: idCand.entry_reference, candidate_token: "tok-p24-unique" },
    }));
    expect(clients.rApi.confirmReceipt).toHaveBeenNthCalledWith(2, expect.objectContaining({
      receipt_id: nipId,
      link: null,
    }));
    expect(clients.rApi.confirmReceipt).toHaveBeenNthCalledWith(3, expect.objectContaining({
      receipt_id: gapId,
      link: null,
    }));
  });

  it("supports M:1 grouping from drawer button, neutral proposal Review group, 1:1 precedence, and bulk bar", async () => {
    const shop1 = receiptSummary({
      receipt_id: "g-1", status: "confirmed", source_kind: "local_ocr", raw_shop_name: "Grouped Shop A", total_amount: "20.00", version: 2,
    });
    const shop2 = receiptSummary({
      receipt_id: "g-2", status: "imported", source_kind: "email", document_kind: "invoice",
      raw_shop_name: "Grouped Shop B", total_amount: "30.00", version: 1,
    });
    const usdRow = receiptSummary({
      receipt_id: "g-usd", status: "confirmed", raw_shop_name: "Foreign Shop", total_amount: "10.00", currency: "USD", version: 1,
    });
    const groupTx = {
      account_id: "local-a",
      entry_reference: "b".repeat(64),
      amount: "50.00",
      currency: "PLN",
      credit_debit_indicator: "DBIT" as const,
      booking_date: "2026-11-15",
      value_date: "2026-11-15",
      transaction_date: "2026-11-14",
      merchant_token_hit: false,
      match_strength: "none" as const,
      date_distance: 0,
      evidence_fingerprint: "fp-internal-1",
      candidate_token: "tok-internal-1",
    };
    const proposal: ReceiptGroupCandidate = {
      ...groupTx,
      fee_amount: null,
      evidence_receipt_id: null,
      members_sum: "50.00",
      member_receipt_ids: ["g-1", "g-2"],
      member_receipts: [shop1, shop2],
      evidence: null,
    };

    const clients = fakeClients([shop1, shop2, usdRow]);
    let shop2State: ReceiptSummary = shop2;
    const baseConfirm = clients.rApi.confirmReceipt;
    clients.rApi.confirmReceipt = vi.fn<ReceiptsApi["confirmReceipt"]>((req) => {
      if (req.receipt_id === "g-2") {
        shop2State = { ...shop2, status: "confirmed", version: 2 };
      }
      return baseConfirm(req);
    });
    clients.rApi.receiptGroupCandidates = vi.fn<NonNullable<ReceiptsApi["receiptGroupCandidates"]>>(() => ok<ReceiptGroupCandidatesResult>({
      anchor_receipt_id: "g-1",
      transactions: [groupTx],
      proposals: [proposal],
      eligible_receipts: [shop1, shop2State],
      eligible_evidence: [],
      no_bank_data: false,
    }));
    clients.rApi.linkReceiptGroup = vi.fn<NonNullable<ReceiptsApi["linkReceiptGroup"]>>(() => ok<ReceiptGroupLinkResult>({
      ok: true,
      group: { group_id: "grp-new", group_version: 1, group_status: "confirmed", member_count: 2, evidence_receipt_id: null, fee_amount: null },
      members: [shop1, { ...shop2, status: "confirmed", version: 2 }],
      evidence: null,
      linked_account_id: "local-a",
      linked_entry_ref: "b".repeat(64),
    }));
    vi.mocked(clients.rApi.receiptDetail).mockImplementation((id) => {
      if (id === "g-2") {
        return ok<ReceiptDetailResult>({
          receipt: receiptDetail({ ...shop2, lines: [] }),
          candidates: [receiptCandidate({ match_strength: "id", amount: "30.00", candidate_token: "tok-strong-1to1" })],
          group_candidates: [proposal],
          no_bank_data: false,
        });
      }
      if (id === "g-1") {
        return ok<ReceiptDetailResult>({
          receipt: receiptDetail({ ...shop1 }),
          candidates: [],
          group_candidates: [proposal],
          no_bank_data: false,
        });
      }
      return ok<ReceiptDetailResult>({ receipt: receiptDetail({ ...usdRow }), candidates: [] });
    });

    await renderReceipts(clients);

    // 1) Strong 1:1 ID candidate takes precedence over neutral M:1 proposal banner, while manual Group button stays available.
    await click(openDetailButton(1));
    const dStrong = dialog();
    expect(dStrong.textContent).toContain("Suggested match");
    expect(dStrong.textContent).not.toContain("Combined charge match");
    expect(button("Group with other receipts", dStrong).disabled).toBe(false);
    await click(button("Close", dStrong));

    // 2) Receipt without strong 1:1 match surfaces neutral Combined charge match banner & Review group + Group with other receipts.
    await click(openDetailButton(0));
    const dGroup = dialog();
    expect(dGroup.textContent).toContain("Combined charge match: 2 receipts =");
    expect(dGroup.innerHTML).not.toContain("tok-internal-1");
    expect(dGroup.innerHTML).not.toContain("fp-internal-1");
    await click(button("Review group", dGroup));
    const modal = [...host.querySelectorAll<HTMLElement>('[role="dialog"]')].at(-1);
    if (modal === undefined) throw new Error("Expected group controller modal");
    expect(modal.textContent).toContain("Build receipt group");
    expect([...modal.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')].every((el) => !el.checked)).toBe(true);
    await click(button("Close", modal));
    await click(button("Close", dialog()));

    // 3) Bulk bar allows 2..8 same currency/kind including mixed confirmed + imported, and rejects mismatched currency.
    const chkUsd = inputByName("select_receipt_g-usd");
    const chk1 = inputByName("select_receipt_g-1");
    const chk2 = inputByName("select_receipt_g-2");
    expect(chk1.disabled).toBe(false);
    await click(chk1);
    await click(chkUsd);
    expect(button("Link selected to one transaction").disabled).toBe(true);
    await click(chkUsd);
    await click(chk2);
    expect(button("Link selected to one transaction").disabled).toBe(false);
    await click(button("Link selected to one transaction"));

    const bulkModal = dialog();
    const txOpt = bulkModal.querySelector<HTMLInputElement>('input[name="group_tx_choice"][value="0"]');
    if (txOpt === null) throw new Error("Expected transaction radio in bulk group modal");
    expect(txOpt.checked).toBe(false);
    await click(txOpt);
    const boxes = [...bulkModal.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')];
    for (const box of boxes) await click(box);
    await click(button("Confirm group link", bulkModal));

    expect(clients.rApi.confirmReceipt).toHaveBeenCalledWith(expect.objectContaining({
      receipt_id: "g-2",
      expected_version: 1,
      link: null,
    }));
    expect(clients.rApi.linkReceiptGroup).toHaveBeenCalledWith({
      link: { account_id: "local-a", entry_reference: "b".repeat(64), candidate_token: "tok-internal-1" },
      members: [
        { receipt_id: "g-1", expected_version: 2 },
        { receipt_id: "g-2", expected_version: 2 },
      ],
    });
    expect(host.textContent).toContain("Receipt group linked.");
  });

  it("handles CAS unlink for needs_review group recovery, dissolve_group delete, stale version conflict, and revoked access", async () => {
    const groupDetail = receiptGroupDetail();
    const groupedRow = groupDetail.members[0];
    if (groupedRow === undefined) throw new Error("Expected grouped member row");
    const recoveredRow: ReceiptDetail = receiptDetail({
      ...groupedRow,
      link_group: null,
      linked: false,
      linked_account_id: null,
      linked_entry_ref: null,
      status: "confirmed",
      version: groupedRow.version + 1,
    });

    const clients = fakeClients([groupedRow]);
    clients.rApi.fetchReceiptLinkGroupDetail = vi.fn<NonNullable<ReceiptsApi["fetchReceiptLinkGroupDetail"]>>(() => ok(groupDetail));
    clients.rApi.unlinkReceiptGroup = vi.fn<NonNullable<ReceiptsApi["unlinkReceiptGroup"]>>()
      .mockResolvedValueOnce({ ok: false, status: 409, error: "budget_receipt_conflict" })
      .mockResolvedValueOnce({
        ok: true,
        data: { ok: true, unlinked: true, group_id: groupDetail.group.group_id, members: [recoveredRow], evidence: null },
      });
    clients.rApi.receiptGroupCandidates = vi.fn<NonNullable<ReceiptsApi["receiptGroupCandidates"]>>(() => ok({
      anchor_receipt_id: groupedRow.receipt_id,
      transactions: [],
      proposals: [],
      eligible_receipts: [recoveredRow],
      eligible_evidence: [],
      no_bank_data: true,
    }));
    clients.rApi.linkReceiptGroup = vi.fn<NonNullable<ReceiptsApi["linkReceiptGroup"]>>();

    let currentReceipt = receiptDetail({ ...groupedRow, link_group: groupDetail.group, linked: true });
    vi.mocked(clients.rApi.receiptDetail).mockImplementation(() => ok({
      receipt: currentReceipt,
      candidates: [],
      group_candidates: [],
    }));

    const onAccessLoss = vi.fn((res: { ok: boolean; status?: number }) => !res.ok && res.status === 401);
    await renderReceipts(clients, { onAccessLoss });
    await click(openDetailButton(0));

    const d1 = dialog();
    expect(d1.textContent).toContain("Bank transaction details changed for this group.");
    // 409 conflict keeps error notice visible and refreshes detail cleanly.
    await click(button("Unlink group", d1));
    expect(clients.rApi.unlinkReceiptGroup).toHaveBeenCalledWith({
      group_id: groupDetail.group.group_id,
      expected_version: groupDetail.group.group_version,
    });
    expect(dialog().textContent).toContain("This document was modified elsewhere.");

    // Second unlink succeeds and transitions member back to confirmed unlinked recovery with "Group with other receipts".
    currentReceipt = recoveredRow;
    await click(button("Unlink group", dialog()));
    expect(host.textContent).toContain("Receipt group unlinked.");
    expect(button("Group with other receipts", dialog()).disabled).toBe(false);
    await click(button("Close", dialog()));

    // Dissolve group & delete sends dissolve_group: true + expected_group_version.
    currentReceipt = receiptDetail({ ...groupedRow, link_group: groupDetail.group, linked: true });
    await click(openDetailButton(0));
    await click(button("Unlink group & delete", dialog()));
    const confirmDlg = alertBox();
    await click(button("Unlink & delete", confirmDlg));
    expect(clients.rApi.deleteReceipt).toHaveBeenCalledWith({
      receipt_id: groupedRow.receipt_id,
      expected_version: groupedRow.version,
      dissolve_group: true,
      expected_group_version: groupDetail.group.group_version,
    });
    expect(host.textContent).toContain("Group unlinked and document deleted.");

    // Revoked access on unlink clears view via onAccessLoss barrier.
    vi.mocked(clients.rApi.unlinkReceiptGroup).mockResolvedValueOnce({
      ok: false, status: 401, error: "budget_unauthorized",
    });
    await click(openDetailButton(0));
    await click(button("Unlink group", dialog()));
    expect(onAccessLoss).toHaveBeenCalled();
    expect(host.querySelector('[role="dialog"]')).toBeNull();
  });

  it("auto-applies filter changes without a button, debounces search, applies on Enter, and ignores stale responses", async () => {
    const slowStatusResp = deferred<ReceiptListResult>();
    const fastStatusResp = deferred<ReceiptListResult>();
    const clients = fakeClients([receiptSummary({ receipt_id: "init-1", raw_shop_name: "Initial Store" })]);
    vi.mocked(clients.rApi.receiptsList).mockImplementation((req) => {
      if (req?.status === "imported") return slowStatusResp.promise;
      if (req?.status === "confirmed") return fastStatusResp.promise;
      return ok<ReceiptListResult>({
        receipts: [receiptSummary({ receipt_id: "init-1", raw_shop_name: "Initial Store" })],
        next_cursor: null,
      });
    });

    await renderReceipts(clients);
    expect([...host.querySelectorAll("button")].some((b) => b.textContent === "Apply filters")).toBe(false);
    expect(clients.rApi.receiptsList).toHaveBeenCalledTimes(1);

    // Immediate filter change triggers fetch without clicking any button, and stale response is ignored.
    await choose(selectByName("status"), "imported");
    expect(clients.rApi.receiptsList).toHaveBeenLastCalledWith({ limit: 50, status: "imported" });

    await choose(selectByName("status"), "confirmed");
    expect(clients.rApi.receiptsList).toHaveBeenLastCalledWith({ limit: 50, status: "confirmed" });

    await act(async () => {
      fastStatusResp.resolve({
        ok: true,
        data: {
          receipts: [receiptSummary({ receipt_id: "fast-1", status: "confirmed", raw_shop_name: "Latest Confirmed Store" })],
          next_cursor: null,
        },
      });
      await Promise.resolve();
    });
    await settle();
    expect(host.textContent).toContain("Latest Confirmed Store");

    await act(async () => {
      slowStatusResp.resolve({
        ok: true,
        data: {
          receipts: [receiptSummary({ receipt_id: "stale-1", status: "imported", raw_shop_name: "Stale Imported Store" })],
          next_cursor: null,
        },
      });
      await Promise.resolve();
    });
    await settle();
    expect(host.textContent).toContain("Latest Confirmed Store");
    expect(host.textContent).not.toContain("Stale Imported Store");

    // Search is debounced (~300 ms), and Enter applies immediately without a duplicate fetch.
    vi.useFakeTimers();
    try {
      const searchClients = fakeClients([receiptSummary({ receipt_id: "s0", raw_shop_name: "Base Store" })]);
      vi.mocked(searchClients.rApi.receiptsSearch).mockImplementation((req) => ok<ReceiptListResult>({
        receipts: [receiptSummary({ receipt_id: `res-${req.query}`, raw_shop_name: `Found ${req.query}` })],
        next_cursor: null,
      }));
      const onAccessLoss = vi.fn(() => false);
      await act(async () => {
        root.render(
          <ReceiptsView
            receiptsApi={searchClients.rApi}
            actorKey="owner-debounce"
            accounts={accounts}
            connections={[connection]}
            locale="en"
            revision={0}
            onAccessLoss={onAccessLoss}
          />,
        );
        await Promise.resolve();
      });
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });

      await typeInto(inputByName("query"), "Bie");
      await act(async () => { await vi.advanceTimersByTimeAsync(150); });
      await typeInto(inputByName("query"), "Biedronka");
      await act(async () => { await vi.advanceTimersByTimeAsync(299); });
      expect(searchClients.rApi.receiptsSearch).not.toHaveBeenCalled();

      await act(async () => { await vi.advanceTimersByTimeAsync(1); });
      await act(async () => { await Promise.resolve(); });
      expect(searchClients.rApi.receiptsSearch).toHaveBeenCalledTimes(1);
      expect(searchClients.rApi.receiptsSearch).toHaveBeenLastCalledWith({ query: "Biedronka", limit: 50 });
      expect(host.textContent).toContain("Found Biedronka");

      await typeInto(inputByName("query"), "Lidl");
      expect(searchClients.rApi.receiptsSearch).toHaveBeenCalledTimes(1);
      await act(async () => {
        inputByName("query").dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
        await Promise.resolve();
      });
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      await act(async () => { await Promise.resolve(); });
      expect(searchClients.rApi.receiptsSearch).toHaveBeenCalledTimes(2);
      expect(searchClients.rApi.receiptsSearch).toHaveBeenLastCalledWith({ query: "Lidl", limit: 50 });
      expect(host.textContent).toContain("Found Lidl");

      await act(async () => { await vi.advanceTimersByTimeAsync(500); });
      expect(searchClients.rApi.receiptsSearch).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("quietly refreshes the loaded range after edit or delete, keeping loaded pages and scroll without full loading state", async () => {
    const singleLine = receiptDetail().lines.slice(0, 1);
    let page1Rows: ReceiptSummary[] = [
      receiptSummary({ receipt_id: "p1-a", status: "needs_review", raw_shop_name: "Page One Shop A", total_amount: "12.50", version: 1 }),
      receiptSummary({ receipt_id: "p1-b", status: "confirmed", raw_shop_name: "Page One Shop B", total_amount: "12.50", version: 1 }),
    ];
    let page2Rows: ReceiptSummary[] = [
      receiptSummary({ receipt_id: "p2-a", status: "needs_review", raw_shop_name: "Page Two Shop A", total_amount: "12.50", version: 2 }),
      receiptSummary({ receipt_id: "p2-b", status: "needs_review", raw_shop_name: "Page Two Shop B", total_amount: "12.50", version: 1 }),
    ];

    const pendingRefreshPage1 = deferred<ReceiptListResult>();
    let holdNextPage1 = false;
    const clients = fakeClients(page1Rows);
    vi.mocked(clients.rApi.receiptsList).mockImplementation((req) => {
      if (req?.cursor === "cursor-2") {
        return ok<ReceiptListResult>({ receipts: page2Rows, next_cursor: "cursor-3" });
      }
      if (req?.cursor === "cursor-3") {
        return ok<ReceiptListResult>({ receipts: [], next_cursor: null });
      }
      if (holdNextPage1) {
        holdNextPage1 = false;
        return pendingRefreshPage1.promise;
      }
      return ok<ReceiptListResult>({ receipts: page1Rows, next_cursor: "cursor-2" });
    });
    vi.mocked(clients.rApi.receiptDetail).mockImplementation((id) => {
      const match = [...page1Rows, ...page2Rows].find((row) => row.receipt_id === id)
        ?? receiptSummary({ receipt_id: id, total_amount: "12.50" });
      return ok<ReceiptDetailResult>({ receipt: receiptDetail({ ...match, lines: singleLine }), candidates: [] });
    });
    vi.mocked(clients.rApi.confirmReceipt).mockImplementation((req) => {
      const target = [...page1Rows, ...page2Rows].find((row) => row.receipt_id === req.receipt_id);
      const updated = receiptDetail({
        ...(target ?? receiptSummary({ receipt_id: req.receipt_id })),
        status: "confirmed",
        ...(req.raw_shop_name === undefined ? {} : { raw_shop_name: req.raw_shop_name }),
        total_amount: req.total_amount,
        version: req.expected_version + 1,
        lines: singleLine,
      });
      page1Rows = page1Rows.map((row) => (row.receipt_id === updated.receipt_id ? updated : row));
      page2Rows = page2Rows.map((row) => (row.receipt_id === updated.receipt_id ? updated : row));
      return ok({ ok: true, receipt: updated });
    });
    vi.mocked(clients.rApi.deleteReceipt).mockImplementation((req) => {
      page1Rows = page1Rows.filter((row) => row.receipt_id !== req.receipt_id);
      page2Rows = page2Rows.filter((row) => row.receipt_id !== req.receipt_id);
      return ok({ ok: true, deleted: true });
    });

    let simulatedScrollY = 400;
    let p2aDocTop = 580;
    const origScrollY = Object.getOwnPropertyDescriptor(window, "scrollY");
    Object.defineProperty(window, "scrollY", {
      configurable: true,
      get: () => simulatedScrollY,
    });
    const scrollToSpy = vi.spyOn(window, "scrollTo").mockImplementation((...args: unknown[]) => {
      if (typeof args[1] === "number") simulatedScrollY = args[1];
    });

    try {
      await renderReceipts(clients);
      await click(button("Load more"));
      expect(host.querySelectorAll(".budget-receipt-row")).toHaveLength(4);
      const listElementBefore = host.querySelector("ul.budget-entry-list");
      const p2aEl = host.querySelector<HTMLElement>('li[data-receipt-id="p2-a"]');
      if (p2aEl === null) throw new Error("expected p2-a row");
      vi.spyOn(p2aEl, "getBoundingClientRect").mockImplementation(() => {
        const top = p2aDocTop - simulatedScrollY;
        return {
          top,
          bottom: top + 40,
          left: 0,
          right: 200,
          width: 200,
          height: 40,
          x: 0,
          y: top,
          toJSON: () => ({}),
        };
      });

      // Edit & confirm a page-2 receipt -> quiet refresh keeps list mounted, shows subtle Refreshing…,
      // refetches all loaded pages sequentially, and uses anchor-based scroll restore (keeping p2-a's
      // viewport top constant at 180 without overcorrecting when both docTop and window.scrollY shift).
      await click(openDetailById("p2-a"));
      const drawer = dialog();
      await click(inputByName("auto_advance_on_confirm", drawer));
      await typeInto(inputByName("raw_shop_name", drawer), "Page Two Shop A Edited");
      holdNextPage1 = true;

      await act(async () => {
        button("Confirm receipt", drawer).click();
        await Promise.resolve();
      });
      await settle();

      expect(host.querySelector("ul.budget-entry-list")).toBe(listElementBefore);
      expect(host.textContent).toContain("Refreshing…");
      expect(host.textContent).not.toContain("Loading documents…");

      // Simulate row above shrinking by 60px (docTop 580 -> 520) AND scroll drifting to 100.
      // Live top is 420 (delta = +240 from saved anchorTop 180); target scrollY must be 100 + 240 = 340 (not 400 + 240 = 640).
      p2aDocTop = 520;
      simulatedScrollY = 100;
      await act(async () => {
        pendingRefreshPage1.resolve({ ok: true, data: { receipts: page1Rows, next_cursor: "cursor-2" } });
        await Promise.resolve();
      });
      await settle();

      expect(host.querySelector("ul.budget-entry-list")).toBe(listElementBefore);
      expect(host.querySelectorAll(".budget-receipt-row")).toHaveLength(4);
      expect(host.textContent).toContain("Page One Shop A");
      expect(host.textContent).toContain("Page Two Shop A Edited");
      expect(host.textContent).toContain("Page Two Shop B");
      expect(button("Load more").disabled).toBe(false);
      expect(scrollToSpy).toHaveBeenCalledWith(0, 340);
      expect(simulatedScrollY).toBe(340);
      expect(p2aEl.getBoundingClientRect().top).toBe(180);

      // Delete another page-2 receipt -> anchor row p2-b disappears, so fallback restores absolute scrollY (340).
      await click(button("Close", dialog()));
      await click(openDetailById("p2-b"));
      const pendingDeleteRefresh = deferred<ReceiptListResult>();
      vi.mocked(clients.rApi.receiptsList).mockImplementationOnce(() => pendingDeleteRefresh.promise);
      await click(button("Delete document", dialog()));
      await act(async () => {
        button("Confirm deletion", dialog()).click();
        await Promise.resolve();
      });
      await settle();
      simulatedScrollY = 0;
      await act(async () => {
        pendingDeleteRefresh.resolve({ ok: true, data: { receipts: page1Rows, next_cursor: "cursor-2" } });
        await Promise.resolve();
      });
      await settle();

      expect(host.querySelector("ul.budget-entry-list")).toBe(listElementBefore);
      expect(host.querySelectorAll(".budget-receipt-row")).toHaveLength(3);
      expect(host.textContent).toContain("Page One Shop A");
      expect(host.textContent).toContain("Page Two Shop A Edited");
      expect(host.textContent).not.toContain("Page Two Shop B");
      expect(simulatedScrollY).toBe(340);
    } finally {
      if (origScrollY !== undefined) {
        Object.defineProperty(window, "scrollY", origScrollY);
      } else {
        Object.defineProperty(window, "scrollY", { configurable: true, value: 0 });
      }
    }
  });

  it("ignores stale list/search/refresh generations and coalesces concurrent refreshes via dirty flag", async () => {
    const singleLine = receiptDetail().lines.slice(0, 1);
    const d1 = receiptSummary({ receipt_id: "d1", status: "needs_review", raw_shop_name: "Lidl First", total_amount: "12.50", version: 1 });
    const d2 = receiptSummary({ receipt_id: "d2", status: "needs_review", raw_shop_name: "Lidl Second", total_amount: "12.50", version: 1 });
    const d2Edited = { ...d2, status: "confirmed" as const, raw_shop_name: "Lidl Fresh", version: 2 };

    const staleSearchResp = deferred<ReceiptListResult>();
    const staleRefreshResp = deferred<ReceiptListResult>();
    let searchCalls = 0;
    const clients = fakeClients([d1, d2]);
    vi.mocked(clients.rApi.receiptsSearch).mockImplementation(() => {
      searchCalls += 1;
      if (searchCalls === 1) return staleSearchResp.promise;
      if (searchCalls === 2) return staleRefreshResp.promise;
      return ok<ReceiptListResult>({ receipts: [d2Edited], next_cursor: null });
    });
    vi.mocked(clients.rApi.receiptDetail).mockImplementation((id) => {
      const match = id === "d1" ? d1 : d2;
      return ok<ReceiptDetailResult>({ receipt: receiptDetail({ ...match, lines: singleLine }), candidates: [] });
    });
    vi.mocked(clients.rApi.confirmReceipt).mockImplementation(() => ok({
      ok: true,
      receipt: receiptDetail({ ...d2Edited, lines: singleLine }),
    }));

    await renderReceipts(clients);

    await typeInto(inputByName("query"), "Lidl");
    await submit(host.querySelector(".budget-range"));
    expect(searchCalls).toBe(1);

    await act(async () => {
      staleSearchResp.resolve({ ok: true, data: { receipts: [d1, d2], next_cursor: null } });
      await Promise.resolve();
    });
    await settle();
    expect(host.querySelectorAll(".budget-receipt-row")).toHaveLength(2);

    // Delete d1 -> starts quiet refetch #1 (staleRefreshResp, pending).
    await click(openDetailById("d1"));
    await click(button("Delete document", dialog()));
    await click(button("Confirm deletion", dialog()));
    expect(searchCalls).toBe(2);

    // While quiet refetch #1 is pending, edit & confirm d2 -> marks dirty instead of firing an overlapping request.
    await click(openDetailById("d2"));
    await click(inputByName("auto_advance_on_confirm", dialog()));
    await typeInto(inputByName("raw_shop_name", dialog()), "Lidl Fresh");
    await act(async () => {
      button("Confirm receipt", dialog()).click();
      await Promise.resolve();
    });
    await settle();
    expect(searchCalls).toBe(2);

    // Older refresh resolves with stale [d1, d2] -> discarded because dirty; runs exactly one follow-up refresh returning [d2Edited].
    await act(async () => {
      staleRefreshResp.resolve({ ok: true, data: { receipts: [d1, d2], next_cursor: null } });
      await Promise.resolve();
    });
    await settle();
    expect(searchCalls).toBe(3);
    expect(host.querySelector('[data-receipt-id="d1"]')).toBeNull();
    expect(host.textContent).toContain("Lidl Fresh");
    expect(host.textContent).not.toContain("Lidl Second");
  });

  it("navigates Previous and Next by ID, auto-loads the next page at the end, and advances to same index or closes when current ID disappears", async () => {
    const r1 = receiptSummary({ receipt_id: "nav-1", status: "imported", raw_shop_name: "First Receipt", version: 1 });
    const r2 = receiptSummary({ receipt_id: "nav-2", status: "imported", raw_shop_name: "Second Receipt", version: 1 });
    const r3 = receiptSummary({ receipt_id: "nav-3", status: "imported", raw_shop_name: "Third Page-2 Receipt", version: 1 });

    let page1 = [r1, r2];
    let page2 = [r3];
    const clients = fakeClients(page1);
    vi.mocked(clients.rApi.receiptsList).mockImplementation((req) => {
      const filterImported = req?.status === "imported";
      const p1 = filterImported ? page1.filter((row) => row.status === "imported") : page1;
      const p2 = filterImported ? page2.filter((row) => row.status === "imported") : page2;
      if (req?.cursor === "nav-cursor-2") {
        return ok<ReceiptListResult>({ receipts: p2, next_cursor: null });
      }
      return ok<ReceiptListResult>({ receipts: p1, next_cursor: p2.length > 0 ? "nav-cursor-2" : null });
    });
    vi.mocked(clients.rApi.receiptDetail).mockImplementation((id) => {
      const match = [...page1, ...page2].find((row) => row.receipt_id === id) ?? r1;
      return ok<ReceiptDetailResult>({ receipt: receiptDetail(match), candidates: [] });
    });
    vi.mocked(clients.rApi.confirmReceipt).mockImplementation((req) => {
      const all = [...page1, ...page2];
      const match = all.find((row) => row.receipt_id === req.receipt_id) ?? r1;
      const updated = receiptDetail({ ...match, status: "confirmed", version: req.expected_version + 1 });
      page1 = page1.map((row) => (row.receipt_id === req.receipt_id ? updated : row));
      page2 = page2.map((row) => (row.receipt_id === req.receipt_id ? updated : row));
      return ok({ ok: true, receipt: updated });
    });

    await renderReceipts(clients);
    await click(openDetailById("nav-1"));
    expect(dialog().textContent).toContain("First Receipt");
    expect(button("Previous", dialog()).disabled).toBe(true);
    expect(button("Next", dialog()).disabled).toBe(false);

    await click(button("Next", dialog()));
    expect(dialog().textContent).toContain("Second Receipt");
    expect(button("Previous", dialog()).disabled).toBe(false);
    expect(button("Next", dialog()).disabled).toBe(false);

    await click(button("Previous", dialog()));
    expect(dialog().textContent).toContain("First Receipt");

    await click(button("Next", dialog()));
    expect(dialog().textContent).toContain("Second Receipt");

    // Reaching the end of page 1 with next_cursor present auto-loads page 2 and navigates to r3.
    await click(button("Next", dialog()));
    expect(clients.rApi.receiptsList).toHaveBeenLastCalledWith({ limit: 50, cursor: "nav-cursor-2" });
    expect(dialog().textContent).toContain("Third Page-2 Receipt");
    expect(button("Next", dialog()).disabled).toBe(true);
    expect(button("Previous", dialog()).disabled).toBe(false);
    expect(host.querySelectorAll(".budget-receipt-row")).toHaveLength(3);
    await click(button("Close", dialog()));

    // Under "Imported" filter with auto-advance OFF, confirming the middle row removes it on refresh
    // and selects the row now at the same index (nav-3); confirming the tail falls back to nav-1; confirming the last closes.
    await choose(selectByName("status"), "imported");
    await settle();
    await click(button("Load more"));
    expect(host.querySelectorAll(".budget-receipt-row")).toHaveLength(3);

    await click(openDetailById("nav-2"));
    await click(inputByName("auto_advance_on_confirm", dialog()));
    await click(button("Confirm receipt", dialog()));
    expect(host.querySelector('[data-receipt-id="nav-2"]')).toBeNull();
    expect(dialog().textContent).toContain("Third Page-2 Receipt");
    expect(button("Previous", dialog()).disabled).toBe(false);
    expect(button("Next", dialog()).disabled).toBe(true);

    await click(button("Confirm receipt", dialog()));
    expect(dialog().textContent).toContain("First Receipt");
    expect(button("Previous", dialog()).disabled).toBe(true);
    expect(button("Next", dialog()).disabled).toBe(true);

    await click(button("Confirm receipt", dialog()));
    expect(host.querySelector('[role="dialog"]')).toBeNull();
    expect(host.querySelectorAll(".budget-receipt-row")).toHaveLength(0);
  });

  it("auto-advances after refresh, persists the toggle, closes with All confirmed when none remain, and never loops on unchanged statuses", async () => {
    const storeMap = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (key: string) => storeMap.get(key) ?? null,
      setItem: (key: string, value: string) => { storeMap.set(key, value); },
      removeItem: (key: string) => { storeMap.delete(key); },
    });

    const r1 = receiptSummary({ receipt_id: "adv-1", status: "imported", raw_shop_name: "First Imported", version: 1 });
    let rowsState: ReceiptSummary[] = [
      r1,
      receiptSummary({ receipt_id: "adv-2", status: "confirmed", raw_shop_name: "Already Confirmed", version: 2 }),
      receiptSummary({ receipt_id: "adv-3", status: "draft", raw_shop_name: "Third Draft", version: 1 }),
      receiptSummary({ receipt_id: "adv-4", status: "needs_review", raw_shop_name: "Fourth Page-2 Review", version: 1 }),
    ];

    const clients = fakeClients(rowsState.slice(0, 3));
    vi.mocked(clients.rApi.receiptsList).mockImplementation((req) => {
      if (req?.cursor === "adv-page-2") {
        return ok<ReceiptListResult>({ receipts: rowsState.slice(3), next_cursor: null });
      }
      return ok<ReceiptListResult>({ receipts: rowsState.slice(0, 3), next_cursor: "adv-page-2" });
    });
    vi.mocked(clients.rApi.receiptDetail).mockImplementation((id) => {
      const match = rowsState.find((row) => row.receipt_id === id) ?? r1;
      return ok<ReceiptDetailResult>({ receipt: receiptDetail(match), candidates: [] });
    });
    vi.mocked(clients.rApi.confirmReceipt).mockImplementation((req) => {
      const match = rowsState.find((row) => row.receipt_id === req.receipt_id) ?? r1;
      const updated = receiptDetail({ ...match, status: "confirmed", version: req.expected_version + 1 });
      rowsState = rowsState.map((row) => (row.receipt_id === req.receipt_id ? updated : row));
      return ok({ ok: true, receipt: updated });
    });

    await renderReceipts(clients, { locale: "pl" });
    await click(openDetailById("adv-1", "Szczegóły"));
    const checkbox = inputByName("auto_advance_on_confirm", dialog());
    expect(checkbox.checked).toBe(true);
    expect(dialog().textContent).toContain("Przejdź do następnego po zatwierdzeniu");

    // Confirm adv-1 -> waits for refresh, skips confirmed adv-2, opens draft adv-3.
    await click(button("Zatwierdź dokument", dialog()));
    expect(dialog().textContent).toContain("Third Draft");

    // Confirm adv-3 -> auto-loads page 2 and opens needs_review adv-4.
    await click(button("Zatwierdź dokument", dialog()));
    expect(clients.rApi.receiptsList).toHaveBeenLastCalledWith({ limit: 50, cursor: "adv-page-2" });
    expect(dialog().textContent).toContain("Fourth Page-2 Review");

    // Confirm adv-4 -> none left -> closes dialog with "Wszystko zatwierdzone." while keeping all 4 rows loaded.
    await click(button("Zatwierdź dokument", dialog()));
    expect(host.querySelector('[role="dialog"]')).toBeNull();
    expect(host.textContent).toContain("Wszystko zatwierdzone.");
    expect(host.querySelectorAll(".budget-receipt-row")).toHaveLength(4);

    // Toggling checkbox OFF persists in localStorage and survives remount.
    await click(openDetailById("adv-1", "Szczegóły"));
    await click(inputByName("auto_advance_on_confirm", dialog()));
    expect(storeMap.get("hestia.receipts.autoAdvance")).toBe("0");
    await click(button("Zamknij", dialog()));

    await act(async () => {
      root.unmount();
      root = createRoot(host);
      await Promise.resolve();
    });
    await renderReceipts(clients, { locale: "en" });
    await click(openDetailById("adv-1"));
    expect(inputByName("auto_advance_on_confirm", dialog()).checked).toBe(false);
    await click(button("Confirm receipt", dialog()));
    expect(dialog().textContent).toContain("First Imported");

    // Throwing localStorage falls back cleanly to ON without error.
    vi.stubGlobal("localStorage", {
      getItem: () => { throw new Error("blocked"); },
      setItem: () => { throw new Error("blocked"); },
      removeItem: () => { throw new Error("blocked"); },
    });
    await act(async () => {
      root.unmount();
      root = createRoot(host);
      await Promise.resolve();
    });
    await renderReceipts(clients, { locale: "en" });
    await click(openDetailById("adv-1"));
    expect(inputByName("auto_advance_on_confirm", dialog()).checked).toBe(true);
    await click(button("Close", dialog()));

    // No-loop guard: two linked needs_review receipts whose status stays unchanged stop after one full pass.
    const u1 = receiptSummary({
      receipt_id: "u1",
      status: "needs_review",
      linked: true,
      linked_account_id: "local-a",
      linked_entry_ref: "a".repeat(64),
      raw_shop_name: "Linked Review One",
      version: 1,
    });
    const u2 = receiptSummary({
      receipt_id: "u2",
      status: "needs_review",
      linked: true,
      linked_account_id: "local-a",
      linked_entry_ref: "b".repeat(64),
      raw_shop_name: "Linked Review Two",
      version: 1,
    });
    const noLoopClients = fakeClients([u1, u2]);
    vi.mocked(noLoopClients.rApi.receiptDetail).mockImplementation((id) => {
      const match = id === "u1" ? u1 : u2;
      return ok<ReceiptDetailResult>({ receipt: receiptDetail(match), candidates: [] });
    });
    vi.mocked(noLoopClients.rApi.confirmReceipt).mockImplementation((req) => {
      const match = req.receipt_id === "u1" ? u1 : u2;
      return ok({
        ok: true,
        receipt: receiptDetail({ ...match, status: "needs_review", linked: true, version: req.expected_version + 1 }),
      });
    });

    await renderReceipts(noLoopClients, { revision: 1 });
    await click(openDetailById("u1"));
    expect(dialog().textContent).toContain("Linked Review One");

    await click(linkRadio("unlink", dialog()));
    await click(button("Confirm receipt", dialog()));
    expect(dialog().textContent).toContain("Linked Review Two");

    await click(linkRadio("unlink", dialog()));
    await click(button("Confirm receipt", dialog()));
    expect(dialog().textContent).toContain("Linked Review Two");
    expect(dialog().textContent).not.toContain("Linked Review One");
    expect(host.textContent).not.toContain("All confirmed.");

    await click(linkRadio("unlink", dialog()));
    await click(button("Confirm receipt", dialog()));
    expect(dialog().textContent).toContain("Linked Review Two");
    expect(dialog().textContent).not.toContain("Linked Review One");
    expect(host.textContent).not.toContain("All confirmed.");
  });

  it("mounts ReceiptsView without throwing and renders the loaded receipts list", async () => {
    const clients = fakeClients([
      receiptSummary({ receipt_id: "mount-1", raw_shop_name: "Clean Mount Store", version: 1 }),
    ]);
    const onAccessLoss = vi.fn(() => false);
    await act(async () => {
      expect(() => {
        root.render(
          <ReceiptsView
            receiptsApi={clients.rApi}
            actorKey="owner-mount"
            accounts={accounts}
            connections={[connection]}
            locale="en"
            revision={0}
            onAccessLoss={onAccessLoss}
          />,
        );
      }).not.toThrow();
      await Promise.resolve();
    });
    await settle();
    expect(host.querySelector('[data-receipt-id="mount-1"]')?.textContent).toContain("Clean Mount Store");
  });

  it("closes group controller before refresh so grouping under Unlinked filter falls back cleanly to same-index survivor", async () => {
    const g1 = receiptSummary({ receipt_id: "ug-1", status: "confirmed", linked: false, raw_shop_name: "First Unlinked", total_amount: "20.00", version: 2 });
    const g2 = receiptSummary({ receipt_id: "ug-2", status: "confirmed", linked: false, raw_shop_name: "Second Unlinked", total_amount: "30.00", version: 2 });
    const g3 = receiptSummary({ receipt_id: "ug-3", status: "confirmed", linked: false, raw_shop_name: "Third Surviving Unlinked", total_amount: "15.00", version: 1 });
    const groupTx = {
      account_id: "local-a",
      entry_reference: "c".repeat(64),
      amount: "50.00",
      currency: "PLN",
      credit_debit_indicator: "DBIT" as const,
      transaction_date: "2026-11-14",
      booking_date: "2026-11-14",
      value_date: "2026-11-14",
      merchant_token_hit: false,
      match_strength: "none" as const,
      date_distance: 0,
      evidence_fingerprint: "fp-ug-1",
      candidate_token: "tok-ug-1",
    };

    let unlinkedRows: ReceiptSummary[] = [g1, g2, g3];
    const clients = fakeClients(unlinkedRows);
    vi.mocked(clients.rApi.receiptsList).mockImplementation((req) => ok<ReceiptListResult>({
      receipts: req?.linked === false ? unlinkedRows : [g1, g2, g3],
      next_cursor: null,
    }));
    vi.mocked(clients.rApi.receiptDetail).mockImplementation((id) => {
      const match = [g1, g2, g3].find((row) => row.receipt_id === id) ?? g1;
      return ok<ReceiptDetailResult>({ receipt: receiptDetail(match), candidates: [], group_candidates: [] });
    });
    clients.rApi.receiptGroupCandidates = vi.fn<NonNullable<ReceiptsApi["receiptGroupCandidates"]>>(() => ok<ReceiptGroupCandidatesResult>({
      anchor_receipt_id: "ug-1",
      transactions: [groupTx],
      proposals: [],
      eligible_receipts: [g1, g2],
      eligible_evidence: [],
      no_bank_data: false,
    }));
    clients.rApi.linkReceiptGroup = vi.fn<NonNullable<ReceiptsApi["linkReceiptGroup"]>>(() => {
      unlinkedRows = [g3];
      return ok<ReceiptGroupLinkResult>({
        ok: true,
        group: { group_id: "grp-ug", group_version: 1, group_status: "confirmed", member_count: 2, evidence_receipt_id: null, fee_amount: null },
        members: [{ ...g1, linked: true, version: 3 }, { ...g2, linked: true, version: 3 }],
        evidence: null,
        linked_account_id: "local-a",
        linked_entry_ref: "c".repeat(64),
      });
    });

    await renderReceipts(clients);
    await choose(selectByName("linked"), "unlinked");
    await settle();
    expect(host.querySelectorAll(".budget-receipt-row")).toHaveLength(3);

    await click(openDetailById("ug-1"));
    await click(button("Group with other receipts", dialog()));
    const groupModal = [...host.querySelectorAll<HTMLElement>('[role="dialog"]')].at(-1);
    if (groupModal === undefined) throw new Error("Expected group controller modal");
    const txRadio = groupModal.querySelector<HTMLInputElement>('input[name="group_tx_choice"][value="0"]');
    if (txRadio === null) throw new Error("Expected tx choice");
    await click(txRadio);
    for (const box of groupModal.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')) {
      if (!box.checked) await click(box);
    }
    await click(button("Confirm group link", groupModal));

    // Group controller closed before refresh, ug-1 disappeared under Unlinked filter -> drawer switches to ug-3.
    expect(host.querySelectorAll('[role="dialog"]')).toHaveLength(1);
    expect(dialog().textContent).toContain("Third Surviving Unlinked");
    expect(dialog().textContent).not.toContain("First Unlinked");
    expect(host.textContent).toContain("Receipt group linked.");
    expect(host.querySelectorAll(".budget-receipt-row")).toHaveLength(1);
  });

  it("applies the shared post-refresh fallback when confirming while a debounced search is loading or when search omits the open receipt", async () => {
    const singleLine = receiptDetail().lines.slice(0, 1);
    const r1 = receiptSummary({ receipt_id: "srch-1", status: "needs_review", raw_shop_name: "Lidl One", total_amount: "12.50", version: 1 });
    const r2 = receiptSummary({ receipt_id: "srch-2", status: "needs_review", raw_shop_name: "Lidl Two", total_amount: "12.50", version: 1 });
    const r3 = receiptSummary({ receipt_id: "srch-3", status: "needs_review", raw_shop_name: "Biedronka Three", total_amount: "12.50", version: 1 });

    const pendingFirstSearch = deferred<ReceiptListResult>();
    let searchCallCount = 0;
    const clients = fakeClients([r1, r2, r3]);
    vi.mocked(clients.rApi.receiptsSearch).mockImplementation((req) => {
      searchCallCount += 1;
      if (req.query === "EmptyQuery") {
        return ok<ReceiptListResult>({ receipts: [], next_cursor: null });
      }
      if (searchCallCount === 1) return pendingFirstSearch.promise;
      return ok<ReceiptListResult>({ receipts: [r3], next_cursor: null });
    });
    vi.mocked(clients.rApi.receiptDetail).mockImplementation((id) => {
      const match = [r1, r2, r3].find((row) => row.receipt_id === id) ?? r1;
      return ok<ReceiptDetailResult>({ receipt: receiptDetail({ ...match, lines: singleLine }), candidates: [] });
    });
    vi.mocked(clients.rApi.confirmReceipt).mockImplementation((req) => ok({
      ok: true,
      receipt: receiptDetail({ ...r2, status: "confirmed", version: req.expected_version + 1, lines: singleLine }),
    }));

    await renderReceipts(clients);
    await click(openDetailById("srch-2"));
    await click(inputByName("auto_advance_on_confirm", dialog()));

    // Start a search that stays in loading state while confirming srch-2.
    await typeInto(inputByName("query"), "Biedronka");
    await submit(host.querySelector(".budget-range"));
    expect(searchCallCount).toBe(1);

    // Confirming while search is loading supersedes it with a post-confirm search returning [r3],
    // and the shared post-refresh handler switches the drawer from omitted srch-2 to r3.
    await click(button("Confirm receipt", dialog()));
    expect(searchCallCount).toBe(2);
    expect(dialog().textContent).toContain("Biedronka Three");
    expect(dialog().textContent).not.toContain("Lidl Two");

    // Late resolution of the superseded search is ignored.
    await act(async () => {
      pendingFirstSearch.resolve({ ok: true, data: { receipts: [r1, r2], next_cursor: null } });
      await Promise.resolve();
    });
    await settle();
    expect(dialog().textContent).toContain("Biedronka Three");

    // A subsequent search returning empty results closes the drawer via the same shared fallback.
    await typeInto(inputByName("query"), "EmptyQuery");
    await submit(host.querySelector(".budget-range"));
    expect(host.querySelector('[role="dialog"]')).toBeNull();
  });

  it("coalesces multi-participant settlement refetches into one in-flight plus at most one dirty follow-up and cancels on unmount", async () => {
    const store = createPendingReceiptStore();
    const m1 = receiptSummary({ receipt_id: "coal-1", raw_shop_name: "Group Member 1", version: 1 });
    const m2 = receiptSummary({ receipt_id: "coal-2", raw_shop_name: "Group Member 2", version: 1 });
    const ev1 = receiptSummary({ receipt_id: "coal-ev", raw_shop_name: "Group Evidence", version: 1 });
    const freshRow = receiptSummary({ receipt_id: "coal-1", raw_shop_name: "Group Member 1 Unlinked", version: 2 });

    const t1 = store.tryAcquire("coal-1", "group_unlink");
    const t2 = store.tryAcquire("coal-2", "group_unlink");
    const t3 = store.tryAcquire("coal-ev", "group_unlink");
    if (t1 === null || t2 === null || t3 === null) throw new Error("expected locks");

    const refresh1 = deferred<ReceiptListResult>();
    let listCallCount = 0;
    const clients = fakeClients([m1, m2, ev1]);
    vi.mocked(clients.rApi.receiptsList).mockImplementation(() => {
      listCallCount += 1;
      if (listCallCount === 1) return ok<ReceiptListResult>({ receipts: [m1, m2, ev1], next_cursor: null });
      if (listCallCount === 2) return refresh1.promise;
      return ok<ReceiptListResult>({ receipts: [freshRow], next_cursor: null });
    });

    const onAccessLoss = vi.fn(() => false);
    await act(async () => {
      root.render(
        <ReceiptsView
          receiptsApi={clients.rApi}
          pendingStore={store}
          actorKey="owner-coalesce"
          accounts={accounts}
          connections={[connection]}
          locale="en"
          revision={0}
          onAccessLoss={onAccessLoss}
        />,
      );
      await Promise.resolve();
    });
    await settle();
    expect(listCallCount).toBe(1);

    // Release all 3 participant locks synchronously -> only 1 refresh starts while the others mark dirty.
    await act(async () => {
      store.release("coal-1", t1, "group_unlinked");
      store.release("coal-2", t2, "group_unlinked");
      store.release("coal-ev", t3, "group_unlinked");
      await Promise.resolve();
    });
    await settle();
    expect(listCallCount).toBe(2);

    // Completing refresh #1 triggers exactly 1 dirty follow-up pass (#3) and renders the fresh row.
    await act(async () => {
      refresh1.resolve({ ok: true, data: { receipts: [m1, m2, ev1], next_cursor: null } });
      await Promise.resolve();
    });
    await settle();
    expect(listCallCount).toBe(3);
    expect(host.textContent).toContain("Group Member 1 Unlinked");

    // Unmounting while a dirty refresh is in flight cancels the follow-up pass completely.
    const u1 = store.tryAcquire("coal-1", "group_unlink");
    const u2 = store.tryAcquire("coal-2", "group_unlink");
    if (u1 === null || u2 === null) throw new Error("expected locks");
    const refreshBeforeUnmount = deferred<ReceiptListResult>();
    vi.mocked(clients.rApi.receiptsList).mockImplementationOnce(() => refreshBeforeUnmount.promise);

    await act(async () => {
      store.release("coal-1", u1, "group_unlinked");
      store.release("coal-2", u2, "group_unlinked");
      await Promise.resolve();
    });
    expect(listCallCount).toBe(3); // mockImplementationOnce intercepted call #4
    const callsBeforeUnmount = vi.mocked(clients.rApi.receiptsList).mock.calls.length;

    await act(async () => {
      root.unmount();
      root = createRoot(host);
      refreshBeforeUnmount.resolve({ ok: true, data: { receipts: [freshRow], next_cursor: null } });
      await Promise.resolve();
    });
    await settle();
    expect(vi.mocked(clients.rApi.receiptsList).mock.calls.length).toBe(callsBeforeUnmount);
  });

  it("keeps the drawer open when search omits a receipt with a dirty draft and applies deferred reconciliation on discard or save", async () => {
    const singleLine = receiptDetail().lines.slice(0, 1);
    const r1 = receiptSummary({ receipt_id: "dirty-s1", status: "needs_review", raw_shop_name: "Lidl Draft One", total_amount: "12.50", version: 1 });
    const r2 = receiptSummary({ receipt_id: "dirty-s2", status: "needs_review", raw_shop_name: "Biedronka Survivor", total_amount: "12.50", version: 1 });
    const r3 = receiptSummary({ receipt_id: "dirty-s3", status: "needs_review", raw_shop_name: "Carrefour Last", total_amount: "12.50", version: 1 });

    let searchResults: ReceiptSummary[] = [r2];
    const clients = fakeClients([r1, r2, r3]);
    vi.mocked(clients.rApi.receiptsSearch).mockImplementation(() => ok<ReceiptListResult>({
      receipts: searchResults,
      next_cursor: null,
    }));
    vi.mocked(clients.rApi.receiptDetail).mockImplementation((id) => {
      const match = [r1, r2, r3].find((row) => row.receipt_id === id) ?? r1;
      return ok<ReceiptDetailResult>({ receipt: receiptDetail({ ...match, lines: singleLine }), candidates: [] });
    });
    vi.mocked(clients.rApi.confirmReceipt).mockImplementation((req) => {
      const match = [r1, r2, r3].find((row) => row.receipt_id === req.receipt_id) ?? r1;
      return ok({
        ok: true,
        receipt: receiptDetail({
          ...match,
          status: "confirmed",
          raw_shop_name: req.raw_shop_name ?? match.raw_shop_name,
          version: req.expected_version + 1,
          lines: singleLine,
        }),
      });
    });

    await renderReceipts(clients);
    await click(openDetailById("dirty-s1"));
    await click(inputByName("auto_advance_on_confirm", dialog()));

    // Edit r1 so its draft is dirty, then search for something that omits r1.
    await typeInto(inputByName("raw_shop_name", dialog()), "Unsaved Merchant Edit");
    await typeInto(inputByName("query"), "Biedronka");
    await submit(host.querySelector(".budget-range"));

    // List updated to [r2], but drawer MUST stay on r1 with unsaved edits preserved and Prev/Next disabled.
    expect(host.querySelectorAll(".budget-receipt-row")).toHaveLength(1);
    expect(host.querySelector('[data-receipt-id="dirty-s2"]')).not.toBeNull();
    expect(dialog().textContent).toContain("Lidl Draft One");
    expect(inputByName("raw_shop_name", dialog()).value).toBe("Unsaved Merchant Edit");
    expect(button("Previous", dialog()).disabled).toBe(true);
    expect(button("Next", dialog()).disabled).toBe(true);

    // Reverting the edit (discard) makes the draft clean -> deferred reconciliation switches cleanly to r2.
    await typeInto(inputByName("raw_shop_name", dialog()), "Lidl Draft One");
    await settle();
    expect(dialog().textContent).toContain("Biedronka Survivor");
    expect(dialog().textContent).not.toContain("Lidl Draft One");

    // Also verify saving a dirty omitted receipt reconciles cleanly after save (closes when search returns empty).
    await typeInto(inputByName("raw_shop_name", dialog()), "Biedronka Edited");
    searchResults = [];
    await typeInto(inputByName("query"), "NoMatchAtAll");
    await submit(host.querySelector(".budget-range"));
    expect(host.querySelectorAll(".budget-receipt-row")).toHaveLength(0);
    expect(dialog().textContent).toContain("Biedronka Survivor");
    expect(inputByName("raw_shop_name", dialog()).value).toBe("Biedronka Edited");

    await click(button("Confirm receipt", dialog()));
    expect(clients.rApi.confirmReceipt).toHaveBeenLastCalledWith(expect.objectContaining({
      receipt_id: "dirty-s2",
      raw_shop_name: "Biedronka Edited",
    }));
    expect(host.querySelector('[role="dialog"]')).toBeNull();
  });

  it("skips post-save detail reload when the user starts a newer dirty draft during the refetch and preserves the draft", async () => {
    const singleLine = receiptDetail().lines.slice(0, 1);
    const v1 = receiptSummary({ receipt_id: "draft-v", status: "needs_review", raw_shop_name: "Initial Shop", total_amount: "12.50", version: 1 });
    const v2 = { ...v1, status: "confirmed" as const, raw_shop_name: "Saved V2 Shop", version: 2 };
    const v3Server = { ...v2, raw_shop_name: "Server Concurrent V3", version: 3 };

    const pendingPostSaveRefresh = deferred<ReceiptListResult>();
    let listCalls = 0;
    let detailCalls = 0;
    const clients = fakeClients([v1]);
    vi.mocked(clients.rApi.receiptsList).mockImplementation(() => {
      listCalls += 1;
      if (listCalls === 1) return ok<ReceiptListResult>({ receipts: [v1], next_cursor: null });
      if (listCalls === 2) return pendingPostSaveRefresh.promise;
      return ok<ReceiptListResult>({ receipts: [v3Server], next_cursor: null });
    });
    vi.mocked(clients.rApi.receiptDetail).mockImplementation(() => {
      detailCalls += 1;
      if (detailCalls === 1) return ok<ReceiptDetailResult>({ receipt: receiptDetail({ ...v1, lines: singleLine }), candidates: [] });
      return ok<ReceiptDetailResult>({ receipt: receiptDetail({ ...v3Server, lines: singleLine }), candidates: [] });
    });
    vi.mocked(clients.rApi.confirmReceipt)
      .mockImplementationOnce(() => ok({ ok: true, receipt: receiptDetail({ ...v2, lines: singleLine }) }))
      .mockImplementationOnce(() => fail(409, "budget_receipt_conflict"));

    await renderReceipts(clients);
    await click(openDetailById("draft-v"));
    await click(inputByName("auto_advance_on_confirm", dialog()));

    // Save v1 -> v2 while post-save list refetch stays pending.
    await typeInto(inputByName("raw_shop_name", dialog()), "Saved V2 Shop");
    await act(async () => {
      button("Confirm receipt", dialog()).click();
      await Promise.resolve();
    });
    await settle();
    expect(dialog().textContent).toContain("Version 2");
    expect(detailCalls).toBe(1);

    // Start editing a newer v3 draft BEFORE the post-save refetch settles.
    await typeInto(inputByName("raw_shop_name", dialog()), "Newer Unsaved V3 Draft");

    // Post-save refetch finishes -> MUST skip detail reload and keep "Newer Unsaved V3 Draft" intact.
    await act(async () => {
      pendingPostSaveRefresh.resolve({ ok: true, data: { receipts: [v2], next_cursor: null } });
      await Promise.resolve();
    });
    await settle();
    expect(detailCalls).toBe(1);
    expect(inputByName("raw_shop_name", dialog()).value).toBe("Newer Unsaved V3 Draft");

    // Submitting the newer draft uses version 2; on 409 conflict, draft MUST stay intact with an inline
    // conflict message and explicit reload button, discarding the draft ONLY on clicking Load latest version.
    await click(button("Confirm receipt", dialog()));
    expect(clients.rApi.confirmReceipt).toHaveBeenLastCalledWith(expect.objectContaining({
      receipt_id: "draft-v",
      expected_version: 2,
      raw_shop_name: "Newer Unsaved V3 Draft",
    }));
    expect(detailCalls).toBe(1);
    expect(dialog().querySelector('[role="alert"]')?.textContent).toContain("This document changed in the meantime.");
    expect(inputByName("raw_shop_name", dialog()).value).toBe("Newer Unsaved V3 Draft");

    await click(button("Load latest version", dialog()));
    expect(detailCalls).toBe(2);
    expect(dialog().textContent).toContain("Version 3");
    expect(inputByName("raw_shop_name", dialog()).value).toBe("Server Concurrent V3");
  });

  it("tracks refresh reconciliation per drawer ID so confirming A, closing A, and grouping B still reconciles B", async () => {
    const singleLine = receiptDetail().lines.slice(0, 1);
    const a = receiptSummary({ receipt_id: "per-id-a", status: "needs_review", linked: false, raw_shop_name: "Receipt Alpha", total_amount: "12.50", version: 1 });
    const b = receiptSummary({ receipt_id: "per-id-b", status: "confirmed", linked: false, raw_shop_name: "Receipt Beta", total_amount: "20.00", version: 2 });
    const b2 = receiptSummary({ receipt_id: "per-id-b2", status: "confirmed", linked: false, raw_shop_name: "Receipt Beta Partner", total_amount: "30.00", version: 2 });
    const c = receiptSummary({ receipt_id: "per-id-c", status: "confirmed", linked: false, raw_shop_name: "Receipt Gamma Survivor", total_amount: "15.00", version: 1 });
    const groupTx = {
      account_id: "local-a",
      entry_reference: "d".repeat(64),
      amount: "50.00",
      currency: "PLN",
      credit_debit_indicator: "DBIT" as const,
      transaction_date: "2026-11-14",
      booking_date: "2026-11-14",
      value_date: "2026-11-14",
      merchant_token_hit: false,
      match_strength: "none" as const,
      date_distance: 0,
      evidence_fingerprint: "fp-per-id",
      candidate_token: "tok-per-id",
    };

    const pendingConfirmARefresh = deferred<ReceiptListResult>();
    let unlinkedListCalls = 0;
    const clients = fakeClients([a, b, b2, c]);
    vi.mocked(clients.rApi.receiptsList).mockImplementation((req) => {
      if (req?.linked !== false) {
        return ok<ReceiptListResult>({ receipts: [a, b, b2, c], next_cursor: null });
      }
      unlinkedListCalls += 1;
      if (unlinkedListCalls === 1) {
        return ok<ReceiptListResult>({ receipts: [a, b, b2, c], next_cursor: null });
      }
      if (unlinkedListCalls === 2) {
        return pendingConfirmARefresh.promise;
      }
      return ok<ReceiptListResult>({ receipts: [{ ...a, status: "confirmed", version: 2 }, c], next_cursor: null });
    });
    vi.mocked(clients.rApi.receiptDetail).mockImplementation((id) => {
      const match = [a, b, b2, c].find((row) => row.receipt_id === id) ?? a;
      return ok<ReceiptDetailResult>({ receipt: receiptDetail({ ...match, lines: singleLine }), candidates: [], group_candidates: [] });
    });
    vi.mocked(clients.rApi.confirmReceipt).mockImplementation((req) => ok({
      ok: true,
      receipt: receiptDetail({ ...a, status: "confirmed", version: req.expected_version + 1, lines: singleLine }),
    }));
    clients.rApi.receiptGroupCandidates = vi.fn<NonNullable<ReceiptsApi["receiptGroupCandidates"]>>(() => ok<ReceiptGroupCandidatesResult>({
      anchor_receipt_id: "per-id-b",
      transactions: [groupTx],
      proposals: [],
      eligible_receipts: [b, b2],
      eligible_evidence: [],
      no_bank_data: false,
    }));
    clients.rApi.linkReceiptGroup = vi.fn<NonNullable<ReceiptsApi["linkReceiptGroup"]>>(() => ok<ReceiptGroupLinkResult>({
      ok: true,
      group: { group_id: "grp-beta", group_version: 1, group_status: "confirmed", member_count: 2, evidence_receipt_id: null, fee_amount: null },
      members: [{ ...b, linked: true, version: 3 }, { ...b2, linked: true, version: 3 }],
      evidence: null,
      linked_account_id: "local-a",
      linked_entry_ref: "d".repeat(64),
    }));

    await renderReceipts(clients);
    await choose(selectByName("linked"), "unlinked");
    await settle();
    expect(unlinkedListCalls).toBe(1);

    // 1. Confirm A -> starts quiet refetch #1 (deferred only for A, stays pending).
    await click(openDetailById("per-id-a"));
    await click(inputByName("auto_advance_on_confirm", dialog()));
    await act(async () => {
      button("Confirm receipt", dialog()).click();
      await Promise.resolve();
    });
    await settle();
    expect(unlinkedListCalls).toBe(2);

    // 2. Close A while refresh #1 is still in flight, then open B and group B + B2.
    await click(button("Close", dialog()));
    await click(openDetailById("per-id-b"));
    await click(button("Group with other receipts", dialog()));
    const groupModal = [...host.querySelectorAll<HTMLElement>('[role="dialog"]')].at(-1);
    if (groupModal === undefined) throw new Error("Expected group controller modal");
    const txRadio = groupModal.querySelector<HTMLInputElement>('input[name="group_tx_choice"][value="0"]');
    if (txRadio === null) throw new Error("Expected tx choice");
    await click(txRadio);
    for (const box of groupModal.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')) {
      if (!box.checked) await click(box);
    }
    await click(button("Confirm group link", groupModal));
    // Coalesced onto A's in-flight refresh (#2), so no overlapping request fired yet.
    expect(unlinkedListCalls).toBe(2);

    // 3. Resolve A's refresh -> dirty follow-up pass (#3) runs and MUST reconcile B (switching to C).
    await act(async () => {
      pendingConfirmARefresh.resolve({ ok: true, data: { receipts: [{ ...a, status: "confirmed", version: 2 }, b, b2, c], next_cursor: null } });
      await Promise.resolve();
    });
    await settle();
    await settle();

    expect(unlinkedListCalls).toBe(3);
    expect(host.querySelectorAll('[role="dialog"]')).toHaveLength(1);
    expect(dialog().textContent).toContain("Receipt Gamma Survivor");
    expect(dialog().textContent).not.toContain("Receipt Beta");
    expect(host.textContent).toContain("Receipt group linked.");
  });

  it("cancels async page-boundary Next navigation if the draft becomes dirty while the next page loads", async () => {
    const r1 = receiptSummary({ receipt_id: "nav-dirty-1", status: "needs_review", raw_shop_name: "Boundary Receipt One", total_amount: "12.50", version: 1 });
    const r2 = receiptSummary({ receipt_id: "nav-dirty-2", status: "needs_review", raw_shop_name: "Page Two Receipt", total_amount: "12.50", version: 1 });
    const pendingPage2 = deferred<ReceiptListResult>();

    const clients = fakeClients([r1]);
    vi.mocked(clients.rApi.receiptsList).mockImplementation((req) => {
      if (req?.cursor === "boundary-cursor-2") return pendingPage2.promise;
      return ok<ReceiptListResult>({ receipts: [r1], next_cursor: "boundary-cursor-2" });
    });
    vi.mocked(clients.rApi.receiptDetail).mockImplementation((id) => {
      const match = id === "nav-dirty-2" ? r2 : r1;
      return ok<ReceiptDetailResult>({ receipt: receiptDetail(match), candidates: [] });
    });

    await renderReceipts(clients);
    await click(openDetailById("nav-dirty-1"));
    expect(dialog().textContent).toContain("Boundary Receipt One");
    expect(button("Next", dialog()).disabled).toBe(false);

    // Click Next at the page boundary -> starts async page 2 load.
    await act(async () => {
      button("Next", dialog()).click();
      await Promise.resolve();
    });

    // While page 2 is still in flight, edit the current receipt so its draft becomes dirty.
    await typeInto(inputByName("raw_shop_name", dialog()), "Edited During Page Load");

    // Resolve page 2 -> list loads r2, but navigation MUST cancel at commit time and keep r1's dirty draft.
    await act(async () => {
      pendingPage2.resolve({ ok: true, data: { receipts: [r2], next_cursor: null } });
      await Promise.resolve();
    });
    await settle();

    expect(host.querySelectorAll(".budget-receipt-row")).toHaveLength(2);
    expect(dialog().textContent).toContain("Boundary Receipt One");
    expect(dialog().textContent).not.toContain("Page Two Receipt");
    expect(inputByName("raw_shop_name", dialog()).value).toBe("Edited During Page Load");
  });

  it("keeps the dirty draft on 409 save conflict with Polish conflict message and reloads only when Wczytaj aktualną wersję is clicked", async () => {
    const singleLine = receiptDetail().lines.slice(0, 1);
    const v1 = receiptSummary({ receipt_id: "pl-409", status: "needs_review", raw_shop_name: "Sklep Pierwotny", total_amount: "12.50", version: 1 });
    const v2Server = { ...v1, raw_shop_name: "Sklep Z Serwera V2", version: 2 };

    let detailCalls = 0;
    const clients = fakeClients([v1]);
    vi.mocked(clients.rApi.receiptDetail).mockImplementation(() => {
      detailCalls += 1;
      if (detailCalls === 1) return ok<ReceiptDetailResult>({ receipt: receiptDetail({ ...v1, lines: singleLine }), candidates: [] });
      return ok<ReceiptDetailResult>({ receipt: receiptDetail({ ...v2Server, lines: singleLine }), candidates: [] });
    });
    vi.mocked(clients.rApi.confirmReceipt).mockImplementationOnce(() => fail(409, "budget_receipt_conflict"));

    await renderReceipts(clients, { locale: "pl" });
    await click(openDetailById("pl-409", "Szczegóły"));
    await typeInto(inputByName("raw_shop_name", dialog()), "Moja Edycja Szkicu");

    await click(button("Zatwierdź dokument", dialog()));

    // 409 MUST keep user's draft untouched and never auto-reload detail.
    expect(detailCalls).toBe(1);
    expect(inputByName("raw_shop_name", dialog()).value).toBe("Moja Edycja Szkicu");
    expect(dialog().querySelector('[role="alert"]')?.textContent).toContain("Dokument zmienił się w międzyczasie");
    const reloadBtn = button("Wczytaj aktualną wersję", dialog());
    expect(reloadBtn.disabled).toBe(false);

    // Explicit click discards draft and loads the latest server version.
    await click(reloadBtn);
    expect(detailCalls).toBe(2);
    expect(dialog().textContent).toContain("Wersja 2");
    expect(inputByName("raw_shop_name", dialog()).value).toBe("Sklep Z Serwera V2");
    expect(dialog().querySelector('[role="alert"]')).toBeNull();
  });

  it("processes a dirty request that joins after pass publish and before coalescer clears in-flight state", async () => {
    const store = createPendingReceiptStore();
    const initialRow = receiptSummary({ receipt_id: "interleave-1", raw_shop_name: "Initial Row", version: 1 });
    const pass1Row = receiptSummary({ receipt_id: "interleave-1", raw_shop_name: "Pass One Stale Row", version: 2 });
    const pass2Row = receiptSummary({ receipt_id: "interleave-1", raw_shop_name: "Pass Two Fresh Row", version: 3 });

    const t1 = store.tryAcquire("interleave-1", "confirm");
    const t2 = store.tryAcquire("interleave-2", "confirm");
    if (t1 === null || t2 === null) throw new Error("expected locks");

    const pass1Deferred = deferred<ReceiptListResult>();
    let listCalls = 0;
    const clients = fakeClients([initialRow]);
    vi.mocked(clients.rApi.receiptsList).mockImplementation(() => {
      listCalls += 1;
      if (listCalls === 1) return ok<ReceiptListResult>({ receipts: [initialRow], next_cursor: null });
      if (listCalls === 2) return pass1Deferred.promise;
      return ok<ReceiptListResult>({ receipts: [pass2Row], next_cursor: null });
    });

    const onAccessLoss = vi.fn(() => false);
    await act(async () => {
      root.render(
        <ReceiptsView
          receiptsApi={clients.rApi}
          pendingStore={store}
          actorKey="owner-interleave"
          accounts={accounts}
          connections={[connection]}
          locale="en"
          revision={0}
          onAccessLoss={onAccessLoss}
        />,
      );
      await Promise.resolve();
    });
    await settle();
    expect(listCalls).toBe(1);

    // Release lock 1 -> starts refresh pass #1 (listCalls becomes 2, awaiting pass1Deferred).
    await act(async () => {
      store.release("interleave-1", t1, "confirmed");
      await Promise.resolve();
    });
    expect(listCalls).toBe(2);

    // Interleave: resolve pass #1 (microtask 1 runs runPass publish & return, queueing microtask 3 for outer loop),
    // while microtask 2 releases lock 2 right AFTER pass #1 published and BEFORE outer loop resumes to clear activeRefreshRef.
    await act(async () => {
      pass1Deferred.resolve({ ok: true, data: { receipts: [pass1Row], next_cursor: null } });
      void Promise.resolve().then(() => {
        store.release("interleave-2", t2, "confirmed");
      });
      await Promise.resolve();
    });
    await settle();

    // Outer loop MUST observe dirty === true synchronously before clearing inFlight and run pass #2.
    expect(listCalls).toBe(3);
    expect(host.textContent).toContain("Pass Two Fresh Row");
    expect(host.textContent).not.toContain("Pass One Stale Row");

    // Once pass #2 finishes and clears inFlight in the same synchronous tick, a subsequent settlement starts a brand-new run.
    const t3 = store.tryAcquire("interleave-3", "confirm");
    if (t3 === null) throw new Error("expected lock 3");
    await act(async () => {
      store.release("interleave-3", t3, "confirmed");
      await Promise.resolve();
    });
    await settle();
    expect(listCalls).toBe(4);
  });
});
