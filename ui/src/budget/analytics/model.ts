// Spending analytics (2D) helpers. Every total is computed by the server per currency and arrives as a decimal
// STRING; the browser never adds, subtracts, converts or mixes amounts. Periods are "YYYY-MM" or "YYYY".
import type { BudgetCapabilities, BudgetComponentStatus, BudgetTransaction } from "../model";

// Hand-written from the agreed analytics contract until the generated OpenAPI types land in openapi.ts.

export interface AnalyticsTotals {
  currency: string;
  gross_debits: string;
  gross_credits: string;
  expenses: string;
  income: string;
  refunds: string;
  unclassified_credit: string;
  excluded_debits: string;
  excluded_credits: string;
  transfer_debits: string;
  transfer_credits: string;
  /** expenses − refunds, computed by the server. */
  net_spending: string;
}

export interface AnalyticsCategoryTotal {
  category_id: string | null;
  name: string | null;
  archived: boolean;
  currency: string;
  expenses: string;
  refunds: string;
  net_spending: string;
}

export interface AnalyticsFreshness {
  account_id: string;
  transactions: BudgetComponentStatus;
  balances: BudgetComponentStatus;
}

export interface AnalyticsScope {
  selected_accounts: number;
  excluded_accounts: number;
  uncertain_count: number;
  unresolved_count: number;
  import_unresolved_count: number;
  freshness: AnalyticsFreshness[];
}

export interface AnalyticsTransferSummary {
  suggested: number;
  accepted: number;
  review: number;
  rejected: number;
  boundary: number;
  /** The candidate scan stopped at its bound; some pairs may be missing. */
  scan_limited: boolean;
}

export interface AnalyticsOverview {
  period: string;
  start: string;
  end: string;
  snapshot_digest: string;
  scope: AnalyticsScope;
  currencies: AnalyticsTotals[];
  months: { month: string; currencies: AnalyticsTotals[] }[];
  categories: AnalyticsCategoryTotal[];
  transfer_summary: AnalyticsTransferSummary;
}

/** The owner's explicit kind choice; "auto" returns the entry to rules and defaults. */
export type KindChoice = "auto" | "expense" | "income" | "refund" | "excluded";
export const KIND_CHOICES: readonly KindChoice[] = ["auto", "expense", "income", "refund", "excluded"];
/** Effective kinds the server reports and filters by. */
export const EFFECTIVE_KINDS = ["expense", "income", "refund", "unclassified_credit", "transfer", "excluded"] as const;

export interface AnalyticsAnnotation {
  version: number;
  mode: "auto" | "manual";
  category_id: string | null;
  kind: KindChoice;
  /** The bank entry changed after this decision was made; it needs the owner's look. */
  stale: boolean;
}

export type TransferStatus = "suggested" | "accepted" | "review" | "boundary" | "rejected" | "undone";

export interface AnalyticsTransaction extends Pick<BudgetTransaction, "amount" | "currency" | "credit_debit_indicator" | "status"
  | "booking_date" | "value_date" | "transaction_date" | "remittance_information" | "source" | "observed_at" | "api_observed_at"> {
  account_id: string;
  entry_reference: string;
  /** Financial fingerprint: an annotation is accepted only for the entry as the owner saw it. */
  fingerprint: string;
  transfer_fingerprint: string;
  annotation: AnalyticsAnnotation | null;
  effective: {
    kind: (typeof EFFECTIVE_KINDS)[number];
    category_id: string | null;
    source: "default" | "manual" | "rule" | "transfer";
    rule_id: string | null;
  };
  metadata: { creditor_name: string | null; debtor_name: string | null; mcc: string | null } | null;
  transfer: { status: "none" | TransferStatus; decision_id: string | null; candidate_count: number; ambiguous: boolean };
}

export interface AnalyticsTransactions {
  period: string;
  start: string;
  end: string;
  snapshot_digest: string;
  transactions: AnalyticsTransaction[];
  filtered_total: number;
  next_cursor: string | null;
}

export interface AnalyticsCategory { id: string; name: string; archived: boolean; version: number }

export interface TransactionFilter { currency: string; category: string; kind: string; query: string }
export const NO_FILTER: TransactionFilter = { currency: "", category: "", kind: "", query: "" };
/** Filter value for entries without a category (the server's explicit uncategorised bucket). */
export const UNCATEGORISED = "uncategorised";

export interface AnnotationRequest {
  account_id: string;
  entry_reference: string;
  mode: "auto" | "manual";
  category_id: string | null;
  kind: KindChoice;
  /** The entry's financial fingerprint as shown; a changed bank entry is refused, not silently re-annotated. */
  expected_fingerprint: string;
  /** 0 creates the first annotation; otherwise the version the owner saw. */
  expected_version: number;
}

/** The owner's category choice in the editor: automatic (rules), explicitly none, or a category id. */
export type CategoryChoice = { mode: "auto" } | { mode: "manual"; category_id: string | null };

