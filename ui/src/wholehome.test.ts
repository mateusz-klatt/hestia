import { afterEach, describe, expect, it, vi } from "vitest";

import { device } from "./fixtures";
import { q, qa } from "./test-dom";
import { openWholeHomeConfig, type WholeHomeConfigDeps } from "./wholehome";

const flush = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));

function panel(): HTMLElement {
  return q(document, ".wholehome-card");
}

afterEach(() => {
  document.body.replaceChildren(); // drop any modal a test left open
});

describe("openWholeHomeConfig", () => {
  const devices = {
    "10": device({ type: "light", name: "Kuchnia" }), // included
    "2": device({ type: "light", name: "Kaganek" }), // opted out (see `excluded`)
    "5": device({ type: "blind", name: "Salon roleta" }),
    "9": device({ type: "thermostat", name: "Grzejnik" }), // not a sweep type → not listed
  };
  const excluded = new Set([2]); // Kaganek (node 2) opted out — sourced from GET /api/whole-home

  function open(over: Partial<WholeHomeConfigDeps> = {}): () => void {
    return openWholeHomeConfig({
      devices,
      excluded,
      excludedEndpoints: new Map(),
      setExcluded: vi.fn(),
      ...over,
    });
  }

  it("lists only lights and blinds, numeric-sorted, with a checkbox per device", () => {
    open();
    const names = qa<HTMLElement>(panel(), ".wholehome-name").map((el) => el.textContent);
    expect(names).toEqual(["Kaganek", "Kuchnia", "Salon roleta"]); // node 2 before 10; thermostat absent
    expect(qa(panel(), ".wholehome-row input").length).toBe(3);
  });

  it("checkbox reflects membership: included → checked, opted-out → unchecked", () => {
    open();
    const boxes = qa<HTMLInputElement>(panel(), ".wholehome-row input");
    expect(boxes[0]?.checked).toBe(false); // Kaganek (in the excluded set)
    expect(boxes[1]?.checked).toBe(true); // Kuchnia (not excluded)
  });

  it("unchecking a device opts it OUT (exclude = true, no ep)", async () => {
    const setExcluded = vi.fn<WholeHomeConfigDeps["setExcluded"]>().mockResolvedValue(true);
    open({ setExcluded });
    const kuchnia = qa<HTMLInputElement>(panel(), ".wholehome-row input")[1]; // included → checked
    if (kuchnia === undefined) throw new Error("no Kuchnia row");
    kuchnia.checked = false;
    kuchnia.dispatchEvent(new Event("change"));
    await flush();
    expect(setExcluded).toHaveBeenCalledWith(10, true);
    expect(q<HTMLElement>(panel(), ".status").textContent).toContain("Saved");
  });

  it("re-checking an opted-out device re-includes it (exclude = false)", async () => {
    const setExcluded = vi.fn<WholeHomeConfigDeps["setExcluded"]>().mockResolvedValue(true);
    open({ setExcluded });
    const kaganek = qa<HTMLInputElement>(panel(), ".wholehome-row input")[0]; // opted out → unchecked
    if (kaganek === undefined) throw new Error("no Kaganek row");
    kaganek.checked = true;
    kaganek.dispatchEvent(new Event("change"));
    await flush();
    expect(setExcluded).toHaveBeenCalledWith(2, false);
  });

  it("reverts the checkbox and reports an error when the save fails", async () => {
    const setExcluded = vi.fn<WholeHomeConfigDeps["setExcluded"]>().mockResolvedValue(false);
    open({ setExcluded });
    const kuchnia = qa<HTMLInputElement>(panel(), ".wholehome-row input")[1];
    if (kuchnia === undefined) throw new Error("no Kuchnia row");
    kuchnia.checked = false; // user unchecks
    kuchnia.dispatchEvent(new Event("change"));
    await flush();
    expect(kuchnia.checked).toBe(true); // reverted to its prior (included) state
    expect(q<HTMLElement>(panel(), ".status").classList.contains("err")).toBe(true);
  });

  it("shows an empty placeholder when there are no lights or blinds", () => {
    open({ devices: { "1": device({ type: "thermostat" }) } });
    expect(qa(panel(), ".wholehome-row").length).toBe(0);
    expect(q<HTMLElement>(panel(), ".room-placeholder")).toBeTruthy();
  });

  it("treats a rejected save as a failure (reverts, no stuck row)", async () => {
    const setExcluded = vi.fn<WholeHomeConfigDeps["setExcluded"]>().mockRejectedValue(new Error("network"));
    open({ setExcluded });
    const kuchnia = qa<HTMLInputElement>(panel(), ".wholehome-row input")[1];
    if (kuchnia === undefined) throw new Error("no Kuchnia row");
    kuchnia.checked = false;
    kuchnia.dispatchEvent(new Event("change"));
    await flush();
    expect(kuchnia.checked).toBe(true); // reverted
    expect(kuchnia.disabled).toBe(false); // never stuck disabled
    expect(q<HTMLElement>(panel(), ".status").classList.contains("err")).toBe(true);
  });

  it("closes on the Done button, a backdrop click, the close handle, and Escape", () => {
    const close = open();
    close(); // returned handle
    expect(document.querySelector(".wholehome-card")).toBeNull();

    open();
    q<HTMLButtonElement>(panel(), ".modal-cancel").click(); // Done button
    expect(document.querySelector(".wholehome-card")).toBeNull();

    open();
    q<HTMLElement>(document, ".modal-overlay").click(); // backdrop (target === overlay)
    expect(document.querySelector(".wholehome-card")).toBeNull();

    open();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(document.querySelector(".wholehome-card")).toBeNull();
  });

  it("invokes onClosed when dismissed", () => {
    const onClosed = vi.fn();
    const close = open({ onClosed });
    close();
    expect(onClosed).toHaveBeenCalledOnce();
  });
});

