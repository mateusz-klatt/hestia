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


class PasswordTests(unittest.TestCase):
    def test_round_trip(self):
        self.assertTrue(auth.verify_password("hunter2", auth.hash_password("hunter2")))

    def test_wrong_password_rejected(self):
        self.assertFalse(auth.verify_password("nope", auth.hash_password("hunter2")))

    def test_salt_is_random(self):
        self.assertNotEqual(auth.hash_password("x"), auth.hash_password("x"))  # fresh salt each time

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
        for bad in ("", "nodot", "a.b.c", "@@@.@@@"):
            self.assertIsNone(auth.verify_session(bad, now=1.0, secret=SECRET))

    def _signed(self, payload: bytes) -> str:
        return f"{auth._b64e(payload)}.{auth._b64e(hmac.new(SECRET, payload, hashlib.sha256).digest())}"

    def test_validly_signed_but_no_separator_rejected(self):
        self.assertIsNone(auth.verify_session(self._signed(b"noseparator"), now=1.0, secret=SECRET))

    def test_validly_signed_non_int_expiry_rejected(self):
        self.assertIsNone(auth.verify_session(self._signed(b"tata|soon"), now=1.0, secret=SECRET))

    def test_validly_signed_non_utf8_rejected(self):
        self.assertIsNone(auth.verify_session(self._signed(b"\xff\xfe|10"), now=1.0, secret=SECRET))


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
        self.assertEqual(auth._cli(["role", "tata", "viewer"]), 0)
        self.assertEqual(store_sql.get_user_db_role("tata"), "viewer")

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
