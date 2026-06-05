import type { AuditEvent, DeviceInfo } from "./api/types";
import { t } from "./i18n";
import type { MessageKey } from "./i18n/locales/en";
import { fmtTemp } from "./render/format";

export type FetchAudit = () => Promise<AuditEvent[] | null>;

// Audit actions whose `target` is a device NODE id (worth resolving to a name). Everything else —
// `ir` (a file path), `automation_set`/`automation_delete` (a rule id), `login`, `graduate` — keeps
// its raw target.
const DEVICE_ACTIONS: ReadonlySet<string> = new Set([
  "switch", "level", "cover", "thermostat", "thermostat_power",
  "setpoint", "thermostat_on", "door", "endpoints", "scene", "motion",
  "name", // /api/name (rename) targets a node id too
]);

/** Map a device-action audit target (a bare-integer node id) to "name · room" using the current
 *  device map; falls back to the raw target for a non-device action, a non-integer target, or an
 *  unknown/removed node. Pure (testable) — `main.ts` passes the live discovery devices. */
export function formatAuditTarget(
  target: string | null,
  action: string,
  devices: Record<string, DeviceInfo>,
): string | null {
  if (target === null || !DEVICE_ACTIONS.has(action) || !/^\d+$/.test(target)) return target;
  const info = devices[target];
  if (info === undefined) return target;
  const name = info.name?.trim();
  const label = name !== undefined && name !== "" ? name : `${info.type} #${target}`;
  const room = info.room?.trim();
  return room !== undefined && room !== "" ? `${label} · ${room}` : label;
}

// ---- display-time humanization (Codex + Copilot consult) ------------------
// The backend stores raw technical facts (actor codes, action verbs, JSON / Python-repr details);
// the feed is made readable HERE, at render time, so renames + locale changes also fix old rows and
// nothing is lost — an unknown actor / action / detail / result always falls back to its raw text.

/** Localise the reserved actors; usernames + unknown actors stay raw. `automation:<rule_id>` keeps
 *  the rule id (it identifies WHICH rule fired) behind a translated "Automation:" prefix. */
export function formatAuditActor(actor: string): string {
  if (actor.startsWith("automation:")) {
    return t("audit.actor.automation", { id: actor.slice("automation:".length) });
  }
  if (actor === "device") return t("audit.actor.device");
  if (actor === "system") return t("audit.actor.system");
  if (actor === "anonymous") return t("audit.actor.anonymous");
  return actor;
}

const ACTION_KEYS: Record<string, MessageKey> = {
  switch: "audit.action.switch",
  level: "audit.action.level",
  cover: "audit.action.cover",
  thermostat: "audit.action.thermostat",
  thermostat_power: "audit.action.thermostat_power",
  ir: "audit.action.ir",
  name: "audit.action.name",
  scene: "audit.action.scene",
  graduate: "audit.action.graduate",
  automation_set: "audit.action.automation_set",
  automation_delete: "audit.action.automation_delete",
  settings: "audit.action.settings",
  room_icon: "audit.action.room_icon",
  login: "audit.action.login",
  door: "audit.action.door",
  endpoints: "audit.action.endpoints",
  thermostat_on: "audit.action.thermostat_on",
  setpoint: "audit.action.setpoint",
  password: "audit.action.password",
  user_add: "audit.action.user_add",
  user_role: "audit.action.user_role",
  user_enable: "audit.action.user_enable",
  user_disable: "audit.action.user_disable",
  user_password: "audit.action.user_password",
};

/** Localise a known audit action verb; an unknown/new action falls back to its raw code. */
export function formatAuditAction(action: string): string {
  const key = ACTION_KEYS[action];
  return key !== undefined ? t(key) : action;
}

const RESULT_KEYS: Record<string, MessageKey> = {
  ok: "audit.result.ok",
  invalid: "audit.result.invalid",
  reported: "audit.result.reported",
  fired: "audit.result.fired",
};

/** Localise the fixed result enums; `error: <msg>` and `<sent>/<total>` (technical) stay raw. */
export function formatAuditResult(result: string | null): string | null {
  if (result === null) return result;
  const key = RESULT_KEYS[result];
  return key !== undefined ? t(key) : result;
}

const UNPARSED = Symbol("unparsed");

