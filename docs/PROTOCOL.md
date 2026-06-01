# Keemple Gateway Protocol — Observed Specification

**Clean-room spec, derived purely from passive on-the-wire observation** of the
Keemple gateway's cloud traffic — correlated against labelled user actions and
the app's Activity log. **No firmware/binary analysis, no decompilation.** The
implementation in `hestia/` is written from *this document*.

Transport: the gateway opens a long-lived TCP connection to
`gateway.keemple.com:8925` (re-resolved every reconnect; the working backend is
an Alibaba IP). All traffic below is on that connection, **cleartext**.

## 1. Framing

```
0x7e | type(1) | cmd(1) | length(2 BE) | payload(length) | checksum(1) | 0x7e
```

- `0x7e` is the frame flag/delimiter.
- `length` covers `payload` only.
- `checksum` = XOR of every byte from `type` through the end of `payload`
  (verified across 70+ captured frames).
- **`0x7e` is NOT escaped — it also occurs as an ordinary data byte** (payload,
  epoch, or checksum). Empirically ~1 frame in 7 contains a `0x7e` somewhere, so a
  naive split on the delimiter shatters those frames into bad-checksum fragments
  (on a long overnight capture: 875 bad bodies vs **0** length-aware). **`0x7d` is
  not an escape either** — PPP-style unstuffing was tested and made decoding
  *worse*, so there is no byte-stuffing. Frame `type` is never `0x7e` (reserved as
  the delimiter; verified across all captures).
- **Deframing is therefore length+checksum driven, not delimiter-split:** read the
  4-byte header by slicing, take `5 + length` bytes, accept only when the XOR
  checksum validates, and on a miss re-hunt to the next `0x7e`. Implemented in
  `hestia/protocol.py` `Deframer` (used by the live transports and `pcap_audit.py`,
  which keys a `Deframer` per TCP flow). `iter_frames()` is a one-shot wrapper over
  it; note `tools/pcap_frames.py` runs it per TCP segment, so a frame split across
  two segments is dropped there (unchanged) — only `pcap_audit.py` reassembles.

## 2. Payload = TLVs

```
tag(2 BE) | length(2 BE) | value(length)   (repeated)
```

## 3. Frame catalog (observed type/cmd)

From a full scan of all captures (26 pcaps, 1390 flows):

| type | cmd | dir | meaning |
|------|-----|-----|---------|
| `64` 'd' | 01 | S→D | session assigned (`0x0064` UUID, `0x001f`=01 seq init, `0x0069` keepalive secs) — §3.1 |
| `64` 'd' | 02 | D→S | device registration / login (serial, firmware, tokens) — §3.1 |
| `64` 'd' | 03 | S→D | timestamp heartbeat (`0x0066` str, `0x0067`, `0x0068` epoch, `0x00d3`), ~1/min |
| `64` 'd' | 06 | S→D | post-login query (`0x001f`) → answered by `[64 07]` |
| `64` 'd' | 07 | D→S | query reply: `0x000d` Wi-Fi SSID (ASCII), `0x0041`, `0x0001` |
| `66` 'f' | 01 | D→S | keepalive / hello (empty) |
| `67` 'g' | 01 | D→S | device reports its LAN IP (`0x0011` ASCII) |
| `67` 'g' | 02 | S→D | ACK of `[g 01]` (`0x0001`) |
| `67` 'g' | 03 | D→S | periodic device report (`0x003f`, `0x0040` — meaning TBD) |
| `67` 'g' | 04 | S→D | ACK of `[g 03]` (`0x0001`) |
| `1e`     | 07 | S→D | **single-node command** (set one attribute) |
| `1e`     | 08 | D→S | **command ACK** (`0x0001` result, `0x001f` echo) — device's reply to `[1e 07]` |
| `1e`     | 32 | S→D | **batch set** (multiple channels; TLV `0x005a`) |
| `1e`     | 33 | D→S | batch response (TLV `0x005b` = per-element result) |
| `1e`     | 09 | D→S | **sensor / state event** (`0x0047` node, `0x0046` data, `0x001f` seq, `0x0068` epoch, `0x004f`) |
| `1e`     | 0a | S→D | event ACK (echoes `0x001f`) |
| `1e`     | 14 | D→S | **request device roster** → answered by `[1e 15]` |
| `1e`     | 15 | S→D | **device roster**: `0x004d` = `<node><flag>` pairs (`01`=battery, `00`=mains), nodes `0x02–0x17` (§7) |
| `1e`     | 16 | D→S | node presence announce (`0x004e` = `<node> 01`) → `[1e 17]` |
| `1e`     | 17 | S→D | ACK of `[1e 16]` (`0x0001`) |
| `06`     | 02 | D→S | telemetry (`0x003d` = ASCII number, e.g. Wi-Fi signal) |
| `00`     | 00 | D→S | frequent flow-control / ack tick (no payload) |

