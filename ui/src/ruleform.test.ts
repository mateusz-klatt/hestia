import { describe, expect, it } from "vitest";

import type { DeviceInfo, Klima, Rule, RuleVocab } from "./api/types";
import { device, ruleVocab } from "./fixtures";
import { coerce, num, parseNode, reconstructionBlocker, renderRuleForm } from "./ruleform";
import type { RuleFormHandle } from "./ruleform";

// ---- pure helpers ---------------------------------------------------------

describe("parseNode", () => {
  const cases: [string, number | null][] = [
    ["26", 26],
    ["0x1a", 26],
    ["0x1A", 26],
    ["  26  ", 26],
    ["0xff", 255],
    ["", null],
    ["   ", null],
    ["abc", null],
    ["0xZZ", null],
    ["-5", null], // no sign
    ["12.5", null], // not an integer literal
    ["0x", null],
  ];
  for (const [input, expected] of cases) {
    it(`${JSON.stringify(input)} → ${String(expected)}`, () => {
      expect(parseNode(input)).toBe(expected);
    });
  }
});

describe("coerce", () => {
  const cases: [string, number | boolean | string | undefined][] = [
    ["", undefined],
    ["   ", undefined],
    ["21", 21],
    ["21.5", 21.5],
    ["-3", -3],
    ["true", true],
    ["false", false],
    ["hello", "hello"],
    ["  on  ", "on"], // trimmed string
    ["0x1a", "0x1a"], // hex is NOT a decimal → kept as a string
  ];
  for (const [input, expected] of cases) {
    it(`${JSON.stringify(input)} → ${String(expected)}`, () => {
      expect(coerce(input)).toBe(expected);
    });
  }
});

describe("num", () => {
  it("parses finite numbers (with trim)", () => {
    expect(num("5", "x")).toBe(5);
    expect(num("21.5", "x")).toBe(21.5);
    expect(num("  7  ", "x")).toBe(7);
    expect(num("-2", "x")).toBe(-2);
  });
  it("throws (labelled) on blank", () => {
    expect(() => num("", "celsius")).toThrow("celsius: number required");
    expect(() => num("   ", "celsius")).toThrow("celsius: number required");
  });
  it("throws (labelled) on non-numbers and infinities", () => {
    expect(() => num("abc", "scene_id")).toThrow("scene_id: invalid number");
    expect(() => num("1e999", "scene_id")).toThrow("scene_id: invalid number");
  });
});

// ---- renderRuleForm -------------------------------------------------------

const KLIMA: Klima = {
  file: "/ext/infrared/klima.ir",
  power_on: { cool: [22, 24], heat: [20] },
  presets: ["off"],
};

interface Form {
  box: HTMLElement;
  out: HTMLTextAreaElement;
  handle: RuleFormHandle;
  control: (label: string) => HTMLElement;
  triggerFields: () => HTMLElement;
  rows: (labelText: string) => HTMLElement;
  build: () => void;
  rule: () => Record<string, unknown>;
  status: () => string;
}

function mkForm(
  opts: { klima?: Klima; vocab?: RuleVocab; devices?: Record<string, DeviceInfo> } = {},
): Form {
  const box = document.createElement("div");
  const out = document.createElement("textarea");
  const handle = renderRuleForm(box, out, opts.vocab ?? ruleVocab(), opts.klima ?? {}, opts.devices);

  const control = (label: string): HTMLElement => {
    for (const lab of box.querySelectorAll("label")) {
      if (lab.textContent === `${label} `) {
        const c = lab.parentElement?.querySelector<HTMLElement>("input, select");
        if (c !== null && c !== undefined) return c;
      }
    }
    throw new Error(`no control labelled "${label}"`);
  };
  const triggerFields = (): HTMLElement => {
    const next = control("trigger").parentElement?.nextElementSibling;
    if (!(next instanceof HTMLElement)) throw new Error("no trigger fields");
    return next;
  };
  // the rows container is the span right after a standalone "conditions: " / "actions: " label span
  const rows = (labelText: string): HTMLElement => {
    const lab = [...box.querySelectorAll("span")].find((s) => s.textContent === labelText);
    const next = lab?.nextElementSibling;
    if (!(next instanceof HTMLElement)) throw new Error(`no container after "${labelText}"`);
    return next;
  };
  const click = (text: string): void => {
    [...box.querySelectorAll("button")].find((b) => b.textContent === text)?.click();
  };
  const build = (): void => {
    click("Build JSON");
  };
  const rule = (): Record<string, unknown> => JSON.parse(out.value) as Record<string, unknown>;
  const status = (): string => box.querySelector(".status")?.textContent ?? "";

  return { box, out, handle, control, triggerFields, rows, build, rule, status };
}

function setInput(container: HTMLElement, placeholder: string, value: string): void {
  const i = container.querySelector<HTMLInputElement>(`input[placeholder="${placeholder}"]`);
  if (i === null) throw new Error(`no input[placeholder="${placeholder}"]`);
  i.value = value;
}
function firstSelect(container: HTMLElement): HTMLSelectElement {
  const s = container.querySelector("select");
  if (s === null) throw new Error("no select");
  return s;
}
function setSelect(select: HTMLSelectElement, value: string): void {
  select.value = value;
  select.dispatchEvent(new Event("change"));
}

// Fill the always-present "id" + a valid default action so build() succeeds when
// we only care about the trigger under test. (Default action with no klima = switch.)
function fillBasics(f: Form, id = "r1"): void {
  setInput(f.box, "rule-id", id);
}
function fillSwitchAction(f: Form, node = "7"): void {
  setInput(f.rows("actions: "), "node", node);
}

