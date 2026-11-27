// Private controller modal for M:1 receipt group review, sequential unlinked pre-confirmation
// of imported documents, candidate token refresh, and CAS-guarded group/link submission.
import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { t } from "../i18n";
import { formatAmount } from "./model";
import type { BudgetAccount, BudgetConnection } from "./model";
import { ReceiptGroupBuilder } from "./receipt_group_builder";
import type { ReceiptGroupBuilderLabels, ReceiptGroupSelection } from "./receipt_group_builder";
import type { ReceiptsApi } from "./receipts_api";
import {
  GROUP_MAX_SPAN_DAYS,
  buildGroupLinkRequest,
  buildGroupPreConfirmRequest,
  groupTransactionFromProposal,
  isGroupDateSpanValid,
  mergeGroupPoolMembers,
  positiveGroupFee,
  receiptAccountLabel,
  suggestedCandidateDates,
} from "./receipts_model";
import type {
  PendingReceiptStore,
  ReceiptGroupCandidate,
  ReceiptGroupCandidatesResult,
  ReceiptGroupLinkResult,
  ReceiptGroupTransactionCandidate,
  ReceiptSettledEvent,
  ReceiptSummary,
} from "./receipts_model";
import "./receipt_group_controller.css";

export type ReceiptAccessBarrier = (
  result: { ok: boolean; status?: number; error?: string },
  options?: { explicitDisabledOnly?: boolean },
) => boolean;

export type ReceiptGroupControllerTarget =
  | { readonly kind: "candidates"; readonly anchor: ReceiptSummary }
  | { readonly kind: "proposal"; readonly anchor: ReceiptSummary; readonly proposal: ReceiptGroupCandidate }
  | { readonly kind: "bulk"; readonly anchor: ReceiptSummary; readonly selectedRows: readonly ReceiptSummary[] };

export interface ReceiptGroupControllerProps {
  api: Pick<
    ReceiptsApi,
    "receiptDetail" | "confirmReceipt" | "receiptGroupCandidates" | "linkReceiptGroup" | "isAuthorizationLocked"
  >;
  store: PendingReceiptStore;
  target: ReceiptGroupControllerTarget;
  actorKey: string;
  accounts: readonly BudgetAccount[];
  connections: readonly BudgetConnection[];
  locale: string;
  revision: number;
  onAccessLoss: ReceiptAccessBarrier;
  onClose: () => void;
  onPartialConfirm: (affectedIds: readonly string[]) => void;
  onGroupLinked: (result: ReceiptGroupLinkResult, affectedIds: readonly string[]) => void;
}

type PoolState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | {
      kind: "ready";
      anchorId: string;
      transactions: readonly ReceiptGroupTransactionCandidate[];
      proposals: readonly ReceiptGroupCandidate[];
      eligibleReceipts: readonly ReceiptSummary[];
      eligibleEvidence: readonly ReceiptSummary[];
      noBankData: boolean;
      fromProposalOnly: boolean;
    };

type SubmitState =
  | { phase: "confirming"; done: number; total: number }
  | { phase: "linking" }
  | null;

function isConflictOrStaleError(result: { status?: number; error?: string }): boolean {
  return result.status === 409
    || result.error === "budget_receipt_conflict"
    || result.error === "budget_receipt_tx_already_linked"
    || result.error === "budget_receipt_stale_source";
}

function settledOutcome(
  result: { ok: boolean; status?: number; error?: string },
  successOutcome: ReceiptSettledEvent["outcome"],
): ReceiptSettledEvent["outcome"] {
  if (result.ok) return successOutcome;
  if (result.status === 404) return "not_found";
  if (isConflictOrStaleError(result)) return "conflict";
  return "error";
}

function preConfirmErrorText(error: string): string {
  switch (error) {
    case "budget_receipt_conflict": return t("budget.receipts.conflict");
    case "budget_receipt_tx_already_linked": return t("budget.receipts.txAlreadyLinked");
    case "budget_receipt_stale_source": return t("budget.receipts.staleSource");
    case "budget_receipt_total_mismatch": return t("budget.receipts.totalMismatch");
    case "budget_receipt_not_found": return t("budget.receipts.notFound");
    case "budget_receipt_in_group": return t("budget.receipts.inGroupConflict");
    default: return t("budget.receipts.confirmError");
  }
}

