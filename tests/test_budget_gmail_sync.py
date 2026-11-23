"""Offline tests for Gmail transport, grant fences and resumable checkpoints."""
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
from unittest import TestCase, mock
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import select

from hestia import db
from hestia.budget import gmail_address, gmail_client as client_module, gmail_sync as sync_module
from hestia.budget.gmail_client import GMAIL_API, TOKEN_URL, GmailClient, GmailCredentials, GmailError, RunLimit
from hestia.budget.gmail_store import DAY, GMAIL_MESSAGE_DOMAIN, GmailStore, PendingMessage, digest
from hestia.budget.gmail_sync import GmailSettings, GmailSync
from hestia.budget.receipt_store import BudgetReceiptStore
from hestia.calendar_sync.providers import HTTPResponse, ProviderError

OWNER = "owner"
MAILBOX = "invoices@example.test"
NOW = 1_800_000_000.0
CREDENTIALS = GmailCredentials("synthetic-oauth", "synthetic-secret", "synthetic-refresh")
SETTINGS = GmailSettings(OWNER, MAILBOX, CREDENTIALS, b"synthetic-gmail-hmac", ("bills@vendor.test",),
                         ("spouse@example.test",))
INVOICE = SimpleNamespace(issuer="Respect Energy", nip="7791011327", invoice_number="FV/A-14/26",
                          amount="100.25", currency="PLN", issue_date="2026-11-02", due_date="2026-11-25")


def response(data, status=200, headers=None):
    return HTTPResponse(status, headers or {}, json.dumps(data).encode())


def page(*ids, token=None):
    return {"messages": [{"id": value} for value in ids], "nextPageToken": token}


def history(*ids, token=None, kind="messagesAdded"):
    return {"history": [{kind: [{"message": {"id": value}} for value in ids]}], "nextPageToken": token}


class FakeGmailTransport:
    """Supports synthetic responses keyed by fixed path and page token."""

    def __init__(self):
        self.calls = []
        self.profile = {"emailAddress": MAILBOX, "historyId": "100"}
        self.labels = {"labels": []}
        self.routes = {}
        self.oauth = [response({"access_token": "synthetic-access", "expires_in": 3600})]

    def request(self, method, url, *, headers, body=None):
        if url == TOKEN_URL:
            assert method == "POST" and headers == {"Content-Type": "application/x-www-form-urlencoded"}
            assert parse_qs(body.decode()).get("refresh_token") == [CREDENTIALS.refresh_token]
            path, params = "token", {}
        else:
            assert url.startswith(GMAIL_API + "/") and method == "GET" and body is None
            assert headers["Authorization"].startswith("Bearer synthetic-")
            split = urlsplit(url.removeprefix(GMAIL_API))
            path, params = split.path, parse_qs(split.query)
        self.calls.append((path, params))
        if path == "token":
            return self.oauth.pop(0) if len(self.oauth) > 1 else self.oauth[0]
        configured = self.routes.get((path, params.get("pageToken", [None])[0]))
        if callable(configured):
            return configured(params)
        if isinstance(configured, Exception):
            raise configured
        if configured is not None:
            return configured
        if path == "/profile":
            return response(self.profile)
        if path == "/labels":
            return response(self.labels)
        return response({})

    def requests(self, path):
        return [item[1] for item in self.calls if item[0] == path]


class FakeClock:
    def __init__(self):
        self.now = NOW
        self.elapsed = 0.0
        self.sleeps = []

    def wall(self):
        return self.now

    def monotonic(self):
        return self.elapsed

    def sleep(self, duration):
        self.sleeps.append(duration)
        self.elapsed += duration


class SyntheticProcessor:
    def __init__(self, clock):
        self.clock = clock
        self.available = True
        self.processed = []
        self.rfc = None
        self.outcome = "imported"

    def ready(self):
        return self.available

    def process(self, run, message_id, message_hmac):
        self.processed.append(message_id)
        run.message(message_id)
        if self.outcome == "imported":
            run.message(message_id, raw=True)
        return PendingMessage(message_hmac, self.clock.wall(), "direct_allowlist", self.outcome, self.rfc, INVOICE)


