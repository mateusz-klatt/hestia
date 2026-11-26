// Pure budget view helpers. Amounts stay decimal STRINGS end to end: no float parsing, no sums across
// unlike currencies or balance types, and no inference of account kind from currency or product name.
import type { components } from "../api/openapi";

type Schemas = components["schemas"];
export type BudgetConnection = Schemas["BudgetConnection"];
export type BudgetCapabilities = Schemas["BudgetCapabilities"];
export type BudgetAccount = Schemas["BudgetAccount"];
export type BudgetComponentStatus = Schemas["BudgetComponentStatus"];
export type BudgetTransaction = Schemas["BudgetTransaction"];
export type BudgetTransactions = Schemas["BudgetTransactions"];
export type BudgetBalances = Schemas["BudgetBalances"];
export type BudgetBalance = Schemas["BudgetBalance"];
export type BudgetJob = Schemas["BudgetJob"];
export type BudgetKind = Schemas["BudgetKindRequest"]["kind"];
export type BudgetCreditLimit = Schemas["BudgetAccountCreditLimit"];
export type BudgetCreditLimitValue = Schemas["BudgetCreditLimitValue"];
export type BudgetSharedLimit = Schemas["BudgetSharedLimit"];
export type BudgetComponentLimit = Schemas["BudgetComponentLimit"];
export type BudgetLimit = BudgetSharedLimit | BudgetComponentLimit;
export type BudgetConsent = Schemas["BudgetConsent"];
export type BudgetConsentFlow = Schemas["BudgetConsentFlow"];
export type ConsentIntent = Schemas["BudgetConsentStartRequest"]["intent"];
export type BudgetConsentRenewalReminder = Schemas["BudgetConsentRenewalReminder"];
export type BudgetConsentRenewalReminders = Schemas["BudgetConsentRenewalReminders"];
export type BudgetConsentReconcileSummary = Schemas["BudgetConsentReconcileSummary"];
export type BudgetConsentReconcileConsent = Schemas["BudgetConsentReconcileConsent"];
export type BudgetConsentReconcileFlow = Schemas["BudgetConsentReconcileFlow"];
export type BudgetConsentReconcile = Schemas["BudgetConsentReconcile"];

/** Flow states the completion page can still finish (mirrors the server's ready set). */
export const READY_FLOW = new Set(["staged", "candidate", "exchanging"]);
/** Flow states that still block a new start and can be cancelled (mirrors the server's active set). */
export const OPEN_FLOW = new Set(["created", "started", ...READY_FLOW]);

/** "renew" once the connection has ever had a working session; "connect" otherwise. Informational only. */
export function consentIntent(connection: BudgetConnection): ConsentIntent {
  const known = connection.last_success !== null || connection.valid_until !== null || connection.attention !== null
    || connection.renewal_due;
  return known ? "renew" : "connect";
}

/** Only a real HTTPS URL may leave the page for the bank's authorization screen. */
export function safeAuthorizationUrl(value: unknown): string | null {
  if (typeof value !== "string") return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.username === "" && url.password === "" ? url.href : null;
  } catch {
    return null;
  }
}
export type BudgetOperation = "transactions" | "balances";

export const KINDS: readonly BudgetKind[] = ["current", "savings", "credit_card", "other"];
export const ACTIVE_JOB = new Set(["queued", "running"]);

/** Validate and canonicalize a decimal credit-limit input (> 0 and <= 10,000,000), or return null. */
export function normalizeCreditLimitInput(raw: string): string | null {
  const trimmed = raw.trim();
  const normalized = trimmed.includes(".") ? trimmed : trimmed.replace(",", ".");
  if (normalized.length === 0 || normalized.length > 80 || !/^\d+(?:\.\d+)?$/.test(normalized)) return null;
  const [wholeRaw = "0", fracRaw = ""] = normalized.split(".");
  const frac = fracRaw.replace(/0+$/, "");
  const whole = wholeRaw.replace(/^0+/, "") || "0";
  const canonical = frac === "" ? whole : `${whole}.${frac}`;
  const intVal = BigInt(whole);
  if (intVal === 0n && frac === "") return null;
  if (intVal > 10_000_000n || (intVal === 10_000_000n && frac !== "")) return null;
  return canonical;
}

/** Validate a 3-letter uppercase ISO currency code, or return null. */
export function normalizeCreditCurrencyInput(raw: string): string | null {
  const cleaned = raw.trim().toUpperCase();
  return /^[A-Z]{3}$/.test(cleaned) ? cleaned : null;
}

