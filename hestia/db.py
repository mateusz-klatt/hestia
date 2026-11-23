"""SQLite persistence foundation — SQLAlchemy 2.0 models + Alembic-managed schema.

INERT in Phase 1: nothing in the live path imports this module yet. It defines the
on-disk schema (one SQLite DB, default ``/data/hestia.db`` via ``HESTIA_DB``) that
later phases migrate the registry / automations / users / settings / audit onto. The
in-memory model stays the source of truth; this is the durability layer (see the #57
migration plan). Importing JSON state, cutting persistence over, and the cancel-safe
``_db_write_and_settle`` write path all land in later, separately-reviewed phases.

We use SYNC SQLAlchemy (driven from the event loop via ``run_in_executor`` in later
phases), NOT async/aiosqlite — it matches hestia's existing offload pattern. Durability
PRAGMAs (WAL, ``synchronous=FULL``, foreign keys, ``busy_timeout``) are set per
connection. Schema changes go through Alembic migrations (``hestia/migrations``), run
programmatically at startup (``init_db``) — never runtime autogenerate.

This module deliberately does NOT use ``from __future__ import annotations``: the
SQLAlchemy declarative mapper resolves ``Mapped[...]`` annotations at class-creation
time, and real (non-stringised) PEP 604 unions are the most robust on Python 3.14.
"""
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint, URL, Engine, ForeignKey, ForeignKeyConstraint, Index, Text, UniqueConstraint, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

DEFAULT_DB_PATH = "/data/hestia.db"


def db_path() -> Path:
    """The SQLite file path (``HESTIA_DB`` env, else ``/data/hestia.db``)."""
    return Path(os.environ.get("HESTIA_DB", DEFAULT_DB_PATH))


class Base(DeclarativeBase):
    pass


class AppMeta(Base):
    """Key/value bookkeeping: runtime ``mode``, per-store authority markers, schema notes."""

    __tablename__ = "app_meta"
    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[str]


class Node(Base):
    """One device-registry node, stored as the exact entry dict (lossless JSON) so unknown
    / future fields survive verbatim — a normalised table would silently drop them."""

    __tablename__ = "nodes"
    key: Mapped[str] = mapped_column(primary_key=True)
    entry_json: Mapped[str]


class Automation(Base):
    """One automation rule (canonical ``Rule.to_dict`` JSON). ``position`` preserves eval order."""

    __tablename__ = "automations"
    id: Mapped[str] = mapped_column(primary_key=True)
    position: Mapped[int]
    rule_json: Mapped[str]


class User(Base):
    """An app-login account: username → scrypt password hash (see ``hestia.auth``) + an access ``role``
    (admin/operator/viewer; enforced by the web authz middleware, resolved via
    ``store_sql.current_user_role``). The column default is ``viewer`` (least privilege) so a row created
    without an explicit role is never silently an admin; legacy/imported accounts are set to ``admin`` at
    migration/cutover (they predate roles, so the operator keeps full access). ``server_default`` mirrors
    migration ``0002`` exactly — the drift guard (``test_models_match_migration_no_drift``) enforces it.

    ``disabled`` (#PR-D) temporarily revokes access without deleting the account: a disabled user is
    filtered out of ``current_users`` (can't log in) AND ``current_user_role`` returns None for them (any
    live cookie dies at once). Default ``false`` (existing accounts stay enabled); mirrors migration 0003."""

    __tablename__ = "users"
    username: Mapped[str] = mapped_column(primary_key=True)
    password_hash: Mapped[str]
    role: Mapped[str] = mapped_column(default="viewer", server_default=text("'viewer'"))
    disabled: Mapped[bool] = mapped_column(default=False, server_default=text("0"))


class UserSetting(Base):
    """Per-user UI preferences (#55): locale / temperature scale / theme."""

    __tablename__ = "user_settings"
    username: Mapped[str] = mapped_column(ForeignKey("users.username"), primary_key=True)
    locale: Mapped[str | None]
    temp_scale: Mapped[str | None]
    theme: Mapped[str | None]


class Audit(Base):
    """Append-only event log (#56): who/what did which action. Pruned by row/age cap later."""

    __tablename__ = "audit"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts: Mapped[float]
    actor: Mapped[str | None]
    action: Mapped[str | None]
    target: Mapped[str | None]
    detail: Mapped[str | None]
    result: Mapped[str | None]


class CalendarGroup(Base):
    """Only explicitly configured calendars participate in a group."""

    __tablename__ = "calendar_groups"
    id: Mapped[str] = mapped_column(primary_key=True)
    enabled: Mapped[bool]


