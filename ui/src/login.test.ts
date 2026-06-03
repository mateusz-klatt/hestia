import { afterEach, describe, expect, it, vi } from "vitest";

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

describe("renderUser", () => {
  afterEach(() => {
    logoutMock.mockReset();
  });

  it("shows the logged-in user", () => {
    const box = document.createElement("div");
    renderUser(box, "tata", vi.fn());
    expect(box.textContent).toContain("signed in: tata");
  });

  it("logs out then calls onLogout when the button is clicked", async () => {
    logoutMock.mockResolvedValue(undefined);
    const box = document.createElement("div");
    const onLogout = vi.fn();
    renderUser(box, "tata", onLogout);
    box.querySelector<HTMLButtonElement>("#logout")?.click();
    await flush();
    expect(logoutMock).toHaveBeenCalledOnce();
    expect(onLogout).toHaveBeenCalledOnce();
  });
});
