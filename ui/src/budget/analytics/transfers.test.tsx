// Synthetic data only: no real account, transaction or bank values.
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BudgetApi, BudgetResult } from "../api";
import { account } from "../fixtures";
import { decisionOf, leg, pair, transfersOf } from "./fixtures";
import type { TransferList, TransferPair } from "./model";
import { TransfersPanel, transferLabels } from "./transfers";

function ok<T>(data: T): Promise<BudgetResult<T>> { return Promise.resolve({ ok: true, data }); }
function fail<T>(status: number, error: string): Promise<BudgetResult<T>> { return Promise.resolve({ ok: false, status, error }); }

function fakeApi(rows: TransferPair[] = [pair()]) {
  return {
    transfers: vi.fn<BudgetApi["transfers"]>((period) => ok(transfersOf(period, rows))),
    decideTransfer: vi.fn<BudgetApi["decideTransfer"]>(() => ok({ decision: decisionOf() })),
  } satisfies Pick<BudgetApi, "transfers" | "decideTransfer">;
}

const accounts = [
  account({ id: "acc-1", display: { product: "Synthetic Current", currency: "PLN", masked_suffix: "0001", source: "offline_import",
    observed_at: 1, imported_at: 2 } }),
  account({ id: "acc-2", display: { product: "Synthetic Card", currency: "PLN", masked_suffix: "0002", source: "offline_import",
    observed_at: 1, imported_at: 2 } }),
];

let host: HTMLElement;
let root: Root;
beforeEach(() => {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => { root.unmount(); await Promise.resolve(); });
  host.remove();
});

async function settle(): Promise<void> {
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
}
async function show(api: ReturnType<typeof fakeApi>, options: { period?: string; lost?: boolean } = {}) {
  const onAccessLoss = vi.fn((result: { ok: boolean }) => options.lost === true && !result.ok);
  const onChanged = vi.fn();
  await act(async () => {
    root.render(<TransfersPanel api={api} period={options.period ?? "2026-11"} accounts={accounts} text={transferLabels.en}
      locale="en" revision={0} onAccessLoss={onAccessLoss} onChanged={onChanged} />);
    await Promise.resolve();
  });
  await settle();
  return { onAccessLoss, onChanged };
}
function buttons(): string[] {
  return [...host.querySelectorAll(".budget-pair button")].map((item) => item.textContent);
}
function button(text: string): HTMLButtonElement {
  const found = [...host.querySelectorAll("button")].find((item) => item.textContent === text);
  if (found === undefined) throw new Error(`button ${text} missing`);
  return found;
}
async function click(text: string): Promise<void> {
  await act(async () => { button(text).click(); await Promise.resolve(); });
  await settle();
}

