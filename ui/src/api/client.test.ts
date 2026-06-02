import { describe, expect, it } from "vitest";

import { apiBase } from "./client";

describe("apiBase", () => {
  it("resolves the API root one level above the /ui/ page", () => {
    expect(apiBase("http://host:8927/ui/").href).toBe("http://host:8927/api/");
  });
  it("preserves a reverse-proxy subpath", () => {
    expect(apiBase("https://host/hestia/ui/").href).toBe("https://host/hestia/api/");
  });
});
