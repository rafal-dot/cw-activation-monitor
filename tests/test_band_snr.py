"""Per-band bounds are complete overrides, consistently applied to activation and Friends paths."""

import contextlib
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import curses

from cw_monitor import cli, models, polling
from cw_monitor.filtering import SnrFilter, filter_reasons
from cw_monitor.friends import Friend, FriendTracker
from cw_monitor.monitor import Monitor
from cw_monitor.snr import BandSnrRule, RECOGNIZED_BANDS, COMMON_BANDS, resolve_snr
from cw_monitor.storage import preferences
from cw_monitor.ui.filter_menu import FilterMenu
from cw_monitor.ui.band_snr_menu import BandSnrMenu
from cw_monitor.ui.table import row_values
from cw_monitor.ui.views import render, screen_frame
from test_pota_alert import NOW


def match(freq, *values):
    return models.Match("SP5TEST", ("PL-0001",), freq,
                        [models.RbnSpot("SP5TEST", receiver, freq, NOW, snr, 20)
                         for receiver, snr in zip(("SP5GQ", "SQ5J"), values)])


class BandSnrTests(unittest.TestCase):
    def setUp(self):
        self.rules = {"40m": BandSnrRule(False, 18, 24), "20m": BandSnrRule(True, 18),
                      "80m": BandSnrRule(True, None, 12), "10m": BandSnrRule(True)}
        self.filter = SnrFilter(10, maximum=30, per_band=self.rules)

    def test_overrides_and_menu_are_limited_to_common_toggle_bands(self):
        expected = ("160m", "80m", "60m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m", "2m", "70cm")
        self.assertEqual(COMMON_BANDS, expected)
        child = BandSnrMenu({}, 10, 30)
        for band in expected:
            self.assertEqual(child.band, band)
            self.assertIn(band, cli.arguments(["--band-snr", f"{band}:5:25"]).band_snr)
            child.handle(curses.KEY_DOWN)
        self.assertEqual(child.band, "70cm")
        frame = child.overlay([("", "normal")] * 40, 40, 100)
        self.assertEqual([text[2:].split()[0] for text, _ in frame if text.startswith("> ") or text.startswith("  ")], list(expected))
        for band in set(RECOGNIZED_BANDS) - set(expected):
            with self.assertRaises(ValueError):
                SnrFilter(per_band={band: BandSnrRule(True, 5, 25)})
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.arguments(["--band-snr", f"{band}:5:25"])
            self.assertEqual(resolve_snr(10, 30, {band: BandSnrRule(True, 5, 25)}, band), (10, 30))

    def test_old_outside_band_overrides_are_removed_without_losing_other_preferences(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "filter.json"
            payload = {"minimum": 10, "maximum": 30, "bands": ["23cm", "20m"], "band_snr": {
                band: {"enabled": True, "minimum": 5, "maximum": 25} for band in RECOGNIZED_BANDS}}
            path.write_text(json.dumps(payload))
            loaded, error = preferences.load_filter(path)
            self.assertEqual(error, "")
            self.assertEqual((loaded.minimum, loaded.maximum, loaded.bands), (10, 30, frozenset({"20m"})))
            self.assertEqual(set(loaded.per_band), set(COMMON_BANDS))
            self.assertEqual(preferences.save_filter(loaded, path), "")
            self.assertEqual(set(json.loads(path.read_text())["band_snr"]), set(COMMON_BANDS))

    def test_override_replaces_both_bounds_and_off_off_bypasses_globals(self):
        for freq, value, allowed in ((7032, 9, False), (7032, 10, True), (7032, 31, False),
                                     (14044, 17, False), (14044, 18, True), (14044, 50, True),
                                     (3530, 8, True), (3530, 12, True), (3530, 13, False), (28030, -90, True)):
            self.assertEqual(self.filter.accepts(match(freq, value)), allowed, (freq, value))
        self.assertEqual(resolve_snr(10, 30, self.rules, "unknown"), (10, 30))
        self.assertEqual(resolve_snr(None, None, self.rules, "20m"), (18, None))

    def test_one_selected_spotter_must_meet_both_and_source_frequency_selects_band(self):
        rule = SnrFilter(10, maximum=30, spotters=frozenset({"SQ5J"}), per_band={"20m": BandSnrRule(True, 15, 25)})
        self.assertFalse(rule.accepts(match(14044, 10, 30)))
        self.assertFalse(rule.accepts(match(14044, 20, 30)))
        self.assertTrue(rule.accepts(match(14044, 30, 15)))
        source = match(14044, 20)
        source.receptions = [replace(source.receptions[0], frequency=7032)]
        self.assertFalse(SnrFilter(10, per_band={"20m": BandSnrRule(True, 25)}).accepts(source))

    def test_rules_are_validated_and_copied_into_immutable_policy(self):
        for low, high in ((True, None), (18.5, None), (101, None), (20, 10)):
            with self.assertRaises(ValueError):
                BandSnrRule(False, low, high)
        with self.assertRaises(ValueError):
            BandSnrRule("yes")
        with self.assertRaises(ValueError):
            SnrFilter(per_band={"bad-band": BandSnrRule()})
        self.rules["20m"] = BandSnrRule()
        self.assertEqual(self.filter.per_band["20m"], BandSnrRule(True, 18))
        with self.assertRaises(TypeError):
            self.filter.per_band["20m"] = BandSnrRule()
        self.assertTrue(SnrFilter(per_band={"20m": BandSnrRule(True, 0, 0)}).accepts(match(14044, 0)))

    def test_cli_and_preferences_reload_and_global_mode_preserves_saved_own_bounds(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(preferences, "filter_settings_path", return_value=Path(directory) / "filter.json"):
            flags = ["--min-snr", "10", "--max-snr", "30", "--band-snr", "20m:18:off", "--band-snr", "80m:off:12"]
            first = cli.arguments(flags)
            preferences.configure_filter(first)
            reopened = cli.arguments([])
            preferences.configure_filter(reopened)
            self.assertEqual(dict(first.band_snr), dict(reopened.band_snr))
            self.assertEqual((reopened.min_snr, reopened.max_snr), (10, 30))
            inherited = cli.arguments(["--band-snr", "20m:global"])
            preferences.configure_filter(inherited)
            self.assertEqual(inherited.band_snr["20m"], BandSnrRule(False, 18))
            self.assertEqual(inherited.band_snr["80m"], BandSnrRule(True, None, 12))
            preferences.save_filter(self.filter)
            loaded, error = preferences.load_filter()
            self.assertEqual(error, ""); self.assertEqual(loaded, self.filter)
            path = preferences.filter_settings_path()
            path.write_text('{"minimum":10,"maximum":30}')
            self.assertEqual(preferences.load_filter()[0], SnrFilter(10, maximum=30))
            for bad in (True, 18.5, 101):
                payload = {"band_snr": {"20m": {"enabled": True, "minimum": bad, "maximum": None}}}
                text = json.dumps(payload)
                path.write_text(text)
                loaded, error = preferences.load_filter()
                self.assertEqual(loaded, SnrFilter()); self.assertIn("Cannot read", error)
                self.assertEqual(path.read_text(), text)

    def test_invalid_cli_inputs_fail_before_network(self):
        for text in ("bad:10:20", "20m:101:off", "20m:18.5:off", "20m:30:10", "20m:yes", "20m:10"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.arguments(["--band-snr", text])

    def test_changing_policy_reveals_entries_quietly_and_rendering_has_same_filter_results(self):
        args = cli.arguments(["--programs", "POTA", "--min-snr", "10", "--band-snr", "20m:25:off"])
        state = Monitor(args)
        spot = models.ActivationSpot("SP5TEST", "PL-0001", 14044, NOW)
        snapshot = models.Snapshot([spot], match(14044, 18).receptions, [], NOW)
        self.assertEqual(state.apply(snapshot, NOW), [])
        self.assertEqual(state.rows(NOW)[0], [])
        shown = SnrFilter(10, per_band={"20m": BandSnrRule(True, 15)})
        with patch.object(preferences, "filter_settings_path", return_value=Path(tempfile.gettempdir()) / "cw-band-snr-test-filter.json"), \
             patch("cw_monitor.monitor.save_filter", return_value=""), patch.object(time, "time", return_value=NOW + 1):
            state.set_filter(shown)
        self.assertEqual(args.band_snr, shown.per_band)
        self.assertTrue(next(iter(state.entries.values())).alerted)
        self.assertEqual(state.apply(snapshot, NOW + 2), [])
        self.assertEqual(len(state.rows(NOW + 2)[0]), 1)
        self.assertIn("SP5TEST", render(state, NOW + 2))
        self.assertNotIn("FILTERED:dB", row_values(*state.rows(NOW + 2)[0][0], NOW + 2, minimum_snr=10, per_band=shown.per_band)[11])
        frame = screen_frame(state, NOW + 2, 35, 160)
        self.assertTrue(any("SNR bands: 1" in text for text, _ in frame))

    def test_friends_use_rbn_band_and_known_readings_are_acknowledged_quietly(self):
        args = cli.arguments(["--band-snr", "20m:25:off", "--min-snr", "10", "--band-snr", "80m:off:12"])
        args.friends, args.friends_alerts = [Friend("SP5TEST")], True
        tracker = FriendTracker()
        weak = match(14044, 18).receptions
        self.assertEqual(tracker.observe(weak, NOW, args), [])
        args.band_snr = {"20m": BandSnrRule(True, 15), "80m": BandSnrRule(True, None, 12)}
        tracker.acknowledge_filters(NOW + 1, args)
        self.assertEqual(tracker.observe([replace(r, time=NOW + 2) for r in weak], NOW + 2, args), [])
        self.assertEqual(len(tracker.observe([replace(r, time=NOW + 3) for r in match(3530, 8).receptions], NOW + 3, args)), 1)
        self.assertEqual(tracker.observe([replace(r, time=NOW + 4) for r in match(7032, 8).receptions], NOW + 4, args), [])

    def test_all_active_bands_stay_in_shared_stream_regardless_of_snr_filters(self):
        args = cli.arguments(["--programs", "POTA", "--min-snr", "10", "--band-snr", "20m:off:off"])
        client = polling.Poller(args)
        spots = [models.ActivationSpot(call, "PL-0001", freq, NOW) for call, freq in (("TWENTY", 14044), ("FORTY", 7032))]
        for spot in spots:
            client.confirmed[spot.key] = [models.RbnSpot(spot.call, "SP5GQ", spot.frequency, NOW, 18, 20)]
        with patch.object(client, "read_source", return_value=(spots, 0, set())), patch.object(time, "time", return_value=NOW), \
             patch.object(client.rbn, "fetch", return_value=[]) as query:
            client.fetch()
        self.assertEqual(query.call_args.args[0], ["FORTY", "TWENTY"])
        self.assertEqual(client.confirmed[spots[0].key][0].time, NOW)

    def test_curses_band_editor_keeps_drafts_validates_and_fits_small_terminals(self):
        parent = FilterMenu(SnrFilter(10, maximum=30))
        parent.handle(ord("x"))
        child = parent.snr_menu
        child.cursor = COMMON_BANDS.index("20m")
        parent.handle(10)
        parent.handle(ord("e"))
        for char in "18": parent.handle(ord(char))
        parent.handle(10)
        self.assertFalse(child.editing)
        parent.handle(ord("a"))
        self.assertEqual(parent.per_band["20m"], BandSnrRule(True, 18))
        value = parent.handle(10)
        self.assertEqual(value.per_band["20m"], BandSnrRule(True, 18))
        self.assertTrue(value.accepts(match(14044, 50)))
        child = BandSnrMenu(self.rules, 10, 30)
        child.cursor = COMMON_BANDS.index("20m")
        child.handle(10)
        for char in "101": child.handle(ord(char))
        child.handle(10)
        self.assertTrue(child.editing)
        self.assertTrue(child.error)
        for size in (8, 12, 24):
            frame = child.overlay([("", "normal")] * size, size, 40)
            self.assertEqual(len(frame), size)
            self.assertTrue(all(len(text) <= 40 for text, _ in frame))
        child.handle(27); child.handle(ord("g"))
        self.assertTrue(all(not rule.enabled for rule in child.rules.values()))
        self.assertEqual(child.rules["20m"].minimum, 18)
        cancelled = FilterMenu(value)
        cancelled.handle(ord("x")); cancelled.handle(ord("g")); cancelled.handle(27)
        self.assertEqual(cancelled.per_band["20m"], BandSnrRule(True, 18))
