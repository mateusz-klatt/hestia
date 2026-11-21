import { t } from "./i18n";

export type ViewName = "rooms" | "events" | "calendar" | "budget" | "admin";

const STORAGE_KEY = "hestia.view";

/**
 * The persisted view choice; defaults to "rooms" (the wife-friendly landing). Storage failures
 * (Safari private mode, disabled cookies) fall back to the default rather than throwing.
 */
export function storedView(): ViewName {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    return stored === "admin" || stored === "events" || stored === "calendar" || stored === "budget" ? stored : "rooms";
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
  calendarEl?: HTMLElement;
  /** Admin-only budget section; its tab stays hidden until `setBudgetAvailable(true)`. */
  budgetEl?: HTMLElement;
}

/** The switcher's handles: `apply` to switch views programmatically, `setRoomsInRoom` to flip the
 *  rooms tab between "🏠 Rooms" (landing) and "← Rooms" (inside a room → a discoverable back). */
export interface ViewSwitch {
  apply: (view: ViewName) => void;
  setRoomsInRoom: (inRoom: boolean) => void;
  /** Reveal or hide the budget tab once the server says whether this owner's budget is enabled. */
  setBudgetAvailable: (available: boolean) => void;
}

export interface ViewSwitchOptions {
  /** Whether to offer the 🔧 Advanced tab (admin only). When false the tab is omitted, the admin
   *  section is force-hidden, and a persisted "admin" choice is coerced back to rooms. Default true. */
  showAdmin?: boolean;
}

/**
 * Build the 🏠 Rooms / 📜 Activity / 🔧 Advanced segmented control into `switchBox` and wire it to show
 * exactly one view section. Applies the persisted choice immediately (calling `onChange` once). The
 * rooms tab doubles as the back affordance: inside a room it reads "← Rooms" (set via `setRoomsInRoom`),
 * so a first-time user knows tapping it returns to the room list. The container `flex-wrap`s, so the
 * third tab wraps to a second line on a narrow phone rather than overflowing.
 */
export function renderViewSwitch(
  els: ViewSwitchEls,
  onChange: (view: ViewName) => void,
  opts: ViewSwitchOptions = {},
): ViewSwitch {
  const showAdmin = opts.showAdmin ?? true;
  const roomsBtn = document.createElement("button");
  const eventsBtn = document.createElement("button");
  const adminBtn = document.createElement("button");
  const tabs: { name: ViewName; btn: HTMLButtonElement; section: HTMLElement }[] = [
    { name: "rooms", btn: roomsBtn, section: els.roomsEl },
    { name: "events", btn: eventsBtn, section: els.eventsEl },
  ];
  if (els.calendarEl !== undefined) {
    const calendarBtn = document.createElement("button");
    calendarBtn.textContent = document.documentElement.lang === "pl" ? "📅 Kalendarz" : "📅 Calendar";
    tabs.push({ name: "calendar", btn: calendarBtn, section: els.calendarEl });
  }
  let budgetAvailable = false;
  const budgetBtn = document.createElement("button");
  if (els.budgetEl !== undefined) {
    budgetBtn.textContent = document.documentElement.lang === "pl" ? "💰 Budżet" : "💰 Budget";
    budgetBtn.hidden = true;
    tabs.push({ name: "budget", btn: budgetBtn, section: els.budgetEl });
  }
  if (showAdmin) tabs.push({ name: "admin", btn: adminBtn, section: els.adminEl });
  // A non-admin never sees the engineer view — force it hidden so a stale stored "admin" can't reveal it.
  if (!showAdmin) els.adminEl.hidden = true;

  const apply = (view: ViewName): void => {
    // Coerce a view this user can't reach (e.g. a persisted "admin" for a non-admin) back to rooms.
    const reachable = (name: ViewName) => tabs.some((tab) => tab.name === name) && (name !== "budget" || budgetAvailable);
    const target = reachable(view) ? view : "rooms";
    roomsBtn.textContent = t("view.rooms"); // reset; setRoomsInRoom(true) flips it to "← Rooms" in a room
    for (const tab of tabs) {
      const active = tab.name === target;
      tab.section.hidden = !active;
      tab.btn.classList.toggle("active", active);
      tab.btn.setAttribute("aria-pressed", active ? "true" : "false");
    }
    // A remembered budget choice survives until the availability check answers.
    if (!(view === "budget" && target !== view)) persistView(target);
    onChange(target);
  };

  const setBudgetAvailable = (available: boolean): void => {
    if (els.budgetEl === undefined) return;
    budgetAvailable = available;
    budgetBtn.hidden = !available;
    const showing = !els.budgetEl.hidden;
    if (available && storedView() === "budget") apply("budget");
    else if (!available && showing) apply("rooms");
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
  return { apply, setRoomsInRoom, setBudgetAvailable };
}