/** Default limit currency from stored limit, observed balance currency, display metadata, or PLN. */
export function defaultCreditLimitCurrency(account: BudgetAccount): string {
  return account.credit_limit?.currency ?? account.observed_balance_currencies?.[0] ?? account.display?.currency ?? "PLN";
}

const DECIMAL = /^(-?)(\d+)(?:\.(\d+))?$/;

function separators(locale: string): { group: string; decimal: string } {
  const parts = new Intl.NumberFormat(locale).formatToParts(11111.1);
  return {
    group: parts.find((part) => part.type === "group")?.value ?? ",",
    decimal: parts.find((part) => part.type === "decimal")?.value ?? ".",
  };
}

/** Format an exact decimal string with locale separators; at least two fraction digits, none dropped.
 *  A debit indicator makes the shown value negative; an unparseable amount is shown verbatim. */
export function formatAmount(amount: string, currency: string, locale: string, indicator?: string): string {
  const match = DECIMAL.exec(amount);
  if (match === null) return `${amount} ${currency}`;
  const minus = match[1] ?? "";
  const whole = match[2] ?? "0";
  const fraction = match[3] ?? "";
  const { group, decimal } = separators(locale);
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, group);
  const negative = (minus === "-") !== (indicator === "DBIT");
  const zero = /^0+$/.test(whole) && /^0*$/.test(fraction);
  return `${negative && !zero ? "−" : ""}${grouped}${decimal}${fraction.padEnd(2, "0")} ${currency}`;
}

/** Round an exact decimal string to signed integer cents (1/100) using half-away-from-zero BigInt arithmetic.
 *  Returns null when the input is not a valid decimal string. */
export function roundToCents(amount: string): bigint | null {
  const match = DECIMAL.exec(amount);
  if (match === null) return null;
  const negative = match[1] === "-";
  const whole = match[2] ?? "0";
  const fraction = match[3] ?? "";
  let cents = BigInt(`${whole}${fraction.slice(0, 2).padEnd(2, "0")}`);
  const nextDigit = fraction[2];
  // Base-10 remainder is >= 0.5 of a cent iff the third fractional digit is at least '5'.
  if (nextDigit !== undefined && nextDigit >= "5") {
    cents += 1n;
  }
  return negative ? -cents : cents;
}

/** Format a signed integer cent count (`value / 100`) as a canonical 2-decimal string without negative zero. */
export function formatCents(cents: bigint): string {
  if (cents === 0n) return "0.00";
  const negative = cents < 0n;
  const digits = (negative ? -cents : cents).toString().padStart(3, "0");
  return `${negative ? "-" : ""}${digits.slice(0, -2)}.${digits.slice(-2)}`;
}

/** Round an exact decimal string to 2 fraction digits, half away from zero, using BigInt arithmetic (never float),
 *  then format with locale separators like `formatAmount`. Unparseable inputs fall back verbatim. */
export function formatEstimate(amount: string, currency: string, locale: string): string {
  const cents = roundToCents(amount);
  return cents === null ? formatAmount(amount, currency, locale) : formatAmount(formatCents(cents), currency, locale);
}

/** The owner-facing name: provider product and masked suffix when known, never a full identifier. */
export function accountLabel(account: BudgetAccount, index: number, fallback: string): string {
  const display = account.display ?? null;
  const product = display?.product ?? null;
  const suffix = display?.masked_suffix ?? null;
  const base = product ?? `${fallback} ${String(index + 1)}`;
  return suffix === null ? base : `${base} ••${suffix}`;
}

/** The single display date of a transaction: booking date, falling back to value then transaction date. */
export function transactionDate(transaction: Pick<BudgetTransaction, "booking_date" | "value_date" | "transaction_date">): string | null {
  return transaction.booking_date ?? transaction.value_date ?? transaction.transaction_date ?? null;
}

export function transactionText(transaction: Pick<BudgetTransaction, "remittance_information">): string {
  return transaction.remittance_information.filter((line) => line.trim() !== "").join(" · ");
}

export type Freshness = "never" | "fresh" | "stale" | "suspended";

export function freshness(component: BudgetComponentStatus): Freshness {
  if (component.suspended) return "suspended";
  if (component.last_success === null) return "never";
  return component.stale ? "stale" : "fresh";
}

/** Local date-time for a Unix timestamp (seconds), or null when absent. */
export function when(seconds: number | null | undefined, locale: string): string | null {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return null;
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(new Date(seconds * 1000));
}

