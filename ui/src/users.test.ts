import { afterEach, describe, expect, it, vi } from "vitest";

import type { UserAccount } from "./api/types";
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

function makeApi(users: UserAccount[] | null, me: string | null, overrides: Partial<UsersApi> = {}): UsersApi {
  return {
    fetchUsers: vi.fn().mockResolvedValue(users),
    addUser: vi.fn().mockResolvedValue({ ok: true, error: null }),
    setUserRole: vi.fn().mockResolvedValue({ ok: true, error: null }),
    setUserDisabled: vi.fn().mockResolvedValue({ ok: true, error: null }),
    resetUserPassword: vi.fn().mockResolvedValue({ ok: true, error: null }),
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
    expect(q<HTMLSelectElement>(self, ".user-role").disabled).toBe(true);
    expect(self.querySelector(".user-toggle")).toBeNull(); // no self-disable
    expect(self.querySelector(".user-reset")).toBeNull(); // self uses the ⚙ change-password flow
    expect(q<HTMLSelectElement>(self, ".user-role").value).toBe("admin");

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

  it("changes a role and refreshes; reverts on failure", async () => {
    const container = document.createElement("div");
    const ok = makeApi(USERS, "admin");
    await renderUsersPanel(container, ok).refresh();
    const sel = q<HTMLSelectElement>(rowFor(container, "bob"), ".user-role");
    sel.value = "admin";
    sel.dispatchEvent(new Event("change"));
    await flush();
    expect(ok.setUserRole).toHaveBeenCalledWith("bob", "admin");

    const fail = makeApi(USERS, "admin", { setUserRole: vi.fn().mockResolvedValue({ ok: false, error: "no" }) });
    const c2 = document.createElement("div");
    await renderUsersPanel(c2, fail).refresh();
    const sel2 = q<HTMLSelectElement>(rowFor(c2, "bob"), ".user-role");
    sel2.value = "admin";
    sel2.dispatchEvent(new Event("change"));
    await flush();
    expect(sel2.value).toBe("operator"); // reverted
    expect(rowFor(c2, "bob").querySelector(".status")?.textContent).toBe("no");
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
