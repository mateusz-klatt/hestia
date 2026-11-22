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
# 0.3.0: /api/whole-home grows per-GANG opt-outs — WholeHomeConfig.excluded_endpoints +
#        WholeHomeRequest.ep (still confined to the whole-home surface; DeviceInfo untouched).
# 0.4.0: response schemas become TOLERANT READERS (additionalProperties:false stripped from every
#        non-request-only schema in the emitted doc) — so a client built from this contract never
#        breaks on a future additive response field. Wire DATA unchanged; requests stay strict.
# 0.5.0: automation grammar grows two additive options — a `motion` state field (PIR triggers) and an
#        optional `endpoint` (1|2) on a state predicate/trigger to target ONE gang of a multi-gang
#        switch. Confined to the Rule/Trigger/StatePredicate/RuleVocab schemas (automations surface);
#        no control/device shape changes, and the iOS client doesn't consume automations.
# 0.6.0: Globals (+ the `globals` SSE patch) grow two additive READ-ONLY fields — `outdoor_temp_ts`
#        (ISO ts of the last outdoor sample) and `outdoor_battery_ok` — for the dashboard freshness /
#        low-battery badge. Additive response/event fields only; tolerant readers (0.4.0) keep pinned
#        clients safe, and no request/control shape changes.
# 0.7.0: Globals (+ SSE patch) grow one more additive READ-ONLY field — `crib_temp_ts` — extending the
#        freshness badge to the baby-monitor temperature (the same "N ago" as outdoor). Still additive /
#        tolerant-reader-safe; no request/control changes. (`outdoor_battery_ok` is now opt-in via
#        HESTIA_RTL433_BATTERY_WARN, so it reads null by default — a behaviour/default change, not a wire change.)
# 0.8.0: SceneRequest grows the additive `blinds_set` op + an optional `value` (0-100 %) — the whole-home
#        blind-position slider (set every blind to a chosen position, not just the up/down extremes). A
#        request-only change; old clients simply never send the new op/field, so it is backward-compatible.
# 0.9.0: SceneRequest.value for `blinds_set` is now a WIRE `cover` position 0-99 (was 0-100 %) — the UI's
#        perceptual blind-scale curve (non-linear, to match how venetian blinds open) moved client-side, so
#        the slider speaks raw wire and the backend just clamps. Request-only; only this web UI sends
#        `blinds_set` (no native client does), so the tightened bound + changed unit is safe.
# 0.10.0: local calendar read/write surface, with optimistic revisions and sync health.
# 0.11.0: portable calendar content preserves unspecified end times.
# 0.12.0: calendar creates require a durable per-draft idempotency key.
# 0.13.0: private owner-scoped stored budget reads and versioned local controls.
# 0.14.0: safe account display metadata, separate from currencies observed in API balances.
# 0.15.0: durable owner refresh jobs, per-connection background opt-in and attention state.
# 0.16.0: browser-bound production bank consent, renewal and durable managed credentials.
# 0.17.0: owner-private budget reports, classifications, rule previews and transfer decisions.
# 0.18.0: owner-private read-only recurring-charge hints under /api/budget/analytics/recurring.
# 0.19.0: owner-private persisted monthly category spending limits (migration 0017).
# 0.20.0: owner-private persisted mortgage reminder schedules and payment marks (migration 0018, evaluation-only).
# 0.21.0: read-only bank consent renewal reminder evaluation (GET /api/budget/consent/renewal-reminders, zero migration).
# 0.22.0: read-only bank consent reconciliation classification (GET /api/budget/consent/reconcile, zero migration).
# 0.23.0: owner-private live active alerts inbox and durable anti-ABA read markers (migration 0019,
#         GET /api/budget/analytics/inbox, POST /api/budget/analytics/inbox-mark).
# 0.24.0: additive live owner-known `ref` on GET /api/budget/analytics/inbox items (zero migration;
#         subject_id remains the durable read-marker key).
# 0.25.0: read-only quarantine inspection (GET /api/budget/analytics/quarantine, zero migration;
#         exact counts + bounded safe ref/date/status/reason details without descriptions or amounts).
# 0.26.0: read-only upcoming commitments forecast (GET /api/budget/analytics/forecast, zero migration).
# 0.27.0: read-only owner-private monthly budget dashboard (GET /api/budget/analytics/monthly-budget, zero migration).
# 0.28.0: additive optional income window on monthly-budget configuration (zero migration).
# 0.29.0: read-only owner-private monthly budget charts (GET /api/budget/analytics/monthly-budget/charts, zero migration).
# 0.30.0: batch rule preview and save (POST /api/budget/analytics/rule-batch-preview and rule-batch-save,
#         zero migration). This branch merges first; the open follow-ups take 0.31.0 and 0.32.0.
# 0.31.0: additive coverage.pending_row_keys on monthly-budget responses (zero migration).
CONTRACT_VERSION = "0.31.0"
OPENAPI_PATH = Path(__file__).resolve().parent.parent / "docs" / "api" / "openapi.json"

# De-duplicated string literals (SonarPython S1192): the OpenAPI content-type, the paths reused
# across the role map + the doc, and a couple of repeated descriptions/messages.
_APP_JSON = "application/json"
_P_SETTINGS = "/api/settings"
_P_ROOM_ICONS = "/api/rooms/icons"
_P_AUTOMATIONS = "/api/automations"
_P_USERS = "/api/users"
_P_WHOLE_HOME = "/api/whole-home"
_P_CALENDARS = "/api/calendars"
_P_CALENDAR_EVENTS = _P_CALENDARS + "/events"
_P_CALENDAR_DELETE = _P_CALENDAR_EVENTS + "/delete"
_P_BUDGET = "/api/budget/"
_P_BUDGET_CONNECTIONS = _P_BUDGET + "connections"
_P_BUDGET_ACCOUNTS = _P_BUDGET + "accounts"
_P_BUDGET_STATUS = _P_BUDGET + "status"
_P_BUDGET_TRANSACTIONS = _P_BUDGET + "transactions"
_P_BUDGET_BALANCES = _P_BUDGET + "balances"
_P_BUDGET_LIMITS = _P_BUDGET + "limits"
_P_BUDGET_SELECTION = _P_BUDGET + "account-selection"
_P_BUDGET_KIND = _P_BUDGET + "account-kind"
_P_BUDGET_CONNECTION_STATE = _P_BUDGET + "connection-state"
_P_BUDGET_BACKGROUND = _P_BUDGET + "background-selection"
_P_BUDGET_REFRESH = _P_BUDGET + "refresh"
_P_BUDGET_JOBS = _P_BUDGET + "jobs"
_ADMIN_ROLE_DESC = "Requires the admin role."
_NO_SUCH_USER = "no such user"

# Strict, exactly like the aiohttp handlers: reject unknown fields, bool-as-int, and int-as-float — so a
# payload the DTO accepts is one ``_validate_control_payload`` accepts (the binding test pins this).
_STRICT = ConfigDict(extra="forbid", strict=True)

# Read/response config: forbid unknown fields IN PYTHON (a new backend key fails the binding tests =
# drift detection), but non-strict (these DESCRIBE output, so an int in a float field is fine).
# NOTE: the strictness is deliberately NOT emitted into the artifact — ``_make_responses_tolerant``
# strips ``additionalProperties: false`` from every response schema so generated clients are tolerant
# readers (the #122 Vesta lesson: a strict decoder turns an additive response field into a crash).
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
    op: Literal["lights_off", "lights_on", "blinds_down", "blinds_up", "blinds_set"]
    # `blinds_set` drives every blind to this wire `cover` position 0-99 (the whole-home slider; the UI
    # applies its perceptual blind-scale curve before sending). Ignored/absent for the on/off scenes;
    # required for `blinds_set` (server-enforced cross-field, not expressible here).
    value: Annotated[int | None, Field(default=None, ge=0, le=99, json_schema_extra=_OMIT)] = None


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
    """GET /api/whole-home — what is opted out of the house-wide "all lights / all blinds" sweeps.
    Registry-only config (deliberately NOT in DeviceInfo, so adding the opt-out never changes the
    device-snapshot wire shape a pinned native client decodes). ``excluded_nodes`` = whole devices;
    ``excluded_endpoints`` = single gangs of a multi-gang switch (node id → sorted gang numbers)."""

    model_config = _READ
    excluded_nodes: list[int]
    excluded_endpoints: dict[str, list[int]]


class WholeHomeRequest(BaseModel):
    """POST /api/whole-home — opt one device in (``exclude``=false) / out (true) of the house-wide
    "all" sweeps. With ``ep`` (gang 1 or 2, like the control op's ``endpoint``) the opt-out targets
    that single gang of a multi-gang switch instead of the whole node."""

    model_config = ConfigDict(extra="forbid", strict=True)
    node: NodeId
    exclude: bool
    ep: Annotated[int | None, Field(default=None, ge=1, le=2, json_schema_extra=_OMIT)] = None


_COMMAND_MODELS = (IrRequest, SceneRequest, SceneResult, NameRequest, Settings, SettingsUpdate,
                   RoomIconRequest, WholeHomeConfig, WholeHomeRequest)


# ---- automation rules (the Rule.to_dict wire shape) ------------------------
# Output (Rule.to_dict) always emits all 7 keys; trigger/condition dicts carry only known keys (extra
# forbid), but ACTIONS pass author fields through verbatim (extra allow). Conditions discriminate by the
# ABSENCE of `type` (state predicate) vs type="time_window". Mirrors hestia/automations.py.
_RULE_FIELDS = frozenset({"door", "motion", "level", "switch", "setpoint", "thermostat_on",
                          "temperature", "power_w", "energy_kwh", "voltage_v", "crib_temp",
                          "outdoor_temp"})
RuleField = Literal["door", "motion", "level", "switch", "setpoint", "thermostat_on", "temperature",
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
    # one gang (1|2) of a multi-gang `switch`; omitted otherwise. Mirrors the control op's `endpoint`.
    endpoint: Annotated[int, Field(default=None, ge=1, le=2, json_schema_extra=_OMIT)] = None


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
    # one gang (1|2) of a multi-gang `switch`; omitted otherwise. Mirrors the control op's `endpoint`.
    endpoint: Annotated[int, Field(default=None, ge=1, le=2, json_schema_extra=_OMIT)] = None


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
    null when its poller is off. ``crib_temp_ts`` / ``outdoor_temp_ts`` are the ISO ts of the last
    crib / outdoor sample (the "N ago" freshness badge); ``outdoor_battery_ok`` is the local 433 sensor's
    battery flag (false = low) — null when HESTIA_RTL433_BATTERY_WARN is off (the default)."""

    model_config = _READ
    crib_temp: float | None
    crib_temp_ts: str | None
    outdoor_temp: float | None
    outdoor_humidity: float | None
    outdoor_temp_ts: str | None
    outdoor_battery_ok: bool | None


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
    """A partial of Globals — the changed global field(s) in a `globals` event (2 keys for the niania /
    Open-Meteo poll: value + its `*_ts`; up to 4 for a 433 reading: temp, humidity, ts, battery)."""

    model_config = _READ
    crib_temp: Annotated[float | None, Field(default=None, json_schema_extra=_OMIT)] = None
    crib_temp_ts: Annotated[str | None, Field(default=None, json_schema_extra=_OMIT)] = None
    outdoor_temp: Annotated[float | None, Field(default=None, json_schema_extra=_OMIT)] = None
    outdoor_humidity: Annotated[float | None, Field(default=None, json_schema_extra=_OMIT)] = None
    outdoor_temp_ts: Annotated[str | None, Field(default=None, json_schema_extra=_OMIT)] = None
    outdoor_battery_ok: Annotated[bool | None, Field(default=None, json_schema_extra=_OMIT)] = None


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


class CalendarDate(BaseModel):
    """All-day calendar date; an event's end date is exclusive."""

    model_config = _STRICT
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class CalendarDateTime(BaseModel):
    """An RFC3339 instant, optionally expressed in an IANA time zone."""

    model_config = _STRICT
    dateTime: str
    timeZone: Annotated[str, Field(default=None, json_schema_extra=_OMIT)] = None


CalendarTime = CalendarDate | CalendarDateTime


class CalendarCancelledInstance(BaseModel):
    model_config = _STRICT
    originalStartTime: CalendarTime
    status: Literal["cancelled"]


class CalendarInstance(BaseModel):
    model_config = _STRICT
    originalStartTime: CalendarTime
    start: CalendarTime
    end: CalendarTime
    endTimeUnspecified: bool = False
    summary: str = ""
    description: str = ""
    location: str = ""
    status: Literal["confirmed", "tentative"] = "confirmed"
    transparency: Literal["opaque", "transparent"] = "opaque"
    visibility: Literal["default", "public", "private", "confidential"] = "default"


class CalendarPayload(BaseModel):
    """Portable core content; recurrence remains an unexpanded RFC5545 series.

    The event codec validates dates, time zones, rule semantics and exceptions.
    Invitations, alarms and provider presentation fields are outside this contract.
    """

    model_config = _STRICT
    start: CalendarTime
    end: CalendarTime
    endTimeUnspecified: bool = Field(default=False, description="The end is unknown; end holds a compatibility placeholder (same instant, or next day for all-day).")
    summary: str = ""
    description: str = ""
    location: str = ""
    recurrence: list[str] = Field(default_factory=list)
    exceptions: list[CalendarCancelledInstance | CalendarInstance] = Field(default_factory=list)
    status: Literal["confirmed", "tentative", "cancelled"] = "confirmed"
    transparency: Literal["opaque", "transparent"] = "opaque"
    visibility: Literal["default", "public", "private", "confidential"] = "default"


class CalendarCreateRequest(BaseModel):
    model_config = _STRICT
    group: str
    payload: CalendarPayload
    idempotency_key: Annotated[str, Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$",
        description="Stable per-draft create key, reused after an uncertain response. Reuse with different content returns 409; retries never restore deleted events.")]


