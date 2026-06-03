import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { loadLocale } from "./i18n";
import { reconcileServerSettings } from "./settings";

function fakeStorage(): Storage {
  const m = new Map<string, string>();
  return {
    get length() {
      return m.size;
    },
    clear: () => {
      m.clear();
    },
    getItem: (k: string) => m.get(k) ?? null,
    key: (i: number) => [...m.keys()][i] ?? null,
    removeItem: (k: string) => m.delete(k),
    setItem: (k: string, v: string) => m.set(k, v),
  };
}

describe("reconcileServerSettings", () => {
  beforeEach(async () => {
    vi.stubGlobal("localStorage", fakeStorage());
    await loadLocale("en");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("writes server values into the local cache when they differ", () => {
    expect(reconcileServerSettings({ locale: "pl", temp_scale: "F", theme: null })).toBe(true);
    expect(localStorage.getItem("hestia.locale")).toBe("pl");
    expect(localStorage.getItem("hestia.tempScale")).toBe("F");
  });

  it("does nothing when server settings already match the effective values", () => {
    localStorage.setItem("hestia.tempScale", "K");
    expect(reconcileServerSettings({ locale: "en", temp_scale: "K", theme: null })).toBe(false);
  });

  it("ignores absent settings and unset server fields", () => {
    expect(reconcileServerSettings(null)).toBe(false);
    expect(reconcileServerSettings({ locale: null, temp_scale: null, theme: "dark" })).toBe(false);
    expect(localStorage.getItem("hestia.locale")).toBeNull();
    expect(localStorage.getItem("hestia.tempScale")).toBeNull();
  });

  it("reports no change when the local write can't persist (so boot can't reload-loop)", () => {
    // Storage that refuses writes (Safari private mode etc.): differing server values still
    // resolve to "no change" so main.ts won't reload forever against a cache it can't update.
    const store = fakeStorage();
    store.setItem = (): never => {
      throw new Error("QuotaExceeded");
    };
    vi.stubGlobal("localStorage", store);
    expect(reconcileServerSettings({ locale: "pl", temp_scale: "F", theme: null })).toBe(false);
  });
});
