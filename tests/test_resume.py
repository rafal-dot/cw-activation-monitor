import contextlib
import csv
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cw_monitor import cli
from cw_monitor import geo as geography
from cw_monitor import models
from cw_monitor import monitor as monitoring
from cw_monitor import polling
from cw_monitor import runtime
from cw_monitor.sources import http
from cw_monitor.storage import events as event_storage
from cw_monitor.storage import session as sessions
from cw_monitor.ui import table as table_ui
from cw_monitor.ui import views as views_ui
import time
from test_pota_alert import NOW


class ResumeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.events = self.root / "events.csv"
        self.daily = self.root / "daily.csv"
        clock = patch.object(time, "time", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def args(self, extra=(), log=True, summary=True):
        options = ["--programs", "POTA", "--qth", "JO91AA"]
        if log:
            options += ["--log-file", str(self.events)]
        if summary:
            options += ["--summary-file", str(self.daily)]
        return cli.arguments([*options, *extra])

    def start(self, extra=(), log=True, summary=True):
        state = monitoring.Monitor(self.args(extra, log, summary))
        if state.log:
            self.addCleanup(state.log.close)
        if state.summary:
            self.addCleanup(state.summary.close)
        return state

    def close(self, state):
        if state.log:
            state.log.close()
        if state.summary:
            state.summary.close()

    def spot(self, when=NOW - 80, call="IU1PZC", reference="IT-0759", frequency=14052.9,
             program="POTA", name='Park Świętokrzyski, "full name"'):
        return models.ActivationSpot(call, reference, frequency, when, name, (45.0, 7.3), program)

    def apply(self, state, spots=None, when=NOW - 60, heard=True):
        spots = [self.spot()] if spots is None else spots
        rbn = [models.RbnSpot(s.call, "SP5GQ", s.frequency, when - 10, 18, 23) for s in spots] if heard else []
        return state.apply(models.Snapshot(spots, rbn, [], when), when)

    def records(self):
        with self.events.open(encoding="utf-8", newline="") as source:
            return list(csv.DictReader(source))

    def test_both_logs_restore_history_names_times_and_values_without_writing_or_alerts(self):
        original = self.start()
        self.apply(original, [self.spot(NOW - 1050)], NOW - 1000)
        self.apply(original, [self.spot(NOW - 820, frequency=14053.1)], NOW - 800, heard=False)
        original.rows(NOW)
        self.close(original)
        saved = self.events.read_bytes(), self.daily.read_bytes()
        with patch.object(http, "get_json", side_effect=AssertionError("restore must be offline")):
            restored = self.start()
        self.assertEqual(restored.restore_count, 1)
        active, history = restored.rows(NOW)
        self.assertEqual(active, [])
        entry, match = history[0]
        self.assertEqual(entry.created_at, NOW - 1000)
        self.assertEqual(entry.spot_time, NOW - 820)
        self.assertEqual(entry.inactive_since, NOW - 520)
        self.assertEqual(match.frequency, 14053.1)
        self.assertEqual(match.reference_names["IT-0759"], self.spot().name)
        self.assertEqual(match.location, geography.locator_coordinates(self.spot().qth))
        self.assertEqual(match.reference_qths["IT-0759"], self.spot().qth)
        self.assertIsNone(match.receptions[0].time)
        values = table_ui.row_values(entry, match, NOW, history=True)
        self.assertEqual(values[7], "—")
        self.assertEqual(values[12:14], ["00:16:40", "00:13:40"])
        self.assertEqual(values[11], "UNKNOWN RESTORED")
        for width in (40, 80, 120, 220):
            frame = views_ui.screen_frame(restored, NOW, 30, width)
            self.assertTrue(any("IU1PZC" in text for text, _ in frame))
            self.assertFalse(any(style == "new" for _, style in frame))
        self.assertEqual(saved, (self.events.read_bytes(), self.daily.read_bytes()))

    def test_fresh_program_report_reuses_logged_confirmation_without_new_alert(self):
        original = self.start()
        self.apply(original)
        self.close(original)
        restored = self.start()
        poller = polling.Poller(restored.args)
        spot = self.spot(NOW)
        with patch.object(poller, "read_source", return_value=([spot], 0, set())), \
             patch.object(poller.rbn, "fetch", return_value=[]) as fetch:
            snapshot = poller.fetch()
        self.assertEqual(fetch.call_args.args[0], [])
        self.assertEqual(restored.apply(snapshot, NOW), [])
        self.assertEqual(restored.rows(NOW)[0], [])
        entry, match = restored.rows(NOW)[1][0]
        self.assertIsNotNone(entry.active_match(NOW))
        self.assertEqual(table_ui.row_values(entry, match, NOW, history=True)[11], "ACTIVE RESTORED")
        self.assertEqual(table_ui.row_values(entry, match, NOW, history=True)[7], "—")
        self.assertEqual([row["event"] for row in self.records()].count("NEW"), 1)
        self.assertFalse(entry.awaiting_source)

    def test_fresh_rbn_replaces_unknown_age_and_coordinates_are_refreshed(self):
        original = self.start()
        self.apply(original)
        self.close(original)
        restored = self.start()
        self.apply(restored, [self.spot(NOW, frequency=14053.0)], NOW)
        entry, match = restored.rows(NOW)[1][0]
        values = table_ui.row_values(entry, match, NOW, qth=restored.args.qth)
        self.assertEqual(values[7], "00:00:10")
        self.assertNotEqual(values[14], "—")
        self.assertNotEqual(values[16], "—")

    def test_history_age_includes_time_while_the_monitor_was_stopped(self):
        original = self.start()
        self.apply(original, [self.spot(NOW - 360, call="KEEP1"), self.spot(NOW - 361, call="DROP1")], NOW - 350)
        self.close(original)
        restored = self.start(["--history-max-age", "60"])
        self.assertEqual([match.call for _, match in restored.rows(NOW)[1]], ["KEEP1"])
        self.assertEqual(restored.rows(NOW + 1), ([], []))

    def test_explicit_inactivity_does_not_get_an_extra_lifetime_after_restart(self):
        original = self.start()
        self.apply(original, [self.spot(NOW - 120)], NOW - 100)
        key = next(iter(original.entries))
        original.apply(models.Snapshot([], [], [], NOW - 80, {key}), NOW - 80)
        self.close(original)
        restored = self.start(["--history-max-age", "90"])
        entry, _ = restored.rows(NOW)[1][0]
        self.assertEqual(entry.inactive_since, NOW - 80)
        self.assertFalse(entry.eligible)
        self.assertEqual(restored.rows(NOW + 11), ([], []))

    def test_daily_only_restores_history_but_requires_receiver_confirmation(self):
        original = self.start(log=False)
        self.apply(original)
        self.close(original)
        saved = self.daily.read_bytes()
        restored = self.start(log=False)
        entry, match = restored.rows(NOW)[1][0]
        values = table_ui.row_values(entry, match, NOW, history=True)
        self.assertEqual(values[12], "—")
        self.assertEqual(values[4:9], ["—"] * 5)
        poller = polling.Poller(restored.args)
        self.assertEqual(poller.confirmed, {})
        self.apply(restored, [self.spot(NOW)], NOW, heard=False)
        self.assertEqual(self.daily.read_bytes(), saved)
        self.assertIsNone(entry.active_match(NOW))
        self.assertEqual(entry.spot_time, NOW - 80)
        self.assertEqual(self.apply(restored, [self.spot(NOW)], NOW), [])
        self.assertIsNotNone(entry.active_match(NOW))

    def test_events_only_retains_receivers_and_first_detection_without_a_daily_file(self):
        original = self.start(summary=False)
        self.apply(original)
        self.close(original)
        restored = self.start(summary=False)
        entry, match = restored.rows(NOW)[1][0]
        self.assertEqual(entry.created_at, NOW - 60)
        self.assertEqual(match.receptions[0].receiver, "SP5GQ")
        self.assertEqual(match.reference_names, {})
        self.assertFalse(self.daily.exists())

    def test_band_reference_and_program_identities_are_restored_separately(self):
        feed = self.root / "sota.json"
        feed.write_text("[]")
        extra = ["--programs", "POTA", "SOTA", "WWFF", "--sota-file", str(feed)]
        original = self.start(extra)
        spots = [self.spot(), self.spot(reference="IT-0760"), self.spot(frequency=7028),
                 self.spot(program="SOTA", reference="SP/BZ-001"),
                 self.spot(program="WWFF", reference="SPFF-0001")]
        self.apply(original, spots)
        self.close(original)
        restored = self.start(extra)
        self.assertEqual(set(restored.entries), {spot.key for spot in spots})
        self.assertEqual(len(restored.rows(NOW)[1]), 5)
        self.close(restored)
        selected = self.start()
        self.assertEqual(len(selected.entries), 3)
        self.assertTrue(all(entry.match.program == "POTA" for entry in selected.entries.values()))

    def test_changed_receiver_filter_does_not_reuse_an_unselected_confirmation(self):
        original = self.start()
        self.apply(original)
        self.close(original)
        restored = self.start(["--spotters", "SQ5J"])
        self.assertEqual(polling.Poller(restored.args).confirmed, {})
        entry = next(iter(restored.entries.values()))
        self.apply(restored, [self.spot(NOW)], NOW, heard=False)
        self.assertIsNone(entry.active_match(NOW))

    def test_source_failure_keeps_restored_history_and_does_not_renew_timestamps(self):
        original = self.start()
        self.apply(original)
        self.close(original)
        restored = self.start()
        entry = next(iter(restored.entries.values()))
        saved = self.daily.read_bytes()
        restored.apply(models.Snapshot([], [], [], NOW, failed_programs={"POTA"}, errors=["POTA: HTTP 503"]), NOW)
        self.assertTrue(entry.awaiting_source)
        self.assertEqual(entry.spot_time, NOW - 80)
        self.assertEqual(self.daily.read_bytes(), saved)
        self.assertEqual(len(restored.rows(NOW)[1]), 1)

    def test_invalid_record_is_skipped_without_losing_valid_history(self):
        original = self.start()
        self.apply(original)
        self.close(original)
        invalid = dict(self.records()[0], spot_khz="not-a-frequency")
        with self.events.open("a", encoding="utf-8", newline="") as output:
            csv.DictWriter(output, fieldnames=event_storage.EventLog.fields).writerow(invalid)
        saved = self.events.read_bytes()
        restored = self.start()
        self.assertEqual(restored.restore_skipped, 1)
        self.assertEqual(len(restored.entries), 1)
        self.assertTrue(any("invalid event log" in message for message in views_ui.reports(restored)))
        self.assertEqual(self.events.read_bytes(), saved)

    def test_plain_mode_displays_saved_history_before_first_network_fetch(self):
        original = self.start()
        self.apply(original)
        self.close(original)
        args = self.args(["--once"])
        output = io.StringIO()

        def fetch():
            text = output.getvalue()
            self.assertIn("HISTORY", text)
            self.assertIn("IU1PZC", text)
            self.assertIn("RESTORED", text)
            return models.Snapshot([], [], [], NOW)

        try:
            with contextlib.redirect_stdout(output), patch.object(polling.Poller, "fetch", side_effect=fetch):
                self.assertEqual(runtime.run_plain(args), 0)
        finally:
            sessions.get_log(args).close()
            sessions.get_summary(args).close()

    def test_demo_ignores_previous_real_entries(self):
        original = self.start()
        self.apply(original)
        self.close(original)
        demo = self.start(["--demo"])
        self.assertEqual(demo.entries, {})
        self.assertEqual(demo.restore_count, 0)

    def test_restore_preserves_a_log_without_a_trailing_newline_and_append_separates_records(self):
        original = self.start()
        self.apply(original)
        self.close(original)
        original_bytes = self.events.read_bytes().rstrip(b"\r\n")
        self.events.write_bytes(original_bytes)
        restored = self.start()
        self.assertEqual(len(restored.entries), 1)
        self.assertEqual(self.events.read_bytes(), original_bytes)
        restored.log.record("SOURCE_OK", NOW, detail="recovered")
        self.assertEqual([row["event"] for row in self.records()], ["NEW", "SOURCE_OK"])

    def test_out_of_order_older_event_cannot_replace_newer_source_data_or_status(self):
        original = self.start()
        self.apply(original)
        self.apply(original, [self.spot(NOW - 40, frequency=14053.0)], NOW - 20)
        original.log.record("INACTIVE", NOW - 50, models.Match("IU1PZC", ("IT-0759",), 14052.9,
                            [models.RbnSpot("IU1PZC", "SP5GQ", 14052.9, NOW - 90, 18, 23)]),
                            spot_time=NOW - 80)
        self.close(original)
        restored = self.start()
        entry = next(iter(restored.entries.values()))
        self.assertEqual(entry.spot_time, NOW - 40)
        self.assertEqual(entry.match.frequency, 14053.0)
        self.assertTrue(entry.eligible)

    def test_malformed_csv_is_rejected_without_modifying_saved_data(self):
        original = self.start()
        self.apply(original)
        self.close(original)
        saved = self.events.read_bytes() + b'"unfinished quoted field'
        self.events.write_bytes(saved)
        args = self.args()
        try:
            with self.assertRaisesRegex(ValueError, "Cannot restore event log"):
                monitoring.Monitor(args)
        finally:
            sessions.get_log(args).close()
            sessions.get_summary(args).close()
        self.assertEqual(self.events.read_bytes(), saved)


if __name__ == "__main__":
    unittest.main()
