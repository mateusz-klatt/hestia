"""Configuration, feature flag, and private secret-file loading for appointments monitoring.

Default OFF via ``HESTIA_APPOINTMENTS_ENABLED``. Mirrors Hestia's operator secret-file
boundaries (regular files, mode 0400/0600, runtime UID, no symlinks, strict JSON).
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
import hmac
import json
import math
import os
from pathlib import Path
import random
import re
import stat
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

FLAG = "HESTIA_APPOINTMENTS_ENABLED"
CONFIG_FILE = "HESTIA_APPOINTMENTS_CONFIG"
CMP_BASE_URL = "https://ic-api.cmp.med.pl/api/v1"
WARSAW_TZ = ZoneInfo("Europe/Warsaw")
SUPPORTED_PROVIDERS = frozenset({"cmp"})
SUPPORTED_AUTH_MODES = frozenset({"none", "bearer"})

_MAX_CONFIG_BYTES = 16 * 1024
_MAX_TOKEN_BYTES = 16 * 1024
_MIN_HMAC_BYTES = 32
_MAX_WATCHES = 20
_MAX_WINDOW_DAYS = 180
_MAX_IMPROVEMENT_MINUTES = 7 * 24 * 60
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z", re.ASCII)
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,127}\Z", re.ASCII)
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z", re.ASCII)
_TIME_RE = re.compile(r"[0-9]{2}:[0-9]{2}\Z", re.ASCII)

_TOP_REQUIRED = frozenset({"owner", "watches"})
_TOP_OPTIONAL = frozenset({"cmp_base_url", "auth_mode", "auth_token_env", "hmac_key_env", "poll_policy"})
_POLICY_FIELDS = frozenset({
    "min_interval_seconds",
    "max_interval_seconds",
    "max_requests_per_run",
    "max_retries",
    "base_backoff_seconds",
    "max_backoff_seconds",
    "min_request_spacing_seconds",
})
_WATCH_REQUIRED = frozenset({"watch_id", "provider", "date_from", "date_to"})
_WATCH_OPTIONAL = frozenset({
    "specialty_id",
    "doctor_ids",
    "location_ids",
    "time_from",
    "time_to",
    "service_id",
    "booked_slot",
    "only_better_than_booked",
    "label",
})
_BOOKED_REQUIRED = frozenset({"start"})
_BOOKED_OPTIONAL = frozenset({
    "doctor_id",
    "location_id",
    "min_improvement_minutes",
    "require_same_doctor",
    "require_same_location",
})


class AppointmentsConfigError(ValueError):
    """Fixed safe diagnostic: never echoes file paths, config values, or secrets."""


def is_enabled(*, environ: Mapping[str, str] | None = None) -> bool:
    """Return True only when ``HESTIA_APPOINTMENTS_ENABLED`` is explicitly ``"1"``."""
    env = os.environ if environ is None else environ
    return env.get(FLAG) == "1"


def _reject_constant(_value: str) -> None:
    raise ValueError("Non-finite JSON constant")


def _unique_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def parse_strict_json(raw: bytes) -> object:
    """Decode UTF-8 JSON rejecting duplicate keys and NaN/Infinity constants."""
    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_unique_object_pairs,
        parse_constant=_reject_constant,
    )


def parse_warsaw_datetime(value: object, *, require_offset: bool = False) -> datetime:
    """Parse an ISO-8601 datetime into ``Europe/Warsaw``.

    Rejects nonexistent spring-forward times, ambiguous fall-back DST fold times,
    and naive strings when ``require_offset`` is True.
    """
    if not isinstance(value, str) or ("T" not in value and " " not in value):
        raise ValueError("Expected ISO-8601 datetime string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        if require_offset:
            raise ValueError("Explicit UTC offset required")
        fold0 = parsed.replace(tzinfo=WARSAW_TZ, fold=0)
        fold1 = parsed.replace(tzinfo=WARSAW_TZ, fold=1)
        utc0 = fold0.astimezone(timezone.utc)
        utc1 = fold1.astimezone(timezone.utc)
        if utc0.astimezone(WARSAW_TZ).replace(tzinfo=None) != parsed:
            raise ValueError("Nonexistent local time in Europe/Warsaw")
        if utc0 != utc1:
            raise ValueError("Ambiguous local time in Europe/Warsaw")
        return utc0.astimezone(WARSAW_TZ)
    return parsed.astimezone(timezone.utc).astimezone(WARSAW_TZ)


def _check_int(value: object, minimum: int, maximum: int, label: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise AppointmentsConfigError(f"Invalid {label}")
    return value


def _check_number(value: object, minimum: float, maximum: float, label: str) -> float:
    if type(value) is int:
        if not minimum <= value <= maximum:
            raise AppointmentsConfigError(f"Invalid {label}")
        return float(value)
    if type(value) is float:
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise AppointmentsConfigError(f"Invalid {label}")
        return value
    raise AppointmentsConfigError(f"Invalid {label}")


def _check_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise AppointmentsConfigError(f"Invalid {label}")
    return value


def _check_text(value: object, label: str, *, limit: int = 128) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
        raise AppointmentsConfigError(f"Invalid {label}")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise AppointmentsConfigError(f"Invalid {label}") from None
    return value


def _check_id(value: object, label: str) -> str:
    text = _check_text(value, label, limit=64)
    if not _ID.fullmatch(text):
        raise AppointmentsConfigError(f"Invalid {label}")
    return text


def _check_id_tuple(values: object, label: str, *, max_items: int = 50) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > max_items:
        raise AppointmentsConfigError(f"Invalid {label}")
    seen: set[str] = set()
    cleaned: list[str] = []
    for item in values:
        ident = _check_id(item, label)
        if ident in seen:
            raise AppointmentsConfigError(f"Duplicate {label}")
        seen.add(ident)
        cleaned.append(ident)
    return tuple(cleaned)


def _check_token(value: object) -> str:
    if (not isinstance(value, str) or not 0 < len(value) <= _MAX_TOKEN_BYTES
            or not value.isascii() or any(not 33 <= ord(ch) <= 126 for ch in value)):
        raise AppointmentsConfigError("Invalid auth token")
    return value


def _check_https_url(value: object) -> str:
    text = _check_text(value, "cmp_base_url", limit=256).rstrip("/")
    parsed = urlsplit(text)
    try:
        port = parsed.port
    except ValueError:
        raise AppointmentsConfigError("Invalid cmp_base_url") from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or port == 0):
        raise AppointmentsConfigError("Invalid cmp_base_url")
    return text


def _check_abs_path(value: object, label: str) -> str:
    text = _check_text(value, label, limit=4096)
    if not Path(text).is_absolute():
        raise AppointmentsConfigError(f"Invalid {label}")
    return text


def _read_secret(path: str, *, max_bytes: int = _MAX_CONFIG_BYTES) -> bytes:
    """Safely read a regular, owner-only (0400/0600), non-symlink file with bounded size."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)
                    or info.st_size > max_bytes):
                raise ValueError
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, max_bytes + 1 - total)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError
            return b"".join(chunks)
        finally:
            os.close(fd)
    except (OSError, ValueError):
        raise AppointmentsConfigError("Cannot safely read appointments secret file") from None


