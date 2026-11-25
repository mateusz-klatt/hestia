import { describe, expect, it } from "vitest";
import { loadLocale } from "../../i18n";
import { CONSENT_STATUS_KEYS, RULE_FIELD_KEYS, accountName, annotationRequest, canPair, canonicalAmount, clampPercentString, counterparty, currentMonth,
  counterpartyRuleCandidate, daysBetween, formatRatioHalfEven, formatScaledBigInt, hasAnalytics, isMonth, isPositiveDecimal, isZero, kindChoice,
  kindChoicesFor, labelOf, legsMatch, limitCurrencies, manualPairRequest, monthLabel, monthlyBudgetCurrencies, mortgageCurrencies,
  pairCandidates, pairDate, pairPeriods, parseIntInRange, parseScaledInt, parseWarningPercent, pluralPl, ratioPercentFloor,
  recurringCurrencies, ruleCandidate, shiftMonth, transactionRowKey, transferActions, transferDecision, validCandidate,
  isScheduledForecastItem, validCurrency, validIsoDate, validLimitAmount, validMortgageLabel } from "./model";
import type { AnalyticsRule, AnalyticsTransaction } from "./model";
import { entry, leg, mortgageSchedule, pair, recurringHint, spendingLimit } from "./fixtures";
import { account, connection } from "../fixtures";

const capabilities = { stored_reads: true, account_controls: true, refresh: false, background_selection: true,
  background_polling: false, consent: false, analytics: false };

describe("analysis month helpers", () => {
  it("offers analytics only on an explicit true", () => {
    expect(hasAnalytics(null)).toBe(false);
    expect(hasAnalytics(capabilities)).toBe(false);
    // An older server omits the field and a malformed one may send something other than a boolean.
    expect(hasAnalytics({ ...capabilities, analytics: "yes" } as unknown as typeof capabilities)).toBe(false);
    expect(hasAnalytics({ ...capabilities, analytics: true })).toBe(true);
  });

  it("recognises a zero decimal string without converting it", () => {
    for (const zero of ["0", "0.00", "-0", "000.000"]) expect(isZero(zero)).toBe(true);
    for (const other of ["0.000001", "1", "-0.1", "", "0.", "abc"]) expect(isZero(other)).toBe(false);
  });

  it("accepts only real YYYY-MM keys", () => {
    expect(isMonth("2026-11")).toBe(true);
    for (const bad of ["2026-13", "2026-00", "2026-1", "26-11", "2026-11-01", ""]) expect(isMonth(bad)).toBe(false);
  });

  it("takes the local calendar month", () => {
    expect(currentMonth(new Date(2026, 0, 1, 0, 30))).toBe("2026-01");
    expect(currentMonth(new Date(2026, 11, 31, 23, 59))).toBe("2026-12");
  });

  it("shifts across year boundaries and leaves invalid keys alone", () => {
    expect(shiftMonth("2026-01", -1)).toBe("2025-12");
    expect(shiftMonth("2026-12", 1)).toBe("2027-01");
    expect(shiftMonth("2026-11", -23)).toBe("2024-12");
    expect(shiftMonth("nope", 1)).toBe("nope");
  });

  it("names the month in the page locale", () => {
    expect(monthLabel("2026-11", "en")).toBe("November 2026");
    expect(monthLabel("2026-11", "pl")).toMatch(/listopad 2026/);
    expect(monthLabel("bad", "en")).toBe("bad");
  });
});

