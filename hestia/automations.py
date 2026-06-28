"""Local rules engine — hestia's cloud-free replacement for the Keemple app's
automations (M1: engine core).

A *rule* couples a **trigger** (a function-button scene press, or a device-state
predicate crossing false→true) to an ordered list of **actions** (the same control-op
vocabulary the control port already speaks — see ``hestia.proxy._OPS`` /
``build_command``), gated by optional ANDed **conditions** evaluated against live
state at fire time. The engine runs in BOTH modes: the proxy injects alongside the
Keemple cloud, the standalone server injects on its own.

Loop guards (the home must not oscillate):
- **Edge-triggered** state predicates fire only on the false→true transition, not on
  every event while the predicate stays true. Combined with ``State.apply``'s
  value-gating (a field only appears in ``changed`` when it actually changed) this
  bounds direct self-loops: our command → device report → no new value → no event.
- **Per-rule debounce** caps re-fire frequency. Cross-rule cycles
  (rule A flips X, rule B triggered by X flips Y, …) are the operator's
  responsibility; debounce keeps any runaway slow.

A condition turning true does NOT retro-fire a rule — only a trigger edge fires;
conditions are an instantaneous gate sampled at that moment.

Persistence mirrors ``hestia.registry`` exactly: a flat ``automations.json``
(``{"schema", "rules": [...]}``), atomic temp+fsync+os.replace, a ``dirty`` flag, and
the shared autosave loop. Stdlib-only.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import math
import os
import re
import tempfile
import time
from pathlib import Path

log = logging.getLogger("hestia.automations")

# Comparison operators usable in a trigger predicate or a condition.
_OPS_CMP = frozenset({"eq", "ne", "lt", "le", "gt", "ge"})

# Action ops come in two families:
#  - FRAME ops translate to a Keemple device frame via ``proxy.build_command`` (injected to the gateway).
#    Hardcoded — NOT imported from ``proxy._OPS`` (``proxy`` imports this module, so that would be a
#    circular import); ``test_automations`` asserts this set equals ``set(proxy._OPS)`` so the two can
#    never silently drift. ``build_command`` is imported lazily at fire time, inside ``_fire``.
#  - EFFECT ops produce no frame — they trigger an out-of-band side effect. ``ir`` transmits an infrared
#    signal through the Flipper, dispatched by enqueuing onto ``rt.ir_queue`` (drained by
#    ``proxy._ir_worker``) rather than via ``build_command``.
_FRAME_ACTION_OPS = frozenset(
    {"raw", "cover", "level", "switch", "lights", "thermostat", "thermostat_power"}
)
_EFFECT_ACTION_OPS = frozenset({"ir"})
_VALID_ACTION_OPS = _FRAME_ACTION_OPS | _EFFECT_ACTION_OPS

# Field name -> the live ``State`` attribute it reads. THE single source of truth: both
# ``current_value`` (which dict to read) and ``_VALID_STATE_FIELDS`` (which fields a rule
# may name) derive from this, so they cannot drift. Only scalar-valued fields appear —
# ``endpoints`` (a dict) and ``scene`` (an event, not state) are deliberately excluded, so
# a rule can never receive a non-scalar as a predicate operand.
# Sentinel for GLOBAL (node-less) scalar fields — a single value on ``State`` (not a per-node dict),
# fed by a poller rather than device events. ``crib_temp`` (the Tuya baby-monitor) is the first; predicates
# on a global field omit ``node`` and are driven by ``AutomationEngine.on_global`` / read by conditions.
_GLOBAL = object()
_UNSET = object()                    # "no value seen yet" sentinel for per-rule gang-edge baselining
_STATE_FIELD_ATTRS = {
    "door": "doors",
    "motion": "motion",              # PIR / occupancy bool (State.motion) — fed by _apply_motion's `changed`
    "level": "levels",
    "switch": "switches",
    "setpoint": "thermostat_setpoint",
    "thermostat_on": "thermostat_on",
    "temperature": "temperature",
    "power_w": "plug_w",
    "energy_kwh": "plug_kwh",
    "voltage_v": "plug_v",
    "crib_temp": _GLOBAL,            # global scalar (State.crib_temp), node-less — Tuya baby-monitor poller
    "outdoor_temp": _GLOBAL,         # global scalar (State.outdoor_temp), node-less — Open-Meteo poller
}
_VALID_STATE_FIELDS = frozenset(_STATE_FIELD_ATTRS)
_VALID_MODES = frozenset({"proxy", "standalone"})

# Public vocab the dashboard's guided rule form reads (via ``rule_vocab()``) so its dropdowns stay in
# lockstep with validation. These tuples are ALSO the single source used inside ``_validate_trigger``
# (its accepted-type list + the sun/presence event checks), so form and validator cannot drift.
_TRIGGER_TYPES = ("scene", "state", "time", "sun", "presence", "cron")
# Condition kinds the guided form offers: a state predicate (the unmarked default — `{node,field,op,value}`)
# or a `time_window` guard (`{type:"time_window", start, end, days?}`). Both are ANDed at fire time.
_CONDITION_TYPES = ("state", "time_window")
_SUN_EVENTS = ("sunrise", "sunset")
_PRESENCE_EVENTS = ("arrive", "leave")


def rule_vocab() -> dict:
    """The rule grammar the dashboard's guided form builds its dropdowns from — sourced from this
    module's own validation constants so the form can't silently drift from ``Rule.from_dict`` (a test
    pins it). ``state_fields`` maps each field → whether it is GLOBAL (node-less → the form omits node)."""
    return {
        "trigger_types": list(_TRIGGER_TYPES),
        "state_fields": {f: (_STATE_FIELD_ATTRS[f] is _GLOBAL) for f in sorted(_VALID_STATE_FIELDS)},
        "cmp_ops": sorted(_OPS_CMP),
        "frame_action_ops": sorted(_FRAME_ACTION_OPS),
        "modes": sorted(_VALID_MODES),
        "sun_events": list(_SUN_EVENTS),
        "presence_events": list(_PRESENCE_EVENTS),
        "condition_types": list(_CONDITION_TYPES),
    }


def _is_number(value) -> bool:
    """A real number for ordered comparison: int/float but NOT bool (else ``switch gt 0``
    would sneak through, since ``True == 1``)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _eval_predicate(value_now, op, target) -> bool:
    """Compare a live value against a target. ``eq``/``ne`` work for any type (so
    ``door eq "open"`` or ``switch eq false`` are valid); the ordered ops require BOTH
    sides to be real numbers, returning False otherwise (never raising) — an unseen
    value (``None``) therefore compares False for the ordered ops. ``op`` is assumed
    already validated to ``_OPS_CMP``."""
    if op == "eq":
        return value_now == target
    if op == "ne":
        return value_now != target
    if not (_is_number(value_now) and _is_number(target)):
        return False
    if op == "lt":
        return value_now < target
    if op == "le":
        return value_now <= target
    if op == "gt":
        return value_now > target
    return value_now >= target          # "ge"


