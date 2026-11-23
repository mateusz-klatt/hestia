"""Length-framed client and isolated parser for in-memory Gmail documents.

The sidecar image runs this standard-library-only module directly. Hestia uses
the client and never invokes the renderer in its own process for live input.
"""
from __future__ import annotations

from html.parser import HTMLParser
import os
from pathlib import Path
import re
import resource
import select
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata


PARSER_SOCKET = "/run/hestia-receipt-parser/worker.sock"
PARSE_TIMEOUT_SECONDS = 5.0
MAX_HTML_BYTES = 1024 * 1024
MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_TEXT_BYTES = 32 * 1024
MAX_TEXT_LINES = 500
_MAX_REQUEST_BYTES = 11 * 1024 * 1024 // 2
_MAX_RESPONSE_BYTES = 64 * 1024
_KINDS = {"html": b"H", "text": b"T", "pdf": b"P"}
_LIMITS = {b"H": MAX_HTML_BYTES, b"T": MAX_HTML_BYTES, b"P": MAX_PDF_BYTES}
_HIDDEN = frozenset({"head", "script", "style", "noscript", "template", "iframe", "object", "svg"})
_BLOCKS = frozenset({"div", "p", "table", "tr", "td", "th", "li", "ul", "ol", "article", "section",
                     "hr", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6", "dt", "dd"})
_CONTROL_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})
_REMOTE = re.compile(r"(?i)\b(?:https?:|cid:|javascript:|data:[a-z0-9/+.-]{0,128}[;,])\S*")
_PARSER_WORKER_SEMAPHORE = threading.Semaphore(1)
_FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "gmail"


class WorkerUnavailable(RuntimeError):
    """Retryable parser transport or deadline failure."""


class WorkerRejected(ValueError):
    """Terminal malformed or oversized document."""


class PDFUnsupported(WorkerRejected):
    """PDF template extraction is pending."""


def _remaining(deadline: float) -> float:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError
    return seconds


def _send_frame(connection: socket.socket, body: bytes, deadline: float) -> None:
    connection.settimeout(_remaining(deadline))
    connection.sendall(len(body).to_bytes(4, "big") + body)


def _read_exactly(connection: socket.socket, size: int, deadline: float) -> bytes:
    body = bytearray()
    while len(body) < size:
        connection.settimeout(_remaining(deadline))
        chunk = connection.recv(size - len(body))
        if not chunk:
            raise WorkerUnavailable
        body.extend(chunk)
    return bytes(body)


def _read_frame(connection: socket.socket, limit: int, deadline: float) -> bytes:
    size = int.from_bytes(_read_exactly(connection, 4, deadline), "big")
    if not 1 <= size <= limit:
        raise WorkerRejected
    return _read_exactly(connection, size, deadline)


def _validate_request(kind: bytes, payload: bytes) -> None:
    if kind not in _LIMITS or not isinstance(payload, bytes) or not 0 < len(payload) <= _LIMITS[kind]:
        raise WorkerRejected
    if kind == b"P" and not payload.startswith(b"%PDF-"):
        raise WorkerRejected


def _validate_output(response: bytes) -> str:
    if response == b"!":
        raise PDFUnsupported
    if response == b"-":
        raise WorkerRejected
    if not response.startswith(b"+") or len(response) > MAX_TEXT_BYTES + 1:
        raise WorkerUnavailable
    try:
        text = response[1:].decode("utf-8")
    except UnicodeDecodeError:
        raise WorkerUnavailable from None
    if text.count("\n") >= MAX_TEXT_LINES or any(
        char != "\n" and unicodedata.category(char) in _CONTROL_CATEGORIES for char in text
    ):
        raise WorkerUnavailable
    return text


class ReceiptParserWorker:
    """Live parsing always uses the isolated Unix socket service."""

    def __init__(self, socket_path: str = PARSER_SOCKET):
        self.socket_path = socket_path

    def _exchange(self, request: bytes) -> bytes:
        deadline = time.monotonic() + PARSE_TIMEOUT_SECONDS
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(_remaining(deadline))
                connection.connect(self.socket_path)
                _send_frame(connection, request, deadline)
                response = _read_frame(connection, _MAX_RESPONSE_BYTES, deadline)
                _remaining(deadline)
                return response
        except (OSError, WorkerRejected):
            raise WorkerUnavailable from None

    def ready(self) -> bool:
        try:
            return self._exchange(b"?") == b"+"
        except WorkerUnavailable:
            return False

    def parse(self, kind: str, payload: bytes) -> str:
        code = _KINDS.get(kind, b"")
        _validate_request(code, payload)
        return _validate_output(self._exchange(code + payload))


class _InvoiceHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = []
        self.fragments = []

    def _boundary(self):
        if self.fragments and not self.fragments[-1].endswith("\n"):
            self.fragments.append("\n")

    def handle_starttag(self, tag, attrs):
        if tag in _HIDDEN:
            self.hidden.append(tag)
        elif not self.hidden:
            if tag == "br":
                self.fragments.append("\n")
            elif tag in _BLOCKS:
                self._boundary()

    def handle_endtag(self, tag):
        if self.hidden:
            if self.hidden[-1] == tag:
                self.hidden.pop()
        elif tag in _BLOCKS:
            self._boundary()

    def handle_data(self, data):
        if self.hidden:
            return
        if "\n" in data and data.isspace():
            # Source indentation around HTML tags is ordinary browser whitespace,
            # unlike a structural newline or an explicit pair of <br> elements.
            if self.fragments and not self.fragments[-1][-1].isspace():
                self.fragments.append(" ")
            return
        self.fragments.append(data)


