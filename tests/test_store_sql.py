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


class Phase4UsersTests(unittest.TestCase):
    """Auth users → DB: cutover, load, upsert, the login backend selector, and the CLI routing."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.db = self.dir / "hestia.db"
        self.users_json = self.dir / "users.json"

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_cutover_users_marks_and_mirrors(self):
        engine, _ = db.init_db(self.db)
        store_sql.cutover_users(engine, {"tata": "h1", "mama": "h2"})
        self.assertTrue(store_sql.is_users_db_authoritative(engine))
        self.assertEqual(store_sql.load_users_db(engine), {"tata": "h1", "mama": "h2"})
        store_sql.cutover_users(engine, {"tata": "h1"})   # replace-mirror: mama dropped
        self.assertEqual(store_sql.load_users_db(engine), {"tata": "h1"})

    def test_set_user_db_upserts(self):
        with mock.patch.dict(os.environ, {"HESTIA_DB": str(self.db)}):
            store_sql.set_user_db("tata", "h1")
            store_sql.set_user_db("tata", "h2")   # update existing
            store_sql.set_user_db("mama", "h3")
            engine, _ = db.init_db(self.db)
            self.assertEqual(store_sql.load_users_db(engine), {"tata": "h2", "mama": "h3"})

    def test_users_db_authoritative_noarg(self):
        with mock.patch.dict(os.environ, {"HESTIA_DB": str(self.db)}):
            self.assertFalse(store_sql.users_db_authoritative())   # no marker yet
            engine, _ = db.init_db(self.db)
            store_sql.cutover_users(engine, {})
            engine.dispose()
            self.assertTrue(store_sql.users_db_authoritative())

    def test_current_users_reads_db_when_authoritative(self):
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.db)}):
            engine, _ = db.init_db(self.db)
            store_sql.cutover_users(engine, {"tata": "h"})
            engine.dispose()
            self.assertEqual(store_sql.current_users(), {"tata": "h"})

    def test_current_users_falls_back_to_json(self):
        self.users_json.write_text('{"mama": "hj"}', encoding="utf-8")
        self.assertEqual(store_sql.current_users(users_path=self.users_json), {"mama": "hj"})  # json mode
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.db)}):
            db.init_db(self.db)   # DB exists but users NOT authoritative → still JSON
            self.assertEqual(store_sql.current_users(users_path=self.users_json), {"mama": "hj"})

    def test_user_settings_unavailable_in_json_mode(self):
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "json", "HESTIA_DB": str(self.db)}):
            self.assertIsNone(store_sql.get_user_settings("tata"))
            self.assertFalse(store_sql.set_user_settings("tata", locale="pl", temp_scale="C", theme=None))
        self.assertFalse(self.db.exists())

    def test_user_settings_sqlite_no_row_then_upsert_and_update(self):
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.db)}):
            engine, Session = db.init_db(self.db)
            with db.session_scope(Session) as s:
                s.add(db.User(username="tata", password_hash="h"))
            engine.dispose()

            self.assertIsNone(store_sql.get_user_settings("tata"))
            self.assertTrue(store_sql.set_user_settings("tata", locale="x" * 80, temp_scale=None, theme="dark" * 30))
            self.assertEqual(store_sql.get_user_settings("tata"),
                             {"locale": "x" * 64, "temp_scale": None, "theme": ("dark" * 30)[:64]})

            self.assertTrue(store_sql.set_user_settings("tata", locale="pl", temp_scale="K", theme=None))
            self.assertEqual(store_sql.get_user_settings("tata"),
                             {"locale": "pl", "temp_scale": "K", "theme": None})

            # A partial update touches ONLY the named column; the others are preserved (the merge is
            # in one transaction, so concurrent partial writes can't clobber each other's field).
            self.assertTrue(store_sql.set_user_settings("tata", locale="de"))
            self.assertEqual(store_sql.get_user_settings("tata"),
                             {"locale": "de", "temp_scale": "K", "theme": None})

    def test_room_icons_unavailable_in_json_mode(self):
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "json", "HESTIA_DB": str(self.db)}):
            self.assertEqual(store_sql.get_room_icons(), {})
            self.assertFalse(store_sql.set_room_icon("Salon", "🛋️"))
        self.assertFalse(self.db.exists())

    def test_room_icons_sqlite_merge_remove_caps_and_bad_rows(self):
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.db)}):
            self.assertEqual(store_sql.get_room_icons(), {})
            self.assertTrue(store_sql.set_room_icon("Salon", "🛋️"))
            self.assertTrue(store_sql.set_room_icon("Kuchnia", "🍳"))
            self.assertEqual(store_sql.get_room_icons(), {"Salon": "🛋️", "Kuchnia": "🍳"})

            self.assertTrue(store_sql.set_room_icon("Salon", ""))
            self.assertEqual(store_sql.get_room_icons(), {"Kuchnia": "🍳"})
            self.assertTrue(store_sql.set_room_icon(5, 9))
            self.assertEqual(store_sql.get_room_icons(), {"Kuchnia": "🍳"})

            long_room = "x" * 80
            long_icon = "abcdef" * 4
            self.assertTrue(store_sql.set_room_icon(long_room, long_icon))
            self.assertEqual(store_sql.get_room_icons()["x" * 64], long_icon[:16])

            engine, Session = db.init_db(self.db)
            with db.session_scope(Session) as s:
                s.get(db.AppMeta, "room_icons").value = "not json"
            engine.dispose()
            self.assertEqual(store_sql.get_room_icons(), {})

            engine, Session = db.init_db(self.db)
            with db.session_scope(Session) as s:
                s.get(db.AppMeta, "room_icons").value = json.dumps([1, 2])
            engine.dispose()
            self.assertEqual(store_sql.get_room_icons(), {})

            engine, Session = db.init_db(self.db)
            with db.session_scope(Session) as s:
                s.get(db.AppMeta, "room_icons").value = json.dumps({"Salon": "🛋️", "bad": 5})
            engine.dispose()
            self.assertEqual(store_sql.get_room_icons(), {"Salon": "🛋️"})

    def test_device_state_cache_is_unavailable_in_json_mode(self):
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "json", "HESTIA_DB": str(self.db)}):
            self.assertIsNone(store_sql.load_device_state())
            self.assertFalse(store_sql.save_device_state({"switches": {"14": True}}))
        self.assertFalse(self.db.exists())

    def test_device_state_cache_sqlite_save_load_absent_and_parse_errors(self):
        snapshot = {"switches": {"14": True}, "gang": {"7": {"1": True}}}
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.db)}):
            self.assertIsNone(store_sql.load_device_state())
            self.assertTrue(store_sql.save_device_state(snapshot))
            self.assertEqual(store_sql.load_device_state(), snapshot)

            engine, Session = db.init_db(self.db)
            with db.session_scope(Session) as s:
                s.get(db.AppMeta, "device_state").value = "not json"
            engine.dispose()
            with self.assertLogs("hestia.store_sql", level="ERROR"):
                self.assertIsNone(store_sql.load_device_state())

            engine, Session = db.init_db(self.db)
            with db.session_scope(Session) as s:
                s.get(db.AppMeta, "device_state").value = json.dumps([1, 2])
            engine.dispose()
            self.assertIsNone(store_sql.load_device_state())

    def test_device_state_cache_best_effort_failures(self):
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.db)}):
            with mock.patch("hestia.store_sql.init_db", side_effect=OSError("boom")):
                with self.assertLogs("hestia.store_sql", level="ERROR"):
                    self.assertFalse(store_sql.save_device_state({"switches": {"14": True}}))
                with self.assertLogs("hestia.store_sql", level="ERROR"):
                    self.assertIsNone(store_sql.load_device_state())

    def test_open_stores_cuts_users_when_registry_already_authoritative(self):
        # the live Phase-3 box: registry authoritative, users NOT → next boot cuts over ONLY users
        self.users_json.write_text('{"tata": "h"}', encoding="utf-8")
        reg_json, auto_json = self.dir / "registry.json", self.dir / "automations.json"
        Registry(reg_json, mode="standalone").save()
        AutomationStore(auto_json).save()
        with mock.patch.dict(os.environ, {"HESTIA_DB": str(self.db)}):
            engine, _ = db.init_db(self.db)
            store_sql.cutover_import(engine, Registry.load(reg_json), AutomationStore.load(auto_json), {})
            self.assertTrue(store_sql.is_db_authoritative(engine))
            self.assertFalse(store_sql.is_users_db_authoritative(engine))
            engine.dispose()
            store_sql.open_stores(registry_path=reg_json, automations_path=auto_json,
                                  users_path=self.users_json, persist="sqlite")
            engine, _ = db.init_db(self.db)
            self.assertTrue(store_sql.is_users_db_authoritative(engine))
            self.assertEqual(store_sql.load_users_db(engine), {"tata": "h"})

    def test_open_stores_skips_users_cutover_when_users_json_missing(self):
        # a missing/bad users.json must NOT permanently promote an empty users table (lockout guard)
        reg_json, auto_json = self.dir / "registry.json", self.dir / "automations.json"
        Registry(reg_json, mode="proxy").save()
        AutomationStore(auto_json).save()
        with mock.patch.dict(os.environ, {"HESTIA_DB": str(self.db)}):  # self.users_json does not exist
            store_sql.open_stores(registry_path=reg_json, automations_path=auto_json,
                                  users_path=self.users_json, persist="sqlite")
            engine, _ = db.init_db(self.db)
            self.assertFalse(store_sql.is_users_db_authoritative(engine))  # not promoted → login falls back to JSON
            engine.dispose()

    def test_auth_cli_writes_db_when_authoritative(self):
        from hestia import auth
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.db)}):
            engine, _ = db.init_db(self.db)
            store_sql.cutover_users(engine, {})
            engine.dispose()
            self.assertEqual(auth._cli(["add", "newuser"], prompt=lambda _p: "secret"), 0)
            engine, _ = db.init_db(self.db)
            self.assertIn("newuser", store_sql.load_users_db(engine))

    def test_auth_cli_writes_json_when_sqlite_not_authoritative(self):
        from hestia import auth
        env = {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.db), "HESTIA_AUTH_USERS_FILE": str(self.users_json)}
        with mock.patch.dict(os.environ, env):
            db.init_db(self.db)   # not authoritative → CLI writes JSON
            self.assertEqual(auth._cli(["add", "ju"], prompt=lambda _p: "secret"), 0)
            self.assertIn("ju", auth.load_users(self.users_json))


class NumEnvTests(unittest.TestCase):
    """proxy._num_env clamps the SSE knobs: bad / out-of-range / non-finite values fall back."""

    def test_clamps_and_falls_back(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("X_KNOB", None)
            self.assertEqual(proxy._num_env("X_KNOB", 5.0, 1.0, 10.0), 5.0)        # unset → default
        for bad in ("abc", "0", "99", "nan", "inf"):
            with mock.patch.dict(os.environ, {"X_KNOB": bad}):
                self.assertEqual(proxy._num_env("X_KNOB", 5.0, 1.0, 10.0), 5.0)    # bad/out-of-range → default
        with mock.patch.dict(os.environ, {"X_KNOB": "7"}):
            self.assertEqual(proxy._num_env("X_KNOB", 5.0, 1.0, 10.0), 7.0)        # in range → the value


class AuditLogTests(unittest.TestCase):
    """The append-only audit log: insert, recent (order/limit), and the row/age prune caps."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.db = self.dir / "hestia.db"
        self.engine, _ = db.init_db(self.db)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_append_and_recent_newest_first(self):
        store_sql.append_audit(self.engine, actor="tata", action="ir", target="/k.ir",
                               detail='{"button": "off"}', result="ok", ts=100.0)
        store_sql.append_audit(self.engine, actor="automation:r1", action="switch", target="14",
                               result="ok", ts=200.0)
        rows = store_sql.recent_audit(self.engine)
        self.assertEqual([r["actor"] for r in rows], ["automation:r1", "tata"])  # newest first
        self.assertEqual((rows[1]["action"], rows[1]["target"], rows[1]["detail"]),
                         ("ir", "/k.ir", '{"button": "off"}'))

    def test_recent_respects_limit(self):
        for i in range(5):
            store_sql.append_audit(self.engine, actor="u", action="a", ts=float(i))
        self.assertEqual(len(store_sql.recent_audit(self.engine, limit=2)), 2)

    def test_prune_by_row_cap(self):
        for i in range(5):
            store_sql.append_audit(self.engine, actor="u", action="a", ts=float(i), max_rows=3)
        self.assertEqual(len(store_sql.recent_audit(self.engine)), 3)   # capped to the newest 3

    def test_prune_by_age(self):
        store_sql.append_audit(self.engine, actor="old", action="a", ts=100.0, max_age_s=10)
        store_sql.append_audit(self.engine, actor="new", action="a", ts=200.0, max_age_s=10)  # cutoff 190 → old gone
        self.assertEqual([r["actor"] for r in store_sql.recent_audit(self.engine)], ["new"])

    def test_open_audit_engine_has_table(self):
        with mock.patch.dict(os.environ, {"HESTIA_DB": str(self.dir / "fresh.db")}):
            engine = store_sql.open_audit_engine()
            self.assertEqual(store_sql.recent_audit(engine), [])   # table exists + empty
            engine.dispose()

    def test_open_audit_engine_returns_none_on_failure(self):
        with mock.patch("hestia.store_sql.init_db", side_effect=OSError("boom")):
            self.assertIsNone(store_sql.open_audit_engine())   # best-effort: never breaks boot

    def test_audit_without_running_loop_is_noop(self):
        # _audit may be called from the engine's sync _fire; off a running loop it must not raise
        # (best-effort). Called here from a plain sync test (no loop) with a real engine → None.
        self.assertIsNone(proxy._audit(SimpleNamespace(audit_engine=self.engine), "actor", "act"))


