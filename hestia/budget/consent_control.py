"""Durable control-plane cooldowns and shared transport slots; no bank I/O.

An explicit connect/renew must work despite acquisition cadence, expired consent,
pause, attention or no selected accounts. It still obeys current owner authority,
immutable connection identity and every applicable shared provider/server limit.
The service sleeps outside the transaction and rechecks its flow before HTTP.
"""
from __future__ import annotations

from .consent_store import BudgetConsentStore
from .scheduler import BudgetDeferredError, BudgetScheduler, _future
from .store import BudgetNotFound, BudgetStoreError


class BudgetConsentControlStore(BudgetConsentStore):
    def control_wait(self, owner, connection, *, application_id, request_interval_seconds=None):
        """Reserve one application transport slot, returning zero or its uncapped delay.

        A positive delay reserves nothing. Suspension raises BudgetDeferredError;
        the caller must not turn it into a short retry. An explicit interval may
        tighten the configured pacing but cannot shorten it. No account debit,
        acquisition attempt, control version or selection changes here.
        """
        interval = connection.policy.request_interval_seconds
        if request_interval_seconds is not None:
            if type(request_interval_seconds) is not int or not 1 <= request_interval_seconds <= 60:
                raise BudgetStoreError("Invalid control request interval")
            interval = max(interval, request_interval_seconds)
        scheduler = BudgetScheduler(self, application_id=application_id)
        with self.transaction() as session:
            row = self._owned_connection(session, owner, connection.id)
            if (not row.enabled or connection.owner != owner
                    or (row.application_id, row.bank_name, row.country) !=
                    (application_id, connection.bank_name, connection.country)):
                raise BudgetNotFound()
            # Sample after BEGIN IMMEDIATE has acquired the writer lock, so a
            # contended worker cannot publish an already-expired pacing slot.
            now = scheduler._now()
            limits = scheduler._shared(session, row)
            if any(limit.suspended for limit in limits if limit is not None):
                raise BudgetDeferredError()
            floors = [max(limit.next_attempt, limit.retry_at or 0) for limit in limits if limit is not None]
            application = limits[0]
            if application is not None:
                floors.append(application.next_request_at)
                if application.last_request_at is not None:
                    floors.append(_future(application.last_request_at, interval))
            ready_at = max(floors, default=0)
            if ready_at > now:
                return ready_at - now
            application = scheduler._limit(session, "application", row.application_id)
            application.last_request_at = now
            application.next_request_at = _future(now, interval)
            return 0.0

    def record_control_rate_limit(self, connection, error, *, application_id):
        """Retain trusted 429 metadata even when its flow/owner was revoked in flight.

        The scheduler verifies the immutable context without granting an account
        acquisition. Old-session expiry is deliberately not a bound for a new
        consent's control requests. Never pass provider text or arbitrary maps.
        """
        BudgetScheduler(self, application_id=application_id).record_rate_limit(
            connection, primary=None, operation=None, error=error)
