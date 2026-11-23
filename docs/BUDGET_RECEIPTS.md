# Budget Receipts: Research & Design Proposal

> **Status:** Revised design proposal v3 (Round-1 and Round-2 Grok 4.7 review findings resolved; **Q1–Q7** and operational rules **D1–D4** decided). No code, migrations, or privacy-policy edits ship with this document.  
> **Baseline:** `master` at `81a432c` (schema `0019`, OpenAPI `0.25.0`, target stack Python 3.14 / `aiohttp` at `Dockerfile:10`, `requirements.txt:24`; SQLite + SQLAlchemy 2.0 + Alembic at `hestia/db.py:10-30`, `requirements.txt:25-26`; React 19 + TypeScript at `ui/package.json:29-31`; Docker at `docker-compose.yml:1-46`).  
> **v1d Extension (2026-11-22):** Normative design for Gmail receipt import, large-expense proof, and opaque-debit enrichment added in [§13](#13-v1d-gmail-import-large-expense-proof--opaque-debit-enrichment) against `master` at `7f94dce` (schema `0019`, OpenAPI `0.30.0`), PR `#238` (`feat/budget-receipts-v1a`) and `feat/budget-household-p3`, which re-parent after the category tree (`0020`, PR `#248`, revises `0019`), resolving all 9 Codex PR `#243` review findings ([§13.1](#131-review-resolution)).

---

## 1. Problem & Hestia Fit

Add an owner-private **Receipts (`Paragony`)** module to Hestia Budget so the household owner can:

1. Capture a Polish retail receipt on mobile (photo or pasted iOS Live Text).
2. Extract the shop (name / NIP), purchase date & time, payment method, `SUMA PLN`, and line items (name, quantity, unit price, discount, PTU letter, line total).
3. Match and manually confirm an **informational** link to an existing canonical bank transaction (`budget_transactions`) using exact amount, currency, nearest transaction/booking/value date (`±4` calendar days), and merchant text — without modifying `analytics_views.py` spending totals in v1.
4. *(v2)* Normalize product variants (e.g., `"ML.UHT 3,2% 1L"`, `"MLEKO WYPASIONE 3,2% 1000ML"`) to compare **price per canonical unit** across Warsaw shops (e.g., Carrefour vs. Biedronka vs. Lidl) over time.

### Non-negotiable Hestia constraints

Every design choice below inherits the existing budget module boundaries:

- **Owner-private & fail-closed auth:** Budget endpoints require `HESTIA_BUDGET_HTTP_ENABLED=1`, `HESTIA_PERSIST=sqlite`, authoritative SQLite users, and an enabled `admin` principal (`hestia/budget/api.py:53-63`). Because `hestia/web.py` resolves `_ROUTE_MIN_ROLE.get((method, path))` by exact path and defaults an unclassified route to `_ADMIN`, a receipt route without an explicit role is not viewer-readable. **Every** receipt route must still use a fixed path (no `{id}` path segments), expose **one** HTTP method (`POST` only for `list` and `search`), and be classified `_ADMIN` in `hestia/web.py:167-206` and `"admin"` in `hestia/api_contract.py:2286-2315`, with `allow_head=False` on reads.
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

- **Unicode & PAN redaction:** Normalizes `NFC` and constructs a diacritic-folded lookup key (`ą→a, ć→c, ę→e, ł→l, ń→n, ó→o, ś→s, ź→z, ż→z`). Before persisting `raw_ocr_text` on any path (`live_text`, `local_ocr`, `cloud_ai`), applies a **PAN redactor** masking digit sequences in `PAN_WINDOW` (`(12, 19)` in `hestia/budget/receipt_parser.py`, shared with every Luhn and mask window), masked card PANs (`****1234`, `5351********1234`), and `NR KARTY` / `AID` terminal blocks. Redacted `raw_ocr_text` expires on the same **30-day** post-confirm deadline as the image (not cleared on confirm; `§5.2`).
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

New tables require owner-reviewed Alembic migrations matching `hestia/db.py` conventions (`PRAGMA foreign_keys=ON` at `hestia/db.py:757`, no `CASCADE`, strict `owner` FKs, exact decimal strings). **v1 (`0020` in the original design) must have zero references to v2 product tables.**

**Shipped revision:** `0022_budget_receipts` (`revision = "0022"`). `down_revision` is temporarily `"0020"` so this branch has a single Alembic head on current master. PR `#251` lands migration `0021` and OpenAPI `0.35.0` first. After `#251` merges, re-parent this revision with `down_revision = "0021"`. Holzera re-parents at merge. This contract is OpenAPI `0.36.0`. The SQL below is the v1 table set.

#### Migration `0022_budget_receipts` (v1, shipped)

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

- **Accepted risk (`linked_entry_ref`):** the column is a foreign key to the ledger key `budget_transactions (account_id, entry_reference)`. It stores that raw provider key so the foreign key and `uq_budget_receipts_tx_link` can be enforced. Its exposure equals the ledger's: a reader who can see `budget_transactions.entry_reference` can see the same value here. The receipts API never returns it. `parse-text`, `detail`, `list`, `search`, candidate lists, and `confirm` return only the sha256 digest of the key.
- **`0022` downgrade order (child-first):** drop indexes → `budget_receipt_lines` → `budget_receipts` → `budget_shops`. The accounts `(id, owner)` unique index added by this revision drops last.
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

> **Cross-reference (`v1d`):** Schema deltas after category tree `0020` (PR `#248`; migrations `0023` `v1d-a` and `0024` `v1d-b`, shifting `v2` `budget_receipt_products` from `0021` to `0025+`) are specified in [§13.6](#136-data-model-and-migration-ordering).

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
| **v1a — Paste + Parser + Match + Review + Draft/Text Purge** | `receipt_parser.py` (with PAN redactor & strict `confirm` line/total validation), matcher (`±4`d, `CRDT`/`DBIT`, transfer exclusion) + `store.py`/`analytics_store.py` drift hook, `0022` migration, static `POST`/`GET` `/api/budget/receipts/*` (`48 KiB` `parse-text`, `128 KiB` `confirm`, `POST` `list`/`search`) + 48h draft & 30d `raw_ocr_text` purge sweep + `privacy-policy.html` update, mobile Paste + Review UI, tests #4, #7–#11, #12 (`v1a` scope), #13–#17 | **5.5–7.5 engineering days** |
| **v1b — Photo upload + Hardened RapidOCR sidecar** | Browser `<canvas>` resize, `client_max_size` + `Content-Length` + magic/4MP/frame/process-wide `Semaphore(1)` WebP pipeline, `ReceiptFiles` (`256 MiB` / `20`-draft quotas) + `30d` image purge extension, hardened `receipt-ocr` Compose sidecar (`127.0.0.1:8928`, shared secret `:ro` in both services), `GET .../image` + `cf-cache-status` check, tests #1–#3, #5, #12 (`v1b` image purge) | **4–5 engineering days** |
| **v1c — Optional Cloud Vision opt-in** | Default-off configurable Cloud Vision adapter (Claude first), `0400` secret file, per-receipt UI opt-in + `privacy-policy.html` provider terms update, test #6 | **1.5–2 engineering days** |
| **v2 — Product normalization & shop charts** | `0021` migration (`budget_normalized_products`, `budget_product_aliases`, `batch_alter_table`), exact `Decimal` unit extractor + alias matcher, shop comparison view + inline SVG charts | **5–7 engineering days** |

**Total:** ~**11–14.5 days** for full v1 (`a+b+c`), or **5.5–7.5 days** for paste-only v1a; **+5–7 days** for v2 price comparison. For the additive `v1d-a`–`v1d-c` effort breakdown (after `v1a` + `v1b`), see [§13.9](#139-synthetic-verification-plan-and-effort-estimate).

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

## 13. v1d: Gmail Import, Large-Expense Proof & Opaque-Debit Enrichment

> **Normative scope & baseline:** Targets `master` at `7f94dce` (schema `0019`, OpenAPI `0.30.0` per `#242`), PR `#238` (`feat/budget-receipts-v1a`) and `feat/budget-household-p3`, which re-parent after the category tree migration (`0020`, `parent_id`, PR `#248`, revises `0019`), with `0.31.0`–`0.32.0` claimed by those pre-`v1d` branches (`hestia/api_contract.py:80-82`).  
> **Phased rollout (`v1d-a` → `v1d-b` → `v1d-c`):** Builds on `v1a` (paste) and `v1b` (`ReceiptFiles`, `6 MiB` multipart framing, `Semaphore(1)`, 30-day file purge):
> 1. **`v1d-a` (Gmail import + dedupe):** Separate revocable Hestia grant, mailbox identity check, sender trust + spouse-forward unwrap, quota/resumable `historyId` sync, `HMAC(gmail_message_id)` dedupe, sandboxed HTML/PDF worker, migration `0023`.
> 2. **`v1d-b` (Large-expense proof):** Per-source matching windows, FX handling & tie-breakers, missing-proof threshold/flagging/clearing + non-PII spouse visibility, manual PDF/image attachments via sandboxed worker, privacy-policy update, migration `0024`.
> 3. **`v1d-c` (Opaque-debit enrichment):** Payment-confirmation matching for opaque `BLIK`/`P24`/`PayU`/`Tpay`/`Autopay`/`APPLE.COM/BILL` debits and category suggestions targeting **any node** in the `0020` category tree, privacy-policy update, zero migration.

### 13.1 Review resolution

Resolution mapping for the 9 must-fix findings from the Codex PR `#243` design review, including round-2 through round-6 follow-ups and new blockers:

| Item | Codex Must-Fix Finding | Resolution Summary | Resolving Section(s) | Round-2 Resolution | Round-3 Resolution | Round-4 Resolution | Round-5 Resolution | Round-6 Resolution |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **1** | **Separate, revocable Gmail grant inside Hestia** | Require `HESTIA_BUDGET_GMAIL_IMPORT_ENABLED=1` + `HESTIA_BUDGET_RECEIPTS_GMAIL_CONFIG` **plus** an explicit per-owner SQLite opt-in (`grant_status = 'active'`, default `'disabled'`). Calendar consent/tokens in `.env` never silently authorize Gmail. `POST /gmail-grant` (`disable`) and `POST /gmail-revoke` stop imports immediately, wipe in-memory RAM access tokens, purge cursor/sync state/pending imports, and use `grant_generation` CAS to abort in-flight runs without breaking shared Calendar Sync tokens. | [§13.3](#133-separate-gmail-grant-identity-verification-and-sender-trust), [§13.6](#136-data-model-and-migration-ordering), [§13.7](#137-additive-api-contract-and-openapi-versioning) | **Local-only revocation + new blockers:** Never call Google's revoke endpoint; document the manual Google step and Calendar impact. Fence every chunk by generation and discard it on mismatch. Clear Gmail FKs before deleting imports, and expose `gmail-status.version` for grant/revoke CAS. [§13.3](#133-separate-gmail-grant-identity-verification-and-sender-trust), [§13.4](#134-quota-resumable-sync-hmac-dedupe-and-sandboxed-worker), [§13.7](#137-additive-api-contract-and-openapi-versioning) | Fence before every Gmail request and token refresh; key RAM tokens by generation, re-fence refresh responses, and drop on mismatch. Round-2 deletion/`version` blockers remain resolved. [§13.3](#133-separate-gmail-grant-identity-verification-and-sender-trust) | Share a per-owner `RLock` between request check/dispatch/response and grant mutation; revoke waits up to 35 s before committing, and changed-generation responses are discarded. [§13.3](#133-separate-gmail-grant-identity-verification-and-sender-trust) | **RESOLVED in round 4; unchanged.** [§13.3](#133-separate-gmail-grant-identity-verification-and-sender-trust) | **RESOLVED; unchanged.** [§13.3](#133-separate-gmail-grant-identity-verification-and-sender-trust) |
| **2** | **Profile / identity check** | Call `GET /gmail/v1/users/me/profile` on opt-in and before **every** sync run (`POST /gmail-sync` and poller); verify `emailAddress` equals configured `owner_mailbox` via canonicalized `hmac.compare_digest` + stored `mailbox_hmac`. Refuse on mismatch (`403`), wipe RAM token, latch `attention = 'identity_mismatch'`, halt polling, and emit zero email addresses in DB/logs. | [§13.3](#133-separate-gmail-grant-identity-verification-and-sender-trust), [§13.8](#138-retention-log-hygiene-and-privacy-policy-gates) | **RESOLVED in round 1; unchanged.** [§13.3](#133-separate-gmail-grant-identity-verification-and-sender-trust) | **RESOLVED; unchanged.** [§13.3](#133-separate-gmail-grant-identity-verification-and-sender-trust) | **RESOLVED; unchanged.** [§13.3](#133-separate-gmail-grant-identity-verification-and-sender-trust) | **RESOLVED; unchanged.** [§13.3](#133-separate-gmail-grant-identity-verification-and-sender-trust) | **RESOLVED; unchanged.** [§13.3](#133-separate-gmail-grant-identity-verification-and-sender-trust) |
| **3** | **Sender trust & spouse forwards** | Reject multi-sender `From` / `Sender` mismatches; require Google topmost `Authentication-Results` (`mx.google.com`) `dkim=pass` + aligned (`spf=pass` or `dmarc=pass`). Allow direct mail only from `sender_allowlist` (no public webmail wildcards). Unwrap forwarded mail **only** when authenticated outer `From` matches explicit exact `spouse_forward_senders` addresses, then verify unwrapped original `From`/merchant against `known_merchant_domains` while never trusting inner headers for auth. | [§13.3](#133-separate-gmail-grant-identity-verification-and-sender-trust), [§13.6](#136-data-model-and-migration-ordering) | Select the topmost header with `authserv-id = mx.google.com`; require aligned `header.d`, treating a `header.i`-only DKIM result as unaligned. [§13.3](#133-separate-gmail-grant-identity-verification-and-sender-trust) | Require Gmail's prepended AR above the first `Received`; accept `header.i`-only DKIM by deriving its domain per RFC 8601 Appendix B.6 and checking `From` alignment. [§13.3](#133-separate-gmail-grant-identity-verification-and-sender-trust) | Use the first/topmost `mx.google.com` AR regardless of `Received` placement, relying on Gmail's incoming-AR stripping/renaming. **New blocker:** require the passing signature's aligned `header.d`; `header.i`-only is unaligned because `d=` is unknown. [§13.3](#133-separate-gmail-grant-identity-verification-and-sender-trust) | For a pass without AR `header.d`, use all supplied `header.i`/`header.b` prefix/selector evidence to identify exactly one actual `DKIM-Signature`, then align its real `d=` with `From`; no/ambiguous match fails closed. [§13.3](#133-separate-gmail-grant-identity-verification-and-sender-trust) | Compare `header.i` with the signature's explicit `i=` or its RFC 6376 default `"@" + d=` when `i=` is absent; after unique correlation, align using the signature's actual `d=`. [§13.3](#133-separate-gmail-grant-identity-verification-and-sender-trust) |
| **4** | **Quota & resume** | Track Gmail quota units (`>= 50 ms` pacing, `MAX_SYNC_MESSAGES_PER_RUN = 100`, `MAX_QUOTA_UNITS_PER_RUN = 1000`); honor `Retry-After` (`rate_limit.py:117`) + persisted exponential backoff on `429`/`403` (`providers.py:74-77`). Persist `sync_phase`, `history_id`, `pending_history_id`, `resume_page_token`, `resume_cutoff_ts`, and `batch_processed_count`, committing `25`-message chunks in `BEGIN IMMEDIATE`; fall back cleanly on `404` expired `historyId` or explicit `force_full_resync` to bounded resumable `full_resync`. | [§13.4](#134-quota-resumable-sync-hmac-dedupe-and-sandboxed-worker), [§13.6](#136-data-model-and-migration-ordering) | Replay partial pages through idempotent dedupe; advance cursors/tokens only in committed chunks. The 500-message cap never promotes `history_id`. Incremental sync retains label/sender scope and fetches metadata only for in-scope IDs. [§13.4](#134-quota-resumable-sync-hmac-dedupe-and-sandboxed-worker), [§13.8](#138-retention-log-hygiene-and-privacy-policy-gates) | Add fenced standalone commits for empty/deduped pages. **New blocker:** rely on `history.list?labelId` without requiring history `labelIds`, and never advance past unknown eligibility; the earlier receipt-only filter blocker remains resolved. [§13.4](#134-quota-resumable-sync-hmac-dedupe-and-sandboxed-worker) | Persist `scope_scan_page_token` with the unchanged `pending_history_id` baseline in every message/state commit and resume the exact scan after quota/errors instead of page 1. [§13.4](#134-quota-resumable-sync-hmac-dedupe-and-sandboxed-worker), [§13.6](#136-data-model-and-migration-ordering) | **New blocker:** bind the checkpoint with `scope_scan_query_hmac`; on query mismatch or Gmail 400/invalid token, clear only the scan token and restart at page 1 while preserving the history baseline. Other item-4 rules remain resolved. [§13.4](#134-quota-resumable-sync-hmac-dedupe-and-sandboxed-worker), [§13.6](#136-data-model-and-migration-ordering) | Scope/query changes reset both history and scope page tokens and restart from retained `history_id`/`pending_history_id`; history-token 400 does likewise, while baseline 404 enters full resync. Scope-token 400 resets only the scope scan. [§13.4](#134-quota-resumable-sync-hmac-dedupe-and-sandboxed-worker) |
| **5** | **Dedupe on Gmail message ID (HMAC)** | Key `budget_gmail_messages` on `HMAC-SHA256(hmac_secret, "hestia/budget/gmail-msg-id/v1\0" + gmail_message_id)` using dedicated `0400`/`0600` `HESTIA_BUDGET_RECEIPTS_HMAC_SECRET_FILE` (never raw Gmail ID, never `HESTIA_SESSION_SECRET`), checked before `messages.get` and enforced idempotently in `BEGIN IMMEDIATE` (with optional `rfc_message_id_hmac` for direct/forwarded duplicates and `outcome = 'deleted'` suppression). | [§13.4](#134-quota-resumable-sync-hmac-dedupe-and-sandboxed-worker), [§13.6](#136-data-model-and-migration-ordering) | **RESOLVED in round 1; unchanged.** [§13.4](#134-quota-resumable-sync-hmac-dedupe-and-sandboxed-worker) | **RESOLVED; unchanged.** [§13.4](#134-quota-resumable-sync-hmac-dedupe-and-sandboxed-worker) | **RESOLVED; unchanged.** [§13.4](#134-quota-resumable-sync-hmac-dedupe-and-sandboxed-worker) | **RESOLVED; unchanged.** [§13.4](#134-quota-resumable-sync-hmac-dedupe-and-sandboxed-worker) | **RESOLVED; unchanged.** [§13.4](#134-quota-resumable-sync-hmac-dedupe-and-sandboxed-worker) |
| **6** | **Sandboxed PDF / HTML worker** | Parse all untrusted email HTML (`<= 1 MiB`) and PDFs (`<= 5 MiB`, `%PDF-`, Gmail + manual) in an isolated process: `receipt-parser` Compose sidecar (`network_mode: "none"`, `read_only`, `cap_drop: [ALL]`, no `/data`/DB/secrets, Unix socket IPC) or `subprocess` sandbox (`env={}`, `RLIMIT_*`, `5 s` `SIGKILL` timeout). Zero JS, zero remote fetch, HTML sanitized (`HTMLParser`), `pypdf` bounded (`<= 10` pages, `<= 4 MiB`/stream, `<= 8 MiB` total, recursion `<= 64`); output treated strictly as untrusted text. | [§13.4](#134-quota-resumable-sync-hmac-dedupe-and-sandboxed-worker) | Require the sidecar in production, restrict the subprocess fallback to explicit non-production use, and enforce a 5-second kill deadline on both paths. [§13.4](#134-quota-resumable-sync-hmac-dedupe-and-sandboxed-worker) | Fallback accepts fixture files only and refuses live Gmail/uploads. **New blocker:** require the parser socket only when Gmail or upload features are enabled. [§13.4](#134-quota-resumable-sync-hmac-dedupe-and-sandboxed-worker) | **RESOLVED in round 3; unchanged, including the parser-readiness blocker.** [§13.4](#134-quota-resumable-sync-hmac-dedupe-and-sandboxed-worker) | **RESOLVED; unchanged.** [§13.4](#134-quota-resumable-sync-hmac-dedupe-and-sandboxed-worker) | **RESOLVED; unchanged.** [§13.4](#134-quota-resumable-sync-hmac-dedupe-and-sandboxed-worker) |
| **7** | **Matching windows, FX & tie-breakers** | Define explicit per-source date windows & tolerances (BLIK `±2`d/`0`, Gateways `±4`d/`0` or `<= 25%` manual `fx_or_net`, Card `±4`d/`0` or `±7`d FX), static/configurable FX sanity rate corridors + merchant/gateway aliases (`APPLE.COM/BILL` ↔ Apple/OpenAI) with manual-only `link_basis = 'fx_or_net'`, and a 7-key deterministic tie-breaker + ambiguity flag. | [§13.5](#135-matching-windows-missing-proof-semantics-and-enrichment) | Bound manual BLIK to exact amount/currency; apply BLIK → gateway → card precedence; sort all-dates-missing rows last and exclude them from windowed matches. [§13.5](#135-matching-windows-missing-proof-semantics-and-enrichment) | Bank-debit evidence controls pair classification (BLIK receipt + gateway debit = gateway); a missing `transaction_date` with other dates sorts `NULLS LAST`; legacy `/confirm` retains exact BLIK `d <= 4`. [§13.5](#135-matching-windows-missing-proof-semantics-and-enrichment), [§13.7](#137-additive-api-contract-and-openapi-versioning) | Legacy `/parse-text` also uses exact BLIK `d <= 4` and issues matcher-bound candidate tokens accepted by legacy `/confirm`; the other round-3 matching rules remain resolved. [§13.5](#135-matching-windows-missing-proof-semantics-and-enrichment), [§13.7](#137-additive-api-contract-and-openapi-versioning) | **RESOLVED in round 4; unchanged.** [§13.5](#135-matching-windows-missing-proof-semantics-and-enrichment), [§13.7](#137-additive-api-contract-and-openapi-versioning) | **RESOLVED; unchanged.** [§13.5](#135-matching-windows-missing-proof-semantics-and-enrichment), [§13.7](#137-additive-api-contract-and-openapi-versioning) |
| **8** | **Missing-proof semantics & spouse view** | Separate `HESTIA_BUDGET_RECEIPTS_PROOF_CONFIG` (default `{"PLN": "500"}`); define qualifying `DBIT` `expense` rows & exclusions (`CRDT`, transfers, Krypto `excluded`, `pass_through` rent/mortgage); live flagging (`missing` / `needs_review`) and deterministic clearing; expose **only** non-PII `proof_status` and aggregate counts on P3 `/api/budget/household/*` (`viewer` + `X-Budget-Grant`), keeping all `/api/budget/receipts/*` routes strictly `_ADMIN`. | [§13.5](#135-matching-windows-missing-proof-semantics-and-enrichment), [§13.7](#137-additive-api-contract-and-openapi-versioning) | Default to the current `Europe/Warsaw` month; cap and deterministically order list items; count the full owner scope visible under the spouse grant before pagination/caps. [§13.5](#135-matching-windows-missing-proof-semantics-and-enrichment), [§13.7](#137-additive-api-contract-and-openapi-versioning) | **RESOLVED in round 2; unchanged.** [§13.5](#135-matching-windows-missing-proof-semantics-and-enrichment), [§13.7](#137-additive-api-contract-and-openapi-versioning) | **RESOLVED; unchanged.** [§13.5](#135-matching-windows-missing-proof-semantics-and-enrichment), [§13.7](#137-additive-api-contract-and-openapi-versioning) | **RESOLVED; unchanged.** [§13.5](#135-matching-windows-missing-proof-semantics-and-enrichment), [§13.7](#137-additive-api-contract-and-openapi-versioning) | **RESOLVED; unchanged.** [§13.5](#135-matching-windows-missing-proof-semantics-and-enrichment), [§13.7](#137-additive-api-contract-and-openapi-versioning) |
| **9** | **API compatibility & migration ordering** | Keep all new endpoints/DTO deltas additive and tolerant-reader compatible, bumping OpenAPI strictly past `0.31.0`/`0.32.0` (`0.33.0` `v1d-a`, `0.34.0` `v1d-b`, `0.35.0` `v1d-c`). Number migrations linearly after the category tree at `0020` (PR `#248`, revises `0019`, ahead of `#238`): `0023` (`v1d-a`) → `0024` (`v1d-b`) → `0025` (`v2`, shifted from `§5.1`), with strict single-head `down_revision` linearisation notes. | [§13.6](#136-data-model-and-migration-ordering), [§13.7](#137-additive-api-contract-and-openapi-versioning) | Map new enums for legacy contract versions; specify FK-off rebuild/check/restore handling; refuse downgrades while v1d-only rows remain. [§13.6](#136-data-model-and-migration-ordering), [§13.7](#137-additive-api-contract-and-openapi-versioning) | **New blocker:** scope the `0024` guard to `fx_or_net` and non-NULL v1d-b attachment fields; Gmail attachment rows using no `0024` fields may roll back. [§13.6](#136-data-model-and-migration-ordering) | **RESOLVED in round 3; unchanged, including the `0024` rollback blocker.** [§13.6](#136-data-model-and-migration-ordering) | **RESOLVED; unchanged.** [§13.6](#136-data-model-and-migration-ordering) | **RESOLVED; unchanged.** [§13.6](#136-data-model-and-migration-ordering) |

### 13.2 Baseline, owner facts, and phased rollout

**Owner facts overriding earlier assumptions:**

- **Google OAuth client (`klatt-ie`):** Gmail access reuses the existing Hestia Google OAuth client (the calendar client in GCP project `klatt-ie`, `In production` and Trusted in Workspace admin, `docs/CALENDAR_SYNC.md:63-72`). Its refresh token now also holds `https://www.googleapis.com/auth/gmail.readonly`, referenced via `.env` (`HESTIA_GOOGLE_CLIENT_ID`, `HESTIA_GOOGLE_CLIENT_SECRET`, and `HESTIA_GMAIL_REFRESH_TOKEN` or shared `HESTIA_GOOGLE_REFRESH_TOKEN`). **Inside Hestia, Gmail consent and execution stay strictly isolated from `calendar_sync`.**
- **Single mailbox + spouse forwards:** Only the **owner's** mailbox (`users/me`) is read. The spouse forwards her e-receipts to the owner's address; `v1d-a` unwraps forwarded mail in memory and recognizes the original merchant/sender only when the outer forward comes from a configured spouse address.
- **Proof for larger expenses:** Larger expenses (`>= 500 PLN` default) require an attached invoice, receipt, payment-request email (e.g., Apple/ChatGPT `999.99 PLN` billed via `APPLE.COM/BILL`), or optional manual PDF/photo attachment (`v1d-b`).
- **Opaque-debit enrichment + category tree:** Opaque `BLIK`, `Przelewy24` (`P24`), and `PayU` (plus `Tpay`, `Autopay`, `APPLE.COM/BILL`) debits are enriched by matching payment-confirmation emails on amount and date, then suggesting a category targeting **any non-archived node** (parent or leaf) in the `0020` `budget_categories` (`parent_id`) tree (`v1d-c`).
- **v1a reuse:** Reuses `v1a` (`PR #238`, migration `0022`, temporarily parented on `0020`) `budget_receipts` storage, `redact_pan()`, `reconcile_receipt_links`, and `BEGIN IMMEDIATE` verification (`receipt_store.py`, `receipt_parser.py`, `receipt_reconcile.py`).

**Runtime prerequisites (all default off):**

1. Base budget & receipts: `HESTIA_BUDGET_HTTP_ENABLED=1`, `HESTIA_PERSIST=sqlite`, authoritative SQLite `admin` owner, `HESTIA_BUDGET_RECEIPTS_ENABLED=1`.
2. **`v1d-a` (Gmail import + dedupe):** `HESTIA_BUDGET_GMAIL_IMPORT_ENABLED=1`, readable `HESTIA_BUDGET_RECEIPTS_GMAIL_CONFIG` (`<= 16 KiB` JSON), `HESTIA_BUDGET_RECEIPTS_HMAC_SECRET_FILE` (`0400`/`0600` via `config._read_secret`, separate from `HESTIA_SESSION_SECRET`), **and** active per-owner SQLite grant (`grant_status = 'active'`).
3. **`v1d-b` (Large-expense proof):** `HESTIA_BUDGET_RECEIPTS_PROOF_ENABLED=1`, optional `HESTIA_BUDGET_RECEIPTS_PROOF_CONFIG` (`<= 16 KiB` JSON, defaults to `{"PLN": "500"}` per owner), **and** updated `ui/public/privacy-policy.html` before enabling.
4. **`v1d-c` (Opaque-debit enrichment):** Requires `v1d-a` + `HESTIA_BUDGET_RECEIPTS_ENRICHMENT_ENABLED=1` (default `0`) **and** updated `ui/public/privacy-policy.html` before enabling.

The v1d-a implementation uses `HESTIA_BUDGET_GMAIL_IMPORT_ENABLED=1`, migration `0025` and contract version `0.40.0` on this branch. Clients can send `X-Hestia-Contract-Version: 0.40.0` to receive native import statuses and invoice fields on existing receipt reads. The deploy owner supplies an explicit refresh-token file reference. Calendar environment settings alone do not provide Gmail credentials.

Set `HESTIA_BUDGET_RECEIPTS_GMAIL_CONFIG` to a JSON file path and `HESTIA_BUDGET_RECEIPTS_HMAC_SECRET_FILE` to a dedicated HMAC key file with at least 32 bytes. For example, a config can contain these fields with synthetic mailbox identities and environment references.

```json
{
  "owner": "owner",
  "owner_mailbox": "invoices@example.test",
  "client_id_env": "HESTIA_GOOGLE_CLIENT_ID",
  "client_secret_env": "HESTIA_GOOGLE_CLIENT_SECRET",
  "refresh_token_env": "HESTIA_GMAIL_REFRESH_TOKEN_FILE",
  "sender_allowlist": ["billing@merchant.example"],
  "spouse_forward_senders": ["spouse@example.test"],
  "known_merchant_domains": ["merchant.example"],
  "label_name": "Receipts"
}
```

The environment variable named by `refresh_token_env` holds an absolute path to a file containing the refresh token. Mount that file and the HMAC key read-only in the Hestia container, owned by the runtime user with mode `0400` or `0600`. Start the `receipt-parser` Compose profile so the Gmail feature can check the parser socket before import. The isolated parser accepts HTML and plain text. PDF extraction is pending, so a message containing invoice fields only in a PDF is not imported.

```text
[Owner Gmail (gmail.readonly)]  ◄── gated by Flag=1 + Config + SQLite grant_status='active'
   │  Pre-flight: GET /users/me/profile == config.owner_mailbox (403 + halt on mismatch)
   ▼
[Resumable Sync (flock, 25-msg commits, 100/run, quota paced, 429 Retry-After backoff)]
   ├─ Dedupe BEFORE fetch: HMAC(hmac_secret, gmail_message_id) ∈ budget_gmail_messages? → skip
   ├─ Trust Gate: single From + Google MX Authentication-Results (dkim=pass & spf|dmarc=pass)
   │    ├─ Direct: From ∈ sender_allowlist
   │    └─ Forward: From ∈ exact spouse_forward_senders → unwrap rfc822/inline → verify merchant domain
   ├─ Sandboxed Worker (live input: production sidecar; dev/test subprocess: fixture files only; 5s kill deadline)
   │    ├─ HTML (≤1 MiB) → pure HTMLParser (no JS/CSS/remote fetch, stripped Cc/Cf/Cs) → ≤32 KiB text
   │    └─ PDF (≤5 MiB, ≤10p, %PDF-) → pypdf (≤4 MiB/stream, ≤8 MiB total, no JS) → ≤32 KiB text
   ▼
[PAN Redactor + Email Scrubber + Receipt/Invoice/Confirmation Parser]  (zero bodies/addresses stored)
   ├─ v1d-a: status='imported' (30d TTL, max 200), raw_ocr_text=NULL, provenance in budget_gmail_messages
   ├─ v1d-b: Per-source matcher (BLIK ±2d | Gateway ±4d | Card ±4d / FX ±7d) + missing-proof view + P3 proof_status
   └─ v1d-c: Opaque BLIK/P24/PayU/Apple suggestions + category suggestion (any node in 0020 tree) → manual confirm
```

### 13.3 Separate Gmail grant, identity verification, and sender trust

#### 1. Separate, revocable Hestia Gmail grant (`v1d-a`)

Having `HESTIA_GOOGLE_REFRESH_TOKEN` or `HESTIA_CALENDAR_CONFIG` configured for Calendar Sync **must never** start Gmail imports, enable Gmail routes, or acquire Gmail access tokens.

- **Per-owner opt-in (`POST /api/budget/receipts/gmail-grant`):** Browser-authenticated (`_principal` + `_browser` before body read) with `{"action": "enable" | "disable", "expected_version": int}`.
  - On `"enable"`: verifies feature flag + config, runs the `GET /users/me/profile` identity check against `config.owner_mailbox`, and inside `BEGIN IMMEDIATE` sets `grant_status = 'active'`, `mailbox_hmac = HMAC(hmac_secret, canonical_mailbox)`, `granted_at = now`, `revoked_at = NULL`, `grant_generation += 1`, `attention = NULL`, `version += 1`, writing a sanitized `budget_analytics_audit` row (`{action, grant_status, grant_generation, version}`).
  - On `"disable"`: inside `BEGIN IMMEDIATE`, sets `grant_status = 'disabled'`, increments `grant_generation` and `version`, and purges the cursor/pending/backoff fields listed in revocation step 3. It clears any in-memory cached Gmail access token on `GmailClient`, stops the background poller and manual `gmail-sync` (`409 budget_receipt_gmail_disabled`), and discards uncommitted read/parse buffers. It preserves `budget_gmail_messages` dedupe tombstones and all receipts for clean pause/resume. Disable is entirely local and **never** calls Google's token-revocation endpoint.
- **Revocation (`POST /api/budget/receipts/gmail-revoke`):** Accepts `{"expected_version": int}` inside `BEGIN IMMEDIATE`:
  1. **Serialize request dispatch against grant transition:** Each owner has one in-process `threading.RLock` shared by `GmailClient` and the grant-mutation service. Every sync-run Gmail API call (`profile`, `labels`, `messages.list`, `history.list`, `messages.get`, attachment downloads) and every token refresh acquires this lock, re-reads SQLite for `grant_status == 'active'` plus the run's captured generation, and holds the lock through dispatch and response-body completion. Disable/revoke acquire the **same lock off the event loop**, waiting at most **35 s** (beyond the 30 s transport timeout) for an in-flight request before opening `BEGIN IMMEDIATE` and committing status, `grant_generation += 1`, and `version += 1`. Lock timeout returns `503 budget_receipt_gmail_transition_busy` without changing grant state; the client retries. Thus revocation cannot commit between a successful pre-send fence and dispatch. The commit fence still re-checks status/generation and rolls back a stale chunk.
  2. Key the RAM access-token cache by **`(owner, grant_generation)`**; a generation/status mismatch drops every cached entry for that owner. Re-check generation after every Gmail/token response and before parsing, caching, or using it; **any response belonging to a changed generation is discarded**, so a racing refresh or Gmail response cannot resurrect access or contribute work. Disable/revoke wipe the owner's entries immediately after acquiring the shared lock; tokens are **never** written to SQLite or `/data`.
  3. Purges **all Gmail sync state** on `budget_gmail_state`: sets `history_id = NULL`, `pending_history_id = NULL`, `resume_page_token = NULL`, `history_page_token = NULL`, `scope_scan_page_token = NULL`, `scope_scan_query_hmac = NULL`, `resume_cutoff_ts = NULL`, `batch_processed_count = 0`, `sync_phase = 'idle'`, `mailbox_hmac = NULL`, `last_sync_at = NULL`, `next_sync_at = 0`, `backoff_until = 0`, `failures = 0`, `attention = NULL`, `last_http_status = NULL`, `last_error = NULL`, `backfill_completed_at = NULL`.
  4. In the same transaction, first sets `receipt_id = NULL` on **all** of the owner's `budget_gmail_messages` rows, then deletes lines and unconfirmed `status = 'imported'` receipts, and only then deletes all `budget_gmail_messages` rows for the owner. Confirmed/`needs_review` accounting receipts remain. This order guarantees no populated Gmail FK references a receipt being deleted.
  5. **Shared OAuth token safety (chosen over a second OAuth client):** Keeping the existing `klatt-ie` client avoids duplicate consent/deployment configuration, but provider-side revocation of any token for that shared client can also revoke Calendar access. Therefore **neither disable nor Hestia revocation ever calls Google's token-revocation endpoint**, even when `HESTIA_GMAIL_REFRESH_TOKEN` is separate; both stop local use, drop access tokens, and purge the state above. A separate manual operator step at [Google Account linked apps](https://myaccount.google.com/linkedapps) may revoke Google-side access, with an explicit warning that it **also affects Calendar Sync** and requires Calendar re-consent afterward. `gmail-status` links these instructions without attempting revocation itself.

#### 2. Mailbox profile / identity check (`v1d-a`)

- `HESTIA_BUDGET_RECEIPTS_GMAIL_CONFIG` (`<= 16 KiB` JSON, parsed via `_unique_error_fields` / `_reject_constant`) requires `owner`, `owner_mailbox`, `client_id_env`, `client_secret_env`, `refresh_token_env`, and explicit sender/spouse rules.
- At opt-in (`gmail-grant`) and at the start of **every** sync run (`gmail-sync` and hourly poller, before `messages.list`, `history.list`, or `messages.get`), `GmailClient` fetches `GET https://gmail.googleapis.com/gmail/v1/users/me/profile` (`1` quota unit).
- Normalize both `profile.get("emailAddress")` and `config.owner_mailbox` (require non-empty valid ASCII email without whitespace/control chars, `.strip().casefold()`) and compare via `hmac.compare_digest` (and verify `HMAC(hmac_secret, canonical_email) == state.mailbox_hmac` when active).
- **On mismatch (or missing `emailAddress`):** Refuse all sync calls immediately, clear the RAM access token, latch `budget_gmail_state.attention = 'identity_mismatch'` and `last_error = 'mailbox_mismatch'` inside `BEGIN IMMEDIATE`, halt background polling, and return `403 budget_receipt_gmail_mailbox_mismatch`. **Never** store, log, or return either email address in SQLite, audit rows, logs, or HTTP responses.

#### 3. Sender trust & spouse-forward unwrapping (`v1d-a`)

- **Fail-closed trust configuration:** At least one of non-empty `sender_allowlist` or non-empty `spouse_forward_senders` must be configured (`label_name` is an optional Gmail search narrow-down only, **never** a trust boundary — any labeled message failing sender trust is skipped as `outcome = 'skipped_untrusted_sender'`).
  - `sender_allowlist`: exact email addresses (`billing@openai.com`) or `@domain` suffixes (`@email.apple.com`, `@tm.openai.com`, `@allegro.pl`, `@payu.pl`, `@przelewy24.pl`, `@tpay.com`, `@autopay.pl`, `@inpost.pl`); config validation rejects broad public webmail wildcards (`@gmail.com`, `@icloud.com`, `@wp.pl`, `@onet.pl`, `@o2.pl`, `@interia.pl`, `@outlook.com`, `@yahoo.com`).
  - `spouse_forward_senders`: explicit exact email addresses of the spouse (`@domain` wildcards forbidden).
  - `known_merchant_domains`: allowlisted merchant/gateway domains merged with `sender_allowlist` domains for recognizing original senders inside spouse-forwarded mail.
- **Envelope & `Authentication-Results` verification:**
  1. Require exactly **one** `From` header containing exactly **one** `addr-spec` mailbox (`email.utils.getaddresses`), and reject messages where a `Sender:` header names a different domain than `From:` unless explicitly aligned.
  2. Trust only the **first (topmost) `Authentication-Results` field** and require its stripped/case-folded `authserv-id` to be exactly **`mx.google.com`**, **regardless of where any `Received` fields occur**. Gmail strips or renames incoming `Authentication-Results` fields claiming its own authserv-id, so a later field never overrides Gmail's topmost result; if the first field is absent or has another authserv-id, fail sender trust. Require `dkim=pass` and an aligned signing domain for that pass. If the AR result carries `header.d`, use it directly. If it does not, locate the corresponding actual `DKIM-Signature` field using **all** available correlation data: exact `header.i` ↔ the signature's **effective `i=`** (its explicit `i=`, or the RFC 6376 §3.5 default `"@" + d=` when `i=` is absent), a non-empty `header.b` prefix against the signature's whitespace-normalized `b=`, and/or exact selector `header.s` ↔ `s=`. The correlation must identify exactly **one** signature satisfying every supplied criterion; use that signature's real **`d=`** domain for alignment with the outer `From`. `header.i` is only a correlation key, never a substitute signing domain (RFC 6376 §3.5). No correlation data, no match, multiple matches, or an invalid/missing `d=` fails closed as `skipped_untrusted_sender`. Also require `spf=pass` or `dmarc=pass` aligned with the outer `From` domain.
  3. **Direct path (`trust_path = 'direct_allowlist'`):** Outer `From` matches `sender_allowlist` and passes step 2.
  4. **Spouse-forward path (`trust_path = 'spouse_forward'`):** Outer `From` matches an exact address in `spouse_forward_senders` and passes step 2. Only on this path does the parser unwrap `message/rfc822` MIME parts (max depth `2`) or bounded inline forward headers (`---------- Forwarded message ---------`, `Begin forwarded message:`, `Przekazana wiadomość`). Extract the inner `From` domain/display brand and match against `sender_allowlist ∪ known_merchant_domains` (`require_known_forwarded_merchant: bool = true` default; unrecognized non-merchant forwards record `skipped_untrusted_sender` / `skipped_non_receipt`).
  5. **Anti-spoofing & scrubbing:** Inner forwarded `From` or inner `Authentication-Results` headers **never** authorize an untrusted outer envelope. Immediately scrub all email addresses (`[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+` → removed) and apply `redact_pan()` before populating `raw_shop_name` or receipt fields.

### 13.4 Quota, resumable sync, HMAC dedupe, and sandboxed worker

#### 1. Gmail API quota handling, backoff, `historyId` cursor & resumable batches (`v1d-a`)

- **Transport & quota pacing (`hestia/budget/gmail_client.py`):** Reuses `HTTPTransport` (`_NoRedirect`, `30 s` timeout, `providers.py:46-70`) and the token refresh pattern (`providers.py:168-221`) against fixed constants (`TOKEN_URL = "https://oauth2.googleapis.com/token"`, `GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"`). Account explicit Gmail v1 quota units (`profile: 1`, `labels: 1`, `history.list: 2`, `messages.list: 5`, `messages.get: 5`), pace requests `>= 50 ms` apart (`<= 100 units/s` vs. `250 units/user/sec`), and cap each sync run at `MAX_SYNC_MESSAGES_PER_RUN = 100` and `MAX_QUOTA_UNITS_PER_RUN = 1000`.
- **Rate-limit & auth backoff:** On `401`, wipe RAM token and refresh once (`providers.py:211-214`); on `invalid_grant` or `403 ACCESS_TOKEN_SCOPE_INSUFFICIENT`, wipe RAM token, latch `attention = 'authorization'`, and halt background polling. On `429` or `403` rate-limit reason (`rateLimitExceeded`, `userRateLimitExceeded`, `quotaExceeded`, `dailyLimitExceeded` per `providers.py:74-77`), parse `Retry-After` via `parse_retry_after(..., now=now)` (`hestia/budget/rate_limit.py:117`) and compute `backoff_until = max(retry_deadline or 0, now + min(21600, 60 * 2**(failures - 1)) + jitter)`. Latch `attention = 'quota'` and pause polling if `dailyLimitExceeded` or `suspended=True` (`> 7d`).
- **Concurrency lock & cadence:** Serialized across `POST /gmail-sync` (`60 s` cooldown) and background poller (`sync_interval_seconds >= 3600`, running off-loop via `_settled_thread`) using per-DB `fcntl.flock` (`hestia/calendar_sync/runtime.py:20-32`).
- **Resumable state machine (`budget_gmail_state`):**
  1. **Initial backfill (`history_id IS NULL`, `sync_phase = 'backfill'`):** Before fetching page 1, capture `pending_history_id = profile.historyId` and freeze `resume_cutoff_ts = now - backfill_days * 86400` (`backfill_days` `1..365`, default `365`; `backfill_max_messages` `1..500`, default `500`, **per recovery cycle**). Build one `scope_query` from `after:<int(resume_cutoff_ts)>`, the configured `label_name` when present, and `from:` clauses for `sender_allowlist`/`spouse_forward_senders`; every `messages.list` call (`maxResults=100`) uses that same query.
  2. **Transactional chunk commits and grant fence:** Check `gmail_id_hmac` dedupe **before** calling `messages.get` (`format=metadata` to verify `sizeEstimate <= 10 MiB` + sender trust before `format=raw`). After reading/parsing at most **`BATCH_COMMIT_SIZE = 25`** messages, open `BEGIN IMMEDIATE` and re-check both `grant_status == 'active'` and the generation captured at run start. On mismatch, roll back and discard the entire chunk without writing receipts, provenance, counts, cursors, or tokens. A matching transaction atomically persists the chunk and its state updates.
  3. **Partial-page replay and token advancement:** `resume_page_token`/`history_page_token` identifies the page currently being drained. Message-bearing chunks before the page's final chunk leave it unchanged; the chunk committing the page's last uncommitted message replaces it with `nextPageToken`. **If a page is empty, fully deduped, or contains only conclusively out-of-scope IDs, issue a standalone cursor/page-token commit:** with no receipt writes, open `BEGIN IMMEDIATE`, re-check active status/generation, then advance the page token and, on the drained final page, promote the history cursor. Never use this state-only commit while any ID's eligibility is unknown. A crash otherwise replays the current page, but pre-fetch HMAC dedupe plus the `(owner, gmail_id_hmac)` primary-key no-op makes replay idempotent: already-committed messages create no second receipt and do not increment import/skip counts again. Neither page tokens nor `history_id` advance outside a successful message or standalone state commit. On run/quota/`429` interruption, retain `sync_phase`, `pending_history_id`, and the committed token and schedule continuation.
  4. **Recovery cap and safe promotion:** Reaching `backfill_max_messages` ends only the current recovery cycle: commit the current chunk, keep `history_id` unchanged and retain `pending_history_id`, `sync_phase`, and `resume_page_token`; reset `batch_processed_count = 0` for the next cycle and schedule it. The 500-message cap is **never** a completion condition. Only the final chunk after `nextPageToken IS None` and every listed in-scope message has a committed terminal outcome may promote `history_id = pending_history_id`, clear pending/page/cutoff state, set `sync_phase = 'idle'`, and set `backfill_completed_at = now`. Thus the cursor never passes the last fully processed message and `pending_history_id` survives until the backlog is drained.
  5. **Incremental sync with receipt-only scope and a query-bound scan checkpoint (`history_id IS NOT NULL`, `sync_phase = 'incremental'`):** On a new phase (`pending_history_id IS NULL`), capture `pending_history_id` from the profile and initialize `scope_scan_page_token = NULL`. Any existing incremental `pending_history_id` denotes a continuation—even when both page tokens are NULL—so retain the original `history_id`, `pending_history_id`, and `history_page_token` baseline. Query `users.history.list?startHistoryId=<id>&historyTypes=messageAdded&historyTypes=labelAdded`. When `label_name` is configured, resolve its exact Gmail `labelId` and pass `labelId` to every `history.list` request; that server-side filter establishes label eligibility, so do not require history `message.labelIds`.
     Canonically encode the effective scope as compact, sorted-key JSON containing the resolved `labelId` and sorted, normalized sender/spouse clauses (no backfill cutoff). Store `scope_scan_query_hmac = HMAC-SHA256(hmac_secret, "hestia/budget/gmail-scope-query/v1\0" + canonical_query)` beside the scan token, never raw sender/query text. Before resuming, compare it with the current scope. **On query-HMAC mismatch**, use a fenced state commit to set **both** `history_page_token = NULL` and `scope_scan_page_token = NULL` and store the new HMAC; restart `history.list` at page 1 with `startHistoryId = <retained history_id>` and restart each scope scan at page 1. Earlier history pages are therefore re-checked under the new scope, with dedupe keeping replay idempotent. **On Gmail `400`/invalid scope-scan token from `messages.list`**, clear only `scope_scan_page_token` and restart that scan at page 1 while retaining the current history page (AIP-158). **On Gmail `400`/invalid history-page token from `history.list`**, clear both page tokens, retain the query HMAC, and restart history pagination at page 1 from the retained baseline `history_id`. All three paths preserve `history_id` and `pending_history_id`; if the baseline itself returns `404`/expired `historyId`, enter the existing full-resync fallback in step 6. Allow one automatic restart per run; a 400 on tokenless page 1 latches `attention = 'configuration'` without advancing any cursor.
     For each current history page, treat its IDs as candidates and scan `messages.list` using the current effective scope, starting/resuming at `scope_scan_page_token`. Process discovered in-scope candidates in 25-message chunks; **every message or standalone scan commit atomically persists `scope_scan_page_token`, `scope_scan_query_hmac`, and the unchanged `pending_history_id` baseline**, while `history_page_token` remains on the current history page. The scan token advances to `nextPageToken` only after all discovered candidates from that scan page commit; empty/no-candidate/deduped scan pages use a fenced standalone state commit. On ordinary quota exhaustion/error, retain both checkpoints, the query HMAC, and the history baseline so the next run resumes the exact scan; only query-mismatch/invalid-token recovery restarts at page 1.
     When the scoped scan reaches its final page, candidates not found are conclusively out of scope; only then may a standalone commit advance `history_page_token` and clear `scope_scan_page_token` for the next history page. Incomplete/erroring scans leave eligibility **unknown**: do not call `messages.get` or advance any scan/page/history cursor past those IDs. After all history pages drain and every candidate is committed or conclusively out of scope, the final commit promotes `history_id = pending_history_id` and clears both page/scan checkpoints plus the scan query HMAC.
  6. **Full-resync fallback:** When `users.history.list` returns `HTTP 404` (expired `historyId`) or `POST /gmail-sync` passes `force_full_resync=True`, clear `history_id = NULL, history_page_token = NULL, scope_scan_page_token = NULL, scope_scan_query_hmac = NULL`, transition `sync_phase = 'full_resync'`, set `pending_history_id = profile.historyId` and `resume_cutoff_ts = max((last_sync_at or now) - 7*86400, now - backfill_days*86400)`, and run the same scoped, resumable `messages.list` flow with 500-message recovery-cycle pacing, skipping existing `gmail_id_hmac` rows cleanly and never promoting at the cap.

#### 2. Keyed HMAC dedupe on Gmail message ID & idempotent re-import (`v1d-a`)

- **Primary dedupe key (`gmail_id_hmac`):** Compute `hmac.new(hmac_secret, b"hestia/budget/gmail-msg-id/v1\0" + gmail_message_id.encode("ascii"), hashlib.sha256).hexdigest()` using dedicated `HESTIA_BUDGET_RECEIPTS_HMAC_SECRET_FILE` (`0400`/`0600` via `config._read_secret`, never `HESTIA_SESSION_SECRET`). **Never** store raw `gmail_message_id`, `threadId`, or RFC `Message-ID`.
- **Duplicate-copy suppression (`rfc_message_id_hmac`):** Optionally compute a nullable `HMAC-SHA256` of normalized RFC `Message-ID` (after outer trust passes) so if both a direct merchant copy and a spouse-forwarded copy carry the same `Message-ID`, the second copy records `outcome = 'skipped_duplicate'` without creating a duplicate receipt.
- **Idempotency & tombstones:**
  - Checked in SQLite **before** `messages.get` (zero duplicate fetches on resume/full-resync/`labelAdded`).
  - Inside `BEGIN IMMEDIATE`, `PRIMARY KEY (owner, gmail_id_hmac)` treats concurrent or replayed inserts safely as a no-op (never `500`).
  - Deleting an imported receipt (`POST /delete`) or 30-day TTL expiry updates `budget_gmail_messages` (`receipt_id = NULL, outcome = 'deleted'`) so later syncs/resyncs do not resurrect it.
  - Transient network/429/worker errors write **no** tombstone so the next run retries; terminal outcomes (`'imported'`, `'skipped_non_receipt'`, `'skipped_untrusted_sender'`, `'skipped_oversized'`, `'skipped_duplicate'`, `'parse_failed'`, `'deleted'`) are recorded as minimal provenance without raw bodies or addresses.

#### 3. Sandboxed HTML / PDF worker (`v1d-a` + `v1d-b`)

Untrusted email HTML (`<= 1 MiB`) and PDF payloads (`<= 5 MiB`, `%PDF-` magic, both Gmail attachments in `v1d-a` and manual PDF uploads in `v1d-b`) are **never** parsed inside the main `hestia` process.

- **Isolated execution boundary:**
  - **Docker Compose production (`receipt-parser` service) — mandatory:** Dedicated container running `user: "1000:1000"`, **`network_mode: "none"`** (kernel-enforced zero LAN/internet egress), `read_only: true`, `cap_drop: ["ALL"]`, `security_opt: ["no-new-privileges:true"]`, `mem_limit: 256m`, `pids_limit: 32`, `cpus: 0.5`, `tmpfs: [/tmp:rw,noexec,nosuid,size=32m]`. Mounts **zero** `/data`, DB, `docker.sock`, or secret paths; communicates with `hestia` strictly via a mounted Unix domain socket (`/run/hestia-receipt-parser/worker.sock`) using length-framed binary/JSON messages (`<= 5.5 MiB` request, `<= 64 KiB` response). Require a healthy socket only in the **feature-scoped readiness check when Gmail import or receipt upload/attachment endpoints are enabled**; with both disabled, production startup and overall readiness do not depend on the parser. Production **never** selects the subprocess fallback. Enabling an affected feature with an unavailable sidecar fails that feature's readiness, and parse attempts return `503` without a terminal Gmail tombstone or persisted upload.
  - **Explicit non-production fixture fallback (`ReceiptParserWorker`):** Allowed only outside production with `HESTIA_BUDGET_RECEIPTS_PARSER_ALLOW_SUBPROCESS=1`, and **only for fixture files**. The parent resolves a server-selected fixture beneath a fixed fixture root with `dir_fd` + `O_NOFOLLOW`, requires a regular HTML/PDF file, and passes only that fixture to the child. Live Gmail bodies/attachments and multipart/manual uploads are forbidden: Gmail sync refuses fallback processing before fetching message content, and `/attach` returns `503` before reading its body; no live bytes reach the host-backed subprocess. The child uses `subprocess.Popen` with `env={}` (zero inherited secrets), empty temp `cwd`, `RLIMIT_AS = 256 MiB`, `RLIMIT_CPU = 5 s`, `RLIMIT_FSIZE = 0`, `RLIMIT_NOFILE = 32`, and pipe-only I/O. Concurrency is serialized via one startup `_PARSER_WORKER_SEMAPHORE = threading.Semaphore(1)`.
  - **Wall-clock kill deadline on both paths:** Hestia applies a `5.0 s` monotonic request deadline to every parse, discards late responses, and never commits partial output. Fallback expiry calls `proc.kill()` (`SIGKILL`). The sidecar forks one child per request and its supervisor enforces the same deadline with `SIGKILL`; Hestia closes the timed-out IPC request, and the supervisor treats that disconnect as cancellation and kills the request child so no orphaned parse continues. Both paths count only wall-clock time, not just CPU time.
- **Untrusted content rules inside worker:**
  - **HTML (`<= 1 MiB` raw decoded bytes):** Pure-Python `html.parser.HTMLParser` (`convert_charrefs=True`, comments skipped); zero JS engine, zero CSS/DOM execution, zero remote resource loads (`http:`/`https:`/`cid:`/`data:`/`javascript:` dropped); discards entire `<script>`, `<style>`, `<noscript>`, `<template>`, `<iframe>`, `<object>`, `<embed>`, `<svg>`, `<meta>` subtrees; inserts newlines on block/table tags; normalizes `NFC`; strips `Cc`/`Cf`/`Cs` chars; caps output at `32 KiB` / `500` lines.
  - **PDF (`<= 5 MiB`, `%PDF-`):** Pure-Python `pypdf` only (no Poppler/MuPDF/Ghostscript C binaries); enforces `MAX_PDF_PAGES = 10`, `MAX_PDF_STREAM_BYTES = 4 MiB` per stream, `MAX_PDF_DECOMPRESSED_BYTES = 8 MiB` total, `MAX_PDF_RECURSION = 64`; rejects password-encrypted PDFs; ignores/never executes `/JS`, `/JavaScript`, `/OpenAction`, `/AA`, `/Launch`; caps extracted plain text at `32 KiB` / `500` lines.
- **Parent validation:** `hestia` treats worker output strictly as untrusted UTF-8 text (re-verifies `<= 32 KiB` / `500` lines, control chars, `redact_pan()`, and email-address scrubbing), discards raw HTML and Gmail PDF bytes immediately (`raw_ocr_text = NULL` for Gmail), and serves manual PDFs (`v1d-b`) solely as downloads (`Content-Disposition: attachment`, `nosniff`, `CSP: default-src 'none'; sandbox`).

### 13.5 Matching windows, missing-proof semantics, and enrichment

#### 1. Per-source matching windows, FX handling & tie-breaking (`v1d-b`)

Distance `d` is `min(abs((date.fromisoformat(tx_d) - receipt_date).days) for tx_d in non_null(transaction_date, booking_date, value_date))` (`v1a` D2). All matches require selected `BudgetAccount.enabled == True`, `uncertain == False`, `BOOK`, not in `budget_transfer_legs`, matching `DBIT`/`CRDT` direction, and explicit owner confirmation inside `BEGIN IMMEDIATE`:

| Source / Payment Path | `link_basis = "exact"` (Tier 1) | `link_basis = "fx_or_net"` (Tier 2 — suggestion only, manual confirm) |
| --- | --- | --- |
| **BLIK** (BLIK bank debit; see pair precedence) | v1d `d <= 2` calendar days; legacy `/confirm` retains v1a exact `d <= 4`; same currency; **`0` amount tolerance** (`canonical_money` exact) | Not offered: explicit v1d manual BLIK selection requires `d <= 2`, identical currency, and exact canonical amount; BLIK never uses `fx_or_net` |
| **Payment Gateways** (`Przelewy24`/`P24`/`PayU`/`Tpay`/`Autopay`) | `d <= 4` calendar days; same currency; **`0` amount tolerance** | Same-currency VAT/fee delta `<= 25%` within `d <= 4`d, **or** FX within `d <= 7`d with gateway/merchant hit + FX corridor check (`auto_suggest = False`) |
| **Card / Subscription** (`payment_method = 'card'`, incl. `APPLE.COM/BILL`) | `d <= 4` calendar days (`±4`d Friday→Monday rule); same currency; **`0` amount tolerance** | Same-currency invoice VAT/fee/tip delta `<= 25%` (`|tx - receipt| / tx <= 0.25`) within `d <= 4`d, **or** cross-currency FX within `d <= 7`d (`auto_suggest = False`) |

- **Pair classification, legacy window, and missing dates:** Classify each receipt/transaction pair by **bank-debit evidence first**, in **BLIK → payment gateway → card/subscription** precedence; a receipt's `payment_method` is consulted only when the bank debit has no BLIK/gateway/card signature. A BLIK receipt paired with a gateway debit therefore uses **gateway** rules, while a BLIK debit remains BLIK even if gateway/merchant aliases also match and cannot fall through to a 7-day FX window. Only v1d paths use BLIK `d <= 2`. Legacy `/parse-text` without `X-Hestia-Contract-Version >= 0.33.0` uses v1a's exact BLIK `d <= 4` to emit candidates and binds each candidate token to that legacy matcher; legacy `/confirm` accepts those tokens and revalidates the same 4-day window, exact amount/currency, and all other v1a checks. A matcher-version mismatch requires reparsing; this does not widen v1d candidates, Tier 2, or enrichment.
  Individually missing dates are ignored by the `min(...)` distance calculation. `tx_date_distance` uses **only** `transaction_date`: when it is missing but `booking_date` or `value_date` exists, the candidate remains eligible using the available-date `d`, but its `tx_date_distance = NULL` sorts **after** equal-`d` candidates having a real transaction date; if both candidates lack it, continue to `amount_delta_bps` and the remaining keys. If all three dates are missing, `d = NULL` and `tx_date_distance = NULL`: the row fails every windowed tier (including manual BLIK) and sorts last in manual candidate search with `NULLS LAST` on both date keys.
- **FX handling (`tx.currency != receipt.currency`, e.g., USD/EUR Apple/OpenAI invoice → `999.99 PLN` `APPLE.COM/BILL`):**
  1. Requires `d <= 7` days, matching direction, **and** either `merchant_token_hit == True` (using curated aliases such as `APPLE.COM/BILL` ↔ `{apple, openai, chatgpt, icloud}`, `PAYPRO`/`P24` ↔ `{przelewy24}`, `PAYU` ↔ `{payu}`) or explicit manual transaction search.
  2. Bounded by configurable static FX sanity corridors (`fx_rate_bounds`, e.g., default `USD/PLN ∈ [3.00, 5.20]`, `EUR/PLN ∈ [3.50, 5.50]`, `GBP/PLN ∈ [4.00, 6.50]`, `CHF/PLN ∈ [3.50, 5.50]`, overridable in proof config — zero external FX network calls).
  3. Every Tier-2 candidate returns `match_tier = 'fx_or_net'`, `auto_suggest = False`, `requires_manual_confirm = True`. `POST /confirm` defaults `link.link_basis = 'exact'` (preserving `v1a` strict rejection with `400`); linking FX or gross/net deltas requires explicit `link_basis = 'fx_or_net'`, re-checked inside `BEGIN IMMEDIATE` (`409` on `uq_budget_receipts_tx_link`, plus `reconcile_receipt_links` drift protection).
- **Deterministic tie-breaking & ambiguity:** Sort candidates by `(tier_rank ASC, source_alignment DESC, merchant_token_hit DESC, d ASC NULLS LAST, tx_date_distance ASC NULLS LAST, amount_delta_bps ASC, account_id ASC, entry_reference ASC)` (preferring `exact`, same payment method/source, merchant/NIP hit, closest overall date, closest `transaction_date`, smallest relative delta, then lexicographic ID). If `>= 2` candidates tie on the first 6 keys, set `ambiguous_top_match = True` and `auto_suggest = False`.

#### 2. Missing-proof config, flagging, clearing & spouse visibility (`v1d-b`)

- **Threshold config (`HESTIA_BUDGET_RECEIPTS_PROOF_CONFIG`, decoupled from Gmail):** Server-only `<= 16 KiB` JSON keyed by `owner` (matching `month_budget.py:110-168`), containing `proof_thresholds` (ISO-4217 currency → positive canonical decimal string; defaults to `{"PLN": "500"}` when `HESTIA_BUDGET_RECEIPTS_PROOF_ENABLED=1` and unset) and optional `fx_rate_bounds`.
- **Qualifying transactions & exclusions:** Evaluated live from one read-only SQLite snapshot (`projection_snapshot` at `analytics_views.py:221-256`, zero mutation of `analytics_views.py` totals or existing endpoints). A transaction **requires proof** iff:
  - Selected (`BudgetAccount.enabled == True`), `uncertain == False`, `status == "BOOK"`, `credit_debit_indicator == "DBIT"`, `effective["kind"] == "expense"`, `currency in proof_thresholds`, and `compare_money(amount, proof_thresholds[currency]) >= 0`.
  - **Automatically excluded:** All `CRDT` rows, transfers (`effective["kind"] == "transfer"` and `budget_transfer_legs`), Krypto (`effective["kind"] == "excluded"`), configured `pass_through` rent/mortgage categories (`month_budget.load_month_config()`), currencies absent from `proof_thresholds`, and debits below threshold.
- **How "missing proof" is flagged and cleared:**
  - **Flagged (derived dynamically; never stored as a stale flag on `budget_transactions`):** `"missing"` when no `BudgetReceipt` is linked with `status IN ('confirmed', 'needs_review')` (`draft` and unconfirmed `'imported'` rows do **not** satisfy proof); `"needs_review"` when the linked receipt drifted (`reconcile_receipt_links`).
  - **Cleared when:** (a) owner confirms & links a `BudgetReceipt` (`status = 'confirmed'`) via Gmail import, manual PDF/photo attachment (`POST /attach` + `POST /confirm`, allowing `lines=[]` for invoices/emails), or paste; (b) owner re-confirms a `needs_review` receipt (`POST /confirm` CAS); or (c) transaction ceases to qualify (reclassified to `excluded`/`transfer`, moved to pass-through category, or account deselected).
- **Missing-proof list defaults, ordering, and cap:** When `period` is omitted, `GET /missing-proof` uses the **current month in `Europe/Warsaw`** at request time (`zoneinfo.ZoneInfo`, never UTC); explicit `YYYY-MM`/`YYYY` periods also compute inclusive `start` and exclusive `end` in that timezone, including DST transitions, and return `evaluation_timezone = "Europe/Warsaw"`. Return at most **`items_cap = 100`** items ordered by `proof_status` (`missing` before `needs_review`), then `COALESCE(transaction_date, booking_date, value_date) DESC NULLS LAST`, then `account_id ASC, entry_reference ASC`. Compute `missing_count`, `needs_review_count`, and `total_flagged_count` over the complete filtered set before the cap; `truncated = (total_flagged_count > len(items))`.
- **Manual PDF / image attachment (`POST /attach`, `GET /attachment`):** Multipart (`<= 6 MiB`, `Content-Length` required, no chunked; `_principal` + `_browser` before reading body). Sniffs magic bytes: images delegate to `v1b` WebP + RapidOCR/Cloud-AI; `%PDF-` runs through the sandboxed PDF worker (`§13.4`), applies `redact_pan()` + email scrubber, stores PAN-redacted text in `raw_ocr_text`, and optionally persists validated PDF bytes in `ReceiptFiles` (`256 MiB` byte + `20`-draft owner quotas, `48h` draft / `30d` post-confirm TTL). `GET /attachment` streams PDFs via `dir_fd` + `O_NOFOLLOW` with `Content-Type: application/pdf`, `Content-Disposition: attachment; filename="receipt-<uuid>.pdf"`, `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'none'; sandbox`, `Cache-Control: no-store`, `Referrer-Policy: no-referrer`.
- **What the spouse sees (P3 Household alignment):** All `/api/budget/receipts/*` routes, receipt text, merchant/shop names, filenames, email metadata, and attachments stay **strictly owner-private (`_ADMIN` only; `403` for `viewer`)**. When both P3 Household (`budget_household_members`) and `v1d-b` proof are active, `GET /api/budget/household/transactions` adds **only** a non-PII enum `proof_status: Literal["not_required", "attached", "missing", "needs_review"]` per row (evaluated inside P3's double-snapshot `require_household_data_grant` gate), and `GET /api/budget/household/quarantine` adds integer counts `missing_proof_count` and `needs_review_proof_count`. Each count covers **all qualifying rows for the single owner authorized by that grant**, using that owner's selected accounts and the exclusions above, before transaction pagination and independently of the owner list's 100-item cap; it never aggregates other owners or unauthorized accounts. The spouse sees which large shared-budget debit still needs an e-receipt forwarded, with **zero** exposure of receipt contents, merchant names, emails, or attachments.

#### 3. Opaque-debit enrichment & category-tree suggestions (`v1d-c`)

- **Opaque debit detection & confirmation matching:** Pure read helper identifies selected `BOOK`, non-uncertain `DBIT` transactions (`effective["kind"] in ("expense", "auto")`, not in `budget_transfer_legs`) whose `remittance_information` / `creditor_name` matches opaque patterns (`Płatność BLIK … Nr transakcji` without merchant, `Przelewy24`/`P24`/`PAYPRO`, `PayU`, `Tpay`/`Krajowy Integrator Płatności`, `Autopay`/`Blue Media`, `APPLE.COM/BILL`) or lacks merchant details. Pairs them against unlinked `imported`/`confirmed` Gmail payment confirmations (`document_kind = 'payment_confirmation'`) using the exact per-source windows & tie-breakers above.
- **Category suggestions across the `0020` category tree:** For each matched opaque debit, evaluates a pure deterministic suggestion (`suggested_category_id`, `suggested_category_name`, `suggestion_reason: 'rule_match' | 'merchant_history' | 'receipt_shop'`) using: (1) owner's `budget_rules` evaluated over the confirmation email's sanitized merchant/product tokens, and (2) owner's prior manual `budget_annotations` or confirmed receipts for that normalized merchant (`shop_id`/`chain_key`). **Suggestions may target ANY non-archived node (`parent_id IS NULL` parent/root OR `parent_id IS NOT NULL` child/leaf) in the `0020` `budget_categories` tree.**
- **Explicit review only:** `GET /api/budget/receipts/enrichment-suggestions` returns suggestions for owner review. Confirming the receipt link calls `POST /receipts/confirm` inside `BEGIN IMMEDIATE`; applying the suggested category (optional checkbox/action) calls the existing reviewed `POST /api/budget/analytics/annotation` endpoint (`expected_version` CAS, `ledger_fingerprint`-bound). `budget_transactions.payload_json` and `budget_transaction_metadata` are **never** mutated.

### 13.6 Data model and migration ordering

#### Linear Alembic migration chain & `down_revision` notes

Hestia enforces a **single linear Alembic head** (`hestia/db.py:813`). Exact ordering across in-flight branches:

1. `0019_budget_owner_inbox_reads` (`master` at `7f94dce`)
2. `0020_budget_category_parent` (PR `#248`; `budget_categories.parent_id`; `down_revision = "0019"`). On master.
3. `0021` one-off annotation label (PR `#251`, not on master yet). Receipts `v1a` ships as `0022_budget_receipts` (PR `#238`, designed in §5.1 as `0020_budget_receipts`). Its `down_revision` is temporarily `"0020"` so this branch stays a single head. Once `#251` merges, Holzera re-parents `0022` onto `"0021"`. Household `P3` re-parents onto the then-current head the same way. OpenAPI `0.35.0` is reserved for `#251`; this receipts contract is `0.36.0`.
4. **`0023_budget_receipts_v1d_a` (`v1d-a`):** `down_revision` is the then-current head — creates `budget_gmail_state` + `budget_gmail_messages`, and alters `budget_receipts` (`batch_alter_table`) for `'imported'`, `'email'`/`'attachment'`, `document_kind`, `import_expires_at`.
5. **`0024_budget_receipts_v1d_b` (`v1d-b`):** `down_revision = "0023"` — alters `budget_receipts` (`batch_alter_table`) to add `link_basis`, `attachment_relpath`, `attachment_mime`, `attachment_expires_at`, backfills `UPDATE budget_receipts SET link_basis = 'exact' WHERE linked_account_id IS NOT NULL`, and updates `ck_budget_receipts_link_fields`.
6. **`v1d-c`:** **zero migration** (uses `0024` + `0020` `budget_categories` tree + `0015` `budget_annotations`).
7. **`0025_budget_receipt_products` (`v2`, previously numbered `0021` in `§5.1`):** renumbered after `0024` (`down_revision = "0024"`).

*Merge-order rule:* At PR merge time, update `down_revision` to the exact then-current `head` in `hestia/migrations/versions/` so two Alembic heads never exist. Verify child-first `downgrade()` and that every migration connection finishes with `PRAGMA foreign_keys=ON` (`hestia/db.py:757`).

**SQLite rebuild protocol for `0023`/`0024`:** Changing `budget_receipts` CHECK constraints requires a batch rebuild even though child FKs may already reference it. Use a dedicated migration connection with writers stopped and FK enforcement initially on; execute `PRAGMA foreign_keys=OFF` **before Alembic begins the transaction** (a PRAGMA inside a transaction is a no-op), perform the copy/rename while preserving IDs and child rows, and run `PRAGMA foreign_key_check` before commit. Any reported violation aborts the migration. After commit, restore `PRAGMA foreign_keys=ON` and verify it reads `1`. Apply the same procedure to both upgrade and downgrade; never use the temporary FK-off setting to accept a violation.

#### Migration `0023_budget_receipts_v1d_a` + `0024_budget_receipts_v1d_b` DDL

```sql
-- 1. budget_gmail_state (0023, v1d-a): separate revocable grant + resumable sync cursor
CREATE TABLE budget_gmail_state (
    owner                   TEXT PRIMARY KEY REFERENCES users(username),
    grant_status            TEXT NOT NULL DEFAULT 'disabled' CHECK (grant_status IN ('disabled', 'active', 'revoked')),
    grant_generation        INTEGER NOT NULL DEFAULT 0 CHECK (grant_generation >= 0),
    granted_at              REAL NULL CHECK (granted_at IS NULL OR granted_at >= 0),
    revoked_at              REAL NULL CHECK (revoked_at IS NULL OR revoked_at >= 0),
    mailbox_hmac            TEXT NULL CHECK (mailbox_hmac IS NULL OR (length(mailbox_hmac) = 64 AND mailbox_hmac NOT GLOB '*[^0-9a-f]*')),
    sync_phase              TEXT NOT NULL DEFAULT 'idle' CHECK (sync_phase IN ('idle', 'backfill', 'incremental', 'full_resync')),
    history_id              TEXT NULL CHECK (history_id IS NULL OR length(history_id) BETWEEN 1 AND 64),
    pending_history_id      TEXT NULL CHECK (pending_history_id IS NULL OR length(pending_history_id) BETWEEN 1 AND 64),
    resume_page_token       TEXT NULL CHECK (resume_page_token IS NULL OR length(resume_page_token) BETWEEN 1 AND 512),
    history_page_token      TEXT NULL CHECK (history_page_token IS NULL OR length(history_page_token) BETWEEN 1 AND 512),
    scope_scan_page_token   TEXT NULL CHECK (scope_scan_page_token IS NULL OR length(scope_scan_page_token) BETWEEN 1 AND 512),
    scope_scan_query_hmac   TEXT NULL CHECK (scope_scan_query_hmac IS NULL OR (length(scope_scan_query_hmac) = 64 AND scope_scan_query_hmac NOT GLOB '*[^0-9a-f]*')),
    resume_cutoff_ts        REAL NULL CHECK (resume_cutoff_ts IS NULL OR resume_cutoff_ts >= 0),
    batch_processed_count   INTEGER NOT NULL DEFAULT 0 CHECK (batch_processed_count >= 0),
    last_sync_at            REAL NULL CHECK (last_sync_at IS NULL OR last_sync_at >= 0),
    next_sync_at            REAL NOT NULL DEFAULT 0 CHECK (next_sync_at >= 0),
    backoff_until           REAL NOT NULL DEFAULT 0 CHECK (backoff_until >= 0),
    failures                INTEGER NOT NULL DEFAULT 0 CHECK (failures >= 0),
    attention               TEXT NULL CHECK (attention IS NULL OR attention IN ('authorization', 'configuration', 'identity_mismatch', 'quota')),
    last_http_status        INTEGER NULL CHECK (last_http_status IS NULL OR (last_http_status BETWEEN 100 AND 599)),
    last_error              TEXT NULL CHECK (last_error IS NULL OR length(last_error) <= 64),
    backfill_completed_at   REAL NULL CHECK (backfill_completed_at IS NULL OR backfill_completed_at >= 0),
    version                 INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1)
);

-- 2. budget_gmail_messages (0023, v1d-a): minimal provenance + HMAC(gmail_message_id) dedupe (zero bodies/addresses)
CREATE TABLE budget_gmail_messages (
    owner                   TEXT NOT NULL REFERENCES users(username),
    gmail_id_hmac           TEXT NOT NULL CHECK (length(gmail_id_hmac) = 64 AND gmail_id_hmac NOT GLOB '*[^0-9a-f]*'),
    rfc_message_id_hmac     TEXT NULL CHECK (rfc_message_id_hmac IS NULL OR (length(rfc_message_id_hmac) = 64 AND rfc_message_id_hmac NOT GLOB '*[^0-9a-f]*')),
    internal_date           REAL NOT NULL CHECK (internal_date >= 0),
    trust_path              TEXT NOT NULL CHECK (trust_path IN ('direct_allowlist', 'spouse_forward', 'untrusted')),
    outcome                 TEXT NOT NULL CHECK (outcome IN ('imported', 'skipped_non_receipt', 'skipped_untrusted_sender', 'skipped_oversized', 'skipped_duplicate', 'parse_failed', 'deleted')),
    receipt_id              TEXT NULL,
    created_at              REAL NOT NULL CHECK (created_at >= 0),
    PRIMARY KEY (owner, gmail_id_hmac),
    FOREIGN KEY (receipt_id, owner) REFERENCES budget_receipts(id, owner)
);
CREATE INDEX ix_budget_gmail_messages_owner_date ON budget_gmail_messages(owner, internal_date);
CREATE INDEX ix_budget_gmail_messages_owner_rfc ON budget_gmail_messages(owner, rfc_message_id_hmac) WHERE rfc_message_id_hmac IS NOT NULL;
```

**`budget_receipts` deltas (`batch_alter_table`, preserving `0020` rows and `uq_budget_receipts_tx_link`):**
- **`0023` (`v1d-a`):** Expand `ck_budget_receipts_status` (`'draft'`, `'imported'`, `'confirmed'`, `'needs_review'`), `ck_budget_receipts_source` (`'live_text'`, `'local_ocr'`, `'cloud_ai'`, `'paste'`, `'photo'`, `'email'`, `'attachment'`), `ck_budget_receipts_confirmed_fields` (`status IN ('draft', 'imported') OR (...)`), and `ck_budget_receipts_link_fields` (`status IN ('confirmed', 'needs_review')` when linked); add `document_kind` (`'receipt' | 'invoice' | 'payment_confirmation'`, default `'receipt'`) and `import_expires_at` (`REAL NULL`).
- **`0024` (`v1d-b`):** Add `link_basis` (`NULL | 'exact' | 'fx_or_net'`), `attachment_relpath`, `attachment_mime` (`NULL | 'application/pdf'`), `attachment_expires_at`; backfill `link_basis = 'exact'` on existing linked rows; enforce `link_basis` non-null whenever linked in `ck_budget_receipts_link_fields`.
- **Delete order:** On `POST /delete` or import expiry, first `UPDATE budget_gmail_messages SET receipt_id = NULL, outcome = 'deleted'`, then delete `budget_receipt_lines` → `budget_receipts` → commit → unlink files. Revocation likewise clears **all** Gmail `receipt_id` FKs before deleting any imported lines/receipts, then deletes the Gmail provenance rows.
- **Refuse unsafe downgrades; do not silently delete accounting data:** Before any schema change, `0024.downgrade()` refuses with a sanitized count **only** when rows actually require v1d-b semantics: `link_basis = 'fx_or_net'` or any of `attachment_relpath`/`attachment_mime`/`attachment_expires_at` is non-NULL. A Gmail `source_kind = 'attachment'` row supported by `0023`, with no such values, does **not** block rollback; an `exact` link loses only the redundant column on downgrade. `0023.downgrade()` refuses if any receipt has `status = 'imported'`, a v1d-added `source_kind` (`paste`/`photo`/`email`/`attachment`), `document_kind != 'receipt'`, or non-NULL `import_expires_at`; it also refuses while any `budget_gmail_messages` rows or `active` Gmail grants exist. The operator must revoke Gmail and explicitly delete remaining v1d-only receipts first. Disabled/revoked sync-state rows are non-accounting state and may be dropped with `budget_gmail_state`. After a permitted preflight, `0024` drops its columns/constraints and `0023` drops indexes → `budget_gmail_messages` → `budget_gmail_state` → restores the `0020` schema, using the FK-off/check/restore protocol above.

### 13.7 Additive API contract and OpenAPI versioning

- **OpenAPI versioning strictly after `0.31.0`/`0.32.0`:** `master` (`7f94dce`) is at `0.30.0`, with `0.31.0` and `0.32.0` reserved for `v1a` (`PR #238`) and `P3` household (`hestia/api_contract.py:80-82`). Each `v1d` phase takes an additive minor bump strictly after `0.32.0` (targeting **`0.33.0` (`v1d-a`)**, **`0.34.0` (`v1d-b`)**, **`0.35.0` (`v1d-c`)**, verified with `python -m hestia.api_contract` on rebase).
- **Non-breaking contract & role guards:** Every new `/api/budget/receipts/*` route uses a fixed static path, one HTTP method, explicit `_ADMIN` in `web._ROUTE_MIN_ROLE` and `"admin"` in `api_contract._PATH_ROLES`, `allow_head=False` on `GET`, `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, and `_principal` + `_browser` before reading request bodies. Existing `/api/budget/*`, `/api/budget/analytics/*`, and `/api/budget/household/*` contracts stay non-breaking under `_READ` tolerant-reader semantics (`hestia/api_contract.py:38-43`). Disabled feature flags return `404` (`budget_receipts_gmail_disabled` / `budget_receipts_proof_disabled` / `budget_receipts_enrichment_disabled`).
- **Closed-enum compatibility:** Every pre-v1d route that serializes a receipt accepts an optional `X-Hestia-Contract-Version` header, registered on the corresponding OpenAPI operations; `0.33.0` or later opts into v1d enum values, while an absent or earlier version receives legacy-safe mappings. Map `status = 'imported'` → `'draft'`; map new `source_kind` values `'paste'`/`'email'` → `'live_text'` and `'photo'`/`'attachment'` → `'local_ocr'`. Reject a malformed/unsupported version with `400` before reading the body. Native v1d values therefore never reach legacy closed-enum readers, while regenerated `0.33.0+` clients send the header and receive the native values. New v1d-only routes have no legacy caller and always use their native schemas.
- **Legacy `/parse-text` + `/confirm` compatibility:** An absent or pre-`0.33.0` `X-Hestia-Contract-Version` makes `/parse-text` retain v1a's exact BLIK `d <= 4` candidate generation and issues candidate tokens bound to that matcher; `/confirm` requires a matching legacy token and revalidates `d <= 4`. `0.33.0+` and all v1d suggestion/enrichment paths use `d <= 2`; a token/request matcher-version mismatch returns `400 budget_receipt_candidate_stale` and requires reparsing. Both versions retain exact amount/currency and existing CAS/uniqueness checks.

| Phase | Route & Role | Framing, Bounds & Behavior |
| --- | --- | --- |
| `v1d-a` | `POST /api/budget/receipts/gmail-grant` (`_ADMIN`) | JSON `<= 4 KiB` (`{action: "enable" \| "disable", expected_version}`); `enable` runs `profile` identity check before setting `grant_status='active'`; `disable` increments generation, clears RAM tokens/cursor state, and stops sync locally without Google revocation. |
| `v1d-a` | `POST /api/budget/receipts/gmail-revoke` (`_ADMIN`) | JSON `<= 4 KiB` (`{expected_version}`); stops sync, wipes RAM tokens, clears Gmail FKs before deleting pending imports/tombstones, and purges state inside `BEGIN IMMEDIATE`; never calls Google's revoke endpoint. |
| `v1d-a` | `POST /api/budget/receipts/gmail-sync` (`_ADMIN`) | JSON `<= 4 KiB` (`{force_full_resync?: bool}`); requires `grant_status='active'` + `60 s` cooldown + `flock`; returns `{ok, sync_phase, imported_count, skipped_count, has_more, attention, next_sync_at}`. |
| `v1d-a` | `GET /api/budget/receipts/gmail-status` (`_ADMIN`) | `allow_head=False`, no query params; returns `{enabled, configured, grant_status, grant_generation, version, sync_phase, attention, last_sync_at, next_sync_at, backoff_until, failures, last_http_status, last_error, pending_imported_count}` (zero emails/tokens). The returned `version` is the value clients pass as `expected_version` to `gmail-grant` and `gmail-revoke`. |
| `v1d-b` | `POST /api/budget/receipts/attach` (`_ADMIN`) | Multipart `<= 6 MiB` (`Content-Length` required, no chunked, file `<= 5 MiB`); routes images to `v1b` WebP and `%PDF-` to sandboxed worker; creates `48h` draft or attaches to existing draft. |
| `v1d-b` | `GET /api/budget/receipts/attachment` (`_ADMIN`) | `?receipt_id=<uuid>`, `allow_head=False`; streams PDF via `dir_fd` + `O_NOFOLLOW` with `Content-Disposition: attachment`, `nosniff`, `CSP: default-src 'none'; sandbox`; `404` if absent/foreign. |
| `v1d-b` | `GET /api/budget/receipts/missing-proof` (`_ADMIN`) | Optional `?period=YYYY-MM\|YYYY&currency=AAA`; omitted period means the current `Europe/Warsaw` month. `<= 4 MiB` bounded UTF-8 JSON (`analytics_api.py:94-105`); returns `{period, start, end, evaluation_timezone, thresholds, missing_count, needs_review_count, total_flagged_count, items, items_cap=100, truncated}` with the deterministic ordering and full-set counts in §13.5. |
| `v1d-b` | `GET /api/budget/household/transactions`, `/quarantine` (`_VIEWER` + `X-Budget-Grant`) | Additive non-PII fields only when proof + P3 enabled: `proof_status` (`not_required\|attached\|missing\|needs_review`) on transactions; `missing_proof_count` / `needs_review_proof_count` on quarantine. |
| `v1d-c` | `GET /api/budget/receipts/enrichment-suggestions` (`_ADMIN`) | Optional `?period=YYYY-MM&currency=AAA`; `<= 4 MiB` bounded JSON returning opaque debits, matched Gmail confirmation candidates, and `suggested_category_id` (any non-archived `0020` tree node) for explicit owner confirmation. |

**Additive receipt DTO fields:** `BudgetReceiptConfirmRequest.link.link_basis: Literal["exact", "fx_or_net"] = "exact"` (`lines=[]` accepted when `source_kind IN ("email", "attachment")`), `BudgetReceiptListRequest.status` expanded with `"imported"` + optional `document_kind`, `BudgetReceiptSummary`/`Detail` adding `document_kind`, `link_basis`, `attachment_available`, `attachment_expires_at`, `import_expires_at`, and `BudgetReceiptCandidate` adding `match_tier`, `source_alignment`, `auto_suggest`, `requires_manual_confirm`, `ambiguous_top_match`.

### 13.8 Retention, log hygiene, and privacy policy gates

- **Zero raw email bodies & minimal provenance:** Raw RFC 5322 bytes, HTML bodies, MIME trees, Gmail PDFs, and email addresses exist **only** in ephemeral memory / sandboxed worker buffers during parsing (`raw_ocr_text = NULL`, `image_relpath = NULL` on Gmail imports). Only parsed structured receipt fields and the minimal `budget_gmail_messages` provenance row (`gmail_id_hmac`, optional `rfc_message_id_hmac`, `internal_date`, `trust_path`, `outcome`, `receipt_id`, `created_at`) persist in SQLite.
- **Retention lifetimes:**
  - Interactive drafts (`status = 'draft'`): **48 hours** (deletes lines, receipt, and `/data` WebP/PDF).
  - Unconfirmed Gmail imports (`status = 'imported'`): **30 days** (`import_expires_at = created_at + 30d`; sweep converts `budget_gmail_messages` row to `receipt_id = NULL, outcome = 'deleted'`).
  - Manual PDF/photo artifacts (`raw_ocr_text`, `image_relpath`, `attachment_relpath`): **30 days after confirmation** (`confirmed_at + 30d`), matching `v1a`/`v1b` **Q2** & **D1**.
  - Dedupe provenance tombstones (`budget_gmail_messages`): **400 days** (`internal_date < now - 400d`, exceeding the `365d` backfill window) during normal operation, or **purged immediately** on `POST /gmail-revoke`.
- **Zero amounts or PII in logs & audits:** Python logging, error JSON, `/api/audit`, SSE, and `budget_analytics_audit` record **only** fixed safe status/error codes, exception class names, `receipt_id` UUIDs, `grant_generation`, counts, and enums — **never** amounts, currencies, email addresses, Gmail message IDs, subjects, sender/shop names, NIPs, PANs, PDF filenames, or line items.
- **Privacy policy gates (`ui/public/privacy-policy.html`):** Commit `2ce999f` (`#237`) added baseline `#gmail` copy (`ui/public/privacy-policy.html:282-295`). Before enabling each phase:
  - **`v1d-a` gate:** Ensure `ui/public/privacy-policy.html` reflects the separate revocable Hestia Gmail opt-in/state purge, manual Google revocation's Calendar impact, mailbox identity check, spouse-forwarded mail unwrapping, and `400d` HMAC provenance / `30d` unconfirmed import retention. Initial and incremental sync must apply the same disclosed label/sender receipt-only scope, with no `messages.get` metadata or raw fetch for out-of-scope IDs; the current policy's fetch-scope wording therefore remains unchanged.
  - **`v1d-b` gate:** Update `ui/public/privacy-policy.html` (`#receipts` / household disclosures) to document manual PDF text extraction + 30-day PDF retention and the spouse household view seeing non-PII `proof_status` indicators (`missing`/`needs_review`/`attached`) on qualifying debits.
  - **`v1d-c` gate:** Update `ui/public/privacy-policy.html` to disclose payment-confirmation email matching and category suggestions for opaque debits.

### 13.9 Synthetic verification plan and effort estimate

Continuing `§10` (`#1–#17`), every `v1d` PR must pass Python statement+branch coverage, UI `typecheck`/`lint`/`test`/`build`, `test_every_registered_route_is_public_or_classified` (`tests/test_web.py:1960`), and OpenAPI drift checks (`tests/test_api_contract.py:98`) using synthetic fixtures only:

18. **`test_gmail_separate_grant_opt_in_disable_and_revoke_purge`:** Use barriers to attempt revoke between every pre-send fence and Gmail/token dispatch; the shared per-owner `RLock` makes dispatch and grant commit mutually exclusive, waits for in-flight responses up to 35 s, and times out without changing state. Changed-generation Gmail/refresh responses are never parsed, cached, or committed, and stale chunks roll back. Retain coverage for local-only revocation, FK-safe deletion, generation-keyed tokens, Calendar warning, and `gmail-status.version` CAS.
19. **`test_gmail_profile_identity_check_refuses_mismatch`:** Verifies `GET /users/me/profile` vs. `config.owner_mailbox` on opt-in and every sync; mismatch returns `403`, clears RAM tokens, latches `attention = 'identity_mismatch'`, halts polling, and emits zero email addresses in DB/logs/HTTP errors.
20. **`test_gmail_sender_trust_and_spouse_only_forwarding`:** Accepts the first/topmost `mx.google.com` AR regardless of `Received` placement and rejects other/lower forged fields. Covers AR `header.d` plus passes lacking it: `header.i` matches explicit signature `i=` or the RFC 6376 default `"@" + d=` when omitted; together with `header.b` prefix/selector evidence, all supplied criteria must identify one actual signature whose real `d=` aligns with `From`. No/ambiguous matches and invalid/missing `d=` fail closed; `header.i` never supplies the domain directly. Retains aligned SPF/DMARC, sender rules, and bounded spouse forwarding.
21. **`test_gmail_quota_429_backoff_resumable_batches_and_404_resync`:** Covers quota/backoff, idempotent replay, and standalone commits. A label/sender change clears both history/scope page tokens, updates the query HMAC, and restarts history from retained `history_id` plus scans at page 1, preserving `pending_history_id` and deduping earlier pages. Scope-token 400 resets only the scan; history-token 400 resets both from the baseline; baseline 404 enters full resync. Tokenless page-1 400 does not loop/advance. Retain checkpoint resume, labelId, cap, and unknown-eligibility cases.
22. **`test_gmail_id_hmac_dedupe_idempotency_and_zero_bodies`:** Verifies `HMAC-SHA256(hmac_secret, gmail_message_id)` pre-fetch dedupe, idempotent replay/race handling inside `BEGIN IMMEDIATE`, `rfc_message_id_hmac` duplicate suppression, `outcome = 'deleted'` retention across resyncs, `raw_ocr_text IS NULL`, zero email addresses/bodies in DB/logs, and `400d` pruning.
23. **`test_sandboxed_html_pdf_worker_isolation_and_bounds`:** Proves the parser socket is required only while Gmail/upload features are enabled and production never uses the fallback. The non-production fallback parses fixed-root fixture files only, refuses live Gmail/upload bytes, and uses `env={}` + `RLIMIT_*`. Both paths kill hung parses at `5 s` and discard late output. Also covers network/JS/remote-content blocks, parser input/output bounds, decompression bombs, and encrypted PDFs.
24. **`test_receipt_matching_windows_fx_and_tie_breakers`:** Verifies v1d BLIK (`±2`d/`0`), bank-first pair classification, no BLIK FX fall-through, and legacy `/parse-text` issuing a 3–4-day exact BLIK candidate token that legacy `/confirm` accepts; v1d confirmation and matcher-version mismatches reject it. Covers Gateway/Card FX, missing-date ties, deterministic ambiguity, transactional link checks, uniqueness, and drift latching.
25. **`test_missing_proof_config_flagging_clearing_and_spouse_view`:** Verifies threshold/exclusion and dynamic flag clearing rules, omitted-period `Europe/Warsaw` month boundaries (including DST), deterministic 100-item ordering/cap with full-set counts, and missing-date ordering. P3 household responses expose only non-PII status/counts for the single grant-authorized owner across all pages, never other owners or unauthorized accounts, while `viewer` remains blocked on owner receipt routes (`403`).
26. **`test_manual_pdf_attachment_retention_and_headers`:** Covers `POST /attach` (`Content-Length`, no chunked, sandboxed PDF worker, `256 MiB`/`20`-draft quotas), `48h` draft / `30d` confirmed purge, and `GET /attachment` `Content-Disposition: attachment` + `sandbox` CSP + `nosniff`.
27. **`test_opaque_debit_enrichment_and_category_tree_suggestions`:** Matches `BLIK`/`P24`/`PayU`/`APPLE.COM/BILL` debits against payment confirmations, suggests `suggested_category_id` targeting both parent and leaf nodes in the `0020` (`parent_id`) category tree, confirms via `POST /receipts/confirm` + reviewed `POST /analytics/annotation`, and leaves `budget_transactions.payload_json` untouched.
28. **`test_v1d_migrations_linear_upgrade_downgrade_and_api_contract`:** With populated child FKs, exercises pre-transaction FK-off rebuild/check/restore; proves v1d-only rows/active grants refuse unsafe downgrade while a Gmail `source_kind='attachment'` row with no `0024` values rolls back cleanly. Also verifies single head, legacy/native enum mappings and exact BLIK windows, `gmail-status.version` CAS, and OpenAPI bumps past `0.32.0`.

| Phase | Scope | Rough effort |
| --- | --- | --- |
| **`v1d-a` — Gmail import + dedupe** | Separate revocable grant (`gmail-grant`/`gmail-revoke`), `profile` identity check, sender trust + spouse-forward unwrap, `historyId`/resumable batches/backoff/quota pacing, `HMAC(gmail_message_id)` dedupe, sandboxed `receipt-parser` HTML/PDF worker, migration `0023`, OpenAPI `0.33.0`, privacy-policy review, tests `#18–#23` & `#28` | **5.5–7 engineering days** |
| **`v1d-b` — Proof for larger expenses** | Per-source windows/FX/tie-breakers, `HESTIA_BUDGET_RECEIPTS_PROOF_CONFIG` + `missing-proof`, spouse P3 `proof_status` + counts, `POST /attach` + `GET /attachment`, `privacy-policy.html` update, migration `0024`, OpenAPI `0.34.0`, tests `#24–#26` & `#28` | **4–5.5 engineering days** |
| **`v1d-c` — Opaque-debit enrichment** | Payment-confirmation templates, opaque debit matcher (`BLIK`/`P24`/`PayU`/`Tpay`/`Autopay`/`APPLE.COM/BILL`), category suggestions across any node in `0020` category tree + reviewed annotation flow, `privacy-policy.html` update, OpenAPI `0.35.0` (zero migration), test `#27` | **2.5–3.5 engineering days** |

**Total (`v1d-a`–`v1d-c`):** ~**12–16 engineering days** (sequenced after `v1a`, `v1b`, `P3`, and category tree `0020`).
