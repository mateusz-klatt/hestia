// Synthetic data only: no real bank, mortgage, category, amount or session values.
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { loadLocale, t } from "../../i18n";
import type { BudgetApi, BudgetResult } from "../api";
import { connection } from "../fixtures";
import { categoryList, inboxConsentItem, inboxMarkSaved, inboxMortgageItem, inboxOf,
  inboxSpendingLimitItem, mortgageSchedule, mortgagesOf } from "./fixtures";
import { InboxView } from "./inbox";
import type { AnalyticsInbox, AnalyticsInboxItem } from "./model";

type Settle<T> = (value: BudgetResult<T>) => void;

function ok<T>(data: T): Promise<BudgetResult<T>> { return Promise.resolve({ ok: true, data }); }
function fail<T>(status: number, error: string): Promise<BudgetResult<T>> { return Promise.resolve({ ok: false, status, error }); }
function deferred<T>(): { promise: Promise<BudgetResult<T>>; settle: Settle<T> } {
  let settle: Settle<T> = () => undefined;
  const promise = new Promise<BudgetResult<T>>((resolve) => { settle = resolve; });
  return { promise, settle };
}

function fakeApi(data: AnalyticsInbox = inboxOf()) {
  return {
    inbox: vi.fn<BudgetApi["inbox"]>(() => ok(data)),
    markInbox: vi.fn<BudgetApi["markInbox"]>(() => ok({ inbox: inboxMarkSaved() })),
    mortgages: vi.fn<BudgetApi["mortgages"]>(() => ok(mortgagesOf([mortgageSchedule()]))),
    categories: vi.fn<BudgetApi["categories"]>(() => ok({ categories: categoryList })),
  } satisfies Pick<BudgetApi, "inbox" | "markInbox" | "mortgages" | "categories">;
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

describe("InboxView", () => {
  it("renders an owner device alert in Polish and marks it read through the inbox", async () => {
    const device: AnalyticsInboxItem = {
      kind: "device_added", subject_id: "a".repeat(64), milestone: "added", read: false, read_at: null,
      ref: { token_id: "synthetic-device-id" }, params: { status: "added", device_name: "Vesta", date: "2026-11-21" },
    };
    const data = inboxOf([device]);
    const api = fakeApi(data);
    await loadLocale("pl");
    await act(async () => {
      root.render(<InboxView api={api} connections={[]} consentEnabled={false} locale="pl"
        revision={0} onAccessLoss={() => false} onOpenAccounts={vi.fn()} onOpenMortgages={vi.fn()} onOpenLimits={vi.fn()} />);
      await Promise.resolve();
    });
    expect(host.querySelector("li strong")?.textContent).toBe("Nowe urządzenie: Vesta, 2026-11-21");
    expect(host.textContent).not.toContain(device.ref.token_id);
    await click(button("Oznacz jako przeczytane"));
    expect(api.markInbox).toHaveBeenCalledWith({
      items: [{ kind: device.kind, subject_id: device.subject_id, milestone: device.milestone }],
      read: true, expected_reads_version: data.reads_version,
    });
  });

  it("renders all three alert kinds resolved via item.ref in EN and PL without raw hashes or storage", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const consentAlert = inboxConsentItem({
      params: { status: "attention_required", valid_until: 1_800_000_000, days_remaining: 4, attention: "invalid_session", paused: true },
    });
    const mortgageAlert = inboxMortgageItem({
      params: { status: "overdue", due_date: "2026-11-10", days_until_due: -3, currency: "PLN", freshness_stale: true },
    });
    const limitAlert = inboxSpendingLimitItem({
      read: true,
      read_at: 1_795_175_000,
      params: { status: "exceeded", period: "2026-11", currency: "PLN", warning_percent: 80, percent_used_floor: 112 },
    });
    const api = fakeApi(inboxOf([consentAlert, mortgageAlert, limitAlert], { reads_count: 1, unread_count: 2 }));
    const onOpenAccounts = vi.fn();
    const onOpenMortgages = vi.fn();
    const onOpenLimits = vi.fn();

    await act(async () => {
      root.render(
        <InboxView api={api} connections={[connection]} consentEnabled locale="en-GB" revision={0}
          onAccessLoss={() => false} onOpenAccounts={onOpenAccounts} onOpenMortgages={onOpenMortgages}
          onOpenLimits={onOpenLimits} />,
      );
      await Promise.resolve();
    });
    await settle();

    const text = host.textContent;
    expect(text).toContain("Evaluated for 2026-11-21 (UTC)");
    expect(text).toContain("Unread: 2");
    expect(text).toContain("Saved read markers: 1 / 500");
    // Display names resolved locally via item.ref.
    expect(text).toContain("Synthetic Bank");
    expect(text).toContain("Bank session is no longer valid — reconnect needed");
    expect(text).toContain("Paused");
    expect(text).toContain("Synthetic Home Loan");
    expect(text).toContain("3 days overdue");
    expect(text).toContain("Ledger data may be outdated");
    expect(text).toContain("Synthetic Food");
    expect(text).toContain("2026-11 · 112% used (warning at 80%) · PLN");
    // Zero raw opaque hashes, zero raw milestones, zero raw attention codes.
    expect(text).not.toContain(consentAlert.subject_id);
    expect(text).not.toContain(mortgageAlert.subject_id);
    expect(text).not.toContain(limitAlert.subject_id);
    expect(text).not.toContain(consentAlert.milestone);
    expect(text).not.toContain("invalid_session");
    expect(setItem).not.toHaveBeenCalled();
    expect(window.location.search).toBe("");

    // Deep-link buttons jump to each section.
    await click(button("Open Consent status"));
    expect(onOpenAccounts).toHaveBeenCalledWith(true);
    await click(button("Open Mortgages"));
    expect(onOpenMortgages).toHaveBeenCalledTimes(1);
    await click(button("Open Spending limits"));
    expect(onOpenLimits).toHaveBeenCalledTimes(1);

    // Filter group carries its own aria-label distinct from the section title.
    expect(host.querySelector('[role="group"]')?.getAttribute("aria-label")).toBe("Alert filter");
    await click(button("Unread only"));
    expect(host.querySelectorAll(".budget-inbox-item")).toHaveLength(2);
    expect(host.textContent).not.toContain("Synthetic Food");

    // Polish copy & pluralization check.
    await loadLocale("pl");
    await act(async () => {
      root.render(
        <InboxView api={api} connections={[connection]} consentEnabled={false} locale="pl-PL" revision={1}
          onAccessLoss={() => false} onOpenAccounts={onOpenAccounts} onOpenMortgages={onOpenMortgages}
          onOpenLimits={onOpenLimits} />,
      );
      await Promise.resolve();
    });
    await settle();
    expect(host.querySelector('[role="group"]')?.getAttribute("aria-label")).toBe("Filtr alertów");
    expect(t("budget.inbox.markedPartial", { marked: 100, total: 105 })).toContain("Oznaczono 100 z 105");
    expect(host.textContent).toContain("Stan na 2026-11-21 (UTC)");
    expect(host.textContent).toContain("Nieprzeczytane: 2");
    expect(host.textContent).toContain("3 dni po terminie");
    expect(host.textContent).toContain("Otwórz Konta");
    await click(button("Otwórz Konta"));
    expect(onOpenAccounts).toHaveBeenCalledWith(false);
  });

  it("stays single-request on an empty inbox and falls back cleanly when lookup endpoints fail", async () => {
    const emptyApi = fakeApi(inboxOf([]));
    await act(async () => {
      root.render(
        <InboxView api={emptyApi} connections={[]} consentEnabled locale="en-GB" revision={0}
          onAccessLoss={() => false} onOpenAccounts={vi.fn()} onOpenMortgages={vi.fn()} onOpenLimits={vi.fn()} />,
      );
      await Promise.resolve();
    });
    await settle();
    expect(host.textContent).toContain("No active alerts right now.");
    expect(emptyApi.mortgages).not.toHaveBeenCalled();
    expect(emptyApi.categories).not.toHaveBeenCalled();
    expect(button("Mark all unread read").disabled).toBe(true);

    // Missing/failing secondary lookups fall back to generic labels without breaking the inbox.
    const fallbackApi = fakeApi(inboxOf([
      inboxConsentItem({ params: { status: "expired", valid_until: 1_700_000_000, days_remaining: -2, attention: null, paused: false } }),
      inboxMortgageItem({ params: { status: "due_today", due_date: "2026-11-21", days_until_due: 0, currency: "PLN", freshness_stale: false } }),
      inboxSpendingLimitItem({ params: { status: "approaching", period: "2026-11", currency: "EUR", warning_percent: 80, percent_used_floor: 85 } }),
    ]));
    fallbackApi.mortgages.mockImplementation(() => fail(500, "budget_internal_error"));
    fallbackApi.categories.mockImplementation(() => fail(500, "budget_internal_error"));
    await act(async () => {
      root.render(
        <InboxView api={fallbackApi} connections={[]} consentEnabled locale="en-GB" revision={1}
          onAccessLoss={() => false} onOpenAccounts={vi.fn()} onOpenMortgages={vi.fn()} onOpenLimits={vi.fn()} />,
      );
      await Promise.resolve();
    });
    await settle();
    expect(host.textContent).toContain("Bank connection");
    expect(host.textContent).toContain("Mortgage schedule");
    expect(host.textContent).toContain("Spending category");
  });

  it("supports single mark/unmark, atomic mark-all (capped at 100), busy locking, 409 reload and 413 cap", async () => {
    const cAlert = inboxConsentItem();
    const mAlert = inboxMortgageItem();
    const sAlert = inboxSpendingLimitItem({ read: true, read_at: 1_795_175_000 });
    const v0 = "0".repeat(64);
    const api = fakeApi(inboxOf([cAlert, mAlert, sAlert], { reads_version: v0, reads_count: 1, unread_count: 2 }));
    const pendingMark = deferred<{ inbox: ReturnType<typeof inboxMarkSaved> }>();
    api.markInbox.mockImplementationOnce(() => pendingMark.promise);

    await act(async () => {
      root.render(
        <InboxView api={api} connections={[connection]} consentEnabled locale="en-GB" revision={0}
          onAccessLoss={() => false} onOpenAccounts={vi.fn()} onOpenMortgages={vi.fn()} onOpenLimits={vi.fn()} />,
      );
      await Promise.resolve();
    });
    await settle();

    // Click Mark all unread read -> sends exact 2 unread items under expected_reads_version and disables buttons while busy.
    await act(async () => { button("Mark all unread read").click(); await Promise.resolve(); });
    expect(api.markInbox).toHaveBeenCalledWith({
      items: [
        { kind: cAlert.kind, subject_id: cAlert.subject_id, milestone: cAlert.milestone },
        { kind: mAlert.kind, subject_id: mAlert.subject_id, milestone: mAlert.milestone },
      ],
      read: true,
      expected_reads_version: v0,
    });
    expect(button("Unread only").disabled).toBe(true);
    expect(button("Mark all unread read").disabled).toBe(true);

    // Finish mark-all -> reloads inbox.
    const v1 = "1".repeat(64);
    api.inbox.mockImplementation(() => ok(inboxOf([
      { ...cAlert, read: true, read_at: 1_795_176_100 },
      { ...mAlert, read: true, read_at: 1_795_176_100 },
      sAlert,
    ], { reads_version: v1, reads_revision: 1, reads_count: 3, unread_count: 0 })));
    await act(async () => { pendingMark.settle({ ok: true, data: { inbox: inboxMarkSaved({ changed_count: 2, reads_version: v1 }) } }); await Promise.resolve(); });
    await settle();
    expect(host.textContent).toContain("Unread: 0");

    // Single unmark -> sends read=false with v1; simulate 409 conflict -> reloads and displays banner.
    api.markInbox.mockImplementationOnce(() => fail(409, "budget_analytics_conflict"));
    const reloadsBefore = api.inbox.mock.calls.length;
    await click(button("Mark unread"));
    expect(api.markInbox).toHaveBeenLastCalledWith({
      items: [{ kind: cAlert.kind, subject_id: cAlert.subject_id, milestone: cAlert.milestone }],
      read: false,
      expected_reads_version: v1,
    });
    expect(api.inbox.mock.calls.length).toBeGreaterThan(reloadsBefore);
    expect(host.textContent).toContain("Inbox read state changed elsewhere");

    // Simulate 409 source_changed -> reloads and displays specific source-changed message.
    api.markInbox.mockImplementationOnce(() => fail(409, "budget_analytics_source_changed"));
    await click(button("Mark unread"));
    expect(host.textContent).toContain("resolved or changed before saving");

    // Simulate 413 cap 500 -> shows cap notice without crashing.
    api.inbox.mockImplementation(() => ok(inboxOf([cAlert], { reads_version: v1, reads_count: 500, reads_cap: 500, unread_count: 1 })));
    await act(async () => {
      root.render(
        <InboxView api={api} connections={[connection]} consentEnabled locale="en-GB" revision={2}
          onAccessLoss={() => false} onOpenAccounts={vi.fn()} onOpenMortgages={vi.fn()} onOpenLimits={vi.fn()} />,
      );
      await Promise.resolve();
    });
    await settle();
    expect(host.textContent).toContain("Saved read markers: 500 / 500");
    api.markInbox.mockImplementationOnce(() => fail(413, "budget_analytics_limit"));
    await click(button("Mark read"));
    expect(host.textContent).toContain("Read marker limit (500) reached");

    // Batch cap 100: when >100 unread items exist, Mark all unread read caps the request at 100 items.
    const many: AnalyticsInboxItem[] = Array.from({ length: 105 }, (_, idx) => inboxConsentItem({
      subject_id: idx.toString(16).padStart(64, "0"),
      milestone: `g1:exp${String(idx)}:expired`,
    }));
    api.inbox.mockImplementation(() => ok(inboxOf(many, { reads_version: v1, unread_count: 105 })));
    api.markInbox.mockImplementationOnce(() => ok({ inbox: inboxMarkSaved({ changed_count: 100 }) }));
    await act(async () => {
      root.render(
        <InboxView api={api} connections={[connection]} consentEnabled locale="en-GB" revision={3}
          onAccessLoss={() => false} onOpenAccounts={vi.fn()} onOpenMortgages={vi.fn()} onOpenLimits={vi.fn()} />,
      );
      await Promise.resolve();
    });
    await settle();
    await click(button("Mark all unread read"));
    const lastCall = api.markInbox.mock.calls.at(-1)?.[0];
    expect(lastCall?.items).toHaveLength(100);
    expect(host.querySelector('[role="status"]')?.textContent).toContain("Marked 100 of 105 unread alerts");
  });

  it("invokes onAccessLoss barrier before generation check even when superseded or unmounted", async () => {
    const req = deferred<AnalyticsInbox>();
    const api = fakeApi();
    api.inbox.mockImplementationOnce(() => req.promise);
    const onAccessLoss = vi.fn((result: { ok: boolean; status?: number }) => !result.ok && result.status === 401);

    await act(async () => {
      root.render(
        <InboxView api={api} connections={[connection]} consentEnabled locale="en-GB" revision={0}
          onAccessLoss={onAccessLoss} onOpenAccounts={vi.fn()} onOpenMortgages={vi.fn()} onOpenLimits={vi.fn()} />,
      );
      await Promise.resolve();
    });
    // Unmount before the first request settles; a 401 response MUST still hit onAccessLoss.
    await act(async () => { root.unmount(); await Promise.resolve(); });
    await act(async () => { req.settle({ ok: false, status: 401, error: "budget_http_401" }); await Promise.resolve(); });
    expect(onAccessLoss).toHaveBeenCalledWith({ ok: false, status: 401, error: "budget_http_401" });
  });
});
