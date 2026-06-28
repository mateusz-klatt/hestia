import { describe, expect, it } from "vitest";

import { coverPercent, coverValue } from "./cover";

describe("blind cover scale", () => {
  it("maps the operator's eyeball anchors (curved, with a bottom cut-off)", () => {
    expect(coverPercent(0)).toBe(0); // fully closed, opaque
    expect(coverPercent(5)).toBe(1); // dead-zone 1..9 reads as the 1 % crack
    expect(coverPercent(10)).toBe(1); // first see-through crack
    expect(coverPercent(50)).toBe(33); // looks ~1/3 open
    expect(coverPercent(64)).toBe(50); // looks ~half open
    expect(coverPercent(99)).toBe(100); // fully open

    expect(coverValue(0)).toBe(0); // closed
    expect(coverValue(1)).toBe(10); // first step above closed → the crack (never wire 1..9)
    expect(coverValue(50)).toBe(64); // drag to half → physically ~half
    expect(coverValue(100)).toBe(99); // fully open
  });

  it("never commands the dead-zone (wire 1..9)", () => {
    for (let p = 0; p <= 100; p++) {
      const w = coverValue(p);
      expect(w === 0 || w >= 10).toBe(true);
    }
  });

  it("is monotonic (non-decreasing) in both directions so the slider can't invert", () => {
    let prevW = -1;
    for (let p = 0; p <= 100; p++) {
      const w = coverValue(p);
      expect(w).toBeGreaterThanOrEqual(prevW);
      prevW = w;
    }
    let prevP = -1;
    for (let w = 0; w <= 99; w++) {
      const p = coverPercent(w);
      expect(p).toBeGreaterThanOrEqual(prevP);
      prevP = p;
    }
  });

  it("clamps out-of-range input at both ends", () => {
    expect(coverPercent(-5)).toBe(0);
    expect(coverPercent(200)).toBe(100);
    expect(coverValue(-5)).toBe(0);
    expect(coverValue(200)).toBe(99);
  });
});
