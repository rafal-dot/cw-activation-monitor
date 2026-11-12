import contextlib
import io
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from cw_monitor import cli
from cw_monitor import models
from cw_monitor import monitor as monitoring
from cw_monitor import polling
from cw_monitor import runtime
from cw_monitor import values as values_api
from cw_monitor.sources import http
from cw_monitor.ui import layout as layout_ui
from cw_monitor.ui import table as table_ui
from cw_monitor.ui import views as views_ui
from queue import Queue
import time
from test_pota_alert import is_row, NOW, pota, rbn


def snapshot(now=NOW, calls=("IU1PZC",), pota_age=30, rbn_age=10):
    return models.Snapshot([models.ActivationSpot(c, "IT-0759", 14052.9, now - pota_age) for c in calls],
                        [models.RbnSpot(c, "SP5GQ", 14052.9, now - rbn_age, 18, 23) for c in calls],
                        [], now)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.args = cli.arguments(["--qth", "JO91AA"])
        self.state = monitoring.Monitor(self.args)
        self.layout = layout_ui.ColumnLayout()

    def test_default_limits_and_validation(self):
        self.assertEqual(self.args.pota_max_age, 300)
        self.assertEqual(self.args.history_max_age, 1800)
        self.assertEqual(cli.arguments(["--history-max-age", "900"]).history_max_age, 900)
        for value in ("0", "-1", "nan", "inf"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.arguments(["--history-max-age", value])

    def test_band_colour_metadata_tracks_layout_without_colouring_headers_or_program(self):
        self.state.apply(snapshot(), NOW)
        for width in (40, 80, 120, 240):
            frame = views_ui.screen_frame(self.state, NOW, 30, width, layout=self.layout)
            row = next(text for text, _ in frame if is_row(text, "IU1PZC"))
            self.assertEqual(len(row.band_spans), 1)
            begin, end, band = row.band_spans[0]
            self.assertEqual(row[begin:end], "20m")
            self.assertEqual(band, "20m")
            header = next(text for text, _ in frame if "Spot kHz" in text)
            self.assertEqual(header.band_spans, ())

    def test_pota_expiration_moves_to_history_without_network(self):
        self.state.apply(snapshot(pota_age=290), NOW)
        self.assertEqual([len(r) for r in self.state.rows(NOW + 10)], [1, 0])
        self.assertEqual([len(r) for r in self.state.rows(NOW + 11)], [0, 1])

    def test_history_retention_starts_at_pota_expiration(self):
        data = snapshot(rbn_age=100)
        self.state.apply(data, NOW)
        self.state.apply(data, NOW + 60)
        self.assertEqual([len(r) for r in self.state.rows(NOW + 2070)], [0, 1])
        self.assertEqual([len(r) for r in self.state.rows(NOW + 2071)], [0, 0])

    def test_missing_spot_stays_active_until_deadline_and_return_does_not_repeat_alert(self):
        data = snapshot()
        self.state.apply(data, NOW)
        self.state.apply(models.Snapshot([], [], [], NOW + 60), NOW + 60)
        self.assertEqual([len(r) for r in self.state.rows(NOW + 60)], [1, 0])
        self.assertEqual(self.state.apply(data, NOW + 120), [])
        active, history = self.state.rows(NOW + 120)
        self.assertEqual(history, [])
        self.assertEqual(table_ui.row_values(*active[0], NOW + 120)[10], "ACTIVE")

    def test_snr_update_does_not_reorder_existing_rows_or_repeat_alert(self):
        self.state.apply(snapshot(calls=("FIRST",)), NOW)
        self.state.apply(snapshot(NOW + 10, calls=("FIRST", "SECOND")), NOW + 10)
        new = self.state.apply(snapshot(NOW + 20, calls=("FIRST", "SECOND"), rbn_age=0), NOW + 20)
        active, _ = self.state.rows(NOW + 20)
        self.assertEqual([m.call for _, m in active], ["SECOND", "FIRST"])
        self.assertEqual(new, [])

    def test_confirmed_receivers_remain_visible_while_pota_is_active(self):
        data = snapshot(rbn_age=10)
        data.rbn.append(models.RbnSpot("IU1PZC", "SQ5J", 14053, NOW - 290, 10, 23))
        self.state.apply(data, NOW)
        active, _ = self.state.rows(NOW + 11)
        self.assertEqual([r.receiver for r in active[0][1].receptions], ["SP5GQ", "SQ5J"])

    def test_failure_retains_active_entries_until_pota_expires(self):
        self.state.apply(snapshot(), NOW)
        self.state.fail("HTTP 503")
        self.assertEqual([len(r) for r in self.state.rows(NOW + 1)], [1, 0])
        self.assertEqual([len(r) for r in self.state.rows(NOW + 271)], [0, 1])
        self.assertEqual(self.state.apply(snapshot(), NOW + 2), [])

    def test_rbn_expiration_does_not_move_active_entry_to_history(self):
        data = snapshot(rbn_age=299)
        self.state.apply(data, NOW)
        self.assertEqual([len(r) for r in self.state.rows(NOW + 2)], [1, 0])
        self.assertEqual(self.state.apply(data, NOW + 60), [])
        active, history = self.state.rows(NOW + 60)
        self.assertEqual(len(active), 1)
        self.assertEqual(history, [])
        self.assertEqual(active[0][1].receptions[0].time, NOW - 299)
        self.assertEqual([len(r) for r in self.state.rows(NOW + 271)], [0, 1])

    def test_renewed_pota_preserves_confirmation_without_new_rbn_hit(self):
        self.state.apply(snapshot(rbn_age=299), NOW)
        renewed = snapshot(NOW + 240, pota_age=0)
        renewed.rbn = []
        self.assertEqual(self.state.apply(renewed, NOW + 240), [])
        self.assertEqual([len(r) for r in self.state.rows(NOW + 301)], [1, 0])
        active = self.state.rows(NOW + 301)[0]
        self.assertEqual(active[0][1].receptions[0].time, NOW - 299)
        self.assertEqual(table_ui.row_values(*active[0], NOW + 301, history=True)[10], "ACTIVE")
        self.assertEqual([len(r) for r in self.state.rows(NOW + 541)], [0, 1])

    def test_first_confirmation_still_requires_fresh_rbn(self):
        self.assertEqual(self.state.apply(snapshot(rbn_age=301), NOW), [])
        self.assertEqual(self.state.rows(NOW), ([], []))

    def test_park_change_on_same_band_creates_separate_entry(self):
        self.state.apply(snapshot(), NOW)
        changed = models.Snapshot([models.ActivationSpot("IU1PZC", "IT-0760", 14052.9, NOW + 10)], [rbn()], [], NOW + 10)
        self.state.apply(changed, NOW + 10)
        self.assertEqual([len(r) for r in self.state.rows(NOW + 10)], [2, 0])
        self.assertEqual(len(self.state.entries), 2)
        self.assertEqual(self.state.rows(NOW + 10)[0][0][1].references, ("IT-0760",))

    def test_frequency_updates_existing_entry_without_history_or_alert(self):
        self.state.apply(snapshot(), NOW)
        self.state.acknowledge(NOW + 1)
        first_rank = self.state.rows(NOW)[0][0][0].rank
        for i, freq in enumerate((14053.0, 14052.8, 14060.0), start=1):
            when = NOW + i * 10
            changed = models.Snapshot([models.ActivationSpot("IU1PZC", "IT-0759", freq, when)], [], [], when)
            self.assertEqual(self.state.apply(changed, when), [])
            active, history = self.state.rows(when)
            self.assertEqual(len(active), 1)
            self.assertEqual(history, [])
            entry, match = active[0]
            self.assertEqual(entry.rank, first_rank)
            self.assertEqual(match.frequency, freq)
            self.assertEqual(table_ui.row_values(entry, match, when)[9], "40m" if freq == 7028 else "20m")
            self.assertEqual(match.receptions[0].frequency, 14052.9)
            self.assertEqual(match.receptions[0].time, NOW - 10)
            self.assertEqual(views_ui.new_banner(active, when), "NEW: —")
            self.assertEqual(table_ui.row_values(entry, match, when)[0], "UPD")

    def test_latest_spot_timestamp_wins_over_order_and_later_stale_response(self):
        newer = models.ActivationSpot("IU1PZC", "IT-0759", 14053.0, NOW - 10)
        older = models.ActivationSpot("IU1PZC", "IT-0759", 14052.9, NOW - 30)
        data = models.Snapshot([newer, older], [rbn()], [], NOW)
        self.state.apply(data, NOW)
        self.assertEqual(self.state.rows(NOW)[0][0][1].frequency, 14053.0)
        self.state.apply(models.Snapshot([older], [], [], NOW + 1), NOW + 1)
        self.assertEqual(self.state.rows(NOW + 1)[0][0][1].frequency, 14053.0)

    def test_pota_and_latest_rbn_frequency_have_distinct_columns(self):
        data = snapshot()
        data.rbn.append(models.RbnSpot("IU1PZC", "SQ5J", 14053.1, NOW - 2, 9, 23))
        self.state.apply(data, NOW)
        entry, match = self.state.rows(NOW)[0][0]
        values = table_ui.row_values(entry, match, NOW)
        self.assertEqual(values[3], "14052.9")
        self.assertEqual(values[8], "14053.1")
        for width in (40, 58, 74, 80, 120):
            lines = [text for text, _ in views_ui.screen_frame(self.state, NOW, 24, width, layout=self.layout)]
            heading = next(text for text in lines if "Spot kHz" in text)
            self.assertIn("RBN kHz", heading)
            row = next(text for text in lines if "IU1PZC" in text and "14052.9" in text and not text.startswith(">>> NEW"))
            self.assertIn("14053.1", row)
            self.assertLess(len(row), width)
        rendered = views_ui.render(self.state, NOW)
        self.assertIn("Spot kHz", rendered)
        self.assertIn("RBN kHz", rendered)
        self.assertIn("RBN 14053.1 kHz", views_ui.new_banner(self.state.rows(NOW)[0], NOW))

    def test_band_is_shown_next_to_pota_frequency_in_all_layouts(self):
        self.state.apply(snapshot(), NOW)
        for width in (40, 58, 74, 80, 120):
            lines = [text for text, _ in views_ui.screen_frame(self.state, NOW, 24, width, layout=self.layout)]
            header = next(text for text in lines if "Spot kHz" in text)
            self.assertLess(header.index("Band"), header.index("Spot kHz"))
            self.assertLess(header.index("Spot kHz"), header.index("RBN kHz"))
            row = next(text for text in lines if "IU1PZC" in text and "14052.9" in text and not text.startswith(">>> NEW"))
            self.assertIn("20m", row)
            self.assertLess(len(row), width)
        self.assertIn("Band", views_ui.render(self.state, NOW))

    def test_band_ranges_and_unknown_frequencies(self):
        for freq, expected in ((1800, "160m"), (3500, "80m"), (4000, "80m"), (5357, "60m"),
                               (7028, "40m"), (10118, "30m"), (14052.9, "20m"), (18078, "17m"),
                               (21060, "15m"), (24906, "12m"), (28060, "10m"), (50100, "6m"),
                               (144050, "2m"), (432050, "70cm"), (1000, "—"), (15000, "—")):
            with self.subTest(freq=freq):
                self.assertEqual(values_api.band_for(freq), expected)

    def test_reference_names_are_shortened_and_preserved_in_history(self):
        data = snapshot()
        data.spots[0] = models.ActivationSpot("IU1PZC", "IT-0759", 14052.9, NOW - 30,
                                    "Parco Naturale del Monte Tre Denti - Freidour")
        self.state.apply(data, NOW)
        entry, match = self.state.rows(NOW)[0][0]
        self.assertEqual(table_ui.reference_label(match, 12), "IT-0759 (Parco Natur…)")
        self.assertEqual(table_ui.reference_label(match, 0), "IT-0759")
        self.assertIn("IT-0759 (", views_ui.render(self.state, NOW))
        self.state.apply(models.Snapshot([], [], [], NOW + 10), NOW + 10)
        self.assertEqual(table_ui.reference_label(self.state.rows(NOW + 10)[0][0][1], 12),
                         "IT-0759 (Parco Natur…)")
        self.assertEqual(table_ui.reference_label(self.state.rows(NOW + 271)[1][0][1], 12),
                         "IT-0759 (Parco Natur…)")

    def test_narrow_terminal_shows_park_name_under_row_and_prioritizes_newest(self):
        data = snapshot(calls=("OLDER",))
        data.spots[0] = models.ActivationSpot("OLDER", "IT-0759", 14052.9, NOW - 30, "Parco Naturale")
        self.state.apply(data, NOW)
        data.spots.append(models.ActivationSpot("NEWEST", "IT-0759", 14052.9, NOW, "Parco Naturale"))
        data.rbn.append(models.RbnSpot("NEWEST", "SP5GQ", 14052.9, NOW, 18, 23))
        self.state.apply(data, NOW)
        frame = views_ui.screen_frame(self.state, NOW, 9, 80, layout=self.layout)
        self.assertIn("NEWEST", frame[4][0])
        self.assertIn("IT-0759 (Parco Naturale)", frame[5][0])
        self.assertIn("Hidden A/H: 1/0", frame[-1][0])

    def test_park_name_length_option_accepts_zero_and_rejects_negative(self):
        self.assertIsNone(self.args.park_name_length)
        self.assertEqual(cli.arguments(["--park-name-length", "0"]).park_name_length, 0)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.arguments(["--park-name-length", "-1"])

    def test_banner_and_flash_have_limited_duration(self):
        self.state.apply(snapshot(), NOW)
        active = self.state.rows(NOW)[0]
        self.assertIn("IU1PZC", views_ui.new_banner(active, NOW + 59))
        self.assertEqual(views_ui.new_banner(active, NOW + 60), "NEW: —")
        frames = [views_ui.screen_frame(self.state, NOW + t, 24, 120, layout=self.layout) for t in (29, 30)]
        self.assertTrue(any(style == "new" for _, style in frames[0]))
        self.assertFalse(any(style == "new" for _, style in frames[1]))
        active = self.state.rows(NOW + 30)[0]
        self.assertEqual(table_ui.row_values(*active[0], NOW + 30)[0], ">>>")
        self.assertEqual(table_ui.row_values(*active[0], NOW + 60)[0], "")

    def test_custom_highlight_duration_and_validation(self):
        self.assertEqual(self.args.highlight_seconds, 30)
        args = cli.arguments(["--highlight-seconds", "10"])
        state = monitoring.Monitor(args)
        state.apply(snapshot(), NOW)
        for seconds, highlighted in ((9, True), (10, False)):
            frame = views_ui.screen_frame(state, NOW + seconds, 24, 120)
            self.assertEqual(any(style == "new" for _, style in frame), highlighted)
        for value in ("0", "-1", "nan", "inf"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.arguments(["--highlight-seconds", value])

    def test_acknowledgement_removes_highlights_but_preserves_data_and_future_alerts(self):
        data = snapshot(calls=("FIRST",))
        self.state.apply(data, NOW)
        before = self.state.rows(NOW + 1)
        self.state.acknowledge(NOW + 1)
        after = self.state.rows(NOW + 1)
        self.assertEqual(before, after)
        self.assertEqual(views_ui.new_banner(after[0], NOW + 1), "NEW: —")
        self.assertEqual(table_ui.row_values(*after[0][0], NOW + 1)[0], "")
        self.assertFalse(any(style in ("new", "banner") for _, style in
                             views_ui.screen_frame(self.state, NOW + 1, 24, 120, layout=self.layout)))
        self.assertEqual(self.state.apply(data, NOW + 2), [])
        self.assertEqual(views_ui.new_banner(self.state.rows(NOW + 2)[0], NOW + 2), "NEW: —")
        self.state.apply(snapshot(NOW + 3, calls=("FIRST", "SECOND")), NOW + 3)
        banner = views_ui.new_banner(self.state.rows(NOW + 3)[0], NOW + 3)
        self.assertIn("SECOND", banner)
        self.assertNotIn("FIRST", banner)

    def test_banner_includes_count_and_receiver_snr(self):
        p, r = runtime.demo_data(NOW, self.args.spotters)
        self.state.apply(models.Snapshot(p, r, [], NOW), NOW)
        banner = views_ui.new_banner(self.state.rows(NOW)[0], NOW)
        self.assertTrue(banner.startswith(">>> NEW ACTIVATIONS (2):"))
        self.assertIn("SQ5J 9 dB", banner)
        self.assertIn("14054.0 kHz", banner)
        self.assertTrue(banner.endswith("<<<"))

    def test_overflow_shows_newest_active_before_history(self):
        self.state.apply(snapshot(NOW - 600, calls=("HISTORY",)), NOW - 600)
        self.state.apply(snapshot(calls=("OLDER",)), NOW)
        self.state.apply(snapshot(NOW + 1, calls=("OLDER", "NEWEST")), NOW + 1)
        frame = views_ui.screen_frame(self.state, NOW + 1, 8, 80, layout=self.layout)
        self.assertEqual(len(frame), 8)
        table_text = "\n".join(text for text, _ in frame[2:5])
        self.assertIn("NEWEST", table_text)
        self.assertNotIn("OLDER", table_text)
        self.assertNotIn("HISTORY", table_text)
        self.assertIn("Hidden A/H: 1/1", frame[-1][0])
        self.assertIn("Space", frame[-1][0])

    def test_reports_above_table_and_details_below(self):
        self.state.apply(snapshot(), NOW)
        self.state.warnings = ["Przykładowe ostrzeżenie"]
        for lines in ([text for text, _ in views_ui.screen_frame(self.state, NOW, 24, 100, layout=self.layout)],
                      views_ui.render(self.state, NOW).splitlines()):
            warning = next(i for i, text in enumerate(lines) if "WARNING" in text)
            table = next(i for i, text in enumerate(lines) if text.startswith("ACTIVE"))
            settings = next(i for i, text in enumerate(lines) if "history ≤" in text)
            self.assertLess(warning, table)
            self.assertGreater(settings, table)

    def test_small_and_resized_frames_do_not_exceed_height(self):
        self.state.apply(snapshot(), NOW)
        for height, width in ((1, 20), (6, 30), (8, 40), (10, 58), (24, 80), (40, 120)):
            frame = views_ui.screen_frame(self.state, NOW, height, width, layout=self.layout)
            self.assertLessEqual(len(frame), height)

    def test_80_columns_preserve_both_receivers_measurements(self):
        p, r = runtime.demo_data(NOW, self.args.spotters)
        self.state.apply(models.Snapshot(p, r, [], NOW), NOW)
        rows = views_ui.screen_frame(self.state, NOW, 24, 80, layout=self.layout)
        text = next(text for text, _ in rows if is_row(text, "YO5BDL"))
        self.assertIn("SP5GQ/SQ5J", text)
        self.assertTrue(any("SNR dB: 12/9" in line or "12/9" in line for line, _ in rows))
        item = next(item for item in self.state.rows(NOW)[0] if item[1].call == "YO5BDL")
        values = dict(zip(table_ui.HEADERS, table_ui.row_values(*item, NOW)))
        self.assertEqual(values["WPM"], "21")
        self.assertNotIn("21/21", "\n".join(line for line, _ in rows))
        self.assertIn("00:00:35/00:00:58", text)
        self.assertLess(len(text), 80)

    def test_equal_measurements_collapse_independently_and_mixed_values_keep_order(self):
        self.state.apply(snapshot(), NOW)
        entry, match = self.state.rows(NOW)[0][0]
        cases = [
            ([(18, 23, 10), (9, 23, 20), (18, 23, 10)],
             ["18 / 9 / 18", "23", "00:00:10 / 00:00:20 / 00:00:10"]),
            ([(18, 23, 10), (18, 23, 10), (18, 25, 10)],
             ["18", "23 / 23 / 25", "00:00:10"]),
            ([(0, 0, None)] * 5, ["0", "0", "—"]),
            ([(18, 23, None), (18, 23, 10)], ["18", "23", "— / 00:00:10"]),
            ([(18, 23, 10)], ["18", "23", "00:00:10"]),
            ([], ["—", "—", "—"]),
        ]
        for readings, expected in cases:
            with self.subTest(readings=readings):
                receptions = [models.RbnSpot(match.call, f"N{i}TEST", match.frequency,
                              NOW - age if age is not None else None, snr, wpm)
                              for i, (snr, wpm, age) in enumerate(readings)]
                row = table_ui.row_values(entry, replace(match, receptions=receptions), NOW)
                self.assertEqual(row[5:8], expected)
                self.assertEqual(row[4], " / ".join(r.receiver for r in receptions) or "—")

    def test_age_format_is_hours_minutes_and_seconds_without_day_wrap(self):
        for seconds, expected in ((-1, "00:00:00"), (0, "00:00:00"), (59.9, "00:00:59"),
                                  (60, "00:01:00"), (754, "00:12:34"), (3599, "00:59:59"),
                                  (3600, "01:00:00"), (6000, "01:40:00"), (86400, "24:00:00"),
                                  (360000, "100:00:00")):
            with self.subTest(seconds=seconds):
                self.assertEqual(values_api.age_text(seconds), expected)

    def test_three_ages_have_independent_clocks_and_identical_fetch_does_not_reset(self):
        data = snapshot(pota_age=30, rbn_age=10)
        self.state.apply(data, NOW)
        self.state.apply(data, NOW + 60)
        row = table_ui.row_values(*self.state.rows(NOW + 75)[0][0], NOW + 75)
        values = dict(zip(table_ui.HEADERS, row))
        self.assertEqual(values["First seen"], "00:01:15")
        self.assertEqual(values["Last spot"], "00:01:45")
        self.assertEqual(values["RBN age"], "00:01:25")

    def test_old_active_pota_update_changes_only_latest_pota_age(self):
        self.state.apply(snapshot(), NOW)
        later = snapshot(NOW + 310, pota_age=5)
        later.spots[0] = models.ActivationSpot("IU1PZC", "IT-0759", 14053.1, NOW + 305)
        later.rbn = []
        self.state.apply(later, NOW + 310)
        self.assertEqual(self.state.rows(NOW + 320)[1], [])
        values = dict(zip(table_ui.HEADERS, table_ui.row_values(
            *self.state.rows(NOW + 320)[0][0], NOW + 320, history=True)))
        self.assertEqual(values["First seen"], "00:05:20")
        self.assertEqual(values["Last spot"], "00:00:15")
        self.assertEqual(values["RBN age"], "00:05:30")
        self.assertEqual(values["Spot kHz"], "14053.1")

    def test_return_after_hidden_history_keeps_first_detection_time(self):
        self.state.apply(snapshot(), NOW)
        self.assertEqual(self.state.rows(NOW + 6000), ([], []))
        self.state.apply(snapshot(NOW + 6010, pota_age=0), NOW + 6010)
        self.state.acknowledge(NOW + 6010)
        values = dict(zip(table_ui.HEADERS, table_ui.row_values(
            *self.state.rows(NOW + 6015)[0][0], NOW + 6015, history=True)))
        self.assertEqual(values["First seen"], "01:40:15")
        self.assertEqual(values["Last spot"], "00:00:05")

    def test_age_columns_in_wide_terminal_and_text_snapshot(self):
        self.state.apply(snapshot(), NOW)
        for lines in ([text for text, _ in views_ui.screen_frame(self.state, NOW + 75, 24, 200, layout=self.layout)],
                      views_ui.render(self.state, NOW + 75).splitlines()):
            header = next(text for text in lines if text.startswith("Program"))
            row = next(text for text in lines if is_row(text, "IU1PZC"))
            self.assertLess(header.index("First seen"), header.index("Last spot"))
            self.assertLess(header.index("Last spot"), header.index("RBN age"))
            self.assertLess(header.index("RBN age"), header.index("Comment"))
            for expected in ("00:01:15", "00:01:45", "00:01:25"):
                self.assertIn(expected, row)

    def test_missing_age_columns_are_shown_in_details_without_truncation(self):
        self.state.apply(snapshot(), NOW)
        for width in (40, 80, 120):
            lines = [text for text, _ in views_ui.screen_frame(self.state, NOW + 75, 24, width, layout=self.layout)]
            header = next(text for text in lines if "Spot kHz" in text)
            for label, expected in (("First seen", "00:01:15"), ("Last spot", "00:01:45")):
                if label not in header:
                    self.assertTrue(any(f"{label}: {expected}" in text for text in lines))
            for text in lines[4:-4]:
                self.assertLess(layout_ui.cell_width(text), width)

    def test_activator_width_depends_on_content_not_free_space(self):
        self.state.apply(snapshot(), NOW)
        widths = []
        for width in (80, 120, 180):
            lines = [text for text, _ in views_ui.screen_frame(self.state, NOW, 24, width, layout=self.layout)]
            header = next(text for text in lines if "Activator" in text)
            widths.append(header.index("Band") - header.index("Activator") - len(self.layout.separator))
        self.assertEqual(widths, [len("Activator")] * 3)

    def test_column_separators_header_rule_and_numeric_alignment(self):
        self.state.apply(snapshot(), NOW)
        for width in (80, 120, 240):
            with self.subTest(width=width):
                frame = views_ui.screen_frame(self.state, NOW, 24, width, layout=self.layout)
                header_index = next(i for i, (text, _) in enumerate(frame) if "Spot kHz" in text)
                header, style = frame[header_index]
                rule, rule_style = frame[header_index + 1]
                self.assertEqual(style, "table_header")
                self.assertEqual(rule_style, "separator")
                self.assertEqual(layout_ui.cell_width(header), layout_ui.cell_width(rule))
                self.assertIn("─", rule)
                self.assertEqual("│" in header, width >= 120)
                row = next(text for text, _ in frame if is_row(text, "IU1PZC"))
                # Read the actual layout: optional columns can move below the row.
                columns = self.layout.choose(
                    (24, width), [table_ui.row_values(*self.state.rows(NOW)[0][0], NOW)], 1, 7, 1800)
                offset = 0
                for field, size in columns:
                    if field in table_ui.NUMERIC_FIELDS:
                        self.assertEqual(row[offset:offset + size], row[offset:offset + size].strip().rjust(size))
                    offset += size + len(self.layout.separator)
                self.assertLess(layout_ui.cell_width(row), width)

    def test_updated_comment_wraps_at_its_column_with_separators(self):
        self.state.apply(snapshot(), NOW)
        updated = snapshot(NOW + 10)
        updated.spots[0] = models.ActivationSpot("IU1PZC", "IT-0759", 14053.1, NOW + 10)
        updated.rbn = []
        self.state.apply(updated, NOW + 10)
        frame = views_ui.screen_frame(self.state, NOW + 10, 24, 120, layout=self.layout)
        header = next(text for text, _ in frame if "Spot kHz" in text)
        comment_start = header.index("Comment")
        row_index = next(i for i, (text, _) in enumerate(frame) if is_row(text, "IU1PZC"))
        parts = [frame[row_index][0][comment_start:].strip()]
        for text, _ in frame[row_index + 1:]:
            if not text.startswith(" " * comment_start) or not text.strip():
                break
            parts.append(text[comment_start:].strip())
        self.assertEqual(" ".join(parts), "ACTIVE NEW UPD | Spot 14052.9 → 14053.1 kHz")

    def test_column_only_shrinks_after_resize(self):
        self.args.history_max_age = 60
        long_call = "HB9/SQ5ABC/P"
        self.state.apply(snapshot(calls=(long_call,)), NOW)

        def call_width(now, width=120, height=24):
            lines = [text for text, _ in views_ui.screen_frame(self.state, now, height, width, layout=self.layout)]
            header = next(text for text in lines if "Activator" in text)
            return header.index("Band") - header.index("Activator") - len(self.layout.separator)

        self.assertEqual(call_width(NOW), len(long_call) + 1)
        self.state.apply(snapshot(NOW + 400), NOW + 400)
        self.assertEqual(call_width(NOW + 400), len(long_call) + 1)
        self.assertEqual(call_width(NOW + 400, width=121), len("Activator"))

    def test_minute_boundary_does_not_move_columns(self):
        data = snapshot()
        data.rbn.append(models.RbnSpot("IU1PZC", "SQ5J", 14052.9, NOW - 20, 9, 23))
        self.state.apply(data, NOW)
        headers = []
        for now in (NOW, NOW + 60):
            lines = [text for text, _ in views_ui.screen_frame(self.state, now, 24, 80, layout=self.layout)]
            headers.append(next(text for text in lines if "RBN age" in text))
            row = next(text for text in lines if is_row(text, "IU1PZC"))
            self.assertIn("00:00:10/00:00:20" if now == NOW else "00:01:10/00:01:20", row)
        self.assertEqual(headers[0], headers[1])

    def test_three_receivers_keep_all_ages_in_a_narrow_layout(self):
        self.args.spotters.append("K9ABC")
        data = snapshot()
        data.rbn.extend([models.RbnSpot("IU1PZC", "SQ5J", 14052.9, NOW - 80, 9, 23),
                         models.RbnSpot("IU1PZC", "K9ABC", 14052.9, NOW - 225, 9, 23)])
        self.state.apply(data, NOW)
        lines = [text for text, _ in views_ui.screen_frame(self.state, NOW, 24, 80, layout=self.layout)]
        self.assertTrue(any("RBN age" in text for text in lines))
        row = next(text for text in lines if "IU1PZC" in text and "00:00:10/00:01:20/00:03:45" in text)
        self.assertLess(len(row), 80)
        self.assertNotIn("…", row)

    def test_hour_boundary_keeps_age_columns_stable(self):
        data = snapshot()
        data.rbn.append(models.RbnSpot("IU1PZC", "SQ5J", 14052.9, NOW - 20, 9, 23))
        self.state.apply(data, NOW)
        first = views_ui.screen_frame(self.state, NOW, 24, 80, layout=self.layout)
        first_header = next(text for text, _ in first if "RBN age" in text)
        renewed = snapshot(NOW + 3600, pota_age=0)
        renewed.rbn = []
        self.state.apply(renewed, NOW + 3600)
        later = views_ui.screen_frame(self.state, NOW + 3600, 24, 80, layout=self.layout)
        self.assertEqual(first_header, next(text for text, _ in later if "RBN age" in text))
        row = next(text for text, _ in later if is_row(text, "IU1PZC"))
        self.assertIn("01:00:10/01:00:20", row)

    def test_long_receiver_ages_wrap_below_narrow_rows_without_splitting_timestamps(self):
        data = snapshot()
        self.args.spotters += ["K9ABC", "K8ABC", "K7ABC"]
        for receiver, age in zip(self.args.spotters[1:], (20, 30, 40, 50)):
            data.rbn.append(models.RbnSpot("IU1PZC", receiver, 14052.9, NOW - age, 9, 23))
        self.state.apply(data, NOW)
        renewed = snapshot(NOW + 36000, pota_age=0)
        renewed.rbn = []
        self.state.apply(renewed, NOW + 36000)
        frame = views_ui.screen_frame(self.state, NOW + 36000, 50, 40, layout=self.layout)
        lines = [text for text, _ in frame]
        self.assertTrue(any("RBN age:" in text for text in lines))
        for seconds in (10, 20, 30, 40, 50):
            self.assertTrue(any(f"10:00:{seconds:02d}" in text for text in lines))
        for text in lines[3:-4]:
            self.assertLess(layout_ui.cell_width(text), 40)

    def test_very_small_window_places_complete_age_below_row(self):
        data = snapshot()
        data.rbn.append(models.RbnSpot("IU1PZC", "SQ5J", 14052.9, NOW - 20, 9, 23))
        self.state.apply(data, NOW)
        lines = [text for text, _ in views_ui.screen_frame(self.state, NOW, 24, 40, layout=self.layout)]
        self.assertTrue(any("RBN age: 00:00:10/00:00:20" in text for text in lines))
        self.assertTrue(any("Spot kHz" in text and "RBN kHz" in text for text in lines))

    def test_refresh_countdown_ticks_locally_and_does_not_go_negative(self):
        self.state.next_poll_at = 160
        for tick, expected in ((100, "01:00"), (100.2, "01:00"), (113, "00:47"),
                               (159.9, "00:01"), (160, "00:00"), (162, "00:00")):
            with self.subTest(tick=tick), patch.object(time, "monotonic", return_value=tick):
                frame = views_ui.screen_frame(self.state, NOW, 24, 80, layout=self.layout)
            self.assertTrue(any(f"Refresh in 00:{expected}" in text for text, _ in frame))

    def test_refresh_countdown_displays_loading_and_initial_wait(self):
        self.assertEqual(views_ui.refresh_status(self.state), "Refresh: waiting…")
        self.state.next_poll_at = 160
        self.state.loading = True
        self.assertEqual(views_ui.refresh_status(self.state), "Refreshing…")

    def test_active_and_history_share_columns_in_snapshots_and_live_frames(self):
        old = snapshot(calls=("LONGCALL/P",), pota_age=299)
        self.state.apply(old, NOW)
        self.state.apply(snapshot(NOW + 10, calls=("SHORT",)), NOW + 10)
        self.assertEqual([len(r) for r in self.state.rows(NOW + 10)], [1, 1])
        self.state.rbn_status = "RBN stream: complete"
        for output in (views_ui.render(self.state, NOW + 10).splitlines(),
                       [text for text, _ in views_ui.screen_frame(self.state, NOW + 10, 40, 240)]):
            rows = [text for text in output if is_row(text, "SHORT") or is_row(text, "LONGCALL/P")]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0].index("14052.9"), rows[1].index("14052.9"))
            self.assertEqual(sum(text.startswith("Program") for text in output), 1)
            refresh = next(i for i, text in enumerate(output) if text.startswith("Refresh:"))
            self.assertEqual(output[refresh + 1], "RBN stream: complete")
            self.assertLess(output.index(rows[0]), next(i for i, text in enumerate(output) if text.startswith("HISTORY (1)")))
            self.assertGreater(output.index(rows[1]), next(i for i, text in enumerate(output) if text.startswith("HISTORY (1)")))


