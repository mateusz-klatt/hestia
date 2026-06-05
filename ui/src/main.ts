import "./style.css";

import {
  apiUrl,
  deleteRule,
  fetchAudit,
  fetchAutomations,
  fetchDbStats,
  fetchDiscovery,
  fetchRf433,
  fetchRoomIcons,
  fetchSettings,
  postControl,
  postIr,
  postName,
  postRule,
  postScene,
  saveRoomIcon as saveRoomIconOnServer,
  saveSettings as saveUserSettings,
  whoami,
} from "./api/client";
import type { DeviceInfo } from "./api/types";
import { formatAuditTarget, renderAuditFeed } from "./audit";
import { renderAutomations } from "./automations";
import { renderActions } from "./controls";
import { renderDbStats } from "./dbstats";
import { initLocale } from "./i18n";
import { applyKlimaState, renderIrButtons, renderKlima } from "./klima";
import { LiveController } from "./live";
import { renderLogin, renderUser } from "./login";
import { bindRow, bindSubRow } from "./registry";
import { renderRf433 } from "./rf433";
import { createRoomsView } from "./rooms";
import { renderRuleForm } from "./ruleform";
import { renderSceneControls } from "./scenes";
import { reconcileServerSettings } from "./settings";
import { renderViewSwitch, type ViewName } from "./view";

function el(id: string): HTMLElement {
  const node = document.getElementById(id);
  if (node === null) throw new Error(`Missing #${id}`);
  return node;
}

const irBox = el("ir-buttons");
const klimaBox = el("klima");
const ruleForm = el("rule-form");
const ruleJson = el("rule-json") as HTMLTextAreaElement;

// Rooms view (wife-friendly): house-wide IR/klima panels live in their own persistent containers;
// the room list/detail rebuilds inside #room-list. The rooms view keeps its own latest snapshot
// (set via update() in onRender), so the switcher just asks it to show the landing.
const roomsIrBox = el("rooms-ir");
const roomsKlimaBox = el("rooms-klima");
// Notify the view-switch when the rooms view enters/leaves a room, so its tab can flip to "← Rooms"
// (a discoverable back). Assigned in startApp once the switch exists; nav events only fire after that.
let onRoomsNav: (inRoom: boolean) => void = () => undefined;
// Set in startApp once the view switcher exists; the settings-menu "edit icons" entry calls it.
let triggerIconEdit: () => void = () => undefined;
let roomIcons: Record<string, string> = {};

// Audit feed: resolve a device row's node-id target → "name · room" at display time (Codex: read-time,
// UI-side — no DB change, renames fix old rows). The current device map feeds the pure formatter.
let latestDevices: Record<string, DeviceInfo> = {};
const resolveAuditTarget = (target: string | null, action: string): string | null =>
  formatAuditTarget(target, action, latestDevices);
const roomsView = createRoomsView(el("room-list"), {
  postControl,
  roomIcons: () => roomIcons,
  saveRoomIcon: async (room, icon) => {
    if (await saveRoomIconOnServer(room, icon)) {
      if (icon === "") {
        roomIcons = Object.fromEntries(Object.entries(roomIcons).filter(([key]) => key !== room));
      } else {
        roomIcons = { ...roomIcons, [room]: icon };
      }
    }
  },
  renderWholeHome: (c) => {
    renderSceneControls(c, postScene); // the "Cały dom" virtual room's detail body
  },
  onNav: (inRoom) => {
    onRoomsNav(inRoom);
  },
});

