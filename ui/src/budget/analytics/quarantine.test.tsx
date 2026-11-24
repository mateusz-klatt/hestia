import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { loadLocale, t, tPlural } from "../../i18n";
import type { BudgetApi, BudgetResult } from "../api";
import { account, connection } from "../fixtures";
import { quarantineAccount, quarantineItem, quarantineOf } from "./fixtures";
import { QUARANTINE_BUCKET_KEYS, QUARANTINE_REASON_KEYS, QUARANTINE_STATUS_KEYS } from "./model";
import type { AnalyticsQuarantine, AnalyticsQuarantineItem } from "./model";
import { QuarantineView } from "./quarantine";

type Settle<T> = (value: BudgetResult<T>) => void;
type QuarantineApi = Pick<BudgetApi, "quarantine">;
type AccessLoss = (result: { ok: boolean; status?: number; error?: string }) => boolean;

function ok<T>(data: T): Promise<BudgetResult<T>> { return Promise.resolve({ ok: true, data }); }
function fail<T>(status: number, error: string): Promise<BudgetResult<T>> { return Promise.resolve({ ok: false, status, error }); }
function deferred<T>(): { promise: Promise<BudgetResult<T>>; settle: Settle<T> } {
  let settle: Settle<T> = () => undefined;
  const promise = new Promise<BudgetResult<T>>((resolve) => { settle = resolve; });
  return { promise, settle };
}

const accounts = [
  account({
    id: "acc-1",
    display: { product: "Synthetic Main", currency: "PLN", masked_suffix: "0001", source: "offline_import", observed_at: 1, imported_at: 2 },
  }),
  account({
    id: "acc-2",
    display: { product: "Synthetic Savings", currency: "EUR", masked_suffix: "0002", source: "offline_import", observed_at: 1, imported_at: 2 },
  }),
];
const keepAccess: AccessLoss = () => false;

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
  await loadLocale("en");
  vi.restoreAllMocks();
});

async function settle(): Promise<void> {
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
}
async function renderView(
  api: QuarantineApi,
  locale: "en" | "pl" = "en",
  revision = 1,
  onAccessLoss: AccessLoss = keepAccess,
): Promise<void> {
  await loadLocale(locale);
  await act(async () => {
    root.render(<QuarantineView api={api} accounts={accounts} connections={[connection]}
      revision={revision} onAccessLoss={onAccessLoss} />);
    await Promise.resolve();
  });
  await settle();
}
async function click(element: HTMLElement): Promise<void> {
  await act(async () => { element.click(); await Promise.resolve(); });
  await settle();
}
function button(label: string): HTMLButtonElement {
  const found = [...host.querySelectorAll("button")].find((item) => item.textContent === label);
  if (found === undefined) throw new Error(`button ${label} missing`);
  return found;
}

const allItems = [
  quarantineItem({ bucket: "uncertain", reason: "reference_conflict", status: "BOOK", entry_reference: "private-ref-0" }),
  quarantineItem({ bucket: "unresolved", reason: "non_booked_status", status: "PDNG", entry_reference: "private-ref-1", account_id: "acc-2" }),
  quarantineItem({ bucket: "unresolved", reason: "missing_reference", status: "HOLD", entry_reference: null }),
  quarantineItem({ bucket: "unresolved", reason: "missing_date", status: "CNCL", date: null, entry_reference: "private-ref-3" }),
  quarantineItem({ bucket: "import_unresolved", reason: "non_canonical", status: "RJCT", source: "offline_import", entry_reference: "private-ref-4" }),
  quarantineItem({ bucket: "import_unresolved", reason: "api_preserved", status: "SCHD", source: "offline_import", entry_reference: "private-ref-5", account_id: "acc-2" }),
  quarantineItem({ bucket: "import_unresolved", reason: "stale_conflict", status: "OTHR", source: "offline_import", entry_reference: "private-ref-6" }),
  quarantineItem({ bucket: "import_unresolved", reason: "same_time_conflict", status: null, source: "offline_import", entry_reference: "private-ref-7" }),
];

