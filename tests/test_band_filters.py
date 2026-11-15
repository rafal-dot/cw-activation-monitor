import contextlib
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from cw_monitor import cli, models
from cw_monitor.filtering import BAND_LABELS, SnrFilter, filter_reasons
from cw_monitor.config import COMMON_BANDS
from cw_monitor.snr import RECOGNIZED_BANDS
from cw_monitor.monitor import Monitor
from cw_monitor.sorting import SortOrder
from cw_monitor.storage import preferences, session
from cw_monitor.ui import views
from cw_monitor.ui.band_menu import BandMenu
from cw_monitor.ui.filter_menu import FilterMenu
from test_pota_alert import NOW


def snapshot(when=NOW, khz=14044.0, snr=18, program="POTA"):
    spot = models.ActivationSpot("G4YBU/P", "GB-0001", khz, when, program=program)
    return models.Snapshot([spot], [models.RbnSpot(spot.call, "SP5GQ", khz, when, snr, 20)], [], when)


class BandFilteringTests(unittest.TestCase):
    def test_multi_band_selection_and_snr_are_combined_using_source_frequency(self):
        filters = SnrFilter(15, bands=frozenset(("20m", "40m")))
        for program in ("POTA", "SOTA", "WWFF"):
            state = Monitor(cli.arguments(["--min-snr", "15", "--bands", "20m", "40m", "--demo"]))
            for khz, snr, accepted in ((14044, 18, True), (7032, 18, True), (18083, 30, False), (14044, 8, False)):
                match = models.Match("G4YBU/P", ("GB-0001",), khz,
                                     [models.RbnSpot("G4YBU/P", "SP5GQ", 99999, NOW, snr, 20)], program=program)
                self.assertEqual(filters.accepts(match), accepted, (program, khz, snr))
            state.apply(snapshot(program=program), NOW)
            self.assertEqual(len(state.rows(NOW)[0]), 1)
        unknown = models.Match("G4YBU/P", ("GB-0001",), 15000, [])
        self.assertTrue(SnrFilter().accepts(unknown))
        self.assertFalse(SnrFilter(bands=frozenset(("20m",))).accepts(unknown))

    def test_hidden_records_keep_activity_history_logging_and_change_bands_silently(self):
        with tempfile.TemporaryDirectory() as directory, patch("time.time", return_value=NOW):
            args = cli.arguments(["--bands", "40m", "--log-file", str(Path(directory)/"events.csv"),
                                  "--summary-file", str(Path(directory)/"daily.csv")])
            self.addCleanup(session.close_logs, args)
            state = Monitor(args)
            self.assertEqual(state.apply(snapshot(), NOW), [])
            entry = next(iter(state.entries.values()))
            self.assertIsNotNone(entry.active_match(NOW)); self.assertFalse(entry.archived)
            self.assertEqual(state.filtered_count(NOW), 1)
            self.assertEqual(state.rows(NOW), ([], []))
            self.assertEqual(len(state.summary.rows), 1)
            with patch("cw_monitor.monitor.save_filter", return_value=""):
                state.set_filter(SnrFilter(bands=frozenset(("20m", "40m"))))
            self.assertTrue(entry.alerted)
            self.assertEqual(state.apply(snapshot(NOW+10), NOW+10), [])
            self.assertEqual(len(state.rows(NOW+10)[0]), 1)
            self.assertEqual(entry.created_at, NOW)
            self.assertEqual(len(state.rows(NOW+311)[1]), 1)
            self.assertIn("FILTER_ACK", [r["event"] for r in state.log.records()])

    def test_peek_reports_each_reason_without_double_counting_or_alerts(self):
        state = Monitor(cli.arguments(["--bands", "40m", "--min-snr", "15", "--show-filtered"]))
        self.assertEqual(state.apply(snapshot(snr=8), NOW), [])
        match = next(iter(state.entries.values())).match
        self.assertEqual(filter_reasons(match, 15, frozenset(("40m",))), ("band", "dB"))
        self.assertEqual(state.filtered_count(NOW), 1)
        self.assertEqual([len(g) for g in state.rows(NOW)], [1, 0])
        output = views.render(state, NOW)
        self.assertIn("FILTERED:band+dB", output)
        self.assertNotIn("ALERT:", output)
        self.assertNotIn(">>> NEW", output)
        self.assertFalse(any(style == "new" for _, style in views.screen_frame(state, NOW, 30, 120)))

    def test_new_selected_band_is_a_separate_alert_and_outside_band_gets_no_new_priority(self):
        state = Monitor(cli.arguments(["--bands", "20m", "--show-filtered"]))
        self.assertEqual(state.apply(snapshot(khz=7032), NOW), [])
        new = state.apply(snapshot(NOW+10), NOW+10)
        self.assertEqual(len(new), 1); self.assertEqual(len(state.entries), 2)
        self.assertEqual(new[0].frequency, 14044)
        state.sort = SortOrder("frequency", "asc")
        self.assertEqual([m.frequency for _, m in state.rows(NOW+10)[0]], [7032, 14044])


class BandPreferencesAndMenuTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        env = patch.dict(os.environ, XDG_CONFIG_HOME=str(self.root))
        env.start(); self.addCleanup(env.stop)

    def test_band_and_frequency_choices_persist_and_all_resets_only_bands(self):
        args = cli.arguments(["--bands", "20m", "40m", "--min-snr", "15", "--sort", "frequency"])
        preferences.configure_filter(args); preferences.configure_sort(args)
        args = cli.arguments([])
        preferences.configure_filter(args); preferences.configure_sort(args)
        self.assertEqual(args.bands, frozenset(("20m", "40m")))
        self.assertEqual(args.min_snr, 15)
        self.assertEqual((args.sort, args.sort_order), ("frequency", "asc"))
        args = cli.arguments(["--bands", "all"])
        preferences.configure_filter(args)
        self.assertIsNone(args.bands); self.assertEqual(args.min_snr, 15)
        path = preferences.filter_settings_path()
        path.write_text('{"minimum":15,"show_filtered":true}')
        self.assertEqual(preferences.load_filter()[0], SnrFilter(15, True))

    def test_cli_and_menu_offer_only_the_common_160m_to_70cm_bands(self):
        expected = ("160m", "80m", "60m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m", "2m", "70cm")
        self.assertEqual(BAND_LABELS, expected)
        self.assertEqual(BAND_LABELS, COMMON_BANDS)
        for band in expected:
            self.assertEqual(cli.arguments(["--bands", band]).bands, frozenset({band}))
        for band in set(RECOGNIZED_BANDS) - set(expected):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.arguments(["--bands", band])
            with self.assertRaises(ValueError):
                SnrFilter(bands=frozenset({band}))
        menu = BandMenu(None)
        frame = menu.overlay([("", "normal")] * 30, 30, 80)
        for band in expected:
            self.assertTrue(any(text.split() and band == text.split()[-1] for text, _ in frame), band)
        self.assertFalse(any("23cm" in text or "2190m" in text for text, _ in frame))

    def test_older_band_choices_are_migrated_without_losing_other_filters(self):
        path = self.root / "filter.json"
        for selected, expected in ((["20m", "23cm", "2190m"], frozenset({"20m"})), (["23cm"], None)):
            path.write_text(json.dumps({"minimum": 12, "maximum": 28, "show_filtered": True,
                "bands": selected, "spotters": ["SQ5J"]}))
            restored, error = preferences.load_filter(path)
            self.assertEqual(error, "")
            self.assertEqual(restored, SnrFilter(12, True, expected, frozenset({"SQ5J"}), 28))
            self.assertEqual(preferences.save_filter(restored, path), "")
            self.assertEqual(preferences.load_filter(path), (restored, ""))

    def test_invalid_empty_unknown_and_mixed_all_selections_are_rejected(self):
        for value in (frozenset(), frozenset(("bad",))):
            with self.assertRaises(ValueError): SnrFilter(bands=value)
        for flags in (["--bands"], ["--bands", "bad"], ["--bands", "all", "20m"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit): cli.arguments(flags)
        path = preferences.filter_settings_path()
        path.parent.mkdir(parents=True)
        for value in ('[]', '["bad"]', '"20m"', '[null]'):
            path.write_text('{"minimum":15,"bands":'+value+'}')
            self.assertIn("Cannot read", preferences.load_filter()[1])

    def test_scrollable_band_menu_preserves_drafts_apply_cancel_all_and_empty_validation(self):
        menu = FilterMenu(SnrFilter(15))
        menu.handle(ord("b"))
        bands = menu.band_menu
        for band in ("20m", "40m"):
            bands.cursor = 1+BAND_LABELS.index(band)
            bands.handle(ord(" "))
        frame = menu.overlay([("", "normal")]*8, 8, 40)
        self.assertEqual(len(frame), 8)
        self.assertTrue(all(len(text)<40 for text, _ in frame))
        menu.handle(10)
        self.assertEqual(menu.handle(10), SnrFilter(15, bands=frozenset(("20m", "40m"))))
        menu = FilterMenu(SnrFilter(bands=frozenset(("20m",))))
        menu.handle(ord("b")); menu.band_menu.handle(ord("a")); menu.handle(27)
        self.assertEqual(menu.bands, frozenset(("20m",)))
        menu.handle(ord("b")); menu.handle(ord("a")); menu.handle(10)
        self.assertIsNone(menu.bands)
        empty = BandMenu(frozenset(("20m",)))
        empty.handle(ord(" ")); empty.handle(10)
        self.assertFalse(empty.closed); self.assertTrue(empty.error)

    def test_real_cli_applies_both_features_and_restores_them_in_another_process(self):
        command = [sys.executable, "pota_alert.py", "--demo", "--qth", "KO02KF"]
        for flags in (["--bands", "20m", "40m", "--sort", "frequency"], []):
            result = subprocess.run(command+flags, cwd=Path(__file__).resolve().parents[1],
                                    capture_output=True, text=True, encoding="utf-8", timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Sort: Frequency ↑", result.stdout)
            active = result.stdout.split("HISTORY (")[0]
            calls = [line.split("|")[1].strip() for line in active.splitlines()
                     if line.startswith(("POTA ", "SOTA ", "WWFF "))]
            self.assertEqual(calls, ["SP9DEMO/P", "SP8DEMO/P", "IU1PZC", "YO5BDL"])