class CalendarEndpoint(Base):
    __tablename__ = "calendar_endpoints"
    id: Mapped[str] = mapped_column(primary_key=True)
    group_id: Mapped[str] = mapped_column(ForeignKey("calendar_groups.id"))
    provider: Mapped[str]
    account: Mapped[str]
    calendar_id: Mapped[str]
    enabled: Mapped[bool]
    cursor: Mapped[str | None]
    last_success: Mapped[float | None]
    last_error: Mapped[str | None]


class CalendarEvent(Base):
    """A canonical series, including exceptions; tombstones are never pruned."""

    __tablename__ = "calendar_events"
    __table_args__ = (UniqueConstraint("group_id", "uid"),
                      Index("uq_calendar_events_create_key", "group_id", "create_key", unique=True))
    id: Mapped[str] = mapped_column(primary_key=True)
    group_id: Mapped[str] = mapped_column(ForeignKey("calendar_groups.id"))
    uid: Mapped[str]
    payload_json: Mapped[str]
    fingerprint: Mapped[str]
    deleted: Mapped[bool]
    deleted_at: Mapped[float | None]
    revision: Mapped[int]
    modified_at: Mapped[float]
    source: Mapped[str]
    create_key: Mapped[str | None]
    create_fingerprint: Mapped[str | None]


class CalendarReplica(Base):
    """Reserved before HTTP, so a lost response cannot allocate a second external ID."""

    __tablename__ = "calendar_replicas"
    __table_args__ = (UniqueConstraint("endpoint_id", "remote_id"),)
    endpoint_id: Mapped[str] = mapped_column(ForeignKey("calendar_endpoints.id"), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("calendar_events.id"), primary_key=True, index=True)
    remote_id: Mapped[str]
    etag: Mapped[str | None]
    observed_hash: Mapped[str | None]
    applied_revision: Mapped[int]
    seen: Mapped[bool]
    deleted: Mapped[bool]
    outbound_hash: Mapped[str | None]
    outbound_revision: Mapped[int | None]
    uncertain: Mapped[bool]


class CalendarOutbox(Base):
    """Coalescing, durable delivery queue. A row survives every failed HTTP operation."""

    __tablename__ = "calendar_outbox"
    endpoint_id: Mapped[str] = mapped_column(ForeignKey("calendar_endpoints.id"), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("calendar_events.id"), primary_key=True, index=True)
    revision: Mapped[int]
    attempts: Mapped[int]
    next_attempt: Mapped[float]
    last_error: Mapped[str | None]


class BudgetConnection(Base):
    """Opt-in bank acquisition state; session IDs and keys stay outside SQLite."""

    __tablename__ = "budget_connections"
    id: Mapped[str] = mapped_column(primary_key=True)
    owner: Mapped[str] = mapped_column(ForeignKey("users.username"))
    application_id: Mapped[str]
    bank_name: Mapped[str]
    country: Mapped[str]
    enabled: Mapped[bool]
    paused: Mapped[bool] = mapped_column(default=False, server_default=text("0"))
    control_version: Mapped[int] = mapped_column(default=1, server_default=text("1"))
    background_enabled: Mapped[bool] = mapped_column(default=False, server_default=text("0"))
    credential_mode: Mapped[str] = mapped_column(default="external", server_default=text("'external'"))
    consent_generation: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    active_consent_id: Mapped[str | None]
    attention: Mapped[str | None]
    valid_until: Mapped[float | None]
    last_attempt: Mapped[float | None]
    last_success: Mapped[float | None]
    next_attempt: Mapped[float]
    failures: Mapped[int]
    last_error: Mapped[str | None]
    # Diagnostic for the last round-level HTTP failure: a status and a fixed Enable Banking
    # identifier, never response text. Cleared whenever last_error stops describing that failure.
    last_http_status: Mapped[int | None]
    last_provider_error: Mapped[str | None]


class BudgetConsentFlow(Base):
    """One browser-bound authorization; codes and session IDs never enter SQLite."""

    __tablename__ = "budget_consent_flows"
    __table_args__ = (Index("ix_budget_consent_flows_owner_created", "owner", "created_at"),)
    id: Mapped[str] = mapped_column(primary_key=True)
    owner: Mapped[str] = mapped_column(ForeignKey("users.username"))
    connection_id: Mapped[str] = mapped_column(ForeignKey("budget_connections.id"))
    application_id: Mapped[str]
    bank_name: Mapped[str]
    country: Mapped[str]
    intent: Mapped[str]
    expected_connection_version: Mapped[int]
    expected_generation: Mapped[int]
    state_hash: Mapped[str] = mapped_column(unique=True)
    nonce_hash: Mapped[str]
    auth_fingerprint: Mapped[str]
    binding_revision: Mapped[int]
    flow_revision: Mapped[int]
    created_at: Mapped[float]
    expires_at: Mapped[float]
    status: Mapped[str]
    error: Mapped[str | None]
    callback_at: Mapped[float | None]
    exchange_at: Mapped[float | None]
    exchange_binding_revision: Mapped[int | None]
    exchange_flow_revision: Mapped[int | None]
    code_ref: Mapped[str | None]
    code_expires_at: Mapped[float | None]
    candidate_consent_id: Mapped[str | None]


