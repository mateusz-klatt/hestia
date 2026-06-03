import { afterEach, describe, expect, it } from "vitest";

import type { ControlOp } from "./api/types";
import { device, discovery } from "./fixtures";
import { createRoomsView } from "./rooms";

/** A rooms view backed by a recording postControl, so control dispatch is observable. */
function mk(): { container: HTMLElement; sent: ControlOp[]; view: ReturnType<typeof createRoomsView> } {
  const container = document.createElement("div");
  const sent: ControlOp[] = [];
  const view = createRoomsView(container, {
    postControl: (op) => {
      sent.push(op);
      return Promise.resolve({ ok: true });
    },
  });
  return { container, sent, view };
}

const openFirstRoom = (container: HTMLElement): void => {
  container.querySelector<HTMLButtonElement>(".room-card")?.click();
};

describe("createRoomsView — landing", () => {
  it("shows a loading placeholder before any snapshot", () => {
    const { container, view } = mk();
    view.update(null);
    expect(container.querySelector(".room-placeholder")?.textContent).toBe("ładowanie…");
  });

  it("shows 'Brak urządzeń' when there are no devices", () => {
    const { container, view } = mk();
    view.update(discovery({}));
    expect(container.querySelector(".room-placeholder")?.textContent).toBe("Brak urządzeń");
  });

  it("renders a card per room (alphabetical, Inne last) with a pluralised count", () => {
    const { container, view } = mk();
    view.update(
      discovery({
        "1": device({ type: "light", room: "Salon" }),
        "2": device({ type: "plug", room: "Salon" }),
        "3": device({ type: "blind", room: "Kuchnia" }),
        "4": device({ type: "door" }), // no room → Inne
      }),
    );
    const cards = container.querySelectorAll(".room-card");
    expect([...cards].map((c) => c.querySelector(".room-card-title")?.textContent)).toEqual([
      "Kuchnia",
      "Salon",
      "Inne",
    ]);
    expect([...cards].map((c) => c.querySelector(".room-card-count")?.textContent)).toEqual([
      "1 urządzenie",
      "2 urządzenia",
      "1 urządzenie",
    ]);
  });

  it("treats a blank/whitespace room as Inne", () => {
    const { container, view } = mk();
    view.update(discovery({ "1": device({ type: "light", room: "   " }) }));
    expect(container.querySelector(".room-card-title")?.textContent).toBe("Inne");
  });
});

describe("createRoomsView — room detail", () => {
  it("opens a room's devices on tap, reuses renderActions, and dispatches a control op", () => {
    const { container, view, sent } = mk();
    view.update(discovery({ "5": device({ type: "light", switch: false, room: "Salon", name: "Lampa" }) }));
    openFirstRoom(container);
    expect(container.querySelector(".room-title")?.textContent).toBe("Salon");
    const card = container.querySelector(".room-device");
    expect(card?.querySelector(".room-device-name")?.textContent).toBe("Lampa");
    expect(card?.querySelector(".room-device-stan")?.textContent).toBe("off");
    [...container.querySelectorAll<HTMLButtonElement>(".room-device-actions button")]
      .find((b) => b.textContent === "Wł")
      ?.click();
    expect(sent).toEqual([{ op: "switch", node: 5, on: true }]);
  });

  it("goToLanding returns to the room list (the 🏠 Pokoje tab calls it — no in-detail back button)", () => {
    const { container, view } = mk();
    view.update(discovery({ "5": device({ type: "light", room: "Salon" }) }));
    openFirstRoom(container);
    expect(container.querySelector(".room-back")).toBeNull(); // back button removed
    view.goToLanding();
    expect(container.querySelector(".room-card")).not.toBeNull();
    expect(container.querySelector(".room-device")).toBeNull();
  });

  it("labels an unnamed device as 'type #node'", () => {
    const { container, view } = mk();
    view.update(discovery({ "7": device({ type: "plug", room: "Salon" }) }));
    openFirstRoom(container);
    expect(container.querySelector(".room-device-name")?.textContent).toBe("plug #7");
  });

  it("renders a hostile device name as text, never HTML", () => {
    const { container, view } = mk();
    view.update(
      discovery({ "1": device({ type: "light", room: "Salon", name: "<img src=x onerror=alert(1)>" }) }),
    );
    openFirstRoom(container);
    const name = container.querySelector(".room-device-name");
    expect(name?.querySelector("img")).toBeNull();
    expect(name?.textContent).toBe("<img src=x onerror=alert(1)>");
  });

  it("summarises a multi-gang switch's channels and shows no controls (read-only)", () => {
    const { container, view } = mk();
    view.update(
      discovery({ "2": device({ type: "light", room: "Salon", endpoints: { "1": true, "2": false } }) }),
    );
    openFirstRoom(container);
    expect(container.querySelector(".room-device-stan")?.textContent).toBe("1: wł · 2: wył");
    expect(container.querySelectorAll(".room-device-actions button")).toHaveLength(0);
  });
});

