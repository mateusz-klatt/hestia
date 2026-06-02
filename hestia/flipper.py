"""Minimal **Flipper Zero RPC** client — transmit an infrared signal saved on the Flipper's SD card,
over USB-serial (a ``pyserial`` transport), with **no cloud**.

The Flipper CLI ``ir tx RAW`` is unusable for a real signal: a hard ~256-byte CLI input-line buffer
truncates anything past ~53 samples (our LG A/C frame is 57). So this drives the device through the
Flipper **RPC** (protobuf over the *same* serial port, entered with the CLI command
``start_rpc_session``) — the path qFlipper / the mobile app use, with no command-length limit:

    AppStart{name:"Infrared", args:"RPC"}  ->  AppLoadFile{path}  ->  AppButtonPressRelease{args:button}
    ->  AppExit

``args`` to ``AppStart`` MUST be the literal ``"RPC"`` (not the file): it puts the on-device Infrared
app into RPC-controllable mode. The button name is a signal name inside the loaded ``.ir`` file; the
press makes the app transmit that signal.

Only the handful of protobuf messages this needs are hand-encoded (no protobuf runtime): the wire
format is varint-tagged fields, and every ``PB.Main`` frame is prefixed by its varint byte-length.
Field numbers are from ``flipperdevices/flipperzero-protobuf`` (``flipper.proto`` / ``application.proto``).

Scope: transmit one named signal via ``transmit_ir``; every serial / RPC / protocol failure surfaces
as a single ``FlipperError`` (the caller never sees a raw OSError). Blocking — run it off the event
loop (``run_in_executor``), like the project's other blocking device reads.
"""
from __future__ import annotations

import time

import serial

DEFAULT_DEVICE = "/dev/ttyACM0"

# PB.Main oneof content field numbers (flipper.proto)
_APP_START = 16
_APP_EXIT = 47
_APP_LOAD_FILE = 48
_APP_BUTTON_PRESS = 49
_APP_BUTTON_RELEASE = 50
_APP_BUTTON_PRESS_RELEASE = 75
# PB.Main scalar fields
_F_COMMAND_ID = 1
_F_COMMAND_STATUS = 2
# CommandStatus codes worth naming in an error message (flipper.proto CommandStatus enum)
_STATUS_NAMES = {
    1: "ERROR", 2: "ERROR_DECODE", 3: "ERROR_NOT_IMPLEMENTED", 4: "ERROR_BUSY",
    16: "ERROR_APP_CANT_START", 17: "ERROR_APP_SYSTEM_LOCKED",
    21: "ERROR_APP_NOT_RUNNING", 22: "ERROR_APP_CMD_ERROR",
}

_IR_APP = "Infrared"
_RPC_ARG = "RPC"


class FlipperError(Exception):
    """Any Flipper serial / RPC / protocol failure (the caller never sees a raw exception)."""


# --- protobuf wire encoding (only what we emit) -----------------------------------------------

def _varint(n: int) -> bytes:
    if n < 0:
        raise FlipperError("varint must be non-negative")
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _f_varint(field: int, value: int) -> bytes:
    return _tag(field, 0) + _varint(value)


def _f_bytes(field: int, data: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(data)) + data


def _f_str(field: int, s: str) -> bytes:
    return _f_bytes(field, s.encode("utf-8"))


def _main_frame(command_id: int, content_field: int, content: bytes) -> bytes:
    """A length-delimited ``PB.Main``: ``command_id`` + the oneof content sub-message, prefixed by the
    varint byte-length of the whole ``Main`` (how the Flipper frames RPC messages on the wire)."""
    main = _f_varint(_F_COMMAND_ID, command_id) + _f_bytes(content_field, content)
    return _varint(len(main)) + main


# --- protobuf wire decoding (only what we read back: a Main's command_id + command_status) ------

def _read_varint(buf, pos: int):
    """Decode a base-128 varint at ``buf[pos:]`` -> ``(value, new_pos)``. ``None`` value (not raising)
    if the buffer ends mid-varint, so the frame reader can wait for more bytes."""
    result = shift = 0
    while True:
        if pos >= len(buf):
            return None, pos
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            raise FlipperError("varint too long")


def _target_scalar(frame: bytes, pos: int, target: int, wire: int):
    if wire != 0:
        raise FlipperError(f"field {target} has non-varint wire type {wire}")
    value, _ = _read_varint(frame, pos)
    if value is None:
        raise FlipperError("truncated scalar in frame")
    return value


def _skip_varint_field(frame: bytes, pos: int) -> int:
    value, pos = _read_varint(frame, pos)
    if value is None:
        raise FlipperError("truncated varint field")
    return pos


def _skip_bytes_field(frame: bytes, pos: int) -> int:
    length, pos = _read_varint(frame, pos)
    if length is None:
        raise FlipperError("truncated length-delimited field")
    return pos + length


def _skip_field(frame: bytes, pos: int, wire: int) -> int:
    if wire == 0:
        return _skip_varint_field(frame, pos)
    if wire == 2:
        return _skip_bytes_field(frame, pos)
    if wire == 5:
        return pos + 4
    if wire == 1:
        return pos + 8
    raise FlipperError(f"unsupported wire type {wire}")


def _scalar(frame: bytes, target: int):
    """Return the varint value of scalar field ``target`` in a decoded ``PB.Main`` body, or ``None`` when
    the field is genuinely ABSENT — a well-formed proto3 default (e.g. an OK ``command_status`` is omitted
    on the wire). A garbled frame is REJECTED rather than read as a default: the target appearing with a
    non-varint wire type, a truncated value, or ANY skipped field that overruns the frame raises
    ``FlipperError`` (so a corrupt reply can never be silently accepted as status 0)."""
    pos = 0
    n = len(frame)
    while pos < n:
        tag, pos = _read_varint(frame, pos)
        if tag is None:
            raise FlipperError("truncated tag in frame")
        field, wire = tag >> 3, tag & 0x7
        if field == target:
            return _target_scalar(frame, pos, target, wire)
        pos = _skip_field(frame, pos, wire)
        if pos > n:                                           # a skipped field's length ran past the frame
            raise FlipperError("field runs past end of frame")
    return None


