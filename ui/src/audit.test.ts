import { describe, expect, it } from "vitest";

import type { AuditEvent } from "./api/types";
import {
  formatAuditAction,
  formatAuditActor,
  formatAuditDetail,
  formatAuditResult,
  formatAuditTarget,
  renderAuditFeed,
  type FetchAudit,
} from "./audit";
import { device } from "./fixtures";
import { currentLocale } from "./i18n";

const flush = (): Promise<void> =>
  new Promise((resolve) => {
    setTimeout(resolve, 0);
  });

function auditEvent(overrides: Partial<AuditEvent> = {}): AuditEvent {
  return {
    id: 1,
    ts: 1_800_000_000,
    actor: "alice",
    action: "login",
    target: "dashboard",
    detail: "from web",
    result: "ok",
    ...overrides,
  };
}

function refreshButton(container: HTMLElement): HTMLButtonElement {
  const button = container.querySelector<HTMLButtonElement>("button");
  if (button === null) throw new Error("missing refresh button");
  return button;
}

describe("renderAuditFeed", () => {
  it("renders events newest-first with timestamps, actor icons, and optional fields", async () => {
    const container = document.createElement("div");
    const events = [
      auditEvent({ id: 1, ts: 100, actor: "<img src=x>", action: "open", target: "door", detail: "", result: "ok" }),
      auditEvent({ id: 2, ts: 400, actor: "automation:bedtime", action: "run", target: "rule", detail: "scene", result: "done" }),
      auditEvent({ id: 3, ts: 300, actor: "device", action: "state", target: "node 7", detail: null, result: "on" }),
      auditEvent({ id: 4, ts: 200, actor: "system", action: "boot", target: null, detail: null, result: null }),
      auditEvent({ id: 5, ts: 50, actor: "anonymous", action: "denied", target: "login", detail: null, result: "401" }),
    ];

    await renderAuditFeed(container, () => Promise.resolve(events)).refresh();

    const rows = [...container.querySelectorAll<HTMLElement>(".audit-row")];
    expect(rows.map((row) => row.dataset.id)).toEqual(["2", "3", "4", "1", "5"]);
    expect(rows[0]?.textContent ?? "").toContain(
      new Date(400 * 1000).toLocaleString(currentLocale(), {
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
      }));
    expect(rows[0]?.querySelector(".audit-ts")?.getAttribute("dir")).toBe("ltr"); // bidi-safe in an RTL feed
    expect(rows[0]?.textContent ?? "").toContain("🤖");
    expect(rows[1]?.textContent ?? "").toContain("📟");
    expect(rows[2]?.textContent ?? "").toContain("⚙");
    expect(rows[3]?.textContent ?? "").toContain("👤");
    expect(rows[4]?.textContent ?? "").toContain("👤");
    expect(rows[0]?.textContent ?? "").toContain("Automation: bedtime"); // actor prefix is localized
    expect(rows[0]?.textContent ?? "").toContain("scene");
    expect(rows[0]?.textContent ?? "").toContain("done");
    expect(rows[2]?.textContent ?? "").not.toContain("null");
    expect(rows[3]?.textContent ?? "").not.toContain("null");
    expect(container.querySelector("img")).toBeNull();
    expect(rows[3]?.textContent ?? "").not.toContain("dashboard");
  });

  it("shows the empty message for an empty, null, or throwing fetch", async () => {
    const container = document.createElement("div");
    const batches: FetchAudit[] = [
      () => Promise.resolve([]),
      () => Promise.resolve(null),
      () => Promise.reject(new Error("offline")),
    ];
    let call = 0;
    const feed = renderAuditFeed(container, () => {
      const fetchAudit = batches[call];
      call += 1;
      return fetchAudit === undefined ? Promise.resolve([]) : fetchAudit();
    });

    await feed.refresh();
    expect(container.querySelector(".audit-empty")?.textContent).toBe("No activity yet");
    await feed.refresh();
    expect(container.querySelector(".audit-empty")?.textContent).toBe("No activity yet");
    await feed.refresh();
    expect(container.querySelector(".audit-empty")?.textContent).toBe("No activity yet");
  });

  it("the Refresh button re-fetches and replaces the list", async () => {
    const container = document.createElement("div");
    const batches: AuditEvent[][] = [
      [auditEvent({ id: 1, actor: "alice", action: "login" })],
      [auditEvent({ id: 2, actor: "system", action: "rotate", target: "session", result: "ok" })],
    ];
    let call = 0;
    const feed = renderAuditFeed(container, () => {
      const events = batches[call] ?? [];
      call += 1;
      return Promise.resolve(events);
    });

    await feed.refresh();
    expect(container.querySelector<HTMLElement>(".audit-row")?.dataset.id).toBe("1");

    refreshButton(container).click();
    await flush();

    expect(call).toBe(2);
    expect(container.querySelector<HTMLElement>(".audit-row")?.dataset.id).toBe("2");
    expect(container.textContent).toContain("rotate");
    expect(container.textContent).not.toContain("login");
  });

  it("is build-once idempotent for the same container", async () => {
    const container = document.createElement("div");
    const first = renderAuditFeed(container, () => Promise.resolve([]));
    const second = renderAuditFeed(container, () =>
      Promise.resolve([auditEvent({ id: 9, actor: "system", action: "unexpected" })]),
    );

    expect(second).toBe(first);
    expect(container.dataset.built).toBe("1");
    expect(container.querySelectorAll(".audit-head")).toHaveLength(1);
    expect(container.querySelectorAll("button")).toHaveLength(1);

    await second.refresh();
    expect(container.querySelector(".audit-empty")?.textContent).toBe("No activity yet");
    expect(container.textContent).not.toContain("unexpected");
  });

  it("resolves device names from the live device map (target + 2-gang detail)", async () => {
    const container = document.createElement("div");
    const events: FetchAudit = () =>
      Promise.resolve([
        auditEvent({ id: 1, ts: 100, actor: "device", action: "endpoints", target: "13",
          detail: "{'1': True, '2': False}", result: "reported" }),
      ]);
    const feed = renderAuditFeed(container, events, () => ({
      "13": device({ type: "switch", name: "Sypialnia", room: "Piętro",
        endpoint_names: { "1": "Lewy", "2": "Prawy" } }),
    }));
    await feed.refresh();
    expect(container.querySelector(".audit-target")?.textContent).toBe("Sypialnia · Piętro");
    expect(container.querySelector(".audit-detail")?.textContent).toBe("Lewy: On · Prawy: Off");
    expect(container.querySelector(".audit-action")?.textContent).toBe("Channels");
    expect(container.querySelector(".audit-actor")?.textContent).toBe("Device");
    expect(container.querySelector(".audit-result")?.textContent).toBe("Reported");
  });

  it("falls back gracefully when no device map is supplied", async () => {
    const container = document.createElement("div");
    const events: FetchAudit = () =>
      Promise.resolve([auditEvent({ id: 1, ts: 100, action: "switch", target: "13", detail: "True", result: "ok" })]);
    await renderAuditFeed(container, events).refresh();
    expect(container.querySelector(".audit-target")?.textContent).toBe("13"); // unknown node → raw
    expect(container.querySelector(".audit-detail")?.textContent).toBe("On");
  });
});