describe("createRoomsView — live updates", () => {
  it("patchState updates the visible card's state text in place", () => {
    const { container, view } = mk();
    view.update(discovery({ "9": device({ type: "light", switch: false, room: "Salon" }) }));
    openFirstRoom(container);
    expect(container.querySelector(".room-device-stan")?.textContent).toBe("off");
    view.patchState(9, device({ type: "light", switch: true }));
    expect(container.querySelector(".room-device-stan")?.textContent).toBe("on");
  });

  it("patchState is a no-op when the node's card is not currently shown (landing)", () => {
    const { container, view } = mk();
    view.update(discovery({ "9": device({ type: "light", room: "Salon" }) }));
    expect(() => {
      view.patchState(9, device({ type: "light", switch: true }));
    }).not.toThrow();
    expect(container.querySelector(".room-device")).toBeNull(); // still on the landing
  });

  it("keeps the open room on a snapshot refresh, re-rendering its cards", () => {
    const { container, view } = mk();
    view.update(discovery({ "1": device({ type: "light", switch: false, room: "Salon" }) }));
    openFirstRoom(container);
    view.update(discovery({ "1": device({ type: "light", switch: true, room: "Salon" }) }));
    expect(container.querySelector(".room-title")?.textContent).toBe("Salon");
    expect(container.querySelector(".room-device-stan")?.textContent).toBe("on");
  });

  it("falls back to the landing when the open room loses all its devices", () => {
    const { container, view } = mk();
    view.update(discovery({ "1": device({ type: "light", room: "Salon" }) }));
    openFirstRoom(container);
    expect(container.querySelector(".room-title")?.textContent).toBe("Salon");
    view.update(discovery({ "1": device({ type: "light", room: "Kuchnia" }) })); // Salon gone
    expect(container.querySelector(".room-title")).toBeNull(); // back on the landing
    expect(container.querySelector(".room-card-title")?.textContent).toBe("Kuchnia");
  });

  it("patchState leaves the action buttons intact (no rebuild → in-flight lock preserved)", () => {
    const { container, view } = mk();
    view.update(discovery({ "5": device({ type: "light", switch: false, room: "Salon" }) }));
    openFirstRoom(container);
    const btn = container.querySelector<HTMLButtonElement>(".room-device-actions button");
    view.patchState(5, device({ type: "light", switch: true })); // a live delta patches text only
    expect(container.querySelector<HTMLButtonElement>(".room-device-actions button")).toBe(btn); // same node
    expect(container.querySelector(".room-device-stan")?.textContent).toBe("on");
  });
});

describe("createRoomsView — rebuild safety vs a focused control", () => {
  afterEach(() => {
    document.body.replaceChildren(); // focus tracking needs connected elements
  });

  it("does not rebuild the cards while a room control is focused (keeps the in-flight lock)", () => {
    const { container, view } = mk();
    document.body.append(container);
    view.update(discovery({ "5": device({ type: "light", switch: false, room: "Salon" }) }));
    openFirstRoom(container);
    const before = container.querySelector(".room-device");
    container.querySelector<HTMLButtonElement>(".room-device-actions button")?.focus();
    view.update(discovery({ "5": device({ type: "light", switch: true, room: "Salon" }) }));
    expect(container.querySelector(".room-device")).toBe(before); // same card — not rebuilt
  });

  it("rebuilds when focus is outside the room view", () => {
    const { container, view } = mk();
    document.body.append(container);
    const outside = document.createElement("input");
    document.body.append(outside);
    view.update(discovery({ "5": device({ type: "light", switch: false, room: "Salon" }) }));
    openFirstRoom(container);
    const before = container.querySelector(".room-device");
    outside.focus();
    view.update(discovery({ "5": device({ type: "light", switch: true, room: "Salon" }) }));
    const after = container.querySelector(".room-device");
    expect(after).not.toBe(before); // rebuilt
    expect(after?.querySelector(".room-device-stan")?.textContent).toBe("on");
  });
});
