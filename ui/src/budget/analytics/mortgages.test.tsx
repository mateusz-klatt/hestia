// Synthetic data only: no real mortgage, account, target or bank values.
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { loadLocale } from "../../i18n";
import type { BudgetApi, BudgetResult } from "../api";
import { account } from "../fixtures";
import { mortgagePaymentSaved, mortgageReminder, mortgageSaved, mortgageSchedule, mortgagesOf } from "./fixtures";
import type { AnalyticsMortgages } from "./model";
import { MortgagesView } from "./mortgages";

type Settle<T> = (value: BudgetResult<T>) => void;

function ok<T>(data: T): Promise<BudgetResult<T>> { return Promise.resolve({ ok: true, data }); }
function fail<T>(status: number, error: string): Promise<BudgetResult<T>> { return Promise.resolve({ ok: false, status, error }); }
function deferred<T>(): { promise: Promise<BudgetResult<T>>; settle: Settle<T> } {
  let settle: Settle<T> = () => undefined;
  const promise = new Promise<BudgetResult<T>>((resolve) => { settle = resolve; });
  return { promise, settle };
}

const accounts = [account({
  id: "acc-1",
  display: { product: "Synthetic Main", currency: "PLN", masked_suffix: "0001", source: "offline_import", observed_at: 1, imported_at: 2 },
})];

function fakeApi() {
  return {
    mortgages: vi.fn<BudgetApi["mortgages"]>(() => ok(mortgagesOf(
      [mortgageSchedule({ id: "mtg-1", version: 2, paid_months: ["2026-10"] })],
      [mortgageReminder({ schedule_id: "mtg-1", due_date: "2026-11-10", days_until_due: 3, status: "due_soon" })],
    ))),
    mortgageReferenceCandidates: vi.fn<BudgetApi["mortgageReferenceCandidates"]>(() => ok({ candidates: [] })),
    saveMortgage: vi.fn<BudgetApi["saveMortgage"]>(() => ok({ schedule: mortgageSaved({ version: 3 }) })),
    setMortgagePayment: vi.fn<BudgetApi["setMortgagePayment"]>(() => ok({ payment: mortgagePaymentSaved({ version: 3 }) })),
  } satisfies Pick<BudgetApi, "mortgages" | "mortgageReferenceCandidates" | "saveMortgage" | "setMortgagePayment">;
}

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
async function click(element: HTMLElement): Promise<void> {
  await act(async () => { element.click(); await Promise.resolve(); });
  await settle();
}
function button(label: string, scope: ParentNode = host): HTMLButtonElement {
  const found = [...scope.querySelectorAll("button")].find((item) => item.textContent === label);
  if (found === undefined) throw new Error(`button ${label} missing`);
  return found;
}
async function type(input: HTMLInputElement | null | undefined, value: string): Promise<void> {
  await act(async () => {
    if (input === null || input === undefined) throw new Error("input missing");
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    await Promise.resolve();
  });
}
async function choose(select: HTMLSelectElement | null | undefined, value: string): Promise<void> {
  await act(async () => {
    if (select === null || select === undefined) throw new Error("select missing");
    select.value = value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    await Promise.resolve();
  });
}