class BudgetConsent(Base):
    """Owned managed session metadata and an opaque private-file reference."""

    __tablename__ = "budget_consents"
    id: Mapped[str] = mapped_column(primary_key=True)
    owner: Mapped[str] = mapped_column(ForeignKey("users.username"))
    connection_id: Mapped[str] = mapped_column(ForeignKey("budget_connections.id"))
    application_id: Mapped[str]
    bank_name: Mapped[str]
    country: Mapped[str]
    provenance: Mapped[str]
    status: Mapped[str]
    capsule_ref: Mapped[str]
    valid_until: Mapped[float | None]
    generation: Mapped[int]
    metadata_json: Mapped[str]
    created_at: Mapped[float]


class BudgetConsentAudit(Base):
    """Private local transitions, never a shared audit or provider payload."""

    __tablename__ = "budget_consent_audit"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    owner: Mapped[str] = mapped_column(ForeignKey("users.username"))
    connection_id: Mapped[str] = mapped_column(ForeignKey("budget_connections.id"))
    flow_id: Mapped[str] = mapped_column(ForeignKey("budget_consent_flows.id"))
    actor: Mapped[str]
    origin: Mapped[str]
    action: Mapped[str]
    old_value_json: Mapped[str]
    new_value_json: Mapped[str]
    changed_at: Mapped[float]


class BudgetAccount(Base):
    """Stable owner-scoped identity, independent of session account UIDs."""

    __tablename__ = "budget_accounts"
    __table_args__ = (UniqueConstraint("owner", "identification_hash"),)
    id: Mapped[str] = mapped_column(primary_key=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey("budget_connections.id"))
    owner: Mapped[str] = mapped_column(ForeignKey("users.username"))
    identification_hash: Mapped[str]
    enabled: Mapped[bool]
    control_version: Mapped[int] = mapped_column(default=1, server_default=text("1"))
    configured: Mapped[bool] = mapped_column(default=False, server_default=text("0"))
    kind: Mapped[str] = mapped_column(default="unknown", server_default=text("'unknown'"))
    kind_confirmed_at: Mapped[float | None]
    kind_confirmed_by: Mapped[str | None]
    kind_confirmation_origin: Mapped[str | None]
    balances_json: Mapped[str]
    observed_at: Mapped[float | None]
    last_attempt: Mapped[float | None]
    last_error: Mapped[str | None]


class BudgetAccountDisplay(Base):
    """Optional portable account labels, independent of controls and API freshness."""

    __tablename__ = "budget_account_display"
    account_id: Mapped[str] = mapped_column(ForeignKey("budget_accounts.id"), primary_key=True)
    product: Mapped[str | None]
    currency: Mapped[str | None]
    masked_suffix: Mapped[str | None]
    source: Mapped[str] = mapped_column(default="offline_import", server_default=text("'offline_import'"))
    observed_at: Mapped[float]
    imported_at: Mapped[float]
    digest: Mapped[str]


class BudgetComponent(Base):
    """Independent complete API observations and crash-safe in-flight markers."""

    __tablename__ = "budget_components"
    account_id: Mapped[str] = mapped_column(ForeignKey("budget_accounts.id"), primary_key=True)
    operation: Mapped[str] = mapped_column(primary_key=True)
    last_attempt: Mapped[float | None]
    last_success: Mapped[float | None]
    last_error: Mapped[str | None]
    failures: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    next_attempt: Mapped[float] = mapped_column(default=0, server_default=text("0"))
    attempt_id: Mapped[str | None]
    lease_until: Mapped[float | None]
    retry_at: Mapped[float | None]
    suspended: Mapped[bool] = mapped_column(default=False, server_default=text("0"))
    backoff_until: Mapped[float] = mapped_column(default=0, server_default=text("0"))
    # Diagnostic for last_error when it came from an HTTP response; see BudgetConnection.
    last_http_status: Mapped[int | None]
    last_provider_error: Mapped[str | None]


class BudgetAcquisition(Base):
    """Immutable quota debits; every page of one acquisition shares a debit."""

    __tablename__ = "budget_acquisitions"
    __table_args__ = (UniqueConstraint("account_id", "operation", "attempt_id"),
                      Index("ix_budget_acquisition_window", "account_id", "operation", "started_at"))
    id: Mapped[str] = mapped_column(primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("budget_accounts.id"))
    operation: Mapped[str]
    attempt_id: Mapped[str]
    started_at: Mapped[float]
    origin: Mapped[str]