function groupLinkErrorText(error: string): string {
  switch (error) {
    case "budget_receipt_conflict": return t("budget.receipts.conflict");
    case "budget_receipt_tx_already_linked": return t("budget.receipts.txAlreadyLinked");
    case "budget_receipt_stale_source": return t("budget.receipts.groupStaleSource");
    case "budget_receipt_group_sum_mismatch": return t("budget.receipts.groupSumMismatch");
    case "budget_receipt_invalid_request": return t("budget.receipts.groupInvalidMembers");
    case "budget_receipt_not_found": return t("budget.receipts.notFound");
    default: return t("budget.receipts.groupLinkError");
  }
}

function matchStrengthLabel(strength: ReceiptGroupTransactionCandidate["match_strength"]): string | null {
  switch (strength) {
    case "id": return t("budget.receipts.matchBadgeId");
    case "nip": return t("budget.receipts.matchBadgeNip");
    case "distinctive": return t("budget.receipts.matchBadgeMerchant");
    default: return null;
  }
}

function initialProposalPool(
  anchor: ReceiptSummary,
  proposal: ReceiptGroupCandidate,
): Extract<PoolState, { kind: "ready" }> {
  return {
    kind: "ready",
    anchorId: anchor.receipt_id,
    transactions: [groupTransactionFromProposal(proposal)],
    proposals: [proposal],
    eligibleReceipts: mergeGroupPoolMembers(anchor, proposal.member_receipts),
    eligibleEvidence: proposal.evidence == null ? [] : [proposal.evidence],
    noBankData: false,
    fromProposalOnly: true,
  };
}

function txKey(tx: Pick<ReceiptGroupTransactionCandidate, "account_id" | "entry_reference" | "evidence_fingerprint">): string {
  return `${tx.account_id}\0${tx.entry_reference}\0${tx.evidence_fingerprint}`;
}

