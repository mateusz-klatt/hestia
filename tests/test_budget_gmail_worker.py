"""Synthetic invoice templates and parser isolation boundaries."""
from __future__ import annotations

from io import BytesIO
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

from hestia.budget import gmail_worker as worker
from hestia.budget.gmail_invoice import ImportedInvoice, _safe_field, extract_invoice, has_provider_invoice_number


_FIXTURES = Path(__file__).parent / "fixtures" / "gmail"
_FIXTURE_ENV = {"HESTIA_BUDGET_RECEIPTS_PARSER_ALLOW_SUBPROCESS": "1"}


class TestGmailInvoice(unittest.TestCase):
    def test_provider_templates_through_the_worker_socket(self):
        templates = {
            "respect-energy.txt": ImportedInvoice("Respect Energy", "7791011327", "RE/09/2026/042", "231.8", "PLN",
                                                  "2026-09-04", "2026-09-25"),
            "orange.html": ImportedInvoice("Orange", "9370008168", "FV/2026/10/12345", "76.45", "PLN",
                                           "2026-10-06", "2026-10-19"),
            "play.txt": ImportedInvoice("Play", None, "P4/2026/002", "49.99", "PLN", "2026-10-16", "2026-10-28"),
            "t-mobile.html": ImportedInvoice("T-Mobile", "7811897358", "TM-2026-120", "1234", "PLN", "2026-11-02",
                                             "2026-11-15"),
        }
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "parse.sock")
            client = worker.ReceiptParserWorker(path)
            self.assertFalse(client.ready())
            with subprocess.Popen(
                (sys.executable, "-B", "-c", "from hestia.budget.gmail_worker import serve; import sys; serve(sys.argv[1])",
                 path),
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
                    with self.assertRaises(worker.PDFUnsupported):
                        client.parse("pdf", b"%PDF-synthetic")
                    with self.assertRaises(worker.WorkerRejected):
                        client.parse("html", b"\xff")
                finally:
                    sidecar.terminate()

    def test_every_field_rejects_pan_iban_and_pesel(self):
        fields = {
            "issuer": "Orange", "nip": "7791011327", "invoice_number": "FV/1234", "amount": "50,23",
            "currency": "PLN", "issue_date": "2026-07-01", "due_date": "2026-07-29",
        }
        template = ("{issuer}\nNIP sprzedawcy: {nip}\nNumer dokumentu: {invoice_number}\nKwota: {amount} {currency}\n"
                    "Data wystawienia: {issue_date}\nTermin płatności: {due_date}")
        self.assertTrue(extract_invoice(template.format(**fields)))
        for secret in ("4111-1111-1111-1111", "PL 61 1090 1014 0000 0712 1981 2874", "44051401458"):
            for name in fields:
                with self.subTest(name=name, secret=secret):
                    modified = fields | {name: f"Orange {secret}" if name == "issuer" else secret}
                    parsed = extract_invoice(template.format(**modified))
                    if name in ("nip", "issue_date"):
                        self.assertIsNotNone(parsed)
                        self.assertIsNone(getattr(parsed, name))
                        self.assertNotIn(secret, repr(parsed))
                    else:
                        self.assertIsNone(parsed)

    def test_text_normalization_and_ambiguous_or_missing_fields(self):
        invoice = "Orange\nNumer dokumentu: FV/2026/042\nKwota: 12,19 EUR\nTermin płatności: 05.02.2026\n"
        cases = (
            "", "Unknown supplier" + invoice[6:], invoice + "\nPlay", invoice + "Kwota: 44 PLN",
            invoice.replace("FV/2026/042", "AA12BB34CC56DD78EE90FF12"),
            invoice.replace("FV/2026/042", "4405FT1401458"),
            invoice.replace("12,19 EUR", "440514014,58 PLN"),
            invoice.replace("05.02.2026", "30.02.2026"), invoice + "Data dokumentu: 06.02.2026",
            invoice.replace("FV/2026/042", "near@example.test"), invoice.replace("Kwota: 12,19 EUR", "Kwota:"),
            invoice.replace("FV/2026/042", "\n" + "x" * 513 + "\nunrelated"),
        )
        for case in cases:
            with self.subTest(case=case):
                self.assertIsNone(extract_invoice(case))
        safe = extract_invoice(invoice.replace("Orange", "Orange invoices@example.test") + "NIP wystawcy: PL7791011327")
        self.assertEqual(safe, ImportedInvoice("Orange", "7791011327", "FV/2026/042", "12.19", "EUR", None,
                                               "2026-02-05"))
        self.assertIsNone(_safe_field("billing@example.test"))


class TestGmailWorker(unittest.TestCase):
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
        self.assertEqual(worker._render(b"P%PDF-synthetic"), b"!")

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
            (root / "fake.pdf").write_bytes(b"%PDF-synthetic")
            for invalid in ("directory.html", "symlink.html"):
                with self.assertRaises(worker.WorkerRejected):
                    worker.parse_fixture(invalid, production=False, environ=environment)
            with self.assertRaises(worker.PDFUnsupported):
                worker.parse_fixture("fake.pdf", production=False, environ=environment)
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