class ScheduleTests(unittest.TestCase):
    def test_adaptive_rbn_interval_is_used_by_the_countdown(self):
        args = cli.arguments([])
        poller = polling.Poller(args)
        poller.rbn.poll_interval = 15
        events, stop = Queue(), Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = True
        with patch.object(runtime, "Poller", return_value=poller), \
             patch.object(poller, "fetch", return_value=snapshot()), \
             patch.object(time, "monotonic", side_effect=[100, 105]):
            runtime.poll_worker(args, events, stop)
        self.assertEqual(events.get_nowait()[0], "loading")
        self.assertEqual(events.get_nowait()[0], "snapshot")
        self.assertEqual(events.get_nowait(), ("scheduled", 115))
        stop.wait.assert_called_once_with(10)

    def test_countdown_uses_remaining_interval_after_fetch(self):
        events = Queue()
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = True
        with patch.object(polling.Poller, "fetch", return_value=snapshot()), \
             patch.object(time, "monotonic", side_effect=[100, 105]):
            runtime.poll_worker(cli.arguments([]), events, stop)
        self.assertEqual(events.get_nowait()[0], "loading")
        self.assertEqual(events.get_nowait()[0], "snapshot")
        self.assertEqual(events.get_nowait(), ("scheduled", 160))
        stop.wait.assert_called_once_with(55)

    def test_retry_after_extends_actual_countdown(self):
        events = Queue()
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = True
        with patch.object(polling.Poller, "fetch", side_effect=models.SourceError("HTTP 429", 120)), \
             patch.object(time, "monotonic", side_effect=[100, 105]):
            runtime.poll_worker(cli.arguments([]), events, stop)
        self.assertEqual(events.get_nowait()[0], "loading")
        self.assertEqual(events.get_nowait(), ("error", "HTTP 429"))
        self.assertEqual(events.get_nowait(), ("scheduled", 225))
        stop.wait.assert_called_once_with(120)


