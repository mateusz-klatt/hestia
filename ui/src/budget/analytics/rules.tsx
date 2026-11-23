// Categories and deterministic rules (2D). Every change is an explicit, versioned owner action; a rule is saved
// only from a fresh server preview showing what it would change, and manual choices always win over rules.
import { useCallback, useEffect, useRef, useState } from "react";
import { t } from "../../i18n";
import type { BudgetApi, BudgetResult } from "../api";
import type { BudgetAccount, BudgetConnection } from "../model";
import { CategoryBadge, CategorySwatch } from "./category_badge";
import { buildCategoryColors, categoryHueToHex } from "./category_colors";
import { listCategoryTree, parentChoices } from "./category_tree";
import type { ParentChoice } from "./category_tree";
import { ANALYTICS_ERROR, CATEGORY_NAME_MAX, KIND_CHOICES, RELOAD_ERRORS, RULE_FIELDS, RULE_FIELD_KEYS,
  RULE_KIND_KEYS, RULE_OPERATORS, RULE_OPERATOR_KEYS, RULE_PRIORITY_MAX, RULE_VALUE_MAX,
  accountName as nameOf, labelOf, ruleCandidate, validCandidate, validCategoryName } from "./model";
import type { AnalyticsCategory, AnalyticsRule, KindChoice, RuleCandidate, RulePreview } from "./model";

type Loss = (result: { ok: boolean; status?: number; error?: string }) => boolean;
type Listed<T> = T[] | "loading" | "error";

export interface CategoriesViewProps {
  api: Pick<BudgetApi, "categories" | "createCategory" | "updateCategory" | "spreadCategoryColors" | "rules" | "previewRule" | "saveRule">;
  accounts: readonly BudgetAccount[];
  connections?: readonly BudgetConnection[] | undefined;
  locale?: string;
  revision: number;
  onAccessLoss: Loss;
  initialRule?: RuleCandidate | null | undefined;
}

export function CategoriesView({ api, accounts, connections = [], locale = "en", revision, onAccessLoss,
  initialRule = null }: CategoriesViewProps) {
  const [categories, setCategories] = useState<Listed<AnalyticsCategory>>("loading");
  const [rules, setRules] = useState<Listed<AnalyticsRule>>("loading");
  const [changes, setChanges] = useState(0);
  // Every reload takes a new generation: lists and saves from an older one are dropped, and the panels drop any
  // preview or pending state that belonged to the previous one.
  const generation = useRef(0);
  const [epoch, setEpoch] = useState(0);
  useEffect(() => {
    const token = ++generation.current;
    setEpoch(token);
    setCategories("loading");
    setRules("loading");
    void api.categories().then((result) => {
      if (onAccessLoss(result) || token !== generation.current) return;
      setCategories(result.ok ? result.data.categories : "error");
    });
    void api.rules().then((result) => {
      if (onAccessLoss(result) || token !== generation.current) return;
      setRules(result.ok ? result.data.rules : "error");
    });
  }, [api, revision, changes, onAccessLoss]);
  useEffect(() => () => { generation.current += 1; }, []);
  const reload = useCallback(() => { setChanges((current) => current + 1); }, []);

  /** One mutation: null when access was lost or the view reloaded meanwhile; a conflict reloads. */
  const run = useCallback(async <T,>(action: () => Promise<BudgetResult<T>>): Promise<BudgetResult<T> | null> => {
    const token = generation.current;
    const result = await action();
    if (onAccessLoss(result) || token !== generation.current) return null;
    if (!result.ok && RELOAD_ERRORS.has(result.error)) reload();
    return result;
  }, [onAccessLoss, reload]);

  const shown = Array.isArray(categories) ? categories : [];
  return (
    <section className="budget-month" aria-label={t("budget.rules.sectionAria")}>
      <CategoriesPanel categories={categories} api={api} run={run} locale={locale} onSaved={reload} epoch={epoch} />
      <RulesPanel rules={rules} categories={shown} accounts={accounts} connections={connections} api={api} run={run}
        onSaved={reload} epoch={epoch} initialRule={initialRule} />
    </section>
  );
}

type Run = <T>(action: () => Promise<BudgetResult<T>>) => Promise<BudgetResult<T> | null>;

const PREVIEW_GONE: ReadonlySet<string> = new Set([ANALYTICS_ERROR.previewExpired, ANALYTICS_ERROR.stalePreview]);

function inactiveKeyNote(reason: string | null | undefined): string | null {
  if (reason === "rule_key_unavailable") return t("budget.rules.inactiveKeyMissing");
  if (reason === "rule_key_rotated") return t("budget.rules.inactiveKeyRotated");
  return null;
}