> Naive `0x7e`-splitting also yielded a few malformed bodies: `65 77 …` (the likely
> duplicate-session reject `"ew"`, seen once during dual-NIC flapping), `[20 20]`
> runs (the connection preamble), and a spurious `0x2020` tag. These were mis-frames
> from a `0x7e` byte inside a payload (§1) — now resolved by the length-aware
> `Deframer`, which decodes them correctly instead of splitting them.

## 3.1 Connection / login sequence

Observed on a clean reconnect:

1. `D→S [66 01]` — empty hello (first frame after TCP connect).
2. `S→D [64 01]` — **session assignment**: `0x0064` = UUID (ASCII, e.g. `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`), `0x001f` = `01` (seq init), `0x0069` = `003c` (keepalive 60 s).
3. `D→S [64 02]` — **registration / login** (~7 s later): `0x0002` serial (ASCII, e.g. `iRemote0000000000000`), `0x0004` firmware (ASCII, e.g. `1.0.000AA`), `0x0065` + `0x004c` 16-byte tokens, `0x0069` keepalive, plus `0x0003 0x0016 0x001a 0x0021 0x0049 0x006c 0x006d` params (TBD), `0x0001` status.
4. `S→D [64 03]` — timestamp heartbeat(s).
5. `D→S [67 01]` (`0x0011` = own LAN IP, ASCII `192.0.2.17`) → `S→D [67 02]` ACK.
6. `S→D [64 06]` → `D→S [64 07]` (`0x000d` = Wi-Fi SSID, ASCII).

**Standalone implication:** hestia *is* the server, so the tokens (`0x0065`/`0x004c`) need **not** be validated — assign a session UUID, accept the registration, keep `[64 03]` heartbeats, and ACK events. The device authenticates *to* the cloud; replacing the cloud means simply accepting it.

## 4. TLV tags (every tag observed across all pcaps)

`0x0000` padding/status · `0x0001` status/result · `0x0002` serial (ASCII) ·
`0x0003`/`0x0016`/`0x001a`/`0x0021`/`0x0049`/`0x006c`/`0x006d` registration params (TBD) ·
`0x0004` firmware (ASCII) · `0x000d` Wi-Fi SSID (ASCII) · `0x0011` LAN IP (ASCII) ·
`0x001f` sequence counter · `0x003d` telemetry number (ASCII) ·
`0x003f`/`0x0040` `[g 03]` report fields (TBD) · `0x0041` `[64 07]` field ·
`0x0046` command/event data (§5) · `0x0047` node id · `0x0048` (00) ·
`0x004c`/`0x0065` 16-byte tokens · `0x004d` device roster (`<node><flag>` list, §7) ·
`0x004e` node id+flag (`[1e 16]`) · `0x004f` per-event flag (in every `[1e 09]`) ·
`0x005a` batch elements ·
`0x005b` batch result · `0x0064` session UUID · `0x0066` timestamp string ·
`0x0067` heartbeat field · `0x0068` unix epoch · `0x0069` keepalive interval (s) ·
`0x00d3` heartbeat flag.

## 5. Attribute data (`0x0046`)

### 5.1 Set level / position — `26 01 <value>`  (value `0x00`–`0x63` = 0–99 %)
Shared primitive for **dimmer brightness** and **roller-blind position**.
- Single node: `[1e 07]` with `0x0046 = 26 01 <value>`, `0x0047 = node`.
- Batch: `[1e 32]` with `0x005a` = repeated elements `<idx:1> 01 <attrlen:1> <node:1> <attr:attrlen>`.
  - `attr` is the same primitive as `[1e 07]`: `26 01 <lvl>` (level/position, attrlen 03), `25 01 <ff/00>` (on/off, attrlen 03), or endpoint-addressed `60 0d 00 <ep> 25 01 <ff/00>` (attrlen 07; ep `01`/`02` = gang 1/2 of a 2-gang switch).
  - **Blinds confirmed both ways:** scheduled 21:00 close set nodes `04 08 05 0b` → `26 01 00`; scheduled 06:30 open set the same four → `26 01 63` (99). Level reports `26 03 <v> 00 fe` came back from those nodes (00, then 63). Batch channel id **==** `[1e 07]` node id.

