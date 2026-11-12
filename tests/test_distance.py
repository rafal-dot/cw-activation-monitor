import contextlib
import io
import math
import os
import unittest
from unittest.mock import patch

from cw_monitor import cli
from cw_monitor import geo as geography
from cw_monitor import models
from cw_monitor import monitor as monitoring
from cw_monitor import polling
from cw_monitor import runtime
from cw_monitor.sources import programs
from cw_monitor.ui import layout as layout_ui
from cw_monitor.ui import table as table_ui
from cw_monitor.ui import views as views_ui
import time
from test_pota_alert import is_row, NOW, pota, rbn


class DistanceTests(unittest.TestCase):
    def test_qth_default_units_and_override(self):
        with patch.dict(os.environ):
            os.environ.pop("CW_ACTIVATION_QTH", None)
            args = cli.arguments([])
        self.assertEqual(args.distance_unit, "km")
        self.assertEqual(args.qth_label, "not set")
        self.assertIsNone(args.qth)
        explicit = cli.arguments(["--qth", "KO02KF"])
        self.assertAlmostEqual(explicit.qth[0], 52.2291666667)
        self.assertAlmostEqual(explicit.qth[1], 20.875)
        custom = cli.arguments(["--qth", "52.23,20.88", "--distance-unit", "mi"])
        self.assertEqual(custom.qth, (52.23, 20.88))
        self.assertEqual(custom.distance_unit, "mi")

    def test_environment_qth_and_explicit_override(self):
        with patch.dict(os.environ, {"CW_ACTIVATION_QTH": "JO91AA"}):
            self.assertEqual(cli.arguments([]).qth, geography.locator_coordinates("JO91AA"))
            self.assertEqual(cli.arguments(["--qth=52.23,20.88"]).qth, (52.23, 20.88))

    def test_missing_qth_warning_lasts_30_seconds_independent_of_fetch_and_wall_clock(self):
        with patch.dict(os.environ):
            os.environ.pop("CW_ACTIVATION_QTH", None)
            with patch.object(time, "monotonic", return_value=1000) as clock:
                state = monitoring.Monitor(cli.arguments([]))
                state.loading = True
                for elapsed in (0, 29.999, 30, 31):
                    clock.return_value = 1000 + elapsed
                    with self.subTest(elapsed=elapsed):
                        for now in (NOW, NOW - 3600, NOW + 3600):
                            frame = views_ui.screen_frame(state, now, 24, 100)
                            warning = [(text, style) for text, style in frame if "QTH not set" in text]
                            self.assertEqual(bool(warning), elapsed < 30)
                            if warning:
                                self.assertEqual(warning[0][1], "error")
                                self.assertIn("distance or azimuth", warning[0][0])
                                self.assertIn("--qth", warning[0][0])
                                self.assertLess(frame.index(warning[0]),
                                                next(i for i, (text, _) in enumerate(frame)
                                                     if text.startswith("ACTIVE")))
                state.apply(models.Snapshot([], [], ["Source warning"], NOW), NOW)
                self.assertEqual(views_ui.reports(state), ["WARNING: Source warning"])

    def test_explicit_or_environment_qth_suppresses_startup_warning(self):
        for extra, environment in ((["--qth", "0,0"], {}), ([], {"CW_ACTIVATION_QTH": "JO91AA"})):
            with self.subTest(extra=extra), patch.dict(os.environ, environment), \
                 patch.object(time, "monotonic", return_value=1000):
                state = monitoring.Monitor(cli.arguments(extra))
                self.assertEqual(views_ui.reports(state), [])

    def test_plain_warning_precedes_first_fetch_without_waiting(self):
        with patch.dict(os.environ):
            os.environ.pop("CW_ACTIVATION_QTH", None)
            args = cli.arguments(["--once"])
        output = io.StringIO()

        def fetch():
            self.assertIn("QTH not set", output.getvalue())
            return models.Snapshot([], [], [], NOW)

        with contextlib.redirect_stdout(output), patch.object(polling.Poller, "fetch", side_effect=fetch), \
             patch.object(time, "sleep", side_effect=AssertionError("startup must not block")):
            self.assertEqual(runtime.run_plain(args), 0)
        self.assertEqual(output.getvalue().count("QTH not set"), 1)

    def test_startup_warning_keeps_latest_entry_visible_on_short_screen(self):
        with patch.dict(os.environ):
            os.environ.pop("CW_ACTIVATION_QTH", None)
            state = monitoring.Monitor(cli.arguments([]))
        selected, _ = programs.select_pota([pota()], NOW, 300)
        state.apply(models.Snapshot(selected, [rbn()], [], NOW), NOW)
        for height, width in ((8, 80), (9, 80)):
            frame = views_ui.screen_frame(state, NOW, height, width)
            self.assertLessEqual(len(frame), height)
            self.assertTrue(any("QTH not set" in text for text, _ in frame))
            self.assertTrue(any(is_row(text, "IU1PZC") for text, _ in frame))

    def test_locator_case_precision_and_cell_centres(self):
        for locator, expected in (("JJ", (5, 10)), ("JJ00", (0.5, 1)),
                                  ("jj00aa", (1 / 48, 1 / 24)),
                                  ("JJ00AA00", (1 / 480, 1 / 240))):
            with self.subTest(locator=locator):
                actual = geography.locator_coordinates(locator)
                self.assertAlmostEqual(actual[0], expected[0])
                self.assertAlmostEqual(actual[1], expected[1])

    def test_invalid_qth_and_unit_are_rejected(self):
        for value in ("", "KO0", "KO02YY", "SS00", "KO02KF1", "91,20",
                      "52,181", "nan,20", "52,inf", "52,20,1", "52,a"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.arguments(["--qth", value])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.arguments(["--distance-unit", "miles"])

    def test_great_circle_distance_and_statute_miles(self):
        # A quarter of the equator is about 10,008 km, or 6,218 statute miles.
        self.assertEqual(geography.distance_text((0, 0), (0, 90), "km"), "10008 km")
        self.assertEqual(geography.distance_text((0, 0), (0, 90), "mi"), "6218 mi")
        self.assertEqual(geography.distance_text((52, 20), (52, 20), "km"), "0 km")
        self.assertEqual(geography.distance_text((0, 179), (0, -179), "km"), "222 km")
        self.assertEqual(geography.distance_text((0, 0), (0, 180), "km"), "20015 km")
        self.assertEqual(geography.distance_text(None, (0, 0), "km"), "—")
        self.assertEqual(geography.distance_text((0, 0), None, "km"), "—")

    def test_optional_bad_coordinates_do_not_discard_valid_pota_spots(self):
        for lat, lon in ((None, None), ("", ""), (math.nan, 20),
                         (90.1, 20), (52, -181), (False, True)):
            with self.subTest(latitude=lat, longitude=lon):
                selected, skipped = programs.select_pota([pota(latitude=lat, longitude=lon)], NOW, 300)
                self.assertEqual(skipped, 0)
                self.assertEqual(len(selected), 1)
                self.assertIsNone(selected[0].location)

    def test_pota_coordinates_precede_grid_and_accept_zero(self):
        selected, _ = programs.select_pota([pota(latitude="0", longitude="0", grid6="KO02KF")], NOW, 300)
        self.assertEqual(selected[0].location, (0, 0))
        selected, _ = programs.select_pota([pota(latitude=None, longitude=None, grid6="KO02KF")], NOW, 300)
        self.assertEqual(selected[0].location, geography.locator_coordinates("KO02KF"))
        selected, _ = programs.select_pota([pota(grid6="invalid", grid4="KO02")], NOW, 300)
        self.assertEqual(selected[0].location, geography.locator_coordinates("KO02"))

    def make_state(self, unit="km"):
        args = cli.arguments(["--qth", "0,0", "--distance-unit", unit])
        state = monitoring.Monitor(args)
        selected, _ = programs.select_pota([pota(latitude=0, longitude=90)], NOW, 300)
        state.apply(models.Snapshot(selected, [rbn()], [], NOW), NOW)
        return state

    def test_distance_and_azimuth_precede_comment_and_narrow_details_are_complete(self):
        for unit, expected in (("km", "10008 km"), ("mi", "6218 mi")):
            state = self.make_state(unit)
            for width in (40, 80, 120, 240):
                with self.subTest(unit=unit, width=width):
                    lines = [text for text, _ in views_ui.screen_frame(state, NOW, 30, width)]
                    header = next(text for text in lines if "Spot kHz" in text)
                    if "Distance" in header:
                        self.assertLess(header.index("Distance"), header.index("Comment"))
                        row = next(text for text in lines if is_row(text, "IU1PZC"))
                        self.assertIn(expected, row)
                    else:
                        self.assertTrue(any("Distance: " + expected in text for text in lines))
                    if "Azimuth" in header:
                        self.assertEqual(header.replace("│", " ").split()[-3:], ["Distance", "Azimuth", "Comment"])
                        self.assertIn("090°", next(text for text in lines if is_row(text, "IU1PZC")))
                    else:
                        self.assertTrue(any("Azimuth: 090°" in text for text in lines))
                    for text in lines[3:-4]:
                        self.assertLess(layout_ui.cell_width(text), width)
            snapshot = views_ui.render(state, NOW)
            self.assertIn(expected, snapshot)
            self.assertIn("QTH 0,0", snapshot)
            header = next(text for text in snapshot.splitlines() if text.startswith("Program"))
            self.assertEqual([c.strip() for c in header.split("|")][-3:], ["Distance", "Azimuth", "Comment"])

    def test_location_updates_in_place_and_survives_history_and_missing_metadata(self):
        state = self.make_state()
        original_entry = next(iter(state.entries.values()))
        for when, metadata, expected in ((NOW + 310, {"latitude": 0, "longitude": 45}, "5004 km"),
                                         (NOW + 370, {}, "5004 km")):
            selected, _ = programs.select_pota([pota(age=0, **metadata)], NOW, 300)
            spot = selected[0]
            selected = [models.ActivationSpot(spot.call, spot.reference, spot.frequency, when, spot.name, spot.location)]
            self.assertEqual(state.apply(models.Snapshot(selected, [], [], when), when), [])
            self.assertIs(next(iter(state.entries.values())), original_entry)
            self.assertEqual(len(state.rows(when)[1]), 1)
            self.assertIn(expected, views_ui.render(state, when))
        state.rows(NOW + 700)
        self.assertIn("5004 km", views_ui.render(state, NOW + 700))

    def test_missing_location_displays_dash_without_hiding_entry(self):
        args = cli.arguments([])
        state = monitoring.Monitor(args)
        selected, _ = programs.select_pota([pota()], NOW, 300)
        state.apply(models.Snapshot(selected, [rbn()], [], NOW), NOW)
        entry, match = state.rows(NOW)[0][0]
        values = table_ui.row_values(entry, match, NOW, qth=args.qth)
        self.assertEqual(dict(zip(table_ui.HEADERS, values))["Distance"], "—")
        self.assertEqual(dict(zip(table_ui.HEADERS, values))["Azimuth"], "—")

    def test_azimuth_cardinal_directions_and_true_north_rounding(self):
        for destination, expected in (((10, 0), "000°"), ((0, 10), "090°"),
                                      ((-10, 0), "180°"), ((0, -10), "270°"),
                                      ((10, -0.001), "000°")):
            with self.subTest(destination=destination):
                self.assertEqual(geography.azimuth_text((0, 0), destination), expected)

    def test_azimuth_is_initial_great_circle_bearing_including_date_line(self):
        self.assertEqual(geography.azimuth_text((0, 0), (45, 90)), "045°")
        self.assertEqual(geography.azimuth_text((45, 90), (0, 0)), "270°")
        self.assertEqual(geography.azimuth_text((0, 179), (0, -179)), "090°")
        self.assertEqual(geography.azimuth_text((0, -179), (0, 179)), "270°")

    def test_missing_and_undefined_azimuths_use_dash(self):
        for origin, destination in ((None, (0, 0)), ((0, 0), None), ((52, 20), (52, 20)),
                                    ((0, 0), (0, 180)), ((90, 0), (80, 0))):
            with self.subTest(origin=origin, destination=destination):
                self.assertEqual(geography.azimuth_text(origin, destination), "—")

    def test_qth_change_updates_azimuth_and_unit_change_does_not(self):
        state = self.make_state()
        before = views_ui.render(state, NOW)
        self.assertIn("090°", before)
        state.args.distance_unit = "mi"
        self.assertIn("090°", views_ui.render(state, NOW))
        state.args.qth = (0, 100)
        self.assertIn("270°", views_ui.render(state, NOW))


if __name__ == "__main__":
    unittest.main()
