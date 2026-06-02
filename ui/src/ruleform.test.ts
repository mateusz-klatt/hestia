import { describe, expect, it } from "vitest";

import type { Klima, RuleVocab } from "./api/types";
import { ruleVocab } from "./fixtures";
import { coerce, num, parseNode, renderRuleForm } from "./ruleform";

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
    expect(() => num("", "celsius")).toThrow("celsius: liczba wymagana");
    expect(() => num("   ", "celsius")).toThrow("celsius: liczba wymagana");
  });
  it("throws (labelled) on non-numbers and infinities", () => {
    expect(() => num("abc", "scene_id")).toThrow("scene_id: nieprawidłowa liczba");
    expect(() => num("1e999", "scene_id")).toThrow("scene_id: nieprawidłowa liczba");
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
  control: (label: string) => HTMLElement;
  triggerFields: () => HTMLElement;
  rows: (labelText: string) => HTMLElement;
  build: () => void;
  rule: () => Record<string, unknown>;
  status: () => string;
}

function mkForm(opts: { klima?: Klima; vocab?: RuleVocab } = {}): Form {
  const box = document.createElement("div");
  const out = document.createElement("textarea");
  renderRuleForm(box, out, opts.vocab ?? ruleVocab(), opts.klima ?? {});

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
  // the rows container is the span right after a standalone "warunki: " / "akcje: " label span
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
    click("Zbuduj JSON");
  };
  const rule = (): Record<string, unknown> => JSON.parse(out.value) as Record<string, unknown>;
  const status = (): string => box.querySelector(".status")?.textContent ?? "";

  return { box, out, control, triggerFields, rows, build, rule, status };
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
  setInput(f.rows("akcje: "), "node", node);
}

describe("renderRuleForm — structure", () => {
  it("renders the header, trigger types, both helper buttons, and the build button", () => {
    const f = mkForm();
    expect(f.box.querySelector("div")?.textContent).toBe("Kreator reguły");
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
    expect(buttonLabels).toContain("+ warunek");
    expect(buttonLabels).toContain("+ akcja");
    expect(buttonLabels).toContain("Zbuduj JSON");
  });

  it("renders one checkbox per mode, all checked by default", () => {
    const f = mkForm();
    expect((f.control("proxy") as HTMLInputElement).checked).toBe(true);
    expect((f.control("standalone") as HTMLInputElement).checked).toBe(true);
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
    expect(f.rule().trigger).toEqual({ type: "state", field: "crib_temp", op: "!=", value: 18 });
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
      op: "!=",
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

    const addCond = [...f.box.querySelectorAll("button")].find((b) => b.textContent === "+ warunek");
    addCond?.click();
    addCond?.click();
    const condBox = f.rows("warunki: ");
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
      if (opSel !== undefined) setSelect(opSel, ">");
    }
    // remove the second (empty) condition via its × button
    const second = editors[1];
    if (second instanceof HTMLElement) {
      [...second.querySelectorAll("button")].find((b) => b.textContent === "×")?.click();
    }
    expect(condBox.children).toHaveLength(1);

    f.build();
    expect(f.rule().conditions).toEqual([{ field: "temperature", op: ">", value: 22, node: 9 }]);
  });
});

describe("renderRuleForm — actions", () => {
  it("switch / thermostat_power → {op, node, on}", () => {
    const f = mkForm();
    fillBasics(f);
    setInput(f.triggerFields(), "node", "5");
    setInput(f.triggerFields(), "scene_id", "1");
    const actBox = f.rows("akcje: ");
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
    const actBox = f.rows("akcje: ");
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
    const actBox = f.rows("akcje: ");
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
    const actBox = f.rows("akcje: ");
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
    const actBox = f.rows("akcje: ");
    const addAct = [...f.box.querySelectorAll("button")].find((b) => b.textContent === "+ akcja");
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
    const actBox = f.rows("akcje: ");
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
    const actBox = f.rows("akcje: ");
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
    const actBox = f.rows("akcje: ");
    expect([...firstSelect(actBox).options].map((o) => o.value)).not.toContain("klima");
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
});

describe("renderRuleForm — validation errors", () => {
  const expectErr = (drive: (f: Form) => void, fragment: string): void => {
    const f = mkForm();
    drive(f);
    f.build();
    expect(f.out.value).toBe(""); // nothing written on error
    expect(f.status()).toContain(fragment);
  };

  it("missing id", () => {
    expectErr(() => undefined, "id wymagane");
  });

  it("no mode selected", () => {
    expectErr((f) => {
      fillBasics(f);
      (f.control("proxy") as HTMLInputElement).checked = false;
      (f.control("standalone") as HTMLInputElement).checked = false;
    }, "wybierz tryb");
  });

  it("scene node invalid", () => {
    expectErr((f) => {
      fillBasics(f);
      setInput(f.triggerFields(), "node", "nope");
      setInput(f.triggerFields(), "scene_id", "1");
    }, "scene: node");
  });

  it("scene_id not a number", () => {
    expectErr((f) => {
      fillBasics(f);
      setInput(f.triggerFields(), "node", "5");
      setInput(f.triggerFields(), "scene_id", "");
    }, "scene_id: liczba wymagana");
  });

  it("predicate without a value", () => {
    expectErr((f) => {
      fillBasics(f);
      setSelect(f.control("trigger") as HTMLSelectElement, "state");
    }, "brak wartości");
  });

  it("predicate (non-global) without a node", () => {
    expectErr((f) => {
      fillBasics(f);
      setSelect(f.control("trigger") as HTMLSelectElement, "state");
      setSelect(firstSelect(f.triggerFields()), "temperature");
      setInput(f.triggerFields(), "value", "22");
    }, "node wymagany");
  });

  it("time without at", () => {
    expectErr((f) => {
      fillBasics(f);
      setSelect(f.control("trigger") as HTMLSelectElement, "time");
    }, "time: at");
  });

  it("presence without mac", () => {
    expectErr((f) => {
      fillBasics(f);
      setSelect(f.control("trigger") as HTMLSelectElement, "presence");
    }, "presence: mac");
  });

  it("cron without expr", () => {
    expectErr((f) => {
      fillBasics(f);
      setSelect(f.control("trigger") as HTMLSelectElement, "cron");
    }, "cron: expr");
  });

  it("ir action missing file/button", () => {
    expectErr((f) => {
      fillBasics(f);
      setInput(f.triggerFields(), "node", "5");
      setInput(f.triggerFields(), "scene_id", "1");
      setSelect(firstSelect(f.rows("akcje: ")), "ir");
    }, "ir: file+button");
  });

  it("a successful build after an error replaces the error status", () => {
    const f = mkForm();
    f.build();
    expect(f.status()).toContain("id wymagane");
    fillBasics(f);
    setInput(f.triggerFields(), "node", "5");
    setInput(f.triggerFields(), "scene_id", "1");
    fillSwitchAction(f);
    f.build();
    expect(f.status()).toContain("zbudowano");
    expect(f.rule().id).toBe("r1");
  });
});
