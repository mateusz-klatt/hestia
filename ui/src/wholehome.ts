import type { DeviceInfo } from "./api/types";
import { t } from "./i18n";
import { typeLabel } from "./render/format";

// The device types the house-wide "all lights / all blinds" sweeps act on — the only ones worth
// listing here. Mirrors the backend's _SCENE_TARGETS (hestia/web.py).
const SWEEP_TYPES = ["light", "blind"] as const;

export interface WholeHomeConfigDeps {
  /** Snapshot of the current devices (taken when the panel opens). */
  devices: Record<string, DeviceInfo>;
  /** The node ids currently opted out of "all" (from GET /api/whole-home — kept off DeviceInfo so the
   *  device snapshot stays wire-stable for native clients). */
  excluded: ReadonlySet<number>;
  /** POST /api/whole-home {node, exclude}. Resolves true on success. */
  setExcluded: (node: number, excluded: boolean) => Promise<boolean>;
  onClosed?: () => void;
}

/** A device's display name: its registry label, else `type #node` — same rule as the room cards. */
function deviceName(node: string, info: DeviceInfo): string {
  const name = info.name?.trim();
  return name !== undefined && name !== "" ? name : `${typeLabel(info.type) || "?"} #${node}`;
}

/** Numeric node order so #2 precedes #10. */
function byNode(a: [string, DeviceInfo], b: [string, DeviceInfo]): number {
  return Number(a[0]) - Number(b[0]);
}

/** One device row: a checkbox (checked = INCLUDED in "all") + the device name. Toggling persists the
 *  opt-out immediately via `setExcluded`; on failure the checkbox reverts and the status reports it. */
function buildRow(node: string, info: DeviceInfo, deps: WholeHomeConfigDeps, status: HTMLElement): HTMLElement {
  const row = document.createElement("label");
  row.className = "wholehome-row";

  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = !deps.excluded.has(Number(node)); // not opted out → included (checked)
  const labelText = deviceName(node, info);
  checkbox.setAttribute("aria-label", `${t("wholeHome.include")}: ${labelText}`);

  const name = document.createElement("span");
  name.className = "wholehome-name";
  name.textContent = labelText;

  row.append(checkbox, name);
  checkbox.addEventListener("change", () => {
    const excluded = !checkbox.checked; // unchecked = opted OUT of the sweeps
    checkbox.disabled = true;
    status.textContent = "…";
    status.className = "status";
    void (async () => {
      // A rejection (a future setExcluded that throws) is treated as failure, so the row never
      // gets stuck disabled at "…" — the wired postName resolves {ok:false} rather than rejecting.
      const ok = await deps.setExcluded(Number(node), excluded).catch(() => false);
      if (ok) {
        status.textContent = t("wholeHome.saved");
        status.className = "status ok";
      } else {
        checkbox.checked = !checkbox.checked; // revert the optimistic toggle
        status.textContent = t("wholeHome.saveError");
        status.className = "status err";
      }
      checkbox.disabled = false;
    })();
  });
  return row;
}

/**
 * Open the admin-only "whole-home" configuration panel: which lights / blinds the house-wide
 * "all" buttons act on. A centred modal (reusing the .modal-* chrome) grouped by device type, each
 * row a checkbox that persists immediately. XSS-safe (textContent + DOM, no innerHTML). Returns a
 * `close()` handle (used by tests); closes on the Done button, a backdrop click, or Escape.
 */
export function openWholeHomeConfig(deps: WholeHomeConfigDeps): () => void {
  const previousFocus = document.activeElement; // restored on close so focus returns to the trigger

  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";

  const card = document.createElement("div");
  card.className = "modal-card wholehome-card";
  card.setAttribute("role", "dialog");
  card.setAttribute("aria-modal", "true");
  card.setAttribute("aria-label", t("wholeHome.title"));

  const heading = document.createElement("h3");
  heading.textContent = t("wholeHome.title");
  const desc = document.createElement("p");
  desc.className = "wholehome-desc";
  desc.textContent = t("wholeHome.desc");

  const status = document.createElement("span");
  status.className = "status";
  status.setAttribute("aria-live", "polite");

  const list = document.createElement("div");
  list.className = "wholehome-list";
  const entries = Object.entries(deps.devices);
  let any = false;
  for (const type of SWEEP_TYPES) {
    const group = entries.filter(([, info]) => info.type === type).sort(byNode);
    if (group.length === 0) continue;
    any = true;
    const groupHeading = document.createElement("h4");
    groupHeading.className = "wholehome-group";
    groupHeading.textContent = t(type === "light" ? "wholeHome.lights" : "wholeHome.blinds");
    list.appendChild(groupHeading);
    for (const [node, info] of group) list.appendChild(buildRow(node, info, deps, status));
  }
  if (!any) {
    const empty = document.createElement("p");
    empty.className = "room-placeholder";
    empty.textContent = t("wholeHome.empty");
    list.appendChild(empty);
  }

  const actions = document.createElement("div");
  actions.className = "modal-actions";
  const done = document.createElement("button");
  done.type = "button";
  done.className = "modal-cancel"; // changes already saved per-toggle → this just closes
  done.textContent = t("wholeHome.done");
  actions.appendChild(done);

  let closed = false;
  const onKey = (event: KeyboardEvent): void => {
    if (event.key === "Escape") close();
  };
  function close(): void {
    if (closed) return;
    closed = true;
    document.removeEventListener("keydown", onKey);
    overlay.remove();
    if (previousFocus instanceof HTMLElement) previousFocus.focus();
    deps.onClosed?.();
  }
  done.addEventListener("click", close);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) close(); // backdrop click (not inside the card)
  });
  document.addEventListener("keydown", onKey);

  card.append(heading, desc, list, actions, status);
  overlay.appendChild(card);
  document.body.appendChild(overlay);
  done.focus();
  return close;
}
