# Gmail payment-confirmation import (receipts v1e)

Draft spec updated after operator mailbox validation on 2026-11-25. Payment confirmations without PDFs are in scope.

## Goal

Before v1e, Gmail imports handled **invoices** from four fixed issuers (`gmail_invoice.py`). Most of the owner's paid purchases arrive as payment emails with no PDF:
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
- `merchant` is required. It contains 1–64 printable characters, including at least one non-whitespace character. It is used as `raw_shop_name` unless a gateway names the payee.
- `gateway` is optional and defaults to `false`. A gateway can name a different payee in the body or subject.
- At most 200 entries. Duplicate `sender` rules are rejected.
- A sender listed in both `sender_allowlist` and `payment_senders` is a config error. Each sender has exactly one import path.

The Gmail search terms (`gmail_sync._terms`) include the payment sender rules. Trust classification treats them exactly like `sender_allowlist`: the same DKIM/DMARC alignment rules, and the trust path `direct_allowlist`. Forwards never use `payment_senders`.

## Processing

`GmailMessageProcessor` uses `inspect_payment` from `hestia/budget/gmail_payment.py` for authenticated direct messages whose From matches a payment-sender rule. The inspection result distinguishes a body with no total from a rejected payment. The same module exposes a single-subject helper for validation.

```python
extract_payment(text: str, *, subject: str, merchant: str, gateway: bool) -> ImportedPayment | None
```

- The processor uses the existing sidecar to render text/plain first. An absent or unreadable plain part can fall back to HTML. A readable plain part also falls back when it contains no total. A rejection marker, negative or ambiguous amount, conflicting values, or failed date cross-check ends the import. PDFs stay outside the payment path.
- Check every decoded Subject header for rejection markers. Normalize each header with `_normalize_receipt_text` before that check. This applies NFKC and removes controls, format characters, and uncomposed combining marks. Cap each normalized subject at 256 chars.
- Normalize the body as for `gmail_invoice`. Apply NFKC, drop Cf characters, cap lines at 512 chars, and cap the total at 32 KiB. Remove simple `p`, `b`, `strong`, `span`, `div`, `br`, `td`, `tr`, `th`, `table`, `tbody`, and `font` tags that appear literally in plain text. Extract fields from text redacted by `redact_pan`.

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

Body markers apply to lines of at most 120 chars that contain neither an amount nor a labelled field in the form `<label>: <value>`. A body line containing `rozpoczęci[ae] transakcji` triggers rejection only when it starts with `Potwierdzenie rozpoczęcia`. This lets a completed payment include a `Data rozpoczęcia transakcji: …` field. Subject markers apply anywhere in a normalized header.

### Amount

Collect *labelled totals*. A label matches a whole line, or a line prefix followed by `:` or whitespace. The value sits on the same line or on the next non-empty line. The labels, from highest priority to lowest:

1. `Amount paid`, `Kwota zapłacona`, `Zapłacono łącznie`, `Zapłacono`, `Paid` (only as a whole-line label or directly before a value)
2. `Do zapłaty`, `Łącznie`, `Razem do zapłaty`, `Suma do zapłaty`, `Total`, `Cena z dostawą`
3. `Razem`, `Suma`, `Kwota transakcji`, `Kwota zamówienia`, `Kwota`
4. A *card-charge line* is an amount on the line right after a masked card (`•••• 1234`, `**** 1234`, `- 1234`, `xxxx 1234`). A total label with an unparseable value still allows this check. A masked PAN such as `654321******0099` is never itself an amount.

- **Value grammar.** An amount with currency `zł` / `PLN` / `EUR` / `€` / `USD` / `$` / `GBP` / `£`, the symbol before or after.
  - Decimal comma or dot, optional thousands separators (space, NBSP or the other separator), exactly 0 or 2 decimals. The first group of a thousands-formatted value starts with a nonzero digit, so `0.005` is invalid.
  - Values must be > 0 and ≤ 1 000 000. A negative value rejects the whole message.
  - Trailing words such as `miesięcznie` or `brutto` are allowed after the amount on the same line.
