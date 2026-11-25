import { describe, expect, it } from "vitest";
import { entry } from "./analytics/fixtures";
import { account, connection } from "./fixtures";
import type { BudgetTransaction } from "./model";
import {
  EMPTY_RECEIPT_FILTER,
  REDACTED_VALUE,
  addDecimals,
  buildConfirmRequest,
  buildReceiptListRequest,
  candidateDisplayDate,
  computeLinesDelta,
  draftFromDetail,
  filterReceiptRows,
  findCurrentLinkedCandidate,
  hasActiveReceiptFilter,
  indexAccountTransactions,
  indexAnalyticsTransactions,
  isRedactedValue,
  listMonthForFilter,
  mergeReceiptRows,
  normalizeDecimalInput,
  receiptAccountLabel,
  receiptLookupDateRange,
  receiptLookupMonths,
  sha256Hex,
  subtractDecimals,
  txLookupKey,
  validPolishNip,
  validReceiptDateRange,
} from "./receipts_model";
import type { ReceiptCandidate, ReceiptDetail, ReceiptSummary } from "./receipts_model";

function summary(overrides: Partial<ReceiptSummary> = {}): ReceiptSummary {
  return {
    receipt_id: "11111111-1111-4111-8111-111111111111",
    status: "needs_review",
    document_kind: "receipt",
    source_kind: "local_ocr",
    receipt_kind: "purchase",
    receipt_date: "2026-11-14",
    receipt_time: "12:30",
    raw_shop_name: "Synthetic Market",
    raw_nip: "5261040828",
    shop_id: null,
    chain_key: null,
    total_amount: "42.50",
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
    ...overrides,
  };
}

function detailOf(overrides: Partial<ReceiptDetail> = {}): ReceiptDetail {
  return {
    ...summary(),
    lines: [
      {
        line_no: 1,
        line_kind: "item",
        raw_name: "Milk",
        raw_name_key: "milk",
        ptu_code: "A",
        quantity: "2",
        raw_unit: "szt",
        unit_price_raw: "5.00",
        gross_line_total: "10.00",
        discount_amount: "0",
        net_line_total: "10.00",
      },
      {
        line_no: 2,
        line_kind: "item",
        raw_name: "Bread",
        raw_name_key: "bread",
        ptu_code: "A",
        quantity: "1",
        raw_unit: "szt",
        unit_price_raw: "32.50",
        gross_line_total: "32.50",
        discount_amount: "0",
        net_line_total: "32.50",
      },
    ],
    ...overrides,
  };
}

function candidateOf(overrides: Partial<ReceiptCandidate> = {}): ReceiptCandidate {
  return {
    account_id: "acc-1",
    entry_reference: sha256Hex("tx-ref-1"),
    amount: "42.50",
    currency: "PLN",
    credit_debit_indicator: "DBIT",
    booking_date: "2026-11-14",
    value_date: "2026-11-14",
    transaction_date: null,
    date_distance: 0,
    merchant_token_hit: true,
    candidate_token: "cand-token-1",
    ...overrides,
  };
}

async function subtleSha256Hex(text: string): Promise<string> {
  const bytes = new TextEncoder().encode(text);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, "0")).join("");
}

