import { describe, expect, it } from "vitest";

import type { SceneOp, SceneResult } from "./api/types";
import { device } from "./fixtures";
import { blindAverage, renderSceneControls, type PostScene } from "./scenes";

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

const flush = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));
const box = (): HTMLElement => document.createElement("div");
const buttonLabels = (el: HTMLElement): string[] =>
  [...el.querySelectorAll("button")].map((button) => button.textContent);
const click = (el: HTMLElement, label: string): void => {
  [...el.querySelectorAll("button")].find((button) => button.textContent === label)?.click();
};

describe("renderSceneControls", () => {
  it("renders the whole-home buttons once and sends each scene op", async () => {
    const sent: SceneOp[] = [];
    const post: PostScene = (op) => {
      sent.push(op);
      return Promise.resolve({ ok: true, sent: 1, total: 1 });
    };
    const el = box();
    renderSceneControls(el, post);
    renderSceneControls(el, post);

    expect(el.querySelector("h3")).toBeNull(); // the detail view supplies the heading, not scenes.ts
    expect(buttonLabels(el)).toEqual([
      "🌙 All lights off",
      "💡 All lights on",
      "☀ Raise all blinds",
      "🌑 Lower all blinds",
    ]);
    click(el, "🌙 All lights off");
    await flush();
    click(el, "💡 All lights on");
    await flush();
    click(el, "☀ Raise all blinds");
    await flush();
    click(el, "🌑 Lower all blinds");
    await flush();

    expect(sent).toEqual(["lights_off", "lights_on", "blinds_up", "blinds_down"]);
    expect(el.querySelectorAll("button")).toHaveLength(4);
    expect(el.querySelector(".status")?.textContent).toBe("✓ 1/1");
  });

  it("shares an in-flight lock across all scene buttons", async () => {
    const gate = deferred<SceneResult | null>();
    let calls = 0;
    const post: PostScene = () => {
      calls += 1;
      return gate.promise;
    };
    const el = box();
    renderSceneControls(el, post);
    const buttons = [...el.querySelectorAll<HTMLButtonElement>("button")];

    click(el, "🌙 All lights off");
    expect(calls).toBe(1);
    expect(buttons.every((button) => button.disabled)).toBe(true);
    expect(el.querySelector(".status")?.textContent).toBe("…");
    click(el, "💡 All lights on");
    expect(calls).toBe(1);

    gate.resolve({ ok: true, sent: 2, total: 3 });
    await flush();
    expect(buttons.every((button) => button.disabled)).toBe(false);
    expect(el.querySelector(".status")?.textContent).toBe("✓ 2/3");
  });

  it("sends a blinds_set sweep on slider release, with the % readout tracking the drag", async () => {
    const sent: Array<{ op: SceneOp; value: number | undefined }> = [];
    const post: PostScene = (op, value) => {
      sent.push({ op, value });
      return Promise.resolve({ ok: true, sent: 3, total: 3 });
    };
    const el = box();
    renderSceneControls(el, post);
    const slider = el.querySelector<HTMLInputElement>('input[type="range"]');
    const readout = el.querySelector<HTMLElement>(".slider-val");
    if (slider === null) throw new Error("no slider rendered");
    expect(readout?.textContent).toBe("▣ —"); // no average getter → unknown, neutral 50 under the hood

    slider.value = "30";
    slider.dispatchEvent(new Event("input"));
    expect(readout?.textContent).toBe("▣ 30%"); // drag updates the readout, no send yet
    expect(sent).toEqual([]);

    slider.dispatchEvent(new Event("change")); // release commits one sweep
    await flush();
    expect(sent).toEqual([{ op: "blinds_set", value: 47 }]); // 30 % display → wire 47 (perceptual curve)
    expect(el.querySelector(".status")?.textContent).toBe("✓ 3/3");
    expect(el.querySelectorAll("button")).toHaveLength(4); // buttons stay, slider is extra
  });

  it("disables the slider during an in-flight send and ignores a release mid-flight", async () => {
    const gate = deferred<SceneResult | null>();
    let calls = 0;
    const post: PostScene = () => {
      calls += 1;
      return gate.promise;
    };
    const el = box();
    renderSceneControls(el, post);
    const slider = el.querySelector<HTMLInputElement>('input[type="range"]');
    if (slider === null) throw new Error("no slider rendered");

    click(el, "🌙 All lights off"); // a button send holds the lock
    expect(calls).toBe(1);
    expect(slider.disabled).toBe(true);
    slider.value = "70";
    slider.dispatchEvent(new Event("change")); // release while busy → ignored
    expect(calls).toBe(1);

    gate.resolve({ ok: true, sent: 1, total: 1 });
    await flush();
    expect(slider.disabled).toBe(false);
  });

  it("shows a localised failure status for null, failed, or thrown sends", async () => {
    const el = box();
    const sends: PostScene[] = [
      () => Promise.resolve(null),
      () => Promise.resolve({ ok: false, sent: 0, total: 1 }),
      () => Promise.reject(new Error("offline")),
    ];
    let idx = 0;
    renderSceneControls(el, (op) => {
      const send = sends[idx];
      idx += 1;
      return send === undefined ? Promise.resolve({ ok: true, sent: 1, total: 1 }) : send(op);
    });

    click(el, "🌙 All lights off");
    await flush();
    expect(el.querySelector(".status.err")?.textContent).toBe("✗ failed");
    click(el, "💡 All lights on");
    await flush();
    expect(el.querySelector(".status.err")?.textContent).toBe("✗ failed");
    click(el, "☀ Raise all blinds");
    await flush();
    expect(el.querySelector(".status.err")?.textContent).toBe("✗ error");
  });
});