class BudgetRequestLimit(Base):
    """Shared persistent cooldowns and application-wide transport pacing."""

    __tablename__ = "budget_request_limits"
    scope: Mapped[str] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(primary_key=True)
    next_attempt: Mapped[float] = mapped_column(default=0, server_default=text("0"))
    next_request_at: Mapped[float] = mapped_column(default=0, server_default=text("0"))
    last_request_at: Mapped[float | None]
    retry_at: Mapped[float | None]
    suspended: Mapped[bool] = mapped_column(default=False, server_default=text("0"))


class BudgetLimitAudit(Base):
    """Operator review of suspended limits, without financial or secret payloads."""

    __tablename__ = "budget_limit_audit"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    scope: Mapped[str]
    target_key: Mapped[str]
    owner: Mapped[str] = mapped_column(ForeignKey("users.username"))
    actor: Mapped[str]
    origin: Mapped[str]
    field: Mapped[str]
    old_value_json: Mapped[str]
    new_value_json: Mapped[str]
    changed_at: Mapped[float]


class BudgetAccountAudit(Base):
    """Append-only account control history, without financial payloads."""

    __tablename__ = "budget_account_audit"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("budget_accounts.id"))
    owner: Mapped[str] = mapped_column(ForeignKey("users.username"))
    actor: Mapped[str]
    origin: Mapped[str]
    field: Mapped[str]
    old_value_json: Mapped[str]
    new_value_json: Mapped[str]
    changed_at: Mapped[float]


class BudgetJob(Base):
    """Durable local acquisition intent; debits in budget_acquisitions stay authoritative."""

    __tablename__ = "budget_jobs"
    __table_args__ = (Index("uq_budget_jobs_active", "connection_id", unique=True,
                            sqlite_where=text("status IN ('queued', 'running')")),
                      Index("ix_budget_jobs_owner_created", "owner", "created_at"))
    id: Mapped[str] = mapped_column(primary_key=True)
    owner: Mapped[str] = mapped_column(ForeignKey("users.username"))
    connection_id: Mapped[str] = mapped_column(ForeignKey("budget_connections.id"))
    source: Mapped[str]
    account_id: Mapped[str | None] = mapped_column(ForeignKey("budget_accounts.id"))
    operation: Mapped[str | None]
    expected_version: Mapped[int]
    status: Mapped[str]
    error: Mapped[str | None]
    next_attempt: Mapped[float | None]
    created_at: Mapped[float]
    started_at: Mapped[float | None]
    finished_at: Mapped[float | None]


class BudgetJobRequest(Base):
    """Owner-scoped idempotency keys; several keys may coalesce into one job."""

    __tablename__ = "budget_job_requests"
    owner: Mapped[str] = mapped_column(ForeignKey("users.username"), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(primary_key=True)
    digest: Mapped[str]
    job_id: Mapped[str] = mapped_column(ForeignKey("budget_jobs.id", ondelete="CASCADE"))
    created_at: Mapped[float]


class BudgetConnectionAudit(Base):
    """Private connection pause controls, separate from shared application audit."""

    __tablename__ = "budget_connection_audit"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey("budget_connections.id"))
    owner: Mapped[str] = mapped_column(ForeignKey("users.username"))
    actor: Mapped[str]
    origin: Mapped[str]
    field: Mapped[str]
    old_value_json: Mapped[str]
    new_value_json: Mapped[str]
    changed_at: Mapped[float]


class BudgetTransaction(Base):
    """Canonical entries have a provider-guaranteed account-scoped reference."""

    __tablename__ = "budget_transactions"
    account_id: Mapped[str] = mapped_column(ForeignKey("budget_accounts.id"), primary_key=True)
    entry_reference: Mapped[str] = mapped_column(primary_key=True)
    payload_json: Mapped[str]
    observed_at: Mapped[float]
    uncertain: Mapped[bool]
    source: Mapped[str] = mapped_column(default="api", server_default=text("'api'"))
    api_observed_at: Mapped[float | None]


class BudgetAccountIdentity(Base):
    __tablename__ = "budget_account_identity"
    account_id: Mapped[str] = mapped_column(ForeignKey("budget_accounts.id"), primary_key=True)
    owner: Mapped[str] = mapped_column(ForeignKey("users.username"))
    identities_json: Mapped[str]
    source: Mapped[str]
    observed_at: Mapped[float]
    version: Mapped[int]


