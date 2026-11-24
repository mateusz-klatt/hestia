"""Local Gmail grant lifecycle and resumable, receipt-scoped import rounds."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import hmac
import json
from pathlib import Path
import random
import re

from .gmail_address import canonical_mailbox
from .gmail_client import GmailCredentials, GmailError, RunLimit
from .gmail_store import DAY, GMAIL_MESSAGE_DOMAIN, MAILBOX_DOMAIN, SCOPE_DOMAIN, _PHASE_RESET, digest
from .rate_limit import parse_retry_after

BATCH_COMMIT_SIZE = 25
MAX_SYNC_MESSAGES_PER_RUN = 100
MANUAL_COOLDOWN = 60
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z", re.ASCII)
_HISTORY_ID = re.compile(r"[0-9]{1,64}\Z", re.ASCII)


@dataclass(frozen=True, repr=False)
class GmailSettings:
    owner: str
    owner_mailbox: str
    credentials: GmailCredentials
    hmac_secret: bytes
    sender_allowlist: tuple[str, ...]
    spouse_forward_senders: tuple[str, ...]
    known_merchant_domains: tuple[str, ...] = ()
    require_known_forwarded_merchant: bool = True
    label_name: str | None = None
    backfill_days: int = 365
    backfill_max_messages: int = 500
    sync_interval_seconds: int = 3600


def _history_id(value):
    if not isinstance(value, str) or not _HISTORY_ID.fullmatch(value):
        raise GmailError("invalid_response")
    return value


def _message_id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise GmailError("invalid_response")
    return value


def _page_token(page, current):
    token = page.get("nextPageToken")
    if token is not None and (not isinstance(token, str) or not 0 < len(token) <= 512
                              or not token.isascii() or not token.isprintable() or token == current):
        raise GmailError("invalid_response")
    return token


def _objects(data, key):
    values = data.get(key, [])
    if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
        raise GmailError("invalid_response")
    return values


def _history_messages(page):
    ids = set()
    for record in _objects(page, "history"):
        for key in ("messagesAdded", "labelsAdded"):
            for entry in _objects(record, key):
                message = entry.get("message")
                if not isinstance(message, dict):
                    raise GmailError("invalid_response")
                ids.add(_message_id(message.get("id")))
    return ids


def _terms(settings):
    senders = set(settings.sender_allowlist + settings.spouse_forward_senders)
    senders.add(settings.owner_mailbox)
    return tuple(f'from:"{sender.removeprefix("@")}"' for sender in sorted(senders))


def _query(settings, terms, cutoff=None):
    parts = []
    if cutoff is not None:
        parts.append(f"after:{int(cutoff)}")
    if settings.label_name is not None:
        label = settings.label_name.replace("\\", "\\\\").replace('"', '\\"')
        parts.append(f'label:"{label}"')
    parts.append("{" + " ".join(terms) + "}")
    return " ".join(parts)


@contextmanager
def _sync_lock(database_path):
    path = Path(str(Path(database_path).resolve()) + ".budget-gmail-sync.lock")
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise GmailError("budget_receipt_gmail_sync_busy", 409) from None
        yield


@contextmanager
def grant_transition_lock(client, owner, *, timeout=35):
    lock = client.lock(owner)
    if not lock.acquire(timeout=timeout):
        raise GmailError("budget_receipt_gmail_busy", 409)
    try:
        yield
    finally:
        lock.release()


@dataclass
class _Progress:
    generation: int
    run: object
    imported: int = 0
    skipped: int = 0
    attempted: int = 0
    restarted: bool = False
    reset_reasons: bool = False
    terms: tuple = field(default_factory=tuple)
    label_id: str | None = None


class GmailSync:
    def __init__(self, settings, store, client, processor, database_path, *, jitter=random.uniform):
        self.settings = settings
        self.store = store
        self.client = client
        self.processor = processor
        self.database_path = database_path
        self.jitter = jitter

    def _require_owner(self, owner):
        if not hmac.compare_digest(owner.encode(), self.settings.owner.encode()):
            raise GmailError("budget_receipt_gmail_unconfigured", 503)

    def _profile(self, run, mailbox_hmac=None):
        profile = run.profile()
        mailbox = canonical_mailbox(profile.get("emailAddress"))
        expected = self.settings.owner_mailbox
        if (mailbox is None or not hmac.compare_digest(mailbox, expected)
                or (mailbox_hmac is not None and not hmac.compare_digest(
                    digest(self.settings.hmac_secret, MAILBOX_DOMAIN, mailbox), mailbox_hmac))):
            self.client.forget(run.owner)
            raise GmailError("mailbox_mismatch", 403, attention="identity_mismatch")
        return _history_id(profile.get("historyId"))

    def transition(self, owner, expected_version, status, *, defer_profile=False):
        self._require_owner(owner)
        with grant_transition_lock(self.client, owner, timeout=0 if defer_profile else 35):
            self.store.check_version(owner, expected_version)
            if status == "active" and defer_profile and not self.processor.ready():
                raise GmailError("parser_unavailable")
            self.client.forget(owner)
            mailbox_hmac = None
            if status == "active":
                if not defer_profile:
                    state = self.store.state(owner)
                    run = self.client.run(owner, state.grant_generation, self.settings.credentials,
                                          lambda: self.store.check_version(owner, expected_version))
                    try:
                        self._profile(run)
                    except GmailError as error:
                        self.store.opt_in_error(owner, expected_version, attention=error.attention, code=error.code,
                                                http_status=error.http_status)
                        raise
                    finally:
                        self.client.forget(owner)
                mailbox_hmac = digest(self.settings.hmac_secret, MAILBOX_DOMAIN, self.settings.owner_mailbox)
            self.store.transition(owner, expected_version, status, mailbox_hmac, queue_sync=defer_profile)
            return self.store.status(owner)

    def _commit(self, owner, progress, changes, pending=(), *, reset_reasons=False):
        should_reset = reset_reasons or (progress.reset_reasons and bool(pending))
        imported, skipped, drained = self.store.commit(
            owner, progress.generation, changes, pending, reset_reasons=should_reset,
        )
        if should_reset:
            progress.reset_reasons = False
        progress.imported += imported
        progress.skipped += skipped
        if not drained:
            raise RunLimit()

    def _start_recovery(self, owner, progress, profile_history, state, *, full):
        now = self.store.clock()
        cutoff = max(0, now - self.settings.backfill_days * DAY)
        if full:
            last_sync = state.last_sync_at if state.last_sync_at is not None else now
            cutoff = max(cutoff, last_sync - 7 * DAY)
        changes = {
            **_PHASE_RESET, "history_id": None, "pending_history_id": profile_history, "resume_cutoff_ts": cutoff,
            "sync_phase": "full_resync" if full else "backfill",
        }
        if full:
            changes["force_full_resync"] = 0
        progress.reset_reasons = False
        self._commit(owner, progress, changes, reset_reasons=True)

    def _completion(self, state):
        now = self.store.clock()
        result = {**_PHASE_RESET, "history_id": state.pending_history_id, "sync_phase": "idle",
                  "last_sync_at": now, "next_sync_at": now + self.settings.sync_interval_seconds,
                  "failures": 0, "backoff_until": 0, "attention": None, "last_http_status": None, "last_error": None}
        if state.sync_phase != "incremental":
            result["backfill_completed_at"] = now
        return result

    def _scope_hmac(self, progress):
        canonical = json.dumps({"labelId": progress.label_id, "labelName": self.settings.label_name,
                                "senders": progress.terms},
                               sort_keys=True, separators=(",", ":"))
        return digest(self.settings.hmac_secret, SCOPE_DOMAIN, canonical)

    def _label(self, run):
        if self.settings.label_name is None:
            return None
        matches = [item for item in _objects(run.labels(), "labels") if item.get("name") == self.settings.label_name]
        if len(matches) != 1:
            raise GmailError("label_missing", attention="configuration")
        return _message_id(matches[0].get("id"))

    def _drain(self, owner, progress, ids, changes, *, backfill_count=None):
        pending = []
        seen = backfill_count
        try:
            for message_id in dict.fromkeys(ids):
                msg_hash = digest(self.settings.hmac_secret, GMAIL_MESSAGE_DOMAIN, message_id)
                if self.store.known(owner, msg_hash):
                    continue
                if progress.attempted >= MAX_SYNC_MESSAGES_PER_RUN:
                    raise RunLimit()
                if seen is not None and seen >= self.settings.backfill_max_messages:
                    raise RunLimit()
                if not pending and self.store.at_pending_cap(owner):
                    raise RunLimit()
                progress.run.require_message_budget()
                progress.attempted += 1
                item = self.processor.process(progress.run, message_id, msg_hash)
                if item is None:
                    continue
                pending.append(item)
                if seen is not None:
                    seen += 1
                if len(pending) >= BATCH_COMMIT_SIZE:
                    chunk, pending = pending, []
                    self._commit(owner, progress, {}, chunk)
            if seen is not None and seen >= self.settings.backfill_max_messages:
                raise RunLimit()
        except (GmailError, RunLimit):
            if pending:
                self._commit(owner, progress, {}, pending)
            raise
        self._commit(owner, progress, changes, pending)

    def _recover_token(self, owner, progress, error, token, *keys):
        if error.http_status != 400:
            raise error
        if token is None or progress.restarted:
            raise GmailError("invalid_page_token", http_status=400, attention="configuration") from None
        progress.restarted = True
        self._commit(owner, progress, dict.fromkeys(keys))

    def _backfill(self, owner, progress):
        scope_hmac = self._scope_hmac(progress)
        while True:
            state = self.store.state(owner)
            token = state.resume_page_token
            if state.scope_scan_query_hmac != scope_hmac:
                self._commit(owner, progress, {"resume_page_token": None, "scope_scan_query_hmac": scope_hmac})
                token = None
            query = _query(self.settings, progress.terms, cutoff=state.resume_cutoff_ts)
            try:
                page = progress.run.messages(query, token)
            except GmailError as error:
                self._recover_token(owner, progress, error, token, "resume_page_token")
                continue
            next_token = _page_token(page, token)
            candidates = [_message_id(item.get("id")) for item in _objects(page, "messages")]
            changes = {"resume_page_token": next_token} if next_token is not None else self._completion(state)
            try:
                self._drain(owner, progress, candidates, changes, backfill_count=state.batch_processed_count)
            except RunLimit:
                if self.store.state(owner).batch_processed_count >= self.settings.backfill_max_messages:
                    self._commit(owner, progress, {"batch_processed_count": 0})
                raise
            if next_token is None:
                return

    def _scope_scan(self, owner, progress, state, ids, history_changes):
        if not ids:
            self._commit(owner, progress, history_changes)
            return
        query = _query(self.settings, progress.terms)
        token = state.scope_scan_page_token
        while True:
            try:
                page = progress.run.messages(query, token)
            except GmailError as error:
                self._recover_token(owner, progress, error, token, "scope_scan_page_token")
                token = None
                continue
            next_token = _page_token(page, token)
            scoped = [_message_id(item.get("id")) for item in _objects(page, "messages")]
            changes = {"scope_scan_page_token": next_token} if next_token is not None else history_changes
            self._drain(owner, progress, (item for item in scoped if item in ids), changes)
            if next_token is None:
                return
            token = next_token

    def _incremental(self, owner, progress, profile_history):
        scope_hmac = self._scope_hmac(progress)
        state = self.store.state(owner)
        if state.pending_history_id is None:
            progress.reset_reasons = True
            self._commit(owner, progress, {"pending_history_id": profile_history, "sync_phase": "incremental"})
        if state.scope_scan_query_hmac != scope_hmac:
            self._commit(owner, progress, {"history_page_token": None, "scope_scan_page_token": None,
                                          "scope_scan_query_hmac": scope_hmac})
        while True:
            state = self.store.state(owner)
            token = state.history_page_token
            try:
                page = progress.run.history(state.history_id, token, progress.label_id)
            except GmailError as error:
                if error.http_status == 404:
                    self._start_recovery(owner, progress, profile_history, state, full=True)
                    self._backfill(owner, progress)
                    return
                self._recover_token(owner, progress, error, token, "history_page_token", "scope_scan_page_token")
                continue
            next_token = _page_token(page, token)
            changes = ({"history_page_token": next_token, "scope_scan_page_token": None}
                       if next_token is not None else self._completion(state))
            self._scope_scan(owner, progress, state, _history_messages(page), changes)
            if next_token is None:
                return

    def _failed(self, owner, progress, error):
        state = self.store.state(owner)
        now = self.store.clock()
        failures = state.failures + 1
        delay = min(21600, MANUAL_COOLDOWN * 2 ** min(9, failures - 1)) + self.jitter(0, 15)
        hint = parse_retry_after(error.retry_after, now=now)
        deadline = max(hint.deadline or 0, now + delay)
        attention = error.attention
        if error.code == "daily_quota" or hint.suspended:
            attention = "quota"
        self._commit(owner, progress, {"failures": failures, "backoff_until": deadline, "run_requested": 0,
                                      "next_sync_at": max(deadline, now + self.settings.sync_interval_seconds),
                                      "attention": attention, "last_http_status": error.http_status,
                                      "last_error": error.code})

    def sync(self, owner, *, force_full_resync=False, poll=False):
        self._require_owner(owner)
        with _sync_lock(self.database_path):
            state = self.store.state(owner)
            generation = state.grant_generation
            self.store.fence(owner, generation)
            now = self.store.clock()
            if poll and state.attention == "identity_mismatch":
                return None
            if poll and not state.run_requested and (state.attention is not None or state.next_sync_at > now):
                return None
            if state.attention == "identity_mismatch":
                raise GmailError("mailbox_mismatch", 403, attention="identity_mismatch")
            if state.backoff_until > now:
                raise GmailError("budget_receipt_gmail_backoff", 429)
            if state.last_attempt_at is not None and state.last_attempt_at + MANUAL_COOLDOWN > now:
                if poll:
                    return None
                raise GmailError("budget_receipt_gmail_cooldown", 429)
            run = self.client.run(owner, generation, self.settings.credentials, lambda: self.store.fence(owner, generation))
            progress = _Progress(generation, run, terms=_terms(self.settings))
            self._commit(owner, progress, {"last_attempt_at": now, "run_requested": 1})
            try:
                if not self.processor.ready():
                    raise GmailError("parser_unavailable")
                if state.mailbox_hmac is None:
                    self.client.forget(owner)
                    raise GmailError("mailbox_mismatch", 403, attention="identity_mismatch")
                profile_history = self._profile(run, state.mailbox_hmac)
                self.store.retain(owner, generation)
                progress.label_id = self._label(run)
                state = self.store.state(owner)
                force = bool(force_full_resync or state.force_full_resync)
                while True:
                    if force or (state.history_id is None and state.pending_history_id is None):
                        self._start_recovery(owner, progress, profile_history, state, full=force)
                    if state.history_id is None or force:
                        self._backfill(owner, progress)
                    else:
                        self._incremental(owner, progress, profile_history)
                    state = self.store.state(owner)
                    if not (state.run_requested and state.force_full_resync):
                        break
                    force = True
            except RunLimit:
                self._commit(owner, progress, {"next_sync_at": self.store.clock() + self.settings.sync_interval_seconds,
                                              "backoff_until": 0, "failures": 0, "attention": None, "run_requested": 0,
                                              "last_http_status": None, "last_error": None})
            except GmailError as error:
                if error.code != "budget_receipt_gmail_disabled":
                    self._failed(owner, progress, error)
                raise
            state = self.store.state(owner)
            self.store.fence(owner, generation)
            return {"ok": True, "sync_phase": state.sync_phase, "imported_count": progress.imported,
                    "skipped_count": progress.skipped, "has_more": state.sync_phase != "idle",
                    "attention": state.attention, "next_sync_at": state.next_sync_at}

    def schedule(self, owner, *, force_full_resync=False):
        """Record a due poller run. Import work stays on the background loop."""
        self._require_owner(owner)
        return self.store.schedule_sync(owner, now=self.store.clock(), force_full_resync=force_full_resync,
                                        cooldown=MANUAL_COOLDOWN)
