import { useId, useState } from "react";
import { t } from "../i18n";
import { isPositiveDecimal, labelOf } from "./analytics/model";
import { formatAmount } from "./model";
import {
  RECEIPT_DOC_KIND_KEYS,
  RECEIPT_STATUS_KEYS,
  addDecimals,
  candidateDisplayDate,
  isRedactedValue,
  subtractDecimals,
} from "./receipts_model";
import type { ReceiptCandidate, ReceiptSummary } from "./receipts_model";

const MIN_MEMBERS = 2;
const MAX_MEMBERS = 8;

type GroupTransaction = Readonly<Pick<ReceiptCandidate,
  "account_id" | "entry_reference" | "amount" | "currency" | "credit_debit_indicator"
  | "booking_date" | "value_date" | "transaction_date" | "candidate_token"
>> & {
  readonly evidence_fingerprint: string;
};

type GroupMember = Readonly<Pick<ReceiptSummary,
  "receipt_id" | "version" | "status" | "receipt_kind" | "receipt_date" | "raw_shop_name"
  | "total_amount" | "currency" | "document_kind"
> & Partial<Pick<ReceiptSummary, "gateway" | "evidence_for_receipt_id">>>;

type GroupEvidence = Readonly<Pick<ReceiptSummary,
  "receipt_id" | "version" | "status" | "receipt_kind" | "receipt_date" | "total_amount" | "currency" | "gateway"
  | "fee_amount" | "evidence_for_receipt_id"
>>;

export interface ReceiptGroupPool {
  /** The entry reference is a digest. The token and fingerprint are opaque. */
  readonly transaction: GroupTransaction;
  readonly anchor_receipt_id: string;
  /** Rows proposed for this transaction, already scoped by the caller for eligibility. */
  readonly members: readonly GroupMember[];
  readonly evidence?: readonly GroupEvidence[];
}

type ReviewedReceipt = {
  receipt_id: string;
  expected_version: number;
  status: "imported" | "confirmed";
};

export interface ReceiptGroupSelection {
  transaction: GroupTransaction;
  members: readonly ReviewedReceipt[];
  evidence: (ReviewedReceipt & { fee_amount: string | null }) | null;
}

export interface ReceiptGroupBuilderLabels {
  title: string;
  transaction: string;
  members: string;
  anchorReceipt: string;
  memberCount: (count: number, min: number, max: number) => string;
  noEligibleMembers: string;
  remainingDelta: string;
  importedFirst: string;
  confirm: string;
}

interface ReceiptGroupBuilderProps {
  pool: ReceiptGroupPool;
  labels: ReceiptGroupBuilderLabels;
  locale: string;
  accountLabel?: string | null;
  onConfirm: (selection: ReceiptGroupSelection) => void;
}

function RedactedOrText({ value, fallback }: { value: string | null; fallback: string }) {
  if (value === null || value === "") return <>{fallback}</>;
  if (isRedactedValue(value)) {
    return (
      <span className="budget-redacted" title={t("budget.receipts.redactedTooltip")}>
        {t("budget.receipts.redactedLabel")}
      </span>
    );
  }
  return <>{value}</>;
}

type SelectableReceipt<T> = T & { status: ReviewedReceipt["status"]; total_amount: string };

function hasSelectableTotal<T extends GroupMember | GroupEvidence>(row: T): row is SelectableReceipt<T> {
  return (row.status === "confirmed" || row.status === "imported")
    && row.total_amount !== null && isPositiveDecimal(row.total_amount);
}

function reviewedReceipt(row: SelectableReceipt<GroupMember | GroupEvidence>): ReviewedReceipt {
  return { receipt_id: row.receipt_id, expected_version: row.version, status: row.status };
}

/** Presentation content for the parent workflow's review surface. */
export function ReceiptGroupBuilder(props: ReceiptGroupBuilderProps) {
  const { account_id, entry_reference, evidence_fingerprint } = props.pool.transaction;
  return (
    <GroupReview
      key={JSON.stringify([account_id, entry_reference, evidence_fingerprint, props.pool.anchor_receipt_id])}
      {...props}
    />
  );
}

