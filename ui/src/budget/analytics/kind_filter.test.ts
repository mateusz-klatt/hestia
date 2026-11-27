import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  KIND_FILTER_EXCLUDE_SKIPPED,
  KIND_FILTER_STORAGE_KEY,
  SKIP_IN_BUDGET_KINDS,
  isSkipInBudgetKind,
  readKindFilterPreference,
  writeKindFilterPreference,
} from "./kind_filter";

describe("month kind filter helpers", () => {
  beforeEach(() => {
    try {
      localStorage.removeItem(KIND_FILTER_STORAGE_KEY);
    } catch {
      // Ignore when storage is unavailable.
    }
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    try {
      localStorage.removeItem(KIND_FILTER_STORAGE_KEY);
    } catch {
      // Ignore when storage is unavailable.
    }
  });

  it("identifies skip-in-budget effective kinds from the model", () => {
    expect(SKIP_IN_BUDGET_KINDS).toEqual(["transfer", "excluded"]);
    expect(isSkipInBudgetKind("transfer")).toBe(true);
    expect(isSkipInBudgetKind("excluded")).toBe(true);
    for (const kept of ["expense", "income", "refund", "unclassified_credit"]) {
      expect(isSkipInBudgetKind(kept)).toBe(false);
    }
  });

  it("defaults to excluding skipped kinds, persists valid selections, and ignores invalid stored values", () => {
    expect(readKindFilterPreference()).toBe(KIND_FILTER_EXCLUDE_SKIPPED);

    writeKindFilterPreference("");
    expect(readKindFilterPreference()).toBe("");

    writeKindFilterPreference("refund");
    expect(readKindFilterPreference()).toBe("refund");

    writeKindFilterPreference(KIND_FILTER_EXCLUDE_SKIPPED);
    expect(readKindFilterPreference()).toBe(KIND_FILTER_EXCLUDE_SKIPPED);

    localStorage.setItem(KIND_FILTER_STORAGE_KEY, "invalid_value");
    expect(readKindFilterPreference()).toBe(KIND_FILTER_EXCLUDE_SKIPPED);
  });

  it("falls back cleanly to default when localStorage throws", () => {
    vi.stubGlobal("localStorage", {
      getItem: () => { throw new Error("blocked"); },
      setItem: () => { throw new Error("blocked"); },
      removeItem: () => { throw new Error("blocked"); },
    });
    expect(readKindFilterPreference()).toBe(KIND_FILTER_EXCLUDE_SKIPPED);
    expect(() => { writeKindFilterPreference(""); }).not.toThrow();
  });
});
