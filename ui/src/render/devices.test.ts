import { describe, expect, it } from "vitest";

import { device } from "../fixtures";
import { deviceRow, renderDeviceRows, summaryText } from "./devices";

describe("summaryText", () => {
  it("renders the confirmed/total/unknown counts", () => {
    expect(summaryText({ total: 22, confirmed: 19, unknown: 3 })).toBe(
      "hestia — devices (19/22 confirmed, 3 unknown)",
    );
  });
});

describe("deviceRow", () => {
  it("lays out node / seen / battery / type / stan / name / room", () => {
    const tr = deviceRow(
      "7",
      device({
        type: "plug",
        confidence: "confirmed",
        battery: 80,
        switch: true,
        power_w: 12,
        name: "fridge",
        room: "kitchen",
      }),
    );
    const tds = tr.querySelectorAll("td");
    expect(tds).toHaveLength(7);
    expect(tds[0]?.textContent).toBe("7");
    expect(tds[1]?.textContent).toBe("—"); // last seen — static until SSE (PR-3)
    expect(tds[2]?.textContent).toBe("80%");
    expect(tds[3]?.textContent).toBe("plug (confirmed)");
    expect(tds[3]?.querySelector(".confirmed")).not.toBeNull();
    expect(tds[4]?.textContent).toBe("on · 12 W");
    expect(tds[5]?.textContent).toBe("fridge");
    expect(tds[6]?.textContent).toBe("kitchen");
    expect(tr.dataset.node).toBe("7");
    expect(tr.dataset.type).toBe("plug");
  });

  it("marks a low-battery cell with the 'low' class", () => {
    const batt = deviceRow("3", device({ battery: 255 })).querySelectorAll("td")[2];
    expect(batt?.classList.contains("low")).toBe(true);
    expect(batt?.textContent).toBe("low");
  });

  it("does not mark the confirmed class for an inferred type", () => {
    const tr = deviceRow("3", device({ type: "light", confidence: "inferred" }));
    expect(tr.querySelector(".confirmed")).toBeNull();
  });

  it("renders a hostile name as inert text (no escaping needed — textContent)", () => {
    const nameCell = deviceRow(
      "9",
      device({ name: "<img src=x onerror=alert(1)>" }),
    ).querySelectorAll("td")[5];
    expect(nameCell?.querySelector("img")).toBeNull();
    expect(nameCell?.textContent).toBe("<img src=x onerror=alert(1)>");
  });

  it("falls back to '?' for an empty inferred type/confidence", () => {
    const span = deviceRow("1", device({ type: "", confidence: "" })).querySelectorAll("td")[3];
    expect(span?.textContent).toBe("? (?)");
  });
});

describe("renderDeviceRows", () => {
  it("sorts by numeric node id and emits read-only multi-gang sub-rows", () => {
    const tbody = document.createElement("tbody");
    renderDeviceRows(tbody, {
      "10": device({ type: "light", switch: true }),
      "2": device({
        type: "light",
        endpoints: { "1": true, "2": false },
        endpoint_names: { "1": "lewy" },
      }),
    });
    const rows = tbody.querySelectorAll("tr");
    expect(rows).toHaveLength(4); // node 2 + 2 sub-rows, then node 10
    expect(rows[0]?.dataset.node).toBe("2"); // numeric sort: 2 before 10
    expect(rows[1]?.classList.contains("subrow")).toBe(true);
    expect(rows[1]?.dataset.ep).toBe("1");
    expect(rows[1]?.textContent).toContain("lewy");
    expect(rows[1]?.textContent).toContain("on");
    expect(rows[2]?.dataset.ep).toBe("2");
    expect(rows[2]?.textContent).toContain("off");
    expect(rows[3]?.dataset.node).toBe("10");
  });

  it("does not emit sub-rows for a single-endpoint switch", () => {
    const tbody = document.createElement("tbody");
    renderDeviceRows(tbody, { "5": device({ type: "light", endpoints: { "1": true } }) });
    expect(tbody.querySelectorAll("tr")).toHaveLength(1);
    expect(tbody.querySelector(".subrow")).toBeNull();
  });

  it("clears previous rows on re-render", () => {
    const tbody = document.createElement("tbody");
    renderDeviceRows(tbody, { "1": device() });
    renderDeviceRows(tbody, { "2": device(), "3": device() });
    const rows = tbody.querySelectorAll("tr");
    expect(rows).toHaveLength(2);
    expect(rows[0]?.dataset.node).toBe("2");
  });
});
