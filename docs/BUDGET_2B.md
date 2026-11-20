# Budget 2B: private HTTP API and Enable Banking consent lifecycle

**Status: design, not implementation or activation.** Baseline: `7bbd7a9`
(#166–168). Fleet deployment reports and an independent read-only check agree:
the running container matches that commit, SQLite is at `0010`, and all eleven
budget tables are empty. Existing CLI behavior is documented in
[BUDGET.md](BUDGET.md); its identity, import, selection and quota guarantees remain
binding. This document does not authorize real SCA, bank requests, data import,
proxy/panel changes or deployment of financial HTTP endpoints.

## Scope and delivery order

2B adds an authenticated API for the configured owner's stored budget, audited
account controls, and a browser consent/renewal flow. Full React budget screens
and product UAT are 2C. A small consent-completion bridge belongs to 2B because
it is required by the existing authentication cookie, not an optional UI feature.
No payments, bank login/password collection, expense predictions or medical module
is added. Bank authentication stays on the bank/Enable Banking pages. Reads never
start acquisition as a side effect. Unattended polling is a separate default-off
piece below; installing the read API does not start it.

Implement in separately reviewed pieces, each preserving the 2A full coverage gate:

1. **2B.1:** HTTP guard, owner-scoped stored reads, web account controls and schemas.
   No public callback or new bank methods enabled in this piece.
2. **2B.jobs:** separately reviewed durable executor and opt-in poller, reusing
   explicitly configured external sessions under 2A limits. No new SCA/callback is
   needed to use those already-authorized sessions; their live use is not implied.
3. **2C:** React budget screens, account choices, freshness/renewal and UAT, first
   using the stored-data API and existing external sessions.
4. **2B.2:** private consent storage, bounded control-plane client, single-use flow,
   Strict-cookie callback bridge and synthetic end-to-end harness.
5. **2B.3:** generation-fenced session adoption, renewal/close semantics,
   crash recovery, proxy/privacy runbook and staged activation checks.

This data-first order delivers useful private data/UI without making callback
logging a prerequisite for stored reads or already-authorized external sessions.
Consent endpoints/bridge remain disabled and unregistered until 2B.2/2B.3; refresh
returns a fixed unavailable response until its executor is enabled. Capabilities
make these boundaries visible to 2C. No dependent piece ships with a placeholder
auth check, and React must never access Enable Banking directly.

## Existing architecture and authentication boundary

Register `hestia/budget/api.py` through a small `web.make_app` hook, following
`calendar_sync/api.py`. Keep orchestration in budget services, SQL in stores and
blocking HTTP/file/SQLite work off the aiohttp event loop. Reuse the project's
cancellation-safe database write/settle pattern where an asynchronous caller can
disconnect while its write is completing. Never hold a SQLite write transaction
across HTTP, browser navigation or sleep.

The current auth middleware deliberately bypasses authentication when auth is
off. Its JSON-user fallback also treats existing JSON users as administrators.
Neither behavior is acceptable for financial endpoints. Every protected budget
handler requires all of:

- enabled Hestia authentication and a valid resolved principal;
- the authoritative, cut-over SQLite users backend;
- a currently enabled SQLite user with the exact `admin` role;
- exact resource ownership, checked again inside the final write transaction.

Do not infer a financial principal from `whoami: {user:null, role:null}`, the
calendar's write permission, a supplied owner field, or a JSON-user fallback.
An administrator cannot read or modify another administrator's budget.

| Condition | HTTP behavior |
| --- | --- |
| Auth off, absent/forged/expired token, deleted or disabled user | `401`; no secret reads or bank calls. Disabled users follow existing Hestia behavior. |
| Authenticated enabled viewer/operator | `403`. |
| Authoritative SQLite users unavailable | `409`, static `budget_requires_sqlite_users`; no fallback. |
| Unknown or another owner's local resource ID | Identical `404`, including mutation targets. |
| Missing/stale expected version during a write | `409`; return safe current metadata to its owner. |
| Feature explicitly disabled | `404`; no activation through a request. |

Classify every non-public route as admin in both `web._ROUTE_MIN_ROLE` and
`api_contract._PATH_ROLES`; unclassified GET routes currently default to viewer.
Preserve existing empty middleware `401/403` responses. Handler errors use fixed
`{ok:false,error:code}` values, never arbitrary exception/provider text.

## Proposed HTTP contract

All resource IDs below are local opaque IDs. Owner/actor/origin, primary hashes,
bank session IDs, key paths, callback URLs and provider account UIDs are never
accepted as caller-controlled ownership or routing inputs. Query/request schemas
reject duplicate scalar parameters, unknown fields and invalid enum/boolean types.

| Method/path | Request and result |
| --- | --- |
| `GET /api/budget/connections` | This owner's configured/stored connections, pause/consent/renewal status and safe capabilities. |
| `GET /api/budget/accounts` | Owner-scoped local IDs, configured/selected/kind metadata and independent component freshness. Optional local `connection_id` filter. |
| `GET /api/budget/status` | 2A aggregate/component/request-limit state, owner-filtered; no network. |
| `GET /api/budget/transactions` | Required owned `account_id`; bounded date filters and opaque keyset cursor, default 50/max 200 rows. Canonical entries only; uncertainty counts separately. |
| `GET /api/budget/balances` | Required owned `account_id`; explicit amount strings, currencies/types and freshness. Imported archived balances are labeled separately. |
| `GET /api/budget/limits` | Effective owner-scoped deadlines/suspensions; no internal shared-scope keys. |
| `POST /api/budget/account-selection` | `{account_id,selected,expected_version}`; persist opt-out and append web audit atomically. |
| `POST /api/budget/account-kind` | `{account_id,kind,expected_version}`; explicit confirmation, independent of selection/currency. |
| `POST /api/budget/connection-state` | `{connection_id,paused,expected_version}`; pause acquisition without erasing history or silently revoking consent. |
| `POST /api/budget/background-selection` | In 2B.jobs: `{connection_id,enabled,expected_version}`; audited owner opt-in, separate from the global poller flag and pause. |
| `POST /api/budget/refresh` | `{connection_id,expected_version,account_id?,operation?}` and required `Idempotency-Key` header. `202` with a durable local job ID, or safe deferred state/deadline. No force/quota bypass. |
| `POST /api/budget/enable-banking/start` | `{connection_id,intent,expected_version}`, intent `connect`/`renew`; returns local flow ID, trusted HTTPS authorization URL and local expiry. |
| `GET /api/budget/enable-banking/callback` | Exact public staging route described below. No exchange or financial response. |
| `GET /budget/connect/complete` | Exact public static completion shell; no secrets or financial data. |
| `GET /api/budget/enable-banking/pending` | Authenticated owner/browser-scoped pending flow metadata; no code, raw state or session ID. |
| `POST /api/budget/enable-banking/complete` | `{flow_id}`; authenticated single-use exchange/verification/adoption. |
| `POST /api/budget/enable-banking/cancel` | `{flow_id}`; local cancellation, not a promise of bank-side revocation. |
| `POST /api/budget/enable-banking/close` | Owned local consent ID and expected version; explicit close intent, subject to managed-session rules below. |

Exact route names and payloads are the implementation contract; add them to
strict Pydantic request/tolerant response models, OpenAPI and generated TypeScript
types. Reads return named lists/objects, writes `{ok:true,...}`. Cursors bind to
owner, account and filters; cap page size and reject reuse against another account.
Never interpret missing rows in a response as deletion of transaction history.

Amounts remain decimal strings, not JSON floats. Do not add unlike currencies or
balance types, treat credit limits as cash, or deduce card repayment dates. Keep
`source`, source observation time, API acquisition time and import time distinct.
Mask account-number display metadata; do not return raw provider JSON.

### Mutations, web audit and jobs

2B mutations are browser-cookie operations. Require JSON framing/body limits and
an exact `Origin` matching the configured public origin; reject missing, `null`
or cross-origin values. SameSite alone is insufficient. Do not enable CORS.
Reads may use existing cookie/bearer principal resolution. For every browser-only
mutation, reject any `Authorization` header (even when a cookie is also present),
then authenticate/recheck the cookie itself and derive owner/actor from that
cookie principal. The global middleware may reject an invalid bearer earlier;
a valid bearer never turns into permission to mutate through a different cookie.
Bearer-only mutation is out of scope. These checks supplement global middleware.

Set `actor` from the authenticated username and `origin="web"` inside the trusted
service; reject caller-supplied actor/origin/owner. Extend 2A control audit without
changing CLI `operator:<uid>` semantics. Optimistic account/control versions prevent
two tabs silently overwriting each other's choices; repeat identical intent is a
no-op. Acquisition timestamps/debits do not increment a control revision.
Kind confirmation never selects an account.

Financial/control audit stays private to the owner. The existing global audit
feed and SSE events are viewer-readable: never publish amounts, descriptions,
account/connection details, flow handles or provider responses there. Do not expose
application/provider-wide suspension clearing over HTTP in 2B; the privileged
operator `limits resume` workflow remains available.

Manual refresh shares 2A persisted debits, cadence, backoff, transport pacing and
component leases. Persist queued/running/terminal jobs and recover interrupted
jobs conservatively. Require a 16–128-character ASCII idempotency key (letters,
digits, `_` and `-`; a random UUID is suitable). Scope it to the owner and retain
it for the job's documented retention period. Repeating the same key and canonical
target returns the same job; changed canonical payload with that key returns
`409` (`idempotency_conflict`). Another tab cannot create extra allowance. Serialize
acquisition through the existing budget lock; do not lock the whole database for
the duration of SCA. GETs never enqueue a refresh. Default refresh jobs remain
background requests and omit all PSU headers.

### Durable executor and opt-in poller (2B.jobs)

Use one dedicated worker thread/executor in the Hestia process, supervised by an
aiohttp cleanup-context task; never execute blocking acquisition on the event loop.
At most one queued/running job exists per owner/connection. More refresh clicks
for the same canonical target coalesce with that job; a different target while
busy returns `409 refresh_in_progress`, not a false acceptance of unexecuted work.
Idempotent retries still return their stored result. Bound queue size and job/idempotency retention explicitly in
implementation configuration. A missing/disabled executor cannot return a job
that will never run: respond with a fixed refresh-unavailable result.

The worker calls the existing acquisition service using full configuration plus
an explicit target selector. **Do not pass a narrowed config into configure():**
its missing entries mean retirement and would disable unrelated connections or
accounts. Select job targets only after full reconciliation. The worker competes
with CLI `once` for the same per-database budget lock and uses the same atomic
per-component reservations; contention defers without consuming an extra debit.

On restart, resume still-queued intents after owner/config/selection/pause checks.
An interrupted running job is reported honestly; consumed debits and cooldowns
stay in force. Do not immediately replay its uncertain acquisition. `deferred`,
`partial`, `failed`, `interrupted` and success are explicit outcomes with safe
next-attempt metadata; a deferred job must not monopolize the worker or one active
slot indefinitely. Stop taking jobs during shutdown, prohibit subsequent requests
and let bounded in-flight work settle off-loop; never release its lease early or
pretend cancellation killed a still-running blocking request.

Add independent default-off executor/poller flags. The poller checks stored due
state periodically (about once a minute, with bounded jitter), enqueues at most
one intent per eligible connection, and performs no HTTP/secret reads when none
is due. This is scheduler polling, not a bank request every minute. Actual starts
stay at most twice per rolling 24 hours, at least 12 hours apart, per selected
endpoint, subject to stricter configuration/Retry-After. Do not replay a backlog
of missed polling ticks after downtime.

Poller execution requires an enabled admin owner, explicit configured allowlist
and owner selection, a non-paused connection and explicitly enabled background
acquisition, including a persisted per-connection owner opt-in. Global enabling
does not grant that opt-in, and config reload cannot silently restore it after
an opt-out. It does not require a live browser login and never borrows old PSU
headers. Authorization/expired-consent failures stop unattended retries and show
attention/reconnect-required until an explicit correction/verification; no SCA is
initiated in the background. Changing a legacy credential reference can be verified
through an explicit operation without granting a new quota allowance.

Existing PoC/external sessions remain external, mounted read-only with verified
process/host UID and owner-only modes. Reuse only the specific sessions the owner
authorizes for Hestia. Before live import or unattended acquisition: update
privacy/terms, verify mounted secrets, back up, obtain account selection and
explicitly enable the relevant flags. API deployment alone neither copies PoC
files nor imports records. Callback-specific logging gates apply later to SCA,
not to this external-session data path.

External-session cutover must coordinate every tool using the same application/
bank/account context: Hestia's local debits cannot see recent PoC requests, and
bank quotas are not separate allowances per tool. Stop competing PoC acquisition
before enabling Hestia. Carry verified recent attempts into conservative quota
state, or wait the applicable full rolling window and any longer server cooldown
from the latest known attempt. Crossing midnight or merely waiting until tomorrow
is not evidence of a fresh allowance. No key/app/session recreation to reset it.

## Browser consent: preserve the Strict session cookie

`hestia_session` is currently HttpOnly, `SameSite=Strict`, with configurable Secure.
It normally does not accompany a cross-site return from the provider. A direct
authenticated callback would fail; trusting state alone in a public callback would
be unsafe. Keep the main cookie Strict and use a staging bridge:

```mermaid
sequenceDiagram
    participant B as Browser
    participant H as Hestia
    participant E as Enable Banking / bank
    B->>H: Authenticated same-origin POST start
    H->>E: POST /auth (server JWT)
    H-->>B: Authorization URL + fresh binding cookie
    B->>E: Explicit SCA / account authorization
    E-->>B: Redirect to callback with state/code
    B->>H: Public GET callback + Lax binding cookie
    H-->>B: 303 to clean static completion page
    B->>H: Same-origin authenticated POST complete
    H->>E: POST /sessions once, then GET verification
    H->>H: Atomic generation-fenced session adoption
    H-->>B: Safe local result; no provider secrets
```

At start, generate independent cryptographic 256-bit state and a fresh per-flow
browser nonce. Store only state hash, nonce hash and a domain-separated HMAC of
the initiating auth token in SQLite, bound to owner, application, bank/country,
connection/generation, intent, registered redirect and expiry. Tokens are signed
`username|expiry`, not unique login IDs; same-user logins in the same second can
coincide, so token fingerprint alone is insufficient.

Set a per-flow `__Host-hestia_budget_<opaque_handle>` binding cookie with Secure,
HttpOnly, SameSite=Lax, short Max-Age, `Path=/` and no Domain. The root path also
lets login/logout see and expire these cookies; a budget-only cookie path would
break that invalidation. Use distinct names for bounded concurrent flows.
Require HTTPS/Secure for consent activation.
Clear binding cookies on terminal completion/cancel and on logout; successful
login invalidates old browser bindings rather than reusing a previous login's
flow. Never recreate a missing binding from state received in a callback.
Cancellation/login/logout must also persist flow invalidation and advance its
binding revision. Cookie deletion alone cannot stop an already-running exchange.
Do not report successful flow cancellation if invalidation failed; inhibit adoption
when a binding's validity cannot be established durably.

Local flow TTL is 15 minutes, with at most two outstanding browser flows per
owner and one per connection. These are Hestia bounds, not a claimed provider
code TTL. After callback, complete immediately through the same-origin bridge;
staged codes expire after at most two minutes and never beyond flow expiry.
Persist start throttling (one per minute, at most three per rolling hour per
owner/bank) as an operational ceiling, with stricter provider backoff taking
precedence. Do not retry a timed-out POST automatically.

Returning in another browser or bank-app webview can lose the binding cookie.
Show a fixed instruction to finish in the browser that started the flow and offer
a safe restart. Do not stage/exchange the returned code without its binding, or
recover through a cross-device/state-only fallback. Such a fallback is a separate
future protocol design. Three simultaneous renewals are queued/sequenced because
only two flows per owner and one per connection may be outstanding.

### Callback and completion steps

1. The public allowlist contains only exact GET callback and GET completion-shell
   routes; no prefix exemption or callback HEAD. Reject duplicate/oversized state,
   code and error parameters, invalid combinations and unknown query fields with
   fixed errors. Recognize optional `error_description` only as bounded,
   discard-only input accompanying an error, so legitimate denial is terminal
   rather than being mistaken for an unknown-field payload. Never echo or persist
   the query or that description.
2. Atomically validate unused state, local TTL, generation and the matching Lax
   browser nonce before claiming the callback. A foreign/missing cookie, reused
   state or another connection's state cannot stage or exchange anything.
   A state-only request must not consume/cancel the legitimate flow; return a
   fixed browser error without modifying it. Only a correctly bound callback can
   produce a terminal flow outcome such as bank denial.
   Even this public route requires enabled consent/auth configuration, the
   authoritative SQLite backend and a still-enabled admin owner from the flow.
3. Stage the code in a private short-lived credential capsule, not plain SQLite,
   frontend storage, redirect URL, HTML or logs. Public GET performs **zero** bank
   HTTP requests and cannot link/select accounts. Provider-denied/error returns
   become a safe terminal flow state, without recording raw error text.
4. Return `303` to fixed `/budget/connect/complete`, without query/fragment. The
   static shell uses only same-origin assets and fetches authenticated pending
   metadata. Auto-complete only when exactly one eligible staged flow is
   unambiguous. If two are ready, require the user to choose from safe connection/
   start-time labels; never select the first row or infer a tab from cookie order.
   A 303 chain alone is not treated as restoring the Strict auth cookie.
5. Complete rechecks the live cookie principal, enabled-admin role, owner,
   fingerprint, browser binding, bank and generation. Missing/changed auth never
   rebinds the flow; login starts a new flow. Atomically claim exchange once before
   HTTP. Reject simultaneous completion/replay without another provider call.
6. Exchange `{code}` at `POST /sessions`, retain the candidate privately and verify
   it with GET before adoption. Remove the code capsule after use or expiry;
   local expiry cleanup must respect an already claimed in-flight exchange.
   Crash/timeout during exchange becomes `exchange_uncertain`: the provider may
   have created a session whose ID Hestia never received. Do not replay the code,
   invent an idempotency key or confuse `authorization_id` with `session_id`.

Use `Cache-Control: no-store` and `Referrer-Policy: no-referrer` on every budget
response, including errors/redirects. The public shell additionally has restrictive
CSP and frame protection, no telemetry or external assets, and renders no private
data without authenticated API calls. Logs may record local operation category,
safe result and correlation ID; never request URLs, query values or capsule data.
API, callback and completion-shell responses must remain uncached through the
edge: verify final headers and `cf-cache-status` is DYNAMIC/BYPASS, including
error/redirect canaries. Do not assume origin no-store overrides every edge rule.

## Enable Banking control plane and consent verification

Add an explicit bounded control-plane client rather than loosening 2A's read path
allowlist. Production origin remains fixed `https://api.enablebanking.com`, with
server-side RS256 JWT, redirects/proxies disabled, bounded bodies/timeouts and
safe allowlisted diagnostics. Allow only required method/path pairs:

| Provider operation | Contract used by Hestia |
| --- | --- |
| `GET /aspsps` | Bounded capability lookup/cache for configured banks, including actual `maximum_consent_validity`; no assumed universal consent lifetime. |
| `POST /auth` | Server constructs `access`, exact configured `aspsp`, opaque state, fixed registered `redirect_url` and explicit personal `psu_type`; returns authorization URL/ID. |
| `POST /sessions` | `{code}` only; returns session ID, full account metadata, bank/access metadata. Some account metadata is only available in this response. |
| `GET /sessions/{id}` | Verify AUTHORIZED, exact bank/country, supported personal access mode, future valid_until and primary account mapping. |
| `DELETE /sessions/{id}` | Explicit managed-consent close; provider says bank-side close occurs only if possible. |

Implement this as `EnableBankingConsentClient`, sharing bounded JWT/transport
primitives with the GET-only acquisition client. Constructor follows
`(application_id, key_pem, *, opener=None, clock=..., monotonic=...)`; expose
`get(path, query=None, *, deadline=None, psu=None)`,
`post(path, body, *, deadline=None, psu=None)` and
`delete(path, *, deadline=None, psu=None)` only for the method/path pairs above.
The injected opener receives a standard urllib Request and bounded timeout;
production never accepts an arbitrary origin/opener through configuration or HTTP.
`psu` is a server-created validated context, not caller-supplied arbitrary headers.
Session routes require strict UUIDs, POST bodies are strict JSON, errors preserve
safe 429 metadata, timeout is at most 60 seconds and the operation has a bounded
monotonic deadline. No retries/redirects are hidden inside these methods.

Control-plane calls share application pacing and applicable rate cooldowns. They
do not consume an account transaction/balance acquisition or bypass its limits.
Keep distinct request contexts so a legacy account cadence floor is not confused
with an auth-endpoint bank limit. No HTTP under a write transaction and no automatic
POST retry after uncertain delivery. Bank URLs, session IDs and paths are derived
only from verified server state, never arbitrary browser targets.

Foreground PSU headers are allowed only for the actual interactive request and
the verified bank requirements. Obtain IP/protocol from an explicitly trusted
proxy chain and the real request context; arbitrary forwarded headers are not
trusted. Reject unavailable required context rather than fabricate online status.
Do not persist raw PSU headers or replay them in queued background jobs.

Request consent validity within the current bank maximum, with a small clock
margin; use returned actual valid_until for status and the seven-day renewal
warning. No hard-coded 180-day promise. Restricted applications may expose only
already-linked accounts. Empty or incomplete selected-account coverage cannot be
treated as successful renewal or deletion of old history.

### Identity and selection stay explicit

2B targets existing operator-configured connections/application; it cannot
reassign owner, bank, application, key or legacy secret path through HTTP. Keep
the existing Enable Banking application, links and localhost redirect. Append the
production redirect only after the deployed callback passes its activation gates.

The version-1 configured account list remains a hard allowlist; SCA and web
selection cannot expand it. Newly discovered identities stay unselected,
unconfirmed and outside acquisition until an operator explicitly adds the primary
hash to configuration through the private operator workflow. 2B must provide safe
candidate local IDs/metadata and document that operator step; it must not silently
rewrite a version-1 config file. A fully browser-managed allowlist/new-connection
mode would require a separate versioned configuration contract.

Match existing accounts only by `(owner, primary identification_hash)`. New
session/account UIDs are routing data, not identity. Secondary hashes, product,
currency and account type never auto-merge identities or confirm kind. If any
previously selected account is absent, ambiguous or belongs to another bank or
unsupported access mode, do not adopt the candidate automatically. Show an owner-scoped
`reconciliation_required` result; preserve prior history and choices. Do not merge
or delete accounts from a restricted/partial bank response, including BNP limits.
The default binding is the Hestia owner plus the verified bank/session and primary
account set. This is not bank-person identity verification: `psu_type=personal`
describes access mode, and `psu_id_hash` derives from an application-supplied or
generated PSU identifier. Neither is a verified human identity; holder names and
emails are not a substitute. A connector-specific verified-person check requires
separately documented guarantees. Do not invent one, reject valid renewal solely
because that application identifier changed, or silently accept a different
primary account set based on matching names or PSU metadata.

## Durable session adoption, renewal and pause

Introduce a connection generation and opaque active-consent reference. Store
session IDs/codes in versioned owner-only capsules under a fixed private directory
(0700, files 0400/0600, regular files, no symlink traversal), following 2A's secret
file boundary. SQLite contains only local references and metadata. Never replace
an externally managed PoC/session file through the browser.

Write/fsync a new immutable capsule and its directory before the short SQLite
transaction adopts its reference. Compare the expected connection generation and
recheck owner/role/selection/bank/primary mapping in that transaction. Also require
the same still-valid exchange claim and flow/binding revision, an unrevoked binding
and an uncanceled eligible flow. Cancellation, login/logout invalidation or expiry
during provider HTTP must prevent adoption after that HTTP finishes; retain a known
candidate for safe reconciliation instead. Filesystem and
SQLite are not one atomic transaction: an unreferenced new capsule is a recoverable
orphan, not permission to overwrite the old file. Cleanup may remove only proven
unreferenced Hestia-owned material; never a mounted or external credential file.

Deployment backup must become a consistent bundle: quiesce consent writers and
capsule cleanup, take a SQLite backup and copy the immutable capsules referenced
by that snapshot, with an integrity/permissions manifest. Protect the bundle and
apply explicit secret-retention policy. A SQLite-only backup is not sufficient to
restore managed consent. Before activation, verify actual container euid/host UID
mapping and bind-mount ownership/modes; do not assume root or UID 1000.
If a capsule is missing, corrupt or inaccessible after restore, block acquisition
and show reconnect/credential-required without crashing the service, deleting
history or substituting some other credential. Stored owner-authorized reads may
still display retained data with blocked/stale acquisition status.

Resolve credentials through the active reference, falling back to the configured
legacy file only for an explicitly external-managed connection. Every acquisition
captures its consent generation and checks it before subsequent requests and
commit. A worker using an older generation cannot publish data or mark a newly
adopted session invalid. Retain already consumed quota debits on an aborted round.
Persist credential mode independently of the active pointer: a managed connection
with a missing/closed pointer must never fall back silently to its old PoC file.

Renewal uses the same flow with intent `renew`, verifies a candidate first and
then swaps the local active reference. Preserve local account IDs, selections,
kinds/audit, imported/API provenance, uncertainties, component freshness and every
quota/deadline. Reauthorization never grants a fresh acquisition allowance.
Some banks expose extended history only briefly after SCA; preserving the existing
quota can miss that window. 2B does not promise a full year or introduce a hidden
foreground quota exception. Offline backfill remains available.

**Bank-side limitation:** some banks invalidate the previous session as soon as a
new authorization succeeds. A failed local renewal therefore preserves the old
local pointer/history, not a guarantee that the old bank session still works.
Revalidate old status when useful, label unavailable/reconnect-required honestly,
and never erase data as a response to invalid/expired/401 consent.

Record provenance `external` vs `hestia_created`; an application ID alone does not
prove exclusive ownership.

A known Hestia-created candidate that fails verification, loses its adoption fence
or needs reconciliation remains `unadopted`, visible only to its initiating owner
with a safe reason and local consent ID. Preserve its private reference so explicit
managed close can be requested; do not hide an active unused consent or auto-delete
it. An uncertain exchange with no returned session ID is different: Hestia cannot
guess/close that remote session and must report the uncertainty honestly.

Local retirement and remote revocation are separate:

- No automatic DELETE of PoC/imported/external sessions during adoption, rollback,
  cleanup or renewal. Do not assume their consent is independent from another tool.
- Hestia-created old sessions become retired locally. Automatic remote cleanup
  stays off until the bank's independence behavior is separately established.
- Explicit close of a known managed consent pauses acquisition, records intent
  before HTTP and attempts DELETE using its private session ID. Retry only under a
  bounded documented close/reconciliation policy; an uncertain response is not
  proof of revocation. A provider absence/closed result may finish the same close
  intent, without deleting local financial history.
- External-managed connections support local pause; requesting a remote close
  returns a safe external-management conflict instead of closing another tool's
  session. Closing a provider session is not local erasure or guaranteed bank-side
  revocation. Data retention/erasure is a separate operator action.

Add persisted owner pause separately from the existing config-derived connection
enabled flag; otherwise `configure()` would undo pause. Reload, restart, refresh
and renewal must preserve it. HTTP pause/resume is audited and version-checked;
it neither deselects accounts nor clears rate suspensions or consumed quota.

## Proposed persistence and configuration changes

Allocate actual migration IDs only when implementing; do not rewrite 0008–0010.

| State | Required metadata/invariants |
| --- | --- |
| Account/connection control versions | Optimistic revision; durable pause and audit attribution, without replacing 2A selection/kind fields. |
| Consent flow | Local ID; owner/connection/app/bank/expected generation; hashed state/browser/session binding; flow/binding revision and revocation; expiry; status and single-use callback/exchange claims; opaque capsule references; safe error. No raw code/state/session IDs. |
| Consent session | Local ID, owner/connection, provenance, generation, candidate/unadopted/active/retired/close-pending/closed/uncertain status, actual validity and opaque capsule reference. |
| Consent control audit | Owner/actor/origin, intent, local target IDs, before/after safe states and time; append atomically with local transitions. No financial or provider payloads. |
| Refresh job | Owner-scoped idempotency key, target local account/operation, expected generation, status and safe outcomes; existing acquisition debits remain authoritative. |

Enable HTTP and consent independently with explicit default-off feature flags.
Keep `HESTIA_BUDGET_CONFIG`; add a validated fixed public HTTPS origin/redirect
and a private capsule directory through project configuration. Never construct
callbacks from Host/Forwarded headers. Missing auth/SQLite/secure-cookie/private
storage prerequisites fail closed; they do not activate a fallback user/backend.
Consent-only directory/redirect prerequisites do not block 2B.1 stored reads when
consent is disabled. The read API requires no key, bank session file or new SCA.
HTTP/consent registration or config loading makes no bank request or import.
Starting the separately enabled poller/executor may process eligible persisted
intents; that opt-in behavior is explicit and separately reviewed.

## Callback logging and activation gates

Current observations distinguish account-specific deployment facts from protocol
guarantees. Hestia currently uses `access_log=None`. Fleet's read-only Apache
inspection found combined access logging (`%r`, which includes query) and an
inherited referrer policy unsuitable for the callback. Those must change before
real SCA: exact callback logging must omit query and Referer, and always emit
`no-referrer`, including errors. Review proxy error handling as well.
Cover both HTTP/HTTPS vhosts, even though the registered callback is HTTPS.
A vhost-level header can override the application header: verify the final
wire response after an exact callback Location override. Include generic aiohttp
exception handling in the no-URL/query logging tests, not just the route handler.
Prepare a reviewed Apache drop-in for both vhosts, configtest, reload and canary
checks. On this host sudo currently requires the operator's interaction; document
that deployment step rather than asking agents to bypass host privileges.

Fleet also observed request-level logging in this Cloudflare account. It has not
demonstrated a query-redaction configuration. Therefore **do not claim that code
or state cannot enter Cloudflare logs**, and do not treat disabling the analytics
beacon or Logpush as proof. One-use state, immediate authenticated exchange and
the required private RS256 application key reduce exposure; they do not satisfy
a no-query-logging guarantee. No provider code TTL is assumed.
Cloudflare documents `ClientRequestURI` as including query and
`ClientRequestPath` as excluding it. Product-specific redaction/export controls
exist, so do not generalize this account observation to "Free cannot redact".
An export transform or hidden UI column is not proof about every retained dataset.

Before real SCA, Home/Holzera must verify the actual edge/proxy configuration with
unique synthetic canaries on success/error/malformed callback paths, checking
all available access/security/error/telemetry sinks without real credentials.
Absence from sampled results alone is not proof; identify dataset, ingestion
fields, redaction scope, retention and access, and record the configuration evidence.
If the proxied callback cannot meet the logging requirement, activation stays off
until a reviewed callback architecture avoids that sink, or the user separately
decides on a clearly documented residual exposure. A peer's risk assessment is
not that decision. Do not change Cloudflare security/DNS or invent support for
fragment/form-post responses to bypass this design gate.

Activation order: reviewed implementation and migrations → privacy/terms updated
for actual bank processing/retention and processors → backup/staged deployment
with consent off → authenticated API/bridge/proxy synthetic UAT → registered
redirect appended while localhost stays → explicit real SCA by the user → explicit
account allowlist/selection → quota-respecting acquisition. No live data import is
implied by deployment. Financial history, backups and secret capsules have separate
retention; published documents must not promise automatic erasure not implemented.

## Acceptance tests and fleet allocation

| Area | Required evidence before its implementation piece is accepted |
| --- | --- |
| Auth/ownership | Auth off and JSON fallback rejected before secrets/network; missing/expired/disabled/deleted 401; viewer/operator 403; two admins isolated; foreign/missing IDs identical 404; demotion/disable/owner change during await blocks commit. |
| Browser/CSRF | Exact Origin/content-type/size/unknown-field matrix; Strict cookie absent on callback but present on same-origin complete; wrong/missing nonce, same-user second browser, logout/relogin and any mutation Authorization header rejected. Two ready flows require explicit selection. |
| One-use flow | Replay, simultaneous callbacks/completions, expired/stale-generation/wrong-bank flow, duplicate/oversized parameters, bank denial, canceled flow; only one POST exchange and no secret echo/log. |
| Provider failures | POST rejection/timeout/crash with uncertain session creation, invalid/expired/empty/partial/wrong-bank or primary-set session; old local data/pointer preserved without claiming old bank availability. |
| Renewal/storage | Changed UIDs with stable primary hashes, different primary requiring reconciliation, bank invalidates old on SCA, file-fsync/DB-swap crashes, stale worker fence, cancel/logout/login during exchange blocks adoption, external sessions never overwritten/deleted, explicit managed close failure. |
| 2A regression | Opt-out/pause/kind/audit survives reload and restart; renewal/import never reset quota or API provenance; 14 pages one acquisition; endpoint/shared 429 and longer Retry-After retained; partial responses never delete history. |
| Executor/poller | One active job per owner/connection, target-aware dedup/busy behavior, CLI lock contention, queue/shutdown/restart recovery, no narrowed-config retirement, no browser-token dependency, owner opt-out/global flag respected, no catch-up burst or HTTP before due. |
| API/privacy | Reads never trigger acquisition, bounded cursor/filter behavior, private audit not global feed/SSE, public bridge contains no private data, canaries absent from controlled logs, documented edge limitation resolved before activation. |
| Contract/deployment | Route-role/OpenAPI drift checks, regenerated TS types, full backend statement/branch gate and UI checks where changed, migration upgrade/downgrade on private copy, logged-in/out UAT after backup. |
| Restore/unused consent | Snapshot plus referenced capsules restores consistently; missing capsule blocks acquisition without fallback/data loss; known unadopted candidate remains owner-visible with explicit close; unknown exchange cannot invent a session ID. |

Codex owns the design, integration and implementation sequencing. Home reviews
auth/callback/UI contracts and performs browser/proxy/edge UAT. Holzera owns the
synthetic Enable Banking harness/fixtures, offline migration pilots and coordinated
deployment. Native subagents can implement disjoint reviewed pieces. Branickiego
has no budget task or credential access. One designated executor deploys; no
parallel panel updates/rebuilds. The user alone performs real bank SCA.

The supplied `fixtures-2b` harness is a test tool, not documentation of bank
guarantees. Keep its assumed error strings/code TTLs labeled synthetic. Required
scenarios include invalidation of the old session during new SCA and timeout after
the server consumed a code. Import only reviewed synthetic fixtures into tests;
never PoC caches, real exports or signing keys.

## Primary sources

Verified against the public sources on 2026-11-20; Hestia policy decisions above
(local TTLs, browser bridge, ceilings and ownership) are design requirements, not
claims that the provider supplies them automatically.

- [Enable Banking API reference](https://enablebanking.com/docs/api/reference/):
  authorization, session creation/resource/closure, account metadata and bank capabilities.
- [Enable Banking FAQ](https://enablebanking.com/docs/faq/): stable identity,
  PSU context, rate limits and possible old-session invalidation during renewal.
- [Control panel and linking](https://enablebanking.com/docs/api/control-panel/):
  restricted applications and existing linked-account constraints.
- [OAuth security BCP, RFC 9700 §2.1](https://www.rfc-editor.org/rfc/rfc9700.html#section-2.1):
  browser/transaction binding and CSRF protection; do not assume undocumented PKCE support.
- [Cloudflare HTTP request fields](https://developers.cloudflare.com/logs/logpush/logpush-job/datasets/zone/http_requests/)
  and [Logpush Transformers](https://developers.cloudflare.com/logs/logpush/transformers/):
  query-bearing fields and export-specific redaction; neither proves this zone's complete logging behavior.
