// Owner-private mortgage reminder schedules (2D, migration 0018). Strictly evaluation-only: evaluated
// against server UTC today, stores destination_kind only (zero PII targets), triggers no notifications,
// shared calendar sync or bank payments. Writes require strict CAS expected_version.
import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { BudgetApi, BudgetResult } from "../api";
import type { BudgetAccount } from "../model";
import { formatAmount } from "../model";
import { ANALYTICS_ERROR, DEFAULT_REMIND_DAYS_BEFORE, MORTGAGE_DESTINATIONS, MORTGAGE_LABEL_MAX, RELOAD_ERRORS,
  SPENDING_LIMIT_AMOUNT_MAX, isMonth, labelOf, mortgageCurrencies, parseIntInRange, pluralPl, validCurrency,
  validIsoDate, validLimitAmount, validMortgageLabel } from "./model";
import type { AnalyticsMortgageReminder, AnalyticsMortgageSchedule, AnalyticsMortgages, MortgageDestination,
  MortgageSaveRequest } from "./model";

export const mortgagesLabels = {
  en: {
    title: "Mortgage reminders",
    intro: "Private mortgage reminder schedules evaluated strictly on the server's UTC date. Evaluation only — no notifications, shared calendars or bank payments are triggered here.",
    evaluatedFor: (date: string, tz: string) => `Evaluated for ${date} (${tz})`,
    ledgerAsOf: (date: string | null) => (date === null ? "No synced ledger data yet" : `Ledger data as of ${date}`),
    freshnessStale: "Ledger data may be outdated — confirm payment status directly before acting.",
    loading: "Loading mortgage reminders…",
    error: "Mortgage reminders could not be loaded. Try again.",
    remindersTitle: "Upcoming & overdue reminders",
    noReminders: "No active mortgage reminders.",
    schedulesTitle: "Configured schedules",
    noSchedules: "No private mortgage schedules configured yet.",
    newSchedule: "New schedule",
    edit: "Edit",
    save: "Save schedule",
    cancel: "Cancel",
    disable: "Disable",
    enable: "Enable",
    enabled: "Enabled",
    disabled: "disabled",
    label: "Schedule label",
    amount: "Installment amount",
    currency: "Currency",
    dueDay: "Due day of month (1–31)",
    startDate: "Start date",
    endDate: "End date (optional)",
    remindDays: "Remind days before (0–31)",
    destinationKind: "Private destination type",
    destinationHelp: "Only the private channel type is stored — never an email address, phone number or shared calendar.",
    destinations: {
      owner_inbox: "Owner inbox",
      owner_push: "Owner push",
      owner_email: "Owner email",
    },
    statuses: {
      overdue: "Overdue",
      due_today: "Due today",
      due_soon: "Due soon",
      upcoming: "Upcoming",
      completed: "Completed",
    },
    dueOn: (date: string) => `Due ${date}`,
    daysOverdue: (days: number) => `${String(days)} ${days === 1 ? "day" : "days"} overdue`,
    daysUntil: (days: number) => `In ${String(days)} ${days === 1 ? "day" : "days"}`,
    allPaid: "All scheduled installments marked paid",
    dueDayFact: (day: number) => `Day ${String(day)} of month`,
    windowFact: (start: string, end: string | null) => (end === null ? `From ${start} (no end date)` : `${start} – ${end}`),
    remindFact: (days: number) => (days === 0 ? "Remind on due day" : `Remind ${String(days)} ${days === 1 ? "day" : "days"} before`),
    markMonthPaid: (month: string) => `Mark ${month} paid`,
    paidMonths: (count: number) => `Paid installments (${String(count)})`,
    noPaidMonths: "No installments marked paid yet.",
    unmark: "Unmark",
    monthInput: "Month (YYYY-MM)",
    markPaid: "Mark paid",
    invalidHint: "Fill in a label, positive amount, 3-letter currency, due day (1–31), valid start date (and end date on or after start), and lead days (0–31).",
    conflict: "This changed elsewhere; the latest state was loaded.",
    limitCap: "Maximum number of mortgage schedules or paid months reached.",
    invalidWrite: "The server rejected this change. Check the date range or installment month and try again.",
    saveError: "Could not save mortgage change. Try again.",
  },
  pl: {
    title: "Przypomnienia hipoteczne",
    intro: "Prywatne harmonogramy przypomnień hipotecznych liczone wyłącznie według daty UTC serwera. Tylko ocena — nie wysyła powiadomień, nie synchronizuje wspólnych kalendarzy i nie wykonuje płatności bankowych.",
    evaluatedFor: (date: string, tz: string) => `Stan na ${date} (${tz})`,
    ledgerAsOf: (date: string | null) => (date === null ? "Brak zsynchronizowanych danych księgowych" : `Dane księgowe na dzień ${date}`),
    freshnessStale: "Dane księgowe mogą być nieaktualne — przed działaniem potwierdź status płatności osobno.",
    loading: "Wczytywanie przypomnień hipotecznych…",
    error: "Nie udało się wczytać przypomnień hipotecznych. Spróbuj ponownie.",
    remindersTitle: "Nadchodzące i zaległe raty",
    noReminders: "Brak aktywnych przypomnień hipotecznych.",
    schedulesTitle: "Skonfigurowane harmonogramy",
    noSchedules: "Nie skonfigurowano jeszcze prywatnych harmonogramów hipotecznych.",
    newSchedule: "Nowy harmonogram",
    edit: "Edytuj",
    save: "Zapisz harmonogram",
    cancel: "Anuluj",
    disable: "Wyłącz",
    enable: "Włącz",
    enabled: "Włączony",
    disabled: "wyłączony",
    label: "Nazwa harmonogramu",
    amount: "Kwota raty",
    currency: "Waluta",
    dueDay: "Dzień płatności w miesiącu (1–31)",
    startDate: "Data początkowa",
    endDate: "Data końcowa (opcjonalnie)",
    remindDays: "Przypomnienie dni wcześniej (0–31)",
    destinationKind: "Prywatny kanał docelowy",
    destinationHelp: "Zapisywany jest wyłącznie typ prywatnego kanału — nigdy adres e-mail, numer telefonu ani wspólny kalendarz.",
    destinations: {
      owner_inbox: "Skrzynka właściciela",
      owner_push: "Push właściciela",
      owner_email: "E-mail właściciela",
    },
    statuses: {
      overdue: "Po terminie",
      due_today: "Termin dziś",
      due_soon: "Wkrótce termin",
      upcoming: "Nadchodząca",
      completed: "Zakończona",
    },
    dueOn: (date: string) => `Termin ${date}`,
    daysOverdue: (days: number) => `${String(days)} ${pluralPl(days, "dzień", "dni", "dni")} po terminie`,
    daysUntil: (days: number) => `Za ${String(days)} ${pluralPl(days, "dzień", "dni", "dni")}`,
    allPaid: "Wszystkie zaplanowane raty oznaczone jako opłacone",
    dueDayFact: (day: number) => `Dzień miesiąca: ${String(day)}`,
    windowFact: (start: string, end: string | null) => (end === null ? `Od ${start} (bez daty końcowej)` : `${start} – ${end}`),
    remindFact: (days: number) => (days === 0
      ? "Przypomnienie w dniu terminu"
      : `Przypomnienie ${String(days)} ${pluralPl(days, "dzień", "dni", "dni")} wcześniej`),
    markMonthPaid: (month: string) => `Oznacz ${month} jako opłaconą`,
    paidMonths: (count: number) => `Opłacone raty (${String(count)})`,
    noPaidMonths: "Żadna rata nie została jeszcze oznaczona jako opłacona.",
    unmark: "Cofnij",
    monthInput: "Miesiąc (RRRR-MM)",
    markPaid: "Oznacz opłaconą",
    invalidHint: "Podaj nazwę, dodatnią kwotę, 3-literową walutę, dzień (1–31), poprawną datę początkową (i końcową nie wcześniejszą niż początkowa) oraz dni wyprzedzenia (0–31).",
    conflict: "Zmieniono to w innym miejscu; wczytano aktualny stan.",
    limitCap: "Osiągnięto maksymalną liczbę harmonogramów lub oznaczonych rat.",
    invalidWrite: "Serwer odrzucił zmianę. Sprawdź zakres dat lub miesiąc raty i spróbuj ponownie.",
    saveError: "Nie udało się zapisać zmiany. Spróbuj ponownie.",
  },
};
export type MortgagesLabels = typeof mortgagesLabels.en;

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
  text: MortgagesLabels;
  locale: string;
  revision: number;
  onAccessLoss: Loss;
}

