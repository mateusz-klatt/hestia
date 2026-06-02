import "./style.css";

import { fetchDiscovery } from "./api/client";
import { renderDiscovery } from "./render/devices";

function el(id: string): HTMLElement {
  const node = document.getElementById(id);
  if (node === null) throw new Error(`Missing #${id}`);
  return node;
}

const dom = {
  hdrText: el("hdr-text"),
  crib: el("g-crib"),
  outdoor: el("g-outdoor"),
  rows: el("rows"),
  status: el("status"),
  refresh: el("refresh"),
};

async function load(): Promise<void> {
  const data = await fetchDiscovery();
  if (data === null) {
    dom.status.textContent = "could not load /api/discovery";
    dom.status.hidden = false;
    return;
  }
  dom.status.hidden = true;
  renderDiscovery(
    { hdrText: dom.hdrText, crib: dom.crib, outdoor: dom.outdoor, rows: dom.rows },
    data,
  );
}

dom.refresh.addEventListener("click", () => {
  void load();
});

void load();
