import { describe, expect, it } from "vitest";

import type { Rule, RuleResult, Trigger } from "./api/types";
import { renderAutomations, trigSummary, type AutomationsDeps } from "./automations";

const flush = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}
const okRule: RuleResult = { ok: true, status: 200, body: { ok: true } };

function rule(overrides: Partial<Rule> = {}): Rule {
  return {
    id: "r1",
    enabled: true,
    modes: ["proxy", "standalone"], // Rule.to_dict always emits modes + debounce
    debounce: 0,
    trigger: { type: "scene", node: 5, scene_id: 2 },
    conditions: [],
    actions: [{ op: "switch", node: 5, on: true }],
    ...overrides,
  };
}

function deps(overrides: Partial<AutomationsDeps> = {}): AutomationsDeps {
  return {
    reload: () => undefined,
    onEdit: () => undefined,
    postRule: () => Promise.resolve(okRule),
    deleteRule: () => Promise.resolve(okRule),
    confirm: () => true,
    ...overrides,
  };
}

describe("trigSummary", () => {
  // op codes are the backend's CmpOp ("gt"/"lt"/…), echoed verbatim by trigSummary; time/sun triggers
  // always carry `days` (null when unscheduled) and `offset_min` per the contract.
  const cases: [Trigger, string][] = [
    [{ type: "scene", node: 5, scene_id: 2 }, "scene 2 @node 5"],
    [{ type: "state", node: 7, field: "temperature", op: "gt", value: 22 }, "node 7 temperature gt 22"],
    [{ type: "state", field: "crib_temp", op: "lt", value: 18 }, "crib_temp lt 18"], // global, no node
    [{ type: "time", at: "07:30", days: null }, "at 07:30"],
    [{ type: "time", at: "07:30", days: [0, 4] }, "at 07:30 [0,4]"],
    [{ type: "sun", event: "sunset", offset_min: 0, days: null }, "sunset"],
    [{ type: "sun", event: "sunset", offset_min: 0, days: null }, "sunset"], // 0 → no suffix
    [{ type: "sun", event: "sunset", offset_min: -15, days: null }, "sunset-15m"],
    [{ type: "sun", event: "sunrise", offset_min: 30, days: [5, 6] }, "sunrise+30m [5,6]"],
    [{ type: "time", at: "07:30", days: [] }, "at 07:30 []"], // empty days ≠ null
    [{ type: "time", at: "01:00", days: null }, "at 01:00"], // server sends days:null (NOT undefined) — must not throw
    [{ type: "sun", event: "sunrise", offset_min: 0, days: null }, "sunrise"], // same null shape for sun
    [{ type: "presence", mac: "aa:bb", event: "leave" }, "aa:bb leave"],
    [{ type: "cron", expr: "*/5 * * * *" }, "cron */5 * * * *"],
  ];
  for (const [t, expected] of cases) {
    it(`summarises ${t.type} → ${expected}`, () => {
      expect(trigSummary(t)).toBe(expected);
    });
  }
});