describe("own-transfer queue", () => {
  it("shows a suggested pair with both legs, its evidence and accept/reject only", async () => {
    const api = fakeApi([pair({ source: "heuristic", date_distance: 1 })]);
    await show(api);
    expect(api.transfers).toHaveBeenCalledWith("2026-11", "suggested");
    const row = host.querySelector(".budget-pair")?.textContent ?? "";
    expect(row).toContain("Synthetic Current ••0001");
    expect(row).toContain("−1,200.00 PLN");
    expect(row).toContain("Synthetic Card ••0002");
    expect(row).toContain("same amount, dates within 3 days");
    expect(row).toContain("1 day apart");
    expect(buttons()).toEqual(["Confirm transfer", "Not a transfer"]);
    expect(button("To review").getAttribute("aria-pressed")).toBe("true");
  });

  it("accepts with both pair fingerprints and the seen version, then re-reads the month", async () => {
    const api = fakeApi([pair({ source: "account_identity" })]);
    const { onChanged } = await show(api);
    await click("Confirm transfer");
    expect(api.decideTransfer).toHaveBeenCalledWith({ action: "accept", source: "account_identity", decision_id: null,
      expected_version: 0, debit: { account_id: "acc-1", entry_reference: "out-1" }, credit: { account_id: "acc-2", entry_reference: "in-1" },
      expected_debit_fingerprint: "pfp-out-1", expected_credit_fingerprint: "pfp-in-1" });
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("never picks among ambiguous pairs: each one is offered and explained", async () => {
    const api = fakeApi([pair({ ambiguous: true }), pair({ ambiguous: true, credit: leg("acc-2", "in-2") })]);
    await show(api);
    expect(host.querySelectorAll(".budget-pair")).toHaveLength(2);
    expect(host.textContent).toContain("Fits more than one pair");
    expect(api.decideTransfer).not.toHaveBeenCalled();
    expect(buttons()).toEqual(["Confirm transfer", "Not a transfer", "Confirm transfer", "Not a transfer"]);
  });

  it("switches queues and offers undo for decided pairs, by decision id", async () => {
    const accepted = pair({ id: "dec-1", version: 4, status: "accepted", source: "manual", date_distance: 0 });
    const api = fakeApi([accepted]);
    await show(api);
    await click("Confirmed");
    expect(api.transfers).toHaveBeenLastCalledWith("2026-11", "accepted");
    expect(host.textContent).toContain("paired by you");
    expect(host.textContent).toContain("same day");
    expect(buttons()).toEqual(["Undo"]);
    await click("Undo");
    expect(api.decideTransfer).toHaveBeenCalledWith({ action: "undo", source: "manual", decision_id: "dec-1", debit: null, credit: null,
      expected_debit_fingerprint: null, expected_credit_fingerprint: null, expected_version: 4 });
  });

  it("reconfirms a pair to re-check as a manual decision with the current fingerprints", async () => {
    const review = pair({ id: "dec-4", version: 3, status: "review", source: "account_identity", stale: true,
      debit: leg("acc-1", "out-1", { fingerprint: "pfp-now-out" }), credit: leg("acc-2", "in-1", { fingerprint: "pfp-now-in" }) });
    const api = fakeApi([review]);
    await show(api);
    await click("To re-check");
    await click("Confirm transfer");
    expect(api.decideTransfer).toHaveBeenCalledWith(expect.objectContaining({ action: "accept", source: "manual", decision_id: "dec-4",
      expected_version: 3, expected_debit_fingerprint: "pfp-now-out", expected_credit_fingerprint: "pfp-now-in" }));
  });

  it("lets the owner re-accept an undone pair explicitly, as a manual decision", async () => {
    const undone = pair({ id: "dec-3", version: 6, status: "undone", source: "heuristic" });
    const api = fakeApi([undone]);
    await show(api);
    await click("Undone");
    expect(api.transfers).toHaveBeenLastCalledWith("2026-11", "undone");
    expect(buttons()).toEqual(["Confirm transfer"]);
    await click("Confirm transfer");
    expect(api.decideTransfer).toHaveBeenCalledWith(expect.objectContaining({ action: "accept", source: "manual", expected_version: 6 }));
  });

  it("starts the list again when the server reports a stale cursor", async () => {
    const api = fakeApi();
    api.transfers.mockImplementationOnce((period) => ok(transfersOf(period, [pair()], "cursor-1")))
      .mockImplementationOnce(() => fail(409, "budget_analytics_stale_cursor"));
    await show(api);
    await click("Load more");
    expect(api.transfers).toHaveBeenCalledTimes(3);
    expect(api.transfers).toHaveBeenLastCalledWith("2026-11", "suggested");
  });

  it("marks stale decisions and boundary pairs, which can only be rejected or undone", async () => {
    const api = fakeApi([
      pair({ id: "dec-2", version: 2, status: "review", stale: true }),
      pair({ status: "boundary", boundary: true, credit: leg("acc-9", "in-9", { fingerprint: null, amount: null, currency: null, date: null, selected: false }) }),
    ]);
    await show(api);
    const rows = [...host.querySelectorAll(".budget-pair")].map((row) => row.textContent);
    expect(rows[0]).toContain("The bank changed one side");
    expect(rows[1]).toContain("Account (excluded account)");
    expect(rows[1]).toContain("One side is on an account excluded");
    expect(rows[1]).not.toContain("PLN →");
    expect(buttons()).toEqual(["Confirm transfer", "Undo", "Not a transfer"]);
  });

  it("reloads after a conflict or a changed bank entry instead of overwriting", async () => {
    const api = fakeApi();
    const { onChanged } = await show(api);
    api.decideTransfer.mockImplementationOnce(() => fail(409, "budget_analytics_source_changed"));
    await click("Confirm transfer");
    expect(host.textContent).toContain("bank data for this pair changed");
    expect(api.transfers).toHaveBeenCalledTimes(2);
    api.decideTransfer.mockImplementationOnce(() => fail(409, "budget_analytics_conflict"));
    await click("Not a transfer");
    expect(host.textContent).toContain("changed elsewhere");
    expect(api.transfers).toHaveBeenCalledTimes(3);
    api.decideTransfer.mockImplementationOnce(() => fail(409, "budget_analytics_leg_reserved"));
    await click("Not a transfer");
    expect(host.textContent).toContain("already in another pair");
    expect(api.transfers).toHaveBeenCalledTimes(4);
    api.decideTransfer.mockImplementationOnce(() => fail(400, "budget_invalid_request"));
    await click("Not a transfer");
    expect(host.textContent).toContain("Could not save");
    expect(api.transfers).toHaveBeenCalledTimes(4);
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("passes answers to the access barrier and drops late ones from another month", async () => {
    let late: (value: BudgetResult<TransferList>) => void = () => undefined;
    const api = fakeApi([pair()]);
    api.transfers.mockImplementationOnce(() => new Promise((resolve) => { late = resolve; }));
    await show(api);
    await show(api, { period: "2026-10" });
    await act(async () => { late({ ok: true, data: transfersOf("2026-11", [pair({ debit: leg("acc-1", "late") })]) }); await Promise.resolve(); });
    await settle();
    expect(host.querySelectorAll(".budget-pair")).toHaveLength(1);
    api.transfers.mockImplementation(() => fail(401, "budget_http_401"));
    const { onAccessLoss } = await show(api, { period: "2026-09", lost: true });
    expect(onAccessLoss).toHaveBeenCalledWith({ ok: false, status: 401, error: "budget_http_401" });
    expect(host.textContent).not.toContain("could not be loaded");
  });

  it("pages with the cursor, keeps a limited-scan warning and reports empty or failed reads", async () => {
    const api = fakeApi();
    api.transfers.mockImplementationOnce((period) => ok(transfersOf(period, [pair()], "cursor-1")))
      .mockImplementationOnce((period) => ok(transfersOf(period, [pair({ debit: leg("acc-1", "out-2") })], null, true)));
    await show(api);
    await click("Load more");
    expect(api.transfers).toHaveBeenLastCalledWith("2026-11", "suggested", "cursor-1");
    expect(host.querySelectorAll(".budget-pair")).toHaveLength(2);
    expect(host.textContent).toContain("stopped at its limit");
    api.transfers.mockImplementation((period) => ok(transfersOf(period)));
    await click("Not transfers");
    expect(host.textContent).toContain("Nothing here for this month.");
    api.transfers.mockImplementation(() => fail(500, "budget_internal_error"));
    await click("To re-check");
    expect(host.textContent).toContain("Transfers could not be loaded.");
  });
});
