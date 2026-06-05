import type { DeviceInfo, Discovery } from "./api/types";
import { type PostControl, renderActions } from "./controls";
import { currentLocale, t, tPlural } from "./i18n";
import { onOff, stateStr, typeLabel } from "./render/format";

/** Devices the rooms view controls; only postControl is needed (IR/klima have their own panels). */
export interface RoomsDeps {
  postControl: PostControl;
  roomIcons: () => Record<string, string>;
  saveRoomIcon: (room: string, icon: string) => Promise<void>;
  /** Render the whole-home scene controls into a container — the "Cały dom" virtual room's detail body. */
  renderWholeHome: (container: HTMLElement) => void;
  /** Notified when the view enters (true) / leaves (false) a detail (a room OR Cały dom) — drives the back-tab. */
  onNav?: (inRoom: boolean) => void;
  /** Whether the current user may actuate devices (operator/admin, or auth-off). A read-only viewer gets
   *  cards with name + live state but no control buttons and no whole-home scene tile. Default: yes. */
  canControl?: () => boolean;
}

/** The rooms view's public surface: a full re-render from a snapshot + a per-node live state patch. */
export interface RoomsView {
  update: (data: Discovery | null) => void;
  patchState: (node: number, info: DeviceInfo) => void;
  /** Return to the room list (the top 🏠 Pokoje tab calls this — there's no in-detail back button). */
  goToLanding: () => void;
  /** Enter per-room icon-edit mode (the entry point lives in the settings menu, not the rooms screen). */
  enterIconEdit: () => void;
}

const NO_ROOM = ""; // empty bucket key for devices with no room set (displayed via roomDisplay)
const DEFAULT_ROOM_ICON = "🚪";
const ROOM_ICON_PRESETS = [
  "🚪", "🛋️", "🛏️", "🍳", "🍽️", "🚿", "🛁", "🚽", "🧸", "🖥️", "💡", "📺", "🧺", "🌿", "🚗", "🪟",
];

/** A device's room bucket key, trimmed; blank / unset devices fall into the catch-all bucket. */
function roomKey(info: DeviceInfo): string {
  const r = info.room?.trim();
  return r !== undefined && r !== "" ? r : NO_ROOM;
}

/** Human label for a room bucket: the empty catch-all shows the localised "Other". */
function roomDisplay(room: string): string {
  return room === NO_ROOM ? t("rooms.other") : room;
}

/** Group devices by room, each room's devices sorted by numeric node id. */
function groupByRoom(devices: Record<string, DeviceInfo>): Map<string, [string, DeviceInfo][]> {
  const groups = new Map<string, [string, DeviceInfo][]>();
  for (const [node, info] of Object.entries(devices)) {
    const room = roomKey(info);
    const list = groups.get(room) ?? [];
    list.push([node, info]);
    groups.set(room, list);
  }
  for (const list of groups.values()) list.sort((a, b) => Number(a[0]) - Number(b[0]));
  return groups;
}

/** Room names sorted by the active-locale collation, with the catch-all bucket always last. */
function sortedRooms(rooms: Iterable<string>): string[] {
  return [...rooms].sort((a, b) => {
    if (a === NO_ROOM) return 1;
    if (b === NO_ROOM) return -1;
    return a.localeCompare(b, currentLocale());
  });
}

/** Live-state text for a card: a multi-gang switch lists its channels; otherwise reuse stateStr. */
function roomStateText(info: DeviceInfo): string {
  const eps = info.endpoints;
  if (eps !== null && Object.keys(eps).length > 1) {
    return Object.keys(eps)
      .sort((a, b) => Number(a) - Number(b))
      .map((ep) => `${ep}: ${onOff(eps[ep] === true)}`)
      .join(" · ");
  }
  return stateStr(info);
}

/** A device's display name: its registry label, else `type #node`. XSS-safe (textContent only). */
function deviceLabel(node: string, info: DeviceInfo): string {
  const name = info.name?.trim();
  return name !== undefined && name !== "" ? name : `${typeLabel(info.type) || "?"} #${node}`;
}

function placeholder(text: string): HTMLElement {
  const p = document.createElement("p");
  p.className = "room-placeholder";
  p.textContent = text;
  return p;
}

function roomIcon(room: string, deps: RoomsDeps): string {
  const icon = deps.roomIcons()[room];
  return icon !== undefined && icon !== "" ? icon : DEFAULT_ROOM_ICON;
}

const WHOLE_HOME_ICON = "🏠";

/** True if any device responds to a whole-home scene (all-lights / all-blinds) — gates the "Cały dom" card. */
function hasSceneDevices(devices: Record<string, DeviceInfo>): boolean {
  return Object.values(devices).some((info) => info.type === "light" || info.type === "blind");
}