describe("renderAutomations", () => {
  it("renders a row per rule: id / enabled / trigger / cond count / action ops", () => {
    const tbody = document.createElement("tbody");
    renderAutomations(
      tbody,
      [
        rule({
          id: "eco",
          conditions: [
            { field: "switch", op: "eq", value: true },
            { field: "door", op: "eq", value: "open" },
          ],
          actions: [{ op: "ir" }, { op: "switch" }],
        }),
      ],
      deps(),
    );
    const tds = tbody.querySelectorAll("td");
    expect(tds[0]?.textContent).toBe("eco");
    expect(tds[1]?.querySelector("input")?.checked).toBe(true);
    expect(tds[2]?.textContent).toBe("scene 2 @node 5");
    expect(tds[3]?.textContent).toBe("2");
    expect(tds[4]?.textContent).toBe("ir, switch");
  });

  it("renders ALL rules even when a later one has a time trigger with days:null", () => {
    // Regression: a `days:null` time trigger made trigSummary throw mid-loop, so only the first row
    // appeared (the live symptom: TS showed 1 automation, legacy showed 2).
    const tbody = document.createElement("tbody");
    renderAutomations(
      tbody,
      [
        rule({ id: "klima-off", trigger: { type: "presence", mac: "aa:bb", event: "leave" } }),
        rule({ id: "blinds-down", trigger: { type: "time", at: "01:00", days: null } }),
      ],
      deps(),
    );
    const ids = [...tbody.querySelectorAll("tr")].map((tr) => tr.dataset.id);
    expect(ids).toEqual(["klima-off", "blinds-down"]); // BOTH rows, in order
    expect(tbody.querySelectorAll("tr")).toHaveLength(2);
  });

  it("tags each cell with a data-label for the mobile card layout", () => {
    const tbody = document.createElement("tbody");
    renderAutomations(tbody, [rule({ id: "eco" })], deps());
    const labels = [...tbody.querySelectorAll("td")].map((td) => td.dataset.label);
    // the trailing edit/delete cell mirrors its blank <th> → no label
    expect(labels).toEqual(["id", "enabled", "trigger", "conditions", "actions", undefined]);
  });

  it("Edit hands the rule to onEdit", () => {
    const tbody = document.createElement("tbody");
    const edited: Rule[] = [];
    renderAutomations(tbody, [rule()], deps({ onEdit: (r) => edited.push(r) }));
    tbody.querySelector<HTMLButtonElement>(".auto-edit")?.click();
    expect(edited).toHaveLength(1);
    expect(edited[0]?.id).toBe("r1");
  });

  it("Delete confirms, deletes by id, then reloads", async () => {
    const tbody = document.createElement("tbody");
    let reloaded = 0;
    const deleted: string[] = [];
    renderAutomations(
      tbody,
      [rule()],
      deps({
        reload: () => {
          reloaded += 1;
        },
        deleteRule: (id) => {
          deleted.push(id);
          return Promise.resolve(okRule);
        },
      }),
    );
    tbody.querySelector<HTMLButtonElement>(".auto-del")?.click();
    await flush();
    expect(deleted).toEqual(["r1"]);
    expect(reloaded).toBe(1);
  });

  it("shows the error and re-enables Delete on failure (no reload)", async () => {
    const tbody = document.createElement("tbody");
    let reloaded = 0;
    renderAutomations(
      tbody,
      [rule()],
      deps({
        reload: () => {
          reloaded += 1;
        },
        deleteRule: () => Promise.resolve({ ok: false, status: 500, body: { ok: false, error: "busy" } }),
      }),
    );
    const del = tbody.querySelector<HTMLButtonElement>(".auto-del");
    del?.click();
    await flush();
    const status = tbody.querySelector(".status");
    expect(status?.textContent).toBe("busy");
    expect(status?.classList.contains("err")).toBe(true);
    expect(del?.disabled).toBe(false); // re-enabled by finally
    expect(reloaded).toBe(0);
  });

  it("drops a second toggle while the first save is in flight", async () => {
    const tbody = document.createElement("tbody");
    const gate = deferred<RuleResult>();
    let calls = 0;
    renderAutomations(
      tbody,
      [rule({ enabled: true })],
      deps({
        postRule: () => {
          calls += 1;
          return gate.promise;
        },
      }),
    );
    const box = tbody.querySelector<HTMLInputElement>(".auto-en");
    if (box !== null) {
      box.checked = false;
      box.dispatchEvent(new Event("change")); // first toggle → in flight
      box.checked = true;
      box.dispatchEvent(new Event("change")); // dropped by the `toggling` guard
    }
    expect(calls).toBe(1);
    gate.resolve(okRule);
    await flush();
  });

  it("renders an empty action cell for 0 actions and falls back to 'error N' when body is null", async () => {
    const tbody = document.createElement("tbody");
    renderAutomations(
      tbody,
      [rule({ actions: [] })],
      deps({ postRule: () => Promise.resolve({ ok: false, status: 503, body: null }) }),
    );
    expect(tbody.querySelectorAll("td")[4]?.textContent).toBe(""); // 0 actions → empty
    const box = tbody.querySelector<HTMLInputElement>(".auto-en");
    if (box !== null) {
      box.checked = !box.checked;
      box.dispatchEvent(new Event("change"));
    }
    await flush();
    expect(tbody.querySelector(".status")?.textContent).toBe("error 503"); // null body → status fallback
  });

  it("Delete is a no-op when not confirmed", async () => {
    const tbody = document.createElement("tbody");
    const deleted: string[] = [];
    renderAutomations(
      tbody,
      [rule()],
      deps({
        confirm: () => false,
        deleteRule: (id) => {
          deleted.push(id);
          return Promise.resolve(okRule);
        },
      }),
    );
    tbody.querySelector<HTMLButtonElement>(".auto-del")?.click();
    await flush();
    expect(deleted).toEqual([]);
  });

  it("toggling enabled re-saves the whole rule with the flag flipped, then reloads", async () => {
    const tbody = document.createElement("tbody");
    const sent: unknown[] = [];
    let reloaded = 0;
    renderAutomations(
      tbody,
      [rule({ enabled: true })],
      deps({
        reload: () => {
          reloaded += 1;
        },
        postRule: (p) => {
          sent.push(p);
          return Promise.resolve(okRule);
        },
      }),
    );
    const box = tbody.querySelector<HTMLInputElement>(".auto-en");
    if (box !== null) {
      box.checked = false;
      box.dispatchEvent(new Event("change"));
    }
    await flush();
    expect(sent).toEqual([rule({ enabled: false })]);
    expect(reloaded).toBe(1);
  });

  it("reverts the checkbox and shows the error when a toggle fails", async () => {
    const tbody = document.createElement("tbody");
    renderAutomations(
      tbody,
      [rule({ enabled: true })],
      deps({
        postRule: () => Promise.resolve({ ok: false, status: 400, body: { ok: false, error: "bad rule" } }),
      }),
    );
    const box = tbody.querySelector<HTMLInputElement>(".auto-en");
    if (box !== null) {
      box.checked = false;
      box.dispatchEvent(new Event("change"));
    }
    await flush();
    expect(box?.checked).toBe(true); // reverted to the server's truth
    expect(tbody.querySelector(".status")?.textContent).toBe("bad rule");
  });
});
