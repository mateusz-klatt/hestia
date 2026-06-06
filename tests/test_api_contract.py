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

from hestia import api_contract
from hestia.web import _validate_control_payload


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
