"""Tests for hestia.api_contract — the public API contract.

Two guarantees:
  1. The checked-in ``docs/api/openapi.json`` never drifts from the models (regenerate with
     ``python -m hestia.api_contract``).
  2. Each request DTO accepts/rejects exactly what the authoritative runtime validator does, so the
     generated client types can never lie about what the server will accept.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from hestia import api_contract
from hestia.automations import rule_vocab
from hestia.proxy import _discovery_entry, globals_snapshot
from hestia.state import State
from hestia.web import _summary, _validate_control_payload


def _wire(model, payload):
    """Validate the JSON-roundtripped payload — the ACTUAL wire shape the client sees (so e.g. a
    multi-gang's int endpoint keys become strings, matching the DTO)."""
    return model.model_validate(json.loads(json.dumps(payload)))


def _min_device() -> dict:
    """A minimal valid DeviceInfo: every always-present field null, no optional labels."""
    nulls = ("power", "confidence", "battery", "level", "switch", "door", "motion", "setpoint",
             "thermostat_on", "thermostat_last_cmd", "temperature", "power_w", "energy_kwh",
             "voltage_v", "endpoints", "last_seen")
    return {**{k: None for k in nulls}, "type": "light"}


def _dto_accepts(payload) -> bool:
    try:
        api_contract.CONTROL_ADAPTER.validate_python(payload)
        return True
    except ValueError:
        return False


class OpenApiArtifactTests(unittest.TestCase):
    def test_checked_in_openapi_matches_models(self):
        on_disk = json.loads(api_contract.OPENAPI_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            on_disk,
            api_contract.build_openapi(),
            "docs/api/openapi.json is stale — run `python -m hestia.api_contract`",
        )

    def test_openapi_is_well_formed_3_1(self):
        doc = api_contract.build_openapi()
        self.assertEqual(doc["openapi"], "3.1.0")
        self.assertEqual(doc["info"]["version"], api_contract.CONTRACT_VERSION)
        schemas = doc["components"]["schemas"]
        for name in ("ControlSwitch", "ControlLevel", "ControlCover", "ControlThermostat",
                     "ControlThermostatPower", "ControlSuccess", "ControlError"):
            self.assertIn(name, schemas)
        # every oneOf branch resolves to a defined schema
        for ref in schemas["ControlRequest"]["oneOf"]:
            self.assertIn(ref["$ref"].rsplit("/", 1)[-1], schemas)

    def test_control_op_paths_and_responses_match_the_handler(self):
        post = api_contract.build_openapi()["paths"]["/api/control"]["post"]
        # the handler returns {ok, sent}/200 on success and {ok, error} on 400 (malformed) / 503 (device)
        self.assertEqual(post["responses"]["200"]["content"]["application/json"]["schema"],
                         {"$ref": "#/components/schemas/ControlSuccess"})
        for code in ("400", "503"):
            self.assertEqual(post["responses"][code]["content"]["application/json"]["schema"],
                             {"$ref": "#/components/schemas/ControlError"})
        self.assertEqual(set(post["responses"]), {"200", "400", "503"})

    def test_discriminator_mapping_covers_every_op(self):
        disc = api_contract.build_openapi()["components"]["schemas"]["ControlRequest"]["discriminator"]
        self.assertEqual(disc["propertyName"], "op")
        # each wire op value maps to a defined schema (tooling needs the explicit mapping)
        self.assertEqual(set(disc["mapping"]), {"switch", "level", "cover", "thermostat", "thermostat_power"})
        schemas = api_contract.build_openapi()["components"]["schemas"]
        for target in disc["mapping"].values():
            self.assertIn(target.rsplit("/", 1)[-1], schemas)

    def test_numeric_bounds_emit_standard_json_schema(self):
        schemas = api_contract.build_openapi()["components"]["schemas"]
        node = schemas["ControlSwitch"]["properties"]["node"]
        self.assertEqual((node["minimum"], node["maximum"]), (0, 255))
        # celsius is int|float; the bounds live on each anyOf arm as standard minimum/maximum (not ge/le)
        for arm in schemas["ControlThermostat"]["properties"]["celsius"]["anyOf"]:
            self.assertEqual((arm["minimum"], arm["maximum"]), (4, 28))

    def test_optional_endpoint_is_omittable_without_a_null_default(self):
        endpoint = api_contract.build_openapi()["components"]["schemas"]["ControlSwitch"]
        # optional (not in `required`) but NO leaked `default: null` (the server rejects explicit null)
        self.assertNotIn("endpoint", endpoint.get("required", []))
        self.assertNotIn("default", endpoint["properties"]["endpoint"])
        self.assertEqual(
            (endpoint["properties"]["endpoint"]["minimum"], endpoint["properties"]["endpoint"]["maximum"]),
            (1, 2),
        )

    def test_write_openapi_roundtrips(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sub" / "openapi.json"  # parent created on demand
            api_contract.write_openapi(path)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), api_contract.build_openapi())
            self.assertTrue(path.read_text(encoding="utf-8").endswith("\n"))


