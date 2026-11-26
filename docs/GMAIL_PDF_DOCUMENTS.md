# Gmail PDF documents beyond issuer templates (receipts v1f-pdf)

Status: spec, 2026-11-26. Author: holzera (operator), drawing on a survey of the owner's real mailbox (681 emails with 921 PDF attachments over 400 days, aggregates only). **Every example in this repository is synthetic.**

## Why

Today PDF text is only matched against four fixed issuer templates in `gmail_invoice.py` (T-Mobile, Play, Orange, a utility). On the real mailbox they recognise 18 of 921 PDFs. Most of the owner's real purchase documents are PDF invoices or receipts from senders without a template:

| family | approx. count | shape |
|---|---|---|
| Polish VAT invoice (grocery delivery, ride-hailing, utilities, shops) | ~150 | `Faktura VAT nr X`, `Sprzedawca:`, `NIP:`, `Data wystawienia:`, `Termin płatności:`, `Suma:` / `Wartość brutto` / `Kwota brutto` / `Kwota do zapłaty`, `Zapłacono` |
| Stripe-style SaaS invoice/receipt (AI and dev-tool vendors) | ~85 | `Invoice number X`, `Receipt number X`, `Date of issue`, `Date due`, `Total €X`, `Amount due $X USD`, `Amount paid $X`, `Bill to` |
| cloud billing (hosting, office suite) | ~25 | `Invoice number: X` or `Billing Number X`, `Date of issue:` or `Document Date dd/mm/yyyy`, `Total due $X` or `Total Amount EUR X` |
| tickets and bookings | ~20 | out of scope for v1f-pdf (later) |
| bank statements, medical documents, newsletters | many | **must not import** |

The owner's rule from v1f still applies. A payment-gateway email is evidence, and the shop's own document is the purchase document. v1f-pdf supplies those documents, so gateway rows can attach to them.

## Scope

In scope:
- a generic labelled extractor for PDF invoices and receipts from **trusted document senders**;
- deduplication of invoice + receipt pairs;
- evidence attachment, re-using v1f.

Out of scope:
- tickets and bookings;
- FX conversion (store the original currency, as v1e does);
- OCR of image-only PDFs. Empty text stays as today: no import, reason `pdf_no_text`.

## Trust: which senders' PDFs are read

- A new optional Gmail config key `document_senders`: `[{"sender": "@vendor.example.test", "merchant": "Vendor"}]`. It uses the same grammar and validation as `payment_senders`, with at most 200 entries.
- A sender may appear in both `payment_senders` and `document_senders`. The email body goes through the payment path and the PDF parts through the document path. A sender listed in `sender_allowlist` (issuer templates) may also be a document sender. The issuer template is tried first, and the generic extractor runs only when no template matches.
- PDFs from any other sender are not read.
- Forwards (spouse/owner) follow v1f.2: the inner sender must match a `document_senders` rule.

## Extraction (`gmail_document.py`, new)

`extract_document(text, *, merchant) -> ImportedDocument | None`. It works on the PDF text rendered by the existing sidecar.

### 1. Reject (return None)

- The text matches statement, medical or newsletter markers: `wyciąg`, `zestawienie operacji`, `statement of account`, `historia rachunku`, `skierowanie`, `recepta`, `e-zwolnienie`, `wynik badania`.
- The text has no document marker: `faktura`, `invoice`, `receipt`, `rachunek`, `paragon`.
- It is a correction or credit note (`korekta`, `credit note`, `faktura korygująca`). That is a later scope, the same as v1e.

### 2. Kind

- `document_kind="invoice"` unless the PDF is a receipt.
- A receipt has a `Receipt number` / `Paragon` label, or `Amount paid` / `Zapłacono` with no amount due, and becomes `document_kind="receipt"`.

### 3. Fields

All fields are labelled. Values appear on the same line or the next non-empty line, using v1e's `_following_values` semantics.

- **Number:**
  - `Faktura( VAT)? nr`, `Numer faktury:`, `Invoice number:?`, `Billing Number`, `Receipt number`;
  - when both an invoice number and a receipt number exist, keep the invoice number and set the `receipt_number` provenance;
  - at most 64 characters, stored as `invoice_number`.
- **Issue date:**
  - `Data wystawienia( faktury)?:?`, `Data faktury`, `Date of issue:?`, `Document Date`;
  - formats `YYYY-MM-DD`, `DD.MM.YYYY`, `DD/MM/YYYY` (Document Date is day-first), `Month D, YYYY`;
  - reuse v1e `_parse_date` and add `DD/MM/YYYY`.
