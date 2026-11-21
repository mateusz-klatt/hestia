// Monthly category spending limits (2D): organisational aids only. Server evaluates exact decimal totals
// per currency without conversion; writes require strict CAS expected_version and never touch bank accounts.
import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { BudgetApi, BudgetResult } from "../api";
import type { BudgetAccount } from "../model";
import { formatAmount } from "../model";
import { ANALYTICS_ERROR, DEFAULT_WARNING_PERCENT, RELOAD_ERRORS, SPENDING_LIMIT_AMOUNT_MAX, currentMonth,
  isMonth, isZero, labelOf, limitCurrencies, monthLabel, parseWarningPercent, shiftMonth, validCurrency,
  validLimitAmount } from "./model";
import type { AnalyticsCategory, AnalyticsSpendingLimit, AnalyticsSpendingLimitStatus,
  AnalyticsSpendingLimits, SpendingLimitSaveRequest } from "./model";

export const spendingLimitsLabels = {
  en: {
    title: "Spending limits",
    intro: "Monthly category limits evaluated per currency without conversion. These are organisational aids only and never block payments or change accounts.",
    previous: "Previous month",
    next: "Next month",
    allCurrencies: "All currencies",
    currencyFilter: "Filter spending limits by currency",
    loading: "Loading spending limits…",
    error: "Spending limits could not be loaded. Try again.",
    evaluationTitle: "Monthly status",
    noStatuses: "No active spending limits to evaluate for this view.",
    configuredTitle: "Configured limits",
    noLimits: "No monthly spending limits configured yet.",
    noLimitsForCurrency: (currency: string) => `No spending limits configured in ${currency}.`,
    noCategoriesHint: "Create at least one active category in “Categories and rules” before adding a spending limit.",
    newLimit: "New limit",
    edit: "Edit",
    save: "Save limit",
    cancel: "Cancel",
    disable: "Disable",
    enable: "Enable",
    enabled: "Enabled",
    disabled: "disabled",
    archived: "archived",
    category: "Category",
    chooseCategory: "Choose category…",
    currency: "Currency",
    monthlyLimit: "Monthly limit",
    warningPercent: "Warning at (%)",
    statuses: {
      within: "Within limit",
      approaching: "Approaching limit",
      at_limit: "At limit",
      exceeded: "Exceeded",
    },
    spentOf: (spent: string, limit: string, percent: number) => `${spent} of ${limit} (${String(percent)}%)`,
    remaining: (amount: string) => `Remaining: ${amount}`,
    overBy: (amount: string) => `Over by: ${amount}`,
    expenses: (amount: string) => `Expenses: ${amount}`,
    refunds: (amount: string) => `Refunds: ${amount}`,
    warningAt: (percent: number) => `Warning at ${String(percent)}%`,
    duplicateHint: "A limit for this category and currency already exists — edit it below.",
    archivedTarget: "This category is archived: the limit can only stay disabled.",
    invalidHint: "Choose a category, 3-letter currency (e.g. PLN), positive amount and warning threshold (1–99%).",
    conflict: "This changed elsewhere; the latest state was loaded.",
    limitCap: "Maximum number of spending limits reached.",
    saveError: "Could not save spending limit. Try again.",
  },
  pl: {
    title: "Limity wydatków",
    intro: "Miesięczne limity kategorii liczone osobno dla każdej waluty, bez przeliczania kursów. To wyłącznie pomoc organizacyjna — nigdy nie blokują płatności ani nie zmieniają kont.",
    previous: "Poprzedni miesiąc",
    next: "Następny miesiąc",
    allCurrencies: "Wszystkie waluty",
    currencyFilter: "Filtruj limity wydatków według waluty",
    loading: "Wczytywanie limitów wydatków…",
    error: "Nie udało się wczytać limitów wydatków. Spróbuj ponownie.",
    evaluationTitle: "Stan miesięczny",
    noStatuses: "Brak aktywnych limitów wydatków do oceny w tym widoku.",
    configuredTitle: "Skonfigurowane limity",
    noLimits: "Nie skonfigurowano jeszcze miesięcznych limitów wydatków.",
    noLimitsForCurrency: (currency: string) => `Brak limitów wydatków w walucie ${currency}.`,
    noCategoriesHint: "Najpierw utwórz aktywną kategorię w zakładce „Kategorie i reguły”, aby dodać limit.",
    newLimit: "Nowy limit",
    edit: "Edytuj",
    save: "Zapisz limit",
    cancel: "Anuluj",
    disable: "Wyłącz",
    enable: "Włącz",
    enabled: "Włączony",
    disabled: "wyłączony",
    archived: "archiwalna",
    category: "Kategoria",
    chooseCategory: "Wybierz kategorię…",
    currency: "Waluta",
    monthlyLimit: "Limit miesięczny",
    warningPercent: "Ostrzeżenie od (%)",
    statuses: {
      within: "W limicie",
      approaching: "Blisko limitu",
      at_limit: "Na limicie",
      exceeded: "Przekroczony",
    },
    spentOf: (spent: string, limit: string, percent: number) => `${spent} z ${limit} (${String(percent)}%)`,
    remaining: (amount: string) => `Pozostało: ${amount}`,
    overBy: (amount: string) => `Przekroczono o: ${amount}`,
    expenses: (amount: string) => `Wydatki: ${amount}`,
    refunds: (amount: string) => `Zwroty: ${amount}`,
    warningAt: (percent: number) => `Ostrzeżenie od ${String(percent)}%`,
    duplicateHint: "Limit dla tej kategorii i waluty już istnieje — edytuj go poniżej.",
    archivedTarget: "Ta kategoria jest archiwalna: limit może pozostać tylko wyłączony.",
    invalidHint: "Wybierz kategorię, 3-literowy kod waluty (np. PLN), dodatnią kwotę i próg ostrzeżenia (1–99%).",
    conflict: "Zmieniono to w innym miejscu; wczytano aktualny stan.",
    limitCap: "Osiągnięto maksymalną liczbę limitów wydatków.",
    saveError: "Nie udało się zapisać limitu wydatków. Spróbuj ponownie.",
  },
};
export type SpendingLimitsLabels = typeof spendingLimitsLabels.en;

