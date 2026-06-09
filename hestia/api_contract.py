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
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic.json_schema import models_json_schema

# The contract's own version, independent of the app release — bumped when the wire shape changes.
# 0.2.0: + GET/POST /api/whole-home — a dedicated admin surface for the per-device opt-out of the
#        house-wide scene sweeps, kept OFF DeviceInfo so the device snapshot stays wire-stable for
#        pinned native clients (additive: new paths/schemas only; DeviceInfo/NameRequest unchanged).
CONTRACT_VERSION = "0.2.0"
OPENAPI_PATH = Path(__file__).resolve().parent.parent / "docs" / "api" / "openapi.json"

# De-duplicated string literals (SonarPython S1192): the OpenAPI content-type, the paths reused
# across the role map + the doc, and a couple of repeated descriptions/messages.
_APP_JSON = "application/json"
_P_SETTINGS = "/api/settings"
_P_ROOM_ICONS = "/api/rooms/icons"
_P_AUTOMATIONS = "/api/automations"
_P_USERS = "/api/users"
_P_WHOLE_HOME = "/api/whole-home"
_ADMIN_ROLE_DESC = "Requires the admin role."
_NO_SUCH_USER = "no such user"

# Strict, exactly like the aiohttp handlers: reject unknown fields, bool-as-int, and int-as-float — so a
# payload the DTO accepts is one ``_validate_control_payload`` accepts (the binding test pins this).
_STRICT = ConfigDict(extra="forbid", strict=True)

# Read/response config: forbid unknown fields (a new backend key fails the contract test = drift
# detection), but non-strict (these DESCRIBE output, so an int in a float field is fine).
_READ = ConfigDict(extra="forbid")

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
    celsius: Annotated[int, Field(ge=4, le=28)] | Annotated[float, Field(ge=4, le=28)] = Field(
        description="target °C, 4..28")


