import type { DeviceInfo, Discovery, Globals, LiveEvent, Scene } from "./api/types";
import { renderDeviceRows, renderGlobals, summaryText } from "./render/devices";
import { fmtTemp, stateStr } from "./render/format";

/** How long a row stays brightly highlighted after activity. */
const HIGHLIGHT_MS = 2200;
/** How long the subtle "recent" glow + fresh "last seen" colour lingers. */
const RECENT_MS = 90_000;
/** How long a scene-press badge shows before it clears. */
const SCENE_MS = 4000;

/** The DOM the live controller writes into (queried once in `main.ts`). */
export interface LiveView {
  hdrText: HTMLElement;
  crib: HTMLElement;
  outdoor: HTMLElement;
  rows: HTMLElement;
  conn: HTMLElement;
  status: HTMLElement;
}

type Refetch = () => Promise<Discovery | null>;

/** Per-row hook run after each rebuild — wires interactive controls onto a row. */
export type RowDecorator = (tr: HTMLTableRowElement, node: number, info: DeviceInfo) => void;

/** Hook run with the full snapshot after each successful render — for panels
 *  built from the whole payload (IR buttons, klima) that live outside the table. */
export type RenderHook = (data: Discovery) => void;

function attr(value: string): string {
  // node ids / endpoint ids are numeric strings from the contract; quote for
  // the attribute selector regardless so a stray value can't break the query.
  return value.replace(/["\\]/g, "\\$&");
}

/**
 * The live data layer for `/ui`: a one-shot snapshot render plus incremental
 * SSE patches. The key invariant is COALESCING — while a full snapshot refresh
 * is in flight, state/globals deltas are queued and replayed against the
 * freshly-built rows afterwards, so a slightly-stale snapshot can never roll
 * back a newer value that arrived mid-refresh.
 *
 * On top of the data layer it drives the heatmap: every `activity` event flashes
 * the node's row (and a scene press shows a transient badge), and a 1 Hz tick
 * renders each row's relative "last seen" time + a lingering "recent" glow.
 */
export class LiveController {
  private readonly view: LiveView;
  private readonly refetch: Refetch;
  private readonly infoByNode = new Map<number, DeviceInfo>();
  private refreshing = false;
  private refreshAgain = false;
  private readonly pendingState = new Map<number, Partial<DeviceInfo>>();
  private pendingGlobals: Partial<Globals> | null = null;
  private readonly lastActiveByNode = new Map<number, number>();
  private readonly flashTimers = new Map<number | string, ReturnType<typeof setTimeout>>();
  private readonly sceneTimers = new Map<number, ReturnType<typeof setTimeout>>();
  private readonly pendingFlash = new Set<number>();
  private readonly pendingScene = new Map<number, Scene>();
  private readonly decorate: RowDecorator | undefined;
  private readonly onRender: RenderHook | undefined;

  constructor(view: LiveView, refetch: Refetch, decorate?: RowDecorator, onRender?: RenderHook) {
    this.view = view;
    this.refetch = refetch;
    this.decorate = decorate;
    this.onRender = onRender;
  }

  /** Parse + dispatch one raw SSE message; malformed JSON is silently ignored. */
  handleMessage(raw: string): void {
    let msg: LiveEvent;
    try {
      msg = JSON.parse(raw) as LiveEvent;
    } catch {
      return;
    }
    switch (msg.type) {
      case "state":
        this.applyState(msg.node, msg.fields);
        break;
      case "globals":
        this.applyGlobals(msg.fields);
        break;
      case "discovery_changed":
        void this.refresh();
        break;
      case "activity":
        this.flash(msg.node);
        if (msg.scene !== undefined) this.flashScene(msg.node, msg.scene);
        break;
    }
  }

  setConnected(connected: boolean): void {
    this.view.conn.textContent = connected ? "" : "(reconnecting…)";
  }

  /** Fetch the full snapshot and rebuild the view; coalesces overlapping calls. */
  async refresh(): Promise<void> {
    // Don't rebuild while the operator is interacting with a row — the rebuild
    // replaces every <input>/<button> and would erase an edit-in-progress or wipe
    // the "saved" status of a just-clicked Save/confirm (and a focused Save button
    // protects an unsaved sibling field too). Scoped to the table, so the header
    // Refresh button still works (unlike the legacy page-wide guard).
    const active = document.activeElement;
    if (
      (active instanceof HTMLInputElement || active instanceof HTMLButtonElement) &&
      this.view.rows.contains(active)
    ) {
      return;
    }
    if (this.refreshing) {
      this.refreshAgain = true; // a refresh landed mid-rebuild → run once more after
      return;
    }
    this.refreshing = true;
    const data = await this.refetch(); // deltas arriving now queue (refreshing === true)
    if (data === null) {
      this.view.status.textContent = "could not load /api/discovery";
      this.view.status.hidden = false;
    } else {
      this.view.status.hidden = true;
      this.render(data);
    }
    this.refreshing = false; // so the drained deltas below apply directly
    this.drainPending();
    if (this.refreshAgain) {
      this.refreshAgain = false; // consume the coalesced request, then run once more
      void this.refresh();
    }
  }

  private render(data: Discovery): void {
    this.view.hdrText.textContent = summaryText(data.summary);
    renderGlobals(this.view.crib, this.view.outdoor, data.globals);
    renderDeviceRows(this.view.rows, data.devices);
    this.infoByNode.clear();
    for (const [node, info] of Object.entries(data.devices)) {
      this.infoByNode.set(Number(node), info);
    }
    // Decorate every row (node rows AND multi-gang sub-rows) so the decorator
    // can wire controls + name/room on node rows and the ep-label on sub-rows;
    // reapply the activity highlight only to node rows.
    for (const tr of this.view.rows.querySelectorAll<HTMLTableRowElement>("tr[data-node]")) {
      const raw = tr.dataset.node;
      if (raw === undefined) continue;
      const node = Number(raw);
      const info = this.infoByNode.get(node);
      if (this.decorate !== undefined && info !== undefined) this.decorate(tr, node, info);
      if (tr.dataset.ep === undefined && this.lastActiveByNode.has(node)) this.applyHighlight(tr, node);
    }
    const queued = [...this.pendingFlash];
    this.pendingFlash.clear();
    for (const node of queued) this.flash(node);
    this.prune();
    this.tick(); // populate the "last seen" cells right after a rebuild
    if (this.onRender !== undefined) this.onRender(data); // IR / klima panels (built once)
  }

  /**
   * Drop all bookkeeping for nodes that vanished from the DOM and whose flash
   * expired — activity, the node flash timer, and any queued flash/scene — so
   * nothing leaks for a node that's gone. (Per-channel `node:ep` flash timers
   * self-delete when their own callback fires, within HIGHLIGHT_MS.)
   */
  private prune(): void {
    for (const [node, ts] of this.lastActiveByNode) {
      if (Date.now() - ts > HIGHLIGHT_MS && this.nodeRow(node) === null) {
        this.lastActiveByNode.delete(node);
        clearTimeout(this.flashTimers.get(node));
        this.flashTimers.delete(node);
        this.pendingFlash.delete(node);
        clearTimeout(this.sceneTimers.get(node));
        this.sceneTimers.delete(node);
        this.pendingScene.delete(node);
      }
    }
  }

  /** Merge a state delta into the cached row and re-render its "stan" only. */
  applyState(node: number, fields: Partial<DeviceInfo>): void {
    if (this.refreshing) {
      this.pendingState.set(node, { ...this.pendingState.get(node), ...fields });
      return;
    }
    const info = this.infoByNode.get(node);
    if (info === undefined) return; // row not built yet — the next refresh covers it
    const prevEndpoints = info.endpoints; // snapshot before the merge — to spot which channel changed
    Object.assign(info, fields);
    const eps = info.endpoints;
    // Multi-gang: live state lives in per-endpoint sub-rows, not the node "stan".
    if ("endpoints" in fields && eps !== null && Object.keys(eps).length > 1) {
      let missing = false;
      for (const ep of Object.keys(eps)) {
        const sub = this.view.rows.querySelector<HTMLElement>(
          `tr[data-node="${attr(String(node))}"][data-ep="${attr(ep)}"]`,
        );
        const stan = sub?.querySelector(".ep-stan") ?? null;
        if (sub === null || stan === null) {
          missing = true;
          continue;
        }
        stan.textContent = eps[ep] === true ? "on" : "off";
        if (prevEndpoints === null || prevEndpoints[ep] !== eps[ep]) {
          this.flashRow(sub, `${String(node)}:${ep}`); // this channel changed → highlight its sub-row
        }
      }
      if (missing) void this.refresh(); // a newly-appeared endpoint → rebuild sub-rows once
      return;
    }
    const val = this.view.rows.querySelector(
      `tr[data-node="${attr(String(node))}"]:not([data-ep]) .stanval`,
    );
    if (val !== null) val.textContent = stateStr(info);
  }

  /** Apply a (partial) globals delta — only the field(s) present are written. */
  applyGlobals(fields: Partial<Globals>): void {
    if (this.refreshing) {
      this.pendingGlobals = { ...this.pendingGlobals, ...fields };
      return;
    }
    if ("crib_temp" in fields) this.view.crib.textContent = fmtTemp(fields.crib_temp ?? null);
    if ("outdoor_temp" in fields) {
      this.view.outdoor.textContent = fmtTemp(fields.outdoor_temp ?? null);
    }
  }

  /**
   * Replay deltas that queued during a rebuild, against the fresh rows.
   *
   * Snapshot-and-clear BEFORE replaying: applyState's multi-gang "missing
   * endpoint" branch can re-enter refresh() (setting `refreshing` true again),
   * after which the remaining replays re-queue. Clearing first means those
   * re-queues land in a fresh, empty queue and survive to the re-entrant
   * refresh's own drain, instead of being wiped by a clear() run afterwards.
   */
  private drainPending(): void {
    const states = [...this.pendingState];
    this.pendingState.clear();
    const scenes = [...this.pendingScene];
    this.pendingScene.clear();
    const globals = this.pendingGlobals;
    this.pendingGlobals = null;
    for (const [node, fields] of states) this.applyState(node, fields);
    for (const [node, scene] of scenes) this.flashScene(node, scene);
    if (globals !== null) this.applyGlobals(globals);
  }

  // ---- heatmap: activity flash, scene badge, "last seen" tick --------------

  private nodeRow(node: number): HTMLElement | null {
    return this.view.rows.querySelector<HTMLElement>(
      `tr[data-node="${attr(String(node))}"]:not([data-ep])`,
    );
  }

  /** Record activity for a node and brightly flash its row (heatmap onboarding). */
  flash(node: number): void {
    this.lastActiveByNode.set(node, Date.now()); // always record — decoupled from the DOM
    const tr = this.nodeRow(node);
    if (tr === null) {
      this.pendingFlash.add(node); // row not built yet → replay after the next rebuild
      return;
    }
    this.applyHighlight(tr, node);
  }

  private applyHighlight(tr: HTMLElement, node: number): void {
    const ts = this.lastActiveByNode.get(node);
    if (ts === undefined) return;
    const elapsed = Date.now() - ts;
    if (elapsed >= HIGHLIGHT_MS) return; // the flash already expired during the rebuild
    clearTimeout(this.flashTimers.get(node));
    tr.classList.add("active");
    this.flashTimers.set(node, setTimeout(() => {
      tr.classList.remove("active");
      this.flashTimers.delete(node); // keep the map bounded to live timers only
    }, HIGHLIGHT_MS - elapsed));
  }

  /** Flash an arbitrary row keyed by id, so a node:ep sub-row and its parent row
   *  never share a timer. */
  private flashRow(tr: HTMLElement, key: string): void {
    clearTimeout(this.flashTimers.get(key));
    tr.classList.add("active");
    this.flashTimers.set(key, setTimeout(() => {
      tr.classList.remove("active");
      this.flashTimers.delete(key); // keep the map bounded to live timers only
    }, HIGHLIGHT_MS));
  }

  /** Show a transient "⏏ scena N" badge next to a node's state on a button press. */
  flashScene(node: number, scene: Scene): void {
    if (this.refreshing) {
      this.pendingScene.set(node, scene); // rebuild in flight → replay after
      return;
    }
    const badge = this.nodeRow(node)?.querySelector(".scene-badge") ?? null;
    if (badge === null) {
      this.pendingScene.set(node, scene); // row not built yet → replay after
      return;
    }
    badge.textContent = `⏏ scena ${String(scene.id)}`;
    badge.classList.add("on");
    clearTimeout(this.sceneTimers.get(node));
    this.sceneTimers.set(node, setTimeout(() => {
      badge.classList.remove("on");
      badge.textContent = "";
      this.sceneTimers.delete(node); // keep the map bounded to live timers only
    }, SCENE_MS));
  }

  private relTime(ms: number | undefined): string {
    if (ms === undefined) return "—";
    const s = Math.floor((Date.now() - ms) / 1000);
    if (s < 2) return "now";
    if (s < 60) return `${String(s)}s ago`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${String(m)}m ago`;
    return `${String(Math.floor(m / 60))}h ago`;
  }

  /** Tick once a second: refresh each row's relative "last seen" + the recent glow. */
  tick(): void {
    for (const tr of this.view.rows.querySelectorAll<HTMLTableRowElement>("tr[data-node]:not([data-ep])")) {
      const raw = tr.dataset.node;
      if (raw === undefined) continue;
      const ts = this.lastActiveByNode.get(Number(raw));
      const cell = tr.querySelector(".seen");
      if (cell !== null) cell.textContent = this.relTime(ts);
      if (ts === undefined) continue;
      const recent = Date.now() - ts < RECENT_MS;
      tr.classList.toggle("recent", recent);
      if (cell !== null) cell.classList.toggle("fresh", recent);
    }
  }
}
