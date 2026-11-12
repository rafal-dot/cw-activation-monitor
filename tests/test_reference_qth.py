import csv
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cw_monitor import cli
from cw_monitor import geo as geography
from cw_monitor import models
from cw_monitor import monitor as monitoring
from cw_monitor.sources import http
from cw_monitor.sources import programs
from cw_monitor.storage import events as event_storage
from cw_monitor.storage import summary as daily_storage
from cw_monitor.ui import table as table_ui
from cw_monitor.ui import views as views_ui
import time
from test_pota_alert import NOW, pota


class ReferenceQthTests(unittest.TestCase):
    def test_supplied_locators_are_preserved_and_normalized(self):
        for value in ("KO02KF", "KO02KF00", "KO02", "KO"):
            with self.subTest(value=value):
                self.assertEqual(geography.reference_qth({"qth": value.lower()}), value)
        self.assertEqual(geography.reference_qth({"grid6": "KO02KF", "latitude": 0, "longitude": 0}), "KO02KF")

    def test_coordinates_produce_six_characters_before_falling_back_to_coarser_grid(self):
        self.assertEqual(geography.reference_qth({"latitude": 52.23, "longitude": 20.88}), "KO02KF")
        self.assertEqual(geography.reference_qth({"latitude": 0, "longitude": 0, "grid4": "JJ00"}), "JJ00AA")
        self.assertEqual(geography.reference_qth({"grid4": "JJ00"}), "JJ00")
        self.assertEqual(geography.reference_qth({"grid6": "INVALID", "latitude": 0, "longitude": 0}), "JJ00AA")
        self.assertEqual(geography.reference_qth({"grid6": "INVALID", "latitude": 91, "longitude": 0}), "")

    def test_locator_conversion_round_trips_centres_and_handles_world_boundaries(self):
        for value in ("KO02KF", "JJ00AA", "AA00AA", "RR99XX", "JO91AA", "JN35AA"):
            with self.subTest(value=value):
                self.assertEqual(geography.location_locator(geography.locator_coordinates(value)), value)
        self.assertEqual(geography.location_locator((-90, -180)), "AA00AA")
        self.assertEqual(geography.location_locator((90, 180)), "RR99XX")

    def test_program_parsers_keep_qth_without_extra_requests(self):
        with patch.object(http, "get_json", side_effect=AssertionError("no extra request")):
            spots, _ = programs.select_pota([pota(grid6="ko02kf00")], NOW, 300)
            self.assertEqual(spots[0].qth, "KO02KF00")
            for program, reference in (("WWFF", "SPFF-0001"), ("SOTA", "SP/BZ-001")):
                payload = [{"activator": "IU1PZC", "reference": reference, "frequency_khz": 14052.9,
                            "spot_time": NOW, "mode": "CW", "latitude": 0, "longitude": 0}]
                selected, _, _ = programs.read_program(payload, program, NOW, 300)
                self.assertEqual(selected[0].qth, "JJ00AA")

    def test_qth_follows_full_or_truncated_name_and_remains_when_names_are_hidden(self):
        match = models.Match("IU1PZC", ("IT-0759",), 14052.9, [], {"IT-0759": "A long park name"},
                          reference_qths={"IT-0759": "JN34PX"})
        self.assertEqual(table_ui.reference_label(match, None), "IT-0759 (A long park name) [JN34PX]")
        self.assertEqual(table_ui.reference_label(match, 6), "IT-0759 (A lon…) [JN34PX]")
        self.assertEqual(table_ui.reference_label(match, 0), "IT-0759 [JN34PX]")


class QthCsvTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.events, self.daily = self.root / "events.csv", self.root / "daily.csv"
        self.match = models.Match("IU1PZC", ("IT-0759",), 14052.9,
                               [models.RbnSpot("IU1PZC", "SP5GQ", 14052.9, NOW - 10, 18, 23)],
                               {"IT-0759": 'Park, "with quotes"'}, reference_qths={"IT-0759": "JN34PX"})

    def seed(self):
        log = event_storage.EventLog(self.events, "events")
        log.record("NEW", NOW, self.match, spot_time=NOW - 30)
        log.close()
        summary = daily_storage.DailySummary(self.daily)
        summary.observe(self.match, NOW - 30)
        summary.close()

    def read(self, path):
        with path.open(encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            return reader.fieldnames, list(reader)

    def legacy(self, path):
        fields, rows = self.read(path)
        fields.remove("qth")
        legacy_rows = [{key: row[key] for key in fields} for row in rows]
        with path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            writer.writerows(legacy_rows)
        return path.read_bytes(), legacy_rows

    def test_both_csv_files_store_reference_qth_in_the_requested_place(self):
        self.seed()
        for path, preceding in ((self.events, "reference"), (self.daily, "name")):
            fields, rows = self.read(path)
            self.assertEqual(fields[fields.index(preceding) + 1], "qth")
            self.assertEqual(rows[0]["qth"], "JN34PX")

    def test_daily_qth_survives_missing_metadata_and_older_reports(self):
        self.seed()
        summary = daily_storage.DailySummary(self.daily)
        self.addCleanup(summary.close)
        no_metadata = models.Match("IU1PZC", ("IT-0759",), 14053.0, [])
        summary.observe(no_metadata, NOW)
        older = models.Match("IU1PZC", ("IT-0759",), 14050, [], reference_qths={"IT-0759": "JJ00AA"})
        summary.observe(older, NOW - 60)
        summary.flush()
        self.assertEqual(self.read(self.daily)[1][0]["qth"], "JN34PX")

    def test_legacy_files_are_upgraded_with_all_old_values_and_exact_backups(self):
        self.seed()
        original_event, event_rows = self.legacy(self.events)
        original_daily, daily_rows = self.legacy(self.daily)
        log = event_storage.EventLog(self.events, "events")
        log.close()
        summary = daily_storage.DailySummary(self.daily)
        summary.close()
        for path, original, expected in ((self.events, original_event, event_rows),
                                         (self.daily, original_daily, daily_rows)):
            fields, rows = self.read(path)
            self.assertIn("qth", fields)
            self.assertEqual([{k: v for k, v in row.items() if k != "qth"} for row in rows], expected)
            self.assertEqual(rows[0]["qth"], "")
            backups = list((self.root / ".csv-backups").glob("*/" + path.name))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)

    def test_invalid_legacy_summary_is_rejected_before_migration(self):
        self.seed()
        self.legacy(self.daily)
        saved = self.daily.read_text().replace("14052.9", "nan").encode()
        self.daily.write_bytes(saved)
        with self.assertRaises(ValueError):
            daily_storage.DailySummary(self.daily)
        self.assertEqual(self.daily.read_bytes(), saved)

    def test_restored_qth_provides_distance_and_azimuth_before_networking(self):
        self.seed()
        args = cli.arguments(["--log-file", str(self.events), "--summary-file", str(self.daily), "--qth", "KO02KF"])
        with patch.object(time, "time", return_value=NOW), \
             patch.object(http, "get_json", side_effect=AssertionError("must remain offline")):
            state = monitoring.Monitor(args)
        self.addCleanup(state.log.close)
        self.addCleanup(state.summary.close)
        entry, match = state.rows(NOW)[1][0]
        values = table_ui.row_values(entry, match, NOW, history=True, qth=args.qth)
        self.assertIn('[JN34PX]', values[2])
        self.assertNotEqual(values[14], "—")
        self.assertNotEqual(values[16], "—")
        self.assertEqual(values[7], "—")

    def test_restore_notice_does_not_hide_missing_home_qth_warning_on_short_screen(self):
        self.seed()
        with patch.dict(os.environ), patch.object(time, "time", return_value=NOW):
            os.environ.pop("CW_ACTIVATION_QTH", None)
            args = cli.arguments(["--log-file", str(self.events), "--summary-file", str(self.daily)])
            state = monitoring.Monitor(args)
        self.addCleanup(state.log.close)
        self.addCleanup(state.summary.close)
        frame = views_ui.screen_frame(state, NOW, 8, 80)
        self.assertTrue(any("QTH not set" in text for text, _ in frame))


if __name__ == "__main__":
    unittest.main()
