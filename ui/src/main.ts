import "./style.css";

import { apiUrl, fetchDiscovery } from "./api/client";
import { LiveController } from "./live";

function el(id: string): HTMLElement {
  const node = document.getElementById(id);
  if (node === null) throw new Error(`Missing #${id}`);
  return node;
}

const live = new LiveController(
  {
    hdrText: el("hdr-text"),
    crib: el("g-crib"),
    outdoor: el("g-outdoor"),
    rows: el("rows"),
    conn: el("conn"),
    status: el("status"),
  },
  fetchDiscovery,
);

el("refresh").addEventListener("click", () => {
  void live.refresh();
});

// Server-Sent Events: live state / globals patches + discovery deltas. The
// browser auto-reconnects on drop; `open` re-syncs the full snapshot.
const events = new EventSource(apiUrl("events"));
events.addEventListener("open", () => {
  live.setConnected(true);
  void live.refresh();
});
events.addEventListener("error", () => {
  live.setConnected(false);
});
events.addEventListener("message", (event) => {
  live.handleMessage(String(event.data));
});

// Once a second, refresh the relative "last seen" times + the lingering glow.
setInterval(() => {
  live.tick();
}, 1000);

void live.refresh();
