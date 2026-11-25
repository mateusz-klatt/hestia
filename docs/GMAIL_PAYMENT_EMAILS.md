# Gmail payment-confirmation import (receipts v1e)

Status: draft spec, 2026-11-25. Owner decision: emails without a PDF that confirm a payment must import.

## Goal

Today the Gmail import accepts only PDF or HTML **invoices** from four fixed issuers (`gmail_invoice.py`). Most of the owner's paid purchases arrive instead as plain payment emails with no PDF:
- payment-gateway confirmations;
- app-store and subscription receipts;
- Stripe-style SaaS receipts;
- food-delivery and shop order confirmations.

Each of these says *what* was paid for, *how much* and *when*. v1e imports them as `document_kind="payment_confirmation"` receipts. The existing receipt-to-bank-transaction candidate flow (`receipt_store`, `receipt_reconcile`) then lets the owner attach each one to its bank transaction.

A survey of the owner's mailbox informed this spec. The survey stays in the operator session; this repository holds synthetic fixtures only.
- The layouts vary widely, so v1e uses a **generic labelled-total extractor**, not per-merchant templates.
- About a third of payment-looking mail is *not* a completed payment. These must be rejected:
  - "finish your payment" reminders;
  - failed-charge notices;
  - "transaction started" notices;
  - pickup and shipping notices that repeat an order total;
  - newsletters with prices.

## Non-goals (v1e)

- PDF invoices from non-issuer senders (follow-up).
- Forwarded payment emails (direct authenticated senders only).
- Item-level lines (follow-up; v1e stores one description line).
- Refunds and credit notes (rejected in v1e).
- Any schema migration. `payment_confirmation` already exists in `ck_budget_receipts_document_kind`, and trust_path `direct_allowlist` is reused.

## Configuration

Add an optional key `payment_senders` to the Gmail JSON config (`gmail_config.py`, documented in DEPLOY.md). It defaults to `[]`.

```json
"payment_senders": [
  {"sender": "@shop.example.test", "merchant": "Example Shop"},
  {"sender": "@pay.example.test", "merchant": "Example Pay", "gateway": true}
]
```

- `sender` uses the same rule grammar as `sender_allowlist`: a bare mailbox, or `@domain` covering subdomains. Public webmail domains are rejected.
- `merchant` is required. It is 1–64 printable characters and is used as `raw_shop_name` unless a gateway names the payee.
- `gateway` is optional and defaults to `false`. A gateway's mail names a different payee in the body.
- At most 200 entries. Duplicate `sender` rules are rejected.
- A sender listed in both `sender_allowlist` and `payment_senders` is a config error. Each sender has exactly one import path.

The Gmail search terms (`gmail_sync._terms`) include the payment sender rules. Trust classification treats them exactly like `sender_allowlist`: the same DKIM/DMARC alignment rules, and the trust path `direct_allowlist`. Forwards never use `payment_senders`.

## Processing

In `GmailMessageProcessor`, an authenticated direct message whose From matches a payment-sender rule goes to `extract_payment` instead of `extract_invoice`. The function lives in a new module `hestia/budget/gmail_payment.py`.

```python
extract_payment(text: str, *, subject: str, merchant: str, gateway: bool) -> ImportedPayment | None
```

- The input `text` is the worker-rendered body: text/plain first, then HTML, through the existing sidecar. PDFs are never passed here.
- `subject` is the decoded Subject header, NFKC-normalized and capped at 256 chars.
- Normalize the body exactly like `gmail_invoice`: NFKC, drop Cf characters, cap lines at 512 chars, cap the total at 32 KiB. Redact with `redact_pan` before any field is read. Fields come only from the redacted text.

### Rejection markers (checked first)

The message is rejected (None) when the subject or any body line matches one of these, case-insensitively and whitespace-tolerant.
- Unfinished or started payments:
  - `dokończ płatność`
  - `płatność nie została (jeszcze )?zakończona`
  - `rozpoczęci[ae] transakcji`
  - `oczekuje na płatność`
  - `nie opłacono`
