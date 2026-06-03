"""Unified entry point: ``python -m hestia`` runs one server in the mode chosen
by ``HESTIA_MODE`` (``proxy`` — relay to the Keemple cloud, default — or
``standalone`` — replace the cloud). Both modes share the same device-facing
session, `State`, and newline-JSON control port; the mode is fixed at startup
(no mid-session switching). The dedicated entries (`python -m hestia.proxy` /
`python -m hestia.server`) still work too.
"""
from __future__ import annotations

import asyncio
import os


def select_main(mode: str):
    """Map a HESTIA_MODE string to the matching server ``main`` coroutine factory."""
    from . import proxy, server
    if mode == "standalone":
        return server.main
    if mode == "proxy":
        return proxy.main
    raise SystemExit(f"unknown HESTIA_MODE={mode!r} (use 'proxy' or 'standalone')")


def resolve_mode(env_mode, registry_path) -> str:
    """The mode to start in: an explicit ``HESTIA_MODE`` env wins (manual override / tests);
    otherwise the PERSISTED registry mode (the Phase-3 graduation target), defaulting to ``proxy``.
    Reading the registry here is what makes a graduate-then-restart come up standalone."""
    if env_mode:
        return env_mode.lower()
    if os.environ.get("HESTIA_PERSIST", "json").lower() == "sqlite":
        from .db import init_db
        from .store_sql import is_db_authoritative, read_mode
        engine, _ = init_db()
        try:
            if is_db_authoritative(engine):      # cut over → the DB holds the persisted mode
                return read_mode(engine)
            # not yet cut over: the JSON registry still owns the mode for this boot
        finally:
            engine.dispose()
    from .registry import Registry
    return Registry.load(registry_path).mode


def main() -> None:  # pragma: no cover
    from .proxy import ProxyConfig
    mode = resolve_mode(os.environ.get("HESTIA_MODE"), ProxyConfig().registry_path)
    try:
        asyncio.run(select_main(mode)())
    except (KeyboardInterrupt, asyncio.CancelledError):   # SIGINT / SIGTERM-driven graceful exit
        pass


if __name__ == "__main__":  # pragma: no cover
    main()
