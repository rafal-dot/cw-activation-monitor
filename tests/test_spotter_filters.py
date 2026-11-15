import contextlib
import curses
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cw_monitor import cli, models
from cw_monitor.filtering import SnrFilter, filter_reasons
from cw_monitor.monitor import Monitor
from cw_monitor.polling import Poller
from cw_monitor.storage import preferences, session
from cw_monitor.ui import views
from cw_monitor.ui.filter_menu import FilterMenu
from test_pota_alert import NOW
from test_snr_filter import snapshot


class SpotterFilterTests(unittest.TestCase):
    def test_any_chosen_spotter_must_also_meet_snr_and_source_band(self):
        value = SnrFilter(15, bands=frozenset(("20m",)), spotters=frozenset(("SQ5J",)))
        for program in ("POTA", "SOTA", "WWFF"):
            for readings, expected in (((30, 8), False), ((8, 15), True), ((30,), False)):
                state = Monitor(cli.arguments(["--demo", "--min-snr", "15", "--bands", "20m", "--filter-spotters", "SQ5J"]))
                data = snapshot(values=readings)
                match = models.Match("G4YBU/P", ("GB-0001",), 14044, data.rbn, program=program)
                self.assertEqual(value.accepts(match), expected)
                self.assertEqual(bool(state.apply(data, NOW)), expected)
        both = SnrFilter(15, spotters=frozenset(("SP5GQ", "SQ5J")))
        self.assertTrue(both.accepts(models.Match("G4YBU/P", ("GB-0001",), 14044, snapshot(values=(8, 15)).rbn)))
        missing = models.Match("G4YBU/P", ("GB-0001",), 7032, snapshot(values=(30,)).rbn)
        self.assertEqual(filter_reasons(missing, 15, frozenset(("20m",)), frozenset(("SQ5J",))), ("band", "spotter", "dB"))
        self.assertFalse(SnrFilter(spotters=frozenset(("SQ5J",))).accepts(missing))

    def test_requery_without_snr_first_selected_reception_alerts_once_and_preserves_all_readings(self):
        args = cli.arguments(["--filter-spotters", "SQ5J", "--show-filtered"])
        state, poller = Monitor(args), Poller(args)
        clock, values, calls = [NOW], [(30,)], []
        def fetch(requested, *_):
            calls.append(requested)
            return snapshot(clock[0], values[0]).rbn
        with patch("time.time", side_effect=lambda: clock[0]), \
                patch.object(poller, "read_source", side_effect=lambda _: (snapshot(clock[0]).spots, 0, set())), \
                patch.object(poller.rbn, "fetch", side_effect=fetch):
            for delta, readings, alerts in ((0, (30,), 0), (60, (30, 8), 1), (120, (35, 9), 0)):
                clock[0], values[0] = NOW + delta, readings
                self.assertEqual(len(state.apply(poller.fetch(), clock[0])), alerts)
                if delta == 0:
                    self.assertIn("FILTERED:spotter", views.render(state, clock[0]))
                    self.assertNotIn("ALERT:", views.render(state, clock[0]))
        self.assertEqual(calls, [["G4YBU/P"]] * 3)
        entry = next(iter(state.entries.values()))
        self.assertEqual([r.snr for r in entry.match.receptions], [35, 9])
        self.assertEqual(entry.created_at, NOW)
        self.assertEqual(state.filtered_count(clock[0]), 0)

    def test_selection_change_is_quiet_and_pending_survives_log_restart(self):
        with tempfile.TemporaryDirectory() as directory, patch("time.time", return_value=NOW), \
                patch("cw_monitor.monitor.save_filter", return_value=""):
            path = Path(directory) / "events.csv"
            def args(): return cli.arguments(["--filter-spotters", "SQ5J", "--log-file", str(path)])
            original_args = args()
            state = Monitor(original_args)
            self.assertEqual(state.apply(snapshot(values=(30,)), NOW), [])
            self.assertTrue(next(iter(state.entries.values())).active_match(NOW))
            session.close_logs(original_args)
            restored_args = args()
            try:
                restored = Monitor(restored_args)
                self.assertFalse(next(iter(restored.entries.values())).alerted)
                restored.set_filter(SnrFilter(spotters=frozenset(("SP5GQ",))))
                self.assertTrue(next(iter(restored.entries.values())).alerted)
                self.assertEqual(restored.apply(snapshot(NOW + 10, (30, 18)), NOW + 10), [])
            finally:
                session.close_logs(restored_args)
            self.assertIn("FILTER_ACK", path.read_text())


class SpotterFilterPreferencesTests(unittest.TestCase):
    def test_cli_persistence_normalization_and_old_defaults(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, XDG_CONFIG_HOME=directory):
            args = cli.arguments(["--bands", "20m", "--min-snr", "15", "--filter-spotters", "sq5j-#"])
            preferences.configure_filter(args)
            expected = SnrFilter(15, bands=frozenset(("20m",)), spotters=frozenset(("SQ5J",)))
            self.assertEqual(preferences.load_filter(), (expected, ""))
            args = cli.arguments([]); preferences.configure_filter(args)
            self.assertEqual(args.filter_spotters, expected.spotters)
            args = cli.arguments(["--spotters", "SP5GQ"]); preferences.configure_filter(args)
            self.assertIsNone(args.filter_spotters)
            self.assertIsNone(preferences.load_filter()[0].spotters)
            args = cli.arguments(["--filter-spotters", "all"]); preferences.configure_filter(args)
            self.assertIsNone(args.filter_spotters); self.assertEqual(args.bands, expected.bands)
            preferences.filter_settings_path().write_text('{"minimum":15}')
            self.assertEqual(preferences.load_filter(), (SnrFilter(15), ""))
            for flags in (["--filter-spotters", "all", "SQ5J"], ["--filter-spotters", "OTHER"], ["--filter-spotters", "!"]):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit): cli.arguments(flags)
            for text in ('{"spotters":[]}', '{"spotters":["bad!"]}', '{"spotters":"SQ5J"}'):
                preferences.filter_settings_path().write_text(text)
                self.assertIn("Cannot read", preferences.load_filter()[1])

    def test_menu_draft_multi_choice_empty_apply_cancel_all_and_small_terminal(self):
        initial = SnrFilter(15, bands=frozenset(("20m",)))
        menu = FilterMenu(initial, ["SP5GQ", "SQ5J"])
        menu.handle(ord("p")); menu.handle(curses.KEY_DOWN); menu.handle(ord(" "))
        self.assertIsNone(menu.handle(10))
        self.assertEqual(menu.spotters, frozenset(("SP5GQ",)))
        menu.handle(ord("p")); menu.handle(ord(" ")); menu.handle(10)
        self.assertFalse(menu.spotter_menu.closed)
        menu.handle(27)  # Cancel the empty draft, keep previous choice.
        frame = menu.overlay([("", "normal")] * 8, 8, 40)
        self.assertEqual(len(frame), 8); self.assertTrue(all(len(text) < 40 for text, _ in frame))
        self.assertEqual(menu.handle(10), SnrFilter(15, bands=initial.bands, spotters=frozenset(("SP5GQ",))))
        menu = FilterMenu(initial, ["SP5GQ", "SQ5J"])
        menu.handle(ord("p")); menu.handle(curses.KEY_DOWN); menu.handle(ord(" ")); menu.handle(ord("a")); menu.handle(10)
        self.assertEqual(menu.handle(10), initial)