class GmailTransportTests(TestCase):
    def setUp(self):
        self.transport = FakeGmailTransport()
        self.clock = FakeClock()
        self.client = GmailClient(self.transport, monotonic=self.clock.monotonic, sleep=self.clock.sleep)
        self.run = self.client.run(OWNER, 1, CREDENTIALS, lambda: None)

    def test_identity_address_validation(self):
        self.assertEqual(gmail_address.canonical_mailbox(" A.B+tag@EXAMPLE.Test \t"), "a.b+tag@example.test")
        for value in (None, "a@-test.invalid", "a..b@example.test", "ñ@example.test", "a@example.test\r\n",
                      "x" * 65 + "@example.test", "a@" + ".".join(["label"] * 60)):
            self.assertIsNone(gmail_address.canonical_mailbox(value))
        self.assertIsNotNone(GmailClient().transport)

    def test_pacing_costs_and_token_reuse_across_runs(self):
        self.run.profile()
        self.run.labels()
        self.run.history("100", None, "Label_7")
        self.run.messages("synthetic query", "next")
        self.run.message("mail-a")
        self.run.message("mail-a", raw=True)
        self.assertEqual(self.run.units, 19)
        self.assertEqual(len(self.clock.sleeps), 6)
        for sleep in self.clock.sleeps:
            self.assertAlmostEqual(sleep, 0.05)
        self.assertEqual(self.transport.requests("/history")[0], {
            "startHistoryId": ["100"], "historyTypes": ["messageAdded", "labelAdded"], "maxResults": ["100"],
            "labelId": ["Label_7"],
        })
        self.assertEqual(self.transport.requests("/messages/mail-a")[0]["metadataHeaders"],
                         ["From", "Sender", "Authentication-Results", "DKIM-Signature"])
        self.assertEqual(self.transport.requests("/messages/mail-a")[0]["fields"],
                         ["id,internalDate,sizeEstimate,payload(headers)"])
        self.assertEqual(self.transport.requests("/messages/mail-a")[1], {"format": ["raw"], "fields": ["id,raw"]})
        self.client.run(OWNER, 1, CREDENTIALS, lambda: None).profile()
        self.assertEqual(len(self.transport.requests("token")), 1)
        self.clock.elapsed += 3600
        self.run.profile()
        self.client.run(OWNER, 2, CREDENTIALS, lambda: None).profile()
        self.assertEqual(len(self.transport.requests("token")), 3)

    def test_google_401_refreshes_once_and_drops_denied_token(self):
        statuses = iter((401, 200))
        self.transport.routes[("/profile", None)] = lambda _params: response({}, next(statuses))
        self.run.profile()
        self.assertEqual(self.run.units, 2)
        self.assertEqual(len(self.transport.requests("token")), 2)
        self.transport.routes[("/profile", None)] = response({}, 401)
        with self.assertRaisesRegex(GmailError, "authorization"):
            self.run.profile()
        self.assertEqual(self.client._tokens, {})
        self.assertEqual(len(self.transport.requests("token")), 3)

    def test_invalid_responses_and_network_errors_are_sanitized(self):
        for source in (b'{"a": NaN}', b'{"a": 1, "a": 2}', b'[]', b'\xff'):
            self.transport.routes[("/profile", None)] = HTTPResponse(200, {}, source)
            with self.assertRaisesRegex(GmailError, "^invalid_response$"):
                self.run.profile()
        self.transport.routes[("/profile", None)] = ProviderError("synthetic private diagnostic")
        with self.assertRaisesRegex(GmailError, "^network$"):
            self.run.profile()

    def test_token_response_is_fenced_and_refresh_errors_are_fixed(self):
        self.transport.oauth = [response({"error": "invalid_grant", "description": "private"}, 400)]
        with self.assertRaises(GmailError) as caught:
            self.run.profile()
        self.assertEqual(caught.exception.attention, "authorization")
        for token, expires in ((None, 3600), ("", 3600), ("ß", 3600), ("synthetic- x", 3600),
                               ("synthetic-access", None), ("synthetic-access", 999999)):
            self.transport.oauth = [response({"access_token": token, "expires_in": expires})]
            with self.assertRaisesRegex(GmailError, "^invalid_token_response$"):
                self.run.profile()
        self.transport.oauth = [response({"access_token": "synthetic-access", "expires_in": 3600})]
        checks = iter((None, None, GmailError("fenced")))

        def fence():
            failure = next(checks)
            if failure:
                raise failure

        self.run.fence = fence
        with self.assertRaisesRegex(GmailError, "^fenced$"):
            self.run.profile()
        self.assertFalse(self.transport.requests("/profile"))
        self.assertEqual(self.client._tokens, {})

    def test_quota_run_limit_prevents_dispatch_and_retry(self):
        self.run.units = client_module.MAX_QUOTA_UNITS_PER_RUN
        with self.assertRaises(RunLimit):
            self.run.profile()
        self.assertEqual(self.transport.calls, [])
        self.run.units -= 1
        self.transport.routes[("/profile", None)] = response({}, 401)
        with self.assertRaises(RunLimit):
            self.run.profile()
        self.assertEqual(len(self.transport.requests("/profile")), 1)
        self.run.units -= 1
        with self.assertRaises(RunLimit):
            self.client._dispatch(self.run, "GET", GMAIL_API + "/profile", {}, cost=2)


class GmailSyncTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(directory.name) / "synthetic.db"
        self.engine, self.sessions = db.init_db(self.path)
        with self.sessions.begin() as session:
            session.add_all([db.User(username=OWNER, password_hash="synthetic", role="admin"),
                             db.AppMeta(key="users_authoritative", value="1")])
        self.clock = FakeClock()
        self.transport = FakeGmailTransport()
        self.client = GmailClient(self.transport, monotonic=self.clock.monotonic, sleep=self.clock.sleep)
        self.processor = SyntheticProcessor(self.clock)
        self.store = GmailStore(self.sessions, clock=self.clock.wall)
        self.service = GmailSync(SETTINGS, self.store, self.client, self.processor, self.path, jitter=lambda *_: 0)

    def rows(self, model):
        with self.sessions() as session:
            return list(session.scalars(select(model)))

    def tick(self, seconds=60):
        self.clock.now += seconds

    def enable(self, version=1):
        return self.service.transition(OWNER, version, "active")

    def sync(self, **options):
        return self.service.sync(OWNER, **options)

    def test_grant_is_local_and_versioned_and_revoke_preserves_confirmed(self):
        self.assertEqual(self.store.status(OWNER)["grant_status"], "disabled")
        with self.assertRaisesRegex(GmailError, "disabled"):
            self.sync()
        self.assertEqual(self.transport.calls, [])
        with self.assertRaisesRegex(GmailError, "unconfigured"):
            self.service.sync("viewer")
        self.assertEqual(self.enable()["version"], 2)
        self.transport.routes[("/messages", None)] = response(page("mail-a", "mail-b"))
        self.assertEqual(self.sync()["imported_count"], 2)
        # One finished accounting receipt must survive revocation. The pending one has a child FK.
        first, second = self.rows(db.BudgetReceipt)
        with self.sessions.begin() as session:
            row = session.get(db.BudgetReceipt, first.id)
            row.status, row.payment_method, row.import_expires_at = "confirmed", "unknown", None
            session.add(db.BudgetReceiptLine(receipt_id=second.id, owner=OWNER, line_no=1, line_kind="item",
                                             raw_name="Synthetic item", raw_name_key="synthetic item", quantity="1",
                                             gross_line_total="1", net_line_total="1"))
        before = len(self.transport.calls)
        with self.assertRaisesRegex(GmailError, "stale_version"):
            self.service.transition(OWNER, 1, "revoked")
        self.assertEqual(self.service.transition(OWNER, 2, "disabled")["grant_generation"], 2)
        self.assertIsNone(self.store.state(OWNER).last_attempt_at)
        self.assertEqual(len(self.rows(db.BudgetGmailMessage)), 2)
        self.assertEqual(len(self.rows(db.BudgetReceipt)), 2)
        self.assertEqual(self.service.transition(OWNER, 3, "revoked")["grant_status"], "revoked")
        self.assertEqual(len(self.transport.calls), before)
        self.assertEqual(self.client._tokens, {})
        self.assertEqual(self.rows(db.BudgetGmailMessage), [])
        self.assertEqual(self.rows(db.BudgetReceiptLine), [])
        self.assertEqual([row.id for row in self.rows(db.BudgetReceipt)], [first.id])
        self.assertEqual([json.loads(row.after_json)["action"] for row in self.rows(db.BudgetAnalyticsAudit)],
                         ["enable", "disable", "revoke"])

    def test_bad_identity_on_enable_and_sync_never_persists_email(self):
        other = "wrong@example.test"
        self.transport.profile["emailAddress"] = other
        with self.assertRaisesRegex(GmailError, "^mailbox_mismatch$"):
            self.enable()
        state = self.store.state(OWNER)
        self.assertEqual(state.grant_status, "disabled")
        self.assertEqual(state.attention, "identity_mismatch")
        self.assertIsNone(state.mailbox_hmac)
        self.assertEqual(state.version, 1)
        self.transport.profile["emailAddress"] = MAILBOX.upper()
        self.enable()
        self.transport.profile.pop("emailAddress")
        with self.assertRaisesRegex(GmailError, "^mailbox_mismatch$"):
            self.sync()
        self.assertIsNone(self.sync(poll=True))
        with self.assertRaisesRegex(GmailError, "^mailbox_mismatch$"):
            self.sync()
        self.assertEqual(self.client._tokens, {})
        self.assertEqual(self.transport.requests("/messages"), [])
        self.assertNotIn(other.encode(), self.path.read_bytes())
        self.assertNotIn(MAILBOX.encode(), self.path.read_bytes())
        self.assertEqual(self.store.state(OWNER).last_error, "mailbox_mismatch")

    def test_profile_also_checks_stored_hash_and_required_history_id(self):
        self.transport.profile["historyId"] = "invalid"
        with self.assertRaisesRegex(GmailError, "invalid_response"):
            self.enable()
        self.transport.profile["historyId"] = "100"
        self.enable()
        with self.sessions.begin() as session:
            session.get(db.BudgetGmailState, OWNER).mailbox_hmac = "0" * 64
        with self.assertRaisesRegex(GmailError, "mailbox_mismatch"):
            self.sync()
        self.enable(2)
        with self.sessions.begin() as session:
            session.get(db.BudgetGmailState, OWNER).mailbox_hmac = None
        with self.assertRaisesRegex(GmailError, "mailbox_mismatch"):
            self.sync()

    def test_failed_reauthorization_fences_an_existing_active_generation(self):
        self.enable()
        self.transport.profile["emailAddress"] = "wrong@example.test"
        with self.assertRaisesRegex(GmailError, "^mailbox_mismatch$"):
            self.enable(2)
        state = self.store.state(OWNER)
        self.assertEqual((state.grant_status, state.grant_generation, state.attention),
                         ("active", 2, "identity_mismatch"))
        with self.assertRaisesRegex(GmailError, "disabled"):
            self.store.fence(OWNER, 1)
        self.assertIsNone(self.sync(poll=True))

    def test_duplicate_import_hmac_dedupe_delete_and_expiry(self):
        self.enable()
        self.processor.rfc = "a" * 64
        self.transport.routes[("/messages", None)] = response(page("mail-one", "mail-two", "mail-one"))
        first = self.sync()
        self.assertEqual((first["imported_count"], first["skipped_count"]), (1, 1))
        self.assertEqual(self.processor.processed, ["mail-one", "mail-two"])
        messages = {row.gmail_id_hmac: row for row in self.rows(db.BudgetGmailMessage)}
        first_hmac = digest(SETTINGS.hmac_secret, GMAIL_MESSAGE_DOMAIN, "mail-one")
        self.assertEqual(messages[first_hmac].outcome, "imported")
        receipt, = self.rows(db.BudgetReceipt)
        self.assertEqual(receipt.invoice_number, INVOICE.invoice_number)
        self.assertEqual(receipt.due_date, INVOICE.due_date)
        self.assertIsNone(receipt.raw_ocr_text)
        self.assertIsNone(receipt.image_relpath)
        receipts = BudgetReceiptStore(self.sessions, clock=self.clock.wall, cursor_secret=b"synthetic-session")
        receipts.delete(OWNER, {"receipt_id": receipt.id, "expected_version": 1})
        self.tick()
        self.sync(force_full_resync=True)
        self.assertEqual(self.processor.processed, ["mail-one", "mail-two"])
        self.assertEqual(self.rows(db.BudgetReceipt), [])
        self.assertIn("deleted", {row.outcome for row in self.rows(db.BudgetGmailMessage)})
        self.processor.rfc = None
        self.transport.routes[("/messages", None)] = response(page("new-mail"))
        self.tick()
        self.sync(force_full_resync=True)
        self.tick(30 * DAY)
        receipts.purge_expired(OWNER)
        self.assertEqual(self.rows(db.BudgetReceipt), [])
        self.assertEqual([row.outcome for row in self.rows(db.BudgetGmailMessage)].count("deleted"), 2)
        self.tick(401 * DAY)
        receipts.purge_expired()
        self.assertEqual(self.rows(db.BudgetGmailMessage), [])

    def test_quota_delay_retains_backfill_page_and_honors_retry_after(self):
        self.enable()
        self.transport.routes[("/messages", None)] = response(page("mail-one", token="backfill-two"))
        self.transport.routes[("/messages", "backfill-two")] = response({}, 429, {"Retry-After": "180"})
        with self.assertRaisesRegex(GmailError, "^quota$"):
            self.sync()
        state = self.store.state(OWNER)
        self.assertEqual((state.history_id, state.pending_history_id, state.resume_page_token),
                         (None, "100", "backfill-two"))
        self.assertEqual((state.backoff_until, state.next_sync_at, state.failures, state.last_http_status),
                         (NOW + 180, NOW + SETTINGS.sync_interval_seconds, 1, 429))
        self.assertEqual(self.store.status(OWNER)["batch_processed_count"], 1)
        self.assertIsNone(self.sync(poll=True))
        with self.assertRaisesRegex(GmailError, "backoff"):
            self.sync()
        self.tick(180)
        self.transport.profile["historyId"] = "110"
        self.transport.routes[("/messages", "backfill-two")] = response(page("mail-one", "mail-two"))
        self.assertIsNone(self.sync(poll=True))
        self.assertEqual(self.sync()["imported_count"], 1)
        state = self.store.state(OWNER)
        self.assertEqual((state.history_id, state.pending_history_id, state.resume_page_token), ("100", None, None))
        self.assertEqual(state.failures, 0)
        self.assertEqual(self.processor.processed, ["mail-one", "mail-two"])
        with self.assertRaisesRegex(GmailError, "cooldown"):
            self.sync()

    def test_daily_and_suspended_quota_halt_polling(self):
        self.enable()
        for error in (response({"error": {"errors": [{"reason": "dailyLimitExceeded"}]}}, 403),
                      response({}, 429, {"Retry-After": "604801"}),
                      response({"error": {"errors": [None, {}, {"reason": "userRateLimitExceeded"}]}}, 403)):
            self.transport.routes[("/messages", None)] = error
            with self.assertRaisesRegex(GmailError, "quota"):
                self.sync()
            state = self.store.state(OWNER)
            self.clock.now = state.backoff_until + 1
            if state.failures < 3:
                self.assertEqual(state.attention, "quota")
                self.assertIsNone(self.sync(poll=True))
            else:
                self.assertIsNone(state.attention)

    def test_authorization_scope_error_halts_polling(self):
        self.enable()
        self.transport.routes[("/profile", None)] = response(
            {"error": {"details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}}, 403,
        )
        with self.assertRaisesRegex(GmailError, "authorization"):
            self.sync()
        self.assertEqual(self.client._tokens, {})
        self.tick(3600)
        self.assertIsNone(self.sync(poll=True))

    def test_backfill_cap_is_never_promotion(self):
        self.service.settings = replace(SETTINGS, backfill_max_messages=2)
        self.enable()
        self.transport.routes[("/messages", None)] = response(page("mail-one", "mail-two", "mail-three"))
        first = self.sync()
        self.assertTrue(first["has_more"])
        state = self.store.state(OWNER)
        self.assertEqual((state.history_id, state.pending_history_id, state.batch_processed_count), (None, "100", 0))
        self.tick()
        self.assertFalse(self.sync()["has_more"])
        self.assertEqual(self.store.state(OWNER).history_id, "100")
        self.assertEqual(self.processor.processed, ["mail-one", "mail-two", "mail-three"])
        self.tick()
        self.transport.routes[("/messages", None)] = response(page("mail-four", "mail-five"))
        exact = self.sync(force_full_resync=True)
        self.assertEqual(exact["imported_count"], 2)
        self.assertTrue(exact["has_more"])
        self.assertIsNone(self.store.state(OWNER).history_id)

    def test_invalid_provider_ids_and_tokens_never_promote_cursor(self):
        self.enable()
        for result in ({"messages": "invalid"}, page("invalid/private"), page(token="\nprivate")):
            self.transport.routes[("/messages", None)] = response(result)
            with self.assertRaisesRegex(GmailError, "^invalid_response$"):
                self.sync()
            state = self.store.state(OWNER)
            self.assertIsNone(state.history_id)
            self.assertEqual(state.pending_history_id, "100")
            self.clock.now = state.backoff_until
        self.transport.routes[("/messages", None)] = response({})
        self.sync()
        self.tick()
        self.transport.profile["historyId"] = "120"
        self.transport.routes[("/history", None)] = response({"history": [{"messagesAdded": [{}]}]})
        with self.assertRaisesRegex(GmailError, "^invalid_response$"):
            self.sync()
        self.assertEqual(self.store.state(OWNER).history_id, "100")
        self.assertEqual(self.store.state(OWNER).pending_history_id, "120")

    def test_backfill_invalid_token_replays_from_first_page(self):
        self.enable()
        self.transport.routes[("/messages", None)] = response(page("first", token="page-2"))
        self.transport.routes[("/messages", "page-2")] = response({}, 429)
        with self.assertRaisesRegex(GmailError, "^quota$"):
            self.sync()
        self.tick()
        self.transport.routes[("/messages", "page-2")] = response({}, 400)
        self.transport.routes[("/messages", None)] = response(page("first", "other"))
        self.sync()
        self.assertEqual(self.store.state(OWNER).history_id, "100")
        self.assertNotIn("pageToken", self.transport.requests("/messages")[-1])
        self.assertEqual(self.processor.processed, ["first", "other"])

    def test_message_disappearance_and_transactional_dedupe_are_terminal(self):
        self.enable()
        self.transport.routes[("/messages", None)] = response(page("vanished"))
        with mock.patch.object(self.processor, "process", return_value=None):
            self.assertFalse(self.sync()["has_more"])
        self.assertEqual(self.rows(db.BudgetGmailMessage), [])
        item = PendingMessage("a" * 64, NOW, "untrusted", "skipped_untrusted_sender")
        self.assertEqual(self.store.commit(OWNER, 1, {}, (item, item)), (0, 1, True))
        self.assertEqual(self.store.commit(OWNER, 1, {}, (item,)), (0, 0, True))
        self.assertEqual(len(self.rows(db.BudgetGmailMessage)), 1)

    def test_run_message_and_quota_caps_replay_unfinished_page(self):
        self.enable()
        self.transport.routes[("/messages", None)] = response(page("one", "two", "three"))
        with mock.patch.object(sync_module, "MAX_SYNC_MESSAGES_PER_RUN", 1):
            self.assertTrue(self.sync()["has_more"])
        self.tick()
        with mock.patch.object(client_module, "MAX_QUOTA_UNITS_PER_RUN", 17):
            self.assertEqual(self.sync()["imported_count"], 1)
        self.tick()
        self.assertEqual(self.sync()["imported_count"], 1)
        self.assertEqual(self.processor.processed, ["one", "two", "three"])
        self.assertEqual(self.store.state(OWNER).history_id, "100")

    def test_pending_cap_does_not_advance_checkpoint(self):
        self.enable()
        self.transport.routes[("/messages", None)] = response(page("one", "two"))
        with mock.patch("hestia.budget.gmail_store.MAX_PENDING_IMPORTS", 1):
            first = self.sync()
            self.assertTrue(first["has_more"])
            self.assertEqual(first["next_sync_at"], NOW + SETTINGS.sync_interval_seconds)
            self.tick()
            self.assertTrue(self.sync()["has_more"])
            self.assertEqual(self.processor.processed, ["one", "two"])
        self.assertEqual(len(self.rows(db.BudgetReceipt)), 1)
        self.assertIsNone(self.store.state(OWNER).history_id)
        self.tick()
        self.assertEqual(self.sync()["imported_count"], 1)
        self.assertEqual(self.store.state(OWNER).history_id, "100")

    def test_ordered_chunk_commit_and_worker_retry(self):
        self.enable()
        self.transport.routes[("/messages", None)] = response(page("one", "two", "three"))
        self.processor.available = False
        with self.assertRaisesRegex(GmailError, "parser_unavailable"):
            self.sync()
        self.assertEqual(len(self.transport.requests("/profile")), 1)
        self.assertEqual(self.rows(db.BudgetGmailMessage), [])
        self.processor.available = True
        self.tick()
        self.transport.routes[("/messages/two", None)] = response({}, 503)
        with mock.patch.object(sync_module, "BATCH_COMMIT_SIZE", 1):
            with self.assertRaisesRegex(GmailError, "provider_http"):
                self.sync()
        self.assertEqual(len(self.rows(db.BudgetGmailMessage)), 1)
        self.assertIsNone(self.store.state(OWNER).history_id)
        self.tick(120)
        self.transport.routes.pop(("/messages/two", None))
        self.assertEqual(self.sync()["imported_count"], 2)
        self.assertEqual(self.processor.processed, ["one", "two", "two", "three"])

    def test_incremental_intersects_history_with_scoped_search_and_resumes(self):
        self.service.settings = replace(SETTINGS, label_name="Bills private")
        self.transport.labels = {"labels": [{"name": "Bills private", "id": "Label_42"}]}
        self.enable()
        self.sync()
        self.tick()
        self.transport.profile["historyId"] = "120"
        self.transport.routes[("/history", None)] = response(history("a", "outside", "b", token="h2"))
        self.transport.routes[("/messages", None)] = response(page("a", "different", token="s2"))
        self.transport.routes[("/messages", "s2")] = response({}, 429)
        with self.assertRaisesRegex(GmailError, "quota"):
            self.sync()
        state = self.store.state(OWNER)
        self.assertEqual((state.history_id, state.pending_history_id, state.scope_scan_page_token), ("100", "120", "s2"))
        self.assertIsNone(state.history_page_token)
        self.assertEqual(self.processor.processed, ["a"])
        self.assertEqual(self.transport.requests("/history")[0]["labelId"], ["Label_42"])
        self.assertIn('label:"Bills private"', self.transport.requests("/messages")[-1]["q"][0])
        self.assertNotIn("after:", self.transport.requests("/messages")[-1]["q"][0])
        self.tick()
        self.transport.profile["historyId"] = "140"
        self.transport.routes[("/messages", "s2")] = response(page("b"))
        self.transport.routes[("/history", "h2")] = response(history("c", kind="labelsAdded"))
        result = self.sync()
        self.assertFalse(result["has_more"])
        self.assertEqual(self.processor.processed, ["a", "b"])
        self.assertEqual(self.store.state(OWNER).history_id, "120")
        self.assertIsNone(self.store.state(OWNER).scope_scan_query_hmac)

    def test_scope_change_restarts_history_tokens_from_same_baseline(self):
        self.enable()
        self.sync()
        self.tick()
        self.transport.profile["historyId"] = "120"
        self.transport.routes[("/history", None)] = response(history("first", token="hp2"))
        self.transport.routes[("/history", "hp2")] = response(history("second"))
        self.transport.routes[("/messages", None)] = response(page("first"))
        # Commit history page 1, then retain both page 2 and a scope checkpoint.
        scans = iter((response(page("first")), response(page(token="scope-2"))))
        self.transport.routes[("/messages", None)] = lambda _params: next(scans)
        self.transport.routes[("/messages", "scope-2")] = response({}, 429)
        with self.assertRaisesRegex(GmailError, "quota"):
            self.sync()
        before = self.store.state(OWNER)
        self.assertEqual((before.history_page_token, before.scope_scan_page_token), ("hp2", "scope-2"))
        self.tick()
        self.service.settings = replace(SETTINGS, sender_allowlist=("new@vendor.test",))
        self.transport.profile["historyId"] = "140"
        self.transport.routes[("/history", None)] = response(history("new"))
        self.transport.routes[("/messages", None)] = response(page("new"))
        self.sync()
        self.assertEqual(self.transport.requests("/history")[-1]["startHistoryId"], ["100"])
        self.assertNotIn("pageToken", self.transport.requests("/history")[-1])
        self.assertNotIn("pageToken", self.transport.requests("/messages")[-1])
        self.assertEqual(self.store.state(OWNER).history_id, "120")

    def test_invalid_checkpoints_restart_once_and_expired_baseline_full_resync(self):
        self.enable()
        self.sync()
        self.tick()
        self.transport.profile["historyId"] = "120"
        self.transport.routes[("/history", None)] = response(history("any", token="history-2"))
        self.transport.routes[("/history", "history-2")] = response(history("any"))
        self.transport.routes[("/messages", None)] = response(page(token="scope-2"))
        self.transport.routes[("/messages", "scope-2")] = response({}, 429)
        with self.assertRaisesRegex(GmailError, "quota"):
            self.sync()
        self.tick()
        self.transport.routes[("/messages", "scope-2")] = response({}, 400)
        self.transport.routes[("/messages", None)] = response(page(token="scope-3"))
        self.transport.routes[("/messages", "scope-3")] = response({}, 400)
        with self.assertRaisesRegex(GmailError, "invalid_page_token"):
            self.sync()
        state = self.store.state(OWNER)
        self.assertEqual(state.scope_scan_page_token, "scope-3")
        self.assertEqual(state.attention, "configuration")
        self.tick(120)
        self.transport.routes[("/history", None)] = response(history(token="history-2"))
        self.transport.routes[("/history", "history-2")] = response({}, 429)
        # Finish current scan and land on a history token before the interruption.
        self.transport.routes[("/messages", "scope-3")] = response({})
        with self.assertRaisesRegex(GmailError, "quota"):
            self.sync()
        self.assertEqual(self.store.state(OWNER).history_page_token, "history-2")
        self.tick(240)
        self.transport.routes[("/history", "history-2")] = response({}, 400)
        self.transport.routes[("/history", None)] = response({}, 404)
        self.transport.routes[("/messages", None)] = response({})
        self.transport.profile["historyId"] = "140"
        self.assertFalse(self.sync()["has_more"])
        self.assertIn(f"after:{int(NOW - 7 * DAY)}", self.transport.requests("/messages")[-1]["q"][0])
        self.assertEqual(self.store.state(OWNER).history_id, "140")

    def test_tokenless_400_and_missing_label_pause_without_checkpoint_loss(self):
        self.enable()
        self.transport.routes[("/messages", None)] = response({}, 400)
        with self.assertRaisesRegex(GmailError, "invalid_page_token"):
            self.sync()
        self.assertEqual(len(self.transport.requests("/messages")), 1)
        self.tick()
        self.service.settings = replace(SETTINGS, label_name='My "Bills"')
        with self.assertRaisesRegex(GmailError, "label_missing"):
            self.sync()
        self.assertEqual(self.store.state(OWNER).pending_history_id, "100")
        self.tick(120)
        self.transport.labels = {"labels": [{"name": 'My "Bills"', "id": "Label_1"}]}
        self.transport.routes[("/messages", None)] = response({})
        self.sync()
        self.assertIn('label:"My \\"Bills\\""', self.transport.requests("/messages")[-1]["q"][0])

    def test_file_lock_transition_timeout_and_stale_chunk(self):
        self.enable()
        with sync_module._sync_lock(self.path):
            with self.assertRaisesRegex(GmailError, "sync_busy"):
                self.sync()
        with mock.patch.object(self.client, "lock", return_value=mock.MagicMock(acquire=lambda **_: False)):
            with self.assertRaisesRegex(GmailError, "transition_busy"):
                self.service.transition(OWNER, 2, "revoked")
        self.assertEqual(self.store.state(OWNER).grant_status, "active")
        self.service.transition(OWNER, 2, "revoked")
        with self.assertRaisesRegex(GmailError, "disabled"):
            self.store.commit(OWNER, 1, {"history_id": "123"}, (PendingMessage("a" * 64, NOW, "untrusted",
                                                                                 "skipped_untrusted_sender"),))
        self.assertEqual(self.rows(db.BudgetGmailMessage), [])

    def test_request_and_transition_serialize(self):
        self.enable()
        started = threading.Event()
        unblock = threading.Event()
        errors = []

        def profile(_params):
            started.set()
            assert unblock.wait(3)
            return response(self.transport.profile)

        self.transport.routes[("/profile", None)] = profile

        def run():
            try:
                self.sync()
            except GmailError as error:
                errors.append(error.code)

        syncer = threading.Thread(target=run)
        revoker = threading.Thread(target=lambda: self.service.transition(OWNER, 2, "revoked"))
        syncer.start()
        try:
            self.assertTrue(started.wait(3))
            revoker.start()
            revoker.join(0.05)
            self.assertTrue(revoker.is_alive())
            self.assertEqual(self.store.state(OWNER).grant_status, "active")
        finally:
            unblock.set()
            syncer.join(3)
            revoker.join(3)
        self.assertFalse(syncer.is_alive())
        self.assertFalse(revoker.is_alive())
        self.assertEqual(self.store.state(OWNER).grant_status, "revoked")
        self.assertEqual(self.client._tokens, {})
        self.assertIn(errors, ([], ["budget_receipt_gmail_disabled"]))

    def test_revoke_between_parse_and_commit_discards_uncommitted_invoice(self):
        self.enable()
        self.transport.routes[("/messages", None)] = response(page("first"))
        process = self.processor.process

        def revoke_after_parse(*args):
            item = process(*args)
            self.service.transition(OWNER, 2, "revoked")
            return item

        with mock.patch.object(self.processor, "process", side_effect=revoke_after_parse):
            with self.assertRaisesRegex(GmailError, "disabled"):
                self.sync()
        self.assertEqual(self.rows(db.BudgetReceipt), [])
        self.assertEqual(self.rows(db.BudgetGmailMessage), [])
        self.assertIsNone(self.store.state(OWNER).last_error)

    def test_restart_after_committed_chunk_resumes_without_duplicates(self):
        self.enable()
        self.transport.routes[("/messages", None)] = response(page("one", "two"))
        self.service.schedule(OWNER)
        process = self.processor.process

        def crash_on_second(*args):
            if args[1] == "two":
                raise RuntimeError("synthetic container stop")
            return process(*args)

        with mock.patch.object(sync_module, "BATCH_COMMIT_SIZE", 1), \
                mock.patch.object(self.processor, "process", side_effect=crash_on_second):
            with self.assertRaisesRegex(RuntimeError, "container stop"):
                self.sync(poll=True)
        state = self.store.state(OWNER)
        self.assertEqual((state.sync_phase, state.history_id, state.pending_history_id, state.run_requested),
                         ("backfill", None, "100", 0))
        self.assertEqual(len(self.rows(db.BudgetReceipt)), 1)
        with sync_module._sync_lock(self.path):
            pass

        self.processor = SyntheticProcessor(self.clock)
        self.client = GmailClient(self.transport, monotonic=self.clock.monotonic, sleep=self.clock.sleep)
        self.service = GmailSync(SETTINGS, self.store, self.client, self.processor, self.path, jitter=lambda *_: 0)
        self.tick(60)
        restarted = self.sync(poll=True)
        state = self.store.state(OWNER)
        self.assertEqual(restarted["imported_count"], 1)
        self.assertEqual(self.processor.processed, ["two"])
        self.assertEqual(len(self.rows(db.BudgetReceipt)), 2)
        self.assertEqual(len(self.rows(db.BudgetGmailMessage)), 2)
        self.assertEqual((state.sync_phase, state.history_id, state.pending_history_id, state.run_requested),
                         ("idle", "100", None, 0))

    def test_repeated_kicks_never_run_parallel_pollers(self):
        self.enable()
        self.transport.routes[("/messages", None)] = response(page("one"))
        self.service.schedule(OWNER)
        self.service.schedule(OWNER)
        before_profiles = len(self.transport.requests("/profile"))
        started = threading.Event()
        unblock = threading.Event()
        errors = []

        def profile(_params):
            started.set()
            assert unblock.wait(3)
            return response(self.transport.profile)

        self.transport.routes[("/profile", None)] = profile

        def poll():
            try:
                self.sync(poll=True)
            except GmailError as error:
                errors.append(error.code)

        first = threading.Thread(target=poll)
        second = threading.Thread(target=poll)
        first.start()
        try:
            self.assertTrue(started.wait(3))
            second.start()
            second.join(3)
            self.assertFalse(second.is_alive())
        finally:
            unblock.set()
            first.join(3)
            second.join(3)
        self.assertFalse(first.is_alive())
        self.assertIn(errors, ([], ["budget_receipt_gmail_sync_busy"]))
        self.assertEqual(len(self.transport.requests("/profile")), before_profiles + 1)
        self.assertEqual(len(self.rows(db.BudgetReceipt)), 1)
        self.assertEqual(self.store.state(OWNER).run_requested, 0)

    def test_schedule_queues_a_poller_run_without_calling_gmail(self):
        with self.assertRaisesRegex(GmailError, "disabled"):
            self.service.schedule(OWNER)
        with self.assertRaisesRegex(GmailError, "unconfigured"):
            self.service.schedule("intruder")
        self.enable()
        calls = list(self.transport.calls)
        queued = self.service.schedule(OWNER, force_full_resync=True)
        state = self.store.state(OWNER)
        self.assertEqual(self.transport.calls, calls)
        self.assertEqual((queued["ok"], queued["sync_phase"], queued["version"]), (True, "idle", state.version))
        self.assertEqual((state.run_requested, state.force_full_resync, state.next_sync_at), (1, 1, self.clock.now))
        kept = self.service.schedule(OWNER, force_full_resync=False)
        self.assertEqual(kept["version"], queued["version"] + 1)
        self.assertEqual(self.store.state(OWNER).force_full_resync, 1)
        with self.sessions.begin() as session:
            session.get(db.BudgetGmailState, OWNER).attention = "identity_mismatch"
        with self.assertRaisesRegex(GmailError, "mailbox_mismatch"):
            self.service.schedule(OWNER)
        self.assertEqual(self.store.state(OWNER).version, kept["version"])

    def test_poller_consumes_a_scheduled_full_resync(self):
        self.enable()
        self.transport.routes[("/messages", None)] = response(page("mail-one"))
        self.sync()
        self.tick(60)
        self.service.schedule(OWNER, force_full_resync=True)
        before_messages = len(self.transport.requests("/messages"))
        before_history = len(self.transport.requests("/history"))
        self.sync(poll=True)
        state = self.store.state(OWNER)
        self.assertEqual((state.force_full_resync, state.run_requested, state.sync_phase), (0, 0, "idle"))
        self.assertGreater(len(self.transport.requests("/messages")), before_messages)
        self.assertEqual(len(self.transport.requests("/history")), before_history)

    def test_poll_waits_out_cooldown_without_recording_a_failure(self):
        self.enable()
        self.transport.routes[("/messages", None)] = response(page("mail-one"))
        self.sync()
        with self.sessions.begin() as session:
            row = session.get(db.BudgetGmailState, OWNER)
            row.next_sync_at = self.clock.now
            row.run_requested = 1
        self.assertIsNone(self.sync(poll=True))
        state = self.store.state(OWNER)
        self.assertEqual((state.failures, state.run_requested), (0, 1))
        with self.assertRaisesRegex(GmailError, "cooldown"):
            self.service.schedule(OWNER)
        self.assertEqual(self.store.state(OWNER).run_requested, 1)
        self.tick(60)
        self.sync(poll=True)
        self.assertEqual(self.store.state(OWNER).run_requested, 0)
        self.assertEqual(self.store.state(OWNER).failures, 0)

    def test_scheduled_run_retries_after_quota_attention(self):
        self.enable()
        self.transport.routes[("/messages", None)] = response(
            {"error": {"errors": [{"reason": "dailyLimitExceeded"}]}}, 403)
        with self.assertRaisesRegex(GmailError, "quota"):
            self.sync()
        with self.assertRaisesRegex(GmailError, "backoff"):
            self.service.schedule(OWNER)
        state = self.store.state(OWNER)
        self.assertEqual(state.attention, "quota")
        self.assertEqual(state.run_requested, 0)
        self.clock.now = state.backoff_until
        self.assertIsNone(self.sync(poll=True))
        self.transport.routes[("/messages", None)] = response(page())
        self.assertEqual(self.service.schedule(OWNER)["ok"], True)
        self.sync(poll=True)
        state = self.store.state(OWNER)
        self.assertIsNone(state.attention)
        self.assertEqual((state.sync_phase, state.run_requested, state.failures), ("idle", 0, 0))
