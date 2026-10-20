"""End-to-end orchestration tests with fake fetchers and a fake sender."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from evprice.config import load_config  # noqa: E402
from evprice.http import SourceError  # noqa: E402
from evprice.main import Outcome, run  # noqa: E402
from evprice.state import State  # noqa: E402
from demo import make_fake_fetcher  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = load_config(ROOT / "config.yaml", env={"GMAIL_USER": "u@example.com", "GMAIL_APP_PASSWORD": "x"})
        self.cfg = cfg.__class__(**{**cfg.__dict__, "state_file": Path(self.tmp.name) / "runs.json",
                                   "output_dir": Path(self.tmp.name) / "out"})
        self.sent = []

    def tearDown(self):
        self.tmp.cleanup()

    def sender(self, cfg, msg):
        self.sent.append(msg)

    def at(self, s: str) -> datetime:
        return datetime.fromisoformat(s).replace(tzinfo=self.cfg.tz)

    def go(self, now: str, **fake):
        n = self.at(now)
        return run(self.cfg, make_fake_fetcher(self.cfg, n, **fake), n, sender=self.sender)


class TestRun(Harness):
    def test_sends_once_and_never_twice(self):
        self.assertEqual(self.go("2026-10-20T14:20"), Outcome.SENT)
        self.assertEqual(self.go("2026-10-20T14:50"), Outcome.SKIPPED)
        self.assertEqual(self.go("2026-10-20T16:50"), Outcome.SKIPPED)
        self.assertEqual(len(self.sent), 1)
        state = json.loads(self.cfg.state_file.read_text())
        self.assertEqual([r["status"] for r in state["runs"]], ["sent", "skipped", "skipped"])
        # next day is a fresh slate
        self.assertEqual(self.go("2026-10-21T14:20"), Outcome.SENT)
        self.assertEqual(len(self.sent), 2)

    def test_before_earliest_time_does_nothing(self):
        self.assertEqual(self.go("2026-10-20T11:30"), Outcome.SKIPPED)
        self.assertEqual(self.sent, [])

    def test_waits_for_tomorrow_until_cutoff_then_sends_with_warning(self):
        self.assertEqual(self.go("2026-10-20T14:20", tomorrow_ok=False), Outcome.SKIPPED)
        self.assertEqual(self.go("2026-10-20T17:50", tomorrow_ok=False), Outcome.SKIPPED)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.go("2026-10-20T18:05", tomorrow_ok=False), Outcome.SENT)
        msg = self.sent[0]
        self.assertIn("data incomplete", msg["Subject"])
        self.assertIn("not yet published", msg.get_body(("plain",)).get_content())

    def test_forecast_outage_still_sends(self):
        self.assertEqual(self.go("2026-10-20T14:20", forecast_ok=False), Outcome.SENT)
        body = self.sent[0].get_body(("plain",)).get_content()
        self.assertIn("forecast unavailable", body)
        self.assertIn("Best:", body)

    def test_actual_source_outage_fails_without_sending(self):
        def broken(url, params):
            raise SourceError("boom")
        n = self.at("2026-10-20T14:20")
        self.assertEqual(run(self.cfg, broken, n, sender=self.sender), Outcome.FAILED)
        self.assertEqual(self.sent, [])
        self.assertFalse(State(self.cfg.state_file).already_sent(n.date()))

    def test_send_failure_is_retried_next_tick(self):
        def bad_sender(cfg, msg):
            raise RuntimeError("smtp down")
        n = self.at("2026-10-20T14:20")
        f = make_fake_fetcher(self.cfg, n)
        self.assertEqual(run(self.cfg, f, n, sender=bad_sender), Outcome.FAILED)
        self.assertEqual(run(self.cfg, f, n + timedelta(minutes=30), sender=self.sender), Outcome.SENT)
        self.assertEqual(len(self.sent), 1)

    def test_email_structure(self):
        self.go("2026-10-20T14:20")
        msg = self.sent[0]
        self.assertEqual(msg["To"], ", ".join(self.cfg.recipients))
        self.assertEqual(len(self.cfg.recipients), 3)
        parts = [p.get_content_type() for p in msg.walk()]
        self.assertIn("text/plain", parts)
        self.assertIn("text/html", parts)
        self.assertEqual(parts.count("image/png"), 2)  # inline + attachment

    def test_dry_run_writes_files_and_no_state(self):
        n = self.at("2026-10-20T14:20")
        self.assertEqual(run(self.cfg, make_fake_fetcher(self.cfg, n), n, dry_run=True, sender=self.sender), Outcome.DRY_RUN)
        self.assertTrue((self.cfg.output_dir / "ev-prices-2026-10-20.png").exists())
        self.assertFalse(self.cfg.state_file.exists())
        self.assertEqual(self.sent, [])

    def test_dst_fall_back_day(self):
        # Run on Sat 24 Oct: tomorrow (25 Oct) has 100 slots. Everything must validate and send.
        self.assertEqual(self.go("2026-10-24T14:20"), Outcome.SENT)
        body = self.sent[0].get_body(("plain",)).get_content()
        self.assertNotIn("incomplete", body)

    def test_dst_spring_forward_day(self):
        self.assertEqual(self.go("2026-03-28T14:20"), Outcome.SENT)
        self.assertNotIn("incomplete", self.sent[0].get_body(("plain",)).get_content())


if __name__ == "__main__":
    unittest.main()
