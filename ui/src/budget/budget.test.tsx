import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BudgetApi, BudgetResult } from "./api";
import { Budget } from "./budget";
import { account, connection } from "./fixtures";
import type { BudgetAccount, BudgetBalances, BudgetJob, BudgetTransaction, BudgetTransactions } from "./model";

type Settle<T> = (value: BudgetResult<T>) => void;

function ok<T>(data: T): Promise<BudgetResult<T>> { return Promise.resolve({ ok: true, data }); }
function fail<T>(status: number, error: string): Promise<BudgetResult<T>> { return Promise.resolve({ ok: false, status, error }); }
function deferred<T>(): { promise: Promise<BudgetResult<T>>; settle: Settle<T> } {
  let settle: Settle<T> = () => undefined;
  const promise = new Promise<BudgetResult<T>>((resolve) => { settle = resolve; });
  return { promise, settle };
}

const capabilities = { stored_reads: true, account_controls: true, refresh: true, background_selection: true,
  background_polling: false, consent: false };
const display = { product: "Synthetic Current", currency: "PLN", masked_suffix: "0001", source: "offline_import",
  observed_at: 1, imported_at: 2 };

function job(changes: Partial<BudgetJob> = {}): BudgetJob {
  return { id: "job-1", connection_id: "bank", account_id: null, operation: null, source: "manual", status: "queued",
    error: null, next_attempt: null, created_at: 1, started_at: null, finished_at: null, ...changes };
}
function tx(reference: string, amount = "1.00"): BudgetTransaction {
  return { entry_reference: reference, amount, currency: "PLN", credit_debit_indicator: "DBIT", status: "BOOK",
    booking_date: "2026-11-20", value_date: null, transaction_date: null, remittance_information: [`Synthetic ${reference}`],
    source: "api", observed_at: 1, api_observed_at: 1 };
}
function page(rows: BudgetTransaction[], cursor: string | null = null, accountId = "local-a"): BudgetTransactions {
  return { account_id: accountId, transactions: rows, account_total: rows.length, uncertain_total: 0, next_cursor: cursor };
}

const balancesData = {
  account_id: "local-a", balances: [] as BudgetBalances["balances"], balances_total: 0, balances_truncated: false,
  freshness: account().components.balances, archived_imports: [] as BudgetBalances["archived_imports"], archived_has_more: false,
};

function fakeApi(accounts: BudgetAccount[] = [account({ display })]) {
  return {
    connections: vi.fn<BudgetApi["connections"]>(() => ok({ connections: [connection], capabilities })),
    accounts: vi.fn<BudgetApi["accounts"]>(() => ok({ accounts })),
    jobs: vi.fn<BudgetApi["jobs"]>(() => ok({ jobs: [] })),
    transactions: vi.fn<BudgetApi["transactions"]>(() => ok(page([]))),
    balances: vi.fn<BudgetApi["balances"]>(() => ok(balancesData)),
    setSelected: vi.fn<BudgetApi["setSelected"]>(),
    setKind: vi.fn<BudgetApi["setKind"]>(),
    setPaused: vi.fn<BudgetApi["setPaused"]>(),
    setBackground: vi.fn<BudgetApi["setBackground"]>(),
    refresh: vi.fn<BudgetApi["refresh"]>(),
  } satisfies BudgetApi;
}

let host: HTMLElement;
let root: Root;

beforeEach(() => {
  // jsdom does not implement scrolling; the detail panel scrolls itself into view when opened.
  Element.prototype.scrollIntoView = vi.fn();
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => { root.unmount(); await Promise.resolve(); });
  host.remove();
});

async function render(api: ReturnType<typeof fakeApi>, active = true): Promise<void> {
  await act(async () => { root.render(<Budget active={active} api={api} pollMs={5} />); await Promise.resolve(); });
  await settle();
}
async function settle(ms = 0): Promise<void> {
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, ms)); });
}
function button(text: string): HTMLButtonElement {
  const found = [...host.querySelectorAll("button")].find((item) => item.textContent.includes(text));
  if (found === undefined) throw new Error(`button ${text} missing`);
  return found;
}
async function click(element: HTMLElement): Promise<void> {
  await act(async () => { element.click(); await Promise.resolve(); });
  await settle();
}