### 5.2 On/off switch — `25 01 <ff=on / 00=off>`
Binary relay (non-dimmable light / switched load), distinct from the `26 01`
level primitive. Single node: `[1e 07]` `0x0046 = 25 01 <ff/00>`, `0x0047 = node`
(observed on nodes `0x0e`, `0x02`). Reports (in `[1e 09]`): `25 03 ff ff 00` = on,
`25 03 00 00 00` = off. Also drives **smart plugs** (`0x13/0x14/0x15`), whose on/off
reports use the short form `25 03 ff` / `25 03 00`.

### 5.3 Thermostat
- Setpoint: `[1e 07]` `0x0046 = 43 01 01 22 <°C×10 : 2B BE>`  (e.g. 21.0 °C → `2200d2`; 28.0 → `220118`; 4.0 → `220028`). Observed range 16–30 °C; the device's setpoint *report* (§5.4) is integer °C.
- Power: `[1e 07]` `0x0046 = 40 01 <01=on / 00=off>`.
- **Cloud poll (read-request):** `40 02` (power) and `31 04 01` (temperature) are sent `[1e 07]`-style to thermostat nodes `09/0c/0d`; the cloud periodically reads state and the device answers with the `40 03` / `31 05` reports below. hestia can poll likewise or just rely on the spontaneous reports.

### 5.4 State reports (carried in `[1e 09]`, `0x0047` = node)
- Sensor (IAS-Zone-like): `0x0046 = 71 05 00 00 00 ff <type> <state> …`:
  - `type 06` = contact (`16` open / `17` closed) — doors `0x11`, `0x12`.
  - `type 07` = motion/PIR (`08` detected / `00` clear) — `0x10`, `0x0f`.
  - `type 01` (`03`) **and** `type 04` (`07`), both → `00` on clear = **smoke detector `0x16` TAMPER / dismantle** — both fire when the unit is lifted off its base, clear when reseated. The app logs a dismantle warning, matched to the second. **This is the tamper channel only — a real smoke/fire alarm has NOT yet been observed.**
  - `type 05` = **water / flood alarm — flood sensor `0x17`**. State byte after the type: `02` = water detected (app "alarm"), `00` = clear (app "recovery"); the alarm payload is `…ff 05 02 00`, the clear is `…ff 05 00 01 02`. A `30 03 <ff=wet / 00=dry> 06` companion attribute mirrors the same edge. Validated to the second against the app activity log.
  - Validated to the second against the app activity log (doors `0x11`/`0x12`, PIR `0x10`).
- Level (dimmer/blind): `0x0046 = 26 03 <value> 00 fe`.
- Thermostat setpoint: `0x0046 = 43 03 01 04 00 00 00 <°C>`.
- Thermostat power: `0x0046 = 40 03 <01/00>`.
- Measured temperature: `0x0046 = 31 05 01 04 00 00 00 <°C>`.
- Battery (PIR): `0x0046 = 80 03 <percent>` (nodes `0x10`/`0x0f` reported `64` = 100 %).
- Illuminance (PIR `0x0f`): `0x0046 = 31 05 03 0a 00 <value>` (e.g. `7b`→`14` as the room darkened — likely a lux / light-level reading from the PIR).

### 5.6 Smart-plug power metering — `32 02`
Smart plugs (`0x13`/`0x14`/`0x15`) switch on/off via `25 01`/`25 03` (§5.2) and
stream metering as `[1e 09]` events, `0x0046 = 32 02 <sub> <A> <mid:2> <B>`, several
subs (decoded live against the app and all three plugs). The reading lands in slot
`A` or `B` (the other reads `0`); `<mid>` is a small per-plug counter (`012d`/`002c`/
`0094`, sometimes `0`). **hestia takes `max(A, B)`** and surfaces W / kWh / V in the
web "stan" column (`state.py`, `State.plug_w`/`plug_kwh`/`plug_v`):
- `21 44` — cumulative **energy** in `×0.01 kWh`: `0x036b` = 8.75 kWh, `0x078b` = 19.31 kWh, `0x0a9c` ≈ 27.16 kWh (app-validated; per-socket cumulative, independent of the load). Reported periodically (~50 s); the app shows a finer **interpolated** value (last cumulative + power × time). Plug `0x15` carries a high-bit `8000` prefix on this field (meter flag, TBD).
- `a1 42` — mains **voltage** in `×0.01 V`: `0x6068` ≈ 246.8 V, `0x5fca` ≈ 245.2 V.
- `a1 4a` — instantaneous **power** in W: `0x0d` = 13 W (app-validated), `00` when idle.
- `21 54` — a secondary rising counter (energy/runtime — TBD).
Read-only (we decode reports; plug on/off reuses `set_switch`).