def current_value(state, node, field, endpoint=None):
    """The live value of a node's scalar field from ``State``, or None when the field
    is unknown or the node has not reported it yet. With ``endpoint`` (a multi-gang
    switch's gang, 1 or 2) the on/off comes from ``State.gang[node][endpoint]`` rather
    than the whole-node ``switches`` map — so a predicate can target a single gang."""
    attr = _STATE_FIELD_ATTRS.get(field)
    if attr is None:
        return None
    if attr is _GLOBAL:                  # node-less scalar (e.g. crib_temp): ignore node
        return getattr(state, field)
    if endpoint is not None:             # one gang of a multi-gang switch
        return state.gang.get(node, {}).get(endpoint)
    return getattr(state, attr).get(node)


def _validate_node(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer, got {value!r}")
    return value


def _validate_predicate(spec, where):
    """Validate one state predicate (a trigger's state body or a condition). Returns a
    normalised ``{node, field, op, value}`` dict; raises ValueError naming the field."""
    if not isinstance(spec, dict):
        raise ValueError(f"{where} must be an object, got {spec!r}")
    field = spec.get("field")          # validate field FIRST — it decides whether `node` is required
    if field not in _VALID_STATE_FIELDS:
        raise ValueError(f"{where}.field {field!r} not in {sorted(_VALID_STATE_FIELDS)}")
    op = spec.get("op")
    if op not in _OPS_CMP:
        raise ValueError(f"{where}.op {op!r} not in {sorted(_OPS_CMP)}")
    if "value" not in spec:
        raise ValueError(f"{where} missing 'value'")
    value = spec["value"]
    # Must be a scalar. Reject null (would match every node that has not yet reported
    # the field, since current_value returns None) and any list/dict.
    if not isinstance(value, (bool, int, float, str)):
        raise ValueError(f"{where}.value must be a scalar (bool/number/string), got {value!r}")
    if isinstance(value, float) and not math.isfinite(value):   # NaN/Infinity (json accepts them)
        raise ValueError(f"{where}.value must be a finite number, got {value!r}")
    # An `endpoint` targets ONE gang of a multi-gang switch — only meaningful for the per-node `switch`
    # bool (the gang's on/off lands in State.gang[node][ep], emitted as the `endpoints` change map).
    # Validate it BEFORE the global-field fast path so a gang on a global / non-switch field is REJECTED,
    # not silently dropped.
    endpoint = spec.get("endpoint")
    if endpoint is not None:
        if field != "switch":
            raise ValueError(f"{where}.endpoint is only valid with field 'switch', got field {field!r}")
        if isinstance(endpoint, bool) or not isinstance(endpoint, int) or endpoint not in (1, 2):
            raise ValueError(f"{where}.endpoint must be the integer 1 or 2, got {endpoint!r}")
    if _STATE_FIELD_ATTRS[field] is _GLOBAL:   # global field: node-less (a supplied node is ignored)
        return {"field": field, "op": op, "value": value}
    node = _validate_node(spec.get("node"), f"{where}.node")
    if endpoint is None:
        return {"node": node, "field": field, "op": op, "value": value}
    return {"node": node, "field": field, "op": op, "value": value, "endpoint": endpoint}


def _validate_days(days, where="trigger.days"):
    """Validate an optional day-of-week filter (non-empty list of ints 0..6, Mon=0). Returns a
    fresh list (or None). Shared by the `time` and `sun` triggers so they can't drift."""
    if days is not None and (
            not isinstance(days, list) or not days
            or any(isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 6 for d in days)):
        raise ValueError(f"{where} must be a non-empty list of ints 0..6, got {days!r}")
    return list(days) if days is not None else None


# --- Sun (sunrise/sunset) — pure NOAA solar calc, stdlib-only, validated vs ephemerides ---
_SUN_ZENITH = math.radians(90.833)     # standard sunrise/sunset altitude (refraction + solar radius)


def sun_event_utc(lat, lon, date, event):
    """The UTC instant of sunrise/sunset for (`lat`, `lon`) [decimal degrees, N/E positive] on the
    given ``datetime.date``, or ``None`` when the sun never crosses the horizon that day (polar
    day/night). ``event`` is ``"sunrise"`` or ``"sunset"``. Returns an aware UTC ``datetime``; a
    time-of-day below 0 or past 24 h lands on the adjacent UTC date (via ``timedelta``), which is
    correct for far-east/far-west longitudes. NOAA "General Solar Position Calculations"
    (single-pass); ~1-3 min vs reference ephemerides — ample for actuating blinds/lights."""
    n = date.timetuple().tm_yday
    # 365.0 is NOAA's approximation (not 365.25/366) — the leap-year drift is sub-minute.
    gamma = (2 * math.pi / 365.0) * (n - 1)            # fractional year (rad); hour=12 single pass
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
                       - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))   # minutes
    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
            - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
            - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))                 # radians
    latr = math.radians(lat)
    cos_ha = (math.cos(_SUN_ZENITH) / (math.cos(latr) * math.cos(decl))
              - math.tan(latr) * math.tan(decl))
    if abs(cos_ha) > 1:                                # sun never reaches the horizon this day
        return None
    ha = math.degrees(math.acos(cos_ha))               # positive hour angle, degrees
    minutes = (720 - 4 * (lon + ha) - eqtime if event == "sunrise"
               else 720 - 4 * (lon - ha) - eqtime)
    midnight = datetime.datetime(date.year, date.month, date.day, tzinfo=datetime.timezone.utc)
    return midnight + datetime.timedelta(minutes=minutes)


