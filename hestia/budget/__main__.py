"""Opt-in bank ingestion with no financial payloads in CLI output."""
from __future__ import annotations

import argparse
import json
import sys

from .config import BudgetConfigError, load_config
from .runtime import BudgetBusyError, budget_lock, run_once
from .store import BudgetStore, BudgetStoreError


def execute(args):
    config = load_config(args.config)
    if config is None:
        raise BudgetConfigError("Set HESTIA_BUDGET_CONFIG or pass --config")
    if args.command == "once":
        return run_once(config, path=args.db)
    from ..db import db_path, init_db

    target = db_path() if args.db is None else args.db
    with budget_lock(target):
        _, sessions = init_db(target)
        store = BudgetStore(sessions)
        store.configure(config)
        return {"status": store.status()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="JSON path (default HESTIA_BUDGET_CONFIG)")
    parser.add_argument("--db", help="SQLite path (default HESTIA_DB)")
    parser.add_argument("command", choices=("status", "once"))
    args = parser.parse_args(argv)
    try:
        result = execute(args)
    except (BudgetConfigError, BudgetBusyError, BudgetStoreError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        print("Bank acquisition failed; previously committed data is retained", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if any(item["state"] in {"failed", "partial"} for item in result.get("results", ())) else 0


if __name__ == "__main__":
    sys.exit(main())
