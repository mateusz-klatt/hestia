import { describe, expect, it } from "vitest";
import { account, connection } from "./fixtures";
import {
  EMPTY_RECEIPT_FILTER,
  REDACTED_VALUE,
  addDecimals,
  availableLinkOptions,
  buildConfirmRequest,
  buildReceiptListRequest,
  candidateDisplayDate,
  computeLinesDelta,
  draftFromDetail,
  filterReceiptRows,
  findCurrentLinkedCandidate,
  hasActiveReceiptFilter,
  hasClientSideFilter,
  isRedactedValue,
  isStaleCursorError,
  listMonthForFilter,
  mergeReceiptRows,
  normalizeDecimalInput,
  receiptAccountLabel,
  subtractDecimals,
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
    entry_reference: "a".repeat(64),
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

describe("receipts_model", () => {
  it("identifies privacy redaction sentinel strictly", () => {
    expect(isRedactedValue(REDACTED_VALUE)).toBe(true);
    expect(isRedactedValue("Regular Shop")).toBe(false);
    expect(isRedactedValue(null)).toBe(false);
    expect(isRedactedValue(undefined)).toBe(false);
  });

  it("builds list request parameters, detects client-side filters & stale cursors, and filters rows cleanly", () => {
    expect(hasActiveReceiptFilter(EMPTY_RECEIPT_FILTER)).toBe(false);
    expect(hasClientSideFilter(EMPTY_RECEIPT_FILTER)).toBe(false);
    expect(hasClientSideFilter({ ...EMPTY_RECEIPT_FILTER, status: "confirmed" })).toBe(false);
    expect(hasClientSideFilter({ ...EMPTY_RECEIPT_FILTER, query: "Milk", status: "confirmed" })).toBe(true);
    expect(hasClientSideFilter({ ...EMPTY_RECEIPT_FILTER, sourceKind: "attachment" })).toBe(true);
    expect(hasClientSideFilter({ ...EMPTY_RECEIPT_FILTER, dateFrom: "2026-11-01" })).toBe(true);

    expect(isStaleCursorError({ ok: false, status: 400, error: "budget_receipt_invalid_request" })).toBe(true);
    expect(isStaleCursorError({ ok: false, status: 400, error: "budget_http_400" })).toBe(true);
    expect(isStaleCursorError({ ok: false, status: 404, error: "budget_receipt_invalid_request" })).toBe(false);
    expect(isStaleCursorError({ ok: true })).toBe(false);

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

  it("enforces backend per-status link transitions for draft, imported, needs_review and confirmed", () => {
    const linkedDigest = "b".repeat(64);
    const cand = candidateOf({ entry_reference: linkedDigest });

    // 1. needs_review + linked + matching candidate -> keep, candidate, or unlink.
    const reviewLinked = detailOf({
      status: "needs_review",
      linked: true,
      linked_account_id: "acc-1",
      linked_entry_ref: linkedDigest,
    });
    expect(findCurrentLinkedCandidate(reviewLinked, [cand])).toEqual(cand);
    expect(availableLinkOptions(reviewLinked, [cand], "card")).toEqual({
      paymentAllowsLink: true,
      canKeep: true,
      canPickCandidates: true,
      mustUnlinkToConfirm: false,
      currentCandidate: cand,
    });
    const draftKeep = draftFromDetail(reviewLinked, [cand]);
    expect(draftKeep.linkChoice).toBe("keep");
    const builtKeep = buildConfirmRequest(reviewLinked, draftKeep, [cand]);
    expect(builtKeep.ok).toBe(true);
    if (builtKeep.ok) {
      expect(builtKeep.request.link).toEqual({
        account_id: "acc-1",
        entry_reference: linkedDigest,
        candidate_token: "cand-token-1",
      });
    }

    // Switching to unlink emits link: null.
    const builtUnlink = buildConfirmRequest(reviewLinked, { ...draftKeep, linkChoice: "unlink" }, [cand]);
    expect(builtUnlink.ok).toBe(true);
    if (builtUnlink.ok) expect(builtUnlink.request.link).toBeNull();

    // Cash or mixed payment disables links cleanly.
    expect(availableLinkOptions(reviewLinked, [cand], "mixed").paymentAllowsLink).toBe(false);
    const cashBuilt = buildConfirmRequest(reviewLinked, { ...draftKeep, paymentMethod: "cash" }, [cand]);
    expect(cashBuilt.ok).toBe(true);
    if (cashBuilt.ok) expect(cashBuilt.request.link).toBeNull();

    // 2. confirmed + linked -> only unlink is valid (no candidates/keep).
    const confirmedLinked = detailOf({
      status: "confirmed",
      linked: true,
      linked_account_id: "acc-1",
      linked_entry_ref: linkedDigest,
    });
    expect(availableLinkOptions(confirmedLinked, [cand], "card")).toEqual({
      paymentAllowsLink: true,
      canKeep: false,
      canPickCandidates: false,
      mustUnlinkToConfirm: true,
      currentCandidate: null,
    });
    const confirmedDraft = draftFromDetail(confirmedLinked, [cand]);
    expect(confirmedDraft.linkChoice).toBe("unlink");
    expect(buildConfirmRequest(confirmedLinked, { ...confirmedDraft, linkChoice: "keep" }, [cand])).toEqual({
      ok: false,
      errorKey: "budget.receipts.invalidForm",
    });
    expect(buildConfirmRequest(confirmedLinked, { ...confirmedDraft, linkChoice: "candidate:0" }, [cand])).toEqual({
      ok: false,
      errorKey: "budget.receipts.invalidForm",
    });
    const confirmedBuilt = buildConfirmRequest(confirmedLinked, confirmedDraft, [cand]);
    expect(confirmedBuilt.ok).toBe(true);
    if (confirmedBuilt.ok) expect(confirmedBuilt.request.link).toBeNull();

    // 3. draft & imported -> never pick candidates; defaults to link: null.
    for (const status of ["draft", "imported"] as const) {
      const unlinkedImport = detailOf({ status, linked: false });
      expect(availableLinkOptions(unlinkedImport, [cand], "card")).toEqual({
        paymentAllowsLink: true,
        canKeep: false,
        canPickCandidates: false,
        mustUnlinkToConfirm: false,
        currentCandidate: null,
      });
      const draftImport = draftFromDetail(unlinkedImport, [cand]);
      expect(draftImport.linkChoice).toBe("unlink");
      expect(buildConfirmRequest(unlinkedImport, { ...draftImport, linkChoice: "candidate:0" }, [cand])).toEqual({
        ok: false,
        errorKey: "budget.receipts.invalidForm",
      });
      const builtImport = buildConfirmRequest(unlinkedImport, draftImport, [cand]);
      expect(builtImport.ok).toBe(true);
      if (builtImport.ok) expect(builtImport.request.link).toBeNull();
    }

    // 4. Total mismatch & invalid NIP rejected before network calls.
    expect(buildConfirmRequest(reviewLinked, { ...draftKeep, totalAmount: "99.99" }, [cand])).toEqual({
      ok: false,
      errorKey: "budget.receipts.totalMismatch",
    });
    expect(buildConfirmRequest(reviewLinked, { ...draftKeep, rawNip: "5261040829" }, [cand])).toEqual({
      ok: false,
      errorKey: "budget.receipts.invalidForm",
    });
  });
});
