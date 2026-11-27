// Synthetic data only.
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { loadLocale } from "../i18n";
import { ReceiptGroupBuilder } from "./receipt_group_builder";
import type { ReceiptGroupBuilderLabels, ReceiptGroupPool, ReceiptGroupSelection } from "./receipt_group_builder";
import { REDACTED_VALUE } from "./receipts_model";

const labels: ReceiptGroupBuilderLabels = {
  title: "Build receipt group",
  transaction: "Target bank transaction",
  members: "Receipts",
  anchorReceipt: "Starting receipt",
  memberCount: (count, min, max) => `${String(count)} selected. Choose ${String(min)} to ${String(max)} receipts.`,
  noEligibleMembers: "No eligible receipts for this transaction.",
  remainingDelta: "Remaining delta",
  importedFirst: "When you confirm, imported rows will be confirmed individually without a bank link before the group is linked.",
  confirm: "Confirm group link",
};

const transaction: ReceiptGroupPool["transaction"] = {
  account_id: "synthetic-account",
  entry_reference: "a".repeat(64),
  amount: "0.30",
  currency: "PLN",
  credit_debit_indicator: "DBIT",
  booking_date: "2026-11-16",
  value_date: null,
  transaction_date: "2026-11-15",
  candidate_token: "opaque-synthetic-token",
  evidence_fingerprint: "opaque-synthetic-fingerprint",
};

type Member = ReceiptGroupPool["members"][number];
type Evidence = NonNullable<ReceiptGroupPool["evidence"]>[number];

function member(receipt_id: string, total_amount: string, overrides: Partial<Member> = {}): Member {
  return {
    receipt_id,
    version: 1,
    status: "confirmed",
    receipt_kind: "purchase",
    receipt_date: "2026-11-15",
    raw_shop_name: `Shop ${receipt_id}`,
    total_amount,
    currency: "PLN",
    document_kind: "receipt",
    ...overrides,
  };
}

function gateway(overrides: Partial<Evidence> = {}): Evidence {
  return {
    receipt_id: "payment",
    version: 5,
    status: "confirmed",
    receipt_kind: "purchase",
    receipt_date: "2026-11-15",
    total_amount: "0.30",
    currency: "PLN",
    gateway: "Synthetic Gateway",
    fee_amount: "0.02",
    evidence_for_receipt_id: null,
    ...overrides,
  };
}

let host: HTMLDivElement;
let root: Root;
const onConfirm = vi.fn<(selection: ReceiptGroupSelection) => void>();

beforeEach(async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  await loadLocale("en");
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  onConfirm.mockClear();
});

