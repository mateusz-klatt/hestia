"""Explicit calendar discovery, reconciliation and local SQLite event management."""
from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
import json
import sys

from .config import ConfigError, load_config, make_provider
from .runtime import SyncBusyError, run_once, sync_lock


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="JSON file (default: HESTIA_CALENDAR_CONFIG)")
    parser.add_argument("--db", help="SQLite path (default: HESTIA_DB or /data/hestia.db)")
    commands = parser.add_subparsers(dest="command", required=True)
    discover = commands.add_parser("discover", help="List available calendars and actual write access")
    discover.add_argument("--account", required=True)
    commands.add_parser("once", help="Run one pull/reconcile/push round")
    commands.add_parser("status", help="Show endpoint state and pending operations")
    events = commands.add_parser("events", help="List local canonical events")
    events.add_argument("group")
    events.add_argument("--include-deleted", action="store_true")
    put = commands.add_parser("put", help="Create or replace a local event using normalized JSON")
    put.add_argument("group")
    put.add_argument("--file", required=True, help="JSON path, or - for stdin")
    put.add_argument("--event", help="Existing local event ID to edit")
    put.add_argument("--expected-revision", type=int)
    delete = commands.add_parser("delete", help="Tombstone an event and queue external deletions")
    delete.add_argument("group")
    delete.add_argument("event")
    delete.add_argument("--expected-revision", type=int)
    return parser


def _read_payload(path):
    try:
        if path == "-":
            return json.load(sys.stdin)
        with open(path, encoding="utf-8") as source:
            return json.load(source)
    except (OSError, ValueError):
        raise ConfigError("Cannot read event JSON") from None


def execute(args):
    config = load_config(args.config)
    if config is None:
        raise ConfigError("Set HESTIA_CALENDAR_CONFIG or pass --config")
    if args.command == "discover":
        if args.account not in config.accounts:
            raise ConfigError("Unknown calendar account")
        items = make_provider(config.accounts[args.account], "").discover()
        return [asdict(item) if is_dataclass(item) else item for item in items]
    if args.command == "once":
        return run_once(config, path=args.db)
    from ..db import db_path, init_db
    from .store import SyncStore

    target = db_path() if args.db is None else args.db
    # Configure under the same process lock as the daemon. The local write itself is
    # serialized by SQLite, and is allowed between in-flight external sync requests.
    with sync_lock(target):
        _, sessions = init_db(target)
        store = SyncStore(sessions)
        store.configure(config.store_groups())
    if args.command == "status":
        return store.status()
    configured_groups = {group.id for group in config.groups}
    if args.group not in configured_groups:
        raise ConfigError("Unknown or disabled calendar group")
    if args.command == "events":
        return store.list_events(args.group, include_deleted=args.include_deleted)
    if args.command == "put":
        return store.save_local(args.group, _read_payload(args.file), event_id=args.event,
                                expected_revision=args.expected_revision)
    return store.delete_local(args.group, args.event, expected_revision=args.expected_revision)


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        result = execute(args)
    except (ConfigError, SyncBusyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        # Never echo payloads, tokens, provider response bodies or arbitrary exceptions.
        print(f"Calendar operation failed ({type(exc).__name__}); check configuration, "
              "event format, account access and status", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
