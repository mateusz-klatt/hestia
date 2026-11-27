// Month transactions kind-filter helpers: exclude skip-in-budget kinds by default and persist the viewer's choice.
import { EFFECTIVE_KINDS } from "./model";

/** Effective kinds omitted from budget spending and income totals (own transfers / card repayments and excluded entries). */
export const SKIP_IN_BUDGET_KINDS = ["transfer", "excluded"] as const;
export type SkipInBudgetKind = (typeof SKIP_IN_BUDGET_KINDS)[number];
const SKIP_IN_BUDGET_SET: ReadonlySet<string> = new Set(SKIP_IN_BUDGET_KINDS);

/** Client-side sentinel kind filter value that hides skip-in-budget kinds by default in Miesiąc. */
export const KIND_FILTER_EXCLUDE_SKIPPED = "exclude_skipped";

/** Whether an effective kind is excluded from budget spending/income totals. */
export function isSkipInBudgetKind(kind: string): boolean {
  return SKIP_IN_BUDGET_SET.has(kind);
}

export const KIND_FILTER_STORAGE_KEY = "hestia.budget.month.kindFilter";
const VALID_KIND_FILTERS: ReadonlySet<string> = new Set([KIND_FILTER_EXCLUDE_SKIPPED, "", ...EFFECTIVE_KINDS]);

export function readKindFilterPreference(): string {
  try {
    const stored = localStorage.getItem(KIND_FILTER_STORAGE_KEY);
    if (stored === null) return KIND_FILTER_EXCLUDE_SKIPPED;
    return VALID_KIND_FILTERS.has(stored) ? stored : KIND_FILTER_EXCLUDE_SKIPPED;
  } catch {
    return KIND_FILTER_EXCLUDE_SKIPPED;
  }
}

export function writeKindFilterPreference(kind: string): void {
  try {
    if (!VALID_KIND_FILTERS.has(kind)) return;
    localStorage.setItem(KIND_FILTER_STORAGE_KEY, kind);
  } catch {
    // Storage unavailable (private mode or blocked storage); keep in-memory state only.
  }
}
