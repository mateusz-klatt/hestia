// Same-origin budget API calls. The HttpOnly session cookie authenticates; mutations send JSON and never an
// Authorization header (the server rejects bearer mutations). Results are discriminated, never thrown, so a
// failed call cannot become an unhandled rejection. Nothing here persists financial data in the browser.
import { apiUrl } from "../api/client";
import type { BudgetAccount, BudgetCapabilities, BudgetConnection, BudgetJob, BudgetKind, BudgetBalances,
  BudgetOperation, BudgetTransactions } from "./model";
import { idempotencyKey } from "./model";

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
  jobs: (connectionId?: string) => call<{ jobs: BudgetJob[] }>(url("jobs", { connection_id: connectionId })),
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
