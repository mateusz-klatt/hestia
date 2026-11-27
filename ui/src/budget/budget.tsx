// Owner-private budget view (2C). Reads only the stored-data API; the "refresh" action queues a server job
// under the server's quotas and never talks to a bank from the browser. Data live only in component state.
import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import { createRoot } from "react-dom/client";
import { logout } from "../api/client";
import { currentLocale, t, tPlural } from "../i18n";
import { budgetApi, createBudgetApi } from "./api";
import type { AuthorizationCapabilities, BudgetApi, BudgetResult } from "./api";
import { ACTIVE_JOB, KINDS, accountLabel, defaultCreditLimitCurrency, effectiveState, formatAmount, freshness,
  hasActiveJob, idempotencyKey, isAccessLoss, isComponentLimit, isSessionUpgradeRequired, latestJob, limitState, newer,
  normalizeCreditCurrencyInput, normalizeCreditLimitInput, otherObservedCurrencies, presetRange, OPEN_FLOW, READY_FLOW,
  consentIntent, safeAuthorizationUrl, transactionDate, transactionText, validRange, when } from "./model";
import type { BudgetAccount, BudgetBalances, BudgetCapabilities, BudgetConnection, BudgetCreditLimitValue, BudgetJob,
  BudgetKind, BudgetConsent, BudgetConsentFlow, BudgetLimit, BudgetSharedLimit, BudgetTransaction, DateRange,
  LimitState } from "./model";
import { ConsentHealthPanel } from "./consent_health";
import { ForecastView } from "./analytics/forecast";
import { InboxView } from "./analytics/inbox";
import { MonthView } from "./analytics/month";
import { MonthBudgetView } from "./analytics/month_budget";
import { MortgagesView } from "./analytics/mortgages";
import { QuarantineView } from "./analytics/quarantine";
import { RecurringView } from "./analytics/recurring";
import { CategoriesView } from "./analytics/rules";
import { SpendingLimitsView } from "./analytics/spending_limits";
import { YearView } from "./analytics/year";
import { MAIN_BALANCE_TYPE_KEYS, MAIN_FRESHNESS_KEYS, MAIN_JOB_STATUS_KEYS, MAIN_KIND_KEYS, MAIN_SCOPE_KEYS,
  MAIN_VIEW_KEYS, hasAnalytics, labelOf } from "./analytics/model";
import type { RuleCandidate } from "./analytics/model";
import { LoginQr, ReceiptDevices } from "./devices";
import { ReceiptsView } from "./receipts";
import type { ReceiptsApi } from "./receipts_api";
import { createPendingReceiptStore } from "./receipts_model";
import "./budget.css";
import "./navigation.css";

const PRESET_KEYS = {
  "30": "budget.main.presets.30",
  "90": "budget.main.presets.90",
  year: "budget.main.presets.year",
  all: "budget.main.presets.all",
} as const;

type View = "monthly_budget" | "accounts" | "inbox" | "month" | "year" | "limits" | "recurring" | "mortgages" | "forecast" | "quarantine" | "rules" | "receipts";

const PRIMARY_VIEWS = ["monthly_budget", "month", "inbox"] as const;
const SECONDARY_VIEWS = [
  "accounts", "year", "limits", "recurring", "mortgages", "forecast", "quarantine", "rules",
] as const;
const SECONDARY_VIEW_SET: ReadonlySet<View> = new Set(SECONDARY_VIEWS);

function serverMonthToToday(month: string): (() => Date) | undefined {
  const match = /^(\d{4})-(0[1-9]|1[0-2])$/.exec(month);
  if (match === null) return undefined;
  const year = Number(match[1]);
  const monthIndex = Number(match[2]) - 1;
  return () => new Date(year, monthIndex, 15, 12);
}

type Load = "loading" | "ready" | "disabled" | "denied" | "error" | "serverChanged" | "sessionUpgrade";

function failureText(error: string): string {
  switch (error) {
    case "budget_version_conflict": return t("budget.main.conflict");
    case "budget_refresh_in_progress": return t("budget.main.busy");
    case "budget_connection_paused": return t("budget.main.pausedError");
    case "budget_refresh_unavailable": return t("budget.main.unavailable");
    default: return t("budget.main.saveError");
  }
}

function balanceLabel(type: string, name: string): string {
  const key = MAIN_BALANCE_TYPE_KEYS[type];
  return key === undefined ? (name || type) : t(key);
}

function jobText(job: BudgetJob, locale: string): string {
  const status = labelOf(MAIN_JOB_STATUS_KEYS, job.status);
  const next = job.next_attempt !== null && !ACTIVE_JOB.has(job.status) ? when(job.next_attempt, locale) : null;
  return [
    t("budget.main.lastRefresh", { status }),
    job.error,
    next === null ? null : t("budget.main.nextPossible", { when: next }),
  ].filter((part) => part !== null && part !== "").join(" · ");
}

export interface BudgetProps {
  active: boolean; api?: BudgetApi; receiptsApi?: ReceiptsApi; pollMs?: number;
  /** The user this page signed in as; budget data are shown only while the cookie still names them. */
  principal?: string | null;
  /** Leaves the page for the bank's authorization screen (tests inject a recorder). */
  navigate?: (url: string) => void;
  /** Runs after logout when signing in again for a v2 session (tests inject a recorder). */
  onLogout?: () => void;
  /** Where the standalone completion page lives. */
  completeUrl?: string;
}