class CalendarUpdateRequest(BaseModel):
    model_config = _STRICT
    group: str
    payload: CalendarPayload
    id: Annotated[str, Field(min_length=1)]
    revision: Annotated[int, Field(ge=1)]


class CalendarDeleteRequest(BaseModel):
    model_config = _STRICT
    group: str
    id: Annotated[str, Field(min_length=1)]
    revision: Annotated[int, Field(ge=1)]


class CalendarEvent(BaseModel):
    model_config = _READ
    id: str
    group_id: str
    uid: str
    payload: CalendarPayload
    revision: int
    deleted: bool
    modified_at: float


class CalendarGroup(BaseModel):
    model_config = _READ
    id: str


class CalendarEndpointStatus(BaseModel):
    model_config = _READ
    id: str
    group_id: str
    enabled: bool
    last_success: float | None
    last_error: str | None


class CalendarPendingOperation(BaseModel):
    model_config = _READ
    endpoint_id: str
    event_id: str
    revision: int
    attempts: int
    next_attempt: float
    last_error: str | None


class CalendarSyncStatus(BaseModel):
    model_config = _READ
    endpoints: list[CalendarEndpointStatus]
    pending: list[CalendarPendingOperation]


class Calendars(BaseModel):
    model_config = _READ
    enabled: bool
    groups: list[CalendarGroup]
    events: list[CalendarEvent]
    status: CalendarSyncStatus


class CalendarSaved(BaseModel):
    model_config = _READ
    ok: Literal[True]
    event: CalendarEvent


class CalendarError(BaseModel):
    model_config = _READ
    error: str


_CALENDAR_MODELS = (CalendarCreateRequest, CalendarUpdateRequest, CalendarDeleteRequest,
                    Calendars, CalendarSaved, CalendarError)


BudgetId = Annotated[str, Field(min_length=1, max_length=128)]
BudgetVersion = Annotated[int, Field(ge=1)]
BudgetConfirmedKind = Literal["current", "savings", "credit_card", "other"]


class BudgetSelectionRequest(BaseModel):
    """Select one owned, configured account using its current control version."""

    model_config = _STRICT
    account_id: BudgetId
    selected: bool
    expected_version: BudgetVersion


class BudgetKindRequest(BaseModel):
    """Explicitly confirm an owned account's kind without inferring it from currency."""

    model_config = _STRICT
    account_id: BudgetId
    kind: BudgetConfirmedKind
    expected_version: BudgetVersion


class BudgetConnectionStateRequest(BaseModel):
    """Pause or resume a local connection; no provider session is created or closed."""

    model_config = _STRICT
    connection_id: BudgetId
    paused: bool
    expected_version: BudgetVersion


class BudgetBackgroundSelectionRequest(BaseModel):
    """Opt one owned connection in or out of unattended acquisition; the global flag never implies it."""

    model_config = _STRICT
    connection_id: BudgetId
    enabled: bool
    expected_version: BudgetVersion


BudgetOperation = Literal["transactions", "balances"]


class BudgetRefreshRequest(BaseModel):
    """Queue one owner refresh; omitted account/operation means every selected component."""

    model_config = _STRICT
    connection_id: BudgetId
    expected_version: BudgetVersion
    account_id: BudgetId | None = None
    operation: BudgetOperation | None = None


class BudgetConsentStartRequest(BaseModel):
    """Start one browser-bound consent for an explicitly configured connection."""

    model_config = _STRICT
    connection_id: BudgetId
    expected_version: BudgetVersion
    intent: Literal["connect", "renew"]


