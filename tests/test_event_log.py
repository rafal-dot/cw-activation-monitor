import contextlib
import csv
import io
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest.mock import Mock, patch

from cw_monitor import cli
from cw_monitor import models
from cw_monitor import monitor as monitoring
from cw_monitor import polling
from cw_monitor.storage import events as event_storage
from cw_monitor.storage import session as sessions
from cw_monitor.ui import views as views_ui
import time
from test_pota_alert import NOW, rbn


class EventLogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "events.csv"
        self.args = cli.arguments(["--programs", "POTA", "--log-file", str(self.path)])
        self.state = monitoring.Monitor(self.args)
        self.addCleanup(self.state.log.close)

    def read(self):
        with self.path.open(encoding="utf-8", newline="") as source:
            return list(csv.DictReader(source))

    def snapshot(self, when=NOW, frequency=14052.9, report_time=None):
        spot = models.ActivationSpot("IU1PZC", "IT-0759", frequency,
                                  when if report_time is None else report_time, 'A park, with "quotes"', qth="JN34PX")
        return models.Snapshot([spot], [rbn()], [], when)

    def test_new_event_contains_original_times_and_repeated_render_does_not_log(self):
        data = self.snapshot(report_time=NOW - 80)
        self.state.apply(data, NOW)
        self.state.apply(data, NOW + 60)
        for now in (NOW + 61, NOW + 62):
            self.state.rows(now)
            views_ui.render(self.state, now)
            views_ui.screen_frame(self.state, now, 24, 120)
        rows = self.read()
        self.assertEqual([r["event"] for r in rows], ["NEW"])
        row = rows[0]
        self.assertEqual(list(row), ["timestamp_utc", "source_time_utc", "event", "program",
                                     "callsign", "reference", "qth", "band", "spot_khz", "rbn_khz", "spotters",
                                     "snr_db", "wpm", "detail"])
        self.assertEqual(row["timestamp_utc"], event_storage.EventLog.utc(NOW))
        self.assertEqual(row["program"], "POTA")
        self.assertEqual(row["callsign"], "IU1PZC")
        self.assertEqual(row["qth"], "JN34PX")
        self.assertEqual(row["band"], "20m")
        self.assertEqual(row["source_time_utc"], event_storage.EventLog.utc(NOW - 80))
        self.assertEqual(row["spotters"], "SP5GQ")
        self.assertEqual(row["spot_khz"], "14052.9")
        self.assertEqual(row["rbn_khz"], "14052.9")
        self.assertEqual(row["snr_db"], "18")
        self.assertEqual(row["wpm"], "23")

    def test_updates_band_and_lifecycle_are_logged_once(self):
        self.state.apply(self.snapshot(), NOW)
        self.state.apply(self.snapshot(NOW + 10, 14053.1), NOW + 10)
        self.state.rows(NOW + 300)
        self.state.rows(NOW + 300)
        self.state.rows(NOW + 311)
        self.state.rows(NOW + 312)
        self.state.apply(self.snapshot(NOW + 320, 14053.1), NOW + 320)
        band = models.ActivationSpot("IU1PZC", "IT-0759", 7028, NOW + 330)
        self.state.apply(models.Snapshot([band], [rbn(age=-330, freq=7028)], [], NOW + 330), NOW + 330)
        events = [r["event"] for r in self.read()]
        self.assertEqual(events.count("NEW"), 1)
        self.assertEqual(events.count("HISTORY"), 1)
        self.assertEqual(events.count("INACTIVE"), 1)
        self.assertEqual(events.count("ACTIVE"), 1)
        self.assertEqual(events.count("BAND"), 1)
        self.assertEqual(events.count("UPDATE"), 2)
        self.assertTrue(any("14052.9 → 14053.1" in r["detail"] for r in self.read()))

    def test_source_failure_and_recovery_do_not_repeat_each_tick(self):
        self.state.fail("HTTP 503")
        self.state.fail("HTTP 503")
        self.state.apply(self.snapshot(), NOW)
        self.state.apply(self.snapshot(), NOW + 1)
        self.assertEqual([r["event"] for r in self.read()], ["SOURCE_ERROR", "NEW", "SOURCE_OK"])

    def test_csv_appends_once_with_quotes_and_can_be_reopened(self):
        self.state.log.record("TEST", NOW, detail='comma, quote " text\nnew line')
        self.state.log.close()
        other = event_storage.EventLog(self.path, "events")
        other.record("TEST2", NOW, detail="second")
        other.close()
        rows = self.read()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["detail"], 'comma, quote " textnew line')
        self.assertEqual(rows[1]["event"], "TEST2")
        self.assertTrue(all(row[field] == "" for row in rows
                            for field in ("source_time_utc", "program", "spot_khz")))
        self.assertEqual(self.path.read_text().count("timestamp_utc"), 1)

    def test_threaded_debug_and_main_events_are_valid_csv(self):
        self.state.log.level = "debug"
        workers = [Thread(target=lambda: [self.state.log.record("FETCH", NOW, detail="a,b", debug=True)
                                          for _ in range(50)]) for _ in range(3)]
        for worker in workers:
            worker.start()
        for _ in range(50):
            self.state.log.record("NEW", NOW)
        for worker in workers:
            worker.join()
        rows = self.read()
        self.assertEqual(len(rows), 200)
        self.assertTrue(all(None not in row for row in rows))

    def test_events_level_omits_debug_and_debug_level_records_rejected_confirmation(self):
        self.state.log.record("FETCH", NOW, debug=True)
        self.assertEqual(self.read(), [])
        self.state.log.level = "debug"
        poller = polling.Poller(self.args)
        with patch.object(poller, "read_source", return_value=(self.snapshot().spots, 0, set())), \
             patch.object(poller.rbn, "fetch", return_value=[]), patch.object(time, "time", return_value=NOW):
            poller.fetch()
        rows = self.read()
        self.assertEqual({r["event"] for r in rows}, {"SOURCE_READ", "RBN_REJECT", "RBN_FETCH"})
        rejected = next(r for r in rows if r["event"] == "RBN_REJECT")
        self.assertEqual(rejected["callsign"], "IU1PZC")
        self.assertEqual(rejected["qth"], "JN34PX")
        self.assertIn("age", rejected["detail"])

    def test_disk_error_does_not_stop_monitor_and_is_reported(self):
        self.state.log.writer = Mock()
        self.state.log.writer.writerow.side_effect = OSError("disk full")
        self.state.apply(self.snapshot(), NOW)
        self.assertEqual(len(self.state.rows(NOW)[0]), 1)
        self.assertIn("LOG ERROR: disk full", views_ui.reports(self.state))

    def test_bad_csv_header_is_not_overwritten_and_cli_reports_error(self):
        self.state.log.close()
        self.path.write_text("other,data\n")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(cli.main(["--demo", "--log-file", str(self.path)]), 2)
        self.assertIn("different CSV header", stderr.getvalue())
        self.assertEqual(self.path.read_text(), "other,data\n")

    def test_cli_demo_writes_log_and_without_option_logging_is_disabled(self):
        self.assertIsNone(sessions.get_log(cli.arguments([])))
        self.state.log.close()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--demo", "--log-file", str(self.path)]), 0)
        self.assertTrue({"POTA", "SOTA", "WWFF"}.issubset({r["program"] for r in self.read()}))


if __name__ == "__main__":
    unittest.main()