class ControlContractBindingTests(unittest.TestCase):
    """The DTO and ``_validate_control_payload`` must agree on EVERY case — valid and invalid."""

    VALID = [
        {"op": "switch", "node": 0, "on": True},
        {"op": "switch", "node": 255, "on": False},
        {"op": "switch", "node": 14, "on": True, "endpoint": 1},
        {"op": "switch", "node": 14, "on": True, "endpoint": 2},
        {"op": "thermostat_power", "node": 9, "on": False},
        {"op": "level", "node": 1, "value": 0},
        {"op": "level", "node": 1, "value": 99},
        {"op": "cover", "node": 4, "value": 50},
        {"op": "thermostat", "node": 9, "celsius": 4},
        {"op": "thermostat", "node": 9, "celsius": 28},
        {"op": "thermostat", "node": 9, "celsius": 21.5},
    ]
    INVALID = [
        {"op": "switch", "node": 14, "on": "yes"},          # on not a bool
        {"op": "switch", "node": True, "on": True},          # bool node (True == 1, but rejected)
        {"op": "switch", "node": -1, "on": True},            # node below range
        {"op": "switch", "node": 256, "on": True},           # node above range
        {"op": "switch", "node": 14, "on": True, "endpoint": 3},   # endpoint not 1/2
        {"op": "switch", "node": 14, "on": True, "endpoint": None},  # present-but-null endpoint
        {"op": "switch", "node": 14, "on": True, "endpoint": 1.0},  # float, not int
        {"op": "switch", "node": 14, "on": True, "extra": 1},      # unknown field
        {"op": "level", "node": 1, "value": 100},            # value above range
        {"op": "level", "node": 1},                          # missing value
        {"op": "thermostat", "node": 9, "celsius": 3},       # below 4
        {"op": "thermostat", "node": 9, "celsius": 29},      # above 28
        {"op": "thermostat", "node": 9, "celsius": True},    # bool, not a number
        {"op": "raw", "node": 1},                            # not a control op
        {"op": "lights", "node": 1, "on": True},             # not a control op
        "not-an-object",                                     # not even a dict
    ]

    def test_valid_payloads_accepted_by_both(self):
        for payload in self.VALID:
            self.assertIsNone(_validate_control_payload(payload), f"validator rejected {payload}")
            self.assertTrue(_dto_accepts(payload), f"DTO rejected {payload}")

    def test_invalid_payloads_rejected_by_both(self):
        for payload in self.INVALID:
            self.assertIsNotNone(_validate_control_payload(payload), f"validator accepted {payload}")
            self.assertFalse(_dto_accepts(payload), f"DTO accepted {payload}")

    def test_response_envelope_shapes(self):
        self.assertEqual(api_contract.ControlSuccess(ok=True, sent="ab12").model_dump(),
                         {"ok": True, "sent": "ab12"})
        self.assertEqual(api_contract.ControlError(ok=False, error="no device connected").model_dump(),
                         {"ok": False, "error": "no device connected"})


if __name__ == "__main__":
    unittest.main()


