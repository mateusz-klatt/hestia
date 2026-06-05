"""JSON → SQLite shadow import (Phase 2 of #57).

Mirrors the live JSON stores (registry, automations, users) into the SQLite DB as a
SHADOW: the JSON files stay the source of truth for every read/write; this only keeps a
DB copy in sync so Phase 3 can cut over to it. Replace-mirror semantics — upsert every
present row AND delete DB rows absent from the JSON — so the DB is an EXACT copy and the
import is idempotent (re-running on each boot can never let the shadow drift from JSON).

Losslessness: a registry node is stored as its exact entry dict serialised to JSON (so
unknown / future fields survive verbatim — a normalised table would drop them); an
automation rule as its canonical ``Rule.to_dict`` JSON (the form already written to
``automations.json``); a user as its scrypt hash string verbatim.

``shadow_import`` is best-effort: any failure is logged and swallowed so a DB problem can
never stop the JSON-backed house from booting (the live path stays on JSON in Phase 2).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from . import auth
from .automations import AutomationStore, Rule
from .db import AppMeta, Audit, Automation, Node, User, UserSetting, db_path, init_db, session_scope
from .registry import Registry

log = logging.getLogger("hestia.store_sql")

# RBAC roles (#73). Ranked low→high: the web authz middleware admits a request when the user's role
# rank ≥ the route's required floor. ``admin`` is the legacy/pre-roles role — accounts that predate
# roles (migration backfill + json→sqlite cutover imports) get it so the operator keeps full access;
# ``viewer`` is the least-privilege default a brand-new account gets unless the CLI sets one explicitly.
ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}
LEGACY_ROLE = "admin"
DEFAULT_NEW_ROLE = "viewer"

# app_meta markers: set once at cutover so subsequent boots load from the DB instead of re-importing
# the (now-frozen) JSON. registry + automations cut over together (Phase 3); users separately (Phase 4).
_AUTHORITATIVE = "registry_authoritative"
_USERS_AUTHORITATIVE = "users_authoritative"
_DEVICE_STATE_KEY = "device_state"
_ROOM_ICONS_KEY = "room_icons"
_STATS_TABLES = (
    ("app_meta", AppMeta),
    ("nodes", Node),
    ("automations", Automation),
    ("users", User),
    ("user_settings", UserSetting),
    ("audit", Audit),
)


def _dump(obj) -> str:
    """Deterministic JSON for a stored payload (sorted keys → stable, idempotent rows)."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def _upsert(session: Session, model, pk_name: str, pk_value, values: dict) -> None:
    """Insert ``model(pk=pk_value, **values)`` or update the existing row's ``values``."""
    obj = session.get(model, pk_value)
    if obj is None:
        session.add(model(**{pk_name: pk_value, **values}))
    else:
        for field, value in values.items():
            setattr(obj, field, value)


def mirror_json_to_db(session: Session, *, registry: Registry, store: AutomationStore, users: dict) -> None:
    """Replace-mirror the three JSON stores into the DB within ``session`` (caller commits):
    upsert every present row, delete DB rows absent from the JSON. The DB ends up an exact copy.
    A ``not_in`` over an EMPTY set deletes ALL rows (SQLAlchemy renders delete-all) — intended:
    an emptied JSON store empties its mirror too."""
    _upsert(session, AppMeta, "key", "mode", {"value": registry.mode})

    node_keys = set(registry.nodes)
    for key, entry in registry.nodes.items():
        _upsert(session, Node, "key", key, {"entry_json": _dump(entry)})
    session.execute(delete(Node).where(Node.key.not_in(node_keys)))

    rule_ids = set(store.rules)
    for position, (rule_id, rule) in enumerate(store.rules.items()):
        _upsert(session, Automation, "id", rule_id, {"position": position, "rule_json": _dump(rule.to_dict())})
    session.execute(delete(Automation).where(Automation.id.not_in(rule_ids)))

    usernames = set(users)
    for username, password_hash in users.items():
        # JSON users predate roles → import them as admin (legacy single-tier). This mirror runs only
        # pre-cutover (the shadow is a no-op once HESTIA_PERSIST=sqlite) and at the one-time cutover, so
        # it never clobbers a role set in the authoritative DB.
        _upsert(session, User, "username", username,
                {"password_hash": password_hash, "role": LEGACY_ROLE})
    session.execute(delete(User).where(User.username.not_in(usernames)))


