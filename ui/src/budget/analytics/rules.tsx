// Categories and deterministic rules (2D). Every change is an explicit, versioned owner action; a rule is saved
// only from a fresh server preview showing what it would change, and manual choices always win over rules.
import { useCallback, useEffect, useRef, useState } from "react";
import type { BudgetApi, BudgetResult } from "../api";
import type { BudgetAccount } from "../model";
import { accountLabel } from "../model";
import { ANALYTICS_ERROR, CATEGORY_NAME_MAX, KIND_CHOICES, RELOAD_ERRORS, labelOf, RULE_FIELDS, RULE_OPERATORS, RULE_PRIORITY_MAX, RULE_VALUE_MAX, ruleCandidate,
  validCandidate, validCategoryName } from "./model";
import type { AnalyticsCategory, AnalyticsRule, KindChoice, RuleCandidate, RulePreview } from "./model";

export const rulesLabels = {
  en: {
    categories: "Categories", rules: "Rules", name: "Name", add: "Add category", rename: "Rename", save: "Save", cancel: "Cancel",
    archive: "Archive", restore: "Restore", archived: "archived", noCategories: "No categories yet.",
    rulesHelp: "Rules sort new and existing entries automatically. Your own choice on an entry always wins over a rule.",
    noRules: "No rules yet.", newRule: "New rule", edit: "Edit", field: "When", operator: "", value: "text",
    fields: { description: "description", counterparty_name: "counterparty", mcc: "card merchant code" },
    operators: { contains: "contains", equals: "equals" },
    category: "Category", keep: "No category change", kind: "Type",
    kinds: { auto: "No type change", expense: "Expense", income: "Income", refund: "Refund", excluded: "Leave out of the budget" },
    priority: "Priority", enabled: "Enabled", disabled: "disabled", preview: "Preview", back: "Change the rule",
    saveRule: "Save rule", affects: (count: number) => `This rule would apply to ${String(count)} entries, for example:`,
    reference: "entry", account: "Account",
    nothingToDo: "Choose a category or a type for the rule.",
    loading: "Loading…", error: "Could not be loaded.", conflict: "This changed elsewhere; the latest state was loaded.",
    expired: "The preview expired or the data changed; preview the rule again.", saveError: "Could not save. Try again.",
  },
  pl: {
    categories: "Kategorie", rules: "Reguły", name: "Nazwa", add: "Dodaj kategorię", rename: "Zmień nazwę", save: "Zapisz",
    cancel: "Anuluj", archive: "Archiwizuj", restore: "Przywróć", archived: "archiwalna", noCategories: "Nie ma jeszcze kategorii.",
    rulesHelp: "Reguły porządkują nowe i dotychczasowe pozycje automatycznie. Twój ręczny wybór przy pozycji zawsze ma pierwszeństwo.",
    noRules: "Nie ma jeszcze reguł.", newRule: "Nowa reguła", edit: "Edytuj", field: "Gdy", operator: "", value: "tekst",
    fields: { description: "opis", counterparty_name: "kontrahent", mcc: "kod branży karty" },
    operators: { contains: "zawiera", equals: "równa się" },
    category: "Kategoria", keep: "Bez zmiany kategorii", kind: "Rodzaj",
    kinds: { auto: "Bez zmiany rodzaju", expense: "Wydatek", income: "Wpływ", refund: "Zwrot", excluded: "Pomiń w budżecie" },
    priority: "Priorytet", enabled: "Włączona", disabled: "wyłączona", preview: "Podgląd", back: "Popraw regułę",
    saveRule: "Zapisz regułę", affects: (count: number) => `Reguła obejmie pozycji: ${String(count)}, na przykład:`,
    reference: "pozycja", account: "Konto",
    nothingToDo: "Wybierz kategorię albo rodzaj dla reguły.",
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
  text: RulesLabels;
  revision: number;
  onAccessLoss: Loss;
}

export function CategoriesView({ api, accounts, text, revision, onAccessLoss }: CategoriesViewProps) {
  const [categories, setCategories] = useState<Listed<AnalyticsCategory>>("loading");
  const [rules, setRules] = useState<Listed<AnalyticsRule>>("loading");
  const [changes, setChanges] = useState(0);
  // Every reload takes a new generation: lists and saves from an older one are dropped.
  const generation = useRef(0);
  useEffect(() => {
    const token = ++generation.current;
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
      <CategoriesPanel categories={categories} api={api} run={run} text={text} onSaved={reload} />
      <RulesPanel rules={rules} categories={shown} accounts={accounts} api={api} run={run} text={text} onSaved={reload} />
    </section>
  );
}

type Run = <T>(action: () => Promise<BudgetResult<T>>) => Promise<BudgetResult<T> | null>;

const PREVIEW_GONE: ReadonlySet<string> = new Set([ANALYTICS_ERROR.previewExpired, ANALYTICS_ERROR.stalePreview]);

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
  onSaved: () => void;
}

function CategoriesPanel({ categories, api, run, text, onSaved }: CategoriesProps) {
  const [draft, setDraft] = useState("");
  const [renaming, setRenaming] = useState<{ id: string; name: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const save = async (action: () => Promise<BudgetResult<{ category: AnalyticsCategory }>>, done: () => void) => {
    setBusy(true);
    setNotice(null);
    const result = await run(action);
    if (result === null) return;
    setBusy(false);
    if (!result.ok) { setNotice(failure(text, result.error)); return; }
    done();
    onSaved();
  };
  const update = (category: AnalyticsCategory, changes: { name?: string; archived?: boolean }) =>
    () => api.updateCategory({ category_id: category.id, name: changes.name ?? category.name, archived: changes.archived ?? category.archived,
      expected_version: category.version });

  return (
    <section className="budget-entries" aria-label={text.categories}>
      <h4>{text.categories}</h4>
      {notice === null ? null : <p className="budget-notice" role="alert">{notice}</p>}
      {categories === "loading" ? <p className="budget-muted" role="status">{text.loading}</p> : null}
      {categories === "error" ? <p className="budget-notice" role="alert">{text.error}</p> : null}
      {Array.isArray(categories) && categories.length === 0 ? <p className="budget-muted">{text.noCategories}</p> : null}
      {Array.isArray(categories) && categories.length > 0 ? (
        <ul className="budget-entry-list">
          {categories.map((category) => (
            <li key={category.id} className="budget-entry budget-category">
              {renaming?.id === category.id ? (
                <form className="budget-entry-editor" onSubmit={(event) => {
                  event.preventDefault();
                  if (validCategoryName(renaming.name)) {
                    void save(update(category, { name: renaming.name.trim() }), () => { setRenaming(null); });
                  }
                }}>
                  <label>{text.name}
                    <input maxLength={CATEGORY_NAME_MAX} value={renaming.name}
                      onChange={(event) => { setRenaming({ id: category.id, name: event.currentTarget.value }); }} />
                  </label>
                  <button type="submit" className="budget-primary" disabled={busy || !validCategoryName(renaming.name)}>{text.save}</button>
                  <button type="button" disabled={busy} onClick={() => { setRenaming(null); }}>{text.cancel}</button>
                </form>
              ) : (
                <div className="budget-entry-facts">
                  <strong className={category.archived ? "budget-muted" : undefined}>{category.name}</strong>
                  {category.archived ? <span>({text.archived})</span> : null}
                  <button type="button" disabled={busy} onClick={() => { setRenaming({ id: category.id, name: category.name }); }}>{text.rename}</button>
                  <button type="button" disabled={busy}
                    onClick={() => { void save(update(category, { archived: !category.archived }), () => undefined); }}>
                    {category.archived ? text.restore : text.archive}
                  </button>
                </div>
              )}
            </li>
          ))}
        </ul>
      ) : null}
      <form className="budget-entry-editor" onSubmit={(event) => {
        event.preventDefault();
        if (validCategoryName(draft)) void save(() => api.createCategory({ name: draft.trim() }), () => { setDraft(""); });
      }}>
        <label>{text.name}
          <input maxLength={CATEGORY_NAME_MAX} value={draft} onChange={(event) => { setDraft(event.currentTarget.value); }} />
        </label>
        <button type="submit" disabled={busy || !validCategoryName(draft)}>{text.add}</button>
      </form>
    </section>
  );
}

interface RulesProps {
  rules: Listed<AnalyticsRule>;
  categories: readonly AnalyticsCategory[];
  accounts: readonly BudgetAccount[];
  api: Pick<BudgetApi, "previewRule" | "saveRule">;
  run: Run;
  text: RulesLabels;
  onSaved: () => void;
}

function RulesPanel({ rules, categories, accounts, api, run, text, onSaved }: RulesProps) {
  const [editing, setEditing] = useState<RuleCandidate | null>(null);
  const [preview, setPreview] = useState<RulePreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const categoryName = (id: string | null) => (id === null ? null : categories.find((item) => item.id === id)?.name ?? null);
  const accountName = (id: string) => {
    const account = accounts.find((item) => item.id === id);
    if (account === undefined) return text.account;
    return accountLabel(account, accounts.filter((item) => item.connection === account.connection).indexOf(account), text.account);
  };

  const open = (rule: AnalyticsRule | null) => { setEditing(ruleCandidate(rule)); setPreview(null); setNotice(null); };
  // Any change to the rule invalidates the preview: only what was previewed can be saved.
  const change = (changes: Partial<RuleCandidate>) => {
    setEditing((current) => (current === null ? null : { ...current, ...changes }));
    setPreview(null);
  };

  const ask = async () => {
    if (editing === null || !validCandidate(editing)) return;
    setBusy(true);
    setNotice(null);
    const result = await run(() => api.previewRule({ ...editing, value: editing.value.trim() }));
    if (result === null) return;
    setBusy(false);
    if (!result.ok) { setNotice(failure(text, result.error)); return; }
    setPreview(result.data.preview);
  };
  const commit = async () => {
    if (preview === null) return;
    setBusy(true);
    setNotice(null);
    const result = await run(() => api.saveRule(preview.preview_id));
    if (result === null) return;
    setBusy(false);
    if (!result.ok) {
      setNotice(failure(text, result.error));
      if (PREVIEW_GONE.has(result.error)) setPreview(null);
      return;
    }
    setEditing(null);
    setPreview(null);
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
          {rules.map((rule) => (
            <li key={rule.id} className="budget-entry budget-rule">
              <div className="budget-entry-facts">
                <span>{labelOf(text.fields, rule.field)} {labelOf(text.operators, rule.operator)} „{rule.value}”</span>
                <span aria-hidden="true">→</span>
                {categoryName(rule.category_id) === null ? null : <span className="budget-chip">{categoryName(rule.category_id)}</span>}
                {rule.kind === "auto" ? null : <span className="budget-chip">{labelOf(text.kinds, rule.kind)}</span>}
                <span>{text.priority} {rule.priority}</span>
                {rule.enabled ? null : <span className="budget-warn">{text.disabled}</span>}
                <button type="button" disabled={busy || editing !== null} onClick={() => { open(rule); }}>{text.edit}</button>
              </div>
            </li>
          ))}
        </ul>
      ) : null}
      {editing === null ? (
        <button type="button" disabled={busy} onClick={() => { open(null); }}>{text.newRule}</button>
      ) : (
        <form className="budget-rule-editor" onSubmit={(event) => { event.preventDefault(); void ask(); }}>
          <label>{text.field}
            <select value={editing.field} onChange={(event) => { change({ field: event.currentTarget.value as RuleCandidate["field"] }); }}>
              {RULE_FIELDS.map((field) => <option key={field} value={field}>{text.fields[field]}</option>)}
            </select>
          </label>
          <label>{text.operator}
            <select aria-label={text.operators.contains} value={editing.operator}
              onChange={(event) => { change({ operator: event.currentTarget.value as RuleCandidate["operator"] }); }}>
              {RULE_OPERATORS.map((operator) => <option key={operator} value={operator}>{text.operators[operator]}</option>)}
            </select>
          </label>
          <label>{text.value}
            <input maxLength={RULE_VALUE_MAX} value={editing.value} onChange={(event) => { change({ value: event.currentTarget.value }); }} />
          </label>
          <label>{text.category}
            <select value={editing.category_id ?? ""} onChange={(event) => { change({ category_id: event.currentTarget.value || null }); }}>
              <option value="">{text.keep}</option>
              {categories.filter((item) => !item.archived || item.id === editing.category_id)
                .map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
            </select>
          </label>
          <label>{text.kind}
            <select value={editing.kind} onChange={(event) => { change({ kind: event.currentTarget.value as KindChoice }); }}>
              {KIND_CHOICES.map((kind) => <option key={kind} value={kind}>{text.kinds[kind]}</option>)}
            </select>
          </label>
          <label>{text.priority}
            <input type="number" min={0} max={RULE_PRIORITY_MAX} step={1} value={editing.priority}
              onChange={(event) => { change({ priority: event.currentTarget.valueAsNumber }); }} />
          </label>
          <label className="budget-toggle">
            <input type="checkbox" checked={editing.enabled} onChange={(event) => { change({ enabled: event.currentTarget.checked }); }} />
            {text.enabled}
          </label>
          {editing.category_id === null && editing.kind === "auto" ? <p className="budget-muted">{text.nothingToDo}</p> : null}
          <div className="budget-actions">
            <button type="submit" disabled={busy || !validCandidate(editing)}>{text.preview}</button>
            <button type="button" disabled={busy} onClick={() => { setEditing(null); setPreview(null); }}>{text.cancel}</button>
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