# --- Presence (phone-by-MAC in the DHCP lease file) ---
_MAC_RE = re.compile(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")


def read_present_macs(path, now_epoch):
    """Parse a dnsmasq/Pi-hole lease file (`<expiry> <mac> <ip> <host> <clientid>` per line) → the set
    of currently-present MACs (lowercased). A lease counts as present when its expiry epoch is in the
    future (or ``0`` = infinite/static); an expired lease still lingering in the file before dnsmasq
    prunes it does NOT count. Returns ``None`` when the file is missing/unreadable — distinct from an
    empty set: the caller treats ``None`` as "presence unknown" and leaves edge state untouched. The
    MAC token is trusted (lowercased, not re-validated) — a malformed token simply never matches a
    rule's validated MAC, so it is harmless."""
    try:
        # errors="replace": the hostname field (DHCP opt 12) is client-supplied and NOT guaranteed
        # UTF-8 (Latin-1/Windows-1252 device names occur); a bad byte must never raise (it would
        # escape into _scheduler and kill the whole scheduler). The expiry + MAC fields are ASCII,
        # so a garbled hostname is harmless — we don't use it.
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.debug("lease file %s unreadable (%r) — presence unknown this tick", path, exc)
        return None
    present = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            expiry = int(parts[0])
        except ValueError:
            continue
        if expiry != 0 and expiry <= now_epoch:
            continue
        present.add(parts[1].lower())
    return present


# --- Cron (5-field) — standard cron over the same scheduler as time/sun, stdlib-only ---
# Fields: minute hour day-of-month month day-of-week (numeric only, ASCII). Items per field:
# `*` · `n` · `a-b` · `*/s` · `a-b/s` · `n/s` (≡ n..max step s — a hestia extension; strict Vixie
# rejects a step without a range). dow 0/7 = Sunday. dom/dow combine per Vixie: when NEITHER field is
# starred (leading `*`) a day matches dom OR dow; otherwise dom AND dow. Wall-clock, minute granularity.
_CRON_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))     # min hour dom month dow


def _cron_int(token):
    """Parse one cron numeric token as an ASCII non-negative int. Rejects ``+5`` / ``1_0`` / full-width
    ``５`` / ``' 5 '`` / ``''`` that Python's ``int()`` would silently accept (leaves ``'05'`` = 5)."""
    if not (token.isascii() and token.isdigit()):
        raise ValueError(f"not a number: {token!r}")
    return int(token)


def _parse_cron_field(field, lo, hi):
    """Expand one cron field to its allowed ints in ``[lo, hi]`` (a frozenset). Supports
    ``* n a-b */s a-b/s n/s``; raises ``ValueError`` on anything malformed / out-of-range / reversed /
    ``step<=0``. A valid field always yields a non-empty set."""
    values = set()
    for item in field.split(","):
        rng, sep, step_s = item.partition("/")
        step = _cron_int(step_s) if sep else 1
        if step <= 0:
            raise ValueError(f"step must be >= 1, got {step}")
        if rng == "*":
            start, end = lo, hi
        elif "-" in rng:
            a, _, b = rng.partition("-")
            start, end = _cron_int(a), _cron_int(b)
        else:
            start = _cron_int(rng)
            end = hi if sep else start              # `n/s` => n..max step s ; bare `n` => just n
        if not (lo <= start <= end <= hi):
            raise ValueError(f"{field!r} out of range / reversed")
        values.update(range(start, end + 1, step))
    return frozenset(values)


def _validate_cron(expr):
    """Validate a 5-field cron string; return its canonical (single-spaced) form. Raises ValueError."""
    if not isinstance(expr, str):
        raise ValueError(f"trigger.expr must be a cron string, got {expr!r}")
    fields = expr.split()                           # collapse whitespace; ignore leading/trailing
    if len(fields) != 5:
        raise ValueError(
            f"trigger.expr must have 5 fields (min hour dom month dow), got {len(fields)}")
    for f, (lo, hi) in zip(fields, _CRON_BOUNDS):
        try:
            _parse_cron_field(f, lo, hi)
        except ValueError as exc:
            raise ValueError(f"trigger.expr field {f!r} invalid: {exc}") from None
    return " ".join(fields)


def _validate_scene_trigger(spec):
    node = _validate_node(spec.get("node"), "trigger.node")
    sid = spec.get("scene_id")
    if isinstance(sid, bool) or not isinstance(sid, int) or sid < 0:
        raise ValueError(f"trigger.scene_id must be a non-negative integer, got {sid!r}")
    return {"type": "scene", "node": node, "scene_id": sid}


def _validate_hhmm(value, label):
    """Validate a wall-clock 'HH:MM' string and return it canonicalised (zero-padded). Shared by
    the `time` trigger and the `time_window` condition so they cannot drift. Raises ValueError."""
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an 'HH:MM' string, got {value!r}")
    try:
        parsed = datetime.datetime.strptime(value, "%H:%M")
    except ValueError:
        raise ValueError(f"{label} must be a valid 'HH:MM' time, got {value!r}") from None
    return parsed.strftime("%H:%M")


