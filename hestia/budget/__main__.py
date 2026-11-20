"""Opt-in bank ingestion with no financial payloads in CLI output."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from .config import BudgetConfigError, load_config
from .import_file import BudgetImportError, load_import
from .runtime import BudgetBusyError, budget_lock, run_once
from .store import ACCOUNT_KINDS, BudgetStore, BudgetStoreError


def execute(args):
    config = load_config(args.config)
    if config is None:
        raise BudgetConfigError("Set HESTIA_BUDGET_CONFIG or pass --config")
    if args.command == "once":
        return run_once(config, path=args.db)
    importing = args.command == "import-snapshot"
    snapshot = load_import(args.file, config, now=time.time()) if importing else None
    from ..db import db_path, init_db

    target = db_path() if args.db is None else args.db
    with budget_lock(target):
        _, sessions = init_db(target)
        store = BudgetStore(sessions)
        if importing:
            return {"import": store.import_snapshot(config, snapshot), "status": store.status()}
        if args.command != "status" or not args.stored_only:
            store.configure(config)
        if args.command == "accounts":
            return {"accounts": store.accounts(args.owner)}
        if args.command == "account":
            actor = "operator:" + str(os.geteuid())
            if args.action == "confirm-kind":
                account = store.confirm_account_kind(args.owner, args.account_id, args.kind, actor=actor)
            else:
                account = store.set_account_selected(args.owner, args.account_id, args.action == "select", actor=actor)
            return {"account": account}
        return {"status": store.status()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="JSON path (default HESTIA_BUDGET_CONFIG)")
    parser.add_argument("--db", help="SQLite path (default HESTIA_DB)")
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status", help="Show acquisition status without reading credentials")
    status.add_argument("--stored-only", action="store_true", help="Inspect stored status without applying configuration")
    commands.add_parser("once", help="Acquire selected accounts when their local quota permits")
    importing = commands.add_parser("import-snapshot", help="Import a complete local snapshot without bank access")
    importing.add_argument("--file", required=True, help="Private JSON snapshot file")
    accounts = commands.add_parser("accounts", help="List local account metadata for one owner")
    accounts.add_argument("--owner", required=True, help="Existing enabled admin username")
    account = commands.add_parser("account", help="Set local account selection or confirm its kind")
    actions = account.add_subparsers(dest="action", required=True)
    for name in ("select", "deselect", "confirm-kind"):
        action = actions.add_parser(name)
        action.add_argument("account_id", help="Local account ID from accounts")
        action.add_argument("--owner", required=True, help="Existing enabled admin username")
        if name == "confirm-kind":
            action.add_argument("--kind", required=True, choices=sorted(ACCOUNT_KINDS), help="Owner-confirmed account kind")
    args = parser.parse_args(argv)
    try:
        result = execute(args)
    except (BudgetConfigError, BudgetImportError, BudgetBusyError, BudgetStoreError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        print("Bank acquisition failed; previously committed data is retained", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if any(item["state"] in {"failed", "partial"} for item in result.get("results", ())) else 0


if __name__ == "__main__":
    sys.exit(main())
