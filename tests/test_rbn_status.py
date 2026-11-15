"""RBN packet telemetry, complete-window averages and terminal presentation."""

from dataclasses import replace
import time
import unittest
from unittest.mock import Mock, patch

from cw_monitor import cli, models, monitor, polling
from cw_monitor.rbn_status import RbnBatch, RbnMode, RbnStatus, RbnTraffic, RbnTrafficSample
from cw_monitor.sources import http, rbn as rbn_source
from cw_monitor.ui import terminal, views
from cw_monitor.ui.layout import cell_width
from cw_monitor.ui.rbn_status import polling_lines
from test_pota_alert import NOW, FIELDS, raw_rbn
from test_rbn_adaptive import Feed


class TrafficTests(unittest.TestCase):
    def test_average_normalizes_different_windows_and_retains_last_five(self):
        traffic = RbnTraffic()
        self.assertIsNone(traffic.average_per_minute)
        traffic.observe(12, 30, True)
        traffic.observe(3, 15, True)
        self.assertEqual(traffic.average_per_minute, 20)
        self.assertEqual(traffic.last, RbnTrafficSample(3, 15))
        for _ in range(5):
            traffic.observe(1, 15, True)
        self.assertEqual(traffic.average_per_minute, 4)
        traffic.observe(200, 15, False)
        self.assertEqual(traffic.last.count, 200)
        self.assertFalse(traffic.complete)
        self.assertEqual(traffic.average_per_minute, 4)
        traffic.observe(0, 15, True)
        self.assertAlmostEqual(traffic.average_per_minute, 3.2)

    def test_partial_startup_and_invalid_durations_do_not_invent_an_average(self):
        traffic = RbnTraffic()
        traffic.observe(100, 300, False)
        for seconds in (0, -1, float("inf"), float("nan")):
            traffic.observe(1, seconds, True)
        self.assertEqual(traffic.last, RbnTrafficSample(100, 300))
        self.assertIsNone(traffic.average_per_minute)
        self.assertFalse(traffic.complete)
        traffic.observe(0, 60, True)
        self.assertEqual(traffic.average_per_minute, 0)


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.feed = Feed()
        self.client = rbn_source.RbnClient(5)
        self.client.version, self.client.fields, self.client.cw_mode = "v1", FIELDS, "1"
        for patcher in (patch.object(http, "get_json", side_effect=self.feed.get),
                        patch.object(time, "time", side_effect=lambda: self.feed.now)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def fetch(self, age=300):
        return self.client.fetch(["IU1PZC"], ["SP5GQ"], age)

    def test_actual_packet_limit_is_distinct_from_next_limit_and_new_selected_cw_count(self):
        for i in range(8):
            self.feed.add(i, NOW - i, call="IU1PZC" if i < 5 else "OTHER1")
        self.feed.rows["7"] = raw_rbn(mode=2, age=7)
        self.assertEqual(len(self.fetch()), 5)
        status = self.client.status
        self.assertEqual(status.batch, RbnBatch(8, 10))
        self.assertEqual(status.last, RbnTrafficSample(7, 300))
        self.assertEqual(status.average_per_minute, 1.4)
        self.assertEqual(status.spotters, ("SP5GQ",))
        self.assertEqual(status.interval, 60)
        self.assertEqual(self.client.streams[(("SP5GQ",), 300)].load.limit, 20)
        self.assertEqual(len(self.feed.queries), 1)

    def test_repeated_pages_do_not_count_spots_again_and_late_arrivals_are_counted(self):
        for i in range(6):
            self.feed.add(i, NOW - 1 - i)
        self.fetch()
        self.feed.now += 60
        self.fetch()
        self.assertEqual(self.client.status.last, RbnTrafficSample(0, 60))
        self.assertEqual(self.client.status.average_per_minute, 1)
        self.feed.now += 60
        self.feed.add("delayed", NOW + 30)
        self.fetch()
        self.assertEqual(self.client.status.last, RbnTrafficSample(1, 60))
        self.assertEqual(self.client.status.average_per_minute, 1)
        self.assertEqual(len(self.feed.queries), 3)

    def test_busy_startup_then_complete_live_window_uses_actual_faster_cadence(self):
        for i in range(100):
            self.feed.add(i, NOW - i / 5, call="BUSY")
        self.fetch()
        self.assertEqual(self.client.status.batch, RbnBatch(100, 100))
        self.assertEqual(self.client.status.interval, 15)
        self.assertFalse(self.client.status.complete)
        self.assertIsNone(self.client.status.average_per_minute)
        self.feed.now += 15
        for i in range(75):
            self.feed.add(f"new{i}", self.feed.now - i / 5, call="BUSY")
        self.fetch()
        self.assertEqual(self.client.status.last, RbnTrafficSample(75, 15))
        self.assertEqual(self.client.status.average_per_minute, 300)
        self.assertTrue(self.client.status.complete)
        self.assertEqual(self.client.warnings, [])

    def test_idle_keeps_packet_without_requests_and_new_spotter_selection_does_not_relabel_it(self):
        self.feed.add("one", NOW - 1)
        self.fetch()
        before = self.client.status
        calls = len(self.feed.queries)
        self.client.fetch([], ["SP5GQ"], 300)
        self.assertEqual(len(self.feed.queries), calls)
        self.assertEqual(self.client.status.batch, before.batch)
        self.assertEqual(self.client.status.last, before.last)
        self.assertEqual(self.client.status.mode, RbnMode.IDLE)
        self.assertIsNone(self.client.status.interval)
        self.client.fetch([], ["G4YBU"], 300)
        self.assertEqual(len(self.feed.queries), calls)
        self.assertIsNone(self.client.status.batch)
        self.assertEqual(self.client.status.spotters, ("G4YBU",))

    def test_targeted_response_does_not_replace_shared_packet_metadata(self):
        for i in range(130):
            self.feed.add(i, NOW - i / 5, call="BUSY")
        self.feed.add("target", NOW - 200)
        self.client.small_batches.set()
        self.assertEqual(len(self.fetch()), 1)
        self.assertEqual(self.feed.queries[-1]["cdx"], ["IU1PZC"])
        self.assertEqual(self.client.status.batch, RbnBatch(100, 100))
        self.assertEqual(self.client.status.mode, RbnMode.BATCHES)

    def test_telemetry_reaches_monitor_via_snapshot_and_survives_unrelated_snapshots(self):
        args = cli.arguments(["--programs", "POTA", "--spotters", "SP5GQ"])
        poller = polling.Poller(args)
        poller.rbn = self.client
        self.feed.add("one", NOW - 1)
        spots = [models.ActivationSpot("IU1PZC", "IT-0759", 14052.9, NOW - 10)]
        with patch.object(poller, "read_sources", return_value=(spots, set(), [], [], set())):
            snapshot = poller.fetch()
        state = monitor.Monitor(args)
        state.apply(snapshot, NOW)
        self.assertEqual(state.rbn_status, self.client.status)
        self.assertIn("RBN: 1/10 rows · every 60s", views.render(state, NOW))
        state.apply(models.Snapshot([], [], [], NOW), NOW)
        self.assertEqual(state.rbn_status, snapshot.rbn_status)


class PresentationTests(unittest.TestCase):
    def setUp(self):
        self.status = RbnStatus(RbnMode.STREAM, RbnBatch(73, 100), 15, ("SP5GQ", "SQ5J"),
                                RbnTrafficSample(12, 15), 42, True)

    def test_width_changes_layout_without_clipping_or_color_and_keeps_partial_labels(self):
        for width in (39, 57, 79, 119):
            with self.subTest(width=width):
                lines = polling_lines(self.status, "Refresh in 00:00:12", width)
                self.assertTrue(all(cell_width(line) <= width for line in lines))
                text = " ".join(lines)
                self.assertIn("RBN: 73/100 rows · every 15s", text)
                self.assertIn("CW: last 12 / 15s · avg ~42.0/min", text)
                self.assertNotIn("\x1b", text)
                self.assertEqual("RBN:" in lines[0], width >= 57)
        partial = replace(self.status, batch=RbnBatch(100, 100), complete=False)
        text = " ".join(polling_lines(partial, "Refresh: waiting…", 39))
        self.assertIn("CW: last 12 / 15s (partial) · last avg ~42.0/min", text)
        self.assertNotIn("WARNING", text)
        waiting = polling_lines(RbnStatus(), "Refresh: waiting…")
        self.assertIn("RBN: waiting", waiting)
        self.assertNotIn("CW:", " ".join(waiting))
        idle = polling_lines(replace(self.status, mode=RbnMode.IDLE, interval=None), "Refresh: waiting…")
        self.assertIn("RBN: 73/100 rows · idle", idle)
        fractional = polling_lines(replace(self.status, interval=37.5), "Refresh: waiting…")
        self.assertIn("RBN: 73/100 rows · every 37.5s", fractional)

    def test_live_and_plain_footers_show_same_data_below_table_without_erasing_on_updates(self):
        args = cli.arguments(["--qth", "KO02KF", "--color", "never"])
        state = monitor.Monitor(args)
        state.apply(models.Snapshot([], [], [], NOW, rbn_status=self.status), NOW)
        state.next_poll_at = 160
        with patch.object(time, "monotonic", return_value=100):
            for width in (40, 58, 80, 120):
                frame = views.screen_frame(state, NOW, 24, width)
                self.assertEqual(len(frame), 24)
                lines = [text for text, _ in frame]
                active = next(i for i, text in enumerate(lines) if text.startswith("ACTIVE"))
                packet = next(i for i, text in enumerate(lines) if "RBN: 73/100" in text)
                self.assertGreater(packet, active)
                for text, style in frame:
                    if text.startswith(("Refresh", "RBN:", "CW:")):
                        self.assertEqual(style, "muted")
                        self.assertLessEqual(cell_width(text), width - 1)
            plain = views.render(state, NOW)
            self.assertIn("RBN: 73/100 rows · every 15s", plain)
            self.assertIn("CW: last 12 / 15s · avg ~42.0/min", plain)
            screen = Mock()
            screen.getmaxyx.return_value = (24, 100)
            with patch.object(terminal.curses, "curs_set"), patch.object(terminal.curses, "doupdate"):
                display = terminal.CursesDisplay(screen, colors=False)
                display.draw(views.screen_frame(state, NOW, 24, 100))
                screen.reset_mock()
                state.rbn_status = replace(self.status, batch=RbnBatch(74, 100), last=RbnTrafficSample(13, 15), average_per_minute=42.1)
                display.draw(views.screen_frame(state, NOW, 24, 100))
                screen.erase.assert_not_called()
                self.assertLess(sum(len(call.args[2]) for call in screen.addstr.call_args_list), 40)
