# Gmail payment evidence (receipts v1f)

Status: spec, 2026-11-25. This builds on `GMAIL_PAYMENT_EMAILS.md` (v1e). The operator surveyed the owner's mailbox in the prod session. Every example below is synthetic.

## Why

v1e imports every gateway email as its own `payment_confirmation` receipt. A survey of about 250 real gateway emails found these problems:

1. **Status is ignored.** Gateways send several emails per payment:
   - a *registered* email ("zarejestrowaliśmy zlecenie", "Nowa transakcja płatnicza");
   - a *paid* email ("Potwierdzenie", "Przekazaliśmy Twoje pieniądze", "opłacona");
   - a *failed* email ("straciła ważność", "anulowana").

   v1e imports registered emails as payments, misses some paid ones, and even imported an *expired* Paynow transaction.

   Many payments only ever get a *registered* email: P24 sends a paid email for roughly 1 in 4 transactions, and PayU never sends one to the payer. So registered emails cannot simply be dropped.
2. **Duplicates.** The registered and paid emails for one transaction both import.
3. **Payee lost.** The real payee gets replaced by the gateway name, for example when a payee line ends in a numeric id ("Odbiorca wpłaty: SHOP SP. Z O.O. 4500000000"). The payee web address is never kept.
4. **Fees.** Gateways add a transaction fee. The bank debit equals base + fee. The two Autopay templates disagree about which amount they show.
5. **Double counting.** A gateway email and the shop's own invoice or receipt for the same purchase both import as separate documents.

**Owner rule (2026-11-25).** A gateway email is **payment evidence, not a purchase document**. Usually the shop sends an invoice or receipt the same day. That document's amount is the gateway total or slightly less, the difference being the fee. Sometimes the shop document is a PDF (for example a train ticket). Sometimes there is none, and the owner downloads it from the shop website. The ledger counts spending from bank transactions, so a gateway email must never be a second document beside the shop's own.

## Scope

In scope:
- gateway status classification;
- merging by gateway transaction id;
- payee and payee host extraction;
- fee extraction;
- attaching evidence to a shop document;
- flagging a lone gateway email as "payment without a document".

