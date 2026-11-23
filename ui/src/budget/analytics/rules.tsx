// Categories and deterministic rules (2D). Every change is an explicit, versioned owner action; a rule is saved
// only from a fresh server preview showing what it would change, and manual choices always win over rules.
import { useCallback, useEffect, useRef, useState } from "react";
import type { BudgetApi, BudgetResult } from "../api";
import type { BudgetAccount, BudgetConnection } from "../model";
import { CategoryBadge, CategorySwatch } from "./category_badge";
import { buildCategoryColors } from "./category_colors";
import { listCategoryTree, parentChoices } from "./category_tree";
import type { ParentChoice } from "./category_tree";
import { ANALYTICS_ERROR, CATEGORY_NAME_MAX, KIND_CHOICES, RELOAD_ERRORS, accountName as nameOf, labelOf, RULE_FIELDS, RULE_OPERATORS, RULE_PRIORITY_MAX, RULE_VALUE_MAX, ruleCandidate,
  validCandidate, validCategoryName } from "./model";
import type { AnalyticsCategory, AnalyticsRule, KindChoice, RuleCandidate, RulePreview } from "./model";

export const rulesLabels = {
  en: {
    categories: "Categories", rules: "Rules", name: "Name", add: "Add category", rename: "Rename", save: "Save", cancel: "Cancel",
    archive: "Archive", restore: "Restore", archived: "archived", noCategories: "No categories yet.",
    parent: "Parent category", noParent: "Top level",
    archiveBlocked: "Move its subcategories first. Archived subcategories still have to be moved.",
    rulesHelp: "Rules sort new and existing entries automatically. Your own choice on an entry always wins over a rule.",
    noRules: "No rules yet.", newRule: "New rule", edit: "Edit", field: "When", operator: "", value: "text",
    fields: { description: "description", counterparty_name: "counterparty", counterparty_account: "counterparty account (IBAN)", mcc: "card merchant code" },
    operators: { contains: "contains", equals: "equals" },
    category: "Category", keep: "No category change", kind: "Type",
    kinds: { auto: "No type change", expense: "Expense", income: "Income", refund: "Refund", excluded: "Leave out of the budget" },
    priority: "Priority", enabled: "Enabled", disabled: "disabled",
    inactiveKeyMissing: "Inactive: missing key",
    inactiveKeyRotated: "Inactive: key changed – create the rule again",
    preview: "Preview", back: "Change the rule",
    saveRule: "Save rule", affects: (count: number) => `This rule would apply to ${String(count)} entries, for example:`,
    reference: "entry", account: "Account",
    nothingToDo: "Choose a category or a type for the rule.",
    archivedTarget: "This category is archived: the rule can only be turned off. To keep it on, choose another category or restore this one.",
    loading: "Loading…", error: "Could not be loaded.", conflict: "This changed elsewhere; the latest state was loaded.",
    expired: "The preview expired or the data changed; preview the rule again.", saveError: "Could not save. Try again.",
  },
  pl: {
    categories: "Kategorie", rules: "Reguły", name: "Nazwa", add: "Dodaj kategorię", rename: "Zmień nazwę", save: "Zapisz",
    cancel: "Anuluj", archive: "Archiwizuj", restore: "Przywróć", archived: "archiwalna", noCategories: "Nie ma jeszcze kategorii.",
    parent: "Kategoria nadrzędna", noParent: "Najwyższy poziom",
    archiveBlocked: "Najpierw przenieś jej podkategorie. Zarchiwizowane też trzeba przenieść.",
    rulesHelp: "Reguły porządkują nowe i dotychczasowe pozycje automatycznie. Twój ręczny wybór przy pozycji zawsze ma pierwszeństwo.",
    noRules: "Nie ma jeszcze reguł.", newRule: "Nowa reguła", edit: "Edytuj", field: "Gdy", operator: "", value: "tekst",
    fields: { description: "opis", counterparty_name: "kontrahent", counterparty_account: "konto kontrahenta (IBAN)", mcc: "kod branży karty" },
    operators: { contains: "zawiera", equals: "równa się" },
    category: "Kategoria", keep: "Bez zmiany kategorii", kind: "Rodzaj",
    kinds: { auto: "Bez zmiany rodzaju", expense: "Wydatek", income: "Wpływ", refund: "Zwrot", excluded: "Pomiń w budżecie" },
    priority: "Priorytet", enabled: "Włączona", disabled: "wyłączona",
    inactiveKeyMissing: "Nieaktywna: brak klucza",
    inactiveKeyRotated: "Nieaktywna: zmieniony klucz – utwórz regułę ponownie",
    preview: "Podgląd", back: "Popraw regułę",
    saveRule: "Zapisz regułę", affects: (count: number) => `Reguła obejmie pozycji: ${String(count)}, na przykład:`,
    reference: "pozycja", account: "Konto",
    nothingToDo: "Wybierz kategorię albo rodzaj dla reguły.",
    archivedTarget: "Ta kategoria jest archiwalna: regułę można tylko wyłączyć. Aby działała dalej, wybierz inną kategorię albo przywróć tę.",
    loading: "Wczytywanie…", error: "Nie udało się wczytać.", conflict: "Zmieniono to w innym miejscu; wczytano aktualny stan.",
    expired: "Podgląd wygasł albo dane się zmieniły; zrób podgląd reguły ponownie.", saveError: "Nie udało się zapisać. Spróbuj ponownie.",
  },
};
export type RulesLabels = typeof rulesLabels.en;

