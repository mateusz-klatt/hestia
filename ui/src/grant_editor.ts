import type { MutationResult } from "./api/client";
import { t } from "./i18n";

export interface GrantSelection {
  permission_id: string;
  target_owner: string | null;
}

export interface GrantCatalogEntry {
  permission_id: string;
  domain: string;
  storage_scope: "installation" | "owner";
  delegation: "self_only" | "delegable" | null;
  requires: string[];
  device_eligible: boolean;
  label: string;
  protected: boolean;
}

export interface UserGrants {
  phase: string;
  username: string;
  budget_owner: boolean;
  budget_owners: string[];
  catalog: GrantCatalogEntry[];
  direct: GrantSelection[];
  effective: GrantSelection[];
}

export interface GrantApi {
  fetchUserGrants: (username: string) => Promise<UserGrants | null>;
  saveUserGrants: (username: string, grants: GrantSelection[]) => Promise<MutationResult>;
}

function grantKey(permission: string, owner: string | null): string {
  return `${permission}\0${owner ?? ""}`;
}

function domain(spec: GrantCatalogEntry): string {
  if (spec.permission_id.startsWith("budget.receipts.")) return "receipts";
  return spec.domain;
}

function domainLabel(value: string): string {
  if (value === "ioc") return t("grants.domain.ioc");
  if (value === "calendar") return t("grants.domain.calendar");
  if (value === "receipts") return t("grants.domain.receipts");
  if (value === "budget") return t("budget.main.title");
  return t("audit.actor.system");
}

