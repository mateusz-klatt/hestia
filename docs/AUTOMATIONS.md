# hestia automations (M1 — engine core)

A local, cloud-free rules engine: hestia reacts to device events on its own, so the
home keeps working when the Keemple cloud is gone. Implemented in
[`hestia/automations.py`](../hestia/automations.py) and wired into both the proxy and
the standalone server. Stdlib-only; rules persist to a flat `automations.json`.

Triggers cover **event-driven** (scene presses + device-state predicates) and
**time-of-day / day-of-week schedules**. Rules are authored either over the control port
as JSON or in the **web dashboard editor** (M3 — see *Web UI* below). Sunrise/sunset +
cron are a later milestone.

## Rule shape

```jsonc
{
  "id": "hall-motion-light",            // unique, non-empty string (the store key)
  "enabled": true,                      // optional, default true
  "modes": ["proxy", "standalone"],     // optional, default both; non-empty subset
  "debounce": 2.0,                      // optional seconds, default 0 (no debounce)
  "trigger": { ... },                   // required (see below)
  "conditions": [ ... ],                // optional, ANDed (default none)
  "actions": [ ... ]                    // required, non-empty, run in order
}
```

`Rule.from_dict` validates every field up front and raises `ValueError` (naming the
offending field) — so a malformed rule is rejected at author time, not silently at
runtime.

### Triggers

- **Scene** — a function-button press (`PROTOCOL.md` §5.7a):
  ```json
  { "type": "scene", "node": 5, "scene_id": 1 }
  ```
  Fires every time node 5 emits scene id 1 (rapid repeats are bounded by `debounce`).

- **State** — a device field crossing a threshold:
  ```json
  { "type": "state", "node": 7, "field": "temperature", "op": "lt", "value": 18 }
  ```
  **Edge-triggered**: fires only on the predicate's `false → true` transition, never
  again while it stays true; it re-arms once the predicate goes false. `field` must be a
  scalar state field (see below); `endpoints` and `scene` are not addressable here.

- **Time** — a wall-clock schedule:
  ```json
  { "type": "time", "at": "07:30", "days": [0, 1, 2, 3, 4] }
  ```
  Fires at the given `HH:MM` (24h, stored canonical/zero-padded). `days` is optional — a
  non-empty list of weekday numbers (`0`=Monday … `6`=Sunday, matching Python's
  `date.weekday()`); omit it to fire every day. See **Schedules** below.

- **Sun** — sunrise/sunset (± an offset) for the deployment's location:
  ```json
  { "type": "sun", "event": "sunset", "offset_min": -15, "days": [0, 1, 2, 3, 4] }
  ```
  Fires at sunrise or sunset shifted by `offset_min` signed minutes (e.g. `sunset`/`-15`
  = 15 min *before* sunset; default `0`; range ±1440). `event` is `"sunrise"` or
  `"sunset"`; `days` works exactly as for `time`. The location comes from **`HESTIA_LAT`**
  / **`HESTIA_LON`** (decimal degrees, north/east positive) — if either is unset/invalid,
  sun rules simply never fire (no error). Sun times are computed locally with a pure
  stdlib NOAA solar calculation (no network, no `astral`), accurate to a few minutes; on
  days when the sun never crosses the horizon (polar day/night) the rule doesn't fire.
  Set the server's timezone to the location's locale (the fire instant is matched in UTC,
  so DST is handled automatically). See **Schedules** below.

- **Cron** — a standard 5-field cron expression, for schedules `time` can't express:
  ```json
  { "type": "cron", "expr": "*/15 9-17 * * 1-5" }
  ```
  Fields are `minute hour day-of-month month day-of-week` (e.g. above = every 15 min, 09:00–17:59,
  Mon–Fri). Each field is a comma-list of `*` · `n` · `a-b` · `*/s` · `a-b/s` · `n/s`. **Numeric
  only** (no `JAN`/`MON` names, no `@daily`/seconds/`L`/`W`/`#` — these raise a clear error).
  **Day-of-week** is `0–7` with **both `0` and `7` = Sunday** (cron convention; `1`=Monday…`6`=Saturday).
  **`n/s`** (a step without a range, e.g. `5/15` = 5,20,35,50) is accepted as an extension — strict
  Vixie cron rejects it. **day-of-month / day-of-week** combine the classic way: when **neither** field
  starts with `*`, a day matches if **dom OR dow** matches; if either starts with `*` (incl. `*/2`),
  it's **dom AND dow** (a `*…` field always counts as "every"). Cron uses its own day fields (no separate
  `days` key). Fires over the same scheduler as `time`/`sun` — **wall-clock minute granularity** (a
  non-existent DST spring-forward minute never fires; a repeated fall-back minute fires once). See
  **Schedules** below.

