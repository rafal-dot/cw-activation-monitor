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

from cw_monitor import cli, models
from cw_monitor.filtering import SnrFilter, accepts_snr
from cw_monitor.monitor import Monitor
from cw_monitor.polling import Poller
from cw_monitor.storage import preferences, session
from cw_monitor.ui import views
from cw_monitor.ui.filter_menu import FilterMenu
from test_pota_alert import NOW


def snapshot(when=NOW, values=(8,), frequency=14044.0, reference="GB-0001"):
    spot = models.ActivationSpot("G4YBU/P", reference, frequency, when)
    heard = [models.RbnSpot(spot.call, receiver, frequency, when, snr, 20)
             for receiver, snr in zip(("SP5GQ", "SQ5J"), values)]
    return models.Snapshot([spot], heard, [], when)


class SnrFilterTests(unittest.TestCase):
    def setUp(self):
        self.args = cli.arguments(["--min-snr", "15", "--qth", "KO02KF", "--programs", "POTA"])
        self.state = Monitor(self.args)

    def test_threshold_is_inclusive_any_spotter_qualifies_and_negative_zero_are_valid(self):
        for minimum, values, expected in ((15, (8, 18), True), (15, (8, 15), True),
                                           (15, (8, 14), False), (0, (-3, 0), True),
                                           (-5, (-8, -6), False), (-5, (-8, -5), True)):
            with self.subTest(minimum=minimum, values=values):
                self.assertEqual(accepts_snr(snapshot(values=values).rbn, minimum), expected)
        self.assertFalse(accepts_snr([], 0))
        self.assertTrue(accepts_snr([], None))

    def test_weak_record_is_saved_active_and_hidden_then_alerts_once_on_improvement(self):
        self.assertEqual(self.state.apply(snapshot(), NOW), [])
        entry = next(iter(self.state.entries.values()))
        self.assertIsNotNone(entry.active_match(NOW))
        self.assertFalse(entry.archived)
        self.assertFalse(entry.alerted)
        self.assertEqual(self.state.rows(NOW), ([], []))
        self.assertEqual(self.state.filtered_count(NOW), 1)
        self.assertEqual(len(self.state.apply(snapshot(NOW + 10, (15,)), NOW + 10)), 1)
        self.assertTrue(entry.alerted)
        self.assertEqual(entry.created_at, NOW)
        self.assertEqual(len(self.state.rows(NOW + 10)[0]), 1)
        self.assertEqual(self.state.apply(snapshot(NOW + 20, (4,)), NOW + 20), [])
        self.assertEqual(self.state.rows(NOW + 20), ([], []))
        self.assertEqual(self.state.apply(snapshot(NOW + 30, (20,)), NOW + 30), [])
        self.assertEqual(len(self.state.entries), 1)

    def test_peek_shows_weak_in_its_actual_section_without_banner_or_alert(self):
        with patch("cw_monitor.monitor.save_filter", return_value=""):
            self.state.set_filter(SnrFilter(15, True))
        self.assertEqual(self.state.apply(snapshot(), NOW), [])
        self.assertEqual([len(g) for g in self.state.rows(NOW)], [1, 0])
        rendered = views.render(self.state, NOW)
        self.assertIn("G4YBU/P", rendered)
        self.assertIn("ACTIVE FILTERED", rendered)
        self.assertIn("Filtered (shown): 1", rendered)
        self.assertNotIn("ALERT:", rendered)
        self.assertNotIn(">>> NEW", rendered)
        frame = views.screen_frame(self.state, NOW, 30, 120)
        self.assertFalse(any(style == "new" for _, style in frame))
        self.assertEqual([len(g) for g in self.state.rows(NOW + 301)], [0, 1])
        self.assertEqual(self.state.rows(NOW + 2101), ([], []))

    def test_lowering_or_disabling_filter_reveals_known_records_without_later_alert_storm(self):
        for value in (SnrFilter(5), SnrFilter()):
            with self.subTest(value=value):
                state = Monitor(cli.arguments(["--min-snr", "15"]))
                self.assertEqual(state.apply(snapshot(), NOW), [])
                with patch("cw_monitor.monitor.save_filter", return_value=""):
                    state.set_filter(value)
                self.assertTrue(next(iter(state.entries.values())).alerted)
                self.assertEqual(state.apply(snapshot(NOW + 10, (20,)), NOW + 10), [])

    def test_colours_keep_their_own_thresholds_and_both_spotter_values(self):
        self.state.apply(snapshot(values=(8, 18)), NOW)
        frame = views.screen_frame(self.state, NOW + 31, 30, 200)
        row = next(text for text, _ in frame if text.startswith("POTA") and "G4YBU/P" in text)
        spans = [(row[a:b], colour) for a, b, colour in row.color_spans]
        self.assertIn(("8", "red"), spans)
        self.assertIn(("18", "yellow"), spans)
        self.assertEqual(self.args.snr_thresholds, (10, 20))

    def test_different_band_and_reference_keep_separate_first_alerts(self):
        self.state.apply(snapshot(), NOW)
        self.assertEqual(len(self.state.apply(snapshot(NOW + 10, (20,), 7032.0), NOW + 10)), 1)
        self.assertEqual(len(self.state.apply(snapshot(NOW + 20, (20,), reference="GB-0002"), NOW + 20)), 1)
        self.assertEqual(len(self.state.entries), 3)

    def test_rbn_improvement_uses_valid_cached_source_but_restored_unknown_needs_validation(self):
        for awaiting, alerts in ((False, 1), (True, 0)):
            state = Monitor(cli.arguments(["--min-snr", "15"]))
            state.apply(snapshot(), NOW)
            entry = next(iter(state.entries.values()))
            entry.awaiting_source = awaiting
            data = models.Snapshot(snapshot().spots, snapshot(NOW + 30, (20,)).rbn, [], NOW + 30,
                                   failed_programs={"POTA"})
            self.assertEqual(len(state.apply(data, NOW + 30)), alerts)
            self.assertEqual(entry.spot_time, NOW)
            self.assertEqual(entry.spot_deadlines, {"GB-0001": NOW + 300})

    def test_polling_rechecks_known_station_and_latest_weak_report_replaces_old_strong_reading(self):
        poller = Poller(self.args)
        self.args.interval = 60
        current = [NOW]
        values = [8]
        requests = []
        def source(program):
            return snapshot(current[0], (values[0],)).spots, 0, set()
        def fetch(calls, *_):
            requests.append(calls)
            return snapshot(current[0], (values[0],)).rbn
        with patch("time.time", side_effect=lambda: current[0]), patch.object(poller, "read_source", side_effect=source), \
                patch.object(poller.rbn, "fetch", side_effect=fetch):
            for delta, snr, alerts in ((0, 8, 0), (60, 15, 1), (120, 4, 0), (180, 25, 0)):
                current[0], values[0] = NOW + delta, snr
                data = poller.fetch()
                self.assertEqual(len(self.state.apply(data, current[0])), alerts)
                self.assertEqual(data.rbn[0].snr, snr)
        self.assertEqual(requests, [["G4YBU/P"]] * 4)


class FilterPreferencesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        env = patch.dict(os.environ, XDG_CONFIG_HOME=str(self.root))
        env.start(); self.addCleanup(env.stop)

    def test_persistence_explicit_off_and_sort_settings_remain_independent(self):
        from cw_monitor.sorting import SortOrder
        preferences.save_sort(SortOrder("wpm", "asc"))
        for flags, expected in ((["--min-snr", "15", "--show-filtered"], SnrFilter(15, True)),
                                 ([], SnrFilter(15, True)), (["--min-snr", "off", "--no-show-filtered"], SnrFilter())):
            args = cli.arguments(flags)
            preferences.configure_filter(args)
            self.assertEqual(SnrFilter(args.min_snr, args.show_filtered), expected)
        self.assertEqual(preferences.load_sort()[0], SortOrder("wpm", "asc"))

    def test_corrupt_settings_fall_back_and_failed_save_keeps_previous_bytes(self):
        path = preferences.filter_settings_path()
        path.parent.mkdir(parents=True)
        for text in ('{"minimum":true}', '{"minimum":101}', '{"show_filtered":"yes"}', '[]', 'bad'):
            path.write_text(text)
            value, error = preferences.load_filter()
            self.assertEqual(value, SnrFilter())
            self.assertIn("Cannot read", error)
            self.assertEqual(path.read_text(), text)
        preferences.save_filter(SnrFilter(15))
        old = path.read_bytes()
        with patch.object(preferences.os, "replace", side_effect=PermissionError("read only")):
            self.assertIn("Cannot save", preferences.save_filter(SnrFilter(20)))
        self.assertEqual(path.read_bytes(), old)

    def test_cli_rejects_invalid_values_and_reloads_filter_in_another_process(self):
        for value in ("101", "-101", "nan", "15.5"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.arguments(["--min-snr", value])
        command = [sys.executable, "pota_alert.py", "--demo", "--qth", "KO02KF"]
        for flags in (["--min-snr", "15"], []):
            result = subprocess.run(command + flags, cwd=Path(__file__).resolve().parents[1],
                                    capture_output=True, text=True, encoding="utf-8", timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Min: 15 dB", result.stdout)
            self.assertNotIn("YO5BDL", result.stdout)

    def test_menu_validates_signed_threshold_applies_peek_and_cancels(self):
        menu = FilterMenu(SnrFilter(15))
        for char in "-5v": menu.handle(ord(char))
        self.assertEqual(menu.handle(10), SnrFilter(-5, True))
        bad = FilterMenu(SnrFilter())
        for char in "101": bad.handle(ord(char))
        self.assertIsNone(bad.handle(10)); self.assertFalse(bad.closed)
        frame = bad.overlay([("", "normal")] * 8, 8, 40)
        self.assertEqual(len(frame), 8)
        self.assertTrue(all(len(text) < 40 for text, _ in frame))
        self.assertIsNone(bad.handle(27)); self.assertTrue(bad.closed)

    def test_pending_and_announced_state_restore_from_csv_without_changing_columns(self):
        events = self.root / "events.csv"
        daily = self.root / "daily.csv"
        def args(): return cli.arguments(["--programs", "POTA", "--min-snr", "15", "--log-file", str(events), "--summary-file", str(daily)])
        clock = [NOW]
        with patch("time.time", side_effect=lambda: clock[0]):
            original_args = args()
            original = Monitor(original_args)
            self.assertEqual(original.apply(snapshot(), NOW), [])
            session.close_logs(original_args)
            second_args = args()
            second = Monitor(second_args)
            self.assertFalse(next(iter(second.entries.values())).alerted)
            clock[0] += 10
            self.assertEqual(len(second.apply(snapshot(clock[0], (20,)), clock[0])), 1)
            session.close_logs(second_args)
            third_args = args()
            third = Monitor(third_args)
            self.assertTrue(next(iter(third.entries.values())).alerted)
            clock[0] += 10
            self.assertEqual(third.apply(snapshot(clock[0], (25,)), clock[0]), [])
            session.close_logs(third_args)
        with events.open(newline="") as source:
            records = list(csv.DictReader(source))
        self.assertEqual(sum(r["event"] == "NEW" for r in records), 1)
        self.assertEqual(sum(r["event"] == "ALERT" for r in records), 1)
        self.assertIn("snr-filter=pending", records[0]["detail"])
        with daily.open(newline="") as source:
            self.assertEqual(len(list(csv.DictReader(source))), 1)
