// jsdom tests for the standalone bank-consent completion bridge served at /budget/connect/complete.
// The script under test lives outside the React app (hestia/budget/static/complete.js); it is loaded from
// the repository and evaluated against the same static HTML, with fetch replaced by synthetic responses.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The bridge is outside the Vite root, so read it from disk (Node's fs is untyped here: no @types/node).
const fsModule = "node:fs";
const { readFileSync } = (await import(/* @vite-ignore */ fsModule)) as {
  readFileSync: (path: string, encoding: "utf8") => string;
};
// Vitest runs from ui/, so the repository's static directory is one level up.
const html = readFileSync("../hestia/budget/static/complete.html", "utf8");
const script = readFileSync("../hestia/budget/static/complete.js", "utf8");

type Reply = { status: number; body?: unknown } | Error;
interface Call { url: string; init: RequestInit }
type Fetch = (url: string, init?: RequestInit) => Promise<Response>;

function sentBody(call: Call | undefined): unknown {
  return JSON.parse(typeof call?.init.body === "string" ? call.init.body : "null");
}

let calls: Call[];
let replies: Map<string, Reply[]>;

function flow(id: string, status = "staged", connection = "bank") {
  return { id, connection_id: connection, intent: "connect", status, error: null, created_at: 1, expires_at: 2,
    candidate_consent_id: null };
}

function reply(operation: string, ...queue: Reply[]) {
  replies.set(operation, queue);
}

async function settle(): Promise<void> {
  for (let round = 0; round < 10; round += 1) await new Promise((done) => setTimeout(done, 0));
}

async function load(): Promise<void> {
  const body = /<main>[\s\S]*<\/main>/.exec(html)?.[0] ?? "";
  document.body.innerHTML = body;
  // The bridge is a classic deferred script; evaluate it once against the prepared document.
  // eslint-disable-next-line @typescript-eslint/no-implied-eval -- evaluating the shipped classic script is the test's purpose
  const run = new Function(script) as () => void;
  run();
  await settle();
}

function message(): string {
  return document.getElementById("message")?.textContent ?? "";
}

function buttons(): HTMLButtonElement[] {
  return [...document.querySelectorAll<HTMLButtonElement>("#flows button")];
}

beforeEach(() => {
  calls = [];
  replies = new Map();
  // A callback URL with secrets must never be read: it is only present to prove that.
  window.history.replaceState(null, "", "/budget/connect/complete?code=SYNTHETIC-CODE-123&state=SYNTHETIC-STATE-456");
  vi.stubGlobal("fetch", vi.fn<Fetch>((url, init = {}) => {
    calls.push({ url, init });
    const operation = url.replace("/api/budget/consent/", "");
    const next = replies.get(operation)?.shift();
    if (next === undefined) return Promise.reject(new Error(`unexpected request ${url}`));
    if (next instanceof Error) return Promise.reject(next);
    return Promise.resolve(new Response(JSON.stringify(next.body ?? {}), { status: next.status }));
  }));
});

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

