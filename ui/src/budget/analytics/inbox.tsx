// Owner-private live active alerts inbox (2D, migration 0019 + OpenAPI 0.24.0).
// Pure read-only GET /analytics/inbox evaluates active consent, mortgage and spending-limit alerts
// in one SQLite snapshot; display names are resolved locally from item.ref (never raw hashes or
// server prose). Read/unread updates use strict anti-ABA expected_reads_version CAS.
import { useCallback, useEffect, useRef, useState } from "react";
import type { BudgetApi, BudgetResult } from "../api";
import { consentHealthLabels } from "../consent_health";
import { when } from "../model";
import type { BudgetConnection } from "../model";
import { ANALYTICS_ERROR, INBOX_MARK_BATCH_MAX, RELOAD_ERRORS, inboxItemKey, inboxRowKey, labelOf,
  pluralPl } from "./model";
import type { AnalyticsCategory, AnalyticsInbox, AnalyticsInboxConsentItem, AnalyticsInboxItem,
  AnalyticsInboxKey, AnalyticsInboxMarkSaved, AnalyticsInboxMortgageItem, AnalyticsInboxSpendingLimitItem,
  AnalyticsMortgageSchedule } from "./model";

export const inboxLabels = {
  en: {
    title: "Owner inbox",
    intro: "Live active alerts from bank consents, mortgage reminders and monthly spending limits. Resolved alerts disappear automatically even if unread. No emails, push notifications or shared calendars are triggered here.",
    evaluatedFor: (date: string, tz: string) => `Evaluated for ${date} (${tz})`,
    unreadCount: (count: number) => `Unread: ${String(count)}`,
    capacity: (used: number, cap: number) => `Saved read markers: ${String(used)} / ${String(cap)}`,
    loading: "Loading inbox…",
    error: "Inbox could not be loaded. Try again.",
    empty: "No active alerts right now.",
    emptyUnread: "No unread active alerts right now.",
    filterLabel: "Alert filter",
    filterAll: "All active",
    filterUnread: "Unread only",
    markAllRead: "Mark all unread read",
    markedPartial: (marked: number, total: number) =>
      `Marked ${String(marked)} of ${String(total)} unread alerts. Click again to mark the rest.`,
    markRead: "Mark read",
    markUnread: "Mark unread",
    readAt: (formatted: string) => `Read ${formatted}`,
    unreadBadge: "Unread",
    readBadge: "Read",
    openConsent: "Open Consent status",
    openAccounts: "Open Accounts",
    openMortgages: "Open Mortgages",
    openLimits: "Open Spending limits",
    kinds: {
      consent_renewal: "Bank consent",
      mortgage_due: "Mortgage",
      spending_limit: "Spending limit",
    },
    consentStatuses: {
      attention_required: "Attention required",
      expired: "Expired",
      due_soon: "Renewal due soon",
    },
    mortgageStatuses: {
      overdue: "Overdue",
      due_today: "Due today",
      due_soon: "Due soon",
    },
    limitStatuses: {
      exceeded: "Exceeded",
      at_limit: "At limit",
      approaching: "Approaching limit",
    },
    attentions: consentHealthLabels.en.attentions,
    attentionFallback: consentHealthLabels.en.attentionFallback,
    validUntil: (formatted: string) => `Valid until ${formatted}`,
    daysLeft: (days: number) => `${String(days)} ${days === 1 ? "day" : "days"} left`,
    expired: "Expired",
    paused: "Paused",
    dueOn: (date: string) => `Due ${date}`,
    daysOverdue: (days: number) => `${String(days)} ${days === 1 ? "day" : "days"} overdue`,
    daysUntil: (days: number) => `In ${String(days)} ${days === 1 ? "day" : "days"}`,
    freshnessStale: "Ledger data may be outdated — verify payment status separately.",
    limitUsage: (period: string, percent: number, warning: number, currency: string) =>
      `${period} · ${String(percent)}% used (warning at ${String(warning)}%) · ${currency}`,
    unknownBank: "Bank connection",
    unknownMortgage: "Mortgage schedule",
    unknownCategory: "Spending category",
    conflict: "Inbox read state changed elsewhere; the latest alerts were loaded.",
    sourceChanged: "One or more alerts resolved or changed before saving; the latest active alerts were loaded.",
    limitCap: "Read marker limit (500) reached. Mark an existing read alert as unread to free a slot.",
    saveError: "Could not update alert status. Try again.",
  },
  pl: {
    title: "Skrzynka właściciela",
    intro: "Aktywne alerty na żywo ze zgód bankowych, przypomnień hipotecznych i miesięcznych limitów wydatków. Rozwiązane alerty znikają automatycznie, nawet jeśli były nieprzeczytane. Nie wysyła e-maili, powiadomień push ani wpisów do wspólnych kalendarzy.",
    evaluatedFor: (date: string, tz: string) => `Stan na ${date} (${tz})`,
    unreadCount: (count: number) => `Nieprzeczytane: ${String(count)}`,
    capacity: (used: number, cap: number) => `Zapisane znaczniki przeczytania: ${String(used)} / ${String(cap)}`,
    loading: "Wczytywanie skrzynki…",
    error: "Nie udało się wczytać skrzynki. Spróbuj ponownie.",
    empty: "Brak aktywnych alertów.",
    emptyUnread: "Brak nieprzeczytanych aktywnych alertów.",
    filterLabel: "Filtr alertów",
    filterAll: "Wszystkie aktywne",
    filterUnread: "Tylko nieprzeczytane",
    markAllRead: "Oznacz wszystkie jako przeczytane",
    markedPartial: (marked: number, total: number) =>
      `Oznaczono ${String(marked)} z ${String(total)} nieprzeczytanych alertów. Kliknij ponownie, aby oznaczyć pozostałe.`,
    markRead: "Oznacz jako przeczytane",
    markUnread: "Oznacz jako nieprzeczytane",
    readAt: (formatted: string) => `Przeczytano ${formatted}`,
    unreadBadge: "Nieprzeczytane",
    readBadge: "Przeczytane",
    openConsent: "Otwórz Stan zgód",
    openAccounts: "Otwórz Konta",
    openMortgages: "Otwórz Hipotekę",
    openLimits: "Otwórz Limity wydatków",
    kinds: {
      consent_renewal: "Zgoda bankowa",
      mortgage_due: "Hipoteka",
      spending_limit: "Limit wydatków",
    },
    consentStatuses: {
      attention_required: "Wymaga uwagi",
      expired: "Wygasła",
      due_soon: "Wkrótce odnowienie",
    },
    mortgageStatuses: {
      overdue: "Po terminie",
      due_today: "Termin dziś",
      due_soon: "Wkrótce termin",
    },
    limitStatuses: {
      exceeded: "Przekroczony",
      at_limit: "Osiągnięty limit",
      approaching: "Blisko limitu",
    },
    attentions: consentHealthLabels.pl.attentions,
    attentionFallback: consentHealthLabels.pl.attentionFallback,
    validUntil: (formatted: string) => `Ważna do ${formatted}`,
    daysLeft: (days: number) => `Pozostało ${String(days)} ${pluralPl(days, "dzień", "dni", "dni")}`,
    expired: "Wygasła",
    paused: "Wstrzymane",
    dueOn: (date: string) => `Termin ${date}`,
    daysOverdue: (days: number) => `${String(days)} ${pluralPl(days, "dzień", "dni", "dni")} po terminie`,
    daysUntil: (days: number) => `Za ${String(days)} ${pluralPl(days, "dzień", "dni", "dni")}`,
    freshnessStale: "Dane księgowe mogą być nieaktualne — potwierdź status płatności osobno.",
    limitUsage: (period: string, percent: number, warning: number, currency: string) =>
      `${period} · wykorzystano ${String(percent)}% (ostrzeżenie od ${String(warning)}%) · ${currency}`,
    unknownBank: "Połączenie z bankiem",
    unknownMortgage: "Harmonogram hipoteczny",
    unknownCategory: "Kategoria wydatków",
    conflict: "Stan skrzynki zmienił się w innym miejscu; wczytano aktualne alerty.",
    sourceChanged: "Co najmniej jeden alert zmienił się lub zniknął przed zapisem; wczytano aktualne aktywne alerty.",
    limitCap: "Osiągnięto limit znaczników przeczytania (500). Oznacz wybrany alert jako nieprzeczytany, aby zwolnić miejsce.",
    saveError: "Nie udało się zapisać statusu alertu. Spróbuj ponownie.",
  },
};
export type InboxLabels = typeof inboxLabels.en;