/** One controllable device card: name + live state + the (reused) control buttons. Returns the state
 *  span so the live layer can patch it WITHOUT rebuilding the buttons (which would reset their lock). */
function deviceCard(
  node: string,
  info: DeviceInfo,
  deps: RoomsDeps,
): { card: HTMLElement; stan: HTMLElement } {
  const card = document.createElement("div");
  card.className = "room-device";
  card.dataset.node = node;

  const head = document.createElement("div");
  head.className = "room-device-head";
  const name = document.createElement("span");
  name.className = "room-device-name";
  name.textContent = deviceLabel(node, info);
  const stan = document.createElement("span");
  stan.className = "room-device-stan";
  stan.textContent = roomStateText(info);
  head.append(name, stan);

  // A read-only viewer gets the card's name + live state but NO control affordances; an operator/admin
  // (or auth-off) gets the table's control renderer verbatim, incl. per-channel controls for multi-gang.
  if (deps.canControl?.() ?? true) {
    const actions = document.createElement("div");
    actions.className = "room-device-actions";
    renderActions(actions, Number(node), info, deps.postControl);
    card.append(head, actions);
  } else {
    card.append(head);
  }
  return { card, stan };
}

/**
 * The room-grouped view: a landing of room cards (tap a room) → that room's device cards with big
 * controls. Rebuilt fresh from each Discovery snapshot; live state deltas patch only the visible
 * card's state text, never the buttons (so a mid-flight control press is never disturbed). `container`
 * is the rebuildable `#room-list`; the house-wide IR/klima panels live in their own persistent
 * containers, owned by main.ts.
 */