type Loss = (result: { ok: boolean; status?: number; error?: string }) => boolean;
type Listed<T> = T[] | "loading" | "error";

export interface CategoriesViewProps {
  api: Pick<BudgetApi, "categories" | "createCategory" | "updateCategory" | "rules" | "previewRule" | "saveRule">;
  accounts: readonly BudgetAccount[];
  connections?: readonly BudgetConnection[] | undefined;
  text: RulesLabels;
  locale?: string;
  revision: number;
  onAccessLoss: Loss;
  initialRule?: RuleCandidate | null | undefined;
}

export function CategoriesView({ api, accounts, connections = [], text, locale = "en", revision, onAccessLoss,
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
    <section className="budget-month" aria-label={`${text.categories} · ${text.rules}`}>
      <CategoriesPanel categories={categories} api={api} run={run} text={text} locale={locale} onSaved={reload} epoch={epoch} />
      <RulesPanel rules={rules} categories={shown} accounts={accounts} connections={connections} api={api} run={run} text={text}
        onSaved={reload} epoch={epoch} initialRule={initialRule} />
    </section>
  );
}

type Run = <T>(action: () => Promise<BudgetResult<T>>) => Promise<BudgetResult<T> | null>;

const PREVIEW_GONE: ReadonlySet<string> = new Set([ANALYTICS_ERROR.previewExpired, ANALYTICS_ERROR.stalePreview]);

function inactiveKeyNote(text: RulesLabels, reason: string | null | undefined): string | null {
  if (reason === "rule_key_unavailable") return text.inactiveKeyMissing;
  if (reason === "rule_key_rotated") return text.inactiveKeyRotated;
  return null;
}

function failure(text: RulesLabels, error: string): string {
  if (RELOAD_ERRORS.has(error)) return text.conflict;
  if (PREVIEW_GONE.has(error)) return text.expired;
  return text.saveError;
}

