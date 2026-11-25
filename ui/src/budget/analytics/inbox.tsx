// Owner-private live active alerts inbox (2D, migration 0019 + OpenAPI 0.24.0).
// GET /analytics/inbox evaluates owner alerts in one SQLite snapshot.
// Read/unread updates use strict anti-ABA expected_reads_version CAS.
import { useCallback, useEffect, useRef, useState } from "react";
import { t, tPlural } from "../../i18n";
import type { BudgetApi, BudgetResult } from "../api";
import { when } from "../model";
import type { BudgetConnection } from "../model";
import { CategorySwatch } from "./category_badge";
import { buildCategoryColors } from "./category_colors";
import type { CategoryColorResolver } from "./category_colors";
import { ANALYTICS_ERROR, CONSENT_ATTENTION_KEYS, INBOX_CONSENT_STATUS_KEYS, INBOX_KIND_KEYS,
  INBOX_LIMIT_STATUS_KEYS, INBOX_MARK_BATCH_MAX, INBOX_MORTGAGE_STATUS_KEYS, RELOAD_ERRORS, inboxItemKey,
  inboxRowKey, labelOf } from "./model";
import type { AnalyticsCategory, AnalyticsInbox, AnalyticsInboxConsentItem, AnalyticsInboxItem,
  AnalyticsInboxKey, AnalyticsInboxMarkSaved, AnalyticsInboxMortgageItem, AnalyticsInboxSpendingLimitItem,
  AnalyticsMortgageSchedule } from "./model";

type Loss = (result: { ok: boolean; status?: number; error?: string }) => boolean;
type Shown = { state: "loading" } | { state: "error" } | { state: "ready"; data: AnalyticsInbox };
type Filter = "all" | "unread";

export interface InboxViewProps {
  api: Pick<BudgetApi, "inbox" | "markInbox" | "mortgages" | "categories">;
  connections: readonly BudgetConnection[];
  consentEnabled: boolean;
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
  categoryColors: CategoryColorResolver;
}

function saveFailure(error: string): string {
  if (error === ANALYTICS_ERROR.sourceChanged) return t("budget.inbox.sourceChanged");
  if (RELOAD_ERRORS.has(error)) return t("budget.inbox.conflict");
  if (error === ANALYTICS_ERROR.limit) return t("budget.inbox.limitCap");
  return t("budget.inbox.saveError");
}

function attentionText(code: string): string {
  const key = CONSENT_ATTENTION_KEYS[code];
  return key !== undefined ? t(key) : t("budget.consent.attentionFallback");
}

function mortgageTiming(params: AnalyticsInboxMortgageItem["params"]): string {
  if (params.days_until_due < 0) return tPlural("budget.inbox.daysOverdue", -params.days_until_due);
  if (params.days_until_due === 0) return t("budget.inbox.mortgageStatuses.due_today");
  return tPlural("budget.inbox.daysUntil", params.days_until_due);
}

function ConsentFacts({ item, locale }: { item: AnalyticsInboxConsentItem; locale: string }) {
  const { params } = item;
  const expiry = when(params.valid_until, locale);
  return (
    <>
      {expiry === null ? null : <span>{t("budget.consent.validUntil", { date: expiry })}</span>}
      {params.days_remaining === null ? null : (
        <span>{params.days_remaining < 0 || params.status === "expired"
          ? t("budget.consent.expired") : tPlural("budget.consent.daysLeft", params.days_remaining)}</span>
      )}
      {params.paused ? <span className="budget-warn">{t("budget.consent.paused")}</span> : null}
      {params.attention === null ? null : <span className="budget-warn">{attentionText(params.attention)}</span>}
    </>
  );
}

function MortgageFacts({ item }: { item: AnalyticsInboxMortgageItem }) {
  const { params } = item;
  return (
    <>
      <span>{t("budget.inbox.dueOn", { date: params.due_date })}</span>
      <span>{mortgageTiming(params)}</span>
      <span>{params.currency}</span>
    </>
  );
}

