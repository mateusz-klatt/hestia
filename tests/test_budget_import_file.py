"""One-account imports use only synthetic documents and private temporary files."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from hestia.budget import config, import_file, normalize
from hestia.budget.import_file import BudgetImportError, ImportSnapshot, load_import, parse_import
from tests.test_budget_config import sample
from tests.test_budget_normalize import balance, transaction

NOW = datetime(2026, 11, 20, 12, tzinfo=timezone.utc).timestamp()
PRIMARY = "explicit-account-hash"


def document():
    return {"version": 1, "connection_id": "personal-ing", "aspsp": {"name": "ING Bank Śląski", "country": "PL"},
            "identification_hash": PRIMARY, "fetched_at": "2026-11-20T12:00:00Z", "complete": True,
            "transactions": {"transactions": [transaction()], "continuation_key": None},
            "balances": {"balances": [balance()]}}


def encoded(raw):
    return json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class ImportParserTests(unittest.TestCase):
    def setUp(self):
        self.config = config.parse_config(sample())

    def parse(self, raw=None, configuration=None, now=NOW):
        return parse_import(document() if raw is None else raw,
                            self.config if configuration is None else configuration, now=now)

    def assert_invalid(self, raw, code=None):
        with self.assertRaises(BudgetImportError) as caught:
            self.parse(raw)
        if code is not None:
            self.assertEqual(caught.exception.code, code)
        self.assertNotIn("SECRET", str(caught.exception))

    def test_exact_frozen_contract_normalization_and_optional_balance_semantics(self):
        result = self.parse()
        self.assertIsInstance(result, ImportSnapshot)
        self.assertEqual(result.connection_id, "personal-ing")
        self.assertEqual(result.identification_hash, PRIMARY)
        self.assertEqual(result.fetched_at, NOW)
        self.assertEqual(result.transactions, (normalize.normalize_transaction(transaction()),))
        self.assertEqual(result.balances, normalize.normalize_balances({"balances": [balance()]}))
        self.assertRegex(result.digest, r"^[a-f0-9]{64}$")
        with self.assertRaises(FrozenInstanceError):
            result.connection_id = "changed"
        raw = document(); del raw["balances"]; del raw["transactions"]["continuation_key"]
        absent = self.parse(raw)
        self.assertIsNone(absent.balances)
        raw["balances"] = {"balances": []}
        empty = self.parse(raw)
        self.assertEqual(empty.balances, ())
        self.assertNotEqual(absent.digest, empty.digest)
        raw["transactions"]["transactions"] = []
        self.assertEqual(self.parse(raw).transactions, ())

    def test_no_history_filter_and_missing_reference_multiplicity_conflicts_survive(self):
        raw = document()
        old = transaction("old", booking_date="2010-01-01")
        missing = transaction(None, booking_date=None, value_date=None)
        conflict = transaction("old", status="PDNG", booking_date="2026-11-20")
        raw["transactions"]["transactions"] = [old, old, missing, missing, conflict, conflict]
        result = self.parse(raw)
        self.assertEqual([row["entry_reference"] for row in result.transactions], ["old", None, None, "old"])
        self.assertEqual(result.transactions[0]["booking_date"], "2010-01-01")
        self.assertEqual(result.transactions[-1]["status"], "PDNG")
        self.assertIsNone(result.transactions[1]["booking_date"])
        self.assertIsNone(result.transactions[1]["value_date"])

    def test_digest_ignores_order_volatile_ids_and_noncanonical_money_but_keeps_multiplicity(self):
        raw = document()
        raw["transactions"]["transactions"].extend([transaction("second"), transaction(None), transaction(None)])
        raw["balances"]["balances"].append(balance(balance_type="ITAV", balance_amount={"amount": "1", "currency": "EUR"}))
        first = self.parse(raw)
        reordered = deepcopy(raw)
        reordered["transactions"]["transactions"].reverse()
        reordered["balances"]["balances"].reverse()
        reordered["fetched_at"] = "2026-11-20T14:00:00+02:00"
        for row in reordered["transactions"]["transactions"]:
            row["transaction_id"] = "changed-transient-id"
            row["transaction_amount"]["amount"] = "123.45"
        reordered["transactions"]["transactions"].append(deepcopy(reordered["transactions"]["transactions"][-1]))
        self.assertEqual(self.parse(reordered).digest, first.digest)
        missing_index = next(index for index, row in enumerate(reordered["transactions"]["transactions"])
                             if row["entry_reference"] is None)
        reordered["transactions"]["transactions"].pop(missing_index)
        self.assertNotEqual(self.parse(reordered).digest, first.digest)

    def test_digest_binds_application_owner_connection_bank_account_and_fetch_time(self):
        baseline = self.parse().digest
        connection = self.config.connections[0]
        cases = [({"application_id": "87654321-1234-4234-8234-123456789abc"}, {}, {}),
                 ({}, {"owner": "bob"}, {}),
                 ({}, {"id": "other"}, {"connection_id": "other"}),
                 ({}, {"bank_name": "Synthetic Bank"}, {"aspsp": {"name": "Synthetic Bank", "country": "PL"}}),
                 ({}, {"country": "DE"}, {"aspsp": {"name": connection.bank_name, "country": "DE"}}),
                 ({}, {"accounts": ("another-hash",)}, {"identification_hash": "another-hash"}),
                 ({}, {}, {"fetched_at": "2026-11-20T11:59:59Z"})]
        for outer, changes, payload in cases:
            with self.subTest(outer=outer, changes=changes, payload=payload):
                configured = replace(self.config, connections=(replace(connection, **changes),), **outer)
                self.assertNotEqual(self.parse({**document(), **payload}, configured).digest, baseline)
        raw = document(); raw["transactions"]["transactions"][0]["remittance_information"] = ["Changed"]
        self.assertNotEqual(self.parse(raw).digest, baseline)

    def test_outer_bank_and_response_objects_reject_missing_unknown_and_error_fields(self):
        for raw in ([], "SECRET", {}, {**document(), "error": "SECRET"}, {**document(), "errors": []}):
            with self.subTest(raw_type=type(raw).__name__):
                self.assert_invalid(raw, "invalid_import")
        for field in document():
            raw = document(); del raw[field]
            if field != "balances":
                self.assert_invalid(raw, "invalid_import")
        for field, invalid in (("aspsp", None), ("aspsp", {"name": "ING Bank Śląski"}),
                ("aspsp", {"name": "ING Bank Śląski", "country": "PL", "host": "SECRET"}),
                ("transactions", {"transactions": [], "error": "SECRET"}),
                ("transactions", {}), ("transactions", []),
                ("balances", None), ("balances", {}), ("balances", {"balances": [], "error": "SECRET"})):
            self.assert_invalid({**document(), field: invalid}, "invalid_import")
        with self.assertRaises(BudgetImportError):
            parse_import(None, self.config, now=NOW)

    def test_version_completion_and_continuation_are_strict(self):
        for version in (True, "1", 0, 2, None):
            self.assert_invalid({**document(), "version": version}, "invalid_import")
        for complete in (False, 1, "true", None, {}):
            self.assert_invalid({**document(), "complete": complete}, "incomplete_import")
        for key in ("more", "", False, 0, [], {}):
            raw = document(); raw["transactions"]["continuation_key"] = key
            self.assert_invalid(raw, "incomplete_import")

    def test_selection_requires_exact_configured_connection_primary_hash_and_bank(self):
        for changes in ({"connection_id": "missing"}, {"connection_id": None},
                        {"identification_hash": "missing"}, {"identification_hash": []},
                        {"aspsp": {"name": "Different Bank", "country": "PL"}},
                        {"aspsp": {"name": "ING Bank Śląski", "country": "DE"}}):
            self.assert_invalid({**document(), **changes}, "context_mismatch")
        for connections in ((), self.config.connections * 2,
                            (replace(self.config.connections[0], accounts=()),)):
            with self.assertRaises(BudgetImportError):
                self.parse(configuration=replace(self.config, connections=connections))

    def test_timestamp_is_aware_finite_nonnegative_not_future(self):
        for value in (None, 1, "2026-11-20", "2026-11-20T12:00:00", "SECRET-invalid-time", "2026-11-20T12:00:01Z",
                      "1969-12-31T23:59:59Z", "2026-11-20T12:00:00+25:00", "2026-11-20T" + "0" * 65):
            self.assert_invalid({**document(), "fetched_at": value}, "invalid_timestamp")
        for now in (None, True, "SECRET", -1, float("nan"), float("inf"), 10 ** 1000):
            with self.subTest(now_type=type(now).__name__), self.assertRaises(BudgetImportError) as caught:
                self.parse(now=now)
            self.assertEqual(caught.exception.code, "invalid_timestamp")
        self.assertEqual(self.parse({**document(), "fetched_at": "1970-01-01T00:00:00Z"}, now=0).fetched_at, 0)

    def test_raw_nonfinite_nonserializable_cycles_and_surrogates_are_rejected_safely(self):
        for value in (float("nan"), float("inf"), object(), "\ud800"):
            raw = document(); raw["transactions"]["transactions"][0]["ignored"] = value
            self.assert_invalid(raw, "invalid_json")
        raw = document(); raw["transactions"]["transactions"].append(raw)
        self.assert_invalid(raw, "invalid_json")
        raw = document(); nested = []
        raw["transactions"]["transactions"][0]["ignored"] = nested
        for _ in range(1200):
            child = []; nested.append(child); nested = child
        self.assert_invalid(raw, "invalid_json")

    def test_shared_normalization_rejects_bad_rows_money_dates_and_balances(self):
        for rows in (None, {}, [None], [transaction(transaction_amount={"amount": 1.2, "currency": "PLN"})],
                     [transaction(transaction_amount={"amount": "-1", "currency": "PLN"})],
                     [transaction(booking_date="SECRET")]):
            raw = document(); raw["transactions"]["transactions"] = rows
            self.assert_invalid(raw)
        for rows in (None, {}, [None], [balance(balance_type="SECRET")]):
            raw = document(); raw["balances"]["balances"] = rows
            self.assert_invalid(raw)

    def test_input_normalized_and_record_limits_apply_before_returning_any_snapshot(self):
        self.assertEqual(import_file.MAX_IMPORT_BYTES, 32 * 1024 * 1024)
        self.assertEqual(import_file.MAX_NORMALIZED_BYTES, 32 * 1024 * 1024)
        self.assertEqual(import_file.MAX_RECORDS, 100_000)
        raw = document()
        length = len(encoded(raw))
        with mock.patch.object(import_file, "MAX_IMPORT_BYTES", length):
            self.parse(raw)
        with mock.patch.object(import_file, "MAX_IMPORT_BYTES", length - 1):
            self.assert_invalid(raw, "limit_exceeded")
        tx = normalize.normalize_transaction(transaction())
        bal = normalize.normalize_balances({"balances": [balance()]})[0]
        tx_length = len(json.dumps(tx, separators=(",", ":")).encode())
        normalized_length = tx_length + len(json.dumps(bal, separators=(",", ":")).encode())
        with mock.patch.object(import_file, "MAX_NORMALIZED_BYTES", normalized_length):
            self.parse(raw)
        for length in (tx_length - 1, normalized_length - 1):
            with mock.patch.object(import_file, "MAX_NORMALIZED_BYTES", length):
                self.assert_invalid(raw, "limit_exceeded")
        with mock.patch.object(import_file, "MAX_RECORDS", 2):
            self.parse(raw)
        with mock.patch.object(import_file, "MAX_RECORDS", 1):
            self.assert_invalid(raw, "limit_exceeded")
        raw["transactions"]["transactions"] *= 3
        with mock.patch.object(import_file, "MAX_RECORDS", 2):
            self.assert_invalid(raw, "limit_exceeded")

    def test_unknown_error_codes_cannot_expose_source_text(self):
        for value in ("SECRET private body", [], None):
            error = BudgetImportError(value)
            self.assertEqual(error.code, "invalid_import")
            self.assertEqual(str(error), "Invalid budget import: invalid_import")


class ImportFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "SECRET-bank-import.json"
        self.path.write_bytes(encoded(document()))
        self.path.chmod(0o600)
        self.config = config.parse_config(sample())

    def load(self, path=None):
        return load_import(self.path if path is None else path, self.config, now=NOW)

    def test_reads_private_synthetic_json_and_whitespace_does_not_affect_digest(self):
        compact = self.load()
        self.assertEqual(compact, parse_import(document(), self.config, now=NOW))
        self.path.write_text(json.dumps(document(), indent=2, sort_keys=True), encoding="utf-8")
        self.path.chmod(0o400)
        self.assertEqual(self.load().digest, compact.digest)

    def test_rejects_malformed_duplicate_nonfinite_utf8_or_deep_json(self):
        overflow = encoded(document()).replace(b'"transient-detail-id"', b'1e10000')
        contents = [b"SECRET malformed", b"\xff", b'{"version":1,"version":1}',
                    b'{"transactions":{"transactions":[],"transactions":[]}}',
                    b'{"value":NaN}', b'{"value":Infinity}', overflow,
                    encoded(document()).replace(b'"transient-detail-id"', b"[" * 1200 + b"]" * 1200)]
        for content in contents:
            with self.subTest(content_length=len(content)):
                self.path.write_bytes(content)
                with self.assertRaises(BudgetImportError) as caught:
                    self.load()
                self.assertEqual(caught.exception.code, "invalid_json")
                self.assertNotIn("SECRET", str(caught.exception))

    def test_reader_rejects_symlink_missing_directory_wrong_owner_and_permissions(self):
        link = self.path.with_name("link.json"); link.symlink_to(self.path)
        for path in (link, self.path.parent, self.path.with_name("missing"), [], "bad\x00SECRET"):
            with self.subTest(path_type=type(path).__name__), self.assertRaises(BudgetImportError) as caught:
                load_import(path, self.config, now=NOW)
            self.assertEqual(caught.exception.code, "invalid_file")
            self.assertNotIn("SECRET", str(caught.exception))
        for permission in (0o644, 0o640, 0o700):
            self.path.chmod(permission)
            with self.assertRaises(BudgetImportError):
                self.load()
        self.path.chmod(0o600)
        with mock.patch.object(config.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(BudgetImportError):
                self.load()

    def test_reader_enforces_size_limit_and_growth_after_stat(self):
        length = self.path.stat().st_size
        with mock.patch.object(import_file, "MAX_IMPORT_BYTES", length):
            self.load()
            self.path.write_bytes(self.path.read_bytes() + b" ")
            with self.assertRaises(BudgetImportError) as caught:
                self.load()
            self.assertEqual(caught.exception.code, "invalid_file")
        self.path.write_bytes(encoded(document()))
        original = os.fstat
        def grow(descriptor):
            info = original(descriptor)
            self.path.write_bytes(b"x" * (length + 1))
            return info
        with mock.patch.object(import_file, "MAX_IMPORT_BYTES", length), mock.patch.object(config.os, "fstat", side_effect=grow):
            with self.assertRaises(BudgetImportError):
                self.load()