describe("receipts_model", () => {
  it("matches WebCrypto UTF-8 SHA-256 digests including non-ASCII and multi-block strings", async () => {
    const vectors = [
      "",
      "abc",
      "entry-ref-001",
      "zażółć gęślą jaźń",
      "x".repeat(200),
    ];
    for (const input of vectors) {
      expect(sha256Hex(input)).toBe(await subtleSha256Hex(input));
    }
  });

  it("identifies privacy redaction sentinel strictly", () => {
    expect(isRedactedValue(REDACTED_VALUE)).toBe(true);
    expect(isRedactedValue("Regular Shop")).toBe(false);
    expect(isRedactedValue(null)).toBe(false);
    expect(isRedactedValue(undefined)).toBe(false);
  });

  it("builds list request parameters, month narrowing, and filters search/list rows cleanly", () => {
    expect(hasActiveReceiptFilter(EMPTY_RECEIPT_FILTER)).toBe(false);
    expect(listMonthForFilter({ dateFrom: "2026-11-01", dateTo: "2026-11-30" })).toBe("2026-11");
    expect(listMonthForFilter({ dateFrom: "2026-10-25", dateTo: "2026-11-05" })).toBeUndefined();
    expect(validReceiptDateRange({ dateFrom: "2026-11-20", dateTo: "2026-11-05" })).toBe(false);

    const filter = {
      query: "",
      status: "needs_review" as const,
      documentKind: "invoice" as const,
      sourceKind: "attachment" as const,
      linked: "linked" as const,
      dateFrom: "2026-11-01",
      dateTo: "2026-11-30",
    };
    expect(hasActiveReceiptFilter(filter)).toBe(true);
    expect(buildReceiptListRequest(filter, "cur-9")).toEqual({
      limit: 50,
      month: "2026-11",
      status: "needs_review",
      document_kind: "invoice",
      linked: true,
      cursor: "cur-9",
    });

    const rows = [
      summary({ receipt_id: "r1", status: "needs_review", document_kind: "invoice", source_kind: "attachment", linked: true, receipt_date: "2026-11-10" }),
      summary({ receipt_id: "r2", status: "confirmed", document_kind: "invoice", source_kind: "attachment", linked: true, receipt_date: "2026-11-10" }),
      summary({ receipt_id: "r3", status: "needs_review", document_kind: "invoice", source_kind: "email", linked: true, receipt_date: "2026-11-10" }),
      summary({ receipt_id: "r4", status: "needs_review", document_kind: "invoice", source_kind: "attachment", linked: true, receipt_date: "2026-12-01" }),
      summary({ receipt_id: "r5", status: "needs_review", document_kind: "invoice", source_kind: "attachment", linked: true, receipt_date: null }),
    ];
    expect(filterReceiptRows(rows, filter, false).map((r) => r.receipt_id)).toEqual(["r1", "r2"]);
    expect(filterReceiptRows(rows, filter, true).map((r) => r.receipt_id)).toEqual(["r1"]);

    const first = rows[0];
    const third = rows[2];
    if (first === undefined || third === undefined) throw new Error("missing synthetic row");
    const merged = mergeReceiptRows([first], [first, third]);
    expect(merged.map((r) => r.receipt_id)).toEqual(["r1", "r3"]);
  });

  it("indexes analytics and stored account transactions by account and entry_reference digest", () => {
    const analyticsRow = entry("bank-entry-alpha", {
      account_id: "acc-1",
      amount: "42.50",
      currency: "PLN",
      credit_debit_indicator: "DBIT",
      booking_date: "2026-11-14",
      metadata: { creditor_name: "Synthetic Grocery", debtor_name: null, mcc: "5411" },
    });
    const aMap = indexAnalyticsTransactions([analyticsRow]);
    const digest = sha256Hex("bank-entry-alpha");
    expect(aMap.get(txLookupKey("acc-1", digest))).toEqual({
      accountId: "acc-1",
      entryRefDigest: digest,
      date: "2026-11-14",
      amount: "42.50",
      currency: "PLN",
      indicator: "DBIT",
      counterparty: "Synthetic Grocery",
    });

    const rawRow: BudgetTransaction = {
      entry_reference: "bank-entry-beta",
      amount: "19.99",
      currency: "PLN",
      credit_debit_indicator: "CRDT",
      status: "BOOK",
      booking_date: null,
      value_date: "2026-11-02",
      transaction_date: "2026-11-01",
      remittance_information: ["Refund Synthetic"],
      source: "api",
      observed_at: 1,
      api_observed_at: 1,
    };
    const rawTxOnly: BudgetTransaction = {
      ...rawRow,
      entry_reference: "bank-entry-gamma",
      value_date: null,
    };
    const tMap = indexAccountTransactions("acc-2", [rawRow, rawTxOnly]);
    expect(tMap.get(txLookupKey("acc-2", sha256Hex("bank-entry-beta")))).toMatchObject({
      date: "2026-11-02",
      counterparty: "Refund Synthetic",
      indicator: "CRDT",
    });
    expect(tMap.get(txLookupKey("acc-2", sha256Hex("bank-entry-gamma")))).toMatchObject({
      date: "2026-11-01",
    });

    expect(receiptLookupMonths("2026-11-03")).toEqual(["2026-10", "2026-11"]);
    expect(receiptLookupMonths("2026-11-15")).toEqual(["2026-11"]);
    expect(receiptLookupDateRange("2026-11-03")).toEqual({ dateFrom: "2026-10-27", dateTo: "2026-11-10" });
    expect(candidateDisplayDate(candidateOf({ booking_date: null, value_date: "2026-11-13" }))).toBe("2026-11-13");

    const acc = account({
      id: "acc-1",
      connection: connection.id,
      display: { product: "Main", currency: "PLN", masked_suffix: "1234", source: "api", observed_at: 1, imported_at: 1 },
    });
    expect(receiptAccountLabel([acc], [connection], "acc-1", "Account")).toContain("Main");
    expect(receiptAccountLabel([acc], [connection], null, "Account")).toBe("Account");
  });

  it("validates Polish NIP checksums and performs exact BigInt decimal arithmetic", () => {
    expect(validPolishNip("5261040828")).toBe(true);
    expect(validPolishNip("5261040829")).toBe(false);
    expect(validPolishNip("1234567890")).toBe(false);
    expect(validPolishNip("123")).toBe(false);

    expect(normalizeDecimalInput(" 12,3000 ")).toBe("12.3");
    expect(normalizeDecimalInput("-0.50")).toBe("-0.5");
    expect(normalizeDecimalInput("12.34567")).toBeNull();
    expect(normalizeDecimalInput("abc")).toBeNull();

    expect(addDecimals(["0.1", "0.2"])).toBe("0.3");
    expect(subtractDecimals("10.00", "2.55")).toBe("7.45");
    expect(computeLinesDelta(detailOf().lines, "42.50")).toBe("0");
    expect(computeLinesDelta(detailOf().lines, "40.00")).toBe("2.5");
  });

  it("builds valid confirm requests and rejects mismatched math, invalid fields, and cash links", () => {
    const linkedDigest = sha256Hex("tx-ref-1");
    const cand = candidateOf({ entry_reference: linkedDigest });
    const linkedDetail = detailOf({
      linked: true,
      linked_account_id: "acc-1",
      linked_entry_ref: linkedDigest,
    });
    expect(findCurrentLinkedCandidate(linkedDetail, [cand])).toEqual(cand);

    const draftKeep = draftFromDetail(linkedDetail, [cand]);
    expect(draftKeep.linkChoice).toBe("keep");
    const builtKeep = buildConfirmRequest(linkedDetail, draftKeep, [cand]);
    expect(builtKeep.ok).toBe(true);
    if (builtKeep.ok) {
      expect(builtKeep.request.link).toEqual({
        account_id: "acc-1",
        entry_reference: linkedDigest,
        candidate_token: "cand-token-1",
      });
    }

    // Confirmed or linked detail without candidates cannot submit "keep" (must choose unlink or have candidates).
    const noCandKeep = draftFromDetail(linkedDetail, []);
    expect(buildConfirmRequest(linkedDetail, noCandKeep, [])).toEqual({
      ok: false,
      errorKey: "budget.receipts.invalidForm",
    });

    // Unlink explicitly emits link: null.
    const builtUnlink = buildConfirmRequest(linkedDetail, { ...draftKeep, linkChoice: "unlink" }, [cand]);
    expect(builtUnlink.ok).toBe(true);
    if (builtUnlink.ok) expect(builtUnlink.request.link).toBeNull();

    // Cash payment drops link cleanly.
    const cashBuilt = buildConfirmRequest(linkedDetail, { ...draftKeep, paymentMethod: "cash" }, [cand]);
    expect(cashBuilt.ok).toBe(true);
    if (cashBuilt.ok) expect(cashBuilt.request.link).toBeNull();

    // Total mismatch rejected before network call.
    const mismatchBuilt = buildConfirmRequest(linkedDetail, { ...draftKeep, totalAmount: "99.99" }, [cand]);
    expect(mismatchBuilt).toEqual({ ok: false, errorKey: "budget.receipts.totalMismatch" });

    // Invalid NIP rejected.
    const badNip = buildConfirmRequest(linkedDetail, { ...draftKeep, rawNip: "5261040829" }, [cand]);
    expect(badNip).toEqual({ ok: false, errorKey: "budget.receipts.invalidForm" });
  });
});
