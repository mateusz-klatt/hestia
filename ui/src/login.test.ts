import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Mock the API client so the form/chrome are tested without real fetches (hoisted: usable in vi.mock).
const { loginMock, logoutMock } = vi.hoisted(() => ({ loginMock: vi.fn(), logoutMock: vi.fn() }));
vi.mock("./api/client", () => ({ login: loginMock, logout: logoutMock }));

import { renderLogin, renderUser } from "./login";

const flush = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));

function input(box: HTMLElement, id: string): HTMLInputElement {
  const el = box.querySelector<HTMLInputElement>(`#${id}`);
  if (el === null) throw new Error(`missing #${id}`);
  return el;
}

function submit(box: HTMLElement): void {
  const form = box.querySelector("form");
  if (form === null) throw new Error("no form");
  form.dispatchEvent(new Event("submit"));
}

describe("renderLogin", () => {
  afterEach(() => {
    loginMock.mockReset();
    logoutMock.mockReset();
  });

  it("builds a user + password form", () => {
    const box = document.createElement("div");
    renderLogin(box, vi.fn());
    expect(input(box, "login-user")).toBeTruthy();
    expect(input(box, "login-pass").type).toBe("password");
  });

  it("logs in with the entered credentials and calls onSuccess", async () => {
    loginMock.mockResolvedValue(true);
    const box = document.createElement("div");
    const onSuccess = vi.fn();
    renderLogin(box, onSuccess);
    input(box, "login-user").value = "tata";
    input(box, "login-pass").value = "s3cret";
    submit(box);
    await flush();
    expect(loginMock).toHaveBeenCalledWith("tata", "s3cret");
    expect(onSuccess).toHaveBeenCalledOnce();
  });

  it("shows an error and does NOT call onSuccess on failure", async () => {
    loginMock.mockResolvedValue(false);
    const box = document.createElement("div");
    const onSuccess = vi.fn();
    renderLogin(box, onSuccess);
    submit(box);
    await flush();
    expect(onSuccess).not.toHaveBeenCalled();
    expect(box.querySelector(".status.err")?.textContent).toContain("Wrong");
    expect(input(box, "login-pass").value).toBe(""); // cleared for a retry
  });
});

// jsdom here has no working localStorage, so stub a Map-backed one for the prefs (locale / scale).
function fakeStorage(): Storage {
  const m = new Map<string, string>();
  return {
    get length() {
      return m.size;
    },
    clear: () => {
      m.clear();
    },
    getItem: (k: string) => m.get(k) ?? null,
    key: (i: number) => [...m.keys()][i] ?? null,
    removeItem: (k: string) => m.delete(k),
    setItem: (k: string, v: string) => m.set(k, v),
  };
}

describe("renderUser", () => {
  beforeEach(() => {
    vi.stubGlobal("localStorage", fakeStorage());
  });
  afterEach(() => {
    logoutMock.mockReset();
    vi.unstubAllGlobals();
  });

  it("shows just the username with a dropdown (no 'signed in' label)", () => {
    const box = document.createElement("div");
    renderUser(box, "tata", { onLogout: vi.fn() });
    expect(box.querySelector("#user-menu-btn")?.textContent).toContain("tata");
    expect(box.textContent).not.toContain("signed in");
    expect(box.querySelector("#locale-select")).not.toBeNull();
    expect(box.querySelector("#scale-select")).not.toBeNull();
  });

  it("toggles the dropdown on the user button", () => {
    const box = document.createElement("div");
    renderUser(box, "tata", { onLogout: vi.fn() });
    const menu = box.querySelector<HTMLElement>("#user-menu");
    expect(menu?.hidden).toBe(true);
    box.querySelector<HTMLButtonElement>("#user-menu-btn")?.click();
    expect(menu?.hidden).toBe(false);
  });

  it("changing the language persists the override and reloads", () => {
    const reload = vi.fn();
    const box = document.createElement("div");
    renderUser(box, "tata", { onLogout: vi.fn(), reload });
    const sel = box.querySelector<HTMLSelectElement>("#locale-select");
    if (sel !== null) {
      sel.value = "pl";
      sel.dispatchEvent(new Event("change"));
    }
    expect(localStorage.getItem("hestia.locale")).toBe("pl");
    expect(reload).toHaveBeenCalledOnce();
  });

  it("changing the temperature scale persists it and reloads", () => {
    const reload = vi.fn();
    const box = document.createElement("div");
    renderUser(box, "tata", { onLogout: vi.fn(), reload });
    const sel = box.querySelector<HTMLSelectElement>("#scale-select");
    if (sel !== null) {
      sel.value = "F";
      sel.dispatchEvent(new Event("change"));
    }
    expect(localStorage.getItem("hestia.tempScale")).toBe("F");
    expect(reload).toHaveBeenCalledOnce();
  });

  it("logs out then calls onLogout when the logout item is clicked", async () => {
    logoutMock.mockResolvedValue(undefined);
    const box = document.createElement("div");
    const onLogout = vi.fn();
    renderUser(box, "tata", { onLogout, reload: vi.fn() });
    box.querySelector<HTMLButtonElement>("#logout")?.click();
    await flush();
    expect(logoutMock).toHaveBeenCalledOnce();
    expect(onLogout).toHaveBeenCalledOnce();
  });
});
