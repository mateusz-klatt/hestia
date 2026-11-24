import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { loadLocale } from "../../i18n";
import type { BudgetApi, BudgetResult } from "../api";
import { account, connection } from "../fixtures";
import { recurringHint, recurringOf } from "./fixtures";
import type { AnalyticsRecurring } from "./model";
import { RecurringView } from "./recurring";

type Settle<T> = (value: BudgetResult<T>) => void;

function ok<T>(data: T): Promise<BudgetResult<T>> { return Promise.resolve({ ok: true, data }); }
function fail<T>(status: number, error: string): Promise<BudgetResult<T>> { return Promise.resolve({ ok: false, status, error }); }
function deferred<T>(): { promise: Promise<BudgetResult<T>>; settle: Settle<T> } {
  let settle: Settle<T> = () => undefined;
  const promise = new Promise<BudgetResult<T>>((resolve) => { settle = resolve; });
  return { promise, settle };
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

describe("RecurringView", () => {
  const accounts = [account({ id: "acc-1", display: { product: "Synthetic Main", currency: "PLN",
    masked_suffix: "0001", source: "offline_import", observed_at: 1, imported_at: 2 } })];

  it("renders exact decimal strings, neutral disclaimer, evidence and truncation notices in en and pl", async () => {
    const exact = "12345678901234567890.05";
    const hints = [
      recurringHint({
        label: "Synthetic Cloud", amount: exact, currency: "PLN",
        occurrence_count: 5, distinct_months: 5, min_gap_days: 30, max_gap_days: 30,
        evidence_truncated: true,
      }),
      recurringHint({
        label: "Synthetic Gym", label_source: "description", amount: "19.9", currency: "EUR",
        occurrence_count: 3, distinct_months: 3, min_gap_days: 28, max_gap_days: 31,
      }),
    ];
    const api: Pick<BudgetApi, "recurring"> = {
      recurring: vi.fn(() => ok(recurringOf(hints, { total_hints: 205, truncated: true }))),
    };
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const onAccessLoss = vi.fn(() => false);

    // English rendering
    await act(async () => {
      root.render(<RecurringView api={api} accounts={accounts} connections={[connection]}
        locale="en-US" revision={1} onAccessLoss={onAccessLoss} />);
      await Promise.resolve();
    });
    await settle();

    expect(host.textContent).toContain("informational hints only");
    expect(host.textContent).toContain("Showing first 2 of 205 recurring hints. Currency filter reflects currencies shown here.");
    expect(host.textContent).toContain("Synthetic Cloud");
    expect(host.textContent).toContain("\u221212,345,678,901,234,567,890.05 PLN");
    expect(host.textContent).toContain("5 occurrences");
    expect(host.textContent).toContain("5 months");
    expect(host.textContent).toContain("30 days apart");
    expect(host.textContent).toContain("28–31 days apart");
    expect(host.textContent).toContain("Showing latest 3 entries.");
    expect(host.textContent).toContain("Synthetic Bank · Synthetic Main ••0001");
    expect(host.textContent).not.toContain("rec-1");
    expect(setItem).not.toHaveBeenCalled();
    expect(window.location.search).toBe("");

    // Polish pluralization rendering (including 1 dzień / 3 miesiące / 5 miesięcy / 3 wystąpienia / 5 wystąpień)
    const plHints = [
      recurringHint({ label: "Synthetic Daily-Edge", occurrence_count: 1, distinct_months: 1, min_gap_days: 1, max_gap_days: 1 }),
      recurringHint({ label: "Synthetic Trio", occurrence_count: 3, distinct_months: 3, min_gap_days: 2, max_gap_days: 2 }),
      recurringHint({ label: "Synthetic Quint", occurrence_count: 5, distinct_months: 5, min_gap_days: 29, max_gap_days: 31 }),
    ];
    const apiPl: Pick<BudgetApi, "recurring"> = { recurring: vi.fn(() => ok(recurringOf(plHints))) };
    await loadLocale("pl");
    await act(async () => {
      root.render(<RecurringView api={apiPl} accounts={accounts} connections={[connection]}
        locale="pl-PL" revision={2} onAccessLoss={onAccessLoss} />);
      await Promise.resolve();
    });
    await settle();

    expect(host.textContent).toContain("wyłącznie podpowiedzi informacyjne");
    expect(host.textContent).toContain("1 wystąpienie");
    expect(host.textContent).toContain("1 miesiąc");
    expect(host.textContent).toContain("co 1 dzień");
    expect(host.textContent).toContain("3 wystąpienia");
    expect(host.textContent).toContain("3 miesiące");
    expect(host.textContent).toContain("co 2 dni");
    expect(host.textContent).toContain("5 wystąpień");
    expect(host.textContent).toContain("5 miesięcy");
    expect(host.textContent).toContain("co 29–31 dni");
  });

  it("filters by currency and drops late answers from earlier generations while still checking access loss", async () => {
    const first = deferred<AnalyticsRecurring>();
    const second = deferred<AnalyticsRecurring>();
    const third = deferred<AnalyticsRecurring>();
    const recurring = vi.fn<BudgetApi["recurring"]>()
      .mockImplementationOnce(() => ok(recurringOf([
        recurringHint({ label: "PLN Hint", currency: "PLN" }),
        recurringHint({ label: "EUR Hint", currency: "EUR" }),
      ])))
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise)
      .mockImplementationOnce(() => third.promise);
    const onAccessLoss = vi.fn((result: { ok: boolean; status?: number; error?: string }) => !result.ok && result.status === 401);

    await act(async () => {
      root.render(<RecurringView api={{ recurring }} accounts={accounts} connections={[connection]}
        locale="en-US" revision={1} onAccessLoss={onAccessLoss} />);
      await Promise.resolve();
    });
    await settle();

    const btn = (label: string) => {
      const found = [...host.querySelectorAll("button")].find((item) => item.textContent === label);
      if (found === undefined) throw new Error(`button ${label} missing`);
      return found;
    };
    await click(btn("PLN"));
    expect(recurring).toHaveBeenLastCalledWith("PLN");
    await click(btn("EUR"));
    expect(recurring).toHaveBeenLastCalledWith("EUR");

    // Newer EUR response settles first.
    await act(async () => {
      second.settle({ ok: true, data: recurringOf([recurringHint({ label: "Fresh EUR", currency: "EUR" })]) });
      await Promise.resolve();
    });
    expect(host.textContent).toContain("Fresh EUR");

    // Older PLN response arrives late with 401 -> must still trip access barrier before generation drop,
    // without overwriting fresh EUR content.
    await act(async () => {
      first.settle({ ok: false, status: 401, error: "budget_http_401" });
      await Promise.resolve();
    });
    expect(onAccessLoss).toHaveBeenCalledWith({ ok: false, status: 401, error: "budget_http_401" });
    expect(host.textContent).toContain("Fresh EUR");

    // Switching back to All and receiving an earlier stale ok response also gets ignored when superseded.
    const fourth = deferred<AnalyticsRecurring>();
    recurring.mockImplementationOnce(() => fourth.promise);
    await click(btn("All currencies"));
    await click(btn("PLN"));
    await act(async () => {
      fourth.settle({ ok: true, data: recurringOf([recurringHint({ label: "Fresh PLN", currency: "PLN" })]) });
      await Promise.resolve();
    });
    await act(async () => {
      third.settle({ ok: true, data: recurringOf([recurringHint({ label: "Stale All", currency: "PLN" })]) });
      await Promise.resolve();
    });
    expect(host.textContent).toContain("Fresh PLN");
    expect(host.textContent).not.toContain("Stale All");
  });

  it("renders empty and error states cleanly without throwing", async () => {
    const emptyApi: Pick<BudgetApi, "recurring"> = { recurring: vi.fn(() => ok(recurringOf([]))) };
    await act(async () => {
      root.render(<RecurringView api={emptyApi} accounts={accounts} connections={[connection]}
        locale="en-US" revision={1} onAccessLoss={() => false} />);
      await Promise.resolve();
    });
    await settle();
    expect(host.textContent).toContain("No recurring monthly charges have been detected yet.");

    const errApi: Pick<BudgetApi, "recurring"> = {
      recurring: vi.fn(() => fail<AnalyticsRecurring>(500, "budget_internal_error")),
    };
    await act(async () => {
      root.render(<RecurringView api={errApi} accounts={accounts} connections={[connection]}
        locale="en-US" revision={2} onAccessLoss={() => false} />);
      await Promise.resolve();
    });
    await settle();
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("could not be loaded");
  });
});
