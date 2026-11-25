"""Household grants and the member read gate (Budget Month P3).

Decisions for this branch:

- The current schema revision includes the household-grant lineage introduced by 0023.
  Downgrade of 0023 returns to
  0022 and deletes grants only. Receipt rows, category parents and one-off
  labels stay.
- ``household grant`` and ``household revoke`` open the existing SQLite file and
  run in one ``BEGIN IMMEDIATE`` transaction on revision 0023 or a known descendant.
  They do not call ``init_db``, so they cannot upgrade the database, load bank
  config, or take the acquisition lock.
- An identical member and owner keep the current ``grant_id``. A different owner
  is refused until revoke. Revoke requires that member, owner and grant id.
  Regrant allocates a new id. Command output and logs use fixed status codes.
- Discovery does not read ``X-Budget-Grant``. Data routes require exactly one
  header and it must equal the grant id from that snapshot.
- The ``viewer`` route floor admits higher roles. Authorization requires an
  enabled ``viewer`` or ``operator`` and an active admin grant owner, in the
  read snapshot and again in one immediate transaction before responding.
- Role changes between viewer and operator preserve access. A member who is
  disabled or promoted to admin is denied with ``budget_forbidden``. The auth
  middleware returns 401 first when it already sees the disabled account.
- Category labels are ``salary`` or ``child_benefit`` only when the stored name
  is exactly ``Wynagrodzenie``, ``Świadczenia ZUS``, ``salary``, ``child benefit``
  or ``child_benefit``. Every other category label is the 1-based ordinal of
  the category id sort. The source name is never serialized.
- Account, mortgage and recurring ordinals are 1-based over internal ids or
  safe hint keys, not over secret names.
- Uncategorised rows use a null category id. The owner token ``uncategorised``
  is not a household alias.
- ``GET /monthly-budget-settings`` reads the P1 bootstrap config only. P4's
  ``budget_month_settings`` table is not part of this change. A missing owner
  entry is an empty income list and a null pass-through pair.
- Series daily points come from ``evaluate_monthly_budget(include_series=True)``
  for the selected month. An unconfigured dashboard emits zero daily totals so
  the series does not publish spending the dashboard withholds. Month buckets
  cover at most 12 periods, end at the selected month, and stop at 1900-01.
  Historical buckets set ``income_estimate_partial`` false.
- Household cursors use their own AEAD domain and a digest of safe rows. The
  actor, owner, grant, route and filters are additional data, so the token
  length does not depend on the owner's name. They are not the owner snapshot
  digest. Recurring output is capped at the owner hint cap; the truncation
  flag is not in the allowlist.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
from pathlib import Path

from alembic.script import ScriptDirectory
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from .. import authz, store_sql
from ..db import AppMeta, BudgetHouseholdMember, PermissionGrant, User, _alembic_config, make_engine

log = logging.getLogger("hestia.budget.household")

SCHEMA_REVISION = "0023"
_GRANT_BYTES = 32
_USERS_MARKER = "users_authoritative"
_GRANT_ID = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_MEMBER_ROLES = ("viewer", "operator")

# Exact stored names that may be serialized as a fixed code. Anything else is an ordinal.
APPROVED_CATEGORY_LABELS = {
    "Wynagrodzenie": "salary",
    "Świadczenia ZUS": "child_benefit",
    "salary": "salary",
    "child benefit": "child_benefit",
    "child_benefit": "child_benefit",
}


class HouseholdCommandError(Exception):
    """Fixed CLI status. The message is the code and nothing else."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class HouseholdAuthError(Exception):
    """Fixed HTTP denial for a household read. No grant id, name or amount."""

    def __init__(self, status, code):
        self.status = status
        self.code = code
        super().__init__(code)


def category_label(name, ordinal):
    """A fixed code, or the decimal ordinal. Never the stored category name."""
    code = APPROVED_CATEGORY_LABELS.get(name)
    if code is not None:
        return code
    return str(ordinal)


def entity_alias(secret, grant_id, domain, key):
    """Domain-separated alias of one internal key, stable for one grant id."""
    if (not isinstance(secret, bytes) or not secret or not isinstance(grant_id, str) or not grant_id
            or not isinstance(domain, str) or not domain or not isinstance(key, str) or not key):
        raise HouseholdAuthError(500, "budget_internal_error")
    material = (b"hestia/budget/household/alias/v1\0" + grant_id.encode("utf-8") + b"\0"
                + domain.encode("utf-8") + b"\0" + key.encode("utf-8"))
    return hmac.new(secret, material, hashlib.sha256).hexdigest()