def _bounded_text(raw: str, *, html: bool) -> bytes:
    text = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ").strip()
    if len(text) > MAX_TEXT_BYTES or text.count("\n") >= MAX_TEXT_LINES:
        raise WorkerRejected
    if html:
        text = _REMOTE.sub("", text)
    visible = []
    combining_run = 0
    for char in text:
        if char != "\n" and unicodedata.category(char) in _CONTROL_CATEGORIES:
            continue
        combining_run = combining_run + 1 if unicodedata.combining(char) else 0
        if combining_run > 8:
            raise WorkerRejected
        visible.append(char)
    normalized = unicodedata.normalize("NFC", "".join(visible))
    result = "\n".join(line.strip() for line in normalized.split("\n")).strip().encode("utf-8")
    if len(result) > MAX_TEXT_BYTES:
        raise WorkerRejected
    return result


def _render(request: bytes) -> bytes:
    """Run only in the sidecar child or the bounded fixture subprocess."""
    kind, payload = request[:1], request[1:]
    try:
        _validate_request(kind, payload)
        if kind == b"P":
            # TODO(v1d-b): bounded pypdf extraction (pages, streams and recursion).
            return b"!"
        text = payload.decode("utf-8")
        if kind == b"H":
            parser = _InvoiceHTML()
            parser.feed(text)
            parser.close()
            text = "".join(parser.fragments)
        return b"+" + _bounded_text(text, html=kind == b"H")
    except (WorkerRejected, UnicodeDecodeError):
        return b"-"


def _parser_child(listener: socket.socket, client: socket.socket, parent: socket.socket,
                  writer: socket.socket, request: bytes, deadline: float) -> None:
    listener.close()
    client.close()
    parent.close()
    with writer:
        _send_frame(writer, _render(request), deadline)


def _kill_and_wait(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    os.waitpid(pid, 0)


def _serve_request(listener: socket.socket, client: socket.socket, deadline: float) -> None:
    request = _read_frame(client, _MAX_REQUEST_BYTES, deadline)
    if request == b"?":
        _send_frame(client, b"+", deadline)
        return
    reader, writer = socket.socketpair()
    with reader, writer:
        pid = os.fork()
        if pid == 0:
            try:
                _parser_child(listener, client, reader, writer, request, deadline)
            finally:
                os._exit(0)
        writer.close()
        try:
            readable, _, _ = select.select((client, reader), (), (), _remaining(deadline))
            # An additional byte or EOF from the client cancels the request.
            if client in readable or reader not in readable:
                return
            response = _read_frame(reader, _MAX_RESPONSE_BYTES, deadline)
            _send_frame(client, response, deadline)
        finally:
            _kill_and_wait(pid)


def serve(socket_path: str = PARSER_SOCKET) -> None:
    """Start the serial supervisor in the confined container."""
    Path(socket_path).unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        # The socket volume is shared only with Hestia, running as the same UID.
        os.umask(0o177)
        listener.bind(socket_path)
        listener.listen(1)
        while True:
            client, _ = listener.accept()
            with client:
                try:
                    _serve_request(listener, client, time.monotonic() + PARSE_TIMEOUT_SECONDS)
                except (OSError, WorkerRejected, WorkerUnavailable):
                    # No access log or exceptions containing message material.
                    pass


def _read_fixture(name: str, kind: bytes) -> bytes:
    try:
        directory = os.open(_FIXTURE_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise WorkerRejected
                with os.fdopen(descriptor, "rb", closefd=False) as source:
                    return source.read(_LIMITS[kind] + 1)
            finally:
                os.close(descriptor)
        finally:
            os.close(directory)
    except OSError:
        raise WorkerRejected from None


def parse_fixture(name: str, *, production: bool = True, environ=None) -> str:
    """Explicit non-production helper for server-selected HTML/PDF fixtures."""
    env = os.environ if environ is None else environ
    if production or env.get("HESTIA_BUDGET_RECEIPTS_PARSER_ALLOW_SUBPROCESS") != "1":
        raise WorkerUnavailable
    path = Path(name)
    kind = {".html": b"H", ".pdf": b"P"}.get(path.suffix, b"")
    if path.name != name or not kind:
        raise WorkerRejected
    payload = _read_fixture(name, kind)
    _validate_request(kind, payload)
    with _PARSER_WORKER_SEMAPHORE, tempfile.TemporaryDirectory() as cwd:
        deadline = time.monotonic() + PARSE_TIMEOUT_SECONDS
        try:
            process = subprocess.Popen(
                (sys.executable, "-I", "-B", os.path.abspath(__file__), "--fixture", kind.decode("ascii")),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env={}, cwd=cwd,
            )
        except OSError:
            raise WorkerUnavailable from None
        with process:
            try:
                response, _ = process.communicate(payload, timeout=_remaining(deadline))
                _remaining(deadline)
            except (subprocess.TimeoutExpired, TimeoutError):
                process.kill()
                process.communicate()
                raise WorkerUnavailable from None
        if process.returncode != 0:
            raise WorkerUnavailable
        return _validate_output(response)


def _fixture_child(kind: bytes) -> None:
    for limit, value in ((resource.RLIMIT_AS, 256 * 1024 * 1024), (resource.RLIMIT_CPU, 5),
                         (resource.RLIMIT_FSIZE, 0), (resource.RLIMIT_NOFILE, 32)):
        resource.setrlimit(limit, (value, value))
    sys.stdout.buffer.write(_render(kind + sys.stdin.buffer.read(_LIMITS[kind] + 1)))


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe"]:
        raise SystemExit(0 if ReceiptParserWorker().ready() else 1)
    if len(sys.argv) == 3 and sys.argv[1] == "--fixture":
        _fixture_child(sys.argv[2].encode("ascii"))
    else:
        serve()