class BudgetConsentFlowRequest(BaseModel):
    model_config = _STRICT
    flow_id: Annotated[str, Field(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")]


class BudgetConsentFlow(BaseModel):
    """Safe local browser-flow state; no provider identifiers or credentials."""

    model_config = _READ
    id: str
    connection_id: str
    intent: Literal["connect", "renew"]
    status: str
    error: str | None
    created_at: float
    expires_at: float
    candidate_consent_id: str | None


class BudgetConsent(BaseModel):
    model_config = _READ
    id: str
    connection_id: str
    provenance: Literal["hestia_created"]
    status: Literal["UNADOPTED", "ACTIVE", "RETIRED"]
    valid_until: float | None
    generation: int
    created_at: float


class BudgetConsentPending(BaseModel):
    model_config = _READ
    pending: list[BudgetConsentFlow]


class BudgetConsents(BaseModel):
    model_config = _READ
    consents: list[BudgetConsent]


class BudgetConsentRenewalReminder(BaseModel):
    """Read-only private consent-renewal status derived from stored provider valid_until and attention."""

    model_config = _READ
    connection_id: str
    bank_name: str
    valid_until: float | None
    seconds_remaining: int | None
    days_remaining: int | None
    status: Literal["attention_required", "expired", "due_soon", "healthy", "unknown_expiry"]
    active_milestone: int | Literal["attention"] | None
    reminder_due: bool
    paused: bool
    attention: str | None
    destination_kind: Literal["owner_inbox", "owner_push", "owner_email"]
    rule_version: int


class BudgetConsentRenewalReminders(BaseModel):
    """Owner-private consent renewal reminders evaluated on demand; no bank requests or delivery."""

    model_config = _READ
    evaluated_at: float
    lead_days: list[int]
    rule_version: int
    reminders: list[BudgetConsentRenewalReminder]


class BudgetConsentReconcileSummary(BaseModel):
    model_config = _READ
    consents_total: int
    flows_total: int
    needs_owner_review: int
    remote_close_eligible: int
    local_retire_eligible: int


class BudgetConsentReconcileConsent(BaseModel):
    """Informational consent classification; consent_id is an opaque non-reversible hash, never a provider session ID."""

    model_config = _READ
    consent_id: str
    connection_id: str
    provenance: Literal["hestia_created", "external_file", "poc_retired"]
    status: Literal["ACTIVE", "UNADOPTED", "RETIRED"]
    valid_until: float | None
    classification: str
    needs_owner_review: bool
    remote_close_eligible: bool
    local_retire_eligible: bool


class BudgetConsentReconcileFlow(BaseModel):
    """Informational consent-flow classification; flow_id and candidate_consent_id are opaque non-reversible hashes."""

    model_config = _READ
    flow_id: str
    connection_id: str
    status: str
    classification: str
    needs_owner_review: bool
    local_retire_eligible: bool
    candidate_consent_id: str | None


class BudgetConsentReconcile(BaseModel):
    """Read-only reconciliation report; *_eligible fields are informational only and trigger no deletion or remote closure."""

    model_config = _READ
    evaluated_at: float
    retention_days: int
    rule_version: int
    summary: BudgetConsentReconcileSummary
    consents: list[BudgetConsentReconcileConsent]
    flows: list[BudgetConsentReconcileFlow]


class BudgetConsentStarted(BaseModel):
    model_config = _READ
    ok: Literal[True]
    flow_id: str
    authorization_url: str
    expires_at: float


class BudgetConsentCompleted(BaseModel):
    model_config = _READ
    ok: Literal[True]
    flow_id: str
    status: str = Field(description="adopted, reconciliation_required or exchange_uncertain; never replay an uncertain exchange.")
    consent: BudgetConsent | None = None
    error: str | None = None


class BudgetConsentCanceled(BaseModel):
    model_config = _READ
    ok: Literal[True]
    flow_id: str
    status: str = Field(description="canceled, or the unchanged terminal flow status when cancellation is no longer applicable.")


class BudgetComponentStatus(BaseModel):
    """Independent API freshness and effective component/shared retry policy."""

    model_config = _READ
    last_attempt: float | None
    last_success: float | None
    last_error: str | None
    failures: int
    next_attempt: float
    state: str = Field(description="Currently blocked, running, error, deferred, never, ready or ok; clients must handle future values.")
    retry_at: float | None
    suspended: bool
    stale: bool
    age_seconds: float | None
    stale_after_seconds: int


class BudgetComponents(BaseModel):
    model_config = _READ
    transactions: BudgetComponentStatus
    balances: BudgetComponentStatus


class BudgetAccountDisplay(BaseModel):
    """Allowlisted display labels and their provenance, never bank account identity."""

    model_config = _READ
    product: str | None
    currency: str | None
    masked_suffix: str | None
    source: str = Field(description="Currently offline_import; clients must handle future values.")
    observed_at: float = Field(description="Time of the last accepted metadata patch; preserved fields can be older.")
    imported_at: float


class BudgetAccount(BaseModel):
    """Owner-visible control metadata; opaque local IDs never reveal bank identities."""

    model_config = _READ
    id: str
    connection: str
    configured: bool
    selected: bool
    active: bool
    paused: bool
    control_version: int
    kind: str = Field(description="Stored account kind; clients must handle future values.")
    kind_confirmed_at: float | None
    kind_confirmed_by: str | None
    kind_confirmation_origin: str | None
    components: BudgetComponents
    display: BudgetAccountDisplay | None = None
    observed_balance_currencies: list[str] = Field(default_factory=list,
        description="Sorted unique currencies from current stored API balances only; independent of declared display currency.")


BudgetAttention = Literal["authorization", "invalid_session", "expired_session", "session_mismatch", "configuration"]


class BudgetConnection(BaseModel):
    model_config = _READ
    id: str
    enabled: bool
    paused: bool
    background_enabled: bool
    attention: BudgetAttention | None = Field(description="Unattended acquisition stopped until an explicit "
                                                          "owner refresh succeeds; reconnect may be required.")
    control_version: int
    bank_name: str
    country: str
    valid_until: float | None
    renewal_due: bool
    last_attempt: float | None
    last_success: float | None
    last_error: str | None


class BudgetCapabilities(BaseModel):
    """Implemented surfaces; refresh/background_polling follow the default-off executor flags."""

    model_config = _READ
    stored_reads: bool
    account_controls: bool
    refresh: bool
    background_selection: bool
    background_polling: bool
    consent: bool
    analytics: Annotated[bool, Field(default=False, json_schema_extra=_OMIT)] = False


class BudgetConnections(BaseModel):
    model_config = _READ
    connections: list[BudgetConnection]
    capabilities: BudgetCapabilities


class BudgetAccounts(BaseModel):
    model_config = _READ
    accounts: list[BudgetAccount]


class BudgetSharedState(BaseModel):
    model_config = _READ
    next_attempt: float
    retry_at: float | None
    suspended: bool


class BudgetAccountStatus(BaseModel):
    model_config = _READ
    id: str
    enabled: bool
    configured: bool
    paused: bool
    control_version: int
    kind: str
    kind_confirmed_at: float | None
    last_attempt: float | None
    last_success: float | None
    last_error: str | None
    components: BudgetComponents


class BudgetConnectionStatus(BaseModel):
    model_config = _READ
    connection: str
    owner: str
    enabled: bool
    paused: bool
    control_version: int
    selection_policy_version: Annotated[int, Field(ge=1)]
    configured_accounts: int
    selected_accounts: int
    transactions: int
    unresolved: int
    uncertain_transactions: int
    offline_imports: int
    offline_transactions: int
    offline_unresolved: int
    last_attempt: float | None
    last_success: float | None
    next_attempt: float
    last_error: str | None
    failures: int
    valid_until: float | None
    request_limits: BudgetSharedState
    renewal_due: bool
    accounts: list[BudgetAccountStatus]


class BudgetStatus(BaseModel):
    model_config = _READ
    status: list[BudgetConnectionStatus]


class BudgetTransaction(BaseModel):
    """Canonical booked observation; amount is an exact unsigned decimal string."""

    model_config = _READ
    entry_reference: str
    amount: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)?$")
    currency: str
    credit_debit_indicator: Literal["CRDT", "DBIT"]
    status: Literal["BOOK"]
    booking_date: str | None
    value_date: str | None
    transaction_date: str | None
    remittance_information: list[str]
    source: str = Field(description="Currently api or offline_import; clients must handle future values.")
    observed_at: float
    api_observed_at: float | None


class BudgetTransactions(BaseModel):
    """A bounded keyset page; totals cover the whole owned account."""

    model_config = _READ
    account_id: str
    transactions: list[BudgetTransaction]
    account_total: int = Field(description="Whole-account canonical booked count, independent of date filters and pagination.")
    uncertain_total: int
    next_cursor: str | None


class BudgetMoney(BaseModel):
    model_config = _READ
    amount: str = Field(pattern=r"^-?[0-9]+(?:\.[0-9]+)?$")
    currency: str


class BudgetBalance(BaseModel):
    model_config = _READ
    name: str
    balance_type: str
    balance_amount: BudgetMoney
    reference_date: str | None


class BudgetArchivedBalances(BaseModel):
    """Historical import metadata; archived values are not combined with live balances."""

    model_config = _READ
    id: str
    fetched_at: float
    imported_at: float
    balance_count: int


class BudgetBalances(BaseModel):
    model_config = _READ
    account_id: str
    balances: list[BudgetBalance]
    balances_total: int
    balances_truncated: bool
    freshness: BudgetComponentStatus
    archived_imports: list[BudgetArchivedBalances]
    archived_has_more: bool


class BudgetLimitFields(BaseModel):
    model_config = _READ
    connection: str
    suspended: bool
    retry_at: float | None
    next_attempt: float
    backoff_until: float
    lease_until: float | None
    last_attempt: float | None
    last_success: float | None
    last_request_at: float | None
    next_request_at: float


class BudgetSharedLimit(BudgetLimitFields):
    scope: Literal["connection", "application", "provider"]


class BudgetComponentLimit(BudgetLimitFields):
    scope: Literal["component"]
    account: str
    operation: Literal["transactions", "balances"]


BudgetLimit = Annotated[BudgetSharedLimit | BudgetComponentLimit, Field(discriminator="scope")]


class BudgetLimits(BaseModel):
    model_config = _READ
    limits: list[BudgetLimit]


class BudgetAccountChanged(BudgetAccount):
    changed: bool


class BudgetConnectionChanged(BudgetConnection):
    changed: bool


class BudgetAccountSaved(BaseModel):
    model_config = _READ
    ok: Literal[True]
    account: BudgetAccountChanged


class BudgetConnectionSaved(BaseModel):
    model_config = _READ
    ok: Literal[True]
    connection: BudgetConnectionChanged


BudgetJobStatus = Literal["queued", "running", "updated", "partial", "failed", "deferred", "interrupted"]


class BudgetJob(BaseModel):
    """A local intent; debits, cadence and Retry-After stay authoritative, never the job."""

    model_config = _READ
    id: str
    connection_id: str
    account_id: str | None
    operation: BudgetOperation | None
    source: Literal["manual", "poller"]
    status: BudgetJobStatus
    error: str | None
    next_attempt: float | None
    created_at: float
    started_at: float | None
    finished_at: float | None


class BudgetJobChanged(BudgetJob):
    created: bool


class BudgetRefreshAccepted(BaseModel):
    model_config = _READ
    ok: Literal[True]
    job: BudgetJobChanged


class BudgetJobs(BaseModel):
    model_config = _READ
    jobs: list[BudgetJob]


class BudgetError(BaseModel):
    model_config = _READ
    ok: Literal[False]
    error: str


class BudgetConflict(BudgetError):
    error: Literal["budget_version_conflict"]
    current: BudgetAccount | BudgetConnection


# Owner-private analytics requests are strict; read values remain forward compatible.
BudgetAnalyticsVersion = Annotated[int, Field(ge=0)]
BudgetAnalyticsFingerprint = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
BudgetAnalyticsReference = Annotated[str, Field(min_length=1, max_length=1024)]
BudgetAnalyticsKind = Literal["auto", "expense", "income", "refund", "excluded"]


class BudgetAnalyticsCategoryCreateRequest(BaseModel):
    model_config = _STRICT
    name: Annotated[str, Field(min_length=1, max_length=80)]


class BudgetAnalyticsCategoryUpdateRequest(BudgetAnalyticsCategoryCreateRequest):
    category_id: BudgetId
    archived: bool
    expected_version: BudgetAnalyticsVersion


class BudgetAnalyticsAnnotationRequest(BaseModel):
    model_config = _STRICT
    account_id: BudgetId
    entry_reference: BudgetAnalyticsReference
    mode: Literal["auto", "manual"]
    category_id: BudgetId | None
    kind: BudgetAnalyticsKind
    expected_version: BudgetAnalyticsVersion
    expected_fingerprint: BudgetAnalyticsFingerprint


class BudgetAnalyticsRulePreviewRequest(BaseModel):
    model_config = _STRICT
    rule_id: BudgetId | None
    expected_version: BudgetAnalyticsVersion
    field: Literal["description", "counterparty_name", "mcc"]
    operator: Literal["contains", "equals"]
    value: Annotated[str, Field(min_length=1, max_length=256)]
    category_id: BudgetId | None
    kind: BudgetAnalyticsKind
    priority: Annotated[int, Field(ge=0, le=10000)]
    enabled: bool


class BudgetAnalyticsRuleSaveRequest(BaseModel):
    model_config = _STRICT
    preview_id: BudgetId


class BudgetAnalyticsLegRequest(BaseModel):
    model_config = _STRICT
    account_id: BudgetId
    entry_reference: BudgetAnalyticsReference


class BudgetAnalyticsTransferDecisionRequest(BaseModel):
    model_config = _STRICT
    decision_id: BudgetId | None = None
    debit: BudgetAnalyticsLegRequest | None = None
    credit: BudgetAnalyticsLegRequest | None = None
    action: Literal["accept", "reject", "undo"]
    expected_version: BudgetAnalyticsVersion
    source: Literal["account_identity", "heuristic", "manual"] = "manual"
    expected_debit_fingerprint: BudgetAnalyticsFingerprint | None = None
    expected_credit_fingerprint: BudgetAnalyticsFingerprint | None = None


class BudgetAnalyticsCategory(BaseModel):
    model_config = _READ
    id: str
    name: str
    archived: bool
    version: int
    created_at: float
    updated_at: float


class BudgetAnalyticsCategorySaved(BudgetAnalyticsCategory):
    changed: bool


class BudgetAnalyticsCategories(BaseModel):
    model_config = _READ
    categories: list[BudgetAnalyticsCategory]


class BudgetAnalyticsCategoryResult(BaseModel):
    model_config = _READ
    ok: Literal[True]
    category: BudgetAnalyticsCategorySaved


class BudgetAnalyticsAnnotationSaved(BaseModel):
    model_config = _READ
    account_id: str
    entry_reference: str
    mode: str
    category_id: str | None
    kind: str
    ledger_fingerprint: str
    version: int
    updated_at: float | None
    changed: bool


class BudgetAnalyticsAnnotationResult(BaseModel):
    model_config = _READ
    ok: Literal[True]
    annotation: BudgetAnalyticsAnnotationSaved


class BudgetAnalyticsRule(BaseModel):
    model_config = _READ
    id: str
    field: str
    operator: str
    value: str
    category_id: str | None
    kind: str
    priority: int
    enabled: bool
    version: int
    created_at: float
    updated_at: float


class BudgetAnalyticsRuleSaved(BudgetAnalyticsRule):
    changed: bool


class BudgetAnalyticsRules(BaseModel):
    model_config = _READ
    rules: list[BudgetAnalyticsRule]


class BudgetAnalyticsRuleResult(BaseModel):
    model_config = _READ
    ok: Literal[True]
    rule: BudgetAnalyticsRuleSaved


class BudgetAnalyticsPreviewExample(BaseModel):
    model_config = _READ
    account_id: str
    entry_reference: str


class BudgetAnalyticsPreview(BaseModel):
    model_config = _READ
    preview_id: str
    expires_at: float
    affected_count: int
    examples: list[BudgetAnalyticsPreviewExample]


class BudgetAnalyticsPreviewResult(BaseModel):
    model_config = _READ
    ok: Literal[True]
    preview: BudgetAnalyticsPreview


class BudgetAnalyticsRuleBatchPreviewRequest(BaseModel):
    """Preview up to 100 rules against one snapshot. Invalid candidates store nothing."""
    model_config = _STRICT
    rules: Annotated[list[BudgetAnalyticsRulePreviewRequest], Field(min_length=1, max_length=100)]


class BudgetAnalyticsRuleBatchSaveRequest(BaseModel):
    """Save the whole batch preview or none of it. A single-rule preview id is rejected."""
    model_config = _STRICT
    preview_id: BudgetId


class BudgetAnalyticsBatchPreview(BaseModel):
    model_config = _READ
    preview_id: str
    expires_at: float
    affected_count: int
    examples: list[BudgetAnalyticsPreviewExample]
    rule_count: int


class BudgetAnalyticsBatchPreviewResult(BaseModel):
    model_config = _READ
    ok: Literal[True]
    preview: BudgetAnalyticsBatchPreview


class BudgetAnalyticsRuleBatch(BaseModel):
    model_config = _READ
    rules: list[BudgetAnalyticsRuleSaved]
    changed: bool


class BudgetAnalyticsRuleBatchResult(BaseModel):
    model_config = _READ
    ok: Literal[True]
    batch: BudgetAnalyticsRuleBatch


class BudgetAnalyticsAnnotation(BaseModel):
    model_config = _READ
    version: int
    mode: str
    category_id: str | None
    kind: str
    stale: bool


class BudgetAnalyticsEffective(BaseModel):
    model_config = _READ
    kind: str
    category_id: str | None
    source: str
    rule_id: str | None


class BudgetAnalyticsMetadata(BaseModel):
    model_config = _READ
    creditor_name: str | None
    debtor_name: str | None
    mcc: str | None


class BudgetAnalyticsTransactionTransfer(BaseModel):
    model_config = _READ
    status: str
    decision_id: str | None
    candidate_count: int
    ambiguous: bool


class BudgetAnalyticsTransaction(BudgetTransaction):
    account_id: str
    fingerprint: str
    transfer_fingerprint: str
    annotation: BudgetAnalyticsAnnotation
    effective: BudgetAnalyticsEffective
    metadata: BudgetAnalyticsMetadata | None
    transfer: BudgetAnalyticsTransactionTransfer


class BudgetAnalyticsPeriod(BaseModel):
    model_config = _READ
    period: str
    start: str
    end: str
    snapshot_digest: str


class BudgetAnalyticsTransactions(BudgetAnalyticsPeriod):
    transactions: list[BudgetAnalyticsTransaction]
    filtered_total: int
    next_cursor: str | None


class BudgetAnalyticsCurrency(BaseModel):
    """Exact decimal strings in one currency; net_spending is expenses minus refunds."""
    model_config = _READ
    currency: str
    gross_debits: str
    gross_credits: str
    expenses: str
    income: str
    refunds: str
    unclassified_credit: str
    excluded_debits: str
    excluded_credits: str
    transfer_debits: str
    transfer_credits: str
    net_spending: str


class BudgetAnalyticsMonth(BaseModel):
    model_config = _READ
    month: str
    currencies: list[BudgetAnalyticsCurrency]


class BudgetAnalyticsCategoryTotal(BaseModel):
    model_config = _READ
    category_id: str | None
    name: str | None
    archived: bool
    currency: str
    expenses: str
    refunds: str
    net_spending: str


class BudgetAnalyticsFreshness(BaseModel):
    model_config = _READ
    account_id: str
    transactions: BudgetComponentStatus
    balances: BudgetComponentStatus


class BudgetAnalyticsScope(BaseModel):
    model_config = _READ
    selected_accounts: int
    excluded_accounts: int
    uncertain_count: int
    unresolved_count: int
    import_unresolved_count: int
    freshness: list[BudgetAnalyticsFreshness]


class BudgetAnalyticsTransferSummary(BaseModel):
    model_config = _READ
    suggested: int
    accepted: int
    review: int
    rejected: int
    boundary: int
    scan_limited: bool


class BudgetAnalyticsOverview(BudgetAnalyticsPeriod):
    scope: BudgetAnalyticsScope
    currencies: list[BudgetAnalyticsCurrency]
    months: list[BudgetAnalyticsMonth]
    categories: list[BudgetAnalyticsCategoryTotal]
    transfer_summary: BudgetAnalyticsTransferSummary


class BudgetAnalyticsLeg(BaseModel):
    model_config = _READ
    account_id: str
    entry_reference: str
    fingerprint: str | None
    amount: str | None
    currency: str | None
    date: str | None
    selected: bool


class BudgetAnalyticsTransfer(BaseModel):
    model_config = _READ
    id: str | None
    version: int
    status: str
    source: str
    debit: BudgetAnalyticsLeg
    credit: BudgetAnalyticsLeg
    date_distance: int | None
    ambiguous: bool
    stale: bool
    boundary: bool


class BudgetAnalyticsTransfers(BudgetAnalyticsPeriod):
    transfers: list[BudgetAnalyticsTransfer]
    filtered_total: int
    next_cursor: str | None
    scan_limited: bool


class BudgetAnalyticsDecisionSaved(BaseModel):
    model_config = _READ
    id: str
    debit_account_id: str
    debit_ref: str
    credit_account_id: str
    credit_ref: str
    source: str
    status: str
    debit_fingerprint: str
    credit_fingerprint: str
    rule_version: int
    version: int
    created_at: float
    updated_at: float
    changed: bool


class BudgetAnalyticsDecisionResult(BaseModel):
    model_config = _READ
    ok: Literal[True]
    decision: BudgetAnalyticsDecisionSaved


class BudgetAnalyticsRecurringOccurrence(BaseModel):
    model_config = _READ
    account_id: str
    entry_reference: str
    date: str


class BudgetAnalyticsRecurringHint(BaseModel):
    model_config = _READ
    label: str
    label_source: Literal["counterparty_name", "description"]
    amount: str
    currency: str
    cadence: Literal["monthly"]
    occurrence_count: int
    distinct_months: int
    first_date: str
    last_date: str
    min_gap_days: int
    max_gap_days: int
    account_ids: list[str]
    evidence: list[BudgetAnalyticsRecurringOccurrence]
    evidence_truncated: bool


class BudgetAnalyticsRecurring(BaseModel):
    model_config = _READ
    snapshot_digest: str
    rule_version: int
    total_hints: int
    truncated: bool
    hints: list[BudgetAnalyticsRecurringHint]


class BudgetAnalyticsSpendingLimitSaveRequest(BaseModel):
    model_config = _STRICT
    category_id: Annotated[str, Field(min_length=1, max_length=128)]
    currency: Annotated[str, Field(pattern="^[A-Z]{3}$")]
    monthly_limit: Annotated[str, Field(min_length=1, max_length=80)]
    warning_percent: Annotated[int, Field(ge=1, le=99)]
    enabled: bool
    expected_version: Annotated[int, Field(ge=0)]


class BudgetAnalyticsSpendingLimit(BaseModel):
    model_config = _READ
    id: str
    category_id: str
    currency: str
    monthly_limit: str
    warning_percent: int
    enabled: bool
    version: int
    created_at: float
    updated_at: float


class BudgetAnalyticsSpendingLimitSaved(BudgetAnalyticsSpendingLimit):
    changed: bool


class BudgetAnalyticsSpendingLimitResult(BaseModel):
    model_config = _READ
    ok: Literal[True]
    limit: BudgetAnalyticsSpendingLimitSaved


class BudgetAnalyticsSpendingLimitStatus(BaseModel):
    model_config = _READ
    period: str
    category_id: str
    name: str | None
    archived: bool
    currency: str
    monthly_limit: str
    expenses: str
    refunds: str
    net_spending: str
    remaining: str
    over_by: str
    percent_used_floor: int
    warning_percent: int
    status: Literal["within", "approaching", "at_limit", "exceeded"]


class BudgetAnalyticsSpendingLimits(BudgetAnalyticsPeriod):
    """Monthly category spending limits and evaluated statuses; ``snapshot_digest`` versions
    the ledger/classification projection while ``limits_version`` versions the owner's saved limits."""
    limits_version: str
    rule_version: int
    limits: list[BudgetAnalyticsSpendingLimit]
    statuses: list[BudgetAnalyticsSpendingLimitStatus]


_MORTGAGE_DESTINATION = Literal["owner_inbox", "owner_push", "owner_email"]


class BudgetAnalyticsMortgageSaveRequest(BaseModel):
    """Create (schedule_id=None, expected_version=0) or update one private mortgage schedule.
    Evaluation-only: stores destination_kind without PII targets and triggers no delivery or payments."""
    model_config = _STRICT
    schedule_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    label: Annotated[str, Field(min_length=1, max_length=256)]
    amount: Annotated[str, Field(min_length=1, max_length=80)]
    currency: Annotated[str, Field(pattern="^[A-Z]{3}$")]
    due_day: Annotated[int, Field(ge=1, le=31)]
    start_date: Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")]
    end_date: Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")] | None = None
    remind_days_before: Annotated[int, Field(ge=0, le=31)]
    destination_kind: _MORTGAGE_DESTINATION
    enabled: bool
    expected_version: Annotated[int, Field(ge=0)]


class BudgetAnalyticsMortgagePaymentSetRequest(BaseModel):
    """Mark or unmark one YYYY-MM installment paid on an owned mortgage schedule."""
    model_config = _STRICT
    schedule_id: Annotated[str, Field(min_length=1, max_length=128)]
    month: Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}$")]
    paid: bool
    expected_version: Annotated[int, Field(ge=0)]


