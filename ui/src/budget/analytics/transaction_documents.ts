import { useEffect, useMemo, useRef, useState } from "react";
import type { ReceiptsApi } from "../receipts_api";
import type { ReceiptSummary } from "../receipts_model";
import { transactionRowKey } from "./model";
import type { AnalyticsTransaction } from "./model";

type ReceiptsList = ReceiptsApi["receiptsList"];
type DocumentIndex = ReadonlyMap<string, readonly ReceiptSummary[]>;
type RowIdentity = [accountId: string, entryReference: string];

export interface TransactionDocuments {
  state: "loading" | "ready" | "unavailable";
  byRow: DocumentIndex;
}

const NO_DOCUMENTS: DocumentIndex = new Map();
const ENTRY_DIGEST = /^[0-9a-f]{64}$/;

async function readLinkedReceipts(read: ReceiptsList, active: () => boolean): Promise<DocumentIndex | null> {
  const records = new Map<string, ReceiptSummary[]>();
  const seenReceipts = new Set<string>();
  const seenCursors = new Set<string>();
  let cursor: string | null = null;
  do {
    const result = await read({ linked: true, limit: 200, ...(cursor === null ? {} : { cursor }) });
    if (!active() || !result.ok) return null;

    for (const receipt of result.data.receipts) {
      const account = receipt.linked_account_id;
      const reference = receipt.linked_entry_ref;
      // Missing identifiers can mean that match permission changed during this load.
      if (!receipt.linked || !account || !reference || !ENTRY_DIGEST.test(reference)
          || seenReceipts.has(receipt.receipt_id)) return null;
      seenReceipts.add(receipt.receipt_id);
      const key = transactionRowKey({ account_id: account, entry_reference: reference });
      const existing = records.get(key);
      if (existing === undefined) records.set(key, [receipt]);
      else existing.push(receipt);
    }

    cursor = result.data.next_cursor;
    if (cursor !== null) {
      if (seenCursors.has(cursor)) return null;
      seenCursors.add(cursor);
    }
  } while (cursor !== null);
  return records;
}

async function matchRows(
  index: DocumentIndex, rows: RowIdentity[], subtle: SubtleCrypto, hashes: Map<string, Promise<string>>,
): Promise<DocumentIndex> {
  const encoder = new TextEncoder();
  const matches = await Promise.all(rows.map(async ([account, reference]) => {
    // The receipts API exposes SHA-256 of the exact UTF-8 provider reference.
    let hash = hashes.get(reference);
    if (hash === undefined) {
      hash = subtle.digest("SHA-256", encoder.encode(reference)).then((bytes) =>
        Array.from(new Uint8Array(bytes), (byte) => byte.toString(16).padStart(2, "0")).join(""));
      hashes.set(reference, hash);
    }
    const digest = await hash;
    const documents = index.get(transactionRowKey({ account_id: account, entry_reference: digest }));
    const key = transactionRowKey({ account_id: account, entry_reference: reference });
    return [key, documents] as const;
  }));
  const byRow = new Map<string, readonly ReceiptSummary[]>();
  for (const [key, documents] of matches) {
    if (documents !== undefined) byRow.set(key, documents);
  }
  return byRow;
}

export function useTransactionDocuments(
  receiptsList: ReceiptsList | undefined, rows: readonly AnalyticsTransaction[], revision: number,
): TransactionDocuments {
  // SubtleCrypto is absent outside secure contexts.
  const browserCrypto = globalThis.crypto as Partial<Crypto> | undefined;
  const subtle = browserCrypto?.subtle;
  const enabled = receiptsList !== undefined && subtle !== undefined;
  const hasRows = rows.length > 0;
  const request = useMemo(() => enabled && hasRows ? { receiptsList, revision } : null,
    [enabled, hasRows, receiptsList, revision]);
  const [listing, setListing] = useState<{ request: typeof request; index: DocumentIndex | null } | null>(null);

  useEffect(() => {
    if (request === null) return undefined;
    let active = true;
    const finish = (index: DocumentIndex | null) => { if (active) setListing({ request, index }); };
    void readLinkedReceipts(request.receiptsList, () => active).then(finish, () => { finish(null); });
    return () => { active = false; };
  }, [request]);

  const index = listing?.request === request ? listing.index : undefined;
  // A row's annotation can change without changing the transaction's identity.
  const rowIdentities = JSON.stringify(rows.map(({ account_id, entry_reference }) => [account_id, entry_reference]));
  const [matches, setMatches] = useState<{ index: DocumentIndex; rows: string; byRow: DocumentIndex | null } | null>(null);
  const digestCache = useRef<{ index: DocumentIndex; subtle: SubtleCrypto; hashes: Map<string, Promise<string>> } | null>(null);

  useEffect(() => {
    if (index === undefined || index === null || subtle === undefined) {
      digestCache.current = null;
      return undefined;
    }
    if (digestCache.current?.index !== index || digestCache.current.subtle !== subtle) {
      digestCache.current = { index, subtle, hashes: new Map() };
    }
    let active = true;
    const finish = (byRow: DocumentIndex | null) => { if (active) setMatches({ index, rows: rowIdentities, byRow }); };
    void matchRows(index, JSON.parse(rowIdentities) as RowIdentity[], subtle, digestCache.current.hashes)
      .then(finish, () => { finish(null); });
    return () => { active = false; };
  }, [index, rowIdentities, subtle]);

  if (!enabled || index === null) return { state: "unavailable", byRow: NO_DOCUMENTS };
  if (!hasRows) return { state: "ready", byRow: NO_DOCUMENTS };
  if (matches?.index !== index || matches?.rows !== rowIdentities) {
    return { state: "loading", byRow: NO_DOCUMENTS };
  }
  return matches.byRow === null ? { state: "unavailable", byRow: NO_DOCUMENTS } : { state: "ready", byRow: matches.byRow };
}
