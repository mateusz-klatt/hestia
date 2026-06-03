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

/**
 * Build the 🏠 Pokoje / 🔧 Zaawansowane segmented control into `switchBox` and wire it to show
 * exactly one view section. Applies the persisted choice immediately (calling `onChange` once) and
 * returns an `apply` so the caller can switch views programmatically too.
 */
export function renderViewSwitch(
  els: ViewSwitchEls,
  onChange: (view: ViewName) => void,
): (view: ViewName) => void {
  const tabs: { name: ViewName; label: string; btn: HTMLButtonElement }[] = [
    { name: "rooms", label: t("view.rooms"), btn: document.createElement("button") },
    { name: "admin", label: t("view.advanced"), btn: document.createElement("button") },
  ];

  const apply = (view: ViewName): void => {
    els.roomsEl.hidden = view !== "rooms";
    els.adminEl.hidden = view !== "admin";
    for (const t of tabs) {
      const active = t.name === view;
      t.btn.classList.toggle("active", active);
      t.btn.setAttribute("aria-pressed", active ? "true" : "false");
    }
    persistView(view);
    onChange(view);
  };

  els.switchBox.replaceChildren();
  for (const t of tabs) {
    t.btn.type = "button";
    t.btn.className = "view-tab";
    t.btn.textContent = t.label;
    t.btn.addEventListener("click", () => {
      apply(t.name);
    });
    els.switchBox.appendChild(t.btn);
  }

  apply(storedView());
  return apply;
}
