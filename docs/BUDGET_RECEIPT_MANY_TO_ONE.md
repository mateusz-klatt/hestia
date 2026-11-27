# Budget Receipts: Many-to-One Transaction Linking

> **Status:** Design spec (no code, migration, or OpenAPI edits).  
> **Baseline:** `origin/master` at `4a0ee02` (schema `0032`, OpenAPI `0.55.0`). Assumes pending **PR #300** (`feat/budget-receipt-id-matching`: ID-first matching, `card_last4`/`blik_ref_hmac`, contract `0.56.0`, migration `0033`) merges first. Both the migration revision and the contract minor version are **assigned at implementation** (expected migration chain: `0033` idmatch → `0034` appointments → `0035` this feature; contract takes the next minor after `master` at implementation time).

---

## 1. Context, Use Cases & Counter-Cases

Today `budget_receipts` enforces **1:1** bank transaction linking via `linked_account_id`, `linked_entry_ref`, `linked_fingerprint`, `linked_evidence`, and the partial unique index `uq_budget_receipts_tx_link` (`hestia/db.py`, migration `0022_budget_receipts.py`). Both `hestia/budget/receipt_store.py` (`_candidates`, `_verify_link`, `confirm`) and `hestia/budget/receipt_reconcile.py` (`reconcile_receipt_links`) assume at most one `confirmed` or `needs_review` receipt per `(account_id, entry_reference)`. Meanwhile, v1f (`docs/GMAIL_PAYMENT_V1F.md`, migrations `0031`/`0032`) introduced `evidence_for_receipt_id` so gateway payment emails attach 1:1 to a shop document and drop out of matching (`has_document=False` when standalone).

### In-scope use cases

1. **Apple / App Store monthly billing:** Multiple App Store / iCloud+ email receipts or invoices from one billing batch settle as one `APPLE.COM/BILL` bank debit (`sum(R_i.total_amount) == tx.amount`).
2. **One shop order with split receipts:** One online or retail checkout fulfilled across multiple shipments produces 2+ fiscal receipts or invoices settled by a single debit.
3. **Allegro multi-seller checkout with one PayU charge:** One `PAYU` debit covers $N$ seller order confirmations/invoices (`D1..DN`) plus an optional standalone PayU `payment_confirmation` email (`G`, left unattached by v1f because each `D_i.total < G.total`).

### Explicit counter-cases (excluded from M:1)

- **Refunds & mixed signs:** `'purchase'` and `'refund'` receipts never mix in one group to net against a debit. Every member must share `receipt_kind` (`purchase -> DBIT`, or `refund -> CRDT` for multi-receipt refunds).
- **Partial refunds:** Returning one item from a multi-receipt checkout creates a separate `CRDT` bank transaction; it must never auto-shrink or rebalance a linked `DBIT` group, and partial sums (`sum < tx.amount`) are never valid links.
- **FX differences:** Cross-currency receipts (e.g. `EUR`/`USD` emails against a `PLN` bank debit) or differing conversion spreads are rejected; every member must match `tx.currency` exactly.
- **Unbacked / guessed fees:** Arbitrary rounding deltas or bank FX spreads never bridge a mismatch. A fee bridges the sum **only** when explicitly backed by an extracted, persisted `fee_amount >= 0` on a standalone gateway `evidence_receipt_id` row (`evidence.total_amount == tx.amount`).

---

## 2. Data Model Options & Recommendation

### Option A: Relax `uq_budget_receipts_tx_link` with a `link_group_id` on `budget_receipts`

Drop `uq_budget_receipts_tx_link`, add `link_group_id` (and fee/evidence pointers) to `budget_receipts`, and allow multiple receipt rows to carry the same `(linked_account_id, linked_entry_ref)`.

- **Why rejected:**
  1. **Weak uniqueness:** A non-unique index on `(linked_account_id, linked_entry_ref)` cannot enforce in SQLite that all rows pointing to a transaction share one `link_group_id` without adding a parent table anyway.
  2. **Hazardous SQLite table rebuild:** Dropping `uq_budget_receipts_tx_link` or changing `ck_budget_receipts_link_fields` requires `batch_alter_table("budget_receipts")` while child FKs point to `budget_receipts` (`budget_receipt_lines`, `budget_gmail_messages`, self-FK `evidence_for_receipt_id` — the hazard noted in `0031`), and breaks the immutable index snapshot in `tests/test_budget_receipts_0024.py`.
  3. **Denormalized drift & lock fan-out:** Duplicating `linked_fingerprint`, `linked_evidence`, and group fee across $N$ rows creates multi-row version/audit fan-out and partial-update bugs.