class BudgetAnalyticsMortgageSchedule(BaseModel):
    model_config = _READ
    id: str
    label: str
    amount: str
    currency: str
    due_day: int
    start_date: str
    end_date: str | None
    remind_days_before: int
    destination_kind: _MORTGAGE_DESTINATION
    enabled: bool
    version: int
    paid_months: list[str]
    created_at: float
    updated_at: float


class BudgetAnalyticsMortgageScheduleSaved(BudgetAnalyticsMortgageSchedule):
    changed: bool


class BudgetAnalyticsMortgageScheduleResult(BaseModel):
    model_config = _READ
    ok: Literal[True]
    schedule: BudgetAnalyticsMortgageScheduleSaved


class BudgetAnalyticsMortgagePaymentSaved(BaseModel):
    model_config = _READ
    schedule_id: str
    month: str
    paid: bool
    version: int
    paid_months: list[str]
    changed: bool


class BudgetAnalyticsMortgagePaymentResult(BaseModel):
    model_config = _READ
    ok: Literal[True]
    payment: BudgetAnalyticsMortgagePaymentSaved


class BudgetAnalyticsMortgageReminder(BaseModel):
    model_config = _READ
    schedule_id: str
    label: str
    amount: str
    currency: str
    due_date: str | None
    days_until_due: int | None
    status: Literal["overdue", "due_today", "due_soon", "upcoming", "completed"]
    reminder_active: bool
    destination_kind: _MORTGAGE_DESTINATION
    freshness_stale: bool
    ledger_as_of: str | None
    rule_version: int


class BudgetAnalyticsMortgages(BaseModel):
    """Owner-private mortgage reminder schedules evaluated strictly against server UTC today.
    Evaluation-only: no notification delivery daemon, shared calendar sync or bank payment runs here."""
    model_config = _READ
    evaluation_date: str
    evaluation_timezone: Literal["UTC"]
    ledger_as_of: str | None
    schedules_version: str
    rule_version: int
    schedules: list[BudgetAnalyticsMortgageSchedule]
    reminders: list[BudgetAnalyticsMortgageReminder]


BudgetAnalyticsInboxKind = Literal["consent_renewal", "mortgage_due", "spending_limit"]
BudgetAnalyticsInboxMilestone = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9:_-]+$")]


class BudgetAnalyticsInboxKeyRequest(BaseModel):
    """One opaque owner-inbox alert key (kind, stable local subject_id, milestone)."""
    model_config = _STRICT
    kind: BudgetAnalyticsInboxKind
    subject_id: BudgetAnalyticsFingerprint
    milestone: BudgetAnalyticsInboxMilestone


class BudgetAnalyticsInboxMarkRequest(BaseModel):
    """Atomically mark or unmark 1..100 owner-inbox alert keys under anti-ABA reads_version CAS."""
    model_config = _STRICT
    items: Annotated[list[BudgetAnalyticsInboxKeyRequest], Field(min_length=1, max_length=100)]
    read: bool
    expected_reads_version: BudgetAnalyticsFingerprint


class BudgetAnalyticsInboxConsentParams(BaseModel):
    """Strictly allowlisted live params for a consent_renewal alert (zero bank/provider IDs)."""
    model_config = _READ
    status: Literal["attention_required", "expired", "due_soon"]
    valid_until: float | None
    days_remaining: int | None
    attention: str | None
    paused: bool


class BudgetAnalyticsInboxMortgageParams(BaseModel):
    """Strictly allowlisted live params for a mortgage_due alert (zero labels or amounts)."""
    model_config = _READ
    status: Literal["overdue", "due_today", "due_soon"]
    due_date: str
    days_until_due: int
    currency: str
    freshness_stale: bool


class BudgetAnalyticsInboxSpendingLimitParams(BaseModel):
    """Strictly allowlisted live params for a spending_limit alert (zero category names or amounts)."""
    model_config = _READ
    status: Literal["approaching", "at_limit", "exceeded"]
    period: str
    currency: str
    warning_percent: int
    percent_used_floor: int


class BudgetAnalyticsInboxConsentRef(BaseModel):
    """Owner-known local connection ID for UI lookup (computed live, never stored or logged)."""
    model_config = _READ
    connection_id: str


class BudgetAnalyticsInboxMortgageRef(BaseModel):
    """Owner-known local mortgage schedule ID for UI lookup (computed live, never stored or logged)."""
    model_config = _READ
    schedule_id: str


class BudgetAnalyticsInboxSpendingLimitRef(BaseModel):
    """Owner-known local category ID and currency for UI lookup (computed live, never stored or logged)."""
    model_config = _READ
    category_id: str
    currency: str


class BudgetAnalyticsInboxItemBase(BaseModel):
    model_config = _READ
    subject_id: str
    milestone: str
    read: bool
    read_at: float | None


class BudgetAnalyticsInboxConsentItem(BudgetAnalyticsInboxItemBase):
    kind: Literal["consent_renewal"]
    ref: BudgetAnalyticsInboxConsentRef
    params: BudgetAnalyticsInboxConsentParams


class BudgetAnalyticsInboxMortgageItem(BudgetAnalyticsInboxItemBase):
    kind: Literal["mortgage_due"]
    ref: BudgetAnalyticsInboxMortgageRef
    params: BudgetAnalyticsInboxMortgageParams


class BudgetAnalyticsInboxSpendingLimitItem(BudgetAnalyticsInboxItemBase):
    kind: Literal["spending_limit"]
    ref: BudgetAnalyticsInboxSpendingLimitRef
    params: BudgetAnalyticsInboxSpendingLimitParams