def _validate_time_trigger(spec):
    # Store the PUBLIC schema (canonical, zero-padded `at`) — never hour/minute — so
    # `Rule.to_dict` (= dict(self.trigger)) round-trips and the listing stays public.
    return {"type": "time", "at": _validate_hhmm(spec.get("at"), "trigger.at"),
            "days": _validate_days(spec.get("days"))}


def _validate_sun_trigger(spec):
    event = spec.get("event")
    if event not in _SUN_EVENTS:
        raise ValueError(f"trigger.event {event!r} must be one of {list(_SUN_EVENTS)}")
    # `offset_min` shifts the fire time relative to the event (e.g. sunset +15, sunrise −30).
    # Strict int (reject bool and float — JSON 15.0 is rejected, matching node/scene_id). The
    # ±1440 cap keeps the scheduler's candidate-date window (±2 days) exhaustive.
    offset = spec.get("offset_min", 0)
    if isinstance(offset, bool) or not isinstance(offset, int) or not -1440 <= offset <= 1440:
        raise ValueError(f"trigger.offset_min must be an int in [-1440, 1440], got {offset!r}")
    return {"type": "sun", "event": event, "offset_min": offset,
            "days": _validate_days(spec.get("days"))}


def _validate_presence_trigger(spec):
    mac = spec.get("mac")
    if not isinstance(mac, str) or not _MAC_RE.match(mac):
        raise ValueError(f"trigger.mac must be a MAC 'aa:bb:cc:dd:ee:ff', got {mac!r}")
    event = spec.get("event")
    if event not in _PRESENCE_EVENTS:
        raise ValueError(f"trigger.event {event!r} must be one of {list(_PRESENCE_EVENTS)}")
    return {"type": "presence", "mac": mac.lower(), "event": event}   # leases are lowercase


_TRIGGER_VALIDATORS = {
    "scene": _validate_scene_trigger,
    "state": lambda spec: {"type": "state", **_validate_predicate(spec, "trigger")},
    "time": _validate_time_trigger,
    "sun": _validate_sun_trigger,
    "presence": _validate_presence_trigger,
    "cron": lambda spec: {"type": "cron", "expr": _validate_cron(spec.get("expr"))},
}


def _cron_match(expr, when):
    """True iff the naive-local ``datetime`` ``when`` matches the (validated) 5-field cron ``expr`` to
    the minute. dom/dow use Vixie semantics (OR only when neither is starred); dow 0/7 = Sunday."""
    minute_f, hour_f, dom_f, mon_f, dow_f = expr.split()
    if when.minute not in _parse_cron_field(minute_f, 0, 59):
        return False
    if when.hour not in _parse_cron_field(hour_f, 0, 23):
        return False
    if when.month not in _parse_cron_field(mon_f, 1, 12):
        return False
    dom = _parse_cron_field(dom_f, 1, 31)
    dow = _parse_cron_field(dow_f, 0, 7)
    if 7 in dow:                                    # normalise Sunday (cron 7 -> Python-cron 0)
        dow = (dow - {7}) | {0}
    cron_dow = (when.weekday() + 1) % 7             # Python Mon=0..Sun=6 -> cron Sun=0..Sat=6
    dom_ok, dow_ok = when.day in dom, cron_dow in dow
    if not dom_f.startswith("*") and not dow_f.startswith("*"):   # both restricted -> OR (Vixie gotcha)
        return dom_ok or dow_ok
    return dom_ok and dow_ok                        # at least one starred -> AND


def _validate_trigger(spec):
    if not isinstance(spec, dict):
        raise ValueError(f"trigger must be an object, got {spec!r}")
    ttype = spec.get("type")
    validator = _TRIGGER_VALIDATORS.get(ttype) if isinstance(ttype, str) else None
    if validator is not None:
        return validator(spec)
    raise ValueError(f"trigger.type {ttype!r} must be one of {list(_TRIGGER_TYPES)}")


def _validate_rule_id(spec):
    rule_id = spec.get("id")
    if not isinstance(rule_id, str) or not rule_id:
        raise ValueError(f"rule.id must be a non-empty string, got {rule_id!r}")
    return rule_id


def _validate_enabled(spec):
    enabled = spec.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"rule.enabled must be a boolean, got {enabled!r}")
    return enabled


def _validate_modes(spec):
    modes = spec.get("modes", ["proxy", "standalone"])
    if (not isinstance(modes, list) or not modes
            or any(m not in _VALID_MODES for m in modes)):
        raise ValueError(
            f"rule.modes must be a non-empty subset of {sorted(_VALID_MODES)}, got {modes!r}")
    return modes


def _validate_debounce(spec):
    debounce = spec.get("debounce", 0.0)
    # math.isfinite rejects NaN/Infinity, which Python's json accepts and which would
    # bypass the >= 0 check (NaN compares False to everything → silently no debounce).
    if (isinstance(debounce, bool) or not isinstance(debounce, (int, float))
            or debounce < 0 or not math.isfinite(debounce)):
        raise ValueError(f"rule.debounce must be a finite number >= 0, got {debounce!r}")
    return float(debounce)


def _validate_time_window(spec, where):
    """Validate a time-of-day GUARD condition: ``{type:"time_window", start, end, days?}``. The window
    is the half-open interval ``[start, end)`` in local wall-clock time; ``start > end`` WRAPS midnight
    (active when ``now >= start`` OR ``now < end``). ``start == end`` is rejected (empty / all-day is
    ambiguous). Optional ``days`` (Mon=0..Sun=6) restricts the window to those weekdays — for a wrapped
    window the post-midnight tail belongs to the weekday the window STARTED on (see ``_eval_time_window``).
    Returns the canonical public dict (no ``days`` key when unset)."""
    start = _validate_hhmm(spec.get("start"), f"{where}.start")
    end = _validate_hhmm(spec.get("end"), f"{where}.end")
    if start == end:
        raise ValueError(f"{where} start and end must differ (empty/all-day window), got {start!r}")
    out = {"type": "time_window", "start": start, "end": end}
    days = _validate_days(spec.get("days"), f"{where}.days")
    if days is not None:
        out["days"] = days
    return out


