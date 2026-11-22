// Same-origin budget API calls. The HttpOnly session cookie authenticates; mutations send JSON and never an
// Authorization header (the server rejects bearer mutations). Results are discriminated, never thrown, so a
// failed call cannot become an unhandled rejection. Nothing here persists financial data in the browser.
import { apiUrl } from "../api/client";
import type { BudgetAccount, BudgetCapabilities, BudgetConnection, BudgetConsent, BudgetConsentFlow,
  BudgetConsentReconcile, BudgetConsentRenewalReminders, BudgetJob, BudgetKind, BudgetBalances, BudgetLimit,
  BudgetOperation, BudgetTransactions, ConsentIntent } from "./model";
import { idempotencyKey } from "./model";
import type { AnalyticsCategory, AnalyticsForecast, AnalyticsInbox, AnalyticsInboxMarkRequest, AnalyticsInboxMarkSaved,
  AnalyticsMonthlyBudget, AnalyticsMonthlyBudgetCharts, AnalyticsMortgages, AnalyticsOverview, AnalyticsQuarantine,
  AnalyticsRecurring, AnalyticsRule, AnalyticsSpendingLimits, AnalyticsTransactions, AnnotationRequest, AnnotationSaved,
  CategoryCreateRequest, CategorySaved, CategoryUpdateRequest, MortgagePaymentSaved, MortgagePaymentSetRequest,
  MortgageSaveRequest, MortgageScheduleSaved, RuleCandidate, RulePreview, RuleSaved, SpendingLimitSaveRequest,
  SpendingLimitSaved, TransactionFilter, TransferDecisionRequest, TransferDecisionResult, TransferList,
  TransferQueue } from "./analytics/model";

export type BudgetResult<T> =
  | { ok: true; data: T }
  | { ok: false; status: number; error: string; current?: unknown };

type Query = Record<string, string | number | undefined>;

function url(path: string, query: Query = {}): URL {
  const target = apiUrl(`budget/${path}`);
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== "") target.searchParams.set(key, String(value));
  }
  return target;
}

async function call<T>(target: URL, init: RequestInit = {}): Promise<BudgetResult<T>> {
  let response: Response;
  try {
    response = await fetch(target, { credentials: "same-origin", cache: "no-store", ...init });
  } catch {
    return { ok: false, status: 0, error: "budget_network" };
  }
  // Middleware 401/403 bodies are empty, which is not JSON.
  const body = (await response.json().catch(() => null)) as unknown;
  if (response.ok) {
    if (body !== null && typeof body === "object") return { ok: true, data: body as T };
    // A 2xx whose body cannot be read leaves the outcome unknown (the write may have happened).
    return { ok: false, status: response.status, error: "budget_uncertain" };
  }
  const record = (body ?? {}) as { error?: unknown; current?: unknown };
  const error = typeof record.error === "string" ? record.error : `budget_http_${String(response.status)}`;
  return { ok: false, status: response.status, error, current: record.current };
}

function post<T>(path: string, body: object, headers: Record<string, string> = {}): Promise<BudgetResult<T>> {
  return call<T>(url(path), {
    method: "POST", headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify(body),
  });
}

export interface ConnectionsData { connections: BudgetConnection[]; capabilities: BudgetCapabilities }

