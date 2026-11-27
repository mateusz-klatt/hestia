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

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, WrapValidator, model_validator
from pydantic.json_schema import models_json_schema

from .budget.gmail_store import PARSE_FAILED_REASONS
from .budget.i18n import LANGUAGES, validate_languages

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
#         zero migration).
# 0.31.0: additive forecast source category_baseline and confidence estimated
#         (HESTIA_BUDGET_FORECAST_CONFIG, zero migration).
# 0.32.0: additive coverage.pending_row_keys / pending_row_keys_truncated and POST
#         transactions-by-keys endpoint (zero migration).
# 0.33.0: a category-baseline conflict skips only that category and lists it on
#         summary.baseline_conflicts (optional, zero migration). Not strictly additive:
#         budget_forecast_config_conflict (0.31–0.32) is withdrawn; a conflict is HTTP 200.
# 0.34.0: additive category parent_id (migration 0020). List rows include parent_id. Create and update
#         accept an optional parent_id: omit it on update to leave the parent, send null to detach.
# 0.35.0: additive one-off label on a budget annotation (migration 0021, down_revision 0020).
#         Charts and category summaries can report that label as its own entry.
# 0.36.0: default-off owner-private paste receipts and informational bank links (migration 0022,
#         down_revision 0021).
# 0.37.0: additive household read API (GET /api/budget/household/*, migration 0023,
#         down_revision 0022). Owner routes stay admin. The one-off label is not on this API.
# 0.38.0: counterparty-account rules store keyed digests and masked display values (zero migration).
#         Optional inactive_reason on a rule when that key is missing or rotated (additive).
# 0.39.0: receipts device tokens and one-time pairing codes (migration 0025, down_revision 0024),
#         plus optional parse-text source_kind.
# 0.40.0: separate Gmail receipt grant, status, sync and revocation (migration 0024).
# Documentation-only follow-up to 0.40.0: clarify the #262 budget browser-session requirement and the
# receipts device-token exception. No schema or wire change.
# 0.41.0: persistent root-category hues and explicit even re-spreading (migration 0026, down_revision 0025).

# 0.42.0: queue Gmail syncs and expose manual-forward review metadata (migration 0027, down_revision 0026).
# 0.44.0: password-based receipt device login, pairing-code cancellation, and owner device alerts (migration 0028, down_revision 0027).
# 0.45.0: ordered per-user budget display languages, category and one-off label translations
#         (migration 0029, down_revision 0028), resolved display fields and admin bulk import.
# 0.46.0: additive 409 device_token_limit_reached response for new password-login device names at the active cap.
# 0.47.0: additive parse_failed_reasons on BudgetReceiptGmailStatus and retry_parse_failed on BudgetReceiptGmailSyncRequest (zero migration).
# 0.48.0: read-only bounded expansion of recurring calendar events into concrete instances.
# 0.49.0: per-card credit_limit ({amount, currency}) control on accounts + additive card_debt on monthly budget (legacy card_exposure kept at 0.47.0 shape for Vesta).
# 0.50.0: additive authorization capabilities, public feature detection, and per-operation catalog requirements.
# 0.51.0: optional/nullable lines and optional raw_shop_name/raw_nip on BudgetReceiptConfirmRequest to keep stored values unchanged.
# 0.52.0: gateway payment evidence — receipt gateway/status/fee/payee_host/evidence fields, payment_evidence on detail, and the evidence list filter.
# 0.54.0: origin-bound QR login, native device exchange/budget reads, delegated receipts, and grant editing.
# 0.55.0: additive match_strength ("nip" | "distinctive" | "generic" | "none") on BudgetReceiptCandidate.
# 0.56.0: Gmail PDF no-text and multiple-document parse diagnostics.
# 0.57.0: ID-first matching ("id" strength), receipt card_last4, and no_bank_data period signal.
# 0.58.0: additive owner-private
#         appointment_slot & appointment_attention items on BudgetAnalyticsInbox.
# 0.59.0: group-linked receipt summaries and owner-private receipt group detail.
# 0.60.0: additive receipt link summary on analytics transactions and linked_transaction summary on receipt lists.
# 0.61.0: additive OCR issuer display name on receipt list, search and detail.
# 0.63.0: owner dismissal of offline import quarantine items and batches.
CONTRACT_VERSION = "0.63.0"
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
_P_CALENDAR_INSTANCES = _P_CALENDARS + "/instances"
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
_P_BUDGET_CREDIT_LIMIT = _P_BUDGET + "account-credit-limit"
_P_BUDGET_CONNECTION_STATE = _P_BUDGET + "connection-state"
_P_BUDGET_BACKGROUND = _P_BUDGET + "background-selection"
_P_BUDGET_REFRESH = _P_BUDGET + "refresh"
_P_BUDGET_JOBS = _P_BUDGET + "jobs"
_ADMIN_ROLE_DESC = "Requires the admin role."
_NO_SUCH_USER = "no such user"
_LOGIN_RETRY_HEADERS = {
    "Retry-After": {"description": "Seconds until another password attempt is allowed.",
                    "schema": {"type": "integer", "minimum": 1}},
}

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


BudgetLanguage = Literal[*LANGUAGES]
_PreferredLanguageList = Annotated[list[BudgetLanguage], Field(max_length=45)]
_PreferredLanguages = Annotated[
    _PreferredLanguageList | None,
    AfterValidator(validate_languages),
    Field(default=None, json_schema_extra=lambda s: s.pop("default", None)),
]


class Settings(BaseModel):
    """GET /api/settings — persisted UI preferences and effective ordered budget display languages."""

    model_config = ConfigDict(extra="forbid")
    locale: str | None
    temp_scale: str | None
    theme: str | None
    preferred_languages: Annotated[list[BudgetLanguage] | None, Field(default=None, json_schema_extra=_OMIT)] = None


class SettingsUpdate(BaseModel):
    """POST /api/settings — a partial update; null restores the locale-derived language default."""

    model_config = ConfigDict(extra="forbid", strict=True)
    locale: Annotated[str | None, Field(default=None, max_length=35, json_schema_extra=_OMIT)] = None
    temp_scale: Annotated[
        Literal["C", "F", "K"] | None, Field(default=None, json_schema_extra=_OMIT)
    ] = None
    theme: Annotated[str | None, Field(default=None, json_schema_extra=_OMIT)] = None
    preferred_languages: _PreferredLanguages = None


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


class OwnBudgetCapabilities(BaseModel):
    model_config = _READ
    permissions: list[str]
    can_view: bool
    can_use_receipts: bool
    can_manage_devices: bool


class DelegatedReceiptCapabilities(BaseModel):
    model_config = _READ
    permissions: list[str]
    can_use: bool