def valid_grant_id(value):
    """Exactly 64 lowercase hex characters. Anything else is rejected before a lookup or ``compare_digest``."""
    return isinstance(value, str) and _GRANT_ID.fullmatch(value) is not None


def authorize_household(session, member, *, header, require_header):
    """Role, grant and owner checks. Caller holds the snapshot used for the read.

    Returns ``(member, owner, grant_id)``. ``header`` is the single header value,
    or None when the caller already rejected a missing or duplicate header.
    A data read validates the header before any database lookup.
    """
    if require_header and not valid_grant_id(header):
        raise HouseholdAuthError(403, "budget_forbidden")
    store_sql.auth_session_phase(session)
    phase = authz.require_supported_policy_phase(session)
    if session.get(AppMeta, _USERS_MARKER) is None:
        from .views import BudgetUsersBackendRequired
        raise BudgetUsersBackendRequired()
    grant = session.get(BudgetHouseholdMember, member)
    if grant is None or grant.member_username != member or grant.owner == member:
        raise HouseholdAuthError(403, "budget_forbidden")
    if phase == authz.POLICY_PHASE_LEGACY:
        member_active = _active(session, member, *_MEMBER_ROLES) is not None
        owner_active = _active(session, grant.owner, "admin") is not None
        permitted = member_active and owner_active
    else:
        permitted = (
            _active(session, member, *store_sql.ROLE_RANK) is not None
            and authz.is_active_budget_owner(session, grant.owner)
            and authz.authorize(session, member, "household.view", owner=grant.owner)
        )
    if not permitted:
        raise HouseholdAuthError(403, "budget_forbidden")
    if require_header and not hmac.compare_digest(header, grant.grant_id):
        raise HouseholdAuthError(403, "budget_forbidden")
    return member, grant.owner, grant.grant_id


def require_household_data_grant(session, member, *, header, require_header):
    """Snapshot gate for a household read. Runs before cache, filters and cursors.

    Requires both budget flags, SQLite, an authenticated and enabled ``viewer`` or
    ``operator``, a grant row for that username, and an enabled admin owner.
    Data reads also require ``header`` to be the one ``X-Budget-Grant`` value and
    to equal the grant id. Discovery passes
    ``require_header=False`` and does not consult the header.
    """
    if os.environ.get("HESTIA_BUDGET_HTTP_ENABLED") != "1":
        raise HouseholdAuthError(404, "budget_disabled")
    if os.environ.get("HESTIA_BUDGET_ANALYTICS_ENABLED") != "1":
        raise HouseholdAuthError(404, "budget_analytics_disabled")
    if os.environ.get("HESTIA_PERSIST", "json").lower() != "sqlite":
        from .views import BudgetUsersBackendRequired
        raise BudgetUsersBackendRequired()
    if not isinstance(member, str) or not member:
        raise HouseholdAuthError(401, "budget_not_signed_in")
    return authorize_household(session, member, header=header, require_header=require_header)


def _immediate_engine(path):
    """SQLite engine that takes the write lock for the whole transaction and does not migrate."""
    engine = make_engine(path)
    event.listen(engine, "connect", lambda dbapi_conn, _rec: setattr(dbapi_conn, "isolation_level", None))
    event.listen(engine, "begin", lambda conn: conn.exec_driver_sql("BEGIN IMMEDIATE"))
    return engine


def _require_lineage(session):
    try:
        versions = session.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    except OperationalError:
        raise HouseholdCommandError("household_schema_required") from None
    script = ScriptDirectory.from_config(_alembic_config(None))
    if not any(versions == [revision.revision] for revision in script.walk_revisions(base=SCHEMA_REVISION)):
        raise HouseholdCommandError("household_schema_required")
    found = session.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'budget_household_members'")).first()
    if found is None:
        raise HouseholdCommandError("household_schema_required")
    store_sql.auth_session_phase(session)
    authz.require_supported_policy_phase(session)
    if session.get(AppMeta, _USERS_MARKER) is None:
        raise HouseholdCommandError("household_users_required")


def _active(session, username, *roles, check_budget_owner=False):
    if not isinstance(username, str) or not username:
        return None
    columns = (User.disabled, User.role, User.budget_owner) if check_budget_owner else (User.disabled, User.role)
    row = session.execute(select(*columns).where(User.username == username)).first()
    if row is None or row.disabled or row.role not in roles:
        return None
    return row


