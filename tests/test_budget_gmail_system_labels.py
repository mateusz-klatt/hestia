"""Mailbox labels gate receipt processing at both Gmail message reads."""
from dataclasses import replace
from unittest import TestCase
from unittest.mock import Mock, call

from hestia.budget.gmail_client import GmailError
from hestia.budget.gmail_message import GmailMessageProcessor
from hestia.budget.gmail_sync import _history_messages
from tests.test_budget_gmail_message import GMAIL_HASH, MERCHANT, MESSAGE_ID, _email, _metadata, _raw
from tests.test_budget_gmail_sync import SETTINGS


class GmailSystemLabelTests(TestCase):
    def setUp(self):
        settings = replace(SETTINGS, sender_allowlist=(MERCHANT,))
        self.worker = Mock()
        self.worker.parse.side_effect = lambda kind, value: value.decode()
        self.processor = GmailMessageProcessor(settings, self.worker)

    def process(self, *responses):
        self.run = Mock()
        self.run.message.side_effect = responses
        return self.processor.process(self.run, MESSAGE_ID, GMAIL_HASH)

    def test_spam_and_trash_are_ignored_and_a_restored_message_can_import(self):
        message = _email()
        for label in ("SPAM", "TRASH"):
            with self.subTest(label=label):
                self.assertIsNone(self.process(_metadata(message) | {"labelIds": ["Label_7", label]}))
                self.run.message.assert_called_once_with(MESSAGE_ID)
        self.worker.parse.assert_not_called()
        for labels in ({}, {"labelIds": []}, {"labelIds": ["INBOX", "Label_7"]}):
            with self.subTest(labels=labels):
                receipt = self.process(_metadata(message) | labels, _raw(message) | labels)
                self.assertEqual(receipt.outcome, "imported")

    def test_gmail_can_move_a_message_after_metadata_and_before_raw(self):
        for label in ("SPAM", "TRASH"):
            with self.subTest(label=label):
                self.assertIsNone(self.process(_metadata(), {"id": MESSAGE_ID, "labelIds": [label]}))
                self.assertEqual(self.run.message.call_args_list, [call(MESSAGE_ID), call(MESSAGE_ID, raw=True)])
        self.worker.parse.assert_not_called()

    def test_only_messages_restored_from_spam_or_trash_are_retried_on_label_removal(self):
        removed = [
            {"message": {"id": "restored-mail"}, "labelIds": ["SPAM"]},
            {"message": {"id": "restored-trash"}, "labelIds": ["TRASH", "Label_7"]},
            {"message": {"id": "marked-read"}, "labelIds": ["UNREAD"]},
        ]
        ids = _history_messages({"history": [{"labelsRemoved": removed}]})
        self.assertEqual(ids, {"restored-mail", "restored-trash"})

    def test_invalid_label_metadata_never_reaches_the_parser(self):
        for invalid in (None, "SPAM", ["INBOX", None], [""], [" TRASH"]):
            with self.subTest(labels=invalid):
                with self.assertRaisesRegex(GmailError, "invalid_response"):
                    self.process(_metadata() | {"labelIds": invalid})
                self.run.message.assert_called_once_with(MESSAGE_ID)
                with self.assertRaisesRegex(GmailError, "invalid_response"):
                    self.process(_metadata(), _raw(_email()) | {"labelIds": invalid})
        self.worker.parse.assert_not_called()
