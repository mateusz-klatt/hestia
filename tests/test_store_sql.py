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
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import select
from sqlalchemy.orm import Session

from hestia import auth, authz, db, proxy, store_sql
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
        with Session(engine) as s, s.begin():
            s.add(db.User(username="stale", password_hash="old"))
        store_sql.cutover_users(engine, {"tata": "h1", "mama": "h2"})
        self.assertTrue(store_sql.is_users_db_authoritative(engine))
        self.assertEqual(store_sql.load_users_db(engine), {"tata": "h1", "mama": "h2"})
        store_sql.cutover_users(engine, {"tata": "stale"})   # already authoritative: must not overwrite/drop
        self.assertEqual(store_sql.load_users_db(engine), {"tata": "h1", "mama": "h2"})

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
                             {"locale": "x" * 64, "temp_scale": None, "theme": ("dark" * 30)[:64],
                              "preferred_languages": ["en"]})

            self.assertTrue(store_sql.set_user_settings("tata", locale="pl", temp_scale="K", theme=None,
                                                        preferred_languages=["fr", "en"]))
            self.assertEqual(store_sql.get_user_settings("tata"),
                             {"locale": "pl", "temp_scale": "K", "theme": None,
                              "preferred_languages": ["fr", "en"]})

            # A partial update touches ONLY the named column; the others are preserved (the merge is
            # in one transaction, so concurrent partial writes can't clobber each other's field).
            self.assertTrue(store_sql.set_user_settings("tata", locale="de"))
            self.assertEqual(store_sql.get_user_settings("tata"),
                             {"locale": "de", "temp_scale": "K", "theme": None,
                              "preferred_languages": ["fr", "en"]})

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
        failure = OSError("disk full")
        with mock.patch.object(store_sql, "append_audit", side_effect=failure), \
                mock.patch.object(proxy.log, "debug") as log:
            fut = proxy._audit(SimpleNamespace(audit_engine=object()), "tata", "ir")
            with self.assertRaisesRegex(OSError, "^disk full$"):
                await fut                          # awaiting here only to deterministically complete it
            await asyncio.sleep(0)                 # allow the scheduled done-callback to log the failure
            log.assert_called_once_with("audit write failed", exc_info=failure)


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
        lock = asyncio.Lock()
        with self.assertRaises(OSError):
            await proxy._persist_obj(lock, reg)
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
            self.assertEqual(store_sql.set_user_role("op", "admin"), "ok")   # promote (no guard)
            self.assertEqual(store_sql.get_user_db_role("op"), "admin")
            self.assertEqual(store_sql.set_user_role("ghost", "admin"), "not_found")  # never creates an account
            self.assertEqual(store_sql.set_user_role("op", "viewer"), "last_admin")   # op is now the only admin
            self.assertEqual(store_sql.get_user_db_role("op"), "admin")               # refused → unchanged

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

    def test_set_user_disabled_toggles_and_guards_the_last_admin(self):
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.db)}):
            engine, _ = db.init_db(self.db)
            store_sql.cutover_users(engine, {"tata": "h", "mama": "h2"})   # both admin (legacy import)
            store_sql.set_user_db("kid", "h", "viewer")
            self.assertEqual(store_sql.set_user_disabled("tata", True), "ok")        # 2 admins → one off is fine
            self.assertEqual(store_sql.set_user_disabled("mama", True), "last_admin")  # mama is the only enabled admin
            self.assertEqual(store_sql.set_user_disabled("ghost", True), "not_found")
            self.assertEqual(store_sql.set_user_disabled("tata", False), "ok")       # re-enable is always allowed
            self.assertEqual(store_sql.set_user_disabled("kid", True), "ok")         # a non-admin never trips the guard

    def test_disabled_user_is_denied_role_and_filtered_from_login(self):
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.db)}):
            engine, _ = db.init_db(self.db)
            store_sql.cutover_users(engine, {"tata": "h", "mama": "h2"})   # 2 admins so one can be disabled
            store_sql.set_user_disabled("mama", True)
            self.assertEqual(store_sql.current_user_role("tata"), "admin")
            self.assertIsNone(store_sql.current_user_role("mama"))         # disabled → role None (live cookie dies)
            self.assertEqual(set(store_sql.current_users()), {"tata"})     # disabled filtered out of the login map

    def test_list_users_returns_metadata_without_hashes(self):
        with mock.patch.dict(os.environ, {"HESTIA_DB": str(self.db)}):
            store_sql.set_user_db("mama", "secret-hash", "admin")
            store_sql.set_user_db("kid", "h", "viewer")
            store_sql.set_user_disabled("kid", True)                        # a disabled non-admin (no guard trip)
            rows = store_sql.list_users()
        self.assertEqual([r["username"] for r in rows], ["kid", "mama"])    # username-sorted
        self.assertEqual(rows[0], {"username": "kid", "role": "viewer", "disabled": True})
        self.assertEqual(rows[1], {"username": "mama", "role": "admin", "disabled": False})
        for r in rows:                                                      # NEVER leak the password hash
            self.assertNotIn("password_hash", r)
            self.assertNotIn("secret-hash", r.values())

    def test_add_user_inserts_once_then_reports_exists(self):
        with mock.patch.dict(os.environ, {"HESTIA_DB": str(self.db)}):
            self.assertEqual(store_sql.add_user("newbie", "h", "operator"), "ok")
            self.assertEqual(self._role_of("newbie"), "operator")
            self.assertEqual(store_sql.add_user("newbie", "h2", "admin"), "exists")  # never overwrites
            self.assertEqual(self._role_of("newbie"), "operator")                    # role + hash untouched

    def test_set_user_password_updates_existing_only(self):
        with mock.patch.dict(os.environ, {"HESTIA_DB": str(self.db)}):
            store_sql.set_user_db("mama", "old-hash", "admin")
            self.assertTrue(store_sql.set_user_password("mama", "new-hash"))
            self.assertFalse(store_sql.set_user_password("ghost", "x"))    # never creates an account
            engine, Session = db.init_db(self.db)
            with Session() as s:
                row = s.get(db.User, "mama")
            self.assertEqual(row.password_hash, "new-hash")
            self.assertEqual(row.role, "admin")                            # role preserved

    def test_password_writes_roll_back_device_revocation_and_preserve_cached_pool(self):
        engine, sessions = db.init_db(self.db)
        store_sql.cutover_users(engine, {"owner": "original"})
        with sessions.begin() as session:
            session.add(db.BudgetReceiptDeviceToken(
                id="device", owner="owner", device_name="Vesta", token_hmac="1" * 64,
                created_at=1, sliding_written_at=1, expires_at=2, hard_expires_at=3,
            ))
            session.add(db.BudgetReceiptPairCode(
                id="pair", owner="owner", code_hmac="2" * 64, created_at=1, expires_at=2,
            ))

        revoke = store_sql._revoke_receipt_devices

        def fail_after_revocation(session, owner):
            revoke(session, owner)
            session.flush()
            self.assertEqual(session.get(db.User, owner).password_hash, "replacement")
            self.assertIsNotNone(session.get(db.BudgetReceiptDeviceToken, "device").revoked_at)
            self.assertIsNotNone(session.get(db.BudgetReceiptPairCode, "pair").consumed_at)
            raise RuntimeError("abort account change")

        for write in (store_sql.set_user_password, store_sql.set_user_db):
            with self.subTest(write=write.__name__):
                with mock.patch.object(engine, "dispose") as dispose, mock.patch.object(
                    store_sql, "_revoke_receipt_devices", side_effect=fail_after_revocation,
                ):
                    with self.assertRaisesRegex(RuntimeError, "abort account change"):
                        write("owner", "replacement", path=self.db)
                dispose.assert_not_called()
                with sessions() as session:
                    self.assertEqual(session.get(db.User, "owner").password_hash, "original")
                    self.assertIsNone(session.get(db.BudgetReceiptDeviceToken, "device").revoked_at)
                    self.assertIsNone(session.get(db.BudgetReceiptPairCode, "pair").consumed_at)

    def test_cli_reset_and_disable_invalidate_only_affected_receipt_credentials(self):
        engine, sessions = db.init_db(self.db)
        owners = ("cli_reset", "disabled", "unchanged")
        store_sql.cutover_users(engine, dict.fromkeys(owners, "old-hash"))
        now = time.time()
        with sessions.begin() as session:
            for index, owner in enumerate(owners):
                session.add(db.BudgetReceiptDeviceToken(
                    id=owner, owner=owner, device_name="Vesta", token_hmac=f"{index:064x}",
                    created_at=now, sliding_written_at=now, expires_at=now + 3600,
                    hard_expires_at=now + 7200,
                ))
                session.add(db.BudgetReceiptPairCode(
                    id=owner, owner=owner, code_hmac=f"{index:064x}",
                    created_at=now, expires_at=now + 300,
                ))

        store_sql.set_user_db("cli_reset", "new-hash", "admin", path=self.db)
        self.assertEqual(store_sql.set_user_disabled("disabled", True, path=self.db), "ok")
        self.assertEqual(store_sql.set_user_disabled("disabled", False, path=self.db), "ok")

        with sessions() as session:
            revoked = dict(session.execute(select(
                db.BudgetReceiptDeviceToken.owner, db.BudgetReceiptDeviceToken.revoked_at,
            )).all())
            consumed = dict(session.execute(select(
                db.BudgetReceiptPairCode.owner, db.BudgetReceiptPairCode.consumed_at,
            )).all())
        self.assertIsNotNone(revoked["cli_reset"])
        self.assertIsNotNone(revoked["disabled"])
        self.assertIsNone(revoked["unchanged"])
        self.assertEqual(consumed, revoked)