class ReadShapeContractTests(unittest.TestCase):
    """Each read DTO must accept the REAL handler output (drift gate — extra=forbid fails on a new field)."""

    def test_globals_matches_real_output(self):
        _wire(api_contract.Globals, globals_snapshot(State()))  # all-null (pollers off)
        st = State()
        st.crib_temp, st.outdoor_temp, st.outdoor_humidity = 21.0, 14.5, 56.0
        g = _wire(api_contract.Globals, globals_snapshot(st))
        self.assertEqual((g.crib_temp, g.outdoor_temp, g.outdoor_humidity), (21.0, 14.5, 56.0))

    def test_summary_matches_real_output(self):
        s = _summary({"1": {"confidence": "confirmed", "type": "light"},
                      "2": {"confidence": "high", "type": "unknown"}})
        m = _wire(api_contract.Summary, s)
        self.assertEqual((m.total, m.confirmed, m.unknown), (2, 1, 1))

    def test_rule_vocab_matches_real_output(self):
        rv = _wire(api_contract.RuleVocab, rule_vocab())
        self.assertIn("time_window", rv.condition_types)

    def test_device_info_matches_real_discovery_entry(self):
        # minimal: an unseen node with no registry labels — every live field null, no name/room
        _wire(api_contract.DeviceInfo, _discovery_entry(SimpleNamespace(state=State()), 5, {}, {}))
        # fully populated: live state + a multi-gang's int endpoint keys (→ strings on the wire) + labels
        st = State()
        st.levels[5], st.switches[5], st.temperature[5], st.plug_w[5] = 50, True, 21.5, 12.0
        st.gang[5] = {1: True, 2: False}
        cls = {"power": "mains", "type": "light", "confidence": "high", "battery": None}
        reg = {"last_seen": "2026-06-06T00:00:00Z", "name": "Lamp", "room": "Hall",
               "endpoint_names": {"1": "L", "2": "R"}}
        d = _wire(api_contract.DeviceInfo, _discovery_entry(SimpleNamespace(state=st), 5, cls, reg))
        self.assertEqual(d.endpoints, {"1": True, "2": False})  # int gang keys serialized to strings
        self.assertEqual((d.name, d.room, d.level, d.temperature), ("Lamp", "Hall", 50, 21.5))

    def test_device_info_admits_null_confidence_from_a_legacy_registry_node(self):
        # a hand-edited/legacy registry node with a `type` but no `confidence` surfaces confidence=null
        entry = _discovery_entry(SimpleNamespace(state=State()), 7, {}, {"type": "light"})
        self.assertIsNone(entry["confidence"])
        self.assertIsNone(_wire(api_contract.DeviceInfo, entry).confidence)

    def test_always_present_fields_are_required_not_optional(self):
        # the backend ALWAYS emits these keys (null when unseen) — a MISSING key is drift and must fail
        with self.assertRaises(ValueError):
            api_contract.Globals.model_validate({"crib_temp": None, "outdoor_temp": None})  # missing humidity
        with self.assertRaises(ValueError):
            api_contract.KlimaState.model_validate({"power": True, "mode": "cool"})          # missing temp

    def test_optional_labels_are_omittable_non_null_no_default(self):
        di = api_contract.build_openapi()["components"]["schemas"]["DeviceInfo"]
        for label in ("name", "room", "endpoint_names"):
            self.assertNotIn(label, di.get("required", []))              # optional (absent ok)
            self.assertNotIn("default", di["properties"][label])         # no leaked default:null
        # present-but-null is rejected (the type is non-nullable when present → generated `name?: string`)
        with self.assertRaises(ValueError):
            api_contract.DeviceInfo.model_validate(_min_device() | {"name": None})

    def test_klima_state_shape(self):
        _wire(api_contract.KlimaState, {"power": True, "mode": "cool", "temp": 22})
        _wire(api_contract.KlimaState, {"power": False, "mode": None, "temp": None})

    def test_klima_and_ir_buttons_match_real_constants(self):
        from hestia.proxy import IR_BUTTONS, KLIMA
        _wire(api_contract.Klima, KLIMA)             # the deployed klima.ir map (or {})
        _wire(api_contract.Klima, {})                # empty when no klima.ir — every field optional
        _wire(api_contract.Klima, {"file": "/x.ir", "modes": {"cool": [18, 22]},
                                   "power_on": {"cool": [22]}, "presets": ["off"]})
        for button in IR_BUTTONS:
            _wire(api_contract.IrButton, button)
        _wire(api_contract.IrButton, {"label": "TV", "file": "/tv.ir", "button": "power"})

    def test_discovery_envelope_matches_assembled_real_pieces(self):
        from hestia.proxy import IR_BUTTONS, KLIMA
        st = State()
        st.switches[5] = True
        cls = {"type": "light", "confidence": "high", "power": "mains", "battery": None}
        devices = {"5": _discovery_entry(SimpleNamespace(state=st), 5, cls, {})}
        payload = {
            "devices": devices, "summary": _summary(devices), "globals": globals_snapshot(State()),
            "ir_buttons": IR_BUTTONS, "klima": KLIMA, "klima_state": None, "rule_vocab": rule_vocab(),
            "mode": "standalone", "target_mode": "standalone", "env_override": None,
        }
        d = _wire(api_contract.Discovery, payload)
        self.assertEqual(list(d.devices), ["5"])
        # a missing envelope key is drift → must fail (every key is required)
        with self.assertRaises(ValueError):
            api_contract.Discovery.model_validate({k: v for k, v in payload.items() if k != "mode"})

    def test_read_models_forbid_unknown_fields(self):
        # extra=forbid is the drift sentinel: a new backend field must fail until the DTO adds it
        with self.assertRaises(ValueError):
            api_contract.Globals.model_validate({"crib_temp": None, "surprise": 1})

    def test_read_schemas_present_in_openapi(self):
        schemas = api_contract.build_openapi()["components"]["schemas"]
        for name in ("DeviceInfo", "Globals", "Summary", "RuleVocab", "KlimaState"):
            self.assertIn(name, schemas)
            # Tolerant reader (#89): no additionalProperties:false on anything a client DECODES —
            # the PYTHON models still forbid unknowns, which is what the binding tests lean on.
            self.assertNotIn("additionalProperties", schemas[name])


