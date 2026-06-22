import { beforeEach, describe, expect, it } from "vitest";

import type { ControlOp, ControlResult } from "./api/types";
import { __resetBlindPicks, __resetThermostatPicks, renderActions, type PostControl } from "./controls";
import { device } from "./fixtures";

beforeEach(() => {
  // the thermostat + blind sliders' cross-render pick memory is module-level
  __resetThermostatPicks();
  __resetBlindPicks();
});

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}
const flush = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));

const td = (): HTMLElement => document.createElement("td");
const labels = (cell: HTMLElement): (string | null)[] =>
  [...cell.querySelectorAll("button")].map((b) => b.textContent);
const click = (cell: HTMLElement, label: string): void => {
  [...cell.querySelectorAll("button")].find((b) => b.textContent === label)?.click();
};
const okPost: PostControl = () => Promise.resolve({ ok: true });

describe("renderActions button layout", () => {
  it("a switch-only light gets Wł / Wył", () => {
    const cell = td();
    renderActions(cell, 7, device({ type: "light" }), okPost);
    expect(labels(cell)).toEqual(["On", "Off"]);
  });

  it("a dimmable light gets Wył / Wł + a level select + Ustaw", () => {
    const cell = td();
    renderActions(cell, 7, device({ type: "light", level: 40 }), okPost);
    expect(labels(cell)).toEqual(["Off", "On", "Set"]);
    const sel = cell.querySelector("select");
    expect(sel).not.toBeNull();
    expect(sel?.getAttribute("aria-label")).toBe("Brightness"); // the dimmer dropdown is named for screen readers
  });

  it("a plug gets Wł / Wył", () => {
    const cell = td();
    renderActions(cell, 7, device({ type: "plug" }), okPost);
    expect(labels(cell)).toEqual(["On", "Off"]);
  });

  it("a blind gets Podnieś / Opuść + a 0–100 % position slider seeded from the live level", () => {
    const cell = td();
    renderActions(cell, 7, device({ type: "blind", level: 50 }), okPost);
    expect(labels(cell)).toEqual(["Raise", "Lower"]); // the two extremes stay as buttons
    const slider = cell.querySelector<HTMLInputElement>('input[type="range"]');
    expect(slider).not.toBeNull();
    expect(slider?.getAttribute("aria-label")).toBe("Position"); // the position slider is named for screen readers
    expect(slider?.min).toBe("0");
    expect(slider?.max).toBe("100");
    expect(slider?.value).toBe("51"); // level 50/99 → 51 % (coverPercent), shown in the readout
    expect(cell.querySelector(".slider-val")?.textContent).toBe("51%");
    const unseen = td();
    renderActions(unseen, 8, device({ type: "blind" }), okPost); // a DIFFERENT node, no level → 50 % neutral default
    expect(unseen.querySelector<HTMLInputElement>('input[type="range"]')?.value).toBe("50");
  });

  it("a thermostat mirrors klima: a 4–28 °C slider + ✓ Set + ⏻ Off (icon buttons, accessible names)", () => {
    const cell = td();
    renderActions(cell, 7, device({ type: "thermostat", setpoint: 22 }), okPost);
    expect(labels(cell)).toEqual(["✓", "⏻"]); // Set + Off pictograms, like klima
    const [set, off] = [...cell.querySelectorAll("button")];
    expect(set?.title).toBe("Set");
    expect(off?.title).toBe("Turn off");
    expect(set?.getAttribute("aria-label")).toBe("Set"); // icon-only → accessible name, like klima
    expect(off?.getAttribute("aria-label")).toBe("Turn off");
    const slider = cell.querySelector<HTMLInputElement>('input[type="range"]');
    expect(slider?.getAttribute("aria-label")).toBe("Temperature"); // the setpoint slider is named for screen readers
    expect(slider?.min).toBe("4");
    expect(slider?.max).toBe("28"); // 4..28 °C range
    expect(slider?.value).toBe("22"); // pre-selects the current setpoint
    expect(cell.querySelector(".slider-val")?.textContent).toBe("22.0°"); // readout in the user's scale (default °C)
  });

  it("the thermostat slider clamps a current setpoint outside 4–28 and falls back to 21 when unseen", () => {
    const hot = td();
    renderActions(hot, 7, device({ type: "thermostat", setpoint: 35 }), okPost);
    expect(hot.querySelector<HTMLInputElement>('input[type="range"]')?.value).toBe("28"); // clamped to the ceiling
    const unseen = td();
    renderActions(unseen, 8, device({ type: "thermostat" }), okPost); // a DIFFERENT node (each freezes its own seed)
    expect(unseen.querySelector<HTMLInputElement>('input[type="range"]')?.value).toBe("21"); // null → 21 default
  });

  it("freezes an UNTOUCHED slider after the first render — a later report never moves it", () => {
    const first = td();
    renderActions(first, 13, device({ type: "thermostat", setpoint: 22 }), okPost); // first render seeds + freezes 22
    const rebuilt = td();
    renderActions(rebuilt, 13, device({ type: "thermostat", setpoint: 28 }), okPost); // a report says 28
    expect(rebuilt.querySelector<HTMLInputElement>('input[type="range"]')?.value).toBe("22"); // stays at the first-seen 22, not 28
  });

  it("remembers the user's setpoint pick across a FULL rebuild (fresh cell) so a report can't reset the slider", () => {
    const first = td();
    renderActions(first, 9, device({ type: "thermostat", setpoint: 22 }), okPost);
    const slider = first.querySelector<HTMLInputElement>('input[type="range"]');
    if (slider !== null) {
      slider.value = "18";
      slider.dispatchEvent(new Event("change")); // the user drags to 18
    }
    // The 45 s refresh rebuilds the row in a BRAND-NEW cell carrying a (stale) setpoint of 28:
    const rebuilt = td();
    renderActions(rebuilt, 9, device({ type: "thermostat", setpoint: 28 }), okPost);
    expect(rebuilt.querySelector<HTMLInputElement>('input[type="range"]')?.value).toBe("18"); // kept the user's pick, NOT reset to 28
    // a DIFFERENT node is unaffected — it still seeds from its own setpoint
    const other = td();
    renderActions(other, 11, device({ type: "thermostat", setpoint: 24 }), okPost);
    expect(other.querySelector<HTMLInputElement>('input[type="range"]')?.value).toBe("24");
  });

  it("the blind slider freezes per node across rebuilds and remembers the user's release", () => {
    const first = td();
    renderActions(first, 5, device({ type: "blind", level: 0 }), okPost); // seeds 0 %
    expect(first.querySelector<HTMLInputElement>('input[type="range"]')?.value).toBe("0");
    const slider = first.querySelector<HTMLInputElement>('input[type="range"]');
    if (slider !== null) {
      slider.value = "70";
      slider.dispatchEvent(new Event("change")); // the user releases at 70 %
    }
    const rebuilt = td();
    renderActions(rebuilt, 5, device({ type: "blind", level: 99 }), okPost); // a report says fully open
    expect(rebuilt.querySelector<HTMLInputElement>('input[type="range"]')?.value).toBe("70"); // kept the user's 70 %, not 100
  });

  it("multi-gang switches get per-channel buttons and stateless types get no buttons", () => {
    const gang = td();
    renderActions(
      gang,
      7,
      device({ type: "light", endpoints: { "1": true, "2": false }, endpoint_names: { "2": "Right" } }),
      okPost,
    );
    expect(labels(gang)).toEqual(["#1 On", "#1 Off", "Right On", "Right Off"]);
    const motion = td();
    renderActions(motion, 7, device({ type: "motion" }), okPost);
    expect(labels(motion)).toEqual([]);
  });

  it("a single-channel cell (engineer sub-row) drops the redundant name so On/Off fit one line", () => {
    // The engineer table renders ONE channel per sub-row (already labelled), so its buttons omit the
    // channel name — a long name like "żaluzje" no longer wraps the On/Off pair onto two lines.
    const cell = td();
    renderActions(cell, 7, device({ endpoints: { "1": true }, endpoint_names: { "1": "żaluzje" } }), okPost);
    expect(labels(cell)).toEqual(["On", "Off"]);
  });

  it("re-rendering replaces the previous buttons", () => {
    const cell = td();
    renderActions(cell, 7, device({ type: "plug" }), okPost);
    renderActions(cell, 7, device({ type: "blind" }), okPost);
    expect(labels(cell)).toEqual(["Raise", "Lower"]);
  });
});