type Loss = (result: { ok: boolean; status?: number; error?: string }) => boolean;
type Shown = { state: "loading" } | { state: "error" } | { state: "ready"; data: AnalyticsInbox };
type Filter = "all" | "unread";

export interface InboxViewProps {
  api: Pick<BudgetApi, "inbox" | "markInbox" | "mortgages" | "categories">;
  connections: readonly BudgetConnection[];
  consentEnabled: boolean;
  text: InboxLabels;
  locale: string;
  revision: number;
  onAccessLoss: Loss;
  onOpenAccounts: (openConsentHealth: boolean) => void;
  onOpenMortgages: () => void;
  onOpenLimits: () => void;
}

interface LookupMaps {
  banks: ReadonlyMap<string, string>;
  schedules: ReadonlyMap<string, string>;
  categories: ReadonlyMap<string, string>;
}

function saveFailure(text: InboxLabels, error: string): string {
  if (error === ANALYTICS_ERROR.sourceChanged) return text.sourceChanged;
  if (RELOAD_ERRORS.has(error)) return text.conflict;
  if (error === ANALYTICS_ERROR.limit) return text.limitCap;
  return text.saveError;
}

function attentionText(text: InboxLabels, code: string): string {
  return (text.attentions as Readonly<Record<string, string>>)[code] ?? text.attentionFallback;
}