type Loss = (result: { ok: boolean; status?: number; error?: string }) => boolean;
type Shown = { state: "loading" } | { state: "error" } | { state: "ready"; data: AnalyticsSpendingLimits };

interface LimitDraft {
  mode: "create" | "edit";
  category_id: string;
  currency: string;
  monthly_limit: string;
  warning_percent: string;
  enabled: boolean;
  expected_version: number;
}

export interface SpendingLimitsViewProps {
  api: Pick<BudgetApi, "spendingLimits" | "saveSpendingLimit" | "categories">;
  accounts: readonly BudgetAccount[];
  text: SpendingLimitsLabels;
  locale: string;
  revision: number;
  onAccessLoss: Loss;
  initialMonth?: string | undefined;
  today?: () => Date;
}

function saveFailure(text: SpendingLimitsLabels, error: string): string {
  if (RELOAD_ERRORS.has(error)) return text.conflict;
  if (error === ANALYTICS_ERROR.limit) return text.limitCap;
  return text.saveError;
}

function buildRequest(draft: LimitDraft): SpendingLimitSaveRequest | null {
  const warning = parseWarningPercent(draft.warning_percent);
  if (draft.category_id === "" || !validCurrency(draft.currency) || !validLimitAmount(draft.monthly_limit) || warning === null) {
    return null;
  }
  return {
    category_id: draft.category_id,
    currency: draft.currency.trim().toUpperCase(),
    monthly_limit: draft.monthly_limit.trim(),
    warning_percent: warning,
    enabled: draft.enabled,
    expected_version: draft.expected_version,
  };
}

interface StatusPanelProps {
  statuses: readonly AnalyticsSpendingLimitStatus[];
  categoryMap: ReadonlyMap<string, AnalyticsCategory>;
  text: SpendingLimitsLabels;
  locale: string;
}

