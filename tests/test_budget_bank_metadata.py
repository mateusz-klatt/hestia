"""Synthetic structured metadata; no provider calls, credentials or real records."""
import json
from dataclasses import replace
import unittest
from unittest import mock

from hestia import db
from hestia.budget import bank_metadata as metadata, normalize
from hestia.budget.analytics_core import financial_fingerprint
from hestia.budget import import_file
from hestia.budget.import_file import parse_import
from hestia.budget.import_file import ImportSnapshot
from tests.test_budget_import_file import document, NOW
from tests.test_budget_config import sample
from hestia.budget.config import parse_config
from tests import test_budget_store as fixtures
from tests.test_budget_normalize import FakeClient, SESSION, PRIMARY, TODAY, NOW as API_NOW, transaction

IBAN = "FI0455231152453547"  # Published API example, never a user's account.
IDS = ({"scheme": "IBAN", "identification": IBAN},)
_NRB_BODY = "00000000" + "0000000000000001"
NRB = f"{98 - int(_NRB_BODY + '252100') % 97:02}" + _NRB_BODY


class BankMetadataNormalizationTests(unittest.TestCase):
    def test_actual_structured_identities_only_with_checksum_and_scheme_boundaries(self):
        raw = {"account_id": {"iban": "fi04 5523 1152 4535 47", "other": {"scheme_name": "BBAN", "identification": " 0123-ABC "}},
               "all_account_ids": [{"scheme_name": "IBAN", "identification": IBAN},
                                   {"scheme_name": "IBAN", "identification": IBAN[:-1] + "8"}],
               "identification_hash": "not-an-account", "uid": "not-an-account"}
        self.assertEqual(metadata.normalize_account_identity(raw),
                         ({"scheme": "BBAN", "identification": "0123-ABC"}, *IDS))
        for raw in (None, [], {}, {"uid": IBAN, "identification_hash": IBAN}, {"account_id": []},
                    {"account_id": {"iban": "x" * 129}}, {"account_id": {"iban": "ＦＩ0455231152453547"}},
                    {"account_id": {"iban": "FI00"}}, {"account_id": {"iban": "FI04-5523-1152453547"}},
                    {"account_id": {"other": None}}, {"account_id": {"other": []}}, {"all_account_ids": {}},
                    {"all_account_ids": [{}] * 17}, {"all_account_ids": [None]},
                    {"all_account_ids": [{"scheme_name": "bad scheme", "identification": "x"}]},
                    {"all_account_ids": [{"scheme_name": "BBAN", "identification": "x\n"}]},
                    {"all_account_ids": [{"scheme_name": "BBAN", "identification": "ż"}]},
                    {"all_account_ids": [{"scheme_name": "BBAN", "identification": " "}]},
                    {"all_account_ids": [{"scheme_name": "BBAN", "identification": "x" * 257}]}):
            with self.subTest(raw=raw):
                self.assertEqual(metadata.normalize_account_identity(raw), ())

    def test_names_mcc_and_additional_ids_do_not_retain_unrelated_party_data(self):
        raw = {"creditor": {"name": "  Cafe\u0301\n  Synthetic\u200b ", "private_id": {"identification": "secret"}},
               "debtor": {"name": "Other"}, "creditor_account": {"iban": IBAN},
               "debtor_account_additional_identification": [{"scheme_name": "BBAN", "identification": "0123"}],
               "merchant_category_code": "0042", "arbitrary": "not retained"}
        self.assertEqual(metadata.normalize_transaction_metadata(raw),
                         {"creditor_name": "Café Synthetic", "debtor_name": "Other", "creditor_accounts": list(IDS),
                          "debtor_accounts": [{"scheme": "BBAN", "identification": "0123"}], "mcc": "0042"})
        for value in (None, 5, "", " ", "x" * 257, "x" * 1025):
            self.assertEqual(metadata.normalize_transaction_metadata({"creditor": {"name": value}}), {})
        for value in (None, 1234, "１２３４", "123", "12345"):
            self.assertEqual(metadata.normalize_transaction_metadata({"merchant_category_code": value}), {})
        self.assertEqual(metadata.normalize_transaction_metadata({"creditor": []}), {})
        self.assertEqual(metadata.normalize_transaction_metadata([]), {})
        self.assertEqual(metadata.normalize_account_identity({"account_id": {"other": {"scheme_name": "BBAN", "identification": "x"}}}),
                         ({"scheme": "BBAN", "identification": "x"},))
        for scheme in ("OAUT", "NIDN", "SOSE", "CCPT", "BANK", "CPAN", "MIBN", "CUSI", "UNRECOGNIZED"):
            self.assertEqual(metadata.normalize_account_identity({"all_account_ids": [
                {"scheme_name": scheme, "identification": "SYNTHETIC-PRIVATE-DO-NOT-RETAIN"}]}), ())
        for scheme in ("BGNR", "PGNR"):
            self.assertEqual(metadata.normalize_account_identity({"all_account_ids": [
                {"scheme_name": scheme, "identification": "012345"}]}),
                ({"scheme": scheme, "identification": "012345"},))

    def test_collector_digest_is_separate_and_bound_to_core_finances_and_context(self):
        core = normalize.normalize_transaction(transaction())
        collector = metadata.MetadataCollector({"account_id": {"iban": IBAN}})
        collector.add({"creditor": {"name": "Synthetic"}}, core)
        collector.add({"merchant_category_code": "0042"}, core)
        result = collector.finish(context={"owner": "owner"})
        self.assertEqual(result.account_identities, IDS)
        self.assertEqual(result.transactions[0].ledger_fingerprint, financial_fingerprint(core))
        self.assertEqual(result.transactions[0].payload, {"creditor_name": "Synthetic", "mcc": "0042"})
        self.assertNotEqual(result.digest, collector.finish(context={"owner": "other"}).digest)
        self.assertEqual(result.digest, collector.finish(context={"owner": "owner"}).digest)
        empty = metadata.MetadataCollector()
        for ref in (None, "", "x" * 1025):
            empty.add({"creditor": {"name": "Synthetic"}}, {**core, "entry_reference": ref})
        empty.add({}, core)
        self.assertIsNone(empty.finish())
        with mock.patch.object(metadata, "MAX_METADATA_BYTES", 1):
            limited = metadata.MetadataCollector()
            limited.add({"creditor": {"name": "Synthetic"}}, core)
        self.assertEqual(limited.finish().rejected, 1)
        self.assertEqual(limited.finish().transactions, ())

    def test_sidecar_matches_only_unambiguous_references_and_ignores_invalid_optional_data(self):
        core = normalize.normalize_transaction(transaction())
        sidecar = {"account": {"account_id": {"iban": IBAN}}, "transactions": [
            {"entry_reference": "entry-1", "creditor": {"name": "Synthetic"}}, None,
            {"entry_reference": "missing", "merchant_category_code": "1234"}], "unknown": "not retained"}
        result = metadata.parse_bank_metadata(sidecar, (core,))
        self.assertEqual(len(result.transactions), 1)
        self.assertEqual(result.rejected, 3)
        self.assertEqual(metadata.parse_bank_metadata(sidecar, (core, core)).transactions, ())
        for bad in (None, [], 4):
            self.assertEqual(metadata.parse_bank_metadata(bad, (core,)).rejected, 1)
        for bad in ({}, [None] * 100001):
            self.assertEqual(metadata.parse_bank_metadata({"transactions": bad}, (core,)).rejected, 1)

    def test_duplicate_financial_rows_with_disagreeing_metadata_do_not_pick_evidence(self):
        core = normalize.normalize_transaction(transaction())
        collector = metadata.MetadataCollector()
        collector.add({"creditor": {"name": "First"}, "merchant_category_code": "1234"}, core)
        collector.add({"creditor": {"name": "Second"}, "merchant_category_code": "1234"}, core)
        collector.add({"creditor": {"name": "First"}}, core)
        result = collector.finish()
        self.assertEqual(result.transactions[0].payload, {"mcc": "1234"})
        self.assertEqual(result.rejected, 1)
        empty = metadata.MetadataCollector()
        empty.add({"creditor": {"name": "First"}}, core)
        empty.add({"creditor": {"name": "Second"}}, core)
        self.assertEqual(empty.finish().transactions, ())

    def test_trusted_country_nrb_alias_and_explicit_iban_mislabeled_bban(self):
        raw = {"account_id": {"other": {"scheme_name": "BBAN", "identification": NRB}}}
        base = ({"scheme": "BBAN", "identification": NRB},)
        self.assertEqual(metadata.normalize_account_identity(raw), base)
        self.assertEqual(metadata.normalize_account_identity(raw, country="DE"), base)
        expected = (*base, {"scheme": "IBAN", "identification": "PL" + NRB})
        self.assertEqual(metadata.normalize_account_identity(raw, country="PL"), expected)
        invalid = {"account_id": {"other": {"scheme_name": "BBAN", "identification": NRB[:-1] + "2"}}}
        self.assertEqual(len(metadata.normalize_account_identity(invalid, country="PL")), 1)
        mislabeled = {"all_account_ids": [{"scheme_name": "BBAN", "identification": IBAN}]}
        self.assertEqual(metadata.normalize_account_identity(mislabeled),
                         ({"scheme": "BBAN", "identification": IBAN}, *IDS))
        raw_tx = {"creditor_account": raw["account_id"]}
        self.assertEqual(metadata.normalize_transaction_metadata(raw_tx, country="PL")["creditor_accounts"], list(expected))
        self.assertEqual(metadata.parse_bank_metadata({"account": raw}, (), context={"country": "PL"}).account_identities, expected)
        self.assertEqual(metadata.parse_bank_metadata({"account": raw}, (), country="PL").account_identities, expected)

    def test_identity_lengths_and_alias_overflow_fail_closed_before_matcher(self):
        def raw(value):
            return {"all_account_ids": [{"scheme_name": "BBAN", "identification": value}]}
        self.assertEqual(len(metadata.normalize_account_identity(raw("x" * 128))), 1)
        self.assertEqual(metadata.normalize_account_identity(raw("x" * 129)), ())
        values = [{"scheme_name": "BBAN", "identification": "synthetic-" + str(n)} for n in range(16)]
        self.assertEqual(len(metadata.normalize_account_identity({"all_account_ids": values})), 16)
        values[-1] = {"scheme_name": "BBAN", "identification": IBAN}
        overflow = {"all_account_ids": values}
        self.assertEqual(metadata.normalize_account_identity(overflow), ())
        self.assertEqual(metadata.MetadataCollector(overflow).finish().rejected, 1)
        transaction_raw = {"creditor_account": {"iban": IBAN},
                           "creditor_account_additional_identification": values}
        self.assertEqual(metadata.normalize_transaction_metadata(transaction_raw), {})
        self.assertEqual(metadata.parse_bank_metadata({"account": overflow}, ()).rejected, 1)
        collector = metadata.MetadataCollector({"all_account_ids": [
            {"scheme_name": "BBAN", "identification": "different-" + str(n)} for n in range(16)]})
        combined = metadata.parse_bank_metadata({"account": {"account_id": {"iban": IBAN}}}, (), collector=collector)
        self.assertEqual((combined.account_identities, combined.rejected), ((), 1))