export function Budget({ active, api: providedApi, receiptsApi: providedReceiptsApi, pollMs = 2000, principal,
  navigate = (url) => { window.location.assign(url); },
  onLogout = () => { window.location.reload(); },
  completeUrl = "/budget/connect/complete" }: BudgetProps) {
  const api = useMemo<BudgetApi>(
    () => providedApi ?? (principal === undefined || principal === null ? budgetApi : createBudgetApi(principal)),
    [providedApi, principal],
  );
  const effectiveReceiptsApi = providedReceiptsApi ?? api;
  const locale = currentLocale();
  const [load, setLoad] = useState<Load>("loading");
  const [sessionUpgrade, setSessionUpgrade] = useState(false);
  const [reloggingIn, setReloggingIn] = useState(false);
  const [connections, setConnections] = useState<BudgetConnection[]>([]);
  const [capabilities, setCapabilities] = useState<BudgetCapabilities | null>(null);
  const [authorization, setAuthorization] = useState<AuthorizationCapabilities | null>(null);
  const [authorizationLocked, setAuthorizationLocked] = useState(true);
  const authorizationLockedRef = useRef(true);
  const [deviceAccess, setDeviceAccess] = useState(true);
  const [deviceUnavailable, setDeviceUnavailable] = useState(false);
  const deviceCapabilityKey = useRef("");
  const [receiptAccess, setReceiptAccess] = useState(true);
  const [receiptUnavailable, setReceiptUnavailable] = useState(false);
  const [receiptEpoch, setReceiptEpoch] = useState(0);
  const receiptAccessRef = useRef(true);
  const receiptUnavailableRef = useRef(false);
  const receiptEpochRef = useRef(0);
  const [accounts, setAccounts] = useState<BudgetAccount[]>([]);
  const [jobs, setJobs] = useState<BudgetJob[]>([]);
  const [showLimits, setShowLimits] = useState(false);
  const [showConsentHealth, setShowConsentHealth] = useState(false);
  const [limits, setLimits] = useState<BudgetLimit[] | null | "error">(null);
  const [consents, setConsents] = useState<BudgetConsent[]>([]);
  const [pending, setPending] = useState<BudgetConsentFlow[]>([]);
  const [detail, setDetail] = useState<string | null>(null);
  const [view, setView] = useState<View>("monthly_budget");
  const [pendingView, setPendingView] = useState<View | null>(null);
  const [moreOpen, setMoreOpen] = useState(false);
  const [devicesFocus, setDevicesFocus] = useState(0);
  // A month chosen in the monthly budget or year view; the month view opens on it.
  const [focusMonth, setFocusMonth] = useState<string | undefined>(undefined);
  const [focusToday, setFocusToday] = useState<(() => Date) | undefined>(undefined);
  const [focusCurrency, setFocusCurrency] = useState<string | undefined>(undefined);
  const [focusProvisional, setFocusProvisional] = useState<{
    keys: readonly string[];
    total: number;
    truncated: boolean;
  } | undefined>(undefined);
  const [focusRule, setFocusRule] = useState<RuleCandidate | undefined>(undefined);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set());
  const retryKeys = useRef(new Map<string, string>());
  // Lifecycle guards: `alive` drops work finishing after unmount; every reload takes a new `generation`
  // so an older load can never overwrite a newer one; `epoch` changes only when data are cleared (signed
  // out, other principal, disabled), so no response started before that may bring the data back.
  const alive = useRef(true);
  const generation = useRef(0);
  const navGeneration = useRef(0);
  const epoch = useRef(0);
  const [sessionEpoch, setSessionEpoch] = useState(0);
  const [revision, setRevision] = useState(0);
  // Bumped whenever the view is hidden or the principal changes; a pending bank start must not navigate then.
  // A layout effect runs in the same commit as the DOM change, so no promise continuation can slip in between.
  const lifecycle = useRef(0);
  const activeRef = useRef(active);
  const principalRef = useRef(principal);
  const [committedPrincipal, setCommittedPrincipal] = useState(principal);
  const pendingReceiptActions = useMemo(() => createPendingReceiptStore(), [principal]);

  const bumpReceiptEpoch = useCallback(() => {
    receiptEpochRef.current += 1;
    setReceiptEpoch(receiptEpochRef.current);
  }, []);

  const setAuthLock = useCallback((locked: boolean) => {
    api.setAuthorizationLocked(locked);
    authorizationLockedRef.current = locked;
    setAuthorizationLocked(locked);
  }, [api]);

  useLayoutEffect(() => {
    activeRef.current = active;
    if (principalRef.current !== principal) {
      principalRef.current = principal;
      epoch.current += 1;
      generation.current += 1;
      navGeneration.current += 1;
      receiptEpochRef.current += 1;
      receiptAccessRef.current = true;
      receiptUnavailableRef.current = false;
      setSessionEpoch((current) => current + 1);
      setReceiptEpoch(receiptEpochRef.current);
      setCommittedPrincipal(principal);
      setConnections([]); setAccounts([]); setJobs([]); setDetail(null); setCapabilities(null); setBusy(new Set());
      api.setAuthorizationLocked(true);
      authorizationLockedRef.current = true;
      setAuthorization(null); setAuthorizationLocked(true);
      setReceiptAccess(true); setReceiptUnavailable(false);
      setLimits(null); setShowLimits(false); setShowConsentHealth(false); setConsents([]); setPending([]);
      setFocusMonth(undefined); setFocusToday(undefined); setFocusCurrency(undefined); setFocusProvisional(undefined); setFocusRule(undefined);
      setView("accounts");
      setPendingView(null);
      setMoreOpen(false);
      setDevicesFocus(0);
      setSessionUpgrade(false);
      setReloggingIn(false);
      setLoad("loading");
    }
    lifecycle.current += 1;
  }, [active, principal, api]);
  const [pollTick, setPollTick] = useState(0);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; generation.current += 1; navGeneration.current += 1; };
  }, []);
  useEffect(() => {
    const next = authorization === null ? "" : [
      authorization.user, authorization.own_budget?.can_view,
      authorization.own_budget?.can_use_receipts, authorization.own_budget?.can_manage_devices,
    ].join(":");
    if (next !== deviceCapabilityKey.current) {
      deviceCapabilityKey.current = next;
      setDeviceUnavailable(false);
    }
  }, [authorization]);

  /** The single access barrier: any endpoint reporting a lost principal or a disabled feature clears every
   *  private value at once and invalidates all pending loads, polls and saves. */
  const invalidate = useCallback((disabled: boolean, next?: Load) => {
    epoch.current += 1;
    generation.current += 1;
    navGeneration.current += 1;
    bumpReceiptEpoch();
    receiptAccessRef.current = true;
    receiptUnavailableRef.current = false;
    setSessionEpoch((current) => current + 1);
    setConnections([]); setAccounts([]); setJobs([]); setDetail(null); setCapabilities(null); setBusy(new Set());
    setAuthLock(true);
    setAuthorization(null); setDeviceAccess(true); setDeviceUnavailable(false);
    setReceiptAccess(true); setReceiptUnavailable(false);
    setLimits(null); setShowLimits(false); setShowConsentHealth(false); setConsents([]); setPending([]);
    setFocusMonth(undefined); setFocusToday(undefined); setFocusCurrency(undefined); setFocusProvisional(undefined); setFocusRule(undefined); setView("accounts");
    setPendingView(null);
    setMoreOpen(false);
    setDevicesFocus(0);
    const target = next ?? (disabled ? "disabled" : "denied");
    setSessionUpgrade(target === "sessionUpgrade");
    setReloggingIn(false);
    setLoad(target);
  }, [bumpReceiptEpoch, setAuthLock]);
  const barrier = useCallback((result: { ok: boolean; status?: number; error?: string }): boolean => {
    if (isSessionUpgradeRequired(result)) {
      if (alive.current) setSessionUpgrade(true);
      return false;
    }
    if (!isAccessLoss(result) && !(!result.ok && result.error === "budget_actor_changed")) return false;
    if (alive.current) invalidate(!result.ok && result.error === "budget_disabled");
    return true;
  }, [invalidate]);

  const reload = useCallback(async () => {
    const token = ++generation.current;
    setSessionUpgrade(false);
    // Each response passes the barrier as soon as it arrives; a slow sibling cannot delay clearing.
    const watch = <T,>(request: Promise<BudgetResult<T>>) => request.then((result) => {
      // A lost-access answer clears the view even when a newer load has started since.
      barrier(result);
      return result;
    });
    const me = await watch(api.capabilities());
    if (!alive.current || token !== generation.current) return;
    if (!me.ok) {
      if (me.status === 404) invalidate(false, "serverChanged");
      else if (isSessionUpgradeRequired(me)) invalidate(false, "sessionUpgrade");
      else {
        setAuthLock(true);
        setLoad("error");
      }
      return;
    }
    if ((principal !== undefined && me.data.user !== principal) || me.data.own_budget?.can_view !== true) {
      invalidate(false);
      return;
    }
    setAuthorization(me.data);
    setAuthLock(false);
    setDeviceAccess(me.data.own_budget.can_manage_devices);
    const nextReceiptAccess = me.data.own_budget.can_use_receipts;
    if (nextReceiptAccess) {
      if (receiptUnavailableRef.current || !receiptAccessRef.current) bumpReceiptEpoch();
      receiptUnavailableRef.current = false;
      setReceiptUnavailable(false);
    }
    receiptAccessRef.current = nextReceiptAccess;
    setReceiptAccess(nextReceiptAccess);
    const [connected, listed, queued] = await Promise.all([
      watch(api.connections()), watch(api.accounts()), watch(api.jobs()),
    ]);
    if (token !== generation.current) return;
    if (!connected.ok || !listed.ok) {
      if (isSessionUpgradeRequired(connected) || isSessionUpgradeRequired(listed)) {
        invalidate(false, "sessionUpgrade");
      } else {
        setLoad("error");
      }
      return;
    }
    setConnections((current) => newer(current, connected.data.connections));
    setCapabilities(connected.data.capabilities);
    setAccounts((current) => newer(current, listed.data.accounts));
    if (queued.ok) setJobs(queued.data.jobs);
    setLoad("ready");
    setRevision((current) => current + 1);
  }, [api, principal, barrier, invalidate, bumpReceiptEpoch]);

  useEffect(() => {
    if (active) void reload();
  }, [active, reload]);

  const refreshAuthorization = useCallback(async (keepUnavailable = false): Promise<boolean> => {
    const result = await api.capabilities();
    if (!alive.current || barrier(result)) return false;
    if (!result.ok) {
      if (result.status === 404) invalidate(false, "serverChanged");
      else if (isSessionUpgradeRequired(result)) invalidate(false, "sessionUpgrade");
      else {
        setAuthLock(true);
      }
      return false;
    }
    if ((principal !== undefined && result.data.user !== principal)
        || result.data.own_budget?.can_view !== true) {
      invalidate(false);
      return false;
    }
    setAuthorization(result.data);
    setAuthLock(false);
    setDeviceAccess(result.data.own_budget.can_manage_devices);
    const nextReceiptAccess = result.data.own_budget.can_use_receipts;
    if (nextReceiptAccess && !keepUnavailable) {
      if (receiptUnavailableRef.current || !receiptAccessRef.current) bumpReceiptEpoch();
      receiptUnavailableRef.current = false;
      setReceiptUnavailable(false);
    }
    receiptAccessRef.current = nextReceiptAccess;
    setReceiptAccess(nextReceiptAccess);
    return true;
  }, [api, principal, barrier, invalidate, bumpReceiptEpoch, setAuthLock]);

  const authorizeView = useCallback((action: () => void) => {
    void refreshAuthorization().then((allowed) => { if (allowed) action(); });
  }, [refreshAuthorization]);

  useEffect(() => {
    if (!active) return undefined;
    const focused = () => { void refreshAuthorization(); };
    const visible = () => { if (document.visibilityState === "visible") focused(); };
    window.addEventListener("focus", focused);
    document.addEventListener("visibilitychange", visible);
    return () => {
      window.removeEventListener("focus", focused);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [active, refreshAuthorization]);

  const deviceBarrier = useCallback((result: { ok: boolean; status?: number; error?: string }, listing = false): boolean => {
    if (!result.ok && (result.status === 403 || result.status === 404) && result.error !== "budget_disabled") {
      if (result.status === 404 || listing) setDeviceUnavailable(true);
      setDeviceAccess(false);
      void refreshAuthorization();
      return true;
    }
    return barrier(result);
  }, [barrier, refreshAuthorization]);
  const receiptBarrier = useCallback((
    result: { ok: boolean; status?: number; error?: string },
    listing = false,
    expectedReceiptEpoch = receiptEpochRef.current,
  ): boolean => {
    if (!result.ok && result.status === 403 && result.error !== "budget_disabled") {
      if (expectedReceiptEpoch !== receiptEpochRef.current) return false;
      if (listing) {
        receiptUnavailableRef.current = true;
        setReceiptUnavailable(true);
      }
      receiptAccessRef.current = false;
      setReceiptAccess(false);
      bumpReceiptEpoch();
      void refreshAuthorization(listing);
      return true;
    }
    return barrier(result);
  }, [barrier, refreshAuthorization, bumpReceiptEpoch]);
  const receiptViewBarrier = useCallback((result: { ok: boolean; status?: number; error?: string }): boolean =>
    isAccessLoss(result) || (!result.ok && result.error === "budget_actor_changed"), []);

  /** Routes every receipt request through master's verified-snapshot gate and parent epoch barrier (never /whoami). */
  const guardedReceiptsApi = useMemo<ReceiptsApi>(() => {
    const runWithGate = async <T,>(
      callFn: () => Promise<BudgetResult<T>>,
      isMutation: boolean,
      listing = false,
    ): Promise<BudgetResult<T>> => {
      const startedEpoch = epoch.current;
      const startedReceiptEpoch = receiptEpochRef.current;
      const expectedPrincipal = principal;
      const samePrincipal = () => alive.current && principalRef.current === expectedPrincipal;
      const activeEpoch = () => samePrincipal() && startedEpoch === epoch.current;
      const currentReceiptPanel = () => activeEpoch() && startedReceiptEpoch === receiptEpochRef.current;
      if (isMutation ? !activeEpoch() : !currentReceiptPanel()) {
        return { ok: false, status: 0, error: "budget_stale_epoch" };
      }
      if (isMutation && (authorizationLockedRef.current || api.isAuthorizationLocked() || (effectiveReceiptsApi.isAuthorizationLocked?.() ?? false))) {
        return { ok: false, status: 503, error: "budget_server_changed" };
      }
      const result = await callFn();
      if (!activeEpoch()) {
        return { ok: false, status: 0, error: "budget_stale_epoch" };
      }
      if (!isMutation && !result.ok && result.status === 403 && result.error !== "budget_disabled"
          && startedReceiptEpoch !== receiptEpochRef.current) {
        return { ok: false, status: 0, error: "budget_stale_epoch" };
      }
      if (samePrincipal()) receiptBarrier(result, listing, startedReceiptEpoch);
      if (!activeEpoch()) {
        return { ok: false, status: 0, error: "budget_stale_epoch" };
      }
      if (!isMutation && startedReceiptEpoch !== receiptEpochRef.current && !(!result.ok && result.status === 403)) {
        return { ok: false, status: 0, error: "budget_stale_epoch" };
      }
      return result;
    };
    const fetchReceiptLinkGroupDetail = effectiveReceiptsApi.fetchReceiptLinkGroupDetail?.bind(effectiveReceiptsApi);
    const receiptGroupCandidates = effectiveReceiptsApi.receiptGroupCandidates?.bind(effectiveReceiptsApi);
    const linkReceiptGroup = effectiveReceiptsApi.linkReceiptGroup?.bind(effectiveReceiptsApi);
    const unlinkReceiptGroup = effectiveReceiptsApi.unlinkReceiptGroup?.bind(effectiveReceiptsApi);
    return {
      receiptsList: (request) => runWithGate(() => effectiveReceiptsApi.receiptsList(request), false, true),
      receiptsSearch: (request) => runWithGate(() => effectiveReceiptsApi.receiptsSearch(request), false, true),
      receiptDetail: (receiptId) => runWithGate(() => effectiveReceiptsApi.receiptDetail(receiptId), false, false),
      ...(fetchReceiptLinkGroupDetail === undefined ? {} : {
        fetchReceiptLinkGroupDetail: (groupId: string) => runWithGate(() => fetchReceiptLinkGroupDetail(groupId), false, false),
      }),
      ...(receiptGroupCandidates === undefined ? {} : {
        receiptGroupCandidates: (receiptId: string) => runWithGate(() => receiptGroupCandidates(receiptId), false),
      }),
      ...(linkReceiptGroup === undefined ? {} : {
        linkReceiptGroup: (request: Parameters<typeof linkReceiptGroup>[0]) => runWithGate(() => linkReceiptGroup(request), true),
      }),
      ...(unlinkReceiptGroup === undefined ? {} : {
        unlinkReceiptGroup: (request: Parameters<typeof unlinkReceiptGroup>[0]) => runWithGate(() => unlinkReceiptGroup(request), true),
      }),
      receiptImage: (receiptId) => runWithGate(() => effectiveReceiptsApi.receiptImage(receiptId), false, false),
      confirmReceipt: (request) => runWithGate(() => effectiveReceiptsApi.confirmReceipt(request), true, false),
      deleteReceipt: (request) => runWithGate(() => effectiveReceiptsApi.deleteReceipt(request), true, false),
      isAuthorizationLocked: () => authorizationLockedRef.current || api.isAuthorizationLocked() || (effectiveReceiptsApi.isAuthorizationLocked?.() ?? false),
    };
  }, [effectiveReceiptsApi, principal, receiptBarrier, api]);
  const canManageDevices = deviceAccess && !deviceUnavailable && authorization?.own_budget?.can_manage_devices === true;

  // Poll only while a job is queued/running and the view is visible; reload stored data once it settles.
  // A failed poll re-arms the timer; an authorization failure goes through reload, which clears data.
  const polling = active && hasActiveJob(jobs);
  useEffect(() => {
    if (!polling) return undefined;
    let cancelled = false;
    const timer = setTimeout(() => {
      void api.jobs().then((result) => {
        // The access barrier runs first: even a poll whose timer was superseded must clear on lost access.
        if (!alive.current || barrier(result) || cancelled) return;
        if (!result.ok) {
          setPollTick((current) => current + 1);
          return;
        }
        setJobs(result.data.jobs);
        if (!hasActiveJob(result.data.jobs)) void reload();
      });
    }, pollMs);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [polling, jobs, pollTick, api, pollMs, reload, barrier]);

  /** Runs one mutation; null when the view was unmounted or its data cleared while it was in flight. */
  const run = useCallback(async <T,>(key: string, action: () => Promise<BudgetResult<T>>): Promise<BudgetResult<T> | null> => {
    if (authorizationLocked) {
      setNotice(t("budget.main.serverChanged"));
      return { ok: false, status: 503, error: "budget_server_changed" };
    }
    const started = epoch.current;
    setBusy((current) => new Set(current).add(key));
    setNotice(null);
    const result = await action();
    if (!alive.current || started !== epoch.current || barrier(result)) return null;
    setBusy((current) => { const next = new Set(current); next.delete(key); return next; });
    if (!result.ok) {
      if (!isSessionUpgradeRequired(result)) setNotice(failureText(result.error));
      if (result.error === "budget_version_conflict") await reload();
    }
    return result;
  }, [reload, barrier, authorizationLocked]);

  const refresh = useCallback(async (connection: BudgetConnection) => {
    // Reuse the key only to retry a request whose outcome is unknown (network failure).
    const key = retryKeys.current.get(connection.id) ?? idempotencyKey();
    retryKeys.current.set(connection.id, key);
    const result = await run(`refresh:${connection.id}`, () => api.refresh(connection, {}, key));
    if (result === null) return;
    // Keep the key while the outcome is unknown (network loss, unreadable 2xx, 5xx); drop it only after a
    // job was returned or the server definitively refused without creating one.
    const definitive = result.ok || (result.status >= 400 && result.status < 500)
      || result.error === "budget_refresh_unavailable";
    if (definitive) retryKeys.current.delete(connection.id);
    if (result.ok) {
      const job = result.data.job;
      setJobs((current) => [job, ...current.filter((item) => item.id !== job.id)]);
    }
  }, [api, run]);

  // A saved entity replaces the shown one only when it is not older than what a later load already shows.
  const saveConnection = useCallback(async (key: string, action: () => Promise<BudgetResult<{ connection: BudgetConnection }>>) => {
    const result = await run(key, action);
    if (result?.ok !== true) return;
    const saved = result.data.connection;
    setConnections((current) => current.map((item) =>
      (item.id === saved.id && saved.control_version >= item.control_version ? saved : item)));
    if (key.startsWith("pause:")) {
      // Pause changes every account's `active` flag; re-read accounts within the same data epoch.
      const started = epoch.current;
      const listed = await api.accounts();
      if (!alive.current || started !== epoch.current || barrier(listed)) return;
      if (listed.ok) setAccounts((current) => newer(current, listed.data.accounts));
    }
  }, [api, run, barrier]);

  const saveAccount = useCallback(async (key: string, action: () => Promise<BudgetResult<{ account: BudgetAccount }>>) => {
    const result = await run(key, action);
    if (result?.ok !== true) return;
    const saved = result.data.account;
    setAccounts((current) => current.map((item) =>
      (item.id === saved.id && saved.control_version >= item.control_version ? { ...item, ...saved } : item)));
  }, [run]);

  // Consent state is read after every completed reload while the consent feature is available.
  const consentEnabled = capabilities?.consent === true;
  useEffect(() => {
    if (!active || !consentEnabled) return undefined;
    let cancelled = false;
    const started = epoch.current;
    // Each read passes the access barrier as soon as it arrives; a hanging sibling cannot delay clearing.
    const watch = <T,>(request: Promise<BudgetResult<T>>) => request.then((result) => {
      if (alive.current && started === epoch.current && (!cancelled || !isSessionUpgradeRequired(result))) {
        barrier(result);
      }
      return result;
    });
    void Promise.all([watch(api.consentStatus()), watch(api.consentPending())]).then(([status, open]) => {
      if (!alive.current || started !== epoch.current || isAccessLoss(status) || isAccessLoss(open) || cancelled) return;
      if (status.ok) setConsents(status.data.consents);
      if (open.ok) setPending(open.data.pending.filter((flow) => OPEN_FLOW.has(flow.status)));
    });
    return () => { cancelled = true; };
  }, [active, consentEnabled, revision, api, barrier]);

  const startConsent = useCallback(async (connection: BudgetConnection) => {
    setNotice(null);
    const ticket = lifecycle.current;
    const result = await run(`consent:${connection.id}`, () => api.consentStart(connection, consentIntent(connection)));
    // Leave for the bank only if this is still the same visible budget for the same principal.
    if (result === null || !activeRef.current || lifecycle.current !== ticket) return;
    if (!result.ok) {
      if (!isSessionUpgradeRequired(result)) setNotice(consentFailureText(result.error));
      // An unfinished flow already exists: reload so its banner (finish / cancel) appears.
      if (result.error === "budget_consent_in_progress") await reload();
      return;
    }
    const target = safeAuthorizationUrl(result.data.authorization_url);
    if (target === null) { setNotice(t("budget.main.consentUrl")); return; }
    // Keep the button disabled while the browser leaves for the bank.
    setBusy((current) => new Set(current).add(`consent:${connection.id}`));
    navigate(target);
  }, [api, run, navigate, reload]);

  const cancelConsent = useCallback(async (flow: BudgetConsentFlow) => {
    const result = await run(`cancel:${flow.id}`, () => api.consentCancel(flow.id));
    if (result?.ok === true) setPending((current) => current.filter((item) => item.id !== flow.id));
  }, [api, run]);

  // Limits are read on demand, and again after every completed reload while shown.
  useEffect(() => {
    if (!active || !showLimits) return undefined;
    let cancelled = false;
    const started = epoch.current;
    void api.limits().then((result) => {
      // Barrier before the local discard: hiding the panel must not swallow a lost-access answer.
      if (!alive.current || started !== epoch.current || barrier(result) || cancelled) return;
      setLimits(result.ok ? result.data.limits : "error");
    });
    return () => { cancelled = true; };
  }, [active, showLimits, revision, api, barrier]);

  const relogin = useCallback(() => {
    setReloggingIn(true);
    void logout().then(onLogout);
  }, [onLogout]);

  const openAuthorizedView = useCallback((nextView: View, beforeShow?: () => void) => {
    const navToken = ++navGeneration.current;
    const startEpoch = epoch.current;
    setMoreOpen(false);
    setPendingView(nextView);
    void refreshAuthorization().then((allowed) => {
      if (!alive.current || startEpoch !== epoch.current || navToken !== navGeneration.current) return;
      setPendingView(null);
      if (allowed) {
        beforeShow?.();
        setView(nextView);
      }
    });
  }, [refreshAuthorization]);

  const selectNavView = useCallback((item: View) => {
    openAuthorizedView(item, () => {
      if (item === "month") {
        setFocusMonth(undefined);
        setFocusToday(undefined);
        setFocusCurrency(undefined);
        setFocusProvisional(undefined);
      }
      if (item === "rules") setFocusRule(undefined);
    });
  }, [openAuthorizedView]);

  useEffect(() => {
    if (!active || load !== "ready" || !hasAnalytics(capabilities)) {
      setMoreOpen(false);
    }
  }, [active, load, capabilities]);

  if (!active) return null;
  const shownLoad: Load = principal === committedPrincipal ? load : "loading";
  if (shownLoad === "serverChanged") {
    return <section className="budget-app"><p className="budget-notice" role="alert">{t("budget.main.serverChanged")}</p></section>;
  }
  const analysis = shownLoad === "ready" && hasAnalytics(capabilities);
  const receiptsFlag = capabilities === null
    ? undefined
    : Object.entries(capabilities).find(([key]) => key === "receipts")?.[1];
  const receiptsAllowed = shownLoad === "ready"
    && capabilities !== null
    && capabilities.stored_reads
    && receiptsFlag !== false
    && receiptAccess
    && !receiptUnavailable
    && authorization?.own_budget?.can_use_receipts === true;
  const receiptPermissions = authorization?.own_budget?.permissions;
  const transactionReceiptsList = receiptsAllowed
    && receiptPermissions?.includes("budget.receipts.view") === true
    && receiptPermissions.includes("budget.receipts.match")
    ? guardedReceiptsApi.receiptsList : undefined;
  // Analytics views are available only while the server offers analytics; receipts are gated independently.
  const shownView: View = view === "receipts"
    ? (receiptsAllowed ? "receipts" : "accounts")
    : (!analysis ? "accounts" : view);
  const pendingNavView: View | null = pendingView === null
    ? null
    : (pendingView === "receipts"
      ? (receiptsAllowed ? "receipts" : "accounts")
      : (!analysis ? "accounts" : pendingView));
  const pendingTarget: View | null = pendingNavView !== null && pendingNavView !== shownView ? pendingNavView : null;
  const header = (
    <header className="budget-header">
      <div><h2>{t("budget.main.title")}</h2><p>{t("budget.main.subtitle")}</p></div>
      <div className="budget-actions">
        {shownLoad === "ready" && consentEnabled ? (
          <button type="button" aria-pressed={showConsentHealth}
            onClick={() => { authorizeView(() => { setShowConsentHealth((current) => !current); }); }}>
            {showConsentHealth ? t("budget.main.consentHealthHide") : t("budget.main.consentHealth")}
          </button>
        ) : null}
        {shownLoad === "ready" && canManageDevices ? (
          <button type="button" onClick={() => { openAuthorizedView("accounts", () => { setDevicesFocus((current) => current + 1); }); }}>
            {t("budget.main.devices")}
          </button>
        ) : null}
        {shownLoad === "ready" && !analysis && receiptsAllowed ? (
          <button type="button" aria-pressed={shownView === "receipts"}
            aria-busy={pendingTarget === "receipts" ? "true" : undefined}
            onClick={() => { openAuthorizedView(shownView === "receipts" ? "accounts" : "receipts"); }}>
            {t("budget.main.views.receipts")}
          </button>
        ) : null}
        {shownLoad === "ready" ? (
          <button type="button" aria-pressed={showLimits}
            onClick={() => { authorizeView(() => { setShowLimits((current) => !current); }); }}>
            {showLimits ? t("budget.main.limitsHide") : t("budget.main.limits")}
          </button>
        ) : null}
        <button type="button" onClick={() => { void reload(); }}>{t("budget.main.reload")}</button>
      </div>
    </header>
  );
  const showSessionUpgrade = principal === committedPrincipal && (sessionUpgrade || shownLoad === "sessionUpgrade");
  const upgradeBanner = showSessionUpgrade ? (
    <div className="budget-notice budget-session-upgrade" role="alert">
      <span>{t("budget.main.sessionUpgrade")}</span>
      <button type="button" className="budget-primary" disabled={reloggingIn} onClick={relogin}>
        {t("budget.main.relogin")}
      </button>
    </div>
  ) : null;
  if (shownLoad === "sessionUpgrade") {
    return <section className="budget-app">{header}{upgradeBanner}</section>;
  }
  if (shownLoad !== "ready") {
    const message = { loading: t("budget.main.loading"), disabled: t("budget.main.disabled"), denied: t("budget.main.denied"), error: t("budget.main.error") }[shownLoad];
    return <section className="budget-app">{header}<p className="budget-status" role="status">{message}</p></section>;
  }
  const opened = accounts.find((account) => account.id === detail) ?? null;
  return (
    <section className="budget-app">
      {header}
      {upgradeBanner}
      {notice === null ? null : <p className="budget-notice" role="alert">{notice}</p>}
      {showLimits ? <p className="budget-muted">{t("budget.main.limitsIntro")}</p> : null}
      {showConsentHealth && consentEnabled ? (
        <ConsentHealthPanel api={api} connections={connections} locale={locale}
          revision={revision} onAccessLoss={barrier} />
      ) : null}
      {pending.length === 0 ? null : (
        <section className="budget-pending" aria-label={t("budget.main.pendingTitle")}>
          <h3>{t("budget.main.pendingTitle")}</h3>
          <ul>
            {pending.map((flow) => (
              <li key={flow.id}>
                <span>{connections.find((item) => item.id === flow.connection_id)?.bank_name ?? t("budget.main.account")}</span>
                {READY_FLOW.has(flow.status)
                  ? <a className="budget-primary" href={completeUrl}>{t("budget.main.finish")}</a>
                  : <span className="budget-muted">{t("budget.main.atBank")}</span>}
                <button type="button" disabled={busy.has(`cancel:${flow.id}`)}
                  onClick={() => { void cancelConsent(flow); }}>{t("budget.main.cancel")}</button>
              </li>
            ))}
          </ul>
        </section>
      )}
      {analysis ? (
        <BudgetNavigation shownView={shownView} pendingView={pendingTarget} receiptsAllowed={receiptsAllowed}
          moreOpen={moreOpen} onOpenChange={setMoreOpen} onSelectView={selectNavView} />
      ) : null}
      {shownView === "monthly_budget" ? (
        <MonthBudgetView api={api} accounts={accounts} locale={locale} revision={revision}
          onAccessLoss={barrier} onOpenMonth={(month, serverCurrentMonth, options) => {
            openAuthorizedView("month", () => {
              setFocusMonth(month);
              setFocusToday(() => serverMonthToToday(serverCurrentMonth));
              setFocusCurrency(options?.currency);
              setFocusProvisional(options?.provisionalKeys === undefined ? undefined : {
                keys: options.provisionalKeys,
                total: options.provisionalTotal ?? options.provisionalKeys.length,
                truncated: options.provisionalTruncated === true,
              });
            });
          }} />
      ) : shownView === "inbox" ? (
        <InboxView api={api} connections={connections} consentEnabled={consentEnabled} locale={locale}
          revision={revision} onAccessLoss={barrier}
          onOpenAccounts={(openConsentHealth) => {
            openAuthorizedView("accounts", () => {
              if (openConsentHealth && consentEnabled) setShowConsentHealth(true);
            });
          }}
          onOpenMortgages={() => { openAuthorizedView("mortgages"); }}
          onOpenLimits={() => { openAuthorizedView("limits"); }} />
      ) : shownView === "month" ? (
        <MonthView key={`${focusMonth ?? "current"}-${focusCurrency ?? "all"}-${focusProvisional === undefined ? "full" : `provisional:${String(focusProvisional.keys.length)}:${focusProvisional.truncated ? "1" : "0"}`}`}
          api={api} receiptsList={transactionReceiptsList} accounts={accounts} connections={connections} locale={locale} revision={revision}
          onAccessLoss={barrier} initialMonth={focusMonth} initialCurrency={focusCurrency}
          initialProvisionalKeys={focusProvisional?.keys}
          initialProvisionalTotal={focusProvisional?.total}
          initialProvisionalTruncated={focusProvisional?.truncated}
          {...(focusToday === undefined ? {} : { today: focusToday })}
          onCreateRule={(candidate) => { openAuthorizedView("rules", () => { setFocusRule(candidate); }); }} />
      ) : shownView === "year" ? (
        <YearView api={api} accounts={accounts} connections={connections} locale={locale} revision={revision} onAccessLoss={barrier}
          onOpenMonth={(month) => {
            openAuthorizedView("month", () => {
              setFocusMonth(month); setFocusToday(undefined); setFocusCurrency(undefined); setFocusProvisional(undefined);
            });
          }} />
      ) : shownView === "limits" ? (
        <SpendingLimitsView api={api} accounts={accounts} locale={locale} revision={revision}
          onAccessLoss={barrier} onOpenRules={() => { openAuthorizedView("rules"); }} />
      ) : shownView === "recurring" ? (
        <RecurringView api={api} accounts={accounts} connections={connections} locale={locale} revision={revision}
          onAccessLoss={barrier} />
      ) : shownView === "mortgages" ? (
        <MortgagesView api={api} accounts={accounts} locale={locale} revision={revision}
          onAccessLoss={barrier} />
      ) : shownView === "forecast" ? (
        <ForecastView api={api} accounts={accounts} connections={connections} locale={locale} revision={revision}
          onAccessLoss={barrier} onOpenMortgages={() => { openAuthorizedView("mortgages"); }} />
      ) : shownView === "quarantine" ? (
        <QuarantineView api={api} accounts={accounts} connections={connections} revision={revision}
          canDismiss={authorization?.own_budget?.permissions.includes("budget.edit") === true}
          onAccessLoss={barrier} />
      ) : shownView === "rules" ? (
        <CategoriesView api={api} accounts={accounts} connections={connections} locale={locale} revision={revision}
          onAccessLoss={barrier} initialRule={focusRule ?? null} />
      ) : shownView === "receipts" ? (
        <ReceiptsView key={`${principal ?? ""}:${String(sessionEpoch)}:${String(receiptEpoch)}`} receiptsApi={guardedReceiptsApi}
          pendingStore={pendingReceiptActions} actorKey={principal ?? ""} accounts={accounts}
          connections={connections} locale={locale} revision={revision} onAccessLoss={receiptViewBarrier} />
      ) : (
        <>
        <LoginQr key={`login-qr:${principal ?? ""}`} api={api} onAccessLoss={barrier} />
        {canManageDevices ? <ReceiptDevices key={principal ?? ""} api={api} locale={locale} revision={revision}
          onAccessLoss={deviceBarrier} focusRevision={devicesFocus} /> : null}
        {connections.length === 0 ? <p className="budget-status">{t("budget.main.empty")}</p> : null}
        {connections.map((connection) => {
          const job = latestJob(jobs, connection.id);
          const owned = accounts.filter((account) => account.connection === connection.id);
          const refreshing = busy.has(`refresh:${connection.id}`) || (job !== null && ACTIVE_JOB.has(job.status));
          return (
            <article className="budget-connection" key={connection.id} aria-label={connection.bank_name}>
              <header>
                <h3>{connection.bank_name} <small>{connection.country}</small></h3>
                <div className="budget-actions">
                  {capabilities?.refresh === true ? (
                    <button type="button" className="budget-primary" disabled={refreshing || connection.paused}
                      onClick={() => { void refresh(connection); }}>{refreshing ? t("budget.main.refreshing") : t("budget.main.refresh")}</button>
                  ) : <span className="budget-muted">{t("budget.main.refreshOff")}</span>}
                  <button type="button" disabled={busy.has(`pause:${connection.id}`)}
                    onClick={() => { void saveConnection(`pause:${connection.id}`, () => api.setPaused(connection, !connection.paused)); }}>
                    {connection.paused ? t("budget.main.resume") : t("budget.main.pause")}
                  </button>
                </div>
              </header>
              <ul className="budget-facts">
                {connection.paused ? <li className="budget-warn">{t("budget.main.paused")}</li> : null}
                {connection.attention === null ? null : <li className="budget-warn">{t("budget.main.attention")}</li>}
                {connection.renewal_due ? <li className="budget-warn">{t("budget.main.renewal")}</li> : null}
                {connection.valid_until === null ? null : <li>{t("budget.main.validUntil", { date: when(connection.valid_until, locale) ?? "" })}</li>}
                <li>{t("budget.main.lastSuccess", { when: when(connection.last_success, locale) ?? t("budget.main.never") })}</li>
                {job === null ? null : <li>{jobText(job, locale)}</li>}
                {consentEnabled ? consentFacts(consents, connection.id, locale) : null}
              </ul>
              {consentEnabled ? (
                <div className="budget-consent">
                  <button type="button" disabled={busy.has(`consent:${connection.id}`)}
                    onClick={() => { void startConsent(connection); }}>
                    {busy.has(`consent:${connection.id}`) ? t("budget.main.connecting")
                      : consentIntent(connection) === "renew" ? t("budget.main.renew") : t("budget.main.connect")}
                  </button>
                  <span className="budget-muted">{t("budget.main.consentHelp")}</span>
                </div>
              ) : null}
              {capabilities?.background_selection === true ? (
                <label className="budget-toggle">
                  <input name="background_enabled" type="checkbox" checked={connection.background_enabled} disabled={busy.has(`background:${connection.id}`)}
                    onChange={(event) => { const enabled = event.currentTarget.checked; void saveConnection(`background:${connection.id}`, () => api.setBackground(connection, enabled)); }} />
                  {t("budget.main.background")}
                  {capabilities.background_polling ? null : <span className="budget-muted"> ({t("budget.main.backgroundOff")})</span>}
                </label>
              ) : null}
              <ul className="budget-accounts">
                {owned.map((account, index) => (
                  <AccountRow key={account.id} account={account} index={index} busy={busy}
                    onOpen={() => { authorizeView(() => { setDetail(account.id); }); }}
                    onSelect={(selected) => { void saveAccount(`select:${account.id}`, () => api.setSelected(account, selected)); }}
                    onKind={(kind) => { void saveAccount(`kind:${account.id}`, () => api.setKind(account, kind)); }}
                    onCreditLimit={(limit) => { void saveAccount(`credit-limit:${account.id}`, () => api.setCreditLimit(account, limit)); }} />
                ))}
              </ul>
              {showLimits ? (
                <LimitsPanel connectionId={connection.id} accounts={owned} limits={limits} locale={locale} />
              ) : null}
            </article>
          );
        })}
        {opened === null ? null : (
          <AccountDetail account={opened} label={accountLabel(opened, accounts.filter((a) => a.connection === opened.connection).indexOf(opened), t("budget.main.account"))}
            api={api} locale={locale} revision={revision} onAccessLoss={barrier}
              onClose={() => { setDetail(null); }} />
          )}
        </>
      )}
    </section>
  );
}

interface BudgetNavigationProps {
  shownView: View;
  pendingView: View | null;
  receiptsAllowed: boolean;
  moreOpen: boolean;
  onOpenChange: (next: boolean) => void;
  onSelectView: (view: View) => void;
}

/** Everyday navigation keeping four primary views visible and placing secondary tools behind More. */
function BudgetNavigation({
  shownView, pendingView, receiptsAllowed, moreOpen, onOpenChange, onSelectView,
}: BudgetNavigationProps) {
  const navRef = useRef<HTMLElement>(null);
  const moreTriggerRef = useRef<HTMLButtonElement>(null);
  const morePanelRef = useRef<HTMLDivElement>(null);
  const focusFirstMoreRef = useRef(false);
  const morePanelId = useId();
  const secondaryActive = SECONDARY_VIEW_SET.has(shownView);
  const pendingSecondary = pendingView !== null && SECONDARY_VIEW_SET.has(pendingView) ? pendingView : null;
  const primaryViews: readonly View[] = receiptsAllowed ? [...PRIMARY_VIEWS, "receipts"] : PRIMARY_VIEWS;

  const closeMore = useCallback((restoreFocus: boolean) => {
    focusFirstMoreRef.current = false;
    onOpenChange(false);
    if (restoreFocus) moreTriggerRef.current?.focus();
  }, [onOpenChange]);

  useEffect(() => {
    if (!moreOpen || !focusFirstMoreRef.current) return;
    focusFirstMoreRef.current = false;
    morePanelRef.current?.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus();
  }, [moreOpen]);

  useEffect(() => {
    if (!moreOpen) return undefined;
    const onDocumentClick = (event: MouseEvent) => {
      const target = event.target;
      if (target instanceof Node && !navRef.current?.contains(target)) {
        closeMore(false);
      }
    };
    const onDocumentKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape" && !event.defaultPrevented) {
        const activeEl = document.activeElement;
        const inside = activeEl !== null && (navRef.current?.contains(activeEl) ?? false);
        event.preventDefault();
        closeMore(inside);
      }
    };
    document.addEventListener("click", onDocumentClick);
    document.addEventListener("keydown", onDocumentKeyDown);
    return () => {
      document.removeEventListener("click", onDocumentClick);
      document.removeEventListener("keydown", onDocumentKeyDown);
    };
  }, [moreOpen, closeMore]);

  const handleNavKeyDown = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (event.key === "Escape" && moreOpen) {
      event.preventDefault();
      event.stopPropagation();
      closeMore(true);
    }
  };

  const handleMorePanelKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    const panel = morePanelRef.current;
    if (panel === null) return;
    const items = [...panel.querySelectorAll<HTMLButtonElement>("button:not(:disabled)")];
    if (items.length === 0) return;
    const activeEl = document.activeElement;
    const currentIndex = activeEl instanceof HTMLButtonElement ? items.indexOf(activeEl) : -1;
    const rtl = getComputedStyle(panel).direction === "rtl";
    const nextArrow = rtl ? "ArrowLeft" : "ArrowRight";
    const prevArrow = rtl ? "ArrowRight" : "ArrowLeft";
    let nextIndex: number | null = null;
    if (event.key === "ArrowDown" || event.key === nextArrow) {
      nextIndex = currentIndex < 0 ? 0 : (currentIndex + 1) % items.length;
    } else if (event.key === "ArrowUp" || event.key === prevArrow) {
      nextIndex = currentIndex <= 0 ? items.length - 1 : currentIndex - 1;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = items.length - 1;
    }
    if (nextIndex !== null) {
      event.preventDefault();
      items[nextIndex]?.focus();
    }
  };

  return (
    <nav ref={navRef} className="budget-views budget-nav" aria-label={t("budget.main.viewsLabel")}
      onKeyDown={handleNavKeyDown}>
      <div className="budget-nav-bar">
        {primaryViews.map((item) => (
          <button type="button" key={item} aria-pressed={shownView === item}
            aria-busy={pendingView === item ? "true" : undefined}
            onClick={() => { onSelectView(item); }}>
            {labelOf(MAIN_VIEW_KEYS, item)}
          </button>
        ))}
        <button ref={moreTriggerRef} type="button" className="budget-nav-more-toggle"
          aria-label={t("budget.main.moreOptions")}
          aria-expanded={moreOpen} aria-controls={morePanelId}
          data-secondary-active={secondaryActive ? "true" : undefined}
          onClick={() => {
            focusFirstMoreRef.current = false;
            onOpenChange(!moreOpen);
          }}
          onKeyDown={(event) => {
            if (event.key === "ArrowDown" && !moreOpen) {
              event.preventDefault();
              focusFirstMoreRef.current = true;
              onOpenChange(true);
            }
          }}>
          {t("budget.main.moreOptions")}
        </button>
        {!moreOpen && secondaryActive ? (
          <button type="button" className="budget-nav-current" aria-pressed="true"
            onClick={() => { onSelectView(shownView); }}>
            {labelOf(MAIN_VIEW_KEYS, shownView)}
          </button>
        ) : null}
        {!moreOpen && pendingSecondary !== null ? (
          <button type="button" className="budget-nav-pending" aria-pressed="false" aria-busy="true"
            onClick={() => { onSelectView(pendingSecondary); }}>
            {labelOf(MAIN_VIEW_KEYS, pendingSecondary)}
          </button>
        ) : null}
      </div>
      {moreOpen ? (
        <div ref={morePanelRef} id={morePanelId} className="budget-nav-more" role="group"
          aria-label={t("budget.main.moreOptions")} onKeyDown={handleMorePanelKeyDown}>
          {SECONDARY_VIEWS.map((item) => (
            <button type="button" key={item} aria-pressed={shownView === item}
              aria-busy={pendingView === item ? "true" : undefined}
              onClick={(event) => {
                const activeEl = document.activeElement;
                const keyboardFromPanel = event.detail === 0
                  && activeEl !== null
                  && (morePanelRef.current?.contains(activeEl) ?? false);
                onSelectView(item);
                if (keyboardFromPanel) moreTriggerRef.current?.focus();
              }}>
              {labelOf(MAIN_VIEW_KEYS, item)}
            </button>
          ))}
        </div>
      ) : null}
    </nav>
  );
}