class DatabaseStatsTests(unittest.TestCase):
    """Operator DB-growth stats: SQLite bytes plus row counts for every app table."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.db = self.dir / "hestia.db"

    def tearDown(self):
        db.reset_engine_cache()   # dispose the cached engine before removing its files
        shutil.rmtree(self.dir)

    def test_counts_rows_and_sums_sqlite_files(self):
        _, Session = db.init_db(self.db)
        with db.session_scope(Session) as s:
            s.add(db.AppMeta(key="mode", value="proxy"))
            s.add(db.Node(key="5", entry_json="{}"))
            s.add(db.Automation(id="r1", position=0, rule_json=json.dumps(Rule.from_dict(SCENE_RULE).to_dict())))
            s.add(db.User(username="tata", password_hash="scrypt$abc"))
            s.add(db.UserSetting(username="tata", locale="pl", temp_scale="c", theme=None))
            s.add(db.Audit(ts=1.0, actor="system", action="boot", target=None, detail=None, result="ok"))

        stats = store_sql.db_stats(self.db)
        self.assertEqual(stats["tables"], {
            "app_meta": 1,
            "nodes": 1,
            "automations": 1,
            "users": 1,
            "user_settings": 1,
            "audit": 1,
        })
        # file_bytes sums the live SQLite files; the exact total depends on the WAL/SHM checkpoint state
        # at measure time (the precise main+WAL+SHM summing is pinned by test_file_bytes_skips_absent_sidecars),
        # so here assert it at least accounts for the main DB file and is positive.
        self.assertGreaterEqual(stats["file_bytes"], os.path.getsize(self.db))
        self.assertGreater(stats["file_bytes"], 0)

    def test_file_bytes_skips_absent_sidecars(self):
        # Only the main DB file exists (no -wal/-shm): the absent sidecars are skipped, so the
        # total is exactly the main file's size.
        self.db.write_bytes(b"x" * 17)
        self.assertFalse(Path(f"{self.db}-wal").exists())
        self.assertEqual(store_sql._db_file_bytes(self.db), 17)


class AutomationActorAuditTests(unittest.TestCase):
    """P5b: a rule firing its actions records actor=automation:<rule_id> in the audit log."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_fire_audits_automation_actor(self):
        from hestia.automations import AutomationEngine, AutomationStore
        eng = AutomationEngine(AutomationStore(self.dir / "a.json"))
        eng.set_rule(Rule.from_dict({"id": "r1", "trigger": {"type": "scene", "node": 2, "scene_id": 3},
                                     "actions": [{"op": "switch", "node": 14, "on": True}]}))
        rt = proxy.ProxyRuntime()
        with mock.patch("hestia.proxy._audit") as audit:
            eng.on_event(rt, 2, {}, {"id": 3, "kind": "scene"})    # fires r1
        self.assertTrue(any(c.args[1] == "automation:r1" and c.args[2] == "switch"
                            for c in audit.call_args_list), audit.call_args_list)


