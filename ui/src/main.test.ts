import { afterEach, expect, it, vi } from "vitest";

const fsModule = "node:fs";
const { readFileSync } = (await import(/* @vite-ignore */ fsModule)) as {
  readFileSync: (path: string, encoding: "utf8") => string;
};
const html = readFileSync("index.html", "utf8");

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

it.each([
  { role: "viewer", permissions: ["calendar.view", "calendar.edit"], editable: true },
  { role: "operator", permissions: ["calendar.view", "ioc.control"], editable: false },
  { role: "operator", permissions: null, editable: true },
])("opens Calendar for $role with $permissions (editable: $editable)", async ({ role, permissions, editable }) => {
  vi.resetModules();
  document.body.innerHTML = new DOMParser().parseFromString(html, "text/html").body.innerHTML;
  const storage = new Map([["hestia.locale", "en"], ["hestia.view", "calendar"]]);
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => { storage.set(key, value); },
  });
  vi.spyOn(window, "setInterval").mockReturnValue(0);
  vi.stubGlobal("EventSource", EventTarget);
  vi.stubGlobal("fetch", (url: URL) => {
    if (url.pathname.endsWith("/whoami")) return Promise.resolve(Response.json({ user: "alex", role }));
    if (url.pathname.endsWith("/me/capabilities") && permissions !== null) {
      return Promise.resolve(Response.json({ user: "alex", actor_tag: "a".repeat(43), installation_permissions: permissions,
        own_budget: null, delegated_receipts: null, household_available: false }));
    }
    if (url.pathname.endsWith("/calendars")) {
      return Promise.resolve(Response.json({ enabled: true, groups: [{ id: "family" }], events: [], status: { endpoints: [], pending: [] } }));
    }
    return Promise.resolve(new Response(null, { status: 404 }));
  });

  await import("./main");

  await vi.waitFor(() => {
    expect(document.querySelector("#calendar-root .calendar-group select")?.textContent).toBe("family");
    const add = document.querySelector<HTMLButtonElement>("#calendar-root .calendar-header button");
    if (editable) expect(add?.textContent).toBe("+ Add event");
    else expect(add).toBeNull();
  });
});
