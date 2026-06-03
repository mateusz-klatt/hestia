import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderViewSwitch, storedView, type ViewName, type ViewSwitchEls } from "./view";

// jsdom in this runner does not provide a working localStorage, so stub a Map-backed one. (view.ts
// tolerates a missing/throwing store anyway — that resilience is exercised by the last test.)
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

function els(): ViewSwitchEls {
  return {
    switchBox: document.createElement("div"),
    roomsEl: document.createElement("section"),
    adminEl: document.createElement("section"),
  };
}

beforeEach(() => {
  vi.stubGlobal("localStorage", fakeStorage());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("storedView", () => {
  it("defaults to rooms with nothing stored", () => {
    expect(storedView()).toBe("rooms");
  });

  it("returns admin when that is the stored choice", () => {
    localStorage.setItem("hestia.view", "admin");
    expect(storedView()).toBe("admin");
  });

  it("treats any unknown stored value as the rooms default", () => {
    localStorage.setItem("hestia.view", "garbage");
    expect(storedView()).toBe("rooms");
  });
});

describe("renderViewSwitch", () => {
  it("builds two tabs and shows the rooms view by default (applied once on mount)", () => {
    const e = els();
    const changes: ViewName[] = [];
    renderViewSwitch(e, (v) => changes.push(v));
    const tabs = e.switchBox.querySelectorAll("button");
    expect(tabs).toHaveLength(2);
    expect(e.roomsEl.hidden).toBe(false);
    expect(e.adminEl.hidden).toBe(true);
    expect(changes).toEqual(["rooms"]);
    expect(tabs[0]?.getAttribute("aria-pressed")).toBe("true");
    expect(tabs[1]?.getAttribute("aria-pressed")).toBe("false");
  });

  it("restores the persisted admin view on mount", () => {
    localStorage.setItem("hestia.view", "admin");
    const e = els();
    renderViewSwitch(e, () => undefined);
    expect(e.adminEl.hidden).toBe(false);
    expect(e.roomsEl.hidden).toBe(true);
  });

  it("clicking a tab switches the view, persists it, and notifies onChange", () => {
    const e = els();
    const changes: ViewName[] = [];
    renderViewSwitch(e, (v) => changes.push(v));
    e.switchBox.querySelectorAll("button")[1]?.click(); // 🔧 Zaawansowane
    expect(e.adminEl.hidden).toBe(false);
    expect(e.roomsEl.hidden).toBe(true);
    expect(localStorage.getItem("hestia.view")).toBe("admin");
    expect(changes).toEqual(["rooms", "admin"]);
  });

  it("the returned apply switches the view programmatically", () => {
    const e = els();
    const apply = renderViewSwitch(e, () => undefined);
    apply("admin");
    expect(e.adminEl.hidden).toBe(false);
    apply("rooms");
    expect(e.roomsEl.hidden).toBe(false);
    expect(e.adminEl.hidden).toBe(true);
  });

  it("survives a throwing localStorage (Safari private mode) — defaults to rooms, no throw", () => {
    vi.stubGlobal("localStorage", {
      getItem: () => {
        throw new Error("storage denied");
      },
      setItem: () => {
        throw new Error("storage denied");
      },
      clear: () => undefined,
      removeItem: () => undefined,
      key: () => null,
      length: 0,
    });
    const e = els();
    expect(() => renderViewSwitch(e, () => undefined)).not.toThrow();
    expect(e.roomsEl.hidden).toBe(false); // defaulted to rooms despite the storage failure
  });
});
