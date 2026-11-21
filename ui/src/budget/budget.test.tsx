import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BudgetApi, BudgetResult } from "./api";
import { Budget } from "./budget";
import { account, componentLimit, connection, sharedLimit } from "./fixtures";
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
    whoami: vi.fn<BudgetApi["whoami"]>(() => ok({ user: "owner", role: "admin" })),
    connections: vi.fn<BudgetApi["connections"]>(() => ok({ connections: [connection], capabilities })),
    accounts: vi.fn<BudgetApi["accounts"]>(() => ok({ accounts })),
    jobs: vi.fn<BudgetApi["jobs"]>(() => ok({ jobs: [] })),
    transactions: vi.fn<BudgetApi["transactions"]>(() => ok(page([]))),
    balances: vi.fn<BudgetApi["balances"]>(() => ok(balancesData)),
    limits: vi.fn<BudgetApi["limits"]>(() => ok({ limits: [] })),
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

async function render(api: ReturnType<typeof fakeApi>, active = true, principal?: string | null): Promise<void> {
  await act(async () => {
    root.render(principal === undefined ? <Budget active={active} api={api} pollMs={5} />
      : <Budget active={active} api={api} pollMs={5} principal={principal} />);
    await Promise.resolve();
  });
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

type Loaded<F extends (...args: never[]) => unknown> = Awaited<ReturnType<F>> extends BudgetResult<infer T> ? T : never;

describe("async lifecycle guards", () => {
  it("never lets an older successful load restore data after a newer unauthorized one", async () => {
    const api = fakeApi();
    await render(api);
    const old = { connections: deferred<Loaded<BudgetApi["connections"]>>(), accounts: deferred<Loaded<BudgetApi["accounts"]>>(),
      jobs: deferred<Loaded<BudgetApi["jobs"]>>() };
    api.connections.mockImplementationOnce(() => old.connections.promise).mockImplementationOnce(() => fail(401, "budget_http_401"));
    api.accounts.mockImplementationOnce(() => old.accounts.promise).mockImplementationOnce(() => fail(401, "budget_http_401"));
    api.jobs.mockImplementationOnce(() => old.jobs.promise).mockImplementationOnce(() => fail(401, "budget_http_401"));
    await click(button("Reload"));
    await click(button("Reload"));
    expect(host.textContent).toContain("Only the budget's owner");
    await act(async () => {
      old.connections.settle({ ok: true, data: { connections: [connection], capabilities } });
      old.accounts.settle({ ok: true, data: { accounts: [account({ display })] } });
      old.jobs.settle({ ok: true, data: { jobs: [] } });
      await Promise.resolve();
    });
    await settle();
    expect(host.textContent).toContain("Only the budget's owner");
    expect(host.textContent).not.toContain("Synthetic Current");
  });

  it("keeps polling after a transient jobs failure and clears data on an unauthorized poll", async () => {
    const api = fakeApi();
    api.jobs.mockImplementationOnce(() => ok({ jobs: [job()] }))
      .mockImplementationOnce(() => fail(500, "budget_internal_error"))
      .mockImplementationOnce(() => ok({ jobs: [job({ status: "running" })] }))
      .mockImplementation(() => ok({ jobs: [job({ status: "updated" })] }));
    await render(api);
    for (let round = 0; round < 20 && !host.textContent.includes("Last refresh: updated"); round += 1) await settle(10);
    expect(host.textContent).toContain("Last refresh: updated");
    expect(api.jobs.mock.calls.length).toBeGreaterThanOrEqual(4);
    api.jobs.mockImplementation(() => ok({ jobs: [job({ id: "job-2" })] }));
    await click(button("Reload"));
    api.jobs.mockImplementation(() => fail(401, "budget_http_401"));
    api.connections.mockImplementation(() => fail(401, "budget_http_401"));
    for (let round = 0; round < 20 && !host.textContent.includes("Only the budget's owner"); round += 1) await settle(10);
    expect(host.textContent).toContain("Only the budget's owner");
    expect(host.textContent).not.toContain("Synthetic Current");
  });

  it("starts no further reads when a pending poll settles after unmount", async () => {
    const api = fakeApi();
    const late = deferred<Loaded<BudgetApi["jobs"]>>();
    api.jobs.mockImplementationOnce(() => ok({ jobs: [job()] })).mockImplementationOnce(() => late.promise);
    await render(api);
    await settle(15);
    expect(api.jobs).toHaveBeenCalledTimes(2);
    await act(async () => { root.unmount(); await Promise.resolve(); });
    root = createRoot(host);
    await act(async () => { late.settle({ ok: true, data: { jobs: [job({ status: "updated" })] } }); await Promise.resolve(); });
    await settle(20);
    expect(api.connections).toHaveBeenCalledTimes(1);
    expect(api.accounts).toHaveBeenCalledTimes(1);
  });

  it("re-reads the opened account when stored data are reloaded", async () => {
    const api = fakeApi();
    api.transactions.mockImplementation(() => ok(page([tx("before-refresh")])));
    await render(api);
    await click(button("Synthetic Current"));
    expect(host.textContent).toContain("Synthetic before-refresh");
    api.transactions.mockImplementation(() => ok(page([tx("after-refresh")])));
    await click(button("Reload"));
    expect(host.textContent).toContain("Synthetic after-refresh");
    expect(host.textContent).not.toContain("before-refresh");
  });

  it("never overwrites a newer loaded version with an older save result", async () => {
    const api = fakeApi();
    const save = deferred<Loaded<BudgetApi["setSelected"]>>();
    api.setSelected.mockImplementation(() => save.promise);
    await render(api);
    const checkbox = () => host.querySelector<HTMLInputElement>(".budget-account input[type=checkbox]");
    await click(checkbox() ?? host);
    api.accounts.mockImplementation(() => ok({ accounts: [account({ display, selected: true, control_version: 3 })] }));
    await click(button("Reload"));
    await act(async () => {
      save.settle({ ok: true, data: { account: account({ display, selected: false, control_version: 2 }) } });
      await Promise.resolve();
    });
    await settle();
    expect(checkbox()?.checked).toBe(true);
  });

  it("drops a save result that arrives after the data were cleared", async () => {
    const api = fakeApi();
    const save = deferred<Loaded<BudgetApi["setSelected"]>>();
    api.setSelected.mockImplementation(() => save.promise);
    await render(api);
    await click(host.querySelector<HTMLInputElement>(".budget-account input[type=checkbox]") ?? host);
    api.connections.mockImplementation(() => fail(403, "budget_http_403"));
    await click(button("Reload"));
    await act(async () => {
      save.settle({ ok: true, data: { account: account({ display, selected: false, control_version: 2 }) } });
      await Promise.resolve();
    });
    await settle();
    expect(host.textContent).toContain("Only the budget's owner");
    expect(host.textContent).not.toContain("Synthetic Current");
  });

  it("clears the view when the cookie no longer names the signed-in principal", async () => {
    const api = fakeApi();
    await render(api, true, "owner");
    expect(host.textContent).toContain("Synthetic Current");
    api.whoami.mockImplementation(() => ok({ user: "someone-else", role: "admin" }));
    await click(button("Reload"));
    expect(host.textContent).toContain("Only the budget's owner");
    expect(host.textContent).not.toContain("Synthetic Current");
    api.whoami.mockImplementation(() => fail(401, "budget_http_401"));
    await click(button("Reload"));
    expect(host.textContent).toContain("Only the budget's owner");
    expect(api.whoami).toHaveBeenCalledTimes(3);
  });
});

describe("access barrier and version-aware reads", () => {
  it("clears at once when any reload response, including jobs, reports lost access", async () => {
    const api = fakeApi();
    await render(api, true, "owner");
    api.jobs.mockImplementation(() => fail(401, "budget_http_401"));
    await click(button("Reload"));
    expect(host.textContent).toContain("Only the budget's owner");
    expect(host.textContent).not.toContain("Synthetic Current");
  });

  it("clears on an unauthorized poll without waiting for a hanging reload", async () => {
    const api = fakeApi();
    api.jobs.mockImplementationOnce(() => ok({ jobs: [job()] })).mockImplementation(() => fail(401, "budget_http_401"));
    await render(api, true, "owner");
    const hanging = deferred<Loaded<BudgetApi["connections"]>>();
    api.connections.mockImplementation(() => hanging.promise);
    for (let round = 0; round < 20 && !host.textContent.includes("Only the budget's owner"); round += 1) await settle(10);
    expect(host.textContent).toContain("Only the budget's owner");
    expect(host.textContent).not.toContain("Synthetic Current");
    await act(async () => { hanging.settle({ ok: true, data: { connections: [connection], capabilities } }); await Promise.resolve(); });
    await settle();
    expect(host.textContent).not.toContain("Synthetic Current");
  });

  it("clears every loaded balance and row when the detail pagination reports lost access", async () => {
    const api = fakeApi();
    api.transactions.mockImplementation((_id, options = {}) => (options.cursor === undefined
      ? ok(page([tx("loaded-before-401")], "opaque-page")) : fail(401, "budget_http_401")));
    api.balances.mockImplementation(() => ok({ ...balancesData, balances: [{ name: "Balance", balance_type: "CLBD",
      balance_amount: { amount: "123.45", currency: "PLN" }, reference_date: null }], balances_total: 1 }));
    await render(api, true, "owner");
    await click(button("Synthetic Current"));
    expect(host.textContent).toContain("123.45 PLN");
    await click(button("Load more"));
    expect(host.textContent).toContain("Only the budget's owner");
    expect(host.textContent).not.toContain("loaded-before-401");
    expect(host.textContent).not.toContain("123.45");
  });

  it.each([["someone-else", "admin"], ["owner", "viewer"]])(
    "clears at once when whoami names %s as %s while another read still hangs", async (user, role) => {
      const api = fakeApi();
      await render(api, true, "owner");
      const hanging = deferred<Loaded<BudgetApi["connections"]>>();
      api.connections.mockImplementation(() => hanging.promise);
      api.whoami.mockImplementation(() => ok({ user, role }));
      await click(button("Reload"));
      expect(host.textContent).toContain("Only the budget's owner");
      expect(host.textContent).not.toContain("Synthetic Current");
      await act(async () => { hanging.settle({ ok: true, data: { connections: [connection], capabilities } }); await Promise.resolve(); });
      await settle();
      expect(host.textContent).not.toContain("Synthetic Current");
    });

  it("clears when the same user is no longer an admin", async () => {
    const api = fakeApi();
    await render(api, true, "owner");
    api.whoami.mockImplementation(() => ok({ user: "owner", role: "viewer" }));
    await click(button("Reload"));
    expect(host.textContent).toContain("Only the budget's owner");
    expect(host.textContent).not.toContain("Synthetic Current");
  });

  it("never lets a delayed post-pause read roll back a newer selection", async () => {
    const api = fakeApi();
    api.setPaused.mockImplementation(() => ok({ connection: { ...connection, paused: true, control_version: 5 } }));
    api.setSelected.mockImplementation(() => ok({ account: account({ display, selected: false, control_version: 2 }) }));
    await render(api, true, "owner");
    const old = deferred<Loaded<BudgetApi["accounts"]>>();
    api.accounts.mockImplementationOnce(() => old.promise);
    await click(button("Pause"));
    await click(host.querySelector<HTMLInputElement>(".budget-account input[type=checkbox]") ?? host);
    expect(host.querySelector<HTMLInputElement>(".budget-account input[type=checkbox]")?.checked).toBe(false);
    await act(async () => {
      old.settle({ ok: true, data: { accounts: [account({ display, selected: true, paused: true, active: false, control_version: 1 })] } });
      await Promise.resolve();
    });
    await settle();
    expect(host.querySelector<HTMLInputElement>(".budget-account input[type=checkbox]")?.checked).toBe(false);
  });
});

describe("download limits", () => {
  const soon = Date.now() / 1000 + 6 * 3600;
  function limitsFor(): Loaded<BudgetApi["limits"]> {
    return { limits: [
      sharedLimit("connection"), sharedLimit("application"), sharedLimit("provider", { retry_at: soon + 600, next_attempt: soon + 600 }),
      componentLimit("local-a", "transactions", { next_attempt: soon, last_attempt: 1_795_000_000 }),
      componentLimit("local-a", "balances", { suspended: true }),
      componentLimit("local-b", "transactions", { lease_until: Date.now() / 1000 + 300 }),
      componentLimit("local-b", "balances"),
      componentLimit("other-account", "balances", { connection: "other-bank" }),
    ] };
  }

  it("loads only on demand, shows readable states per account and offers no way to lift them", async () => {
    const api = fakeApi([account({ display }), account({ id: "local-b", selected: false,
      display: { ...display, product: "Synthetic Savings", masked_suffix: "0002" } })]);
    api.limits.mockImplementation(() => ok(limitsFor()));
    await render(api);
    expect(api.limits).not.toHaveBeenCalled();
    await click(button("Download limits"));
    expect(api.limits).toHaveBeenCalledTimes(1);
    const panel = host.querySelector(".budget-limits");
    expect(panel?.textContent).toContain("This bank: bank asked for a pause until");
    const rows = [...host.querySelectorAll(".budget-limits-table tbody tr")].map((row) => row.textContent);
    expect(rows[0]).toContain("Synthetic Current ••0001");
    expect(rows[0]).toContain("bank asked for a pause until");
    expect(rows[0]).toContain("suspended — needs the operator");
    expect(rows[0]).toContain("last attempt");
    expect(rows[1]).toContain("(excluded)");
    expect(rows[1]).toContain("download in progress");
    expect(host.textContent).not.toContain("local-a");
    expect(host.textContent).not.toContain("other-account");
    expect([...host.querySelectorAll(".budget-limits button")]).toHaveLength(0);
    await click(button("Reload"));
    expect(api.limits).toHaveBeenCalledTimes(2);
    await click(button("Hide limits"));
    expect(host.querySelector(".budget-limits")).toBeNull();
  });

  it("shows free shared limits, a load failure plainly, and clears everything on lost access", async () => {
    const api = fakeApi();
    api.limits.mockImplementation(() => fail(500, "budget_internal_error"));
    await render(api);
    await click(button("Download limits"));
    expect(host.textContent).toContain("Limits could not be loaded.");
    api.limits.mockImplementation(() => ok({ limits: [sharedLimit("connection"), componentLimit("local-a", "balances")] }));
    await click(button("Reload"));
    expect(host.textContent).toContain("Shared limits: no restriction");
    expect(host.textContent).toContain("available now");
    api.limits.mockImplementation(() => fail(401, "budget_http_401"));
    await click(button("Reload"));
    expect(host.textContent).toContain("Only the budget's owner");
    expect(host.querySelector(".budget-limits")).toBeNull();
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

  it("keeps the key while the outcome is unknown and drops it only after a definitive answer", async () => {
    const api = fakeApi();
    const keys: string[] = [];
    type Refreshed = Loaded<BudgetApi["refresh"]>;
    const answers: [number, string][] = [[202, "budget_uncertain"], [500, "budget_internal_error"],
      [409, "budget_refresh_in_progress"], [503, "budget_refresh_unavailable"], [0, "budget_network"]];
    api.refresh.mockImplementation((_connection, _target, key) => {
      keys.push(key ?? "");
      const [status, error] = answers.shift() ?? [0, "budget_network"];
      return fail<Refreshed>(status, error);
    });
    await render(api);
    for (let index = 0; index < 5; index += 1) await click(button("Refresh from bank"));
    expect(keys[1]).toBe(keys[0]);
    expect(keys[2]).toBe(keys[0]);
    expect(keys[3]).not.toBe(keys[2]);
    expect(keys[4]).not.toBe(keys[3]);
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
    expect(host.textContent).toContain("Booked (closing)");
    expect(host.querySelector(".budget-balances span")?.getAttribute("title")).toBe("Booked");
    expect(host.textContent).toContain("metadata only, not current funds");
    expect(host.textContent).toContain("2 balances");
    expect(host.textContent).toContain("could not be loaded");
    await click(button("Close"));
    expect(host.querySelector(".budget-detail")).toBeNull();
  });
});