/** Decode an audit `detail` string: control ops are JSON (`{"endpoint":1,"on":true}`); observed
 *  transitions are Python `str()` (`True` / `{'1': True, '2': False}` / `21`). Returns UNPARSED for a
 *  bare word (e.g. door `open`/`closed`) so the caller can handle or pass it through raw. */
function decodeDetail(detail: string): unknown {
  try {
    return JSON.parse(detail);
  } catch {
    /* not JSON — try Python repr below */
  }
  const py = detail
    .replace(/\bTrue\b/g, "true")
    .replace(/\bFalse\b/g, "false")
    .replace(/\bNone\b/g, "null")
    .replace(/'/g, '"');
  try {
    return JSON.parse(py);
  } catch {
    return UNPARSED;
  }
}

/** Safe text for an unknown decoded value: primitives stringify directly; a non-primitive (never
 *  expected in our detail shapes) is JSON-encoded rather than rendered as "[object Object]". */
function scalarText(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

function boolLabel(value: unknown): string {
  return value === true || value === "true" || value === "True" ? t("ctl.on") : t("ctl.off");
}

/** Render a numeric detail field only when it is a finite number; a malformed value falls back to
 *  its raw text instead of leaking `NaN%` / `NaN°` (keeps the lossless guarantee). */
function numericField(value: unknown, render: (n: number) => string): string {
  const n = Number(value);
  return Number.isFinite(n) ? render(n) : scalarText(value);
}

/** A 2-gang channel's friendly name (`Lewy`), falling back to `#1` when the channel is unnamed. */
function channelLabel(endpoint: string, epNames?: Record<string, string>): string {
  const name = epNames?.[endpoint]?.trim();
  return name !== undefined && name !== "" ? name : `#${endpoint}`;
}

function endpointNamesFor(
  target: string | null,
  devices: Record<string, DeviceInfo>,
): Record<string, string> | undefined {
  if (target === null || !/^\d+$/.test(target)) return undefined;
  return devices[target]?.endpoint_names;
}

/** Humanise a scalar detail value by the action it belongs to (setpoint → temperature, level/cover →
 *  percent, booleans → On/Off, door words → open/closed); unknown shapes return raw text. */
function humanizeScalar(value: unknown, action: string): string {
  if (typeof value === "boolean") return boolLabel(value);
  if (typeof value === "number") {
    if (action === "setpoint" || action === "thermostat") return fmtTemp(value);
    if (action === "level" || action === "cover") return `${String(Math.round(value))}%`;
    return String(value);
  }
  if (value === "open") return t("state.open");
  if (value === "closed") return t("state.closed");
  return scalarText(value);
}

const KNOWN_OP_FIELDS: ReadonlySet<string> = new Set([
  "endpoint", "on", "value", "level", "celsius", "temp", "mode",
]);

/** Humanise an object detail. A pure channel map (`{"1": true, "2": false}`) becomes
 *  `Lewy: On · Prawy: Off`; a recognised control-op payload becomes a compact summary (redundant
 *  `node`/`op` dropped). Returns null for an unfamiliar shape so the caller shows the raw detail. */
function humanizeObject(
  obj: Record<string, unknown>,
  action: string,
  epNames?: Record<string, string>,
): string | null {
  const keys = Object.keys(obj).filter((k) => k !== "node" && k !== "op");
  if (keys.length === 0) return null;
  if (keys.every((k) => /^\d+$/.test(k))) {
    return keys
      .sort((a, b) => Number(a) - Number(b))
      .map((k) => `${channelLabel(k, epNames)}: ${boolLabel(obj[k])}`)
      .join(" · ");
  }
  if (!keys.every((k) => KNOWN_OP_FIELDS.has(k))) return null; // unfamiliar → raw fallback (lossless)
  const parts: string[] = [];
  const endpoint = obj["endpoint"];
  if (endpoint !== undefined && obj["on"] !== undefined) {
    parts.push(`${channelLabel(scalarText(endpoint), epNames)}: ${boolLabel(obj["on"])}`);
  } else {
    if (endpoint !== undefined) parts.push(channelLabel(scalarText(endpoint), epNames));
    if (obj["on"] !== undefined) parts.push(boolLabel(obj["on"]));
  }
  if (obj["value"] !== undefined) parts.push(humanizeScalar(obj["value"], action));
  if (obj["level"] !== undefined) parts.push(numericField(obj["level"], (n) => `${String(Math.round(n))}%`));
  if (obj["celsius"] !== undefined) parts.push(numericField(obj["celsius"], fmtTemp));
  if (obj["temp"] !== undefined) parts.push(numericField(obj["temp"], fmtTemp));
  if (obj["mode"] !== undefined) parts.push(scalarText(obj["mode"]));
  return parts.length > 0 ? parts.join(" · ") : null;
}

/** Humanise the audit `detail` at display time: substitute machine values (true/false → On/Off,
 *  endpoint number → channel name, setpoint → temperature) inside the existing structure. Anything
 *  unrecognised is shown raw, so no information is lost. Pure — testable in isolation. */
export function formatAuditDetail(
  detail: string | null,
  action: string,
  target: string | null,
  devices: Record<string, DeviceInfo>,
): string | null {
  if (detail === null || detail === "") return detail;
  const epNames = endpointNamesFor(target, devices);
  const value = decodeDetail(detail);
  if (value === UNPARSED) {
    if (detail === "open") return t("state.open");
    if (detail === "closed") return t("state.closed");
    return detail;
  }
  if (value === null) return detail; // a literal None/null — show the raw text, not "null"
  if (typeof value === "object" && !Array.isArray(value)) {
    return humanizeObject(value as Record<string, unknown>, action, epNames) ?? detail;
  }
  return humanizeScalar(value, action);
}

export interface AuditFeed {
  refresh: () => Promise<void>;
}

const feeds = new WeakMap<HTMLElement, AuditFeed>();

function actorIcon(actor: string): string {
  if (actor.startsWith("automation:")) return "🤖";
  if (actor === "device") return "📟";
  if (actor === "system") return "⚙";
  return "👤";
}

function span(className: string, text: string): HTMLSpanElement {
  const node = document.createElement("span");
  node.className = className;
  node.textContent = text;
  return node;
}

function appendOptional(row: HTMLElement, className: string, value: string | null): void {
  if (value === null || value === "") return;
  row.appendChild(span(className, value));
}

function eventRow(event: AuditEvent, devices: Record<string, DeviceInfo>): HTMLLIElement {
  const row = document.createElement("li");
  row.className = "audit-row";
  row.dataset.id = String(event.id);

  const ts = document.createElement("time");
  ts.className = "audit-ts";
  ts.textContent = new Date(event.ts * 1000).toLocaleString();

  row.append(
    ts,
    span("audit-icon", actorIcon(event.actor)),
    span("audit-actor", formatAuditActor(event.actor)),
    span("audit-action", formatAuditAction(event.action)),
  );
  appendOptional(row, "audit-target", formatAuditTarget(event.target, event.action, devices));
  appendOptional(row, "audit-detail", formatAuditDetail(event.detail, event.action, event.target, devices));
  appendOptional(row, "audit-result", formatAuditResult(event.result));
  return row;
}

function emptyRow(): HTMLLIElement {
  const row = document.createElement("li");
  row.className = "audit-empty";
  row.textContent = t("audit.empty");
  return row;
}

/**
 * Build the audit-log controls once, then refresh only the event list. Failures
 * use the empty state so the Advanced view remains usable while auth/network recovers.
 * `getDevices` supplies the live discovery map so targets + details resolve to friendly names.
 */
export function renderAuditFeed(
  container: HTMLElement,
  fetchAudit: FetchAudit,
  getDevices?: () => Record<string, DeviceInfo>,
): AuditFeed {
  const existing = feeds.get(container);
  if (existing !== undefined) return existing;

  container.dataset.built = "1";

  const head = document.createElement("div");
  head.className = "audit-head";

  const title = document.createElement("h3");
  title.textContent = t("audit.title");

  const button = document.createElement("button");
  button.type = "button";
  button.textContent = t("audit.refresh");

  const list = document.createElement("ol");
  list.className = "audit-list";

  const refresh = async (): Promise<void> => {
    try {
      const events = await fetchAudit();
      list.replaceChildren();
      if (events === null || events.length === 0) {
        list.appendChild(emptyRow());
        return;
      }
      const devices = getDevices ? getDevices() : {};
      const newestFirst = [...events].sort((a, b) => b.ts - a.ts);
      for (const event of newestFirst) list.appendChild(eventRow(event, devices));
    } catch {
      list.replaceChildren(emptyRow());
    }
  };

  button.addEventListener("click", () => {
    void refresh();
  });

  head.append(title, button);
  container.replaceChildren(head, list);

  const feed = { refresh };
  feeds.set(container, feed);
  return feed;
}