class BudgetTransactionMetadata(Base):
    __tablename__ = "budget_transaction_metadata"
    __table_args__ = (ForeignKeyConstraint(["account_id", "entry_reference"],
                                           ["budget_transactions.account_id", "budget_transactions.entry_reference"]),)
    account_id: Mapped[str] = mapped_column(primary_key=True)
    entry_reference: Mapped[str] = mapped_column(primary_key=True)
    owner: Mapped[str] = mapped_column(ForeignKey("users.username"))
    ledger_fingerprint: Mapped[str]
    payload_json: Mapped[str]
    provenance_json: Mapped[str] = mapped_column(Text, default="{}", server_default=text("'{}'"))
    source: Mapped[str]
    observed_at: Mapped[float]
    version: Mapped[int]


class BudgetCategory(Base):
    """Owner category. ``parent_id`` is an optional same-table parent (depth is enforced in the store)."""

    __tablename__ = "budget_categories"
    __table_args__ = (
        UniqueConstraint("owner", "name_key"),
        CheckConstraint("parent_id IS NULL OR parent_id != id", name="ck_budget_categories_parent_not_self"),
        Index("ix_budget_categories_parent_id", "parent_id"),
    )
    id: Mapped[str] = mapped_column(primary_key=True)
    owner: Mapped[str] = mapped_column(ForeignKey("users.username"))
    name: Mapped[str]
    name_key: Mapped[str]
    archived: Mapped[bool]
    version: Mapped[int]
    created_at: Mapped[float]
    updated_at: Mapped[float]
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("budget_categories.id", name="fk_budget_categories_parent_id"))


class BudgetCategoryLimit(Base):
    """Owner-configured monthly spending limit for one category and currency."""

    __tablename__ = "budget_category_limits"
    __table_args__ = (
        UniqueConstraint("owner", "category_id", "currency"),
        CheckConstraint("version >= 1", name="ck_budget_category_limits_version"),
        CheckConstraint("warning_percent >= 1 AND warning_percent <= 99",
                        name="ck_budget_category_limits_warning_percent"),
        CheckConstraint("enabled IN (0, 1)", name="ck_budget_category_limits_enabled"),
        CheckConstraint("length(monthly_limit) >= 1 AND length(monthly_limit) <= 80",
                        name="ck_budget_category_limits_monthly_limit_len"),
    )
    id: Mapped[str] = mapped_column(primary_key=True)
    owner: Mapped[str] = mapped_column(ForeignKey("users.username"))
    category_id: Mapped[str] = mapped_column(ForeignKey("budget_categories.id"))
    currency: Mapped[str]
    monthly_limit: Mapped[str]
    warning_percent: Mapped[int]
    enabled: Mapped[bool]
    version: Mapped[int]
    created_at: Mapped[float]
    updated_at: Mapped[float]


class BudgetMortgageSchedule(Base):
    """Owner-private mortgage reminder schedule; stores destination_kind only (no PII targets)."""

    __tablename__ = "budget_mortgage_schedules"
    __table_args__ = (
        UniqueConstraint("id", "owner", name="uq_budget_mortgage_schedules_id_owner"),
        Index("ix_budget_mortgage_schedules_owner", "owner"),
        CheckConstraint("length(label) >= 1 AND length(label) <= 256",
                        name="ck_budget_mortgage_schedules_label_len"),
        CheckConstraint("length(amount) >= 1 AND length(amount) <= 80",
                        name="ck_budget_mortgage_schedules_amount_len"),
        CheckConstraint("length(currency) = 3 AND currency GLOB '[A-Z][A-Z][A-Z]'",
                        name="ck_budget_mortgage_schedules_currency"),
        CheckConstraint("due_day >= 1 AND due_day <= 31",
                        name="ck_budget_mortgage_schedules_due_day"),
        CheckConstraint(
            "length(start_date) = 10 AND start_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'",
            name="ck_budget_mortgage_schedules_start_date",
        ),
        CheckConstraint(
            "end_date IS NULL OR (length(end_date) = 10 "
            "AND end_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' "
            "AND end_date >= start_date)",
            name="ck_budget_mortgage_schedules_end_date",
        ),
        CheckConstraint("remind_days_before >= 0 AND remind_days_before <= 31",
                        name="ck_budget_mortgage_schedules_remind_days"),
        CheckConstraint("destination_kind IN ('owner_inbox', 'owner_push', 'owner_email')",
                        name="ck_budget_mortgage_schedules_destination_kind"),
        CheckConstraint("enabled IN (0, 1)", name="ck_budget_mortgage_schedules_enabled"),
        CheckConstraint("version >= 1", name="ck_budget_mortgage_schedules_version"),
    )
    id: Mapped[str] = mapped_column(primary_key=True)
    owner: Mapped[str] = mapped_column(ForeignKey("users.username"))
    label: Mapped[str]
    amount: Mapped[str]
    currency: Mapped[str]
    due_day: Mapped[int]
    start_date: Mapped[str]
    end_date: Mapped[str | None]
    remind_days_before: Mapped[int]
    destination_kind: Mapped[str]
    enabled: Mapped[bool]
    version: Mapped[int]
    created_at: Mapped[float]
    updated_at: Mapped[float]


