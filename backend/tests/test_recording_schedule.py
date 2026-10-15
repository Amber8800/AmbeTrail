import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from dev_snapshot.common import settings_from
from dev_snapshot.engine import Session


class RecordingScheduleTests(unittest.TestCase):
    def session(self, **values):
        # Exercise the real scheduler with a fixed clock, without Git or background writes.
        store = SimpleNamespace(directory=Path.cwd(), writable=True, journals=lambda: [])
        with patch("dev_snapshot.engine.read_json", return_value={}), patch("dev_snapshot.engine.time.monotonic", return_value=1000):
            session = Session(SimpleNamespace(), store, settings_from({"reconcileSeconds": 100000, **values}))
        session.start_capture = Mock(return_value={"state": "running"})
        return session

    def test_custom_idle_wait_is_used(self):
        session = self.session(idleSeconds=15)
        with patch("dev_snapshot.engine.time.monotonic", return_value=1000):
            session.event("auth.py")
        with patch("dev_snapshot.engine.time.monotonic", return_value=1014):
            self.assertFalse(session.tick())
        with patch("dev_snapshot.engine.time.monotonic", return_value=1015):
            self.assertTrue(session.tick())
        session.start_capture.assert_called_once_with("automatic", full=False, changed_paths={"auth.py"})

    def test_custom_maximum_interval_captures_while_changes_continue(self):
        session = self.session(idleSeconds=15, maxIntervalSeconds=180)
        with patch("dev_snapshot.engine.time.monotonic", return_value=1000):
            session.event("auth.py")
        with patch("dev_snapshot.engine.time.monotonic", return_value=1179):
            session.event("login.py")
            self.assertFalse(session.tick())
        with patch("dev_snapshot.engine.time.monotonic", return_value=1180):
            self.assertTrue(session.tick())
        session.start_capture.assert_called_once_with("automatic", full=False, changed_paths={"auth.py", "login.py"})

    def test_custom_full_hash_interval_is_used_without_file_events(self):
        session = self.session(fullHashSeconds=900)
        with patch("dev_snapshot.engine.time.monotonic", return_value=1899):
            self.assertFalse(session.tick())
        with patch("dev_snapshot.engine.time.monotonic", return_value=1900):
            self.assertTrue(session.tick())
        session.start_capture.assert_called_once_with("automatic", full=True, changed_paths=None)

    def test_reconciliation_does_not_bypass_a_longer_user_recording_delay(self):
        session = self.session(idleSeconds=180, maxIntervalSeconds=300, reconcileSeconds=120)
        with patch("dev_snapshot.engine.time.monotonic", return_value=1000):
            session.event("auth.py")
        with patch("dev_snapshot.engine.time.monotonic", return_value=1120):
            self.assertFalse(session.tick())
        with patch("dev_snapshot.engine.time.monotonic", return_value=1180):
            self.assertTrue(session.tick())
        session.start_capture.assert_called_once_with("automatic", full=False, changed_paths={"auth.py"})

    def test_reconciliation_still_checks_for_missed_events_when_idle(self):
        session = self.session(reconcileSeconds=120)
        with patch("dev_snapshot.engine.time.monotonic", return_value=1120):
            self.assertTrue(session.tick())
        session.start_capture.assert_called_once_with("automatic", full=False, changed_paths=None)