describe("budget view states", () => {
  it.each([
    [404, "budget_disabled", "not enabled"],
    [403, "budget_http_403", "Only the budget's owner"],
    [401, "budget_http_401", "Only the budget's owner"],
    [500, "budget_internal_error", "could not be loaded"],
  ])("shows a plain message for %s without stale data", async (status, error, message) => {
    const api = fakeApi();
    await render(api);
    expect(host.textContent).toContain("Synthetic Current ••0001");
    api.connections.mockImplementation(() => fail(status, error));
    await click(button("Reload"));
    expect(host.textContent).toContain(message);
    expect(host.textContent).not.toContain("Synthetic Current");
  });

  it("renders owner labels, declared and observed currencies and no raw identifiers", async () => {
    const api = fakeApi([account({ display, observed_balance_currencies: ["PLN", "EUR"] }), account({ id: "local-b" })]);
    await render(api);
    const text = host.textContent;
    expect(text).toContain("Synthetic Current ••0001");
    expect(text).toContain("balances seen in: EUR");
    expect(text).toContain("Account 2");
    expect(text).not.toContain("local-a");
    expect(text).not.toContain("local-b");
    expect(text).toContain("automatic refresh is disabled on the server");
  });

  it("renders nothing and fetches nothing while inactive, and reloads on every activation", async () => {
    const api = fakeApi();
    await render(api, false);
    expect(host.textContent).toBe("");
    expect(api.connections).not.toHaveBeenCalled();
    await render(api, true);
    await render(api, false);
    await render(api, true);
    expect(api.connections).toHaveBeenCalledTimes(2);
  });

  it("explains a server without a refresh executor instead of offering the action", async () => {
    const api = fakeApi();
    api.connections.mockImplementation(() => ok({ connections: [connection], capabilities: { ...capabilities, refresh: false } }));
    await render(api);
    expect(host.textContent).toContain("Refreshing is disabled on the server.");
    expect(host.textContent).not.toContain("Refresh from bank");
  });
});

describe("refresh jobs", () => {
  it("keeps one key across a lost response, then polls until the job settles and reloads", async () => {
    const api = fakeApi();
    const keys: string[] = [];
    api.refresh.mockImplementation((_connection, _target, key) => {
      keys.push(key ?? "");
      return keys.length === 1 ? fail(0, "budget_network") : ok({ job: { ...job(), created: keys.length === 2 } });
    });
    let polls = 0;
    api.jobs.mockImplementation(() => {
      polls += 1;
      return ok({ jobs: polls < 3 ? [] : [job({ status: polls < 4 ? "running" : "deferred", next_attempt: 1_795_300_000 })] });
    });
    await render(api);
    await click(button("Refresh from bank"));
    expect(host.textContent).toContain("Could not save");
    await click(button("Refresh from bank"));
    expect(keys[1]).toBe(keys[0]);
    expect(host.textContent).toContain("Refresh queued…");
    for (let round = 0; round < 20 && !host.textContent.includes("not due yet"); round += 1) await settle(10);
    expect(polls).toBeGreaterThanOrEqual(4);
    expect(host.textContent).toContain("Last refresh: not due yet");
    expect(host.textContent).toContain("next possible");
    expect(api.connections.mock.calls.length).toBeGreaterThanOrEqual(2);
    await click(button("Refresh from bank"));
    expect(keys[2]).not.toBe(keys[1]);
  });

  it("explains busy and paused refusals without retrying silently", async () => {
    const api = fakeApi();
    api.refresh.mockImplementation(() => fail(409, "budget_refresh_in_progress"));
    await render(api);
    await click(button("Refresh from bank"));
    expect(host.textContent).toContain("already in progress");
    api.refresh.mockImplementation(() => fail(503, "budget_refresh_unavailable"));
    await click(button("Refresh from bank"));
    expect(host.textContent).toContain("unavailable right now");
    expect(api.refresh).toHaveBeenCalledTimes(2);
  });
});

describe("controls", () => {
  it("reloads and explains a version conflict instead of overwriting the other choice", async () => {
    const api = fakeApi();
    api.setSelected.mockImplementation(() => fail(409, "budget_version_conflict"));
    await render(api);
    api.accounts.mockImplementation(() => ok({ accounts: [account({ display, selected: false, control_version: 2 })] }));
    const checkbox = host.querySelector<HTMLInputElement>(".budget-account input[type=checkbox]");
    if (checkbox === null) throw new Error("selection missing");
    await click(checkbox);
    expect(host.textContent).toContain("changed elsewhere");
    expect(api.connections).toHaveBeenCalledTimes(2);
    expect(host.querySelector<HTMLInputElement>(".budget-account input[type=checkbox]")?.checked).toBe(false);
  });

  it("keeps selection, kind, pause and background as separate saved controls", async () => {
    const api = fakeApi();
    api.setSelected.mockImplementation(() => ok({ account: account({ display, selected: false, control_version: 2 }) }));
    api.setKind.mockImplementation(() => ok({ account: account({ display, selected: false, kind: "savings", kind_confirmed_at: 5, control_version: 3 }) }));
    api.setPaused.mockImplementation(() => ok({ connection: { ...connection, paused: true, control_version: 5 } }));
    api.setBackground.mockImplementation(() => ok({ connection: { ...connection, background_enabled: true, control_version: 6 } }));
    await render(api);
    const [selection, background] = [host.querySelector<HTMLInputElement>(".budget-account input[type=checkbox]"),
      host.querySelector<HTMLInputElement>(".budget-connection > .budget-toggle input")];
    if (selection === null || background === null) throw new Error("controls missing");
    await click(selection);
    expect(api.setSelected).toHaveBeenCalledWith(expect.objectContaining({ id: "local-a" }), false);
    const kind = host.querySelector("select");
    if (kind === null) throw new Error("kind missing");
    await act(async () => { kind.value = "savings"; kind.dispatchEvent(new Event("change", { bubbles: true })); await Promise.resolve(); });
    await settle();
    expect(api.setKind).toHaveBeenCalledWith(expect.objectContaining({ control_version: 2 }), "savings");
    await click(button("Pause"));
    expect(host.textContent).toContain("Paused");
    expect(button("Refresh from bank").disabled).toBe(true);
    await click(background);
    expect(api.setBackground).toHaveBeenCalledWith(expect.objectContaining({ control_version: 5 }), true);
    expect(api.setSelected).toHaveBeenCalledTimes(1);
  });
});

