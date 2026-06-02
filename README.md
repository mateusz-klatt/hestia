# hestia

Local, cloud-free control for **Keemple** smart-home devices (roller-shutter /
blind controllers, switches, dimmers, thermostats, smart plugs, and sensors built
on Hi-Flying Wi-Fi and WCH Ethernet serial-bridge modules).

These devices phone home to a vendor cloud over a **cleartext, custom
`0x7e`-framed binary protocol**. hestia reimplements that protocol locally, so the
devices talk to *it* instead of the cloud — no dependency on the vendor cloud, no
third-party CDN, no public IP. Run it as a transparent **proxy** (relay to the
cloud while decoding everything) or as a **standalone** server that *replaces* the
cloud entirely.

Standard-library core; the sole runtime dependency is **`cryptography`** (the AES-128 primitive for the optional Tuya client). The Keemple protocol/command/state codec stays pure-stdlib, first-party code.

## Clean-room methodology

hestia was built clean-room, in two separate roles:

1. **Observation → specification.** One group worked only from **passive,
   on-the-wire observation** of the gateway's own cleartext LAN traffic, correlated
   with labelled user actions and the app's activity log, and wrote it up as
   [`docs/PROTOCOL.md`](docs/PROTOCOL.md). No firmware, no decompilation, no binary
   analysis.
2. **Specification → implementation.** A second group implemented the codec,
   servers, and tooling **solely from `docs/PROTOCOL.md`** — not from the raw
   traffic. Implementation comments and tests therefore cite sections of
   `PROTOCOL.md`, not captures.

The optional pcap helpers (`tools/decode_stream.py`, `tools/pcap_frames.py`,
`tools/pcap_audit.py`) let you re-derive the same observations from your **own**
local captures; they are validation aids, not the source of the spec.

## Run

```sh
python3 -m hestia            # proxy (default) or standalone, per HESTIA_MODE / persisted mode
# or in Docker (host networking, so it sees real device source IPs):
docker compose up -d --build
```

To make the devices reach hestia instead of the cloud, redirect the gateway's
cloud hostname to this host (e.g. a local DNS override) and/or an iptables
PREROUTING redirect on `:8925`. An extra LAN-IP alias on this host lets an Ethernet
unit that dials a fixed local gateway connect with zero device-side change. See
[`docs/PROTOCOL.md`](docs/PROTOCOL.md) for the wire protocol and the connection
sequence.

## What it does

- **Decodes & forges** the full protocol: framing + TLV codec, every actuator
  command (blinds, dimmers, switches, thermostats, scene/function buttons) and
  every sensor/state report (doors, motion, smoke/flood, smart-plug power
  metering), the login/handshake, and the device roster.
- **Live web dashboard** (stdlib `http.server` + SSE): per-node state, power
  metering, battery %, inline naming, and a local rules editor (loopback-only by
  default).
- **Automations engine** — a local, cloud-free rules engine: event / time / cron /
  sun / presence / global-field triggers → conditioned, debounced actions. See
  [`docs/AUTOMATIONS.md`](docs/AUTOMATIONS.md).
- **Optional integrations** (all opt-in, off by default): a Tuya v3.3 LAN
  client for a temperature device ([`docs/TUYA.md`](docs/TUYA.md)), an
  outdoor-temperature poller (Open-Meteo), and IR control via a serial-attached
  transmitter.

## Tests

```sh
python3 -m unittest discover -s tests
```

100 % line + branch coverage (stdlib `unittest`; `.coveragerc` `fail_under=100`).
