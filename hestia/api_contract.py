"""The hestia public API contract — the SINGLE SOURCE OF TRUTH for the request/response shapes that
external clients consume (today the vanilla-TS web UI; next the native iOS app "Vesta").

Pydantic v2 models here generate ``docs/api/openapi.json`` (OpenAPI 3.1), from which typed clients are
code-generated downstream (Swift for Vesta; TypeScript / optional Zod for the web UI). Regenerate the
checked-in artifact after changing any model::

    python -m hestia.api_contract        # rewrites docs/api/openapi.json

``tests/test_api_contract.py`` fails the build if the checked-in file drifts from the models, and pins
each *request* model to the authoritative runtime validator so the contract cannot misdescribe behaviour.

SCOPE: this is the public WIRE contract only. It does NOT replace the authoritative validators
(``web._validate_control_payload``, ``automations.Rule.from_dict``) — those remain the source of truth
for *behaviour*; the models here describe the *shape* and are kept honest by the binding tests.

This first slice covers the ``POST /api/control`` command (request + response). Read shapes
(discovery / device / live events) and other endpoints land in follow-up slices.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic.json_schema import models_json_schema

# The contract's own version, independent of the app release — bumped when the wire shape changes.
CONTRACT_VERSION = "0.1.0"
OPENAPI_PATH = Path(__file__).resolve().parent.parent / "docs" / "api" / "openapi.json"

# Strict, exactly like the aiohttp handlers: reject unknown fields, bool-as-int, and int-as-float — so a
# payload the DTO accepts is one ``_validate_control_payload`` accepts (the binding test pins this).
_STRICT = ConfigDict(extra="forbid", strict=True)

# A device node id (every control op carries one). Matches ``web._control_node_error``.
NodeId = Annotated[int, Field(ge=0, le=255, description="device node id")]


class ControlSwitch(BaseModel):
    """Turn a relay/light on or off (optionally a single gang of a multi-gang node)."""

    model_config = _STRICT
    op: Literal["switch"]
    node: NodeId
    on: bool
    # Optional: ABSENT for a single-gang node. When present it must be the integer 1 or 2 — a strict int
    # range (NOT Literal[1,2], which would accept 1.0 since 1.0 == 1) so it rejects floats like the
    # handler; the default makes "absent" valid without making an explicit null valid. The default=None
    # is an internal sentinel for "omitted" — strip it from the SCHEMA (``json_schema_extra``) so a
    # generated client never materializes `endpoint: null`, which the server rejects.
    endpoint: Annotated[
        int,
        Field(
            default=None,
            ge=1,
            le=2,
            description="multi-gang channel 1 or 2; omit for single-gang",
            json_schema_extra=lambda s: s.pop("default", None),
        ),
    ] = None


class ControlThermostatPower(BaseModel):
    """Power a thermostat/TRV on or off."""

    model_config = _STRICT
    op: Literal["thermostat_power"]
    node: NodeId
    on: bool


class ControlLevel(BaseModel):
    """Set a dimmer level (0..99)."""

    model_config = _STRICT
    op: Literal["level"]
    node: NodeId
    value: Annotated[int, Field(ge=0, le=99, description="dim level 0..99")]


class ControlCover(BaseModel):
    """Set a blind/cover position (0..99)."""

    model_config = _STRICT
    op: Literal["cover"]
    node: NodeId
    value: Annotated[int, Field(ge=0, le=99, description="cover position 0..99")]


class ControlThermostat(BaseModel):
    """Set a thermostat/TRV target temperature (4..28 °C)."""

    model_config = _STRICT
    op: Literal["thermostat"]
    node: NodeId
    # int OR float in 4..28 (the handler accepts both). Bounds live on EACH arm so they emit standard
    # JSON-Schema minimum/maximum per anyOf branch (an outer Field on the union emits non-standard ge/le).
    celsius: Union[
        Annotated[int, Field(ge=4, le=28)],
        Annotated[float, Field(ge=4, le=28)],
    ] = Field(description="target °C, 4..28")


# A device control command — a oneOf discriminated on ``op`` (mirrors ``web._CONTROL_OPS``).
ControlRequest = Annotated[
    Union[ControlSwitch, ControlThermostatPower, ControlLevel, ControlCover, ControlThermostat],
    Field(discriminator="op"),
]

_CONTROL_MODELS = (ControlSwitch, ControlThermostatPower, ControlLevel, ControlCover, ControlThermostat)
CONTROL_ADAPTER = TypeAdapter(ControlRequest)


class ControlSuccess(BaseModel):
    """The 200 body when a control command reaches the device: ``ok`` true + the ``sent`` wire frame."""

    model_config = ConfigDict(extra="forbid")
    ok: bool
    sent: str = Field(description="the device command frame that was sent, hex-encoded")


class ControlError(BaseModel):
    """The 400 (malformed) / 503 (device unavailable) body: ``ok`` false + a human error string."""

    model_config = ConfigDict(extra="forbid")
    ok: bool
    error: str


class IrRequest(BaseModel):
    """POST /api/ir — transmit a saved Flipper IR signal (same 200/400/503 envelopes as /api/control)."""

    model_config = ConfigDict(extra="forbid", strict=True)
    file: Annotated[str, Field(min_length=1)]
    button: Annotated[str, Field(min_length=1)]


class SceneRequest(BaseModel):
    """POST /api/scene — fan one house-wide scene out across the per-device control path."""

    model_config = ConfigDict(extra="forbid", strict=True)
    op: Literal["lights_off", "lights_on", "blinds_down", "blinds_up"]


class SceneResult(BaseModel):
    """200 from /api/scene: how many of the scene's per-device commands the gateway accepted."""

    model_config = ConfigDict(extra="forbid")
    ok: bool
    sent: int
    total: int


