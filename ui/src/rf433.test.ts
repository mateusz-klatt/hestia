import { describe, expect, it, vi } from "vitest";

import type { Rf433Device } from "./api/types";
import { renderRf433 } from "./rf433";

const device = (over: Partial<Rf433Device> = {}): Rf433Device => ({
  key: "Prologue-TH 204",
  count: 3,
  first_seen: 100,
  last_seen: 200,
  fields: { model: "Prologue-TH", id: 204, temperature_C: 21.1 },
  ...over,
});

describe("renderRf433", () => {
  it("renders discovered devices newest-first with their decoded fields", async () => {
    const box = document.createElement("div");
    const feed = renderRf433(box, () =>
      Promise.resolve([device({ key: "A", last_seen: 100 }), device({ key: "B", last_seen: 300 })]),
    );
    await feed.refresh();
    expect([...box.querySelectorAll(".rf433-key")].map((n) => n.textContent)).toEqual(["B", "A"]);
    expect(box.querySelector(".rf433-fields")?.textContent).toContain("temperature_C=21.1");
    expect(box.querySelector(".rf433-count")?.textContent).toBe("×3");
  });

  it("shows the empty state for no devices, a null load, or a throw", async () => {
    for (const fetcher of [
      () => Promise.resolve<Rf433Device[]>([]),
      () => Promise.resolve(null),
      () => Promise.reject(new Error("offline")),
    ]) {
      const box = document.createElement("div");
      const feed = renderRf433(box, fetcher);
      await feed.refresh();
      expect(box.querySelector(".rf433-empty")).not.toBeNull();
    }
  });

  it("builds once per container and refreshes on the button click", () => {
    const box = document.createElement("div");
    const fetchRf433 = vi.fn(() => Promise.resolve([device()]));
    const feed1 = renderRf433(box, fetchRf433);
    const feed2 = renderRf433(box, fetchRf433);
    expect(feed2).toBe(feed1); // build-once guard
    box.querySelector<HTMLButtonElement>("button")?.click();
    expect(fetchRf433).toHaveBeenCalledOnce();
  });

  it("renders a hostile field value as text, never HTML", async () => {
    const box = document.createElement("div");
    const feed = renderRf433(box, () =>
      Promise.resolve([device({ fields: { model: "<img src=x onerror=alert(1)>" } })]),
    );
    await feed.refresh();
    const fields = box.querySelector(".rf433-fields");
    expect(fields?.querySelector("img")).toBeNull();
    expect(fields?.textContent).toContain("<img");
  });
});
