import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BudgetResult } from "../api";
import type { ReceiptsApi } from "../receipts_api";
import type { ReceiptListResult, ReceiptSummary } from "../receipts_model";
import { entry } from "./fixtures";
import { transactionRowKey } from "./model";
import type { AnalyticsTransaction } from "./model";
import { useTransactionDocuments } from "./transaction_documents";

const reference = " synthetic/łódź ";
const referenceDigest = "0223c81111ee37ff944e20e35347eec646b45a05ba50a0290895858b63aaa209";
const rows = [entry(reference), entry(reference, { account_id: "acc-2" })];

function receipt(id: string): ReceiptSummary {
  return {
    receipt_id: id, shop_id: null, chain_key: null, raw_shop_name: "Synthetic Grocery", raw_nip: null,
    receipt_kind: "purchase", receipt_date: "2026-11-03", receipt_time: null, total_amount: "12.5",
    currency: "PLN", payment_method: "card", status: "confirmed", source_kind: "email", document_kind: "invoice",
    linked: true, linked_account_id: "acc-1", linked_entry_ref: referenceDigest,
    version: 1, created_at: 1, updated_at: 1, image_available: false, image_expires_at: null,
    raw_text_expires_at: null, math_delta: "0", card_last4: null, due_date: null,
    evidence_for_receipt_id: null, fee_amount: null, forwarded_manually: false, forwarded_sender_domain: null,
    gateway: null, gateway_status: null, has_document: true, import_expires_at: null, invoice_number: null,
    payee_host: null,
  };
}

function page(receipts: ReceiptSummary[], nextCursor: string | null = null): BudgetResult<ReceiptListResult> {
  return { ok: true, data: { receipts, next_cursor: nextCursor } };
}

function Probe({ read, entries, revision }: {
  read: ReceiptsApi["receiptsList"] | undefined; entries: readonly AnalyticsTransaction[]; revision: number;
}) {
  const result = useTransactionDocuments(read, [...entries], revision);
  return <output data-state={result.state}>{entries.map((row) =>
    (result.byRow.get(transactionRowKey(row)) ?? []).map((item) => item.receipt_id).join(",") || "–").join("|")}</output>;
}

let host: HTMLElement;
let root: Root;

beforeEach(() => {
  host = document.createElement("div");
  root = createRoot(host);
  vi.stubGlobal("crypto", { subtle: {
    digest: (algorithm: string, data: Uint8Array) => {
      expect(algorithm).toBe("SHA-256");
      expect(new TextDecoder().decode(data)).toBe(reference);
      const bytes = Uint8Array.from({ length: 32 }, (_, i) => Number.parseInt(referenceDigest.slice(i * 2, i * 2 + 2), 16));
      return Promise.resolve(bytes.buffer);
    },
  } });
});

afterEach(async () => {
  await act(async () => { root.unmount(); await Promise.resolve(); });
  vi.unstubAllGlobals();
});

async function show(read: ReceiptsApi["receiptsList"] | undefined, revision = 0): Promise<void> {
  await act(async () => {
    root.render(<Probe read={read} entries={rows} revision={revision} />);
    await Promise.resolve();
  });
}

const state = () => host.querySelector("output")?.dataset.state;

describe("linked expense documents", () => {
  it("joins all pages to exact UTF-8 references and keeps grouped receipts together on the right account", async () => {
    const digest = vi.spyOn(crypto.subtle, "digest");
    const read = vi.fn<ReceiptsApi["receiptsList"]>();
    read.mockResolvedValueOnce(page([receipt("doc-a")], "later"));
    read.mockResolvedValueOnce(page([receipt("doc-b")]));

    await show(read);

    expect(read.mock.calls).toEqual([[{ linked: true, limit: 200 }], [{ linked: true, limit: 200, cursor: "later" }]]);
    expect(state()).toBe("ready");
    expect(host.textContent).toBe("doc-a,doc-b|–");
    await act(async () => {
      root.render(<Probe read={read} entries={[...rows, entry(reference, { account_id: "acc-3" })]} revision={0} />);
      await Promise.resolve();
    });
    expect(host.textContent).toBe("doc-a,doc-b|–|–");
    expect(digest).toHaveBeenCalledOnce();
    digest.mockRestore();
  });

  it("does not publish a partial result when a later page is denied", async () => {
    const read = vi.fn<ReceiptsApi["receiptsList"]>();
    read.mockResolvedValueOnce(page([receipt("doc-a")], "later"));
    read.mockResolvedValueOnce({ ok: false, status: 403, error: "budget_forbidden" });

    await show(read);

    expect(state()).toBe("unavailable");
    expect(host.textContent).toBe("–|–");
  });

  it("ignores superseded pages and clears documents if access is removed", async () => {
    const read = vi.fn<ReceiptsApi["receiptsList"]>();
    let publishOld: (value: BudgetResult<ReceiptListResult>) => void = () => undefined;
    read.mockReturnValueOnce(new Promise((resolve) => { publishOld = resolve; }));
    read.mockResolvedValueOnce(page([receipt("current")]));

    await show(read);
    expect(state()).toBe("loading");
    await show(read, 1);
    await act(async () => { publishOld(page([receipt("old")], "stale")); await Promise.resolve(); });
    expect(read).toHaveBeenCalledTimes(2);
    expect(state()).toBe("ready");
    expect(host.textContent).toBe("current|–");

    await show(undefined, 1);
    expect(state()).toBe("unavailable");
    expect(host.textContent).toBe("–|–");
  });

  it("keeps availability unknown when the browser cannot hash the references", async () => {
    vi.stubGlobal("crypto", {});
    const read = vi.fn<ReceiptsApi["receiptsList"]>();

    await show(read);

    expect(read).not.toHaveBeenCalled();
    expect(state()).toBe("unavailable");
    expect(host.textContent).toBe("–|–");
  });
});
