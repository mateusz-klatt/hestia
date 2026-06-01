"""Per-install device registry: the user-owned map of node → {type, name, room}.

Persisted as a flat JSON file (zero-dep, human-readable, easy to back up). The
`Classifier` may fill in / refresh the inferred type, power class and last-seen,
but **never overwrites a type the user has confirmed**. Naming or assigning a room
does NOT freeze the type — so a node you label before it is classified can still be
typed automatically later. Home Assistant is a downstream client of this file,
never the source of truth (the registry must survive HA being offline).

Node ids are normalised to decimal-string keys (`_key`), accepting int, `"5"` or
`"0x05"` so a caller/UI can't split one device across several entries.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

log = logging.getLogger("hestia.registry")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _key(node) -> str:
    """Canonical decimal-string key for a node id (int, '5' or '0x05' → '5')."""
    return str(int(node, 0) if isinstance(node, str) else int(node))


class Registry:
    SCHEMA = 2
    MODES = ("proxy", "standalone")          # persisted runtime mode (Phase-3 graduation target)

    def __init__(self, path, nodes=None, mode="proxy"):
        self.path = Path(path)
        self.nodes = nodes if nodes is not None else {}
        self.mode = mode if mode in self.MODES else "proxy"   # coerce anything unknown → proxy
        self.dirty = False                          # set on any write, cleared on save

    @classmethod
    def load(cls, path) -> "Registry":
        p = Path(path)
        if not p.exists():
            return cls(path)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("registry %s unreadable (%r) — starting empty", p, exc)
            return cls(path)
        return cls(path, data.get("nodes", {}), data.get("mode", "proxy"))   # schema-1 files lack mode → proxy

    def payload_for_mode(self, mode) -> bytes:
        """Serialize the registry AS IF its mode were ``mode``, WITHOUT mutating ``self`` — this lets
        graduation write the new mode to disk and flip the in-memory mode ONLY after the write is durable,
        so a failed/cancelled write can never leave a falsely-graduated in-memory state."""
        if mode not in self.MODES:
            raise ValueError(f"mode must be one of {list(self.MODES)}, got {mode!r}")
        snap = self.snapshot()
        snap["mode"] = mode
        return json.dumps(snap, indent=2, ensure_ascii=False).encode("utf-8")

    def serialize(self) -> bytes:
        """Build the JSON payload synchronously — cheap and atomic in the caller's
        thread (no `await`), so concurrent `observe()` cannot race a save."""
        return self.payload_for_mode(self.mode)

    def write_payload(self, payload: bytes) -> None:
        """Atomic file write — pure blocking I/O, safe to run off the event loop."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except OSError:
            Path(tmp).unlink(missing_ok=True)
            raise

    def save(self) -> None:
        """Atomically persist the registry: serialise, write a temp file,
        fsync, ``os.replace``. Single-call entry point for sync callers
        (`set_user`, tests); the autosave loop instead snapshots inside the
        event loop and offloads only the I/O to keep the loop responsive."""
        self.write_payload(self.serialize())
        self.dirty = False

    def set_mode(self, mode) -> None:
        """Set the persisted runtime mode (Phase-3 graduation). Dirties the registry so the next
        save (explicit `_persist` or the autosave loop) flushes it. Rejects an unknown mode."""
        if mode not in self.MODES:
            raise ValueError(f"mode must be one of {list(self.MODES)}, got {mode!r}")
        self.mode = mode
        self.dirty = True

    def observe(self, node, dtype, confidence, power=None, battery=None) -> bool:
        """Update one node from auto-discovery. A user-confirmed *type* is kept; a
        classifier verdict of ``"unknown"`` never overwrites a real prior type
        (so a fresh-start classifier doesn't trash an inference from the previous
        session). Only sets `dirty` when a meaningful field actually changes —
        `last_seen` updates in memory but doesn't trigger persistence on its own.
        Returns True iff a dirty-worthy field (type / power / battery / new node)
        changed — the proxy uses this to decide between a full `discovery_changed`
        refetch and a cheap state delta."""
        entry = self.nodes.setdefault(_key(node), {"first_seen": _now()})
        entry["last_seen"] = _now()
        changed = False
        if power is not None and entry.get("power") != power:
            entry["power"] = power
            self.dirty = True
            changed = True
        if battery is not None and entry.get("battery") != battery:
            entry["battery"] = battery                # last-known % survives a restart
            self.dirty = True
            changed = True
        if not entry.get("type_confirmed"):
            prior_type = entry.get("type")
            if dtype != "unknown" or prior_type in (None, "unknown"):
                if entry.get("type") != dtype or entry.get("confidence") != confidence:
                    entry["type"] = dtype
                    entry["confidence"] = confidence
                    self.dirty = True
                    changed = True
        return changed

    def set_user(self, node, *, name=None, room=None, dtype=None, ep=None) -> None:
        """Apply a user edit. Confirming a *type* freezes it against auto-discovery;
        a name or room does not (a named-but-unclassified node can still be typed).
        When ``ep`` is given, a ``name`` labels that endpoint of a multi-gang switch
        (stored under ``endpoint_names[str(ep)]``) instead of the node itself.

        A call with nothing to write (an ``ep`` with no ``name``, or all fields
        ``None``) is a no-op — it neither creates a stub entry nor dirties the file."""
        if ep is not None:
            if name is None:
                return                                # endpoint label with no name → nothing to do
        elif name is None and room is None and dtype is None:
            return                                    # pure no-op → don't create a ghost / dirty
        entry = self.nodes.setdefault(_key(node), {"first_seen": _now()})
        if ep is not None:
            entry.setdefault("endpoint_names", {})[str(ep)] = name
        elif name is not None:
            entry["name"] = name
        if room is not None:
            entry["room"] = room
        if dtype is not None:
            entry["type"] = dtype
            entry["confidence"] = "confirmed"
            entry["type_confirmed"] = True
        self.dirty = True

    def record_scene(self, node, scene_id, batch_hex) -> bool:
        """Persist the cloud's batch reaction to a function-button scene, keyed by
        ``(node, scene_id)`` (hex of the `[1e 32]` ``0x005a`` element block). Learned
        in proxy mode, replayed in standalone mode (``docs/PROTOCOL.md`` §5.7a). The
        scene id is fixed per device and a node is either a switch or a blind, so
        ``(node, scene_id)`` uniquely identifies the scene. Idempotent: returns True
        and sets ``dirty`` only when the stored batch actually changes."""
        scenes = self.nodes.setdefault(_key(node), {"first_seen": _now()}).setdefault("scenes", {})
        key = str(int(scene_id))
        if scenes.get(key) == batch_hex:
            return False
        scenes[key] = batch_hex
        self.dirty = True
        return True

    def scene_batch(self, node, scene_id) -> "str | None":
        """Return the learned ``0x005a`` hex for ``(node, scene_id)``, or None."""
        return self.nodes.get(_key(node), {}).get("scenes", {}).get(str(int(scene_id)))

    def snapshot(self) -> dict:
        """Schema-tagged copy for persistence. The node dict is a *shallow* copy:
        safe ONLY because `serialize()` runs synchronously in the event-loop
        thread — `json.dumps` finishes before any other coroutine can call
        `observe()`. If you ever call `snapshot()` from a worker thread, take a
        deep copy of the inner entries first."""
        return {"schema": self.SCHEMA, "mode": self.mode, "nodes": dict(self.nodes)}
