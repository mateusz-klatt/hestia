import { login, logout } from "./api/client";
import { t } from "./i18n";

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

/**
 * Render the "logged in as <user>" indicator + a logout button into `container`; on logout call
 * `onLogout` (main.ts clears the cookie via the API then reloads → the login form shows again).
 */
export function renderUser(container: HTMLElement, user: string, onLogout: () => void): void {
  container.replaceChildren();
  const label = document.createElement("span");
  label.id = "auth-user";
  label.textContent = t("user.loggedInAs", { user });

  const button = document.createElement("button");
  button.id = "logout";
  button.type = "button";
  button.textContent = t("user.logout");
  button.addEventListener("click", () => {
    button.disabled = true;
    void logout().then(onLogout);
  });

  container.append(" ", label, " ", button);
}
