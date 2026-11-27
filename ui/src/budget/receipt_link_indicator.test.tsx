import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { loadLocale } from "../i18n";
import { account, connection } from "./fixtures";
import { LinkedTransactionIndicator } from "./receipt_link_indicator";
import { ReceiptsView } from "./receipts";
import type { ReceiptsApi } from "./receipts_api";
import type { ReceiptSummary } from "./receipts_model";

const fsModule = "node:fs";
const { readFileSync } = (await import(/* @vite-ignore */ fsModule)) as {
  readFileSync: (path: string, encoding: "utf8") => string;
};

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function ok<T>(data: T) {
  return Promise.resolve({ ok: true as const, data });
}

function receiptRow(id: string, changes: Partial<ReceiptSummary> = {}): ReceiptSummary {
  return {
    receipt_id: id,
    shop_id: null,
    chain_key: null,
    raw_shop_name: "Synthetic Shop",
    raw_nip: null,
    receipt_kind: "purchase",
    receipt_date: "2026-11-10",
    receipt_time: null,
    total_amount: "42.50",
    currency: "PLN",
    payment_method: "card",
    status: "confirmed",
    source_kind: "email",
    linked: true,
    linked_account_id: "local-a",
    linked_entry_ref: "a".repeat(64),
    version: 1,
    created_at: 1,
    updated_at: 1,
    image_available: false,
    image_expires_at: null,
    raw_text_expires_at: null,
    math_delta: "0",
    document_kind: "receipt",
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
    link_group: null,
    linked_transaction: null,
    ...changes,
  };
}

let host: HTMLDivElement;
let root: Root;

beforeEach(async () => {
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
});

