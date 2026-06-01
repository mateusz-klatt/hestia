"""Unit tests for hestia.automations — the M1 rules engine: Rule validation,
predicate evaluation, the persisted AutomationStore, and the AutomationEngine's
trigger/condition/edge/debounce/action behaviour."""
from __future__ import annotations

import datetime
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hestia import automations, proxy
from hestia.automations import AutomationEngine, AutomationStore, Rule

SCENE_RULE = {
    "id": "r1",
    "trigger": {"type": "scene", "node": 2, "scene_id": 3},
    "actions": [{"op": "switch", "node": 14, "on": True}],
}
STATE_RULE = {
    "id": "cold",
    "trigger": {"type": "state", "node": 7, "field": "temperature", "op": "lt", "value": 18},
    "actions": [{"op": "switch", "node": 14, "on": True}],
}
TIME_RULE = {
    "id": "morning",
    "trigger": {"type": "time", "at": "07:30"},
    "actions": [{"op": "switch", "node": 14, "on": True}],
}
SUN_RULE = {
    "id": "blinds-sunset",
    "trigger": {"type": "sun", "event": "sunset", "offset_min": 15},
    "actions": [{"op": "cover", "node": 4, "value": 0}],
}
PRESENCE_RULE = {
    "id": "arrive-home",
    "trigger": {"type": "presence", "mac": "aa:bb:cc:dd:ee:ff", "event": "arrive"},
    "actions": [{"op": "thermostat_power", "node": 9, "on": True}],
}
CRIB_RULE = {       # global (node-less) state trigger on the Tuya baby-monitor crib temperature
    "id": "crib-hot",
    "trigger": {"type": "state", "field": "crib_temp", "op": "gt", "value": 24},
    "actions": [{"op": "switch", "node": 14, "on": True}],
}
# 2026-01-05 is a Monday (weekday() == 0).
MON_0730 = datetime.datetime(2026, 1, 5, 7, 30)


def _rt(mode="proxy"):
    rt = proxy.ProxyRuntime()
    rt.mode = mode
    return rt


def _engine(*rules, clock=None):
    store = AutomationStore("unused.json")
    eng = AutomationEngine(store) if clock is None else AutomationEngine(store, clock=clock)
    for spec in rules:
        eng.set_rule(Rule.from_dict(spec))
    return eng


# --- predicate evaluation ----------------------------------------------------

class EvalPredicateTests(unittest.TestCase):
    def test_eq_ne(self):
        self.assertTrue(automations._eval_predicate("open", "eq", "open"))
        self.assertFalse(automations._eval_predicate("open", "eq", "closed"))
        self.assertTrue(automations._eval_predicate(True, "ne", False))
        self.assertFalse(automations._eval_predicate(5, "ne", 5))

    def test_ordered_numeric(self):
        self.assertTrue(automations._eval_predicate(3, "lt", 5))
        self.assertFalse(automations._eval_predicate(5, "lt", 5))
        self.assertTrue(automations._eval_predicate(5, "le", 5))
        self.assertFalse(automations._eval_predicate(6, "le", 5))
        self.assertTrue(automations._eval_predicate(6, "gt", 5))
        self.assertFalse(automations._eval_predicate(5, "gt", 5))
        self.assertTrue(automations._eval_predicate(5, "ge", 5))
        self.assertFalse(automations._eval_predicate(4, "ge", 5))

    def test_ordered_requires_numbers(self):
        self.assertFalse(automations._eval_predicate("hot", "gt", 5))   # str vs num
        self.assertFalse(automations._eval_predicate(5, "lt", "x"))     # num vs str
        self.assertFalse(automations._eval_predicate(None, "gt", 5))    # unseen value
        self.assertFalse(automations._eval_predicate(True, "gt", 0))    # bool is not a number


class CurrentValueTests(unittest.TestCase):
    def test_known_field(self):
        rt = _rt()
        rt.state.temperature[7] = 21
        self.assertEqual(automations.current_value(rt.state, 7, "temperature"), 21)

    def test_unseen_node(self):
        self.assertIsNone(automations.current_value(_rt().state, 99, "switch"))

    def test_unknown_field_returns_none(self):
        self.assertIsNone(automations.current_value(_rt().state, 7, "bogus"))

    def test_global_field_ignores_node(self):
        rt = _rt()
        rt.state.crib_temp = 25.6
        self.assertEqual(automations.current_value(rt.state, None, "crib_temp"), 25.6)
        self.assertEqual(automations.current_value(rt.state, 999, "crib_temp"), 25.6)  # node ignored

    def test_global_field_default_none(self):
        self.assertIsNone(automations.current_value(_rt().state, None, "crib_temp"))


class DriftTests(unittest.TestCase):
    """Hardcoded sets must track their real sources (no module-level proxy import)."""

    def test_action_ops_match_proxy_ops(self):
        # FRAME action ops must equal the build_command sources (proxy._OPS); "ir" is an EFFECT op
        # (no frame — dispatched via rt.ir_queue), so it is engine-only and not in proxy._OPS.
        self.assertEqual(automations._FRAME_ACTION_OPS, set(proxy._OPS))
        self.assertEqual(automations._VALID_ACTION_OPS, automations._FRAME_ACTION_OPS | {"ir"})

    def test_state_fields_match_mapping(self):
        self.assertEqual(automations._VALID_STATE_FIELDS, set(automations._STATE_FIELD_ATTRS))


# --- Rule validation ---------------------------------------------------------

