import { afterEach, describe, expect, it } from "vitest";

import type { ControlOp } from "./api/types";
import { device, discovery } from "./fixtures";
import { createRoomsView } from "./rooms";

interface SavedRoomIcon {
  room: string;
  icon: string;
}

/** A rooms view backed by a recording postControl, so control dispatch is observable. */
function mk(initialIcons: Record<string, string> = {}): {
  container: HTMLElement;
  roomIcons: () => Record<string, string>;
  savedIcons: SavedRoomIcon[];
  sent: ControlOp[];
  view: ReturnType<typeof createRoomsView>;
} {
  const container = document.createElement("div");
  const sent: ControlOp[] = [];
  let icons = { ...initialIcons };
  const savedIcons: SavedRoomIcon[] = [];
  const view = createRoomsView(container, {
    postControl: (op) => {
      sent.push(op);
      return Promise.resolve({ ok: true });
    },
    roomIcons: () => icons,
    saveRoomIcon: (room, icon) => {
      savedIcons.push({ room, icon });
      if (icon === "") {
        icons = Object.fromEntries(Object.entries(icons).filter(([key]) => key !== room));
      } else {
        icons = { ...icons, [room]: icon };
      }
      return Promise.resolve();
    },
    renderWholeHome: (c) => {
      const stub = document.createElement("button");
      stub.className = "scene-stub";
      stub.textContent = "scenes";
      c.appendChild(stub);
    },
  });
  return { container, roomIcons: () => icons, savedIcons, sent, view };
}

const openFirstRoom = (container: HTMLElement): void => {
  container.querySelector<HTMLButtonElement>(".room-card")?.click();
};
const flush = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));

