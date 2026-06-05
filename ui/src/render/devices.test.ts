import { describe, expect, it } from "vitest";

import { device, discovery } from "../fixtures";
import {
  deviceRow,
  modeText,
  renderDeviceRows,
  renderDiscovery,
  renderGlobals,
  summaryText,
} from "./devices";

describe("modeText", () => {
  it("notes cloud-free when running standalone", () => {
    expect(modeText({ mode: "standalone", target_mode: "standalone", env_override: null })).toBe(
      "mode: standalone (cloud-free)",
    );
  });
  it("is plain when running proxy with nothing pending", () => {
    expect(modeText({ mode: "proxy", target_mode: "proxy", env_override: null })).toBe("mode: proxy");
  });
  it("flags a saved-but-not-applied standalone graduation", () => {
    expect(modeText({ mode: "proxy", target_mode: "standalone", env_override: null })).toBe(
      "mode: proxy → standalone saved — restart hestia",
    );
  });
  it("flags an env-pinned mode", () => {
    expect(modeText({ mode: "proxy", target_mode: "standalone", env_override: "proxy" })).toBe(
      "mode: proxy (HESTIA_MODE=proxy forces the mode; saved: standalone)",
    );
  });
});

describe("summaryText", () => {
  it("shows confirmed/total + unknown when there is something to flag", () => {
    expect(summaryText({ total: 22, confirmed: 19, unknown: 3 })).toBe(
      "hestia (19/22 confirmed, 3 unknown)",
    );
  });
  it("omits the unknown part when there are none", () => {
    expect(summaryText({ total: 22, confirmed: 20, unknown: 0 })).toBe("hestia (20/22 confirmed)");
  });
  it("drops the whole parenthetical once everything is confirmed", () => {
    expect(summaryText({ total: 22, confirmed: 22, unknown: 0 })).toBe("hestia");
  });
});