class MeCapabilities(BaseModel):
    """Effective permissions for the authenticated actor; delegated responses never name an owner."""

    model_config = _READ
    user: str | None
    actor_tag: Annotated[str, Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")] | None
    installation_permissions: list[str]
    own_budget: OwnBudgetCapabilities | None
    delegated_receipts: DelegatedReceiptCapabilities | None
    household_available: bool


class AuthFeatures(BaseModel):
    model_config = _READ
    auth_contract: Annotated[int, Field(ge=2)]
    features: list[str]
    qr_origin: str | None


class AuthErrorDetail(BaseModel):
    model_config = _READ
    code: str


class AuthError(BaseModel):
    model_config = _READ
    error: AuthErrorDetail


class QrCodeRequest(BaseModel):
    model_config = _STRICT
    origin: str


class QrCodeResult(BaseModel):
    model_config = _READ
    qr_payload: str


class QrRedeemRequest(BaseModel):
    model_config = _STRICT
    origin: str
    code: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{19}$")]


class QrRedeemSuccess(BaseModel):
    model_config = _READ
    ok: Literal[True]
    user: str
    token: str


class AuthDeviceTokenRequest(BaseModel):
    model_config = _STRICT
    device_name: Annotated[str, Field(min_length=1, max_length=64)]
    prior_token: Annotated[str | None, Field(default=None, json_schema_extra=_OMIT)] = None


class AuthDeviceTokenResult(BaseModel):
    model_config = _READ
    token: str
    token_id: str
    scope: Literal["receipts", "device"]
    actor: str
    owner: str
    owner_key: Annotated[str, Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")]
    issued_permissions: list[str]
    expires_at: str


class OkResult(BaseModel):
    """A bare ``{ok: true}`` acknowledgement (e.g. /api/logout)."""

    model_config = ConfigDict(extra="forbid")
    ok: bool


_AUTH_MODELS = (
    LoginRequest, LoginSuccess, WhoAmI, OwnBudgetCapabilities, DelegatedReceiptCapabilities,
    MeCapabilities, AuthFeatures, AuthErrorDetail, AuthError, QrCodeRequest, QrCodeResult,
    QrRedeemRequest, QrRedeemSuccess, AuthDeviceTokenRequest, AuthDeviceTokenResult, OkResult,
)


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


class GrantSelection(BaseModel):
    model_config = _STRICT
    permission_id: str
    target_owner: str | None


class GrantCatalogEntry(BaseModel):
    model_config = _READ
    permission_id: str
    domain: str
    storage_scope: Literal["installation", "owner"]
    delegation: Literal["self_only", "delegable"] | None
    requires: list[str]
    device_eligible: bool
    label: str
    protected: bool


class UserGrantsResult(BaseModel):
    model_config = _READ
    phase: str
    username: str
    budget_owner: bool
    budget_owners: list[str]
    catalog: list[GrantCatalogEntry]
    direct: list[GrantSelection]
    effective: list[GrantSelection]


class UpdateUserGrantsRequest(BaseModel):
    model_config = _STRICT
    username: str
    grants: list[GrantSelection]


class UpdateUserGrantsResult(BaseModel):
    model_config = _READ
    ok: Literal[True]
    phase: str


_USER_MODELS = (AddUserRequest, SetRoleRequest, SetDisabledRequest, ResetPasswordRequest,
                ChangePasswordRequest, UserRow, UsersList, Rf433Device, Rf433Feed, DbStats,
                GrantSelection, GrantCatalogEntry, UserGrantsResult, UpdateUserGrantsRequest,
                UpdateUserGrantsResult)


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


class CalendarExpandedInstance(BaseModel):
    model_config = _READ
    instance_id: str = Field(description="Unique within this response and stable across calls. Series use event_id/original-start, where all-day dates use YYYY-MM-DD and timed slots use their canonical UTC instant; moved exceptions retain the original slot ID. Single events use event_id.")
    event_id: str
    revision: Annotated[int, Field(ge=1)]
    group: str
    summary: str
    location: str
    start: CalendarTime
    end: CalendarTime
    all_day: bool
    status: Literal["confirmed", "tentative"]
    recurrence_id: CalendarTime | None = Field(description="Original series slot; null for a non-recurring event.")


class CalendarInstances(BaseModel):
    model_config = _READ
    enabled: bool
    groups: list[CalendarGroup]
    instances: list[CalendarExpandedInstance] = Field(max_length=2000)
    truncated: bool = Field(description="True when matching instances were omitted at the 2000-instance cap or an event could not be fully expanded within compatibility and work limits.")


class CalendarSaved(BaseModel):
    model_config = _READ
    ok: Literal[True]
    event: CalendarEvent


class CalendarError(BaseModel):
    model_config = _READ
    error: str


_CALENDAR_MODELS = (CalendarCreateRequest, CalendarUpdateRequest, CalendarDeleteRequest,
                    Calendars, CalendarInstances, CalendarSaved, CalendarError)


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


def _check_credit_limit_amount(value: str) -> str:
    from .budget.store import parse_credit_limit_amount
    parse_credit_limit_amount(value)
    return value


BudgetCreditLimitAmount = Annotated[
    str,
    Field(min_length=1, max_length=80, pattern=r"^[0-9]+(?:\.[0-9]+)?$"),
    AfterValidator(_check_credit_limit_amount),
]
BudgetCreditLimitCurrency = Annotated[str, Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")]


class BudgetCreditLimitValue(BaseModel):
    """Positive decimal limit (> 0, <= 10,000,000) paired with its 3-letter ISO currency code."""

    model_config = _STRICT
    amount: BudgetCreditLimitAmount
    currency: BudgetCreditLimitCurrency


class BudgetCreditLimitRequest(BaseModel):
    """Set or clear an owner-configured credit limit and currency for card debt calculation."""

    model_config = _STRICT
    account_id: BudgetId
    credit_limit: BudgetCreditLimitValue | None
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


class BudgetAccountCreditLimit(BaseModel):
    model_config = _READ
    amount: str
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]


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
    credit_limit: Annotated[BudgetAccountCreditLimit | None, Field(default=None, json_schema_extra=_OMIT)] = None


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
BudgetCategoryHue = Annotated[int, Field(ge=0, le=359)]
BudgetAnalyticsFingerprint = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
BudgetAnalyticsReference = Annotated[str, Field(min_length=1, max_length=1024)]
_ROW_KEY_PATTERN = r"^[^/\x00-\x1f\x7f]{1,128}/[^\x00-\x1f\x7f]{1,1024}$"
BudgetAnalyticsRowKey = Annotated[str, Field(min_length=3, max_length=1153, pattern=_ROW_KEY_PATTERN)]
BudgetAnalyticsKind = Literal["auto", "expense", "income", "refund", "excluded"]


class BudgetAnalyticsTransactionsByKeysRequest(BaseModel):
    model_config = _STRICT
    row_keys: Annotated[list[BudgetAnalyticsRowKey], Field(min_length=1, max_length=500)]
    cursor: Annotated[str, Field(min_length=1, max_length=8192)] | None = None
    limit: Annotated[int, Field(ge=1, le=200)] = 50
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None
    category_id: BudgetId | None = None
    kind: Literal["expense", "income", "refund", "excluded", "unclassified_credit", "transfer"] | None = None
    query: Annotated[str, Field(min_length=1, max_length=256)] | None = None


class BudgetAnalyticsCategoryCreateRequest(BaseModel):
    model_config = _STRICT
    name: Annotated[str, Field(min_length=1, max_length=80)]
    parent_id: BudgetId | None = Field(
        default=None,
        description="Parent category owned by the same user. Null is a top-level category. "
                    "On update, omit this field to leave the parent unchanged and send null to detach.",
    )


class BudgetAnalyticsCategoryUpdateRequest(BudgetAnalyticsCategoryCreateRequest):
    category_id: BudgetId
    archived: bool
    expected_version: BudgetAnalyticsVersion
    # Omitted keeps the current hue. Null selects the largest-gap automatic hue; only roots accept this field.
    color_hue: Annotated[BudgetCategoryHue | None, Field(json_schema_extra=_OMIT)] = None


class BudgetAnalyticsAnnotationRequest(BaseModel):
    model_config = _STRICT
    account_id: BudgetId
    entry_reference: BudgetAnalyticsReference
    mode: Literal["auto", "manual"]
    category_id: BudgetId | None
    kind: BudgetAnalyticsKind
    expected_version: BudgetAnalyticsVersion
    expected_fingerprint: BudgetAnalyticsFingerprint
    # Omitted leaves the stored label unchanged. Null or blank clears it. The default stays out of the
    # schema so generated clients treat the field as optional.
    label: Annotated[str | None, Field(max_length=80, json_schema_extra=_OMIT)] = None


class BudgetCategoryTranslationDeleteRequest(BaseModel):
    model_config = _STRICT
    category_id: BudgetId
    lang: BudgetLanguage


class BudgetCategoryTranslationSetRequest(BudgetCategoryTranslationDeleteRequest):
    name: Annotated[str, Field(min_length=1, max_length=80)]


class BudgetLabelTranslationDeleteRequest(BaseModel):
    model_config = _STRICT
    account_id: BudgetId
    entry_reference: BudgetAnalyticsReference
    lang: BudgetLanguage


class BudgetLabelTranslationSetRequest(BudgetLabelTranslationDeleteRequest):
    label: Annotated[str, Field(min_length=1, max_length=80)]


class BudgetCategoryTranslationImportEntry(BaseModel):
    model_config = _STRICT
    kind: Literal["category"]
    category_id: BudgetId
    lang: BudgetLanguage
    name: Annotated[str, Field(min_length=1, max_length=80)]


class BudgetLabelTranslationImportEntry(BaseModel):
    model_config = _STRICT
    kind: Literal["label"]
    account_id: BudgetId
    entry_reference: BudgetAnalyticsReference
    lang: BudgetLanguage
    label: Annotated[str, Field(min_length=1, max_length=80)]


def _preserve_invalid_translation_entry(value, handler):
    try:
        return handler(value)
    except ValidationError:
        return value


BudgetTranslationImportEntry = Annotated[
    BudgetCategoryTranslationImportEntry | BudgetLabelTranslationImportEntry,
    Field(discriminator="kind"),
    WrapValidator(_preserve_invalid_translation_entry),
]


class BudgetTranslationImportRequest(BaseModel):
    model_config = _STRICT
    entries: Annotated[list[BudgetTranslationImportEntry], Field(min_length=1, max_length=500)]


class BudgetCategoryTranslationSaved(BaseModel):
    model_config = _READ
    category_id: str
    lang: BudgetLanguage
    name: str
    changed: bool


class BudgetCategoryTranslationDeleted(BaseModel):
    model_config = _READ
    category_id: str
    lang: BudgetLanguage
    changed: bool


class BudgetLabelTranslationSaved(BaseModel):
    model_config = _READ
    account_id: str
    entry_reference: str
    lang: BudgetLanguage
    label: str
    changed: bool


class BudgetLabelTranslationDeleted(BaseModel):
    model_config = _READ
    account_id: str
    entry_reference: str
    lang: BudgetLanguage
    changed: bool


class BudgetCategoryTranslationSetResult(BaseModel):
    model_config = _READ
    ok: Literal[True]
    translation: BudgetCategoryTranslationSaved


class BudgetCategoryTranslationDeleteResult(BaseModel):
    model_config = _READ
    ok: Literal[True]
    translation: BudgetCategoryTranslationDeleted


class BudgetLabelTranslationSetResult(BaseModel):
    model_config = _READ
    ok: Literal[True]
    translation: BudgetLabelTranslationSaved


class BudgetLabelTranslationDeleteResult(BaseModel):
    model_config = _READ
    ok: Literal[True]
    translation: BudgetLabelTranslationDeleted


class BudgetTranslationImportEntryResult(BaseModel):
    model_config = _READ
    index: Annotated[int, Field(ge=0, le=499)]
    ok: bool
    changed: Annotated[bool | None, Field(default=None, json_schema_extra=_OMIT)] = None
    error: Annotated[str | None, Field(default=None, json_schema_extra=_OMIT)] = None


class BudgetTranslationImportResult(BaseModel):
    model_config = _READ
    ok: Literal[True]
    results: list[BudgetTranslationImportEntryResult]


class BudgetAnalyticsRulePreviewRequest(BaseModel):
    model_config = _STRICT
    rule_id: BudgetId | None
    expected_version: BudgetAnalyticsVersion
    field: Literal["description", "counterparty_name", "counterparty_account", "mcc"]
    operator: Literal["contains", "equals"]
    value: Annotated[str, Field(max_length=256, json_schema_extra=_OMIT)] = ""
    counterparty_account_key: Annotated[str | None, Field(pattern=r"^[0-9a-f]{64}$", json_schema_extra=_OMIT)] = None
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
    display_name: Annotated[str | None, Field(default=None, json_schema_extra=_OMIT)] = None
    translations: Annotated[dict[BudgetLanguage, str] | None, Field(default=None, json_schema_extra=_OMIT)] = None
    archived: bool
    version: int
    created_at: float
    updated_at: float
    parent_id: str | None
    color_hue: Annotated[BudgetCategoryHue | None, Field(json_schema_extra=_OMIT)] = None


class BudgetAnalyticsCategorySaved(BudgetAnalyticsCategory):
    changed: bool


class BudgetAnalyticsCategories(BaseModel):
    model_config = _READ
    categories: list[BudgetAnalyticsCategory]


class BudgetAnalyticsCategoryResult(BaseModel):
    model_config = _READ
    ok: Literal[True]
    category: BudgetAnalyticsCategorySaved


class BudgetAnalyticsCategoryColorSpreadRequest(BaseModel):
    model_config = _STRICT


class BudgetAnalyticsCategoryColorSpreadResult(BaseModel):
    model_config = _READ
    ok: Literal[True]
    categories: list[BudgetAnalyticsCategory]


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
    label: Annotated[str | None, Field(json_schema_extra=_OMIT)] = None
    display_label: Annotated[str | None, Field(default=None, json_schema_extra=_OMIT)] = None
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
    counterparty_account_key: Annotated[str | None, Field(pattern=r"^[0-9a-f]{64}$", json_schema_extra=_OMIT)] = None
    inactive_reason: Annotated[
        Literal["rule_key_unavailable", "rule_key_rotated"] | None,
        Field(
            description="Set when a counterparty-account rule cannot match because its key is missing or was rotated.",
            json_schema_extra=_OMIT,
        ),
    ] = None
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
    label: Annotated[str | None, Field(json_schema_extra=_OMIT)] = None
    display_label: Annotated[str | None, Field(default=None, json_schema_extra=_OMIT)] = None


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


BudgetReceiptId = Annotated[str, Field(pattern=r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")]


class BudgetAnalyticsTransactionReceipt(BaseModel):
    model_config = _READ
    receipt_id: BudgetReceiptId
    shop_name: str | None
    total: str | None
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None
    status: Literal["confirmed", "needs_review"]


class BudgetAnalyticsTransaction(BudgetTransaction):
    account_id: str
    fingerprint: str
    transfer_fingerprint: str
    annotation: BudgetAnalyticsAnnotation
    effective: BudgetAnalyticsEffective
    metadata: BudgetAnalyticsMetadata | None
    counterparty_account_key: Annotated[str | None, Field(pattern=r"^[0-9a-f]{64}$", json_schema_extra=_OMIT)] = None
    counterparty_account: Annotated[str | None, Field(json_schema_extra=_OMIT)] = None
    transfer: BudgetAnalyticsTransactionTransfer
    receipt: Annotated[BudgetAnalyticsTransactionReceipt | None, Field(default=None, json_schema_extra=_OMIT)] = None


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
    display_name: Annotated[str | None, Field(default=None, json_schema_extra=_OMIT)] = None
    display_label: Annotated[str | None, Field(default=None, json_schema_extra=_OMIT)] = None
    archived: bool
    currency: str
    expenses: str
    refunds: str
    net_spending: str
    one_off: Annotated[bool, Field(json_schema_extra=_OMIT)] = False
    # Distinct from category_id. A short hash of the account and entry reference, never the label text.
    one_off_key: Annotated[str | None, Field(pattern=r"^[0-9a-f]{64}$", json_schema_extra=_OMIT)] = None


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
    display_name: Annotated[str | None, Field(default=None, json_schema_extra=_OMIT)] = None
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


BudgetAnalyticsInboxKind = Literal[
    "consent_renewal",
    "mortgage_due",
    "spending_limit",
    "device_added",
    "appointment_slot",
    "appointment_attention",
]
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


class BudgetAnalyticsInboxDeviceParams(BaseModel):
    model_config = _READ
    status: Literal["added"]
    device_name: Annotated[str, Field(min_length=1, max_length=64)]
    date: Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")]
    actor: Annotated[str | None, Field(default=None, json_schema_extra=_OMIT)] = None


class BudgetAnalyticsInboxDeviceRef(BaseModel):
    model_config = _READ
    token_id: str


class BudgetAnalyticsInboxDeviceItem(BudgetAnalyticsInboxItemBase):
    kind: Literal["device_added"]
    ref: BudgetAnalyticsInboxDeviceRef
    params: BudgetAnalyticsInboxDeviceParams


class BudgetAnalyticsInboxAppointmentRef(BaseModel):
    model_config = _READ
    watch_id: str


class BudgetAnalyticsInboxAppointmentSlotParams(BaseModel):
    model_config = _READ
    status: Literal["better_slot", "new_slot"]
    date: Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")]
    message: Annotated[str, Field(min_length=1, max_length=256)]
    better_than_booked: bool


class BudgetAnalyticsInboxAppointmentSlotItem(BudgetAnalyticsInboxItemBase):
    kind: Literal["appointment_slot"]
    ref: BudgetAnalyticsInboxAppointmentRef
    params: BudgetAnalyticsInboxAppointmentSlotParams


class BudgetAnalyticsInboxAppointmentAttentionParams(BaseModel):
    model_config = _READ
    status: Literal["circuit_open", "authorization"]
    error_code: Literal[
        "network",
        "rate_limit",
        "provider_http",
        "authorization",
        "invalid_response",
        "run_limit",
    ]
    consecutive_failures: Annotated[int, Field(ge=1)]


class BudgetAnalyticsInboxAppointmentAttentionItem(BudgetAnalyticsInboxItemBase):
    kind: Literal["appointment_attention"]
    ref: BudgetAnalyticsInboxAppointmentRef
    params: BudgetAnalyticsInboxAppointmentAttentionParams


BudgetAnalyticsInboxItem = Annotated[
    BudgetAnalyticsInboxConsentItem
    | BudgetAnalyticsInboxMortgageItem
    | BudgetAnalyticsInboxSpendingLimitItem
    | BudgetAnalyticsInboxDeviceItem
    | BudgetAnalyticsInboxAppointmentSlotItem
    | BudgetAnalyticsInboxAppointmentAttentionItem,
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


class BudgetAnalyticsQuarantineBatchRequest(BaseModel):
    model_config = _STRICT
    batch_id: Annotated[str, Field(min_length=1, max_length=128)]


class BudgetAnalyticsQuarantineImportKey(BudgetAnalyticsQuarantineBatchRequest):
    ordinal: Annotated[int, Field(ge=0, le=2**53 - 1)]


class BudgetAnalyticsQuarantineDismissal(BaseModel):
    model_config = _READ
    dismissed_count: Annotated[int, Field(ge=0)]


class BudgetAnalyticsQuarantineDismissResult(BaseModel):
    model_config = _READ
    ok: Literal[True]
    dismissal: BudgetAnalyticsQuarantineDismissal


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
    import_key: Annotated[BudgetAnalyticsQuarantineImportKey | None, Field(
        description="Local dismissal key on import_unresolved items only, starting at 0.63.0.",
        json_schema_extra=_OMIT,
    )] = None


class BudgetAnalyticsQuarantine(BaseModel):
    """Read-only owner-scoped quarantine counts and bounded safe entry metadata (zero mutations, zero bank I/O)."""
    model_config = _READ
    rule_version: int
    summary: BudgetAnalyticsQuarantineSummary
    accounts: list[BudgetAnalyticsQuarantineAccount]
    items_cap: int
    truncated: bool
    items: list[BudgetAnalyticsQuarantineItem]


class BudgetAnalyticsForecastBaselineConflict(BaseModel):
    """A configured category omitted because an active hint or an enabled mortgage already represents it.

    No amounts. A category may appear twice when both reasons apply.
    """
    model_config = _READ
    category_id: Annotated[str, Field(min_length=1, max_length=128)]
    reason: Literal["recurring", "mortgage"]


class BudgetAnalyticsForecastSummary(BaseModel):
    model_config = _READ
    selected_accounts: int
    excluded_accounts: int
    stale_accounts: int
    overdue_mortgage_schedule_count: int
    lapsed_recurring_count: int
    deduped_recurring_count: int
    stale_recurring_count: int
    baseline_conflicts: Annotated[
        list[BudgetAnalyticsForecastBaselineConflict], Field(default_factory=list, max_length=64)]


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
    source: Literal["mortgage", "recurring", "category_baseline"]
    schedule_id: str | None
    label: BudgetAnalyticsForecastLabel
    display_name: Annotated[str | None, Field(default=None, json_schema_extra=_OMIT)] = None
    amount: str
    currency: str
    expected_date: str
    confidence: Literal["scheduled", "high", "medium", "estimated"]
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
    pending_row_keys: Annotated[list[BudgetAnalyticsRowKey], Field(default_factory=list, max_length=500)]
    pending_row_keys_truncated: bool = Field(default_factory=lambda: False)
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
    display_name: Annotated[str | None, Field(default=None, json_schema_extra=_OMIT)] = None
    display_label: Annotated[str | None, Field(default=None, json_schema_extra=_OMIT)] = None
    archived: bool
    color_hue: Annotated[BudgetCategoryHue | None, Field(json_schema_extra=_OMIT)] = None
    expenses: BudgetAnalyticsMonthlyBudgetMoney
    refunds: BudgetAnalyticsMonthlyBudgetMoney
    net_spending: BudgetAnalyticsMonthlyBudgetMoney
    limit: BudgetAnalyticsMonthlyBudgetCategoryLimit | None
    one_off: Annotated[bool, Field(json_schema_extra=_OMIT)] = False
    one_off_key: Annotated[str | None, Field(pattern=r"^[0-9a-f]{64}$", json_schema_extra=_OMIT)] = None


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


class BudgetAnalyticsMonthlyBudgetCardDebtAccount(BaseModel):
    model_config = _READ
    account_id: str
    debt: BudgetAnalyticsMonthlyBudgetMoney
    limit: BudgetAnalyticsMonthlyBudgetMoney
    available: BudgetAnalyticsMonthlyBudgetMoney
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    as_of: str | None


class BudgetAnalyticsMonthlyBudgetCardDebt(BaseModel):
    model_config = _READ
    status: Literal["available", "partial", "unavailable"]
    amount: BudgetAnalyticsMonthlyBudgetMoney | None
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    as_of_date: str | None
    accounts: Annotated[
        list[BudgetAnalyticsMonthlyBudgetCardDebtAccount] | None,
        Field(default=None, json_schema_extra=_OMIT),
    ] = None


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
    card_debt: Annotated[
        BudgetAnalyticsMonthlyBudgetCardDebt | None,
        Field(default=None, json_schema_extra=_OMIT),
    ] = None


BudgetReceiptMoney = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^-?[0-9]+(?:\.[0-9]+)?$")]
BudgetReceiptCursor = Annotated[str, Field(min_length=1, max_length=8192)]
BudgetReceiptDocumentKind = Literal["receipt", "invoice", "payment_confirmation"]
BudgetReceiptGmailGrantStatus = Literal["disabled", "active", "revoked"]
BudgetReceiptGmailSyncPhase = Literal["idle", "backfill", "incremental", "full_resync"]
BudgetReceiptGmailAttention = Literal["authorization", "configuration", "identity_mismatch", "quota"]
BudgetReceiptGmailParseFailedReason = Literal[*PARSE_FAILED_REASONS]


class BudgetReceiptParseTextRequest(BaseModel):
    model_config = _STRICT
    text: Annotated[str, Field(min_length=1, max_length=32768,
                               description="UTF-8 text is additionally capped at 32 KiB and 500 lines.")]
    source_kind: Annotated[Literal["paste", "local_ocr"], Field(
        description="Defaults to paste, stored as live_text. local_ocr is stored as local_ocr.",
        json_schema_extra=_OMIT,
    )] = "paste"


class BudgetReceiptLineInput(BaseModel):
    model_config = _STRICT
    line_kind: Literal["item", "deposit", "basket_discount", "other"]
    raw_name: Annotated[str, Field(min_length=1, max_length=128)]
    ptu_code: Literal["A", "B", "C", "D", "E", "F", "G"] | None
    quantity: BudgetReceiptMoney
    raw_unit: Annotated[str, Field(max_length=16)] | None
    unit_price_raw: BudgetReceiptMoney | None
    gross_line_total: BudgetReceiptMoney
    discount_amount: BudgetReceiptMoney
    net_line_total: BudgetReceiptMoney


class BudgetReceiptLinkRequest(BaseModel):
    model_config = _STRICT
    account_id: BudgetId
    entry_reference: BudgetAnalyticsReference
    candidate_token: Annotated[str, Field(
        min_length=1, max_length=4096,
        description="HMAC bound to the owner, transaction, three dates and merchant evidence shown at preview.")]


_ConfirmReceiptLines = Annotated[list[BudgetReceiptLineInput], Field(max_length=300)]


class BudgetReceiptConfirmRequest(BaseModel):
    model_config = _STRICT
    receipt_id: BudgetReceiptId
    expected_version: BudgetVersion
    raw_shop_name: Annotated[
        Annotated[str, Field(max_length=128)] | None,
        Field(
            default=None,
            description="Omit to keep the stored shop name unchanged; send null to clear.",
            json_schema_extra=_OMIT,
        ),
    ] = None
    raw_nip: Annotated[
        Annotated[str, Field(pattern=r"^(?:|[0-9]{10})$")] | None,
        Field(
            default=None,
            description="Omit to keep the stored NIP unchanged; send null to clear.",
            json_schema_extra=_OMIT,
        ),
    ] = None
    receipt_kind: Literal["purchase", "refund"]
    receipt_date: Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
                                       json_schema_extra={"format": "date"})]
    receipt_time: Annotated[str, Field(pattern=r"^(?:|(?:[01][0-9]|2[0-3]):[0-5][0-9])$")] | None
    total_amount: BudgetReceiptMoney
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    payment_method: Literal["card", "blik", "cash", "mixed", "unknown"]
    lines: Annotated[
        _ConfirmReceiptLines | None,
        Field(
            default=None,
            description="Omit or send null to keep stored lines unchanged and re-validate totals against them.",
            json_schema_extra=_OMIT,
        ),
    ] = None
    link: BudgetReceiptLinkRequest | None


class BudgetReceiptListRequest(BaseModel):
    model_config = _STRICT
    month: Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}$")] | None = None
    status: Literal["draft", "confirmed", "needs_review", "imported"] | None = None
    document_kind: BudgetReceiptDocumentKind | None = None
    linked: bool | None = None
    evidence: Literal["exclude", "only", "include"] = "exclude"
    limit: Annotated[int, Field(ge=1, le=200)] = 50
    cursor: BudgetReceiptCursor | None = None


class BudgetReceiptSearchRequest(BaseModel):
    model_config = _STRICT
    query: Annotated[str, Field(min_length=1, max_length=128)]
    limit: Annotated[int, Field(ge=1, le=200)] = 50
    cursor: BudgetReceiptCursor | None = None


class BudgetReceiptDeleteRequest(BaseModel):
    model_config = _STRICT
    receipt_id: BudgetReceiptId
    expected_version: BudgetVersion
    dissolve_group: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "When true with expected_group_version and budget.receipts.match, dissolve an active or "
                "needs_review receipt group and restore surviving members/evidence before deleting this receipt."
            ),
            json_schema_extra=_OMIT,
        ),
    ] = False
    expected_group_version: Annotated[
        BudgetVersion | None,
        Field(
            default=None,
            description="Required current group version when dissolve_group is true; must be omitted or null otherwise.",
            json_schema_extra=_OMIT,
        ),
    ] = None

    @model_validator(mode="after")
    def _check_dissolve_group(self) -> "BudgetReceiptDeleteRequest":
        if self.dissolve_group and self.expected_group_version is None:
            raise ValueError("expected_group_version is required when dissolve_group is true")
        if not self.dissolve_group and self.expected_group_version is not None:
            raise ValueError("expected_group_version requires dissolve_group=true")
        return self


