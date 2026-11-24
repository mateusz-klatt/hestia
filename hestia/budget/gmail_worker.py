"""Length-framed client and isolated parser for in-memory Gmail documents.

The sidecar image runs this module directly. Only the PDF renderer imports
pypdf. Hestia uses the client for all live input.
"""
from __future__ import annotations

from html.parser import HTMLParser
from io import BytesIO
import logging
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
import zlib


PARSER_SOCKET = "/run/hestia-receipt-parser/worker.sock"
PARSE_TIMEOUT_SECONDS = 5.0
_READY_TIMEOUT_SECONDS = 1.0
MAX_HTML_BYTES = 1024 * 1024
MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_PDF_PAGES = 10
MAX_PDF_STREAM_BYTES = 4 * 1024 * 1024
MAX_PDF_DECOMPRESSED_BYTES = 8 * 1024 * 1024
MAX_PDF_RECURSION = 64
_PDF_CHILD_ADDRESS_SPACE = 192 * 1024 * 1024
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
_PDF_BENIGN_WARNINGS = frozenset({
    ("pypdf._reader", "Xref table not zero-indexed. ID numbers for objects will be corrected."),
    ("pypdf._cmap", "fontTools is required to fully parse the encoding of a CFF Type1 font in font "
     "dictionary %(ft)s, but is not installed. Consider installing fontTools if you encounter encoding problems."),
})


class WorkerUnavailable(RuntimeError):
    """Retryable parser transport or deadline failure."""


class WorkerRejected(ValueError):
    """Terminal malformed or oversized document."""