function saveFailure(text: MortgagesLabels, error: string): string {
  if (RELOAD_ERRORS.has(error)) return text.conflict;
  if (error === ANALYTICS_ERROR.limit) return text.limitCap;
  if (error === ANALYTICS_ERROR.invalidRequest) return text.invalidWrite;
  return text.saveError;
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

function timingNote(item: AnalyticsMortgageReminder, text: MortgagesLabels): string {
  if (item.due_date === null || item.days_until_due === null) return text.allPaid;
  if (item.days_until_due < 0) return text.daysOverdue(-item.days_until_due);
  if (item.days_until_due === 0) return text.statuses.due_today;
  return text.daysUntil(item.days_until_due);
}

interface RemindersPanelProps {
  reminders: readonly AnalyticsMortgageReminder[];
  scheduleMap: ReadonlyMap<string, AnalyticsMortgageSchedule>;
  busy: boolean;
  text: MortgagesLabels;
  locale: string;
  onMarkPaid: (schedule: AnalyticsMortgageSchedule, month: string) => void;
}

function RemindersPanel({ reminders, scheduleMap, busy, text, locale, onMarkPaid }: RemindersPanelProps) {
  return (
    <section className="budget-entries" aria-label={text.remindersTitle}>
      <h4>{text.remindersTitle}</h4>
      {reminders.length === 0 ? <p className="budget-muted">{text.noReminders}</p> : (
        <ul className="budget-entry-list">
          {reminders.map((item) => {
            const schedule = scheduleMap.get(item.schedule_id);
            const month = item.due_date === null ? null : item.due_date.slice(0, 7);
            return (
              <li key={item.schedule_id} className="budget-entry">
                <div className="budget-limit-main">
                  <span className="budget-entry-text"><strong>{item.label}</strong></span>
                  <span className="budget-chip budget-mortgage-status" data-status={item.status}>
                    {labelOf(text.statuses, item.status)}
                  </span>
                </div>
                <div className="budget-entry-facts">
                  <strong className="budget-limit-figures">{formatAmount(item.amount, item.currency, locale)}</strong>
                  {item.due_date === null ? null : <span>{text.dueOn(item.due_date)}</span>}
                  <span>{timingNote(item, text)}</span>
                  <span className="budget-chip">{labelOf(text.destinations, item.destination_kind)}</span>
                  {schedule !== undefined && month !== null ? (
                    <button type="button" disabled={busy} onClick={() => { onMarkPaid(schedule, month); }}>
                      {text.markMonthPaid(month)}
                    </button>
                  ) : null}
                </div>
                {item.freshness_stale ? <p className="budget-muted">{text.freshnessStale}</p> : null}
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
  text: MortgagesLabels;
  onChange: (patch: Partial<ScheduleDraft>) => void;
  onSubmit: (request: MortgageSaveRequest) => void;
  onCancel: () => void;
}

function ScheduleEditor({ draft, currencySuggestions, busy, text, onChange, onSubmit, onCancel }: ScheduleEditorProps) {
  const listId = useId();
  const request = buildScheduleRequest(draft);
  const canSave = !busy && request !== null;

  return (
    <form className="budget-rule-editor" onSubmit={(event) => {
      event.preventDefault();
      if (canSave) onSubmit(request);
    }}>
      <label>{text.label}
        <input name="mortgage_label" type="text" maxLength={MORTGAGE_LABEL_MAX} value={draft.label} disabled={busy}
          onChange={(event) => { onChange({ label: event.currentTarget.value }); }} />
      </label>
      <label>{text.amount}
        <input name="mortgage_amount" type="text" inputMode="decimal" maxLength={SPENDING_LIMIT_AMOUNT_MAX}
          value={draft.amount} disabled={busy} placeholder="2500.00"
          onChange={(event) => { onChange({ amount: event.currentTarget.value }); }} />
      </label>
      <label>{text.currency}
        <input name="mortgage_currency" type="text" list={listId} maxLength={3} value={draft.currency} disabled={busy}
          placeholder="PLN" onChange={(event) => { onChange({ currency: event.currentTarget.value.toUpperCase() }); }} />
        <datalist id={listId}>
          {currencySuggestions.map((code) => <option key={code} value={code} />)}
        </datalist>
      </label>
      <label>{text.dueDay}
        <input name="mortgage_due_day" type="number" min={1} max={31} step={1} value={draft.due_day} disabled={busy}
          onChange={(event) => { onChange({ due_day: event.currentTarget.value }); }} />
      </label>
      <label>{text.startDate}
        <input name="mortgage_start_date" type="date" value={draft.start_date} disabled={busy}
          onChange={(event) => { onChange({ start_date: event.currentTarget.value }); }} />
      </label>
      <label>{text.endDate}
        <input name="mortgage_end_date" type="date" value={draft.end_date} min={draft.start_date || undefined} disabled={busy}
          onChange={(event) => { onChange({ end_date: event.currentTarget.value }); }} />
      </label>
      <label>{text.remindDays}
        <input name="mortgage_remind_days" type="number" min={0} max={31} step={1} value={draft.remind_days_before}
          disabled={busy} onChange={(event) => { onChange({ remind_days_before: event.currentTarget.value }); }} />
      </label>
      <label>{text.destinationKind}
        <select name="mortgage_destination" value={draft.destination_kind} disabled={busy}
          onChange={(event) => { onChange({ destination_kind: event.currentTarget.value as MortgageDestination }); }}>
          {MORTGAGE_DESTINATIONS.map((kind) => (
            <option key={kind} value={kind}>{labelOf(text.destinations, kind)}</option>
          ))}
        </select>
      </label>
      <label className="budget-toggle">
        <input name="mortgage_enabled" type="checkbox" checked={draft.enabled} disabled={busy}
          onChange={(event) => { onChange({ enabled: event.currentTarget.checked }); }} />
        {text.enabled}
      </label>
      <p className="budget-muted">{text.destinationHelp}</p>
      {request === null ? <p className="budget-muted">{text.invalidHint}</p> : null}
      <div className="budget-actions">
        <button type="submit" className="budget-primary" disabled={!canSave}>{text.save}</button>
        <button type="button" disabled={busy} onClick={onCancel}>{text.cancel}</button>
      </div>
    </form>
  );
}

interface ScheduleRowProps {
  schedule: AnalyticsMortgageSchedule;
  busy: boolean;
  text: MortgagesLabels;
  locale: string;
  onEdit: (schedule: AnalyticsMortgageSchedule) => void;
  onToggle: (schedule: AnalyticsMortgageSchedule) => void;
  onSetPayment: (schedule: AnalyticsMortgageSchedule, month: string, paid: boolean) => void;
}

function ScheduleRow({ schedule, busy, text, locale, onEdit, onToggle, onSetPayment }: ScheduleRowProps) {
  const [monthDraft, setMonthDraft] = useState("");
  const trimmedMonth = monthDraft.trim();
  const canMark = !busy && isMonth(trimmedMonth) && !schedule.paid_months.includes(trimmedMonth);

  return (
    <li className="budget-entry">
      <div className="budget-limit-main">
        <span className="budget-entry-text">
          <strong className={schedule.enabled ? undefined : "budget-muted"}>{schedule.label}</strong>
        </span>
        <strong className="budget-limit-figures">{formatAmount(schedule.amount, schedule.currency, locale)}</strong>
      </div>
      <div className="budget-entry-facts">
        <span>{text.dueDayFact(schedule.due_day)}</span>
        <span>{text.windowFact(schedule.start_date, schedule.end_date)}</span>
        <span>{text.remindFact(schedule.remind_days_before)}</span>
        <span className="budget-chip">{labelOf(text.destinations, schedule.destination_kind)}</span>
        {schedule.enabled ? null : <span className="budget-chip">{text.disabled}</span>}
        <button type="button" disabled={busy} onClick={() => { onEdit(schedule); }}>{text.edit}</button>
        <button type="button" disabled={busy} onClick={() => { onToggle(schedule); }}>
          {schedule.enabled ? text.disable : text.enable}
        </button>
      </div>
      <details className="budget-mortgage-paid">
        <summary>{text.paidMonths(schedule.paid_months.length)}</summary>
        {schedule.paid_months.length === 0 ? <p className="budget-muted">{text.noPaidMonths}</p> : (
          <ul className="budget-mortgage-months">
            {schedule.paid_months.map((month) => (
              <li key={`${schedule.id}/${month}`}>
                <span>{month}</span>
                <button type="button" disabled={busy}
                  onClick={() => { onSetPayment(schedule, month, false); }}>{text.unmark}</button>
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
          <label>{text.monthInput}
            <input name="paid_month" type="text" maxLength={7} placeholder="2026-11" value={monthDraft}
              disabled={busy} onChange={(event) => { setMonthDraft(event.currentTarget.value); }} />
          </label>
          <button type="submit" disabled={!canMark}>{text.markPaid}</button>
        </form>
      </details>
    </li>
  );
}

export function MortgagesView({ api, accounts, text, locale, revision, onAccessLoss }: MortgagesViewProps) {
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
      setNotice(saveFailure(text, result.error));
      return;
    }
    if (closeEditor) setEditing(null);
    reload();
  }, [onAccessLoss, reload, text]);

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

  return (
    <section className="budget-month" aria-label={text.title}>
      <p className="budget-muted">{text.intro}</p>
      {shown.state === "ready" ? (
        <div className="budget-entry-facts">
          <span>{text.evaluatedFor(shown.data.evaluation_date, shown.data.evaluation_timezone)}</span>
          <span>{text.ledgerAsOf(shown.data.ledger_as_of)}</span>
        </div>
      ) : null}
      {notice === null ? null : <p className="budget-notice" role="alert">{notice}</p>}
      {shown.state === "loading" ? <p className="budget-muted" role="status">{text.loading}</p> : null}
      {shown.state === "error" ? <p className="budget-notice" role="alert">{text.error}</p> : null}
      {shown.state === "ready" ? (
        <>
          <RemindersPanel reminders={shown.data.reminders} scheduleMap={scheduleMap} busy={busy} text={text}
            locale={locale} onMarkPaid={(schedule, month) => { setPayment(schedule, month, true); }} />
          <section className="budget-entries" aria-label={text.schedulesTitle}>
            <div className="budget-limit-main">
              <h4>{text.schedulesTitle}</h4>
              {editing === null ? (
                <button type="button" disabled={busy} onClick={openNew}>{text.newSchedule}</button>
              ) : null}
            </div>
            {editing === null ? null : (
              <ScheduleEditor draft={editing} currencySuggestions={suggestions} busy={busy} text={text}
                onChange={(patch) => { setEditing((current) => (current === null ? null : { ...current, ...patch })); }}
                onSubmit={(request) => { void mutate(() => api.saveMortgage(request), true); }}
                onCancel={() => { setEditing(null); }} />
            )}
            {schedules.length === 0 ? <p className="budget-muted">{text.noSchedules}</p> : (
              <ul className="budget-entry-list">
                {schedules.map((schedule) => (
                  <ScheduleRow key={schedule.id} schedule={schedule} busy={busy} text={text} locale={locale}
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