describe("renderRuleForm — structure", () => {
  it("renders the header, trigger types, both helper buttons, and the build button", () => {
    const f = mkForm();
    expect(f.box.querySelector("div")?.textContent).toBe("Rule wizard");
    const trig = f.control("trigger") as HTMLSelectElement;
    expect([...trig.querySelectorAll("option")].map((o) => o.value)).toEqual([
      "scene",
      "state",
      "time",
      "sun",
      "presence",
      "cron",
    ]);
    const buttonLabels = [...f.box.querySelectorAll("button")].map((b) => b.textContent);
    expect(buttonLabels).toContain("+ condition");
    expect(buttonLabels).toContain("+ action");
    expect(buttonLabels).toContain("Build JSON");
  });

  it("renders one checkbox per mode, all checked by default", () => {
    const f = mkForm();
    expect((f.control("proxy") as HTMLInputElement).checked).toBe(true);
    expect((f.control("standalone") as HTMLInputElement).checked).toBe(true);
  });

  it("the time-trigger day picker shows localized Mon..Sun in backend order (UTC-pinned)", () => {
    // Guards the off-by-one: without timeZone:"UTC" a west-of-UTC runner would render the prior weekday,
    // mislabelling the Mon=0..Sun=6 checkboxes. The labels must always start at Monday.
    const f = mkForm();
    setSelect(f.control("trigger") as HTMLSelectElement, "time");
    const dayLabels = [...f.triggerFields().querySelectorAll("label")].map((l) => l.textContent);
    expect(dayLabels).toEqual(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]);
  });

  it("is built only once (idempotent across re-renders)", () => {
    const box = document.createElement("div");
    const out = document.createElement("textarea");
    renderRuleForm(box, out, ruleVocab(), {});
    const n = box.childNodes.length;
    renderRuleForm(box, out, ruleVocab(), {});
    expect(box.childNodes.length).toBe(n);
    expect(box.dataset.built).toBe("1");
  });

  it("the vocab fixture mirrors the real backend grammar (word-token cmp_ops, not symbols)", () => {
    // Guards against the form's whole premise drifting: the live server validates against
    // these exact tokens, so the dropdowns (and the tests) must use them, not "!=" / ">".
    const v = ruleVocab();
    expect(v.cmp_ops).toEqual(["eq", "ge", "gt", "le", "lt", "ne"]);
    expect(v.frame_action_ops).toContain("raw");
    expect(v.frame_action_ops).toContain("lights");
    expect(v.state_fields.crib_temp).toBe(true); // GLOBAL → node-less
    expect(v.state_fields.temperature).toBe(false); // per-node
  });

  it("does not mark itself built if the build throws — a later good payload rebuilds cleanly", () => {
    const box = document.createElement("div");
    const out = document.createElement("textarea");
    const bad = { ...ruleVocab(), modes: undefined as unknown as string[] };
    expect(() => {
      renderRuleForm(box, out, bad, {});
    }).toThrow(); // for...of undefined
    expect(box.dataset.built).toBeUndefined(); // NOT wedged: a malformed payload leaves it un-built
    renderRuleForm(box, out, ruleVocab(), {}); // a later well-formed render
    expect(box.dataset.built).toBe("1");
    const buttons = [...box.querySelectorAll("button")].map((b) => b.textContent);
    expect(buttons).toContain("Build JSON");
    expect(buttons.filter((t) => t === "Build JSON")).toHaveLength(1); // no duplicate from the partial attempt
  });
});

