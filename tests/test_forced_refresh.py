"""Manual refresh bypasses local cadence; server Retry-After and existing alert identities remain authoritative."""

import queue
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch

from cw_monitor import cli, models, polling, runtime
from cw_monitor.friends import Friend
from cw_monitor.monitor import Monitor
from test_pota_alert import NOW


class ForcedRefreshTests(unittest.TestCase):
    def test_force_reads_sources_and_updates_known_rbn_without_renewing_source_time(self):
        args = cli.arguments(["--programs", "POTA"])
        client = polling.Poller(args)
        spot = models.ActivationSpot("SP5TEST", "PL-0001", 14044, NOW)
        reading = models.RbnSpot("SP5TEST", "SP5GQ", 14044, NOW, 18, 20)
        current = [0]
        with patch.object(client, "read_source", return_value=([spot], 0, set())) as sources, \
             patch.object(client.rbn, "fetch", return_value=[reading]) as rbn, \
             patch.object(time, "time", side_effect=lambda: NOW + current[0]), patch.object(time, "monotonic", side_effect=lambda: current[0]):
            first = client.poll(0)
            rbn.return_value = [models.RbnSpot("SP5TEST", "SP5GQ", 14044, NOW + 1, 26, 25)]
            current[0] = 1
            client.poll(1)
            self.assertEqual(sources.call_count, 1)
            self.assertEqual(rbn.call_args.args[0], [])  # Ordinary reuse does not require another spot query.
            forced = client.poll(1, force=True)
            self.assertEqual(sources.call_count, 2)
            self.assertEqual(rbn.call_args.args[0], ["SP5TEST"])
            self.assertEqual((forced.rbn[0].snr, forced.rbn[0].wpm), (26, 25))
            self.assertEqual(forced.spots[0].time, NOW)
            self.assertEqual(client.next_source_at, 61)
            self.assertEqual(first.spots[0].time, forced.spots[0].time)

    def test_recent_active_source_missing_from_one_feed_is_refreshed_but_qrt_wins(self):
        args = cli.arguments(["--programs", "POTA"])
        client = polling.Poller(args)
        spot = models.ActivationSpot("SP5TEST", "PL-0001", 14044, NOW)
        old = models.RbnSpot("SP5TEST", "SP5GQ", 14044, NOW, 18, 20)
        new = models.RbnSpot("SP5TEST", "SP5GQ", 14044, NOW + 1, 25, 25)
        with patch.object(client, "read_source", side_effect=[([spot], 0, set()), ([], 0, set()), ([], 0, {spot.key})]), \
             patch.object(client.rbn, "fetch", side_effect=[[old], [new], []]), patch.object(time, "time", return_value=NOW + 2):
            client.poll(0)
            refreshed = client.poll(1, force=True)
            self.assertEqual(refreshed.spots, [spot])
            self.assertEqual(refreshed.rbn, [new])
            stopped = client.poll(2, force=True)
            self.assertEqual(stopped.spots, [])
            self.assertIn(spot.key, stopped.inactive)

    def test_force_respects_retry_after_but_bypasses_local_error_backoff(self):
        for delay in (0, 60):
            args = cli.arguments(["--programs", "POTA"])
            client = polling.Poller(args)
            current = [0]
            with patch.object(client, "read_source", side_effect=[models.SourceError("HTTP 503", delay), ([], 0, set())]) as reads, \
                 patch.object(client.rbn, "fetch", return_value=[]), patch.object(time, "monotonic", side_effect=lambda: current[0]):
                client.poll(0)
                current[0] = 1
                result = client.poll(1, force=True)
                self.assertEqual(reads.call_count, 1 if delay else 2)
                if delay:
                    self.assertTrue(any("Retry-After" in text for text in result.errors))
                    current[0] = 60
                    client.poll(60, force=True)
                    self.assertEqual(reads.call_count, 2)

    def test_refresh_key_event_wakes_scheduled_worker_and_quit_stays_responsive(self):
        args = cli.arguments([])
        args._refresh_requested = Event()
        stop, refreshed = Event(), Event()
        events = queue.Queue()
        calls = []
        class FakePoller:
            def __init__(self, *_): pass
            def poll(self, started, force=False):
                calls.append(force)
                if force:
                    refreshed.set(); stop.set()
                return models.Snapshot([], [], [], NOW)
            def next_poll_at(self, started, finished): return finished + 60
        with patch.object(runtime, "Poller", FakePoller):
            worker = Thread(target=runtime.poll_worker, args=(args, events, stop), daemon=True)
            worker.start()
            try:
                while events.get(timeout=2)[0] != "scheduled": pass
                args._refresh_requested.set()
                self.assertTrue(refreshed.wait(2))
            finally:
                stop.set(); args._refresh_requested.set(); worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(calls, [False, True])
