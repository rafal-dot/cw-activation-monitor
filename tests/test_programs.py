import contextlib
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import patch

from cw_monitor import cli
from cw_monitor import config
from cw_monitor import models
from cw_monitor import monitor as monitoring
from cw_monitor import polling
from cw_monitor import values as values_api
from cw_monitor.sources import http
from cw_monitor.sources import programs
from cw_monitor.ui import layout as layout_ui
from cw_monitor.ui import views as views_ui
import time
from test_pota_alert import NOW, is_row, pota, rbn


def wwff(age=30, **overrides):
    data = {"activator": "IU1PZC", "reference": "IFFF-0001", "reference_name": "WWFF park",
            "frequency_khz": 14052.9, "mode": "CW", "spot_time": NOW - age,
            "remarks": "CQ", "latitude": 45, "longitude": 7}
    data.update(overrides)
    return data


def sota(age=30, **overrides):
    data = {"activatorCallsign": "IU1PZC", "associationCode": "I", "summitCode": "PM-001",
            "summitDetails": "Demo summit", "frequency": "14.0529", "mode": "CW",
            "timeStamp": datetime.fromtimestamp(NOW - age, config.UTC).isoformat(), "comments": "CQ"}
    data.update(overrides)
    return data


class ProgramTests(unittest.TestCase):
    def test_default_programs_separate_age_limits_and_validation(self):
        args = cli.arguments([])
        self.assertEqual(args.programs, ["POTA", "WWFF"])
        self.assertEqual([values_api.max_spot_age(args, p) for p in config.PROGRAMS], [300, 600, 900])
        custom = cli.arguments(["--pota-max-age", "60", "--sota-max-age", "120", "--wwff-max-age", "180"])
        self.assertEqual([values_api.max_spot_age(custom, p) for p in config.PROGRAMS], [60, 120, 180])
        for option in ("--sota-max-age", "--wwff-max-age"):
            for value in ("0", "-1", "nan", "inf"):
                with self.subTest(option=option, value=value), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    cli.arguments([option, value])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.arguments(["--programs", "SOTA"])
        self.assertEqual(cli.arguments(["--programs", "wwff", "WWFF"]).programs, ["WWFF"])
        self.assertEqual(cli.arguments(["--reference-name-length", "20"]).park_name_length, 20)

    def test_adapters_normalize_frequency_timestamps_names_and_location(self):
        for program, payload in (("WWFF", wwff()), ("SOTA", sota())):
            spots, skipped, inactive = programs.read_program([payload], program, NOW, 900)
            self.assertEqual((skipped, inactive), (0, set()))
            self.assertEqual(spots[0].frequency, 14052.9)
            self.assertEqual(spots[0].time, NOW - 30)
            self.assertEqual(spots[0].program, program)
            self.assertTrue(spots[0].name)
        self.assertEqual(programs.read_program([wwff()], "WWFF", NOW, 900)[0][0].location, (45, 7))
        normalized = wwff(reference="SP/BZ-001")
        self.assertEqual(programs.read_program([normalized], "SOTA", NOW, 600)[0][0].reference, "SP/BZ-001")

    def test_latest_qrt_and_mode_change_do_not_revive_previous_cw(self):
        for program, make in (("WWFF", wwff), ("SOTA", sota)):
            for metadata in ({"mode": "SSB"}, {"remarks": "QRT", "comments": "QRT"}, {"invalid": True}):
                old, new = make(age=60), make(age=5, **metadata)
                spots, skipped, inactive = programs.read_program([new, old], program, NOW, 900)
                self.assertEqual(spots, [])
                self.assertEqual(skipped, 0)
                self.assertEqual(len(inactive), 1)
                self.assertEqual(next(iter(inactive))[3], program)

    def test_parsers_reject_invalid_data_but_keep_other_entries(self):
        spots, skipped, _ = programs.read_program([wwff(), wwff(spot_time="bad"), None,
                                            wwff(reference="bad"), wwff(frequency_khz=-1)], "WWFF", NOW, 900)
        self.assertEqual((len(spots), skipped), (1, 4))
        for payload in ({}, [None], [wwff(spot_time=float("inf"))]):
            with self.assertRaises(models.SourceError):
                programs.read_program(payload, "WWFF", NOW, 900)
        self.assertEqual(programs.read_program([], "WWFF", NOW, 900), ([], 0, set()))
        with self.assertRaises(models.SourceError):
            programs.read_program([{"associationCode": "DEPRECATED"}], "SOTA", NOW, 600)

    def test_each_program_uses_its_own_age_for_matching_and_history(self):
        args = cli.arguments([])
        state = monitoring.Monitor(args)
        spots = [models.ActivationSpot("IU1PZC", "IT-0001", 14052.9, NOW, program=p) for p in config.PROGRAMS]
        self.assertEqual(len(state.apply(models.Snapshot(spots, [rbn()], [], NOW), NOW)), 3)
        self.assertEqual(len(state.entries), 3)
        # Same callsign/band/reference across programs remains independent.
        for elapsed, statuses in ((301, [False, True, True]), (601, [False, False, True]), (901, [False] * 3)):
            state.rows(NOW + elapsed)
            self.assertEqual([state.entries[s.key].active_match(NOW + elapsed) is not None for s in spots], statuses)
        state.rows(NOW + 2101)
        self.assertNotIn(spots[0].key, {m.key for _, m in state.rows(NOW + 2101)[1]})
        self.assertIn(spots[2].key, {m.key for _, m in state.rows(NOW + 2101)[1]})
        # A new record older than POTA's limit may still be accepted by WWFF.
        state = monitoring.Monitor(args)
        aged = [models.ActivationSpot("IU1PZC", "IT-0001", 14052.9, NOW - 700, program=p) for p in config.PROGRAMS]
        state.apply(models.Snapshot(aged, [rbn()], [], NOW), NOW)
        self.assertEqual({m.program for _, m in state.rows(NOW)[0]}, {"WWFF"})

    def test_explicit_qrt_does_not_disable_other_program_or_announce_new_band(self):
        state = monitoring.Monitor(cli.arguments([]))
        p = models.ActivationSpot("IU1PZC", "IT-0001", 14052.9, NOW)
        w = models.ActivationSpot("IU1PZC", "IFFF-0001", 7028, NOW, program="WWFF")
        state.apply(models.Snapshot([p], [rbn()], [], NOW), NOW)
        state.apply(models.Snapshot([p, w], [rbn(freq=7028)], [], NOW), NOW)
        self.assertEqual(state.entries[w.key].previous_bands, ())
        state.apply(models.Snapshot([w], [], [], NOW + 10, {p.key}), NOW + 10)
        self.assertIsNone(state.entries[p.key].active_match(NOW + 10))
        self.assertIsNotNone(state.entries[w.key].active_match(NOW + 10))

    def test_program_is_first_column_and_links_are_program_specific(self):
        state = monitoring.Monitor(cli.arguments([]))
        spots = [models.ActivationSpot("IU1PZC", "IT-0001", 14052.9, NOW, program=p) for p in config.PROGRAMS]
        state.apply(models.Snapshot(spots, [rbn()], [], NOW), NOW)
        for width in (40, 80, 120, 260):
            frame = views_ui.screen_frame(state, NOW, 50, width)
            header = next(text for text, _ in frame if "Spot kHz" in text)
            self.assertTrue(header.startswith("Program"))
            rows = [text for text, _ in frame if is_row(text, "IU1PZC")]
            self.assertEqual({text.split()[0] for text in rows}, set(config.PROGRAMS))
            self.assertTrue(all(layout_ui.cell_width(text) < width for text in rows))
        rendered = views_ui.render(state, NOW)
        self.assertIn("Last spot", rendered)
        self.assertIn("POTA ≤ 300s", rendered)
        self.assertIn("WWFF ≤ 900s", rendered)
        for spot in spots:
            match = state.entries[spot.key].match
            links = views_ui.links_for(match, state.args)
            self.assertIn(spot.program + ": https://", links[1])
            if spot.program != "POTA":
                self.assertNotIn("pota.app", " ".join(links))


