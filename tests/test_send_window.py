"""Send-time freshness includes days after the feed stops, without a database."""

import contextlib
import io
import os
import sys
import unittest
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import check_send_window as window


def at(value):
    return datetime.fromisoformat(value)


class SendWindowTests(unittest.TestCase):
    def test_fresh_daily_bulletins(self):
        stamps = [at(f"2026-10-0{day}T05:00:00+05:30") for day in (1, 2, 3)]
        self.assertEqual(window.worst_age_at(5, stamps,
                         at("2026-10-03T06:00:00+05:30")), (0.0, 3))

    def test_stopped_feed_includes_missing_days_and_tail(self):
        stamps = [at(f"2026-09-{day}T05:00:00+05:30") for day in (23, 24, 25)]
        self.assertEqual(window.worst_age_at(5, stamps,
                         at("2026-10-03T21:00:00+05:30")), (192.0, 11))

    def test_gap_inside_window_is_not_dropped(self):
        stamps = [at("2026-10-01T05:00:00+05:30"),
                  at("2026-10-03T05:00:00+05:30")]
        self.assertEqual(window.worst_age_at(5, stamps,
                         at("2026-10-03T06:00:00+05:30")), (24.0, 3))

    def test_previous_day_is_the_reading_available_to_the_sender(self):
        self.assertEqual(window.worst_age_at(5,
                         [at("2026-10-02T23:00:00+05:30")],
                         at("2026-10-03T06:00:00+05:30")), (6.0, 1))

    def test_future_send_and_future_bulletin_are_not_scored(self):
        stamps = [at("2026-10-02T05:00:00+05:30"),
                  at("2026-10-03T05:00:00+05:30")]
        self.assertEqual(window.worst_age_at(5, stamps,
                         at("2026-10-03T04:00:00+05:30")), (0.0, 1))

    def test_calendar_boundary_uses_ist(self):
        self.assertEqual(window.worst_age_at(5,
                         [at("2026-10-02T05:00:00+05:30")],
                         at("2026-10-02T23:59:00+00:00")), (24.0, 2))

    def test_empty_feed_has_no_measurement(self):
        self.assertEqual(window.worst_age_at(5, [],
                         at("2026-10-03T06:00:00+05:30")), (None, 0))

    def test_command_refuses_outage_instead_of_recommending_a_send_hour(self):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return at("2026-10-03T21:00:00+05:30").astimezone(tz)

        stamps = [at(f"2026-09-{day}T05:00:00+05:30") for day in (23, 24, 25)]
        output = io.StringIO()
        with patch.object(window, "datetime", FixedDateTime), \
                patch.object(window, "connect"), \
                patch.object(window, "bulletins", return_value=stamps), \
                patch.object(sys, "argv", ["check_send_window.py"]), \
                contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                window.main()
        self.assertIn("FAIL", str(raised.exception))
        self.assertIn("collection failures", str(raised.exception))
        self.assertIn("192.0h", output.getvalue())
        self.assertNotIn("OK —", output.getvalue())


if __name__ == "__main__":
    unittest.main()
