import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Calendar, mountCalendar } from "./calendar";
import { dateKey, draftPayload, eventDraft, localTime, moveDraftStart, onDay, shiftDay, weekStart } from "./calendar-model";
import type { CalendarData, CalendarEvent } from "./calendar-model";

async function flush(action: () => void): Promise<void> {
  await act(async () => { action(); await Promise.resolve(); });
}
function required<T>(value: T | null): T {
  if (value === null) throw new Error("Required test element missing");
  return value;
}
function event(): CalendarEvent {
  return { id: "local-1", group_id: "family", revision: 3, deleted: false,
    payload: { summary: "Family walk", description: "At the park", location: "Warsaw",
      start: { dateTime: `${dateKey(new Date())}T10:00:00+02:00`, timeZone: "Europe/Warsaw" },
      end: { dateTime: `${dateKey(new Date())}T11:00:00+02:00`, timeZone: "Europe/Warsaw" } } };
}
function data(): CalendarData {
  return { enabled: true, groups: [{ id: "family" }, { id: "work" }], events: [event()],
    status: { endpoints: [{ id: "icloud", group_id: "family", enabled: true, last_success: null,
      last_error: "write_permission_denied" }], pending: [{ endpoint_id: "icloud" }] } };
}

describe("calendar date and payload semantics", () => {
  it("moves a new one-hour event across midnight and year boundaries using civil time", () => {
    const draft = { ...eventDraft(null, "2026-12-31"), timezone: "Pacific/Auckland" };
    const moved = moveDraftStart(draft, "2026-12-31T23:45").draft;
    expect(moved.end).toBe("2027-01-01T00:45");
    expect(draftPayload(moved, null).end).toEqual({ dateTime: "2027-01-01T00:45:00", timeZone: "Pacific/Auckland" });
  });
  it("preserves a manually chosen duration on subsequent start changes", () => {
    const draft = eventDraft(null, "2026-10-24");
    const customized = { ...moveDraftStart(draft, "2026-10-24T12:00").draft, end: "2026-10-24T12:30" };
    expect(moveDraftStart(customized, "2026-10-26T08:15").draft.end).toBe("2026-10-26T08:45");
  });
  it("preserves exclusive all-day duration over leap days and DST weekends", () => {
    const draft = { ...eventDraft(null, "2028-02-27"), allDay: true, start: "2028-02-27", end: "2028-03-01" };
    const moved = moveDraftStart(draft, "2026-10-24").draft;
    expect(moved.end).toBe("2026-10-27");
    expect(moveDraftStart(moved, "2026-12-30").draft.end).toBe("2027-01-02");
  });
  it("keeps the end and last complete start while start input is cleared or invalid", () => {
    const draft = { ...eventDraft(null, "2026-10-24"), end: "2026-10-24T11:30", startOffset: "+02:00", endOffset: "+02:00" };
    for (const incomplete of ["", "2026-10-", "2026-02-30T09:00", "2026-10-24T25:00", "0000-10-24T09:00"]) {
      const editing = moveDraftStart(draft, incomplete);
      expect(editing.draft.end).toBe(draft.end);
      expect(editing.draft.endOffset).toBe("+02:00");
      expect(editing.startAnchor).toBe(draft.start);
      const completed = moveDraftStart(editing.draft, "2026-10-26T09:00", editing.startAnchor);
      expect(completed.draft.end).toBe("2026-10-26T11:30");
      expect(completed.draft.endOffset).toBe("");
    }
  });
  it("keeps untouched offsets and does not replace an invalid manually entered end", () => {
    const draft = { ...eventDraft(null, "2026-10-24"), startOffset: "+02:00", endOffset: "+02:00" };
    expect(moveDraftStart(draft, draft.start).draft).toBe(draft);
    for (const end of ["", "2026-10-24T08:00"]) {
      const moved = moveDraftStart({ ...draft, end }, "2026-10-25T09:00").draft;
      expect(moved.end).toBe(end);
      expect(moved.endOffset).toBe("+02:00");
    }
    const allDay = { ...draft, allDay: true, start: "2026-10-24", end: "2026-10-24" };
    expect(moveDraftStart(allDay, "2026-10-25").draft.end).toBe("2026-10-24");
  });
  it("preserves point-event zero duration and the displayed duration across DST", () => {
    const draft = { ...eventDraft(null, "2026-03-28"), timezone: "Europe/Warsaw", start: "2026-03-28T01:30", end: "2026-03-28T03:30" };
    expect(moveDraftStart(draft, "2026-03-29T01:30").draft.end).toBe("2026-03-29T03:30");
    const point = { ...draft, end: draft.start };
    expect(moveDraftStart(point, "2026-10-25T04:00").draft.end).toBe("2026-10-25T04:00");
  });
  it("uses date-only arithmetic across daylight saving and Monday weeks", () => {
    expect(weekStart("2026-10-25")).toBe("2026-10-19");
    expect(weekStart("2026-10-26")).toBe("2026-10-26");
    expect(shiftDay("2026-10-25", 1)).toBe("2026-10-26");
    expect(shiftDay("2026-12-31", 1)).toBe("2027-01-01");
  });
  it("preserves exact fallback offsets, recurrence and occurrence exceptions when editing", () => {
    const original = event();
    original.payload.start = { dateTime: "2026-10-25T02:30:00+01:00", timeZone: "Europe/Warsaw" };
    original.payload.end = { dateTime: "2026-10-25T03:30:00+01:00", timeZone: "Europe/Warsaw" };
    original.payload.recurrence = ["RRULE:FREQ=WEEKLY", "EXDATE;TZID=Europe/Warsaw:20261101T023000"];
    original.payload.exceptions = [{ originalStartTime: { date: "2026-11-08" }, status: "cancelled" }];
    const draft = eventDraft(original, "2026-10-25");
    expect(draft.start).toBe("2026-10-25T02:30");
    const payload = draftPayload({ ...draft, summary: "Changed" }, original);
    expect(payload.start).toEqual(original.payload.start);
    expect(payload.exceptions).toEqual(original.payload.exceptions);
    expect(payload.recurrence).toEqual(original.payload.recurrence);
    expect(draftPayload({ ...draft, start: "2026-10-25T04:00", startOffset: "", recurrence: "" }, original).start)
      .toEqual({ dateTime: "2026-10-25T04:00:00", timeZone: "Europe/Warsaw" });
    expect(draftPayload({ ...draft, recurrence: "" }, original).recurrence).toBeUndefined();
    expect(draftPayload({ ...draft, recurrence: "" }, original).exceptions).toBeUndefined();
    expect(draftPayload({ ...draft, startOffset: "+02:00" }, original).start.dateTime)
      .toBe("2026-10-25T02:30:00+02:00");
  });
  it("preserves leading-zero years when formatting legacy UTC records", () => {
    expect(localTime({ dateTime: "0008-10-24T09:00:00Z", timeZone: "UTC" }, "UTC")).toBe("0008-10-24T09:00");
  });
  it("preserves separate arrival time zones when editing an imported journey", () => {
    const journey = event();
    journey.payload.start = { dateTime: "2026-10-24T09:00:00+02:00", timeZone: "Europe/Warsaw" };
    journey.payload.end = { dateTime: "2026-10-24T10:00:00-04:00", timeZone: "America/New_York" };
    const draft = eventDraft(journey, "2026-10-24");
    expect(draft.end).toBe("2026-10-24T16:00");
    expect(draft.endOffset).toBe("");
    expect(draftPayload({ ...draft, summary: "Flight edited" }, journey).end).toEqual(journey.payload.end);
    expect(draftPayload({ ...draft, end: "2026-10-24T16:30" }, journey).end)
      .toEqual({ dateTime: "2026-10-24T16:30:00", timeZone: "Europe/Warsaw" });
  });
  it("uses exclusive all-day end and excludes deleted and recurring series from daily cards", () => {
    const original = event();
    original.payload.start = { date: "2026-10-24" };
    original.payload.end = { date: "2026-10-25" };
    expect(onDay(original, "2026-10-24", "Europe/Warsaw")).toBe(true);
    expect(onDay(original, "2026-10-25", "Europe/Warsaw")).toBe(false);
    expect(localTime(original.payload.start, "Europe/Warsaw")).toBe("2026-10-24");
    const draft = eventDraft(original, "2026-10-24");
    expect(draftPayload(draft, original).end).toEqual({ date: "2026-10-25" });
    expect(onDay({ ...original, deleted: true }, "2026-10-24", "Europe/Warsaw")).toBe(false);
    expect(onDay({ ...original, payload: { ...original.payload, recurrence: ["RRULE:FREQ=DAILY"] } }, "2026-10-24", "Europe/Warsaw")).toBe(false);
  });
  it("keeps midnight point events on their start day and preserves unknown end semantics", () => {
    const point = event();
    point.payload.start = { dateTime: "2026-10-24T00:00:00Z", timeZone: "UTC" };
    point.payload.end = { ...point.payload.start };
    expect(onDay(point, "2026-10-24", "UTC")).toBe(true);
    expect(onDay(point, "2026-10-23", "UTC")).toBe(false);
    expect(onDay(point, "2026-10-25", "UTC")).toBe(false);
    point.payload.endTimeUnspecified = true;
    const draft = eventDraft(point, "2026-10-24");
    expect(draft.endUnknown).toBe(true);
    const payload = draftPayload({ ...draft, summary: "Retitled", start: "2026-10-26T12:00" }, point);
    expect(payload.endTimeUnspecified).toBe(true);
    expect(payload.end).toEqual(payload.start);
    expect(draftPayload({ ...draft, endUnknown: false }, point).endTimeUnspecified).toBeUndefined();
    point.payload.start = { date: "2026-10-24" };
    point.payload.end = { date: "2026-10-25" };
    const allDay = draftPayload({ ...eventDraft(point, "2026-10-24"), start: "2026-10-31" }, point);
    expect(allDay.end).toEqual({ date: "2026-11-01" });
    expect(allDay.endTimeUnspecified).toBe(true);
  });
  it("includes a nonzero interval extending seconds past midnight on both days", () => {
    const original = event();
    original.payload.start = { dateTime: "2026-10-24T23:59:59Z", timeZone: "UTC" };
    original.payload.end = { dateTime: "2026-10-25T00:00:30Z", timeZone: "UTC" };
    expect(onDay(original, "2026-10-24", "UTC")).toBe(true);
    expect(onDay(original, "2026-10-25", "UTC")).toBe(true);
  });
  it("does not assign midnight end to the following day", () => {
    const original = event();
    original.payload.start = { dateTime: "2026-10-24T23:00:00+02:00", timeZone: "Europe/Warsaw" };
    original.payload.end = { dateTime: "2026-10-25T00:00:00+02:00", timeZone: "Europe/Warsaw" };
    expect(onDay(original, "2026-10-24", "Europe/Warsaw")).toBe(true);
    expect(onDay(original, "2026-10-25", "Europe/Warsaw")).toBe(false);
    expect(localTime({ dateTime: "2026-10-24T09:30:00", timeZone: "Europe/Warsaw" }, "Europe/Warsaw")).toBe("2026-10-24T09:30");
  });
});

