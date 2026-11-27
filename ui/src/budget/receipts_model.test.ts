import { describe, expect, it } from "vitest";
import { account, connection } from "./fixtures";
import {
  EMPTY_RECEIPT_FILTER,
  RECEIPT_MAX_PAGE_LIMIT,
  RECEIPT_PAGE_LIMIT,
  REDACTED_VALUE,
  addDecimals,
  availableLinkOptions,
  buildBulkConfirmRequest,
  buildBulkGroupPlan,
  buildBulkSelectionPlan,
  buildConfirmRequest,
  buildGroupLinkRequest,
  buildGroupPreConfirmRequest,
  buildPlainBulkConfirmRequest,
  buildReceiptListRequest,
  candidateDisplayDate,
  candidateSelectionBinding,
  clearStaleCandidateSelection,
  computeLinesDelta,
  createPendingReceiptStore,
  draftFromDetail,
  editReceiptLine,
  filterReceiptRows,
  findAdjacentReceiptId,
  findCurrentLinkedCandidate,
  findNextUnconfirmedReceiptId,
  formatBulkErrorBucket,
  hasActiveReceiptFilter,
  hasClientSideFilter,
  hasMatchingFieldsChanged,
  hasReceiptFieldOrLineEdits,
  haveConfirmLinesChanged,
  isBulkConfirmable,
  isExcludedDraftForBulk,
  isGmailImportSource,
  isGroupDateSpanValid,
  isGroupMemberEligible,
  isReceiptDraftDirty,
  isReceiptGrouped,
  mergeGroupPoolMembers,
  positiveGroupFee,
  isRedactedValue,
  isStaleCursorError,
  isStrongMatchStrength,
  keepCandidateBinding,
  listMonthForFilter,
  merchantBulkGroups,
  mergeReceiptRows,
  normalizeDecimalInput,
  preselectedCandidate,
  receiptAccountLabel,
  receiptHasRedactedLines,
  receiptMerchantGroupIdentity,
  receiptMerchantGroupKey,
  receiptNeedsConfirmation,
  receiptTotalAdjustment,
  reconcileLinkChoice,
  resolveDraftFromDetailSession,
  resolvePostRefreshSelectionId,
  sameReceiptFilter,
  selectCandidateDraft,
  selectKeepDraft,
  subtractDecimals,
  suggestedCandidateDates,
  uniqueIdCandidate,
  validPolishNip,
  validReceiptDateRange,
} from "./receipts_model";
import type { ReceiptCandidate, ReceiptDetail, ReceiptFilter, ReceiptSummary } from "./receipts_model";

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
    card_last4: null,
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

  it("sends Needs action to list and applies the same statuses to search results", () => {
    const filter: ReceiptFilter = { ...EMPTY_RECEIPT_FILTER, status: "needs_action" };
    expect(buildReceiptListRequest(filter, "next-page", 100)).toEqual({
      statuses: ["imported", "needs_review", "draft"],
      cursor: "next-page",
      limit: 100,
    });

    const search = { ...filter, query: "Synthetic Grocery" };
    const rows = [
      summary({ receipt_id: "search-imported", status: "imported" }),
      summary({ receipt_id: "search-confirmed", status: "confirmed" }),
      summary({ receipt_id: "search-review", status: "needs_review" }),
      summary({ receipt_id: "search-draft", status: "draft" }),
    ];
    expect(hasClientSideFilter(filter)).toBe(false);
    expect(hasClientSideFilter(search)).toBe(true);
    expect(filterReceiptRows(rows, search, true).map((row) => row.receipt_id))
      .toEqual(["search-imported", "search-review", "search-draft"]);
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
    expect(suggestedCandidateDates(
      candidateOf({ transaction_date: "2026-11-12", booking_date: "2026-11-14" }),
      { receipt_date: "2026-11-12", document_kind: "receipt", due_date: null },
    )).toEqual({
      primaryDate: "2026-11-12",
      bookingDayMonth: "14.11",
      displayDistance: 0,
    });
    expect(suggestedCandidateDates(
      candidateOf({ transaction_date: null, booking_date: "2026-11-16", value_date: "2026-11-14", date_distance: 0 }),
      { receipt_date: "2026-11-14", document_kind: "receipt", due_date: null },
    )).toEqual({
      primaryDate: "2026-11-14",
      bookingDayMonth: "16.11",
      displayDistance: 0,
    });
    expect(suggestedCandidateDates(
      candidateOf({ transaction_date: "2026-11-10", booking_date: "2026-11-16", value_date: "2026-11-18", date_distance: 2 }),
      { receipt_date: "2026-11-14", document_kind: "invoice", due_date: "2026-11-20" },
    )).toEqual({
      primaryDate: "2026-11-16",
      bookingDayMonth: null,
      displayDistance: 2,
    });
    expect(suggestedCandidateDates(
      candidateOf({ transaction_date: "2026-11-14", booking_date: "2026-11-14" }),
      { receipt_date: "2026-11-14", document_kind: "invoice", due_date: null },
    )).toEqual({
      primaryDate: "2026-11-14",
      bookingDayMonth: null,
      displayDistance: 0,
    });
    expect(suggestedCandidateDates(candidateOf({ transaction_date: null, booking_date: "2026-11-14", value_date: "2026-11-13" }))).toEqual({
      primaryDate: "2026-11-14",
      bookingDayMonth: null,
      displayDistance: 0,
    });
    expect(suggestedCandidateDates(candidateOf({ transaction_date: null, booking_date: null, value_date: "2026-11-13", date_distance: 1 }))).toEqual({
      primaryDate: "2026-11-13",
      bookingDayMonth: null,
      displayDistance: 1,
    });
    expect(suggestedCandidateDates(candidateOf({ transaction_date: null, booking_date: null, value_date: null, date_distance: 0 }))).toEqual({
      primaryDate: null,
      bookingDayMonth: null,
      displayDistance: 0,
    });
    expect(suggestedCandidateDates(
      candidateOf({ transaction_date: "2026-02-30", booking_date: "2026", value_date: null, date_distance: 3 }),
      { receipt_date: "2026-11-12", document_kind: "receipt", due_date: null },
    )).toEqual({
      primaryDate: "2026-02-30",
      bookingDayMonth: "2026",
      displayDistance: 3,
    });
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
    const draftKeep = selectKeepDraft(draftReview, cand);
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

      const builtExplicitLinked = buildConfirmRequest(unlinkedImport, selectCandidateDraft(draftImport, cand, 0), [cand]);
      expect(builtExplicitLinked.ok).toBe(true);
      if (builtExplicitLinked.ok) {
        expect(builtExplicitLinked.request.link).toEqual({
          account_id: cand.account_id,
          entry_reference: cand.entry_reference,
          candidate_token: cand.candidate_token,
        });
      }
      // Missing binding, altered match_strength, or altered evidence_fingerprint fails closed with candidateListChanged,
      // while a randomized candidate_token rotation for an identical candidate submits the fresh token from CURRENT candidates.
      expect(buildConfirmRequest(
        unlinkedImport,
        { ...draftImport, linkChoice: "candidate:0", selectedCandidate: null },
        [cand],
      )).toEqual({ ok: false, errorKey: "budget.receipts.candidateListChanged" });
      const rotatedTokenBuilt = buildConfirmRequest(
        unlinkedImport,
        selectCandidateDraft(draftImport, cand, 0),
        [{ ...cand, candidate_token: "tok-rotated" }],
      );
      expect(rotatedTokenBuilt.ok).toBe(true);
      if (rotatedTokenBuilt.ok) {
        expect(rotatedTokenBuilt.request.link).toEqual({
          account_id: cand.account_id,
          entry_reference: cand.entry_reference,
          candidate_token: "tok-rotated",
        });
      }
      expect(buildConfirmRequest(
        unlinkedImport,
        selectCandidateDraft(draftImport, { ...cand, match_strength: "nip" }, 0),
        [{ ...cand, match_strength: "generic" }],
      )).toEqual({ ok: false, errorKey: "budget.receipts.candidateListChanged" });
      const candWithFp = { ...cand, evidence_fingerprint: "fp-1" } as ReceiptCandidate;
      const candChangedFp = { ...cand, evidence_fingerprint: "fp-2" } as ReceiptCandidate;
      expect(buildConfirmRequest(
        unlinkedImport,
        selectCandidateDraft(draftImport, candWithFp, 0),
        [candChangedFp],
      )).toEqual({ ok: false, errorKey: "budget.receipts.candidateListChanged" });

      const draftOtherReceipt = draftFromDetail({ ...unlinkedImport, receipt_id: "other-receipt" }, [cand]);
      expect(clearStaleCandidateSelection(draftOtherReceipt, unlinkedImport.receipt_id, draftImport.candidateSetKey)).toBeNull();
      expect(clearStaleCandidateSelection(draftImport, unlinkedImport.receipt_id, draftImport.candidateSetKey)).toBe(draftImport);
      expect(clearStaleCandidateSelection(
        selectCandidateDraft(draftImport, cand, 0),
        unlinkedImport.receipt_id,
        "changed-set",
      )).toMatchObject({ linkChoice: "", selectedCandidate: null });
      expect(reconcileLinkChoice(unlinkedImport, "candidate:0", { ...draftImport, rawShopName: "Changed" }, [cand])).toBe("");

      const noCandidatesDraft = draftFromDetail(unlinkedImport, []);
      expect(noCandidatesDraft.linkChoice).toBe("unlink");
      const builtNoCand = buildConfirmRequest(unlinkedImport, noCandidatesDraft, []);
      expect(builtNoCand.ok).toBe(true);
      if (builtNoCand.ok) expect(builtNoCand.request.link).toBeNull();
    }

    // 7. Changing the total invalidates the bank link. An invalid NIP still blocks confirmation.
    expect(buildConfirmRequest(reviewLinked, { ...draftKeep, totalAmount: "99.99" }, [cand])).toEqual({
      ok: false,
      errorKey: "budget.receipts.invalidForm",
    });
    expect(buildConfirmRequest(reviewLinked, { ...draftKeep, rawNip: "5261040829" }, [cand])).toEqual({
      ok: false,
      errorKey: "budget.receipts.invalidForm",
    });
  });

  const ocrLines = ["10", "20", "30"].map<ReceiptDetail["lines"][number]>((amount, index) => ({
    line_no: index + 1,
    line_kind: "item",
    raw_name: `Synthetic item ${String(index + 1)}`,
    raw_name_key: `synthetic item ${String(index + 1)}`,
    ptu_code: null,
    quantity: "1",
    raw_unit: null,
    unit_price_raw: null,
    gross_line_total: amount,
    discount_amount: "0",
    net_line_total: amount,
  }));

  it("confirms a partially read receipt with the original lines and a single positive remainder", () => {
    const detail = detailOf({ status: "draft", total_amount: "67.01", lines: ocrLines });
    const draft = draftFromDetail(detail);
    const built = buildConfirmRequest(detail, draft, []);

    expect(built.ok).toBe(true);
    if (built.ok) {
      expect(built.request.lines).toEqual([
        ...draft.lines,
        {
          line_kind: "item",
          raw_name: "Pozostałe / nierozpoznane pozycje",
          ptu_code: null,
          quantity: "1",
          raw_unit: null,
          unit_price_raw: null,
          gross_line_total: "7.01",
          discount_amount: "0",
          net_line_total: "7.01",
        },
      ]);
    }
  });

  it("confirms with a single total line when the OCR lines exceed the receipt total", () => {
    const detail = detailOf({ status: "draft", total_amount: "52.99", lines: ocrLines });
    const built = buildConfirmRequest(detail, draftFromDetail(detail), []);

    expect(built.ok).toBe(true);
    if (built.ok) {
      expect(built.request.lines).toEqual([{
        line_kind: "other",
        raw_name: "Tylko suma",
        ptu_code: null,
        quantity: "1",
        raw_unit: null,
        unit_price_raw: null,
        gross_line_total: "52.99",
        discount_amount: "0",
        net_line_total: "52.99",
      }]);
    }
  });

  it.each([
    {
      rawName: "Product with an excessive discount",
      receiptKind: "purchase",
      lineKind: "item",
      gross: "2",
      discount: "5",
      net: "-3",
    },
    {
      rawName: "KARTA PODARUNKOWA",
      receiptKind: "refund",
      lineKind: "item",
      gross: "-30",
      discount: "0",
      net: "-30",
    },
    {
      rawName: "KAUCJA",
      receiptKind: "purchase",
      lineKind: "deposit",
      gross: "0",
      discount: "0",
      net: "0",
    },
  ] as const)("shows a total-only adjustment for invalid OCR line $rawName", (example) => {
    const detail = detailOf({
      status: "draft",
      receipt_kind: example.receiptKind,
      total_amount: "50.00",
      lines: ocrLines.slice(0, 1).map((line) => ({
        ...line,
        raw_name: example.rawName,
        line_kind: example.lineKind,
        gross_line_total: example.gross,
        discount_amount: example.discount,
        net_line_total: example.net,
      })),
    });

    expect(receiptTotalAdjustment(detail, draftFromDetail(detail))).toEqual({ kind: "summary" });
  });

  it.each(["deposit", "basket_discount", "other"] as const)("keeps a valid negative %s line on a refund", (kind) => {
    const detail = detailOf({
      status: "draft",
      receipt_kind: "refund",
      total_amount: "50.00",
      lines: ocrLines.slice(0, 1).map((line) => ({
        ...line,
        line_kind: kind,
        gross_line_total: "-30",
        net_line_total: "-30",
      })),
    });
    const draft = draftFromDetail(detail);

    expect(receiptTotalAdjustment(detail, draft)).toEqual({ kind: "remainder", amount: "80" });
    expect(buildConfirmRequest(detail, draft, [])).toMatchObject({
      ok: true,
      request: {
        receipt_kind: "refund",
        total_amount: "50",
        lines: [
          { line_kind: kind, net_line_total: "-30" },
          { raw_name: "Pozostałe / nierozpoznane pozycje", net_line_total: "80" },
        ],
      },
    });
  });

  it.each([
    { label: "inconsistent gross and net with a balanced total", fields: { gross_line_total: "11" } },
    { label: "an unreadable net amount", fields: { net_line_total: "?" } },
  ])("confirms and keeps the bank link despite $label", ({ fields }) => {
    const detail = detailOf({
      status: "draft",
      total_amount: "60.00",
      lines: ocrLines.map((line, index) => index === 0 ? { ...line, ...fields } : line),
    });
    const candidate = candidateOf({ amount: "60.00", match_strength: "nip" });
    const draft = draftFromDetail(detail, [candidate]);

    expect(buildConfirmRequest(detail, draft, [candidate])).toMatchObject({
      ok: true,
      request: {
        total_amount: "60",
        link: {
          account_id: candidate.account_id,
          entry_reference: candidate.entry_reference,
          candidate_token: candidate.candidate_token,
        },
        lines: [{ line_kind: "other", raw_name: "Tylko suma", gross_line_total: "60", net_line_total: "60" }],
      },
    });
  });

  it("keeps stored receipt lines when their sum equals the total", () => {
    const detail = detailOf({ status: "draft", total_amount: "60.00", lines: ocrLines });
    const built = buildConfirmRequest(detail, draftFromDetail(detail), []);

    expect(built.ok).toBe(true);
    if (built.ok) expect(built.request).not.toHaveProperty("lines");
  });

  it("uses a total-only line when appending a remainder would exceed the request line limit", () => {
    const lines = Array.from({ length: 100 }, () => ocrLines).flat()
      .map((line, index) => ({ ...line, line_no: index + 1 }));
    const detail = detailOf({ status: "draft", total_amount: "6007.01", lines });
    const built = buildConfirmRequest(detail, draftFromDetail(detail), []);

    expect(built).toMatchObject({
      ok: true,
      request: { lines: [{ raw_name: "Tylko suma", net_line_total: "6007.01" }] },
    });
  });

  it.each([
    { reason: "exceeds the amount range", total: "999999999", deposit: "-1" },
    { reason: "exceeds the privacy digit limit", total: "123456789", deposit: "-0.1234" },
    { reason: "matches a private identifier checksum", total: "123456789", deposit: "-0.27" },
  ])("uses a total-only line when the remainder $reason", ({ total, deposit }) => {
    const detail = detailOf({
      status: "draft",
      total_amount: total,
      lines: ocrLines.slice(0, 1).map((line) => ({
        ...line,
        line_kind: "deposit",
        gross_line_total: deposit,
        net_line_total: deposit,
      })),
    });
    const draft = draftFromDetail(detail);

    expect(receiptTotalAdjustment(detail, draft)).toEqual({ kind: "summary" });
    expect(buildConfirmRequest(detail, draft, [])).toMatchObject({
      ok: true,
      request: { lines: [{ raw_name: "Tylko suma", net_line_total: total }] },
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

  it("keeps grouped Gmail receipts out of legacy link and bulk-confirm flows", () => {
    const receipt = detailOf({ status: "imported", source_kind: "email" });
    const grouped = {
      ...receipt,
      link_group: { group_id: "group-1", group_version: 1, group_status: "confirmed" as const,
        member_count: 2, evidence_receipt_id: null, fee_amount: null },
    };
    const candidate = candidateOf({ match_strength: "id" });
    expect(isReceiptGrouped(receipt)).toBe(false);
    expect(isReceiptGrouped({ link_group: null })).toBe(false);
    expect(isBulkConfirmable(receipt)).toBe(true);
    expect(isBulkConfirmable(grouped)).toBe(false);
    expect(preselectedCandidate(grouped, [candidate], "card")).toBeNull();
    expect(uniqueIdCandidate(grouped, [candidate])).toBeNull();
    expect(availableLinkOptions({ ...grouped, linked: true }, [candidate], "card")).toMatchObject({
      canKeep: false, canPickCandidates: false, mustUnlinkToConfirm: false,
    });
    expect(buildConfirmRequest(grouped, draftFromDetail(receipt, [candidate]), [candidate]).ok).toBe(false);
    expect(buildBulkConfirmRequest(grouped, [candidate], "Document", { linkById: true }).ok).toBe(false);
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

    // Bulk confirmation still rejects OCR drafts, needs_review and payment confirmations without lines.
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
      for (const genericShop of ["FIRMA HANDLOWA", "COMPANY", "SKLEP OGÓLNOSPOŻYWCZY"]) {
        expect(draftFromDetail(detailOf({ status, linked: false, raw_shop_name: genericShop }), [genericCand]).linkChoice).toBe("");
      }
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

  it("confirms zero-line Gmail invoices with or without a link while rejecting zero-line non-invoices", () => {
    const nipCand = candidateOf({ match_strength: "nip", candidate_token: "tok-inv-nip" });
    const zeroInv = detailOf({
      status: "imported",
      source_kind: "email",
      document_kind: "invoice",
      invoice_number: "FV/2026/05",
      payment_method: null,
      total_amount: "99.50",
      lines: [],
      linked: false,
      version: 3,
    });
    const preDraft = draftFromDetail(zeroInv, [nipCand]);
    expect(preDraft.linkChoice).toBe("candidate:0");
    const builtLinked = buildConfirmRequest(zeroInv, preDraft, [nipCand], "Receipt summary");
    expect(builtLinked.ok).toBe(true);
    if (builtLinked.ok) {
      expect(builtLinked.request.link).toEqual({
        account_id: nipCand.account_id,
        entry_reference: nipCand.entry_reference,
        candidate_token: nipCand.candidate_token,
      });
      expect(builtLinked.request.lines).toEqual([{
        line_kind: "other",
        raw_name: "FV/2026/05",
        ptu_code: null,
        quantity: "1",
        raw_unit: null,
        unit_price_raw: null,
        gross_line_total: "99.5",
        discount_amount: "0",
        net_line_total: "99.5",
      }]);
    }

    const builtUnlinked = buildConfirmRequest(zeroInv, { ...preDraft, linkChoice: "unlink" }, [nipCand], "Receipt summary");
    expect(builtUnlinked.ok).toBe(true);
    if (builtUnlinked.ok) {
      expect(builtUnlinked.request.link).toBeNull();
      expect(builtUnlinked.request.lines?.length).toBe(1);
    }

    const redactedZeroInv = detailOf({
      status: "imported",
      source_kind: "email",
      document_kind: "invoice",
      invoice_number: REDACTED_VALUE,
      raw_shop_name: REDACTED_VALUE,
      raw_nip: REDACTED_VALUE,
      payment_method: null,
      total_amount: "42.50",
      lines: [],
      linked: false,
    });
    const builtRedactedInv = buildConfirmRequest(
      redactedZeroInv,
      draftFromDetail(redactedZeroInv, [nipCand]),
      [nipCand],
      "Receipt summary",
    );
    expect(builtRedactedInv.ok).toBe(true);
    if (builtRedactedInv.ok) {
      expect(builtRedactedInv.request.lines?.[0]?.raw_name).toBe("Receipt summary");
    }

    const zeroNonInv = detailOf({ status: "imported", source_kind: "email", document_kind: "payment_confirmation", lines: [] });
    expect(buildConfirmRequest(zeroNonInv, { ...draftFromDetail(zeroNonInv, [nipCand]), linkChoice: "unlink" }, [nipCand])).toEqual({
      ok: false,
      errorKey: "budget.receipts.totalMismatch",
    });
  });

  it("preserves explicit link choices across reloads and suppresses preselection when touched or candidates change", () => {
    const candA = candidateOf({ entry_reference: "a".repeat(64), match_strength: "nip", candidate_token: "tok-a" });
    const candB = candidateOf({ entry_reference: "b".repeat(64), match_strength: "nip", candidate_token: "tok-b" });
    const inv = detailOf({ status: "imported", linked: false });

    const first = resolveDraftFromDetailSession(inv, [candA], undefined);
    expect(first.draft.linkChoice).toBe("candidate:0");
    expect(first.showSuggestedBanner).toBe(true);

    // Untouched + same candidate identity set with rotated random candidate_token keeps preselection and uses fresh token.
    const candARotated: ReceiptCandidate = { ...candA, candidate_token: "tok-rand-2" };
    const sameReload = resolveDraftFromDetailSession(inv, [candARotated], first.nextSessionEntry);
    expect(sameReload.draft.linkChoice).toBe("candidate:0");
    expect(sameReload.showSuggestedBanner).toBe(true);
    const builtAfterReopen = buildConfirmRequest(inv, sameReload.draft, [candARotated]);
    expect(builtAfterReopen.ok).toBe(true);
    if (builtAfterReopen.ok) {
      expect(builtAfterReopen.request.link).toMatchObject({ candidate_token: "tok-rand-2" });
    }

    // Optional evidence_fingerprint participates in identity binding when present.
    const candFp1 = { ...candA, evidence_fingerprint: "fp-1" } as ReceiptCandidate;
    const candFp2 = { ...candA, candidate_token: "tok-fp-2", evidence_fingerprint: "fp-2" } as ReceiptCandidate;
    const firstFp = resolveDraftFromDetailSession(inv, [candFp1], undefined);
    expect(firstFp.draft.linkChoice).toBe("candidate:0");
    const driftedFp = resolveDraftFromDetailSession(inv, [candFp2], firstFp.nextSessionEntry);
    expect(driftedFp.draft.linkChoice).toBe("");
    expect(driftedFp.showSuggestedBanner).toBe(false);

    // User touched Odłącz -> stays unlink across reloads even though strong candA is returned.
    const touchedUnlink = { ...first.nextSessionEntry, touched: true, choice: { kind: "unlink" as const } };
    const afterUnlinkReload = resolveDraftFromDetailSession(inv, [candA], touchedUnlink);
    expect(afterUnlinkReload.draft.linkChoice).toBe("unlink");
    expect(afterUnlinkReload.showSuggestedBanner).toBe(false);

    // User touched Zmień -> stays "" across reloads.
    const touchedEmpty = { ...first.nextSessionEntry, touched: true, choice: { kind: "empty" as const } };
    const afterEmptyReload = resolveDraftFromDetailSession(inv, [candA], touchedEmpty);
    expect(afterEmptyReload.draft.linkChoice).toBe("");
    expect(afterEmptyReload.showSuggestedBanner).toBe(false);

    // User chose specific candidate -> rebinds by stable identity (even with rotated token/order) and drops if gone or strength changes.
    const touchedCandA = {
      ...first.nextSessionEntry,
      touched: true,
      choice: {
        kind: "candidate" as const,
        ...candidateSelectionBinding(candA),
      },
    };
    expect(resolveDraftFromDetailSession(inv, [candB, candARotated], touchedCandA).draft.linkChoice).toBe("candidate:1");
    expect(resolveDraftFromDetailSession(
      inv,
      [{ ...candA, match_strength: "generic" }],
      touchedCandA,
    ).draft.linkChoice).toBe("");
    const afterMissing = resolveDraftFromDetailSession(inv, [candB], touchedCandA);
    expect(afterMissing.draft.linkChoice).toBe("");
    expect(afterMissing.showSuggestedBanner).toBe(false);

    // Untouched, candidate set changes ([candA] -> [candB], strength change, or [] -> [candA]) -> never preselects.
    const changedCandSet = resolveDraftFromDetailSession(inv, [candB], first.nextSessionEntry);
    expect(changedCandSet.draft.linkChoice).toBe("");
    expect(changedCandSet.showSuggestedBanner).toBe(false);
    expect(changedCandSet.nextSessionEntry.candidateSetChanged).toBe(true);
    expect(resolveDraftFromDetailSession(
      inv,
      [{ ...candA, match_strength: "generic" }],
      first.nextSessionEntry,
    ).draft.linkChoice).toBe("");

    const startedEmpty = resolveDraftFromDetailSession(inv, [], undefined);
    expect(startedEmpty.draft.linkChoice).toBe("unlink");
    const laterArrived = resolveDraftFromDetailSession(inv, [candA], startedEmpty.nextSessionEntry);
    expect(laterArrived.draft.linkChoice).toBe("");
    expect(laterArrived.showSuggestedBanner).toBe(false);

    // Keep choice submits reviewed_token when evidence matches (even if token rotated),
    // fails closed with bankEvidenceChanged when bank evidence drifts, and clears on server version bump.
    const reviewLinked = detailOf({ status: "needs_review", linked: true, linked_account_id: candA.account_id, linked_entry_ref: candA.entry_reference, version: 2 });
    const touchedKeep = {
      baselineVersion: 2,
      baselineStatus: "needs_review" as const,
      baselineLinked: true,
      baselineCandidateKeys: [`${candA.account_id}:${candA.entry_reference}:${candA.match_strength}:`],
      touched: true,
      choice: { kind: "keep" as const, keepBinding: keepCandidateBinding(candA) },
      candidateSetChanged: false,
    };
    const keepUnchanged = resolveDraftFromDetailSession(reviewLinked, [candARotated], touchedKeep);
    expect(keepUnchanged.draft.linkChoice).toBe("keep");
    expect(keepUnchanged.keepEvidenceInvalidated).toBe(false);
    const keepBuilt = buildConfirmRequest(reviewLinked, keepUnchanged.draft, [candARotated]);
    expect(keepBuilt.ok).toBe(true);
    if (keepBuilt.ok) {
      // Must submit reviewed_token ("tok-a"), NEVER swap in the fresh rotated token ("tok-rand-2").
      expect(keepBuilt.request.link).toMatchObject({ candidate_token: "tok-a" });
    }

    const candAChangedAmount: ReceiptCandidate = { ...candA, candidate_token: "tok-changed", amount: "43.00" };
    expect(buildConfirmRequest(reviewLinked, keepUnchanged.draft, [candAChangedAmount])).toEqual({
      ok: false,
      errorKey: "budget.receipts.bankEvidenceChanged",
    });
    expect(clearStaleCandidateSelection(
      keepUnchanged.draft,
      reviewLinked.receipt_id,
      keepUnchanged.draft.candidateSetKey,
      candAChangedAmount,
    )).toMatchObject({ linkChoice: "", selectedCandidate: null, keepBinding: null });
    const afterEvidenceDrift = resolveDraftFromDetailSession(reviewLinked, [candAChangedAmount], touchedKeep);
    expect(afterEvidenceDrift.draft.linkChoice).toBe("");
    expect(afterEvidenceDrift.draft.keepBinding).toBeNull();
    expect(afterEvidenceDrift.keepEvidenceInvalidated).toBe(true);
    expect(afterEvidenceDrift.nextSessionEntry.choice).toEqual({ kind: "empty" });

    expect(resolveDraftFromDetailSession(reviewLinked, [candB], touchedKeep).keepEvidenceInvalidated).toBe(true);
    const bumpedVersion = detailOf({ status: "needs_review", linked: false, version: 3 });
    const afterBump = resolveDraftFromDetailSession(bumpedVersion, [candA], touchedKeep);
    expect(afterBump.draft.linkChoice).toBe("");
    expect(afterBump.showSuggestedBanner).toBe(false);
    expect(afterBump.nextSessionEntry.touched).toBe(true);
  });

  it("treats id as strong for preselection and limits bulk ID linking strictly to single-id matches", () => {
    expect(isStrongMatchStrength("id")).toBe(true);
    const idCand: ReceiptCandidate = {
      account_id: "acc-1",
      entry_reference: "1".repeat(64),
      amount: "42.50",
      currency: "PLN",
      credit_debit_indicator: "DBIT",
      transaction_date: "2026-11-14",
      booking_date: "2026-11-14",
      value_date: "2026-11-14",
      merchant_token_hit: false,
      match_strength: "id",
      date_distance: 0,
      candidate_token: "tok-id-1",
    };
    const nipCand: ReceiptCandidate = {
      ...idCand,
      entry_reference: "2".repeat(64),
      match_strength: "nip",
      candidate_token: "tok-nip-1",
    };
    const weakCand: ReceiptCandidate = {
      ...idCand,
      entry_reference: "3".repeat(64),
      match_strength: "none",
      candidate_token: "tok-none-1",
    };
    const gatewayImported = detailOf({
      status: "imported",
      source_kind: "email",
      document_kind: "payment_confirmation",
      gateway: "Przelewy24",
      gateway_status: "paid",
      fee_amount: null,
      payee_host: null,
      evidence_for_receipt_id: null,
      has_document: false,
    });
    expect(preselectedCandidate(gatewayImported, [idCand], "card")).toEqual(idCand);
    expect(preselectedCandidate(gatewayImported, [idCand, weakCand], "card")).toBeNull();

    // Unique "id" links even when an extra "none" coincidence candidate exists, while "nip" / "distinctive" / empty / 2-"id" confirm unlinked.
    expect(uniqueIdCandidate(gatewayImported, [idCand, weakCand])?.candidate).toEqual(idCand);
    expect(uniqueIdCandidate(gatewayImported, [nipCand])).toBeNull();
    expect(uniqueIdCandidate(gatewayImported, [idCand, { ...idCand, candidate_token: "tok-id-2", entry_reference: "4".repeat(64) }])).toBeNull();
    expect(uniqueIdCandidate(gatewayImported, [{ ...idCand, candidate_token: "   " }])).toBeNull();
    expect(uniqueIdCandidate({ ...gatewayImported, evidence_for_receipt_id: "ev-1" }, [idCand])).toBeNull();
    expect(uniqueIdCandidate({ ...gatewayImported, linked: true, linked_account_id: "acc-1", linked_entry_ref: "1".repeat(64) }, [idCand])).toBeNull();
    expect(uniqueIdCandidate({ ...gatewayImported, payment_method: "cash" }, [idCand])).toBeNull();

    const plain = buildBulkConfirmRequest(gatewayImported, [idCand], "Summary", { linkById: false });
    expect(plain.ok).toBe(true);
    if (plain.ok) {
      expect(plain.linkedById).toBe(false);
      expect(plain.request.link).toBeNull();
    }

    const withId = buildBulkConfirmRequest(gatewayImported, [weakCand, idCand], "Summary", { linkById: true });
    expect(withId.ok).toBe(true);
    if (withId.ok) {
      expect(withId.linkedById).toBe(true);
      expect(withId.request.link).toEqual({
        account_id: idCand.account_id,
        entry_reference: idCand.entry_reference,
        candidate_token: "tok-id-1",
      });
    }

    const withNipOnly = buildBulkConfirmRequest(gatewayImported, [nipCand], "Summary", { linkById: true });
    expect(withNipOnly.ok).toBe(true);
    if (withNipOnly.ok) {
      expect(withNipOnly.linkedById).toBe(false);
      expect(withNipOnly.request.link).toBeNull();
    }
  });

  it("validates M:1 group member eligibility, bulk plan constraints, and 14-day date spans", () => {
    const base = detailOf({ status: "confirmed", linked: false, payment_method: "card" });
    expect(isGroupMemberEligible(base)).toBe(true);
    expect(isGroupMemberEligible({ ...base, status: "imported" })).toBe(true);
    expect(isGroupMemberEligible({ ...base, receipt_kind: "refund" })).toBe(true);
    expect(isGroupMemberEligible({ ...base, status: "needs_review" })).toBe(false);
    expect(isGroupMemberEligible({ ...base, status: "draft" })).toBe(false);
    expect(isGroupMemberEligible({ ...base, linked: true, linked_account_id: "acc-1", linked_entry_ref: "1".repeat(64) })).toBe(false);
    expect(isGroupMemberEligible({
      ...base,
      link_group: { group_id: "g1", group_version: 1, group_status: "confirmed", member_count: 2, evidence_receipt_id: null, fee_amount: null },
    })).toBe(false);
    expect(isGroupMemberEligible({ ...base, gateway: "Przelewy24" })).toBe(false);
    expect(isGroupMemberEligible({ ...base, evidence_for_receipt_id: "other" })).toBe(false);
    expect(isGroupMemberEligible({ ...base, payment_method: "cash" })).toBe(false);
    expect(isGroupMemberEligible({ ...base, payment_method: "mixed" })).toBe(false);
    expect(isGroupMemberEligible({ ...base, total_amount: "0" })).toBe(false);
    expect(isGroupMemberEligible({ ...base, currency: null })).toBe(false);
    expect(isGroupMemberEligible({ ...base, receipt_date: null })).toBe(false);

    const r1 = detailOf({ receipt_id: "r1", status: "confirmed", currency: "PLN", receipt_kind: "purchase" });
    const r2 = detailOf({ receipt_id: "r2", status: "imported", currency: "PLN", receipt_kind: "purchase" });
    const eur = detailOf({ receipt_id: "r3", status: "confirmed", currency: "EUR", receipt_kind: "purchase" });
    const ref = detailOf({ receipt_id: "r4", status: "confirmed", currency: "PLN", receipt_kind: "refund" });
    const rows = [r1, r2, eur, ref, ...Array.from({ length: 7 }, (_, i) => detailOf({
      receipt_id: `extra-${String(i)}`, status: "confirmed", currency: "PLN", receipt_kind: "purchase",
    }))];

    expect(buildBulkGroupPlan(rows, new Set(["r1"]))).toMatchObject({ eligible: false, reason: "count" });
    expect(buildBulkGroupPlan(rows, new Set(["r1", "r2"]))).toMatchObject({
      eligible: true, reason: "none", currency: "PLN", receiptKind: "purchase",
    });
    expect(buildBulkGroupPlan(rows, new Set(["r1", "r3"]))).toMatchObject({ eligible: false, reason: "mismatch" });
    expect(buildBulkGroupPlan(rows, new Set(["r1", "r4"]))).toMatchObject({ eligible: false, reason: "mismatch" });
    expect(buildBulkGroupPlan(rows, new Set(rows.filter((r) => r.currency === "PLN" && r.receipt_kind === "purchase").map((r) => r.receipt_id)))).toMatchObject({
      eligible: false, reason: "count",
    });

    expect(isGroupDateSpanValid(["2026-11-01", "2026-11-15"])).toBe(true);
    expect(isGroupDateSpanValid(["2026-11-01", "2026-11-16"])).toBe(false);
    expect(isGroupDateSpanValid(["2026-11-01", null])).toBe(false);
    expect(isGroupDateSpanValid([])).toBe(false);
  });

  it("builds unlinked group pre-confirm payloads, CAS group/link requests, and merges pool versions safely", () => {
    const importedInv = detailOf({
      receipt_id: "imp-inv",
      status: "imported",
      document_kind: "invoice",
      lines: [],
      total_amount: "19.99",
      version: 4,
    });
    const pre = buildGroupPreConfirmRequest(importedInv, 4, "Summary");
    expect(pre.ok).toBe(true);
    if (pre.ok) {
      expect(pre.request.link).toBeNull();
      expect(pre.request.expected_version).toBe(4);
      expect(pre.request.lines).toHaveLength(1);
    }
    expect(buildGroupPreConfirmRequest(importedInv, 3, "Summary")).toEqual({
      ok: false, errorCode: "budget_receipt_conflict",
    });
    expect(buildGroupPreConfirmRequest({ ...importedInv, linked: true, linked_account_id: "acc-1", linked_entry_ref: "a".repeat(64) }, 4, "Summary")).toEqual({
      ok: false, errorCode: "budget_receipt_tx_already_linked",
    });

    const staleAnchor = detailOf({ receipt_id: "r1", version: 1 });
    const freshAnchor = detailOf({ receipt_id: "r1", version: 2 });
    const pooled = mergeGroupPoolMembers(staleAnchor, [freshAnchor, detailOf({ receipt_id: "r2", version: 5 })]);
    expect(pooled.map((r) => [r.receipt_id, r.version])).toEqual([["r1", 2], ["r2", 5]]);

    expect(positiveGroupFee("0.00")).toBeNull();
    expect(positiveGroupFee("1.25")).toBe("1.25");
    expect(buildGroupLinkRequest({
      transaction: { account_id: "acc-1", entry_reference: "a".repeat(64), candidate_token: "fresh-tok" },
      members: [{ receipt_id: "r1", expected_version: 2 }, { receipt_id: "r2", expected_version: 5 }],
      evidence: { receipt_id: "ev-1", expected_version: 3, fee_amount: "0.50" },
    })).toEqual({
      link: { account_id: "acc-1", entry_reference: "a".repeat(64), candidate_token: "fresh-tok" },
      members: [{ receipt_id: "r1", expected_version: 2 }, { receipt_id: "r2", expected_version: 5 }],
      evidence_receipt_id: "ev-1",
      evidence_expected_version: 3,
      fee_amount: "0.5",
    });
  });

  it("supports filter equality, list limit sizing, post-refresh selection, and adjacent/unconfirmed navigation", () => {
    expect(RECEIPT_PAGE_LIMIT).toBe(50);
    expect(RECEIPT_MAX_PAGE_LIMIT).toBe(200);
    expect(buildReceiptListRequest(EMPTY_RECEIPT_FILTER, null, 120)).toEqual({ limit: 120 });
    expect(buildReceiptListRequest(EMPTY_RECEIPT_FILTER, "c-2", RECEIPT_MAX_PAGE_LIMIT)).toEqual({
      limit: 200,
      cursor: "c-2",
    });

    expect(sameReceiptFilter(EMPTY_RECEIPT_FILTER, { ...EMPTY_RECEIPT_FILTER, query: "   " })).toBe(true);
    expect(sameReceiptFilter(EMPTY_RECEIPT_FILTER, { ...EMPTY_RECEIPT_FILTER, query: "Lidl" })).toBe(false);
    expect(sameReceiptFilter(EMPTY_RECEIPT_FILTER, { ...EMPTY_RECEIPT_FILTER, status: "imported" })).toBe(false);
    expect(sameReceiptFilter(EMPTY_RECEIPT_FILTER, { ...EMPTY_RECEIPT_FILTER, sourceKind: "email" })).toBe(false);

    const r1 = summary({ receipt_id: "r1", status: "imported", version: 1 });
    const r2 = summary({ receipt_id: "r2", status: "confirmed", version: 2 });
    const r3 = summary({ receipt_id: "r3", status: "draft", version: 1 });
    const r4 = summary({ receipt_id: "r4", status: "needs_review", version: 1 });
    const rows = [r1, r2, r3, r4];

    expect(receiptNeedsConfirmation(r1)).toBe(true);
    expect(receiptNeedsConfirmation(r2)).toBe(false);
    expect(receiptNeedsConfirmation(r3)).toBe(true);
    expect(receiptNeedsConfirmation(r4)).toBe(true);

    expect(findAdjacentReceiptId(rows, "r1", "prev")).toBeNull();
    expect(findAdjacentReceiptId(rows, "r1", "next")).toBe("r2");
    expect(findAdjacentReceiptId(rows, "r4", "prev")).toBe("r3");
    expect(findAdjacentReceiptId(rows, "r4", "next")).toBeNull();
    expect(findAdjacentReceiptId(rows, "missing", "next")).toBeNull();

    // Post-refresh selection keeps current ID when present, falls back to same index (clamped) when gone, or null when empty.
    expect(resolvePostRefreshSelectionId([r1, r2, r3], "r2", 1)).toBe("r2");
    expect(resolvePostRefreshSelectionId([r1, r3], "r2", 1)).toBe("r3");
    expect(resolvePostRefreshSelectionId([r1, r2], "r3", 2)).toBe("r2");
    expect(resolvePostRefreshSelectionId([], "r1", 0)).toBeNull();

    expect(findNextUnconfirmedReceiptId(rows, "r1", { wrap: false })).toBe("r3");
    expect(findNextUnconfirmedReceiptId(rows, "r4", { wrap: false })).toBeNull();
    expect(findNextUnconfirmedReceiptId(rows, "r4", { wrap: true })).toBe("r1");
    expect(findNextUnconfirmedReceiptId(rows, "r1", { wrap: false, predicate: (r) => r.receipt_id !== "r3" })).toBe("r4");
    expect(findNextUnconfirmedReceiptId([r1, r3], "r2", { wrap: false, startIndex: 1 })).toBe("r3");
    expect(findNextUnconfirmedReceiptId([r1], "r2", { wrap: true, startIndex: 1 })).toBe("r1");
    const allConfirmed = rows.map((r) => ({ ...r, status: "confirmed" as const }));
    expect(findNextUnconfirmedReceiptId(allConfirmed, "r1", { wrap: true })).toBeNull();
  });

  it("detects dirty receipt drafts via a single full-draft snapshot comparison including link, candidate, and keep state", () => {
    const baseDetail = detailOf({
      receipt_id: "dirty-1",
      currency: null,
      payment_method: null,
      receipt_time: null,
      raw_nip: null,
    });
    const clean = draftFromDetail(baseDetail, []);
    expect(isReceiptDraftDirty(baseDetail, clean)).toBe(false);
    expect(isReceiptDraftDirty(baseDetail, { ...clean, receiptId: "other-id", rawShopName: "Changed" })).toBe(false);

    // Header and line edits mark the draft dirty.
    expect(isReceiptDraftDirty(baseDetail, { ...clean, rawShopName: "Edited Shop" })).toBe(true);
    expect(isReceiptDraftDirty(baseDetail, { ...clean, rawNip: "5261040828" })).toBe(true);
    expect(isReceiptDraftDirty(baseDetail, { ...clean, receiptKind: "refund" })).toBe(true);
    expect(isReceiptDraftDirty(baseDetail, { ...clean, receiptDate: "2026-11-15" })).toBe(true);
    expect(isReceiptDraftDirty(baseDetail, { ...clean, receiptTime: "10:15" })).toBe(true);
    expect(isReceiptDraftDirty(baseDetail, { ...clean, totalAmount: "43.00" })).toBe(true);
    expect(isReceiptDraftDirty(baseDetail, { ...clean, currency: "EUR" })).toBe(true);
    expect(isReceiptDraftDirty(baseDetail, { ...clean, paymentMethod: "cash" })).toBe(true);
    expect(isReceiptDraftDirty(baseDetail, { ...clean, lines: [] })).toBe(true);
    const editedLine = clean.lines.map((line, idx) => (idx === 0 ? { ...line, raw_name: "Edited Milk" } : line));
    expect(isReceiptDraftDirty(baseDetail, { ...clean, lines: editedLine })).toBe(true);
    expect(hasReceiptFieldOrLineEdits(baseDetail, { ...clean, rawShopName: "Edited Shop" })).toBe(true);
    expect(hasReceiptFieldOrLineEdits(baseDetail, { ...clean, lines: editedLine })).toBe(true);

    // Link choice (Unlink / Keep) and candidate selection edits mark the draft dirty.
    const candNip: ReceiptCandidate = {
      account_id: "acc-1",
      entry_reference: "a".repeat(64),
      amount: "42.50",
      currency: "PLN",
      credit_debit_indicator: "DBIT",
      transaction_date: "2026-11-14",
      booking_date: "2026-11-14",
      value_date: "2026-11-14",
      merchant_token_hit: true,
      match_strength: "nip",
      date_distance: 0,
      candidate_token: "tok-1",
    };
    const candAlt: ReceiptCandidate = {
      ...candNip,
      entry_reference: "b".repeat(64),
      match_strength: "none",
      candidate_token: "tok-2",
    };

    const importedUnlinked = detailOf({ receipt_id: "dirty-imp", status: "imported", linked: false });
    const preselectedClean = draftFromDetail(importedUnlinked, [candNip]);
    expect(isReceiptDraftDirty(importedUnlinked, preselectedClean, [candNip])).toBe(false);
    // Token rotation on the same candidate does not introduce false dirtiness.
    expect(isReceiptDraftDirty(importedUnlinked, preselectedClean, [{ ...candNip, candidate_token: "tok-rotated" }])).toBe(false);
    // Choosing Unlink as the only edit makes the draft dirty (while header/line edit helper stays false).
    const unlinkedOnly = { ...preselectedClean, linkChoice: "unlink", selectedCandidate: null, keepBinding: null };
    expect(isReceiptDraftDirty(importedUnlinked, unlinkedOnly, [candNip])).toBe(true);
    expect(hasReceiptFieldOrLineEdits(importedUnlinked, unlinkedOnly)).toBe(false);
    // Reverting back to candidate:0 restores clean state.
    expect(isReceiptDraftDirty(importedUnlinked, selectCandidateDraft(unlinkedOnly, candNip, 0), [candNip])).toBe(false);

    // Linked needs_review receipt: baseline is "", so choosing Keep, Unlink, or an alternate candidate is dirty.
    const linkedReview = detailOf({
      receipt_id: "dirty-linked",
      status: "needs_review",
      linked: true,
      linked_account_id: candNip.account_id,
      linked_entry_ref: candNip.entry_reference,
    });
    const linkedClean = draftFromDetail(linkedReview, [candNip, candAlt]);
    expect(isReceiptDraftDirty(linkedReview, linkedClean, [candNip, candAlt])).toBe(false);
    expect(isReceiptDraftDirty(linkedReview, selectKeepDraft(linkedClean, candNip), [candNip, candAlt])).toBe(true);
    expect(isReceiptDraftDirty(linkedReview, { ...linkedClean, linkChoice: "unlink" }, [candNip, candAlt])).toBe(true);
    expect(isReceiptDraftDirty(linkedReview, selectCandidateDraft(linkedClean, candAlt, 1), [candNip, candAlt])).toBe(true);
    expect(hasReceiptFieldOrLineEdits(linkedReview, selectKeepDraft(linkedClean, candNip))).toBe(false);
  });
});