class DeviceStateAuditTests(unittest.TestCase):
    """P5b+ (#56): physical/external device state CHANGES are audited as actor=device; telemetry isn't."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir)

    def _engine(self):
        from hestia.automations import AutomationEngine, AutomationStore
        return AutomationEngine(AutomationStore(self.dir / "a.json"))

    def test_logs_transitions_and_scene_but_not_telemetry(self):
        rt = proxy.ProxyRuntime()
        rt.audit_engine = object()                                 # truthy → _audit_observed proceeds
        with mock.patch("hestia.proxy._audit") as audit:
            self._engine().on_event(rt, 5, {"door": "open"}, None)
            self._engine().on_event(rt, 5, {"power_w": 120, "voltage_v": 230}, None)  # telemetry → no audit
            self._engine().on_event(rt, 5, {}, {"id": 3, "kind": "scene"})            # scene → audit
        pairs = [(c.args[1], c.args[2]) for c in audit.call_args_list]
        self.assertIn(("device", "door"), pairs)
        self.assertIn(("device", "scene"), pairs)
        self.assertFalse(any(act in ("power_w", "voltage_v") for _, act in pairs))

    def test_noop_without_audit_engine(self):
        rt = proxy.ProxyRuntime()                                  # audit_engine None
        with mock.patch("hestia.proxy._audit") as audit:
            self._engine().on_event(rt, 5, {"door": "open"}, None)
        self.assertFalse(any(c.args[1] == "device" for c in audit.call_args_list))


class AuditHelperTests(unittest.IsolatedAsyncioTestCase):
    """proxy._audit: best-effort, off-loop — no-op without an engine, writes with one, swallows errors."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.db = self.dir / "hestia.db"

    def tearDown(self):
        shutil.rmtree(self.dir)

    async def test_noop_without_engine(self):
        self.assertIsNone(proxy._audit(SimpleNamespace(audit_engine=None), "tata", "ir"))

    async def test_writes_with_engine(self):
        engine, _ = db.init_db(self.db)
        await proxy._audit(SimpleNamespace(audit_engine=engine), "tata", "ir", target="/k.ir", result="ok")
        rows = store_sql.recent_audit(engine)
        self.assertEqual((rows[0]["actor"], rows[0]["action"], rows[0]["target"]), ("tata", "ir", "/k.ir"))

    async def test_failure_is_logged_not_raised_to_caller(self):
        # fire-and-forget: the caller never awaits, so a failed write is swallowed by the done-callback.
        fut = proxy._audit(SimpleNamespace(audit_engine=object()), "tata", "ir")   # bad engine → write fails
        with self.assertRaises(Exception):
            await fut                              # awaiting here only to deterministically complete it


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