export const budgetApi = {
  /** The cookie's current principal; another tab may have signed out or in as someone else. */
  whoami: () => call<{ user: string | null; role: string | null }>(apiUrl("whoami")),
  connections: () => call<ConnectionsData>(url("connections")),
  accounts: (connectionId?: string) =>
    call<{ accounts: BudgetAccount[] }>(url("accounts", { connection_id: connectionId })),
  transactions: (accountId: string,
    options: { cursor?: string | undefined; dateFrom?: string; dateTo?: string; limit?: number } = {}) =>
    call<BudgetTransactions>(url("transactions", {
      account_id: accountId, cursor: options.cursor, date_from: options.dateFrom, date_to: options.dateTo,
      limit: options.limit,
    })),
  balances: (accountId: string) => call<BudgetBalances>(url("balances", { account_id: accountId })),
  /** Stored limits only: no bank request, no way to lift a suspension or deadline from the browser. */
  limits: () => call<{ limits: BudgetLimit[] }>(url("limits")),
  consentStatus: () => call<{ consents: BudgetConsent[] }>(url("consent/status")),
  consentPending: () => call<{ pending: BudgetConsentFlow[] }>(url("consent/pending")),
  /** Read-only consent renewal reminders derived from stored valid_until and attention (no bank requests). */
  consentRenewalReminders: () => call<BudgetConsentRenewalReminders>(url("consent/renewal-reminders")),
  /** Read-only consent reconciliation summary and opaque classifications (zero actions, no bank requests). */
  consentReconcile: () => call<BudgetConsentReconcile>(url("consent/reconcile")),
  /** Starts a bank authorization; the browser then leaves for the returned URL. No bank data is read here. */
  consentStart: (connection: BudgetConnection, intent: ConsentIntent) =>
    post<{ flow_id: string; authorization_url: string; expires_at: number }>("consent/start",
      { connection_id: connection.id, expected_version: connection.control_version, intent }),
  consentCancel: (flowId: string) => post<{ flow_id: string; status: string }>("consent/cancel", { flow_id: flowId }),
  jobs: (connectionId?: string) => call<{ jobs: BudgetJob[] }>(url("jobs", { connection_id: connectionId })),
  /** Server-computed per-currency totals of one period ("YYYY-MM" or "YYYY") for the selected accounts. */
  overview: (period: string, currency?: string) =>
    call<AnalyticsOverview>(url("analytics/overview", { period, currency })),
  /** One server-bounded page of a period's entries; empty filter values are omitted. */
  analyticsTransactions: (period: string, filter: TransactionFilter, cursor?: string, limit?: number) =>
    call<AnalyticsTransactions>(url("analytics/transactions", {
      period, currency: filter.currency, category_id: filter.category, kind: filter.kind, query: filter.query, cursor, limit,
    })),
  categories: () => call<{ categories: AnalyticsCategory[] }>(url("analytics/categories")),
  /** The owner's explicit category/kind decision for one entry, versioned against concurrent edits. */
  annotate: (request: AnnotationRequest) => post<{ annotation: AnnotationSaved }>("analytics/annotation", request),
  /** Own-transfer pairs of a period in one state; suggestions are never accepted without the owner. */
  transfers: (period: string, status: TransferQueue, cursor?: string) =>
    call<TransferList>(url("analytics/transfers", { period, status, cursor })),
  decideTransfer: (request: TransferDecisionRequest) =>
    post<{ decision: TransferDecisionResult }>("analytics/transfer-decision", request),
  createCategory: (request: CategoryCreateRequest) => post<{ category: CategorySaved }>("analytics/category-create", request),
  updateCategory: (request: CategoryUpdateRequest) => post<{ category: CategorySaved }>("analytics/category-update", request),
  rules: () => call<{ rules: AnalyticsRule[] }>(url("analytics/rules")),
  /** Shows what a rule would change; nothing is applied until the preview is saved. */
  previewRule: (candidate: RuleCandidate) => post<{ preview: RulePreview }>("analytics/rule-preview", candidate),
  saveRule: (previewId: string) => post<{ rule: RuleSaved }>("analytics/rule-save", { preview_id: previewId }),
  /** Read-only recurring-charge hints computed by the server across selected eligible expenses. */
  recurring: (currency?: string) => call<AnalyticsRecurring>(url("analytics/recurring", { currency })),
  /** Exact quarantine counts and bounded safe metadata, optionally for one owned account. */
  quarantine: (accountId?: string) => call<AnalyticsQuarantine>(url("analytics/quarantine", { account_id: accountId })),
  /** Monthly category spending limits and server-evaluated statuses for one YYYY-MM period. */
  spendingLimits: (period: string, currency?: string) =>
    call<AnalyticsSpendingLimits>(url("analytics/spending-limits", { period, currency })),
  /** Creates (expected_version=0) or updates one monthly category spending limit with strict CAS. */
  saveSpendingLimit: (request: SpendingLimitSaveRequest) =>
    post<{ limit: SpendingLimitSaved }>("analytics/spending-limit-save", request),
  /** Owner-private mortgage schedules evaluated against server UTC today (evaluation-only). */
  mortgages: () => call<AnalyticsMortgages>(url("analytics/mortgages")),
  /** Read-only estimate of scheduled and recurring commitments, with exact per-currency totals. */
  forecast: (months?: number, currency?: string) => call<AnalyticsForecast>(url("analytics/forecast", { months, currency })),
  /** Read-only owner-private monthly budget dashboard evaluated in Europe/Warsaw from one snapshot. */
  monthlyBudget: (period?: string, currency?: string) =>
    call<AnalyticsMonthlyBudget>(url("analytics/monthly-budget", { period, currency })),
  /** Read-only owner-private monthly budget dashboard and chart series evaluated from one snapshot. */
  monthlyBudgetCharts: (period?: string, currency?: string, history?: 6 | 12) =>
    call<AnalyticsMonthlyBudgetCharts>(url("analytics/monthly-budget/charts", { period, currency, history })),
  /** Creates (schedule_id=null, expected_version=0) or updates one private mortgage schedule. */
  saveMortgage: (request: MortgageSaveRequest) =>
    post<{ schedule: MortgageScheduleSaved }>("analytics/mortgage-save", request),
  /** Marks or unmarks one YYYY-MM installment paid on an owned mortgage schedule with strict CAS. */
  setMortgagePayment: (request: MortgagePaymentSetRequest) =>
    post<{ payment: MortgagePaymentSaved }>("analytics/mortgage-payment-set", request),
  /** Pure read-only single-snapshot evaluation of currently active owner inbox alerts. */
  inbox: () => call<AnalyticsInbox>(url("analytics/inbox")),
  /** Atomically marks or unmarks 1..100 owner inbox alert keys under anti-ABA reads_version CAS. */
  markInbox: (request: AnalyticsInboxMarkRequest) =>
    post<{ inbox: AnalyticsInboxMarkSaved }>("analytics/inbox-mark", request),
  setSelected: (account: BudgetAccount, selected: boolean) =>
    post<{ account: BudgetAccount }>("account-selection",
      { account_id: account.id, selected, expected_version: account.control_version }),
  setKind: (account: BudgetAccount, kind: BudgetKind) =>
    post<{ account: BudgetAccount }>("account-kind",
      { account_id: account.id, kind, expected_version: account.control_version }),
  setPaused: (connection: BudgetConnection, paused: boolean) =>
    post<{ connection: BudgetConnection }>("connection-state",
      { connection_id: connection.id, paused, expected_version: connection.control_version }),
  setBackground: (connection: BudgetConnection, enabled: boolean) =>
    post<{ connection: BudgetConnection }>("background-selection",
      { connection_id: connection.id, enabled, expected_version: connection.control_version }),
  /** One key per user action: a retried or double-clicked request reuses it and gets the same job. */
  refresh: async (connection: BudgetConnection, target: { accountId?: string; operation?: BudgetOperation } = {},
    key: string = idempotencyKey()): Promise<BudgetResult<{ job: BudgetJob & { created: boolean } }>> => {
    const result = await post<{ job?: unknown }>("refresh", {
      connection_id: connection.id, expected_version: connection.control_version,
      ...(target.accountId === undefined ? {} : { account_id: target.accountId }),
      ...(target.operation === undefined ? {} : { operation: target.operation }),
    }, { "Idempotency-Key": key });
    if (!result.ok) return result;
    const job: unknown = result.data.job;
    const fields = (typeof job === "object" && job !== null ? job : {}) as Record<string, unknown>;
    if (typeof fields.id !== "string" || typeof fields.status !== "string" || typeof fields.connection_id !== "string") {
      return { ok: false, status: 202, error: "budget_uncertain" };
    }
    return { ok: true, data: { job: job as BudgetJob & { created: boolean } } };
  },
};

export type BudgetApi = typeof budgetApi;
