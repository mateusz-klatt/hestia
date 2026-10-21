import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { SyntheticEvent } from "react";
import { createRoot } from "react-dom/client";
import { apiUrl } from "./api/client";
import { currentLocale } from "./i18n";
import {
  changeDraftAllDay, dateKey, draftPayload, eventDraft, localTime, moveDraftStart, onDay, shiftDay, weekStart,
} from "./calendar-model";
import type { CalendarData, CalendarEvent, EventDraft } from "./calendar-model";
import "./calendar.css";

const labels = {
  en: {
    title: "Your shared calendar", subtitle: "A little more time, together.", group: "Calendar group",
    add: "Add event", refresh: "Refresh", today: "Today", previous: "Previous week", next: "Next week",
    loading: "Loading calendar…", disabled: "Calendar synchronization is not configured.",
    setup: "Connect selected Apple/iCloud and Google calendars in the server configuration. Setup: docs/CALENDAR_SYNC.md.",
    noGroups: "No calendar groups selected. Choose calendars in the server configuration.",
    empty: "Nothing planned", allDay: "All day", readonly: "Read only — your account cannot change events.",
    error: "Calendar could not be loaded. Your last view is kept. Try refreshing.",
    saveError: "Could not save. Check the event times, connection and permissions, then retry.",
    conflict: "This event changed elsewhere. Your draft is kept. Cancel and reopen the event to use its latest version.",
    saved: "Saved locally. Calendar copies will update during synchronization.",
    deleted: "Deleted locally. Deletion of the calendar copies is queued.",
    details: "Event details", close: "Close",
    edit: "Edit event", new: "New event", titleField: "Title", place: "Location", description: "Notes",
    endUnknown: "End time unknown",
    endUnknownHelp: "Only the start is known. A displayed compatibility end does not set a duration.",
    start: "Starts", end: "Ends", exclusive: "End date (exclusive)", timezone: "Time zone (IANA)",
    recurrence: "Recurrence rules (optional)", recurrenceHelp: "One RRULE, EXDATE or RDATE per line. Changes apply to the whole series; existing exceptions are preserved. Removing all rules also removes the exceptions.",
    typeLocked: "Changing between all-day and timed events is disabled for recurring series and occurrence exceptions. Remove recurrence, save and reopen the event before changing its type.",
    offset: "UTC offset (optional)", startOffset: "Start offset", endOffset: "End offset",
    offsetHelp: "For an ambiguous autumn time, specify +02:00 or +01:00. The offset must match the IANA zone. Nonexistent spring times cannot be saved.",
    offsetMismatch: "The UTC offset does not match this local date and time in the selected time zone. Check the daylight-saving transition.",
    googleRestricted: "Google restricts changes to this Gmail or Contacts event. Edit it in its source or use a regular calendar event. The local change remains queued.",
    permission: "Calendar access is restricted. Check account authorization and sharing permissions for full read and write access.",
    exceptions: "Existing occurrence exceptions", save: "Save event", saving: "Saving…", cancel: "Cancel",
    delete: "Delete event", confirm: "Delete this event and all its calendar copies? For a recurring event this deletes the whole series.",
    recurring: "Recurring series", recurringHelp: "Series are shown below with their original start. Individual occurrences are not expanded in this view.",
    sync: "Synchronization", queued: "pending operations", never: "Waiting for first synchronization", last: "Last synchronized",
    invalid: "Enter a title, a valid time zone and valid event times. Timed events may have equal start and end; all-day end dates must be later.", off: "Disabled endpoint",
  },
  pl: {
    title: "Wasz wspólny kalendarz", subtitle: "Więcej czasu razem.", group: "Grupa kalendarzy",
    add: "Dodaj wydarzenie", refresh: "Odśwież", today: "Dzisiaj", previous: "Poprzedni tydzień", next: "Następny tydzień",
    loading: "Wczytywanie kalendarza…", disabled: "Synchronizacja kalendarzy nie jest skonfigurowana.",
    setup: "Połącz wybrane kalendarze Apple/iCloud i Google w konfiguracji serwera. Instrukcja: docs/CALENDAR_SYNC.md.",
    noGroups: "Nie wybrano grup kalendarzy. Wskaż kalendarze w konfiguracji serwera.",
    empty: "Bez planów", allDay: "Cały dzień", readonly: "Tylko odczyt — Twoje konto nie może zmieniać wydarzeń.",
    error: "Nie udało się wczytać kalendarza. Poprzedni widok został zachowany. Spróbuj odświeżyć.",
    saveError: "Nie udało się zapisać. Sprawdź godziny, połączenie i uprawnienia, następnie spróbuj ponownie.",
    conflict: "To wydarzenie zmieniono w innym miejscu. Szkic został zachowany. Anuluj i otwórz wydarzenie ponownie, aby użyć najnowszej wersji.",
    saved: "Zapisano lokalnie. Kopie kalendarza zostaną zaktualizowane podczas synchronizacji.",
    deleted: "Usunięto lokalnie. Usunięcie pozostałych kopii oczekuje w kolejce.",
    details: "Szczegóły wydarzenia", close: "Zamknij",
    edit: "Edytuj wydarzenie", new: "Nowe wydarzenie", titleField: "Tytuł", place: "Miejsce", description: "Notatki",
    endUnknown: "Nieznany czas zakończenia",
    endUnknownHelp: "Znany jest tylko początek. Techniczna data końca nie określa długości wydarzenia.",
    start: "Początek", end: "Koniec", exclusive: "Data końca (wyłączna)", timezone: "Strefa czasowa (IANA)",
    recurrence: "Reguły cykliczności (opcjonalnie)", recurrenceHelp: "Jedna reguła RRULE, EXDATE lub RDATE w wierszu. Edytujesz całą serię; istniejące wyjątki zostają zachowane. Usunięcie wszystkich reguł usuwa też wyjątki.",
    typeLocked: "Zmiana między wydarzeniem całodniowym a godzinowym jest zablokowana dla serii i wyjątków wystąpień. Usuń cykliczność, zapisz i otwórz wydarzenie ponownie, aby zmienić jego typ.",
    offset: "Przesunięcie UTC (opcjonalnie)", startOffset: "Przesunięcie początku", endOffset: "Przesunięcie końca",
    offsetHelp: "Dla dwuznacznej godziny jesiennej podaj +02:00 lub +01:00. Przesunięcie musi odpowiadać strefie IANA. Nieistniejące godziny wiosenne nie mogą zostać zapisane.",
    offsetMismatch: "Przesunięcie UTC nie odpowiada tej dacie i godzinie w wybranej strefie. Sprawdź zmianę czasu.",
    googleRestricted: "Google ogranicza zmiany tego wydarzenia z Gmaila lub Kontaktów. Zmień je w źródle albo użyj zwykłego wydarzenia. Lokalna zmiana pozostaje w kolejce.",
    permission: "Ograniczony dostęp do kalendarza. Sprawdź autoryzację konta i udostępnianie z pełnymi uprawnieniami odczytu i zapisu.",
    exceptions: "Istniejące wyjątki wystąpień", save: "Zapisz wydarzenie", saving: "Zapisywanie…", cancel: "Anuluj",
    delete: "Usuń wydarzenie", confirm: "Usunąć wydarzenie i wszystkie jego kopie? Wydarzenie cykliczne zostanie usunięte z całą serią.",
    recurring: "Wydarzenia cykliczne", recurringHelp: "Poniżej pokazano serie z datą pierwszego wystąpienia. Ten widok nie rozwija pojedynczych wystąpień.",
    sync: "Synchronizacja", queued: "oczekujących operacji", never: "Oczekiwanie na pierwszą synchronizację", last: "Ostatnia synchronizacja",
    invalid: "Podaj tytuł, poprawną strefę i czas wydarzenia. Początek i koniec mogą być równe dla wydarzenia godzinowego; całodniowa data końca musi być późniejsza.", off: "Wyłączony kalendarz",
  },
};
type Copy = typeof labels.en;

