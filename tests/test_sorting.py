import contextlib
from dataclasses import replace
import io
import os
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
from unittest.mock import patch

from cw_monitor import cli, models
from cw_monitor.monitor import Monitor
from cw_monitor.sorting import SortOrder, ordered_rows
from cw_monitor.storage import preferences
from cw_monitor.ui.sort_menu import SortMenu
from test_pota_alert import NOW


def row(call, last, first, snrs, wpms, longitude=None, rank=1):
    receptions = [models.RbnSpot(call, f"RX{i}", 14044.0, NOW, snr, wpm)
                  for i, (snr, wpm) in enumerate(zip(snrs, wpms))]
    match = models.Match(call, ("GB-0001",), 14044.0, receptions,
                         location=(0, longitude) if longitude is not None else None)
    entry = models.Entry(match, {"GB-0001": last + 300}, rank, 0, 0, created_at=first, spot_time=last)
    return entry, match


class SortingTests(unittest.TestCase):
    def setUp(self):
        self.a = row("A", NOW - 10, NOW - 100, [-8, -3], [15, 18], 0.0011)
        self.b = row("B", NOW - 20, NOW - 50, [30, 5], [30, 10], 0.0012)
        self.c = row("C", NOW - 30, NOW - 200, [12], [23], 1)
        self.rows = [self.c, self.b, self.a]

    def calls(self, order, rows=None, origin=(0, 0)):
        return [m.call for _, m in ordered_rows(self.rows if rows is None else rows, order, origin, NOW)]

    def test_all_fields_and_directions_use_source_first_seen_and_maximum_spotter_values(self):
        expected = {"last": ["C", "B", "A"], "first": ["C", "A", "B"],
                    "db": ["A", "C", "B"], "wpm": ["A", "C", "B"], "distance": ["A", "B", "C"]}
        for field, ascending in expected.items():
            for direction in ("asc", "desc"):
                with self.subTest(field=field, direction=direction):
                    self.assertEqual(self.calls(SortOrder(field, direction)),
                                     ascending if direction == "asc" else ascending[::-1])
        self.assertEqual([m.call for _, m in self.rows], ["C", "B", "A"])

    def test_default_ties_use_signal_then_stable_detection_rank_without_clock_dependency(self):
        a = row("A", NOW, NOW, [10], [20], rank=1)
        b = row("B", NOW, NOW, [20], [20], rank=2)
        c = row("C", NOW, NOW, [20], [20], rank=3)
        self.assertEqual(self.calls(SortOrder(), [a, b, c]), ["C", "B", "A"])
        # New RBN timestamps do not pretend to be newer source activity.
        self.a[1].receptions[0] = replace(self.a[1].receptions[0], time=NOW + 100)
        self.assertEqual(self.calls(SortOrder()), ["A", "B", "C"])

    def test_unknown_values_are_last_in_both_directions_and_zero_is_known(self):
        unknown = row("UNKNOWN", NOW, NOW, [], [])
        unknown[0].first_seen_known = False
        zero = row("ZERO", NOW, NOW, [0], [0], 0)
        for field in ("first", "db", "wpm", "distance"):
            for direction in ("asc", "desc"):
                with self.subTest(field=field, direction=direction):
                    calls = self.calls(SortOrder(field, direction), [unknown, zero, self.a])
                    self.assertEqual(calls[-1], "UNKNOWN")
        self.assertEqual(self.calls(SortOrder("distance", "desc"), origin=None), ["A", "B", "C"])

    def test_sections_are_sorted_independently_and_ticks_do_not_change_order_or_highlights(self):
        args = cli.arguments(["--qth", "0,0"])
        state = Monitor(args)
        inactive = row("INACTIVE", NOW, NOW, [100], [40])
        inactive[0].eligible = False
        inactive[0].inactive_since = NOW
        state.entries = {entry.match.key: entry for entry, _ in [*self.rows, inactive]}
        for when in (NOW, NOW + 1, NOW + 60):
            active, history = state.rows(when)
            self.assertEqual([m.call for _, m in active], ["A", "B", "C"])
            self.assertEqual([m.call for _, m in history], ["INACTIVE"])
        first_seen = [e.created_at for e, _ in self.rows]
        with patch("cw_monitor.monitor.save_sort", return_value=""):
            state.set_sort(SortOrder("db", "desc"))
        self.assertEqual([m.call for _, m in state.rows(NOW)[0]], ["B", "C", "A"])
        self.assertEqual([e.created_at for e, _ in self.rows], first_seen)
        self.assertTrue(all(e.new_until == 0 for e, _ in self.rows))

    def test_descending_dates_prepend_new_group_by_detection_then_expire_to_source_order(self):
        old = row("OLD", NOW - 1, NOW - 600, [30], [20], rank=1)
        first = row("FIRSTNEW", NOW - 100, NOW - 20, [5], [20], rank=2)
        latest = row("NEWEST", NOW - 200, NOW - 10, [5], [20], rank=3)
        first[0].new_until, latest[0].new_until = NOW + 40, NOW + 50
        rows = [old, first, latest]
        for field in ("last", "first"):
            self.assertEqual(self.calls(SortOrder(field, "desc"), rows), ["NEWEST", "FIRSTNEW", "OLD"])
        for when, expected in ((NOW + 39.999, ["NEWEST", "FIRSTNEW", "OLD"]),
                               (NOW + 40, ["NEWEST", "OLD", "FIRSTNEW"]),
                               (NOW + 50, ["OLD", "FIRSTNEW", "NEWEST"])):
            self.assertEqual([m.call for _, m in ordered_rows(rows, SortOrder(), None, when)], expected)
        self.assertEqual(first[0].created_at, NOW - 20)
        self.assertEqual(first[0].spot_time, NOW - 100)
        strongest = row("STRONGEST", NOW - 100, NOW - 20, [30], [20], rank=1)
        strongest[0].new_until = first[0].new_until
        self.assertEqual(self.calls(SortOrder(), [first, strongest, old]), ["STRONGEST", "FIRSTNEW", "OLD"])

    def test_only_descending_dates_prioritize_active_qualifying_new_records(self):
        old = row("OLD", NOW - 1, NOW - 100, [30], [15], 1)
        fresh = row("NEW", NOW - 120, NOW - 200, [8], [30], 2)
        fresh[0].new_until = NOW + 60
        ordinary = (replace(fresh[0], new_until=0), fresh[1])
        for field in ("last", "first", "db", "wpm", "distance"):
            for direction in ("asc", "desc"):
                order = SortOrder(field, direction)
                expected = ["NEW", "OLD"] if field in ("last", "first") and direction == "desc" else self.calls(order, [old, ordinary])
                self.assertEqual(self.calls(order, [old, fresh]), expected, (field, direction))
        for entry in (replace(fresh[0], awaiting_source=True), replace(fresh[0], eligible=False),
                      replace(fresh[0], spot_deadlines={"GB-0001": NOW - 1}), replace(fresh[0], new_until=NOW)):
            self.assertEqual(self.calls(SortOrder(), [old, (entry, fresh[1])]), ["OLD", "NEW"])
        self.assertEqual([m.call for _, m in ordered_rows([old, fresh], SortOrder(), None, NOW, 15)], ["OLD", "NEW"])

    def test_monitor_uses_frame_time_and_acknowledgement_for_new_priority(self):
        state = Monitor(cli.arguments(["--programs", "POTA", "--qth", "0,0"]))
        def spot(call, when):
            return models.ActivationSpot(call, "GB-0001", 14044.0, when)
        def heard(call, when):
            return models.RbnSpot(call, "SP5GQ", 14044.0, when, 20, 20)
        state.apply(models.Snapshot([spot("OLD", NOW - 120)], [heard("OLD", NOW - 120)], [], NOW - 120), NOW - 120)
        found = state.apply(models.Snapshot([spot("OLD", NOW), spot("NEW", NOW - 100)],
                                            [heard("OLD", NOW), heard("NEW", NOW)], [], NOW), NOW)
        self.assertEqual([m.call for m in found], ["NEW"])
        def calls(when): return [m.call for _, m in state.rows(when)[0]]
        self.assertEqual(calls(NOW), ["NEW", "OLD"])
        self.assertEqual(calls(NOW + 59.999), ["NEW", "OLD"])
        self.assertEqual(calls(NOW + 60), ["OLD", "NEW"])
        state.acknowledge(NOW + 1)
        self.assertEqual(calls(NOW + 1), ["OLD", "NEW"])
        entries = {e.match.call: e for e in state.entries.values()}
        self.assertEqual(entries["OLD"].created_at, NOW - 120)
        self.assertEqual(entries["OLD"].spot_time, NOW)
        self.assertEqual(entries["NEW"].spot_time, NOW - 100)

    def test_frequency_sort_uses_unrounded_source_frequency_in_both_directions_without_new_priority(self):
        rows = [row("HIGH", NOW, NOW, [30], [20]), row("LOW", NOW, NOW, [20], [20]),
                row("MIDHIGH", NOW, NOW, [20], [20]), row("MIDLOW", NOW, NOW, [20], [20])]
        for item, khz in zip(rows, (18083.0, 7032.0, 14044.04, 14044.01)):
            item[1].frequency = khz
            item[1].receptions[0] = replace(item[1].receptions[0], frequency=99999.0 - khz)
        rows[0][0].new_until = NOW + 60
        self.assertEqual(self.calls(SortOrder("frequency", "asc"), rows), ["LOW", "MIDLOW", "MIDHIGH", "HIGH"])
        self.assertEqual(self.calls(SortOrder("frequency", "desc"), rows), ["HIGH", "MIDHIGH", "MIDLOW", "LOW"])
        self.assertEqual(SortOrder.resolve("frequency", None), SortOrder("frequency", "asc"))
        menu = SortMenu(SortOrder())
        menu.handle(ord("6"))
        self.assertEqual(menu.handle(10), SortOrder("frequency", "asc"))
        self.assertEqual(len(menu.overlay([("", "normal")] * 8, 8, 40)), 8)


class SortPreferencesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        env = patch.dict(os.environ, XDG_CONFIG_HOME=str(self.root))
        env.start()
        self.addCleanup(env.stop)

    def test_defaults_persistence_and_cli_precedence(self):
        self.assertEqual(preferences.load_sort(), (SortOrder(), ""))
        saved = SortOrder("wpm", "desc")
        self.assertEqual(preferences.save_sort(saved), "")
        args = cli.arguments([])
        preferences.configure_sort(args)
        self.assertEqual((args.sort, args.sort_order), ("wpm", "desc"))
        args = cli.arguments(["--sort", "distance"])
        preferences.configure_sort(args)
        self.assertEqual(preferences.load_sort()[0], SortOrder("distance", "asc"))
        args = cli.arguments(["--sort-order", "desc"])
        preferences.configure_sort(args)
        self.assertEqual(preferences.load_sort()[0], SortOrder("distance", "desc"))
        args = cli.arguments(["--sort", "first"])
        preferences.configure_sort(args)
        self.assertEqual(preferences.load_sort()[0], SortOrder("first", "desc"))

    def test_invalid_or_unreadable_preferences_fall_back_and_explicit_choice_repairs_them(self):
        path = preferences.sort_settings_path()
        path.parent.mkdir(parents=True)
        for text in ("broken", "[]", '{"field":[]}', '{"direction":"sideways"}', "x" * 65_537):
            path.write_text(text)
            self.assertEqual(preferences.load_sort()[0], SortOrder())
            self.assertIn("Cannot read", preferences.load_sort()[1])
            self.assertEqual(path.read_text(), text)
        args = cli.arguments(["--sort", "db", "--sort-order", "desc"])
        preferences.configure_sort(args)
        self.assertEqual(args.sort_error, "")
        self.assertEqual(preferences.load_sort()[0], SortOrder("db", "desc"))

    def test_failed_atomic_save_keeps_previous_settings_and_reports_error(self):
        preferences.save_sort(SortOrder())
        previous = preferences.sort_settings_path().read_bytes()
        with patch.object(preferences.os, "replace", side_effect=PermissionError("read only")):
            self.assertIn("Cannot save", preferences.save_sort(SortOrder("db", "asc")))
        self.assertEqual(preferences.sort_settings_path().read_bytes(), previous)
        self.assertEqual(list(preferences.sort_settings_path().parent.glob("*.tmp")), [])

    def test_cli_rejects_invalid_choices(self):
        for flag, value in (("--sort", "power"), ("--sort-order", "sideways")):
            with self.subTest(flag=flag), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.arguments([flag, value])

    def test_real_cli_reloads_sort_choice_in_another_process(self):
        command = [sys.executable, "pota_alert.py", "--demo", "--qth", "0,0"]
        root = Path(__file__).resolve().parents[1]
        for flags in (["--sort", "wpm", "--sort-order", "asc"], []):
            result = subprocess.run(command + flags, cwd=root, capture_output=True, text=True,
                                    encoding="utf-8", timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Sort: WPM ↑", result.stdout)
        self.assertEqual(preferences.load_sort()[0], SortOrder("wpm", "asc"))

    def test_menu_changes_draft_then_applies_or_cancels_and_fits_small_terminal(self):
        menu = SortMenu(SortOrder())
        menu.handle(ord("3"))
        self.assertEqual(menu.order, SortOrder("db", "asc"))
        menu.handle(ord("d"))
        frame = menu.overlay([("", "normal")] * 8, 8, 40)
        self.assertEqual(len(frame), 8)
        self.assertTrue(all(len(text) < 40 for text, _ in frame))
        self.assertEqual(menu.handle(10), SortOrder("db", "desc"))
        self.assertTrue(menu.closed)
        cancelled = SortMenu(SortOrder())
        cancelled.handle(ord("5"))
        self.assertIsNone(cancelled.handle(27))
        self.assertTrue(cancelled.closed)
        self.assertEqual(preferences.load_sort(), (SortOrder(), ""))