describe("renderRuleForm — triggers", () => {
  it("scene → {type, node, scene_id} (hex node accepted)", () => {
    const f = mkForm();
    fillBasics(f);
    setInput(f.triggerFields(), "node", "0x1a");
    setInput(f.triggerFields(), "scene_id", "2");
    fillSwitchAction(f);
    f.build();
    expect(f.rule().trigger).toEqual({ type: "scene", node: 26, scene_id: 2 });
  });

  it("state (global field) → no node; (non-global) → node required", () => {
    const f = mkForm();
    fillBasics(f);
    const trig = f.control("trigger") as HTMLSelectElement;
    setSelect(trig, "state");
    // default field = crib_temp (GLOBAL) → node input hidden, not needed
    const node = f.triggerFields().querySelector<HTMLInputElement>('input[placeholder="node"]');
    expect(node?.style.display).toBe("none");
    setInput(f.triggerFields(), "value", "18");
    fillSwitchAction(f);
    f.build();
    // default op = first of the real backend cmp_ops (sorted) = "eq" (NOT a symbolic operator)
    expect(f.rule().trigger).toEqual({ type: "state", field: "crib_temp", op: "eq", value: 18 });
  });

  it("state (non-global field) includes the node", () => {
    const f = mkForm();
    fillBasics(f);
    setSelect(f.control("trigger") as HTMLSelectElement, "state");
    const field = firstSelect(f.triggerFields());
    setSelect(field, "temperature"); // non-global → node shown
    const node = f.triggerFields().querySelector<HTMLInputElement>('input[placeholder="node"]');
    expect(node?.style.display).toBe("");
    setInput(f.triggerFields(), "value", "22.5");
    setInput(f.triggerFields(), "node", "9");
    fillSwitchAction(f);
    f.build();
    expect(f.rule().trigger).toEqual({
      type: "state",
      field: "temperature",
      op: "eq",
      value: 22.5,
      node: 9,
    });
  });

  it("time → {type, at} and includes days only when ticked", () => {
    const f = mkForm();
    fillBasics(f);
    setSelect(f.control("trigger") as HTMLSelectElement, "time");
    setInput(f.triggerFields(), "HH:MM", "07:30");
    fillSwitchAction(f);
    f.build();
    expect(f.rule().trigger).toEqual({ type: "time", at: "07:30" });

    // tick Pn (Mon=0) and Pt (Fri=4)
    const days = [...f.triggerFields().querySelectorAll<HTMLInputElement>('input[type="checkbox"]')];
    if (days[0] !== undefined) days[0].checked = true;
    if (days[4] !== undefined) days[4].checked = true;
    f.build();
    expect(f.rule().trigger).toEqual({ type: "time", at: "07:30", days: [0, 4] });
  });

  it("sun → default offset 0; custom offset parsed; days optional", () => {
    const f = mkForm();
    fillBasics(f);
    setSelect(f.control("trigger") as HTMLSelectElement, "sun");
    setSelect(firstSelect(f.triggerFields()), "sunset");
    fillSwitchAction(f);
    f.build();
    expect(f.rule().trigger).toEqual({ type: "sun", event: "sunset", offset_min: 0 });

    setInput(f.triggerFields(), "offset min", "-15");
    f.build();
    expect(f.rule().trigger).toEqual({ type: "sun", event: "sunset", offset_min: -15 });
  });

  it("sun → blank offset is treated as 0", () => {
    const f = mkForm();
    fillBasics(f);
    setSelect(f.control("trigger") as HTMLSelectElement, "sun");
    setInput(f.triggerFields(), "offset min", "");
    fillSwitchAction(f);
    f.build();
    expect(f.rule().trigger).toEqual({ type: "sun", event: "sunrise", offset_min: 0 });
  });

  it("presence → {type, mac, event}", () => {
    const f = mkForm();
    fillBasics(f);
    setSelect(f.control("trigger") as HTMLSelectElement, "presence");
    setInput(f.triggerFields(), "aa:bb:cc:dd:ee:ff", "de:ad:be:ef:00:01");
    setSelect(firstSelect(f.triggerFields()), "leave");
    fillSwitchAction(f);
    f.build();
    expect(f.rule().trigger).toEqual({ type: "presence", mac: "de:ad:be:ef:00:01", event: "leave" });
  });

  it("cron → {type, expr}", () => {
    const f = mkForm();
    fillBasics(f);
    setSelect(f.control("trigger") as HTMLSelectElement, "cron");
    setInput(f.triggerFields(), "* * * * *", "*/5 * * * *");
    fillSwitchAction(f);
    f.build();
    expect(f.rule().trigger).toEqual({ type: "cron", expr: "*/5 * * * *" });
  });
});

describe("renderRuleForm — conditions", () => {
  it("adds predicate conditions and serialises them; × removes one", () => {
    const f = mkForm();
    fillBasics(f);
    setInput(f.triggerFields(), "node", "5");
    setInput(f.triggerFields(), "scene_id", "1");
    fillSwitchAction(f);

    const addCond = [...f.box.querySelectorAll("button")].find((b) => b.textContent === "+ condition");
    addCond?.click();
    addCond?.click();
    const condBox = f.rows("conditions: ");
    const editors = [...condBox.children];
    expect(editors).toHaveLength(2);

    // fill the first condition: temperature > 22 @node 9
    const first = editors[0];
    if (first instanceof HTMLElement) {
      const fieldSel = firstSelect(first);
      setSelect(fieldSel, "temperature");
      setInput(first, "value", "22");
      setInput(first, "node", "9");
      const opSel = first.querySelectorAll("select")[1];
      if (opSel !== undefined) setSelect(opSel, "gt"); // real backend token, not ">"
    }
    // remove the second (empty) condition via its × button
    const second = editors[1];
    if (second instanceof HTMLElement) {
      [...second.querySelectorAll("button")].find((b) => b.textContent === "×")?.click();
    }
    expect(condBox.children).toHaveLength(1);

    f.build();
    expect(f.rule().conditions).toEqual([{ field: "temperature", op: "gt", value: 22, node: 9 }]);
  });

  it("adds a time_window condition with optional days", () => {
    const f = mkForm();
    fillBasics(f);
    setInput(f.triggerFields(), "node", "5");
    setInput(f.triggerFields(), "scene_id", "1");
    fillSwitchAction(f);

    [...f.box.querySelectorAll("button")].find((b) => b.textContent === "+ active window")?.click();
    const condBox = f.rows("conditions: ");
    const win = condBox.children[0];
    expect(win).toBeInstanceOf(HTMLElement);
    if (win instanceof HTMLElement) {
      const times = win.querySelectorAll<HTMLInputElement>('input[placeholder="HH:MM"]');
      expect(times).toHaveLength(2);
      times.item(0).value = "6:00"; // canonicalised server-side; the wizard emits it verbatim
      times.item(1).value = "22:00";
      const day0 = win.querySelector<HTMLInputElement>('input[type="checkbox"]'); // Monday
      if (day0 !== null) day0.checked = true;
    }
    f.build();
    expect(f.rule().conditions).toEqual([
      { type: "time_window", start: "6:00", end: "22:00", days: [0] },
    ]);
  });

  it("rejects an active window with a blank time", () => {
    const f = mkForm();
    fillBasics(f);
    setInput(f.triggerFields(), "node", "5");
    setInput(f.triggerFields(), "scene_id", "1");
    fillSwitchAction(f);

    [...f.box.querySelectorAll("button")].find((b) => b.textContent === "+ active window")?.click();
    const win = f.rows("conditions: ").children[0];
    if (win instanceof HTMLElement) {
      const times = win.querySelectorAll<HTMLInputElement>('input[placeholder="HH:MM"]');
      expect(times).toHaveLength(2);
      times.item(1).value = "22:00";
    }
    f.build();
    expect(f.status()).toContain("start"); // first blank field flagged, no JSON emitted
  });
});

