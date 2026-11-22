// Synthetic consent health fixtures only; no real bank, session ID or credential values.
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BudgetApi, BudgetResult } from "./api";
import { ConsentHealthPanel, consentHealthLabels } from "./consent_health";
import { connection } from "./fixtures";
import type { BudgetConsentReconcile, BudgetConsentRenewalReminders } from "./model";

type Settle<T> = (value: BudgetResult<T>) => void;

function ok<T>(data: T): Promise<BudgetResult<T>> { return Promise.resolve({ ok: true, data }); }
function fail<T>(status: number, error: string): Promise<BudgetResult<T>> { return Promise.resolve({ ok: false, status, error }); }
function deferred<T>(): { promise: Promise<BudgetResult<T>>; settle: Settle<T> } {
  let settle: Settle<T> = () => undefined;
  const promise = new Promise<BudgetResult<T>>((resolve) => { settle = resolve; });
  return { promise, settle };
}

const HASH_CONSENT = "a".repeat(64);
const HASH_FLOW = "b".repeat(64);

function renewalsOf(reminders: BudgetConsentRenewalReminders["reminders"]): BudgetConsentRenewalReminders {
  return { evaluated_at: 1_800_000_000, lead_days: [14, 7, 3, 1], rule_version: 1, reminders };
}

function reconcileOf(changes: Partial<BudgetConsentReconcile> = {}): BudgetConsentReconcile {
  return {
    evaluated_at: 1_800_000_000, retention_days: 90, rule_version: 1,
    summary: { consents_total: 0, flows_total: 5, needs_owner_review: 0, remote_close_eligible: 0, local_retire_eligible: 0 },
    consents: [],
    flows: [{ flow_id: HASH_FLOW, connection_id: "bank", status: "failed", classification: "terminal_retained",
      needs_owner_review: false, local_retire_eligible: false, candidate_consent_id: null }],
    ...changes,
  };
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
  vi.restoreAllMocks();
});

async function settle(): Promise<void> {
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
}