class NameRequest(BaseModel):
    """POST /api/name — set a device's registry labels. ``node`` is required; at least one of
    name/room/type must be present (server-enforced cross-field, not expressible here). name/room
    accept null (clear); type is a DeviceType; ep is a multi-gang channel label. Unknown keys → 400."""

    model_config = ConfigDict(extra="forbid", strict=True)
    node: int
    op: Annotated[Literal["name"], Field(default=None, json_schema_extra=_OMIT)] = None
    name: Annotated[Union[str, None], Field(default=None, max_length=256, json_schema_extra=_OMIT)] = None
    room: Annotated[Union[str, None], Field(default=None, max_length=256, json_schema_extra=_OMIT)] = None
    type: Annotated[
        Union[Literal["light", "blind", "thermostat", "door", "motion", "smoke", "water", "plug", "unknown"], None],
        Field(default=None, json_schema_extra=_OMIT),
    ] = None
    ep: Annotated[Union[int, None], Field(default=None, ge=0, json_schema_extra=_OMIT)] = None


class Settings(BaseModel):
    """GET /api/settings — the logged-in user's UI prefs. All three keys are ALWAYS present, null when
    unset (an unauthenticated / no-row request returns all-null, never an error)."""

    model_config = ConfigDict(extra="forbid")
    locale: Union[str, None]
    temp_scale: Union[str, None]
    theme: Union[str, None]


class SettingsUpdate(BaseModel):
    """POST /api/settings — a PARTIAL update: only the keys present are persisted (absent keys are left
    untouched). null clears a field. temp_scale ∈ {C,F,K}. locale ≤ 35 chars."""

    model_config = ConfigDict(extra="forbid", strict=True)
    locale: Annotated[Union[str, None], Field(default=None, max_length=35, json_schema_extra=_OMIT)] = None
    temp_scale: Annotated[
        Union[Literal["C", "F", "K"], None], Field(default=None, json_schema_extra=_OMIT)
    ] = None
    theme: Annotated[Union[str, None], Field(default=None, json_schema_extra=_OMIT)] = None


