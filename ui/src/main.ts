import "./style.css";

import { apiUrl, fetchDiscovery, postControl, postName } from "./api/client";
import { renderActions } from "./controls";
import { LiveController } from "./live";
import { bindRow, bindSubRow } from "./registry";

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
  (tr, node, info) => {
    const ep = tr.dataset.ep;
    if (ep !== undefined) {
      bindSubRow(tr, node, Number(ep), postName); // multi-gang channel label
      return;
    }
    const cell = tr.querySelector<HTMLElement>(".actions");
    if (cell !== null) renderActions(cell, node, info, postControl);
    bindRow(tr, node, info, postName); // confirm + name/room save
  },
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