def _take_frame(buf: bytearray):
    """Pop one complete length-delimited ``PB.Main`` from the front of ``buf`` -> the Main body bytes,
    consuming the length prefix + body. Returns ``None`` (leaving ``buf`` intact) when a full frame is
    not yet buffered."""
    length, pos = _read_varint(buf, 0)
    if length is None:                                        # length prefix incomplete
        return None
    if len(buf) - pos < length:                               # body not fully arrived yet
        return None
    body = bytes(buf[pos:pos + length])
    del buf[:pos + length]
    return body


# --- serial transport (raw 8N1 over the USB-CDC CLI port) -------------------------------------

class SerialTransport:
    """Raw 8N1 access to the Flipper's USB-CDC serial port via ``pyserial``. ``write`` is bounded by a
    write timeout; ``read`` waits up to ``timeout`` for the first byte then drains what is buffered
    (possibly empty). Injectable: ``transmit_ir`` takes a factory so tests pass a fake."""

    def __init__(self, device: str = DEFAULT_DEVICE):
        # pyserial's defaults are raw 8N1 with no flow control — exactly the old termios setup.
        try:
            self._ser = serial.Serial(device, baudrate=115200, timeout=0, write_timeout=5.0)
        except (serial.SerialException, OSError) as exc:
            raise FlipperError(f"cannot open {device}: {exc}") from None

    def write(self, data: bytes, timeout: float = 5.0) -> None:
        """Write all of ``data``, BOUNDED by ``timeout``: a wedged write raises ``FlipperError``
        rather than spin forever (it would otherwise jam the sole IR worker and stall shutdown)."""
        try:
            self._ser.write_timeout = timeout
            self._ser.write(data)
            self._ser.flush()
        except serial.SerialTimeoutException:
            raise FlipperError("serial write timed out") from None
        except (serial.SerialException, OSError) as exc:
            raise FlipperError(f"serial write failed: {exc}") from None

    def read(self, timeout: float) -> bytes:
        """Wait up to ``timeout`` for the first byte, then return it plus whatever else is buffered."""
        try:
            self._ser.timeout = max(0.0, timeout)
            first = self._ser.read(1)
            if not first:
                return b""
            return first + self._ser.read(self._ser.in_waiting)
        except (serial.SerialException, OSError) as exc:
            raise FlipperError(f"serial read failed: {exc}") from None

    def close(self) -> None:
        try:
            self._ser.close()
        except (serial.SerialException, OSError):
            pass


# --- the public operation ---------------------------------------------------------------------

def _drain(transport, seconds: float) -> None:
    """Read and discard for ``seconds`` — used to swallow CLI echo/output (e.g. after
    ``start_rpc_session``) so it can't be mis-parsed as a binary RPC frame."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        transport.read(min(0.1, end - time.monotonic()))


def _await_status(transport, command_id: int, timeout: float) -> int:
    """Read length-delimited ``PB.Main`` frames until the response for ``command_id`` arrives; return its
    ``command_status``. Unsolicited frames (e.g. app-state notifications) are skipped. Raises on timeout."""
    buf = bytearray()
    end = time.monotonic() + timeout
    while True:
        frame = _take_frame(buf)
        if frame is not None:
            if _scalar(frame, _F_COMMAND_ID) == command_id:
                return _scalar(frame, _F_COMMAND_STATUS) or 0
            continue                                          # someone else's / unsolicited frame
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise FlipperError(f"timeout awaiting response to command {command_id}")
        chunk = transport.read(min(0.2, remaining))
        if chunk:
            buf += chunk


def transmit_ir(ir_file: str, button: str, *, device: str = DEFAULT_DEVICE,
                transport_factory=SerialTransport, press_release: bool = True,
                timeout: float = 2.5) -> None:
    """Transmit the signal named ``button`` from the saved ``.ir`` file ``ir_file`` on the Flipper's SD
    card. Enters RPC mode, runs AppStart→AppLoadFile→press→AppExit, and verifies every step's status.
    Raises ``FlipperError`` on any non-OK status, timeout, or I/O failure. Returns ``None`` on success."""
    transport = transport_factory(device)
    cid = 0

    def step(content_field: int, content: bytes) -> None:
        nonlocal cid
        cid += 1
        transport.write(_main_frame(cid, content_field, content))
        status = _await_status(transport, cid, timeout)
        if status != 0:
            raise FlipperError(
                f"RPC command (field {content_field}) failed: "
                f"{_STATUS_NAMES.get(status, f'status {status}')}")

    try:
        transport.write(b"\x03")                              # Ctrl-C: abort any half-typed CLI line
        _drain(transport, 0.3)
        transport.write(b"loader close\r")                    # ensure no app holds the RPC session
        _drain(transport, 1.0)
        transport.write(b"start_rpc_session\r")               # switch the port to binary protobuf
        _drain(transport, 0.8)                                # swallow the command echo
        step(_APP_START, _f_str(1, _IR_APP) + _f_str(2, _RPC_ARG))
        step(_APP_LOAD_FILE, _f_str(1, ir_file))
        if press_release:
            step(_APP_BUTTON_PRESS_RELEASE, _f_str(1, button))
        else:
            step(_APP_BUTTON_PRESS, _f_str(1, button))
            step(_APP_BUTTON_RELEASE, b"")
        step(_APP_EXIT, b"")
    finally:
        transport.close()