describe("formatAuditActor", () => {
  it("localizes reserved actors and keeps usernames + the rule id raw", () => {
    expect(formatAuditActor("device")).toBe("Device");
    expect(formatAuditActor("system")).toBe("System");
    expect(formatAuditActor("anonymous")).toBe("Anonymous");
    expect(formatAuditActor("automation:bedtime")).toBe("Automation: bedtime");
    expect(formatAuditActor("mateusz")).toBe("mateusz");
  });
});

describe("formatAuditAction", () => {
  it("localizes a known action and passes an unknown one through raw", () => {
    expect(formatAuditAction("switch")).toBe("Switch");
    expect(formatAuditAction("door")).toBe("Door state");
    expect(formatAuditAction("endpoints")).toBe("Channels");
    expect(formatAuditAction("automation_delete")).toBe("Automation deleted");
    expect(formatAuditAction("mystery")).toBe("mystery");
  });
});

describe("formatAuditResult", () => {
  it("localizes the known enums; keeps error/ratio/null raw", () => {
    expect(formatAuditResult("ok")).toBe("OK");
    expect(formatAuditResult("invalid")).toBe("Invalid");
    expect(formatAuditResult("reported")).toBe("Reported");
    expect(formatAuditResult("fired")).toBe("Fired");
    expect(formatAuditResult("error: serial closed")).toBe("error: serial closed");
    expect(formatAuditResult("2/3")).toBe("2/3");
    expect(formatAuditResult(null)).toBeNull();
  });
});

