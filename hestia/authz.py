"""Authorization permission catalog, policy phase guards, and scope evaluators."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable

from sqlalchemy import select

from .db import AppMeta, BudgetHouseholdMember, PermissionGrant, User

POLICY_PHASE_KEY = "authz_policy_phase"
POLICY_PHASE_LEGACY = "legacy"
POLICY_PHASE_COMPAT = "catalog_compat"
POLICY_PHASE_CUSTOM = "catalog_custom"
SUPPORTED_POLICY_PHASES = frozenset(
    {POLICY_PHASE_LEGACY, POLICY_PHASE_COMPAT, POLICY_PHASE_CUSTOM}
)
LEGACY_RECEIPT_PERMISSIONS = frozenset({
    "budget.receipts.add",
    "budget.receipts.view",
    "budget.receipts.edit",
    "budget.receipts.match",
})


class CatalogValidationError(ValueError):
    """Raised when the declared permission catalog violates an invariant."""


class PolicyPhaseUnsupported(RuntimeError):
    """Raised when SQLite holds an authorization policy phase this image cannot serve."""

    def __init__(self, phase: str) -> None:
        self.phase = phase
        super().__init__(f"unsupported authorization policy phase: {phase!r}")


@dataclass(frozen=True)
class PermissionSpec:
    """Declarative definition of one permission in the Hestia catalog."""

    permission_id: str
    domain: str
    storage_scope: str
    delegation: "str | None"
    requires: tuple[str, ...]
    device_eligible: bool
    label: str
    protected: bool = False


_CATALOG_SPECS: tuple[PermissionSpec, ...] = (
    PermissionSpec(
        permission_id="ioc.view",
        domain="ioc",
        storage_scope="installation",
        delegation=None,
        requires=(),
        device_eligible=False,
        label="View IOC devices and state",
    ),
    PermissionSpec(
        permission_id="ioc.control",
        domain="ioc",
        storage_scope="installation",
        delegation=None,
        requires=("ioc.view",),
        device_eligible=False,
        label="Control IOC devices and scenes",
    ),
    PermissionSpec(
        permission_id="ioc.configure",
        domain="ioc",
        storage_scope="installation",
        delegation=None,
        requires=("ioc.view",),
        device_eligible=False,
        label="Configure IOC automations and settings",
    ),
    PermissionSpec(
        permission_id="calendar.view",
        domain="calendar",
        storage_scope="installation",
        delegation=None,
        requires=(),
        device_eligible=False,
        label="View calendars",
    ),
    PermissionSpec(
        permission_id="calendar.edit",
        domain="calendar",
        storage_scope="installation",
        delegation=None,
        requires=("calendar.view",),
        device_eligible=False,
        label="Edit calendar events",
    ),
    PermissionSpec(
        permission_id="system.audit.view",
        domain="system",
        storage_scope="installation",
        delegation=None,
        requires=(),
        device_eligible=False,
        label="View system audit feed",
    ),
    PermissionSpec(
        permission_id="system.manage_users",
        domain="system",
        storage_scope="installation",
        delegation=None,
        requires=(),
        device_eligible=False,
        label="Manage accounts and grants",
        protected=True,
    ),
    PermissionSpec(
        permission_id="budget.view",
        domain="budget",
        storage_scope="owner",
        delegation="self_only",
        requires=(),
        device_eligible=True,
        label="View full budget ledger",
    ),
    PermissionSpec(
        permission_id="budget.edit",
        domain="budget",
        storage_scope="owner",
        delegation="self_only",
        requires=("budget.view",),
        device_eligible=False,
        label="Edit budget rules and categories",
    ),
    PermissionSpec(
        permission_id="budget.receipts.add",
        domain="budget",
        storage_scope="owner",
        delegation="delegable",
        requires=(),
        device_eligible=True,
        label="Add budget receipts",
    ),
    PermissionSpec(
        permission_id="budget.receipts.view",
        domain="budget",
        storage_scope="owner",
        delegation="delegable",
        requires=(),
        device_eligible=True,
        label="View budget receipts",
    ),
    PermissionSpec(
        permission_id="budget.receipts.edit",
        domain="budget",
        storage_scope="owner",
        delegation="delegable",
        requires=("budget.receipts.add", "budget.receipts.view"),
        device_eligible=True,
        label="Edit budget receipts",
    ),
    PermissionSpec(
        permission_id="budget.receipts.match",
        domain="budget",
        storage_scope="owner",
        delegation="self_only",
        requires=("budget.receipts.view",),
        device_eligible=True,
        label="Match receipts to bank transactions",
    ),
    PermissionSpec(
        permission_id="household.view",
        domain="budget",
        storage_scope="owner",
        delegation="delegable",
        requires=(),
        device_eligible=False,
        label="View household budget summary",
    ),
)


def validate_catalog(specs: Iterable[PermissionSpec]) -> "dict[str, PermissionSpec]":
    """Validate uniqueness, scopes, delegation hierarchy, and acyclicity of catalog entries."""
    by_id: dict[str, PermissionSpec] = {}
    for spec in specs:
        if spec.permission_id in by_id:
            raise CatalogValidationError(f"duplicate permission id: {spec.permission_id!r}")
        if spec.storage_scope == "installation":
            if spec.delegation is not None:
                raise CatalogValidationError(
                    f"installation permission {spec.permission_id!r} cannot declare delegation {spec.delegation!r}"
                )
        elif spec.storage_scope == "owner":
            if spec.delegation not in {"self_only", "delegable"}:
                raise CatalogValidationError(
                    f"owner permission {spec.permission_id!r} has invalid delegation {spec.delegation!r}"
                )
        else:
            raise CatalogValidationError(
                f"permission {spec.permission_id!r} has invalid storage scope {spec.storage_scope!r}"
            )
        by_id[spec.permission_id] = spec

    for spec in by_id.values():
        for req_id in spec.requires:
            req = by_id.get(req_id)
            if req is None:
                raise CatalogValidationError(
                    f"permission {spec.permission_id!r} requires unknown permission {req_id!r}"
                )
            if req.domain != spec.domain:
                raise CatalogValidationError(
                    f"cross-domain implication {spec.permission_id!r} -> {req_id!r}"
                )
            if req.storage_scope != spec.storage_scope:
                raise CatalogValidationError(
                    f"cross-storage-scope implication {spec.permission_id!r} -> {req_id!r}"
                )
            if spec.delegation == "delegable" and req.delegation == "self_only":
                raise CatalogValidationError(
                    f"delegable permission {spec.permission_id!r} cannot require self-only {req_id!r}"
                )

    state: dict[str, int] = {}

    def visit(pid: str) -> None:
        node_state = state.get(pid, 0)
        if node_state == 2:
            return
        if node_state == 1:
            raise CatalogValidationError(f"cycle detected involving permission {pid!r}")
        state[pid] = 1
        for req_id in by_id[pid].requires:
            visit(req_id)
        state[pid] = 2

    for pid in by_id:
        visit(pid)

    return by_id


CATALOG = MappingProxyType(validate_catalog(_CATALOG_SPECS))
DEVICE_ELIGIBLE_PERMISSIONS = frozenset(
    spec.permission_id for spec in CATALOG.values() if spec.device_eligible
)


def policy_phase(session) -> str:
    """Read the persisted authorization policy phase, defaulting absent to ``legacy``."""
    row = session.get(AppMeta, POLICY_PHASE_KEY)
    return POLICY_PHASE_LEGACY if row is None else row.value


def require_supported_policy_phase(session) -> str:
    """Return the active authorization policy phase or fail closed with ``PolicyPhaseUnsupported``."""
    phase = policy_phase(session)
    if phase not in SUPPORTED_POLICY_PHASES:
        raise PolicyPhaseUnsupported(phase)
    return phase


def _is_active_owner(user_row: "User | None", phase: str) -> bool:
    return (
        user_row is not None
        and not user_row.disabled
        and bool(user_row.budget_owner)
        and (phase == POLICY_PHASE_CUSTOM or user_row.role == "admin")
    )


def is_active_budget_owner(session, actor: str) -> bool:
    """Return whether ``actor`` is currently an enabled, provisioned self-owner."""
    if not actor:
        return False
    phase = require_supported_policy_phase(session)
    return _is_active_owner(session.get(User, actor), phase)


def has_legacy_receipt_profile(session, actor: str, *, owner: "str | None" = None) -> bool:
    """PR 2's legacy receipt surface is self-only and requires the complete effective profile."""
    target = actor if owner is None else owner
    return (
        actor == target
        and is_active_budget_owner(session, actor)
        and LEGACY_RECEIPT_PERMISSIONS <= effective_permissions(session, actor, owner=target)
    )


