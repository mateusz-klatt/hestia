// Spending analytics (2D) helpers. Every total is computed by the server per currency and arrives as a decimal
// STRING; the browser never adds, subtracts, converts or mixes amounts. Periods are "YYYY-MM" or "YYYY".
import type { components } from "../../api/openapi";
import type { BudgetAccount, BudgetCapabilities, BudgetConnection } from "../model";
import { accountLabel } from "../model";

type Schemas = components["schemas"];

// Generated contract types. Server enums arrive as plain strings, so every label lookup has a fallback.

export type AnalyticsTotals = Schemas["BudgetAnalyticsCurrency"];

export type AnalyticsCategoryTotal = Schemas["BudgetAnalyticsCategoryTotal"];

export type AnalyticsFreshness = Schemas["BudgetAnalyticsFreshness"];

export type AnalyticsScope = Schemas["BudgetAnalyticsScope"];

export type AnalyticsTransferSummary = Schemas["BudgetAnalyticsTransferSummary"];

export type AnalyticsOverview = Schemas["BudgetAnalyticsOverview"];

/** The owner's explicit kind choice; "auto" returns the entry to rules and defaults. */
export type KindChoice = "auto" | "expense" | "income" | "refund" | "excluded";
export const KIND_CHOICES: readonly KindChoice[] = ["auto", "expense", "income", "refund", "excluded"];
/** Effective kinds the server reports and filters by. */
export const EFFECTIVE_KINDS = ["expense", "income", "refund", "unclassified_credit", "transfer", "excluded"] as const;

/** Every entry carries one: mode "auto" with version 0 until the owner decides. */
export type AnalyticsAnnotation = Schemas["BudgetAnalyticsAnnotation"];

export type AnnotationSaved = Schemas["BudgetAnalyticsAnnotationSaved"];

export type TransferStatus = "suggested" | "accepted" | "review" | "boundary" | "rejected" | "undone";

export type AnalyticsTransaction = Schemas["BudgetAnalyticsTransaction"];

export type AnalyticsTransactions = Schemas["BudgetAnalyticsTransactions"];

export type AnalyticsCategory = Schemas["BudgetAnalyticsCategory"];
export type CategorySaved = Schemas["BudgetAnalyticsCategorySaved"];

export interface TransactionFilter { currency: string; category: string; kind: string; query: string }
export const NO_FILTER: TransactionFilter = { currency: "", category: "", kind: "", query: "" };
/** Filter value for entries without a category (the server's explicit uncategorised bucket). */
export const UNCATEGORISED = "uncategorised";

export type AnnotationRequest = Schemas["BudgetAnalyticsAnnotationRequest"];

/** The owner's category choice in the editor: automatic (rules), explicitly none, or a category id. */
export type CategoryChoice = { mode: "auto" } | { mode: "manual"; category_id: string | null };

/** "auto" resets both category and type to rules and defaults; any explicit type is part of a manual decision. */
export function annotationRequest(row: AnalyticsTransaction, category: CategoryChoice, kind: KindChoice): AnnotationRequest {
  return {
    account_id: row.account_id, entry_reference: row.entry_reference, mode: category.mode,
    category_id: category.mode === "manual" ? category.category_id : null, kind: category.mode === "manual" ? kind : "auto",
    expected_fingerprint: row.fingerprint, expected_version: row.annotation.version,
  };
}

/** The editor's starting choice: what the owner set before, or automatic. */
export function currentChoice(row: AnalyticsTransaction): CategoryChoice {
  const annotation = row.annotation;
  return annotation.mode === "manual" ? { mode: "manual", category_id: annotation.category_id } : { mode: "auto" };
}

/** A label for a server enum value; an unknown value is shown as it came. */
export function labelOf(labels: Readonly<Record<string, string>>, value: string): string {
  return labels[value] ?? value;
}

/** The kinds an entry can take by its direction: a payment out is never income or a refund, an incoming one never
 *  an expense. The server rejects the others; rules may name any kind because they skip incompatible entries. */
export function kindChoicesFor(indicator: "CRDT" | "DBIT"): readonly KindChoice[] {
  return indicator === "DBIT" ? ["auto", "expense", "excluded"] : ["auto", "income", "refund", "excluded"];
}