interface CategoriesProps {
  categories: Listed<AnalyticsCategory>;
  api: Pick<BudgetApi, "createCategory" | "updateCategory">;
  run: Run;
  text: RulesLabels;
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

function CategoriesPanel({ categories, api, run, text, locale, onSaved, epoch }: CategoriesProps) {
  const [draft, setDraft] = useState("");
  const [draftParent, setDraftParent] = useState("");
  const [renaming, setRenaming] = useState<{ id: string; name: string; parent: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  // A reload settles whatever was pending: its answer is discarded, so its controls must not stay locked.
  useEffect(() => { setBusy(false); }, [epoch]);

  const save = async (action: () => Promise<BudgetResult<{ category: AnalyticsCategory }>>, done: () => void) => {
    setBusy(true);
    setNotice(null);
    const result = await run(action);
    setBusy(false);
    if (result === null) return;
    if (!result.ok) { setNotice(failure(text, result.error)); return; }
    done();
    onSaved();
  };
  const update = (category: AnalyticsCategory, changes: { name?: string; archived?: boolean; parent_id?: string | null }) =>
    () => api.updateCategory({
      category_id: category.id, name: changes.name ?? category.name, archived: changes.archived ?? category.archived,
      expected_version: category.version,
      parent_id: changes.parent_id === undefined ? category.parent_id : changes.parent_id,
    });

  return (
    <section className="budget-entries" aria-label={text.categories}>
      <h4>{text.categories}</h4>
      {notice === null ? null : <p className="budget-notice" role="alert">{notice}</p>}
      {categories === "loading" ? <p className="budget-muted" role="status">{text.loading}</p> : null}
      {categories === "error" ? <p className="budget-notice" role="alert">{text.error}</p> : null}
      {Array.isArray(categories) && categories.length === 0 ? <p className="budget-muted">{text.noCategories}</p> : null}
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
                        }), () => { setRenaming(null); });
                      }
                    }}>
                      <label>{text.name}
                        <input name="category_name" maxLength={CATEGORY_NAME_MAX} value={renaming.name}
                          onChange={(event) => { setRenaming({ id: category.id, name: event.currentTarget.value, parent: renaming.parent }); }} />
                      </label>
                      <ParentField label={text.parent} none={text.noParent} value={renaming.parent} disabled={busy}
                        choices={parentChoices(categories, category.id, locale)}
                        onChange={(parent) => { setRenaming({ id: category.id, name: renaming.name, parent }); }} />
                      <button type="submit" className="budget-primary" disabled={busy || !validCategoryName(renaming.name)}>{text.save}</button>
                      <button type="button" disabled={busy} onClick={() => { setRenaming(null); }}>{text.cancel}</button>
                    </form>
                  ) : (
                    <div className="budget-entry-facts">
                      <CategorySwatch color={categoryColors.get(category.id)} />
                      <strong className={category.archived ? "budget-muted" : undefined}>{category.name}</strong>
                      {category.archived ? <span>({text.archived})</span> : null}
                      <button type="button" disabled={busy} onClick={() => { setRenaming({ id: category.id, name: category.name, parent: category.parent_id ?? "" }); }}>{text.rename}</button>
                      <button type="button" disabled={busy || archiveBlocked} title={archiveBlocked ? text.archiveBlocked : undefined}
                        onClick={() => { void save(update(category, { archived: !category.archived }), () => undefined); }}>
                        {category.archived ? text.restore : text.archive}
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
        <label>{text.name}
          <input name="category_name" maxLength={CATEGORY_NAME_MAX} value={draft} onChange={(event) => { setDraft(event.currentTarget.value); }} />
        </label>
        <ParentField label={text.parent} none={text.noParent} value={draftParent} disabled={busy}
          choices={parentChoices(Array.isArray(categories) ? categories : [], null, locale)} onChange={setDraftParent} />
        <button type="submit" disabled={busy || !validCategoryName(draft)}>{text.add}</button>
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
  text: RulesLabels;
  onSaved: () => void;
  /** Changes with every reload of the view. */
  epoch: number;
  initialRule: RuleCandidate | null;
}