# A device control command — a oneOf discriminated on ``op`` (mirrors ``web._CONTROL_OPS``).
ControlRequest = Annotated[
    ControlSwitch | ControlThermostatPower | ControlLevel | ControlCover | ControlThermostat,
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
    name: Annotated[str | None, Field(default=None, max_length=256, json_schema_extra=_OMIT)] = None
    room: Annotated[str | None, Field(default=None, max_length=256, json_schema_extra=_OMIT)] = None
    type: Annotated[
        Literal["light", "blind", "thermostat", "door", "motion", "smoke", "water", "plug", "unknown"] | None,
        Field(default=None, json_schema_extra=_OMIT),
    ] = None
    ep: Annotated[int | None, Field(default=None, ge=0, json_schema_extra=_OMIT)] = None


class Settings(BaseModel):
    """GET /api/settings — the logged-in user's UI prefs. All three keys are ALWAYS present, null when
    unset (an unauthenticated / no-row request returns all-null, never an error)."""

    model_config = ConfigDict(extra="forbid")
    locale: str | None
    temp_scale: str | None
    theme: str | None


class SettingsUpdate(BaseModel):
    """POST /api/settings — a PARTIAL update: only the keys present are persisted (absent keys are left
    untouched). null clears a field. temp_scale ∈ {C,F,K}. locale ≤ 35 chars."""

    model_config = ConfigDict(extra="forbid", strict=True)
    locale: Annotated[str | None, Field(default=None, max_length=35, json_schema_extra=_OMIT)] = None
    temp_scale: Annotated[
        Literal["C", "F", "K"] | None, Field(default=None, json_schema_extra=_OMIT)
    ] = None
    theme: Annotated[str | None, Field(default=None, json_schema_extra=_OMIT)] = None


class RoomIconRequest(BaseModel):
    """POST /api/rooms/icons — set one shared room emoji. Send icon="" (empty string, NOT null) to clear."""

    model_config = ConfigDict(extra="forbid", strict=True)
    room: Annotated[str, Field(max_length=64)]
    icon: Annotated[str, Field(max_length=16)]


class WholeHomeConfig(BaseModel):
    """GET /api/whole-home — which devices are opted out of the house-wide "all lights / all blinds"
    sweeps. Registry-only config (deliberately NOT in DeviceInfo, so adding the opt-out never changes
    the device-snapshot wire shape a pinned native client decodes). ``excluded_nodes`` = the node ids
    a device is fully opted out by."""

    model_config = _READ
    excluded_nodes: list[int]


class WholeHomeRequest(BaseModel):
    """POST /api/whole-home — opt one device in (``exclude``=false) / out (true) of the house-wide
    "all" sweeps."""

    model_config = ConfigDict(extra="forbid", strict=True)
    node: NodeId
    exclude: bool


_COMMAND_MODELS = (IrRequest, SceneRequest, SceneResult, NameRequest, Settings, SettingsUpdate,
                   RoomIconRequest, WholeHomeConfig, WholeHomeRequest)


# ---- automation rules (the Rule.to_dict wire shape) ------------------------
# Output (Rule.to_dict) always emits all 7 keys; trigger/condition dicts carry only known keys (extra
# forbid), but ACTIONS pass author fields through verbatim (extra allow). Conditions discriminate by the
# ABSENCE of `type` (state predicate) vs type="time_window". Mirrors hestia/automations.py.
_RULE_FIELDS = frozenset({"door", "level", "switch", "setpoint", "thermostat_on", "temperature",
                          "power_w", "energy_kwh", "voltage_v", "crib_temp", "outdoor_temp"})
RuleField = Literal["door", "level", "switch", "setpoint", "thermostat_on", "temperature",
                    "power_w", "energy_kwh", "voltage_v", "crib_temp", "outdoor_temp"]
CmpOp = Literal["eq", "ne", "lt", "le", "gt", "ge"]
RuleValue = bool | int | float | str  # a predicate target — never null/list/dict
Weekday = Annotated[int, Field(ge=0, le=6)]


class TriggerScene(BaseModel):
    model_config = _READ
    type: Literal["scene"]
    node: int
    scene_id: int


class TriggerState(BaseModel):
    model_config = _READ
    type: Literal["state"]
    field: RuleField
    op: CmpOp
    value: RuleValue
    node: Annotated[int, Field(default=None, json_schema_extra=_OMIT)] = None  # omitted for GLOBAL fields


class TriggerTime(BaseModel):
    model_config = _READ
    type: Literal["time"]
    at: str
    days: list[Weekday] | None  # always present; null when unset


class TriggerSun(BaseModel):
    model_config = _READ
    type: Literal["sun"]
    event: Literal["sunrise", "sunset"]
    offset_min: int
    days: list[Weekday] | None


class TriggerPresence(BaseModel):
    model_config = _READ
    type: Literal["presence"]
    mac: str
    event: Literal["arrive", "leave"]


class TriggerCron(BaseModel):
    model_config = _READ
    type: Literal["cron"]
    expr: str


Trigger = Annotated[
    TriggerScene | TriggerState | TriggerTime | TriggerSun | TriggerPresence | TriggerCron,
    Field(discriminator="type"),
]


class StatePredicate(BaseModel):
    """A condition with NO `type` key (the absence IS the discriminant vs time_window)."""

    model_config = _READ
    field: RuleField
    op: CmpOp
    value: RuleValue
    node: Annotated[int, Field(default=None, json_schema_extra=_OMIT)] = None


class TimeWindowCondition(BaseModel):
    """A time-of-day guard condition. `days` is OMITTED entirely when unset (unlike time/sun triggers)."""

    model_config = _READ
    type: Literal["time_window"]
    start: str
    end: str
    days: Annotated[list[Weekday], Field(default=None, json_schema_extra=_OMIT)] = None


# A condition is a time_window (has `type`) OR a bare state predicate (no `type`) — disjoint by their
# required fields, so a smart union resolves them without a shared discriminator.
RuleCondition = TimeWindowCondition | StatePredicate


class RuleAction(BaseModel):
    """One action. Only `op` (+ ir's file/button) is validated at save; all other per-op fields are
    author-supplied and pass through verbatim — so this is open (extra=allow), not a closed per-op union."""

    model_config = ConfigDict(extra="allow")
    op: Literal["raw", "cover", "level", "switch", "lights", "thermostat", "thermostat_power", "ir"]


class Rule(BaseModel):
    """A saved automation rule (automations.Rule.to_dict) — all 7 keys ALWAYS present in the output."""

    model_config = _READ
    id: str
    enabled: bool
    modes: list[str]
    debounce: float
    trigger: Trigger
    conditions: list[RuleCondition]
    actions: list[RuleAction]


class RuleInput(BaseModel):
    """POST /api/automations body. id/trigger/actions required; enabled/modes/debounce/conditions are
    optional (server-defaulted by Rule.from_dict: enabled=true, modes=[proxy,standalone], debounce=0,
    conditions=[]). Authoritative validation is Rule.from_dict — this shape is the codegen guide."""

    model_config = ConfigDict(extra="forbid")
    id: str
    trigger: Trigger
    actions: list[RuleAction]
    enabled: Annotated[bool, Field(default=None, json_schema_extra=_OMIT)] = None
    modes: Annotated[list[str], Field(default=None, json_schema_extra=_OMIT)] = None
    debounce: Annotated[float, Field(default=None, json_schema_extra=_OMIT)] = None
    conditions: Annotated[list[RuleCondition], Field(default=None, json_schema_extra=_OMIT)] = None


class AutomationsList(BaseModel):
    """GET /api/automations — every saved rule. `automations` (NOT `rules`) is always present."""

    model_config = ConfigDict(extra="forbid")
    ok: bool
    automations: list[Rule]


class AutomationSaved(BaseModel):
    """200 from POST /api/automations: the saved rule's id (NOT the rule object)."""

    model_config = ConfigDict(extra="forbid")
    ok: bool
    id: str


class AutomationDeleted(BaseModel):
    """200 from POST /api/automations/delete: `deleted` is false (200, not 404) for an absent id."""

    model_config = ConfigDict(extra="forbid")
    ok: bool
    deleted: bool


_AUTOMATION_MODELS = (TriggerScene, TriggerState, TriggerTime, TriggerSun, TriggerPresence, TriggerCron,
                      StatePredicate, TimeWindowCondition, RuleAction, Rule, RuleInput,
                      AutomationsList, AutomationSaved, AutomationDeleted)


# ---- read shapes (responses the web UI + Vesta consume) --------------------
# (read/response config `_READ` is defined near the top, beside `_STRICT`.)

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
    crib_temp: float | None
    outdoor_temp: float | None
    outdoor_humidity: float | None


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
    ``confidence``) can surface null, so the contract admits it. NOTE: the whole-home scene opt-out
    (``exclude_from_all``) is deliberately NOT exposed here — it lives only on the dedicated
    ``/api/whole-home`` surface, so adding it never changes this (client-decoded) response shape."""

    model_config = _READ
    power: str | None
    type: str
    confidence: str | None
    battery: int | None
    level: int | None
    switch: bool | None
    door: str | None
    motion: bool | None
    setpoint: float | None
    thermostat_on: bool | None
    thermostat_last_cmd: float | None
    temperature: float | None
    power_w: float | None
    energy_kwh: float | None
    voltage_v: float | None
    endpoints: DeviceEndpoints | None
    last_seen: str | None
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
    mode: str | None
    temp: int | None


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
    klima_state: KlimaState | None
    rule_vocab: RuleVocab
    mode: str
    target_mode: str
    env_override: str | None


_READ_MODELS = (Globals, Summary, DeviceInfo, RuleVocab, KlimaState, IrButton, Klima, Discovery)


# ---- audit feed -------------------------------------------------------------
class AuditEvent(BaseModel):
    """One audit-log row (store_sql.recent_audit). id/ts are non-null; the five string fields are
    nullable at the DB layer (model defensively as str|null), null when the action has no such field."""

    model_config = _READ
    id: int
    ts: float
    actor: str | None
    action: str | None
    target: str | None
    detail: str | None
    result: str | None


class AuditFeed(BaseModel):
    """GET /api/audit — newest-first rows (capped 200); `events` always present ([] when audit off)."""

    model_config = ConfigDict(extra="forbid")
    events: list[AuditEvent]


# ---- live events (GET /api/events, Server-Sent Events) ----------------------
# Each SSE `data:` frame is one LiveEvent, a discriminated union on `type` (5 variants). `state`/`globals`
# carry a PARTIAL of DeviceInfo / Globals (only the changed keys). `conn` is NOT a server event (it's a
# UI-derived connection indicator). Keepalive `:` comment lines carry no data.
class Scene(BaseModel):
    """A function-button press riding an `activity` event. `kind` distinguishes the two frame types."""

    model_config = _READ
    id: int
    kind: Literal["scene", "central"]


class DeviceStatePatch(BaseModel):
    """A partial of DeviceInfo — exactly the live-state keys a frame can change (State.apply's `changed`
    map, after `scene` is popped). Discovery-side fields (power/type/confidence/battery/last_seen) never
    ride a `state` event — they arrive via a discovery_changed refetch."""

    model_config = _READ
    door: Annotated[str | None, Field(default=None, json_schema_extra=_OMIT)] = None
    motion: Annotated[bool | None, Field(default=None, json_schema_extra=_OMIT)] = None
    level: Annotated[int | None, Field(default=None, json_schema_extra=_OMIT)] = None
    switch: Annotated[bool | None, Field(default=None, json_schema_extra=_OMIT)] = None
    endpoints: Annotated[dict[str, bool] | None, Field(default=None, json_schema_extra=_OMIT)] = None
    setpoint: Annotated[float | None, Field(default=None, json_schema_extra=_OMIT)] = None
    thermostat_on: Annotated[bool | None, Field(default=None, json_schema_extra=_OMIT)] = None
    temperature: Annotated[float | None, Field(default=None, json_schema_extra=_OMIT)] = None
    power_w: Annotated[float | None, Field(default=None, json_schema_extra=_OMIT)] = None
    energy_kwh: Annotated[float | None, Field(default=None, json_schema_extra=_OMIT)] = None
    voltage_v: Annotated[float | None, Field(default=None, json_schema_extra=_OMIT)] = None


class GlobalsPatch(BaseModel):
    """A partial of Globals — the changed global field(s) in a `globals` event (1 key, or 2 for 433)."""

    model_config = _READ
    crib_temp: Annotated[float | None, Field(default=None, json_schema_extra=_OMIT)] = None
    outdoor_temp: Annotated[float | None, Field(default=None, json_schema_extra=_OMIT)] = None
    outdoor_humidity: Annotated[float | None, Field(default=None, json_schema_extra=_OMIT)] = None


class DiscoveryChangedEvent(BaseModel):
    """Re-fetch GET /api/discovery (identity/name/classifier change). No payload."""

    model_config = _READ
    type: Literal["discovery_changed"]


class StateEvent(BaseModel):
    model_config = _READ
    type: Literal["state"]
    node: int
    fields: DeviceStatePatch


class ActivityEvent(BaseModel):
    """Heatmap row-flash on every decoded frame with a node; `scene` rides only a function-button frame."""

    model_config = _READ
    type: Literal["activity"]
    node: int
    ts: float
    scene: Annotated[Scene, Field(default=None, json_schema_extra=_OMIT)] = None


class GlobalsEvent(BaseModel):
    model_config = _READ
    type: Literal["globals"]
    fields: GlobalsPatch


class KlimaEvent(BaseModel):
    model_config = _READ
    type: Literal["klima"]
    klima: KlimaState


LiveEvent = Annotated[
    DiscoveryChangedEvent | StateEvent | ActivityEvent | GlobalsEvent | KlimaEvent,
    Field(discriminator="type"),
]

_EVENT_MODELS = (AuditEvent, AuditFeed, Scene, DeviceStatePatch, GlobalsPatch,
                 DiscoveryChangedEvent, StateEvent, ActivityEvent, GlobalsEvent, KlimaEvent)


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
    user: str | None
    role: str | None


class OkResult(BaseModel):
    """A bare ``{ok: true}`` acknowledgement (e.g. /api/logout)."""

    model_config = ConfigDict(extra="forbid")
    ok: bool


_AUTH_MODELS = (LoginRequest, LoginSuccess, WhoAmI, OkResult)


# ---- user management + engineering observability ----------------------------
# Admin account administration (/api/users*), the self-service password change (/api/me/password,
# viewer floor — any signed-in user, with the CURRENT password verified), and two admin read-only
# diagnostics (/api/rf433, /api/db/stats). Request DTOs are pinned to the authoritative validators
# (web._username_error / web._password_error / store_sql.ROLE_RANK) by the binding tests.
Role = Literal["viewer", "operator", "admin"]  # the three RBAC roles (store_sql.ROLE_RANK keys)


class AddUserRequest(BaseModel):
    """POST /api/users — admin: create an account. ``username`` non-empty, ≤ 64 chars, no ``|`` or ``/``
    (the session token is ``username|expiry``); ``password`` 8..1024; ``role`` one of the three. A
    duplicate username is a 409 (never an overwrite)."""

    model_config = _STRICT
    username: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[^|/]+$")]
    password: Annotated[str, Field(min_length=8, max_length=1024)]
    role: Role


class SetRoleRequest(BaseModel):
    """POST /api/users/role — admin: change ANOTHER user's role. Refuses self (no self-demote) and the
    last enabled admin (server-enforced). ``username`` only has to be non-empty (it names an existing
    account, so the create-time char rules don't re-apply)."""

    model_config = _STRICT
    username: Annotated[str, Field(min_length=1)]
    role: Role


class SetDisabledRequest(BaseModel):
    """POST /api/users/disabled — admin: enable/disable ANOTHER account. Refuses self + the last enabled admin."""

    model_config = _STRICT
    username: Annotated[str, Field(min_length=1)]
    disabled: bool


class ResetPasswordRequest(BaseModel):
    """POST /api/users/reset-password — admin: set ANOTHER user's password (no current-password check).
    Refuses self (an admin rotates their OWN credential via /api/me/password, which verifies the current
    one). NOTE the field is ``new`` (not ``password``)."""

    model_config = _STRICT
    username: Annotated[str, Field(min_length=1)]
    new: Annotated[str, Field(min_length=8, max_length=1024)]


class ChangePasswordRequest(BaseModel):
    """POST /api/me/password — the signed-in user changes their OWN password. ``current`` is verified
    (a stolen cookie alone can't rotate it) before the ``new`` hash is written. ``new`` is 8..1024."""

    model_config = _STRICT
    current: str
    new: Annotated[str, Field(min_length=8, max_length=1024)]


class UserRow(BaseModel):
    """One account in the admin user list (``store_sql.list_users``) — username / role / disabled,
    NEVER the password hash."""

    model_config = _READ
    username: str
    role: Role
    disabled: bool


class UsersList(BaseModel):
    """GET /api/users — every account (admin). ``users`` is always present; there is NO top-level ``ok``."""

    model_config = ConfigDict(extra="forbid")
    users: list[UserRow]


# A decoded-433 field value: rf433._keep_field admits only FINITE str/int/float/bool scalars.
Rf433Value = bool | int | float | str


class Rf433Device(BaseModel):
    """One discovered 433 MHz device (``rf433.Rf433Registry.snapshot``). ``fields`` is the last decoded
    packet's JSON-safe scalars (rtl_433 ``time``/``mic`` noise stripped). All five keys are always present."""

    model_config = _READ
    key: str
    first_seen: float
    last_seen: float
    count: int
    fields: dict[str, Rf433Value]


class Rf433Feed(BaseModel):
    """GET /api/rf433 — every decoded 433 device, newest-seen first (admin observability). ``devices`` is
    ``[]`` until the local-433 feeder is running."""

    model_config = ConfigDict(extra="forbid")
    devices: list[Rf433Device]


class DbStats(BaseModel):
    """GET /api/db/stats — SQLite on-disk size + per-table row counts (admin). ``tables`` maps each
    tracked table name to its current row count."""

    model_config = _READ
    file_bytes: int
    tables: dict[str, int]


_USER_MODELS = (AddUserRequest, SetRoleRequest, SetDisabledRequest, ResetPasswordRequest,
                ChangePasswordRequest, UserRow, UsersList, Rf433Device, Rf433Feed, DbStats)


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
    ("GET", _P_WHOLE_HOME): "admin",
    ("POST", _P_WHOLE_HOME): "admin",
    ("GET", _P_SETTINGS): "viewer",
    ("POST", _P_SETTINGS): "viewer",
    ("GET", _P_ROOM_ICONS): "viewer",
    ("POST", _P_ROOM_ICONS): "admin",
    ("POST", "/api/login"): "public",
    ("POST", "/api/logout"): "public",
    ("GET", "/api/whoami"): "viewer",
    ("GET", "/api/discovery"): "viewer",
    ("GET", _P_AUTOMATIONS): "admin",
    ("POST", _P_AUTOMATIONS): "admin",
    ("POST", "/api/automations/delete"): "admin",
    ("GET", "/api/audit"): "viewer",
    ("GET", "/api/events"): "viewer",
    ("GET", _P_USERS): "admin",
    ("POST", _P_USERS): "admin",
    ("POST", "/api/users/role"): "admin",
    ("POST", "/api/users/disabled"): "admin",
    ("POST", "/api/users/reset-password"): "admin",
    ("POST", "/api/me/password"): "viewer",
    ("GET", "/api/rf433"): "admin",
    ("GET", "/api/db/stats"): "admin",
}


