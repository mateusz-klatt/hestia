import type { Rule, RuleResult, Trigger } from "./api/types";
import { t as msg } from "./i18n"; // aliased: `trigSummary` already binds `t` to its Trigger argument

/** One-line human summary of a rule's trigger (for the list). */
export function trigSummary(t: Trigger): string {
  switch (t.type) {
    case "scene":
      return `scene ${String(t.scene_id)} @node ${String(t.node)}`;
    case "state": {
      if (t.node === undefined) return `${t.field} ${t.op} ${String(t.value)}`; // global (node-less) field
      const gang = t.endpoint !== undefined ? ` gang ${String(t.endpoint)}` : ""; // one gang of a multi-gang switch
      return `node ${String(t.node)}${gang} ${t.field} ${t.op} ${String(t.value)}`;
    }
    case "time":
      // The server serialises an unscheduled-days trigger as `days: null` (not omitted), so guard with
      // Array.isArray — a bare `=== undefined` check let `null.join()` throw and killed the row loop.
      return `at ${t.at}${Array.isArray(t.days) ? ` [${t.days.join(",")}]` : ""}`;
    case "sun": {
      // offset_min is always present in the contract (0 when unset), so no undefined guard needed.
      const off =
        t.offset_min !== 0 ? `${t.offset_min > 0 ? "+" : ""}${String(t.offset_min)}m` : "";
      return `${t.event}${off}${Array.isArray(t.days) ? ` [${t.days.join(",")}]` : ""}`;
    }
    case "presence":
      return `${t.mac} ${t.event}`;
    case "cron":
      return `cron ${t.expr}`;
    default:
      // The rule is cast from JSON (server-validated, but defensively): an
      // unknown future trigger type shows its raw `type` rather than "undefined".
      return (t as { type: string }).type;
  }
}

/** Wiring a row's mutations needs the API + a reload + the JSON editor's load hook. */
export interface AutomationsDeps {
  reload: () => void;
  onEdit: (rule: Rule) => void;
  postRule: (payload: unknown) => Promise<RuleResult>;
  deleteRule: (id: string) => Promise<RuleResult>;
  confirm: (message: string) => boolean;
}

function ruleError(result: RuleResult): string {
  return result.body?.error ?? `error ${String(result.status)}`;
}

/** `label` → `data-label`, the cell's heading in the mobile card layout (thead hidden — see style.css). */
function cell(text: string, label?: string): HTMLTableCellElement {
  const td = document.createElement("td");
  td.textContent = text;
  if (label !== undefined) td.dataset.label = label;
  return td;
}

function rowButton(label: string, cls: string): HTMLButtonElement {
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = label;
  b.className = cls;
  b.style.marginRight = "0.3rem";
  return b;
}

function automationRow(rule: Rule, deps: AutomationsDeps): HTMLTableRowElement {
  const tr = document.createElement("tr");
  tr.dataset.id = rule.id;
  tr.appendChild(cell(rule.id, msg("auto.id")));

  const enTd = document.createElement("td");
  enTd.dataset.label = msg("auto.enabled");
  const en = document.createElement("input");
  en.type = "checkbox";
  en.className = "auto-en";
  en.checked = rule.enabled;
  enTd.appendChild(en);
  tr.appendChild(enTd);

  tr.appendChild(cell(trigSummary(rule.trigger), msg("auto.trigger")));
  tr.appendChild(cell(String(rule.conditions.length), msg("auto.conditions")));
  tr.appendChild(cell(rule.actions.map((a) => a.op).join(", "), msg("auto.actions")));

  const actTd = document.createElement("td");
  const edit = rowButton(msg("auto.edit"), "auto-edit");
  const del = rowButton(msg("auto.delete"), "auto-del");
  const status = document.createElement("span");
  status.className = "status";
  actTd.append(edit, del, status);
  tr.appendChild(actTd);

  const showError = (msg: string): void => {
    status.textContent = msg;
    status.className = "status err";
  };

  edit.addEventListener("click", () => {
    deps.onEdit(rule);
  });

  let deleting = false;
  del.addEventListener("click", () => {
    if (deleting || !deps.confirm(msg("auto.deleteConfirm", { id: rule.id }))) return;
    deleting = true;
    del.disabled = true;
    void (async () => {
      try {
        const res = await deps.deleteRule(rule.id);
        if (res.ok) deps.reload(); // success rebuilds the list
        else showError(ruleError(res));
      } catch {
        showError(msg("ctl.error"));
      } finally {
        // Always release — a no-op reload, an unexpected reject, etc. must never
        // leave the row permanently dead. (On success the row is replaced anyway.)
        deleting = false;
        del.disabled = false;
      }
    })();
  });

  // Enable/disable inline: re-save the whole rule with `enabled` flipped.
  let toggling = false;
  en.addEventListener("change", () => {
    if (toggling) return;
    toggling = true;
    en.disabled = true;
    const want = en.checked;
    void (async () => {
      try {
        const res = await deps.postRule({ ...rule, enabled: want });
        if (res.ok) deps.reload();
        else {
          en.checked = rule.enabled; // revert the box, surface the error
          showError(ruleError(res));
        }
      } catch {
        en.checked = rule.enabled;
        showError(msg("ctl.error"));
      } finally {
        toggling = false;
        en.disabled = false;
      }
    })();
  });

  return tr;
}

/** Replace the automations table body with one row per rule. */
export function renderAutomations(tbody: HTMLElement, rules: Rule[], deps: AutomationsDeps): void {
  tbody.replaceChildren();
  for (const rule of rules) tbody.appendChild(automationRow(rule, deps));
}