export function createRoomsView(container: HTMLElement, deps: RoomsDeps): RoomsView {
  let selectedRoom: string | null = null;
  let inWholeHome = false; // showing the "Cały dom" virtual room's scene detail
  let latest: Discovery | null = null;
  let stateSpans = new Map<number, HTMLElement>();
  let editIcons = false;
  const canControl = deps.canControl ?? ((): boolean => true); // viewer = read-only (no buttons, no scenes)

  // Finish editing → back to the plain room grid. (Entering edit mode now lives in the settings
  // menu, not on the rooms screen — so the landing isn't cluttered with a config toggle.)
  function iconEditDone(): HTMLButtonElement {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "room-icon-edit-done";
    btn.textContent = `✓ ${t("rooms.editIconsDone")}`;
    btn.addEventListener("click", () => {
      editIcons = false;
      renderLanding();
    });
    return btn;
  }

  function roomIconEditor(room: string): HTMLElement {
    const row = document.createElement("div");
    row.className = "room-icon-row";

    const select = document.createElement("select");
    select.className = "room-icon-select";
    select.setAttribute("aria-label", `${t("rooms.editIcons")} ${roomDisplay(room)}`);

    const none = document.createElement("option");
    none.value = "";
    none.textContent = t("rooms.iconNone");
    select.appendChild(none);

    const current = deps.roomIcons()[room] ?? "";
    const presets = current !== "" && !ROOM_ICON_PRESETS.includes(current)
      ? [current, ...ROOM_ICON_PRESETS]
      : ROOM_ICON_PRESETS;
    for (const icon of presets) {
      const option = document.createElement("option");
      option.value = icon;
      option.textContent = icon;
      select.appendChild(option);
    }
    select.value = current;
    select.addEventListener("change", () => {
      const icon = select.value;
      select.disabled = true;
      void (async () => {
        try {
          await deps.saveRoomIcon(room, icon);
        } catch {
          // Best-effort server save; re-render from the getter either way.
        }
        renderLanding();
      })();
    });

    const name = document.createElement("span");
    name.className = "room-icon-name";
    name.textContent = roomDisplay(room);
    row.append(select, name);
    return row;
  }

  function renderIconEditor(rooms: string[]): void {
    container.appendChild(iconEditDone());
    const list = document.createElement("div");
    list.className = "room-icon-list";
    for (const room of rooms) list.appendChild(roomIconEditor(room));
    container.appendChild(list);
  }

  // The "Cały dom" tile in the landing grid — a virtual room, NOT a `.room-card` (so the table's
  // room selectors and the tests' "first room" helper still target real rooms). Tapping it opens the
  // whole-home scene controls in a detail view, instead of a panel cluttering every rooms screen.
  function wholeHomeCard(): HTMLButtonElement {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "whole-home-card";
    const icon = document.createElement("span");
    icon.className = "whole-home-icon";
    icon.textContent = WHOLE_HOME_ICON;
    const title = document.createElement("span");
    title.className = "whole-home-title";
    title.textContent = t("scene.title");
    btn.append(icon, title);
    btn.addEventListener("click", () => {
      navigateWholeHome();
    });
    return btn;
  }

  function renderWholeHomeDetail(): void {
    stateSpans = new Map();
    container.replaceChildren();
    const title = document.createElement("h2");
    title.className = "room-title";
    title.textContent = t("scene.title");
    container.append(title); // back via the 🏠 Pokoje tab (goToLanding), like a real room detail
    const panel = document.createElement("div");
    panel.className = "whole-home-detail";
    deps.renderWholeHome(panel);
    container.append(panel);
  }

  function renderLanding(): void {
    stateSpans = new Map();
    container.replaceChildren();
    if (latest === null) {
      container.appendChild(placeholder(t("common.loading")));
      return;
    }
    const groups = groupByRoom(latest.devices);
    if (groups.size === 0) {
      container.appendChild(placeholder(t("rooms.empty")));
      return;
    }
    const rooms = sortedRooms(groups.keys());
    if (editIcons) {
      renderIconEditor(rooms);
      return;
    }
    const grid = document.createElement("div");
    grid.className = "room-grid";
    if (canControl() && hasSceneDevices(latest.devices)) grid.appendChild(wholeHomeCard()); // master tile (controllers only)
    for (const room of rooms) {
      const list = groups.get(room) ?? [];
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "room-card";
      const icon = document.createElement("span");
      icon.className = "room-card-icon";
      icon.textContent = roomIcon(room, deps);
      const title = document.createElement("span");
      title.className = "room-card-title";
      title.textContent = roomDisplay(room);
      const count = document.createElement("span");
      count.className = "room-card-count";
      count.textContent = tPlural("rooms.deviceCount", list.length);
      btn.append(icon, title, count);
      btn.addEventListener("click", () => {
        navigate(room);
      });
      grid.appendChild(btn);
    }
    container.appendChild(grid);
  }

  function renderDetail(): void {
    stateSpans = new Map();
    container.replaceChildren();
    if (latest === null) {
      container.appendChild(placeholder(t("common.loading")));
      return;
    }
    const room = selectedRoom;
    if (room === null) {
      renderLanding();
      return;
    }
    const list = groupByRoom(latest.devices).get(room) ?? []; // render() already resolved a vanished room
    const title = document.createElement("h2");
    title.className = "room-title";
    title.textContent = roomDisplay(room);
    container.append(title); // no back button — the top 🏠 Pokoje tab returns to the list (goToLanding)
    for (const [node, info] of list) {
      const { card, stan } = deviceCard(node, info, deps);
      stateSpans.set(Number(node), stan);
      container.appendChild(card);
    }
  }

  function render(): void {
    // resolve a selection that vanished from the latest snapshot before painting
    if (selectedRoom !== null && latest !== null && !groupByRoom(latest.devices).has(selectedRoom)) {
      selectedRoom = null;
    }
    if (inWholeHome && (!canControl() || (latest !== null && !hasSceneDevices(latest.devices)))) {
      inWholeHome = false; // a viewer (or a home with no scene devices) drops back to the landing
    }
    if (selectedRoom !== null) renderDetail();
    else if (inWholeHome) renderWholeHomeDetail();
    else renderLanding();
    deps.onNav?.(selectedRoom !== null || inWholeHome); // either detail flips the tab to "← Pokoje"
  }

  /** Set the selected room (or null for the landing) and re-render — the single nav choke point. */
  function navigate(room: string | null): void {
    selectedRoom = room;
    inWholeHome = false;
    render();
  }

  /** Open the "Cały dom" virtual room (the whole-home scene controls). */
  function navigateWholeHome(): void {
    if (!canControl()) return; // scenes are for controllers only — the tile isn't shown to a viewer
    selectedRoom = null;
    editIcons = false;
    inWholeHome = true;
    render();
  }

  return {
    update(data: Discovery | null): void {
      latest = data;
      // Don't rebuild the cards out from under an in-flight control press: a rebuild replaces the
      // buttons and would drop renderActions' busy lock (the table has the same guard, scoped to its
      // own rows). patchState keeps the visible state live meanwhile; the next refresh rebuilds once
      // focus leaves the control.
      const active = document.activeElement;
      if (
        (active instanceof HTMLButtonElement || active instanceof HTMLSelectElement) &&
        container.contains(active)
      ) {
        return;
      }
      render();
    },
    patchState(node: number, info: DeviceInfo): void {
      const span = stateSpans.get(node);
      if (span !== undefined) span.textContent = roomStateText(info);
    },
    enterIconEdit(): void {
      editIcons = true;
      navigate(null); // selectedRoom=null → renderLanding → editIcons → the icon editor
    },
    goToLanding(): void {
      editIcons = false; // tapping the 🏠 Pokoje tab always returns to the plain room grid
      navigate(null);
    },
  };
}