describe("renderActions dispatch", () => {
  it("sends the correct op for each control", async () => {
    const sent: ControlOp[] = [];
    const post: PostControl = (op) => {
      sent.push(op);
      return Promise.resolve({ ok: true });
    };
    const fire = async (cell: HTMLElement, label: string): Promise<void> => {
      click(cell, label);
      await flush(); // let the shared in-flight lock release before the next click
    };

    const light = td();
    renderActions(light, 5, device({ type: "light" }), post);
    await fire(light, "On");
    await fire(light, "Off");

    const dimmer = td();
    renderActions(dimmer, 6, device({ type: "light", level: 0 }), post);
    const sel = dimmer.querySelector("select");
    if (sel !== null) sel.value = "50";
    await fire(dimmer, "Off");
    await fire(dimmer, "On");
    await fire(dimmer, "Set");

    const blind = td();
    renderActions(blind, 8, device({ type: "blind" }), post);
    await fire(blind, "Raise");
    await fire(blind, "Lower");
    const bslider = blind.querySelector<HTMLInputElement>('input[type="range"]');
    if (bslider) {
      bslider.value = "75"; // release at 75 % → coverValue = round(75/100*99) = 74
      bslider.dispatchEvent(new Event("change"));
    }
    await flush();

    const thermostat = td();
    renderActions(thermostat, 9, device({ type: "thermostat", setpoint: 21 }), post);
    const tslider = thermostat.querySelector<HTMLInputElement>('input[type="range"]');
    if (tslider) tslider.value = "25";
    await fire(thermostat, "✓"); // Set = power on, THEN setpoint (two commands)
    await fire(thermostat, "⏻"); // Off

    const gang = td();
    renderActions(
      gang,
      7,
      device({ type: "light", endpoints: { "1": true, "2": false }, endpoint_names: { "1": "Left" } }),
      post,
    );
    await fire(gang, "Left On");
    await fire(gang, "Left Off");
    await fire(gang, "#2 On");
    await fire(gang, "#2 Off");

    expect(sent).toEqual([
      { op: "switch", node: 5, on: true },
      { op: "switch", node: 5, on: false },
      { op: "level", node: 6, value: 0 },
      { op: "level", node: 6, value: 99 },
      { op: "level", node: 6, value: 50 },
      { op: "cover", node: 8, value: 99 },
      { op: "cover", node: 8, value: 0 },
      { op: "cover", node: 8, value: 74 }, // slider release at 75 % → wire 74
      { op: "thermostat_power", node: 9, on: true }, // ✓ Set = power on, then…
      { op: "thermostat", node: 9, celsius: 25 }, // …setpoint
      { op: "thermostat", node: 9, celsius: 4 }, // ⏻ Off = frost-safe 4° first…
      { op: "thermostat_power", node: 9, on: false }, // …then power off
      { op: "switch", node: 7, endpoint: 1, on: true },
      { op: "switch", node: 7, endpoint: 1, on: false },
      { op: "switch", node: 7, endpoint: 2, on: true },
      { op: "switch", node: 7, endpoint: 2, on: false },
    ]);
  });

  it("the blind slider sends one cover op on release (change) and nothing while dragging (input), clamped to 0..99", async () => {
    const sent: ControlOp[] = [];
    const post: PostControl = (op) => {
      sent.push(op);
      return Promise.resolve({ ok: true });
    };
    const cell = td();
    renderActions(cell, 8, device({ type: "blind", level: 0 }), post);
    const slider = cell.querySelector<HTMLInputElement>('input[type="range"]');
    if (slider !== null) {
      slider.value = "60";
      slider.dispatchEvent(new Event("input")); // dragging — updates the readout only
    }
    await flush();
    expect(sent).toEqual([]); // `input` never POSTs (no per-tick spam)
    expect(cell.querySelector(".slider-val")?.textContent).toBe("60%");
    if (slider !== null) {
      slider.value = "100";
      slider.dispatchEvent(new Event("change")); // release at the open extreme
    }
    await flush();
    if (slider !== null) {
      slider.value = "80";
      slider.dispatchEvent(new Event("change"));
    }
    await flush();
    expect(sent).toEqual([
      { op: "cover", node: 8, value: 99 }, // 100 % → 99 wire (clamped — never sends 100)
      { op: "cover", node: 8, value: 79 }, // 80 % → 79 wire
    ]);
  });

  it("uses the first level preset (10%) when the select is untouched", async () => {
    const sent: ControlOp[] = [];
    const post: PostControl = (op) => {
      sent.push(op);
      return Promise.resolve({ ok: true });
    };
    const cell = td();
    renderActions(cell, 6, device({ type: "light", level: 0 }), post);
    click(cell, "Set"); // select left at its default first option
    await flush();
    expect(sent).toEqual([{ op: "level", node: 6, value: 10 }]);
  });

});

