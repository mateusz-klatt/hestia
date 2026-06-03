"""Unit tests for the JSON -> SQLite shadow import (hestia.store_sql) — Phase 2 of #57.

Proves the mirror is lossless + idempotent + replace-mirror (deletes absent rows), that
shadow_import is best-effort (never raises), and that the boot helper honours the opt-out.
"""
from __future__ import annotations

import asyncio
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

    def test_setup_failure_never_breaks_boot(self):
        # A failure BEFORE shadow_import's own guard (here load_users raising) must be swallowed
        # by _shadow_sync_db itself — boot must never crash because of the shadow DB.
        with mock.patch("hestia.auth.load_users", side_effect=OSError("boom")):
            with mock.patch.dict(os.environ, {"HESTIA_DB": str(self.path)}):
                os.environ.pop("HESTIA_DB_SHADOW", None)
                proxy._shadow_sync_db(self.rt)  # must NOT raise

    def test_skips_shadow_when_sqlite_authoritative(self):
        # In sqlite mode the DB is already the live store — no shadow needed (and not touched here).
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.path)}):
            os.environ.pop("HESTIA_DB_SHADOW", None)
            proxy._shadow_sync_db(self.rt)
        self.assertFalse(self.path.exists())


class ResolveModeSqliteTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.db = self.dir / "hestia.db"
        self.reg = self.dir / "registry.json"

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_reads_mode_from_db_when_authoritative(self):
        from hestia.__main__ import resolve_mode
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.db)}):
            engine, _ = db.init_db(self.db)
            store_sql.cutover_import(engine, Registry(self.reg, mode="standalone"),
                                     AutomationStore(self.dir / "a.json"), {})
            engine.dispose()
            self.assertEqual(resolve_mode(None, self.reg), "standalone")  # mode comes from the DB

    def test_falls_back_to_json_when_not_authoritative(self):
        from hestia.__main__ import resolve_mode
        Registry(self.reg, mode="standalone").save()   # JSON still owns the mode pre-cutover
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.db)}):
            self.assertEqual(resolve_mode(None, self.reg), "standalone")


