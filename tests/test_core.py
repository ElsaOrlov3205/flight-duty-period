"""Tests for flight_duty_period.core.

All times are explicit literals; no wall-clock dependency anywhere.
"""

import unittest
from datetime import datetime, timedelta

from flight_duty_period.core import (
    CrewMember,
    Duty,
    FDPResult,
    RestRequirement,
    calculate_fdp,
    calculate_rest,
    parse_time,
)


def dt(s: str) -> datetime:
    return parse_time(s)


class TestParseTime(unittest.TestCase):
    def test_basic_iso(self):
        t = parse_time("2024-06-10T06:00")
        self.assertEqual(t, datetime(2024, 6, 10, 6, 0))

    def test_space_separator(self):
        t = parse_time("2024-06-10 06:00")
        self.assertEqual(t, datetime(2024, 6, 10, 6, 0))

    def test_truncates_seconds(self):
        t = parse_time("2024-06-10T06:00:45")
        self.assertEqual(t.second, 0)

    def test_rejects_timezone_aware(self):
        with self.assertRaises(ValueError):
            parse_time("2024-06-10T06:00+02:00")

    def test_rejects_garbage(self):
        with self.assertRaises(ValueError):
            parse_time("not-a-date")

    def test_rejects_non_string(self):
        with self.assertRaises(TypeError):
            parse_time(12345)  # type: ignore[arg-type]


class TestDutyConstruction(unittest.TestCase):
    def test_basic_duty(self):
        d = Duty(report=dt("2024-06-10T08:00"), debrief_end=dt("2024-06-10T18:00"), sectors=2)
        self.assertEqual(d.sectors, 2)
        self.assertEqual(d.fdp_duration, timedelta(hours=10))

    def test_debrief_excluded_from_fdp(self):
        # 10h wall-to-wall, 30m debrief -> 9h30m FDP.
        d = Duty(report=dt("2024-06-10T08:00"), debrief_end=dt("2024-06-10T18:00"))
        self.assertEqual(d.debrief_duration, timedelta(minutes=30))
        self.assertEqual(d.fdp_without_debrief, timedelta(hours=9, minutes=30))

    def test_debrief_clamped_for_tiny_duty(self):
        d = Duty(report=dt("2024-06-10T08:00"), debrief_end=dt("2024-06-10T08:10"))
        self.assertEqual(d.debrief_duration, timedelta(minutes=10))
        self.assertEqual(d.fdp_without_debrief, timedelta(0))

    def test_rejects_end_before_start(self):
        with self.assertRaises(ValueError):
            Duty(report=dt("2024-06-10T18:00"), debrief_end=dt("2024-06-10T08:00"))

    def test_rejects_zero_sectors(self):
        with self.assertRaises(ValueError):
            Duty(report=dt("2024-06-10T08:00"), debrief_end=dt("2024-06-10T18:00"), sectors=0)

    def test_rejects_tz_aware_times(self):
        from datetime import timezone
        with self.assertRaises(ValueError):
            Duty(
                report=datetime(2024, 6, 10, 8, 0, tzinfo=timezone.utc),
                debrief_end=datetime(2024, 6, 10, 18, 0, tzinfo=timezone.utc),
            )


