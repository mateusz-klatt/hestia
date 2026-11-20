"""Local operator recovery of suspension flags without bypassing retry floors."""
from __future__ import annotations

from sqlalchemy import select, text

from ..db import BudgetAccount as Account, BudgetComponent as Component, BudgetConnection as Connection
from ..db import BudgetLimitAudit as LimitAudit, BudgetRequestLimit as RequestLimit
from .scheduler import provider_key
from .store import BudgetStoreError, OPERATIONS


LIMIT_SCOPES = frozenset({"component", "connection", "application", "provider"})
SHARED_SCOPES = ("connection", "application", "provider")


class BudgetLimits:
    def __init__(self, store, config):
        self.store, self.config = store, config

    def _connection(self, session, owner, connection_id):
        item = next((item for item in self.config.connections if item.id == connection_id and item.owner == owner), None)
        row = session.get(Connection, connection_id) if isinstance(connection_id, str) else None
        if (item is None or row is None or (row.owner, row.application_id, row.bank_name, row.country)
                != (owner, self.config.application_id, item.bank_name, item.country)):
            raise BudgetStoreError("Unknown bank connection")
        return row

    @staticmethod
    def _shared_key(connection, scope):
        return {"connection": connection.id, "application": connection.application_id,
                "provider": provider_key(connection.bank_name, connection.country)}[scope]

    @staticmethod
    def _component(session, connection, account_id, operation):
        if not isinstance(account_id, str) or not isinstance(operation, str) or operation not in OPERATIONS:
            raise BudgetStoreError("Component scope requires a local account and operation")
        account = session.get(Account, account_id)
        if account is None or account.owner != connection.owner or account.connection_id != connection.id:
            raise BudgetStoreError("Unknown bank account")
        return session.get(Component, (account_id, operation))

    def _target(self, session, connection, scope, account_id, operation):
        if not isinstance(scope, str) or scope not in LIMIT_SCOPES:
            raise BudgetStoreError("Unknown bank limit scope")
        if scope == "component":
            target = self._component(session, connection, account_id, operation)
            return target, account_id + ":" + operation, {"account": account_id, "operation": operation}
        if account_id is not None or operation is not None:
            raise BudgetStoreError("Account and operation are valid only for component scope")
        key = self._shared_key(connection, scope)
        return session.get(RequestLimit, (scope, key)), key, {}

    @staticmethod
    def _entry(connection, scope, target, details):
        entry = {"connection": connection.id, "scope": scope, **details,
                 "suspended": bool(getattr(target, "suspended", False)),
                 "retry_at": getattr(target, "retry_at", None),
                 "next_attempt": getattr(target, "next_attempt", 0),
                 "backoff_until": getattr(target, "backoff_until", 0),
                 "lease_until": getattr(target, "lease_until", None),
                 "last_attempt": getattr(target, "last_attempt", None),
                 "last_success": getattr(target, "last_success", None),
                 "last_request_at": getattr(target, "last_request_at", None),
                 "next_request_at": getattr(target, "next_request_at", 0)}
        if scope == "connection":
            entry["next_attempt"] = max(entry["next_attempt"], connection.next_attempt)
        return entry

    def list(self, owner):
        """Show only local identities and sanitized state for this owner's config."""
        with self.store.sessions() as session:
            session.execute(text("BEGIN"))
            self.store._owner(session, owner)
            result = []
            for item in self.config.connections:
                if item.owner != owner:
                    continue
                connection = self._connection(session, owner, item.id)
                for scope in SHARED_SCOPES:
                    target, _, details = self._target(session, connection, scope, None, None)
                    result.append(self._entry(connection, scope, target, details))
                accounts = session.scalars(select(Account).where(
                    Account.connection_id == connection.id, Account.owner == owner).order_by(Account.id))
                for account in accounts:
                    for operation in sorted(OPERATIONS):
                        target, _, details = self._target(session, connection, "component", account.id, operation)
                        result.append(self._entry(connection, "component", target, details))
            return result

    def _audit(self, session, scope, key, owner, actor, origin):
        session.add(LimitAudit(scope=scope, target_key=key, owner=owner, actor=actor, origin=origin,
                               field="suspended", old_value_json="true", new_value_json="false",
                               changed_at=self.store.clock()))

    def resume(self, owner, connection_id, scope, *, account_id=None, operation=None, actor, origin="operator"):
        """Clear suspension only; durable retry, quota and lease state is retained."""
        self.store._actor(actor, origin)
        with self.store.transaction() as session:
            self.store._owner(session, owner)
            connection = self._connection(session, owner, connection_id)
            target, key, details = self._target(session, connection, scope, account_id, operation)
            changed = target is not None and target.suspended
            if changed:
                target.suspended = False
                self._audit(session, scope, key, owner, actor, origin)
            return {**self._entry(connection, scope, target, details), "changed": changed}