export function ReceiptGroupController({
  api,
  store,
  target,
  actorKey,
  accounts,
  connections,
  locale,
  revision,
  onAccessLoss,
  onClose,
  onPartialConfirm,
  onGroupLinked,
}: ReceiptGroupControllerProps) {
  const titleId = useId();
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const mountedRef = useRef(true);
  const actorKeyRef = useRef(actorKey);
  const sessionGenRef = useRef(0);
  const loadGenRef = useRef(0);
  const submittingRef = useRef(false);

  const [poolState, setPoolState] = useState<PoolState>(() => (
    target.kind === "proposal" ? initialProposalPool(target.anchor, target.proposal) : { kind: "loading" }
  ));
  const [selectedTxIndex, setSelectedTxIndex] = useState<number | null>(
    target.kind === "proposal" ? 0 : null,
  );
  const [submitting, setSubmitting] = useState<SubmitState>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [partialNoteCount, setPartialNoteCount] = useState(0);

  useLayoutEffect(() => {
    closeButtonRef.current?.focus();
  }, []);

  useLayoutEffect(() => {
    if (actorKeyRef.current !== actorKey) {
      actorKeyRef.current = actorKey;
      sessionGenRef.current += 1;
      loadGenRef.current += 1;
      submittingRef.current = false;
      setSubmitting(null);
      setActionError(null);
      setPartialNoteCount(0);
      setSelectedTxIndex(null);
      setPoolState({ kind: "loading" });
    }
  }, [actorKey]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      sessionGenRef.current += 1;
      loadGenRef.current += 1;
    };
  }, []);

  const extraSelectedRows = useMemo(
    () => (target.kind === "bulk" ? target.selectedRows : []),
    [target],
  );

  const applyCandidatesResult = useCallback((
    data: ReceiptGroupCandidatesResult,
    preserveTxKey: string | null,
  ) => {
    const mergedMembers = mergeGroupPoolMembers(target.anchor, data.eligible_receipts, extraSelectedRows);
    let nextIdx: number | null = null;
    if (preserveTxKey !== null) {
      const idx = data.transactions.findIndex((tx) => txKey(tx) === preserveTxKey);
      nextIdx = idx >= 0 ? idx : null;
    }
    setPoolState({
      kind: "ready",
      anchorId: data.anchor_receipt_id,
      transactions: data.transactions,
      proposals: data.proposals,
      eligibleReceipts: mergedMembers,
      eligibleEvidence: data.eligible_evidence,
      noBankData: data.no_bank_data === true,
      fromProposalOnly: false,
    });
    setSelectedTxIndex(nextIdx);
  }, [target.anchor, extraSelectedRows]);

  const loadCandidates = useCallback(async (preserveTxKey: string | null = null, keepError = false) => {
    if (api.receiptGroupCandidates === undefined) {
      setPoolState({ kind: "error", message: t("budget.receipts.groupCandidatesLoadError") });
      return;
    }
    const gen = ++loadGenRef.current;
    const expectedActor = actorKey;
    const expectedSession = sessionGenRef.current;
    if (!keepError) {
      setActionError(null);
      setPoolState({ kind: "loading" });
    }
    const result = await api.receiptGroupCandidates(target.anchor.receipt_id);
    if (onAccessLoss(result, { explicitDisabledOnly: true })) return;
    if (!mountedRef.current || actorKeyRef.current !== expectedActor
      || sessionGenRef.current !== expectedSession || loadGenRef.current !== gen) {
      return;
    }
    if (!result.ok) {
      if (!keepError) {
        setPoolState({
          kind: "error",
          message: result.status === 404
            ? t("budget.receipts.notFound")
            : t("budget.receipts.groupCandidatesLoadError"),
        });
      }
      return;
    }
    applyCandidatesResult(result.data, preserveTxKey);
  }, [api, target.anchor.receipt_id, actorKey, onAccessLoss, applyCandidatesResult]);

  useEffect(() => {
    if (submittingRef.current) return;
    if (target.kind === "proposal") {
      setPoolState(initialProposalPool(target.anchor, target.proposal));
      setSelectedTxIndex(0);
      setActionError(null);
      setPartialNoteCount(0);
      return;
    }
    void loadCandidates(null, false);
  }, [target, revision, loadCandidates]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && submitting === null && !submittingRef.current) {
        onClose();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => { document.removeEventListener("keydown", onKeyDown); };
  }, [onClose, submitting]);

  const builderLabels: ReceiptGroupBuilderLabels = useMemo(() => ({
    title: t("budget.receipts.groupBuilderTitle"),
    transaction: t("budget.receipts.groupTransactionLabel"),
    members: t("budget.receipts.groupMembersLabel"),
    anchorReceipt: t("budget.receipts.groupAnchorLabel"),
    memberCount: (selected, min, max) =>
      t("budget.receipts.groupMemberCount", { selected, min, max }),
    noEligibleMembers: t("budget.receipts.groupNoEligibleMembers"),
    remainingDelta: t("budget.receipts.groupRemainingDelta"),
    importedFirst: t("budget.receipts.groupImportedFirst"),
    confirm: t("budget.receipts.groupConfirmAction"),
  }), []);

  const handleConfirmGroup = useCallback(async (selection: ReceiptGroupSelection) => {
    if (submittingRef.current || poolState.kind !== "ready") return;
    if (api.isAuthorizationLocked?.() === true) {
      setActionError(t("budget.main.serverChanged"));
      return;
    }
    if (api.linkReceiptGroup === undefined) {
      setActionError(t("budget.receipts.groupLinkError"));
      return;
    }

    const memberById = new Map(poolState.eligibleReceipts.map((row) => [row.receipt_id, row]));
    const evidenceById = new Map(poolState.eligibleEvidence.map((row) => [row.receipt_id, row]));
    const selectedDates: Array<string | null | undefined> = selection.members.map(
      (member) => memberById.get(member.receipt_id)?.receipt_date,
    );
    if (selection.evidence !== null) {
      selectedDates.push(evidenceById.get(selection.evidence.receipt_id)?.receipt_date);
    }
    if (!isGroupDateSpanValid(selectedDates, GROUP_MAX_SPAN_DAYS)) {
      setActionError(t("budget.receipts.groupDateSpanError"));
      return;
    }

    const participantIds = [
      ...selection.members.map((member) => member.receipt_id),
      ...(selection.evidence === null ? [] : [selection.evidence.receipt_id]),
    ];
    const acquired: Array<{ id: string; token: symbol }> = [];
    for (const id of participantIds) {
      const token = store.tryAcquire(id, "group_link");
      if (token === null) {
        for (const prev of acquired) store.release(prev.id, prev.token);
        setActionError(t("budget.receipts.groupBusyError"));
        return;
      }
      acquired.push({ id, token });
    }

    submittingRef.current = true;
    setActionError(null);
    setPartialNoteCount(0);

    const expectedActor = actorKey;
    const expectedSession = sessionGenRef.current;
    const anchorId = poolState.anchorId;
    const isAlive = () => mountedRef.current
      && actorKeyRef.current === expectedActor
      && sessionGenRef.current === expectedSession;
    const isLocked = () => api.isAuthorizationLocked?.() === true;

    const importedTargets = [
      ...selection.members.filter((item) => item.status === "imported"),
      ...(selection.evidence !== null && selection.evidence.status === "imported" ? [selection.evidence] : []),
    ];
    const confirmedVersions = new Map<string, number>();
    for (const member of selection.members) confirmedVersions.set(member.receipt_id, member.expected_version);
    if (selection.evidence !== null) {
      confirmedVersions.set(selection.evidence.receipt_id, selection.evidence.expected_version);
    }

    let confirmedCount = 0;
    const confirmedIds: string[] = [];
    let settledOutcomeValue: ReceiptSettledEvent["outcome"] | undefined;
    let activeCandidateToken = selection.transaction.candidate_token;

    try {
      for (const [idx, item] of importedTargets.entries()) {
        if (!isAlive() || isLocked()) {
          if (isAlive() && isLocked()) {
            setActionError(t("budget.main.serverChanged"));
            if (confirmedCount > 0) {
              setPartialNoteCount(confirmedCount);
              onPartialConfirm(confirmedIds);
            }
          } else {
            settledOutcomeValue = confirmedCount > 0 ? "confirmed" : undefined;
          }
          return;
        }
        setSubmitting({ phase: "confirming", done: idx + 1, total: importedTargets.length });

        const detailRes = await api.receiptDetail(item.receipt_id);
        if (onAccessLoss(detailRes)) {
          settledOutcomeValue = confirmedCount > 0 ? "confirmed" : "error";
          return;
        }
        if (!isAlive()) {
          settledOutcomeValue = confirmedCount > 0 ? "confirmed" : undefined;
          return;
        }
        if (isLocked()) {
          setActionError(t("budget.main.serverChanged"));
          if (confirmedCount > 0) {
            setPartialNoteCount(confirmedCount);
            onPartialConfirm(confirmedIds);
          }
          return;
        }
        if (!detailRes.ok) {
          settledOutcomeValue = settledOutcome(detailRes, "confirmed");
          setActionError(detailRes.status === 404
            ? t("budget.receipts.notFound")
            : preConfirmErrorText(detailRes.error));
          if (confirmedCount > 0) {
            setPartialNoteCount(confirmedCount);
            onPartialConfirm(confirmedIds);
          }
          return;
        }
        if (detailRes.data.receipt.receipt_id !== item.receipt_id) {
          settledOutcomeValue = "conflict";
          setActionError(t("budget.receipts.conflict"));
          if (confirmedCount > 0) {
            setPartialNoteCount(confirmedCount);
            onPartialConfirm(confirmedIds);
          }
          return;
        }
        const built = buildGroupPreConfirmRequest(
          detailRes.data.receipt,
          item.expected_version,
          t("budget.receipts.summaryLineDefault"),
        );
        if (!built.ok) {
          settledOutcomeValue = built.errorCode === "budget_receipt_conflict" ? "conflict" : "error";
          setActionError(preConfirmErrorText(built.errorCode));
          if (confirmedCount > 0) {
            setPartialNoteCount(confirmedCount);
            onPartialConfirm(confirmedIds);
          }
          if (built.errorCode === "budget_receipt_conflict") {
            void loadCandidates(null, true);
          }
          return;
        }
        if (!isAlive() || isLocked()) {
          if (isAlive() && isLocked()) {
            setActionError(t("budget.main.serverChanged"));
            if (confirmedCount > 0) {
              setPartialNoteCount(confirmedCount);
              onPartialConfirm(confirmedIds);
            }
          }
          return;
        }

        const confirmRes = await api.confirmReceipt(built.request);
        if (onAccessLoss(confirmRes)) {
          settledOutcomeValue = "error";
          return;
        }
        if (!isAlive()) {
          settledOutcomeValue = settledOutcome(confirmRes, "confirmed");
          return;
        }
        if (!confirmRes.ok) {
          settledOutcomeValue = settledOutcome(confirmRes, "confirmed");
          setActionError(preConfirmErrorText(confirmRes.error));
          setPartialNoteCount(confirmedCount);
          // Reload list even on uncertain/network error so any committed confirm stays reflected.
          onPartialConfirm(confirmedCount > 0 ? confirmedIds : participantIds);
          if (isConflictOrStaleError(confirmRes)) {
            void loadCandidates(null, true);
          }
          return;
        }

        confirmedVersions.set(item.receipt_id, confirmRes.data.receipt.version);
        confirmedCount += 1;
        confirmedIds.push(item.receipt_id);
      }

      if (confirmedCount > 0) {
        if (!isAlive() || isLocked()) {
          if (isAlive() && isLocked()) {
            setActionError(t("budget.main.serverChanged"));
            setPartialNoteCount(confirmedCount);
            onPartialConfirm(confirmedIds);
          } else {
            settledOutcomeValue = "confirmed";
          }
          return;
        }
        if (api.receiptGroupCandidates === undefined) {
          setActionError(t("budget.receipts.groupCandidatesLoadError"));
          setPartialNoteCount(confirmedCount);
          onPartialConfirm(confirmedIds);
          return;
        }

        const refreshed = await api.receiptGroupCandidates(anchorId);
        if (onAccessLoss(refreshed, { explicitDisabledOnly: true })) {
          settledOutcomeValue = "confirmed";
          return;
        }
        if (!isAlive()) {
          settledOutcomeValue = "confirmed";
          return;
        }
        if (isLocked()) {
          setActionError(t("budget.main.serverChanged"));
          setPartialNoteCount(confirmedCount);
          onPartialConfirm(confirmedIds);
          return;
        }
        if (!refreshed.ok) {
          setActionError(t("budget.receipts.groupCandidatesLoadError"));
          setPartialNoteCount(confirmedCount);
          onPartialConfirm(confirmedIds);
          return;
        }

        const matchedTx = refreshed.data.transactions.find(
          (tx) => tx.account_id === selection.transaction.account_id
            && tx.entry_reference === selection.transaction.entry_reference,
        );
        const txTermsUnchanged = matchedTx !== undefined
          && matchedTx.amount === selection.transaction.amount
          && matchedTx.currency === selection.transaction.currency
          && matchedTx.credit_debit_indicator === selection.transaction.credit_debit_indicator;
        if (!txTermsUnchanged) {
          applyCandidatesResult(refreshed.data, null);
          setActionError(t("budget.receipts.bankEvidenceChanged"));
          setPartialNoteCount(confirmedCount);
          onPartialConfirm(confirmedIds);
          return;
        }

        const refreshedMembers = new Map(refreshed.data.eligible_receipts.map((row) => [row.receipt_id, row]));
        const refreshedEvidence = new Map(refreshed.data.eligible_evidence.map((row) => [row.receipt_id, row]));
        const memberVersionDrifted = selection.members.some((member) => {
          const fresh = refreshedMembers.get(member.receipt_id);
          return fresh !== undefined && fresh.version !== confirmedVersions.get(member.receipt_id);
        });
        const evidenceRow = selection.evidence === null ? undefined : refreshedEvidence.get(selection.evidence.receipt_id);
        const evidenceDrifted = selection.evidence !== null && evidenceRow !== undefined && (
          evidenceRow.version !== confirmedVersions.get(selection.evidence.receipt_id)
          || positiveGroupFee(evidenceRow.fee_amount) !== selection.evidence.fee_amount
        );
        if (memberVersionDrifted || evidenceDrifted) {
          applyCandidatesResult(refreshed.data, txKey(matchedTx));
          setActionError(t(evidenceDrifted ? "budget.receipts.bankEvidenceChanged" : "budget.receipts.conflict"));
          setPartialNoteCount(confirmedCount);
          onPartialConfirm(confirmedIds);
          return;
        }
        activeCandidateToken = matchedTx.candidate_token;
      }

      if (!isAlive() || isLocked()) {
        if (isAlive() && isLocked()) {
          setActionError(t("budget.main.serverChanged"));
          if (confirmedCount > 0) {
            setPartialNoteCount(confirmedCount);
            onPartialConfirm(confirmedIds);
          }
        } else if (confirmedCount > 0) {
          settledOutcomeValue = "confirmed";
        }
        return;
      }

      setSubmitting({ phase: "linking" });
      const linkRequest = buildGroupLinkRequest({
        transaction: {
          account_id: selection.transaction.account_id,
          entry_reference: selection.transaction.entry_reference,
          candidate_token: activeCandidateToken,
        },
        members: selection.members.map((member) => ({
          receipt_id: member.receipt_id,
          expected_version: confirmedVersions.get(member.receipt_id) ?? member.expected_version,
        })),
        evidence: selection.evidence === null ? null : {
          receipt_id: selection.evidence.receipt_id,
          expected_version: confirmedVersions.get(selection.evidence.receipt_id) ?? selection.evidence.expected_version,
          fee_amount: selection.evidence.fee_amount,
        },
      });

      const linkRes = await api.linkReceiptGroup(linkRequest);
      if (onAccessLoss(linkRes, { explicitDisabledOnly: true })) {
        settledOutcomeValue = "error";
        return;
      }
      if (!isAlive()) {
        settledOutcomeValue = settledOutcome(linkRes, "group_linked");
        return;
      }
      if (!linkRes.ok) {
        settledOutcomeValue = settledOutcome(linkRes, "group_linked");
        setActionError(groupLinkErrorText(linkRes.error));
        if (confirmedCount > 0) setPartialNoteCount(confirmedCount);
        onPartialConfirm(confirmedCount > 0 ? confirmedIds : participantIds);
        if (isConflictOrStaleError(linkRes) && api.receiptGroupCandidates !== undefined) {
          void loadCandidates(null, true);
        }
        return;
      }

      settledOutcomeValue = undefined;
      onGroupLinked(linkRes.data, participantIds);
    } finally {
      submittingRef.current = false;
      if (mountedRef.current) setSubmitting(null);
      const notifyOutcome = isAlive() ? undefined : settledOutcomeValue;
      for (const { id, token } of acquired) {
        store.release(id, token, notifyOutcome);
      }
    }
  }, [
    api,
    actorKey,
    poolState,
    store,
    onAccessLoss,
    onPartialConfirm,
    onGroupLinked,
    applyCandidatesResult,
    loadCandidates,
  ]);

  const activeTransaction = poolState.kind === "ready" && selectedTxIndex !== null
    ? (poolState.transactions[selectedTxIndex] ?? null)
    : null;
  const builderMembers = useMemo(() => {
    if (poolState.kind !== "ready") return [];
    return mergeGroupPoolMembers(target.anchor, poolState.eligibleReceipts, extraSelectedRows);
  }, [poolState, target.anchor, extraSelectedRows]);

  return (
    <div
      className="modal-overlay"
      onClick={(event) => {
        if (event.target === event.currentTarget && submitting === null && !submittingRef.current) {
          onClose();
        }
      }}
    >
      <div
        className="modal-card budget-receipt-group-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >
        <header className="budget-receipt-drawer-head">
          <h3 id={titleId}>{t("budget.receipts.groupBuilderTitle")}</h3>
          <button
            ref={closeButtonRef}
            type="button"
            disabled={submitting !== null}
            onClick={onClose}
          >
            {t("budget.main.close")}
          </button>
        </header>

        {poolState.kind === "loading" ? (
          <p className="budget-muted" role="status">{t("budget.receipts.detailLoading")}</p>
        ) : poolState.kind === "error" ? (
          <div className="budget-notice budget-entries-action" role="alert">
            <span>{poolState.message}</span>
            {api.receiptGroupCandidates === undefined ? null : (
              <button type="button" onClick={() => { void loadCandidates(null, false); }}>
                {t("budget.receipts.retry")}
              </button>
            )}
          </div>
        ) : (
          <div className="budget-receipt-drawer-body">
            {poolState.fromProposalOnly && api.receiptGroupCandidates !== undefined ? (
              <div className="budget-actions">
                <button
                  type="button"
                  disabled={submitting !== null}
                  onClick={() => { void loadCandidates(null, false); }}
                >
                  {t("budget.receipts.groupShowAllCandidates")}
                </button>
              </div>
            ) : null}

            {poolState.transactions.length === 0 ? (
              <p className="budget-muted" role="status">
                {poolState.noBankData
                  ? t("budget.receipts.noBankDataPeriod")
                  : t("budget.receipts.groupNoTransactions")}
              </p>
            ) : (
              <>
                <fieldset className="budget-receipt-link-choices">
                  <legend>{t("budget.receipts.groupSelectTransaction")}</legend>
                  {poolState.transactions.map((tx, index) => {
                    const accountText = receiptAccountLabel(accounts, connections, tx.account_id, t("budget.main.account"));
                    const { primaryDate, bookingDayMonth, displayDistance } = suggestedCandidateDates(tx, target.anchor);
                    const baseDate = primaryDate ?? t("budget.receipts.noDate");
                    const dateText = bookingDayMonth === null
                      ? baseDate
                      : `${baseDate} ${t("budget.receipts.bookingDateNote", { date: bookingDayMonth })}`;
                    const daysText = displayDistance === 0
                      ? t("budget.receipts.candidateSameDay")
                      : t("budget.receipts.candidateDays", { days: displayDistance });
                    const amountText = formatAmount(tx.amount, tx.currency, locale, tx.credit_debit_indicator);
                    const badge = matchStrengthLabel(tx.match_strength);
                    return (
                      <label key={`${tx.account_id}:${String(index)}`} className="budget-toggle budget-receipt-candidate">
                        <input
                          type="radio"
                          name="group_tx_choice"
                          value={String(index)}
                          checked={selectedTxIndex === index}
                          disabled={submitting !== null}
                          onChange={() => {
                            setSelectedTxIndex(index);
                            setActionError(null);
                          }}
                        />
                        <span className="budget-receipt-group-tx-row">
                          <span className="budget-muted">{dateText}</span>
                          <span>{accountText} ({daysText})</span>
                          {badge === null ? null : (
                            <span
                              className="budget-chip budget-receipt-match-badge"
                              data-match-strength={tx.match_strength}
                            >
                              {badge}
                            </span>
                          )}
                          {tx.merchant_token_hit ? (
                            <span className="budget-chip">{t("budget.receipts.candidateMerchantMatch")}</span>
                          ) : null}
                          <strong className={tx.credit_debit_indicator === "DBIT" ? "is-debit" : "is-credit"}>
                            {amountText}
                          </strong>
                        </span>
                      </label>
                    );
                  })}
                </fieldset>

                {activeTransaction === null ? null : (
                  <>
                    {builderMembers.length < 2 ? (
                      <p className="budget-muted" role="note">{t("budget.receipts.groupNeedTwoMembers")}</p>
                    ) : null}
                    <ReceiptGroupBuilder
                      pool={{
                        transaction: activeTransaction,
                        anchor_receipt_id: poolState.anchorId,
                        members: builderMembers,
                        evidence: poolState.eligibleEvidence,
                      }}
                      labels={builderLabels}
                      locale={locale}
                      accountLabel={receiptAccountLabel(
                        accounts,
                        connections,
                        activeTransaction.account_id,
                        t("budget.main.account"),
                      )}
                      disabled={submitting !== null}
                      onConfirm={(selection) => { void handleConfirmGroup(selection); }}
                    />
                  </>
                )}
              </>
            )}

            {submitting === null ? null : (
              <p className="budget-notice budget-receipt-group-progress" role="status">
                {submitting.phase === "confirming"
                  ? t("budget.receipts.groupConfirmingProgress", {
                      done: submitting.done,
                      total: submitting.total,
                    })
                  : t("budget.receipts.linkingGroup")}
              </p>
            )}

            {actionError === null ? null : (
              <div className="budget-notice" role="alert">
                <p>{actionError}</p>
                {partialNoteCount > 0 ? (
                  <p className="budget-muted">
                    {t("budget.receipts.groupPartialConfirmNote", { count: partialNoteCount })}
                  </p>
                ) : null}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
