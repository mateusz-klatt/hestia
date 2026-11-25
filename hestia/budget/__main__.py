"""Opt-in bank ingestion with no financial payloads in CLI output."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from .. import authz, store_sql
from .config import BudgetConfigError, load_config
from .import_file import BudgetImportError, load_import
from .limits import BudgetLimits, LIMIT_SCOPES
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
        with sessions() as session:
            store_sql.auth_session_phase(session)
            authz.require_supported_policy_phase(session)
        store = BudgetStore(sessions)
        if importing:
            return {"import": store.import_snapshot(config, snapshot), "status": store.status()}
        if args.command != "status" or not args.stored_only:
            store.configure(config)
        if args.command == "accounts":
            return {"accounts": store.accounts(args.owner)}
        if args.command == "limits":
            limits = BudgetLimits(store, config)
            if args.action == "list":
                return {"limits": limits.list(args.owner)}
            return {"limit": limits.resume(args.owner, args.connection, args.scope,
                                            account_id=args.account, operation=args.operation,
                                            actor="operator:" + str(os.geteuid()))}
        if args.command == "account":
            actor = "operator:" + str(os.geteuid())
            if args.action == "confirm-kind":
                account = store.confirm_account_kind(args.owner, args.account_id, args.kind, actor=actor)
            else:
                account = store.set_account_selected(args.owner, args.account_id, args.action == "select", actor=actor)
            return {"account": account}
        return {"status": store.status()}


def _run_gmail(args):
    from .. import store_sql
    from ..db import db_path, init_db
    from . import gmail_config
    from .gmail_client import GmailError
    from .gmail_store import GmailStore
    from .gmail_sync import GmailSync
    from .views import BudgetUsersBackendRequired

    try:
        secret = os.environ.get("HESTIA_SESSION_SECRET", "").encode("utf-8")
        settings = gmail_config.load_settings(session_secret=secret)
        if settings is None:
            raise BudgetConfigError("Set HESTIA_BUDGET_RECEIPTS_GMAIL_CONFIG")
        target = db_path() if args.db is None else args.db
        engine, sessions = init_db(target)
        if not store_sql.is_users_db_authoritative(engine):
            raise BudgetUsersBackendRequired()
        service = GmailSync(settings, GmailStore(sessions, clock=time.time), None, None, target)
        result = service.schedule(settings.owner, force_full_resync=args.force_full_resync,
                                  retry_parse_failed=args.retry_parse_failed)
    except GmailError as exc:
        print(exc.code, file=sys.stderr)
        return 2
    except (BudgetConfigError, BudgetStoreError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        print("Gmail sync scheduling failed", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


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
    limits = commands.add_parser("limits", help="Inspect limits or clear operator suspension without bypassing deadlines")
    limit_actions = limits.add_subparsers(dest="action", required=True)
    listed = limit_actions.add_parser("list", help="Show stored limits for one configured owner")
    listed.add_argument("--owner", required=True)
    resumed = limit_actions.add_parser("resume", help="Clear suspension while retaining retry deadlines and quota")
    resumed.add_argument("--owner", required=True)
    resumed.add_argument("--connection", required=True)
    resumed.add_argument("--scope", required=True, choices=sorted(LIMIT_SCOPES))
    resumed.add_argument("--account", help="Local account ID, required only for component scope")
    resumed.add_argument("--operation", choices=("transactions", "balances"), help="Required only for component scope")
    household = commands.add_parser("household", help="Grant or revoke read-only access to an admin budget")
    household_actions = household.add_subparsers(dest="action", required=True)
    grant = household_actions.add_parser("grant", help="Create or reuse one household grant")
    grant.add_argument("--member", required=True, help="Enabled viewer or operator username")
    grant.add_argument("--owner", required=True, help="Enabled admin username")
    revoke = household_actions.add_parser("revoke", help="Revoke one grant when member, owner and id all match")
    revoke.add_argument("--member", required=True, help="Member username on the grant")
    revoke.add_argument("--owner", required=True, help="Admin username on the grant")
    revoke.add_argument("--grant-id", required=True, help="Current grant id")
    gmail = commands.add_parser("gmail", help="Queue Gmail receipt sync without a browser session")
    gmail_actions = gmail.add_subparsers(dest="action", required=True)
    gmail_sync = gmail_actions.add_parser("sync", help="Schedule one Gmail receipt sync for the background poller")
    gmail_sync.add_argument("--force-full-resync", action="store_true",
                            help="Scan the recent resync window instead of incremental history")
    gmail_sync.add_argument("--retry-parse-failed", action="store_true",
                            help="Delete parse_failed markers and scan the full backfill window")
    args = parser.parse_args(argv)
    if args.command == "household":
        # Before bank config, the acquisition lock, imports and configure(). No init_db.
        from .household import run_household_command
        return run_household_command(args)
    if args.command == "gmail":
        return _run_gmail(args)
    try:
        result = execute(args)
    except (BudgetConfigError, BudgetImportError, BudgetBusyError, BudgetStoreError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (authz.PolicyPhaseUnsupported, store_sql.SessionPhaseUnsupported):
        print("authorization_bridge_incompatible", file=sys.stderr)
        return 1
    except Exception:
        print("Bank acquisition failed; previously committed data is retained", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if any(item["state"] in {"failed", "partial"} for item in result.get("results", ())) else 0


if __name__ == "__main__":
    sys.exit(main())
