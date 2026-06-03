import type { AuditEvent } from "./api/types";
import { t } from "./i18n";

export type FetchAudit = () => Promise<AuditEvent[] | null>;

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

function eventRow(event: AuditEvent): HTMLLIElement {
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
  appendOptional(row, "audit-target", event.target);
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
export function renderAuditFeed(container: HTMLElement, fetchAudit: FetchAudit): AuditFeed {
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
      for (const event of newestFirst) list.appendChild(eventRow(event));
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