describe("account detail", () => {
  it("never lets a late response for the previous account replace the current one", async () => {
    const api = fakeApi([account({ display }), account({ id: "local-b", display: { ...display, product: "Synthetic Savings", masked_suffix: "0002" } })]);
    const late = deferred<BudgetTransactions>();
    api.transactions.mockImplementation((accountId) => (accountId === "local-a" ? late.promise
      : ok(page([tx("current-b")], null, "local-b"))));
    await render(api);
    await click(button("Synthetic Current"));
    await click(button("Synthetic Savings"));
    expect(host.textContent).toContain("Synthetic current-b");
    await act(async () => { late.settle({ ok: true, data: page([tx("stale-a")]) }); await Promise.resolve(); });
    await settle();
    expect(host.textContent).toContain("Synthetic current-b");
    expect(host.textContent).not.toContain("stale-a");
  });

  it("pages with the cursor, resets it on filter change and rejects an inverted range", async () => {
    const api = fakeApi();
    api.transactions.mockImplementation((_id, options = {}) =>
      options.cursor === "cursor-1" ? ok(page([tx("second", "98765432109876543210.123")]))
        : options.dateFrom ? ok(page([tx("filtered")])) : ok(page([tx("first")], "cursor-1")));
    await render(api);
    await click(button("Synthetic Current"));
    expect(host.textContent).toContain("Synthetic first");
    await click(button("Load more"));
    expect(host.textContent).toContain("Synthetic second");
    expect(host.textContent).toContain("−98,765,432,109,876,543,210.123 PLN");
    await click(button("Last 30 days"));
    const last = api.transactions.mock.lastCall;
    expect(last?.[0]).toBe("local-a");
    expect(last?.[1]?.dateTo).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(last?.[1]?.cursor).toBeUndefined();
    expect(host.textContent).toContain("Synthetic filtered");
    expect(host.textContent).not.toContain("Synthetic first");
    const [from, to] = [...host.querySelectorAll<HTMLInputElement>(".budget-range input")];
    if (from === undefined || to === undefined) throw new Error("range inputs missing");
    const calls = api.transactions.mock.calls.length;
    const setValue = (input: HTMLInputElement, value: string) => {
      // React tracks the prototype setter; an own-property assignment would not register as a change.
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    };
    await act(async () => {
      setValue(from, "2026-11-10");
      setValue(to, "2026-11-01");
      await Promise.resolve();
    });
    await click(button("Apply"));
    expect(host.textContent).toContain("must not be after");
    expect(api.transactions.mock.calls.length).toBe(calls);
  });

  it("shows archived imports only as labelled metadata and a failure plainly", async () => {
    const api = fakeApi();
    api.balances.mockImplementation(() => ok({ ...balancesData,
      balances: [{ name: "Booked", balance_type: "CLBD", balance_amount: { amount: "-15.001", currency: "PLN" }, reference_date: null }],
      balances_total: 1,
      archived_imports: [{ id: "batch", fetched_at: 1_795_000_000, imported_at: 1_795_000_100, balance_count: 2 }] }));
    api.transactions.mockImplementation(() => fail(500, "budget_internal_error"));
    await render(api);
    await click(button("Synthetic Current"));
    expect(host.textContent).toContain("−15.001 PLN");
    expect(host.textContent).toContain("metadata only, not current funds");
    expect(host.textContent).toContain("2 balances");
    expect(host.textContent).toContain("could not be loaded");
    await click(button("Close"));
    expect(host.querySelector(".budget-detail")).toBeNull();
  });
});