def _closure(direct_ids: set[str]) -> frozenset[str]:
    """Expand valid direct permission IDs along catalog ``requires`` edges."""
    effective = set(direct_ids)
    stack = list(direct_ids)
    while stack:
        pid = stack.pop()
        for req_id in CATALOG[pid].requires:
            if req_id not in effective:
                effective.add(req_id)
                stack.append(req_id)
    return frozenset(effective)


def effective_permissions(session, actor: str, *, owner: "str | None" = None) -> frozenset[str]:
    """Return the transitive closure of valid direct grants for ``actor`` in that exact scope."""
    phase = require_supported_policy_phase(session)
    if not actor:
        return frozenset()
    actor_row = session.get(User, actor)
    if actor_row is None or actor_row.disabled:
        return frozenset()

    if owner is None:
        rows = session.execute(
            select(PermissionGrant.permission_id).where(
                PermissionGrant.username == actor,
                PermissionGrant.target_owner.is_(None),
            )
        ).scalars()
        direct = {
            pid for pid in rows
            if (spec := CATALOG.get(pid)) is not None and spec.storage_scope == "installation"
        }
        return _closure(direct)

    owner_row = actor_row if owner == actor else session.get(User, owner)
    if not _is_active_owner(owner_row, phase):
        return frozenset()
    rows = session.execute(
        select(PermissionGrant.permission_id).where(
            PermissionGrant.username == actor,
            PermissionGrant.target_owner == owner,
        )
    ).scalars()
    direct = {
        pid for pid in rows
        if (spec := CATALOG.get(pid)) is not None
        and spec.storage_scope == "owner"
        and (
            actor == owner or spec.delegation == "delegable"
        )
    }
    return _closure(direct)