export function annotationRequest(row: AnalyticsTransaction, category: CategoryChoice, kind: KindChoice): AnnotationRequest {
  return {
    account_id: row.account_id, entry_reference: row.entry_reference, mode: category.mode,
    category_id: category.mode === "manual" ? category.category_id : null, kind,
    expected_fingerprint: row.fingerprint, expected_version: row.annotation?.version ?? 0,
  };
}

/** The editor's starting choice: what the owner set before, or automatic. */
export function currentChoice(row: AnalyticsTransaction): CategoryChoice {
  const annotation = row.annotation;
  return annotation === null || annotation.mode === "auto" ? { mode: "auto" } : { mode: "manual", category_id: annotation.category_id };
}

/** The other party's name as the bank reported it: the creditor of a payment, the debtor of an incoming one. */
export function counterparty(row: AnalyticsTransaction): string | null {
  const metadata = row.metadata;
  if (metadata === null) return null;
  return row.credit_debit_indicator === "DBIT" ? metadata.creditor_name : metadata.debtor_name;
}

/** Accounts whose stored transactions are outdated or never downloaded, so the period may be incomplete. */
export function staleAccounts(scope: AnalyticsScope): string[] {
  return scope.freshness.filter((item) => item.transactions.stale || item.transactions.suspended).map((item) => item.account_id);
}

export interface TransferLeg {
  account_id: string;
  entry_reference: string;
  /** Pair fingerprint; null (with amount, currency and date) for a leg outside the selected accounts. */
  fingerprint: string | null;
  amount: string | null;
  currency: string | null;
  date: string | null;
  selected: boolean;
}

export interface TransferPair {
  /** null while the pair is only a suggestion nobody has decided on. */
  id: string | null;
  /** 0 before the first decision. */
  version: number;
  status: TransferStatus;
  /** account_identity: matched by account number; heuristic: equal amount within 3 days; manual: the owner's pairing. */
  source: "account_identity" | "heuristic" | "manual";
  debit: TransferLeg;
  credit: TransferLeg;
  date_distance: number | null;
  /** One of the legs also fits another pair; the owner has to choose, nothing is picked automatically. */
  ambiguous: boolean;
  /** A leg changed at the bank after the decision. */
  stale: boolean;
  /** Only one leg lies inside the selected accounts. */
  boundary: boolean;
}

export interface TransferList {
  period: string;
  start: string;
  end: string;
  snapshot_digest: string;
  transfers: TransferPair[];
  filtered_total: number;
  next_cursor: string | null;
  scan_limited: boolean;
}

export type TransferAction = "accept" | "reject" | "undo";
/** The queues the owner reviews. Rejected and undone pairs never return as suggestions; the owner may re-accept them. */
export const TRANSFER_QUEUES = ["suggested", "review", "boundary", "accepted", "rejected", "undone"] as const;
export type TransferQueue = (typeof TRANSFER_QUEUES)[number];

export type TransferDecisionRequest =
  | {
    action: "accept" | "reject";
    source: TransferPair["source"];
    debit: { account_id: string; entry_reference: string };
    credit: { account_id: string; entry_reference: string };
    expected_debit_fingerprint: string | null;
    expected_credit_fingerprint: string | null;
    expected_version: number;
  }
  | { action: "undo"; decision_id: string; expected_version: number };

export function transferDecision(pair: TransferPair, action: TransferAction): TransferDecisionRequest | null {
  if (action === "undo") return pair.id === null ? null : { action, decision_id: pair.id, expected_version: pair.version };
  // Re-accepting a rejected or undone pair is the owner's explicit override: it is recorded as manual, while the
  // pair's historical source stays untouched on the server.
  const reaccept = action === "accept" && (pair.status === "rejected" || pair.status === "undone");
  return {
    action, source: reaccept ? "manual" : pair.source, expected_version: pair.version,
    debit: { account_id: pair.debit.account_id, entry_reference: pair.debit.entry_reference },
    credit: { account_id: pair.credit.account_id, entry_reference: pair.credit.entry_reference },
    expected_debit_fingerprint: pair.debit.fingerprint, expected_credit_fingerprint: pair.credit.fingerprint,
  };
}

/** The result of a decision; the view re-reads the period rather than patching rows from it. */
export interface TransferDecisionResult { id: string; version: number; status: TransferStatus }

/** The actions the owner can take on a pair; a pair with a leg outside the selection cannot be accepted. */
export function transferActions(pair: TransferPair): TransferAction[] {
  const decided = pair.id !== null;
  switch (pair.status) {
    case "suggested": return pair.boundary ? ["reject"] : ["accept", "reject"];
    case "review": return pair.boundary ? ["undo"] : ["accept", "undo"];
    case "boundary": return decided ? ["undo"] : ["reject"];
    case "accepted": return decided ? ["undo"] : [];
    case "rejected":
    case "undone": return pair.boundary ? [] : ["accept"];
  }
}

