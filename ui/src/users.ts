import type { MutationResult } from "./api/client";
import type { UserAccount } from "./api/types";
import { t } from "./i18n";
import type { MessageKey } from "./i18n/locales/en";
import { MIN_PASSWORD, openFormModal } from "./modal";

export type FetchUsers = () => Promise<UserAccount[] | null>;

/** Everything the admin Users panel needs — injected so it stays pure/testable (main.ts wires the
 *  real `client` calls; tests pass fakes). `currentUser` lets the panel mark/guard the admin's OWN row
 *  (the server also blocks self-demote / self-disable; this is convenience). */
export interface UsersApi {
  fetchUsers: FetchUsers;
  addUser: (username: string, password: string, role: string) => Promise<MutationResult>;
  setUserRole: (username: string, role: string) => Promise<MutationResult>;
  setUserDisabled: (username: string, disabled: boolean) => Promise<MutationResult>;
  resetUserPassword: (username: string, newPassword: string) => Promise<MutationResult>;
  currentUser: () => string | null;
}

export interface UsersPanel {
  refresh: () => Promise<void>;
}

const ROLES = ["admin", "operator", "viewer"] as const;
const ROLE_KEYS: Record<string, MessageKey> = {
  admin: "role.admin",
  operator: "role.operator",
  viewer: "role.viewer",
};

function roleLabel(role: string): string {
  const key = ROLE_KEYS[role];
  return key !== undefined ? t(key) : role;
}

function roleSelect(value: string): HTMLSelectElement {
  const sel = document.createElement("select");
  sel.className = "user-role";
  for (const role of ROLES) {
    const option = document.createElement("option");
    option.value = role;
    option.textContent = roleLabel(role);
    if (role === value) option.selected = true;
    sel.appendChild(option);
  }
  return sel;
}

function button(className: string, text: string): HTMLButtonElement {
  const b = document.createElement("button");
  b.type = "button";
  b.className = className;
  b.textContent = text;
  return b;
}

function setStatus(el: HTMLElement, text: string, isErr: boolean): void {
  el.textContent = text;
  el.className = isErr ? "status err" : "status ok";
}

function userRow(u: UserAccount, me: string | null, api: UsersApi, refresh: () => Promise<void>): HTMLElement {
  const row = document.createElement("div");
  row.className = "user-row";
  row.dataset.username = u.username;
  if (u.disabled) row.classList.add("user-disabled");
  const isSelf = u.username === me;

  const name = document.createElement("span");
  name.className = "user-name";
  name.textContent = isSelf ? `${u.username} ${t("users.you")}` : u.username;

  const status = document.createElement("span");
  status.className = "status";
  status.setAttribute("aria-live", "polite");

  const controls = document.createElement("span");
  controls.className = "user-controls";

  const sel = roleSelect(u.role);
  sel.setAttribute("aria-label", t("users.role"));
  sel.disabled = isSelf; // can't change your own role (the server enforces this too)
  sel.addEventListener("change", () => {
    void (async () => {
      sel.disabled = true;
      const result = await api.setUserRole(u.username, sel.value);
      if (result.ok) {
        await refresh();
      } else {
        sel.value = u.role; // revert the optimistic selection
        sel.disabled = false;
        setStatus(status, result.error ?? t("modal.error"), true);
      }
    })();
  });
  controls.appendChild(sel);

  // The admin manages their OWN password/role from the ⚙ menu (Change password); the panel only
  // offers enable/disable + reset on OTHER accounts (self-disable / self-reset are server-refused).
  if (!isSelf) {
    const toggle = button("user-toggle", u.disabled ? t("users.enable") : t("users.disable"));
    toggle.addEventListener("click", () => {
      void (async () => {
        toggle.disabled = true;
        const result = await api.setUserDisabled(u.username, !u.disabled);
        if (result.ok) {
          await refresh();
        } else {
          toggle.disabled = false;
          setStatus(status, result.error ?? t("modal.error"), true);
        }
      })();
    });
    controls.appendChild(toggle);

    const reset = button("user-reset", t("users.resetPassword"));
    reset.addEventListener("click", () => {
      openFormModal({
        title: `${t("users.resetPassword")} — ${u.username}`,
        fields: [
          { name: "next", label: t("user.newPassword"), autocomplete: "new-password" },
          { name: "confirm", label: t("user.confirmPassword"), autocomplete: "new-password" },
        ],
        submitLabel: t("users.resetPassword"),
        successText: t("user.passwordChanged"),
        onSubmit: (values) => {
          const next = values["next"] ?? "";
          if (next.length < MIN_PASSWORD) {
            return Promise.resolve({ ok: false, error: t("user.passwordTooShort") });
          }
          if (next !== (values["confirm"] ?? "")) {
            return Promise.resolve({ ok: false, error: t("user.passwordMismatch") });
          }
          return api.resetUserPassword(u.username, next);
        },
      });
    });
    controls.appendChild(reset);
  }

  row.append(name, controls, status);
  return row;
}