describe("createRoomsView — landing", () => {
  it("shows a loading placeholder before any snapshot", () => {
    const { container, view } = mk();
    view.update(null);
    expect(container.querySelector(".room-placeholder")?.textContent).toBe("Loading…");
  });

  it("shows 'Brak urządzeń' when there are no devices", () => {
    const { container, view } = mk();
    view.update(discovery({}));
    expect(container.querySelector(".room-placeholder")?.textContent).toBe("No devices");
  });

  it("renders a card per room (alphabetical, Inne last) with a pluralised count", () => {
    const { container, view } = mk({ Kuchnia: "🍳", Salon: "🛋️" });
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
      "Other",
    ]);
    expect([...cards].map((c) => c.querySelector(".room-card-icon")?.textContent)).toEqual(["🍳", "🛋️", "🚪"]);
    expect([...cards].map((c) => c.querySelector(".room-card-count")?.textContent)).toEqual([
      "1 device",
      "2 devices",
      "1 device",
    ]);
  });

  it("treats a blank/whitespace room as Inne", () => {
    const { container, view } = mk();
    view.update(discovery({ "1": device({ type: "light", room: "   " }) }));
    expect(container.querySelector(".room-card-title")?.textContent).toBe("Other");
  });

  it("has no edit-icons toggle on the landing (the entry point moved to settings)", () => {
    const { container, view } = mk({ Salon: "🛋️" });
    view.update(discovery({ "1": device({ type: "light", room: "Salon" }) }));
    expect(container.querySelector(".room-icon-edit-toggle")).toBeNull(); // not on the rooms screen
    expect(container.querySelector(".room-card")).not.toBeNull();         // just the room grid
  });

  it("enterIconEdit shows the editor; the Done button (and goToLanding) return to the grid", () => {
    const { container, view } = mk({ Salon: "🛋️" });
    view.update(discovery({ "1": device({ type: "light", room: "Salon" }) }));
    view.enterIconEdit();
    expect(container.querySelector(".room-card")).toBeNull();             // editor, not the grid
    expect(container.querySelector(".room-icon-select")).not.toBeNull();
    container.querySelector<HTMLButtonElement>(".room-icon-edit-done")?.click();
    expect(container.querySelector(".room-card")).not.toBeNull();         // back to the grid
    view.enterIconEdit();
    expect(container.querySelector(".room-icon-select")).not.toBeNull();
    view.goToLanding();                                                   // tapping 🏠 Pokoje also exits
    expect(container.querySelector(".room-icon-select")).toBeNull();
    expect(container.querySelector(".room-card")).not.toBeNull();
  });

  it("toggles icon edit mode and saves preset or clear selections", async () => {
    const { container, roomIcons, savedIcons, view } = mk({ Salon: "🛋️" });
    view.update(discovery({ "1": device({ type: "light", room: "Salon" }) }));

    view.enterIconEdit();
    expect(container.querySelector(".room-card")).toBeNull();
    expect(container.querySelector(".room-icon-name")?.textContent).toBe("Salon");
    const select = container.querySelector<HTMLSelectElement>(".room-icon-select");
    expect(select?.value).toBe("🛋️");

    if (select !== null) {
      select.value = "🍳";
      select.dispatchEvent(new Event("change"));
    }
    await flush();
    expect(savedIcons).toEqual([{ room: "Salon", icon: "🍳" }]);
    expect(roomIcons()).toEqual({ Salon: "🍳" });
    expect(container.querySelector<HTMLSelectElement>(".room-icon-select")?.value).toBe("🍳");

    const updatedSelect = container.querySelector<HTMLSelectElement>(".room-icon-select");
    if (updatedSelect !== null) {
      updatedSelect.value = "";
      updatedSelect.dispatchEvent(new Event("change"));
    }
    await flush();
    expect(savedIcons).toEqual([
      { room: "Salon", icon: "🍳" },
      { room: "Salon", icon: "" },
    ]);
    expect(roomIcons()).toEqual({});
    expect(container.querySelector<HTMLSelectElement>(".room-icon-select")?.value).toBe("");
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
    expect(card?.querySelector(".room-device-stan")?.textContent).toBe("⚪ Off");
    [...container.querySelectorAll<HTMLButtonElement>(".room-device-actions button")]
      .find((b) => b.textContent === "On")
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

  it("summarises a multi-gang switch's channels and dispatches per-channel controls", async () => {
    const { container, sent, view } = mk();
    view.update(
      discovery({
        "2": device({
          type: "light",
          room: "Salon",
          endpoints: { "1": true, "2": false },
          endpoint_names: { "2": "Right" },
        }),
      }),
    );
    openFirstRoom(container);
    expect(container.querySelector(".room-device-stan")?.textContent).toBe("1: 🟢 On · 2: ⚪ Off");
    const buttons = [...container.querySelectorAll<HTMLButtonElement>(".room-device-actions button")];
    expect(buttons.map((button) => button.textContent)).toEqual(["#1 On", "#1 Off", "Right On", "Right Off"]);
    buttons[0]?.click();
    await flush();
    buttons[3]?.click();
    await flush();
    expect(sent).toEqual([
      { op: "switch", node: 2, endpoint: 1, on: true },
      { op: "switch", node: 2, endpoint: 2, on: false },
    ]);
  });
});