describe("renderRuleForm — node device combo", () => {
  const devices = {
    "26": device({ name: "Salon lampa", room: "Salon" }),
    "9": device({ name: "Termostat" }), // no room
    "40": device(), // unnamed → labelled by node id
  };

  it("builds a datalist of device names (value = node id), sorted by label; node inputs reference it", () => {
    const f = mkForm({ devices });
    const dl = f.box.querySelector<HTMLDataListElement>("datalist#rule-node-options");
    expect(dl).not.toBeNull();
    const opts = [...(dl?.querySelectorAll("option") ?? [])].map((o) => [o.value, o.label]);
    // sorted by label: "node 40" (n) < "Salon lampa · Salon" (S) < "Termostat" (T)
    expect(opts).toEqual([
      ["40", "node 40"],
      ["26", "Salon lampa · Salon"],
      ["9", "Termostat"],
    ]);
    // a node field (the scene trigger's) is a combo wired to the shared list
    setSelect(f.control("trigger") as HTMLSelectElement, "scene");
    const nodeInput = f.triggerFields().querySelector<HTMLInputElement>('input[placeholder="node"]');
    expect(nodeInput?.getAttribute("list")).toBe("rule-node-options");
  });

  it("picking a device inserts its node id, which serialises as the numeric node", () => {
    const f = mkForm({ devices });
    fillBasics(f);
    setSelect(f.control("trigger") as HTMLSelectElement, "scene");
    // datalist selection drops the option VALUE (the node id) into the input — emulate that
    setInput(f.triggerFields(), "node", "26");
    setInput(f.triggerFields(), "scene_id", "1");
    fillSwitchAction(f);
    f.build();
    expect((f.rule().trigger as { node: number }).node).toBe(26);
  });

  it("tolerates an empty device map (no options; free-text node still works)", () => {
    const f = mkForm(); // no devices
    const dl = f.box.querySelector<HTMLDataListElement>("datalist#rule-node-options");
    expect(dl?.querySelectorAll("option")).toHaveLength(0);
    fillBasics(f);
    setSelect(f.control("trigger") as HTMLSelectElement, "scene");
    setInput(f.triggerFields(), "node", "0x1a"); // undiscovered, hand-typed hex
    setInput(f.triggerFields(), "scene_id", "1");
    fillSwitchAction(f);
    f.build();
    expect((f.rule().trigger as { node: number }).node).toBe(26);
  });
});

describe("renderRuleForm — actions", () => {
  it("switch / thermostat_power → {op, node, on}", () => {
    const f = mkForm();
    fillBasics(f);
    setInput(f.triggerFields(), "node", "5");
    setInput(f.triggerFields(), "scene_id", "1");
    const actBox = f.rows("actions: ");
    setInput(actBox, "node", "7"); // default op = switch
    f.build();
    expect(f.rule().actions).toEqual([{ op: "switch", node: 7, on: true }]);

    setSelect(firstSelect(actBox), "thermostat_power");
    setInput(actBox, "node", "8");
    const on = [...actBox.querySelectorAll("select")].find((s) =>
      [...s.options].some((o) => o.value === "off"),
    );
    if (on !== undefined) setSelect(on, "off");
    f.build();
    expect(f.rule().actions).toEqual([{ op: "thermostat_power", node: 8, on: false }]);
  });

  it("level / cover → {op, node, value}", () => {
    const f = mkForm();
    fillBasics(f);
    setInput(f.triggerFields(), "node", "5");
    setInput(f.triggerFields(), "scene_id", "1");
    const actBox = f.rows("actions: ");
    setSelect(firstSelect(actBox), "level");
    setInput(actBox, "node", "3");
    setInput(actBox, "value", "50");
    f.build();
    expect(f.rule().actions).toEqual([{ op: "level", node: 3, value: 50 }]);

    setSelect(firstSelect(actBox), "cover");
    setInput(actBox, "node", "4");
    setInput(actBox, "value", "100");
    f.build();
    expect(f.rule().actions).toEqual([{ op: "cover", node: 4, value: 100 }]);
  });

  it("thermostat → {op, node, celsius}", () => {
    const f = mkForm();
    fillBasics(f);
    setInput(f.triggerFields(), "node", "5");
    setInput(f.triggerFields(), "scene_id", "1");
    const actBox = f.rows("actions: ");
    setSelect(firstSelect(actBox), "thermostat");
    setInput(actBox, "node", "6");
    setInput(actBox, "°C", "21");
    f.build();
    expect(f.rule().actions).toEqual([{ op: "thermostat", node: 6, celsius: 21 }]);
  });

  it("ir → {op, file, button}", () => {
    const f = mkForm();
    fillBasics(f);
    setInput(f.triggerFields(), "node", "5");
    setInput(f.triggerFields(), "scene_id", "1");
    const actBox = f.rows("actions: ");
    setSelect(firstSelect(actBox), "ir");
    setInput(actBox, "/ext/infrared/x.ir", "/ext/infrared/tv.ir");
    setInput(actBox, "button", "Power");
    f.build();
    expect(f.rule().actions).toEqual([{ op: "ir", file: "/ext/infrared/tv.ir", button: "Power" }]);
  });

  it("adds and removes actions, keeping at least one", () => {
    const f = mkForm();
    fillBasics(f);
    setInput(f.triggerFields(), "node", "5");
    setInput(f.triggerFields(), "scene_id", "1");
    const actBox = f.rows("actions: ");
    const addAct = [...f.box.querySelectorAll("button")].find((b) => b.textContent === "+ action");
    addAct?.click();
    expect(actBox.children).toHaveLength(2);
    // remove the second
    const second = actBox.children[1];
    if (second instanceof HTMLElement) {
      [...second.querySelectorAll("button")].find((b) => b.textContent === "×")?.click();
    }
    expect(actBox.children).toHaveLength(1);
    // the × on the last remaining action is a no-op
    const last = actBox.children[0];
    if (last instanceof HTMLElement) {
      [...last.querySelectorAll("button")].find((b) => b.textContent === "×")?.click();
    }
    expect(actBox.children).toHaveLength(1);
  });
});