const panels = new WeakMap<HTMLElement, UsersPanel>();

/**
 * The admin user-management panel: an add-user form + a row per account (role dropdown, enable/disable,
 * reset password). Build-once (idempotent per container); `refresh()` re-fetches + rebuilds the list.
 * The server is the security boundary — this UI's self-guards are convenience.
 */
export function renderUsersPanel(container: HTMLElement, api: UsersApi): UsersPanel {
  const existing = panels.get(container);
  if (existing !== undefined) return existing;
  container.dataset.built = "1";

  const head = document.createElement("div");
  head.className = "users-head";
  const title = document.createElement("h3");
  title.textContent = t("users.title");
  const refreshBtn = button("", t("users.refresh"));
  head.append(title, refreshBtn);

  const addForm = document.createElement("form");
  addForm.className = "users-add";
  const addName = document.createElement("input");
  addName.className = "users-add-name";
  addName.placeholder = t("users.username");
  addName.setAttribute("aria-label", t("users.username"));
  addName.autocomplete = "off";
  const addPass = document.createElement("input");
  addPass.type = "password";
  addPass.className = "users-add-pass";
  addPass.placeholder = t("users.password");
  addPass.setAttribute("aria-label", t("users.password"));
  addPass.autocomplete = "new-password";
  const addRole = roleSelect("viewer");
  addRole.classList.add("users-add-role");
  const addBtn = button("users-add-btn", t("users.add"));
  addBtn.type = "submit";
  const addStatus = document.createElement("span");
  addStatus.className = "status";
  addStatus.setAttribute("aria-live", "polite");
  addForm.append(addName, addPass, addRole, addBtn, addStatus);

  const list = document.createElement("div");
  list.className = "users-list";

  const refresh = async (): Promise<void> => {
    let users: UserAccount[] | null;
    try {
      users = await api.fetchUsers();
    } catch {
      users = null;
    }
    list.replaceChildren();
    if (users === null) {
      const empty = document.createElement("p");
      empty.className = "users-empty";
      empty.textContent = t("users.empty");
      list.appendChild(empty);
      return;
    }
    const me = api.currentUser();
    for (const u of [...users].sort((a, b) => a.username.localeCompare(b.username))) {
      list.appendChild(userRow(u, me, api, refresh));
    }
  };

  addForm.addEventListener("submit", (event) => {
    event.preventDefault();
    void (async () => {
      const username = addName.value.trim();
      const password = addPass.value;
      if (username === "") {
        setStatus(addStatus, t("users.usernameRequired"), true);
        return;
      }
      if (password.length < MIN_PASSWORD) {
        setStatus(addStatus, t("user.passwordTooShort"), true);
        return;
      }
      addBtn.disabled = true;
      addStatus.textContent = "…"; // neutral pending state (not the green "ok" class)
      addStatus.className = "status";
      const result = await api.addUser(username, password, addRole.value);
      addBtn.disabled = false;
      if (result.ok) {
        setStatus(addStatus, t("users.addedOk"), false);
        addName.value = "";
        addPass.value = "";
        await refresh();
      } else {
        setStatus(addStatus, result.error ?? t("modal.error"), true);
      }
    })();
  });

  refreshBtn.addEventListener("click", () => {
    void refresh();
  });

  container.replaceChildren(head, addForm, list);
  const panel = { refresh };
  panels.set(container, panel);
  return panel;
}
