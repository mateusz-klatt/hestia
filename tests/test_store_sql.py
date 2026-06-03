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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
