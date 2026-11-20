"""Private budget wire DTOs: strict controls, typed reads and codegen metadata."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from hestia import api_contract as contract
from hestia import db
from hestia.budget.store import BudgetControlConflict, BudgetStore
from hestia.budget.views import BudgetHTTPStore
from tests.test_budget_store import configuration, prepare_selected, snapshot, transaction


CAPABILITIES = {"stored_reads": True, "account_controls": True, "refresh": False,
                "background_selection": False, "consent": False}
COMPONENT = {"last_attempt": None, "last_success": None, "last_error": None, "failures": 0,
             "next_attempt": 0, "state": "never", "retry_at": None, "suspended": False,
             "stale": True, "age_seconds": None, "stale_after_seconds": 86400}
ACCOUNT = {"id": "local-account", "connection": "local-connection", "configured": True, "selected": False,
           "active": False, "paused": False, "control_version": 1, "kind": "unknown",
           "kind_confirmed_at": None, "kind_confirmed_by": None, "kind_confirmation_origin": None,
           "components": {"transactions": COMPONENT, "balances": COMPONENT}}
CONNECTION = {"id": "local-connection", "enabled": True, "paused": False, "control_version": 1,
              "bank_name": "Synthetic Bank", "country": "PL", "valid_until": None, "renewal_due": False,
              "last_attempt": None, "last_success": None, "last_error": None}
TRANSACTION = {"entry_reference": "synthetic-reference", "amount": "12345678901234567890.001",
               "currency": "PLN", "credit_debit_indicator": "DBIT", "status": "BOOK",
               "booking_date": "2026-11-20", "value_date": None, "transaction_date": None,
               "remittance_information": ["Synthetic fixture"], "source": "api", "observed_at": 1000,
               "api_observed_at": 1000}
BALANCE = {"name": "Synthetic booked balance", "balance_type": "CLBD",
           "balance_amount": {"amount": "-15.001", "currency": "PLN"}, "reference_date": None}


class BudgetRequestContractTests(unittest.TestCase):
    CASES = ((contract.BudgetSelectionRequest, {"account_id": "local-account", "selected": True, "expected_version": 1}),
             (contract.BudgetKindRequest, {"account_id": "local-account", "kind": "credit_card", "expected_version": 1}),
             (contract.BudgetConnectionStateRequest, {"connection_id": "local-connection", "paused": False, "expected_version": 1}))

    def test_valid_controls_roundtrip_without_coercing_values_or_adding_defaults(self):
        for model, payload in self.CASES:
            with self.subTest(model=model.__name__):
                self.assertEqual(model.model_validate(payload).model_dump(), payload)
                self.assertEqual(model.model_validate_json(json.dumps(payload)).model_dump(), payload)

    def test_all_controls_reject_missing_null_noninteger_and_bool_versions(self):
        for model, payload in self.CASES:
            for version in (None, True, False, "1", 1.0, 0, -1, [], {}):
                with self.subTest(model=model.__name__, version=version), self.assertRaises(ValueError):
                    model.model_validate({**payload, "expected_version": version})
            without = {key: value for key, value in payload.items() if key != "expected_version"}
            with self.subTest(model=model.__name__, version="absent"), self.assertRaises(ValueError):
                model.model_validate(without)

    def test_all_controls_reject_unknown_keys_and_invalid_resource_ids(self):
        for model, payload in self.CASES:
            key = "account_id" if "account_id" in payload else "connection_id"
            for identifier in (None, 1, True, "", "x" * 129, [], {}):
                with self.subTest(model=model.__name__, identifier=identifier), self.assertRaises(ValueError):
                    model.model_validate({**payload, key: identifier})
            with self.subTest(model=model.__name__, key="extra"), self.assertRaises(ValueError):
                model.model_validate({**payload, "owner": "another-user"})
            self.assertEqual(getattr(model.model_validate({**payload, key: "x" * 128}), key), "x" * 128)

    def test_boolean_controls_are_exact_and_kind_cannot_be_inferred_or_cleared(self):
        for model, field, payload in ((self.CASES[0][0], "selected", self.CASES[0][1]),
                                      (self.CASES[2][0], "paused", self.CASES[2][1])):
            for value in (None, 0, 1, "true", [], {}):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    model.model_validate({**payload, field: value})
        for value in ("unknown", "PLN", "credit", None, True, 1):
            with self.subTest(kind=value), self.assertRaises(ValueError):
                contract.BudgetKindRequest.model_validate({"account_id": "local-account", "kind": value, "expected_version": 1})


class BudgetReadContractTests(unittest.TestCase):
    def test_display_additions_are_optional_typed_and_forward_compatible(self):
        legacy = contract.BudgetAccount.model_validate(ACCOUNT)
        self.assertIsNone(legacy.display)
        self.assertEqual(legacy.observed_balance_currencies, [])
        display = {"product": "Synthetic Card", "currency": "CHF", "masked_suffix": "A123",
                   "source": "future_metadata_source", "observed_at": 900, "imported_at": 1000}
        metadata = contract.BudgetAccount.model_validate({**ACCOUNT, "display": display,
                                                         "observed_balance_currencies": ["EUR", "PLN"]})
        self.assertEqual(metadata.display.source, "future_metadata_source")
        self.assertEqual(metadata.display.currency, "CHF")
        self.assertEqual(metadata.observed_balance_currencies, ["EUR", "PLN"])
        contract.BudgetAccountDisplay.model_validate({**display, "product": None, "currency": None, "masked_suffix": None})
        for private_field in ("digest", "identification_hash", "uid", "name", "iban", "account_number"):
            with self.subTest(private_field=private_field), self.assertRaises(ValueError):
                contract.BudgetAccountDisplay.model_validate({**display, private_field: "not-public"})

    def test_named_packets_and_capabilities_describe_only_implemented_operations(self):
        reads = ((contract.BudgetConnections, {"connections": [CONNECTION], "capabilities": CAPABILITIES}),
                 (contract.BudgetAccounts, {"accounts": [ACCOUNT]}),
                 (contract.BudgetStatus, {"status": []}), (contract.BudgetLimits, {"limits": []}))
        for model, payload in reads:
            with self.subTest(model=model.__name__):
                model.model_validate(payload)
                with self.assertRaises(ValueError):
                    model.model_validate({**payload, "private_session": "not-a-wire-field"})
        future = contract.BudgetCapabilities.model_validate({**CAPABILITIES, "refresh": True})
        self.assertTrue(future.refresh)

    def test_success_and_conflict_entities_have_versions_components_and_changed_noop(self):
        for model, key, entity in ((contract.BudgetAccountSaved, "account", ACCOUNT),
                                    (contract.BudgetConnectionSaved, "connection", CONNECTION)):
            saved = model.model_validate({"ok": True, key: {**entity, "changed": False}})
            self.assertFalse(getattr(saved, key).changed)
            conflict = contract.BudgetConflict.model_validate(
                {"ok": False, "error": "budget_version_conflict", "current": entity})
            self.assertEqual(conflict.current.control_version, 1)
            with self.assertRaises(ValueError):
                model.model_validate({"ok": True, key: entity})
            with self.assertRaises(ValueError):
                contract.BudgetConflict.model_validate({"ok": False, "error": "budget_version_conflict",
                                                       "current": {**entity, "identification_hash": "not-public"}})
        with self.assertRaises(ValueError):
            contract.BudgetAccount.model_validate({key: value for key, value in ACCOUNT.items() if key != "components"})

    def test_transactions_keep_exact_strings_dates_provenance_and_uncertain_total(self):
        page = {"account_id": "local-account", "transactions": [TRANSACTION], "account_total": 5,
                "uncertain_total": 2, "next_cursor": "opaque-cursor"}
        parsed = contract.BudgetTransactions.model_validate(page)
        self.assertEqual(parsed.transactions[0].amount, "12345678901234567890.001")
        self.assertEqual(parsed.uncertain_total, 2)
        self.assertEqual(contract.BudgetTransaction.model_validate({**TRANSACTION, "source": "future_source"}).source,
                         "future_source")
        self.assertEqual(contract.BudgetAccount.model_validate({**ACCOUNT, "kind": "future_kind"}).kind, "future_kind")
        self.assertEqual(contract.BudgetComponentStatus.model_validate({**COMPONENT, "state": "future_state"}).state,
                         "future_state")
        for changes in ({"amount": 1.5}, {"amount": "-1"}, {"status": "PDNG"},
                        {"transaction_id": "volatile-provider-id"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                contract.BudgetTransaction.model_validate({**TRANSACTION, **changes})

    def test_live_balances_remain_separate_from_archived_summary(self):
        payload = {"account_id": "local-account", "balances": [BALANCE], "balances_total": 201,
                   "balances_truncated": True, "freshness": COMPONENT,
                   "archived_imports": [{"id": "archive", "fetched_at": 900, "imported_at": 950, "balance_count": 2}],
                   "archived_has_more": False}
        result = contract.BudgetBalances.model_validate(payload)
        self.assertEqual(result.balances[0].balance_amount.amount, "-15.001")
        self.assertEqual(result.archived_imports[0].balance_count, 2)
        with self.assertRaises(ValueError):
            contract.BudgetBalance.model_validate({**BALANCE, "balance_amount": {"amount": -15.001, "currency": "PLN"}})
        with self.assertRaises(ValueError):
            contract.BudgetArchivedBalances.model_validate({**payload["archived_imports"][0], "balance_amount": "never-mixed"})

    def test_limit_union_requires_account_and_operation_only_for_component_scope(self):
        common = {"connection": "local-connection", "suspended": False, "retry_at": None, "next_attempt": 1000,
                  "backoff_until": 0, "lease_until": None, "last_attempt": None, "last_success": None,
                  "last_request_at": None, "next_request_at": 0}
        shared = {**common, "scope": "application"}
        component = {**common, "scope": "component", "account": "local-account", "operation": "transactions"}
        contract.BudgetLimits.model_validate({"limits": [shared, component]})
        for invalid in ({**shared, "key": "private-app-uuid"}, {**shared, "account": "not-allowed"},
                        {**component, "operation": "details"}, {**common, "scope": "component"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                contract.BudgetLimits.model_validate({"limits": [invalid]})


class BudgetOpenApiTests(unittest.TestCase):
    def test_only_nine_phase_one_routes_are_declared_and_all_require_admin(self):
        document = contract.build_openapi()
        budget = {path: methods for path, methods in document["paths"].items() if path.startswith("/api/budget/")}
        self.assertEqual({path.rsplit("/", 1)[1] for path in budget},
                         {"connections", "accounts", "status", "transactions", "balances", "limits",
                          "account-selection", "account-kind", "connection-state"})
        for path, methods in budget.items():
            method = "post" if path.endswith(("account-selection", "account-kind", "connection-state")) else "get"
            self.assertEqual(set(methods), {method})
            self.assertEqual(methods[method]["x-required-role"], "admin")
        self.assertEqual(document["info"]["version"], "0.14.0")

    def test_query_metadata_pins_required_account_dates_limit_and_opaque_cursor(self):
        paths = contract.build_openapi()["paths"]
        query = {p["name"]: p for p in paths["/api/budget/transactions"]["get"]["parameters"]}
        self.assertEqual(set(query), {"account_id", "date_from", "date_to", "limit", "cursor"})
        self.assertTrue(query["account_id"]["required"])
        self.assertEqual(query["limit"]["schema"], {"type": "integer", "minimum": 1, "maximum": 200, "default": 50})
        self.assertEqual(query["cursor"]["schema"]["maxLength"], 8192)
        for name in ("date_from", "date_to"):
            self.assertEqual(query[name]["schema"]["format"], "date")
        self.assertEqual([p["name"] for p in paths["/api/budget/balances"]["get"]["parameters"]], ["account_id"])
        self.assertEqual([p["name"] for p in paths["/api/budget/accounts"]["get"]["parameters"]], ["connection_id"])

    def test_budget_requests_stay_strict_and_all_typed_response_dependencies_are_tolerant(self):
        schemas = contract.build_openapi()["components"]["schemas"]
        request_names = {model.__name__ for model, _ in BudgetRequestContractTests.CASES}
        for name, schema in schemas.items():
            if name in request_names:
                self.assertIs(schema["additionalProperties"], False)
                self.assertEqual(schema["properties"]["expected_version"]["minimum"], 1)
            elif name.startswith("Budget"):
                self.assertNotIn("additionalProperties", schema)
        for name in ("BudgetTransaction", "BudgetMoney"):
            self.assertEqual(schemas[name]["properties"]["amount"]["type"], "string")
        for model, _ in BudgetRequestContractTests.CASES:
            self.assertEqual(model.model_config["extra"], "forbid")
        future = deepcopy(COMPONENT)
        future["new_health_field"] = True
        with self.assertRaises(ValueError):
            contract.BudgetComponentStatus.model_validate(future)
        account = schemas["BudgetAccount"]
        for field in ("display", "observed_balance_currencies"):
            self.assertNotIn(field, account["required"])
        display = schemas["BudgetAccountDisplay"]
        self.assertEqual(set(display["properties"]), {"product", "currency", "masked_suffix", "source", "observed_at", "imported_at"})
        self.assertNotIn("enum", display["properties"]["source"])


class BudgetProviderBindingTests(unittest.TestCase):
    """Validate actual owner-private SQLite views, including nested response drift."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "contract.db"
        _, self.sessions = db.init_db(self.path)
        with self.sessions.begin() as session:
            session.add_all([db.AppMeta(key="users_authoritative", value="1"),
                             db.User(username="owner", password_hash="unused", role="admin")])
        self.now = 1000
        self.config = configuration()
        self.base = BudgetStore(self.sessions, clock=lambda: self.now)
        prepare_selected(self.base, self.config)
        self.views = BudgetHTTPStore(self.sessions, clock=lambda: self.now, cursor_secret=b"synthetic-cursor-secret")
        self.account_id = self.views.accounts("owner")[0]["id"]

    def tearDown(self):
        db.reset_engine_cache()
        self.directory.cleanup()

    def assert_reads_match(self):
        reads = ((contract.BudgetConnections, {"connections": self.views.connections("owner"), "capabilities": CAPABILITIES}),
                 (contract.BudgetAccounts, {"accounts": self.views.accounts("owner")}),
                 (contract.BudgetStatus, {"status": self.views.status("owner")}),
                 (contract.BudgetLimits, {"limits": self.views.limits("owner")}),
                 (contract.BudgetTransactions, self.views.transactions("owner", self.account_id, limit=1)),
                 (contract.BudgetBalances, self.views.balances("owner", self.account_id)))
        for model, payload in reads:
            wire = json.loads(json.dumps(payload))
            with self.subTest(model=model.__name__):
                self.assertEqual(model.model_validate(wire).model_dump(mode="json"), wire)

    def test_actual_empty_and_populated_views_match_all_six_response_models(self):
        self.assert_reads_match()
        self.base.commit_snapshot("bank", snapshot(transaction("visible"), transaction("uncertain"), transaction(None)))
        offline = transaction("offline")
        with self.sessions.begin() as session:
            session.get(db.BudgetTransaction, (self.account_id, "uncertain")).uncertain = True
            session.add(db.BudgetTransaction(account_id=self.account_id, entry_reference="offline", payload_json=json.dumps(offline),
                                            source="offline_import", uncertain=False, observed_at=900, api_observed_at=None))
            session.add(db.BudgetImportBatch(id="synthetic-batch", fingerprint="synthetic-digest", account_id=self.account_id,
                                            fetched_at=900, imported_at=950, balances_json=json.dumps([BALANCE]), report_json="{}"))
            session.add(db.BudgetRequestLimit(scope="application", key=self.config.application_id,
                                             suspended=True, retry_at=100000, next_attempt=100000))
            session.add(db.BudgetAccountDisplay(account_id=self.account_id, product="Synthetic product", currency="CHF",
                                                masked_suffix="A123", source="offline_import", observed_at=900,
                                                imported_at=1000, digest="private-binding-digest"))
        self.assert_reads_match()
        page = self.views.transactions("owner", self.account_id, limit=1)
        self.assertIsNotNone(page["next_cursor"])
        self.assertEqual((page["account_total"], page["uncertain_total"]), (2, 1))
        filtered = contract.BudgetTransactions.model_validate(
            self.views.transactions("owner", self.account_id, date_from="2026-11-21", limit=1))
        self.assertEqual(filtered.transactions, [])
        self.assertEqual((filtered.account_total, filtered.uncertain_total), (2, 1))
        next_page = self.views.transactions("owner", self.account_id, limit=1, cursor=page["next_cursor"])
        contract.BudgetTransactions.model_validate(next_page)
        self.assertIsNone(next_page["next_cursor"])
        self.assertEqual(self.views.balances("owner", self.account_id)["archived_imports"][0]["balance_count"], 1)

    def test_real_control_noops_and_conflicts_keep_full_typed_safe_entities(self):
        version = self.views.accounts("owner")[0]["control_version"]
        changed = self.views.set_account_selected("owner", self.account_id, False,
                                                  expected_version=version, actor="owner", origin="web")
        contract.BudgetAccountSaved.model_validate({"ok": True, "account": changed})
        repeated = self.views.set_account_selected("owner", self.account_id, False,
                                                   expected_version=version, actor="owner", origin="web")
        parsed = contract.BudgetAccountSaved.model_validate({"ok": True, "account": repeated})
        self.assertFalse(parsed.account.changed)
        self.assertEqual(parsed.account.control_version, version + 1)
        with self.assertRaises(BudgetControlConflict) as conflict:
            self.views.confirm_account_kind("owner", self.account_id, "credit_card",
                                             expected_version=version, actor="owner", origin="web")
        account = contract.BudgetConflict.model_validate({"ok": False, "error": "budget_version_conflict",
                                                         "current": conflict.exception.current})
        self.assertEqual(account.current.control_version, version + 1)
        self.assertIsNotNone(account.current.components)
        connection = self.views.set_connection_paused("owner", "bank", True,
                                                       expected_version=1, actor="owner", origin="web")
        contract.BudgetConnectionSaved.model_validate({"ok": True, "connection": connection})
        repeated = self.views.set_connection_paused("owner", "bank", True,
                                                    expected_version=1, actor="owner", origin="web")
        self.assertFalse(contract.BudgetConnectionSaved.model_validate({"ok": True, "connection": repeated}).connection.changed)
        with self.assertRaises(BudgetControlConflict) as conflict:
            self.views.set_connection_paused("owner", "bank", False, expected_version=1, actor="owner", origin="web")
        current = contract.BudgetConflict.model_validate({"ok": False, "error": "budget_version_conflict",
                                                         "current": conflict.exception.current})
        self.assertTrue(current.current.paused)
