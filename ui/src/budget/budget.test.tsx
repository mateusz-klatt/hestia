import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { loadLocale } from "../i18n";
import type { BudgetApi, BudgetResult } from "./api";
import { Budget } from "./budget";
import type { ReceiptsApi } from "./receipts_api";
import { account, componentLimit, connection, sharedLimit } from "./fixtures";
import type { BudgetAccount, BudgetBalances, BudgetJob, BudgetTransaction, BudgetTransactions } from "./model";
import { entriesOf, entry, forecastCurrencyTotal, forecastItem, forecastOf, inboxConsentItem, inboxMarkSaved, inboxOf,
  monthlyBudgetChartsOf, monthlyBudgetOf, mortgageReminder, mortgageSchedule, mortgagesOf, overviewOf, quarantineOf,
  recurringHint, recurringOf, spendingLimit, spendingLimitsOf, spendingLimitStatus, transfersOf } from "./analytics/fixtures";

type Settle<T> = (value: BudgetResult<T>) => void;

function ok<T>(data: T): Promise<BudgetResult<T>> { return Promise.resolve({ ok: true, data }); }
function fail<T>(status: number, error: string): Promise<BudgetResult<T>> { return Promise.resolve({ ok: false, status, error }); }
function deferred<T>(): { promise: Promise<BudgetResult<T>>; settle: Settle<T> } {
  let settle: Settle<T> = () => undefined;
  const promise = new Promise<BudgetResult<T>>((resolve) => { settle = resolve; });
  return { promise, settle };
}

const capabilities = { stored_reads: true, account_controls: true, refresh: true, background_selection: true,
  background_polling: false, consent: false, analytics: false };
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

const authorization = {
  user: "owner", actor_tag: "A".repeat(43), installation_permissions: [],
  own_budget: { permissions: ["budget.view"], can_view: true, can_use_receipts: true, can_manage_devices: true },
  delegated_receipts: null, household_available: false,
};

const balancesData = {
  account_id: "local-a", balances: [] as BudgetBalances["balances"], balances_total: 0, balances_truncated: false,
  freshness: account().components.balances, archived_imports: [] as BudgetBalances["archived_imports"], archived_has_more: false,
};

function fakeApi(accounts: BudgetAccount[] = [account({ display })]) {
  let locked = false;
  return {
    setAuthorizationLocked: vi.fn((value: boolean) => { locked = value; }),
    isAuthorizationLocked: vi.fn(() => locked),
    capabilities: vi.fn<BudgetApi["capabilities"]>(() => ok(authorization)),
    connections: vi.fn<BudgetApi["connections"]>(() => ok({ connections: [connection], capabilities })),
    accounts: vi.fn<BudgetApi["accounts"]>(() => ok({ accounts })),
    jobs: vi.fn<BudgetApi["jobs"]>(() => ok({ jobs: [] })),
    transactions: vi.fn<BudgetApi["transactions"]>(() => ok(page([]))),
    balances: vi.fn<BudgetApi["balances"]>(() => ok(balancesData)),
    limits: vi.fn<BudgetApi["limits"]>(() => ok({ limits: [] })),
    consentStatus: vi.fn<BudgetApi["consentStatus"]>(() => ok({ consents: [] })),
    consentPending: vi.fn<BudgetApi["consentPending"]>(() => ok({ pending: [] })),
    consentRenewalReminders: vi.fn<BudgetApi["consentRenewalReminders"]>(() => ok({
      evaluated_at: 1_800_000_000, lead_days: [14, 7, 3, 1], rule_version: 1, reminders: [],
    })),
    consentReconcile: vi.fn<BudgetApi["consentReconcile"]>(() => ok({
      evaluated_at: 1_800_000_000, retention_days: 90, rule_version: 1,
      summary: { consents_total: 0, flows_total: 0, needs_owner_review: 0, remote_close_eligible: 0, local_retire_eligible: 0 },
      consents: [], flows: [],
    })),
    consentStart: vi.fn<BudgetApi["consentStart"]>(),
    consentCancel: vi.fn<BudgetApi["consentCancel"]>(),
    setSelected: vi.fn<BudgetApi["setSelected"]>(),
    setKind: vi.fn<BudgetApi["setKind"]>(),
    setCreditLimit: vi.fn<BudgetApi["setCreditLimit"]>(),
    setPaused: vi.fn<BudgetApi["setPaused"]>(),
    setBackground: vi.fn<BudgetApi["setBackground"]>(),
    refresh: vi.fn<BudgetApi["refresh"]>(),
    overview: vi.fn<BudgetApi["overview"]>((period) => ok(overviewOf(period))),
    analyticsTransactions: vi.fn<BudgetApi["analyticsTransactions"]>((period) => ok(entriesOf(period))),
    analyticsTransactionsByKeys: vi.fn<BudgetApi["analyticsTransactionsByKeys"]>(() => ok(entriesOf(""))),
    categories: vi.fn<BudgetApi["categories"]>(() => ok({ categories: [] })),
    annotate: vi.fn<BudgetApi["annotate"]>(),
    transfers: vi.fn<BudgetApi["transfers"]>((period) => ok(transfersOf(period))),
    decideTransfer: vi.fn<BudgetApi["decideTransfer"]>(),
    createCategory: vi.fn<BudgetApi["createCategory"]>(),
    updateCategory: vi.fn<BudgetApi["updateCategory"]>(),
    spreadCategoryColors: vi.fn<BudgetApi["spreadCategoryColors"]>(),
    rules: vi.fn<BudgetApi["rules"]>(() => ok({ rules: [] })),
    previewRule: vi.fn<BudgetApi["previewRule"]>(),
    saveRule: vi.fn<BudgetApi["saveRule"]>(),
    recurring: vi.fn<BudgetApi["recurring"]>(() => ok(recurringOf([]))),
    quarantine: vi.fn<BudgetApi["quarantine"]>(() => ok(quarantineOf([]))),
    spendingLimits: vi.fn<BudgetApi["spendingLimits"]>((period) => ok(spendingLimitsOf(period))),
    saveSpendingLimit: vi.fn<BudgetApi["saveSpendingLimit"]>(),
    mortgages: vi.fn<BudgetApi["mortgages"]>(() => ok(mortgagesOf())),
    forecast: vi.fn<BudgetApi["forecast"]>(() => ok(forecastOf())),
    monthlyBudget: vi.fn<BudgetApi["monthlyBudget"]>(() => ok(monthlyBudgetOf())),
    monthlyBudgetCharts: vi.fn<BudgetApi["monthlyBudgetCharts"]>(() => ok(monthlyBudgetChartsOf())),
    saveMortgage: vi.fn<BudgetApi["saveMortgage"]>(),
    setMortgagePayment: vi.fn<BudgetApi["setMortgagePayment"]>(),
    inbox: vi.fn<BudgetApi["inbox"]>(() => ok(inboxOf())),
    markInbox: vi.fn<BudgetApi["markInbox"]>(() => ok({ inbox: inboxMarkSaved() })),
    receiptDevices: vi.fn<BudgetApi["receiptDevices"]>(() => ok([])),
    pairReceiptDevice: vi.fn<BudgetApi["pairReceiptDevice"]>(),
    cancelReceiptPairCode: vi.fn<BudgetApi["cancelReceiptPairCode"]>(),
    revokeReceiptDevice: vi.fn<BudgetApi["revokeReceiptDevice"]>(),
    receiptsList: vi.fn<BudgetApi["receiptsList"]>(() => ok({ receipts: [], next_cursor: null })),
    receiptsSearch: vi.fn<BudgetApi["receiptsSearch"]>(() => ok({ receipts: [], next_cursor: null })),
    receiptDetail: vi.fn<BudgetApi["receiptDetail"]>(),
    receiptImage: vi.fn<BudgetApi["receiptImage"]>(),
    confirmReceipt: vi.fn<BudgetApi["confirmReceipt"]>(),
    deleteReceipt: vi.fn<BudgetApi["deleteReceipt"]>(),
  } satisfies BudgetApi;
}

let host: HTMLElement;
let root: Root;

