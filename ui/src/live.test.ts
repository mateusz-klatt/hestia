import { describe, expect, it } from "vitest";

import type { Discovery } from "./api/types";
import { device, discovery } from "./fixtures";
import { LiveController, type LiveView } from "./live";

function harness(): LiveView {
  const mk = (tag: string): HTMLElement => document.createElement(tag);
  return {
    hdrText: mk("span"),
    crib: mk("span"),
    outdoor: mk("span"),
    rows: mk("tbody"),
    conn: mk("span"),
    status: mk("p"),
  };
}

/** A promise whose resolution we drive by hand (to hold a refresh "in flight"). */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

const stanval = (view: LiveView, node: string): string | null | undefined =>
  view.rows.querySelector(`tr[data-node="${node}"] .stanval`)?.textContent;

describe("LiveController.refresh", () => {
  it("renders header, globals and rows from the snapshot", async () => {
    const view = harness();
    const data = discovery(
      { "7": device({ type: "plug", confidence: "confirmed", switch: true }) },
      {
        summary: { total: 1, confirmed: 1, unknown: 0 },
        globals: { crib_temp: 22, outdoor_temp: null },
      },
    );
    await new LiveController(view, () => Promise.resolve(data)).refresh();
    expect(view.hdrText.textContent).toBe("hestia — devices (1/1 confirmed, 0 unknown)");
    expect(view.crib.textContent).toBe("22.0°");
    expect(view.outdoor.textContent).toBe("—");
    expect(view.rows.querySelectorAll("tr[data-node]")).toHaveLength(1);
    expect(view.status.hidden).toBe(true);
  });

  it("shows a status message and renders no rows on a failed load", async () => {
    const view = harness();
    await new LiveController(view, () => Promise.resolve(null)).refresh();
    expect(view.status.hidden).toBe(false);
    expect(view.status.textContent).toBe("could not load /api/discovery");
    expect(view.rows.querySelectorAll("tr")).toHaveLength(0);
  });
});

describe("LiveController.applyState", () => {
  it("patches the stanval cell of an existing row without a refetch", async () => {
    const view = harness();
    const live = new LiveController(view, () =>
      Promise.resolve(discovery({ "7": device({ type: "plug", switch: false }) })),
    );
    await live.refresh();
    expect(stanval(view, "7")).toBe("off");
    live.applyState(7, { switch: true, power_w: 12 });
    expect(stanval(view, "7")).toBe("on · 12 W");
  });

  it("is a no-op for a node that has no row yet", () => {
    const view = harness();
    const live = new LiveController(view, () => Promise.resolve(null));
    expect(() => {
      live.applyState(99, { switch: true });
    }).not.toThrow();
  });

  it("patches the changed channel of a multi-gang switch", async () => {
    const view = harness();
    const live = new LiveController(view, () =>
      Promise.resolve(discovery({ "2": device({ type: "light", endpoints: { "1": true, "2": false } }) })),
    );
    await live.refresh();
    const ep2 = (): string | null | undefined =>
      view.rows.querySelector('tr[data-node="2"][data-ep="2"] .ep-stan')?.textContent;
    expect(ep2()).toBe("off");
    live.applyState(2, { endpoints: { "1": true, "2": true } });
    expect(ep2()).toBe("on");
  });
});

describe("LiveController.applyGlobals", () => {
  it("updates only the field present in the delta", async () => {
    const view = harness();
    const live = new LiveController(view, () =>
      Promise.resolve(discovery({}, { globals: { crib_temp: 20, outdoor_temp: 10 } })),
    );
    await live.refresh();
    expect(view.crib.textContent).toBe("20.0°");
    live.applyGlobals({ crib_temp: 25.2 });
    expect(view.crib.textContent).toBe("25.2°");
    expect(view.outdoor.textContent).toBe("10.0°"); // untouched by a crib-only delta
    live.applyGlobals({ outdoor_temp: null });
    expect(view.outdoor.textContent).toBe("—");
  });
});

describe("LiveController coalescing (deltas during an in-flight refresh)", () => {
  it("queues a state delta and replays it against the fresh row", async () => {
    const view = harness();
    const gate = deferred<Discovery | null>();
    const live = new LiveController(view, () => gate.promise);
    const refreshing = live.refresh(); // in flight: refetch is pending
    live.applyState(7, { switch: true }); // arrives mid-refresh → queued
    gate.resolve(discovery({ "7": device({ type: "plug", switch: false }) }));
    await refreshing;
    expect(stanval(view, "7")).toBe("on"); // pending replayed, not rolled back to "off"
  });

  it("queues a globals delta and does not let the snapshot roll it back", async () => {
    const view = harness();
    const gate = deferred<Discovery | null>();
    const live = new LiveController(view, () => gate.promise);
    const refreshing = live.refresh();
    live.applyGlobals({ crib_temp: 99 }); // newer than the snapshot → queued
    gate.resolve(discovery({}, { globals: { crib_temp: 20, outdoor_temp: 10 } }));
    await refreshing;
    expect(view.crib.textContent).toBe("99.0°");
  });

  it("coalesces overlapping refreshes into a single re-run", async () => {
    const view = harness();
    let calls = 0;
    const gate = deferred<Discovery | null>();
    const live = new LiveController(view, () => {
      calls += 1;
      return calls === 1 ? gate.promise : Promise.resolve(discovery({}));
    });
    const first = live.refresh(); // in flight
    void live.refresh(); // overlapping → sets refreshAgain
    void live.refresh(); // still in flight → no extra run
    gate.resolve(discovery({}));
    await first;
    expect(calls).toBe(2); // the in-flight call + exactly one coalesced re-run
  });
});

describe("LiveController.handleMessage", () => {
  it("dispatches state / globals / discovery_changed and ignores malformed JSON", async () => {
    const view = harness();
    let fetches = 0;
    const live = new LiveController(view, () => {
      fetches += 1;
      return Promise.resolve(
        discovery({ "7": device({ type: "plug", switch: false }) }, {
          globals: { crib_temp: 1, outdoor_temp: 2 },
        }),
      );
    });
    await live.refresh();
    expect(fetches).toBe(1);

    live.handleMessage(JSON.stringify({ type: "state", node: 7, fields: { switch: true } }));
    expect(stanval(view, "7")).toBe("on");

    live.handleMessage(JSON.stringify({ type: "globals", fields: { crib_temp: 9 } }));
    expect(view.crib.textContent).toBe("9.0°");

    live.handleMessage("{not json"); // ignored — must not throw

    live.handleMessage(JSON.stringify({ type: "discovery_changed" }));
    await Promise.resolve();
    await Promise.resolve();
    expect(fetches).toBe(2); // discovery_changed → refetch
  });

  it("sets the conn indicator", () => {
    const view = harness();
    const live = new LiveController(view, () => Promise.resolve(null));
    live.setConnected(false);
    expect(view.conn.textContent).toBe("(reconnecting…)");
    live.setConnected(true);
    expect(view.conn.textContent).toBe("");
  });
});