function consentFailureText(error: string): string {
  switch (error) {
    case "budget_version_conflict": return t("budget.main.conflict");
    case "budget_consent_in_progress": return t("budget.main.consentInProgress");
    case "budget_consent_rate_limited": return t("budget.main.consentRateLimited");
    case "budget_consent_psu_context_required": return t("budget.main.consentContext");
    default: return t("budget.main.consentError");
  }
}

function consentFacts(consents: readonly BudgetConsent[], connectionId: string, locale: string) {
  const own = consents.filter((consent) => consent.connection_id === connectionId);
  const active = own.find((consent) => consent.status === "ACTIVE");
  const waiting = own.some((consent) => consent.status === "UNADOPTED");
  return (
    <>
      {active === undefined ? null : <li>{t("budget.main.consentActive", { when: when(active.valid_until, locale) ?? "—" })}</li>}
      {waiting ? <li className="budget-warn">{t("budget.main.consentWaiting")}</li> : null}
    </>
  );
}

function stateText(state: LimitState, locale: string): string {
  switch (state.kind) {
    case "suspended": return t("budget.main.limitSuspended");
    case "running": return t("budget.main.limitRunning");
    case "waiting": {
      const until = when(state.until, locale) ?? "";
      return state.retry ? t("budget.main.limitRetry", { when: until }) : t("budget.main.limitFrom", { when: until });
    }
    default: return t("budget.main.limitReady");
  }
}

