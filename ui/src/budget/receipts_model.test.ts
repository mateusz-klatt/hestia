import { describe, expect, it } from "vitest";
import { account, connection } from "./fixtures";
import {
  EMPTY_RECEIPT_FILTER,
  REDACTED_VALUE,
  addDecimals,
  availableLinkOptions,
  buildBulkSelectionPlan,
  buildConfirmRequest,
  buildPlainBulkConfirmRequest,
  buildReceiptListRequest,
  candidateDisplayDate,
  computeLinesDelta,
  createPendingReceiptStore,
  draftFromDetail,
  editReceiptLine,
  filterReceiptRows,
  findCurrentLinkedCandidate,
  formatBulkErrorBucket,
  hasActiveReceiptFilter,
  hasClientSideFilter,
  hasMatchingFieldsChanged,
  haveConfirmLinesChanged,
  isBulkConfirmable,
  isExcludedDraftForBulk,
  isGmailImportSource,
  isRedactedValue,
  isStaleCursorError,
  listMonthForFilter,
  merchantBulkGroups,
  mergeReceiptRows,
  normalizeDecimalInput,
  receiptAccountLabel,
  receiptHasRedactedLines,
  receiptMerchantGroupIdentity,
  receiptMerchantGroupKey,
  reconcileLinkChoice,
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
    gateway: null,
    gateway_status: null,
    fee_amount: null,
    payee_host: null,
    evidence_for_receipt_id: null,
    has_document: true,
    created_at: 1_700_000_000,
    updated_at: 1_700_000_000,
    ...overrides,
  };
}