describe("manual own-transfer pairing", () => {
  const credit = (reference: string, changes: Partial<AnalyticsTransaction> = {}) => entry(reference, {
    account_id: "acc-2", credit_debit_indicator: "CRDT", amount: "0100.00", booking_date: "2026-11-10",
    effective: { kind: "income", category_id: null, source: "default", rule_id: null }, ...changes,
  });
  const debit = () => entry("debit", { amount: "100.00", booking_date: "2026-11-10" });
  const transfer = { status: "none", decision_id: null, candidate_count: 0, ambiguous: false };

  it("canonicalizes valid decimal strings textually and rejects every other shape", () => {
    expect(canonicalAmount("0100.50")).toBe("100.5");
    expect(canonicalAmount("100.00")).toBe("100");
    expect(canonicalAmount("0.0")).toBe("0");
    expect(canonicalAmount("000")).toBe("0");
    expect(canonicalAmount("001.2300")).toBe("1.23");
    expect(canonicalAmount("10")).toBe("10");
    for (const invalid of ["", "abc", "-1", "+1", "1.", ".5", "1,2", "1.2.3", " 1", "1e2"]) {
      expect(canonicalAmount(invalid)).toBeNull();
    }
  });

  it("canonicalizes and matches a 30-digit integer without numeric conversion", () => {
    const amount = "000123456789012345678901234567890.10";
    const canonical = "123456789012345678901234567890.1";
    expect(canonicalAmount(amount)).toBe(canonical);
    expect(pairCandidates(entry("long-debit", { amount, booking_date: "2026-11-10" }), [credit("long-credit", { amount: canonical })]))
      .toEqual([credit("long-credit", { amount: canonical })]);
  });

  it("uses booking then value dates and measures absolute UTC day distances", () => {
    expect(pairDate(entry("a", { booking_date: "2026-11-10", value_date: "2026-11-09" }))).toBe("2026-11-10");
    expect(pairDate(entry("b", { booking_date: null, value_date: "2026-11-09" }))).toBe("2026-11-09");
    expect(pairDate(entry("c", { booking_date: null, value_date: null, transaction_date: "2026-11-10" }))).toBeNull();
    expect(daysBetween("2026-11-10", "2026-11-10")).toBe(0);
    expect(daysBetween("2026-11-13", "2026-11-10")).toBe(3);
    expect(daysBetween("2026-12-31", "2027-01-01")).toBe(1);
  });

  it("covers both months whenever the three-day window crosses a month or year", () => {
    expect(pairPeriods("2026-11-10")).toEqual(["2026-11"]);
    expect(pairPeriods("2026-11-02")).toEqual(["2026-10", "2026-11"]);
    expect(pairPeriods("2026-11-29")).toEqual(["2026-11", "2026-12"]);
    expect(pairPeriods("2026-12-31")).toEqual(["2026-12", "2027-01"]);
    expect(pairPeriods("2026-01-01")).toEqual(["2025-12", "2026-01"]);
  });

  it("handles leap-year and non-leap-year windows across February", () => {
    expect(pairPeriods("2028-02-28")).toEqual(["2028-02", "2028-03"]);
    expect(pairPeriods("2028-03-01")).toEqual(["2028-02", "2028-03"]);
    expect(daysBetween("2028-02-28", "2028-03-01")).toBe(2);
    expect(daysBetween("2027-02-28", "2027-03-01")).toBe(1);
  });

  it("offers only acceptable, deduplicated credits and compares amounts canonically", () => {
    const rows = [
      credit("ok"), credit("ok"),
      credit("direction", { credit_debit_indicator: "DBIT" }),
      credit("currency", { currency: "EUR" }),
      credit("amount", { amount: "100.01" }),
      credit("account", { account_id: "acc-1" }),
      credit("far", { booking_date: "2026-11-14" }),
      credit("accepted", { transfer: { ...transfer, status: "accepted" } }),
      credit("review", { transfer: { ...transfer, status: "review" } }),
      credit("boundary", { transfer: { ...transfer, status: "boundary" } }),
      credit("kind", { effective: { kind: "transfer", category_id: null, source: "default", rule_id: null } }),
      credit("date", { booking_date: null, value_date: null, transaction_date: "2026-11-10" }),
    ];
    expect(pairCandidates(debit(), rows).map((row) => row.entry_reference)).toEqual(["ok"]);
    expect(pairCandidates(entry("bad", { amount: "100.00", booking_date: null }), rows)).toEqual([]);
    expect(pairCandidates(entry("bad", { amount: "not-decimal" }), rows)).toEqual([]);
  });

  it("orders candidates by distance, date, account and reference", () => {
    const rows = [
      credit("later", { account_id: "acc-3", booking_date: "2026-11-11" }),
      credit("b", { booking_date: "2026-11-09" }),
      credit("a", { booking_date: "2026-11-09" }),
      credit("same", { account_id: "acc-3" }),
      credit("same"),
    ];
    expect(pairCandidates(debit(), rows).map((row) => `${row.account_id}/${row.entry_reference}`)).toEqual([
      "acc-2/same", "acc-3/same", "acc-2/a", "acc-2/b", "acc-3/later",
    ]);
  });

  it("offers pairing only for eligible, non-zero debit rows", () => {
    expect(canPair(debit())).toBe(true);
    for (const status of ["none", "suggested", "rejected", "undone"] as const) {
      expect(canPair(debit())).toBe(true);
      expect(canPair(entry("debit", { transfer: { ...transfer, status } }))).toBe(true);
    }
    expect(canPair(credit("credit"))).toBe(false);
    expect(canPair(entry("zero", { amount: "000.00" }))).toBe(false);
    expect(canPair(entry("invalid", { amount: "-1" }))).toBe(false);
    expect(canPair(entry("dateless", { booking_date: null, value_date: null }))).toBe(false);
    expect(canPair(entry("kind", { effective: { kind: "transfer", category_id: null, source: "default", rule_id: null } }))).toBe(false);
    for (const status of ["accepted", "review", "boundary"] as const) {
      expect(canPair(entry("debit", { transfer: { ...transfer, status } }))).toBe(false);
    }
  });

  it("builds the exact first manual decision with both shown transfer fingerprints", () => {
    expect(manualPairRequest(debit(), credit("credit"))).toEqual({
      action: "accept", source: "manual", decision_id: null, expected_version: 0,
      debit: { account_id: "acc-1", entry_reference: "debit" },
      credit: { account_id: "acc-2", entry_reference: "credit" },
      expected_debit_fingerprint: "tfp-debit", expected_credit_fingerprint: "tfp-credit",
    });
  });
});

