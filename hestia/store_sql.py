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

from sqlalchemy import delete
from sqlalchemy.orm import Session

from .automations import AutomationStore
from .db import AppMeta, Automation, Node, User, init_db, session_scope
from .registry import Registry

log = logging.getLogger("hestia.store_sql")


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
    upsert every present row, delete DB rows absent from the JSON. The DB ends up an exact copy."""
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
        _upsert(session, User, "username", username, {"password_hash": password_hash})
    session.execute(delete(User).where(User.username.not_in(usernames)))


def shadow_import(registry: Registry, store: AutomationStore, users: dict, *, path=None) -> bool:
    """Open/upgrade the DB and replace-mirror the JSON stores into it. Returns True on success.
    Best-effort: ANY failure is logged and returns False — the shadow must never break boot."""
    try:
        engine, session_factory = init_db(path)
        with session_scope(session_factory) as session:
            mirror_json_to_db(session, registry=registry, store=store, users=users)
        engine.dispose()
        return True
    except Exception:  # noqa: BLE001 - shadow is best-effort; a DB issue must not stop a JSON-backed boot
        log.exception("SQLite shadow import failed — continuing on JSON")
        return False