function detailOf(overrides: Partial<ReceiptDetail> = {}): ReceiptDetail {
  return {
    ...summary(),
    payment_evidence: [],
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
    match_strength: "none",
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

  it("enforces per-status link rules, prevents implicit unlinks, and disables stale tokens when matching fields change", () => {
    const linkedDigest = "b".repeat(64);
    const cand = candidateOf({ entry_reference: linkedDigest });

    // 1. needs_review + linked + matching candidate -> keep, candidate, or explicit unlink.
    const reviewLinked = detailOf({
      status: "needs_review",
      linked: true,
      linked_account_id: "acc-1",
      linked_entry_ref: linkedDigest,
    });
    expect(findCurrentLinkedCandidate(reviewLinked, [cand])).toEqual(cand);
    expect(availableLinkOptions(reviewLinked, [cand], "card")).toEqual({
      paymentAllowsLink: true,
      matchingFieldsChanged: false,
      canKeep: true,
      canPickCandidates: true,
      mustUnlinkToConfirm: false,
      currentCandidate: cand,
    });
    const draftReview = draftFromDetail(reviewLinked, [cand]);
    expect(draftReview.linkChoice).toBe("");
    expect(buildConfirmRequest(reviewLinked, draftReview, [cand])).toEqual({
      ok: false,
      errorKey: "budget.receipts.explicitUnlinkRequired",
    });
    const draftKeep = { ...draftReview, linkChoice: "keep" };
    const builtKeep = buildConfirmRequest(reviewLinked, draftKeep, [cand]);
    expect(builtKeep.ok).toBe(true);
    if (builtKeep.ok) {
      expect(builtKeep.request.link).toEqual({
        account_id: "acc-1",
        entry_reference: linkedDigest,
        candidate_token: "cand-token-1",
      });
    }

    // 2. Linked needs_review WITHOUT matching keep candidate never defaults to implicit unlink.
    const reviewLinkedNoKeep = detailOf({
      status: "needs_review",
      linked: true,
      linked_account_id: "acc-1",
      linked_entry_ref: "c".repeat(64),
    });
    const noKeepDraft = draftFromDetail(reviewLinkedNoKeep, [cand]);
    expect(noKeepDraft.linkChoice).toBe("");
    expect(buildConfirmRequest(reviewLinkedNoKeep, noKeepDraft, [cand])).toEqual({
      ok: false,
      errorKey: "budget.receipts.explicitUnlinkRequired",
    });
    const explicitUnlink = buildConfirmRequest(reviewLinkedNoKeep, { ...noKeepDraft, linkChoice: "unlink" }, [cand]);
    expect(explicitUnlink.ok).toBe(true);
    if (explicitUnlink.ok) expect(explicitUnlink.request.link).toBeNull();

    // 3. Switching payment method to cash on a linked receipt must still require explicit unlink (not auto-drop link).
    expect(availableLinkOptions(reviewLinked, [cand], "mixed").paymentAllowsLink).toBe(false);
    expect(buildConfirmRequest(reviewLinked, { ...draftKeep, paymentMethod: "cash" }, [cand])).toEqual({
      ok: false,
      errorKey: "budget.receipts.invalidForm",
    });
    const explicitCashUnlink = buildConfirmRequest(
      reviewLinked,
      { ...draftKeep, paymentMethod: "cash", linkChoice: "unlink" },
      [cand],
    );
    expect(explicitCashUnlink.ok).toBe(true);
    if (explicitCashUnlink.ok) expect(explicitCashUnlink.request.link).toBeNull();

    // 4. Editing merchant, NIP, or amount invalidates matching fields & rejects stale tokens without implicit unlink.
    expect(hasMatchingFieldsChanged(reviewLinked, draftKeep)).toBe(false);
    expect(hasMatchingFieldsChanged(reviewLinked, { ...draftKeep, totalAmount: "42.5" })).toBe(false);
    expect(hasMatchingFieldsChanged(reviewLinked, { ...draftKeep, rawShopName: "Changed Store" })).toBe(true);
    expect(hasMatchingFieldsChanged(reviewLinked, { ...draftKeep, rawNip: "5261040829" })).toBe(true);
    expect(hasMatchingFieldsChanged(reviewLinked, { ...draftKeep, totalAmount: "50.00" })).toBe(true);
    expect(availableLinkOptions(reviewLinked, [cand], "card", true)).toEqual({
      paymentAllowsLink: true,
      matchingFieldsChanged: true,
      canKeep: false,
      canPickCandidates: false,
      mustUnlinkToConfirm: true,
      currentCandidate: cand,
    });
    expect(reconcileLinkChoice(reviewLinked, "keep", { ...draftKeep, rawShopName: "Changed Store" }, [cand])).toBe("");
    const unlinkedReview = detailOf({ status: "needs_review", linked: false });
    expect(draftFromDetail(unlinkedReview, [cand]).linkChoice).toBe("");
    expect(reconcileLinkChoice(unlinkedReview, "candidate:0", { ...draftKeep, rawNip: "" }, [cand])).toBe("");
    expect(buildConfirmRequest(reviewLinked, { ...draftKeep, rawShopName: "Changed Store" }, [cand])).toEqual({
      ok: false,
      errorKey: "budget.receipts.invalidForm",
    });

    // 5. confirmed + linked -> defaults to "", only explicit unlink is accepted.
    const confirmedLinked = detailOf({
      status: "confirmed",
      linked: true,
      linked_account_id: "acc-1",
      linked_entry_ref: linkedDigest,
    });
    const confirmedDraft = draftFromDetail(confirmedLinked, [cand]);
    expect(confirmedDraft.linkChoice).toBe("");
    expect(buildConfirmRequest(confirmedLinked, confirmedDraft, [cand])).toEqual({
      ok: false,
      errorKey: "budget.receipts.explicitUnlinkRequired",
    });
    expect(buildConfirmRequest(confirmedLinked, { ...confirmedDraft, linkChoice: "keep" }, [cand])).toEqual({
      ok: false,
      errorKey: "budget.receipts.invalidForm",
    });
    const confirmedBuilt = buildConfirmRequest(confirmedLinked, { ...confirmedDraft, linkChoice: "unlink" }, [cand]);
    expect(confirmedBuilt.ok).toBe(true);
    if (confirmedBuilt.ok) expect(confirmedBuilt.request.link).toBeNull();

    // 6. unlinked draft & imported -> offer candidates, default to "" until user explicitly picks unlink or a candidate.
    for (const status of ["draft", "imported"] as const) {
      const unlinkedImport = detailOf({ status, linked: false });
      expect(availableLinkOptions(unlinkedImport, [cand], "card").canPickCandidates).toBe(true);
      const draftImport = draftFromDetail(unlinkedImport, [cand]);
      expect(draftImport.linkChoice).toBe("");
      expect(buildConfirmRequest(unlinkedImport, draftImport, [cand])).toEqual({
        ok: false,
        errorKey: "budget.receipts.explicitUnlinkRequired",
      });
      const builtExplicitUnlinked = buildConfirmRequest(unlinkedImport, { ...draftImport, linkChoice: "unlink" }, [cand]);
      expect(builtExplicitUnlinked.ok).toBe(true);
      if (builtExplicitUnlinked.ok) expect(builtExplicitUnlinked.request.link).toBeNull();

      const builtExplicitLinked = buildConfirmRequest(unlinkedImport, { ...draftImport, linkChoice: "candidate:0" }, [cand]);
      expect(builtExplicitLinked.ok).toBe(true);
      if (builtExplicitLinked.ok) {
        expect(builtExplicitLinked.request.link).toEqual({
          account_id: cand.account_id,
          entry_reference: cand.entry_reference,
          candidate_token: cand.candidate_token,
        });
      }
      expect(reconcileLinkChoice(unlinkedImport, "candidate:0", { ...draftImport, rawShopName: "Changed" }, [cand])).toBe("");

      const noCandidatesDraft = draftFromDetail(unlinkedImport, []);
      expect(noCandidatesDraft.linkChoice).toBe("unlink");
      const builtNoCand = buildConfirmRequest(unlinkedImport, noCandidatesDraft, []);
      expect(builtNoCand.ok).toBe(true);
      if (builtNoCand.ok) expect(builtNoCand.request.link).toBeNull();
    }

    // 7. Total mismatch & invalid NIP rejected before network calls.
    expect(buildConfirmRequest(reviewLinked, { ...draftKeep, totalAmount: "99.99" }, [cand])).toEqual({
      ok: false,
      errorKey: "budget.receipts.totalMismatch",
    });
    expect(buildConfirmRequest(reviewLinked, { ...draftKeep, rawNip: "5261040829" }, [cand])).toEqual({
      ok: false,
      errorKey: "budget.receipts.invalidForm",
    });
  });

  it("tracks persistent pending receipt locks and settled notifications cleanly", () => {
    const store = createPendingReceiptStore();
    const events: unknown[] = [];
    const unsubscribe = store.subscribe((ev) => { events.push(ev ?? null); });

    const tok1 = store.tryAcquire("r-1", "confirm");
    expect(tok1).not.toBeNull();
    expect(store.get("r-1")).toBe("confirm");
    expect(store.tryAcquire("r-1", "confirm")).toBeNull();

    if (tok1 === null) throw new Error("expected lock token");
    store.release("r-1", Symbol("wrong"));
    expect(store.get("r-1")).toBe("confirm");

    store.release("r-1", tok1, "confirmed");
    expect(store.get("r-1")).toBeNull();
    expect(events).toEqual([null, { receiptId: "r-1", kind: "confirm", outcome: "confirmed" }]);

    unsubscribe();
  });

  it("preserves gross, discount, and net consistency across line edits and no-op decimal reformatting", () => {
    const itemLine = {
      line_kind: "item" as const,
      raw_name: "Coffee",
      ptu_code: "A" as const,
      quantity: "1",
      raw_unit: "szt",
      unit_price_raw: "10.00",
      gross_line_total: "10.00",
      discount_amount: "1.00",
      net_line_total: "9.00",
    };

    // Pure reformat of net ("9.00" -> "9," -> "9,00") must not touch gross or discount.
    const typingComma = editReceiptLine(itemLine, "net_line_total", "9,");
    expect(typingComma.gross_line_total).toBe("10.00");
    expect(typingComma.discount_amount).toBe("1.00");

    const reformatted = editReceiptLine(typingComma, "net_line_total", "9,00");
    expect(reformatted.gross_line_total).toBe("10.00");
    expect(reformatted.discount_amount).toBe("1.00");
    expect(reformatted.net_line_total).toBe("9,00");
    expect(computeLinesDelta([reformatted], "9.00")).toBe("0");

    // True edit to net keeps discount untouched and adjusts gross = net + discount.
    const editedNet = editReceiptLine(itemLine, "net_line_total", "8.00");
    expect(editedNet.gross_line_total).toBe("9");
    expect(editedNet.discount_amount).toBe("1.00");
    expect(editedNet.net_line_total).toBe("8.00");

    // Edit to gross keeps discount untouched and updates net = gross - discount.
    const editedGross = editReceiptLine(itemLine, "gross_line_total", "12.00");
    expect(editedGross.gross_line_total).toBe("12.00");
    expect(editedGross.discount_amount).toBe("1.00");
    expect(editedGross.net_line_total).toBe("11");

    // Edit to discount keeps gross untouched and updates net = gross - discount.
    const editedDiscount = editReceiptLine(itemLine, "discount_amount", "2.50");
    expect(editedDiscount.gross_line_total).toBe("10.00");
    expect(editedDiscount.discount_amount).toBe("2.50");
    expect(editedDiscount.net_line_total).toBe("7.5");

    // Confirm omits lines when net was only reformatted (or unedited) so stored/redacted lines never overwrite DB.
    const rec = detailOf({ total_amount: "9.00", lines: [{ ...itemLine, line_no: 1, raw_name_key: "coffee" }] });
    expect(haveConfirmLinesChanged(rec.lines, [reformatted])).toBe(false);
    const draft = { ...draftFromDetail(rec, []), lines: [reformatted] };
    const built = buildConfirmRequest(rec, draft, []);
    expect(built.ok).toBe(true);
    if (built.ok) {
      expect("lines" in built.request).toBe(false);
    }

    // Confirming an unedited receipt with redacted lines keeps stored lines by omitting `lines`;
    // attempting to submit mutated redacted lines or typing the placeholder is rejected.
    const redactedRec = detailOf({
      total_amount: "9.00",
      lines: [{ ...itemLine, line_no: 1, raw_name: REDACTED_VALUE, raw_name_key: "[usunieto]", raw_unit: REDACTED_VALUE }],
    });
    expect(receiptHasRedactedLines(redactedRec.lines)).toBe(true);
    const builtRedacted = buildConfirmRequest(redactedRec, draftFromDetail(redactedRec, []), []);
    expect(builtRedacted.ok).toBe(true);
    if (builtRedacted.ok) {
      expect("lines" in builtRedacted.request).toBe(false);
    }
    expect(buildConfirmRequest(redactedRec, {
      ...draftFromDetail(redactedRec, []),
      lines: [{ ...itemLine, raw_name: "Edited" }],
    }, [])).toEqual({ ok: false, errorKey: "budget.receipts.redactedLinesEditBlocked" });
    expect(buildConfirmRequest(rec, {
      ...draftFromDetail(rec, []),
      lines: [{ ...itemLine, raw_name: REDACTED_VALUE }],
    }, [])).toEqual({ ok: false, errorKey: "budget.receipts.redactedLinesEditBlocked" });

    // True edit sends normalized `lines`.
    const builtEdited = buildConfirmRequest(rec, { ...draftFromDetail(rec, []), totalAmount: "11.00", lines: [editedGross] }, []);
    expect(builtEdited.ok).toBe(true);
    if (builtEdited.ok) {
      expect(builtEdited.request.lines?.[0]).toMatchObject({
        gross_line_total: "12",
        discount_amount: "1",
        net_line_total: "11",
      });
    }

    // Non-item lines enforce discount="0" and net=gross.
    const otherLine = { ...itemLine, line_kind: "other" as const, discount_amount: "0", net_line_total: "10.00" };
    const editedOther = editReceiptLine(otherLine, "net_line_total", "15.00");
    expect(editedOther.gross_line_total).toBe("15");
    expect(editedOther.discount_amount).toBe("0");
    expect(editedOther.net_line_total).toBe("15.00");
  });

  it("restricts bulk confirm to Gmail imports, groups by NIP or normalized name, and omits lines on confirm", () => {
    expect(isGmailImportSource("email")).toBe(true);
    expect(isGmailImportSource("attachment")).toBe(true);
    expect(isGmailImportSource("local_ocr")).toBe(false);
    expect(isGmailImportSource("paste")).toBe(false);
    expect(isGmailImportSource("live_text")).toBe(false);

    const rows = [
      summary({ receipt_id: "r-imp-inv", status: "imported", source_kind: "attachment", document_kind: "invoice", raw_shop_name: "Alpha Ltd", raw_nip: "7791011327", linked: false }),
      summary({ receipt_id: "r-imp-pay", status: "imported", source_kind: "email", document_kind: "payment_confirmation", raw_shop_name: "Alpha Ltd", raw_nip: "7791011327", linked: false }),
      summary({ receipt_id: "r-imp-other-nip", status: "imported", source_kind: "email", document_kind: "payment_confirmation", raw_shop_name: "Alpha Ltd", raw_nip: "5261040828", linked: false }),
      summary({ receipt_id: "r-imp-nonip-1", status: "imported", source_kind: "email", document_kind: "payment_confirmation", raw_shop_name: "  Cafe   Noir ", raw_nip: null, linked: false }),
      summary({ receipt_id: "r-imp-nonip-2", status: "imported", source_kind: "email", document_kind: "payment_confirmation", raw_shop_name: "cafe noir", raw_nip: null, linked: false }),
      summary({ receipt_id: "r-rev", status: "needs_review", source_kind: "email", raw_shop_name: "Alpha Ltd", raw_nip: "7791011327", linked: false }),
      summary({ receipt_id: "r-draft-ocr", status: "draft", source_kind: "local_ocr", raw_shop_name: "OCR Draft", raw_nip: null, linked: false }),
      summary({ receipt_id: "r-draft-paste", status: "draft", source_kind: "paste", raw_shop_name: "Paste Draft", raw_nip: null, linked: false }),
      summary({ receipt_id: "r-draft-live", status: "draft", source_kind: "live_text", raw_shop_name: "Live Draft", raw_nip: null, linked: false }),
      summary({ receipt_id: "r-conf", status: "confirmed", source_kind: "email", raw_shop_name: "Alpha Ltd", raw_nip: "7791011327", linked: true }),
    ];

    expect(rows.map((r) => isBulkConfirmable(r))).toEqual([true, true, true, true, true, false, false, false, false, false]);
    expect(rows.map((r) => isExcludedDraftForBulk(r))).toEqual([false, false, false, false, false, false, true, true, true, false]);
    expect(receiptMerchantGroupKey(summary({ raw_shop_name: REDACTED_VALUE, raw_nip: null, forwarded_sender_domain: "billing.example" }))).toBe("billing.example");
    expect(receiptMerchantGroupKey(summary({ raw_shop_name: REDACTED_VALUE, raw_nip: null, forwarded_sender_domain: null }))).toBeNull();
    expect(receiptMerchantGroupIdentity(summary({ raw_shop_name: REDACTED_VALUE, raw_nip: "7791011327", forwarded_sender_domain: null }))).toEqual({
      key: "nip:7791011327",
      label: "NIP 7791011327",
    });

    // Two sellers with the same name but different NIPs never merge; same normalized name without NIP merges.
    expect(merchantBulkGroups(rows)).toEqual([
      { key: "nip:7791011327", merchant: "Alpha Ltd (NIP 7791011327)", confirmableIds: ["r-imp-inv", "r-imp-pay"], excludedNeedsReview: 1, excludedDrafts: 0 },
      { key: "nip:5261040828", merchant: "Alpha Ltd (NIP 5261040828)", confirmableIds: ["r-imp-other-nip"], excludedNeedsReview: 0, excludedDrafts: 0 },
      { key: "name:cafe noir", merchant: "Cafe   Noir".replace("   ", "   "), confirmableIds: ["r-imp-nonip-1", "r-imp-nonip-2"], excludedNeedsReview: 0, excludedDrafts: 0 },
    ]);

    const plan = buildBulkSelectionPlan(rows, new Set(["r-imp-inv", "r-imp-other-nip", "r-rev", "r-draft-ocr", "r-draft-paste", "r-draft-live", "r-conf"]), "Unknown");
    expect(plan.confirmable.map((r) => r.receipt_id)).toEqual(["r-imp-inv", "r-imp-other-nip"]);
    expect(plan.excludedNeedsReview).toBe(1);
    expect(plan.excludedDrafts).toBe(3);
    expect(plan.merchants).toEqual(["Alpha Ltd (NIP 7791011327)", "Alpha Ltd (NIP 5261040828)"]);
    expect(formatBulkErrorBucket(409, "budget_receipt_conflict")).toBe("409 budget_receipt_conflict");

    // Gmail invoice with 0 lines gets 1 summary line and link: null.
    const invoiceDetail = detailOf({
      receipt_id: "r-imp-inv",
      status: "imported",
      source_kind: "attachment",
      document_kind: "invoice",
      invoice_number: "FV/2026/05",
      payment_method: null,
      total_amount: "99.50",
      lines: [],
      linked: false,
      version: 4,
    });
    const builtInv = buildPlainBulkConfirmRequest(invoiceDetail, "Summary");
    expect(builtInv).toEqual({
      ok: true,
      request: expect.objectContaining({
        receipt_id: "r-imp-inv",
        expected_version: 4,
        payment_method: "unknown",
        total_amount: "99.5",
        link: null,
        lines: [expect.objectContaining({
          line_kind: "other",
          raw_name: "FV/2026/05",
          gross_line_total: "99.5",
          discount_amount: "0",
          net_line_total: "99.5",
        }) as unknown],
      }) as unknown,
    });

    // Gmail payment_confirmation (even with redacted line) omits `lines` cleanly on confirm.
    const paymentDetail = detailOf({
      receipt_id: "r-imp-pay",
      status: "imported",
      source_kind: "email",
      document_kind: "payment_confirmation",
      payment_method: null,
      total_amount: "30.00",
      lines: [{
        line_no: 1,
        line_kind: "other",
        raw_name: REDACTED_VALUE,
        raw_name_key: "[usunieto]",
        ptu_code: null,
        quantity: "1",
        raw_unit: null,
        unit_price_raw: null,
        gross_line_total: "30.00",
        discount_amount: "0",
        net_line_total: "30.00",
      }],
      linked: false,
      version: 2,
    });
    const builtPay = buildPlainBulkConfirmRequest(paymentDetail, "Summary");
    expect(builtPay.ok).toBe(true);
    if (builtPay.ok) {
      expect("lines" in builtPay.request).toBe(false);
      expect("raw_shop_name" in builtPay.request).toBe(false);
      expect("raw_nip" in builtPay.request).toBe(false);
    }

    // Redacted header fields (raw_shop_name / raw_nip) are omitted on single & bulk confirm, never echoed back,
    // while explicit edits send new values / null and manually entered "[usunięto]" is rejected.
    const redactedShopDetail = detailOf({
      status: "imported",
      source_kind: "email",
      document_kind: "payment_confirmation",
      raw_shop_name: REDACTED_VALUE,
      raw_nip: REDACTED_VALUE,
      payment_method: null,
      total_amount: "30.00",
      lines: [{
        line_no: 1,
        line_kind: "other",
        raw_name: REDACTED_VALUE,
        raw_name_key: "[usunieto]",
        ptu_code: null,
        quantity: "1",
        raw_unit: null,
        unit_price_raw: null,
        gross_line_total: "30.00",
        discount_amount: "0",
        net_line_total: "30.00",
      }],
    });
    const builtRedactedBulk = buildPlainBulkConfirmRequest(redactedShopDetail, "Summary");
    expect(builtRedactedBulk.ok).toBe(true);
    if (builtRedactedBulk.ok) {
      expect("raw_shop_name" in builtRedactedBulk.request).toBe(false);
      expect("raw_nip" in builtRedactedBulk.request).toBe(false);
      expect("lines" in builtRedactedBulk.request).toBe(false);
    }
    const shopEditedToRedacted = buildConfirmRequest(detailOf({ raw_shop_name: "Safe Shop" }), {
      ...draftFromDetail(detailOf({ raw_shop_name: "Safe Shop" }), []),
      rawShopName: REDACTED_VALUE,
    }, []);
    expect(shopEditedToRedacted).toEqual({ ok: false, errorKey: "budget.receipts.invalidForm" });
    const nipEditedToRedacted = buildConfirmRequest(detailOf({ raw_nip: "7791011327" }), {
      ...draftFromDetail(detailOf({ raw_nip: "7791011327" }), []),
      rawNip: REDACTED_VALUE,
    }, []);
    expect(nipEditedToRedacted).toEqual({ ok: false, errorKey: "budget.receipts.invalidForm" });
    const shopCleared = buildConfirmRequest(redactedShopDetail, {
      ...draftFromDetail(redactedShopDetail, []),
      rawShopName: "",
    }, []);
    expect(shopCleared.ok).toBe(true);
    if (shopCleared.ok) {
      expect(shopCleared.request.raw_shop_name).toBeNull();
      expect("raw_nip" in shopCleared.request).toBe(false);
    }

    // Zero-line OCR draft & needs_review are rejected (never synthesize lines for non-invoices).
    expect(buildPlainBulkConfirmRequest(detailOf({ status: "draft", source_kind: "local_ocr", document_kind: "receipt", lines: [] }), "Summary")).toEqual({
      ok: false,
      errorCode: "budget_receipt_invalid_request",
    });
    expect(buildPlainBulkConfirmRequest(detailOf({ status: "imported", source_kind: "email", document_kind: "payment_confirmation", lines: [] }), "Summary")).toEqual({
      ok: false,
      errorCode: "budget_receipt_total_mismatch",
    });
    expect(buildPlainBulkConfirmRequest(detailOf({ status: "needs_review", source_kind: "email" }), "Summary")).toEqual({
      ok: false,
      errorCode: "budget_receipt_conflict",
    });

    expect(builtPay.ok).toBe(true);
    if (builtPay.ok) {
      expect(builtPay.request.link).toBeNull();
    }
  });

  it("preselects candidate:0 only for a single strong unlinked match and never in bulk or non-qualifying cases", () => {
    const nipCand = candidateOf({ match_strength: "nip", candidate_token: "tok-nip" });
    const distCand = candidateOf({ match_strength: "distinctive", candidate_token: "tok-dist" });
    const genericCand = candidateOf({ match_strength: "generic", candidate_token: "tok-gen" });
    const noneCand = candidateOf({ match_strength: "none", merchant_token_hit: false, candidate_token: "tok-none" });

    for (const status of ["imported", "draft", "needs_review"] as const) {
      for (const strongCand of [nipCand, distCand]) {
        const unlinked = detailOf({
          status,
          linked: false,
          linked_account_id: null,
          linked_entry_ref: null,
        });
        const draft = draftFromDetail(unlinked, [strongCand]);
        expect(draft.linkChoice).toBe("candidate:0");
        const built = buildConfirmRequest(unlinked, draft, [strongCand]);
        expect(built.ok).toBe(true);
        if (built.ok) {
          expect(built.request.link).toEqual({
            account_id: strongCand.account_id,
            entry_reference: strongCand.entry_reference,
            candidate_token: strongCand.candidate_token,
          });
        }
      }

      expect(draftFromDetail(detailOf({ status, linked: false }), [genericCand]).linkChoice).toBe("");
      expect(draftFromDetail(detailOf({ status, linked: false }), [noneCand]).linkChoice).toBe("");
      expect(draftFromDetail(detailOf({ status, linked: false }), [nipCand, noneCand]).linkChoice).toBe("");
    }

    // Never preselect if linked flag or stored link fields are set, or when confirmed, or for cash/mixed.
    expect(draftFromDetail(detailOf({
      status: "needs_review",
      linked: false,
      linked_account_id: "acc-1",
      linked_entry_ref: "b".repeat(64),
    }), [nipCand]).linkChoice).toBe("");
    expect(draftFromDetail(detailOf({
      status: "needs_review",
      linked: true,
      linked_account_id: "acc-1",
      linked_entry_ref: "b".repeat(64),
    }), [nipCand]).linkChoice).toBe("");
    expect(draftFromDetail(detailOf({ status: "confirmed", linked: false }), [nipCand]).linkChoice).toBe("unlink");
    expect(draftFromDetail(detailOf({ status: "imported", payment_method: "cash", linked: false }), [nipCand]).linkChoice).toBe("unlink");
    expect(draftFromDetail(detailOf({ status: "imported", payment_method: "mixed", linked: false }), [nipCand]).linkChoice).toBe("unlink");

    // Bulk confirm never preselects even when single strong candidate exists.
    const bulkTarget = detailOf({ status: "imported", source_kind: "email", document_kind: "invoice", lines: [], linked: false });
    const bulkDraft = draftFromDetail(bulkTarget, []);
    expect(bulkDraft.linkChoice).toBe("unlink");
    const builtBulk = buildPlainBulkConfirmRequest(bulkTarget, "Summary");
    expect(builtBulk.ok).toBe(true);
    if (builtBulk.ok) expect(builtBulk.request.link).toBeNull();
  });
});