async function readCalendar(group: string): Promise<CalendarData> {
  const url = apiUrl("calendars");
  if (group !== "") url.searchParams.set("group", group);
  const response = await fetch(url);
  if (!response.ok) throw new Error("calendar_load_failed");
  return await response.json() as CalendarData;
}

function createIdempotencyKey(): string {
  // Unlike randomUUID, getRandomValues also supports HTTP LAN installations.
  return Array.from(crypto.getRandomValues(new Uint8Array(32)),
    (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function EventEditor({ event, group, day, copy, onClose, onSaved, onConflict }: {
  event: CalendarEvent | null; group: string; day: string; copy: Copy;
  onClose: () => void; onSaved: (message: string) => void; onConflict: () => Promise<void>;
}) {
  const [editing, setEditing] = useState(() => {
    const draft = eventDraft(event, day);
    return { draft, startAnchor: draft.start };
  });
  const { draft } = editing;
  const [idempotencyKey] = useState(() => event === null ? createIdempotencyKey() : null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const title = useRef<HTMLInputElement>(null);
  const mounted = useRef(false);
  const typeNote = useId();
  const typeLocked = draft.recurrence.trim() !== "" || (event?.payload.exceptions?.length ?? 0) > 0;
  useEffect(() => {
    mounted.current = true;
    title.current?.focus();
    return () => { mounted.current = false; };
  }, []);
  const field = <K extends keyof EventDraft,>(name: K, value: EventDraft[K]): void => {
    setEditing((old) => old.draft[name] === value ? old : ({ ...old, draft: { ...old.draft, [name]: value,
      ...(name === "timezone" ? { startOffset: "" } : {}),
      ...(name === "end" || name === "timezone" ? { endOffset: "" } : {}),
    } }));
  };
  const submit = async (remove: boolean): Promise<void> => {
    if (remove && !window.confirm(copy.confirm)) return;
    if (!remove) {
      try {
        new Intl.DateTimeFormat("en", { timeZone: draft.timezone }).format();
        const explicitTimes = !draft.allDay && draft.startOffset !== "" && draft.endOffset !== "";
        const badOrder = draft.endUnknown ? false : draft.allDay ? draft.end <= draft.start : explicitTimes
          ? Date.parse(`${draft.end}:00${draft.endOffset}`) < Date.parse(`${draft.start}:00${draft.startOffset}`)
          : draft.startOffset === "" && draft.endOffset === "" && draft.end < draft.start;
        if (draft.summary.trim() === "" || badOrder
            || (draft.endUnknown ? [draft.startOffset] : [draft.startOffset, draft.endOffset]).some((offset) => offset !== "" && !/^(Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/.test(offset))) throw new Error("invalid");
        const times = [{ value: draft.start, offset: draft.startOffset },
          ...(draft.endUnknown ? [] : [{ value: draft.end, offset: draft.endOffset }])];
        if (!draft.allDay && times.some(({ value, offset }) => offset !== ""
            && localTime({ dateTime: `${value}:00${offset}` }, draft.timezone) !== value)) {
          setError(copy.offsetMismatch);
          return;
        }
      } catch {
        setError(copy.invalid);
        return;
      }
    }
    setBusy(true);
    setError("");
    try {
      const body = remove ? { group, id: event?.id, revision: event?.revision }
        : { group, payload: draftPayload(draft, event), ...(event === null ? { idempotency_key: idempotencyKey }
          : { id: event.id, revision: event.revision }) };
      const response = await fetch(apiUrl(remove ? "calendars/events/delete" : "calendars/events"), {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      if (!mounted.current) return;
      if (!response.ok) {
        setError(response.status === 409 ? copy.conflict : copy.saveError);
        if (response.status === 409) await onConflict();
        return;
      }
      onSaved(remove ? copy.deleted : copy.saved);
    } catch {
      if (mounted.current) setError(copy.saveError);
    } finally {
      if (mounted.current) setBusy(false);
    }
  };
  const formSubmit = (formEvent: SyntheticEvent<HTMLFormElement>): void => {
    formEvent.preventDefault();
    void submit(false);
  };
  return <div className="calendar-editor">
    <form onSubmit={formSubmit} aria-label={event === null ? copy.new : copy.edit}>
      <fieldset className="calendar-editor-fields" disabled={busy}>
      <div className="calendar-editor-heading"><h3>{event === null ? copy.new : copy.edit}</h3>
        <button type="button" onClick={onClose} disabled={busy}>{copy.cancel}</button></div>
      <label>{copy.titleField}<input ref={title} required value={draft.summary} maxLength={1000}
        onChange={(e) => { field("summary", e.target.value); }} /></label>
      <label className="calendar-checkbox"><input type="checkbox" checked={draft.allDay} disabled={typeLocked}
        aria-describedby={typeLocked ? typeNote : undefined} onChange={(e) => {
        const allDay = e.target.checked;
        setEditing((old) => {
          const draft = changeDraftAllDay(old.draft, allDay);
          return { startAnchor: draft.start, draft };
        });
      }} />{copy.allDay}</label>
      {typeLocked && <p id={typeNote} className="calendar-note">{copy.typeLocked}</p>}
      <label className="calendar-checkbox"><input type="checkbox" checked={draft.endUnknown}
        onChange={(e) => { field("endUnknown", e.target.checked); }} />{copy.endUnknown}</label>
      {draft.endUnknown && <p className="calendar-note">{copy.endUnknownHelp}</p>}
      <div className="calendar-form-row">
        <label>{copy.start}<input required type={draft.allDay ? "date" : "datetime-local"} value={draft.start}
          onChange={(e) => { const start = e.target.value; setEditing((old) => moveDraftStart(old.draft, start, old.startAnchor)); }} /></label>
        <label>{draft.allDay ? copy.exclusive : copy.end}<input required={!draft.endUnknown} disabled={draft.endUnknown} type={draft.allDay ? "date" : "datetime-local"}
          value={draft.end} onChange={(e) => { field("end", e.target.value); }} /></label>
      </div>
      {!draft.allDay && <label>{copy.timezone}<input required value={draft.timezone} placeholder="Europe/Warsaw"
        onChange={(e) => { field("timezone", e.target.value); }} /></label>}
      {!draft.allDay && <details><summary>{copy.offset}</summary><p className="calendar-note">{copy.offsetHelp}</p>
        <div className="calendar-form-row"><label>{copy.startOffset}<input value={draft.startOffset} placeholder="+02:00"
          onChange={(e) => { field("startOffset", e.target.value); }} /></label>
          <label>{copy.endOffset}<input disabled={draft.endUnknown} value={draft.endOffset} placeholder="+01:00"
          onChange={(e) => { field("endOffset", e.target.value); }} /></label></div></details>}
      <label>{copy.place}<input value={draft.location} onChange={(e) => { field("location", e.target.value); }} /></label>
      <label>{copy.description}<textarea rows={3} value={draft.description} onChange={(e) => { field("description", e.target.value); }} /></label>
      <details open={draft.recurrence !== ""}><summary>{copy.recurrence}</summary>
        <textarea aria-label={copy.recurrence} rows={3} value={draft.recurrence} placeholder="RRULE:FREQ=WEEKLY;BYDAY=MO"
          onChange={(e) => { field("recurrence", e.target.value); }} /><p className="calendar-note">{copy.recurrenceHelp}</p>
        {(event?.payload.exceptions?.length ?? 0) > 0 && <p className="calendar-note">{copy.exceptions}: {event?.payload.exceptions?.length}</p>}
      </details>
      {error !== "" && <p className="calendar-error" role="alert">{error}</p>}
      <div className="calendar-form-actions"><button className="calendar-primary" type="submit" disabled={busy}>{busy ? copy.saving : copy.save}</button>
        {event !== null && <button className="calendar-delete" type="button" disabled={busy} onClick={() => { void submit(true); }}>{copy.delete}</button>}</div>
      </fieldset>
    </form>
  </div>;
}

export function Calendar({ canWrite, active = true }: { canWrite: boolean; active?: boolean }) {
  const copy: Copy = currentLocale() === "pl" ? labels.pl : labels.en;
  const [data, setData] = useState<CalendarData | null>(null);
  const [group, setGroup] = useState("");
  const [week, setWeek] = useState(() => weekStart(dateKey(new Date())));
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(true);
  const [editor, setEditor] = useState<{ id: number; event: CalendarEvent | null; day: string } | null>(null);
  const editorNumber = useRef(0);
  const requestNumber = useRef(0);
  const refresh = useCallback(async (): Promise<void> => {
    const request = ++requestNumber.current;
    setLoading(true);
    try {
      const result = await readCalendar(group);
      if (request !== requestNumber.current) return;
      setData(result);
      setError("");
      if (group === "" && result.groups[0] !== undefined) setGroup(result.groups[0].id);
    } catch {
      if (request === requestNumber.current) setError(copy.error);
    } finally {
      if (request === requestNumber.current) setLoading(false);
    }
  }, [group, copy.error]);
  useEffect(() => {
    if (!active) return;
    void refresh();
    const interval = window.setInterval(() => { void refresh(); }, 30000);
    return () => { window.clearInterval(interval); ++requestNumber.current; };
  }, [active, refresh]);
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const locale = currentLocale();
  const events = data?.events.filter((event) => !event.deleted && event.group_id === group) ?? [];
  const days = Array.from({ length: 7 }, (_, index) => shiftDay(week, index));
  const today = dateKey(new Date());
  const endpointIds = new Set(data?.status.endpoints.filter((item) => item.group_id === group).map((item) => item.id) ?? []);
  const pending = data?.status.pending.filter((item) => item.endpoint_id !== undefined && endpointIds.has(item.endpoint_id)) ?? [];
  const errorLabel = (code: string): string => code === "google_event_type_restricted" ? copy.googleRestricted
    : /permission_denied|http_40[13]/.test(code) ? copy.permission : code;
  const series = events.filter((event) => (event.payload.recurrence?.length ?? 0) > 0);
  const open = (event: CalendarEvent | null, day: string): void => {
    setNotice("");
    setEditor({ id: ++editorNumber.current, event, day });
  };
  const card = (event: CalendarEvent, recurring = false) => <button type="button" className="calendar-event" key={event.id}
    onClick={() => { open(event, event.payload.start.date ?? localTime(event.payload.start, zone).slice(0, 10)); }}>
    <span className="calendar-event-time">{event.payload.start.date !== undefined ? copy.allDay
      : localTime(event.payload.start, zone).slice(11, 16)}{recurring ? ` · ${event.payload.start.date ?? localTime(event.payload.start, zone).slice(0, 10)}` : ""}</span>
    {event.payload.endTimeUnspecified === true && <span>{copy.endUnknown}</span>}
    <strong>{event.payload.summary}</strong>{event.payload.location !== "" && <span>{event.payload.location}</span>}
    {recurring && <code>{event.payload.recurrence?.find((line) => line.startsWith("RRULE:"))}</code>}
  </button>;
  return <div className="calendar-app">
    <header className="calendar-header"><div><p className="calendar-eyebrow">HESTIA · {zone}</p><h2>{copy.title}</h2><p>{copy.subtitle}</p></div>
      {canWrite && data?.enabled === true && group !== "" && <button className="calendar-primary" type="button" onClick={() => { open(null, today); }}>+ {copy.add}</button>}</header>
    {error !== "" && <p className="calendar-error" role="alert">{error}</p>}
    {notice !== "" && <p className="calendar-notice" role="status">{notice}</p>}
    {data?.enabled === false && <div className="calendar-empty"><h3>{copy.disabled}</h3><p>{copy.setup}</p></div>}
    {data?.enabled === true && data.groups.length === 0 && <p>{copy.noGroups}</p>}
    {data?.enabled === true && data.groups.length > 0 && <>
      <div className="calendar-toolbar"><label className="calendar-group">{copy.group}<select value={group} onChange={(e) => {
        ++editorNumber.current; setGroup(e.target.value); setEditor(null); setNotice("");
      }}>{data.groups.map((item) => <option key={item.id} value={item.id}>{item.id}</option>)}</select></label>
        <div className="calendar-navigation"><button type="button" aria-label={copy.previous} onClick={() => { setWeek(shiftDay(week, -7)); }}>←</button>
          <button type="button" onClick={() => { setWeek(weekStart(today)); }}>{copy.today}</button>
          <button type="button" aria-label={copy.next} onClick={() => { setWeek(shiftDay(week, 7)); }}>→</button></div>
        <button type="button" onClick={() => { void refresh(); }} disabled={loading}>{copy.refresh}</button></div>
      <div className="calendar-range"><h3>{new Intl.DateTimeFormat(locale, { month: "long", year: "numeric" }).format(new Date(`${week}T12:00:00`))}</h3>
        {!canWrite && <span>{copy.readonly}</span>}</div>
      {editor !== null && canWrite && <EventEditor key={`${group}:${String(editor.id)}`} event={editor.event} day={editor.day} group={group} copy={copy}
        onClose={() => { ++editorNumber.current; setEditor(null); }}
        onConflict={async () => { if (editor.id === editorNumber.current) await refresh(); }}
        onSaved={(message) => {
          if (editor.id !== editorNumber.current) return;
          ++editorNumber.current; setEditor(null); setNotice(message); void refresh();
        }} />}
      {editor?.event !== undefined && editor.event !== null && !canWrite && <article className="calendar-editor" aria-label={copy.details}>
        <div className="calendar-editor-heading"><h3>{editor.event.payload.summary}</h3><button type="button" onClick={() => { setEditor(null); }}>{copy.close}</button></div>
        <p>{copy.start}: {localTime(editor.event.payload.start, editor.event.payload.start.timeZone ?? zone).replace("T", " ")}</p>
        {editor.event.payload.endTimeUnspecified === true ? <p>{copy.endUnknown}</p>
          : <p>{editor.event.payload.end.date === undefined ? copy.end : copy.exclusive}: {localTime(editor.event.payload.end, editor.event.payload.end.timeZone ?? zone).replace("T", " ")}</p>}
        <p>{editor.event.payload.start.timeZone}</p><p>{editor.event.payload.location}</p>
        <p className="calendar-description">{editor.event.payload.description}</p>
        {editor.event.payload.recurrence?.map((line) => <p key={line}><code>{line}</code></p>)}
      </article>}
      <div className="calendar-week">{days.map((day) => <section className={`calendar-day${day === today ? " is-today" : ""}`} key={day}>
        <div className="calendar-day-heading"><h4><span>{new Intl.DateTimeFormat(locale, { weekday: "short" }).format(new Date(`${day}T12:00:00`))}</span><b>{Number(day.slice(-2))}</b></h4>
          {canWrite && <button type="button" aria-label={`${copy.add} ${day}`} onClick={() => { open(null, day); }}>+</button>}</div>
        <div className="calendar-day-events">{events.filter((event) => onDay(event, day, zone))
          .sort((a, b) => localTime(a.payload.start, zone).localeCompare(localTime(b.payload.start, zone))).map((event) => card(event))}
          {!events.some((event) => onDay(event, day, zone)) && <p className="calendar-no-events">{copy.empty}</p>}</div>
      </section>)}</div>
      {series.length > 0 && <section className="calendar-series"><h3>{copy.recurring}</h3><p className="calendar-note">{copy.recurringHelp}</p><div>{series.map((event) => card(event, true))}</div></section>}
      <details className="calendar-sync"><summary>{copy.sync} · {pending.length} {copy.queued}</summary>
        <ul>{data.status.endpoints.filter((endpoint) => endpoint.group_id === group).map((endpoint) => <li key={endpoint.id}>
          <strong>{endpoint.id}</strong><span>{!endpoint.enabled ? copy.off : endpoint.last_success === null ? copy.never
            : `${copy.last}: ${new Date(endpoint.last_success * 1000).toLocaleString(locale)}`}</span>
          {Array.from(new Set([endpoint.last_error, ...pending.filter((item) => item.endpoint_id === endpoint.id)
            .map((item) => item.last_error)].filter((item): item is string => typeof item === "string" && item !== "")))
            .map((code) => <span key={code} className="calendar-error">{errorLabel(code)}</span>)}</li>)}</ul>
      </details>
    </>}
    {loading && <p className="calendar-note" role="status">{copy.loading}</p>}
    {data === null && !loading && <button type="button" onClick={() => { void refresh(); }}>{copy.refresh}</button>}
  </div>;
}

export function mountCalendar(element: HTMLElement, canWrite: boolean): { setActive: (active: boolean) => void; destroy: () => void } {
  const root = createRoot(element);
  return { setActive: (active) => { root.render(<Calendar canWrite={canWrite} active={active} />); }, destroy: () => { root.unmount(); } };
}
