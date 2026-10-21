"""Authenticated calendar routes; authorization is Hestia's existing middleware."""
from __future__ import annotations

import asyncio

from aiohttp import web

from ..db import init_db
from .config import ConfigError, load_config
from .store import CalendarConflict, SyncStore

PATH = "/api/calendars"
EVENTS_PATH = PATH + "/events"
DELETE_PATH = EVENTS_PATH + "/delete"


def _store():
    config = load_config()
    if config is None:
        return None
    _, sessions = init_db()
    store = SyncStore(sessions)
    store.configure(config.store_groups())
    return store


def _read(group):
    store = _store()
    if store is None:
        return {"enabled": False, "groups": [], "events": [], "status": {"endpoints": [], "pending": []}}
    groups = store.groups()
    selected = group or (groups[0]["id"] if groups else None)
    events = store.list_events(selected) if selected is not None else []
    status = store.status()
    ids = {row["id"] for row in status["endpoints"] if row["enabled"]}
    status["endpoints"] = [row for row in status["endpoints"] if row["id"] in ids]
    status["pending"] = [row for row in status["pending"] if row["endpoint_id"] in ids]
    return {"enabled": True, "groups": groups, "events": events, "status": status}


def _write(body, deleting):
    allowed = {"group", "id", "revision"} | (set() if deleting else {"payload", "idempotency_key"})
    if not isinstance(body, dict) or set(body) - allowed:
        raise ValueError("Invalid calendar request")
    if not isinstance(body.get("group"), str):
        raise ValueError("Calendar group is required")
    event_id = body.get("id")
    if "id" in body and (not isinstance(event_id, str) or not event_id):
        raise ValueError("Invalid calendar event ID")
    if deleting and event_id is None:
        raise ValueError("Calendar event ID is required")
    if event_id is not None and (type(body.get("revision")) is not int or body["revision"] < 1):
        raise ValueError("The current calendar event revision is required")
    if event_id is None and "revision" in body:
        raise ValueError("A new calendar event cannot specify a revision")
    if event_id is None and not isinstance(body.get("idempotency_key"), str):
        raise ValueError("A new calendar event requires an idempotency key")
    if event_id is not None and "idempotency_key" in body:
        raise ValueError("Calendar create keys cannot be used for edits")
    store = _store()
    if store is None:
        raise ConfigError("Calendar synchronization is disabled")
    if deleting:
        return store.delete_local(body["group"], event_id, expected_revision=body["revision"])
    return store.save_local(body["group"], body.get("payload"), event_id=event_id,
                            expected_revision=body.get("revision"), idempotency_key=body.get("idempotency_key"))


def register(app, read_json_body):
    async def read(request):
        try:
            result = await asyncio.to_thread(_read, request.query.get("group"))
            return web.json_response(result)
        except (ConfigError, CalendarConflict):
            return web.json_response({"error": "calendar_configuration_invalid"}, status=503)
        except ValueError:
            return web.json_response({"error": "unknown_calendar_group"}, status=400)

    async def write(request):
        body, error = await read_json_body(request, 65536)
        if error:
            return body
        try:
            event = await asyncio.to_thread(_write, body, request.path == DELETE_PATH)
            return web.json_response({"ok": True, "event": event})
        except CalendarConflict:
            return web.json_response({"error": "calendar_revision_conflict"}, status=409)
        except ConfigError:
            return web.json_response({"error": "calendar_configuration_invalid"}, status=503)
        except (ValueError, TypeError):
            return web.json_response({"error": "invalid_calendar_event"}, status=400)

    app.router.add_get(PATH, read, allow_head=False)
    app.router.add_post(EVENTS_PATH, write)
    app.router.add_post(DELETE_PATH, write)