class BudgetReceiptLine(BudgetReceiptLineInput):
    model_config = _READ
    line_no: int
    raw_name_key: str


class BudgetReceiptLinkGroupRef(BaseModel):
    model_config = _READ
    group_id: BudgetReceiptId
    group_status: Literal["confirmed", "needs_review"]
    group_version: BudgetVersion
    member_count: Annotated[int, Field(ge=0)]
    fee_amount: BudgetReceiptMoney | None
    evidence_receipt_id: BudgetReceiptId | None


class BudgetReceiptLinkedTransaction(BaseModel):
    model_config = _READ
    account_label: str
    booking_date: Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
                                       json_schema_extra={"format": "date"})] | None
    amount: str | None
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None


class BudgetReceiptSummary(BaseModel):
    model_config = _READ
    receipt_id: BudgetReceiptId
    shop_id: str | None
    chain_key: str | None
    raw_shop_name: str | None
    display_shop_name: Annotated[str | None, Field(
        description="Canonical OCR issuer name when available. Emitted on list, search and detail from 0.61.0.",
        json_schema_extra=_OMIT,
    )] = None
    raw_nip: str | None
    receipt_kind: Literal["purchase", "refund"]
    receipt_date: str | None
    receipt_time: str | None
    total_amount: str | None
    currency: str | None
    payment_method: str | None
    status: Literal["draft", "confirmed", "needs_review", "imported"]
    source_kind: Literal["live_text", "local_ocr", "cloud_ai", "paste", "photo", "email", "attachment"]
    linked: bool
    linked_account_id: str | None
    linked_entry_ref: str | None
    version: int
    created_at: float
    updated_at: float
    image_available: bool
    image_expires_at: float | None
    raw_text_expires_at: float | None
    math_delta: str | None
    document_kind: BudgetReceiptDocumentKind | None = None
    invoice_number: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    due_date: Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
                                   json_schema_extra={"format": "date"})] | None = None
    import_expires_at: float | None = None
    forwarded_manually: bool = False
    forwarded_sender_domain: Annotated[str, Field(min_length=1, max_length=253)] | None = None
    gateway: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    gateway_status: Literal["registered", "paid"] | None = None
    fee_amount: str | None = None
    payee_host: Annotated[str, Field(min_length=1, max_length=253)] | None = None
    evidence_for_receipt_id: BudgetReceiptId | None = None
    has_document: bool = True
    card_last4: Annotated[str, Field(pattern=r"^[0-9]{4}$")] | None = None
    link_group: Annotated[BudgetReceiptLinkGroupRef | None, Field(default=None, json_schema_extra=_OMIT)] = None
    linked_transaction: Annotated[
        BudgetReceiptLinkedTransaction | None, Field(default=None, json_schema_extra=_OMIT)
    ] = None


class BudgetReceiptPaymentEvidence(BaseModel):
    model_config = _READ
    receipt_id: BudgetReceiptId
    gateway: str | None
    gateway_status: Literal["registered", "paid"] | None
    total_amount: str | None
    fee_amount: str | None
    receipt_date: str | None


class BudgetReceiptDetail(BudgetReceiptSummary):
    lines: list[BudgetReceiptLine]
    payment_evidence: list[BudgetReceiptPaymentEvidence] = []


class BudgetReceiptCandidate(BaseModel):
    model_config = _READ
    account_id: str
    entry_reference: str
    amount: str
    currency: str
    credit_debit_indicator: Literal["DBIT", "CRDT"]
    transaction_date: str | None
    booking_date: str | None
    value_date: str | None
    merchant_token_hit: bool
    match_strength: Literal["id", "nip", "distinctive", "generic", "none"]
    date_distance: int
    candidate_token: Annotated[str, Field(min_length=1, max_length=4096)]


class BudgetReceiptParseResult(BaseModel):
    model_config = _READ
    receipt_id: BudgetReceiptId
    draft: BudgetReceiptDetail
    candidates: list[BudgetReceiptCandidate]
    version: int
    no_bank_data: Annotated[bool | None, Field(default=None, json_schema_extra=_OMIT)] = None


class BudgetReceiptGroupTransactionCandidate(BaseModel):
    model_config = _READ
    account_id: str
    entry_reference: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    amount: str
    currency: str
    credit_debit_indicator: Literal["DBIT", "CRDT"]
    transaction_date: str | None
    booking_date: str | None
    value_date: str | None
    merchant_token_hit: bool
    match_strength: Literal["id", "nip", "distinctive", "generic", "none"]
    date_distance: int
    evidence_fingerprint: str
    candidate_token: Annotated[str, Field(min_length=1, max_length=4096)]


class BudgetReceiptGroupCandidate(BaseModel):
    model_config = _READ
    account_id: str
    entry_reference: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    amount: str
    currency: str
    credit_debit_indicator: Literal["DBIT", "CRDT"]
    transaction_date: str | None
    booking_date: str | None
    value_date: str | None
    match_strength: Literal["id", "nip", "distinctive", "generic", "none"]
    date_distance: int
    evidence_fingerprint: str
    candidate_token: Annotated[str, Field(min_length=1, max_length=4096)]
    fee_amount: BudgetReceiptMoney | None
    evidence_receipt_id: BudgetReceiptId | None
    members_sum: BudgetReceiptMoney
    member_receipt_ids: list[BudgetReceiptId]
    member_receipts: list[BudgetReceiptSummary]
    evidence: BudgetReceiptSummary | None = None


class BudgetReceiptGroupCandidatesRequest(BaseModel):
    model_config = _STRICT
    receipt_id: BudgetReceiptId


class BudgetReceiptGroupCandidatesResult(BaseModel):
    model_config = _READ
    anchor_receipt_id: BudgetReceiptId
    transactions: list[BudgetReceiptGroupTransactionCandidate]
    proposals: list[BudgetReceiptGroupCandidate]
    eligible_receipts: list[BudgetReceiptSummary]
    eligible_evidence: list[BudgetReceiptSummary]
    no_bank_data: Annotated[bool | None, Field(default=None, json_schema_extra=_OMIT)] = None


class BudgetReceiptGroupMemberInput(BaseModel):
    model_config = _STRICT
    receipt_id: BudgetReceiptId
    expected_version: BudgetVersion