@dataclass(frozen=True)
class PollPolicy:
    """Conservative, ToS-friendly polling and backoff ceilings."""

    min_interval_seconds: int = 600
    max_interval_seconds: int = 900
    max_requests_per_run: int = 6
    max_retries: int = 3
    base_backoff_seconds: float = 2.0
    max_backoff_seconds: float = 60.0
    min_request_spacing_seconds: float = 1.0

    def __post_init__(self) -> None:
        min_iv = _check_int(self.min_interval_seconds, 300, 86400, "poll_policy")
        max_iv = _check_int(self.max_interval_seconds, min_iv, 86400, "poll_policy")
        _check_int(self.max_requests_per_run, 1, 20, "poll_policy")
        _check_int(self.max_retries, 0, 5, "poll_policy")
        base_b = _check_number(self.base_backoff_seconds, 1.0, 60.0, "poll_policy")
        max_b = _check_number(self.max_backoff_seconds, base_b, 600.0, "poll_policy")
        spacing = _check_number(self.min_request_spacing_seconds, 0.0, 60.0, "poll_policy")
        object.__setattr__(self, "max_interval_seconds", max_iv)
        object.__setattr__(self, "base_backoff_seconds", base_b)
        object.__setattr__(self, "max_backoff_seconds", max_b)
        object.__setattr__(self, "min_request_spacing_seconds", spacing)

    def next_poll_delay(self, *, jitter: Callable[[], float] = random.random) -> float:
        """Return a jittered delay within ``[min_interval_seconds, max_interval_seconds]``."""
        factor = max(0.0, min(1.0, float(jitter())))
        span = self.max_interval_seconds - self.min_interval_seconds
        return float(self.min_interval_seconds) + span * factor