export async function renderGrantEditor(
  host: HTMLElement,
  username: string,
  api: GrantApi,
  currentUser: string | null,
  onSaved: () => Promise<void>,
): Promise<void> {
  host.textContent = t("common.loading");
  const data = await api.fetchUserGrants(username);
  if (data === null) {
    host.textContent = t("modal.error");
    return;
  }
  host.replaceChildren();
  const form = document.createElement("form");
  form.className = "grant-editor";
  const title = document.createElement("h4");
  title.textContent = `${t("users.permissions")} — ${username}`;
  form.appendChild(title);

  const initialTargets = data.direct.flatMap((grant) => grant.target_owner === null ? [] : [grant.target_owner]);
  let target = data.budget_owner ? username : (initialTargets[0] ?? data.budget_owners[0] ?? null);
  const direct = new Set(data.direct.map((grant) => grantKey(grant.permission_id, grant.target_owner)));
  const explicit = new Set(data.direct
    .filter((grant) => grant.target_owner === null || grant.target_owner === target)
    .map((grant) => grant.permission_id));
  const inputs = new Map<string, HTMLInputElement>();
  const byId = new Map(data.catalog.map((spec) => [spec.permission_id, spec]));

  const targetLabel = document.createElement("label");
  targetLabel.className = "grant-target";
  targetLabel.textContent = t("users.grantOwner");
  const targetSelect = document.createElement("select");
  targetSelect.setAttribute("aria-label", t("users.grantOwner"));
  const owners = data.budget_owner ? [username] : data.budget_owners;
  for (const owner of owners) {
    const option = document.createElement("option");
    option.value = owner;
    option.textContent = owner;
    option.selected = owner === target;
    targetSelect.appendChild(option);
  }
  targetSelect.disabled = data.budget_owner;
  targetLabel.appendChild(targetSelect);
  if (data.catalog.some((spec) => spec.storage_scope === "owner")) form.appendChild(targetLabel);

  const withRequirements = (permissions: Iterable<string>): Set<string> => {
    const effective = new Set(permissions);
    for (const permission of effective) {
      for (const required of byId.get(permission)?.requires ?? []) effective.add(required);
    }
    return effective;
  };

  const syncImplications = (): void => {
    const effective = withRequirements(explicit);
    for (const [permission, input] of inputs) {
      input.checked = effective.has(permission);
    }
  };

  for (const name of ["ioc", "calendar", "budget", "receipts", "system"]) {
    const specs = data.catalog.filter((spec) => domain(spec) === name);
    if (specs.length === 0) continue;
    const group = document.createElement("fieldset");
    group.className = `grant-domain grant-domain-${name}`;
    const legend = document.createElement("legend");
    legend.textContent = domainLabel(name);
    group.appendChild(legend);
    for (const spec of specs) {
      const label = document.createElement("label");
      label.className = "grant-permission";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = spec.permission_id;
      input.dataset.permission = spec.permission_id;
      input.dataset.scope = spec.storage_scope;
      const protectedSelf = spec.protected && username === currentUser;
      input.disabled = protectedSelf || (spec.storage_scope === "owner"
        && (target === null || (!data.budget_owner && spec.delegation === "self_only")));
      if (protectedSelf) {
        explicit.add(spec.permission_id);
        input.title = t("users.grantSelfAdmin");
      }
      input.addEventListener("change", () => {
        if (input.checked) {
          explicit.add(spec.permission_id);
        } else {
          const withdrawn = new Set([spec.permission_id]);
          for (const permission of withdrawn) {
            for (const candidate of data.catalog) {
              if (candidate.requires.includes(permission)) withdrawn.add(candidate.permission_id);
            }
          }
          for (const permission of withdrawn) explicit.delete(permission);
        }
        syncImplications();
      });
      inputs.set(spec.permission_id, input);
      const code = document.createElement("code");
      code.textContent = spec.permission_id;
      label.append(input, code);
      if (spec.requires.length > 0) {
        const reason = document.createElement("small");
        reason.textContent = t("users.grantImplied", { permissions: spec.requires.join(", ") });
        label.appendChild(reason);
      }
      group.appendChild(label);
    }
    form.appendChild(group);
  }
  syncImplications();

  const full = document.createElement("button");
  full.type = "button";
  full.className = "grant-full";
  full.textContent = t("users.fullAccess");
  full.addEventListener("click", () => {
    const selectable = data.catalog.filter((spec) => {
      const input = inputs.get(spec.permission_id);
      return input !== undefined && !input.disabled && !spec.protected;
    });
    const implied = withRequirements(selectable.flatMap((spec) => spec.requires));
    for (const spec of selectable) {
      if (!implied.has(spec.permission_id)) explicit.add(spec.permission_id);
    }
    syncImplications();
  });

  const save = document.createElement("button");
  save.type = "submit";
  save.className = "grant-save";
  save.textContent = t("dev.save");
  const status = document.createElement("span");
  status.className = "status";
  status.setAttribute("aria-live", "polite");
  form.append(full, save, status);

  targetSelect.addEventListener("change", () => {
    target = targetSelect.value || null;
    for (const spec of data.catalog) {
      if (spec.storage_scope !== "owner") continue;
      const input = inputs.get(spec.permission_id);
      if (input === undefined) continue;
      if (direct.has(grantKey(spec.permission_id, target))) {
        explicit.add(spec.permission_id);
      } else {
        explicit.delete(spec.permission_id);
      }
      input.disabled = target === null || (!data.budget_owner && spec.delegation === "self_only");
    }
    syncImplications();
  });

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    void (async () => {
      save.disabled = true;
      const grants: GrantSelection[] = [];
      for (const spec of data.catalog) {
        if (!explicit.has(spec.permission_id)) continue;
        grants.push({
          permission_id: spec.permission_id,
          target_owner: spec.storage_scope === "owner" ? target : null,
        });
      }
      const result = await api.saveUserGrants(username, grants);
      if (result.ok) {
        status.className = "status ok";
        status.textContent = t("wholeHome.saved");
        await onSaved();
      } else {
        save.disabled = false;
        status.className = "status err";
        status.textContent = result.error ?? t("modal.error");
      }
    })();
  });
  host.appendChild(form);
}