class AuthContractTests(unittest.TestCase):
    def test_auth_request_response_shapes_validate(self):
        api_contract.LoginRequest.model_validate({"user": "x", "password": "y"})
        api_contract.LoginRequest.model_validate({"user": "x", "password": "y", "bearer": True})
        api_contract.LoginSuccess.model_validate({"ok": True, "user": "x"})              # token optional
        api_contract.LoginSuccess.model_validate({"ok": True, "user": "x", "token": "t"})
        api_contract.WhoAmI.model_validate({"user": None, "role": None})                 # auth off → nulls
        api_contract.WhoAmI.model_validate({"user": "x", "role": "admin"})
        api_contract.OkResult.model_validate({"ok": True})

    def test_login_token_is_optional_no_default(self):
        ls = api_contract.build_openapi()["components"]["schemas"]["LoginSuccess"]
        self.assertNotIn("token", ls.get("required", []))   # present only when bearer requested
        self.assertNotIn("default", ls["properties"]["token"])

    def test_whoami_fields_required_even_when_null(self):
        with self.assertRaises(ValueError):
            api_contract.WhoAmI.model_validate({"user": "x"})   # role is required (nullable), not optional

    def test_auth_paths_present(self):
        paths = api_contract.build_openapi()["paths"]
        for p in ("/api/login", "/api/logout", "/api/whoami"):
            self.assertIn(p, paths)


class CommandContractTests(unittest.TestCase):
    def test_ir_request(self):
        api_contract.IrRequest.model_validate({"file": "/x.ir", "button": "power"})
        for bad in ({"file": "", "button": "p"}, {"file": "/x.ir"}, {"file": "/x.ir", "button": "p", "x": 1}):
            with self.assertRaises(ValueError):
                api_contract.IrRequest.model_validate(bad)

    def test_scene_request_literals_match_the_handler(self):
        from hestia.web import _SCENE_TARGETS
        from typing import get_args
        lit = api_contract.SceneRequest.model_fields["op"].annotation
        self.assertEqual(set(get_args(lit)), set(_SCENE_TARGETS))   # contract ≡ handler allowlist
        for op in _SCENE_TARGETS:
            api_contract.SceneRequest.model_validate({"op": op})
        with self.assertRaises(ValueError):
            api_contract.SceneRequest.model_validate({"op": "nope"})

    def test_scene_result(self):
        api_contract.SceneResult.model_validate({"ok": True, "sent": 3, "total": 4})

    def test_command_paths_present(self):
        doc = api_contract.build_openapi()
        paths = doc["paths"]
        self.assertIn("/api/ir", paths)
        self.assertIn("/api/scene", paths)
        # IR success is {ok:true} (OkResult), NOT control's {ok,sent} (ControlSuccess)
        self.assertEqual(paths["/api/ir"]["post"]["responses"]["200"]["content"]["application/json"]["schema"],
                         {"$ref": "#/components/schemas/OkResult"})


class RegistrySettingsContractTests(unittest.TestCase):
    def test_settings_matches_real_empty_default(self):
        from hestia.web import _EMPTY_SETTINGS
        api_contract.Settings.model_validate(_EMPTY_SETTINGS)                 # the real all-null default
        api_contract.Settings.model_validate({"locale": "pl", "temp_scale": "C", "theme": "warm"})
        with self.assertRaises(ValueError):                                   # all 3 keys required (nullable)
            api_contract.Settings.model_validate({"locale": None, "temp_scale": None})

    def test_settings_update_is_partial_with_scale_enum(self):
        api_contract.SettingsUpdate.model_validate({})                        # empty partial is valid
        api_contract.SettingsUpdate.model_validate({"theme": "dark"})
        api_contract.SettingsUpdate.model_validate({"locale": None, "temp_scale": "F"})
        with self.assertRaises(ValueError):
            api_contract.SettingsUpdate.model_validate({"temp_scale": "X"})   # not C/F/K

    def test_name_request(self):
        api_contract.NameRequest.model_validate({"node": 14, "name": "Lamp", "room": "Hall"})
        api_contract.NameRequest.model_validate({"node": 1, "room": None})    # null clears
        api_contract.NameRequest.model_validate({"node": 1, "type": "thermostat"})
        for bad in ({"node": 1, "type": "nope"}, {"node": 1, "name": "x", "zzz": 1}, {"name": "x"}):
            with self.assertRaises(ValueError):
                api_contract.NameRequest.model_validate(bad)                  # bad type / unknown field / node missing

    def test_room_icon_request(self):
        api_contract.RoomIconRequest.model_validate({"room": "Salon", "icon": "\U0001f6cb"})
        api_contract.RoomIconRequest.model_validate({"room": "Salon", "icon": ""})   # "" clears
        with self.assertRaises(ValueError):
            api_contract.RoomIconRequest.model_validate({"room": "Salon"})           # icon required

    def test_command_paths_present(self):
        paths = api_contract.build_openapi()["paths"]
        for p in ("/api/name", "/api/settings", "/api/rooms/icons"):
            self.assertIn(p, paths)
        # name's save-failure is 500, not 503 (it omits fail_status)
        self.assertIn("500", paths["/api/name"]["post"]["responses"])
        # room-icons GET is a bare string→string map (no envelope)
        self.assertEqual(
            paths["/api/rooms/icons"]["get"]["responses"]["200"]["content"]["application/json"]["schema"],
            {"type": "object", "additionalProperties": {"type": "string"}})


