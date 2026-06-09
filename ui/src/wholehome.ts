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
  /** Per-gang opt-outs of multi-gang switches: node id → the gang numbers opted out. */
  excludedEndpoints: ReadonlyMap<number, ReadonlySet<number>>;
  /** POST /api/whole-home {node, exclude} — or {node, exclude, ep} for one gang. Resolves true on success. */
  setExcluded: (node: number, excluded: boolean, ep?: number) => Promise<boolean>;
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

/** The gang numbers of a multi-gang switch, ascending; [] for single-gang. Mirrors the backend's
 *  gang universe (live endpoints ∪ labels ∪ flagged) so an existing per-gang opt-out stays visible
 *  and editable even when the live gang map is sparse (e.g. right after a restart). */
function gangsOf(info: DeviceInfo, flagged: ReadonlySet<number> | undefined): number[] {
  const eps = new Set(Object.keys(info.endpoints ?? {}).map(Number));
  for (const k of Object.keys(info.endpoint_names ?? {})) eps.add(Number(k));
  for (const ep of flagged ?? []) eps.add(ep);
  return eps.size > 1 ? [...eps].sort((a, b) => a - b) : [];
}

/** One toggle row: a checkbox (checked = INCLUDED in "all") + a label. Toggling persists immediately
 *  via `save`; on failure (or rejection) the checkbox reverts and the shared status reports it. */
function toggleRow(opts: {
  label: string;
  aria: string;
  included: boolean;
  gang?: boolean; // an indented per-gang row under its device heading
  save: (excluded: boolean) => Promise<boolean>;
  status: HTMLElement;
}): HTMLElement {
  const row = document.createElement("label");
  row.className = opts.gang === true ? "wholehome-row wholehome-gang" : "wholehome-row";

  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = opts.included;
  checkbox.setAttribute("aria-label", `${t("wholeHome.include")}: ${opts.aria}`);

  const name = document.createElement("span");
  name.className = "wholehome-name";
  name.textContent = opts.label;

  row.append(checkbox, name);
  checkbox.addEventListener("change", () => {
    const excluded = !checkbox.checked; // unchecked = opted OUT of the sweeps
    checkbox.disabled = true;
    opts.status.textContent = "…";
    opts.status.className = "status";
    void (async () => {
      // A rejection (a future save that throws) is treated as failure, so the row never gets stuck
      // disabled at "…" — the wired client call resolves {ok:false} rather than rejecting.
      const ok = await opts.save(excluded).catch(() => false);
      if (ok) {
        opts.status.textContent = t("wholeHome.saved");
        opts.status.className = "status ok";
      } else {
        checkbox.checked = !checkbox.checked; // revert the optimistic toggle
        opts.status.textContent = t("wholeHome.saveError");
        opts.status.className = "status err";
      }
      checkbox.disabled = false;
    })();
  });
  return row;
}

/** The rows for one device: a single toggle, or — for a multi-gang switch — a heading plus one
 *  toggle per gang (labelled from endpoint_names, falling back to a numbered gang), each gang
 *  independently in/out of "all". A node-level opt-out shows every gang unchecked; re-checking a
 *  gang then writes per-gang flags (the server demotes the node flag, preserving the other gangs). */
function deviceRows(node: string, info: DeviceInfo, deps: WholeHomeConfigDeps, status: HTMLElement): HTMLElement[] {
  const nodeNum = Number(node);
  const label = deviceName(node, info);
  const epsOut = deps.excludedEndpoints.get(nodeNum);
  const gangs = gangsOf(info, epsOut);
  if (gangs.length === 0) {
    return [toggleRow({
      label,
      aria: label,
      included: !deps.excluded.has(nodeNum),
      save: (excluded) => deps.setExcluded(nodeNum, excluded),
      status,
    })];
  }
  const heading = document.createElement("div");
  heading.className = "wholehome-device";
  heading.textContent = label;
  return [heading, ...gangs.map((ep) => {
    const gangLabel = info.endpoint_names?.[String(ep)] ?? t("wholeHome.gang", { n: ep });
    return toggleRow({
      label: gangLabel,
      aria: `${label} – ${gangLabel}`,
      included: !(deps.excluded.has(nodeNum) || epsOut?.has(ep) === true),
      gang: true,
      save: (excluded) => deps.setExcluded(nodeNum, excluded, ep),
      status,
    });
  })];
}

/**
 * Open the admin-only "whole-home" configuration panel: which lights / blinds (and which single
 * gangs of a multi-gang switch) the house-wide "all" buttons act on. A centred modal (reusing the
 * .modal-* chrome) grouped by device type, each row a checkbox that persists immediately. XSS-safe
 * (textContent + DOM, no innerHTML). Returns a `close()` handle (used by tests); closes on the Done
 * button, a backdrop click, or Escape.
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
    for (const [node, info] of group) list.append(...deviceRows(node, info, deps, status));
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