interface LimitsProps {
  connectionId: string; accounts: BudgetAccount[]; limits: BudgetLimit[] | null | "error"; locale: string;
}

/** Read-only view of stored limits for one connection; there is deliberately no way to lift any of them here. */
function LimitsPanel({ connectionId, accounts, limits, locale }: LimitsProps) {
  if (limits === null) return <p className="budget-muted" role="status">{t("budget.main.limitsLoading")}</p>;
  if (limits === "error") return <p className="budget-notice" role="alert">{t("budget.main.limitsError")}</p>;
  const now = Date.now() / 1000;
  const own = limits.filter((limit) => limit.connection === connectionId);
  const shared = own.filter((limit): limit is BudgetSharedLimit => !isComponentLimit(limit));
  const blocked = shared.map((limit) => [limit, limitState(limit, now)] as const).filter(([, state]) => state.kind !== "ready");
  const component = (accountId: string, operation: "transactions" | "balances") =>
    own.find((limit) => isComponentLimit(limit) && limit.account === accountId && limit.operation === operation);
  return (
    <section className="budget-limits" aria-label={t("budget.main.limits")}>
      <ul className="budget-facts">
        {blocked.length === 0 ? <li>{t("budget.main.sharedFree")}</li> : blocked.map(([limit, state]) => (
          <li key={limit.scope} className="budget-warn">
            {t("budget.main.scopeWithState", { scope: labelOf(MAIN_SCOPE_KEYS, limit.scope), state: stateText(state, locale) })}
          </li>
        ))}
      </ul>
      <table className="budget-limits-table">
        <thead><tr><th scope="col">{t("budget.main.account")}</th><th scope="col">{t("budget.main.transactions")}</th><th scope="col">{t("budget.main.balances")}</th></tr></thead>
        <tbody>
          {accounts.map((account, index) => (
            <tr key={account.id} className={account.selected ? undefined : "is-excluded"}>
              <th scope="row">
                {accountLabel(account, index, t("budget.main.account"))}
                {account.selected ? null : <small> ({t("budget.main.excluded")})</small>}
              </th>
              {(["transactions", "balances"] as const).map((operation) => {
                const limit = component(account.id, operation);
                if (limit === undefined || !isComponentLimit(limit)) return <td key={operation}>—</td>;
                const state = effectiveState(limit, shared, now);
                const last = when(limit.last_attempt, locale);
                return (
                  <td key={operation} data-state={state.kind}>
                    {stateText(state, locale)}
                    {last === null ? null : <small className="budget-muted"><br />{t("budget.main.lastTry", { when: last })}</small>}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

interface RowProps {
  account: BudgetAccount; index: number; busy: ReadonlySet<string>;
  onOpen: () => void; onSelect: (selected: boolean) => void; onKind: (kind: BudgetKind) => void;
  onCreditLimit: (limit: BudgetCreditLimitValue | null) => void;
}

function AccountRow({ account, index, busy, onOpen, onSelect, onKind, onCreditLimit }: RowProps) {
  const kind = account.kind_confirmed_at === null ? "" : account.kind;
  const serverLimit = account.credit_limit?.amount ?? "";
  const serverCurrency = defaultCreditLimitCurrency(account);
  const [limitDraft, setLimitDraft] = useState(serverLimit);
  const [currencyDraft, setCurrencyDraft] = useState(serverCurrency);
  const [limitError, setLimitError] = useState(false);
  useEffect(() => {
    setLimitDraft(serverLimit);
    setCurrencyDraft(serverCurrency);
    setLimitError(false);
  }, [serverLimit, serverCurrency]);
  const state = (name: "transactions" | "balances") => labelOf(MAIN_FRESHNESS_KEYS, freshness(account.components[name]));
  const isConfirmedCard = account.kind_confirmed_at !== null && account.kind === "credit_card";
  const limitBusy = busy.has(`credit-limit:${account.id}`);

  function submitCreditLimit() {
    const trimmed = limitDraft.trim();
    if (trimmed === "") {
      if (account.credit_limit === null || account.credit_limit === undefined) {
        setLimitError(true);
        return;
      }
      setLimitError(false);
      onCreditLimit(null);
      return;
    }
    const parsed = normalizeCreditLimitInput(trimmed);
    const parsedCurrency = normalizeCreditCurrencyInput(currencyDraft);
    if (parsed === null || parsedCurrency === null) {
      setLimitError(true);
      return;
    }
    setLimitError(false);
    onCreditLimit({ amount: parsed, currency: parsedCurrency });
  }

  return (
    <li className={account.selected ? "budget-account" : "budget-account is-excluded"}>
      <button type="button" className="budget-account-name" onClick={onOpen}>
        {accountLabel(account, index, t("budget.main.account"))}
        {account.display?.currency ? <small> {account.display.currency}</small> : null}
      </button>
      {otherObservedCurrencies(account).length === 0 ? null
        : <span className="budget-muted">{t("budget.main.observed", { currencies: otherObservedCurrencies(account).join(", ") })}</span>}
      <span className="budget-muted">{t("budget.main.componentFreshness", { transactions: state("transactions"), balances: state("balances") })}</span>
      <label className="budget-toggle">
        <input name="selected" type="checkbox" checked={account.selected} disabled={!account.configured || busy.has(`select:${account.id}`)}
          onChange={(event) => { onSelect(event.currentTarget.checked); }} />
        {t("budget.main.selected")}
      </label>
      <label className="budget-kind">
        <span>{t("budget.main.kind")}</span>
        <select name="account_kind" value={kind} disabled={busy.has(`kind:${account.id}`)}
          onChange={(event) => { if (event.currentTarget.value !== "") onKind(event.currentTarget.value as BudgetKind); }}>
          <option value="" disabled>{t("budget.main.kindUnknown")}</option>
          {KINDS.map((item) => <option key={item} value={item}>{labelOf(MAIN_KIND_KEYS, item)}</option>)}
        </select>
      </label>
      {isConfirmedCard ? (
        <div className="budget-account-limit">
          <label className="budget-kind">
            <span>{t("budget.main.creditLimit")}</span>
            <input name="credit_limit" type="text" inputMode="decimal" value={limitDraft} disabled={limitBusy}
              onChange={(event) => { setLimitDraft(event.currentTarget.value); setLimitError(false); }} />
          </label>
          <label className="budget-kind">
            <span>{t("budget.limits.currency")}</span>
            <input name="credit_limit_currency" type="text" maxLength={3} value={currencyDraft} disabled={limitBusy}
              onChange={(event) => { setCurrencyDraft(event.currentTarget.value); setLimitError(false); }} />
          </label>
          <button type="button" disabled={limitBusy} onClick={submitCreditLimit}>{t("budget.main.creditLimitSave")}</button>
          {account.credit_limit ? (
            <button type="button" disabled={limitBusy} onClick={() => { setLimitError(false); onCreditLimit(null); }}>
              {t("budget.main.creditLimitClear")}
            </button>
          ) : null}
          {limitError ? <span className="budget-warn" role="alert">{t("budget.main.creditLimitInvalid")}</span> : null}
        </div>
      ) : null}
    </li>
  );
}

interface DetailProps {
  account: BudgetAccount; label: string; api: BudgetApi; locale: string; revision: number;
  /** Reports a lost principal/disabled response so the whole view clears, not just this panel. */
  onAccessLoss: (result: { ok: boolean; status?: number; error?: string }) => boolean;
  onClose: () => void;
}

interface Ledger { rows: BudgetTransaction[]; cursor: string | null; uncertain: number; failed: boolean; loading: boolean }

const EMPTY_LEDGER: Ledger = { rows: [], cursor: null, uncertain: 0, failed: false, loading: true };

function AccountDetail({ account, label, api, locale, revision, onAccessLoss, onClose }: DetailProps) {
  const panel = useRef<HTMLElement>(null);
  useEffect(() => {
    // The detail renders below every connection; bring it into view when another account is opened.
    panel.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [account.id]);
  const [balances, setBalances] = useState<BudgetBalances | null>(null);
  const [ledger, setLedger] = useState<Ledger>(EMPTY_LEDGER);
  const [range, setRange] = useState<DateRange>({ from: "", to: "" });
  const [draft, setDraft] = useState<DateRange>({ from: "", to: "" });
  // Every account/filter change starts a new generation; a late response from an older one is dropped,
  // so a previous account's or filter's rows can never replace the current ones.
  const generation = useRef(0);

  // A completed reload (for example after a refresh job settles) re-reads the open account as well.
  useEffect(() => {
    const token = ++generation.current;
    setLedger(EMPTY_LEDGER);
    setBalances(null);
    void api.balances(account.id).then((result) => {
      if (onAccessLoss(result) || token !== generation.current) return;
      setBalances(result.ok ? result.data : null);
    });
    void api.transactions(account.id, { dateFrom: range.from, dateTo: range.to }).then((result) => {
      if (onAccessLoss(result) || token !== generation.current) return;
      setLedger(result.ok
        ? { rows: result.data.transactions, cursor: result.data.next_cursor, uncertain: result.data.uncertain_total,
            failed: false, loading: false }
        : { ...EMPTY_LEDGER, failed: true, loading: false });
    });
  }, [api, account.id, range, revision, onAccessLoss]);
  useEffect(() => () => { generation.current += 1; }, []);

  const more = useCallback(async () => {
    const token = generation.current;
    const cursor = ledger.cursor;
    if (cursor === null) return;
    setLedger((current) => ({ ...current, loading: true }));
    const result = await api.transactions(account.id, { cursor, dateFrom: range.from, dateTo: range.to });
    if (onAccessLoss(result) || token !== generation.current) return;
    setLedger((current) => (result.ok
      ? { rows: [...current.rows, ...result.data.transactions], cursor: result.data.next_cursor,
          uncertain: result.data.uncertain_total, failed: false, loading: false }
      : { ...current, failed: true, loading: false }));
  }, [api, account.id, range, ledger.cursor, onAccessLoss]);

  const apply = (next: DateRange) => { setDraft(next); if (validRange(next)) setRange(next); };
  return (
    <aside className="budget-detail" aria-label={label} ref={panel}>
      <header><h3>{label}</h3><button type="button" onClick={onClose}>{t("budget.main.close")}</button></header>
      <h4>{t("budget.main.balances")}</h4>
      {balances === null || balances.balances.length === 0 ? <p className="budget-muted">{t("budget.main.noBalances")}</p> : (
        <ul className="budget-balances">
          {balances.balances.map((balance, index) => (
            <li key={`${balance.balance_type}-${String(index)}`}>
              <span title={balance.name || balance.balance_type}>{balanceLabel(balance.balance_type, balance.name)}</span>
              <strong>{formatAmount(balance.balance_amount.amount, balance.balance_amount.currency, locale)}</strong>
            </li>
          ))}
        </ul>
      )}
      {balances !== null && balances.archived_imports.length > 0 ? (
        <details className="budget-archived">
          <summary>{t("budget.main.archivedHelp")}</summary>
          <ul>
            {balances.archived_imports.map((item) => (
              <li key={item.id}>
                {t("budget.main.archivedImport", {
                  fetched: when(item.fetched_at, locale) ?? "",
                  imported: when(item.imported_at, locale) ?? "",
                  count: tPlural("budget.main.balanceCount", item.balance_count),
                })}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
      <h4>{t("budget.main.transactions")}</h4>
      <div className="budget-presets" role="group" aria-label={t("budget.main.transactions")}>
        {(["30", "90", "year", "all"] as const).map((preset) => (
          <button type="button" key={preset} onClick={() => { apply(presetRange(preset)); }}>{t(PRESET_KEYS[preset])}</button>
        ))}
      </div>
      <form className="budget-range" onSubmit={(event) => { event.preventDefault(); apply(draft); }}>
        <label>{t("budget.main.from")}<input name="date_from" type="date" value={draft.from} max={draft.to || undefined}
          onChange={(event) => { setDraft({ ...draft, from: event.currentTarget.value }); }} /></label>
        <label>{t("budget.main.to")}<input name="date_to" type="date" value={draft.to} min={draft.from || undefined}
          onChange={(event) => { setDraft({ ...draft, to: event.currentTarget.value }); }} /></label>
        <button type="submit">{t("budget.main.filter")}</button>
      </form>
      {validRange(draft) ? null : <p className="budget-notice" role="alert">{t("budget.main.badRange")}</p>}
      {ledger.failed ? <p className="budget-notice" role="alert">{t("budget.main.error")}</p> : null}
      {ledger.loading && ledger.rows.length === 0 ? <p className="budget-muted" role="status">{t("budget.main.loading")}</p> : null}
      {!ledger.loading && ledger.rows.length === 0 && !ledger.failed ? <p className="budget-muted">{t("budget.main.noTransactions")}</p> : null}
      {ledger.rows.length === 0 ? null : (
        <table className="budget-transactions">
          <tbody>
            {ledger.rows.map((row) => (
              <tr key={row.entry_reference}>
                <td>{transactionDate(row)}</td>
                <td>{transactionText(row)}</td>
                <td className={row.credit_debit_indicator === "DBIT" ? "is-debit" : "is-credit"}>
                  {formatAmount(row.amount, row.currency, locale, row.credit_debit_indicator)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {ledger.uncertain > 0 ? <p className="budget-muted">{tPlural("budget.main.uncertainEntries", ledger.uncertain)}</p> : null}
      {ledger.cursor === null ? null
        : <button type="button" disabled={ledger.loading} onClick={() => { void more(); }}>{t("budget.main.more")}</button>}
    </aside>
  );
}

export function mountBudget(
  element: HTMLElement,
  principal: string | null,
  onLogout?: () => void,
): { setActive: (active: boolean) => void; destroy: () => void } {
  const root = createRoot(element);
  return {
    setActive: (active) => {
      root.render(onLogout === undefined
        ? <Budget active={active} principal={principal} />
        : <Budget active={active} principal={principal} onLogout={onLogout} />);
    },
    destroy: () => { root.unmount(); },
  };
}