class TestCalculateFDP(unittest.TestCase):
    def test_morning_two_sector_within_limit(self):
        d = Duty(report=dt("2024-06-10T06:00"), debrief_end=dt("2024-06-10T18:00"), sectors=2)
        r = calculate_fdp(d)
        self.assertIsInstance(r, FDPResult)
        self.assertTrue(r.within_limit)
        # 06:00-07:59, 2 sectors -> 13h max; 12h wall - 30m debrief = 11h30 actual.
        self.assertEqual(r.max_fdp, timedelta(hours=13))
        self.assertEqual(r.actual_fdp, timedelta(hours=11, minutes=30))
        self.assertEqual(r.margin, timedelta(hours=1, minutes=30))
        self.assertFalse(r.over_limit)

    def test_late_report_single_sector(self):
        d = Duty(report=dt("2024-06-10T19:00"), debrief_end=dt("2024-06-11T04:00"), sectors=1)
        r = calculate_fdp(d)
        # 18:00-24:00, 1 sector -> 12h max; 9h wall - 30m debrief = 8h30 actual.
        # WOCL penalty applies: duty 19:00-04:00 touches 02:00-04:59 of next day.
        self.assertEqual(r.max_fdp, timedelta(hours=11, minutes=30))
        self.assertTrue(r.within_limit)

    def test_over_limit_detected(self):
        d = Duty(report=dt("2024-06-10T01:00"), debrief_end=dt("2024-06-10T13:00"), sectors=1)
        r = calculate_fdp(d)
        # 00:00-05:00, 1 sector -> 11h max; WOCL penalty (start 01:00 inside
        # 02:00-04:59 window) -> 10h30 max; 12h wall - 30m debrief = 11h30 actual
        # -> over by 1h.
        self.assertFalse(r.within_limit)
        self.assertTrue(r.over_limit)
        self.assertEqual(r.margin, timedelta(hours=-1))

    def test_wocl_penalty_applied(self):
        # Duty 05:00 -> 17:00, 1 sector. Band 05:00-05:59, 1 sector -> 13h.
        # Spans WOCL? 05:00 report, 17:00 end. WOCL on start date is 02:00-04:59,
        # so duty start (05:00) is NOT before 05:00... boundary check: spans
        # WOCL only if start < 05:00. Here start == 05:00, so no WOCL.
        d_no_wocl = Duty(
            report=dt("2024-06-10T05:00"), debrief_end=dt("2024-06-10T17:00"), sectors=1
        )
        r_no = calculate_fdp(d_no_wocl)
        self.assertFalse(r_no.spans_wocl)
        self.assertEqual(r_no.max_fdp, timedelta(hours=13))

        # Duty 04:30 -> 16:30: start 04:30 is inside WOCL -> penalty applies.
        d_wocl = Duty(
            report=dt("2024-06-10T04:30"), debrief_end=dt("2024-06-10T16:30"), sectors=1
        )
        r_yes = calculate_fdp(d_wocl)
        self.assertTrue(r_yes.spans_wocl)
        # 04:30 is in band 00:00-05:00, 1 sector -> 11h, minus 30m = 10h30.
        self.assertEqual(r_yes.max_fdp, timedelta(hours=10, minutes=30))

    def test_duty_crossing_midnight_into_wocl(self):
        # Starts 23:00, ends 06:00 next day. Touches 02:00-04:59 of next day.
        d = Duty(report=dt("2024-06-10T23:00"), debrief_end=dt("2024-06-11T06:00"), sectors=1)
        r = calculate_fdp(d)
        self.assertTrue(r.spans_wocl)

    def test_crew_acclimatisation_flag(self):
        d = Duty(report=dt("2024-06-10T08:00"), debrief_end=dt("2024-06-10T18:00"))
        acclim = CrewMember(name="PW", acclimatised_as_of=dt("2024-06-08T00:00").date())
        fresh = CrewMember(name="PW", acclimatised_as_of=dt("2024-06-01T00:00").date())
        r1 = calculate_fdp(d, crew=acclim)
        r2 = calculate_fdp(d, crew=fresh)
        self.assertTrue(r1.acclimatised)
        self.assertFalse(r2.acclimatised)
        # Numeric limits identical regardless of acclimatisation flag.
        self.assertEqual(r1.max_fdp, r2.max_fdp)

    def test_three_or_more_sectors_collapse(self):
        d3 = Duty(report=dt("2024-06-10T06:00"), debrief_end=dt("2024-06-10T16:00"), sectors=3)
        d5 = Duty(report=dt("2024-06-10T06:00"), debrief_end=dt("2024-06-10T16:00"), sectors=5)
        r3 = calculate_fdp(d3)
        r5 = calculate_fdp(d5)
        self.assertEqual(r3.max_fdp, r5.max_fdp)


class TestCalculateRest(unittest.TestCase):
    def test_no_preceding_duty(self):
        r = calculate_rest(None)
        self.assertIsInstance(r, RestRequirement)
        self.assertEqual(r.min_rest, timedelta(hours=10))
        self.assertIn("10h floor", r.basis)

    def test_short_fdp_uses_floor(self):
        d = Duty(report=dt("2024-06-10T08:00"), debrief_end=dt("2024-06-10T12:00"))
        r = calculate_rest(d)
        self.assertEqual(r.min_rest, timedelta(hours=10))
        self.assertIn("10h floor", r.basis)

    def test_long_fdp_drives_rest(self):
        # 14h wall, 30m debrief -> 13h30 FDP, > 10h floor.
        d = Duty(report=dt("2024-06-10T06:00"), debrief_end=dt("2024-06-10T20:00"))
        r = calculate_rest(d)
        self.assertEqual(r.preceding_fdp, timedelta(hours=13, minutes=30))
        self.assertEqual(r.min_rest, timedelta(hours=13, minutes=30))
        self.assertIn("exceeds 10h floor", r.basis)

    def test_available_rest_reported_in_basis_when_next_report_given(self):
        d = Duty(report=dt("2024-06-10T08:00"), debrief_end=dt("2024-06-10T18:00"))
        # Next report 22:00 same day -> only 4h rest, below 10h.
        r = calculate_rest(d, next_report=dt("2024-06-10T22:00"))
        self.assertIn("available rest", r.basis)
        self.assertIn("< required", r.basis)

    def test_sufficient_rest_no_warning(self):
        d = Duty(report=dt("2024-06-10T08:00"), debrief_end=dt("2024-06-10T18:00"))
        r = calculate_rest(d, next_report=dt("2024-06-11T08:00"))
        self.assertNotIn("< required", r.basis)


class TestReturnTypes(unittest.TestCase):
    def test_fdp_result_fields(self):
        d = Duty(report=dt("2024-06-10T08:00"), debrief_end=dt("2024-06-10T18:00"))
        r = calculate_fdp(d)
        self.assertIsInstance(r.max_fdp, timedelta)
        self.assertIsInstance(r.actual_fdp, timedelta)
        self.assertIsInstance(r.margin, timedelta)
        self.assertIsInstance(r.within_limit, bool)
        self.assertIsInstance(r.spans_wocl, bool)
        self.assertIsInstance(r.acclimatised, bool)

    def test_rest_requirement_fields(self):
        d = Duty(report=dt("2024-06-10T08:00"), debrief_end=dt("2024-06-10T20:00"))
        r = calculate_rest(d)
        self.assertIsInstance(r.preceding_fdp, timedelta)
        self.assertIsInstance(r.min_rest, timedelta)
        self.assertIsInstance(r.basis, str)


if __name__ == "__main__":
    unittest.main()
