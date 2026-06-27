import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Discovery, KlimaState } from "./api/types";
import { device, discovery } from "./fixtures";
import { LiveController, type LiveView } from "./live";

function harness(): LiveView {
  const mk = (tag: string): HTMLElement => document.createElement(tag);
  return {
    hdrText: mk("span"),
    mode: mk("span"),
    crib: mk("span"),
    cribMeta: mk("span"),
    outdoor: mk("span"),
    outdoorHumidity: mk("span"),
    outdoorMeta: mk("span"),
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

/** Flush the microtask queue (lets a re-entrant async refresh settle). */
const flush = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));

const stanval = (view: LiveView, node: string): string | null | undefined =>
  view.rows.querySelector(`tr[data-node="${node}"] .stanval`)?.textContent;

describe("LiveController.refresh", () => {
  it("renders header, globals and rows from the snapshot", async () => {
    const view = harness();
    const data = discovery(
      { "7": device({ type: "plug", confidence: "confirmed", switch: true }) },
      {
        summary: { total: 1, confirmed: 1, unknown: 0 },
        globals: { crib_temp: 22, outdoor_temp: 19.8, outdoor_humidity: 56 },
      },
    );
    await new LiveController(view, () => Promise.resolve(data)).refresh();
    expect(view.hdrText.textContent).toBe("hestia"); // all confirmed, 0 unknown → no parenthetical (title only)
    expect(view.mode.textContent).toBe("mode: standalone (cloud-free)"); // fixture: standalone
    expect(view.crib.textContent).toBe("22.0°");
    expect(view.outdoor.textContent).toBe("19.8°");
    expect(view.outdoorHumidity.textContent).toBe("56%");
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

  it("a throwing onRender hook does not wedge the refresh loop", async () => {
    const view = harness();
    let calls = 0;
    let boom = true;
    const data = discovery({ "7": device({ type: "plug", switch: false }) });
    const live = new LiveController(
      view,
      () => {
        calls += 1;
        return Promise.resolve(data);
      },
      undefined,
      () => {
        if (boom) throw new Error("hook boom"); // a panel hook choking on a bad payload
      },
    );
    // The throw propagates, but `finally` must still release `refreshing`.
    await expect(live.refresh()).rejects.toThrow("hook boom");
    expect(calls).toBe(1);
    // Loop not wedged: a later refresh runs and renders cleanly.
    boom = false;
    await live.refresh();
    expect(calls).toBe(2);
    expect(view.rows.querySelectorAll("tr[data-node]")).toHaveLength(1);
  });
});

describe("LiveController.applyState", () => {
  it("patches the stanval cell of an existing row without a refetch", async () => {
    const view = harness();
    const live = new LiveController(view, () =>
      Promise.resolve(discovery({ "7": device({ type: "plug", switch: false }) })),
    );
    await live.refresh();
    expect(stanval(view, "7")).toBe("⚪ Off");
    live.applyState(7, { switch: true, power_w: 12 });
    expect(stanval(view, "7")).toBe("🟢 On · 12 W");
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
    expect(ep2()).toBe("⚪ Off");
    live.applyState(2, { endpoints: { "1": true, "2": true } });
    expect(ep2()).toBe("🟢 On");
  });

  it("patches the node stanval (not a sub-row) for a single-endpoint light", async () => {
    const view = harness();
    const live = new LiveController(view, () =>
      Promise.resolve(discovery({ "5": device({ type: "light", endpoints: { "1": false } }) })),
    );
    await live.refresh();
    expect(stanval(view, "5")).toBe("⚪ Off");
    live.applyState(5, { endpoints: { "1": true } }); // length 1 → falls through to the node row
    expect(stanval(view, "5")).toBe("🟢 On");
    expect(view.rows.querySelector('tr[data-node="5"][data-ep]')).toBeNull();
  });

  it("refetches once when a delta brings a new endpoint with no sub-row", async () => {
    const view = harness();
    let calls = 0;
    const live = new LiveController(view, () => {
      calls += 1;
      const eps = calls === 1 ? { "1": true, "2": false } : { "1": true, "2": false, "3": true };
      return Promise.resolve(discovery({ "2": device({ type: "light", endpoints: eps }) }));
    });
    await live.refresh();
    expect(calls).toBe(1);
    expect(view.rows.querySelector('tr[data-node="2"][data-ep="3"]')).toBeNull();
    live.applyState(2, { endpoints: { "1": true, "2": false, "3": true } }); // ep 3 has no row yet
    await flush();
    expect(calls).toBe(2); // single recovery refetch rebuilt the sub-rows
    expect(view.rows.querySelector('tr[data-node="2"][data-ep="3"] .ep-stan')?.textContent).toBe("🟢 On");
  });

  it("does not refetch when a multi-gang delta touches only existing channels", async () => {
    const view = harness();
    let calls = 0;
    const live = new LiveController(view, () => {
      calls += 1;
      return Promise.resolve(discovery({ "2": device({ type: "light", endpoints: { "1": true, "2": false } }) }));
    });
    await live.refresh();
    live.applyState(2, { endpoints: { "1": false, "2": true } });
    await flush();
    expect(calls).toBe(1); // no missing endpoint → no over-eager refresh
  });
});

describe("LiveController.applyGlobals", () => {
  it("updates only the field present in the delta", async () => {
    const view = harness();
    const live = new LiveController(view, () =>
      Promise.resolve(
        discovery({}, { globals: { crib_temp: 20, outdoor_temp: 10, outdoor_humidity: 50 } }),
      ),
    );
    await live.refresh();
    expect(view.crib.textContent).toBe("20.0°");
    expect(view.outdoorHumidity.textContent).toBe("50%");
    live.applyGlobals({ crib_temp: 25.2 });
    expect(view.crib.textContent).toBe("25.2°");
    expect(view.outdoor.textContent).toBe("10.0°"); // untouched by a crib-only delta
    expect(view.outdoorHumidity.textContent).toBe("50%"); // untouched too
    live.applyGlobals({ outdoor_temp: null });
    expect(view.outdoor.textContent).toBe("—");
    live.applyGlobals({ outdoor_humidity: 58 }); // 433 push carries humidity
    expect(view.outdoorHumidity.textContent).toBe("58%");
    live.applyGlobals({ outdoor_humidity: null });
    expect(view.outdoorHumidity.textContent).toBe("—");
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
    expect(stanval(view, "7")).toBe("🟢 On"); // pending replayed, not rolled back to "off"
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

  it("merges two state deltas for the same node queued during a refresh", async () => {
    const view = harness();
    const gate = deferred<Discovery | null>();
    const live = new LiveController(view, () => gate.promise);
    const refreshing = live.refresh();
    live.applyState(7, { switch: true }); // queued
    live.applyState(7, { power_w: 12 }); // queued — must merge with the first, not clobber
    gate.resolve(discovery({ "7": device({ type: "plug", switch: false }) }));
    await refreshing;
    expect(stanval(view, "7")).toBe("🟢 On · 12 W"); // both fields survived
  });

  it("merges two globals deltas queued during a refresh", async () => {
    const view = harness();
    const gate = deferred<Discovery | null>();
    const live = new LiveController(view, () => gate.promise);
    const refreshing = live.refresh();
    live.applyGlobals({ crib_temp: 99 }); // queued
    live.applyGlobals({ outdoor_temp: 5 }); // queued — must accumulate with the first
    gate.resolve(discovery({}, { globals: { crib_temp: 20, outdoor_temp: 10 } }));
    await refreshing;
    expect(view.crib.textContent).toBe("99.0°");
    expect(view.outdoor.textContent).toBe("5.0°");
  });

  it("does not drop other queued deltas when a drain triggers a re-entrant refresh", async () => {
    const view = harness();
    let calls = 0;
    const gate = deferred<Discovery | null>();
    const live = new LiveController(view, () => {
      calls += 1;
      if (calls === 1) return gate.promise;
      // Recovery snapshot: ep 3 now present, but node 7 / crib at their PRE-delta
      // values — so only a surviving replay (not the snapshot) can show the deltas.
      return Promise.resolve(
        discovery(
          {
            "2": device({ type: "light", endpoints: { "1": true, "2": false, "3": false } }),
            "7": device({ type: "plug", switch: false }),
          },
          { globals: { crib_temp: 20, outdoor_temp: 10 } },
        ),
      );
    });
    const refreshing = live.refresh();
    live.applyState(2, { endpoints: { "1": true, "2": false, "3": true } }); // brings new ep 3 → drain re-enters refresh
    live.applyState(7, { switch: true }); // must NOT be lost by the re-entrant refresh
    live.applyGlobals({ crib_temp: 99 }); // must NOT be lost either
    gate.resolve(
      discovery(
        { "2": device({ type: "light", endpoints: { "1": true, "2": false } }), "7": device({ type: "plug", switch: false }) },
        { globals: { crib_temp: 20, outdoor_temp: 10 } },
      ),
    );
    await refreshing;
    await flush();
    expect(calls).toBe(2);
    expect(view.rows.querySelector('tr[data-node="2"][data-ep="3"]')).not.toBeNull(); // recovery rebuilt sub-rows
    expect(stanval(view, "7")).toBe("🟢 On"); // queued node-7 delta survived the re-entrant drain
    expect(view.crib.textContent).toBe("99.0°"); // queued globals survived too
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
    expect(stanval(view, "7")).toBe("🟢 On");

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

describe("LiveController edit-safety guard", () => {
  afterEach(() => {
    document.body.replaceChildren(); // focus tracking needs connected elements
  });

  it("skips a rebuild while a name input inside the table is focused", async () => {
    const view = harness();
    document.body.append(view.rows);
    let calls = 0;
    const live = new LiveController(view, () => {
      calls += 1;
      return Promise.resolve(discovery({ "7": device({ type: "light", name: "x" }) }));
    });
    await live.refresh();
    expect(calls).toBe(1);
    const input = view.rows.querySelector<HTMLInputElement>('tr[data-node="7"] input.name');
    input?.focus();
    expect(document.activeElement).toBe(input);
    await live.refresh(); // guard: input focused inside rows → no refetch
    expect(calls).toBe(1);
  });

  it("skips a rebuild while a Save button inside the table is focused", async () => {
    const view = harness();
    document.body.append(view.rows);
    let calls = 0;
    const live = new LiveController(view, () => {
      calls += 1;
      return Promise.resolve(discovery({ "7": device({ type: "light" }) }));
    });
    await live.refresh();
    view.rows.querySelector<HTMLButtonElement>('tr[data-node="7"] .save-name')?.focus();
    await live.refresh(); // guard: button focused inside rows → no refetch (keeps a just-shown status)
    expect(calls).toBe(1);
  });

  it("rebuilds when focus is outside the table", async () => {
    const view = harness();
    document.body.append(view.rows);
    const outside = document.createElement("input");
    document.body.append(outside);
    let calls = 0;
    const live = new LiveController(view, () => {
      calls += 1;
      return Promise.resolve(discovery({}));
    });
    await live.refresh();
    outside.focus();
    await live.refresh(); // focus outside rows → proceeds
    expect(calls).toBe(2);
  });
});

describe("LiveController onRender hook", () => {
  it("fires once with the snapshot on success, and not on a failed load", async () => {
    const view = harness();
    const seen: Discovery[] = [];
    const data = discovery({ "7": device({ type: "plug" }) });
    let ok = true;
    const live = new LiveController(
      view,
      () => Promise.resolve(ok ? data : null),
      undefined,
      (d) => {
        seen.push(d);
      },
    );
    await live.refresh();
    expect(seen).toEqual([data]); // fired with the exact snapshot
    ok = false;
    await live.refresh();
    expect(seen).toHaveLength(1); // NOT fired on a null/failed load
  });
});

describe("LiveController onState hook", () => {
  it("fires with the FULLY-MERGED info after a single-value state delta", async () => {
    const view = harness();
    const seen: [number, boolean | null][] = [];
    const live = new LiveController(
      view,
      () => Promise.resolve(discovery({ "7": device({ type: "plug", switch: false }) })),
      undefined,
      undefined,
      (node, info) => {
        seen.push([node, info.switch]);
      },
    );
    await live.refresh();
    live.applyState(7, { switch: true, power_w: 12 });
    expect(seen).toEqual([[7, true]]); // merged switch value, fired once
  });

  it("fires for a multi-gang channel delta too", async () => {
    const view = harness();
    const seen: number[] = [];
    const live = new LiveController(
      view,
      () => Promise.resolve(discovery({ "2": device({ type: "light", endpoints: { "1": true, "2": false } }) })),
      undefined,
      undefined,
      (node) => {
        seen.push(node);
      },
    );
    await live.refresh();
    live.applyState(2, { endpoints: { "1": true, "2": true } });
    expect(seen).toEqual([2]);
  });

  it("does NOT fire mid-refresh; replays once on drain with the merged value", async () => {
    const view = harness();
    const gate = deferred<Discovery | null>();
    const seen: [number, boolean | null][] = [];
    const live = new LiveController(
      view,
      () => gate.promise,
      undefined,
      undefined,
      (node, info) => {
        seen.push([node, info.switch]);
      },
    );
    const refreshing = live.refresh();
    live.applyState(7, { switch: true }); // queued — must NOT notify while refreshing
    expect(seen).toEqual([]);
    gate.resolve(discovery({ "7": device({ type: "plug", switch: false }) }));
    await refreshing;
    expect(seen).toEqual([[7, true]]); // drainPending replayed it, merged
  });

  it("swallows a throwing onState so the live layer keeps patching", async () => {
    const view = harness();
    let calls = 0;
    const live = new LiveController(
      view,
      () => Promise.resolve(discovery({ "7": device({ type: "plug", switch: false }) })),
      undefined,
      undefined,
      () => {
        calls += 1;
        throw new Error("observer boom");
      },
    );
    await live.refresh();
    expect(() => {
      live.applyState(7, { switch: true });
    }).not.toThrow();
    expect(calls).toBe(1);
    expect(stanval(view, "7")).toBe("🟢 On"); // row still patched despite the observer throwing
    live.applyState(7, { switch: false }); // a later delta still flows
    expect(calls).toBe(2);
    expect(stanval(view, "7")).toBe("⚪ Off");
  });

  it("does not fire for a node with no cached info (unknown row)", async () => {
    const view = harness();
    const seen: number[] = [];
    const live = new LiveController(
      view,
      () => Promise.resolve(discovery({ "7": device({ type: "plug" }) })),
      undefined,
      undefined,
      (node) => {
        seen.push(node);
      },
    );
    await live.refresh();
    live.applyState(99, { switch: true }); // unknown node → returns before merge
    expect(seen).toEqual([]);
  });
});

describe("LiveController decorate hook", () => {
  it("runs the decorator against each node row's actions cell after a rebuild", async () => {
    const view = harness();
    const seen: number[] = [];
    const live = new LiveController(
      view,
      () => Promise.resolve(discovery({ "2": device({ type: "plug" }), "5": device({ type: "light" }) })),
      (tr, node) => {
        seen.push(node);
        tr.querySelector(".actions")?.append("●");
      },
    );
    await live.refresh();
    expect([...seen].sort((a, b) => a - b)).toEqual([2, 5]);
    expect(view.rows.querySelector('tr[data-node="2"] .actions')?.textContent).toBe("●");
    expect(view.rows.querySelector('tr[data-node="5"] .actions')?.textContent).toBe("●");
  });
});

describe("LiveController heatmap (flash / scene / last-seen)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(0);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  const onePlug = (): Discovery => discovery({ "7": device({ type: "plug" }) });

  it("flashes a node row active on activity, then clears after the window", async () => {
    const view = harness();
    const live = new LiveController(view, () => Promise.resolve(onePlug()));
    await live.refresh();
    const row = (): HTMLElement | null => view.rows.querySelector('tr[data-node="7"]');
    expect(row()?.classList.contains("active")).toBe(false);
    live.flash(7);
    expect(row()?.classList.contains("active")).toBe(true);
    vi.advanceTimersByTime(2200); // HIGHLIGHT_MS
    expect(row()?.classList.contains("active")).toBe(false);
  });

  it("handles an activity event with a scene badge that clears after SCENE_MS", async () => {
    const view = harness();
    const live = new LiveController(view, () => Promise.resolve(onePlug()));
    await live.refresh();
    live.handleMessage(JSON.stringify({ type: "activity", node: 7, ts: 1, scene: { id: 3 } }));
    const row = view.rows.querySelector('tr[data-node="7"]');
    expect(row?.classList.contains("active")).toBe(true);
    const badge = row?.querySelector(".scene-badge");
    expect(badge?.textContent).toBe("⏏ scene 3");
    expect(badge?.classList.contains("on")).toBe(true);
    vi.advanceTimersByTime(4000); // SCENE_MS
    expect(badge?.textContent).toBe("");
    expect(badge?.classList.contains("on")).toBe(false);
  });

  it("queues a scene during an in-flight refresh and replays it after", async () => {
    const view = harness();
    const gate = deferred<Discovery | null>();
    const live = new LiveController(view, () => gate.promise);
    const refreshing = live.refresh(); // in flight
    live.flashScene(7, { id: 9, kind: "scene" }); // refreshing → queued (and the row isn't built yet)
    expect(view.rows.querySelector('tr[data-node="7"] .scene-badge')).toBeNull();
    gate.resolve(discovery({ "7": device({ type: "plug" }) }));
    await refreshing;
    const badge = view.rows.querySelector('tr[data-node="7"] .scene-badge');
    expect(badge?.textContent).toBe("⏏ scene 9"); // drainPending replayed it
    expect(badge?.classList.contains("on")).toBe(true);
  });

  it("re-flashing within the window extends the highlight (clears the prior timer)", async () => {
    const view = harness();
    const live = new LiveController(view, () => Promise.resolve(onePlug()));
    await live.refresh();
    const active = (): boolean =>
      view.rows.querySelector('tr[data-node="7"]')?.classList.contains("active") ?? false;
    live.flash(7); // t=0, window ends at 2200
    vi.advanceTimersByTime(1500);
    expect(active()).toBe(true);
    live.flash(7); // t=1500, resets the window to end at 3700
    vi.advanceTimersByTime(800); // t=2300 — past the ORIGINAL 2200 deadline
    expect(active()).toBe(true); // stays active only because the first timer was cleared
    vi.advanceTimersByTime(1400); // t=3700
    expect(active()).toBe(false);
  });

  it("keeps a scene badge when a state patch updates the same row", async () => {
    const view = harness();
    const live = new LiveController(view, () => Promise.resolve(onePlug()));
    await live.refresh();
    live.flashScene(7, { id: 5, kind: "scene" });
    const badge = view.rows.querySelector('tr[data-node="7"] .scene-badge');
    expect(badge?.textContent).toBe("⏏ scene 5");
    live.applyState(7, { switch: true }); // patches .stanval only
    expect(view.rows.querySelector('tr[data-node="7"] .stanval')?.textContent).toBe("🟢 On");
    expect(badge?.textContent).toBe("⏏ scene 5"); // badge survives the state patch
    expect(badge?.classList.contains("on")).toBe(true);
  });

  it("queues a flash for an unbuilt row and replays it after a refresh", async () => {
    const view = harness();
    const live = new LiveController(view, () => Promise.resolve(onePlug()));
    live.flash(7); // no row yet → queued
    expect(view.rows.querySelector('tr[data-node="7"]')).toBeNull();
    await live.refresh();
    expect(view.rows.querySelector('tr[data-node="7"]')?.classList.contains("active")).toBe(true);
  });

  it("flashes only the changed channel of a multi-gang switch", async () => {
    const view = harness();
    const live = new LiveController(view, () =>
      Promise.resolve(discovery({ "2": device({ type: "light", endpoints: { "1": true, "2": false } }) })),
    );
    await live.refresh();
    const sub = (ep: string): HTMLElement | null =>
      view.rows.querySelector(`tr[data-node="2"][data-ep="${ep}"]`);
    live.applyState(2, { endpoints: { "1": true, "2": true } }); // only ep 2 changed
    expect(sub("2")?.classList.contains("active")).toBe(true);
    expect(sub("1")?.classList.contains("active")).toBe(false);
    vi.advanceTimersByTime(2200);
    expect(sub("2")?.classList.contains("active")).toBe(false);
  });

  it("renders relative last-seen times and toggles the recent glow", async () => {
    const view = harness();
    const live = new LiveController(view, () => Promise.resolve(onePlug()));
    await live.refresh();
    const seen = (): string | null | undefined =>
      view.rows.querySelector('tr[data-node="7"] .seen')?.textContent;
    const row = view.rows.querySelector('tr[data-node="7"]');
    expect(seen()).toBe("—"); // never seen
    live.flash(7); // lastActive = 0
    live.tick();
    expect(seen()).toBe("now");
    expect(row?.classList.contains("recent")).toBe(true);
    vi.setSystemTime(5000);
    live.tick();
    expect(seen()).toBe("5s ago");
    vi.setSystemTime(120_000);
    live.tick();
    expect(seen()).toBe("2m ago");
    vi.setSystemTime(2 * 3_600_000);
    live.tick();
    expect(seen()).toBe("2h ago");
    expect(row?.classList.contains("recent")).toBe(false); // older than RECENT_MS
  });

  it("renders + advances the outdoor freshness / low-battery badge", async () => {
    const view = harness();
    // Snapshot: sampled 2 min ago, battery healthy.
    const live = new LiveController(view, () =>
      Promise.resolve(discovery({}, {
        globals: {
          crib_temp: null, outdoor_temp: 21, outdoor_humidity: 40,
          outdoor_temp_ts: new Date(-120_000).toISOString(), outdoor_battery_ok: true,
        },
      })),
    );
    vi.setSystemTime(0);
    await live.refresh();
    const meta = view.outdoorMeta;
    expect(meta.textContent).toBe("2m ago");
    expect(meta.classList.contains("warn")).toBe(false);

    // A 433 push delta: fresh sample, but the battery now reads low → low marker + warn.
    live.applyGlobals({ outdoor_temp_ts: new Date(0).toISOString(), outdoor_battery_ok: false });
    expect(meta.textContent).toBe("now · 🪫 low");
    expect(meta.classList.contains("warn")).toBe(true);

    // No new delta, but 20 min pass: the tick alone re-evaluates → stale (still low).
    vi.setSystemTime(20 * 60_000);
    live.tick();
    expect(meta.textContent).toBe("20m ago · 🪫 low");
    expect(meta.classList.contains("warn")).toBe(true);
  });

  it("renders + advances the crib freshness badge (no battery part)", async () => {
    const view = harness();
    const live = new LiveController(view, () =>
      Promise.resolve(discovery({}, {
        globals: { crib_temp: 22.6, crib_temp_ts: new Date(-60_000).toISOString() },
      })),
    );
    vi.setSystemTime(0);
    await live.refresh();
    expect(view.cribMeta.textContent).toBe("1m ago");
    expect(view.cribMeta.classList.contains("warn")).toBe(false);

    // A niania poll delta carries crib_temp + its ts; 20 min later the tick flags it stale (never a 🪫).
    live.applyGlobals({ crib_temp: 22.7, crib_temp_ts: new Date(0).toISOString() });
    expect(view.cribMeta.textContent).toBe("now");
    vi.setSystemTime(20 * 60_000);
    live.tick();
    expect(view.cribMeta.textContent).toBe("20m ago");
    expect(view.cribMeta.textContent).not.toContain("🪫");
    expect(view.cribMeta.classList.contains("warn")).toBe(true);
  });

  it("does not re-highlight a row whose flash already expired before a rebuild", async () => {
    const view = harness();
    const live = new LiveController(view, () => Promise.resolve(onePlug()));
    await live.refresh();
    live.flash(7); // active at t=0
    vi.advanceTimersByTime(3000); // > HIGHLIGHT_MS: the timer cleared 'active'; lastActive stays 0
    expect(view.rows.querySelector('tr[data-node="7"]')?.classList.contains("active")).toBe(false);
    await live.refresh(); // rebuild → reapply sees a stale timestamp → skips
    expect(view.rows.querySelector('tr[data-node="7"]')?.classList.contains("active")).toBe(false);
  });

  it("prunes activity for a node that vanished and whose flash expired", async () => {
    const view = harness();
    let present = true;
    const live = new LiveController(view, () => Promise.resolve(present ? onePlug() : discovery({})));
    await live.refresh();
    live.flash(7);
    vi.advanceTimersByTime(3000); // flash expired
    present = false;
    await live.refresh(); // node 7 gone + expired → pruned
    present = true;
    await live.refresh(); // node 7 back; its last-seen must be "—", not a stale time
    expect(view.rows.querySelector('tr[data-node="7"] .seen')?.textContent).toBe("—");
  });
});

describe("LiveController klima (A/C status)", () => {
  const cool22: KlimaState = { power: true, mode: "cool", temp: 22 };
  const off: KlimaState = { power: false, mode: "cool", temp: 22 };

  it("routes a klima SSE event to the onKlima hook", () => {
    const seen: (KlimaState | null)[] = [];
    const live = new LiveController(harness(), () => Promise.resolve(null),
      undefined, undefined, undefined, (s) => seen.push(s));
    live.handleMessage(JSON.stringify({ type: "klima", klima: cool22 }));
    expect(seen).toEqual([cool22]);
  });

  it("pushes the snapshot's klima_state through onKlima on render", async () => {
    const seen: (KlimaState | null)[] = [];
    const live = new LiveController(harness(), () => Promise.resolve(discovery({}, { klima_state: off })),
      undefined, undefined, undefined, (s) => seen.push(s));
    await live.refresh();
    expect(seen).toEqual([off]);
  });

  it("queues a klima delta during a refresh and replays it after the snapshot", async () => {
    const seen: (KlimaState | null)[] = [];
    const gate = deferred<Discovery | null>();
    const live = new LiveController(harness(), () => gate.promise,
      undefined, undefined, undefined, (s) => seen.push(s));
    const refreshing = live.refresh();
    live.applyKlima(cool22); // newer than the snapshot → queued
    gate.resolve(discovery({}, { klima_state: off }));
    await refreshing;
    expect(seen).toEqual([off, cool22]); // snapshot first, then the fresher delta — not rolled back
  });

  it("replays a queued null klima delta (distinct from nothing queued)", async () => {
    const seen: (KlimaState | null)[] = [];
    const gate = deferred<Discovery | null>();
    const live = new LiveController(harness(), () => gate.promise,
      undefined, undefined, undefined, (s) => seen.push(s));
    const refreshing = live.refresh();
    live.applyKlima(null); // A/C cleared mid-refresh → still must replay
    gate.resolve(discovery({}, { klima_state: cool22 }));
    await refreshing;
    expect(seen).toEqual([cool22, null]);
  });
});
