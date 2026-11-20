"""Synthetic HTTP hints: no requests, credentials, persistence, or quota policy."""
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import format_datetime
from fractions import Fraction
import math
import sys
import unittest
from unittest.mock import patch

from hestia.budget import rate_limit as module
from hestia.budget.rate_limit import RateScope, RetryHint, parse_retry_after, rate_scope


NOW = datetime(2026, 11, 20, 12, tzinfo=timezone.utc).timestamp()
ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
ASPSP = "ASPSP_RATE_LIMIT_EXCEEDED"


def http_date(stamp):
    return format_datetime(datetime.fromtimestamp(stamp, timezone.utc), usegmt=True)


class RetryAfterTests(unittest.TestCase):
    def test_frozen_hint_and_delta_seconds_do_not_add_policy_floor(self):
        for raw, delay in (("0", 0), ("1", 1), ("000060", 60), (" \t120\t ", 120)):
            with self.subTest(raw=raw):
                self.assertEqual(parse_retry_after(raw, now=NOW), RetryHint(NOW + delay, False))
        hint = parse_retry_after("120", now=NOW + .25)
        self.assertEqual(hint.deadline, NOW + 120.25)
        with self.assertRaises(FrozenInstanceError):
            hint.deadline = 0

    def test_missing_invalid_and_non_ascii_headers_are_not_hints(self):
        invalid = (None, 3, b"12", [], {}, "", " ", "\t", "-1", "+1", "1.0", "1e2",
                   "NaN", "Infinity", "12, 13", "12 13", "12\t13", "12\n", "12\r\n",
                   "١٢", "１２", "²", "\u00a012", "12\u00a0", "0x10", "PRIVATE/FILE")
        for raw in invalid:
            with self.subTest(raw=raw):
                self.assertEqual(parse_retry_after(raw, now=NOW), RetryHint(None, False))

    def test_header_limit_counts_original_characters_and_accepts_128_ascii_digits(self):
        self.assertEqual(parse_retry_after("0" * 127 + "1", now=NOW), RetryHint(NOW + 1, False))
        self.assertEqual(parse_retry_after("x" * 128, now=NOW), RetryHint(None, False))
        with patch.object(module, "_delta_deadline", side_effect=AssertionError("Must not parse")), \
                patch.object(module, "_http_deadline", side_effect=AssertionError("Must not parse")):
            for raw in ("0" * 128 + "1", " " * 128 + "1", "9" * 129, "x" * 129, "\u0661" * 129):
                with self.subTest(raw=raw):
                    self.assertEqual(parse_retry_after(raw, now=NOW), RetryHint(None, True))

    def test_exact_seven_day_boundary_and_session_expiry_are_independent(self):
        week = module.MAX_AUTOMATIC_DELAY
        self.assertEqual(parse_retry_after(str(week), now=NOW), RetryHint(NOW + week, False))
        self.assertEqual(parse_retry_after(str(week + 1), now=NOW), RetryHint(NOW + week + 1, True))
        self.assertEqual(parse_retry_after("60", now=NOW, valid_until=NOW + 61), RetryHint(NOW + 60, False))
        self.assertEqual(parse_retry_after("60", now=NOW, valid_until=NOW + 60), RetryHint(NOW + 60, True))
        self.assertEqual(parse_retry_after("60", now=NOW, valid_until=NOW + 59), RetryHint(NOW + 60, True))
        self.assertEqual(parse_retry_after("0", now=0, valid_until=0), RetryHint(0.0, True))
        self.assertEqual(parse_retry_after(None, now=NOW, valid_until=0), RetryHint(None, False))

    def test_extreme_valid_integer_is_preserved_and_never_rounded_toward_the_past(self):
        seconds = 10 ** 127 + 1
        hint = parse_retry_after(str(seconds), now=NOW + .25)
        self.assertTrue(hint.suspended)
        self.assertTrue(math.isfinite(hint.deadline))
        self.assertGreaterEqual(Fraction(hint.deadline), Fraction(NOW + .25) + seconds)
        precise = parse_retry_after(str(2 ** 53 + 1), now=0)
        self.assertGreaterEqual(Fraction(precise.deadline), 2 ** 53 + 1)
        self.assertEqual(parse_retry_after("0", now=sys.float_info.max), RetryHint(sys.float_info.max, False))

    def test_unrepresentable_sum_suspends_instead_of_shortening_or_overflowing(self):
        self.assertEqual(parse_retry_after("1", now=sys.float_info.max), RetryHint(None, True))
        # This finite integer rounds to float max, but its exact sum crosses the
        # overflow midpoint: cover conversion overflow separately from nextafter.
        last_roundable_integer = int(sys.float_info.max) + 2 ** 970 - 1
        self.assertTrue(math.isfinite(last_roundable_integer))
        self.assertEqual(parse_retry_after("1", now=last_roundable_integer), RetryHint(None, True))

    def test_http_gmt_dates_are_aware_without_host_timezone_guessing(self):
        for offset in (0, 1, 60, module.MAX_AUTOMATIC_DELAY):
            with self.subTest(offset=offset):
                self.assertEqual(parse_retry_after(http_date(NOW + offset), now=NOW),
                                 RetryHint(NOW + offset, False))
        self.assertEqual(parse_retry_after(http_date(NOW - 1), now=NOW), RetryHint(None, False))
        self.assertEqual(parse_retry_after(http_date(NOW), now=NOW + .25), RetryHint(None, False))
        self.assertEqual(parse_retry_after(" \t" + http_date(NOW + 60) + "\t ", now=NOW),
                         RetryHint(NOW + 60, False))

    def test_future_and_extreme_dates_follow_same_suspension_boundaries(self):
        after_week = NOW + module.MAX_AUTOMATIC_DELAY + 1
        self.assertEqual(parse_retry_after(http_date(after_week), now=NOW), RetryHint(after_week, True))
        self.assertEqual(parse_retry_after(http_date(NOW + 60), now=NOW, valid_until=NOW + 60),
                         RetryHint(NOW + 60, True))
        far = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp()
        self.assertEqual(parse_retry_after(http_date(far), now=NOW), RetryHint(far, True))

    def test_http_date_rejects_other_zones_naive_dates_lists_and_malformed_calendar_values(self):
        date = http_date(NOW + 60)
        invalid = (date.replace("GMT", "UTC"), date.replace("GMT", "+0000"),
                   date.replace("GMT", "-0800"), date.replace("GMT", "PST"), date[:-4],
                   date + ", " + date, date + " GMT", date + "\n", date.lower(),
                   date.replace("Fri", "Thursday"), date.replace("Fri", "Thu"),
                   date.replace("Nov", "Nox"), date.replace("20 Nov", "31 Feb"),
                   date.replace("12:01:00", "24:01:00"), date.replace("12:01:00", "12:60:00"),
                   date.replace("12:01:00", "12:01:61"), date.replace("2026", "0000"),
                   date.replace("20 Nov", "2 Nov"), date.replace("20 Nov", " 20 Nov"),
                   "2026-11-20T12:01:00Z", "Fri Nov 20 12:01:00 2026 GMT")
        for raw in invalid:
            with self.subTest(raw=raw):
                self.assertEqual(parse_retry_after(raw, now=NOW), RetryHint(None, False))

    def test_both_obsolete_http_date_forms_are_explicitly_utc(self):
        self.assertEqual(parse_retry_after("Friday, 20-Nov-26 12:01:00 GMT", now=NOW),
                         RetryHint(NOW + 60, False))
        self.assertEqual(parse_retry_after("Fri Nov 20 12:01:00 2026", now=NOW),
                         RetryHint(NOW + 60, False))
        single_day = datetime(2026, 12, 6, 12, tzinfo=timezone.utc).timestamp()
        self.assertEqual(parse_retry_after("Sun Dec  6 12:00:00 2026", now=NOW), RetryHint(single_day, True))
        self.assertEqual(parse_retry_after("Sun Dec 06 12:00:00 2026", now=NOW), RetryHint(single_day, True))

    def test_rfc850_uses_injected_clock_for_rolling_fifty_year_rule(self):
        exact = datetime(2076, 11, 20, 12, tzinfo=timezone.utc)
        raw = exact.strftime("%A, %d-%b-%y %H:%M:%S GMT")
        self.assertEqual(parse_retry_after(raw, now=NOW), RetryHint(exact.timestamp(), True))
        past = datetime(1976, 11, 20, 12, 0, 1, tzinfo=timezone.utc)
        self.assertEqual(parse_retry_after(past.strftime("%A, %d-%b-%y %H:%M:%S GMT"), now=NOW),
                         RetryHint(None, False))
        next_century = datetime(2101, 1, 1, tzinfo=timezone.utc)
        later_now = datetime(2090, 1, 1, tzinfo=timezone.utc).timestamp()
        raw = next_century.strftime("%A, %d-%b-%y %H:%M:%S GMT")
        self.assertEqual(parse_retry_after(raw, now=later_now), RetryHint(next_century.timestamp(), True))
        self.assertEqual(parse_retry_after("Friday, 20-Nov-26 12:01:00 GMT", now=sys.float_info.max),
                         RetryHint(None, False))

    def test_leap_second_is_not_silently_shortened(self):
        before = datetime(2016, 12, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp()
        self.assertEqual(parse_retry_after("Sat, 31 Dec 2016 23:59:60 GMT", now=before),
                         RetryHint(before + 1, False))
        last = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp()
        self.assertEqual(parse_retry_after("Fri, 31 Dec 9999 23:59:60 GMT", now=NOW),
                         RetryHint(last + 1, True))

    def test_invalid_clock_parameters_raise_only_safe_generic_errors(self):
        invalid = (None, True, "PRIVATE-CLOCK", [], Decimal(2), -1, math.nan, math.inf,
                   -math.inf, 10 ** 1000)
        for value in invalid:
            with self.subTest(now=repr(value)):
                with self.assertRaisesRegex(ValueError, "^Invalid retry clock$"):
                    parse_retry_after("60", now=value)
            if value is not None:
                with self.subTest(valid_until=repr(value)):
                    with self.assertRaisesRegex(ValueError, "^Invalid retry clock$"):
                        parse_retry_after("60", now=NOW, valid_until=value)


class RateScopeTests(unittest.TestCase):
    def test_only_documented_aspsp_code_on_exact_account_routes_is_account_scoped(self):
        for endpoint in ("transactions", "balances"):
            for uid in (ACCOUNT, ACCOUNT.upper()):
                with self.subTest(endpoint=endpoint, uid=uid):
                    self.assertEqual(rate_scope("/accounts/" + uid + "/" + endpoint, ASPSP),
                                     RateScope.ACCOUNT_ENDPOINT)
        self.assertEqual(RateScope.APPLICATION.value, "application")

    def test_unknown_codes_sessions_and_unvalidated_paths_use_connection_scope(self):
        account_path = "/accounts/" + ACCOUNT + "/transactions"
        for code in (None, [], {}, "", "rate_limit", ASPSP.lower(), ASPSP + "\n", "RATE_LIMIT_EXCEEDED"):
            with self.subTest(code=code):
                self.assertEqual(rate_scope(account_path, code), RateScope.CONNECTION)
        invalid = (None, 1, [], "", "/sessions/" + ACCOUNT, account_path + "/", account_path + "?limit=1",
                   account_path + "\n", account_path + "#fragment", account_path.replace("accounts", "ACCOUNTS"),
                   account_path.replace(ACCOUNT, "not-a-uuid"), account_path.replace("transactions", "details"),
                   "https://api.enablebanking.com" + account_path, "/accounts/../transactions")
        for path in invalid:
            with self.subTest(path=path):
                self.assertEqual(rate_scope(path, ASPSP), RateScope.CONNECTION)
