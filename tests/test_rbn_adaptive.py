from threading import Event
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from cw_monitor import models
from cw_monitor.sources import http
from cw_monitor.sources import rbn as rbn_source
import time
from test_pota_alert import NOW, FIELDS, raw_rbn


class Feed:
    def __init__(self):
        self.now = NOW
        self.rows = {}
        self.queries = []
        self.fail = False

    def add(self, key, when, call="IU1PZC", receiver="SP5GQ"):
        self.rows[str(key)] = raw_rbn(call=call, receiver=receiver, age=NOW - when)

    def get(self, url, timeout):
        params = parse_qs(urlsplit(url).query)
        self.queries.append(params)
        if self.fail:
            raise models.SourceError("HTTP 503")
        calls = params.get("cdx", [""])[0].split(",")
        receivers = params["cde"][0].split(",")
        age, limit = int(params["ma"][0]), int(params["r"][0])
        selected = [(key, row) for key, row in self.rows.items()
                    if row[0] in receivers and self.now - age <= row[-1] <= self.now
                    and ("cdx" not in params or row[2] in calls)]
        selected.sort(key=lambda pair: pair[1][-1], reverse=True)
        return {"ver_h": "v1", "now": self.now, "spots": dict(selected[:limit])}


class LoadTests(unittest.TestCase):
    def test_growth_uses_average_peak_and_longer_windows(self):
        load = rbn_source.RbnLoad()
        self.assertEqual(load.start(75), 10)
        load.observe(8, 60, 75, True)
        self.assertEqual(load.average, 8)
        self.assertEqual(load.limit, 20)
        self.assertEqual(load.start(300), 100)

    def test_recent_peak_holds_capacity_then_shrink_is_slow(self):
        load = rbn_source.RbnLoad()
        load.observe(40, 60, 75, True)
        self.assertEqual(load.limit, 100)
        for _ in range(9):
            load.observe(0, 60, 75, True)
            self.assertEqual(load.limit, 100)
        load.observe(0, 60, 75, True)
        self.assertEqual(load.limit, 100)
        load.observe(0, 60, 75, True)
        self.assertEqual(load.limit, 100)
        load.observe(0, 60, 75, True)
        self.assertEqual(load.limit, 50)

    def test_truncated_counts_do_not_change_measured_average_or_peaks(self):
        load = rbn_source.RbnLoad()
        load.observe(8, 60, 75, True)
        before = load.average, tuple(load.peaks)
        load.observe(100, 120, 135, False)
        self.assertEqual((load.average, tuple(load.peaks)), before)
        self.assertEqual(load.limit, 100)