def shadow_import(registry: Registry, store: AutomationStore, users: dict, *, path=None) -> bool:
    """Open/upgrade the DB and replace-mirror the JSON stores into it. Returns True on success.
    Best-effort: ANY failure is logged and returns False — the shadow must never break boot."""
    engine = None
    try:
        engine, session_factory = init_db(path)
        with session_scope(session_factory) as session:
            mirror_json_to_db(session, registry=registry, store=store, users=users)
        return True
    except Exception:   # shadow is best-effort; a DB issue must NOT stop a JSON-backed boot
        log.exception("SQLite shadow import failed — continuing on JSON")
        return False
    finally:
        if engine is not None:   # always release the pool/WAL handle, even on the failure path
            engine.dispose()


# --- Phase 3: DB as the authoritative backend -------------------------------------------------
# The cutover reuses ALL of hestia's cancel-safe persistence machinery (_write_and_settle /
# _persist_obj / _commit_automation / _control_graduate) unchanged: those call obj.write_payload
# with the SAME serialized JSON payload they'd write to disk. We just give the store a backend
# `writer` that lands that payload in the DB instead. A DB error is raised as OSError so every
# existing `except OSError` handler (autosave, graduate) treats it exactly like a failed file write.


def _payload_to_db(engine, payload: bytes, mirror) -> None:
    """Apply a serialized store payload to the DB in one SYNCHRONOUS, blocking transaction —
    returns only after the commit lands, so the executor-thread completion semantics match
    ``os.replace``. This is the hard persistence boundary: ANY failure (DB error, bad JSON, or a
    malformed payload shape) is re-raised as
    ``OSError`` — chained, so the real cause is still logged — so the cancel-safe write path
    (``_persist_obj`` re-arms ``dirty``; autosave logs+retries) handles it exactly like a failed
    file write and a leaked non-OSError can never kill the autosave loop."""
    try:
        data = json.loads(payload)
        with Session(engine) as session, session.begin():
            mirror(session, data)
    except Exception as exc:   # hard boundary: never leak a non-OSError into the OSError-only handlers
        raise OSError(f"SQLite persist failed: {exc!r}") from exc


def _mirror_registry_payload(session: Session, data: dict) -> None:
    _upsert(session, AppMeta, "key", "mode", {"value": data.get("mode", "proxy")})
    nodes = data.get("nodes", {})
    for key, entry in nodes.items():
        _upsert(session, Node, "key", key, {"entry_json": _dump(entry)})
    session.execute(delete(Node).where(Node.key.not_in(set(nodes))))


def _mirror_automations_payload(session: Session, data: dict) -> None:
    rules = data.get("rules", [])
    ids = set()
    for position, rule in enumerate(rules):
        ids.add(rule["id"])
        _upsert(session, Automation, "id", rule["id"], {"position": position, "rule_json": _dump(rule)})
    session.execute(delete(Automation).where(Automation.id.not_in(ids)))


def registry_db_writer(engine):
    """A ``write_payload`` backend for a Registry that lands the payload in the DB."""
    return lambda payload: _payload_to_db(engine, payload, _mirror_registry_payload)


def automations_db_writer(engine):
    """A ``write_payload`` backend for an AutomationStore that lands the payload in the DB."""
    return lambda payload: _payload_to_db(engine, payload, _mirror_automations_payload)


def read_mode(engine) -> str:
    """The persisted runtime mode from the DB (``app_meta.mode``), defaulting to ``proxy``."""
    with Session(engine) as session:
        row = session.get(AppMeta, "mode")
    return row.value if row is not None else "proxy"


def is_db_authoritative(engine) -> bool:
    """True once the one-time cutover import has run (DB is the source of truth, not the JSON)."""
    with Session(engine) as session:
        return session.get(AppMeta, _AUTHORITATIVE) is not None


def load_registry(engine, path, *, writer) -> Registry:
    """Build a Registry from the DB rows, wired to persist back to the DB via ``writer``."""
    with Session(engine) as session:
        nodes = {n.key: json.loads(n.entry_json) for n in session.execute(select(Node)).scalars()}
    return Registry(path, nodes=nodes, mode=read_mode(engine), writer=writer)