describe("createRoomsView — whole-home (Cały dom)", () => {
  it("shows a 'Cały dom' tile when a light or blind exists; tapping it opens the scene detail", () => {
    const { container, view } = mk();
    view.update(discovery({ "1": device({ type: "light", room: "Salon" }) }));
    const card = container.querySelector<HTMLButtonElement>(".whole-home-card");
    expect(card).not.toBeNull();
    expect(card?.querySelector(".whole-home-title")?.textContent).toBe("Whole home");
    expect(container.querySelector(".whole-home-card")).not.toBe(container.querySelector(".room-card")); // distinct class
    card?.click();
    expect(container.querySelector(".room-title")?.textContent).toBe("Whole home");
    expect(container.querySelector(".whole-home-detail .scene-stub")).not.toBeNull(); // renderWholeHome ran
    expect(container.querySelector(".room-card")).toBeNull();
  });

  it("hides the 'Cały dom' tile when there are no lights or blinds", () => {
    const { container, view } = mk();
    view.update(discovery({ "1": device({ type: "plug", room: "Salon" }), "2": device({ type: "door" }) }));
    expect(container.querySelector(".whole-home-card")).toBeNull();
    expect(container.querySelector(".room-card")).not.toBeNull(); // real rooms still render
  });

  it("the 🏠 Pokoje tab (goToLanding) returns from Cały dom to the grid", () => {
    const { container, view } = mk();
    view.update(discovery({ "1": device({ type: "blind", room: "Salon" }) }));
    container.querySelector<HTMLButtonElement>(".whole-home-card")?.click();
    expect(container.querySelector(".whole-home-detail")).not.toBeNull();
    view.goToLanding();
    expect(container.querySelector(".whole-home-detail")).toBeNull();
    expect(container.querySelector(".whole-home-card")).not.toBeNull(); // back on the landing
  });

  it("falls back to the landing if the home loses all scene devices while Cały dom is open", () => {
    const { container, view } = mk();
    view.update(discovery({ "1": device({ type: "light", room: "Salon" }) }));
    container.querySelector<HTMLButtonElement>(".whole-home-card")?.click();
    expect(container.querySelector(".whole-home-detail")).not.toBeNull();
    view.update(discovery({ "1": device({ type: "plug", room: "Salon" }) })); // no more light/blind
    expect(container.querySelector(".whole-home-detail")).toBeNull();
    expect(container.querySelector(".room-card-title")?.textContent).toBe("Salon");
  });

  it("fires onNav(true) entering Cały dom, onNav(false) on return", () => {
    const container = document.createElement("div");
    const nav: boolean[] = [];
    const view = createRoomsView(container, {
      postControl: () => Promise.resolve({ ok: true }),
      roomIcons: () => ({}),
      saveRoomIcon: () => Promise.resolve(),
      renderWholeHome: () => undefined,
      onNav: (inRoom) => nav.push(inRoom),
    });
    view.update(discovery({ "1": device({ type: "light", room: "Salon" }) }));
    container.querySelector<HTMLButtonElement>(".whole-home-card")?.click();
    expect(nav.at(-1)).toBe(true); // a detail view → the back tab flips to "← Pokoje"
    view.goToLanding();
    expect(nav.at(-1)).toBe(false);
  });
});

describe("createRoomsView — live updates", () => {
  it("patchState updates the visible card's state text in place", () => {
    const { container, view } = mk();
    view.update(discovery({ "9": device({ type: "light", switch: false, room: "Salon" }) }));
    openFirstRoom(container);
    expect(container.querySelector(".room-device-stan")?.textContent).toBe("⚪ Off");
    view.patchState(9, device({ type: "light", switch: true }));
    expect(container.querySelector(".room-device-stan")?.textContent).toBe("🟢 On");
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
    expect(container.querySelector(".room-device-stan")?.textContent).toBe("🟢 On");
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
    expect(container.querySelector(".room-device-stan")?.textContent).toBe("🟢 On");
  });
});

describe("createRoomsView — onNav callback", () => {
  it("fires onNav(true) entering a room, onNav(false) returning to the landing", () => {
    const container = document.createElement("div");
    const nav: boolean[] = [];
    const view = createRoomsView(container, {
      postControl: () => Promise.resolve({ ok: true }),
      roomIcons: () => ({}),
      saveRoomIcon: () => Promise.resolve(),
      renderWholeHome: () => undefined,
      onNav: (inRoom) => nav.push(inRoom),
    });
    view.update(discovery({ "5": device({ type: "light", room: "Salon" }) }));
    expect(nav).toEqual([false]); // initial landing render
    container.querySelector<HTMLButtonElement>(".room-card")?.click();
    expect(nav).toEqual([false, true]); // entered the room
    view.goToLanding();
    expect(nav).toEqual([false, true, false]); // back to the landing
  });

  it("fires onNav(false) when the open room vanishes on a refresh", () => {
    const container = document.createElement("div");
    const nav: boolean[] = [];
    const view = createRoomsView(container, {
      postControl: () => Promise.resolve({ ok: true }),
      roomIcons: () => ({}),
      saveRoomIcon: () => Promise.resolve(),
      renderWholeHome: () => undefined,
      onNav: (inRoom) => nav.push(inRoom),
    });
    view.update(discovery({ "1": device({ type: "light", room: "Salon" }) }));
    container.querySelector<HTMLButtonElement>(".room-card")?.click(); // → Salon (true)
    view.update(discovery({ "1": device({ type: "light", room: "Kuchnia" }) })); // Salon gone → landing
    expect(nav.at(-1)).toBe(false);
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
    expect(after?.querySelector(".room-device-stan")?.textContent).toBe("🟢 On");
  });
});