describe("openWholeHomeConfig — multi-gang switches", () => {
  // Node 12 = the 2-gang hall/nightlight switch: gang 1 labelled "Hol", gang 2 unlabelled.
  const devices = {
    "12": device({
      type: "light",
      name: "Przedpokój",
      endpoints: { "1": true, "2": false },
      endpoint_names: { "1": "Hol" },
    }),
    "3": device({ type: "light", name: "Kuchnia" }), // single-gang control row stays plain
  };

  function open(over: Partial<WholeHomeConfigDeps> = {}): () => void {
    return openWholeHomeConfig({
      devices,
      excluded: new Set<number>(),
      excludedEndpoints: new Map<number, Set<number>>(),
      setExcluded: vi.fn(),
      ...over,
    });
  }

  it("renders a device heading plus one row per gang (label from endpoint_names, else Gang N)", () => {
    open();
    expect(q<HTMLElement>(panel(), ".wholehome-device").textContent).toBe("Przedpokój");
    const gangNames = qa<HTMLElement>(panel(), ".wholehome-gang .wholehome-name").map((el) => el.textContent);
    expect(gangNames).toEqual(["Hol", "Gang 2"]); // ascending; unlabelled gang falls back
    expect(qa(panel(), ".wholehome-row input").length).toBe(3); // 2 gangs + the single-gang Kuchnia
  });

  it("a gang opted out via excludedEndpoints renders unchecked; others checked", () => {
    open({ excludedEndpoints: new Map([[12, new Set([2])]]) });
    const boxes = qa<HTMLInputElement>(panel(), ".wholehome-gang input");
    expect(boxes[0]?.checked).toBe(true); // Hol included
    expect(boxes[1]?.checked).toBe(false); // gang 2 (kaganek) opted out
  });

  it("a node-level exclusion shows every gang unchecked", () => {
    open({ excluded: new Set([12]) });
    const boxes = qa<HTMLInputElement>(panel(), ".wholehome-gang input");
    expect(boxes.map((b) => b.checked)).toEqual([false, false]);
  });

  it("toggling a gang posts {node, exclude, ep}", async () => {
    const setExcluded = vi.fn<WholeHomeConfigDeps["setExcluded"]>().mockResolvedValue(true);
    open({ setExcluded });
    const gang2 = qa<HTMLInputElement>(panel(), ".wholehome-gang input")[1];
    if (gang2 === undefined) throw new Error("no gang 2 row");
    gang2.checked = false; // opt the nightlight gang out
    gang2.dispatchEvent(new Event("change"));
    await flush();
    expect(setExcluded).toHaveBeenCalledWith(12, true, 2);
  });

  it("gang rows survive a sparse live map: labels and flagged gangs still enumerate", () => {
    // Live state only knows gang 1, but gang 2 is flagged excluded → BOTH rows must render
    // (otherwise the opt-out would be invisible and uncleanable from the panel).
    const sparse = {
      "12": device({ type: "light", name: "Przedpokój", endpoints: { "1": true }, endpoint_names: { "1": "Hol" } }),
    };
    open({ devices: sparse, excludedEndpoints: new Map([[12, new Set([2])]]) });
    const boxes = qa<HTMLInputElement>(panel(), ".wholehome-gang input");
    expect(boxes.length).toBe(2);
    expect(boxes.map((b) => b.checked)).toEqual([true, false]); // gang 2 shown excluded

    document.body.replaceChildren();
    // No live state at all, but both gangs labelled → still per-gang rows.
    const labelsOnly = {
      "12": device({ type: "light", name: "Przedpokój", endpoint_names: { "1": "Hol", "2": "Kaganek" } }),
    };
    open({ devices: labelsOnly });
    expect(qa(panel(), ".wholehome-gang input").length).toBe(2);
  });

  it("re-including gangs of a node-excluded device works in one open modal (no refetch)", async () => {
    // The server demotes the node flag on the first per-gang write, leaving the OTHER gangs
    // per-gang-excluded — exactly what the still-open panel already shows (unchecked). So checking
    // gang 1 and then gang 2 must just post two per-gang re-includes, with no stale-state surprises.
    const setExcluded = vi.fn<WholeHomeConfigDeps["setExcluded"]>().mockResolvedValue(true);
    open({ excluded: new Set([12]), setExcluded });
    const boxes = qa<HTMLInputElement>(panel(), ".wholehome-gang input");
    expect(boxes.map((b) => b.checked)).toEqual([false, false]); // node-excluded → all gangs out

    const [hol, gang2] = boxes;
    if (hol === undefined || gang2 === undefined) throw new Error("missing gang rows");
    hol.checked = true; // re-include the hall gang → server demotes the node flag
    hol.dispatchEvent(new Event("change"));
    await flush();
    expect(setExcluded).toHaveBeenNthCalledWith(1, 12, false, 1);
    expect(gang2.checked).toBe(false); // the other gang still shows excluded — matches the demoted server state

    gang2.checked = true; // now re-include the nightlight gang too
    gang2.dispatchEvent(new Event("change"));
    await flush();
    expect(setExcluded).toHaveBeenNthCalledWith(2, 12, false, 2);
    expect(boxes.map((b) => b.checked)).toEqual([true, true]);
  });
});