class RoleAndBindingContractTests(unittest.TestCase):
    """x-required-role on every operation must match the real server floor; the request DTOs must agree
    with the authoritative validators on the field rules."""

    def test_x_required_role_matches_the_server(self):
        from hestia import web
        for path, ops in api_contract.build_openapi()["paths"].items():
            for method, op in ops.items():
                m = method.upper()
                # symmetric: derive the truth from the server, compare to the contract's claim — so a
                # public route mislabelled with a role (or vice-versa) fails, not just a wrong floor.
                expected = "public" if web._is_public_route(m, path) else web._required_role(m, path)
                self.assertEqual(op["x-required-role"], expected, f"{m} {path}")

    def test_settings_update_agrees_with_validator(self):
        from hestia.web import _settings_error
        for body in ({}, {"theme": "dark"}, {"temp_scale": "C"}, {"locale": None}, {"locale": "pl-PL"},
                     {"temp_scale": "X"}, {"locale": "x" * 36}, {"theme": 123}, {"temp_scale": 1}):
            backend_ok = _settings_error(body) is None
            try:
                api_contract.SettingsUpdate.model_validate(body)
                dto_ok = True
            except ValueError:
                dto_ok = False
            self.assertEqual(backend_ok, dto_ok, f"settings disagree on {body}")

    def test_room_icon_agrees_with_validator(self):
        from hestia.web import _room_icon_error
        for body in ({"room": "S", "icon": "x"}, {"room": "S", "icon": ""}, {"room": "S"},
                     {"room": "x" * 65, "icon": "a"}, {"room": "S", "icon": "x" * 17}, {"icon": "a"},
                     {"room": 1, "icon": "a"}):
            backend_ok = _room_icon_error(body) is None
            try:
                api_contract.RoomIconRequest.model_validate(body)
                dto_ok = True
            except ValueError:
                dto_ok = False
            self.assertEqual(backend_ok, dto_ok, f"room-icon disagree on {body}")

    def test_name_type_literals_match_device_types(self):
        from hestia.web import _TYPES
        for t in _TYPES:
            api_contract.NameRequest.model_validate({"node": 1, "type": t})
        with self.assertRaises(ValueError):
            api_contract.NameRequest.model_validate({"node": 1, "type": "bogus"})

    def test_whole_home_request_agrees_with_validator(self):
        from hestia.web import _whole_home_error
        for body in ({"node": 1, "exclude": True}, {"node": 1, "exclude": False},
                     {"node": 1, "exclude": 1}, {"node": 1}, {"exclude": True},
                     {"node": 300, "exclude": True}, {"node": 1, "exclude": True, "zzz": 1},
                     # per-gang: ep 1/2 (or explicit null) ok; 0/3/str/bool rejected
                     {"node": 1, "exclude": True, "ep": 1}, {"node": 1, "exclude": True, "ep": 2},
                     {"node": 1, "exclude": True, "ep": None}, {"node": 1, "exclude": True, "ep": 0},
                     {"node": 1, "exclude": True, "ep": 3}, {"node": 1, "exclude": True, "ep": "x"},
                     {"node": 1, "exclude": True, "ep": True}):
            backend_ok = _whole_home_error(body) is None
            try:
                api_contract.WholeHomeRequest.model_validate(body)
                dto_ok = True
            except ValueError:
                dto_ok = False
            self.assertEqual(backend_ok, dto_ok, f"whole-home disagree on {body}")

    def test_whole_home_config_shape(self):
        api_contract.WholeHomeConfig.model_validate({"excluded_nodes": [], "excluded_endpoints": {}})
        api_contract.WholeHomeConfig.model_validate(
            {"excluded_nodes": [2, 14], "excluded_endpoints": {"7": [2]}})
        for bad in ({"excluded_nodes": [2]},                                   # endpoints key required
                    {"excluded_nodes": [2], "excluded_endpoints": {}, "extra": 1}):
            with self.assertRaises(ValueError):
                api_contract.WholeHomeConfig.model_validate(bad)