describe("LinkedTransactionIndicator and ReceiptsView bank link indicators", () => {
  it("renders bank icon, short account label, booking date, and accessible tooltip", async () => {
    await act(async () => {
      root.render(
        <LinkedTransactionIndicator
          transaction={{
            account_label: "mBank · eKonto ••1234",
            booking_date: "2026-11-10",
            amount: "42.50",
            currency: "PLN",
          }}
          locale="en"
        />,
      );
      await Promise.resolve();
    });

    const badge = host.querySelector<HTMLButtonElement>("button.budget-receipt-tx-link");
    expect(badge).not.toBeNull();
    expect(badge?.type).toBe("button");
    expect(badge?.getAttribute("aria-expanded")).toBe("false");
    expect(badge?.textContent).toContain("🏦");
    expect(badge?.textContent).toContain("mBank · eKonto ••1234 · 2026-11-10");
    expect(badge?.getAttribute("title")).toContain("Linked bank transaction:");
    expect(badge?.getAttribute("title")).toContain("mBank · eKonto ••1234");
    expect(badge?.getAttribute("title")).toContain("2026-11-10");
    expect(badge?.getAttribute("aria-label")).toBe(badge?.getAttribute("title"));

    await act(async () => {
      badge?.click();
      await Promise.resolve();
    });
    expect(badge?.getAttribute("aria-expanded")).toBe("true");
    expect(badge?.querySelector(".budget-link-detail")?.textContent).toContain("42.50 PLN");

    await act(async () => {
      badge?.click();
      await Promise.resolve();
    });
    expect(badge?.getAttribute("aria-expanded")).toBe("false");
  });

  it("wraps expanded details at full width at <=480px so date and amount stay visible with long account labels", async () => {
    const longLabel = "Very Long Synthetic Cooperative Bank · Premium Multi-Currency Account ••1234";
    await act(async () => {
      root.render(
        <LinkedTransactionIndicator
          transaction={{
            account_label: longLabel,
            booking_date: "2026-11-10",
            amount: "42.50",
            currency: "PLN",
          }}
          locale="en"
        />,
      );
      await Promise.resolve();
    });

    const badge = host.querySelector<HTMLButtonElement>("button.budget-receipt-tx-link");
    expect(badge).not.toBeNull();
    expect(badge?.querySelector(".budget-link-primary")?.textContent).toBe(longLabel);
    expect([...badge?.querySelectorAll(".budget-link-meta") ?? []].map((el) => el.textContent.trim()))
      .toEqual(["· 2026-11-10"]);

    await act(async () => {
      badge?.click();
      await Promise.resolve();
    });
    expect(badge?.getAttribute("aria-expanded")).toBe("true");
    expect(badge?.querySelector(".budget-link-primary")?.textContent).toBe(longLabel);
    expect([...badge?.querySelectorAll(".budget-link-meta") ?? []].map((el) => el.textContent.trim()))
      .toEqual(["· 2026-11-10", "· 42.50 PLN"]);

    const css = readFileSync("src/budget/budget.css", "utf8");
    expect(css).toMatch(/\.budget-link-primary\s*\{[^}]*text-overflow:\s*ellipsis;/);
    expect(css).toMatch(/\.budget-link-meta\s*\{[^}]*flex:\s*0 0 auto;[^}]*white-space:\s*nowrap;/);
    expect(css).not.toMatch(/\.budget-link-meta\s*\{[^}]*text-overflow:\s*ellipsis/);
    const narrowStart = css.indexOf("@media (max-width: 480px)");
    const narrowBlock = css.slice(narrowStart, css.indexOf(".budget-one-off", narrowStart));
    expect(narrowBlock).toMatch(/\.budget-link-indicator\[aria-expanded="true"\]\s*\{[^}]*flex-basis:\s*100%;[^}]*width:\s*100%;[^}]*white-space:\s*normal;/);
    expect(narrowBlock).toMatch(/\.budget-link-indicator\[aria-expanded="true"\]\s+\.budget-link-detail\s*\{[^}]*display:\s*flex;[^}]*flex-wrap:\s*wrap;[^}]*max-width:\s*none;/);
  });

  it("renders inline bank link indicator on linked receipts in Paragony i dowody and omits it when unlinked", async () => {
    const linked = receiptRow("10000000-0000-4000-8000-000000000001", {
      raw_shop_name: "Biedronka",
      linked: true,
      linked_transaction: {
        account_label: "mBank · eKonto ••1234",
        booking_date: "2026-11-10",
        amount: "42.50",
        currency: "PLN",
      },
    });
    const unlinked = receiptRow("10000000-0000-4000-8000-000000000002", {
      raw_shop_name: "Lidl",
      linked: false,
      linked_account_id: null,
      linked_entry_ref: null,
      linked_transaction: null,
    });
    const rApi: ReceiptsApi = {
      receiptsList: vi.fn(() => ok({ receipts: [linked, unlinked], next_cursor: null })),
      receiptsSearch: vi.fn(() => ok({ receipts: [], next_cursor: null })),
      receiptDetail: vi.fn(() => Promise.resolve({ ok: false as const, status: 404, error: "budget_receipt_not_found" })),
      receiptImage: vi.fn(() => Promise.resolve({ ok: false as const, status: 404, error: "budget_receipt_not_found" })),
      confirmReceipt: vi.fn(() => Promise.resolve({ ok: false as const, status: 500, error: "unused" })),
      deleteReceipt: vi.fn(() => Promise.resolve({ ok: false as const, status: 500, error: "unused" })),
    };

    await act(async () => {
      root.render(
        <ReceiptsView
          receiptsApi={rApi}
          accounts={[account()]}
          connections={[connection]}
          locale="en"
          revision={0}
          onAccessLoss={() => false}
        />,
      );
      await Promise.resolve();
    });
    await act(async () => { await Promise.resolve(); });

    const rows = [...host.querySelectorAll(".budget-receipt-row")];
    expect(rows).toHaveLength(2);
    const linkedBadge = rows[0]?.querySelector(".budget-receipt-tx-link");
    expect(linkedBadge?.textContent).toContain("🏦");
    expect(linkedBadge?.textContent).toContain("mBank · eKonto ••1234");
    expect(linkedBadge?.textContent).toContain("2026-11-10");
    expect(linkedBadge?.getAttribute("title")).toContain("mBank · eKonto ••1234");
    expect(linkedBadge?.getAttribute("aria-label")).toContain("2026-11-10");
    expect(rows[1]?.querySelector(".budget-receipt-tx-link")).toBeNull();
  });
});
