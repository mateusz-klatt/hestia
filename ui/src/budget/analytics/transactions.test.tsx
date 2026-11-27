// Synthetic data only: no real account, transaction or bank values.
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { loadLocale, t, tPlural } from "../../i18n";
import type { BudgetApi, BudgetResult } from "../api";
import { account } from "../fixtures";
import type { ReceiptSummary } from "../receipts_model";
import { annotationOf, categoryList, decisionOf, entriesOf, entry } from "./fixtures";
import type { AnalyticsTransaction, AnalyticsTransactions, AnnotationSaved, TransferDecisionResult } from "./model";
import { KIND_FILTER_EXCLUDE_SKIPPED, KIND_FILTER_STORAGE_KEY } from "./kind_filter";
import { TRANSACTION_ROW_CAP, TransactionsPanel } from "./transactions";

function ok<T>(data: T): Promise<BudgetResult<T>> { return Promise.resolve({ ok: true, data }); }
function fail<T>(status: number, error: string): Promise<BudgetResult<T>> { return Promise.resolve({ ok: false, status, error }); }
function pageOf(rows: AnalyticsTransaction[], cursor: string | null = null): AnalyticsTransactions {
  return entriesOf("2026-11", rows, cursor);
}
function pairDebit(reference = "out", changes: Partial<AnalyticsTransaction> = {}): AnalyticsTransaction {
  return entry(reference, { amount: "100.00", booking_date: "2026-11-10", ...changes });
}
function pairCredit(reference = "in", changes: Partial<AnalyticsTransaction> = {}): AnalyticsTransaction {
  return entry(reference, {
    account_id: "acc-2", amount: "0100.00", credit_debit_indicator: "CRDT", booking_date: "2026-11-11",
    metadata: { creditor_name: null, debtor_name: "Synthetic Card Repayment", mcc: null },
    effective: { kind: "income", category_id: null, source: "default", rule_id: null }, ...changes,
  });
}
function mockPairSearch(api: ReturnType<typeof fakeApi>, debit: AnalyticsTransaction, credits: AnalyticsTransaction[]): void {
  api.analyticsTransactions.mockImplementation((period, ...args) =>
    ok(args[2] === 200 ? entriesOf(period, credits) : pageOf([debit])));
}

function fakeApi(rows: AnalyticsTransaction[] = [entry("e1")]) {
  return {
    analyticsTransactions: vi.fn<BudgetApi["analyticsTransactions"]>(() => ok(pageOf(rows))),
    analyticsTransactionsByKeys: vi.fn<BudgetApi["analyticsTransactionsByKeys"]>(() => ok(entriesOf("", rows))),
    categories: vi.fn<BudgetApi["categories"]>(() => ok({ categories: categoryList })),
    annotate: vi.fn<BudgetApi["annotate"]>(() => ok({ annotation: annotationOf() })),
    decideTransfer: vi.fn<BudgetApi["decideTransfer"]>(() => ok({ decision: decisionOf() })),
  } satisfies Pick<BudgetApi,
    "analyticsTransactions" | "analyticsTransactionsByKeys" | "categories" | "annotate" | "decideTransfer">;
}

const accounts = [
  account({ id: "acc-1", display: { product: "Synthetic Current", currency: "PLN", masked_suffix: "0001",
    source: "offline_import", observed_at: 1, imported_at: 2 } }),
  account({ id: "acc-2", display: { product: "Synthetic Savings", currency: "PLN", masked_suffix: "0002",
    source: "offline_import", observed_at: 1, imported_at: 2 } }),
];

let host: HTMLElement;
let root: Root;
beforeEach(async () => {
  try {
    localStorage.removeItem(KIND_FILTER_STORAGE_KEY);
  } catch {
    // Ignore when localStorage is unavailable in this test environment.
  }
  await loadLocale("en");
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => { root.unmount(); await Promise.resolve(); });
  host.remove();
  vi.unstubAllGlobals();
  try {
    localStorage.removeItem(KIND_FILTER_STORAGE_KEY);
  } catch {
    // Ignore when localStorage is unavailable in this test environment.
  }
  await loadLocale("en");
});

async function settle(): Promise<void> {
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
}

interface Options {
  period?: string;
  revision?: number;
  lost?: boolean;
  rowCap?: number;
  locale?: string;
  receiptsList?: BudgetApi["receiptsList"];
}
async function show(api: ReturnType<typeof fakeApi>, options: Options = {}) {
  if (options.locale !== undefined) await loadLocale(options.locale.startsWith("pl") ? "pl" : "en");
  const onAccessLoss = vi.fn((result: { ok: boolean }) => options.lost === true && !result.ok);
  const onChanged = vi.fn();
  const onCreateRule = vi.fn();
  await act(async () => {
    root.render(<TransactionsPanel api={api} receiptsList={options.receiptsList} period={options.period ?? "2026-11"} currencies={["EUR", "PLN"]}
      accounts={accounts} locale={options.locale ?? "en"} revision={options.revision ?? 0}
      onAccessLoss={onAccessLoss} onChanged={onChanged} rowCap={options.rowCap} onCreateRule={onCreateRule} />);
    await Promise.resolve();
  });
  await settle();
  return { onAccessLoss, onChanged, onCreateRule };
}

async function drain(times = 12): Promise<void> {
  for (let i = 0; i < times; i += 1) await settle();
}

function button(text: string, scope: ParentNode = host): HTMLButtonElement {
  const found = [...scope.querySelectorAll("button")].find((item) => item.textContent === text);
  if (found === undefined) throw new Error(`button ${text} missing`);
  return found;
}
async function click(element: HTMLElement): Promise<void> {
  await act(async () => { element.click(); await Promise.resolve(); });
  await settle();
}
function transactionDrawer(): HTMLElement {
  const found = host.querySelector<HTMLElement>('.budget-entry-drawer[role="dialog"]');
  if (found === null) throw new Error("transaction details drawer missing");
  return found;
}
function rowDetailsButton(scope: ParentNode = host): HTMLButtonElement {
  const found = scope.querySelector<HTMLButtonElement>("button.budget-entry-open");
  if (found === null) throw new Error("transaction details button missing");
  return found;
}
function categoryButton(scope: ParentNode = host): HTMLButtonElement {
  const found = scope.querySelector<HTMLButtonElement>("button.budget-entry-category");
  if (found === null) throw new Error("transaction category button missing");
  return found;
}
async function editCategory(scope?: ParentNode): Promise<void> {
  await click(categoryButton(scope));
}
async function openPair(scope?: ParentNode): Promise<void> {
  await click(rowDetailsButton(scope));
  await click(button(t("budget.transactions.pair"), transactionDrawer()));
}
async function pressEscape(): Promise<void> {
  act(() => {
    (document.activeElement ?? document).dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  });
  await settle();
}
function select(label: string, scope: ParentNode = host): HTMLSelectElement {
  const found = [...scope.querySelectorAll("label")].find((item) => item.textContent.startsWith(label))?.querySelector("select");
  if (found === null || found === undefined) throw new Error(`select ${label} missing`);
  return found;
}
async function typeInto(input: HTMLInputElement, value: string): Promise<void> {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    await Promise.resolve();
  });
}
async function choose(element: HTMLSelectElement, value: string): Promise<void> {
  await act(async () => {
    element.value = value;
    element.dispatchEvent(new Event("change", { bubbles: true }));
    await Promise.resolve();
  });
}
async function submit(form: HTMLFormElement | null): Promise<void> {
  await act(async () => { form?.requestSubmit(); await Promise.resolve(); });
  await settle();
}
async function applyCurrencyFilter(): Promise<void> {
  await choose(select("Currency"), "EUR");
  await submit(host.querySelector(".budget-range"));
}