class TolerantResponseContractTests(unittest.TestCase):
    """Response schemas must be TOLERANT READERS (the #122 Vesta lesson: a strict generated decoder
    turns an additive response field into a client crash). additionalProperties:false may appear in
    the emitted artifact ONLY on request-side schemas, never on anything a client decodes."""

    REQUEST_ONLY = {
        "AddUserRequest", "ChangePasswordRequest", "ControlCover", "ControlLevel", "ControlRequest",
        "ControlSwitch", "ControlThermostat", "ControlThermostatPower", "IrRequest", "LoginRequest",
        "NameRequest", "ResetPasswordRequest", "RoomIconRequest", "RuleInput", "SceneRequest",
        "SetDisabledRequest", "SetRoleRequest", "SettingsUpdate", "WholeHomeRequest",
    }

    def test_strictness_lives_only_on_request_schemas(self):
        schemas = api_contract.build_openapi()["components"]["schemas"]
        strict = {name for name, schema in schemas.items()
                  if '"additionalProperties": false' in json.dumps(schema)}
        self.assertEqual(strict - self.REQUEST_ONLY, set(),
                         "a response-reachable schema with additionalProperties:false would crash a "
                         "strict generated decoder on the next additive field")
        for canary in ("ControlSwitch", "NameRequest", "LoginRequest", "WholeHomeRequest"):
            self.assertIs(schemas[canary].get("additionalProperties"), False, canary)

    def test_no_strict_schema_is_response_reachable(self):
        # Closure-derived (not allowlist-derived): even after future endpoint/schema moves, nothing a
        # client can ever DECODE may carry additionalProperties:false. If a request DTO starts being
        # echoed in a response, this fails until that schema goes tolerant.
        doc = api_contract.build_openapi()
        schemas = doc["components"]["schemas"]
        response_roots = set()
        for ops in doc["paths"].values():
            for op in ops.values():
                response_roots |= api_contract._schema_refs(op.get("responses", {}))
        reachable = api_contract._ref_closure(schemas, response_roots)
        strict = {name for name, schema in schemas.items()
                  if '"additionalProperties": false' in json.dumps(schema)}
        self.assertEqual(strict & reachable, set())

    def test_decoded_shapes_are_tolerant(self):
        # The shapes a pinned native client decodes: the snapshot, live events, command results.
        schemas = api_contract.build_openapi()["components"]["schemas"]
        for name in ("DeviceInfo", "Discovery", "Globals", "Summary", "WhoAmI", "LoginSuccess",
                     "ControlSuccess", "ControlError", "SceneResult", "OkResult", "Settings",
                     "WholeHomeConfig", "StateEvent", "DeviceStatePatch", "GlobalsEvent",
                     "GlobalsPatch", "ActivityEvent", "KlimaEvent", "DiscoveryChangedEvent",
                     "AuditFeed", "AuditEvent", "Rule", "AutomationsList", "UsersList", "UserRow",
                     "Rf433Feed", "Rf433Device", "DbStats", "Klima", "KlimaState", "RuleVocab",
                     "IrButton"):
            self.assertNotIn('"additionalProperties": false', json.dumps(schemas[name]), name)


class AutomationsContractTests(unittest.TestCase):
    """The Rule DTO must accept the real Rule.to_dict() output for EVERY trigger/condition/action variant."""

    SWITCH = [{"op": "switch", "node": 14, "on": True}]
    TRIGGERS = {
        "scene": {"type": "scene", "node": 2, "scene_id": 3},
        "state-node": {"type": "state", "node": 7, "field": "temperature", "op": "lt", "value": 18},
        "state-global": {"type": "state", "field": "crib_temp", "op": "gt", "value": 24},  # no node
        "time-days": {"type": "time", "at": "07:30", "days": [0, 4]},
        "time-nodays": {"type": "time", "at": "07:30"},
        "sun": {"type": "sun", "event": "sunset", "offset_min": 15, "days": [5, 6]},
        "presence": {"type": "presence", "mac": "AA:BB:CC:DD:EE:FF", "event": "arrive"},
        "cron": {"type": "cron", "expr": "30 7 * * 1"},
    }

    def _real_rule(self, **spec):
        from hestia.automations import Rule as BackendRule
        return BackendRule.from_dict({"id": "r", "actions": self.SWITCH, **spec}).to_dict()

    def test_every_trigger_variant_round_trips(self):
        for name, tg in self.TRIGGERS.items():
            api_contract.Rule.model_validate(self._real_rule(trigger=tg))  # raises → fails with the variant name
            self.assertTrue(name)

    def test_condition_variants_round_trip(self):
        rule = self._real_rule(trigger=self.TRIGGERS["scene"], conditions=[
            {"node": 9, "field": "switch", "op": "eq", "value": True},          # state predicate (no type)
            {"type": "time_window", "start": "06:00", "end": "22:00", "days": [0, 1]},
            {"type": "time_window", "start": "22:00", "end": "06:00"},           # wrap, no days
        ])
        m = api_contract.Rule.model_validate(rule)
        self.assertEqual(len(m.conditions), 3)

    def test_all_action_ops_round_trip(self):
        acts = [{"op": "switch", "node": 14, "on": True, "endpoint": 2}, {"op": "ir", "file": "/x.ir", "button": "p"},
                {"op": "thermostat", "node": 9, "celsius": 21}, {"op": "cover", "node": 4, "value": 0},
                {"op": "level", "node": 5, "value": 50}, {"op": "thermostat_power", "node": 9, "on": False},
                {"op": "lights", "channels": [[1, 0], [2, 1]]}, {"op": "raw", "hex": "5aa5"}]
        from hestia.automations import Rule as BackendRule
        rule = BackendRule.from_dict({"id": "r", "trigger": self.TRIGGERS["scene"], "actions": acts}).to_dict()
        m = api_contract.Rule.model_validate(rule)
        self.assertEqual(m.actions[0].op, "switch")
        self.assertEqual(getattr(m.actions[0], "endpoint", None), 2)  # author field passed through (extra=allow)

    def test_automations_list_wraps_real_rules(self):
        rules = [self._real_rule(trigger=tg) for tg in self.TRIGGERS.values()]
        api_contract.AutomationsList.model_validate({"ok": True, "automations": rules})

    def test_rule_input_minimal_and_responses(self):
        api_contract.RuleInput.model_validate({"id": "r", "trigger": self.TRIGGERS["scene"], "actions": self.SWITCH})
        api_contract.RuleInput.model_validate(  # with the optional defaults
            {"id": "r", "trigger": self.TRIGGERS["cron"], "actions": self.SWITCH,
             "enabled": False, "modes": ["standalone"], "debounce": 2.0, "conditions": []})
        with self.assertRaises(ValueError):
            api_contract.RuleInput.model_validate({"id": "r", "trigger": self.TRIGGERS["scene"]})  # actions required
        api_contract.AutomationSaved.model_validate({"ok": True, "id": "r"})
        api_contract.AutomationDeleted.model_validate({"ok": True, "deleted": False})

    def test_automations_paths_present(self):
        paths = api_contract.build_openapi()["paths"]
        for p in ("/api/automations", "/api/automations/delete"):
            self.assertIn(p, paths)