class RoomIconRequest(BaseModel):
    """POST /api/rooms/icons — set one shared room emoji. Send icon="" (empty string, NOT null) to clear."""

    model_config = ConfigDict(extra="forbid", strict=True)
    room: Annotated[str, Field(max_length=64)]
    icon: Annotated[str, Field(max_length=16)]


_COMMAND_MODELS = (IrRequest, SceneRequest, SceneResult,
                   NameRequest, Settings, SettingsUpdate, RoomIconRequest)


# ---- read shapes (responses the web UI + Vesta consume) --------------------
# extra="forbid" here is DELIBERATE drift detection: if a backend dict grows a field the DTO doesn't
# list, the contract test (which validates real handler output) fails — forcing the model to keep up.
# Non-strict (no `strict=True`): these DESCRIBE output, so an int landing in a float field is fine.
_READ = ConfigDict(extra="forbid")

# A multi-gang switch's per-endpoint on/off, keyed by endpoint id (string on the wire). null when N/A.
DeviceEndpoints = dict[str, bool]

# An OPTIONAL registry label: ABSENT until the operator sets it, then a value — NEVER an explicit null
# (so the generated type is `name?: T`, not `name?: T | null`). The default=None is the "omitted"
# sentinel; strip it from the schema so a client never materializes the field as null.
_OMIT = lambda s: s.pop("default", None)  # noqa: E731 — tiny schema post-processor


class Globals(BaseModel):
    """Node-less global fields (``proxy.globals_snapshot``). Every key is ALWAYS present (required),
    null when its poller is off."""

    model_config = _READ
    crib_temp: Union[float, None]
    outdoor_temp: Union[float, None]
    outdoor_humidity: Union[float, None]


class Summary(BaseModel):
    """Discovery roster counters (``web._summary``)."""

    model_config = _READ
    total: int
    confirmed: int
    unknown: int


class DeviceInfo(BaseModel):
    """One device, merged from the classifier + the user registry (``proxy._discovery_entry``). The base
    + live-state fields are ALWAYS present (required), null when unseen — so `0`/`false` are never lost.
    The registry labels (``name``/``room``/``endpoint_names``) are OPTIONAL — absent until set, never null.
    ``confidence`` is usually a string but a legacy/hand-edited registry node (a ``type`` without a
    ``confidence``) can surface null, so the contract admits it."""

    model_config = _READ
    power: Union[str, None]
    type: str
    confidence: Union[str, None]
    battery: Union[int, None]
    level: Union[int, None]
    switch: Union[bool, None]
    door: Union[str, None]
    motion: Union[bool, None]
    setpoint: Union[float, None]
    thermostat_on: Union[bool, None]
    thermostat_last_cmd: Union[float, None]
    temperature: Union[float, None]
    power_w: Union[float, None]
    energy_kwh: Union[float, None]
    voltage_v: Union[float, None]
    endpoints: Union[DeviceEndpoints, None]
    last_seen: Union[str, None]
    name: Annotated[str, Field(default=None, json_schema_extra=_OMIT)] = None
    room: Annotated[str, Field(default=None, json_schema_extra=_OMIT)] = None
    endpoint_names: Annotated[dict[str, str], Field(default=None, json_schema_extra=_OMIT)] = None


class RuleVocab(BaseModel):
    """The automation-rule grammar the guided form builds from (``automations.rule_vocab``)."""

    model_config = _READ
    trigger_types: list[str]
    state_fields: dict[str, bool]
    cmp_ops: list[str]
    frame_action_ops: list[str]
    modes: list[str]
    sun_events: list[str]
    presence_events: list[str]
    condition_types: list[str]


class KlimaState(BaseModel):
    """The A/C state derived from IR traffic (``state``): power + optional mode/target."""

    model_config = _READ
    power: bool
    mode: Union[str, None]
    temp: Union[int, None]


class IrButton(BaseModel):
    """A configured one-tap IR button (``HESTIA_IR_BUTTONS``) → transmits a saved Flipper signal."""

    model_config = _READ
    label: str
    file: str
    button: str


