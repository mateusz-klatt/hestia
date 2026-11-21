"""Durable browser-bound consent claims; no provider calls or private-file reads."""
from __future__ import annotations

import hmac
import json
import math
import re
import uuid

from sqlalchemy import and_, or_, select, text
from sqlalchemy.exc import IntegrityError

from ..db import BudgetAccount as Account, BudgetConnection as Connection
from ..db import BudgetConsent as Consent, BudgetConsentAudit as Audit, BudgetConsentFlow as Flow
from .bank_metadata import _identities_patch, apply_account_identity
from .job_store import BudgetJobStore, BudgetUsersRequired
from .store import BudgetNotFound, BudgetStoreError, _json

_HASH = re.compile(r"[a-f0-9]{64}\Z", re.ASCII)
_ACTIVE = ("created", "started", "staged", "exchanging", "candidate")
_ERRORS = frozenset({"authorization", "network", "rate_limit", "invalid_response", "invalid_session",
                     "expired_session", "session_mismatch", "account_selection", "unavailable",
                     "acquisition_timeout", "deferred", "configuration", "exchange_uncertain",
                     "binding_invalid", "expired", "control_changed", "canceled", "internal_error"})
_IDENTITY_LIMIT = 1000
_IDENTITY_BYTES = 256 * 1024
_EVIDENCE = "account_identity_evidence"
_UIDS = "account_uids"


class BudgetConsentError(BudgetStoreError):
    """Fixed diagnostics, never provider text or browser/private-file values."""

    def __init__(self, code="budget_consent_conflict", status=409):
        self.code, self.status = code, status
        super().__init__(code)