class RolesTests(unittest.TestCase):
    """RBAC role storage + resolution (#73): the legacy→admin import rule, the viewer column default,
    the role read/write helpers, and current_user_role's sqlite / JSON / bad-input semantics."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.db = self.dir / "hestia.db"

    def tearDown(self):
        db.reset_engine_cache()   # current_user_role uses the cached engine WITHOUT disposing it
        shutil.rmtree(self.dir)

    def _role_of(self, username):
        engine, Session = db.init_db(self.db)
        with Session() as s:
            row = s.get(db.User, username)
        return None if row is None else row.role

    def test_cutover_users_imports_legacy_accounts_as_admin(self):
        engine, _ = db.init_db(self.db)
        store_sql.cutover_users(engine, {"tata": "h", "mama": "h2"})
        self.assertEqual(self._role_of("tata"), "admin")   # pre-roles accounts → admin (no lockout)
        self.assertEqual(self._role_of("mama"), "admin")

    def test_mirror_json_to_db_imports_users_as_admin(self):
        _, Session = db.init_db(self.db)
        reg = Registry(self.dir / "r.json")
        store = AutomationStore(self.dir / "a.json")
        with db.session_scope(Session) as s:
            store_sql.mirror_json_to_db(s, registry=reg, store=store, users={"ju": "h"})
        self.assertEqual(self._role_of("ju"), "admin")

    def test_set_user_db_defaults_to_viewer_and_takes_an_explicit_role(self):
        with mock.patch.dict(os.environ, {"HESTIA_DB": str(self.db)}):
            store_sql.set_user_db("kid", "h")                    # role omitted → least-privilege viewer
            store_sql.set_user_db("op", "h", "operator")
        self.assertEqual(self._role_of("kid"), "viewer")
        self.assertEqual(self._role_of("op"), "operator")

    def test_get_user_db_role(self):
        with mock.patch.dict(os.environ, {"HESTIA_DB": str(self.db)}):
            store_sql.set_user_db("op", "h", "operator")
            self.assertEqual(store_sql.get_user_db_role("op"), "operator")
            self.assertIsNone(store_sql.get_user_db_role("ghost"))

    def test_set_user_role_changes_existing_and_reports_missing(self):
        with mock.patch.dict(os.environ, {"HESTIA_DB": str(self.db)}):
            store_sql.set_user_db("op", "h", "viewer")
            self.assertTrue(store_sql.set_user_role("op", "admin"))
            self.assertEqual(store_sql.get_user_db_role("op"), "admin")
            self.assertFalse(store_sql.set_user_role("ghost", "admin"))   # never creates an account

    def test_current_user_role_from_db_when_authoritative(self):
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.db)}):
            engine, _ = db.init_db(self.db)
            store_sql.cutover_users(engine, {"tata": "h"})      # tata → admin, users authoritative
            store_sql.set_user_db("kid", "h", "viewer")
            self.assertEqual(store_sql.current_user_role("tata"), "admin")
            self.assertEqual(store_sql.current_user_role("kid"), "viewer")
            self.assertIsNone(store_sql.current_user_role("ghost"))   # removed account → denied at once

    def test_current_user_role_rejects_bad_input(self):
        self.assertIsNone(store_sql.current_user_role(None))
        self.assertIsNone(store_sql.current_user_role(""))
        self.assertIsNone(store_sql.current_user_role(123))

    def test_current_user_role_json_backend_is_admin_if_present(self):
        # JSON backend (default): a known account is legacy single-tier → admin; an unknown one → None.
        with mock.patch.object(store_sql.auth, "load_users", return_value={"tata": "h"}):
            self.assertEqual(store_sql.current_user_role("tata"), "admin")
            self.assertIsNone(store_sql.current_user_role("ghost"))

    def test_current_user_role_sqlite_not_authoritative_falls_back_to_json(self):
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.db)}):
            db.init_db(self.db)   # schema exists but users NOT cut over → JSON back-compat
            with mock.patch.object(store_sql.auth, "load_users", return_value={"tata": "h"}):
                self.assertEqual(store_sql.current_user_role("tata"), "admin")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