function failure(error: string): string {
  if (RELOAD_ERRORS.has(error)) return t("budget.rules.conflict");
  if (PREVIEW_GONE.has(error)) return t("budget.rules.expired");
  return t("budget.rules.saveError");
}

interface CategoriesProps {
  categories: Listed<AnalyticsCategory>;
  api: Pick<BudgetApi, "createCategory" | "updateCategory" | "spreadCategoryColors">;
  run: Run;
  locale: string;
  onSaved: () => void;
  /** Changes with every reload of the view. */
  epoch: number;
}

function ParentField({ label, none, value, choices, disabled, onChange }: {
  label: string; none: string; value: string; choices: readonly ParentChoice[]; disabled: boolean;
  onChange: (value: string) => void;
}) {
  return (
    <label>{label}
      <select name="parent_id" value={value} disabled={disabled} onChange={(event) => { onChange(event.currentTarget.value); }}>
        <option value="">{none}</option>
        {choices.map((choice) => (
          <option key={choice.id} value={choice.id}>{"\u2003".repeat(Math.max(0, choice.depth - 1))}{choice.name}</option>
        ))}
      </select>
    </label>
  );
}

function CategoriesPanel({ categories, api, run, locale, onSaved, epoch }: CategoriesProps) {
  const [draft, setDraft] = useState("");
  const [draftParent, setDraftParent] = useState("");
  const [renaming, setRenaming] = useState<{
    id: string; name: string; parent: string; hue: number; hueChange: "keep" | "set" | "clear";
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  // A reload settles whatever was pending: its answer is discarded, so its controls must not stay locked.
  useEffect(() => { setBusy(false); }, [epoch]);

  const save = async <T,>(action: () => Promise<BudgetResult<T>>, done: () => void) => {
    setBusy(true);
    setNotice(null);
    const result = await run(action);
    setBusy(false);
    if (result === null) return;
    if (!result.ok) { setNotice(failure(result.error)); return; }
    done();
    onSaved();
  };
  const update = (category: AnalyticsCategory, changes: {
    name?: string; archived?: boolean; parent_id?: string | null; color_hue?: number | null | undefined;
  }) => () => api.updateCategory({
    category_id: category.id, name: changes.name ?? category.name, archived: changes.archived ?? category.archived,
    expected_version: category.version,
    parent_id: changes.parent_id === undefined ? category.parent_id : changes.parent_id,
    ...(changes.color_hue === undefined ? {} : { color_hue: changes.color_hue }),
  });

  const categoriesTitle = t("budget.rules.categories");
  const nameLabel = t("budget.rules.name");
  const parentLabel = t("budget.rules.parent");
  const noParentLabel = t("budget.rules.noParent");
  const archivedLabel = t("budget.rules.archived");
  const archiveBlockedLabel = t("budget.rules.archiveBlocked");

  return (
    <section className="budget-entries" aria-label={categoriesTitle}>
      <h4>{categoriesTitle}</h4>
      <button type="button" disabled={busy || !Array.isArray(categories)
        || !categories.some((category) => category.parent_id === null && !category.archived)}
        onClick={() => { void save(() => api.spreadCategoryColors(), () => undefined); }}>{t("budget.rules.spreadColors")}</button>
      {notice === null ? null : <p className="budget-notice" role="alert">{notice}</p>}
      {categories === "loading" ? <p className="budget-muted" role="status">{t("budget.rules.loading")}</p> : null}
      {categories === "error" ? <p className="budget-notice" role="alert">{t("budget.rules.error")}</p> : null}
      {Array.isArray(categories) && categories.length === 0 ? <p className="budget-muted">{t("budget.rules.noCategories")}</p> : null}
      {Array.isArray(categories) && categories.length > 0 ? (() => {
        const categoryColors = buildCategoryColors(categories);
        return (
          <ul className="budget-entry-list">
            {listCategoryTree(categories, locale).map(({ category, depth, has_children }) => {
              const archiveBlocked = has_children && !category.archived;
              return (
                <li key={category.id} className="budget-entry budget-category" data-depth={depth}>
                  {renaming?.id === category.id ? (
                    <form className="budget-entry-editor" onSubmit={(event) => {
                      event.preventDefault();
                      if (validCategoryName(renaming.name)) {
                        void save(update(category, {
                          name: renaming.name.trim(),
                          parent_id: renaming.parent === "" ? null : renaming.parent,
                          color_hue: renaming.parent !== "" || renaming.hueChange === "keep" ? undefined
                            : renaming.hueChange === "clear" ? null : renaming.hue,
                        }), () => { setRenaming(null); });
                      }
                    }}>
                      <label>{nameLabel}
                        <input name="category_name" maxLength={CATEGORY_NAME_MAX} value={renaming.name}
                          onChange={(event) => { setRenaming({ ...renaming, name: event.currentTarget.value }); }} />
                      </label>
                      <ParentField label={parentLabel} none={noParentLabel} value={renaming.parent} disabled={busy}
                        choices={parentChoices(categories, category.id, locale)}
                        onChange={(parent) => { setRenaming({ ...renaming, parent }); }} />
                      {renaming.parent === "" ? (
                        <label>{t("budget.rules.colorHue")}
                          <span className="budget-entry-facts">
                            <CategorySwatch color={{ background: categoryHueToHex(renaming.hue), hatched: false }} />
                            <input name="color_hue" type="range" min="0" max="359" step="1" value={renaming.hue}
                              disabled={busy} onChange={(event) => {
                                setRenaming({ ...renaming, hue: Number(event.currentTarget.value), hueChange: "set" });
                              }} />
                            <output>{renaming.hue}°</output>
                          </span>
                        </label>
                      ) : null}
                      {renaming.parent === "" ? (
                        <button type="button" disabled={busy || renaming.hueChange === "clear"}
                          onClick={() => { setRenaming({ ...renaming, hueChange: "clear" }); }}>{t("budget.rules.automaticColor")}</button>
                      ) : null}
                      <button type="submit" className="budget-primary" disabled={busy || !validCategoryName(renaming.name)}>
                        {t("budget.rules.save")}
                      </button>
                      <button type="button" disabled={busy} onClick={() => { setRenaming(null); }}>{t("budget.rules.cancel")}</button>
                    </form>
                  ) : (
                    <div className="budget-entry-facts">
                      <CategorySwatch color={categoryColors.get(category.id)} />
                      <strong className={category.archived ? "budget-muted" : undefined}>{category.name}</strong>
                      {category.archived ? <span>({archivedLabel})</span> : null}
                      <button type="button" disabled={busy} onClick={() => {
                        setRenaming({ id: category.id, name: category.name, parent: category.parent_id ?? "",
                          hue: category.color_hue ?? 20, hueChange: "keep" });
                      }}>{t("budget.rules.rename")}</button>
                      <button type="button" disabled={busy || archiveBlocked} title={archiveBlocked ? archiveBlockedLabel : undefined}
                        onClick={() => { void save(update(category, { archived: !category.archived }), () => undefined); }}>
                        {category.archived ? t("budget.rules.restore") : t("budget.rules.archive")}
                      </button>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        );
      })() : null}
      <form className="budget-entry-editor" onSubmit={(event) => {
        event.preventDefault();
        if (validCategoryName(draft)) {
          void save(() => api.createCategory({ name: draft.trim(), parent_id: draftParent === "" ? null : draftParent }), () => {
            setDraft("");
            setDraftParent("");
          });
        }
      }}>
        <label>{nameLabel}
          <input name="category_name" maxLength={CATEGORY_NAME_MAX} value={draft} onChange={(event) => { setDraft(event.currentTarget.value); }} />
        </label>
        <ParentField label={parentLabel} none={noParentLabel} value={draftParent} disabled={busy}
          choices={parentChoices(Array.isArray(categories) ? categories : [], null, locale)} onChange={setDraftParent} />
        <button type="submit" disabled={busy || !validCategoryName(draft)}>{t("budget.rules.add")}</button>
      </form>
    </section>
  );
}

interface RulesProps {
  rules: Listed<AnalyticsRule>;
  categories: readonly AnalyticsCategory[];
  accounts: readonly BudgetAccount[];
  connections: readonly BudgetConnection[];
  api: Pick<BudgetApi, "previewRule" | "saveRule">;
  run: Run;
  onSaved: () => void;
  /** Changes with every reload of the view. */
  epoch: number;
  initialRule: RuleCandidate | null;
}

function RulesPanel({ rules, categories, accounts, connections, api, run, onSaved, epoch, initialRule }: RulesProps) {
  const [editing, setEditing] = useState<RuleCandidate | null>(initialRule);
  const [preview, setPreview] = useState<RulePreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  // Each edit of the form takes a new draft revision; a preview answer is shown only for the exact draft it was
  // asked for, so a late answer can never make an older rule savable under a newer form.
  const draft = useRef(0);
  // A reload discards pending answers and any preview taken before it.
  useEffect(() => { draft.current += 1; setBusy(false); setPreview(null); }, [epoch]);
  const categoryName = (id: string | null) => (id === null ? null : categories.find((item) => item.id === id)?.name ?? null);
  const categoryColors = buildCategoryColors(categories);
  const accountName = (id: string) => nameOf(accounts, connections, id, t("budget.rules.account"));

  const open = (rule: AnalyticsRule | null) => {
    draft.current += 1;
    setEditing(ruleCandidate(rule));
    setPreview(null);
    setNotice(null);
  };
  // Any change to the rule invalidates the preview: only what was previewed can be saved.
  const change = (changes: Partial<RuleCandidate>) => {
    draft.current += 1;
    setEditing((current) => (current === null ? null : { ...current, ...changes }));
    setPreview(null);
  };
  const close = () => { draft.current += 1; setEditing(null); setPreview(null); };

  // An enabled rule cannot point at an archived category (the server refuses it). An existing rule whose category was
  // archived keeps it and may still be turned off; turning it on again needs an active category. Only the rule's own
  // archived category is ever offered, so a new assignment to an archived one cannot be made here.
  const archivedTarget = editing !== null && editing.category_id !== null
    && categories.some((item) => item.id === editing.category_id && item.archived);
  const ready = editing !== null && validCandidate(editing) && !(archivedTarget && editing.enabled);

  const ask = async () => {
    if (editing === null || !ready) return;
    const asked = draft.current;
    setBusy(true);
    setNotice(null);
    const candidate: RuleCandidate = { ...editing, value: editing.value.trim() };
    if (candidate.counterparty_account_key == null) delete candidate.counterparty_account_key;
    const result = await run(() => api.previewRule(candidate));
    setBusy(false);
    // Dropped: access was lost, the view reloaded, or the form changed since this preview was asked for.
    if (result === null || asked !== draft.current) return;
    if (!result.ok) { setNotice(failure(result.error)); return; }
    setPreview(result.data.preview);
  };
  const commit = async () => {
    if (preview === null) return;
    const asked = draft.current;
    setBusy(true);
    setNotice(null);
    const result = await run(() => api.saveRule(preview.preview_id));
    setBusy(false);
    if (result === null || asked !== draft.current) return;
    if (!result.ok) {
      setNotice(failure(result.error));
      if (PREVIEW_GONE.has(result.error)) setPreview(null);
      return;
    }
    close();
    onSaved();
  };

  const rulesTitle = t("budget.rules.rules");
  const archivedText = t("budget.rules.archived");
  const previewText = t("budget.rules.preview");

  return (
    <section className="budget-entries" aria-label={rulesTitle}>
      <h4>{rulesTitle}</h4>
      <p className="budget-muted">{t("budget.rules.rulesHelp")}</p>
      {notice === null ? null : <p className="budget-notice" role="alert">{notice}</p>}
      {rules === "loading" ? <p className="budget-muted" role="status">{t("budget.rules.loading")}</p> : null}
      {rules === "error" ? <p className="budget-notice" role="alert">{t("budget.rules.error")}</p> : null}
      {Array.isArray(rules) && rules.length === 0 ? <p className="budget-muted">{t("budget.rules.noRules")}</p> : null}
      {Array.isArray(rules) && rules.length > 0 ? (
        <ul className="budget-entry-list">
          {rules.map((rule) => {
            const ruleCategory = categoryName(rule.category_id);
            const keyNote = inactiveKeyNote(rule.inactive_reason);
            return (
              <li key={rule.id} className="budget-entry budget-rule">
                <div className="budget-entry-facts">
                  <span>
                    {t("budget.rules.conditionSummary", {
                      field: labelOf(RULE_FIELD_KEYS, rule.field),
                      operator: labelOf(RULE_OPERATOR_KEYS, rule.operator),
                      value: rule.value,
                    })}
                  </span>
                  <span aria-hidden="true">→</span>
                  {ruleCategory === null ? null : (
                    <CategoryBadge color={categoryColors.get(rule.category_id)} label={ruleCategory} />
                  )}
                  {rule.kind === "auto" ? null : <span className="budget-chip">{labelOf(RULE_KIND_KEYS, rule.kind)}</span>}
                  <span>{t("budget.rules.priorityValue", { priority: rule.priority })}</span>
                  {rule.enabled ? null : <span className="budget-warn">{t("budget.rules.disabled")}</span>}
                  {keyNote === null ? null : <span className="budget-muted">{keyNote}</span>}
                  <button type="button" disabled={busy || editing !== null} onClick={() => { open(rule); }}>{t("budget.rules.edit")}</button>
                </div>
              </li>
            );
          })}
        </ul>
      ) : null}
      {editing === null ? (
        <button type="button" disabled={busy} onClick={() => { open(null); }}>{t("budget.rules.newRule")}</button>
      ) : (
        <form className="budget-rule-editor" onSubmit={(event) => { event.preventDefault(); void ask(); }}>
          <label>{t("budget.rules.field")}
            <select name="field" value={editing.field} disabled={busy} onChange={(event) => {
              const field = event.currentTarget.value as RuleCandidate["field"];
              change({ field, operator: field === "counterparty_account" ? "equals" : editing.operator, counterparty_account_key: null });
            }}>
              {RULE_FIELDS.map((field) => <option key={field} value={field}>{labelOf(RULE_FIELD_KEYS, field)}</option>)}
            </select>
          </label>
          <label>
            <select name="operator" aria-label={t("budget.rules.operators.contains")} value={editing.operator}
              disabled={busy || editing.field === "counterparty_account"}
              onChange={(event) => { change({ operator: event.currentTarget.value as RuleCandidate["operator"] }); }}>
              {(editing.field === "counterparty_account" ? ["equals"] as const : RULE_OPERATORS)
                .map((operator) => <option key={operator} value={operator}>{labelOf(RULE_OPERATOR_KEYS, operator)}</option>)}
            </select>
          </label>
          <label>{editing.field === "counterparty_account" ? t("budget.rules.fields.counterparty_account") : t("budget.rules.value")}
            <input name="value" maxLength={RULE_VALUE_MAX} value={editing.value} disabled={busy} onChange={(event) => {
              change({ value: event.currentTarget.value, counterparty_account_key: null });
            }} />
          </label>
          <label>{t("budget.rules.category")}
            <select name="category_id" value={editing.category_id ?? ""} disabled={busy} onChange={(event) => { change({ category_id: event.currentTarget.value || null }); }}>
              <option value="">{t("budget.rules.keep")}</option>
              {categories.filter((item) => !item.archived || item.id === editing.category_id).map((item) => (
                <option key={item.id} value={item.id}>{item.archived ? `${item.name} (${archivedText})` : item.name}</option>
              ))}
            </select>
          </label>
          <label>{t("budget.rules.kind")}
            <select name="kind" value={editing.kind} disabled={busy} onChange={(event) => { change({ kind: event.currentTarget.value as KindChoice }); }}>
              {KIND_CHOICES.map((kind) => <option key={kind} value={kind}>{labelOf(RULE_KIND_KEYS, kind)}</option>)}
            </select>
          </label>
          <label>{t("budget.rules.priority")}
            <input name="priority" type="number" min={0} max={RULE_PRIORITY_MAX} step={1} value={editing.priority} disabled={busy}
              onChange={(event) => { change({ priority: event.currentTarget.valueAsNumber }); }} />
          </label>
          <label className="budget-toggle">
            <input name="enabled" type="checkbox" checked={editing.enabled} disabled={busy} onChange={(event) => { change({ enabled: event.currentTarget.checked }); }} />
            {t("budget.rules.enabled")}
          </label>
          {editing.category_id === null && editing.kind === "auto" ? <p className="budget-muted">{t("budget.rules.nothingToDo")}</p> : null}
          {archivedTarget ? <p className="budget-notice" role={editing.enabled ? "alert" : "note"}>{t("budget.rules.archivedTarget")}</p> : null}
          <div className="budget-actions">
            <button type="submit" disabled={busy || !ready}>{previewText}</button>
            <button type="button" disabled={busy} onClick={close}>{t("budget.rules.cancel")}</button>
          </div>
          {preview === null ? null : (
            <div className="budget-preview" role="region" aria-label={previewText}>
              <p>{t("budget.rules.affects", { count: preview.affected_count })}</p>
              <ul>
                {preview.examples.map((example) => (
                  <li key={`${example.account_id}/${example.entry_reference}`}>
                    {accountName(example.account_id)} · <span className="budget-muted">{t("budget.rules.reference")} {example.entry_reference}</span>
                  </li>
                ))}
              </ul>
              <div className="budget-actions">
                <button type="button" className="budget-primary" disabled={busy} onClick={() => { void commit(); }}>{t("budget.rules.saveRule")}</button>
                <button type="button" disabled={busy} onClick={() => { setPreview(null); }}>{t("budget.rules.back")}</button>
              </div>
            </div>
          )}
        </form>
      )}
    </section>
  );
}