class RuleValidationTests(unittest.TestCase):
    def test_valid_round_trip(self):
        rule = Rule.from_dict(SCENE_RULE)
        d = rule.to_dict()
        self.assertEqual(d["id"], "r1")
        self.assertEqual(d["modes"], ["proxy", "standalone"])
        self.assertEqual(d["debounce"], 0.0)
        self.assertEqual(d["conditions"], [])
        self.assertEqual(Rule.from_dict(d).to_dict(), d)            # idempotent

    def test_valid_state_rule_with_conditions(self):
        spec = dict(STATE_RULE, conditions=[{"node": 10, "field": "switch", "op": "eq", "value": False}],
                    modes=["standalone"], debounce=5, enabled=False)
        rule = Rule.from_dict(spec)
        self.assertEqual(rule.modes, ["standalone"])
        self.assertEqual(rule.debounce, 5.0)
        self.assertFalse(rule.enabled)
        self.assertEqual(rule.trigger["type"], "state")

    def _bad(self, spec):
        with self.assertRaises(ValueError):
            Rule.from_dict(spec)

    def test_not_a_dict(self):
        self._bad(["not", "a", "dict"])

    def test_bad_id(self):
        self._bad(dict(SCENE_RULE, id=""))
        self._bad(dict(SCENE_RULE, id=5))

    def test_bad_enabled(self):
        self._bad(dict(SCENE_RULE, enabled="yes"))

    def test_bad_modes(self):
        self._bad(dict(SCENE_RULE, modes="proxy"))         # not a list
        self._bad(dict(SCENE_RULE, modes=[]))              # empty
        self._bad(dict(SCENE_RULE, modes=["cloud"]))       # unknown mode

    def test_bad_debounce(self):
        self._bad(dict(SCENE_RULE, debounce=True))         # bool
        self._bad(dict(SCENE_RULE, debounce="5"))          # str
        self._bad(dict(SCENE_RULE, debounce=-1))           # negative
        self._bad(dict(SCENE_RULE, debounce=float("nan")))    # NaN would silently disable debounce
        self._bad(dict(SCENE_RULE, debounce=float("inf")))    # Infinity would suppress forever

    def test_bad_predicate_value(self):
        self._bad(dict(STATE_RULE, trigger={"type": "state", "node": 7, "field": "temperature",
                                            "op": "eq", "value": None}))      # null matches unseen
        self._bad(dict(STATE_RULE, trigger={"type": "state", "node": 7, "field": "temperature",
                                            "op": "eq", "value": [1, 2]}))    # non-scalar
        self._bad(dict(STATE_RULE, trigger={"type": "state", "node": 7, "field": "temperature",
                                            "op": "lt", "value": float("inf")}))   # non-finite

    def test_bad_trigger_not_dict(self):
        self._bad(dict(SCENE_RULE, trigger="scene"))

    def test_bad_trigger_type(self):
        self._bad(dict(SCENE_RULE, trigger={"type": "timer"}))

    def test_bad_scene_node(self):
        self._bad(dict(SCENE_RULE, trigger={"type": "scene", "node": -1, "scene_id": 3}))
        self._bad(dict(SCENE_RULE, trigger={"type": "scene", "node": True, "scene_id": 3}))

    def test_bad_scene_id(self):
        self._bad(dict(SCENE_RULE, trigger={"type": "scene", "node": 2, "scene_id": "x"}))
        self._bad(dict(SCENE_RULE, trigger={"type": "scene", "node": 2, "scene_id": True}))

    def test_bad_state_field(self):
        self._bad(dict(STATE_RULE, trigger={"type": "state", "node": 7, "field": "endpoints",
                                            "op": "eq", "value": 1}))   # non-scalar rejected
        self._bad(dict(STATE_RULE, trigger={"type": "state", "node": 7, "field": "nope",
                                            "op": "eq", "value": 1}))   # typo rejected

    def test_bad_state_op(self):
        self._bad(dict(STATE_RULE, trigger={"type": "state", "node": 7, "field": "temperature",
                                            "op": "between", "value": 1}))

    def test_state_missing_value(self):
        self._bad(dict(STATE_RULE, trigger={"type": "state", "node": 7,
                                            "field": "temperature", "op": "lt"}))

    def test_bad_state_node(self):
        self._bad(dict(STATE_RULE, trigger={"type": "state", "node": "7",
                                            "field": "temperature", "op": "lt", "value": 1}))

    def test_global_field_omits_node(self):
        # a global (node-less) state trigger is accepted WITHOUT a node; the normalized
        # predicate carries no "node" key.
        rule = Rule.from_dict(CRIB_RULE)
        self.assertEqual(rule.trigger,
                         {"type": "state", "field": "crib_temp", "op": "gt", "value": 24})
        self.assertNotIn("node", rule.trigger)

    def test_global_field_supplied_node_dropped(self):
        # a node accidentally supplied on a global predicate is ignored, not stored.
        rule = Rule.from_dict(dict(CRIB_RULE, trigger={
            "type": "state", "node": 5, "field": "crib_temp", "op": "gt", "value": 24}))
        self.assertNotIn("node", rule.trigger)

    def test_non_global_state_still_requires_node(self):
        # per-node fields keep requiring a node — a missing node is rejected.
        self._bad(dict(STATE_RULE, trigger={
            "type": "state", "field": "temperature", "op": "lt", "value": 18}))

    def test_global_condition_omits_node(self):
        rule = Rule.from_dict(dict(SCENE_RULE,
                                   conditions=[{"field": "crib_temp", "op": "lt", "value": 18}]))
        self.assertEqual(rule.conditions[0], {"field": "crib_temp", "op": "lt", "value": 18})

    def test_bad_conditions_not_list(self):
        self._bad(dict(SCENE_RULE, conditions={"node": 1}))

    def test_bad_condition_predicate(self):
        self._bad(dict(SCENE_RULE, conditions=[{"node": 1, "field": "scene", "op": "eq", "value": 1}]))

    def test_bad_condition_not_an_object(self):
        self._bad(dict(SCENE_RULE, conditions=["not-a-dict"]))

    def test_bad_actions(self):
        self._bad(dict(SCENE_RULE, actions="switch"))      # not a list
        self._bad(dict(SCENE_RULE, actions=[]))            # empty
        self._bad(dict(SCENE_RULE, actions=["raw"]))       # element not a dict
        self._bad(dict(SCENE_RULE, actions=[{"op": "explode"}]))   # unknown op

    def test_valid_time_rule(self):
        rule = Rule.from_dict(TIME_RULE)
        self.assertEqual(rule.trigger, {"type": "time", "at": "07:30", "days": None})
        self.assertEqual(Rule.from_dict(rule.to_dict()).to_dict(), rule.to_dict())   # round-trips

    def test_time_at_canonicalised(self):
        rule = Rule.from_dict(dict(TIME_RULE, trigger={"type": "time", "at": "7:5", "days": [0, 4]}))
        self.assertEqual(rule.trigger, {"type": "time", "at": "07:05", "days": [0, 4]})

    def test_to_dict_days_is_copied(self):
        rule = Rule.from_dict(dict(TIME_RULE, trigger={"type": "time", "at": "07:30", "days": [0, 1]}))
        rule.to_dict()["trigger"]["days"].append(6)
        self.assertEqual(rule.trigger["days"], [0, 1])             # live rule unaffected by caller edit

    def test_bad_time_at(self):
        self._bad(dict(TIME_RULE, trigger={"type": "time", "at": 1200}))             # not a string
        self._bad(dict(TIME_RULE, trigger={"type": "time", "at": "7h30"}))           # bad format
        self._bad(dict(TIME_RULE, trigger={"type": "time", "at": "24:00"}))          # hour out of range
        self._bad(dict(TIME_RULE, trigger={"type": "time", "at": "07:60"}))          # minute out of range

    def test_bad_time_days(self):
        self._bad(dict(TIME_RULE, trigger={"type": "time", "at": "07:30", "days": 0}))       # not a list
        self._bad(dict(TIME_RULE, trigger={"type": "time", "at": "07:30", "days": []}))      # empty → never fires
        self._bad(dict(TIME_RULE, trigger={"type": "time", "at": "07:30", "days": [7]}))     # out of range
        self._bad(dict(TIME_RULE, trigger={"type": "time", "at": "07:30", "days": [True]}))  # bool

    def test_valid_sun_rule(self):
        rule = Rule.from_dict(SUN_RULE)
        self.assertEqual(rule.trigger,
                         {"type": "sun", "event": "sunset", "offset_min": 15, "days": None})
        self.assertEqual(Rule.from_dict(rule.to_dict()).to_dict(), rule.to_dict())   # round-trips

    def test_sun_offset_defaults_zero(self):
        rule = Rule.from_dict(dict(SUN_RULE, trigger={"type": "sun", "event": "sunrise"}))
        self.assertEqual(rule.trigger,
                         {"type": "sun", "event": "sunrise", "offset_min": 0, "days": None})

    def test_sun_days_is_copied(self):
        rule = Rule.from_dict(dict(SUN_RULE,
                                   trigger={"type": "sun", "event": "sunset", "days": [5, 6]}))
        self.assertEqual(rule.trigger["days"], [5, 6])
        rule.to_dict()["trigger"]["days"].append(0)
        self.assertEqual(rule.trigger["days"], [5, 6])             # live rule unaffected by caller edit

    def test_bad_sun_event(self):
        self._bad(dict(SUN_RULE, trigger={"type": "sun", "event": "noon"}))    # not sunrise/sunset
        self._bad(dict(SUN_RULE, trigger={"type": "sun"}))                     # missing event

    def test_bad_sun_offset(self):
        self._bad(dict(SUN_RULE, trigger={"type": "sun", "event": "sunset", "offset_min": 15.0}))   # float
        self._bad(dict(SUN_RULE, trigger={"type": "sun", "event": "sunset", "offset_min": True}))   # bool
        self._bad(dict(SUN_RULE, trigger={"type": "sun", "event": "sunset", "offset_min": 1441}))   # too big
        self._bad(dict(SUN_RULE, trigger={"type": "sun", "event": "sunset", "offset_min": -1441}))  # too small

    def test_bad_sun_days(self):
        self._bad(dict(SUN_RULE, trigger={"type": "sun", "event": "sunset", "days": [7]}))   # out of range
        self._bad(dict(SUN_RULE, trigger={"type": "sun", "event": "sunset", "days": []}))    # empty

    def test_valid_presence_rule(self):
        rule = Rule.from_dict(PRESENCE_RULE)
        self.assertEqual(rule.trigger,
                         {"type": "presence", "mac": "aa:bb:cc:dd:ee:ff", "event": "arrive"})
        self.assertEqual(Rule.from_dict(rule.to_dict()).to_dict(), rule.to_dict())   # round-trips

    def test_presence_mac_lowercased(self):
        rule = Rule.from_dict(dict(PRESENCE_RULE,
                                   trigger={"type": "presence", "mac": "AA:BB:CC:DD:EE:FF", "event": "leave"}))
        self.assertEqual(rule.trigger, {"type": "presence", "mac": "aa:bb:cc:dd:ee:ff", "event": "leave"})

    def test_bad_presence_mac(self):
        self._bad(dict(PRESENCE_RULE, trigger={"type": "presence", "mac": "not-a-mac", "event": "arrive"}))
        self._bad(dict(PRESENCE_RULE, trigger={"type": "presence", "mac": 123, "event": "arrive"}))  # non-str
        self._bad(dict(PRESENCE_RULE, trigger={"type": "presence", "event": "arrive"}))              # missing

    def test_bad_presence_event(self):
        self._bad(dict(PRESENCE_RULE,
                       trigger={"type": "presence", "mac": "aa:bb:cc:dd:ee:ff", "event": "home"}))


