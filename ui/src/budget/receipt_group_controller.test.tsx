// Synthetic data only: no real receipts, merchants, NIPs, or bank transactions.
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { loadLocale } from "../i18n";
import type { BudgetResult } from "./api";
import { account, connection } from "./fixtures";
import { ReceiptGroupController } from "./receipt_group_controller";
import type { ReceiptGroupControllerProps, ReceiptGroupControllerTarget } from "./receipt_group_controller";
import { REDACTED_VALUE, createPendingReceiptStore } from "./receipts_model";
import type {
  PendingReceiptStore,
  ReceiptDetail,
  ReceiptGroupCandidate,
  ReceiptGroupCandidatesResult,
  ReceiptGroupLinkResult,
  ReceiptGroupTransactionCandidate,
  ReceiptSummary,
} from "./receipts_model";

function ok<T>(data: T): Promise<BudgetResult<T>> { return Promise.resolve({ ok: true, data }); }

interface Deferred<T> { promise: Promise<BudgetResult<T>>; resolve: (value: BudgetResult<T>) => void; }
function deferred<T>(): Deferred<T> {
  let resolve: (value: BudgetResult<T>) => void = () => undefined;
  const promise = new Promise<BudgetResult<T>>((done) => { resolve = done; });
  return { promise, resolve };
}

