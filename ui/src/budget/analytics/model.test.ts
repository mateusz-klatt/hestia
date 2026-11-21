import { describe, expect, it } from "vitest";
import { annotationRequest, counterparty, currentMonth, kindChoice, hasAnalytics, isMonth, isZero, monthLabel, shiftMonth, transferActions, transferDecision } from "./model";
import { entry, pair } from "./fixtures";

const capabilities = { stored_reads: true, account_controls: true, refresh: false, background_selection: true,
  background_polling: false, consent: false };

describe("analysis month helpers", () => {
  it("offers analytics only on an explicit true", () => {
    expect(hasAnalytics(null)).toBe(false);
    expect(hasAnalytics(capabilities)).toBe(false);
    expect(hasAnalytics({ ...capabilities, analytics: "yes" } as typeof capabilities)).toBe(false);
    expect(hasAnalytics({ ...capabilities, analytics: true } as typeof capabilities)).toBe(true);
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

describe("analytics decisions", () => {
  it("never sends an explicit type with an automatic annotation", () => {
    const row = entry("a");
    expect(annotationRequest(row, { mode: "auto" }, "income")).toMatchObject({ mode: "auto", category_id: null, kind: "auto" });
    expect(annotationRequest(row, { mode: "manual", category_id: null }, "income")).toMatchObject({ mode: "manual", kind: "income" });
    expect(kindChoice("refund")).toBe("refund");
    expect(kindChoice("future_kind")).toBe("auto");
  });

  it("names the other party from the bank metadata by direction", () => {
    const metadata = { creditor_name: "Synthetic Payee", debtor_name: "Synthetic Payer", mcc: null };
    expect(counterparty(entry("a", { metadata }))).toBe("Synthetic Payee");
    expect(counterparty(entry("b", { metadata, credit_debit_indicator: "CRDT" }))).toBe("Synthetic Payer");
    expect(counterparty(entry("c"))).toBeNull();
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

  it("cannot undo a pair nobody decided on", () => {
    expect(transferDecision(pair(), "undo")).toBeNull();
    expect(transferDecision(pair({ id: "d", version: 3 }), "undo")).toEqual({ action: "undo", source: "heuristic", decision_id: "d",
      debit: null, credit: null, expected_debit_fingerprint: null, expected_credit_fingerprint: null, expected_version: 3 });
    expect(transferDecision(pair({ id: "d", source: "future_source" }), "undo")).toMatchObject({ source: "manual" });
    expect(transferActions(pair({ status: "future_status" }))).toEqual([]);
  });
});