class BankMetadataPersistenceTests(unittest.TestCase):
    setUp = fixtures.BudgetStoreTests.setUp
    tearDown = fixtures.BudgetStoreTests.tearDown
    rows = fixtures.BudgetStoreTests.rows

    def ready(self):
        fixtures.prepare_selected(self.store, fixtures.configuration())
        account = self.rows(db.BudgetAccount)[0]
        core = fixtures.transaction("entry-1")
        self.store.commit_snapshot("bank", fixtures.snapshot(core))
        return account.id, core, financial_fingerprint(core)

    def apply_tx(self, account, fp, patch, source="api", when=1000, ref="entry-1"):
        with self.sessions.begin() as session:
            return metadata.apply_transaction_metadata(session, account, ref, fp, patch, source, when)

    def test_identity_monotonic_source_protection_and_replay(self):
        account, _, _ = self.ready()
        def apply(patch=IDS, source="offline_import", when=10, target=account):
            with self.sessions.begin() as session:
                return metadata.apply_account_identity(session, target, patch, source, when)
        self.assertEqual(apply(patch=()), "empty")
        self.assertEqual(apply(), "updated")
        self.assertEqual(apply(), "unchanged")
        self.assertEqual(apply(when=9), "older")
        self.assertEqual(apply(source="api", when=20), "unchanged")
        alternate = ({"scheme": "BBAN", "identification": "synthetic-2"},)
        self.assertEqual(apply(alternate, "offline_import", 30), "older")
        self.assertEqual(apply(alternate, "api", 30), "updated")
        row = self.rows(db.BudgetAccountIdentity)[0]
        self.assertEqual((row.version, row.source, row.observed_at), (2, "api", 30))
        self.assertEqual(apply(target="unknown"), "invalid")
        self.assertEqual(apply(target=None), "invalid")
        for bad in (None, {}, [None], [{"scheme": "IBAN", "identification": "bad"}], list(IDS) * 19):
            self.assertEqual(apply(bad), "invalid")
        for source, stamp in (("other", 10), ("api", True), ("api", float("nan")), ("api", -1), ("api", 10**1000)):
            self.assertEqual(apply(source=source, when=stamp), "invalid")
        with self.sessions.begin() as session:
            session.get(db.BudgetAccountIdentity, account).owner = "other"
        self.assertEqual(apply(), "invalid")

    def test_transaction_metadata_per_field_provenance_fills_gaps_without_overwriting_api(self):
        account, core, fp = self.ready()
        self.assertEqual(self.apply_tx(account, fp, {"creditor_name": "API name"}), "updated")
        self.assertEqual(self.apply_tx(account, fp, {"creditor_name": "Import name", "creditor_accounts": list(IDS)},
                                      "offline_import", 100), "updated")
        row = self.rows(db.BudgetTransactionMetadata)[0]
        self.assertEqual(json.loads(row.payload_json), {"creditor_name": "API name", "creditor_accounts": list(IDS)})
        self.assertEqual(json.loads(row.provenance_json)["creditor_accounts"], {"source": "offline_import", "observed_at": 100})
        self.assertEqual((row.source, row.observed_at), ("api", 1000))
        self.assertEqual(self.apply_tx(account, fp, {"creditor_name": "Import name"}, "offline_import", 2000), "older")
        self.assertEqual(self.apply_tx(account, fp, {"creditor_accounts": list(IDS)}, "offline_import", 99), "older")
        self.assertEqual(self.apply_tx(account, fp, {"creditor_accounts": list(IDS)}, "api", 100), "unchanged")
        self.assertEqual(self.apply_tx(account, fp, {"creditor_name": "API name"}, "api", 1001), "unchanged")
        self.assertEqual(self.apply_tx(account, fp, {"creditor_name": "different"}, "api", 1001), "older")
        with self.sessions.begin() as session:
            row = session.get(db.BudgetTransactionMetadata, (account, "entry-1"))
            row.provenance_json = "{}"  # Existing fixture/legacy fallback.
        self.assertEqual(self.apply_tx(account, fp, {"mcc": "1234"}, "offline_import", 1), "updated")
        self.assertEqual(self.apply_tx(account, fp, {"creditor_name": "new API"}, "api", 1002), "updated")
        with self.sessions.begin() as session:
            row = session.get(db.BudgetTransaction, (account, "entry-1"))
            row.payload_json = json.dumps({**core, "amount": "2"})
        newfp = financial_fingerprint({**core, "amount": "2"})
        self.assertEqual(self.apply_tx(account, fp, {"mcc": "9999"}), "mismatch")
        self.assertEqual(self.apply_tx(account, newfp, {"debtor_name": "Current"}, "offline_import", 1), "updated")
        self.assertEqual(json.loads(self.rows(db.BudgetTransactionMetadata)[0].payload_json), {"debtor_name": "Current"})

    def test_invalid_optional_metadata_ownership_and_uncertainty_do_not_write(self):
        account, _, fp = self.ready()
        self.assertEqual(self.apply_tx(account, fp, {}), "empty")
        self.assertEqual(self.apply_tx(account, "bad", {"mcc": "1234"}), "invalid")
        self.assertEqual(self.apply_tx("unknown", fp, {"mcc": "1234"}), "invalid")
        self.assertEqual(self.apply_tx(account, fp, {"mcc": "1234"}, "invalid"), "invalid")
        self.assertEqual(self.apply_tx(account, fp, {"mcc": "1234"}, ref="missing"), "mismatch")
        for patch in (None, [], {"unknown": "x"}, {"mcc": 1234}, {"mcc": "12"},
                      {"creditor_name": ""}, {"creditor_name": " unnormalized "},
                      {"creditor_accounts": []}, {"creditor_accounts": [{"scheme": "IBAN", "identification": "bad"}]}):
            self.assertEqual(self.apply_tx(account, fp, patch), "invalid")
        self.assertEqual(self.rows(db.BudgetTransactionMetadata), [])
        self.apply_tx(account, fp, {"mcc": "1234"})
        with self.sessions.begin() as session:
            session.get(db.BudgetTransactionMetadata, (account, "entry-1")).owner = "other"
        self.assertEqual(self.apply_tx(account, fp, {"mcc": "1234"}), "invalid")
        with self.sessions.begin() as session:
            session.get(db.BudgetTransaction, (account, "entry-1")).uncertain = True
        self.assertEqual(self.apply_tx(account, fp, {"mcc": "1234"}), "mismatch")
        with self.sessions.begin() as session:
            session.get(db.BudgetConnection, "bank").owner = "other"
            self.assertEqual(metadata.apply_account_identity(session, account, IDS, "api", 10), "invalid")

    def test_duplicate_import_enriches_separately_without_reimporting_ledger_or_changing_controls(self):
        from tests.test_budget_import_store import quota_state
        account, core, fp = self.ready()
        config = fixtures.configuration()
        base = ImportSnapshot("bank", config.connections[0].accounts[0], 100, (core,), None, "a" * 64)
        first = self.store.import_snapshot(config, base)
        before_ledger = self.rows(db.BudgetTransaction)[0].payload_json
        before_quota = quota_state(self.rows)
        before_controls = [(a.id, a.enabled, a.configured, a.control_version) for a in self.rows(db.BudgetAccount)]
        sidecar = metadata.BankMetadata(IDS, (metadata.TransactionMetadata("entry-1", fp, {"mcc": "1234"}),), "b" * 64, 2)
        enriched = replace(base, bank_metadata=sidecar)
        result = self.store.import_snapshot(config, enriched)
        self.assertTrue(result["duplicate"])
        self.assertEqual(result["batch_id"], first["batch_id"])
        self.assertEqual(result["bank_metadata"]["updated"], 2)
        self.assertEqual(result["bank_metadata"]["rejected"], 2)
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(len(self.rows(db.BudgetImportBatch)), 1)
        self.assertEqual(self.rows(db.BudgetTransaction)[0].payload_json, before_ledger)
        self.assertEqual(quota_state(self.rows), before_quota)
        self.assertEqual([(a.id, a.enabled, a.configured, a.control_version) for a in self.rows(db.BudgetAccount)], before_controls)
        again = self.store.import_snapshot(config, enriched)
        self.assertEqual(again["bank_metadata"]["unchanged"], 2)
        newer = replace(enriched, digest="c" * 64, fetched_at=200)
        self.assertFalse(self.store.import_snapshot(config, newer)["duplicate"])
        self.assertEqual(len(self.rows(db.BudgetImportBatch)), 2)
        self.assertEqual(self.rows(db.BudgetTransactionMetadata)[0].version, 1)

    def test_api_commit_evidence_follows_successful_current_canonical_entry_only(self):
        account, core, fp = self.ready()
        sidecar = metadata.BankMetadata(IDS, (metadata.TransactionMetadata("entry-1", fp,
                                                                          {"creditor_accounts": list(IDS)}),))
        observed = normalize.AccountSnapshot("synthetic-hash", (), (core,), bank_metadata=sidecar)
        snapshot = normalize.Snapshot(1_000_000, (observed,), "2026-01-01", "2026-12-31")
        self.store.commit_snapshot("bank", snapshot)
        row = self.rows(db.BudgetTransactionMetadata)[0]
        self.assertEqual((row.source, row.ledger_fingerprint), ("api", fp))
        self.assertEqual(json.loads(row.payload_json), {"creditor_accounts": list(IDS)})
        self.assertEqual(self.rows(db.BudgetAccountIdentity)[0].source, "api")
        with self.sessions.begin() as session:
            report = metadata.apply_bank_metadata(session, account, metadata.BankMetadata(), source="api", observed_at=1001)
        self.assertEqual(report["empty"], 1)
        # Conflicting core observations become uncertain and cannot acquire evidence.
        changed = {**core, "amount": "2"}
        self.store.commit_snapshot("bank", replace(snapshot, accounts=(replace(observed, transactions=(core, changed)),)))
        self.assertTrue(self.rows(db.BudgetTransaction)[0].uncertain)
        self.assertEqual(self.rows(db.BudgetTransactionMetadata)[0].version, 1)