- **Multiple values.**
  - A second currency amount on a labelled total's value line is ambiguous and rejects the message. A negative total rejects even at a lower priority.
  - Use the highest-priority level that has any value.
  - All values at that level must agree in amount and currency; if they disagree, reject.
  - Lower levels are ignored.
  - "Fees" lines (`Dodatkowe opłaty`, `Opłata za realizację transakcji`, `Opłata za usługę`, `Dostawa`) are never totals.
- **Legal footers.** Lines containing `kapitał`, `KRS` or `zakładowego` are ignored entirely.
- The extractor recognizes adjacent `Total` and `Amount paid` cells in a flattened `Receipt #…` summary. It scans the bounded row and label separators without quadratic backtracking.
- With no labelled total and no card-charge line, the body yields no total. A newsletter with prices therefore yields `None` when no other body has a total.

### Merchant and description

- **Non-gateway sender.** `merchant` comes from config. Description candidates have the following priority.
  - `Opis zamówienia w <X>: <D>` → D
  - `Tytuł: <D>` or `Opis płatności: <D>`
  - `Twoje zamówienie z lokalu <X>` → X
  - the subject

  Skip generic single-word descriptions (`Płatność`, `Payment`, `Zamówienie`, `Order`, `Receipt`, `Faktura`). Prefer candidates without digit runs longer than 10. When the subject is the only candidate, keep its sanitized text. Sanitization uses PAN, IBAN, and PESEL redaction, removes long digit runs, collapses whitespace, and caps the result at 128 chars. An empty result falls back to `Płatność`.
- **Contact filtering.** In one whitespace-token pass, drop any token containing `://`, `mailto:` or `www.`, or an `@` joining two non-empty parts. This also removes composite tokens such as `order_https://shop.example.test/`.
- **Gateway sender.** Collect payee candidates from every matching label on a line and from gateway subject forms. A payee must be unique, 2–128 chars and free of digit runs longer than 6. Use the config merchant if no candidate qualifies. Conflicting valid payees reject the message. Description extraction follows the same rules as above. `Tytuł:` never supplies a payee.
  - Body forms include `Odbiorca wpłaty: <X>`, `Odbiorca: <X>`, `Opis zamówienia w <X>:`, `płatność dla <X>`, `dla - <url-host>`, and `zarejestrowaliśmy zlecenie płatności dla <X> (http://…).` Strip the trailing contact token, its parentheses, and the final period from the registered-payment payee.
  - SaaS receipts can use `Receipt from <X> Receipt #…`, `Receipt from <X>`, or `Payment to <X> : <amount>` in the body. The subject can use `Your receipt from <X> #1234-5678`, `Your <X> receipt [#1234-5678]`, or `Twoje pokwitowanie od <X> nr 1234-5678`.
  - `Do <X>` supplies the payee when it follows the completed-payment heading `Przekazaliśmy Twoje pieniądze`. Registration and completion emails can both supply a valid total. The dedupe rules below handle the pair.
- **Raw text.** No raw body text is persisted. `raw_ocr_text` stays NULL, as for invoices.

### Date

`receipt_date` is the message `internalDate` converted to the Europe/Warsaw calendar date. A parseable body date (`Date paid`, `Data`, `Paid <date>`) more than 3 days from `internalDate` rejects the message. Ignore `Paid <date>` inside description fields such as `Tytuł:` and `Opis`.

## Storage

`GmailStore` gains `_payment` beside `_invoice`, creating a `BudgetReceipt` with these fields:
- `document_kind="payment_confirmation"`, `status="imported"`, `source_kind="email"`;
- `raw_shop_name=merchant`, `receipt_date`, `total_amount` (`canonical_money`) and `currency`;
- `invoice_number=None`, `due_date=None`, and `import_expires_at` as for invoices.

The description is stored as exactly one `BudgetReceiptLine` with `line_kind="other"`, `raw_name=description` and amount = total, if the line schema allows that without a migration. If it does not, the implementer reports back before choosing another field.

### Cross-email dedupe

Check Gmail ID and RFC Message-ID dedupe first. The payment-specific checks then compare an existing `payment_confirmation` receipt for the same owner, `total_amount`, and `currency`.