class MultiSourcePollerTests(unittest.TestCase):
    def test_one_rbn_query_for_same_callsign_across_programs_and_cached_confirmation(self):
        poller = polling.Poller(cli.arguments([]))
        def source(url, timeout):
            return [pota()] if url == config.POTA_URL else [wwff(age=700)]
        with patch.object(http, "get_json", side_effect=source), patch.object(time, "time", return_value=NOW), \
             patch.object(poller.rbn, "fetch", side_effect=[[rbn()], []]) as fetch:
            first, second = poller.fetch(), poller.fetch()
        self.assertEqual(fetch.call_args_list[0].args[0], ["IU1PZC"])
        self.assertEqual(fetch.call_args_list[1].args[0], [])
        self.assertEqual(len(poller.confirmed), 2)
        self.assertEqual({p.program for p in first.spots}, {"POTA", "WWFF"})
        self.assertEqual(second.rbn[0].time, NOW - 42)

    def test_sources_start_independently_and_failure_retains_last_known_state(self):
        poller = polling.Poller(cli.arguments([]))
        state = monitoring.Monitor(poller.args)
        blocked, other_started, release = Event(), Event(), Event()
        phase = [0]
        def source(url, timeout):
            if phase[0] and url == config.POTA_URL:
                blocked.set()
                if not release.wait(2):
                    raise AssertionError("test did not release blocked source")
                raise models.SourceError("HTTP 503")
            if phase[0]:
                other_started.set()
            return [pota()] if url == config.POTA_URL else [wwff()]
        with patch.object(http, "get_json", side_effect=source), patch.object(time, "time", return_value=NOW), \
             patch.object(poller.rbn, "fetch", return_value=[rbn()]):
            state.apply(poller.fetch(), NOW)
            phase[0] = 1
            result = []
            worker = Thread(target=lambda: result.append(poller.fetch()), daemon=True)
            worker.start()
            try:
                self.assertTrue(blocked.wait(1))
                self.assertTrue(other_started.wait(1))
            finally:
                release.set()
                worker.join(3)
            self.assertFalse(worker.is_alive())
        data = result[0]
        self.assertEqual(data.failed_programs, {"POTA"})
        self.assertIn("HTTP 503", data.errors[0])
        state.apply(data, NOW + 20)
        self.assertEqual(len(state.rows(NOW + 20)[0]), 2)
        self.assertTrue(all(not e.archived for e in state.entries.values()))
        self.assertIn("SOURCE ERROR", views_ui.render(state, NOW + 20))

    def test_retry_after_pauses_only_failed_source(self):
        poller = polling.Poller(cli.arguments([]))
        clock = [0]
        calls = []
        def source(url, timeout):
            calls.append(url)
            if url == config.POTA_URL:
                raise models.SourceError("HTTP 429", retry_after=120)
            return [wwff()]
        with patch.object(http, "get_json", side_effect=source), patch.object(time, "time", return_value=NOW), \
             patch.object(time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(poller.rbn, "fetch", return_value=[rbn()]):
            poller.fetch()
            clock[0] = 60
            second = poller.fetch()
            self.assertEqual(calls.count(config.POTA_URL), 1)
            self.assertEqual(calls.count(config.WWFF_URL), 2)
            self.assertIn("Retry-After", second.errors[0])
            clock[0] = 120
            poller.fetch()
        self.assertEqual(calls.count(config.POTA_URL), 2)

    def test_rbn_error_does_not_discard_source_updates_or_cached_receptions(self):
        poller = polling.Poller(cli.arguments([]))
        with patch.object(poller, "read_source", side_effect=lambda p: programs.read_pota([pota()], NOW, 300)
                         if p == "POTA" else programs.read_program([wwff()], p, NOW, 900)), \
             patch.object(time, "time", return_value=NOW), \
             patch.object(poller.rbn, "fetch", side_effect=[[rbn()], models.SourceError("HTTP 503")]):
            first, second = poller.fetch(), poller.fetch()
        self.assertEqual(second.spots, first.spots)
        self.assertEqual(second.rbn, first.rbn)
        self.assertIn("RBN", second.errors[0])

    def test_sota_local_file_is_reloaded_without_any_sota_network_request(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sota.json"
            path.write_text(json.dumps([sota()]))
            args = cli.arguments(["--programs", "SOTA", "--sota-file", str(path)])
            poller = polling.Poller(args)
            state = monitoring.Monitor(args)
            with patch.object(http, "get_json", side_effect=AssertionError("unexpected network request")), \
                 patch.object(time, "time", return_value=NOW), \
                 patch.object(poller.rbn, "fetch", return_value=[rbn()]):
                first = poller.fetch()
                state.apply(first, NOW)
                path.write_text("incomplete JSON")
                failed = poller.fetch()
                state.apply(failed, NOW + 10)
                self.assertEqual(len(state.rows(NOW + 10)[0]), 1)
                self.assertEqual(failed.failed_programs, {"SOTA"})
                path.write_text(json.dumps([sota(age=5, frequency="14.0531")]))
                updated = poller.fetch()
                state.apply(updated, NOW + 20)
            self.assertEqual(first.spots[0].program, "SOTA")
            self.assertEqual(updated.errors, [])
            self.assertEqual(updated.spots[0].frequency, 14053.1)
            entry = next(iter(state.entries.values()))
            self.assertEqual(entry.created_at, NOW)
            self.assertEqual(entry.match.frequency, 14053.1)
            self.assertEqual(entry.last_heard, NOW - 42)


if __name__ == "__main__":
    unittest.main()