class BudgetMortgagePayment(Base):
    """Explicit owner-recorded YYYY-MM payment marks bound to an owned mortgage schedule."""

    __tablename__ = "budget_mortgage_payments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["schedule_id", "owner"],
            ["budget_mortgage_schedules.id", "budget_mortgage_schedules.owner"],
            name="fk_budget_mortgage_payments_schedule_owner",
        ),
        Index("ix_budget_mortgage_payments_owner", "owner"),
        CheckConstraint(
            "length(month) = 7 AND month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]' "
            "AND substr(month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_budget_mortgage_payments_month",
        ),
    )
    schedule_id: Mapped[str] = mapped_column(primary_key=True)
    month: Mapped[str] = mapped_column(primary_key=True)
    owner: Mapped[str] = mapped_column(ForeignKey("users.username"))
    marked_at: Mapped[float]


class BudgetOwnerInboxState(Base):
    """Monotonic per-owner inbox read-marker revision for anti-ABA CAS (outside _source_digest)."""

    __tablename__ = "budget_owner_inbox_state"
    __table_args__ = (
        CheckConstraint("reads_revision >= 0", name="ck_budget_owner_inbox_state_reads_revision"),
        CheckConstraint("updated_at >= 0", name="ck_budget_owner_inbox_state_updated_at"),
    )
    owner: Mapped[str] = mapped_column(ForeignKey("users.username"), primary_key=True)
    reads_revision: Mapped[int]
    updated_at: Mapped[float]


class BudgetOwnerInboxRead(Base):
    """Durable per-owner read markers for live inbox alerts; never pruned automatically in v1."""

    __tablename__ = "budget_owner_inbox_reads"
    __table_args__ = (
        Index("ix_budget_owner_inbox_reads_owner", "owner"),
        CheckConstraint(
            "kind IN ('consent_renewal', 'mortgage_due', 'spending_limit')",
            name="ck_budget_owner_inbox_reads_kind",
        ),
        CheckConstraint(
            "length(subject_id) = 64 AND subject_id NOT GLOB '*[^0-9a-f]*'",
            name="ck_budget_owner_inbox_reads_subject_id",
        ),
        CheckConstraint(
            "length(milestone) >= 1 AND length(milestone) <= 128",
            name="ck_budget_owner_inbox_reads_milestone",
        ),
        CheckConstraint("read_at >= 0", name="ck_budget_owner_inbox_reads_read_at"),
    )
    owner: Mapped[str] = mapped_column(ForeignKey("users.username"), primary_key=True)
    kind: Mapped[str] = mapped_column(primary_key=True)
    subject_id: Mapped[str] = mapped_column(primary_key=True)
    milestone: Mapped[str] = mapped_column(primary_key=True)
    read_at: Mapped[float]


class BudgetAnnotation(Base):
    __tablename__ = "budget_annotations"
    __table_args__ = (ForeignKeyConstraint(["account_id", "entry_reference"],
                                           ["budget_transactions.account_id", "budget_transactions.entry_reference"]),)
    account_id: Mapped[str] = mapped_column(primary_key=True)
    entry_reference: Mapped[str] = mapped_column(primary_key=True)
    owner: Mapped[str] = mapped_column(ForeignKey("users.username"))
    mode: Mapped[str]
    category_id: Mapped[str | None] = mapped_column(ForeignKey("budget_categories.id"))
    kind: Mapped[str]
    ledger_fingerprint: Mapped[str]
    version: Mapped[int]
    updated_at: Mapped[float]


class BudgetRule(Base):
    __tablename__ = "budget_rules"
    id: Mapped[str] = mapped_column(primary_key=True)
    owner: Mapped[str] = mapped_column(ForeignKey("users.username"))
    field: Mapped[str]
    operator: Mapped[str]
    value: Mapped[str]
    category_id: Mapped[str | None] = mapped_column(ForeignKey("budget_categories.id"))
    kind: Mapped[str]
    priority: Mapped[int]
    enabled: Mapped[bool]
    version: Mapped[int]
    created_at: Mapped[float]
    updated_at: Mapped[float]


