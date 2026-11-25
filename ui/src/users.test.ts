import { afterEach, describe, expect, it, vi } from "vitest";

import type { UserAccount } from "./api/types";
import type { UserGrants } from "./grant_editor";
import { nth, q } from "./test-dom";
import { renderUsersPanel, type UsersApi } from "./users";

const flush = (): Promise<void> =>
  new Promise((resolve) => {
    setTimeout(resolve, 0);
  });

afterEach(() => {
  document.body.replaceChildren();
});

const USERS: UserAccount[] = [
  { username: "admin", role: "admin", disabled: false },
  { username: "bob", role: "operator", disabled: false },
  { username: "carol", role: "viewer", disabled: true },
];

function grantsFor(username: string): UserGrants {
  const owner = username === "admin";
  return {
    phase: "catalog_compat", username, budget_owner: owner, budget_owners: ["admin"],
    catalog: [
      { permission_id: "ioc.view", domain: "ioc", storage_scope: "installation", delegation: null,
        requires: [], device_eligible: false, label: "View", protected: false },
      { permission_id: "ioc.control", domain: "ioc", storage_scope: "installation", delegation: null,
        requires: ["ioc.view"], device_eligible: false, label: "Control", protected: false },
      { permission_id: "budget.view", domain: "budget", storage_scope: "owner", delegation: "self_only",
        requires: [], device_eligible: false, label: "Budget", protected: false },
      { permission_id: "budget.receipts.add", domain: "budget", storage_scope: "owner", delegation: "delegable",
        requires: [], device_eligible: true, label: "Add", protected: false },
      { permission_id: "budget.receipts.view", domain: "budget", storage_scope: "owner", delegation: "delegable",
        requires: [], device_eligible: true, label: "View", protected: false },
      { permission_id: "budget.receipts.edit", domain: "budget", storage_scope: "owner", delegation: "delegable",
        requires: ["budget.receipts.add", "budget.receipts.view"], device_eligible: true, label: "Edit", protected: false },
      { permission_id: "system.manage_users", domain: "system", storage_scope: "installation", delegation: null,
        requires: [], device_eligible: false, label: "Manage", protected: true },
    ],
    direct: [
      { permission_id: "ioc.control", target_owner: null },
      ...(owner ? [{ permission_id: "system.manage_users", target_owner: null }] : []),
    ],
    effective: [
      { permission_id: "ioc.control", target_owner: null },
      { permission_id: "ioc.view", target_owner: null },
    ],
  };
}

function makeApi(users: UserAccount[] | null, me: string | null, overrides: Partial<UsersApi> = {}): UsersApi {
  return {
    fetchUsers: vi.fn().mockResolvedValue(users),
    addUser: vi.fn().mockResolvedValue({ ok: true, error: null }),
    setUserRole: vi.fn().mockResolvedValue({ ok: true, error: null }),
    setUserDisabled: vi.fn().mockResolvedValue({ ok: true, error: null }),
    resetUserPassword: vi.fn().mockResolvedValue({ ok: true, error: null }),
    fetchUserGrants: vi.fn().mockImplementation((username: string) => Promise.resolve(grantsFor(username))),
    saveUserGrants: vi.fn().mockResolvedValue({ ok: true, error: null }),
    currentUser: () => me,
    ...overrides,
  };
}

function rowFor(container: HTMLElement, username: string): HTMLElement {
  return q<HTMLElement>(container, `.user-row[data-username="${username}"]`);
}

function submitAdd(container: HTMLElement): void {
  q(container, ".users-add").dispatchEvent(new Event("submit"));
}

function submitModal(): void {
  q(document, "form").dispatchEvent(new Event("submit"));
}

