import contextlib
from datetime import datetime
import io
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from cw_monitor import cli
from cw_monitor import config
from cw_monitor import models
from cw_monitor import monitor as monitoring
from cw_monitor import values as values_api
from cw_monitor.sources import http
from cw_monitor.sources import programs
from cw_monitor.sources import rbn as rbn_source
import time


NOW = datetime(2026, 11, 10, 10, 30, tzinfo=config.UTC).timestamp()
FIELDS = ["de", "freq", "dx", "db", "wpm", "time", "diff", "band", "type", "mode", "epoch"]


def is_row(text, call):
    fields = text.replace("│", " ").replace("|", " ").split()
    return len(fields) > 1 and fields[0] in config.PROGRAMS and fields[1] == call


def pota(age=30, **overrides):
    item = {"activator": "IU1PZC", "reference": "IT-0759", "frequency": "14052.9",
            "mode": "CW", "spotTime": datetime.fromtimestamp(NOW - age, config.UTC).isoformat(),
            "comments": "CQ POTA", "invalid": None}
    item.update(overrides)
    return item


def rbn(age=42, receiver="SP5GQ", call="IU1PZC", freq=14052.9, snr=18):
    return models.RbnSpot(call, receiver, freq, NOW - age, snr, 23)


def raw_rbn(call="IU1PZC", receiver="SP5GQ", mode=1, age=42):
    return [receiver, "14052.9", call, 18, 23, "1029z 10 Nov", age, 22, 1, mode, NOW - age]


class PotaTests(unittest.TestCase):
    def test_utc_without_offset_and_with_offset(self):
        self.assertEqual(values_api.timestamp("2026-11-10T10:30:00"), NOW)
        self.assertEqual(values_api.timestamp("2026-11-10T10:30:00Z"), NOW)
        self.assertEqual(values_api.timestamp("2026-11-10T11:30:00+01:00"), NOW)

    def test_only_fresh_cw_not_qrt_invalid_or_future(self):
        data = [pota(300), pota(301, activator="OLD1"), pota(-1, activator="FUTURE1"),
                pota(mode="SSB", activator="SSB1"), pota(comments="tnx QRT 73", activator="QRT1"),
                pota(invalid="invalid", activator="BAD1")]
        selected, skipped = programs.select_pota(data, NOW, 300)
        self.assertEqual([s.call for s in selected], ["IU1PZC"])
        self.assertEqual(skipped, 0)

    def test_new_qrt_or_mode_change_overrides_old_cw(self):
        for override in ({"comments": "QRT"}, {"mode": "SSB"}):
            with self.subTest(override=override):
                self.assertEqual(programs.select_pota([pota(60), pota(5, **override)], NOW, 300)[0], [])

    def test_duplicate_activations_and_invalid_records(self):
        selected, skipped = programs.select_pota([pota(60), pota(10), None, pota(parkName=None,
                                                 reference="IT-0760", frequency="nan")], NOW, 300)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].time, NOW - 10)
        self.assertEqual(skipped, 2)

    def test_schema_error_and_empty_feed_are_different(self):
        self.assertEqual(programs.select_pota([], NOW, 300), ([], 0))
        for data in ({"error": "unavailable"}, [{"wrong": "schema"}]):
            with self.assertRaises(models.SourceError):
                programs.select_pota(data, NOW, 300)

    def test_park_name_is_read_from_payload_and_has_no_terminal_controls(self):
        for fields in ({"name": "Ojcowski Park Narodowy"},
                       {"name": None, "parkName": "Ojcowski Park Narodowy"}):
            with self.subTest(fields=fields):
                result, _ = programs.select_pota([pota(**fields)], NOW, 300)
                self.assertEqual(result[0].name, "Ojcowski Park Narodowy")
        result, _ = programs.select_pota([pota(name="Ojcowski\x1b  Park\x07")], NOW, 300)
        self.assertEqual(result[0].name, "Ojcowski Park")


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.spots = programs.select_pota([pota()], NOW, 300)[0]

    def join(self, data, pota_data=None, now=NOW):
        return programs.join_spots(self.spots if pota_data is None else pota_data, data,
                              ["SP5GQ", "SQ5J"], now, 300, 300, 0.5)

    def test_both_receivers_and_latest_reception(self):
        result = self.join([rbn(50, snr=30), rbn(10, snr=8), rbn(20, receiver="SQ5J")])
        self.assertEqual(len(result), 1)
        self.assertEqual([r.receiver for r in result[0].receptions], ["SP5GQ", "SQ5J"])
        self.assertEqual(result[0].receptions[0].snr, 8)

    def test_other_calls_bands_receivers_expired_and_future_are_excluded(self):
        self.assertEqual(self.join([rbn(call="IU1PZC/P"), rbn(freq=7052.9), rbn(receiver="OTHER"),
                                    rbn(age=301), rbn(age=-1)]), [])

    def test_age_and_frequency_boundaries(self):
        self.assertEqual(len(self.join([rbn(age=300, freq=14053.4)])), 1)
        self.assertEqual(self.join([rbn(freq=14053.401)]), [])

    def test_pota_expires_even_if_rbn_is_fresh(self):
        self.assertEqual(self.join([rbn(age=-280)], now=NOW + 280), [])

    def test_multiple_parks_have_separate_rows(self):
        more = self.spots + [models.ActivationSpot("IU1PZC", "IT-0760", 14052.9, NOW - 40)]
        result = self.join([rbn()], more)
        self.assertEqual(len(result), 2)
        self.assertEqual({m.references for m in result}, {("IT-0759",), ("IT-0760",)})

    def test_alerts_do_not_repeat_when_known_band_returns_from_history(self):
        state = monitoring.Monitor(cli.arguments([]))
        snapshot = models.Snapshot(self.spots, [rbn()], [], NOW)
        self.assertEqual(len(state.apply(snapshot, NOW)), 1)
        self.assertEqual(state.apply(snapshot, NOW + 60), [])
        state.apply(models.Snapshot([], [], [], NOW + 120), NOW + 120)
        self.assertEqual(state.apply(snapshot, NOW + 180), [])
        self.assertEqual(len(state.rows(NOW + 180)[1]), 1)


