import type { AuditEvent, DeviceInfo } from "./api/types";
import { t } from "./i18n";

export type FetchAudit = () => Promise<AuditEvent[] | null>;

/** Resolve a device-row's `target` (a node id) to a friendly "name · room" at display time; returns
 *  the raw target for non-device rows or unknown nodes. Read-time/UI-side so renames fix old rows. */
export type ResolveAuditTarget = (target: string | null, action: string) => string | null;

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

function eventRow(event: AuditEvent, resolveTarget?: ResolveAuditTarget): HTMLLIElement {
  const row = document.createElement("li");
  row.className = "audit-row";
  row.dataset.id = String(event.id);

  const ts = document.createElement("time");
  ts.className = "audit-ts";
  ts.textContent = new Date(event.ts * 1000).toLocaleString();

  row.append(
    ts,
    span("audit-icon", actorIcon(event.actor)),
    span("audit-actor", event.actor),
    span("audit-action", event.action),
  );
  const target = resolveTarget ? resolveTarget(event.target, event.action) : event.target;
  appendOptional(row, "audit-target", target);
  appendOptional(row, "audit-detail", event.detail);
  appendOptional(row, "audit-result", event.result);
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
 */
export function renderAuditFeed(
  container: HTMLElement,
  fetchAudit: FetchAudit,
  resolveTarget?: ResolveAuditTarget,
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
      const newestFirst = [...events].sort((a, b) => b.ts - a.ts);
      for (const event of newestFirst) list.appendChild(eventRow(event, resolveTarget));
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
