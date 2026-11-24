# Hestia authorization redesign (approved 2026-11-24)

Status: approved after four cross-family review rounds (author Codex gpt-6.2-sol; reviewer Agy gemini-4-hydrogen-xhigh). Owner sign-off on 2026-11-24: start PR 1; QR login is https-only; a code redeem also raises the „Nowe urządzenie” alert.

This file has two parts. Part A is the v4 design. Part B holds the v5 amendments; **where the two overlap, Part B takes precedence.**

---

# Part A — design v4


## Bottom line

Keep authentication separate from authorization, store individual grants in one SQLite table, and declare IOC, Calendar, and Budget permissions in code. In the first rollout, each user has at most one budget target, full-ledger access stays owner-only, and the spouse keeps IOC control, Calendar editing, and the aliased household view until the admin explicitly adds receipt submission. Ship a schema and session bridge first, then cut over authorization, add origin-bearing QR sign-in and receipt exchange, and retire compatibility only after Vesta adoption.

## Changes vs v3

| Review finding | Verdict | v4 resolution |
|---|---|---|
| **Blocker 1.** `budget.receipts.match` crosses catalog scopes when it requires `.view`. | Accepted | Separate storage scope from delegation. Both permissions are owner-scoped. A self-only permission can imply a delegable permission on the same owner. |
| **Blocker 2.** `requires` and the return value of `authorize()` are ambiguous. | Accepted | `requires` is an implication. Effective permissions are the transitive closure of direct grants. `authorize()` returns a Boolean for an explicit scope. `resolve_budget_target()` separately returns the sole owner or `None`. |
| **Blocker 3.** Session phase, CLI resets, and direct verifier calls are underspecified. | Accepted | Use one persisted marker to switch issuance, validation, and generation writes together. Leave JSON mode alone, fence password verification against concurrent resets, and replace all five production `verify_session` call sites as specified in PR 1. [Login](../hestia/web.py:395) · [CLI password write](../hestia/store_sql.py:304) |
| **Major 4.** PR 2 rollback can revive stale roles or bypass ownership. | Accepted | Mirror `users.role` and its grant projection transactionally during compatibility. The bridge honors provisioned ownership in that phase. Once custom grants are enabled, stop legacy role writes and roll back only to a grant-aware image. [Legacy role writes](../hestia/store_sql.py:359) · [Current owner check](../hestia/budget/store.py:118) |
| **Major 5.** Budget’s Origin configuration would break core HTTP LAN and non-Budget deployments. | Accepted | Use optional `HESTIA_PUBLIC_ORIGIN` for core browser mutations, with a direct-request fallback. QR issuance requires that setting to be an explicit HTTPS origin. Existing Budget Origin checks keep their own setting. [Current Budget Origin check](../hestia/budget/api.py:73) |
| **Major 6.** General QR login could depend on Budget’s optional rule key. | Accepted | Derive the login-code HMAC key from `HESTIA_SESSION_SECRET` in an auth-specific HKDF namespace. Existing receipt keys and token HMACs stay in their current namespaces. [Current Budget key derivation](../hestia/budget/device_token.py:168) |
| **Major 7.** Old role checks can blank an authorized owner’s Budget UI. | Accepted | Replace role gating in `main.ts`, `budget.tsx`, and `budget/api.ts` before enabling custom grants. Budget boot and invalidation use live own-ledger capability and actor identity. [App mount](../ui/src/main.ts:183) · [Budget invalidation](../ui/src/budget/budget.tsx:135) |
| **Major 8.** Owner provisioning and **Pełny dostęp** lack a concrete contract. | Accepted | Add an idempotent local `budget-owner` command in PR 1. Presets select only applicable ordinary permissions for the user and target. [Auth CLI](../hestia/auth.py:209) · [SQLite users bootstrap](../hestia/store_sql.py:219) |
| **Prior disputed claims.** Vesta device labels collide by design, and current login demonstrates CSRF. | Reject the original claims as stated | Vesta already includes a per-install suffix. Keep proof-based rotation because labels remain caller-supplied. Current Hestia login requires JSON and issues a Strict cookie, and the reviewed app path does not demonstrate the reported CSRF. Apply purpose separation and Origin protection to the new browser/native boundary. [Vesta device label](https://github.com/mateusz-klatt/vesta/blob/master/Vesta/Receipts/ReceiptPairing.swift:49) · [Login](../hestia/web.py:395) |
| **Owner amendments.** Independent Calendar domain and origin-bearing QR. | Adopted | Calendar gets its own two permissions and legacy backfill. QR carries the configured HTTPS origin, uses the version 4/L renderer, and asks Vesta users to confirm a first or changed origin before contacting it. |

## Permission catalog

| Permission ID | Storage scope | Delegation in v1 | Requires | Device-eligible | Allows |
|---|---|---|---|---|---|
| `ioc.view` | Installation | — | — | No | Discovery, device and room state, room icons, and IOC events. |
| `ioc.control` | Installation | — | `ioc.view` | No | Device commands, scenes, and IR. |
| `ioc.configure` | Installation | — | `ioc.view` | No | Automations, registry, device names, room icons, whole-home settings, and diagnostics. Configuration mutations use a browser session. |
| `calendar.view` | Installation | — | — | No | `GET /api/calendars` and `GET /api/calendars/instances?from&to[&group]` when registered. |
| `calendar.edit` | Installation | — | `calendar.view` | No | `POST /api/calendars/events` and `POST /api/calendars/events/delete`. |
| `system.audit.view` | Installation | — | — | No | Existing shared audit feed. New private grant and budget events remain private. |
| `system.manage_users` | Installation | Protected | — | No | Manage accounts and all ordinary grants from the browser. |
| `budget.view` | Owner | Self only | — | No | Full ledger, balances, analytics, categories, and rules. |
| `budget.edit` | Owner | Self only | `budget.view` | No | Rules, categories, annotations, limits, and other ordinary bookkeeping. |
| `budget.receipts.add` | Owner | Delegable | — | Yes | Parse, finalize, or discard the actor’s own unconfirmed draft. |
| `budget.receipts.view` | Owner | Delegable | — | Yes | List, search, and read receipts and available attachments. |
| `budget.receipts.edit` | Owner | Delegable | `budget.receipts.add`, `budget.receipts.view` | Yes | Edit and delete receipts subject to the link policy. |
| `budget.receipts.match` | Owner | Self only | `budget.receipts.view` | Yes | See bank-match candidates and change links when also authorized to write the receipt. |
| `household.view` | Owner | Delegable | — | No | Existing aliased and redacted household projection. Display under Budget. |

Implement the catalog in `hestia/authz.py`. Each entry declares ID, display domain, storage scope, delegation, `requires`, device eligibility, label, and whether its grant is protected. Validate duplicate and unknown IDs, cycles, cross-domain or cross-storage-scope edges, and a delegable permission requiring a self-only permission. A self-only permission can imply a delegable permission for the **same owner**. Every owner-scoped grant targets a provisioned account.

Use these public functions in that module:

- `effective_permissions(session, actor, *, owner=None) -> frozenset[str]` returns the transitive closure of valid direct rows in that exact scope. `owner=None` means installation. Reject disabled or missing actors, and reject missing, disabled, or unprovisioned budget owners. Ignore invalid stored IDs and invalid self-only rows. A direct `ioc.control` row confers `ioc.view` even if there is no direct view row.
- `authorize(session, actor, permission_id, *, owner=None) -> bool` tests membership in that set. It performs no target inference and no commit. A permission used with the wrong storage scope is denied.
- `resolve_budget_target(session, actor) -> str | None` returns the actor’s own provisioned ledger or the sole owner with a valid effective owner-scoped grant. If those sources produce multiple owners, deny instead of choosing. A caller then checks the operation’s permission on that target. `household.view` alone cannot enroll a receipt device.

The API serves the catalog and separate direct/effective permission sets. The editor auto-checks implied requirements, shows why they are checked, and stores only direct selections. **Pełny dostęp** selects applicable ordinary IDs for the chosen scope once. Hide or disable self-only entries for delegates and exclude the protected admin grant. A new catalog entry does not join earlier presets automatically. This provides the convenience of Snapper’s bundles without stored roles.

Calendar proves the schema is independent of the catalog. Move the two existing Calendar route floors and their OpenAPI entries, and gate Calendar UI separately from IOC. The `feat/calendar-instances` checkout inspected still points to the same commit as `master` and has no registered instances route. Add its `calendar.view` classification and binding test in the change that registers it. If it lands before catalog cutover, retain its viewer floor until PR 2. [Current Calendar routes](../hestia/calendar_sync/api.py:12) · [Legacy route floors](../hestia/web.py:256)

## Ownership and owner-only guard

Add `users.budget_owner`, default false. It records trusted provisioning and is neither a role nor an editable checkbox. Seed it for legacy self-budget-eligible admins, including disabled admins whose access would return on re-enable and admins who have yet to connect a bank. A stale connection row owned by a formerly eligible account does not independently activate ownership. `BudgetConnection.owner` and existing resource predicates continue to scope ledger data. [User model](../hestia/db.py:70) · [Connection model](../hestia/db.py:184) · [Pre-connection setup](../hestia/budget/store.py:118)

The owner-only guard requires an enabled, provisioned owner and `actor == owner`. Check resource ownership too. Bank connection setup and controls, consent and SCA, refresh and jobs, Gmail integration and sync, owner device administration, the private mixed inbox, and future money movement are outside the grantable catalog. Retain the provider callback’s existing state/nonce protocol and the owner-bound checks for background services. Legacy device password login retains its real `BudgetConnection` requirement. Legacy owner pairing-code issuance and redemption remain available before the first connection where they are available today. [Password device eligibility](../hestia/budget/device_token.py:142) · [Consent callback](../hestia/budget/consent_api.py:211)

Use one installation-admin authority for all checkboxes and account recovery. Allow self-edit of ordinary grants. Protect self-removal of administration and keep the last-enabled-admin guard under `BEGIN IMMEDIATE`. Administration alone confers no ledger runtime access. Account recovery remains trusted and can reset another account’s password. [Account administration](../hestia/web.py:744) · [Write-locked guard](../hestia/store_sql.py:328)

Enforce the single-target rule on writes. A user’s receipt and household grants must point to the same owner, and a provisioned owner’s target is self. During rollback compatibility, owner-only access also retains the legacy admin eligibility requirement. When custom grants are enabled, ownership and installation administration become independent.

## Login (password, QR) and device exchange

### Password and browser Origin

`POST /api/login` retains scrypt, dummy verification for ineligible users, and the shared limiter with legacy device password login. Versioned browser and native sessions carry the same identity and generation, with a signed `purpose` controlling which carrier and routes may use the credential. A browser cookie carries `browser`; an `Authorization` bearer carries `native`. Neither carries grants. Password and QR sign-in both succeed independently of product access. The exact wire encoding, activation sequence, and validation sites are in PR 1. [Password verification](../hestia/auth.py:106) · [Shared limiter coverage](../tests/test_budget_device_tokens.py:783)

For core protected browser mutations, require a validated browser session and exactly one `Origin`. When set, compare against `HESTIA_PUBLIC_ORIGIN`. Otherwise use the direct request’s scheme and validated host/port. Ignore forwarded-origin headers in that fallback. Direct HTTP LAN and loopback can continue to use password sign-in and the browser UI. A reverse proxy that terminates TLS or rewrites the authority should set the external public origin explicitly. Preserve auth-off development behavior and Budget’s existing `HESTIA_BUDGET_PUBLIC_ORIGIN` checks. Once versioned issuance is active, sensitive browser operations require a signed browser-purpose session. Legacy generation-zero credentials remain accepted on their ordinary compatibility routes.

### QR sign-in

Add browser `POST /api/auth/qr-code` with `{origin}` from `location.origin`. Require a current browser-purpose session, exactly one matching `Origin` header, and equality of both origins to explicitly configured HTTPS `HESTIA_PUBLIC_ORIGIN`. Use a canonical ASCII origin with no userinfo, path, query, or fragment. Normalize host case and the default HTTPS port, and retain any nondefault port. Never take the QR destination from a forwarded header.

Return `hestia-login:<origin>#<code>`. Preserve the v3 choice of 14 random bytes encoded as 19 unpadded base64url characters. Version 4/L byte mode supports the owner’s 78-byte limit. The 13-byte prefix, separator, and code leave **45 bytes** for the canonical origin. Reject oversize payloads before persisting a code, with a typed error and password sign-in still available. Update `ui/src/budget/qr.ts` to the version 4/L matrix and codeword layout. Keep the old `hestia-pair:<code>` pairing contract. Update the QR fixture to represent 33-column rows without 32-bit truncation. [Current renderer](../ui/src/budget/qr.ts:1) · [Legacy QR issuance](../hestia/budget/device_token.py:423)

Derive a 32-byte key using HKDF-SHA256 with `HESTIA_SESSION_SECRET` and info `hestia/auth/qr-login/v1`. Store only the HMAC of `canonical_origin || NUL || code`, with the issuing user and generation. Use five-minute expiry, one-time consumption under `BEGIN IMMEDIATE`, a per-user outstanding-code cap, and bounded per-source and global redemption throttling. `POST /api/auth/qr-redeem` receives `{origin, code}` in JSON, checks the configured origin, rejects Cookie and Authorization, validates the code and actor generation under the write lock, and returns the password flow’s native session shape without `Set-Cookie`. Keep QR codes and tokens out of logs, URLs sent to the server, and audit payloads. Budget flags and `HESTIA_BUDGET_RULE_KEY_FILE` play no part in native QR login. [Existing Budget key and HMAC code](../hestia/budget/device_token.py:168)

Vesta’s unconfigured/sign-in screen offers **Scan a code from Hestia** as the primary action, with manual password setup available separately. Accept only a valid HTTPS QR origin. Compare canonical scheme, host, and effective port with the saved origin. First use or a changed origin, including a changed nondefault port, requires confirmation before any network request. A saved-origin match auto-connects. Redeem using an isolated client with cookies, saved credentials, and redirects disabled. Commit the origin and origin-bound session only after successful redemption, guarded by the existing session-generation mechanism. Cancel and failure leave the old connection intact. The current `saveAndConnect` sequence saves the new URL before logout, so the QR path must stage redemption separately to avoid sending an old bearer to the scanned host. [Vesta connection screen](https://github.com/mateusz-klatt/vesta/blob/master/Vesta/Views/ConnectView.swift:56) · [Backend switch](https://github.com/mateusz-klatt/vesta/blob/master/Vesta/Models/AppState.swift:121) · [Bearer middleware](https://github.com/mateusz-klatt/vesta/blob/master/Vesta/Services/BearerAuthMiddleware.swift:20)

### Device exchange, receipts, and alerts

`POST /api/auth/device-token` lives outside `/api/budget/*`. It requires a current versioned native session and the existing Budget HTTP and receipt flags. Resolve the target server-side. Issue the intersection of effective grants for that owner and device-eligible IDs. At use, intersect the issued set with live grants and enforce the existing receipt allowlist. Compute implications before intersecting. New device tokens have a distinct wire prefix and HMAC namespace. The legacy 43-character format and its HMAC remain unchanged. [Current bearer dispatch](../hestia/web.py:330) · [Receipts allowlist](../hestia/budget/device_token.py:58)

Check both actor and owner are enabled. Narrow the stored permission ceiling on partial withdrawal and revoke on an empty intersection. Regranting cannot widen the old token. Vesta can exchange the current identity session for new scope. Use five active slots per `(owner, actor)`, retain the current sliding and hard expiry, and require prior-credential proof for new rotation. Preserve the logical token ID on verified rotation. Confine legacy same-label rotation and QR eviction to the owner’s self slots. Keep the `400 budget_auth_ambiguous` guard and token-ID-bound, idempotent self-revoke. [Current rotation](../hestia/budget/device_token.py:490) · [Self-revoke](../hestia/budget/device_token.py:593)

Receipt handlers check actor, owner, grant, and device scope in the store transaction. Record `submitted_by` on new drafts and retain it through confirmation and later edits. Add-only users can finalize or discard their own drafts. A repeat finalize returns a stable `409 already_submitted` without returning the confirmed receipt. Gate candidates in both parse and detail, redact link identifiers without matching permission, and bind delegated receipt cursors to actor and owner. For the new contract, explicit `keep`, `unlink`, and `link` actions distinguish ordinary edits from link changes. Without matching permission, editing match-sensitive fields on a linked receipt preserves its reservation and moves it to `needs_review` without querying the bank transaction. Keep the 20-draft ledger cap, a contributor cap, and an owner reserve. [Parse and draft cap](../hestia/budget/receipt_store.py:231) · [Confirm and legacy null link](../hestia/budget/receipt_store.py:485) · [Detail candidates](../hestia/budget/receipt_store.py:661)

A new attachment to a ledger creates one private **Nowe urządzenie** event for the owner, whether sign-in used password or QR, and whether the actor is the owner or a delegate. Include actor and device label. Verified rotation, sliding renewal, and narrowing stay quiet. Reuse existing token-based inbox references and preserve historical events and read markers. [Inbox projection](../hestia/budget/analytics_views.py:1266) · [Inbox item](../hestia/budget/owner_inbox.py:195)

## Household

`household.view` authorizes the existing projection. Keep `BudgetHouseholdMember` for its grant ID, alias scope, and one-owner-per-member metadata. Update the grant and metadata atomically. Explicit revocation followed by regrant produces a fresh ID. Retain `/api/budget/household/access` returning only `grant_id`, `X-Budget-Grant` on reads, cursor and alias binding, private audit, and all three existing authorization checks. Capability discovery for a household-only user reports availability without exposing the owner’s username.

Backfill compatible relationships with their existing IDs. Let the live disabled check preserve denial for disabled accounts and eligibility after re-enable. Preserve relationships suppressed by legacy roles as dormant metadata. Household visibility grants neither a full ledger read nor receipt submission. [Household checks](../hestia/budget/household.py:121) · [Snapshot and final check](../hestia/budget/household_views.py:134)

## Old-client compatibility

Vesta sends no contract version and treats the session bearer as opaque. Keep these contracts through adoption:

| Route or type | Compatibility contract |
|---|---|
| `POST /api/login` | Request `{user, password, bearer?}`, response `{ok, user, token?}`. `bearer: true` returns `token`. Continue accepting a cached `Authorization` header on this password endpoint because old Vesta can send one. Browser login sets `hestia_session`. In the active versioned phase, native login returns no browser cookie. |
| `GET /api/whoami` | `{user, role}` with nullable string `role`. Use exact literals `viewer`, `operator`, `admin` for authenticated callers and preserve auth-off nulls. |
| `GET /api/users` and user mutations | `{users: [{username, role, disabled}]}` with required enum `role`. Keep browser-only `POST /api/users`, `/api/users/role`, `/api/users/disabled`, and `/api/users/reset-password`. Once custom grants are active, `/api/users/role` returns `409 legacy_role_changes_disabled`. Old Vesta calls none of these. |
| IOC and logout | Preserve native bearer on `GET /api/discovery`, `GET /api/events`, and ordinary actions including `POST /api/control`, `/api/scene`, and `/api/ir`. Keep `POST /api/logout`. |
| `POST /api/budget/receipts/device-token/login` | `{username, password, device_name}` → `{token, token_id, scope, expires_at}` with existing eligibility, lockout, and errors. |
| `POST /api/budget/receipts/device-token/redeem` | `{code, device_name}` → the same response. Preserve manual eight-character codes, 43-character receipt bearers, UUID `token_id`, literal `scope: "receipts"`, and RFC 3339 expiry. |
| Other legacy receipt routes | Keep `/api/budget/receipts/{parse-text,confirm,delete,list,search,detail,image}` with their existing methods and payloads. Keep `/api/budget/receipts/device-token/{pair-code,pair-code-cancel,list,revoke}`. Preserve legacy confirmation with nullable `link`, and self-revoke with `{token_id}`. |

During the rollback-compatible phase, expose the mirrored legacy role. With custom grants active, project `admin` for a provisioned self-owner with `system.manage_users`, `ioc.control`, and the full effective legacy receipt set (`add`, `view`, `edit`, `match`). Project `operator` for other users with `ioc.control`, and `viewer` otherwise. Do not require a bank connection to display `admin`, since legacy QR pairing works before connection. `UserRow` projects declared grants for disabled accounts, with `disabled` independently reported. The server never authorizes from the projection.

In PR 2, decode actual Hestia HTTP responses against the pinned [Vesta 0.9.0 OpenAPI](https://github.com/mateusz-klatt/vesta/blob/master/Vesta/openapi.json). Keep a baseline pinned to that revision when regenerating. Cover fresh password login, cached legacy session, exact `WhoAmI` strings, required `UserRow.role`, discovery and control, both admin pairing paths, receipt parse/confirm/delete, and self-revoke. Assert status and behavior as well as schema shape. Exercise the actual 0.9.0 generated Swift decoder in PR 4. The spouse’s old Vesta must still log in and control IOC, the viewer must be denied control, and the eligible admin must pair receipts. [Vesta role predicates](https://github.com/mateusz-klatt/vesta/blob/master/Vesta/Models/Units.swift:65) · [Legacy response models](../hestia/api_contract.py:2953)

## Migration plan

**Large overall.** Four PRs, one additive schema revision, and no runtime shadow authorization middleware. Use the route matrix and an offline production-copy parity check before cutover.

### PR 1 · Catalog, schema, and session bridge · Medium

**Files and schema.** Add `hestia/authz.py` with the catalog and three functions specified above. Add `PermissionGrant` and `NativeLoginCode` to `hestia/db.py`, extend `User`, `BudgetReceipt`, and `BudgetReceiptDeviceToken`, and add `hestia/migrations/versions/0030_authorization_bridge.py` with `down_revision = "0029"`. Keep `User.role`, the existing pairing table, receipt token digests, and existing insert defaults. Use matching ORM types and server defaults. `issued_permissions` is nullable `TEXT` containing a validated JSON array of permission IDs when populated. `NULL` on pre-cutover tokens is the legacy representation. [Existing models](../hestia/db.py:70) · [Device models](../hestia/db.py:1005) · [Revision 0029](../hestia/migrations/versions/0029_budget_i18n_names.py:14)

The upgrade DDL is:

```sql
ALTER TABLE users ADD COLUMN budget_owner BOOLEAN NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN credential_generation INTEGER NOT NULL DEFAULT 0;

CREATE TABLE permission_grants (
    id INTEGER NOT NULL PRIMARY KEY,
    username TEXT NOT NULL REFERENCES users(username),
    permission_id TEXT NOT NULL,
    target_owner TEXT REFERENCES users(username)
);

CREATE UNIQUE INDEX uq_permission_grants_installation
    ON permission_grants(username, permission_id)
    WHERE target_owner IS NULL;

CREATE UNIQUE INDEX uq_permission_grants_owner
    ON permission_grants(username, permission_id, target_owner)
    WHERE target_owner IS NOT NULL;

ALTER TABLE budget_receipts ADD COLUMN submitted_by TEXT DEFAULT NULL
    CONSTRAINT fk_budget_receipts_submitted_by REFERENCES users(username);

ALTER TABLE budget_receipt_device_tokens ADD COLUMN actor TEXT DEFAULT NULL
    CONSTRAINT fk_budget_receipt_device_tokens_actor REFERENCES users(username);

ALTER TABLE budget_receipt_device_tokens ADD COLUMN issued_permissions TEXT DEFAULT NULL;

CREATE INDEX ix_budget_receipt_device_tokens_owner_actor
    ON budget_receipt_device_tokens(owner, actor);

CREATE TABLE native_login_codes (
    id TEXT NOT NULL PRIMARY KEY,
    username TEXT NOT NULL REFERENCES users(username),
    code_hmac TEXT NOT NULL,
    generation INTEGER NOT NULL,
    created_at FLOAT NOT NULL,
    expires_at FLOAT NOT NULL,
    consumed_at FLOAT,
    CONSTRAINT uq_native_login_codes_code_hmac UNIQUE (code_hmac),
    CONSTRAINT ck_native_login_codes_hmac CHECK (
        length(code_hmac) = 64 AND code_hmac NOT GLOB '*[^0-9a-f]*'
    ),
    CONSTRAINT ck_native_login_codes_generation CHECK (generation >= 0),
    CONSTRAINT ck_native_login_codes_expiry CHECK (expires_at > created_at)
);

CREATE INDEX ix_native_login_codes_active_user
    ON native_login_codes(username, expires_at)
    WHERE consumed_at IS NULL;
```

Use inline nullable `REFERENCES` for the two added FKs. No receipt-table rebuild is needed on upgrade. On downgrade of a verification copy, remove the v4 `AppMeta` keys, new tables, and new indexes first. Use guarded Alembic batch removal for FK-bearing columns, dropping their named constraints first, and preserve the receipt `owner, receipt_date DESC, id` index across the rebuild. Use the real `init_db` and `downgrade_db` runner for both directions. [SQLite ADD COLUMN rules](https://www.sqlite.org/lang_altertable.html#altertabaddcol) · [Receipt rebuild precedent](../hestia/migrations/versions/0024_budget_receipts_v1d_a.py:46) · [Migration runner](../hestia/db.py:1281)

**Phase wiring.** Use the existing `AppMeta(key, value)` table. The migration writes neither activation marker. Only consult them for authoritative SQLite users. With `HESTIA_PERSIST=json`, do not open SQLite from authentication. SQLite with no `users_authoritative` marker retains the existing JSON user fallback. An unexpected phase value fails closed for the affected SQLite path. [AppMeta](../hestia/db.py:44) · [Current user fallback](../hestia/store_sql.py:402)

| Key | Values and behavior |
|---|---|
| `auth_session_phase` | Absent means `legacy`. Emit v1, preserve legacy carrier behavior and existing device revocation, and neither compare nor bump generation. `v2` enables versioned issuance, signed purpose, live generation checks, and generation bumps together. For ordinary compatibility routes, decode legacy sessions as generation zero and accept them only against a live enabled user at generation zero. Require signed browser purpose for protected mutations in the active phase. |
| `authz_policy_phase` | Absent means `legacy`. `catalog_compat` means PR 2 has backfilled and is enforcing grants with transactionally mirrored role writes. PR 1 can boot as its rollback image and enforce the equivalent legacy matrix plus provisioned ownership. `catalog_custom` means direct grants can diverge and role reconciliation has stopped. PR 1 refuses to serve that phase. PR 2 remains grant-aware in that phase and rejects the new device credential format safely. |

**Session code and every verifier caller.** In `hestia/auth.py`, extend `make_session` with optional `generation` and `purpose`, supplied together for v2. Add `decode_session(token, *, now, secret) -> SessionEnvelope | None` where the envelope carries `username`, `expiry`, `generation`, `version`, and `purpose`. Keep `verify_session` as a username-returning compatibility wrapper for existing primitive tests. Preserve legacy encoding, including usernames containing `|`. Encode v2 as `v2.<payload_b64>.<mac_b64>`, using unpadded base64url for compact UTF-8 JSON and the HMAC-SHA256 of `b"hestia/session/v2\0" + payload_b64_ascii`. The JSON has exactly `username`, `expiry`, `generation`, and `purpose`. Check MAC before JSON interpretation, exact types, nonnegative generation, valid purpose and expiry, and reject malformed or unsupported formats. The decoder provides no account authorization. [Current codec](../hestia/auth.py:137)

| Production `auth.verify_session` caller | PR 1 change |
|---|---|
| `hestia/web.py::_auth_middleware`, budget bearer branch | Use the decoded envelope only to identify a signed session carrier and preserve `403 budget_browser_session_required`. Do not bind a principal there. [Branch](../hestia/web.py:348) |
| `hestia/web.py::_auth_middleware`, main session branch | Decode the selected carrier. Load enabled account, phase, and generation in one snapshot. Validate the carrier/purpose and live generation as applicable, then set request identity and a validated session context. Retain the existing role decision for PR 1. [Ingress](../hestia/web.py:361) |
| `hestia/budget/device_token.py::decide` | Treat decoded signed sessions as session-shaped credentials, distinct from opaque receipt tokens. The type check confers no access. Preserve ambiguity and allowlist ordering. [Classifier](../hestia/budget/device_token.py:332) |
| `hestia/budget/api.py::_browser` | Use the validated cookie-carrier context and original cookie bytes. Require the expected actor, apply the active browser-purpose rules, and retain the existing Origin checks. [Browser guard](../hestia/budget/api.py:73) |
| `hestia/budget/consent_api.py::_browser` | Use the same validated cookie context and retain fingerprinting of the **original** cookie bytes. Leave the public callback on its existing protocol. [Consent browser guard](../hestia/budget/consent_api.py:102) |

In `hestia/store_sql.py`, add `auth_session_phase(session)`, `load_auth_state(username)`, `load_login_snapshot(username)`, and `finish_sqlite_login(snapshot, *, native, now, secret)`. Auth state includes the active backend, phase, enabled account, role, and generation where applicable. The login snapshot additionally captures the password hash. In `web.py::_login`, run one real or dummy scrypt outside the lock. For an authoritative SQLite login that passed, use `_immediate_engine` to reread account, hash, and generation. Reject a missing or disabled account, backend change, or changed hash or generation. Issue with the current phase inside that transaction. A later reset makes the just-issued generation stale. Preserve legacy behavior before activation. In active mode, native login returns the token without `Set-Cookie` and skips `consent_api.auth_transition`, which can emit cookie deletions. Browser login retains its consent transition and secure cookie handling. [Current login response](../hestia/web.py:413) · [Consent transition](../hestia/budget/consent_api.py:141)

**Writes, provisioning, and rollback.**

- In `store_sql.py`, update `set_user_db`, `set_user_password`, and the enabled-to-disabled branch of `set_user_disabled`. Use one write-locked helper to increment generation only in the active phase and revoke credentials in the same transaction. On password change or reset, match `actor = username OR (actor IS NULL AND owner = username)`. On disable, match `actor = username OR owner = username`. Invalidate the user’s pending native and legacy pairing codes. Re-enable keeps the bumped generation. In `web.py::_me_password`, pass the observed hash and generation into the password write for compare-and-set after verifying the old password. In `device_token.py::DeviceTokenStore.login`, retain the hash recheck and capture/recheck generation too. [CLI upsert](../hestia/store_sql.py:304) · [Disable](../hestia/store_sql.py:381) · [Password writes](../hestia/store_sql.py:469) · [Receipt password issuance fence](../hestia/budget/device_token.py:458)
- Add `auth.py::_cli_activate_sessions_v2` for local `python -m hestia.auth activate-sessions-v2`, backed by `store_sql.activate_sessions_v2`. Require authoritative SQLite users and configured session authentication. Under `BEGIN IMMEDIATE`, write `auth_session_phase=v2` once. Run this in PR 2 rollout **after** the PR 1 image is a verified rollback target. The rollback image reads and honors the marker and continues issuing v2. Never toggle the marker back to legacy.
- Add `auth.py::_cli_budget_owner` for `python -m hestia.auth budget-owner <username>`, backed by `store_sql.provision_budget_owner`. Use `_immediate_engine`, require authoritative SQLite and an existing enabled user, and refuse a conflicting target. Before custom grants, require current legacy admin eligibility to preserve rollback parity. In custom mode, local provisioning can designate an enabled account independently of installation administration. Set the marker and seed direct self-budget permissions in the same transaction. A repeat on an already provisioned account leaves later grant edits intact. Browser add, role changes, and password reset never set ownership. On a fresh install, use the existing `auth add` and SQLite users import first, then provision the owner before first-bank setup. [CLI dispatch](../hestia/auth.py:209) · [Users import](../hestia/store_sql.py:291)
- Put `sync_legacy_grants(session)` and phase handling in the bridge so it can accept legacy writes after PR 2 rollback. In `store_sql.py::add_user`, `set_user_role`, explicit-role `set_user_db`, and `cutover_users`, mirror changes in `catalog_compat` under the same write lock. Keep `users.role` current. Resolve a CLI password upsert with no explicit role inside the lock so it preserves the current role. Preserve legacy targeted receipt revocation on role change. Bring household grant/revoke in `budget/household.py` through the same compatibility transaction. In `catalog_compat`, apply the provisioned-owner check in the bridge’s legacy budget entry points and token paths. A role promotion alone cannot create an owner. [Role mutations](../hestia/store_sql.py:359) · [Household write path](../hestia/budget/household.py:200)

**Tests and acceptance.** Add `tests/test_authz.py` for catalog validation, storage/delegation scope, closure without an explicit prerequisite row, calendar-only grants, disabled/dormant users, and single-target resolution. Extend `tests/test_auth.py`, `tests/test_web.py`, `tests/test_budget_device_tokens.py`, and `tests/test_budget_consent_api.py` for phase-off reset then login, phase-on v1 generation zero, generation mismatch after reset/disable, concurrent reset during scrypt, all three password write paths, native cookie substitution, signed bearer classification, consent cookie fingerprinting, and JSON mode without `init_db`. Test owner CLI rejection, idempotency, and first-bank setup. Extend `tests/test_db.py` and `tests/test_migration_downgrade.py` for DDL drift, legacy inserts, FK and index preservation, and a populated `0029 → 0030 → 0029 → 0030` round trip. [Current auth tests](../tests/test_auth.py:102) · [Consent tests](../tests/test_budget_consent_api.py:141) · [Migration tests](../tests/test_migration_downgrade.py:110)

The rollback artifact is a **legacy-behavior image built with `0030` and the complete session/role bridge**. Verify that image against populated `0030` and both session phases before production advances. Stock `0029` is usable only with a `0029` database. Accept PR 1 when the production-copy checks pass, the phase remains off, and old behavior and legacy inserts survive the image swap. New QR routes, catalog authorization cutover, and grant editing are later work. [Alembic startup](../hestia/db.py:1322)

### PR 2 · Backfill and server cutover · Medium

Replace role floors in `web.py` and `api_contract.py` with explicit operation requirements. Backfill `viewer` to `ioc.view` and `calendar.view`, `operator` to those plus `ioc.control` and `calendar.edit`, and `admin` to the full existing IOC and Calendar set, installation administration, and its own Budget permissions. Preserve `system.audit.view` for every legacy role. Seed legacy owner markers and compatible household grants. Bind existing tokens to actor-equals-owner with unchanged digests, IDs, expiry, and revocation state. Reconcile once under the write lock and set `authz_policy_phase=catalog_compat`. Keep legacy roles and grant projection mirrored while PR 1 remains the rollback floor.

Move duplicate role checks out of the Budget and household adapters, preserve resource scoping, wire protected browser mutations to core Origin checking, and recheck SSE periodically. Add the exhaustive route matrix, Calendar classification including instances when registered, pinned Vesta decode tests, and offline production-copy parity. Activate v2 sessions only after the bridge is verified. **Rollback image:** PR 1 bridge. **Accept when** the spouse retains IOC and Calendar operation plus her household view, admins retain old pairing, and no viewer or operator gains a full ledger. [Current route matrix test](../tests/test_web.py:2139) · [Budget view gate](../hestia/budget/views.py:73)

### PR 3 · QR, receipt exchange, and capability UI · Medium

Reuse rate limiting and one-time code mechanics from `budget/device_token.py` in core auth, with separate HMAC keys. Add origin-bearing QR issue/redeem, the version 4/L renderer, receipt exchange, attribution, narrowed scopes, and actor-aware inbox projection. Reuse `receipt_api.py` and store transactions for draft, link, and quota checks. Move `users.ts` to catalog checkboxes. Switch `main.ts`, `budget.tsx`, and `budget/api.ts` to live capabilities before activating `catalog_custom`. Separate Calendar mount and editing from IOC boot and control. In particular, unchecking the owner’s IOC or receipt permissions cannot wipe a still-authorized ledger. At activation, make legacy role writes read-only in effect by rejecting the HTTP role mutation and the corresponding CLI role changes.

Test QR Origin and key independence from Budget, code size and replay, default and changed ports, same-label device isolation, partial narrowing, permission changes with an existing native session, matching redaction, draft limits and retries, owner alerts, and role-projection loss without Budget UI invalidation. **Rollback image:** PR 2 grant-aware image, which honors custom grants, refuses new bearer formats, and preserves old Vesta. **Accept when** a granted spouse can submit an attributed receipt without seeing full ledger data, and the owner receives one alert. [Receipt API](../hestia/budget/receipt_api.py:120) · [Current editor](../ui/src/users.ts:63) · [Budget role check](../ui/src/budget/budget.tsx:162)

### PR 4 · Vesta adoption and gated retirement · Medium

Update `ConnectView`, `AppState`, `BackendURLStore`, the native API transport, and receipt credential storage. Reuse the scanner, isolate QR redemption, bind saved credentials to canonical origin and actor, and use native session exchange after either login method. Refresh permissions on reconnect and foreground. Make the receipts surface usable without IOC discovery. Regenerate the contract and exercise both current and pinned decoders. Cover first/changed-origin confirmation, no request before confirmation, cancelled and failed redemption, default port equivalence, no previous bearer on a new-origin request, cached sign-in, and self-revoke.

**Rollback image:** PR 3 with legacy compatibility enabled. **Accept when** the spouse’s installed Vesta passes QR and password login, IOC, and receipt smoke checks, the admin receipt flow works, and other supported clients have migrated. Only then retire the legacy role projection and the old password-pairing and redeem endpoints. Keep password login and native bearer sign-in. Leave physical legacy column removal outside this rollout. [Vesta sign-in](https://github.com/mateusz-klatt/vesta/blob/master/Vesta/Views/ConnectView.swift:75) · [Backend switching](https://github.com/mateusz-klatt/vesta/blob/master/Vesta/Models/AppState.swift:121)

## Risks

- **Rollback and ownership drift.** Additive SQLite columns alone do not make an old image understand the new Alembic head. Verify the schema-aware legacy image and both session phases on an offline production copy through the actual runner. Once custom grants exist, rollback is grant-aware only. [Startup migration](../hestia/db.py:1322) · [Deployment runbook](../docs/DEPLOY.md:13)
- **Generation-zero compatibility.** Resets before activation cannot be reconstructed from legacy tokens. Their compatibility access lasts until natural expiry or a tracked revocation. New issuing operations require a versioned sign-in. An existing Vesta session can refresh grants without reauthentication once it has a current versioned session.
- **QR destination and size.** A reverse proxy must advertise the configured public HTTPS origin. A canonical origin over 45 bytes does not fit this QR format. Show a precise failure before issuing a code and keep manual password sign-in available. The Vesta origin confirmation and isolated transport are required before enabling scanning.
- **Partial grants.** Authorization must be operation-specific, including POST reads, mixed receipt operations, and long-lived SSE. Changes to implication edges can widen existing grants, so review them as authorization changes.
- **Verification status.** This was a read-only source review. No files were changed, and no tests, migrations, or production-copy verification ran. Iris reports that the repository lacks its pre-commit guard. For implementation, `iris guard install` adds it and `git config iris.guardHint false` hides the reminder.

## Owner questions with recommended defaults

| Decision | Recommended default |
|---|---|
| What should the spouse get? | Preserve IOC control, Calendar editing, and `household.view`. Explicitly add only `budget.receipts.add` for the owner’s ledger. |
| Which address should Vesta scan? | Use the canonical public HTTPS origin as `HESTIA_PUBLIC_ORIGIN`. Keep it within 45 bytes for this QR payload. Retain manual password setup for direct HTTP LAN use. |
| Who can designate a ledger owner? | Use the local `budget-owner` CLI. The installation admin manages ordinary grants in the browser. |
| Should a delegate be able to press **Refresh now**? | Keep refresh owner-only in v1. Consider `budget.sync` later for an existing consent, independently of bank setup, consent, and SCA. |
| Should delegates see the full ledger or bank matches? | Defer both. Use the household projection and independent receipt grants. |
| What limits should apply? | Five active device slots per actor and owner. Start with three active drafts per contributor and reserve at least five of the existing 20 ledger draft slots for the owner. |
| When should legacy compatibility retire? | After the spouse and other supported clients pass the installed-version smoke checks. Keep the Vesta 0.9.0 fixture through that release. |

---

# Part B — v5 amendments (take precedence over Part A)


## 1. PR 1 · Correct `0030` DDL and downgrade

**Accepted.** A source trace against the reported versions reproduces the failure without running a migration. SQLite’s `PRAGMA foreign_key_list` reports the inline FK without a name. SQLAlchemy 2.0.58 tries to recover names from table-level `FOREIGN KEY (...)` clauses, so the inline name never enters Alembic’s named-constraint map. Alembic 1.20.1 raises `ValueError` when batch flush tries to remove that name. Batch `drop_column` instead removes the column from the copy and skips constraints that depend on it. The type drift is separate. Alembic compares compiled type names, so `TEXT` versus `String()` produces a diff despite their shared SQLite affinity. [SQLAlchemy FK reflection](https://github.com/sqlalchemy/sqlalchemy/blob/rel_2_0_58/lib/sqlalchemy/dialects/sqlite/base.py#L2434-L2588) · [Alembic batch reconstruction](https://github.com/sqlalchemy/alembic/blob/rel_1_20_1/alembic/operations/batch.py#L346-L376) · [Alembic named drop](https://github.com/sqlalchemy/alembic/blob/rel_1_20_1/alembic/operations/batch.py#L663-L683) · [Type comparison](https://github.com/sqlalchemy/alembic/blob/rel_1_20_1/alembic/ddl/impl.py#L518-L581)

**Replace the affected `0030.upgrade()` declarations and their ORM types.** In `permission_grants`, declare `username`, `permission_id`, and `target_owner` as `VARCHAR`. In `native_login_codes`, declare `id`, `username`, and `code_hmac` as `VARCHAR`. Use matching `String()` columns in `hestia/db.py`. Replace the three nullable additions with:

```sql
ALTER TABLE budget_receipts
    ADD COLUMN submitted_by VARCHAR REFERENCES users(username);

ALTER TABLE budget_receipt_device_tokens
    ADD COLUMN actor VARCHAR REFERENCES users(username);

ALTER TABLE budget_receipt_device_tokens
    ADD COLUMN issued_permissions TEXT;
```

Emit the two FK additions using raw `op.execute` statements. Model both as nullable `String()` with an unnamed `ForeignKey("users.username")` and default FK actions. Use `Text()` only for `issued_permissions`. All three columns have implicit `NULL` defaults. Ordinary `op.add_column(... ForeignKey(...))` may emit a separate constraint operation that SQLite rejects. Alembic also has an `inline_references` option, but PR 1 uses the explicit SQLite DDL above. [Alembic add-column implementation](https://github.com/sqlalchemy/alembic/blob/rel_1_20_1/alembic/operations/toimpl.py#L151-L184) · [SQLite ADD COLUMN](https://www.sqlite.org/lang_altertable.html#altertabaddcol)

**Replace the downgrade instructions with:**

```python
def downgrade():
    op.execute(
        "DELETE FROM app_meta "
        "WHERE key IN ('auth_session_phase', 'authz_policy_phase')"
    )
    op.drop_table("native_login_codes")
    op.drop_table("permission_grants")
    op.drop_index(
        "ix_budget_receipt_device_tokens_owner_actor",
        table_name="budget_receipt_device_tokens",
    )
    op.drop_index("ix_budget_receipts_owner_date", table_name="budget_receipts")

    for table, columns in (
        ("budget_receipt_device_tokens", ("issued_permissions", "actor")),
        ("budget_receipts", ("submitted_by",)),
        ("users", ("credential_generation", "budget_owner")),
    ):
        op.execute(f'DROP TABLE IF EXISTS "_alembic_tmp_{table}"')
        with op.batch_alter_table(table, recreate="always") as batch:
            for column in columns:
                batch.drop_column(column)

    op.create_index(
        "ix_budget_receipts_owner_date",
        "budget_receipts",
        ["owner", sa.text("receipt_date DESC"), "id"],
    )
```

Use the existing migration runner for this downgrade on a verification copy. There is no `drop_constraint` call for either inline FK. The explicit index recreation follows `0024`, which documents that reflection loses the descending sort order. [Receipt index precedent](../hestia/migrations/versions/0024_budget_receipts_v1d_a.py:46) · [Migration runner](../hestia/db.py:1281)

**Acceptance tests.** Keep `test_models_match_migration_no_drift` unchanged and require an empty diff. Add a focused populated `0029 → 0030 → 0029 → 0030` test with receipt lines, Gmail provenance, and a legacy device token. Assert old inserts still work, new nullable columns default to `NULL`, the two FKs enforce references at `0030`, existing rows and token HMACs survive, and integrity and FK checks pass with no temporary tables left behind. Assert `receipt_date DESC` via `PRAGMA index_xinfo`. Keep the Gmail fixture in this focused test because the stepwise suite crosses an older downgrade that rejects Gmail provenance. This was source verification only. No tests or migrations were executed. [Drift test](../tests/test_db.py:141) · [Stepwise test](../tests/test_migration_downgrade.py:110) · [Older downgrade guard](../hestia/migrations/versions/0024_budget_receipts_v1d_a.py:168)

## 2. PR 1 · Freeze the active-phase browser route and status contracts

**Accepted.** The core routes have role floors but no browser-purpose check. Consent GET and POST share a browser guard. Only consent POST currently adds the Origin check. Several Budget handlers recheck the browser credential after reading the body, so replacing their verifier with an unchecked copy of the ingress principal would lose the expiry check. [Core role floors](../hestia/web.py:256) · [Consent guard](../hestia/budget/consent_api.py:102) · [Expiry regression](../tests/test_budget_api.py:411)

**Replace the deferral of core Origin checking to PR 2.** In PR 1, add the following exact `(method, path)` browser-purpose policy in `web.py`. Apply it only to authoritative SQLite sessions in the active `auth_session_phase=v2`. Preserve the existing authentication and role checks.

| Routes | Active-phase rule |
|---|---|
| `GET /api/users` | Signed v2 browser cookie. No Origin requirement for this read. |
| `POST /api/users`, `/api/users/role`, `/api/users/disabled`, `/api/users/reset-password`, `/api/me/password` | Signed v2 browser cookie and core Origin check. |
| `POST /api/name`, `/api/whole-home`, `/api/automations`, `/api/automations/delete`, `/api/graduate`, `/api/rooms/icons` | Signed v2 browser cookie and core Origin check. |
| Ordinary IOC actions, Calendar reads and writes, `GET` and `POST /api/settings`, and existing configuration GETs | Existing session-carrier and role behavior. They are outside the new core browser-only set. |

Use one `Origin` header matched against configured `HESTIA_PUBLIC_ORIGIN`, or the direct scheme and validated authority when the setting is absent. Ignore forwarded headers in that fallback. Preserve password `/api/login`, public logout behavior, auth-off mode, and the current signed native bearer on ordinary IOC routes.

**Budget keeps its existing route partition.** Add active-phase browser purpose to the following paths through the existing Budget helpers. Keep Budget feature checks and its separate Origin configuration.

| Existing guard | Exact routes and behavior |
|---|---|
| `budget/api.py::_browser` | Core Budget POSTs `account-selection`, `account-kind`, `connection-state`, `background-selection`, `refresh`. All registered analytics POSTs, including `transactions-by-keys`. Browser receipt POSTs `parse-text`, `confirm`, `list`, `search`, `delete`, and browser device `pair-code`, `pair-code-cancel`, `list`, `revoke`. Gmail `POST gmail-grant`, `gmail-revoke`, `gmail-sync`, and `GET gmail-status`. Preserve existing Origin checks and the existing same-origin Fetch Metadata fallback for Gmail GET. |
| `budget/consent_api.py::_browser` | `GET /api/budget/consent/{pending,status,renewal-reminders,reconcile}` requires the cookie without a new Origin requirement. `POST /api/budget/consent/{start,complete,cancel}` also runs the existing Budget Origin check. |
| Existing exceptions | Receipt `GET detail` and `GET image` retain their current read paths. Legacy device login and redeem, valid receipt bearers on their allowlist, and exact-token self-revoke retain their own credential rules. The public `/api/budget/enable-banking/callback` and `/budget/connect/complete{,.js}` retain their current protocol and public routing. |

[Budget mutations](../hestia/budget/api.py:224) · [Analytics dispatch](../hestia/budget/analytics_api.py:157) · [Receipt dispatch](../hestia/budget/receipt_api.py:120) · [Gmail dispatch](../hestia/budget/gmail_api.py:116) · [Consent registrations](../hestia/budget/consent_api.py:277)

**Status contract.**

- Missing, invalid, expired, wrong-carrier, and stale-generation sessions retain the ingress `401`. A live v1 cookie on a protected route returns `401` with `browser_session_upgrade_required` in core or `budget_browser_session_upgrade_required` in Budget. The browser can prompt for a fresh password login.
- An authenticated native bearer on a core browser-only route returns `403 browser_session_required`. A signed session bearer on Budget keeps `403 budget_browser_session_required`. Receipt cookie plus Authorization keeps its earlier `400 budget_auth_ambiguous` check.
- A valid browser cookie with missing or mismatched required Origin returns `403 origin_required` in core or the existing `403 budget_origin_required` in Budget. Malformed configured core origin returns `503 auth_origin_unavailable`. Existing feature-disabled responses and role denials retain their precedence.

Make `validate_browser_context(request, *, actor, now)` consume only the validated ingress cookie context. Require the same actor and original cookie bytes, enforce `now < expiry` on **every** invocation, and apply the purpose rule for the observed phase. Keep invoking it before and after the body read wherever `_browser` currently does. Consent fingerprints the original cookie bytes. A direct request without a validated context fails closed. [Existing file-level verifier sites](../hestia/budget/api.py:73) · [Consent fingerprint](../hestia/budget/consent_api.py:102)

**Acceptance tests.** Cover the exact route matrix with a v1 cookie, v2 browser cookie, and v2 native bearer, including every private consent GET, callback exemption, receipt ambiguity, and ordinary Vesta control. Advance an injected clock during JSON reading to prove the post-body check prevents a write. Update the `verify_session` mock in `tests/test_budget_api.py`, the direct consent mock tests, and the `current_user_role` mocks in `tests/test_web.py` and `tests/test_public_documents.py` to exercise the validated context or `load_auth_state`. Confirm direct HTTP LAN succeeds without Budget or core Origin configuration when the request’s Origin matches. [Expiry test](../tests/test_budget_api.py:411) · [Direct consent test](../tests/test_budget_consent_api.py:419) · [Public-route mock](../tests/test_public_documents.py:27)

## 3. PR 1 · Define legacy projection, write locking, and bridge refusal

**Accepted.** `add_user` currently uses a deferred transaction. `cutover_users` replaces legacy user rows, and the JSON shadow mirror can also upsert or delete them. The household CLI opens its own engine, so a web-only phase check cannot protect these paths. [Add user](../hestia/store_sql.py:453) · [JSON mirror](../hestia/store_sql.py:76) · [Users cutover](../hestia/store_sql.py:291) · [Household CLI](../hestia/budget/household.py:228)

**Replace the `sync_legacy_grants` paragraph with this contract.** `store_sql.sync_legacy_grants(session) -> None` requires the caller to hold `BEGIN IMMEDIATE`. Read the phase in that transaction and flush pending user and household writes first. In `legacy`, return without changing grants. In `catalog_compat`, calculate the desired direct rows across the small user roster and reconcile by set difference. Preserve IDs of unchanged grants. In `catalog_custom` or an unknown phase, raise the bridge phase error before any reconciliation. The helper never commits, changes roles or ownership markers, or rewrites household metadata.

| Legacy source | Direct projection in `catalog_compat` |
|---|---|
| `viewer` | `ioc.view`, `calendar.view`, `system.audit.view` |
| `operator` | Viewer set plus `ioc.control`, `calendar.edit` |
| `admin` | Operator set plus `ioc.configure`, `system.manage_users` |
| Legacy `admin` with `budget_owner=true` | Additionally grant `budget.view`, `budget.edit`, `budget.receipts.add`, `.view`, `.edit`, and `.match`, all on self. |
| Existing household metadata | Grant `household.view` on the recorded owner only if member and owner differ, the member role is `viewer` or `operator`, and the owner role is `admin` with `budget_owner=true`. |

Installation rows have `target_owner=NULL`. Reconcile those installation IDs, the listed self-budget rows, and owner-scoped `household.view` rows. Remove stale managed rows, including a household permission for a revoked or changed pair. Leave unrelated rows untouched, and do not treat them as part of the compatibility policy. Use stored roles for projection even when an account is disabled. Runtime enabled checks keep dormant grants inaccessible, allowing re-enable to restore legacy behavior. Role suppression removes the projected grant while keeping the household metadata and grant ID. Before custom activation, a marker on a demoted admin does not create an effective self target. Validate target conflicts before switching to custom mode. [Legacy household gate](../hestia/budget/household.py:121)

Switch `add_user` to `_immediate_engine(path)` with the same `ok` and duplicate-username `exists` results. Call the reconciler after legacy role and account writes in `add_user`, `set_user_role`, explicit-role `set_user_db`, and `cutover_users`, and after household grant/revoke or compatible owner provisioning. Run mutation and projection under the same transaction. Keep `users.role` current. Seed `budget_owner` for eligible existing legacy admins only in PR 2’s initial cutover or the explicit local provisioning command. Later role promotion alone does not provision a ledger. For a new `budget-owner` designation, check both grants and **dormant** household metadata for a conflicting target. [Immediate writer](../hestia/store_sql.py:328) · [Household writes](../hestia/budget/household.py:200)

**Replace “PR 1 refuses to serve `catalog_custom`” with an explicit boundary.** Add `authz.require_supported_policy_phase(session)` with a supported-phase set compiled into the image. PR 1 supports `legacy` and `catalog_compat`. PR 2 adds `catalog_custom`. PR 1 raises a typed `PolicyPhaseUnsupported` for any other value. Apply it at these boundaries:

- **Startup and HTTP.** Check in SQLite `open_stores` before import and worker startup, and at the standalone web app’s SQLite startup boundary. Recheck on live guarded requests through `load_auth_state` and login through `load_login_snapshot` and `finish_sqlite_login`. Cover SQLite auth-off before it can bypass middleware policy. At startup, fail before serving. At runtime, map to `503 authorization_bridge_incompatible` before role or JSON fallback.
- **Account writes and local CLI.** Check under the write lock before account mutations, owner provisioning, phase activation, initial users import, and direct household grant/revoke through `_require_lineage`. On CLI, return a fixed incompatibility error and a nonzero exit. Guard `cutover_import` and `shadow_import` when they can touch users, with the phase read under the same write lock. The JSON mirror must not overwrite an authoritative SQLite users store, even during compatibility.
- **Direct Budget paths.** Check in `BudgetStore._owner`, `BudgetHTTPStore._owner`, household authorization and CLI, and the device store’s read and write entry points, including inspection and exact-token self-revoke. Guard direct Budget CLI and worker startup before provider work. In `catalog_compat`, the PR 1 legacy owner gate requires both existing admin eligibility and `budget_owner=true`, including legacy receipt-token use.
- **Maintenance.** Leave schema-only `init_db` and `downgrade_db` usable. Pure JSON authentication still does not open SQLite. Switching persistence back to JSON is outside the image-rollback procedure.

[Bootstrap](../hestia/store_sql.py:219) · [Budget owner gate](../hestia/budget/store.py:118) · [HTTP owner gate](../hestia/budget/views.py:73) · [Household lineage](../hestia/budget/household.py:175)

**Acceptance tests.** Cover the exact projection, admin promotion without owner provisioning, role reversals on both sides of a household grant without ID churn, disabled/re-enabled accounts, dormant target conflicts, duplicate account creation, and account and household writes after PR 2 → PR 1 rollback. Assert an already-custom DB causes PR 1 startup, direct CLI, device, and worker entry points to fail before writes or provider calls. Verify schema maintenance still opens the same test DB.

## 4. PR 2 rollback · Restrict the legacy receipt surface

**Accepted, with an additional restriction on partial self-owner grants.** The current middleware accepts a device credential and assigns its owner as the request actor. The receipt store’s parse and detail methods can then return bank candidates, and confirmation can modify any receipt owned by the supplied owner. A check after actor substitution cannot protect delegation. A self-owner with only part of the old permission set also cannot safely use a handler that still assumes full legacy access. [Device dispatch](../hestia/web.py:343) · [Receipt parse](../hestia/budget/receipt_store.py:231) · [Confirm](../hestia/budget/receipt_store.py:485) · [Detail](../hestia/budget/receipt_store.py:661)

**Replace the PR 2 rollback policy with:** PR 2 honors custom IOC, Calendar, account, household, and supported own-ledger grants. Its legacy receipt data surface is available only to an enabled, provisioned **self-owner** with the complete effective legacy receipt set: `budget.receipts.add`, `.view`, `.edit`, and `.match`. Apply that rule to parse, confirm, list, search, detail, image, and delete. Require the full profile for legacy pairing-code issuance, password pairing, and code redemption. Keep browser device list/revoke under the separate owner-only guard. PR 2 advertises no delegated receipt runtime capability. It preserves custom grant rows and attributed data for when PR 3 returns.

Enforce the receipt guard before reading a body, looking up a receipt or candidates, or sliding a token. Repeat it in the receipt store’s read or write transaction. In `device_token.py`, preserve the verified actor separately from the owner. Interpret `actor=NULL` as self only for a verified legacy bearer. Before accepting a legacy bearer for receipt data, require `actor == owner` and the full profile in both current grants and the issued mask. Interpret a `NULL` mask as the legacy profile only for that verified legacy self credential. Reject PR 3’s new bearer namespace as unsupported without falling back to session handling. Keep legacy exact-ID self-revoke as a credential operation, including when live receipt grants have been withdrawn. Restrict legacy rotation and eviction to self slots so the rollback image cannot replace a delegate’s token. [Token inspection](../hestia/budget/device_token.py:547) · [Legacy revoke](../hestia/budget/device_token.py:593)

**Acceptance tests.** Open a PR 3-shaped DB in PR 2 with `catalog_custom`, a delegate grant and attributed draft, a delegate bearer, a narrowed self-owner profile, and a valid legacy owner bearer. With no reseed or downgrade, verify delegated cookie requests return `403 budget_forbidden`, unsupported receipt bearers return `401 device_token_invalid`, and partial self-owner access cannot reach legacy candidates or writes. Assert no change to receipt rows, audit, or denied token timestamps. Verify full-profile legacy owner use and exact legacy self-revoke still work, and legacy rotation and eviction leave delegate rows untouched.

## 5. PR 2 rollback · Move the capability-backed Budget UI into PR 2

**Accepted.** `main.ts` uses the role projection to decide whether to mount Budget, and `budget.tsx` clears private state when the same actor’s role falls below `admin`. The current access barrier also treats any optional operation’s `403` as loss of the entire ledger. All three behaviors can hide an authorized owner’s ledger after rollback. [Initial mount](../ui/src/main.ts:182) · [Reload and invalidation](../ui/src/budget/budget.tsx:135) · [Access-loss helper](../ui/src/budget/model.ts:150)

**Move the server capability read and browser consumption from PR 3 to PR 2.** Add authenticated, no-store `GET /api/me/capabilities`. Return `user`, effective installation permission IDs, and `own_budget` as either `null` or an object with effective own-ledger permission IDs and runtime booleans `can_view`, `can_use_receipts`, and `can_manage_devices`. Resolve them from the same authenticated actor. `can_view` requires the enabled feature and authorized self-ledger view. In PR 2, `can_use_receipts` reflects the complete legacy profile from item 4. Compute device administration independently. Keep `/api/whoami` exactly `{user, role}` for Vesta 0.9.0. A household-only user gets no owner identifier through capabilities.

In `ui/src/main.ts`, mount and expose Budget using `own_budget.can_view`. In `ui/src/budget/api.ts`, replace the Budget component’s role probe with capabilities and bind its scoped client to the actor captured at mount. Send `X-Hestia-Expected-Actor` on its owner-Budget requests. In `budget/api.py::_principal`, treat that optional header only as a precondition. An authenticated mismatch returns private `409 budget_actor_changed` before payload lookup. Never use the header to resolve an owner. Existing clients that omit it retain their contract. This prevents parallel account, connection, and job responses from populating an old view after a browser account switch. [Shared Budget principal](../hestia/budget/api.py:53) · [Budget API wrapper](../ui/src/budget/api.ts:75)

In `ui/src/budget/budget.tsx`, use the existing invalidation and request-epoch mechanism on changed actor, lost ledger access, feature disablement, or `budget_actor_changed`. An optional receipt or device denial clears or hides that panel and refreshes capabilities. It does not clear a still-authorized ledger. Keep device administration available when `can_manage_devices` remains true. Roll back PR 3 → PR 2 with `catalog_custom` intact and the same capability contract. PR 3 adds the grant editor and upgraded receipt behavior on top of this baseline. [Browser reload](../ui/src/budget/budget.tsx:152) · [Device panel mount](../ui/src/budget/budget.tsx:439)

**Acceptance tests.** On the PR 2 rollback image, an authorized owner projected as `viewer` or `operator` still sees the ledger after dropping IOC or receipt permissions. A forbidden optional receipt call leaves accounts and connections visible. Real loss of `budget.view`, actor mismatch, or logout clears private state immediately, and late successful responses cannot restore it. Test an account switch during parallel reload and confirm that the mismatched request reaches no owner lookup. Keep the pinned Vesta 0.9.0 decode test passing without new fields in `WhoAmI`.

---

# Part C: wire contract details agreed with Vesta (2026-11-24)

These details complete Parts A and B for client implementers. Each field below is additive.

1. **`GET /api/me/capabilities`** (PR 2) is authenticated and `Cache-Control: no-store`. It returns:
   ```json
   {"user": "…"|null,
    "installation_permissions": ["ioc.view", "…"],
    "own_budget": {"permissions": ["…"], "can_view": true, "can_use_receipts": true, "can_manage_devices": true} | null,
    "delegated_receipts": {"permissions": ["budget.receipts.add"], "can_use": true} | null,
    "household_available": false}
   ```
   - A delegate never sees the owner's username.
   - In auth-off mode, `user` is `null` and `installation_permissions` lists the full ordinary catalog.
   - A client that gets 404 (an older server) falls back to the legacy `whoami.role` mapping.
2. **`POST /api/auth/device-token`** (PR 3):
   - The request is `{"device_name": "…", "prior_token": "…"?}`. `prior_token` is the proof for a verified rotation.
   - The response is `{"token", "token_id", "scope": "receipts", "actor", "owner", "issued_permissions": […], "expires_at"}`.
   - Self-revoke stays on `POST /api/budget/receipts/device-token/revoke` with `{token_id}`.
3. **Errors** use the existing `{"error": {"code": …}}` envelope:
   - `qr-redeem`: 400 `qr_auth_ambiguous`, 400 `qr_origin_mismatch`, 401 `qr_code_invalid`, 429 `qr_rate_limited` with `Retry-After`, 503 `auth_origin_unavailable`;
   - `device-token`: 401 for a missing or stale native session, 403 `budget_forbidden` when there is no eligible receipt target, 409 `device_token_limit_reached`.
4. **`POST /api/budget/receipts/confirm`** keeps its exact shape for Vesta 2.0, including `"link": null` with the legacy semantics. Explicit `keep`/`unlink`/`link` actions arrive later as an optional additive field; clients that omit it keep today's behaviour.
5. **Transport:**
   - `device-token` needs a current native session and is allowed over plain-HTTP LAN.
   - QR issuance and redemption stay https-only. Their origin is the configured `HESTIA_PUBLIC_ORIGIN`.
   - A 200 response from `qr-redeem` always includes `token`, using the same shape as `POST /api/login` with `bearer: true`, i.e. `{ok, user, token}`.

## Part C addendum: feature detection, token binding, calendar instances (2026-11-24)

6. **Feature probe (PR 2):** a new public, unauthenticated, `no-store` route `GET /api/auth/features` returns
   ```json
   {"auth_contract": 2, "features": ["capabilities", "qr_login", "device_exchange", "calendar_instances"], "qr_origin": "https://hestia.example"|null}
   ```
   - `features` lists only what the server actually has enabled. `qr_origin` is the configured `HESTIA_PUBLIC_ORIGIN` when QR login is available.
   - A server older than PR 2 answers this path with 401, 403 or 404 depending on the auth state (the 0.46.0 middleware applies the admin floor before routing). **Any response other than a 200 carrying `auth_contract >= 2` means legacy:** use `whoami.role`, show no QR sign-in, and skip the exchange.
   - Clients must not infer support from a 404 on `/api/me/capabilities`.
   - Each later PR adds its feature string when its endpoint ships, so a client never sees a QR or exchange button on a server that can't serve it.
7. **Owner-scope key:** the `device-token` response also carries `owner_key`. It is an opaque, stable per-installation HMAC of the target owner (namespace `hestia/auth/owner-key/v1`), never the username. Clients bind a stored token to `(origin, actor, owner_key)`.
8. **Legacy device tokens:** no introspection route. A client migrates a legacy receipts token by calling `POST /api/auth/device-token` with `prior_token` set to the legacy token. The server verifies it as rotation proof, binds it as actor = owner (the self slot) and returns the new token, preserving `token_id`. Until then the legacy token keeps working on the legacy routes, up to PR 4.
9. **`GET /api/calendars/instances`** (OpenAPI 0.47.0) freezes its shape when it merges:
   - the response is `{enabled, groups, truncated, instances[{instance_id, event_id, revision, group, summary, location, start, end, all_day, status, recurrence_id}]}`;
   - query parameters are `from` (inclusive), `to` (exclusive), `group?` and `tz?` (IANA, default UTC).
   - Distinct outcomes: a route missing on an older server means `calendar_instances` is absent from the feature probe; a disabled feature gives 200 with `enabled:false`; a missing permission gives 403.
10. **`prior_token` outcomes on `POST /api/auth/device-token` (PR 3):**
   - A valid proof returns 200 and the new token. The old token is retired server-side and `token_id` is preserved.
   - A proof that is rejected (unknown, revoked, expired, or bound to a different actor/owner) returns **409 `device_token_prior_invalid`**. Nothing is issued and nothing is revoked.
     - Only this code tells the client it may drop the prior token locally. It then repeats the exchange without `prior_token`, which gets a fresh `token_id` and raises the "Nowe urządzenie" alert.
     - After the fresh exchange succeeds, the client may self-revoke the old `token_id` as a best-effort step.
   - Any 401 (native session missing or stale) or 403 (`budget_forbidden`) leaves the prior token as it is. The client keeps it, retries later, and never revokes it on these codes.
11. **Native budget read (PR 3):**
   - The same exchange covers it. There is no separate request or scope parameter.
   - Device-eligible permissions are `budget.view` plus the `budget.receipts.*` set.
   - `issued_permissions` is the intersection of the actor's effective grants for the resolved owner with that set.
   - Eligibility means a non-empty intersection, so an owner holding only `budget.view` gets a token.
   - `scope` is `"receipts"` when the issued set contains only receipt permissions, and `"device"` otherwise.
   - With `budget.view` issued, the bearer may call these GET routes, which keep their 0.46.0 response bodies:
     - `/api/budget/status`;
     - `/api/budget/analytics/monthly-budget`;
     - `/api/budget/analytics/monthly-budget/charts`;
     - `/api/budget/analytics/transactions`;
     - `/api/budget/analytics/overview`;
     - `/api/budget/analytics/categories`.
   - Every other budget route stays browser-only and returns 403 `budget_forbidden` to a device token.
   - At use, the live grants are re-intersected, as for receipts.
   - The feature string is **`budget_native_read`** in `GET /api/auth/features`.
   - The credential is `Authorization: Bearer <device token>` with no cookie. Cookie plus bearer together stays 400 `budget_auth_ambiguous`.