describe("deviceRow", () => {
  it("lays out node / seen / battery / type / stan / akcje / name / room", () => {
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
    expect(tds).toHaveLength(8);
    expect(tds[0]?.textContent).toBe("7");
    expect(tds[1]?.textContent).toBe("—"); // last seen — static until SSE drives it
    expect(tds[2]?.textContent).toBe("80%");
    expect(tds[3]?.querySelector("span")?.textContent).toBe("plug"); // confirmed → no "(confirmed)" word
    expect(tds[3]?.querySelector(".confirmed")).not.toBeNull();      // the green colour IS the signal
    expect(tds[3]?.querySelector(".confirm")).toBeNull();            // confirmed → the disabled button is omitted
    expect(tds[4]?.textContent).toBe("🟢 On · 12 W");
    expect(tds[5]?.classList.contains("actions")).toBe(true); // akcje — empty until decorated
    expect(tds[6]?.querySelector<HTMLInputElement>("input.name")?.value).toBe("fridge");
    expect(tds[7]?.querySelector<HTMLInputElement>("input.room")?.value).toBe("kitchen");
    expect(tr.dataset.node).toBe("7");
    expect(tr.dataset.type).toBe("plug");
  });

  it("tags every cell with a data-label for the mobile card layout", () => {
    const tr = deviceRow("7", device({ type: "plug", name: "fridge", room: "kitchen" }));
    const labels = [...tr.querySelectorAll("td")].map((td) => td.dataset.label);
    expect(labels).toEqual(["node", "last seen", "battery", "inferred type", "state", "actions", "name", "room"]);
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

  it("shows an enabled confirm button for an inferred type and a disabled one for unknown", () => {
    const inferred = deviceRow("3", device({ type: "light", confidence: "inferred" })).querySelectorAll("td")[3];
    expect(inferred?.querySelector("span")?.textContent).toBe("light (inferred)"); // keeps the confidence note
    expect(inferred?.querySelector<HTMLButtonElement>(".confirm")?.disabled).toBe(false); // a type to confirm
    const unknown = deviceRow("4", device({ type: "unknown", confidence: "guess" })).querySelectorAll("td")[3];
    expect(unknown?.querySelector<HTMLButtonElement>(".confirm")?.disabled).toBe(true); // nothing to confirm yet
  });

  it("keeps a hostile name inert — set via input.value, never parsed as HTML", () => {
    const nameCell = deviceRow(
      "9",
      device({ name: "<img src=x onerror=alert(1)>" }),
    ).querySelectorAll("td")[6];
    expect(nameCell?.querySelector("img")).toBeNull();
    expect(nameCell?.querySelector<HTMLInputElement>("input.name")?.value).toBe(
      "<img src=x onerror=alert(1)>",
    );
  });

  it("falls back to '?' for an empty inferred type/confidence", () => {
    const span = deviceRow("1", device({ type: "", confidence: "" }))
      .querySelectorAll("td")[3]
      ?.querySelector("span");
    expect(span?.textContent).toBe("? (?)");
  });
});

describe("renderDeviceRows", () => {
  it("sorts by numeric node id and emits multi-gang sub-rows", () => {
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
    expect(rows[1]?.dataset.node).toBe("2"); // shares the parent node id (addressable by SSE)
    expect(rows[1]?.dataset.ep).toBe("1");
    expect(rows[1]?.querySelector(".sub-label")?.textContent).toBe("↳ channel 1");
    expect(rows[1]?.querySelectorAll("td")[4]?.textContent).toBe("🟢 On"); // stan
    expect(rows[1]?.querySelectorAll("td")[5]?.classList.contains("actions")).toBe(true);
    expect(rows[1]?.querySelectorAll("td")[6]?.querySelector<HTMLInputElement>("input.ep-name")?.value).toBe("lewy"); // labelled channel
    expect(rows[2]?.dataset.ep).toBe("2");
    expect(rows[2]?.querySelectorAll("td")[4]?.textContent).toBe("⚪ Off");
    expect(rows[2]?.querySelectorAll("td")[6]?.querySelector<HTMLInputElement>("input.ep-name")?.value).toBe(""); // ep 2 unlabelled → per-key `?? ""`
    expect(rows[3]?.dataset.node).toBe("10");
  });

  it("renders multi-gang sub-rows with no endpoint_names (empty name cells)", () => {
    const tbody = document.createElement("tbody");
    renderDeviceRows(tbody, {
      "4": device({ type: "light", endpoints: { "1": true, "2": false } }),
    });
    const rows = tbody.querySelectorAll("tr"); // exercises the whole-object `endpoint_names ?? {}` fallback
    expect(rows).toHaveLength(3); // node + 2 sub-rows
    expect(rows[1]?.dataset.ep).toBe("1");
    expect(rows[1]?.querySelector(".sub-label")?.textContent).toBe("↳ channel 1");
    expect(rows[1]?.querySelectorAll("td")[4]?.textContent).toBe("🟢 On");
    expect(rows[1]?.querySelectorAll("td")[6]?.querySelector<HTMLInputElement>("input.ep-name")?.value).toBe("");
    expect(rows[2]?.dataset.ep).toBe("2");
    expect(rows[2]?.querySelector(".sub-label")?.textContent).toBe("↳ channel 2");
    expect(rows[2]?.querySelectorAll("td")[4]?.textContent).toBe("⚪ Off");
    expect(rows[2]?.querySelectorAll("td")[6]?.querySelector<HTMLInputElement>("input.ep-name")?.value).toBe("");
  });

  it("labels only the meaningful sub-row cells (empty placeholders stay unlabeled → hidden on mobile)", () => {
    const tbody = document.createElement("tbody");
    renderDeviceRows(tbody, {
      "2": device({ type: "light", endpoints: { "1": true, "2": false }, endpoint_names: { "1": "lewy" } }),
    });
    const sub = tbody.querySelector("tr.subrow");
    const labels = [...(sub?.querySelectorAll("td") ?? [])].map((td) => td.dataset.label);
    // Lock the full placeholder contract: node / last-seen / battery / kanał / room cells stay unlabeled;
    // per-channel stan, endpoint actions, and editable name carry headings.
    expect(labels).toEqual([undefined, undefined, undefined, undefined, "state", "actions", "name", undefined]);
    expect(sub?.querySelector(".sub-label")?.textContent).toBe("↳ channel 1"); // self-describing → no data-label
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

describe("renderGlobals", () => {
  it("writes temp + humidity into each cell, including null → em dash", () => {
    const crib = document.createElement("span");
    const outdoor = document.createElement("span");
    const humidity = document.createElement("span");
    // Always writes every cell (the contract guarantees the keys, null when a
    // poller is off) — unlike the legacy `if ('crib_temp' in g)` guard.
    renderGlobals(crib, outdoor, humidity, { crib_temp: 25.2, outdoor_temp: 19.8, outdoor_humidity: 56 });
    expect(crib.textContent).toBe("25.2°");
    expect(outdoor.textContent).toBe("19.8°");
    expect(humidity.textContent).toBe("56%");
  });

  it("renders em dashes when pollers are off / humidity absent", () => {
    const crib = document.createElement("span");
    const outdoor = document.createElement("span");
    const humidity = document.createElement("span");
    // outdoor_temp present but humidity null (e.g. the open-meteo source) → temp shown, humidity em dash.
    renderGlobals(crib, outdoor, humidity, { crib_temp: null, outdoor_temp: 12.3, outdoor_humidity: null });
    expect(crib.textContent).toBe("—");
    expect(outdoor.textContent).toBe("12.3°");
    expect(humidity.textContent).toBe("—");
  });
});

describe("renderDiscovery", () => {
  it("populates the header, globals and table from a discovery payload", () => {
    const view = {
      hdrText: document.createElement("span"),
      crib: document.createElement("span"),
      outdoor: document.createElement("span"),
      outdoorHumidity: document.createElement("span"),
      rows: document.createElement("tbody"),
    };
    const data = discovery(
      { "7": device({ type: "plug", confidence: "confirmed", switch: true }) },
      {
        summary: { total: 1, confirmed: 1, unknown: 0 },
        globals: { crib_temp: 22, outdoor_temp: 14.5, outdoor_humidity: 61 },
      },
    );
    renderDiscovery(view, data);
    expect(view.hdrText.textContent).toBe("hestia"); // all confirmed, 0 unknown → no parenthetical
    expect(view.crib.textContent).toBe("22.0°");
    expect(view.outdoor.textContent).toBe("14.5°");
    expect(view.outdoorHumidity.textContent).toBe("61%");
    expect(view.rows.querySelectorAll("tr")).toHaveLength(1);
    expect(view.rows.querySelector("tr")?.dataset.node).toBe("7");
  });
});
