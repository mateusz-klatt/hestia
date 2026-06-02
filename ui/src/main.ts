import "./style.css";

import {
  apiUrl,
  deleteRule,
  fetchAutomations,
  fetchDiscovery,
  postControl,
  postIr,
  postName,
  postRule,
  whoami,
} from "./api/client";
import { renderAutomations } from "./automations";
import { renderActions } from "./controls";
import { renderIrButtons, renderKlima } from "./klima";
import { LiveController } from "./live";
import { renderLogin, renderUser } from "./login";
import { bindRow, bindSubRow } from "./registry";
import { renderRuleForm } from "./ruleform";

function el(id: string): HTMLElement {
  const node = document.getElementById(id);
  if (node === null) throw new Error(`Missing #${id}`);
  return node;
}

const irBox = el("ir-buttons");
const klimaBox = el("klima");
const ruleForm = el("rule-form");
const ruleJson = el("rule-json") as HTMLTextAreaElement;

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
  },
);

/** Wire up the live app (events, intervals, initial fetch). Called only once authenticated. */
function startApp(): void {
  el("refresh").addEventListener("click", () => {
    void live.refresh();
  });

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
  if (me.user !== null) {
    renderUser(el("auth"), me.user, () => {
      location.reload();
    });
  }
  startApp();
})();