class BankMetadataHookTests(unittest.TestCase):
    def test_legacy_normalization_helpers_keep_optional_collector_defaults(self):
        raw = transaction()
        core = normalize.normalize_transaction(raw)
        self.assertEqual(list(normalize._normalized_records([raw], {"remaining": 1, "bytes": 10000})), [core])
        self.assertEqual(import_file._transactions({"transactions": [raw]}, {"records": 1, "bytes": 10000}), (core,))
        client = FakeClient()
        config = normalize.ConnectionConfig("bank", "owner", "Synthetic Bank", "PL", "/unused", (PRIMARY,))
        expiry, hashes = normalize._session_state(client, SESSION, config, API_NOW)
        self.assertGreater(expiry, API_NOW)
        self.assertIn(PRIMARY, hashes)

    def test_api_existing_requests_collect_sidecar_without_changing_core_or_failing_invalid_optional(self):
        # Reuse only a synthetic client/config; no mocked HTTP transport can leave this process.
        client = FakeClient()
        config = normalize.ConnectionConfig("bank", "owner", "Synthetic Bank", "PL", "/unused", (PRIMARY,))
        client.session["accounts_data"][0]["account_id"] = {"iban": IBAN}
        client.pages[next(iter(client.pages))][0]["transactions"][0].update(
            {"creditor": {"name": "Synthetic"}, "creditor_account": {"iban": IBAN}, "merchant_category_code": "bad"})
        result = normalize.fetch_snapshot(client, SESSION, config, today=TODAY, now=API_NOW)
        account = result.accounts[0]
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(account.transactions, (normalize.normalize_transaction(transaction()),))
        self.assertEqual(account.bank_metadata.account_identities, IDS)
        self.assertEqual(account.bank_metadata.rejected, 1)
        self.assertEqual(account.bank_metadata.transactions[0].payload["creditor_accounts"], list(IDS))
        client = FakeClient()
        nrb_account = {"other": {"scheme_name": "BBAN", "identification": NRB}}
        client.session["accounts_data"][0]["account_id"] = nrb_account
        client.pages[next(iter(client.pages))][0]["transactions"][0]["creditor_account"] = nrb_account
        observed = normalize.fetch_snapshot(client, SESSION, config, today=TODAY, now=API_NOW).accounts[0]
        self.assertIn({"scheme": "IBAN", "identification": "PL" + NRB}, observed.bank_metadata.account_identities)
        self.assertIn({"scheme": "IBAN", "identification": "PL" + NRB}, observed.bank_metadata.transactions[0].payload["creditor_accounts"])

    def test_incomplete_transaction_pages_do_not_publish_partial_metadata(self):
        from hestia.budget.client import BudgetAPIError
        client = FakeClient()
        config = normalize.ConnectionConfig("bank", "owner", "Synthetic Bank", "PL", "/unused", (PRIMARY,))
        uid = next(iter(client.pages))
        client.pages[uid][0]["transactions"][0]["creditor"] = {"name": "Partial must not publish"}
        client.pages[uid][0]["continuation_key"] = "next"
        client.pages[uid].append(BudgetAPIError(503, "unavailable"))
        result = normalize.fetch_snapshot(client, SESSION, config, today=TODAY, now=API_NOW)
        self.assertIsNone(result.accounts[0].transactions)
        self.assertIsNone(result.accounts[0].bank_metadata)
        self.assertEqual(result.accounts[0].transactions_error, "unavailable")

    def test_import_sidecar_does_not_change_core_batch_digest(self):
        config = parse_config(sample())
        raw = document()
        plain = parse_import(raw, config, now=NOW)
        self.assertIsNone(plain.bank_metadata)
        raw["bank_metadata"] = {"account": {"account_id": {"iban": IBAN}},
                                "transactions": [{"entry_reference": "entry-1", "creditor": {"name": "Synthetic"}}]}
        enriched = parse_import(raw, config, now=NOW)
        self.assertEqual((plain.digest, plain.transactions), (enriched.digest, enriched.transactions))
        self.assertEqual(enriched.bank_metadata.account_identities, IDS)
        raw["bank_metadata"] = "invalid optional block"
        rejected = parse_import(raw, config, now=NOW)
        self.assertEqual(rejected.digest, plain.digest)
        self.assertEqual(rejected.bank_metadata.rejected, 1)
        raw["bank_metadata"] = {"account": {"account_id": {"other": {"scheme_name": "BBAN", "identification": NRB}}}}
        self.assertIn({"scheme": "IBAN", "identification": "PL" + NRB},
                      parse_import(raw, config, now=NOW).bank_metadata.account_identities)


if __name__ == "__main__":
    unittest.main()
