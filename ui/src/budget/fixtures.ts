// Synthetic test fixtures only; no real account, transaction or bank data.
import type { BudgetAccount, BudgetComponentStatus, BudgetConnection } from "./model";

export const component: BudgetComponentStatus = {
  last_attempt: null, last_success: null, last_error: null, failures: 0, next_attempt: 0, state: "never",
  retry_at: null, suspended: false, stale: true, age_seconds: null, stale_after_seconds: 86400,
};

export function account(changes: Partial<BudgetAccount> = {}): BudgetAccount {
  return {
    id: "local-a", connection: "bank", configured: true, selected: true, active: true, paused: false,
    control_version: 1, kind: "unknown", kind_confirmed_at: null, kind_confirmed_by: null,
    kind_confirmation_origin: null, components: { transactions: component, balances: component },
    display: null, observed_balance_currencies: [], ...changes,
  };
}

export const connection: BudgetConnection = {
  id: "bank", enabled: true, paused: false, background_enabled: false, attention: null, control_version: 4,
  bank_name: "Synthetic Bank", country: "PL", valid_until: null, renewal_due: false, last_attempt: null,
  last_success: null, last_error: null,
};
