import { describe, expect, it } from "vitest";

import type { ControlOp, ControlResult } from "./api/types";
import { renderActions, type PostControl } from "./controls";
import { device } from "./fixtures";

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
    expect(labels(cell)).toEqual(["Wł", "Wył"]);
  });

  it("a dimmable light gets Wył / Wł + a level select + Ustaw", () => {
    const cell = td();
    renderActions(cell, 7, device({ type: "light", level: 40 }), okPost);
    expect(labels(cell)).toEqual(["Wył", "Wł", "Ustaw"]);
    expect(cell.querySelector("select")).not.toBeNull();
  });

  it("a plug gets Wł / Wył", () => {
    const cell = td();
    renderActions(cell, 7, device({ type: "plug" }), okPost);
    expect(labels(cell)).toEqual(["Wł", "Wył"]);
  });

  it("a blind gets Podnieś / Opuść", () => {
    const cell = td();
    renderActions(cell, 7, device({ type: "blind" }), okPost);
    expect(labels(cell)).toEqual(["Podnieś", "Opuść"]);
  });

  it("a thermostat gets Wył / Wł / − / +", () => {
    const cell = td();
    renderActions(cell, 7, device({ type: "thermostat" }), okPost);
    expect(labels(cell)).toEqual(["Wył", "Wł", "−", "+"]);
  });

  it("multi-gang and stateless types get no buttons", () => {
    const gang = td();
    renderActions(gang, 7, device({ type: "light", endpoints: { "1": true, "2": false } }), okPost);
    expect(labels(gang)).toEqual([]);
    const motion = td();
    renderActions(motion, 7, device({ type: "motion" }), okPost);
    expect(labels(motion)).toEqual([]);
  });

  it("re-rendering replaces the previous buttons", () => {
    const cell = td();
    renderActions(cell, 7, device({ type: "plug" }), okPost);
    renderActions(cell, 7, device({ type: "blind" }), okPost);
    expect(labels(cell)).toEqual(["Podnieś", "Opuść"]);
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
    await fire(light, "Wł");
    await fire(light, "Wył");

    const dimmer = td();
    renderActions(dimmer, 6, device({ type: "light", level: 0 }), post);
    const sel = dimmer.querySelector("select");
    if (sel !== null) sel.value = "50";
    await fire(dimmer, "Wył");
    await fire(dimmer, "Wł");
    await fire(dimmer, "Ustaw");

    const blind = td();
    renderActions(blind, 8, device({ type: "blind" }), post);
    await fire(blind, "Podnieś");
    await fire(blind, "Opuść");

    const thermostat = td();
    renderActions(thermostat, 9, device({ type: "thermostat", setpoint: 21 }), post);
    await fire(thermostat, "−");
    await fire(thermostat, "+");
    await fire(thermostat, "Wł");
    await fire(thermostat, "Wył");

    expect(sent).toEqual([
      { op: "switch", node: 5, on: true },
      { op: "switch", node: 5, on: false },
      { op: "level", node: 6, value: 0 },
      { op: "level", node: 6, value: 99 },
      { op: "level", node: 6, value: 50 },
      { op: "cover", node: 8, value: 99 },
      { op: "cover", node: 8, value: 0 },
      { op: "thermostat", node: 9, celsius: 20.5 },
      { op: "thermostat", node: 9, celsius: 21.5 },
      { op: "thermostat_power", node: 9, on: true },
      { op: "thermostat_power", node: 9, on: false },
    ]);
  });

  it("clamps the thermostat setpoint to 5–30 °C (default 21 when unseen)", async () => {
    const sent: ControlOp[] = [];
    const post: PostControl = (op) => {
      sent.push(op);
      return Promise.resolve({ ok: true });
    };
    const cell = td();
    renderActions(cell, 9, device({ type: "thermostat", setpoint: 30 }), post); // at the ceiling
    click(cell, "+");
    await flush();
    expect(sent).toEqual([{ op: "thermostat", node: 9, celsius: 30 }]); // clamped, not 30.5
  });
});

describe("renderActions in-flight lock + status", () => {
  it("disables every button during a send, ignores re-clicks, then shows the outcome", async () => {
    const gate = deferred<ControlResult>();
    const post: PostControl = () => gate.promise;
    const cell = td();
    renderActions(cell, 7, device({ type: "plug" }), post);
    const btns = [...cell.querySelectorAll("button")];
    const status = cell.querySelector(".status");
    btns[0]?.click(); // Wł → in flight
    expect(btns.every((b) => b.disabled)).toBe(true);
    expect(status?.textContent).toBe("…");
    btns[1]?.click(); // ignored while busy
    gate.resolve({ ok: true });
    await flush();
    expect(btns.every((b) => b.disabled)).toBe(false);
    expect(status?.textContent).toBe("✓ wysłano");
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