function mortgageTiming(params: AnalyticsInboxMortgageItem["params"], text: InboxLabels): string {
  if (params.days_until_due < 0) return text.daysOverdue(-params.days_until_due);
  if (params.days_until_due === 0) return text.mortgageStatuses.due_today;
  return text.daysUntil(params.days_until_due);
}

function ConsentFacts({ item, text, locale }: { item: AnalyticsInboxConsentItem; text: InboxLabels; locale: string }) {
  const { params } = item;
  const expiry = when(params.valid_until, locale);
  return (
    <>
      {expiry === null ? null : <span>{text.validUntil(expiry)}</span>}
      {params.days_remaining === null ? null : (
        <span>{params.days_remaining < 0 || params.status === "expired"
          ? text.expired : text.daysLeft(params.days_remaining)}</span>
      )}
      {params.paused ? <span className="budget-warn">{text.paused}</span> : null}
      {params.attention === null ? null : <span className="budget-warn">{attentionText(text, params.attention)}</span>}
    </>
  );
}

function MortgageFacts({ item, text }: { item: AnalyticsInboxMortgageItem; text: InboxLabels }) {
  const { params } = item;
  return (
    <>
      <span>{text.dueOn(params.due_date)}</span>
      <span>{mortgageTiming(params, text)}</span>
      <span>{params.currency}</span>
    </>
  );
}

function SpendingLimitFacts({ item, text }: { item: AnalyticsInboxSpendingLimitItem; text: InboxLabels }) {
  const { params } = item;
  return <span>{text.limitUsage(params.period, params.percent_used_floor, params.warning_percent, params.currency)}</span>;
}

interface RowProps {
  item: AnalyticsInboxItem;
  lookups: LookupMaps;
  consentEnabled: boolean;
  busy: boolean;
  text: InboxLabels;
  locale: string;
  onToggleRead: (item: AnalyticsInboxItem) => void;
  onOpenAccounts: (openConsentHealth: boolean) => void;
  onOpenMortgages: () => void;
  onOpenLimits: () => void;
}

function subjectLabel(item: AnalyticsInboxItem, lookups: LookupMaps, text: InboxLabels): string {
  if (item.kind === "consent_renewal") return lookups.banks.get(item.ref.connection_id) ?? text.unknownBank;
  if (item.kind === "mortgage_due") return lookups.schedules.get(item.ref.schedule_id) ?? text.unknownMortgage;
  return lookups.categories.get(item.ref.category_id) ?? text.unknownCategory;
}

