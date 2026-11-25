// Same-origin budget API calls use the HttpOnly session cookie and send JSON for mutations. Results are
// discriminated, never thrown, so a failed call cannot become an unhandled rejection. Nothing here persists
// financial data in the browser.
import { apiUrl } from "../api/client";
import type { BudgetAccount, BudgetCapabilities, BudgetConnection, BudgetConsent, BudgetConsentFlow,
  BudgetConsentReconcile, BudgetConsentRenewalReminders, BudgetCreditLimitValue, BudgetJob, BudgetKind, BudgetBalances,
  BudgetLimit, BudgetOperation, BudgetTransactions, ConsentIntent } from "./model";
import { idempotencyKey } from "./model";
import type { AnalyticsCategory, AnalyticsForecast, AnalyticsInbox, AnalyticsInboxMarkRequest, AnalyticsInboxMarkSaved,
  AnalyticsMonthlyBudget, AnalyticsMonthlyBudgetCharts, AnalyticsMortgages, AnalyticsOverview, AnalyticsQuarantine,
  AnalyticsRecurring, AnalyticsRule, AnalyticsSpendingLimits, AnalyticsTransactions, AnalyticsTransactionsByKeysRequest,
  AnnotationRequest, AnnotationSaved, CategoryCreateRequest, CategorySaved, CategoryUpdateRequest, MortgagePaymentSaved,
  MortgagePaymentSetRequest, MortgageSaveRequest, MortgageScheduleSaved, RuleCandidate, RulePreview, RuleSaved,
  SpendingLimitSaveRequest, SpendingLimitSaved, TransactionFilter, TransferDecisionRequest, TransferDecisionResult,
  TransferList, TransferQueue } from "./analytics/model";

export interface ReceiptDevice {
  token_id: string;
  device_name: string;
  created_at: number;
  last_used_at: number | null;
  expires_at: number;
}

export interface ReceiptPair {
  code_id: string;
  code: string;
  expires_at: number;
  qr_payload: string;
}

export type BudgetResult<T> =
  | { ok: true; data: T }
  | { ok: false; status: number; error: string; current?: unknown };

type Query = Record<string, string | number | undefined>;

function url(path: string, query: Query = {}): URL {
  const target = apiUrl(`budget/${path}`);
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined) continue;
    const text = String(value);
    if (text !== "") target.searchParams.set(key, text);
  }
  return target;
}

async function request<T>(target: URL, init: RequestInit = {}): Promise<BudgetResult<T>> {
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
  const nested = typeof record.error === "object" && record.error !== null
    ? (record.error as { code?: unknown }).code : undefined;
  const error = typeof record.error === "string" ? record.error
    : typeof nested === "string" ? nested : `budget_http_${String(response.status)}`;
  return { ok: false, status: response.status, error, current: record.current };
}

export interface ConnectionsData { connections: BudgetConnection[]; capabilities: BudgetCapabilities }

export interface OwnBudgetAuthorization {
  permissions: string[]; can_view: boolean; can_use_receipts: boolean; can_manage_devices: boolean;
}
export interface AuthorizationCapabilities {
  user: string | null;
  actor_tag: string | null;
  installation_permissions: string[];
  own_budget: OwnBudgetAuthorization | null;
  delegated_receipts: { permissions: string[]; can_use: boolean } | null;
  household_available: boolean;
}

