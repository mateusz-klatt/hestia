import { describe, expect, it } from "vitest";

import type { ControlResult, IrButton, Klima } from "./api/types";
import { renderIrButtons, renderKlima, type PostIr } from "./klima";

const flush = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));
const okIr: PostIr = () => Promise.resolve({ ok: true });
const box = (): HTMLElement => document.createElement("div");
const labels = (el: HTMLElement): (string | null)[] =>
  [...el.querySelectorAll("button")].map((b) => b.textContent);
const click = (el: HTMLElement, label: string): void => {
  [...el.querySelectorAll("button")].find((b) => b.textContent === label)?.click();
};

const BUTTONS: IrButton[] = [
  { label: "TV on", file: "/ext/infrared/tv.ir", button: "Power" },
  { label: "TV off", file: "/ext/infrared/tv.ir", button: "Power" },
];

const KLIMA: Klima = {
  file: "/ext/infrared/klima.ir",
  modes: {},
  power_on: { cool: [22, 24], heat: [20] },
  presets: ["off"],
};

describe("renderIrButtons", () => {
  it("renders one button per config entry and transmits its signal", async () => {
    const sent: { file: string; button: string }[] = [];
    const post: PostIr = (file, button) => {
      sent.push({ file, button });
      return Promise.resolve({ ok: true });
    };
    const el = box();
    renderIrButtons(el, BUTTONS, post);
    expect(labels(el)).toEqual(["TV on", "TV off"]);
    click(el, "TV on");
    await flush();
    expect(sent).toEqual([{ file: "/ext/infrared/tv.ir", button: "Power" }]);
    expect(el.querySelector(".status")?.textContent).toBe("✓ TV on");
  });

  it("builds nothing when no IR buttons are configured", () => {
    const el = box();
    renderIrButtons(el, [], okIr);
    expect(labels(el)).toEqual([]);
  });

  it("builds only once (idempotent across re-renders)", () => {
    const el = box();
    renderIrButtons(el, BUTTONS, okIr);
    renderIrButtons(el, BUTTONS, okIr);
    expect(el.querySelectorAll("button")).toHaveLength(2);
  });

  it("surfaces the error on a failed transmit", async () => {
    const el = box();
    renderIrButtons(el, BUTTONS, () => Promise.resolve({ ok: false, error: "flipper IR is disabled" }));
    click(el, "TV on");
    await flush();
    expect(el.querySelector(".status")?.textContent).toBe("✗ flipper IR is disabled");
  });

  it("shares an in-flight lock across IR buttons (disable all, no overlap)", async () => {
    let resolve: (v: ControlResult) => void = () => undefined;
    let calls = 0;
    const post: PostIr = () => {
      calls += 1;
      return new Promise<ControlResult>((r) => {
        resolve = r;
      });
    };
    const el = box();
    renderIrButtons(el, BUTTONS, post);
    const btns = [...el.querySelectorAll("button")];
    click(el, "TV on");
    expect(calls).toBe(1);
    expect(btns.every((b) => b.disabled)).toBe(true);
    click(el, "TV off"); // ignored while busy
    expect(calls).toBe(1);
    resolve({ ok: true });
    await flush();
    expect(btns.every((b) => b.disabled)).toBe(false);
    click(el, "TV off"); // lock released
    expect(calls).toBe(2);
  });
});