@dataclass(frozen=True)
class BookedAppointment:
    """Reference for the owner's currently booked appointment ("better than booked" rule)."""

    start: datetime
    doctor_id: str | None = None
    location_id: str | None = None
    min_improvement_minutes: int = 0
    require_same_doctor: bool = False
    require_same_location: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.start, datetime) or self.start.tzinfo is None:
            raise AppointmentsConfigError("Invalid booked_slot start")
        object.__setattr__(self, "start", self.start.astimezone(WARSAW_TZ))
        if self.doctor_id is not None:
            _check_id(self.doctor_id, "booked_slot doctor_id")
        if self.location_id is not None:
            _check_id(self.location_id, "booked_slot location_id")
        _check_int(self.min_improvement_minutes, 0, _MAX_IMPROVEMENT_MINUTES, "booked_slot min_improvement_minutes")
        _check_bool(self.require_same_doctor, "booked_slot require_same_doctor")
        _check_bool(self.require_same_location, "booked_slot require_same_location")
        if self.require_same_doctor and self.doctor_id is None:
            raise AppointmentsConfigError("Invalid booked_slot doctor requirement")
        if self.require_same_location and self.location_id is None:
            raise AppointmentsConfigError("Invalid booked_slot location requirement")


@dataclass(frozen=True)
class WatchCriteria:
    """Owner-defined filter and comparison criteria for one appointment watch."""

    watch_id: str
    provider: str
    date_from: date
    date_to: date
    specialty_id: str | None = None
    doctor_ids: tuple[str, ...] = ()
    location_ids: tuple[str, ...] = ()
    time_from: time | None = None
    time_to: time | None = None
    service_id: str | None = None
    booked_slot: BookedAppointment | None = None
    only_better_than_booked: bool = False
    label: str | None = None

    def __post_init__(self) -> None:
        _check_id(self.watch_id, "watch_id")
        if not isinstance(self.provider, str) or self.provider not in SUPPORTED_PROVIDERS:
            raise AppointmentsConfigError("Unsupported provider")
        if type(self.date_from) is not date or type(self.date_to) is not date:
            raise AppointmentsConfigError("Invalid date window")
        if self.date_from > self.date_to or (self.date_to - self.date_from).days > _MAX_WINDOW_DAYS:
            raise AppointmentsConfigError("Invalid date window")
        if self.specialty_id is not None:
            _check_id(self.specialty_id, "specialty_id")
        _check_id_tuple(self.doctor_ids, "doctor_ids")
        _check_id_tuple(self.location_ids, "location_ids")
        if self.specialty_id is None and not self.doctor_ids:
            raise AppointmentsConfigError("Watch requires specialty_id or doctor_ids")
        if self.time_from is not None and (type(self.time_from) is not time or self.time_from.tzinfo is not None):
            raise AppointmentsConfigError("Invalid time window")
        if self.time_to is not None and (type(self.time_to) is not time or self.time_to.tzinfo is not None):
            raise AppointmentsConfigError("Invalid time window")
        if self.time_from is not None and self.time_to is not None and self.time_from > self.time_to:
            raise AppointmentsConfigError("Invalid time window")
        if self.service_id is not None:
            _check_id(self.service_id, "service_id")
        if self.booked_slot is not None and type(self.booked_slot) is not BookedAppointment:
            raise AppointmentsConfigError("Invalid booked_slot")
        _check_bool(self.only_better_than_booked, "only_better_than_booked")
        if self.only_better_than_booked and self.booked_slot is None:
            raise AppointmentsConfigError("only_better_than_booked requires booked_slot")
        if self.label is not None:
            _check_text(self.label, "label", limit=64)


