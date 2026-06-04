"""Standalone ``0x7e`` server — hestia *replaces* the Keemple cloud.

Where the proxy (`hestia.proxy`) relays to the Keemple cloud, this owns the device
session itself: it answers the handshake the gateway expects, **ACKs every state
event**, and sends periodic timestamps — so the home keeps working with no cloud,
no internet, no public IP. The reply set is built straight from the
spec (`docs/PROTOCOL.md` §3.1 + §6); it is a **hypothesis until proven against
live hardware** (the gateway may have keepalive/sequence expectations not yet
seen). The decoding tap, `State`, and the newline-JSON control port are shared
with the proxy, so command injection works identically in either mode.

Run:  python -m hestia.server   (or `HESTIA_MODE=standalone python -m hestia`)
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import time
import uuid

from . import commands
from .protocol import Deframer, Frame, build_frame, tlv
from .proxy import (ProxyConfig, ProxyRuntime, _close, _echo_command_frame, _feed_discovery,
                    _start, summarize)
from .state import tlv_value

log = logging.getLogger("hestia.server")

HEARTBEAT_SECS = 60  # the cloud sent a [64 03] timestamp ~1/min
# (type, cmd) pairs that are pure plumbing — not logged at INFO.
_NOISE = {(0x66, 0x01), (0x00, 0x00), (0x1E, 0x0A)}


def _event_node(frame: Frame) -> "bytes | None":
    return tlv_value(frame, 0x0047) if (frame.type, frame.cmd) == (0x1E, 0x09) else None


def _log_state_changes(node_b: bytes, changed: dict) -> None:
    for key, value in changed.items():
        log.info("  ~ [%#04x] %s = %s", node_b[0], key, value)


def _publish_observed_events(rt, frame: Frame, node_b: "bytes | None", changed: dict, scene) -> None:
    if _feed_discovery(rt, frame):          # identity changed → full refetch
        rt.event_bus.publish({"type": "discovery_changed"})
    elif changed and node_b:                # live value(s) → cheap cell patch
        rt.event_bus.publish({"type": "state", "node": node_b[0], "fields": changed})
    if node_b:
        event = {"type": "activity", "node": node_b[0], "ts": time.time()}
        if scene:                           # function-button scene rides the flash
            event["scene"] = scene
        rt.event_bus.publish(event)         # heatmap: row flashes (every event)


def _scene_replay(rt, node_b: "bytes | None", scene) -> "list[bytes]":
    if not (scene and node_b):
        return []
    hexbatch = rt.registry.scene_batch(node_b[0], scene["id"])
    if not hexbatch:
        return []
    try:
        # A hand-edited/corrupted registry can hold a non-string (TypeError),
        # non-hex (ValueError), or over-long batch whose 2-byte TLV length
        # overflows (OverflowError); skip the replay rather than tear down
        # the session.
        return [commands.scene_batch(rt.next_seq(), bytes.fromhex(hexbatch))]
    except (TypeError, ValueError, OverflowError):
        log.error("corrupt learned scene batch for node %#04x scene %s — "
                  "skipping replay", node_b[0], scene["id"])
        return []


def _append_automation_replay(rt, replay: "list[bytes]", node_b: "bytes | None",
                              changed: dict, scene) -> "list[bytes]":
    # Automations run in standalone too (rt.mode == "standalone"); append after
    # any scene-replay so the order is ACK -> scene-replay -> automation actions.
    if node_b and (changed or scene):
        return replay + rt.engine.on_event(rt, node_b[0], changed, scene)
    return replay


def make_session_assign() -> bytes:
    """`[64 01]` session assignment: a fresh UUID, seq init = `01`, keepalive 60 s."""
    session = str(uuid.uuid4()).encode("ascii")
    return build_frame(0x64, 0x01, tlv(0x0064, session) + tlv(0x001F, b"\x01") + tlv(0x0069, b"\x00\x3c"))


def make_timestamp() -> bytes:
    """`[64 03]` timestamp heartbeat (string + epoch), as the cloud sent ~1/min."""
    now = datetime.datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M:%S").encode("ascii")
    epoch = int(now.timestamp()).to_bytes(4, "big")
    return build_frame(
        0x64, 0x03,
        tlv(0x0066, stamp) + tlv(0x0067, b"\x02") + tlv(0x0068, epoch) + tlv(0x00D3, b"\x01"),
    )


class StandaloneSession:
    """Owns one device connection with no cloud: handshake + event ACKs + tap."""

    def __init__(self, rt, reader, writer, heartbeat_secs: float = HEARTBEAT_SECS):
        self.rt = rt
        self.reader = reader
        self.writer = writer
        self.heartbeat_secs = heartbeat_secs
        self.peer = writer.get_extra_info("peername")
        self._session_sent = False
        self.serial = None

    def react(self, frame: Frame) -> "list[bytes]":
        """The minimal replies that keep the gateway connected (PROTOCOL §3.1, §6)."""
        kind = (frame.type, frame.cmd)
        if kind == (0x66, 0x01):                  # hello -> assign a session, once
            if self._session_sent:
                return []
            self._session_sent = True
            return [make_session_assign()]
        if kind == (0x1E, 0x09):                  # state event -> ACK its 0x001f
            seq = tlv_value(frame, 0x001F)
            return [build_frame(0x1E, 0x0A, tlv(0x001F, seq))] if seq else []
        if kind == (0x67, 0x01):                  # device IP report -> ack
            return [build_frame(0x67, 0x02, tlv(0x0001, b"\x00\x00"))]
        if kind == (0x67, 0x03):                  # periodic device report -> ack
            return [build_frame(0x67, 0x04, tlv(0x0001, b"\x00\x00"))]
        return []

    def _duplicate_serial(self) -> bool:
        """True if another live session already owns our serial — the dual-homed
        gateway connects on both NICs and we keep exactly one session per serial
        (the Keemple cloud rejects the second with `ew`). First-wins for now; a
        newest-wins / liveness-based takeover is a future refinement."""
        return any(other is not self and getattr(other, "serial", None) == self.serial
                   for other in self.rt.sessions)

    def _observe(self, frame: Frame) -> "list[bytes]":
        """Tap one frame: update state, publish events, and return any frames to
        inject back to the device — namely a **scene replay**: with no cloud to run
        the scene, a learned function-button batch (§5.7a) is re-emitted here. Returns
        ``[]`` for every non-replaying frame."""
        if len(frame.body) < 4:
            return []
        if (frame.type, frame.cmd) not in _NOISE:
            log.info("%s %s", self.peer, summarize(frame))
        if not frame.checksum_ok:
            return []
        changed = self.rt.state.apply(frame)
        node_b = _event_node(frame)
        if node_b:                                   # changed is only ever non-empty here
            _log_state_changes(node_b, changed)
        scene = changed.pop("scene", None)           # a button press: an event, not state
        _publish_observed_events(self.rt, frame, node_b, changed, scene)
        replay = _scene_replay(self.rt, node_b, scene)
        return _append_automation_replay(self.rt, replay, node_b, changed, scene)

    def _drop_duplicate_registration(self, frame: Frame) -> bool:
        if (frame.type, frame.cmd) != (0x64, 0x02):   # registration
            return False
        self.serial = tlv_value(frame, 0x0002)
        if self.serial and self._duplicate_serial():
            log.warning("duplicate session for serial %r — dropping %s", self.serial, self.peer)
            return True
        return False

    async def _write_replies(self, replies: "list[bytes]") -> None:
        for reply in replies:
            self.writer.write(reply)
        if replies:
            await self.writer.drain()
            for reply in replies:                        # post-send: echo any switch/2-gang command we
                _echo_command_frame(self.rt, reply)      # injected here (a fired automation) — relays don't report

    async def _finish_run(self, heartbeat) -> None:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        await _close(self.writer)
        self.rt.sessions.remove(self)   # always present once we reach here
        log.info("- device %s", self.peer)

    async def run(self) -> None:
        log.info("+ device %s (standalone)", self.peer)
        self.rt.sessions.append(self)
        heartbeat = asyncio.create_task(self._heartbeat())
        deframer = Deframer()
        try:
            while True:
                data = await self.reader.read(4096)
                if not data:
                    break
                for body in deframer.feed(data):
                    frame = Frame(body)
                    replay = self._observe(frame)
                    if self._drop_duplicate_registration(frame):
                        return
                    # ACK the event first (the cloud's observed order), then the
                    # replayed scene batch — so the device never sees an unsolicited
                    # command ahead of its event's acknowledgement.
                    await self._write_replies(self.react(frame) + replay)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            await self._finish_run(heartbeat)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_secs)
            self.writer.write(make_timestamp())
            await self.writer.drain()

    async def inject_to_device(self, raw: bytes) -> None:
        self.writer.write(raw)
        await self.writer.drain()
        log.info("INJECT -> %s %s", self.peer, raw.hex())
        _echo_command_frame(self.rt, raw)            # post-send: echo a switch/2-gang set (control / scheduler)


def _standalone_session(rt, reader, writer, _config):
    return StandaloneSession(rt, reader, writer)


async def main() -> None:  # pragma: no cover
    from .automations import AutomationEngine
    from .proxy import (FLIPPER_ENABLED, IR_QUEUE_MAX, _autosave, _install_term_handler,
                        _ir_worker, _niania_poller, _persist, _persist_state, _persist_store,
                        _scheduler, _sensor433_poller, _shadow_sync_db, _weather_poller,
                        seed_device_state)
    from .web import start_web, stop_web
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = ProxyConfig()
    from .auth import users_path
    from .store_sql import open_audit_engine, open_stores
    registry, store = open_stores(registry_path=config.registry_path,      # HESTIA_PERSIST=sqlite → DB authoritative
                                  automations_path=config.automations_path,
                                  users_path=str(users_path()))
    rt = ProxyRuntime(registry=registry, engine=AutomationEngine(store), mode="standalone")
    seed_device_state(rt)
    _shadow_sync_db(rt)                                # Phase-2 #57: mirror JSON -> SQLite (no-op in sqlite mode)
    rt.audit_engine = open_audit_engine()              # Phase-5 #56: who-did-what audit log
    if FLIPPER_ENABLED:                                # create the IR backlog before anything can fire
        rt.ir_queue = asyncio.Queue(maxsize=IR_QUEUE_MAX)
    device_srv, control_srv = await _start(rt, config, _standalone_session)
    autosave = asyncio.create_task(_autosave(rt))
    scheduler = asyncio.create_task(_scheduler(rt))
    niania = asyncio.create_task(_niania_poller(rt))   # no-op unless HESTIA_NIANIA_* configured
    weather_task = asyncio.create_task(_weather_poller(rt))  # no-op unless HESTIA_OUTDOOR_TEMP + source=open-meteo
    sensor433_task = asyncio.create_task(_sensor433_poller(rt))  # no-op unless HESTIA_OUTDOOR_TEMP + source=local
    ir_worker = asyncio.create_task(_ir_worker(rt))    # no-op unless HESTIA_FLIPPER enabled
    loop = asyncio.get_running_loop()
    _install_term_handler(loop, asyncio.current_task())   # SIGTERM -> graceful persist (docker/systemd stop)
    log.info("hestia STANDALONE: device :%d (no cloud) | control %s:%d | web %s:%d | registry %s",
             config.listen_port, config.control_host, config.control_port,
             config.web_host, config.web_port, config.registry_path)
    try:
        web_handle = await start_web(rt, config.web_host, config.web_port)
        try:
            async with device_srv, control_srv:
                await asyncio.gather(device_srv.serve_forever(), control_srv.serve_forever())
        finally:
            rt.event_bus.close()
            await stop_web(web_handle)
    finally:
        autosave.cancel()
        scheduler.cancel()
        niania.cancel()
        weather_task.cancel()
        sensor433_task.cancel()
        ir_worker.cancel()
        await asyncio.gather(autosave, scheduler, niania, weather_task, sensor433_task, ir_worker,
                             return_exceptions=True)
        try:
            await _persist(rt)                 # share save_lock — any in-flight
        except OSError:                        # control-op write finishes first
            log.exception("final save failed at shutdown")
        try:
            await _persist_store(rt)           # symmetric: don't lose last-interval rule edits
        except OSError:
            log.exception("final automations save failed at shutdown")
        try:
            await _persist_state(rt)           # best-effort last telemetry cache
        except Exception:
            log.exception("final device-state cache save failed at shutdown")


if __name__ == "__main__":  # pragma: no cover
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):   # SIGINT / SIGTERM-driven graceful exit
        pass
