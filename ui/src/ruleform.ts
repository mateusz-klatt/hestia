// The M3.1 guided rule form. It builds a trigger + conditions + actions rule
// object from dropdowns/inputs and writes it (pretty-printed) into the rule
// JSON textarea, where the operator reviews it and the existing "Save rule"
// button submits it for server-side validation (`Rule.from_dict`).
//
// Vocab comes from `data.rule_vocab` so the form's dropdowns cannot drift from
// validation; the klima action reuses `data.klima`. `raw` / `lights` ops are
// intentionally not offered here (author those directly in the JSON box). All
// text is set via `value` / `textContent`, never innerHTML, so device-supplied
// strings can never inject markup. The form is built once.

import type { Klima, RuleVocab } from "./api/types";

// ---- pure helpers (exported for unit testing) -----------------------------

/** Parse a node id written as hex (`0x1a`) or decimal (`26`); `null` when neither. */
export function parseNode(s: string): number | null {
  const t = s.trim();
  if (/^0x[0-9a-fA-F]+$/.test(t)) return parseInt(t, 16);
  if (/^[0-9]+$/.test(t)) return parseInt(t, 10);
  return null;
}

/**
 * Coerce a predicate value the way the validator expects: a decimal (incl.
 * floats like `21.5`) → number, `true`/`false` → boolean, anything else →
 * the trimmed string. A blank field → `undefined` (the predicate has no value).
 */
export function coerce(s: string): number | boolean | string | undefined {
  const t = s.trim();
  if (t === "") return undefined;
  if (/^-?[0-9]+(\.[0-9]+)?$/.test(t)) return Number(t);
  if (t === "true") return true;
  if (t === "false") return false;
  return t;
}

/**
 * A required finite number; throws (with a localized, field-labelled message)
 * when blank, NaN or infinite. Action params like `scene_id` / `celsius` are
 * NOT re-checked by `Rule.from_dict`, so a bad number must fail here.
 */
export function num(s: string, label: string): number {
  const t = s.trim();
  if (t === "") throw new Error(`${label}: liczba wymagana`);
  const n = Number(t);
  if (!Number.isFinite(n)) throw new Error(`${label}: nieprawidłowa liczba`);
  return n;
}

// ---- DOM builders ---------------------------------------------------------

function opt(parent: HTMLSelectElement, value: string, label?: string): void {
  const o = document.createElement("option");
  o.value = value;
  o.textContent = label ?? value;
  parent.appendChild(o);
}

function sel(values: readonly string[]): HTMLSelectElement {
  const s = document.createElement("select");
  s.style.marginRight = "0.25rem";
  for (const v of values) opt(s, v);
  return s;
}

function finp(placeholder: string, size = 7): HTMLInputElement {
  const i = document.createElement("input");
  i.type = "text";
  i.placeholder = placeholder;
  i.size = size;
  i.style.marginRight = "0.25rem";
  return i;
}

const DAY_NAMES = ["Pn", "Wt", "Śr", "Cz", "Pt", "So", "Nd"] as const;

/** Mon=0..Sun=6 weekday checkboxes (matches the backend `_validate_days`); none ticked → `null`. */
function daysPicker(): { el: HTMLElement; read: () => number[] | null } {
  const wrap = document.createElement("span");
  wrap.style.marginRight = "0.3rem";
  const boxes = DAY_NAMES.map((nm) => {
    const c = document.createElement("input");
    c.type = "checkbox";
    const l = document.createElement("label");
    l.style.marginRight = "0.15rem";
    l.append(c, document.createTextNode(nm));
    wrap.appendChild(l);
    return c;
  });
  return {
    el: wrap,
    read: () => {
      const d = boxes.map((c, i) => (c.checked ? i : -1)).filter((i) => i >= 0);
      return d.length > 0 ? d : null;
    },
  };
}