function statusLabel(item: AnalyticsInboxItem, text: InboxLabels): string {
  if (item.kind === "consent_renewal") return labelOf(text.consentStatuses, item.params.status);
  if (item.kind === "mortgage_due") return labelOf(text.mortgageStatuses, item.params.status);
  return labelOf(text.limitStatuses, item.params.status);
}

function InboxRow({
  item, lookups, consentEnabled, busy, text, locale,
  onToggleRead, onOpenAccounts, onOpenMortgages, onOpenLimits,
}: RowProps) {
  const readStamp = when(item.read_at, locale);
  const title = subjectLabel(item, lookups, text);
  const openAction = item.kind === "consent_renewal"
    ? { label: consentEnabled ? text.openConsent : text.openAccounts, run: () => { onOpenAccounts(consentEnabled); } }
    : item.kind === "mortgage_due"
      ? { label: text.openMortgages, run: onOpenMortgages }
      : { label: text.openLimits, run: onOpenLimits };

  return (
    <li className={item.read ? "budget-entry budget-inbox-item is-read" : "budget-entry budget-inbox-item"}>
      <div className="budget-limit-main">
        <span className="budget-entry-text">
          <span className="budget-chip">{labelOf(text.kinds, item.kind)}</span>{" "}
          <strong>{title}</strong>
        </span>
        <div className="budget-actions">
          <span className="budget-chip budget-inbox-status" data-status={item.params.status}>
            {statusLabel(item, text)}
          </span>
          <span className="budget-chip">{item.read ? text.readBadge : text.unreadBadge}</span>
        </div>
      </div>
      <div className="budget-entry-facts">
        {item.kind === "consent_renewal" ? <ConsentFacts item={item} text={text} locale={locale} /> : null}
        {item.kind === "mortgage_due" ? <MortgageFacts item={item} text={text} /> : null}
        {item.kind === "spending_limit" ? <SpendingLimitFacts item={item} text={text} /> : null}
        {readStamp === null ? null : <span>{text.readAt(readStamp)}</span>}
      </div>
      {item.kind === "mortgage_due" && item.params.freshness_stale ? (
        <p className="budget-muted">{text.freshnessStale}</p>
      ) : null}
      <div className="budget-entry-facts">
        <button type="button" disabled={busy} onClick={() => { onToggleRead(item); }}>
          {item.read ? text.markUnread : text.markRead}
        </button>
        <button type="button" disabled={busy} onClick={openAction.run}>{openAction.label}</button>
      </div>
    </li>
  );
}