BudgetAnalyticsInboxItem = Annotated[
    BudgetAnalyticsInboxConsentItem | BudgetAnalyticsInboxMortgageItem | BudgetAnalyticsInboxSpendingLimitItem,
    Field(discriminator="kind"),
]


class BudgetAnalyticsInbox(BaseModel):
    """Owner-private live active alerts evaluated in one SQLite read snapshot (pure read-only, zero DB writes).
    Represents currently active alerts only: resolved alerts disappear automatically even if unread.
    Read markers are durable dedupe receipts (no pruning in v1, cap 500 per owner)."""
    model_config = _READ
    evaluation_date: str
    evaluation_timezone: Literal["UTC"]
    evaluated_at: float
    reads_revision: int
    reads_version: str
    reads_count: int
    reads_cap: int
    unread_count: int
    rule_version: int
    items: list[BudgetAnalyticsInboxItem]


class BudgetAnalyticsInboxMarkSaved(BaseModel):
    model_config = _READ
    read: bool
    changed: bool
    changed_count: int
    reads_revision: int
    reads_version: str
    reads_count: int
    reads_cap: int


class BudgetAnalyticsInboxMarkResult(BaseModel):
    model_config = _READ
    ok: Literal[True]
    inbox: BudgetAnalyticsInboxMarkSaved


BudgetAnalyticsQuarantineBucket = Literal["uncertain", "unresolved", "import_unresolved"]
BudgetAnalyticsQuarantineReason = Literal[
    "non_booked_status",
    "missing_reference",
    "missing_date",
    "reference_conflict",
    "non_canonical",
    "api_preserved",
    "stale_conflict",
    "same_time_conflict",
]


class BudgetAnalyticsQuarantineSummary(BaseModel):
    model_config = _READ
    uncertain_count: int
    unresolved_count: int
    import_unresolved_count: int
    total_count: int


class BudgetAnalyticsQuarantineAccount(BaseModel):
    model_config = _READ
    account_id: str
    selected: bool
    uncertain_count: int
    unresolved_count: int
    import_unresolved_count: int
    total_count: int


class BudgetAnalyticsQuarantineItem(BaseModel):
    """Safe non-prose quarantine entry (ref, date, status and exclusion reason only; no descriptions or amounts)."""
    model_config = _READ
    bucket: BudgetAnalyticsQuarantineBucket
    account_id: str
    selected: bool
    entry_reference: str | None
    date: str | None
    status: str | None
    reason: BudgetAnalyticsQuarantineReason
    source: str
    observed_at: float


class BudgetAnalyticsQuarantine(BaseModel):
    """Read-only owner-scoped quarantine counts and bounded safe entry metadata (zero mutations, zero bank I/O)."""
    model_config = _READ
    rule_version: int
    summary: BudgetAnalyticsQuarantineSummary
    accounts: list[BudgetAnalyticsQuarantineAccount]
    items_cap: int
    truncated: bool
    items: list[BudgetAnalyticsQuarantineItem]


class BudgetAnalyticsForecastSummary(BaseModel):
    model_config = _READ
    selected_accounts: int
    excluded_accounts: int
    stale_accounts: int
    overdue_mortgage_schedule_count: int
    lapsed_recurring_count: int
    deduped_recurring_count: int
    stale_recurring_count: int


class BudgetAnalyticsForecastCurrencyTotal(BaseModel):
    """Exact decimal commitment totals in one currency."""
    model_config = _READ
    currency: str
    total: str
    scheduled_total: str
    estimated_total: str


BudgetAnalyticsForecastLabel = Annotated[str, Field(max_length=256)]


class BudgetAnalyticsForecastDeduplication(BaseModel):
    model_config = _READ
    schedule_id: str
    schedule_label: BudgetAnalyticsForecastLabel
    recurring_label: BudgetAnalyticsForecastLabel
    amount: str
    currency: str
    matched_due_date: str
    last_observed_date: str
    day_distance: Annotated[int, Field(ge=0, le=5)]
    account_stale: bool


class BudgetAnalyticsForecastItem(BaseModel):
    model_config = _READ
    source: Literal["mortgage", "recurring"]
    schedule_id: str | None
    label: BudgetAnalyticsForecastLabel
    amount: str
    currency: str
    expected_date: str
    confidence: Literal["scheduled", "high", "medium"]
    past_due: bool
    account_stale: bool


class BudgetAnalyticsForecastMonth(BaseModel):
    model_config = _READ
    month: str
    currencies: list[BudgetAnalyticsForecastCurrencyTotal]
    items: list[BudgetAnalyticsForecastItem]


class BudgetAnalyticsForecast(BaseModel):
    """Read-only estimate of scheduled and recurring commitments by UTC calendar month."""
    model_config = _READ
    evaluation_date: str
    evaluation_timezone: Literal["UTC"]
    horizon_months: Annotated[int, Field(ge=1, le=12)]
    start_month: str
    end_month: str
    ledger_as_of: str | None
    freshness_stale: bool
    stale_account_ids: Annotated[list[str], Field(max_length=200)]
    stale_account_ids_truncated: bool
    snapshot_digest: str
    schedules_version: str
    rule_version: int
    summary: BudgetAnalyticsForecastSummary
    totals: list[BudgetAnalyticsForecastCurrencyTotal]
    deduplications: list[BudgetAnalyticsForecastDeduplication]
    months: Annotated[list[BudgetAnalyticsForecastMonth], Field(min_length=1, max_length=12)]


BudgetAnalyticsMonthlyBudgetMoney = Annotated[str, Field(max_length=192)]


class BudgetAnalyticsMonthlyBudgetIncomeWindow(BaseModel):
    model_config = _READ
    start: str
    end: str
    start_day: Annotated[int, Field(ge=1, le=28)]


class BudgetAnalyticsMonthlyBudgetConfiguration(BaseModel):
    model_config = _READ
    status: Literal["ready", "unconfigured"]
    source: Literal["bootstrap", "saved", "none"]
    income_window: Annotated[
        BudgetAnalyticsMonthlyBudgetIncomeWindow,
        Field(default=None, json_schema_extra=_OMIT),
    ] = None


class BudgetAnalyticsMonthlyBudgetCoverage(BaseModel):
    model_config = _READ
    ledger_as_of: float | None
    stale_selected_accounts: int
    purchase_date_fallback_count: int
    pending_transfer_rows: int
    pending_row_keys: Annotated[list[str], Field(default_factory=list, max_length=500)]
    transfer_scan_limited: bool
    provisional: bool
    has_activity: bool


class BudgetAnalyticsMonthlyBudgetSummary(BaseModel):
    model_config = _READ
    start_budget: BudgetAnalyticsMonthlyBudgetMoney
    spent: BudgetAnalyticsMonthlyBudgetMoney
    remaining: BudgetAnalyticsMonthlyBudgetMoney


class BudgetAnalyticsMonthlyBudgetExpectedIncomeItem(BaseModel):
    model_config = _READ
    category_id: str
    amount: BudgetAnalyticsMonthlyBudgetMoney
    expected_date: str | None
    source: Literal["previous_month"]


class BudgetAnalyticsMonthlyBudgetExpectedIncome(BaseModel):
    model_config = _READ
    status: Literal["estimated", "partial", "unavailable", "none", "historical"]
    total: BudgetAnalyticsMonthlyBudgetMoney | None
    items: list[BudgetAnalyticsMonthlyBudgetExpectedIncomeItem]


class BudgetAnalyticsMonthlyBudgetRatio(BaseModel):
    model_config = _READ
    numerator: BudgetAnalyticsMonthlyBudgetMoney
    denominator: Annotated[int, Field(ge=1)]


class BudgetAnalyticsMonthlyBudgetPace(BaseModel):
    model_config = _READ
    basis: Literal["received", "received_plus_expected"]
    budget: BudgetAnalyticsMonthlyBudgetMoney
    status: Literal["historical", "no_budget", "already_reached", "no_positive_pace", "runs_out", "within_budget"]
    daily_rate: BudgetAnalyticsMonthlyBudgetRatio | None
    projected_month_spend: BudgetAnalyticsMonthlyBudgetRatio | None
    exhaustion_day: Annotated[int, Field(ge=1, le=31)] | None


class BudgetAnalyticsMonthlyBudgetCategoryLimit(BaseModel):
    model_config = _READ
    monthly_limit: str
    remaining: BudgetAnalyticsMonthlyBudgetMoney
    over_by: BudgetAnalyticsMonthlyBudgetMoney
    percent_used_floor: BudgetAnalyticsMonthlyBudgetMoney
    warning_percent: Annotated[int, Field(ge=1, le=99)]
    status: Literal["within", "approaching", "at_limit", "exceeded"]


class BudgetAnalyticsMonthlyBudgetCategorySpend(BaseModel):
    model_config = _READ
    category_id: str | None
    name: str | None
    archived: bool
    expenses: BudgetAnalyticsMonthlyBudgetMoney
    refunds: BudgetAnalyticsMonthlyBudgetMoney
    net_spending: BudgetAnalyticsMonthlyBudgetMoney
    limit: BudgetAnalyticsMonthlyBudgetCategoryLimit | None


class BudgetAnalyticsMonthlyBudgetPassThrough(BaseModel):
    model_config = _READ
    rent_category_id: str
    mortgage_category_id: str
    rent_received: BudgetAnalyticsMonthlyBudgetMoney
    mortgage_debits: BudgetAnalyticsMonthlyBudgetMoney
    mortgage_refunds: BudgetAnalyticsMonthlyBudgetMoney
    unapplied_mortgage_refunds: BudgetAnalyticsMonthlyBudgetMoney
    matched: BudgetAnalyticsMonthlyBudgetMoney
    unapplied_rent: BudgetAnalyticsMonthlyBudgetMoney
    uncovered_mortgage: BudgetAnalyticsMonthlyBudgetMoney


class BudgetAnalyticsMonthlyBudgetCardExposure(BaseModel):
    model_config = _READ
    status: Literal["unavailable"]
    amount: str | None
    as_of_date: str | None


class BudgetAnalyticsMonthlyBudget(BaseModel):
    """Read-only owner-private monthly budget dashboard evaluated in Europe/Warsaw from one snapshot."""
    model_config = _READ
    period: str
    start: str
    end: str
    cutoff: str
    current_month: str
    evaluated_at: float
    evaluation_date: str
    evaluation_timezone: Literal["Europe/Warsaw"]
    currency: str
    available_currencies: list[str]
    configuration: BudgetAnalyticsMonthlyBudgetConfiguration
    coverage: BudgetAnalyticsMonthlyBudgetCoverage
    summary: BudgetAnalyticsMonthlyBudgetSummary | None
    expected_income: BudgetAnalyticsMonthlyBudgetExpectedIncome | None
    pace: BudgetAnalyticsMonthlyBudgetPace | None
    category_spend: list[BudgetAnalyticsMonthlyBudgetCategorySpend]
    pass_through: BudgetAnalyticsMonthlyBudgetPassThrough | None
    card_exposure: BudgetAnalyticsMonthlyBudgetCardExposure


class BudgetAnalyticsMonthlyBudgetDailyPoint(BaseModel):
    model_config = _READ
    date: str
    cumulative_spent: BudgetAnalyticsMonthlyBudgetMoney


class BudgetAnalyticsMonthlyBudgetSeriesCategory(BaseModel):
    model_config = _READ
    category_id: str | None
    net_spending: BudgetAnalyticsMonthlyBudgetMoney


class BudgetAnalyticsMonthlyBudgetSeriesMonth(BaseModel):
    model_config = _READ
    period: str
    has_activity: bool
    is_current_month: bool
    income_estimate_partial: bool
    provisional: bool
    start_budget: BudgetAnalyticsMonthlyBudgetMoney
    spent: BudgetAnalyticsMonthlyBudgetMoney
    categories: list[BudgetAnalyticsMonthlyBudgetSeriesCategory]


class BudgetAnalyticsMonthlyBudgetCategoryLabel(BaseModel):
    model_config = _READ
    category_id: str
    name: str
    archived: bool


class BudgetAnalyticsMonthlyBudgetSeries(BaseModel):
    model_config = _READ
    daily: Annotated[list[BudgetAnalyticsMonthlyBudgetDailyPoint], Field(min_length=1, max_length=31)]
    months: Annotated[list[BudgetAnalyticsMonthlyBudgetSeriesMonth], Field(min_length=1, max_length=12)]
    category_labels: list[BudgetAnalyticsMonthlyBudgetCategoryLabel]


