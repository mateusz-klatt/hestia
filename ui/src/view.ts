import { t } from "./i18n";

export type ViewName = "rooms" | "admin";

const STORAGE_KEY = "hestia.view";

/**
 * The persisted view choice; defaults to "rooms" (the wife-friendly landing). Storage failures
 * (Safari private mode, disabled cookies) fall back to the default rather than throwing.
 */
export function storedView(): ViewName {
  try {
    return localStorage.getItem(STORAGE_KEY) === "admin" ? "admin" : "rooms";
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

/** The DOM the switcher drives: its own button container + the two view sections it toggles. */
export interface ViewSwitchEls {
  switchBox: HTMLElement;
  roomsEl: HTMLElement;
  adminEl: HTMLElement;
}

/** The switcher's handles: `apply` to switch views programmatically, `setRoomsInRoom` to flip the
 *  rooms tab between "🏠 Rooms" (landing) and "← Rooms" (inside a room → a discoverable back). */
export interface ViewSwitch {
  apply: (view: ViewName) => void;
  setRoomsInRoom: (inRoom: boolean) => void;
}

/**
 * Build the 🏠 Rooms / 🔧 Advanced segmented control into `switchBox` and wire it to show exactly one
 * view section. Applies the persisted choice immediately (calling `onChange` once). The rooms tab
 * doubles as the back affordance: inside a room it reads "← Rooms" (set via `setRoomsInRoom`), so a
 * first-time user knows tapping it returns to the room list.
 */
export function renderViewSwitch(els: ViewSwitchEls, onChange: (view: ViewName) => void): ViewSwitch {
  const roomsBtn = document.createElement("button");
  const adminBtn = document.createElement("button");
  const tabs: { name: ViewName; btn: HTMLButtonElement }[] = [
    { name: "rooms", btn: roomsBtn },
    { name: "admin", btn: adminBtn },
  ];

  const apply = (view: ViewName): void => {
    els.roomsEl.hidden = view !== "rooms";
    els.adminEl.hidden = view !== "admin";
    roomsBtn.textContent = t("view.rooms"); // reset; setRoomsInRoom(true) flips it to "← Rooms" in a room
    for (const tab of tabs) {
      const active = tab.name === view;
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
  adminBtn.textContent = t("view.advanced");
  roomsBtn.textContent = t("view.rooms");
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
