"""Synthetic invoice templates and parser isolation boundaries."""
from __future__ import annotations

from base64 import a85encode
from io import BytesIO
import logging
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import pypdf
from pypdf._encryption import AlgV5, PasswordType
from pypdf.constants import UserAccessPermissions
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject, TextStringObject
from pypdf.generic import _font

from hestia.budget import gmail_worker as worker
from hestia.budget.gmail_invoice import ImportedInvoice, extract_invoice, has_provider_invoice_number


_FIXTURES = Path(__file__).parent / "fixtures" / "gmail"
_FIXTURE_ENV = {"HESTIA_BUDGET_RECEIPTS_PARSER_ALLOW_SUBPROCESS": "1"}


def _pdf_text(*lines: bytes) -> bytes:
    return b"BT /F1 12 Tf 14 TL 72 720 Td " + b" T* ".join(b"(" + line + b") Tj" for line in lines) + b" ET"


def _write_pdf(writer: pypdf.PdfWriter) -> bytes:
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _add_pdf_page(writer: pypdf.PdfWriter, data: bytes | None, *, flate: bool = False) -> None:
    page = writer.add_blank_page(width=612, height=792)
    if data is None:
        return
    stream = DecodedStreamObject()
    stream.set_data(data)
    page[NameObject("/Contents")] = writer._add_object(stream.flate_encode() if flate else stream)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }),
        }),
    })


def _pdf_writer(*streams: bytes | None, flate: bool = False) -> pypdf.PdfWriter:
    writer = pypdf.PdfWriter()
    for data in streams:
        _add_pdf_page(writer, data, flate=flate)
    return writer


def _without_free_xref_entry(pdf: bytes) -> bytes:
    # The offsets still address the same objects and xref table after omitting
    # object 0. Only the first subsection's start number and count change.
    before, xref, entries = pdf.partition(b"\nxref\n")
    subsection, _, entries = entries.partition(b"\n")
    first, count = subsection.split()
    assert first == b"0" and entries.startswith(b"0000000000 65535 f \n")
    return before + xref + f"1 {int(count) - 1}\n".encode() + entries.split(b"\n", 1)[1]


class TestGmailInvoice(unittest.TestCase):
    def test_provider_templates_through_the_worker_socket(self):
        templates = {
            "respect-energy.txt": ImportedInvoice("Respect Energy", "7791011327", "RE/09/2026/042", "231.8", "PLN",
                                                  "2026-09-04", "2026-09-25"),
            "pdf_text/orange_invoice.txt": ImportedInvoice("Orange", "5260250995", "26110200000001", "88.1", "PLN",
                                                           "2026-11-14", "2026-11-30"),
            "pdf_text/play_invoice.txt": ImportedInvoice("Play", "9512120077", "F/40000001/11/26", "150.37", "PLN",
                                                         "2026-11-08", "2026-11-23"),
            "pdf_text/tmobile_invoice.txt": ImportedInvoice("T-Mobile", "5261040567", "501234567890", "59.99", "PLN",
                                                            "2026-09-09", "2026-09-23"),
        }
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "parse.sock")
            client = worker.ReceiptParserWorker(path)
            self.assertFalse(client.ready())
            sidecar_script = """
import sys
from hestia.budget.gmail_worker import serve

assert 'pypdf' not in sys.modules
sys.path.insert(0, sys.argv[2])
serve(sys.argv[1])
"""
            with subprocess.Popen(
                (sys.executable, "-B", "-c", sidecar_script, path, str(Path(pypdf.__file__).resolve().parents[1])),
                env={}, stderr=subprocess.DEVNULL,
            ) as sidecar:
                try:
                    for _ in range(50):
                        if client.ready():
                            break
                        time.sleep(0.01)
                    self.assertTrue(client.ready())
                    self.assertEqual(stat.S_IMODE(Path(path).stat().st_mode), 0o600)
                    for name, expected in templates.items():
                        with self.subTest(name=name):
                            kind = "html" if name.endswith(".html") else "text"
                            parsed = client.parse(kind, (_FIXTURES / name).read_bytes())
                            self.assertEqual(extract_invoice(parsed), expected)
                            self.assertTrue(has_provider_invoice_number(parsed))
                            self.assertFalse(has_provider_invoice_number(parsed.replace(expected.invoice_number,
                                                                                        "unexpected-format")))
                    writer = _pdf_writer(_pdf_text(
                        b"Orange", b"Numer dokumentu: FV/2026/042", b"Kwota: 12,19 EUR", b"Termin platnosci: 05.02.2026",
                    ), flate=True)
                    permissions = UserAccessPermissions.all() & ~(
                        UserAccessPermissions.EXTRACT | UserAccessPermissions.EXTRACT_TEXT_AND_GRAPHICS
                    )
                    writer.encrypt(user_password="", owner_password="synthetic owner", algorithm="AES-256",
                                   permissions_flag=permissions)
                    parsed = client.parse("pdf", _write_pdf(writer))
                    self.assertEqual(parsed, "Orange\nNumer dokumentu: FV/2026/042\nKwota: 12,19 EUR\n"
                                             "Termin platnosci: 05.02.2026")
                    self.assertEqual(extract_invoice(parsed), ImportedInvoice(
                        "Orange", None, "FV/2026/042", "12.19", "EUR", None, "2026-02-05",
                    ))
                    with self.assertRaises(worker.WorkerRejected):
                        client.parse("pdf", b"%PDF-synthetic")
                    with self.assertRaises(worker.WorkerRejected):
                        client.parse("html", b"\xff")
                finally:
                    sidecar.terminate()

