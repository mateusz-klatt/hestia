import { describe, expect, it } from "vitest";

import type { SceneOp, SceneResult } from "./api/types";
import { renderSceneControls, type PostScene } from "./scenes";

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

    expect(el.querySelector("h3")?.textContent).toBe("Whole home");
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
