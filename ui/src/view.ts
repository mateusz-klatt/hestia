import { t } from "./i18n";

export type ViewName = "rooms" | "events" | "admin";

const STORAGE_KEY = "hestia.view";

/**
 * The persisted view choice; defaults to "rooms" (the wife-friendly landing). Storage failures
 * (Safari private mode, disabled cookies) fall back to the default rather than throwing.
 */
export function storedView(): ViewName {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    return stored === "admin" || stored === "events" ? stored : "rooms";
  } catch {
    return "rooms";
  }
}

function persistView(view: ViewName): void {
  try {
    localStorage.setItem(STORAGE_KEY, view);
  } catch {
    /* storage unavailable — the choice just won't survive a reload */
  }
}

/** The DOM the switcher drives: its own button container + the three view sections it toggles. */
export interface ViewSwitchEls {
  switchBox: HTMLElement;
  roomsEl: HTMLElement;
  eventsEl: HTMLElement;
  adminEl: HTMLElement;
}

/** The switcher's handles: `apply` to switch views programmatically, `setRoomsInRoom` to flip the
 *  rooms tab between "🏠 Rooms" (landing) and "← Rooms" (inside a room → a discoverable back). */
export interface ViewSwitch {
  apply: (view: ViewName) => void;
  setRoomsInRoom: (inRoom: boolean) => void;
}

/**
 * Build the 🏠 Rooms / 📜 Activity / 🔧 Advanced segmented control into `switchBox` and wire it to show
 * exactly one view section. Applies the persisted choice immediately (calling `onChange` once). The
 * rooms tab doubles as the back affordance: inside a room it reads "← Rooms" (set via `setRoomsInRoom`),
 * so a first-time user knows tapping it returns to the room list. The container `flex-wrap`s, so the
 * third tab wraps to a second line on a narrow phone rather than overflowing.
 */
export function renderViewSwitch(els: ViewSwitchEls, onChange: (view: ViewName) => void): ViewSwitch {
  const roomsBtn = document.createElement("button");
  const eventsBtn = document.createElement("button");
  const adminBtn = document.createElement("button");
  const tabs: { name: ViewName; btn: HTMLButtonElement; section: HTMLElement }[] = [
    { name: "rooms", btn: roomsBtn, section: els.roomsEl },
    { name: "events", btn: eventsBtn, section: els.eventsEl },
    { name: "admin", btn: adminBtn, section: els.adminEl },
  ];

  const apply = (view: ViewName): void => {
    roomsBtn.textContent = t("view.rooms"); // reset; setRoomsInRoom(true) flips it to "← Rooms" in a room
    for (const tab of tabs) {
      const active = tab.name === view;
      tab.section.hidden = !active;
      tab.btn.classList.toggle("active", active);
      tab.btn.setAttribute("aria-pressed", active ? "true" : "false");
    }
    persistView(view);
    onChange(view);
  };

  const setRoomsInRoom = (inRoom: boolean): void => {
    roomsBtn.textContent = inRoom ? t("view.back") : t("view.rooms");
  };

  els.switchBox.replaceChildren();
  roomsBtn.textContent = t("view.rooms");
  eventsBtn.textContent = t("view.events");
  adminBtn.textContent = t("view.advanced");
  for (const tab of tabs) {
    tab.btn.type = "button";
    tab.btn.className = "view-tab";
    tab.btn.addEventListener("click", () => {
      apply(tab.name);
    });
    els.switchBox.appendChild(tab.btn);
  }

  apply(storedView());
  return { apply, setRoomsInRoom };
}
