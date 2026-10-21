"""Provider-neutral sync records; methods run in Hestia's background executor."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol


@dataclass(frozen=True)
class CalendarInfo:
    id: str
    name: str
    writable: bool
    reason: str | None = None
    timezone: str | None = None


@dataclass(frozen=True)
class RemoteEvent:
    remote_id: str
    uid: str
    payload: dict | None
    etag: str | None
    updated: float
    deleted: bool = False


@dataclass(frozen=True)
class Snapshot:
    events: list[RemoteEvent]
    cursor: str | None
    complete: bool
    full: bool


class ProviderError(Exception):
    """Safe diagnostic: never contains HTTP bodies, URLs, or credentials."""

    def __init__(self, message: str, *, status: int | None = None,
                 retryable: bool = False, conflict: bool = False, code: str | None = None,
                 no_mutation: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.conflict = conflict
        self.code = code
        # True only when this call certainly failed before attempting any mutation.
        self.no_mutation = no_mutation


class CalendarProvider(Protocol):
    discover: Callable[[], list[CalendarInfo]]
    allocate_id: Callable[[str], str]
    pull: Callable[[str | None], Snapshot]
    put: Callable[..., RemoteEvent]
    delete: Callable[..., None]


Provider = CalendarProvider
