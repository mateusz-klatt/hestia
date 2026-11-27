import { t } from "../i18n";
import { formatAmount } from "./model";
import type { ReceiptSummary } from "./receipts_model";

export type ReceiptLinkedTransaction = NonNullable<ReceiptSummary["linked_transaction"]>;

export function LinkedTransactionIndicator({ transaction, locale }: {
  transaction: ReceiptLinkedTransaction;
  locale: string;
}) {
  const inlineText = transaction.booking_date === null || transaction.booking_date === ""
    ? transaction.account_label
    : `${transaction.account_label} · ${transaction.booking_date}`;
  const amountText = transaction.amount !== null && transaction.currency !== null
    ? formatAmount(transaction.amount, transaction.currency, locale)
    : null;
  const details = amountText === null ? inlineText : `${inlineText} · ${amountText}`;
  const label = t("budget.receipts.linkedTransaction", { details });
  return (
    <span
      className="budget-chip budget-link-indicator budget-receipt-tx-link"
      title={label}
      aria-label={label}
    >
      <span className="budget-link-icon" aria-hidden="true">🏦</span>
      <span className="budget-link-detail">{inlineText}</span>
    </span>
  );
}