describe("renderRuleForm — klima action", () => {
  it("offers klima as the first op and builds the idempotent power-on signal", () => {
    const f = mkForm({ klima: KLIMA });
    fillBasics(f);
    setInput(f.triggerFields(), "node", "5");
    setInput(f.triggerFields(), "scene_id", "1");
    const actBox = f.rows("actions: ");
    expect([...firstSelect(actBox).options].map((o) => o.value)).toEqual([
      "klima",
      "switch",
      "level",
      "cover",
      "thermostat",
      "thermostat_power",
      "ir",
    ]);
    // default mode = cool → temps [22,24]; default temp = 22
    f.build();
    expect(f.rule().actions).toEqual([
      { op: "ir", file: "/ext/infrared/klima.ir", button: "on_cool_22" },
    ]);
  });

  it("klima mode switch refills temps; off → button 'off' (no temp)", () => {
    const f = mkForm({ klima: KLIMA });
    fillBasics(f);
    setInput(f.triggerFields(), "node", "5");
    setInput(f.triggerFields(), "scene_id", "1");
    const actBox = f.rows("actions: ");
    const selects = actBox.querySelectorAll("select");
    const mode = selects[1]; // [0]=op, [1]=mode, [2]=temp
    const temp = selects[2];
    if (mode !== undefined) setSelect(mode, "heat");
    expect([...(temp?.options ?? [])].map((o) => o.value)).toEqual(["20"]);
    f.build();
    expect(f.rule().actions).toEqual([
      { op: "ir", file: "/ext/infrared/klima.ir", button: "on_heat_20" },
    ]);

    if (mode !== undefined) setSelect(mode, "off");
    expect(temp?.style.display).toBe("none");
    f.build();
    expect(f.rule().actions).toEqual([{ op: "ir", file: "/ext/infrared/klima.ir", button: "off" }]);
  });

  it("does not offer klima when no klima.ir is loaded", () => {
    const f = mkForm(); // no klima
    const actBox = f.rows("actions: ");
    expect([...firstSelect(actBox).options].map((o) => o.value)).not.toContain("klima");
  });

  it("sorts the mode options regardless of power_on key order", () => {
    // power_on keys come from JSON (insertion order not guaranteed) → the form must .sort() them
    const f = mkForm({
      klima: { file: "/ext/infrared/klima.ir", power_on: { heat: [20], cool: [22], auto: [21] }, presets: ["off"] },
    });
    fillBasics(f);
    setInput(f.triggerFields(), "node", "5");
    setInput(f.triggerFields(), "scene_id", "1");
    const actBox = f.rows("actions: ");
    const mode = actBox.querySelectorAll("select")[1]; // [0]=op, [1]=mode (+off), [2]=temp
    expect([...(mode?.options ?? [])].map((o) => o.value)).toEqual(["auto", "cool", "heat", "off"]);
    // default selected mode = first sorted ("auto") → its temp 21
    f.build();
    expect(f.rule().actions).toEqual([
      { op: "ir", file: "/ext/infrared/klima.ir", button: "on_auto_21" },
    ]);
  });
});

describe("renderRuleForm — id / enabled / debounce / modes", () => {
  it("captures enabled=false, a custom debounce, and a partial mode selection", () => {
    const f = mkForm();
    fillBasics(f, "eco");
    (f.control("enabled") as HTMLInputElement).checked = false;
    (f.control("debounce s") as HTMLInputElement).value = "30";
    (f.control("standalone") as HTMLInputElement).checked = false; // only proxy
    setInput(f.triggerFields(), "node", "5");
    setInput(f.triggerFields(), "scene_id", "1");
    fillSwitchAction(f);
    f.build();
    const r = f.rule();
    expect(r.id).toBe("eco");
    expect(r.enabled).toBe(false);
    expect(r.debounce).toBe(30);
    expect(r.modes).toEqual(["proxy"]);
  });

  it("blank debounce defaults to 0", () => {
    const f = mkForm();
    fillBasics(f);
    (f.control("debounce s") as HTMLInputElement).value = "";
    setInput(f.triggerFields(), "node", "5");
    setInput(f.triggerFields(), "scene_id", "1");
    fillSwitchAction(f);
    f.build();
    expect(f.rule().debounce).toBe(0);
  });

  it("pins the untouched defaults: enabled=true, debounce=0, all modes on", () => {
    // Build a minimal rule WITHOUT touching enabled / debounce / mode boxes, so a
    // regression that flips a prefilled default (e.g. ships rules disabled or with a
    // non-zero debounce) is caught here rather than silently shipping.
    const f = mkForm();
    fillBasics(f);
    setInput(f.triggerFields(), "node", "5");
    setInput(f.triggerFields(), "scene_id", "1");
    fillSwitchAction(f);
    f.build();
    const r = f.rule();
    expect(r.enabled).toBe(true);
    expect(r.debounce).toBe(0);
    expect(r.modes).toEqual(["proxy", "standalone"]);
  });
});