class BudgetReceiptGroupLinkRequest(BaseModel):
    model_config = _STRICT
    link: BudgetReceiptLinkRequest
    members: Annotated[list[BudgetReceiptGroupMemberInput], Field(min_length=2, max_length=8)]
    evidence_receipt_id: Annotated[BudgetReceiptId | None, Field(default=None, json_schema_extra=_OMIT)] = None
    evidence_expected_version: Annotated[BudgetVersion | None, Field(default=None, json_schema_extra=_OMIT)] = None
    fee_amount: Annotated[BudgetReceiptMoney | None, Field(default=None, json_schema_extra=_OMIT)] = None

    @model_validator(mode="after")
    def _check_group_link(self) -> "BudgetReceiptGroupLinkRequest":
        member_ids = [member.receipt_id for member in self.members]
        if len(set(member_ids)) != len(member_ids):
            raise ValueError("members must contain unique receipt_id values")
        if (self.evidence_receipt_id is None) != (self.evidence_expected_version is None):
            raise ValueError("evidence_receipt_id and evidence_expected_version must be supplied together")
        if self.evidence_receipt_id is not None and self.evidence_receipt_id in set(member_ids):
            raise ValueError("evidence_receipt_id cannot also appear in members")
        if self.fee_amount is not None:
            if self.fee_amount.startswith("-"):
                raise ValueError("fee_amount must be non-negative")
            whole, frac = (self.fee_amount.split(".") + [""])[:2]
            if any(ch != "0" for ch in whole + frac) and self.evidence_receipt_id is None:
                raise ValueError("positive fee_amount requires evidence_receipt_id")
        return self


class BudgetReceiptGroupLinkResult(BaseModel):
    model_config = _READ
    ok: Literal[True]
    group: BudgetReceiptLinkGroupRef
    members: list[BudgetReceiptSummary]
    evidence: BudgetReceiptSummary | None
    linked_account_id: str | None
    linked_entry_ref: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None


class BudgetReceiptGroupUnlinkRequest(BaseModel):
    model_config = _STRICT
    group_id: BudgetReceiptId
    expected_version: BudgetVersion


class BudgetReceiptGroupUnlinkResult(BaseModel):
    model_config = _READ
    ok: Literal[True]
    unlinked: Literal[True]
    group_id: BudgetReceiptId
    members: list[BudgetReceiptSummary]
    evidence: BudgetReceiptSummary | None


class BudgetReceiptDetailResult(BaseModel):
    model_config = _READ
    receipt: BudgetReceiptDetail
    # Present only when the receipt is needs_review, so it can re-issue a token for its own link.
    candidates: Annotated[
        list[BudgetReceiptCandidate] | None, Field(default=None, json_schema_extra=_OMIT),
    ] = None
    group_candidates: Annotated[
        list[BudgetReceiptGroupCandidate] | None, Field(default=None, json_schema_extra=_OMIT),
    ] = None
    no_bank_data: Annotated[bool | None, Field(default=None, json_schema_extra=_OMIT)] = None


class BudgetReceiptGroupDetailResult(BaseModel):
    model_config = _READ
    group: BudgetReceiptLinkGroupRef
    members: list[BudgetReceiptSummary]
    evidence: BudgetReceiptSummary | None
    linked_account_id: str | None
    linked_entry_ref: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None


class BudgetReceiptConfirmResult(BaseModel):
    model_config = _READ
    ok: Literal[True]
    receipt: BudgetReceiptDetail


class BudgetReceiptListResult(BaseModel):
    model_config = _READ
    receipts: list[BudgetReceiptSummary]
    next_cursor: str | None


class BudgetReceiptDeleteResult(BaseModel):
    model_config = _READ
    ok: Literal[True]
    deleted: Literal[True]


class BudgetReceiptDeviceBrowserRequest(BaseModel):
    """Empty JSON object for pairing-code issuance and the device list."""

    model_config = _STRICT


class BudgetReceiptDevicePairResult(BaseModel):
    model_config = _READ
    code_id: BudgetReceiptId
    code: Annotated[str, Field(pattern=r"^[0-9A-HJKMNP-TV-Z]{8}$")]
    expires_at: float
    qr_payload: Annotated[str, Field(min_length=1, max_length=64)]


class BudgetReceiptDeviceCancelRequest(BaseModel):
    model_config = _STRICT
    code_id: BudgetReceiptId


class BudgetReceiptDeviceLoginRequest(BaseModel):
    model_config = _STRICT
    username: str
    password: Annotated[str, Field(json_schema_extra={"format": "password", "writeOnly": True})]
    device_name: Annotated[str, Field(min_length=1, max_length=64, description="Printable device name.")]


class BudgetReceiptDeviceRedeemRequest(BaseModel):
    model_config = _STRICT
    code: Annotated[str, Field(min_length=8, max_length=4096, description=(
        "Eight Crockford base32 characters after uppercasing, stripping spaces and dashes, "
        "and mapping O to 0 and I or L to 1. U is rejected."
    ))]
    device_name: Annotated[str, Field(min_length=1, max_length=64)]


class BudgetReceiptDeviceRedeemResult(BaseModel):
    model_config = _READ
    token: Annotated[str, Field(min_length=43, max_length=43)]
    token_id: BudgetReceiptId
    scope: Literal["receipts"]
    expires_at: AwareDatetime


class BudgetReceiptDeviceToken(BaseModel):
    model_config = _READ
    token_id: BudgetReceiptId
    device_name: Annotated[str, Field(min_length=1, max_length=64)]
    created_at: float
    last_used_at: float | None
    expires_at: float


class BudgetReceiptDeviceRevokeRequest(BaseModel):
    model_config = _STRICT
    token_id: BudgetReceiptId


class BudgetReceiptDeviceRevokeResult(BaseModel):
    model_config = _READ
    ok: Literal[True]

class BudgetReceiptGmailGrantRequest(BaseModel):
    model_config = _STRICT
    action: Literal["enable", "disable"]
    expected_version: BudgetVersion


class BudgetReceiptGmailRevokeRequest(BaseModel):
    model_config = _STRICT
    expected_version: BudgetVersion


class BudgetReceiptGmailSyncRequest(BaseModel):
    model_config = _STRICT
    force_full_resync: bool = False
    retry_parse_failed: bool = False


class BudgetReceiptGmailGrantResult(BaseModel):
    model_config = _READ
    ok: Literal[True]
    grant_status: BudgetReceiptGmailGrantStatus
    grant_generation: int
    version: int


class BudgetReceiptGmailSyncResult(BaseModel):
    model_config = _READ
    ok: Literal[True]
    sync_phase: BudgetReceiptGmailSyncPhase
    version: int


class BudgetReceiptGmailStatus(BaseModel):
    model_config = _READ
    enabled: bool
    configured: bool
    grant_status: BudgetReceiptGmailGrantStatus
    grant_generation: int
    version: int
    sync_phase: BudgetReceiptGmailSyncPhase
    attention: BudgetReceiptGmailAttention | None
    last_sync_at: float | None
    next_sync_at: float
    backoff_until: float
    failures: int
    last_http_status: int | None
    last_error: str | None
    pending_imported_count: int
    batch_processed_count: int
    parse_failed_reasons: dict[BudgetReceiptGmailParseFailedReason, int]
    google_revoke_url: Literal["https://myaccount.google.com/linkedapps"]
    google_revoke_affects_calendar_sync: Literal[True]


_RECEIPT_MODELS = (
    BudgetReceiptParseTextRequest, BudgetReceiptConfirmRequest, BudgetReceiptListRequest,
    BudgetReceiptSearchRequest, BudgetReceiptDeleteRequest, BudgetReceiptGroupCandidatesRequest,
    BudgetReceiptGroupMemberInput, BudgetReceiptGroupLinkRequest, BudgetReceiptGroupUnlinkRequest,
    BudgetReceiptParseResult, BudgetReceiptGroupTransactionCandidate, BudgetReceiptGroupCandidate,
    BudgetReceiptGroupCandidatesResult, BudgetReceiptDetailResult, BudgetReceiptGroupDetailResult,
    BudgetReceiptGroupLinkResult, BudgetReceiptGroupUnlinkResult, BudgetReceiptConfirmResult,
    BudgetReceiptListResult, BudgetReceiptDeleteResult,
    BudgetReceiptDeviceBrowserRequest, BudgetReceiptDevicePairResult, BudgetReceiptDeviceCancelRequest,
    BudgetReceiptDeviceLoginRequest, BudgetReceiptDeviceRedeemRequest,
    BudgetReceiptDeviceRedeemResult, BudgetReceiptDeviceToken, BudgetReceiptDeviceRevokeRequest,
    BudgetReceiptDeviceRevokeResult,

    BudgetReceiptGmailGrantRequest, BudgetReceiptGmailRevokeRequest, BudgetReceiptGmailSyncRequest,
    BudgetReceiptGmailGrantResult, BudgetReceiptGmailSyncResult, BudgetReceiptGmailStatus,
)


class BudgetAnalyticsMonthlyBudgetDailyPoint(BaseModel):
    model_config = _READ
    date: str
    cumulative_spent: BudgetAnalyticsMonthlyBudgetMoney


class BudgetAnalyticsMonthlyBudgetSeriesCategory(BaseModel):
    model_config = _READ
    category_id: str | None
    name: Annotated[str | None, Field(json_schema_extra=_OMIT)] = None
    display_name: Annotated[str | None, Field(default=None, json_schema_extra=_OMIT)] = None
    display_label: Annotated[str | None, Field(default=None, json_schema_extra=_OMIT)] = None
    one_off: Annotated[bool, Field(json_schema_extra=_OMIT)] = False
    one_off_key: Annotated[str | None, Field(pattern=r"^[0-9a-f]{64}$", json_schema_extra=_OMIT)] = None
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
    display_name: Annotated[str | None, Field(default=None, json_schema_extra=_OMIT)] = None
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



_HouseholdGrantId = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)]
_HouseholdAlias = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)]
_HouseholdMoney = Annotated[str, Field(min_length=1, max_length=192)]


class BudgetHouseholdAccess(BaseModel):
    """Discovery. The only household read that does not take X-Budget-Grant."""
    model_config = _READ
    grant_id: _HouseholdGrantId


class BudgetHouseholdAccount(BaseModel):
    model_config = _READ
    account_id: _HouseholdAlias
    ordinal: Annotated[int, Field(ge=1)]


class BudgetHouseholdAccounts(BaseModel):
    model_config = _READ
    grant_id: _HouseholdGrantId
    accounts: list[BudgetHouseholdAccount]


class BudgetHouseholdSeriesDay(BaseModel):
    model_config = _READ
    date: str
    cumulative_spent: _HouseholdMoney


class BudgetHouseholdSeriesCategory(BaseModel):
    model_config = _READ
    category_id: _HouseholdAlias | None
    net_spending: _HouseholdMoney


class BudgetHouseholdSeriesMonth(BaseModel):
    model_config = _READ
    period: str
    has_activity: bool
    is_current_month: bool
    income_estimate_partial: bool
    provisional: bool
    start_budget: _HouseholdMoney
    spent: _HouseholdMoney
    categories: list[BudgetHouseholdSeriesCategory]


class BudgetHouseholdCategoryLabel(BaseModel):
    model_config = _READ
    category_id: _HouseholdAlias
    name: str
    archived: bool
    color_hue: BudgetCategoryHue | None


class BudgetHouseholdSeries(BaseModel):
    model_config = _READ
    daily: list[BudgetHouseholdSeriesDay]
    months: list[BudgetHouseholdSeriesMonth]
    category_labels: list[BudgetHouseholdCategoryLabel]


class BudgetHouseholdMonthlyBudget(BaseModel):
    """Same calculations as the owner dashboard, with grant-scoped aliases and safe labels."""
    model_config = _READ
    grant_id: _HouseholdGrantId
    budget: BudgetAnalyticsMonthlyBudget
    series: BudgetHouseholdSeries


class BudgetHouseholdMoney(BaseModel):
    model_config = _READ
    currency: str
    expenses: _HouseholdMoney
    refunds: _HouseholdMoney
    income: _HouseholdMoney
    net_spending: _HouseholdMoney


class BudgetHouseholdOverviewMonth(BaseModel):
    model_config = _READ
    month: str
    currencies: list[BudgetHouseholdMoney]


class BudgetHouseholdOverviewCategory(BaseModel):
    model_config = _READ
    category_id: _HouseholdAlias | None
    label: str | None
    archived: bool
    color_hue: BudgetCategoryHue | None
    currency: str
    expenses: _HouseholdMoney
    refunds: _HouseholdMoney
    income: _HouseholdMoney
    net_spending: _HouseholdMoney


class BudgetHouseholdOverview(BaseModel):
    model_config = _READ
    grant_id: _HouseholdGrantId
    period: str
    currencies: list[BudgetHouseholdMoney]
    months: list[BudgetHouseholdOverviewMonth]
    categories: list[BudgetHouseholdOverviewCategory]


class BudgetHouseholdTransaction(BaseModel):
    model_config = _READ
    id: _HouseholdAlias
    date: str
    account_id: _HouseholdAlias
    category_id: _HouseholdAlias | None
    kind: Literal["expense", "income", "refund", "excluded", "unclassified_credit", "transfer"]
    currency: str
    amount: _HouseholdMoney
    direction: Literal["DBIT", "CRDT"]


class BudgetHouseholdTransactions(BaseModel):
    model_config = _READ
    grant_id: _HouseholdGrantId
    transactions: list[BudgetHouseholdTransaction]
    filtered_total: int
    next_cursor: str | None


class BudgetHouseholdCategory(BaseModel):
    model_config = _READ
    id: _HouseholdAlias
    label: str
    archived: bool
    color_hue: BudgetCategoryHue | None


class BudgetHouseholdCategories(BaseModel):
    model_config = _READ
    grant_id: _HouseholdGrantId
    categories: list[BudgetHouseholdCategory]