### Option B: Dedicated link tables alongside untouched 1:1 `budget_receipts` (Recommended)

Keep `budget_receipts.linked_*` and `uq_budget_receipts_tx_link` untouched for 1:1 links, and store M:1 links (`k >= 2`) in two normalized tables:

```sql
CREATE TABLE budget_receipt_link_groups (
    id                      TEXT PRIMARY KEY,
    owner                   TEXT NOT NULL REFERENCES users(username),
    account_id              TEXT NOT NULL,
    entry_reference         TEXT NOT NULL,
    currency                TEXT NOT NULL CHECK (length(currency) = 3 AND currency GLOB '[A-Z][A-Z][A-Z]'),
    tx_amount               TEXT NOT NULL CHECK (length(tx_amount) BETWEEN 1 AND 80),
    credit_debit_indicator  TEXT NOT NULL CHECK (credit_debit_indicator IN ('DBIT', 'CRDT')),
    fee_amount              TEXT NULL CHECK (fee_amount IS NULL OR (length(fee_amount) BETWEEN 1 AND 80 AND fee_amount NOT GLOB '-*')),
    evidence_receipt_id     TEXT NULL,
    status                  TEXT NOT NULL CHECK (status IN ('confirmed', 'needs_review')),
    linked_fingerprint      TEXT NOT NULL,
    linked_evidence         TEXT NOT NULL CHECK (length(linked_evidence) BETWEEN 1 AND 4096),
    version                 INTEGER NOT NULL CHECK (version >= 1),
    created_at              REAL NOT NULL,
    updated_at              REAL NOT NULL,
    UNIQUE (id, owner),
    CONSTRAINT uq_budget_receipt_link_groups_tx UNIQUE (account_id, entry_reference),
    FOREIGN KEY (account_id, entry_reference) REFERENCES budget_transactions (account_id, entry_reference),
    FOREIGN KEY (account_id, owner) REFERENCES budget_accounts (id, owner),
    FOREIGN KEY (evidence_receipt_id, owner) REFERENCES budget_receipts (id, owner)
);
CREATE UNIQUE INDEX uq_budget_receipt_link_groups_evidence
    ON budget_receipt_link_groups (evidence_receipt_id) WHERE evidence_receipt_id IS NOT NULL;
CREATE INDEX ix_budget_receipt_link_groups_owner_status
    ON budget_receipt_link_groups (owner, status, id);

CREATE TABLE budget_receipt_link_members (
    group_id    TEXT NOT NULL,
    receipt_id  TEXT PRIMARY KEY,
    owner       TEXT NOT NULL,
    FOREIGN KEY (group_id, owner) REFERENCES budget_receipt_link_groups (id, owner),
    FOREIGN KEY (receipt_id, owner) REFERENCES budget_receipts (id, owner)
);
CREATE INDEX ix_budget_receipt_link_members_group ON budget_receipt_link_members (group_id);
```

- **Mutual-exclusion invariants (enforced inside `BEGIN IMMEDIATE` + SQLite `BEFORE INSERT/UPDATE` triggers):**
  1. A transaction `(account_id, entry_reference)` cannot simultaneously exist in `budget_receipt_link_groups`, have an active 1:1 link in `budget_receipts`, or appear in `budget_transfer_legs`.
  2. A receipt cannot simultaneously have `linked_account_id IS NOT NULL`, appear in `budget_receipt_link_members`, or appear as `evidence_receipt_id` on a group.
  3. Only `status IN ('confirmed', 'needs_review')` receipts with `evidence_for_receipt_id IS NULL` may exist in `budget_receipt_link_members` (`gateway IS NULL` shop/document rows — though another gateway row may point *to* a member via v1f `evidence_for_receipt_id`).
  4. **Group evidence exclusivity:** A receipt referenced by `budget_receipt_link_groups.evidence_receipt_id` MUST be `confirmed` (or `needs_review` once its group latches), MUST have `gateway IS NOT NULL`, `linked_account_id IS NULL`, and **MUST NOT** have `evidence_for_receipt_id` set (and must not appear in `budget_receipt_link_members`). Conversely, SQLite triggers on `budget_receipts` reject setting `evidence_for_receipt_id IS NOT NULL`, `linked_account_id IS NOT NULL`, `gateway IS NULL`, or `status IN ('draft', 'imported')` on any receipt referenced by `budget_receipt_link_groups.evidence_receipt_id` — making v1f attachment and group evidence mutually exclusive at the DB layer.