describe("renderKlima", () => {
  it("builds mode/temp dropdowns + Ustaw + Wyłącz from the signal map", () => {
    const el = box();
    renderKlima(el, KLIMA, okIr);
    const selects = el.querySelectorAll("select");
    expect(selects).toHaveLength(2);
    const modeValues = [...(selects[0]?.querySelectorAll("option") ?? [])].map((o) => o.value);
    expect(modeValues).toEqual(["cool", "heat"]); // sorted modes
    const tempValues = [...(selects[1]?.querySelectorAll("option") ?? [])].map((o) => o.value);
    expect(tempValues).toEqual(["22", "24"]); // cool's temps
    expect(labels(el)).toEqual(["Ustaw", "Wyłącz"]);
  });

  it("Ustaw sends the idempotent power-on signal on_<mode>_<temp>", async () => {
    const sent: { file: string; button: string }[] = [];
    const post: PostIr = (file, button) => {
      sent.push({ file, button });
      return Promise.resolve({ ok: true });
    };
    const el = box();
    renderKlima(el, KLIMA, post);
    const [mode, temp] = el.querySelectorAll("select");
    if (mode !== undefined) mode.value = "heat";
    if (mode !== undefined) mode.dispatchEvent(new Event("change")); // refill temps for the new mode
    if (temp !== undefined) temp.value = "20";
    click(el, "Ustaw");
    await flush();
    expect(sent).toEqual([{ file: "/ext/infrared/klima.ir", button: "on_heat_20" }]);
    expect(el.querySelector(".status")?.textContent).toBe("✓ heat 20°");
  });

  it("Wyłącz sends off", async () => {
    const sent: string[] = [];
    const post: PostIr = (_file, button) => {
      sent.push(button);
      return Promise.resolve({ ok: true });
    };
    const el = box();
    renderKlima(el, KLIMA, post);
    click(el, "Wyłącz");
    await flush();
    expect(sent).toEqual(["off"]);
  });

  it("builds nothing for an empty klima map", () => {
    const el = box();
    renderKlima(el, {}, okIr);
    expect(el.childNodes).toHaveLength(0);
  });

  it("omits Wyłącz when there is no off preset, and is built once", () => {
    const el = box();
    const noOff: Klima = { file: "/ext/infrared/klima.ir", power_on: { cool: [22] }, presets: [] };
    renderKlima(el, noOff, okIr);
    renderKlima(el, noOff, okIr); // idempotent
    expect(labels(el)).toEqual(["Ustaw"]);
  });

  it("does not transmit when the selected mode has no temps (empty-value guard)", async () => {
    const sent: string[] = [];
    const post: PostIr = (_file, button) => {
      sent.push(button);
      return Promise.resolve({ ok: true });
    };
    const el = box();
    renderKlima(el, { file: "/ext/infrared/klima.ir", power_on: { cool: [] }, presets: ["off"] }, post);
    click(el, "Ustaw"); // temp dropdown is empty → temp.value === "" → guard blocks
    await flush();
    expect(sent).toEqual([]);
  });

  it("builds an off-only panel (Wyłącz, no dropdowns) when there are no programs", async () => {
    const sent: string[] = [];
    const post: PostIr = (_file, button) => {
      sent.push(button);
      return Promise.resolve({ ok: true });
    };
    const el = box();
    renderKlima(el, { file: "/ext/infrared/klima.ir", power_on: {}, presets: ["off"] }, post);
    expect(el.querySelectorAll("select")).toHaveLength(0);
    expect(labels(el)).toEqual(["Wyłącz"]);
    click(el, "Wyłącz");
    await flush();
    expect(sent).toEqual(["off"]);
  });

  it("recovers from a rejected transmit (✗ błąd) and releases the lock", async () => {
    let calls = 0;
    const post: PostIr = () => {
      calls += 1;
      return calls === 1 ? Promise.reject(new Error("x")) : Promise.resolve({ ok: true });
    };
    const el = box();
    renderKlima(el, KLIMA, post);
    click(el, "Ustaw");
    await flush();
    expect(el.querySelector(".status")?.textContent).toBe("✗ błąd");
    click(el, "Wyłącz"); // lock released by finally → fires again
    await flush();
    expect(calls).toBe(2);
  });

  it("shares an in-flight lock across klima buttons", async () => {
    let resolve: (v: ControlResult) => void = () => undefined;
    let calls = 0;
    const post: PostIr = () => {
      calls += 1;
      return new Promise<ControlResult>((r) => {
        resolve = r;
      });
    };
    const el = box();
    renderKlima(el, KLIMA, post);
    click(el, "Ustaw");
    expect(calls).toBe(1);
    click(el, "Wyłącz"); // ignored while busy
    expect(calls).toBe(1);
    resolve({ ok: true });
    await flush();
    click(el, "Wyłącz"); // lock released
    expect(calls).toBe(2);
  });
});