- Failed payments:
  - `payment failed`
  - `unable to process`
  - `was declined`
  - `nieudana płatność`
  - `płatność odrzucona`
  - `nie udało się (pobrać|obciążyć)`
- Refunds and corrections:
  - `zwrot`
  - `refund`
  - `korekta`
  - `faktura korygująca`
  - `credit note`
- Pickup and shipping notices:
  - `czeka na odbiór`
  - `gotowe do odbioru`
  - `potwierdzenie odbioru`
  - `została wysłana`
  - `wysłaliśmy`
  - `shipped`
  - `out for delivery`

Line-item text cannot trigger a marker. Markers match the subject, or a whole body line of at most 120 chars that contains no amount.

### Amount

Collect *labelled totals*. A label matches a whole line, or a line prefix followed by `:` or whitespace. The value sits on the same line or on the next non-empty line. The labels, from highest priority to lowest:

1. `Amount paid`, `Kwota zapłacona`, `Zapłacono`, `Paid` (only as a whole-line label or directly before a value)
2. `Kwota transakcji`, `Do zapłaty`, `Łącznie`, `Razem do zapłaty`, `Suma do zapłaty`, `Total`, `Cena z dostawą`
3. `Razem`, `Suma`, `Kwota zamówienia`, `Kwota`
4. A *card-charge line*: an amount on the line right after a masked card (`•••• 1234`, `**** 1234`, `- 1234`, `xxxx 1234`)

- **Value grammar.** An amount with currency `zł` / `PLN` / `EUR` / `€` / `USD` / `$` / `GBP` / `£`, the symbol before or after.
  - Decimal comma or dot, optional thousands separators (space, NBSP or the other separator), exactly 0 or 2 decimals.
  - Values must be > 0 and ≤ 1 000 000. A negative value rejects the whole message.
  - Trailing words such as `miesięcznie` or `brutto` are allowed after the amount on the same line.
- **Multiple values.**
  - Use the highest-priority level that has any value.
  - All values at that level must agree in amount and currency; if they disagree, reject.
  - Lower levels are ignored.
  - "Fees" lines (`Dodatkowe opłaty`, `Opłata za realizację transakcji`, `Opłata za usługę`, `Dostawa`) are never totals.
- **Legal footers.** Lines containing `kapitał`, `KRS` or `zakładowego` are ignored entirely.
- With no labelled total and no card-charge line, reject. A newsletter with prices therefore yields None.

### Merchant and description

- **Non-gateway sender.** `merchant` comes from config. The description is the first of:
  - `Opis zamówienia w <X>: <D>` → D
  - `Tytuł: <D>`
  - the subject

  Each is sanitized: `_safe_field`-style digit gate off, with emails, URLs, PAN/IBAN/PESEL and long digit runs (>10) removed, then whitespace-collapsed and capped at 128 chars.
- **Gateway sender.** The payee comes from `Odbiorca wpłaty: <X>`, `Opis zamówienia w <X>:`, `płatność dla <X>` or `dla - <url-host>`. It must be unique, 2–128 chars and free of digits runs > 6. The description is the same as above. If no payee is found, use the config merchant.
- **Raw text.** No raw body text is persisted. `raw_ocr_text` stays NULL, as for invoices.

### Date

`receipt_date` is the message `internalDate` converted to the Europe/Warsaw calendar date. A body date (`Date paid`, `Data`, `Paid <date>`) is used only to cross-check: if one is present, parseable, and more than 3 days from internalDate, reject.

## Storage

`GmailStore` gains `_payment` beside `_invoice`, creating a `BudgetReceipt` with these fields:
- `document_kind="payment_confirmation"`, `status="imported"`, `source_kind="email"`;
- `raw_shop_name=merchant`, `receipt_date`, `total_amount` (`canonical_money`) and `currency`;
- `invoice_number=None`, `due_date=None`, and `import_expires_at` as for invoices.