@dataclass(frozen=True, repr=False)
class CmpCredentials:
    """CMP portal credentials; repr disabled to avoid leaking secrets."""

    auth_mode: str = "none"
    bearer_token: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.auth_mode, str) or self.auth_mode not in SUPPORTED_AUTH_MODES:
            raise AppointmentsConfigError("Invalid auth_mode")
        if self.auth_mode == "bearer":
            _check_token(self.bearer_token)
        elif self.bearer_token is not None:
            raise AppointmentsConfigError("Unexpected bearer_token for auth_mode=none")


@dataclass(frozen=True, repr=False)
class AppointmentsSettings:
    """Validated operator settings for the appointments monitor."""

    owner: str
    cmp_base_url: str
    cmp_credentials: CmpCredentials
    poll_policy: PollPolicy
    watches: tuple[WatchCriteria, ...]
    hmac_secret: bytes | None = None


def _env_ref(data: dict[str, object], env: Mapping[str, str], field: str) -> str:
    name = data[field]
    if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):
        raise ValueError
    return env[name]


def _parse_date(value: object) -> date:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise ValueError
    return date.fromisoformat(value)


def _parse_time(value: object) -> time:
    if not isinstance(value, str) or not _TIME_RE.fullmatch(value):
        raise ValueError
    return time.fromisoformat(value)


def _parse_id_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError
    return tuple(value)


def _parse_booked(raw: object) -> BookedAppointment:
    if not isinstance(raw, dict) or not _BOOKED_REQUIRED <= raw.keys() or raw.keys() - (_BOOKED_REQUIRED | _BOOKED_OPTIONAL):
        raise ValueError
    return BookedAppointment(
        start=parse_warsaw_datetime(raw["start"], require_offset=True),
        doctor_id=raw.get("doctor_id"),
        location_id=raw.get("location_id"),
        min_improvement_minutes=raw.get("min_improvement_minutes", 0),
        require_same_doctor=raw.get("require_same_doctor", False),
        require_same_location=raw.get("require_same_location", False),
    )


def _parse_watch(raw: object) -> WatchCriteria:
    if not isinstance(raw, dict) or not _WATCH_REQUIRED <= raw.keys() or raw.keys() - (_WATCH_REQUIRED | _WATCH_OPTIONAL):
        raise ValueError
    booked_raw = raw.get("booked_slot")
    time_from_raw = raw.get("time_from")
    time_to_raw = raw.get("time_to")
    return WatchCriteria(
        watch_id=raw["watch_id"],
        provider=raw["provider"],
        date_from=_parse_date(raw["date_from"]),
        date_to=_parse_date(raw["date_to"]),
        specialty_id=raw.get("specialty_id"),
        doctor_ids=_parse_id_list(raw.get("doctor_ids", [])),
        location_ids=_parse_id_list(raw.get("location_ids", [])),
        time_from=None if time_from_raw is None else _parse_time(time_from_raw),
        time_to=None if time_to_raw is None else _parse_time(time_to_raw),
        service_id=raw.get("service_id"),
        booked_slot=None if booked_raw is None else _parse_booked(booked_raw),
        only_better_than_booked=raw.get("only_better_than_booked", False),
        label=raw.get("label"),
    )