describe("formatAuditDetail", () => {
  const devices = {
    "12": device({ type: "switch", endpoint_names: { "1": "Lewy", "2": "Prawy" } }),
    "13": device({ type: "thermostat" }), // no endpoint names
    "14": device({ type: "blind" }), // cover/level read through the perceptual curve
    "15": device({ type: "light", level: 40 }), // dimmer: level stays linear
  };

  it("maps observed booleans (Python repr) to On/Off", () => {
    expect(formatAuditDetail("True", "switch", "13", devices)).toBe("On");
    expect(formatAuditDetail("False", "thermostat_on", "13", devices)).toBe("Off");
  });

  it("maps door words and JSON booleans", () => {
    expect(formatAuditDetail("open", "door", "13", devices)).toBe("open");
    expect(formatAuditDetail("closed", "door", "13", devices)).toBe("closed");
    expect(formatAuditDetail("true", "switch", "13", devices)).toBe("On");
  });

  it("renders an observed 2-gang map with channel names (named + unnamed fallback)", () => {
    expect(formatAuditDetail("{'1': True, '2': False}", "endpoints", "12", devices))
      .toBe("Lewy: On · Prawy: Off");
    expect(formatAuditDetail("{'1': True, '2': False}", "endpoints", "13", devices))
      .toBe("#1: On · #2: Off"); // node 13 has no endpoint names
  });

  it("summarizes a control-op payload (endpoint → name, on → On/Off, node dropped)", () => {
    expect(formatAuditDetail('{"endpoint": 1, "node": 12, "on": true}', "switch", "12", devices))
      .toBe("Lewy: On");
    expect(formatAuditDetail('{"node": 13, "on": false}', "switch", "13", devices)).toBe("Off");
  });

  it("humanizes numeric values by action (setpoint → temperature, level → percent)", () => {
    expect(formatAuditDetail("21", "setpoint", "13", devices)).toBe("21.0°");
    expect(formatAuditDetail("40", "level", "13", devices)).toBe("40%");
    expect(formatAuditDetail('{"node": 13, "value": 40}', "level", "13", devices)).toBe("40%");
    expect(formatAuditDetail('{"node": 13, "celsius": 22}', "thermostat", "13", devices)).toBe("22.0°");
  });

  it("reads a blind position through the perceptual curve, but keeps a dimmer level linear", () => {
    // a `cover` command (object + scalar) and an observed `level` report on a blind → curved %
    expect(formatAuditDetail('{"node": 14, "value": 64}', "cover", "14", devices)).toBe("50%");
    expect(formatAuditDetail("64", "cover", "14", devices)).toBe("50%");
    expect(formatAuditDetail("64", "level", "14", devices)).toBe("50%"); // blind level report → curved
    // the same wire on a dimmer (node 15, a light) stays linear
    expect(formatAuditDetail("64", "level", "15", devices)).toBe("64%");
  });

  it("shows unrecognized / unparseable / empty details raw, losing nothing", () => {
    expect(formatAuditDetail('{"name": "Salon"}', "name", "13", devices)).toBe('{"name": "Salon"}');
    expect(formatAuditDetail("from web", "login", null, devices)).toBe("from web");
    expect(formatAuditDetail("None", "switch", "13", devices)).toBe("None");
    expect(formatAuditDetail("", "switch", "13", devices)).toBe("");
    expect(formatAuditDetail(null, "switch", "13", devices)).toBeNull();
    expect(formatAuditDetail("99", "switch", "13", devices)).toBe("99"); // number, no % action → raw
    // a malformed numeric field falls back to its raw text, never "NaN%"/"NaN°"
    expect(formatAuditDetail('{"node": 13, "level": "oops"}', "level", "13", devices)).toBe("oops");
    expect(formatAuditDetail('{"node": 13, "celsius": "x"}', "thermostat", "13", devices)).toBe("x");
  });
});

describe("formatAuditTarget", () => {
  const devices = {
    "13": device({ type: "thermostat", name: "Thermostat", room: "Hall" }),
    "7": device({ type: "light" }), // no name/room
  };

  it("resolves a device-action integer target to name · room (incl. a rename)", () => {
    expect(formatAuditTarget("13", "setpoint", devices)).toBe("Thermostat · Hall");
    expect(formatAuditTarget("13", "name", devices)).toBe("Thermostat · Hall"); // /api/name targets a node
  });

  it("uses 'type #node' when the device has no name", () => {
    expect(formatAuditTarget("7", "switch", devices)).toBe("light #7");
  });

  it("keeps the raw target for a non-device action (ir file / rule id)", () => {
    expect(formatAuditTarget("/ext/klima.ir", "ir", devices)).toBe("/ext/klima.ir");
    expect(formatAuditTarget("bedtime", "automation_set", devices)).toBe("bedtime");
  });

  it("keeps the raw target for a non-integer or unknown node, and passes null through", () => {
    expect(formatAuditTarget("nope", "switch", devices)).toBe("nope");
    expect(formatAuditTarget("99", "switch", devices)).toBe("99"); // unknown node
    expect(formatAuditTarget(null, "switch", devices)).toBeNull();
  });
});
