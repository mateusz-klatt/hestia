import { describe, expect, it } from "vitest";
import { currentMonth, hasAnalysis, isMonth, monthLabel, shiftMonth } from "./model";

const capabilities = { stored_reads: true, account_controls: true, refresh: false, background_selection: true,
  background_polling: false, consent: false };

describe("analysis month helpers", () => {
  it("offers analysis only on an explicit true", () => {
    expect(hasAnalysis(null)).toBe(false);
    expect(hasAnalysis(capabilities)).toBe(false);
    expect(hasAnalysis({ ...capabilities, analysis: "yes" } as typeof capabilities)).toBe(false);
    expect(hasAnalysis({ ...capabilities, analysis: true } as typeof capabilities)).toBe(true);
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