def load_automations(engine, path, *, writer) -> AutomationStore:
    """Build an AutomationStore from the DB rows (eval order = ``position``), persisting via ``writer``.
    A row that fails validation is skipped+logged — one bad rule can't lock out the rest (mirrors
    ``AutomationStore.load``)."""
    store = AutomationStore(path, writer=writer)
    with Session(engine) as session:
        rows = session.execute(select(Automation).order_by(Automation.position)).scalars().all()
    for row in rows:
        try:
            rule = Rule.from_dict(json.loads(row.rule_json))
        except ValueError as exc:
            log.warning("automations DB: skipping invalid rule %r (%s)", row.id, exc)
            continue
        store.rules[rule.id] = rule
    return store


def cutover_import(engine, registry: Registry, store: AutomationStore, users: dict) -> None:
    """One-time: replace-mirror the current JSON-loaded state into the DB and set the authority
    marker, in ONE transaction — after this the DB is the source of truth for registry+automations.
    The mode mirrored here is the JSON registry's persisted mode; the shipped docker-compose leaves
    HESTIA_MODE unset so the DB mode stays the single source of truth (an env override that is later
    removed would surface the DB mode on the next boot — don't mix HESTIA_MODE with the sqlite backend)."""
    with Session(engine) as session, session.begin():
        mirror_json_to_db(session, registry=registry, store=store, users=users)
        _upsert(session, AppMeta, "key", _AUTHORITATIVE, {"value": "1"})


def open_stores(*, registry_path, automations_path, users_path, persist=None):
    """Return ``(registry, store)`` for the selected backend. ``HESTIA_PERSIST`` (default ``json``)
    keeps the JSON files authoritative; ``sqlite`` makes the DB authoritative — importing the
    current JSON once (then marking it) and loading from the DB thereafter, with DB-backed writers
    so every save lands in the DB. Reuses the cancel-safe write path unchanged."""
    persist = (persist if persist is not None else os.environ.get("HESTIA_PERSIST", "json")).lower()
    if persist != "sqlite":
        return Registry.load(registry_path), AutomationStore.load(automations_path)
    engine, _ = init_db()
    if not is_db_authoritative(engine):
        cutover_import(engine, Registry.load(registry_path), AutomationStore.load(automations_path),
                       auth.load_users(Path(users_path)))
    if not is_users_db_authoritative(engine):   # Phase 4: flip users (independent of the registry cutover)
        users = auth.load_users(Path(users_path))
        if users:                                # only promote a REAL, non-empty users.json: load_users returns
            cutover_users(engine, users)         # {} for a missing/unreadable/malformed file, and promoting that
            #                                      would wipe the DB users + set the marker → permanent lockout.
            #                                      Empty/bad → skip (stay on JSON, retry next boot).
    return (load_registry(engine, registry_path, writer=registry_db_writer(engine)),
            load_automations(engine, automations_path, writer=automations_db_writer(engine)))


def export_to_json(*, registry_path, automations_path, path=None) -> bool:
    """Rebuild the registry + automations JSON files from the DB (rollback escape hatch: switch back
    to HESTIA_PERSIST=json with current data). Returns True on success. NOTE: ``users.json`` is NOT
    rewritten — in Phase 3 the auth users stay JSON-authoritative (the DB user rows are only the
    frozen cutover snapshot, so writing them back would clobber any post-cutover account change)."""
    engine, _ = init_db(path)
    try:
        with Session(engine) as session:
            nodes = {n.key: json.loads(n.entry_json) for n in session.execute(select(Node)).scalars()}
            rules = [json.loads(r.rule_json)
                     for r in session.execute(select(Automation).order_by(Automation.position)).scalars()]
        Registry(registry_path, nodes=nodes, mode=read_mode(engine)).save()
        out = AutomationStore(automations_path)
        out.rules = {r["id"]: Rule.from_dict(r) for r in rules}
        out.save()
        return True
    finally:
        engine.dispose()


