"""Explicit calendar selection and environment-only credential references."""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """A configuration error whose message never contains user-supplied values."""


@dataclass(frozen=True)
class AccountConfig:
    id: str
    provider: str
    credential_env: dict[str, str]
    url: str = "https://caldav.icloud.com/"


@dataclass(frozen=True)
class CalendarConfig:
    id: str
    account: str
    calendar_id: str


@dataclass(frozen=True)
class GroupConfig:
    id: str
    calendars: tuple[CalendarConfig, ...]


@dataclass(frozen=True)
class SyncConfig:
    poll_seconds: float
    accounts: dict[str, AccountConfig]
    groups: tuple[GroupConfig, ...]

    def store_groups(self) -> list[dict]:
        return [{"id": group.id, "calendars": [
            {"id": item.id, "account": item.account, "calendar_id": item.calendar_id,
             "provider": self.accounts[item.account].provider}
            for item in group.calendars]} for group in self.groups]


_CREDENTIALS = {"caldav": ("username", "password"),
                "google": ("client_id", "client_secret", "refresh_token")}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _object(raw, allowed: set[str], label: str) -> dict:
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise ConfigError(f"Invalid {label} fields; inline credentials are not supported")
    return raw


def _string(raw, label: str, pattern=None) -> str:
    if (not isinstance(raw, str) or not raw or raw != raw.strip()
            or (pattern is not None and not pattern.fullmatch(raw))):
        raise ConfigError(f"Invalid {label}")
    return raw


def _https(raw, label: str) -> str:
    value = _string(raw, label)
    try:
        parts = urlsplit(value)
        valid = (parts.scheme == "https" and bool(parts.hostname) and parts.username is None
                 and parts.password is None and not parts.query and not parts.fragment)
        _ = parts.port
    except ValueError:
        valid = False
    if not valid:
        raise ConfigError(f"Invalid {label}; use an HTTPS URL without embedded credentials")
    return value


def parse_config(raw) -> SyncConfig:
    data = _object(raw, {"version", "poll_seconds", "accounts", "groups"}, "configuration")
    if type(data.get("version")) is not int or data["version"] != 1:
        raise ConfigError("Calendar configuration version must be 1")
    poll = data.get("poll_seconds", 60)
    if (isinstance(poll, bool) or not isinstance(poll, (float, int))
            or not math.isfinite(poll) or not 10 <= poll <= 86400):
        raise ConfigError("poll_seconds must be a finite number between 10 and 86400")
    account_data = data.get("accounts")
    if not isinstance(account_data, dict):
        raise ConfigError("accounts must be an object")
    accounts = {}
    for account_id, item in account_data.items():
        _string(account_id, "account identifier", _ID)
        item = _object(item, {"provider", "url", "username_env", "password_env", "client_id_env",
                              "client_secret_env", "refresh_token_env"}, "account")
        provider = item.get("provider")
        if not isinstance(provider, str) or provider not in _CREDENTIALS:
            raise ConfigError("Account provider must be google or caldav")
        required = _CREDENTIALS[provider]
        if set(item) - {"provider", "url", *(name + "_env" for name in required)}:
            raise ConfigError("Credential references do not match the account provider")
        if provider == "google" and "url" in item:
            raise ConfigError("Google account does not accept a custom API URL")
        refs = {name: _string(item.get(name + "_env"), "credential environment reference", _ENV)
                for name in required}
        url = _https(item.get("url", "https://caldav.icloud.com/"), "CalDAV server URL")
        accounts[account_id] = AccountConfig(account_id, provider, refs, url)
    group_data = data.get("groups")
    if not isinstance(group_data, list):
        raise ConfigError("groups must be an explicit list (an empty list enables discovery only)")
    groups, group_ids, endpoint_ids, calendars = [], set(), set(), set()
    for item in group_data:
        item = _object(item, {"id", "calendars"}, "group")
        group_id = _string(item.get("id"), "group identifier", _ID)
        if group_id in group_ids:
            raise ConfigError("Duplicate group identifier")
        group_ids.add(group_id)
        selected = item.get("calendars")
        if not isinstance(selected, list) or not selected:
            raise ConfigError("Each group must explicitly select at least one external calendar")
        endpoints = []
        for endpoint in selected:
            endpoint = _object(endpoint, {"id", "account", "calendar_id"}, "calendar")
            endpoint_id = _string(endpoint.get("id"), "endpoint identifier", _ID)
            account = _string(endpoint.get("account"), "calendar account", _ID)
            if account not in accounts:
                raise ConfigError("Calendar references an unknown account")
            calendar_id = _string(endpoint.get("calendar_id"), "calendar identifier")
            if accounts[account].provider == "caldav":
                _https(calendar_id, "CalDAV calendar URL")
                if not calendar_id.endswith("/"):
                    raise ConfigError("CalDAV calendar URL must end with a slash")
            if accounts[account].provider == "google" and calendar_id == "primary":
                raise ConfigError("Select the explicit Google calendar ID returned by discovery")
            identity = (accounts[account].provider, calendar_id)
            if endpoint_id in endpoint_ids or identity in calendars:
                raise ConfigError("An endpoint or calendar may belong to only one group")
            endpoint_ids.add(endpoint_id)
            calendars.add(identity)
            endpoints.append(CalendarConfig(endpoint_id, account, calendar_id))
        groups.append(GroupConfig(group_id, tuple(endpoints)))
    return SyncConfig(float(poll), accounts, tuple(groups))


def load_config(path=None, *, environ=None) -> SyncConfig | None:
    """Unset HESTIA_CALENDAR_CONFIG disables synchronization without touching the DB."""
    env = os.environ if environ is None else environ
    target = path if path is not None else env.get("HESTIA_CALENDAR_CONFIG")
    if not target:
        return None
    try:
        with Path(target).open(encoding="utf-8") as source:
            raw = json.load(source)
    except (OSError, ValueError):
        raise ConfigError("Cannot read calendar configuration as JSON") from None
    return parse_config(raw)


def make_provider(account: AccountConfig, calendar_id: str, *, environ=None):
    """Create a provider without logging or persisting resolved credentials."""
    from .providers import CalDAVProvider, GoogleProvider

    env = os.environ if environ is None else environ
    credentials = {}
    for name, reference in account.credential_env.items():
        value = env.get(reference)
        if not value:
            raise ConfigError("A required calendar credential environment variable is missing")
        credentials[name] = value
    if account.provider == "google":
        return GoogleProvider(calendar_id, **credentials)
    return CalDAVProvider(calendar_id, url=account.url, **credentials)


def make_providers(config: SyncConfig, *, environ=None) -> dict:
    return {endpoint.id: make_provider(config.accounts[endpoint.account], endpoint.calendar_id,
                                       environ=environ)
            for group in config.groups for endpoint in group.calendars}