describe("React calendar", () => {
  let host: HTMLDivElement;
  let root: Root;
  let fetcher: ReturnType<typeof vi.fn<typeof fetch>>;
  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    host = document.createElement("div"); document.body.append(host); root = createRoot(host);
    fetcher = vi.fn<typeof fetch>().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(data()), { status: 200 })));
    vi.stubGlobal("fetch", fetcher);
  });
  afterEach(async () => {
    await flush(() => { root.unmount(); }); host.remove(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks();
  });
  const click = async (button: HTMLElement): Promise<void> => { await flush(() => { button.click(); }); };
  const button = (text: string): HTMLButtonElement => {
    const result = Array.from(host.querySelectorAll("button")).find((item) => item.textContent.includes(text));
    if (result === undefined) throw new Error(`Missing button: ${text}`); return result;
  };
  const input = async (label: string, value: string): Promise<void> => {
    const field = Array.from(host.querySelectorAll("label")).find((item) => item.textContent.includes(label))?.querySelector("input");
    if (field === null || field === undefined) throw new Error(`Missing field: ${label}`);
    await flush(() => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(field, value);
      field.dispatchEvent(new Event("input", { bubbles: true })); field.dispatchEvent(new Event("change", { bubbles: true }));
    });
  };
  const inputValue = (label: string): string => required(Array.from(host.querySelectorAll("label"))
    .find((item) => item.textContent === label)?.querySelector("input") ?? null).value;
  const postBodies = (): Record<string, unknown>[] => fetcher.mock.calls.filter((call) => call[1]?.method === "POST")
    .map((call) => JSON.parse(typeof call[1]?.body === "string" ? call[1].body : "{}") as Record<string, unknown>);

  it("loads groups and status, navigates weeks and keeps viewer read-only", async () => {
    await flush(() => { root.render(<Calendar canWrite={false} />); });
    expect(host.textContent).toContain("Family walk"); expect(host.textContent).toContain("Read only");
    expect(host.textContent).toContain("Calendar access is restricted"); expect(button("Family walk").disabled).toBe(false);
    await click(button("Family walk")); expect(host.textContent).toContain("At the park");
    expect(host.querySelector("form")).toBeNull(); await click(button("Close"));
    expect(Array.from(host.querySelectorAll("button")).some((item) => item.textContent.includes("Add event"))).toBe(false);
    await click(required(host.querySelector<HTMLButtonElement>('[aria-label="Next week"]')));
    expect(host.textContent).not.toContain("Family walk"); await click(button("Today")); expect(host.textContent).toContain("Family walk");
    const select = required(host.querySelector("select"));
    await flush(() => { select.value = "work"; select.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(fetcher.mock.calls.some((call) => (call[0] instanceof URL ? call[0].href : typeof call[0] === "string" ? call[0] : call[0].url).includes("group=work"))).toBe(true);
  });
  it("creates an event and shows local save feedback", async () => {
    await flush(() => { root.render(<Calendar canWrite />); }); await click(button("Add event")); await input("Title", "Dinner together");
    await flush(() => { required(host.querySelector("form")).dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    expect(postBodies()[0]?.group).toBe("family"); expect((postBodies()[0]?.payload as { summary: string }).summary).toBe("Dinner together");
    expect(postBodies()[0]?.id).toBeUndefined(); expect(host.textContent).toContain("Saved locally");
  });
  it("creates a one-hour overnight event and retains a manually selected shorter duration", async () => {
    await flush(() => { root.render(<Calendar canWrite />); });
    await click(button("Add event")); await input("Title", "Evening plan");
    await input("Starts", "2026-12-31T23:30");
    expect(inputValue("Ends")).toBe("2027-01-01T00:30");
    await input("Ends", "2027-01-01T00:00");
    await input("Starts", "2027-01-04T08:00");
    expect(inputValue("Ends")).toBe("2027-01-04T08:30");
    await flush(() => { required(host.querySelector("form")).dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    expect(postBodies()[0]?.payload).toMatchObject({
      start: { dateTime: "2027-01-04T08:00:00" }, end: { dateTime: "2027-01-04T08:30:00" },
    });
  });
  it("waits through a cleared start without losing the edited duration or end offset", async () => {
    await flush(() => { root.render(<Calendar canWrite />); });
    await click(button("Family walk"));
    const day = inputValue("Starts").slice(0, 10);
    await input("Ends", `${day}T12:30`);
    await input("End offset", "+02:00");
    await input("Starts", "");
    expect(inputValue("Ends")).toBe(`${day}T12:30`);
    expect(inputValue("End offset")).toBe("+02:00");
    await input("Starts", "2027-01-04T08:00");
    expect(inputValue("Ends")).toBe("2027-01-04T10:30");
    expect(inputValue("End offset")).toBe("");
  });
  it("moves a multi-day all-day event while retaining its exclusive end date", async () => {
    vi.setSystemTime(new Date("2026-10-24T12:00:00Z"));
    const fixture = data();
    const original = event();
    original.payload.start = { date: "2026-10-24" };
    original.payload.end = { date: "2026-10-27" };
    fixture.events = [original];
    fetcher.mockImplementation(() => Promise.resolve(new Response(JSON.stringify(fixture), { status: 200 })));
    await flush(() => { root.render(<Calendar canWrite />); });
    await click(button("Family walk"));
    await input("Starts", "2026-10-31");
    expect(inputValue("End date (exclusive)")).toBe("2026-11-03");
    await flush(() => { required(host.querySelector("form")).dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    expect(postBodies()[0]?.payload).toMatchObject({ start: { date: "2026-10-31" }, end: { date: "2026-11-03" } });
  });
  it("automatically moves the end and clears both changed offsets across DST", async () => {
    vi.setSystemTime(new Date("2026-10-24T12:00:00Z"));
    const fixture = data();
    const original = event();
    original.payload.start = { dateTime: "2026-10-24T09:00:00+02:00", timeZone: "Europe/Warsaw" };
    original.payload.end = { dateTime: "2026-10-24T10:00:00+02:00", timeZone: "Europe/Warsaw" };
    fixture.events = [original];
    fetcher.mockImplementation(() => Promise.resolve(new Response(JSON.stringify(fixture), { status: 200 })));
    await flush(() => { root.render(<Calendar canWrite />); });
    await click(button("Family walk"));
    const startOffset = required(Array.from(host.querySelectorAll("label")).find((label) => label.textContent === "Start offset")?.querySelector("input") ?? null);
    const endOffset = required(Array.from(host.querySelectorAll("label")).find((label) => label.textContent === "End offset")?.querySelector("input") ?? null);
    await input("Starts", "2026-10-26T09:00");
    expect(startOffset.value).toBe(""); expect(endOffset.value).toBe("");
    expect(inputValue("Ends")).toBe("2026-10-26T10:00");
    await flush(() => { required(host.querySelector("form")).dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    expect(postBodies()[0]?.payload).toMatchObject({
      start: { dateTime: "2026-10-26T09:00:00", timeZone: "Europe/Warsaw" },
      end: { dateTime: "2026-10-26T10:00:00", timeZone: "Europe/Warsaw" },
    });
  });
  it("preserves untouched fallback offsets and accepts explicit choices after editing times", async () => {
    vi.setSystemTime(new Date("2026-10-25T12:00:00Z"));
    const fixture = data();
    const original = event();
    original.payload.start = { dateTime: "2026-10-25T02:30:00+01:00", timeZone: "Europe/Warsaw" };
    original.payload.end = { dateTime: "2026-10-25T03:30:00+01:00", timeZone: "Europe/Warsaw" };
    fixture.events = [original];
    fetcher.mockImplementation(() => Promise.resolve(new Response(JSON.stringify(fixture), { status: 200 })));
    await flush(() => { root.render(<Calendar canWrite />); });
    await click(button("Family walk")); await input("Title", "Title only");
    await flush(() => { required(host.querySelector("form")).dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    expect(postBodies()[0]?.payload).toMatchObject({ start: original.payload.start, end: original.payload.end });
    await click(button("Family walk"));
    await input("Starts", "2026-10-25T02:15"); await input("Ends", "2026-10-25T02:45");
    await input("Start offset", "+01:00"); await input("End offset", "+01:00");
    await flush(() => { required(host.querySelector("form")).dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    expect(postBodies()[1]?.payload).toMatchObject({
      start: { dateTime: "2026-10-25T02:15:00+01:00", timeZone: "Europe/Warsaw" },
      end: { dateTime: "2026-10-25T02:45:00+01:00", timeZone: "Europe/Warsaw" },
    });
  });
  it.each([
    { name: "nonexistent spring start", start: "2026-03-29T02:30", end: "2026-03-29T03:30", startOffset: "+01:00", endOffset: "+02:00" },
    { name: "nonexistent spring end", start: "2026-03-29T01:30", end: "2026-03-29T02:30", startOffset: "+01:00", endOffset: "+01:00" },
    { name: "wrong offset for a winter date", start: "2026-10-26T09:00", end: "2026-10-26T10:00", startOffset: "+02:00", endOffset: "+02:00" },
  ])("rejects a $name without posting or changing the user's wall times", async ({ start, end, startOffset, endOffset }) => {
    await flush(() => { root.render(<Calendar canWrite />); });
    await click(button("Add event")); await input("Title", "Explicit offset test");
    await input("Time zone (IANA)", "Europe/Warsaw");
    await input("Starts", start); await input("Ends", end);
    await input("Start offset", startOffset); await input("End offset", endOffset);
    await flush(() => { required(host.querySelector("form")).dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    expect(postBodies()).toHaveLength(0);
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("UTC offset does not match");
    expect(inputValue("Starts")).toBe(start); expect(inputValue("Ends")).toBe(end);
  });
  it.each(["+02:00", "+01:00"])("accepts the valid Warsaw autumn occurrence selected by %s", async (offset) => {
    await flush(() => { root.render(<Calendar canWrite />); });
    await click(button("Add event")); await input("Title", "Autumn choice");
    await input("Time zone (IANA)", "Europe/Warsaw");
    await input("Starts", "2026-10-25T02:15"); await input("Ends", "2026-10-25T02:45");
    await input("Start offset", offset); await input("End offset", offset);
    await flush(() => { required(host.querySelector("form")).dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    expect(postBodies()[0]?.payload).toMatchObject({
      start: { dateTime: `2026-10-25T02:15:00${offset}`, timeZone: "Europe/Warsaw" },
      end: { dateTime: `2026-10-25T02:45:00${offset}`, timeZone: "Europe/Warsaw" },
    });
  });
  it("ignores an unused invalid end offset when only the start is known", async () => {
    await flush(() => { root.render(<Calendar canWrite />); });
    await click(button("Add event")); await input("Title", "Only a known start");
    await input("Time zone (IANA)", "Europe/Warsaw");
    await input("Starts", "2026-03-29T01:30"); await input("Start offset", "+01:00");
    await input("End offset", "invalid");
    await click(required(Array.from(host.querySelectorAll("label"))
      .find((label) => label.textContent === "End time unknown")?.querySelector("input") ?? null));
    await flush(() => { required(host.querySelector("form")).dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    const payload = postBodies()[0]?.payload;
    expect(payload).toMatchObject({ endTimeUnspecified: true,
      start: { dateTime: "2026-03-29T01:30:00+01:00", timeZone: "Europe/Warsaw" },
      end: { dateTime: "2026-03-29T01:30:00+01:00", timeZone: "Europe/Warsaw" } });
  });
  it.each(["family", "work"])("keeps the current %s editor when an older save finishes", async (nextGroup) => {
    const fixture = data();
    const second = event();
    second.id = "local-2"; second.group_id = nextGroup; second.payload.summary = "Second plan";
    fixture.events.push(second);
    let resolveSave: (response: Response) => void = () => { throw new Error("Save has not started"); };
    const pendingSave = new Promise<Response>((resolve) => { resolveSave = resolve; });
    fetcher.mockImplementation((_, options) => options?.method === "POST" ? pendingSave
      : Promise.resolve(new Response(JSON.stringify(fixture), { status: 200 })));
    await flush(() => { root.render(<Calendar canWrite />); });
    await click(button("Family walk")); await input("Title", "Saved first plan");
    await flush(() => { required(host.querySelector("form")).dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    if (nextGroup !== "family") {
      const select = required(host.querySelector("select"));
      await flush(() => { select.value = nextGroup; select.dispatchEvent(new Event("change", { bubbles: true })); });
    }
    await click(button("Second plan")); await input("Title", "Keep this second draft");
    const readCount = fetcher.mock.calls.filter((call) => call[1]?.method !== "POST").length;
    await flush(() => { resolveSave(new Response("{}", { status: 200 })); });
    expect(inputValue("Title")).toBe("Keep this second draft");
    expect(host.textContent).not.toContain("Saved locally");
    expect(required(host.querySelector("select")).value).toBe(nextGroup);
    expect(fetcher.mock.calls.filter((call) => call[1]?.method !== "POST")).toHaveLength(readCount);
    expect(postBodies()).toHaveLength(1);
  });
  it("gives a reopened event a fresh editor while its older save is still pending", async () => {
    let resolveSave: (response: Response) => void = () => { throw new Error("Save has not started"); };
    const pendingSave = new Promise<Response>((resolve) => { resolveSave = resolve; });
    fetcher.mockImplementation((_, options) => options?.method === "POST" ? pendingSave
      : Promise.resolve(new Response(JSON.stringify(data()), { status: 200 })));
    await flush(() => { root.render(<Calendar canWrite />); });
    await click(button("Family walk")); await input("Title", "First draft");
    await flush(() => { required(host.querySelector("form")).dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    await click(button("Family walk"));
    expect(inputValue("Title")).toBe("Family walk");
    await input("Title", "Reopened draft");
    expect(button("Save event").disabled).toBe(false);
    await flush(() => { resolveSave(new Response("{}", { status: 200 })); });
    expect(inputValue("Title")).toBe("Reopened draft");
    expect(host.textContent).not.toContain("Saved locally");
  });
  it("clears both offsets on a zone change and when toggling all-day mode", async () => {
    await flush(() => { root.render(<Calendar canWrite />); }); await click(button("Family walk"));
    const startOffset = required(Array.from(host.querySelectorAll("label")).find((label) => label.textContent === "Start offset")?.querySelector("input") ?? null);
    const endOffset = required(Array.from(host.querySelectorAll("label")).find((label) => label.textContent === "End offset")?.querySelector("input") ?? null);
    await input("Time zone (IANA)", "America/New_York");
    expect(startOffset.value).toBe(""); expect(endOffset.value).toBe("");
    await input("Start offset", "-04:00"); await input("End offset", "-04:00");
    const allDay = required(Array.from(host.querySelectorAll("label")).find((label) => label.textContent === "All day")?.querySelector("input") ?? null);
    await click(allDay); await click(allDay);
    const returnedStartOffset = required(Array.from(host.querySelectorAll("label")).find((label) => label.textContent === "Start offset")?.querySelector("input") ?? null);
    const returnedEndOffset = required(Array.from(host.querySelectorAll("label")).find((label) => label.textContent === "End offset")?.querySelector("input") ?? null);
    expect(returnedStartOffset.value).toBe(""); expect(returnedEndOffset.value).toBe("");
  });
  it("edits a point event and explicitly controls an unspecified end", async () => {
    const fixture = data();
    const point = event();
    point.payload.start = { dateTime: `${dateKey(new Date())}T00:00:00Z`, timeZone: "UTC" };
    point.payload.end = { ...point.payload.start };
    point.payload.endTimeUnspecified = true;
    fixture.events = [point];
    fetcher.mockImplementation(() => Promise.resolve(new Response(JSON.stringify(fixture), { status: 200 })));
    await flush(() => { root.render(<Calendar canWrite />); });
    expect(host.textContent).toContain("End time unknown");
    await click(button("Family walk"));
    const unknown = required(Array.from(host.querySelectorAll("label")).find((label) => label.textContent === "End time unknown")?.querySelector("input") ?? null);
    const end = required(Array.from(host.querySelectorAll("label")).find((label) => label.textContent === "Ends")?.querySelector("input") ?? null);
    expect(unknown.checked).toBe(true); expect(end.disabled).toBe(true);
    await input("Title", "Unknown end edited");
    await flush(() => { required(host.querySelector("form")).dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    const first = postBodies()[0]?.payload as { endTimeUnspecified: boolean; start: unknown; end: unknown };
    expect(first.endTimeUnspecified).toBe(true); expect(first.end).toEqual(first.start);
    await click(button("Family walk"));
    const unknownAgain = required(Array.from(host.querySelectorAll("label")).find((label) => label.textContent === "End time unknown")?.querySelector("input") ?? null);
    await click(unknownAgain);
    await flush(() => { required(host.querySelector("form")).dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    const second = postBodies()[1]?.payload as { endTimeUnspecified?: boolean; start: unknown; end: unknown };
    expect(second.endTimeUnspecified).toBeUndefined(); expect(second.end).toEqual(second.start);
  });
  it("still rejects an all-day end equal to its start", async () => {
    await flush(() => { root.render(<Calendar canWrite />); });
    await click(button("Family walk"));
    const allDay = required(Array.from(host.querySelectorAll("label")).find((label) => label.textContent === "All day")?.querySelector("input") ?? null);
    await click(allDay);
    const start = required(Array.from(host.querySelectorAll("label")).find((label) => label.textContent === "Starts")?.querySelector("input") ?? null);
    await input("End date (exclusive)", start.value);
    await flush(() => { required(host.querySelector("form")).dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    expect(postBodies()).toHaveLength(0);
    expect(host.textContent).toContain("all-day end dates must be later");
  });
  it("keeps the draft on conflict and deletes with its original revision", async () => {
    await flush(() => { root.render(<Calendar canWrite />); }); await click(button("Family walk")); await input("Title", "Changed walk");
    fetcher.mockImplementation((_, options) => Promise.resolve(new Response(JSON.stringify(data()), { status: options?.method === "POST" ? 409 : 200 })));
    await flush(() => { required(host.querySelector("form")).dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    expect(host.textContent).toContain("This event changed elsewhere"); expect(host.querySelector<HTMLInputElement>("form input")?.value).toBe("Changed walk");
    expect(postBodies()[0]).toMatchObject({ id: "local-1", revision: 3, group: "family" });
    fetcher.mockImplementation(() => Promise.resolve(new Response(JSON.stringify(data()), { status: 200 })));
    vi.spyOn(window, "confirm").mockReturnValue(true); await click(button("Delete event"));
    expect(postBodies()[1]).toEqual({ id: "local-1", revision: 3, group: "family" }); expect(host.textContent).toContain("Deleted locally");
  });
  it("explains provider restrictions from pending operations after a successful pull", async () => {
    const fixture = data();
    fixture.status.endpoints[0] = { id: "icloud", group_id: "family", enabled: true, last_success: 1, last_error: null };
    fixture.status.pending = [{ endpoint_id: "icloud", last_error: "google_event_type_restricted" }];
    fetcher.mockImplementation(() => Promise.resolve(new Response(JSON.stringify(fixture), { status: 200 })));
    await flush(() => { root.render(<Calendar canWrite />); });
    expect(host.textContent).toContain("Google restricts changes to this Gmail or Contacts event");
  });
  it("retains last fetched events on a network failure", async () => {
    await flush(() => { root.render(<Calendar canWrite />); }); fetcher.mockRejectedValue(new Error("offline")); await click(button("Refresh"));
    expect(host.textContent).toContain("Family walk"); expect(host.textContent).toContain("Calendar could not be loaded");
  });
  it("shows disabled configuration and mounts as an inactive island", async () => {
    fetcher.mockResolvedValue(new Response(JSON.stringify({ ...data(), enabled: false, groups: [], events: [] }), { status: 200 }));
    await flush(() => { root.render(<Calendar canWrite={false} />); }); expect(host.textContent).toContain("Calendar synchronization is not configured");
    const island = mountCalendar(document.createElement("div"), false); await flush(() => { island.setActive(false); });
    const before = fetcher.mock.calls.length; await flush(() => { island.setActive(true); }); expect(fetcher.mock.calls.length).toBeGreaterThan(before);
    await flush(() => { island.destroy(); });
  });
});