class AdaptiveScheduleTests(unittest.TestCase):
    def setUp(self):
        self.clock = 0
        self.poller = polling.Poller(cli.arguments([]))
        self.poller.rbn.poll_interval = 15
        for patcher in (patch.object(time, "monotonic", side_effect=lambda: self.clock),
                        patch.object(time, "time", side_effect=lambda: NOW + self.clock)):
            patcher.start()
            self.addCleanup(patcher.stop)
        source_patch = patch.object(self.poller, "read_source", side_effect=self.read_source)
        rbn_patch = patch.object(self.poller.rbn, "fetch", return_value=[])
        self.source = source_patch.start()
        self.rbn = rbn_patch.start()
        self.addCleanup(source_patch.stop)
        self.addCleanup(rbn_patch.stop)

    def read_source(self, program):
        reference = "IT-0759" if program == "POTA" else "IFFF-0001"
        return [models.ActivationSpot("IU1PZC", reference, 14052.9, NOW + self.clock, program=program)], 0, set()

    def poll(self, when):
        self.clock = when
        return self.poller.poll(when)

    def test_fast_rbn_reads_reuse_sources_until_their_normal_deadline(self):
        self.poll(0)
        self.assertEqual(self.poller.next_poll_at(0, 2), 15)
        for when in (15, 30, 45):
            data = self.poll(when)
            self.assertTrue(all(spot.time == NOW for spot in data.spots))
        self.assertEqual(self.source.call_count, 2)
        self.assertEqual(self.rbn.call_count, 4)
        latest = self.poll(60)
        self.assertEqual(self.source.call_count, 4)
        self.assertTrue(all(spot.time == NOW + 60 for spot in latest.spots))

    def test_source_errors_and_retry_after_survive_cached_cycles_and_clear_on_recovery(self):
        def source(program):
            if program == "POTA" and self.clock == 0:
                raise models.SourceError("HTTP 429", 120)
            return self.read_source(program)
        self.source.side_effect = source
        first = self.poll(0)
        self.assertEqual(first.failed_programs, {"POTA"})
        for when in (15, 30, 45, 60, 75):
            data = self.poll(when)
            self.assertEqual(data.failed_programs, {"POTA"})
            self.assertIn("Retry-After", data.errors[0])
        self.assertEqual([call.args[0] for call in self.source.call_args_list].count("POTA"), 1)
        recovered = self.poll(120)
        self.assertEqual(recovered.failed_programs, set())
        self.assertEqual(recovered.errors, [])
        self.assertNotIn("POTA", self.poller.source_errors)

    def test_rbn_retry_after_pauses_rbn_but_keeps_sources_on_their_own_schedule(self):
        self.rbn.side_effect = [models.SourceError("HTTP 429", 120), []]
        self.poll(0)
        self.assertEqual(self.poller.next_poll_at(0, 2), 60)
        delayed = self.poll(60)
        self.assertEqual(self.rbn.call_count, 1)
        self.assertEqual(self.source.call_count, 4)
        self.assertIn("Retry-After", delayed.errors[0])
        self.assertEqual(self.poller.next_poll_at(60, 62), 120)
        self.poll(120)
        self.assertEqual(self.rbn.call_count, 2)

    def test_confirming_all_targets_does_not_postpone_the_next_source_refresh(self):
        def rbn_fetch(calls, receivers, age):
            if not calls:
                self.poller.rbn.poll_interval = self.poller.args.interval
            return [rbn()] if self.clock >= 30 and calls else []
        self.rbn.side_effect = rbn_fetch
        for when in (0, 15, 30, 45):
            self.poll(when)
        self.assertEqual(len(self.poller.confirmed), 2)
        self.assertEqual(self.source.call_count, 2)
        self.assertEqual(self.poller.rbn.poll_interval, 60)
        self.assertEqual(self.poller.next_poll_at(45, 46), 60)