describe("MortgagesView", () => {
  it("renders server UTC evaluation date, exact decimals, freshness warning and EN/PL copy without storage", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const exact = "12345678901234567890.05";
    const api = fakeApi();
    api.mortgages.mockImplementation(() => ok(mortgagesOf(
      [
        mortgageSchedule({ id: "mtg-a", label: "Synthetic Overdue", amount: exact, remind_days_before: 1 }),
        mortgageSchedule({ id: "mtg-b", label: "Synthetic Done", amount: "100", enabled: true, end_date: "2026-10-31", remind_days_before: 0 }),
      ],
      [
        mortgageReminder({ schedule_id: "mtg-a", label: "Synthetic Overdue", amount: exact, due_date: "2026-11-06",
          days_until_due: -1, status: "overdue", freshness_stale: true }),
        mortgageReminder({ schedule_id: "mtg-b", label: "Synthetic Done", amount: "100", due_date: null,
          days_until_due: null, status: "completed", reminder_active: false, destination_kind: "owner_push" }),
      ],
      { evaluation_date: "2026-11-07", ledger_as_of: null },
    )));

    await act(async () => {
      root.render(<MortgagesView api={api} accounts={accounts}
        locale="en-US" revision={1} onAccessLoss={() => false} />);
      await Promise.resolve();
    });
    await settle();

    expect(host.textContent).toContain("Evaluation only");
    expect(host.textContent).toContain("Evaluated for 2026-11-07 (UTC)");
    expect(host.textContent).toContain("No synced ledger data yet");
    expect(host.textContent).toContain("12,345,678,901,234,567,890.05 PLN");
    expect(host.textContent).toContain("1 day overdue");
    expect(host.textContent).toContain("Ledger data may be outdated");
    expect(host.textContent).toContain("All scheduled installments marked paid");
    expect(host.textContent).toContain("Owner push");
    expect(setItem).not.toHaveBeenCalled();
    expect(window.location.search).toBe("");

    await loadLocale("pl");
    await act(async () => {
      root.render(<MortgagesView api={api} accounts={accounts}
        locale="pl-PL" revision={2} onAccessLoss={() => false} />);
      await Promise.resolve();
    });
    await settle();

    expect(host.textContent).toContain("Tylko ocena");
    expect(host.textContent).toContain("Stan na 2026-11-07 (UTC)");
    expect(host.textContent).toContain("1 dzień po terminie");
    expect(host.textContent).toContain("Przypomnienie 1 dzień wcześniej");
    expect(host.textContent).toContain("Przypomnienie w dniu terminu");
    expect(host.textContent).toContain("Push właściciela");
    expect(host.textContent).toContain("Zakończona");
  });

  it("creates, edits, toggles and marks/unmarks payments with strict CAS, busy lock and 409/413/400 handling", async () => {
    const api = fakeApi();
    await act(async () => {
      root.render(<MortgagesView api={api} accounts={accounts}
        locale="en-US" revision={1} onAccessLoss={() => false} />);
      await Promise.resolve();
    });
    await settle();

    // Create new schedule -> schedule_id: null, expected_version: 0, no destination_target field.
    await click(button("New schedule"));
    const form = host.querySelector("form");
    if (form === null) throw new Error("create form missing");
    expect(form.querySelector("fieldset")).toBeNull();
    expect(api.mortgageReferenceCandidates).not.toHaveBeenCalled();
    expect(button("Save schedule", form).disabled).toBe(true);
    await type(form.querySelector('input[name="mortgage_label"]'), "  Synthetic Flat  ");
    await type(form.querySelector('input[name="mortgage_amount"]'), "3100.25");
    await type(form.querySelector('input[name="mortgage_currency"]'), "eur");
    await type(form.querySelector('input[name="mortgage_due_day"]'), "15");
    await type(form.querySelector('input[name="mortgage_start_date"]'), "2026-02-01");
    await type(form.querySelector('input[name="mortgage_end_date"]'), "2036-02-01");
    await type(form.querySelector('input[name="mortgage_remind_days"]'), "7");
    await choose(form.querySelector('select[name="mortgage_destination"]'), "owner_email");

    const inflight = deferred<{ schedule: ReturnType<typeof mortgageSaved> }>();
    api.saveMortgage.mockImplementationOnce(() => inflight.promise);
    await act(async () => { form.requestSubmit(); await Promise.resolve(); });
    expect(button("Save schedule", form).disabled).toBe(true);
    expect(button("Disable").disabled).toBe(true);
    const payload = api.saveMortgage.mock.calls[0]?.[0];
    expect(payload).toEqual({
      schedule_id: null, label: "Synthetic Flat", amount: "3100.25", currency: "EUR", due_day: 15,
      start_date: "2026-02-01", end_date: "2036-02-01", remind_days_before: 7, destination_kind: "owner_email",
      enabled: true, expected_version: 0,
    });
    expect("destination_target" in (payload ?? {})).toBe(false);
    await act(async () => { inflight.settle({ ok: true, data: { schedule: mortgageSaved() } }); await Promise.resolve(); });
    await settle();
    expect(host.querySelector('form input[name="mortgage_label"]')).toBeNull();
    expect(api.mortgages).toHaveBeenCalledTimes(2);

    // Edit existing schedule -> expected_version: 2; 409 reloads and closes editor.
    await click(button("Edit"));
    const editForm = host.querySelector("form");
    if (editForm === null) throw new Error("edit form missing");
    api.saveMortgage.mockImplementationOnce(() => fail(409, "budget_analytics_conflict"));
    await act(async () => { editForm.requestSubmit(); await Promise.resolve(); });
    await settle();
    expect(api.saveMortgage).toHaveBeenLastCalledWith(expect.objectContaining({
      schedule_id: "mtg-1", expected_version: 2,
    }));
    expect(host.textContent).toContain("changed elsewhere");
    expect(host.querySelector('input[name="mortgage_label"]')).toBeNull();
    expect(api.mortgages).toHaveBeenCalledTimes(3);

    // Quick mark paid from reminder -> sends month 2026-11, paid: true, expected_version: 2.
    await click(button("Mark 2026-11 paid"));
    expect(api.setMortgagePayment).toHaveBeenCalledWith({
      schedule_id: "mtg-1", month: "2026-11", paid: true, expected_version: 2,
    });

    // Unmark paid month from schedule list; 413 displays cap error.
    api.setMortgagePayment.mockImplementationOnce(() => fail(413, "budget_analytics_limit"));
    await click(button("Unmark"));
    expect(api.setMortgagePayment).toHaveBeenLastCalledWith({
      schedule_id: "mtg-1", month: "2026-10", paid: false, expected_version: 2,
    });
    expect(host.textContent).toContain("Maximum number of mortgage schedules or paid months reached.");

    // Manual month form -> 400 invalid request displays clear message.
    api.setMortgagePayment.mockImplementationOnce(() => fail(400, "budget_analytics_invalid_request"));
    const monthInput = host.querySelector<HTMLInputElement>('input[name="paid_month"]');
    await type(monthInput, "2025-12");
    await click(button("Mark paid"));
    expect(host.textContent).toContain("Check the date range or installment month");

    // Disable toggle sends enabled: false with expected_version: 2.
    await click(button("Disable"));
    expect(api.saveMortgage).toHaveBeenLastCalledWith(expect.objectContaining({
      schedule_id: "mtg-1", enabled: false, expected_version: 2,
    }));
  });

  it("links two masked bank payments and removes a saved reference absent from current candidates", async () => {
    await loadLocale("pl");
    const api = fakeApi();
    const stored = { hash: "a".repeat(64), last4: "Z001" };
    const installment = { hash: "b".repeat(64), last4: "Z002" };
    const insurance = { hash: "c".repeat(64), last4: "Z003" };
    const other = { hash: "d".repeat(64), last4: "Z004" };
    api.mortgages.mockImplementation(() => ok(mortgagesOf([
      mortgageSchedule({ match_references: [stored] }),
    ], [], { match_references_supported: true })));
    api.mortgageReferenceCandidates.mockImplementation(() => ok({ candidates: [
      { ...installment, label: "RATA KREDYTU NR •••• Z002" },
      { ...insurance, label: "UBEZPIECZENIE NR •••• Z003" },
      { ...other, label: "UBEZPIECZENIE NR •••• Z003" },
    ] }));
    await act(async () => {
      root.render(<MortgagesView api={api} accounts={accounts} locale="pl-PL" revision={1} onAccessLoss={() => false} />);
    });
    await settle();
    expect(api.mortgageReferenceCandidates).not.toHaveBeenCalled();
    await click(button("Edytuj"));

    expect(api.mortgageReferenceCandidates).toHaveBeenCalledOnce();
    const picker = host.querySelector("fieldset");
    expect(picker?.querySelector("legend")?.textContent).toBe("Powiąż z płatnością w banku");
    const [old, debit, premium] = [...(picker?.querySelectorAll<HTMLInputElement>('input[type="checkbox"]') ?? [])];
    if (old === undefined || debit === undefined || premium === undefined) throw new Error("reference choices missing");
    expect(old.checked).toBe(true);
    expect(debit.checked).toBe(false);
    expect(premium.checked).toBe(false);
    expect(picker?.textContent).toContain("•••• Z001");
    expect(picker?.textContent).toContain("RATA KREDYTU NR •••• Z002");
    expect(picker?.textContent).toContain("UBEZPIECZENIE NR •••• Z003 (••••Z003)");
    expect(picker?.textContent).toContain("UBEZPIECZENIE NR •••• Z003 (••••Z004)");
    expect(picker?.innerHTML).not.toContain(installment.hash);
    await click(old);
    await click(debit);
    await click(premium);
    await click(button("Zapisz harmonogram"));

    expect(api.saveMortgage).toHaveBeenCalledWith(expect.objectContaining({ match_references: [installment, insurance] }));
  });

  it("offers the picker on a capable server with no schedules or candidates", async () => {
    const api = fakeApi();
    api.mortgages.mockImplementation(() => ok(mortgagesOf([], [], { match_references_supported: true })));
    await act(async () => {
      root.render(<MortgagesView api={api} accounts={accounts} locale="en-US" revision={1} onAccessLoss={() => false} />);
    });
    await settle();
    await click(button("New schedule"));
    expect(host.querySelector("fieldset")?.textContent).toContain("Link to a bank payment");
    expect(host.textContent).toContain("No recurring bank payments with a reference found.");
  });

  it("drops superseded reads while always tripping onAccessLoss and handles empty/error states", async () => {
    const first = deferred<AnalyticsMortgages>();
    const second = deferred<AnalyticsMortgages>();
    const api = fakeApi();
    api.mortgages
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise);
    const onAccessLoss = vi.fn((result: { ok: boolean; status?: number; error?: string }) => !result.ok && result.status === 401);

    await act(async () => {
      root.render(<MortgagesView api={api} accounts={accounts}
        locale="en-US" revision={1} onAccessLoss={onAccessLoss} />);
      await Promise.resolve();
    });
    await act(async () => {
      root.render(<MortgagesView api={api} accounts={accounts}
        locale="en-US" revision={2} onAccessLoss={onAccessLoss} />);
      await Promise.resolve();
    });

    await act(async () => {
      second.settle({ ok: true, data: mortgagesOf([], []) });
      await Promise.resolve();
    });
    expect(host.textContent).toContain("No active mortgage reminders.");
    expect(host.textContent).toContain("No private mortgage schedules configured yet.");

    await act(async () => {
      first.settle({ ok: false, status: 401, error: "budget_http_401" });
      await Promise.resolve();
    });
    expect(onAccessLoss).toHaveBeenCalledWith({ ok: false, status: 401, error: "budget_http_401" });

    api.mortgages.mockImplementationOnce(() => fail(500, "budget_internal_error"));
    await act(async () => {
      root.render(<MortgagesView api={api} accounts={accounts}
        locale="en-US" revision={3} onAccessLoss={onAccessLoss} />);
      await Promise.resolve();
    });
    await settle();
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("could not be loaded");
  });
});