describe("renderUsersPanel", () => {
  it("lists users username-sorted, marks the current admin's row, and guards it", async () => {
    const container = document.createElement("div");
    await renderUsersPanel(container, makeApi(USERS, "admin")).refresh();
    expect([...container.querySelectorAll<HTMLElement>(".user-row")].map((r) => r.dataset.username))
      .toEqual(["admin", "bob", "carol"]);

    const self = rowFor(container, "admin");
    expect(self.querySelector(".user-name")?.textContent).toContain("(you)");
    expect(self.querySelector(".user-role-label")?.textContent).toBe("Admin");
    expect(self.querySelector(".user-toggle")).toBeNull(); // no self-disable
    expect(self.querySelector(".user-reset")).toBeNull(); // self uses the ⚙ change-password flow
    expect(self.querySelector(".user-grants")).not.toBeNull();

    const carol = rowFor(container, "carol");
    expect(carol.classList.contains("user-disabled")).toBe(true);
    expect(carol.querySelector(".user-toggle")?.textContent).toBe("Enable"); // disabled → offer Enable
    expect(rowFor(container, "bob").querySelector(".user-toggle")?.textContent).toBe("Disable");
  });

  it("with no current user (auth-off) every row is editable", async () => {
    const container = document.createElement("div");
    await renderUsersPanel(container, makeApi(USERS, null)).refresh();
    expect(container.querySelectorAll(".user-toggle")).toHaveLength(3);
    expect(container.querySelector(".user-name")?.textContent ?? "").not.toContain("(you)");
  });

  it("validates the add-user form then adds + refreshes", async () => {
    const container = document.createElement("div");
    const api = makeApi(USERS, "admin");
    await renderUsersPanel(container, api).refresh();
    const name = q<HTMLInputElement>(container, ".users-add-name");
    const pass = q<HTMLInputElement>(container, ".users-add-pass");

    submitAdd(container);
    await flush();
    expect(container.querySelector(".users-add .status")?.textContent).toBe("Username is required");
    expect(api.addUser).not.toHaveBeenCalled();

    name.value = "dan";
    pass.value = "short";
    submitAdd(container);
    await flush();
    expect(api.addUser).not.toHaveBeenCalled(); // password too short

    pass.value = "longenough";
    submitAdd(container);
    await flush();
    expect(api.addUser).toHaveBeenCalledWith("dan", "longenough", "viewer");
    expect(api.fetchUsers).toHaveBeenCalledTimes(2); // initial + post-add refresh
  });

  it("surfaces a server error when the add fails", async () => {
    const container = document.createElement("div");
    const api = makeApi(USERS, "admin", { addUser: vi.fn().mockResolvedValue({ ok: false, error: "dup" }) });
    await renderUsersPanel(container, api).refresh();
    q<HTMLInputElement>(container, ".users-add-name").value = "x";
    q<HTMLInputElement>(container, ".users-add-pass").value = "longenough";
    submitAdd(container);
    await flush();
    expect(container.querySelector(".users-add .status")?.textContent).toBe("dup");
  });

  it("shows implied grants without persisting them when a manager edits other grants", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const api = makeApi(USERS, "admin");
    await renderUsersPanel(container, api).refresh();
    q<HTMLButtonElement>(rowFor(container, "bob"), ".user-grants").click();
    await flush();
    expect(api.fetchUserGrants).toHaveBeenCalledWith("bob");
    const editor = q<HTMLElement>(rowFor(container, "bob"), ".grant-editor");
    expect(q<HTMLInputElement>(editor, '[data-permission="ioc.view"]').checked).toBe(true);
    q<HTMLInputElement>(editor, '[data-permission="budget.receipts.edit"]').click();
    expect(q<HTMLInputElement>(editor, '[data-permission="budget.receipts.add"]').checked).toBe(true);
    expect(q<HTMLInputElement>(editor, '[data-permission="budget.receipts.view"]').checked).toBe(true);
    editor.dispatchEvent(new Event("submit"));
    await flush();
    expect(api.saveUserGrants).toHaveBeenCalledWith("bob", [
      { permission_id: "ioc.control", target_owner: null },
      { permission_id: "budget.receipts.edit", target_owner: "admin" },
    ]);
    expect(api.fetchUsers).toHaveBeenCalledTimes(2);
  });

  it("withdraws grants that imply an unchecked permission and keeps unrelated and standalone grants", async () => {
    const data = grantsFor("bob");
    data.direct.push({ permission_id: "budget.receipts.add", target_owner: "admin" });
    const api = makeApi(USERS, "admin", { fetchUserGrants: vi.fn().mockResolvedValue(data) });
    const container = document.createElement("div");
    document.body.appendChild(container);
    await renderUsersPanel(container, api).refresh();
    q<HTMLButtonElement>(rowFor(container, "bob"), ".user-grants").click();
    await flush();
    const editor = q<HTMLElement>(rowFor(container, "bob"), ".grant-editor");
    q<HTMLInputElement>(editor, '[data-permission="budget.receipts.edit"]').click();
    q<HTMLInputElement>(editor, '[data-permission="ioc.view"]').click();
    expect(q<HTMLInputElement>(editor, '[data-permission="ioc.control"]').checked).toBe(false);
    expect(q<HTMLInputElement>(editor, '[data-permission="budget.receipts.edit"]').checked).toBe(true);
    editor.dispatchEvent(new Event("submit"));
    await flush();
    expect(api.saveUserGrants).toHaveBeenCalledWith("bob", [
      { permission_id: "budget.receipts.add", target_owner: "admin" },
      { permission_id: "budget.receipts.edit", target_owner: "admin" },
    ]);
  });

  it("protects self-admin, limits presets, and surfaces grant errors", async () => {
    const container = document.createElement("div");
    const api = makeApi(USERS, "admin");
    await renderUsersPanel(container, api).refresh();
    q<HTMLButtonElement>(rowFor(container, "admin"), ".user-grants").click();
    await flush();
    const selfEditor = q<HTMLElement>(rowFor(container, "admin"), ".grant-editor");
    expect(q<HTMLInputElement>(selfEditor, '[data-permission="system.manage_users"]').disabled).toBe(true);
    expect(q<HTMLInputElement>(selfEditor, '[data-permission="system.manage_users"]').checked).toBe(true);
    q<HTMLButtonElement>(selfEditor, ".grant-full").click();
    expect(q<HTMLInputElement>(selfEditor, '[data-permission="budget.view"]').checked).toBe(true);
    expect(q<HTMLInputElement>(selfEditor, '[data-permission="budget.receipts.view"]').checked).toBe(true);
    selfEditor.dispatchEvent(new Event("submit"));
    await flush();
    expect(api.saveUserGrants).toHaveBeenCalledWith("admin", [
      { permission_id: "ioc.control", target_owner: null },
      { permission_id: "budget.view", target_owner: "admin" },
      { permission_id: "budget.receipts.edit", target_owner: "admin" },
      { permission_id: "system.manage_users", target_owner: null },
    ]);

    const failing = makeApi(USERS, "admin", {
      fetchUserGrants: vi.fn().mockResolvedValue(null),
    });
    const c2 = document.createElement("div");
    await renderUsersPanel(c2, failing).refresh();
    q<HTMLButtonElement>(rowFor(c2, "bob"), ".user-grants").click();
    await flush();
    expect(rowFor(c2, "bob").querySelector(".user-grant-editor")?.textContent).toBe("Something went wrong");

    const saveFail = makeApi(USERS, "admin", {
      saveUserGrants: vi.fn().mockResolvedValue({ ok: false, error: "denied" }),
    });
    const c3 = document.createElement("div");
    await renderUsersPanel(c3, saveFail).refresh();
    q<HTMLButtonElement>(rowFor(c3, "bob"), ".user-grants").click();
    await flush();
    const editor = q<HTMLElement>(rowFor(c3, "bob"), ".grant-editor");
    editor.dispatchEvent(new Event("submit"));
    await flush();
    expect(editor.querySelector(".status")?.textContent).toBe("denied");
    expect(q<HTMLButtonElement>(editor, ".grant-save").disabled).toBe(false);
  });

  it("disables delegate self-only grants, excludes protected presets, and changes owner targets", async () => {
    const data = grantsFor("bob");
    data.budget_owners = ["admin", "other"];
    const api = makeApi(USERS, "admin", { fetchUserGrants: vi.fn().mockResolvedValue(data) });
    const container = document.createElement("div");
    await renderUsersPanel(container, api).refresh();
    const row = rowFor(container, "bob");
    q<HTMLButtonElement>(row, ".user-grants").click();
    await flush();
    const editor = q<HTMLElement>(row, ".grant-editor");
    expect(q<HTMLInputElement>(editor, '[data-permission="budget.view"]').disabled).toBe(true);
    q<HTMLButtonElement>(editor, ".grant-full").click();
    expect(q<HTMLInputElement>(editor, '[data-permission="system.manage_users"]').checked).toBe(false);
    expect(q<HTMLInputElement>(editor, '[data-permission="budget.view"]').checked).toBe(false);
    expect(q<HTMLInputElement>(editor, '[data-permission="ioc.control"]').checked).toBe(true);
    const target = q<HTMLSelectElement>(editor, ".grant-target select");
    target.value = "other";
    target.dispatchEvent(new Event("change"));
    expect(q<HTMLInputElement>(editor, '[data-permission="budget.receipts.add"]').checked).toBe(false);
    const add = q<HTMLInputElement>(editor, '[data-permission="budget.receipts.add"]');
    add.checked = true;
    add.dispatchEvent(new Event("change"));
    editor.dispatchEvent(new Event("submit"));
    await flush();
    expect(api.saveUserGrants).toHaveBeenCalledWith("bob", [
      { permission_id: "ioc.control", target_owner: null },
      { permission_id: "budget.receipts.add", target_owner: "other" },
    ]);
  });

  it("toggles enable/disable", async () => {
    const container = document.createElement("div");
    const api = makeApi(USERS, "admin");
    await renderUsersPanel(container, api).refresh();
    q<HTMLButtonElement>(rowFor(container, "bob"), ".user-toggle").click();
    await flush();
    expect(api.setUserDisabled).toHaveBeenCalledWith("bob", true);

    const fail = makeApi(USERS, "admin", { setUserDisabled: vi.fn().mockResolvedValue({ ok: false, error: "x" }) });
    const c2 = document.createElement("div");
    await renderUsersPanel(c2, fail).refresh();
    q<HTMLButtonElement>(rowFor(c2, "carol"), ".user-toggle").click();
    await flush();
    expect(fail.setUserDisabled).toHaveBeenCalledWith("carol", false); // disabled → enable
    expect(rowFor(c2, "carol").querySelector(".status")?.textContent).toBe("x");
  });

  it("opens a reset-password modal, validates it, then calls resetUserPassword", async () => {
    const container = document.createElement("div");
    const api = makeApi(USERS, "admin");
    await renderUsersPanel(container, api).refresh();
    q<HTMLButtonElement>(rowFor(container, "bob"), ".user-reset").click();
    expect(document.querySelector(".modal-card h3")?.textContent).toContain("bob");

    nth<HTMLInputElement>(document, ".modal-form input", 0).value = "longenough";
    nth<HTMLInputElement>(document, ".modal-form input", 1).value = "different";
    submitModal();
    await flush();
    expect(api.resetUserPassword).not.toHaveBeenCalled();
    expect(document.querySelector(".modal-form .status")?.textContent).toBe("Passwords don't match");

    nth<HTMLInputElement>(document, ".modal-form input", 1).value = "longenough";
    submitModal();
    await flush();
    expect(api.resetUserPassword).toHaveBeenCalledWith("bob", "longenough");
  });

  it("rejects a too-short password in the reset modal", async () => {
    const container = document.createElement("div");
    const api = makeApi(USERS, "admin");
    await renderUsersPanel(container, api).refresh();
    q<HTMLButtonElement>(rowFor(container, "bob"), ".user-reset").click();
    nth<HTMLInputElement>(document, ".modal-form input", 0).value = "short";
    nth<HTMLInputElement>(document, ".modal-form input", 1).value = "short";
    submitModal();
    await flush();
    expect(api.resetUserPassword).not.toHaveBeenCalled();
    expect(document.querySelector(".modal-form .status")?.textContent).toBe("Password must be at least 8 characters");
  });

  it("shows an empty message when users can't be loaded (null or throw)", async () => {
    const c1 = document.createElement("div");
    await renderUsersPanel(c1, makeApi(null, "admin")).refresh();
    expect(c1.querySelector(".users-empty")?.textContent).toBe("Could not load users");

    const c2 = document.createElement("div");
    const throwing = makeApi(USERS, "admin", { fetchUsers: vi.fn().mockRejectedValue(new Error("x")) });
    await renderUsersPanel(c2, throwing).refresh();
    expect(c2.querySelector(".users-empty")).not.toBeNull();
  });

  it("refresh button re-fetches; builds once per container", async () => {
    const container = document.createElement("div");
    const api = makeApi(USERS, "admin");
    const first = renderUsersPanel(container, api);
    const second = renderUsersPanel(container, makeApi(USERS, "admin"));
    expect(second).toBe(first); // build-once
    expect(container.dataset.built).toBe("1");
    q<HTMLButtonElement>(container, ".users-head button").click();
    await flush();
    expect(api.fetchUsers).toHaveBeenCalled();
  });
});