describe("renderRuleForm — validation errors", () => {
  const statusClass = (f: Form): string => f.box.querySelector(".status")?.className ?? "";
  const expectErr = (drive: (f: Form) => void, fragment: string): void => {
    const f = mkForm();
    drive(f);
    f.build();
    expect(f.out.value).toBe(""); // nothing written on error
    expect(f.status()).toContain(fragment);
    expect(statusClass(f).split(" ")).toContain("err"); // error → red `err` class
  };

  it("missing id", () => {
    expectErr(() => undefined, "id required");
  });

  it("no mode selected", () => {
    expectErr((f) => {
      fillBasics(f);
      (f.control("proxy") as HTMLInputElement).checked = false;
      (f.control("standalone") as HTMLInputElement).checked = false;
    }, "select a mode");
  });

  it("scene node invalid", () => {
    expectErr((f) => {
      fillBasics(f);
      setInput(f.triggerFields(), "node", "nope");
      setInput(f.triggerFields(), "scene_id", "1");
    }, "node: required");
  });

  it("scene_id not a number", () => {
    expectErr((f) => {
      fillBasics(f);
      setInput(f.triggerFields(), "node", "5");
      setInput(f.triggerFields(), "scene_id", "");
    }, "scene_id: number required");
  });

  it("predicate without a value", () => {
    expectErr((f) => {
      fillBasics(f);
      setSelect(f.control("trigger") as HTMLSelectElement, "state");
    }, "no value");
  });

  it("predicate (non-global) without a node", () => {
    expectErr((f) => {
      fillBasics(f);
      setSelect(f.control("trigger") as HTMLSelectElement, "state");
      setSelect(firstSelect(f.triggerFields()), "temperature");
      setInput(f.triggerFields(), "value", "22");
    }, "node required");
  });

  it("time without at", () => {
    expectErr((f) => {
      fillBasics(f);
      setSelect(f.control("trigger") as HTMLSelectElement, "time");
    }, "at: required");
  });

  it("presence without mac", () => {
    expectErr((f) => {
      fillBasics(f);
      setSelect(f.control("trigger") as HTMLSelectElement, "presence");
    }, "mac: required");
  });

  it("cron without expr", () => {
    expectErr((f) => {
      fillBasics(f);
      setSelect(f.control("trigger") as HTMLSelectElement, "cron");
    }, "expr: required");
  });

  it("ir action missing file/button", () => {
    expectErr((f) => {
      fillBasics(f);
      setInput(f.triggerFields(), "node", "5");
      setInput(f.triggerFields(), "scene_id", "1");
      setSelect(firstSelect(f.rows("actions: ")), "ir");
    }, "file+button: required");
  });

  it("a successful build after an error replaces the error status and clears the err class", () => {
    const f = mkForm();
    f.build();
    expect(f.status()).toContain("id required");
    expect(statusClass(f).split(" ")).toContain("err");
    fillBasics(f);
    setInput(f.triggerFields(), "node", "5");
    setInput(f.triggerFields(), "scene_id", "1");
    fillSwitchAction(f);
    f.build();
    expect(f.status()).toContain("built");
    expect(statusClass(f).split(" ")).not.toContain("err"); // success clears the err class
    expect(f.rule().id).toBe("r1");
  });
});

// ---- A3: "Edit" reconstructs the wizard from a saved rule -----------------
function buildButton(f: Form): HTMLButtonElement {
  const b = [...f.box.querySelectorAll("button")].find((x) => x.textContent === "Build JSON");
  if (!(b instanceof HTMLButtonElement)) throw new Error("no Build JSON button");
  return b;
}
function asRule(o: unknown): Rule {
  return o as Rule;
}
function makeRule(over: Record<string, unknown>): Rule {
  return asRule({
    id: "r1",
    enabled: true,
    modes: ["proxy", "standalone"],
    debounce: 0,
    trigger: { type: "scene", node: 1, scene_id: 1 },
    conditions: [],
    actions: [{ op: "switch", node: 1, on: true }],
    ...over,
  });
}

