import contextlib
import csv
from datetime import datetime
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cw_monitor import cli
from cw_monitor import config
from cw_monitor import models
from cw_monitor import monitor as monitoring
from cw_monitor.sources import http
from cw_monitor.sources import rbn as rbn_source
from cw_monitor.storage import events as event_storage
from cw_monitor.storage import session as sessions
from cw_monitor.storage import summary as daily_storage
from cw_monitor.ui import views as views_ui
import time
from test_pota_alert import NOW


class DailySummaryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = self.directory / "daily.csv"
        self.state = self.monitor()

    def monitor(self, extra=()):
        args = cli.arguments(["--summary-file", str(self.path), *extra])
        state = monitoring.Monitor(args)
        self.addCleanup(state.summary.close)
        if state.log:
            self.addCleanup(state.log.close)
        return state

    def read(self):
        with self.path.open(encoding="utf-8", newline="") as source:
            return list(csv.DictReader(source))

    def apply(self, when=NOW, observed=None, frequency=14052.9, call="IU1PZC", reference="IT-0759",
              name="Full park name", program="POTA", confirmed=True, failed=False, state=None):
        state = state or self.state
        observed = when if observed is None else observed
        spot = models.ActivationSpot(call, reference, frequency, when, name, program=program)
        receptions = [models.RbnSpot(call, "SP5GQ", frequency, observed - 10, 18, 23)] if confirmed else []
        data = models.Snapshot([spot], receptions, [], observed,
                            failed_programs={program} if failed else set())
        state.apply(data, observed)
        return data

    def test_one_row_uses_source_timestamps_not_detection_or_refresh_time(self):
        data = self.apply(when=NOW - 80, observed=NOW)
        original = self.path.read_bytes()
        with patch.object(os, "replace", side_effect=AssertionError("identical observations must not rewrite CSV")):
            self.state.apply(data, NOW + 60)
            self.state.rows(NOW + 70)
            views_ui.render(self.state, NOW + 70)
            views_ui.screen_frame(self.state, NOW + 70, 24, 120)
        self.assertEqual(self.path.read_bytes(), original)
        row, = self.read()
        self.assertEqual(list(row), ["date_utc", "first_seen_utc", "last_seen_utc", "program", "callsign",
                                     "reference", "name", "qth", "band", "last_frequency_khz"])
        self.assertEqual(row["date_utc"], "2026-11-10")
        self.assertEqual(row["first_seen_utc"], event_storage.EventLog.utc(NOW - 80))
        self.assertEqual(row["last_seen_utc"], row["first_seen_utc"])

    def test_frequency_and_last_seen_update_in_place_with_full_name(self):
        name = 'A very long nature reserve, named "Świętokrzyski", with a complete untruncated name'
        self.state.args.park_name_length = 3
        self.apply(name=name)
        self.apply(NOW + 60, frequency=14060.125, name=name)
        row, = self.read()
        self.assertEqual(row["name"], name)
        self.assertEqual(row["first_seen_utc"], event_storage.EventLog.utc(NOW))
        self.assertEqual(row["last_seen_utc"], event_storage.EventLog.utc(NOW + 60))
        self.assertEqual(float(row["last_frequency_khz"]), 14060.125)

    def test_restart_keeps_first_seen_and_older_data_cannot_roll_back_last_frequency(self):
        self.apply(NOW)
        self.apply(NOW + 100, frequency=14060)
        self.state.summary.close()
        restarted = self.monitor()
        self.apply(NOW - 20, observed=NOW + 110, frequency=14040, state=restarted)
        row, = self.read()
        self.assertEqual(row["first_seen_utc"], event_storage.EventLog.utc(NOW - 20))
        self.assertEqual(row["last_seen_utc"], event_storage.EventLog.utc(NOW + 100))
        self.assertEqual(float(row["last_frequency_khz"]), 14060)
        self.apply(NOW + 120, frequency=14061, state=restarted)
        row, = self.read()
        self.assertEqual(row["first_seen_utc"], event_storage.EventLog.utc(NOW - 20))
        self.assertEqual(row["last_seen_utc"], event_storage.EventLog.utc(NOW + 120))

    def test_program_callsign_reference_and_band_have_separate_rows(self):
        for parameters in ({}, {"call": "OTHER/P"}, {"reference": "IT-0760"},
                           {"frequency": 7028}, {"program": "WWFF", "reference": "IFFF-0001"},
                           {"program": "SOTA", "reference": "I/PM-001"}):
            self.apply(**parameters)
        rows = self.read()
        self.assertEqual(len(rows), 6)
        self.assertEqual(len({tuple(row[k] for k in ("date_utc", "program", "callsign", "reference", "band"))
                              for row in rows}), 6)

    def test_source_utc_day_changes_only_when_a_new_days_spot_arrives(self):
        before = datetime(2026, 11, 10, 23, 59, 50, tzinfo=config.UTC).timestamp()
        data = self.apply(before, observed=before + 20)
        self.state.apply(data, before + 25)
        self.assertEqual(len(self.read()), 1)
        self.apply(before + 30, observed=before + 40)
        self.assertEqual({r["date_utc"] for r in self.read()}, {"2026-11-10", "2026-11-11"})
        self.assertTrue(all(r["first_seen_utc"] == r["last_seen_utc"] for r in self.read()))

    def test_missing_name_can_be_filled_and_does_not_erase_known_name(self):
        self.apply(name="")
        self.assertEqual(self.read()[0]["name"], "")
        self.apply(NOW + 10, name="Complete name")
        self.apply(NOW + 20, name="")
        self.assertEqual(self.read()[0]["name"], "Complete name")

    def test_inactive_history_and_failed_sources_do_not_extend_last_seen(self):
        data = self.apply()
        saved = self.path.read_bytes()
        self.state.rows(NOW + 400)
        self.assertEqual(self.path.read_bytes(), saved)
        self.state.apply(models.Snapshot([], [], [], NOW + 410, {data.spots[0].key}), NOW + 410)
        self.assertEqual(self.path.read_bytes(), saved)
        self.apply(NOW + 420, observed=NOW + 420, failed=True)
        self.assertEqual(self.path.read_bytes(), saved)
        self.apply(NOW + 430)
        row, = self.read()
        self.assertEqual(row["first_seen_utc"], event_storage.EventLog.utc(NOW))
        self.assertEqual(row["last_seen_utc"], event_storage.EventLog.utc(NOW + 430))

    def test_unconfirmed_spots_are_not_summarized_and_file_is_not_rbn_confirmation(self):
        self.apply(confirmed=False)
        self.assertEqual(self.read(), [])
        self.apply()
        saved = self.path.read_bytes()
        self.state.summary.close()
        restarted = self.monitor()
        self.apply(NOW + 100, confirmed=False, state=restarted)
        self.assertEqual(restarted.entries, {})
        self.assertEqual(self.path.read_bytes(), saved)

    def test_valid_duplicate_rows_are_merged_on_reload(self):
        self.apply()
        row = self.read()[0]
        self.state.summary.close()
        newer = dict(row, last_seen_utc=event_storage.EventLog.utc(NOW + 100), last_frequency_khz="14060.0", name="New name")
        with self.path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=daily_storage.DailySummary.fields)
            writer.writeheader()
            writer.writerows([newer, row])
        restarted = self.monitor()
        restarted.summary.flush()
        row, = self.read()
        self.assertEqual(row["first_seen_utc"], event_storage.EventLog.utc(NOW))
        self.assertEqual(row["last_seen_utc"], event_storage.EventLog.utc(NOW + 100))
        self.assertEqual(row["name"], "New name")
        self.assertEqual(float(row["last_frequency_khz"]), 14060)

    def test_bad_existing_data_is_rejected_without_overwriting(self):
        self.apply()
        row = self.read()[0]
        self.state.summary.close()
        for changes in ({"last_seen_utc": event_storage.EventLog.utc(NOW - 1)}, {"date_utc": "2026-11-11"},
                        {"band": "40m"}, {"program": "UNKNOWN"}, {"last_frequency_khz": "nan"}):
            with self.path.open("w", encoding="utf-8", newline="") as output:
                writer = csv.DictWriter(output, fieldnames=daily_storage.DailySummary.fields)
                writer.writeheader()
                writer.writerow(dict(row, **changes))
            saved = self.path.read_bytes()
            with self.assertRaisesRegex(ValueError, "Invalid daily summary"):
                daily_storage.DailySummary(self.path)
            self.assertEqual(self.path.read_bytes(), saved)

    def test_failed_atomic_replace_keeps_old_file_and_can_retry(self):
        self.apply()
        saved = self.path.read_bytes()
        with patch.object(os, "replace", side_effect=OSError("disk full")):
            self.apply(NOW + 100)
        self.assertEqual(self.path.read_bytes(), saved)
        self.assertIn("SUMMARY ERROR: disk full", views_ui.reports(self.state))
        self.assertEqual(list(self.directory.glob("*.tmp")), [])
        self.assertEqual(list(self.directory.glob(".*.tmp")), [])
        self.state.summary.flush()
        self.assertEqual(self.read()[0]["last_seen_utc"], event_storage.EventLog.utc(NOW + 100))
        self.assertEqual(self.state.summary.error, "")

    def test_two_simultaneous_writers_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "already in use"):
            daily_storage.DailySummary(self.path)
        self.apply()
        self.assertEqual(len(self.read()), 1)
        self.state.summary.close()
        restarted = self.monitor()
        self.assertEqual(len(restarted.summary.rows), 1)

    def test_summary_and_event_log_can_be_enabled_independently(self):
        self.assertIsNone(sessions.get_summary(cli.arguments([])))
        self.assertIsNone(self.state.log)
        self.state.summary.close()
        events = self.directory / "events.csv"
        both = self.monitor(["--log-file", str(events)])
        self.apply(state=both)
        self.apply(NOW + 10, frequency=14060, state=both)
        with events.open(encoding="utf-8", newline="") as source:
            rows = list(csv.DictReader(source))
        self.assertEqual([r["event"] for r in rows], ["NEW", "UPDATE"])
        self.assertEqual(len(self.read()), 1)

    def test_event_log_and_summary_cannot_use_same_path_or_hard_link(self):
        alias = self.directory / "alias.csv"
        os.link(self.path, alias)
        for destination in (self.path, alias):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.arguments(["--log-file", str(self.path), "--summary-file", str(destination)])

    def test_demo_restarts_merge_rows_and_bad_header_has_clear_cli_error(self):
        self.state.summary.close()
        for _ in range(2):
            with patch.object(time, "time", return_value=NOW), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["--demo", "--summary-file", str(self.path)]), 0)
        rows = self.read()
        self.assertEqual(len(rows), 5)
        self.assertEqual({r["program"] for r in rows}, set(config.PROGRAMS))
        self.path.write_text("incompatible,header\n")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(cli.main(["--demo", "--summary-file", str(self.path)]), 2)
        self.assertIn("different CSV header", stderr.getvalue())
        self.assertEqual(self.path.read_text(), "incompatible,header\n")

    def test_sota_export_keeps_full_summit_name_without_extra_network_requests(self):
        self.state.summary.close()
        name = 'A long summit name, with "quotes" and Polish characters: Śnieżka'
        feed = self.directory / "sota.json"
        feed.write_text(json.dumps([{"activatorCallsign": "IU1PZC", "associationCode": "SP", "summitCode": "KA-001",
                                     "frequency": "14.0529", "mode": "CW", "timeStamp": event_storage.EventLog.utc(NOW),
                                     "summitDetails": name}]))
        reception = models.RbnSpot("IU1PZC", "SP5GQ", 14052.9, NOW - 10, 18, 23)
        with patch.object(time, "time", return_value=NOW), patch.object(rbn_source.RbnClient, "fetch", return_value=[reception]), \
             patch.object(http, "get_json", side_effect=AssertionError("unexpected extra HTTP request")), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--once", "--programs", "SOTA", "--sota-file", str(feed),
                                       "--summary-file", str(self.path), "--reference-name-length", "3"]), 0)
        row, = self.read()
        self.assertEqual(row["name"], name)
        self.assertEqual(row["reference"], "SP/KA-001")


if __name__ == "__main__":
    unittest.main()
