"""Exercise Gmail configuration and credential file boundaries with fake values."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

from hestia.budget import config, gmail_config

REFRESH_TOKEN = "synthetic-refresh"
MAILBOX = "invoices@example.test"
OAUTH_KEY = "synthetic-oauth"
OAUTH_SECRET = "synthetic-secret"
HMAC_KEY = b"fake-dedicated-hmac-key-for-receipts"


class GmailSettingsTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.settings = directory / "gmail.json"
        self.refresh = directory / "gmail.refresh"
        self.key = directory / "gmail.key"
        self.refresh.write_bytes(REFRESH_TOKEN.encode() + b"\r\n")
        self.key.write_bytes(HMAC_KEY)
        self.refresh.chmod(0o400)
        self.key.chmod(0o600)
        self.env = {
            gmail_config.CONFIG_FILE: str(self.settings), gmail_config.HMAC_FILE: str(self.key),
            "SYNTHETIC_CLIENT_ID": OAUTH_KEY, "SYNTHETIC_CLIENT_SECRET": OAUTH_SECRET,
            "SYNTHETIC_REFRESH_FILE": str(self.refresh),
        }
        self.data = {"owner": "alice", "owner_mailbox": " INVOICES@example.test ",
                     "client_id_env": "SYNTHETIC_CLIENT_ID", "client_secret_env": "SYNTHETIC_CLIENT_SECRET",
                     "refresh_token_env": "SYNTHETIC_REFRESH_FILE", "sender_allowlist": ["@Vendor.example.test"],
                     "spouse_forward_senders": ["spouse@example.test"]}
        self.settings.write_text(json.dumps(self.data), encoding="utf-8")
        self.settings.chmod(0o600)

    def load(self, **changes):
        self.settings.write_text(json.dumps(self.data | changes), encoding="utf-8")
        return gmail_config.load_settings(environ=self.env)

    def assert_invalid(self, environ=None, **options):
        with self.assertRaises(config.BudgetConfigError) as error:
            gmail_config.load_settings(environ=self.env if environ is None else environ, **options)
        self.assertEqual(str(error.exception), "Invalid Gmail receipt configuration")

    def test_explicit_credentials_and_defaults(self):
        with mock.patch.object(config, "_read_secret", wraps=config._read_secret) as read_secret:
            self.assertIsNone(gmail_config.load_settings(environ={"HESTIA_GOOGLE_REFRESH_TOKEN": "synthetic-calendar"}))
            read_secret.assert_not_called()
            settings = self.load()
            self.assertEqual([call.args[0] for call in read_secret.call_args_list],
                             [str(self.settings), str(self.refresh), str(self.key)])
        self.assertEqual(settings.owner, "alice")
        self.assertEqual(settings.owner_mailbox, MAILBOX)
        self.assertEqual(settings.credentials.client_id, OAUTH_KEY)
        self.assertEqual(settings.credentials.client_secret, OAUTH_SECRET)
        self.assertEqual(settings.credentials.refresh_token, REFRESH_TOKEN)
        self.assertEqual(settings.hmac_secret, HMAC_KEY)
        self.assertEqual(settings.sender_allowlist, ("@vendor.example.test",))
        self.assertEqual(settings.spouse_forward_senders, ("spouse@example.test",))
        self.assertEqual(settings.known_merchant_domains, ("vendor.example.test",))
        self.assertTrue(settings.require_known_forwarded_merchant)
        self.assertEqual((settings.label_name, settings.backfill_days, settings.backfill_max_messages,
                          settings.sync_interval_seconds), (None, 365, 500, 3600))
        self.assertNotIn(REFRESH_TOKEN, repr(settings))

        custom = self.load(backfill_days=1, backfill_max_messages=10, sync_interval_seconds=7200,
                           require_known_forwarded_merchant=False, known_merchant_domains=["other.example.test"],
                           label_name="Faktury")
        self.assertEqual((custom.label_name, custom.backfill_days, custom.backfill_max_messages,
                          custom.sync_interval_seconds), ("Faktury", 1, 10, 7200))
        self.assertFalse(custom.require_known_forwarded_merchant)
        self.assertEqual(custom.known_merchant_domains, ("other.example.test", "vendor.example.test"))

    def test_strict_json_and_config_bounds(self):
        kept = {key: value for key, value in self.data.items() if key != "owner"}
        for content in (
            b'{"owner":"alice","owner":"bob"}', b'{"value":NaN}', b"x" * 16385, b"[" * 2000 + b"]" * 2000,
            b"[]", json.dumps(kept).encode(), json.dumps(self.data | {"unexpected": True}).encode(),
        ):
            with self.subTest(length=len(content)):
                self.settings.write_bytes(content)
                self.assert_invalid()
        self.settings.unlink()
        self.assert_invalid()
        os.mkfifo(self.settings)
        self.assert_invalid()

    def test_reject_invalid_owner_rule_or_policy(self):
        for invalid in (
            {"owner_mailbox": "invalid"}, {"owner": "\n"}, {"sender_allowlist": ["@gmail.com"]},
            {"spouse_forward_senders": ["@example.test"]}, {"require_known_forwarded_merchant": "yes"},
            {"label_name": "\n"}, {"label_name": "\ud800"}, {"owner": "\ud800"},
            {"backfill_days": False}, {"backfill_days": 366}, {"backfill_max_messages": 0},
            {"sync_interval_seconds": 3599}, {"refresh_token_env": "lower-case"}, {"client_id_env": 123},
        ):
            with self.subTest(invalid=invalid):
                self.settings.write_text(json.dumps(self.data | invalid), encoding="utf-8")
                self.assert_invalid()

    def test_reject_calendar_fallback_and_invalid_oauth_values(self):
        missing = self.env | {"HESTIA_GOOGLE_REFRESH_TOKEN": "synthetic-calendar"}
        del missing["SYNTHETIC_REFRESH_FILE"]
        self.assert_invalid(missing)
        self.assert_invalid(self.env | {"SYNTHETIC_REFRESH_FILE": REFRESH_TOKEN})
        for value in ("", "f" * 16385, "contains\nnewline", "nön-ascii"):
            with self.subTest(length=len(value)):
                self.assert_invalid(self.env | {"SYNTHETIC_CLIENT_ID": value})
        self.refresh.chmod(0o600)
        self.refresh.write_bytes(b"\xff")
        self.assert_invalid()

    def test_credential_files_are_private_and_hmac_material_is_distinct(self):
        self.refresh.chmod(0o644)
        self.assert_invalid()
        self.refresh.chmod(0o400)
        alias = self.refresh.with_name("alias.refresh")
        alias.symlink_to(self.refresh)
        self.assert_invalid(self.env | {"SYNTHETIC_REFRESH_FILE": str(alias)})
        self.key.write_bytes(b"short")
        self.assert_invalid()
        self.key.write_bytes(HMAC_KEY)
        self.assert_invalid(session_secret=HMAC_KEY)
        self.refresh.chmod(0o600)
        self.refresh.write_bytes(HMAC_KEY + b"\n")
        self.assert_invalid()
        self.key.write_bytes(HMAC_KEY + b"\n")
        self.assert_invalid()

    def test_json_config_requires_runtime_owner_and_private_mode(self):
        for mode in (0o644, 0o640, 0o660, 0o700):
            with self.subTest(mode=oct(mode)):
                self.settings.chmod(mode)
                self.assert_invalid()
        for mode in (0o400, 0o600):
            with self.subTest(mode=oct(mode)):
                self.settings.chmod(mode)
                self.assertEqual(gmail_config.load_settings(environ=self.env).owner, "alice")
        wrong_uid = os.geteuid() + 1
        with mock.patch.object(config.os, "geteuid", return_value=wrong_uid):
            self.assert_invalid()
        alias = self.settings.with_name("alias.json")
        alias.symlink_to(self.settings)
        self.assert_invalid(self.env | {gmail_config.CONFIG_FILE: str(alias)})
