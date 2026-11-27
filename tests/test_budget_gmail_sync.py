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

from hestia import api_contract, db
from hestia.budget import gmail_address, gmail_client as client_module, gmail_message, gmail_sync as sync_module
from hestia.budget.gmail_client import GMAIL_API, TOKEN_URL, GmailClient, GmailCredentials, GmailError, RunLimit
from hestia.budget.gmail_store import (
    DAY, GMAIL_MESSAGE_DOMAIN, IMPORT_TTL, PARSE_FAILED_REASONS, GmailStore, PendingMessage,
    _reasons_key, digest,
)
from hestia.budget.gmail_sync import GmailSettings, GmailSync, PaymentSender
from hestia.budget.gmail_worker import WorkerUnavailable
from hestia.budget.receipt_reconcile import reconcile_receipt_links
from hestia.budget.receipt_store import BudgetReceiptStore
from hestia.calendar_sync.providers import HTTPResponse, ProviderError
from tests.test_budget_gmail_message import MERCHANT, _email, _metadata, _raw

OWNER = "owner"
MAILBOX = "invoices@example.test"
NOW = 1_800_000_000.0
CREDENTIALS = GmailCredentials("synthetic-oauth", "synthetic-secret", "synthetic-refresh")
SETTINGS = GmailSettings(OWNER, MAILBOX, CREDENTIALS, b"synthetic-gmail-hmac", ("bills@vendor.test",),
                         ("spouse@example.test",), require_sender_authentication=True)
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
        self.parse_failed_reason = None

    def ready(self):
        return self.available

    def process(self, run, message_id, message_hmac):
        self.processed.append(message_id)
        run.message(message_id)
        if self.outcome == "imported":
            run.message(message_id, raw=True)
        return PendingMessage(message_hmac, self.clock.wall(), "direct_allowlist", self.outcome, self.rfc, INVOICE,
                              parse_failed_reason=self.parse_failed_reason)


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
            "startHistoryId": ["100"], "historyTypes": ["messageAdded", "labelAdded", "labelRemoved"], "maxResults": ["100"],
            "labelId": ["Label_7"],
        })
        self.assertEqual(self.transport.requests("/messages/mail-a")[0]["metadataHeaders"],
                         ["From", "Sender", "Authentication-Results", "DKIM-Signature"])
        self.assertEqual(self.transport.requests("/messages/mail-a")[0]["fields"],
                         ["id,internalDate,sizeEstimate,payload(headers),labelIds"])
        self.assertEqual(self.transport.requests("/messages/mail-a")[1], {"format": ["raw"], "fields": ["id,raw,labelIds"]})
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

    def test_manual_review_expiry_cap_and_revoke_preserve_receipt_returned_to_review(self):
        self.enable()
        manual = PendingMessage("a" * 64, NOW, "owner_forward", "imported",
                                forwarded_manually=True, sender_domain="vendor.test")
        old_items = tuple(replace(manual, gmail_id_hmac=key * 64) for key in "abc")
        self.assertEqual(self.store.commit(OWNER, 1, {}, old_items), (3, 0, True))
        messages = {row.gmail_id_hmac[0]: row for row in self.rows(db.BudgetGmailMessage)}
        with self.sessions.begin() as session:
            legacy = session.get(db.BudgetReceipt, messages["b"].receipt_id)
            legacy.import_expires_at = None
            session.add(db.BudgetConnection(id="connection", owner=OWNER, application_id="fixture",
                                            bank_name="Fixture", country="PL", enabled=True,
                                            next_attempt=0, failures=0))
            session.flush()
            session.add(db.BudgetAccount(id="account", connection_id="connection", owner=OWNER,
                                         identification_hash="fixture", enabled=True, balances_json="{}"))
            session.flush()
            session.add(db.BudgetTransaction(account_id="account", entry_reference="old-entry",
                                             payload_json="{}", observed_at=NOW, uncertain=True))
            session.flush()
            confirmed = session.get(db.BudgetReceipt, messages["c"].receipt_id)
            confirmed.status, confirmed.payment_method, confirmed.import_expires_at = "confirmed", "card", None
            confirmed.receipt_date, confirmed.total_amount, confirmed.currency = "2026-11-21", "100", "PLN"
            confirmed.linked_account_id, confirmed.linked_entry_ref = "account", "old-entry"
            confirmed.linked_fingerprint, confirmed.linked_evidence = "prior-fingerprint", "{}"
            self.assertEqual(reconcile_receipt_links(session, OWNER, NOW), 1)
            self.assertEqual(confirmed.status, "needs_review")

        self.assertEqual(self.store.status(OWNER)["pending_imported_count"], 2)
        with mock.patch("hestia.budget.gmail_store.MAX_PENDING_IMPORTS", 2):
            self.assertTrue(self.store.at_pending_cap(OWNER))
        with mock.patch("hestia.budget.gmail_store.MAX_PENDING_IMPORTS", 3):
            self.assertFalse(self.store.at_pending_cap(OWNER))

        self.tick(IMPORT_TTL - 1)
        self.store.retain(OWNER, 1)
        self.assertEqual(len(self.rows(db.BudgetReceipt)), 3)
        with self.sessions() as session:
            self.assertEqual(session.get(db.BudgetReceipt, messages["a"].receipt_id).import_expires_at,
                             NOW + IMPORT_TTL)
        self.tick(1)
        self.store.retain(OWNER, 1)
        self.assertEqual([row.id for row in self.rows(db.BudgetReceipt)], [messages["c"].receipt_id])
        tombstones = {row.gmail_id_hmac[0]: row for row in self.rows(db.BudgetGmailMessage)}
        for key in "ab":
            self.assertEqual((tombstones[key].outcome, tombstones[key].receipt_id), ("deleted", None))

        fresh = replace(manual, gmail_id_hmac="d" * 64, internal_date=self.clock.now)
        self.assertEqual(self.store.commit(OWNER, 1, {}, (fresh,)), (1, 0, True))
        self.service.transition(OWNER, 2, "revoked")
        self.assertEqual([row.id for row in self.rows(db.BudgetReceipt)], [messages["c"].receipt_id])
        self.assertEqual(self.rows(db.BudgetGmailMessage), [])

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

    def test_manual_placeholder_and_authenticated_invoice_dedupe_independently_in_either_order(self):
        self.enable()
        for manual_id, direct_id, rfc, manual_first in (("a", "b", "e", True), ("c", "d", "f", False)):
            with self.subTest(manual_first=manual_first):
                manual = PendingMessage(manual_id * 64, NOW, "owner_forward", "imported", rfc * 64,
                                        forwarded_manually=True, sender_domain="vendor.test")
                direct = PendingMessage(direct_id * 64, NOW, "direct_allowlist", "imported", rfc * 64, INVOICE)
                items = (manual, direct) if manual_first else (direct, manual)
                self.assertEqual(self.store.commit(OWNER, 1, {}, items), (2, 0, True))
                self.assertEqual(self.store.commit(OWNER, 1, {}, items), (0, 0, True))
                with self.sessions() as session:
                    review = session.get(db.BudgetGmailMessage, (OWNER, manual.gmail_id_hmac))
                    invoice = session.get(db.BudgetGmailMessage, (OWNER, direct.gmail_id_hmac))
                    self.assertIsNone(review.rfc_message_id_hmac)
                    self.assertEqual(invoice.rfc_message_id_hmac, rfc * 64)
                    self.assertEqual(session.get(db.BudgetReceipt, review.receipt_id).status, "needs_review")
                    self.assertEqual(session.get(db.BudgetReceipt, invoice.receipt_id).status, "imported")

    def test_legacy_linked_placeholder_hash_does_not_suppress_an_invoice(self):
        self.enable()
        for manual_id, direct_id, rfc, expire_first in (("a", "b", "e", False), ("c", "d", "f", True)):
            with self.subTest(expire_first=expire_first):
                now = self.clock.now
                manual = PendingMessage(manual_id * 64, now, "spouse_forward", "imported",
                                        forwarded_manually=True, sender_domain="vendor.test")
                self.assertEqual(self.store.commit(OWNER, 1, {}, (manual,)), (1, 0, True))
                with self.sessions.begin() as session:
                    row = session.get(db.BudgetGmailMessage, (OWNER, manual.gmail_id_hmac))
                    row.rfc_message_id_hmac = rfc * 64
                if expire_first:
                    self.tick(IMPORT_TTL)
                    self.store.retain(OWNER, 1)
                    with self.sessions() as session:
                        row = session.get(db.BudgetGmailMessage, (OWNER, manual.gmail_id_hmac))
                        self.assertEqual((row.outcome, row.rfc_message_id_hmac), ("deleted", None))
                direct = PendingMessage(direct_id * 64, self.clock.now, "direct_allowlist", "imported",
                                        rfc * 64, INVOICE)
                self.assertEqual(self.store.commit(OWNER, 1, {}, (direct,)), (1, 0, True))

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

    def _use_poison_pdf(self, *, strict=False):
        settings = replace(SETTINGS, require_sender_authentication=strict, sender_allowlist=(MERCHANT,))
        worker = mock.Mock()
        worker.ready.return_value = True

        def parse(kind, part):
            if kind == "pdf":
                raise WorkerUnavailable("synthetic parse exchange failed")
            return part.decode("utf-8")

        worker.parse.side_effect = parse
        self.service.settings = settings
        self.service.processor = gmail_message.GmailMessageProcessor(settings, worker, discover_documents=True)
        self._route_crashing_pdf("poison", sender=MERCHANT if strict else "invoices@unlisted.example")
        self._route_mime("good", _email())
        self.transport.routes[("/messages", None)] = response(page("poison", "good"))
        self.enable()
        return worker

    def _route_crashing_pdf(self, message_id, *, sender="invoices@unlisted.example"):
        message = _email(sender, text="")
        message.set_content(b"", maintype="text", subtype="plain")
        message.add_attachment(b"%PDF-1.4 synthetic PDF", maintype="application", subtype="pdf")
        self._route_mime(message_id, message)

    def _route_mime(self, message_id, message):
        responses = {"raw": _raw(message), "metadata": _metadata(message)}
        self.transport.routes[("/messages/" + message_id, None)] = lambda params: response(
            {**responses[params["format"][0]], "id": message_id}
        )

    def test_failed_pdf_with_healthy_probe_is_skipped_and_can_be_retried(self):
        worker = self._use_poison_pdf()
        self.transport.routes[("/messages", None)] = response(page("poison", token="next"))
        self.transport.routes[("/messages", "next")] = response(page("good"))
        with self.assertLogs("hestia.budget.gmail_sync", level="WARNING") as logs:
            result = self.sync()
        self.assertIn("reason=worker_crash", logs.output[0])
        self.assertEqual((result["imported_count"], result["skipped_count"], result["has_more"]), (1, 1, False))
        self.assertEqual(worker.ready.call_count, 2)
        self.assertEqual(self.store.status(OWNER)["parse_failed_reasons"], {"worker_crash": 1})
        failed_hash = digest(SETTINGS.hmac_secret, GMAIL_MESSAGE_DOMAIN, "poison")
        outcomes = {row.gmail_id_hmac: row.outcome for row in self.rows(db.BudgetGmailMessage)}
        self.assertEqual(outcomes[failed_hash], "parse_failed")
        self.assertIsNone(self.store.state(OWNER).last_error)

        self.tick()
        self.service.schedule(OWNER, retry_parse_failed=True)
        self.assertFalse(self.store.known(OWNER, failed_hash))
        worker.parse.reset_mock(side_effect=True)
        worker.parse.return_value = ""
        self.sync(poll=True)
        worker.parse.assert_called_once_with("pdf", b"%PDF-1.4 synthetic PDF")
        outcomes = {row.gmail_id_hmac: row.outcome for row in self.rows(db.BudgetGmailMessage)}
        self.assertEqual(outcomes[failed_hash], "skipped_non_receipt")

    def test_startup_outage_preserves_the_message_abort_marker(self):
        worker = self._use_poison_pdf()
        # The second attempt never reaches the message because the worker is still down.
        worker.ready.side_effect = [True, False, False, True, False]
        for delay in (60, 120):
            with self.assertRaisesRegex(GmailError, "^parser_unavailable$"):
                self.sync()
            status = self.store.status(OWNER)
            self.assertEqual(status["last_error"], "parser_unavailable")
            self.assertEqual(status["batch_processed_count"], 0)
            self.assertEqual(status["parse_failed_reasons"], {})
            self.assertEqual(self.rows(db.BudgetGmailMessage), [])
            self.assertEqual(self.transport.requests("/messages/good"), [])
            self.tick(delay)
        self.assertEqual(worker.parse.call_count, 1)
        with self.assertRaisesRegex(GmailError, "^parser_unavailable$"):
            self.sync()
        quarantined, = self.rows(db.BudgetGmailMessage)
        self.assertEqual(quarantined.outcome, "parse_failed")
        self.assertEqual(self.store.status(OWNER)["parse_failed_reasons"], {"worker_rejected": 1})

    def test_probe_healthy_crashes_in_consecutive_messages_abort_without_marking_mail(self):
        worker = self._use_poison_pdf()
        self._route_crashing_pdf("second-pdf")
        self.transport.routes[("/messages", None)] = response(page("poison", token="next"))
        self.transport.routes[("/messages", "next")] = response(page("poison", "second-pdf", "good"))
        with self.assertRaisesRegex(GmailError, "^parser_unavailable$"):
            self.sync()
        self.assertEqual(worker.parse.call_count, 2)
        self.assertEqual(worker.ready.call_count, 3)
        self.assertEqual(self.transport.requests("/messages/good"), [])
        self.assertEqual(self.rows(db.BudgetGmailMessage), [])
        state = self.store.state(OWNER)
        self.assertEqual((state.last_error, state.failures, state.resume_page_token), ("parser_unavailable", 1, None))

    def test_worker_crash_total_cap_applies_to_explicit_retry_with_text_between_crashes(self):
        worker = self._use_poison_pdf()
        for message_id in ("second-pdf", "third-pdf"):
            self._route_crashing_pdf(message_id)
        self._route_mime("second-text", _email(rfc_id="<second@synthetic.example>"))
        self.transport.routes[("/messages", None)] = response(
            page("poison", "good", "second-pdf", "second-text", "third-pdf", "after"),
        )
        failed_hash = digest(SETTINGS.hmac_secret, GMAIL_MESSAGE_DOMAIN, "poison")
        self.store.commit(OWNER, 1, {}, (
            PendingMessage(failed_hash, NOW, "untrusted", "parse_failed", parse_failed_reason="worker_crash"),
        ))
        self.service.schedule(OWNER, retry_parse_failed=True)
        self.assertFalse(self.store.known(OWNER, failed_hash))
        with self.assertRaisesRegex(GmailError, "^parser_unavailable$"):
            self.sync(poll=True)
        self.assertEqual([call.args[0] for call in worker.parse.call_args_list], ["pdf", "text", "pdf", "text", "pdf"])
        self.assertEqual(self.transport.requests("/messages/after"), [])
        outcomes = {message.gmail_id_hmac: message.outcome for message in self.rows(db.BudgetGmailMessage)}
        self.assertEqual(list(outcomes.values()).count("imported"), 2)
        self.assertEqual(list(outcomes.values()).count("parse_failed"), 2)
        self.assertNotIn(digest(SETTINGS.hmac_secret, GMAIL_MESSAGE_DOMAIN, "third-pdf"), outcomes)
        self.assertEqual(self.store.status(OWNER)["parse_failed_reasons"], {"worker_crash": 2})
        self.assertEqual(self.store.state(OWNER).last_error, "parser_unavailable")

    def test_different_messages_aborting_consecutive_runs_are_not_quarantined(self):
        self._use_poison_pdf()
        self._route_crashing_pdf("second-pdf")
        for message_id in ("poison", "second-pdf"):
            self.transport.routes[("/messages", None)] = response(page(message_id))
            with self.assertRaisesRegex(GmailError, "^parser_unavailable$"):
                self.sync()
            self.assertEqual(self.rows(db.BudgetGmailMessage), [])
            self.tick()
        self.assertEqual(self.store.consume_parser_abort(OWNER, 1),
                         digest(SETTINGS.hmac_secret, GMAIL_MESSAGE_DOMAIN, "second-pdf"))

    def test_unconfirmed_crash_at_end_or_attempt_cap_preserves_checkpoint(self):
        worker = self._use_poison_pdf()
        self.transport.routes[("/messages", None)] = response(page("poison"))
        with self.assertRaisesRegex(GmailError, "^parser_unavailable$"):
            self.sync()
        self.assertEqual(self.rows(db.BudgetGmailMessage), [])
        self.assertIsNone(self.store.state(OWNER).history_id)

        self.tick()
        self._route_crashing_pdf("different")
        self.transport.routes[("/messages", None)] = response(page("different", "good"))
        with mock.patch.object(sync_module, "MAX_SYNC_MESSAGES_PER_RUN", 1):
            with self.assertRaisesRegex(GmailError, "^parser_unavailable$"):
                self.sync()
        self.assertEqual(self.rows(db.BudgetGmailMessage), [])
        self.assertEqual(worker.parse.call_count, 2)
        self.assertEqual(self.store.state(OWNER).last_error, "parser_unavailable")

    def test_incremental_crash_confirmation_saves_matching_history_and_scope_tokens(self):
        self._use_poison_pdf()
        self.transport.routes[("/messages", None)] = response(page())
        self.sync()
        self.tick()
        self.transport.profile["historyId"] = "120"
        self.transport.routes[("/history", None)] = response(history("poison", token="h2"))
        self.transport.routes[("/history", "h2")] = response(history("good", "later"))
        self.transport.routes[("/messages", None)] = response(page("poison", "good", token="s2"))
        self.transport.routes[("/messages", "s2")] = response({}, 503)
        with self.assertRaisesRegex(GmailError, "^provider_http$"):
            self.sync()
        state = self.store.state(OWNER)
        self.assertEqual((state.history_page_token, state.scope_scan_page_token), ("h2", "s2"))
        self.assertEqual(self.store.status(OWNER)["parse_failed_reasons"], {"worker_crash": 1})

    def test_expired_history_during_a_held_crash_leaves_the_previous_checkpoint(self):
        self._use_poison_pdf()
        self.transport.routes[("/messages", None)] = response(page())
        self.sync()
        self.tick()
        self.transport.profile["historyId"] = "120"
        self.transport.routes[("/messages", None)] = response(page("poison"))
        self.transport.routes[("/history", None)] = response(history("poison", token="h2"))
        self.transport.routes[("/history", "h2")] = response({}, 404)
        with self.assertRaisesRegex(GmailError, "^parser_unavailable$"):
            self.sync()
        state = self.store.state(OWNER)
        self.assertEqual((state.sync_phase, state.history_id, state.history_page_token), ("incremental", "100", None))
        self.assertEqual(self.rows(db.BudgetGmailMessage), [])

    def test_parser_abort_marker_survives_a_short_commit_without_advancing_the_page(self):
        self.enable()
        message_hash = "a" * 64
        pending = PendingMessage("b" * 64, NOW, "direct_allowlist", "imported", invoice=INVOICE)
        with mock.patch("hestia.budget.gmail_store.MAX_PENDING_IMPORTS", 0):
            self.assertEqual(self.store.commit(OWNER, 1, {"resume_page_token": "next"}, (pending,),
                                               parser_abort=message_hash), (0, 0, False))
        self.assertIsNone(self.store.state(OWNER).resume_page_token)
        self.assertEqual(self.store.consume_parser_abort(OWNER, 1), message_hash)

    def test_two_consecutive_probe_failures_quarantine_the_same_message(self):
        worker = self._use_poison_pdf()
        worker.ready.side_effect = [True, False, True, False, True]
        with self.assertRaisesRegex(GmailError, "^parser_unavailable$"):
            self.sync()
        self.assertEqual(self.rows(db.BudgetGmailMessage), [])

        self.tick()
        # The next service instance reads the abort marker from the database.
        self.service = GmailSync(
            self.service.settings, GmailStore(self.sessions, clock=self.clock.wall), self.client,
            self.service.processor, self.path, jitter=lambda *_: 0,
        )
        with self.assertLogs("hestia.budget.gmail_sync", level="WARNING") as logs:
            with self.assertRaisesRegex(GmailError, "^parser_unavailable$"):
                self.sync()
        self.assertIn("reason=worker_rejected", logs.output[0])
        failed, = self.rows(db.BudgetGmailMessage)
        self.assertEqual((failed.outcome, failed.receipt_id), ("parse_failed", None))
        self.assertEqual(self.store.status(OWNER)["parse_failed_reasons"], {"worker_rejected": 1})
        self.assertEqual(self.store.state(OWNER).batch_processed_count, 1)

        self.tick(120)
        result = self.sync()
        self.assertEqual((result["imported_count"], result["skipped_count"], result["has_more"]), (1, 0, False))
        self.assertEqual(len(self.transport.requests("/messages/poison")), 4)
        self.assertEqual([call.args[0] for call in worker.parse.call_args_list], ["pdf", "pdf", "text"])

    def test_buffered_imports_at_pending_cap_do_not_mask_a_parser_outage(self):
        worker = self._use_poison_pdf(strict=True)
        worker.ready.side_effect = [True, False]
        self._route_mime("second", _email(rfc_id="<second@synthetic.example>"))
        self.transport.routes[("/messages", None)] = response(page("good", "second", "poison"))
        with mock.patch("hestia.budget.gmail_store.MAX_PENDING_IMPORTS", 1):
            with self.assertRaisesRegex(GmailError, "^parser_unavailable$"):
                self.sync()
        imported, = self.rows(db.BudgetGmailMessage)
        self.assertEqual(imported.outcome, "imported")
        self.assertEqual(self.store.status(OWNER)["parse_failed_reasons"], {})

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

    def test_gmail_document_queries_include_body_and_pdf_in_a_configured_label(self):
        self.service.settings = replace(SETTINGS, require_sender_authentication=False, label_name="Bills")
        self.transport.labels = {"labels": [{"id": "Label_7", "name": "Bills"}]}
        self.enable()
        self.sync()
        self.assertEqual(self.transport.requests("/history"), [])
        self.tick()
        self.transport.routes[("/history", None)] = response(history("new-card-invoice"))
        self.transport.routes[("/messages", None)] = response(page("new-card-invoice", token="unneeded"))
        self.sync()
        queries = [params["q"][0] for params in self.transport.requests("/messages")]
        self.assertEqual(len(queries), 2)
        self.assertIn("after:", queries[0])
        self.assertNotIn("after:", queries[1])
        for query in queries:
            self.assertIn('label:"Bills" {', query)
            self.assertIn('from:"bills@vendor.test"', query)
            for term in ('"dokument rozliczeniowy"', "filename:pdf", "faktura", "paragon", "invoice", "receipt"):
                self.assertIn(term, query)
        self.assertEqual(self.processor.processed, ["new-card-invoice"])

    def test_relaxing_sender_authentication_retries_prior_misses_once(self):
        self.enable()
        self.sync()
        def key(message_id):
            return digest(SETTINGS.hmac_secret, GMAIL_MESSAGE_DOMAIN, message_id)

        receipts = {"invoice": "skipped_untrusted_sender", "rejected": "skipped_untrusted_sender",
                    "removed": "deleted", "other": "skipped_non_receipt", "failed": "parse_failed",
                    "oversized": "skipped_oversized", "duplicate": "skipped_duplicate"}
        self.store.commit(OWNER, 1, {}, tuple(PendingMessage(key(msg), NOW, "untrusted", outcome)
                                              for msg, outcome in receipts.items()))
        self.tick(15 * DAY)
        self.service.settings = replace(SETTINGS, require_sender_authentication=False)
        self.transport.routes[("/messages", None)] = response(page(*receipts))
        original_process = self.processor.process

        def parse(run, message_id, message_hmac):
            self.processor.outcome = "skipped_untrusted_sender" if message_id == "rejected" else "imported"
            return original_process(run, message_id, message_hmac)
        with mock.patch.object(self.processor, "process", side_effect=parse):
            with mock.patch.object(sync_module, "MAX_SYNC_MESSAGES_PER_RUN", 1):
                self.assertTrue(self.sync()["has_more"])
            self.tick()
            self.assertFalse(self.sync()["has_more"])
            self.assertEqual(self.processor.processed, ["invoice", "rejected", "other", "failed"])
            queries = [entry["q"][0] for entry in self.transport.requests("/messages")]
            cutoff = int(NOW + 15 * DAY - SETTINGS.backfill_days * DAY)
            self.assertTrue(all(f"after:{cutoff}" in query for query in queries[-2:]))
            self.transport.routes[("/history", None)] = response(history(*receipts))
            self.tick()
            self.sync()
            self.assertEqual(self.processor.processed, ["invoice", "rejected", "other", "failed"])
            self.assertNotIn("after:", self.transport.requests("/messages")[-1]["q"][0])

        messages = {row.gmail_id_hmac: row.outcome for row in self.rows(db.BudgetGmailMessage)}
        self.assertEqual(messages, {key(msg): "imported" if msg in ("invoice", "other", "failed") else outcome
                                    for msg, outcome in receipts.items()})
        self.assertEqual(len(self.rows(db.BudgetReceipt)), 3)
        self.service.settings = SETTINGS
        self.tick()
        self.sync()
        self.assertNotIn("filename:pdf", self.transport.requests("/messages")[-1]["q"][0])
        self.service.settings = replace(SETTINGS, require_sender_authentication=False)
        self.tick()
        self.sync()
        self.assertEqual(self.processor.processed, ["invoice", "rejected", "other", "failed", "rejected"])

    def test_payment_senders_are_searched_in_backfill_and_incremental_scope(self):
        self.service.settings = replace(SETTINGS, payment_senders=(
            PaymentSender("@pay.example.test", "Example Pay", True),
            PaymentSender("bill@shop.example.test", "Example Shop"),
        ))
        self.enable()
        self.sync()
        self.tick()
        self.transport.profile["historyId"] = "120"
        self.transport.routes[("/history", None)] = response(history("shop-message"))
        self.transport.routes[("/messages", None)] = response(page("shop-message"))
        self.sync()
        queries = [params["q"][0] for params in self.transport.requests("/messages")]
        self.assertEqual(len(queries), 2)
        for query in queries:
            self.assertIn('from:"pay.example.test"', query)
            self.assertIn('from:"bill@shop.example.test"', query)
            self.assertIn(f'from:"{MAILBOX}"', query)
            self.assertIn('from:"bills@vendor.test"', query)
            self.assertIn('from:"spouse@example.test"', query)

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
            with self.assertRaisesRegex(GmailError, "^budget_receipt_gmail_busy$"):
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
                         ("backfill", None, "100", 1))
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
        queued = self.service.schedule(OWNER)
        upgraded = self.service.schedule(OWNER, force_full_resync=True)
        self.assertEqual(upgraded["version"], queued["version"])
        state = self.store.state(OWNER)
        self.assertEqual(self.transport.calls, calls)
        self.assertEqual((queued["ok"], queued["sync_phase"], queued["version"]), (True, "idle", state.version))
        self.assertEqual((state.run_requested, state.force_full_resync, state.next_sync_at), (1, 1, self.clock.now))
        kept = self.service.schedule(OWNER, force_full_resync=False)
        self.assertEqual(kept["version"], queued["version"])
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
        self.assertEqual(self.service.schedule(OWNER)["version"], state.version)
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

    def test_accepted_retry_survives_restart_with_attention_latched(self):
        self.enable()
        self.transport.routes[("/profile", None)] = response({}, 401)
        with self.assertRaisesRegex(GmailError, "authorization"):
            self.sync()
        state = self.store.state(OWNER)
        self.assertEqual(state.attention, "authorization")
        self.clock.now = state.backoff_until
        self.service.schedule(OWNER)

        with mock.patch.object(self.processor, "ready", side_effect=RuntimeError("synthetic restart")):
            with self.assertRaisesRegex(RuntimeError, "synthetic restart"):
                self.sync(poll=True)
        state = self.store.state(OWNER)
        self.assertEqual((state.attention, state.run_requested), ("authorization", 1))

        self.tick(60)
        del self.transport.routes[("/profile", None)]
        client = GmailClient(self.transport, monotonic=self.clock.monotonic, sleep=self.clock.sleep)
        restarted = GmailSync(SETTINGS, self.store, client, self.processor, self.path)
        self.assertTrue(restarted.sync(OWNER, poll=True)["ok"])
        state = self.store.state(OWNER)
        self.assertEqual((state.attention, state.run_requested), (None, 0))

    def test_parse_failed_reasons_accumulate_across_batch_and_reset_on_new_run(self):
        self.enable()
        self.assertEqual(self.store.status(OWNER)["parse_failed_reasons"], {})

        reasons_by_id = {"mail-1": "pdf_unsupported", "mail-2": "pdf_unsupported", "mail-3": "message_defects"}

        def fail_with_reasons(run, message_id, message_hmac):
            self.processor.processed.append(message_id)
            run.message(message_id)
            return PendingMessage(message_hmac, self.clock.wall(), "direct_allowlist", "parse_failed",
                                  parse_failed_reason=reasons_by_id[message_id])

        self.transport.routes[("/messages", None)] = response(page("mail-1", "mail-2", "mail-3"))
        with mock.patch.object(sync_module, "MAX_SYNC_MESSAGES_PER_RUN", 1), \
                mock.patch.object(self.processor, "process", side_effect=fail_with_reasons):
            self.assertTrue(self.sync()["has_more"])
            self.assertEqual(self.store.status(OWNER)["parse_failed_reasons"], {"pdf_unsupported": 1})
            self.tick()
            self.assertTrue(self.sync()["has_more"])
            self.assertEqual(self.store.status(OWNER)["parse_failed_reasons"], {"pdf_unsupported": 2})
            self.tick()
            self.assertFalse(self.sync()["has_more"])
        # Counts persist after the batch returns to idle.
        self.assertEqual(self.store.status(OWNER)["parse_failed_reasons"],
                         {"pdf_unsupported": 2, "message_defects": 1})

        # Empty incremental poll preserves the prior batch counts.
        self.tick()
        self.transport.profile["historyId"] = "110"
        self.transport.routes[("/history", None)] = response(history())
        self.sync()
        self.assertEqual(self.store.status(OWNER)["parse_failed_reasons"],
                         {"pdf_unsupported": 2, "message_defects": 1})

        # Incremental poll processing a new failing message replaces prior batch counts.
        self.tick()
        self.transport.profile["historyId"] = "120"
        self.transport.routes[("/history", None)] = response(history("mail-4"))
        self.transport.routes[("/messages", None)] = response(page("mail-4"))
        reasons_by_id["mail-4"] = "part_defects"
        with mock.patch.object(self.processor, "process", side_effect=fail_with_reasons):
            self.sync()
        self.assertEqual(self.store.status(OWNER)["parse_failed_reasons"], {"part_defects": 1})

        # Incremental poll processing an imported message clears prior failure counts.
        self.tick()
        self.transport.profile["historyId"] = "130"
        self.transport.routes[("/history", None)] = response(history("mail-5"))
        self.transport.routes[("/messages", None)] = response(page("mail-5"))
        self.sync()
        self.assertEqual(self.store.status(OWNER)["parse_failed_reasons"], {})

        # force_full_resync clears prior counts when a new recovery generation starts, and transition resets them too.
        self.assertEqual(self.store.commit(OWNER, 1, {}, (
            PendingMessage("e" * 64, NOW, "direct_allowlist", "parse_failed"),
        )), (0, 1, True))
        self.assertEqual(self.store.status(OWNER)["parse_failed_reasons"], {"other": 1})
        self.tick()
        self.transport.routes[("/messages", None)] = response(page())
        self.sync(force_full_resync=True)
        self.assertEqual(self.store.status(OWNER)["parse_failed_reasons"], {})

        self.assertEqual(self.store.commit(OWNER, 1, {}, (
            PendingMessage("f" * 64, NOW, "direct_allowlist", "parse_failed", parse_failed_reason="charset_error"),
        )), (0, 1, True))
        self.assertEqual(self.store.status(OWNER)["parse_failed_reasons"], {"charset_error": 1})
        self.service.transition(OWNER, self.store.state(OWNER).version, "disabled")
        self.assertEqual(self.store.status(OWNER)["parse_failed_reasons"], {})

    def test_retry_parse_failed_deletes_only_parse_failed_and_scans_full_backfill_window(self):
        with self.sessions.begin() as session:
            session.add(db.User(username="other", password_hash="synthetic", role="admin"))
            session.flush()
            session.add(db.BudgetGmailState(
                owner="other", grant_status="active", grant_generation=1, version=1,
                sync_phase="idle", next_sync_at=0, backoff_until=0, failures=0,
                batch_processed_count=0, run_requested=0, force_full_resync=0,
            ))
        self.enable()
        self.transport.routes[("/messages", None)] = response(page("imported-1", "fail-1", "fail-2"))

        def mixed_outcomes(run, message_id, message_hmac):
            self.processor.processed.append(message_id)
            run.message(message_id)
            if message_id == "imported-1":
                run.message(message_id, raw=True)
                return PendingMessage(message_hmac, self.clock.wall(), "direct_allowlist", "imported",
                                      "a" * 64, INVOICE)
            return PendingMessage(message_hmac, self.clock.wall(), "direct_allowlist", "parse_failed",
                                  parse_failed_reason="pdf_unsupported")

        with mock.patch.object(self.processor, "process", side_effect=mixed_outcomes):
            self.sync()

        # Seed every non-parse_failed outcome plus another owner's parse_failed row.
        other_items = (PendingMessage("0" * 64, NOW, "direct_allowlist", "parse_failed",
                                      parse_failed_reason="pdf_unsupported"),)
        self.assertEqual(self.store.commit("other", 1, {}, other_items), (0, 1, True))
        extra_items = (
            PendingMessage("1" * 64, NOW, "direct_allowlist", "skipped_non_receipt"),
            PendingMessage("2" * 64, NOW, "untrusted", "skipped_untrusted_sender"),
            PendingMessage("3" * 64, NOW, "direct_allowlist", "skipped_oversized"),
            PendingMessage("4" * 64, NOW, "direct_allowlist", "skipped_duplicate"),
            PendingMessage("5" * 64, NOW, "direct_allowlist", "deleted"),
        )
        self.assertEqual(self.store.commit(OWNER, 1, {}, extra_items), (0, 5, True))
        receipt_before, = self.rows(db.BudgetReceipt)

        # Advance 15 days (> 7d force_full_resync window, < 30d IMPORT_TTL) so full-window cutoff differs cleanly.
        self.tick(15 * DAY)
        last_sync = self.store.state(OWNER).last_sync_at

        # Flag-less force_full_resync keeps parse_failed rows and uses last_sync - 7d cutoff.
        self.processor.processed.clear()
        self.transport.routes[("/messages", None)] = response(page("imported-1", "fail-1", "fail-2"))
        self.service.schedule(OWNER, force_full_resync=True)
        self.sync(poll=True)
        self.assertEqual(self.processor.processed, [])
        self.assertIn(f"after:{int(last_sync - 7 * DAY)}", self.transport.requests("/messages")[-1]["q"][0])
        owner_outcomes = [row.outcome for row in self.rows(db.BudgetGmailMessage) if row.owner == OWNER]
        self.assertEqual(owner_outcomes.count("parse_failed"), 2)

        # Backoff / cooldown blocks retry_parse_failed without deleting rows.
        with self.assertRaisesRegex(GmailError, "cooldown"):
            self.service.schedule(OWNER, retry_parse_failed=True)
        owner_outcomes = [row.outcome for row in self.rows(db.BudgetGmailMessage) if row.owner == OWNER]
        self.assertEqual(owner_outcomes.count("parse_failed"), 2)

        # After cooldown, retry_parse_failed deletes only OWNER's parse_failed rows and queues full-window scan.
        self.tick(60)
        queued = self.service.schedule(OWNER, retry_parse_failed=True)
        self.assertTrue(queued["ok"])
        self.assertTrue(self.store.retry_full_window(OWNER))
        # Idempotent if called again while already queued.
        again = self.service.schedule(OWNER, retry_parse_failed=True)
        self.assertEqual(again["version"], queued["version"])

        with self.sessions() as session:
            owner_rows = list(session.scalars(
                select(db.BudgetGmailMessage).where(db.BudgetGmailMessage.owner == OWNER)
            ))
            other_rows = list(session.scalars(
                select(db.BudgetGmailMessage).where(db.BudgetGmailMessage.owner == "other")
            ))
        self.assertEqual({row.outcome for row in owner_rows}, {
            "imported", "skipped_non_receipt", "skipped_untrusted_sender",
            "skipped_oversized", "skipped_duplicate", "deleted",
        })
        self.assertEqual(len(other_rows), 1)
        self.assertEqual(other_rows[0].outcome, "parse_failed")
        self.assertEqual([row.id for row in self.rows(db.BudgetReceipt)], [receipt_before.id])

        # Poller runs over the full backfill_days window, re-processes fail-1 and fail-2, and updates history_id.
        self.transport.profile["historyId"] = "150"
        self.transport.routes[("/messages", None)] = response(page("imported-1", "fail-1", "fail-2"))
        self.processor.rfc = None
        synced = self.sync(poll=True)
        self.assertEqual((synced["imported_count"], synced["skipped_count"]), (2, 0))
        self.assertEqual(self.processor.processed, ["fail-1", "fail-2"])
        expected_cutoff = int(self.clock.now - SETTINGS.backfill_days * DAY)
        query = self.transport.requests("/messages")[-1]["q"][0]
        self.assertIn(f"after:{expected_cutoff}", query)
        self.assertNotIn(f"after:{int(last_sync - 7 * DAY)}", query)
        self.assertFalse(self.store.retry_full_window(OWNER))

        state = self.store.state(OWNER)
        self.assertEqual((state.sync_phase, state.history_id, state.pending_history_id,
                          state.force_full_resync, state.run_requested), ("idle", "150", None, 0, 0))
        self.assertEqual(len(self.rows(db.BudgetReceipt)), 3)

        # Incremental sync remains consistent with the new history_id.
        self.tick(SETTINGS.sync_interval_seconds)
        self.transport.profile["historyId"] = "160"
        self.transport.routes[("/history", None)] = response(history("inc-1"))
        self.transport.routes[("/messages", None)] = response(page("inc-1"))
        inc = self.sync(poll=True)
        self.assertEqual(inc["imported_count"], 1)
        self.assertEqual(self.transport.requests("/history")[-1]["startHistoryId"], ["150"])
        self.assertEqual(self.store.state(OWNER).history_id, "160")

    def test_concurrent_retry_parse_failed_during_incremental_404_and_resumed_backfill(self):
        self.enable()
        self.transport.routes[("/messages", None)] = response(page())
        self.sync()
        self.tick(15 * DAY)
        last_sync = self.store.state(OWNER).last_sync_at
        fail_hmac = digest(SETTINGS.hmac_secret, GMAIL_MESSAGE_DOMAIN, "fail-old")
        self.assertEqual(self.store.commit(OWNER, 1, {}, (
            PendingMessage(fail_hmac, NOW - 20 * DAY, "direct_allowlist", "parse_failed",
                           parse_failed_reason="pdf_unsupported"),
        )), (0, 1, True))

        # 1. Concurrent retry queued AFTER /history returns 404 and BEFORE the recovery transaction commits.
        history_returned_404 = False

        def history_404(_params):
            nonlocal history_returned_404
            history_returned_404 = True
            return response({}, 404)

        real_commit = self.store.commit
        injected = False

        def commit_with_404_race(owner, generation, changes, pending=(), **kwargs):
            nonlocal injected
            if kwargs.get("starting_recovery") and not injected:
                self.assertTrue(history_returned_404)
                injected = True
                self.service.schedule(OWNER, retry_parse_failed=True)
            return real_commit(owner, generation, changes, pending, **kwargs)

        self.transport.routes[("/history", None)] = history_404
        self.transport.routes[("/messages", None)] = response(page("fail-old", "new-m1"))
        self.transport.profile["historyId"] = "200"
        schedule_cutoff = int(self.clock.now - SETTINGS.backfill_days * DAY)
        with mock.patch.object(sync_module, "MAX_SYNC_MESSAGES_PER_RUN", 1), \
                mock.patch.object(self.store, "commit", side_effect=commit_with_404_race):
            partial = self.sync()
        self.assertTrue(injected)
        self.assertTrue(partial["has_more"])
        self.assertEqual(partial["sync_phase"], "full_resync")
        self.assertTrue(self.store.retry_full_window(OWNER))
        query = self.transport.requests("/messages")[-1]["q"][0]
        self.assertIn(f"after:{schedule_cutoff}", query)
        self.assertNotIn(f"after:{int(last_sync - 7 * DAY)}", query)

        # Even if force_full_resync=True is requested while the 365d window is still incomplete, schedule_cutoff stays active.
        self.tick(60)
        finished = self.sync(force_full_resync=True)
        self.assertFalse(finished["has_more"])
        self.assertEqual(finished["sync_phase"], "idle")
        self.assertFalse(self.store.retry_full_window(OWNER))
        self.assertIn(f"after:{schedule_cutoff}", self.transport.requests("/messages")[-1]["q"][0])
        self.assertEqual(self.processor.processed, ["fail-old", "new-m1"])

        # 2. Re-queuing retry_parse_failed while a previous retry is paused in "running" state keeps the earlier cutoff.
        self.tick(15 * DAY)
        first_cutoff = int(self.clock.now - SETTINGS.backfill_days * DAY)
        self.service.schedule(OWNER, retry_parse_failed=True)
        self.transport.routes[("/messages", None)] = response(page("p1", "p2"))
        with mock.patch.object(sync_module, "MAX_SYNC_MESSAGES_PER_RUN", 1):
            paused = self.sync(poll=True)
        self.assertTrue(paused["has_more"])
        self.assertTrue(self.store.retry_full_window(OWNER))
        self.tick(60)
        self.service.schedule(OWNER, retry_parse_failed=True)
        resumed = self.sync(poll=True)
        self.assertFalse(resumed["has_more"])
        self.assertFalse(self.store.retry_full_window(OWNER))
        self.assertIn(f"after:{first_cutoff}", self.transport.requests("/messages")[-1]["q"][0])

        # 3. Concurrent retry arriving AFTER a 7-day 404 recovery starts is NOT cleared by the 7-day scan.
        self.tick(15 * DAY)
        seven_day_last_sync = self.store.state(OWNER).last_sync_at
        self.transport.routes[("/history", None)] = response({}, 404)
        self.transport.profile["historyId"] = "210"
        calls = []

        def messages_triggering_retry(params):
            calls.append(params["q"][0])
            if len(calls) == 1:
                self.service.schedule(OWNER, retry_parse_failed=True)
            return response(page())

        self.transport.routes[("/messages", None)] = messages_triggering_retry
        self.sync()
        self.assertEqual(len(calls), 2)
        self.assertIn(f"after:{int(seven_day_last_sync - 7 * DAY)}", calls[0])
        self.assertIn(f"after:{int(self.clock.now - SETTINGS.backfill_days * DAY)}", calls[1])
        self.assertFalse(self.store.retry_full_window(OWNER))

        # 4. Concurrent retry arriving during a successful (non-404) incremental run triggers the 365d scan next.
        self.tick(15 * DAY)
        self.transport.profile["historyId"] = "230"

        def clean_history_with_concurrent_retry(_params):
            self.service.schedule(OWNER, retry_parse_failed=True)
            return response(history())

        self.transport.routes[("/history", None)] = clean_history_with_concurrent_retry
        self.transport.routes[("/messages", None)] = response(page())
        self.sync()
        self.assertIn(f"after:{int(self.clock.now - SETTINGS.backfill_days * DAY)}",
                      self.transport.requests("/messages")[-1]["q"][0])
        self.assertFalse(self.store.retry_full_window(OWNER))

    def test_concurrent_retry_during_first_backfill_does_not_trigger_extra_scan_or_reset_reasons(self):
        self.enable()
        self.transport.routes[("/messages", None)] = response(page("msg-pdf"))

        def fail_pdf(run, message_id, message_hmac):
            self.processor.processed.append(message_id)
            run.message(message_id)
            return PendingMessage(message_hmac, self.clock.wall(), "direct_allowlist", "parse_failed",
                                  parse_failed_reason="pdf_unsupported")

        real_commit = self.store.commit
        injected = False

        def commit_during_first_backfill(owner, generation, changes, pending=(), **kwargs):
            nonlocal injected
            if kwargs.get("starting_recovery") and not injected:
                injected = True
                # Worker has already read force=False for the initial backfill and now commits recovery.
                self.assertEqual(changes["sync_phase"], "backfill")
                self.service.schedule(OWNER, retry_parse_failed=True)
            return real_commit(owner, generation, changes, pending, **kwargs)

        with mock.patch.object(self.store, "commit", side_effect=commit_during_first_backfill), \
                mock.patch.object(self.processor, "process", side_effect=fail_pdf):
            result = self.sync()
        self.assertTrue(injected)
        self.assertFalse(result["has_more"])
        self.assertEqual(len(self.transport.requests("/messages")), 1)
        self.assertEqual(self.processor.processed, ["msg-pdf"])
        self.assertEqual(self.store.status(OWNER)["parse_failed_reasons"], {"pdf_unsupported": 1})
        state = self.store.state(OWNER)
        self.assertEqual((state.sync_phase, state.history_id, state.force_full_resync, state.run_requested),
                         ("idle", "100", 0, 0))
        self.assertFalse(self.store.retry_full_window(OWNER))

    def test_requeued_retry_while_running_keeps_earlier_cutoff_for_delete_and_scan(self):
        self.enable()
        self.transport.routes[("/messages", None)] = response(page())
        self.sync()
        self.tick(60)
        t_sched = self.clock.now
        cutoff_1 = int(t_sched - SETTINGS.backfill_days * DAY)
        dated_ids = {
            "outside-old": cutoff_1 - 1,
            "edge-1": cutoff_1 + 1,
            "edge-2-later": cutoff_1 + 10,
            "early-page": t_sched - 5 * DAY,
        }
        hmacs = {mid: digest(SETTINGS.hmac_secret, GMAIL_MESSAGE_DOMAIN, mid) for mid in dated_ids}
        self.assertEqual(self.store.commit(OWNER, 1, {}, (
            PendingMessage(hmacs["outside-old"], dated_ids["outside-old"], "direct_allowlist", "parse_failed",
                           parse_failed_reason="pdf_unsupported"),
            PendingMessage(hmacs["edge-1"], dated_ids["edge-1"], "direct_allowlist", "parse_failed",
                           parse_failed_reason="pdf_unsupported"),
            PendingMessage(hmacs["early-page"], dated_ids["early-page"], "direct_allowlist", "parse_failed",
                           parse_failed_reason="pdf_unsupported"),
        )), (0, 3, True))

        # First retry deletes edge-1 (1s inside) and early-page, preserving outside-old.
        self.service.schedule(OWNER, retry_parse_failed=True)
        with self.sessions() as session:
            self.assertIsNotNone(session.get(db.BudgetGmailMessage, (OWNER, hmacs["outside-old"])))
            self.assertIsNone(session.get(db.BudgetGmailMessage, (OWNER, hmacs["edge-1"])))
            self.assertIsNone(session.get(db.BudgetGmailMessage, (OWNER, hmacs["early-page"])))

        # First poll pauses after early-page before reaching edge-1.
        def messages_by_date(params):
            query = params["q"][0]
            after_ts = int(next(part for part in query.split() if part.startswith("after:")).split(":", 1)[1])
            matched = [mid for mid in ("early-page", "edge-1", "edge-2-later", "outside-old")
                       if dated_ids[mid] >= after_ts]
            return response(page(*matched))

        self.transport.routes[("/messages", None)] = messages_by_date
        with mock.patch.object(sync_module, "MAX_SYNC_MESSAGES_PER_RUN", 1):
            paused = self.sync(poll=True)
        self.assertTrue(paused["has_more"])
        self.assertEqual(self.processor.processed, ["early-page"])

        # Seed edge-2-later (10s inside cutoff_1, 50s older than cutoff_1 + 60) and re-queue retry 60s later.
        self.assertEqual(self.store.commit(OWNER, 1, {}, (
            PendingMessage(hmacs["edge-2-later"], dated_ids["edge-2-later"], "direct_allowlist", "parse_failed",
                           parse_failed_reason="pdf_unsupported"),
        )), (0, 1, True))
        self.tick(60)
        self.service.schedule(OWNER, retry_parse_failed=True)
        with self.sessions() as session:
            # Second schedule delete also uses min(cutoff_1, cutoff_2) = cutoff_1.
            self.assertIsNotNone(session.get(db.BudgetGmailMessage, (OWNER, hmacs["outside-old"])))
            self.assertIsNone(session.get(db.BudgetGmailMessage, (OWNER, hmacs["edge-2-later"])))

        # Next poll uses cutoff_1 (not now - 365d), re-fetching and re-processing both edge-1 and edge-2-later.
        resumed = self.sync(poll=True)
        self.assertFalse(resumed["has_more"])
        self.assertFalse(self.store.retry_full_window(OWNER))
        self.assertEqual(self.processor.processed, ["early-page", "edge-1", "edge-2-later"])
        last_query = self.transport.requests("/messages")[-1]["q"][0]
        self.assertIn(f"after:{cutoff_1}", last_query)
        self.assertNotIn(f"after:{int(self.clock.now - SETTINGS.backfill_days * DAY)}", last_query)

    def test_forced_recovery_resets_reasons_even_if_first_list_returns_503(self):
        self.enable()
        self.transport.routes[("/messages", None)] = response(page("fail-mime"))

        def fail_mime(run, message_id, message_hmac):
            self.processor.processed.append(message_id)
            run.message(message_id)
            return PendingMessage(message_hmac, self.clock.wall(), "direct_allowlist", "parse_failed",
                                  parse_failed_reason="mime_error")

        with mock.patch.object(self.processor, "process", side_effect=fail_mime):
            self.sync()
        self.assertEqual(self.store.status(OWNER)["parse_failed_reasons"], {"mime_error": 1})

        # Forced recovery clears prior mime_error at generation start; first list request returns 503.
        self.tick(60)
        self.transport.routes[("/messages", None)] = response({}, 503)
        with self.assertRaisesRegex(GmailError, "provider_http"):
            self.sync(force_full_resync=True)
        self.assertEqual(self.store.status(OWNER)["parse_failed_reasons"], {})

        # Next poll resumes recovery and imports a new message; old mime_error never returns.
        self.clock.now = self.store.state(OWNER).next_sync_at
        self.transport.routes[("/messages", None)] = response(page("new-ok"))
        resumed = self.sync(poll=True)
        self.assertEqual((resumed["imported_count"], resumed["has_more"]), (1, False))
        self.assertEqual(self.processor.processed, ["fail-mime", "new-ok"])
        self.assertEqual(self.store.status(OWNER)["parse_failed_reasons"], {})

    def test_paused_retry_force_resumed_with_import_preserves_failure_counts(self):
        self.enable()
        self.transport.routes[("/messages", None)] = response(page())
        self.sync()
        self.tick(60)
        fail_id = "fail-pdf"
        fail_hmac = digest(SETTINGS.hmac_secret, GMAIL_MESSAGE_DOMAIN, fail_id)
        self.assertEqual(self.store.commit(OWNER, 1, {}, (
            PendingMessage(fail_hmac, self.clock.now - 5 * DAY, "direct_allowlist", "parse_failed",
                           parse_failed_reason="pdf_unsupported"),
        )), (0, 1, True))
        self.service.schedule(OWNER, retry_parse_failed=True)

        def process_mixed(run, message_id, message_hmac):
            self.processor.processed.append(message_id)
            run.message(message_id)
            if message_id == fail_id:
                return PendingMessage(message_hmac, self.clock.wall(), "direct_allowlist", "parse_failed",
                                      parse_failed_reason="pdf_unsupported")
            run.message(message_id, raw=True)
            return PendingMessage(message_hmac, self.clock.wall(), "direct_allowlist", "imported",
                                  None, INVOICE)

        self.transport.routes[("/messages", None)] = response(page(fail_id, "new-ok"))
        with mock.patch.object(sync_module, "MAX_SYNC_MESSAGES_PER_RUN", 1), \
                mock.patch.object(self.processor, "process", side_effect=process_mixed):
            paused = self.sync(poll=True)
        self.assertTrue(paused["has_more"])
        self.assertTrue(self.store.retry_full_window(OWNER))
        self.assertEqual(self.store.status(OWNER)["parse_failed_reasons"], {"pdf_unsupported": 1})

        # Force-resuming the still-running retry imports new-ok without erasing pdf_unsupported.
        self.tick(60)
        with mock.patch.object(self.processor, "process", side_effect=process_mixed):
            finished = self.sync(force_full_resync=True)
        self.assertEqual((finished["imported_count"], finished["has_more"]), (1, False))
        self.assertFalse(self.store.retry_full_window(OWNER))
        self.assertEqual(self.store.status(OWNER)["parse_failed_reasons"], {"pdf_unsupported": 1})
        with self.sessions() as session:
            self.assertEqual(session.get(db.BudgetGmailMessage, (OWNER, fail_hmac)).outcome, "parse_failed")

    def test_retry_parse_failed_deletes_only_rows_inside_scan_window_and_reuses_schedule_cutoff(self):
        self.enable()
        self.transport.routes[("/messages", None)] = response(page())
        self.sync()
        self.tick(60)
        t_sched = self.clock.now
        old_hmac = "1" * 64
        edge_id = "edge-msg"
        edge_hmac = digest(SETTINGS.hmac_secret, GMAIL_MESSAGE_DOMAIN, edge_id)
        edge_date = t_sched - SETTINGS.backfill_days * DAY + 1
        self.assertEqual(self.store.commit(OWNER, 1, {}, (
            PendingMessage(old_hmac, t_sched - SETTINGS.backfill_days * DAY - 1,
                           "direct_allowlist", "parse_failed", parse_failed_reason="pdf_unsupported"),
            PendingMessage(edge_hmac, edge_date,
                           "direct_allowlist", "parse_failed", parse_failed_reason="pdf_unsupported"),
        )), (0, 2, True))

        self.service.schedule(OWNER, retry_parse_failed=True)
        with self.sessions() as session:
            self.assertIsNotNone(session.get(db.BudgetGmailMessage, (OWNER, old_hmac)))
            self.assertIsNone(session.get(db.BudgetGmailMessage, (OWNER, edge_hmac)))

        # Polling 60s later still reuses the schedule-time cutoff so edge_date (1s inside) is re-fetched and imported.
        self.tick(60)
        expected_after = int(t_sched - SETTINGS.backfill_days * DAY)

        def messages_filtered_by_after(params):
            query = params["q"][0]
            after_token = next(part for part in query.split() if part.startswith("after:"))
            after_ts = int(after_token.split(":", 1)[1])
            return response(page(edge_id) if after_ts <= edge_date else page())

        self.transport.routes[("/messages", None)] = messages_filtered_by_after
        synced = self.sync(poll=True)
        self.assertEqual((synced["imported_count"], synced["skipped_count"]), (1, 0))
        self.assertEqual(self.processor.processed, [edge_id])
        self.assertEqual(len(self.rows(db.BudgetReceipt)), 1)
        last_query = self.transport.requests("/messages")[-1]["q"][0]
        self.assertIn(f"after:{expected_after}", last_query)
        self.assertNotIn(f"after:{int(self.clock.now - SETTINGS.backfill_days * DAY)}", last_query)

        # Custom backfill_days also bounds the deletion window.
        self.service.settings = replace(SETTINGS, backfill_days=30)
        self.tick(60)
        outside_30 = "4" * 64
        inside_30 = "5" * 64
        self.assertEqual(self.store.commit(OWNER, 1, {}, (
            PendingMessage(outside_30, self.clock.now - 31 * DAY,
                           "direct_allowlist", "parse_failed", parse_failed_reason="pdf_unsupported"),
            PendingMessage(inside_30, self.clock.now - 29 * DAY,
                           "direct_allowlist", "parse_failed", parse_failed_reason="pdf_unsupported"),
        )), (0, 2, True))
        self.service.schedule(OWNER, retry_parse_failed=True)
        with self.sessions() as session:
            self.assertIsNotNone(session.get(db.BudgetGmailMessage, (OWNER, outside_30)))
            self.assertIsNone(session.get(db.BudgetGmailMessage, (OWNER, inside_30)))

    def test_parse_failed_reasons_are_validated_against_enum_and_map_unknown_to_other(self):
        self.enable()
        valid_items = tuple(
            PendingMessage(f"{index:064x}", NOW, "direct_allowlist", "parse_failed", parse_failed_reason=reason)
            for index, reason in enumerate(PARSE_FAILED_REASONS, start=1)
        )
        invalid_reasons = (None, "", "unknown", "raw-sensitive-error", 123)
        invalid_items = tuple(
            PendingMessage(f"{index:064x}", NOW, "direct_allowlist", "parse_failed", parse_failed_reason=reason)
            for index, reason in enumerate(invalid_reasons, start=100)
        )
        self.assertEqual(self.store.commit(OWNER, 1, {}, valid_items + invalid_items),
                         (0, len(valid_items) + len(invalid_items), True))

        expected = {reason: 1 for reason in PARSE_FAILED_REASONS}
        expected["other"] += len(invalid_reasons)
        status = self.store.status(OWNER)
        self.assertEqual(status["parse_failed_reasons"], expected)
        api_contract.BudgetReceiptGmailStatus.model_validate({
            "enabled": True, "configured": True, **status,
            "google_revoke_url": "https://myaccount.google.com/linkedapps",
            "google_revoke_affects_calendar_sync": True,
        })

        # Unnormalized legacy values already stored in AppMeta also normalize on read and merge.
        with self.sessions.begin() as session:
            session.get(db.AppMeta, _reasons_key(OWNER)).value = json.dumps({"unknown": 2, "custom": 1})
        self.assertEqual(self.store.status(OWNER)["parse_failed_reasons"], {"other": 3})
        self.assertEqual(self.store.commit(OWNER, 1, {}, (
            PendingMessage("f" * 64, NOW, "direct_allowlist", "parse_failed", parse_failed_reason="arbitrary"),
        )), (0, 1, True))
        with self.sessions() as session:
            self.assertEqual(json.loads(session.get(db.AppMeta, _reasons_key(OWNER)).value), {"other": 4})

        # End-to-end processor -> store -> SQLite persistence keeps raw unknown rejection detail out of logs & DB.
        import base64
        from email.message import EmailMessage

        raw_secret = "raw-sensitive-error"
        msg = EmailMessage()
        msg["From"] = "bills@vendor.test"
        msg["Authentication-Results"] = "mx.google.com; dkim=pass header.d=vendor.test; dmarc=pass header.from=vendor.test"
        msg.set_content("Synthetic bill")
        payload = msg.as_bytes()
        self.transport.routes[("/messages", None)] = response(page("msg-raw-err"))
        self.transport.routes[("/messages/msg-raw-err", None)] = lambda params: response(
            {"id": "msg-raw-err", "sizeEstimate": len(payload), "internalDate": "1795566945123",
             "payload": {"headers": [{"name": k, "value": v} for k, v in msg.raw_items()]}}
            if params["format"] == ["metadata"]
            else {"id": "msg-raw-err", "raw": base64.urlsafe_b64encode(payload).decode().rstrip("=")}
        )
        worker = mock.Mock(ready=mock.Mock(return_value=True),
                           parse=mock.Mock(side_effect=gmail_message._Rejected(raw_secret)))
        real_service = GmailSync(SETTINGS, self.store, self.client,
                                 gmail_message.GmailMessageProcessor(SETTINGS, worker), self.path, jitter=lambda *_: 0)
        self.tick(60)
        with self.assertLogs("hestia.budget.gmail_message", level="WARNING") as logs:
            real_service.sync(OWNER, force_full_resync=True)
        self.assertEqual(logs.output, ["WARNING:hestia.budget.gmail_message:gmail parse_failed reason=other"])
        self.assertNotIn(raw_secret, logs.output[0])
        self.assertEqual(self.store.status(OWNER)["parse_failed_reasons"], {"other": 1})
        with self.sessions() as session:
            session.connection().exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        self.assertNotIn(raw_secret.encode(), self.path.read_bytes())