class BudgetAnalyticsMonthlyBudgetCharts(BaseModel):
    """Read-only owner-private monthly budget dashboard and chart series evaluated from one snapshot."""
    model_config = _READ
    budget: BudgetAnalyticsMonthlyBudget
    series: BudgetAnalyticsMonthlyBudgetSeries | None


_ANALYTICS_MODELS = (BudgetAnalyticsCategoryCreateRequest, BudgetAnalyticsCategoryUpdateRequest,
                     BudgetAnalyticsAnnotationRequest, BudgetAnalyticsRulePreviewRequest,
                     BudgetAnalyticsRuleSaveRequest, BudgetAnalyticsTransferDecisionRequest,
                     BudgetAnalyticsSpendingLimitSaveRequest, BudgetAnalyticsMortgageSaveRequest,
                     BudgetAnalyticsMortgagePaymentSetRequest, BudgetAnalyticsInboxMarkRequest,
                     BudgetAnalyticsCategories, BudgetAnalyticsCategoryResult, BudgetAnalyticsAnnotationResult,
                     BudgetAnalyticsRules, BudgetAnalyticsRuleResult, BudgetAnalyticsPreviewResult,
                     BudgetAnalyticsRuleBatchPreviewRequest, BudgetAnalyticsRuleBatchSaveRequest,
                     BudgetAnalyticsBatchPreview, BudgetAnalyticsBatchPreviewResult,
                     BudgetAnalyticsRuleBatch, BudgetAnalyticsRuleBatchResult,
                     BudgetAnalyticsOverview, BudgetAnalyticsTransactions, BudgetAnalyticsTransfers,
                     BudgetAnalyticsDecisionResult, BudgetAnalyticsRecurring,
                     BudgetAnalyticsSpendingLimits, BudgetAnalyticsSpendingLimitResult,
                     BudgetAnalyticsMortgages, BudgetAnalyticsMortgageScheduleResult,
                     BudgetAnalyticsMortgagePaymentResult, BudgetAnalyticsInbox,
                     BudgetAnalyticsInboxMarkResult, BudgetAnalyticsQuarantine, BudgetAnalyticsForecast,
                     BudgetAnalyticsMonthlyBudget, BudgetAnalyticsMonthlyBudgetDailyPoint,
                     BudgetAnalyticsMonthlyBudgetSeriesCategory, BudgetAnalyticsMonthlyBudgetSeriesMonth,
                     BudgetAnalyticsMonthlyBudgetCategoryLabel, BudgetAnalyticsMonthlyBudgetSeries,
                     BudgetAnalyticsMonthlyBudgetCharts)


_BUDGET_MODELS = (BudgetSelectionRequest, BudgetKindRequest, BudgetConnectionStateRequest,
                  BudgetBackgroundSelectionRequest, BudgetRefreshRequest,
                  BudgetConsentStartRequest, BudgetConsentFlowRequest, BudgetConsentPending,
                  BudgetConsents, BudgetConsentRenewalReminders, BudgetConsentReconcile,
                  BudgetConsentStarted, BudgetConsentCompleted, BudgetConsentCanceled,
                  BudgetConnections, BudgetAccounts, BudgetStatus, BudgetTransactions,
                  BudgetBalances, BudgetLimits, BudgetJobs, BudgetAccountSaved, BudgetConnectionSaved,
                  BudgetRefreshAccepted, BudgetError, BudgetConflict)


def _ref(name: str) -> dict:
    return {"$ref": f"#/components/schemas/{name}"}


# The RBAC floor for each contracted operation, as `x-required-role` (mirrors web._ROUTE_MIN_ROLE;
# "public" = no auth). A test pins each value to web._required_role / web._is_public_route — so a
# mislabelled role fails CI rather than misleading a client.
_PATH_ROLES = {
    ("GET", "/api/budget/analytics/overview"): "admin",
    ("GET", "/api/budget/analytics/transactions"): "admin",
    ("GET", "/api/budget/analytics/categories"): "admin",
    ("GET", "/api/budget/analytics/rules"): "admin",
    ("GET", "/api/budget/analytics/transfers"): "admin",
    ("GET", "/api/budget/analytics/recurring"): "admin",
    ("GET", "/api/budget/analytics/spending-limits"): "admin",
    ("GET", "/api/budget/analytics/mortgages"): "admin",
    ("GET", "/api/budget/analytics/inbox"): "admin",
    ("GET", "/api/budget/analytics/quarantine"): "admin",
    ("GET", "/api/budget/analytics/forecast"): "admin",
    ("GET", "/api/budget/analytics/monthly-budget"): "admin",
    ("GET", "/api/budget/analytics/monthly-budget/charts"): "admin",
    ("POST", "/api/budget/analytics/category-create"): "admin",
    ("POST", "/api/budget/analytics/category-update"): "admin",
    ("POST", "/api/budget/analytics/annotation"): "admin",
    ("POST", "/api/budget/analytics/rule-preview"): "admin",
    ("POST", "/api/budget/analytics/rule-save"): "admin",
    ("POST", "/api/budget/analytics/rule-batch-preview"): "admin",
    ("POST", "/api/budget/analytics/rule-batch-save"): "admin",
    ("POST", "/api/budget/analytics/transfer-decision"): "admin",
    ("POST", "/api/budget/analytics/spending-limit-save"): "admin",
    ("POST", "/api/budget/analytics/mortgage-save"): "admin",
    ("POST", "/api/budget/analytics/mortgage-payment-set"): "admin",
    ("POST", "/api/budget/analytics/inbox-mark"): "admin",

    ("GET", "/api/budget/enable-banking/callback"): "public",
    ("GET", "/api/budget/consent/pending"): "admin",
    ("GET", "/api/budget/consent/status"): "admin",
    ("GET", "/api/budget/consent/renewal-reminders"): "admin",
    ("GET", "/api/budget/consent/reconcile"): "admin",
    ("POST", "/api/budget/consent/start"): "admin",
    ("POST", "/api/budget/consent/complete"): "admin",
    ("POST", "/api/budget/consent/cancel"): "admin",
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
    ("GET", _P_CALENDARS): "viewer",
    ("POST", _P_CALENDAR_EVENTS): "operator",
    ("POST", _P_CALENDAR_DELETE): "operator",
    ("GET", _P_BUDGET_CONNECTIONS): "admin",
    ("GET", _P_BUDGET_ACCOUNTS): "admin",
    ("GET", _P_BUDGET_STATUS): "admin",
    ("GET", _P_BUDGET_TRANSACTIONS): "admin",
    ("GET", _P_BUDGET_BALANCES): "admin",
    ("GET", _P_BUDGET_LIMITS): "admin",
    ("POST", _P_BUDGET_SELECTION): "admin",
    ("POST", _P_BUDGET_KIND): "admin",
    ("POST", _P_BUDGET_CONNECTION_STATE): "admin",
    ("POST", _P_BUDGET_BACKGROUND): "admin",
    ("POST", _P_BUDGET_REFRESH): "admin",
    ("GET", _P_BUDGET_JOBS): "admin",
}


def _component_schemas() -> dict:
    """The OpenAPI ``components.schemas`` map for every model, with cross-refs under that path."""
    _, combined = models_json_schema(
        [(model, "validation")
         for model in (*_CONTROL_MODELS, ControlSuccess, ControlError, *_COMMAND_MODELS,
                       *_READ_MODELS, *_AUTH_MODELS, *_AUTOMATION_MODELS, *_EVENT_MODELS,
                       *_USER_MODELS, *_CALENDAR_MODELS, *_BUDGET_MODELS, *_ANALYTICS_MODELS)],
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
            _P_CALENDARS: {
                "get": {
                    "operationId": "getCalendars",
                    "summary": "Selected local calendar events and synchronization health",
                    "description": "Requires viewer. Account credentials and remote calendar URLs are never returned.",
                    "parameters": [{"name": "group", "in": "query", "schema": {"type": "string"},
                                    "description": "Selected configured group; defaults to the first group."}],
                    "responses": {
                        "200": {"description": "calendar state", "content": {_APP_JSON: {"schema": _ref("Calendars")}}},
                        "400": {"description": "unknown group", "content": {_APP_JSON: {"schema": _ref("CalendarError")}}},
                        "503": {"description": "invalid configuration", "content": {_APP_JSON: {"schema": _ref("CalendarError")}}},
                    },
                },
            },
            _P_CALENDAR_EVENTS: {
                "post": {
                    "operationId": "saveCalendarEvent",
                    "summary": "Create or edit a local calendar event and enqueue synchronization",
                    "description": "Requires operator. Creates require a stable per-draft idempotency_key; retries return the same event's current state, including deletion. Reusing a key with different content returns 409. Existing events require id and current revision, and cannot supply a create key; stale edits return 409.",
                    "requestBody": {"required": True, "content": {_APP_JSON: {"schema": {
                        "oneOf": [_ref("CalendarCreateRequest"), _ref("CalendarUpdateRequest")]}}}},
                    "responses": {
                        "200": {"description": "saved", "content": {_APP_JSON: {"schema": _ref("CalendarSaved")}}},
                        "400": {"description": "invalid event", "content": {_APP_JSON: {"schema": _ref("CalendarError")}}},
                        "409": {"description": "revision or create-key content conflict", "content": {_APP_JSON: {"schema": _ref("CalendarError")}}},
                        "503": {"description": "disabled or invalid configuration", "content": {_APP_JSON: {"schema": _ref("CalendarError")}}},
                    },
                },
            },
            _P_CALENDAR_DELETE: {
                "post": {
                    "operationId": "deleteCalendarEvent",
                    "summary": "Permanently tombstone an event and enqueue deletion of remote copies",
                    "description": "Requires operator. The current revision is mandatory; stale deletes return 409.",
                    "requestBody": {"required": True, "content": {_APP_JSON: {"schema": _ref("CalendarDeleteRequest")}}},
                    "responses": {
                        "200": {"description": "tombstoned", "content": {_APP_JSON: {"schema": _ref("CalendarSaved")}}},
                        "400": {"description": "invalid request", "content": {_APP_JSON: {"schema": _ref("CalendarError")}}},
                        "409": {"description": "revision conflict", "content": {_APP_JSON: {"schema": _ref("CalendarError")}}},
                        "503": {"description": "disabled or invalid configuration", "content": {_APP_JSON: {"schema": _ref("CalendarError")}}},
                    },
                },
            },
        },
        "components": {"schemas": _component_schemas()},
    }
    doc["paths"].update(_budget_paths())
    # Tag every operation with its RBAC floor (machine-readable; a test cross-checks each against the
    # real web._required_role / public allowlist so the contract's roles can never drift from the server).
    for path, ops in doc["paths"].items():
        for method, op in ops.items():
            op["x-required-role"] = _PATH_ROLES[(method.upper(), path)]
    _make_responses_tolerant(doc)
    return doc


def _budget_responses(model):
    response = {"200": {"description": "owner-scoped stored state; no bank request",
                        "content": {_APP_JSON: {"schema": _ref(model)}}}}
    failures = {"400": "invalid request", "401": "session required; middleware may return an empty body",
                "403": "admin owner or browser-origin requirement failed; middleware may return an empty body",
                "404": "unknown, disabled or foreign resource", "409": "SQLite session users required",
                "500": "internal error", "503": "budget configuration unavailable"}
    for code, description in failures.items():
        response[code] = {"description": description, "content": {_APP_JSON: {"schema": _ref("BudgetError")}}}
    return response


def _budget_query(name, *, required=False, schema=None, description=""):
    return {"name": name, "in": "query", "required": required,
            "schema": schema or {"type": "string", "minLength": 1, "maxLength": 128},
            "description": description}