# --- Phase 4: auth users → DB --------------------------------------------------------------------
# Users cut over separately from registry+automations (own marker), since the live box was already
# cut over for registry in Phase 3 with users still JSON-authoritative. Reads happen only at LOGIN
# (rare — the auth middleware gates per-request on the signed cookie, not a user lookup), so a short
# per-login engine is fine; the stdlib auth primitives (hash/verify/session) stay backend-agnostic.


def is_users_db_authoritative(engine) -> bool:
    with Session(engine) as session:
        return session.get(AppMeta, _USERS_AUTHORITATIVE) is not None


def users_db_authoritative() -> bool:
    """Whether the DB is the authoritative users store right now (own short engine). Uses init_db so
    a fresh DB (e.g. the auth CLI running before the daemon ever booted) has its schema — then the
    marker is simply absent and this returns False, so the caller falls back to JSON."""
    engine, _ = init_db()
    try:
        return is_users_db_authoritative(engine)
    finally:
        engine.dispose()


def load_users_db(engine) -> dict:
    with Session(engine) as session:
        return {u.username: u.password_hash for u in session.execute(select(User)).scalars()}


def cutover_users(engine, users: dict) -> None:
    """One-time: replace-mirror users.json into the DB and set the users authority marker (one txn).
    Runs at boot in sqlite mode until set — independent of the registry cutover, so a box already on
    sqlite for registry+automations (Phase 3) flips users on the next boot."""
    with Session(engine) as session, session.begin():
        for username, password_hash in users.items():
            # legacy (pre-roles) JSON accounts → admin so the operator keeps full access after cutover
            _upsert(session, User, "username", username,
                    {"password_hash": password_hash, "role": LEGACY_ROLE})
        session.execute(delete(User).where(User.username.not_in(set(users))))
        _upsert(session, AppMeta, "key", _USERS_AUTHORITATIVE, {"value": "1"})


def set_user_db(username: str, password_hash: str, role: str = DEFAULT_NEW_ROLE, *, path=None) -> None:
    """Upsert one user (username, hash, role) into the DB (auth CLI / change-password). Own short
    session; WAL lets this run concurrently with the daemon. The caller passes the role explicitly so
    a forgotten role can never silently become admin (the CLI preserves an existing user's role on a
    bare password reset and defaults a brand-new account to viewer)."""
    engine, _ = init_db(path)
    try:
        with Session(engine) as session, session.begin():
            _upsert(session, User, "username", username, {"password_hash": password_hash, "role": role})
    finally:
        engine.dispose()


def get_user_db_role(username: str, *, path=None) -> "str | None":
    """The role stored for ``username`` in the DB, or ``None`` if the account doesn't exist. DB-only (no
    JSON fallback) — for the auth CLI's preserve-role-on-password-reset and the ``role`` subcommand."""
    engine, _ = init_db(path)
    try:
        with Session(engine) as session:
            row = session.get(User, username)
        return row.role if row is not None else None
    finally:
        engine.dispose()


def set_user_role(username: str, role: str, *, path=None) -> bool:
    """Change an existing user's role in the DB. Returns ``False`` (and changes nothing) when the user
    doesn't exist — never creates an account (use ``set_user_db`` for that)."""
    engine, _ = init_db(path)
    try:
        with Session(engine) as session, session.begin():
            row = session.get(User, username)
            if row is None:
                return False
            row.role = role
        return True
    finally:
        engine.dispose()


def current_user_role(username) -> "str | None":
    """The access role of ``username`` in the ACTIVE users backend, for the web authz middleware:

    * sqlite + users-authoritative → the stored ``role`` (``None`` if the account no longer exists, so a
      removed/renamed user is denied at once rather than at cookie expiry);
    * otherwise (JSON backend, or sqlite before the users cutover) → ``admin`` iff the username still
      exists in the JSON users map (legacy single-tier, back-compat), else ``None``.

    ``None`` for a non-string / empty / unknown username so the caller fails closed. Uses the CACHED
    engine WITHOUT disposing it — unlike the per-login helpers, this runs on every authenticated request,
    so churning the connection pool each call would be wasteful (the cache owns the engine lifecycle)."""
    if not isinstance(username, str) or not username:
        return None
    if os.environ.get("HESTIA_PERSIST", "json").lower() == "sqlite":
        engine, _ = init_db()
        if is_users_db_authoritative(engine):
            with Session(engine) as session:
                row = session.get(User, username)
            return row.role if row is not None else None
    return LEGACY_ROLE if username in auth.load_users() else None


