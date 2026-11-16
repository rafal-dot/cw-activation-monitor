"""Individual bad service records stay diagnostic; whole-source failures stay visible."""

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from cw_monitor import cli, models, monitor, polling
from cw_monitor.sources import http
from cw_monitor.sources.rbn import RbnClient
from cw_monitor.ui import views
from test_pota_alert import NOW, FIELDS, pota, raw_rbn
from test_programs import wwff


class QuietRecordTests(unittest.TestCase):
    def test_partial_program_records_are_skipped_and_counted_only_in_debug_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            for program, valid in [("POTA", pota()), ("WWFF", wwff()), ("SOTA", {
                    "activator": "IU1PZC", "reference": "I/PM-001", "frequency_khz": 14052.9,
                    "spot_time": NOW - 10, "mode": "CW"})]:
                with self.subTest(program=program):
                    payload = [valid, None]
                    path = Path(directory) / "sota.json"
                    path.write_text(json.dumps(payload))
                    options = ["--programs", program] + (["--sota-file", str(path)] if program == "SOTA" else [])
                    poller = polling.Poller(cli.arguments(options))
                    poller.log = Mock()
                    with patch.object(time, "time", return_value=NOW), patch.object(http, "get_json", return_value=payload):
                        spots, failed, warnings, errors, inactive = poller.read_sources()
                    self.assertEqual(len(spots), 1)
                    self.assertEqual((failed, warnings, errors, inactive), (set(), [], [], set()))
                    poller.log.record.assert_called_once_with("SOURCE_READ", NOW,
                        detail=f"{program}: CW=1, skipped=1, inactive=0", debug=True)

    def test_unreadable_program_feed_still_reaches_the_main_error_report(self):
        args = cli.arguments(["--programs", "POTA"])
        poller = polling.Poller(args)
        with patch.object(time, "time", return_value=NOW), patch.object(http, "get_json", return_value=[None]):
            snapshot = poller.fetch()
        self.assertEqual(snapshot.failed_programs, {"POTA"})
        self.assertTrue(any("could not parse any entries" in text for text in snapshot.errors))
        state = monitor.Monitor(args)
        state.apply(snapshot, NOW)
        self.assertIn("SOURCE ERROR:", views.render(state, NOW))

    def test_partial_rbn_rows_are_quiet_and_do_not_prove_complete_coverage(self):
        client = RbnClient(5)
        client.version, client.fields, client.cw_mode = "v1", FIELDS, "1"
        bad = raw_rbn()
        bad[FIELDS.index("db")] = "invalid"
        payload = {"ver_h": "v1", "spots": {"good": raw_rbn(), "bad": bad}}
        with patch.object(time, "time", return_value=NOW), patch.object(http, "get_json", return_value=payload):
            received = client.fetch(["IU1PZC"], ["SP5GQ"], 300)
        self.assertEqual(len(received), 1)
        self.assertEqual(client.warnings, [])
        self.assertTrue(client.request_log)
        self.assertTrue(all("skipped=1" in text for text in client.request_log))
        self.assertFalse(client.status.complete)
        self.assertIsNone(client.status.average_per_minute)

    def test_wholly_invalid_rbn_page_still_raises_source_error(self):
        client = RbnClient(5)
        client.version, client.fields, client.cw_mode = "v1", FIELDS, "1"
        bad = raw_rbn()
        bad[FIELDS.index("db")] = "invalid"
        with patch.object(http, "get_json", return_value={"ver_h": "v1", "spots": {"bad": bad}}):
            with self.assertRaisesRegex(models.SourceError, "could not parse any spots"):
                client.fetch(["IU1PZC"], ["SP5GQ"], 300)