- **Justification:** Zero DDL mutations to `budget_receipts` (`0024` snapshot tests and `0031`/`0032` triggers stay untouched), SQLite-enforced 1-group-per-tx and 1-group-per-receipt uniqueness, composite `(id, owner)` FKs with no `ON DELETE CASCADE`, and a single CAS `version` + single `linked_fingerprint`/`linked_evidence` for the whole bank charge.

---

## 3. Sum-Matching Rule & Bounded Candidate Proposal

### Exact sum-matching invariants

All monetary comparisons use exact scaled-integer arithmetic (`analytics_core.canonical_money`, `sum_money`, `_parts`; never floats):

- **Cardinality:** `2 <= k <= MAX_GROUP_SIZE` (`MAX_GROUP_SIZE = 8`).
- **Member eligibility (for persistence):** Every member $r_i \in R$ belongs to `owner`, has `status == 'confirmed'`, `payment_method not in ('cash', 'mixed')`, `total_amount > 0`, `gateway IS NULL`, same `currency == tx.currency`, uniform `receipt_kind` (`purchase -> DBIT`, `refund -> CRDT`), `linked_account_id IS NULL`, not in `budget_receipt_link_members`, and `evidence_for_receipt_id IS NULL`. Note that a member $r_i$ *may* itself be the **target** of another gateway row's v1f `evidence_for_receipt_id` pointer; that attached v1f gateway row has `evidence_for_receipt_id IS NOT NULL` and stays strictly outside $R$.
  - *Proposal vs confirmation:* Candidate **proposal** (`detail` / receipt-anchored `group/candidates`) also considers unlinked `status == 'imported'` receipts (`payment_method` may be `NULL`, as inserted by `gmail_store._invoice`/`_payment`), since Apple and Allegro emails arrive as `imported`. **Persisting** a group requires every member (and any `evidence_receipt_id`) to be `'confirmed'` so `ck_budget_receipts_confirmed_fields` and line-math checks remain owned by `confirm`.
  - *Card scoping (#300):* If any member or attached `evidence_receipt_id` carries `card_last4` mapped in `HESTIA_BUDGET_CARD_ACCOUNTS`, `tx.account_id` must fall within the intersection of those mapped enabled accounts.
- **Gateway evidence isolation:** Gateway rows (`gateway IS NOT NULL`) enter a group **only** as `evidence_receipt_id`, never inside the summed member set $R$. For Allegro/PayU, seller documents form $R$ and the standalone PayU email (`status == 'confirmed'`, `evidence_for_receipt_id IS NULL`) attaches as `evidence_receipt_id`.
- **Date window:** Every member independently passes `_date_distance(tx_payload, r_i.receipt_date, document_kind=r_i.document_kind, due_date=r_i.due_date) is not None` (`±4` days across `transaction_date`/`booking_date`/`value_date` for receipts/confirmations; `[receipt_date - 1, max(due_date, receipt_date) + 14]` for invoices), and `max(r_date) - min(r_date) <= 14` days across the group. *(If Apple consolidated billing requires a wider pre-debit cycle, only same-merchant proposal windows may widen up to a hard `14`-day cap, re-verified inside `BEGIN IMMEDIATE`.)*
- **Exact sum identity (the only two valid identities):**
  1. `sum_money(r_i.total_amount for r_i in R) == tx_amount` (`fee_amount = None`);
  2. `sum_money(r_i.total_amount for r_i in R) + evidence.fee_amount == tx_amount`, where `evidence` is the group's attached `evidence_receipt_id` with `evidence.fee_amount > 0`, `evidence.currency == tx.currency`, and `evidence.total_amount == tx_amount` (gateway surcharge on top of seller receipts).

### Bounded candidate proposal (no combinatorial explosion)

Running unconstrained subset-sum across all unlinked receipts and transactions is $O(M \cdot 2^N)$ and produces false coincidences. Proposal uses two-stage narrowing inside the `_read()` snapshot:

1. **Transaction pre-filter (`MAX_GROUP_TX_CANDIDATES = 10`):** From `_candidate_rows` (additionally excluding `budget_receipt_link_groups`), keep unlinked `BOOK` rows in `_scope_accounts(row)` with matching `currency`, direction, `tx_amount > row.total_amount`, and valid `_date_distance`. Rank by `(id_hit DESC, merchant_token_hit DESC, date_distance ASC)` and inspect at most `10` transactions.
   - **ID-first pinning (#300):** Run `_unique_id_matches` on the anchor receipt and on any unattached standalone gateway row (`gateway IS NOT NULL`, `evidence_for_receipt_id IS NULL`) in the same date window with `total_amount == tx_amount`. An ID match pins that exact bank transaction first.
2. **Merchant / gateway bucket isolation (`MAX_GROUP_POOL = 24`):** Never subset-sum across unrelated shops. For transaction $T$, pool only eligible unlinked non-evidence peers (`gateway IS NULL`, `evidence_for_receipt_id IS NULL`) in $T$'s date window and currency/direction sharing **at least one affinity signal** with the anchor or $T$:
   - same `shop_id`, `raw_nip`, `chain_key`, `_payee_key(raw_shop_name)`, `payee_host`, or `forwarded_sender_domain`; OR
   - shared gateway checkout affinity (an ID-matched standalone gateway evidence row $G$ for $T$, or bank remittance matching a gateway/`APPLE.COM/BILL` token paired with matching-domain/merchant receipts in the `±4`d window).
   - Sort deterministically by `(affinity_rank, date_distance, total_amount DESC, receipt_id)` and cap at `24` peers.
3. **Bounded subset-sum solver:**
   - **Fast paths:** test (a) full same-day/same-merchant bucket (`len >= 2`), then (b) exact 2-receipt pairs ($O(|R_{\text{pool}}|)$ hash lookup).
   - **Pruned DFS (`2 <= k <= 8`):** sort minor-unit integers descending with suffix sums; prune whenever `running > target` or `running + suffix < target`; stop at **`MAX_SOLVER_STEPS = 2_000`** nodes and emit at most **`MAX_GROUP_PROPOSALS = 5`** groups.

---

## 4. Interaction with ID Matching (#300) and Preselection (#294)

| Match type | Response field | Auto-preselected (#294)? | Bulk ID auto-linked (#300)? | Confirmation rule |
| --- | --- | --- | --- | --- |
| 1:1 `"id"` (#300) | `candidates[]` | Yes (sole strong match) | Yes (when `linkById: true`) | One-click / bulk confirm |
| 1:1 `"nip"` / `"distinctive"` (#294) | `candidates[]` | Yes (sole strong match) | No (`link: null` in bulk) | Confirm drawer selection |
| M:1 group (even with `"id"` gateway pin) | `group_candidates[]` | **NEVER** | **NEVER** | Explicit group confirm |

- **Wire separation:** `group_candidates[]` never mixes into 1:1 `candidates[]`, so #294's `preselectedCandidate` and #300's `uniqueIdCandidate` (`buildBulkConfirmRequest`) never see or auto-link an M:1 group.
- **1:1 precedence & #300 boost:** Strong 1:1 matches always take priority. When no 1:1 match fits, #300's `gateway_ref_hmac`/`blik_ref_hmac` (on a standalone PayU/gateway evidence row) and `card_last4` account scoping pin the target bank transaction (`match_strength = "id"` on `group_candidates`), while still requiring explicit owner confirmation.

---

## 5. Reconcile Semantics & Single-Member Mutations

### Bank transaction changes (`reconcile_receipt_links`)

`BudgetStore.commit_snapshot` and `BudgetStore.import_snapshot` (`hestia/budget/store.py`) reconcile groups inside `BEGIN IMMEDIATE`.

1. Load active `BudgetReceiptLinkGroup` rows (`status == 'confirmed'`) for `owner`, batch-loading `(account_id, entry_reference)` transactions and `budget_transfer_legs`.
2. Recompute `current_fp = _fingerprint(transaction)` (`None` if missing, `account/connection` disabled, `uncertain=True`, non-`BOOK`, or reserved in `budget_transfer_legs`) and canonical group `linked_evidence` (bank evidence digests + `current_fp` + sorted member `(receipt_id, total_amount, raw_shop_name, raw_nip)` + `fee_amount` + `evidence_receipt_id`).
3. On any mismatch, latch **`group.status = 'needs_review'`** (`group.version += 1`, `updated_at = now`) **and** latch every member `BudgetReceipt.status = 'needs_review'` (and any attached `evidence_receipt_id`, bumping `version += 1`, `updated_at = now`) and write sanitized `source_changed` audit rows. Just like 1:1 (`receipt_reconcile.py:233`), `needs_review` is a one-way latch: in **v1**, the owner resolves it via `group/unlink` + `group/link` (and in **v2**, optionally via `group/reconfirm`).

`BudgetAnalyticsStore.decide_transfer` checks group reservations inside the same `BEGIN IMMEDIATE` transaction that would create the transfer legs. If either transaction is reserved by a `confirmed` or `needs_review` group, accepting the transfer returns `409` before any transfer leg, decision, version bump, or audit row is written. The owner can explicitly unlink the group and retry. The reciprocal triggers still prevent overlap on direct writes. Snapshot reconciliation handles any pre-existing inconsistent overlap defensively.

### Single-receipt unlink, edit, or delete

Because every receipt has `total_amount > 0`, dropping one member always drops the sum below `tx_amount`. A partial group is **never** left `'confirmed'`:

- **Group unlink (`POST .../group/unlink`):** Requires `budget.receipts.edit` + `budget.receipts.match` and `expected_version`. Atomically deletes the `budget_receipt_link_members` and `budget_receipt_link_groups` rows, restores all members (and any attached `evidence_receipt_id`) to `status = 'confirmed'` with `linked = False`, bumps versions, and logs sanitized `group_unlinked` audit rows.
- **Direct `confirm` / `delete` on a grouped receipt (or attached `evidence_receipt_id`):**
  - `POST /confirm` changing any matching field (`total_amount`, `currency`, `receipt_date`, `receipt_kind`, `payment_method`) or `link` on a grouped receipt (or `evidence_receipt_id`) returns **`409 budget_receipt_in_group`** (in v1, owner runs `group/unlink` first; v2 adds `group/replace`).
  - `POST /delete` on a grouped receipt (or `evidence_receipt_id`) returns **`409 budget_receipt_in_group`** unless `dissolve_group: true` + `expected_group_version` (and `budget.receipts.match`) are provided, in which case the server atomically dissolves the group (restoring surviving members and any `evidence_receipt_id` to `confirmed` unlinked) before tombstoning and deleting the target receipt.
  - Gmail `reset_unconfirmed_payments` and `_purge` never touch grouped receipts or attached `evidence_receipt_id` (all are `'confirmed'`/`'needs_review'` and explicitly excluded from reset queries). Likewise, `gmail_store._attach_evidence` / `_attach_gateway_for_document` refuse attaching `G` via `evidence_for_receipt_id` if `G.id` is already referenced by `budget_receipt_link_groups.evidence_receipt_id`.

---

## 6. API Contract & Migration

### Additive, version-gated API (`X-Hestia-Contract-Version`)

The contract minor version is **assigned at implementation** (next minor after `master` at merge time). Bump `api_contract.CONTRACT_VERSION` to that minor and add `_MANY_TO_ONE_VERSION` in `hestia/budget/receipt_api.py`:

- **Additive response fields (stripped by `_response` when client version `<` assigned minor):**
  - `BudgetReceiptSummary` / `BudgetReceiptDetail`: `link_group: BudgetReceiptLinkGroupRef | None = None` (`group_id`, `group_status`, `group_version`, `member_count`, `fee_amount`, `evidence_receipt_id`).
  - `BudgetReceiptDetail`: `group_members: list[BudgetReceiptGroupMember] = []`.
  - `BudgetReceiptDetailResult`: `group_candidates: list[BudgetReceiptGroupCandidate] = []` (sealed `candidate_token`, digested `entry_reference`, `match_strength`, `fee_amount`, `evidence_receipt_id`, `members_sum`, `member_receipts`).
  - *Backward compatibility (older clients and `list`/`search` filters):* `list(linked=...)` includes `BudgetReceiptLinkMember` rows; `_summary()` emits `linked: true`, `linked_account_id`, and sha256-digested `linked_entry_ref` (when `_match_allowed`) from the group while popping the new contract fields for older clients.
- **New fixed-path, single-method `_ADMIN` routes** (no `{id}` URL segments; no raw `entry_reference` in URLs/logs; `allow_head=False` on `GET`; classified in `hestia/web.py` `_ROUTE_MIN_ROLE` and `api_contract._PATH_ROLES`):
  - `POST /api/budget/receipts/group/candidates` (**v1:** `{receipt_id}` receipt-anchored lookup for device-token/browser callers with `budget.receipts.match`; **v2:** browser-admin `{account_id, entry_reference}` transaction-anchored lookup) -> `proposals[]`, `eligible_receipts[]`, sealed `candidate_token`s.
  - `POST /api/budget/receipts/group/link` (**v1**, `budget.receipts.edit` + `budget.receipts.match`) -> `{link, members: 2..8 {receipt_id, expected_version}, evidence_receipt_id?, evidence_expected_version?, fee_amount?}`; verifies inside `BEGIN IMMEDIATE`, maps constraint races to `409 budget_receipt_tx_already_linked`, invalid sums to `400 budget_receipt_group_sum_mismatch`.
  - `POST /api/budget/receipts/group/unlink` (**v1**, `budget.receipts.edit` + `budget.receipts.match`, CAS `expected_version`).
  - `GET /api/budget/receipts/group/detail?group_id=<uuid>` (**v1**, `budget.receipts.view`; returns group, members, and refreshed `candidate_token` when `needs_review`).
  - `POST /api/budget/receipts/delete` (**v1**) gains optional `dissolve_group: bool = False` and `expected_group_version: int | None = None`.
  - `POST /api/budget/receipts/group/replace` and `POST /api/budget/receipts/group/reconfirm` (**v2**, `budget.receipts.edit` + `budget.receipts.match`).

### Migration (revision assigned at implementation)

Revision ID and `down_revision` are **assigned at implementation** (expected chain: `0033` idmatch → `0034` appointments → `0035` this migration).

- **Upgrade:** Creates `budget_receipt_link_groups`, `budget_receipt_link_members`, their indexes, and SQLite mutual-exclusion triggers (including v1f `evidence_for_receipt_id` vs `evidence_receipt_id` exclusivity). Leaves `budget_receipts` columns and `uq_budget_receipts_tx_link` untouched.
- **Downgrade:** Refuses with `RuntimeError` if any rows remain in `budget_receipt_link_groups` (matching the `0024` pattern), then drops triggers, indexes, `budget_receipt_link_members`, and `budget_receipt_link_groups`.

---

## 7. UI Design

- **Non-preselected drawer suggestion (v1):** In `ReceiptDetailDrawer`, when `group_candidates.length > 0` and no strong 1:1 match is preselected, show a neutral card (*"Combined charge match: N receipts = X PLN"*) with **"Review group"** (never auto-selected).
- **Group Builder modal (browser memory only, `RedactedOrText` everywhere):**
  1. **v1 entry points:** Opened from a receipt's `group_candidates` suggestion, from **"Group with other receipts"** in a receipt drawer (receipt-anchored), or from the Receipts list bulk bar (**"Link selected to one transaction"** for `2..8` same-currency/kind selections). (**v2** adds the bank transaction row **"Link multiple receipts"** entry point.)
  2. Displays target bank tx, selected receipts, optional standalone gateway `evidence_receipt_id` (`confirmed`, `evidence_for_receipt_id IS NULL`), and an exact-decimal **Remaining delta** (`0.00` required to enable **"Confirm group link"**).
  3. If any selected receipt (or gateway evidence row) is still `status == 'imported'`, the modal discloses that those rows will be confirmed (`link: null`) first and sequentially confirms them before calling `group/link`, stopping cleanly on any conflict/validation failure.
- **List & transaction presentation:**
  - Receipts list keeps one row per receipt (preserving keyset pagination and `linked` filter) with a **`Grouped (N)`** chip (`needs_review` warning when `group_status == 'needs_review'`).
  - Grouped receipt drawers display the **Linked group** section (sibling links, optional fee, **"Unlink group"**; plus v2 **"Swap receipt"** / **"Re-confirm"**) and require confirmation for **"Unlink group & delete"** (`dissolve_group: true`).
  - Bank transaction rows show **`Linked (N receipts)`** opening `group/detail`.

---

## 8. Phasing

- **v1 (one PR):**
  - Migration (assigned revision, expected `0035`), models, triggers, and `reconcile_receipt_links` latch (`needs_review`).
  - Receipt-anchored candidates (`detail.group_candidates` and receipt-anchored `group/candidates`), `group/link`, `group/unlink`, `group/detail`, `delete` returning `409 budget_receipt_in_group` + `dissolve_group: true`, and `confirm` guard (`409`).
  - Additive contract gating (assigned minor), UI drawer suggestion, receipt-anchored Group Builder, `Grouped (N)` badge, and `dissolve_group` delete prompt.
- **v2 (follow-up):**
  - `POST /api/budget/receipts/group/replace` (atomic member swap without unlink/relink).
  - `POST /api/budget/receipts/group/reconfirm` (in-place recovery from `needs_review`; v1 uses `group/unlink` + `group/link`).
  - Transaction-anchored lookup (`{account_id, entry_reference}` on `group/candidates`) and its bank-transaction UI entry point.

---

## 9. Tests, Risks & Rollout

### Named synthetic test plan (100% line+branch coverage)

- **Migration & `0024` snapshot:** Upgrade from `down_revision` (expected `0034` appointments after `0033` idmatch), verify uniqueness constraints and mutual-exclusion triggers (1:1 vs M:1 tx collision, dual-group receipt, draft/imported/evidence member rejection, and **v1f `evidence_for_receipt_id` vs group `evidence_receipt_id` mutual exclusion + non-confirmed `evidence_receipt_id` rejection**), non-empty downgrade refusal, clean empty downgrade/re-upgrade, and unchanged `0024` `budget_receipts` index snapshot.
- **Matching & solver bounds:** Apple 3-receipt consolidated debit; split shop 2-receipt order; Allegro 3-seller checkout + confirmed standalone PayU `evidence_receipt_id` (`fee_amount > 0`, `evidence.total == tx.amount`) + `#300` `gateway_ref_hmac`/`blik_ref_hmac` ID pin; `card_last4` scoping; **v1f evidence-target member allowed (`G.evidence_for_receipt_id = D1.id` does not block `D1` from joining `R`, while `G` stays excluded from `R`)**; rejection of mixed `purchase`+`refund`, partial refunds, FX mismatch, unbacked fees, `gateway IS NOT NULL` inside $R$, `cash`/`mixed`, and worst-case cap test (`MAX_GROUP_POOL = 24`, `MAX_SOLVER_STEPS = 2_000`).
- **#300 / #294 safety:** `group_candidates` excluded from `candidates[]`, never preselected by `preselectedCandidate`, and ignored by `buildBulkConfirmRequest(..., { linkById: true })`.
- **Reconcile & single-member mutations:** `commit_snapshot` and `import_snapshot` latch the group, members, and `evidence_receipt_id` to `needs_review`. `group/unlink` restores the rows to `confirmed` and clears the link. A matching-field edit or delete returns `409 budget_receipt_in_group`. `delete(dissolve_group=true)` atomically dissolves the group and deletes the target receipt.
- **Transfer exclusions:** A group link on an existing transfer leg fails. Accepting a transfer on a grouped transaction returns `409` for both `confirmed` and `needs_review` groups, with no partial transfer, version bump, or audit row. Accepting the transfer after an explicit group unlink succeeds.
- **Security & contract gating:** Delegated permissions (`budget.receipts.match` / `budget.receipts.edit`), device-token boundaries, cross-owner trigger/404 rejection, zero raw `entry_reference`/PII in audit/wire payloads, and `X-Hestia-Contract-Version` stripping below the assigned minor.
- **UI (`receipts_model.test.ts`, `receipts.test.tsx`):** Zero-float exact-decimal group delta, non-preselection, sequential pre-confirm of `imported` members/evidence, `Grouped (N)` list badge, and `dissolve_group` delete prompt.

### Risks & rollout

- **Risks:** (1) *False subset-sum matches* → mitigated by merchant/gateway/ID buckets, `k <= 8`, and mandatory manual confirmation; (2) *gateway email double-counting* → mitigated by restricting $R$ to `gateway IS NULL` and `evidence_for_receipt_id IS NULL` while allowing standalone gateway proof strictly via `evidence_receipt_id`; (3) *multi-step UI confirm interruption* → confirmed unlinked rows stay valid and resumable.
- **Rollout:** Land pending **PR #300** (`0033` idmatch) and appointments (`0034`) → implement **v1** (`0035`, next contract minor after `master`, UI Group Builder + `Grouped (N)`) → verify against Apple monthly, split-order, and Allegro/PayU cases in prod UAT → follow up with **v2** (`group/replace`, `group/reconfirm`, tx-anchored lookup).