def _component_schemas() -> dict:
    """The OpenAPI ``components.schemas`` map for every model, with cross-refs under that path."""
    _, combined = models_json_schema(
        [(model, "validation")
         for model in (*_CONTROL_MODELS, ControlSuccess, ControlError, *_COMMAND_MODELS,
                       *_READ_MODELS, *_AUTH_MODELS, *_AUTOMATION_MODELS, *_EVENT_MODELS,
                       *_USER_MODELS)],
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
    # One SSE `data:` frame — a discriminated union on `type` over the 5 live-event variants.
    _events = {"discovery_changed": DiscoveryChangedEvent, "state": StateEvent, "activity": ActivityEvent,
               "globals": GlobalsEvent, "klima": KlimaEvent}
    schemas["LiveEvent"] = {
        "oneOf": [_ref(m.__name__) for m in _events.values()],
        "discriminator": {"propertyName": "type",
                          "mapping": {k: f"#/components/schemas/{m.__name__}" for k, m in _events.items()}},
        "description": "One Server-Sent-Events frame (the JSON after `data:`).",
    }
    return schemas


def build_openapi() -> dict:
    """Assemble the OpenAPI 3.1 document for the current contract slice. Deterministic (no clock/host),
    so the checked-in ``docs/api/openapi.json`` stays byte-stable across regenerations."""
    err = {"content": {_APP_JSON: {"schema": _ref("ControlError")}}}
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
                        "content": {_APP_JSON: {"schema": _ref("ControlRequest")}},
                    },
                    "responses": {
                        "200": {
                            "description": "command sent to the device",
                            "content": {_APP_JSON: {"schema": _ref("ControlSuccess")}},
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
                        "content": {_APP_JSON: {"schema": _ref("IrRequest")}},
                    },
                    "responses": {
                        # the IR success body is just {"ok": true} (no `sent` frame, unlike /api/control)
                        "200": {"description": "transmitted",
                                "content": {_APP_JSON: {"schema": _ref("OkResult")}}},
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
                        "content": {_APP_JSON: {"schema": _ref("SceneRequest")}},
                    },
                    "responses": {
                        "200": {"description": "scene dispatched",
                                "content": {_APP_JSON: {"schema": _ref("SceneResult")}}},
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
                                    "content": {_APP_JSON: {"schema": _ref("NameRequest")}}},
                    "responses": {
                        "200": {"description": "labels saved",
                                "content": {_APP_JSON: {"schema": _ref("OkResult")}}},
                        "400": {"description": "malformed", **err},
                        "500": {"description": "registry save failed", **err},
                    },
                }
            },
            _P_SETTINGS: {
                "get": {
                    "operationId": "getSettings",
                    "summary": "The logged-in user's UI preferences",
                    "description": "Requires the viewer role. All keys present, null when unset.",
                    "responses": {
                        "200": {"description": "the user's settings",
                                "content": {_APP_JSON: {"schema": _ref("Settings")}}},
                    },
                },
                "post": {
                    "operationId": "setSettings",
                    "summary": "Update UI preferences (partial — only the keys sent are persisted)",
                    "description": "Requires the viewer role.",
                    "requestBody": {"required": True,
                                    "content": {_APP_JSON: {"schema": _ref("SettingsUpdate")}}},
                    "responses": {
                        "200": {"description": "saved",
                                "content": {_APP_JSON: {"schema": _ref("OkResult")}}},
                        "400": {"description": "malformed", **err},
                    },
                },
            },
            _P_ROOM_ICONS: {
                "get": {
                    "operationId": "getRoomIcons",
                    "summary": "Shared room→emoji map",
                    "description": "Requires the viewer role. A bare object (room name → emoji); {} when none set.",
                    "responses": {
                        "200": {
                            "description": "the room→icon map",
                            "content": {_APP_JSON: {
                                "schema": {"type": "object", "additionalProperties": {"type": "string"}}}},
                        },
                    },
                },
                "post": {
                    "operationId": "setRoomIcon",
                    "summary": "Set one room's emoji (send icon=\"\" to clear)",
                    "description": _ADMIN_ROLE_DESC,
                    "requestBody": {"required": True,
                                    "content": {_APP_JSON: {"schema": _ref("RoomIconRequest")}}},
                    "responses": {
                        "200": {"description": "saved",
                                "content": {_APP_JSON: {"schema": _ref("OkResult")}}},
                        "400": {"description": "malformed", **err},
                    },
                },
            },
            _P_WHOLE_HOME: {
                "get": {
                    "operationId": "getWholeHome",
                    "summary": "Which devices are opted out of the house-wide \"all\" sweeps",
                    "description": f"{_ADMIN_ROLE_DESC} Registry-only config — kept off DeviceInfo so the "
                                   "device snapshot stays wire-stable for pinned native clients.",
                    "responses": {
                        "200": {"description": "the opt-out config",
                                "content": {_APP_JSON: {"schema": _ref("WholeHomeConfig")}}},
                    },
                },
                "post": {
                    "operationId": "setWholeHome",
                    "summary": "Opt one device in/out of the house-wide \"all\" sweeps",
                    "description": _ADMIN_ROLE_DESC,
                    "requestBody": {"required": True,
                                    "content": {_APP_JSON: {"schema": _ref("WholeHomeRequest")}}},
                    "responses": {
                        "200": {"description": "saved",
                                "content": {_APP_JSON: {"schema": _ref("OkResult")}}},
                        "400": {"description": "malformed", **err},
                        "500": {"description": "registry save failed", **err},
                    },
                },
            },
            "/api/audit": {
                "get": {
                    "operationId": "audit",
                    "summary": "Recent audit-log rows (newest first, capped 200)",
                    "description": "Requires the viewer role.",
                    "responses": {
                        "200": {"description": "the audit feed",
                                "content": {_APP_JSON: {"schema": _ref("AuditFeed")}}},
                    },
                }
            },
            "/api/events": {
                "get": {
                    "operationId": "events",
                    "summary": "Live updates (Server-Sent Events)",
                    "description": "Requires the viewer role. An unbounded text/event-stream; each `data:` "
                    "frame is one LiveEvent. `:`-comment keepalives carry no data. Browser/native clients "
                    "auto-reconnect (the stream closes on a max-lifetime deadline).",
                    "responses": {
                        "200": {
                            "description": "the event stream (one LiveEvent per data frame)",
                            "content": {"text/event-stream": {"schema": _ref("LiveEvent")}},
                        },
                    },
                }
            },
            "/api/login": {
                "post": {
                    "operationId": "login",
                    "summary": "Exchange credentials for a session (cookie + optional bearer token)",
                    "description": "Public. Sets the httponly session cookie; with `bearer: true` the 200 "
                    "body also carries a `token` for `Authorization: Bearer` (native clients).",
                    "requestBody": {
                        "required": True,
                        "content": {_APP_JSON: {"schema": _ref("LoginRequest")}},
                    },
                    "responses": {
                        "200": {
                            "description": "authenticated",
                            "content": {_APP_JSON: {"schema": _ref("LoginSuccess")}},
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
                            "content": {_APP_JSON: {"schema": _ref("OkResult")}},
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
                            "content": {_APP_JSON: {"schema": _ref("WhoAmI")}},
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
                            "content": {_APP_JSON: {"schema": _ref("Discovery")}},
                        }
                    },
                }
            },
            _P_AUTOMATIONS: {
                "get": {
                    "operationId": "listAutomations",
                    "summary": "List every saved automation rule",
                    "description": "Requires the admin role (rules can carry presence-trigger MACs).",
                    "responses": {
                        "200": {"description": "the rules",
                                "content": {_APP_JSON: {"schema": _ref("AutomationsList")}}},
                    },
                },
                "post": {
                    "operationId": "saveAutomation",
                    "summary": "Create or replace a rule (the body IS the rule; id is client-supplied)",
                    "description": "Requires the admin role. Authoritative validation is Rule.from_dict.",
                    "requestBody": {"required": True,
                                    "content": {_APP_JSON: {"schema": _ref("RuleInput")}}},
                    "responses": {
                        "200": {"description": "saved",
                                "content": {_APP_JSON: {"schema": _ref("AutomationSaved")}}},
                        "400": {"description": "invalid rule", **err},
                        "500": {"description": "save failed", **err},
                    },
                },
            },
            "/api/automations/delete": {
                "post": {
                    "operationId": "deleteAutomation",
                    "summary": "Delete a rule by id (deleted=false, still 200, for an absent id)",
                    "description": _ADMIN_ROLE_DESC,
                    "requestBody": {
                        "required": True,
                        "content": {_APP_JSON: {"schema": {
                            "type": "object", "required": ["id"], "additionalProperties": False,
                            "properties": {"id": {"type": "string"}}}}},
                    },
                    "responses": {
                        "200": {"description": "deleted (or no-op)",
                                "content": {_APP_JSON: {"schema": _ref("AutomationDeleted")}}},
                        "400": {"description": "invalid id", **err},
                        "500": {"description": "save failed", **err},
                    },
                },
            },
            _P_USERS: {
                "get": {
                    "operationId": "listUsers",
                    "summary": "List every account (username / role / disabled — never the password hash)",
                    "description": "Requires the admin role. 409 when the SQLite users backend isn't active.",
                    "responses": {
                        "200": {"description": "the accounts",
                                "content": {_APP_JSON: {"schema": _ref("UsersList")}}},
                        "409": {"description": "user management requires the SQLite backend", **err},
                    },
                },
                "post": {
                    "operationId": "addUser",
                    "summary": "Create a new account",
                    "description": "Requires the admin role. 409 on a duplicate username (never an overwrite).",
                    "requestBody": {"required": True,
                                    "content": {_APP_JSON: {"schema": _ref("AddUserRequest")}}},
                    "responses": {
                        "200": {"description": "account created",
                                "content": {_APP_JSON: {"schema": _ref("OkResult")}}},
                        "400": {"description": "invalid username / password / role", **err},
                        "409": {"description": "duplicate username (or SQLite backend inactive)", **err},
                    },
                },
            },
            "/api/users/role": {
                "post": {
                    "operationId": "setUserRole",
                    "summary": "Change another user's role",
                    "description": "Requires the admin role. Refuses self (403) and the last enabled admin (409).",
                    "requestBody": {"required": True,
                                    "content": {_APP_JSON: {"schema": _ref("SetRoleRequest")}}},
                    "responses": {
                        "200": {"description": "role changed",
                                "content": {_APP_JSON: {"schema": _ref("OkResult")}}},
                        "400": {"description": "invalid username / role", **err},
                        "403": {"description": "cannot change your own role", **err},
                        "404": {"description": _NO_SUCH_USER, **err},
                        "409": {"description": "would leave no enabled admin (or SQLite backend inactive)", **err},
                    },
                },
            },
            "/api/users/disabled": {
                "post": {
                    "operationId": "setUserDisabled",
                    "summary": "Enable or disable another account",
                    "description": "Requires the admin role. Refuses self (403) and the last enabled admin (409).",
                    "requestBody": {"required": True,
                                    "content": {_APP_JSON: {"schema": _ref("SetDisabledRequest")}}},
                    "responses": {
                        "200": {"description": "updated",
                                "content": {_APP_JSON: {"schema": _ref("OkResult")}}},
                        "400": {"description": "invalid username / disabled flag", **err},
                        "403": {"description": "cannot disable your own account", **err},
                        "404": {"description": _NO_SUCH_USER, **err},
                        "409": {"description": "would leave no enabled admin (or SQLite backend inactive)", **err},
                    },
                },
            },
            "/api/users/reset-password": {
                "post": {
                    "operationId": "resetUserPassword",
                    "summary": "Reset another user's password (admin reset — no current-password check)",
                    "description": "Requires the admin role. Refuses self (403); rotate your own via "
                    "/api/me/password.",
                    "requestBody": {"required": True,
                                    "content": {_APP_JSON: {"schema": _ref("ResetPasswordRequest")}}},
                    "responses": {
                        "200": {"description": "password reset",
                                "content": {_APP_JSON: {"schema": _ref("OkResult")}}},
                        "400": {"description": "invalid username / password", **err},
                        "403": {"description": "use change-password for your own account", **err},
                        "404": {"description": _NO_SUCH_USER, **err},
                        "409": {"description": "SQLite backend inactive", **err},
                    },
                },
            },
            "/api/me/password": {
                "post": {
                    "operationId": "changeOwnPassword",
                    "summary": "Change your own password (verifies the current one)",
                    "description": "Requires a session (viewer+). 401 when not signed in; 403 when the "
                    "current password is wrong.",
                    "requestBody": {"required": True,
                                    "content": {_APP_JSON: {"schema": _ref("ChangePasswordRequest")}}},
                    "responses": {
                        "200": {"description": "password changed",
                                "content": {_APP_JSON: {"schema": _ref("OkResult")}}},
                        "400": {"description": "invalid / missing fields", **err},
                        "401": {"description": "not signed in", **err},
                        "403": {"description": "current password is incorrect", **err},
                        "409": {"description": "account no longer exists (or SQLite backend inactive)", **err},
                    },
                },
            },
            "/api/rf433": {
                "get": {
                    "operationId": "rf433",
                    "summary": "Discovered 433 MHz devices (newest-seen first)",
                    "description": "Requires the admin role. Engineering observability; empty until the "
                    "local-433 feeder runs.",
                    "responses": {
                        "200": {"description": "the 433 device feed",
                                "content": {_APP_JSON: {"schema": _ref("Rf433Feed")}}},
                    },
                }
            },
            "/api/db/stats": {
                "get": {
                    "operationId": "dbStats",
                    "summary": "SQLite file size + per-table row counts",
                    "description": _ADMIN_ROLE_DESC,
                    "responses": {
                        "200": {"description": "the database stats",
                                "content": {_APP_JSON: {"schema": _ref("DbStats")}}},
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