describe("QuarantineView", () => {
  it("renders every bucket, reason and status in English and Polish in a collapsed details list", async () => {
    await renderView({ quarantine: vi.fn(() => ok(quarantineOf(allItems, { accounts: [] }))) });
    const details = host.querySelector("details");
    expect(details).not.toBeNull();
    expect(details?.hasAttribute("open")).toBe(false);
    expect(details?.querySelectorAll("li")).toHaveLength(8);
    const english = details?.textContent ?? "";
    for (const key of Object.values(QUARANTINE_BUCKET_KEYS)) expect(english).toContain(t(key));
    for (const key of Object.values(QUARANTINE_REASON_KEYS)) expect(english).toContain(t(key));
    for (const key of Object.values(QUARANTINE_STATUS_KEYS)) expect(english).toContain(t(key));
    expect(english).toContain("No date");
    expect(english).toContain("No status");
    expect(english).toContain("Synthetic Bank · Synthetic Main ••0001");
    expect(english).toContain("Synthetic Bank · Synthetic Savings ••0002");

    await renderView({ quarantine: vi.fn(() => ok(quarantineOf(allItems, { accounts: [] }))) }, "pl", 2);
    const polish = host.querySelector("details")?.textContent ?? "";
    for (const key of Object.values(QUARANTINE_BUCKET_KEYS)) expect(polish).toContain(t(key));
    for (const key of Object.values(QUARANTINE_REASON_KEYS)) expect(polish).toContain(t(key));
    for (const key of Object.values(QUARANTINE_STATUS_KEYS)) expect(polish).toContain(t(key));
    expect(polish).toContain("Brak daty");
    expect(polish).toContain("Brak statusu");
  });

  it("uses a plain-language fallback for an unknown reason", async () => {
    const unknown = quarantineItem({ reason: "future_reason" as AnalyticsQuarantineItem["reason"] });
    await renderView({ quarantine: vi.fn(() => ok(quarantineOf([unknown]))) });
    expect(host.querySelector("details")?.textContent).toContain("This entry is excluded for an unrecognized reason (future_reason).");
  });

  it("shows summary counts and per-account names, counts and exclusion state", async () => {
    const data = quarantineOf(allItems.slice(0, 3), {
      summary: { uncertain_count: 1, unresolved_count: 2, import_unresolved_count: 3, total_count: 6 },
      accounts: [
        quarantineAccount({ uncertain_count: 1, unresolved_count: 2, import_unresolved_count: 3, total_count: 6 }),
        quarantineAccount({ account_id: "acc-2", selected: false }),
      ],
    });
    await renderView({ quarantine: vi.fn(() => ok(data)) });
    const summary = host.querySelector(".budget-total");
    expect(summary?.textContent).toContain("1 entry");
    expect(summary?.textContent).toContain("2 entries");
    expect(summary?.textContent).toContain("3 entries");
    expect(summary?.textContent).toContain("6 entries");
    const rows = [...host.querySelectorAll("tbody tr")];
    expect(rows[0]?.textContent).toContain("Synthetic Bank · Synthetic Main ••0001");
    expect(rows[0]?.textContent).toContain("1236");
    expect(rows[1]?.textContent).toContain("Synthetic Bank · Synthetic Savings ••0002");
    expect(rows[1]?.textContent).toContain("(excluded)");
    expect(host.textContent).not.toContain("acc-1");
    expect(host.textContent).not.toContain("acc-2");
  });

  it("uses correct Polish plural forms, including teen and 2–4 edge cases", async () => {
    await loadLocale("pl");
    expect(tPlural("budget.quarantine.entries", 1)).toBe("1 wpis");
    expect(tPlural("budget.quarantine.entries", 2)).toBe("2 wpisy");
    expect(tPlural("budget.quarantine.entries", 5)).toBe("5 wpisów");
    expect(tPlural("budget.quarantine.entries", 12)).toBe("12 wpisów");
    expect(tPlural("budget.quarantine.entries", 22)).toBe("22 wpisy");
    expect(tPlural("budget.quarantine.entries", 25)).toBe("25 wpisów");
    expect(tPlural("budget.quarantine.truncatedShown", 2, { total: tPlural("budget.quarantine.ofTotal", 5) }))
      .toBe("Pokazano 2 najnowsze wpisy z 5 wpisów.");
    expect(tPlural("budget.quarantine.truncatedShown", 1, { total: tPlural("budget.quarantine.ofTotal", 2) }))
      .toBe("Pokazano 1 najnowszy wpis z 2 wpisów.");
  });

  it("refetches with account_id for an account and without it for all accounts", async () => {
    const all = quarantineOf([quarantineItem({ date: "2026-11-20" })]);
    const filtered = quarantineOf([quarantineItem({ account_id: "acc-2", date: "2026-11-01" })], {
      accounts: [quarantineAccount({ account_id: "acc-2", uncertain_count: 1, total_count: 1 })],
    });
    const quarantine = vi.fn<BudgetApi["quarantine"]>((accountId) => ok(accountId === "acc-2" ? filtered : all));
    await renderView({ quarantine });
    await click(button("Synthetic Bank · Synthetic Savings ••0002"));
    expect(quarantine).toHaveBeenLastCalledWith("acc-2");
    expect(host.querySelector("details")?.textContent).toContain("2026-11-01");
    expect(host.querySelector("details")?.textContent).not.toContain("2026-11-20");
    await click(button("All accounts"));
    expect(quarantine).toHaveBeenLastCalledWith(undefined);
    expect(quarantine.mock.calls.map((call) => call[0])).toEqual([undefined, "acc-2", undefined]);
    expect(host.querySelector("details")?.textContent).toContain("2026-11-20");
  });

  it("drops a stale response after the account filter starts a newer generation", async () => {
    const first = deferred<AnalyticsQuarantine>();
    const second = deferred<AnalyticsQuarantine>();
    const quarantine = vi.fn<BudgetApi["quarantine"]>()
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise);
    await renderView({ quarantine });
    await click(button("Synthetic Bank · Synthetic Savings ••0002"));
    await act(async () => {
      second.settle({ ok: true, data: quarantineOf([quarantineItem({ account_id: "acc-2", date: "2026-11-01" })]) });
      await Promise.resolve();
    });
    await settle();
    expect(host.querySelector("details")?.textContent).toContain("2026-11-01");
    await act(async () => {
      first.settle({ ok: true, data: quarantineOf([quarantineItem({ date: "2025-01-01" })]) });
      await Promise.resolve();
    });
    await settle();
    expect(host.querySelector("details")?.textContent).toContain("2026-11-01");
    expect(host.textContent).not.toContain("2025-01-01");
  });

  it("runs the access-loss barrier before dropping an old-generation response", async () => {
    const first = deferred<AnalyticsQuarantine>();
    const second = deferred<AnalyticsQuarantine>();
    const quarantine = vi.fn<BudgetApi["quarantine"]>()
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise);
    const onAccessLoss = vi.fn((result: { ok: boolean; status?: number; error?: string }) => !result.ok && result.status === 401);
    await renderView({ quarantine }, "en", 1, onAccessLoss);
    await click(button("Synthetic Bank · Synthetic Savings ••0002"));
    await act(async () => {
      second.settle({ ok: true, data: quarantineOf([quarantineItem({ account_id: "acc-2", date: "2026-11-01" })]) });
      await Promise.resolve();
    });
    await settle();
    const denied = { ok: false as const, status: 401, error: "budget_http_401" };
    await act(async () => { first.settle(denied); await Promise.resolve(); });
    await settle();
    expect(onAccessLoss).toHaveBeenCalledWith(denied);
    expect(host.querySelector("details")?.textContent).toContain("2026-11-01");
  });

  it("shows a localized error and clears loaded data when the current generation fails", async () => {
    const quarantine = vi.fn<BudgetApi["quarantine"]>()
      .mockImplementationOnce(() => ok(quarantineOf([quarantineItem({ date: "2026-11-20" })])))
      .mockImplementationOnce(() => fail<AnalyticsQuarantine>(500, "budget_internal_error"));
    await renderView({ quarantine });
    expect(host.querySelector(".budget-total")?.textContent).toContain("Entries kept out of totals");
    expect(host.querySelector("details")?.textContent).toContain("2026-11-20");

    await click(button("Synthetic Bank · Synthetic Savings ••0002"));
    expect(quarantine).toHaveBeenLastCalledWith("acc-2");
    expect(host.querySelector('[role="alert"]')?.textContent).toBe(t("budget.quarantine.error"));
    expect(host.querySelector(".budget-total")).toBeNull();
    expect(host.querySelector("details")).toBeNull();
    expect(host.textContent).not.toContain("2026-11-20");
  });

  it("shows the newest-item truncation notice using the exact total", async () => {
    const data = quarantineOf(allItems.slice(0, 2), {
      summary: { uncertain_count: 200, unresolved_count: 3, import_unresolved_count: 2, total_count: 205 },
      truncated: true,
    });
    await renderView({ quarantine: vi.fn(() => ok(data)) });
    expect(host.querySelector('[role="note"]')?.textContent).toBe("Showing the newest 2 of 205 entries.");
  });

  it("shows the localized empty state when total_count is zero", async () => {
    await renderView({ quarantine: vi.fn(() => ok(quarantineOf([]))) });
    expect(host.textContent).toContain("All entries are included in totals.");
    expect(host.querySelector("details")).toBeNull();
    await renderView({ quarantine: vi.fn(() => ok(quarantineOf([]))) }, "pl", 2);
    expect(host.textContent).toContain("Wszystkie wpisy są uwzględnione w sumach.");
  });

  it("never exposes entry_reference text anywhere in the DOM", async () => {
    const secret = "private-reference-must-not-render-7f42c9";
    const data = quarantineOf([
      quarantineItem({ entry_reference: secret }),
      quarantineItem({ entry_reference: null, reason: "missing_reference", bucket: "unresolved" }),
    ]);
    await renderView({ quarantine: vi.fn(() => ok(data)) });
    expect(host.textContent).not.toContain(secret);
    expect(host.innerHTML).not.toContain(secret);
    expect(host.querySelector("details")?.querySelectorAll("li")).toHaveLength(2);
  });

  it("does not persist filter state or add URL query state", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    await renderView({ quarantine: vi.fn(() => ok(quarantineOf([]))) });
    await click(button("Synthetic Bank · Synthetic Main ••0001"));
    expect(setItem).not.toHaveBeenCalled();
    expect(window.location.search).toBe("");
    expect(host.querySelectorAll("input")).toHaveLength(0);
  });
});
