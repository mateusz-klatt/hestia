import { describe, expect, it } from "vitest";

import type { DbStats } from "./api/types";
import { renderDbStats, type FetchDbStats } from "./dbstats";

const flush = (): Promise<void> =>
  new Promise((resolve) => {
    setTimeout(resolve, 0);
  });

function refreshButton(container: HTMLElement): HTMLButtonElement {
  const button = container.querySelector<HTMLButtonElement>("button");
  if (button === null) throw new Error("missing refresh button");
  return button;
}

function stats(overrides: Partial<DbStats> = {}): DbStats {
  return {
    file_bytes: 1_572_864,
    tables: { nodes: 22, automations: 2, audit: 9 },
    ...overrides,
  };
}

describe("renderDbStats", () => {
  it("renders file size and table counts with safe text nodes", async () => {
    const container = document.createElement("div");
    await renderDbStats(container, () =>
      Promise.resolve(stats({ tables: { nodes: 22, automations: 2, "<img src=x>": 1 } })),
    ).refresh();

    expect(container.querySelector("h3")?.textContent).toBe("Database");
    expect(container.querySelector(".dbstats-line")?.textContent).toBe(
      "💾 1.5 MiB · nodes 22 · automations 2 · <img src=x> 1",
    );
    expect(container.querySelector("img")).toBeNull();
  });

  it("formats bytes and KiB, and the Refresh button re-fetches", async () => {
    const container = document.createElement("div");
    const batches = [
      stats({ file_bytes: 512, tables: {} }),
      stats({ file_bytes: 1536, tables: { users: 3 } }),
    ];
    let call = 0;
    const panel = renderDbStats(container, () => {
      const item = batches[call] ?? stats({ file_bytes: 0, tables: {} });
      call += 1;
      return Promise.resolve(item);
    });

    await panel.refresh();
    expect(container.querySelector(".dbstats-line")?.textContent).toBe("💾 512 B");

    refreshButton(container).click();
    await flush();

    expect(call).toBe(2);
    expect(container.querySelector(".dbstats-line")?.textContent).toBe("💾 1.5 KiB · users 3");
  });

  it("shows the icon placeholder for null or throwing fetches", async () => {
    const container = document.createElement("div");
    const batches: FetchDbStats[] = [
      () => Promise.resolve(null),
      () => Promise.reject(new Error("offline")),
    ];
    let call = 0;
    const panel = renderDbStats(container, () => {
      const fetchStats = batches[call];
      call += 1;
      return fetchStats === undefined ? Promise.resolve(null) : fetchStats();
    });

    await panel.refresh();
    expect(container.querySelector(".dbstats-line")?.textContent).toBe("💾 —");
    await panel.refresh();
    expect(container.querySelector(".dbstats-line")?.textContent).toBe("💾 —");
  });

  it("is build-once idempotent for the same container", async () => {
    const container = document.createElement("div");
    const first = renderDbStats(container, () => Promise.resolve(stats()));
    const second = renderDbStats(container, () => Promise.resolve(stats({ file_bytes: 512, tables: {} })));

    expect(second).toBe(first);
    expect(container.dataset.built).toBe("1");
    expect(container.querySelectorAll(".dbstats-head")).toHaveLength(1);
    expect(container.querySelectorAll("button")).toHaveLength(1);

    await second.refresh();
    expect(container.querySelector(".dbstats-line")?.textContent).toContain("1.5 MiB");
  });
});
