"""In-memory roll-up of decoded ``rtl_433`` packets, for 433 MHz device DISCOVERY.

The local-433 feeder (``sensor433``) filters to one configured weather sensor for the
``outdoor_temp`` global. This registry, by contrast, records EVERY packet rtl_433 decodes
— so the operator can spot a *new* 433 device (a weather station, a garage-door remote,
a doorbell) from its decoded fields and then define / automate it.

Bounded + in-memory (reset on restart) and display-only — it never drives automations and
holds no secrets (ambient 433 MHz telemetry). Keyed by model+id+channel, it keeps the last
decoded fields plus a hit count + first/last-seen, oldest evicted past the cap.
"""
from __future__ import annotations

_DEFAULT_CAP = 200
# rtl_433 bookkeeping fields that don't help identify a device.
_NOISE_FIELDS = frozenset({"time", "mic"})


def _device_key(packet: dict) -> str:
    """A stable identity for a 433 device: model + id + channel (whichever are present)."""
    parts = [str(packet[k]) for k in ("model", "id", "channel") if packet.get(k) is not None]
    return " ".join(parts) if parts else "unknown"


def _fields(packet: dict) -> dict:
    """The decoded fields worth showing — JSON-safe scalars only, minus rtl_433 noise."""
    return {k: v for k, v in packet.items()
            if k not in _NOISE_FIELDS and isinstance(v, (str, int, float, bool))}


class Rf433Registry:
    """Deduped, bounded roll-up of decoded 433 MHz packets for device discovery."""

    def __init__(self, cap: int = _DEFAULT_CAP):
        self.cap = cap
        self._devices: dict = {}

    def record(self, packet, now: float) -> None:
        """Fold one decoded packet into the roll-up. NEVER raises — a malformed packet must
        not drop the SDR stream (the caller hands us whatever rtl_433 emitted)."""
        if not isinstance(packet, dict):
            return
        key = _device_key(packet)
        rec = self._devices.get(key)
        if rec is None:
            if len(self._devices) >= self.cap:                       # bound memory: evict the stalest
                oldest = min(self._devices, key=lambda k: self._devices[k]["last_seen"])
                del self._devices[oldest]
            rec = {"key": key, "first_seen": now, "count": 0}
            self._devices[key] = rec
        rec["count"] += 1
        rec["last_seen"] = now
        rec["fields"] = _fields(packet)

    def snapshot(self) -> list:
        """All discovered devices, newest-seen first — JSON-able for ``GET /api/rf433``."""
        return sorted(self._devices.values(), key=lambda r: r["last_seen"], reverse=True)