function summaryRow(overrides: Partial<ReceiptSummary> = {}): ReceiptSummary {
  return {
    receipt_id: "rec-a",
    status: "confirmed",
    document_kind: "receipt",
    source_kind: "local_ocr",
    receipt_kind: "purchase",
    receipt_date: "2026-11-14",
    receipt_time: "12:15",
    raw_shop_name: "Synthetic Alpha",
    raw_nip: "5261040828",
    shop_id: null,
    chain_key: null,
    total_amount: "20.00",
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

function detailRow(summary: ReceiptSummary): ReceiptDetail {
  return {
    ...summary,
    payment_evidence: [],
    lines: summary.document_kind === "invoice" ? [] : [
      {
        line_no: 1,
        line_kind: "item",
        raw_name: "Synthetic Line",
        raw_name_key: "synthetic line",
        ptu_code: "A",
        quantity: "1",
        raw_unit: "szt",
        unit_price_raw: summary.total_amount ?? "20.00",
        gross_line_total: summary.total_amount ?? "20.00",
        discount_amount: "0",
        net_line_total: summary.total_amount ?? "20.00",
      },
    ],
  };
}

function txCandidate(overrides: Partial<ReceiptGroupTransactionCandidate> = {}): ReceiptGroupTransactionCandidate {
  return {
    account_id: "local-a",
    entry_reference: "secret-entry-ref-never-render",
    amount: "50.00",
    currency: "PLN",
    credit_debit_indicator: "DBIT",
    booking_date: "2026-11-15",
    value_date: "2026-11-15",
    transaction_date: "2026-11-14",
    merchant_token_hit: false,
    match_strength: "none",
    date_distance: 0,
    evidence_fingerprint: "secret-fingerprint-never-render",
    candidate_token: "secret-candidate-token-v1",
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

async function click(el: HTMLElement): Promise<void> {
  await act(async () => {
    el.click();
    await Promise.resolve();
  });
  await settle();
}

function makeControllerApi(overrides: Partial<ReceiptGroupControllerProps["api"]> = {}): ReceiptGroupControllerProps["api"] {
  return {
    receiptDetail: vi.fn<ReceiptGroupControllerProps["api"]["receiptDetail"]>(),
    confirmReceipt: vi.fn<ReceiptGroupControllerProps["api"]["confirmReceipt"]>(),
    receiptGroupCandidates: vi.fn<NonNullable<ReceiptGroupControllerProps["api"]["receiptGroupCandidates"]>>(),
    linkReceiptGroup: vi.fn<NonNullable<ReceiptGroupControllerProps["api"]["linkReceiptGroup"]>>(),
    isAuthorizationLocked: vi.fn(() => false),
    ...overrides,
  };
}

async function renderController(
  props: Partial<ReceiptGroupControllerProps> & {
    api: ReceiptGroupControllerProps["api"];
    target: ReceiptGroupControllerTarget;
  },
) {
  const store: PendingReceiptStore = props.store ?? createPendingReceiptStore();
  const onAccessLoss = props.onAccessLoss ?? vi.fn(() => false);
  const onClose = props.onClose ?? vi.fn();
  const onPartialConfirm = props.onPartialConfirm ?? vi.fn();
  const onGroupLinked = props.onGroupLinked ?? vi.fn();
  await act(async () => {
    root.render(
      <ReceiptGroupController
        api={props.api}
        store={store}
        target={props.target}
        actorKey={props.actorKey ?? "owner"}
        accounts={accounts}
        connections={[connection]}
        locale="en"
        revision={props.revision ?? 0}
        onAccessLoss={onAccessLoss}
        onClose={onClose}
        onPartialConfirm={onPartialConfirm}
        onGroupLinked={onGroupLinked}
      />,
    );
    await Promise.resolve();
  });
  await settle();
  return { store, onAccessLoss, onClose, onPartialConfirm, onGroupLinked };
}

function button(text: string): HTMLButtonElement {
  const found = [...host.querySelectorAll("button")].find((b) => b.textContent === text);
  if (found === undefined) throw new Error(`Button "${text}" not found in: ${host.textContent}`);
  return found;
}

function checkbox(labelSubstr: string): HTMLInputElement {
  const found = [...host.querySelectorAll("label")].find((l) => l.textContent.includes(labelSubstr));
  const input = found?.querySelector<HTMLInputElement>('input[type="checkbox"]');
  if (input == null) throw new Error(`Checkbox "${labelSubstr}" not found`);
  return input;
}

function txRadio(index = 0): HTMLInputElement {
  const radios = [...host.querySelectorAll<HTMLInputElement>('input[name="group_tx_choice"]')];
  const radio = radios[index];
  if (radio === undefined) throw new Error(`Tx radio #${String(index)} missing`);
  return radio;
}

describe("ReceiptGroupController", () => {
  it("never preselects transactions or members in candidates mode, redacts names, and hides internal tokens", async () => {
    const anchor = summaryRow({ receipt_id: "rec-a", raw_shop_name: REDACTED_VALUE, total_amount: "20.00" });
    const peer = summaryRow({ receipt_id: "rec-b", raw_shop_name: "Synthetic Beta", total_amount: "30.00" });
    const api = makeControllerApi({
      receiptGroupCandidates: vi.fn(() => ok<ReceiptGroupCandidatesResult>({
        anchor_receipt_id: "rec-a",
        transactions: [txCandidate()],
        proposals: [],
        eligible_receipts: [anchor, peer],
        eligible_evidence: [],
        no_bank_data: false,
      })),
    });

    await renderController({ api, target: { kind: "candidates", anchor } });

    expect(api.receiptGroupCandidates).toHaveBeenCalledExactlyOnceWith("rec-a");
    expect(txRadio(0).checked).toBe(false);
    expect(host.querySelectorAll('input[type="checkbox"]')).toHaveLength(0);

    await click(txRadio(0));
    expect(host.textContent).toContain("Removed");
    expect(host.textContent).toContain("Synthetic Beta");
    expect(host.textContent).not.toContain(REDACTED_VALUE);
    expect(host.innerHTML).not.toContain("secret-candidate-token-v1");
    expect(host.innerHTML).not.toContain("secret-fingerprint-never-render");
    expect(host.innerHTML).not.toContain("secret-entry-ref-never-render");
    expect([...host.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')].every((el) => !el.checked)).toBe(true);
  });

  it("sequentially pre-confirms imported rows (link: null), refreshes candidate_token, and submits group/link", async () => {
    const confirmedA = summaryRow({ receipt_id: "rec-a", status: "confirmed", version: 2, total_amount: "20.00" });
    const importedB = summaryRow({
      receipt_id: "imp-b", status: "imported", source_kind: "email", document_kind: "invoice", version: 3, total_amount: "30.00",
    });
    const refreshedTx = txCandidate({
      evidence_fingerprint: "changed-fingerprint-v2",
      candidate_token: "fresh-token-v2",
    });

    const api = makeControllerApi({
      receiptGroupCandidates: vi.fn()
        .mockResolvedValueOnce({
          ok: true,
          data: {
            anchor_receipt_id: "rec-a",
            transactions: [txCandidate()],
            proposals: [],
            eligible_receipts: [confirmedA, importedB],
            eligible_evidence: [],
          },
        })
        .mockResolvedValueOnce({
          ok: true,
          data: {
            anchor_receipt_id: "rec-a",
            transactions: [refreshedTx],
            proposals: [],
            eligible_receipts: [confirmedA, { ...importedB, status: "confirmed", version: 4 }],
            eligible_evidence: [],
          },
        }),
      receiptDetail: vi.fn<ReceiptGroupControllerProps["api"]["receiptDetail"]>(
        (id) => ok({ receipt: detailRow({ ...importedB, receipt_id: id }), candidates: [] }),
      ),
      confirmReceipt: vi.fn<ReceiptGroupControllerProps["api"]["confirmReceipt"]>(() => ok({
        ok: true,
        receipt: detailRow({ ...importedB, status: "confirmed", version: 4 }),
      })),
      linkReceiptGroup: vi.fn<NonNullable<ReceiptGroupControllerProps["api"]["linkReceiptGroup"]>>(() => ok<ReceiptGroupLinkResult>({
        ok: true,
        group: { group_id: "grp-1", group_version: 1, group_status: "confirmed", member_count: 2, evidence_receipt_id: null, fee_amount: null },
        members: [],
        evidence: null,
        linked_account_id: "local-a",
        linked_entry_ref: "secret-entry-ref-never-render",
      })),
    });

    const { store, onGroupLinked } = await renderController({
      api,
      target: { kind: "candidates", anchor: confirmedA },
    });

    await click(txRadio(0));
    const boxes = [...host.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')];
    expect(boxes).toHaveLength(2);
    for (const box of boxes) {
      await click(box);
    }
    expect(host.textContent).toContain("Selected imported documents will be confirmed individually without a bank link");

    await click(button("Confirm group link"));

    expect(api.confirmReceipt).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
      receipt_id: "imp-b",
      expected_version: 3,
      link: null,
    }));
    expect(api.receiptGroupCandidates).toHaveBeenCalledTimes(2);
    expect(api.linkReceiptGroup).toHaveBeenCalledExactlyOnceWith({
      link: {
        account_id: "local-a",
        entry_reference: "secret-entry-ref-never-render",
        candidate_token: "fresh-token-v2",
      },
      members: [
        { receipt_id: "rec-a", expected_version: 2 },
        { receipt_id: "imp-b", expected_version: 4 },
      ],
    });
    expect(onGroupLinked).toHaveBeenCalledTimes(1);
    expect(store.snapshot().size).toBe(0);
  });

  it("halts cleanly without calling group/link or auto-retrying when a partial imported confirm fails", async () => {
    const imp1 = summaryRow({
      receipt_id: "imp-1", status: "imported", source_kind: "email", document_kind: "invoice",
      raw_shop_name: "Shop One", total_amount: "20.00", version: 1,
    });
    const imp2 = summaryRow({
      receipt_id: "imp-2", status: "imported", source_kind: "email", document_kind: "invoice",
      raw_shop_name: "Shop Two", total_amount: "30.00", version: 1,
    });

    const api = makeControllerApi({
      receiptGroupCandidates: vi.fn(() => ok<ReceiptGroupCandidatesResult>({
        anchor_receipt_id: "imp-1",
        transactions: [txCandidate()],
        proposals: [],
        eligible_receipts: [imp1, imp2],
        eligible_evidence: [],
      })),
      receiptDetail: vi.fn((id) => ok({
        receipt: detailRow(id === "imp-1" ? imp1 : imp2),
        candidates: [],
      })),
      confirmReceipt: vi.fn()
        .mockResolvedValueOnce({ ok: true, data: { ok: true, receipt: detailRow({ ...imp1, status: "confirmed", version: 2 }) } })
        .mockResolvedValueOnce({ ok: false, status: 0, error: "budget_network" }),
      linkReceiptGroup: vi.fn(),
    });

    const { store, onPartialConfirm, onGroupLinked } = await renderController({
      api,
      target: { kind: "bulk", anchor: imp1, selectedRows: [imp1, imp2] },
    });

    await click(txRadio(0));
    await click(checkbox("Shop One"));
    await click(checkbox("Shop Two"));
    await click(button("Confirm group link"));

    expect(api.confirmReceipt).toHaveBeenCalledTimes(2);
    expect(api.linkReceiptGroup).not.toHaveBeenCalled();
    expect(onGroupLinked).not.toHaveBeenCalled();
    expect(onPartialConfirm).toHaveBeenCalledExactlyOnceWith(["imp-1"]);
    expect(host.textContent).toContain("Confirmed without bank link before error: 1.");
    expect(store.snapshot().size).toBe(0);
  });

  it("enforces 14-day date span, authorization lock, and concurrent store locks before network mutations", async () => {
    const early = summaryRow({ receipt_id: "r-early", raw_shop_name: "Early Shop", receipt_date: "2026-10-01", total_amount: "20.00" });
    const late = summaryRow({ receipt_id: "r-late", raw_shop_name: "Late Shop", receipt_date: "2026-10-20", total_amount: "30.00" });
    const store = createPendingReceiptStore();
    const api = makeControllerApi({
      receiptGroupCandidates: vi.fn(() => ok<ReceiptGroupCandidatesResult>({
        anchor_receipt_id: "r-early",
        transactions: [txCandidate()],
        proposals: [],
        eligible_receipts: [early, late],
        eligible_evidence: [],
      })),
      linkReceiptGroup: vi.fn(),
    });

    await renderController({ api, store, target: { kind: "candidates", anchor: early } });
    await click(txRadio(0));
    await click(checkbox("Early Shop"));
    await click(checkbox("Late Shop"));
    await click(button("Confirm group link"));

    expect(api.linkReceiptGroup).not.toHaveBeenCalled();
    expect(host.textContent).toContain("Selected documents must be within 14 days of each other.");

    const sameDay1 = summaryRow({ receipt_id: "r-1", raw_shop_name: "Same One", receipt_date: "2026-11-14", total_amount: "20.00" });
    const sameDay2 = summaryRow({ receipt_id: "r-2", raw_shop_name: "Same Two", receipt_date: "2026-11-14", total_amount: "30.00" });
    const lockMock = vi.fn(() => false);
    const sameDayApi = makeControllerApi({
      receiptGroupCandidates: vi.fn<NonNullable<ReceiptGroupControllerProps["api"]["receiptGroupCandidates"]>>(() => ok({
        anchor_receipt_id: "r-1",
        transactions: [txCandidate()],
        proposals: [],
        eligible_receipts: [sameDay1, sameDay2],
        eligible_evidence: [],
      })),
      linkReceiptGroup: vi.fn<NonNullable<ReceiptGroupControllerProps["api"]["linkReceiptGroup"]>>(),
      isAuthorizationLocked: lockMock,
    });
    await renderController({ api: sameDayApi, store, target: { kind: "candidates", anchor: sameDay1 } });
    await click(txRadio(0));
    await click(checkbox("Same One"));
    await click(checkbox("Same Two"));

    const foreignToken = store.tryAcquire("r-2", "confirm");
    await click(button("Confirm group link"));
    expect(sameDayApi.linkReceiptGroup).not.toHaveBeenCalled();
    expect(host.textContent).toContain("One of the selected documents has another action in progress.");
    if (foreignToken !== null) store.release("r-2", foreignToken);

    lockMock.mockReturnValue(true);
    await click(button("Confirm group link"));
    expect(sameDayApi.linkReceiptGroup).not.toHaveBeenCalled();
    expect(store.snapshot().size).toBe(0);
  });

  it("drops stale data and closes cleanly on principal change, proposal expansion, and access loss", async () => {
    const r1 = summaryRow({ receipt_id: "r-1", raw_shop_name: "Proposal One", total_amount: "20.00" });
    const r2 = summaryRow({ receipt_id: "r-2", raw_shop_name: "Proposal Two", total_amount: "30.00" });
    const proposal: ReceiptGroupCandidate = {
      ...txCandidate(),
      fee_amount: null,
      evidence_receipt_id: null,
      members_sum: "50.00",
      member_receipt_ids: ["r-1", "r-2"],
      member_receipts: [r1, r2],
      evidence: null,
    };
    const pendingCandidates = deferred<ReceiptGroupCandidatesResult>();
    const api = makeControllerApi({
      receiptGroupCandidates: vi.fn(() => pendingCandidates.promise),
    });
    const onAccessLoss = vi.fn((res: { ok: boolean; status?: number }) => !res.ok && res.status === 401);

    await renderController({
      api,
      target: { kind: "proposal", anchor: r1, proposal },
      actorKey: "owner",
      onAccessLoss,
    });

    await click(button("Show all transactions and eligible receipts"));
    expect(api.receiptGroupCandidates).toHaveBeenCalledWith("r-1");

    // Switching principal invalidates the old request and triggers a fresh load for the new actorKey.
    await act(async () => {
      root.render(
        <ReceiptGroupController
          api={api}
          store={createPendingReceiptStore()}
          target={{ kind: "proposal", anchor: r1, proposal }}
          actorKey="other-owner"
          accounts={accounts}
          connections={[connection]}
          locale="en"
          revision={0}
          onAccessLoss={onAccessLoss}
          onClose={vi.fn()}
          onPartialConfirm={vi.fn()}
          onGroupLinked={vi.fn()}
        />,
      );
      await Promise.resolve();
    });
    pendingCandidates.resolve({ ok: false, status: 401, error: "budget_unauthorized" });
    await settle();
    expect(onAccessLoss).toHaveBeenCalled();
  });
});