describe("renderRuleForm — Edit round-trip (loadRule)", () => {
  it("fully reconstructs a rule and re-emits it unchanged (scene + time_window + predicate + switch)", () => {
    const rule = makeRule({
      id: "pir-light",
      debounce: 2,
      trigger: { type: "scene", node: 14, scene_id: 1 },
      conditions: [
        { type: "time_window", start: "06:00", end: "22:00", days: [0, 1, 2, 3, 4] },
        { node: 9, field: "temperature", op: "lt", value: 18 },
      ],
      actions: [{ op: "switch", node: 14, on: true }],
    });
    const f = mkForm();
    expect(f.handle.loadRule(rule)).toEqual({ mode: "loaded" });
    f.build();
    expect(f.rule()).toEqual(rule); // semantic round-trip (key order ignored by deepEqual)
  });

  const triggers: Record<string, unknown>[] = [
    { type: "scene", node: 14, scene_id: 3 },
    { type: "state", node: 7, field: "temperature", op: "lt", value: 18 },
    { type: "state", node: 16, field: "motion", op: "eq", value: true }, // PIR motion edge
    { type: "state", node: 1, field: "switch", op: "eq", value: true }, // switch, no gang → endpoint stays absent
    { type: "state", node: 2, endpoint: 1, field: "switch", op: "eq", value: false }, // one gang of a 2-gang switch
    { type: "state", field: "crib_temp", op: "gt", value: 24 }, // GLOBAL → no node
    { type: "time", at: "07:30", days: [0, 4] },
    { type: "time", at: "07:30" }, // no days
    { type: "sun", event: "sunset", offset_min: 15, days: [5, 6] },
    { type: "presence", mac: "aa:bb:cc:dd:ee:ff", event: "arrive" },
    { type: "cron", expr: "30 7 * * 1" },
  ];
  for (const trigger of triggers) {
    it(`round-trips the ${String(trigger.type)} trigger (${JSON.stringify(trigger)})`, () => {
      const rule = makeRule({ trigger });
      const f = mkForm();
      expect(f.handle.loadRule(rule).mode).toBe("loaded");
      f.build();
      expect((f.rule() as { trigger: unknown }).trigger).toEqual(trigger);
    });
  }

  const actions: Record<string, unknown>[] = [
    { op: "switch", node: 14, on: true },
    { op: "switch", node: 10, on: true, endpoint: 1 }, // one gang of a multi-gang switch
    { op: "thermostat_power", node: 9, on: false },
    { op: "level", node: 5, value: 50 },
    { op: "cover", node: 4, value: 0 },
    { op: "thermostat", node: 9, celsius: 21 },
    { op: "ir", file: "/ext/infrared/klima.ir", button: "on_cool_22" },
  ];
  for (const action of actions) {
    it(`round-trips the ${String(action.op)} action`, () => {
      const rule = makeRule({ actions: [action] });
      const f = mkForm();
      expect(f.handle.loadRule(rule).mode).toBe("loaded");
      f.build();
      expect((f.rule() as { actions: unknown[] }).actions).toEqual([action]);
    });
  }

  it("clears stale condition/action rows when loading a smaller rule", () => {
    const f = mkForm();
    f.handle.loadRule(
      makeRule({
        conditions: [
          { node: 1, field: "switch", op: "eq", value: true },
          { type: "time_window", start: "06:00", end: "22:00" },
        ],
        actions: [
          { op: "switch", node: 1, on: true },
          { op: "cover", node: 2, value: 0 },
        ],
      }),
    );
    expect(f.rows("conditions: ").children).toHaveLength(2);
    expect(f.rows("actions: ").children).toHaveLength(2);
    f.handle.loadRule(makeRule({ conditions: [], actions: [{ op: "switch", node: 9, on: false }] }));
    expect(f.rows("conditions: ").children).toHaveLength(0);
    expect(f.rows("actions: ").children).toHaveLength(1);
  });
});

describe("renderRuleForm — Edit raw-only fallback", () => {
  const cases: [string, Record<string, unknown>, string][] = [
    ["a raw action op", { actions: [{ op: "raw", frame: "ab" }] }, "raw"],
    ["a lights action op", { actions: [{ op: "lights", node: 1, on: true }] }, "lights"],
    ["a switch action with an out-of-range gang", { actions: [{ op: "switch", node: 1, on: true, endpoint: 3 }] }, "switch"],
    ["a thermostat_power action with a gang", { actions: [{ op: "thermostat_power", node: 1, on: true, endpoint: 1 }] }, "thermostat_power"],
    ["a state trigger with an out-of-range gang", { trigger: { type: "state", node: 2, endpoint: 3, field: "switch", op: "eq", value: true } }, "state"],
    ["a gang on a non-switch condition", { conditions: [{ node: 7, endpoint: 1, field: "temperature", op: "lt", value: 18 }] }, "#1"],
    ["an unknown trigger type", { trigger: { type: "webhook", url: "x" } }, "webhook"],
    ["a malformed condition", { conditions: [{ foo: "bar" }] }, "#1"],
  ];
  for (const [label, over, needle] of cases) {
    it(`falls back to raw-only for ${label} (Build JSON disabled, reason names it)`, () => {
      const f = mkForm();
      const report = f.handle.loadRule(makeRule(over));
      expect(report.mode).toBe("raw-only");
      if (report.mode === "raw-only") expect(report.reason).toContain(needle);
      expect(buildButton(f).disabled).toBe(true);
      expect(f.status()).toContain(needle);
    });
  }

  it("reset() re-enables Build JSON and clears the form after a raw-only load", () => {
    const f = mkForm();
    f.handle.loadRule(makeRule({ actions: [{ op: "raw", frame: "ab" }] }));
    expect(buildButton(f).disabled).toBe(true);
    f.handle.reset();
    expect(buildButton(f).disabled).toBe(false);
    expect((f.control("id") as HTMLInputElement).value).toBe("");
    expect(f.rows("conditions: ").children).toHaveLength(0);
    expect(f.rows("actions: ").children).toHaveLength(1); // always ≥1 action row
  });

  it("loading a representable rule after a raw-only one re-enables Build JSON", () => {
    const f = mkForm();
    f.handle.loadRule(makeRule({ actions: [{ op: "raw", frame: "ab" }] }));
    expect(buildButton(f).disabled).toBe(true);
    expect(f.handle.loadRule(makeRule({})).mode).toBe("loaded");
    expect(buildButton(f).disabled).toBe(false);
  });
});