class Klima(BaseModel):
    """The A/C control map parsed from the klima.ir signal names — an empty ``{}`` when no klima.ir is
    present, so every field is OPTIONAL (absent, never null). ``modes``/``power_on`` map each mode to its
    sorted temps; ``presets`` carries ``off`` + any non-temp signal."""

    model_config = _READ
    file: Annotated[str, Field(default=None, json_schema_extra=_OMIT)] = None
    modes: Annotated[dict[str, list[int]], Field(default=None, json_schema_extra=_OMIT)] = None
    power_on: Annotated[dict[str, list[int]], Field(default=None, json_schema_extra=_OMIT)] = None
    presets: Annotated[list[str], Field(default=None, json_schema_extra=_OMIT)] = None


class Discovery(BaseModel):
    """``GET /api/discovery`` — the whole dashboard snapshot. Every key is always present; ``klima_state``
    is null until an A/C IR command is seen, ``env_override`` is null unless ``HESTIA_MODE`` pins the mode."""

    model_config = _READ
    devices: dict[str, DeviceInfo]
    summary: Summary
    globals: Globals
    ir_buttons: list[IrButton]
    klima: Klima
    klima_state: Union[KlimaState, None]
    rule_vocab: RuleVocab
    mode: str
    target_mode: str
    env_override: Union[str, None]


_READ_MODELS = (Globals, Summary, DeviceInfo, RuleVocab, KlimaState, IrButton, Klima, Discovery)


# ---- auth (login / whoami / logout) ----------------------------------------
class LoginRequest(BaseModel):
    """POST /api/login body. ``bearer: true`` opts a native client into a token in the response (which
    it then sends as ``Authorization: Bearer``); the browser omits it and relies on the httponly cookie."""

    model_config = ConfigDict(extra="forbid")
    user: str
    password: str
    bearer: Annotated[bool, Field(default=None, json_schema_extra=_OMIT)] = None


class LoginSuccess(BaseModel):
    """200 from /api/login. ``token`` is present ONLY when the request set ``bearer: true``."""

    model_config = ConfigDict(extra="forbid")
    ok: bool
    user: str
    token: Annotated[str, Field(default=None, json_schema_extra=_OMIT)] = None


class WhoAmI(BaseModel):
    """GET /api/whoami — the caller's identity + RBAC role; both null when auth is off (loopback/dev)."""

    model_config = _READ
    user: Union[str, None]
    role: Union[str, None]


class OkResult(BaseModel):
    """A bare ``{ok: true}`` acknowledgement (e.g. /api/logout)."""

    model_config = ConfigDict(extra="forbid")
    ok: bool


_AUTH_MODELS = (LoginRequest, LoginSuccess, WhoAmI, OkResult)


def _ref(name: str) -> dict:
    return {"$ref": f"#/components/schemas/{name}"}


# The RBAC floor for each contracted operation, as `x-required-role` (mirrors web._ROUTE_MIN_ROLE;
# "public" = no auth). A test pins each value to web._required_role / web._is_public_route — so a
# mislabelled role fails CI rather than misleading a client.
_PATH_ROLES = {
    ("POST", "/api/control"): "operator",
    ("POST", "/api/ir"): "operator",
    ("POST", "/api/scene"): "operator",
    ("POST", "/api/name"): "admin",
    ("GET", "/api/settings"): "viewer",
    ("POST", "/api/settings"): "viewer",
    ("GET", "/api/rooms/icons"): "viewer",
    ("POST", "/api/rooms/icons"): "admin",
    ("POST", "/api/login"): "public",
    ("POST", "/api/logout"): "public",
    ("GET", "/api/whoami"): "viewer",
    ("GET", "/api/discovery"): "viewer",
}


