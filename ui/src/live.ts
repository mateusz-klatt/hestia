import type { DeviceInfo, Discovery, Globals, LiveEvent } from "./api/types";
import { renderDeviceRows, renderGlobals, summaryText } from "./render/devices";
import { fmtTemp, stateStr } from "./render/format";

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
 * (Activity flash, scene badges and the "last seen" tick are layered on in PR-3b.)
 */
export class LiveController {
  private readonly view: LiveView;
  private readonly refetch: Refetch;
  private readonly infoByNode = new Map<number, DeviceInfo>();
  private refreshing = false;
  private refreshAgain = false;
  private readonly pendingState = new Map<number, Partial<DeviceInfo>>();
  private pendingGlobals: Partial<Globals> | null = null;

  constructor(view: LiveView, refetch: Refetch) {
    this.view = view;
    this.refetch = refetch;
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
        break; // PR-3b: row flash + scene badge
    }
  }

  setConnected(connected: boolean): void {
    this.view.conn.textContent = connected ? "" : "(reconnecting…)";
  }

  /** Fetch the full snapshot and rebuild the view; coalesces overlapping calls. */
  async refresh(): Promise<void> {
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
  }

  /** Merge a state delta into the cached row and re-render its "stan" only. */
  applyState(node: number, fields: Partial<DeviceInfo>): void {
    if (this.refreshing) {
      this.pendingState.set(node, { ...this.pendingState.get(node), ...fields });
      return;
    }
    const info = this.infoByNode.get(node);
    if (info === undefined) return; // row not built yet — the next refresh covers it
    Object.assign(info, fields);
    const eps = info.endpoints;
    // Multi-gang: live state lives in per-endpoint sub-rows, not the node "stan".
    if ("endpoints" in fields && eps !== null && Object.keys(eps).length > 1) {
      let missing = false;
      for (const ep of Object.keys(eps)) {
        const sub = this.view.rows.querySelector(
          `tr[data-node="${attr(String(node))}"][data-ep="${attr(ep)}"] .ep-stan`,
        );
        if (sub === null) {
          missing = true;
          continue;
        }
        sub.textContent = eps[ep] === true ? "on" : "off";
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
    const globals = this.pendingGlobals;
    this.pendingGlobals = null;
    for (const [node, fields] of states) this.applyState(node, fields);
    if (globals !== null) this.applyGlobals(globals);
  }
}