def authorize(session, actor: str, permission_id: str, *, owner: "str | None" = None) -> bool:
    """Return True if ``actor`` holds ``permission_id`` in the exact requested scope."""
    return permission_id in effective_permissions(session, actor, owner=owner)


def household_available(session, actor: str) -> bool:
    """Report only availability, never the household owner's identifier."""
    phase = require_supported_policy_phase(session)
    actor_row = session.get(User, actor) if actor else None
    if actor_row is None or actor_row.disabled:
        return False
    if phase != POLICY_PHASE_LEGACY:
        targets = session.execute(
            select(PermissionGrant.target_owner).where(
                PermissionGrant.username == actor,
                PermissionGrant.permission_id == "household.view",
                PermissionGrant.target_owner.is_not(None),
            ).distinct()
        ).scalars()
        return any(authorize(session, actor, "household.view", owner=owner) for owner in targets)
    if actor_row.role not in {"viewer", "operator"}:
        return False
    grants = session.execute(
        select(BudgetHouseholdMember).where(BudgetHouseholdMember.member_username == actor)
    ).scalars()
    return any(
        (owner_row := session.get(User, grant.owner)) is not None
        and not owner_row.disabled and owner_row.role == "admin"
        for grant in grants
    )


def project_legacy_role(session, actor: str, *, include_disabled: bool = False) -> "str | None":
    """Expose the old role enum without using that projection for authorization."""
    phase = require_supported_policy_phase(session)
    user_row = session.get(User, actor) if actor else None
    if user_row is None or (user_row.disabled and not include_disabled):
        return None
    if phase != POLICY_PHASE_CUSTOM:
        return user_row.role

    def declared(*, owner: "str | None" = None) -> frozenset[str]:
        query = select(PermissionGrant.permission_id).where(PermissionGrant.username == actor)
        query = query.where(
            PermissionGrant.target_owner.is_(None) if owner is None
            else PermissionGrant.target_owner == owner
        )
        scope = "installation" if owner is None else "owner"
        return _closure({
            pid for pid in session.execute(query).scalars()
            if (spec := CATALOG.get(pid)) is not None and spec.storage_scope == scope
        })

    installation = declared()
    own = declared(owner=actor) if user_row.budget_owner else frozenset()
    if (
        user_row.budget_owner
        and "system.manage_users" in installation
        and "ioc.control" in installation
        and LEGACY_RECEIPT_PERMISSIONS <= own
    ):
        return "admin"
    return "operator" if "ioc.control" in installation else "viewer"


def delegated_receipt_permissions(session, actor: str) -> frozenset[str]:
    """Return sole-target delegated receipt permissions without exposing the owner."""
    target = resolve_budget_target(session, actor)
    if target is None or target == actor:
        return frozenset()
    return effective_permissions(session, actor, owner=target) & LEGACY_RECEIPT_PERMISSIONS


def resolve_budget_target(session, actor: str) -> "str | None":
    """Return ``actor``'s sole active budget target owner, or ``None`` if none or ambiguous."""
    phase = require_supported_policy_phase(session)
    if not actor:
        return None
    actor_row = session.get(User, actor)
    if actor_row is None or actor_row.disabled:
        return None

    targets: set[str] = set()
    if _is_active_owner(actor_row, phase):
        targets.add(actor)

    candidate_owners = session.execute(
        select(PermissionGrant.target_owner)
        .where(
            PermissionGrant.username == actor,
            PermissionGrant.target_owner.is_not(None),
        )
        .distinct()
    ).scalars()
    for candidate_owner in candidate_owners:
        if candidate_owner not in targets and effective_permissions(session, actor, owner=candidate_owner):
            targets.add(candidate_owner)

    return next(iter(targets)) if len(targets) == 1 else None
