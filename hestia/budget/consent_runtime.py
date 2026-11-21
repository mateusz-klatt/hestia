"""Read-only managed-session overlay, independent of consent HTTP feature flags."""
from __future__ import annotations

from dataclasses import replace
import math
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import quote

from .config import BudgetConfigError
from .consent_files import ConsentFiles, ConsentFilesError

DEFAULT_CONSENT_DIRECTORY = "/data/budget-consent"


def default_files():
    return ConsentFiles(os.environ.get("HESTIA_BUDGET_CONSENT_DIR") or DEFAULT_CONSENT_DIRECTORY)


def _overlay(connection, item, application_id, row, files, now):
    generation = row["consent_generation"]
    available = (type(generation) is int and generation >= 0
                 and (row["owner"], row["application_id"], row["bank_name"], row["country"])
                 == (item.owner, application_id, item.bank_name, item.country))
    unavailable = replace(item, credential_available=False,
                          consent_generation=generation if type(generation) is int and generation >= 0 else 0)
    if not available:
        return unavailable
    if row["credential_mode"] == "external":
        return replace(item, credential_available=True, consent_generation=generation)
    if row["credential_mode"] != "managed" or not row["active_consent_id"]:
        return unavailable
    try:
        consent = connection.execute("SELECT * FROM budget_consents WHERE id=?", (row["active_consent_id"],)).fetchone()
        if (consent is None or consent["status"] != "ACTIVE" or consent["generation"] != generation
                or (consent["owner"], consent["application_id"], consent["bank_name"], consent["country"], consent["connection_id"])
                != (item.owner, application_id, item.bank_name, item.country, item.id)):
            return unavailable
        validity = consent["valid_until"]
        if type(validity) not in (int, float) or not math.isfinite(validity) or validity <= now:
            return unavailable
        filename = (default_files() if files is None else files).path(consent["capsule_ref"], kind="session")
    except (sqlite3.Error, ConsentFilesError, IndexError):
        return unavailable
    return replace(item, session_file=filename, credential_available=True, consent_generation=generation)


def resolve_config(config, *, path, files=None, clock=time.time):
    """Resolve within the acquisition budget_lock; never migrate SQLite or read secrets.

    Old schemas contain only external credentials. Once a managed mode is present,
    unavailable metadata/capsules fail closed rather than restoring the legacy file.
    """
    try:
        now = clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise ValueError
        target = Path(path).absolute()
        try:
            target.stat()
        except FileNotFoundError:
            return config
        connection = sqlite3.connect("file:" + quote(str(target), safe="/") + "?mode=ro", uri=True)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")
            fields = {row[1] for row in connection.execute("PRAGMA table_info(budget_connections)")}
            if "credential_mode" not in fields:
                return config
            if not {"consent_generation", "active_consent_id"} <= fields:
                raise ValueError
            items = []
            for item in config.connections:
                row = connection.execute("SELECT * FROM budget_connections WHERE id=?", (item.id,)).fetchone()
                items.append(item if row is None else _overlay(connection, item, config.application_id, row, files, now))
            return replace(config, connections=tuple(items))
        finally:
            connection.close()
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError, IndexError):
        raise BudgetConfigError("Cannot resolve budget credential configuration") from None
