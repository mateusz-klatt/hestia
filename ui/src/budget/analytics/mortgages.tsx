// Owner-private mortgage reminder schedules (2D, migration 0018). Strictly evaluation-only: evaluated
// against server UTC today, stores destination_kind only (zero PII targets), triggers no notifications,
// shared calendar sync or bank payments. Writes require strict CAS expected_version.
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { t, tPlural } from "../../i18n";
import type { BudgetApi, BudgetResult } from "../api";
import type { BudgetAccount } from "../model";
import { formatAmount } from "../model";
import { ANALYTICS_ERROR, DEFAULT_REMIND_DAYS_BEFORE, MORTGAGE_DESTINATIONS, MORTGAGE_DESTINATION_KEYS,
  MORTGAGE_LABEL_MAX, MORTGAGE_STATUS_KEYS, RELOAD_ERRORS, SPENDING_LIMIT_AMOUNT_MAX, isMonth, labelOf,
  mortgageCurrencies, parseIntInRange, validCurrency, validIsoDate, validLimitAmount,
  validMortgageLabel } from "./model";
import type { AnalyticsMortgageReminder, AnalyticsMortgageSchedule, AnalyticsMortgages, MortgageDestination,
  MortgageSaveRequest } from "./model";

type Loss = (result: { ok: boolean; status?: number; error?: string }) => boolean;
type Shown = { state: "loading" } | { state: "error" } | { state: "ready"; data: AnalyticsMortgages };

interface ScheduleDraft {
  schedule_id: string | null;
  label: string;
  amount: string;
  currency: string;
  due_day: string;
  start_date: string;
  end_date: string;
  remind_days_before: string;
  destination_kind: MortgageDestination;
  enabled: boolean;
  expected_version: number;
}

export interface MortgagesViewProps {
  api: Pick<BudgetApi, "mortgages" | "saveMortgage" | "setMortgagePayment">;
  accounts: readonly BudgetAccount[];
  locale: string;
  revision: number;
  onAccessLoss: Loss;
}

function saveFailure(error: string): string {
  if (RELOAD_ERRORS.has(error)) return t("budget.mortgages.conflict");
  if (error === ANALYTICS_ERROR.limit) return t("budget.mortgages.limitCap");
  if (error === ANALYTICS_ERROR.invalidRequest) return t("budget.mortgages.invalidWrite");
  return t("budget.mortgages.saveError");
}

function buildScheduleRequest(draft: ScheduleDraft): MortgageSaveRequest | null {
  const dueDay = parseIntInRange(draft.due_day, 1, 31);
  const remindDays = parseIntInRange(draft.remind_days_before, 0, 31);
  const start = draft.start_date.trim();
  const end = draft.end_date.trim();
  if (!validMortgageLabel(draft.label) || !validLimitAmount(draft.amount) || !validCurrency(draft.currency)
    || dueDay === null || remindDays === null || !validIsoDate(start)) {
    return null;
  }
  if (end !== "" && (!validIsoDate(end) || end < start)) return null;
  return {
    schedule_id: draft.schedule_id,
    label: draft.label.trim(),
    amount: draft.amount.trim(),
    currency: draft.currency.trim().toUpperCase(),
    due_day: dueDay,
    start_date: start,
    end_date: end === "" ? null : end,
    remind_days_before: remindDays,
    destination_kind: draft.destination_kind,
    enabled: draft.enabled,
    expected_version: draft.expected_version,
  };
}

function timingNote(item: AnalyticsMortgageReminder): string {
  if (item.due_date === null || item.days_until_due === null) return t("budget.mortgages.allPaid");
  if (item.days_until_due < 0) return tPlural("budget.mortgages.daysOverdue", -item.days_until_due);
  if (item.days_until_due === 0) return t("budget.mortgages.statuses.due_today");
  return tPlural("budget.mortgages.daysUntil", item.days_until_due);
}

