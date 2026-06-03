import { login, logout } from "./api/client";
import { currentLocale, FLAGS, LOCALES, t } from "./i18n";
import { setLocaleOverride, setTempScale, tempScale, type TempScale } from "./prefs";

/**
 * Render the login form into `container`; on a successful login call `onSuccess` (main.ts reloads the
 * page, so the now-authenticated app boots normally). XSS-safe (DOM nodes + textContent, no innerHTML).
 */
export function renderLogin(container: HTMLElement, onSuccess: () => void): void {
  container.replaceChildren();
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

  container.appendChild(form);
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
  opts: { onLogout: () => void; reload?: () => void },
): void {
  const reload = opts.reload ?? ((): void => {
    location.reload();
  });
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
    if (setLocaleOverride(langSel.value)) reload(); // only reload if the choice actually persisted
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
    if (setTempScale(scaleSel.value as TempScale)) reload();
  });
  scaleRow.appendChild(scaleSel);

  menu.append(langRow, scaleRow);

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

  let open = false;
  const setOpen = (next: boolean): void => {
    open = next;
    menu.hidden = !next;
    btn.setAttribute("aria-expanded", next ? "true" : "false");
  };
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
