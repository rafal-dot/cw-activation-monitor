import contextlib
import csv
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from cw_monitor import cli
from cw_monitor.filtering import SnrFilter, accepts_snr
from cw_monitor.monitor import Monitor
from cw_monitor.polling import Poller
from cw_monitor.storage import preferences, session
from cw_monitor.ui.filter_menu import FilterMenu
from cw_monitor.ui import views
from test_pota_alert import NOW
from test_snr_filter import snapshot


class SnrBoundsTests(unittest.TestCase):
    def test_independent_inclusive_bounds_same_spotter_and_negative_zero_missing(self):
        for low, high, readings, expected in ((None, 10, (5, 25), True), (None, 10, (11, 25), False),
                (10, 20, (5, 25), False), (10, 20, (10, 25), True), (10, 20, (5, 20), True),
                (10, 10, (10,), True), (10, 10, (11,), False), (None, 0, (-1, 0), True),
                (-5, 0, (-6, 1), False), (-5, 0, (-5,), True), (None, None, (), True), (None, 0, (), False)):
            with self.subTest(low=low, high=high, readings=readings):
                self.assertEqual(accepts_snr(snapshot(values=readings).rbn, low, high), expected)
        for low, high in ((20, 10), (None, 101), (None, True), (None, -101)):
            with self.assertRaises(ValueError): SnrFilter(low, maximum=high)
        state = Monitor(cli.arguments(["--min-snr", "10", "--max-snr", "20", "--filter-spotters", "SQ5J"]))
        self.assertEqual(state.apply(snapshot(values=(15, 25)), NOW), [])
        self.assertEqual(len(state.apply(snapshot(NOW + 10, (30, 20)), NOW + 10)), 1)

    def test_maximum_keeps_rechecking_strong_reports_then_alerts_once_and_keeps_actual_activity(self):
        args = cli.arguments(["--max-snr", "10", "--show-filtered"])
        state, poller = Monitor(args), Poller(args)
        clock, snr, requests = [NOW], [25], []
        def fetch(calls, *_):
            requests.append(calls); return snapshot(clock[0], (snr[0],)).rbn
        with patch("time.time", side_effect=lambda: clock[0]), \
                patch.object(poller, "read_source", side_effect=lambda _: (snapshot(clock[0]).spots, 0, set())), \
                patch.object(poller.rbn, "fetch", side_effect=fetch):
            for delta, strength, count in ((0, 25, 0), (60, 10, 1), (120, 30, 0), (180, 8, 0)):
                clock[0], snr[0] = NOW + delta, strength
                self.assertEqual(len(state.apply(poller.fetch(), clock[0])), count)
                self.assertEqual(next(iter(state.entries.values())).created_at, NOW)
                if delta == 0:
                    self.assertIsNotNone(next(iter(state.entries.values())).active_match(NOW))
                    self.assertIn("FILTERED:dB", views.render(state, NOW))
                    self.assertNotIn("ALERT:", views.render(state, NOW))
        self.assertEqual(requests, [["G4YBU/P"]] * 4)
        self.assertEqual(next(iter(state.entries.values())).match.receptions[0].snr, 8)

    def test_relaxing_maximum_is_quiet_and_does_not_change_retention_or_colours(self):
        state = Monitor(cli.arguments(["--max-snr", "10", "--show-filtered"]))
        state.apply(snapshot(values=(25,)), NOW)
        with patch("cw_monitor.monitor.save_filter", return_value=""):
            state.set_filter(SnrFilter(maximum=30))
        self.assertTrue(next(iter(state.entries.values())).alerted)
        self.assertEqual(state.apply(snapshot(NOW + 10, (28,)), NOW + 10), [])
        self.assertEqual([len(g) for g in state.rows(NOW + 311)], [0, 1])
        self.assertEqual(state.args.snr_thresholds, (10, 20))

    def test_pending_and_announced_maximum_state_survives_csv_restart(self):
        clock = [NOW]
        with tempfile.TemporaryDirectory() as directory, patch("time.time", side_effect=lambda: clock[0]):
            events = Path(directory)/"events.csv"
            def args(): return cli.arguments(["--max-snr", "10", "--log-file", str(events)])
            for delta, strength, count in ((0, 25, 0), (10, 10, 1), (20, 8, 0)):
                clock[0] = NOW + delta
                current = args()
                try:
                    state = Monitor(current)
                    self.assertEqual(len(state.apply(snapshot(NOW + delta, (strength,)), NOW + delta)), count)
                finally: session.close_logs(current)
            with events.open(newline="") as source: records = list(csv.DictReader(source))
            self.assertEqual(sum(r["event"] == "NEW" for r in records), 1)
            self.assertEqual(sum(r["event"] == "ALERT" for r in records), 1)


class SnrBoundsPreferencesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        env = patch.dict(os.environ, XDG_CONFIG_HOME=directory.name); env.start(); self.addCleanup(env.stop)

    def test_saved_bounds_are_independent_and_invalid_cli_or_saved_combinations_do_not_overwrite_preferences(self):
        for flags, expected in ((["--min-snr", "off", "--max-snr", "10"], (None, 10)),
                (["--min-snr", "5"], (5, 10)), ([], (5, 10)), (["--max-snr", "off"], (5, None))):
            args = cli.arguments(flags); preferences.configure_filter(args)
            self.assertEqual((args.min_snr, args.max_snr), expected)
        old = preferences.filter_settings_path().read_bytes()
        with self.assertRaises(ValueError): preferences.configure_filter(cli.arguments(["--max-snr", "4"]))
        self.assertEqual(preferences.filter_settings_path().read_bytes(), old)
        for flags in (["--min-snr", "20", "--max-snr", "10"], ["--max-snr", "101"], ["--max-snr", "nan"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit): cli.arguments(flags)
        preferences.filter_settings_path().write_text('{"minimum":15}')
        self.assertEqual(preferences.load_filter(), (SnrFilter(15), ""))
        for text in ('{"maximum":true}', '{"minimum":20,"maximum":10}'):
            preferences.filter_settings_path().write_text(text)
            self.assertIn("Cannot read", preferences.load_filter()[1])

    def test_menu_switches_bounds_disables_independently_rejects_reversed_range_and_fits_40x8(self):
        menu = FilterMenu(SnrFilter(15))
        for char in "u10": menu.handle(ord(char))
        self.assertIsNone(menu.handle(10)); self.assertFalse(menu.closed)
        self.assertIn("Minimum", menu.error)
        frame = menu.overlay([("", "normal")] * 8, 8, 40)
        self.assertEqual(len(frame), 8); self.assertTrue(all(len(text) < 40 for text, _ in frame))
        for char in "lo": menu.handle(ord(char))
        self.assertEqual(menu.handle(10), SnrFilter(maximum=10))
        menu = FilterMenu(SnrFilter(5, maximum=10))
        menu.handle(9); menu.handle(ord("o"))
        self.assertEqual(menu.handle(10), SnrFilter(5))
        cancelled = FilterMenu(SnrFilter(maximum=10))
        for char in "u20": cancelled.handle(ord(char))
        self.assertIsNone(cancelled.handle(27)); self.assertTrue(cancelled.closed)
        malformed = FilterMenu(SnrFilter(maximum=10))
        for char in "--1": malformed.handle(ord(char))
        self.assertIsNone(malformed.handle(10)); self.assertFalse(malformed.closed)
        self.assertIn("whole dB", malformed.error)

    def test_cli_maximum_only_persists_across_real_processes(self):
        command = [sys.executable, "pota_alert.py", "--demo", "--qth", "KO02KF"]
        for flags in (["--min-snr", "off", "--max-snr", "10"], []):
            result = subprocess.run(command+flags, cwd=Path(__file__).resolve().parents[1],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("YO5BDL", result.stdout); self.assertNotIn("IU1PZC", result.stdout)
            self.assertIn("Filters: On", result.stdout)
