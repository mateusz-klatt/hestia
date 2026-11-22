# Budget Receipts: Research & Design Proposal

Status: **Proposed (design only)** — no code, migrations, or privacy-policy edits are shipped with this document.  
Target stack: Python 3.14 / `aiohttp` (`Dockerfile:10`, `requirements.txt:24`), SQLite + SQLAlchemy 2.0 + Alembic (`hestia/db.py:10-30`, `requirements.txt:25-26`), React 19 + TypeScript (`ui/package.json:29-31`), Docker (`docker-compose.yml:1-46`).

---

## 1. Problem & Hestia Fit

Add an owner-private **Receipts (`Paragony`)** module to Hestia Budget so the household owner can:

1. Capture a Polish retail receipt on mobile (photo or pasted iOS Live Text).
2. Extract the shop (name / NIP), purchase date & time, payment method, `SUMA PLN`, and line items (name, quantity, unit price, discount, PTU letter, line total).
3. Match and manually confirm a link to an existing canonical bank transaction (`budget_transactions`) using exact amount, currency, booking/value date (`±2` calendar days), and merchant text.
4. *(v2)* Normalize product variants (e.g., `"ML.UHT 3,2% 1L"`, `"MLEKO WYPASIONE 3,2% 1000ML"`) to compare **price per canonical unit** across Warsaw shops (e.g., Carrefour vs. Biedronka vs. Lidl) over time.

### Non-negotiable Hestia constraints

Every design choice below inherits the existing budget module boundaries:

- **Owner-private & fail-closed auth:** Budget endpoints require `HESTIA_BUDGET_HTTP_ENABLED=1`, `HESTIA_PERSIST=sqlite`, authoritative SQLite users, and an enabled `admin` principal (`hestia/budget/api.py:53-63`). All routes must be classified `_ADMIN` in both `hestia/web.py:167-206` and `hestia/api_contract.py:2283-2315`.
- **Browser-only mutations & headers:** State-changing POSTs require the `HttpOnly` `hestia_session` cookie, reject any `Authorization` header, and enforce `Origin == HESTIA_BUDGET_PUBLIC_ORIGIN` (`hestia/budget/api.py:73-91`, `hestia/budget/analytics_api.py:109-120`). All budget responses carry `Cache-Control: no-store` and `Referrer-Policy: no-referrer` (`hestia/budget/api.py:38,214-216`).
- **Browser memory only:** No receipt images, line items, or prices in `localStorage`, `sessionStorage`, or URL query strings (`docs/BUDGET.md:247-252`, `ui/src/budget/budget.tsx:1-2`). Any `401`/`403`/`404` clears budget state immediately.
- **Exact decimals (no floats):** Monetary values, quantities, discounts, and unit prices must use validated canonical decimal strings (`hestia/budget/normalize.py:98-115`) and scaled-integer arithmetic (`hestia/budget/analytics_core.py:38-87`, `docs/BUDGET_2D.md:26-29`) — never SQLite `REAL`, Python `float`, or JS `Number`.
- **Zero financial/receipt payloads in logs:** Application logs, error responses, global `/api/audit`, and SSE (`/api/events`) may record only fixed safe error codes or exception class names (`hestia/budget/api.py:208`, `hestia/budget/client.py:77-115`). Private mutations log sanitized metadata only in `budget_analytics_audit` (`hestia/db.py:703-712`, `docs/BUDGET_2D.md:292-295`). Note that Apache/Cloudflare reverse-proxy access logs record request paths and query parameters (`ui/public/privacy-policy.html:226-230`), so receipt/product search strings should travel in `POST` JSON bodies rather than `GET` query parameters.
- **Read-only bank access & manual confirmation:** Linking a receipt never contacts Enable Banking, consumes acquisition quota, or alters canonical `budget_transactions` payloads (`docs/BUDGET_2D.md:180-181`). Like transfer matching (`hestia/budget/transfer_match.py:1-15`), matching only proposes candidates and requires explicit owner confirmation.
- **Request-size boundary in `web.py`:** `make_app` currently sets `client_max_size = MAX_RULE_BODY + 4096` (`69,632` bytes in `hestia/web.py:51,1160-1163`), and `_read_json_body` enforces per-route JSON limits (`hestia/web.py:905-935`). Pasted text fits inside `64 KiB`, whereas photo upload requires raising `client_max_size` on `web.Application` (e.g., to `6 MiB`) while keeping strict per-route limits in `_read_json_body` for all existing JSON endpoints.
- **Privacy policy gate:** Storing receipt photos or adding an opt-in cloud vision processor requires updating `ui/public/privacy-policy.html` before live activation (`docs/BUDGET.md:13-14,660-662`).

---

## 2. Polish Fiscal Receipt (*Paragon Fiskalny*) Structure & Existing Parsers

### 2.1 Anatomy of a Polish thermal receipt

Under the Polish Ministry of Finance regulation on cash registers (*Rozporządzenie Ministra Finansów w sprawie kas rejestrujących*), a paper or electronic fiscal receipt follows a predictable top-to-bottom layout:

1. **Seller header:** Legal entity name + branch address (e.g., `JERONIMO MARTINS POLSKA S.A.` / store number, `CARREFOUR POLSKA SP. Z O.O.`), followed by the mandatory **10-digit seller NIP** (e.g., `NIP 779-10-11-327`, `NIP: 9370008168`, `NIP 7811897358`). A mod-11 NIP checksum (`weights = (6,5,7,2,3,4,5,6,7)`, digit 10 == sum mod 11, rejecting remainder 10) filters out phone numbers and card suffixes with near-zero false positives.
2. **Document marker & metadata:** `PARAGON FISKALNY`, sequential print/receipt number, and timestamp (`YYYY-MM-DD HH:MM` or `DD-MM-YYYY HH:MM`).
3. **Line items:** Product name (up to ~40 chars, heavily abbreviated) + quantity × unit price + line gross total + **PTU/VAT tax letter `A`–`G`** (`A=23%`, `B=8%`, `C=5%`, `D=0%`, `E=zw.`, `F/G=0%` technical):
   - *Single-line style:* `CHLEB BALTON.500G    1 x 4,99 = 4,99 C`
   - *Two-line style (common in Lidl/Biedronka):*
     ```text
     MLEKO WYPASIONE 3,2% 1L
                    2 * 4,49    8,98 C
     Rabat                     -2,00 C
     ```
   - *Weighed produce:* `ZIEMNIAKI JADALNE LUZ  1,420 kg x 3,99  5,67 C`
   - *Deposit return lines (`Kaucja` — Poland's system kaucyjny):* non-VAT bottle/can deposit additions (e.g., `0,50` PET/can, `1,00` reusable glass) and bottle-machine voucher deductions printed near items or totals.
4. **Tax summary & grand total:** `SPRZEDAŻ OPODATKOWANA A..G`, `SUMA PTU`, and **`SUMA PLN`** (or `DO ZAPŁATY`) followed by the gross amount.
5. **Payment & fiscal footer:** `KARTA PŁATNICZA` / `KARTA` / `BLIK` / `GOTÓWKA`, optional masked card PAN (`************1234`), and the fiscal logo `#` + cash register unique ID (e.g., `EAO ...`).

### 2.2 Chain quirks vs. existing open-source tools

- **Centralized chain NIPs vs. franchise NIPs:** National chains print one corporate NIP across branches — verified examples: Biedronka (`7791011327`), Carrefour (`9370008168`), Lidl (`7811897358`). Franchise stores (Żabka, Carrefour Express, Lewiatan) print the *local franchisee's NIP*, so shop identification cannot rely solely on a hardcoded NIP table; it needs both a verified chain-NIP lookup and header keyword rules (`ZABKA`, `CARREFOUR EXPRESS`, etc.) backed by owner-editable shop records.
- **Discounts (`Rabat` / `Opust`):** Biedronka and Lidl frequently print the full shelf price on line `N` and a separate negative `Rabat` / `Promocja` line on `N+1` (or a basket voucher near `SUMA PLN`). Ignoring discount lines breaks both `sum(lines) == SUMA PLN` validation and unit-price accuracy.
- **Existing open-source ecosystem:**
  - `filip-rupniewski/receipt_parsing` ([GitHub](https://github.com/filip-rupniewski/receipt_parsing)): OpenCV + Tesseract pipeline tailored to Swiss Lidl/Denner receipts; illustrates preprocessing + regex structure but does not handle Polish fiscal conventions or multi-line `Rabat` blocks.
  - `KamKubicki/fidl-plus` ([GitHub](https://github.com/KamKubicki/fidl-plus)): Python/Docker scraper and price-history UI for *digital* Lidl Plus HTML/JSON receipts; useful inspiration for price comparison, not applicable to paper photos or other chains.
  - `bhimrazy/receipt-ocr` ([GitHub](https://github.com/bhimrazy/receipt-ocr)) & `Rutikasri/DocuParse_AI` ([GitHub](https://github.com/Rutikasri/DocuParse_AI)): Python wrappers using Tesseract or EasyOCR + LLM/LayoutLMv3; general prototypes rather than Polish fiscal parsers.
  - `RysOCR` (`kacperwikiel/RysOCR` / `anon13370/RysOCR`): Polish LoRA fine-tune for `PaddleOCR-VL` (vision-language model), too heavy for a CPU-only home server.
  - Ministry of Finance **HUB Paragonowy** ([podatki.gov.pl/e-paragony/](https://www.podatki.gov.pl/e-paragony/)): publishes the `eParagonInf.mf.gov.pl_v1-0.json` schema for official e-receipts, though consumer e-paragon retrieval currently runs through the closed mobile app rather than a self-hosted personal export API.

**Conclusion:** There is no maintained drop-in parser for Polish thermal receipts. Hestia should implement a small, deterministic, pure-Python parser (`hestia/budget/receipt_parser.py`) tested against synthetic Biedronka, Lidl, Carrefour, and generic/franchise fixtures, accepting both plain text (Live Text) and geometry-grouped OCR lines (`(text, x0, y0, x1, y1)`).

---

## 3. Input & OCR Options Matrix

Every candidate below was evaluated against official documentation and repositories for **Polish thermal receipt accuracy**, **home-server CPU/RAM footprint**, **Docker deployment**, **licence**, **maintenance**, **line-item extraction**, and **privacy**.

| Option / Path | Polish accuracy on thermal receipts | Home-server CPU & RAM (no GPU) | Docker availability | Licence | Maintenance | Line-item extraction quality | Privacy |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **(c) iOS Live Text (paste text)** | **Medium–High** on sharp photos; Apple Feature Availability lists Polish among >40 Live Text locales, though thermal fonts can still flatten `ł`/`ą`/`ę` (`l`/`t`/`a`/`e`) | **~0 MB RAM, <5 ms CPU** on server (runs on iPhone Neural Engine) | Built into existing Hestia container (zero deps) | Proprietary OS feature on user's iPhone | Active (Apple iOS 15+) | **Medium** — copy-paste sometimes merges/wraps two-column lines (`name` + `price`), requiring line heuristics | **100% local/on-device** until pasted to Hestia |
| **(a1) Tesseract OCR (`pol` `tessdata_best` + Pillow/OpenCV)** | **Low–Medium** out of the box; **Medium** with upscaling, adaptive thresholding, `--psm 4`/`6`, and `tessdata_best`; weak on creases/skew/shadows | **50–150 MB RAM** (keep <512 MB cap); 1–3 s/receipt on CPU | `apt-get install tesseract-ocr tesseract-ocr-pol` (+60–100 MB layer) | **Apache-2.0** | Active (`tesseract-ocr/tesseract`) | **Low–Medium** unless using `--tsv`/`hOCR` word bounding boxes to pair left labels with right prices | **100% local** |
| **(a2) OCRmyPDF (CLI / Docker)** | Same as Tesseract (`-l pol --clean --deskew` via `unpaper`) | **300 MB–1.5 GB peak RAM**; 2–5 s/receipt (image→PDF→OCR→sidecar overhead) | Official multi-arch `jbarlow83/ocrmypdf` | **MPL-2.0** core (optional webservice example is **AGPLv3**) | Active (`ocrmypdf/OCRmyPDF`) | **Low–Medium** — PDF archival tool; `--sidecar` drops layout coordinates | **100% local** |
| **(a3) pdfsandwich** | Same as Tesseract + `unpaper` | ~200–600 MB RAM; slow | Community images only (no official upstream image) | **GPL-2.0** | **Stalled** (last release v0.1.7, Aug 2018) | **Low** — PDF output only | **100% local** |
| **(d1) Stirling-PDF** | Same as Tesseract (`pol.traineddata` mounted at `/usr/share/tessdata`); requires img→PDF step before `/api/v1/misc/ocr-pdf` | **~500 MB idle, 2–4 GB recommended RAM** (Java/Spring JVM + Tesseract + LibreOffice) | Official `stirlingtools/stirling-pdf` | **Open-core** (MIT core + proprietary enterprise modules) | Very active (`Stirling-Tools/Stirling-PDF`) | **Low** — returns searchable PDF/hOCR, heavy wrapper for receipt photos | **100% local** |
| **(d2) Paperless-ngx** | Same as OCRmyPDF / Tesseract (`PAPERLESS_OCR_LANGUAGE=pol+eng`) | **300–800 MB idle, 1.5–2+ GB peak RAM/worker** (Django + Redis + Celery + DB) | Official `ghcr.io/paperless-ngx/paperless-ngx` | **GPL-3.0** | Very active (`paperless-ngx/paperless-ngx`) | **Low** — full DMS archive, not a stateless receipt parser | **100% local** |
| **(d3a) PaddleOCR (`PP-OCRv4`/`v5`)** | **High** text detection (`DBNet`) on crumpled/angled thermal paper; `lang='pl'` / `PP-OCRv5-latin` covers Polish (some diacritic drops in older generic Latin models; `PaddleOCR-VL` fixes more but needs 4–16 GB RAM/VRAM) | **0.5–1.5 GB RAM** (`PP-OCRv4`/`v5` CPU, ~0.3–1 s/receipt); **4–16 GB RAM** for `PaddleOCR-VL` | Official `paddlepaddle/paddle` images are **4–8 GB** (custom slim CPU Dockerfile recommended) | **Apache-2.0** | Very active (`PaddlePaddle/PaddleOCR`) | **High** — returns exact polygon bounding boxes + confidence per text span for geometric row reconstruction | **100% local** |
| **(d3b) RapidOCR (`rapidocr-onnxruntime`)** | **High** detection + **Medium–High** Polish recognition (runs converted `PP-OCRv4`/`v5` weights via ONNX Runtime; must configure `latin`/`pl` dict for diacritics) | **200–500 MB RAM**, ~0.3–1.2 s/receipt on CPU; no PyTorch/PaddlePaddle bloat | Slim custom sidecar (~350–500 MB image on `python:3.11-slim`) | **Apache-2.0** | Active (`RapidAI/RapidOCR`) | **High** — identical bounding-box output to PaddleOCR in ~1/3 the RAM | **100% local** |
| **(d4) docTR (`mindee/doctr`)** | **Medium–High** with `VOCABS["polish"]` whitelist; less tuned for narrow thermal receipts than PP-OCR | **1.5–4 GB RAM** (PyTorch CPU); 3–10+ s/receipt on CPU | Official `ghcr.io/mindee/doctr` | **Apache-2.0** | Active (`mindee/doctr`) | **High** — structured page/block/line/word hierarchy | **100% local** |
| *(Note)* EasyOCR / Surya | EasyOCR (`['pl']`): Medium; Surya: High | EasyOCR: **1–3 GB RAM**; Surya: **4–8+ GB RAM** (GPU-oriented) | Community / custom | EasyOCR: **Apache-2.0**; Surya: **Apache-2.0** code / **OpenRAIL-M** weights (<$5M cap) | Active | High | **100% local** |
| **(b) Cloud AI Vision (Claude API / Gemini Paid API)** | **Very High** — reads crumpled/faded thermal print, Polish diacritics, multi-line `Rabat`, and weighed items reliably | **~0 MB local OCR RAM** (outbound HTTPS JSON only) | No extra container | Proprietary API | Active (Anthropic / Google) | **Very High** — native Structured Outputs (`output_config.format` / `responseJsonSchema`) emit strict schema JSON directly | **Third-party cloud** (explicit opt-in only; see below) |

### Plain-language privacy trade-off for Cloud AI Vision (Path b)

Sending a receipt photo to an external AI API discloses **fine-grained personal consumption data** outside the host: every purchased item (groceries, alcohol, OTC pharmacy items, baby products), exact store branch and timestamp, loyalty card hints, and potentially masked card digits.

- **Anthropic Claude API (Commercial Terms):** Does **not** train models on commercial API inputs/outputs by default; inputs and outputs are retained up to **30 days** for abuse/safety enforcement unless the organization holds a separate Zero Data Retention (ZDR) agreement, and safety-flagged requests may be retained longer ([Anthropic Commercial Terms](https://www.anthropic.com/legal/commercial-terms), [Claude Vision Docs](https://docs.anthropic.com/en/docs/build-with-claude/vision)).
- **Google Gemini API:** Under the **Paid Tier** (Cloud Billing enabled), Google does **not** use prompts, images, or responses to train models, but logs them for **55 days** for abuse monitoring; under the **Unpaid/Free Tier**, Google *may* use inputs/outputs to train models and human reviewers may inspect them (with EEA/UK/CH exceptions) ([Gemini API Terms](https://ai.google.dev/gemini-api/terms), [Structured Output Docs](https://ai.google.dev/gemini-api/docs/structured-output)).

**Policy rule for Hestia:** Cloud Vision MUST be **disabled by default**, require a server opt-in flag (`HESTIA_BUDGET_RECEIPTS_AI_ENABLED=1`) plus an **explicit per-receipt action** in the UI with a clear warning banner, use paid/commercial API keys only (never free-tier keys), strip EXIF/GPS metadata before upload, never include bank transactions or account identifiers in the prompt, and be documented in `ui/public/privacy-policy.html` before enabling.

---

## 4. Recommended v1 Architecture (Pragmatic Minimalism)

### 4.1 High-level pipeline

Ship v1 in three cleanly decoupled capabilities, all gated behind `HESTIA_BUDGET_RECEIPTS_ENABLED=1` (default off, reusing `HESTIA_BUDGET_HTTP_ENABLED=1` and SQLite admin ownership):

```text
  [iOS Live Text / Clipboard] ──(POST /parse, JSON text ≤32 KiB)──────┐
                                                                        ▼
  [Mobile Photo <input>]                                      [Polish Fiscal Parser]
     └─ Browser <canvas>: resize ≤1800px, WebP/JPEG, strip EXIF         │
          ├─ Default: Local OCR sidecar (RapidOCR ONNX, 127.0.0.1) ─────┤
          │           └─ Geometric row reconstructor (y-band + x-sort) ─┘
          └─ Explicit per-receipt opt-in (if AI flag=1):                │
                      Claude / Gemini Paid API (JSON Schema) ───────────┘
                                                                        ▼
                                                       [Candidate Transaction Matcher]
                                                       (BOOK, exact amount+currency, ±2d)
                                                                        ▼
                                                       [Owner Review & Manual Confirm]
                                                       (POST /confirm, CAS + fingerprint)
```

### 4.2 Why this OCR choice wins for v1

1. **Zero-dependency Paste path works immediately:** Pasting iOS Live Text into Hestia requires **zero** new Python packages, **zero** extra RAM, and **zero** Docker image changes — ideal for quick deployment and testing the parser + transaction matching end-to-end first.
2. **Optional `receipt-ocr` sidecar using RapidOCR (`rapidocr-onnxruntime`):**
   - Rejects **Stirling-PDF** (2–4 GB JVM + Tesseract-only + PDF-only `/api/v1/misc/ocr-pdf`), **Paperless-ngx** (full DMS stack + GPL-3.0 + 1.5 GB+ worker spikes), **OCRmyPDF / pdfsandwich** (PDF archival wrappers around Tesseract), and **docTR** (1.5–4 GB PyTorch).
   - Keeps Hestia's main image (`Dockerfile:10-39`, `requirements.txt:22-29`) slim and untouched.
   - Runs under an optional Docker Compose `profiles: ["receipts-ocr"]`, bound strictly to `127.0.0.1:8928` (matching Hestia's `network_mode: host` in `docker-compose.yml:10`), capped at e.g. `mem_limit: 768m`.
   - Returns **bounding-box spans** (`text`, `box`, `score`), allowing Hestia to group words on the same horizontal row (`|y_mid_a - y_mid_b| < 0.5 * median_line_height`), sort left-to-right, and accurately separate product descriptions from right-aligned prices and PTU letters even on tilted photos.
   - *Lightweight fallback:* If the owner prefers zero sidecar containers at the cost of lower accuracy on crumpled receipts, `tesseract-ocr` + `tesseract-ocr-pol` (`tessdata_best`) with `--psm 4` TSV output can sit behind the exact same internal `OcrProvider` Python interface.

### 4.3 Polish parser & sanity checks

`hestia/budget/receipt_parser.py` (pure Python, zero network/DB):

- Normalizes Unicode (`NFC`) and prepares a diacritic-folded lookup string (`ą→a, ć→c, ę→e, ł→l, ń→n, ó→o, ś→s, ź→z, ż→z`) so degraded Live Text or OCR diacritics still match shop keywords and `SUMA PLN` / `KARTA` / `RABAT`.
- Extracts **NIP** (`10` digits validated by mod-11 checksum), **shop name**, **date/time** (ISO normalized `YYYY-MM-DD`), **payment method** (`card` / `blik` / `cash` / `unknown`), **`SUMA PLN`**, **PTU breakdown**, and **line items**.
- Links adjacent `Rabat` / `Opust` lines to the preceding item (`discount_amount`) and marks `Kaucja` lines (`line_kind = 'deposit'`) so they never corrupt product unit prices.
- Computes an arithmetic check (`sum(line_net_totals) + deposits + unattributed_discounts == receipt_total`) and surfaces any mismatch (`math_delta`) directly on the Review screen — never silently inventing or fudging numbers.

### 4.4 Transaction matching (v1)

Following `hestia/budget/transfer_match.py:16-237`:

- Candidate pool: owner's **selected** accounts (`BudgetAccount.enabled == True`), canonical `BOOK` entries (`BudgetTransaction.uncertain == False`), matching currency (default `PLN`) and direction (`DBIT` for purchases, `CRDT` for return receipts).
- Hard filter:
  1. `canonical_money(tx.amount) == canonical_money(receipt.total_amount)` (exact decimal match; no silent tolerance).
  2. `abs(effective_date(tx) - receipt.receipt_date) <= 2` calendar days (`booking_date` fallback `value_date` via `analytics_core.effective_date`), covering standard 1–2 day Polish card settlement lag.
  3. Exclude transactions already linked to another confirmed receipt (1:1 primary link in v1; split payments stay manual).
- Ranking (for suggestion display only, never auto-linking):
  - Combine receipt shop name / resolved chain (`BIEDRONKA`, `CARREFOUR`, `LIDL`) with normalized `" ".join(payload["remittance_information"])` + `BudgetTransactionMetadata.creditor_name` (e.g., `"JMP S.A. BIEDRONKA 459"` scores strong token match on `BIEDRONKA` + NIP-mapped alias `JMP`).
  - Rank `same_day` > `±1 day` > `±2 days`.
- **Confirmation & drift protection:**
  - If `payment_method == "cash"`, default suggestion is `"Cash / Do not link"`.
  - If exactly one candidate passes hard filters with a merchant match, pre-select it for confirmation; if multiple candidates match (e.g., two identical `49.99` purchases on the same day), show all candidates and require manual choice.
  - Confirmation stores `linked_account_id`, `linked_entry_reference`, and `linked_ledger_fingerprint = financial_fingerprint(tx.payload)` (`hestia/budget/analytics_core.py:115-125`). If a later bank refresh changes the underlying transaction's financial fingerprint, the receipt link latches `needs_review` instead of silently staying valid.

### 4.5 Initial v1 API surface (draft)

All routes under `/api/budget/receipts/...`, gated identically to `hestia/budget/analytics_api.py:97-130`:

- `POST /api/budget/receipts/parse`: accepts `{text, source_kind}` (JSON ≤ 32 KiB) **or** `multipart/form-data` image (≤ 5 MiB) + optional `use_cloud_ai: bool` (requires `HESTIA_BUDGET_RECEIPTS_AI_ENABLED=1` and explicit UI consent flag); creates a short-lived `draft` receipt, runs parser + candidate matcher, and returns `{receipt_id, draft, candidates, version}`.
- `POST /api/budget/receipts/confirm`: browser-only JSON mutation (`{receipt_id, expected_version, shop, receipt_date, total_amount, currency, payment_method, lines, link: {account_id, entry_reference} | null}`); validates exact decimals and arithmetic, locks the transaction link (if chosen) inside `BEGIN IMMEDIATE`, and appends a sanitized row to `budget_analytics_audit`.
- `GET /api/budget/receipts`: paged owner receipts (keyset cursor, optional `month=YYYY-MM` or `linked=true|false` filter — no item text in URL query!).
- `GET /api/budget/receipts/{receipt_id}` & `GET /api/budget/receipts/{receipt_id}/image`: owner-scoped detail and private image stream (`no-store`, `nosniff`, `CSP: default-src 'none'`).
- `POST /api/budget/receipts/delete`: browser-only mutation (`{receipt_id, expected_version}`) to delete a draft/receipt and unlink its stored image file.

---

## 5. Rough v2 & Proposed Data Model

### 5.1 Relational schema sketch (Alembic migration — requires owner review)

> **Migration note:** Adding these tables requires a new Alembic migration (`0020` for v1 receipt/shop/line tables, `0021` for v2 normalized products & aliases) reviewed before merge, matching `hestia/db.py` conventions (restrictive `ForeignKeyConstraint` without `CASCADE`, explicit `CheckConstraint`s, string decimals, and strict `owner` scoping).

```sql
-- 1. shop (table: budget_shops) — v1 core + v2 chain grouping
CREATE TABLE budget_shops (
    id              TEXT PRIMARY KEY,              -- opaque UUID
    owner           TEXT NOT NULL REFERENCES users(username),
    name            TEXT NOT NULL,                 -- display name, e.g. "Biedronka #459 (Mokotów)"
    chain_key       TEXT NOT NULL,                 -- canonical chain slug, e.g. "biedronka", "carrefour", "lidl"
    nip             TEXT NULL,                     -- 10 ASCII digits (validated mod-11) when known
    merchant_tokens TEXT NOT NULL DEFAULT '[]',    -- JSON array of bank remittance tokens, e.g. ["JMP S.A.", "BIEDRONKA"]
    version         INTEGER NOT NULL CHECK (version >= 1),
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    UNIQUE (id, owner)
);
CREATE INDEX ix_budget_shops_owner_chain ON budget_shops(owner, chain_key);

-- 2. normalized_product (table: budget_normalized_products) — v2
CREATE TABLE budget_normalized_products (
    id              TEXT PRIMARY KEY,              -- opaque UUID
    owner           TEXT NOT NULL REFERENCES users(username),
    canonical_name  TEXT NOT NULL,                 -- e.g. "Mleko 3,2%"
    name_key        TEXT NOT NULL,                 -- diacritic-folded lowercase key for uniqueness/search
    base_unit       TEXT NOT NULL CHECK (base_unit IN ('kg', 'l', 'pcs')),
    category_hint   TEXT NULL,                     -- optional link/tag (e.g. "Nabiał", "Pieczywo", "Warzywa")
    version         INTEGER NOT NULL CHECK (version >= 1),
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    UNIQUE (id, owner),
    UNIQUE (owner, name_key, base_unit)
);

-- 3. receipt (table: budget_receipts) — v1
CREATE TABLE budget_receipts (
    id                    TEXT PRIMARY KEY,        -- opaque UUID
    owner                 TEXT NOT NULL REFERENCES users(username),
    shop_id               TEXT NULL,
    raw_shop_name         TEXT NULL,
    raw_nip               TEXT NULL,
    receipt_date          TEXT NOT NULL CHECK (length(receipt_date) = 10 AND receipt_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    receipt_time          TEXT NULL,               -- optional "HH:MM"
    total_amount          TEXT NOT NULL CHECK (length(total_amount) BETWEEN 1 AND 80),
    currency              TEXT NOT NULL CHECK (length(currency) = 3 AND currency GLOB '[A-Z][A-Z][A-Z]'),
    payment_method        TEXT NOT NULL CHECK (payment_method IN ('card', 'blik', 'cash', 'mixed', 'unknown')),
    status                TEXT NOT NULL CHECK (status IN ('draft', 'confirmed', 'needs_review')),
    source_kind           TEXT NOT NULL CHECK (source_kind IN ('live_text', 'local_ocr', 'cloud_ai')),
    image_relpath         TEXT NULL,               -- opaque "<owner_sha256_prefix>/<receipt_id>.webp" under HESTIA_BUDGET_RECEIPTS_DIR
    image_expires_at      REAL NULL,               -- scheduled photo purge timestamp (metadata rows stay)
    raw_ocr_text          TEXT NULL,               -- bounded raw input text for re-parse; never logged
    linked_account_id     TEXT NULL,
    linked_entry_ref      TEXT NULL,
    linked_fingerprint    TEXT NULL,               -- financial_fingerprint(tx) at confirmation time
    version               INTEGER NOT NULL CHECK (version >= 1),
    created_at            REAL NOT NULL,
    updated_at            REAL NOT NULL,
    FOREIGN KEY (shop_id, owner) REFERENCES budget_shops(id, owner),
    FOREIGN KEY (linked_account_id, linked_entry_ref)
        REFERENCES budget_transactions(account_id, entry_reference),
    UNIQUE (id, owner)
);
CREATE INDEX ix_budget_receipts_owner_date ON budget_receipts(owner, receipt_date DESC, id);
CREATE UNIQUE INDEX uq_budget_receipts_tx_link
    ON budget_receipts(linked_account_id, linked_entry_ref)
    WHERE linked_account_id IS NOT NULL AND linked_entry_ref IS NOT NULL;

-- 4. receipt_line (table: budget_receipt_lines) — v1 core + v2 product/unit columns
CREATE TABLE budget_receipt_lines (
    receipt_id            TEXT NOT NULL,
    line_no               INTEGER NOT NULL CHECK (line_no >= 1),
    owner                 TEXT NOT NULL REFERENCES users(username),
    line_kind             TEXT NOT NULL CHECK (line_kind IN ('item', 'deposit', 'basket_discount', 'other')),
    raw_name              TEXT NOT NULL,           -- verbatim receipt label, e.g. "ML.UHT 3,2% 1L"
    raw_name_key          TEXT NOT NULL,           -- folded search key
    ptu_code              TEXT NULL CHECK (ptu_code IS NULL OR ptu_code IN ('A','B','C','D','E','F','G')),
    quantity              TEXT NOT NULL,           -- exact decimal string, e.g. "1.420" or "2"
    raw_unit              TEXT NULL,               -- e.g. "kg", "szt", "l", "g", "ml"
    unit_price_raw        TEXT NULL,               -- printed price per receipt unit
    gross_line_total      TEXT NOT NULL,           -- pre-discount exact decimal string
    discount_amount       TEXT NOT NULL DEFAULT '0', -- positive decimal string subtracted from gross
    net_line_total        TEXT NOT NULL,           -- effective paid decimal string (= gross - discount)
    -- v2 normalization & unit-price fields (nullable until mapped):
    normalized_product_id TEXT NULL,
    base_quantity         TEXT NULL,               -- e.g. "1" (for 1000ml -> 1 l) or "0.5" (for 500g -> 0.5 kg)
    base_unit             TEXT NULL CHECK (base_unit IS NULL OR base_unit IN ('kg', 'l', 'pcs')),
    unit_price_base       TEXT NULL,               -- exact decimal string: net_line_total / base_quantity (PLN per base_unit)
    mapping_source        TEXT NULL CHECK (mapping_source IS NULL OR mapping_source IN ('alias', 'manual', 'suggested')),
    PRIMARY KEY (receipt_id, line_no),
    FOREIGN KEY (receipt_id, owner) REFERENCES budget_receipts(id, owner),
    FOREIGN KEY (normalized_product_id, owner) REFERENCES budget_normalized_products(id, owner)
);
CREATE INDEX ix_budget_receipt_lines_product ON budget_receipt_lines(owner, normalized_product_id);

-- 5. Remembered alias map (table: budget_product_aliases) — v2
CREATE TABLE budget_product_aliases (
    owner                 TEXT NOT NULL REFERENCES users(username),
    chain_key             TEXT NOT NULL,           -- e.g. "biedronka" or "*" for shop-agnostic
    raw_name_key          TEXT NOT NULL,           -- normalized line name key without promo suffixes
    normalized_product_id TEXT NOT NULL,
    pack_quantity         TEXT NULL,               -- remembered pack size when omitted on short labels
    pack_unit             TEXT NULL CHECK (pack_unit IS NULL OR pack_unit IN ('kg', 'l', 'pcs')),
    created_at            REAL NOT NULL,
    PRIMARY KEY (owner, chain_key, raw_name_key),
    FOREIGN KEY (normalized_product_id, owner) REFERENCES budget_normalized_products(id, owner)
);
```

### 5.2 Photo storage, retention, size limits & log hygiene

- **Storage location (never SQLite BLOBs):** Store images on the persistent `/data` volume (`docker-compose.yml:44`) under `HESTIA_BUDGET_RECEIPTS_DIR=/data/budget-receipts`, following `hestia/budget/consent_files.py:72-100`: directory mode `0700`, files `0600`, `O_NOFOLLOW`, `st_uid == os.geteuid()`, atomic temp-file + `fsync` + `os.replace`. Keeping binary photos out of `hestia.db` prevents SQLite WAL bloat and keeps DB backups fast. Include the folder alongside `hestia.db` and `budget-consent` in backups (`docs/BUDGET_2B.md:475-478`), and handle missing image files gracefully (`image_available: false`).
- **Size limits & sanitization:**
  - Client pre-compresses photos in browser `<canvas>` (`<= 1800 px` long edge, WebP/JPEG quality ~0.82), dropping EXIF/GPS tags before upload.
  - Server enforces a **5 MiB** decoded image cap (`6 MiB` multipart limit), verifies magic bytes (`JPEG`/`PNG`/`WebP`), caps pixel dimensions (`Image.MAX_IMAGE_PIXELS` equivalent, e.g., `6000 × 12000`) against decompression bombs, and re-encodes via Pillow to strip any remaining EXIF/ICC metadata and discard original phone filenames (`IMG_1234.HEIC`).
  - Pasted Live Text capped at **32 KiB** UTF-8 / **500 lines**.
- **Retention:**
  - `draft` receipts + their temp images auto-purge after **48 hours** if never confirmed.
  - Confirmed receipt images obey `HESTIA_BUDGET_RECEIPTS_IMAGE_RETENTION`: default **`30d_after_confirm`** (purges image file after 30 days while retaining structured SQLite rows permanently until deleted by owner; options: `discard_after_confirm`, `30d_after_confirm`, `keep`).
- **Never in logs:**
  - Photo bytes, base64 payloads, upload filenames, raw OCR text, shop names, NIPs, product descriptions, and totals **must never** enter Python `logging`, exception messages, HTTP error JSON, `/api/audit`, or `/api/events` SSE.
  - Error JSON returns fixed codes only (e.g. `budget_receipt_too_large`, `budget_receipt_invalid_image`, `budget_receipt_ocr_unavailable`).
  - Cloud Vision calls log only timing/status/error class, never request/response payloads.

---

## 6. Product Normalization & Price-per-Unit Comparison (v2)

### 6.1 Normalizing Polish receipt variants

Polish supermarket labels compress brand, fat/type, and pack size into 20–35 uppercase ASCII/Polish chars. Examples for one staple:

| Shop | Raw receipt label | Printed qty × price | Discount line | Base quantity | Effective net | Canonical product & unit price |
| --- | --- | --- | --- | --- | --- | --- |
| Biedronka | `ML.UHT 3,2% 1L MLEKOVITA` | `2 * 4,49 = 8,98` | `Rabat -2,00` | `2 l` | `6.98 PLN` | `Mleko UHT 3,2%` → **`3.49 PLN/l`** |
| Carrefour | `MLEKO WYPASIONE 3,2% 1000ML` | `1 * 4,29 = 4,29` | — | `1 l` | `4.29 PLN` | `Mleko 3,2%` → **`4.29 PLN/l`** |
| Lidl | `CHLEB BALTON. 500G` | `1 * 4,79 = 4,79` | — | `0.5 kg` | `4.79 PLN` | `Chleb baltonowski` → **`9.58 PLN/kg`** |
| Biedronka | `ZIEMNIAKI JAD.LUZ` | `1,420 kg x 3,99` | — | `1.42 kg` | `5.67 PLN` | `Ziemniaki jadalne` → **`3.99 PLN/kg`** |

**Deterministic normalization pipeline:**

1. **Cleanup & folding:** `NFC` normalize, strip trailing PTU letter (`A`–`G`), fold Polish diacritics into `raw_name_key` (`MŁEKO` OCR glitch → `mleko`), expand common retail abbreviations (`ML.` → `MLEKO`, `BALTON.` → `BALTONOWSKI`, `JAD.` → `JADALNE`, `ŚW.`/`SW.` → `SWIEZE`).
2. **Pack-size & unit extraction (exact `Decimal`):**
   - Multipack regex first: `(\d+)\s*[x*]\s*(\d+(?:[.,]\d+)?)\s*(kg|g|dag|l|ml|szt|pak|rol)\b`.
   - Single pack regex second: `(\d+(?:[.,]\d+)?)\s*(kg|g|dag|l|ml|szt|op)\b`.
   - When the receipt line already carries a weight/volume quantity (`1,420 kg`), multiply `line_qty * pack_size` (or use `line_qty` directly when `raw_unit == 'kg'`).
   - Convert strictly with exact factors (`g → kg ÷ 1000`, `dag → kg ÷ 100`, `ml → l ÷ 1000`, `szt/op → pcs`).
3. **Effective net line price:** Allocate any linked item `Rabat` before dividing (`net_line_total / base_quantity`), keeping exact scaled integers (`grosze` and `milligrams`/`milliliters`) on the server and formatting rounded canonical decimal strings (e.g., 2 decimal places for `PLN/l` or `PLN/kg`, 4 for small items) only at serialization.
4. **Canonical product resolution:**
   - First check `budget_product_aliases` for `(owner, chain_key, raw_name_key)` (exact deterministic hit once the user confirms an item the first time).
   - Fallback: token-set Jaccard / `difflib` (or optional `rapidfuzz`) over existing `budget_normalized_products` matching compatible `base_unit` (`kg`/`l`/`pcs`) and key attributes (e.g., `%` fat numbers like `3,2%` vs `2%`). Unknown items stay `unmapped` until the user clicks a suggested product or creates one inline.

### 6.2 Comparison & chart ideas

All charts render locally in React using lightweight inline SVG (zero third-party charting bundles or external CDNs, matching existing `budget.css` / `progress` meters):

1. **Unit-Price Time-Series Line/Step Chart (per product):** X-axis = date, Y-axis = `PLN / base_unit` (`kg` or `l`), one colored line/marker series per shop chain (`Biedronka`, `Carrefour`, `Lidl`), with distinct hollow vs. solid dots for promotional (`discount > 0`) vs. regular shelf prices.
2. **Cross-Shop Staple Comparison Bars:** Horizontal grouped bar chart for top staple products (`Mleko 3,2%`, `Chleb baltonowski`, `Ziemniaki`, `Masło 82%`) showing **latest price/unit** and **90-day range (min–max whisker)** per shop plus `% delta` against the cheapest shop.
3. **Personal Basket Cost Sparkline:** Tracks total cost of a user-pinned 5–15 item recurring basket over the last 6–12 months using exact server-side decimal sums (`sum_money`) of each product's monthly average unit price.

---

## 7. UI Sketch (Mobile-First)

Integrates as a new `"Receipts"` / `"Paragony"` tab in `ui/src/budget/budget.tsx:29,75`, using `budget.css` card layouts (`@media (max-width: 640px)` single-column rules at `ui/src/budget/budget.css:34-36,85-88`).

### 7.1 Screen A — Capture / Paste (Mobile-first)

```text
+------------------------------------------+
| Budżet  [Konta] [Miesiąc] ... [Paragony] |
+------------------------------------------+
| Dodaj paragon                            |
|                                          |
|  [ 📷 Zrób zdjęcie / Wybierz plik ]      |
|  (Zmniejszane na telefonie przed wysł.)  |
|                                          |
|  lub wklej tekst (iPhone Tekst na żywo): |
|  +------------------------------------+  |
|  | BIEDRONKA ...                      |  |
|  | MLEKO 3,2% 1L   4,49 C             |  |
|  | SUMA PLN 45,99                     |  |
|  +------------------------------------+  |
|  [ 📋 Wklej ze schowka ]                 |
|                                          |
|  [ ] Wyślij zdjęcie do Cloud AI (Claude) |  <-- shown only if AI flag=1;
|      ⚠ Treść paragonu opuści serwer.     |      unchecked by default
|                                          |
|           [ Analizuj paragon ]           |
+------------------------------------------+
```

### 7.2 Screen B — Review, Transaction Match & Confirm

```text
+------------------------------------------+
| Weryfikacja paragonu            [ Anuluj ]|
+------------------------------------------+
| Sklep:   [ Biedronka #459     ] NIP: ... |
| Data:    [ 2026-11-20 ]  Płatność: Karta |
| Suma:    [ 45,99 ] PLN                   |
| ✓ Suma pozycji (45,99) zgodna z paragonem|
+------------------------------------------+
| Dopasowana transakcja bankowa:           |
| (*) 2026-11-21 · JMP S.A. BIEDRONKA 459  |
|     mBank · -45,99 PLN (1 dzień później) |
| ( ) Inna transakcja / Gotówka / Później  |
+------------------------------------------+
| Pozycje (12)              [+ Dodaj linię]|
| +--------------------------------------+ |
| | ML.UHT 3,2% 1L MLEKOVITA       [C]   | |
| | 2 szt × 4,49 · Rabat -2,00  = 6,98   | |
| | Produkt (v2): [Mleko 3,2% · 3,49/l]  | |
| +--------------------------------------+ |
| | ZIEMNIAKI JADALNE LUZ          [C]   | |
| | 1,420 kg × 3,99             = 5,67   | |
| +--------------------------------------+ |
|                                          |
|            [ Potwierdź i zapisz ]        |
+------------------------------------------+
```

*(On viewports `> 860px`, Screen B renders the compressed receipt photo/raw text sticky on the left and the editable form + bank match on the right.)*

### 7.3 Screen C — Shop Price Comparison (v2)

```text
+------------------------------------------+
| Porównanie cen w sklepach                |
| [ Szukaj produktu (np. mleko, chleb)   ] |
| Okres: [90 dni v]   Kategoria: [Wszyst.] |
+------------------------------------------+
| Mleko 3,2% (PLN / 1 l)                   |
|   Biedronka : 3,49 (promo) · śr. 3,89    |
|   Lidl      : 3,79         · śr. 3,95    |
|   Carrefour : 4,29         · śr. 4,19    |
|   → Biedronka tańsza o 18,6% vs Carrefour|
|   [ Wykres historii cen (SVG) ▾ ]        |
+------------------------------------------+
| Chleb baltonowski (PLN / 1 kg)           |
|   Carrefour : 8,40 (1,0 kg)              |
|   Biedronka : 9,58 (500 g)               |
|   [ Wykres historii cen (SVG) ▾ ]        |
+------------------------------------------+
```

---

## 8. Risks & Mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| **Faded/crumpled thermal print or missed diacritics** | Wrong prices, `Rabat` dropped, or mangled names | Geometric row grouping from RapidOCR bounding boxes; mandatory `sum(lines) == SUMA PLN` check highlighting discrepancies on Review; optional per-receipt Cloud Vision escape hatch |
| **Franchise NIPs (Żabka, Carrefour Express)** | Different NIP per branch breaks chain grouping | Store `chain_key` + `nip` separately on `budget_shops`; match via both verified chain NIPs and header regexes + editable shop chain field |
| **Apache/Cloudflare logs capture query strings (`ui/public/privacy-policy.html:228`)** | Product names or shop searches leak into reverse-proxy logs | Use opaque UUIDs in paths; pass search/filter strings in `POST` JSON bodies or filter the already-loaded in-memory product list client-side |
| **iOS Safari `.heic` uploads** | Pillow cannot decode HEIC without `pillow-heif` / `libheif` | Browser `<canvas>` converts `<input type="file" accept="image/*">` to WebP/JPEG client-side before upload; reject unsupported raw formats cleanly (`budget_receipt_invalid_image`) |
| **Large uploads exhaust `aiohttp` body limit or SQLite size** | Uploads fail at `69,632 B` (`hestia/web.py:1161`) or bloat `hestia.db` | Raise `Application(client_max_size=6*1024*1024)` while keeping `_read_json_body` strict limits; store images on `/data/budget-receipts` (mode `0700`/`0600`) with 30-day purge default |
| **Cloud AI data disclosure / free-tier training** | Shopping habits sent to third-party servers | Default off; server flag + per-receipt explicit opt-in warning; paid API keys only; strip EXIF; update `ui/public/privacy-policy.html` first |
| **Unit-price distortion (multipacks, deposits, promos)** | `2x1L` or `Kaucja` skews `PLN/l` comparisons | Separate `line_kind='deposit'`; parse multipacks (`NxM unit`); compute `unit_price_base` from post-discount `net_line_total` using exact `Decimal` division |

---

## 9. Open Questions for Owner (with Recommended Defaults)

1. **Local photo OCR engine for v1b:** Deploy the optional `RapidOCR` (`rapidocr-onnxruntime`) Compose sidecar (~250–500 MB RAM) or embed `tesseract-ocr-pol` directly in Hestia's container (~60–100 MB image)?  
   *Recommended default:* **RapidOCR sidecar behind Compose `profiles: ["receipts-ocr"]`**, keeping the main Hestia image untouched and allowing Paste-only v1a to run with zero extra containers.
2. **Receipt photo retention after confirmation:** Delete immediately (`discard_after_confirm`), keep for a grace window (`30d_after_confirm`), or retain permanently (`keep`)?  
   *Recommended default:* **`30d_after_confirm`** (with an owner UI button to purge earlier or mark individual receipts to keep).
3. **Cloud AI Vision opt-in provider (if enabled):** Support Anthropic Claude Messages API, Google Gemini Paid API, or both behind a single config selector?  
   *Recommended default:* **Optional adapter supporting Gemini Paid (`gemini-2.5-flash`) or Claude Haiku/Sonnet** via an `0400`/`0600` API key file, strictly off by default and manual per-receipt trigger only.
4. **Search privacy vs. reverse proxy logs:** Keep product/receipt search completely client-side over paged/loaded items, or use `POST /api/budget/receipts/search` so search text never hits Apache/Cloudflare `GET` query logs?  
   *Recommended default:* **`POST` search body (or client-side filtering for small v1 lists)** — never `GET ?query=...` for product names.
5. **Line-item category splitting into `budget_annotations`:** Should v1 keep receipt link informational on the single bank transaction, or allow splitting one supermarket receipt across multiple Hestia budget categories (e.g., Food vs. Household Cleaning)?  
   *Recommended default:* **Defer category splitting to v2+**; v1 keeps `budget_annotations` 1:1 per bank transaction so existing `analytics_views.py` projections remain untouched.
6. **Card-linked vs. cash receipts:** Should cash receipts (`GOTÓWKA`) be saved for price tracking in v1 without a bank transaction?  
   *Recommended default:* **Yes** — allow saving unlinked/cash receipts so item-price history covers cash purchases too.
7. **Basket discount allocation:** When a receipt has a whole-basket coupon (e.g., `-10 zł przy zakupach za 100 zł`) rather than an item-specific `Rabat`, allocate proportionally across items or keep as `line_kind='basket_discount'`?  
   *Recommended default:* **Keep as `line_kind='basket_discount'`** so individual item shelf/promo prices remain accurate for shop comparison.

---

## 10. Effort Estimate

| Phase | Scope | Rough effort |
| --- | --- | --- |
| **v1a — Paste + Parser + Match + Review** | Polish fiscal parser (`receipt_parser.py`), transaction candidate matcher, Alembic `0020` (`budget_shops`, `budget_receipts`, `budget_receipt_lines`), `/api/budget/receipts/*` + OpenAPI/TS types, mobile-first Paste + Review UI, synthetic tests + full coverage gate | **4–6 engineering days** |
| **v1b — Photo upload + RapidOCR sidecar** | Client `<canvas>` resize/EXIF strip, `client_max_size` + Pillow validation, private `/data/budget-receipts` store + 30d purge, `receipt-ocr` Compose sidecar (`rapidocr-onnxruntime`) + geometric row grouping, image viewer UI | **3–4 engineering days** |
| **v1c — Optional Cloud Vision opt-in** | Default-off provider client (Claude / Gemini Paid JSON Schema), `0400` secret file config, explicit per-receipt UI toggle + privacy policy update | **1.5–2 engineering days** |
| **v2 — Product normalization & shop charts** | Alembic `0021` (`budget_normalized_products`, `budget_product_aliases`), exact `Decimal` unit extractor + alias/fuzzy matcher, shop comparison view + inline SVG charts | **5–7 engineering days** |

**Total:** ~**8.5–12 days** for full v1 (a+b+c), or **4–6 days** to ship v1a (Live Text paste + bank transaction matching) immediately; **+5–7 days** for v2 price comparison.

---

## 11. Sources

### Repository references
- `Dockerfile:10-39`, `docker-compose.yml:1-46`, `requirements.txt:22-29`, `ui/package.json:10-32`
- `hestia/db.py:10-30,182-748,753-816`
- `hestia/web.py:50-52,167-206,905-935,1157-1167`
- `hestia/api_contract.py:2283-2315`
- `hestia/budget/api.py:38,53-91,208,214-216`
- `hestia/budget/analytics_api.py:97-130`
- `hestia/budget/analytics_core.py:38-87,115-125`
- `hestia/budget/transfer_match.py:1-237`
- `hestia/budget/normalize.py:98-115`
- `hestia/budget/config.py:171-195`
- `hestia/budget/consent_files.py:72-100`
- `hestia/budget/client.py:77-115`
- `docs/BUDGET.md:13-14,23-24,247-252,660-662`
- `docs/BUDGET_2B.md:370-374,475-478`
- `docs/BUDGET_2D.md:26-29,180-182,292-295`
- `ui/src/budget/budget.tsx:1-2,29,75`, `ui/src/budget/budget.css:34-36,85-88`
- `ui/public/privacy-policy.html:161-266`

### Public documentation & external references
- Enable Banking API reference: https://enablebanking.com/docs/api/reference/
- Apple iOS & iPadOS Feature Availability (Live Text languages): https://www.apple.com/ios/feature-availability/
- Polish Ministry of Finance — e-Paragony & HUB Paragonowy: https://www.podatki.gov.pl/e-paragony/
- Tesseract OCR & `tessdata_best` / `tessdata_fast`: https://github.com/tesseract-ocr/tesseract , https://github.com/tesseract-ocr/tessdata_best , https://github.com/tesseract-ocr/tessdata_fast
- OCRmyPDF & documentation: https://github.com/ocrmypdf/OCRmyPDF , https://ocrmypdf.readthedocs.io
- pdfsandwich: https://www.tobias-elze.de/pdfsandwich/ , https://sourceforge.net/projects/pdfsandwich/
- Stirling-PDF: https://github.com/Stirling-Tools/Stirling-PDF
- Paperless-ngx & GPL-3.0 licence: https://github.com/paperless-ngx/paperless-ngx , https://github.com/paperless-ngx/paperless-ngx/blob/main/LICENSE , https://docs.paperless-ngx.com
- PaddleOCR: https://github.com/PaddlePaddle/PaddleOCR
- RapidOCR: https://github.com/RapidAI/RapidOCR
- docTR: https://github.com/mindee/doctr
- EasyOCR & Surya (comparison notes): https://github.com/JaidedAI/EasyOCR , https://github.com/datalab-to/surya
- Anthropic Claude Vision & Commercial Terms: https://docs.anthropic.com/en/docs/build-with-claude/vision , https://www.anthropic.com/legal/commercial-terms
- Google Gemini API Terms & Structured Outputs: https://ai.google.dev/gemini-api/terms , https://ai.google.dev/gemini-api/docs/structured-output
- Open-source receipt parser references: https://github.com/filip-rupniewski/receipt_parsing , https://github.com/KamKubicki/fidl-plus , https://github.com/bhimrazy/receipt-ocr , https://github.com/Rutikasri/DocuParse_AI , https://github.com/YanzhongSu/lidl
