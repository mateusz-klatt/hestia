// Monthly category spending limits (2D): organisational aids only. Server evaluates exact decimal totals
// per currency without conversion; writes require strict CAS expected_version and never touch bank accounts.
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { t } from "../../i18n";
import type { BudgetApi, BudgetResult } from "../api";
import type { BudgetAccount } from "../model";
import { formatAmount } from "../model";
import { CategorySwatch } from "./category_badge";
import { buildCategoryColors } from "./category_colors";
import type { CategoryColorResolver } from "./category_colors";
import { ANALYTICS_ERROR, DEFAULT_WARNING_PERCENT, LIMIT_STATUS_KEYS, RELOAD_ERRORS, SPENDING_LIMIT_AMOUNT_MAX,
  currentMonth, isMonth, isZero, labelOf, limitCurrencies, monthLabel, parseWarningPercent, shiftMonth, validCurrency,
  validLimitAmount } from "./model";
import type { AnalyticsCategory, AnalyticsSpendingLimit, AnalyticsSpendingLimitStatus,
  AnalyticsSpendingLimits, SpendingLimitSaveRequest } from "./model";

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
  locale: string;
  revision: number;
  onAccessLoss: Loss;
  onOpenRules?: (() => void) | undefined;
  initialMonth?: string | undefined;
  today?: () => Date;
}

function saveFailure(error: string): string {
  if (RELOAD_ERRORS.has(error)) return t("budget.limits.conflict");
  if (error === ANALYTICS_ERROR.limit) return t("budget.limits.limitCap");
  return t("budget.limits.saveError");
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
  categoryColors: CategoryColorResolver;
  locale: string;
}

