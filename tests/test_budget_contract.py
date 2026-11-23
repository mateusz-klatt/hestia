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
                "background_selection": True, "background_polling": False, "consent": False, "analytics": False}
COMPONENT = {"last_attempt": None, "last_success": None, "last_error": None, "failures": 0,
             "next_attempt": 0, "state": "never", "retry_at": None, "suspended": False,
             "stale": True, "age_seconds": None, "stale_after_seconds": 86400}
ACCOUNT = {"id": "local-account", "connection": "local-connection", "configured": True, "selected": False,
           "active": False, "paused": False, "control_version": 1, "kind": "unknown",
           "kind_confirmed_at": None, "kind_confirmed_by": None, "kind_confirmation_origin": None,
           "components": {"transactions": COMPONENT, "balances": COMPONENT}}
CONNECTION = {"id": "local-connection", "enabled": True, "paused": False, "background_enabled": False,
              "attention": None, "control_version": 1,
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
             (contract.BudgetConnectionStateRequest, {"connection_id": "local-connection", "paused": False, "expected_version": 1}),
             (contract.BudgetBackgroundSelectionRequest, {"connection_id": "local-connection", "enabled": True, "expected_version": 1}),
             (contract.BudgetRefreshRequest, {"connection_id": "local-connection", "expected_version": 1,
                                              "account_id": "local-account", "operation": "balances"}),
             (contract.BudgetConsentStartRequest, {"connection_id": "local-connection", "expected_version": 1,
                                                   "intent": "renew"}))

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
            key = "connection_id" if "connection_id" in payload else "account_id"
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

    def test_consent_uses_local_flow_ids_and_rejects_caller_credentials(self):
        value = {"flow_id": "11111111-1111-4111-8111-111111111111"}
        self.assertEqual(contract.BudgetConsentFlowRequest.model_validate(value).model_dump(), value)
        for changes in ({"flow_id": "other"}, {"session_id": "secret"}, {"code": "secret"}, {"state": "secret"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                contract.BudgetConsentFlowRequest.model_validate({**value, **changes})
        with self.assertRaises(ValueError):
            contract.BudgetConsentStartRequest.model_validate({"connection_id": "local", "expected_version": 1,
                                                               "intent": "business"})


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
    def test_only_implemented_routes_are_declared_with_exact_callback_exception(self):
        document = contract.build_openapi()
        budget = {path: methods for path, methods in document["paths"].items() if path.startswith("/api/budget/")}
        posts = ("account-selection", "account-kind", "connection-state", "background-selection", "refresh")
        consent = {path: methods for path, methods in budget.items() if "/consent/" in path or "/enable-banking/" in path}
        analytics = {path: methods for path, methods in budget.items() if "/analytics/" in path}
        household = {path: methods for path, methods in budget.items() if "/household/" in path}
        receipts = {path: methods for path, methods in budget.items() if "/receipts/" in path}
        stored = {path: methods for path, methods in budget.items()
                  if path not in consent and path not in analytics and path not in household and path not in receipts}
        self.assertEqual({path.rsplit("/", 1)[1] for path in stored},
                         {"connections", "accounts", "status", "transactions", "balances", "limits", "jobs", *posts})
        for path, methods in stored.items():
            method = "post" if path.endswith(posts) else "get"
            self.assertEqual(set(methods), {method})
            self.assertEqual(methods[method]["x-required-role"], "admin")
        self.assertEqual(set(consent), {"/api/budget/consent/" + name for name in
            ("pending", "status", "renewal-reminders", "reconcile", "start", "complete", "cancel")} | {"/api/budget/enable-banking/callback"})
        for path, methods in consent.items():
            for entry in methods.values():
                self.assertEqual(entry["x-required-role"], "public" if path.endswith("callback") else "admin")
        for read_path in ("/api/budget/consent/renewal-reminders", "/api/budget/consent/reconcile"):
            self.assertIn("413", consent[read_path]["get"]["responses"])
        analytics_reads = {"overview", "transactions", "categories", "rules", "transfers", "recurring",
                           "spending-limits", "mortgages", "inbox", "quarantine", "forecast",
                           "monthly-budget", "monthly-budget/charts"}
        analytics_writes = {"transactions-by-keys", "category-create", "category-update", "annotation", "rule-preview",
                            "rule-save", "rule-batch-preview", "rule-batch-save",
                            "transfer-decision", "spending-limit-save", "mortgage-save", "mortgage-payment-set",
                            "inbox-mark"}
        self.assertEqual(set(analytics), {"/api/budget/analytics/" + name for name in analytics_reads | analytics_writes})
        for path, methods in analytics.items():
            method = "get" if path.removeprefix("/api/budget/analytics/") in analytics_reads else "post"
            self.assertEqual(set(methods), {method})
            self.assertEqual(methods[method]["x-required-role"], "admin")
            self.assertIn("413", methods[method]["responses"])
        household_reads = {"access", "report-accounts", "monthly-budget", "overview", "transactions", "categories",
                           "rules", "transfers", "spending-limits", "recurring", "mortgages", "inbox", "quarantine",
                           "monthly-budget-settings"}
        self.assertEqual(set(household), {"/api/budget/household/" + name for name in household_reads})
        for path, methods in household.items():
            self.assertEqual(set(methods), {"get"})
            self.assertEqual(methods["get"]["x-required-role"], "viewer")
            self.assertIn("413", methods["get"]["responses"])
            names = [param["name"] for param in methods["get"].get("parameters", [])]
            if path.endswith("/access"):
                self.assertNotIn("X-Budget-Grant", names)
            else:
                self.assertEqual(names[0], "X-Budget-Grant")
                self.assertTrue(methods["get"]["parameters"][0]["required"])
        self.assertEqual(set(receipts), {"/api/budget/receipts/" + name for name in
                         ("parse-text", "confirm", "list", "search", "detail", "image", "delete")})
        for path, methods in receipts.items():
            name = path.rsplit("/", 1)[1]
            method = "get" if name in ("detail", "image") else "post"
            self.assertEqual(set(methods), {method})
            self.assertEqual(methods[method]["x-required-role"], "admin")
            if method == "post":
                self.assertIn("requestBody", methods[method])
                self.assertIn("Content-Length", methods[method]["responses"]["411"]["description"])
                self.assertIn("application/json", methods[method]["responses"]["415"]["description"])
                self.assertIn("bytes", methods[method]["responses"]["413"]["description"])
            else:
                self.assertNotIn("411", methods[method]["responses"])
                self.assertNotIn("415", methods[method]["responses"])
                self.assertIn("budget_requires_sqlite_users", methods[method]["responses"]["409"]["description"])
        self.assertIn("budget_receipt_stale_source",
                      receipts["/api/budget/receipts/confirm"]["post"]["responses"]["409"]["description"])
        self.assertIn("budget_receipt_conflict",
                      receipts["/api/budget/receipts/delete"]["post"]["responses"]["409"]["description"])
        self.assertNotIn("budget_receipt_stale_source",
                         receipts["/api/budget/receipts/parse-text"]["post"]["responses"]["409"]["description"])
        parse_limit = receipts["/api/budget/receipts/parse-text"]["post"]["responses"]["413"]["description"]
        self.assertIn("49152", parse_limit)
        self.assertIn("budget_receipt_quota_exceeded", parse_limit)
        self.assertIn("131072", receipts["/api/budget/receipts/confirm"]["post"]["responses"]["413"]["description"])
        self.assertIn("16384", receipts["/api/budget/receipts/list"]["post"]["responses"]["413"]["description"])
        self.assertEqual(document["info"]["version"], "0.37.0")
        detail = receipts["/api/budget/receipts/detail"]["get"]
        self.assertNotIn("raw text", detail["summary"])
        confirm = receipts["/api/budget/receipts/confirm"]["post"]
        self.assertNotIn("30 days", confirm["summary"])
        self.assertNotIn("redacted text", confirm["summary"])
        self.assertIn("optional bank link", confirm["summary"])
        self.assertIn("128 KiB", confirm["summary"])
        detail_result = document["components"]["schemas"]["BudgetReceiptDetailResult"]
        self.assertNotIn("candidates", detail_result["required"])
        self.assertNotIn("default", detail_result["properties"]["candidates"])
        batch = analytics["/api/budget/analytics/rule-batch-preview"]["post"]["description"]
        self.assertIn("1..100", batch)
        self.assertIn("All-or-nothing", batch)
        save = analytics["/api/budget/analytics/rule-batch-save"]["post"]["description"]
        self.assertIn("MAX_RULES = 5000", save)
        mark_desc = analytics["/api/budget/analytics/inbox-mark"]["post"]["description"]
        for phrase in ("reads_version", "1..100", "500"):
            self.assertIn(phrase, mark_desc)
        refresh = budget["/api/budget/refresh"]["post"]
        self.assertEqual(refresh["parameters"][0]["name"], "Idempotency-Key")
        self.assertTrue(refresh["parameters"][0]["required"])
        self.assertIn("202", refresh["responses"])
        self.assertNotIn("200", refresh["responses"])

    def test_category_parent_is_additive_on_list_create_and_update(self):
        schemas = contract.build_openapi()["components"]["schemas"]
        category = schemas["BudgetAnalyticsCategory"]
        self.assertEqual(category["properties"]["parent_id"]["anyOf"][1], {"type": "null"})
        self.assertIn("parent_id", category["required"])
        saved = schemas["BudgetAnalyticsCategorySaved"]["required"]
        self.assertIn("parent_id", saved)
        for name in ("BudgetAnalyticsCategoryCreateRequest", "BudgetAnalyticsCategoryUpdateRequest"):
            schema = schemas[name]
            parent = schema["properties"]["parent_id"]
            self.assertNotIn("parent_id", schema["required"])
            self.assertIsNone(parent["default"])
            self.assertIn("omit this field", parent["description"])

    def test_forecast_query_and_error_contract(self):
        paths = contract.build_openapi()["paths"]
        forecast = paths["/api/budget/analytics/forecast"]["get"]
        parameters = {param["name"]: param for param in forecast["parameters"]}
        self.assertEqual(set(parameters), {"months", "currency"})
        for param in parameters.values():
            self.assertEqual(param["in"], "query")
            self.assertFalse(param["required"])
        self.assertEqual(parameters["months"]["schema"],
                         {"type": "string", "pattern": "^[0-9]{1,2}$", "default": "3"})
        self.assertEqual(parameters["currency"]["schema"], {"type": "string", "pattern": "^[A-Z]{3}$"})
        responses = forecast["responses"]
        self.assertEqual(set(responses), {"200", "400", "401", "403", "404", "409", "413", "500", "503"})
        self.assertEqual(responses["200"]["content"]["application/json"]["schema"],
                         {"$ref": "#/components/schemas/BudgetAnalyticsForecast"})
        for code, description in {
            "400": "Invalid forecast query: months must be an integer 1..12, currency must match ^[A-Z]{3}$, "
                   "and unknown or duplicate parameters (including today) are rejected.",
            "409": "SQLite backend required: forecast requires HESTIA_PERSIST=sqlite.",
            "413": "Forecast limit exceeded (>100,000 selected rows, >100 schedules, >1,200 paid marks per schedule, "
                   ">200 active currency-filtered hints, >64 category baselines, >4,368 projected items, or >4 MiB UTF-8 "
                   "JSON payload); zero partial totals returned.",
            "500": "Stored mortgage schedule, ledger, or metadata integrity check failed (`budget_analytics_data_error`), "
                   "including stored-data helper errors mapped to 500 after forecast query validation, or unexpected "
                   "internal failure (`budget_internal_error`).",
            "503": "Analytics storage or budget configuration unavailable (`budget_analytics_unavailable` or "
                   "`budget_unavailable`). budget_forecast_config_conflict (0.31–0.32) wycofany w 0.33; "
                   "konflikt to teraz 200 + summary.baseline_conflicts.",
        }.items():
            with self.subTest(code=code):
                self.assertEqual(responses[code]["description"], description)
                self.assertEqual(responses[code]["content"]["application/json"]["schema"],
                                 {"$ref": "#/components/schemas/BudgetError"})
        self.assertNotEqual(paths["/api/budget/analytics/quarantine"]["get"]["responses"]["409"]["description"],
                            responses["409"]["description"])

    def test_forecast_schemas_pin_wire_fields_and_bounds(self):
        schemas = contract.build_openapi()["components"]["schemas"]
        optional_keys = {"Summary": {"baseline_conflicts"}}
        for suffix, keys in {
            "": {"evaluation_date", "evaluation_timezone", "horizon_months", "start_month", "end_month",
                 "ledger_as_of", "freshness_stale", "stale_account_ids", "stale_account_ids_truncated",
                 "snapshot_digest", "schedules_version", "rule_version", "summary", "totals", "deduplications", "months"},
            "Summary": {"selected_accounts", "excluded_accounts", "stale_accounts", "overdue_mortgage_schedule_count",
                        "lapsed_recurring_count", "deduped_recurring_count", "stale_recurring_count",
                        "baseline_conflicts"},
            "CurrencyTotal": {"currency", "total", "scheduled_total", "estimated_total"},
            "Deduplication": {"schedule_id", "schedule_label", "recurring_label", "amount", "currency",
                              "matched_due_date", "last_observed_date", "day_distance", "account_stale"},
            "Month": {"month", "currencies", "items"},
            "Item": {"source", "schedule_id", "label", "amount", "currency", "expected_date", "confidence",
                     "past_due", "account_stale"},
            "BaselineConflict": {"category_id", "reason"},
        }.items():
            with self.subTest(schema=suffix):
                schema = schemas["BudgetAnalyticsForecast" + suffix]
                self.assertEqual(set(schema["properties"]), keys)
                self.assertEqual(set(schema["required"]), keys - optional_keys.get(suffix, set()))
                self.assertNotIn("additionalProperties", schema)
        forecast = schemas["BudgetAnalyticsForecast"]["properties"]
        item = schemas["BudgetAnalyticsForecastItem"]["properties"]
        dedup = schemas["BudgetAnalyticsForecastDeduplication"]["properties"]
        totals = schemas["BudgetAnalyticsForecastCurrencyTotal"]["properties"]
        self.assertEqual(item["source"]["enum"], ["mortgage", "recurring", "category_baseline"])
        self.assertEqual(item["confidence"]["enum"], ["scheduled", "high", "medium", "estimated"])
        self.assertEqual(forecast["evaluation_timezone"]["const"], "UTC")
        self.assertEqual(forecast["stale_account_ids"]["maxItems"], 200)
        conflicts = schemas["BudgetAnalyticsForecastSummary"]["properties"]["baseline_conflicts"]
        self.assertEqual(conflicts["maxItems"], 64)
        self.assertEqual(conflicts["items"], {"$ref": "#/components/schemas/BudgetAnalyticsForecastBaselineConflict"})
        conflict = schemas["BudgetAnalyticsForecastBaselineConflict"]["properties"]
        self.assertEqual(set(conflict), {"category_id", "reason"})
        self.assertEqual(conflict["reason"]["enum"], ["recurring", "mortgage"])
        self.assertEqual(conflict["category_id"]["maxLength"], 128)
        self.assertNotIn("amount", conflict)
        for label in (item["label"], dedup["schedule_label"], dedup["recurring_label"]):
            self.assertEqual(label["maxLength"], 256)
        for money in (item["amount"], dedup["amount"], totals["total"], totals["scheduled_total"], totals["estimated_total"]):
            self.assertEqual(money["type"], "string")

    def test_monthly_budget_query_and_error_contract(self):
        paths = contract.build_openapi()["paths"]
        monthly = paths["/api/budget/analytics/monthly-budget"]["get"]
        parameters = {param["name"]: param for param in monthly["parameters"]}
        self.assertEqual(set(parameters), {"period", "currency"})
        for param in parameters.values():
            self.assertEqual(param["in"], "query")
            self.assertFalse(param["required"])
        self.assertEqual(parameters["period"]["schema"],
                         {"type": "string", "pattern": r"^[0-9]{4}-[0-9]{2}$"})
        self.assertEqual(parameters["currency"]["schema"],
                         {"type": "string", "pattern": "^[A-Z]{3}$", "default": "PLN"})
        responses = monthly["responses"]
        self.assertEqual(set(responses), {"200", "400", "401", "403", "404", "409", "413", "500", "503"})
        self.assertEqual(responses["200"]["content"]["application/json"]["schema"],
                         {"$ref": "#/components/schemas/BudgetAnalyticsMonthlyBudget"})
        for code, description in {
            "400": "Invalid monthly budget query: period must match ^[0-9]{4}-[0-9]{2}$ through the current "
                   "Europe/Warsaw month, currency must match ^[A-Z]{3}$, and unknown or duplicate parameters are rejected.",
            "409": "SQLite backend required: monthly-budget requires HESTIA_PERSIST=sqlite.",
            "413": "Monthly budget limit exceeded (>100,000 selected rows, >1,000 category limits, "
                   ">192-character aggregate decimal output, or >4 MiB UTF-8 JSON payload); no partial totals returned.",
            "500": "Stored ledger, category, limit, metadata, or timestamp integrity check failed "
                   "(`budget_analytics_data_error`), including stored-data helper errors mapped to 500 after query "
                   "validation, or unexpected internal failure (`budget_internal_error`).",
            "503": "Analytics storage, Europe/Warsaw time zone, or monthly budget configuration unavailable "
                   "(`budget_analytics_unavailable` or `budget_unavailable`).",
        }.items():
            with self.subTest(code=code):
                self.assertEqual(responses[code]["description"], description)
                self.assertEqual(responses[code]["content"]["application/json"]["schema"],
                                 {"$ref": "#/components/schemas/BudgetError"})

    def test_monthly_budget_schemas_pin_wire_fields_and_bounds(self):
        schemas = contract.build_openapi()["components"]["schemas"]
        optional_keys = {"Configuration": {"income_window"},
                         "Coverage": {"pending_row_keys", "pending_row_keys_truncated"},
                         "CategorySpend": {"one_off", "one_off_key"}}
        for suffix, keys in {
            "": {"period", "start", "end", "cutoff", "current_month", "evaluated_at", "evaluation_date",
                 "evaluation_timezone", "currency", "available_currencies", "configuration", "coverage",
                 "summary", "expected_income", "pace", "category_spend", "pass_through", "card_exposure"},
            "Configuration": {"status", "source", "income_window"},
            "IncomeWindow": {"start", "end", "start_day"},
            "Coverage": {"ledger_as_of", "stale_selected_accounts", "purchase_date_fallback_count",
                         "pending_transfer_rows", "pending_row_keys", "pending_row_keys_truncated",
                         "transfer_scan_limited", "provisional", "has_activity"},
            "Summary": {"start_budget", "spent", "remaining"},
            "ExpectedIncome": {"status", "total", "items"},
            "ExpectedIncomeItem": {"category_id", "amount", "expected_date", "source"},
            "Ratio": {"numerator", "denominator"},
            "Pace": {"basis", "budget", "status", "daily_rate", "projected_month_spend", "exhaustion_day"},
            "CategoryLimit": {"monthly_limit", "remaining", "over_by", "percent_used_floor",
                              "warning_percent", "status"},
            "CategorySpend": {"category_id", "name", "archived", "expenses", "refunds", "net_spending", "limit",
                              "one_off", "one_off_key"},
            "PassThrough": {"rent_category_id", "mortgage_category_id", "rent_received", "mortgage_debits",
                            "mortgage_refunds", "unapplied_mortgage_refunds", "matched", "unapplied_rent",
                            "uncovered_mortgage"},
            "CardExposure": {"status", "amount", "as_of_date"},
        }.items():
            with self.subTest(schema=suffix):
                schema = schemas["BudgetAnalyticsMonthlyBudget" + suffix]
                self.assertEqual(set(schema["properties"]), keys)
                self.assertEqual(set(schema["required"]), keys - optional_keys.get(suffix, set()))
                self.assertNotIn("additionalProperties", schema)
        window = schemas["BudgetAnalyticsMonthlyBudgetIncomeWindow"]["properties"]
        self.assertEqual(window["start_day"]["type"], "integer")
        self.assertEqual((window["start_day"]["minimum"], window["start_day"]["maximum"]), (1, 28))
        self.assertNotIn("income_window", schemas["BudgetAnalyticsMonthlyBudgetConfiguration"]["required"])
        pending_row_keys = schemas["BudgetAnalyticsMonthlyBudgetCoverage"]["properties"]["pending_row_keys"]
        self.assertEqual(pending_row_keys["type"], "array")
        self.assertEqual(pending_row_keys["maxItems"], 500)
        self.assertEqual(pending_row_keys["items"], {
            "type": "string",
            "minLength": 3,
            "maxLength": 1153,
            "pattern": contract._ROW_KEY_PATTERN,
        })
        self.assertNotIn("pending_row_keys", schemas["BudgetAnalyticsMonthlyBudgetCoverage"]["required"])
        self.assertNotIn("pending_row_keys_truncated", schemas["BudgetAnalyticsMonthlyBudgetCoverage"]["required"])
        self.assertEqual(
            schemas["BudgetAnalyticsMonthlyBudgetCoverage"]["properties"]["pending_row_keys_truncated"]["type"],
            "boolean",
        )
        tx_params = {
            param["name"]: param
            for param in contract.build_openapi()["paths"]["/api/budget/analytics/transactions"]["get"]["parameters"]
        }
        self.assertEqual(set(tx_params), {"period", "currency", "category_id", "kind", "query", "limit", "cursor"})
        tx_by_keys = contract.build_openapi()["paths"]["/api/budget/analytics/transactions-by-keys"]["post"]
        self.assertEqual(
            tx_by_keys["requestBody"]["content"]["application/json"]["schema"],
            {"$ref": "#/components/schemas/BudgetAnalyticsTransactionsByKeysRequest"},
        )
        self.assertEqual(
            tx_by_keys["responses"]["200"]["content"]["application/json"]["schema"],
            {"$ref": "#/components/schemas/BudgetAnalyticsTransactions"},
        )
        by_keys_req = schemas["BudgetAnalyticsTransactionsByKeysRequest"]
        self.assertIs(by_keys_req["additionalProperties"], False)
        self.assertEqual(by_keys_req["required"], ["row_keys"])
        self.assertEqual(by_keys_req["properties"]["row_keys"]["minItems"], 1)
        self.assertEqual(by_keys_req["properties"]["row_keys"]["maxItems"], 500)
        self.assertEqual(by_keys_req["properties"]["row_keys"]["items"], {
            "type": "string",
            "minLength": 3,
            "maxLength": 1153,
            "pattern": contract._ROW_KEY_PATTERN,
        })
        self.assertEqual(
            set(by_keys_req["properties"]),
            {"row_keys", "cursor", "limit", "currency", "category_id", "kind", "query"},
        )
        root = schemas["BudgetAnalyticsMonthlyBudget"]["properties"]
        self.assertEqual(root["evaluation_timezone"]["const"], "Europe/Warsaw")
        self.assertEqual(schemas["BudgetAnalyticsMonthlyBudgetCardExposure"]["properties"]["status"]["const"],
                         "unavailable")
        self.assertEqual(schemas["BudgetAnalyticsMonthlyBudgetSummary"]["properties"]["spent"]["maxLength"], 192)
        self.assertEqual(
            schemas["BudgetAnalyticsMonthlyBudgetCategoryLimit"]["properties"]["percent_used_floor"]["type"],
            "string",
        )

    def test_monthly_budget_charts_query_error_and_schema_contract(self):
        doc = contract.build_openapi()
        charts = doc["paths"]["/api/budget/analytics/monthly-budget/charts"]["get"]
        parameters = {param["name"]: param for param in charts["parameters"]}
        self.assertEqual(set(parameters), {"period", "currency", "history"})
        for param in parameters.values():
            self.assertEqual(param["in"], "query")
            self.assertFalse(param["required"])
        self.assertEqual(parameters["history"]["schema"], {"type": "string", "enum": ["6", "12"], "default": "6"})
        responses = charts["responses"]
        self.assertEqual(set(responses), {"200", "400", "401", "403", "404", "409", "413", "500", "503"})
        self.assertEqual(responses["200"]["content"]["application/json"]["schema"],
                         {"$ref": "#/components/schemas/BudgetAnalyticsMonthlyBudgetCharts"})
        for code in ("400", "409", "413", "500", "503"):
            self.assertEqual(responses[code]["content"]["application/json"]["schema"],
                             {"$ref": "#/components/schemas/BudgetError"})
            self.assertIn("monthly-budget/charts", responses["409"]["description"])

        schemas = doc["components"]["schemas"]
        optional = {"SeriesCategory": {"name", "one_off", "one_off_key"}}
        for suffix, keys in {
            "DailyPoint": {"date", "cumulative_spent"},
            "SeriesCategory": {"category_id", "net_spending", "name", "one_off", "one_off_key"},
            "SeriesMonth": {"period", "has_activity", "is_current_month", "income_estimate_partial",
                            "provisional", "start_budget", "spent", "categories"},
            "CategoryLabel": {"category_id", "name", "archived"},
            "Series": {"daily", "months", "category_labels"},
            "Charts": {"budget", "series"},
        }.items():
            with self.subTest(schema=suffix):
                schema = schemas["BudgetAnalyticsMonthlyBudget" + suffix]
                self.assertEqual(set(schema["properties"]), keys)
                self.assertEqual(set(schema["required"]), keys - optional.get(suffix, set()))
                self.assertNotIn("additionalProperties", schema)
        series_props = schemas["BudgetAnalyticsMonthlyBudgetSeries"]["properties"]
        self.assertEqual((series_props["daily"]["minItems"], series_props["daily"]["maxItems"]), (1, 31))
        self.assertEqual((series_props["months"]["minItems"], series_props["months"]["maxItems"]), (1, 12))
        self.assertEqual(schemas["BudgetAnalyticsMonthlyBudgetDailyPoint"]["properties"]["cumulative_spent"]["maxLength"], 192)
        self.assertEqual(schemas["BudgetAnalyticsMonthlyBudgetSeriesMonth"]["properties"]["spent"]["maxLength"], 192)

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
        request_names.add("BudgetConsentFlowRequest")
        request_names.update({"BudgetAnalyticsCategoryCreateRequest", "BudgetAnalyticsCategoryUpdateRequest",
                              "BudgetAnalyticsAnnotationRequest", "BudgetAnalyticsRulePreviewRequest",
                              "BudgetAnalyticsRuleSaveRequest", "BudgetAnalyticsRuleBatchPreviewRequest",
                              "BudgetAnalyticsRuleBatchSaveRequest", "BudgetAnalyticsTransferDecisionRequest",
                              "BudgetAnalyticsLegRequest", "BudgetAnalyticsSpendingLimitSaveRequest",
                              "BudgetAnalyticsMortgageSaveRequest", "BudgetAnalyticsMortgagePaymentSetRequest",
                              "BudgetAnalyticsInboxKeyRequest", "BudgetAnalyticsInboxMarkRequest",
                              "BudgetAnalyticsTransactionsByKeysRequest",
                              "BudgetReceiptParseTextRequest", "BudgetReceiptConfirmRequest", "BudgetReceiptLineInput",
                              "BudgetReceiptLinkRequest", "BudgetReceiptListRequest", "BudgetReceiptSearchRequest",
                              "BudgetReceiptDeleteRequest"})
        for name, schema in schemas.items():
            if name in request_names:
                self.assertIs(schema["additionalProperties"], False)
                if "expected_version" in schema["properties"]:
                    self.assertEqual(schema["properties"]["expected_version"]["minimum"], 0 if name.startswith("BudgetAnalytics") else 1)
            elif name.startswith("Budget"):
                self.assertNotIn("additionalProperties", schema)
        for name in ("BudgetTransaction", "BudgetMoney", "BudgetAnalyticsRecurringHint",
                     "BudgetAnalyticsMortgageSchedule", "BudgetAnalyticsMortgageReminder"):
            self.assertEqual(schemas[name]["properties"]["amount"]["type"], "string")
        for name in ("BudgetAnalyticsMortgageSaveRequest", "BudgetAnalyticsMortgageSchedule",
                     "BudgetAnalyticsMortgageReminder", "BudgetConsentRenewalReminder"):
            self.assertNotIn("destination_target", schemas[name]["properties"])
        for sensitive in ("session_id", "authorization_url", "code", "capsule_ref", "aspsp_id", "psu_type"):
            self.assertNotIn(sensitive, schemas["BudgetConsentRenewalReminder"]["properties"])
            self.assertNotIn(sensitive, schemas["BudgetConsentReconcileConsent"]["properties"])
            self.assertNotIn(sensitive, schemas["BudgetConsentReconcileFlow"]["properties"])
        for inbox_params in ("BudgetAnalyticsInboxConsentParams", "BudgetAnalyticsInboxMortgageParams",
                             "BudgetAnalyticsInboxSpendingLimitParams"):
            props = schemas[inbox_params]["properties"]
            for forbidden in ("destination_target", "bank_name", "label", "amount", "session_id", "connection_id",
                              "schedule_id", "category_id", "account_id", "error", "name"):
                self.assertNotIn(forbidden, props)
        self.assertEqual(set(schemas["BudgetAnalyticsInboxConsentRef"]["properties"]), {"connection_id"})
        self.assertEqual(set(schemas["BudgetAnalyticsInboxMortgageRef"]["properties"]), {"schedule_id"})
        self.assertEqual(set(schemas["BudgetAnalyticsInboxSpendingLimitRef"]["properties"]), {"category_id", "currency"})
        q_item_props = schemas["BudgetAnalyticsQuarantineItem"]["properties"]
        self.assertEqual(
            set(q_item_props),
            {"bucket", "account_id", "selected", "entry_reference", "date", "status", "reason", "source", "observed_at"},
        )
        for forbidden in ("amount", "currency", "remittance_information", "description", "creditor_name",
                          "debtor_name", "credit_debit_indicator", "transaction_id", "identification_hash"):
            self.assertNotIn(forbidden, q_item_props)
        self.assertEqual(schemas["BudgetAnalyticsSpendingLimit"]["properties"]["monthly_limit"]["type"], "string")
        for field in ("monthly_limit", "expenses", "refunds", "net_spending", "remaining", "over_by"):
            self.assertEqual(schemas["BudgetAnalyticsSpendingLimitStatus"]["properties"][field]["type"], "string")
        self.assertNotIn("fingerprint", schemas["BudgetAnalyticsRecurringOccurrence"]["properties"])
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
