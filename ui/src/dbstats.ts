import type { DbStats } from "./api/types";
import { t } from "./i18n";

export type FetchDbStats = () => Promise<DbStats | null>;

export interface DbStatsPanel {
  refresh: () => Promise<void>;
}

const panels = new WeakMap<HTMLElement, DbStatsPanel>();

function formatBytes(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${String(bytes)} B`;
}

function statsText(stats: DbStats): string {
  const counts = Object.entries(stats.tables)
    .map(([table, count]) => `${table} ${String(count)}`)
    .join(" · ");
  const size = formatBytes(stats.file_bytes);
  return counts.length === 0 ? `💾 ${size}` : `💾 ${size} · ${counts}`;
}

/** Build the DB stats controls once, then refresh only the text row. */
export function renderDbStats(container: HTMLElement, fetchDbStats: FetchDbStats): DbStatsPanel {
  const existing = panels.get(container);
  if (existing !== undefined) return existing;

  container.dataset.built = "1";

  const head = document.createElement("div");
  head.className = "dbstats-head";

  const title = document.createElement("h3");
  title.textContent = t("dbstats.title");

  const button = document.createElement("button");
  button.type = "button";
  button.textContent = t("audit.refresh");

  const line = document.createElement("p");
  line.className = "dbstats-line";
  line.textContent = "💾 —";

  const refresh = async (): Promise<void> => {
    try {
      const stats = await fetchDbStats();
      line.textContent = stats === null ? "💾 —" : statsText(stats);
    } catch {
      line.textContent = "💾 —";
    }
  };

  button.addEventListener("click", () => {
    void refresh();
  });

  head.append(title, button);
  container.replaceChildren(head, line);

  const panel = { refresh };
  panels.set(container, panel);
  return panel;
}