function StatusPanel({ statuses, categoryMap, categoryColors, locale }: StatusPanelProps) {
  const evaluationTitle = t("budget.limits.evaluationTitle");
  const archivedText = t("budget.limits.archived");
  return (
    <section className="budget-entries" aria-label={evaluationTitle}>
      <h4>{evaluationTitle}</h4>
      {statuses.length === 0 ? <p className="budget-muted">{t("budget.limits.noStatuses")}</p> : (
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
                    <CategorySwatch color={categoryColors.get(item.category_id)} />
                    <strong>{label}</strong>
                    {item.archived ? <span className="budget-muted"> ({archivedText})</span> : null}
                  </span>
                  <span className="budget-chip budget-limit-status" data-status={item.status}>
                    {labelOf(LIMIT_STATUS_KEYS, item.status)}
                  </span>
                </div>
                <div className="budget-limit-meter">
                  <progress value={progress} max={100} aria-label={label} />
                  <span className="budget-limit-figures">
                    {t("budget.limits.spentOf", { spent, limit: cap, percent: item.percent_used_floor })}
                  </span>
                </div>
                <div className="budget-entry-facts">
                  {isZero(item.over_by) ? (
                    <span>{t("budget.limits.remaining", { amount: formatAmount(item.remaining, item.currency, locale) })}</span>
                  ) : (
                    <span className="budget-warn">
                      {t("budget.limits.overBy", { amount: formatAmount(item.over_by, item.currency, locale) })}
                    </span>
                  )}
                  <span>{t("budget.limits.expenses", { amount: formatAmount(item.expenses, item.currency, locale) })}</span>
                  {isZero(item.refunds) ? null : (
                    <span>{t("budget.limits.refunds", { amount: formatAmount(item.refunds, item.currency, locale) })}</span>
                  )}
                  <span>{t("budget.limits.warningAt", { percent: item.warning_percent })}</span>
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
  onChange: (patch: Partial<LimitDraft>) => void;
  onSubmit: (request: SpendingLimitSaveRequest) => void;
  onCancel: () => void;
}

function LimitEditor({ draft, categories, currencySuggestions, existingKeys, busy, onChange,
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
          <label>{t("budget.limits.category")}
            <select name="limit_category" value={draft.category_id} disabled={busy}
              onChange={(event) => { onChange({ category_id: event.currentTarget.value }); }}>
              <option value="">{t("budget.limits.chooseCategory")}</option>
              {activeCategories.map((item) => (
                <option key={item.id} value={item.id}>{item.name}</option>
              ))}
            </select>
          </label>
          <label>{t("budget.limits.currency")}
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
          {archivedTarget ? ` (${t("budget.limits.archived")})` : ""}
        </p>
      )}
      <label>{t("budget.limits.monthlyLimit")}
        <input name="monthly_limit" type="text" inputMode="decimal" maxLength={SPENDING_LIMIT_AMOUNT_MAX}
          value={draft.monthly_limit} disabled={busy} placeholder="500.00"
          onChange={(event) => { onChange({ monthly_limit: event.currentTarget.value }); }} />
      </label>
      <label>{t("budget.limits.warningPercent")}
        <input name="warning_percent" type="number" min={1} max={99} step={1} value={draft.warning_percent}
          disabled={busy} onChange={(event) => { onChange({ warning_percent: event.currentTarget.value }); }} />
      </label>
      <label className="budget-toggle">
        <input name="limit_enabled" type="checkbox" checked={draft.enabled}
          disabled={busy || (archivedTarget && !draft.enabled)}
          onChange={(event) => { onChange({ enabled: event.currentTarget.checked }); }} />
        {t("budget.limits.enabled")}
      </label>
      {duplicate ? <p className="budget-notice" role="alert">{t("budget.limits.duplicateHint")}</p> : null}
      {archivedTarget && draft.enabled ? <p className="budget-notice" role="alert">{t("budget.limits.archivedTarget")}</p> : null}
      {!duplicate && !(archivedTarget && draft.enabled) && request === null ? (
        <p className="budget-muted">{t("budget.limits.invalidHint")}</p>
      ) : null}
      <div className="budget-actions">
        <button type="submit" className="budget-primary" disabled={!canSave}>{t("budget.limits.save")}</button>
        <button type="button" disabled={busy} onClick={onCancel}>{t("budget.limits.cancel")}</button>
      </div>
    </form>
  );
}

export function SpendingLimitsView({ api, accounts, locale, revision, onAccessLoss, onOpenRules, initialMonth,
  today = () => new Date() }: SpendingLimitsViewProps) {
  const latest = currentMonth(today());
  const [month, setMonth] = useState(
    initialMonth !== undefined && isMonth(initialMonth) && initialMonth < latest ? initialMonth : latest);
  const [currency, setCurrency] = useState("");
  const [currencies, setCurrencies] = useState<string[]>([]);
  const [allLimitKeys, setAllLimitKeys] = useState<ReadonlySet<string>>(new Set());
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
        setAllLimitKeys(new Set(result.data.limits.map((item) => `${item.category_id}/${item.currency}`)));
      } else {
        setCurrencies((current) => (current.includes(currency) ? current
          : [...current, currency].sort((a, b) => a.localeCompare(b))));
        setAllLimitKeys((current) => {
          const next = new Set(current);
          for (const item of result.data.limits) next.add(`${item.category_id}/${item.currency}`);
          return next;
        });
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
      setNotice(saveFailure(result.error));
      return;
    }
    setAllLimitKeys((current) => new Set([...current, `${request.category_id}/${request.currency}`]));
    if (closeEditor) {
      setEditing(null);
      if (currency !== "" && request.currency !== currency) setCurrency("");
    }
    reload();
  }, [api, currency, onAccessLoss, reload]);

  const categoryMap = new Map(categories.map((item) => [item.id, item]));
  const categoryColors = buildCategoryColors(categories);
  const activeCategories = categories.filter((item) => !item.archived);
  const limits = shown.state === "ready" ? shown.data.limits : [];
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

  const title = t("budget.limits.title");
  const configuredTitle = t("budget.limits.configuredTitle");
  const archivedText = t("budget.limits.archived");

  return (
    <section className="budget-month" aria-label={title}>
      <nav className="budget-month-nav">
        <button type="button" aria-label={t("budget.limits.previous")} disabled={busy}
          onClick={() => { setMonth((value) => shiftMonth(value, -1)); }}>◀</button>
        <h3>{monthLabel(month, locale)}</h3>
        <button type="button" aria-label={t("budget.limits.next")} disabled={busy || month >= latest}
          onClick={() => { setMonth((value) => (isMonth(value) && value < latest ? shiftMonth(value, 1) : value)); }}>▶</button>
      </nav>
      <p className="budget-muted">{t("budget.limits.intro")}</p>
      {showCurrencies ? (
        <div className="budget-views" role="group" aria-label={t("budget.limits.currencyFilter")}>
          <button type="button" disabled={busy} aria-pressed={currency === ""} onClick={() => { setCurrency(""); }}>
            {t("budget.limits.allCurrencies")}
          </button>
          {currencies.map((code) => (
            <button type="button" key={code} disabled={busy} aria-pressed={currency === code} onClick={() => { setCurrency(code); }}>
              {code}
            </button>
          ))}
        </div>
      ) : null}
      {notice === null ? null : <p className="budget-notice" role="alert">{notice}</p>}
      {shown.state === "loading" ? <p className="budget-muted" role="status">{t("budget.limits.loading")}</p> : null}
      {shown.state === "error" ? <p className="budget-notice" role="alert">{t("budget.limits.error")}</p> : null}
      {shown.state === "ready" ? (
        <>
          <StatusPanel statuses={shown.data.statuses} categoryMap={categoryMap} categoryColors={categoryColors} locale={locale} />
          <section className="budget-entries" aria-label={configuredTitle}>
            <div className="budget-limit-main">
              <h4>{configuredTitle}</h4>
              {editing === null && activeCategories.length > 0 ? (
                <button type="button" disabled={busy} onClick={openNew}>{t("budget.limits.newLimit")}</button>
              ) : null}
            </div>
            {editing === null && activeCategories.length === 0 ? (
              <p className="budget-muted">
                {t("budget.limits.noCategoriesHint")}
                {onOpenRules === undefined ? null : (
                  <>
                    {" "}
                    <button type="button" className="budget-link" disabled={busy} onClick={onOpenRules}>
                      {t("budget.limits.openCategoriesLink")}
                    </button>
                  </>
                )}
              </p>
            ) : null}
            {editing === null ? null : (
              <LimitEditor draft={editing} categories={categories} currencySuggestions={suggestions}
                existingKeys={allLimitKeys} busy={busy}
                onChange={(patch) => { setEditing((current) => (current === null ? null : { ...current, ...patch })); }}
                onSubmit={(request) => { void submit(request, true); }}
                onCancel={() => { setEditing(null); }} />
            )}
            {limits.length === 0 ? (
              <p className="budget-muted">
                {currency === "" ? t("budget.limits.noLimits") : t("budget.limits.noLimitsForCurrency", { currency })}
              </p>
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
                          <CategorySwatch color={categoryColors.get(limit.category_id)} />
                          <strong className={limit.enabled ? undefined : "budget-muted"}>
                            {category?.name ?? limit.category_id}
                          </strong>
                          {archived ? <span className="budget-muted"> ({archivedText})</span> : null}
                        </span>
                        <strong className="budget-limit-figures">
                          {formatAmount(limit.monthly_limit, limit.currency, locale)}
                        </strong>
                      </div>
                      <div className="budget-entry-facts">
                        <span>{t("budget.limits.warningAt", { percent: limit.warning_percent })}</span>
                        {limit.enabled ? null : <span className="budget-chip">{t("budget.limits.disabled")}</span>}
                        <button type="button" disabled={busy} onClick={() => { openEdit(limit); }}>{t("budget.limits.edit")}</button>
                        <button type="button" disabled={!canToggle} onClick={() => { toggleLimit(limit); }}>
                          {limit.enabled ? t("budget.limits.disable") : t("budget.limits.enable")}
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
