"""Synthetic unit tests for pure private consent-renewal reminder evaluation."""
from __future__ import annotations

from datetime import datetime, timezone
import math
import random
import unittest

from hestia.budget.renewal_reminders import (
    ATTENTION_MILESTONE,
    DEFAULT_LEAD_DAYS,
    EXPIRED_MILESTONE,
    MAX_CONNECTIONS,
    MAX_LEAD_DAYS,
    MAX_NOTIFIED,
    MAX_TIMESTAMP,
    PRIVATE_DESTINATIONS,
    RULE_VERSION,
    SECONDS_PER_DAY,
    STATUSES,
    ConsentRenewalReminder,
    ConsentRenewalTarget,
    RenewalInputError,
    RenewalLimitError,
    evaluate_renewal_reminders,
)

NOW = 1_800_000_000


def _target(connection_id="conn-1", valid_until=NOW + 30 * SECONDS_PER_DAY, **changes):
    values = {
        "owner": "owner",
        "connection_id": connection_id,
        "bank_name": "Synthetic Bank",
        "valid_until": valid_until,
        "destination_kind": "owner_inbox",
        "destination_target": "inbox:owner-private",
        "enabled": True,
        "paused": False,
        "attention": None,
        "notified_milestones": frozenset(),
    }
    values.update(changes)
    return ConsentRenewalTarget(**values)


