"""Calendar transactions persist successful writes and release locks on every exit."""

import tempfile
import unittest

from sqlalchemy import event as sqlalchemy_event

from hestia import db
from hestia.calendar_sync.store import SyncStore


class CalendarTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        _, self.sessions = db.init_db(self.temp.name + "/sync.db")
        self.store = SyncStore(self.sessions)

    def tearDown(self):
        db.reset_engine_cache()
        self.temp.cleanup()

    def assert_group_persisted(self, identifier):
        with self.sessions() as session:
            self.assertIsNotNone(session.get(db.CalendarGroup, identifier))

    def test_successful_transaction_commits_before_releasing_session(self):
        with self.store.transaction() as session:
            session.add(db.CalendarGroup(id="committed", enabled=True))
        self.assert_group_persisted("committed")

    def test_body_failure_rolls_back_flushed_writes_and_releases_write_lock(self):
        for error in (RuntimeError, KeyboardInterrupt, GeneratorExit):
            with self.subTest(error=error.__name__):
                identifier = error.__name__
                with self.assertRaises(error):
                    with self.store.transaction() as session:
                        session.add(db.CalendarGroup(id=identifier, enabled=True))
                        session.flush()
                        raise error("Interrupted calendar transaction")
                with self.sessions() as session:
                    self.assertIsNone(session.get(db.CalendarGroup, identifier))
                with self.store.transaction() as session:
                    session.add(db.CalendarGroup(id=identifier + "-retry", enabled=True))
                self.assert_group_persisted(identifier + "-retry")

    def test_commit_failure_rolls_back_flushed_writes_and_releases_write_lock(self):
        def fail_commit(session):
            raise RuntimeError("Commit interrupted")

        sqlalchemy_event.listen(self.sessions, "before_commit", fail_commit)
        try:
            with self.assertRaisesRegex(RuntimeError, "Commit interrupted"):
                with self.store.transaction() as session:
                    session.add(db.CalendarGroup(id="discarded", enabled=True))
                    session.flush()
        finally:
            sqlalchemy_event.remove(self.sessions, "before_commit", fail_commit)
        with self.sessions() as session:
            self.assertIsNone(session.get(db.CalendarGroup, "discarded"))
        with self.store.transaction() as session:
            session.add(db.CalendarGroup(id="retry", enabled=True))
        self.assert_group_persisted("retry")