# --- AutomationStore ---------------------------------------------------------

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "automations.json"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _write(self, obj):
        self.path.write_text(json.dumps(obj), encoding="utf-8")

    def test_load_missing_is_empty(self):
        self.assertEqual(AutomationStore.load(self.path).rules, {})

    def test_load_bad_json(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertLogs("hestia.automations", level="WARNING"):
            self.assertEqual(AutomationStore.load(self.path).rules, {})

    def test_load_non_dict_top(self):
        self._write(["a", "list"])
        with self.assertLogs("hestia.automations", level="WARNING"):
            self.assertEqual(AutomationStore.load(self.path).rules, {})

    def test_load_non_list_rules(self):
        self._write({"schema": 1, "rules": {"r1": {}}})
        with self.assertLogs("hestia.automations", level="WARNING"):
            self.assertEqual(AutomationStore.load(self.path).rules, {})

    def test_load_valid(self):
        self._write({"schema": 1, "rules": [SCENE_RULE]})
        store = AutomationStore.load(self.path)
        self.assertEqual(list(store.rules), ["r1"])

    def test_load_skips_invalid_keeps_valid(self):
        self._write({"schema": 1, "rules": [SCENE_RULE, {"id": "bad", "trigger": {}}]})
        with self.assertLogs("hestia.automations", level="WARNING"):
            store = AutomationStore.load(self.path)
        self.assertEqual(list(store.rules), ["r1"])

    def test_load_duplicate_id_last_wins(self):
        first = dict(SCENE_RULE, debounce=1)
        second = dict(SCENE_RULE, debounce=9)
        self._write({"schema": 1, "rules": [first, second]})
        with self.assertLogs("hestia.automations", level="WARNING"):
            store = AutomationStore.load(self.path)
        self.assertEqual(store.rules["r1"].debounce, 9.0)

    def test_load_schema_mismatch_warns_but_loads(self):
        self._write({"schema": 99, "rules": [SCENE_RULE]})
        with self.assertLogs("hestia.automations", level="WARNING"):
            store = AutomationStore.load(self.path)
        self.assertEqual(list(store.rules), ["r1"])

    def test_set_and_delete(self):
        store = AutomationStore(self.path)
        store.set_rule(Rule.from_dict(SCENE_RULE))
        self.assertTrue(store.dirty)
        self.assertEqual(store.snapshot()[0]["id"], "r1")
        self.assertTrue(store.delete_rule("r1"))
        self.assertEqual(store.rules, {})
        self.assertFalse(store.delete_rule("missing"))     # absent → False

    def test_serialize_round_trip(self):
        store = AutomationStore(self.path)
        store.set_rule(Rule.from_dict(SCENE_RULE))
        store.write_payload(store.serialize())
        reloaded = AutomationStore.load(self.path)
        self.assertEqual(reloaded.snapshot(), store.snapshot())

    def test_serialize_round_trip_time_rule(self):
        store = AutomationStore(self.path)
        store.set_rule(Rule.from_dict(dict(TIME_RULE, trigger={"type": "time", "at": "7:5", "days": [0, 2]})))
        store.write_payload(store.serialize())
        reloaded = AutomationStore.load(self.path)
        self.assertEqual(reloaded.snapshot(), store.snapshot())                 # public schema round-trips
        self.assertEqual(reloaded.rules["morning"].trigger,
                         {"type": "time", "at": "07:05", "days": [0, 2]})       # canonical, no hour/minute

    def test_save_clears_dirty(self):
        store = AutomationStore(self.path)
        store.set_rule(Rule.from_dict(SCENE_RULE))
        store.save()
        self.assertFalse(store.dirty)
        self.assertTrue(self.path.exists())

    def test_write_payload_cleans_tmp_on_oserror(self):
        store = AutomationStore(self.path)
        with mock.patch("hestia.automations.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.write_payload(b"{}")
        self.assertFalse(self.path.exists())                       # never landed
        self.assertEqual(list(self.tmp.glob("*.tmp")), [])         # temp file cleaned up


# --- AutomationEngine --------------------------------------------------------

class EngineTriggerTests(unittest.TestCase):
    def test_scene_match_fires(self):
        eng = _engine(SCENE_RULE)
        frames = eng.on_event(_rt(), 2, {}, {"id": 3, "kind": "scene"})
        self.assertEqual(len(frames), 1)

    def test_scene_misses(self):
        eng = _engine(SCENE_RULE)
        rt = _rt()
        self.assertEqual(eng.on_event(rt, 9, {}, {"id": 3, "kind": "scene"}), [])   # wrong node
        self.assertEqual(eng.on_event(rt, 2, {}, {"id": 5, "kind": "scene"}), [])   # wrong scene id
        self.assertEqual(eng.on_event(rt, 2, {"door": "open"}, None), [])           # not a scene event

    def test_state_edge_fires_once_then_recovers(self):
        eng = _engine(STATE_RULE)
        rt = _rt()
        self.assertEqual(eng.on_event(rt, 7, {"temperature": 19}, None), [])        # 19: not < 18
        self.assertEqual(len(eng.on_event(rt, 7, {"temperature": 17}, None)), 1)    # 17: false→true edge
        self.assertEqual(eng.on_event(rt, 7, {"temperature": 16}, None), [])        # 16: still true, no edge
        self.assertEqual(eng.on_event(rt, 7, {"temperature": 20}, None), [])        # 20: back to false
        self.assertEqual(len(eng.on_event(rt, 7, {"temperature": 15}, None)), 1)    # 15: edge again

    def test_state_node_or_field_mismatch(self):
        eng = _engine(STATE_RULE)
        rt = _rt()
        self.assertEqual(eng.on_event(rt, 9, {"temperature": 17}, None), [])        # wrong node
        self.assertEqual(eng.on_event(rt, 7, {"level": 5}, None), [])               # field not in changed

    def test_no_rules_no_node(self):
        self.assertEqual(_engine().on_event(_rt(), None, {}, None), [])

    def test_time_rule_ignored_by_on_event(self):
        eng = _engine(TIME_RULE)
        self.assertEqual(eng.on_event(_rt(), 2, {}, {"id": 3, "kind": "scene"}), [])


class EngineTimeTests(unittest.TestCase):
    def test_fires_at_matching_minute(self):
        eng = _engine(TIME_RULE)
        self.assertEqual(len(eng.on_time(_rt(), MON_0730)), 1)

    def test_no_fire_at_different_minute(self):
        eng = _engine(TIME_RULE)
        self.assertEqual(eng.on_time(_rt(), MON_0730.replace(minute=31)), [])

    def test_days_filter(self):
        eng = _engine(dict(TIME_RULE, trigger={"type": "time", "at": "07:30", "days": [0]}))  # Monday only
        self.assertEqual(len(eng.on_time(_rt(), MON_0730)), 1)                       # Monday → fires
        self.assertEqual(eng.on_time(_rt(), MON_0730 + datetime.timedelta(days=1)), [])  # Tuesday → no

    def test_slot_dedup_then_refire_next_day(self):
        eng = _engine(TIME_RULE)
        rt = _rt()
        self.assertEqual(len(eng.on_time(rt, MON_0730)), 1)          # first this minute
        self.assertEqual(eng.on_time(rt, MON_0730), [])             # same minute → deduped
        self.assertEqual(len(eng.on_time(rt, MON_0730 + datetime.timedelta(days=1))), 1)  # new slot

    def test_conditions_gate_retries_within_minute(self):
        eng = _engine(dict(TIME_RULE, conditions=[{"node": 10, "field": "switch", "op": "eq", "value": True}]))
        rt = _rt()
        self.assertEqual(eng.on_time(rt, MON_0730), [])             # condition false → slot NOT consumed
        rt.state.switches[10] = True
        self.assertEqual(len(eng.on_time(rt, MON_0730)), 1)        # same minute, condition now met → fires
        self.assertEqual(eng.on_time(rt, MON_0730), [])            # and then deduped for the rest of the minute

    def test_disabled_and_mode_filter(self):
        self.assertEqual(_engine(dict(TIME_RULE, enabled=False)).on_time(_rt(), MON_0730), [])
        self.assertEqual(_engine(dict(TIME_RULE, modes=["standalone"])).on_time(_rt("proxy"), MON_0730), [])

    def test_non_time_rule_ignored(self):
        self.assertEqual(_engine(SCENE_RULE).on_time(_rt(), MON_0730), [])


CRON_RULE = {       # fires Monday 07:30 — MON_0730 matches
    "id": "cr",
    "trigger": {"type": "cron", "expr": "30 7 * * 1"},
    "actions": [{"op": "switch", "node": 5, "on": True}],
}


class CronIntTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(automations._cron_int("5"), 5)
        self.assertEqual(automations._cron_int("05"), 5)            # leading zero ok

    def test_rejects_non_ascii_int(self):
        for bad in ("+5", "1_0", "５", " 5 ", "", "-1", "x"):   # ５ = full-width '5'
            with self.assertRaises(ValueError):
                automations._cron_int(bad)


class CronFieldTests(unittest.TestCase):
    def P(self, field, lo=0, hi=59):
        return automations._parse_cron_field(field, lo, hi)

    def test_forms(self):
        self.assertEqual(self.P("*"), frozenset(range(60)))
        self.assertEqual(self.P("5"), {5})
        self.assertEqual(self.P("1-5"), {1, 2, 3, 4, 5})
        self.assertEqual(self.P("*/15"), {0, 15, 30, 45})
        self.assertEqual(self.P("1-10/2"), {1, 3, 5, 7, 9})
        self.assertEqual(self.P("5/15"), {5, 20, 35, 50})          # n/s = n..max step s (extension)
        self.assertEqual(self.P("1,3,5-7"), {1, 3, 5, 6, 7})

    def test_rejects(self):
        for bad in (",,", "5-1", "60", "*/0", "5/0", "1-2-3", "1.5", "", "*/2.5", "1-", "-", "+5"):
            with self.assertRaises(ValueError):
                self.P(bad)
        with self.assertRaises(ValueError):
            self.P("-5")                                            # leading '-' → empty left token


class CronValidationTests(unittest.TestCase):
    def test_non_string(self):
        with self.assertRaises(ValueError):
            automations._validate_cron(5)

    def test_wrong_field_count(self):
        with self.assertRaises(ValueError):
            automations._validate_cron("* * * *")                  # 4
        with self.assertRaises(ValueError):
            automations._validate_cron("* * * * * *")              # 6

    def test_canonical_collapses_whitespace(self):
        self.assertEqual(automations._validate_cron("  */5   *  * * 1-5 "), "*/5 * * * 1-5")

    def test_field_error_names_the_field(self):
        with self.assertRaisesRegex(ValueError, "99"):
            automations._validate_cron("99 * * * *")

    def test_rule_round_trip(self):
        rule = Rule.from_dict(dict(CRON_RULE, trigger={"type": "cron", "expr": "*/15 9-17 * * 1-5"}))
        self.assertEqual(rule.trigger, {"type": "cron", "expr": "*/15 9-17 * * 1-5"})
        self.assertNotIn("days", rule.trigger)                     # cron owns dow
        self.assertEqual(Rule.from_dict(rule.to_dict()).to_dict(), rule.to_dict())  # idempotent

    def test_round_trip_extension_and_lists(self):
        # the `n/s` extension + comma-lists + range-steps survive validation + round-trip unchanged
        # (no numeric canonicalisation — only whitespace collapses).
        expr = "5/15 0-23/2 1,15 * 1,3,5-7"
        rule = Rule.from_dict(dict(CRON_RULE, trigger={"type": "cron", "expr": expr}))
        self.assertEqual(rule.trigger["expr"], expr)
        self.assertEqual(Rule.from_dict(rule.to_dict()).to_dict(), rule.to_dict())


class CronMatchTests(unittest.TestCase):
    def M(self, expr, when):
        return automations._cron_match(expr, when)

    def at(self, y, mo, d, h, mi):
        return datetime.datetime(y, mo, d, h, mi)

    def test_minute_hour_month(self):
        self.assertTrue(self.M("30 9 * * *", self.at(2026, 5, 31, 9, 30)))
        self.assertFalse(self.M("30 9 * * *", self.at(2026, 5, 31, 9, 31)))   # minute miss
        self.assertFalse(self.M("30 9 * * *", self.at(2026, 5, 31, 8, 30)))   # hour miss
        self.assertFalse(self.M("30 9 3 * *", self.at(2026, 5, 31, 9, 30)))   # dom miss (single restricted)
        self.assertFalse(self.M("0 0 * 6 *", self.at(2026, 5, 31, 0, 0)))     # month miss

    def test_sunday_zero_and_seven(self):
        sunday = self.at(2026, 5, 31, 0, 0)                        # 2026-05-31 is a Sunday
        self.assertEqual(sunday.weekday(), 6)
        self.assertTrue(self.M("0 0 * * 0", sunday))               # 0 = Sunday
        self.assertTrue(self.M("0 0 * * 7", sunday))               # 7 = Sunday (normalised)
        self.assertFalse(self.M("0 0 * * 1", sunday))              # Monday-only, but it's Sunday

    def test_sunday_seven_inside_range_and_list(self):
        # the 7→0 normalisation must work when 7 arrives via a RANGE or a LIST, not just bare "7".
        sunday = self.at(2026, 5, 31, 0, 0)
        self.assertTrue(self.M("0 0 * * 6-7", sunday))             # Sat-Sun range → fires on Sunday
        self.assertTrue(self.M("0 0 * * 5-7", sunday))             # Fri-Sun range
        self.assertTrue(self.M("0 0 * * 1,7", sunday))             # list containing 7
        self.assertFalse(self.M("0 0 * * 5-6", sunday))            # Fri-Sat (no Sunday) → no

    def test_star_detection_is_positional_first_token(self):
        # Vixie keys the dom/dow OR-vs-AND on the FIRST token only (entry.c `ch == '*'`), so a list
        # whose star is NOT first is "restricted" (OR), while a leading star is "starred" (AND) — even
        # for the same value set. Pin it so a naive "any element starred" refactor can't slip through.
        tue = self.at(2026, 6, 2, 0, 0)                            # day=2 (∉ */2), Tuesday
        self.assertTrue(self.M("0 0 5,*/2 * 2", tue))              # leading "5" → OR → Tuesday matches dow
        self.assertFalse(self.M("0 0 */2,5 * 2", tue))             # leading "*" → AND → day 2 ∉ {odd,5}

    def test_dom_dow_or_when_both_restricted(self):
        monday = self.at(2026, 6, 1, 0, 0)                         # 2026-06-01, day=1, Monday
        self.assertEqual(monday.weekday(), 0)
        self.assertTrue(self.M("0 0 1 * 3", monday))               # dom=1 matches (OR), dow=Wed doesn't
        self.assertTrue(self.M("0 0 5 * 1", monday))               # dow=Mon matches (OR), dom=5 doesn't
        self.assertFalse(self.M("0 0 5 * 3", monday))              # neither dom=5 nor dow=Wed

    def test_star_field_uses_and(self):
        monday = self.at(2026, 6, 1, 0, 0)                         # day=1 (odd), Monday
        self.assertTrue(self.M("0 0 * * 1", monday))               # dom '*' → AND → just Monday
        self.assertTrue(self.M("0 0 1 * *", monday))               # dow '*' → AND → just dom=1
        # Vixie star gotcha: '*/2' is starred → AND (not OR) with dow.
        self.assertTrue(self.M("0 0 */2 * 1", monday))             # day 1 ∈ */2 AND Monday → fires
        self.assertFalse(self.M("0 0 */2 * 1", self.at(2026, 6, 2, 0, 0)))  # day 2 ∉ */2 → AND fails
        self.assertTrue(self.M("0 0 * * *", monday))               # both '*' → every day

    def test_boundary_extremes(self):
        self.assertTrue(self.M("59 23 31 12 *", self.at(2026, 12, 31, 23, 59)))   # all field maxima
        self.assertTrue(self.M("0 0 1 1 *", self.at(2026, 1, 1, 0, 0)))           # all field minima
        self.assertFalse(self.M("59 23 31 12 *", self.at(2026, 12, 31, 23, 58)))  # one minute short of 59


class EngineCronTests(unittest.TestCase):
    def test_fires_at_matching_minute(self):
        self.assertEqual(len(_engine(CRON_RULE).on_time(_rt(), MON_0730)), 1)

    def test_no_fire_off_minute(self):
        self.assertEqual(_engine(CRON_RULE).on_time(_rt(), MON_0730.replace(minute=31)), [])

    def test_no_fire_wrong_weekday(self):
        # CRON_RULE = "30 7 * * 1" (Monday). cron owns dow (no `days` key), so on_time must skip Tuesday.
        self.assertEqual(_engine(CRON_RULE).on_time(_rt(), MON_0730 + datetime.timedelta(days=1)), [])

    def test_slot_dedup_then_refire(self):
        eng = _engine(CRON_RULE)
        rt = _rt()
        self.assertEqual(len(eng.on_time(rt, MON_0730)), 1)
        self.assertEqual(eng.on_time(rt, MON_0730), [])                       # same minute → deduped
        self.assertEqual(len(eng.on_time(rt, MON_0730 + datetime.timedelta(days=7))), 1)  # next Monday

    def test_conditions_gate(self):
        eng = _engine(dict(CRON_RULE,
                           conditions=[{"node": 10, "field": "switch", "op": "eq", "value": True}]))
        rt = _rt()
        self.assertEqual(eng.on_time(rt, MON_0730), [])                      # condition false → slot free
        rt.state.switches[10] = True
        self.assertEqual(len(eng.on_time(rt, MON_0730)), 1)                  # now met → fires

    def test_disabled_and_mode_filter(self):
        self.assertEqual(_engine(dict(CRON_RULE, enabled=False)).on_time(_rt(), MON_0730), [])
        self.assertEqual(_engine(dict(CRON_RULE, modes=["standalone"])).on_time(_rt("proxy"), MON_0730), [])

    def test_cron_rule_ignored_by_on_event(self):
        self.assertEqual(_engine(CRON_RULE).on_event(_rt(), 2, {}, {"id": 3, "kind": "scene"}), [])


class SunEventTests(unittest.TestCase):
    """`sun_event_utc` vs independent ephemeris (sunrise-sunset.org), within 240 s — tight enough to
    catch a sign/formula error (hours off) yet allow the ~2-3 min refraction-model residual."""

    def _assert(self, lat, lon, d, event, expected_iso):
        got = automations.sun_event_utc(lat, lon, d, event)
        self.assertIsNotNone(got)
        self.assertEqual(got.tzinfo, datetime.timezone.utc)
        exp = datetime.datetime.fromisoformat(expected_iso)
        self.assertLess(abs((got - exp).total_seconds()), 240, f"{event} {got} vs {exp}")

    def test_london_summer(self):
        d = datetime.date(2024, 6, 21)
        self._assert(51.4769, -0.0005, d, "sunrise", "2024-06-21T03:40:42+00:00")
        self._assert(51.4769, -0.0005, d, "sunset", "2024-06-21T20:23:09+00:00")

    def test_nyc_winter(self):
        d = datetime.date(2024, 12, 21)
        self._assert(40.7128, -74.0060, d, "sunrise", "2024-12-21T12:15:14+00:00")
        self._assert(40.7128, -74.0060, d, "sunset", "2024-12-21T21:33:38+00:00")

    def test_sydney_date_wrap(self):                 # UTC+10: sunrise lands on the PREVIOUS UTC date
        d = datetime.date(2024, 6, 21)
        self._assert(-33.8688, 151.2093, d, "sunrise", "2024-06-20T20:58:40+00:00")
        self._assert(-33.8688, 151.2093, d, "sunset", "2024-06-21T06:55:19+00:00")

    def test_singapore_equinox(self):
        d = datetime.date(2024, 3, 20)
        self._assert(1.3521, 103.8198, d, "sunrise", "2024-03-19T23:07:42+00:00")
        self._assert(1.3521, 103.8198, d, "sunset", "2024-03-20T11:16:31+00:00")

    def test_polar_day_returns_none(self):           # Tromsø midsummer: sun never sets (cos_ha < -1)
        d = datetime.date(2024, 6, 21)
        self.assertIsNone(automations.sun_event_utc(69.6492, 18.9553, d, "sunrise"))
        self.assertIsNone(automations.sun_event_utc(69.6492, 18.9553, d, "sunset"))

    def test_polar_night_returns_none(self):         # Tromsø midwinter: sun never rises (cos_ha > 1)
        d = datetime.date(2024, 12, 21)
        self.assertIsNone(automations.sun_event_utc(69.6492, 18.9553, d, "sunrise"))
        self.assertIsNone(automations.sun_event_utc(69.6492, 18.9553, d, "sunset"))


_UTC = datetime.timezone.utc


class EngineSunTests(unittest.TestCase):
    """`on_time` sun branch. `TZ=UTC` (local==UTC) makes `now.astimezone(utc)` deterministic, and we
    patch `sun_event_utc` so the candidate-date matching is tested without real astronomy/tz."""

    def setUp(self):
        self._tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()

    def tearDown(self):
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()

    @staticmethod
    def _rt_sun(lat=51.5, lon=-0.1, mode="proxy"):
        rt = _rt(mode)
        rt.lat, rt.lon = lat, lon
        return rt

    @staticmethod
    def _patch(target_date, want):
        """Fake `sun_event_utc`: return `want` (aware UTC) only for `target_date`, else a far-off dt."""
        def fake(lat, lon, d, event):
            return want if d == target_date else datetime.datetime(2000, 1, 1, tzinfo=_UTC)
        return mock.patch("hestia.automations.sun_event_utc", side_effect=fake)

    def _fires_via_delta(self, delta, offset=30, days=None, rt=None):
        now = datetime.datetime(2026, 6, 3, 12, 0)               # Wed; local==UTC under TZ=UTC
        now_utc = now.astimezone(_UTC)
        target = now_utc.date() + datetime.timedelta(days=delta)
        want = now_utc - datetime.timedelta(minutes=offset)      # so want + offset == now_utc
        tg = {"type": "sun", "event": "sunset", "offset_min": offset}
        if days is not None:
            tg["days"] = days
        eng = _engine(dict(SUN_RULE, trigger=tg))
        with self._patch(target, want):
            return eng.on_time(rt or self._rt_sun(), now)

    def test_fires_via_each_candidate_delta(self):
        for delta in (-2, -1, 0, 1, 2):              # the full exhaustive window, each as the matching arm
            with self.subTest(delta=delta):
                self.assertEqual(len(self._fires_via_delta(delta)), 1)

    def test_no_candidate_matches_no_fire(self):
        now = datetime.datetime(2026, 6, 3, 12, 0)
        eng = _engine(SUN_RULE)
        with self._patch(datetime.date(1999, 1, 1), datetime.datetime(2026, 6, 3, 12, 0, tzinfo=_UTC)):
            self.assertEqual(eng.on_time(self._rt_sun(), now), [])     # no candidate date matches → return False

    def test_polar_candidates_no_fire(self):
        now = datetime.datetime(2026, 6, 3, 12, 0)
        eng = _engine(SUN_RULE)
        with mock.patch("hestia.automations.sun_event_utc", return_value=None):   # every candidate polar
            self.assertEqual(eng.on_time(self._rt_sun(), now), [])

    def test_unconfigured_location_no_fire(self):
        self.assertEqual(self._fires_via_delta(0, rt=self._rt_sun(lat=None)), [])  # lat unset
        self.assertEqual(self._fires_via_delta(0, rt=self._rt_sun(lon=None)), [])  # lon unset

    def test_days_filter(self):
        self.assertEqual(self._fires_via_delta(0, days=[0]), [])      # now is Wed (2); Monday-only → no fire
        self.assertEqual(len(self._fires_via_delta(0, days=[2])), 1)  # Wednesday → fires

    def test_slot_dedup(self):
        now = datetime.datetime(2026, 6, 3, 12, 0)
        now_utc = now.astimezone(_UTC)
        rt = self._rt_sun()
        eng = _engine(dict(SUN_RULE, trigger={"type": "sun", "event": "sunset", "offset_min": 0}))
        with self._patch(now_utc.date(), now_utc):
            self.assertEqual(len(eng.on_time(rt, now)), 1)           # first this minute
            self.assertEqual(eng.on_time(rt, now), [])               # same minute → deduped

    def test_conditions_gate_retries_within_minute(self):
        now = datetime.datetime(2026, 6, 3, 12, 0)
        now_utc = now.astimezone(_UTC)
        rt = self._rt_sun()
        eng = _engine(dict(SUN_RULE, trigger={"type": "sun", "event": "sunset", "offset_min": 0},
                           conditions=[{"node": 10, "field": "switch", "op": "eq", "value": True}]))
        with self._patch(now_utc.date(), now_utc):
            self.assertEqual(eng.on_time(rt, now), [])               # condition false → slot NOT consumed
            rt.state.switches[10] = True
            self.assertEqual(len(eng.on_time(rt, now)), 1)           # same minute, now met → fires

    def test_disabled_and_mode_filter(self):
        now = datetime.datetime(2026, 6, 3, 12, 0)
        now_utc = now.astimezone(_UTC)
        tg = {"type": "sun", "event": "sunset", "offset_min": 0}
        with self._patch(now_utc.date(), now_utc):
            self.assertEqual(_engine(dict(SUN_RULE, enabled=False, trigger=tg))
                             .on_time(self._rt_sun(), now), [])                       # disabled → no fire
            self.assertEqual(_engine(dict(SUN_RULE, modes=["standalone"], trigger=tg))
                             .on_time(self._rt_sun(mode="proxy"), now), [])           # mode-excluded → no fire

    def test_two_sun_rules_reuse_now_utc(self):      # 2nd rule hits the memoized `now_utc is not None`
        now = datetime.datetime(2026, 6, 3, 12, 0)
        now_utc = now.astimezone(_UTC)
        tg = {"type": "sun", "event": "sunset", "offset_min": 0}
        eng = _engine(dict(SUN_RULE, id="a", trigger=tg), dict(SUN_RULE, id="b", trigger=tg))
        with self._patch(now_utc.date(), now_utc):
            self.assertEqual(len(eng.on_time(self._rt_sun(), now)), 2)   # both fire; 2nd reuses now_utc

    def test_fires_under_non_utc_dst_timezone(self):
        """Real deployments aren't UTC. With the server in a DST zone (Europe/Dublin, summer = IST =
        UTC+1), a sun rule must still fire: `on_time` converts naive-local `now` → UTC (offset
        applied) before matching the (UTC) event instant."""
        os.environ["TZ"] = "Europe/Dublin"
        time.tzset()
        try:
            now = datetime.datetime(2026, 6, 21, 22, 0)       # 22:00 IST
            now_utc = now.astimezone(_UTC)
            self.assertEqual((now_utc.hour, now_utc.minute), (21, 0))   # DST +1h actually applied
            eng = _engine(dict(SUN_RULE, trigger={"type": "sun", "event": "sunset", "offset_min": 0}))
            with self._patch(now_utc.date(), now_utc):
                self.assertEqual(len(eng.on_time(self._rt_sun(), now)), 1)
        finally:
            os.environ["TZ"] = "UTC"                            # restore the class fixture
            time.tzset()

    def test_on_event_ignores_sun_rule(self):        # sun is scheduler-driven, never event-driven
        self.assertEqual(_engine(SUN_RULE).on_event(self._rt_sun(), 4, {"level": 0}, None), [])


class ReadPresentMacsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "leases"
        self.now = 1_000_000_000

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_present_static_included_expired_excluded(self):
        self.path.write_text(
            f"{self.now + 3600} AA:BB:CC:DD:EE:FF 192.0.2.2 phone *\n"   # future → present (lowercased)
            f"{self.now - 10} 11:22:33:44:55:66 192.0.2.3 old *\n"        # expired → excluded
            f"0 de:ad:be:ef:00:01 192.0.2.4 static *\n",                 # expiry 0 (static) → present
            encoding="utf-8")
        self.assertEqual(automations.read_present_macs(self.path, self.now),
                         {"aa:bb:cc:dd:ee:ff", "de:ad:be:ef:00:01"})

    def test_malformed_lines_skipped(self):
        self.path.write_text("garbage\nonlytoken\nnotanumber aa:bb:cc:dd:ee:ff x\n\n", encoding="utf-8")
        self.assertEqual(automations.read_present_macs(self.path, self.now), set())   # all skipped

    def test_empty_file_is_empty_set(self):
        self.path.write_text("", encoding="utf-8")
        self.assertEqual(automations.read_present_macs(self.path, self.now), set())

    def test_missing_file_is_none(self):              # unreadable → None (≠ empty set)
        self.assertIsNone(automations.read_present_macs(self.tmp / "nope", self.now))

    def test_non_utf8_hostname_tolerated(self):
        # dnsmasq copies the client hostname (DHCP opt 12) verbatim — it may not be UTF-8. A bad
        # byte must NOT raise (UnicodeDecodeError would escape into _scheduler and kill it); the
        # expiry + MAC fields are ASCII, so the MAC still parses.
        self.path.write_bytes(f"{self.now + 3600} aa:bb:cc:dd:ee:ff 192.0.2.2 ".encode("ascii")
                              + b"\xff\xfe-host *\n")
        self.assertEqual(automations.read_present_macs(self.path, self.now), {"aa:bb:cc:dd:ee:ff"})


class EnginePresenceTests(unittest.TestCase):
    MAC = "aa:bb:cc:dd:ee:ff"

    def _leave(self):
        return dict(PRESENCE_RULE, trigger={"type": "presence", "mac": self.MAC, "event": "leave"})

    def test_none_no_fire_and_baseline_untouched(self):
        eng = _engine(PRESENCE_RULE)
        self.assertEqual(eng.on_presence(_rt(), None), [])
        self.assertEqual(eng._last_presence, {})         # unreadable → edges untouched, no baseline set

    def test_baseline_then_arrival_then_no_reedge(self):
        eng = _engine(PRESENCE_RULE)
        rt = _rt()
        self.assertEqual(eng.on_presence(rt, set()), [])            # first obs → baseline absent, no fire
        self.assertEqual(len(eng.on_presence(rt, {self.MAC})), 1)   # absent→present = arrival → fires
        self.assertEqual(eng.on_presence(rt, {self.MAC}), [])       # still present → no edge

    def test_arrive_rule_ignores_departure_edge(self):
        eng = _engine(PRESENCE_RULE)
        rt = _rt()
        eng.on_presence(rt, {self.MAC})                             # baseline present
        self.assertEqual(eng.on_presence(rt, set()), [])            # present→absent: wrong direction for arrive

    def test_leave_rule_fires_on_departure_only(self):
        rt = _rt()
        eng = _engine(self._leave())
        eng.on_presence(rt, {self.MAC})                             # baseline present
        self.assertEqual(len(eng.on_presence(rt, set())), 1)        # present→absent = leave → fires
        eng2 = _engine(self._leave())
        eng2.on_presence(rt, set())                                 # baseline absent
        self.assertEqual(eng2.on_presence(rt, {self.MAC}), [])      # absent→present: wrong direction for leave

    def test_disabled_and_mode_filter(self):
        rt = _rt("proxy")
        self.assertEqual(_engine(dict(PRESENCE_RULE, enabled=False)).on_presence(rt, {self.MAC}), [])
        self.assertEqual(_engine(dict(PRESENCE_RULE, modes=["standalone"])).on_presence(rt, {self.MAC}), [])

    def test_non_presence_rule_ignored(self):
        self.assertEqual(_engine(SCENE_RULE).on_presence(_rt(), {self.MAC}), [])

    def test_condition_gates_the_edge(self):
        eng = _engine(dict(PRESENCE_RULE,
                           conditions=[{"node": 10, "field": "switch", "op": "eq", "value": True}]))
        rt = _rt()
        eng.on_presence(rt, set())                                 # baseline absent
        self.assertEqual(eng.on_presence(rt, {self.MAC}), [])       # arrival edge but condition false → no frames

    def test_reset_runtime_clears_presence(self):
        eng = _engine(PRESENCE_RULE)
        rt = _rt()
        eng.on_presence(rt, {self.MAC})                            # baseline present
        eng.reset_runtime("arrive-home")
        self.assertNotIn("arrive-home", eng._last_presence)        # baseline dropped → re-baselines next obs

    def test_has_presence_rules(self):
        self.assertTrue(_engine(PRESENCE_RULE).has_presence_rules())
        self.assertFalse(_engine(SCENE_RULE).has_presence_rules())
        self.assertFalse(_engine().has_presence_rules())


class EngineGlobalTests(unittest.TestCase):
    """`on_global` — poller-driven threshold triggers on a node-less (global) state field."""

    def test_edge_fires_once_then_rearms(self):
        eng = _engine(CRIB_RULE)
        rt = _rt()
        self.assertEqual(eng.on_global(rt, "crib_temp", 22.0), [])          # 22 ≯ 24 → no edge
        self.assertEqual(len(eng.on_global(rt, "crib_temp", 25.6)), 1)      # 22→25.6 crosses → fires
        self.assertEqual(eng.on_global(rt, "crib_temp", 26.0), [])          # stays true → no re-fire
        self.assertEqual(eng.on_global(rt, "crib_temp", 20.0), [])          # back below → re-arm
        self.assertEqual(len(eng.on_global(rt, "crib_temp", 25.0)), 1)      # false→true again → fires

    def test_non_matching_field_noop(self):
        self.assertEqual(_engine(CRIB_RULE).on_global(_rt(), "outdoor_temp", 30.0), [])

    def test_non_state_rule_ignored(self):
        self.assertEqual(_engine(SCENE_RULE).on_global(_rt(), "crib_temp", 30.0), [])

    def test_disabled_and_mode_filter(self):
        rt = _rt("proxy")
        self.assertEqual(_engine(dict(CRIB_RULE, enabled=False)).on_global(rt, "crib_temp", 30.0), [])
        self.assertEqual(_engine(dict(CRIB_RULE, modes=["standalone"])).on_global(rt, "crib_temp", 30.0), [])

    def test_condition_suppresses_but_consumes_edge(self):
        eng = _engine(dict(CRIB_RULE,
                           conditions=[{"node": 10, "field": "switch", "op": "eq", "value": True}]))
        rt = _rt()
        self.assertEqual(eng.on_global(rt, "crib_temp", 25.6), [])  # edge but condition false → no frames
        rt.state.switches[10] = True
        self.assertEqual(eng.on_global(rt, "crib_temp", 26.0), [])  # still true (no new edge) → no fire
        eng.on_global(rt, "crib_temp", 20.0)                        # re-arm (below threshold)
        self.assertEqual(len(eng.on_global(rt, "crib_temp", 25.0)), 1)  # fresh edge + condition true → fires

    def test_reset_runtime_clears_edge(self):
        eng = _engine(CRIB_RULE)
        rt = _rt()
        eng.on_global(rt, "crib_temp", 25.6)                        # fires, _last_match → True
        eng.reset_runtime("crib-hot")
        self.assertEqual(len(eng.on_global(rt, "crib_temp", 25.6)), 1)  # edge re-armed → fires again

    def test_on_event_skips_global_trigger(self):
        # the poller owns global fields; a device event must never fire a global state rule.
        eng = _engine(CRIB_RULE)
        self.assertEqual(eng.on_event(_rt(), 5, {"crib_temp": 30}, None), [])

    def test_crib_temp_as_condition(self):
        # a scene rule gated by a global condition (exercises _condition_ok's node-less .get path).
        eng = _engine(dict(SCENE_RULE,
                           conditions=[{"field": "crib_temp", "op": "lt", "value": 18}]))
        rt = _rt()
        self.assertEqual(eng.on_event(rt, 2, {}, {"id": 3, "kind": "scene"}), [])   # crib_temp None → ≮18
        rt.state.crib_temp = 16.0
        self.assertEqual(len(eng.on_event(rt, 2, {}, {"id": 3, "kind": "scene"})), 1)  # 16<18 → fires

    def test_outdoor_temp_second_global_field(self):
        # a SECOND global field rides the same generic mechanism end-to-end.
        spec = {"id": "cold",
                "trigger": {"type": "state", "field": "outdoor_temp", "op": "lt", "value": 0},
                "actions": [{"op": "switch", "node": 9, "on": True}]}
        rule = Rule.from_dict(spec)
        self.assertNotIn("node", rule.trigger)                     # node-less, like crib_temp
        eng = _engine(spec)
        rt = _rt()
        self.assertEqual(eng.on_global(rt, "outdoor_temp", 5.0), [])       # 5 ≮ 0 → no edge
        self.assertEqual(len(eng.on_global(rt, "outdoor_temp", -3.0)), 1)  # 5→-3 crosses <0 → fires
        self.assertEqual(eng.on_global(rt, "crib_temp", -3.0), [])         # different global field → no-op


class EngineConditionTests(unittest.TestCase):
    def _rule(self):
        return dict(SCENE_RULE, conditions=[{"node": 10, "field": "switch", "op": "eq", "value": False}])

    def test_condition_passes(self):
        eng = _engine(self._rule())
        rt = _rt()
        rt.state.switches[10] = False
        self.assertEqual(len(eng.on_event(rt, 2, {}, {"id": 3, "kind": "scene"})), 1)

    def test_condition_fails(self):
        eng = _engine(self._rule())
        rt = _rt()
        rt.state.switches[10] = True
        self.assertEqual(eng.on_event(rt, 2, {}, {"id": 3, "kind": "scene"}), [])


class EngineGuardTests(unittest.TestCase):
    def test_disabled_rule_skipped(self):
        eng = _engine(dict(SCENE_RULE, enabled=False))
        self.assertEqual(eng.on_event(_rt(), 2, {}, {"id": 3, "kind": "scene"}), [])

    def test_mode_filter(self):
        eng = _engine(dict(SCENE_RULE, modes=["standalone"]))
        self.assertEqual(eng.on_event(_rt("proxy"), 2, {}, {"id": 3, "kind": "scene"}), [])
        self.assertEqual(len(eng.on_event(_rt("standalone"), 2, {}, {"id": 3, "kind": "scene"})), 1)

    def test_debounce(self):
        t = [100.0]
        eng = _engine(dict(SCENE_RULE, debounce=5), clock=lambda: t[0])
        rt = _rt()
        self.assertEqual(len(eng.on_event(rt, 2, {}, {"id": 3, "kind": "scene"})), 1)  # first fires
        t[0] = 102.0
        self.assertEqual(eng.on_event(rt, 2, {}, {"id": 3, "kind": "scene"}), [])      # within window
        t[0] = 106.0
        self.assertEqual(len(eng.on_event(rt, 2, {}, {"id": 3, "kind": "scene"})), 1)  # after window

    def test_bad_action_skipped_good_action_runs(self):
        rule = dict(SCENE_RULE, actions=[{"op": "raw"},                       # missing 'hex' → KeyError
                                         {"op": "switch", "node": 14, "on": True}])
        eng = _engine(rule)
        with self.assertLogs("hestia.automations", level="ERROR"):
            frames = eng.on_event(_rt(), 2, {}, {"id": 3, "kind": "scene"})
        self.assertEqual(len(frames), 1)                                       # only the good action

    def test_all_actions_fail_still_advances_debounce(self):
        """If every action errors the rule still counts as fired — debounce advances so a
        broken rule can't spam build_command errors on every matching event."""
        eng = _engine(dict(SCENE_RULE, debounce=5, actions=[{"op": "raw"}]))   # missing 'hex'
        with self.assertLogs("hestia.automations", level="ERROR"):
            self.assertEqual(eng.on_event(_rt(), 2, {}, {"id": 3, "kind": "scene"}), [])
        self.assertIn("r1", eng._last_fired)                                   # debounce advanced


class EngineCacheLifecycleTests(unittest.TestCase):
    def test_set_rule_resets_edge(self):
        eng = _engine(STATE_RULE)
        rt = _rt()
        self.assertEqual(len(eng.on_event(rt, 7, {"temperature": 17}, None)), 1)   # edge fired
        self.assertEqual(eng.on_event(rt, 7, {"temperature": 16}, None), [])       # suppressed by edge
        eng.set_rule(Rule.from_dict(STATE_RULE))                                   # replace same id
        self.assertEqual(len(eng.on_event(rt, 7, {"temperature": 16}, None)), 1)   # edge reset → fires

    def test_set_rule_resets_debounce(self):
        t = [100.0]
        eng = _engine(dict(SCENE_RULE, debounce=5), clock=lambda: t[0])
        rt = _rt()
        self.assertEqual(len(eng.on_event(rt, 2, {}, {"id": 3, "kind": "scene"})), 1)
        eng.set_rule(Rule.from_dict(dict(SCENE_RULE, debounce=5)))                  # clears _last_fired
        self.assertEqual(len(eng.on_event(rt, 2, {}, {"id": 3, "kind": "scene"})), 1)  # fires despite window

    def test_delete_rule_clears_caches(self):
        eng = _engine(STATE_RULE)
        rt = _rt()
        eng.on_event(rt, 7, {"temperature": 17}, None)                             # populate caches
        self.assertIn("cold", eng._last_match)
        self.assertIn("cold", eng._last_fired)
        self.assertTrue(eng.delete_rule("cold"))
        self.assertNotIn("cold", eng._last_match)
        self.assertNotIn("cold", eng._last_fired)

    def test_delete_absent_returns_false(self):
        self.assertFalse(_engine().delete_rule("ghost"))

    def test_set_rule_resets_time_slot(self):
        eng = _engine(TIME_RULE)
        rt = _rt()
        self.assertEqual(len(eng.on_time(rt, MON_0730)), 1)         # fires, slot recorded
        self.assertEqual(eng.on_time(rt, MON_0730), [])            # same minute → deduped
        eng.set_rule(Rule.from_dict(TIME_RULE))                     # replace → reset_runtime
        self.assertEqual(len(eng.on_time(rt, MON_0730)), 1)        # slot cleared → fires again

    def test_delete_rule_clears_time_slot(self):
        eng = _engine(TIME_RULE)
        eng.on_time(_rt(), MON_0730)                                # populate _last_time_fire
        self.assertIn("morning", eng._last_time_fire)
        self.assertTrue(eng.delete_rule("morning"))
        self.assertNotIn("morning", eng._last_time_fire)


class RuleVocabTests(unittest.TestCase):
    """rule_vocab() (the dashboard guided form's grammar source) must mirror the validation constants
    so the form can't drift from Rule.from_dict, and the shapes the form emits must validate."""

    def test_vocab_mirrors_constants(self):
        v = automations.rule_vocab()
        self.assertEqual(set(v["cmp_ops"]), automations._OPS_CMP)
        self.assertEqual(set(v["frame_action_ops"]), automations._FRAME_ACTION_OPS)
        self.assertEqual(set(v["modes"]), automations._VALID_MODES)
        self.assertEqual(set(v["state_fields"]), automations._VALID_STATE_FIELDS)
        for field, is_global in v["state_fields"].items():
            self.assertEqual(is_global, automations._STATE_FIELD_ATTRS[field] is automations._GLOBAL)
        self.assertEqual(v["trigger_types"], list(automations._TRIGGER_TYPES))
        self.assertEqual(v["sun_events"], list(automations._SUN_EVENTS))
        self.assertEqual(v["presence_events"], list(automations._PRESENCE_EVENTS))

    def test_form_built_shapes_validate(self):
        # the exact rule shapes the guided form emits for representative cases must pass Rule.from_dict:
        # global-field state trigger + klima→ir action, presence + condition + off, sun + switch,
        # node-scoped state trigger + thermostat.
        examples = [
            {"id": "a", "enabled": True, "modes": ["proxy", "standalone"], "debounce": 0,
             "trigger": {"type": "state", "field": "outdoor_temp", "op": "gt", "value": 26},
             "conditions": [],
             "actions": [{"op": "ir", "file": "/ext/infrared/klima.ir", "button": "on_cool_24"}]},
            {"id": "b", "enabled": True, "modes": ["proxy"], "debounce": 0,
             "trigger": {"type": "presence", "mac": "aa:bb:cc:dd:ee:ff", "event": "leave"},
             "conditions": [{"field": "outdoor_temp", "op": "lt", "value": 10}],
             "actions": [{"op": "ir", "file": "/ext/infrared/klima.ir", "button": "off"}]},
            {"id": "c", "enabled": False, "modes": ["standalone"], "debounce": 0,
             "trigger": {"type": "sun", "event": "sunset", "offset_min": 0, "days": [4, 5]},
             "conditions": [], "actions": [{"op": "switch", "node": 18, "on": True}]},
            {"id": "d", "enabled": True, "modes": ["proxy", "standalone"], "debounce": 0,
             "trigger": {"type": "state", "field": "temperature", "op": "lt", "value": 18, "node": 5},
             "conditions": [], "actions": [{"op": "thermostat", "node": 5, "celsius": 21.0}]},
        ]
        for ex in examples:
            self.assertEqual(Rule.from_dict(ex).id, ex["id"])   # must not raise


if __name__ == "__main__":
    unittest.main()