function RulesPanel({ rules, categories, accounts, connections, api, run, text, onSaved, epoch, initialRule }: RulesProps) {
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
  const accountName = (id: string) => nameOf(accounts, connections, id, text.account);

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
    if (!result.ok) { setNotice(failure(text, result.error)); return; }
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
      setNotice(failure(text, result.error));
      if (PREVIEW_GONE.has(result.error)) setPreview(null);
      return;
    }
    close();
    onSaved();
  };

  return (
    <section className="budget-entries" aria-label={text.rules}>
      <h4>{text.rules}</h4>
      <p className="budget-muted">{text.rulesHelp}</p>
      {notice === null ? null : <p className="budget-notice" role="alert">{notice}</p>}
      {rules === "loading" ? <p className="budget-muted" role="status">{text.loading}</p> : null}
      {rules === "error" ? <p className="budget-notice" role="alert">{text.error}</p> : null}
      {Array.isArray(rules) && rules.length === 0 ? <p className="budget-muted">{text.noRules}</p> : null}
      {Array.isArray(rules) && rules.length > 0 ? (
        <ul className="budget-entry-list">
          {rules.map((rule) => {
            const ruleCategory = categoryName(rule.category_id);
            const keyNote = inactiveKeyNote(text, rule.inactive_reason);
            return (
              <li key={rule.id} className="budget-entry budget-rule">
                <div className="budget-entry-facts">
                  <span>{labelOf(text.fields, rule.field)} {labelOf(text.operators, rule.operator)} „{rule.value}”</span>
                  <span aria-hidden="true">→</span>
                  {ruleCategory === null ? null : (
                    <CategoryBadge color={categoryColors.get(rule.category_id)} label={ruleCategory} />
                  )}
                  {rule.kind === "auto" ? null : <span className="budget-chip">{labelOf(text.kinds, rule.kind)}</span>}
                  <span>{text.priority} {rule.priority}</span>
                  {rule.enabled ? null : <span className="budget-warn">{text.disabled}</span>}
                  {keyNote === null ? null : <span className="budget-muted">{keyNote}</span>}
                  <button type="button" disabled={busy || editing !== null} onClick={() => { open(rule); }}>{text.edit}</button>
                </div>
              </li>
            );
          })}
        </ul>
      ) : null}
      {editing === null ? (
        <button type="button" disabled={busy} onClick={() => { open(null); }}>{text.newRule}</button>
      ) : (
        <form className="budget-rule-editor" onSubmit={(event) => { event.preventDefault(); void ask(); }}>
          <label>{text.field}
            <select name="field" value={editing.field} disabled={busy} onChange={(event) => {
              const field = event.currentTarget.value as RuleCandidate["field"];
              change({ field, operator: field === "counterparty_account" ? "equals" : editing.operator, counterparty_account_key: null });
            }}>
              {RULE_FIELDS.map((field) => <option key={field} value={field}>{text.fields[field]}</option>)}
            </select>
          </label>
          <label>{text.operator}
            <select name="operator" aria-label={text.operators.contains} value={editing.operator}
              disabled={busy || editing.field === "counterparty_account"}
              onChange={(event) => { change({ operator: event.currentTarget.value as RuleCandidate["operator"] }); }}>
              {(editing.field === "counterparty_account" ? ["equals"] as const : RULE_OPERATORS)
                .map((operator) => <option key={operator} value={operator}>{text.operators[operator]}</option>)}
            </select>
          </label>
          <label>{editing.field === "counterparty_account" ? text.fields.counterparty_account : text.value}
            <input name="value" maxLength={RULE_VALUE_MAX} value={editing.value} disabled={busy} onChange={(event) => {
              change({ value: event.currentTarget.value, counterparty_account_key: null });
            }} />
          </label>
          <label>{text.category}
            <select name="category_id" value={editing.category_id ?? ""} disabled={busy} onChange={(event) => { change({ category_id: event.currentTarget.value || null }); }}>
              <option value="">{text.keep}</option>
              {categories.filter((item) => !item.archived || item.id === editing.category_id).map((item) => (
                <option key={item.id} value={item.id}>{item.archived ? `${item.name} (${text.archived})` : item.name}</option>
              ))}
            </select>
          </label>
          <label>{text.kind}
            <select name="kind" value={editing.kind} disabled={busy} onChange={(event) => { change({ kind: event.currentTarget.value as KindChoice }); }}>
              {KIND_CHOICES.map((kind) => <option key={kind} value={kind}>{text.kinds[kind]}</option>)}
            </select>
          </label>
          <label>{text.priority}
            <input name="priority" type="number" min={0} max={RULE_PRIORITY_MAX} step={1} value={editing.priority} disabled={busy}
              onChange={(event) => { change({ priority: event.currentTarget.valueAsNumber }); }} />
          </label>
          <label className="budget-toggle">
            <input name="enabled" type="checkbox" checked={editing.enabled} disabled={busy} onChange={(event) => { change({ enabled: event.currentTarget.checked }); }} />
            {text.enabled}
          </label>
          {editing.category_id === null && editing.kind === "auto" ? <p className="budget-muted">{text.nothingToDo}</p> : null}
          {archivedTarget ? <p className="budget-notice" role={editing.enabled ? "alert" : "note"}>{text.archivedTarget}</p> : null}
          <div className="budget-actions">
            <button type="submit" disabled={busy || !ready}>{text.preview}</button>
            <button type="button" disabled={busy} onClick={close}>{text.cancel}</button>
          </div>
          {preview === null ? null : (
            <div className="budget-preview" role="region" aria-label={text.preview}>
              <p>{text.affects(preview.affected_count)}</p>
              <ul>
                {preview.examples.map((example) => (
                  <li key={`${example.account_id}/${example.entry_reference}`}>
                    {accountName(example.account_id)} · <span className="budget-muted">{text.reference} {example.entry_reference}</span>
                  </li>
                ))}
              </ul>
              <div className="budget-actions">
                <button type="button" className="budget-primary" disabled={busy} onClick={() => { void commit(); }}>{text.saveRule}</button>
                <button type="button" disabled={busy} onClick={() => { setPreview(null); }}>{text.back}</button>
              </div>
            </div>
          )}
        </form>
      )}
    </section>
  );
}