### 5.7a Function buttons — scene events (`2b 01` switches · `5b 03` blinds)
Every wall switch (1- and 2-gang) and blind has an extra physical **function button**.
Pressing it emits a Z-Wave scene event in `[1e 09]`, `0x0047` = node:
- **Wall switches → Scene Activation** `0x0046 = 2b 01 <sceneId> <dimDuration>` (CC `0x2B`,
  Set). `dimDuration` always `00`. Observed: nodes `02/07/0a` → scene **3**, `03/06/0e` →
  scene **2** (battery sensors don't have it). The **scene id is fixed per device**
  (assigned in the Keemple app), **not** derived from the gesture — single / double / long
  press are indistinguishable on the wire (controlled test 2026-05-30: 3 gestures → 3
  identical `2b 01 02 00`). No sequence number.
- **Blinds → Central Scene** `0x0046 = 5b 03 <seq> <keyAttr> <sceneId>` (CC `0x5B`,
  Notification). `keyAttr` observed `0x80` (slow-refresh bit + "pressed 1×"); blinds `04/05`
  → scene **1**. `seq` increments per press; the device may re-send the same notification
  (slow refresh) so a **consecutive identical `seq`** is a duplicate. (Any `keyAttr` counts
  as a press — the gesture isn't reliably encoded.)

hestia decodes both in `state.py` (`State.apply`) and emits them as a **transient event**
(rides the SSE `activity` flash as `scene:{id,kind}`; the web UI shows a brief "⏏ scena N"),
never as persistent device state — see `state.scene_seq` (dedup only, not exposed).

**Cloud reaction (matters for standalone).** A press makes the cloud run the scene: it pushes
a batch `[1e 32]` with `0x005a` = repeated `<idx:1> 01 <attrlen:1> <node:1> <attr>` switch
commands (§5.1), acked per element by `[1e 33]` `0x005b`. Observed: node `0x02` scene 3 →
turn-**off** of 7 loads (`06`, `07`/ep1+2, `0e`, `03`, `0a`/ep1+2).

**Standalone-replay (implemented).** Two mutually-exclusive halves — capture needs the cloud,
replay only runs where there is none, so they never both fire (no double-actuation):
- **Capture** (proxy only, `ProxySession._capture_scene_batch`): a decoded press (D→C `[1e 09]`)
  arms `pending_scene = (node, sceneId, monotonic)`; the first cloud batch (C→D `[1e 32]`)
  carrying a `0x005a` within `SCENE_CAPTURE_WINDOW` (2 s) is stored verbatim via
  `Registry.record_scene` under `nodes[node].scenes[sceneId]`, then the pending press is
  consumed. The `0x005a` hex is replayed as-is, so endpoint-addressed (`60 0d`) members need no
  decoding. *Heuristic limit:* one pending slot per session means two presses on different nodes
  within 2 s can cross-attribute; re-pressing re-learns, and a mis-learn can't actuate until you
  switch to standalone — verify first via the `scenes` control op / the "learned scene" log line.
- **Replay** (standalone only, `StandaloneSession._observe` → `commands.scene_batch`): on a press,
  look up the learned batch for `(node, sceneId)` and inject a freshly-`next_seq()`-stamped
  `[1e 32]` to the device, **after** the event ACK (cloud-observed order). Central-scene (blind)
  dedup is inherited from `State.apply`, so a slow-refresh resend replays only once.

The learned table is internal (never surfaced in `/api/discovery`); inspect it with the control
op `{"op":"scenes"}` → `{node: {sceneId: hex}}`. Ties into Phase 3.

### 5.7 Observed `0x0046` prefixes still undecoded (minor, non-blocking)
- `30 03` (×48, node `0x10` PIR) — companion to motion (`30 03 ff 0c` on, `00 0c` clear); occupancy / illuminance?
- `84 07` — battery-sensor status flag (PIR `0x10`, smoke `0x16`) emitted just before `80 03` battery; exact field TBD.
- `70 xx` (PIR `0x0f`) — Z-Wave parameter config exchanged on re-join: cloud `70 04`/`70 05` (set), device `70 06` (report).
- (`40 02` / `31 04` turned out to be thermostat polls — see §5.3.)

## 6. Sequencing / ACK
Each `[1e 09]` event carries `0x001f`, a per-session counter starting at `0001`.
The cloud ACKs with `[1e 0a]` echoing the same `0x001f`. Single-node commands
`[1e 07]` carry their own 4-byte `0x001f`. A standalone server must ACK events
promptly to keep the gateway happy. The device in turn ACKs each command:
`[1e 07]` → `[1e 08]` and `[1e 32]` → `[1e 33]`, each carrying `0x0001` (result) and
the command's `0x001f`; a standalone server should expect these (but need not act).

## 7. Device inventory

| kind | count | nodes |
|------|-------|-------|
| roller blinds | 4 | `0x04`, `0x05`, `0x08`, `0x0b` |
| thermostats | 3 | `0x09`, `0x0c`, `0x0d` |
| lights (dimmer + on/off) | 6 | `0x02`, `0x03`, `0x06`, `0x07` (2-gang), `0x0a` (2-gang), `0x0e` |
| door contacts | 2 | `0x11`, `0x12` |
| motion (PIR) | 2 | `0x10`, `0x0f` (confirmed via battery swap) |
| smart plugs | 3 | nodes `0x13/0x14/0x15` (all active); on/off (`25 01`) + **power metering (`32 02` — DECODED, §5.6)** |
| flood | 1 | node `0x17` (battery) — **CONFIRMED**: water-alarm `71 05 … ff 05` + `30 03 <ff/00> 06` companion + `80 03` battery, validated to the second against the app (§5.4) |
| smoke | 1 | node `0x16` — observed **tamper** (`71 05 … ff 01/04`) + `84 07` + `80 03` battery; fire/smoke signal NOT yet observed; **life-safety, handle last** |

Node map — the gateway's own roster (`[1e 14]`→`[1e 15]`, TLV `0x004d`) lists all
**22 nodes `0x02–0x17`** as `<node><flag>` pairs, matching the inventory
exactly (4 blinds + 3 thermostats + 6 lights + 2 doors + 2 PIR + 3 plugs + flood
+ smoke = 22).

> **Roster `flag` is NOT the power source.** It marks deep-sleep sensors (`01`)
> vs always-listening nodes (`00`) — so battery FLiRS devices flag like mains.
> Confirmed 2026-05-29: all three **thermostats are battery-powered** (`0x0c`
> reported `80 03 4a` = 74 %) yet roster-flag `00`. The reliable battery signal is
> **the presence of an `80 03 <pct>` Battery-CC report**, not the flag; hestia
> derives the displayed power class (and the battery %) from that. True split:
> *battery* = thermostats + PIR + doors + smoke + flood (9); *mains* = light
> switches + blind motors + plugs (13).

**Roster `flag 00` (16) — NB: thermostats here are battery, see note above:**
- lights: `0x02` (ep02), `0x03`, `0x06`, `0x07` (2-gang: ep01 / ep02), `0x0a` (2-gang: ep01 / ep02), `0x0e` (also PIR-driven)
- blinds: `0x04`, `0x05`, `0x08`, `0x0b`
- thermostats: `0x09`, `0x0c`, `0x0d` (range 16–30 °C)
- smart plugs: `0x13` (nr 1), `0x14` (nr 2), `0x15` (nr 3) — all confirmed (on/off + metering)

**Roster `flag 01` (6) — deep-sleep sensors (thermostats also battery, but flag 00):**
- `0x10` PIR · `0x0f` PIR (woke on a battery swap — same `ff 07` motion as `0x10`, + an illuminance reading) · `0x11` door · `0x12` door · `0x16` smoke (tamper)
- `0x17` = flood — **confirmed**: water-alarm `ff 05` (alarm `02` / clear `00`), validated to the second against the app

**All mains devices confirmed by direct labelled actions** (live toggles / setpoints):
every blind, every thermostat (max→min→off), all 6 lights (incl. both `0x0a` endpoints:
ep01 / ep02), both doors, both PIR (`0x10`, `0x0f`), smoke `0x16`, all 3 plugs
(`0x13`/`0x14`/`0x15`). **All 22 nodes identified and exercised** — flood `0x17` confirmed
(water-alarm + clear, validated to the second against the app).

## 8. Clean-room note
Every fact in this document was derived from **passive traffic capture** (which
bytes, when), correlated to labelled user actions and the app activity log — **no
firmware, no decompilation, no binary analysis**. This specification is the sole
input to the implementation: the code in `hestia/` is written from *this document*,
not from the captures. The command encodings (§5) are pinned by the example frames
quoted inline, so an implementation can be checked against the spec directly; the
optional pcap tools (`tools/decode_stream.py`, `tools/pcap_frames.py`,
`tools/pcap_audit.py`) let you re-derive these same observations from your own local
captures. Entries marked **TBD** are observed-but-unlabelled and await a future
labelled-action session. The gateway's own roster (`[1e 14]`→`[1e 15]`)
independently enumerates all 22 nodes, confirming the inventory is complete.
