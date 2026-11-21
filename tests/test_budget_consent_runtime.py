"""Managed file resolution and real acquisition hooks on synthetic databases only."""
from dataclasses import replace
from contextlib import closing
import math
import os
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

from hestia import db
from hestia.budget import config as settings, consent_runtime as module, runtime
from hestia.budget.consent_files import ConsentFiles
from tests.test_budget_jobs import RunnerCase
from tests.test_budget_store import APP


class ManagedConsentRuntimeTests(RunnerCase):
    def setUp(self):
        super().setUp()
        self.files = ConsentFiles(self.path.parent / "capsules")
        self.key = self.path.parent / "key.pem"
        self.key.write_bytes(b"SYNTHETIC_PRIVATE_KEY"); self.key.chmod(0o600)
        self.config = replace(self.config, key_file=str(self.key))
        self.loaded = self.config
        self.credentials.side_effect = settings.credentials

    def managed(self, **changes):
        ref = self.files.put("session", {"session_id": APP})
        with self.sessions.begin() as session:
            session.add(db.BudgetConsent(id="synthetic-consent", owner="owner", connection_id="bank",
                                         application_id=APP, bank_name=self.config.connections[0].bank_name, country="PL",
                                         provenance="hestia_created", status="ACTIVE", capsule_ref=ref,
                                         valid_until=self.now + 86400, generation=1, metadata_json="{}", created_at=self.now))
            row = session.get(db.BudgetConnection, "bank")
            row.credential_mode, row.consent_generation, row.active_consent_id = "managed", 1, "synthetic-consent"
            row.control_version += 1
            for name, value in changes.items():
                setattr(row, name, value)
        return ref

    def resolve(self, config=None):
        return module.resolve_config(self.config if config is None else config, path=self.path,
                                     files=self.files, clock=lambda: self.now)

    def unavailable(self):
        result = self.resolve().connections[0]
        self.assertFalse(result.credential_available)
        self.assertEqual(result.session_file, self.config.connections[0].session_file)
        with mock.patch.object(settings, "_read_secret") as secret:
            with self.assertRaises(settings.BudgetConfigError):settings.credentials(self.config, result)
        secret.assert_not_called()

    def test_external_and_fresh_legacy_schemas_never_touch_private_storage(self):
        with mock.patch.object(module, "default_files", side_effect=AssertionError("No files for external")):
            self.assertEqual(self.resolve(), self.config)
            self.assertEqual(module.resolve_config(self.config, path=self.path.parent / "missing.db"), self.config)
            legacy = self.path.parent / "legacy.db"
            with closing(sqlite3.connect(legacy)) as session, session:
                session.execute("CREATE TABLE budget_connections(id TEXT)")
            self.assertEqual(module.resolve_config(self.config, path=legacy), self.config)
        self.assertFalse(self.files.directory.exists())
        self.assertEqual(self.resolve(replace(self.config, connections=())), replace(self.config, connections=()))
        new = replace(self.config.connections[0], id="unconfigured")
        self.assertEqual(self.resolve(replace(self.config, connections=(new,))).connections[0], new)

    def test_active_exact_context_overlays_path_without_reading_secret_contents(self):
        ref = self.managed()
        before = self.rows(db.BudgetConnection)[0]
        with mock.patch.object(self.files, "get", side_effect=AssertionError("No secret contents")), \
                mock.patch.object(settings, "_read_secret", side_effect=AssertionError("No secret contents")):
            resolved = self.resolve()
        item = resolved.connections[0]
        self.assertEqual(item.session_file, self.files.path(ref, kind="session"))
        self.assertEqual((item.credential_available, item.consent_generation), (True, 1))
        self.assertEqual((resolved.application_id, resolved.key_file, item.accounts, item.policy),
                         (self.config.application_id, self.config.key_file,
                          self.config.connections[0].accounts, self.config.connections[0].policy))
        after = self.rows(db.BudgetConnection)[0]
        self.assertEqual((before.control_version, before.next_attempt, before.background_enabled),
                         (after.control_version, after.next_attempt, after.background_enabled))

    def test_closed_missing_wrong_kind_and_deleted_capsules_never_fall_back(self):
        ref = self.managed()
        for status in ("RETIRED", "UNADOPTED", "CLOSED"):
            with self.sessions.begin() as session:session.get(db.BudgetConsent, "synthetic-consent").status = status
            self.unavailable()
        with self.sessions.begin() as session:
            session.get(db.BudgetConsent, "synthetic-consent").status = "ACTIVE"
            session.get(db.BudgetConnection, "bank").active_consent_id = None
        self.unavailable()
        with self.sessions.begin() as session:session.get(db.BudgetConnection, "bank").active_consent_id = "missing"
        self.unavailable()
        wrong = self.files.put("code", {"code": "SYNTHETIC_CODE"})
        with self.sessions.begin() as session:
            session.get(db.BudgetConnection, "bank").active_consent_id = "synthetic-consent"
            session.get(db.BudgetConsent, "synthetic-consent").capsule_ref = wrong
        self.unavailable()
        with self.sessions.begin() as session:session.get(db.BudgetConsent, "synthetic-consent").capsule_ref = ref
        self.files.delete(ref)
        self.unavailable()
        with self.sessions.begin() as session:session.get(db.BudgetConnection, "bank").credential_mode = "unknown"
        self.unavailable()

    def test_identity_generation_and_active_expiry_are_strict(self):
        self.managed()
        with self.sessions.begin() as session:
            session.add(db.User(username="other", password_hash="unused", role="admin"))
            session.flush()
            session.add(db.BudgetConnection(id="other", owner="other", application_id=APP,
                                             bank_name="Other", country="PL", enabled=False, next_attempt=0, failures=0))
        for field, bad in (("owner", "other"), ("application_id", "other"), ("bank_name", "Other"),
                           ("country", "GB"), ("connection_id", "other"), ("generation", 2)):
            with self.subTest(field=field):
                with self.sessions.begin() as session:
                    row = session.get(db.BudgetConsent, "synthetic-consent"); old = getattr(row, field); setattr(row, field, bad)
                self.unavailable()
                with self.sessions.begin() as session:setattr(session.get(db.BudgetConsent, "synthetic-consent"), field, old)
        for bad in (None, "invalid", math.inf, self.now):
            with closing(sqlite3.connect(self.path)) as session, session:
                session.execute("UPDATE budget_consents SET valid_until=? WHERE id=?", (bad, "synthetic-consent"))
            self.unavailable()
        with self.sessions.begin() as session:session.get(db.BudgetConsent, "synthetic-consent").valid_until = self.now + 86400
        for field, bad in (("owner", "other"), ("application_id", "other"), ("bank_name", "Other"),
                           ("country", "GB"), ("consent_generation", -1), ("consent_generation", "invalid")):
            with self.subTest(field=field):
                with self.sessions.begin() as session:
                    row = session.get(db.BudgetConnection, "bank"); old = getattr(row, field); setattr(row, field, bad)
                self.unavailable()
                with self.sessions.begin() as session:setattr(session.get(db.BudgetConnection, "bank"), field, old)

    def test_missing_consent_table_and_partial_or_unreadable_schema_fail_closed(self):
        self.managed()
        with closing(sqlite3.connect(self.path)) as session, session:session.execute("DROP TABLE budget_consents")
        self.unavailable()
        partial = self.path.parent / "partial.db"
        with closing(sqlite3.connect(partial)) as session, session:session.execute("CREATE TABLE budget_connections(credential_mode TEXT)")
        for target in (partial, self.path.parent):
            with self.assertRaises(settings.BudgetConfigError):module.resolve_config(self.config, path=target)
        with mock.patch.object(Path, "stat", side_effect=PermissionError("SYNTHETIC_SECRET")):
            with self.assertRaises(settings.BudgetConfigError) as caught:module.resolve_config(self.config, path=self.path)
        self.assertNotIn("SYNTHETIC_SECRET", str(caught.exception))

    def test_invalid_clock_and_dataclass_fields_cannot_bypass_secret_guard(self):
        for value in (None, True, math.inf, math.nan, -1, 10 ** 1000):
            with self.subTest(value=type(value).__name__), self.assertRaises(settings.BudgetConfigError):
                module.resolve_config(self.config, path=self.path, clock=lambda: value)
        for changes in ({"credential_available": 1}, {"consent_generation": True}, {"consent_generation": -1}):
            with self.assertRaises(settings.BudgetConfigError):replace(self.config.connections[0], **changes)
        for raw in ("", "/private/custom"):
            with mock.patch.dict(os.environ, {"HESTIA_BUDGET_CONSENT_DIR": raw}):
                actual = module.default_files()
                self.assertEqual(str(actual.directory), raw or module.DEFAULT_CONSENT_DIRECTORY)

    def test_cli_resolves_actual_sqlite_path_with_consent_http_disabled(self):
        ref = self.managed()
        with mock.patch.dict(os.environ, {"HESTIA_BUDGET_CONSENT_DIR": str(self.files.directory),
                                         "HESTIA_BUDGET_CONSENT_ENABLED": "0"}):
            result = runtime.run_once(self.config, path=self.path, clock=lambda: self.now, monotonic=self.monotonic,
                                      sleeper=self.sleep, client_factory=self.factory)
        self.assertEqual(result["results"][0]["state"], "updated")
        resolved_connection = self.credentials.call_args.args[1]
        self.assertEqual(resolved_connection.session_file, self.files.path(ref, kind="session"))
        self.assertEqual(resolved_connection.consent_generation, 1)
        self.assertEqual(len(self.debits()), 4)

    def test_worker_resolves_after_full_configuration_and_does_not_poll_closed_credentials(self):
        ref = self.managed()
        self.opt_in()
        with mock.patch.dict(os.environ, {"HESTIA_BUDGET_CONSENT_DIR": str(self.files.directory)}):
            self.assertEqual(self.runner.poll(), 1)
            self.assertEqual(self.runner.step()["status"], "updated")
            self.assertEqual(self.credentials.call_args.args[1].session_file, self.files.path(ref, kind="session"))
            with self.sessions.begin() as session:
                row = session.get(db.BudgetConsent, "synthetic-consent"); row.status = "CLOSED"
            self.now += 86400
            reads = self.credentials.call_count
            self.assertEqual(self.runner.poll(), 0)
            self.assertEqual(self.credentials.call_count, reads)