Out of scope:
- non-issuer PDF documents (v1f-pdf, next);
- FX and credit notes;
- bank matching (Home's #rcand). #rcand will consume the new fields.

## Data model (migration 0031, additive)

New nullable columns on `budget_receipts`:

| column | type | meaning |
|---|---|---|
| `gateway` | VARCHAR(64) NULL | The configured gateway merchant (e.g. "Przelewy24"). NULL for non-gateway rows. |
| `gateway_status` | VARCHAR NULL, CHECK IN ('registered','paid') | Only for gateway rows. |
| `gateway_ref_hmac` | VARCHAR(64) NULL, hex CHECK like the other hmac columns | Keyed HMAC of the normalized gateway transaction id, using a new domain constant. Partial UNIQUE index on (owner, gateway, gateway_ref_hmac) WHERE gateway_ref_hmac IS NOT NULL. |
| `fee_amount` | VARCHAR NULL, `canonical_money`, ≥ 0 | The gateway fee. `total_amount` always means the amount charged (base + fee). |
| `payee_host` | VARCHAR(253) NULL | Lowercased host of the payee site, without scheme, path or `www.`. Domain syntax is validated. |
| `evidence_for_receipt_id` | VARCHAR NULL, FK (evidence_for_receipt_id, owner) → budget_receipts(id, owner) ON DELETE SET NULL | Set when this gateway row is evidence for a shop document. |

- `raw_shop_name` keeps the real **payee**, the shop. It falls back to the gateway merchant only when no payee was extracted.
- The downgrade drops the columns and the index. It must be FK-safe, following the 0030 pattern, and goes through `db.downgrade_db` only.
- Existing v1e rows are untouched, with every new field NULL. Re-processing (below) fills them in.

## Status classification

`inspect_payment` gains `status: Literal["registered","paid"] | None` and `reject_reason`. For a gateway sender, classify using the subject and body, case-insensitively and after NFKC:

| gateway | paid | registered | reject (not imported) |
|---|---|---|---|
| P24 | subject `^Potwierdzenie\b`; body "potwierdzeniu realizacji płatności" | subject "Nowa transakcja płatnicza"; body "zarejestrowaniu nowej transakcji" | body "Rejestracja karty" + "zostanie anulowana" (card verification) |
| Tpay | subject `^Potwierdzenie transakcji` | subject "Nowa transakcja płatnicza"; body "ZAREJESTROWANIE NOWEJ TRANSAKCJI" | — |
| Autopay | body "Płatność zakończona sukcesem", "Przekazaliśmy Twoje pieniądze", "wpłata została przekazana odbiorcy"; subject "Przekazaliśmy Twoją płatność", "Potwierdzenie przekazania pieniędzy" | subject "Zarejestrowaliśmy Twoje zlecenie", "Potwierdzenie rozpoczęcia transakcji" | marketing (no amount label) — already v1e |
| PayU | (none observed) | body "zarejestrowaliśmy zlecenie płatności" | subject "Zwrot środków" (refund, later), "Dokończ płatność" |
| Paynow | body "opłacona", "zakończona sukcesem" | body "Zarejestrowaliśmy zlecenie płatności" | body "straciła ważność", "anulowan", "odrzucon" → **failed** |
| Stripe and shop senders | always paid (v1e behaviour) | — | v1e rejections |

- An email matching both a paid and a registered marker counts as paid.
- An unknown gateway email with a valid amount is registered. Silence is never treated as success.

## Gateway transaction id

Extract the id per gateway:
- P24 `P24-XXX-XXX-XXX`;
- Tpay `TR-[A-Z0-9]+-[A-Z0-9]+`, with any `/...` suffix dropped;
- Autopay "Identyfikator transakcji[ Autopay]: X";
- PayU the numeric id in the subject parentheses or the `transId` in the status link, never the token;
- Paynow `XXXX-XXX-XXX-XXX`.

Normalize to uppercase and HMAC it. When no id is found, `gateway_ref_hmac` is NULL and the v1e dedupe applies.

## Merge rules (store, inside the import transaction)

All lookups use (owner, gateway, gateway_ref_hmac).

| incoming | existing row with the same ref | action |
|---|---|---|
| registered | none | insert (`gateway_status='registered'`) |
| registered | registered or paid | `skipped_duplicate` |
| paid | none | insert (`'paid'`) |
| paid | registered, still `imported` | **upgrade in place**: set status `paid`, refresh payee/fee/total/payee_host, bump `version` and audit; outcome `skipped_duplicate` for the new message |
| paid | registered, owner-confirmed | do not mutate amounts; set `gateway_status='paid'` only if total and currency are equal |
| failed | registered, `imported`, not linked | delete it through the existing tombstone path (`tombstone_receipt`); record the message as `skipped_non_receipt` |
| failed | confirmed or linked | leave it; outcome `skipped_non_receipt` |
| failed | none | `skipped_non_receipt` |

## Payee, host and fee

- **Payee labels:**
  - "Odbiorca wpłaty:", "Odbiorca:" (also on the next line);
  - "Opis zamówienia w X:" (Tpay);
  - "dla X" in the subject or first sentence;
  - "zlecenie płatności dla X (http…)" (PayU).

  Strip one trailing whitespace-separated all-digit token of 6 or more digits ("SHOP SP. Z O.O. 4500000000" → "SHOP SP. Z O.O."). Keep v1e's `_payee_key` normalization for dedupe.
- **Host:** taken from "Adres www:", `(http(s)://host…)` after the payee, or "dla https://host/" in the subject. Store only the host.
- **Fee labels:**
  - "Dodatkowe opłaty( obciążające klienta)?:" (the value may be on the next line);
  - "Opłata za realizację transakcji:";
  - "OPŁATA ZA REALIZACJĘ TRANSAKCJI:".

  A missing fee label gives `fee_amount=NULL`. A fee of 0 is stored as `"0"`.
- **Amount charged:**
  - When "Do zapłaty", "Łączna kwota( do zapłaty| transakcji)?" or "ŁĄCZNA KWOTA" is present, use it as `total_amount`, and check that it equals base + fee when both exist. A mismatch rejects the email.
  - Otherwise `total_amount` is base + fee, or base when there is no fee.

  This normalizes both Autopay templates to the charged amount.

## Evidence attachment

After a gateway row is inserted or upgraded, look for its shop document. Also run the check the other way round: when a non-gateway document is inserted, look for an unattached gateway row. Pick D for gateway row G when all of these hold:

- the same owner;
- D is not a gateway row (`gateway IS NULL`) and is not itself evidence;
- `D.document_kind IN ('invoice','receipt','payment_confirmation')`;
- the currencies are equal;
- the amount matches: `D.total == G.total` or `D.total == G.total - G.fee`;
- the date window:
  - for an invoice, `D.receipt_date - 1 ≤ G.date ≤ max(D.due_date, D.receipt_date) + 14`;
  - otherwise `|G.date - D.receipt_date| ≤ 1`.

Attach only when **exactly one** D qualifies after this tie-break: prefer a `_payee_key` match between G's payee and `D.raw_shop_name`, or a matching NIP when both have one. Then set `G.evidence_for_receipt_id = D.id`.

- Ambiguous matches or no match leave G standalone.
- An owner-confirmed G or D is never re-pointed automatically.
- Evidence attached by the owner is out of scope for v1f; the column allows it later.

## Counting and API (contract: next minor after master at merge time)

- The receipt list gains filter `evidence: "exclude"|"only"|"include"`, default **exclude**. Gateway rows attached as evidence therefore disappear from the default list and from the pending-import cap (`_unconfirmed_import`).
- The receipt detail of D gains `payment_evidence: [{receipt_id, gateway, gateway_status, total_amount, fee_amount, receipt_date}]`.
- The detail and list items of G gain these additive fields:
  - `gateway`, `gateway_status`, `fee_amount`, `payee_host`, `evidence_for_receipt_id`;
  - `has_document: bool`, false for a standalone gateway row.

  The UI shows a standalone one as "płatność bez dokumentu — pobierz ze strony {payee_host}".
- Confirming D cascades nothing. Confirming a standalone G stays allowed.
- `gateway_status='registered'` rows are shown as "niepotwierdzona przez bramkę". They are still confirmable and linkable, because the bank transaction is the proof.

## Re-processing existing imports

Add a CLI step `python -m hestia.budget gmail reprocess-payments`. It schedules a scan over `budget_gmail_messages` rows with `outcome IN ('imported','skipped_duplicate')` and a gateway payment-sender rule. For each row it re-fetches the message, re-classifies it, and applies the merge rules and attachment to the receipt it produced. Unconfirmed `imported` rows only; confirmed rows are never touched. It must be idempotent and must run under the existing per-run quota and `RunLimit`. The alternative is a documented `--retry-...` style flag on `gmail sync`. Choose whichever fits `gmail_sync` with less new surface, and explain the choice.

## Tests (synthetic only; fake names, amounts, ids, hosts)

New fixtures in `tests/fixtures/gmail/payment/v1f/`. At minimum:

- P24 registered + P24 paid, same id → one row, upgraded to paid, payee from "Odbiorca wpłaty" with the trailing digit id stripped;
- P24 registered only → registered row;
- P24 card verification ("Rejestracja karty w aplikacji mobilnej. Transakacja zostanie anulowana.") → rejected;
- Tpay "Nowa transakcja płatnicza TR-ABCD-1234XYZX" + "Potwierdzenie transakcji TR-ABCD-1234XYZX/9999 za …" → one paid row, with the payee from "Opis zamówienia w X:";
- Autopay template A (Kwota transakcji, Opłata za realizację transakcji, Do zapłaty) and template B (Kwota transakcji, Opłata…, Łączna kwota) → the same charged total and fee;
- Autopay "Zarejestrowaliśmy…" then "Przekazaliśmy…", same Identyfikator transakcji → one paid row;
- Autopay "Płatność zakończona sukcesem" with "Adres www: shop.example.test" → payee_host;
- PayU "zarejestrowaliśmy zlecenie płatności dla FUNDACJA EXAMPLE (http://www.example.test)" → registered, payee + host, ref from the subject id;
- Paynow registered then "straciła ważność", same id → the registered row is deleted (tombstoned) and no receipt remains; the same pair after the owner confirmed → the row is untouched;
- a total mismatch (Do zapłaty ≠ Kwota + Opłata) → rejected;
- evidence:
  - a T-Mobile-style invoice (issuer template) and a P24 paid email for the invoice total + fee within the due window → attached;
  - two invoices with the same total → ambiguous → standalone;
  - a shop payment_confirmation and a gateway email, same day, total = gateway total − fee → attached;
- the list default excludes evidence and the pending cap ignores it;
- the migration upgrade, downgrade and re-upgrade via the db runners, with FK-safe downgrade;
- re-processing idempotency: run twice, same state.

Coverage stays at 100% line+branch. No real data in fixtures.

## Real-data validation (operator, holzera, not CI)

The operator re-runs the corpus harness against the implementation and reports aggregates only:
- per gateway, the counts of paid, registered, rejected and failed emails;
- merged ids;
- attached vs standalone;
- fee rows.

Expected on the current mailbox:
- P24 ≈ 52 ids, of which 12 paid and 40 registered;
- Tpay 17 ids, of which 8 paid;
- PayU ≈ 87 registered;
- Paynow: the expired transaction is no longer imported.

## Revision 2 (2026-11-25, after the Codex review of PR #295 and the operator's real-mailbox validation)

This revision overrides the sections above wherever they conflict.

### R1. Replace in-place reprocess with a reset + re-import

The owner has confirmed **none** of the production Gmail imports. Instead of re-classifying legacy rows in place (Codex P0/P1 #2, #3, #4, #5), remove the `reprocess-payments` scan and all code paths that retag or mutate legacy rows. Add an operator CLI instead:

`python -m hestia.budget gmail reset-unconfirmed-payments [--dry-run]`

It runs in ONE write transaction (`BEGIN IMMEDIATE`), fenced to the active grant generation. It refuses unless the grant is active.

- **Selects** the owner's receipts where:
  - `document_kind='payment_confirmation'`;
  - `status='imported'`;
  - `source_kind='email'`;
  - `linked_account_id IS NULL`;
  - `forwarded_manually=0`.

  Invoices and receipts are never selected, nor is any confirmed, needs_review or linked row.
- **Deletes** each selected receipt through the existing tombstone/delete path, including its AppMeta provenance and evidence pointers.
- **Deletes** the `budget_gmail_messages` rows whose `receipt_id` was one of them. It also deletes every `skipped_duplicate` row and every `skipped_non_receipt` row with `trust_path='direct_allowlist'`, so the next full-window backfill re-fetches and re-evaluates those messages. Invoice rows with `outcome='imported'` stay, so invoices do not duplicate.
- **Schedules** a full-window backfill, as `--retry-parse-failed` does today.
- **Prints** counts only.

`--dry-run` prints the counts and rolls back.

### R2. Provider-scoped parsing (Codex #1, operator F1)

`payment_senders` entries with `gateway: true` gain an optional `provider` in `{"p24","tpay","autopay","payu","paynow","stripe"}`. When it is absent, the provider is inferred from the sender domain: przelewy24.pl→p24, tpay.com→tpay, autopay.pl→autopay, payu.com→payu, paynow.pl→paynow, stripe.com→stripe. An unknown domain gives provider None, meaning v1e behaviour with status registered.

Status markers and id extraction use ONLY that provider's patterns. Ids are normalized to upper case before the HMAC.

Status precedence:
1. failed;
2. card verification (p24);
3. an explicit paid subject;
4. an explicit registered subject or body marker;
5. an explicit paid body marker;
6. otherwise registered.

The paid body markers must be **whole-line or labelled statements**:
- Paynow: a line equal to `opłacona` (optionally preceded by the line `Transakcja została`), or `Transakcja została opłacona`;
- Autopay: `Płatność zakończona sukcesem`, `Przekazaliśmy Twoje pieniądze`, `Twoja wpłata została przekazana odbiorcy.`;
- P24: `niniejsza wiadomość jest informacją o potwierdzeniu realizacji płatności`.

Never use bare `opłacona` inside a sentence. Real registered emails contain "Jeżeli transakcja nie została opłacona" (P24) and "sprawdzić, czy transakcja została już opłacona" (PayU).

A conditional cancellation sentence ("Jeśli wpłata nie została dokonana, transakcja zostanie anulowana") is NOT failed. Failed requires a status line: Paynow `straciła ważność` / `odrzucona` / `anulowana` as the status value.

### R3. Payee resolution (operator F2, F3)

- A labelled payee (`Odbiorca wpłaty:` or `Odbiorca:`, value on the same line or on the next non-empty line) wins over unlabelled candidates (`Do X`, `dla X`, subject).
- A candidate that is a URL or a dotted domain (`4parents.pl`, `https://sklep.example.test/`) is a **host**, never a payee. It feeds `payee_host`.
- Multiple distinct candidates reject only when two **labelled** payees disagree.

### R4. Ordering-independent merge in normal import (Codex #4, #6)

- **Failed** with a reference and no row: record a ref-level disposition in AppMeta, `budget_gmail_gateway_failed:<receipt-independent key>`, holding the ref HMAC plus gateway. It follows the provenance lifecycle and expires with the import window. A later registered email with that ref is skipped as `skipped_non_receipt`. A later paid email with that ref imports normally: paid wins and the disposition is cleared.
- **Remove** the v1e "gateway and shop within ±2 days → `skipped_non_receipt`" rule. Both import, and evidence attachment links them. Same-sender v1e dedupe stays.
- **Evidence attachment candidates** exclude any row with `gateway IS NOT NULL`, so gateway rows never become shop documents.
- When a paid upgrade changes the amount, fee or date, **revalidate** an existing attachment, unless either side is owner-confirmed.

### R5. Money safety (Codex #7)

- Reject a gateway email whose fee currency differs from its amount or total currency.
- A paid upgrade never changes `currency`. A currency mismatch with the existing row means `skipped_duplicate` with no mutation.

### R6. Contract gating (Codex #8)

- The new receipt fields (`gateway`, `gateway_status`, `fee_amount`, `payee_host`, `evidence_for_receipt_id`, `has_document`, `payment_evidence`) are emitted only when the client negotiates the new contract version, using the existing response-shaper / profile pattern in receipt_api. Older pinned clients get exactly the old shapes.
- Update the web UI contract pin.
- Default list cursors stay valid: an absent `evidence` filter signs the same cursor context as before.

### R7. Same-owner evidence pointer (Codex #9)

Same-owner integrity must be enforced in SQLite. The pointer gets `SET NULL` on target delete without touching `owner`. Use triggers (BEFORE INSERT/UPDATE: raise unless the target exists with the same owner; AFTER DELETE on the target: null the pointers) or an equivalent. The downgrade drops them.

### R8. Tests

Add regression tests for:
- both arrival orders: registered/paid, failed/registered and gateway/shop;
- a provider-scoped id when another provider's id appears in a title;
- the real-world phrases from R2;
- next-line `Odbiorca:`;
- a host-like `Do X`;
- fee currency mismatch;
- contract gating on wire responses (absent, pinned and current headers);
- the cursor compatibility rule;
- a cross-owner pointer rejection;
- reset CLI dry-run and real runs: counts, invoices untouched, confirmed/linked rows untouched, grant fencing.

## Revision 3 — forwarded payment emails

This revision adds v1f.2 (parsing forwarded payment emails) and records the v1f.1 review and prod-validation refinements.

### Forwarded payment emails (v1f.2)

For `trust_path` in `("spouse_forward", "owner_forward")`, after `unwrap_trusted_forward` returns the inner original message:

1. If the inner chain qualified for automatic issuer-invoice parsing (`not manual`) and `_invoice` matches an allowlisted issuer template and number pattern, keep the existing invoice import.
2. Otherwise, check the inner message headers against `payment_senders` using `payment_sender_for` semantics (exact mailbox first, then most specific domain). Inline forwards preserve `Subject`/`Temat` onto the unwrapped inner message alongside `From` and `Message-ID`, so provider-scoped subjects and references work identically for MIME `message/rfc822` and inline forwards.
3. When a `payment_senders` rule matches, run the same `_payment` path on the inner message: provider-scoped status and gateway transaction id extraction, gateway merge and fee/payee/host extraction, order reference hashing, and evidence attachment.
   - `trust_path` stays `spouse_forward` or `owner_forward`.
   - `forwarded_manually` stays `0` when payment parsing produces an `imported` payment.
   - `rfc_message_id_hmac` is computed from the **inner** message's `Message-ID`, so a forwarded copy and a direct copy of the same original email dedupe against each other in either arrival order without creating two receipts.
4. If payment parsing finds no total, rejects (including failed/refund/card-verification/date mismatch), or hits an unparseable part, fall back to the existing `_manual_forward` behaviour (`status='needs_review'`, `forwarded_manually=1`, `forwarded_sender_domain` from the inner sender, no amount or payment provenance). Unknown inner senders likewise fall back to `_manual_forward`.

### Review and real-data refinements (v1f.1)

- **Failed-gateway provenance TTL:** `budget_gmail_gateway_failed:*` AppMeta entries expire at `internal_date + PROVENANCE_TTL` (400 days), matching `budget_gmail_messages` provenance retention rather than `internal_date + backfill_days`.
- **Gateway total/currency checks:** `_gateway_payment_total` rejects any currency disagreement across base, charged, and fee (even when fee is absent) and rejects `fee > charged` even when base is absent.
- **Trailing dot host detection:** `_candidate_host` strips one trailing `.` before host detection, so sentence-final merchant names (`"Allegro."`) remain payees while sentence-final domains (`"4parents.pl."`) resolve to `payee_host="4parents.pl"`.
- **Evidence integrity & migration 0032:** `_evidence_candidates` and reverse attachment require `document.receipt_kind == gateway.receipt_kind`; revalidation on paid upgrade protects any target whose `status != 'imported'` (not only `'confirmed'`). Additive migration `0032` recreates the SQLite evidence triggers to reject self-references (`evidence_for_receipt_id = id`) while leaving deployed migration `0031` untouched; downgrade restores the `0031` triggers.
- **Autopay extraction:** Autopay prefers `Numer płatności` (inline or next line) as its gateway ref and falls back to `Identyfikator transakcji` (inline or next line for HTML table cells); labelled payees strip leading `<gateway> w imieniu ` prefixes so registered and paid emails share the same normalized payee key; bulleted and fee lines immediately after an empty `Odbiorca:` are ignored as payee values.
