import "./style.css";

import {
  apiUrl,
  deleteRule,
  fetchAudit,
  fetchAutomations,
  fetchDbStats,
  fetchDiscovery,
  fetchSettings,
  postControl,
  postIr,
  postName,
  postRule,
  saveSettings as saveUserSettings,
  whoami,
} from "./api/client";
import { renderAuditFeed } from "./audit";
import { renderAutomations } from "./automations";
import { renderActions } from "./controls";
import { renderDbStats } from "./dbstats";
import { initLocale } from "./i18n";
import { renderIrButtons, renderKlima } from "./klima";
import { LiveController } from "./live";
import { renderLogin, renderUser } from "./login";
import { bindRow, bindSubRow } from "./registry";
import { createRoomsView } from "./rooms";
import { renderRuleForm } from "./ruleform";
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
const roomsView = createRoomsView(el("room-list"), {
  postControl,
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
      bindSubRow(tr, node, Number(ep), postName); // multi-gang channel label
      return;
    }
    const cell = tr.querySelector<HTMLElement>(".actions");
    if (cell !== null) renderActions(cell, node, info, postControl);
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
    roomsView.update(data);
  },
  (node, info) => {
    roomsView.patchState(node, info); // live state delta → patch the visible room card's state text
  },
);

/** Wire up the live app (events, intervals, initial fetch). Called only once authenticated. */
function startApp(): void {
  const audit = renderAuditFeed(el("audit-feed"), fetchAudit);
  const dbStats = renderDbStats(el("dbstats"), fetchDbStats);

  el("refresh").addEventListener("click", () => {
    void live.refresh();
  });

  // View switcher: 🏠 Rooms (default) ↔ 🔧 Advanced. Applies the persisted choice immediately;
  // switching into the rooms view returns to the room list.
  let currentView: ViewName = "rooms";
  const switcher = renderViewSwitch(
    { switchBox: el("view-switch"), roomsEl: el("rooms-view"), adminEl: el("admin-view") },
    (view) => {
      currentView = view;
      if (view === "rooms") {
        roomsView.goToLanding(); // tapping the rooms tab always returns to the list
      } else {
        void audit.refresh();
        void dbStats.refresh();
      }
    },
  );
  // The "← Rooms" back label only reflects nav while the rooms view is the active one — a background
  // refresh re-rendering the hidden room detail must not flip the tab while Advanced is showing.
  onRoomsNav = (inRoom) => {
    if (currentView === "rooms") switcher.setRoomsInRoom(inRoom);
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
  // Always render the user/settings chip; auth-off (me.user === null) shows a settings-only menu
  // (language + temperature scale, no logout — see renderUser).
  const userOpts = {
    onLogout: () => {
      location.reload();
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
