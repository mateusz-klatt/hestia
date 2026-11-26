"""Unit tests for appointments configuration, feature flag, and secret-file boundaries."""
from __future__ import annotations

from datetime import date, datetime, time, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

from hestia.appointments import (
    CMP_BASE_URL,
    CONFIG_FILE,
    FLAG,
    WARSAW_TZ,
    AppointmentsConfigError,
    BookedAppointment,
    CmpCredentials,
    PollPolicy,
    WatchCriteria,
    is_enabled,
    load_settings,
)
from hestia.appointments import config as appt_config

SYNTHETIC_TOKEN = "synthetic-cmp-bearer-token-value"
SYNTHETIC_HMAC = b"synthetic-appointments-hmac-key-32bytes!"


class AppointmentsConfigTests(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.cfg_path = directory / "appointments.json"
        self.token_path = directory / "cmp.token"
        self.hmac_path = directory / "appointments.hmac"
        self.token_path.write_bytes(SYNTHETIC_TOKEN.encode("ascii") + b"\r\n")
        self.hmac_path.write_bytes(SYNTHETIC_HMAC)
        self.token_path.chmod(0o400)
        self.hmac_path.chmod(0o400)
        self.env = {
            FLAG: "1",
            CONFIG_FILE: str(self.cfg_path),
            "SYNTHETIC_CMP_TOKEN_FILE": str(self.token_path),
            "SYNTHETIC_APPT_HMAC_FILE": str(self.hmac_path),
        }
        self.base_data: dict[str, object] = {
            "owner": "example-owner",
            "watches": [
                {
                    "watch_id": "watch-1",
                    "provider": "cmp",
                    "specialty_id": "spec-01",
                    "date_from": "2026-12-01",
                    "date_to": "2026-12-31",
                }
            ],
        }
        self.cfg_path.write_text(json.dumps(self.base_data), encoding="utf-8")
        self.cfg_path.chmod(0o400)

    def write_cfg(self, payload: object) -> None:
        self.cfg_path.chmod(0o600)
        self.cfg_path.write_text(json.dumps(payload), encoding="utf-8")
        self.cfg_path.chmod(0o400)

    def assert_invalid_load(self, *, environ=None, session_secret=b"", require_enabled=True) -> None:
        with self.assertRaises(AppointmentsConfigError) as ctx:
            load_settings(
                environ=self.env if environ is None else environ,
                session_secret=session_secret,
                require_enabled=require_enabled,
            )
        self.assertEqual(str(ctx.exception), "Invalid appointments configuration")

    def test_feature_flag_defaults_off_and_skips_secret_reads(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(is_enabled())
            self.assertIsNone(load_settings())
        self.assertFalse(is_enabled(environ={}))
        self.assertFalse(is_enabled(environ={FLAG: "0"}))
        self.assertFalse(is_enabled(environ={FLAG: "true"}))
        self.assertTrue(is_enabled(environ={FLAG: "1"}))
        with mock.patch.object(appt_config, "_read_secret", wraps=appt_config._read_secret) as read_secret:
            self.assertIsNone(load_settings(environ={CONFIG_FILE: str(self.cfg_path)}))
            self.assertIsNone(load_settings(environ={FLAG: "0", CONFIG_FILE: str(self.cfg_path)}))
            self.assertIsNone(load_settings(environ={FLAG: "1"}))
            self.assertIsNone(load_settings(environ={FLAG: "1", CONFIG_FILE: ""}))
            read_secret.assert_not_called()

    def test_load_minimal_and_full_settings(self) -> None:
        settings = load_settings(environ=self.env)
        self.assertIsNotNone(settings)
        assert settings is not None
        self.assertEqual(settings.owner, "example-owner")
        self.assertEqual(settings.cmp_base_url, CMP_BASE_URL)
        self.assertEqual(settings.cmp_credentials, CmpCredentials("none", None))
        self.assertEqual(settings.poll_policy, PollPolicy())
        self.assertIsNone(settings.hmac_secret)
        self.assertEqual(len(settings.watches), 1)
        self.assertEqual(settings.watches[0].watch_id, "watch-1")

        # Pre-enable dry-run validation works when require_enabled=False even with flag OFF.
        dry_run = load_settings(environ={CONFIG_FILE: str(self.cfg_path)}, require_enabled=False)
        self.assertEqual(dry_run, settings)

        full_data = {
            "owner": "example-owner",
            "cmp_base_url": "https://ic-api.cmp.med.pl:443/api/v1/",
            "auth_mode": "bearer",
            "auth_token_env": "SYNTHETIC_CMP_TOKEN_FILE",
            "hmac_key_env": "SYNTHETIC_APPT_HMAC_FILE",
            "poll_policy": {
                "min_interval_seconds": 600,
                "max_interval_seconds": 1200,
                "max_requests_per_run": 4,
                "max_retries": 2,
                "base_backoff_seconds": 3,
                "max_backoff_seconds": 45.5,
                "min_request_spacing_seconds": 0.5,
            },
            "watches": [
                {
                    "watch_id": "cmp-checkup-1",
                    "provider": "cmp",
                    "label": "Checkup A",
                    "specialty_id": "spec-01",
                    "doctor_ids": ["doc-101", "doc-102"],
                    "location_ids": ["loc-01"],
                    "service_id": "srv-10",
                    "date_from": "2026-12-01",
                    "date_to": "2026-12-31",
                    "time_from": "08:00",
                    "time_to": "17:00",
                    "booked_slot": {
                        "start": "2026-12-20T14:00:00",
                        "doctor_id": "doc-101",
                        "location_id": "loc-01",
                        "min_improvement_minutes": 60,
                        "require_same_doctor": True,
                        "require_same_location": True,
                    },
                    "only_better_than_booked": True,
                }
            ],
        }
        self.write_cfg(full_data)
        loaded = load_settings(environ=self.env, session_secret=b"different-session-secret-32bytes!")
        assert loaded is not None
        self.assertEqual(loaded.cmp_base_url, "https://ic-api.cmp.med.pl:443/api/v1")
        self.assertEqual(loaded.cmp_credentials.auth_mode, "bearer")
        self.assertEqual(loaded.cmp_credentials.bearer_token, SYNTHETIC_TOKEN)
        self.assertEqual(loaded.hmac_secret, SYNTHETIC_HMAC)
        self.assertNotIn(SYNTHETIC_TOKEN, repr(loaded))
        self.assertNotIn(SYNTHETIC_TOKEN, repr(loaded.cmp_credentials))
        watch = loaded.watches[0]
        self.assertEqual(watch.label, "Checkup A")
        self.assertEqual(watch.service_id, "srv-10")
        self.assertEqual(watch.time_from, time(8, 0))
        self.assertEqual(watch.time_to, time(17, 0))
        assert watch.booked_slot is not None
        self.assertEqual(watch.booked_slot.start, datetime(2026, 12, 20, 14, 0, tzinfo=WARSAW_TZ))
        self.assertTrue(watch.only_better_than_booked)

    def test_poll_policy_validation_and_next_delay(self) -> None:
        policy = PollPolicy(min_interval_seconds=600, max_interval_seconds=900)
        self.assertEqual(policy.next_poll_delay(jitter=lambda: 0.0), 600.0)
        self.assertEqual(policy.next_poll_delay(jitter=lambda: 0.5), 750.0)
        self.assertEqual(policy.next_poll_delay(jitter=lambda: 1.0), 900.0)
        self.assertEqual(policy.next_poll_delay(jitter=lambda: -1.0), 600.0)
        self.assertEqual(policy.next_poll_delay(jitter=lambda: 2.0), 900.0)

        for kwargs in (
            {"min_interval_seconds": 299},
            {"min_interval_seconds": False},
            {"min_interval_seconds": 900, "max_interval_seconds": 600},
            {"max_requests_per_run": 0},
            {"max_requests_per_run": 21},
            {"max_retries": -1},
            {"max_retries": 6},
            {"base_backoff_seconds": 0.5},
            {"base_backoff_seconds": float("nan")},
            {"base_backoff_seconds": True},
            {"base_backoff_seconds": 10.0, "max_backoff_seconds": 5.0},
            {"min_request_spacing_seconds": -0.1},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(AppointmentsConfigError):
                    PollPolicy(**kwargs)

    def test_booked_appointment_and_watch_criteria_direct_validation(self) -> None:
        aware_utc = datetime(2026, 12, 20, 13, 0, tzinfo=timezone.utc)
        booked = BookedAppointment(start=aware_utc, doctor_id="doc-1", location_id="loc-1")
        self.assertEqual(booked.start, datetime(2026, 12, 20, 14, 0, tzinfo=WARSAW_TZ))

        for kwargs in (
            {"start": datetime(2026, 12, 20, 14, 0)},  # naive
            {"start": "2026-12-20T14:00:00Z"},  # wrong type
            {"start": aware_utc, "doctor_id": "bad id!"},
            {"start": aware_utc, "location_id": ""},
            {"start": aware_utc, "min_improvement_minutes": -1},
            {"start": aware_utc, "min_improvement_minutes": 10081},
            {"start": aware_utc, "require_same_doctor": "yes"},
            {"start": aware_utc, "require_same_location": 1},
            {"start": aware_utc, "require_same_doctor": True},
            {"start": aware_utc, "require_same_location": True},
        ):
            with self.subTest(booked_kwargs=kwargs):
                with self.assertRaises(AppointmentsConfigError):
                    BookedAppointment(**kwargs)

        valid_watch = {
            "watch_id": "w1",
            "provider": "cmp",
            "date_from": date(2026, 12, 1),
            "date_to": date(2026, 12, 10),
            "specialty_id": "spec-01",
        }
        self.assertEqual(WatchCriteria(**valid_watch).provider, "cmp")
        # Doctor-only watch (without specialty_id) is valid.
        doctor_only = {k: v for k, v in valid_watch.items() if k != "specialty_id"} | {"doctor_ids": ("doc-1",)}
        self.assertEqual(WatchCriteria(**doctor_only).doctor_ids, ("doc-1",))

        for overrides in (
            {"watch_id": "-bad"},
            {"provider": "luxmed"},
            {"provider": 123},
            {"date_from": datetime(2026, 12, 1, 0, 0)},
            {"date_to": datetime(2026, 12, 10, 0, 0)},
            {"date_from": date(2026, 12, 20), "date_to": date(2026, 12, 10)},
            {"date_from": date(2026, 1, 1), "date_to": date(2026, 12, 31)},
            {"specialty_id": "bad spec"},
            {"specialty_id": None, "doctor_ids": ()},
            {"doctor_ids": ["doc-1"]},  # list instead of tuple
            {"doctor_ids": ("doc-1", "doc-1")},
            {"location_ids": ("loc-1", "loc-1")},
            {"time_from": "08:00"},
            {"time_from": time(8, 0, tzinfo=timezone.utc)},
            {"time_to": "17:00"},
            {"time_to": time(17, 0, tzinfo=timezone.utc)},
            {"time_from": time(18, 0), "time_to": time(8, 0)},
            {"service_id": "bad service"},
            {"booked_slot": {"start": "2026-12-20T14:00:00Z"}},
            {"only_better_than_booked": "yes"},
            {"only_better_than_booked": True},
            {"label": "\n"},
            {"label": "\ud800"},
        ):
            with self.subTest(watch_overrides=overrides):
                with self.assertRaises(AppointmentsConfigError):
                    WatchCriteria(**(valid_watch | overrides))

        with self.assertRaises(AppointmentsConfigError):
            CmpCredentials(auth_mode="cookie")
        with self.assertRaises(AppointmentsConfigError):
            CmpCredentials(auth_mode="none", bearer_token="tok")
        with self.assertRaises(AppointmentsConfigError):
            CmpCredentials(auth_mode="bearer", bearer_token="has space")

    def test_strict_json_url_and_schema_rejections(self) -> None:
        self.cfg_path.chmod(0o600)
        for raw in (
            b'{"owner":"a","owner":"b","watches":[]}',
            b'{"owner":"a","watches":[],"x":NaN}',
            b"[]",
            b"x" * 16385,
            b"[" * 2000 + b"]" * 2000,
        ):
            with self.subTest(raw_len=len(raw)):
                self.cfg_path.write_bytes(raw)
                self.assert_invalid_load()
        self.cfg_path.chmod(0o400)

        for payload in (
            {"owner": "alice"},  # missing watches
            self.base_data | {"unexpected": 1},
            self.base_data | {"owner": "   "},
            self.base_data | {"cmp_base_url": "http://ic-api.cmp.med.pl/api/v1"},
            self.base_data | {"cmp_base_url": "https://user:pass@ic-api.cmp.med.pl/api/v1"},
            self.base_data | {"cmp_base_url": "https://ic-api.cmp.med.pl/api/v1?x=1"},
            self.base_data | {"cmp_base_url": "https://ic-api.cmp.med.pl:0/api/v1"},
            self.base_data | {"cmp_base_url": "https://ic-api.cmp.med.pl:99999/api/v1"},
            self.base_data | {"poll_policy": "bad"},
            self.base_data | {"poll_policy": {"unknown_field": 1}},
            self.base_data | {"watches": []},
            self.base_data | {"watches": "not-a-list"},
            self.base_data | {"watches": [self.base_data["watches"][0], self.base_data["watches"][0]]},  # dup id
            self.base_data | {"watches": ["not-a-dict"]},
            self.base_data | {"watches": [{"watch_id": "w1", "provider": "cmp"}]},
            self.base_data | {"watches": [{**self.base_data["watches"][0], "extra": 1}]},
            self.base_data | {"watches": [{**self.base_data["watches"][0], "date_from": "2026/12/01"}]},
            self.base_data | {"watches": [{**self.base_data["watches"][0], "date_to": 20261231}]},
            self.base_data | {"watches": [{**self.base_data["watches"][0], "time_from": "8:00"}]},
            self.base_data | {"watches": [{**self.base_data["watches"][0], "time_to": 1700}]},
            self.base_data | {"watches": [{**self.base_data["watches"][0], "doctor_ids": "doc-1"}]},
            self.base_data | {"watches": [{**self.base_data["watches"][0], "booked_slot": "bad"}]},
            self.base_data | {"watches": [{**self.base_data["watches"][0], "booked_slot": {"start": "2026-12-20"}}]},
            self.base_data | {"watches": [{**self.base_data["watches"][0], "booked_slot": {"start": "2026-03-29T02:30:00"}}]},
            self.base_data | {"watches": [{**self.base_data["watches"][0], "booked_slot": {"start": 123}}]},
            self.base_data | {"watches": [{**self.base_data["watches"][0], "booked_slot": {"start": "2026-12-20T14:00:00Z", "bad": 1}}]},
            self.base_data | {"auth_token_env": "SYNTHETIC_CMP_TOKEN_FILE"},  # auth_mode=none forbids auth_token_env
            self.base_data | {"auth_mode": "bearer", "auth_token_env": "lower_case"},
            self.base_data | {"auth_mode": "bearer", "auth_token_env": "MISSING_ENV_VAR"},
        ):
            with self.subTest(payload=payload):
                self.write_cfg(payload)
                self.assert_invalid_load()

    def test_secret_file_permissions_symlinks_hmac_distinctness_and_growth_race(self) -> None:
        # Relative config path rejected.
        self.assert_invalid_load(environ={FLAG: "1", CONFIG_FILE: "relative/appointments.json"})

        # Mode checks on config file.
        for mode in (0o644, 0o640, 0o660, 0o700):
            with self.subTest(mode=oct(mode)):
                self.cfg_path.chmod(mode)
                self.assert_invalid_load()
        self.cfg_path.chmod(0o600)
        self.assertEqual(load_settings(environ=self.env).owner, "example-owner")
        self.cfg_path.chmod(0o400)

        # Wrong UID rejected.
        with mock.patch.object(appt_config.os, "geteuid", return_value=os.geteuid() + 1):
            self.assert_invalid_load()

        # Symlink rejected.
        alias = self.cfg_path.with_name("alias.json")
        alias.symlink_to(self.cfg_path)
        self.assert_invalid_load(environ=self.env | {CONFIG_FILE: str(alias)})

        # FIFO rejected.
        fifo = self.cfg_path.with_name("pipe.json")
        os.mkfifo(fifo)
        self.assert_invalid_load(environ=self.env | {CONFIG_FILE: str(fifo)})

        # File-growth TOCTOU race guard inside _read_secret.
        with mock.patch.object(appt_config.os, "read", return_value=b"a" * 20000):
            self.assert_invalid_load()

        # Token & HMAC secret validation.
        self.write_cfg(self.base_data | {
            "auth_mode": "bearer",
            "auth_token_env": "SYNTHETIC_CMP_TOKEN_FILE",
            "hmac_key_env": "SYNTHETIC_APPT_HMAC_FILE",
        })
        self.token_path.chmod(0o644)
        self.assert_invalid_load()
        self.token_path.chmod(0o600)
        self.token_path.write_bytes(b"\xff")
        self.assert_invalid_load()
        self.token_path.write_bytes(SYNTHETIC_TOKEN.encode("ascii"))
        self.token_path.chmod(0o400)

        # Short HMAC key rejected.
        self.hmac_path.chmod(0o600)
        self.hmac_path.write_bytes(b"short")
        self.assert_invalid_load()

        # HMAC equal to session_secret rejected.
        self.hmac_path.write_bytes(SYNTHETIC_HMAC)
        self.assert_invalid_load(session_secret=SYNTHETIC_HMAC)

        # HMAC equal to raw token bytes or stripped token rejected.
        self.token_path.chmod(0o600)
        self.token_path.write_bytes(SYNTHETIC_HMAC + b"\n")
        self.hmac_path.write_bytes(SYNTHETIC_HMAC + b"\n")
        self.assert_invalid_load()
        self.hmac_path.write_bytes(SYNTHETIC_HMAC)
        self.assert_invalid_load()