class BudgetRulePreview(Base):
    __tablename__ = "budget_rule_previews"
    id: Mapped[str] = mapped_column(primary_key=True)
    owner: Mapped[str] = mapped_column(ForeignKey("users.username"))
    candidate_json: Mapped[str]
    snapshot_digest: Mapped[str]
    created_at: Mapped[float]
    expires_at: Mapped[float]
    saved_rule_id: Mapped[str | None] = mapped_column(ForeignKey("budget_rules.id"))
    saved_version: Mapped[int | None]


class BudgetTransferDecision(Base):
    __tablename__ = "budget_transfer_decisions"
    __table_args__ = (UniqueConstraint("owner", "debit_account_id", "debit_ref", "credit_account_id", "credit_ref"),
                      ForeignKeyConstraint(["debit_account_id", "debit_ref"],
                                           ["budget_transactions.account_id", "budget_transactions.entry_reference"]),
                      ForeignKeyConstraint(["credit_account_id", "credit_ref"],
                                           ["budget_transactions.account_id", "budget_transactions.entry_reference"]))
    id: Mapped[str] = mapped_column(primary_key=True)
    owner: Mapped[str] = mapped_column(ForeignKey("users.username"))
    debit_account_id: Mapped[str]
    debit_ref: Mapped[str]
    credit_account_id: Mapped[str]
    credit_ref: Mapped[str]
    source: Mapped[str]
    status: Mapped[str]
    debit_fingerprint: Mapped[str]
    credit_fingerprint: Mapped[str]
    rule_version: Mapped[int]
    version: Mapped[int]
    created_at: Mapped[float]
    updated_at: Mapped[float]


class BudgetTransferLeg(Base):
    __tablename__ = "budget_transfer_legs"
    __table_args__ = (ForeignKeyConstraint(["account_id", "entry_reference"],
                                           ["budget_transactions.account_id", "budget_transactions.entry_reference"]),)
    account_id: Mapped[str] = mapped_column(primary_key=True)
    entry_reference: Mapped[str] = mapped_column(primary_key=True)
    decision_id: Mapped[str] = mapped_column(ForeignKey("budget_transfer_decisions.id"))


class BudgetAnalyticsAudit(Base):
    __tablename__ = "budget_analytics_audit"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    owner: Mapped[str] = mapped_column(ForeignKey("users.username"))
    subject_type: Mapped[str]
    subject_id: Mapped[str]
    action: Mapped[str]
    before_json: Mapped[str]
    after_json: Mapped[str]
    changed_at: Mapped[float]


class BudgetUnresolved(Base):
    """Last complete ambiguous observations; ordinal preserves real multiplicity."""

    __tablename__ = "budget_unresolved"
    account_id: Mapped[str] = mapped_column(ForeignKey("budget_accounts.id"), primary_key=True)
    ordinal: Mapped[int] = mapped_column(primary_key=True)
    payload_json: Mapped[str]
    observed_at: Mapped[float]


class BudgetImportBatch(Base):
    """One normalized offline observation; no file paths or credential material."""

    __tablename__ = "budget_import_batches"
    id: Mapped[str] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(unique=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("budget_accounts.id"))
    fetched_at: Mapped[float]
    imported_at: Mapped[float]
    balances_json: Mapped[str | None]
    report_json: Mapped[str]


class BudgetImportUnresolved(Base):
    """Offline uncertainty never replaces the latest API unresolved snapshot."""

    __tablename__ = "budget_import_unresolved"
    batch_id: Mapped[str] = mapped_column(ForeignKey("budget_import_batches.id"), primary_key=True)
    ordinal: Mapped[int] = mapped_column(primary_key=True)
    reason: Mapped[str]
    payload_json: Mapped[str]


metadata = Base.metadata

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _apply_pragmas(dbapi_conn) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=FULL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