function StatusPanel({ statuses, categoryMap, text, locale }: StatusPanelProps) {
  return (
    <section className="budget-entries" aria-label={text.evaluationTitle}>
      <h4>{text.evaluationTitle}</h4>
      {statuses.length === 0 ? <p className="budget-muted">{text.noStatuses}</p> : (
        <ul className="budget-entry-list">
          {statuses.map((item) => {
            const label = item.name ?? categoryMap.get(item.category_id)?.name ?? item.category_id;
            const spent = formatAmount(item.net_spending, item.currency, locale);
            const cap = formatAmount(item.monthly_limit, item.currency, locale);
            const progress = Math.min(Math.max(item.percent_used_floor, 0), 100);
            return (
              <li key={`${item.currency}/${item.category_id}`} className="budget-entry">
                <div className="budget-limit-main">
                  <span className="budget-entry-text">
                    <strong>{label}</strong>
                    {item.archived ? <span className="budget-muted"> ({text.archived})</span> : null}
                  </span>
                  <span className="budget-chip budget-limit-status" data-status={item.status}>
                    {labelOf(text.statuses, item.status)}
                  </span>
                </div>
                <div className="budget-limit-meter">
                  <progress value={progress} max={100} aria-label={label} />
                  <span className="budget-limit-figures">{text.spentOf(spent, cap, item.percent_used_floor)}</span>
                </div>
                <div className="budget-entry-facts">
                  {isZero(item.over_by) ? (
                    <span>{text.remaining(formatAmount(item.remaining, item.currency, locale))}</span>
                  ) : (
                    <span className="budget-warn">{text.overBy(formatAmount(item.over_by, item.currency, locale))}</span>
                  )}
                  <span>{text.expenses(formatAmount(item.expenses, item.currency, locale))}</span>
                  {isZero(item.refunds) ? null : (
                    <span>{text.refunds(formatAmount(item.refunds, item.currency, locale))}</span>
                  )}
                  <span>{text.warningAt(item.warning_percent)}</span>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

interface LimitEditorProps {
  draft: LimitDraft;
  categories: readonly AnalyticsCategory[];
  currencySuggestions: readonly string[];
  existingKeys: ReadonlySet<string>;
  busy: boolean;
  text: SpendingLimitsLabels;
  onChange: (patch: Partial<LimitDraft>) => void;
  onSubmit: (request: SpendingLimitSaveRequest) => void;
  onCancel: () => void;
}

function LimitEditor({ draft, categories, currencySuggestions, existingKeys, busy, text, onChange,
  onSubmit, onCancel }: LimitEditorProps) {
  const listId = useId();
  const activeCategories = categories.filter((item) => !item.archived);
  const selectedCategory = categories.find((item) => item.id === draft.category_id);
  const archivedTarget = selectedCategory?.archived === true;
  const normalizedCurrency = draft.currency.trim().toUpperCase();
  const duplicate = draft.mode === "create" && draft.category_id !== ""
    && validCurrency(normalizedCurrency) && existingKeys.has(`${draft.category_id}/${normalizedCurrency}`);
  const request = buildRequest(draft);
  const canSave = !busy && request !== null && !duplicate && !(archivedTarget && draft.enabled);

  return (
    <form className="budget-rule-editor" onSubmit={(event) => {
      event.preventDefault();
      if (canSave) onSubmit(request);
    }}>
      {draft.mode === "create" ? (
        <>
          <label>{text.category}
            <select name="limit_category" value={draft.category_id} disabled={busy}
              onChange={(event) => { onChange({ category_id: event.currentTarget.value }); }}>
              <option value="">{text.chooseCategory}</option>
              {activeCategories.map((item) => (
                <option key={item.id} value={item.id}>{item.name}</option>
              ))}
            </select>
          </label>
          <label>{text.currency}
            <input name="limit_currency" type="text" list={listId} maxLength={3} value={draft.currency} disabled={busy}
              placeholder="PLN" onChange={(event) => { onChange({ currency: event.currentTarget.value.toUpperCase() }); }} />
            <datalist id={listId}>
              {currencySuggestions.map((code) => <option key={code} value={code} />)}
            </datalist>
          </label>
        </>
      ) : (
        <p className="budget-muted">
          <strong>{selectedCategory?.name ?? draft.category_id}</strong> · {draft.currency}
          {archivedTarget ? ` (${text.archived})` : ""}
        </p>
      )}
      <label>{text.monthlyLimit}
        <input name="monthly_limit" type="text" inputMode="decimal" maxLength={SPENDING_LIMIT_AMOUNT_MAX}
          value={draft.monthly_limit} disabled={busy} placeholder="500.00"
          onChange={(event) => { onChange({ monthly_limit: event.currentTarget.value }); }} />
      </label>
      <label>{text.warningPercent}
        <input name="warning_percent" type="number" min={1} max={99} step={1} value={draft.warning_percent}
          disabled={busy} onChange={(event) => { onChange({ warning_percent: event.currentTarget.value }); }} />
      </label>
      <label className="budget-toggle">
        <input name="limit_enabled" type="checkbox" checked={draft.enabled}
          disabled={busy || (archivedTarget && !draft.enabled)}
          onChange={(event) => { onChange({ enabled: event.currentTarget.checked }); }} />
        {text.enabled}
      </label>
      {duplicate ? <p className="budget-notice" role="alert">{text.duplicateHint}</p> : null}
      {archivedTarget && draft.enabled ? <p className="budget-notice" role="alert">{text.archivedTarget}</p> : null}
      {!duplicate && !(archivedTarget && draft.enabled) && request === null ? (
        <p className="budget-muted">{text.invalidHint}</p>
      ) : null}
      <div className="budget-actions">
        <button type="submit" className="budget-primary" disabled={!canSave}>{text.save}</button>
        <button type="button" disabled={busy} onClick={onCancel}>{text.cancel}</button>
      </div>
    </form>
  );
}

export function SpendingLimitsView({ api, accounts, text, locale, revision, onAccessLoss, initialMonth,
  today = () => new Date() }: SpendingLimitsViewProps) {
  const latest = currentMonth(today());
  const [month, setMonth] = useState(
    initialMonth !== undefined && isMonth(initialMonth) && initialMonth < latest ? initialMonth : latest);
  const [currency, setCurrency] = useState("");
  const [currencies, setCurrencies] = useState<string[]>([]);
  const [shown, setShown] = useState<Shown>({ state: "loading" });
  const [categories, setCategories] = useState<AnalyticsCategory[]>([]);
  const [changes, setChanges] = useState(0);
  const [editing, setEditing] = useState<LimitDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const generation = useRef(0);

  useEffect(() => {
    const token = ++generation.current;
    setShown({ state: "loading" });
    setBusy(false);
    void api.spendingLimits(month, currency === "" ? undefined : currency).then((result) => {
      // Run the access barrier before dropping superseded answers so a 401/403/404 always clears the view.
      if (onAccessLoss(result) || token !== generation.current) return;
      if (!result.ok) {
        setShown({ state: "error" });
        return;
      }
      setShown({ state: "ready", data: result.data });
      if (currency === "") {
        setCurrencies(limitCurrencies(result.data.limits));
      } else {
        setCurrencies((current) => (current.includes(currency) ? current
          : [...current, currency].sort((a, b) => a.localeCompare(b))));
      }
    });
    void api.categories().then((result) => {
      if (onAccessLoss(result) || token !== generation.current) return;
      if (result.ok) setCategories(result.data.categories);
    });
  }, [api, month, currency, revision, changes, onAccessLoss]);
  useEffect(() => () => { generation.current += 1; }, []);

  const reload = useCallback(() => { setChanges((current) => current + 1); }, []);
  const submit = useCallback(async (request: SpendingLimitSaveRequest, closeEditor: boolean) => {
    const token = generation.current;
    setBusy(true);
    setNotice(null);
    const result: BudgetResult<{ limit: unknown }> = await api.saveSpendingLimit(request);
    if (onAccessLoss(result) || token !== generation.current) return;
    setBusy(false);
    if (!result.ok) {
      if (RELOAD_ERRORS.has(result.error)) {
        setEditing(null);
        reload();
      }
      setNotice(saveFailure(text, result.error));
      return;
    }
    if (closeEditor) {
      setEditing(null);
      if (currency !== "" && request.currency !== currency) setCurrency("");
    }
    reload();
  }, [api, currency, onAccessLoss, reload, text]);

  const categoryMap = new Map(categories.map((item) => [item.id, item]));
  const activeCategories = categories.filter((item) => !item.archived);
  const limits = shown.state === "ready" ? shown.data.limits : [];
  const existingKeys = new Set(limits.map((item) => `${item.category_id}/${item.currency}`));
  const suggestions = limitCurrencies(limits, accounts);
  const defaultCurrency = currency !== "" ? currency : (suggestions[0] ?? "PLN");
  const showCurrencies = currencies.length > 1 || currency !== "";

  const openNew = () => {
    setNotice(null);
    setEditing({
      mode: "create",
      category_id: activeCategories[0]?.id ?? "",
      currency: defaultCurrency,
      monthly_limit: "",
      warning_percent: String(DEFAULT_WARNING_PERCENT),
      enabled: true,
      expected_version: 0,
    });
  };
  const openEdit = (limit: AnalyticsSpendingLimit) => {
    setNotice(null);
    setEditing({
      mode: "edit",
      category_id: limit.category_id,
      currency: limit.currency,
      monthly_limit: limit.monthly_limit,
      warning_percent: String(limit.warning_percent),
      enabled: limit.enabled,
      expected_version: limit.version,
    });
  };
  const toggleLimit = (limit: AnalyticsSpendingLimit) => {
    void submit({
      category_id: limit.category_id,
      currency: limit.currency,
      monthly_limit: limit.monthly_limit,
      warning_percent: limit.warning_percent,
      enabled: !limit.enabled,
      expected_version: limit.version,
    }, false);
  };

  return (
    <section className="budget-month" aria-label={text.title}>
      <nav className="budget-month-nav">
        <button type="button" aria-label={text.previous} onClick={() => { setMonth((value) => shiftMonth(value, -1)); }}>◀</button>
        <h3>{monthLabel(month, locale)}</h3>
        <button type="button" aria-label={text.next} disabled={month >= latest}
          onClick={() => { setMonth((value) => (isMonth(value) && value < latest ? shiftMonth(value, 1) : value)); }}>▶</button>
      </nav>
      <p className="budget-muted">{text.intro}</p>
      {showCurrencies ? (
        <div className="budget-views" role="group" aria-label={text.currencyFilter}>
          <button type="button" aria-pressed={currency === ""} onClick={() => { setCurrency(""); }}>
            {text.allCurrencies}
          </button>
          {currencies.map((code) => (
            <button type="button" key={code} aria-pressed={currency === code} onClick={() => { setCurrency(code); }}>
              {code}
            </button>
          ))}
        </div>
      ) : null}
      {notice === null ? null : <p className="budget-notice" role="alert">{notice}</p>}
      {shown.state === "loading" ? <p className="budget-muted" role="status">{text.loading}</p> : null}
      {shown.state === "error" ? <p className="budget-notice" role="alert">{text.error}</p> : null}
      {shown.state === "ready" ? (
        <>
          <StatusPanel statuses={shown.data.statuses} categoryMap={categoryMap} text={text} locale={locale} />
          <section className="budget-entries" aria-label={text.configuredTitle}>
            <div className="budget-limit-main">
              <h4>{text.configuredTitle}</h4>
              {editing === null && activeCategories.length > 0 ? (
                <button type="button" disabled={busy} onClick={openNew}>{text.newLimit}</button>
              ) : null}
            </div>
            {editing === null && activeCategories.length === 0 ? (
              <p className="budget-muted">{text.noCategoriesHint}</p>
            ) : null}
            {editing === null ? null : (
              <LimitEditor draft={editing} categories={categories} currencySuggestions={suggestions}
                existingKeys={existingKeys} busy={busy} text={text}
                onChange={(patch) => { setEditing((current) => (current === null ? null : { ...current, ...patch })); }}
                onSubmit={(request) => { void submit(request, true); }}
                onCancel={() => { setEditing(null); }} />
            )}
            {limits.length === 0 ? (
              <p className="budget-muted">{currency === "" ? text.noLimits : text.noLimitsForCurrency(currency)}</p>
            ) : (
              <ul className="budget-entry-list">
                {limits.map((limit) => {
                  const category = categoryMap.get(limit.category_id);
                  const archived = category?.archived === true;
                  const canToggle = !busy && !(archived && !limit.enabled);
                  return (
                    <li key={limit.id} className="budget-entry">
                      <div className="budget-limit-main">
                        <span className="budget-entry-text">
                          <strong className={limit.enabled ? undefined : "budget-muted"}>
                            {category?.name ?? limit.category_id}
                          </strong>
                          {archived ? <span className="budget-muted"> ({text.archived})</span> : null}
                        </span>
                        <strong className="budget-limit-figures">
                          {formatAmount(limit.monthly_limit, limit.currency, locale)}
                        </strong>
                      </div>
                      <div className="budget-entry-facts">
                        <span>{text.warningAt(limit.warning_percent)}</span>
                        {limit.enabled ? null : <span className="budget-chip">{text.disabled}</span>}
                        <button type="button" disabled={busy} onClick={() => { openEdit(limit); }}>{text.edit}</button>
                        <button type="button" disabled={!canToggle} onClick={() => { toggleLimit(limit); }}>
                          {limit.enabled ? text.disable : text.enable}
                        </button>
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
          </section>
        </>
      ) : null}
    </section>
  );
}