describe("reconstructionBlocker", () => {
  const vocab = ruleVocab();
  it("returns null for a fully representable rule", () => {
    expect(reconstructionBlocker(makeRule({}), vocab)).toBeNull();
    expect(
      reconstructionBlocker(
        makeRule({
          trigger: { type: "state", field: "crib_temp", op: "gt", value: 24 },
          conditions: [{ type: "time_window", start: "06:00", end: "22:00", days: [0] }],
          actions: [{ op: "ir", file: "/x.ir", button: "b" }],
        }),
        vocab,
      ),
    ).toBeNull();
  });
  it("flags an unknown comparison op as an unrepresentable predicate condition", () => {
    expect(
      reconstructionBlocker(makeRule({ conditions: [{ node: 1, field: "switch", op: "between", value: 1 }] }), vocab),
    ).toContain("#1");
  });
  it("flags a non-global predicate missing its node", () => {
    expect(
      reconstructionBlocker(makeRule({ trigger: { type: "state", field: "temperature", op: "lt", value: 5 } }), vocab),
    ).not.toBeNull();
  });
  it("flags out-of-range days on a time trigger", () => {
    expect(reconstructionBlocker(makeRule({ trigger: { type: "time", at: "07:00", days: [9] } }), vocab)).not.toBeNull();
  });
  it("accepts a time_window whose days are absent or null", () => {
    expect(
      reconstructionBlocker(makeRule({ conditions: [{ type: "time_window", start: "22:00", end: "06:00", days: null }] }), vocab),
    ).toBeNull();
  });
});

// ---- A3 hardening (from the adversarial review) ---------------------------
describe("renderRuleForm — Edit value-type fidelity & op-change", () => {
  it("round-trips a genuine string predicate value (door eq \"open\")", () => {
    const trigger = { type: "state", node: 5, field: "door", op: "eq", value: "open" };
    const f = mkForm();
    expect(f.handle.loadRule(makeRule({ trigger })).mode).toBe("loaded");
    f.build();
    expect((f.rule() as { trigger: unknown }).trigger).toEqual(trigger);
  });

  it("sends a numeric-/bool-looking STRING predicate value to raw-only (coerce would re-type it)", () => {
    for (const value of ["18", "21.5", "true", "false"]) {
      const f = mkForm();
      const report = f.handle.loadRule(makeRule({ conditions: [{ node: 9, field: "temperature", op: "eq", value }] }));
      expect(report.mode).toBe("raw-only"); // would otherwise round-trip to a number/boolean
    }
  });

  it("changing an action's op type after load does NOT re-seed the loaded rule's stale values", () => {
    const f = mkForm();
    f.handle.loadRule(makeRule({ actions: [{ op: "switch", node: 14, on: true }] }));
    const actBox = f.rows("actions: ");
    const opSel = actBox.querySelector("select");
    if (opSel === null) throw new Error("no op select");
    setSelect(opSel, "cover");
    const node = actBox.querySelector<HTMLInputElement>('input[placeholder="node"]');
    expect(node?.value).toBe(""); // fresh cover fields — not the switch action's node "14"
  });

  it("round-trips a single-mode subset and enabled:false", () => {
    const rule = makeRule({ modes: ["standalone"], enabled: false });
    const f = mkForm();
    expect(f.handle.loadRule(rule).mode).toBe("loaded");
    f.build();
    expect(f.rule()).toEqual(rule);
  });

  it("reconstructionBlocker accepts a real string value but rejects a numeric-/bool-looking string", () => {
    const v = ruleVocab();
    expect(reconstructionBlocker(makeRule({ trigger: { type: "state", node: 5, field: "door", op: "eq", value: "open" } }), v)).toBeNull();
    expect(reconstructionBlocker(makeRule({ conditions: [{ node: 9, field: "temperature", op: "eq", value: "18" }] }), v)).toContain("#1");
    expect(reconstructionBlocker(makeRule({ conditions: [{ node: 1, field: "switch", op: "eq", value: "true" }] }), v)).not.toBeNull();
  });
});

// ---- A3 hardening round 2 (from the final Codex review): trim/exponent fidelity ----
describe("renderRuleForm — Edit lossless trim & exponent guards", () => {
  it("sends an exponent-notation numeric predicate value to raw-only (coerce can't reparse it)", () => {
    const f = mkForm();
    const report = f.handle.loadRule(makeRule({ conditions: [{ node: 9, field: "temperature", op: "gt", value: 1e-7 }] }));
    expect(report.mode).toBe("raw-only"); // 1e-7 → "1e-7" → would re-emit as the STRING "1e-7"
  });

  it("still loads a plain-decimal numeric value", () => {
    const f = mkForm();
    expect(f.handle.loadRule(makeRule({ conditions: [{ node: 9, field: "temperature", op: "gt", value: 21.5 }] })).mode).toBe("loaded");
  });

  it("sends an ir action with whitespace-padded file/button to raw-only (Build trims them)", () => {
    const v = ruleVocab();
    expect(reconstructionBlocker(makeRule({ actions: [{ op: "ir", file: " /x.ir", button: "on" }] }), v)).toContain("ir");
    expect(reconstructionBlocker(makeRule({ actions: [{ op: "ir", file: "/x.ir", button: "on " }] }), v)).toContain("ir");
    expect(reconstructionBlocker(makeRule({ actions: [{ op: "ir", file: "/x.ir", button: "on" }] }), v)).toBeNull();
  });

  it("sends a rule whose id has surrounding whitespace to raw-only (Build trims the id)", () => {
    const f = mkForm();
    expect(f.handle.loadRule(makeRule({ id: " r1 " })).mode).toBe("raw-only");
    expect(reconstructionBlocker(makeRule({ id: " r1 " }), ruleVocab())).not.toBeNull();
    expect(reconstructionBlocker(makeRule({ id: "r1" }), ruleVocab())).toBeNull();
  });
});
