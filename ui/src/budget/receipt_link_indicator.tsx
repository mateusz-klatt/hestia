import { useState } from "react";
import { t } from "../i18n";
import { formatAmount } from "./model";
import type { ReceiptSummary } from "./receipts_model";

export type ReceiptLinkedTransaction = NonNullable<ReceiptSummary["linked_transaction"]>;

export function LinkedTransactionIndicator({ transaction, locale }: {
  transaction: ReceiptLinkedTransaction;
  locale: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const inlineText = transaction.booking_date === null || transaction.booking_date === ""
    ? transaction.account_label
    : `${transaction.account_label} · ${transaction.booking_date}`;
  const amountText = transaction.amount !== null && transaction.currency !== null
    ? formatAmount(transaction.amount, transaction.currency, locale)
    : null;
  const details = amountText === null ? inlineText : `${inlineText} · ${amountText}`;
  const label = t("budget.receipts.linkedTransaction", { details });
  return (
    <button
      type="button"
      className="budget-chip budget-link-indicator budget-receipt-tx-link"
      aria-expanded={expanded}
      title={label}
      aria-label={label}
      onClick={() => { setExpanded((current) => !current); }}
    >
      <span className="budget-link-icon" aria-hidden="true">🏦</span>
      <span className="budget-link-detail">{expanded ? details : inlineText}</span>
    </button>
  );
}