def _hash(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise BudgetConsentError("budget_consent_invalid_request", 400)


def _reference(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise BudgetConsentError("budget_consent_invalid_request", 400) from None


def _account_hashes(values):
    if (not isinstance(values, (list, tuple, set, frozenset))
            or any(not isinstance(v, str) or not v or len(v) > 1024
                   or any(ord(c) < 32 or ord(c) == 127 for c in v) for v in values)
            or len(set(values)) != len(values)):
        raise BudgetConsentError("budget_consent_invalid_request", 400)
    return set(values)


def _evidence_row(uid, row, now):
    try:
        _reference(uid)
        if not isinstance(row, dict) or set(row) != {"identification_hash", "identities", "observed_at"}:
            return None
        _account_hashes([row["identification_hash"]])
        identities = _identities_patch(row["identities"])
        observed = row["observed_at"]
        if (not identities or type(observed) not in (int, float) or not math.isfinite(observed)
                or not 0 <= observed <= now):
            return None
        return {"identification_hash": row["identification_hash"], "identities": list(identities), "observed_at": observed}
    except (BudgetConsentError, OverflowError):
        return None


def _identity_evidence(raw, now):
    """Optional normalized evidence is bounded and never makes a valid consent fail."""
    if not isinstance(raw, dict) or len(raw) > _IDENTITY_LIMIT:
        return {}
    result, size = {}, 0
    for uid, value in raw.items():
        row = _evidence_row(uid, value, now)
        if row is not None:
            size += len(_json([uid, row]))
            if size > _IDENTITY_BYTES:
                return {}
            result[uid] = row
    return result


def _account_uid_map(raw, hashes):
    if not isinstance(raw, dict) or len(raw) > _IDENTITY_LIMIT:
        return {}
    result = {}
    for primary, uid in raw.items():
        try:
            _reference(uid)
            if primary in hashes:
                result[primary] = uid
        except BudgetConsentError:
            continue
    return result


def _candidate_metadata(metadata, valid_until, now):
    if (not isinstance(metadata, dict) or "account_hashes" not in metadata
            or set(metadata) - {"account_hashes", _EVIDENCE, _UIDS}):
        raise BudgetConsentError("budget_consent_invalid_request", 400)
    hashes = _account_hashes(metadata["account_hashes"])
    if valid_until is None and hashes:
        raise BudgetConsentError("budget_consent_invalid_request", 400)
    result = {"account_hashes": sorted(hashes)}
    evidence = _identity_evidence(metadata.get(_EVIDENCE), now)
    mapping = _account_uid_map(metadata.get(_UIDS), hashes)
    if evidence:
        result[_EVIDENCE] = evidence
    if mapping:
        result[_UIDS] = mapping
    return result


def _retain_evidence(metadata, prior):
    for key in (_EVIDENCE, _UIDS):
        if key in prior:
            if key in metadata and metadata[key] != prior[key]:
                raise BudgetConsentError()
            metadata[key] = prior[key]
    return _json(metadata)


def _adopt_identity_evidence(session, candidate, accounts, now):
    metadata = json.loads(candidate.metadata_json)
    evidence = _identity_evidence(metadata.get(_EVIDENCE), now)
    mapping = _account_uid_map(metadata.get(_UIDS), metadata["account_hashes"])
    changed = False
    for account in accounts:
        row = evidence.get(mapping.get(account.identification_hash))
        if row is not None and row["identification_hash"] == account.identification_hash:
            outcome = apply_account_identity(session, account.id, row["identities"], "api", row["observed_at"])
            changed = outcome == "updated" or changed
    if changed:
        from .analytics_reconcile import reconcile_transfers
        reconcile_transfers(session, candidate.owner, now)


class BudgetConsentStore(BudgetJobStore):
    """One callback and one exchange claim; final adoption uses the same write transaction as CAS."""

    @staticmethod
    def _internal(row):
        return {column.name: getattr(row, column.name) for column in row.__table__.columns}

    @staticmethod
    def _safe_flow(flow):
        return {key: getattr(flow, key) for key in ("id", "connection_id", "intent", "status", "error",
                "created_at", "expires_at", "candidate_consent_id")}

    @staticmethod
    def _safe_consent(row):
        return {key: getattr(row, key) for key in ("id", "connection_id", "provenance", "status",
                                                  "valid_until", "generation", "created_at")}

    def _flow(self, session, owner, flow_id):
        self._owner(session, owner)
        flow = session.get(Flow, flow_id)
        if flow is None or flow.owner != owner:
            raise BudgetNotFound()
        return flow

    def _cleanup_flow(self, session, owner, flow_id):
        """Retain/fence a previously claimed result without granting fresh owner authority.

        A revoked owner cannot read, verify or adopt. A known result of their
        already-durable exchange may only remain UNADOPTED, or become uncertain.
        Before any exchange claim the ordinary enabled-admin check still applies.
        """
        if not self.users_authoritative(session):
            raise BudgetUsersRequired()
        flow = session.get(Flow, flow_id)
        if flow is None or flow.owner != owner:
            raise BudgetNotFound()
        if flow.exchange_at is None:
            self._owner(session, owner)
        else:
            connection = session.get(Connection, flow.connection_id)
            if connection is None or self._identity(flow) != self._connection_identity(connection):
                raise BudgetConsentError()
        return flow

    @staticmethod
    def _binding(flow, auth_fingerprint, nonce_hash):
        _hash(auth_fingerprint)
        _hash(nonce_hash)
        if not (hmac.compare_digest(flow.auth_fingerprint, auth_fingerprint)
                and hmac.compare_digest(flow.nonce_hash, nonce_hash)):
            raise BudgetConsentError("budget_consent_binding_invalid", 403)

    def _transition(self, session, flow, status, error=None, *, invalidate=False, action=None):
        old = {"status": flow.status, "binding_revision": flow.binding_revision,
               "flow_revision": flow.flow_revision}
        flow.status, flow.error = status, error
        if invalidate:
            flow.binding_revision += 1
            flow.flow_revision += 1
        session.add(Audit(owner=flow.owner, connection_id=flow.connection_id, flow_id=flow.id,
                          actor=flow.owner, origin="web", action=action or status,
                          old_value_json=_json(old), new_value_json=_json({"status": status,
                          "binding_revision": flow.binding_revision, "flow_revision": flow.flow_revision}),
                          changed_at=self.clock()))

    def _expire(self, session, owner):
        for flow in session.scalars(select(Flow).where(Flow.owner == owner,
                                                       or_(Flow.expires_at <= self.clock(),
                                                           and_(Flow.status == "staged", Flow.code_expires_at <= self.clock())),
                                                       Flow.status.in_(("created", "started", "staged", "candidate")))):
            self._transition(session, flow, "expired", "expired", invalidate=True)

    @staticmethod
    def _identity(row):
        return row.owner, row.connection_id, row.application_id, row.bank_name, row.country

    @staticmethod
    def _connection_identity(connection):
        return connection.owner, connection.id, connection.application_id, connection.bank_name, connection.country

    def create_flow(self, owner, connection_id, *, intent, expected_version, state_hash, nonce_hash,
                    auth_fingerprint):
        for value in (state_hash, nonce_hash, auth_fingerprint):
            _hash(value)
        if intent not in ("connect", "renew"):
            raise BudgetConsentError("budget_consent_invalid_request", 400)
        now = self.clock()
        try:
            with self.transaction() as session:
                connection = self._owned_connection(session, owner, connection_id)
                self._check_version(connection, expected_version, True, self._connection_dict(connection), "web")
                if not connection.enabled:
                    raise BudgetNotFound()
                self._expire(session, owner)
                rows = list(session.scalars(select(Flow).where(Flow.owner == owner)))
                active = [row for row in rows if row.status in _ACTIVE]
                bank_rows = [row for row in rows if (row.bank_name, row.country) ==
                             (connection.bank_name, connection.country)]
                if (any(row.created_at > now - 60 for row in bank_rows)
                        or sum(row.created_at > now - 3600 for row in bank_rows) >= 3):
                    raise BudgetConsentError("budget_consent_rate_limited", 429)
                if len(active) >= 2 or any(row.connection_id == connection.id for row in active):
                    raise BudgetConsentError("budget_consent_in_progress", 409)
                flow = Flow(id=str(uuid.uuid4()), owner=owner, connection_id=connection.id,
                            application_id=connection.application_id, bank_name=connection.bank_name,
                            country=connection.country, intent=intent, expected_connection_version=expected_version,
                            expected_generation=connection.consent_generation, state_hash=state_hash,
                            nonce_hash=nonce_hash, auth_fingerprint=auth_fingerprint, binding_revision=1,
                            flow_revision=1, created_at=now, expires_at=now + 900, status="created")
                session.add(flow)
                session.flush()
                self._transition(session, flow, "created")
                return self._internal(flow)
        except IntegrityError:
            raise BudgetConsentError() from None

    def mark_started(self, owner, flow_id):
        with self.transaction() as session:
            flow = self._flow(session, owner, flow_id)
            if flow.status == "started":
                return self._internal(flow)
            if flow.status != "created" or flow.expires_at <= self.clock():
                raise BudgetConsentError()
            self._transition(session, flow, "started")
            return self._internal(flow)

    def fail_flow(self, owner, flow_id, error):
        with self.transaction() as session:
            flow = self._cleanup_flow(session, owner, flow_id)
            if flow.status not in _ACTIVE:
                return self._safe_flow(flow)
            code = error if error in _ERRORS else "internal_error"
            status = "exchange_uncertain" if flow.status == "exchanging" else "failed"
            self._transition(session, flow, status, code, invalidate=True)
            return self._safe_flow(flow)

    def flow_for_state(self, state_hash):
        _hash(state_hash)
        with self.sessions() as session:
            session.execute(text("BEGIN"))
            flow = session.scalar(select(Flow).where(Flow.state_hash == state_hash))
            if flow is None:
                raise BudgetNotFound()
            self._owner(session, flow.owner)
            return self._internal(flow)

    def consent_selected_hashes(self, owner, connection_id):
        """Selected coverage is independent of pause; renewal never changes the selection."""
        with self.sessions() as session:
            session.execute(text("BEGIN"))
            self._owned_connection(session, owner, connection_id)
            return set(session.scalars(select(Account.identification_hash).where(Account.owner == owner,
                       Account.connection_id == connection_id, Account.enabled, Account.configured)))

    def flow_for_browser(self, owner, flow_id, auth_fingerprint, nonce_hash):
        with self.transaction() as session:
            flow = self._flow(session, owner, flow_id)
            self._binding(flow, auth_fingerprint, nonce_hash)
            self._expire(session, owner)
            return self._internal(flow)

    def candidate_for_flow(self, owner, flow_id):
        with self.sessions() as session:
            session.execute(text("BEGIN"))
            flow = self._flow(session, owner, flow_id)
            row = self._candidate(session, flow)
            return None if row is None else self._internal(row)

    def _candidate(self, session, flow):
        if flow.candidate_consent_id is None:
            return None
        row = session.get(Consent, flow.candidate_consent_id)
        if row is None or self._identity(row) != self._identity(flow):
            raise BudgetConsentError()
        return row

    def callback(self, state_hash, nonce_hash, *, code_ref=None, error=None):
        _hash(state_hash)
        _hash(nonce_hash)
        if (code_ref is None) == (error is None):
            raise BudgetConsentError("budget_consent_invalid_request", 400)
        if code_ref is not None:
            _reference(code_ref)
        with self.transaction() as session:
            flow = session.scalar(select(Flow).where(Flow.state_hash == state_hash))
            if flow is None:
                raise BudgetNotFound()
            self._owner(session, flow.owner)
            if not hmac.compare_digest(flow.nonce_hash, nonce_hash):
                raise BudgetConsentError("budget_consent_binding_invalid", 403)
            self._fence(flow, self._owned_connection(session, flow.owner, flow.connection_id))
            if flow.status not in ("created", "started") or flow.expires_at <= self.clock():
                raise BudgetConsentError()
            flow.callback_at = self.clock()
            if error is not None:
                self._transition(session, flow, "failed", error if error in _ERRORS else "authorization",
                                 invalidate=True)
            else:
                flow.code_ref = code_ref
                flow.code_expires_at = min(self.clock() + 120, flow.expires_at)
                self._transition(session, flow, "staged")
            return self._internal(flow)

    def pending(self, owner, auth_fingerprint, bindings):
        _hash(auth_fingerprint)
        with self.transaction() as session:
            self._owner(session, owner)
            self._expire(session, owner)
            rows = session.scalars(select(Flow).where(Flow.owner == owner, Flow.auth_fingerprint == auth_fingerprint)
                                   .order_by(Flow.created_at, Flow.id))
            return [self._safe_flow(row) for row in rows if row.status in _ACTIVE
                    and row.binding_revision == 1 and row.nonce_hash == bindings.get(row.id)]

    def claim_exchange(self, owner, flow_id, auth_fingerprint, nonce_hash):
        with self.transaction() as session:
            flow = self._flow(session, owner, flow_id)
            self._binding(flow, auth_fingerprint, nonce_hash)
            if (flow.status != "staged" or flow.binding_revision != 1 or flow.expires_at <= self.clock()
                    or flow.code_expires_at <= self.clock()):
                raise BudgetConsentError()
            connection = self._owned_connection(session, owner, flow.connection_id)
            self._fence(flow, connection)
            self._transition(session, flow, "exchanging")
            flow.exchange_at = self.clock()
            flow.exchange_binding_revision = flow.binding_revision
            flow.exchange_flow_revision = flow.flow_revision
            return self._internal(flow)

    @classmethod
    def _fence(cls, flow, connection):
        if (not connection.enabled or cls._identity(flow) != cls._connection_identity(connection)
                or flow.expected_generation != connection.consent_generation
                or flow.expected_connection_version != connection.control_version):
            raise BudgetConsentError("budget_consent_control_changed", 409)

    def record_candidate(self, owner, flow_id, *, capsule_ref, valid_until, metadata):
        _reference(capsule_ref)
        if valid_until is not None and (type(valid_until) not in (int, float)
                or not math.isfinite(valid_until) or valid_until <= self.clock()):
            raise BudgetConsentError("budget_consent_invalid_request", 400)
        metadata = _candidate_metadata(metadata, valid_until, self.clock())
        normalized = _json(metadata)
        with self.transaction() as session:
            flow = (self._cleanup_flow(session, owner, flow_id) if valid_until is None
                    else self._flow(session, owner, flow_id))
            if flow.exchange_at is None:
                raise BudgetConsentError()
            if flow.candidate_consent_id is not None:
                prior = self._candidate(session, flow)
                normalized = _retain_evidence(metadata, json.loads(prior.metadata_json))
                if prior.capsule_ref == capsule_ref and prior.valid_until is None and valid_until is not None:
                    prior.valid_until, prior.metadata_json = valid_until, normalized
                    self._transition(session, flow, flow.status, flow.error, action="candidate_verified")
                    return self._internal(prior)
                if (prior.capsule_ref, prior.valid_until, prior.metadata_json) != (capsule_ref, valid_until, normalized):
                    raise BudgetConsentError()
                return self._internal(prior)
            candidate = Consent(id=str(uuid.uuid4()), owner=owner, connection_id=flow.connection_id,
                                application_id=flow.application_id, bank_name=flow.bank_name, country=flow.country,
                                provenance="hestia_created", status="UNADOPTED", capsule_ref=capsule_ref,
                                valid_until=valid_until, generation=flow.expected_generation + 1,
                                metadata_json=normalized, created_at=self.clock())
            session.add(candidate)
            session.flush()
            flow.candidate_consent_id = candidate.id
            status = "candidate" if flow.status == "exchanging" else flow.status
            self._transition(session, flow, status, flow.error, action="candidate_recorded")
            return self._internal(candidate)

    def adopt(self, owner, flow_id, *, auth_fingerprint, nonce_hash, selected_hashes):
        selected = _account_hashes(selected_hashes)
        with self.transaction() as session:
            flow = self._flow(session, owner, flow_id)
            self._binding(flow, auth_fingerprint, nonce_hash)
            if (flow.status != "candidate" or flow.expires_at <= self.clock()
                    or flow.binding_revision != flow.exchange_binding_revision
                    or flow.flow_revision != flow.exchange_flow_revision):
                raise BudgetConsentError()
            connection = self._owned_connection(session, owner, flow.connection_id)
            self._fence(flow, connection)
            accounts = list(session.scalars(select(Account).where(Account.owner == owner,
                         Account.connection_id == connection.id, Account.enabled, Account.configured)))
            actual = {account.identification_hash for account in accounts}
            candidate = self._candidate(session, flow)
            if (candidate is None or actual != selected
                    or not actual <= set(json.loads(candidate.metadata_json)["account_hashes"])
                    or candidate.valid_until is None or candidate.valid_until <= self.clock()
                    or candidate.status != "UNADOPTED"
                    or self._identity(candidate) != self._connection_identity(connection)):
                raise BudgetConsentError("budget_consent_account_selection", 409)
            if connection.credential_mode not in ("external", "managed"):
                raise BudgetConsentError()
            if connection.active_consent_id is not None:
                prior = session.get(Consent, connection.active_consent_id)
                if (connection.credential_mode != "managed" or prior is None or prior.status != "ACTIVE"
                        or self._identity(prior) != self._identity(candidate)
                        or prior.generation != connection.consent_generation):
                    raise BudgetConsentError()
                prior.status = "RETIRED"
            _adopt_identity_evidence(session, candidate, accounts, self.clock())
            connection.consent_generation += 1
            connection.control_version += 1
            connection.credential_mode = "managed"
            connection.active_consent_id = candidate.id
            connection.valid_until = candidate.valid_until
            candidate.generation = connection.consent_generation
            candidate.status = "ACTIVE"
            self._transition(session, flow, "adopted", invalidate=True)
            return self._internal(candidate)

    def cancel(self, owner, flow_id, auth_fingerprint, nonce_hash):
        with self.transaction() as session:
            flow = self._flow(session, owner, flow_id)
            self._binding(flow, auth_fingerprint, nonce_hash)
            if flow.status in _ACTIVE:
                self._transition(session, flow, "canceled", "canceled", invalidate=True)
            return self._safe_flow(flow)

    def invalidate_bindings(self, auth_fingerprint, bindings):
        _hash(auth_fingerprint)
        with self.transaction() as session:
            if not self.users_authoritative(session):
                raise BudgetUsersRequired()
            count = 0
            for flow_id, nonce in bindings.items():
                flow = session.get(Flow, flow_id)
                if (flow is not None and flow.auth_fingerprint == auth_fingerprint
                        and flow.nonce_hash == nonce and flow.status in _ACTIVE):
                    self._transition(session, flow, "canceled", "binding_invalid", invalidate=True)
                    count += 1
            return count

    def managed(self, owner):
        with self.sessions() as session:
            session.execute(text("BEGIN"))
            self._owner(session, owner)
            return [self._safe_consent(row) for row in session.scalars(select(Consent).where(Consent.owner == owner)
                                                                      .order_by(Consent.created_at, Consent.id))]

    def renewal_reminders(self, owner, *, now=None):
        """Evaluate private consent-renewal reminders for owned connections (read-only, no bank I/O)."""
        from .renewal_reminders import (
            DEFAULT_LEAD_DAYS,
            RULE_VERSION,
            ConsentRenewalTarget,
            RenewalInputError,
            RenewalLimitError,
            evaluate_renewal_reminders,
        )

        evaluated_at = self.clock() if now is None else now
        with self.sessions() as session:
            session.execute(text("BEGIN"))
            self._owner(session, owner)
            rows = list(session.scalars(
                select(Connection).where(Connection.owner == owner).order_by(Connection.id)))
            targets = [
                ConsentRenewalTarget(
                    owner=owner,
                    connection_id=row.id,
                    bank_name=row.bank_name,
                    valid_until=row.valid_until,
                    destination_kind="owner_inbox",
                    destination_target=None,
                    enabled=row.enabled,
                    paused=row.paused,
                    attention=row.attention,
                )
                for row in rows
            ]
        try:
            reminders = evaluate_renewal_reminders(owner, evaluated_at, targets)
        except RenewalLimitError:
            raise BudgetConsentError("budget_consent_limit", 413) from None
        except RenewalInputError:
            raise BudgetConsentError("budget_consent_invalid_request", 400) from None
        return {
            "evaluated_at": evaluated_at,
            "lead_days": list(DEFAULT_LEAD_DAYS),
            "rule_version": RULE_VERSION,
            "reminders": [
                {
                    "connection_id": item.connection_id,
                    "bank_name": item.bank_name,
                    "valid_until": item.valid_until,
                    "seconds_remaining": item.seconds_remaining,
                    "days_remaining": item.days_remaining,
                    "status": item.status,
                    "active_milestone": item.active_milestone,
                    "reminder_due": item.reminder_due,
                    "paused": item.paused,
                    "attention": item.attention,
                    "destination_kind": item.destination_kind,
                    "rule_version": item.rule_version,
                }
                for item in reminders
            ],
        }

    def recover_interrupted(self):
        with self.transaction() as session:
            if not self.users_authoritative(session):
                raise BudgetUsersRequired()
            rows = list(session.scalars(select(Flow).where(Flow.status == "exchanging")))
            for flow in rows:
                self._transition(session, flow, "exchange_uncertain", "exchange_uncertain", invalidate=True)
            return len(rows)
