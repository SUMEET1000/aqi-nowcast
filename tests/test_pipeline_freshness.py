"""Freshness queries use ingester outcomes, tested on rollback-only temp tables."""

import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import gate1_check as gate
from db import connect


class PipelineFreshnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = connect()
        with cls.conn.cursor() as cur:
            cur.execute("CREATE TEMP TABLE fetch_log (run_ts timestamptz, outcome text)")
            cur.execute("CREATE TEMP TABLE observations (observation_ts timestamptz)")

    @classmethod
    def tearDownClass(cls):
        cls.conn.rollback()
        cls.conn.close()

    def setUp(self):
        gate.failures.clear()
        with self.conn.cursor() as cur:
            cur.execute("TRUNCATE pg_temp.fetch_log, pg_temp.observations")
            cur.execute("INSERT INTO pg_temp.observations VALUES (now() - interval '2 hours')")

    def check(self):
        with self.conn.cursor() as cur, contextlib.redirect_stdout(io.StringIO()):
            gate.check_freshness(cur)
        return list(gate.failures)

    def test_recent_ingest_and_bulletin_pass(self):
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO pg_temp.fetch_log VALUES "
                        "(now() - interval '5 minutes', 'success')")
        self.assertEqual(self.check(), [])

    def test_recent_send_does_not_hide_stopped_ingest(self):
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO pg_temp.fetch_log VALUES "
                        "(now() - interval '120 minutes', 'success'), "
                        "(now() - interval '1 minute', 'alerts_sent'), "
                        "(now() - interval '1 minute', 'monitor_ok')")
        failures = self.check()
        self.assertEqual(len(failures), 1)
        self.assertIn("last run was 2.0h ago", failures[0])

    def test_send_without_any_ingest_is_not_ingester_liveness(self):
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO pg_temp.fetch_log VALUES "
                        "(now() - interval '1 minute', 'alerts_sent')")
        failures = self.check()
        self.assertEqual(len(failures), 1)
        self.assertIn("ingester has never run", failures[0])

    def test_recent_ingest_does_not_hide_stale_bulletin(self):
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO pg_temp.fetch_log VALUES "
                        "(now() - interval '5 minutes', 'stale')")
            cur.execute("UPDATE pg_temp.observations "
                        "SET observation_ts = now() - interval '24 hours'")
        failures = self.check()
        self.assertEqual(len(failures), 1)
        self.assertIn("newest bulletin is 24.0h old", failures[0])


if __name__ == "__main__":
    unittest.main()