/** A fresh per-click idempotency key (RFC 4122 v4, 36 chars of `[0-9a-f-]`). */
export function idempotencyKey(random: () => string = () => crypto.randomUUID()): string {
  return random();
}

export function latestJob(jobs: readonly BudgetJob[], connectionId: string): BudgetJob | null {
  return jobs.find((job) => job.connection_id === connectionId) ?? null;
}

export interface DateRange { from: string; to: string }

function iso(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${String(date.getFullYear())}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** Inclusive local-date presets; "all" is an empty range (no filter). */
export function presetRange(preset: "30" | "90" | "year" | "all", today: Date = new Date()): DateRange {
  if (preset === "all") return { from: "", to: "" };
  if (preset === "year") return { from: `${String(today.getFullYear())}-01-01`, to: iso(today) };
  const start = new Date(today.getFullYear(), today.getMonth(), today.getDate() - Number(preset) + 1);
  return { from: iso(start), to: iso(today) };
}

export function validRange(range: DateRange): boolean {
  const date = /^\d{4}-\d{2}-\d{2}$/;
  if (range.from !== "" && !date.test(range.from)) return false;
  if (range.to !== "" && !date.test(range.to)) return false;
  return range.from === "" || range.to === "" || range.from <= range.to;
}

/** Currencies seen in current API balances that differ from the provider-declared account currency. */
export function otherObservedCurrencies(account: BudgetAccount): string[] {
  const declared = account.display?.currency ?? null;
  return (account.observed_balance_currencies ?? []).filter((currency) => currency !== declared);
}

/** A refusal that means a legacy browser session must sign in again under sessions-v2. */
export function isSessionUpgradeRequired(result: { ok: boolean; status?: number; error?: string }): boolean {
  return !result.ok && result.status === 401
    && (result.error === "budget_browser_session_upgrade_required"
      || result.error === "browser_session_upgrade_required");
}

/** A refusal that means "not this owner any more" (signed out, lost ledger capability) or "feature disabled". */
export function isAccessLoss(result: { ok: boolean; status?: number; error?: string }): boolean {
  if (isSessionUpgradeRequired(result)) return false;
  return !result.ok && (result.status === 401 || result.status === 403 || result.error === "budget_disabled");
}

/** Merge a freshly read list without letting it roll back an entity the page already shows at a newer
 *  control version (a save that landed while an older read was in flight). Membership follows `incoming`. */
export function newer<T extends { id: string; control_version: number }>(current: readonly T[], incoming: readonly T[]): T[] {
  const shown = new Map(current.map((item) => [item.id, item]));
  return incoming.map((item) => {
    const existing = shown.get(item.id);
    return existing !== undefined && existing.control_version > item.control_version ? existing : item;
  });
}

export function isComponentLimit(limit: BudgetLimit): limit is BudgetComponentLimit {
  return limit.scope === "component" && "account" in limit;
}

export type LimitState =
  | { kind: "suspended" }
  | { kind: "running"; until: number }
  | { kind: "waiting"; until: number; retry: boolean }
  | { kind: "ready" };

/** What a stored limit means right now: an operator suspension, an in-flight lease, a deadline
 *  (cadence, backoff or the bank's Retry-After) or free. Purely descriptive; nothing here can lift it. */
export function limitState(limit: BudgetLimit, now: number): LimitState {
  if (limit.suspended) return { kind: "suspended" };
  if (limit.lease_until !== null && limit.lease_until > now) return { kind: "running", until: limit.lease_until };
  const retry = limit.retry_at ?? 0;
  const until = Math.max(limit.next_attempt, retry, limit.backoff_until);
  if (until > now) return { kind: "waiting", until, retry: limit.retry_at !== null && retry >= until };
  return { kind: "ready" };
}

/** A component can start only when it and every shared scope of its connection allow it. */
export function effectiveState(component: BudgetComponentLimit, shared: readonly BudgetSharedLimit[], now: number): LimitState {
  const states = [component, ...shared].map((limit) => limitState(limit, now));
  if (states.some((state) => state.kind === "suspended")) return { kind: "suspended" };
  const own = states[0];
  if (own?.kind === "running") return own;
  const waits = states.filter((state): state is Extract<LimitState, { kind: "waiting" }> => state.kind === "waiting");
  if (waits.length === 0) return { kind: "ready" };
  const latest = waits.reduce((a, b) => (b.until > a.until ? b : a));
  return latest;
}

export function hasActiveJob(jobs: readonly BudgetJob[]): boolean {
  return jobs.some((job) => ACTIVE_JOB.has(job.status));
}