afterEach(async () => {
  await act(async () => {
    root.unmount();
    await Promise.resolve();
  });
  host.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function render(
  pool: Omit<ReceiptGroupPool, "anchor_receipt_id"> & { anchor_receipt_id?: string },
  disabled = false,
): Promise<void> {
  await act(async () => {
    root.render(
      <ReceiptGroupBuilder
        pool={{ anchor_receipt_id: pool.members[0]?.receipt_id ?? "", ...pool }}
        labels={labels}
        locale="en-US"
        accountLabel="Synthetic account"
        disabled={disabled}
        onConfirm={onConfirm}
      />,
    );
    await Promise.resolve();
  });
}

function checkbox(text: string): HTMLInputElement {
  const control = Array.from(host.querySelectorAll<HTMLInputElement>('input[type="checkbox"]'))
    .find((input) => Array.from(input.labels ?? []).some((label) => label.textContent.includes(text)));
  if (control === undefined) throw new Error(`Checkbox not found for ${text}`);
  return control;
}

function confirmButton(): HTMLButtonElement {
  const button = Array.from(host.querySelectorAll("button")).find((item) => item.textContent === labels.confirm);
  if (button === undefined) throw new Error("Confirm button not found");
  return button;
}

async function click(control: HTMLElement): Promise<void> {
  await act(async () => {
    control.click();
    await Promise.resolve();
  });
}

function expectRemaining(value: string): void {
  expect(host.querySelector("output")?.textContent).toBe(`Remaining delta ${value} PLN`);
}

describe("ReceiptGroupBuilder", () => {
  it("uses exact decimal deltas and only hands off a reviewed selection on explicit confirmation", async () => {
    const fetch = vi.spyOn(globalThis, "fetch");
    const storage = vi.spyOn(Storage.prototype, "setItem");
    const target = { ...transaction, amount: "0.3001" };
    await render({
      transaction: target,
      members: [member("A", "0.10", { version: 3 }), member("B", "0.20"), member("C", "0.0001")],
    });

    const section = host.querySelector("section");
    expect(section).not.toBeNull();
    expect(document.getElementById(section?.getAttribute("aria-labelledby") ?? "")?.textContent).toBe(labels.title);
    expect(host.textContent).toContain("Synthetic account");
    expect(host.textContent).toContain("2026-11-16");
    expect(host.textContent).toContain("−0.3001 PLN");
    expect(host.innerHTML).not.toContain(target.candidate_token);
    expect(host.innerHTML).not.toContain(target.evidence_fingerprint);
    expect(host.innerHTML).not.toContain(target.entry_reference);
    expect(host.querySelectorAll(":checked")).toHaveLength(0);
    expectRemaining("0.3001");

    await click(checkbox("Shop A"));
    await click(checkbox("Shop B"));
    expectRemaining("0.0001");
    expect(confirmButton().disabled).toBe(true);
    await click(confirmButton());
    await click(checkbox("Shop C"));
    expectRemaining("0.00");
    expect(confirmButton().disabled).toBe(false);
    expect(onConfirm).not.toHaveBeenCalled();

    await click(confirmButton());
    expect(onConfirm).toHaveBeenCalledExactlyOnceWith({
      transaction: target,
      members: [
        { receipt_id: "A", expected_version: 3, status: "confirmed" },
        { receipt_id: "B", expected_version: 1, status: "confirmed" },
        { receipt_id: "C", expected_version: 1, status: "confirmed" },
      ],
      evidence: null,
    });
    expect(fetch).not.toHaveBeenCalled();
    expect(storage).not.toHaveBeenCalled();
  });

  it("requires at least two members and disables confirmation when the total exceeds the target", async () => {
    await render({ transaction, members: [member("A", "0.30"), member("B", "0.01")] });

    await click(checkbox("Shop A"));
    expectRemaining("0.00");
    expect(confirmButton().disabled).toBe(true);
    await click(checkbox("Shop B"));
    expectRemaining("−0.01");
    expect(confirmButton().disabled).toBe(true);
    await click(confirmButton());
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("discloses preconfirmation for selected imported members and standalone evidence", async () => {
    await render({
      transaction,
      members: [
        member("A", "0.10", { raw_shop_name: REDACTED_VALUE, status: "imported", version: 4 }),
        member("B", "0.18"),
      ],
      evidence: [gateway({ status: "imported" })],
    });

    expect(host.innerHTML).not.toContain(REDACTED_VALUE);
    const importedMember = host.querySelector<HTMLInputElement>(".budget-redacted")?.closest("label")?.control;
    if (!(importedMember instanceof HTMLInputElement)) throw new Error("Redacted member checkbox not found");
    expect(host.textContent).not.toContain(labels.importedFirst);
    await click(importedMember);
    expect(host.textContent).toContain(labels.importedFirst);
    await click(importedMember);
    expect(host.textContent).not.toContain(labels.importedFirst);

    await click(checkbox("Synthetic Gateway"));
    expect(host.textContent).toContain(labels.importedFirst);
    expectRemaining("0.28");
    expect(confirmButton().disabled).toBe(true);
    await click(importedMember);
    await click(checkbox("Shop B"));
    expectRemaining("0.00");
    const disclosureId = confirmButton().getAttribute("aria-describedby") ?? "";
    expect(document.getElementById(disclosureId)?.textContent).toBe(labels.importedFirst);
    expect(onConfirm).not.toHaveBeenCalled();

    await click(confirmButton());
    expect(onConfirm).toHaveBeenCalledExactlyOnceWith({
      transaction,
      members: [
        { receipt_id: "A", expected_version: 4, status: "imported" },
        { receipt_id: "B", expected_version: 1, status: "confirmed" },
      ],
      evidence: { receipt_id: "payment", expected_version: 5, status: "imported", fee_amount: "0.02" },
    });

    await click(checkbox("Synthetic Gateway"));
    expectRemaining("0.02");
    expect(confirmButton().disabled).toBe(true);
  });

  it("limits a group to eight distinct receipts in the target currency and direction", async () => {
    const members = Array.from({ length: 9 }, (_, index) => member(String(index), "1"));
    await render({
      transaction: { ...transaction, credit_debit_indicator: "CRDT", amount: "8" },
      members: [
        ...members.map((item) => ({ ...item, receipt_kind: "refund" as const })),
        member("wrong-kind", "1"),
        member("wrong-currency", "1", { receipt_kind: "refund", currency: "EUR" }),
        member("draft", "1", { receipt_kind: "refund", status: "draft" }),
        member("zero", "0", { receipt_kind: "refund" }),
        member("gateway", "1", { receipt_kind: "refund", gateway: "Synthetic Gateway" }),
        member("attached", "1", { receipt_kind: "refund", evidence_for_receipt_id: "0" }),
        member("0", "1", { receipt_kind: "refund" }),
      ],
    });
    expect(host.querySelectorAll('input[type="checkbox"]')).toHaveLength(9);

    for (const item of members.slice(0, 8)) await click(checkbox(`Shop ${item.receipt_id}`));
    expect(checkbox("Shop 8").disabled).toBe(true);
    expect(host.textContent).toContain("8 selected");
    expectRemaining("0.00");
    expect(confirmButton().disabled).toBe(false);

    await click(checkbox("Shop 0"));
    expect(checkbox("Shop 8").disabled).toBe(false);
    await click(checkbox("Shop 8"));
    expectRemaining("0.00");
    expect(confirmButton().disabled).toBe(true);
    expect(onConfirm).not.toHaveBeenCalled();
    await click(checkbox("Shop 1"));
    await click(checkbox("Shop 0"));
    await click(confirmButton());
    expect(onConfirm.mock.calls[0]?.[0].members.map((row) => row.receipt_id)).toEqual(["0", "2", "3", "4", "5", "6", "7", "8"]);
  });

  it.each<Partial<Evidence>>([
    { evidence_for_receipt_id: "A" },
    { currency: "EUR" },
    { receipt_kind: "refund" },
    { total_amount: "0.29" },
    { fee_amount: "-0.02" },
    { status: "needs_review" },
  ])("excludes gateway evidence outside the eligible transaction pool (%j)", async (overrides) => {
    await render({
      transaction,
      members: [member("A", "0.10"), member("B", "0.18"), member("payment", "0.02")],
      evidence: [gateway(overrides)],
    });

    expect(host.querySelectorAll('input[type="checkbox"]')).toHaveLength(2);
    expect(host.textContent).not.toContain("Synthetic Gateway");
    await click(checkbox("Shop A"));
    await click(checkbox("Shop B"));
    expectRemaining("0.02");
    expect(confirmButton().disabled).toBe(true);
  });

  it("accepts optional standalone evidence with no fee and scopes selection to the reviewed transaction and versions", async () => {
    const pool = {
      transaction,
      members: [member("A", "0.10"), member("B", "0.20")],
      evidence: [gateway({ fee_amount: null })],
    };
    await render(pool);
    await click(checkbox("Shop A"));
    await click(checkbox("Shop B"));
    await click(checkbox("Synthetic Gateway"));
    expectRemaining("0.00");
    await click(confirmButton());
    expect(onConfirm.mock.calls[0]?.[0].evidence).toEqual(
      { receipt_id: "payment", expected_version: 5, status: "confirmed", fee_amount: null },
    );

    await render({
      ...pool,
      members: [member("A", "0.10", { version: 2 }), member("B", "0.20")],
      evidence: [gateway({ fee_amount: null, version: 6 })],
    });
    expect(checkbox("Shop A").checked).toBe(false);
    expect(checkbox("Synthetic Gateway").checked).toBe(false);
    expect(checkbox("Shop B").checked).toBe(true);
    expect(confirmButton().disabled).toBe(true);

    await render({ ...pool, transaction: { ...transaction, entry_reference: "b".repeat(64) } });
    expect(host.querySelectorAll(":checked")).toHaveLength(0);
    expectRemaining("0.30");
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("supports multiple evidence choices and treats a zero recorded fee as no fee", async () => {
    await render({
      transaction,
      members: [member("A", "0.10"), member("B", "0.20")],
      evidence: [gateway(), gateway({ receipt_id: "alternate", gateway: "Alternate Gateway", fee_amount: "0.00" })],
    });

    await click(checkbox("Shop A"));
    await click(checkbox("Shop B"));
    await click(checkbox("Synthetic Gateway"));
    expectRemaining("−0.02");
    expect(confirmButton().disabled).toBe(true);
    await click(checkbox("Alternate Gateway"));
    expect(checkbox("Synthetic Gateway").checked).toBe(false);
    expectRemaining("0.00");
    await click(confirmButton());
    expect(onConfirm.mock.calls[0]?.[0].evidence).toEqual(
      { receipt_id: "alternate", expected_version: 5, status: "confirmed", fee_amount: null },
    );
  });

  it("locks all checkboxes and confirmation while disabled during pending writes", async () => {
    const pool: ReceiptGroupPool = {
      transaction,
      anchor_receipt_id: "A",
      members: [member("A", "0.10"), member("B", "0.20")],
      evidence: [gateway()],
    };
    await render(pool, false);
    await click(checkbox("Shop A"));
    await click(checkbox("Shop B"));
    expect(confirmButton().disabled).toBe(false);

    await render(pool, true);
    expect(checkbox("Shop A").disabled).toBe(true);
    expect(checkbox("Shop B").disabled).toBe(true);
    expect(checkbox("Synthetic Gateway").disabled).toBe(true);
    expect(confirmButton().disabled).toBe(true);
    await click(confirmButton());
    expect(onConfirm).not.toHaveBeenCalled();

    await render(pool, false);
    expect(checkbox("Shop A").disabled).toBe(false);
    expect(confirmButton().disabled).toBe(false);
  });
});
