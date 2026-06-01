# Tuya v3.3 local client (`hestia/tuya.py`)

A **stdlib-only** reader for Tuya **protocol v3.3** devices on the LAN (TCP `6668`) — no cloud, no
third-party dependency. The AES-128-ECB primitive the protocol needs is implemented in pure Python
(`aes_ecb_encrypt`/`aes_ecb_decrypt`, S-box computed at import, pinned to FIPS-197 test vectors).
Built for the Neno baby monitor (crib temperature); works for any Tuya v3.3 device.

## Wire format (v3.3, big-endian)
```
prefix 0x000055AA | seq(4) | cmd(4) | length(4) | <region> | crc32(4) | suffix 0x0000AA55
```
- `length` counts `<region> + crc + suffix`.
- A **device→app** `<region>` starts with a 4-byte **return code** (commands carry none).
- Payloads are `AES-ECB(compact-json)`. Every command **except `DP_QUERY` (0x0a)** — and every
  device→app reply — also carries a **15-byte cleartext version header** `b"3.3" + 12·NUL` in front of
  the ciphertext (`NO_PROTOCOL_HEADER_CMDS = {0x0a}`). The header is stripped *before* decryption /
  prepended *before* encryption — it is never part of the AES payload.

## "device22"
Devices whose id is **22 characters** return `"data unvalid"` to a plain `DP_QUERY (0x0a)`; they must
be read via `CONTROL_NEW (0x0d)` with an explicit null-valued `dps` map
(`{"dps":{"1":null,…,"20":null}}`). `TuyaDevice` auto-selects this path when `len(device_id) == 22`.
(The Neno baby monitor `0123456789abcdefghijkl` is a device22.)

## API
```python
from hestia.tuya import TuyaDevice, TuyaError
dev = TuyaDevice("192.0.2.19", "<device_id>", "<16-char local_key>")
dps = dev.status()        # -> {"1": True, "2": 259, ...}  (raises TuyaError on any failure)
```
`status()` is read-only and short-lived (connect → query → read → close); it reads with a recv-loop
(`_recv_exact`), caps a frame at 8 KiB, skips empty ACKs (up to `_MAX_FRAMES`), and validates
`retcode == 0` + an accepted cmd + a `dps` dict.

## Getting the `local_key`
A per-device secret, fetched once via the Tuya cloud (the device keeps talking to its cloud; hestia
reads it locally). Easiest: `pip install tinytuya && python -m tinytuya wizard` (logs into the
Smart Life / Tuya account and dumps every device's `local_key`). Pass it to `TuyaDevice`.

## Notes / limits
- v3.3 only (not 3.1/3.4/3.5); read-only (no DPS *set* yet — same codec extends to it later).
- The 15-byte `b"3.3"` header detection has a ~1-in-16M theoretical false-positive on a non-header
  ciphertext; it then fails cleanly as a `TuyaError` (bad padding), never silent-wrong-data.
- `binascii.crc32` returns an unsigned int in Python 3 (no masking needed).