interface RemindersPanelProps {
  reminders: readonly AnalyticsMortgageReminder[];
  scheduleMap: ReadonlyMap<string, AnalyticsMortgageSchedule>;
  busy: boolean;
  locale: string;
  onMarkPaid: (schedule: AnalyticsMortgageSchedule, month: string) => void;
}

function RemindersPanel({ reminders, scheduleMap, busy, locale, onMarkPaid }: RemindersPanelProps) {
  const remindersTitle = t("budget.mortgages.remindersTitle");
  return (
    <section className="budget-entries" aria-label={remindersTitle}>
      <h4>{remindersTitle}</h4>
      {reminders.length === 0 ? <p className="budget-muted">{t("budget.mortgages.noReminders")}</p> : (
        <ul className="budget-entry-list">
          {reminders.map((item) => {
            const schedule = scheduleMap.get(item.schedule_id);
            const month = item.due_date === null ? null : item.due_date.slice(0, 7);
            return (
              <li key={item.schedule_id} className="budget-entry">
                <div className="budget-limit-main">
                  <span className="budget-entry-text"><strong>{item.label}</strong></span>
                  <span className="budget-chip budget-mortgage-status" data-status={item.status}>
                    {labelOf(MORTGAGE_STATUS_KEYS, item.status)}
                  </span>
                </div>
                <div className="budget-entry-facts">
                  <strong className="budget-limit-figures">{formatAmount(item.amount, item.currency, locale)}</strong>
                  {item.due_date === null ? null : <span>{t("budget.mortgages.dueOn", { date: item.due_date })}</span>}
                  <span>{timingNote(item)}</span>
                  <span className="budget-chip">{labelOf(MORTGAGE_DESTINATION_KEYS, item.destination_kind)}</span>
                  {schedule !== undefined && month !== null ? (
                    <button type="button" disabled={busy} onClick={() => { onMarkPaid(schedule, month); }}>
                      {t("budget.mortgages.markMonthPaid", { month })}
                    </button>
                  ) : null}
                </div>
                {item.freshness_stale ? <p className="budget-muted">{t("budget.mortgages.freshnessStale")}</p> : null}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

interface ScheduleEditorProps {
  draft: ScheduleDraft;
  currencySuggestions: readonly string[];
  busy: boolean;
  onChange: (patch: Partial<ScheduleDraft>) => void;
  onSubmit: (request: MortgageSaveRequest) => void;
  onCancel: () => void;
}

function ScheduleEditor({ draft, currencySuggestions, busy, onChange, onSubmit, onCancel }: ScheduleEditorProps) {
  const listId = useId();
  const request = buildScheduleRequest(draft);
  const canSave = !busy && request !== null;

  return (
    <form className="budget-rule-editor" onSubmit={(event) => {
      event.preventDefault();
      if (canSave) onSubmit(request);
    }}>
      <label>{t("budget.mortgages.label")}
        <input name="mortgage_label" type="text" maxLength={MORTGAGE_LABEL_MAX} value={draft.label} disabled={busy}
          onChange={(event) => { onChange({ label: event.currentTarget.value }); }} />
      </label>
      <label>{t("budget.mortgages.amount")}
        <input name="mortgage_amount" type="text" inputMode="decimal" maxLength={SPENDING_LIMIT_AMOUNT_MAX}
          value={draft.amount} disabled={busy} placeholder="2500.00"
          onChange={(event) => { onChange({ amount: event.currentTarget.value }); }} />
      </label>
      <label>{t("budget.mortgages.currency")}
        <input name="mortgage_currency" type="text" list={listId} maxLength={3} value={draft.currency} disabled={busy}
          placeholder="PLN" onChange={(event) => { onChange({ currency: event.currentTarget.value.toUpperCase() }); }} />
        <datalist id={listId}>
          {currencySuggestions.map((code) => <option key={code} value={code} />)}
        </datalist>
      </label>
      <label>{t("budget.mortgages.dueDay")}
        <input name="mortgage_due_day" type="number" min={1} max={31} step={1} value={draft.due_day} disabled={busy}
          onChange={(event) => { onChange({ due_day: event.currentTarget.value }); }} />
      </label>
      <label>{t("budget.mortgages.startDate")}
        <input name="mortgage_start_date" type="date" value={draft.start_date} disabled={busy}
          onChange={(event) => { onChange({ start_date: event.currentTarget.value }); }} />
      </label>
      <label>{t("budget.mortgages.endDate")}
        <input name="mortgage_end_date" type="date" value={draft.end_date} min={draft.start_date || undefined} disabled={busy}
          onChange={(event) => { onChange({ end_date: event.currentTarget.value }); }} />
      </label>
      <label>{t("budget.mortgages.remindDays")}
        <input name="mortgage_remind_days" type="number" min={0} max={31} step={1} value={draft.remind_days_before}
          disabled={busy} onChange={(event) => { onChange({ remind_days_before: event.currentTarget.value }); }} />
      </label>
      <label>{t("budget.mortgages.destinationKind")}
        <select name="mortgage_destination" value={draft.destination_kind} disabled={busy}
          onChange={(event) => { onChange({ destination_kind: event.currentTarget.value as MortgageDestination }); }}>
          {MORTGAGE_DESTINATIONS.map((kind) => (
            <option key={kind} value={kind}>{labelOf(MORTGAGE_DESTINATION_KEYS, kind)}</option>
          ))}
        </select>
      </label>
      <label className="budget-toggle">
        <input name="mortgage_enabled" type="checkbox" checked={draft.enabled} disabled={busy}
          onChange={(event) => { onChange({ enabled: event.currentTarget.checked }); }} />
        {t("budget.mortgages.enabled")}
      </label>
      <p className="budget-muted">{t("budget.mortgages.destinationHelp")}</p>
      {request === null ? <p className="budget-muted">{t("budget.mortgages.invalidHint")}</p> : null}
      <div className="budget-actions">
        <button type="submit" className="budget-primary" disabled={!canSave}>{t("budget.mortgages.save")}</button>
        <button type="button" disabled={busy} onClick={onCancel}>{t("budget.mortgages.cancel")}</button>
      </div>
    </form>
  );
}

interface ScheduleRowProps {
  schedule: AnalyticsMortgageSchedule;
  busy: boolean;
  locale: string;
  onEdit: (schedule: AnalyticsMortgageSchedule) => void;
  onToggle: (schedule: AnalyticsMortgageSchedule) => void;
  onSetPayment: (schedule: AnalyticsMortgageSchedule, month: string, paid: boolean) => void;
}

function ScheduleRow({ schedule, busy, locale, onEdit, onToggle, onSetPayment }: ScheduleRowProps) {
  const [monthDraft, setMonthDraft] = useState("");
  const trimmedMonth = monthDraft.trim();
  const canMark = !busy && isMonth(trimmedMonth) && !schedule.paid_months.includes(trimmedMonth);
  const windowText = schedule.end_date === null
    ? t("budget.mortgages.windowOpen", { start: schedule.start_date })
    : t("budget.mortgages.windowRange", { start: schedule.start_date, end: schedule.end_date });
  const remindText = schedule.remind_days_before === 0
    ? t("budget.mortgages.remindOnDueDay")
    : tPlural("budget.mortgages.remindBefore", schedule.remind_days_before);

  return (
    <li className="budget-entry">
      <div className="budget-limit-main">
        <span className="budget-entry-text">
          <strong className={schedule.enabled ? undefined : "budget-muted"}>{schedule.label}</strong>
        </span>
        <strong className="budget-limit-figures">{formatAmount(schedule.amount, schedule.currency, locale)}</strong>
      </div>
      <div className="budget-entry-facts">
        <span>{t("budget.mortgages.dueDayFact", { day: schedule.due_day })}</span>
        <span>{windowText}</span>
        <span>{remindText}</span>
        <span className="budget-chip">{labelOf(MORTGAGE_DESTINATION_KEYS, schedule.destination_kind)}</span>
        {schedule.enabled ? null : <span className="budget-chip">{t("budget.mortgages.disabled")}</span>}
        <button type="button" disabled={busy} onClick={() => { onEdit(schedule); }}>{t("budget.mortgages.edit")}</button>
        <button type="button" disabled={busy} onClick={() => { onToggle(schedule); }}>
          {schedule.enabled ? t("budget.mortgages.disable") : t("budget.mortgages.enable")}
        </button>
      </div>
      <details className="budget-mortgage-paid">
        <summary>{t("budget.mortgages.paidMonths", { count: schedule.paid_months.length })}</summary>
        {schedule.paid_months.length === 0 ? <p className="budget-muted">{t("budget.mortgages.noPaidMonths")}</p> : (
          <ul className="budget-mortgage-months">
            {schedule.paid_months.map((month) => (
              <li key={`${schedule.id}/${month}`}>
                <span>{month}</span>
                <button type="button" disabled={busy}
                  onClick={() => { onSetPayment(schedule, month, false); }}>{t("budget.mortgages.unmark")}</button>
              </li>
            ))}
          </ul>
        )}
        <form className="budget-entry-editor" onSubmit={(event) => {
          event.preventDefault();
          if (canMark) {
            onSetPayment(schedule, trimmedMonth, true);
            setMonthDraft("");
          }
        }}>
          <label>{t("budget.mortgages.monthInput")}
            <input name="paid_month" type="text" maxLength={7} placeholder="2026-11" value={monthDraft}
              disabled={busy} onChange={(event) => { setMonthDraft(event.currentTarget.value); }} />
          </label>
          <button type="submit" disabled={!canMark}>{t("budget.mortgages.markPaid")}</button>
        </form>
      </details>
    </li>
  );
}

export function MortgagesView({ api, accounts, locale, revision, onAccessLoss }: MortgagesViewProps) {
  const [shown, setShown] = useState<Shown>({ state: "loading" });
  const [changes, setChanges] = useState(0);
  const [editing, setEditing] = useState<ScheduleDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const generation = useRef(0);

  useEffect(() => {
    const token = ++generation.current;
    setShown({ state: "loading" });
    setBusy(false);
    void api.mortgages().then((result) => {
      // Run the access barrier before dropping superseded answers so a 401/403/404 always clears the view.
      if (onAccessLoss(result) || token !== generation.current) return;
      setShown(result.ok ? { state: "ready", data: result.data } : { state: "error" });
    });
  }, [api, revision, changes, onAccessLoss]);
  useEffect(() => () => { generation.current += 1; }, []);

  const reload = useCallback(() => { setChanges((current) => current + 1); }, []);
  const mutate = useCallback(async <T,>(action: () => Promise<BudgetResult<T>>, closeEditor: boolean) => {
    const token = generation.current;
    setBusy(true);
    setNotice(null);
    const result = await action();
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
    if (closeEditor) setEditing(null);
    reload();
  }, [onAccessLoss, reload]);

  const schedules = shown.state === "ready" ? shown.data.schedules : [];
  const scheduleMap = new Map(schedules.map((item) => [item.id, item]));
  const suggestions = mortgageCurrencies(schedules, accounts);
  const defaultCurrency = suggestions[0] ?? "PLN";
  const defaultStart = shown.state === "ready" ? shown.data.evaluation_date : "";

  const openNew = () => {
    setNotice(null);
    setEditing({
      schedule_id: null,
      label: "",
      amount: "",
      currency: defaultCurrency,
      due_day: "10",
      start_date: defaultStart,
      end_date: "",
      remind_days_before: String(DEFAULT_REMIND_DAYS_BEFORE),
      destination_kind: "owner_inbox",
      enabled: true,
      expected_version: 0,
    });
  };
  const openEdit = (schedule: AnalyticsMortgageSchedule) => {
    setNotice(null);
    setEditing({
      schedule_id: schedule.id,
      label: schedule.label,
      amount: schedule.amount,
      currency: schedule.currency,
      due_day: String(schedule.due_day),
      start_date: schedule.start_date,
      end_date: schedule.end_date ?? "",
      remind_days_before: String(schedule.remind_days_before),
      destination_kind: schedule.destination_kind,
      enabled: schedule.enabled,
      expected_version: schedule.version,
    });
  };
  const toggleSchedule = (schedule: AnalyticsMortgageSchedule) => {
    void mutate(() => api.saveMortgage({
      schedule_id: schedule.id,
      label: schedule.label,
      amount: schedule.amount,
      currency: schedule.currency,
      due_day: schedule.due_day,
      start_date: schedule.start_date,
      end_date: schedule.end_date,
      remind_days_before: schedule.remind_days_before,
      destination_kind: schedule.destination_kind,
      enabled: !schedule.enabled,
      expected_version: schedule.version,
    }), false);
  };
  const setPayment = (schedule: AnalyticsMortgageSchedule, month: string, paid: boolean) => {
    void mutate(() => api.setMortgagePayment({
      schedule_id: schedule.id,
      month,
      paid,
      expected_version: schedule.version,
    }), false);
  };

  const title = t("budget.mortgages.title");
  const schedulesTitle = t("budget.mortgages.schedulesTitle");
  const ledgerAsOf = shown.state !== "ready" ? "" : shown.data.ledger_as_of === null
    ? t("budget.mortgages.ledgerAsOfEmpty")
    : t("budget.mortgages.ledgerAsOfDate", { date: shown.data.ledger_as_of });

  return (
    <section className="budget-month" aria-label={title}>
      <p className="budget-muted">{t("budget.mortgages.intro")}</p>
      {shown.state === "ready" ? (
        <div className="budget-entry-facts">
          <span>
            {t("budget.mortgages.evaluatedFor", {
              date: shown.data.evaluation_date,
              tz: shown.data.evaluation_timezone,
            })}
          </span>
          <span>{ledgerAsOf}</span>
        </div>
      ) : null}
      {notice === null ? null : <p className="budget-notice" role="alert">{notice}</p>}
      {shown.state === "loading" ? <p className="budget-muted" role="status">{t("budget.mortgages.loading")}</p> : null}
      {shown.state === "error" ? <p className="budget-notice" role="alert">{t("budget.mortgages.error")}</p> : null}
      {shown.state === "ready" ? (
        <>
          <RemindersPanel reminders={shown.data.reminders} scheduleMap={scheduleMap} busy={busy}
            locale={locale} onMarkPaid={(schedule, month) => { setPayment(schedule, month, true); }} />
          <section className="budget-entries" aria-label={schedulesTitle}>
            <div className="budget-limit-main">
              <h4>{schedulesTitle}</h4>
              {editing === null ? (
                <button type="button" disabled={busy} onClick={openNew}>{t("budget.mortgages.newSchedule")}</button>
              ) : null}
            </div>
            {editing === null ? null : (
              <ScheduleEditor draft={editing} currencySuggestions={suggestions} busy={busy}
                onChange={(patch) => { setEditing((current) => (current === null ? null : { ...current, ...patch })); }}
                onSubmit={(request) => { void mutate(() => api.saveMortgage(request), true); }}
                onCancel={() => { setEditing(null); }} />
            )}
            {schedules.length === 0 ? <p className="budget-muted">{t("budget.mortgages.noSchedules")}</p> : (
              <ul className="budget-entry-list">
                {schedules.map((schedule) => (
                  <ScheduleRow key={schedule.id} schedule={schedule} busy={busy} locale={locale}
                    onEdit={openEdit} onToggle={toggleSchedule} onSetPayment={setPayment} />
                ))}
              </ul>
            )}
          </section>
        </>
      ) : null}
    </section>
  );
}