class Phase3CutoverTests(unittest.TestCase):
    """DB-as-authoritative backend: writers, loaders, open_stores selection, export, mode."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.db = self.dir / "hestia.db"
        self.reg_path = self.dir / "registry.json"
        self.auto_path = self.dir / "automations.json"
        self.users_path = self.dir / "users.json"

    def tearDown(self):
        shutil.rmtree(self.dir)

    def _seed_json(self):
        reg = Registry(self.reg_path, mode="standalone")
        reg.nodes = {"5": dict(RICH_NODE)}
        reg.save()
        store = AutomationStore(self.auto_path)
        store.rules = {"r1": Rule.from_dict(SCENE_RULE)}
        store.save()
        self.users_path.write_text('{"tata": "scrypt$abc"}', encoding="utf-8")

    def test_registry_db_writer_roundtrips_and_replace_mirrors(self):
        engine, _ = db.init_db(self.db)
        reg = Registry(self.reg_path, mode="standalone", writer=store_sql.registry_db_writer(engine))
        reg.set_user(5, name="Lampa", room="Salon")
        reg.observe(5, "blind", "inferred", power="mains")
        reg.set_user(9, name="Drzwi")
        reg.save()                                    # write_payload -> DB
        loaded = store_sql.load_registry(engine, self.reg_path, writer=None)
        self.assertEqual(loaded.mode, "standalone")
        self.assertEqual(loaded.nodes["5"]["name"], "Lampa")
        self.assertEqual(loaded.nodes["9"]["name"], "Drzwi")
        # replace-mirror: drop node 9, persist again -> gone from the DB
        del reg.nodes["9"]
        reg.save()
        self.assertNotIn("9", store_sql.load_registry(engine, self.reg_path, writer=None).nodes)

    def test_automations_db_writer_roundtrips(self):
        engine, _ = db.init_db(self.db)
        store = AutomationStore(self.auto_path, writer=store_sql.automations_db_writer(engine))
        store.set_rule(Rule.from_dict(SCENE_RULE))
        store.set_rule(Rule.from_dict(STATE_RULE))
        store.save()
        loaded = store_sql.load_automations(engine, self.auto_path, writer=None)
        self.assertEqual(list(loaded.rules), ["r1", "cold"])
        store.delete_rule("cold")
        store.save()
        self.assertEqual(list(store_sql.load_automations(engine, self.auto_path, writer=None).rules), ["r1"])

    def test_db_writer_wraps_every_bad_payload_as_oserror(self):
        # The writer is the hard persistence boundary — a malformed payload must surface as OSError
        # (not KeyError/TypeError/AttributeError) so it can never kill the OSError-only autosave loop.
        engine, _ = db.init_db(self.db)
        rw, aw = store_sql.registry_db_writer(engine), store_sql.automations_db_writer(engine)
        for bad in (b"not json", b"[]", b'{"nodes": []}'):       # bad JSON / wrong top type / nodes not a dict
            with self.assertRaises(OSError):
                rw(bad)
        for bad in (b'{"rules": [{}]}', b'{"rules": [1]}'):       # rule missing "id" / rule not a dict
            with self.assertRaises(OSError):
                aw(bad)

    def test_load_automations_skips_invalid_row(self):
        engine, Session = db.init_db(self.db)
        with db.session_scope(Session) as s:
            s.add(db.Automation(id="bad", position=0, rule_json='{"id": "bad"}'))  # missing trigger/actions
            s.add(db.Automation(id="r1", position=1, rule_json=json.dumps(Rule.from_dict(SCENE_RULE).to_dict())))
        loaded = store_sql.load_automations(engine, self.auto_path, writer=None)
        self.assertEqual(list(loaded.rules), ["r1"])   # the invalid row is skipped + logged

    def test_open_stores_json_is_default(self):
        self._seed_json()
        reg, store = store_sql.open_stores(registry_path=self.reg_path, automations_path=self.auto_path,
                                           users_path=self.users_path, persist="json")
        self.assertIsNone(reg._writer)                 # JSON-backed, no DB writer
        self.assertEqual(reg.nodes["5"]["type"], "blind")
        self.assertEqual(list(store.rules), ["r1"])

    def test_open_stores_sqlite_cutover_then_loads_from_db(self):
        self._seed_json()
        with mock.patch.dict(os.environ, {"HESTIA_DB": str(self.db)}):
            reg, store = store_sql.open_stores(registry_path=self.reg_path, automations_path=self.auto_path,
                                               users_path=self.users_path, persist="sqlite")
            self.assertIsNotNone(reg._writer)          # DB-backed
            self.assertEqual(reg.mode, "standalone")
            self.assertEqual(reg.nodes["5"]["name"], RICH_NODE["name"])
            self.assertEqual(list(store.rules), ["r1"])
            engine, _ = db.init_db(self.db)
            self.assertTrue(store_sql.is_db_authoritative(engine))
            self.assertEqual(store_sql.read_mode(engine), "standalone")
            # a save now lands in the DB; mutate JSON files afterwards → ignored (DB is authoritative)
            reg.set_user(7, name="NewFromDB")
            reg.save()
            self.reg_path.write_text('{"schema": 2, "mode": "proxy", "nodes": {}}', encoding="utf-8")
            reg2, _ = store_sql.open_stores(registry_path=self.reg_path, automations_path=self.auto_path,
                                            users_path=self.users_path, persist="sqlite")
            self.assertEqual(reg2.nodes["7"]["name"], "NewFromDB")  # from DB, not the rewritten JSON
            self.assertEqual(reg2.mode, "standalone")

    def test_export_rebuilds_registry_automations_but_leaves_users(self):
        self._seed_json()
        with mock.patch.dict(os.environ, {"HESTIA_DB": str(self.db)}):
            store_sql.open_stores(registry_path=self.reg_path, automations_path=self.auto_path,
                                  users_path=self.users_path, persist="sqlite")
            # users stay JSON-authoritative in Phase 3: a post-cutover account change lands in users.json
            self.users_path.write_text('{"mama": "scrypt$new"}', encoding="utf-8")
            self.reg_path.unlink()
            self.auto_path.unlink()
            self.assertTrue(store_sql.export_to_json(registry_path=self.reg_path,
                                                     automations_path=self.auto_path, path=self.db))
        self.assertEqual(Registry.load(self.reg_path).nodes["5"]["type"], "blind")
        self.assertEqual(list(AutomationStore.load(self.auto_path).rules), ["r1"])
        # export must NOT clobber the current users.json with the stale DB cutover snapshot
        self.assertEqual(json.loads(self.users_path.read_text()), {"mama": "scrypt$new"})


class DbPersistIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """The cancel-safe write path (_persist_obj / _write_and_settle) is backend-agnostic — prove it
    drives a DB-backed Registry exactly like the JSON one (success, and the OSError re-arm path)."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.db = self.dir / "hestia.db"

    def tearDown(self):
        shutil.rmtree(self.dir)

    async def test_persist_obj_lands_in_the_db(self):
        engine, _ = db.init_db(self.db)
        reg = Registry(self.dir / "r.json", mode="standalone", writer=store_sql.registry_db_writer(engine))
        reg.set_user(5, name="Lampa", room="Salon")   # sets dirty
        await proxy._persist_obj(asyncio.Lock(), reg)  # serialize() in-loop, write_payload off-loop
        self.assertFalse(reg.dirty)                    # cleared after the durable write
        self.assertEqual(store_sql.load_registry(engine, self.dir / "r.json", writer=None).nodes["5"]["name"], "Lampa")

    async def test_persist_obj_rearms_dirty_on_db_failure(self):
        reg = Registry(self.dir / "r.json", writer=mock.Mock(side_effect=OSError("disk full")))
        reg.set_user(5, name="x")
        with self.assertRaises(OSError):
            await proxy._persist_obj(asyncio.Lock(), reg)
        self.assertTrue(reg.dirty)                     # failed write re-armed dirty for the next retry


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
