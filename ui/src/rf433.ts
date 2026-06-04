import type { Rf433Device } from "./api/types";
import { t } from "./i18n";

export type FetchRf433 = () => Promise<Rf433Device[] | null>;

export interface Rf433Feed {
  refresh: () => Promise<void>;
}

const feeds = new WeakMap<HTMLElement, Rf433Feed>();

function span(className: string, text: string): HTMLSpanElement {
  const node = document.createElement("span");
  node.className = className;
  node.textContent = text;
  return node;
}

/** The decoded fields as compact `key=value` text — what the operator reads to identify a device. */
function fieldsText(device: Rf433Device): string {
  return Object.entries(device.fields)
    .map(([key, value]) => `${key}=${String(value)}`)
    .join("  ");
}

function deviceRow(device: Rf433Device): HTMLLIElement {
  const row = document.createElement("li");
  row.className = "rf433-row";

  const ts = document.createElement("time");
  ts.className = "rf433-ts";
  ts.textContent = new Date(device.last_seen * 1000).toLocaleString();

  row.append(
    span("rf433-key", device.key),
    span("rf433-count", `×${String(device.count)}`),
    ts,
    span("rf433-fields", fieldsText(device)),
  );
  return row;
}

function emptyRow(): HTMLLIElement {
  const row = document.createElement("li");
  row.className = "rf433-empty";
  row.textContent = t("rf433.empty");
  return row;
}

/**
 * Build the 433-discovery controls once, then refresh only the device list. Lists every 433 MHz
 * device hestia has decoded (model+id+channel, hit count, last-seen, last decoded fields) so a new
 * device — a weather station, a garage-door remote — can be identified and then defined. Failures
 * fall back to the empty state so the Advanced view stays usable.
 */
export function renderRf433(container: HTMLElement, fetchRf433: FetchRf433): Rf433Feed {
  const existing = feeds.get(container);
  if (existing !== undefined) return existing;

  const head = document.createElement("div");
  head.className = "rf433-head";

  const title = document.createElement("h3");
  title.textContent = t("rf433.title");

  const button = document.createElement("button");
  button.type = "button";
  button.textContent = t("rf433.refresh");

  const list = document.createElement("ol");
  list.className = "rf433-list";

  const refresh = async (): Promise<void> => {
    try {
      const devices = await fetchRf433();
      list.replaceChildren();
      if (devices === null || devices.length === 0) {
        list.appendChild(emptyRow());
        return;
      }
      const newestFirst = [...devices].sort((a, b) => b.last_seen - a.last_seen);
      for (const device of newestFirst) list.appendChild(deviceRow(device));
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