class PollerTests(unittest.TestCase):
    def test_confirmed_unchanged_spot_is_not_requested_again(self):
        poller = polling.Poller(cli.arguments(["--programs", "POTA"]))
        with patch.object(time, "time", return_value=NOW), \
             patch.object(http, "get_json", return_value=[pota()]), \
             patch.object(poller.rbn, "fetch", side_effect=[[rbn()], []]) as fetch:
            first, second = poller.fetch(), poller.fetch()
        self.assertEqual(fetch.call_args_list[0].args[0], ["IU1PZC"])
        self.assertEqual(fetch.call_args_list[1].args[0], [])
        self.assertEqual(first.rbn, second.rbn)
        self.assertEqual(second.rbn[0].time, NOW - 42)

    def test_temporary_absence_does_not_forget_confirmed_spot(self):
        poller = polling.Poller(cli.arguments(["--programs", "POTA"]))
        with patch.object(time, "time", return_value=NOW), \
             patch.object(http, "get_json", side_effect=[[pota()], [], [pota()]]), \
             patch.object(poller.rbn, "fetch", side_effect=[[rbn()], [], []]) as fetch:
            poller.fetch()
            poller.fetch()
            again = poller.fetch()
        self.assertEqual(fetch.call_args_list[2].args[0], [])
        self.assertEqual(again.rbn, [rbn()])

    def test_park_name_update_does_not_require_another_rbn_query(self):
        poller = polling.Poller(cli.arguments(["--programs", "POTA"]))
        with patch.object(time, "time", return_value=NOW), \
             patch.object(http, "get_json", side_effect=[[pota(name="Old name")], [pota(name="New name")]]), \
             patch.object(poller.rbn, "fetch", side_effect=[[rbn()], []]) as fetch:
            poller.fetch()
            updated = poller.fetch()
        self.assertEqual(fetch.call_args_list[1].args[0], [])
        self.assertEqual(updated.spots[0].name, "New name")

    def test_unconfirmed_spot_is_checked_again(self):
        poller = polling.Poller(cli.arguments(["--programs", "POTA"]))
        with patch.object(time, "time", return_value=NOW), \
             patch.object(http, "get_json", return_value=[pota()]), \
             patch.object(poller.rbn, "fetch", return_value=[]) as fetch:
            poller.fetch()
            poller.fetch()
        self.assertEqual([call.args[0] for call in fetch.call_args_list], [["IU1PZC"], ["IU1PZC"]])

    def test_same_band_time_or_frequency_update_does_not_repeat_rbn_query(self):
        for updated in (pota(age=5), pota(frequency="14054")):
            poller = polling.Poller(cli.arguments(["--programs", "POTA"]))
            with self.subTest(updated=updated), patch.object(time, "time", return_value=NOW), \
                 patch.object(http, "get_json", side_effect=[[pota()], [updated]]), \
                 patch.object(poller.rbn, "fetch", side_effect=[[rbn()], []]) as fetch:
                poller.fetch()
                poller.fetch()
            self.assertEqual(fetch.call_args_list[1].args[0], [])

    def test_new_band_requires_separate_rbn_check(self):
        poller = polling.Poller(cli.arguments(["--programs", "POTA"]))
        with patch.object(time, "time", return_value=NOW), \
             patch.object(http, "get_json", side_effect=[[pota()], [pota(frequency="7028")]]), \
             patch.object(poller.rbn, "fetch", side_effect=[[rbn()], []]) as fetch:
            poller.fetch()
            second = poller.fetch()
        self.assertEqual(fetch.call_args_list[1].args[0], ["IU1PZC"])
        self.assertEqual(second.rbn, [])

    def test_old_cached_rbn_does_not_gain_a_new_timestamp(self):
        args = cli.arguments(["--programs", "POTA", "--pota-max-age", "1000"])
        poller, state = polling.Poller(args), monitoring.Monitor(args)
        with patch.object(time, "time", return_value=NOW), \
             patch.object(http, "get_json", return_value=[pota()]), \
             patch.object(poller.rbn, "fetch", side_effect=[[rbn(age=299)], []]) as fetch:
            state.apply(poller.fetch(), NOW)
            with patch.object(time, "time", return_value=NOW + 60):
                state.apply(poller.fetch(), NOW + 60)
        self.assertEqual(fetch.call_args_list[1].args[0], [])
        self.assertEqual([len(r) for r in state.rows(NOW + 60)], [1, 0])
        self.assertEqual(state.rows(NOW + 60)[0][0][0].last_heard, NOW - 299)
        self.assertEqual([len(r) for r in state.rows(NOW + 971)], [0, 1])


if __name__ == "__main__":
    unittest.main()