def current_users(*, users_path=None) -> dict:
    """The users map from the active backend: the DB when sqlite + users-authoritative, else the JSON
    file. Used by the login handler (the only place that reads the users store)."""
    if os.environ.get("HESTIA_PERSIST", "json").lower() == "sqlite":
        engine, _ = init_db()               # idempotent; ensures the schema even on an odd-state boot
        try:
            if is_users_db_authoritative(engine):
                return load_users_db(engine)
        finally:
            engine.dispose()
    return auth.load_users(Path(users_path) if users_path is not None else None)


# --- Phase 4b/#55: per-user UI settings --------------------------------------------------------

def _settings_enabled() -> bool:
    return os.environ.get("HESTIA_PERSIST", "json").lower() == "sqlite"


def _cap_setting(value):
    return value[:64] if isinstance(value, str) else None


def _settings_dict(row) -> dict:
    return {"locale": row.locale, "temp_scale": row.temp_scale, "theme": row.theme}


def get_user_settings(username) -> "dict | None":
    """Per-user UI settings from SQLite, or None when settings are unavailable/no row exists."""
    if not _settings_enabled():
        return None
    engine, _ = init_db()
    try:
        with Session(engine) as session:
            row = session.get(UserSetting, username)
            return None if row is None else _settings_dict(row)
    finally:
        engine.dispose()


def set_user_settings(username, **fields) -> bool:
    """Upsert ONLY the named UI-settings columns for one user (an absent kwarg is left untouched, so
    a partial update preserves the others). The merge happens inside a single transaction, so two
    concurrent partial updates can't lose each other's field. Returns False under the JSON backend."""
    if not _settings_enabled():
        return False
    engine, session_factory = init_db()
    try:
        with session_scope(session_factory) as session:
            _upsert(session, UserSetting, "username", username,
                    {key: _cap_setting(value) for key, value in fields.items()})
        return True
    finally:
        engine.dispose()


# --- Shared room icon preferences (#54) --------------------------------------------------------

def _room_icon_map(value: str) -> dict:
    try:
        data = json.loads(value)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(room): icon for room, icon in data.items() if isinstance(icon, str)}


def get_room_icons() -> dict:
    """Shared per-room emoji choices from SQLite AppMeta, or {} when unavailable."""
    if not _settings_enabled():
        return {}
    engine, _ = init_db()
    try:
        with Session(engine) as session:
            row = session.get(AppMeta, _ROOM_ICONS_KEY)
        return {} if row is None else _room_icon_map(row.value)
    finally:
        engine.dispose()


def set_room_icon(room, icon) -> bool:
    """Set or clear one shared room emoji in a single read-merge-write transaction."""
    if not _settings_enabled():
        return False
    room_key = room[:64] if isinstance(room, str) else ""
    icon_value = icon[:16] if isinstance(icon, str) else ""
    engine, session_factory = init_db()
    try:
        with session_scope(session_factory) as session:
            row = session.get(AppMeta, _ROOM_ICONS_KEY)
            icons = {} if row is None else _room_icon_map(row.value)
            if icon_value:
                icons[room_key] = icon_value
            else:
                icons.pop(room_key, None)
            _upsert(session, AppMeta, "key", _ROOM_ICONS_KEY, {"value": _dump(icons)})
        return True
    finally:
        engine.dispose()


# --- Best-effort live device-state cache (#48a) -----------------------------------------------

def save_device_state(snapshot: dict) -> bool:
    """Persist the live telemetry snapshot in SQLite AppMeta. Best-effort and SQLite-only."""
    if not _settings_enabled():
        return False
    engine = None
    try:
        engine, session_factory = init_db()
        with session_scope(session_factory) as session:
            _upsert(session, AppMeta, "key", _DEVICE_STATE_KEY, {"value": _dump(snapshot)})
        return True
    except Exception:
        log.exception("device-state cache save failed")
        return False
    finally:
        if engine is not None:
            engine.dispose()