class AdaptiveFetchTests(unittest.TestCase):
    def setUp(self):
        self.feed = Feed()
        self.client = rbn_source.RbnClient(5)
        self.client.version, self.client.fields, self.client.cw_mode = "v1", FIELDS, "1"
        for patcher in (patch.object(http, "get_json", side_effect=self.feed.get),
                        patch.object(time, "time", side_effect=lambda: self.feed.now)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def fetch(self, calls=None, receivers=None):
        return self.client.fetch(calls or ["IU1PZC"], receivers or ["SP5GQ"], 300)

    def stream(self, receivers=("SP5GQ",)):
        return self.client.streams[(tuple(sorted(receivers)), 300)]

    def test_initial_fetch_expands_to_cover_full_freshness_window(self):
        for i in range(25):
            self.feed.add(i, NOW - i * 10)
        rows = self.fetch()
        self.assertEqual(len(rows), 25)
        self.assertEqual([int(q["r"][0]) for q in self.feed.queries], [10, 20, 50])
        self.assertTrue(all("cdx" not in q for q in self.feed.queries))
        self.assertEqual(self.stream().watermark, NOW)
        self.assertEqual(len(self.stream().spots), 25)
        self.assertAlmostEqual(self.stream().load.average, 5)

    def test_fewer_than_limit_is_complete_even_when_all_rows_are_recent(self):
        self.feed.add("one", NOW - 1)
        self.assertEqual(len(self.fetch()), 1)
        self.assertEqual(len(self.feed.queries), 1)
        self.assertEqual(self.stream().watermark, NOW)

    def test_receiver_stream_cache_can_confirm_another_callsign_later(self):
        self.feed.add("one", NOW - 1)
        self.feed.add("other", NOW - 2, call="NEWCALL")
        self.fetch()
        self.feed.now += 60
        rows = self.fetch(["NEWCALL"])
        self.assertEqual([r.call for r in rows], ["NEWCALL"])
        self.assertEqual(rows[0].time, NOW - 2)
        self.assertEqual(len(self.stream().spots), 2)

    def test_complete_interval_deduplicates_overlap_and_normalizes_elapsed_time(self):
        self.feed.add("one", NOW - 1)
        self.fetch()
        self.feed.now += 120
        for i in range(4):
            self.feed.add(f"new{i}", NOW + 10 + i)
        self.fetch()
        # The old ID is not counted again; four arrivals over two minutes = 2/min.
        self.assertAlmostEqual(self.stream().load.peaks[-1], 2)
        self.assertEqual(len(self.stream().spots), 5)

    def test_full_response_reaching_before_watermark_covers_interval(self):
        self.feed.add("old", NOW - 200)
        self.fetch()
        self.feed.now += 60
        for i in range(9):
            self.feed.add(f"new{i}", NOW + 2 + i)
        # The full ten-row answer includes an old sentinel, covering the window.
        self.fetch()
        self.assertEqual([int(q["r"][0]) for q in self.feed.queries], [10, 10])
        self.assertEqual(self.stream().watermark, NOW + 60)

    def test_overflow_does_not_advance_watermark_or_train_on_partial_count(self):
        self.fetch()
        before = self.stream().load.average, tuple(self.stream().load.peaks)
        self.feed.now += 60
        for i in range(130):
            self.feed.add(i, NOW + 2 + i / 3, call="BUSY")
        self.assertEqual(self.fetch(), [])
        self.assertEqual(self.stream().watermark, NOW)
        self.assertEqual((self.stream().load.average, tuple(self.stream().load.peaks)), before)
        self.assertEqual(self.stream().load.limit, 100)
        self.assertTrue(any("incomplete" in w and "press b" in w for w in self.client.warnings))
        self.assertTrue(all("cdx" not in q for q in self.feed.queries))
        self.assertEqual(self.client.poll_interval, 30)
        self.assertIn("every 30s", self.client.status)

    def test_heavier_burst_shortens_to_fifteen_seconds_and_respects_the_minimum(self):
        for i in range(200):
            self.feed.add(i, NOW - i / 5, call="BUSY")
        self.fetch()
        self.assertEqual(self.client.poll_interval, 15)
        self.assertIsNone(self.stream().watermark)
        self.feed.now += 15
        for i in range(1000):
            self.feed.add(f"burst{i}", self.feed.now - i / 100, call="BUSY")
        self.fetch()
        self.assertEqual(self.client.poll_interval, 15)
        self.assertTrue(any("incomplete" in w for w in self.client.warnings))

    def test_overlapping_fast_reads_fill_future_window_without_concealing_the_old_gap(self):
        self.fetch()
        self.feed.add("missed", NOW + 1)
        self.feed.now += 60
        for i in range(200):
            self.feed.add(f"initial{i}", NOW + 1 + i * 0.295, call="BUSY")
        self.fetch()
        gap_end = self.stream().covered_since
        self.assertGreater(gap_end, NOW)
        self.assertEqual(self.client.poll_interval, 15)
        for cycle in range(22):
            previous = self.feed.now
            self.feed.now += 15
            for i in range(50):
                self.feed.add(f"new{cycle}-{i}", previous + (i + 1) * 0.3, call="BUSY")
            self.fetch()
            if self.feed.now - 300 <= gap_end:
                self.assertEqual(self.stream().watermark, NOW)
                self.assertTrue(any("incomplete" in w for w in self.client.warnings))
            else:
                self.assertEqual(self.stream().watermark, self.feed.now)
                self.assertEqual(self.client.warnings, [])
        self.assertGreater(len(self.stream().spots), 100)
        self.assertNotIn("missed", self.stream().spots)
        self.assertEqual(self.client.poll_interval, 15)

    def test_another_gap_restarts_coverage_instead_of_claiming_continuity(self):
        self.fetch()
        for advance in (60, 100):
            self.feed.now += advance
            for i in range(200):
                self.feed.add(f"{advance}-{i}", self.feed.now - i * 0.1, call="BUSY")
            self.fetch()
            self.assertAlmostEqual(self.stream().covered_since, self.feed.now - 9.9)
            self.assertEqual(self.stream().watermark, NOW)

    def test_interval_returns_slowly_to_default_after_complete_quiet_reads(self):
        for i in range(200):
            self.feed.add(i, NOW - i * 0.2, call="BUSY")
        self.fetch()
        self.assertEqual(self.client.poll_interval, 15)
        self.feed.now += 301
        for cycle in range(12):
            self.fetch()
            self.assertEqual(self.client.poll_interval, 15 if cycle < 5 else 30 if cycle < 11 else 60)
            self.feed.now += self.client.poll_interval

    def test_full_initial_history_but_quiet_last_minute_does_not_speed_up(self):
        for i in range(150):
            self.feed.add(i, NOW - i * 2, call="BUSY")
        self.fetch()
        self.assertEqual(self.client.poll_interval, 60)
        self.assertTrue(any("incomplete" in w for w in self.client.warnings))

    def test_no_pending_calls_suspends_fast_polling_without_erasing_profile_history(self):
        for i in range(200):
            self.feed.add(i, NOW - i * 0.2, call="BUSY")
        self.fetch()
        queries = len(self.feed.queries)
        self.assertEqual(self.client.fetch([], ["SP5GQ"], 300), [])
        self.assertEqual(len(self.feed.queries), queries)
        self.assertEqual(self.client.poll_interval, 60)
        self.assertEqual(self.stream().interval, 15)

    def test_acknowledged_batches_find_target_hidden_by_busy_stream(self):
        self.fetch()
        self.feed.now += 60
        self.feed.add("target", NOW + 1)
        for i in range(130):
            self.feed.add(i, NOW + 2 + i / 3, call="BUSY")
        self.assertEqual(self.fetch(), [])
        self.client.small_batches.set()
        rows = self.fetch()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].call, "IU1PZC")
        self.assertEqual(self.feed.queries[-1]["cdx"], ["IU1PZC"])
        self.assertIn("batches", self.client.status)
        self.assertEqual(self.client.warnings, [])
        self.assertEqual(self.stream().watermark, NOW)  # Global coverage is still unknown.

    def test_after_incomplete_snapshot_recovered_sample_includes_partially_seen_ids(self):
        self.fetch()
        self.feed.now += 60
        for i in range(130):
            self.feed.add(i, NOW + 1 + i / 3)
        self.fetch()
        self.feed.now += 260
        self.fetch()
        # Once enough rows expire to fit, IDs seen in a partial read still count.
        self.assertEqual(self.stream().watermark, NOW + 320)
        retained = sum(row[-1] >= NOW + 20 for row in self.feed.rows.values())
        self.assertAlmostEqual(self.stream().load.peaks[-1], retained / 5)

    def test_source_error_preserves_watermark_and_statistics_then_recovers(self):
        self.feed.add("old", NOW - 1)
        self.fetch()
        before = self.stream().load.average, tuple(self.stream().load.peaks)
        self.feed.now += 60
        self.feed.fail = True
        with self.assertRaises(models.SourceError):
            self.fetch()
        self.assertEqual(self.stream().watermark, NOW)
        self.assertEqual((self.stream().load.average, tuple(self.stream().load.peaks)), before)
        self.feed.now += 60
        self.feed.fail = False
        self.feed.add("new", NOW + 70)
        self.fetch()
        self.assertEqual(self.stream().watermark, NOW + 120)
        self.assertAlmostEqual(self.stream().load.peaks[-1], 0.5)

    def test_cache_expires_and_filters_have_independent_statistics(self):
        self.feed.add("one", NOW - 299)
        self.fetch()
        self.feed.now += 2
        self.assertEqual(self.fetch(), [])
        self.assertEqual(self.stream().spots, {})
        self.feed.add("two", self.feed.now - 1, receiver="SQ5J")
        self.fetch(receivers=["SQ5J"])
        self.assertEqual(len(self.client.streams), 2)
        self.assertEqual(self.stream(("SQ5J",)).load.limit, 10)


if __name__ == "__main__":
    unittest.main()
