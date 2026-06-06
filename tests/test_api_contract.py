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
            self.assertEqual(schemas[name].get("additionalProperties"), False)  # forbids unknowns


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