- **Same payment-sender rule.** A date within ±7 days is `skipped_duplicate` unless both emails have order references and they differ. A missing reference on either side permits the match. This applies to shop and gateway rules.
- **Same gateway sender rule and payee.** The same payee (case-insensitively) and a date within ±2 days is `skipped_duplicate` even when both order references are present and differ. This covers registration and completion emails with the same payee.
- **Gateway and shop.** A date within ±2 days and exactly one gateway is `skipped_non_receipt`.
- Distinct shop sender rules can produce separate receipts with equal totals.

An order reference contains at least 6 letters or digits after `zamówieni[ae]( nr| numer)?`, `order( #| no\.?)?`, or `nr zamówienia` in the subject or body. Check subjects first and ignore common status words such as `received` and `potwierdzone` after a bare order label. Compare references case-insensitively. For dedupe, persist only the keyed HMAC of the reference with the matched sender-rule fingerprint in receipt-associated `AppMeta` provenance. Treat existing rows without a reference hash as missing a reference. Dedupe resolves gateway roles from the current config. Sender and order-reference fingerprints follow the Gmail provenance cleanup lifecycle.

## API, UI, contract

- `payment_confirmation` is already in `BudgetReceiptDocumentKind`. Check that the receipts list and detail UI render it with an i18n label: add the key to `en.ts`/`pl.ts` and mark the rest for Codex translation if missing.
- No new routes. The OpenAPI bump is needed only if a schema changes.

## Tests (synthetic only)

The fixtures live in `tests/fixtures/gmail/payment/*.txt`. The expected results:

| Fixture | Subject (passed by the test) | Gateway? | Expected |
|---|---|---|---|
| `gateway_p24_style.txt` | `Nowa transakcja płatnicza (P24-AAA-BBB-CCC)` | yes | 217.00 PLN, merchant `PRZYKŁADOWY SKLEP SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ`, description `Zamówienie 100200300` |
| `gateway_tpay_style.txt` | `Potwierdzenie transakcji TR-AAAA-0000000` | yes | 83.84 PLN, merchant `PRZYKŁADOWA SPÓŁKA AKCYJNA`, description `Zamówienie 0000000001` |
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
| `ride_inline_tags.txt` | `Przejazd opłacony` | no | 42.35 PLN from the tagged `Zapłacono łącznie` line |
| `gateway_fee_inclusive.txt` | `Potwierdzenie płatności` | yes | 214.70 PLN including the fee |
| `gateway_start_date_field.txt` | `Płatność zakończona` | yes | 38.12 PLN, with the transaction-start field ignored |
| `gateway_registered.txt` | `Zarejestrowaliśmy Twoje zlecenie płatnicze` | yes | 47.25 PLN, merchant `Example Studio`, description `Materiały do projektu` |
| `gateway_completed.txt` | `Przekazaliśmy Twoje pieniądze` | yes | 47.25 PLN, merchant `Example Studio`, description `Materiały do projektu` |
| `saas_gateway_receipt.txt` | `Your receipt from Example Workshop #1234-5678`, `Your Example Workshop receipt [#1234-5678]`, or `Twoje pokwitowanie od Example Workshop nr 1234-5678` | yes | 8.27 USD, merchant `Example Workshop` |
| `shop_order_updates.txt` | Repeated notices for `Zamówienie nr 00000000001`, a notice with no reference, and a notice for `Zamówienie nr 00000000002` | no | 67.18 PLN, with the two known references kept separate and repeated notices deduped |
| `food_description.txt` | `Potwierdzenie zamówienia nr 00000000001` | no | 26.42 PLN, description `Przykładowa Restauracja` |

Also required:
- disagreement, two amounts on one total line, and negative-value rejects;
- NBSP amounts;
- a marker inside an item line does not reject;
- the legal-footer capital line is ignored;
- config validation, including blank merchants;
- trust and search-term inclusion;
- repeat-order dedupe with equal, unequal, and missing references, registration/completion dedupe, distinct shops, and gateway-to-shop dedupe;
- contact tokens, marker normalization across multiple Subject headers, and the plain-text to HTML fallback;
- the pipeline over the worker socket;
- no body text in the DB;
- 100% line and branch coverage on touched modules.

## Real-data validation (operator, not CI)

Before merge, the operator runs `extract_payment` over the owner's real payment-like mail in-session and reports only aggregates: counts per outcome per sender group. The operator also spot-checks values. The synthetic fixtures do not carry the real layouts' noise (headings, prose, repeated labels), so this step is mandatory.