- **Due date:** `Termin płatności:?`, `Date due`, `Payment due on:?`, stored as `due_date`.
- **Total (gross), first match wins, by priority:**
  1. `Kwota do zapłaty`, `Do zapłaty`, `Amount due`, `Total due`;
  2. `Suma:` (the line after a VAT table), `Wartość brutto`, `Kwota brutto`, `Total Amount`, `Total`;
  3. `Zapłacono`, `Amount paid`.
  - Reuse v1e's `_parse_amount` and its currency symbols: `zł`, `PLN`, `€`, `EUR`, `$`, `USD`, `GBP`.
  - Reject when the same priority level yields conflicting totals.
  - Never use `netto`/`net`/`Subtotal`/`excluding tax`/`Podatek`/`VAT` lines.
- **Seller:**
  - the `Sprzedawca:` value, `Faktura wystawiona przez`, or `From` / the first non-empty line for Stripe-style documents;
  - otherwise the configured `merchant`.
- **Seller NIP:**
  - `NIP:` directly under or after the seller block. It must pass `valid_nip`. A buyer NIP after `Nabywca:`/`NABYWCA:`/`Bill to` must not be taken.
  - EU VAT ids (`VAT ID`, `Nr VAT`, `IE…`, `CHE…`) are not NIPs and are ignored.
- **Paid flag:** `Zapłacono`, `Amount paid`, `Payment history`, or `Płatność kartą` present. Store it in provenance; it is used for matching later.

The receipt date is the issue date. The email date is only a cross-check: if the issue date is more than 400 days before or 7 days after the email date, reject.

## Storage

- `BudgetReceipt`:
  - `document_kind` invoice or receipt;
  - `status="imported"`, `source_kind="attachment"`;
  - `raw_shop_name` = seller and `raw_nip` = seller NIP;
  - `receipt_date`, `due_date`, `invoice_number`, `total_amount`, `currency`;
  - one `other` line carrying the total (as v1e does).
- **No new columns.** If the implementer finds one strictly necessary, ask the operator, who assigns the migration number. 0033 and 0034 may be taken by parallel PRs.
- **Dedupe:**
  - the RFC Message-ID first (existing);
  - then the same owner + seller key (`_payee_key`) + HMAC of the `invoice_number` (new AppMeta provenance, `budget_gmail_document_number:<receipt_id>`) = duplicate;
  - a Stripe-style "Receipt" PDF for an invoice number already imported marks that row paid (provenance only) and is `skipped_duplicate`. The reverse order works too: the invoice arrives after the receipt and becomes skipped_duplicate, with the paid flag kept.
  - A single email with two PDFs for the same number (invoice + receipt) produces ONE receipt row.
- **Evidence:** a newly imported document runs v1f's reverse attachment (`_attach_gateway_for_document`), so an already-imported gateway row for the same purchase attaches to it. The v1f rules are unchanged: exactly one candidate, never re-point confirmed rows.
- **Cap:** documents count toward the 200 pending-import cap, the same as other imports.

## Contract

There are no new fields. `document_kind` already includes `receipt` and `invoice`. If the receipt detail gains `paid` or `receipt_number`, that is a contract minor bump, which the operator assigns at merge.

## Tests (synthetic only: fake sellers, NIPs that pass the checksum but are fake, *.example.test)

Fixtures go in `tests/fixtures/gmail/documents/*.txt` as rendered PDF text. At minimum:
1. a Polish VAT invoice with a full VAT table, where `Suma:` is the gross total and `Zapłacono` is present, plus seller and buyer NIPs, of which only the seller's is taken;
2. a Polish ride receipt-invoice, `Kwota brutto X zł` / `Płatność kartą kredytową X zł`, with the issue date in `D.MM.YYYY, HH:MM`;
3. a utility settlement invoice, `Kwota do zapłaty X zł`, `Termin płatności`, with several `netto` lines that must be ignored;
4. a Stripe-style invoice in EUR, with `Subtotal`, `Total excluding tax`, a `VAT - Poland 23% on €X` line and `Total €Y` → total Y;
5. a Stripe-style receipt for the same invoice number in another email → one row, receipt flagged paid. Then the reverse order.
6. a Stripe-style invoice in USD, with `Amount due $X USD` and `Date due`;
7. a cloud invoice, `Invoice number: X`, `Date of issue: November 3, 2026`, `Total due $X`;
8. an office-suite invoice, `Billing Number`, `Document Date 03/11/2026` (day-first), `Total Amount EUR X`;
9. rejects: a bank statement, a medical document, a newsletter PDF with prices, a credit note, conflicting totals, and a sender not in `document_senders`;
10. a gateway email (v1f P24 paid, total = invoice + fee) imported before the PDF invoice → it attaches after the invoice import, in both orders;
11. config validation for `document_senders`: grammar, duplicates, the 200 limit, coexistence with `payment_senders`/`sender_allowlist`;
12. a forwarded document (v1f.2 path) from a document sender.

Coverage must be 100% line+branch.

## Operator validation (holzera, not CI)

The operator re-runs the PDF survey against the branch and reports aggregates only: per sender family, how many imported, rejected and deduplicated, and how many gateway rows attached. The operator then adds `document_senders` to the prod config.
