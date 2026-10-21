"""Explicit selection, secret handling, CLI and background-worker safety; no network."""
from __future__ import annotations

import asyncio
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from hestia.calendar_sync import __main__ as cli
from hestia.calendar_sync import config, runtime


def sample():
    return {"version": 1, "accounts": {
        "wife": {"provider": "caldav", "username_env": "C_USER", "password_env": "C_PASS"},
        "me": {"provider": "google", "client_id_env": "G_ID", "client_secret_env": "G_SECRET",
               "refresh_token_env": "G_TOKEN"}},
        "groups": [{"id": "family", "calendars": [
            {"id": "apple", "account": "wife", "calendar_id": "https://caldav.example/one/"},
            {"id": "google", "account": "me", "calendar_id": "family@example.com"}]}]}


class CalendarConfigTests(unittest.TestCase):
    def test_explicit_selection_roundtrip(self):
        parsed = config.parse_config(sample())
        self.assertEqual(parsed.poll_seconds, 60)
        self.assertEqual(parsed.store_groups()[0]["calendars"][0]["provider"], "caldav")
        self.assertEqual(len(parsed.groups[0].calendars), 2)
        self.assertIsNone(parsed.groups[0].calendars[0].floating_time_zone)
        self.assertIsNone(config.CalendarConfig("endpoint", "account", "calendar").floating_time_zone)
        self.assertEqual(config.parse_config({"version": 1, "accounts": {}, "groups": []}).groups, ())

    def test_disabled_load_and_valid_file(self):
        self.assertIsNone(config.load_config(environ={}))
        self.assertIsNone(config.load_config(""))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(sample()))
            self.assertEqual(config.load_config(path).groups[0].id, "family")
            self.assertEqual(config.load_config(environ={"HESTIA_CALENDAR_CONFIG": str(path)}).groups[0].id,
                             "family")
            path.write_text("not JSON SECRET")
            with self.assertRaisesRegex(config.ConfigError, "Cannot read"):
                config.load_config(path)
            with self.assertRaises(config.ConfigError):
                config.load_config(path / "missing")

    def test_invalid_top_level_and_polling(self):
        for raw in ([], {"token": "secret"}, {"version": True}, {"version": 2}):
            with self.subTest(raw=raw), self.assertRaises(config.ConfigError):
                config.parse_config(raw)
        for poll in (False, "60", 0, 9, 86401, float("nan"), float("inf")):
            raw = sample()
            raw["poll_seconds"] = poll
            with self.subTest(poll=poll), self.assertRaises(config.ConfigError):
                config.parse_config(raw)
        raw = sample()
        raw["accounts"] = []
        with self.assertRaises(config.ConfigError):
            config.parse_config(raw)
        raw = sample()
        raw["groups"] = None
        with self.assertRaises(config.ConfigError):
            config.parse_config(raw)

    def test_invalid_accounts_and_no_inline_secrets(self):
        for change in ({"provider": "ics"}, {"provider": []}, {"password": "SECRET"},
                       {"username_env": "BAD ENV"}, {"username_env": None},
                       {"username_env": " "}, {"username_env": ""},
                       {"client_id_env": "UNEXPECTED"}, {"url": "http://insecure/"}):
            raw = sample()
            raw["accounts"]["wife"].update(change)
            with self.subTest(change=change), self.assertRaises(config.ConfigError) as caught:
                config.parse_config(raw)
            self.assertNotIn("SECRET", str(caught.exception))
        raw = sample()
        raw["accounts"]["me"]["url"] = "https://example/"
        with self.assertRaises(config.ConfigError):
            config.parse_config(raw)
        for account in ("", " bad", " bad ", "../bad"):
            raw = sample()
            raw["accounts"][account] = raw["accounts"].pop("wife")
            with self.assertRaises(config.ConfigError):
                config.parse_config(raw)

    def test_https_validation_rejects_embedded_secrets_and_malformed_urls(self):
        for url in ("https://user:SECRET@example/", "https:///missing/", "https://example/?q=SECRET",
                    "https://example/#secret", "https://example:bad/", "https://[broken/", "https://example/ "):
            with self.subTest(url=url), self.assertRaises(config.ConfigError):
                config._https(url, "URL")
        self.assertEqual(config._https("https://example:443/", "URL"), "https://example:443/")

    def test_group_and_calendar_validation(self):
        cases = []
        raw = sample(); raw["groups"].append(raw["groups"][0]); cases.append(raw)
        raw = sample(); raw["groups"][0]["calendars"] = []; cases.append(raw)
        raw = sample(); raw["groups"][0]["calendars"] = {}; cases.append(raw)
        raw = sample(); raw["groups"][0]["calendars"][0]["account"] = "absent"; cases.append(raw)
        raw = sample(); raw["groups"][0]["calendars"][0]["calendar_id"] = "https://example/one"; cases.append(raw)
        raw = sample(); raw["groups"][0]["calendars"][1]["id"] = "apple"; cases.append(raw)
        raw = sample(); duplicate = dict(raw["groups"][0]["calendars"][0], id="another")
        raw["groups"].append({"id": "other", "calendars": [duplicate]}); cases.append(raw)
        raw = sample(); raw["groups"][0]["calendars"][1]["calendar_id"] = "primary"; cases.append(raw)
        raw = sample(); raw["accounts"]["other"] = raw["accounts"]["wife"].copy()
        raw["groups"].append({"id": "other", "calendars": [{"id": "other-apple", "account": "other",
            "calendar_id": raw["groups"][0]["calendars"][0]["calendar_id"]}]}); cases.append(raw)
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(config.ConfigError):
                config.parse_config(raw)

    def test_explicit_caldav_floating_zone_is_a_runtime_policy_only(self):
        raw = sample()
        baseline = config.parse_config(raw).store_groups()
        raw["groups"][0]["calendars"][0]["floating_time_zone"] = "Europe/Warsaw"
        parsed = config.parse_config(raw)
        self.assertEqual(parsed.groups[0].calendars[0].floating_time_zone, "Europe/Warsaw")
        self.assertIsNone(parsed.groups[0].calendars[1].floating_time_zone)
        self.assertEqual(parsed.store_groups(), baseline)
        raw["groups"][0]["calendars"][0]["floating_time_zone"] = "UTC"
        self.assertEqual(config.parse_config(raw).groups[0].calendars[0].floating_time_zone, "UTC")

    def test_rejects_implicit_or_invalid_floating_time_policies(self):
        for zone in (None, "", " Europe/Warsaw", "Europe/Warsaw ", False, 60, [], {},
                     "Unknown/SECRET", "/Europe/Warsaw", "../Europe/Warsaw",
                     "localtime", "posixrules", "posix/Europe/Warsaw", "right/Europe/Warsaw"):
            raw = sample()
            raw["groups"][0]["calendars"][0]["floating_time_zone"] = zone
            with self.subTest(zone=zone), self.assertRaises(config.ConfigError) as caught:
                config.parse_config(raw)
            self.assertNotIn("SECRET", str(caught.exception))
        for zone in ("Europe/Warsaw", None):
            raw = sample()
            raw["groups"][0]["calendars"][1]["floating_time_zone"] = zone
            with self.subTest(google_zone=zone), self.assertRaisesRegex(config.ConfigError, "only for CalDAV"):
                config.parse_config(raw)

    def test_floating_zone_is_passed_only_to_selected_caldav_endpoint(self):
        raw = sample()
        raw["groups"][0]["calendars"][0]["floating_time_zone"] = "Europe/Warsaw"
        parsed = config.parse_config(raw)
        providers = SimpleNamespace(GoogleProvider=mock.Mock(), CalDAVProvider=mock.Mock())
        env = {"C_USER": "private-user", "C_PASS": "SECRET", "G_ID": "id", "G_SECRET": "secret",
               "G_TOKEN": "token"}
        with mock.patch.dict(sys.modules, {"hestia.calendar_sync.providers": providers}):
            config.make_providers(parsed, environ=env)
            providers.CalDAVProvider.assert_called_once_with("https://caldav.example/one/",
                username="private-user", password="SECRET", url="https://caldav.icloud.com/",
                floating_time_zone="Europe/Warsaw")
            providers.GoogleProvider.assert_called_once_with("family@example.com", client_id="id",
                client_secret="secret", refresh_token="token")
            with self.assertRaisesRegex(config.ConfigError, "only for CalDAV"):
                config.make_provider(parsed.accounts["me"], "family@example.com",
                                     floating_time_zone="Europe/Warsaw", environ=env)
            with self.assertRaises(config.ConfigError):
                config.make_provider(parsed.accounts["wife"], "", floating_time_zone="Unknown/SECRET",
                                     environ=env)
            # Account-level discovery does not opt calendars into an interpretation policy.
            providers.CalDAVProvider.reset_mock()
            config.make_provider(parsed.accounts["wife"], "", environ=env)
            providers.CalDAVProvider.assert_called_once_with("", username="private-user",
                password="SECRET", url="https://caldav.icloud.com/")

    def test_provider_factories_resolve_env_only(self):
        parsed = config.parse_config(sample())
        providers = SimpleNamespace(GoogleProvider=mock.Mock(), CalDAVProvider=mock.Mock())
        env = {"C_USER": "private-user", "C_PASS": "SECRET", "G_ID": "id", "G_SECRET": "secret",
               "G_TOKEN": "token"}
        with mock.patch.dict(sys.modules, {"hestia.calendar_sync.providers": providers}):
            with self.assertRaisesRegex(config.ConfigError, "credential"):
                config.make_provider(parsed.accounts["wife"], "", environ={})
            made = config.make_providers(parsed, environ=env)
            self.assertEqual(set(made), {"apple", "google"})
            providers.CalDAVProvider.assert_called_once_with("https://caldav.example/one/",
                username="private-user", password="SECRET", url="https://caldav.icloud.com/")
            with mock.patch.dict(os.environ, env):
                config.make_provider(parsed.accounts["me"], "family@example.com")
        self.assertNotIn("private-user", repr(parsed))