function SpendingLimitFacts({ item }: { item: AnalyticsInboxSpendingLimitItem }) {
  const { params } = item;
  return (
    <span>
      {t("budget.inbox.limitUsage", {
        period: params.period,
        percent: params.percent_used_floor,
        warning: params.warning_percent,
        currency: params.currency,
      })}
    </span>
  );
}

interface RowProps {
  item: AnalyticsInboxItem;
  lookups: LookupMaps;
  consentEnabled: boolean;
  busy: boolean;
  locale: string;
  onToggleRead: (item: AnalyticsInboxItem) => void;
  onOpenAccounts: (openConsentHealth: boolean) => void;
  onOpenMortgages: () => void;
  onOpenLimits: () => void;
}

function subjectLabel(item: AnalyticsInboxItem, lookups: LookupMaps): string {
  if (item.kind === "device_added") {
    const name = item.params.actor === undefined ? item.params.device_name
      : `${item.params.device_name} · ${item.params.actor}`;
    return t("budget.inbox.newDevice", { name, date: item.params.date });
  }
  if (item.kind === "consent_renewal") return lookups.banks.get(item.ref.connection_id) ?? t("budget.inbox.unknownBank");
  if (item.kind === "mortgage_due") return lookups.schedules.get(item.ref.schedule_id) ?? t("budget.inbox.unknownMortgage");
  return lookups.categories.get(item.ref.category_id) ?? t("budget.inbox.unknownCategory");
}

function statusLabel(item: AnalyticsInboxItem): string | null {
  if (item.kind === "device_added") return null;
  if (item.kind === "consent_renewal") return labelOf(INBOX_CONSENT_STATUS_KEYS, item.params.status);
  if (item.kind === "mortgage_due") return labelOf(INBOX_MORTGAGE_STATUS_KEYS, item.params.status);
  return labelOf(INBOX_LIMIT_STATUS_KEYS, item.params.status);
}

function InboxRow({
  item, lookups, consentEnabled, busy, locale,
  onToggleRead, onOpenAccounts, onOpenMortgages, onOpenLimits,
}: RowProps) {
  const readStamp = when(item.read_at, locale);
  const title = subjectLabel(item, lookups);
  const status = statusLabel(item);
  const openAction = item.kind === "device_added" ? null : item.kind === "consent_renewal"
    ? { label: consentEnabled ? t("budget.inbox.openConsent") : t("budget.inbox.openAccounts"), run: () => { onOpenAccounts(consentEnabled); } }
    : item.kind === "mortgage_due"
      ? { label: t("budget.inbox.openMortgages"), run: onOpenMortgages }
      : { label: t("budget.inbox.openLimits"), run: onOpenLimits };

  return (
    <li className={item.read ? "budget-entry budget-inbox-item is-read" : "budget-entry budget-inbox-item"}>
      <div className="budget-limit-main">
        <span className="budget-entry-text">
          <span className="budget-chip">{labelOf(INBOX_KIND_KEYS, item.kind)}</span>{" "}
          {item.kind === "spending_limit" ? (
            <CategorySwatch color={lookups.categoryColors.get(item.ref.category_id)} />
          ) : null}
          <strong>{title}</strong>
        </span>
        <div className="budget-actions">
          {status === null ? null : (
            <span className="budget-chip budget-inbox-status" data-status={item.params.status}>{status}</span>
          )}
          <span className="budget-chip">{item.read ? t("budget.inbox.readBadge") : t("budget.inbox.unreadBadge")}</span>
        </div>
      </div>
      <div className="budget-entry-facts">
        {item.kind === "consent_renewal" ? <ConsentFacts item={item} locale={locale} /> : null}
        {item.kind === "mortgage_due" ? <MortgageFacts item={item} /> : null}
        {item.kind === "spending_limit" ? <SpendingLimitFacts item={item} /> : null}
        {readStamp === null ? null : <span>{t("budget.inbox.readAt", { date: readStamp })}</span>}
      </div>
      {item.kind === "mortgage_due" && item.params.freshness_stale ? (
        <p className="budget-muted">{t("budget.inbox.freshnessStale")}</p>
      ) : null}
      <div className="budget-entry-facts">
        <button type="button" disabled={busy} onClick={() => { onToggleRead(item); }}>
          {item.read ? t("budget.inbox.markUnread") : t("budget.inbox.markRead")}
        </button>
        {openAction === null ? null : (
          <button type="button" disabled={busy} onClick={openAction.run}>{openAction.label}</button>
        )}
      </div>
    </li>
  );
}