class AuthorizationBridgeTests(unittest.TestCase):
    """Tests for PR 1 bridge: session/policy phases, auth state, login snapshot/finish,
    credential generation & scoped revocation, sync_legacy_grants, and owner provisioning."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.addCleanup(db.reset_engine_cache)
        self.db = self.dir / "hestia.db"
        self.users_json = self.dir / "users.json"

    def _set_meta(self, key: str, value: str) -> None:
        engine, _ = db.init_db(self.db)
        with Session(engine) as session, session.begin():
            row = session.get(db.AppMeta, key)
            if row is None:
                session.add(db.AppMeta(key=key, value=value))
            else:
                row.value = value

    def _grants_of(self, username: str) -> dict[tuple[str, str | None], int]:
        engine, _ = db.init_db(self.db)
        with Session(engine) as session:
            rows = session.execute(
                select(db.PermissionGrant).where(db.PermissionGrant.username == username)
            ).scalars().all()
            return {(r.permission_id, r.target_owner): r.id for r in rows}

    def test_auth_session_phase_and_unsupported_checks(self):
        engine, _ = db.init_db(self.db)
        with Session(engine) as session:
            self.assertEqual(store_sql.auth_session_phase(session), "legacy")
        self._set_meta("auth_session_phase", "v2")
        with Session(engine) as session:
            self.assertEqual(store_sql.auth_session_phase(session), "v2")
        self._set_meta("auth_session_phase", "v99")
        with Session(engine) as session:
            with self.assertRaises(store_sql.SessionPhaseUnsupported):
                store_sql.auth_session_phase(session)
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.db)}):
            with self.assertRaises(store_sql.SessionPhaseUnsupported):
                store_sql.check_supported_policy_phase()
        self._set_meta("auth_session_phase", "v2")
        self._set_meta("authz_policy_phase", "catalog_compat")
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.db)}):
            self.assertEqual(store_sql.check_supported_policy_phase(), "catalog_compat")
            self.assertFalse(store_sql.users_db_authoritative())
            self.assertEqual(store_sql.current_users(users_path=self.users_json), {})

    def test_load_auth_state_backends_and_unsupported_phase(self):
        self.users_json.write_text(json.dumps({"tata": "hj"}), encoding="utf-8")
        # Pure JSON mode must never open SQLite.
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "json", "HESTIA_AUTH_USERS_FILE": str(self.users_json)}), \
             mock.patch("hestia.store_sql.init_db", side_effect=AssertionError("opened DB in JSON mode")):
            state = store_sql.load_auth_state("tata")
            self.assertEqual(
                state,
                store_sql.AuthState(
                    backend="json",
                    session_phase="legacy",
                    policy_phase="legacy",
                    enabled=True,
                    role="admin",
                    generation=0,
                ),
            )
            missing = store_sql.load_auth_state("ghost")
            self.assertEqual(missing.enabled, False)
            self.assertIsNone(missing.role)

        # SQLite before users_authoritative falls back to JSON unless phase is unsupported.
        db.init_db(self.db)
        with mock.patch.dict(
            os.environ,
            {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.db), "HESTIA_AUTH_USERS_FILE": str(self.users_json)},
        ):
            self._set_meta("auth_session_phase", "v2")
            fallback = store_sql.load_auth_state("tata")
            self.assertEqual(fallback.backend, "sqlite_json")
            self.assertEqual(fallback.session_phase, "legacy")
            self.assertTrue(fallback.enabled)
            self.assertEqual(fallback.role, "admin")

            self._set_meta("authz_policy_phase", "catalog_custom")
            with self.assertRaises(authz.PolicyPhaseUnsupported):
                store_sql.load_auth_state("tata")
            self._set_meta("authz_policy_phase", "catalog_compat")

            # Cut over to authoritative SQLite users.
            engine, _ = db.init_db(self.db)
            store_sql.cutover_users(engine, {"tata": "h1", "bob": "h2"})
            store_sql.set_user_db("bob", "h2", "operator")
            store_sql.set_user_disabled("bob", True)
            disabled_state = store_sql.load_auth_state("bob")
            self.assertFalse(disabled_state.enabled)
            self.assertIsNone(disabled_state.role)
            self.assertEqual(disabled_state.generation, 0)
            enabled_state = store_sql.load_auth_state("tata")
            self.assertTrue(enabled_state.enabled)
            self.assertEqual(enabled_state.backend, "sqlite")
            self.assertEqual(enabled_state.session_phase, "v2")
            self.assertEqual(enabled_state.policy_phase, "catalog_compat")
            self.assertEqual(enabled_state.role, "admin")
            self.assertEqual(enabled_state.generation, 0)

    def test_load_login_snapshot_and_finish_sqlite_login_paths(self):
        self.users_json.write_text(json.dumps({"tata": "json-hash"}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "json", "HESTIA_AUTH_USERS_FILE": str(self.users_json)}), \
             mock.patch("hestia.store_sql.init_db", side_effect=AssertionError("opened DB in JSON mode")):
            snap = store_sql.load_login_snapshot("tata")
            self.assertEqual(snap.backend, "json")
            self.assertEqual(snap.password_hash, "json-hash")
            self.assertIsNone(store_sql.load_login_snapshot(None).password_hash)
            self.assertIsNone(store_sql.finish_sqlite_login(snap, native=False, now=10.0, secret=b"s"))

        db.init_db(self.db)
        env = {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.db), "HESTIA_AUTH_USERS_FILE": str(self.users_json)}
        with mock.patch.dict(os.environ, env):
            fallback_snap = store_sql.load_login_snapshot("tata")
            self.assertEqual(fallback_snap.backend, "sqlite_json")
            self.assertEqual(fallback_snap.password_hash, "json-hash")

            engine, _ = db.init_db(self.db)
            store_sql.cutover_users(engine, {"tata": "db-hash", "admin2": "db-hash2"})

            # Phase off: issues v1 tokens regardless of native flag.
            snap = store_sql.load_login_snapshot("tata")
            self.assertIsNone(store_sql.load_login_snapshot("ghost").password_hash)
            issued_v1 = store_sql.finish_sqlite_login(snap, native=True, now=100.0, secret=b"sec")
            self.assertEqual(issued_v1.session_phase, "legacy")
            self.assertEqual(auth.decode_session(issued_v1.token, now=100.0, secret=b"sec").version, 1)

            # Activate sessions v2: issues v2 browser or native token.
            self.assertEqual(store_sql.activate_sessions_v2(), "activated")
            self.assertEqual(store_sql.activate_sessions_v2(), "already_active")
            snap_v2 = store_sql.load_login_snapshot("tata")
            issued_browser = store_sql.finish_sqlite_login(snap_v2, native=False, now=100.0, secret=b"sec")
            issued_native = store_sql.finish_sqlite_login(snap_v2, native=True, now=100.0, secret=b"sec")
            self.assertEqual(issued_browser.session_phase, "v2")
            self.assertEqual(auth.decode_session(issued_browser.token, now=100.0, secret=b"sec").purpose, "browser")
            self.assertEqual(auth.decode_session(issued_native.token, now=100.0, secret=b"sec").purpose, "native")

            # Concurrent hash change during scrypt invalidates finish_sqlite_login.
            snap_stale_hash = store_sql.load_login_snapshot("tata")
            self.assertTrue(store_sql.set_user_password("tata", "db-hash-new"))
            self.assertIsNone(store_sql.finish_sqlite_login(snap_stale_hash, native=False, now=100.0, secret=b"sec"))

            # Same hash rewritten (generation bumped) also invalidates snapshot.
            snap_stale_gen = store_sql.load_login_snapshot("tata")
            self.assertTrue(store_sql.set_user_password("tata", "db-hash-new"))
            self.assertIsNone(store_sql.finish_sqlite_login(snap_stale_gen, native=False, now=100.0, secret=b"sec"))

            # Disable during scrypt invalidates finish_sqlite_login.
            snap_disabled = store_sql.load_login_snapshot("tata")
            self.assertEqual(store_sql.set_user_disabled("tata", True), "ok")
            self.assertIsNone(store_sql.finish_sqlite_login(snap_disabled, native=False, now=100.0, secret=b"sec"))

            # Re-enable retains bumped generation and lets fresh login succeed.
            self.assertEqual(store_sql.set_user_disabled("tata", False), "ok")
            snap_re = store_sql.load_login_snapshot("tata")
            self.assertEqual(snap_re.generation, 3)

            # Removal of users_authoritative during scrypt invalidates login.
            with Session(engine) as session, session.begin():
                session.delete(session.get(db.AppMeta, "users_authoritative"))
            self.assertIsNone(store_sql.finish_sqlite_login(snap_re, native=False, now=100.0, secret=b"sec"))

        # Backend switched away from sqlite during scrypt invalidates login.
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "json"}):
            self.assertIsNone(store_sql.finish_sqlite_login(snap_v2, native=False, now=100.0, secret=b"sec"))

    def test_generation_bump_cas_and_scoped_revocation(self):
        engine, sessions = db.init_db(self.db)
        store_sql.cutover_users(engine, {"alice": "h1", "bob": "h2", "carol": "h3"})
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.db)}):
            store_sql.set_user_db("bob", "h2", "viewer")
            store_sql.set_user_db("carol", "h3", "admin")

            # Phase off: password reset and disable leave credential_generation at 0.
            self.assertTrue(store_sql.set_user_password("bob", "h2b"))
            self.assertEqual(store_sql.set_user_disabled("bob", True), "ok")
            self.assertEqual(store_sql.set_user_disabled("bob", False), "ok")
            with sessions() as s:
                self.assertEqual(s.get(db.User, "bob").credential_generation, 0)

            # Activate v2 and populate device tokens, pair codes, and native login codes.
            self.assertEqual(store_sql.activate_sessions_v2(), "activated")
            now = time.time()
            with sessions.begin() as s:
                s.add(db.BudgetReceiptDeviceToken(
                    id="legacy-self", owner="alice", actor=None, device_name="Old", token_hmac="a" * 64,
                    created_at=now, sliding_written_at=now, expires_at=now + 60, hard_expires_at=now + 120,
                ))
                s.add(db.BudgetReceiptDeviceToken(
                    id="delegate-bob", owner="alice", actor="bob", device_name="BobVesta", token_hmac="b" * 64,
                    created_at=now, sliding_written_at=now, expires_at=now + 60, hard_expires_at=now + 120,
                ))
                s.add(db.BudgetReceiptDeviceToken(
                    id="other-alice-delegate", owner="carol", actor="alice", device_name="Ali", token_hmac="c" * 64,
                    created_at=now, sliding_written_at=now, expires_at=now + 60, hard_expires_at=now + 120,
                ))
                s.add(db.BudgetReceiptPairCode(
                    id="pair-alice", owner="alice", code_hmac="d" * 64, created_at=now, expires_at=now + 60,
                ))
                s.add(db.NativeLoginCode(
                    id="qr-alice", username="alice", code_hmac="e" * 64, generation=0,
                    created_at=now, expires_at=now + 60,
                ))
                s.add(db.NativeLoginCode(
                    id="qr-bob", username="bob", code_hmac="f" * 64, generation=0,
                    created_at=now, expires_at=now + 60,
                ))

            # CAS checks on set_user_password
            self.assertFalse(store_sql.set_user_password("alice", "hx", expected_hash="wrong"))
            self.assertFalse(store_sql.set_user_password("alice", "hx", expected_generation=99))
            self.assertTrue(
                store_sql.set_user_password("alice", "h1b", expected_hash="h1", expected_generation=0)
            )

            with sessions() as s:
                self.assertEqual(s.get(db.User, "alice").credential_generation, 1)
                self.assertIsNotNone(s.get(db.BudgetReceiptDeviceToken, "legacy-self").revoked_at)
                self.assertIsNotNone(s.get(db.BudgetReceiptDeviceToken, "other-alice-delegate").revoked_at)
                # Delegate token on alice's budget owned by bob is preserved on alice password reset!
                self.assertIsNone(s.get(db.BudgetReceiptDeviceToken, "delegate-bob").revoked_at)
                self.assertIsNotNone(s.get(db.BudgetReceiptPairCode, "pair-alice").consumed_at)
                self.assertIsNotNone(s.get(db.NativeLoginCode, "qr-alice").consumed_at)
                self.assertIsNone(s.get(db.NativeLoginCode, "qr-bob").consumed_at)

            # Disabling alice bumps generation and revokes all owner=alice tokens (including delegate-bob!).
            self.assertEqual(store_sql.set_user_disabled("alice", True), "ok")
            with sessions() as s:
                self.assertEqual(s.get(db.User, "alice").credential_generation, 2)
                self.assertIsNotNone(s.get(db.BudgetReceiptDeviceToken, "delegate-bob").revoked_at)
            # Re-disabling an already-disabled user does not bump again; re-enable preserves generation=2.
            self.assertEqual(store_sql.set_user_disabled("alice", True), "ok")
            self.assertEqual(store_sql.set_user_disabled("alice", False), "ok")
            with sessions() as s:
                self.assertEqual(s.get(db.User, "alice").credential_generation, 2)

    def test_sync_legacy_grants_and_immediate_add_user(self):
        engine, _ = db.init_db(self.db)
        store_sql.cutover_users(engine, {"owner": "h1", "op": "h2", "view": "h3"})
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.db)}):
            store_sql.set_user_db("op", "h2", "operator")
            store_sql.set_user_db("view", "h3", "viewer")

            # Legacy phase: provision sets budget_owner and seeds all 6 direct self grants (no role grants yet).
            self.assertEqual(store_sql.provision_budget_owner("owner"), "ok")
            self.assertEqual(
                set(self._grants_of("owner")),
                {(pid, "owner") for pid in store_sql._LEGACY_SELF_BUDGET_GRANTS},
            )

            # Populate unrelated custom grant + household row before switching to catalog_compat.
            with Session(engine) as s, s.begin():
                s.add(db.PermissionGrant(username="op", permission_id="budget.receipts.add", target_owner="owner"))
                s.add(db.PermissionGrant(username="view", permission_id="household.view", target_owner="owner"))
                s.add(db.BudgetHouseholdMember(member_username="op", owner="owner", grant_id="1" * 64))

            self._set_meta("authz_policy_phase", "catalog_compat")
            # Immediate add_user reconciles grants in catalog_compat and rejects duplicate without overwrite.
            self.assertEqual(store_sql.add_user("newv", "h4", "viewer"), "ok")
            self.assertEqual(store_sql.add_user("newv", "h-other", "admin"), "exists")
            self.assertEqual(set(self._grants_of("newv")), {
                ("ioc.view", None), ("calendar.view", None), ("system.audit.view", None),
            })

            # Owner receives 7 installation + 6 self-budget grants; operator receives 5 + unrelated + household.
            owner_grants = self._grants_of("owner")
            self.assertEqual(
                set(owner_grants),
                {
                    ("ioc.view", None), ("ioc.control", None), ("ioc.configure", None),
                    ("calendar.view", None), ("calendar.edit", None),
                    ("system.audit.view", None), ("system.manage_users", None),
                    ("budget.view", "owner"), ("budget.edit", "owner"),
                    ("budget.receipts.add", "owner"), ("budget.receipts.view", "owner"),
                    ("budget.receipts.edit", "owner"), ("budget.receipts.match", "owner"),
                },
            )
            op_grants = self._grants_of("op")
            self.assertEqual(
                set(op_grants),
                {
                    ("ioc.view", None), ("ioc.control", None),
                    ("calendar.view", None), ("calendar.edit", None),
                    ("system.audit.view", None),
                    ("budget.receipts.add", "owner"),
                    ("household.view", "owner"),
                },
            )
            # Stale managed household row for "view" (no BudgetHouseholdMember) was removed!
            self.assertNotIn(("household.view", "owner"), self._grants_of("view"))
            # Unrelated delegated grant ID and kept installation IDs stay stable across reconcile/add.
            kept_receipt_id = op_grants[("budget.receipts.add", "owner")]
            kept_ioc_id = op_grants[("ioc.control", None)]

            # Disabling keeps stored-role grant projections.
            self.assertEqual(store_sql.set_user_disabled("op", True), "ok")
            self.assertEqual(store_sql.set_user_disabled("op", False), "ok")
            self.assertEqual(self._grants_of("op")[("household.view", "owner")], op_grants[("household.view", "owner")])

            # Role reversal on member ("op" -> "admin") suppresses household.view while preserving metadata & grant_id.
            self.assertEqual(store_sql.set_user_role("op", "admin"), "ok")
            self.assertNotIn(("household.view", "owner"), self._grants_of("op"))
            self.assertNotIn(("budget.view", "op"), self._grants_of("op"))  # role promotion alone does NOT provision!
            with Session(engine) as s:
                self.assertEqual(s.get(db.BudgetHouseholdMember, "op").grant_id, "1" * 64)
            # Restoring member role restores household.view, still preserving unrelated & kept IDs.
            self.assertEqual(store_sql.set_user_role("op", "operator"), "ok")
            self.assertEqual(store_sql.set_user_role("op", "operator"), "ok")  # no-op same role
            self.assertIn(("household.view", "owner"), self._grants_of("op"))
            self.assertEqual(self._grants_of("op")[("budget.receipts.add", "owner")], kept_receipt_id)
            self.assertEqual(self._grants_of("op")[("ioc.control", None)], kept_ioc_id)

            # Role reversal on owner ("owner" -> "operator") suppresses owner self-grants and household.view,
            # leaving owner.budget_owner=True marker suppressed until restored.
            self.assertEqual(store_sql.add_user("adm2", "h5", "admin"), "ok")
            self.assertEqual(store_sql.set_user_role("owner", "operator"), "ok")
            self.assertNotIn(("budget.view", "owner"), self._grants_of("owner"))
            self.assertNotIn(("household.view", "owner"), self._grants_of("op"))
            with Session(engine) as s:
                self.assertEqual(
                    authz.effective_permissions(s, "owner", owner="owner"),
                    frozenset(),
                )
            # Explicit-role set_user_db restores admin and re-projects grants (with same role no-op branch too).
            self.assertEqual(store_sql.set_user_db("owner", "h1", "admin"), "admin")
            self.assertIn(("budget.view", "owner"), self._grants_of("owner"))
            self.assertIn(("household.view", "owner"), self._grants_of("op"))
            self.assertEqual(store_sql.set_user_db("owner", "h1b", "admin"), "admin")

    def test_provision_budget_owner_guards_conflicts_and_idempotency(self):
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.db)}):
            db.init_db(self.db)
            self.assertEqual(store_sql.provision_budget_owner("alice"), "users_not_authoritative")
            engine, _ = db.init_db(self.db)
            store_sql.cutover_users(
                engine,
                {"alice": "h1", "admin2": "h2", "dis_adm": "h3", "op": "h4", "dorm": "h5", "del": "h6"},
            )
            store_sql.set_user_db("dis_adm", "h3", "admin")
            store_sql.set_user_disabled("dis_adm", True)
            store_sql.set_user_db("op", "h4", "operator")
            with Session(engine) as s, s.begin():
                # "dorm" has admin role but dormant BudgetHouseholdMember pointing to alice.
                s.add(db.BudgetHouseholdMember(member_username="dorm", owner="alice", grant_id="2" * 64))
                # "del" has admin role but a non-self PermissionGrant targeting alice.
                s.add(db.PermissionGrant(username="del", permission_id="budget.receipts.add", target_owner="alice"))

            self.assertEqual(store_sql.provision_budget_owner("ghost"), "not_found")
            self.assertEqual(store_sql.provision_budget_owner("\ud800"), "not_found")
            self.assertEqual(store_sql.provision_budget_owner("dis_adm"), "disabled")
            self.assertEqual(store_sql.provision_budget_owner("op"), "not_admin")
            self.assertEqual(store_sql.provision_budget_owner("dorm"), "conflict")
            self.assertEqual(store_sql.provision_budget_owner("del"), "conflict")

            # Default legacy phase seeds all 6 direct self grants in the owner transaction.
            self.assertEqual(store_sql.provision_budget_owner("admin2"), "ok")
            self.assertEqual(
                set(self._grants_of("admin2")),
                {(pid, "admin2") for pid in store_sql._LEGACY_SELF_BUDGET_GRANTS},
            )

            self._set_meta("authz_policy_phase", "catalog_compat")
            self.assertEqual(store_sql.provision_budget_owner("alice"), "ok")
            # Simulate a later custom grant edit under alice; repeat provision must leave edits intact!
            with Session(engine) as s, s.begin():
                row = s.execute(
                    select(db.PermissionGrant).where(
                        db.PermissionGrant.username == "alice",
                        db.PermissionGrant.permission_id == "budget.edit",
                    )
                ).scalar_one()
                s.delete(row)
            self.assertEqual(store_sql.provision_budget_owner("alice"), "already_provisioned")
            self.assertNotIn(("budget.edit", "alice"), self._grants_of("alice"))

    def test_bridge_refuses_custom_phase_and_protects_authoritative_users_in_mirrors(self):
        engine, _ = db.init_db(self.db)
        store_sql.cutover_users(engine, {"sqlite_only": "h-db"})
        reg = Registry(self.dir / "r.json")
        store = AutomationStore(self.dir / "a.json")

        # Authoritative SQLite users are never overwritten or deleted by mirror_json_to_db / shadow_import.
        self._set_meta("authz_policy_phase", "catalog_compat")
        with Session(engine) as s, s.begin():
            s.get(db.User, "sqlite_only").budget_owner = True
            store_sql.sync_legacy_grants(s)
        self.assertTrue(store_sql.shadow_import(reg, store, {"json_only": "h-j"}, path=self.db))
        with Session(engine) as s:
            self.assertIsNotNone(s.get(db.User, "sqlite_only"))
            self.assertIsNone(s.get(db.User, "json_only"))
            self.assertIn(("budget.view", "sqlite_only"), self._grants_of("sqlite_only"))

        # Setting catalog_custom causes all PR 1 bridge entry points and mirrors to refuse cleanly.
        self._set_meta("authz_policy_phase", "catalog_custom")
        reg.nodes = {"9": dict(RICH_NODE)}
        self.assertFalse(store_sql.shadow_import(reg, store, {"json_only": "h-j"}, path=self.db))
        with Session(engine) as s:
            self.assertIsNone(s.get(db.Node, "9"))

        with self.assertRaises(authz.PolicyPhaseUnsupported):
            store_sql.cutover_import(engine, reg, store, {"json_only": "h-j"})
        with self.assertRaises(authz.PolicyPhaseUnsupported):
            store_sql.cutover_users(engine, {"json_only": "h-j"})
        with self.assertRaises(authz.PolicyPhaseUnsupported):
            with Session(engine) as s, s.begin():
                store_sql.sync_legacy_grants(s)

        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.db)}):
            for fn in (
                lambda: store_sql.check_supported_policy_phase(),
                lambda: store_sql.users_db_authoritative(),
                lambda: store_sql.current_users(),
                lambda: store_sql.load_auth_state("sqlite_only"),
                lambda: store_sql.load_login_snapshot("sqlite_only"),
                lambda: store_sql.open_stores(
                    registry_path=self.dir / "r.json",
                    automations_path=self.dir / "a.json",
                    users_path=self.users_json,
                ),
                lambda: store_sql.set_user_db("sqlite_only", "new"),
                lambda: store_sql.set_user_role("sqlite_only", "viewer"),
                lambda: store_sql.set_user_disabled("sqlite_only", True),
                lambda: store_sql.add_user("other", "h", "viewer"),
                lambda: store_sql.set_user_password("sqlite_only", "new"),
                lambda: store_sql.activate_sessions_v2(),
                lambda: store_sql.provision_budget_owner("sqlite_only"),
            ):
                with self.assertRaises(authz.PolicyPhaseUnsupported):
                    fn()
            # finish_sqlite_login raises if phase flips to custom while scrypt ran.
            snap = store_sql.LoginSnapshot(
                username="sqlite_only",
                backend="sqlite",
                password_hash="h-db",
                generation=0,
                policy_phase="legacy",
                session_phase="legacy",
                db_path=self.db,
            )
            with self.assertRaises(authz.PolicyPhaseUnsupported):
                store_sql.finish_sqlite_login(snap, native=False, now=1.0, secret=b"sec")

    def test_load_auth_state_and_snapshot_keep_single_snapshot_under_interleaving(self):
        engine, _ = db.init_db(self.db)
        store_sql.cutover_users(engine, {"tata": "h1"})
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.db)}):
            real_req = authz.require_supported_policy_phase
            fired = {"auth_state": False}

            def interleave_v2_and_reset(session):
                res = real_req(session)
                if not fired["auth_state"]:
                    fired["auth_state"] = True
                    store_sql.activate_sessions_v2(path=self.db)
                    self.assertTrue(store_sql.set_user_password("tata", "h2"))
                return res

            with mock.patch("hestia.authz.require_supported_policy_phase", side_effect=interleave_v2_and_reset):
                state = store_sql.load_auth_state("tata")
            # Must never see session_phase="legacy" paired with generation=1 from a newer snapshot.
            self.assertEqual((state.session_phase, state.generation), ("legacy", 0))
            after = store_sql.load_auth_state("tata")
            self.assertEqual((after.session_phase, after.generation), ("v2", 1))

            # Single-snapshot read also protects load_login_snapshot from torn hash vs generation.
            fired["snap"] = False

            def interleave_reset_during_snapshot(session):
                res = real_req(session)
                if not fired["snap"]:
                    fired["snap"] = True
                    self.assertTrue(store_sql.set_user_password("tata", "h3"))
                return res

            with mock.patch("hestia.authz.require_supported_policy_phase", side_effect=interleave_reset_during_snapshot):
                snap = store_sql.load_login_snapshot("tata")
            self.assertEqual((snap.password_hash, snap.generation), ("h2", 1))
            self.assertIsNone(store_sql.finish_sqlite_login(snap, native=False, now=1.0, secret=b"sec"))

            # Unencodable usernames are treated as ineligible without SQLite binding errors.
            self.assertFalse(store_sql.load_auth_state("\ud800").enabled)
            self.assertIsNone(store_sql.load_login_snapshot("\ud800").password_hash)

    def test_concurrent_cutover_rechecks_authority_under_begin_immediate(self):
        import sqlite3
        import threading

        reg_json, auto_json = self.dir / "registry.json", self.dir / "automations.json"
        Registry(reg_json, mode="proxy").save()
        AutomationStore(auto_json).save()
        self.users_json.write_text('{"tata": "stale-pw"}', encoding="utf-8")

        # 1. Interleaved second open_stores boot after first boot already cut over + password reset.
        real_load_users = auth.load_users
        boot2_reg = Registry.load(reg_json)
        boot2_store = AutomationStore.load(auto_json)
        raced_reset = {"done": False}

        def race_first_boot(path=None):
            data = real_load_users(path)
            if not raced_reset["done"]:
                raced_reset["done"] = True
                engine, _ = db.init_db(self.db)
                store_sql.cutover_import(
                    engine,
                    Registry(reg_json, mode="standalone"),
                    AutomationStore(auto_json),
                    {"tata": "initial-pw"},
                )
                store_sql.cutover_users(engine, {"tata": "initial-pw"})
                self.assertTrue(store_sql.set_user_password("tata", "reset-pw"))
                self.assertEqual(store_sql.add_user("bob", "bob-pw", "viewer"), "ok")
            return data

        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.db)}), \
             mock.patch("hestia.auth.load_users", side_effect=race_first_boot):
            reg, _ = store_sql.open_stores(
                registry_path=reg_json,
                automations_path=auto_json,
                users_path=self.users_json,
                persist="sqlite",
            )
            self.assertEqual(reg.mode, "standalone")
            self.assertEqual(store_sql.current_users(), {"tata": "reset-pw", "bob": "bob-pw"})

        # Direct delayed cutover_import / cutover_users with stale snapshots also no-op.
        engine, _ = db.init_db(self.db)
        store_sql.cutover_import(engine, boot2_reg, boot2_store, {"tata": "stale-pw"})
        store_sql.cutover_users(engine, {"tata": "stale-pw"})
        self.assertEqual(store_sql.read_mode(engine), "standalone")
        self.assertEqual(store_sql.load_users_db(engine), {"tata": "reset-pw", "bob": "bob-pw"})
        engine.dispose()

        # 2. Connection-level BEGIN IMMEDIATE blocks delayed boot until first writer commits authority.
        db2_path = self.dir / "race_lock.db"
        engine2, _ = db.init_db(db2_path)
        lock_conn = sqlite3.connect(str(db2_path), isolation_level=None, timeout=5.0)
        lock_conn.execute("PRAGMA busy_timeout=5000")
        lock_conn.execute("BEGIN IMMEDIATE")
        errors: list[BaseException] = []

        def delayed_cutover():
            try:
                store_sql.cutover_users(engine2, {"tata": "stale-pw"})
            except BaseException as exc:  # pragma: no cover - failure guard
                errors.append(exc)

        thread = threading.Thread(target=delayed_cutover)
        thread.start()
        time.sleep(0.05)
        self.assertTrue(thread.is_alive())
        lock_conn.execute(
            "INSERT INTO users (username, password_hash, role, disabled, credential_generation, budget_owner) "
            "VALUES ('tata', 'winner-pw', 'admin', 0, 0, 0)"
        )
        lock_conn.execute("INSERT INTO app_meta (key, value) VALUES ('users_authoritative', '1')")
        lock_conn.execute("COMMIT")
        lock_conn.close()
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(store_sql.load_users_db(engine2), {"tata": "winner-pw"})
        engine2.dispose()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