class CalendarRuntimeTests(unittest.TestCase):
    def test_process_lock_excludes_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "events.db"
            with runtime.sync_lock(path):
                with self.assertRaises(runtime.SyncBusyError):
                    with runtime.sync_lock(path):
                        self.fail("Acquired lock twice")
            with self.assertRaises(ValueError):
                with runtime.sync_lock(path):
                    raise ValueError("failure")
            with runtime.sync_lock(path):
                pass

    def test_run_once_uses_project_database_and_explicit_providers(self):
        parsed = config.parse_config(sample())
        store = mock.Mock()
        engine = mock.Mock()
        engine.run_once.return_value = {"pulled": 2}
        stores = SimpleNamespace(SyncStore=mock.Mock(return_value=store))
        engines = SimpleNamespace(SyncEngine=mock.Mock(return_value=engine))
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict(sys.modules, {"hestia.calendar_sync.store": stores,
                                            "hestia.calendar_sync.engine": engines}), \
             mock.patch("hestia.db.init_db", return_value=(None, "sessions")), \
             mock.patch("hestia.db.db_path", return_value=Path(directory) / "events.db"), \
             mock.patch.object(runtime, "make_providers", return_value={"apple": "provider"}):
            self.assertEqual(runtime.run_once(parsed), {"pulled": 2})
            runtime.run_once(parsed, path=Path(directory) / "other.db")
        store.configure.assert_called_with(parsed.store_groups())
        engines.SyncEngine.assert_called_with(store, {"apple": "provider"})


class CalendarPollerTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_and_invalid_config_do_not_touch_database(self):
        with mock.patch.object(runtime, "load_config", return_value=None), \
             mock.patch.object(runtime, "run_once") as run:
            await runtime.poll_calendars()
            run.assert_not_called()
        with mock.patch.object(runtime, "load_config", side_effect=config.ConfigError("Invalid configuration")), \
             self.assertLogs("hestia.calendar_sync", "ERROR"):
            await runtime.poll_calendars()

    async def test_success_and_failures_retry_without_leaking_exceptions(self):
        parsed = config.parse_config(sample())
        rounds = mock.AsyncMock(side_effect=[runtime.SyncBusyError(), config.ConfigError("Missing credential"),
                                            RuntimeError("SECRET response body"), {}])
        with mock.patch.object(runtime, "load_config", return_value=parsed), \
             mock.patch.object(runtime, "_settled_thread", rounds), \
             mock.patch.object(runtime.asyncio, "sleep", mock.AsyncMock(side_effect=[None, None, None,
                                                                                   asyncio.CancelledError()])), \
             self.assertLogs("hestia.calendar_sync", "INFO") as output:
            with self.assertRaises(asyncio.CancelledError):
                await runtime.poll_calendars()
        self.assertEqual(rounds.await_count, 4)
        self.assertNotIn("SECRET", " ".join(output.output))
        # Execute the per-round closure as well; it calls the blocking shared entrypoint.
        with mock.patch.object(runtime, "run_once", return_value={}) as run:
            rounds.call_args.args[0]()
            run.assert_called_once_with(parsed)

    async def test_settled_thread_success_and_error(self):
        self.assertEqual(await runtime._settled_thread(lambda: 3), 3)
        with self.assertRaises(ValueError):
            await runtime._settled_thread(mock.Mock(side_effect=ValueError()))

    async def test_cancel_waits_for_inflight_round(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        def operation():
            started.set()
            release.wait(timeout=3)
            finished.set()
        task = asyncio.create_task(runtime._settled_thread(operation))
        await asyncio.to_thread(started.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())


class CalendarCliTests(unittest.TestCase):
    def setUp(self):
        self.config = config.parse_config(sample())
        self.store = mock.Mock()
        self.stores = SimpleNamespace(SyncStore=mock.Mock(return_value=self.store))
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = str(Path(self.directory.name) / "events.db")
        for patcher in (mock.patch.object(cli, "load_config", return_value=self.config),
                        mock.patch.dict(sys.modules, {"hestia.calendar_sync.store": self.stores}),
                        mock.patch("hestia.db.init_db", return_value=(None, "sessions")),
                        mock.patch("hestia.db.db_path", return_value=self.db)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def args(self, *values):
        return cli._parser().parse_args(list(values))

    def test_discovery_and_once(self):
        @dataclass
        class Calendar:
            id: str
            writable: bool
        provider = mock.Mock()
        provider.discover.return_value = [Calendar("family", True), {"id": "other", "writable": False}]
        with mock.patch.object(cli, "make_provider", return_value=provider):
            self.assertEqual(cli.execute(self.args("discover", "--account", "wife"))[0]["writable"], True)
            with self.assertRaises(config.ConfigError):
                cli.execute(self.args("discover", "--account", "absent"))
        with mock.patch.object(cli, "run_once", return_value={"ok": True}) as run:
            self.assertEqual(cli.execute(self.args("--db", self.db, "once")), {"ok": True})
            run.assert_called_once_with(self.config, path=self.db)

    def test_local_status_events_put_delete_and_unknown_group(self):
        self.store.status.return_value = {"pending": 2}
        self.assertEqual(cli.execute(self.args("status")), {"pending": 2})
        cli.execute(self.args("--db", self.db, "events", "family", "--include-deleted"))
        self.store.list_events.assert_called_once_with("family", include_deleted=True)
        with self.assertRaises(config.ConfigError):
            cli.execute(self.args("events", "absent"))
        with mock.patch.object(sys, "stdin", io.StringIO('{"summary":"Family"}')):
            cli.execute(self.args("put", "family", "--file", "-", "--event", "event-id", "--expected-revision", "2"))
        self.store.save_local.assert_called_once_with("family", {"summary": "Family"}, event_id="event-id",
                                                     expected_revision=2)
        cli.execute(self.args("delete", "family", "event-id", "--expected-revision", "3"))
        self.store.delete_local.assert_called_once_with("family", "event-id", expected_revision=3)

    def test_payload_read_errors_and_required_configuration(self):
        path = Path(self.directory.name) / "event.json"
        path.write_text('{"summary":"OK"}')
        self.assertEqual(cli._read_payload(path), {"summary": "OK"})
        path.write_text("INVALID SECRET")
        with self.assertRaisesRegex(config.ConfigError, "Cannot read event"):
            cli._read_payload(path)
        with self.assertRaises(config.ConfigError):
            cli._read_payload(path / "missing")
        with mock.patch.object(cli, "load_config", return_value=None), self.assertRaises(config.ConfigError):
            cli.execute(self.args("status"))

    def test_main_json_and_sanitized_errors(self):
        with mock.patch.object(cli, "execute", return_value={"ok": True}), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["status"]), 0)
            self.assertEqual(json.loads(output.getvalue()), {"ok": True})
        for error, status in ((config.ConfigError("Invalid config"), 2),
                              (runtime.SyncBusyError("Already running"), 2),
                              (RuntimeError("SECRET token"), 1)):
            with mock.patch.object(cli, "execute", side_effect=error), redirect_stderr(io.StringIO()) as output:
                self.assertEqual(cli.main(["status"]), status)
                self.assertNotIn("SECRET", output.getvalue())

    def test_module_entrypoint(self):
        # Execute __main__ as Python does, verifying the actual exit status.
        with mock.patch.object(config, "load_config", return_value=None), \
             mock.patch.object(sys, "argv", ["calendar_sync", "status"]), \
             redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
            runpy.run_module("hestia.calendar_sync", run_name="__main__")
        self.assertEqual(result.exception.code, 2)