def _component_schemas() -> dict:
    """The OpenAPI ``components.schemas`` map for every model, with cross-refs under that path."""
    _, combined = models_json_schema(
        [(model, "validation")
         for model in (*_CONTROL_MODELS, ControlSuccess, ControlError, *_COMMAND_MODELS,
                       *_READ_MODELS, *_AUTH_MODELS)],
        ref_template="#/components/schemas/{model}",
    )
    schemas = combined.get("$defs", {})
    # The request body is the discriminated union over the per-op models. The explicit `mapping` ties
    # each wire `op` value to its schema (tooling can't infer "switch" → "ControlSwitch" on its own).
    schemas["ControlRequest"] = {
        "oneOf": [_ref(m.__name__) for m in _CONTROL_MODELS],
        "discriminator": {
            "propertyName": "op",
            "mapping": {m.model_fields["op"].annotation.__args__[0]: f"#/components/schemas/{m.__name__}"
                        for m in _CONTROL_MODELS},
        },
        "description": "A device control command — one variant per device op.",
    }
    return schemas


def build_openapi() -> dict:
    """Assemble the OpenAPI 3.1 document for the current contract slice. Deterministic (no clock/host),
    so the checked-in ``docs/api/openapi.json`` stays byte-stable across regenerations."""
    err = {"content": {"application/json": {"schema": _ref("ControlError")}}}
    doc = {
        "openapi": "3.1.0",
        "info": {
            "title": "hestia API",
            "version": CONTRACT_VERSION,
            "description": "Local cloud-free smart-home control API. Generated from hestia/api_contract.py "
            "— do not edit by hand; run `python -m hestia.api_contract`.",
        },
        "paths": {
            "/api/control": {
                "post": {
                    "operationId": "control",
                    "summary": "Issue a device control command",
                    "description": "Requires the operator role.",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": _ref("ControlRequest")}},
                    },
                    "responses": {
                        "200": {
                            "description": "command sent to the device",
                            "content": {"application/json": {"schema": _ref("ControlSuccess")}},
                        },
                        "400": {"description": "malformed command", **err},
                        "503": {"description": "device unavailable / command rejected", **err},
                    },
                }
            },
            "/api/ir": {
                "post": {
                    "operationId": "ir",
                    "summary": "Transmit a saved IR signal via the Flipper",
                    "description": "Requires the operator role.",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": _ref("IrRequest")}},
                    },
                    "responses": {
                        # the IR success body is just {"ok": true} (no `sent` frame, unlike /api/control)
                        "200": {"description": "transmitted",
                                "content": {"application/json": {"schema": _ref("OkResult")}}},
                        "400": {"description": "malformed", **err},
                        "503": {"description": "IR disabled / queue full / failed", **err},
                    },
                }
            },
            "/api/scene": {
                "post": {
                    "operationId": "scene",
                    "summary": "Run a house-wide scene (all lights/blinds on/off)",
                    "description": "Requires the operator role. 200 reports how many device commands landed.",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": _ref("SceneRequest")}},
                    },
                    "responses": {
                        "200": {"description": "scene dispatched",
                                "content": {"application/json": {"schema": _ref("SceneResult")}}},
                        "400": {"description": "unknown scene", **err},
                    },
                }
            },
            "/api/name": {
                "post": {
                    "operationId": "setName",
                    "summary": "Set a device's registry labels (name / room / type / endpoint)",
                    "description": "Requires the admin role. Save failure maps to 500 (not 503).",
                    "requestBody": {"required": True,
                                    "content": {"application/json": {"schema": _ref("NameRequest")}}},
                    "responses": {
                        "200": {"description": "labels saved",
                                "content": {"application/json": {"schema": _ref("OkResult")}}},
                        "400": {"description": "malformed", **err},
                        "500": {"description": "registry save failed", **err},
                    },
                }
            },
            "/api/settings": {
                "get": {
                    "operationId": "getSettings",
                    "summary": "The logged-in user's UI preferences",
                    "description": "Requires the viewer role. All keys present, null when unset.",
                    "responses": {
                        "200": {"description": "the user's settings",
                                "content": {"application/json": {"schema": _ref("Settings")}}},
                    },
                },
                "post": {
                    "operationId": "setSettings",
                    "summary": "Update UI preferences (partial — only the keys sent are persisted)",
                    "description": "Requires the viewer role.",
                    "requestBody": {"required": True,
                                    "content": {"application/json": {"schema": _ref("SettingsUpdate")}}},
                    "responses": {
                        "200": {"description": "saved",
                                "content": {"application/json": {"schema": _ref("OkResult")}}},
                        "400": {"description": "malformed", **err},
                    },
                },
            },
            "/api/rooms/icons": {
                "get": {
                    "operationId": "getRoomIcons",
                    "summary": "Shared room→emoji map",
                    "description": "Requires the viewer role. A bare object (room name → emoji); {} when none set.",
                    "responses": {
                        "200": {
                            "description": "the room→icon map",
                            "content": {"application/json": {
                                "schema": {"type": "object", "additionalProperties": {"type": "string"}}}},
                        },
                    },
                },
                "post": {
                    "operationId": "setRoomIcon",
                    "summary": "Set one room's emoji (send icon=\"\" to clear)",
                    "description": "Requires the admin role.",
                    "requestBody": {"required": True,
                                    "content": {"application/json": {"schema": _ref("RoomIconRequest")}}},
                    "responses": {
                        "200": {"description": "saved",
                                "content": {"application/json": {"schema": _ref("OkResult")}}},
                        "400": {"description": "malformed", **err},
                    },
                },
            },
            "/api/login": {
                "post": {
                    "operationId": "login",
                    "summary": "Exchange credentials for a session (cookie + optional bearer token)",
                    "description": "Public. Sets the httponly session cookie; with `bearer: true` the 200 "
                    "body also carries a `token` for `Authorization: Bearer` (native clients).",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": _ref("LoginRequest")}},
                    },
                    "responses": {
                        "200": {
                            "description": "authenticated",
                            "content": {"application/json": {"schema": _ref("LoginSuccess")}},
                        },
                        "401": {"description": "invalid credentials", **err},
                    },
                }
            },
            "/api/logout": {
                "post": {
                    "operationId": "logout",
                    "summary": "Clear the session cookie",
                    "description": "Public. Clears the cookie; a bearer client simply discards its token.",
                    "responses": {
                        "200": {
                            "description": "logged out",
                            "content": {"application/json": {"schema": _ref("OkResult")}},
                        }
                    },
                }
            },
            "/api/whoami": {
                "get": {
                    "operationId": "whoami",
                    "summary": "The caller's identity + RBAC role",
                    "description": "Requires a session (cookie or bearer). user/role are null when auth is off.",
                    "responses": {
                        "200": {
                            "description": "the current session's identity",
                            "content": {"application/json": {"schema": _ref("WhoAmI")}},
                        }
                    },
                }
            },
            "/api/discovery": {
                "get": {
                    "operationId": "discovery",
                    "summary": "The full dashboard snapshot (devices + globals + klima + rule grammar)",
                    "description": "Requires the viewer role. The primary read a client polls/refetches.",
                    "responses": {
                        "200": {
                            "description": "the current snapshot",
                            "content": {"application/json": {"schema": _ref("Discovery")}},
                        }
                    },
                }
            },
        },
        "components": {"schemas": _component_schemas()},
    }
    # Tag every operation with its RBAC floor (machine-readable; a test cross-checks each against the
    # real web._required_role / public allowlist so the contract's roles can never drift from the server).
    for path, ops in doc["paths"].items():
        for method, op in ops.items():
            op["x-required-role"] = _PATH_ROLES[(method.upper(), path)]
    return doc


def write_openapi(path: Path = OPENAPI_PATH) -> None:
    """Write the OpenAPI document to ``path`` (sorted keys + trailing newline → a stable git diff)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(build_openapi(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":  # pragma: no cover
    write_openapi()
    print(f"wrote {OPENAPI_PATH}")