The description is stored as exactly one `BudgetReceiptLine` with `line_kind="other"`, `raw_name=description` and amount = total, if the line schema allows that without a migration. If it does not, the implementer reports back before choosing another field.

### Cross-email dedupe

A gateway confirmation and the shop's own confirmation often describe the same purchase. Before inserting, skip the new payment (outcome `skipped_non_receipt`) when an existing `payment_confirmation` receipt of the same owner matches on all of these:
- same `total_amount` and `currency`;
- `receipt_date` within ±2 days;
- exactly one of the two came from a `gateway` sender.

Persist a boolean in `BudgetGmailMessage` provenance only if that is possible without a migration; otherwise derive "came from a gateway" from the config at dedupe time. Two non-gateway receipts with equal amounts are never merged, because separate equal purchases are common.

## API, UI, contract

- `payment_confirmation` is already in `BudgetReceiptDocumentKind`. Check that the receipts list and detail UI render it with an i18n label: add the key to `en.ts`/`pl.ts` and mark the rest for Codex translation if missing.
- No new routes. The OpenAPI bump is needed only if a schema changes.

## Tests (synthetic only)

The fixtures live in `tests/fixtures/gmail/payment/*.txt`. The expected results:

| Fixture | Subject (passed by the test) | Gateway? | Expected |
|---|---|---|---|
| `gateway_p24_style.txt` | `Nowa transakcja płatnicza (P24-AAA-BBB-CCC)` | yes | 217.00 PLN, merchant `PRZYKŁADOWY SKLEP SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ`, description `Zamówienie 100200300` |
| `gateway_tpay_style.txt` | `Potwierdzenie transakcji TR-AAAA-0000000` | yes | 83.84 PLN, merchant `PRZYKŁADOWA SPÓŁKA AKCYJNA`, description `Zamówienie 00000000001` |
| `gateway_started.txt` | `Potwierdzenie rozpoczęcia transakcji dla - https://sklep.example.test/` | yes | None |
| `gateway_unfinished.txt` | `Dokończ płatność` | yes | None |
| `food_order.txt` | `Zamówienie potwierdzone` | no | 55.52 PLN (`Łącznie` outranks `Razem`), merchant from config |
| `app_store_invoice.txt` | `Twoja faktura z firmy Example` | no | 419.99 PLN (card-charge line) |
| `stripe_receipt.txt` | `Your Example Labs receipt [#0000-0000]` | no | 399.66 PLN |
| `stripe_receipt_usd.txt` | `Your receipt from Example Cloud, Inc. #0000-0000-0000` | no | 6.15 USD |
| `subscription_eur.txt` | `Potwierdzenie zamówienia z 13 lis 2026` | no | 99.99 EUR |
| `payment_failed.txt` | `Important: Your Example Family subscription payment failed` | no | None |
| `pickup_notice.txt` | `Twoje zamówienie nr 00000000000000 czeka na odbiór w salonie` | no | None |
| `shop_order.txt` | `Dziękujemy za zamówienie nr 00000000001` | no | 83.84 PLN |
| `newsletter_with_prices.txt` | `Nowości w tym tygodniu` | no | None |

Also required:
- disagreement and negative-value rejects;
- NBSP amounts;
- a marker inside an item line does not reject;
- the legal-footer capital line is ignored;
- config validation;
- trust and search-term inclusion;
- dedupe (gateway plus shop merges; two shops do not);
- the pipeline over the worker socket;
- no body text in the DB;
- 100% line and branch coverage on touched modules.

## Real-data validation (operator, not CI)

Before merge, the operator runs `extract_payment` over the owner's real payment-like mail in-session and reports only aggregates: counts per outcome per sender group. The operator also spot-checks values. The synthetic fixtures do not carry the real layouts' noise (headings, prose, repeated labels), so this step is mandatory.