const live = new LiveController(
  {
    hdrText: el("hdr-text"),
    mode: el("mode"),
    crib: el("g-crib"),
    outdoor: el("g-outdoor"),
    outdoorHumidity: el("g-outdoor-humidity"),
    rows: el("rows"),
    conn: el("conn"),
    status: el("status"),
  },
  fetchDiscovery,
  (tr, node, info) => {
    const ep = tr.dataset.ep;
    if (ep !== undefined) {
      const endpointState = info.endpoints?.[ep];
      const cell = tr.querySelector<HTMLElement>(".actions");
      if (cell !== null && endpointState !== undefined) {
        renderActions(cell, node, { ...info, endpoints: { [ep]: endpointState } }, postControl);
      }
      bindSubRow(tr, node, Number(ep), postName); // multi-gang channel label
      return;
    }
    const cell = tr.querySelector<HTMLElement>(".actions");
    if (cell !== null) {
      // A multi-gang node's per-channel on/off lives in the sub-rows below, so the node row must NOT
      // also render every channel's buttons (that duplicated them). Single-endpoint / non-endpoint
      // devices have no sub-rows, so they keep their node-level controls. (The rooms view has no
      // sub-rows either, so it still renders all channels on the card via its own renderActions call.)
      const eps = info.endpoints;
      if (eps !== null && Object.keys(eps).length > 1) cell.replaceChildren();
      else renderActions(cell, node, info, postControl);
    }
    bindRow(tr, node, info, postName); // confirm + name/room save
  },
  (data) => {
    renderIrButtons(irBox, data.ir_buttons, postIr); // built once from the static config
    renderKlima(klimaBox, data.klima, postIr);
    renderRuleForm(ruleForm, ruleJson, data.rule_vocab, data.klima); // guided form → fills #rule-json
    // Rooms view: the same house-wide IR/klima into their own (built-once) containers, then rebuild
    // the room list from the fresh snapshot. Kept last so a throw here can't skip the panels above.
    renderIrButtons(roomsIrBox, data.ir_buttons, postIr);
    renderKlima(roomsKlimaBox, data.klima, postIr);
    latestDevices = data.devices; // feed the audit-target resolver with current names/rooms
    roomsView.update(data);
  },
  (node, info) => {
    roomsView.patchState(node, info); // live state delta → patch the visible room card's state text
  },
  (klimaState) => {
    applyKlimaState([klimaBox, roomsKlimaBox], klimaState); // A/C status pictogram on both panels
  },
);