- **Presence** — a phone (by MAC) arriving on / leaving the LAN, read from the DHCP lease file:
  ```json
  { "type": "presence", "mac": "aa:bb:cc:dd:ee:ff", "event": "arrive" }
  ```
  `event` is `"arrive"` (the MAC's lease appears → `absent → present` edge) or `"leave"`
  (`present → absent`). **Edge-triggered** per rule: the first poll only establishes the
  baseline (no fire at startup); thereafter only the matching transition fires. Reads the
  lease file at **`HESTIA_LEASES`** every scheduler tick (only while a presence rule exists);
  a lease counts as present until its expiry. **Caveats:** *arrival* is prompt (a phone does
  DHCP on joining Wi-Fi), but *departure* lags by the DHCP lease time (the lease lingers until
  it expires) — use a short lease for responsiveness; and modern phones use **random/private
  Wi-Fi MACs** — turn that off for the home SSID, or pin a router static lease, so the MAC is
  stable. Deployment: hestia must be able to **read** the lease file — see *Deployment* below.

- **Global state field** — a *node-less* sensor crossing a threshold:
  ```json
  { "type": "state", "field": "crib_temp", "op": "gt", "value": 24 }
  { "type": "state", "field": "outdoor_temp", "op": "lt", "value": 0 }
  ```
  Identical edge semantics to a per-node `state` trigger, but the field is **global** (no
  `node`): it belongs to the deployment, not a Z-Wave node. The global fields are
  **`crib_temp`** — the Neno baby-monitor crib temperature, read cloud-free over the LAN by
  the baby-monitor poller (*Deployment — Neno baby-monitor* below; `docs/TUYA.md`) — and **`outdoor_temp`** —
  the outdoor temperature from Open-Meteo (*Deployment — outdoor temperature* below). A global
  field is driven by its poller, not by device events; `node` is omitted (supplying one is
  ignored).

### Conditions

A list of conditions, all of which must hold (AND) at the instant the trigger fires.
Two kinds, freely mixed:

- **State predicate** — `{node, field, op, value}` evaluated against **live** state
  (`node` omitted for a global field):

  ```json
  [ { "node": 10, "field": "switch", "op": "eq", "value": false } ]
  ```

- **Time window** — `{type:"time_window", start, end, days?}` — a time-of-day guard so a
  rule fires only during certain hours. `start`/`end` are `"HH:MM"` (local wall clock);
  the window is the half-open interval `[start, end)` (so `end` is exclusive). `start > end`
  **wraps midnight** (active when `now >= start` **or** `now < end`); `start == end` is
  rejected. Optional `days` (a list of `0..6`, Mon=0) restricts it to those weekdays — for a
  wrapped window the post-midnight tail belongs to the weekday the window **started** on
  (`Mon 22:00–06:00` covers Tue 00:00–05:59).

  ```json
  [ { "type": "time_window", "start": "06:00", "end": "22:00", "days": [0, 1, 2, 3, 4] } ]
  ```

  Because it is a condition, it applies uniformly to **every** trigger type — including
  edge triggers (a flaky PIR can't switch a light on at night; a no-motion→OFF rule won't
  fire mid-bath).

A condition becoming true does **not** retro-fire the rule — only a trigger edge fires;
conditions are an instantaneous gate sampled at that moment.

### Comparison operators

`eq`, `ne` work on any value (`door eq "open"`, `switch eq false`). The ordered ops
`lt`, `le`, `gt`, `ge` require both operands to be real numbers (bool is **not** a
number) — otherwise the predicate is simply false (it never raises). An unseen value
(node hasn't reported the field yet) is `None`: it compares false for the ordered ops,
and for `eq`/`ne` it compares as `None` — so `ne` of an unseen field is true (the field
is genuinely "not that value"). Rule `value`s must be finite scalars (NaN/Infinity are
rejected at author time).

### Scalar state fields

`door`, `level`, `switch`, `setpoint`, `thermostat_on`, `temperature`, `power_w`,
`energy_kwh`, `voltage_v` — the same names the web dashboard / `/api/discovery` use; these
are **per-node** (a predicate needs `node`). Plus the **global** (node-less) fields
**`crib_temp`** (°C from the baby-monitor poller) and **`outdoor_temp`** (°C from the Open-Meteo
poller), usable both as a trigger and a condition — omit `node` for them.

### Actions

An ordered list of control ops — the exact vocabulary the control port already speaks
(`hestia/proxy.py` `_OPS` / `build_command`):

```json
[
  { "op": "switch", "node": 14, "on": true },
  { "op": "level", "node": 9, "value": 60 },
  { "op": "cover", "node": 3, "value": 0 },
  { "op": "thermostat", "node": 7, "celsius": 21.5 },
  { "op": "thermostat_power", "node": 7, "on": true },
  { "op": "lights", "channels": [[1, 99], [2, 0]] },
  { "op": "raw", "hex": "7e..." }
]
```

The op **name** is validated when the rule is stored; per-op argument validity is checked
when the action fires. A single bad action is logged and skipped — it can never tear down
the event loop or block the rule's other actions.

## Loop guards

Our injected command makes the device report new state, which could re-trigger a rule.
Two guards bound this:

1. **Edge-triggered** state predicates (above) plus `State.apply`'s value-gating (a field
   only appears in the change set when it actually changed) prevent a rule from
   re-firing on its own effect.
2. **Per-rule `debounce`** caps re-fire frequency for both trigger kinds. Note a
   debounced state crossing still **consumes** the edge: the engine records the
   predicate as true even when it suppresses the action, so after the window expires the
   predicate must recover (go false) and cross again to re-arm — it won't fire just
   because the window closed.

Cross-rule cycles (rule A flips X → rule B triggered by X flips Y → …) are the
operator's responsibility; debounce keeps any runaway slow. Replacing or deleting a rule
resets that rule's edge/debounce state, so a re-authored rule starts clean.

## Schedules

A background scheduler (one per running proxy/standalone process) wakes every
`HESTIA_SCHEDULER_SECS` seconds (default 20, kept under 60 so every minute is observed)
and fires any due `time`, `sun` and `cron` rule, injecting its actions to the current device session.
Properties to know:

- **Minute granularity, once per minute.** A rule fires once for its matching minute; the
  several sub-minute ticks within that minute are de-duplicated. If the rule's conditions
  are not met at a tick, the minute is *not* consumed — later ticks retry until the
  conditions hold (or the minute passes).
- **No retroactive fire.** A rule whose minute already passed when the process started
  does not fire late — it waits for the next matching minute. Conversely, because the
  fired-this-minute record is in memory, **restarting within a rule's matching minute
  re-fires it that minute**.
- **No device → dropped, not deferred.** If no gateway is connected when a schedule
  fires, its actions are logged and dropped (the minute is still marked fired); they are
  not queued for when a device reconnects.
- **Local wall-clock.** Times are local (`datetime.now()`); a DST transition shifts or
  skips a local time exactly as the wall clock does.

Conditions and `debounce` apply to time rules exactly as to event rules.

## Command reliability — blinds (confirm + retry + pacing)

The Keemple gateway occasionally **drops a command** under burst load — sending all blinds up/down
(a scene or a `door-blinds` rule fires one frame per blind, back-to-back) can leave one blind behind.
Measured on the live log: ~10–20 % of should-move blind commands produced no position report, evenly
across blinds (so it's the gateway under load, not one bad device). Two mitigations, **standalone only**
(in proxy the cloud owns retries):

- **Confirm + retry.** A blind reports its own position after it actually moves, so a *missing* report ≈ a
  dropped command. After every `cover` command (control, scene, or automation — they all build through one
  point) hestia arms a confirm: if the blind hasn't reported ~the commanded position within
  `HESTIA_COVER_CONFIRM_SECS`, it **re-sends** the command, up to `2` times, then gives up with an audit
  row (`actor=system, action=cover, result=confirm-failed`). A newer command for the same blind supersedes
  the pending one; a command to a blind already at the target is a no-op (nothing to confirm).
- **Pacing.** A small gap (`HESTIA_INJECT_GAP_SECS`) is inserted between the frames of a hestia-originated
  burst (the scene fan-out and the scheduler's automation inject) so back-to-back frames don't wedge the
  gateway. It does **not** pace the proxy's cloud→device passthrough or single commands.

| env | meaning | default |
|---|---|---|
| `HESTIA_COVER_CONFIRM_SECS` | window to wait for a blind's confirming position report before re-sending; clamped to `[20, 120]`; `0` disables confirm+retry | `45` |
| `HESTIA_INJECT_GAP_SECS` | inter-frame gap for a hestia burst, clamped to `[0, 2]`; `0` disables pacing | `0.15` |

> **Scope.** Confirm + retry covers `cover` commands dispatched through the normal command path —
> per-device control, the `/api/scene` sweep, and automation `cover` actions (incl. the `door-blinds`
> rules). A *replayed learned Keemple scene batch* (a captured `[1e 32]` multi-device scene button) is sent
> verbatim and is **not** individually confirmed; use a `cover` automation action if you want a blind move
> retried.

## Modes

Each rule's `modes` list says which session it runs in. The proxy runs with
`mode="proxy"`, the standalone server with `mode="standalone"`. A rule that duplicates a
cloud automation can opt out of `proxy` to avoid double-action while the cloud is still
in charge.

## Control-port ops

Over the loopback control port (`:8926`, newline-JSON):

| Op | Request | Response |
|----|---------|----------|
| List | `{"op":"automations"}` | `{"ok":true,"automations":[<rule>,...]}` |
| Create/replace | `{"op":"automation_set","rule":{<rule>}}` | `{"ok":true,"id":"<id>"}` or `{"ok":false,"error":...}` |
| Delete | `{"op":"automation_delete","id":"<id>"}` | `{"ok":true,"deleted":<bool>}` |

`automation_set`/`automation_delete` persist `automations.json` immediately (sharing the
registry's `save_lock`); the periodic autosave and the clean-shutdown save flush it too.

## Web UI (M3)

The web dashboard (`hestia/web.py`, default `http://127.0.0.1:8927/`) has an
**automations** section below the device table: a live list of rules (id · enabled
toggle · trigger summary · condition/action counts · Edit/Delete) plus a JSON editor.

| Endpoint | Method | Body | Maps to |
|----------|--------|------|---------|
| `/api/automations` | `GET` | — | `automations` (list) |
| `/api/automations` | `POST` | the rule object | `automation_set` |
| `/api/automations/delete` | `POST` | `{"id":"<id>"}` | `automation_delete` |

The editor is a JSON textarea (with a *New rule template* button), not a per-field form:
the rule schema spans three trigger types, ANDed conditions, and seven action ops, so the
authoritative validator is `Rule.from_dict` server-side — a rejected rule returns `400`
with its exact message, surfaced inline. `POST /api/automations` doubles as the
enable/disable toggle (it re-saves the whole rule with `enabled` flipped). Requests route
through the same thread→loop bridge as `/api/name` (`_LoopClosed`→503, `_BridgeTimeout`→504,
a persist `OSError`→500). Every mutating POST requires `Content-Type: application/json` (→ `415`
otherwise): a non-CORS-simple content type forces a cross-origin preflight that this server never
grants, blocking CSRF writes (e.g. a forged time rule that actuates devices) from a malicious page
in the operator's browser. The web UI stays **loopback-only** unless `HESTIA_WEB_ALLOW_REMOTE=1`;
it is unauthenticated, so do not expose it directly (front it with a TLS+auth reverse proxy).

## Persistence

`automations.json` (`HESTIA_AUTOMATIONS` to relocate): `{"schema":1,"rules":[...]}`,
written atomically (temp + fsync + `os.replace`). Load degrades gracefully — a missing
file is empty; an unreadable / non-object file, or a non-list `rules`, starts empty with
a warning; an individual invalid rule is skipped; a duplicate id keeps the later
definition; a foreign `schema` is a warning, not a failure — so one bad entry can never
lock the operator out of every working rule.

## Deployment — presence lease file

`presence` triggers read the DHCP lease file at **`HESTIA_LEASES`** (default
`/var/lib/misc/dnsmasq.leases`). On a Pi-hole host set it to `/etc/pihole/dhcp.leases`. The
hestia process must be able to **read** that file — Pi-hole's is `pihole:pihole 0640`, so add the
hestia user to the `pihole` group and restart:
```bash
sudo usermod -aG pihole "$(id -un)"      # then restart hestia.service to pick up the new group
```
If the file is missing or unreadable, presence is simply treated as *unknown* (no rule fires, no
error) — so a misconfigured path can never crash or mis-actuate. The lease read happens only when
at least one `presence` rule exists.

## Deployment — Neno baby-monitor (crib temperature)

The global **`crib_temp`** field is fed by a background poller that reads the Neno baby monitor's
crib-temperature DP **locally** (Tuya v3.3 over the LAN, cloud-free — see `docs/TUYA.md`). It is
**off by default** and does **zero network** unless all three of these are set:

| Env var | Meaning | Default |
|---|---|---|
| `HESTIA_NIANIA_IP` | device LAN IP | — (required) |
| `HESTIA_NIANIA_ID` | Tuya device id (`gwId`) | — (required) |
| `HESTIA_NIANIA_KEY` | **local key** — a per-device secret (see `docs/TUYA.md`) | — (required) |
| `HESTIA_NIANIA_TEMP_DP` | temperature data-point | `238` |
| `HESTIA_NIANIA_SCALE` | divisor: °C = `rawDP / scale` | `10.0` |
| `HESTIA_NIANIA_SECS` | poll interval, clamped to `[30, 3600]` | `90` |

> **The local key is a secret** — set it via the environment (e.g. the systemd unit's
> `Environment=`/`EnvironmentFile=`), never commit it. The poll runs off the event loop (the Tuya
> client uses blocking sockets); the camera allows one connection at a time, so a failed poll simply
> keeps the last value and retries next tick — it never clears `crib_temp` or stalls the loop.

## Deployment — outdoor temperature

The global **`outdoor_temp`** field is fed by a background poller that fetches the current outdoor
temperature from **Open-Meteo** (a free public weather API, no key) for the deployment's location. It
is **opt-in and OFF by default** — it makes **no** network call unless explicitly enabled:

| Env var | Meaning | Default |
|---|---|---|
| `HESTIA_OUTDOOR_TEMP` | enable flag — true only for `1`/`true`/`yes`/`on` (case-insensitive) | off |
| `HESTIA_OUTDOOR_TEMP_SOURCE` | which feeder: `open-meteo` (cloud) or `local` (433 sensor) | `open-meteo` |
| `HESTIA_LAT` / `HESTIA_LON` | location (decimal degrees) — **reused** from the `sun` trigger config | — (required for `open-meteo`) |
| `HESTIA_OUTDOOR_SECS` | poll interval, clamped to `[60, 3600]` | `600` |

> **Egress / privacy:** enabling the `open-meteo` source performs an outbound HTTPS GET to
> `api.open-meteo.com` carrying the configured latitude/longitude (nothing else, no API key). It ships
> **disabled**; turning it on (`HESTIA_OUTDOOR_TEMP=1`, with `HESTIA_LAT`/`LON` set) is your explicit
> opt-in. An unset/blank/false flag — or no coordinates — means the poller never starts (zero egress).
> Like the baby-monitor poller it runs off the event loop and keeps the last value on a failed fetch.

### Source — `local` (a 433 MHz weather sensor via `rtl_433`)

Set `HESTIA_OUTDOOR_TEMP_SOURCE=local` (with `HESTIA_OUTDOOR_TEMP=1`) to feed `outdoor_temp` from a
local 433 MHz weather sensor decoded by the external **`rtl_433`** binary instead of the cloud —
**on-LAN, zero egress**. The two sources are **mutually exclusive** (exactly one feeder runs); an
unknown/typo source disables `outdoor_temp` entirely (fail-safe). `rtl_433` is invoked as a subprocess
(no shell), so it is a runtime *system* dependency, not a Python package — the zero-deps rule holds.

This is a **PUSH** feeder: hestia spawns **one long-lived** `rtl_433 -d <dev> -F json` and applies each
matching reading the instant `rtl_433` streams it (no polling interval, no reception window). Alongside
`outdoor_temp` it also fills a display-only **`outdoor_humidity`** (%RH) global when the packet carries it.
If `rtl_433` exits (e.g. `rtl_tcp` restart), the stream is relaunched after `HESTIA_RTL433_RESTART_SECS`.

| Env var | Meaning | Default |
|---|---|---|
| `HESTIA_RTL433_DEVICE` | rtl_433 `-d` source — e.g. `rtl_tcp:127.0.0.1:1234` or `0` for a direct dongle | `rtl_tcp:127.0.0.1:1234` |
| `HESTIA_RTL433_MODEL` | only accept readings whose `model` matches (recommended) | — (any) |
| `HESTIA_RTL433_ID` | only accept readings whose sensor `id` matches; **`*` = any id** (for a sensor whose id randomises on each battery change) | — (any) |
| `HESTIA_RTL433_CHANNEL` | only accept readings on this `channel` (a physical DIP switch, **stable** across battery changes); pair with `HESTIA_RTL433_ID=*` to track one sensor across battery swaps yet still exclude a neighbour on another channel | — (any) |
| `HESTIA_RTL433_BATTERY_WARN` | show the sensor's `battery_ok` flag on the dashboard (a low battery turns the badge red). **Opt-in** — off by default because some sensors (e.g. a rechargeable `Prologue-TH`) report low *permanently* | off |
| `HESTIA_RTL433_PROTOCOL` | restrict decoding to one rtl_433 protocol number (`-R`) | — (all) |
| `HESTIA_RTL433_RESTART_SECS` | delay before relaunching `rtl_433` after it exits, clamped to `[1, 600]` | `30` |

> Filters (`MODEL` / `ID` / `CHANNEL`) are **exact, case-sensitive** string matches (`id` / `channel` are
> `str()`-ed first, so int `1` matches `"1"`), each skipped when unset/empty. When a `CHANNEL` filter is set,
> a packet that **omits** `channel` is rejected (it is a different device).

> **Hardware requirement:** `local` needs a **433-tuned antenna near the sensor** and a **dedicated**
> SDR/`rtl_tcp` endpoint. Do **not** point `HESTIA_RTL433_DEVICE` at an `rtl_tcp` shared with another
> consumer — neither an FM/RDS receiver (a 433 retune **monopolises** the dongle) nor a second `rtl_433`
> (`rtl_tcp` serves a **single client**, so the second reader silently gets nothing). hestia consumes the
> long-lived stream, takes each matching finite `temperature_C` (+ `humidity` and `battery_ok` if present),
> and **terminates and reaps** its `rtl_433` child on shutdown so a leaked process can never block the SDR.

> **Freshness badge (dashboard):** each sample stamps the time it landed (`outdoor_temp_ts`, and
> `crib_temp_ts` for the baby-monitor temperature), surfaced under the reading as a muted "**N ago**" badge
> that turns **red** once the reading is stale (no sample for >15 min). A silent sensor therefore shows
> visibly stale instead of freezing a believable-but-old number — this is the canonical dead-sensor signal,
> independent of battery chemistry. The optional low-battery flag (`outdoor_battery_ok`, see
> `HESTIA_RTL433_BATTERY_WARN`) adds a red "🪫 low" to the badge; it stays `null` (no badge) while that
> opt-in is off (the default).

> **⚠ `id` changes when the battery dies:** a `Prologue-TH`-class sensor picks a **new random `id`** each
> time it loses power (flat/replaced battery). If you pinned `HESTIA_RTL433_ID`, the feeder then matches
> nothing and `outdoor_temp` freezes. The robust fix is to **not pin the volatile id** — match the stable
> identity instead:
>
> ```
> HESTIA_RTL433_MODEL=Prologue-TH
> HESTIA_RTL433_ID=*            # any id — survives the random renumber on each battery swap
> HESTIA_RTL433_CHANNEL=1       # the physical DIP switch — stable, and excludes a neighbour on another channel
> # HESTIA_RTL433_BATTERY_WARN  # leave off: this sensor reports battery_ok=low permanently on a rechargeable cell
> ```
>
> Why this happens (per rtl_433's `Prologue-TH` decoder): the `id` is *"a random id generated when the
> sensor starts"* (the **same** battery often yields the same id, a **new/replaced** one a different id), and
> `channel` is a separate sensor-set field — so `id` is the volatile part and `channel` the stable one.
>
> The `battery_ok` flag is genuine (`0` = low, `1` = ok), but two quirks make the badge opt-in: the decoder
> notes *"the first reading always says low"* (so even a fresh alkaline shows a transient low on power-up),
> and a **rechargeable** cell reads `battery_ok=0` **persistently** (its ~1.2 V sits below the sensor's
> threshold). So the low-battery flag is only meaningful with an alkaline cell, settled after the first
> reading — and the freshness badge above is the chemistry-independent "sensor went silent" signal.
