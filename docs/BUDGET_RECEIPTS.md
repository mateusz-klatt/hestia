# Budget Receipts: Research & Design Proposal

> **Status:** Revised design proposal v3 (Round-1 and Round-2 Grok 4.7 review findings resolved; **Q1–Q7** and operational rules **D1–D4** decided). No code, migrations, or privacy-policy edits ship with this document.  
> **Baseline:** `master` at `81a432c` (schema `0019`, OpenAPI `0.25.0`, target stack Python 3.14 / `aiohttp` at `Dockerfile:10`, `requirements.txt:24`; SQLite + SQLAlchemy 2.0 + Alembic at `hestia/db.py:10-30`, `requirements.txt:25-26`; React 19 + TypeScript at `ui/package.json:29-31`; Docker at `docker-compose.yml:1-46`).  
> **v1d Extension (2026-11-22):** Normative design for Gmail receipt import, large-expense proof, and opaque-debit enrichment added in [§13](#13-v1d--gmail-import-and-proof-for-larger-expenses) against `master` at `518e5bb` (OpenAPI `0.29.0`, privacy-policy Gmail update `2ce999f`) and PR #238 (`feat/budget-receipts-v1a` at `ed853b0`, migration `0020`, OpenAPI `0.30.0`).

---

## 1. Problem & Hestia Fit

Add an owner-private **Receipts (`Paragony`)** module to Hestia Budget so the household owner can:

1. Capture a Polish retail receipt on mobile (photo or pasted iOS Live Text).
2. Extract the shop (name / NIP), purchase date & time, payment method, `SUMA PLN`, and line items (name, quantity, unit price, discount, PTU letter, line total).
3. Match and manually confirm an **informational** link to an existing canonical bank transaction (`budget_transactions`) using exact amount, currency, nearest transaction/booking/value date (`±4` calendar days), and merchant text — without modifying `analytics_views.py` spending totals in v1.
4. *(v2)* Normalize product variants (e.g., `"ML.UHT 3,2% 1L"`, `"MLEKO WYPASIONE 3,2% 1000ML"`) to compare **price per canonical unit** across Warsaw shops (e.g., Carrefour vs. Biedronka vs. Lidl) over time.

### Non-negotiable Hestia constraints

Every design choice below inherits the existing budget module boundaries:

- **Owner-private & fail-closed auth:** Budget endpoints require `HESTIA_BUDGET_HTTP_ENABLED=1`, `HESTIA_PERSIST=sqlite`, authoritative SQLite users, and an enabled `admin` principal (`hestia/budget/api.py:53-63`). Because `hestia/web.py:245-253` resolves `_ROUTE_MIN_ROLE.get((method, path))` by exact path and defaults unclassified `GET` routes to `_VIEWER`, **every** receipt route must use a fixed path (no `{id}` path segments), expose **one** HTTP method (`POST` only for `list` and `search`), and be classified `_ADMIN` in `hestia/web.py:167-206` and `"admin"` in `hestia/api_contract.py:2286-2315`, with `allow_head=False` on reads.
- **Browser-only mutations, multipart CSRF & headers:** Every state-changing POST — including `multipart/form-data` image uploads that bypass `_read_json_body`'s JSON-only guard (`hestia/web.py:918-921`) — must run `api._principal(request, auth_enabled)` and `api._browser(request, owner, secret)` **before reading any request body bytes**, enforcing the `HttpOnly` `hestia_session` cookie, rejecting any `Authorization` header, and checking exact `Origin == HESTIA_BUDGET_PUBLIC_ORIGIN` (`hestia/budget/api.py:73-91,236-241`, `hestia/budget/analytics_api.py:109-120`). All budget responses carry `Cache-Control: no-store` and `Referrer-Policy: no-referrer` (`hestia/budget/api.py:38,214-216`).
- **Browser memory only:** No receipt images, raw text, line items, or prices in `localStorage`, `sessionStorage`, or URL query strings (`docs/BUDGET.md:247-252`, `ui/src/budget/budget.tsx:1-2`). Any `401`/`403`/`404` clears budget state immediately.
- **Exact decimals (no floats):** Monetary values, quantities, discounts, and unit prices use validated canonical decimal strings (`hestia/budget/normalize.py:98-115`) and scaled-integer arithmetic (`hestia/budget/analytics_core.py:38-87`, `docs/BUDGET_2D.md:26-29`) — never SQLite `REAL`, Python `float`, or JS `Number`.
- **Zero financial/receipt payloads in logs:** Application logs, error responses, global `/api/audit`, and SSE (`/api/events`) may record only fixed safe error codes or exception class names (`hestia/budget/api.py:208`, `hestia/budget/client.py:77-115`). Private mutations log sanitized metadata only in `budget_analytics_audit` (`hestia/db.py:703-712`, `hestia/budget/analytics_store.py:127-129`, `docs/BUDGET_2D.md:292-295`) — **never** `raw_ocr_text`, NIPs, PANs, shop names, line items, or amounts. Reverse proxies (Apache/Cloudflare) log paths and query parameters (`ui/public/privacy-policy.html:226-230`), so receipt IDs are opaque UUIDs and `list`/`search` use `POST` JSON bodies rather than `GET` query strings.
- **Read-only bank access & manual confirmation:** Linking a receipt never contacts Enable Banking, consumes acquisition quota, or alters canonical `budget_transactions` payloads (`docs/BUDGET_2D.md:180-181`). Following `hestia/budget/transfer_match.py:1-15`, matching only proposes candidates and requires explicit owner confirmation inside `BEGIN IMMEDIATE`.
- **Upload framing, body limit & decompression-bomb defense:** `make_app` currently sets `client_max_size = MAX_RULE_BODY + 4096` (`69,632` bytes in `hestia/web.py:51,1160-1163`), and `_read_json_body` enforces per-route caps (`hestia/web.py:905-935`). Raising `web.Application(client_max_size=6 * 1024 * 1024)` accommodates `6 MiB` multipart uploads while preserving existing `MAX_BODY`/`MAX_RULE_BODY` caps inside `_read_json_body`, capping `parse-text` at `48 KiB` (`49,152` B) and `confirm` at `128 KiB` (`131,072` B), requiring `Content-Length`, refusing chunked uploads, checking header pixel bounds (`<= 2000 px` long side, `<= 4 MP`) before `load()`, and serializing WebP re-encoding under a single process-wide `Semaphore(1)` (`§5.2`).
- **Privacy policy gate:** `ui/public/privacy-policy.html` must be updated **before** enabling `v1a` (to document receipt text/metadata storage and retention) and updated again **before** enabling `v1c` (to name the selected Cloud Vision provider and its retention terms, `docs/BUDGET.md:13-14,660-662`).

---

## 2. Owner Decisions & Review Resolution

### 2.1 Decided rules (delegated to engineering)

| ID | Topic | Decided Rule | Resolving Section |
| --- | --- | --- | --- |
| **Q1** | Local photo OCR engine (v1b) | **RapidOCR (`rapidocr-onnxruntime`) sidecar** under Docker Compose `profiles: ["receipts-ocr"]`, bound only to `127.0.0.1:8928` on a bridge network with shared-secret `Authorization` header. | [§4.2](#42-hardened-rapidocr-sidecar--client-contract) |
| **Q2** | Photo retention after confirmation | Images are kept for **30 days after confirmation** (`30d_after_confirm`), then automatically purged. | [§5.2](#52-photo-storage-hardening-dos-bounds-retention--log-hygiene) |
| **Q3** | Cloud AI Vision provider (v1c) | **Single configurable adapter**, with **Anthropic Claude** as the first implementation; stays off by default (`HESTIA_BUDGET_RECEIPTS_AI_ENABLED=0`), manual per-receipt opt-in only, privacy-policy update required before enabling. | [§9](#9-decisions-on-formerly-open-questions-q3q4) |
| **Q4** | Search transport (`v1`/`v2`) | **`POST /api/budget/receipts/search`** with the search query in the JSON body (never in `GET` query strings); small v1 lists may also filter client-side. | [§4.5](#45-v1-api-surface-static-_admin-routes), [§9](#9-decisions-on-formerly-open-questions-q3q4) |
| **Q5** | Analytics totals & category splits in v1 | Receipts stay **OUT** of `analytics_views.py` totals in v1; transaction links are **informational only** (no `budget_annotations` splitting until v2+). | [§4.4](#44-transaction-matching--ingestion-drift-hook-v1) |
| **Q6** | Cash receipts (`GOTÓWKA`) | **Yes** — unlinked and cash receipts can be confirmed and stored in v1 for item history and v2 price comparison. | [§4.4](#44-transaction-matching--ingestion-drift-hook-v1) |
| **Q7** | Whole-basket coupons | Kept as a **single negative line** (`line_kind = 'basket_discount'`), never allocated into item unit prices. | [§4.3](#43-polish-parser--line-total-identity) |
| **D1** | `raw_ocr_text` lifetime | PAN-redacted before save; kept until the **same 30-day deadline as the image** (no deletion on confirm; applies to all `source_kind`s including `live_text`/`cloud_ai`). Unconfirmed drafts and their `raw_ocr_text` expire after **48 hours** (implemented in `v1a`). | [§4.3](#43-polish-parser--line-total-identity), [§5.2](#52-photo-storage-hardening-dos-bounds-retention--log-hygiene) |
| **D2** | Matching window & dates | **`±4` calendar days**, measured as the minimum distance across `transaction_date`, `booking_date`, and `value_date`. | [§4.4](#44-transaction-matching--ingestion-drift-hook-v1) |
| **D3** | Exact amount linking | Transaction links require an **exact canonical amount and currency match**; mixed/split payments save unlinked. | [§4.4](#44-transaction-matching--ingestion-drift-hook-v1) |
| **D4** | `/data` disk quota | Dedicated `ReceiptFiles` store enforces a per-owner **byte quota** (`256 MiB`) and **draft-count quota** (`20` active drafts). | [§5.2](#52-photo-storage-hardening-dos-bounds-retention--log-hygiene) |

### 2.2 Round 1 review resolution (Grok 4.7 → revised sections)

| Finding | Summary of Fix | Resolving Section(s) |
| --- | --- | --- |
| **#1 CRITICAL** (`network_mode: host` + exposed sidecar) | Place sidecar on a Compose bridge network publishing only `127.0.0.1:8928:8928`; mount no `/data`, Docker socket, or DB; `cap_drop: [ALL]`, non-root user, `mem_limit`, `pids_limit`, `read_only`; shared-secret `Authorization` header; constant loopback URL, no redirects, short timeout, capped response in client; sidecar logs only status and duration. | [§4.2](#42-hardened-rapidocr-sidecar--client-contract) |
| **#2 HIGH** (Image decode DoS & `ConsentFiles` mismatch) | Require `Content-Length`, refuse chunked uploads, cap multipart at `6 MiB`; sniff magic bytes (JPEG/PNG/WebP only); verify header dimensions (`<= 2000 px` long side, `<= 4 MP`) on every frame before `load()`; treat `DecompressionBombWarning` as error; re-encode to WebP under `Semaphore(1)` in worker thread and pass only re-encoded bytes; enforce `/data` byte + draft quotas; build dedicated `ReceiptFiles` instead of `ConsentFiles` (`1 MiB` JSON limit). | [§1](#1-problem--hestia-fit), [§4.5](#45-v1-api-surface-static-_admin-routes), [§5.2](#52-photo-storage-hardening-dos-bounds-retention--log-hygiene) |
| **#3 HIGH** (Multipart CSRF & `_ROUTE_MIN_ROLE` fallback) | Call `_principal` and `_browser` (`Origin` + `hestia_session`, reject `Authorization`) before reading multipart/JSON bodies; use fixed route paths (no `{id}` segments) registered `_ADMIN` in `_ROUTE_MIN_ROLE` (`hestia/web.py:167-206`) and `_PATH_ROLES` (`hestia/api_contract.py:2286-2315`) with `allow_head=False`. | [§1](#1-problem--hestia-fit), [§4.5](#45-v1-api-surface-static-_admin-routes) |
| **#4 HIGH** (`raw_ocr_text`/audit leak, image fetch & privacy policy) | Redact PAN/card strings before storage; expire `raw_ocr_text` on 30d deadline; store only `{receipt_id, action, status, version, linked, source_kind}` in `budget_analytics_audit`; update `privacy-policy.html` before `v1a` and again before `v1c`; serve `GET .../image` via `dir_fd` + `O_NOFOLLOW` with `no-store`, `nosniff`, `image/webp`, and verify `cf-cache-status`. | [§1](#1-problem--hestia-fit), [§4.5](#45-v1-api-surface-static-_admin-routes), [§5.1](#51-relational-schema--alembic-migrations-0020--0021), [§5.2](#52-photo-storage-hardening-dos-bounds-retention--log-hygiene) |
| **#5 HIGH** (Matching bugs & stale-link hook) | Use min distance across `transaction_date`, `booking_date`, `value_date` (`<= 4` days); match refunds only to `CRDT` and exclude `budget_transfer_legs`; reload & re-verify inside `BEGIN IMMEDIATE`; store `financial_fingerprint`; save mixed payments unlinked; map `IntegrityError` to `409`; latch `needs_review` on ingestion/transfer changes. | [§4.4](#44-transaction-matching--ingestion-drift-hook-v1), [§4.5](#45-v1-api-surface-static-_admin-routes) |
| **#6 HIGH** (Line-item double-counting) | Enforce single exact identity `sum_money(net_line_total) == total_amount`; item discounts live only in `discount_amount`; basket discounts are single negative `basket_discount` lines; deposits are `deposit` lines; cap lines (`300`) and `raw_name` length (`128`). | [§4.3](#43-polish-parser--line-total-identity) |
| **#7 HIGH** (Broken `0020`/`0021` migrations & delete order) | Split `0020` (shops/receipts/lines only, no v2 FKs) from `0021` (products/aliases + `batch_alter_table`); make draft receipt fields and `chain_key` nullable with status-gated `CHECK`s; index `uq_budget_receipts_tx_link` only on `status IN ('confirmed', 'needs_review')`; child-first downgrades and delete order (`lines -> receipt -> image file`). | [§5.1](#51-relational-schema--alembic-migrations-0020--0021) |
| **#8 MEDIUM** (Missing test specification) | Add dedicated `§10` listing named security, matching, persistence, privacy, and migration tests plus CI coverage gates. | [§10](#10-testing--verification-plan-required-synthetic-cases) |
| **#9 MEDIUM** (Open questions out of sync) | Move decided questions to `§2.1` and record Q3/Q4 decisions in `§9`. | [§2.1](#21-decided-rules-delegated-to-engineering), [§9](#9-decisions-on-formerly-open-questions-q3q4) |

### 2.3 Round 2 review resolution (Grok 4.7 → revised sections)

| Finding | Summary of Fix | Resolving Section(s) |
| --- | --- | --- |
| **R2-1** (Retention contradiction across `D1`, `§4.3`, `§5.2`, test #12) | Standardize on **one rule**: PAN-redacted `raw_ocr_text` lives until the same **30-day** post-confirm deadline as the image (**no deletion on confirm**); unconfirmed drafts + text expire at **48 hours** (sweep + purge test ship in `v1a`). | [§2.1](#21-decided-rules-delegated-to-engineering), [§5.1](#51-relational-schema--alembic-migrations-0020--0021), [§5.2](#52-photo-storage-hardening-dos-bounds-retention--log-hygiene), [§10](#10-testing--verification-plan-required-synthetic-cases), [§11](#11-effort-estimate) |
| **R2-2** (`needs_review` latch hook locations) | Fire `reconcile_receipt_links` from the committing import transactions (`store.py:441` — covering the `530-539` `uncertain=True` early return as well as `546-547` — and offline `store.py:576,603`, covering `665-671` & `674-681`), and from `decide_transfer` after `budget_transfer_legs` changes (`analytics_store.py:600-606`); rewire Test #11 to all three paths. | [§4.4](#44-transaction-matching--ingestion-drift-hook-v1), [§10](#10-testing--verification-plan-required-synthetic-cases) |
| **R2-3** (Body limits, `confirm` validation, positive refund total) | Raise `parse-text` body cap to **`48 KiB` (`49,152` B)** and `confirm` cap to **`128 KiB` (`131,072` B)**; validate per-line `net = gross - discount`, line-kind signs, positive `total_amount` (even on refunds, where only `receipt_kind` selects `CRDT`), and `sum(net_line_total) == total_amount`; add rejection tests #16a/#16b. | [§1](#1-problem--hestia-fit), [§4.3](#43-polish-parser--line-total-identity), [§4.4](#44-transaction-matching--ingestion-drift-hook-v1), [§4.5](#45-v1-api-surface-static-_admin-routes), [§10](#10-testing--verification-plan-required-synthetic-cases) |
| **R2-4** (`kind == excluded` wrongly filtered out) | Remove `kind == 'excluded'` exclusion (`analytics_views.py:316` is for transfer matching; excluded card purchases can still link receipts); keep exclusion of `budget_transfer_legs` (`hestia/db.py:694-700`) and add Test #17. | [§4.4](#44-transaction-matching--ingestion-drift-hook-v1), [§10](#10-testing--verification-plan-required-synthetic-cases) |
| **R2-5** (One method per route; `POST` only for `list`/`search`; `§9` cleanup) | Remove `"or GET"` from `list`; add `POST /api/budget/receipts/search` to `§4.5`, `_ROUTE_MIN_ROLE`, and `_PATH_ROLES`; remove stale `"questions remain open"` paragraph and leftover default line from `§9`. | [§1](#1-problem--hestia-fit), [§4.5](#45-v1-api-surface-static-_admin-routes), [§9](#9-decisions-on-formerly-open-questions-q3q4) |
| **R2-Minor** (Diagram paths, startup semaphore, secret mount on `hestia`) | Rename diagram endpoints in `§4.1` to `POST /parse-text` (`JSON ≤48 KiB`) and `POST /parse-image`; specify one process-wide `_IMAGE_DECODE_SEMAPHORE = threading.Semaphore(1)` created at module/app startup; mount shared secret `:ro` in both `receipt-ocr` and `hestia` in `§4.2`. | [§4.1](#41-pipeline-overview), [§4.2](#42-hardened-rapidocr-sidecar--client-contract), [§5.2](#52-photo-storage-hardening-dos-bounds-retention--log-hygiene) |

---

## 3. Polish Fiscal Receipt (*Paragon Fiskalny*) & OCR Options

### 3.1 Anatomy of a Polish thermal receipt

Under the Polish Ministry of Finance regulation on cash registers (*Rozporządzenie Ministra Finansów w sprawie kas rejestrujących*), a fiscal receipt follows a predictable structure:

1. **Seller header & NIP:** Legal entity + branch address (e.g., `JERONIMO MARTINS POLSKA S.A.`, `CARREFOUR POLSKA SP. Z O.O.`) and mandatory **10-digit seller NIP** (verified chain examples: Biedronka `7791011327`, Carrefour `9370008168`, Lidl `7811897358`), validated via mod-11 checksum (`weights = (6,5,7,2,3,4,5,6,7)`, digit 10 == sum mod 11, rejecting remainder 10). Franchise chains (Żabka, Carrefour Express) print the franchisee's NIP, so shop detection combines NIP lookup with header rules and owner-editable `budget_shops` rows.
2. **Marker & timestamp:** `PARAGON FISKALNY`, sequential receipt/print number, date and time (`YYYY-MM-DD HH:MM` or `DD-MM-YYYY HH:MM`).
3. **Item, discount & deposit lines:** Abbreviated product name + qty × unit price + line total + **PTU letter `A`–`G`** (`A=23%`, `B=8%`, `C=5%`, `D=0%`, `E=zw.`, `F/G=0%` tech.), including two-line items with a `Rabat`/`Opust` line underneath, weighed produce (`1,420 kg x 3,99`), and `Kaucja` bottle/can deposits (`0,50` / `1,00` PLN).
4. **Totals & payment footer:** `SPRZEDAŻ OPODATKOWANA`, `SUMA PTU`, **`SUMA PLN`** (or `DO ZAPŁATY`), payment method (`KARTA PŁATNICZA` / `BLIK` / `GOTÓWKA`), optional masked card PAN (redacted before persistence), and fiscal logo `#` + cash-register ID.

Existing open-source tools ([`filip-rupniewski/receipt_parsing`](https://github.com/filip-rupniewski/receipt_parsing) for Swiss chains, [`KamKubicki/fidl-plus`](https://github.com/KamKubicki/fidl-plus) for Lidl Plus HTML/JSON, [`bhimrazy/receipt-ocr`](https://github.com/bhimrazy/receipt-ocr), [`Rutikasri/DocuParse_AI`](https://github.com/Rutikasri/DocuParse_AI), [`RysOCR`](https://github.com/PaddlePaddle/PaddleOCR) VL LoRA, and MF [`eParagonInf`](https://www.podatki.gov.pl/e-paragony/)) do not provide a lightweight, self-hosted paper parser for Polish retail receipts. Hestia therefore uses a pure-Python deterministic parser (`hestia/budget/receipt_parser.py`).

### 3.2 Input & OCR options comparison

| Option / Path | Polish accuracy on thermal receipts | Home-server CPU & RAM (no GPU) | Docker availability | Licence | Maintenance | Line-item extraction | Privacy |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **(c) iOS Live Text (paste text)** | **Medium–High** on sharp photos; Apple Feature Availability lists Polish among >40 Live Text locales, though thermal fonts can flatten `ł`/`ą`/`ę` | **~0 MB RAM, <5 ms CPU** on server | Built into Hestia (zero deps) | Proprietary OS feature on user's iPhone | Active (Apple iOS 15+) | **Medium** — copy-paste can wrap/merge columns | **100% on-device** until pasted |
| **(a1) Tesseract OCR (`pol` `tessdata_best`)** | **Low–Medium** default; **Medium** with upscaling, adaptive thresholding, `--psm 4`/`6` | **50–150 MB RAM**; 1–3 s/receipt | `apt-get` (+60–100 MB layer) | **Apache-2.0** | Active (`tesseract-ocr/tesseract`) | **Low–Medium** without TSV bounding-box grouping | **100% local** |
| **(a2) OCRmyPDF** | Same as Tesseract (`-l pol --clean --deskew`) | **300 MB–1.5 GB peak RAM**; 2–5 s | Official `jbarlow83/ocrmypdf` | **MPL-2.0** (webservice **AGPLv3**) | Active (`ocrmypdf/OCRmyPDF`) | **Low–Medium** — PDF archival focus | **100% local** |
| **(a3) pdfsandwich** | Same as Tesseract + `unpaper` | ~200–600 MB RAM; slow | Community only | **GPL-2.0** | **Stalled** (v0.1.7, Aug 2018) | **Low** — PDF output only | **100% local** |
| **(d1) Stirling-PDF** | Same as Tesseract; requires img→PDF before `/api/v1/misc/ocr-pdf` | **~500 MB idle, 2–4 GB recommended** (JVM + Tesseract + LibreOffice) | Official `stirlingtools/stirling-pdf` | **Open-core** (MIT core + proprietary modules) | Very active | **Low** — returns PDF/hOCR, heavy wrapper | **100% local** |
| **(d2) Paperless-ngx** | Same as OCRmyPDF / Tesseract | **300–800 MB idle, 1.5–2+ GB peak/worker** (Django + Redis + Celery) | Official `ghcr.io/paperless-ngx/paperless-ngx` | **GPL-3.0** | Very active | **Low** — full DMS, not a receipt extractor | **100% local** |
| **(d3a) PaddleOCR (`PP-OCRv4`/`v5`)** | **High** detection (`DBNet`) on crumpled receipts; `lang='pl'` / `PP-OCRv5-latin` | **0.5–1.5 GB RAM** CPU (`PP-OCRv4`/`v5`); **4–16 GB** (`PaddleOCR-VL`) | Official `paddlepaddle/paddle` (**4–8 GB** images) | **Apache-2.0** | Very active | **High** — polygon bounding boxes + confidence | **100% local** |
| **(d3b) RapidOCR (`rapidocr-onnxruntime`)** | **High** detection + **Medium–High** Polish recognition (ONNX Runtime `PP-OCR` weights with `latin`/`pl` dict) | **200–500 MB RAM**, ~0.3–1.2 s/receipt on CPU | Slim sidecar (~350–500 MB on `python:3.11-slim`) | **Apache-2.0** | Active (`RapidAI/RapidOCR`) | **High** — same bounding boxes at ~1/3 RAM | **100% local** |
| **(d4) docTR (`mindee/doctr`)** | **Medium–High** with `VOCABS["polish"]` | **1.5–4 GB RAM** (PyTorch CPU); 3–10+ s | Official `ghcr.io/mindee/doctr` | **Apache-2.0** | Active | **High** — page/block/line/word hierarchy | **100% local** |
| *(Note)* EasyOCR / Surya | EasyOCR: Medium; Surya: High | EasyOCR **1–3 GB**; Surya **4–8+ GB** | Community / custom | EasyOCR **Apache-2.0**; Surya **Apache-2.0** / **OpenRAIL-M** (<$5M) | Active | High | **100% local** |
| **(b) Cloud AI Vision (Claude / Gemini Paid)** | **Very High** on crumpled receipts, diacritics, and `Rabat` blocks | **~0 MB local OCR RAM** | None | Proprietary API | Active | **Very High** — native JSON Schema (`output_config.format` / `responseJsonSchema`) | **Third-party cloud** (explicit opt-in only) |

### Plain-language privacy trade-off for Cloud AI Vision (Path b)

Sending a receipt photo to an external AI API discloses granular household purchasing history (groceries, alcohol, OTC pharmacy items, store branch, timestamp, loyalty/card fragments) outside the host:
- **Anthropic Claude API (Commercial Terms):** Does **not** train on API inputs/outputs by default; retains inputs/outputs up to **30 days** for abuse/safety enforcement absent a Zero Data Retention agreement ([Commercial Terms](https://www.anthropic.com/legal/commercial-terms), [Vision Docs](https://docs.anthropic.com/en/docs/build-with-claude/vision)).
- **Google Gemini API:** Paid Tier (Cloud Billing enabled) does **not** use prompts/images for training, but logs them for **55 days** for abuse monitoring; Unpaid/Free Tier allows training and human review outside EEA/UK/CH ([Gemini Terms](https://ai.google.dev/gemini-api/terms), [Structured Output Docs](https://ai.google.dev/gemini-api/docs/structured-output)).

**Policy rule:** Cloud Vision stays **disabled by default**, requires `HESTIA_BUDGET_RECEIPTS_AI_ENABLED=1` plus an **explicit per-receipt user opt-in** with warning text, uses paid/commercial keys only (never free tier), sends only EXIF-stripped WebP bytes (never bank data), and requires updating `ui/public/privacy-policy.html` first.

---

## 4. Recommended v1 Architecture

### 4.1 Pipeline overview

All routes gate behind `HESTIA_BUDGET_RECEIPTS_ENABLED=1` (default off) + `HESTIA_BUDGET_HTTP_ENABLED=1` + `HESTIA_PERSIST=sqlite` + enabled `admin` owner:

```text
  [iOS Live Text / Clipboard] ──(POST /parse-text, JSON ≤48 KiB, text ≤32 KiB)──┐
                                                                                 ▼
  [Mobile Photo] ──(POST /parse-image, multipart ≤6 MiB)                [PAN Redactor + Polish Parser]
     └─ Browser <canvas> resize → Server magic/4MP/Semaphore(1) WebP             │
          ├─ Default (Q1): RapidOCR sidecar (127.0.0.1:8928, bridge) ────────────┤
          │                └─ Geometric y-band + x-sort row builder ─────────────┘
          └─ Explicit per-receipt opt-in (AI flag=1):                            │
                           Cloud Vision Paid API (JSON Schema) ──────────────────┘
                                                                                 ▼
                                                               [Candidate Matcher (±4d, exact amount)]
                                                                                 ▼
                                                               [Owner Review & BEGIN IMMEDIATE Confirm]
                                                               (POST /confirm, JSON ≤128 KiB)
```

### 4.2 Hardened RapidOCR sidecar & client contract

Because `hestia` runs with `network_mode: host` (`docker-compose.yml:10`), Docker Compose ignores `ports:` on `hestia` itself and any sidecar listening on `0.0.0.0:8928` with host networking or unfiltered port publishing would expose an unauthenticated OCR endpoint on every network interface, bypassing Apache, Cloudflare, and `hestia_session`.

**Mandatory sidecar topology (`docker-compose.yml`):**

```yaml
networks:
  receipt_ocr_net:
    driver: bridge

services:
  hestia:
    # Existing host-networked service (`network_mode: host`, `docker-compose.yml:10`)
    environment:
      HESTIA_BUDGET_RECEIPTS_OCR_SECRET_FILE: /run/secrets/receipt_ocr_secret
    volumes:
      # Mount the SAME shared secret read-only (`0400`/`0600`, uid 1000) in `hestia`
      - /path/to/secrets/receipt_ocr_secret:/run/secrets/receipt_ocr_secret:ro

  receipt-ocr:
    profiles: ["receipts-ocr"]
    build: ./ocr-sidecar
    container_name: hestia-receipt-ocr
    networks: [receipt_ocr_net]
    # Publish ONLY to host loopback; inside the container the HTTP server binds 0.0.0.0:8928
    # so Docker bridge forwarding delivers host 127.0.0.1:8928 traffic to the container.
    ports:
      - "127.0.0.1:8928:8928"
    user: "1000:1000"
    read_only: true
    tmpfs:
      - /tmp:rw,noexec,nosuid,size=64m
    cap_drop: ["ALL"]
    security_opt: ["no-new-privileges:true"]
    mem_limit: 768m
    pids_limit: 64
    restart: unless-stopped
    environment:
      OCR_SHARED_SECRET_FILE: /run/secrets/receipt_ocr_secret
    # Mount ONLY the shared secret read-only — NEVER /data, NEVER docker.sock, NEVER hestia.db.
    volumes:
      - /path/to/secrets/receipt_ocr_secret:/run/secrets/receipt_ocr_secret:ro
```

**Sidecar & client invariants:**

- **Isolation:** Sidecar gets no `/data` mount, no `/var/run/docker.sock`, no SQLite DB access, `cap_drop: [ALL]`, `no-new-privileges:true`, non-root `1000:1000`, `read_only: true`, `mem_limit: 768m`, `pids_limit: 64`.
- **Shared secret:** Both `hestia` and `receipt-ocr` mount and read the same server-only secret file (`0400`/`0600`, matching `hestia/budget/config.py:171-195`). Hestia sends `Authorization: Bearer <secret>` to the sidecar, which verifies it with `hmac.compare_digest` and rejects missing/invalid headers with `401`.
- **Hardened client (`hestia/budget/ocr_client.py`):** Uses a constant loopback URL (`http://127.0.0.1:8928/ocr` — never derived from request inputs), disables redirects (`_NoRedirect` + `ProxyHandler({})` per `hestia/budget/client.py:118-120`), enforces a short timeout (`15 s` request deadline), and caps the OCR JSON response body at `2 MiB`.
- **Zero-content sidecar logging:** Sidecar runs with `access_log=None` (or logs exclusively `status=<int> duration_ms=<int>`) — never image sizes, headers, bounding boxes, or OCR text.

### 4.3 Polish parser & line-total identity

`hestia/budget/receipt_parser.py` (pure Python, zero network/DB):

- **Unicode & PAN redaction:** Normalizes `NFC` and constructs a diacritic-folded lookup key (`ą→a, ć→c, ę→e, ł→l, ń→n, ó→o, ś→s, ź→z, ż→z`). Before persisting `raw_ocr_text` on any path (`live_text`, `local_ocr`, `cloud_ai`), applies a **PAN redactor** masking 12–19 digit sequences, masked card PANs (`****1234`, `5351********1234`), and `NR KARTY` / `AID` terminal blocks. Redacted `raw_ocr_text` expires on the same **30-day** post-confirm deadline as the image (not cleared on confirm; `§5.2`).
- **Bounded inputs:** Caps receipts at **`MAX_RECEIPT_LINES = 300`**, **`MAX_LINE_NAME_LEN = 128`**, **`MAX_SHOP_NAME_LEN = 128`**, and pasted text at **`32 KiB` (`32,768` B) / `500` lines** inside a **`48 KiB` (`49,152` B)** `parse-text` JSON body.
- **Disjoint line representation & strict `confirm` validation:**
  - `line_kind = 'item'`: `gross_line_total >= 0`, `discount_amount >= 0`, `net_line_total = gross_line_total - discount_amount >= 0` (item-level `Rabat`/`Opust` lives **only** in `discount_amount`).
  - `line_kind = 'deposit'`: non-zero bottle/can `Kaucja` addition (`> 0`) or return voucher deduction (`< 0`), `discount_amount = '0'`, `net_line_total = gross_line_total`.
  - `line_kind = 'basket_discount'`: per **Q7**, whole-basket coupons are **strictly negative** (`gross_line_total < 0`, `discount_amount = '0'`, `net_line_total = gross_line_total`).
  - `line_kind = 'other'`: `discount_amount = '0'`, `net_line_total = gross_line_total`.
  - **Receipt total & reconciliation identity:** `receipt.total_amount` is always a strictly positive canonical decimal string (`> 0`), **even for refunds** (matching non-negative ledger amounts at `hestia/budget/normalize.py:106-108,120` and `hestia/budget/analytics_core.py:120`), while `receipt_kind` selects `DBIT` vs. `CRDT`. For every `confirm` submission, the server validates **every** line's `net_line_total == gross_line_total - discount_amount`, every `line_kind` sign rule, `total_amount > 0`, and the single identity:
    $$\sum_{i} \text{net\_line\_total}_i \equiv \text{receipt.total\_amount}$$
    Any mismatch on `confirm` is rejected (`400 budget_receipt_total_mismatch`); on `parse-*` drafts, `math_delta` is surfaced on Review without fudging numbers.

### 4.4 Transaction matching & ingestion drift hook (v1)

Per **Q5**, **Q6**, **D2**, and **D3**, matching creates informational receipt↔transaction links only and **never** alters `analytics_views.py` spending/category totals in v1:

- **Candidate pool:** Owner's selected accounts (`BudgetAccount.enabled == True`), `BudgetTransaction.uncertain == False`, `payload["status"] == "BOOK"`, **excluding** rows reserved in `budget_transfer_legs` (`hestia/db.py:694-700`) and excluding transactions already linked to a `confirmed` or `needs_review` receipt. Transactions annotated/ruled as `kind == 'excluded'` are **NOT** excluded — an excluded card purchase is still the owner's payment that may link a receipt.
- **Hard match filters (all mandatory):**
  1. **Direction by `receipt_kind`:** `'purchase'` (`total_amount > 0`) matches **only** `'DBIT'`; `'refund'` (`ZWROT`, `total_amount > 0`) matches **only** `'CRDT'`.
  2. **Exact amount & currency (D3):** `canonical_money(tx.amount) == canonical_money(receipt.total_amount)` and `tx.currency == receipt.currency`. Mixed/split payments where bank charge differs from `receipt.total_amount` save **unlinked** (`linked_* = NULL`).
  3. **`±4`-day minimum date distance (D2):** Gather all non-null valid ISO dates from `(payload.get("transaction_date"), payload.get("booking_date"), payload.get("value_date"))` (`hestia/budget/normalize.py:132-133`) and require `min(abs((date.fromisoformat(d) - receipt_date).days) for d in tx_dates) <= 4`. A Friday purchase (`2026-11-20`) booked Monday (`2026-11-23`) has distance `3` days (or `0` via `transaction_date`) and matches reliably.
- **Ranking (suggestion only):** Rank by `(merchant_token_hit DESC, min_date_distance ASC, entry_reference ASC)`, combining NIP-resolved chain (`BIEDRONKA`, `CARREFOUR`, `LIDL`) and header tokens against `" ".join(remittance_information)` + `BudgetTransactionMetadata.creditor_name`. Cash receipts (`GOTÓWKA`) default to `"Cash / No bank link"` (**Q6**).
- **Atomic server verification inside `BEGIN IMMEDIATE`:** `confirm` ignores client assertions about validity and reloads `BudgetAccount`, `BudgetConnection`, `BudgetTransaction`, and `BudgetTransferLeg` inside `BEGIN IMMEDIATE` (mirroring `hestia/budget/analytics_store.py:109-125,549-606`):
  - Re-verifies `account.owner == owner`, `connection.owner == owner`, `account.enabled == True`, `tx.uncertain == False`, `BOOK`, not in `budget_transfer_legs`, matching `DBIT`/`CRDT` direction, exact amount/currency, and `<= 4`-day date window.
  - Persists `linked_fingerprint = financial_fingerprint(payload)` (`hestia/budget/analytics_core.py:115-125`).
  - Catches SQLite `IntegrityError` on `uq_budget_receipts_tx_link` (concurrent confirms racing on the same bank entry) and returns **`409 budget_receipt_tx_already_linked`**.
- **Ingestion & transfer drift hook (`needs_review`):** Analogous to `reconcile_transfers` (`hestia/budget/store.py:441,576,603`, `hestia/budget/analytics_reconcile.py:85-100`), add `reconcile_receipt_links(session, owner, now)` invoked inside the **committing transactions** that mutate ledger state or transfer legs:
  1. `BudgetStore.commit_snapshot` (`hestia/budget/store.py:441`) right alongside `reconcile_transfers` — covering both the `uncertain=True` early return (`store.py:530-539`) and canonical `payload_json` replacement (`store.py:546-547`).
  2. `BudgetStore.import_snapshot` (`hestia/budget/store.py:576,603`) — covering duplicate-batch runs, `_offline_conflict` (`store.py:665-671`), and `_update_offline` (`store.py:674-681`).
  3. `BudgetAnalyticsStore.decide_transfer` (`hestia/budget/analytics_store.py:600-606`) inside `BEGIN IMMEDIATE` right after updating `budget_transfer_legs`.
  If a linked transaction becomes missing, `uncertain`, non-`BOOK`, enters `budget_transfer_legs`, or its recomputed `financial_fingerprint(payload) != receipt.linked_fingerprint`, latch `receipt.status = 'needs_review'` and increment `receipt.version`.

### 4.5 v1 API surface (static `_ADMIN` routes)

Every route uses a fixed static path and **exactly one HTTP method**, registered `_ADMIN` in `hestia/web.py:167-206` and `"admin"` in `hestia/api_contract.py:2286-2315`, with `allow_head=False` on `GET`:

| Route | Auth / CSRF Order | Purpose & Bounds |
| --- | --- | --- |
| `POST /api/budget/receipts/parse-text` | `_principal` → `_browser` (`Origin` + cookie, no `Authorization`) **before** `_read_json_body(request, 49152)` | `48 KiB` JSON body (`text <= 32 KiB`, `<= 500` lines); redacts PANs, creates `draft` (respecting 20-draft quota), sets `raw_text_expires_at = now + 48h`, returns `{receipt_id, draft, candidates, version}`. |
| `POST /api/budget/receipts/parse-image` | `_principal` → `_browser` (`Origin` + cookie, no `Authorization`) **before** checking `Content-Length` / rejecting chunked / reading multipart (`<= 6 MiB`) | Sniffs magic bytes, enforces `<= 2000 px` / `<= 4 MP` / single-frame before `load()`, re-encodes WebP under process-wide `Semaphore(1)`, saves to `ReceiptFiles` within `256 MiB` quota, runs RapidOCR (or opt-in Cloud AI if `AI_ENABLED=1` and `use_cloud_ai=true`), returns draft + candidates. |
| `POST /api/budget/receipts/confirm` | `_principal` → `_browser` → `_read_json_body(request, 131072)` | `128 KiB` JSON body (handles up to `300` lines × `128` chars + metadata); validates every line's `net = gross - discount`, line-kind signs, positive `total_amount`, `sum(net_line_total) == total_amount`, `expected_version` CAS, and optional `link` inside `BEGIN IMMEDIATE`; sets `image_expires_at = raw_text_expires_at = confirmed_at + 30d`; maps `IntegrityError` to `409`; writes sanitized `budget_analytics_audit`. |
| `POST /api/budget/receipts/list` | `_principal` → `_browser` → `_read_json_body(request, 16384)` | **POST only** (no `GET` alias); keyset-paged owner receipts (`month`, `status`, `linked` filters in JSON body). |
| `POST /api/budget/receipts/search` | `_principal` → `_browser` → `_read_json_body(request, 16384)` | **POST only** (Q4); searches owner receipts / products using query text in JSON body so search terms never appear in reverse-proxy `GET` logs (`ui/public/privacy-policy.html:228`). |
| `GET /api/budget/receipts/detail` | `_principal` (`?receipt_id=<uuid>`, `allow_head=False`) | Returns owned receipt + lines + `image_available: bool`; foreign/unknown ID returns `404`. |
| `GET /api/budget/receipts/image` | `_principal` (`?receipt_id=<uuid>`, `allow_head=False`) | Opens WebP via `ReceiptFiles` (`dir_fd` + `O_NOFOLLOW`), returns `Content-Type: image/webp`, `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'none'`. Verify `cf-cache-status` is `DYNAMIC`/`BYPASS`. |
| `POST /api/budget/receipts/delete` | `_principal` → `_browser` → `_read_json_body(request, 16384)` | CAS version check; deletes `budget_receipt_lines` → `budget_receipts` → commits → unlinks image file. |

---

## 5. Rough v2 & Proposed Data Model

### 5.1 Relational schema & Alembic migrations (`0020` + `0021`)

New tables require owner-reviewed Alembic migrations matching `hestia/db.py` conventions (`PRAGMA foreign_keys=ON` at `hestia/db.py:757`, no `CASCADE`, strict `owner` FKs, exact decimal strings). **v1 (`0020`) must have zero references to v2 (`0021`) tables.**

#### Migration `0020_budget_receipts` (v1)

```sql
-- 1. shop (table: budget_shops) — v1
CREATE TABLE budget_shops (
    id              TEXT PRIMARY KEY,
    owner           TEXT NOT NULL REFERENCES users(username),
    name            TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 128),
    chain_key       TEXT NULL CHECK (chain_key IS NULL OR (length(chain_key) BETWEEN 1 AND 64)),
    nip             TEXT NULL CHECK (nip IS NULL OR (length(nip) = 10 AND nip GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]')),
    merchant_tokens TEXT NOT NULL DEFAULT '[]',
    version         INTEGER NOT NULL CHECK (version >= 1),
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    UNIQUE (id, owner)
);
CREATE INDEX ix_budget_shops_owner_chain ON budget_shops(owner, chain_key);

-- 2. receipt (table: budget_receipts) — v1
-- Draft-tolerant nullability with status-gated CHECK constraints.
CREATE TABLE budget_receipts (
    id                    TEXT PRIMARY KEY,
    owner                 TEXT NOT NULL REFERENCES users(username),
    shop_id               TEXT NULL,
    raw_shop_name         TEXT NULL CHECK (raw_shop_name IS NULL OR length(raw_shop_name) <= 128),
    raw_nip               TEXT NULL,
    receipt_kind          TEXT NOT NULL DEFAULT 'purchase' CHECK (receipt_kind IN ('purchase', 'refund')),
    receipt_date          TEXT NULL CHECK (receipt_date IS NULL OR (length(receipt_date) = 10 AND receipt_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')),
    receipt_time          TEXT NULL,
    total_amount          TEXT NULL CHECK (total_amount IS NULL OR length(total_amount) BETWEEN 1 AND 80),
    currency              TEXT NULL CHECK (currency IS NULL OR (length(currency) = 3 AND currency GLOB '[A-Z][A-Z][A-Z]')),
    payment_method        TEXT NULL CHECK (payment_method IS NULL OR payment_method IN ('card', 'blik', 'cash', 'mixed', 'unknown')),
    status                TEXT NOT NULL CHECK (status IN ('draft', 'confirmed', 'needs_review')),
    source_kind           TEXT NOT NULL CHECK (source_kind IN ('live_text', 'local_ocr', 'cloud_ai')),
    image_relpath         TEXT NULL,
    image_expires_at      REAL NULL,
    raw_ocr_text          TEXT NULL,               -- PAN-redacted; kept 30d after confirm (or 48h for drafts; no delete on confirm)
    raw_text_expires_at   REAL NULL,
    linked_account_id     TEXT NULL,
    linked_entry_ref      TEXT NULL,
    linked_fingerprint    TEXT NULL,
    version               INTEGER NOT NULL CHECK (version >= 1),
    created_at            REAL NOT NULL,
    updated_at            REAL NOT NULL,
    CHECK (status = 'draft' OR (receipt_date IS NOT NULL AND total_amount IS NOT NULL AND currency IS NOT NULL AND payment_method IS NOT NULL)),
    CHECK ((linked_account_id IS NULL AND linked_entry_ref IS NULL AND linked_fingerprint IS NULL)
           OR (status != 'draft' AND linked_account_id IS NOT NULL AND linked_entry_ref IS NOT NULL AND linked_fingerprint IS NOT NULL)),
    FOREIGN KEY (shop_id, owner) REFERENCES budget_shops(id, owner),
    FOREIGN KEY (linked_account_id, linked_entry_ref)
        REFERENCES budget_transactions(account_id, entry_reference),
    UNIQUE (id, owner)
);
CREATE INDEX ix_budget_receipts_owner_date ON budget_receipts(owner, receipt_date DESC, id);
CREATE UNIQUE INDEX uq_budget_receipts_tx_link
    ON budget_receipts(linked_account_id, linked_entry_ref)
    WHERE status IN ('confirmed', 'needs_review')
      AND linked_account_id IS NOT NULL
      AND linked_entry_ref IS NOT NULL;

-- 3. receipt_line (table: budget_receipt_lines) — v1 (no v2 FK here)
CREATE TABLE budget_receipt_lines (
    receipt_id            TEXT NOT NULL,
    line_no               INTEGER NOT NULL CHECK (line_no BETWEEN 1 AND 300),
    owner                 TEXT NOT NULL REFERENCES users(username),
    line_kind             TEXT NOT NULL CHECK (line_kind IN ('item', 'deposit', 'basket_discount', 'other')),
    raw_name              TEXT NOT NULL CHECK (length(raw_name) BETWEEN 1 AND 128),
    raw_name_key          TEXT NOT NULL CHECK (length(raw_name_key) BETWEEN 1 AND 128),
    ptu_code              TEXT NULL CHECK (ptu_code IS NULL OR ptu_code IN ('A','B','C','D','E','F','G')),
    quantity              TEXT NOT NULL,
    raw_unit              TEXT NULL,
    unit_price_raw        TEXT NULL,
    gross_line_total      TEXT NOT NULL,
    discount_amount       TEXT NOT NULL DEFAULT '0',
    net_line_total        TEXT NOT NULL,
    PRIMARY KEY (receipt_id, line_no),
    FOREIGN KEY (receipt_id, owner) REFERENCES budget_receipts(id, owner)
);
```

- **`0020` downgrade order (child-first):** drop indexes → `budget_receipt_lines` → `budget_receipts` → `budget_shops`.
- **Runtime delete order:** inside `BEGIN IMMEDIATE`, `DELETE FROM budget_receipt_lines` → `DELETE FROM budget_receipts` → commit → unlink image file on disk (`ReceiptFiles.delete`).

#### Migration `0021_budget_receipt_products` (v2)

Creates `budget_normalized_products` (`normalized_product`) and `budget_product_aliases`, and adds `normalized_product_id`, `base_quantity`, `base_unit`, `unit_price_base`, `mapping_source` + composite FK `(normalized_product_id, owner) -> budget_normalized_products(id, owner)` onto `budget_receipt_lines` via SQLite `op.batch_alter_table`:

```sql
CREATE TABLE budget_normalized_products (
    id              TEXT PRIMARY KEY,
    owner           TEXT NOT NULL REFERENCES users(username),
    canonical_name  TEXT NOT NULL,
    name_key        TEXT NOT NULL,
    base_unit       TEXT NOT NULL CHECK (base_unit IN ('kg', 'l', 'pcs')),
    category_hint   TEXT NULL,
    version         INTEGER NOT NULL CHECK (version >= 1),
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    UNIQUE (id, owner),
    UNIQUE (owner, name_key, base_unit)
);

CREATE TABLE budget_product_aliases (
    owner                 TEXT NOT NULL REFERENCES users(username),
    chain_key             TEXT NOT NULL,
    raw_name_key          TEXT NOT NULL,
    normalized_product_id TEXT NOT NULL,
    pack_quantity         TEXT NULL,
    pack_unit             TEXT NULL CHECK (pack_unit IS NULL OR pack_unit IN ('kg', 'l', 'pcs')),
    created_at            REAL NOT NULL,
    PRIMARY KEY (owner, chain_key, raw_name_key),
    FOREIGN KEY (normalized_product_id, owner) REFERENCES budget_normalized_products(id, owner)
);
-- Plus batch_alter_table on budget_receipt_lines adding v2 nullable columns + FK + ix_budget_receipt_lines_product.
```

- **`0021` downgrade order (child-first):** drop `ix_budget_receipt_lines_product` and v2 columns/FK on `budget_receipt_lines` via `batch_alter_table` → drop `budget_product_aliases` → drop `budget_normalized_products`.

> **Cross-reference (`v1d`):** Schema deltas after `0021` (including expanded `source_kind`/`status`, proof-link metadata, manual attachment retention columns, and Gmail state/dedupe tables) are specified in [§13.3](#133-data-model-delta-migration-tbd-after-0021).

### 5.2 Photo storage hardening, DoS bounds, retention & log hygiene

- **Dedicated `ReceiptFiles` module (not `ConsentFiles`):** `hestia/budget/consent_files.py:13,30-42` hardcodes `MAX_FILE_BYTES = 1 MiB` and JSON-only payloads. Build a dedicated `hestia/budget/receipt_files.py` storing binary `.webp` files under `HESTIA_BUDGET_RECEIPTS_DIR=/data/budget-receipts` (`docker-compose.yml:44`) while keeping `consent_files.py:72-100,147-159`'s exact OS invariants:
  - Per-owner subdirectory `<owner_sha256_prefix>` + strict UUID filename `<receipt_id>.webp`; reject `..`, slashes, or symlinks.
  - Directory mode `0700`, file mode `0600`, opened exclusively via `os.open(..., dir_fd=dir_fd, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)` with `st_uid == os.geteuid()` and `S_ISREG`.
  - Atomic write: temp file + `flush` + `os.fsync` + `os.replace` + directory `fsync`.
- **Decompression-bomb & upload DoS defense:**
  1. Require `Content-Length` (`411` if absent); **refuse `Transfer-Encoding: chunked`**; cap multipart body at **`6 MiB`** (raw image part `<= 5 MiB`).
  2. Sniff magic bytes (`JPEG`, `PNG`, `WebP` only); reject HEIC/PDF/SVG/other formats with `400 budget_receipt_invalid_image`.
  3. Set `Image.MAX_IMAGE_PIXELS = 4_000_000` and treat `Image.DecompressionBombWarning` as an error (`warnings.simplefilter("error", Image.DecompressionBombWarning)`).
  4. Inspect header dimensions **before `img.load()`**: reject if `max(width, height) > 2000` or `width * height > 4_000_000` (`4 MP`), and iterate `ImageSequence.Iterator` / check `getattr(img, "n_frames", 1) == 1` (checking every frame if multi-frame container). Worst-case RGBA buffer stays `<= 16 MB`.
  5. Re-encode cleanly to WebP (stripping EXIF/GPS/ICC chunks after safe orientation transpose) in `api._settled_thread` (`hestia/budget/api.py:174-188`) under a **single process-wide semaphore created at module/app startup** (`_IMAGE_DECODE_SEMAPHORE = threading.Semaphore(1)`, never instantiated per request) so concurrent uploads never stack image buffers. Pass **only** the re-encoded WebP bytes to `ReceiptFiles` and OCR; discard uploaded multipart bytes immediately.
- **Disk quotas on `/data` (D4):** Check before writing new drafts: max **`256 MiB`** total stored image bytes per owner and max **`20` active `draft` receipts** per owner (`413 budget_receipt_quota_exceeded` after sweeping expired drafts).
- **Unified retention & purge rules (Q2, D1):**
  - **Unconfirmed drafts (`48 hours`):** Draft rows, their `raw_ocr_text`, and any draft image files are deleted after **48 hours** (`v1a` implements the draft + `raw_ocr_text` purge sweep and test; `v1b` extends it to unlink draft WebP files).
  - **Confirmed / `needs_review` receipts (`30 days`):** Both the WebP image and PAN-redacted `raw_ocr_text` live for **30 days after confirmation** (`image_expires_at = raw_text_expires_at = confirmed_at + 30d`, with **NO deletion on confirm**; applies across all `source_kind`s including `live_text` and `cloud_ai`). At the 30-day deadline, the purge sweep unlinks the WebP file (if present) and clears `image_relpath = NULL, image_expires_at = NULL, raw_ocr_text = NULL, raw_text_expires_at = NULL`, retaining structured receipt and line rows until explicit owner deletion (`v1a` purges `raw_ocr_text`; `v1b` purges the WebP file in the same pass).
- **Zero-leakage logging & audit:** Never log image bytes, filenames, `raw_ocr_text`, NIPs, PANs, shop names, line labels, or amounts anywhere in Python `logging`, exception text, `/api/audit`, or SSE. `budget_analytics_audit` stores only `{receipt_id, action, status, version, linked, source_kind}` (`before_json`/`after_json` never copy `raw_ocr_text` or line items).

---

## 6. Product Normalization & Price-per-Unit Comparison (v2)

### 6.1 Normalizing Polish receipt variants

| Shop | Raw receipt label | Printed qty × price | Discount line | Base quantity | Effective net | Canonical product & unit price |
| --- | --- | --- | --- | --- | --- | --- |
| Biedronka | `ML.UHT 3,2% 1L MLEKOVITA` | `2 * 4,49 = 8,98` | `Rabat -2,00` | `2 l` | `6.98 PLN` | `Mleko UHT 3,2%` → **`3.49 PLN/l`** |
| Carrefour | `MLEKO WYPASIONE 3,2% 1000ML` | `1 * 4,29 = 4,29` | — | `1 l` | `4.29 PLN` | `Mleko 3,2%` → **`4.29 PLN/l`** |
| Lidl | `CHLEB BALTON. 500G` | `1 * 4,79 = 4,79` | — | `0.5 kg` | `4.79 PLN` | `Chleb baltonowski` → **`9.58 PLN/kg`** |
| Biedronka | `ZIEMNIAKI JAD.LUZ` | `1,420 kg x 3,99` | — | `1.42 kg` | `5.67 PLN` | `Ziemniaki jadalne` → **`3.99 PLN/kg`** |

1. **Cleanup & folding:** `NFC`, strip PTU suffix (`A`–`G`), fold Polish diacritics into `raw_name_key`, expand retail abbreviations (`ML.` → `MLEKO`, `BALTON.` → `BALTONOWSKI`, `JAD.` → `JADALNE`).
2. **Exact `Decimal` pack/unit extraction:** Match multipack (`(\d+)\s*[x*]\s*(\d+(?:[.,]\d+)?)\s*(kg|g|dag|l|ml|szt|pak|rol)\b`), then single pack (`(\d+(?:[.,]\d+)?)\s*(kg|g|dag|l|ml|szt|op)\b`), combining with `quantity` when weighed (`kg`). Convert via exact factors (`g → kg ÷ 1000`, `dag → kg ÷ 100`, `ml → l ÷ 1000`, `szt/op → pcs`).
3. **Unit price from item-net total:** Compute `net_line_total / base_quantity` from post-item-`Rabat` `net_line_total` (whole-basket `basket_discount` lines remain separate per **Q7**), using scaled integers (`grosze` and `mg`/`ml`) and formatting exact decimal strings at serialization.
4. **Alias memory & candidate match:** Exact `(owner, chain_key, raw_name_key)` lookup in `budget_product_aliases` first, then unit-compatible token overlap suggestions for explicit owner confirmation.

### 6.2 Chart ideas (inline SVG in React)

1. **Multi-shop unit-price timeline (SVG line/step chart):** Date on X, `PLN / base_unit` on Y, per-shop colored series with hollow dots for discounted (`discount_amount > 0`) vs. solid dots for regular prices.
2. **Cross-shop staple comparison bars:** Grouped horizontal bars showing latest `PLN/unit` + 90-day min–max range and `% delta` vs. cheapest shop.
3. **Recurring basket cost sparkline:** Monthly exact decimal total (`sum_money`) across pinned staples over 6–12 months.

---

## 7. UI Sketch (Mobile-First)

Adds a `"Receipts"` / `"Paragony"` tab in `ui/src/budget/budget.tsx:29,75`, using `ui/src/budget/budget.css:34-36,85-88` responsive patterns:

- **Screen A — Capture / Paste:** Large mobile buttons for *Photo upload* (client `<canvas>` resize `<= 1800 px`, EXIF strip) or *Paste Live Text* (`text <= 32 KiB` inside `48 KiB` JSON body), optional unchecked per-receipt *Cloud Vision* checkbox (only if `AI_ENABLED=1`), and *Analyze* CTA.
- **Screen B — Review, Link & Confirm:** Editable shop/NIP/date/`SUMA PLN`, reconciliation banner (`sum(net_line_total) == total_amount`), candidate transaction radio list (`±4` days, exact amount, or *Cash / No bank link*), editable line cards (showing item `Rabat`, `Kaucja`, or `basket_discount` badges), and *Confirm & Save*. On desktop (`> 860px`), image/raw text sticks beside the form.
- **Screen C — Shop Comparison (v2):** Product filter (`POST /api/budget/receipts/search`), shop comparison cards (`Biedronka` vs. `Carrefour` vs. `Lidl` per `1 l` / `1 kg`), and inline SVG price history.

---

## 8. Risks & Mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| **Exposed sidecar under `network_mode: host`** | Unauthenticated LAN/WAN OCR access | Bridge network + `127.0.0.1:8928` loopback publish, `Authorization` shared secret mounted `:ro` on both services, `cap_drop: [ALL]`, no `/data`/socket mounts (`§4.2`). |
| **Image bomb / RAM OOM in main process** | `288 MB+` RGBA spike crashes Hestia | Require `Content-Length`, refuse chunked, `6 MiB` limit, magic-byte check, header `<= 2000 px`/`4 MP`/single-frame check before `load()`, `DecompressionBombWarning` as error, process-wide `Semaphore(1)` (`§5.2`). |
| **Multipart CSRF or `_VIEWER` fallback** | Cross-origin upload or non-admin read | `_principal` + `_browser` (`Origin` + cookie, no `Authorization`) before reading body; static single-method `_ADMIN` routes in `_ROUTE_MIN_ROLE` and `_PATH_ROLES` (`§4.5`). |
| **`raw_ocr_text` / audit / proxy log leaks** | PAN, NIP, or shopping list leaks | PAN redactor before save; `raw_ocr_text` purged on 30d image deadline (48h for drafts); minimal `budget_analytics_audit`; `POST`-only `list`/`search` (`§4.3`, `§4.5`, `§5.2`). |
| **Settlement lag, refunds, transfers, races** | Wrong or stale bank links | `±4`d min across `transaction`/`booking`/`value_date`; direction filter; exclude `budget_transfer_legs`; `BEGIN IMMEDIATE` re-verify; `409` on `IntegrityError`; ingestion + transfer hooks (`§4.4`). |
| **Franchise NIPs (Żabka, Carrefour Express)** | Branches have different NIPs | Separate nullable `chain_key` + `nip` on `budget_shops` + header rules + owner edit (`§3.1`, `§5.1`). |

---

## 9. Decisions on Formerly Open Questions (Q3–Q4)

Questions **Q1, Q2, Q5, Q6, Q7** and **D1–D4** are recorded in [§2.1](#21-decided-rules-delegated-to-engineering). **Q3** and **Q4** were decided on **2026-11-22** (owner delegated decisions to engineering; reviewable next week):

- **Q3 — Cloud AI Vision provider (`v1c`, off by default):** A single configurable adapter, with **Anthropic Claude** as the first implementation. It stays off by default (`HESTIA_BUDGET_RECEIPTS_AI_ENABLED=0`), runs only after an explicit per-receipt opt-in, and requires the privacy-policy update (`ui/public/privacy-policy.html`) before it is enabled.
- **Q4 — Product/receipt search transport (`v1`/`v2`):** **`POST /api/budget/receipts/search`** with the search query in the JSON body (`§4.5`). Search text never appears in `GET` query strings or reverse-proxy logs (`ui/public/privacy-policy.html:228`); small v1 lists may also filter client-side.

---

## 10. Testing & Verification Plan (Required Synthetic Cases)

Every PR must pass the full Python statement+branch coverage gate, UI `typecheck` / `lint` / `test` / `build`, and OpenAPI / `_ROUTE_MIN_ROLE` / `_PATH_ROLES` drift checks using synthetic fixtures only:

1. **`test_receipt_decompression_bomb_and_oversized_header`:** Rejects `> 2000 px` long side, `> 4 MP` (`4_000_000` pixels), multi-frame animated WebP/PNG, and `DecompressionBombWarning` before `img.load()`, plus chunked / missing `Content-Length` / `> 6 MiB` uploads.
2. **`test_receipt_rejects_heic_and_non_image_uploads`:** Sniffs magic bytes and rejects HEIC, PDF, SVG, and plain text spoofed as `.jpg` with `400 budget_receipt_invalid_image`.
3. **`test_receipt_storage_blocks_traversal_and_symlinks`:** Rejects `../`, non-UUID IDs, symlinks inside `HESTIA_BUDGET_RECEIPTS_DIR`, unexpected UID/mode, and enforces `256 MiB` byte + `20`-draft quotas.
4. **`test_receipt_owner_isolation_foreign_id`:** Second admin gets `404` on detail, image, confirm, list/search, and delete; viewer/operator gets `403`; auth-off/JSON users fail closed (`401`/`409`).
5. **`test_receipt_multipart_csrf_requires_origin_and_cookie`:** Multipart upload without `Origin`, with foreign `Origin`, or carrying `Authorization` header is rejected before reading multipart bytes.
6. **`test_receipt_cloud_ai_flag_and_explicit_opt_in`:** Cloud AI disabled by default (`503`/`400`); enabled flag without `use_cloud_ai=true` never triggers cloud egress; opt-in strips EXIF and sends zero bank data.
7. **`test_receipt_match_friday_purchase_booked_monday`:** Friday `2026-11-20` receipt matches Monday `2026-11-23` bank entry (`±4`d rule) and uses minimum distance across `transaction_date`, `booking_date`, `value_date`.
8. **`test_receipt_refund_matches_only_crdt`:** Refund (`ZWROT`, `receipt_kind='refund'`, positive `total_amount > 0`) never suggests or accepts a `DBIT` transaction and matches `CRDT` only.
9. **`test_receipt_match_excludes_transfer_legs`:** Entries in `budget_transfer_legs` are omitted from candidates and rejected inside `BEGIN IMMEDIATE` on `confirm`.
10. **`test_receipt_concurrent_confirm_returns_409`:** Concurrent confirms linking two receipts to one bank entry (or stale `expected_version`) trigger `409 budget_receipt_tx_already_linked` / conflict, never `500`.
11. **`test_receipt_fingerprint_change_and_transfer_latch_needs_review`:** Covers all committing ingestion/transfer paths: (a) `BudgetStore.commit_snapshot` (`hestia/budget/store.py:441`) when an API update replaces `payload_json` (`store.py:546-547`) or writes `uncertain=True` and returns early (`store.py:530-539`), (b) `BudgetStore.import_snapshot` (`store.py:576,603`, incl. `665-671` & `674-681`), and (c) `BudgetAnalyticsStore.decide_transfer` (`hestia/budget/analytics_store.py:600-606`) reserving a linked entry into `budget_transfer_legs`; asserts linked receipt latches `status='needs_review'` and bumps `version`.
12. **`test_receipt_retention_purge_48h_draft_and_30d_confirmed`:** Confirms `confirm` retains PAN-redacted `raw_ocr_text` (`v1a`); 48h draft purge (`v1a` for drafts/text, `v1b` for draft WebP) deletes unconfirmed drafts and files; 30d confirmed purge clears `raw_ocr_text` (`v1a`) and unlinks the WebP file together (`v1b`) while keeping structured rows.
13. **`test_receipt_migration_upgrade_downgrade`:** `0019 → 0020 → 0019` and `0020 → 0021 → 0020` succeed under `PRAGMA foreign_keys=ON` (`hestia/db.py:757`) with child-first table/column removal.
14. **`test_receipt_confirm_leaves_overview_totals_unchanged`:** Confirming, linking, and deleting receipts produces zero change in `GET /api/budget/analytics/overview` totals in v1.
15. **`test_receipt_logs_contain_no_nip_pan_or_amounts`:** Asserts PAN redaction before persistence and confirms zero NIPs, PANs, shop names, item names, or amounts appear in Python logs, error JSON, `/api/audit`, SSE, or `budget_analytics_audit`.
16. **`test_receipt_confirm_rejects_mismatched_total_and_wrong_line_net`:** `confirm` returns `400` when `sum(net_line_total) != total_amount`, when any line violates `net_line_total == gross_line_total - discount_amount`, or when `deposit`/`basket_discount`/`total_amount` sign rules are violated.
17. **`test_receipt_match_allows_excluded_category_purchase`:** A `BOOK` card purchase annotated or ruled as `kind == 'excluded'` (and not in `budget_transfer_legs`) remains eligible for receipt matching and confirmation.

---

## 11. Effort Estimate

| Phase | Scope | Rough effort |
| --- | --- | --- |
| **v1a — Paste + Parser + Match + Review + Draft/Text Purge** | `receipt_parser.py` (with PAN redactor & strict `confirm` line/total validation), matcher (`±4`d, `CRDT`/`DBIT`, transfer exclusion) + `store.py`/`analytics_store.py` drift hook, `0020` migration, static `POST`/`GET` `/api/budget/receipts/*` (`48 KiB` `parse-text`, `128 KiB` `confirm`, `POST` `list`/`search`) + 48h draft & 30d `raw_ocr_text` purge sweep + `privacy-policy.html` update, mobile Paste + Review UI, tests #4, #7–#11, #12 (`v1a` scope), #13–#17 | **5.5–7.5 engineering days** |
| **v1b — Photo upload + Hardened RapidOCR sidecar** | Browser `<canvas>` resize, `client_max_size` + `Content-Length` + magic/4MP/frame/process-wide `Semaphore(1)` WebP pipeline, `ReceiptFiles` (`256 MiB` / `20`-draft quotas) + `30d` image purge extension, hardened `receipt-ocr` Compose sidecar (`127.0.0.1:8928`, shared secret `:ro` in both services), `GET .../image` + `cf-cache-status` check, tests #1–#3, #5, #12 (`v1b` image purge) | **4–5 engineering days** |
| **v1c — Optional Cloud Vision opt-in** | Default-off configurable Cloud Vision adapter (Claude first), `0400` secret file, per-receipt UI opt-in + `privacy-policy.html` provider terms update, test #6 | **1.5–2 engineering days** |
| **v2 — Product normalization & shop charts** | `0021` migration (`budget_normalized_products`, `budget_product_aliases`, `batch_alter_table`), exact `Decimal` unit extractor + alias matcher, shop comparison view + inline SVG charts | **5–7 engineering days** |

**Total:** ~**11–14.5 days** for full v1 (`a+b+c`), or **5.5–7.5 days** for paste-only v1a; **+5–7 days** for v2 price comparison. For the additive `v1d.1`–`v1d.3` effort breakdown (after `v1a` + `v1b`), see [§13.8](#138-effort-estimate).

---

## 12. Sources

### Repository references (verified against rebased `master` at `81a432c`)
- `Dockerfile:10-39`, `docker-compose.yml:1-46`, `requirements.txt:22-29`, `ui/package.json:10-32`
- `hestia/db.py:10-30,182-748,753-816` (`BudgetTransferLeg` `:694-700`, `BudgetAnalyticsAudit` `:703-712`, `PRAGMA foreign_keys=ON` `:757`)
- `hestia/web.py:50-52,144,167-206,245-253,276-300,905-935,1157-1167,1221`
- `hestia/api_contract.py:2283-2315`
- `hestia/budget/api.py:38,53-91,174-188,208,214-216,236-241`
- `hestia/budget/analytics_api.py:97-130`
- `hestia/budget/analytics_core.py:38-87,89-98,115-125`
- `hestia/budget/analytics_store.py:109-129,549-606`
- `hestia/budget/analytics_reconcile.py:85-100`
- `hestia/budget/store.py:441,530-539,545-547,576,603,665-671,674-681`
- `hestia/budget/transfer_match.py:1-237`
- `hestia/budget/normalize.py:98-134`
- `hestia/budget/views.py:31-32,69-84`
- `hestia/budget/config.py:171-195`
- `hestia/budget/consent_files.py:13,30-42,72-100,147-159`
- `hestia/budget/client.py:77-120`
- `docs/BUDGET.md:13-14,23-24,247-252,660-662`, `docs/BUDGET_2B.md:370-374,475-478`, `docs/BUDGET_2D.md:26-29,180-182,292-295`, `docs/BUDGET_FORECAST.md:1-80`
- `ui/src/budget/budget.tsx:1-2,29,75`, `ui/src/budget/budget.css:34-36,85-88`, `ui/public/privacy-policy.html:161-266`

### Public documentation & external references
- Enable Banking API reference: https://enablebanking.com/docs/api/reference/
- Apple iOS & iPadOS Feature Availability (Live Text languages): https://www.apple.com/ios/feature-availability/
- Polish Ministry of Finance — e-Paragony & HUB Paragonowy: https://www.podatki.gov.pl/e-paragony/
- Tesseract OCR & `tessdata_best` / `tessdata_fast`: https://github.com/tesseract-ocr/tesseract , https://github.com/tesseract-ocr/tessdata_best , https://github.com/tesseract-ocr/tessdata_fast
- OCRmyPDF & documentation: https://github.com/ocrmypdf/OCRmyPDF , https://ocrmypdf.readthedocs.io
- pdfsandwich: https://www.tobias-elze.de/pdfsandwich/ , https://sourceforge.net/projects/pdfsandwich/
- Stirling-PDF: https://github.com/Stirling-Tools/Stirling-PDF
- Paperless-ngx & GPL-3.0 licence: https://github.com/paperless-ngx/paperless-ngx , https://github.com/paperless-ngx/paperless-ngx/blob/main/LICENSE , https://docs.paperless-ngx.com
- PaddleOCR & RapidOCR: https://github.com/PaddlePaddle/PaddleOCR , https://github.com/RapidAI/RapidOCR
- docTR, EasyOCR & Surya: https://github.com/mindee/doctr , https://github.com/JaidedAI/EasyOCR , https://github.com/datalab-to/surya
- Anthropic Claude Vision & Commercial Terms: https://docs.anthropic.com/en/docs/build-with-claude/vision , https://www.anthropic.com/legal/commercial-terms
- Google Gemini API Terms & Structured Outputs: https://ai.google.dev/gemini-api/terms , https://ai.google.dev/gemini-api/docs/structured-output
- Open-source receipt parser references: https://github.com/filip-rupniewski/receipt_parsing , https://github.com/KamKubicki/fidl-plus , https://github.com/bhimrazy/receipt-ocr , https://github.com/Rutikasri/DocuParse_AI , https://github.com/YanzhongSu/lidl

---

## 13. v1d — Gmail import and proof for larger expenses

> **Normative scope & implementation baseline:** `master` at `518e5bb` (schema `0019`, OpenAPI `0.29.0`, privacy-policy Gmail update `2ce999f` / PR `#237`) and `v1a` on `feat/budget-receipts-v1a` at `ed853b0` (PR `#238`, migration `0020`, OpenAPI `0.30.0`, `hestia/budget/receipt_api.py`, `receipt_store.py`, `receipt_parser.py`, `receipt_reconcile.py`).  
> **Delivery order:** `v1a` (paste) → `v1b` (`ReceiptFiles`, `6 MiB` multipart framing, `Semaphore(1)` media decoding, 30-day file purge) → `v1d base + v1d.1` (Gmail receipt import) → `v1d.2` (large-expense proof linking, missing-proof flag, manual PDF attachments) → `v1d.3` (opaque-debit enrichment). All new flags default off.

### 13.1 Owner decisions (2026-11-22) & feature gates

| ID | Topic | Normative Rule |
| --- | --- | --- |
| **G1** | Google OAuth & mailbox scope | Reuse `.env` credentials `HESTIA_GOOGLE_CLIENT_ID`, `HESTIA_GOOGLE_CLIENT_SECRET`, and `HESTIA_GOOGLE_REFRESH_TOKEN` shared with `calendar_sync` (`docs/CALENDAR_SYNC.md:36-38,88-90`, `hestia/calendar_sync/config.py:53-54`). The OAuth client (`"Hestia Calendar"`, GCP project `klatt-ie`) is `In production` and Workspace-trusted (`docs/CALENDAR_SYNC.md:63-72`), carrying `gmail.readonly` for the **owner's mailbox only** (`users/me`). Refresh reuses the `GoogleCalendarProvider._token` flow (`hestia/calendar_sync/providers.py:168-199`). |
| **G2** | Mail selection & spouse forwards | Fetch **only** receipt-like messages matching an explicit Gmail label (e.g., `"Paragony"`) or explicit `sender_allowlist`. The spouse forwards her e-receipts to the owner's mailbox; `v1d.1` unwraps forwarded messages and recovers the original merchant/sender hint in memory after validating outer envelope authentication. |
| **G3** | Zero-body / zero-address persistence | Store **only** the parsed receipt fields (`raw_ocr_text = NULL`, `image_relpath = NULL`) plus `HMAC-SHA256(server_secret, Message-ID)` for deduplication. **Never** store raw email bodies, raw HTML, Gmail attachments, or email addresses anywhere in SQLite, `/data`, logs, or audit rows. |
| **G4** | Untrusted HTML & PDF bounds | Convert email HTML to text with zero script execution and zero remote resource loads (`<= 1 MiB` HTML → `<= 32 KiB` / `500` lines text). Parse PDFs (both Gmail attachments in `v1d.1` and manual uploads in `v1d.2`) with byte (`<= 5 MiB`), page (`<= 10`), and decompressed-stream bounds in an off-loop thread with a timeout (`5 s`) and process-wide `Semaphore(1)`, matching `v1b`. |
| **G5** | Sync cadence & history cursor | Run **on demand** and **at most hourly** (`3600 s`) while enabled, bounded to Gmail API quotas. Incremental sync uses `users.history` with a persisted `historyId`; initial backfill is bounded (default `365` days, max `500` messages total). |
| **P1** | Single receipt model (no evidence table) | **No separate evidence table.** Reuse `budget_receipts` across `source_kind` values `paste` (`v1a` `live_text`), `photo` (`v1b` `local_ocr`/`cloud_ai`), `email`, and `attachment`. **"Proof"** is defined as a `confirmed` receipt linked to a transaction, using the same PAN redaction, retention boundaries, and `BEGIN IMMEDIATE` link re-verification (`receipt_store.py:296-365`). |
| **P2** | Exact vs. FX / gross-net matching | Same-currency exact amount (`±4`d) → auto-suggest candidate (`match_tier = "exact"`). Cross-currency (e.g., Apple/OpenAI billed in `USD`/`EUR` vs. a `PLN` card debit such as `APPLE.COM/BILL 999.99 PLN` for ChatGPT) or gross/net difference → **suggestion only** (`match_tier = "fx_or_net"`), requiring explicit manual confirmation and **never** auto-linked. |
| **P3** | Missing-proof flag & non-breaking API | Applies **only** to selected booked `DBIT` transactions with effective `kind == "expense"` — **excluding** transfers (`budget_transfer_legs` and accepted transfer pairs), Krypto (`kind == "excluded"`, `docs/BUDGET_MONTH.md:32`), and pass-through (`month_budget.load_month_config()`, `hestia/budget/month_budget.py:73-84,842-845`). Thresholds are per-currency in config, defaulting to `{"PLN": "500"}` (`>= 500 PLN`). UI displays count + list via a **new** route with a minor OpenAPI bump; existing endpoint responses stay unchanged. |
| **P4** | Manual PDF attachments & privacy gate | `POST /api/budget/receipts/attach` accepts bounded manual PDF (or image) uploads, storing PAN-redacted `raw_ocr_text` plus an optional original file kept `48h` (draft) / `30d` (post-confirm), like photos. **`ui/public/privacy-policy.html` must be updated before enabling `v1d.2`/`v1d.3`.** |
| **P5** | Opaque-debit enrichment (`v1d.3`) | Opaque bank debits (e.g., `"Płatność BLIK … Nr transakcji"` without merchant, `Przelewy24`/`P24`, `PayU`, `Tpay`, `Autopay`, `APPLE.COM/BILL`) match Gmail payment-confirmation emails by amount and date (`±4`d) as **suggestions only**; confirmation links a receipt without mutating `budget_transactions.payload_json` or `analytics_views.py` totals. |

**Feature gates & runtime prerequisites (all default off):**

1. Base budget & receipts gates (`hestia/budget/api.py:53-63`, `feat/budget-receipts-v1a:hestia/budget/receipt_api.py:19,30-31`): `HESTIA_BUDGET_HTTP_ENABLED=1`, `HESTIA_PERSIST=sqlite`, authoritative SQLite `admin` owner, and `HESTIA_BUDGET_RECEIPTS_ENABLED=1`.
2. Gmail sync gate (`v1d.1` / `v1d.3`): `HESTIA_BUDGET_RECEIPTS_GMAIL_ENABLED=1` **and** readable server-only JSON config at `HESTIA_BUDGET_RECEIPTS_GMAIL_CONFIG` (default unset → disabled, matching `HESTIA_CALENDAR_CONFIG` at `hestia/calendar_sync/config.py:168-179`).
3. Proof / attachment gate (`v1d.2`): `HESTIA_BUDGET_RECEIPTS_PROOF_ENABLED=1` (default `0`; must remain off until `ui/public/privacy-policy.html` covers manual PDF attachments).
4. Stable dedupe key (`v1d.1`): `HESTIA_BUDGET_RECEIPTS_HMAC_SECRET_FILE` (`0400`/`0600` server-only secret file read via `hestia/budget/config.py:171-195`'s `_read_secret`), separate from `HESTIA_SESSION_SECRET` so rotating web session secrets never invalidates `budget_gmail_messages` deduplication.

### 13.2 Phased architecture (`v1d.1`, `v1d.2`, `v1d.3`)

```text
[Owner Gmail (gmail.readonly)]
   │  label:"Paragony" OR from:sender_allowlist (after:<365d>, cap 500 backfill / 100 per run)
   ▼
[Google OAuth refresh + HTTPTransport (_NoRedirect, ≤30s)]   (providers.py:46-70,168-199)
   │
   ├─ Outer Authentication-Results (dkim/spf pass) & sizeEstimate ≤ 10 MiB
   ├─ Dedupe: HMAC-SHA256(hmac_secret, Message-ID) ∈ budget_gmail_messages? → skip
   ├─ Spouse forward unwrap (in memory): message/rfc822 (depth ≤2) / forward block → merchant hint; strip addresses
   ├─ HTML (≤1 MiB) → pure-Python HTMLParser (no scripts/CSS/remote URLs) → ≤32 KiB text
   ├─ PDF attachment (≤5 MiB, ≤10p, %PDF-) → pypdf off-loop thread + Semaphore(1) + 5s timeout + zlib cap
   ▼
[PAN Redactor + Email Scrubber + Receipt/Invoice/Gateway Parser]
   │  Gmail: raw_ocr_text = NULL, image_relpath = NULL, status = 'imported' (30d expiry, cap 200)
   │  Manual PDF attach (v1d.2): raw_ocr_text (PAN-redacted) + ReceiptFiles PDF/WebP (48h draft / 30d confirmed)
   ▼
[Matcher: exact (±4d, same currency/amount) | fx_or_net (±7d, suggestion only)]
   │  v1d.2 missing-proof view: DBIT + kind=expense + ≥ threshold (500 PLN) − transfers − Krypto − pass-through
   │  v1d.3 opaque-debit view: BLIK/P24/PayU/PayPro/Apple debits matched to Gmail confirmations (suggestion only)
   ▼
[Owner Review & BEGIN IMMEDIATE Confirm] (reconcile_receipt_links latches needs_review on bank drift)
```

#### Phase `v1d.1` — Gmail import of receipts

1. **Configuration (`HESTIA_BUDGET_RECEIPTS_GMAIL_CONFIG`):** Modeled on `hestia/calendar_sync/config.py:53-62,98-200` and `hestia/budget/month_budget.py:110-168` (rejecting duplicate JSON keys and constants via `_unique_error_fields` / `_reject_constant`, `MAX_CONFIG_BYTES = 16 KiB`). Stores **only** environment-variable / file-path references (never inline OAuth tokens):
   - `owner`: existing SQLite `admin` username (`users.username`).
   - `client_id_env` (`"HESTIA_GOOGLE_CLIENT_ID"`), `client_secret_env` (`"HESTIA_GOOGLE_CLIENT_SECRET"`), `refresh_token_env` (`"HESTIA_GOOGLE_REFRESH_TOKEN"`).
   - `label_name`: optional Gmail label (e.g., `"Paragony"`, resolved via `GET /gmail/v1/users/me/labels` to immutable `labelId`).
   - `sender_allowlist`: optional list of exact sender emails or `@domain` suffixes for direct receipts (e.g., `@email.apple.com`, `@tm.openai.com`, `allegro.pl`). **Fail closed:** at least one of `label_name` or non-empty `sender_allowlist` must be configured, or sync refuses with `configuration`.
   - `spouse_forward_senders`: explicit exact email address(es) of the spouse allowed to forward e-receipts into the owner's mailbox.
   - `backfill_days`: integer `1..365` (default `365`), `backfill_max_messages`: `1..500` (default `500`), `sync_interval_seconds`: integer `>= 3600` (default `3600`).
   - `proof_thresholds`: currency map (`{"PLN": "500"}` default; consumed by `v1d.2`).
2. **Transport & OAuth reuse (`hestia/budget/gmail_client.py`):** Reuses `HTTPTransport` (`_NoRedirect`, `30 s` timeout, bounded response read) and `GoogleCalendarProvider._token()` (`hestia/calendar_sync/providers.py:46-70,168-221`) against fixed constants `TOKEN_URL = "https://oauth2.googleapis.com/token"` and `GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"`. On HTTP `401`, invalidates cached access token and refreshes once (`providers.py:211-214`). On `invalid_grant` or `403` insufficient scope (`ACCESS_TOKEN_SCOPE_INSUFFICIENT`), latches `budget_gmail_state.attention = "authorization"` and halts background polling. On `429` or `403` rate-limit reason (`rateLimitExceeded`, `userRateLimitExceeded`, `quotaExceeded` per `providers.py:74-77`), honors `Retry-After` via `parse_retry_after` (`hestia/budget/rate_limit.py:117`) and exponential backoff (`min(21600, 60 * 2**failures)`).
3. **Sync protocol (`users.history` + bounded initial backfill):**
   - Serialized by a per-DB `fcntl.flock` (`hestia/calendar_sync/runtime.py:20-32`) across HTTP `POST /gmail-sync` and the background poller (`asyncio.create_task` loop at `hestia/server.py:264`, `hestia/proxy.py:1994`, running off-loop via `_settled_thread` per `hestia/budget/api.py:174-188` and stopping cleanly on shutdown like `BudgetJobService.stop` at `hestia/budget/jobs.py:208-221`).
   - **Initial backfill (`history_id IS NULL`):**
     1. Read `GET /profile` to capture starting `historyId` **before** listing so messages arriving mid-backfill are picked up on the first delta sync.
     2. Query `GET /messages` with `q` built strictly from configured `label_name` / `sender_allowlist` / `spouse_forward_senders` + `after:<epoch_now - backfill_days*86400>`, `maxResults=100`, capped at `500` total messages across at most `5` list pages (`100` processed per sync run, recording progress or completing across bounded batches).
   - **Incremental sync (`history_id IS NOT NULL`):**
     1. Query `GET /history?startHistoryId=<id>&historyTypes=messageAdded&historyTypes=labelAdded` (filtering to `labelIds=[label_id]` when label-filtered, or checking returned message metadata against allowlist).
     2. Fetch at most `MAX_SYNC_MESSAGES_PER_RUN = 100` candidate messages per run: pre-check `format=metadata` (`sizeEstimate`, `Message-ID`, `From`, `Authentication-Results`) or fetch `format=raw` while capping decoded RFC 5322 size at `MAX_GMAIL_MESSAGE_BYTES = 10 MiB` (`10_485_760` B). Oversized messages record `outcome = 'skipped_oversized'` in `budget_gmail_messages` without decoding payloads.
     3. Commit parsed receipts, `budget_gmail_messages` tombstones, and the new `history_id` inside one `BEGIN IMMEDIATE` transaction (`hestia/budget/store.py:110-114`).
   - **Expired cursor (`HTTP 404` on `users.history`):** Fall back to a bounded catch-up `messages.list` scan over `max(last_sync_at - 7d, now - backfill_days*86400)` (max `200` messages) relying on `message_id_hmac` dedupe, then store the fresh `profile.historyId`.
   - **Quota ceiling:** Client paces calls (`>= 50 ms` gap, max `<= 110` API calls / `~550` quota units per hourly sync against Gmail's `250 units/user/sec` quota).
4. **Spouse forwarding unwrap & address stripping:**
   - Parse raw RFC 5322 bytes in memory using stdlib `email.parser.BytesParser(policy=email.policy.default)`.
   - Verify Google MX's topmost `Authentication-Results` header (`mx.google.com`, ignoring any lower forged header) contains `dkim=pass` and (`spf=pass` or `dmarc=pass`) aligned with the outer sender when matching `sender_allowlist` or `spouse_forward_senders`.
   - If the outer sender is the owner or in `spouse_forward_senders`, inspect attachments for `message/rfc822` (max recursion depth `2`) or bounded inline forward markers (`---------- Forwarded message ---------`, `Begin forwarded message:`, `Przekazana wiadomość`) to extract the original sender's display brand/domain token (`recovered_merchant_hint`, e.g., `Apple` / `OpenAI` / `Allegro` / `InPost`) and original date.
   - **Never** treat unwrapped inner `From:` headers as an authentication bypass and **never** persist any email address. Before constructing `raw_shop_name` or line labels, run both `redact_pan()` (`feat/budget-receipts-v1a:hestia/budget/receipt_parser.py:94`) and an RFC 5322 email-address scrubber (`[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+` → removed).
5. **Untrusted HTML & PDF extraction:**
   - **HTML parts (`<= 1 MiB` raw decoded bytes):** Decode declared charset safely (`errors="replace"`), parse via pure-Python `html.parser.HTMLParser` (`convert_charrefs=True`, skipping HTML comments, zero JS/DOM engine, zero external CSS/image/link fetching, blocking `http:`/`https:`/`cid:`/`data:` loads), drop `<script>`, `<style>`, `<noscript>`, `<template>`, `<iframe>`, `<object>`, `<embed>`, `<svg>` subtrees entirely, emit line breaks on block/table boundaries (`<p>`, `<div>`, `<tr>`, `<li>`, `<br>`), normalize `NFC`, strip `Cc`/`Cf`/`Cs` characters, and cap at `32 KiB` / `500` lines before parsing.
   - **PDF parts (`<= 5 MiB`, `%PDF-` magic):** Parse with pure-Python `pypdf` (BSD-3-Clause; no Poppler/MuPDF/Ghostscript C binaries in `hestia`), capped at `MAX_PDF_PAGES = 10`, `MAX_PDF_STREAM_BYTES = 4 MiB` per stream, `MAX_PDF_DECOMPRESSED_BYTES = 8 MiB` total, and `MAX_PDF_RECURSION = 64`, executing in a worker thread guarded by a process-wide `_PDF_PARSE_SEMAPHORE = threading.Semaphore(1)` created at startup and a cooperative `5 s` parse timeout (`PDF_PARSE_TIMEOUT_SECONDS = 5.0`). Encrypted/password-protected PDFs fail cleanly without brute-forcing.
   - **Zero-body storage rule:** Gmail-imported rows persist with `status = 'imported'` (`MAX_GMAIL_PENDING = 200`, separate from `MAX_DRAFTS = 20` at `receipt_store.py:27`), `source_kind = 'email'` (body) or `'attachment'` (Gmail PDF/image), `raw_ocr_text = NULL`, `image_relpath = NULL`, `attachment_relpath = NULL`, and `import_expires_at = now + 30d`.

#### Phase `v1d.2` — Proof linking, missing-proof flag & manual attachments

1. **Source kinds & `validate_confirm` deltas:**
   - Extend `budget_receipts.source_kind` `CHECK` to allow `('live_text', 'local_ocr', 'cloud_ai', 'paste', 'photo', 'email', 'attachment')` — preserving backward compatibility with `v1a`/`v1b` rows (`0020_budget_receipts.py:95`, `db.py:804`) while adopting the `v1d` taxonomy (`paste`/`photo`/`email`/`attachment`).
   - Add `document_kind` (`'receipt' | 'invoice' | 'payment_confirmation'`, default `'receipt'`).
   - Update `validate_confirm` (`feat/budget-receipts-v1a:hestia/budget/receipt_parser.py:362-403`): when `lines == []` and `source_kind IN ('email', 'attachment')`, accept `lines=[]` (still validating `total_amount > 0`, valid date/currency/payment method, and PAN/email redaction); whenever `lines` is non-empty (and always for `paste`/`live_text`/`photo`/`local_ocr`/`cloud_ai`), retain strict per-line `net = gross - discount` and `sum(net_line_total) == total_amount` validation.
   - Support confirming directly from `status IN ('draft', 'imported', 'confirmed', 'needs_review')` (with `expected_version` CAS).
2. **Exact vs. FX / gross-net matching & `BEGIN IMMEDIATE` verification:**
   - Extend `BudgetReceiptStore._candidates` and `_verify_link` (`feat/budget-receipts-v1a:hestia/budget/receipt_store.py:245-365`):
     - **Tier 1 — `exact` (auto-suggest):** Same currency, `canonical_money(tx.amount) == canonical_money(receipt.total_amount)`, `DBIT`/`CRDT` direction match, non-transfer, `BOOK`, `uncertain=False`, and `min_date_distance <= 4` (`D2`). Ranked top; still requires owner click to confirm.
     - **Tier 2 — `fx_or_net` (suggestion only, manual confirmation required, never auto-linked):**
       - *Cross-currency (`tx.currency != receipt.currency`):* Within `±7` calendar days (to accommodate card FX settlement lag) and same `DBIT`/`CRDT` direction; requires either a merchant/gateway token hit (including curated aliases such as `APPLE.COM/BILL` ↔ `apple`/`openai`/`chatgpt`) or explicit manual picker search.
       - *Gross/net delta (same currency, `tx.amount != receipt.amount`):* Within `±4` days and same direction where `receipt.total_amount` differs by VAT/fee/FX-spread margin (e.g., within `0.70x..1.30x` or explicit manual picker selection).
       - Every Tier-2 candidate returns `match_tier = "fx_or_net"`, `auto_suggest = False`, `requires_manual_confirm = True`.
     - **Server re-check inside `BEGIN IMMEDIATE`:** `BudgetReceiptConfirmRequest.link` adds `link_basis: Literal["exact", "fx_or_net"] = "exact"` (default `"exact"` preserves strict `v1a` rejection of any currency/amount mismatch). When `link_basis == "fx_or_net"`, `_verify_link` still enforces `account.owner == owner`, `connection.owner == owner`, `account.enabled == True`, `tx.uncertain == False`, `BOOK`, not in `budget_transfer_legs`, matching `DBIT`/`CRDT`, `date_distance <= 7`, partial unique index `uq_budget_receipts_tx_link` (`409 budget_receipt_tx_already_linked`), and stores `linked_fingerprint = financial_fingerprint(payload)` + `link_basis = 'fx_or_net'`. Existing `reconcile_receipt_links` hooks (`receipt_reconcile.py:80-108`, `store.py:441,576,603`, `analytics_store.py:609-611`) automatically latch `needs_review` if the linked bank entry later changes or becomes a transfer leg.
3. **Missing-proof evaluation (`GET /api/budget/receipts/missing-proof`):**
   - Accepts optional `period` (`YYYY-MM` or `YYYY` via `analytics_core.parse_period`, defaulting to current `Europe/Warsaw` month via `month_budget.warsaw_date`) and optional `currency`; rejects unknown or duplicate query parameters (`400`, matching `analytics_api.py:60-72`).
   - Evaluates a single read-only SQLite snapshot (`BudgetHTTPStore._read` + `projection_snapshot` at `hestia/budget/analytics_views.py:221-256`) without altering `analytics_views.py` totals or any existing API response:
     - **Qualifying pool:** Selected (`enabled == True`), `BOOK`, `uncertain == False`, `payload["credit_debit_indicator"] == "DBIT"`, and `effective["kind"] == "expense"`.
     - **Automatic & explicit exclusions:**
       - Own-account transfers (`effective["kind"] == "transfer"` from `_accepted_projection` at `analytics_views.py:483-502`, plus explicit `budget_transfer_legs` exclusion at `hestia/db.py:694-700`).
       - **Krypto:** Classified by owner rules/annotations as `effective["kind"] == "excluded"` (`docs/BUDGET_MONTH.md:32`), thus excluded automatically by `kind == "expense"`.
       - **Pass-through:** Read `month_budget.load_month_config()` (`hestia/budget/month_budget.py:35,73-84,162-168`); if `settings.pass_through` is configured for `owner`, exclude any transaction with `effective["category_id"] in {settings.pass_through.mortgage_category_id, settings.pass_through.rent_category_id}` (`month_budget.py:842-845,910-913`).
       - All `CRDT` entries (income, refunds, unclassified credits).
     - **Threshold check:** Look up `currency` in `proof_thresholds` (default `{"PLN": "500"}`). If `currency` has a threshold and `compare_money(amount, threshold) >= 0` (`hestia/budget/analytics_core.py:81-86`), require a linked `BudgetReceipt` with `status == "confirmed"`. Currencies absent from `proof_thresholds` are omitted rather than guessed.
     - **Flagging:** If unlinked → `missing_reason = "unlinked"`; if linked to a receipt latched `needs_review` → `missing_reason = "needs_review"`. Returns `missing_count`, `needs_review_count`, `total_flagged_count`, and bounded `items` (ordered by date descending, capped at `MAX_MISSING_PROOF_ITEMS = 500`, and `4 MiB` UTF-8 JSON limit via `_encode_bounded` at `hestia/budget/analytics_api.py:94-105`).
4. **Manual PDF / image attachments (`POST /api/budget/receipts/attach`, `GET /api/budget/receipts/attachment`):**
   - Multipart upload (`<= 6 MiB` total, raw file `<= 5 MiB`, `Content-Length` required, chunked rejected) verified by `_principal` + `_browser` **before** reading any body bytes (`hestia/budget/api.py:53-91`).
   - Sniffs magic bytes: if `JPEG`/`PNG`/`WebP`, delegates to the `v1b` WebP re-encode + RapidOCR/Cloud-AI pipeline (`image_relpath` / `image_expires_at`, served via `GET /image`); if `%PDF-`, runs the bounded `pypdf` extractor under `_PDF_PARSE_SEMAPHORE`, applies `redact_pan()` + email scrubber, stores extracted text in `raw_ocr_text` (`<= 32 KiB`), and optionally stores the validated PDF bytes in `ReceiptFiles` (`attachment_relpath`, `attachment_mime = 'application/pdf'`, `attachment_expires_at`) within the shared `256 MiB` owner quota (**D4**) and `20`-draft quota.
   - Supports attaching directly to a new draft (pre-filled with an optional `suggested_link` `{account_id, entry_reference}` from the missing-proof UI list) or attaching a PDF/photo file onto an existing owned draft (`receipt_id`, `expected_version`).
   - Retention matches **Q2** & **D1**: `48 hours` for unconfirmed `draft`s, and `confirm` sets all non-null artifact deadlines (`image_expires_at`, `attachment_expires_at`, `raw_text_expires_at`) to `confirmed_at + 30d`.
   - `GET /api/budget/receipts/attachment` streams owned PDF bytes via `dir_fd` + `O_NOFOLLOW` (`hestia/budget/consent_files.py:88-99`) with `Content-Type: application/pdf`, `Content-Disposition: attachment; filename="receipt-<uuid>.pdf"`, `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'none'; sandbox`, `Cache-Control: no-store`, and `Referrer-Policy: no-referrer`.

#### Phase `v1d.3` — Opaque-debit enrichment

1. **Opaque debit identification:** Pure read helper identifies booked selected `DBIT` transactions (`effective.kind == "expense"` or `"auto"`, not in `budget_transfer_legs`) whose `remittance_information` / `creditor_name` matches opaque payment patterns (`Płatność BLIK` + `Nr transakcji` without merchant name, `Przelewy24`/`P24`/`PAYPRO`, `PayU`, `Tpay`/`Krajowy Integrator Płatności`, `Autopay`/`Blue Media`, `APPLE.COM/BILL`) or has empty/gateway-only `creditor_name`.
2. **Confirmation-email ingestion & matching:** `v1d.1` parser templates extended to recognize Polish payment-gateway and subscription confirmation emails (`document_kind = "payment_confirmation"`), extracting sanitized merchant/item summary (email addresses removed), `total_amount`, `currency`, `receipt_date`, and `payment_method`. Matcher pairs unlinked `imported` / unlinked `confirmed` confirmations with opaque debits by exact amount/currency (or `fx_or_net`) within `±4` calendar days (`D2`).
3. **Strict suggestion-only invariant:** `GET /api/budget/receipts/enrichment-suggestions` returns opaque debits with ranked `candidates` for owner review. Confirming a suggestion calls `POST /api/budget/receipts/confirm` inside `BEGIN IMMEDIATE` to link the `BudgetReceipt` to `(account_id, entry_reference)` — **never** mutating `budget_transactions.payload_json`, `budget_transaction_metadata`, or `analytics_views.py` spending totals.

### 13.3 Data model delta (migration TBD after `0021`)

Migration ID is **TBD after `0021`** (planned as `0022_budget_receipts_v1d` following `0020_budget_receipts` from `v1a` and `0021_budget_receipt_products` from `v2` in `§5.1`; if `v1d` ships before `v2`, `0021` / `0022` ordering swaps). All changes honor `PRAGMA foreign_keys=ON` (`hestia/db.py:757`), composite `(id, owner)` FKs, and child-first downgrades.

1. **Migrate `budget_receipts` (`batch_alter_table`)** (preserving all `0020` rows and `uq_budget_receipts_tx_link` on `status IN ('confirmed', 'needs_review')`; **backfill `UPDATE budget_receipts SET link_basis = 'exact' WHERE linked_account_id IS NOT NULL` before applying the updated `ck_budget_receipts_link_fields` constraint**):
   - Expand `ck_budget_receipts_status`: `status IN ('draft', 'imported', 'confirmed', 'needs_review')`.
   - Expand `ck_budget_receipts_source`: `source_kind IN ('live_text', 'local_ocr', 'cloud_ai', 'paste', 'photo', 'email', 'attachment')`.
   - Update `ck_budget_receipts_confirmed_fields`: `status IN ('draft', 'imported') OR (receipt_date IS NOT NULL AND total_amount IS NOT NULL AND currency IS NOT NULL AND payment_method IS NOT NULL)`.
   - Update `ck_budget_receipts_link_fields`: `(linked_account_id IS NULL AND linked_entry_ref IS NULL AND linked_fingerprint IS NULL AND link_basis IS NULL) OR (status IN ('confirmed', 'needs_review') AND linked_account_id IS NOT NULL AND linked_entry_ref IS NOT NULL AND linked_fingerprint IS NOT NULL AND link_basis IS NOT NULL)`.
   - Add columns:
     - `document_kind TEXT NOT NULL DEFAULT 'receipt' CHECK (document_kind IN ('receipt', 'invoice', 'payment_confirmation'))`
     - `link_basis TEXT NULL CHECK (link_basis IS NULL OR link_basis IN ('exact', 'fx_or_net'))`
     - `attachment_relpath TEXT NULL`
     - `attachment_mime TEXT NULL CHECK (attachment_mime IS NULL OR attachment_mime IN ('application/pdf'))`
     - `attachment_expires_at REAL NULL`
     - `import_expires_at REAL NULL` (30-day TTL for unconfirmed `'imported'` Gmail rows)

2. **New tables (`budget_gmail_state` & `budget_gmail_messages`):**

```sql
CREATE TABLE budget_gmail_state (
    owner                  TEXT PRIMARY KEY REFERENCES users(username),
    history_id             TEXT NULL CHECK (history_id IS NULL OR length(history_id) BETWEEN 1 AND 64),
    last_sync_at           REAL NULL CHECK (last_sync_at IS NULL OR last_sync_at >= 0),
    next_sync_at           REAL NOT NULL DEFAULT 0 CHECK (next_sync_at >= 0),
    backoff_until          REAL NOT NULL DEFAULT 0 CHECK (backoff_until >= 0),
    failures               INTEGER NOT NULL DEFAULT 0 CHECK (failures >= 0),
    attention              TEXT NULL CHECK (attention IS NULL OR attention IN ('authorization', 'configuration')),
    last_http_status       INTEGER NULL CHECK (last_http_status IS NULL OR (last_http_status BETWEEN 100 AND 599)),
    last_error             TEXT NULL CHECK (last_error IS NULL OR length(last_error) <= 64),
    backfill_completed_at  REAL NULL CHECK (backfill_completed_at IS NULL OR backfill_completed_at >= 0),
    version                INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1)
);

CREATE TABLE budget_gmail_messages (
    owner            TEXT NOT NULL REFERENCES users(username),
    message_id_hmac  TEXT NOT NULL CHECK (length(message_id_hmac) = 64 AND message_id_hmac NOT GLOB '*[^0-9a-f]*'),
    internal_date    REAL NOT NULL CHECK (internal_date >= 0),
    outcome          TEXT NOT NULL CHECK (outcome IN ('imported', 'skipped_non_receipt', 'skipped_oversized', 'parse_failed', 'deleted')),
    receipt_id       TEXT NULL,
    created_at       REAL NOT NULL CHECK (created_at >= 0),
    PRIMARY KEY (owner, message_id_hmac),
    FOREIGN KEY (receipt_id, owner) REFERENCES budget_receipts(id, owner)
);
CREATE INDEX ix_budget_gmail_messages_owner_date ON budget_gmail_messages(owner, internal_date);
```

- **Runtime delete order on `receipts/delete` & draft/import purge:** When deleting or purging a `BudgetReceipt` whose `id` is referenced by `budget_gmail_messages.receipt_id`, first set `receipt_id = NULL, outcome = 'deleted'` inside `BEGIN IMMEDIATE` (no `ON DELETE CASCADE`), then delete `budget_receipt_lines` → `budget_receipts` → commit → unlink any disk files (`ReceiptFiles.delete`). Retaining the `message_id_hmac` row stops subsequent hourly syncs from re-importing a deleted/expired email.
- **Migration downgrade order (child-first):** drop `ix_budget_gmail_messages_owner_date` → drop `budget_gmail_messages` → drop `budget_gmail_state` → `batch_alter_table` on `budget_receipts` dropping new columns and restoring `0020` `CHECK` constraints (after clearing/backing up `v1d`-only rows, matching `0019` backup guidance).

### 13.4 API routes, role classification & body limits

Every `v1d` route uses a static path and a single HTTP method, registered `_ADMIN` in `hestia/web.py:167-209` and `"admin"` in `hestia/api_contract.py:2549-2624` (pinned by `tests/test_web.py:1960-1967` and `tests/test_api_contract.py:420-428`), with `allow_head=False` on `GET` and `Cache-Control: no-store` / `Referrer-Policy: no-referrer` (`hestia/budget/api.py:38,214-216`). Disabled feature flags return `404` (`budget_receipts_disabled` when `HESTIA_BUDGET_RECEIPTS_ENABLED != "1"`, `budget_receipts_gmail_disabled` when `HESTIA_BUDGET_RECEIPTS_GMAIL_ENABLED != "1"`, `budget_receipts_proof_disabled` when `HESTIA_BUDGET_RECEIPTS_PROOF_ENABLED != "1"`). All `POST` routes execute `api._principal` and `api._browser` (`Origin` + `hestia_session`, rejecting `Authorization`) **before reading any request body bytes**. OpenAPI bumps by a minor version per phase (after `v1a`'s `0.30.0` and `v1b`).

| Phase | Route | Role / CSRF / Framing | Body / Response Cap & Contract Behavior |
| --- | --- | --- | --- |
| `v1d.1` | `POST /api/budget/receipts/gmail-sync` | `_ADMIN`, `_principal` → `_browser` before body | JSON `<= 4 KiB` (`4,096` B, `{}` or `{force_catchup: bool}`); `60 s` cooldown + `flock`; returns sanitized `{ok, imported_count, skipped_count, history_id_present, attention, next_sync_at}` (zero mail subjects/addresses). |
| `v1d.1` | `GET /api/budget/receipts/gmail-status` | `_ADMIN`, `allow_head=False` | No query params; returns `{enabled, configured, attention, last_sync_at, next_sync_at, backoff_until, failures, last_http_status, last_error, pending_imported_count}`. |
| `v1d.2` | `POST /api/budget/receipts/attach` | `_ADMIN`, `_principal` → `_browser` before `Content-Length` check & multipart read | `multipart/form-data`, `Content-Length` required, chunked rejected, total `<= 6 MiB` (`file <= 5 MiB`, PDF `<= 10` pages or `v1b` WebP); accepts optional `receipt_id`+`expected_version` or creates a `48h` `draft`; requires `client_max_size >= 6 MiB` (from `v1b`) or bounded `request.content` streaming (`hestia/web.py:925-933,1164`). |
| `v1d.2` | `GET /api/budget/receipts/attachment` | `_ADMIN`, `allow_head=False`, `?receipt_id=<uuid>` | Opens PDF via `ReceiptFiles` (`dir_fd` + `O_NOFOLLOW`), returns `application/pdf`, `Content-Disposition: attachment; filename="receipt-<uuid>.pdf"`, `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'none'; sandbox`; foreign/unknown/absent returns `404`. |
| `v1d.2` | `GET /api/budget/receipts/missing-proof` | `_ADMIN`, `allow_head=False`, optional `?period=YYYY-MM\|YYYY&currency=AAA` | **New operation** leaving all existing analytics/budget responses untouched; `<= 4 MiB` bounded UTF-8 JSON (`analytics_api.py:94-105`); returns `{period, start, end, evaluation_timezone, thresholds, missing_count, needs_review_count, total_flagged_count, items, items_cap, truncated}`. |
| `v1d.3` | `GET /api/budget/receipts/enrichment-suggestions` | `_ADMIN`, `allow_head=False`, optional `?period=YYYY-MM&currency=AAA` | `<= 4 MiB` bounded UTF-8 JSON; lists opaque debits (`BLIK`/`P24`/`PayU`/`Tpay`/`Autopay`/`APPLE.COM/BILL`) with matching Gmail confirmation candidates (`match_tier`, `date_distance`, sanitized merchant label) for explicit owner confirmation via `POST /confirm`. |

**Additive receipt-namespace DTO deltas (non-breaking to `/api/budget/*` and `/api/budget/analytics/*`):**
- `BudgetReceiptConfirmRequest`: additive optional `link_basis: Literal["exact", "fx_or_net"] = "exact"` inside `link`, and `lines=[]` allowed when `source_kind IN ("email", "attachment")`.
- `BudgetReceiptListRequest`: optional `status` filter expanded with `"imported"`; optional `document_kind` filter.
- `BudgetReceiptSummary` / `BudgetReceiptDetail`: additive `document_kind`, `link_basis`, `attachment_available: bool`, `attachment_expires_at: float | None`, `import_expires_at: float | None`.
- `BudgetReceiptCandidate`: additive `match_tier: Literal["exact", "fx_or_net"]`, `auto_suggest: bool`, `requires_manual_confirm: bool`.

### 13.5 Threat model & mitigations

| Threat | Attack / Failure Mode | Mandatory Mitigation |
| --- | --- | --- |
| **Malicious email HTML** | `<script>`, `onerror=`, SVG/CSS `url()`, tracking pixels (`<img src>`), `cid:` probing, billion-laughs nested tags, or bidi/control spoofing inside email bodies. | Pure-Python `HTMLParser` text extractor only (`<= 1 MiB` HTML → `<= 32 KiB` / `500` lines); drops `<script>`, `<style>`, `<noscript>`, `<template>`, `<iframe>`, `<object>`, `<embed>`, `<svg>`; strips all URLs/attributes and makes **zero** network requests; strips `Cc`/`Cf`/`Cs` characters; raw HTML is discarded immediately and never persisted or served. |
| **Malicious PDF (Gmail & manual)** | `FlateDecode` decompression bomb, deeply nested object/XRef graph, embedded PDF JavaScript (`/JS`/`/OpenAction`), `>10` pages, C-library PDF parser RCE, or XSS via inline browser PDF preview. | Sniff `%PDF-`; enforce `<= 5 MiB` upload (`Content-Length`, no chunked) and `<= 10` pages; pure-Python `pypdf` only (no Poppler/MuPDF/Ghostscript rasterization in `hestia`); cap per-stream (`4 MiB`), total decompressed (`8 MiB`), and object recursion (`64`); execute in worker thread under process-wide `Semaphore(1)` and `5 s` timeout; never persist Gmail PDFs; serve manual PDFs solely with `Content-Disposition: attachment`, `nosniff`, and `Content-Security-Policy: default-src 'none'; sandbox`. |
| **Header spoofing on forwarded or direct mail** | External attacker sends spoofed `From: spouse@...` or injects fake `---------- Forwarded message --------- From: Apple <no_reply@email.apple.com>` headers to plant false receipts or hijack bank links. | Require outer Gmail `Authentication-Results` from Google MX showing `dkim=pass` and (`spf=pass` or `dmarc=pass`) aligned with `sender_allowlist` or `spouse_forward_senders`; treat unwrapped `message/rfc822` or inline forwarded headers strictly as **untrusted extraction hints** (for merchant tokenization after stripping email addresses), never as an authorization identity; land all Gmail imports as unconfirmed `status = 'imported'` with zero auto-linking; verify every link inside `BEGIN IMMEDIATE`. |
| **Quota & storage exhaustion** | Thousands of matching historical emails, rapid `gmail-sync` button mashing, `429` storms, or `MAX_DRAFTS = 20` / `/data` volume starvation. | Bound initial backfill (`365` days, max `500` messages) and per-run batch (`100` messages, `<= 10 MiB` raw mail); pace calls (`>= 50 ms`) and apply `Retry-After` + exponential backoff on `429`/`403 rateLimitExceeded` (`providers.py:74-77`, `rate_limit.py:117`); serialize syncs with `flock` + `60 s` HTTP cooldown; isolate Gmail unconfirmed rows under `status = 'imported'` (`MAX_GMAIL_PENDING = 200`) separate from `MAX_DRAFTS = 20`; enforce shared `256 MiB` `ReceiptFiles` `/data` cap for manual PDFs/photos and `4 MiB` JSON cap on `missing-proof`. |
| **OAuth token, PII & address leakage** | Refresh/access tokens, raw email bodies, spouse/merchant email addresses, PANs, or amounts exposed in config files, logs, exceptions, DB, audit rows, or reverse-proxy logs. | Keep `HESTIA_GOOGLE_*` in `.env` and `HESTIA_BUDGET_RECEIPTS_HMAC_SECRET_FILE` as `0400`/`0600` (`config.py:171-195`); JSON config holds env-var names only (`calendar_sync/config.py:53-62`); `HTTPTransport` uses `_NoRedirect` and constant Google URLs (`providers.py:46-57,170`); exceptions/logs emit only fixed error codes/classes (`providers.py:5-6`, `client.py:77-115`); strip all email addresses + run `redact_pan()` before persisting any field; set `raw_ocr_text = NULL` on Gmail imports; store only `HMAC-SHA256` of `Message-ID`; log only sanitized IDs/statuses/source/document kinds in `budget_analytics_audit`. |

### 13.6 Retention, purge & privacy-policy gates

| Artifact / Record | Retention Lifetime | Purge / Cleanup Mechanism |
| --- | --- | --- |
| Interactive drafts (`status = 'draft'` — paste, photo, manual PDF) | **48 hours** from creation | Hourly `retention` sweep (`receipt_api.py:89-112`) and `ReceiptStore._purge` delete lines + receipt rows and unlink disk WebP/PDF via `ReceiptFiles.delete` (`§5.2`). |
| Unconfirmed Gmail imports (`status = 'imported'`) | **30 days** (`import_expires_at = created_at + 30d`) | Same `_purge` sweep deletes unconfirmed lines + receipt rows while keeping `budget_gmail_messages` tombstone (`outcome = 'deleted'`) so hourly sync does not re-import. |
| Manual PDF / photo proof (`raw_ocr_text`, `image_relpath`, `attachment_relpath`) | **30 days after confirmation** (`confirmed_at + 30d`) | `_purge` clears `raw_ocr_text`, `raw_text_expires_at`, `image_relpath`, `attachment_relpath`, `attachment_mime`, `attachment_expires_at`, and unlinks `/data/budget-receipts` files while keeping structured receipt/link rows until explicit deletion (**Q2**, **D1**). |
| Gmail-sourced confirmed receipts (`email` / Gmail `attachment`) | `raw_ocr_text = NULL` & no stored file from creation; structured metadata kept until deleted | Zero mail bodies or files are ever written; owner `delete` clears `budget_gmail_messages.receipt_id = NULL` (`outcome = 'deleted'`) before deleting lines/receipt. |
| Dedupe tombstones (`budget_gmail_messages`) | **400 days** (`internal_date < now - 400d`, exceeding `365d` backfill window) | Pruned during `_purge` / post-sync sweep so the dedupe table remains bounded without causing re-imports inside the backfill window. |

- **Privacy policy status & gate:** Commit `2ce999f` (`#237`) updated `ui/public/privacy-policy.html:282-295` and `ui/public/terms-of-service.html:157-164` for `#gmail` receipt import (`gmail.readonly`, label/sender filtering, zero stored bodies/addresses, Google Limited Use). However, `ui/public/privacy-policy.html:268-281` does **not** yet disclose manual PDF attachment text extraction/30-day PDF storage (`v1d.2`) or Gmail payment-confirmation enrichment (`v1d.3`). **Operator gate:** `ui/public/privacy-policy.html` (and `terms-of-service.html` if needed) **must be updated before enabling `v1d.2` or `v1d.3`**.

### 13.7 Testing & verification plan (`v1d` synthetic cases)

Continuing `§10` (`#1–#17`), every `v1d` PR must pass full Python statement+branch coverage, UI `typecheck`/`lint`/`test`/`build`, and `test_every_registered_route_is_public_or_classified` / OpenAPI drift checks (`tests/test_web.py:1960-1967`, `tests/test_api_contract.py:49-54,420-428`) using synthetic fixtures only:

18. **`test_gmail_oauth_token_refresh_and_auth_attention_latch`:** Uses injected `HTTPTransport` (`providers.py:173-199`) with `_NoRedirect`; refreshes expired access token once on `401`; latches `budget_gmail_state.attention = 'authorization'` on `invalid_grant` / insufficient scope and halts background sync without leaking tokens, URLs, or HTTP bodies in logs.
19. **`test_gmail_history_sync_backfill_bounds_and_404_fallback`:** Initial sync captures `profile.historyId`, enforces `365d` / `500`-message backfill cap and `100`-message per-run cap; incremental `users.history` consumes `messageAdded` + `labelAdded` and advances `history_id` only after SQLite commit; `404` expired history triggers bounded catch-up without duplicates.
20. **`test_gmail_dedupe_hmac_stores_no_bodies_or_addresses`:** Verifies `HMAC-SHA256(hmac_secret, Message-ID)` dedupe in `budget_gmail_messages`, asserts `raw_ocr_text IS NULL` and zero email addresses, HTML tags, or body strings appear anywhere in SQLite tables, `/data`, audit rows, or logs, and verifies deleted/expired imports stay suppressed on re-sync.
21. **`test_gmail_forwarded_unwrap_and_header_spoofing_defense`:** Spouse `message/rfc822` and inline forwarded receipts recover the merchant/brand hint (`Apple`, `OpenAI`, `Allegro`) with email addresses stripped; spoofed outer `From` or forged inner forward headers failing `Authentication-Results` (`dkim`/`spf`) are rejected and never auto-confirm or bypass link verification.
22. **`test_gmail_html_sanitization_blocks_scripts_and_remote_resources`:** Strips `<script>`, `<style>`, `<iframe>`, `<svg>`, tracking `<img src>`, `cid:` refs, and Unicode bidi controls with zero outbound network calls; enforces `1 MiB` HTML and `32 KiB` / `500`-line output bounds.
23. **`test_gmail_quota_429_backoff_and_sync_lock`:** Verifies `429` and `403 rateLimitExceeded` (`providers.py:74-77`) honor `Retry-After` (`rate_limit.py:117`) and exponential backoff; `flock` prevents concurrent backfill/HTTP sync; `MAX_GMAIL_PENDING = 200` does not exhaust `MAX_DRAFTS = 20`.
24. **`test_receipt_fx_and_gross_net_suggests_only_and_requires_manual_confirm`:** Covers USD/EUR invoice vs. `APPLE.COM/BILL 999.99 PLN` debit and same-currency gross/net VAT deltas: surfaces `match_tier = 'fx_or_net'` with `auto_suggest = False`; rejects default `link_basis = 'exact'` with `400`; validates `link_basis = 'fx_or_net'` inside `BEGIN IMMEDIATE` (persisting `linked_fingerprint`, enforcing `409` uniqueness and `reconcile_receipt_links` `needs_review` latching).
25. **`test_missing_proof_threshold_and_exclusions`:** Flags `DBIT`, `kind='expense'` `>= 500 PLN` when unlinked or latched `needs_review`; excludes `< 500 PLN`, `CRDT`, `transfer` / `budget_transfer_legs`, Krypto (`kind='excluded'`), and `pass_through` mortgage/rent categories (`month_budget.load_month_config()`); verifies existing `overview`, `transactions`, and `monthly-budget` payloads are unchanged.
26. **`test_manual_pdf_bomb_page_limit_timeout_and_attachment_headers`:** Rejects `> 5 MiB`, `> 10` pages, `FlateDecode` stream bombs (`> 4 MiB` stream / `> 8 MiB` total), password-encrypted PDFs, and non-PDF magic bytes; verifies `_PDF_PARSE_SEMAPHORE`, `5 s` timeout, `30d` confirmed file + `raw_ocr_text` purge, and `GET /attachment` `Content-Disposition: attachment` + `sandbox` CSP headers.
27. **`test_opaque_debit_enrichment_suggests_without_mutating_ledger`:** Matches opaque `"Płatność BLIK … Nr transakcji"` / `PayU` / `Przelewy24` / `APPLE.COM/BILL` debits against Gmail payment confirmations by amount and `±4`d date as suggestions only; confirms link inside `BEGIN IMMEDIATE` while keeping `budget_transactions.payload_json` and `analytics_views.py` totals untouched.
28. **`test_receipt_v1d_migration_upgrade_downgrade`:** Round-trips `0021 → 0022 (TBD) → 0021` under `PRAGMA foreign_keys=ON` (`hestia/db.py:757`) with child-first table/column removal and verifies `v1a`/`v1b` receipt compatibility.

### 13.8 Effort estimate

| Phase | Scope | Rough effort |
| --- | --- | --- |
| **v1d.1 — Gmail import of receipts** | `HESTIA_BUDGET_RECEIPTS_GMAIL_CONFIG` + `gmail_client.py` (reusing `HESTIA_GOOGLE_*` / `providers.py`), `users.history` + bounded 12m/500 backfill + hourly poller + `flock`, spouse-forward unwrap + `Authentication-Results` guard, HTML sanitizer + bounded `pypdf` extractor (`Semaphore(1)` + timeout), `budget_gmail_state`/`budget_gmail_messages` migration delta + `gmail-sync`/`gmail-status` routes + Gmail Inbox UI, tests #18–#23 & #28 | **5–6.5 engineering days** |
| **v1d.2 — Proof linking, missing-proof & manual attachments** | `link_basis` (`exact` vs. `fx_or_net`) matching & `BEGIN IMMEDIATE` confirm + `lines=[]` support for email/attachment, `GET /missing-proof` (default `500 PLN`, excluding transfers/Krypto/pass-through), `POST /attach` + `GET /attachment` (`ReceiptFiles` PDF support + `30d` retention purge), UI missing-proof badge/list + attach flow + `privacy-policy.html` update, tests #24–#26 | **4–5.5 engineering days** |
| **v1d.3 — Opaque-debit enrichment** | Payment-confirmation email parser templates (`document_kind='payment_confirmation'`), opaque-debit pattern detector (`BLIK`/`P24`/`PayU`/`Tpay`/`Autopay`/`APPLE.COM/BILL`), `GET /enrichment-suggestions` + UI suggestion review/confirm, test #27 | **2–3 engineering days** |

**Total (`v1d.1`–`v1d.3`):** ~**11–15 engineering days** (sequenced after `v1a` + `v1b`).
