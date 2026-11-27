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
  const bookingDate = transaction.booking_date !== null && transaction.booking_date !== ""
    ? transaction.booking_date
    : null;
  const inlineText = bookingDate === null
    ? transaction.account_label
    : `${transaction.account_label} · ${bookingDate}`;
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
      <span className="budget-link-detail">
        <span className="budget-link-primary">{transaction.account_label}</span>
        {bookingDate === null ? null : <span className="budget-link-meta">{" · "}{bookingDate}</span>}
        {expanded && amountText !== null ? <span className="budget-link-meta">{" · "}{amountText}</span> : null}
      </span>
    </button>
  );
}
