"""Budget configuration stays opt-in and resolves only private, bounded files."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from hestia.budget import config


APPLICATION = "12345678-1234-4234-8234-123456789abc"
SESSION = "12345678-5678-4234-8234-123456789abc"


def sample():
    return {"version": 1, "application_id": APPLICATION, "key_file": "/private/budget-key.pem",
            "connections": [{"id": "personal-ing", "owner": "alice", "bank_name": "ING Bank Śląski",
                             "country": "PL", "session_file": "/private/budget-session.json",
                             "accounts": ["explicit-account-hash"]}]}


class BudgetConfigurationTests(unittest.TestCase):
    def assert_invalid(self, raw):
        with self.assertRaises(config.BudgetConfigError) as caught:
            config.parse_config(raw)
        self.assertNotIn("SECRET", str(caught.exception))

    def test_config_is_immutable_and_does_not_discover_or_read_files(self):
        parsed = config.parse_config(sample())
        self.assertEqual(parsed.application_id, APPLICATION)
        self.assertEqual(parsed.connections[0].accounts, ("explicit-account-hash",))
        self.assertEqual(parsed.connections[0].bank_name, "ING Bank Śląski")
        with self.assertRaises(FrozenInstanceError):
            parsed.key_file = "/different"
        with self.assertRaises(FrozenInstanceError):
            parsed.connections[0].owner = "bob"
        raw = sample(); raw["connections"][0]["accounts"] = []
        self.assertEqual(config.parse_config(raw).connections[0].accounts, ())
        raw["connections"] = []
        self.assertEqual(config.parse_config(raw).connections, ())

    def test_disabled_loading_and_explicit_path_precedence(self):
        self.assertIsNone(config.load_config(environ={}))
        self.assertIsNone(config.load_config("", environ={"HESTIA_BUDGET_CONFIG": "/not-used"}))
        self.assertIsNone(config.load_config(environ={"HESTIA_BUDGET_CONFIG": ""}))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(config.load_config())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "budget.json"
            path.write_text(json.dumps(sample()))
            self.assertEqual(config.load_config(path).application_id, APPLICATION)
            self.assertEqual(config.load_config(environ={"HESTIA_BUDGET_CONFIG": str(path)}).application_id, APPLICATION)
            self.assertEqual(config.load_config(path, environ={"HESTIA_BUDGET_CONFIG": "/not-used"}).application_id, APPLICATION)

    def test_invalid_or_unreadable_json_never_echoes_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "SECRET-config.json"
            for content in ("SECRET malformed", '{"version":1,"version":2}', '\xff'):
                path.write_bytes(content.encode("latin-1"))
                with self.assertRaisesRegex(config.BudgetConfigError, "Cannot read") as caught:
                    config.load_config(path)
                self.assertNotIn("SECRET", str(caught.exception))
            with self.assertRaises(config.BudgetConfigError):
                config.load_config(path / "missing")
            with self.assertRaises(config.BudgetConfigError):
                config.load_config([])
            with self.assertRaises(config.BudgetConfigError):
                config.load_config("bad\x00SECRET")

    def test_top_level_and_version_are_strict(self):
        for raw in ([], None, {}, {**sample(), "token": "SECRET"},
                    {key: value for key, value in sample().items() if key != "connections"}):
            self.assert_invalid(raw)
        for version in (True, "1", 0, 2, None):
            self.assert_invalid({**sample(), "version": version})
        for connections in (None, {}, "SECRET"):
            self.assert_invalid({**sample(), "connections": connections})

    def test_application_uuid_and_private_paths_are_explicit(self):
        for application in (None, 1, "SECRET", APPLICATION.upper(), APPLICATION.replace("-", ""),
                            " " + APPLICATION, "{" + APPLICATION + "}"):
            self.assert_invalid({**sample(), "application_id": application})
        for path in (None, 1, "", " ", "relative/SECRET", "file:///private/SECRET", "/bad\x00SECRET", "/" + "x" * 4096):
            self.assert_invalid({**sample(), "key_file": path})
            raw = sample(); raw["connections"][0]["session_file"] = path
            self.assert_invalid(raw)

    def test_connection_identity_and_metadata_are_strict(self):
        for item in (None, {}, [], {**sample()["connections"][0], "session_id": "SECRET"}):
            self.assert_invalid({**sample(), "connections": [item]})
        for identifier in (None, [], "", "../SECRET", "has space", "_leading", "x" * 129):
            raw = sample(); raw["connections"][0]["id"] = identifier
            self.assert_invalid(raw)
        raw = sample(); raw["connections"].append(deepcopy(raw["connections"][0]))
        self.assert_invalid(raw)
        for field in ("owner", "bank_name"):
            for value in (None, {}, "", " ", "SECRET\nline", "SECRET\x7f", "x" * 129):
                raw = sample(); raw["connections"][0][field] = value
                self.assert_invalid(raw)
        for country in (None, "pl", "POL", "P1", " PL", "", "ÅÅ"):
            raw = sample(); raw["connections"][0]["country"] = country
            self.assert_invalid(raw)

    def test_accounts_require_an_explicit_list_and_reject_duplicate_hashes(self):
        for accounts in (None, {}, "SECRET", [None], [""], [" "], ["SECRET\n"], ["x" * 1025], ["same", "same"]):
            raw = sample(); raw["connections"][0]["accounts"] = accounts
            self.assert_invalid(raw)
        raw = sample(); raw["connections"][0]["accounts"] = ["opaque+/hash=", "different-hash"]
        self.assertEqual(config.parse_config(raw).connections[0].accounts, ("opaque+/hash=", "different-hash"))
        other = deepcopy(raw["connections"][0]); other["id"] = "other"; other["owner"] = "bob"
        raw["connections"].append(other)
        self.assertEqual(len(config.parse_config(raw).connections), 2)


class BudgetCredentialTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.key = Path(self.directory.name) / "fake-key.pem"
        self.session = Path(self.directory.name) / "fake-session.json"
        self.key.write_bytes(b"FAKE private key; no live secret")
        self.session.write_text(json.dumps({"session_id": SESSION}))
        self.key.chmod(0o600); self.session.chmod(0o600)
        raw = sample(); raw["key_file"] = str(self.key); raw["connections"][0]["session_file"] = str(self.session)
        self.config = config.parse_config(raw)
        self.connection = self.config.connections[0]

    def read(self):
        return config.credentials(self.config, self.connection)

    def test_reads_only_fake_regular_files_and_leaves_pem_validation_to_client(self):
        self.assertEqual(self.read(), (b"FAKE private key; no live secret", SESSION))
        self.key.chmod(0o400); self.session.chmod(0o400)
        self.assertEqual(self.read()[1], SESSION)
        self.key.chmod(0o600); self.key.write_bytes(b"")
        self.assertEqual(self.read()[0], b"")

    def test_symlink_missing_and_non_regular_files_are_rejected(self):
        link = Path(self.directory.name) / "symlink.pem"
        link.symlink_to(self.key)
        for path in (link, self.key.parent, self.key.parent / "missing"):
            changed = config.BudgetConfig(APPLICATION, str(path), self.config.connections)
            with self.assertRaisesRegex(config.BudgetConfigError, "Cannot safely read"):
                config.credentials(changed, self.connection)

    def test_permission_and_owner_failures_never_read_secret_content(self):
        for permission in (0o644, 0o640, 0o604, 0o700, 0o200, 0o000):
            self.key.chmod(permission)
            with self.assertRaises(config.BudgetConfigError):
                self.read()
        self.key.chmod(0o600)
        with mock.patch.object(config.os, "geteuid", return_value=os.geteuid() + 1), \
             mock.patch.object(config.os, "read") as read:
            with self.assertRaises(config.BudgetConfigError):
                self.read()
            read.assert_not_called()

    def test_size_limit_applies_to_key_and_session_including_exact_boundary(self):
        self.key.write_bytes(b"x" * 65536)
        self.assertEqual(len(self.read()[0]), 65536)
        self.key.write_bytes(b"x" * 65537)
        with self.assertRaises(config.BudgetConfigError):
            self.read()
        self.key.write_bytes(b"FAKE")
        body = json.dumps({"session_id": SESSION}).encode()
        self.session.write_bytes(body + b" " * (65536 - len(body)))
        self.assertEqual(self.read(), (b"FAKE", SESSION))
        self.session.write_bytes(b"x" * 65537)
        with self.assertRaises(config.BudgetConfigError):
            self.read()

    def test_file_growth_after_stat_is_bounded_and_rejected(self):
        original_stat = os.fstat
        def grow_after_stat(descriptor):
            info = original_stat(descriptor)
            self.key.write_bytes(b"x" * 65537)
            return info
        with mock.patch.object(config.os, "fstat", side_effect=grow_after_stat):
            with self.assertRaises(config.BudgetConfigError):
                self.read()

    def test_descriptor_is_closed_after_read_or_stat_failure_and_errors_are_sanitized(self):
        for operation in ("read", "fstat"):
            with mock.patch.object(config.os, operation, side_effect=OSError("SECRET path and content")), \
                 mock.patch.object(config.os, "close", wraps=os.close) as close:
                with self.assertRaises(config.BudgetConfigError) as caught:
                    self.read()
                self.assertNotIn("SECRET", str(caught.exception))
                close.assert_called_once()

    def test_session_file_requires_exact_object_and_canonical_uuid(self):
        for value in (None, [], {}, {"session_id": SESSION, "token": "SECRET"},
                      {"session_id": None}, {"session_id": "SECRET"}, {"session_id": SESSION.upper()}):
            self.session.write_text(json.dumps(value))
            with self.assertRaises(config.BudgetConfigError) as caught:
                self.read()
            self.assertNotIn("SECRET", str(caught.exception))
        for content in (b"", b"SECRET malformed", b"\xff", b'{"session_id":"a","session_id":"b"}'):
            self.session.write_bytes(content)
            with self.assertRaisesRegex(config.BudgetConfigError, "Invalid budget session"):
                self.read()


if __name__ == "__main__":
    unittest.main()