class BudgetHouseholdRule(BaseModel):
    model_config = _READ
    id: _HouseholdAlias
    field: Literal["description", "counterparty_name", "mcc"]
    operator: Literal["contains", "equals"]
    category_id: _HouseholdAlias | None
    kind: Literal["auto", "expense", "income", "refund", "excluded"]
    priority: int
    enabled: bool


class BudgetHouseholdRules(BaseModel):
    model_config = _READ
    grant_id: _HouseholdGrantId
    rules: list[BudgetHouseholdRule]


class BudgetHouseholdTransferLeg(BaseModel):
    model_config = _READ
    account_id: _HouseholdAlias
    date: str
    amount: _HouseholdMoney
    currency: str


class BudgetHouseholdTransfer(BaseModel):
    model_config = _READ
    id: _HouseholdAlias
    status: Literal["suggested", "accepted", "review", "boundary", "rejected", "undone"]
    debit: BudgetHouseholdTransferLeg
    credit: BudgetHouseholdTransferLeg


class BudgetHouseholdTransfers(BaseModel):
    model_config = _READ
    grant_id: _HouseholdGrantId
    transfers: list[BudgetHouseholdTransfer]
    filtered_total: int
    next_cursor: str | None


class BudgetHouseholdSpendingLimit(BaseModel):
    model_config = _READ
    category_id: _HouseholdAlias
    currency: str
    monthly_limit: str
    warning_percent: Annotated[int, Field(ge=1, le=99)]
    status: Literal["within", "approaching", "at_limit", "exceeded"]
    net_spending: _HouseholdMoney


class BudgetHouseholdSpendingLimits(BaseModel):
    model_config = _READ
    grant_id: _HouseholdGrantId
    limits: list[BudgetHouseholdSpendingLimit]


class BudgetHouseholdRecurringHint(BaseModel):
    model_config = _READ
    ordinal: Annotated[int, Field(ge=1)]
    amount: _HouseholdMoney
    currency: str
    cadence: str
    first_date: str
    last_date: str
    occurrence_count: Annotated[int, Field(ge=0)]


class BudgetHouseholdRecurring(BaseModel):
    model_config = _READ
    grant_id: _HouseholdGrantId
    hints: list[BudgetHouseholdRecurringHint]


class BudgetHouseholdMortgageSchedule(BaseModel):
    model_config = _READ
    id: _HouseholdAlias
    ordinal: Annotated[int, Field(ge=1)]
    currency: str
    amount: _HouseholdMoney
    enabled: bool
    due_day: Annotated[int, Field(ge=1, le=31)]
    start_date: str
    end_date: str | None
    paid_months: list[str]


class BudgetHouseholdMortgageReminder(BaseModel):
    model_config = _READ
    schedule_id: _HouseholdAlias
    due_date: str | None
    status: Literal["overdue", "due_today", "due_soon", "upcoming", "completed"]


class BudgetHouseholdMortgages(BaseModel):
    model_config = _READ
    grant_id: _HouseholdGrantId
    evaluation_date: str
    schedules: list[BudgetHouseholdMortgageSchedule]
    reminders: list[BudgetHouseholdMortgageReminder]


class BudgetHouseholdInboxItem(BaseModel):
    model_config = _READ
    id: _HouseholdAlias
    kind: Literal["mortgage_due", "spending_limit"]
    read: bool
    status: str
    schedule_id: _HouseholdAlias | None = None
    due_date: str | None = None
    category_id: _HouseholdAlias | None = None
    currency: str | None = None
    period: str | None = None


class BudgetHouseholdInbox(BaseModel):
    model_config = _READ
    grant_id: _HouseholdGrantId
    evaluation_date: str
    unread_count: Annotated[int, Field(ge=0)]
    items: list[BudgetHouseholdInboxItem]


class BudgetHouseholdQuarantine(BaseModel):
    model_config = _READ
    grant_id: _HouseholdGrantId
    uncertain_count: Annotated[int, Field(ge=0)]
    unresolved_count: Annotated[int, Field(ge=0)]
    import_unresolved_count: Annotated[int, Field(ge=0)]


class BudgetHouseholdPassThrough(BaseModel):
    model_config = _READ
    rent_category_id: _HouseholdAlias
    mortgage_category_id: _HouseholdAlias


class BudgetHouseholdSettings(BaseModel):
    """Bootstrap month config only. An absent owner entry is an empty income list and null pass-through."""
    model_config = _READ
    grant_id: _HouseholdGrantId
    income_category_ids: list[_HouseholdAlias]
    pass_through: BudgetHouseholdPassThrough | None


_HOUSEHOLD_MODELS = (BudgetHouseholdAccess, BudgetHouseholdAccounts, BudgetHouseholdMonthlyBudget,
                     BudgetHouseholdOverview, BudgetHouseholdTransactions, BudgetHouseholdCategories,
                     BudgetHouseholdRules, BudgetHouseholdTransfers, BudgetHouseholdSpendingLimits,
                     BudgetHouseholdRecurring, BudgetHouseholdMortgages, BudgetHouseholdInbox,
                     BudgetHouseholdQuarantine, BudgetHouseholdSettings)

_ANALYTICS_MODELS = (BudgetAnalyticsCategoryCreateRequest, BudgetAnalyticsCategoryUpdateRequest,
                     BudgetAnalyticsCategoryColorSpreadRequest, BudgetAnalyticsCategoryColorSpreadResult,
                     BudgetAnalyticsAnnotationRequest, BudgetAnalyticsTransactionsByKeysRequest,
                     BudgetCategoryTranslationSetRequest, BudgetCategoryTranslationDeleteRequest,
                     BudgetLabelTranslationSetRequest, BudgetLabelTranslationDeleteRequest,
                     BudgetTranslationImportRequest, BudgetCategoryTranslationSetResult,
                     BudgetCategoryTranslationDeleteResult, BudgetLabelTranslationSetResult,
                     BudgetLabelTranslationDeleteResult, BudgetTranslationImportResult,
                     BudgetAnalyticsRulePreviewRequest, BudgetAnalyticsRuleSaveRequest,
                     BudgetAnalyticsTransferDecisionRequest, BudgetAnalyticsSpendingLimitSaveRequest,
                     BudgetAnalyticsMortgageSaveRequest, BudgetAnalyticsMortgagePaymentSetRequest,
                     BudgetAnalyticsInboxMarkRequest, BudgetAnalyticsCategories,
                     BudgetAnalyticsCategoryResult, BudgetAnalyticsAnnotationResult,
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
                     BudgetAnalyticsQuarantineBatchRequest, BudgetAnalyticsQuarantineImportKey,
                     BudgetAnalyticsQuarantineDismissResult,
                     BudgetAnalyticsMonthlyBudget, BudgetAnalyticsMonthlyBudgetDailyPoint,
                     BudgetAnalyticsMonthlyBudgetSeriesCategory, BudgetAnalyticsMonthlyBudgetSeriesMonth,
                     BudgetAnalyticsMonthlyBudgetCategoryLabel, BudgetAnalyticsMonthlyBudgetSeries,
                     BudgetAnalyticsMonthlyBudgetCharts)


