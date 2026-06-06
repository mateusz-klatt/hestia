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

import type { DeviceInfo, Klima, Rule, RuleAction, RuleVocab } from "./api/types";
import { currentLocale, t } from "./i18n";

// ---- pure helpers (exported for unit testing) -----------------------------

/** Parse a node id written as hex (`0x1a`) or decimal (`26`); `null` when neither. */
export function parseNode(s: string): number | null {
  const trimmed = s.trim();
  if (/^0x[0-9a-fA-F]+$/.test(trimmed)) return parseInt(trimmed, 16);
  if (/^[0-9]+$/.test(trimmed)) return parseInt(trimmed, 10);
  return null;
}

/**
 * Coerce a predicate value the way the validator expects: a decimal (incl.
 * floats like `21.5`) → number, `true`/`false` → boolean, anything else →
 * the trimmed string. A blank field → `undefined` (the predicate has no value).
 */
export function coerce(s: string): number | boolean | string | undefined {
  const trimmed = s.trim();
  if (trimmed === "") return undefined;
  if (/^-?[0-9]+(\.[0-9]+)?$/.test(trimmed)) return Number(trimmed);
  if (trimmed === "true") return true;
  if (trimmed === "false") return false;
  return trimmed;
}

/**
 * A required finite number; throws (with a localized, field-labelled message)
 * when blank, NaN or infinite. Action params like `scene_id` / `celsius` are
 * NOT re-checked by `Rule.from_dict`, so a bad number must fail here.
 */