/** A state predicate (`{field, op, value [, node]}`) — `node` only when the field isn't GLOBAL. */
export type Predicate = {
  field: string;
  op: string;
  value: number | boolean | string;
  node?: number;
};

/**
 * field/op/value (+ node, hidden when the chosen field is GLOBAL). Reused by
 * the `state` trigger and by each condition. `read()` returns the predicate or
 * throws an Error describing the first invalid input.
 */
function predicateEditor(vocab: RuleVocab): { el: HTMLElement; read: () => Predicate } {
  const wrap = document.createElement("span");
  const field = sel(Object.keys(vocab.state_fields));
  const op = sel(vocab.cmp_ops);
  const val = finp("value", 7);
  const node = finp("node", 6);
  const syncNode = (): void => {
    node.style.display = vocab.state_fields[field.value] === true ? "none" : "";
  };
  field.addEventListener("change", syncNode);
  wrap.append(field, op, val, node);
  syncNode();
  return {
    el: wrap,
    read: () => {
      const f = field.value;
      const v = coerce(val.value);
      if (v === undefined) throw new Error(`predykat „${f}": brak wartości`);
      const p: Predicate = { field: f, op: op.value, value: v };
      if (vocab.state_fields[f] !== true) {
        const n = parseNode(node.value);
        if (n === null) throw new Error(`predykat „${f}": node wymagany`);
        p.node = n;
      }
      return p;
    },
  };
}

// ---- the form -------------------------------------------------------------

type ReadObj = Record<string, unknown>;

/**
 * Build the guided rule form into `box`, writing its result into `output` (the
 * rule JSON textarea). Built once — guarded by `dataset.built`, like the IR /
 * klima panels — so it survives the SSE-driven re-renders unchanged.
 */