def _validate_condition(spec, where):
    """One rule condition: either a ``time_window`` guard or a state predicate. A predicate carries no
    ``type`` key, so unmarked conditions stay backward-compatible and route to ``_validate_predicate``."""
    if isinstance(spec, dict) and spec.get("type") == "time_window":
        return _validate_time_window(spec, where)
    return _validate_predicate(spec, where)


def _validate_conditions(spec):
    conditions = spec.get("conditions", [])
    if not isinstance(conditions, list):
        raise ValueError(f"rule.conditions must be a list, got {conditions!r}")
    return [_validate_condition(c, f"conditions[{i}]") for i, c in enumerate(conditions)]


def _eval_time_window(cond, now_local) -> bool:
    """True iff the naive-local ``now_local`` falls inside the ``time_window`` ``cond``. Non-wrapping
    (``start < end``): active on ``[start, end)`` the same calendar day. Wrapping (``start > end``):
    active when ``now >= start`` (head, today) OR ``now < end`` (tail, after midnight). An optional
    ``days`` filter is checked against the weekday the window STARTED on — so the post-midnight tail of
    a wrapped window is owned by the previous day (``Mon 22:00-06:00`` includes Tue 00:00-05:59)."""
    cur = now_local.hour * 60 + now_local.minute
    sh, sm = (int(p) for p in cond["start"].split(":"))
    eh, em = (int(p) for p in cond["end"].split(":"))
    start, end = sh * 60 + sm, eh * 60 + em
    if start < end:                                    # same-day window
        if not start <= cur < end:
            return False
        owning_day = now_local.weekday()
    elif cur >= start:                                 # wrapped: head (still today)
        owning_day = now_local.weekday()
    elif cur < end:                                    # wrapped: tail (after midnight — owned by yesterday)
        owning_day = (now_local - datetime.timedelta(days=1)).weekday()
    else:                                              # wrapped: in the gap between end and start
        return False
    return cond.get("days") is None or owning_day in cond["days"]


def _local_now():
    """The current naive-local wall-clock ``datetime`` — the default ``time_window`` clock. Matches
    the naive-local ``now`` the scheduler already feeds ``on_time`` (see ``proxy``). Injectable in tests."""
    return datetime.datetime.now()


def _validate_action(action, i: int) -> None:
    if not isinstance(action, dict):
        raise ValueError(f"actions[{i}] must be an object, got {action!r}")
    if action.get("op") not in _VALID_ACTION_OPS:
        raise ValueError(
            f"actions[{i}].op {action.get('op')!r} not in {sorted(_VALID_ACTION_OPS)}")
    if action.get("op") == "ir" and not (
            isinstance(action.get("file"), str) and action.get("file")
            and isinstance(action.get("button"), str) and action.get("button")):
        raise ValueError(f"actions[{i}] ir requires non-empty string 'file' and 'button'")


def _validate_actions(spec):
    actions = spec.get("actions")
    if not isinstance(actions, list) or not actions:
        raise ValueError(f"rule.actions must be a non-empty list, got {actions!r}")
    for i, action in enumerate(actions):
        _validate_action(action, i)
    return [dict(a) for a in actions]


def _copy_condition(cond):
    """A one-level copy of a condition, also copying a ``time_window``'s ``days`` list so a caller
    editing ``to_dict`` output cannot mutate the stored rule (mirrors the trigger ``days`` copy)."""
    out = dict(cond)
    if isinstance(out.get("days"), list):
        out["days"] = list(out["days"])
    return out


class Rule:
    """A validated automation rule. Build via ``Rule.from_dict`` (raises ValueError on
    any malformed field); ``to_dict`` round-trips it back to JSON-able form."""

    def __init__(self, rule_id, trigger, actions, *, enabled=True, modes=None,
                 debounce=0.0, conditions=None):
        self.id = rule_id
        self.trigger = trigger
        self.actions = actions
        self.enabled = enabled
        self.modes = modes if modes is not None else ["proxy", "standalone"]
        self.debounce = debounce
        self.conditions = conditions if conditions is not None else []

    @classmethod
    def from_dict(cls, spec):
        if not isinstance(spec, dict):
            raise ValueError(f"rule must be an object, got {spec!r}")
        rule_id = _validate_rule_id(spec)
        enabled = _validate_enabled(spec)
        modes = _validate_modes(spec)
        debounce = _validate_debounce(spec)
        trigger = _validate_trigger(spec.get("trigger"))
        conditions = _validate_conditions(spec)
        actions = _validate_actions(spec)
        return cls(rule_id, trigger, actions, enabled=enabled,
                   modes=list(modes), debounce=debounce, conditions=conditions)

    def to_dict(self):
        """A JSON-able copy. Nested dicts/lists are copied one level so a caller editing
        the result cannot mutate the stored rule's structures (incl. a time trigger's
        ``days`` list)."""
        trigger = dict(self.trigger)
        if isinstance(trigger.get("days"), list):
            trigger["days"] = list(trigger["days"])
        return {
            "id": self.id,
            "enabled": self.enabled,
            "modes": list(self.modes),
            "debounce": self.debounce,
            "trigger": trigger,
            "conditions": [_copy_condition(c) for c in self.conditions],
            "actions": [dict(a) for a in self.actions],
        }