describe("static consent completion bridge", () => {
  it("never puts the callback code or state into requests, the document or browser storage", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    reply("pending", { status: 200, body: { pending: [flow("flow-1")] } });
    reply("complete", { status: 200, body: { ok: true, flow_id: "flow-1", status: "adopted" } });
    await load();
    const sent = JSON.stringify(calls.map((call) => [call.url, call.init.body ?? null]));
    expect(sent).not.toContain("SYNTHETIC-CODE");
    expect(sent).not.toContain("SYNTHETIC-STATE");
    expect(document.documentElement.innerHTML).not.toContain("SYNTHETIC");
    expect(setItem).not.toHaveBeenCalled();
    expect(html).toContain('<meta name="referrer" content="no-referrer">');
    for (const call of calls) {
      expect(call.init).toMatchObject({ credentials: "same-origin", cache: "no-store" });
      expect(Object.keys(call.init.headers ?? {})).not.toContain("Authorization");
    }
  });

  it("completes exactly one ready flow automatically with a single exchange", async () => {
    reply("pending", { status: 200, body: { pending: [flow("flow-1"), flow("flow-2", "expired"), flow("flow-3", "adopted")] } });
    reply("complete", { status: 200, body: { ok: true, flow_id: "flow-1", status: "adopted" } });
    await load();
    expect(calls.map((call) => call.url)).toEqual(["/api/budget/consent/pending", "/api/budget/consent/complete"]);
    expect(sentBody(calls[1])).toEqual({ flow_id: "flow-1" });
    expect(calls[1]?.init.method).toBe("POST");
    expect(message()).toContain("Bank został połączony");
    expect(buttons()).toHaveLength(0);
  });

  it("asks the owner to choose among several ready flows and never auto-picks", async () => {
    reply("pending", { status: 200, body: { pending: [flow("flow-1", "staged", "bank-a"), flow("flow-2", "candidate", "bank-b")] } });
    reply("complete", { status: 202, body: { ok: true, flow_id: "flow-2", status: "in_progress" } },
      { status: 200, body: { ok: true, flow_id: "flow-2", status: "adopted" } });
    await load();
    expect(calls).toHaveLength(1);
    expect(message()).toContain("Wybierz połączenie");
    expect(buttons().map((button) => button.textContent)).toEqual(["Dokończ: bank-a", "Dokończ: bank-b"]);
    buttons()[1]?.click();
    await settle();
    expect(sentBody(calls[1])).toEqual({ flow_id: "flow-2" });
    expect(message()).toContain("Hestia nie wymieni kodu drugi raz");
    expect(buttons().map((button) => button.textContent)).toEqual(["Dokończ weryfikację"]);
    buttons()[0]?.click();
    await settle();
    expect(calls).toHaveLength(3);
    expect(message()).toContain("Bank został połączony");
  });

  it("shows an uncertain exchange honestly and offers no automatic or manual re-exchange", async () => {
    reply("pending", { status: 200, body: { pending: [flow("flow-1", "exchanging")] } });
    reply("complete", { status: 202, body: { ok: true, flow_id: "flow-1", status: "exchange_uncertain" } });
    await load();
    expect(message()).toContain("Hestia nie powtórzy tej operacji");
    expect(buttons()).toHaveLength(0);
    expect(calls).toHaveLength(2);
  });

  it("ignores a double click while a completion is in flight", async () => {
    reply("pending", { status: 200, body: { pending: [flow("flow-1"), flow("flow-2")] } });
    let finish: (value: Response) => void = () => undefined;
    replies.set("complete", []);
    await load();
    vi.mocked(fetch as Fetch).mockImplementationOnce((url, init = {}) => {
      calls.push({ url, init });
      return new Promise<Response>((done) => { finish = done; });
    });
    const [first] = buttons();
    first?.click();
    first?.click();
    await settle();
    expect(calls.filter((call) => call.url.endsWith("/complete"))).toHaveLength(1);
    finish(new Response(JSON.stringify({ ok: true, flow_id: "flow-1", status: "adopted" }), { status: 200 }));
    await settle();
    expect(message()).toContain("Bank został połączony");
  });

  function respondOnce(response: () => Promise<Response>): void {
    vi.mocked(fetch as Fetch).mockImplementationOnce((url, init = {}) => {
      calls.push({ url, init });
      return response();
    });
  }

  it.each([
    ["a rejected request", () => Promise.reject(new TypeError("Failed to fetch: synthetic network detail"))],
    ["a body that is not JSON", () => Promise.resolve(new Response("<html>not json</html>", { status: 200 }))],
    ["a response without a pending list", () => Promise.resolve(new Response('{"unexpected":true}', { status: 200 }))],
  ])("shows a fixed Polish message for %s instead of raw error text", async (_name, response) => {
    respondOnce(response);
    await load();
    expect(message()).toBe("Nie udało się połączyć z Hestią. Wróć do budżetu i spróbuj ponownie.");
    expect(message()).not.toMatch(/fetch|JSON|undefined|synthetic/i);
    expect(buttons()).toHaveLength(0);
  });

  it("explains no pending flow, a lost session and other failures without exposing details", async () => {
    reply("pending", { status: 200, body: { pending: [] } });
    await load();
    expect(message()).toContain("Brak oczekującego połączenia");
    expect(buttons()).toHaveLength(0);

    reply("pending", { status: 401, body: { ok: false, error: "budget_not_signed_in" } });
    await load();
    expect(message()).toContain("Zaloguj się ponownie");

    reply("pending", { status: 200, body: { pending: [flow("flow-1")] } });
    reply("complete", { status: 409, body: { ok: false, error: "PRIVATE detail must not be shown" } });
    await load();
    expect(message()).toContain("Nie udało się zakończyć połączenia");
    expect(message()).not.toContain("PRIVATE");
  });
});
