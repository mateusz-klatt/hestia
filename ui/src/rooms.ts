import type { DeviceInfo, Discovery } from "./api/types";
import { type PostControl, renderActions } from "./controls";
import { stateStr } from "./render/format";

/** Devices the rooms view controls; only postControl is needed (IR/klima have their own panels). */
export interface RoomsDeps {
  postControl: PostControl;
}

/** The rooms view's public surface: a full re-render from a snapshot + a per-node live state patch. */
export interface RoomsView {
  update: (data: Discovery | null) => void;
  patchState: (node: number, info: DeviceInfo) => void;
}

const NO_ROOM = "Inne";

/** A device's room label, trimmed; blank / unset devices fall into "Inne". */
function roomOf(info: DeviceInfo): string {
  const r = info.room?.trim();
  return r !== undefined && r !== "" ? r : NO_ROOM;
}

/** Group devices by room, each room's devices sorted by numeric node id. */
function groupByRoom(devices: Record<string, DeviceInfo>): Map<string, [string, DeviceInfo][]> {
  const groups = new Map<string, [string, DeviceInfo][]>();
  for (const [node, info] of Object.entries(devices)) {
    const room = roomOf(info);
    const list = groups.get(room) ?? [];
    list.push([node, info]);
    groups.set(room, list);
  }
  for (const list of groups.values()) list.sort((a, b) => Number(a[0]) - Number(b[0]));
  return groups;
}

/** Room names sorted alphabetically (pl collation), with the catch-all "Inne" always last. */
function sortedRooms(rooms: Iterable<string>): string[] {
  return [...rooms].sort((a, b) => {
    if (a === NO_ROOM) return 1;
    if (b === NO_ROOM) return -1;
    return a.localeCompare(b, "pl");
  });
}

/** Polish plural of "urządzenie" for a device count (1 → urządzenie, 2–4 → urządzenia, else urządzeń). */
function deviceWord(n: number): string {
  if (n === 1) return "urządzenie";
  const d = n % 10;
  const dd = n % 100;
  return d >= 2 && d <= 4 && (dd < 12 || dd > 14) ? "urządzenia" : "urządzeń";
}

/** Live-state text for a card: a multi-gang switch lists its channels; otherwise reuse stateStr. */
function roomStateText(info: DeviceInfo): string {
  const eps = info.endpoints;
  if (eps !== null && Object.keys(eps).length > 1) {
    return Object.keys(eps)
      .sort((a, b) => Number(a) - Number(b))
      .map((ep) => `${ep}: ${eps[ep] === true ? "wł" : "wył"}`)
      .join(" · ");
  }
  return stateStr(info);
}

/** A device's display name: its registry label, else `type #node`. XSS-safe (textContent only). */
function deviceLabel(node: string, info: DeviceInfo): string {
  const name = info.name?.trim();
  return name !== undefined && name !== "" ? name : `${info.type || "?"} #${node}`;
}

function placeholder(text: string): HTMLElement {
  const p = document.createElement("p");
  p.className = "room-placeholder";
  p.textContent = text;
  return p;
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

  const actions = document.createElement("div");
  actions.className = "room-device-actions";
  // Reuse the table's control renderer verbatim. Multi-gang returns no buttons (read-only until #48).
  renderActions(actions, Number(node), info, deps.postControl);

  card.append(head, actions);
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
  let latest: Discovery | null = null;
  let stateSpans = new Map<number, HTMLElement>();

  function renderLanding(): void {
    stateSpans = new Map();
    container.replaceChildren();
    if (latest === null) {
      container.appendChild(placeholder("ładowanie…"));
      return;
    }
    const groups = groupByRoom(latest.devices);
    if (groups.size === 0) {
      container.appendChild(placeholder("Brak urządzeń"));
      return;
    }
    const grid = document.createElement("div");
    grid.className = "room-grid";
    for (const room of sortedRooms(groups.keys())) {
      const list = groups.get(room) ?? [];
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "room-card";
      const title = document.createElement("span");
      title.className = "room-card-title";
      title.textContent = room;
      const count = document.createElement("span");
      count.className = "room-card-count";
      count.textContent = `${String(list.length)} ${deviceWord(list.length)}`;
      btn.append(title, count);
      btn.addEventListener("click", () => {
        selectedRoom = room;
        renderDetail();
      });
      grid.appendChild(btn);
    }
    container.appendChild(grid);
  }

  function renderDetail(): void {
    stateSpans = new Map();
    container.replaceChildren();
    if (latest === null) {
      container.appendChild(placeholder("ładowanie…"));
      return;
    }
    const room = selectedRoom;
    const list = room !== null ? groupByRoom(latest.devices).get(room) : undefined;
    if (room === null || list === undefined) {
      selectedRoom = null; // the selected room lost all its devices → back to the landing
      renderLanding();
      return;
    }
    const back = document.createElement("button");
    back.type = "button";
    back.className = "room-back";
    back.textContent = "← Pokoje";
    back.addEventListener("click", () => {
      selectedRoom = null;
      renderLanding();
    });
    const title = document.createElement("h2");
    title.className = "room-title";
    title.textContent = room;
    container.append(back, title);
    for (const [node, info] of list) {
      const { card, stan } = deviceCard(node, info, deps);
      stateSpans.set(Number(node), stan);
      container.appendChild(card);
    }
  }

  function render(): void {
    if (selectedRoom === null) renderLanding();
    else renderDetail();
  }

  return {
    update(data: Discovery | null): void {
      latest = data;
      render();
    },
    patchState(node: number, info: DeviceInfo): void {
      const span = stateSpans.get(node);
      if (span !== undefined) span.textContent = roomStateText(info);
    },
  };
}