class AutomationStore:
    """Persisted set of rules (``id`` -> ``Rule``), mirroring ``Registry``'s flat-JSON +
    atomic-write + ``dirty`` pattern. Insertion order is preserved (it is the rule
    evaluation order)."""

    SCHEMA = 1

    def __init__(self, path, rules=None, *, writer=None):
        self.path = Path(path)
        self.rules = rules if rules is not None else {}
        self.dirty = False
        # Optional persistence backend (see Registry): callable(payload_bytes) -> None; default
        # None writes the atomic JSON file. The SQLite cutover (#57 P3) injects a DB writer.
        self._writer = writer

    @classmethod
    def load(cls, path):
        """Load rules from disk, degrading gracefully: a missing file is empty; an
        unreadable/non-JSON/non-object file, or a non-list ``rules``, logs a warning and
        starts empty; an individual invalid rule is logged and skipped; a duplicate id
        keeps the later definition; a foreign ``schema`` is a warning, not a failure —
        one bad entry must never lock the operator out of every working rule."""
        p = Path(path)
        if not p.exists():
            return cls(path)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("automations %s unreadable (%r) — starting empty", p, exc)
            return cls(path)
        if not isinstance(data, dict):
            log.warning("automations %s: top level is not an object — starting empty", p)
            return cls(path)
        schema = data.get("schema")
        if schema is not None and schema != cls.SCHEMA:
            log.warning("automations %s: schema %r != %d — proceeding best-effort",
                        p, schema, cls.SCHEMA)
        raw_rules = data.get("rules", [])
        if not isinstance(raw_rules, list):
            log.warning("automations %s: 'rules' is not a list — starting empty", p)
            return cls(path)
        rules = {}
        for item in raw_rules:
            try:
                rule = Rule.from_dict(item)
            except ValueError as exc:
                log.warning("automations %s: skipping invalid rule (%s)", p, exc)
                continue
            if rule.id in rules:
                log.warning("automations %s: duplicate rule id %r — later one wins", p, rule.id)
            rules[rule.id] = rule
        return cls(path, rules)

    def serialize_rules(self, rules) -> bytes:
        """Serialize an explicit ``id -> Rule`` mapping (not necessarily ``self.rules``),
        so a control op can persist a *prospective* rule-set before swapping it live."""
        payload = {"schema": self.SCHEMA, "rules": [r.to_dict() for r in rules.values()]}
        return json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")

    def serialize(self) -> bytes:
        return self.serialize_rules(self.rules)

    def write_payload(self, payload: bytes) -> None:
        """Persist the payload off the event loop: a backend ``writer`` (SQLite cutover) if set,
        else an atomic JSON file write (same idiom as ``Registry.write_payload``). Both raise
        ``OSError`` on failure, so ``_write_and_settle``'s cancel handling is identical either way."""
        if self._writer is not None:
            self._writer(payload)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except OSError:
            Path(tmp).unlink(missing_ok=True)
            raise

    def save(self) -> None:
        self.write_payload(self.serialize())
        self.dirty = False

    def set_rule(self, rule: "Rule") -> None:
        self.rules[rule.id] = rule
        self.dirty = True

    def delete_rule(self, rule_id) -> bool:
        if rule_id in self.rules:
            del self.rules[rule_id]
            self.dirty = True
            return True
        return False

    def snapshot(self) -> list:
        return [r.to_dict() for r in self.rules.values()]


