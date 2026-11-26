"""Medical appointment slot monitoring (default OFF via HESTIA_APPOINTMENTS_ENABLED).

Slice 1 provides operator configuration, the CMP response parser, the pure slot-diff
engine, and the transport-injected CMP client. Nothing is wired into Hestia's web app,
database, or background scheduler until Slice 2.
"""
from __future__ import annotations

from .config import (
    CMP_BASE_URL,
    CONFIG_FILE,
    FLAG,
    WARSAW_TZ,
    AppointmentsConfigError,
    AppointmentsSettings,
    BookedAppointment,
    CmpCredentials,
    PollPolicy,
    WatchCriteria,
    is_enabled,
    load_settings,
)
from .parser import (
    AppointmentSlot,
    CmpParseError,
    ParseResult,
    parse_cmp_scheduler,
    parse_cmp_scheduler_bytes,
)
from .slots import (
    SlotDiff,
    dedupe_and_sort,
    diff_slots,
    is_better_than_booked,
    matches_criteria,
    slot_digest,
    slot_identity_key,
    slot_sort_key,
)

__all__ = [
    "AppointmentSlot",
    "AppointmentsConfigError",
    "AppointmentsSettings",
    "BookedAppointment",
    "CMP_BASE_URL",
    "CONFIG_FILE",
    "CmpCredentials",
    "CmpParseError",
    "FLAG",
    "ParseResult",
    "PollPolicy",
    "SlotDiff",
    "WARSAW_TZ",
    "WatchCriteria",
    "dedupe_and_sort",
    "diff_slots",
    "is_better_than_booked",
    "is_enabled",
    "load_settings",
    "matches_criteria",
    "parse_cmp_scheduler",
    "parse_cmp_scheduler_bytes",
    "slot_digest",
    "slot_identity_key",
    "slot_sort_key",
]
