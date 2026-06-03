import { afterEach, describe, expect, it, vi } from "vitest";

import { localeOverride, setLocaleOverride, setTempScale, tempScale } from "./prefs";

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

describe("prefs", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("round-trips the locale override and temperature scale, returning true on a persisted write", () => {
    vi.stubGlobal("localStorage", fakeStorage());
    expect(localeOverride()).toBeNull();
    expect(tempScale()).toBe("C");
    expect(setLocaleOverride("pl")).toBe(true);
    expect(setTempScale("F")).toBe(true);
    expect(localeOverride()).toBe("pl");
    expect(tempScale()).toBe("F");
  });

  it("falls back to Celsius for an unknown stored scale", () => {
    const m = new Map([["hestia.tempScale", "X"]]);
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => m.get(k) ?? null,
      setItem: () => undefined,
      removeItem: () => undefined,
      clear: () => undefined,
      key: () => null,
      length: 1,
    });
    expect(tempScale()).toBe("C");
  });

  it("is throw-safe: a throwing localStorage yields defaults and a false (unpersisted) write", () => {
    vi.stubGlobal("localStorage", {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("denied");
      },
      removeItem: () => undefined,
      clear: () => undefined,
      key: () => null,
      length: 0,
    });
    expect(localeOverride()).toBeNull();
    expect(tempScale()).toBe("C");
    expect(setLocaleOverride("pl")).toBe(false); // not persisted → caller won't reload
    expect(setTempScale("F")).toBe(false);
  });
});
