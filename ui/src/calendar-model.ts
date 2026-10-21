/** Calendar wire data and editing helpers. Date-only values never pass through UTC. */
export interface EventTime {
  date?: string;
  dateTime?: string;
  timeZone?: string;
}
export interface CalendarPayload {
  summary: string;
  description?: string;
  location?: string;
  start: EventTime;
  end: EventTime;
  endTimeUnspecified?: true;
  recurrence?: string[];
  exceptions?: Record<string, unknown>[];
  [field: string]: unknown;
}
export interface CalendarEvent {
  id: string;
  group_id: string;
  revision: number;
  payload: CalendarPayload;
  deleted: boolean;
}
export interface CalendarEndpoint {
  id: string;
  group_id: string;
  last_success: number | null;
  last_error: string | null;
  enabled: boolean;
}
export interface CalendarData {
  enabled: boolean;
  groups: { id: string }[];
  events: CalendarEvent[];
  status: {
    endpoints: CalendarEndpoint[];
    pending: { endpoint_id?: string; group_id?: string; [field: string]: unknown }[];
  };
}
export interface EventDraft {
  summary: string;
  description: string;
  location: string;
  allDay: boolean;
  endUnknown: boolean;
  start: string;
  end: string;
  timezone: string;
  startOffset: string;
  endOffset: string;
  recurrence: string;
}

export function dateKey(date: Date): string {
  return `${String(date.getFullYear()).padStart(4, "0")}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}
export function shiftDay(day: string, offset: number): string {
  const date = new Date(`${day}T12:00:00`);
  date.setDate(date.getDate() + offset);
  return dateKey(date);
}
export function weekStart(day: string): string {
  const date = new Date(`${day}T12:00:00`);
  return shiftDay(day, -(date.getDay() + 6) % 7);
}
export function localTime(value: EventTime, timezone: string, includeSeconds = false): string {
  if (value.date !== undefined) return value.date;
  const dateTime = value.dateTime ?? "";
  // Naive input from an editor is already expressed in the selected zone.
  if (!/(Z|[+-]\d{2}:\d{2})$/.test(dateTime)) {
    return includeSeconds ? `${dateTime.slice(0, 16)}:${dateTime.slice(17, 19) || "00"}` : dateTime.slice(0, 16);
  }
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    ...(includeSeconds ? { second: "2-digit" as const } : {}),
  }).formatToParts(new Date(dateTime));
  const part = (name: Intl.DateTimeFormatPartTypes): string => parts.find((item) => item.type === name)?.value ?? "";
  return `${part("year").padStart(4, "0")}-${part("month")}-${part("day")}T${part("hour")}:${part("minute")}${includeSeconds ? `:${part("second")}` : ""}`;
}
export function eventDraft(event: CalendarEvent | null, day: string): EventDraft {
  const payload = event?.payload;
  const timezone = payload?.start.timeZone ?? Intl.DateTimeFormat().resolvedOptions().timeZone;
  return {
    summary: payload?.summary ?? "", description: payload?.description ?? "", location: payload?.location ?? "",
    allDay: payload?.start.date !== undefined,
    endUnknown: payload?.endTimeUnspecified === true,
    start: payload === undefined ? `${day}T09:00` : localTime(payload.start, timezone),
    end: payload === undefined ? `${day}T10:00` : localTime(payload.end, timezone),
    timezone,
    startOffset: payload?.start.dateTime?.match(/(Z|[+-]\d{2}:\d{2})$/)?.[0] ?? "",
    endOffset: payload?.end.timeZone === timezone
      ? payload.end.dateTime?.match(/(Z|[+-]\d{2}:\d{2})$/)?.[0] ?? "" : "",
    recurrence: payload?.recurrence?.join("\n") ?? "",
  };
}
export function draftPayload(draft: EventDraft, original: CalendarEvent | null): CalendarPayload {
  // Preserve supported fields and occurrence exceptions while editing the series.
  const time = (field: "start" | "end"): EventTime => {
    if (draft.allDay) return { date: draft[field] };
    const old = original?.payload[field];
    const offset = field === "start" ? draft.startOffset : draft.endOffset;
    const originalOffset = old?.timeZone === original?.payload.start.timeZone
      ? old?.dateTime?.match(/(Z|[+-]\d{2}:\d{2})$/)?.[0] ?? "" : "";
    if (old?.dateTime !== undefined && original?.payload.start.timeZone === draft.timezone
        && localTime(old, draft.timezone) === draft[field] && originalOffset === offset) return old;
    return { dateTime: `${draft[field]}:00${offset}`, timeZone: draft.timezone };
  };
  const payload: CalendarPayload = {
    ...original?.payload,
    summary: draft.summary.trim(), description: draft.description, location: draft.location,
    start: time("start"), end: time("end"),
  };
  if (draft.endUnknown) {
    payload.endTimeUnspecified = true;
    payload.end = draft.allDay ? { date: shiftDay(draft.start, 1) } : { ...payload.start };
  } else delete payload.endTimeUnspecified;
  const recurrence = draft.recurrence.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  if (recurrence.length > 0) payload.recurrence = recurrence;
  else {
    delete payload.recurrence;
    delete payload.exceptions;
  }
  return payload;
}
/** Intervals use [start,end); point events and unknown ends belong to their start day. */
export function onDay(event: CalendarEvent, day: string, timezone: string): boolean {
  if (event.deleted || (event.payload.recurrence?.length ?? 0) > 0) return false;
  const { start, end } = event.payload;
  if (start.date !== undefined) return start.date <= day && (end.date ?? start.date) > day;
  const startLocal = localTime(start, timezone, true);
  const endLocal = localTime(end, timezone, true);
  const nextDay = `${shiftDay(day, 1)}T00:00:00`;
  if (event.payload.endTimeUnspecified === true || startLocal === endLocal) {
    return startLocal >= `${day}T00:00:00` && startLocal < nextDay;
  }
  return startLocal < nextDay && endLocal > `${day}T00:00:00`;
}