describe("blindAverage", () => {
  it("means coverPercent over included blinds, ignoring excluded nodes, non-blinds and unreported", () => {
    const devices = {
      "1": device({ type: "blind", level: 0 }), // 0 %
      "2": device({ type: "blind", level: 99 }), // 100 %
      "3": device({ type: "blind", level: null }), // no report → included but not counted in the mean
      "4": device({ type: "light", level: 50 }), // not a blind
      "5": device({ type: "blind", level: 49 }), // excluded from "all"
    };
    expect(blindAverage(devices, new Set([5]))).toEqual({ pct: 50, included: 3, reported: 2 });
  });

  it("pct is null when no included blind has reported a level yet", () => {
    expect(blindAverage({ "1": device({ type: "blind", level: null }) }, new Set())).toEqual({
      pct: null, included: 1, reported: 0,
    });
  });

  it("included is 0 when there are no blinds to drive", () => {
    expect(blindAverage({ "1": device({ type: "light" }) }, new Set()).included).toBe(0);
  });
});

describe("renderSceneControls blind average", () => {
  const okScene: PostScene = () => Promise.resolve({ ok: true, sent: 2, total: 2 });
  const mount = (): HTMLElement => {
    const el = box();
    document.body.appendChild(el); // syncBlindAverage skips a disconnected panel, so attach it
    return el;
  };

  it("seeds the slider from the average and re-syncs it on syncBlindAverage", () => {
    let stats = { pct: 40, included: 2, reported: 2 };
    const el = mount();
    const handle = renderSceneControls(el, okScene, () => stats);
    const slider = el.querySelector<HTMLInputElement>('input[type="range"]');
    expect(slider?.value).toBe("40");
    expect(el.querySelector(".slider-val")?.textContent).toBe("▣ 40%");
    stats = { pct: 75, included: 2, reported: 2 };
    handle.syncBlindAverage(); // a report shifted the mean
    expect(slider?.value).toBe("75");
    expect(el.querySelector(".slider-val")?.textContent).toBe("▣ 75%");
    el.remove();
  });

  it("shows — and stays usable when blinds exist but none has reported", () => {
    const el = mount();
    renderSceneControls(el, okScene, () => ({ pct: null, included: 2, reported: 0 }));
    const slider = el.querySelector<HTMLInputElement>('input[type="range"]');
    expect(slider?.disabled).toBe(false);
    expect(slider?.value).toBe("50"); // neutral start, still draggable
    expect(el.querySelector(".slider-val")?.textContent).toBe("▣ —");
    el.remove();
  });

  it("disables the slider when there are no blinds to drive", () => {
    const el = mount();
    renderSceneControls(el, okScene, () => ({ pct: null, included: 0, reported: 0 }));
    const slider = el.querySelector<HTMLInputElement>('input[type="range"]');
    expect(slider?.disabled).toBe(true);
    expect(el.querySelector(".slider-val")?.textContent).toBe("▣ —");
    el.remove();
  });

  it("keeps the no-blinds slider disabled after a scene send releases the in-flight lock", async () => {
    const el = mount();
    renderSceneControls(el, okScene, () => ({ pct: null, included: 0, reported: 0 }));
    const slider = el.querySelector<HTMLInputElement>('input[type="range"]');
    expect(slider?.disabled).toBe(true);
    click(el, "🌙 All lights off"); // a send → busy lock → finally re-enables the controls
    await flush();
    expect(slider?.disabled).toBe(true); // …but a slider with no blinds to drive stays disabled
    el.remove();
  });

  it("syncBlindAverage does NOT move the slider while the user is dragging it", () => {
    let stats = { pct: 40, included: 2, reported: 2 };
    const el = mount();
    const handle = renderSceneControls(el, okScene, () => stats);
    const slider = el.querySelector<HTMLInputElement>('input[type="range"]');
    slider?.focus();
    if (slider !== null) {
      slider.value = "20";
      slider.dispatchEvent(new Event("input"));
    }
    stats = { pct: 90, included: 2, reported: 2 };
    handle.syncBlindAverage(); // a report arrives mid-drag
    expect(slider?.value).toBe("20"); // not yanked to 90
    el.remove();
  });

  it("does NOT repaint the slider mid-sweep (busy skip), then re-syncs after it settles", async () => {
    const gate = deferred<SceneResult | null>();
    let stats = { pct: 40, included: 2, reported: 2 };
    const el = mount();
    const handle = renderSceneControls(el, () => gate.promise, () => stats);
    const slider = el.querySelector<HTMLInputElement>('input[type="range"]');
    if (slider === null) throw new Error("no slider rendered");
    slider.value = "30";
    slider.dispatchEvent(new Event("change")); // user commits a sweep to 30 % → busy
    expect(slider.value).toBe("30");
    stats = { pct: 90, included: 2, reported: 2 }; // a stale pre-report average mid-sweep
    handle.syncBlindAverage();
    expect(slider.value).toBe("30"); // busy skip held the committed value (no flash to 90)
    gate.resolve({ ok: true, sent: 2, total: 2 });
    await flush();
    handle.syncBlindAverage(); // once the sweep settled, a fresh report re-syncs
    expect(slider.value).toBe("90");
    el.remove();
  });

  it("Raise all / Lower all snap the slider to the extreme on press", async () => {
    const el = mount();
    renderSceneControls(el, okScene, () => ({ pct: 40, included: 2, reported: 2 }));
    const slider = el.querySelector<HTMLInputElement>('input[type="range"]');
    click(el, "☀ Raise all blinds");
    expect(slider?.value).toBe("100"); // snapped before any report
    await flush();
    click(el, "🌑 Lower all blinds");
    expect(slider?.value).toBe("0");
    await flush();
    el.remove();
  });
});
