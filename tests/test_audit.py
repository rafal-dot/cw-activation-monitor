"""Regression coverage for external data boundaries and bounded service traffic."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from cw_monitor import cli, config, models, polling
from cw_monitor.sources import http, programs
from cw_monitor.sources.rbn import RbnClient
from test_pota_alert import NOW, FIELDS, pota, raw_rbn
from test_programs import wwff


class InputAuditTests(unittest.TestCase):
    def test_converted_frequency_overflow_is_invalid_and_two_metres_remains_valid(self):
        row = wwff(frequency="1e308")
        del row["frequency_khz"]
        with self.assertRaises(models.SourceError):
            programs.read_program([row], "WWFF", NOW, 300)
        self.assertEqual(programs.read_pota([pota(frequency=144071.1)], NOW, 300)[0][0].frequency, 144071.1)

    def test_future_duplicate_does_not_hide_valid_activity(self):
        rows = programs.read_pota([pota(age=30), pota(age=-60)], NOW, 300)[0]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].time, NOW - 30)
        rows = programs.read_program([wwff(age=30), wwff(age=-60)], "WWFF", NOW, 300)[0]
        self.assertEqual(rows[0].time, NOW - 30)

    def test_nonstandard_and_excessively_nested_json_are_reported(self):
        for payload in ('{"value":NaN}', '[Infinity]', '[' * 2000 + '0' + ']' * 2000):
            with patch.object(http, "get_text", return_value=payload), self.assertRaises(models.SourceError):
                http.get_json("https://example.test/data", 5)

    def test_local_sota_rejects_nonstandard_numbers_without_http(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sota.json"
            path.write_text('[NaN]')
            args = cli.arguments(["--programs", "SOTA", "--sota-file", str(path)])
            with patch.object(http, "get_json", side_effect=AssertionError("local file must not use HTTP")):
                with self.assertRaises(models.SourceError):
                    polling.Poller(args).read_source("SOTA")

    def test_calls_and_cli_spotter_count_are_bounded(self):
        self.assertIsNone(config.CALL_PATTERN.fullmatch("A" * 65))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.arguments(["--spotters", "A1", "A2", "A3", "A4", "A5", "A6"])

    def test_rbn_rejects_fractional_boolean_and_out_of_range_readings(self):
        client = RbnClient(5)
        client.version, client.fields, client.cw_mode = "v1", FIELDS, "1"
        for field, value in (("wpm", 20.5), ("wpm", -1), ("wpm", True), ("db", 101), ("dx", "A" * 65)):
            good = raw_rbn()
            bad = good.copy()
            bad[FIELDS.index(field)] = value
            payload = {"ver_h": "v1", "spots": {"good": good, "bad": bad}}
            with patch.object(http, "get_json", return_value=payload):
                client.remaining_requests = 8
                spots, _, skipped = client._request(None, ["SP5GQ"], 300, 10)
            self.assertEqual(set(spots), {"good"})
            self.assertEqual(skipped, 1)


class ServiceAuditTests(unittest.TestCase):
    def test_error_backoff_increases_and_honours_retry_after(self):
        poller = polling.Poller(cli.arguments([]))
        with patch("time.monotonic", return_value=100):
            poller.backoff("POTA", models.SourceError("HTTP 503"), 60)
            self.assertEqual(poller.retry_at["POTA"], 160)
            poller.backoff("POTA", models.SourceError("HTTP 503"), 60)
            self.assertEqual(poller.retry_at["POTA"], 220)
            poller.backoff("POTA", models.SourceError("HTTP 429", 600), 60)
            self.assertEqual(poller.retry_at["POTA"], 700)
        self.assertNotIn("WWFF", poller.retry_at)

    def test_saturated_batches_preserve_partial_results_and_rotate_calls_within_groups(self):
        client = RbnClient(5)
        client.version, client.fields, client.cw_mode = "v1", FIELDS, "1"
        client.small_batches.set()
        queries = []
        calls = [f"TEST{i:02d}" for i in range(40)]

        def get(url, timeout):
            q = parse_qs(urlsplit(url).query)
            queries.append(q)
            call = q.get("cdx", ["BUSY1"])[0].split(',')[0]
            return {"ver_h": "v1", "spots": {f"{call}-{i}": raw_rbn(call=call, age=i * .1)
                                             for i in range(int(q["r"][0]))}}

        with patch.object(http, "get_json", side_effect=get), patch("time.time", return_value=NOW):
            rows = client.fetch(calls, ["SP5GQ"], 300)
            self.assertLessEqual(len(queries), 8)
            self.assertTrue(rows)
            self.assertTrue(any("budget" in w for w in client.warnings))
            first = next(q["cdx"][0].split(',')[0] for q in queries if "cdx" in q)
            queries.clear()
            rows = client.fetch(calls, ["SP5GQ"], 300)
            self.assertLessEqual(len(queries), 8)
            next_call = next(q["cdx"][0].split(',')[0] for q in queries if "cdx" in q)
            self.assertNotEqual(first, next_call)
            self.assertTrue({first, next_call}.issubset({r.call for r in rows}))