describe("ConsentHealthPanel", () => {
  it("clearly distinguishes external imported consents from Hestia-managed ones in EN and PL without raw hashes or actions", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const api: Pick<BudgetApi, "consentRenewalReminders" | "consentReconcile"> = {
      consentRenewalReminders: vi.fn(() => ok(renewalsOf([
        { connection_id: "bank", bank_name: "Synthetic Bank A", valid_until: 1_815_379_200, seconds_remaining: 178 * 86400,
          days_remaining: 178, status: "healthy", active_milestone: null, reminder_due: false, paused: false,
          attention: null, destination_kind: "owner_inbox", rule_version: 1 },
        { connection_id: "bank-2", bank_name: "Synthetic Bank B", valid_until: 1_800_086_400, seconds_remaining: 86400,
          days_remaining: 1, status: "due_soon", active_milestone: 1, reminder_due: true, paused: true,
          attention: null, destination_kind: "owner_inbox", rule_version: 1 },
        { connection_id: "bank-3", bank_name: "Synthetic Bank C", valid_until: 1_799_900_000, seconds_remaining: -100_000,
          days_remaining: -2, status: "attention_required", active_milestone: "attention", reminder_due: true, paused: false,
          attention: "expired_session", destination_kind: "owner_inbox", rule_version: 1 },
      ]))),
      consentReconcile: vi.fn(() => ok(reconcileOf())),
    };

    await act(async () => {
      root.render(<ConsentHealthPanel api={api} connections={[connection]} text={consentHealthLabels.en}
        locale="en-US" revision={1} onAccessLoss={() => false} />);
      await Promise.resolve();
    });
    await settle();

    expect(host.textContent).toContain("External consents (imported): 3 · managed by Hestia: 0");
    expect(host.textContent).toContain("Authorization flows: 5");
    expect(host.textContent).toContain("178 days left");
    expect(host.textContent).toContain("1 day left");
    expect(host.textContent).toContain("Attention: expired_session");
    expect(host.textContent).toContain("Paused");
    expect(host.textContent).toContain("Finished flow (retained)");
    expect(host.textContent).not.toContain(HASH_FLOW);
    expect([...host.querySelectorAll("button")]).toHaveLength(0);
    expect(setItem).not.toHaveBeenCalled();
    expect(window.location.search).toBe("");

    await act(async () => {
      root.render(<ConsentHealthPanel api={api} connections={[connection]} text={consentHealthLabels.pl}
        locale="pl-PL" revision={2} onAccessLoss={() => false} />);
      await Promise.resolve();
    });
    await settle();

    expect(host.textContent).toContain("Zgody zewnętrzne (import): 3 · zarządzane przez Hestię: 0");
    expect(host.textContent).toContain("Pozostało 1 dzień");
    expect(host.textContent).toContain("Pozostało 178 dni");
    expect(host.textContent).toContain("Zakończona próba (zachowana)");
  });

  it("renders managed consents, advisory counts, error states and always runs onAccessLoss on stale responses", async () => {
    const firstRenewals = deferred<BudgetConsentRenewalReminders>();
    const secondRenewals = deferred<BudgetConsentRenewalReminders>();
    const onAccessLoss = vi.fn((result: { ok: boolean; status?: number; error?: string }) => !result.ok && result.status === 401);
    const api: Pick<BudgetApi, "consentRenewalReminders" | "consentReconcile"> = {
      consentRenewalReminders: vi.fn<BudgetApi["consentRenewalReminders"]>()
        .mockImplementationOnce(() => firstRenewals.promise)
        .mockImplementationOnce(() => secondRenewals.promise),
      consentReconcile: vi.fn(() => ok(reconcileOf({
        summary: { consents_total: 1, flows_total: 0, needs_owner_review: 1, remote_close_eligible: 1, local_retire_eligible: 1 },
        consents: [{ consent_id: HASH_CONSENT, connection_id: "bank", provenance: "hestia_created", status: "ACTIVE",
          valid_until: 1_815_379_200, classification: "active_current", needs_owner_review: true,
          remote_close_eligible: true, local_retire_eligible: true }],
        flows: [],
      }))),
    };

    await act(async () => {
      root.render(<ConsentHealthPanel api={api} connections={[connection]} text={consentHealthLabels.en}
        locale="en-US" revision={1} onAccessLoss={onAccessLoss} />);
      await Promise.resolve();
    });
    await act(async () => {
      root.render(<ConsentHealthPanel api={api} connections={[connection]} text={consentHealthLabels.en}
        locale="en-US" revision={2} onAccessLoss={onAccessLoss} />);
      await Promise.resolve();
    });
    await act(async () => {
      secondRenewals.settle({ ok: true, data: renewalsOf([
        { connection_id: "bank", bank_name: "Synthetic Bank", valid_until: 1_815_379_200, seconds_remaining: 864000,
          days_remaining: 10, status: "healthy", active_milestone: null, reminder_due: false, paused: false,
          attention: null, destination_kind: "owner_inbox", rule_version: 1 },
      ]) });
      await Promise.resolve();
    });
    await settle();

    // Active managed consent on "bank" is NOT double-counted as external!
    expect(host.textContent).toContain("External consents (imported): 0 · managed by Hestia: 1");
    expect(host.textContent).toContain("Eligible for remote closure (informational): 1");
    expect(host.textContent).toContain("Eligible for local cleanup (informational): 1");
    expect(host.textContent).not.toContain(HASH_CONSENT);

    // Stale first read fails with 401 -> still triggers access barrier!
    await act(async () => {
      firstRenewals.settle({ ok: false, status: 401, error: "budget_http_401" });
      await Promise.resolve();
    });
    expect(onAccessLoss).toHaveBeenCalledWith({ ok: false, status: 401, error: "budget_http_401" });

    // Error fallback renders alert cleanly.
    const errApi: Pick<BudgetApi, "consentRenewalReminders" | "consentReconcile"> = {
      consentRenewalReminders: vi.fn(() => fail<BudgetConsentRenewalReminders>(500, "budget_consent_data_error")),
      consentReconcile: vi.fn(() => fail<BudgetConsentReconcile>(500, "budget_consent_data_error")),
    };
    await act(async () => {
      root.render(<ConsentHealthPanel api={errApi} connections={[connection]} text={consentHealthLabels.en}
        locale="en-US" revision={3} onAccessLoss={() => false} />);
      await Promise.resolve();
    });
    await settle();
    expect(host.querySelectorAll('[role="alert"]')).toHaveLength(2);
  });
});
