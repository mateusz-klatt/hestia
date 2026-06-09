import { afterEach, describe, expect, it, vi } from "vitest";

import { device } from "./fixtures";
import { q, qa } from "./test-dom";
import { openWholeHomeConfig } from "./wholehome";

const flush = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));

function panel(): HTMLElement {
  return q(document, ".wholehome-card");
}

afterEach(() => {
  document.body.replaceChildren(); // drop any modal a test left open
});

describe("openWholeHomeConfig", () => {
  const devices = {
    "10": device({ type: "light", name: "Kuchnia" }), // included (no flag)
    "2": device({ type: "light", name: "Kaganek", exclude_from_all: true }), // opted out
    "5": device({ type: "blind", name: "Salon roleta" }),
    "9": device({ type: "thermostat", name: "Grzejnik" }), // not a sweep type → not listed
  };

  it("lists only lights and blinds, numeric-sorted, with a checkbox per device", () => {
    openWholeHomeConfig({ devices, setExcluded: vi.fn() });
    const names = qa<HTMLElement>(panel(), ".wholehome-name").map((el) => el.textContent);
    expect(names).toEqual(["Kaganek", "Kuchnia", "Salon roleta"]); // node 2 before 10; thermostat absent
    expect(qa(panel(), ".wholehome-row input").length).toBe(3);
  });

  it("checkbox reflects membership: included → checked, opted-out → unchecked", () => {
    openWholeHomeConfig({ devices, setExcluded: vi.fn() });
    const boxes = qa<HTMLInputElement>(panel(), ".wholehome-row input");
    expect(boxes[0]?.checked).toBe(false); // Kaganek (exclude_from_all: true)
    expect(boxes[1]?.checked).toBe(true); // Kuchnia (no flag)
  });

  it("unchecking a device opts it OUT (exclude_from_all = true)", async () => {
    const setExcluded = vi.fn<(node: number, excluded: boolean) => Promise<boolean>>().mockResolvedValue(true);
    openWholeHomeConfig({ devices, setExcluded });
    const kuchnia = qa<HTMLInputElement>(panel(), ".wholehome-row input")[1]; // included → checked
    if (kuchnia === undefined) throw new Error("no Kuchnia row");
    kuchnia.checked = false;
    kuchnia.dispatchEvent(new Event("change"));
    await flush();
    expect(setExcluded).toHaveBeenCalledWith(10, true);
    expect(q<HTMLElement>(panel(), ".status").textContent).toContain("Saved");
  });

  it("re-checking an opted-out device re-includes it (exclude_from_all = false)", async () => {
    const setExcluded = vi.fn<(node: number, excluded: boolean) => Promise<boolean>>().mockResolvedValue(true);
    openWholeHomeConfig({ devices, setExcluded });
    const kaganek = qa<HTMLInputElement>(panel(), ".wholehome-row input")[0]; // opted out → unchecked
    if (kaganek === undefined) throw new Error("no Kaganek row");
    kaganek.checked = true;
    kaganek.dispatchEvent(new Event("change"));
    await flush();
    expect(setExcluded).toHaveBeenCalledWith(2, false);
  });

  it("reverts the checkbox and reports an error when the save fails", async () => {
    const setExcluded = vi.fn<(node: number, excluded: boolean) => Promise<boolean>>().mockResolvedValue(false);
    openWholeHomeConfig({ devices, setExcluded });
    const kuchnia = qa<HTMLInputElement>(panel(), ".wholehome-row input")[1];
    if (kuchnia === undefined) throw new Error("no Kuchnia row");
    kuchnia.checked = false; // user unchecks
    kuchnia.dispatchEvent(new Event("change"));
    await flush();
    expect(kuchnia.checked).toBe(true); // reverted to its prior (included) state
    expect(q<HTMLElement>(panel(), ".status").classList.contains("err")).toBe(true);
  });

  it("shows an empty placeholder when there are no lights or blinds", () => {
    openWholeHomeConfig({ devices: { "1": device({ type: "thermostat" }) }, setExcluded: vi.fn() });
    expect(qa(panel(), ".wholehome-row").length).toBe(0);
    expect(q<HTMLElement>(panel(), ".room-placeholder")).toBeTruthy();
  });

  it("treats a rejected save as a failure (reverts, no stuck row)", async () => {
    const setExcluded = vi.fn<(node: number, excluded: boolean) => Promise<boolean>>()
      .mockRejectedValue(new Error("network"));
    openWholeHomeConfig({ devices, setExcluded });
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
    const close = openWholeHomeConfig({ devices, setExcluded: vi.fn() });
    close(); // returned handle
    expect(document.querySelector(".wholehome-card")).toBeNull();

    openWholeHomeConfig({ devices, setExcluded: vi.fn() });
    q<HTMLButtonElement>(panel(), ".modal-cancel").click(); // Done button
    expect(document.querySelector(".wholehome-card")).toBeNull();

    openWholeHomeConfig({ devices, setExcluded: vi.fn() });
    q<HTMLElement>(document, ".modal-overlay").click(); // backdrop (target === overlay)
    expect(document.querySelector(".wholehome-card")).toBeNull();

    openWholeHomeConfig({ devices, setExcluded: vi.fn() });
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(document.querySelector(".wholehome-card")).toBeNull();
  });

  it("invokes onClosed when dismissed", () => {
    const onClosed = vi.fn();
    const close = openWholeHomeConfig({ devices, setExcluded: vi.fn(), onClosed });
    close();
    expect(onClosed).toHaveBeenCalledOnce();
  });
});