export function InboxView({
  api, connections, consentEnabled, locale, revision, onAccessLoss,
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
      setNotice({ kind: "error", text: saveFailure(result.error) });
      return;
    }
    if (partialTotal !== undefined && partialTotal > items.length) {
      setNotice({
        kind: "info",
        text: t("budget.inbox.markedPartial", { marked: result.data.inbox.changed_count, total: partialTotal }),
      });
    }
    reload();
  }, [api, onAccessLoss, reload]);

  const lookups: LookupMaps = {
    banks: new Map(connections.map((item) => [item.id, item.bank_name])),
    schedules: new Map(schedules.map((item) => [item.id, item.label])),
    categories: new Map(categories.map((item) => [item.id, item.name])),
    categoryColors: buildCategoryColors(categories),
  };

  const data = shown.state === "ready" ? shown.data : null;
  const visible = data === null ? [] : filter === "unread" ? data.items.filter((item) => !item.read) : data.items;
  const unreadBatch = data === null ? [] : data.items.filter((item) => !item.read).slice(0, INBOX_MARK_BATCH_MAX);
  const atCap = data !== null && data.reads_count >= data.reads_cap;
  const title = t("budget.inbox.title");

  return (
    <section className="budget-month" aria-label={title}>
      <p className="budget-muted">{t("budget.inbox.intro")}</p>
      {data === null ? null : (
        <div className="budget-entry-facts">
          <span>{t("budget.inbox.evaluatedFor", { date: data.evaluation_date, tz: data.evaluation_timezone })}</span>
          <strong className={data.unread_count > 0 ? "budget-warn" : undefined}>
            {t("budget.inbox.unreadCount", { count: data.unread_count })}
          </strong>
          <span className={atCap ? "budget-warn" : undefined}>
            {t("budget.inbox.capacity", { used: data.reads_count, cap: data.reads_cap })}
          </span>
        </div>
      )}
      {notice === null ? null : (
        <p className="budget-notice" role={notice.kind === "error" ? "alert" : "status"}>{notice.text}</p>
      )}
      {shown.state === "loading" ? <p className="budget-muted" role="status">{t("budget.inbox.loading")}</p> : null}
      {shown.state === "error" ? <p className="budget-notice" role="alert">{t("budget.inbox.error")}</p> : null}
      {data === null ? null : (
        <section className="budget-entries" aria-label={title}>
          <div className="budget-limit-main">
            <div className="budget-views" role="group" aria-label={t("budget.inbox.filterLabel")}>
              <button type="button" aria-pressed={filter === "all"} disabled={busy}
                onClick={() => { setFilter("all"); }}>{t("budget.inbox.filterAll")}</button>
              <button type="button" aria-pressed={filter === "unread"} disabled={busy}
                onClick={() => { setFilter("unread"); }}>{t("budget.inbox.filterUnread")}</button>
            </div>
            <button type="button" className="budget-primary" disabled={busy || unreadBatch.length === 0}
              onClick={() => { void mutate(unreadBatch.map(inboxItemKey), true, data.reads_version, data.unread_count); }}>
              {t("budget.inbox.markAllRead")}
            </button>
          </div>
          {visible.length === 0 ? (
            <p className="budget-muted">{filter === "unread" ? t("budget.inbox.emptyUnread") : t("budget.inbox.empty")}</p>
          ) : (
            <ul className="budget-entry-list">
              {visible.map((item) => (
                <InboxRow key={inboxRowKey(item)} item={item} lookups={lookups} consentEnabled={consentEnabled}
                  busy={busy} locale={locale}
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