beforeEach(async () => {
  await loadLocale("en");
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  // jsdom does not implement scrolling; the detail panel scrolls itself into view when opened.
  Element.prototype.scrollIntoView = vi.fn();
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => { root.unmount(); await Promise.resolve(); });
  host.remove();
  vi.unstubAllGlobals();
  await loadLocale("en");
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
  const buttons = [...host.querySelectorAll("button")];
  const found = buttons.find((item) => item.textContent === text) ?? buttons.find((item) => item.textContent.includes(text));
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

  it("hides only the optional device panel when its request is forbidden", async () => {
    const api = fakeApi();
    await render(api, true, "owner");
    api.receiptDevices.mockImplementation(() => fail(403, "budget_forbidden"));
    api.capabilities.mockImplementationOnce(() => ok(authorization)).mockImplementation(() => ok({
      ...authorization,
      own_budget: { ...authorization.own_budget, can_manage_devices: false },
    }));
    await click(button("Reload"));
    expect(host.querySelector(".budget-devices")).toBeNull();
    expect(host.textContent).toContain("Synthetic Current");
    expect(host.textContent).not.toContain("Only the budget's owner");
    expect(api.capabilities.mock.calls.length).toBeGreaterThanOrEqual(3);
  });

  it("keeps device administration when refreshed capabilities still allow it", async () => {
    const api = fakeApi();
    await render(api, true, "owner");
    api.pairReceiptDevice.mockImplementation(() => fail(403, "budget_forbidden"));
    await click(button("Add device"));
    expect(host.querySelector(".budget-devices")).not.toBeNull();
    expect(host.textContent).toContain("Synthetic Current");
    expect(host.textContent).not.toContain("Only the budget's owner");
  });

  it("shows only a localized server-change notice and makes no Budget requests on a 404", async () => {
    const api = fakeApi();
    api.capabilities.mockImplementation(() => fail(404, "budget_http_404"));
    await loadLocale("pl");
    await render(api, true, "owner");
    expect(host.textContent).toBe("Serwer został zmieniony — odśwież stronę");
    expect(host.querySelector("button")).toBeNull();
    for (const request of [api.connections, api.accounts, api.jobs, api.receiptDevices]) {
      expect(request).not.toHaveBeenCalled();
    }
  });

  it.each(["focus", "visibilitychange"])("revalidates and clears private state on %s", async (eventName) => {
    const api = fakeApi();
    await render(api, true, "owner");
    const checks = api.capabilities.mock.calls.length;
    api.capabilities.mockImplementation(() => fail(404, "budget_http_404"));
    await act(async () => {
      (eventName === "focus" ? window : document).dispatchEvent(new Event(eventName));
      await Promise.resolve();
    });
    await settle();
    expect(api.capabilities).toHaveBeenCalledTimes(checks + 1);
    expect(host.textContent).toBe("The server has changed — refresh the page");
  });

  it.each([503, 0])("keeps mutations locked after a capability refresh fails with %s", async (status) => {
    const api = fakeApi();
    await render(api, true, "owner");
    api.pairReceiptDevice.mockImplementation(() => fail(403, "budget_forbidden"));
    api.capabilities.mockImplementation(() => fail(status, "budget_unavailable"));
    await click(button("Add device"));
    expect(api.setAuthorizationLocked).toHaveBeenLastCalledWith(true);
    await click(button("Pause"));
    expect(api.setPaused).not.toHaveBeenCalled();
    expect(host.textContent).toContain("The server has changed — refresh the page");

    api.capabilities.mockImplementation(() => ok(authorization));
    api.setPaused.mockImplementation(() => ok({ connection: { ...connection, paused: true } }));
    await click(button("Reload"));
    expect(api.setAuthorizationLocked).toHaveBeenLastCalledWith(false);
    await click(button("Pause"));
    expect(api.setPaused).toHaveBeenCalledTimes(1);
  });

  it("does not retry a forbidden device list until capabilities change", async () => {
    const api = fakeApi();
    await render(api, true, "owner");
    api.receiptDevices.mockImplementation(() => fail(403, "budget_origin_required"));
    await click(button("Reload"));
    await settle(20);
    expect(api.receiptDevices).toHaveBeenCalledTimes(2);
    expect(host.querySelector(".budget-devices")).toBeNull();
    await click(button("Reload"));
    await settle(20);
    expect(api.receiptDevices).toHaveBeenCalledTimes(2);
  });

  it("clears the ledger when an owner request reports an actor change", async () => {
    const api = fakeApi();
    await render(api, true, "owner");
    api.connections.mockImplementation(() => fail(409, "budget_actor_changed"));
    await click(button("Reload"));
    expect(host.textContent).toContain("Only the budget's owner");
    expect(host.textContent).not.toContain("Synthetic Current");
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
    api.capabilities.mockImplementation(() => ok({ ...authorization, user: "someone-else" }));
    await click(button("Reload"));
    expect(host.textContent).toContain("Only the budget's owner");
    expect(host.textContent).not.toContain("Synthetic Current");
    api.capabilities.mockImplementation(() => fail(401, "budget_http_401"));
    await click(button("Reload"));
    expect(host.textContent).toContain("Only the budget's owner");
    expect(api.capabilities).toHaveBeenCalledTimes(3);
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

  it.each([["someone-else", true], ["owner", false]])(
    "clears at once for actor %s with can_view=%s while another read still hangs", async (user, canView) => {
      const api = fakeApi();
      await render(api, true, "owner");
      const hanging = deferred<Loaded<BudgetApi["connections"]>>();
      api.connections.mockImplementation(() => hanging.promise);
      api.capabilities.mockImplementation(() => ok({
        ...authorization, user,
        own_budget: canView ? authorization.own_budget : { ...authorization.own_budget, can_view: false },
      }));
      await click(button("Reload"));
      expect(host.textContent).toContain("Only the budget's owner");
      expect(host.textContent).not.toContain("Synthetic Current");
      await act(async () => { hanging.settle({ ok: true, data: { connections: [connection], capabilities } }); await Promise.resolve(); });
      await settle();
      expect(host.textContent).not.toContain("Synthetic Current");
    });

  it("keeps ledger access when the actor still has own-budget view without a legacy role", async () => {
    const api = fakeApi();
    await render(api, true, "owner");
    await click(button("Reload"));
    expect(host.textContent).toContain("Synthetic Current");
    expect(host.textContent).not.toContain("Only the budget's owner");
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

describe("bank consent", () => {
  const consentCaps = { ...capabilities, consent: true };
  async function renderConsent(api: ReturnType<typeof fakeApi>, navigate = vi.fn<(url: string) => void>()) {
    api.connections.mockImplementation(() => ok({ connections: [connection], capabilities: consentCaps }));
    await act(async () => {
      root.render(<Budget active api={api} pollMs={5} principal="owner" navigate={navigate} completeUrl="/synthetic/complete" />);
      await Promise.resolve();
    });
    await settle();
    return navigate;
  }
  function flowOf(id: string, status: string) {
    return { id, connection_id: "bank", intent: "renew" as const, status, error: null, created_at: 1, expires_at: 2,
      candidate_consent_id: null };
  }

  it("is absent until the server offers consent", async () => {
    const api = fakeApi();
    await render(api, true, "owner");
    expect(host.textContent).not.toContain("Connect bank");
    expect(api.consentStatus).not.toHaveBeenCalled();
  });

  it("starts a connect flow, then leaves only for the HTTPS authorization URL", async () => {
    const api = fakeApi();
    api.consentStart.mockImplementation(() => ok({ flow_id: "flow-1", authorization_url: "https://bank.example.test/auth?x=1", expires_at: 9 }));
    const navigate = await renderConsent(api);
    expect(host.textContent).toContain("Hestia never sees your banking password");
    await click(button("Connect bank"));
    expect(api.consentStart).toHaveBeenCalledWith(expect.objectContaining({ id: "bank", control_version: 4 }), "connect");
    expect(navigate).toHaveBeenCalledWith("https://bank.example.test/auth?x=1");
    expect(button("Opening the bank").disabled).toBe(true);
  });

  it("uses renew for a connection that has worked before and refuses a non-HTTPS URL", async () => {
    const api = fakeApi();
    api.consentStart.mockImplementation(() => ok({ flow_id: "flow-1", authorization_url: "javascript:alert(1)", expires_at: 9 }));
    api.connections.mockImplementation(() => ok({ connections: [{ ...connection, last_success: 5 }], capabilities: consentCaps }));
    const navigate = vi.fn<(url: string) => void>();
    await act(async () => { root.render(<Budget active api={api} pollMs={5} principal="owner" navigate={navigate} />); await Promise.resolve(); });
    await settle();
    await click(button("Renew bank consent"));
    expect(api.consentStart).toHaveBeenCalledWith(expect.anything(), "renew");
    expect(navigate).not.toHaveBeenCalled();
    expect(host.textContent).toContain("invalid address; nothing was opened");
    expect(button("Renew bank consent").disabled).toBe(false);
  });

  it.each([[409, "budget_version_conflict", "changed elsewhere"], [409, "budget_consent_in_progress", "already in progress"],
    [429, "budget_consent_rate_limited", "within an hour"], [409, "budget_consent_psu_context_required", "usual address"],
    [503, "budget_consent_unavailable", "Could not start"], [409, "budget_consent_something_new", "Could not start"]])(
    "explains a %s %s start refusal without leaving the page", async (status, error, text) => {
      const api = fakeApi();
      api.consentStart.mockImplementation(() => fail(status, error));
      const navigate = await renderConsent(api);
      await click(button("Connect bank"));
      expect(host.textContent).toContain(text);
      expect(navigate).not.toHaveBeenCalled();
    });

  it("reloads after an in-progress refusal so the unfinished flow's banner appears", async () => {
    const api = fakeApi();
    api.consentStart.mockImplementation(() => fail(409, "budget_consent_in_progress"));
    await renderConsent(api);
    expect(host.querySelector(".budget-pending")).toBeNull();
    api.consentPending.mockImplementation(() => ok({ pending: [flowOf("flow-open", "staged")] }));
    const before = api.connections.mock.calls.length;
    await click(button("Connect bank"));
    await settle();
    expect(api.connections.mock.calls.length).toBeGreaterThan(before);
    expect(host.querySelector(".budget-pending")?.textContent).toContain("Synthetic Bank");
    expect(host.textContent).toContain("already in progress");
  });

  it("lists a flow abandoned at the bank with cancel only, so a blocked start can be cleared", async () => {
    const api = fakeApi();
    api.consentPending.mockImplementation(() => ok({ pending: [flowOf("flow-left", "started"), flowOf("flow-new", "created"),
      flowOf("flow-done", "adopted"), flowOf("flow-gone", "canceled")] }));
    api.consentCancel.mockImplementation(() => ok({ flow_id: "flow-left", status: "canceled" }));
    await renderConsent(api);
    const banner = host.querySelector(".budget-pending");
    expect(banner?.querySelectorAll("li")).toHaveLength(2);
    expect(banner?.querySelector("a")).toBeNull();
    expect(banner?.textContent).toContain("started at the bank");
    const cancel = [...(banner?.querySelectorAll("button") ?? [])].find((item) => item.textContent === "Cancel");
    if (cancel === undefined) throw new Error("cancel missing");
    await click(cancel);
    expect(api.consentCancel).toHaveBeenCalledWith("flow-left");
    expect(host.querySelectorAll(".budget-pending li")).toHaveLength(1);
  });

  it("shows consent validity, a waiting candidate and unfinished flows with finish and cancel", async () => {
    const api = fakeApi();
    api.consentStatus.mockImplementation(() => ok({ consents: [
      { id: "c1", connection_id: "bank", provenance: "hestia_created", status: "ACTIVE", valid_until: 1_810_000_000, generation: 2, created_at: 1 },
      { id: "c2", connection_id: "bank", provenance: "hestia_created", status: "UNADOPTED", valid_until: null, generation: 3, created_at: 2 },
      { id: "c0", connection_id: "bank", provenance: "hestia_created", status: "RETIRED", valid_until: 1, generation: 1, created_at: 0 },
    ] }));
    api.consentPending.mockImplementation(() => ok({ pending: [flowOf("flow-ready", "staged"), flowOf("flow-old", "expired")] }));
    api.consentCancel.mockImplementation(() => ok({ flow_id: "flow-ready", status: "canceled" }));
    await renderConsent(api);
    expect(host.textContent).toContain("Consent from Hestia valid until");
    expect(host.textContent).toContain("A new consent is waiting to be finished");
    const banner = host.querySelector(".budget-pending");
    expect(banner?.textContent).toContain("Synthetic Bank");
    expect(banner?.querySelector("a")?.getAttribute("href")).toBe("/synthetic/complete");
    expect(banner?.querySelectorAll("li")).toHaveLength(1);
    expect(host.textContent).not.toContain("flow-ready");
    expect(host.textContent).not.toContain("generation");
    const cancel = [...(banner?.querySelectorAll("button") ?? [])].find((item) => item.textContent === "Cancel");
    if (cancel === undefined) throw new Error("cancel missing");
    await click(cancel);
    expect(api.consentCancel).toHaveBeenCalledWith("flow-ready");
    expect(host.querySelector(".budget-pending")).toBeNull();
  });

  it("clears everything when a consent read reports lost access", async () => {
    const api = fakeApi();
    api.consentPending.mockImplementation(() => fail(401, "budget_http_401"));
    await renderConsent(api);
    expect(host.textContent).toContain("Only the budget's owner");
    expect(host.textContent).not.toContain("Synthetic Current");
  });

  it("opens consent health on demand only when consent is enabled and clears everything on 401", async () => {
    const api = fakeApi();
    await render(api);
    expect([...host.querySelectorAll("button")].some((item) => item.textContent === "Consent status")).toBe(false);
    api.consentRenewalReminders.mockImplementation(() => ok({
      evaluated_at: 1_800_000_000, lead_days: [14, 7, 3, 1], rule_version: 1,
      reminders: [{ connection_id: "bank", bank_name: "Synthetic Bank", valid_until: 1_815_379_200,
        seconds_remaining: 178 * 86400, days_remaining: 178, status: "healthy", active_milestone: null,
        reminder_due: false, paused: false, attention: null, destination_kind: "owner_inbox", rule_version: 1 }],
    }));
    await renderConsent(api);
    expect(api.consentRenewalReminders).not.toHaveBeenCalled();
    await click(button("Consent status"));
    expect(api.consentRenewalReminders).toHaveBeenCalledTimes(1);
    expect(api.consentReconcile).toHaveBeenCalledTimes(1);
    expect(host.textContent).toContain("External consents (imported): 1 · managed by Hestia: 0");
    await click(button("Hide consent status"));
    expect(host.querySelector(".budget-consent-health")).toBeNull();

    api.consentReconcile.mockImplementationOnce(() => fail(401, "budget_http_401"));
    await click(button("Consent status"));
    expect(host.textContent).toContain("Only the budget's owner");
    expect(host.querySelector(".budget-consent-health")).toBeNull();
  });
});

describe("consent lifecycle guards", () => {
  function enable(api: ReturnType<typeof fakeApi>) {
    api.connections.mockImplementation(() => ok({ connections: [connection], capabilities: { ...capabilities, consent: true } }));
  }
  async function scoped(api: ReturnType<typeof fakeApi>, navigate: (url: string) => void, principal = "owner", active = true) {
    await act(async () => { root.render(<Budget active={active} api={api} pollMs={5} principal={principal} navigate={navigate} />); await Promise.resolve(); });
    await settle();
  }
  const started = { ok: true as const, data: { flow_id: "flow-old", authorization_url: "https://bank.example.test/start?old", expires_at: 9 } };

  it.each(["status", "pending"])("clears at once on a %s 401 even while the sibling consent read hangs", async (failed) => {
    const api = fakeApi();
    enable(api);
    if (failed === "pending") {
      api.consentStatus.mockImplementation(() => new Promise(() => undefined));
      api.consentPending.mockImplementation(() => fail(401, "budget_http_401"));
    } else {
      api.consentStatus.mockImplementation(() => fail(401, "budget_http_401"));
      api.consentPending.mockImplementation(() => new Promise(() => undefined));
    }
    await scoped(api, vi.fn());
    expect(host.textContent).not.toContain("Synthetic Current");
    expect(host.textContent).toContain("Only the budget's owner");
  });

  it("does not navigate from a start answer that belongs to a previous principal", async () => {
    const api = fakeApi();
    enable(api);
    const navigate = vi.fn<(url: string) => void>();
    const late = deferred<Loaded<BudgetApi["consentStart"]>>();
    api.consentStart.mockImplementation(() => late.promise);
    await scoped(api, navigate);
    await click(button("Connect bank"));
    api.capabilities.mockImplementation(() => ok({ ...authorization, user: "new-owner" }));
    await scoped(api, navigate, "new-owner");
    await act(async () => { late.settle(started); await Promise.resolve(); });
    await settle();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("does not navigate after the owner leaves the budget tab while a start is pending", async () => {
    const api = fakeApi();
    enable(api);
    const navigate = vi.fn<(url: string) => void>();
    const late = deferred<Loaded<BudgetApi["consentStart"]>>();
    api.consentStart.mockImplementation(() => late.promise);
    await scoped(api, navigate);
    await click(button("Connect bank"));
    await scoped(api, navigate, "owner", false);
    expect(host.textContent).toBe("");
    await act(async () => { late.settle(started); await Promise.resolve(); });
    await settle();
    expect(navigate).not.toHaveBeenCalled();
    await scoped(api, navigate, "owner", true);
    await click(button("Connect bank"));
    expect(api.consentStart).toHaveBeenCalledTimes(2);
  });
});

describe("bank navigation commit timing", () => {
  it("blocks navigation as soon as the hidden view commits, before passive effects run", async () => {
    const api = fakeApi();
    const navigate = vi.fn<(url: string) => void>();
    api.connections.mockImplementation(() => ok({ connections: [connection], capabilities: { ...capabilities, consent: true } }));
    const late = deferred<Loaded<BudgetApi["consentStart"]>>();
    api.consentStart.mockImplementation(() => late.promise);
    await act(async () => { root.render(<Budget active api={api} principal="owner" navigate={navigate} />); await Promise.resolve(); });
    await settle();
    await click(button("Connect bank"));
    await new Promise<void>((done, reject) => {
      const timer = setTimeout(() => { reject(new Error("hide commit not observed")); }, 2000);
      const observer = new MutationObserver(() => {
        if (host.textContent !== "") return;
        observer.disconnect();
        clearTimeout(timer);
        late.settle({ ok: true, data: { flow_id: "old", authorization_url: "https://bank.example.test/start?old", expires_at: 9 } });
        setTimeout(done, 20);
      });
      observer.observe(host, { childList: true, subtree: true });
      // A normal (non-act) render, as a real tab switch would schedule it.
      root.render(<Budget active={false} api={api} principal="owner" navigate={navigate} />);
    });
    expect(host.textContent).toBe("");
    expect(navigate).not.toHaveBeenCalled();
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

describe("lost access always wins over local discards", () => {
  const ledger = () => ok(page([tx("private-loaded-ledger")]));
  const balance = () => ok({ ...balancesData, balances: [{ name: "Balance", balance_type: "CLBD",
    balance_amount: { amount: "123.45", currency: "PLN" }, reference_date: null }], balances_total: 1 });

  it.each([[401, "budget_http_401"], [403, "budget_http_403"], [404, "budget_disabled"]])(
    "a late limits %s still clears loaded financial data after Hide limits", async (status, error) => {
      const api = fakeApi();
      const pending = deferred<Loaded<BudgetApi["limits"]>>();
      api.limits.mockImplementation(() => pending.promise);
      api.transactions.mockImplementation(ledger);
      api.balances.mockImplementation(balance);
      await render(api, true, "owner");
      await click(button("Synthetic Current"));
      expect(host.textContent).toContain("123.45 PLN");
      await click(button("Download limits"));
      await click(button("Hide limits"));
      await act(async () => { pending.settle({ ok: false, status, error }); await Promise.resolve(); });
      await settle();
      expect(host.textContent).not.toContain("private-loaded-ledger");
      expect(host.textContent).not.toContain("123.45");
      expect(host.textContent).not.toContain("Synthetic Current");
      expect(host.textContent).toContain(status === 404 ? "not enabled" : "Only the budget's owner");
    });

  it("a late 401 for a previously opened account still clears the view", async () => {
    const api = fakeApi([account({ display }), account({ id: "local-b", display: { ...display, product: "Synthetic Savings", masked_suffix: "0002" } })]);
    const late = deferred<Loaded<BudgetApi["transactions"]>>();
    api.transactions.mockImplementation((accountId) => (accountId === "local-a" ? late.promise : ledger()));
    await render(api, true, "owner");
    await click(button("Synthetic Current"));
    await click(button("Synthetic Savings"));
    expect(host.textContent).toContain("private-loaded-ledger");
    await act(async () => { late.settle({ ok: false, status: 401, error: "budget_http_401" }); await Promise.resolve(); });
    await settle();
    expect(host.textContent).not.toContain("private-loaded-ledger");
    expect(host.textContent).toContain("Only the budget's owner");
  });

  it("a late 401 from an older reload still clears the view", async () => {
    const api = fakeApi();
    await render(api, true, "owner");
    const old = deferred<Loaded<BudgetApi["jobs"]>>();
    api.jobs.mockImplementationOnce(() => old.promise);
    await click(button("Reload"));
    await click(button("Reload"));
    expect(host.textContent).toContain("Synthetic Current");
    await act(async () => { old.settle({ ok: false, status: 401, error: "budget_http_401" }); await Promise.resolve(); });
    await settle();
    expect(host.textContent).not.toContain("Synthetic Current");
    expect(host.textContent).toContain("Only the budget's owner");
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

  it("shows, validates, saves, and clears credit limit only for confirmed credit_card accounts", async () => {
    const api = fakeApi([
      account({ id: "acct-current", display, kind: "current", kind_confirmed_at: 5, control_version: 1 }),
      account({
        id: "acct-card",
        display: { ...display, product: "Synthetic Card", masked_suffix: "9999" },
        kind: "credit_card",
        kind_confirmed_at: 5,
        control_version: 2,
        observed_balance_currencies: ["PLN"],
        credit_limit: null,
      }),
    ]);
    api.setCreditLimit
      .mockImplementationOnce(() => ok({
        account: account({
          id: "acct-card",
          display: { ...display, product: "Synthetic Card", masked_suffix: "9999" },
          kind: "credit_card",
          kind_confirmed_at: 5,
          control_version: 3,
          observed_balance_currencies: ["PLN"],
          credit_limit: { amount: "10000", currency: "EUR" },
        }),
      }))
      .mockImplementationOnce(() => ok({
        account: account({
          id: "acct-card",
          display: { ...display, product: "Synthetic Card", masked_suffix: "9999" },
          kind: "credit_card",
          kind_confirmed_at: 5,
          control_version: 4,
          observed_balance_currencies: ["PLN"],
          credit_limit: null,
        }),
      }));

    await render(api);
    const inputs = host.querySelectorAll<HTMLInputElement>('input[name="credit_limit"]');
    expect(inputs).toHaveLength(1);
    const input = inputs[0];
    if (input === undefined) throw new Error("credit_limit input missing");
    const currencyInput = host.querySelector<HTMLInputElement>('input[name="credit_limit_currency"]');
    if (currencyInput === null) throw new Error("credit_limit_currency input missing");
    expect(currencyInput.value).toBe("PLN");

    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, "0");
      input.dispatchEvent(new Event("input", { bubbles: true }));
      await Promise.resolve();
    });
    await click(button("Save limit"));
    expect(api.setCreditLimit).not.toHaveBeenCalled();
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("greater than 0");

    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, "10000,00");
      input.dispatchEvent(new Event("input", { bubbles: true }));
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(currencyInput, "eur");
      currencyInput.dispatchEvent(new Event("input", { bubbles: true }));
      await Promise.resolve();
    });
    await click(button("Save limit"));
    expect(api.setCreditLimit).toHaveBeenCalledWith(
      expect.objectContaining({ id: "acct-card", control_version: 2 }),
      { amount: "10000", currency: "EUR" },
    );

    await click(button("Clear"));
    expect(api.setCreditLimit).toHaveBeenLastCalledWith(expect.objectContaining({ id: "acct-card", control_version: 3 }), null);
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

describe("analytics views", () => {
  const analysis = { ...capabilities, analytics: true };

  it("offers no month view until the server enables analytics", async () => {
    const api = fakeApi();
    await render(api);
    expect(host.textContent).toContain("Synthetic Current ••0001");
    expect(host.querySelector(".budget-views")).toBeNull();
    expect(api.overview).not.toHaveBeenCalled();
    expect(api.monthlyBudgetCharts).not.toHaveBeenCalled();
  });

  it("checks capabilities first when the actor changes and Rules opens after a rollback", async () => {
    const api = fakeApi();
    api.capabilities.mockImplementation(() => ok({ ...authorization, user: "alice" }));
    api.connections.mockImplementation(() => ok({ connections: [connection], capabilities: analysis }));
    await render(api, true, "alice");
    api.categories.mockClear();
    api.rules.mockClear();
    const checks = api.capabilities.mock.calls.length;
    api.capabilities.mockImplementation(() => fail(404, "budget_http_404"));
    await click(button("Categories and rules"));
    expect(api.capabilities).toHaveBeenCalledTimes(checks + 1);
    expect(api.categories).not.toHaveBeenCalled();
    expect(api.rules).not.toHaveBeenCalled();
    expect(host.textContent).toBe("The server has changed — refresh the page");
  });

  it("opens Accounts from the header and scrolls to the devices panel", async () => {
    const api = fakeApi();
    api.connections.mockImplementation(() => ok({ connections: [connection], capabilities: analysis }));
    await render(api);
    expect(host.querySelector(".budget-devices")).toBeNull();
    const scrollSpy = vi.spyOn(Element.prototype, "scrollIntoView");
    scrollSpy.mockClear();
    await click(button("Devices (Vesta)"));
    expect(button("Accounts").getAttribute("aria-pressed")).toBe("true");
    expect(host.querySelector(".budget-devices")).not.toBeNull();
    expect(api.receiptDevices).toHaveBeenCalledTimes(1);
    expect(scrollSpy).toHaveBeenCalledWith({ behavior: "smooth", block: "start" });
  });

  it("gates the quarantine tab on analytics, defaults to Monthly budget, and keeps Quarantine after Forecast", async () => {
    const api = fakeApi();
    await render(api);
    expect([...host.querySelectorAll("button")].some((item) => item.textContent === "Quarantine")).toBe(false);
    expect([...host.querySelectorAll("button")].some((item) => item.textContent === "Monthly budget")).toBe(false);
    expect(api.quarantine).not.toHaveBeenCalled();
    expect(api.monthlyBudgetCharts).not.toHaveBeenCalled();

    api.connections.mockImplementation(() => ok({ connections: [connection], capabilities: analysis }));
    await click(button("Reload"));
    const tabs = [...host.querySelectorAll(".budget-views button")].map((item) => item.textContent);
    expect(tabs[0]).toBe("Monthly budget");
    expect(button("Monthly budget").getAttribute("aria-pressed")).toBe("true");
    expect(api.monthlyBudgetCharts).toHaveBeenCalledTimes(1);
    expect(tabs.indexOf("Quarantine")).toBe(tabs.indexOf("Forecast") + 1);
    await click(button("Quarantine"));
    expect(api.quarantine).toHaveBeenCalledWith(undefined);
    expect(host.textContent).toContain("All entries are included in totals.");
    expect(button("Quarantine").getAttribute("aria-pressed")).toBe("true");

    api.connections.mockImplementation(() => ok({ connections: [connection], capabilities }));
    await click(button("Reload"));
    expect(host.querySelector(".budget-views")).toBeNull();
    expect(host.textContent).not.toContain("All entries are included in totals.");
    expect(host.textContent).toContain("Synthetic Current ••0001");
  });

  it("defaults to Monthly budget and links directly to Month view for the same month", async () => {
    const api = fakeApi();
    api.connections.mockImplementation(() => ok({ connections: [connection], capabilities: analysis }));
    api.monthlyBudgetCharts.mockImplementation(() => ok(monthlyBudgetChartsOf({
      period: "2026-11",
      current_month: "2026-11",
      cutoff: "2026-11-22",
      summary: { start_budget: "5000", spent: "320.5", remaining: "4679.5" },
    })));
    await render(api);
    expect(button("Monthly budget").getAttribute("aria-pressed")).toBe("true");
    expect(host.textContent).toContain("5,000.00 PLN");
    await click(button("View transactions"));
    expect(button("Month").getAttribute("aria-pressed")).toBe("true");
    expect(api.overview).toHaveBeenLastCalledWith("2026-11");

    // Malformed server current_month falls back cleanly without breaking Month view navigation.
    await click(button("Monthly budget"));
    api.monthlyBudgetCharts.mockImplementation(() => ok(monthlyBudgetChartsOf({ period: "2026-10", cutoff: "2026-10-31", current_month: "invalid" })));
    await click(button("Reload"));
    await click(button("View transactions"));
    expect(button("Month").getAttribute("aria-pressed")).toBe("true");
  });

  it("opens a closed month from Monthly budget with Next month enabled and without the in-progress notice", async () => {
    const api = fakeApi();
    api.connections.mockImplementation(() => ok({ connections: [connection], capabilities: analysis }));
    api.monthlyBudgetCharts.mockImplementation(() => ok(monthlyBudgetChartsOf({
      period: "2024-01",
      start: "2024-01-01",
      end: "2024-01-31",
      cutoff: "2024-01-31",
      current_month: "2024-02",
    })));
    await render(api);

    await click(button("View transactions"));
    expect(button("Month").getAttribute("aria-pressed")).toBe("true");
    expect(api.overview).toHaveBeenLastCalledWith("2024-01");
    expect(host.querySelector(".budget-month-nav h3")?.textContent).toBe("January 2024");
    const nextMonth = () => host.querySelector<HTMLButtonElement>('button[aria-label="Next month"]');
    expect(nextMonth()?.disabled).toBe(false);
    expect(host.textContent).not.toContain("This period is still in progress.");

    await click(nextMonth() as HTMLButtonElement);
    expect(api.overview).toHaveBeenLastCalledWith("2024-02");
    expect(host.querySelector(".budget-month-nav h3")?.textContent).toBe("February 2024");
    expect(nextMonth()?.disabled).toBe(true);
    expect(host.textContent).toContain("This period is still in progress.");
  });

  it("opens Month transactions filtered to the provisional rows when clicking View transactions on the provisional banner", async () => {
    const api = fakeApi();
    api.connections.mockImplementation(() => ok({ connections: [connection], capabilities: analysis }));
    const provRows = [
      entry("prov-1", { account_id: "acc-secret-a", booking_date: "2026-11-10", remittance_information: ["Synthetic Prov One"] }),
      entry("prov-2-dec", {
        account_id: "acc-secret-a",
        booking_date: "2026-12-02",
        transaction_date: "2026-11-29",
        remittance_information: ["Synthetic Prov Two Dec"],
        effective: { kind: "expense", category_id: "cat-food", source: "rule", rule_id: "rule-auto" },
      }),
      entry("prov-3-oct", {
        account_id: "acc-secret-a",
        booking_date: "2026-10-28",
        remittance_information: ["Synthetic Prov Three Oct"],
        transfer: { status: "review", decision_id: "d-1", candidate_count: 1, ambiguous: false },
      }),
      entry("prov-4", {
        account_id: "acc-secret-a",
        remittance_information: ["Synthetic Prov Four"],
        annotation: { version: 1, mode: "manual", category_id: null, kind: "excluded", stale: true },
      }),
    ];
    const novRows = [
      provRows[0] as typeof provRows[number],
      provRows[3] as typeof provRows[number],
      entry("clean-rule", { remittance_information: ["Synthetic Clean Rule"], effective: { kind: "expense", category_id: "cat-food", source: "rule", rule_id: "rule-exp" } }),
      entry("clean-manual", {
        remittance_information: ["Synthetic Clean Manual"],
        annotation: { version: 1, mode: "manual", category_id: "cat-food", kind: "expense", stale: false },
        effective: { kind: "expense", category_id: "cat-food", source: "manual", rule_id: null },
      }),
      entry("nov-extra-1", { remittance_information: ["Synthetic Nov Extra 1"] }),
      entry("nov-extra-2", { remittance_information: ["Synthetic Nov Extra 2"] }),
    ];
    const keys = [
      "acc-secret-a/prov-1",
      "acc-secret-a/prov-2-dec",
      "acc-secret-a/prov-3-oct",
      "acc-secret-a/prov-4",
    ];
    api.monthlyBudgetCharts.mockImplementation(() => ok(monthlyBudgetChartsOf({
      period: "2026-11",
      current_month: "2026-11",
      cutoff: "2026-11-22",
      currency: "PLN",
      coverage: {
        ledger_as_of: 1_795_345_200,
        stale_selected_accounts: 0,
        purchase_date_fallback_count: 0,
        pending_transfer_rows: 4,
        pending_row_keys: keys,
        pending_row_keys_truncated: false,
        transfer_scan_limited: false,
        provisional: true,
        has_activity: true,
      },
    })));
    api.transfers.mockImplementation((period) => ok(transfersOf(period, [])));
    api.analyticsTransactions.mockImplementation((period) => ok(entriesOf(period, novRows)));
    api.analyticsTransactionsByKeys.mockImplementation(() => ok(entriesOf("", provRows)));

    await render(api);
    for (const secret of [...keys, "acc-secret-a", "prov-2-dec", "prov-3-oct"]) {
      expect(host.textContent).not.toContain(secret);
    }
    const scrollSpy = vi.spyOn(Element.prototype, "scrollIntoView");
    const links = [...host.querySelectorAll("button")].filter((item) => item.textContent === "View transactions");
    expect(links).toHaveLength(2);
    await click(links[1] as HTMLButtonElement);

    expect(button("Month").getAttribute("aria-pressed")).toBe("true");
    expect(api.rules).not.toHaveBeenCalled();
    expect(api.analyticsTransactionsByKeys).toHaveBeenCalledWith({ row_keys: keys, currency: "PLN" });
    const provisionalToggle = host.querySelector<HTMLInputElement>('input[name="provisional"]');
    expect(provisionalToggle?.checked).toBe(true);
    expect(host.querySelector<HTMLSelectElement>('select[name="currency"]')?.value).toBe("PLN");
    expect(scrollSpy).toHaveBeenCalled();
    expect(scrollSpy.mock.instances).toContain(host.querySelector(".budget-entries"));

    const visibleTexts = () => [...host.querySelectorAll(".budget-entries .budget-entry-text")].map((item) => item.textContent);
    expect(visibleTexts()).toEqual([
      "Synthetic Prov One",
      "Synthetic Prov Two Dec",
      "Synthetic Prov Three Oct",
      "Synthetic Prov Four",
    ]);
    expect(host.querySelector(".budget-entries")?.textContent).toContain("4 entries");
    for (const secret of [...keys, "acc-secret-a", "prov-2-dec", "prov-3-oct"]) {
      expect(host.textContent).not.toContain(secret);
    }

    // Toggling the provisional checkbox loads all month entries via GET and re-enabling uses POST transactions-by-keys.
    if (provisionalToggle === null) throw new Error("missing provisional toggle");
    await click(provisionalToggle);
    expect(api.analyticsTransactions).toHaveBeenLastCalledWith("2026-11", { currency: "PLN", category: "", kind: "", query: "" });
    expect(visibleTexts()).toHaveLength(6);
    expect(host.querySelector(".budget-entries")?.textContent).toContain("6 entries");
    const toggledAgain = host.querySelector<HTMLInputElement>('input[name="provisional"]');
    if (toggledAgain === null) throw new Error("missing provisional toggle after uncheck");
    await click(toggledAgain);
    expect(api.analyticsTransactionsByKeys).toHaveBeenCalledTimes(2);
    expect(api.analyticsTransactionsByKeys).toHaveBeenLastCalledWith({ row_keys: keys, currency: "PLN" });
    expect(visibleTexts()).toHaveLength(4);

    // Returning to Monthly budget and opening the general View transactions link clears the provisional filter.
    await click(button("Monthly budget"));
    const freshLinks = [...host.querySelectorAll("button")].filter((item) => item.textContent === "View transactions");
    await click(freshLinks[0] as HTMLButtonElement);
    expect(host.querySelector<HTMLInputElement>('input[name="provisional"]')).toBeNull();
    expect(host.querySelectorAll(".budget-entries .budget-entry")).toHaveLength(6);
  });

  it("offers Forecast after Mortgages only while analytics is enabled", async () => {
    const api = fakeApi();
    await render(api);
    expect(host.textContent).not.toContain("Forecast");
    expect(api.forecast).not.toHaveBeenCalled();

    api.connections.mockImplementationOnce(() => ok({ connections: [connection], capabilities: analysis }));
    await click(button("Reload"));
    expect(button("Mortgages").nextElementSibling).toBe(button("Forecast"));
    expect(api.forecast).toHaveBeenCalledWith(2, "PLN");
    await click(button("Forecast"));
    expect(button("Forecast").getAttribute("aria-pressed")).toBe("true");
    expect(api.forecast).toHaveBeenCalledTimes(2);

    await click(button("Reload"));
    expect(host.textContent).not.toContain("Forecast");
    expect(host.textContent).toContain("Synthetic Current ••0001");
    expect(api.forecast).toHaveBeenCalledTimes(2);
  });

  it("opens a transaction's opaque account key in the rule editor", async () => {
    const api = fakeApi();
    const key = "d".repeat(64);
    api.connections.mockImplementation(() => ok({ connections: [connection], capabilities: analysis }));
    api.analyticsTransactions.mockImplementation((period) => ok(entriesOf(period, [
      entry("electricity", { account_id: "local-a", counterparty_account_key: key, counterparty_account: "PL** … 1234" }),
    ])));
    api.previewRule.mockImplementation(() => ok({ preview: {
      preview_id: "account-preview", affected_count: 1, expires_at: 2_000_000_000,
      examples: [{ account_id: "local-a", entry_reference: "electricity" }],
    } }));
    await render(api);
    await click(button("Month"));
    await click(button("Change"));
    await click(button("Create rule from recipient account"));
    expect(button("Categories and rules").getAttribute("aria-pressed")).toBe("true");
    const form = host.querySelector<HTMLFormElement>(".budget-rule-editor");
    expect(form?.querySelector<HTMLSelectElement>('select[name="field"]')?.value).toBe("counterparty_account");
    expect(form?.querySelector<HTMLSelectElement>('select[name="operator"]')?.value).toBe("equals");
    expect(form?.querySelector<HTMLInputElement>('input[name="value"]')?.value).toBe("PL** … 1234");
    await act(async () => { form?.requestSubmit(); await Promise.resolve(); });
    await settle();
    expect(api.previewRule).toHaveBeenCalledWith(expect.objectContaining({
      field: "counterparty_account", operator: "equals", value: "PL** … 1234", counterparty_account_key: key,
    }));
  });

  it("switches between accounts and the month view", async () => {
    const api = fakeApi();
    api.connections.mockImplementation(() => ok({ connections: [connection], capabilities: analysis }));
    await render(api);
    await click(button("Month"));
    expect(api.overview).toHaveBeenCalledTimes(1);
    expect(host.querySelector(".budget-total")?.textContent).toContain("40.50 PLN");
    expect(host.textContent).not.toContain("Synthetic Current ••0001");
    expect(button("Month").getAttribute("aria-pressed")).toBe("true");
    await click(button("Categories and rules"));
    expect(api.rules).toHaveBeenCalledTimes(1);
    expect(host.textContent).toContain("No categories yet.");
    api.recurring.mockImplementation(() => ok(recurringOf([recurringHint({ label: "Synthetic Fiber" })])));
    await click(button("Recurring"));
    expect(api.recurring).toHaveBeenCalledTimes(2);
    expect(host.textContent).toContain("Synthetic Fiber");
    expect(button("Recurring").getAttribute("aria-pressed")).toBe("true");
    api.spendingLimits.mockImplementation((period) => ok(spendingLimitsOf(
      period, [spendingLimit()], [spendingLimitStatus({ period, name: "Synthetic Food" })])));
    await click(button("Spending limits"));
    expect(api.spendingLimits).toHaveBeenCalledTimes(1);
    expect(host.textContent).toContain("Synthetic Food");
    expect(button("Spending limits").getAttribute("aria-pressed")).toBe("true");
    await click(button("Open Categories and rules"));
    expect(button("Categories and rules").getAttribute("aria-pressed")).toBe("true");
    api.mortgages.mockImplementation(() => ok(mortgagesOf([mortgageSchedule()], [mortgageReminder()])));
    await click(button("Mortgages"));
    expect(api.mortgages).toHaveBeenCalledTimes(2);
    expect(host.textContent).toContain("Synthetic Home Loan");
    expect(button("Mortgages").getAttribute("aria-pressed")).toBe("true");
    api.inbox.mockImplementation(() => ok(inboxOf([inboxConsentItem()])));
    await click(button("Inbox"));
    expect(api.inbox).toHaveBeenCalledTimes(1);
    expect(host.textContent).toContain("Synthetic Bank");
    expect(button("Inbox").getAttribute("aria-pressed")).toBe("true");
    await click(button("Accounts"));
    expect(host.textContent).toContain("Synthetic Current ••0001");
    expect(host.querySelector(".budget-month")).toBeNull();
  });

  it.each([[401, "budget_http_401"], [403, "budget_forbidden"]])(
    "clears the whole budget when the quarantine read reports lost access (%s)", async (status, error) => {
      const api = fakeApi();
      api.connections.mockImplementation(() => ok({ connections: [connection], capabilities: analysis }));
      api.quarantine.mockImplementation(() => fail(status, error));
      await render(api);
      expect(button("Monthly budget").getAttribute("aria-pressed")).toBe("true");
      await click(button("Quarantine"));
      expect(host.textContent).toContain("Only the budget's owner");
      expect(host.querySelector(".budget-views")).toBeNull();
      expect(host.querySelector(".budget-entries")).toBeNull();
      expect(host.textContent).not.toContain("Synthetic Current");
    });

  it.each([[401, "budget_http_401"], [403, "budget_forbidden"]])(
    "clears loaded monthly budget data and the whole budget when monthlyBudgetCharts reports lost access (%s)", async (status, error) => {
      const api = fakeApi();
      api.connections.mockImplementation(() => ok({ connections: [connection], capabilities: analysis }));
      api.monthlyBudgetCharts.mockImplementationOnce(() => ok(monthlyBudgetChartsOf({
        summary: { start_budget: "7777", spent: "111", remaining: "7666" },
      }))).mockImplementationOnce(() => fail(status, error));
      await render(api);
      expect(host.textContent).toContain("7,777.00 PLN");
      await click(button("Reload"));
      expect(host.textContent).toContain("Only the budget's owner");
      expect(host.textContent).not.toContain("7,777.00 PLN");
      expect(host.querySelector(".budget-views")).toBeNull();
    });

  it("clears the whole budget when the inbox read reports lost access", async () => {
    const api = fakeApi();
    api.connections.mockImplementation(() => ok({ connections: [connection], capabilities: analysis }));
    api.inbox.mockImplementation(() => fail(403, "budget_forbidden"));
    await render(api);
    await click(button("Inbox"));
    expect(host.textContent).toContain("Only the budget's owner");
    expect(host.querySelector(".budget-views")).toBeNull();
  });

  it("clears the whole budget when the mortgages read reports lost access", async () => {
    const api = fakeApi();
    api.connections.mockImplementation(() => ok({ connections: [connection], capabilities: analysis }));
    api.mortgages.mockImplementationOnce(() => ok(mortgagesOf()))
      .mockImplementation(() => fail(403, "budget_forbidden"));
    await render(api);
    await click(button("Mortgages"));
    expect(host.textContent).toContain("Only the budget's owner");
    expect(host.querySelector(".budget-views")).toBeNull();
  });

  it.each([[401, "budget_not_signed_in"], [403, "budget_forbidden"]])(
    "clears loaded forecast data and the whole budget when a forecast read returns %s", async (status, error) => {
      const api = fakeApi();
      const label = "Synthetic Forecast Commitment";
      const amount = "29.95";
      const total = forecastCurrencyTotal({ total: amount, scheduled_total: amount });
      const data = forecastOf();
      const months = data.months.map((month) => month.month === data.start_month ? {
        ...month, currencies: [total], items: [forecastItem({ label, amount })],
      } : month);
      api.connections.mockImplementation(() => ok({ connections: [connection], capabilities: analysis }));
      api.forecast.mockImplementation(() => ok({ ...data, totals: [total], months }));
      await render(api);
      await click(button("Forecast"));
      expect(host.textContent).toContain(label);
      expect(host.textContent).toContain(`${amount} PLN`);

      api.forecast.mockImplementationOnce(() => fail(status, error));
      await click(button("Reload"));
      expect(api.forecast).toHaveBeenCalledTimes(3);
      expect(host.textContent).toContain("Only the budget's owner");
      expect(host.textContent).not.toContain(label);
      expect(host.textContent).not.toContain(`${amount} PLN`);
      expect(host.querySelector(".budget-views")).toBeNull();
    },
  );

  it("clears the whole budget when the spending limits read reports lost access", async () => {
    const api = fakeApi();
    api.connections.mockImplementation(() => ok({ connections: [connection], capabilities: analysis }));
    api.spendingLimits.mockImplementation(() => fail(401, "budget_http_401"));
    await render(api);
    await click(button("Spending limits"));
    expect(host.textContent).toContain("Only the budget's owner");
    expect(host.querySelector(".budget-views")).toBeNull();
  });

  it("clears the whole budget when the recurring read reports lost access", async () => {
    const api = fakeApi();
    api.connections.mockImplementation(() => ok({ connections: [connection], capabilities: analysis }));
    api.recurring.mockImplementationOnce(() => ok(recurringOf()))
      .mockImplementation(() => fail(403, "budget_forbidden"));
    await render(api);
    await click(button("Recurring"));
    expect(host.textContent).toContain("Only the budget's owner");
    expect(host.querySelector(".budget-views")).toBeNull();
  });

  it("clears everything when the month read reports lost access, and reopens on accounts", async () => {
    const api = fakeApi();
    api.connections.mockImplementation(() => ok({ connections: [connection], capabilities: analysis }));
    await render(api);
    api.overview.mockImplementation(() => fail(401, "budget_http_401"));
    await click(button("Month"));
    expect(host.textContent).toContain("Only the budget's owner");
    expect(host.textContent).not.toContain("PLN");
    api.overview.mockImplementation((period) => ok(overviewOf(period)));
    await click(button("Reload"));
    expect(host.textContent).toContain("Synthetic Current ••0001");
    expect(host.querySelector(".budget-month")).toBeNull();
  });

  it("opens a month chosen in the year view", async () => {
    const api = fakeApi();
    api.connections.mockImplementation(() => ok({ connections: [connection], capabilities: analysis }));
    api.overview.mockImplementation((period) => ok(period.length === 4
      ? overviewOf(period, { months: [{ month: `${period}-01`, currencies: overviewOf(period).currencies }] })
      : overviewOf(period)));
    await render(api);
    await click(button("Year"));
    const year = String(new Date().getFullYear());
    expect(api.overview).toHaveBeenLastCalledWith(year);
    const open = host.querySelector<HTMLButtonElement>(".budget-year button");
    if (open === null) throw new Error("month link missing");
    await click(open);
    expect(api.overview).toHaveBeenLastCalledWith(`${year}-01`);
    expect(button("Month").getAttribute("aria-pressed")).toBe("true");
  });

  it("falls back to accounts when a reload withdraws the capability", async () => {
    const api = fakeApi();
    api.connections.mockImplementation(() => ok({ connections: [connection], capabilities: analysis }));
    await render(api);
    await click(button("Month"));
    api.connections.mockImplementation(() => ok({ connections: [connection], capabilities }));
    await click(button("Reload"));
    expect(host.querySelector(".budget-views")).toBeNull();
    expect(host.textContent).toContain("Synthetic Current ••0001");
  });
});

describe("receipts tab & capability gate", () => {
  const withAnalytics = { ...capabilities, analytics: true };

  function fakeReceiptsApi(): ReceiptsApi {
    return {
      receiptsList: vi.fn<ReceiptsApi["receiptsList"]>(() => ok({
        receipts: [{
          receipt_id: "11111111-1111-4111-8111-111111111111",
          status: "needs_review",
          document_kind: "receipt",
          source_kind: "local_ocr",
          receipt_kind: "purchase",
          receipt_date: "2026-11-14",
          receipt_time: null,
          raw_shop_name: "Tab Integration Market",
          raw_nip: null,
          shop_id: null,
          chain_key: null,
          total_amount: "19.50",
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
        }],
        next_cursor: null,
      })),
      receiptsSearch: vi.fn<ReceiptsApi["receiptsSearch"]>(() => ok({ receipts: [], next_cursor: null })),
      receiptDetail: vi.fn<ReceiptsApi["receiptDetail"]>(() => ok({
        receipt: {
          receipt_id: "11111111-1111-4111-8111-111111111111",
          status: "needs_review",
          document_kind: "receipt",
          source_kind: "local_ocr",
          receipt_kind: "purchase",
          receipt_date: "2026-11-14",
          receipt_time: null,
          raw_shop_name: "Tab Integration Market",
          raw_nip: null,
          shop_id: null,
          chain_key: null,
          total_amount: "19.50",
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
          lines: [{
            line_no: 1,
            line_kind: "item",
            raw_name: "Synthetic Grocery",
            raw_name_key: "synthetic grocery",
            ptu_code: "A",
            quantity: "1",
            raw_unit: "szt",
            unit_price_raw: "19.50",
            gross_line_total: "19.50",
            discount_amount: "0",
            net_line_total: "19.50",
          }],
        },
        candidates: [],
      })),
      receiptImage: vi.fn<ReceiptsApi["receiptImage"]>(),
      confirmReceipt: vi.fn<ReceiptsApi["confirmReceipt"]>(),
      deleteReceipt: vi.fn<ReceiptsApi["deleteReceipt"]>(),
    };
  }

  async function renderWithReceipts(
    api: ReturnType<typeof fakeApi>,
    rApi: ReceiptsApi,
    principal?: string | null,
  ): Promise<void> {
    await act(async () => {
      root.render(principal === undefined
        ? <Budget active api={api} receiptsApi={rApi} pollMs={5} />
        : <Budget active api={api} receiptsApi={rApi} pollMs={5} principal={principal} />);
      await Promise.resolve();
    });
    await settle();
  }

  it("gates Receipts independently of analytics, hiding only on stored_reads=false or receipts=false", async () => {
    const api = fakeApi();
    const rApi = fakeReceiptsApi();

    // 1. analytics=false, stored_reads=true -> analytics tabs remain absent, but Receipts header toggle works.
    api.connections.mockImplementation(() => ok({ connections: [connection], capabilities }));
    await renderWithReceipts(api, rApi);
    expect(host.querySelector(".budget-views")).toBeNull();
    expect([...host.querySelectorAll("button")].some((item) => item.textContent === "Receipts and documents")).toBe(true);
    await click(button("Receipts and documents"));
    expect(rApi.receiptsList).toHaveBeenCalledWith({ limit: 50 });
    expect(host.textContent).toContain("Tab Integration Market");

    // 2. receipts=false (or stored_reads=false) hides Receipts even when analytics=true.
    api.connections.mockImplementation(() => ok({
      connections: [connection],
      capabilities: { ...withAnalytics, receipts: false } as never,
    }));
    await click(button("Reload"));
    expect([...host.querySelectorAll("button")].some((item) => item.textContent === "Receipts and documents")).toBe(false);
    expect(host.textContent).not.toContain("Tab Integration Market");

    api.connections.mockImplementation(() => ok({ connections: [connection], capabilities: { ...withAnalytics, stored_reads: false } }));
    await click(button("Reload"));
    expect([...host.querySelectorAll("button")].some((item) => item.textContent === "Receipts and documents")).toBe(false);

    // 3. own_budget.can_use_receipts=false hides Receipts even when analytics=true and stored_reads=true.
    api.connections.mockImplementation(() => ok({ connections: [connection], capabilities: withAnalytics }));
    api.capabilities.mockImplementation(() => ok({
      ...authorization,
      own_budget: { ...authorization.own_budget, can_use_receipts: false },
    }));
    await click(button("Reload"));
    expect(host.querySelector(".budget-views")).not.toBeNull();
    expect([...host.querySelectorAll("button")].some((item) => item.textContent === "Receipts and documents")).toBe(false);

    // 4. analytics=true, stored_reads=true, can_use_receipts=true restores Receipts inside .budget-views.
    api.capabilities.mockImplementation(() => ok(authorization));
    await click(button("Reload"));
    expect(host.querySelector(".budget-views")).not.toBeNull();
    await click(button("Receipts and documents"));
    expect(host.textContent).toContain("Tab Integration Market");
  });

  it("keeps Budget intact on 404 disabled, but clears all Budget on unmounted 401/403 from a receipt read", async () => {
    const api = fakeApi();
    const rApi = fakeReceiptsApi();
    api.connections.mockImplementation(() => ok({ connections: [connection], capabilities: withAnalytics }));
    vi.mocked(rApi.receiptsList).mockImplementationOnce(() => fail(404, "budget_receipts_disabled"));

    await renderWithReceipts(api, rApi);
    await click(button("Receipts and documents"));
    expect(host.textContent).toContain("Receipts are disabled on this server");
    expect(host.querySelector(".budget-views")).not.toBeNull();

    // Start slow receipt read, switch to Accounts before it returns 401, and verify barrier still clears Budget.
    const slowRead = deferred<Loaded<ReceiptsApi["receiptsList"]>>();
    vi.mocked(rApi.receiptsList).mockImplementationOnce(() => slowRead.promise);
    await click(button("Accounts"));
    await click(button("Receipts and documents"));
    await click(button("Accounts"));
    expect(host.textContent).toContain("Synthetic Current");

    await act(async () => {
      slowRead.settle({ ok: false, status: 401, error: "budget_http_401" });
      await Promise.resolve();
    });
    await settle();
    expect(host.textContent).toContain("Only the budget's owner");
    expect(host.textContent).not.toContain("Synthetic Current");
    expect(host.querySelector(".budget-views")).toBeNull();
  });

  it("hides only Receipts and refreshes capabilities on receipt 403 while keeping ledger intact for budget.view, and restores when capabilities recover", async () => {
    const api = fakeApi();
    const rApi = fakeReceiptsApi();
    api.connections.mockImplementation(() => ok({ connections: [connection], capabilities: withAnalytics }));
    await renderWithReceipts(api, rApi, "owner");
    await click(button("Receipts and documents"));
    expect(host.textContent).toContain("Tab Integration Market");

    // 1. Listing 403 when capability refresh now revokes can_use_receipts hides only Receipts; ledger stays intact.
    await click(button("Accounts"));
    const listCallsBefore403 = vi.mocked(rApi.receiptsList).mock.calls.length;
    vi.mocked(rApi.receiptsList).mockImplementationOnce(() => fail(403, "budget_forbidden"));
    api.capabilities
      .mockImplementationOnce(() => ok(authorization))
      .mockImplementation(() => ok({
        ...authorization,
        own_budget: { ...authorization.own_budget, can_use_receipts: false },
      }));
    await click(button("Receipts and documents"));
    expect(host.textContent).toContain("Synthetic Current");
    expect(host.textContent).not.toContain("Tab Integration Market");
    expect(host.textContent).not.toContain("Only the budget's owner");
    expect([...host.querySelectorAll("button")].some((item) => item.textContent === "Receipts and documents")).toBe(false);
    expect(vi.mocked(rApi.receiptsList).mock.calls).toHaveLength(listCallsBefore403 + 1);

    // 2. Subsequent capability refresh on focus restores Receipts based on new capabilities.
    api.capabilities.mockImplementation(() => ok(authorization));
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
      await Promise.resolve();
    });
    await settle();
    expect([...host.querySelectorAll("button")].some((item) => item.textContent === "Receipts and documents")).toBe(true);
    expect(host.textContent).toContain("Tab Integration Market");

    // 3. Transient listing 403 when capabilities still report can_use_receipts=true marks receipts unavailable without looping,
    //    then Reload (confirming can_use_receipts=true even with unchanged capabilities) clears receiptUnavailable & remounts panel.
    await click(button("Accounts"));
    const callsBeforeForbiddenLoop = vi.mocked(rApi.receiptsList).mock.calls.length;
    vi.mocked(rApi.receiptsList).mockImplementationOnce(() => fail(403, "budget_origin_required"));
    await click(button("Receipts and documents"));
    await settle(20);
    expect(vi.mocked(rApi.receiptsList).mock.calls).toHaveLength(callsBeforeForbiddenLoop + 1);
    expect(host.textContent).toContain("Synthetic Current");
    expect(host.textContent).not.toContain("Tab Integration Market");
    expect([...host.querySelectorAll("button")].some((item) => item.textContent === "Receipts and documents")).toBe(false);

    // Reload with unchanged can_use_receipts=true clears receiptUnavailable and remounts Receipts panel immediately.
    await click(button("Reload"));
    expect([...host.querySelectorAll("button")].some((item) => item.textContent === "Receipts and documents")).toBe(true);
    expect(host.textContent).toContain("Tab Integration Market");

    // Search 403 is recovered by focus capability refresh with unchanged can_use_receipts=true too.
    const setSearchInput = (text: string) => {
      const searchQuery = host.querySelector<HTMLInputElement>('input[name="query"]');
      if (searchQuery === null) throw new Error("receipt search input missing");
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(searchQuery, text);
      searchQuery.dispatchEvent(new Event("input", { bubbles: true }));
    };
    vi.mocked(rApi.receiptsSearch).mockImplementationOnce(() => fail(403, "budget_forbidden"));
    await act(async () => {
      setSearchInput("Tab");
      await Promise.resolve();
    });
    await click(button("Apply filters"));
    expect(rApi.receiptsSearch).toHaveBeenCalledTimes(1);
    expect(host.textContent).not.toContain("Tab Integration Market");
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
      await Promise.resolve();
    });
    await settle();
    expect(host.textContent).toContain("Tab Integration Market");

    // 4. Delayed 403 from an earlier receipt panel epoch is ignored after the panel recovers.
    const delayedOldRead = deferred<Loaded<ReceiptsApi["receiptDetail"]>>();
    vi.mocked(rApi.receiptDetail).mockImplementationOnce(() => delayedOldRead.promise);
    await click(button("Details"));
    // Trigger search 403 -> focus recovery while delayedOldRead is still in flight.
    vi.mocked(rApi.receiptsSearch).mockImplementationOnce(() => fail(403, "budget_forbidden"));
    await act(async () => {
      setSearchInput("Tab");
      await Promise.resolve();
    });
    await click(button("Apply filters"));
    expect(rApi.receiptsSearch).toHaveBeenCalledTimes(2);
    expect(host.textContent).not.toContain("Tab Integration Market");
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
      await Promise.resolve();
    });
    await settle();
    expect(host.textContent).toContain("Tab Integration Market");
    await act(async () => {
      delayedOldRead.settle({ ok: false, status: 403, error: "budget_forbidden" });
      await Promise.resolve();
    });
    await settle();
    expect(host.textContent).toContain("Tab Integration Market");
    expect([...host.querySelectorAll("button")].some((item) => item.textContent === "Receipts and documents")).toBe(true);

    // 5. Non-listing receipt 403 (confirm) keeps Receipts accessible if refreshed capabilities still allow can_use_receipts=true,
    //    and clears the entire ledger if refreshed capabilities now deny can_view / return 401.
    await click(button("Details"));
    vi.mocked(rApi.confirmReceipt).mockImplementationOnce(() => fail(403, "budget_forbidden"));
    await click(button("Confirm receipt"));
    expect(host.textContent).toContain("Tab Integration Market");
    expect(host.textContent).not.toContain("Only the budget's owner");

    vi.mocked(rApi.confirmReceipt).mockImplementationOnce(() => fail(403, "budget_forbidden"));
    api.capabilities.mockImplementationOnce(() => fail(401, "budget_http_401"));
    await click(button("Details"));
    await click(button("Confirm receipt"));
    expect(host.textContent).toContain("Only the budget's owner");
    expect(host.querySelector(".budget-views")).toBeNull();
  });

  it("unmounts previous actor drawer on principal switch, re-authorizes view opening via capabilities, and clears receipt state on focus/visibilitychange", async () => {
    const api = fakeApi();
    const rApi = fakeReceiptsApi();
    api.capabilities.mockImplementation(() => ok({ ...authorization, user: "alice" }));
    api.connections.mockImplementation(() => ok({ connections: [connection], capabilities: withAnalytics }));

    await renderWithReceipts(api, rApi, "alice");
    const capCallsBeforeOpen = api.capabilities.mock.calls.length;
    await click(button("Receipts and documents"));
    expect(api.capabilities.mock.calls.length).toBeGreaterThan(capCallsBeforeOpen);
    await click(button("Details"));
    expect(host.querySelector('[role="dialog"]')).not.toBeNull();

    // Switching principal prop unmounts alice's drawer and resets cleanly to bob before issuing receipt requests.
    const listCallsBeforeBob = vi.mocked(rApi.receiptsList).mock.calls.length;
    api.capabilities.mockImplementation(() => ok({ ...authorization, user: "bob" }));
    await renderWithReceipts(api, rApi, "bob");
    expect(host.querySelector('[role="dialog"]')).toBeNull();
    expect(host.textContent).toContain("Synthetic Current");
    expect(vi.mocked(rApi.receiptsList).mock.calls).toHaveLength(listCallsBeforeBob);

    // If authorizeView sees revoked can_use_receipts on clicking Receipts, Receipts never opens or fetches.
    api.capabilities.mockImplementationOnce(() => ok({
      ...authorization,
      user: "bob",
      own_budget: { ...authorization.own_budget, can_use_receipts: false },
    }));
    await click(button("Receipts and documents"));
    expect(vi.mocked(rApi.receiptsList).mock.calls).toHaveLength(listCallsBeforeBob);
    expect([...host.querySelectorAll("button")].some((item) => item.textContent === "Receipts and documents")).toBe(false);

    // Restore capability; if authorizeView sees actor change on opening Receipts, Budget clears and never fetches receipts.
    api.capabilities.mockImplementation(() => ok({ ...authorization, user: "owner" }));
    await renderWithReceipts(api, rApi, "owner");
    const listCallsBeforeDenied = vi.mocked(rApi.receiptsList).mock.calls.length;
    api.capabilities.mockImplementationOnce(() => fail(409, "budget_actor_changed"));
    await click(button("Receipts and documents"));
    expect(vi.mocked(rApi.receiptsList).mock.calls).toHaveLength(listCallsBeforeDenied);
    expect(host.textContent).toContain("Only the budget's owner");
    expect(host.querySelector(".budget-views")).toBeNull();

    // Focus and visibilitychange revalidate capabilities and clear receipt state at once if actor changed.
    api.capabilities.mockImplementation(() => ok({ ...authorization, user: "owner" }));
    await click(button("Reload"));
    vi.mocked(rApi.receiptsList).mockImplementationOnce(() => ok({
      receipts: [{
        receipt_id: "33333333-3333-4333-8333-333333333333",
        status: "needs_review",
        document_kind: "receipt",
        source_kind: "local_ocr",
        receipt_kind: "purchase",
        receipt_date: "2026-11-14",
        receipt_time: null,
        raw_shop_name: "Focus Check Store",
        raw_nip: null,
        shop_id: null,
        chain_key: null,
        total_amount: "11.00",
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
      }],
      next_cursor: "owner-cur-focus",
    }));
    await click(button("Receipts and documents"));
    expect(host.textContent).toContain("Focus Check Store");

    api.capabilities.mockImplementationOnce(() => fail(409, "budget_actor_changed"));
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
      await Promise.resolve();
    });
    await settle();
    expect(host.textContent).not.toContain("Focus Check Store");
    expect(host.textContent).toContain("Only the budget's owner");

    // visibilitychange clears when becoming visible with denied actor, ignores hidden transitions.
    api.capabilities.mockImplementation(() => ok({ ...authorization, user: "owner" }));
    await click(button("Reload"));
    await click(button("Receipts and documents"));
    const capCallsBeforeHidden = api.capabilities.mock.calls.length;
    const visSpy = vi.spyOn(document, "visibilityState", "get");
    try {
      visSpy.mockReturnValue("hidden");
      await act(async () => {
        document.dispatchEvent(new Event("visibilitychange"));
        await Promise.resolve();
      });
      expect(api.capabilities.mock.calls).toHaveLength(capCallsBeforeHidden);

      visSpy.mockReturnValue("visible");
      api.capabilities.mockImplementationOnce(() => fail(401, "budget_http_401"));
      await act(async () => {
        document.dispatchEvent(new Event("visibilitychange"));
        await Promise.resolve();
      });
      await settle();
      expect(host.textContent).toContain("Only the budget's owner");
      expect(host.querySelector(".budget-views")).toBeNull();
    } finally {
      visSpy.mockRestore();
    }
  });

  it("keeps receipt mutation locks across Accounts <-> Receipts switches until settled", async () => {
    const api = fakeApi();
    const rApi = fakeReceiptsApi();
    api.connections.mockImplementation(() => ok({ connections: [connection], capabilities: withAnalytics }));
    const pendingConfirm = deferred<Loaded<ReceiptsApi["confirmReceipt"]>>();
    vi.mocked(rApi.confirmReceipt).mockImplementationOnce(() => pendingConfirm.promise);

    await renderWithReceipts(api, rApi);
    await click(button("Receipts and documents"));
    await click(button("Details"));
    await click(button("Confirm receipt"));
    expect(rApi.confirmReceipt).toHaveBeenCalledTimes(1);

    // Leave Receipts to Accounts, then return -> lock survives unmount and still blocks re-submit.
    await click(button("Accounts"));
    await click(button("Receipts and documents"));
    expect(host.textContent).toContain("Confirming…");
    await click(button("Details"));
    expect(button("Confirming…").disabled).toBe(true);
    const form = host.querySelector<HTMLFormElement>("form.budget-receipt-confirm");
    await act(async () => {
      form?.requestSubmit();
      await Promise.resolve();
    });
    expect(rApi.confirmReceipt).toHaveBeenCalledTimes(1);

    await act(async () => {
      pendingConfirm.settle({
        ok: true,
        data: {
          ok: true,
          receipt: {
            receipt_id: "11111111-1111-4111-8111-111111111111",
            status: "confirmed",
            document_kind: "receipt",
            source_kind: "local_ocr",
            receipt_kind: "purchase",
            receipt_date: "2026-11-14",
            receipt_time: null,
            raw_shop_name: "Tab Integration Market",
            raw_nip: null,
            shop_id: null,
            chain_key: null,
            total_amount: "19.50",
            currency: "PLN",
            payment_method: "card",
            math_delta: "0",
            linked: false,
            linked_account_id: null,
            linked_entry_ref: null,
            version: 2,
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
            lines: [],
          },
        },
      });
      await Promise.resolve();
    });
    await settle();
    expect(button("Confirm receipt").disabled).toBe(false);
  });

  it("aborts in-flight bulk confirm on principal switch, authorization lock, or actor change", async () => {
    const api = fakeApi();
    const rApi = fakeReceiptsApi();
    api.capabilities.mockImplementation(() => ok({ ...authorization, user: "alice" }));
    api.connections.mockImplementation(() => ok({ connections: [connection], capabilities: withAnalytics }));

    const item1 = {
      receipt_id: "bbbbbbbb-1111-4111-8111-111111111111",
      status: "imported" as const,
      document_kind: "payment_confirmation" as const,
      source_kind: "email" as const,
      receipt_kind: "purchase" as const,
      receipt_date: "2026-11-14",
      receipt_time: null,
      raw_shop_name: "Bulk Merchant",
      raw_nip: null,
      shop_id: null,
      chain_key: null,
      total_amount: "19.50",
      currency: "PLN",
      payment_method: null,
      math_delta: null,
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
    };
    const item2 = { ...item1, receipt_id: "bbbbbbbb-2222-4222-8222-222222222222", version: 2 };
    vi.mocked(rApi.receiptsList).mockImplementation(() => ok({ receipts: [item1, item2], next_cursor: null }));
    vi.mocked(rApi.receiptDetail).mockImplementation((id) => ok({
      receipt: {
        ...(id === item1.receipt_id ? item1 : item2),
        lines: [{
          line_no: 1,
          line_kind: "other",
          raw_name: "Payment",
          raw_name_key: "payment",
          ptu_code: null,
          quantity: "1",
          raw_unit: null,
          unit_price_raw: null,
          gross_line_total: "19.50",
          discount_amount: "0",
          net_line_total: "19.50",
        }],
      },
      candidates: [],
    }));
    const pendingFirstConfirm = deferred<Loaded<ReceiptsApi["confirmReceipt"]>>();
    vi.mocked(rApi.confirmReceipt).mockImplementationOnce(() => pendingFirstConfirm.promise);

    await renderWithReceipts(api, rApi, "alice");
    await click(button("Receipts and documents"));
    await click(button("Confirm all from: Bulk Merchant"));
    await act(async () => {
      button("Start bulk confirm").click();
      await Promise.resolve();
    });
    await settle();
    expect(rApi.confirmReceipt).toHaveBeenCalledTimes(1);

    // Switching principal unmounts alice's ReceiptsView, aborts item2, and settles cleanly under bob.
    api.capabilities.mockImplementation(() => ok({ ...authorization, user: "bob" }));
    await renderWithReceipts(api, rApi, "bob");
    await act(async () => {
      pendingFirstConfirm.settle({
        ok: true,
        data: { ok: true, receipt: { ...item1, status: "confirmed", version: 2, lines: [] } },
      });
      await Promise.resolve();
    });
    await settle();
    expect(rApi.confirmReceipt).toHaveBeenCalledTimes(1);
    expect(host.textContent).not.toContain("Confirming");

    // Locking authorization via focus capability failure before item 2 stops bulk confirm before item 2 detail/confirm.
    vi.mocked(rApi.receiptDetail).mockClear();
    vi.mocked(rApi.confirmReceipt).mockClear();
    const lockFirstConfirm = deferred<Loaded<ReceiptsApi["confirmReceipt"]>>();
    vi.mocked(rApi.confirmReceipt).mockImplementationOnce(() => lockFirstConfirm.promise);
    await click(button("Receipts and documents"));
    await click(button("Confirm all from: Bulk Merchant"));
    await act(async () => {
      button("Start bulk confirm").click();
      await Promise.resolve();
    });
    await settle();
    expect(rApi.receiptDetail).toHaveBeenCalledTimes(1);
    expect(rApi.confirmReceipt).toHaveBeenCalledTimes(1);

    api.capabilities.mockImplementationOnce(() => fail(503, "budget_unavailable"));
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
      await Promise.resolve();
    });
    await settle();
    expect(api.isAuthorizationLocked()).toBe(true);
    await act(async () => {
      lockFirstConfirm.settle({
        ok: true,
        data: { ok: true, receipt: { ...item1, status: "confirmed", version: 2, lines: [] } },
      });
      await Promise.resolve();
    });
    await settle();
    expect(rApi.receiptDetail).toHaveBeenCalledTimes(1);
    expect(rApi.confirmReceipt).toHaveBeenCalledTimes(1);
    expect(host.textContent).toContain("Bulk confirm cancelled after 1 confirmed (1 remaining, 0 failed).");

    // Actor-changed error on bulk confirm clears Budget immediately and never sends item 2.
    api.capabilities.mockImplementation(() => ok({ ...authorization, user: "bob" }));
    await click(button("Reload"));
    await click(button("Receipts and documents"));
    vi.mocked(rApi.receiptDetail).mockClear();
    vi.mocked(rApi.confirmReceipt).mockClear();
    vi.mocked(rApi.confirmReceipt).mockImplementationOnce(() => fail(409, "budget_actor_changed"));
    await click(button("Confirm all from: Bulk Merchant"));
    await click(button("Start bulk confirm"));
    expect(rApi.receiptDetail).toHaveBeenCalledTimes(1);
    expect(rApi.confirmReceipt).toHaveBeenCalledTimes(1);
    expect(host.textContent).toContain("Only the budget's owner");
    expect(host.querySelector(".budget-views")).toBeNull();
  });
});