def load_device_state() -> "dict | None":
    """Load the cached live telemetry snapshot, or None when unavailable/invalid."""
    if not _settings_enabled():
        return None
    engine = None
    try:
        engine, _ = init_db()
        with Session(engine) as session:
            row = session.get(AppMeta, _DEVICE_STATE_KEY)
        if row is None:
            return None
        data = json.loads(row.value)
        return data if isinstance(data, dict) else None
    except Exception:
        log.exception("device-state cache load failed")
        return None
    finally:
        if engine is not None:
            engine.dispose()


# --- Operator DB observability (#55/#57 P6a) ----------------------------------------------------

def _sqlite_files(path: Path) -> tuple[Path, Path, Path]:
    return path, Path(f"{path}-wal"), Path(f"{path}-shm")


def _db_file_bytes(path: Path) -> int:
    """On-disk size of the SQLite DB plus its WAL/SHM sidecars (only those that exist)."""
    return sum(os.path.getsize(f) for f in _sqlite_files(path) if f.exists())


def db_stats(path=None) -> dict:
    """SQLite file growth stats for the operator: DB/WAL/SHM bytes + per-table row counts."""
    resolved = db_path() if path is None else Path(path)
    engine, _ = init_db(path)
    try:
        with Session(engine) as session:
            tables = {
                name: int(session.execute(select(func.count()).select_from(model)).scalar_one())
                for name, model in _STATS_TABLES
            }
        return {"file_bytes": _db_file_bytes(resolved), "tables": tables}
    finally:
        engine.dispose()


# --- Phase 5: audit log (#56) --------------------------------------------------------------------
# Append-only "who did what" log in the `audit` table (actor = a username | "automation:<rule>" |
# "system"). Written off the event loop, best-effort (a failed audit write must never disturb the
# action it records). Capped by row count + age so it can't grow unbounded on the home box.

AUDIT_MAX_ROWS = int(os.environ.get("HESTIA_AUDIT_MAX_ROWS", "50000"))
AUDIT_MAX_AGE_S = float(os.environ.get("HESTIA_AUDIT_MAX_AGE_DAYS", "365")) * 86400.0


def open_audit_engine():
    """An engine for the audit log, BEST-EFFORT: returns the engine, or ``None`` (logged) on any
    init failure so a DB problem disables auditing this run rather than preventing boot — the audit
    log is observability, never a reason the house can't start."""
    try:
        engine, _ = init_db()
        return engine
    except Exception:
        log.exception("audit DB init failed — audit log disabled this run")
        return None


def append_audit(engine, *, actor, action, target=None, detail=None, result=None, ts,
                 max_rows=AUDIT_MAX_ROWS, max_age_s=AUDIT_MAX_AGE_S) -> None:
    """Append one audit row, then prune to the newest ``max_rows`` and drop anything older than
    ``max_age_s`` (so the table stays bounded). One transaction."""
    with Session(engine) as session, session.begin():
        session.add(Audit(ts=ts, actor=actor, action=action, target=target, detail=detail, result=result))
        session.flush()
        session.execute(delete(Audit).where(Audit.ts < ts - max_age_s))
        # row cap: the id at offset max_rows (newest-first) is the first to drop — delete it + older.
        oldest_kept = session.execute(
            select(Audit.id).order_by(Audit.id.desc()).limit(1).offset(max_rows)).scalar()
        if oldest_kept is not None:
            session.execute(delete(Audit).where(Audit.id <= oldest_kept))


def recent_audit(engine, limit=200) -> list:
    """The most recent audit rows (newest first) as plain dicts for the feed endpoint."""
    with Session(engine) as session:
        rows = session.execute(select(Audit).order_by(Audit.id.desc()).limit(limit)).scalars().all()
    return [{"id": r.id, "ts": r.ts, "actor": r.actor, "action": r.action,
             "target": r.target, "detail": r.detail, "result": r.result} for r in rows]


def _cli(argv) -> int:  # pragma: no cover - thin env-driven wrapper around export_to_json
    if argv[:1] != ["export"]:
        print("usage: python -m hestia.store_sql export", flush=True)
        return 2
    export_to_json(registry_path=os.environ.get("HESTIA_REGISTRY", "registry.json"),
                   automations_path=os.environ.get("HESTIA_AUTOMATIONS", "automations.json"))
    print("exported DB -> JSON (registry + automations; users.json left as-is)", flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