class AutomationEngine:
    """Evaluates rules against device events and emits device-bound command frames.

    Owns the in-memory loop-guard caches (``_last_match`` for state-trigger edges,
    ``_last_fired`` for debounce), keyed by rule id. Rule CRUD goes THROUGH the engine
    (``set_rule``/``delete_rule``) so a replaced or deleted id can never inherit stale
    edge/debounce state. ``clock`` (monotonic, for debounce) and ``wall`` (naive-local
    ``datetime`` now, for ``time_window`` conditions) are injectable for deterministic tests."""

    def __init__(self, store: "AutomationStore", clock=time.monotonic, wall=_local_now):
        self.store = store
        self._clock = clock
        self._wall = wall
        self._last_match: "dict[str, bool]" = {}        # state-trigger edge
        self._last_fired: "dict[str, float]" = {}       # debounce (monotonic)
        self._last_time_fire: "dict[str, tuple]" = {}   # time-trigger minute-slot dedup
        self._last_presence: "dict[str, bool]" = {}     # presence-trigger edge (per rule)
        self._last_gang: "dict[str, bool]" = {}         # per-gang switch-trigger edge: last value of the watched gang

    def reset_runtime(self, rule_id) -> None:
        """Drop a rule's loop-guard state so a re-authored/replaced id starts clean: the next true
        edge fires, no debounce carries over, and the time-slot/presence dedup resets."""
        self._last_match.pop(rule_id, None)
        self._last_fired.pop(rule_id, None)
        self._last_time_fire.pop(rule_id, None)
        self._last_presence.pop(rule_id, None)
        self._last_gang.pop(rule_id, None)

    def set_rule(self, rule: "Rule") -> None:
        """In-memory create/replace (used by tests and any non-durable caller). The
        control port persists durably first — see ``proxy._commit_automation``."""
        self.store.set_rule(rule)
        self.reset_runtime(rule.id)

    def delete_rule(self, rule_id) -> bool:
        found = self.store.delete_rule(rule_id)
        self.reset_runtime(rule_id)
        return found

    def on_event(self, rt, node, changed, scene) -> list:
        """React to one decoded device event (after ``State.apply``): ``changed`` is the
        post-``scene``-pop change dict, ``scene`` the popped scene event (or None), ``node``
        the reporting node id. Returns the command frames every matching scene/state rule
        produced, in rule order. Time-triggered rules are handled by ``on_time``."""
        from .proxy import _audit_observed       # lazy: avoid a proxy<->automations cycle
        _audit_observed(rt, node, changed, scene)  # #56: log the physical/external state change (actor=device)
        frames = []
        for rule in self.store.rules.values():
            if not rule.enabled or rt.mode not in rule.modes:
                continue
            # Only scene/state are event-driven; time/sun/cron (and any future scheduled type) are
            # handled by on_time. This allow-set is closed by design — a new EVENT type must be
            # added here explicitly, never fall through to fire on every event.
            if rule.trigger["type"] not in ("scene", "state"):
                continue
            tg = rule.trigger
            # A global-field (node-less, poller-driven) state trigger is NOT device-event-driven —
            # skip it here so `_triggered` never reads its absent `node` key; `on_global` owns it.
            if tg["type"] == "state" and _STATE_FIELD_ATTRS.get(tg.get("field")) is _GLOBAL:
                continue
            if self._triggered(rule, node, changed, scene):
                frames.extend(self._fire(rt, rule))
        return frames

    def on_time(self, rt, now) -> list:
        """React to the wall-clock ``now`` (a naive-local ``datetime``): fire each due `time`, `sun`
        and `cron` rule. A rule is due when ``now`` matches its event minute (`time`: the `at` HH:MM;
        `sun`: sunrise/sunset ± offset, see ``_sun_due``; `cron`: the 5-field expr, see ``_cron_match``)
        and (if set) its day-of-week filter, and it has not already fired this minute (``_last_time_fire``
        slot dedup, so a sub-minute tick can't double-fire). Returns the action frames in rule order."""
        slot = (now.year, now.month, now.day, now.hour, now.minute)
        now_utc = None                                  # lazily derived (only if a sun rule needs it)
        frames = []
        for rule in self.store.rules.values():
            ready, now_utc = self._scheduled_rule_ready(rt, rule, now, now_utc, slot)
            if not ready:
                continue
            self._last_time_fire[rule.id] = slot
            frames.extend(self._fire(rt, rule, now))
        return frames

    def _sun_due(self, rt, now_utc, tg) -> bool:
        """True iff ``now_utc`` (aware UTC, compared to the minute) is the rule's sunrise/sunset
        plus ``offset_min``. Checks candidate event dates ``now_utc.date() + {-2..+2}`` — provably
        exhaustive for ``offset_min`` ∈ [-1440, 1440] across all lat/lon (the event's UTC time-of-day
        spans ~[-0.5, +1.5] days from its date, plus a ±1-day offset; see docs/AUTOMATIONS.md).
        Polar (no-event) candidates are skipped; slot-dedup in ``on_time`` makes a match idempotent."""
        for delta in (-2, -1, 0, 1, 2):
            ev = sun_event_utc(rt.lat, rt.lon, now_utc.date() + datetime.timedelta(days=delta),
                               tg["event"])
            if ev is None:
                continue
            fire = ev + datetime.timedelta(minutes=tg["offset_min"])
            if (fire.year, fire.month, fire.day, fire.hour, fire.minute) == (
                    now_utc.year, now_utc.month, now_utc.day, now_utc.hour, now_utc.minute):
                return True
        return False

    def has_presence_rules(self) -> bool:
        """Whether any rule uses a `presence` trigger — lets the scheduler skip the lease-file read
        entirely when none do (zero I/O for non-presence deployments)."""
        return any(r.trigger["type"] == "presence" for r in self.store.rules.values())

    def on_presence(self, rt, present_macs) -> list:
        """React to the current set of present MACs (from ``read_present_macs``): fire each `presence`
        rule on its arrival (absent→present) or departure (present→absent) edge. ``present_macs`` is
        ``None`` when the lease file is unreadable — presence is then UNKNOWN, so edges are left
        untouched (no fire, no baseline change). The first readable observation only establishes the
        baseline (``prev is None`` → no fire at startup). Returns the action frames in rule order."""
        if present_macs is None:                          # presence unknown → don't disturb edges
            return []
        frames = []
        for rule in self.store.rules.values():
            if not rule.enabled or rt.mode not in rule.modes:
                continue
            tg = rule.trigger
            if tg["type"] != "presence":
                continue
            now_present = tg["mac"] in present_macs
            prev = self._last_presence.get(rule.id)
            self._last_presence[rule.id] = now_present    # always track (even at baseline / no edge)
            if prev is None or now_present == prev:        # first observation, or no transition
                continue
            if now_present != (tg["event"] == "arrive"):   # edge direction must match the rule
                continue
            frames.extend(self._fire(rt, rule))            # conditions + debounce reused
        return frames

    def on_global(self, rt, field, value) -> list:
        """React to a fresh value of a GLOBAL (node-less) state field — fed by a poller, e.g. the Neno baby-monitor
        ``crib_temp``. Fires each enabled, mode-matching `state` rule on that field whose predicate
        crosses false->true (edge-detected via ``_last_match``, like a device state trigger, but driven
        here — ``on_event`` skips global fields). The edge is consumed even when conditions/debounce
        suppress the action (re-fires only on the next false->true). Returns frames in rule order."""
        frames = []
        for rule in self.store.rules.values():
            if not rule.enabled or rt.mode not in rule.modes:
                continue
            tg = rule.trigger
            if (tg["type"] != "state" or tg.get("field") != field
                    or _STATE_FIELD_ATTRS.get(field) is not _GLOBAL):
                continue
            now_true = _eval_predicate(value, tg["op"], tg["value"])
            prev = self._last_match.get(rule.id, False)
            self._last_match[rule.id] = now_true           # always track, even if suppressed below
            if now_true and not prev:
                frames.extend(self._fire(rt, rule))
        return frames

    def _scheduled_due(self, rt, now, now_utc, tg):
        ttype = tg["type"]
        if ttype == "time":
            hour, minute = (int(p) for p in tg["at"].split(":"))   # tg["at"] is validated HH:MM
            return hour == now.hour and minute == now.minute, now_utc
        if ttype == "sun":
            if None in (rt.lat, rt.lon):            # location unconfigured → sun rules can't fire
                return False, now_utc
            if now_utc is None:                     # naive-local now → aware UTC (DST-correct)
                now_utc = now.astimezone(datetime.timezone.utc)
            return self._sun_due(rt, now_utc, tg), now_utc
        if ttype == "cron":
            return _cron_match(tg["expr"], now), now_utc      # cron does its own dom/month/dow internally
        return False, now_utc                                # scene/state/presence aren't scheduler-driven

    @staticmethod
    def _scheduled_day_allowed(tg, now) -> bool:
        # cron has no `days` key (it owns dow); time/sun always set it → `.get` is equivalent there.
        return tg.get("days") is None or now.weekday() in tg["days"]

    def _scheduled_conditions_ok(self, rt, rule, now) -> bool:
        return all(self._condition_ok(rt.state, c, now) for c in rule.conditions)

    def _scheduled_rule_ready(self, rt, rule, now, now_utc, slot):
        if not rule.enabled or rt.mode not in rule.modes:
            return False, now_utc
        tg = rule.trigger
        due, now_utc = self._scheduled_due(rt, now, now_utc, tg)
        if not due or not self._scheduled_day_allowed(tg, now):
            return False, now_utc
        if self._last_time_fire.get(rule.id) == slot:
            return False, now_utc
        # Check conditions BEFORE consuming the minute slot: if they're false now, the
        # slot stays free so a later tick this minute can retry once they hold. (_fire
        # re-checks with the SAME `now` — harmless — and applies debounce + builds the actions.)
        if not self._scheduled_conditions_ok(rt, rule, now):
            return False, now_utc
        return True, now_utc

    def _fire(self, rt, rule, now_local=None) -> list:
        """Shared firing tail once a rule's trigger has matched: AND its conditions against
        live state, apply per-rule debounce, then build its action frames (one bad action
        logged + skipped, never tearing down the loop). Returns the frames (possibly empty).
        ``now_local`` (naive-local) gates ``time_window`` conditions; event-driven callers pass
        None to sample ``self._wall()`` here, while ``on_time`` threads its own scheduler ``now``."""
        if now_local is None:
            now_local = self._wall()
        if not all(self._condition_ok(rt.state, c, now_local) for c in rule.conditions):
            return []
        now = self._clock()
        if rule.debounce > 0 and now - self._last_fired.get(rule.id, -math.inf) < rule.debounce:
            return []
        self._last_fired[rule.id] = now
        from .proxy import _audit, _cover_reps, build_command   # lazy: avoid a proxy<->automations cycle
        actor = f"automation:{rule.id}"            # #56: distinguishes a rule firing from a user action
        frames = []
        for action in rule.actions:
            op = action.get("op")
            target = action.get("node", action.get("file"))
            target = str(target) if target is not None else None
            if op == "ir":                           # effect op: no frame — hand to the Flipper worker
                self._dispatch_ir(rt, rule, action)
                _audit(rt, actor, op, target=target, result="fired")
                continue
            try:
                # A cover action is emitted COVER_REPEAT× (idempotent redundancy); one audit row per action.
                for _ in range(_cover_reps(rt, op)):
                    frames.append(build_command(rt, action))
                _audit(rt, actor, op, target=target, result="fired")
            except (ValueError, KeyError, TypeError, OverflowError):
                log.exception("automation %r: action %r failed — skipping", rule.id, action)
                _audit(rt, actor, op, target=target, result="error")
        return frames

    def _dispatch_ir(self, rt, rule, action) -> None:
        """Hand an ``ir`` effect to the Flipper transmit worker via ``rt.ir_queue`` (fire-and-forget;
        ordered only among IR actions — a rule mixing device frames and ``ir`` does not guarantee
        cross-transport ordering). No queue (Flipper IR disabled) → skip; a full queue → drop. Never
        raises, so a missing/over-full queue can't tear down the engine loop."""
        queue = getattr(rt, "ir_queue", None)
        if queue is None:
            log.warning("automation %r: ir action but Flipper IR is disabled — skipping", rule.id)
            return
        try:
            queue.put_nowait((action["file"], action["button"], None))
        except asyncio.QueueFull:
            log.warning("automation %r: ir queue full — dropping ir action", rule.id)

    def _triggered(self, rule, node, changed, scene) -> bool:
        tg = rule.trigger
        if tg["type"] == "scene":
            return scene is not None and node == tg["node"] and scene["id"] == tg["scene_id"]
        # state trigger: edge-detected on the predicate's false->true transition.
        if node != tg["node"]:
            return False
        if tg.get("endpoint") is not None:
            return self._gang_triggered(rule, tg, changed)
        if tg["field"] not in changed:
            return False
        now_true = _eval_predicate(changed[tg["field"]], tg["op"], tg["value"])
        prev = self._last_match.get(rule.id, False)
        self._last_match[rule.id] = now_true     # always track, even if conditions/debounce
        return now_true and not prev             # later suppress the action (the edge is real)

    def _gang_triggered(self, rule, tg, changed) -> bool:
        """Edge for a per-gang `switch` trigger. ``changed["endpoints"]`` is the FULL per-node {ep: on}
        roll-up (emitted on ANY gang change), so its presence does NOT prove the WATCHED gang moved.
        Track the watched gang's last value per rule and fire only on a real transition into a
        predicate-true value. First sight is a baseline (no fire) — like a presence trigger — because the
        prior gang state can't be known from a roll-up; this also stops a sibling-gang change from firing
        a freshly-(re)loaded rule whose gang already matches."""
        ep = tg["endpoint"]
        endpoints = changed.get("endpoints")
        if endpoints is None or ep not in endpoints:
            return False
        value_now = endpoints[ep]
        prev_value = self._last_gang.get(rule.id, _UNSET)
        self._last_gang[rule.id] = value_now
        if prev_value is _UNSET or value_now == prev_value:
            return False                         # baseline, or a sibling gang moved (this one didn't)
        return _eval_predicate(value_now, tg["op"], tg["value"])  # a real flip → fire iff now predicate-true

    @staticmethod
    def _condition_ok(state, cond, now_local) -> bool:
        # A time_window guard is evaluated against the wall clock; everything else is a state predicate.
        if cond.get("type") == "time_window":
            return _eval_time_window(cond, now_local)
        # cond.get("node"): a global-field condition has no `node` (current_value ignores it anyway).
        # cond.get("endpoint"): present only for a per-gang switch predicate (reads State.gang[node][ep]).
        return _eval_predicate(
            current_value(state, cond.get("node"), cond["field"], cond.get("endpoint")),
            cond["op"], cond["value"])
