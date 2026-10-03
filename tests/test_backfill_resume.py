"""Recover interruptions without replacing full hours with partial aggregates."""

import contextlib
import io
import os
import sys
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import backfill_openaq as backfill


def at(value):
    return datetime.fromisoformat(value)


class BackfillResumeTests(unittest.TestCase):
    def setUp(self):
        self.now = at("2026-10-03T12:00:00+00:00")

    def test_recent_station_replays_seven_days(self):
        self.assertEqual(backfill.resume_start(at("2026-10-02T07:30:00+00:00"), self.now),
                         at("2026-09-26T12:00:00+00:00"))

    def test_interrupted_station_resumes_before_the_rolling_window(self):
        self.assertEqual(backfill.resume_start(at("2026-09-24T16:30:00+00:00"), self.now),
                         at("2026-09-24T16:30:00+00:00"))

    def test_new_station_has_a_bounded_initial_pull(self):
        self.assertEqual(backfill.resume_start(None, self.now),
                         at("2026-09-26T12:00:00+00:00"))

    def test_archive_lag_longer_than_seven_days_does_not_outrun_coverage(self):
        latest = at("2026-09-20T16:30:00+00:00")
        coverage = (123, at("2025-02-18T19:30:00+00:00"),
                    at("2026-09-21T16:30:00+00:00"))
        cur = Mock()
        cur.rowcount = 1
        with patch.object(backfill, "pm25_sensor", return_value=coverage), \
                patch.object(backfill, "measurements", return_value=[(latest, 81.0)]) as fetched, \
                contextlib.redirect_stdout(io.StringIO()):
            changed = backfill.backfill_station(cur, 15, "S", 6964, "fixture",
                                                backfill.resume_start(latest, self.now), None)
        self.assertEqual(changed, 1)
        self.assertEqual(fetched.call_args.args[1], at("2026-09-20T16:30:00+00:00"))
        self.assertEqual(cur.executemany.call_args.args[1], [(15, latest, 81.0)])


def reading(start, end, value, coverage=100):
    return {"period": {"datetimeFrom": {"utc": start},
                       "datetimeTo": {"utc": end}},
            "value": value, "coverage": {"percentComplete": coverage}}


class HourlyBoundaryTests(unittest.TestCase):
    def pull(self, start, end, rows):
        with patch.object(backfill, "openaq_get", return_value={"results": rows}), \
                contextlib.redirect_stderr(io.StringIO()) as log:
            result = list(backfill.measurements(14258997, at(start), at(end), "fixture"))
        return result, log.getvalue()

    def test_measured_mid_hour_request_does_not_overwrite_full_hour(self):
        full = reading("2026-09-26T15:30:00Z", "2026-09-26T16:30:00Z", 21.2)
        partial = reading("2026-09-26T15:30:00Z", "2026-09-26T16:30:00Z", 19.8, 75)
        end = "2026-09-26T16:30:00+00:00"
        result, log = self.pull("2026-09-26T15:30:00+00:00", end, [full])
        self.assertEqual(result, [(at("2026-09-26T15:30:00+00:00"), 21.2)])
        self.assertEqual(log, "")
        result, log = self.pull("2026-09-26T15:45:00+00:00", end, [partial])
        self.assertEqual(result, [])
        self.assertIn("1 hour(s) skipped", log)

    def test_both_request_boundaries_are_excluded(self):
        rows = [reading("2026-09-26T14:30:00Z", "2026-09-26T15:30:00Z", 25.7, 50),
                reading("2026-09-26T15:30:00Z", "2026-09-26T16:30:00Z", 21.2),
                reading("2026-09-26T16:30:00Z", "2026-09-26T17:30:00Z", 16.4, 50)]
        result, log = self.pull("2026-09-26T15:00:00+00:00",
                                "2026-09-26T17:00:00+00:00", rows)
        self.assertEqual(result, [(at("2026-09-26T15:30:00+00:00"), 21.2)])
        self.assertIn("2 hour(s) skipped", log)

    def test_genuine_low_sensor_coverage_inside_request_is_preserved(self):
        rows = [reading("2026-09-26T15:30:00Z", "2026-09-26T16:30:00Z", 19.8, 75)]
        result, log = self.pull("2026-09-26T15:00:00+00:00",
                                "2026-09-26T17:00:00+00:00", rows)
        self.assertEqual(result, [(at("2026-09-26T15:30:00+00:00"), 19.8)])
        self.assertEqual(log, "")

    def test_paging_uses_raw_page_size_after_boundary_rejection(self):
        clipped = reading("2026-09-26T14:30:00Z", "2026-09-26T15:30:00Z", 25.7, 50)
        full = reading("2026-09-26T15:30:00Z", "2026-09-26T16:30:00Z", 21.2)
        with patch.object(backfill, "PAGE_LIMIT", 1), \
                patch.object(backfill, "openaq_get", side_effect=[
                    {"results": [clipped]}, {"results": [full]}, {"results": []}
                ]) as fetched, contextlib.redirect_stderr(io.StringIO()):
            result = list(backfill.measurements(14258997,
                at("2026-09-26T15:00:00+00:00"), at("2026-09-26T17:00:00+00:00"), "fixture"))
        self.assertEqual(result, [(at("2026-09-26T15:30:00+00:00"), 21.2)])
        self.assertEqual([call.kwargs["page"] for call in fetched.call_args_list], [1, 2, 3])

    def test_missing_or_non_hourly_period_fails_visibly(self):
        invalid = [reading("2026-09-26T15:30:00Z", "2026-09-26T17:30:00Z", 21.2),
                   {"period": {"datetimeFrom": {"utc": "2026-09-26T15:30:00Z"}}, "value": 21.2}]
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.pull("2026-09-26T15:00:00+00:00", "2026-09-26T18:00:00+00:00", [row])


if __name__ == "__main__":
    unittest.main()