export function createBudgetApi(expectedActor: string | null = null) {
  let authorizationLocked = false;
  let lockGeneration = 0;
  let verificationGeneration = 0;
  let capabilitiesGeneration = 0;
  let verified = false;
  let snapshotUser: string | null | undefined;
  let snapshotTag: string | null | undefined;
  let denialError = "budget_server_changed";
  const pendingReads = new Set<AbortController>();

  function denial(error = denialError): BudgetResult<never> {
    return { ok: false, status: error === "budget_actor_changed" ? 409 : 503, error };
  }

  function unverify(error: string): void {
    verified = false;
    denialError = error;
    verificationGeneration += 1;
    for (const controller of pendingReads) controller.abort();
  }

  async function capabilities(): Promise<BudgetResult<AuthorizationCapabilities>> {
    const ticket = ++capabilitiesGeneration;
    const result = await request<AuthorizationCapabilities>(apiUrl("me/capabilities"));
    if (ticket !== capabilitiesGeneration) return denial("budget_server_changed");
    if (!result.ok) {
      unverify("budget_server_changed");
      return result;
    }
    const { user, actor_tag: tag } = result.data;
    const validTag = user === null ? tag === null : typeof tag === "string" && /^[A-Za-z0-9_-]{43}$/.test(tag);
    const expected = expectedActor === null || user === expectedActor;
    const sameActor = snapshotUser === undefined || (snapshotUser === user && snapshotTag === tag);
    if (!validTag || !expected || !sameActor) {
      unverify("budget_actor_changed");
      return denial();
    }
    snapshotUser = user;
    snapshotTag = tag;
    verified = true;
    denialError = "budget_server_changed";
    return result;
  }

  async function call<T>(target: URL, init: RequestInit = {}, mutation = false): Promise<BudgetResult<T>> {
    if (!verified) return denial();
    const verification = verificationGeneration;
    const lock = lockGeneration;
    const controller = mutation ? null : new AbortController();
    if (controller !== null) {
      pendingReads.add(controller);
      init = { ...init, signal: controller.signal };
    }
    if (snapshotTag != null) {
      const headers = new Headers(init.headers);
      headers.set("X-Hestia-Expected-Actor", snapshotTag);
      init = { ...init, headers };
    }
    if (mutation && (authorizationLocked || lock !== lockGeneration)) return denial("budget_server_changed");
    try {
      const result = await request<T>(target, init);
      if (verification !== verificationGeneration) return denial();
      if (mutation && lock !== lockGeneration) return denial("budget_server_changed");
      return result;
    } finally {
      if (controller !== null) pendingReads.delete(controller);
    }
  }

  function post<T>(path: string, body: object, headers: Record<string, string> = {},
    mutation = true): Promise<BudgetResult<T>> {
    if (mutation && authorizationLocked) {
      return Promise.resolve({ ok: false, status: 503, error: "budget_server_changed" });
    }
    return call<T>(url(path), {
      method: "POST", headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify(body),
    }, mutation);
  }
  return {
  setAuthorizationLocked: (value: boolean) => {
    if (authorizationLocked !== value) {
      authorizationLocked = value;
      lockGeneration += 1;
    }
  },
  capabilities,
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
      period, currency: filter.currency, category_id: filter.category, kind: filter.kind, query: filter.query,
      cursor, limit,
    })),
  /** One server-bounded page matching 1..500 exact row keys across periods via JSON POST. */
  analyticsTransactionsByKeys: (request: AnalyticsTransactionsByKeysRequest) =>
    post<AnalyticsTransactions>("analytics/transactions-by-keys", request, {}, false),
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
  spreadCategoryColors: () => post<{ categories: AnalyticsCategory[] }>("analytics/category-colors-spread", {}),
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
  /** Unrevoked receipts devices. A 404 means the receipts flag is off. */
  receiptDevices: () => post<ReceiptDevice[]>("receipts/device-token/list", {}, {}, false),
  pairReceiptDevice: () => post<ReceiptPair>("receipts/device-token/pair-code", {}),
  cancelReceiptPairCode: (codeId: string) =>
    post<{ ok: true }>("receipts/device-token/pair-code-cancel", { code_id: codeId }),
  revokeReceiptDevice: (tokenId: string) =>
    post<{ ok: true }>("receipts/device-token/revoke", { token_id: tokenId }),
  setSelected: (account: BudgetAccount, selected: boolean) =>
    post<{ account: BudgetAccount }>("account-selection",
      { account_id: account.id, selected, expected_version: account.control_version }),
  setKind: (account: BudgetAccount, kind: BudgetKind) =>
    post<{ account: BudgetAccount }>("account-kind",
      { account_id: account.id, kind, expected_version: account.control_version }),
  setCreditLimit: (account: BudgetAccount, creditLimit: BudgetCreditLimitValue | null) =>
    post<{ account: BudgetAccount }>("account-credit-limit",
      { account_id: account.id, credit_limit: creditLimit, expected_version: account.control_version }),
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
}

export const budgetApi = createBudgetApi();
export type BudgetApi = ReturnType<typeof createBudgetApi>;
