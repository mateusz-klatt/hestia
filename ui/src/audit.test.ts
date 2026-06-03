import { describe, expect, it } from "vitest";

import type { AuditEvent } from "./api/types";
import { renderAuditFeed, type FetchAudit } from "./audit";

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
});