class RenewalRemindersTests(unittest.TestCase):
    def test_all_five_statuses_milestones_attention_paused_and_no_180_day_assumption(self):
        targets = [
            _target("c-unknown", valid_until=None),
            _target("c-healthy-short-30d", valid_until=NOW + 14 * SECONDS_PER_DAY + 1),
            _target("c-tier-14", valid_until=NOW + 14 * SECONDS_PER_DAY, destination_kind="owner_push"),
            _target("c-tier-7-notified", valid_until=NOW + 7 * SECONDS_PER_DAY,
                    notified_milestones=frozenset({14, 7})),
            _target("c-tier-3", valid_until=NOW + 2 * SECONDS_PER_DAY + 43_200,
                    notified_milestones=frozenset({14, 7}), paused=True),
            _target("c-tier-1", valid_until=NOW + 12 * 3600, destination_kind="owner_email"),
            _target("c-expired-exact", valid_until=NOW),
            _target("c-expired-past", valid_until=NOW - 3600, notified_milestones=frozenset({0})),
            _target("c-attention-early", valid_until=NOW + 60 * SECONDS_PER_DAY, attention="expired_session"),
            _target("c-disabled", valid_until=NOW - 10, enabled=False),
        ]
        results = evaluate_renewal_reminders("owner", NOW, (item for item in targets))
        by_id = {item.connection_id: item for item in results}
        self.assertEqual(set(item.status for item in results), set(STATUSES))
        self.assertNotIn("c-disabled", by_id)
        self.assertEqual([item.connection_id for item in results], [
            "c-attention-early",
            "c-expired-past",
            "c-expired-exact",
            "c-tier-1",
            "c-tier-3",
            "c-tier-7-notified",
            "c-tier-14",
            "c-healthy-short-30d",
            "c-unknown",
        ])

        att = by_id["c-attention-early"]
        self.assertEqual((att.status, att.active_milestone, att.reminder_due, att.attention,
                          att.days_remaining, att.rule_version),
                         ("attention_required", ATTENTION_MILESTONE, True, "expired_session", 60, RULE_VERSION))
        self.assertEqual((by_id["c-expired-exact"].status, by_id["c-expired-exact"].seconds_remaining,
                          by_id["c-expired-exact"].active_milestone, by_id["c-expired-exact"].reminder_due),
                         ("expired", 0, EXPIRED_MILESTONE, True))
        self.assertEqual((by_id["c-expired-past"].status, by_id["c-expired-past"].seconds_remaining,
                          by_id["c-expired-past"].days_remaining, by_id["c-expired-past"].reminder_due),
                         ("expired", -3600, -1, False))
        self.assertEqual((by_id["c-tier-1"].status, by_id["c-tier-1"].active_milestone,
                          by_id["c-tier-1"].days_remaining, by_id["c-tier-1"].reminder_due),
                         ("due_soon", 1, 0, True))
        self.assertEqual((by_id["c-tier-3"].status, by_id["c-tier-3"].active_milestone,
                          by_id["c-tier-3"].reminder_due, by_id["c-tier-3"].paused),
                         ("due_soon", 3, True, True))
        self.assertEqual((by_id["c-tier-7-notified"].status, by_id["c-tier-7-notified"].active_milestone,
                          by_id["c-tier-7-notified"].reminder_due),
                         ("due_soon", 7, False))
        self.assertEqual((by_id["c-tier-14"].status, by_id["c-tier-14"].active_milestone,
                          by_id["c-tier-14"].reminder_due),
                         ("due_soon", 14, True))
        self.assertEqual((by_id["c-healthy-short-30d"].status, by_id["c-healthy-short-30d"].active_milestone,
                          by_id["c-healthy-short-30d"].reminder_due),
                         ("healthy", None, False))
        self.assertEqual((by_id["c-unknown"].status, by_id["c-unknown"].valid_until,
                          by_id["c-unknown"].seconds_remaining, by_id["c-unknown"].days_remaining,
                          by_id["c-unknown"].active_milestone, by_id["c-unknown"].reminder_due),
                         ("unknown_expiry", None, None, None, None, False))

        # State transitions: recording "attention" does not suppress a later expiry reminder (marker 0),
        # and UTC epoch arithmetic is invariant across local DST dates.
        dst_spring = int(datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc).timestamp())
        notified = set()
        step1 = evaluate_renewal_reminders(
            "owner", dst_spring, [_target("c-flow", valid_until=dst_spring + 14 * SECONDS_PER_DAY)]
        )[0]
        self.assertEqual((step1.status, step1.active_milestone, step1.reminder_due), ("due_soon", 14, True))
        notified.add(step1.active_milestone)
        step2 = evaluate_renewal_reminders(
            "owner", dst_spring + 3600,
            [_target("c-flow", valid_until=dst_spring + 14 * SECONDS_PER_DAY,
                     attention="authorization", notified_milestones=frozenset(notified))]
        )[0]
        self.assertEqual((step2.status, step2.active_milestone, step2.reminder_due),
                         ("attention_required", ATTENTION_MILESTONE, True))
        notified.add(step2.active_milestone)
        step3 = evaluate_renewal_reminders(
            "owner", dst_spring + 14 * SECONDS_PER_DAY,
            [_target("c-flow", valid_until=dst_spring + 14 * SECONDS_PER_DAY,
                     attention=None, notified_milestones=frozenset(notified))]
        )[0]
        self.assertEqual((step3.status, step3.active_milestone, step3.reminder_due),
                         ("expired", EXPIRED_MILESTONE, True))
        notified.add(step3.active_milestone)
        step4 = evaluate_renewal_reminders(
            "owner", dst_spring + 15 * SECONDS_PER_DAY,
            [_target("c-flow", valid_until=dst_spring + 14 * SECONDS_PER_DAY,
                     attention=None, notified_milestones=frozenset(notified))]
        )[0]
        self.assertEqual((step4.status, step4.active_milestone, step4.reminder_due),
                         ("expired", EXPIRED_MILESTONE, False))

        # Fractional remaining time floors conservatively so expiry is never understated.
        sub_second = evaluate_renewal_reminders("owner", NOW + 0.4, [_target("c-frac", valid_until=NOW + 1.0)])
        self.assertEqual((sub_second[0].seconds_remaining, sub_second[0].days_remaining, sub_second[0].status),
                         (0, 0, "expired"))
        # Exact upper timestamp bound preserves integer arithmetic without float conversion.
        at_bound = evaluate_renewal_reminders("owner", MAX_TIMESTAMP - 1, [_target("c-max", valid_until=MAX_TIMESTAMP)])
        self.assertIs(type(at_bound[0].valid_until), int)
        self.assertEqual((at_bound[0].seconds_remaining, at_bound[0].status), (1, "due_soon"))

        # Optional destination_target=None permits evaluation-only callers without storing PII targets.
        no_target = evaluate_renewal_reminders("owner", NOW, [_target("c-none-target", destination_target=None)])[0]
        self.assertIsNone(no_target.destination_target)
        self.assertEqual(no_target.destination_kind, "owner_inbox")

    def test_custom_lead_days_permutation_invariance_and_tiebreaking(self):
        targets = [
            _target("conn-b", valid_until=NOW + 5 * SECONDS_PER_DAY),
            _target("conn-a", valid_until=NOW + 5 * SECONDS_PER_DAY),
            _target("conn-c", valid_until=NOW + 20 * SECONDS_PER_DAY),
            _target("conn-u2", valid_until=None),
            _target("conn-u1", valid_until=None),
        ]
        baseline = evaluate_renewal_reminders("owner", NOW, targets, lead_days=(20, 5))
        self.assertEqual([(item.connection_id, item.active_milestone) for item in baseline], [
            ("conn-a", 5),
            ("conn-b", 5),
            ("conn-c", 20),
            ("conn-u1", None),
            ("conn-u2", None),
        ])
        rng = random.Random(20261121)
        for _ in range(25):
            perm = list(targets)
            rng.shuffle(perm)
            self.assertEqual(evaluate_renewal_reminders("owner", NOW, perm, lead_days=(5, 20)), baseline)

    def test_shared_calendars_invalid_inputs_and_caps_fail_closed(self):
        self.assertEqual(PRIVATE_DESTINATIONS, frozenset({"owner_inbox", "owner_push", "owner_email"}))
        self.assertEqual(DEFAULT_LEAD_DAYS, (14, 7, 3, 1))
        bad_calls = [
            lambda: evaluate_renewal_reminders(" ", NOW, []),
            lambda: evaluate_renewal_reminders(" " * 1025, NOW, []),
            lambda: evaluate_renewal_reminders("owner\n", NOW, []),
            lambda: evaluate_renewal_reminders("owner", True, []),
            lambda: evaluate_renewal_reminders("owner", -1, []),
            lambda: evaluate_renewal_reminders("owner", MAX_TIMESTAMP + 1, []),
            lambda: evaluate_renewal_reminders("owner", float(MAX_TIMESTAMP + 1), []),
            lambda: evaluate_renewal_reminders("owner", 2**53 + 1, []),
            lambda: evaluate_renewal_reminders("owner", 10**400, []),
            lambda: evaluate_renewal_reminders("owner", datetime(2026, 11, 21, 12, 0), []),
            lambda: evaluate_renewal_reminders("owner", datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc), []),
            lambda: evaluate_renewal_reminders("owner", math.inf, []),
            lambda: evaluate_renewal_reminders("owner", math.nan, []),
            lambda: evaluate_renewal_reminders("owner", NOW, [], max_connections=True),
            lambda: evaluate_renewal_reminders("owner", NOW, [], max_connections=-1),
            lambda: evaluate_renewal_reminders("owner", NOW, "not-iterable"),
            lambda: evaluate_renewal_reminders("owner", NOW, {"a": 1}),
            lambda: evaluate_renewal_reminders("owner", NOW, [], lead_days="7"),
            lambda: evaluate_renewal_reminders("owner", NOW, [], lead_days=()),
            lambda: evaluate_renewal_reminders("owner", NOW, [], lead_days=(0,)),
            lambda: evaluate_renewal_reminders("owner", NOW, [], lead_days=(366,)),
            lambda: evaluate_renewal_reminders("owner", NOW, [], lead_days=(True,)),
            lambda: evaluate_renewal_reminders("owner", NOW, [], lead_days=(7, 7)),
            lambda: evaluate_renewal_reminders("owner", NOW, ["bad"]),
            lambda: evaluate_renewal_reminders("owner", NOW, [_target(owner="other")]),
            lambda: evaluate_renewal_reminders("owner", NOW, [_target(enabled=1)]),
            lambda: evaluate_renewal_reminders("owner", NOW, [_target(paused=1)]),
            lambda: evaluate_renewal_reminders("owner", NOW, [_target(destination_kind="shared_calendar")]),
            lambda: evaluate_renewal_reminders("owner", NOW, [_target(destination_kind="OWNER_INBOX")]),
            lambda: evaluate_renewal_reminders("owner", NOW, [_target(destination_kind=" owner_inbox ")]),
            lambda: evaluate_renewal_reminders("owner", NOW, [_target(destination_kind="owner_inbóx")]),
            lambda: evaluate_renewal_reminders("owner", NOW,
                                               [_target(destination_kind="shared_calendar", enabled=False)]),
            lambda: evaluate_renewal_reminders("owner", NOW, [_target(destination_kind=None)]),
            lambda: evaluate_renewal_reminders("owner", NOW, [_target(destination_target=" ")]),
            lambda: evaluate_renewal_reminders("owner", NOW, [_target(connection_id="x" * 129)]),
            lambda: evaluate_renewal_reminders("owner", NOW, [_target(bank_name="x" * 257)]),
            lambda: evaluate_renewal_reminders("owner", NOW, [_target(valid_until=-5)]),
            lambda: evaluate_renewal_reminders("owner", NOW, [_target(valid_until=MAX_TIMESTAMP + 1)]),
            lambda: evaluate_renewal_reminders("owner", NOW, [_target(valid_until=2**53 + 1)]),
            lambda: evaluate_renewal_reminders("owner", NOW, [_target(valid_until=10**400)]),
            lambda: evaluate_renewal_reminders("owner", NOW, [_target(valid_until=True)]),
            lambda: evaluate_renewal_reminders("owner", NOW, [_target(valid_until=math.nan)]),
            lambda: evaluate_renewal_reminders("owner", NOW, [_target(attention="INVALID CODE")]),
            lambda: evaluate_renewal_reminders("owner", NOW, [_target(attention=123)]),
            lambda: evaluate_renewal_reminders("owner", NOW, [_target(notified_milestones=[7])]),
            lambda: evaluate_renewal_reminders("owner", NOW, [_target(notified_milestones=frozenset({True}))]),
            lambda: evaluate_renewal_reminders("owner", NOW, [_target(notified_milestones=frozenset({-1}))]),
            lambda: evaluate_renewal_reminders("owner", NOW, [_target(notified_milestones=frozenset({366}))]),
            lambda: evaluate_renewal_reminders("owner", NOW, [_target(notified_milestones=frozenset({"other"}))]),
            lambda: evaluate_renewal_reminders("owner", NOW, [_target("dup"), _target("dup", enabled=False)]),
        ]
        for call in bad_calls:
            with self.assertRaises(RenewalInputError) as exc:
                call()
            self.assertEqual(str(exc.exception), "Invalid renewal reminder input")

        self.assertEqual(evaluate_renewal_reminders("owner", NOW, [], max_connections=0), ())

        with self.assertRaises(RenewalLimitError) as exc:
            evaluate_renewal_reminders("owner", NOW, [_target("a"), _target("b")], max_connections=1)
        self.assertEqual(str(exc.exception), "Renewal reminder limit exceeded")

        with self.assertRaises(RenewalLimitError):
            evaluate_renewal_reminders("owner", NOW, [], lead_days=range(1, MAX_LEAD_DAYS + 2))

        at_cap = [_target(f"conn-{i:03d}") for i in range(MAX_CONNECTIONS)]
        self.assertEqual(len(evaluate_renewal_reminders("owner", NOW, at_cap)), MAX_CONNECTIONS)
        with self.assertRaises(RenewalLimitError):
            evaluate_renewal_reminders("owner", NOW, [*at_cap, _target("conn-over")])

        at_notified_cap = frozenset(range(MAX_NOTIFIED))
        self.assertEqual(len(evaluate_renewal_reminders("owner", NOW, [_target(notified_milestones=at_notified_cap)])), 1)
        with self.assertRaises(RenewalLimitError):
            evaluate_renewal_reminders("owner", NOW, [_target(notified_milestones=at_notified_cap | {MAX_NOTIFIED})])

    def test_repr_redacts_all_private_fields(self):
        target = _target(
            "conn-secret-77",
            owner="alice-secret",
            bank_name="Private Bank 77",
            destination_target="private-push-handle-88",
            attention="expired_session",
        )
        reminder: ConsentRenewalReminder = evaluate_renewal_reminders("alice-secret", NOW, [target])[0]
        combined = repr(target) + repr(reminder)
        for token in ("alice-secret", "conn-secret-77", "Private Bank 77", "private-push-handle-88", "expired_session"):
            self.assertNotIn(token, combined)


if __name__ == "__main__":
    unittest.main()
