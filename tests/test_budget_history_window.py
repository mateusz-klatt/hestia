"""Per-connection history windows and coverage-aware quarantine (Variant A + U1)."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from sqlalchemy import select

from hestia import db
from hestia.budget import config, consent_runtime, normalize, runtime
from hestia.budget.client import BudgetAPIError
from hestia.budget.consent_files import ConsentFiles
from hestia.budget.import_file import ImportSnapshot
from hestia.budget.scheduler import BudgetScheduler
from hestia.budget.store import BudgetStore, BudgetStoreError
from tests import test_budget_store as fixtures


APPLICATION = "12345678-1234-4234-8234-123456789abc"
SESSION = "12345678-5678-4234-8234-123456789abc"
UID = "00000000-0000-4000-8000-0000000000aa"
PRIMARY = "synthetic-hash"
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc).timestamp()


def _sample_raw(**connection_overrides):
    conn = {"id": "bnp-personal", "owner": "owner", "bank_name": "BNP Paribas",
            "country": "PL", "session_file": "/private/bnp-session.json",
            "accounts": [PRIMARY], **connection_overrides}
    return {"version": 1, "application_id": APPLICATION, "key_file": "/private/key.pem",
            "connections": [conn]}


def _raw_tx(ref="ref-1", booking_date="2026-11-20", *, amount="10.50", status="BOOK", value_date=None):
    return {"entry_reference": ref, "transaction_amount": {"amount": amount, "currency": "PLN"},
            "credit_debit_indicator": "DBIT", "status": status,
            "booking_date": booking_date, "value_date": value_date, "transaction_date": None,
            "remittance_information": ["Synthetic purchase"]}


def _windowed_snapshot(*transactions, date_from="2026-08-24", date_to="2026-11-21",
                       account_hash=PRIMARY, valid_until=NOW + 31_536_000):
    account = normalize.AccountSnapshot(
        account_hash,
        ({"name": "Booked", "balance_type": "CLBD",
          "balance_amount": {"amount": "100", "currency": "PLN"}, "reference_date": date_to},),
        tuple(transactions),
    )
    return normalize.Snapshot(valid_until, (account,), date_from, date_to)


class _RecordingClient:
    def __init__(self, pages, *, balances=None, bank_name="BNP Paribas", primary=PRIMARY):
        self.pages = list(pages)
        self.balances = [{"name": "Booked", "balance_type": "CLBD",
                          "balance_amount": {"amount": "100.00", "currency": "PLN"},
                          "reference_date": "2026-11-21"}] if balances is None else balances
        self.bank_name = bank_name
        self.primary = primary
        self.calls = []
        self._page_index = 0

    def get(self, path, *, query=None):
        self.calls.append((path, None if query is None else dict(query)))
        if path == f"/sessions/{SESSION}":
            return {"status": "AUTHORIZED", "aspsp": {"name": self.bank_name, "country": "PL"},
                    "access": {"valid_until": "2027-01-01T00:00:00+00:00"},
                    "accounts_data": [{"identification_hash": self.primary, "uid": UID}]}
        if path == f"/accounts/{UID}/balances":
            return {"balances": self.balances}
        if path == f"/accounts/{UID}/transactions":
            outcome = self.pages[self._page_index]
            self._page_index += 1
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        raise AssertionError(f"Unexpected path: {path}")


class ConfigHistoryWindowTests(unittest.TestCase):
    def test_max_history_days_defaults_valid_values_and_strict_validation(self):
        parsed = config.parse_config(_sample_raw())
        self.assertEqual(parsed.connections[0].max_history_days, 364)
        direct = config.ConnectionConfig("bnp", "owner", "BNP Paribas", "PL", "/test/session.json", (PRIMARY,))
        self.assertEqual(direct.max_history_days, 364)

        for value in (1, 89, 364):
            with self.subTest(valid=value):
                raw = _sample_raw(max_history_days=value)
                before = deepcopy(raw)
                parsed = config.parse_config(raw)
                self.assertEqual(parsed.connections[0].max_history_days, value)
                self.assertEqual(raw, before)

        raw = _sample_raw(max_history_days=89, policy={"max_acquisitions_24h": 1})
        raw["connections"].append({"id": "ing-personal", "owner": "owner", "bank_name": "ING Bank Śląski",
                                   "country": "PL", "session_file": "/private/ing-session.json",
                                   "accounts": ["other-hash"]})
        parsed = config.parse_config(raw)
        self.assertEqual((parsed.connections[0].max_history_days, parsed.connections[1].max_history_days), (89, 364))

        for bad in (None, True, False, 0, -1, 365, 89.0, "89", "SECRET", [], {}):
            with self.subTest(invalid=bad):
                with self.assertRaisesRegex(config.BudgetConfigError, "^Invalid max_history_days$") as caught:
                    config.parse_config(_sample_raw(max_history_days=bad))
                self.assertNotIn("SECRET", str(caught.exception))
                with self.assertRaisesRegex(config.BudgetConfigError, "^Invalid max_history_days$"):
                    config.ConnectionConfig("bnp", "owner", "BNP Paribas", "PL", "/test/session.json",
                                            (PRIMARY,), max_history_days=bad)

        raw_extra = _sample_raw(max_history_days=89, unknown="SECRET")
        with self.assertRaises(config.BudgetConfigError):
            config.parse_config(raw_extra)
        raw_missing = _sample_raw(max_history_days=89)
        del raw_missing["connections"][0]["accounts"]
        with self.assertRaises(config.BudgetConfigError):
            config.parse_config(raw_missing)

    def test_max_history_days_preserved_across_consent_runtime_resolve_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_file = root / "hestia.db"
            consent_dir = root / "consent"
            files = ConsentFiles(consent_dir)
            capsule_ref = files.put("session", {"session_id": SESSION})
            capsule_path = Path(files.path(capsule_ref, kind="session"))

            cfg = config.parse_config(_sample_raw(max_history_days=89))
            # Missing DB file preserves max_history_days.
            self.assertEqual(consent_runtime.resolve_config(cfg, path=db_file, files=files, clock=lambda: NOW)
                             .connections[0].max_history_days, 89)

            _, sessions = db.init_db(db_file)
            try:
                with sessions.begin() as session:
                    session.add(db.User(username="owner", password_hash="unused", role="admin"))
                BudgetStore(sessions, clock=lambda: NOW).configure(cfg)
                # External credential_mode overlay preserves max_history_days.
                resolved_external = consent_runtime.resolve_config(cfg, path=db_file, files=files, clock=lambda: NOW)
                self.assertEqual(resolved_external.connections[0].max_history_days, 89)
                self.assertTrue(resolved_external.connections[0].credential_available)

                # Managed ACTIVE consent overlay preserves max_history_days.
                with sessions.begin() as session:
                    session.add(db.BudgetConsent(
                        id="consent-1", connection_id="bnp-personal", owner="owner",
                        application_id=APPLICATION, bank_name="BNP Paribas", country="PL",
                        provenance="hestia_created", status="ACTIVE", generation=1,
                        capsule_ref=capsule_ref, valid_until=NOW + 86400,
                        metadata_json="{}", created_at=NOW,
                    ))
                    row = session.get(db.BudgetConnection, "bnp-personal")
                    row.credential_mode = "managed"
                    row.consent_generation = 1
                    row.active_consent_id = "consent-1"
                resolved_managed = consent_runtime.resolve_config(cfg, path=db_file, files=files, clock=lambda: NOW)
                self.assertEqual(resolved_managed.connections[0].max_history_days, 89)
                self.assertTrue(resolved_managed.connections[0].credential_available)
                self.assertEqual(resolved_managed.connections[0].session_file, str(capsule_path))

                # Unavailable / expired managed overlay still preserves max_history_days.
                resolved_expired = consent_runtime.resolve_config(
                    cfg, path=db_file, files=files, clock=lambda: NOW + 100_000)
                self.assertEqual(resolved_expired.connections[0].max_history_days, 89)
                self.assertFalse(resolved_expired.connections[0].credential_available)
            finally:
                db.reset_engine_cache()

            # Pre-managed legacy schema preserves max_history_days as well.
            legacy_db = root / "legacy.db"
            conn = sqlite3.connect(legacy_db)
            try:
                conn.execute("CREATE TABLE budget_connections (id TEXT PRIMARY KEY)")
                conn.commit()
            finally:
                conn.close()
            resolved_legacy = consent_runtime.resolve_config(cfg, path=legacy_db, files=files, clock=lambda: NOW)
            self.assertEqual(resolved_legacy.connections[0].max_history_days, 89)


class NormalizeHistoryWindowTests(unittest.TestCase):
    def _connection(self, **changes):
        return config.ConnectionConfig("bnp-personal", "owner", "BNP Paribas", "PL",
                                       "/private/bnp-session.json", (PRIMARY,), **changes)

    def test_89_day_window_sent_on_all_pages_and_applied_locally(self):
        client = _RecordingClient([
            {
                "transactions": [
                    _raw_tx("before-window", "2026-08-23"),
                    _raw_tx("start-edge", "2026-08-24"),
                    _raw_tx("dup-cross", "2026-08-23", amount="10"),
                    _raw_tx("dateless", None),
                ],
                "continuation_key": "page-2",
            },
            {
                "transactions": [
                    _raw_tx("dup-cross", "2026-08-25", amount="20"),
                    _raw_tx("end-edge", "2026-11-21"),
                    _raw_tx("after-window", "2026-11-22"),
                ],
                "continuation_key": None,
            },
        ])
        snap = normalize.fetch_snapshot(client, SESSION, self._connection(max_history_days=89),
                                        today=date(2026, 11, 21), now=NOW)
        self.assertEqual((snap.date_from, snap.date_to), ("2026-08-24", "2026-11-21"))
        tx_queries = [q for path, q in client.calls if path == f"/accounts/{UID}/transactions"]
        self.assertEqual(tx_queries, [
            {"date_from": "2026-08-24", "date_to": "2026-11-21"},
            {"date_from": "2026-08-24", "date_to": "2026-11-21", "continuation_key": "page-2"},
        ])
        refs = [tx["entry_reference"] for tx in snap.accounts[0].transactions]
        # Out-of-window conflicting version of in-window ref stays visible; unrelated out-of-window rows are dropped.
        self.assertEqual(refs, ["start-edge", "dup-cross", "dateless", "dup-cross", "end-edge"])

    def test_default_364_and_1_day_bounds_and_invalid_stub_windows(self):
        client_default = _RecordingClient([{"transactions": []}])
        snap_default = normalize.fetch_snapshot(client_default, SESSION, self._connection(),
                                                today=date(2026, 11, 21), now=NOW)
        self.assertEqual((snap_default.date_from, snap_default.date_to), ("2025-11-22", "2026-11-21"))

        client_one = _RecordingClient([{"transactions": []}])
        snap_one = normalize.fetch_snapshot(client_one, SESSION, self._connection(max_history_days=1),
                                            today=date(1, 1, 2), now=NOW)
        self.assertEqual((snap_one.date_from, snap_one.date_to), ("0001-01-01", "0001-01-02"))
        with self.assertRaises(normalize.BudgetDataError):
            normalize.fetch_snapshot(_RecordingClient([]), SESSION, self._connection(max_history_days=1),
                                     today=date(1, 1, 1), now=NOW)

        client_89_min = _RecordingClient([{"transactions": []}])
        snap_89_min = normalize.fetch_snapshot(client_89_min, SESSION, self._connection(max_history_days=89),
                                               today=date(1, 3, 31), now=NOW)
        self.assertEqual((snap_89_min.date_from, snap_89_min.date_to), ("0001-01-01", "0001-03-31"))
        with self.assertRaises(normalize.BudgetDataError):
            normalize.fetch_snapshot(_RecordingClient([]), SESSION, self._connection(max_history_days=89),
                                     today=date(1, 3, 30), now=NOW)

        stub_no_attr = SimpleNamespace(bank_name="BNP Paribas", country="PL", accounts=(PRIMARY,))
        client_stub = _RecordingClient([{"transactions": []}])
        snap_stub = normalize.fetch_snapshot(client_stub, SESSION, stub_no_attr, today=date(2026, 11, 21), now=NOW)
        self.assertEqual((snap_stub.date_from, snap_stub.date_to), ("2025-11-22", "2026-11-21"))

        for bad_days in (0, -1, 365, True, "89", None):
            with self.subTest(bad_days=bad_days), self.assertRaises(normalize.BudgetDataError):
                stub_bad = SimpleNamespace(bank_name="BNP Paribas", country="PL", accounts=(PRIMARY,),
                                           max_history_days=bad_days)
                normalize.fetch_snapshot(_RecordingClient([]), SESSION, stub_bad, today=date(2026, 11, 21), now=NOW)

    def test_late_page_error_publishes_zero_transactions(self):
        client = _RecordingClient([
            {"transactions": [_raw_tx("page1-tx", "2026-11-19")], "continuation_key": "page-2"},
            BudgetAPIError(400, "provider_error", provider_error="ASPSP_ERROR"),
        ])
        snap = normalize.fetch_snapshot(client, SESSION, self._connection(max_history_days=89),
                                        today=date(2026, 11, 21), now=NOW)
        obs = snap.accounts[0]
        self.assertIsNone(obs.transactions)
        self.assertEqual(obs.transactions_error, "provider_error")
        self.assertEqual(obs.transactions_diagnostic.provider_error, "ASPSP_ERROR")
        self.assertIsNotNone(obs.balances)


class StoreCoverageQuarantineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "synthetic.db"
        self.engine, self.sessions = db.init_db(self.path)
        with self.sessions.begin() as session:
            session.add(db.User(username="owner", password_hash="unused", role="admin"))
        self.now = NOW
        self.store = BudgetStore(self.sessions, clock=lambda: self.now)
        self.config = fixtures.configuration(accounts=(PRIMARY,), max_history_days=89)
        fixtures.prepare_selected(self.store, self.config)

    def tearDown(self):
        db.reset_engine_cache()
        self.directory.cleanup()

    def rows(self, model):
        with self.sessions() as session:
            return list(session.scalars(select(model)))

    def test_snapshot_window_validation_rejects_malformed_or_reversed_windows(self):
        account = normalize.AccountSnapshot(PRIMARY, (), ())
        bad_snapshots = (
            normalize.Snapshot(self.now + 1000, (account,), None, None),
            normalize.Snapshot(self.now + 1000, (account,), "2026-08-24", None),
            normalize.Snapshot(self.now + 1000, (account,), None, "2026-11-21"),
            normalize.Snapshot(self.now + 1000, (account,), "2026-8-24", "2026-11-21"),
            normalize.Snapshot(self.now + 1000, (account,), "2026-02-30", "2026-11-21"),
            normalize.Snapshot(self.now + 1000, (account,), "2026-11-21", "2026-08-24"),
            SimpleNamespace(valid_until=self.now + 1000, accounts=(account,), errors=(), date_from="2026-08-24"),
            SimpleNamespace(valid_until=self.now + 1000, accounts=(account,), errors=(), date_to="2026-11-21"),
        )
        for bad in bad_snapshots:
            with self.subTest(bad=bad), self.assertRaisesRegex(BudgetStoreError, "Invalid snapshot date window"):
                self.store.commit_snapshot("bank", bad)
        self.assertEqual(self.rows(db.BudgetTransaction), [])

    def test_older_offline_and_api_records_survive_narrow_89_day_fetch(self):
        old_offline = fixtures.transaction("offline-old", booking_date="2026-03-15", amount="42.00")
        imported = ImportSnapshot("bank", PRIMARY, self.now - 100, (old_offline,), None, "offline-digest-1")
        self.assertEqual(self.store.import_snapshot(self.config, imported)["inserted"], 1)

        old_api = fixtures.transaction("api-old", booking_date="2026-05-01", amount="15.00")
        self.store.commit_snapshot("bank", _windowed_snapshot(old_api, date_from="2025-11-22", date_to="2026-11-21"))

        recent_api = fixtures.transaction("api-new", booking_date="2026-11-20", amount="9.99")
        self.store.commit_snapshot("bank", _windowed_snapshot(recent_api, date_from="2026-08-24", date_to="2026-11-21"))

        refs = {row.entry_reference: (row.source, row.uncertain) for row in self.rows(db.BudgetTransaction)}
        self.assertEqual(refs, {
            "offline-old": ("offline_import", False),
            "api-old": ("api", False),
            "api-new": ("api", False),
        })

    def test_coverage_aware_unresolved_keeps_out_of_window_and_replaces_covered(self):
        old_booking = fixtures.transaction("old-pdng", status="PDNG", booking_date="2026-08-23")
        old_value = fixtures.transaction("old-val-pdng", status="PDNG", booking_date=None, value_date="2026-08-20")
        future_booking = fixtures.transaction("future-pdng", status="PDNG", booking_date="2026-11-22")
        in_booking = fixtures.transaction("in-pdng", status="PDNG", booking_date="2026-09-10")
        in_value = fixtures.transaction("in-val-pdng", status="PDNG", booking_date=None, value_date="2026-09-15")
        undated = fixtures.transaction("undated-book", booking_date=None, value_date=None)

        self.store.commit_snapshot(
            "bank",
            _windowed_snapshot(old_booking, old_value, future_booking, in_booking, in_value, undated,
                               date_from="2025-11-22", date_to="2026-11-30"),
        )
        self.assertEqual(len(self.rows(db.BudgetUnresolved)), 6)

        fresh_in = fixtures.transaction("fresh-pdng", status="PDNG", booking_date="2026-11-20")
        self.store.commit_snapshot("bank", _windowed_snapshot(fresh_in, date_from="2026-08-24", date_to="2026-11-21"))

        unresolved = sorted(self.rows(db.BudgetUnresolved), key=lambda row: row.ordinal)
        refs_and_ordinals = [(row.ordinal, json.loads(row.payload_json)["entry_reference"]) for row in unresolved]
        self.assertEqual(refs_and_ordinals, [
            (0, "old-pdng"),
            (1, "old-val-pdng"),
            (2, "future-pdng"),
            (3, "fresh-pdng"),
        ])
        # Repeating the same window replaces covered ordinal 3 without unbounded ordinal growth.
        fresh_in_2 = fixtures.transaction("fresh-pdng-2", status="PDNG", booking_date="2026-11-21")
        self.store.commit_snapshot("bank", _windowed_snapshot(fresh_in_2, date_from="2026-08-24", date_to="2026-11-21"))
        unresolved_again = sorted(self.rows(db.BudgetUnresolved), key=lambda row: row.ordinal)
        self.assertEqual([(row.ordinal, json.loads(row.payload_json)["entry_reference"]) for row in unresolved_again], [
            (0, "old-pdng"),
            (1, "old-val-pdng"),
            (2, "future-pdng"),
            (3, "fresh-pdng-2"),
        ])

    def test_boundary_split_conflict_stays_uncertain_in_both_orderings_and_is_idempotent(self):
        copy_old = fixtures.transaction("dup-boundary", booking_date="2026-08-23", amount="10")
        copy_new = fixtures.transaction("dup-boundary", booking_date="2026-08-25", amount="20")

        for label, first_round in (("older-first", (copy_old, copy_new)), ("newer-first", (copy_new, copy_old))):
            with self.subTest(ordering=label):
                # Reset per subTest via a fresh store
                with tempfile.TemporaryDirectory() as sub_dir:
                    _, sub_sessions = db.init_db(Path(sub_dir) / "sub.db")
                    try:
                        with sub_sessions.begin() as session:
                            session.add(db.User(username="owner", password_hash="unused", role="admin"))
                        store = BudgetStore(sub_sessions, clock=lambda: self.now)
                        fixtures.prepare_selected(store, self.config)

                        # Round 1: wide window discovers conflict across the future 89-day boundary.
                        store.commit_snapshot("bank", _windowed_snapshot(*first_round,
                                                                         date_from="2025-11-22", date_to="2026-11-21"))
                        # Round 2: narrow 89-day window sees only the in-window copy (2026-08-25).
                        store.commit_snapshot("bank", _windowed_snapshot(copy_new,
                                                                         date_from="2026-08-24", date_to="2026-11-21"))
                        with sub_sessions() as session:
                            tx = session.scalar(select(db.BudgetTransaction))
                            unresolved = list(session.scalars(select(db.BudgetUnresolved)))
                        self.assertTrue(tx.uncertain)
                        self.assertEqual(len(unresolved), 2)
                        self.assertEqual({json.loads(u.payload_json)["amount"] for u in unresolved}, {"10", "20"})

                        # Round 3: bank returns both copies (e.g. via _windowed_records); no duplicate growth.
                        store.commit_snapshot("bank", _windowed_snapshot(copy_old, copy_new,
                                                                         date_from="2026-08-24", date_to="2026-11-21"))
                        with sub_sessions() as session:
                            tx = session.scalar(select(db.BudgetTransaction))
                            unresolved = list(session.scalars(select(db.BudgetUnresolved)))
                        self.assertTrue(tx.uncertain)
                        self.assertEqual(len(unresolved), 2)

                        # Round 4: narrow window empty, then Round 5: narrow window returns copy_new again.
                        store.commit_snapshot("bank", _windowed_snapshot(date_from="2026-08-24", date_to="2026-11-21"))
                        store.commit_snapshot("bank", _windowed_snapshot(copy_new,
                                                                         date_from="2026-08-24", date_to="2026-11-21"))
                        with sub_sessions() as session:
                            tx = session.scalar(select(db.BudgetTransaction))
                            unresolved = list(session.scalars(select(db.BudgetUnresolved)))
                        self.assertTrue(tx.uncertain)
                        self.assertEqual(len(unresolved), 2)
                    finally:
                        db.reset_engine_cache()

    def test_out_of_window_offline_uncertain_cannot_be_cleared_while_in_window_conflict_can_resolve(self):
        # Offline conflict dated 2026-08-10 (outside 89-day window 2026-08-24..2026-11-21).
        offline_a = fixtures.transaction("offline-conflict", booking_date="2026-08-10", amount="10")
        offline_b = fixtures.transaction("offline-conflict", booking_date="2026-08-10", amount="20")
        imported = ImportSnapshot("bank", PRIMARY, self.now - 50, (offline_a, offline_b), None, "offline-conflict-1")
        self.assertEqual(self.store.import_snapshot(self.config, imported)["unresolved"], 2)

        # Narrow 89-day API fetch returning a PDNG row for that ref leaves existing offline row untouched.
        pdng_row = fixtures.transaction("offline-conflict", status="PDNG", booking_date="2026-08-25", amount="30")
        self.store.commit_snapshot("bank", _windowed_snapshot(pdng_row, date_from="2026-08-24", date_to="2026-11-21"))
        row = self.rows(db.BudgetTransaction)[0]
        self.assertTrue(row.uncertain)
        self.assertEqual(row.source, "offline_import")
        self.assertIsNone(row.api_observed_at)

        # Narrow 89-day API fetch returning a single BOOK copy must NOT clear the out-of-window uncertain flag.
        api_single = fixtures.transaction("offline-conflict", booking_date="2026-08-25", amount="30")
        self.store.commit_snapshot("bank", _windowed_snapshot(api_single, date_from="2026-08-24", date_to="2026-11-21"))
        row = self.rows(db.BudgetTransaction)[0]
        self.assertTrue(row.uncertain)
        self.assertEqual(row.source, "offline_import")
        self.assertEqual(json.loads(row.payload_json)["amount"], "10")
        self.assertEqual(row.api_observed_at, self.now)
        self.assertEqual(len(self.rows(db.BudgetUnresolved)), 1)

        # Contrast: an in-window conflict (2026-09-01 & 2026-09-02) DOES resolve when the 89-day window sees 1 clean copy.
        in_a = fixtures.transaction("in-window-conflict", booking_date="2026-09-01", amount="10")
        in_b = fixtures.transaction("in-window-conflict", booking_date="2026-09-02", amount="20")
        self.store.commit_snapshot("bank", _windowed_snapshot(in_a, in_b, date_from="2026-08-24", date_to="2026-11-21"))
        in_row = next(r for r in self.rows(db.BudgetTransaction) if r.entry_reference == "in-window-conflict")
        self.assertTrue(in_row.uncertain)

        self.store.commit_snapshot("bank", _windowed_snapshot(in_a, date_from="2026-08-24", date_to="2026-11-21"))
        in_row = next(r for r in self.rows(db.BudgetTransaction) if r.entry_reference == "in-window-conflict")
        self.assertFalse(in_row.uncertain)
        self.assertEqual(self.rows(db.BudgetUnresolved), [])

    def test_runtime_acquire_preserves_max_history_days_and_late_page_failure_preserves_prior_state(self):
        root = Path(self.directory.name)
        key_file = root / "key.pem"
        session_file = root / "session.json"
        key_file.write_bytes(b"FAKE private key")
        session_file.write_text(json.dumps({"session_id": SESSION}), encoding="utf-8")
        key_file.chmod(0o600)
        session_file.chmod(0o600)

        cfg = config.BudgetConfig(
            APPLICATION, str(key_file),
            (config.ConnectionConfig("bnp-personal", "owner", "BNP Paribas", "PL",
                                     str(session_file), ("bnp-hash",), max_history_days=89),),
        )
        fixtures.prepare_selected(self.store, cfg)

        # Seed prior ledger + unresolved state one day earlier so the scheduler allows Round 1 at NOW.
        self.now = NOW - 86_400
        prior_tx = fixtures.transaction("prior-ok", booking_date="2026-07-01", amount="50")
        prior_pdng = fixtures.transaction("prior-pdng", status="PDNG", booking_date="2026-07-02", amount="12")
        self.store.commit_snapshot("bnp-personal",
                                   _windowed_snapshot(prior_tx, prior_pdng, account_hash="bnp-hash",
                                                      date_from="2025-11-22", date_to="2026-11-20"))
        self.now = NOW

        mono = [0.0]

        def advance(seconds):
            mono[0] += seconds
            self.now += seconds

        # Round 1 via runtime.acquire: succeeds with 89-day window, preserving older ledger & unresolved rows.
        client_ok = _RecordingClient([
            {"transactions": [_raw_tx("new-tx", "2026-11-20", amount="7.50")], "continuation_key": None},
        ], primary="bnp-hash")
        scheduler = BudgetScheduler(self.store, application_id=APPLICATION)
        state, error = runtime.acquire(
            cfg, cfg.connections[0], self.store, scheduler,
            lambda _app, _key: client_ok,
            clock=lambda: self.now, monotonic=lambda: mono[0], sleeper=advance,
        )
        self.assertEqual((state, error), ("updated", None))
        tx_queries = [q for path, q in client_ok.calls if path == f"/accounts/{UID}/transactions"]
        self.assertEqual(tx_queries, [{"date_from": "2026-08-24", "date_to": "2026-11-21"}])
        self.assertEqual({row.entry_reference for row in self.rows(db.BudgetTransaction)}, {"prior-ok", "new-tx"})
        self.assertEqual([json.loads(u.payload_json)["entry_reference"] for u in self.rows(db.BudgetUnresolved)],
                         ["prior-pdng"])

        # Round 2 via runtime.acquire after cooldown: page 2 fails; zero page-1 records leak and prior state survives.
        self.now += 50_000
        client_fail = _RecordingClient([
            {"transactions": [_raw_tx("uncommitted-p1", "2026-11-21", amount="99.00")], "continuation_key": "p2"},
            BudgetAPIError(400, "provider_error", provider_error="ASPSP_ERROR"),
        ], primary="bnp-hash")
        state, error = runtime.acquire(
            cfg, cfg.connections[0], self.store, scheduler,
            lambda _app, _key: client_fail,
            clock=lambda: self.now, monotonic=lambda: mono[0], sleeper=advance,
        )
        self.assertEqual((state, error), ("partial", "provider_error"))
        self.assertEqual({row.entry_reference for row in self.rows(db.BudgetTransaction)}, {"prior-ok", "new-tx"})
        self.assertEqual([json.loads(u.payload_json)["entry_reference"] for u in self.rows(db.BudgetUnresolved)],
                         ["prior-pdng"])


if __name__ == "__main__":
    unittest.main()