_BUDGET_MODELS = (BudgetSelectionRequest, BudgetKindRequest, BudgetCreditLimitRequest, BudgetConnectionStateRequest,
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
    ("POST", "/api/budget/analytics/transactions-by-keys"): "admin",
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
    ("GET", "/api/budget/household/access"): "viewer",
    ("GET", "/api/budget/household/report-accounts"): "viewer",
    ("GET", "/api/budget/household/monthly-budget"): "viewer",
    ("GET", "/api/budget/household/overview"): "viewer",
    ("GET", "/api/budget/household/transactions"): "viewer",
    ("GET", "/api/budget/household/categories"): "viewer",
    ("GET", "/api/budget/household/rules"): "viewer",
    ("GET", "/api/budget/household/transfers"): "viewer",
    ("GET", "/api/budget/household/spending-limits"): "viewer",
    ("GET", "/api/budget/household/recurring"): "viewer",
    ("GET", "/api/budget/household/mortgages"): "viewer",
    ("GET", "/api/budget/household/inbox"): "viewer",
    ("GET", "/api/budget/household/quarantine"): "viewer",
    ("GET", "/api/budget/household/monthly-budget-settings"): "viewer",
    ("POST", "/api/budget/analytics/category-create"): "admin",
    ("POST", "/api/budget/analytics/category-update"): "admin",
    ("POST", "/api/budget/analytics/category-translation-set"): "admin",
    ("POST", "/api/budget/analytics/category-translation-delete"): "admin",
    ("POST", "/api/budget/analytics/label-translation-set"): "admin",
    ("POST", "/api/budget/analytics/label-translation-delete"): "admin",
    ("POST", "/api/budget/analytics/translation-import"): "admin",
    ("POST", "/api/budget/analytics/category-colors-spread"): "admin",
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
    ("POST", "/api/budget/analytics/quarantine-dismiss"): "admin",
    ("POST", "/api/budget/analytics/quarantine-dismiss-batch"): "admin",
    ("POST", "/api/budget/receipts/parse-text"): "admin",
    ("POST", "/api/budget/receipts/confirm"): "admin",
    ("POST", "/api/budget/receipts/list"): "admin",
    ("POST", "/api/budget/receipts/search"): "admin",
    ("GET", "/api/budget/receipts/detail"): "admin",
    ("GET", "/api/budget/receipts/group/detail"): "admin",
    ("POST", "/api/budget/receipts/group/candidates"): "admin",
    ("POST", "/api/budget/receipts/group/link"): "admin",
    ("POST", "/api/budget/receipts/group/unlink"): "admin",
    ("GET", "/api/budget/receipts/image"): "admin",
    ("POST", "/api/budget/receipts/delete"): "admin",
    ("POST", "/api/budget/receipts/device-token/pair-code"): "admin",
    ("POST", "/api/budget/receipts/device-token/pair-code-cancel"): "admin",
    ("POST", "/api/budget/receipts/device-token/login"): "public",
    ("POST", "/api/budget/receipts/device-token/redeem"): "public",
    ("POST", "/api/budget/receipts/device-token/list"): "admin",
    ("POST", "/api/budget/receipts/device-token/revoke"): "admin",

    ("POST", "/api/budget/receipts/gmail-grant"): "admin",
    ("POST", "/api/budget/receipts/gmail-revoke"): "admin",
    ("POST", "/api/budget/receipts/gmail-sync"): "admin",
    ("GET", "/api/budget/receipts/gmail-status"): "admin",

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
    ("GET", "/api/auth/features"): "public",
    ("POST", "/api/auth/qr-code"): "viewer",
    ("POST", "/api/auth/qr-redeem"): "public",
    ("POST", "/api/auth/device-token"): "viewer",
    ("GET", "/api/whoami"): "viewer",
    ("GET", "/api/me/capabilities"): "viewer",
    ("GET", "/api/discovery"): "viewer",
    ("GET", _P_AUTOMATIONS): "admin",
    ("POST", _P_AUTOMATIONS): "admin",
    ("POST", "/api/automations/delete"): "admin",
    ("GET", "/api/audit"): "viewer",
    ("GET", "/api/events"): "viewer",
    ("GET", _P_USERS): "admin",
    ("POST", _P_USERS): "admin",
    ("GET", "/api/users/grants"): "admin",
    ("POST", "/api/users/grants"): "admin",
    ("POST", "/api/users/role"): "admin",
    ("POST", "/api/users/disabled"): "admin",
    ("POST", "/api/users/reset-password"): "admin",
    ("POST", "/api/me/password"): "viewer",
    ("GET", "/api/rf433"): "admin",
    ("GET", "/api/db/stats"): "admin",
    ("GET", _P_CALENDARS): "viewer",
    ("GET", _P_CALENDAR_INSTANCES): "viewer",
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
    ("POST", _P_BUDGET_CREDIT_LIMIT): "admin",
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
                       *_USER_MODELS, *_CALENDAR_MODELS, *_BUDGET_MODELS, *_ANALYTICS_MODELS,
                       *_RECEIPT_MODELS, *_HOUSEHOLD_MODELS)],
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
    auth_err = {"content": {_APP_JSON: {"schema": _ref("AuthError")}}}
    auth_body_errors = {
        "408": {"description": "request body read timed out", **auth_err},
        "411": {"description": "Content-Length is required", **auth_err},
        "413": {"description": "request body exceeds the byte limit", **auth_err},
        "415": {"description": "Content-Type must be application/json", **auth_err},
    }
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
                        "429": {"description": "Too many attempts. Shares the device login quota by username and IP.",
                                "headers": _LOGIN_RETRY_HEADERS, **err},
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
            "/api/me/capabilities": {
                "get": {
                    "operationId": "meCapabilities",
                    "summary": "Effective installation and own-Budget capabilities",
                    "description": "Authenticated. Cache-Control: no-store. Delegated responses never identify an owner.",
                    "responses": {
                        "200": {
                            "description": "the current actor's effective capabilities",
                            "headers": {"Cache-Control": {"schema": {"type": "string", "const": "no-store"}}},
                            "content": {_APP_JSON: {"schema": _ref("MeCapabilities")}},
                        }
                    },
                }
            },
            "/api/auth/features": {
                "get": {
                    "operationId": "authFeatures",
                    "summary": "Public authentication contract and available features",
                    "description": "Public and Cache-Control: no-store. Only actually registered features are listed.",
                    "responses": {
                        "200": {
                            "description": "the authentication contract and feature strings",
                            "headers": {"Cache-Control": {"schema": {"type": "string", "const": "no-store"}}},
                            "content": {_APP_JSON: {"schema": _ref("AuthFeatures")}},
                        }
                    },
                }
            },
            "/api/auth/qr-code": {
                "post": {
                    "operationId": "issueQrLoginCode",
                    "summary": "Issue an origin-bound, single-use native login QR payload",
                    "description": "Requires a v2 browser cookie, the configured HTTPS public origin, and a matching Origin header.",
                    "requestBody": {"required": True,
                                    "content": {_APP_JSON: {"schema": _ref("QrCodeRequest")}}},
                    "responses": {
                        **auth_body_errors,
                        "200": {"description": "the QR payload",
                                "content": {_APP_JSON: {"schema": _ref("QrCodeResult")}}},
                        "400": {"description": "invalid JSON (qr_invalid_request), origin mismatch, or an origin "
                                               "over 45 bytes", **auth_err},
                        "401": {"description": "browser session missing or requiring upgrade", **auth_err},
                        "403": {"description": "native bearer is not a browser session", **auth_err},
                        "409": {"description": "browser session differs from the expected actor (budget_actor_changed)",
                                **auth_err},
                        "503": {"description": "HTTPS public origin unavailable or incompatible authorization bridge",
                                **auth_err},
                    },
                },
            },
            "/api/auth/qr-redeem": {
                "post": {
                    "operationId": "redeemQrLoginCode",
                    "summary": "Redeem a single-use QR code for a native session",
                    "description": "Public but HTTPS-origin-bound. Cookie and Authorization credentials are forbidden; no cookie is set.",
                    "requestBody": {"required": True,
                                    "content": {_APP_JSON: {"schema": _ref("QrRedeemRequest")}}},
                    "responses": {
                        **auth_body_errors,
                        "200": {"description": "native session issued",
                                "content": {_APP_JSON: {"schema": _ref("QrRedeemSuccess")}}},
                        "400": {"description": "invalid JSON (qr_invalid_request), ambiguous credentials, or "
                                               "origin mismatch", **auth_err},
                        "401": {"description": "invalid, expired, consumed, or stale code", **auth_err},
                        "429": {"description": "redemption throttled", **auth_err,
                                "headers": {"Retry-After": {"schema": {"type": "string"}}}},
                        "503": {"description": "HTTPS public origin unavailable or incompatible authorization bridge",
                                **auth_err},
                    },
                },
            },
            "/api/auth/device-token": {
                "post": {
                    "operationId": "exchangeNativeSession",
                    "summary": "Exchange a v2 native session for a narrowed receipt/device token",
                    "description": "Allowed on HTTP LAN. prior_token rotates or migrates a credential without widening its permission ceiling.",
                    "requestBody": {"required": True,
                                    "content": {_APP_JSON: {"schema": _ref("AuthDeviceTokenRequest")}}},
                    "responses": {
                        **auth_body_errors,
                        "200": {"description": "device credential issued",
                                "content": {_APP_JSON: {"schema": _ref("AuthDeviceTokenResult")}}},
                        "400": {"description": "invalid device name or request", **auth_err},
                        "401": {"description": "missing, stale, or non-native session (native_session_required)",
                                **auth_err},
                        "403": {"description": "no eligible live receipt/device target", **auth_err},
                        "404": {"description": "Budget or receipt support is disabled", **auth_err},
                        "409": {"description": "active slot limit or rejected prior_token proof", **auth_err},
                        "503": {"description": "incompatible authorization bridge or unavailable device service",
                                **auth_err},
                    },
                },
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
            "/api/users/grants": {
                "get": {
                    "operationId": "getUserGrants",
                    "summary": "Read the permission catalog and one account's direct/effective grants",
                    "description": "Requires account management. Delegated target rules and implication closures are resolved server-side.",
                    "parameters": [{"name": "username", "in": "query", "required": True,
                                    "schema": {"type": "string", "minLength": 1}}],
                    "responses": {
                        "200": {"description": "catalog and grants",
                                "content": {_APP_JSON: {"schema": _ref("UserGrantsResult")}}},
                        "400": {"description": "missing username", **err},
                        "404": {"description": _NO_SUCH_USER, **err},
                        "409": {"description": "user management requires the SQLite backend", **err},
                    },
                },
                "post": {
                    "operationId": "updateUserGrants",
                    "summary": "Replace direct grants and activate the custom catalog on the first edit",
                    "description": "Requires a v2 browser session and Origin. The server preserves unchanged grant IDs and protects administration.",
                    "requestBody": {"required": True,
                                    "content": {_APP_JSON: {"schema": _ref("UpdateUserGrantsRequest")}}},
                    "responses": {
                        "200": {"description": "grants saved",
                                "content": {_APP_JSON: {"schema": _ref("UpdateUserGrantsResult")}}},
                        "400": {"description": "invalid, duplicate, or out-of-scope grants", **err},
                        "403": {"description": "cannot remove your own administration grant", **err},
                        "404": {"description": _NO_SUCH_USER, **err},
                        "409": {"description": "last administrator, target conflict, or SQLite backend inactive", **err},
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
            _P_CALENDAR_INSTANCES: {
                "get": {
                    "operationId": "getCalendarInstances",
                    "summary": "Concrete calendar occurrences in a half-open date window",
                    "description": (
                        "Requires viewer. Expands S1-S7 allowlisted RRULE shapes and RDATEs, applies moved or "
                        "modified exceptions, and omits EXDATE and every cancelled occurrence. Events outside "
                        "the S1-S7 allowlist (including SECONDLY/MINUTELY/HOURLY rules, multiple RRULEs, >500 "
                        "RDATE/EXDATEs, or >31-day recurring durations) or exceeding 5000 occurrences in the "
                        "padded window return only DTSTART with truncated=true when within budget. Requests "
                        "also enforce shared SQL and expansion budgets (5000 events, 16 MiB stored JSON, "
                        "600000 replay day-check units, and 50000 candidates): events over the SQL or "
                        "pre-parse explicit-date/override budget are omitted without parsing and set "
                        "truncated=true, and exhausting the candidate budget stops expansion with "
                        "truncated=true. The window is [from, to) at local midnights in tz (default UTC), "
                        "from is inclusive, to is exclusive, and its duration cannot exceed 92 days. Instances "
                        "are sorted by start and capped at 2000. Disabled synchronization returns 200 with "
                        "enabled=false and empty groups and instances."
                    ),
                    "parameters": [
                        {"name": "from", "in": "query", "required": True, "schema": {"type": "string", "format": "date"},
                         "description": "Inclusive window start date (YYYY-MM-DD)."},
                        {"name": "to", "in": "query", "required": True, "schema": {"type": "string", "format": "date"},
                         "description": "Exclusive window end date (YYYY-MM-DD), matching exclusive CalendarDate end semantics."},
                        {"name": "tz", "in": "query", "schema": {"type": "string", "default": "UTC"},
                         "description": "IANA time zone used to interpret from/to as local midnights. Defaults to UTC; aliases resolve through the server's canonical timezone helpers. Host keys such as localtime, posixrules, posix/* and right/* are rejected."},
                        {"name": "group", "in": "query", "schema": {"type": "string"},
                         "description": "Selected configured group; defaults to the first group."},
                    ],
                    "responses": {
                        "200": {"description": "expanded calendar instances", "content": {_APP_JSON: {"schema": _ref("CalendarInstances")}}},
                        "400": {"description": "invalid window (invalid_calendar_event) or unknown group (unknown_calendar_group)", "content": {_APP_JSON: {"schema": _ref("CalendarError")}}},
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
    # Keep the old role extension as a compatibility projection; catalog requirements are authoritative.
    from . import web
    for path, ops in doc["paths"].items():
        for method, op in ops.items():
            upper = method.upper()
            op["x-required-role"] = _PATH_ROLES[(upper, path)]
            op["x-required-permission"] = (
                "public" if web._is_public_route(upper, path) else web._required_permission(upper, path)
            )
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
        (_P_BUDGET_CREDIT_LIMIT, "setBudgetAccountCreditLimit", "Set or clear one owned card account's credit limit", "BudgetCreditLimitRequest", "BudgetAccountSaved"),
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
    paths.update(_receipt_paths())
    paths.update(_gmail_receipt_paths())
    paths.update(_household_paths())
    return paths


def _receipt_error(description):
    return {"description": description, "content": {_APP_JSON: {"schema": _ref("BudgetError")}}}


_RECEIPT_AMBIGUITY = "Cookie plus Authorization returns 400 budget_auth_ambiguous."
_DEVICE_UNAUTHENTICATED = "A missing or invalid credential, device_token_expired, or device_token_revoked."
_BUDGET_BROWSER_AUTH = (
    "Browser budget routes require the hestia_session cookie and configured same-origin Origin. "
    "A login/session Authorization: Bearer from /api/login returns 403 budget_browser_session_required on every "
    "browser budget route, including GETs. Device login rejects any Cookie or Authorization header. A receipts-scoped "
    "device token obtained through browser pairing or password login is accepted only on receipts parse-text, confirm, "
    "list, search, delete, detail and image, plus device-token/revoke for itself. Household access is browser-only."
)


def _receipt_paths():
    prefix = "/api/budget/receipts/"
    contract_version = {
        "name": "X-Hestia-Contract-Version", "in": "header", "required": False,
        "description": "Request native Gmail receipt values starting at 0.40.0. Earlier versions or an omitted header "
                       "receive legacy receipt statuses and source kinds.",
        "schema": {"type": "string", "pattern": r"^0\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})$"},
    }
    # 409 is per route. 411 and 415 are the shared JSON framing checks. 413 is the body cap,
    # and parse-text also uses 413 when the owner already has 20 active drafts.
    writes = (
        ("parse-text", "parseBudgetReceiptText", "BudgetReceiptParseTextRequest", "BudgetReceiptParseResult",
         "Paste Live Text, redact PAN data and create a 48-hour draft; JSON body cap 48 KiB.",
         "Requires budget.receipts.add. Bank candidates are available with budget.receipts.match.",
         "SQLite users backend is required (budget_requires_sqlite_users).",
         "JSON body exceeds 49152 bytes, or the owner already has 20 active drafts (budget_receipt_quota_exceeded)."),
        ("confirm", "confirmBudgetReceipt", "BudgetReceiptConfirmRequest", "BudgetReceiptConfirmResult",
         "Validate exact line and total identities, review an optional bank link; JSON body cap 128 KiB.",
         "With budget.receipts.add, a caller can finalize their own draft. Other drafts and saved receipts "
         "require budget.receipts.edit. Changing a bank link requires budget.receipts.match.",
         "Stale expected_version (budget_receipt_conflict), the bank transaction is already linked "
         "(budget_receipt_tx_already_linked), or the candidate token no longer matches the owner, transaction, "
         "dates and merchant evidence shown at preview (budget_receipt_stale_source). Repeating confirmation of "
         "one's own draft without budget.receipts.edit returns already_submitted. A missing SQLite users backend "
         "also returns 409 (budget_requires_sqlite_users).",
         "JSON body exceeds 131072 bytes."),
        ("list", "listBudgetReceipts", "BudgetReceiptListRequest", "BudgetReceiptListResult",
         "Keyset-page receipts on the target ledger using filters in the JSON body.",
         "Requires budget.receipts.view.",
         "SQLite users backend is required (budget_requires_sqlite_users).",
         "JSON body exceeds 16384 bytes."),
        ("search", "searchBudgetReceipts", "BudgetReceiptSearchRequest", "BudgetReceiptListResult",
         "Search receipts on the target ledger with the query in the JSON body.",
         "Requires budget.receipts.view.",
         "SQLite users backend is required (budget_requires_sqlite_users).",
         "JSON body exceeds 16384 bytes."),
        ("delete", "deleteBudgetReceipt", "BudgetReceiptDeleteRequest", "BudgetReceiptDeleteResult",
         "Delete receipt lines before their owned receipt using expected_version CAS.",
         "With budget.receipts.add, a caller can discard their own draft. Other drafts and saved receipts "
         "require budget.receipts.edit. Deleting a linked receipt also requires budget.receipts.match.",
         "Stale expected_version (budget_receipt_conflict). A missing SQLite users backend also returns 409 "
         "(budget_requires_sqlite_users).",
         "JSON body exceeds 16384 bytes."),
        ("group/candidates", "listBudgetReceiptGroupCandidates", "BudgetReceiptGroupCandidatesRequest",
         "BudgetReceiptGroupCandidatesResult",
         "Compute bounded many-to-one bank transaction and peer receipt group candidates for an owned receipt.",
         "Requires budget.receipts.match. Accepts browser sessions or receipts device tokens. Requires contract "
         "version 0.59.0 or later.",
         "SQLite users backend is required (budget_requires_sqlite_users).",
         "JSON body exceeds 16384 bytes."),
        ("group/link", "linkBudgetReceiptGroup", "BudgetReceiptGroupLinkRequest", "BudgetReceiptGroupLinkResult",
         "Atomically link 2 to 8 confirmed same-currency receipts (plus optional standalone gateway evidence) to one bank transaction.",
         "Browser session, budget.receipts.edit and budget.receipts.match required. Requires contract version 0.59.0 or later.",
         "Stale expected_version or evidence_expected_version (budget_receipt_conflict), receipt or transaction "
         "already linked or reserved in a transfer leg (budget_receipt_tx_already_linked), or stale candidate "
         "token / source mismatch (budget_receipt_stale_source). A missing SQLite users backend also returns 409 "
         "(budget_requires_sqlite_users).",
         "JSON body exceeds 16384 bytes."),
        ("group/unlink", "unlinkBudgetReceiptGroup", "BudgetReceiptGroupUnlinkRequest", "BudgetReceiptGroupUnlinkResult",
         "Atomically unlink a receipt group and restore all members and optional gateway evidence to confirmed unlinked status.",
         "Browser session, budget.receipts.edit and budget.receipts.match required. Requires contract version 0.59.0 or later.",
         "Stale expected_version (budget_receipt_conflict). A missing SQLite users backend also returns 409 "
         "(budget_requires_sqlite_users).",
         "JSON body exceeds 16384 bytes."),
    )
    paths = {}
    for name, operation, request, response, summary, access, conflict, too_large in writes:
        responses = _budget_responses(response)
        responses["401"] = _receipt_error(_DEVICE_UNAUTHENTICATED)
        responses["409"] = _receipt_error(conflict)
        responses["411"] = _receipt_error("Content-Length is required.")
        responses["413"] = _receipt_error(too_large)
        responses["415"] = _receipt_error("Content-Type must be application/json.")
        if name.startswith("group/"):
            responses["404"] = _receipt_error("Unknown, disabled or foreign resource, or contract version below 0.59.0.")
        paths[prefix + name] = {"post": {
            "operationId": operation, "summary": summary,
            "description": (
                "Default-off with HESTIA_BUDGET_RECEIPTS_ENABLED. Requires a SQLite users backend and a "
                "browser cookie with exact Origin or a receipts-capable device token. Permissions apply "
                f"to the target ledger and may be delegated. {access} {_RECEIPT_AMBIGUITY} "
                "411 means Content-Length is required and 415 means Content-Type must be application/json."
            ),
            "requestBody": {"required": True, "content": {_APP_JSON: {"schema": _ref(request)}}},
            "responses": responses,
        }}
        if name == "parse-text":
            paths[prefix + name]["post"]["responses"]["429"] = _receipt_error(
                "A receipts device token is limited to 30 parse-text requests per minute (Retry-After). "
                "Browser cookie requests are not limited here.")

        if name.startswith("group/"):
            paths[prefix + name]["post"]["parameters"] = [
                {**contract_version, "required": True, "description": "Receipt groups are available from 0.59.0."}
            ]
        elif name != "delete":
            paths[prefix + name]["post"]["parameters"] = [contract_version]
    receipt_query = [_budget_query("receipt_id", required=True,
                                   schema={"type": "string",
                                           "pattern": r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$"},
                                   description="Opaque owned receipt UUID.")]
    for name, operation, response, summary in (
        ("detail", "getBudgetReceiptDetail", "BudgetReceiptDetailResult",
         "Read one owned receipt and its structured lines."),
        ("image", "getBudgetReceiptImage", None,
         "Read one owned WebP image; v1a has no stored images and therefore returns 404."),
    ):
        responses = _budget_responses(response) if response else _budget_responses("BudgetError")
        responses["401"] = _receipt_error(_DEVICE_UNAUTHENTICATED)
        if response is None:
            responses["200"] = {"description": "Owner-private WebP bytes (available from v1b).",
                                "content": {"image/webp": {"schema": {"type": "string", "format": "binary"}}}}
        responses["409"] = _receipt_error(
            "SQLite users backend is required (budget_requires_sqlite_users). This read does not accept a JSON body.")
        paths[prefix + name] = {"get": {
            "operationId": operation, "summary": summary,
            "description": ("Default-off owner-private read using a browser cookie or receipts device token. "
                            "Unknown and foreign receipt IDs both return 404. " + _RECEIPT_AMBIGUITY),
            "parameters": [*receipt_query, contract_version] if name == "detail" else receipt_query,
            "responses": responses,
        }}
    group_responses = _budget_responses("BudgetReceiptGroupDetailResult")
    group_responses["404"] = _receipt_error("Unknown or foreign group, or contract version below 0.59.0.")
    group_responses["409"] = _receipt_error("SQLite users backend is required (budget_requires_sqlite_users).")
    paths[prefix + "group/detail"] = {"get": {
        "operationId": "getBudgetReceiptGroupDetail",
        "summary": "Read an owned receipt link group, its member receipts and optional gateway evidence.",
        "description": "Browser session and budget.receipts.view required. Transaction account and reference digest "
                       "also require budget.receipts.match. Requires contract version 0.59.0 or later.",
        "parameters": [
            _budget_query("group_id", required=True,
                          schema={"type": "string", "pattern": r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$"},
                          description="Opaque owned receipt group UUID."),
            {**contract_version, "required": True, "description": "Receipt group detail is available from 0.59.0."},
        ],
        "responses": group_responses,
    }}
    paths.update(_device_token_paths())
    return paths


def _device_token_paths():
    """Browser pairing management and public device credential exchanges."""
    prefix = "/api/budget/receipts/device-token/"
    framing = {
        "411": _receipt_error("Content-Length is required."),
        "413": _receipt_error("JSON body exceeds 4096 bytes."),
        "415": _receipt_error("Content-Type must be application/json."),
    }
    browser = _budget_responses("BudgetError")
    browser.update(framing)
    pair = dict(browser)
    pair["200"] = {"description": "One-time pairing code. The raw code is not stored.",
                   "content": {_APP_JSON: {"schema": _ref("BudgetReceiptDevicePairResult")}}}
    pair["409"] = _receipt_error(
        "The owner already has 3 active pairing codes (device_pair_limit), or SQLite users are required.")
    listed = dict(browser)
    listed["200"] = {"description": "Unrevoked device tokens for the owner, including expired ones.",
                     "content": {_APP_JSON: {"schema": {"type": "array", "items": _ref("BudgetReceiptDeviceToken")}}}}
    revoke = dict(browser)
    revoke["401"] = _receipt_error(_DEVICE_UNAUTHENTICATED)
    revoke["200"] = {"description": "Idempotent. An unknown id for this owner is still ok.",
                     "content": {_APP_JSON: {"schema": _ref("BudgetReceiptDeviceRevokeResult")}}}
    cancel = dict(browser)
    cancel["200"] = {"description": "Idempotently cancel an unused code belonging to this owner.",
                     "content": {_APP_JSON: {"schema": _ref("BudgetReceiptDeviceRevokeResult")}}}
    redeem_responses = {
        "200": {"description": "One-time receipt device token with a sliding 90-day TTL and a 180-day hard cap.",
                "content": {_APP_JSON: {"schema": _ref("BudgetReceiptDeviceRedeemResult")}}},
        "400": _receipt_error("Generic device_token_rejected for invalid framing, input or pairing state. "
                              + _RECEIPT_AMBIGUITY),
        "403": {"description": "A browser session below the admin role floor is forbidden."},
        "404": _receipt_error("Receipts are disabled."),
        "429": _receipt_error("Rate limited per IP and globally (Retry-After). The error is device_token_rejected."),
        "500": _receipt_error("Internal error."),
        "503": _receipt_error("Budget configuration unavailable."),
    }
    login = {
        **framing,
        "200": redeem_responses["200"],
        "400": _receipt_error("Invalid JSON request or budget_auth_ambiguous if Cookie or Authorization is present."),
        "401": _receipt_error("Generic device_token_rejected for invalid credentials or an ineligible account."),
        "404": _receipt_error("Budget HTTP or receipts are disabled."),
        "409": _receipt_error("The owner has 5 active tokens and this is a new device name "
                              "(device_token_limit_reached). Same-name rotation is still allowed."),
        "408": _receipt_error("Request body read timed out."),
        "429": {**_receipt_error("Too many password attempts (device_token_rate_limited)."),
                "headers": _LOGIN_RETRY_HEADERS},
        "500": redeem_responses["500"],
        "503": redeem_responses["503"],
    }
    note = ("Default-off with HESTIA_BUDGET_RECEIPTS_ENABLED. " + _RECEIPT_AMBIGUITY + " ")
    return {
        prefix + "pair-code": {"post": {
            "operationId": "pairBudgetReceiptDevice",
            "summary": "Issue one 5-minute pairing code for the signed-in admin owner.",
            "description": note + " Browser cookie and exact Origin. At most 3 active codes. " + _BUDGET_BROWSER_AUTH,
            "requestBody": {"required": True, "content": {_APP_JSON: {
                "schema": _ref("BudgetReceiptDeviceBrowserRequest")}}},
            "responses": pair,
        }},
        prefix + "pair-code-cancel": {"post": {
            "operationId": "cancelBudgetReceiptDevicePairCode",
            "summary": "Cancel an unused pairing code by its server-issued ID.",
            "description": note + " Browser owner cookie and exact Origin. A used, expired, or foreign ID is a no-op.",
            "requestBody": {"required": True, "content": {_APP_JSON: {
                "schema": _ref("BudgetReceiptDeviceCancelRequest")}}},
            "responses": cancel,
        }},
        prefix + "login": {"post": {
            "operationId": "loginBudgetReceiptDevice",
            "summary": "Issue a receipts-scoped device token using an enabled budget owner's password.",
            "description": ("Requires budget HTTP and receipts. Send uncompressed application/json (at most 4096 bytes) "
                            "without Cookie or Authorization. Shares the /api/login quota of 10 attempts per minute "
                            "per username and IPv4 address or IPv6 /64. An unrevoked token with the same device name "
                            "is rotated. At five active tokens, a new device name is rejected with 409 "
                            "device_token_limit_reached; same-name rotation remains allowed."),
            "requestBody": {"required": True, "content": {_APP_JSON: {
                "schema": _ref("BudgetReceiptDeviceLoginRequest")}}},
            "responses": login,
        }},
        prefix + "redeem": {"post": {
            "operationId": "redeemBudgetReceiptDevice",
            "summary": "Exchange a pairing code for a receipts-scoped bearer, once.",
            "description": (note + "Send an application/json body of at most 4096 bytes without cookie or "
                            "Authorization. Redemption has per-IP backoff and global throttling. At five active "
                            "tokens, issuance revokes the least recently used token."),
            "requestBody": {"required": True, "content": {_APP_JSON: {
                "schema": _ref("BudgetReceiptDeviceRedeemRequest")}}},
            "responses": redeem_responses,
        }},
        prefix + "list": {"post": {
            "operationId": "listBudgetReceiptDevices",
            "summary": "List the owner's unrevoked receipts device tokens.",
            "description": note + " Browser cookie and exact Origin.",
            "requestBody": {"required": True, "content": {_APP_JSON: {
                "schema": _ref("BudgetReceiptDeviceBrowserRequest")}}},
            "responses": listed,
        }},
        prefix + "revoke": {"post": {
            "operationId": "revokeBudgetReceiptDevice",
            "summary": "Revoke one receipts device token.",
            "description": note + " The browser owner may revoke any of their tokens. "
                           "A device token may revoke only itself; any other id is 403.",
            "requestBody": {"required": True, "content": {_APP_JSON: {
                "schema": _ref("BudgetReceiptDeviceRevokeRequest")}}},
            "responses": revoke,
        }},
    }

def _gmail_receipt_paths():
    prefix = "/api/budget/receipts/gmail-"
    description = ("Default-off with HESTIA_BUDGET_GMAIL_IMPORT_ENABLED. Requires an authenticated SQLite admin "
                   "and a same-origin browser session.")
    paths = {}
    for name, operation, summary, request_model, response_model, conflict in (
        ("grant", "setBudgetReceiptGmailGrant", "Enable or disable the local Gmail grant",
         "BudgetReceiptGmailGrantRequest", "BudgetReceiptGmailGrantResult",
         "Version conflict, missing authoritative SQLite users backend, or Gmail busy (budget_receipt_gmail_busy)."),
        ("revoke", "revokeBudgetReceiptGmailGrant", "Revoke the local grant and purge unconfirmed imports",
         "BudgetReceiptGmailRevokeRequest", "BudgetReceiptGmailGrantResult",
         "Version conflict, missing authoritative SQLite users backend, or Gmail busy (budget_receipt_gmail_busy)."),
        ("sync", "syncBudgetReceiptGmail", "Schedule one Gmail import for the background poller",
         "BudgetReceiptGmailSyncRequest", "BudgetReceiptGmailSyncResult",
         "Grant is inactive or authoritative SQLite users are unavailable."),
    ):
        responses = _budget_responses(response_model)
        if name == "sync":
            accepted = responses.pop("200")
            accepted["description"] = ("Accepted for the background poller. Kicks share queued or running work. "
                                       "A full-resync request upgrades that work.")
            responses["202"] = accepted
        else:
            responses["200"]["description"] = summary
        responses["409"] = _receipt_error(conflict)
        responses["411"] = _receipt_error("Content-Length is required.")
        responses["413"] = _receipt_error("JSON body exceeds 4096 bytes.")
        responses["415"] = _receipt_error("Content-Type must be application/json.")
        if name in ("grant", "sync"):
            responses["429"] = _receipt_error("Gmail quota, persisted backoff, or the sync cooldown applies.")
        paths[prefix + name] = {"post": {
            "operationId": operation, "summary": summary,
            "description": description + " Send exact Origin and a JSON body of at most 4 KiB.",
            "requestBody": {"required": True, "content": {_APP_JSON: {"schema": _ref(request_model)}}},
            "responses": responses,
        }}
    responses = _budget_responses("BudgetReceiptGmailStatus")
    responses["409"] = _receipt_error("SQLite users backend is required (budget_requires_sqlite_users).")
    paths[prefix + "status"] = {"get": {
        "operationId": "getBudgetReceiptGmailStatus", "summary": "Read safe Gmail import state",
        "description": description + " Use the returned version for grant changes. Manual Google revocation can "
                       "affect Calendar Sync.",
        "parameters": [{
            "name": "X-Hestia-Contract-Version", "in": "header", "required": False,
            "description": "Request Gmail PDF diagnostics starting at 0.56.0. Earlier versions or an omitted header "
                           "receive those counts under other.",
            "schema": {"type": "string", "pattern": r"^0\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})$"},
        }],
        "responses": responses,
    }}
    return paths


def _household_grant_header():
    return {"name": "X-Budget-Grant", "in": "header", "required": True,
            "schema": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "description": "Exactly one household grant id, as returned by discovery. A missing, duplicate or "
                           "different value is rejected."}


def _household_responses(model):
    responses = _analytics_responses(model)
    responses["200"]["description"] = (
        "Household view. No bank request. The body is sent only after a fresh grant check commits in one "
        "immediate transaction. A successful data read may write one sanitised analytics-audit row naming "
        "the member, owner and view, at most once per 60 seconds. Discovery writes no audit row. A denial "
        "writes nothing.")
    responses["403"]["description"] = ("The member must be an enabled viewer or operator with a grant whose owner "
                                       "is an enabled admin. Data reads also require exactly one matching X-Budget-Grant. "
                                       "Middleware may return an empty body.")
    responses["413"]["description"] = ("Household response exceeded 4 MiB of UTF-8 JSON, or a monthly-budget "
                                       "processing cap was hit. No partial body is returned.")
    return responses


def _household_paths():
    """P3 household reads. Existing owner routes stay admin. Settings reads the P1 bootstrap config only."""
    prefix = "/api/budget/household/"
    period = _budget_query("period", required=True, schema={"type": "string", "pattern": r"^[0-9]{4}(?:-[0-9]{2})?$"},
                           description="Inclusive calendar month or year, 1900 through 9999.")
    currency = _budget_query("currency", schema={"type": "string", "pattern": "^[A-Z]{3}$"})
    month = _budget_query("period", required=True, schema={"type": "string", "pattern": r"^[0-9]{4}-[0-9]{2}$"},
                          description="Inclusive calendar month YYYY-MM.")
    page = [_budget_query("limit", schema={"type": "integer", "minimum": 1, "maximum": 200, "default": 50}),
            _budget_query("cursor", schema={"type": "string", "minLength": 1, "maxLength": 8192},
                          description="Opaque cursor bound to the member, owner, grant, route and safe rows.")]
    month_optional = _budget_query(
        "period", schema={"type": "string", "pattern": r"^[0-9]{4}-[0-9]{2}$"},
        description="Optional calendar month YYYY-MM through the current Europe/Warsaw month. Defaults to that month.")
    month_currency = _budget_query(
        "currency", schema={"type": "string", "pattern": "^[A-Z]{3}$", "default": "PLN"},
        description="Optional uppercase 3-letter ISO currency code. Defaults to PLN.")
    alias = _budget_query("account_id", description="Grant-scoped alias of a selected account.")
    category = _budget_query("category_id", description="Grant-scoped category alias. The owner token uncategorised is rejected.")
    kind = _budget_query("kind", schema={"type": "string", "enum":
        ["expense", "income", "refund", "excluded", "unclassified_credit", "transfer"]})
    status = _budget_query("status", schema={"type": "string", "enum":
        ["suggested", "accepted", "review", "boundary", "rejected", "undone"], "default": "suggested"})
    reads = (
        ("access", "getBudgetHouseholdAccess", "Discover the caller's household grant id", "BudgetHouseholdAccess", []),
        ("report-accounts", "getBudgetHouseholdReportAccounts", "Selected accounts as grant-scoped aliases and ordinals",
         "BudgetHouseholdAccounts", []),
        ("monthly-budget", "getBudgetHouseholdMonthlyBudget", "Household monthly budget and series",
         "BudgetHouseholdMonthlyBudget", [month_optional, month_currency]),
        ("overview", "getBudgetHouseholdOverview", "Household selected-account totals",
         "BudgetHouseholdOverview", [period, currency]),
        ("transactions", "getBudgetHouseholdTransactions", "Household transactions without bank text",
         "BudgetHouseholdTransactions", [period, currency, kind, alias, category, *page]),
        ("categories", "getBudgetHouseholdCategories", "Household category aliases and safe labels",
         "BudgetHouseholdCategories", []),
        ("rules", "getBudgetHouseholdRules", "Household rules without literal operands",
         "BudgetHouseholdRules", []),
        ("transfers", "getBudgetHouseholdTransfers", "Household transfer pairs whose both legs are selected",
         "BudgetHouseholdTransfers", [period, currency, status, *page]),
        ("spending-limits", "getBudgetHouseholdSpendingLimits", "Enabled household spending limits",
         "BudgetHouseholdSpendingLimits", [month, currency]),
        ("recurring", "getBudgetHouseholdRecurring", "Household recurring hints with generic ordinals",
         "BudgetHouseholdRecurring", [currency]),
        ("mortgages", "getBudgetHouseholdMortgages", "Household mortgage schedules without labels",
         "BudgetHouseholdMortgages", []),
        ("inbox", "getBudgetHouseholdInbox", "Household mortgage and spending-limit inbox",
         "BudgetHouseholdInbox", []),
        ("quarantine", "getBudgetHouseholdQuarantine", "Household quarantine counts over selected accounts",
         "BudgetHouseholdQuarantine", []),
        ("monthly-budget-settings", "getBudgetHouseholdMonthlyBudgetSettings",
         "Bootstrap monthly-budget settings with grant-scoped category ids",
         "BudgetHouseholdSettings", []),
    )
    paths = {}
    for name, operation, summary, model, parameters in reads:
        entry = {"operationId": operation, "summary": summary,
                 "description": "Requires an authenticated, enabled viewer or operator, both budget flags, SQLite "
                                "users and a grant whose owner is an enabled admin. The grant header is 64 lowercase "
                                "hex characters. Reads the same owner projection through a separate allowlist. No bank "
                                "request. Data reads may write one sanitised internal analytics-audit row for the "
                                "member, owner and view name, at most once per 60 seconds, in the same immediate "
                                "transaction as the fresh grant check. Household audit rows older than 180 days are "
                                "pruned on that write. GET /access returns only the member's grant id and writes no "
                                "audit row. Failures write nothing. The row is not visible on /api/audit or the SSE "
                                "stream. GET /monthly-budget-settings reads the P1 bootstrap config only.",
                 "parameters": [] if name == "access" else [_household_grant_header(), *parameters],
                 "responses": _household_responses(model)}
        paths[prefix + name] = {"get": entry}
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
    tx_contract_version = {
        "name": "X-Hestia-Contract-Version", "in": "header", "required": False,
        "description": "Negotiate linked receipt summaries on transaction rows starting at 0.60.0. "
                       "Earlier versions or an omitted header omit the receipt key entirely.",
        "schema": {"type": "string", "pattern": r"^0\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})$"},
    }
    quarantine_contract_version = {
        **tx_contract_version,
        "description": "Negotiate import_key on offline import quarantine items starting at 0.63.0. "
                       "Earlier versions or an omitted header omit the key.",
    }
    reads = (
        ("overview", "getBudgetAnalyticsOverview", "BudgetAnalyticsOverview", [period, currency]),
        ("transactions", "getBudgetAnalyticsTransactions", "BudgetAnalyticsTransactions",
         [period, currency, _budget_query("category_id", description="Category ID, or uncategorised for no category."),
          _budget_query("kind"), _budget_query("query", schema={"type": "string", "minLength": 1, "maxLength": 256}),
          *page, tx_contract_version]),
        ("categories", "getBudgetAnalyticsCategories", "BudgetAnalyticsCategories", []),
        ("rules", "getBudgetAnalyticsRules", "BudgetAnalyticsRules", []),
        ("transfers", "getBudgetAnalyticsTransfers", "BudgetAnalyticsTransfers",
         [period, currency, _budget_query("status", schema={"type": "string", "enum":
             ["suggested", "accepted", "review", "boundary", "rejected", "undone"], "default": "suggested"}), *page]),
        ("recurring", "getBudgetAnalyticsRecurring", "BudgetAnalyticsRecurring", [currency]),
        ("spending-limits", "getBudgetAnalyticsSpendingLimits", "BudgetAnalyticsSpendingLimits", [month, currency]),
        ("mortgages", "getBudgetAnalyticsMortgages", "BudgetAnalyticsMortgages", []),
        ("inbox", "getBudgetAnalyticsInbox", "BudgetAnalyticsInbox", [{
            "name": "X-Hestia-Contract-Version", "in": "header", "required": False,
            "description": "Negotiate owner-private appointment_slot and appointment_attention inbox items "
                           "starting at 0.58.0. Earlier versions or an omitted header receive only legacy inbox kinds.",
            "schema": {"type": "string", "pattern": r"^0\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})$"},
        }]),
        ("quarantine", "getBudgetAnalyticsQuarantine", "BudgetAnalyticsQuarantine",
         [_budget_query("account_id", description="Optional owned local account ID filter."), quarantine_contract_version]),
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
            "suppress duplicate estimates. When HESTIA_BUDGET_FORECAST_CONFIG names categories, each month also "
            "includes a category_baseline estimate (confidence estimated, schedule_id null, label is the category "
            "name): the median or trimmed mean of net spending over prior complete months inside each account's "
            "coverage window, including months with no spend. The current month subtracts net spending already "
            "booked, floored at zero. A configured category already represented by an active recurring hint or an "
            "enabled mortgage schedule is omitted from the baselines and listed in summary.baseline_conflicts "
            "(category id and reason only, no amounts). Every other baseline, mortgage and recurring item is still "
            "computed. An optional currency filter scopes commitments, deduplications and their summary counts. "
            "Snapshot metadata and account freshness cover all selected accounts. The estimate excludes spending "
            "limits and makes no bank requests or stored changes."
        )),
        "monthly-budget": ("Read the owner-private monthly budget dashboard", (
            "Pure read-only single-snapshot monthly budget dashboard evaluated in Europe/Warsaw from one UTC instant. "
            "Computes booked configured start budget, adjusted category spending and limits, rent/mortgage pass-through, "
            "unresolved transfer and repayment candidate provisional flags, previous-month expected income estimates "
            "(including cross-account salary matching), and spending pace. Legacy card_exposure stays unavailable "
            "for 0.47.0 compatibility; additive card_debt computes credit card debt as max(0, credit_limit - ITAV) "
            "in the budget currency from owner-set limits and fresh ITAV balances. "
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
               ">200 active currency-filtered hints, >64 category baselines, >4,368 projected items, or >4 MiB UTF-8 "
               "JSON payload); zero partial totals returned.",
        "500": "Stored mortgage schedule, ledger, or metadata integrity check failed (`budget_analytics_data_error`), "
               "including stored-data helper errors mapped to 500 after forecast query validation, or unexpected "
               "internal failure (`budget_internal_error`).",
        "503": "Analytics storage or budget configuration unavailable (`budget_analytics_unavailable` or "
               "`budget_unavailable`). budget_forecast_config_conflict (0.31–0.32) wycofany w 0.33; "
               "konflikt to teraz 200 + summary.baseline_conflicts.",
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
        ("category-translation-set", "setBudgetCategoryTranslation", "BudgetCategoryTranslationSetRequest", "BudgetCategoryTranslationSetResult"),
        ("category-translation-delete", "deleteBudgetCategoryTranslation", "BudgetCategoryTranslationDeleteRequest", "BudgetCategoryTranslationDeleteResult"),
        ("label-translation-set", "setBudgetLabelTranslation", "BudgetLabelTranslationSetRequest", "BudgetLabelTranslationSetResult"),
        ("label-translation-delete", "deleteBudgetLabelTranslation", "BudgetLabelTranslationDeleteRequest", "BudgetLabelTranslationDeleteResult"),
        ("translation-import", "importBudgetTranslations", "BudgetTranslationImportRequest", "BudgetTranslationImportResult"),
        ("category-colors-spread", "spreadBudgetAnalyticsCategoryColors", "BudgetAnalyticsCategoryColorSpreadRequest", "BudgetAnalyticsCategoryColorSpreadResult"),
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
    paths[prefix + "inbox-mark"]["post"]["parameters"] = [{
        "name": "X-Hestia-Contract-Version", "in": "header", "required": False,
        "description": "Negotiate owner-private appointment_slot and appointment_attention inbox mark keys "
                       "starting at 0.58.0.",
        "schema": {"type": "string", "pattern": r"^0\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})$"},
    }]
    paths[prefix + "inbox-mark"]["post"]["summary"] = "Mark or unmark owner-private inbox alerts (atomic anti-ABA CAS)"
    paths[prefix + "inbox-mark"]["post"]["description"] = (
        "Requires the exact admin owner, browser cookie and configured HTTPS Origin. Atomically marks or unmarks "
        "1..100 deduplicated alert keys under anti-ABA reads_version CAS (409 budget_analytics_conflict if stale; "
        "409 budget_analytics_source_changed if any unmarked key is inactive on read=true; 413 budget_analytics_limit "
        "at the 500-marker cap on create only). No bank request is made."
    )
    paths[prefix + "transactions-by-keys"] = {"post": {
        "operationId": "listBudgetAnalyticsTransactionsByKeys",
        "summary": "Read owner-private transactions matching exact row keys (pure read-only)",
        "description": "Requires the exact admin owner, browser cookie and configured HTTPS Origin. "
                       "Returns the same paginated BudgetAnalyticsTransactions shape as GET transactions for "
                       "1..500 exact <account_id>/<entry_reference> keys in the JSON body (<= 1 MiB) regardless of "
                       "period, preventing oversized GET query strings and key leakage in access logs. "
                       "Makes no bank requests and no database writes.",
        "parameters": [tx_contract_version],
        "requestBody": {"required": True, "content": {_APP_JSON: {"schema": _ref("BudgetAnalyticsTransactionsByKeysRequest")}}},
        "responses": _analytics_responses("BudgetAnalyticsTransactions"),
    }}
    for path, operation, request, target in (
        ("quarantine-dismiss", "dismissBudgetAnalyticsQuarantineItem", "BudgetAnalyticsQuarantineImportKey",
         "one unresolved offline import item"),
        ("quarantine-dismiss-batch", "dismissBudgetAnalyticsQuarantineBatch", "BudgetAnalyticsQuarantineBatchRequest",
         "all unresolved offline import items from one batch"),
    ):
        paths[prefix + path] = {"post": {
            "operationId": operation,
            "summary": "Dismiss " + target,
            "description": "Requires the exact owner, budget edit permission, browser cookie and configured HTTPS Origin. "
                           "Records an audit event when rows are removed. A repeat on the same owned batch returns a "
                           "dismissed_count of zero. The original import report remains available to the operator.",
            "parameters": [{
                **quarantine_contract_version, "required": True,
                "description": "Requires contract 0.63.0 or later. Earlier versions and an omitted header receive 404.",
            }],
            "requestBody": {"required": True, "content": {_APP_JSON: {"schema": _ref(request)}}},
            "responses": _analytics_responses("BudgetAnalyticsQuarantineDismissResult"),
        }}
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