export function renderRuleForm(
  box: HTMLElement,
  output: HTMLTextAreaElement,
  vocab: RuleVocab,
  klima: Klima,
): void {
  if (box.dataset.built !== undefined) return;
  box.dataset.built = "1";

  // label + control on one inline span; returns the control so callers keep its precise type.
  const mk = <T extends HTMLElement>(label: string, element: T): T => {
    const w = document.createElement("span");
    w.style.marginRight = "0.6rem";
    const l = document.createElement("label");
    l.textContent = `${label} `;
    l.style.color = "#555";
    w.append(l, element);
    box.appendChild(w);
    return element;
  };
  const line = (): void => {
    box.appendChild(document.createElement("br"));
  };

  const hdr = document.createElement("div");
  hdr.textContent = "Kreator reguły";
  hdr.style.cssText = "font-weight:bold;margin-bottom:0.3rem;";
  box.appendChild(hdr);

  // id / enabled / debounce / modes
  const idIn = mk("id", finp("rule-id", 14));
  const enIn = document.createElement("input");
  enIn.type = "checkbox";
  enIn.checked = true;
  mk("enabled", enIn);
  const dbIn = finp("0", 4);
  dbIn.value = "0";
  mk("debounce s", dbIn);
  const modeBoxes = new Map<string, HTMLInputElement>();
  for (const m of vocab.modes) {
    const c = document.createElement("input");
    c.type = "checkbox";
    c.checked = true;
    modeBoxes.set(m, c);
    mk(m, c);
  }
  line();

  // trigger — the field set is rebuilt whenever the type changes
  const tType = mk("trigger", sel(vocab.trigger_types));
  const tFields = document.createElement("span");
  box.appendChild(tFields);
  let tRead: () => ReadObj = () => ({});
  const buildTrigger = (): void => {
    tFields.replaceChildren();
    const t = tType.value;
    if (t === "scene") {
      const node = finp("node", 6);
      const sid = finp("scene_id", 5);
      tFields.append(node, sid);
      tRead = () => {
        const n = parseNode(node.value);
        if (n === null) throw new Error("scene: node");
        return { node: n, scene_id: num(sid.value, "scene_id") };
      };
    } else if (t === "state") {
      const pe = predicateEditor(vocab);
      tFields.append(pe.el);
      tRead = () => pe.read();
    } else if (t === "time") {
      const at = finp("HH:MM", 6);
      const days = daysPicker();
      tFields.append(at, days.el);
      tRead = () => {
        const a = at.value.trim();
        if (a === "") throw new Error("time: at");
        const o: ReadObj = { at: a };
        const d = days.read();
        if (d !== null) o.days = d;
        return o;
      };
    } else if (t === "sun") {
      const ev = sel(vocab.sun_events);
      const off = finp("offset min", 6);
      off.value = "0";
      const days = daysPicker();
      tFields.append(ev, off, days.el);
      tRead = () => {
        const o: ReadObj = {
          event: ev.value,
          offset_min: off.value.trim() === "" ? 0 : num(off.value, "offset"),
        };
        const d = days.read();
        if (d !== null) o.days = d;
        return o;
      };
    } else if (t === "presence") {
      const mac = finp("aa:bb:cc:dd:ee:ff", 17);
      const ev = sel(vocab.presence_events);
      tFields.append(mac, ev);
      tRead = () => {
        const m = mac.value.trim();
        if (m === "") throw new Error("presence: mac");
        return { mac: m, event: ev.value };
      };
    } else {
      const expr = finp("* * * * *", 14);
      tFields.append(expr);
      tRead = () => {
        const e = expr.value.trim();
        if (e === "") throw new Error("cron: expr");
        return { expr: e };
      };
    }
  };
  tType.addEventListener("change", buildTrigger);
  buildTrigger();
  line();

  // conditions (0+)
  const condLbl = document.createElement("span");
  condLbl.textContent = "warunki: ";
  condLbl.style.color = "#555";
  box.appendChild(condLbl);
  const condBox = document.createElement("span");
  box.appendChild(condBox);
  const conds: { read: () => Predicate }[] = [];
  const addCondBtn = document.createElement("button");
  addCondBtn.type = "button";
  addCondBtn.textContent = "+ warunek";
  addCondBtn.addEventListener("click", () => {
    const pe = predicateEditor(vocab);
    const row = document.createElement("span");
    row.style.marginRight = "0.3rem";
    const entry = { read: pe.read };
    const rm = document.createElement("button");
    rm.type = "button";
    rm.textContent = "×";
    rm.title = "usuń";
    rm.addEventListener("click", () => {
      condBox.removeChild(row);
      const i = conds.indexOf(entry);
      if (i >= 0) conds.splice(i, 1);
    });
    row.append(pe.el, rm);
    condBox.appendChild(row);
    conds.push(entry);
  });
  box.appendChild(addCondBtn);
  line();

  // actions (1+) — klima offered as a friendly preset when a klima.ir is loaded
  const actLbl = document.createElement("span");
  actLbl.textContent = "akcje: ";
  actLbl.style.color = "#555";
  box.appendChild(actLbl);
  const actBox = document.createElement("span");
  box.appendChild(actBox);
  const acts: { read: () => ReadObj }[] = [];
  const klimaModes = klima.power_on !== undefined ? Object.keys(klima.power_on).sort() : [];
  const addAction = (): void => {
    const row = document.createElement("span");
    row.style.marginRight = "0.3rem";
    const op = sel(
      (klimaModes.length > 0 ? ["klima"] : []).concat([
        "switch",
        "level",
        "cover",
        "thermostat",
        "thermostat_power",
        "ir",
      ]),
    );
    const fields = document.createElement("span");
    let aRead: () => ReadObj = () => ({});
    const buildAct = (): void => {
      fields.replaceChildren();
      const k = op.value;
      if (k === "klima") {
        const mode = sel(klimaModes.concat(["off"]));
        const temp = sel([]);
        const fill = (): void => {
          temp.replaceChildren();
          temp.style.display = mode.value === "off" ? "none" : "";
          for (const t of klima.power_on?.[mode.value] ?? []) opt(temp, String(t), `${String(t)}°`);
        };
        mode.addEventListener("change", fill);
        fields.append(mode, temp);
        fill();
        // the idempotent power-on signal `on_<mode>_<temp>` (or `off`) — an `ir` action under the hood.
        aRead = () => ({
          op: "ir",
          file: klima.file,
          button: mode.value === "off" ? "off" : `on_${mode.value}_${temp.value}`,
        });
      } else if (k === "ir") {
        const file = finp("/ext/infrared/x.ir", 18);
        const btn = finp("button", 10);
        fields.append(file, btn);
        aRead = () => {
          const f = file.value.trim();
          const b = btn.value.trim();
          if (f === "" || b === "") throw new Error("ir: file+button");
          return { op: "ir", file: f, button: b };
        };
      } else if (k === "switch" || k === "thermostat_power") {
        const node = finp("node", 6);
        const on = sel(["on", "off"]);
        fields.append(node, on);
        aRead = () => {
          const n = parseNode(node.value);
          if (n === null) throw new Error(`${k}: node`);
          return { op: k, node: n, on: on.value === "on" };
        };
      } else if (k === "level" || k === "cover") {
        const node = finp("node", 6);
        const value = finp("value", 5);
        fields.append(node, value);
        aRead = () => {
          const n = parseNode(node.value);
          if (n === null) throw new Error(`${k}: node`);
          return { op: k, node: n, value: num(value.value, `${k} value`) };
        };
      } else {
        const node = finp("node", 6);
        const c = finp("°C", 5);
        fields.append(node, c);
        aRead = () => {
          const n = parseNode(node.value);
          if (n === null) throw new Error("thermostat: node");
          return { op: "thermostat", node: n, celsius: num(c.value, "celsius") };
        };
      }
    };
    op.addEventListener("change", buildAct);
    buildAct();
    const entry = { read: () => aRead() };
    const rm = document.createElement("button");
    rm.type = "button";
    rm.textContent = "×";
    rm.title = "usuń";
    rm.addEventListener("click", () => {
      if (acts.length <= 1) return; // always keep at least one action
      actBox.removeChild(row);
      const i = acts.indexOf(entry);
      if (i >= 0) acts.splice(i, 1);
    });
    row.append(op, fields, rm);
    actBox.appendChild(row);
    acts.push(entry);
  };
  const addActBtn = document.createElement("button");
  addActBtn.type = "button";
  addActBtn.textContent = "+ akcja";
  addActBtn.addEventListener("click", addAction);
  box.appendChild(addActBtn);
  addAction();
  line();

  // build → JSON (operator reviews, then "Save rule" validates server-side)
  const buildBtn = document.createElement("button");
  buildBtn.type = "button";
  buildBtn.textContent = "Zbuduj JSON";
  const formStatus = document.createElement("span");
  formStatus.className = "status";
  formStatus.style.marginLeft = "0.5rem";
  buildBtn.addEventListener("click", () => {
    try {
      const id = idIn.value.trim();
      if (id === "") throw new Error("id wymagane");
      const modes = vocab.modes.filter((m) => modeBoxes.get(m)?.checked === true);
      if (modes.length === 0) throw new Error("wybierz tryb");
      const rule = {
        id,
        enabled: enIn.checked,
        modes,
        debounce: dbIn.value.trim() === "" ? 0 : num(dbIn.value, "debounce"),
        trigger: Object.assign({ type: tType.value }, tRead()),
        conditions: conds.map((c) => c.read()),
        actions: acts.map((a) => a.read()),
      };
      output.value = JSON.stringify(rule, null, 2);
      formStatus.textContent = 'zbudowano — sprawdź i „Save rule"';
      formStatus.className = "status";
    } catch (e) {
      formStatus.textContent = `✗ ${e instanceof Error ? e.message : "błąd"}`;
      formStatus.className = "status err";
    }
  });
  box.append(buildBtn, formStatus);
}
