"""Unit tests for the JSON -> SQLite shadow import (hestia.store_sql) — Phase 2 of #57.

Proves the mirror is lossless + idempotent + replace-mirror (deletes absent rows), that
shadow_import is best-effort (never raises), and that the boot helper honours the opt-out.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import select

from hestia import db, proxy, store_sql
from hestia.automations import AutomationStore, Rule
from hestia.registry import Registry

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

RICH_NODE = {
    "type": "blind", "confidence": "confirmed", "type_confirmed": True,
    "name": "Sypialnia", "room": "Piętro", "power": "mains", "battery": 87,
    "first_seen": "2026-01-01T00:00:00Z", "last_seen": "2026-06-01T10:00:00Z",
    "endpoint_names": {"1": "Lewa", "2": "Prawa"}, "scenes": {"3": "1e3200aa"},
}


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "hestia.db"
        self.reg = Registry(self.dir / "r.json", mode="standalone")
        self.reg.nodes = {"5": dict(RICH_NODE), "9": {"type": "switch", "first_seen": "2026-02-02T00:00:00Z"}}
        self.store = AutomationStore(self.dir / "a.json")
        self.store.rules = {"r1": Rule.from_dict(SCENE_RULE), "cold": Rule.from_dict(STATE_RULE)}
        self.users = {"tata": "scrypt$abc", "mama": "scrypt$def"}
        _, self.Session = db.init_db(self.path)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def _mirror(self):
        with db.session_scope(self.Session) as s:
            store_sql.mirror_json_to_db(s, registry=self.reg, store=self.store, users=self.users)

    def test_mirror_is_field_exact(self):
        self._mirror()
        with db.session_scope(self.Session) as s:
            self.assertEqual(s.get(db.AppMeta, "mode").value, "standalone")
            self.assertEqual(json.loads(s.get(db.Node, "5").entry_json), RICH_NODE)
            self.assertEqual(json.loads(s.get(db.Node, "9").entry_json), self.reg.nodes["9"])
            autos = s.execute(select(db.Automation).order_by(db.Automation.position)).scalars().all()
            self.assertEqual([(a.id, a.position) for a in autos], [("r1", 0), ("cold", 1)])
            self.assertEqual(json.loads(autos[0].rule_json), Rule.from_dict(SCENE_RULE).to_dict())
            self.assertEqual(s.get(db.User, "tata").password_hash, "scrypt$abc")
            self.assertEqual(s.get(db.User, "mama").password_hash, "scrypt$def")

    def test_idempotent(self):
        self._mirror()
        snap1 = self._snapshot()
        self._mirror()  # second run exercises the _upsert UPDATE branch
        self.assertEqual(self._snapshot(), snap1)

    def test_replace_mirror_deletes_absent_rows(self):
        self._mirror()
        del self.reg.nodes["9"]
        del self.store.rules["cold"]
        self.users.pop("mama")
        self._mirror()
        with db.session_scope(self.Session) as s:
            self.assertIsNone(s.get(db.Node, "9"))
            self.assertIsNone(s.get(db.Automation, "cold"))
            self.assertIsNone(s.get(db.User, "mama"))
            self.assertEqual(len(s.execute(select(db.Node)).scalars().all()), 1)

    def test_empty_stores_clear_the_mirror(self):
        self._mirror()
        self.reg.nodes = {}
        self.store.rules = {}
        self.users = {}
        self._mirror()  # not_in(empty) -> delete all
        with db.session_scope(self.Session) as s:
            self.assertEqual(len(s.execute(select(db.Node)).scalars().all()), 0)
            self.assertEqual(len(s.execute(select(db.Automation)).scalars().all()), 0)
            self.assertEqual(len(s.execute(select(db.User)).scalars().all()), 0)

    def _snapshot(self) -> dict:
        with db.session_scope(self.Session) as s:
            return {
                "meta": {m.key: m.value for m in s.execute(select(db.AppMeta)).scalars()},
                "nodes": {n.key: n.entry_json for n in s.execute(select(db.Node)).scalars()},
                "autos": {(a.id, a.position): a.rule_json for a in s.execute(select(db.Automation)).scalars()},
                "users": {u.username: u.password_hash for u in s.execute(select(db.User)).scalars()},
            }


class ShadowImportTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "hestia.db"
        self.reg = Registry(self.dir / "r.json")
        self.reg.nodes = {"5": dict(RICH_NODE)}
        self.store = AutomationStore(self.dir / "a.json")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_success_populates_db(self):
        self.assertTrue(store_sql.shadow_import(self.reg, self.store, {"tata": "h"}, path=self.path))
        _, Session = db.init_db(self.path)
        with db.session_scope(Session) as s:
            self.assertEqual(json.loads(s.get(db.Node, "5").entry_json), RICH_NODE)

    def test_failure_is_swallowed(self):
        with mock.patch("hestia.store_sql.init_db", side_effect=OSError("boom")):
            self.assertFalse(store_sql.shadow_import(self.reg, self.store, {}, path=self.path))


class ShadowSyncBootTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "hestia.db"
        reg = Registry(self.dir / "r.json")
        reg.nodes = {"5": dict(RICH_NODE)}
        self.rt = SimpleNamespace(registry=reg, engine=SimpleNamespace(store=AutomationStore(self.dir / "a.json")))

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_enabled_mirrors_at_boot(self):
        env = {"HESTIA_DB": str(self.path), "HESTIA_AUTH_USERS_FILE": str(self.dir / "users.json")}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("HESTIA_DB_SHADOW", None)  # default is on
            proxy._shadow_sync_db(self.rt)
        self.assertTrue(self.path.exists())
        _, Session = db.init_db(self.path)
        with db.session_scope(Session) as s:
            self.assertIsNotNone(s.get(db.Node, "5"))

    def test_opt_out_does_nothing(self):
        with mock.patch.dict(os.environ, {"HESTIA_DB_SHADOW": "0", "HESTIA_DB": str(self.path)}):
            proxy._shadow_sync_db(self.rt)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
