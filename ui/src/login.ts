import type { UserSettings } from "./api/types";
import { login, logout } from "./api/client";
import { currentLocale, FLAGS, LOCALES, t } from "./i18n";
import { setLocaleOverride, setTempScale, tempScale, type TempScale } from "./prefs";

const SVG_NS = "http://www.w3.org/2000/svg";

/** The hestia hearth-flame mark as an SVG element (same path as the header logo); `currentColor` fill
 *  so it inherits the accent. Built via createElementNS — no innerHTML (XSS-safe even for trusted markup). */
function flameLogo(size: number): SVGSVGElement {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("fill", "currentColor");
  path.setAttribute(
    "d",
    "M13.5.67s.74 2.65.74 4.8c0 2.06-1.35 3.73-3.41 3.73-2.07 0-3.63-1.67-3.63-3.73l.03-.36C5.21 7.51 4 " +
      "10.62 4 14c0 4.42 3.58 8 8 8s8-3.58 8-8C20 8.61 17.41 3.8 13.5.67zM11.71 19c-1.78 0-3.22-1.4-3.22-3.14 " +
      "0-1.62 1.05-2.76 2.81-3.12 1.77-.36 3.6-1.21 4.62-2.58.39 1.29.59 2.65.59 4.04 0 2.65-2.15 4.8-4.8 4.8z",
  );
  svg.appendChild(path);
  return svg;
}

/**
 * Render the login form into `container`, centred on the page with the hestia hearth logo + wordmark
 * above it. On a successful login call `onSuccess` (main.ts reloads the page, so the now-authenticated
 * app boots normally). XSS-safe (DOM nodes + textContent / createElementNS, no innerHTML).
 */
export function renderLogin(container: HTMLElement, onSuccess: () => void): void {
  container.replaceChildren();

  // Brand block: the hearth flame + "hestia" wordmark, centred above the form.
  const brand = document.createElement("div");
  brand.className = "login-brand";
  const name = document.createElement("span");
  name.className = "login-brand-name";
  name.textContent = "hestia";
  brand.append(flameLogo(44), name);

  const form = document.createElement("form");
  form.id = "login-form";

  const user = document.createElement("input");
  user.id = "login-user";
  user.name = "username"; // a real `name` lets password managers offer/save the credential
  user.placeholder = t("login.username");
  user.setAttribute("aria-label", t("login.username"));
  user.autocomplete = "username";

  const pass = document.createElement("input");
  pass.id = "login-pass";
  pass.name = "password";
  pass.type = "password";
  pass.placeholder = t("login.password");
  pass.setAttribute("aria-label", t("login.password"));
  pass.autocomplete = "current-password";

  const submit = document.createElement("button");
  submit.type = "submit";
  submit.textContent = t("login.submit");

  const status = document.createElement("span");
  status.className = "status";
  status.setAttribute("aria-live", "polite"); // screen readers announce a login error

  form.append(user, pass, submit, status);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    void (async () => {
      submit.disabled = true;
      status.textContent = "…";
      status.className = "status";
      if (await login(user.value, pass.value)) {
        onSuccess();
      } else {
        status.textContent = t("login.error");
        status.className = "status err";
        submit.disabled = false;
        pass.value = "";
        pass.focus();
      }
    })();
  });

  // Centre the brand + form together in a card (the page-centring lives in #login's CSS).
  const card = document.createElement("div");
  card.className = "login-card";
  card.append(brand, form);
  container.appendChild(card);
  user.focus();
}

/** Drop a trailing region subtag (my-MM → my) so DisplayNames doesn't append "(Region)"; keep a
 *  script subtag (zh-Hant) which it uses to name the variant. */
function displayCode(code: string): string {
  return code.replace(/-[A-Za-z]{2}$|-\d{3}$/, "");
}

/** A locale's name in its own language (autonym) via Intl.DisplayNames; falls back to the code. */
function localeName(code: string): string {
  try {
    return new Intl.DisplayNames([code], { type: "language" }).of(displayCode(code)) ?? code;
  } catch {
    return code;
  }
}

/**
 * Render the user chip: just the username + a caret, opening a neat dropdown with language +
 * temperature-scale pickers and Log out. Changing a preference persists it and reloads, so the whole
 * UI re-renders in the new locale/scale. `onLogout` runs after logout (main.ts reloads → login form);
 * `reload` is injectable for tests.
 */