/** A server kind string as an editor choice; anything unknown, or incompatible with the direction, is automatic. */
export function kindChoice(kind: string, indicator?: "CRDT" | "DBIT"): KindChoice {
  const allowed: readonly string[] = indicator === undefined ? KIND_CHOICES : kindChoicesFor(indicator);
  return allowed.includes(kind) ? kind as KindChoice : "auto";
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

export type TransferLeg = Schemas["BudgetAnalyticsLeg"];

export type TransferSource = "account_identity" | "heuristic" | "manual";
const TRANSFER_SOURCES: readonly string[] = ["account_identity", "heuristic", "manual"];

export type TransferPair = Schemas["BudgetAnalyticsTransfer"];

export type TransferList = Schemas["BudgetAnalyticsTransfers"];

export type TransferAction = "accept" | "reject" | "undo";
/** The queues the owner reviews. Rejected and undone pairs never return as suggestions; the owner may re-accept them. */
export const TRANSFER_QUEUES = ["suggested", "review", "boundary", "accepted", "rejected", "undone"] as const;
export type TransferQueue = (typeof TRANSFER_QUEUES)[number];

/** Every field is always sent; legs and fingerprints are null for undo, which names the decision instead. */
export type TransferDecisionRequest = Schemas["BudgetAnalyticsTransferDecisionRequest"];

export function transferDecision(pair: TransferPair, action: TransferAction): TransferDecisionRequest | null {
  const source: TransferSource = TRANSFER_SOURCES.includes(pair.source) ? pair.source as TransferSource : "manual";
  if (action === "undo") {
    if (pair.id === null) return null;
    return { action, source, decision_id: pair.id, debit: null, credit: null, expected_debit_fingerprint: null,
      expected_credit_fingerprint: null, expected_version: pair.version };
  }
  // Confirming a pair again (to re-check after the bank data changed, or rejected or undone) is the owner's explicit
  // reconfirmation with the current fingerprints: it is recorded as manual, because the pair's shown source is
  // historical and the current evidence may differ. Only a fresh suggestion is sent with its verified source.
  const reaccept = action === "accept" && (pair.status === "review" || pair.status === "rejected" || pair.status === "undone");
  return {
    action, source: reaccept ? "manual" : source, decision_id: pair.id, expected_version: pair.version,
    debit: { account_id: pair.debit.account_id, entry_reference: pair.debit.entry_reference },
    credit: { account_id: pair.credit.account_id, entry_reference: pair.credit.entry_reference },
    expected_debit_fingerprint: pair.debit.fingerprint, expected_credit_fingerprint: pair.credit.fingerprint,
  };
}

/** The result of a decision; the view re-reads the period rather than patching rows from it. */
export type TransferDecisionResult = Schemas["BudgetAnalyticsDecisionSaved"];

/** Whether the two legs still form a valid own transfer as shown: both selected, same currency, equal amount (a textual
 *  comparison of the server's canonical decimals, never arithmetic) and at most three days apart. A bank correction
 *  can break this for a pair decided earlier; such a pair can only be undone. */
export function legsMatch(pair: TransferPair): boolean {
  const { debit, credit } = pair;
  return debit.selected && credit.selected && debit.amount !== null && debit.currency !== null
    && debit.amount === credit.amount && debit.currency === credit.currency
    && pair.date_distance !== null && pair.date_distance <= 3;
}

/** The actions the owner can take on a pair; a pair with a leg outside the selection, or legs that no longer match,
 *  cannot be accepted. */
export function transferActions(pair: TransferPair): TransferAction[] {
  const decided = pair.id !== null;
  const acceptable = !pair.boundary && legsMatch(pair);
  switch (pair.status) {
    case "suggested": return acceptable ? ["accept", "reject"] : ["reject"];
    case "review": return acceptable ? ["accept", "undo"] : ["undo"];
    case "boundary": return decided ? ["undo"] : ["reject"];
    case "accepted": return decided ? ["undo"] : [];
    case "rejected":
    case "undone": return acceptable ? ["accept"] : [];
    default: return [];
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

export type CategoryCreateRequest = Schemas["BudgetAnalyticsCategoryCreateRequest"];
export type CategoryUpdateRequest = Schemas["BudgetAnalyticsCategoryUpdateRequest"];

export type RuleField = "description" | "counterparty_name" | "mcc";
export type RuleOperator = "contains" | "equals";
export const RULE_FIELDS: readonly RuleField[] = ["description", "counterparty_name", "mcc"];
export const RULE_OPERATORS: readonly RuleOperator[] = ["contains", "equals"];

export type AnalyticsRule = Schemas["BudgetAnalyticsRule"];
export type RuleSaved = Schemas["BudgetAnalyticsRuleSaved"];

/** A rule as proposed for preview; nothing changes until the previewed rule is saved. */
export type RuleCandidate = Schemas["BudgetAnalyticsRulePreviewRequest"];

/** Only the keys of an affected entry; the preview carries no amounts, dates or descriptions. */
export type RulePreviewExample = Schemas["BudgetAnalyticsPreviewExample"];

export type RulePreview = Schemas["BudgetAnalyticsPreview"];

export function ruleCandidate(rule: AnalyticsRule | null): RuleCandidate {
  if (rule === null) {
    return { rule_id: null, expected_version: 0, field: "description", operator: "contains", value: "", category_id: null,
      kind: "auto", priority: 100, enabled: true };
  }
  const field = (RULE_FIELDS as readonly string[]).includes(rule.field) ? rule.field as RuleField : "description";
  const operator = (RULE_OPERATORS as readonly string[]).includes(rule.operator) ? rule.operator as RuleOperator : "contains";
  return { rule_id: rule.id, expected_version: rule.version, field, operator, value: rule.value,
    category_id: rule.category_id, kind: kindChoice(rule.kind), priority: rule.priority, enabled: rule.enabled };
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

/** An account's name across banks: the bank first, then the account's own label (product and masked suffix, or a
 *  numbered fallback that is only unique within its bank). */
export function accountName(accounts: readonly BudgetAccount[], connections: readonly BudgetConnection[], id: string,
  fallback: string): string {
  const account = accounts.find((item) => item.id === id);
  if (account === undefined) return fallback;
  const label = accountLabel(account, accounts.filter((item) => item.connection === account.connection).indexOf(account), fallback);
  const bank = connections.find((item) => item.id === account.connection)?.bank_name;
  return bank === undefined ? label : `${bank} · ${label}`;
}

/** True only when the server explicitly offers the analytics views. */
export function hasAnalytics(capabilities: BudgetCapabilities | null): boolean {
  return capabilities?.analytics === true;
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