export function num(s: string, label: string): number {
  const trimmed = s.trim();
  if (trimmed === "") throw new Error(t("rule.errNumberRequired", { label }));
  const n = Number(trimmed);
  if (!Number.isFinite(n)) throw new Error(t("rule.errInvalidNumber", { label }));
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

// The shared <datalist> id every `node` input points at, so picking a device name inserts its node id.
const NODE_LIST_ID = "rule-node-options";

/** A `node` field as a COMBO box: a free-text input (so an undiscovered node id still works, and
 *  `parseNode` reads it unchanged) backed by the shared datalist of discovered device names. */
function nodeInput(): HTMLInputElement {
  const i = finp(t("rule.phNode"), 14); // wider so a picked "name · room" label stays readable
  i.setAttribute("list", NODE_LIST_ID);
  return i;
}

/** Build the datalist of discovered nodes: option value = the node id (what lands in the input and
 *  feeds `parseNode`); option label = "name · room" (or the bare node id when unnamed). Sorted by
 *  label so the dropdown reads alphabetically. XSS-safe (value/label only, never innerHTML). */
function nodeDatalist(devices: Record<string, DeviceInfo>): HTMLDataListElement {
  const dl = document.createElement("datalist");
  dl.id = NODE_LIST_ID;
  const rows = Object.entries(devices).map(([node, info]) => {
    const name = info.name ?? "";
    const room = info.room ?? "";
    const label = name === "" ? `${t("rule.phNode")} ${node}` : room === "" ? name : `${name} · ${room}`;
    return { node, label };
  });
  rows.sort((a, b) => a.label.localeCompare(b.label));
  for (const { node, label } of rows) {
    const o = document.createElement("option");
    o.value = node;
    o.label = label;
    dl.appendChild(o);
  }
  return dl;
}

/** Localized Mon..Sun short weekday names for the day picker (backend order: Mon=0..Sun=6).
 *  2024-01-01 is a Monday; format it + the next 6 days in the app's locale. */
function dayNames(): string[] {
  // timeZone:"UTC" so a UTC-midnight date formats on its OWN day — without it a west-of-UTC browser
  // would render the previous weekday, shifting every label off-by-one against the Mon=0..Sun=6 index.
  const fmt = new Intl.DateTimeFormat(currentLocale(), { weekday: "short", timeZone: "UTC" });
  return Array.from({ length: 7 }, (_, i) => fmt.format(new Date(Date.UTC(2024, 0, 1 + i))));
}

/** Coerce a seed scalar (from a saved rule) back into the text an input expects; `coerce`/`num`
 *  reparse it on read. Bool → "true"/"false", number → its decimal text, string → itself. */
function scalarToInput(v: unknown): string {
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") return String(v);
  return typeof v === "string" ? v : "";
}

/** Mon=0..Sun=6 weekday checkboxes (matches the backend `_validate_days`); none ticked → `null`.
 *  `initial` pre-ticks the given weekday indices (used when reconstructing a saved rule). */
function daysPicker(initial?: readonly number[] | null): { el: HTMLElement; read: () => number[] | null } {
  const wrap = document.createElement("span");
  wrap.style.marginRight = "0.3rem";
  const seeded = new Set(initial ?? []);
  const boxes = dayNames().map((nm, i) => {
    const c = document.createElement("input");
    c.type = "checkbox";
    c.checked = seeded.has(i);
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
function predicateEditor(
  vocab: RuleVocab,
  seed?: { node?: number; field: string; op: string; value: unknown },
): { el: HTMLElement; read: () => Predicate } {
  const wrap = document.createElement("span");
  const field = sel(Object.keys(vocab.state_fields));
  const op = sel(vocab.cmp_ops);
  const val = finp(t("rule.phValue"), 7);
  const node = nodeInput();
  const syncNode = (): void => {
    node.style.display = vocab.state_fields[field.value] === true ? "none" : "";
  };
  field.addEventListener("change", syncNode);
  wrap.append(field, op, val, node);
  if (seed !== undefined) {
    field.value = seed.field;
    op.value = seed.op;
    val.value = scalarToInput(seed.value);
    if (seed.node !== undefined) node.value = String(seed.node);
  }
  syncNode();
  return {
    el: wrap,
    read: () => {
      const f = field.value;
      const v = coerce(val.value);
      if (v === undefined) throw new Error(t("rule.errNoValue", { field: f }));
      const p: Predicate = { field: f, op: op.value, value: v };
      if (vocab.state_fields[f] !== true) {
        const n = parseNode(node.value);
        if (n === null) throw new Error(t("rule.errNoNode", { field: f }));
        p.node = n;
      }
      return p;
    },
  };
}

/** A time-of-day GUARD condition: the rule fires only while `now` ∈ [start, end). `start > end`
 * wraps midnight; optional `days` (Mon=0..Sun=6) restricts it to those weekdays. */
export type TimeWindow = {
  type: "time_window";
  start: string;
  end: string;
  days?: number[];
};

/**
 * start/end (HH:MM) + an optional weekday picker → a `time_window` condition. Empty start/end
 * throw a localized error; the HH:MM format + the start≠end rule are re-checked server-side.
 */
function timeWindowEditor(seed?: TimeWindow): { el: HTMLElement; read: () => TimeWindow } {
  const wrap = document.createElement("span");
  const start = finp(t("rule.phWindowStart"), 6);
  const end = finp(t("rule.phWindowEnd"), 6);
  const days = daysPicker(seed?.days);
  if (seed !== undefined) {
    start.value = seed.start;
    end.value = seed.end;
  }
  const sep = document.createElement("span");
  sep.textContent = "→ ";
  sep.style.color = "#555";
  wrap.append(t("rule.windowFrom") + " ", start, sep, end, days.el);
  return {
    el: wrap,
    read: () => {
      const s = start.value.trim();
      const e = end.value.trim();
      if (s === "") throw new Error(t("rule.errRequired", { field: "start" }));
      if (e === "") throw new Error(t("rule.errRequired", { field: "end" }));
      const w: TimeWindow = { type: "time_window", start: s, end: e };
      const d = days.read();
      if (d !== null) w.days = d;
      return w;
    },
  };
}

// ---- the form -------------------------------------------------------------

type ReadObj = Record<string, unknown>;

// ---- "Edit" → reconstruct the wizard from a saved rule (A3) ----------------

/** Imperative handle returned by `renderRuleForm` so the Edit button can drive the wizard. */
export interface RuleFormHandle {
  /** Reconstruct the wizard from a saved rule, or report raw-only when it can't be represented. */
  loadRule: (rule: Rule) => RuleFormLoadReport;
  /** Reset the wizard to an empty "new rule" state and re-enable Build JSON. */
  reset: () => void;
}

/** `loaded`: the wizard now fully represents the rule. `raw-only`: it can't, so Build JSON is disabled
 *  and the JSON textarea stays authoritative; `reason` names the first unrepresentable part. */
export type RuleFormLoadReport = { mode: "loaded" } | { mode: "raw-only"; reason: string };

// The action ops the wizard can author, mapped to the EXACT non-`op` keys it emits. An action whose op
// is absent here (raw/lights/unknown), or that carries any other key (e.g. a per-endpoint `switch`),
// can't be represented losslessly → the rule loads raw-only.
const WIZARD_ACTION_KEYS: Record<string, readonly string[]> = {
  switch: ["node", "on"],
  thermostat_power: ["node", "on"],
  level: ["node", "value"],
  cover: ["node", "value"],
  thermostat: ["node", "celsius"],
  ir: ["file", "button"],
};

const FORM_HANDLES = new WeakMap<HTMLElement, RuleFormHandle>();

function isScalar(v: unknown): v is string | number | boolean {
  return typeof v === "string" || typeof v === "number" || typeof v === "boolean";
}

function daysRepresentable(d: unknown): boolean {
  if (d === undefined || d === null) return true;
  return (
    Array.isArray(d) &&
    d.length > 0 &&
    d.every((x) => typeof x === "number" && Number.isInteger(x) && x >= 0 && x <= 6)
  );
}

function onlyKeys(o: Record<string, unknown>, allowed: readonly string[]): boolean {
  const set = new Set(allowed);
  return Object.keys(o).every((k) => set.has(k));
}

/** A state predicate the wizard can edit: known field + op, a scalar value, a node when the field
 *  isn't GLOBAL, and no surprise keys. Shared by the `state` trigger and predicate conditions. */
function predicateRepresentable(o: Record<string, unknown>, vocab: RuleVocab): boolean {
  if (typeof o.field !== "string" || !(o.field in vocab.state_fields)) return false;
  if (typeof o.op !== "string" || !vocab.cmp_ops.includes(o.op)) return false;
  if (!isScalar(o.value)) return false;
  // The value must survive the wizard's write→read round-trip. coerce re-types a number-/bool-looking
  // STRING ("18"→18, "true"→true) and can't parse a number whose text isn't plain decimal (1e-7, 1e21),
  // so those load raw-only; `door eq "open"`, 18, 21.5, true all survive.
  if (coerce(scalarToInput(o.value)) !== o.value) return false;
  if (vocab.state_fields[o.field] !== true && typeof o.node !== "number") return false;
  return onlyKeys(o, ["type", "node", "field", "op", "value"]);
}

function timeWindowRepresentable(o: Record<string, unknown>): boolean {
  return (
    typeof o.start === "string" &&
    typeof o.end === "string" &&
    daysRepresentable(o.days) &&
    onlyKeys(o, ["type", "start", "end", "days"])
  );
}

function conditionRepresentable(c: unknown, vocab: RuleVocab): boolean {
  if (typeof c !== "object" || c === null) return false;
  const o = c as Record<string, unknown>;
  return o.type === "time_window" ? timeWindowRepresentable(o) : predicateRepresentable(o, vocab);
}

function triggerRepresentable(o: Record<string, unknown>, vocab: RuleVocab): boolean {
  switch (o.type) {
    case "scene":
      return typeof o.node === "number" && typeof o.scene_id === "number";
    case "state":
      return predicateRepresentable(o, vocab);
    case "time":
      return typeof o.at === "string" && daysRepresentable(o.days);
    case "sun":
      return (
        typeof o.event === "string" &&
        vocab.sun_events.includes(o.event) &&
        (o.offset_min === undefined || typeof o.offset_min === "number") &&
        daysRepresentable(o.days)
      );
    case "presence":
      return (
        typeof o.mac === "string" && typeof o.event === "string" && vocab.presence_events.includes(o.event)
      );
    case "cron":
      return typeof o.expr === "string";
    default:
      return false;
  }
}

function actionRepresentable(a: unknown): boolean {
  if (typeof a !== "object" || a === null) return false;
  const o = a as Record<string, unknown>;
  if (typeof o.op !== "string") return false;
  const keys = WIZARD_ACTION_KEYS[o.op];
  if (keys === undefined) return false; // raw / lights / unknown op
  if (!onlyKeys(o, ["op", ...keys])) return false; // no extra keys (e.g. a per-endpoint switch)
  if (!keys.every((k) => k in o)) return false; // every expected field present
  if ("node" in o && typeof o.node !== "number") return false;
  if ("on" in o && typeof o.on !== "boolean") return false;
  if ("value" in o && typeof o.value !== "number") return false;
  if ("celsius" in o && typeof o.celsius !== "number") return false;
  if ("file" in o && typeof o.file !== "string") return false;
  if ("button" in o && typeof o.button !== "string") return false;
  // Build trims ir file/button, so a saved value padded with whitespace wouldn't round-trip → raw-only.
  if (typeof o.file === "string" && o.file !== o.file.trim()) return false;
  if (typeof o.button === "string" && o.button !== o.button.trim()) return false;
  return true;
}

/** The first reason the wizard can't losslessly represent `rule` (localized), or null when it can.
 *  Exported for unit tests. */
export function reconstructionBlocker(rule: Rule, vocab: RuleVocab): string | null {
  // Build JSON trims the id, so an id padded with whitespace (or empty) wouldn't round-trip.
  if (typeof rule.id !== "string" || rule.id === "" || rule.id !== rule.id.trim()) {
    return t("rule.rcId");
  }
  const tr = rule.trigger as unknown as Record<string, unknown>;
  if (!triggerRepresentable(tr, vocab)) {
    return t("rule.rcTrigger", { type: typeof tr.type === "string" ? tr.type : "?" });
  }
  const conditions = Array.isArray(rule.conditions) ? rule.conditions : [];
  for (let i = 0; i < conditions.length; i++) {
    if (!conditionRepresentable(conditions[i], vocab)) return t("rule.rcCondition", { n: i + 1 });
  }
  if (!Array.isArray(rule.actions) || rule.actions.length === 0) return t("rule.rcAction", { op: "?" });
  for (const a of rule.actions) {
    if (!actionRepresentable(a)) {
      const op = typeof (a as Record<string, unknown>).op === "string"
        ? String((a as Record<string, unknown>).op)
        : "?";
      return t("rule.rcAction", { op });
    }
  }
  return null;
}

/**
 * Build the guided rule form into `box`, writing its result into `output` (the
 * rule JSON textarea). Built once (cached by `box`), like the IR / klima panels —
 * so it survives the SSE-driven re-renders unchanged. Returns a handle whose
 * `loadRule` reconstructs the form from a saved rule (the "Edit" round-trip).
 */
export function renderRuleForm(
  box: HTMLElement,
  output: HTMLTextAreaElement,
  vocab: RuleVocab,
  klima: Klima,
  devices: Record<string, DeviceInfo> = {},
): RuleFormHandle {
  const cached = FORM_HANDLES.get(box);
  if (cached !== undefined) return cached; // built once → re-renders return the same handle
  // Clear any partial build left by a prior throw so a retry can't duplicate nodes. The handle is
  // cached only at the very end (on success) — if a malformed runtime payload makes the build throw,
  // the form stays un-built and a later well-formed render rebuilds it cleanly.
  box.replaceChildren();
  // The shared device-name datalist (built once from the discovery snapshot). Every `node` input is a
  // combo box over it: pick a name → its node id lands in the field; an undiscovered id is still typable.
  box.appendChild(nodeDatalist(devices));

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
  hdr.textContent = t("rule.wizardTitle");
  hdr.style.cssText = "font-weight:bold;margin-bottom:0.3rem;";
  box.appendChild(hdr);

  // id / enabled / debounce / modes
  const idIn = mk(t("rule.id"), finp(t("rule.phRuleId"), 14));
  const enIn = document.createElement("input");
  enIn.type = "checkbox";
  enIn.checked = true;
  mk(t("rule.enabled"), enIn);
  const dbIn = finp("0", 4);
  dbIn.value = "0";
  mk(t("rule.debounce"), dbIn);
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
  const tType = mk(t("rule.trigger"), sel(vocab.trigger_types));
  const tFields = document.createElement("span");
  box.appendChild(tFields);
  let tRead: () => ReadObj = () => ({});
  // `seed` (a saved rule's trigger) pre-fills the just-built fields when reconstructing via loadRule;
  // the type-change listener rebuilds with NO seed (a fresh, empty field set).
  const buildTrigger = (seed?: Record<string, unknown>): void => {
    tFields.replaceChildren();
    const tt = tType.value;
    if (tt === "scene") {
      const node = nodeInput();
      const sid = finp("scene_id", 5);
      tFields.append(node, sid);
      if (seed !== undefined) {
        node.value = scalarToInput(seed.node);
        sid.value = scalarToInput(seed.scene_id);
      }
      tRead = () => {
        const n = parseNode(node.value);
        if (n === null) throw new Error(t("rule.errRequired", { field: "node" }));
        return { node: n, scene_id: num(sid.value, "scene_id") };
      };
    } else if (tt === "state") {
      const pe = predicateEditor(
        vocab,
        seed === undefined ? undefined : (seed as { node?: number; field: string; op: string; value: unknown }),
      );
      tFields.append(pe.el);
      tRead = () => pe.read();
    } else if (tt === "time") {
      const at = finp("HH:MM", 6);
      const days = daysPicker(seed?.days as number[] | null | undefined);
      tFields.append(at, days.el);
      if (seed !== undefined) at.value = scalarToInput(seed.at);
      tRead = () => {
        const a = at.value.trim();
        if (a === "") throw new Error(t("rule.errRequired", { field: "at" }));
        const o: ReadObj = { at: a };
        const d = days.read();
        if (d !== null) o.days = d;
        return o;
      };
    } else if (tt === "sun") {
      const ev = sel(vocab.sun_events);
      const off = finp(t("rule.phOffset"), 6);
      off.value = "0";
      const days = daysPicker(seed?.days as number[] | null | undefined);
      tFields.append(ev, off, days.el);
      if (seed !== undefined) {
        if (typeof seed.event === "string") ev.value = seed.event;
        if (seed.offset_min !== undefined) off.value = scalarToInput(seed.offset_min);
      }
      tRead = () => {
        const o: ReadObj = {
          event: ev.value,
          offset_min: off.value.trim() === "" ? 0 : num(off.value, "offset"),
        };
        const d = days.read();
        if (d !== null) o.days = d;
        return o;
      };
    } else if (tt === "presence") {
      const mac = finp("aa:bb:cc:dd:ee:ff", 17);
      const ev = sel(vocab.presence_events);
      tFields.append(mac, ev);
      if (seed !== undefined) {
        mac.value = scalarToInput(seed.mac);
        if (typeof seed.event === "string") ev.value = seed.event;
      }
      tRead = () => {
        const m = mac.value.trim();
        if (m === "") throw new Error(t("rule.errRequired", { field: "mac" }));
        return { mac: m, event: ev.value };
      };
    } else {
      const expr = finp("* * * * *", 14);
      tFields.append(expr);
      if (seed !== undefined) expr.value = scalarToInput(seed.expr);
      tRead = () => {
        const e = expr.value.trim();
        if (e === "") throw new Error(t("rule.errRequired", { field: "expr" }));
        return { expr: e };
      };
    }
  };
  tType.addEventListener("change", () => {
    buildTrigger();
  });
  buildTrigger();
  line();

  // conditions (0+)
  const condLbl = document.createElement("span");
  condLbl.textContent = `${t("rule.conditions")} `;
  condLbl.style.color = "#555";
  box.appendChild(condLbl);
  const condBox = document.createElement("span");
  box.appendChild(condBox);
  const conds: { read: () => Predicate | TimeWindow }[] = [];
  // Add one condition row (a state predicate or a time_window) with its own ✕ remove button.
  const addCondRow = (editor: { el: HTMLElement; read: () => Predicate | TimeWindow }): void => {
    const row = document.createElement("span");
    row.style.marginRight = "0.3rem";
    const entry = { read: editor.read };
    const rm = document.createElement("button");
    rm.type = "button";
    rm.textContent = "×";
    rm.title = t("rule.remove");
    rm.addEventListener("click", () => {
      condBox.removeChild(row);
      const i = conds.indexOf(entry);
      if (i >= 0) conds.splice(i, 1);
    });
    row.append(editor.el, rm);
    condBox.appendChild(row);
    conds.push(entry);
  };
  const addCondBtn = document.createElement("button");
  addCondBtn.type = "button";
  addCondBtn.textContent = t("rule.addCondition");
  addCondBtn.addEventListener("click", () => { addCondRow(predicateEditor(vocab)); });
  const addWindowBtn = document.createElement("button");
  addWindowBtn.type = "button";
  addWindowBtn.style.marginLeft = "0.3rem";
  addWindowBtn.textContent = t("rule.addWindow");
  addWindowBtn.addEventListener("click", () => { addCondRow(timeWindowEditor()); });
  box.append(addCondBtn, addWindowBtn);
  line();

  // actions (1+) — klima offered as a friendly preset when a klima.ir is loaded
  const actLbl = document.createElement("span");
  actLbl.textContent = `${t("rule.actions")} `;
  actLbl.style.color = "#555";
  box.appendChild(actLbl);
  const actBox = document.createElement("span");
  box.appendChild(actBox);
  const acts: { read: () => ReadObj }[] = [];
  const klimaModes = klima.power_on !== undefined ? Object.keys(klima.power_on).sort() : [];
  // `seed` (a saved action) pre-fills the row when reconstructing. A saved `ir` action reconstructs as
  // the generic `ir` editor (lossless), never as the `klima` preset — that preset is authoring-only.
  const addAction = (seed?: RuleAction): void => {
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
    // `s` seeds the per-op fields ONLY on the initial build; a manual op-type change rebuilds empty
    // (mirrors the trigger), so switching op never re-applies the loaded rule's stale values.
    const buildAct = (s?: RuleAction): void => {
      fields.replaceChildren();
      const k = op.value;
      if (k === "klima") {
        const mode = sel(klimaModes.concat(["off"]));
        const temp = sel([]);
        const fill = (): void => {
          temp.replaceChildren();
          temp.style.display = mode.value === "off" ? "none" : "";
          for (const c of klima.power_on?.[mode.value] ?? []) opt(temp, String(c), `${String(c)}°`);
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
        const btn = finp(t("rule.phButton"), 10);
        fields.append(file, btn);
        if (s !== undefined) {
          file.value = scalarToInput(s.file);
          btn.value = scalarToInput(s.button);
        }
        aRead = () => {
          const f = file.value.trim();
          const b = btn.value.trim();
          if (f === "" || b === "") throw new Error(t("rule.errRequired", { field: "file+button" }));
          return { op: "ir", file: f, button: b };
        };
      } else if (k === "switch" || k === "thermostat_power") {
        const node = nodeInput();
        const on = sel(["on", "off"]);
        fields.append(node, on);
        if (s !== undefined) {
          node.value = scalarToInput(s.node);
          on.value = s.on === false ? "off" : "on";
        }
        aRead = () => {
          const n = parseNode(node.value);
          if (n === null) throw new Error(t("rule.errRequired", { field: "node" }));
          return { op: k, node: n, on: on.value === "on" };
        };
      } else if (k === "level" || k === "cover") {
        const node = nodeInput();
        const value = finp(t("rule.phValue"), 5);
        fields.append(node, value);
        if (s !== undefined) {
          node.value = scalarToInput(s.node);
          value.value = scalarToInput(s.value);
        }
        aRead = () => {
          const n = parseNode(node.value);
          if (n === null) throw new Error(t("rule.errRequired", { field: "node" }));
          return { op: k, node: n, value: num(value.value, `${k} value`) };
        };
      } else {
        const node = nodeInput();
        const c = finp("°C", 5);
        fields.append(node, c);
        if (s !== undefined) {
          node.value = scalarToInput(s.node);
          c.value = scalarToInput(s.celsius);
        }
        aRead = () => {
          const n = parseNode(node.value);
          if (n === null) throw new Error(t("rule.errRequired", { field: "node" }));
          return { op: "thermostat", node: n, celsius: num(c.value, "celsius") };
        };
      }
    };
    op.addEventListener("change", () => {
      buildAct();
    });
    if (seed !== undefined && typeof seed.op === "string") op.value = seed.op;
    buildAct(seed);
    const entry = { read: () => aRead() };
    const rm = document.createElement("button");
    rm.type = "button";
    rm.textContent = "×";
    rm.title = t("rule.remove");
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
  addActBtn.textContent = t("rule.addAction");
  addActBtn.addEventListener("click", () => {
    addAction();
  });
  box.appendChild(addActBtn);
  addAction();
  line();

  // build → JSON (operator reviews, then "Save rule" validates server-side)
  const buildBtn = document.createElement("button");
  buildBtn.type = "button";
  buildBtn.textContent = t("rule.buildJson");
  const formStatus = document.createElement("span");
  formStatus.className = "status";
  formStatus.style.marginLeft = "0.5rem";
  buildBtn.addEventListener("click", () => {
    try {
      const id = idIn.value.trim();
      if (id === "") throw new Error(t("rule.errIdRequired"));
      const modes = vocab.modes.filter((m) => modeBoxes.get(m)?.checked === true);
      if (modes.length === 0) throw new Error(t("rule.errSelectMode"));
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
      formStatus.textContent = t("rule.built");
      formStatus.className = "status";
    } catch (e) {
      formStatus.textContent = `✗ ${e instanceof Error ? e.message : t("rule.errGeneric")}`;
      formStatus.className = "status err";
    }
  });
  box.append(buildBtn, formStatus);

  // ---- imperative handle (A3): reconstruct the form from a saved rule ----
  const setModes = (modes?: string[]): void => {
    const want = modes !== undefined ? new Set(modes) : null; // undefined → all (backend default)
    for (const [m, cb] of modeBoxes) cb.checked = want === null || want.has(m);
  };
  const clearConds = (): void => {
    condBox.replaceChildren();
    conds.length = 0;
  };
  const clearActs = (): void => {
    actBox.replaceChildren();
    acts.length = 0;
  };
  const setStatus = (text: string, isErr: boolean): void => {
    formStatus.textContent = text;
    formStatus.className = isErr ? "status err" : "status";
  };

  const reset = (): void => {
    idIn.value = "";
    enIn.checked = true;
    dbIn.value = "0";
    setModes(undefined);
    tType.value = vocab.trigger_types[0] ?? "scene";
    buildTrigger();
    clearConds();
    clearActs();
    addAction();
    buildBtn.disabled = false;
    setStatus("", false);
  };

  const loadRule = (rule: Rule): RuleFormLoadReport => {
    const reason = reconstructionBlocker(rule, vocab);
    if (reason !== null) {
      // raw-only: clear the wizard so it can't misrepresent the saved rule, and disable Build JSON so a
      // partial reconstruction can't overwrite it. The JSON textarea (already holding the rule) is the truth.
      reset();
      buildBtn.disabled = true;
      setStatus(`⚠ ${t("rule.cantReconstruct", { what: reason })}`, true);
      return { mode: "raw-only", reason };
    }
    idIn.value = rule.id;
    enIn.checked = rule.enabled;
    dbIn.value = String(rule.debounce);
    setModes(rule.modes);
    tType.value = rule.trigger.type;
    buildTrigger(rule.trigger);
    clearConds();
    for (const c of Array.isArray(rule.conditions) ? rule.conditions : []) {
      const o = c as Record<string, unknown>;
      if (o.type === "time_window") {
        addCondRow(timeWindowEditor(o as unknown as TimeWindow));
      } else {
        addCondRow(predicateEditor(vocab, o as { node?: number; field: string; op: string; value: unknown }));
      }
    }
    clearActs();
    for (const a of rule.actions) addAction(a);
    if (acts.length === 0) addAction(); // a saved rule always has ≥1 action, but never leave zero rows
    buildBtn.disabled = false;
    setStatus(t("rule.loadedToForm"), false);
    return { mode: "loaded" };
  };

  const handle: RuleFormHandle = { loadRule, reset };
  FORM_HANDLES.set(box, handle); // re-renders return this same handle (built once)
  box.dataset.built = "1";
  return handle;
}