export function InboxView({
  api, connections, consentEnabled, text, locale, revision, onAccessLoss,
  onOpenAccounts, onOpenMortgages, onOpenLimits,
}: InboxViewProps) {
  const [shown, setShown] = useState<Shown>({ state: "loading" });
  const [schedules, setSchedules] = useState<readonly AnalyticsMortgageSchedule[]>([]);
  const [categories, setCategories] = useState<readonly AnalyticsCategory[]>([]);
  const [filter, setFilter] = useState<Filter>("all");
  const [changes, setChanges] = useState(0);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ kind: "error" | "info"; text: string } | null>(null);
  const generation = useRef(0);

  useEffect(() => {
    const token = ++generation.current;
    setShown({ state: "loading" });
    setBusy(false);
    void api.inbox().then((result) => {
      // Barrier runs before generation check so hiding or reloading never swallows a 401/403/404.
      if (onAccessLoss(result) || token !== generation.current) return;
      if (!result.ok) {
        setShown({ state: "error" });
        return;
      }
      setShown({ state: "ready", data: result.data });
      if (result.data.items.some((item) => item.kind === "mortgage_due")) {
        void api.mortgages().then((res) => {
          if (onAccessLoss(res) || token !== generation.current) return;
          if (res.ok) setSchedules(res.data.schedules);
        });
      }
      if (result.data.items.some((item) => item.kind === "spending_limit")) {
        void api.categories().then((res) => {
          if (onAccessLoss(res) || token !== generation.current) return;
          if (res.ok) setCategories(res.data.categories);
        });
      }
    });
  }, [api, revision, changes, onAccessLoss]);
  useEffect(() => () => { generation.current += 1; }, []);

  const reload = useCallback(() => { setChanges((current) => current + 1); }, []);
  const mutate = useCallback(async (items: AnalyticsInboxKey[], read: boolean, version: string, partialTotal?: number) => {
    const token = generation.current;
    setBusy(true);
    setNotice(null);
    const result: BudgetResult<{ inbox: AnalyticsInboxMarkSaved }> = await api.markInbox({
      items, read, expected_reads_version: version,
    });
    if (onAccessLoss(result) || token !== generation.current) return;
    setBusy(false);
    if (!result.ok) {
      if (RELOAD_ERRORS.has(result.error)) reload();
      setNotice({ kind: "error", text: saveFailure(text, result.error) });
      return;
    }
    if (partialTotal !== undefined && partialTotal > items.length) {
      setNotice({ kind: "info", text: text.markedPartial(result.data.inbox.changed_count, partialTotal) });
    }
    reload();
  }, [api, onAccessLoss, reload, text]);

  const lookups: LookupMaps = {
    banks: new Map(connections.map((item) => [item.id, item.bank_name])),
    schedules: new Map(schedules.map((item) => [item.id, item.label])),
    categories: new Map(categories.map((item) => [item.id, item.name])),
  };

  const data = shown.state === "ready" ? shown.data : null;
  const visible = data === null ? [] : filter === "unread" ? data.items.filter((item) => !item.read) : data.items;
  const unreadBatch = data === null ? [] : data.items.filter((item) => !item.read).slice(0, INBOX_MARK_BATCH_MAX);
  const atCap = data !== null && data.reads_count >= data.reads_cap;

  return (
    <section className="budget-month" aria-label={text.title}>
      <p className="budget-muted">{text.intro}</p>
      {data === null ? null : (
        <div className="budget-entry-facts">
          <span>{text.evaluatedFor(data.evaluation_date, data.evaluation_timezone)}</span>
          <strong className={data.unread_count > 0 ? "budget-warn" : undefined}>
            {text.unreadCount(data.unread_count)}
          </strong>
          <span className={atCap ? "budget-warn" : undefined}>
            {text.capacity(data.reads_count, data.reads_cap)}
          </span>
        </div>
      )}
      {notice === null ? null : (
        <p className="budget-notice" role={notice.kind === "error" ? "alert" : "status"}>{notice.text}</p>
      )}
      {shown.state === "loading" ? <p className="budget-muted" role="status">{text.loading}</p> : null}
      {shown.state === "error" ? <p className="budget-notice" role="alert">{text.error}</p> : null}
      {data === null ? null : (
        <section className="budget-entries" aria-label={text.title}>
          <div className="budget-limit-main">
            <div className="budget-views" role="group" aria-label={text.filterLabel}>
              <button type="button" aria-pressed={filter === "all"} disabled={busy}
                onClick={() => { setFilter("all"); }}>{text.filterAll}</button>
              <button type="button" aria-pressed={filter === "unread"} disabled={busy}
                onClick={() => { setFilter("unread"); }}>{text.filterUnread}</button>
            </div>
            <button type="button" className="budget-primary" disabled={busy || unreadBatch.length === 0}
              onClick={() => { void mutate(unreadBatch.map(inboxItemKey), true, data.reads_version, data.unread_count); }}>
              {text.markAllRead}
            </button>
          </div>
          {visible.length === 0 ? (
            <p className="budget-muted">{filter === "unread" ? text.emptyUnread : text.empty}</p>
          ) : (
            <ul className="budget-entry-list">
              {visible.map((item) => (
                <InboxRow key={inboxRowKey(item)} item={item} lookups={lookups} consentEnabled={consentEnabled}
                  busy={busy} text={text} locale={locale}
                  onToggleRead={(entry) => { void mutate([inboxItemKey(entry)], !entry.read, data.reads_version); }}
                  onOpenAccounts={onOpenAccounts} onOpenMortgages={onOpenMortgages} onOpenLimits={onOpenLimits} />
              ))}
            </ul>
          )}
        </section>
      )}
    </section>
  );
}