class PDFUnsupported(WorkerRejected):
    """The parser reported an unsupported PDF."""


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

    def _exchange(self, request: bytes, *, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
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
            return self._exchange(b"?", timeout=_READY_TIMEOUT_SECONDS) == b"+"
        except WorkerUnavailable:
            return False

    def parse(self, kind: str, payload: bytes) -> str:
        code = _KINDS.get(kind, b"")
        _validate_request(code, payload)
        return _validate_output(self._exchange(code + payload, timeout=PARSE_TIMEOUT_SECONDS))


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


def _pdf_text(payload: bytes) -> bytes:
    # These hooks are scoped to a single render in the disposable child. They
    # cover streams pypdf reads before it exposes any pages (xref and objects).
    from pypdf import PasswordType, PdfReader, apply_configuration, filters
    from pypdf._encryption import AlgV4, AlgV5
    from pypdf.generic import StreamObject

    class RejectPDF(BaseException):
        """Abort pypdf recovery paths that catch Exception and return partial text."""

    class RejectPDFWarning(logging.Handler):
        def emit(self, record):
            # Match pinned pypdf templates without formatting document data.
            if record.levelno == logging.WARNING and (record.name, record.msg) in _PDF_BENIGN_WARNINGS:
                return
            raise RejectPDF

    class EmptyUserPasswordReader(PdfReader):
        def _handle_encryption(self, password):
            super()._handle_encryption(b"")
            encryption = self._encryption
            if encryption._password_type == PasswordType.USER_PASSWORD:
                return

            # pypdf tries the owner credential first. If that matches, require
            # the empty user credential to independently unlock the same key.
            values = encryption.values
            if encryption.V <= 4:
                user_key = AlgV4.verify_user_password(
                    b"", encryption.R, encryption.Length, values.O, values.U,
                    encryption.P, encryption.id1_entry, encryption.EncryptMetadata,
                )
            else:
                user_key = AlgV5.verify_user_password(encryption.R, b"", values.U, values.UE)
            if user_key != encryption._key:
                raise RejectPDF

    initialize_stream = StreamObject.initialize_from_dictionary
    decode_stream = filters.decode_stream_data
    decompress = filters.decompress
    decode_ascii85 = filters.ASCII85Decode.decode
    total = 0

    def remaining_stream_bytes() -> int:
        return min(MAX_PDF_STREAM_BYTES, MAX_PDF_DECOMPRESSED_BYTES - total)

    def charge_stream(data: bytes) -> None:
        nonlocal total
        total += len(data)
        if len(data) > MAX_PDF_STREAM_BYTES or total > MAX_PDF_DECOMPRESSED_BYTES:
            raise RejectPDF

    def read_stream(data):
        if "/F" in data:
            raise RejectPDF
        if "/Filter" not in data:
            charge_stream(data["__streamdata__"])
        return initialize_stream(data)

    def read_filtered_stream(stream):
        # EncodedStreamObject caches this result, so rereads do not spend budget.
        limit = max(1, remaining_stream_bytes())
        with apply_configuration(
            lzw_maximum_output_length=limit,
            run_length_maximum_output_length=limit,
            brotli_maximum_output_length=limit,
        ):
            data = decode_stream(stream)
        charge_stream(data)
        return data

    def strict_flate(data):
        # pypdf's default Flate recovery can accept a truncated zlib member.
        decoder = zlib.decompressobj()
        limit = remaining_stream_bytes()
        result = decoder.decompress(data, limit + 1)
        if len(result) > limit or not decoder.eof or decoder.unused_data:
            raise RejectPDF
        return result

    def bounded_ascii85(data):
        # The decoder has no output cap. Each 'z' encodes four zero bytes.
        digits = data.translate(None, b"\0\t\n\f\r ").removeprefix(b"<~").removesuffix(b"~>")
        zero_groups = digits.count(b"z")
        if 4 * zero_groups + 4 * (len(digits) - zero_groups) // 5 > remaining_stream_bytes():
            raise RejectPDF
        return decode_ascii85(data)

    logger = logging.getLogger("pypdf")
    handlers, level, propagate = logger.handlers, logger.level, logger.propagate
    recursion_limit = sys.getrecursionlimit()
    depth, frame = 0, sys._getframe()
    while frame is not None:
        depth += 1
        frame = frame.f_back

    logger.handlers = [RejectPDFWarning(logging.WARNING)]
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    StreamObject.initialize_from_dictionary = staticmethod(read_stream)
    filters.decode_stream_data = read_filtered_stream
    filters.decompress = strict_flate
    filters.ASCII85Decode.decode = staticmethod(bounded_ascii85)
    try:
        with apply_configuration(
            maximum_declared_stream_length=MAX_PDF_STREAM_BYTES,
            array_based_stream_maximum_output_length=MAX_PDF_DECOMPRESSED_BYTES,
            stream_decoding_work_maximum_length=3 * MAX_PDF_STREAM_BYTES,
            page_tree_maximum_depth=MAX_PDF_RECURSION,
            page_tree_maximum_entries=MAX_PDF_PAGES * MAX_PDF_RECURSION,
            jbig2dec_binary=None,
            disable_legacy_handling=True,
        ):
            # The page tree is traversed iteratively. The interpreter limit
            # separately bounds recursive object parsing and form extraction.
            sys.setrecursionlimit(depth + MAX_PDF_RECURSION)
            reader = EmptyUserPasswordReader(BytesIO(payload), strict=True)
            if len(reader.pages) > MAX_PDF_PAGES:
                raise RejectPDF
            pages = []
            characters = 0
            for page in reader.pages:
                # pypdf's text extractor skips /Contents when resources are absent.
                page.get_contents()
                page_text = page.extract_text().strip()
                characters += len(page_text)
                if characters > MAX_TEXT_BYTES:
                    raise RejectPDF
                if page_text:
                    pages.append(page_text)
    except (Exception, RejectPDF):
        raise WorkerRejected from None
    finally:
        sys.setrecursionlimit(recursion_limit)
        filters.ASCII85Decode.decode = staticmethod(decode_ascii85)
        filters.decompress = decompress
        filters.decode_stream_data = decode_stream
        StreamObject.initialize_from_dictionary = staticmethod(initialize_stream)
        logger.handlers, logger.propagate = handlers, propagate
        logger.setLevel(level)
    return _bounded_text("\n".join(pages), html=False)


def _render(request: bytes) -> bytes:
    """Run only in the sidecar child or the bounded fixture subprocess."""
    kind, payload = request[:1], request[1:]
    try:
        _validate_request(kind, payload)
        if kind == b"P":
            return b"+" + _pdf_text(payload)
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
        if request.startswith(b"P"):
            # Leave headroom for the supervisor under the 256 MiB cgroup cap.
            resource.setrlimit(resource.RLIMIT_AS, (_PDF_CHILD_ADDRESS_SPACE,) * 2)
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