describe("analytics decisions", () => {
  it("never sends an explicit type with an automatic annotation", () => {
    const row = entry("a");
    expect(annotationRequest(row, { mode: "auto" }, "income")).toMatchObject({ mode: "auto", category_id: null, kind: "auto" });
    expect(annotationRequest(row, { mode: "manual", category_id: null }, "income")).toMatchObject({ mode: "manual", kind: "income" });
    expect(kindChoice("refund")).toBe("refund");
    expect(kindChoice("future_kind")).toBe("auto");
    expect(kindChoicesFor("DBIT")).toEqual(["auto", "expense", "excluded"]);
    expect(kindChoicesFor("CRDT")).toEqual(["auto", "income", "refund", "excluded"]);
    expect(kindChoice("refund", "DBIT")).toBe("auto");
    expect(kindChoice("expense", "CRDT")).toBe("auto");
    expect(kindChoice("excluded", "CRDT")).toBe("excluded");
  });

  it("names the other party from the bank metadata by direction", () => {
    const metadata = { creditor_name: "Synthetic Payee", debtor_name: "Synthetic Payer", mcc: null };
    expect(counterparty(entry("a", { metadata }))).toBe("Synthetic Payee");
    expect(counterparty(entry("b", { metadata, credit_debit_indicator: "CRDT" }))).toBe("Synthetic Payer");
    expect(counterparty(entry("c"))).toBeNull();
  });

  it("builds counterparty-account drafts without a raw identifier", () => {
    const key = "c".repeat(64);
    const categorized = entry("account", { counterparty_account_key: key, counterparty_account: "PL** … 1234",
      effective: { kind: "expense", category_id: "cat-food", source: "manual", rule_id: null } });
    expect(counterpartyRuleCandidate(categorized)).toMatchObject({ field: "counterparty_account", operator: "equals",
      counterparty_account_key: key, value: "PL** … 1234", category_id: "cat-food", kind: "auto" });
    const credit = entry("credit", { credit_debit_indicator: "CRDT", counterparty_account_key: key,
      counterparty_account: "PL** … 1234" });
    expect(counterpartyRuleCandidate(credit)?.kind).toBe("income");
    expect(counterpartyRuleCandidate(entry("missing"))).toBeNull();
    expect(counterpartyRuleCandidate(entry("bad-key", { counterparty_account_key: "not-a-key",
      counterparty_account: "PL** … 1234" }))).toBeNull();
    const saved: AnalyticsRule = { id: "rule-account", version: 3, field: "counterparty_account", operator: "equals",
      value: "PL** … 1234", counterparty_account_key: key, category_id: "cat-food", kind: "auto", priority: 20,
      enabled: true, created_at: 1, updated_at: 2 };
    const draft = ruleCandidate(saved);
    expect(draft.counterparty_account_key).toBe(key);
    expect(validCandidate(draft)).toBe(true);
    expect(validCandidate({ ...draft, operator: "contains" })).toBe(false);
  });

  it("offers only the actions each transfer state allows", () => {
    expect(transferActions(pair())).toEqual(["accept", "reject"]);
    expect(transferActions(pair({ boundary: true }))).toEqual(["reject"]);
    expect(transferActions(pair({ status: "review", id: "d" }))).toEqual(["accept", "undo"]);
    expect(transferActions(pair({ status: "review", id: "d", boundary: true }))).toEqual(["undo"]);
    expect(transferActions(pair({ status: "boundary" }))).toEqual(["reject"]);
    expect(transferActions(pair({ status: "boundary", id: "d" }))).toEqual(["undo"]);
    expect(transferActions(pair({ status: "accepted", id: "d" }))).toEqual(["undo"]);
    expect(transferActions(pair({ status: "rejected", id: "d" }))).toEqual(["accept"]);
    expect(transferActions(pair({ status: "undone", id: "d" }))).toEqual(["accept"]);
    expect(transferActions(pair({ status: "undone", id: "d", boundary: true }))).toEqual([]);
  });

  it("records reconfirming a review, rejected or undone pair as the owner's manual choice", () => {
    expect(transferDecision(pair({ source: "heuristic" }), "accept")).toMatchObject({ source: "heuristic" });
    // The shown source of a pair to re-check is historical; the current evidence may say otherwise in either direction.
    for (const source of ["heuristic", "account_identity"]) {
      expect(transferDecision(pair({ id: "d", version: 2, status: "review", source,
        debit: { ...pair().debit, fingerprint: "pfp-now-out" }, credit: { ...pair().credit, fingerprint: "pfp-now-in" } }), "accept"))
        .toMatchObject({ source: "manual", decision_id: "d", expected_version: 2, expected_debit_fingerprint: "pfp-now-out",
          expected_credit_fingerprint: "pfp-now-in" });
    }
    for (const status of ["rejected", "undone"] as const) {
      expect(transferDecision(pair({ id: "d", version: 5, status, source: "account_identity" }), "accept"))
        .toMatchObject({ action: "accept", source: "manual", expected_version: 5 });
    }
    expect(transferDecision(pair({ status: "rejected", source: "heuristic" }), "reject")).toMatchObject({ source: "heuristic" });
  });

  it("offers acceptance only while both legs still match", () => {
    expect(legsMatch(pair())).toBe(true);
    for (const broken of [pair({ credit: leg("acc-2", "in-1", { amount: "1200.01" }) }), pair({ credit: leg("acc-2", "in-1", { currency: "EUR" }) }),
      pair({ date_distance: 4 }), pair({ date_distance: null }), pair({ credit: leg("acc-2", "in-1", { selected: false }) }),
      pair({ debit: leg("acc-1", "out-1", { amount: null, currency: null }) })]) {
      expect(legsMatch(broken)).toBe(false);
    }
    const changed = { credit: leg("acc-2", "in-1", { amount: "100.01" }) };
    expect(transferActions(pair({ ...changed, id: "d", status: "review" }))).toEqual(["undo"]);
    expect(transferActions(pair({ ...changed, id: "d", status: "undone" }))).toEqual([]);
    expect(transferActions(pair({ ...changed }))).toEqual(["reject"]);
  });

  it("names an account with its bank so equal fallback labels stay distinguishable", () => {
    const accounts = [account({ id: "a", connection: "bank" }), account({ id: "b", connection: "other" })];
    const connections = [connection, { ...connection, id: "other", bank_name: "Other Synthetic Bank" }];
    expect(accountName(accounts, connections, "a", "Account")).toBe("Synthetic Bank · Account 1");
    expect(accountName(accounts, connections, "b", "Account")).toBe("Other Synthetic Bank · Account 1");
    expect(accountName(accounts, [], "a", "Account")).toBe("Account 1");
    expect(accountName(accounts, connections, "missing", "Account")).toBe("Account");
  });

  it("cannot undo a pair nobody decided on", () => {
    expect(transferDecision(pair(), "undo")).toBeNull();
    expect(transferDecision(pair({ id: "d", version: 3 }), "undo")).toEqual({ action: "undo", source: "heuristic", decision_id: "d",
      debit: null, credit: null, expected_debit_fingerprint: null, expected_credit_fingerprint: null, expected_version: 3 });
    expect(transferDecision(pair({ id: "d", source: "future_source" }), "undo")).toMatchObject({ source: "manual" });
    expect(transferActions(pair({ status: "future_status" }))).toEqual([]);
  });

  it("extracts sorted unique currencies from recurring hints and pluralizes Polish integers accurately", () => {
    expect(recurringCurrencies([
      recurringHint({ currency: "USD" }),
      recurringHint({ currency: "PLN" }),
      recurringHint({ currency: "EUR" }),
      recurringHint({ currency: "PLN" }),
    ])).toEqual(["EUR", "PLN", "USD"]);
    expect(recurringCurrencies([])).toEqual([]);

    const days = (n: number) => `${String(n)} ${pluralPl(n, "dzień", "dni", "dni")}`;
    expect([0, 1, 2, 4, 5, 12, 22].map(days)).toEqual([
      "0 dni", "1 dzień", "2 dni", "4 dni", "5 dni", "12 dni", "22 dni",
    ]);
    const months = (n: number) => `${String(n)} ${pluralPl(n, "miesiąc", "miesiące", "miesięcy")}`;
    expect([0, 1, 2, 3, 4, 5, 11, 12, 14, 21, 22, 24, 25].map(months)).toEqual([
      "0 miesięcy", "1 miesiąc", "2 miesiące", "3 miesiące", "4 miesiące", "5 miesięcy",
      "11 miesięcy", "12 miesięcy", "14 miesięcy", "21 miesięcy", "22 miesiące", "24 miesiące", "25 miesięcy",
    ]);
    const occ = (n: number) => `${String(n)} ${pluralPl(n, "wystąpienie", "wystąpienia", "wystąpień")}`;
    expect([1, 2, 3, 5, 13, 23].map(occ)).toEqual([
      "1 wystąpienie", "2 wystąpienia", "3 wystąpienia", "5 wystąpień", "13 wystąpień", "23 wystąpienia",
    ]);
  });

  it("validates spending limit inputs textually without floating-point coercion", () => {
    expect(validLimitAmount("500")).toBe(true);
    expect(validLimitAmount(" 12345678901234567890.05 ")).toBe(true);
    expect(validLimitAmount("0")).toBe(false);
    expect(validLimitAmount("0.00")).toBe(false);
    expect(validLimitAmount("-50")).toBe(false);
    expect(validLimitAmount("1e5")).toBe(false);
    expect(validLimitAmount("1".repeat(81))).toBe(false);
    expect(validCurrency("pln")).toBe(true);
    expect(validCurrency("EUR")).toBe(true);
    expect(validCurrency("PL")).toBe(false);
    expect(validCurrency("123")).toBe(false);
    expect(parseWarningPercent("1")).toBe(1);
    expect(parseWarningPercent("80")).toBe(80);
    expect(parseWarningPercent("99")).toBe(99);
    expect(parseWarningPercent("0")).toBeNull();
    expect(parseWarningPercent("100")).toBeNull();
    expect(parseWarningPercent("80.5")).toBeNull();
    expect(parseWarningPercent("")).toBeNull();
    expect(limitCurrencies(
      [spendingLimit({ currency: "EUR" }), spendingLimit({ id: "lim-2", currency: "PLN" })],
      [account({ display: { product: "Synthetic", currency: "USD", masked_suffix: "0001", source: "api", observed_at: 1, imported_at: 2 } })],
    )).toEqual(["EUR", "PLN", "USD"]);
  });

  it("validates mortgage labels (including NFC length), integer bounds and real calendar dates", () => {
    expect(validMortgageLabel("Synthetic Loan")).toBe(true);
    expect(validMortgageLabel("   ")).toBe(false);
    expect(validMortgageLabel("bad\nline")).toBe(false);
    // Rejects Unicode Cf (zero-width) characters matching server Cc/Cf/Cs check.
    expect(validMortgageLabel("zero\u200bwidth")).toBe(false);
    // Counts Unicode code points rather than UTF-16 code units (>128 astral emoji still valid up to 256).
    expect(validMortgageLabel("🏠".repeat(130))).toBe(true);
    expect(validMortgageLabel("🏠".repeat(257))).toBe(false);
    // U+0958 decomposes under NFC to 2 code points (200 -> 400 > 256).
    expect(validMortgageLabel("\u0958".repeat(200))).toBe(false);
    expect(validMortgageLabel("\u0958".repeat(128))).toBe(true);
    expect(parseIntInRange("1", 1, 31)).toBe(1);
    expect(parseIntInRange("31", 1, 31)).toBe(31);
    expect(parseIntInRange("0", 1, 31)).toBeNull();
    expect(parseIntInRange("0", 0, 31)).toBe(0);
    expect(parseIntInRange("32", 0, 31)).toBeNull();
    expect(parseIntInRange("5.5", 0, 31)).toBeNull();
    expect(validIsoDate("2024-02-29")).toBe(true);
    expect(validIsoDate("2026-02-29")).toBe(false);
    expect(validIsoDate("2026-02-30")).toBe(false);
    expect(validIsoDate("not-a-date")).toBe(false);
    expect(mortgageCurrencies(
      [mortgageSchedule({ currency: "EUR" })],
      [account({ display: { product: "Synthetic", currency: "PLN", masked_suffix: "0001", source: "api", observed_at: 1, imported_at: 2 } })],
    )).toEqual(["EUR", "PLN"]);
  });

  it("evaluates decimal signs, BigInt half-even ratios, clamped percents and monthly budget currencies", () => {
    expect(isPositiveDecimal("0.01")).toBe(true);
    expect(isPositiveDecimal("100")).toBe(true);
    expect(isPositiveDecimal("0")).toBe(false);
    expect(isPositiveDecimal("0.00")).toBe(false);
    expect(isPositiveDecimal("-5")).toBe(false);
    expect(isPositiveDecimal("bad")).toBe(false);

    expect(formatRatioHalfEven({ numerator: "0.05", denominator: 2 }, "PLN", "en-US")).toBe("0.02 PLN");
    expect(formatRatioHalfEven({ numerator: "0.07", denominator: 2 }, "PLN", "en-US")).toBe("0.04 PLN");
    expect(formatRatioHalfEven({ numerator: "0.071", denominator: 2 }, "PLN", "en-US")).toBe("0.04 PLN");
    expect(formatRatioHalfEven({ numerator: "0", denominator: 5 }, "PLN", "en-US")).toBe("0.00 PLN");
    expect(formatRatioHalfEven({ numerator: "-10.05", denominator: 2 }, "EUR", "en-US")).toBe("−5.02 EUR");
    expect(formatRatioHalfEven({ numerator: "12345678901234567890.05", denominator: 2 }, "PLN", "en-US"))
      .toBe("6,172,839,450,617,283,945.02 PLN");
    expect(formatRatioHalfEven({ numerator: "bad", denominator: 2 }, "PLN", "en-US")).toBe("bad PLN");
    expect(formatRatioHalfEven({ numerator: "10", denominator: 0 }, "PLN", "en-US")).toBe("10 PLN");

    expect(ratioPercentFloor("250", "1000")).toBe("25");
    expect(ratioPercentFloor("1500", "1000")).toBe("150");
    expect(ratioPercentFloor("12345678901234567890123", "1")).toBe("1234567890123456789012300");
    expect(ratioPercentFloor("-10", "1000")).toBe("0");
    expect(ratioPercentFloor("10", "0")).toBe("0");
    expect(ratioPercentFloor("10", "-50")).toBe("0");
    expect(ratioPercentFloor("bad", "100")).toBe("0");

    expect(clampPercentString("0")).toBe(0);
    expect(clampPercentString("64")).toBe(64);
    expect(clampPercentString("100")).toBe(100);
    expect(clampPercentString("99999999999999999999999")).toBe(100);
    expect(clampPercentString("-5")).toBe(0);

    expect(monthlyBudgetCurrencies(["PLN", "eur", "EURO", "PLN"], [
      account({ display: { product: "Main", currency: "CHF", masked_suffix: "0001", source: "api", observed_at: 1, imported_at: 2 },
        observed_balance_currencies: ["USD", "PLN", "123"] }),
      account({ id: "acc-2", display: null }),
    ])).toEqual(["PLN", "EUR", "CHF", "USD"]);
    expect(monthlyBudgetCurrencies([])).toEqual(["PLN"]);

    expect(parseScaledInt("0")).toEqual({ negative: false, coeff: 0n, scale: 0 });
    expect(parseScaledInt("-123.450")).toEqual({ negative: true, coeff: 123450n, scale: 3 });
    expect(parseScaledInt("bad")).toBeNull();
    expect(formatScaledBigInt(0n, 2)).toBe("0");
    expect(formatScaledBigInt(-123450n, 3)).toBe("-123.45");
    expect(formatScaledBigInt(42n, 0)).toBe("42");
  });

  it("builds stable account/reference keys for transactions", () => {
    expect(transactionRowKey(entry("e-42"))).toBe("acc-1/e-42");
    expect(transactionRowKey(entry("ref-9", { account_id: "card-2" }))).toBe("card-2/ref-9");
  });

  it("resolves server code maps through the active i18n catalog and preserves raw fallback", async () => {
    try {
      await loadLocale("en");
      expect(labelOf(CONSENT_STATUS_KEYS, "healthy")).toBe("Healthy");
      expect(labelOf(RULE_FIELD_KEYS, "counterparty_account")).toBe("counterparty account (IBAN)");
      expect(labelOf(CONSENT_STATUS_KEYS, "future_server_code")).toBe("future_server_code");

      await loadLocale("pl");
      expect(labelOf(CONSENT_STATUS_KEYS, "healthy")).toBe("Aktywna");
      expect(labelOf(RULE_FIELD_KEYS, "counterparty_account")).toBe("konto kontrahenta (IBAN)");
      expect(labelOf(CONSENT_STATUS_KEYS, "future_server_code")).toBe("future_server_code");
    } finally {
      await loadLocale("en");
    }
  });

  it("distinguishes scheduled forecast items from estimates", () => {
    expect(isScheduledForecastItem({ source: "mortgage", confidence: "scheduled" })).toBe(true);
    expect(isScheduledForecastItem({ source: "recurring", confidence: "high" })).toBe(false);
    expect(isScheduledForecastItem({ source: "recurring", confidence: "medium" })).toBe(false);
    expect(isScheduledForecastItem({ source: "category_baseline", confidence: "estimated" })).toBe(false);
  });
});