def _grant(session, member, owner):
    phase = authz.require_supported_policy_phase(session)
    member_roles = store_sql.ROLE_RANK if phase == authz.POLICY_PHASE_CUSTOM else _MEMBER_ROLES
    if _active(session, member, *member_roles) is None or member == owner:
        raise HouseholdCommandError("household_member_invalid")
    if phase == authz.POLICY_PHASE_CUSTOM:
        owner_valid = authz.is_active_budget_owner(session, owner)
    else:
        owner_row = _active(session, owner, "admin", check_budget_owner=(phase == authz.POLICY_PHASE_COMPAT))
        owner_valid = owner_row is not None and (
            phase != authz.POLICY_PHASE_COMPAT or owner_row.budget_owner
        )
    if not owner_valid:
        raise HouseholdCommandError("household_owner_invalid")
    if phase == authz.POLICY_PHASE_CUSTOM:
        member_row = session.get(User, member)
        if member_row.budget_owner:
            raise HouseholdCommandError("household_member_invalid")
        targets = session.execute(select(PermissionGrant.target_owner).where(
            PermissionGrant.username == member,
            PermissionGrant.target_owner.is_not(None),
            PermissionGrant.target_owner != owner,
        ).distinct()).scalars()
        if any(authz.effective_permissions(session, member, owner=target) for target in targets):
            raise HouseholdCommandError("household_member_invalid")
    existing = session.get(BudgetHouseholdMember, member)
    if existing is not None:
        if existing.owner != owner:
            raise HouseholdCommandError("household_grant_conflict")
        return {"ok": True, "status": "unchanged", "grant_id": existing.grant_id}
    grant_id = secrets.token_hex(_GRANT_BYTES)
    session.add(BudgetHouseholdMember(member_username=member, owner=owner, grant_id=grant_id))
    try:
        session.flush()
    except IntegrityError:
        raise HouseholdCommandError("household_grant_conflict") from None
    if phase == authz.POLICY_PHASE_CUSTOM:
        session.add(PermissionGrant(
            username=member, permission_id="household.view", target_owner=owner,
        ))
    else:
        store_sql.sync_legacy_grants(session)
    return {"ok": True, "status": "granted", "grant_id": grant_id}


def _revoke(session, member, owner, grant_id):
    phase = authz.require_supported_policy_phase(session)
    row = session.get(BudgetHouseholdMember, member) if isinstance(member, str) and member else None
    if (row is None or row.owner != owner or not isinstance(grant_id, str)
            or not hmac.compare_digest(row.grant_id, grant_id)):
        raise HouseholdCommandError("household_grant_mismatch")
    session.delete(row)
    if phase == authz.POLICY_PHASE_CUSTOM:
        permission = session.scalar(select(PermissionGrant).where(
            PermissionGrant.username == member,
            PermissionGrant.permission_id == "household.view",
            PermissionGrant.target_owner == owner,
        ))
        if permission is not None:
            session.delete(permission)
    else:
        store_sql.sync_legacy_grants(session)
    return {"ok": True, "status": "revoked"}


def _run(path, mutation):
    """One immediate transaction on an existing database. No migration and no bank I/O."""
    if path is None:
        path = os.environ.get("HESTIA_DB")
    if not isinstance(path, str) or not path or not Path(path).is_file():
        raise HouseholdCommandError("household_db_required")
    engine = _immediate_engine(path)
    try:
        with Session(engine) as session, session.begin():
            _require_lineage(session)
            return mutation(session)
    finally:
        engine.dispose()


def grant_member(member, owner, *, path=None):
    return _run(path, lambda session: _grant(session, member, owner))


def revoke_grant(member, owner, grant_id, *, path=None):
    return _run(path, lambda session: _revoke(session, member, owner, grant_id))


def run_household_command(args):
    """CLI entry. Prints a fixed code on failure and never loads bank configuration."""
    try:
        if args.action == "grant":
            result = grant_member(args.member, args.owner, path=args.db)
        elif args.action == "revoke":
            result = revoke_grant(args.member, args.owner, args.grant_id, path=args.db)
        else:
            raise HouseholdCommandError("household_command_invalid")
    except HouseholdCommandError as exc:
        log.error("household status=%s", exc.code)
        print(exc.code, file=_stderr())
        return 2
    except (authz.PolicyPhaseUnsupported, store_sql.SessionPhaseUnsupported):
        log.error("household status=authorization_bridge_incompatible")
        print("authorization_bridge_incompatible", file=_stderr())
        return 1
    except Exception as exc:
        log.error("household status=household_internal exception=%s", type(exc).__name__)
        print("household_internal", file=_stderr())
        return 1
    log.info("household status=%s", result["status"])
    print(_dumps(result))
    return 0


def _stderr():
    import sys
    return sys.stderr


def _dumps(result):
    import json
    return json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2)
