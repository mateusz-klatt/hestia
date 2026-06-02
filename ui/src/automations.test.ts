import { describe, expect, it } from "vitest";

import type { Rule, RuleResult, Trigger } from "./api/types";
import { renderAutomations, trigSummary, type AutomationsDeps } from "./automations";

const flush = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));
const okRule: RuleResult = { ok: true, status: 200, body: { ok: true } };

function rule(overrides: Partial<Rule> = {}): Rule {
  return {
    id: "r1",
    enabled: true,
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
  const cases: [Trigger, string][] = [
    [{ type: "scene", node: 5, scene_id: 2 }, "scene 2 @node 5"],
    [{ type: "state", node: 7, field: "temperature", op: ">", value: 22 }, "node 7 temperature > 22"],
    [{ type: "state", field: "crib_temp", op: "<", value: 18 }, "crib_temp < 18"], // global, no node
    [{ type: "time", at: "07:30" }, "at 07:30"],
    [{ type: "time", at: "07:30", days: [0, 4] }, "at 07:30 [0,4]"],
    [{ type: "sun", event: "sunset" }, "sunset"],
    [{ type: "sun", event: "sunset", offset_min: -15 }, "sunset-15m"],
    [{ type: "sun", event: "sunrise", offset_min: 30, days: [5, 6] }, "sunrise+30m [5,6]"],
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
      [rule({ id: "eco", conditions: [1, 2], actions: [{ op: "ir" }, { op: "switch" }] })],
      deps(),
    );
    const tds = tbody.querySelectorAll("td");
    expect(tds[0]?.textContent).toBe("eco");
    expect(tds[1]?.querySelector("input")?.checked).toBe(true);
    expect(tds[2]?.textContent).toBe("scene 2 @node 5");
    expect(tds[3]?.textContent).toBe("2");
    expect(tds[4]?.textContent).toBe("ir, switch");
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
