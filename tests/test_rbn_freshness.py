from dataclasses import replace
import unittest

from cw_monitor import cli, models
from cw_monitor.monitor import Monitor
from cw_monitor.ui import colors, views
from test_pota_alert import NOW, is_row


class RbnFreshnessTests(unittest.TestCase):
    def state(self):
        state = Monitor(cli.arguments(["--qth", "KO02KF", "--color", "never"]))
        old_time = NOW - 19 * 60
        spot = models.ActivationSpot("F5JDG/M", "FR-0001", 14044.0, old_time)
        heard = models.RbnSpot(spot.call, "SP5GQ", spot.frequency, old_time, 18, 20)
        state.apply(models.Snapshot([spot], [heard], [], old_time), old_time)
        self.assertEqual(state.apply(models.Snapshot([replace(spot, time=NOW)], [], [], NOW), NOW), [])
        state.acknowledge(NOW)
        return state, spot, heard

    def test_outdated_report_keeps_source_activity_and_uses_configured_threshold(self):
        state, spot, heard = self.state()
        active, history = state.rows(NOW)
        self.assertEqual(len(active), 1)
        self.assertEqual(history, [])
        match = active[0][1]
        self.assertTrue(models.rbn_confirmation_outdated(match, NOW, state.args.rbn_max_age))
        self.assertFalse(models.rbn_confirmation_outdated(match, NOW, 19 * 60))
        self.assertTrue(models.rbn_confirmation_outdated(match, NOW + .001, 19 * 60))
        self.assertFalse(models.rbn_confirmation_outdated(replace(match, receptions=[]), NOW, 300))
        self.assertFalse(models.rbn_confirmation_outdated(replace(match, receptions=[replace(heard, time=None)]), NOW, 300))
        state.apply(models.Snapshot([replace(spot, time=NOW)], [replace(heard, receiver="SQ5J", time=NOW - 2)], [], NOW), NOW)
        self.assertFalse(models.rbn_confirmation_outdated(state.rows(NOW)[0][0][1], NOW, 300))

    def test_wide_and_narrow_frames_colour_only_latest_rbn_clock_and_keep_notice_without_colour(self):
        state, _, _ = self.state()
        for width in (80, 240):
            frame = views.screen_frame(state, NOW, 40, width)
            self.assertTrue(any("RBN confirmation outdated" in text for text, _ in frame))
            header = next(text for text, _ in frame if "Activator" in text)
            if "Last RBN" in header:
                text = next(text for text, _ in frame if is_row(text, "F5JDG/M"))
            else:
                text = next(text for text, _ in frame if "Last RBN:" in text)
            self.assertIn(("00:19:00", "yellow"), [(text[a:b], role) for a, b, role in text.color_spans])
            self.assertTrue(all("\x1b" not in text for text, _ in frame))
        plain = views.render(state, NOW)
        self.assertIn("RBN confirmation outdated", plain)
        self.assertNotIn("\x1b", plain)
        state.args.color = "always"
        self.assertIn("\x1b[33m00:19:00", views.render(state, NOW, color=True))
        state.args.rbn_max_age = 1200
        self.assertNotIn("RBN confirmation outdated", views.render(state, NOW))
        frame = views.screen_frame(state, NOW, 40, 240)
        text = next(text for text, _ in frame if is_row(text, "F5JDG/M"))
        self.assertNotIn(("00:19:00", "yellow"), [(text[a:b], role) for a, b, role in text.color_spans])

    def test_new_session_can_show_historical_confirmation_silently(self):
        state = Monitor(cli.arguments(["--qth", "KO02KF"]))
        spot = models.ActivationSpot("F5JDG/M", "FR-0001", 14044.0, NOW)
        old = models.RbnSpot(spot.call, "SP5GQ", spot.frequency, NOW - 19 * 60, 18, 20)
        self.assertEqual(state.apply(models.Snapshot([spot], [old], [], NOW), NOW), [])
        active, history = state.rows(NOW)
        self.assertEqual(len(active), 1)
        self.assertEqual(history, [])
        self.assertEqual(active[0][0].new_until, 0)
        self.assertTrue(models.rbn_confirmation_outdated(active[0][1], NOW, 300))
        self.assertEqual(active[0][0].last_heard, old.time)

    def test_age_spans_do_not_colour_first_program_or_per_spotter_ages(self):
        text = "First seen: 00:20:00 | Last RBN: 00:19:00 | Last spot: 00:00:10"
        self.assertEqual([(text[a:b], role) for a, b, role in colors.rbn_age_spans(text, True)], [("00:19:00", "yellow")])
        self.assertEqual(colors.rbn_age_spans(text, False), [])
        for field in (7, 12, 13):
            self.assertEqual(colors.value_spans(field, "00:19:00", (10, 20), (15, 24), True), [])