function GroupReview({ pool, labels, locale, accountLabel, onConfirm }: ReceiptGroupBuilderProps) {
  const headingId = useId();
  const countId = useId();
  const importedId = useId();
  const [memberVersions, setMemberVersions] = useState<ReadonlyMap<string, number>>(new Map());
  const [evidenceChoice, setEvidenceChoice] = useState<ReviewedReceipt | null>(null);
  const { transaction, evidence = [] } = pool;
  const { amount, currency, credit_debit_indicator: direction } = transaction;
  const receiptKind = direction === "DBIT" ? "purchase" : "refund";
  const evidenceIds = new Set(evidence.map((receipt) => receipt.receipt_id));
  const eligible = new Map<string, SelectableReceipt<GroupMember>>();
  for (const member of pool.members) {
    if (hasSelectableTotal(member) && member.currency === currency && member.receipt_kind === receiptKind
      && member.gateway == null && member.evidence_for_receipt_id == null
      && !evidenceIds.has(member.receipt_id)) {
      eligible.set(member.receipt_id, member);
    }
  }
  const members = [...eligible.values()];
  const selected = members.filter((member) => memberVersions.get(member.receipt_id) === member.version);
  const gateways = evidence.filter((receipt): receipt is SelectableReceipt<GroupEvidence> => (
    hasSelectableTotal(receipt) && receipt.gateway !== null && receipt.gateway !== ""
    && receipt.evidence_for_receipt_id === null && receipt.currency === currency && receipt.receipt_kind === receiptKind
    && subtractDecimals(amount, receipt.total_amount) === "0"
    && (receipt.fee_amount === null || isPositiveDecimal(receipt.fee_amount) || addDecimals([receipt.fee_amount]) === "0")
  ));
  const selectedEvidence = gateways.find((receipt) => receipt.receipt_id === evidenceChoice?.receipt_id
    && receipt.version === evidenceChoice.expected_version);
  const selectedFee = positiveFee(selectedEvidence?.fee_amount ?? null);

  const totals = selected.map((member) => member.total_amount);
  if (selectedFee !== null) totals.push(selectedFee);
  const sum = addDecimals(totals);
  const remaining = sum === null ? null : subtractDecimals(amount, sum);
  const imported = selected.some((member) => member.status === "imported") || selectedEvidence?.status === "imported";
  const canConfirm = selected.length >= MIN_MEMBERS && selected.length <= MAX_MEMBERS && remaining === "0"
    && selected.some((member) => member.receipt_id === pool.anchor_receipt_id);

  return (
    <section className="budget-receipt-drawer-body" aria-labelledby={headingId}>
      <h3 id={headingId}>{labels.title}</h3>
      <div className="budget-notice budget-receipt-suggested">
        <strong>{labels.transaction}</strong>
        <div className="budget-entry-main">
          <span className="budget-muted">
            <RedactedOrText value={candidateDisplayDate(transaction)} fallback={t("budget.receipts.noDate")} />
          </span>
          <span className="budget-entry-text">
            <RedactedOrText value={accountLabel ?? null} fallback={t("budget.main.account")} />
          </span>
          <strong className={direction === "DBIT" ? "is-debit" : "is-credit"}>
            {isPositiveDecimal(amount) ? formatAmount(amount, currency, locale, direction) : t("budget.receipts.noAmount")}
          </strong>
        </div>
      </div>

      <fieldset className="budget-receipt-link-choices" aria-describedby={countId}>
        <legend>{labels.members}</legend>
        <p id={countId} className="budget-muted">{labels.memberCount(selected.length, MIN_MEMBERS, MAX_MEMBERS)}</p>
        {members.length === 0 && <p className="budget-muted">{labels.noEligibleMembers}</p>}
        {members.map((member) => {
          const checked = memberVersions.get(member.receipt_id) === member.version;
          return (
            <label key={member.receipt_id} className="budget-receipt-candidate">
              <input
                type="checkbox"
                checked={checked}
                disabled={!checked && selected.length >= MAX_MEMBERS}
                onChange={(event) => {
                  const next = new Map(selected.map((row) => [row.receipt_id, row.version]));
                  if (event.currentTarget.checked) next.set(member.receipt_id, member.version);
                  else next.delete(member.receipt_id);
                  setMemberVersions(next);
                }}
              />{" "}
              <span className="budget-receipt-merchant">
                <RedactedOrText value={member.raw_shop_name} fallback={t("budget.receipts.unknownMerchant")} />
              </span>
              {member.receipt_id === pool.anchor_receipt_id && <>{" · "}{labels.anchorReceipt}</>}
              {" · "}<RedactedOrText value={member.receipt_date} fallback={t("budget.receipts.noDate")} />
              {member.document_kind !== null && <>{" · "}{labelOf(RECEIPT_DOC_KIND_KEYS, member.document_kind)}</>}
              {" · "}{labelOf(RECEIPT_STATUS_KEYS, member.status)}
              {" · "}<strong className="budget-receipt-total">{formatAmount(member.total_amount, currency, locale)}</strong>
            </label>
          );
        })}
      </fieldset>

      {gateways.length > 0 && (
        <fieldset className="budget-receipt-link-choices">
          <legend>{t("budget.receipts.paymentEvidence")}</legend>
          {gateways.map((gateway) => {
            const fee = positiveFee(gateway.fee_amount);
            return (
              <label key={gateway.receipt_id} className="budget-receipt-candidate">
                <input
                  type="checkbox"
                  checked={selectedEvidence?.receipt_id === gateway.receipt_id}
                  onChange={(event) => {
                    setEvidenceChoice(event.currentTarget.checked ? reviewedReceipt(gateway) : null);
                  }}
                />{" "}
                <RedactedOrText value={gateway.gateway} fallback={t("budget.receipts.unknownMerchant")} />
                {" · "}<RedactedOrText value={gateway.receipt_date} fallback={t("budget.receipts.noDate")} />
                {" · "}{labelOf(RECEIPT_STATUS_KEYS, gateway.status)}
                {fee !== null && <>{" · "}{t("budget.receipts.paymentEvidenceFee", {
                  amount: formatAmount(fee, currency, locale),
                })}</>}
              </label>
            );
          })}
        </fieldset>
      )}

      <output aria-live="polite">
        {labels.remainingDelta}{" "}
        <strong>{remaining === null ? t("budget.receipts.noAmount") : formatAmount(remaining, currency, locale)}</strong>
      </output>
      <div aria-live="polite">
        {imported && <p className="budget-notice" id={importedId}>{labels.importedFirst}</p>}
      </div>
      <div className="budget-actions">
        <button
          type="button"
          disabled={!canConfirm}
          aria-describedby={imported ? importedId : undefined}
          onClick={() => {
            if (canConfirm) {
              onConfirm({
                transaction,
                members: selected.map(reviewedReceipt),
                evidence: selectedEvidence === undefined ? null : {
                  ...reviewedReceipt(selectedEvidence),
                  fee_amount: selectedFee,
                },
              });
            }
          }}
        >
          {labels.confirm}
        </button>
      </div>
    </section>
  );
}

function positiveFee(amount: string | null): string | null {
  return amount !== null && isPositiveDecimal(amount) ? amount : null;
}
