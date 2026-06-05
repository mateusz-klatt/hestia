// DEV-ONLY API mock so `npm run dev` renders the whole app without a backend ("frontend-only", per the
// responsive-layout check). Activated by `?mock` on the dev server. This module is imported ONLY from
// the `import.meta.env.DEV` branch in main.ts, so a production `vite build` (DEV === false) dead-eliminates
// that branch and never bundles this file — it can never affect the shipped app.
import type { Discovery } from "../api/types";
import { device, discovery } from "../fixtures";

// Deliberately long, multi-word room + device names (the registry, not i18n) to stress the room cards and
// device rows the way real Polish names do; the locale switch stresses the UI chrome (tabs, mode names…).
const RICH: Discovery = discovery(
  {
    "5": device({ type: "light", confidence: "confirmed", level: 60, room: "Salon z aneksem kuchennym", name: "Lampa sufitowa nad stołem" }),
    "6": device({ type: "light", confidence: "confirmed", switch: true, room: "Salon z aneksem kuchennym", name: "Kinkiet przy kanapie" }),
    "7": device({ type: "plug", confidence: "confirmed", switch: false, room: "Gabinet", name: "Ładowarka do laptopa" }),
    "8": device({ type: "blind", confidence: "confirmed", level: 40, room: "Sypialnia rodziców", name: "Roleta okno południowe" }),
    "9": device({
      type: "thermostat", confidence: "confirmed", setpoint: 21, thermostat_on: true, temperature: 20.5,
      room: "Pokój dziecięcy na poddaszu", name: "Termostat przy grzejniku",
    }),
    "10": device({ type: "motion", confidence: "inferred", motion: false, room: "Przedpokój", name: "Czujnik ruchu" }),
    "11": device({ type: "door", confidence: "confirmed", door: "closed", room: "Wejście główne", name: "Kontaktron drzwi" }),
    "12": device({
      type: "light", confidence: "confirmed", endpoints: { "1": true, "2": false }, endpoint_names: { "1": "Lewy", "2": "Prawy" },
      room: "Łazienka", name: "Oświetlenie dwuobwodowe",
    }),
  },
  {
    globals: { crib_temp: 22.6, outdoor_temp: 27.2, outdoor_humidity: 42 },
    klima: {
      file: "/ext/infrared/klima.ir",
      power_on: { cool: [18, 20, 22, 24], heat: [20, 22, 24], auto: [22], dry: [20], fan: [22] },
      presets: ["off"],
    },
    klima_state: { power: true, mode: "cool", temp: 21 },
    ir_buttons: [{ label: "TV ⏻", file: "/ext/infrared/tv.ir", button: "Power" }],
  },
);

// Hand-picked rows that exercise every display-time humanizer (actor/action/result localization +
// detail value substitution): a physical 2-gang transition, a per-channel control, a door close, a
// setpoint, a scene, a dimmer level, a login, a plug toggle.
const AUDIT = {
  events: [
    { id: 108, ts: 1717590000, actor: "device", action: "endpoints", target: "12",
      detail: "{'1': True, '2': False}", result: "reported" },
    { id: 107, ts: 1717589400, actor: "mateusz", action: "switch", target: "12",
      detail: '{"endpoint": 1, "node": 12, "on": true}', result: "ok" },
    { id: 106, ts: 1717588800, actor: "device", action: "door", target: "11",
      detail: "closed", result: "reported" },
    { id: 105, ts: 1717588200, actor: "mateusz", action: "thermostat", target: "9",
      detail: '{"node": 9, "celsius": 22}', result: "ok" },
    { id: 104, ts: 1717587600, actor: "automation:wieczorny-scenariusz", action: "scene",
      target: "wieczorny-scenariusz", detail: null, result: "fired" },
    { id: 103, ts: 1717587000, actor: "mateusz", action: "level", target: "8",
      detail: '{"node": 8, "value": 40}', result: "ok" },
    { id: 102, ts: 1717586400, actor: "mateusz", action: "login", target: "dashboard",
      detail: null, result: "ok" },
    { id: 101, ts: 1717585800, actor: "device", action: "switch", target: "7",
      detail: "False", result: "reported" },
  ],
};

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

const ROUTES: Record<string, () => Response> = {
  whoami: () => jsonResponse({ user: null, role: null }), // auth-off → boots straight through, full access
  discovery: () => jsonResponse(RICH),
  settings: () => jsonResponse({ locale: null, temp_scale: null, theme: null }),
  "rooms/icons": () => jsonResponse({ "Salon z aneksem kuchennym": "🛋️", "Łazienka": "🛁" }),
  audit: () => jsonResponse(AUDIT),
  automations: () => jsonResponse({ automations: [] }),
  rf433: () => jsonResponse({ devices: [] }),
  "db/stats": () => jsonResponse({ file_bytes: 196608, tables: { nodes: 8, automations: 0, users: 3, audit: 8 } }),
  users: () => jsonResponse({
    users: [
      { username: "mateusz", role: "admin", disabled: false },
      { username: "anna", role: "operator", disabled: false },
      { username: "gosc", role: "viewer", disabled: true },
    ],
  }),
};

/** Patch fetch + EventSource so every `/api/*` call resolves from the fixtures above. */
export function installMock(): void {
  const realFetch = window.fetch.bind(window);
  window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const match = /\/api\/(.+?)\/?(?:\?|$)/.exec(url);
    const key = match?.[1];
    const handler = key !== undefined ? ROUTES[key] : undefined;
    if (handler !== undefined) return Promise.resolve(handler());
    if (key !== undefined && (init?.method ?? "GET") !== "GET") return Promise.resolve(jsonResponse({ ok: true }));
    return realFetch(input, init);
  };
  // Stub EventSource so the live snapshot loads on "open" without a noisy reconnect to a dead /api/events.
  class MockEventSource extends EventTarget {
    constructor() {
      super();
      setTimeout(() => this.dispatchEvent(new Event("open")), 0);
    }
    close(): void {
      /* no-op */
    }
  }
  (window as unknown as { EventSource: typeof EventSource }).EventSource =
    MockEventSource as unknown as typeof EventSource;
}