export function renderUser(
  container: HTMLElement,
  user: string | null,
  opts: {
    onLogout: () => void;
    reload?: () => void;
    saveSettings?: (settings: Partial<UserSettings>) => Promise<void>;
    onEditIcons?: () => void;
  },
): void {
  const reload = opts.reload ?? ((): void => {
    location.reload();
  });
  const saveSettings = opts.saveSettings ?? ((): Promise<void> => Promise.resolve());
  container.replaceChildren();

  const wrap = document.createElement("span");
  wrap.id = "user-menu-wrap";

  const btn = document.createElement("button");
  btn.id = "user-menu-btn";
  btn.type = "button";
  // Auth-on: show the username. Auth-off (no session user): a settings gear — the language/scale
  // prefs still apply, there's just no one to log out.
  btn.textContent = user !== null ? `${user} ▾` : "⚙ ▾";
  btn.setAttribute("aria-haspopup", "true");
  btn.setAttribute("aria-expanded", "false");

  const menu = document.createElement("div");
  menu.id = "user-menu";
  menu.hidden = true;

  const langRow = document.createElement("label");
  langRow.className = "menu-row";
  langRow.append(`${t("user.language")} `);
  const langSel = document.createElement("select");
  langSel.id = "locale-select";
  for (const code of LOCALES) {
    const o = document.createElement("option");
    o.value = code;
    o.textContent = `${FLAGS[code]} ${localeName(code)}`;
    if (code === currentLocale()) o.selected = true;
    langSel.appendChild(o);
  }
  langSel.addEventListener("change", () => {
    if (setLocaleOverride(langSel.value)) {
      void saveSettings({ locale: langSel.value }).finally(() => {
        reload();
      }).catch(() => undefined);
    }
  });
  langRow.appendChild(langSel);

  const scaleRow = document.createElement("label");
  scaleRow.className = "menu-row";
  scaleRow.append(`${t("user.temperature")} `);
  const scaleSel = document.createElement("select");
  scaleSel.id = "scale-select";
  for (const [value, text] of [
    ["C", "°C"],
    ["F", "°F"],
    ["K", "K"],
  ] as const) {
    const o = document.createElement("option");
    o.value = value;
    o.textContent = text;
    if (value === tempScale()) o.selected = true;
    scaleSel.appendChild(o);
  }
  scaleSel.addEventListener("change", () => {
    if (setTempScale(scaleSel.value as TempScale)) {
      void saveSettings({ temp_scale: scaleSel.value }).finally(() => {
        reload();
      }).catch(() => undefined);
    }
  });
  scaleRow.appendChild(scaleSel);

  menu.append(langRow, scaleRow);

  let open = false;
  const setOpen = (next: boolean): void => {
    open = next;
    menu.hidden = !next;
    btn.setAttribute("aria-expanded", next ? "true" : "false");
  };

  // Per-room icon editing lives here in settings, not on the rooms screen. Closes the menu, then
  // hands off to main.ts which switches to the rooms view and enters icon-edit mode.
  const onEditIcons = opts.onEditIcons;
  if (onEditIcons !== undefined) {
    const iconsBtn = document.createElement("button");
    iconsBtn.id = "edit-room-icons";
    iconsBtn.type = "button";
    iconsBtn.className = "menu-action";
    iconsBtn.textContent = `✏️ ${t("rooms.editIcons")}`;
    iconsBtn.addEventListener("click", () => {
      setOpen(false);
      onEditIcons();
    });
    menu.append(iconsBtn);
  }

  // Logout only when there's a session user (auth-off has no one to log out).
  if (user !== null) {
    const logoutBtn = document.createElement("button");
    logoutBtn.id = "logout";
    logoutBtn.type = "button";
    logoutBtn.className = "menu-logout";
    logoutBtn.textContent = t("user.logout");
    logoutBtn.addEventListener("click", () => {
      logoutBtn.disabled = true;
      void logout().then(opts.onLogout);
    });
    menu.append(logoutBtn);
  }

  btn.addEventListener("click", () => {
    setOpen(!open);
  });
  document.addEventListener("click", (event) => {
    if (!wrap.contains(event.target as Node)) setOpen(false); // close on an outside click
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setOpen(false);
  });

  wrap.append(btn, menu);
  container.appendChild(wrap);
}
