import { describe, expect, it } from "vitest";

import type { AuditEvent } from "./api/types";
import { formatAuditTarget, renderAuditFeed, type FetchAudit } from "./audit";
import { device } from "./fixtures";

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
    expect(rows[0]?.textContent ?? "").toContain(new Date(400 * 1000).toLocaleString());
    expect(rows[0]?.textContent ?? "").toContain("🤖");
    expect(rows[1]?.textContent ?? "").toContain("📟");
    expect(rows[2]?.textContent ?? "").toContain("⚙");
    expect(rows[3]?.textContent ?? "").toContain("👤");
    expect(rows[4]?.textContent ?? "").toContain("👤");
    expect(rows[0]?.textContent ?? "").toContain("automation:bedtime");
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

  it("renders a resolved target when a resolver is supplied", async () => {
    const container = document.createElement("div");
    const events: FetchAudit = () =>
      Promise.resolve([auditEvent({ id: 1, ts: 100, action: "setpoint", target: "13", result: "ok" })]);
    const feed = renderAuditFeed(container, events, (target, action) =>
      action === "setpoint" && target === "13" ? "Thermostat · Hall" : target);
    await feed.refresh();
    expect(container.querySelector(".audit-target")?.textContent).toBe("Thermostat · Hall");
  });
});

describe("formatAuditTarget", () => {
  const devices = {
    "13": device({ type: "thermostat", name: "Thermostat", room: "Hall" }),
    "7": device({ type: "light" }), // no name/room
  };

  it("resolves a device-action integer target to name · room", () => {
    expect(formatAuditTarget("13", "setpoint", devices)).toBe("Thermostat · Hall");
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