class AuditEventsContractTests(unittest.TestCase):
    def test_audit_event_and_feed(self):
        # recent_audit emits exactly these 7 keys; the 5 strings are nullable at the DB layer
        api_contract.AuditEvent.model_validate(
            {"id": 1, "ts": 1749200000.1, "actor": "tata", "action": "login", "target": None,
             "detail": None, "result": "ok"})
        api_contract.AuditEvent.model_validate(
            {"id": 2, "ts": 1.0, "actor": None, "action": None, "target": None, "detail": None, "result": None})
        api_contract.AuditFeed.model_validate({"events": []})        # the audit-off / empty case
        with self.assertRaises(ValueError):
            api_contract.AuditEvent.model_validate({"id": 1, "ts": 1.0, "actor": "x"})  # missing keys (required)

    def test_live_event_variants(self):
        from pydantic import TypeAdapter
        le = TypeAdapter(api_contract.LiveEvent)
        for ev in (
            {"type": "discovery_changed"},
            {"type": "state", "node": 7, "fields": {"switch": True, "level": 50}},
            {"type": "state", "node": 7, "fields": {"temperature": None, "endpoints": {"1": True}}},  # nullable + int-key map
            {"type": "activity", "node": 2, "ts": 1749200000.1},
            {"type": "activity", "node": 2, "ts": 1749200000.1, "scene": {"id": 3, "kind": "scene"}},
            {"type": "activity", "node": 2, "ts": 1749200000.1, "scene": {"id": 5, "kind": "central"}},
            {"type": "globals", "fields": {"crib_temp": 21.5}},
            {"type": "globals", "fields": {"outdoor_temp": 14.0, "outdoor_humidity": 56}},  # 433 two-key
            {"type": "klima", "klima": {"power": True, "mode": "cool", "temp": 22}},
        ):
            le.validate_python(ev)
        with self.assertRaises(ValueError):
            le.validate_python({"type": "conn"})       # `conn` is a UI-derived indicator, NOT a server event

    def test_live_event_discriminator_covers_the_five_published_types(self):
        mapping = api_contract.build_openapi()["components"]["schemas"]["LiveEvent"]["discriminator"]["mapping"]
        self.assertEqual(set(mapping), {"discovery_changed", "state", "activity", "globals", "klima"})

    def test_audit_and_events_paths_present(self):
        paths = api_contract.build_openapi()["paths"]
        self.assertIn("/api/audit", paths)
        self.assertEqual(  # /api/events is an SSE stream, not JSON
            list(paths["/api/events"]["get"]["responses"]["200"]["content"]), ["text/event-stream"])


