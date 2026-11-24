"""Tests for hestia.auth — per-user password hashing, signed session tokens, the users store, and the CLI.

scrypt is real (no monkeypatching of cost) but parameters are small enough that the handful of hashes
here stay fast. No clock/env/fs leakage: `now`/`secret` are injected and the store is a tempfile.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hestia import auth, db, store_sql

SECRET = b"unit-test-secret"


class LoginLimiterTests(unittest.TestCase):
    def test_attempts_share_ipv6_prefix_and_expire(self):
        limiter = auth.LoginLimiter()
        for _ in range(auth.LOGIN_ATTEMPTS):
            self.assertIsNone(limiter.take("owner", "2001:db8::1", 1))
        self.assertEqual(limiter.take("owner", "2001:db8::2", 2), auth.LOGIN_WINDOW - 1)
        self.assertIsNone(limiter.take("other", "2001:db8::2", 2))
        self.assertIsNone(limiter.take("owner", "2001:db8:0:1::2", 2))
        self.assertIsNone(limiter.take("owner", "2001:db8::2", 1 + auth.LOGIN_WINDOW))

    def test_bounded_keys_fail_closed_without_evicting_active_buckets(self):
        limiter = auth.LoginLimiter()
        with mock.patch.object(auth, "_MAX_LOGIN_BUCKETS", 1):
            self.assertIsNone(limiter.take("owner", "192.0.2.1", 0))
            self.assertIsNone(limiter.take("owner", "::ffff:192.0.2.1", 1))
            self.assertEqual(limiter.take("other", "invalid", 1), auth.LOGIN_WINDOW - 1)
            self.assertIsNone(limiter.take("other", "invalid", auth.LOGIN_WINDOW))

    def test_one_source_cannot_exhaust_table_or_evict_lockouts(self):
        for source, same_source, other in (("192.0.2.1", "::ffff:192.0.2.1", "192.0.2.2"),
                                           ("2001:db8::1", "2001:db8::2", "2001:db8:0:1::1")):
            with self.subTest(source=source):
                limiter = auth.LoginLimiter()
                for _ in range(auth.LOGIN_ATTEMPTS):
                    self.assertIsNone(limiter.take("owner", source, 0))
                for index in range(auth._MAX_LOGIN_SOURCE_BUCKETS - 1):
                    self.assertIsNone(limiter.take(str(index), source, 1))
                self.assertEqual(limiter.take("overflow", same_source, 2), auth.LOGIN_WINDOW - 2)
                self.assertIsNone(limiter.take("unrelated", other, 2))
                self.assertEqual(limiter.take("owner", source, 2), auth.LOGIN_WINDOW - 2)
                self.assertIsNone(limiter.take("0", same_source, 2))  # existing buckets still work
                self.assertEqual(len(limiter._attempts), auth._MAX_LOGIN_SOURCE_BUCKETS + 1)
                self.assertIsNone(limiter.take("overflow", source, 1 + auth.LOGIN_WINDOW))
                self.assertEqual(len(limiter._sources[source if ":" not in source else "2001:db8::/64"]), 1)


class PasswordTests(unittest.TestCase):
    def test_round_trip(self):
        self.assertTrue(auth.verify_password("hunter2", auth.hash_password("hunter2")))

    def test_wrong_password_rejected(self):
        self.assertFalse(auth.verify_password("nope", auth.hash_password("hunter2")))

    def test_salt_is_random(self):
        first_hash = auth.hash_password("x")
        second_hash = auth.hash_password("x")
        self.assertNotEqual(first_hash, second_hash)
        self.assertNotEqual(first_hash.split("$")[4], second_hash.split("$")[4])  # fresh salt each time

    def test_stored_format(self):
        stored = auth.hash_password("x")
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertEqual(len(stored.split("$")), 6)

    def test_non_scrypt_algo_rejected(self):
        self.assertFalse(auth.verify_password("x", "bcrypt$2$8$1$YQ==$Yg=="))

    def test_non_string_stored_rejected(self):
        for bad in (None, 12345, ["scrypt"]):       # a non-str stored must return False, not raise
            self.assertFalse(auth.verify_password("x", bad))

    def test_non_string_password_rejected(self):
        valid = auth.hash_password("x")
        for bad in (None, 12345, ["x"]):             # a non-str password must return False, not raise
            self.assertFalse(auth.verify_password(bad, valid))

    def test_malformed_stored_rejected(self):
        for bad in ("", "scrypt$only", "scrypt$x$y$z$bad$base64!!", "scrypt$16384$8$1$@@@$@@@",
                    "scrypt$999999999999999999999$8$1$YQ==$Yg=="):   # absurd n → OverflowError, must be caught
            self.assertFalse(auth.verify_password("x", bad))

    def test_unknown_user_never_logs_in_with_dummy_password(self):
        # The timing-equaliser verifies against a hash of a FIXED string; submitting that exact string for
        # an unknown user must NOT authenticate (regression guard against a dummy-hash auth bypass).
        self.assertFalse(auth.authenticate("ghost", auth._DUMMY_PASSWORD, {}))


class SessionTests(unittest.TestCase):
    def test_round_trip(self):
        token = auth.make_session("tata", now=1000.0, secret=SECRET, ttl=100.0)
        self.assertEqual(auth.verify_session(token, now=1050.0, secret=SECRET), "tata")

    def test_username_with_pipe_round_trips(self):
        token = auth.make_session("a|b", now=1000.0, secret=SECRET, ttl=100.0)
        self.assertEqual(auth.verify_session(token, now=1000.0, secret=SECRET), "a|b")

    def test_expired_rejected(self):
        token = auth.make_session("tata", now=1000.0, secret=SECRET, ttl=100.0)
        self.assertIsNone(auth.verify_session(token, now=1100.0, secret=SECRET))   # now == expiry → expired

    def test_wrong_secret_rejected(self):
        token = auth.make_session("tata", now=1000.0, secret=SECRET, ttl=100.0)
        self.assertIsNone(auth.verify_session(token, now=1050.0, secret=b"other"))

    def test_tampered_signature_rejected(self):
        token = auth.make_session("tata", now=1000.0, secret=SECRET, ttl=100.0)
        payload_b64, _sig = token.split(".")
        forged = f"{payload_b64}.{auth._b64e(b'not-the-sig')}"
        self.assertIsNone(auth.verify_session(forged, now=1050.0, secret=SECRET))

    def test_malformed_token_rejected(self):
        for bad in ("", "nodot", "a.a", "a.b.c", "@@@.@@@"):
            self.assertIsNone(auth.verify_session(bad, now=1.0, secret=SECRET))

    def _signed(self, payload: bytes) -> str:
        return f"{auth._b64e(payload)}.{auth._b64e(hmac.new(SECRET, payload, hashlib.sha256).digest())}"

    def test_validly_signed_but_no_separator_rejected(self):
        self.assertIsNone(auth.verify_session(self._signed(b"noseparator"), now=1.0, secret=SECRET))

    def test_validly_signed_non_int_expiry_rejected(self):
        self.assertIsNone(auth.verify_session(self._signed(b"tata|soon"), now=1.0, secret=SECRET))

    def test_validly_signed_non_utf8_rejected(self):
        self.assertIsNone(auth.verify_session(self._signed(b"\xff\xfe|10"), now=1.0, secret=SECRET))

    def _signed_v2_raw(self, payload_b64: str) -> str:
        mac = hmac.new(SECRET, b"hestia/session/v2\0" + payload_b64.encode("ascii"), hashlib.sha256).digest()
        return f"v2.{payload_b64}.{auth._b64u_encode(mac)}"

    def _signed_v2_json(self, raw_json: bytes) -> str:
        return self._signed_v2_raw(auth._b64u_encode(raw_json))

    def test_v1_envelope_compatibility(self):
        token = auth.make_session("tata", now=1000.0, secret=SECRET, ttl=100.0)
        env = auth.decode_session(token, now=1050.0, secret=SECRET)
        self.assertEqual(
            env,
            auth.SessionEnvelope(username="tata", expiry=1100, generation=0, version=1, purpose=None),
        )

    def test_v2_round_trip_browser_and_native(self):
        for purpose in ("browser", "native"):
            with self.subTest(purpose=purpose):
                token = auth.make_session(
                    "alice|żółć",
                    now=1000.0,
                    secret=SECRET,
                    ttl=100.0,
                    generation=3,
                    purpose=purpose,
                )
                parts = token.split(".")
                self.assertEqual(len(parts), 3)
                self.assertEqual(parts[0], "v2")
                self.assertNotIn("=", parts[1])
                self.assertNotIn("=", parts[2])
                # Compact UTF-8 JSON preserves Unicode without \\u escaping.
                decoded_json = auth._b64u_decode(parts[1]).decode("utf-8")
                self.assertIn("alice|żółć", decoded_json)
                env = auth.decode_session(token, now=1050.0, secret=SECRET)
                self.assertEqual(
                    env,
                    auth.SessionEnvelope(
                        username="alice|żółć",
                        expiry=1100,
                        generation=3,
                        version=2,
                        purpose=purpose,
                    ),
                )
                self.assertEqual(auth.verify_session(token, now=1050.0, secret=SECRET), "alice|żółć")

    def test_make_session_v2_argument_validation(self):
        with self.assertRaises(ValueError):
            auth.make_session("alice", now=10.0, secret=SECRET, generation=0)
        with self.assertRaises(ValueError):
            auth.make_session("alice", now=10.0, secret=SECRET, purpose="browser")
        with self.assertRaises(ValueError):
            auth.make_session("", now=10.0, secret=SECRET, generation=0, purpose="browser")
        with self.assertRaises(ValueError):
            auth.make_session("alice", now=10.0, secret=SECRET, generation=-1, purpose="browser")
        with self.assertRaises(ValueError):
            auth.make_session("alice", now=10.0, secret=SECRET, generation=True, purpose="browser")
        with self.assertRaises(ValueError):
            auth.make_session("alice", now=10.0, secret=SECRET, generation=0, purpose="admin")

    def test_decode_v2_checks_mac_before_json(self):
        # Non-JSON payload with wrong MAC must be rejected at MAC comparison without decoding JSON.
        forged = "v2.bm90LWpzb24." + auth._b64u_encode(b"\x00" * 32)
        with mock.patch("hestia.auth.json.loads", side_effect=AssertionError("JSON parsed before MAC")):
            self.assertIsNone(auth.decode_session(forged, now=10.0, secret=SECRET))

    def test_decode_v2_rejects_malformed_envelopes_and_claims(self):
        # Non-string, unsupported versions, wrong segment count, bad base64url
        self.assertIsNone(auth.decode_session(None, now=10.0, secret=SECRET))
        self.assertIsNone(auth.decode_session("v3.abc.def", now=10.0, secret=SECRET))
        self.assertIsNone(auth.decode_session("v2.abc", now=10.0, secret=SECRET))
        self.assertIsNone(auth.decode_session("v2.abc.def.ghi", now=10.0, secret=SECRET))
        self.assertIsNone(auth.decode_session("v2..def", now=10.0, secret=SECRET))
        self.assertIsNone(auth.decode_session("v2.ż.def", now=10.0, secret=SECRET))
        self.assertIsNone(auth.decode_session("v2.abc.====", now=10.0, secret=SECRET))
        self.assertIsNone(auth.decode_session("v2.abc.!!!", now=10.0, secret=SECRET))
        # MAC has valid base64url chars and 32-byte length check: non-canonical trailing bits in payload_b64 (valid MAC)
        # Encodes b"a" -> "YQ"; "YR" has non-zero unused low bits so _b64u_encode(b"a") == "YQ" != "YR".
        self.assertIsNone(auth.decode_session(self._signed_v2_raw("YR"), now=10.0, secret=SECRET))
        # Valid base64url JSON failures
        self.assertIsNone(auth.decode_session(self._signed_v2_json(b"\xff\xfe"), now=10.0, secret=SECRET))
        self.assertIsNone(auth.decode_session(self._signed_v2_json(b"{not-json"), now=10.0, secret=SECRET))
        self.assertIsNone(auth.decode_session(self._signed_v2_json(b"[1,2,3]"), now=10.0, secret=SECRET))
        self.assertIsNone(
            auth.decode_session(
                self._signed_v2_json(
                    b'{"username":"a","username":"b","expiry":2000,"generation":0,"purpose":"browser"}'
                ),
                now=10.0,
                secret=SECRET,
            )
        )
        # Missing / extra / wrong-typed / out-of-range claims
        bad_payloads = [
            {"username": "a", "expiry": 2000, "generation": 0},
            {"username": "a", "expiry": 2000, "generation": 0, "purpose": "browser", "extra": 1},
            {"username": "", "expiry": 2000, "generation": 0, "purpose": "browser"},
            {"username": 123, "expiry": 2000, "generation": 0, "purpose": "browser"},
            {"username": "a", "expiry": True, "generation": 0, "purpose": "browser"},
            {"username": "a", "expiry": 2000.5, "generation": 0, "purpose": "browser"},
            {"username": "a", "expiry": 100, "generation": 0, "purpose": "browser"},  # expired when now == 100
            {"username": "a", "expiry": 2000, "generation": -1, "purpose": "browser"},
            {"username": "a", "expiry": 2000, "generation": True, "purpose": "browser"},
            {"username": "a", "expiry": 2000, "generation": 0, "purpose": 123},
            {"username": "a", "expiry": 2000, "generation": 0, "purpose": "receipt"},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                token = self._signed_v2_json(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
                self.assertIsNone(auth.decode_session(token, now=100.0, secret=SECRET))
        # NaN constant
        self.assertIsNone(
            auth.decode_session(
                self._signed_v2_json(b'{"username":"a","expiry":NaN,"generation":0,"purpose":"browser"}'),
                now=10.0,
                secret=SECRET,
            )
        )


class UsersStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "users.json"

    def test_missing_file_is_empty(self):
        self.assertEqual(auth.load_users(self.path), {})

    def test_bad_json_is_empty(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(auth.load_users(self.path), {})

    def test_non_dict_json_is_empty(self):
        self.path.write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(auth.load_users(self.path), {})

    def test_valid_store_loads(self):
        self.path.write_text(json.dumps({"tata": "scrypt$x"}), encoding="utf-8")
        self.assertEqual(auth.load_users(self.path), {"tata": "scrypt$x"})

    def test_users_path_default(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop("HESTIA_AUTH_USERS_FILE", None)
            self.assertEqual(str(auth.users_path()), auth.DEFAULT_USERS_FILE)

    def test_users_path_override(self):
        with mock.patch.dict("os.environ", {"HESTIA_AUTH_USERS_FILE": "/tmp/u.json"}):
            self.assertEqual(str(auth.users_path()), "/tmp/u.json")


class AuthenticateTests(unittest.TestCase):
    def setUp(self):
        self.users = {"tata": auth.hash_password("correct horse")}

    def test_existing_user_correct_password(self):
        self.assertTrue(auth.authenticate("tata", "correct horse", self.users))

    def test_existing_user_wrong_password(self):
        self.assertFalse(auth.authenticate("tata", "wrong", self.users))

    def test_unknown_user_rejected(self):
        self.assertFalse(auth.authenticate("ghost", "anything", self.users))   # runs the dummy hash

    def test_non_string_stored_rejected(self):
        self.assertFalse(auth.authenticate("x", "y", {"x": 12345}))

    def test_non_string_login_inputs_rejected(self):
        # malformed login JSON must fail closed (return False), never raise (e.g. users.get([]) / None.encode)
        self.assertFalse(auth.authenticate([], "pw", self.users))            # non-str username
        self.assertFalse(auth.authenticate("tata", None, self.users))        # non-str password, valid user
        self.assertFalse(auth.authenticate("tata", ["pw"], self.users))


class _Prompt:
    """A getpass stand-in returning scripted answers in order."""
    def __init__(self, *answers):
        self._answers = list(answers)

    def __call__(self, _msg):
        return self._answers.pop(0)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "sub" / "users.json"   # parent dir does not exist yet

    def test_usage_error(self):
        self.assertEqual(auth._cli([], prompt=_Prompt(), path=self.path), 2)
        self.assertEqual(auth._cli(["remove", "tata"], prompt=_Prompt(), path=self.path), 2)

    def test_invalid_username(self):
        for bad in ("", "a|b", "a/b"):
            self.assertEqual(auth._cli(["add", bad], prompt=_Prompt(), path=self.path), 2)

    def test_empty_password(self):
        self.assertEqual(auth._cli(["add", "tata"], prompt=_Prompt(""), path=self.path), 1)

    def test_passwords_differ(self):
        self.assertEqual(auth._cli(["add", "tata"], prompt=_Prompt("a", "b"), path=self.path), 1)

    def test_add_creates_store_and_hash(self):
        rc = auth._cli(["add", "tata"], prompt=_Prompt("s3cret", "s3cret"), path=self.path)
        self.assertEqual(rc, 0)
        users = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIn("tata", users)
        self.assertTrue(auth.verify_password("s3cret", users["tata"]))   # stored as a usable hash

    def test_add_upserts_into_existing_store(self):
        auth._cli(["add", "tata"], prompt=_Prompt("a", "a"), path=self.path)
        auth._cli(["add", "mama"], prompt=_Prompt("b", "b"), path=self.path)
        users = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(sorted(users), ["mama", "tata"])   # both kept

    def test_add_with_role_in_json_mode_saves_without_role(self):
        # JSON has no role column; --role is accepted but noted as ignored (the user is still saved).
        rc = auth._cli(["add", "tata", "--role", "viewer"], prompt=_Prompt("a", "a"), path=self.path)
        self.assertEqual(rc, 0)
        self.assertIn("tata", json.loads(self.path.read_text(encoding="utf-8")))

    def test_add_missing_role_value_is_usage_error(self):
        self.assertEqual(auth._cli(["add", "tata", "--role"], prompt=_Prompt(), path=self.path), 2)

    def test_add_too_many_positionals_is_usage_error(self):
        self.assertEqual(auth._cli(["add", "tata", "extra"], prompt=_Prompt(), path=self.path), 2)

    def test_role_subcommand_requires_sqlite(self):
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "json"}):
            self.assertEqual(auth._cli(["role", "tata", "admin"]), 1)

    def test_role_subcommand_bad_argc(self):
        self.assertEqual(auth._cli(["role", "tata"]), 2)


class CliSqliteTests(unittest.TestCase):
    """The CLI's SQLite branches: add (new→viewer / explicit role / preserve-role-on-reset / invalid) and
    the role subcommand, against a cut-over users DB (tata = admin)."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.db = self.dir / "hestia.db"
        self.env = mock.patch.dict(os.environ, {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.db)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(db.reset_engine_cache)
        engine, _ = db.init_db(self.db)
        store_sql.cutover_users(engine, {"tata": auth.hash_password("old")})  # tata=admin, users authoritative
        engine.dispose()

    def test_add_new_user_defaults_to_viewer(self):
        self.assertEqual(auth._cli(["add", "kid"], prompt=_Prompt("pw", "pw")), 0)
        self.assertEqual(store_sql.get_user_db_role("kid"), "viewer")

    def test_add_with_explicit_role(self):
        self.assertEqual(auth._cli(["add", "mama", "--role", "operator"], prompt=_Prompt("pw", "pw")), 0)
        self.assertEqual(store_sql.get_user_db_role("mama"), "operator")

    def test_add_existing_user_password_reset_preserves_role(self):
        self.assertEqual(auth._cli(["add", "tata"], prompt=_Prompt("new", "new")), 0)
        self.assertEqual(store_sql.get_user_db_role("tata"), "admin")   # not demoted to viewer

    def test_add_invalid_role_is_rejected(self):
        self.assertEqual(auth._cli(["add", "x", "--role", "superuser"], prompt=_Prompt("pw", "pw")), 2)
        self.assertIsNone(store_sql.get_user_db_role("x"))   # nothing written

    def test_role_changes_existing_user(self):
        self.assertEqual(auth._cli(["add", "boss", "--role", "admin"], prompt=_Prompt("pw", "pw")), 0)  # 2nd admin
        self.assertEqual(auth._cli(["role", "tata", "viewer"]), 0)
        self.assertEqual(store_sql.get_user_db_role("tata"), "viewer")

    def test_role_refuses_demoting_the_last_admin(self):
        self.assertEqual(auth._cli(["role", "tata", "viewer"]), 1)        # tata is the only admin → refused
        self.assertEqual(store_sql.get_user_db_role("tata"), "admin")     # unchanged

    def test_role_unknown_user(self):
        self.assertEqual(auth._cli(["role", "ghost", "admin"]), 1)

    def test_role_invalid_role(self):
        self.assertEqual(auth._cli(["role", "tata", "boss"]), 2)

    def test_role_requires_users_cut_over(self):
        other = self.dir / "fresh.db"   # a sqlite DB whose users table is NOT yet authoritative
        with mock.patch.dict(os.environ, {"HESTIA_DB": str(other)}):
            self.assertEqual(auth._cli(["role", "tata", "admin"]), 1)

    def test_disable_and_enable(self):
        # a 2nd admin so disabling tata won't trip the last-admin guard
        self.assertEqual(auth._cli(["add", "boss", "--role", "admin"], prompt=_Prompt("pw", "pw")), 0)
        self.assertEqual(auth._cli(["disable", "tata"]), 0)
        self.assertIsNone(store_sql.current_user_role("tata"))         # disabled → denied
        self.assertEqual(auth._cli(["enable", "tata"]), 0)
        self.assertEqual(store_sql.current_user_role("tata"), "admin")  # re-enabled

    def test_disable_last_admin_refused(self):
        self.assertEqual(auth._cli(["disable", "tata"]), 1)   # tata is the only enabled admin

    def test_disable_unknown_user(self):
        self.assertEqual(auth._cli(["disable", "ghost"]), 1)

    def test_disable_bad_argc(self):
        self.assertEqual(auth._cli(["disable"]), 2)
        self.assertEqual(auth._cli(["enable", "a", "b"]), 2)

    def test_disable_requires_sqlite(self):
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "json"}):
            self.assertEqual(auth._cli(["disable", "tata"]), 1)

    def test_disable_requires_users_cut_over(self):
        other = self.dir / "fresh2.db"
        with mock.patch.dict(os.environ, {"HESTIA_DB": str(other)}):
            self.assertEqual(auth._cli(["disable", "tata"]), 1)

    def test_add_falls_back_to_json_when_sqlite_users_not_cut_over(self):
        other = self.dir / "fresh_add.db"
        json_users = self.dir / "users.json"
        with mock.patch.dict(
            os.environ,
            {"HESTIA_DB": str(other), "HESTIA_AUTH_USERS_FILE": str(json_users)},
        ):
            db.init_db(other)
            self.assertEqual(auth._cli(["add", "kid"], prompt=_Prompt("pw", "pw")), 0)
        self.assertIn("kid", json.loads(json_users.read_text(encoding="utf-8")))

    def test_activate_sessions_v2_cli(self):
        self.assertEqual(auth._cli(["activate-sessions-v2", "extra"]), 2)
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "json"}):
            self.assertEqual(auth._cli(["activate-sessions-v2"]), 1)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HESTIA_SESSION_SECRET", None)
            self.assertEqual(auth._cli(["activate-sessions-v2"]), 1)
        other = self.dir / "fresh_v2.db"
        with mock.patch.dict(os.environ, {"HESTIA_DB": str(other), "HESTIA_SESSION_SECRET": "secret"}):
            self.assertEqual(auth._cli(["activate-sessions-v2"]), 1)
        with mock.patch.dict(os.environ, {"HESTIA_SESSION_SECRET": "secret"}):
            self.assertEqual(auth._cli(["activate-sessions-v2"]), 0)
            self.assertEqual(auth._cli(["activate-sessions-v2"]), 0)  # idempotent repeat

    def test_budget_owner_cli(self):
        self.assertEqual(auth._cli(["budget-owner"]), 2)
        self.assertEqual(auth._cli(["budget-owner", "bad|name"]), 2)
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "json"}):
            self.assertEqual(auth._cli(["budget-owner", "tata"]), 1)
        other = self.dir / "fresh_owner.db"
        with mock.patch.dict(os.environ, {"HESTIA_DB": str(other)}):
            self.assertEqual(auth._cli(["budget-owner", "tata"]), 1)
        self.assertEqual(auth._cli(["add", "kid", "--role", "viewer"], prompt=_Prompt("pw", "pw")), 0)
        self.assertEqual(auth._cli(["budget-owner", "ghost"]), 1)
        self.assertEqual(auth._cli(["budget-owner", "kid"]), 1)
        self.assertEqual(auth._cli(["budget-owner", "tata"]), 0)
        self.assertEqual(auth._cli(["budget-owner", "tata"]), 0)  # idempotent repeat

    def test_cli_fails_closed_with_fixed_error_on_unsupported_policy_phase(self):
        import io
        engine, _ = db.init_db(self.db)
        with engine.begin() as conn:
            conn.exec_driver_sql("INSERT INTO app_meta (key, value) VALUES ('authz_policy_phase', 'catalog_custom')")
        engine.dispose()
        with mock.patch.dict(os.environ, {"HESTIA_SESSION_SECRET": "secret"}):
            for argv, kw in (
                (["role", "tata", "viewer"], {}),
                (["disable", "tata"], {}),
                (["add", "newu"], {"prompt": _Prompt("pw", "pw")}),
                (["activate-sessions-v2"], {}),
                (["budget-owner", "tata"], {}),
            ):
                with self.subTest(argv=argv), mock.patch("sys.stderr", new_callable=io.StringIO) as buf:
                    self.assertEqual(auth._cli(argv, **kw), 1)
                    self.assertIn("incompatible authorization bridge phase", buf.getvalue())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