class RbnClientTests(unittest.TestCase):
    def setUp(self):
        clock = patch.object(time, "time", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def client(self):
        client = rbn_source.RbnClient(5)
        client.version, client.fields, client.cw_mode = "v1", FIELDS, "1"
        return client

    def test_parse_real_schema_and_receiver_marker(self):
        data = {"ver_h": "v1", "spots": {"1": raw_rbn(receiver="SP5GQ-#"),
                                                 "2": raw_rbn(receiver="SQ5J"),
                                                 "3": raw_rbn(mode=34),
                                                 "4": raw_rbn(call="OTHER")}}
        with patch.object(http, "get_json", return_value=data) as get:
            result = self.client().fetch(["IU1PZC"], ["SP5GQ", "SQ5J"], 300)
        self.assertEqual([s.receiver for s in result], ["SP5GQ", "SQ5J"])
        params = parse_qs(urlsplit(get.call_args.args[0]).query)
        self.assertEqual(params["cde"], ["SP5GQ,SQ5J"])
        self.assertNotIn("cdx", params)
        self.assertEqual(params["r"], ["10"])
        self.assertEqual(params["ma"], ["300"])

    def test_no_calls_means_no_network(self):
        with patch.object(http, "get_json") as get, patch.object(http, "get_text") as text:
            self.assertEqual(rbn_source.RbnClient(5).fetch([], ["SP5GQ"], 300), [])
        get.assert_not_called()
        text.assert_not_called()

    def test_initialization_reads_metadata(self):
        with patch.object(http, "get_text", return_value='window.RBN_version_hash = "abc123";'), \
             patch.object(http, "get_json", return_value={"spot_fields": FIELDS,
                                                       "modes": {"7": {"mode": "cw"}}}):
            client = rbn_source.RbnClient(5)
            client.initialize()
        self.assertEqual((client.version, client.cw_mode), ("abc123", "7"))

    def test_changed_version_refreshes_schema_and_retries(self):
        client = self.client()

        def initialize():
            client.version = "v2"

        with patch.object(client, "initialize", side_effect=initialize) as init, \
             patch.object(http, "get_json", side_effect=[{"ver_h": "v2"},
                                                       {"ver_h": "v2", "spots": {"1": raw_rbn()}}]):
            result = client.fetch(["IU1PZC"], ["SP5GQ"], 300)
        init.assert_called_once()
        self.assertEqual(len(result), 1)

    def test_row_limit_splits_calls_then_receivers(self):
        queries = []

        def get(url, timeout):
            params = parse_qs(urlsplit(url).query)
            queries.append(params)
            calls, receivers = params.get("cdx", ["IU1PZC,YO5BDL"])[0].split(","), params["cde"][0].split(",")
            size = int(params["r"][0]) if len(calls) > 1 or len(receivers) > 1 else 1
            return {"ver_h": "v1", "spots": {f"{calls[0]}-{receivers[0]}-{i}": raw_rbn(call=calls[0], receiver=receivers[0])
                                             for i in range(size)}}

        client = self.client()
        client.small_batches.set()
        with patch.object(http, "get_json", side_effect=get):
            result = client.fetch(["IU1PZC", "YO5BDL"], ["SP5GQ", "SQ5J"], 300)
        self.assertEqual({(r.call, r.receiver) for r in result},
                         {(c, r) for c in ("IU1PZC", "YO5BDL") for r in ("SP5GQ", "SQ5J")})
        self.assertEqual([int(q["r"][0]) for q in queries[:4]], [10, 20, 50, 100])
        self.assertEqual(len(queries), 11)

    def test_single_pair_limit_emits_warning(self):
        data = {"ver_h": "v1", "spots": {str(i): raw_rbn() for i in range(100)}}
        client = self.client()
        with patch.object(http, "get_json", return_value=data):
            client.fetch(["IU1PZC"], ["SP5GQ"], 300)
        self.assertIn("incomplete", client.warnings[0])

    def test_schema_and_server_errors_are_not_empty_success(self):
        for data in ({"ver_h": "v1", "error": "unavailable"},
                     {"ver_h": "v1", "spots": {"1": [1, 2]}},
                     {"ver_h": "v1", "spots": "wrong"}):
            with self.subTest(data=data), patch.object(http, "get_json", return_value=data):
                with self.assertRaises(models.SourceError):
                    self.client().fetch(["IU1PZC"], ["SP5GQ"], 300)


class CommandTests(unittest.TestCase):
    def test_demo_is_offline_and_has_expected_table(self):
        output = io.StringIO()
        with patch.object(http, "urlopen", side_effect=AssertionError("Sieć w demo")), \
             contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["--demo", "--links"]), 0)
        text = output.getvalue()
        for value in ("FICTIONAL DATA", "IU1PZC", "SP5GQ / SQ5J", "18", "23", "00:00:42",
                      "https://www.reversebeacon.net/main.php?", "https://pota.app/#/park/IT-0759"):
            self.assertIn(value, text)
        self.assertNotIn("\033", text)

    def test_once_failure_has_exit_code_one_and_no_empty_table_claim(self):
        output = io.StringIO()
        with patch.object(http, "get_json", side_effect=models.SourceError("HTTP 503")), \
             contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["--once", "--programs", "POTA"]), 1)
        self.assertIn("SOURCE ERROR", output.getvalue())
        self.assertNotIn("No fresh confirmations", output.getvalue())

    def test_once_full_flow_with_controlled_time(self):
        output = io.StringIO()
        response = {"ver_h": "v1", "spots": {"1": raw_rbn()}}
        meta = {"spot_fields": FIELDS, "modes": {"1": {"mode": "cw"}}}
        with patch.object(time, "time", return_value=NOW), \
             patch.object(http, "get_json", side_effect=[[pota()], meta, response]), \
             patch.object(http, "get_text", return_value='RBN_version_hash = "v1";'), \
             contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["--once", "--programs", "POTA"]), 0)
        self.assertIn("confirmed: 1", output.getvalue())
        self.assertIn("ALERT: IU1PZC", output.getvalue())

    def test_bad_cli_values_are_rejected(self):
        for option in (["--interval", "1"], ["--tolerance", "nan"],
                       ["--timeout", "0"], ["--spotters", "SQ5J\nINJECT"]):
            with self.subTest(option=option), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exc:
                    cli.arguments(option)
                self.assertEqual(exc.exception.code, 2)

    def test_receiver_normalization_preserves_ssid(self):
        self.assertEqual(values_api.receiver_call("sp5gq-#"), "SP5GQ")
        self.assertEqual(values_api.receiver_call("SQ5J-2"), "SQ5J-2")

    def test_external_control_characters_are_removed(self):
        self.assertEqual(values_api.clean("abc\x1b\n\r\x07def"), "abcdef")


if __name__ == "__main__":
    unittest.main()
