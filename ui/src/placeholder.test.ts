import { describe, expect, it } from "vitest";

import { placeholderText } from "./placeholder";

describe("placeholderText", () => {
  it("falls back for blank labels", () => {
    expect(placeholderText("   ")).toBe("TypeScript dashboard shell");
  });
});