describe("period entries", () => {
  it("lists the server's rows with category, source, kind, transfer and stale facts", async () => {
    const api = fakeApi([
      entry("e1", { metadata: { creditor_name: "Synthetic Grocer", debtor_name: "Synthetic Owner", mcc: "5411" },
        effective: { category_id: "cat-food", kind: "expense", source: "rule", rule_id: "rule-1" } }),
      entry("e2", { credit_debit_indicator: "CRDT", amount: "40", metadata: { creditor_name: "Synthetic Owner", debtor_name: null, mcc: null },
        effective: { category_id: null, kind: "unclassified_credit", source: "default", rule_id: null },
        transfer: { status: "suggested", decision_id: null, candidate_count: 2, ambiguous: true } }),
      entry("e3", { annotation: { mode: "manual", kind: "refund", category_id: "cat-old", version: 2, stale: true },
        effective: { category_id: "cat-old", kind: "refund", source: "manual", rule_id: null },
        transfer: { status: "review", decision_id: "dec-1", candidate_count: 1, ambiguous: false } }),
    ]);
    await show(api);
    expect(api.analyticsTransactions).toHaveBeenCalledWith("2026-11", { currency: "", category: "", kind: "", query: "" });
    const entries = [...host.querySelectorAll(".budget-transaction-table tbody tr")];
    const rows = entries.map((row) => row.textContent);
    expect(rows[0]).toContain("Synthetic Grocer");
    expect(rows[0]).toContain("−12.50 PLN");
    expect(rows[0]).toContain("Synthetic Current ••0001");
    expect(rows[0]).toContain("Synthetic Food");
    expect(rows[1]).toContain("Synthetic shop e2");
    expect(rows[1]).toContain("40.00 PLN");
    expect(rows[1]).toContain("Uncategorised");
    expect(rows[1]).toContain("To classify");
    expect(rows[1]).toContain("possible own transfer");
    expect(rows[1]).toContain("fits several pairs");
    expect(rows[1]).not.toContain("Synthetic Owner");
    expect(rows[2]).toContain("Synthetic Old");
    expect(rows[2]).toContain("Refund");
    expect(rows[2]).toContain("own transfer to re-check");
    expect(rows[2]).toContain("the bank changed this entry");
    expect(host.textContent).toContain("3 entries");

    await click(rowDetailsButton(entries[0]));
    expect(transactionDrawer().textContent).toContain("by rule");
    await pressEscape();
    await click(rowDetailsButton(entries[2]));
    expect(transactionDrawer().textContent).toContain("set by you");
  });

  it("opens details from the row and restores focus to the details or category button on Escape", async () => {
    const api = fakeApi([entry("details", {
      metadata: { creditor_name: "Synthetic Grocer", debtor_name: null, mcc: "5411" },
      effective: { category_id: "cat-food", kind: "expense", source: "manual", rule_id: null },
    })]);
    await show(api);
    const row = host.querySelector<HTMLElement>(".budget-transaction-table tbody tr");
    if (row === null) throw new Error("transaction row missing");
    const detailsButton = rowDetailsButton(row);
    const edit = categoryButton(row);
    expect(edit.textContent).toContain("Synthetic Food");
    expect(row.textContent).not.toContain(t("budget.transactions.pair"));
    expect([...row.querySelectorAll("button")].map((item) => item.textContent)).not.toContain(t("budget.transactions.edit"));

    await click(row);
    const drawer = transactionDrawer();
    expect(drawer.textContent).toContain("Synthetic Grocer");
    expect(drawer.textContent).toContain("−12.50 PLN");
    expect(drawer.contains(document.activeElement)).toBe(true);
    expect(drawer.querySelector(".budget-entry-editor")).toBeNull();
    await pressEscape();
    expect(host.querySelector(".budget-entry-drawer")).toBeNull();
    expect(document.activeElement).toBe(detailsButton);

    await click(edit);
    expect(transactionDrawer().querySelector(".budget-entry-editor")).not.toBeNull();
    await pressEscape();
    expect(host.querySelector(".budget-entry-drawer")).toBeNull();
    expect(document.activeElement).toBe(edit);
  });

  it("distinguishes a linked invoice, missing proof, a payment confirmation and a document to recheck", async () => {
    const invoice: ReceiptSummary = {
      receipt_id: "11111111-1111-4111-8111-111111111111", raw_shop_name: "Synthetic linked invoice",
      document_kind: "invoice", status: "confirmed", source_kind: "attachment", receipt_kind: "purchase",
      receipt_date: "2026-11-03", receipt_time: null, raw_nip: null, shop_id: null, chain_key: null,
      total_amount: "12.5", currency: "PLN", payment_method: "card", math_delta: "0", linked: true,
      linked_account_id: "acc-1", linked_entry_ref: "69".repeat(32), version: 1, image_available: false,
      image_expires_at: null, raw_text_expires_at: null, invoice_number: "SYN/100", due_date: null,
      import_expires_at: null, forwarded_manually: false, forwarded_sender_domain: null, gateway: null,
      gateway_status: null, fee_amount: null, payee_host: null, evidence_for_receipt_id: null,
      has_document: true, card_last4: null, created_at: 1, updated_at: 1,
    };
    const receiptsList = vi.fn<BudgetApi["receiptsList"]>(() => ok({
      next_cursor: null,
      receipts: [
        invoice,
        { ...invoice, receipt_id: "22222222-2222-4222-8222-222222222222", linked_entry_ref: "70".repeat(32),
          document_kind: "payment_confirmation", source_kind: "email", gateway: "Synthetic gateway", has_document: false },
        { ...invoice, receipt_id: "33333333-3333-4333-8333-333333333333", linked_entry_ref: "72".repeat(32), status: "needs_review" },
      ],
    }));
    // Table rendering is the subject of this test. Map distinct synthetic references to repeatable digests.
    vi.stubGlobal("crypto", { subtle: { digest: (_algorithm: string, bytes: Uint8Array) => Promise.resolve(new Uint8Array(32).fill(bytes[0] ?? 0).buffer) } });
    try {
      await show(fakeApi([entry("invoice"), entry("missing"), entry("payment"), entry("review")]), { receiptsList });
      const rows = [...host.querySelectorAll(".budget-transaction-table tbody tr")];
      expect(rows.map((row) => row.querySelector(".budget-entry-document")?.getAttribute("data-document-state")))
        .toEqual(["present", "missing", "payment", "review"]);
      await click(rowDetailsButton(rows[0]));
      expect(transactionDrawer().textContent).toContain("Synthetic linked invoice");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("offers a recipient-account rule in details and the editor using only the opaque key and masked display", async () => {
    const syntheticIban = `PL${"0".repeat(22)}1234`;
    const key = "a".repeat(64);
    const api = fakeApi([entry("account-row", { counterparty_account_key: key, counterparty_account: "PL** … 1234" })]);
    const { onCreateRule } = await show(api, { locale: "pl" });
    const expected = {
      rule_id: null, expected_version: 0, field: "counterparty_account", operator: "equals", value: "PL** … 1234",
      counterparty_account_key: key, category_id: null, kind: "expense", priority: 100, enabled: true,
    };
    expect(host.querySelector(".budget-transaction-table")?.textContent).not.toContain("Utwórz regułę z konta odbiorcy");
    await click(rowDetailsButton());
    const detailAction = button("Utwórz regułę z konta odbiorcy", transactionDrawer());
    expect(detailAction.title).toBe("PL** … 1234");
    await click(detailAction);
    expect(onCreateRule).toHaveBeenCalledWith(expected);

    onCreateRule.mockClear();
    await pressEscape();
    await editCategory();
    const editor = host.querySelector(".budget-entry-editor");
    expect(editor).not.toBeNull();
    await click(button("Utwórz regułę z konta odbiorcy", editor as HTMLElement));
    expect(onCreateRule).toHaveBeenCalledWith(expected);
    expect(JSON.stringify(onCreateRule.mock.calls[0]?.[0])).not.toContain(syntheticIban);
  });

  it("applies filters only on submit and offers active categories plus uncategorised", async () => {
    const api = fakeApi();
    await show(api);
    const options = [...select("Category").options].map((option) => option.value);
    expect(options).toEqual(["", "uncategorised", "cat-food"]);
    await choose(select("Currency"), "EUR");
    await choose(select("Category"), "uncategorised");
    await choose(select("Type"), "refund");
    const search = host.querySelector<HTMLInputElement>('input[type="search"]');
    await act(async () => {
      if (search === null) throw new Error("search missing");
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(search, "shop");
      search.dispatchEvent(new Event("input", { bubbles: true }));
      await Promise.resolve();
    });
    expect(api.analyticsTransactions).toHaveBeenCalledTimes(1);
    await submit(host.querySelector(".budget-range"));
    expect(api.analyticsTransactions).toHaveBeenLastCalledWith("2026-11", { currency: "EUR", category: "uncategorised", kind: "refund", query: "shop" });
  });

  it("loads further pages with the server cursor and keeps the filter", async () => {
    const api = fakeApi();
    api.analyticsTransactions.mockImplementationOnce(() => ok(pageOf([entry("e1")], "cursor-1")))
      .mockImplementationOnce(() => ok(pageOf([entry("e2")])));
    await show(api);
    await drain();
    expect(api.analyticsTransactions).toHaveBeenLastCalledWith("2026-11", { currency: "", category: "", kind: "", query: "" }, "cursor-1");
    expect(host.querySelectorAll(".budget-transaction-table tbody tr")).toHaveLength(2);
    expect(host.textContent).not.toContain("Loading…");
  });

  it("saves a first manual category with version 0 and tells the view to re-read", async () => {
    const api = fakeApi();
    const { onChanged } = await show(api);
    await editCategory();
    const editor = host.querySelector(".budget-entry-editor");
    if (editor === null) throw new Error("editor missing");
    expect([...select("Category", editor).options].map((option) => option.value)).toEqual(["auto", "none", "cat:cat-food"]);
    await choose(select("Category", editor), "cat:cat-food");
    await choose(select("Type", editor), "expense");
    await submit(host.querySelector(".budget-entry-editor"));
    expect(api.annotate).toHaveBeenCalledWith({ account_id: "acc-1", entry_reference: "e1", mode: "manual",
      category_id: "cat-food", kind: "expense", expected_fingerprint: "fp-e1", expected_version: 0, label: null });
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(host.querySelector(".budget-entry-editor")).toBeNull();
  });

  it("shows a one-off label on the row and saves a trimmed label from the editor", async () => {
    const phrase = "Remont łazienki";
    const annotated = entry("e1", {
      annotation: {
        version: 3, mode: "manual", category_id: "cat-food", kind: "expense", stale: true, label: phrase,
      },
    });
    const api = fakeApi([annotated]);
    await show(api);
    expect(host.querySelector(".budget-one-off")?.textContent).toBe(phrase);
    expect(host.textContent).toContain(t("budget.transactions.stale"));
    await editCategory();
    const editor = host.querySelector(".budget-entry-editor");
    const input = editor?.querySelector<HTMLInputElement>('input[name="one_off_label"]');
    if (editor === null || input === undefined || input === null) throw new Error("label field missing");
    expect(input.value).toBe(phrase);
    expect(editor.textContent).toContain("One-off label");
    expect(editor.textContent).toContain("instead of Other");
    await typeInto(input, "  Fachowiec  ");
    await submit(editor instanceof HTMLFormElement ? editor : null);
    expect(api.annotate).toHaveBeenCalledWith(expect.objectContaining({ label: "Fachowiec", expected_version: 3 }));
  });

  it("uses the Polish one-off label and clears it when the field is blank", async () => {
    const api = fakeApi();
    await show(api, { locale: "pl" });
    await editCategory();
    const editor = host.querySelector(".budget-entry-editor");
    const input = editor?.querySelector<HTMLInputElement>('input[name="one_off_label"]');
    if (editor === null || input === undefined || input === null) throw new Error("label field missing");
    expect(editor.textContent).toContain("Etykieta jednorazowa");
    expect(editor.textContent).toContain("zamiast w Pozostałe");
    await typeInto(input, "   ");
    await submit(editor instanceof HTMLFormElement ? editor : null);
    expect(api.annotate).toHaveBeenCalledWith(expect.objectContaining({ label: null }));
  });

  it("distinguishes explicit uncategorised from a reset to automatic, with the seen version", async () => {
    const annotated = entry("e1", { credit_debit_indicator: "CRDT",
      annotation: { mode: "manual", kind: "income", category_id: "cat-old", version: 7, stale: false } });
    const api = fakeApi([annotated]);
    await show(api);
    await editCategory();
    const editor = host.querySelector(".budget-entry-editor");
    if (editor === null) throw new Error("editor missing");
    // The archived category the entry already uses stays selectable for it.
    expect(select("Category", editor).value).toBe("cat:cat-old");
    expect(select("Type", editor).value).toBe("income");
    await choose(select("Category", editor), "none");
    await submit(host.querySelector(".budget-entry-editor"));
    expect(api.annotate).toHaveBeenLastCalledWith(expect.objectContaining({ mode: "manual", category_id: null, kind: "income", expected_version: 7 }));
    await pressEscape();
    await editCategory();
    await choose(select("Category", host.querySelector(".budget-entry-editor") ?? host), "auto");
    await choose(select("Type", host.querySelector(".budget-entry-editor") ?? host), "auto");
    await submit(host.querySelector(".budget-entry-editor"));
    expect(api.annotate).toHaveBeenLastCalledWith(expect.objectContaining({ mode: "auto", category_id: null, kind: "auto" }));
  });

  it("makes a type choice a manual decision that keeps the entry's current category visibly", async () => {
    const api = fakeApi([entry("e1", { effective: { category_id: "cat-old", kind: "expense", source: "rule", rule_id: "rule-1" } }),
      entry("e2")]);
    await show(api);
    const [first, second] = [...host.querySelectorAll<HTMLElement>(".budget-transaction-table tbody tr")];
    if (first === undefined || second === undefined) throw new Error("rows missing");
    await editCategory(first);
    let editor = transactionDrawer().querySelector(".budget-entry-editor") ?? host;
    expect(select("Category", editor).value).toBe("auto");
    // The archived category the entry currently shows stays available for it, marked as archived.
    expect([...select("Category", editor).options].map((option) => option.value)).toContain("cat:cat-old");
    expect([...select("Category", editor).options].find((option) => option.value === "cat:cat-old")?.textContent).toBe("Synthetic Old (archived)");
    await choose(select("Type", editor), "excluded");
    expect(select("Category", editor).value).toBe("cat:cat-old");
    await submit(transactionDrawer().querySelector(".budget-entry-editor"));
    expect(api.annotate).toHaveBeenLastCalledWith(expect.objectContaining({ entry_reference: "e1", mode: "manual",
      category_id: "cat-old", kind: "excluded" }));
    await pressEscape();
    await editCategory(second);
    editor = host.querySelector(".budget-entry-editor") ?? host;
    await choose(select("Type", editor), "expense");
    expect(select("Category", editor).value).toBe("none");
    // Going back to automatic hands the type back to the rules as well.
    await choose(select("Category", editor), "auto");
    expect(select("Type", editor).value).toBe("auto");
    await submit(host.querySelector(".budget-entry-editor"));
    expect(api.annotate).toHaveBeenLastCalledWith(expect.objectContaining({ entry_reference: "e2", mode: "auto", kind: "auto" }));
  });

  it("never offers an archived category from a stale manual choice that the entry no longer shows", async () => {
    const api = fakeApi([entry("e1", { annotation: { version: 4, mode: "manual", category_id: "cat-old", kind: "expense", stale: true },
      effective: { category_id: "cat-food", kind: "expense", source: "rule", rule_id: "rule-1" } })]);
    await show(api);
    await editCategory();
    const options = [...select("Category", host.querySelector(".budget-entry-editor") ?? host).options].map((option) => option.value);
    expect(options).toEqual(["auto", "none", "cat:cat-food"]);
  });

  it("offers only types that fit the entry's direction and drops an incompatible stored one to automatic", async () => {
    const api = fakeApi([entry("out"), entry("in", { credit_debit_indicator: "CRDT",
      annotation: { version: 2, mode: "manual", category_id: null, kind: "expense", stale: false } })]);
    await show(api);
    const rows = [...host.querySelectorAll<HTMLElement>(".budget-transaction-table tbody tr")];
    await editCategory(rows[0]);
    let editor = host.querySelector(".budget-entry-editor") ?? host;
    expect([...select("Type", editor).options].map((option) => option.value)).toEqual(["auto", "expense", "excluded"]);
    await click(button("Cancel", editor));
    await pressEscape();
    await editCategory(host.querySelectorAll<HTMLElement>(".budget-transaction-table tbody tr")[1]);
    editor = host.querySelector(".budget-entry-editor") ?? host;
    expect([...select("Type", editor).options].map((option) => option.value)).toEqual(["auto", "income", "refund", "excluded"]);
    expect(select("Type", editor).value).toBe("auto");
  });

  it("locks the editor while a save is in flight", async () => {
    let saved: (value: BudgetResult<{ annotation: AnnotationSaved }>) => void = () => undefined;
    const api = fakeApi();
    await show(api);
    api.annotate.mockImplementationOnce(() => new Promise((resolve) => { saved = resolve; }));
    await editCategory();
    await submit(host.querySelector(".budget-entry-editor"));
    const editor = host.querySelector(".budget-entry-editor") ?? host;
    expect(select("Category", editor).disabled).toBe(true);
    expect(select("Type", editor).disabled).toBe(true);
    await act(async () => { saved({ ok: true, data: { annotation: annotationOf() } }); await Promise.resolve(); });
    await settle();
  });

  it("reloads instead of overwriting after a version conflict and explains other failures", async () => {
    const api = fakeApi();
    const { onChanged } = await show(api);
    api.annotate.mockImplementationOnce(() => fail(409, "budget_analytics_conflict"));
    await editCategory();
    await submit(host.querySelector(".budget-entry-editor"));
    expect(host.textContent).toContain("changed elsewhere");
    expect(api.analyticsTransactions).toHaveBeenCalledTimes(2);
    expect(onChanged).not.toHaveBeenCalled();
    api.annotate.mockImplementationOnce(() => fail(400, "budget_invalid_request"));
    await editCategory();
    await submit(host.querySelector(".budget-entry-editor"));
    expect(host.textContent).toContain("Could not save");
    expect(host.querySelector(".budget-entry-editor")).not.toBeNull();
    await click(button("Cancel"));
    expect(host.querySelector(".budget-entry-editor")).toBeNull();
  });

  it("reloads with a clear message when the bank changed the entry, and restarts on a stale cursor", async () => {
    const api = fakeApi();
    await show(api);
    api.annotate.mockImplementationOnce(() => fail(409, "budget_analytics_source_changed"));
    await editCategory();
    await submit(host.querySelector(".budget-entry-editor"));
    expect(host.textContent).toContain("The bank changed this entry");
    expect(api.analyticsTransactions).toHaveBeenCalledTimes(2);
    api.analyticsTransactions.mockImplementationOnce(() => ok(pageOf([entry("e1")], "cursor-1")))
      .mockImplementationOnce(() => fail(409, "budget_analytics_stale_cursor"));
    await show(api, { revision: 1 });
    await drain();
    expect(api.analyticsTransactions).toHaveBeenLastCalledWith("2026-11", { currency: "", category: "", kind: "", query: "" });
  });

  it("hands list and save answers to the access barrier first", async () => {
    const api = fakeApi();
    api.analyticsTransactions.mockImplementation(() => fail(401, "budget_http_401"));
    const { onAccessLoss } = await show(api, { lost: true });
    expect(onAccessLoss).toHaveBeenCalledWith({ ok: false, status: 401, error: "budget_http_401" });
    expect(host.textContent).not.toContain("could not be loaded");
  });

  it("drops a late page from a previous period", async () => {
    let late: (value: BudgetResult<AnalyticsTransactions>) => void = () => undefined;
    const api = fakeApi([entry("october")]);
    api.analyticsTransactions.mockImplementationOnce(() => new Promise((resolve) => { late = resolve; }));
    await show(api);
    expect(host.textContent).toContain("Loading entries");
    await show(api, { period: "2026-10" });
    await act(async () => { late({ ok: true, data: pageOf([entry("november")]) }); await Promise.resolve(); });
    await settle();
    expect(host.textContent).toContain("Synthetic shop october");
    expect(host.textContent).not.toContain("november");
  });

  it("reports a late save without restoring an editor from another period", async () => {
    let saved: (value: BudgetResult<{ annotation: AnnotationSaved }>) => void = () => undefined;
    const api = fakeApi();
    const first = await show(api);
    api.annotate.mockImplementationOnce(() => new Promise((resolve) => { saved = resolve; }));
    await editCategory();
    await submit(host.querySelector(".budget-entry-editor"));
    expect(button("Save").disabled).toBe(true);
    await show(api, { period: "2026-10" });
    await act(async () => { saved({ ok: true, data: { annotation: annotationOf() } }); await Promise.resolve(); });
    await settle();
    expect(first.onChanged).toHaveBeenCalledTimes(1);
    expect(host.querySelector(".budget-entry-editor")).toBeNull();
  });

  it("reports a successful save after a filter change without restoring stale UI", async () => {
    let saved: (value: BudgetResult<{ annotation: AnnotationSaved }>) => void = () => undefined;
    const api = fakeApi();
    const { onChanged } = await show(api);
    api.annotate.mockImplementationOnce(() => new Promise((resolve) => { saved = resolve; }));
    await editCategory();
    await submit(host.querySelector(".budget-entry-editor"));
    await applyCurrencyFilter();
    await act(async () => { saved({ ok: true, data: { annotation: annotationOf() } }); await Promise.resolve(); });
    await settle();
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(host.querySelector(".budget-entry-editor")).toBeNull();
    expect(host.querySelector(".budget-notice")).toBeNull();
  });

  it("drops a failed save after a filter change without notifying or showing a notice", async () => {
    let saved: (value: BudgetResult<{ annotation: AnnotationSaved }>) => void = () => undefined;
    const api = fakeApi();
    const { onChanged } = await show(api);
    api.annotate.mockImplementationOnce(() => new Promise((resolve) => { saved = resolve; }));
    await editCategory();
    await submit(host.querySelector(".budget-entry-editor"));
    await applyCurrencyFilter();
    await act(async () => { saved({ ok: false, status: 500, error: "budget_internal_error" }); await Promise.resolve(); });
    await settle();
    expect(onChanged).not.toHaveBeenCalled();
    expect(host.querySelector(".budget-notice")).toBeNull();
  });

  it("reports failed and empty reads plainly", async () => {
    const api = fakeApi([]);
    await show(api);
    expect(host.textContent).toContain("No entries match.");
    api.analyticsTransactions.mockImplementation(() => fail(500, "budget_internal_error"));
    api.categories.mockImplementation(() => fail(500, "budget_internal_error"));
    await show(api, { revision: 1 });
    expect(host.textContent).toContain("Entries could not be loaded.");
    expect(host.textContent).not.toContain("No entries match.");
  });

  function names(): string[] {
    return [...host.querySelectorAll(".budget-transaction-table tbody .budget-entry-text")]
      .map((item) => item.textContent);
  }
  function sortButton(label: string): HTMLButtonElement {
    const headers = host.querySelector(".budget-transaction-table thead");
    if (headers === null) throw new Error("transaction table headers missing");
    const control = [...headers.querySelectorAll("th[scope=col] button")].find((item) => item.firstElementChild?.textContent === label);
    if (!(control instanceof HTMLButtonElement)) throw new Error(`sort control missing for ${label}`);
    return control;
  }
  function sortDirection(label: string): string | null | undefined {
    return sortButton(label).closest("th")?.getAttribute("aria-sort");
  }
  function classified(reference: string, changes: Partial<AnalyticsTransaction>): AnalyticsTransaction {
    return entry(reference, changes);
  }

  it("sorts by date, category and absolute amount from the column headers", async () => {
    const rows = [
      classified("mid", { booking_date: "2026-11-02", amount: "10.00",
        effective: { kind: "expense", category_id: "cat-food", source: "default", rule_id: null } }),
      classified("early", { booking_date: "2026-11-01", amount: "-30.00",
        effective: { kind: "expense", category_id: null, source: "default", rule_id: null } }),
      classified("late", { booking_date: null, value_date: "2026-11-03", amount: "2.50", credit_debit_indicator: "CRDT",
        effective: { kind: "income", category_id: "cat-old", source: "default", rule_id: null } }),
    ];
    const api = fakeApi(rows);
    await show(api);
    expect(host.querySelector("table.budget-transaction-table")?.getAttribute("aria-label")).toBe("Transactions");
    expect(host.querySelector(".budget-sort")).toBeNull();
    expect(names()).toEqual(["Synthetic shop mid", "Synthetic shop early", "Synthetic shop late"]);
    expect(sortDirection("Date")).toBeNull();
    expect(sortDirection("Category")).toBeNull();
    expect(sortDirection("Amount")).toBeNull();

    await click(sortButton("Date"));
    expect(sortDirection("Date")).toBe("ascending");
    expect(sortButton("Date").querySelector('[aria-hidden="true"]')?.textContent.trim()).toBe("↑");
    expect(names()).toEqual(["Synthetic shop early", "Synthetic shop mid", "Synthetic shop late"]);
    await click(sortButton("Date"));
    expect(sortDirection("Date")).toBe("descending");
    expect(sortButton("Date").querySelector('[aria-hidden="true"]')?.textContent.trim()).toBe("↓");
    expect(names()).toEqual(["Synthetic shop late", "Synthetic shop mid", "Synthetic shop early"]);

    await click(sortButton("Category"));
    expect(sortDirection("Date")).toBeNull();
    expect(sortDirection("Category")).toBe("ascending");
    expect(names()).toEqual(["Synthetic shop mid", "Synthetic shop late", "Synthetic shop early"]);
    await click(sortButton("Category"));
    expect(sortDirection("Category")).toBe("descending");
    expect(names()).toEqual(["Synthetic shop early", "Synthetic shop late", "Synthetic shop mid"]);

    await click(sortButton("Amount"));
    expect(sortDirection("Amount")).toBe("ascending");
    expect(sortDirection("Category")).toBeNull();
    // Absolute values: 2.50, 10.00, 30.00. The negative amount is not treated as smaller.
    expect(names()).toEqual(["Synthetic shop late", "Synthetic shop mid", "Synthetic shop early"]);
    await click(sortButton("Amount"));
    expect(sortDirection("Amount")).toBe("descending");
    expect(names()).toEqual(["Synthetic shop early", "Synthetic shop mid", "Synthetic shop late"]);
  });

  it("orders equal absolute amounts by their previous order and keeps a missing date last", async () => {
    const rows = [
      classified("debit", { booking_date: "2026-11-04", amount: "40.00", credit_debit_indicator: "DBIT" }),
      classified("none", { booking_date: null, value_date: null, transaction_date: null, amount: "1.00" }),
      classified("credit", { booking_date: "2026-11-02", amount: "-40.00", credit_debit_indicator: "CRDT" }),
    ];
    const api = fakeApi(rows);
    await show(api);
    await click(sortButton("Amount"));
    expect(names()).toEqual(["Synthetic shop none", "Synthetic shop debit", "Synthetic shop credit"]);
    await click(sortButton("Date"));
    expect(names()).toEqual(["Synthetic shop credit", "Synthetic shop debit", "Synthetic shop none"]);
    await click(sortButton("Date"));
    expect(names()).toEqual(["Synthetic shop debit", "Synthetic shop credit", "Synthetic shop none"]);
  });

  it("sorts amounts above 2^53 and mixed scales exactly without float rounding", async () => {
    const rows = [
      classified("huge-hi-neg", { amount: "-9007199254740993.25" }),
      classified("ten-half", { amount: "10.500" }),
      classified("huge-mid", { amount: "9007199254740993" }),
      classified("ten-int", { amount: "10" }),
      classified("huge-lo", { amount: "9007199254740992.5" }),
      classified("ten-frac-pad", { amount: "0010.05" }),
      classified("ten-fortyfive", { amount: "10.45" }),
    ];
    const api = fakeApi(rows);
    await show(api);
    await click(sortButton("Amount"));
    expect(names()).toEqual([
      "Synthetic shop ten-int",
      "Synthetic shop ten-frac-pad",
      "Synthetic shop ten-fortyfive",
      "Synthetic shop ten-half",
      "Synthetic shop huge-lo",
      "Synthetic shop huge-mid",
      "Synthetic shop huge-hi-neg",
    ]);
    await click(sortButton("Amount"));
    expect(names()).toEqual([
      "Synthetic shop huge-hi-neg",
      "Synthetic shop huge-mid",
      "Synthetic shop huge-lo",
      "Synthetic shop ten-half",
      "Synthetic shop ten-fortyfive",
      "Synthetic shop ten-frac-pad",
      "Synthetic shop ten-int",
    ]);
  });

  it("keeps unparsable amounts last in both sort directions", async () => {
    const rows = [
      classified("bad-text", { amount: "abc" }),
      classified("high", { amount: "-100.00" }),
      classified("bad-empty", { amount: "" }),
      classified("low", { amount: "5.00" }),
      classified("bad-dots", { amount: "1.2.3" }),
    ];
    const api = fakeApi(rows);
    await show(api);
    await click(sortButton("Amount"));
    expect(names()).toEqual([
      "Synthetic shop low",
      "Synthetic shop high",
      "Synthetic shop bad-text",
      "Synthetic shop bad-empty",
      "Synthetic shop bad-dots",
    ]);
    await click(sortButton("Amount"));
    expect(names()).toEqual([
      "Synthetic shop high",
      "Synthetic shop low",
      "Synthetic shop bad-text",
      "Synthetic shop bad-empty",
      "Synthetic shop bad-dots",
    ]);
  });

  it("sorts Polish category names using Polish collation and labels the list Transakcje", async () => {
    const plCategories = [
      { id: "c-zolw", name: "Żółw", archived: false, version: 1, created_at: 1, updated_at: 1, parent_id: null },
      { id: "c-zebra", name: "Zebra", archived: false, version: 1, created_at: 1, updated_at: 1, parent_id: null },
      { id: "c-zrebak", name: "Źrebak", archived: false, version: 1, created_at: 1, updated_at: 1, parent_id: null },
      { id: "c-lodz", name: "Łódź", archived: false, version: 1, created_at: 1, updated_at: 1, parent_id: null },
      { id: "c-lod", name: "Lód", archived: false, version: 1, created_at: 1, updated_at: 1, parent_id: null },
      { id: "c-cma", name: "Ćma", archived: false, version: 1, created_at: 1, updated_at: 1, parent_id: null },
      { id: "c-cebula", name: "Cebula", archived: false, version: 1, created_at: 1, updated_at: 1, parent_id: null },
    ];
    const rows = [
      classified("zolw", { effective: { kind: "expense", category_id: "c-zolw", source: "default", rule_id: null } }),
      classified("lodz", { effective: { kind: "expense", category_id: "c-lodz", source: "default", rule_id: null } }),
      classified("cma", { effective: { kind: "expense", category_id: "c-cma", source: "default", rule_id: null } }),
      classified("zebra", { effective: { kind: "expense", category_id: "c-zebra", source: "default", rule_id: null } }),
      classified("lod", { effective: { kind: "expense", category_id: "c-lod", source: "default", rule_id: null } }),
      classified("zrebak", { effective: { kind: "expense", category_id: "c-zrebak", source: "default", rule_id: null } }),
      classified("cebula", { effective: { kind: "expense", category_id: "c-cebula", source: "default", rule_id: null } }),
    ];
    const api = fakeApi(rows);
    api.categories.mockImplementation(() => ok({ categories: plCategories }));
    await show(api, { locale: "pl" });
    expect(host.querySelector(".budget-transaction-table")?.getAttribute("aria-label")).toBe("Transakcje");
    await click(sortButton(t("budget.transactions.category")));
    expect(names()).toEqual([
      "Synthetic shop cebula",
      "Synthetic shop cma",
      "Synthetic shop lod",
      "Synthetic shop lodz",
      "Synthetic shop zebra",
      "Synthetic shop zrebak",
      "Synthetic shop zolw",
    ]);
    await click(sortButton(t("budget.transactions.category")));
    expect(names()).toEqual([
      "Synthetic shop zolw",
      "Synthetic shop zrebak",
      "Synthetic shop zebra",
      "Synthetic shop lodz",
      "Synthetic shop lod",
      "Synthetic shop cma",
      "Synthetic shop cebula",
    ]);
  });

  it("keeps fetching pages in the background and shows how many rows are in", async () => {
    const cursors: Array<string | undefined> = [];
    let release: (value: BudgetResult<AnalyticsTransactions>) => void = () => undefined;
    const api = fakeApi();
    api.analyticsTransactions.mockImplementation((_period, _filter, cursor) => {
      cursors.push(cursor);
      if (cursor === undefined) return ok(pageOf([entry("p1")], "c1"));
      if (cursor === "c1") return new Promise((resolve) => { release = resolve; });
      return ok(pageOf([entry("p3")]));
    });
    await show(api);
    expect(host.textContent).toContain("Synthetic shop p1");
    expect(host.textContent).toContain(t("budget.transactions.loadingMore", { count: 1 }));
    await act(async () => { release({ ok: true, data: pageOf([entry("p2")], "c2") }); await Promise.resolve(); });
    await drain();
    expect(cursors).toEqual([undefined, "c1", "c2"]);
    expect(names()).toEqual(["Synthetic shop p1", "Synthetic shop p2", "Synthetic shop p3"]);
    expect(host.textContent).not.toContain("Loading…");
    await loadLocale("pl");
    expect(t("budget.transactions.loadingMore", { count: 4 })).toBe("Wczytywanie… 4");
  });

  it("drops a background page that resolves after the period changes", async () => {
    let late: (value: BudgetResult<AnalyticsTransactions>) => void = () => undefined;
    const api = fakeApi();
    api.analyticsTransactions.mockImplementation((period, _filter, cursor) => {
      if (period === "2026-11" && cursor === undefined) return ok(pageOf([entry("first")], "c1"));
      if (period === "2026-11") return new Promise((resolve) => { late = resolve; });
      return ok(pageOf([entry("october")]));
    });
    await show(api);
    expect(host.textContent).toContain(t("budget.transactions.loadingMore", { count: 1 }));
    await show(api, { period: "2026-10" });
    expect(names()).toEqual(["Synthetic shop october"]);
    await act(async () => { late({ ok: true, data: pageOf([entry("late")]) }); await Promise.resolve(); });
    await drain();
    expect(names()).toEqual(["Synthetic shop october"]);
    expect(host.textContent).not.toContain("late");
  });

  it("ignores a stale background page arriving after a filter change", async () => {
    let late: (value: BudgetResult<AnalyticsTransactions>) => void = () => undefined;
    const api = fakeApi();
    api.analyticsTransactions.mockImplementation((_period, filter, cursor) => {
      if (filter.currency === "EUR") return ok(pageOf([entry("eur-only")]));
      if (cursor === undefined) return ok(pageOf([entry("all-first")], "c1"));
      return new Promise((resolve) => { late = resolve; });
    });
    await show(api);
    expect(names()).toEqual(["Synthetic shop all-first"]);
    await applyCurrencyFilter();
    expect(names()).toEqual(["Synthetic shop eur-only"]);
    await act(async () => { late({ ok: true, data: pageOf([entry("stale-after-filter")]) }); await Promise.resolve(); });
    await drain();
    expect(names()).toEqual(["Synthetic shop eur-only"]);
    expect(host.textContent).not.toContain("stale-after-filter");
  });

  it("stops the background walk when access is lost", async () => {
    const calls: Array<string | undefined> = [];
    const api = fakeApi();
    api.analyticsTransactions.mockImplementation((_period, _filter, cursor) => {
      calls.push(cursor);
      if (cursor === undefined) return ok(pageOf([entry("kept")], "c1"));
      return fail(401, "budget_http_401");
    });
    const { onAccessLoss } = await show(api, { lost: true });
    await drain();
    expect(calls).toEqual([undefined, "c1"]);
    expect(onAccessLoss).toHaveBeenCalledWith({ ok: false, status: 401, error: "budget_http_401" });
    expect(names()).toEqual(["Synthetic shop kept"]);
    expect(host.textContent).not.toContain("could not be loaded");
    expect(host.textContent).not.toContain("Loading…");
  });

  it("recovers from an error mid-walk by retrying from the failed cursor", async () => {
    const calls: Array<string | undefined> = [];
    let failSecond = true;
    const api = fakeApi();
    api.analyticsTransactions.mockImplementation((_period, _filter, cursor) => {
      calls.push(cursor);
      if (cursor === undefined) return ok(pageOf([entry("p1")], "c1"));
      if (cursor === "c1" && failSecond) return fail(500, "budget_internal_error");
      if (cursor === "c1") return ok(pageOf([entry("p2")], "c2"));
      return ok(pageOf([entry("p3")]));
    });
    await show(api);
    await drain();
    expect(calls).toEqual([undefined, "c1"]);
    expect(names()).toEqual(["Synthetic shop p1"]);
    expect(host.querySelector('[role="alert"]')?.textContent).toContain(t("budget.transactions.error"));
    await loadLocale("pl");
    expect(t("budget.transactions.retry")).toBe("Ponów");
    await loadLocale("en");

    failSecond = false;
    await click(button(t("budget.transactions.retry")));
    await drain();
    expect(calls).toEqual([undefined, "c1", "c1", "c2"]);
    expect(names()).toEqual(["Synthetic shop p1", "Synthetic shop p2", "Synthetic shop p3"]);
    expect(host.querySelector('[role="alert"]')).toBeNull();
  });

  it("stops background loading at the row cap, shows a truncation notice, and continues via load more", async () => {
    expect(TRANSACTION_ROW_CAP).toBe(5000);
    await loadLocale("pl");
    expect(t("budget.transactions.more")).toBe("Wczytaj więcej");
    await loadLocale("en");
    const calls: Array<string | undefined> = [];
    const api = fakeApi();
    api.analyticsTransactions.mockImplementation((_period, _filter, cursor) => {
      calls.push(cursor);
      const page = calls.length;
      if (page === 3) return ok({ ...pageOf([entry("p3a"), entry("p3b")]), filtered_total: 6 });
      return ok({ ...pageOf([entry(`p${String(page)}a`), entry(`p${String(page)}b`)], `c${String(page)}`), filtered_total: 6 });
    });
    await show(api, { rowCap: 3 });
    await drain();
    expect(calls).toEqual([undefined, "c1"]);
    expect(names()).toEqual(["Synthetic shop p1a", "Synthetic shop p1b", "Synthetic shop p2a", "Synthetic shop p2b"]);
    expect(host.textContent).not.toContain("Loading…");
    expect(host.querySelector('[role="note"]')?.textContent).toContain(t("budget.transactions.truncated", { shown: 4, total: 6 }));

    await click(button(t("budget.transactions.more")));
    await drain();
    expect(calls).toEqual([undefined, "c1", "c2"]);
    expect(names()).toEqual([
      "Synthetic shop p1a", "Synthetic shop p1b", "Synthetic shop p2a", "Synthetic shop p2b", "Synthetic shop p3a", "Synthetic shop p3b",
    ]);
    expect(host.querySelector('[role="note"]')).toBeNull();
  });

  it("caps automatic reloads on staleCursor at two and deduplicates rows by rowKey", async () => {
    const calls: Array<string | undefined> = [];
    let staleRemaining = 3;
    const api = fakeApi();
    api.analyticsTransactions.mockImplementation((_period, _filter, cursor) => {
      calls.push(cursor);
      if (cursor === undefined) return ok(pageOf([entry("p1"), entry("dup")], "c1"));
      if (staleRemaining > 0) {
        staleRemaining -= 1;
        return fail(409, "budget_analytics_stale_cursor");
      }
      return ok(pageOf([entry("dup"), entry("p2")]));
    });
    await show(api);
    await drain();
    // Initial attempt + at most 2 automatic reloads = 3 pairs of (undefined, c1).
    expect(calls).toEqual([undefined, "c1", undefined, "c1", undefined, "c1"]);
    expect(host.querySelector('[role="alert"]')?.textContent).toContain(t("budget.transactions.error"));

    await click(button(t("budget.transactions.retry")));
    await drain();
    expect(names()).toEqual(["Synthetic shop p1", "Synthetic shop dup", "Synthetic shop p2"]);
    expect(host.querySelector('[role="alert"]')).toBeNull();
  });

  it("keeps row order stable while the editor is open and re-sorts when closed or re-sorted", async () => {
    let releasePage2: (value: BudgetResult<AnalyticsTransactions>) => void = () => undefined;
    let releasePage3: (value: BudgetResult<AnalyticsTransactions>) => void = () => undefined;
    const api = fakeApi();
    api.analyticsTransactions.mockImplementation((_period, _filter, cursor) => {
      if (cursor === undefined) {
        return ok(pageOf([entry("high", { amount: "50.00" }), entry("mid", { amount: "20.00" })], "c1"));
      }
      if (cursor === "c1") return new Promise((resolve) => { releasePage2 = resolve; });
      return new Promise((resolve) => { releasePage3 = resolve; });
    });
    await show(api);
    await click(sortButton("Amount"));
    expect(names()).toEqual(["Synthetic shop mid", "Synthetic shop high"]);

    await editCategory(host.querySelectorAll(".budget-transaction-table tbody tr")[0]);
    expect(host.querySelector(".budget-entry-editor")).not.toBeNull();

    // Background page arrives with a smaller amount while editor is open: existing rows must not jump.
    await act(async () => {
      releasePage2({ ok: true, data: pageOf([entry("low", { amount: "5.00" })], "c2") });
      await Promise.resolve();
    });
    await settle();
    expect(names()).toEqual(["Synthetic shop mid", "Synthetic shop high", "Synthetic shop low"]);
    expect(transactionDrawer().querySelector(".budget-entry-editor")).not.toBeNull();

    // User explicitly re-sorts while editor is still open: order updates immediately.
    await click(sortButton("Amount"));
    expect(names()).toEqual(["Synthetic shop high", "Synthetic shop mid", "Synthetic shop low"]);

    // Another background page arrives while editor remains open: newly sorted order stays stable.
    await act(async () => {
      releasePage3({ ok: true, data: pageOf([entry("huge", { amount: "100.00" })]) });
      await Promise.resolve();
    });
    await settle();
    expect(names()).toEqual(["Synthetic shop high", "Synthetic shop mid", "Synthetic shop low", "Synthetic shop huge"]);

    // Closing the editor restores live sorting across all loaded rows.
    await click(button(t("budget.transactions.cancel")));
    expect(names()).toEqual(["Synthetic shop huge", "Synthetic shop high", "Synthetic shop mid", "Synthetic shop low"]);
  });

  it("stops background loading when a page adds no unique rows after deduplication", async () => {
    const calls: Array<string | undefined> = [];
    const api = fakeApi();
    api.analyticsTransactions.mockImplementation((_period, _filter, cursor) => {
      calls.push(cursor);
      if (cursor === undefined) return ok(pageOf([entry("p1")], "c1"));
      if (cursor === "c1") return ok(pageOf([entry("p1")], "c2"));
      return ok(pageOf([entry("unreachable")]));
    });
    await show(api);
    await drain();
    expect(calls).toEqual([undefined, "c1"]);
    expect(names()).toEqual(["Synthetic shop p1"]);
    expect(host.textContent).not.toContain("Loading…");
    expect(host.querySelector('[role="note"]')).toBeNull();
  });

  it("keeps row order stable while the pair picker is open and restores live sorting when closed or confirmed", async () => {
    const listNames = names;
    let releasePage2: (value: BudgetResult<AnalyticsTransactions>) => void = () => undefined;
    let releasePage3: (value: BudgetResult<AnalyticsTransactions>) => void = () => undefined;
    const api = fakeApi();
    api.analyticsTransactions.mockImplementation((period, ...args) => {
      const [, cursor, limit] = args;
      if (limit === 200) return ok(entriesOf(period, [pairCredit("in", { amount: "20.00" })]));
      if (cursor === undefined) {
        return ok(pageOf([pairDebit("high", { amount: "50.00" }), pairDebit("mid", { amount: "20.00" })], "c1"));
      }
      if (cursor === "c1") return new Promise((resolve) => { releasePage2 = resolve; });
      return new Promise((resolve) => { releasePage3 = resolve; });
    });
    await show(api);
    await click(sortButton("Amount"));
    expect(listNames()).toEqual(["Synthetic shop mid", "Synthetic shop high"]);

    await openPair(host.querySelectorAll(".budget-transaction-table tbody tr")[0]);
    expect(host.querySelector(".budget-pair-picker")).not.toBeNull();

    // Background page arrives with a smaller amount while picker is open: existing rows must not jump.
    await act(async () => {
      releasePage2({ ok: true, data: pageOf([pairDebit("low", { amount: "5.00" })], "c2") });
      await Promise.resolve();
    });
    await settle();
    expect(listNames()).toEqual(["Synthetic shop mid", "Synthetic shop high", "Synthetic shop low"]);
    expect(transactionDrawer().querySelector(".budget-pair-picker")).not.toBeNull();

    // User explicitly re-sorts while picker is still open: order updates immediately.
    await click(sortButton("Amount"));
    expect(listNames()).toEqual(["Synthetic shop high", "Synthetic shop mid", "Synthetic shop low"]);

    // Another background page arrives while picker remains open: newly sorted order stays stable.
    await act(async () => {
      releasePage3({ ok: true, data: pageOf([pairDebit("huge", { amount: "100.00" })]) });
      await Promise.resolve();
    });
    await settle();
    expect(listNames()).toEqual(["Synthetic shop high", "Synthetic shop mid", "Synthetic shop low", "Synthetic shop huge"]);

    // Closing the picker restores live sorting across all loaded rows.
    await click(button(t("budget.transactions.cancel")));
    expect(listNames()).toEqual(["Synthetic shop huge", "Synthetic shop high", "Synthetic shop mid", "Synthetic shop low"]);

    // Confirming a pair also releases the frozen order.
    let releaseConfirmPage2: (value: BudgetResult<AnalyticsTransactions>) => void = () => undefined;
    api.analyticsTransactions.mockImplementation((period, ...args) => {
      const [, cursor, limit] = args;
      if (limit === 200) return ok(entriesOf(period, [pairCredit("in", { amount: "20.00" })]));
      if (cursor === undefined) {
        return ok(pageOf([pairDebit("high", { amount: "50.00" }), pairDebit("mid", { amount: "20.00" })], "c1"));
      }
      return new Promise((resolve) => { releaseConfirmPage2 = resolve; });
    });
    await show(api, { revision: 1 });
    await click(sortButton("Amount"));
    await openPair(host.querySelectorAll(".budget-transaction-table tbody tr")[0]);
    await act(async () => {
      releaseConfirmPage2({ ok: true, data: pageOf([pairDebit("low", { amount: "5.00" })]) });
      await Promise.resolve();
    });
    await settle();
    expect(listNames()).toEqual(["Synthetic shop mid", "Synthetic shop high", "Synthetic shop low"]);
    await click(button(t("budget.transactions.pairConfirm")));
    expect(host.querySelector(".budget-pair-picker")).toBeNull();
    expect(listNames()).toEqual(["Synthetic shop low", "Synthetic shop mid", "Synthetic shop high"]);
  });
});

describe("manual own-transfer pairing", () => {
  it("localizes same-day, singular and plural day distances", async () => {
    const daysText = (days: number) => (days === 0 ? t("budget.transactions.pairDaysSame") : tPlural("budget.transactions.pairDays", days));
    await loadLocale("en");
    expect([0, 1, 2].map(daysText)).toEqual(["same day", "1 day apart", "2 days apart"]);
    await loadLocale("pl");
    expect([0, 1, 2].map(daysText)).toEqual(["ten sam dzień", "1 dzień różnicy", "2 dni różnicy"]);
  });

  it("shows the pairing action only in the details of an eligible debit", async () => {
    const rows = [
      pairDebit("eligible"),
      pairDebit("credit", { credit_debit_indicator: "CRDT" }),
      pairDebit("zero", { amount: "0.00" }),
      pairDebit("dateless", { booking_date: null, value_date: null }),
      pairDebit("kind", { effective: { kind: "transfer", category_id: null, source: "default", rule_id: null } }),
      pairDebit("accepted", { transfer: { status: "accepted", decision_id: "dec-1", candidate_count: 0, ambiguous: false } }),
    ];
    const api = fakeApi(rows);
    await show(api);
    await click(button(t("budget.transactions.showAll")));
    const rendered = [...host.querySelectorAll<HTMLElement>(".budget-transaction-table tbody tr")];
    expect(rendered).toHaveLength(rows.length);
    for (const [index, row] of rendered.entries()) {
      expect(row.textContent).not.toContain(t("budget.transactions.pair"));
      await click(rowDetailsButton(row));
      expect([...transactionDrawer().querySelectorAll("button")]
        .some((item) => item.textContent === t("budget.transactions.pair"))).toBe(index === 0);
      await pressEscape();
    }
  });

  it("searches both adjacent months with currency and limit, following every cursor", async () => {
    const debit = pairDebit("out", { booking_date: "2026-11-02" });
    const credit = pairCredit("in", { booking_date: "2026-10-31" });
    const api = fakeApi([debit]);
    await show(api);
    api.analyticsTransactions.mockImplementation((period, ...args) => {
      const [, cursor, limit] = args;
      if (limit !== 200) return ok(pageOf([debit]));
      if (period === "2026-10" && cursor === undefined) return ok(entriesOf(period, [], "october-next"));
      return ok(entriesOf(period, period === "2026-10" ? [credit] : []));
    });
    await openPair();
    const filter = { currency: "PLN", category: "", kind: "", query: "" };
    expect(api.analyticsTransactions.mock.calls.slice(1)).toEqual([
      ["2026-10", filter, undefined, 200], ["2026-10", filter, "october-next", 200], ["2026-11", filter, undefined, 200],
    ]);
    expect(host.textContent).toContain(t("budget.transactions.pairTitle"));
    expect(host.textContent).toContain("Synthetic Card Repayment");
    expect(host.textContent).toContain("Synthetic Savings ••0002");
    expect(host.textContent).toContain("100.00 PLN");
    expect(host.textContent).toContain("2 days apart");
    expect(host.querySelectorAll(".budget-pair-picker .budget-entry")).toHaveLength(1);
  });

  it("stops each month at ten pages and warns that the search was partial", async () => {
    const debit = pairDebit();
    const api = fakeApi([debit]);
    await show(api);
    let searchPage = 0;
    api.analyticsTransactions.mockImplementation((period, ...args) => {
      if (args[2] !== 200) return ok(pageOf([debit]));
      searchPage += 1;
      return ok(entriesOf(period, [], `search-${String(searchPage)}`));
    });
    await openPair();
    expect(api.analyticsTransactions.mock.calls.filter((args) => args[3] === 200)).toHaveLength(10);
    expect(host.textContent).toContain(t("budget.transactions.pairLimited"));
    expect(host.textContent).toContain(t("budget.transactions.pairEmpty"));
  });

  it("restarts a stale search once, then reports a second stale cursor as a failure", async () => {
    const debit = pairDebit();
    const api = fakeApi([debit]);
    await show(api);
    let searchPage = 0;
    api.analyticsTransactions.mockImplementation((period, ...args) => {
      if (args[2] !== 200) return ok(pageOf([debit]));
      searchPage += 1;
      return searchPage % 2 === 1 ? ok(entriesOf(period, [], "stale")) : fail(409, "budget_analytics_stale_cursor");
    });
    await openPair();
    expect(api.analyticsTransactions.mock.calls.filter((args) => args[3] === 200)).toHaveLength(4);
    expect(host.textContent).toContain(t("budget.transactions.pairError"));
    expect(host.textContent).not.toContain(t("budget.transactions.pairLoading"));
  });

  it("renders an explicit empty state when the search has no acceptable credit", async () => {
    const debit = pairDebit();
    const api = fakeApi([debit]);
    await show(api);
    mockPairSearch(api, debit, [pairCredit("wrong", { amount: "100.01" })]);
    await openPair();
    expect(host.textContent).toContain(t("budget.transactions.pairEmpty"));
    expect(host.querySelectorAll(".budget-pair-picker .budget-entry")).toHaveLength(0);
  });

  it("sends the exact manual decision and reloads the view after confirmation", async () => {
    const debit = pairDebit();
    const credit = pairCredit();
    const api = fakeApi([debit]);
    const { onChanged } = await show(api);
    mockPairSearch(api, debit, [credit]);
    await openPair();
    await click(button(t("budget.transactions.pairConfirm")));
    expect(api.decideTransfer).toHaveBeenCalledWith({
      action: "accept", source: "manual", decision_id: null, expected_version: 0,
      debit: { account_id: "acc-1", entry_reference: "out" },
      credit: { account_id: "acc-2", entry_reference: "in" },
      expected_debit_fingerprint: "tfp-out", expected_credit_fingerprint: "tfp-in",
    });
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(host.querySelector(".budget-pair-picker")).toBeNull();
  });

  it.each([
    { code: "budget_analytics_source_changed", notice: "budget.transactions.changed" as const, reload: true },
    { code: "budget_analytics_leg_reserved", notice: "budget.transactions.pairReserved" as const, reload: true },
    { code: "budget_analytics_conflict", notice: "budget.transactions.pairExisting" as const, reload: true },
    { code: "budget_analytics_invalid_request", notice: "budget.transactions.pairInvalid" as const, reload: false },
    { code: "budget_analytics_stale_cursor", notice: "budget.transactions.conflict" as const, reload: true },
    { code: "budget_internal_error", notice: "budget.transactions.saveError" as const, reload: false },
  ])("maps $code to its pairing notice and reload behavior", async ({ code, notice, reload }) => {
    const debit = pairDebit();
    const credit = pairCredit();
    const api = fakeApi([debit]);
    const { onChanged } = await show(api);
    mockPairSearch(api, debit, [credit]);
    api.decideTransfer.mockImplementationOnce(() => fail(409, code));
    await openPair();
    await click(button(t("budget.transactions.pairConfirm")));
    expect(host.textContent).toContain(t(notice));
    expect(api.analyticsTransactions.mock.calls.filter((args) => args[3] !== 200)).toHaveLength(reload ? 2 : 1);
    expect(host.querySelector(".budget-pair-picker") === null).toBe(reload);
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("hands a failed search to the access barrier before doing anything else", async () => {
    const debit = pairDebit();
    const api = fakeApi([debit]);
    const { onAccessLoss } = await show(api, { lost: true });
    api.analyticsTransactions.mockImplementation((...args) =>
      args[3] === 200 ? fail(401, "budget_http_401") : ok(pageOf([debit])));
    await openPair();
    expect(onAccessLoss).toHaveBeenCalledWith({ ok: false, status: 401, error: "budget_http_401" });
    expect(api.analyticsTransactions.mock.calls.filter((args) => args[3] === 200)).toHaveLength(1);
    expect(host.textContent).not.toContain(t("budget.transactions.pairError"));
  });

  it("hands confirmation to the access barrier first and releases its busy state", async () => {
    const debit = pairDebit();
    const api = fakeApi([debit]);
    const { onAccessLoss, onChanged } = await show(api, { lost: true });
    mockPairSearch(api, debit, [pairCredit()]);
    api.decideTransfer.mockImplementationOnce(() => fail(401, "budget_http_401"));
    await openPair();
    await click(button(t("budget.transactions.pairConfirm")));
    expect(onAccessLoss).toHaveBeenCalledWith({ ok: false, status: 401, error: "budget_http_401" });
    expect(onChanged).not.toHaveBeenCalled();
    expect(host.textContent).not.toContain(t("budget.transactions.saveError"));
    expect(button(t("budget.transactions.pairConfirm")).disabled).toBe(false);
  });

  it("ignores a search answer that arrives after cancellation", async () => {
    const debit = pairDebit();
    const api = fakeApi([debit]);
    await show(api);
    let loaded: (value: BudgetResult<AnalyticsTransactions>) => void = () => undefined;
    api.analyticsTransactions.mockImplementation((...args) => {
      if (args[3] !== 200) return ok(pageOf([debit]));
      return new Promise((resolve) => { loaded = resolve; });
    });
    await openPair();
    expect(host.textContent).toContain(t("budget.transactions.pairLoading"));
    await click(button(t("budget.transactions.cancel")));
    await act(async () => { loaded({ ok: true, data: entriesOf("2026-11", [pairCredit()]) }); await Promise.resolve(); });
    await settle();
    expect(host.querySelector(".budget-pair-picker")).toBeNull();
    expect(host.textContent).not.toContain("Synthetic Card Repayment");
  });

  it("closes the picker and drops its pending answer when the editor opens", async () => {
    const debit = pairDebit();
    const api = fakeApi([debit]);
    await show(api);
    let loaded: (value: BudgetResult<AnalyticsTransactions>) => void = () => undefined;
    api.analyticsTransactions.mockImplementation((...args) => {
      if (args[3] !== 200) return ok(pageOf([debit]));
      return new Promise((resolve) => { loaded = resolve; });
    });
    await openPair();
    expect(host.textContent).toContain(t("budget.transactions.pairLoading"));
    await editCategory(transactionDrawer());
    expect(host.querySelector(".budget-pair-picker")).toBeNull();
    expect(host.querySelector(".budget-entry-editor")).not.toBeNull();
    await act(async () => { loaded({ ok: true, data: entriesOf("2026-11", [pairCredit()]) }); await Promise.resolve(); });
    await settle();
    expect(host.querySelector(".budget-pair-picker")).toBeNull();
    expect(host.querySelector(".budget-entry-editor")).not.toBeNull();
    expect(host.textContent).not.toContain("Synthetic Card Repayment");
  });

  it("drops a late search answer after the panel reloads", async () => {
    const debit = pairDebit();
    const api = fakeApi([debit]);
    await show(api);
    let loaded: (value: BudgetResult<AnalyticsTransactions>) => void = () => undefined;
    api.analyticsTransactions.mockImplementationOnce(() => new Promise((resolve) => { loaded = resolve; }));
    await openPair();
    api.analyticsTransactions.mockImplementation(() => ok(pageOf([debit])));
    await show(api, { revision: 1 });
    await act(async () => { loaded({ ok: true, data: entriesOf("2026-11", [pairCredit()]) }); await Promise.resolve(); });
    await settle();
    expect(host.querySelector(".budget-pair-picker")).toBeNull();
    expect(host.textContent).not.toContain("Synthetic Card Repayment");
  });

  it("reports a late confirmation without restoring a picker after the panel reloads", async () => {
    const debit = pairDebit();
    const api = fakeApi([debit]);
    const { onChanged } = await show(api);
    mockPairSearch(api, debit, [pairCredit()]);
    let decided: (value: BudgetResult<{ decision: TransferDecisionResult }>) => void = () => undefined;
    api.decideTransfer.mockImplementationOnce(() => new Promise((resolve) => { decided = resolve; }));
    await openPair();
    await click(button(t("budget.transactions.pairConfirm")));
    await show(api, { revision: 1 });
    await act(async () => { decided({ ok: true, data: { decision: decisionOf() } }); await Promise.resolve(); });
    await settle();
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(host.querySelector(".budget-pair-picker")).toBeNull();
  });

  it("reports a successful confirmation after a filter change without restoring stale UI", async () => {
    const debit = pairDebit();
    const api = fakeApi([debit]);
    const { onChanged } = await show(api);
    mockPairSearch(api, debit, [pairCredit()]);
    let decided: (value: BudgetResult<{ decision: TransferDecisionResult }>) => void = () => undefined;
    api.decideTransfer.mockImplementationOnce(() => new Promise((resolve) => { decided = resolve; }));
    await openPair();
    await click(button(t("budget.transactions.pairConfirm")));
    await applyCurrencyFilter();
    await act(async () => { decided({ ok: true, data: { decision: decisionOf() } }); await Promise.resolve(); });
    await settle();
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(host.querySelector(".budget-pair-picker")).toBeNull();
    expect(host.querySelector(".budget-notice")).toBeNull();
  });

  it("drops a failed confirmation after a filter change without notifying or showing a notice", async () => {
    const debit = pairDebit();
    const api = fakeApi([debit]);
    const { onChanged } = await show(api);
    mockPairSearch(api, debit, [pairCredit()]);
    let decided: (value: BudgetResult<{ decision: TransferDecisionResult }>) => void = () => undefined;
    api.decideTransfer.mockImplementationOnce(() => new Promise((resolve) => { decided = resolve; }));
    await openPair();
    await click(button(t("budget.transactions.pairConfirm")));
    await applyCurrencyFilter();
    await act(async () => { decided({ ok: false, status: 500, error: "budget_internal_error" }); await Promise.resolve(); });
    await settle();
    expect(onChanged).not.toHaveBeenCalled();
    expect(host.querySelector(".budget-notice")).toBeNull();
  });

  it("disables row and picker actions while a pair is being confirmed", async () => {
    const debit = pairDebit();
    const api = fakeApi([debit]);
    const { onChanged } = await show(api);
    mockPairSearch(api, debit, [pairCredit()]);
    let decided: (value: BudgetResult<{ decision: TransferDecisionResult }>) => void = () => undefined;
    api.decideTransfer.mockImplementationOnce(() => new Promise((resolve) => { decided = resolve; }));
    await openPair();
    await click(button(t("budget.transactions.pairConfirm")));
    expect(button(t("budget.transactions.pairConfirm")).disabled).toBe(true);
    expect(button(t("budget.transactions.cancel")).disabled).toBe(true);
    expect([...host.querySelectorAll("button")].some((item) => item.textContent === t("budget.transactions.pair"))).toBe(false);
    expect(categoryButton(transactionDrawer()).disabled).toBe(true);
    expect(button(t("budget.main.close"), transactionDrawer()).disabled).toBe(true);
    await pressEscape();
    expect(transactionDrawer().querySelector(".budget-pair-picker")).not.toBeNull();
    await act(async () => { decided({ ok: true, data: { decision: decisionOf() } }); await Promise.resolve(); });
    await settle();
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(button(t("budget.transactions.pair"), transactionDrawer()).disabled).toBe(false);
  });

  it("cancels an open picker without changing anything", async () => {
    const debit = pairDebit();
    const api = fakeApi([debit]);
    const { onChanged } = await show(api);
    mockPairSearch(api, debit, [pairCredit()]);
    await openPair();
    expect([...host.querySelectorAll("button")].some((item) => item.textContent === t("budget.transactions.pair"))).toBe(false);
    await click(button(t("budget.transactions.cancel")));
    expect(host.querySelector(".budget-pair-picker")).toBeNull();
    expect(button(t("budget.transactions.pair"))).toBeDefined();
    expect(api.decideTransfer).not.toHaveBeenCalled();
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("closes the category editor when the picker opens", async () => {
    const debit = pairDebit();
    const api = fakeApi([debit]);
    await show(api);
    mockPairSearch(api, debit, [pairCredit()]);
    await editCategory();
    expect(host.querySelector(".budget-entry-editor")).not.toBeNull();
    await click(button(t("budget.transactions.pair"), transactionDrawer()));
    expect(host.querySelector(".budget-entry-editor")).toBeNull();
    expect(host.querySelector(".budget-pair-picker")).not.toBeNull();
  });

  it("keeps one picker open and drops a late answer from the previous picker", async () => {
    const rows = [pairDebit("first"), pairDebit("second")];
    const api = fakeApi(rows);
    await show(api);
    let loaded: (value: BudgetResult<AnalyticsTransactions>) => void = () => undefined;
    api.analyticsTransactions.mockImplementationOnce(() => new Promise((resolve) => { loaded = resolve; }));
    const rendered = [...host.querySelectorAll<HTMLElement>(".budget-transaction-table tbody tr")];
    await openPair(rendered[0]);
    await pressEscape();
    api.analyticsTransactions.mockImplementation(() => ok(entriesOf("2026-11", [pairCredit()])));
    await openPair(rendered[1]);
    await act(async () => { loaded({ ok: true, data: entriesOf("2026-11", [pairCredit("late")]) }); await Promise.resolve(); });
    await settle();
    expect(host.querySelectorAll(".budget-pair-picker")).toHaveLength(1);
    const drawer = transactionDrawer();
    expect(drawer.textContent).toContain("Synthetic shop second");
    expect(drawer.textContent).toContain("Synthetic Card Repayment");
    expect(drawer.textContent).not.toContain("Synthetic shop late");
  });

  it("fetches server-provided row keys across adjacent periods, shows truncation note without leaking keys, and toggles cleanly", async () => {
    const scrollIntoView = vi.fn();
    const prevScroll = (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
    Element.prototype.scrollIntoView = scrollIntoView;
    try {
      const confirmedCard = account({ id: "acc-card-secret", kind: "credit_card", kind_confirmed_at: 10 });
      const provisionalRows = [
        entry("ref-secret-1", { account_id: "acc-secret-1", booking_date: "2026-11-10", remittance_information: ["Synthetic Grocery"] }),
        entry("ref-secret-dec", { account_id: "acc-secret-1", booking_date: "2026-12-02", transaction_date: "2026-11-29", remittance_information: ["Synthetic Dec Booked"] }),
        entry("ref-secret-oct", { account_id: "acc-card-secret", booking_date: "2026-10-28", remittance_information: ["Synthetic Window Salary"] }),
        entry("ref-secret-stale", {
          account_id: "acc-secret-1",
          remittance_information: ["Synthetic Stale Exclusion"],
          annotation: { version: 1, mode: "manual", category_id: null, kind: "excluded", stale: true },
          effective: { kind: "expense", category_id: "cat-food", source: "rule", rule_id: "rule-exp" },
        }),
      ];
      const novemberRows = [
        provisionalRows[0] as AnalyticsTransaction,
        provisionalRows[3] as AnalyticsTransaction,
        entry("nov-clean-1", { booking_date: "2026-11-11", remittance_information: ["Synthetic November Clean A"] }),
        entry("nov-clean-2", { booking_date: "2026-11-12", remittance_information: ["Synthetic November Clean B"] }),
        entry("nov-clean-3", { booking_date: "2026-11-13", remittance_information: ["Synthetic November Clean C"] }),
        entry("nov-clean-4", { booking_date: "2026-11-14", remittance_information: ["Synthetic November Clean D"] }),
      ];
      const keys = [
        "acc-secret-1/ref-secret-1",
        "acc-secret-1/ref-secret-dec",
        "acc-card-secret/ref-secret-oct",
        "acc-secret-1/ref-secret-stale",
      ];
      const api = fakeApi(novemberRows);
      api.analyticsTransactions.mockImplementation((period) => ok(entriesOf(period, novemberRows)));
      api.analyticsTransactionsByKeys.mockImplementation(() => ok(entriesOf("", provisionalRows)));

      await act(async () => {
        root.render(
          <TransactionsPanel api={api} period="2026-11" currencies={["EUR", "PLN"]}
            accounts={[...accounts, confirmedCard]} locale="en" revision={0}
            onAccessLoss={() => false} onChanged={() => undefined}
            initialCurrency="PLN"
            initialProvisionalKeys={keys}
            initialProvisionalTotal={505}
            initialProvisionalTruncated={true}
            focusOnMount={true} />,
        );
        await Promise.resolve();
      });
      await settle();

      expect(api.analyticsTransactionsByKeys).toHaveBeenCalledWith({ row_keys: keys, currency: "PLN" });
      expect(api.analyticsTransactions).not.toHaveBeenCalled();
      expect(scrollIntoView).toHaveBeenCalled();
      expect(select("Currency").value).toBe("PLN");
      const checkbox = host.querySelector<HTMLInputElement>('input[name="provisional"]');
      expect(checkbox?.checked).toBe(true);
      const entryNames = () => [...host.querySelectorAll(".budget-entry-text")].map((item) => item.textContent);
      expect(entryNames()).toEqual([
        "Synthetic Grocery",
        "Synthetic Dec Booked",
        "Synthetic Window Salary",
        "Synthetic Stale Exclusion",
      ]);
      expect(host.textContent).toContain("4 entries");
      expect(host.textContent).toContain("Showing first 4 of 505.");
      for (const secret of ["acc-secret-1", "acc-card-secret", "ref-secret-1", "ref-secret-dec", "ref-secret-oct", "ref-secret-stale"]) {
        expect(host.textContent).not.toContain(secret);
      }

      // Unchecking loads full month entries via GET and hides the truncation note; re-checking uses POST transactions-by-keys.
      await act(async () => { checkbox?.click(); await Promise.resolve(); });
      await settle();
      expect(api.analyticsTransactions).toHaveBeenLastCalledWith("2026-11", { currency: "PLN", category: "", kind: "", query: "" });
      expect(entryNames()).toHaveLength(6);
      expect(host.textContent).toContain("6 entries");
      expect(host.textContent).not.toContain("Showing first");

      await act(async () => { host.querySelector<HTMLInputElement>('input[name="provisional"]')?.click(); await Promise.resolve(); });
      await settle();
      expect(api.analyticsTransactionsByKeys).toHaveBeenCalledTimes(2);
      expect(api.analyticsTransactionsByKeys).toHaveBeenLastCalledWith({ row_keys: keys, currency: "PLN" });
      expect(entryNames()).toHaveLength(4);
    } finally {
      if (prevScroll === undefined) {
        delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
      } else {
        Element.prototype.scrollIntoView = prevScroll as typeof Element.prototype.scrollIntoView;
      }
    }
  });

  it("handles empty initialProvisionalKeys without querying empty row_keys", async () => {
    const api = fakeApi([entry("e1")]);
    await act(async () => {
      root.render(
        <TransactionsPanel api={api} period="2026-11" currencies={["PLN"]} accounts={accounts}
          locale="en" revision={0} onAccessLoss={() => false}
          onChanged={() => undefined} initialProvisionalKeys={[]} />,
      );
      await Promise.resolve();
    });
    await settle();
    expect(api.analyticsTransactions).not.toHaveBeenCalled();
    expect(api.analyticsTransactionsByKeys).not.toHaveBeenCalled();
    expect(host.textContent).toContain("No entries match.");
  });

  it("hides the provisional toggle when opened without initialProvisionalKeys", async () => {
    const api = fakeApi([entry("e1")]);
    await act(async () => {
      root.render(
        <TransactionsPanel api={api} period="2026-11" currencies={["PLN"]} accounts={accounts}
          locale="en" revision={0} onAccessLoss={() => false}
          onChanged={() => undefined} />,
      );
      await Promise.resolve();
    });
    await settle();
    expect(host.querySelector('input[name="provisional"]')).toBeNull();
    expect(host.querySelectorAll(".budget-transaction-table tbody tr")).toHaveLength(1);
  });

  it("renders inline receipt link indicators with tooltip for 1:1 and group links, and omits them when unlinked", async () => {
    const linked = entry("e-1to1", {
      receipt: {
        receipt_id: "10000000-0000-4000-8000-000000000001",
        shop_name: "Biedronka",
        total: "42.50",
        currency: "PLN",
        status: "confirmed",
      },
    });
    const grouped = entry("e-group", {
      receipt: {
        receipt_id: "10000000-0000-4000-8000-000000000011",
        shop_name: "Apple +2",
        total: "60.00",
        currency: "PLN",
        status: "needs_review",
      },
    });
    const unlinked = entry("e-unlinked", { receipt: null });

    await show(fakeApi([linked, grouped, unlinked]));
    const rows = [...host.querySelectorAll(".budget-transaction-table tbody tr")];
    expect(rows).toHaveLength(3);

    const firstBadge = rows[0]?.querySelector<HTMLButtonElement>("button.budget-tx-receipt-link");
    expect(firstBadge).not.toBeNull();
    expect(firstBadge?.type).toBe("button");
    expect(firstBadge?.getAttribute("aria-expanded")).toBe("false");
    expect(firstBadge?.textContent).toContain("🧾");
    expect(firstBadge?.textContent).toContain("Biedronka");
    expect(firstBadge?.getAttribute("title")).toContain("Biedronka");
    expect(firstBadge?.getAttribute("aria-label")).toContain("Biedronka");
    expect(firstBadge?.getAttribute("data-receipt-status")).toBe("confirmed");

    // Tapping the indicator button expands details inline (for touch viewports) without opening the row drawer.
    await click(firstBadge as HTMLButtonElement);
    expect(firstBadge?.getAttribute("aria-expanded")).toBe("true");
    expect(firstBadge?.querySelector(".budget-link-primary")?.textContent).toBe("Biedronka");
    expect(firstBadge?.querySelector(".budget-link-meta")?.textContent).toContain("42.50 PLN");
    expect(host.querySelector(".budget-entry-drawer")).toBeNull();
    await click(firstBadge as HTMLButtonElement);
    expect(firstBadge?.getAttribute("aria-expanded")).toBe("false");

    const secondBadge = rows[1]?.querySelector<HTMLButtonElement>("button.budget-tx-receipt-link");
    expect(secondBadge).not.toBeNull();
    expect(secondBadge?.textContent).toContain("🧾");
    expect(secondBadge?.textContent).toContain("Apple +2");
    expect(secondBadge?.getAttribute("title")).toContain("Apple +2");
    expect(secondBadge?.getAttribute("aria-label")).toContain("Apple +2");
    expect(secondBadge?.getAttribute("data-receipt-status")).toBe("needs_review");

    expect(rows[2]?.querySelector(".budget-tx-receipt-link")).toBeNull();
  });
});

describe("default skip-in-budget kind filter", () => {
  const rowNames = (): string[] => [...host.querySelectorAll(".budget-transaction-table tbody .budget-entry-text")]
    .map((item) => item.textContent);

  it("hides skip-in-budget kinds by default, shows the hidden count hint, and reveals them on Show or All", async () => {
    const rows = [
      entry("groceries", {
        metadata: { creditor_name: "Synthetic Grocer", debtor_name: null, mcc: null },
        effective: { kind: "expense", category_id: "cat-food", source: "default", rule_id: null },
      }),
      entry("salary", {
        credit_debit_indicator: "CRDT",
        metadata: { creditor_name: null, debtor_name: "Synthetic Employer", mcc: null },
        effective: { kind: "income", category_id: null, source: "manual", rule_id: null },
      }),
      entry("own-transfer", {
        metadata: { creditor_name: "Synthetic Own Transfer", debtor_name: null, mcc: null },
        effective: { kind: "transfer", category_id: null, source: "transfer", rule_id: null },
        transfer: { status: "accepted", decision_id: "dec-1", candidate_count: 0, ambiguous: false },
      }),
      entry("card-repay", {
        metadata: { creditor_name: "Synthetic Card Repayment", debtor_name: null, mcc: null },
        effective: { kind: "transfer", category_id: null, source: "default", rule_id: null },
      }),
      entry("manual-skip", {
        metadata: { creditor_name: "Synthetic Manual Skip", debtor_name: null, mcc: null },
        effective: { kind: "excluded", category_id: null, source: "manual", rule_id: null },
      }),
    ];
    const api = fakeApi(rows);
    await show(api, { locale: "pl" });

    // Server receives kind: "" (no contract change); skip kinds are hidden client-side.
    expect(api.analyticsTransactions).toHaveBeenCalledTimes(1);
    expect(api.analyticsTransactions).toHaveBeenCalledWith("2026-11", { currency: "", category: "", kind: "", query: "" });
    const kindSelect = select("Rodzaj");
    expect(kindSelect.value).toBe(KIND_FILTER_EXCLUDE_SKIPPED);
    expect([...kindSelect.options].slice(0, 2).map((option) => [option.value, option.textContent])).toEqual([
      [KIND_FILTER_EXCLUDE_SKIPPED, "Bez pominiętych"],
      ["", "Wszystkie"],
    ]);

    expect(rowNames()).toEqual(["Synthetic Grocer", "Synthetic Employer"]);
    expect(host.textContent).toContain("Pozycji: 2");
    expect(host.textContent).toContain("Ukryto 3 (przelewy własne / spłaty kart) · Pokaż");

    // Clicking "Pokaż" switches to "Wszystkie", reveals the hidden rows without a redundant fetch, and hides the hint.
    await click(button("Pokaż"));
    expect(api.analyticsTransactions).toHaveBeenCalledTimes(1);
    expect(select("Rodzaj").value).toBe("");
    expect(rowNames()).toEqual([
      "Synthetic Grocer",
      "Synthetic Employer",
      "Synthetic Own Transfer",
      "Synthetic Card Repayment",
      "Synthetic Manual Skip",
    ]);
    expect(host.textContent).toContain("Pozycji: 5");
    expect(host.textContent).not.toContain("Ukryto 3");

    // Switching back to "Bez pominiętych" hides them again.
    await choose(select("Rodzaj"), KIND_FILTER_EXCLUDE_SKIPPED);
    await submit(host.querySelector(".budget-range"));
    expect(rowNames()).toEqual(["Synthetic Grocer", "Synthetic Employer"]);
    expect(host.textContent).toContain("Ukryto 3 (przelewy własne / spłaty kart) · Pokaż");
  });

  it("shows the hidden-skipped hint even when every loaded entry is a skip-in-budget kind", async () => {
    const api = fakeApi([
      entry("only-transfer", {
        metadata: { creditor_name: "Synthetic Only Transfer", debtor_name: null, mcc: null },
        effective: { kind: "transfer", category_id: null, source: "transfer", rule_id: null },
      }),
    ]);
    await show(api);
    expect(rowNames()).toEqual([]);
    expect(host.textContent).toContain("No entries match.");
    expect(host.textContent).toContain("1 hidden (own transfers / card repayments) · Show");

    await click(button("Show"));
    expect(rowNames()).toEqual(["Synthetic Only Transfer"]);
    expect(host.textContent).not.toContain("1 hidden");
  });

  it("persists the selected kind filter across remounts", async () => {
    const rows = [
      entry("expense-1", { metadata: { creditor_name: "Synthetic Expense", debtor_name: null, mcc: null } }),
      entry("transfer-1", {
        metadata: { creditor_name: "Synthetic Transfer", debtor_name: null, mcc: null },
        effective: { kind: "transfer", category_id: null, source: "transfer", rule_id: null },
      }),
    ];
    const api = fakeApi(rows);
    await show(api);
    expect(rowNames()).toEqual(["Synthetic Expense"]);

    await choose(select("Type"), "");
    await submit(host.querySelector(".budget-range"));
    expect(localStorage.getItem(KIND_FILTER_STORAGE_KEY)).toBe("");
    expect(rowNames()).toEqual(["Synthetic Expense", "Synthetic Transfer"]);

    await act(async () => { root.unmount(); await Promise.resolve(); });
    root = createRoot(host);
    await show(api);
    expect(select("Type").value).toBe("");
    expect(rowNames()).toEqual(["Synthetic Expense", "Synthetic Transfer"]);
    expect(host.textContent).not.toContain("hidden");
  });

  it("still defaults to hiding skip-in-budget kinds when localStorage throws", async () => {
    vi.stubGlobal("localStorage", {
      getItem: () => { throw new Error("storage disabled"); },
      setItem: () => { throw new Error("storage disabled"); },
      removeItem: () => { throw new Error("storage disabled"); },
    });
    const rows = [
      entry("expense-1", { metadata: { creditor_name: "Synthetic Expense", debtor_name: null, mcc: null } }),
      entry("transfer-1", {
        metadata: { creditor_name: "Synthetic Transfer", debtor_name: null, mcc: null },
        effective: { kind: "transfer", category_id: null, source: "transfer", rule_id: null },
      }),
    ];
    const api = fakeApi(rows);
    await show(api);
    expect(select("Type").value).toBe(KIND_FILTER_EXCLUDE_SKIPPED);
    expect(rowNames()).toEqual(["Synthetic Expense"]);
    expect(host.textContent).toContain("1 hidden (own transfers / card repayments) · Show");

    await click(button("Show"));
    expect(select("Type").value).toBe("");
    expect(rowNames()).toEqual(["Synthetic Expense", "Synthetic Transfer"]);
  });
});
