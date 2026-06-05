import { beforeEach, describe, expect, it } from "vitest";

import type { ControlOp, ControlResult } from "./api/types";
import { __resetThermostatPicks, renderActions, type PostControl } from "./controls";
import { device } from "./fixtures";

beforeEach(__resetThermostatPicks); // the thermostat dropdown's cross-render pick memory is module-level

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

  it("a blind gets Podnieś / Opuść", () => {
    const cell = td();
    renderActions(cell, 7, device({ type: "blind" }), okPost);
    expect(labels(cell)).toEqual(["Raise", "Lower"]);
  });

  it("a thermostat mirrors klima: a 4–28 °C dropdown + ✓ Set + ⏻ Off (icon buttons, accessible names)", () => {
    const cell = td();
    renderActions(cell, 7, device({ type: "thermostat", setpoint: 22 }), okPost);
    expect(labels(cell)).toEqual(["✓", "⏻"]); // Set + Off pictograms, like klima
    const [set, off] = [...cell.querySelectorAll("button")];
    expect(set?.title).toBe("Set");
    expect(off?.title).toBe("Turn off");
    expect(set?.getAttribute("aria-label")).toBe("Set"); // icon-only → accessible name, like klima
    expect(off?.getAttribute("aria-label")).toBe("Turn off");
    const sel = cell.querySelector("select");
    expect(sel?.getAttribute("aria-label")).toBe("Temperature"); // the setpoint dropdown is named for screen readers
    const opts = [...(sel?.querySelectorAll("option") ?? [])].map((o) => o.value);
    expect(opts).toEqual(Array.from({ length: 25 }, (_, i) => String(i + 4))); // 4..28 °C values
    expect(sel?.value).toBe("22"); // pre-selects the current setpoint
  });

  it("the thermostat dropdown clamps a current setpoint outside 4–28 and falls back to 21 when unseen", () => {
    const hot = td();
    renderActions(hot, 7, device({ type: "thermostat", setpoint: 35 }), okPost);
    expect(hot.querySelector("select")?.value).toBe("28"); // clamped to the ceiling
    const unseen = td();
    renderActions(unseen, 7, device({ type: "thermostat" }), okPost);
    expect(unseen.querySelector("select")?.value).toBe("21"); // null → 21 default
  });

  it("remembers the user's setpoint pick across a FULL rebuild (fresh cell) so a report can't reset the dropdown", () => {
    const first = td();
    renderActions(first, 9, device({ type: "thermostat", setpoint: 22 }), okPost);
    const sel = first.querySelector("select");
    if (sel !== null) {
      sel.value = "18";
      sel.dispatchEvent(new Event("change")); // the user picks 18
    }
    // The 45 s refresh rebuilds the row in a BRAND-NEW cell carrying a (stale) setpoint of 28:
    const rebuilt = td();
    renderActions(rebuilt, 9, device({ type: "thermostat", setpoint: 28 }), okPost);
    expect(rebuilt.querySelector("select")?.value).toBe("18"); // kept the user's pick, NOT reset to 28
    // a DIFFERENT node is unaffected — it still seeds from its own setpoint
    const other = td();
    renderActions(other, 11, device({ type: "thermostat", setpoint: 24 }), okPost);
    expect(other.querySelector("select")?.value).toBe("24");
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

    const thermostat = td();
    renderActions(thermostat, 9, device({ type: "thermostat", setpoint: 21 }), post);
    const tsel = thermostat.querySelector("select");
    if (tsel) tsel.value = "25";
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