class UserManagementContractTests(unittest.TestCase):
    """User-mgmt + observability DTOs: requests pinned to the real validators, reads to real output."""

    def test_add_user_agrees_with_validators(self):
        from hestia.store_sql import ROLE_RANK
        from hestia.web import _password_error, _username_error
        cases = [
            {"username": "tata", "password": "longenough", "role": "admin"},      # valid
            {"username": "", "password": "longenough", "role": "admin"},           # empty username
            {"username": "a|b", "password": "longenough", "role": "viewer"},       # forbidden '|'
            {"username": "a/b", "password": "longenough", "role": "viewer"},       # forbidden '/'
            {"username": "x" * 65, "password": "longenough", "role": "operator"},  # username too long
            {"username": "ok", "password": "short", "role": "viewer"},             # password too short
            {"username": "ok", "password": "longenough", "role": "root"},          # role not in ROLE_RANK
            # edge cases where the `^[^|/]+$` pattern must track _username_error exactly: a username is
            # rejected ONLY for '|' / '/' / empty / >64 — newlines, tabs, unicode are all allowed (the
            # Rust-regex `$` does NOT special-case a trailing newline, so "abc\n" still validates).
            {"username": "abc\n", "password": "longenough", "role": "viewer"},     # trailing newline (allowed)
            {"username": "ab\ncd", "password": "longenough", "role": "viewer"},    # embedded newline (allowed)
            {"username": "café", "password": "longenough", "role": "viewer"},      # unicode (allowed)
            {"username": "ab\tcd", "password": "longenough", "role": "viewer"},    # tab (allowed)
            {"username": "\n|", "password": "longenough", "role": "viewer"},       # newline + '|' (rejected)
        ]
        for c in cases:
            backend_ok = (_username_error(c["username"]) is None
                          and _password_error(c["password"]) is None
                          and c["role"] in ROLE_RANK)
            try:
                api_contract.AddUserRequest.model_validate(c)
                dto_ok = True
            except ValueError:
                dto_ok = False
            self.assertEqual(backend_ok, dto_ok, f"add-user disagrees on {c}")

    def test_role_literal_matches_role_rank(self):
        from typing import get_args
        from hestia.store_sql import ROLE_RANK
        self.assertEqual(set(get_args(api_contract.Role)), set(ROLE_RANK))

    def test_password_fields_agree_with_password_error(self):
        from hestia.web import _password_error
        for pw in ("", "short", "x" * 7, "x" * 8, "x" * 1024, "x" * 1025, "exactly8"):
            backend_ok = _password_error(pw) is None
            for model, body in ((api_contract.ChangePasswordRequest, {"current": "whatever", "new": pw}),
                                (api_contract.ResetPasswordRequest, {"username": "u", "new": pw})):
                try:
                    model.model_validate(body)
                    dto_ok = True
                except ValueError:
                    dto_ok = False
                self.assertEqual(backend_ok, dto_ok, f"{model.__name__} `new` disagrees on len {len(pw)}")

    def test_change_password_requires_both_fields(self):
        api_contract.ChangePasswordRequest.model_validate({"current": "", "new": "longenough"})  # empty current ok
        for bad in ({"new": "longenough"}, {"current": "x"}, {"current": "x", "new": "longenough", "z": 1}):
            with self.assertRaises(ValueError):
                api_contract.ChangePasswordRequest.model_validate(bad)

    def test_set_disabled_requires_strict_bool(self):
        api_contract.SetDisabledRequest.model_validate({"username": "u", "disabled": True})
        for bad in ({"username": "u", "disabled": 1}, {"username": "u"}, {"username": "", "disabled": True}):
            with self.assertRaises(ValueError):
                api_contract.SetDisabledRequest.model_validate(bad)   # strict bool / missing / empty username

    def test_users_list_and_db_stats_match_real_output(self):
        import shutil

        from hestia import store_sql
        d = Path(tempfile.mkdtemp())
        try:
            dbp = d / "hestia.db"
            self.assertEqual(store_sql.add_user("mama", "scrypt$h", "admin", path=dbp), "ok")
            self.assertEqual(store_sql.add_user("kid", "scrypt$h2", "viewer", path=dbp), "ok")
            rows = store_sql.list_users(path=dbp)
            api_contract.UsersList.model_validate({"users": rows})       # the real list shape
            for r in rows:
                api_contract.UserRow.model_validate(r)
            m = api_contract.DbStats.model_validate(store_sql.db_stats(path=dbp))
            self.assertIn("users", m.tables)
            self.assertGreaterEqual(m.file_bytes, 0)
        finally:
            shutil.rmtree(d)

    def test_rf433_feed_matches_real_snapshot(self):
        from hestia.rf433 import Rf433Registry
        reg = Rf433Registry()
        reg.record({"model": "Prologue-TH", "id": 204, "channel": 1, "temperature_C": 14.5,
                    "humidity": 56, "battery_ok": 1, "time": "2026-06-06 00:00:00", "mic": "CRC"}, 1749200000.0)
        reg.record({"model": "Acurite-Rain", "id": 9, "rain_mm": 0.0, "button": True}, 1749200100.0)
        snap = reg.snapshot()
        self.assertEqual(len(snap), 2)
        _wire(api_contract.Rf433Feed, {"devices": snap})                # real snapshot validates
        for dev in snap:
            _wire(api_contract.Rf433Device, dev)
        api_contract.Rf433Feed.model_validate({"devices": []})          # empty until the feeder runs

    def test_required_read_fields_fail_when_missing(self):
        with self.assertRaises(ValueError):
            api_contract.UserRow.model_validate({"username": "u", "role": "admin"})        # missing disabled
        with self.assertRaises(ValueError):
            api_contract.DbStats.model_validate({"file_bytes": 0})                          # missing tables
        with self.assertRaises(ValueError):
            api_contract.Rf433Device.model_validate({"key": "k", "first_seen": 1.0})        # missing keys

    def test_user_paths_present_and_floored(self):
        paths = api_contract.build_openapi()["paths"]
        for p in ("/api/users", "/api/users/role", "/api/users/disabled",
                  "/api/users/reset-password", "/api/me/password", "/api/rf433", "/api/db/stats"):
            self.assertIn(p, paths)
        self.assertEqual(paths["/api/me/password"]["post"]["x-required-role"], "viewer")  # any signed-in user
        self.assertEqual(paths["/api/users"]["get"]["x-required-role"], "admin")
