import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cw_monitor.storage.events import EventLog
from cw_monitor.storage.files import lock_csv


class EventCsvSchemaTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "events.csv"
        self.row = dict.fromkeys(EventLog.fields, "")
        self.row.update(timestamp_utc="2026-11-12T07:00:00+00:00", source_time_utc="2026-11-12T06:59:00+00:00",
                        event="NEW", program="POTA", callsign="IU1PZC", reference="IT-0759", qth="JN35PA",
                        band="20m", spot_khz="14052.9", rbn_khz="14052.9/14053.0", spotters="SP5GQ/SQ5J",
                        snr_db="18/9", wpm="23/25", detail='Park Świętokrzyski, "test"\nsecond line')

    def seed_legacy(self, qth=True):
        fields = ["receivers" if name == "spotters" else name for name in EventLog.fields if qth or name != "qth"]
        with self.path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(fields)
            writer.writerow([self.row["spotters" if name == "receivers" else name] for name in fields])
        return self.path.read_bytes()

    def read(self):
        with self.path.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source, strict=True)
            return reader.fieldnames, list(reader)

    def test_legacy_names_with_or_without_qth_upgrade_and_append_without_data_loss(self):
        for with_qth in (True, False):
            with self.subTest(with_qth=with_qth):
                original = self.seed_legacy(qth=with_qth)
                log = EventLog(self.path, "events")
                try:
                    self.assertEqual(list(log.records())[0]["spotters"], "SP5GQ/SQ5J")
                    log.record("TEST", 1, detail="new row")
                finally:
                    log.close()
                fields, rows = self.read()
                self.assertEqual(tuple(fields), EventLog.fields)
                self.assertNotIn("receivers", fields)
                expected = dict(self.row, qth=self.row["qth"] if with_qth else "")
                self.assertEqual(rows[0], expected)
                self.assertEqual(rows[1]["event"], "TEST")
                self.assertEqual(rows[1]["detail"], "new row")
                backups = list((self.path.parent / ".csv-backups").glob("*/events.csv"))
                self.assertTrue(any(p.read_bytes() == original for p in backups))

    def test_reopening_current_schema_does_not_migrate_or_create_another_backup(self):
        self.seed_legacy()
        EventLog(self.path, "events").close()
        original = self.path.read_bytes()
        backups = list((self.path.parent / ".csv-backups").glob("*/events.csv"))
        EventLog(self.path, "events").close()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list((self.path.parent / ".csv-backups").glob("*/events.csv")), backups)

    def test_invalid_legacy_rows_leave_original_untouched(self):
        valid = self.seed_legacy()
        header = valid.split(b"\r\n", 1)[0] + b"\r\n"
        invalid_rows = [b"only,two\r\n", b"x," * len(EventLog.fields) + b"extra\r\n", b'"unterminated']
        for row in invalid_rows:
            with self.subTest(row=row):
                original = header + row
                self.path.write_bytes(original)
                with self.assertRaises(ValueError):
                    EventLog(self.path, "events")
                self.assertEqual(self.path.read_bytes(), original)
                self.assertEqual(list(self.path.parent.glob(".events.csv.*.tmp")), [])

    def test_replace_failure_preserves_original_and_retry_migrates(self):
        original = self.seed_legacy()
        with patch("cw_monitor.storage.files.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                EventLog(self.path, "events")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob(".events.csv.*.tmp")), [])
        EventLog(self.path, "events").close()
        self.assertEqual(self.read()[1][0], self.row)

    def test_active_writer_lock_prevents_migration(self):
        original = self.seed_legacy()
        with lock_csv(self.path, "Test writer"):
            with self.assertRaisesRegex(ValueError, "already in use"):
                EventLog(self.path, "events")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse((self.path.parent / ".csv-backups").exists())

    def test_conflicting_old_and_new_columns_are_rejected_without_overwrite(self):
        original = self.seed_legacy().replace(b",receivers,", b",receivers,spotters,", 1)
        self.path.write_bytes(original)
        with self.assertRaisesRegex(ValueError, "different CSV header"):
            EventLog(self.path, "events")
        self.assertEqual(self.path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