def _budget_paths():
    """Phase 2B.1 is a private stored-data surface, with no consent or refresh routes."""
    account = _budget_query("account_id", required=True, description="Owned opaque local account ID.")
    dates = [_budget_query(name, schema={"type": "string", "format": "date", "pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
                           description="Inclusive stored booking date, falling back to value date.")
             for name in ("date_from", "date_to")]
    transactions = [account, *dates,
                    _budget_query("limit", schema={"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                                  description="Maximum page size; totals cover the whole account."),
                    _budget_query("cursor", schema={"type": "string", "minLength": 1, "maxLength": 8192},
                                  description="Opaque encrypted keyset cursor bound to owner, account and filters.")]
    reads = (
        (_P_BUDGET_CONNECTIONS, "getBudgetConnections", "Stored connections and implemented capabilities", "BudgetConnections", []),
        (_P_BUDGET_ACCOUNTS, "getBudgetAccounts", "Stored accounts and versioned owner controls", "BudgetAccounts",
         [_budget_query("connection_id", description="Optional owned connection filter.")]),
        (_P_BUDGET_STATUS, "getBudgetStatus", "Stored component freshness and acquisition health", "BudgetStatus", []),
        (_P_BUDGET_TRANSACTIONS, "getBudgetTransactions", "Stored canonical booked transactions", "BudgetTransactions", transactions),
        (_P_BUDGET_BALANCES, "getBudgetBalances", "Live stored balances and separate archived import metadata", "BudgetBalances", [account]),
        (_P_BUDGET_LIMITS, "getBudgetLimits", "Sanitized stored limits without provider or application keys", "BudgetLimits", []),
        (_P_BUDGET_JOBS, "getBudgetJobs", "Recent owner refresh jobs, newest first, at most 20", "BudgetJobs",
         [_budget_query("connection_id", description="Optional owned connection filter.")]),
    )
    paths = {path: {"get": {"operationId": operation, "summary": summary,
                            "description": "Requires an authenticated SQLite admin owner. Reads never contact a bank.",
                            "parameters": parameters, "responses": _budget_responses(model)}}
             for path, operation, summary, model, parameters in reads}
    writes = (
        (_P_BUDGET_SELECTION, "setBudgetAccountSelection", "Select or deselect one owned account", "BudgetSelectionRequest", "BudgetAccountSaved"),
        (_P_BUDGET_KIND, "setBudgetAccountKind", "Explicitly confirm one owned account's kind", "BudgetKindRequest", "BudgetAccountSaved"),
        (_P_BUDGET_CONNECTION_STATE, "setBudgetConnectionState", "Pause or resume one owned connection locally", "BudgetConnectionStateRequest", "BudgetConnectionSaved"),
        (_P_BUDGET_BACKGROUND, "setBudgetBackgroundSelection", "Opt one owned connection in or out of unattended acquisition", "BudgetBackgroundSelectionRequest", "BudgetConnectionSaved"),
    )
    for path, operation, summary, request, model in writes:
        responses = _budget_responses(model)
        responses["200"]["description"] = "local control saved; changed=false is a no-op"
        responses["409"] = {"description": "missing version, stale conflicting change or SQLite session users required",
                            "content": {_APP_JSON: {"schema": {"anyOf": [_ref("BudgetError"), _ref("BudgetConflict")]}}}}
        paths[path] = {"post": {"operationId": operation, "summary": summary,
                                "description": "Requires the exact admin owner and a same-origin browser session; bearer-only mutations are rejected. expected_version is required. Missing versions or stale conflicting changes return 409 with current safe state after owner validation; an already-applied desired state returns 200 with changed=false even after a stale retry. No bank request is made.",
                                "requestBody": {"required": True, "content": {_APP_JSON: {"schema": _ref(request)}}},
                                "responses": responses}}
    paths[_P_BUDGET_REFRESH] = {"post": _refresh_operation()}
    paths.update(_consent_paths())
    paths.update(_analytics_paths())
    return paths


def _analytics_paths():
    prefix = "/api/budget/analytics/"
    period = _budget_query("period", required=True, schema={"type": "string", "pattern": r"^[0-9]{4}(?:-[0-9]{2})?$"},
                           description="Inclusive calendar month or year, 1900 through 9999.")
    currency = _budget_query("currency", schema={"type": "string", "pattern": "^[A-Z]{3}$"})
    page = [_budget_query("limit", schema={"type": "integer", "minimum": 1, "maximum": 200, "default": 50}),
            _budget_query("cursor", schema={"type": "string", "minLength": 1, "maxLength": 8192},
                          description="Opaque cursor bound to the current projection. A changed snapshot returns 409.")]
    month = _budget_query("period", required=True, schema={"type": "string", "pattern": r"^[0-9]{4}-[0-9]{2}$"},
                          description="Inclusive calendar month YYYY-MM, 1900 through 9999.")
    horizon = _budget_query("months", schema={"type": "string", "pattern": "^[0-9]{1,2}$", "default": "3"},
                            description="Integer 1..12, starting with the current server UTC calendar month.")
    month_optional = _budget_query(
        "period",
        schema={"type": "string", "pattern": r"^[0-9]{4}-[0-9]{2}$"},
        description="Optional calendar month YYYY-MM, 1900 through the current Europe/Warsaw month; defaults to current month.",
    )
    month_currency = _budget_query(
        "currency",
        schema={"type": "string", "pattern": "^[A-Z]{3}$", "default": "PLN"},
        description="Optional uppercase 3-letter ISO currency code; defaults to PLN.",
    )
    month_history = _budget_query(
        "history",
        schema={"type": "string", "enum": ["6", "12"], "default": "6"},
        description="Optional monthly history window (6 or 12 months); defaults to 6.",
    )
    reads = (
        ("overview", "getBudgetAnalyticsOverview", "BudgetAnalyticsOverview", [period, currency]),
        ("transactions", "getBudgetAnalyticsTransactions", "BudgetAnalyticsTransactions",
         [period, currency, _budget_query("category_id", description="Category ID, or uncategorised for no category."),
          _budget_query("kind"), _budget_query("query", schema={"type": "string", "minLength": 1, "maxLength": 256}), *page]),
        ("categories", "getBudgetAnalyticsCategories", "BudgetAnalyticsCategories", []),
        ("rules", "getBudgetAnalyticsRules", "BudgetAnalyticsRules", []),
        ("transfers", "getBudgetAnalyticsTransfers", "BudgetAnalyticsTransfers",
         [period, currency, _budget_query("status", schema={"type": "string", "enum":
             ["suggested", "accepted", "review", "boundary", "rejected", "undone"], "default": "suggested"}), *page]),
        ("recurring", "getBudgetAnalyticsRecurring", "BudgetAnalyticsRecurring", [currency]),
        ("spending-limits", "getBudgetAnalyticsSpendingLimits", "BudgetAnalyticsSpendingLimits", [month, currency]),
        ("mortgages", "getBudgetAnalyticsMortgages", "BudgetAnalyticsMortgages", []),
        ("inbox", "getBudgetAnalyticsInbox", "BudgetAnalyticsInbox", []),
        ("quarantine", "getBudgetAnalyticsQuarantine", "BudgetAnalyticsQuarantine",
         [_budget_query("account_id", description="Optional owned local account ID filter.")]),
        ("forecast", "getBudgetAnalyticsForecast", "BudgetAnalyticsForecast", [horizon, currency]),
        ("monthly-budget", "getBudgetAnalyticsMonthlyBudget", "BudgetAnalyticsMonthlyBudget",
         [month_optional, month_currency]),
        ("monthly-budget/charts", "getBudgetAnalyticsMonthlyBudgetCharts", "BudgetAnalyticsMonthlyBudgetCharts",
         [month_optional, month_currency, month_history]),
    )
    paths = {prefix + path: {"get": {"operationId": operation, "summary": "Read owner-private local budget analytics",
              "parameters": parameters, "responses": _analytics_responses(model)}}
             for path, operation, model, parameters in reads}
    read_overrides = {
        "inbox": ("Read owner-private live active alerts inbox (pure read-only)", (
            "Pure read-only single-snapshot evaluation of currently active consent renewal, mortgage due and "
            "spending-limit alerts against durable owner read markers (zero DB writes, zero bank requests). "
            "Resolved alerts disappear automatically even if unread; read markers are not pruned in v1 (cap 500)."
        )),
        "quarantine": ("Read owner-private quarantine counts and safe entry metadata (read-only)", (
            "Pure read-only single-snapshot summary of uncertain, unresolved and offline-import-unresolved observations "
            "excluded from report totals (zero mutations, zero bank requests). Returns exact whole-scope and per-account "
            "counts plus up to 200 safe details ordered newest effective date first (undated last), then observed_at "
            "descending (entry_reference, date, status and exclusion reason only; never amounts, descriptions or "
            "counterparty names). Whole-scope bounded integrity probes fail closed with 500 budget_analytics_data_error "
            "for invalid stored reasons, sources, timestamps or SQL-verifiable payload fields (duplicate JSON keys are "
            "validated on extracted candidate items)."
        )),
        "forecast": ("Read the owner-private upcoming commitments estimate", (
            "Read-only evaluation of unmarked mortgage instalments and active recurring charges from one local "
            "snapshot using the server UTC date. Matches recurring hints to individual scheduled instalments to "
            "suppress duplicate estimates. An optional currency filter scopes commitments, deduplications and "
            "their summary counts. Snapshot metadata and account freshness cover all selected accounts. "
            "The estimate excludes spending limits and makes no bank requests or stored changes."
        )),
        "monthly-budget": ("Read the owner-private monthly budget dashboard", (
            "Pure read-only single-snapshot monthly budget dashboard evaluated in Europe/Warsaw from one UTC instant. "
            "Computes booked configured start budget, adjusted category spending and limits, rent/mortgage pass-through, "
            "unresolved transfer and repayment candidate provisional flags, previous-month expected income estimates "
            "(including cross-account salary matching), and spending pace. Card exposure is reported as unavailable in P1. "
            "Makes no bank requests and no database writes."
        )),
        "monthly-budget/charts": ("Read the owner-private monthly budget dashboard and chart series", (
            "Pure read-only single-snapshot monthly budget dashboard and chart series evaluated in Europe/Warsaw "
            "from one UTC instant. Returns the MonthlyBudget dashboard alongside cumulative daily spending through "
            "cutoff and 6 or 12 historical monthly category/income buckets computed with each month's configured income "
            "window. Returns series=null when unconfigured. Makes no bank requests and no database writes."
        )),
    }
    for path, (summary, description) in read_overrides.items():
        paths[prefix + path]["get"].update(summary=summary, description=description)
    forecast_responses = paths[prefix + "forecast"]["get"]["responses"]
    forecast_descriptions = {
        "200": "Owner-private upcoming commitments estimate and freshness from one local snapshot.",
        "400": "Invalid forecast query: months must be an integer 1..12, currency must match ^[A-Z]{3}$, "
               "and unknown or duplicate parameters (including today) are rejected.",
        "409": "SQLite backend required: forecast requires HESTIA_PERSIST=sqlite.",
        "413": "Forecast limit exceeded (>100,000 selected rows, >100 schedules, >1,200 paid marks per schedule, "
               ">200 active currency-filtered hints, >3,600 projected items, or >4 MiB UTF-8 JSON payload); "
               "zero partial totals returned.",
        "500": "Stored mortgage schedule, ledger, or metadata integrity check failed (`budget_analytics_data_error`), "
               "including stored-data helper errors mapped to 500 after forecast query validation, or unexpected "
               "internal failure (`budget_internal_error`).",
        "503": "Analytics storage or budget configuration unavailable (`budget_analytics_unavailable` or "
               "`budget_unavailable`).",
    }
    for code, description in forecast_descriptions.items():
        forecast_responses[code]["description"] = description
    month_responses = paths[prefix + "monthly-budget"]["get"]["responses"]
    month_descriptions = {
        "200": "Owner-private monthly budget dashboard and ledger coverage from one local snapshot.",
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
    }
    for code, description in month_descriptions.items():
        month_responses[code]["description"] = description
    charts_responses = paths[prefix + "monthly-budget/charts"]["get"]["responses"]
    charts_descriptions = {
        "200": "Owner-private monthly budget dashboard and chart series from one local snapshot.",
        "400": "Invalid monthly budget charts query: period must match ^[0-9]{4}-[0-9]{2}$ through the current "
               "Europe/Warsaw month, currency must match ^[A-Z]{3}$, history must be 6 or 12, and unknown or "
               "duplicate parameters are rejected.",
        "409": "SQLite backend required: monthly-budget/charts requires HESTIA_PERSIST=sqlite.",
        "413": "Monthly budget charts limit exceeded (>100,000 selected rows, >1,000 category limits, "
               ">192-character aggregate decimal output, or >4 MiB UTF-8 JSON payload); no partial totals returned.",
        "500": month_descriptions["500"],
        "503": month_descriptions["503"],
    }
    for code, description in charts_descriptions.items():
        charts_responses[code]["description"] = description
    writes = (
        ("category-create", "createBudgetAnalyticsCategory", "BudgetAnalyticsCategoryCreateRequest", "BudgetAnalyticsCategoryResult"),
        ("category-update", "updateBudgetAnalyticsCategory", "BudgetAnalyticsCategoryUpdateRequest", "BudgetAnalyticsCategoryResult"),
        ("annotation", "annotateBudgetAnalyticsTransaction", "BudgetAnalyticsAnnotationRequest", "BudgetAnalyticsAnnotationResult"),
        ("rule-preview", "previewBudgetAnalyticsRule", "BudgetAnalyticsRulePreviewRequest", "BudgetAnalyticsPreviewResult"),
        ("rule-save", "saveBudgetAnalyticsRule", "BudgetAnalyticsRuleSaveRequest", "BudgetAnalyticsRuleResult"),
        ("rule-batch-preview", "previewBudgetAnalyticsRules", "BudgetAnalyticsRuleBatchPreviewRequest", "BudgetAnalyticsBatchPreviewResult"),
        ("rule-batch-save", "saveBudgetAnalyticsRules", "BudgetAnalyticsRuleBatchSaveRequest", "BudgetAnalyticsRuleBatchResult"),
        ("transfer-decision", "decideBudgetAnalyticsTransfer", "BudgetAnalyticsTransferDecisionRequest", "BudgetAnalyticsDecisionResult"),
        ("spending-limit-save", "saveBudgetAnalyticsSpendingLimit", "BudgetAnalyticsSpendingLimitSaveRequest", "BudgetAnalyticsSpendingLimitResult"),
        ("mortgage-save", "saveBudgetAnalyticsMortgage", "BudgetAnalyticsMortgageSaveRequest", "BudgetAnalyticsMortgageScheduleResult"),
        ("mortgage-payment-set", "setBudgetAnalyticsMortgagePayment", "BudgetAnalyticsMortgagePaymentSetRequest", "BudgetAnalyticsMortgagePaymentResult"),
        ("inbox-mark", "markBudgetAnalyticsInbox", "BudgetAnalyticsInboxMarkRequest", "BudgetAnalyticsInboxMarkResult"),
    )
    for path, operation, request, model in writes:
        paths[prefix + path] = {"post": {"operationId": operation, "summary": "Save an owner-private local classification",
            "description": "Requires the exact admin owner, browser cookie and configured HTTPS Origin. "
                           "Version and source fingerprints prevent stale changes. No bank request is made.",
            "requestBody": {"required": True, "content": {_APP_JSON: {"schema": _ref(request)}}},
            "responses": _analytics_responses(model)}}
    batch_overrides = {
        "rule-batch-preview": (
            "Preview up to 100 owner-private rules against one snapshot",
            "Requires the exact admin owner, browser cookie and configured HTTPS Origin. All-or-nothing: "
            "every candidate is validated before a preview row is stored, and one snapshot digest fences the "
            "whole batch (1..100 rules). affected_count is the combined change, not a sum of single-rule previews. "
            "No bank request is made.",
        ),
        "rule-batch-save": (
            "Save a batch rule preview atomically",
            "Requires the exact admin owner, browser cookie and configured HTTPS Origin. Applies every rule in the "
            "batch preview or none: one digest check, per-rule expected_version checks, and the per-owner rule cap (MAX_RULES = 5000) are "
            "evaluated before any rule row changes. Replaying a saved batch returns changed=false. A single-rule "
            "preview id is rejected. No bank request is made.",
        ),
    }
    for path, (summary, description) in batch_overrides.items():
        paths[prefix + path]["post"].update(summary=summary, description=description)
    paths[prefix + "inbox-mark"]["post"]["summary"] = "Mark or unmark owner-private inbox alerts (atomic anti-ABA CAS)"
    paths[prefix + "inbox-mark"]["post"]["description"] = (
        "Requires the exact admin owner, browser cookie and configured HTTPS Origin. Atomically marks or unmarks "
        "1..100 deduplicated alert keys under anti-ABA reads_version CAS (409 budget_analytics_conflict if stale; "
        "409 budget_analytics_source_changed if any unmarked key is inactive on read=true; 413 budget_analytics_limit "
        "at the 500-marker cap on create only). No bank request is made."
    )
    return paths


def _analytics_responses(model):
    responses = _budget_responses(model)
    responses["409"]["description"] = "Version, financial source, reserved transfer leg, preview or cursor changed; reload and review."
    responses["413"] = {"description": "Explicit analytics processing or owner configuration limit; no partial totals.",
                        "content": {_APP_JSON: {"schema": _ref("BudgetError")}}}
    return responses


def _consent_paths():
    paths = {}
    prefix = "/api/budget/consent/"
    operations = (
        ("pending", "get", "getBudgetConsentPending", None, "BudgetConsentPending"),
        ("status", "get", "getBudgetConsents", None, "BudgetConsents"),
        ("renewal-reminders", "get", "getBudgetConsentRenewalReminders", None, "BudgetConsentRenewalReminders"),
        ("reconcile", "get", "getBudgetConsentReconcile", None, "BudgetConsentReconcile"),
        ("start", "post", "startBudgetConsent", "BudgetConsentStartRequest", "BudgetConsentStarted"),
        ("complete", "post", "completeBudgetConsent", "BudgetConsentFlowRequest", "BudgetConsentCompleted"),
        ("cancel", "post", "cancelBudgetConsent", "BudgetConsentFlowRequest", "BudgetConsentCanceled"),
    )
    for name, method, operation, request, response in operations:
        responses = _budget_responses(response)
        if name == "complete":
            responses["202"] = {"description": "Known candidate awaits verification/adoption, or exchange is uncertain. "
                "Repeating complete never repeats POST /sessions. No acquisition quota is reset.",
                "content": {_APP_JSON: {"schema": _ref(response)}}}
        if name in ("renewal-reminders", "reconcile"):
            responses["413"] = {"description": "Owner consent evaluation cap exceeded; no partial results returned.",
                                "content": {_APP_JSON: {"schema": _ref("BudgetError")}}}
        responses["429"] = {"description": "Durable consent throttle or provider cooldown; no automatic new consent.",
                             "content": {_APP_JSON: {"schema": _ref("BudgetError")}}}
        entry = {"operationId": operation, "summary": "Browser-bound bank consent: " + name,
                 "description": "Default-off. Requires the SQLite admin owner and signed browser cookie; mutations "
                     "also require exact Origin and JSON. Each flow has a Secure HttpOnly SameSite=Lax binding cookie. "
                     "Owner reads and cancel never contact a bank. Start uses GET /aspsps and POST /auth; complete "
                     "claims one POST /sessions before delivery, verifies GET /sessions and adopts under version fences. "
                     "Configured accounts, selections, history, pauses and acquisition debits are preserved.",
                 "responses": responses}
        if request:
            entry["requestBody"] = {"required": True, "content": {_APP_JSON: {"schema": _ref(request)}}}
        paths[prefix + name] = {method: entry}
    paths["/api/budget/enable-banking/callback"] = {"get": {
        "operationId": "stageBudgetConsentCallback", "summary": "Stage a browser-bound one-time bank code",
        "description": "Public exact GET only; state plus the matching per-flow HttpOnly cookie are mandatory. "
            "No provider request, adoption or account selection occurs. The code is kept in a private short-lived file. "
            "The fixed clean completion page restores the Strict Hestia session for an owner-only POST.",
        "parameters": [_budget_query("state", required=True), _budget_query("code"), _budget_query("error"),
                       _budget_query("error_description")],
        "responses": {"303": {"description": "Success and failure both redirect to /budget/connect/complete "
            "without query or fragment. Failure bodies contain only a fixed safe error code; an invalid "
            "or foreign browser never consumes another browser's flow."}}}}
    return paths


def _refresh_operation():
    responses = _budget_responses("BudgetRefreshAccepted")
    accepted = responses.pop("200")
    accepted["description"] = ("job queued, coalesced with the active job for the same target, or the stored "
                               "job for a repeated idempotency key; no bank request is made by this call")
    responses["202"] = accepted
    responses["409"] = {"description": "budget_version_conflict, budget_idempotency_conflict, "
                                       "budget_refresh_in_progress, budget_connection_paused or SQLite users required",
                        "content": {_APP_JSON: {"schema": {"anyOf": [_ref("BudgetError"), _ref("BudgetConflict")]}}}}
    responses["503"]["description"] = "budget_refresh_unavailable (executor disabled or queue full) or configuration unavailable"
    key = {"name": "Idempotency-Key", "in": "header", "required": True,
           "schema": {"type": "string", "pattern": "^[A-Za-z0-9_-]{16,128}$"},
           "description": "Owner-scoped; repeating it with the same target returns the same job."}
    return {"operationId": "requestBudgetRefresh", "summary": "Queue one owner refresh under existing quotas",
            "description": "Requires the exact admin owner and a same-origin browser session; bearer-only requests are "
                           "rejected. One queued/running job per connection: the same canonical target coalesces, a "
                           "different one returns 409. Never bypasses persisted debits, cadence or Retry-After.",
            "parameters": [key],
            "requestBody": {"required": True, "content": {_APP_JSON: {"schema": _ref("BudgetRefreshRequest")}}},
            "responses": responses}


def _schema_refs(node) -> "set[str]":
    """Every ``#/components/schemas/<name>`` referenced anywhere under ``node``."""
    found = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.rsplit("/", 1)[1])
        for value in node.values():
            found |= _schema_refs(value)
    elif isinstance(node, list):
        for value in node:
            found |= _schema_refs(value)
    return found


def _ref_closure(schemas: dict, roots: "set[str]") -> "set[str]":
    """``roots`` plus every component schema transitively referenced from them."""
    seen, queue = set(), list(roots)
    while queue:
        name = queue.pop()
        if name in seen or name not in schemas:
            continue
        seen.add(name)
        queue.extend(_schema_refs(schemas[name]))
    return seen


def _drop_strictness(node) -> None:
    """Recursively remove ``additionalProperties: false`` (typed maps like string→string are kept)."""
    if isinstance(node, dict):
        if node.get("additionalProperties") is False:
            del node["additionalProperties"]
        for value in node.values():
            _drop_strictness(value)
    elif isinstance(node, list):
        for value in node:
            _drop_strictness(value)


def _make_responses_tolerant(doc: dict) -> None:
    """Make every RESPONSE schema a tolerant reader: strip ``additionalProperties: false`` from each
    component schema except the request-ONLY ones (request DTOs keep it — it documents the server's
    unknown-field 400). Why: a pinned native client (Vesta) code-generates STRICT decoders from
    ``additionalProperties: false``, so adding a field to a response would crash it — exactly the
    #122 incident. Tolerant response schemas make additive changes safe for every future client
    build. Schemas shared by requests and responses (the Rule components) go tolerant too — the
    reader side must win; the server still rejects unknown request fields at runtime. The Python
    models stay ``extra="forbid"`` so the binding/drift tests keep catching undescribed backend
    fields — strictness moves out of the ARTIFACT, not out of the gate."""
    schemas = doc["components"]["schemas"]
    request_roots, response_roots = set(), set()
    for ops in doc["paths"].values():
        for op in ops.values():
            request_roots |= _schema_refs(op.get("requestBody", {}))
            response_roots |= _schema_refs(op.get("responses", {}))
    request_only = _ref_closure(schemas, request_roots) - _ref_closure(schemas, response_roots)
    for name, schema in schemas.items():
        if name not in request_only:
            _drop_strictness(schema)
    for ops in doc["paths"].values():                  # inline (non-$ref) response schemas, if any
        for op in ops.values():
            _drop_strictness(op.get("responses", {}))


def write_openapi(path: Path = OPENAPI_PATH) -> None:
    """Write the OpenAPI document to ``path`` (sorted keys + trailing newline → a stable git diff)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(build_openapi(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":  # pragma: no cover
    write_openapi()
    print(f"wrote {OPENAPI_PATH}")