def make_engine(path) -> Engine:
    """A SQLite engine for ``path`` with hestia's durability PRAGMAs applied per connection.
    The parent dir is created so a first run on an empty volume succeeds."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: writes run in run_in_executor worker threads (the cancel-safe write
    # path), so the connection must be usable off the creating thread. Explicit, not relying on the
    # dialect default. Single-writer serialisation is handled by the caller's save_lock + WAL.
    engine = create_engine(URL.create("sqlite", database=str(p)),
                           connect_args={"check_same_thread": False})
    event.listen(engine, "connect", lambda dbapi_conn, _record: _apply_pragmas(dbapi_conn))
    return engine


def _alembic_config(connection) -> Config:
    """An Alembic config wired to ship our migration scripts + the live connection (no .ini file)."""
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.attributes["connection"] = connection
    return cfg


# One engine + session factory per resolved DB path, created (and Alembic-upgraded) ONCE for the
# process lifetime. The hot store_sql paths (the ~30 s autosave, every audit row, each /api/db/stats)
# call init_db on every invocation; without this they each built a fresh engine AND re-ran the Alembic
# upgrade (logging "Context impl SQLiteImpl…" every time + churning a new connection pool). Keyed by the
# resolved path string so tests on distinct temp DBs stay isolated; reset_engine_cache() disposes them.
_ENGINE_CACHE: "dict[str, tuple[Engine, sessionmaker[Session]]]" = {}
_ENGINE_CACHE_LOCK = RLock()


def init_db(path=None) -> tuple[Engine, sessionmaker[Session]]:
    """Engine + session factory for the DB at ``path`` (default ``HESTIA_DB``), upgraded to the latest
    schema via Alembic. **Cached per resolved path**: the engine is built and the migration run ONCE per
    DB file for the process lifetime; later calls return the same pair (call ``reset_engine_cache`` to
    rebuild). Migrations run programmatically — never runtime autogenerate. The upgrade uses one
    dedicated connection: foreign keys off, a real ``BEGIN``, then ``PRAGMA foreign_key_check`` before
    commit. Every other connection still opens with ``PRAGMA foreign_keys=ON``."""
    with _ENGINE_CACHE_LOCK:
        return _init_db_locked(path)


def _set_sqlite_foreign_keys(connection, enabled: bool) -> None:
    """Set ``PRAGMA foreign_keys`` and require the new value to read back.

    SQLite ignores this pragma while a transaction is open and leaves the previous
    value in place. Callers must already be in autocommit, with no transaction open.
    """
    statement = "PRAGMA foreign_keys=ON" if enabled else "PRAGMA foreign_keys=OFF"
    cursor = connection.connection.dbapi_connection.cursor()
    try:
        cursor.execute(statement)
        cursor.execute("PRAGMA foreign_keys")
        observed = cursor.fetchone()[0]
    finally:
        cursor.close()
    if int(observed) != int(enabled):
        raise RuntimeError(
            f"{statement} did not apply (read back {observed!r}). "
            "SQLite ignores this pragma inside a transaction."
        )


def _run_sqlite_migration(engine: Engine, apply) -> None:
    """Run one Alembic operation in the SQLite batch transaction Alembic recommends.

    ``batch_alter_table`` rebuilds a table by copying it and dropping the original.
    Child foreign keys reject that DROP while ``PRAGMA foreign_keys`` is ON, and the
    pragma cannot be changed inside a transaction. sqlite3's legacy transaction mode
    also autocommits DDL unless a real ``BEGIN`` is already open, which is how a
    failed rebuild leaves ``_alembic_tmp_*`` behind.

    On this connection only: turn foreign keys off in autocommit and read the pragma
    back, ``BEGIN``, run ``apply``, then ``PRAGMA foreign_key_check``. Any violation
    rolls the whole migration back (no partial DDL). Commit, turn foreign keys back
    on, and invalidate the connection so the pool never reuses autocommit or
    foreign_keys=OFF. Other connections keep the usual pragmas.
    """
    connection = engine.connect()
    try:
        raw = connection.connection.dbapi_connection
        # sqlite3 must not emit its own BEGIN; SQLAlchemy's begin() does not either.
        # An explicit BEGIN then keeps DDL inside the transaction we commit or roll back.
        raw.isolation_level = None
        _set_sqlite_foreign_keys(connection, False)
        transaction = connection.begin()
        try:
            connection.exec_driver_sql("BEGIN")
            apply(connection)
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
            if violations:
                detail = ", ".join(repr(tuple(row)) for row in violations)
                raise RuntimeError(f"PRAGMA foreign_key_check failed after migration: {detail}")
            transaction.commit()
        except Exception:
            transaction.rollback()
            raise
        _set_sqlite_foreign_keys(connection, True)
    finally:
        connection.invalidate()
        connection.close()


def _init_db_locked(path) -> tuple[Engine, sessionmaker[Session]]:
    key = str(db_path() if path is None else path)
    cached = _ENGINE_CACHE.get(key)
    if cached is not None:
        return cached
    engine = make_engine(key)
    try:
        _run_sqlite_migration(
            engine, lambda connection: command.upgrade(_alembic_config(connection), "head")
        )
    except Exception:
        engine.dispose()
        raise
    result = (engine, sessionmaker(bind=engine))
    _ENGINE_CACHE[key] = result
    return result


def reset_engine_cache() -> None:
    """Dispose every cached engine and clear the cache. For test isolation (a fresh temp DB per test)
    and the rare case of re-initialising after the on-disk file is replaced underneath us."""
    with _ENGINE_CACHE_LOCK:
        for engine, _ in _ENGINE_CACHE.values():
            engine.dispose()
        _ENGINE_CACHE.clear()


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """A transactional session: commit on success, roll back on error, always close."""
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