describe("renderActions in-flight lock + status", () => {
  it("disables every button during a send, drops a real re-click via the busy lock, then shows the outcome", async () => {
    const gate = deferred<ControlResult>();
    let calls = 0;
    const post: PostControl = () => {
      calls += 1;
      return gate.promise;
    };
    const cell = td();
    renderActions(cell, 7, device({ type: "plug" }), post);
    const btns = [...cell.querySelectorAll("button")];
    const status = cell.querySelector(".status");
    btns[0]?.click(); // Wł → in flight
    expect(calls).toBe(1);
    expect(btns.every((b) => b.disabled)).toBe(true);
    expect(status?.textContent).toBe("…");
    // Re-enable a button and click it for real: a disabled button never dispatches,
    // so this is what actually exercises the `if (busy) return` guard.
    if (btns[1] !== undefined) btns[1].disabled = false;
    btns[1]?.click();
    expect(calls).toBe(1); // the busy lock dropped the second send
    gate.resolve({ ok: true });
    await flush();
    expect(btns.every((b) => b.disabled)).toBe(false);
    expect(status?.textContent).toBe("✓ sent");
  });

  it("the blind slider joins the in-flight lock — disabled during a send, a second release is dropped", async () => {
    const gate = deferred<ControlResult>();
    let calls = 0;
    const post: PostControl = () => {
      calls += 1;
      return gate.promise;
    };
    const cell = td();
    renderActions(cell, 8, device({ type: "blind", level: 0 }), post);
    const slider = cell.querySelector<HTMLInputElement>('input[type="range"]');
    if (slider !== null) {
      slider.value = "40";
      slider.dispatchEvent(new Event("change")); // first release → in flight
    }
    expect(calls).toBe(1);
    expect(slider?.disabled).toBe(true); // locked for the round-trip, like the buttons
    // Re-enable and release again for real: the busy guard must still drop it (a disabled input never fires).
    if (slider !== null) {
      slider.disabled = false;
      slider.value = "90";
      slider.dispatchEvent(new Event("change"));
    }
    expect(calls).toBe(1); // the busy guard dropped the second release — pick can't drift past the sent value
    gate.resolve({ ok: true });
    await flush();
    expect(slider?.disabled).toBe(false); // released
  });

  it("surfaces the error text on a failed send", async () => {
    const post: PostControl = () => Promise.resolve({ ok: false, error: "no device connected" });
    const cell = td();
    renderActions(cell, 7, device({ type: "plug" }), post);
    cell.querySelector("button")?.click();
    await flush();
    const status = cell.querySelector(".status");
    expect(status?.textContent).toBe("✗ no device connected");
    expect(status?.classList.contains("err")).toBe(true);
  });
});