def _parse_policy(raw: object) -> PollPolicy:
    if not isinstance(raw, dict) or raw.keys() - _POLICY_FIELDS:
        raise ValueError
    return PollPolicy(**raw)


def _load_credentials(data: dict[str, object], env: Mapping[str, str]) -> tuple[CmpCredentials, bytes]:
    auth_mode = data.get("auth_mode", "none")
    if auth_mode == "bearer":
        token_path = _check_abs_path(_env_ref(data, env, "auth_token_env"), "auth_token_file")
        token_bytes = _read_secret(token_path, max_bytes=_MAX_TOKEN_BYTES)
        token = _check_token(token_bytes.decode("ascii").strip("\r\n"))
        return CmpCredentials(auth_mode="bearer", bearer_token=token), token_bytes
    if "auth_token_env" in data:
        raise ValueError
    return CmpCredentials(auth_mode=auth_mode), b""


def _load_hmac_secret(
    data: dict[str, object],
    env: Mapping[str, str],
    *,
    session_secret: bytes,
    token_bytes: bytes,
    token_text: str | None,
) -> bytes | None:
    if "hmac_key_env" not in data:
        return None
    hmac_path = _check_abs_path(_env_ref(data, env, "hmac_key_env"), "hmac_key_file")
    hmac_secret = _read_secret(hmac_path, max_bytes=_MAX_TOKEN_BYTES)
    if (len(hmac_secret) < _MIN_HMAC_BYTES
            or (session_secret and hmac.compare_digest(hmac_secret, session_secret))
            or (token_bytes and hmac.compare_digest(hmac_secret, token_bytes))
            or (token_text is not None and hmac.compare_digest(hmac_secret, token_text.encode("ascii")))):
        raise ValueError
    return hmac_secret


def load_settings(
    *,
    environ: Mapping[str, str] | None = None,
    session_secret: bytes = b"",
    require_enabled: bool = True,
) -> AppointmentsSettings | None:
    """Load operator appointments settings; returns None when flag is OFF or config path is unset."""
    env = os.environ if environ is None else environ
    if require_enabled and not is_enabled(environ=env):
        return None
    path = env.get(CONFIG_FILE)
    if not path:
        return None
    try:
        config_path = _check_abs_path(path, "appointments_config")
        data = parse_strict_json(_read_secret(config_path, max_bytes=_MAX_CONFIG_BYTES))
        if (not isinstance(data, dict)
                or not _TOP_REQUIRED <= data.keys()
                or data.keys() - (_TOP_REQUIRED | _TOP_OPTIONAL)):
            raise ValueError
        owner = _check_text(data["owner"], "owner")
        cmp_base_url = _check_https_url(data.get("cmp_base_url", CMP_BASE_URL))
        poll_policy = _parse_policy(data.get("poll_policy", {}))
        raw_watches = data["watches"]
        if not isinstance(raw_watches, list) or not 1 <= len(raw_watches) <= _MAX_WATCHES:
            raise ValueError
        watches: list[WatchCriteria] = []
        watch_ids: set[str] = set()
        for raw_watch in raw_watches:
            watch = _parse_watch(raw_watch)
            if watch.watch_id in watch_ids:
                raise ValueError
            watch_ids.add(watch.watch_id)
            watches.append(watch)
        credentials, token_bytes = _load_credentials(data, env)
        hmac_secret = _load_hmac_secret(
            data,
            env,
            session_secret=session_secret,
            token_bytes=token_bytes,
            token_text=credentials.bearer_token,
        )
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        raise AppointmentsConfigError("Invalid appointments configuration") from None
    return AppointmentsSettings(
        owner=owner,
        cmp_base_url=cmp_base_url,
        cmp_credentials=credentials,
        poll_policy=poll_policy,
        watches=tuple(watches),
        hmac_secret=hmac_secret,
    )