class TestGmailWorker(unittest.TestCase):
    def test_pdf_page_limit_and_empty_text(self):
        pages = [None] * worker.MAX_PDF_PAGES
        self.assertEqual(worker._render(b"P" + _write_pdf(_pdf_writer(*pages))), b"+")
        self.assertEqual(worker._render(b"P" + _write_pdf(_pdf_writer(*pages, None))), b"-")
        pdf = _write_pdf(_pdf_writer(_pdf_text(b"First page"), None, _pdf_text(b"Last page")))
        self.assertEqual(worker._render(b"P" + pdf), b"+First page\nLast page")

    def test_pdf_allows_only_empty_user_passwords(self):
        for algorithm, user_password, owner_password, expected in (
            ("AES-256", "", "synthetic owner", b"+Synthetic invoice"),
            ("AES-256", "", None, b"+Synthetic invoice"),
            ("AES-128", "", None, b"+Synthetic invoice"),
            ("AES-256", "synthetic user", "synthetic owner", b"-"),
        ):
            with self.subTest(algorithm=algorithm, empty_user=not user_password, explicit_owner=owner_password is not None):
                writer = _pdf_writer(_pdf_text(b"Synthetic invoice"), flate=True)
                writer.encrypt(user_password=user_password, owner_password=owner_password, algorithm=algorithm)
                self.assertEqual(worker._render(b"P" + _write_pdf(writer)), expected)

    def test_pdf_rejects_empty_owner_password_with_nonempty_user_password(self):
        writer = _pdf_writer(_pdf_text(b"Synthetic invoice"))
        generate_values = AlgV5.generate_values

        def generate_with_empty_owner(revision, user_password, _owner_password, *args):
            # PdfWriter substitutes the user password for an empty owner password.
            return generate_values(revision, user_password, b"", *args)

        with patch.object(AlgV5, "generate_values", side_effect=generate_with_empty_owner):
            writer.encrypt(user_password="synthetic user", owner_password="", algorithm="AES-256")
        pdf = _write_pdf(writer)
        self.assertEqual(pypdf.PdfReader(BytesIO(pdf), strict=True).decrypt(b""), PasswordType.OWNER_PASSWORD)
        self.assertEqual(worker._render(b"P" + pdf), b"-")

    def test_pdf_allows_nonzero_xref_subsection_in_strict_mode(self):
        pdf = _without_free_xref_entry(_write_pdf(_pdf_writer(_pdf_text(b"Synthetic invoice"))))
        with self.assertLogs("pypdf._reader", level="WARNING") as warnings:
            reader = pypdf.PdfReader(BytesIO(pdf), strict=True)
            self.assertEqual(reader.pages[0].extract_text(), "Synthetic invoice")
        self.assertIn("Xref table not zero-indexed.", warnings.output[0])
        with self.assertNoLogs("pypdf", level="WARNING"):
            self.assertEqual(worker._render(b"P" + pdf), b"+Synthetic invoice")

    def test_pdf_allows_cff_encoding_fallback(self):
        writer = _pdf_writer(_pdf_text(b"Synthetic invoice"))
        font = writer.pages[0]["/Resources"]["/Font"]["/F1"]
        cff = DecodedStreamObject()
        cff[NameObject("/Subtype")] = NameObject("/Type1C")
        font[NameObject("/FontDescriptor")] = DictionaryObject({
            NameObject("/FontFile3"): writer._add_object(cff),
        })
        pdf = _write_pdf(writer)
        with patch.object(_font, "HAS_FONTTOOLS", False):
            with self.assertLogs("pypdf._cmap", level="WARNING") as warnings:
                reader = pypdf.PdfReader(BytesIO(pdf), strict=True)
                self.assertEqual(reader.pages[0].extract_text(), "Synthetic invoice")
            self.assertIn("fontTools is required to fully parse the encoding of a CFF Type1 font", warnings.output[0])
            with self.assertNoLogs("pypdf", level="WARNING"):
                self.assertEqual(worker._render(b"P" + pdf), b"+Synthetic invoice")

    def test_pdf_keeps_unrecognized_warning_templates_fail_closed(self):
        pdf = _write_pdf(_pdf_writer(_pdf_text(b"Synthetic invoice")))
        allowed_message = "Xref table not zero-indexed. ID numbers for objects will be corrected."
        logger = logging.getLogger("pypdf._reader")
        for log in (
            lambda: logger.warning("%s", allowed_message),
            lambda: logger.error(allowed_message),
            lambda: logging.getLogger("pypdf.filters").warning(allowed_message),
        ):
            with patch.object(pypdf.PdfReader, "read", side_effect=log), self.assertNoLogs("pypdf", level="WARNING"):
                self.assertEqual(worker._render(b"P" + pdf), b"-")

    def test_pdf_rejects_raw_and_decompressed_stream_overflow(self):
        oversized = b" " * (worker.MAX_PDF_STREAM_BYTES + 1)
        for flate in (False, True):
            with self.subTest(flate=flate):
                pdf = _write_pdf(_pdf_writer(oversized, flate=flate))
                self.assertLess(len(pdf), worker.MAX_PDF_BYTES)
                self.assertEqual(worker._render(b"P" + pdf), b"-")

        writer = _pdf_writer(oversized, flate=True)
        del writer.pages[0]["/Resources"]
        self.assertEqual(worker._render(b"P" + _write_pdf(writer)), b"-")

    def test_pdf_supports_bounded_ascii85_streams(self):
        writer = _pdf_writer(a85encode(_pdf_text(b"Synthetic text"), adobe=True, wrapcol=17))
        writer.pages[0]["/Contents"][NameObject("/Filter")] = NameObject("/ASCII85Decode")
        self.assertEqual(worker._render(b"P" + _write_pdf(writer)), b"+Synthetic text")

        writer = _pdf_writer(b"z" * (worker.MAX_PDF_STREAM_BYTES // 4 + 1) + b"~>")
        writer.pages[0]["/Contents"][NameObject("/Filter")] = NameObject("/ASCII85Decode")
        pdf = _write_pdf(writer)
        self.assertLess(len(pdf), worker.MAX_PDF_STREAM_BYTES)
        self.assertEqual(worker._render(b"P" + pdf), b"-")

    def test_pdf_rejects_aggregate_decompressed_overflow(self):
        stream = b" " * worker.MAX_PDF_STREAM_BYTES
        pdf = _write_pdf(_pdf_writer(stream, stream, flate=True))
        self.assertEqual(worker._render(b"P" + pdf), b"+")
        for flate in (False, True):
            with self.subTest(flate=flate):
                writer = _pdf_writer(stream, stream, flate=True)
                _add_pdf_page(writer, b"q Q", flate=flate)
                self.assertEqual(worker._render(b"P" + _write_pdf(writer)), b"-")

    def test_pdf_rejects_filter_output_above_stream_limit(self):
        writer = _pdf_writer(b"\x00" * worker.MAX_PDF_STREAM_BYTES)
        contents = writer.pages[0]["/Contents"]
        # pypdf's pure-Python CCITT filter adds a TIFF header to the encoded bytes.
        contents[NameObject("/Filter")] = NameObject("/CCITTFaxDecode")
        pdf = _write_pdf(writer)
        self.assertLess(len(pdf), worker.MAX_PDF_BYTES)
        self.assertEqual(worker._render(b"P" + pdf), b"-")

    def test_pdf_applies_stream_budget_to_cross_reference_streams(self):
        original = _write_pdf(_pdf_writer(None))
        incremental = pypdf.PdfWriter(BytesIO(original), incremental=True)
        incremental.add_metadata({"/Subject": "Synthetic update"})
        pdf = _write_pdf(incremental)
        self.assertEqual(worker._render(b"P" + pdf), b"+")
        with patch.object(worker, "MAX_PDF_DECOMPRESSED_BYTES", 1):
            self.assertEqual(worker._render(b"P" + pdf), b"-")

    def test_pdf_bounds_flate_trailing_whitespace(self):
        writer = _pdf_writer(_pdf_text(b"Synthetic invoice"), flate=True)
        contents = writer.pages[0]["/Contents"]
        encoded = contents._data
        max_padding = b" \t\r\n\f\0" * 5 + b"\r\n"
        for trailing, expected in (
            (b"\n", b"+Synthetic invoice"),
            (b"\r\n", b"+Synthetic invoice"),
            (max_padding, b"+Synthetic invoice"),
            (max_padding + b" ", b"-"),
            (b"\r\ntrailing bytes\n", b"-"),
            (b"\v", b"-"),
        ):
            with self.subTest(trailing=trailing):
                contents._data = encoded + trailing
                self.assertEqual(worker._render(b"P" + _write_pdf(writer)), expected)
        contents._data = encoded[:-1]
        self.assertEqual(worker._render(b"P" + _write_pdf(writer)), b"-")

    def test_pdf_rejects_external_streams(self):
        writer = _pdf_writer(_pdf_text(b"External invoice"))
        writer.pages[0]["/Contents"][NameObject("/F")] = TextStringObject("https://example.test/content")
        self.assertEqual(worker._render(b"P" + _write_pdf(writer)), b"-")

    def test_pdf_rejects_nested_page_tree_and_content(self):
        writer = _pdf_writer(_pdf_text(b"Visible invoice"))
        node = writer.pages[0].indirect_reference
        pages = writer.root_object["/Pages"]
        for _ in range(worker.MAX_PDF_RECURSION + 1):
            parent = DictionaryObject({
                NameObject("/Type"): NameObject("/Pages"),
                NameObject("/Kids"): ArrayObject([node]),
                NameObject("/Count"): NumberObject(1),
            })
            parent_ref = writer._add_object(parent)
            node.get_object()[NameObject("/Parent")] = parent_ref
            node = parent_ref
        pages[NameObject("/Kids")] = ArrayObject([node])
        node.get_object()[NameObject("/Parent")] = pages.indirect_reference
        self.assertEqual(worker._render(b"P" + _write_pdf(writer)), b"-")

        brackets = worker.MAX_PDF_RECURSION + 1
        nested = b"BT /F1 12 Tf " + b"[" * brackets + b"(Hidden)" + b"]" * brackets + b" TJ ET"
        self.assertEqual(worker._render(b"P" + _write_pdf(_pdf_writer(nested))), b"-")

    def test_pdf_rejects_malformed_input_and_text_overflow(self):
        truncated = _write_pdf(_pdf_writer(_pdf_text(b"Synthetic invoice"))).removesuffix(b"%%EOF\n")
        for data in (b"%PDF-1.7\n", b"%PDF-1.7\n%%EOF", b"%PDF-synthetic", truncated):
            with self.subTest(data=data):
                self.assertEqual(worker._render(b"P" + data), b"-")
        writer = _pdf_writer(None)
        writer.pages[0][NameObject("/Contents")] = NumberObject(1)
        self.assertEqual(worker._render(b"P" + _write_pdf(writer)), b"-")
        for lines in ((b"a" * (worker.MAX_TEXT_BYTES + 1),), (b"line",) * (worker.MAX_TEXT_LINES + 1)):
            with self.subTest(line_count=len(lines)):
                pdf = _write_pdf(_pdf_writer(_pdf_text(*lines)))
                self.assertEqual(worker._render(b"P" + pdf), b"-")

    def test_pdf_does_not_resolve_actions(self):
        writer = _pdf_writer(_pdf_text(b"Visible invoice"))
        launch = DictionaryObject({
            NameObject("/S"): NameObject("/Launch"),
            NameObject("/F"): TextStringObject("https://example.test/action"),
        })
        javascript = DictionaryObject({
            NameObject("/S"): NameObject("/JavaScript"),
            NameObject("/JS"): TextStringObject("app.alert('Hidden JavaScript')"),
        })
        writer.root_object[NameObject("/OpenAction")] = launch
        writer.pages[0][NameObject("/AA")] = DictionaryObject({NameObject("/O"): javascript})
        self.assertEqual(worker._render(b"P" + _write_pdf(writer)), b"+Visible invoice")

    def test_pdf_rejects_cyclic_forms_without_logging_their_contents(self):
        writer = _pdf_writer(b"/SyntheticForm Do")
        form = DecodedStreamObject()
        form.set_data(b"/SyntheticForm Do")
        form[NameObject("/Subtype")] = NameObject("/Form")
        xobjects = DictionaryObject({NameObject("/SyntheticForm"): writer._add_object(form)})
        resources = writer.pages[0]["/Resources"]
        resources[NameObject("/XObject")] = xobjects
        form[NameObject("/Resources")] = resources
        with self.assertNoLogs("pypdf", level="WARNING"):
            self.assertEqual(worker._render(b"P" + _write_pdf(writer)), b"-")

    def test_timed_out_client_disconnect_kills_real_parser_child(self):
        runnable = """
import os
from pathlib import Path
import sys
import time
from hestia.budget import gmail_worker as worker

def hang(_request):
    Path(sys.argv[2]).write_text(str(os.getpid()))
    time.sleep(20)
    return b'+late'

worker._render = hang
worker.serve(sys.argv[1])
"""
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "parse.sock")
            marker = Path(directory) / "child.pid"
            client = worker.ReceiptParserWorker(path)
            with subprocess.Popen((sys.executable, "-B", "-c", runnable, path, str(marker)),
                                  env={}, stderr=subprocess.DEVNULL) as sidecar:
                try:
                    for _ in range(50):
                        if client.ready():
                            break
                        time.sleep(0.01)
                    self.assertTrue(client.ready())
                    with patch.object(worker, "PARSE_TIMEOUT_SECONDS", 1.0):
                        with self.assertRaises(worker.WorkerUnavailable):
                            client.parse("text", b"synthetic slow fixture")
                        self.assertTrue(client.ready())
                    child = int(marker.read_text())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(child, 0)
                finally:
                    sidecar.terminate()

    def test_worker_suppresses_active_html_and_preserves_forward_headers(self):
        markup = b"""<div>Begin forwarded message:</div>
        <div>From: Invoice &lt;billing@example.test&gt;</div><br><br>
        <div>A<style>invisible</style><svg><template>hidden</template></svg>
        <!-- invisible --><noscript><div>none</div></noscript><iframe>network</iframe><object>plugin</object>
        <img src="cid:mail" alt="read"/><embed src="https://example.test/x">B</div>
        <p>https://example.test/x cid:local data:;base64,AAAA javascript:alert(1)</p>"""
        rendered = worker._validate_output(worker._render(b"H" + markup))
        self.assertIn("Begin forwarded message:\nFrom: Invoice <billing@example.test>\n", rendered)
        self.assertIn("A B", rendered)
        for unwanted in ("invisible", "hidden", "none", "network", "plugin", "example.test/x", "alert(1)", "base64"):
            self.assertNotIn(unwanted, rendered)
        plain = "z\u0307\r\na\u200b\x07\tB\rEND"
        self.assertEqual(worker._validate_output(worker._render(b"T" + plain.encode())), "ż\na B\nEND")

    def test_request_and_response_bounds(self):
        for kind, body in ((b"X", b"a"), (b"T", None), (b"H", b""), (b"P", b"not PDF"),
                           (b"P", b" \n%PDF-1.7"), (b"P", b"\xef\xbb\xbf%PDF-1.7"),
                           (b"H", b"a" * (worker.MAX_HTML_BYTES + 1)),
                           (b"P", b"%PDF-" + b"x" * worker.MAX_PDF_BYTES)):
            with self.subTest(kind=kind), self.assertRaises(worker.WorkerRejected):
                worker._validate_request(kind, body)
        for result in (b"???", b"+\xff", b"+\0", b"+" + b"X" * (worker.MAX_TEXT_BYTES + 1),
                       b"+" + b"\n" * worker.MAX_TEXT_LINES):
            with self.subTest(result=result[:10]), self.assertRaises(worker.WorkerUnavailable):
                worker._validate_output(result)
        for value in ("x" * (worker.MAX_TEXT_BYTES + 1), "é" * (worker.MAX_TEXT_BYTES // 2 + 1),
                      "x\n" * (worker.MAX_TEXT_LINES + 1), "x" + "\u0301" * 9):
            with self.subTest(value=value[:10]):
                self.assertEqual(worker._render(b"T" + value.encode()), b"-")
        with self.assertRaises(worker.WorkerRejected):
            worker.ReceiptParserWorker().parse("unknown", b"invalid")
        with self.assertRaises(worker.PDFUnsupported):
            worker._validate_output(b"!")

    def test_framing_and_deadline(self):
        with self.assertRaises(TimeoutError):
            worker._remaining(time.monotonic() - 1)
        reader, writer = socket.socketpair()
        deadline = time.monotonic() + 1
        with reader, writer:
            writer.sendall((0).to_bytes(4, "big"))
            with self.assertRaises(worker.WorkerRejected):
                worker._read_frame(reader, 9, deadline)
            writer.sendall((10).to_bytes(4, "big"))
            with self.assertRaises(worker.WorkerRejected):
                worker._read_frame(reader, 9, deadline)
            writer.close()
            with self.assertRaises(worker.WorkerUnavailable):
                worker._read_frame(reader, 9, deadline)
        with patch.object(worker.ReceiptParserWorker, "_exchange", return_value=b"-"):
            self.assertFalse(worker.ReceiptParserWorker().ready())

    def test_readiness_probe_times_out_when_the_socket_stays_silent(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket(socket.AF_UNIX) as listener:
            path = str(Path(directory) / "silent.sock")
            listener.bind(path)
            listener.listen(1)
            started = time.monotonic()
            self.assertFalse(worker.ReceiptParserWorker(path).ready())
            self.assertLess(time.monotonic() - started, 2)

    def test_supervisor_kills_child_on_timeout_and_peer_disconnect(self):
        listener, client = MagicMock(), MagicMock()
        reader, writer = MagicMock(), MagicMock()
        deadline = time.monotonic() + 1
        with patch.object(worker.socket, "socketpair", return_value=(reader, writer)), \
             patch.object(worker.os, "fork", return_value=1), \
             patch.object(worker, "_read_frame", side_effect=(b"Tfixture", b"+fixture")), \
             patch.object(worker, "_send_frame") as send, \
             patch.object(worker.select, "select", return_value=([reader], [], [])), \
             patch.object(worker, "_kill_and_wait") as cleanup:
            worker._serve_request(listener, client, deadline)
        send.assert_called_once_with(client, b"+fixture", deadline)
        cleanup.assert_called_once_with(1)
        for ready in ([client, reader], []):
            with patch.object(worker.socket, "socketpair", return_value=(reader, writer)), \
                 patch.object(worker.os, "fork", return_value=2), \
                 patch.object(worker, "_read_frame", return_value=b"Tfixture"), \
                 patch.object(worker, "_send_frame") as send, \
                 patch.object(worker.select, "select", return_value=(ready, [], [])), \
                 patch.object(worker.os, "kill") as kill, \
                 patch.object(worker.os, "waitpid") as wait:
                worker._serve_request(listener, client, deadline)
            send.assert_not_called()
            kill.assert_called_once_with(2, signal.SIGKILL)
            wait.assert_called_once_with(2, 0)
        with patch.object(worker.os, "kill", side_effect=ProcessLookupError), patch.object(worker.os, "waitpid") as wait:
            worker._kill_and_wait(3)
        wait.assert_called_once_with(3, 0)

    def test_supervisor_ping_and_parser_child(self):
        listener, client, reader, writer = (MagicMock() for _ in range(4))
        deadline = time.monotonic() + 1
        with patch.object(worker, "_read_frame", return_value=b"?"), patch.object(worker, "_send_frame") as send:
            worker._serve_request(listener, client, deadline)
        send.assert_called_once_with(client, b"+", deadline)
        with patch.object(worker, "_read_frame", return_value=b"Tfixture"), \
             patch.object(worker, "_send_frame") as send, \
             patch.object(worker.socket, "socketpair", return_value=(reader, writer)), \
             patch.object(worker.os, "fork", return_value=0), \
             patch.object(worker.os, "_exit", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                worker._serve_request(listener, client, deadline)
        send.assert_called_once_with(writer, b"+fixture", deadline)
        for connection in (listener, client, reader):
            connection.close.assert_called_once()

    def test_pdf_parser_child_leaves_memory_headroom_for_the_supervisor(self):
        listener, client, reader, writer = (MagicMock() for _ in range(4))
        request = b"P" + _write_pdf(_pdf_writer(_pdf_text(b"Synthetic invoice")))
        deadline = time.monotonic() + 1
        with patch.object(worker.resource, "setrlimit") as set_limit, patch.object(worker, "_send_frame") as send:
            worker._parser_child(listener, client, reader, writer, request, deadline)
        set_limit.assert_called_once_with(worker.resource.RLIMIT_AS, (192 * 1024 * 1024,) * 2)
        send.assert_called_once_with(writer, b"+Synthetic invoice", deadline)

    def test_supervisor_survives_a_rejected_client(self):
        listener, first, second = MagicMock(), MagicMock(), MagicMock()
        listener.__enter__.return_value = listener
        listener.accept.side_effect = ((first, None), (second, None), StopIteration)
        with patch.object(worker.Path, "unlink"), patch.object(worker.os, "umask"), \
             patch.object(worker.socket, "socket", return_value=listener), \
             patch.object(worker, "_serve_request", side_effect=(worker.WorkerRejected, None)) as serve:
            with self.assertRaises(StopIteration):
                worker.serve("/synthetic.sock")
        self.assertEqual([call.args[1] for call in serve.call_args_list], [first, second])

    def test_fixture_only_subprocess_without_inherited_environment(self):
        environment = _FIXTURE_ENV | {"FAKE_SECRET": "not propagated"}
        for production, configured in ((True, environment), (False, {})):
            with self.assertRaises(worker.WorkerUnavailable):
                worker.parse_fixture("orange.html", production=production, environ=configured)
        with patch.dict(os.environ, environment, clear=False):
            rendered = worker.parse_fixture("orange.html", production=False)
        self.assertIn("Orange Polska", rendered)
        self.assertNotIn("not propagated", rendered)
        for invalid in ("../orange.html", "/orange.html", "not.html", "play.txt"):
            with self.assertRaises(worker.WorkerRejected):
                worker.parse_fixture(invalid, production=False, environ=environment)
        with tempfile.TemporaryDirectory() as directory, patch.object(worker, "_FIXTURE_ROOT", Path(directory)):
            root = Path(directory)
            (root / "directory.html").mkdir()
            (root / "symlink.html").symlink_to(_FIXTURES / "orange.html")
            (root / "fake.pdf").write_bytes(b"not a PDF")
            for invalid in ("directory.html", "symlink.html", "fake.pdf"):
                with self.assertRaises(worker.WorkerRejected):
                    worker.parse_fixture(invalid, production=False, environ=environment)
        with patch.object(worker.subprocess, "Popen", side_effect=OSError):
            with self.assertRaises(worker.WorkerUnavailable):
                worker.parse_fixture("orange.html", production=False, environ=environment)

    def test_fixture_subprocess_limits_and_forced_stop(self):
        process = MagicMock()
        process.communicate.side_effect = (subprocess.TimeoutExpired("synthetic", 5), (b"", b""))
        with patch.object(worker.subprocess, "Popen", return_value=process) as spawn:
            with self.assertRaises(worker.WorkerUnavailable):
                worker.parse_fixture("orange.html", production=False, environ=_FIXTURE_ENV)
        self.assertEqual(spawn.call_args.kwargs["env"], {})
        process.kill.assert_called_once()
        process.communicate.side_effect = None
        process.communicate.return_value = (b"+ignored", b"")
        process.returncode = 1
        with patch.object(worker.subprocess, "Popen", return_value=process):
            with self.assertRaises(worker.WorkerUnavailable):
                worker.parse_fixture("orange.html", production=False, environ=_FIXTURE_ENV)
        stdout = BytesIO()
        with patch.object(worker.sys, "stdin", SimpleNamespace(buffer=BytesIO(b"<p>hello</p>"))), \
             patch.object(worker.sys, "stdout", SimpleNamespace(buffer=stdout)), \
             patch.object(worker.resource, "setrlimit") as set_limit:
            worker._fixture_child(b"H")
        self.assertEqual(stdout.getvalue(), b"+hello")
        self.assertEqual([call.args[1] for call in set_limit.call_args_list],
                         [(256 * 1024 * 1024,) * 2, (5, 5), (0, 0), (32, 32)])