/** Wire up the live app (events, intervals, initial fetch). Called only once authenticated. */
function startApp(): void {
  const audit = renderAuditFeed(el("audit-feed"), fetchAudit, resolveAuditTarget);
  const rf433 = renderRf433(el("rf433"), fetchRf433);
  const dbStats = renderDbStats(el("dbstats"), fetchDbStats);

  el("refresh").addEventListener("click", () => {
    void live.refresh();
  });

  // View switcher: 🏠 Rooms (default) · 📜 Activity (the event log) · 🔧 Advanced. Applies the persisted
  // choice immediately; switching into the rooms view returns to the room list.
  let currentView: ViewName = "rooms";
  const switcher = renderViewSwitch(
    {
      switchBox: el("view-switch"),
      roomsEl: el("rooms-view"),
      eventsEl: el("events-view"),
      adminEl: el("admin-view"),
    },
    (view) => {
      currentView = view;
      if (view === "rooms") {
        roomsView.goToLanding(); // tapping the rooms tab always returns to the list
      } else if (view === "events") {
        void audit.refresh(); // 📜 the event log is its own top-level view now
      } else {
        void rf433.refresh();
        void dbStats.refresh();
      }
    },
  );
  // The "← Rooms" back label only reflects nav while the rooms view is the active one — a background
  // refresh re-rendering the hidden room detail must not flip the tab while Advanced is showing.
  onRoomsNav = (inRoom) => {
    if (currentView === "rooms") switcher.setRoomsInRoom(inRoom);
  };
  // Settings → "Edit icons": make sure the rooms view is showing, then enter icon-edit mode.
  triggerIconEdit = () => {
    switcher.apply("rooms");
    roomsView.enterIconEdit();
  };

  // ---- Automations editor -------------------------------------------------
  const autoRows = el("auto-rows");
  const ruleStatus = el("rule-status");

  const RULE_TEMPLATE = {
    id: "my-rule",
    enabled: true,
    modes: ["proxy", "standalone"],
    debounce: 0,
    trigger: { type: "scene", node: 0, scene_id: 1 },
    conditions: [],
    actions: [{ op: "switch", node: 0, on: true }],
  };

  function setRuleStatus(text: string, isErr: boolean): void {
    ruleStatus.textContent = text;
    ruleStatus.className = isErr ? "status err" : "status";
  }

  async function loadAutomations(): Promise<void> {
    const rules = await fetchAutomations();
    if (rules === null) {
      autoRows.replaceChildren();
      setRuleStatus("(automations unavailable)", true);
      return;
    }
    renderAutomations(autoRows, rules, {
      reload: () => {
        void loadAutomations();
      },
      onEdit: (rule) => {
        ruleJson.value = JSON.stringify(rule, null, 2);
        setRuleStatus(`editing ${rule.id}`, false);
      },
      postRule,
      deleteRule,
      confirm: (message) => window.confirm(message),
    });
  }

  el("rule-template").addEventListener("click", () => {
    ruleJson.value = JSON.stringify(RULE_TEMPLATE, null, 2);
    setRuleStatus("template loaded — edit then Save", false);
  });

  el("save-rule").addEventListener("click", () => {
    void (async () => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(ruleJson.value);
      } catch (e) {
        setRuleStatus(`invalid JSON: ${e instanceof Error ? e.message : "parse error"}`, true);
        return;
      }
      const res = await postRule(parsed);
      if (res.ok) {
        setRuleStatus("saved", false);
        ruleJson.value = "";
        void loadAutomations();
      } else {
        setRuleStatus(res.body?.error ?? `error ${String(res.status)}`, true);
      }
    })();
  });

  // Server-Sent Events: live state / globals patches + discovery deltas. The
  // browser auto-reconnects on drop; `open` re-syncs the full snapshot.
  const events = new EventSource(apiUrl("events"));
  events.addEventListener("open", () => {
    live.setConnected(true);
    void live.refresh();
    void loadAutomations(); // re-sync the rule list on (re)connect
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

  // Re-fetch the snapshot every 45 s so time-based UI (the thermostat "not responding ⚠" badge, which
  // keys off last_seen vs the last command) re-evaluates with fresh data even when no SSE delta arrives.
  // refresh() is coalesced + skips while the operator is interacting, so it's cheap and non-disruptive.
  setInterval(() => {
    void live.refresh();
  }, 45_000);

  void live.refresh();
  void loadAutomations();
}

// Auth gate: probe /api/whoami. 401 (null) → show the login form (auth is on, not logged in). Otherwise
// boot the app; when a username is present (auth on), show the logged-in indicator + a logout button.
// `whoami` returns {user: null} when auth is OFF, so loopback/dev boots straight through with no login UI.
void (async () => {
  await initLocale(navigator.languages); // pick + apply the browser locale (sets <html lang>/dir) before any render
  const me = await whoami();
  if (me === null) {
    el("app").hidden = true;
    const box = el("login");
    box.hidden = false;
    renderLogin(box, () => {
      location.reload();
    });
    return;
  }
  if (me.user !== null && reconcileServerSettings(await fetchSettings())) {
    location.reload();
    return;
  }
  roomIcons = (await fetchRoomIcons()) ?? {};
  // Always render the user/settings chip; auth-off (me.user === null) shows a settings-only menu
  // (language + temperature scale, no logout — see renderUser).
  const userOpts = {
    onLogout: () => {
      location.reload();
    },
    onEditIcons: () => {
      triggerIconEdit();
    },
  };
  const authedUserOpts = {
    ...userOpts,
    saveSettings: async (settings: Parameters<typeof saveUserSettings>[0]): Promise<void> => {
      await saveUserSettings(settings);
    },
  };
  renderUser(el("auth"), me.user, me.user !== null ? authedUserOpts : userOpts);
  startApp();
})();