/** Analytics refusal codes. A conflict or changed source reloads; a stale or expired preview must be redone. */
export const ANALYTICS_ERROR = {
  conflict: "budget_analytics_conflict",
  sourceChanged: "budget_analytics_source_changed",
  stalePreview: "budget_analytics_stale_preview",
  previewExpired: "budget_analytics_preview_expired",
  legReserved: "budget_analytics_leg_reserved",
  staleCursor: "budget_analytics_stale_cursor",
} as const;
export const RELOAD_ERRORS: ReadonlySet<string> = new Set([ANALYTICS_ERROR.conflict, ANALYTICS_ERROR.sourceChanged,
  ANALYTICS_ERROR.legReserved, ANALYTICS_ERROR.staleCursor]);

export const CATEGORY_NAME_MAX = 80;
export const RULE_VALUE_MAX = 256;
export const RULE_PRIORITY_MAX = 10000;

export interface CategoryCreateRequest { name: string }
export interface CategoryUpdateRequest { category_id: string; name: string; archived: boolean; expected_version: number }

export type RuleField = "description" | "counterparty_name" | "mcc";
export type RuleOperator = "contains" | "equals";
export const RULE_FIELDS: readonly RuleField[] = ["description", "counterparty_name", "mcc"];
export const RULE_OPERATORS: readonly RuleOperator[] = ["contains", "equals"];

export interface AnalyticsRule {
  id: string;
  version: number;
  field: RuleField;
  operator: RuleOperator;
  value: string;
  category_id: string | null;
  kind: KindChoice;
  priority: number;
  enabled: boolean;
}

/** A rule as proposed for preview; nothing changes until the previewed rule is saved. */
export interface RuleCandidate {
  rule_id: string | null;
  /** 0 for a new rule, otherwise the version being replaced. */
  expected_version: number;
  field: RuleField;
  operator: RuleOperator;
  value: string;
  category_id: string | null;
  kind: KindChoice;
  priority: number;
  enabled: boolean;
}

export interface RulePreviewExample {
  account_id: string;
  entry_reference: string;
  date: string | null;
  amount: string;
  currency: string;
  credit_debit_indicator: "CRDT" | "DBIT";
  description: string;
}

export interface RulePreview { preview_id: string; affected_count: number; examples: RulePreviewExample[]; expires_at: number }

export function ruleCandidate(rule: AnalyticsRule | null): RuleCandidate {
  if (rule === null) {
    return { rule_id: null, expected_version: 0, field: "description", operator: "contains", value: "", category_id: null,
      kind: "auto", priority: 100, enabled: true };
  }
  return { rule_id: rule.id, expected_version: rule.version, field: rule.field, operator: rule.operator, value: rule.value,
    category_id: rule.category_id, kind: rule.kind, priority: rule.priority, enabled: rule.enabled };
}

/** A candidate the server could accept: a trimmed non-empty value within limits and an integer priority in range. */
export function validCandidate(candidate: RuleCandidate): boolean {
  const value = candidate.value.trim();
  return value !== "" && value.length <= RULE_VALUE_MAX && Number.isInteger(candidate.priority)
    && candidate.priority >= 0 && candidate.priority <= RULE_PRIORITY_MAX
    && (candidate.category_id !== null || candidate.kind !== "auto");
}

export function validCategoryName(name: string): boolean {
  const trimmed = name.trim();
  return trimmed !== "" && trimmed.length <= CATEGORY_NAME_MAX;
}

const MONTH = /^(\d{4})-(0[1-9]|1[0-2])$/;
const ZERO = /^-?0+(?:\.0+)?$/;

/** True only when the server explicitly offers the analytics views. */
export function hasAnalytics(capabilities: BudgetCapabilities | null): boolean {
  return capabilities !== null && (capabilities as Record<string, unknown>).analytics === true;
}

export function isMonth(value: string): boolean {
  return MONTH.test(value);
}

/** A decimal string equal to zero; a textual check, never a numeric conversion. */
export function isZero(amount: string): boolean {
  return ZERO.test(amount);
}

/** The owner's current calendar month. */
export function currentMonth(now: Date = new Date()): string {
  return `${String(now.getFullYear()).padStart(4, "0")}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

/** The month `delta` months away; an invalid key is returned unchanged. */
export function shiftMonth(month: string, delta: number): string {
  const match = MONTH.exec(month);
  if (match === null) return month;
  const index = Number(match[1]) * 12 + Number(match[2]) - 1 + delta;
  return `${String(Math.floor(index / 12)).padStart(4, "0")}-${String((index % 12) + 1).padStart(2, "0")}`;
}

/** "listopad 2026" / "November 2026" in the page locale; an invalid key is shown as it is. */
export function monthLabel(month: string, locale: string): string {
  const match = MONTH.exec(month);
  if (match === null) return month;
  const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, 15));
  return new Intl.DateTimeFormat(locale, { month: "long", year: "numeric", timeZone: "UTC" }).format(date);
}
