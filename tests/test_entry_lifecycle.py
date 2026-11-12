import unittest

from cw_monitor import cli
from cw_monitor import models
from cw_monitor import monitor as monitoring
from cw_monitor import values as values_api
from cw_monitor.sources import programs
from cw_monitor.ui import layout as layout_ui
from cw_monitor.ui import table as table_ui
from cw_monitor.ui import views as views_ui
from test_pota_alert import is_row, NOW, pota


def snapshot(when, freq=14052.9, heard=True):
    return models.Snapshot([models.ActivationSpot("IU1PZC", "IT-0759", freq, when)],
                        [models.RbnSpot("IU1PZC", "SQ5J", freq, when, 18, 23)] if heard else [], [], when)


class EntryLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.args = cli.arguments([])
        self.state = monitoring.Monitor(self.args)

    def test_band_change_keeps_old_record_and_creates_only_one_new_record(self):
        self.state.apply(snapshot(NOW), NOW)
        self.assertEqual(len(self.state.apply(snapshot(NOW + 60, 7028), NOW + 60)), 1)
        self.assertEqual(set(self.state.entries), {("IU1PZC", "20m", "IT-0759", "POTA"), ("IU1PZC", "40m", "IT-0759", "POTA")})
        recent, history = self.state.rows(NOW + 60)
        self.assertEqual(recent[0][1].frequency, 7028)
        self.assertEqual(table_ui.row_values(*recent[0], NOW + 60)[0], "BND")
        self.assertIn("NEW BAND: 20m → 40m", views_ui.new_banner(recent, NOW + 60))
        self.assertEqual(history, [])
        self.assertEqual(recent[1][1].frequency, 14052.9)
        self.assertEqual(table_ui.row_values(*recent[1], NOW + 60)[10], "ACTIVE")
        self.assertEqual(self.state.apply(snapshot(NOW + 120, 14053.0, False), NOW + 120), [])
        active, history = self.state.rows(NOW + 120)
        self.assertEqual(history, [])
        self.assertEqual(len(active), 2)
        old_band = next(row for row in active if row[1].key[1] == "20m")
        self.assertEqual(old_band[1].frequency, 14053.0)
        self.assertEqual(table_ui.row_values(*old_band, NOW + 120, history=True)[0], "UPD")

    def test_history_stays_visible_after_hour_with_same_old_rbn_confirmation(self):
        self.state.apply(snapshot(NOW), NOW)
        for elapsed in range(60, 3601, 60):
            self.state.apply(snapshot(NOW + elapsed, heard=False), NOW + elapsed)
            recent, history = self.state.rows(NOW + elapsed)
            self.assertEqual(len(recent) + len(history), 1)
            row = (recent + history)[0]
            self.assertEqual(table_ui.row_values(*row, NOW + elapsed, history=bool(history))[10], "ACTIVE")
            self.assertEqual(row[1].receptions[0].time, NOW)
        stop = NOW + 3601
        self.state.apply(models.Snapshot([], [], [], stop, {("IU1PZC", "20m", "IT-0759", "POTA")}), stop)
        self.assertEqual(len(self.state.rows(stop)[1]), 1)
        self.assertEqual(table_ui.row_values(*self.state.rows(stop)[1][0], stop, history=True)[10], "INACTIVE")
        self.assertEqual(len(self.state.rows(stop + 1800)[1]), 1)
        self.assertEqual(self.state.rows(stop + 1801), ([], []))

    def test_known_entry_returns_after_hidden_history_without_new_alert(self):
        self.state.apply(snapshot(NOW), NOW)
        self.assertEqual(self.state.rows(NOW + 2101), ([], []))
        self.assertEqual(self.state.apply(snapshot(NOW + 2200, heard=False), NOW + 2200), [])
        recent, history = self.state.rows(NOW + 2200)
        self.assertEqual(history, [])
        self.assertEqual(len(recent), 1)
        self.assertEqual(table_ui.row_values(*recent[0], NOW + 2200)[10], "ACTIVE")

    def test_new_window_and_status_are_independent(self):
        self.args.new_max_age = 60
        self.state.apply(snapshot(NOW), NOW)
        self.assertEqual([len(rows) for rows in self.state.rows(NOW + 59)], [1, 0])
        recent, history = self.state.rows(NOW + 60)
        self.assertEqual(history, [])
        self.assertEqual(table_ui.row_values(*recent[0], NOW + 60)[10], "ACTIVE")
        self.assertTrue(recent[0][0].archived)
        self.assertEqual(views_ui.new_banner(recent, NOW + 60), "NEW: —")
        self.assertEqual(self.state.entries[("IU1PZC", "20m", "IT-0759", "POTA")].created_at, NOW)

    def test_old_active_frequency_change_is_reported_and_can_be_acknowledged(self):
        self.state.apply(snapshot(NOW), NOW)
        self.state.rows(NOW + 301)
        self.state.apply(snapshot(NOW + 310, 14053.1, False), NOW + 310)
        recent, history = self.state.rows(NOW + 310)
        self.assertEqual(history, [])
        self.assertIn("Spot 14052.9 → 14053.1 kHz", models.update_description(*recent[0], NOW + 310))
        frame = views_ui.screen_frame(self.state, NOW + 310, 24, 120)
        self.assertFalse(any("UPDATED:" in text for text, _ in frame))
        row, style = next((text, style) for text, style in frame if is_row(text, "IU1PZC"))
        self.assertIn("ACTIVE", row)
        self.assertIn("UPD", row)
        self.assertEqual(style, "normal")
        self.assertEqual([row[start:end] for start, end in row.highlights], ["14053.1"])
        rendered = views_ui.render(self.state, NOW + 310)
        self.assertNotIn("^UPDATED:", rendered)
        self.assertIn("ACTIVE UPD | Spot 14052.9 → 14053.1 kHz", rendered)
        self.state.acknowledge(NOW + 311)
        self.assertEqual(models.update_description(*recent[0], NOW + 311), "")
        self.assertEqual(table_ui.row_values(*recent[0], NOW + 311)[0], "")

    def test_comment_is_last_and_contains_keywords_instead_of_prefix_columns(self):
        self.state.apply(snapshot(NOW), NOW)
        recent, _ = self.state.rows(NOW)
        self.assertEqual(table_ui.row_values(*recent[0], NOW)[11], "ACTIVE NEW")
        for width in (80, 120):
            lines = [text for text, _ in views_ui.screen_frame(self.state, NOW, 24, width)]
            header = next(text for text in lines if text.startswith("Program"))
            self.assertTrue(header.rstrip().endswith("Comment"))
            self.assertNotIn("Status", header)
            self.assertLess(header.index("Band"), header.index("Spot kHz"))
            self.assertLess(header.index("Spot kHz"), header.index("RBN kHz"))
            row = next(text for text in lines if is_row(text, "IU1PZC"))
            self.assertTrue(row.rstrip().endswith("ACTIVE NEW"))
            self.assertTrue(any("BND=" in text and "UPD=" in text for text in lines))
        header = next(line for line in views_ui.render(self.state, NOW).splitlines() if "Comment" in line and "Activator" in line)
        self.assertTrue(header.rstrip().endswith("Comment"))

    def test_both_frequency_sources_highlight_only_their_updated_values(self):
        self.state.apply(snapshot(NOW), NOW)
        data = snapshot(NOW + 310, 14053.1)
        data.rbn[0] = models.RbnSpot("IU1PZC", "SQ5J", 14053.0, NOW + 310, 18, 23)
        self.state.apply(data, NOW + 310)
        frame = views_ui.screen_frame(self.state, NOW + 310, 24, 120)
        row = next(text for text, _ in frame if is_row(text, "IU1PZC"))
        self.assertEqual([row[start:end] for start, end in row.highlights], ["14053.1", "14053.0"])
        for when in (NOW + 370,):
            expired = next(text for text, _ in views_ui.screen_frame(self.state, when, 24, 120)
                           if is_row(text, "IU1PZC"))
            self.assertEqual(expired.highlights, ())

    def test_update_description_wraps_inside_last_comment_column(self):
        self.state.apply(snapshot(NOW), NOW)
        self.state.apply(snapshot(NOW + 10, 14053.1, False), NOW + 10)
        frame = views_ui.screen_frame(self.state, NOW + 10, 24, 80)
        header = next(text for text, _ in frame if text.startswith("Program"))
        comment_column = header.index("Comment")
        row_index = next(i for i, (text, _) in enumerate(frame) if is_row(text, "IU1PZC"))
        parts = [frame[row_index][0][comment_column:].strip()]
        for text, _ in frame[row_index + 1:]:
            if not text.startswith(" " * comment_column) or not text.strip():
                break
            parts.append(text[comment_column:].strip())
            self.assertLess(len(text), 80)
        self.assertGreater(len(parts), 1)
        self.assertEqual(" ".join(parts), "ACTIVE NEW UPD | Spot 14052.9 → 14053.1 kHz")

    def test_band_specific_pota_entries_and_qrt_are_preserved(self):
        first = pota(age=60)
        second = pota(age=10, frequency="7028")
        selected, _, inactive = programs.read_pota([first, second], NOW, 300)
        self.assertEqual({values_api.band_for(p.frequency) for p in selected}, {"20m", "40m"})
        selected, _, inactive = programs.read_pota([first, second, pota(age=0, frequency="7028", comments="QRT")], NOW, 300)
        self.assertEqual([values_api.band_for(p.frequency) for p in selected], ["20m"])
        self.assertEqual(inactive, {("IU1PZC", "40m", "IT-0759", "POTA")})

    def test_each_park_has_its_own_record_on_the_same_band(self):
        self.state.apply(snapshot(NOW), NOW)
        changed = snapshot(NOW + 10, heard=False)
        changed.spots.append(models.ActivationSpot("IU1PZC", "IT-0760", 14052.9, NOW + 10))
        changed.rbn = [models.RbnSpot("IU1PZC", "SQ5J", 14052.9, NOW + 10, 18, 23)]
        self.assertEqual(len(self.state.apply(changed, NOW + 10)), 1)
        self.assertEqual(len(self.state.entries), 2)
        self.assertEqual({m.references for _, m in self.state.rows(NOW + 10)[0]}, {("IT-0759",), ("IT-0760",)})
        self.assertEqual(self.state.apply(changed, NOW + 20), [])

    def test_new_keyword_lasts_one_minute_and_can_coexist_with_band_and_update(self):
        self.state.apply(snapshot(NOW), NOW)
        self.state.apply(snapshot(NOW + 10, 7028), NOW + 10)
        entry = self.state.entries[("IU1PZC", "40m", "IT-0759", "POTA")]
        self.assertEqual(table_ui.row_values(entry, entry.match, NOW + 69)[11], "ACTIVE NEW BND")
        self.assertEqual(table_ui.row_values(entry, entry.match, NOW + 70)[11], "ACTIVE")
        self.state.apply(snapshot(NOW + 20, 7028.1, False), NOW + 20)
        self.assertIn("ACTIVE NEW BND UPD", table_ui.row_values(entry, entry.match, NOW + 20)[11])
        self.state.acknowledge(NOW + 21)
        self.assertEqual(table_ui.row_values(entry, entry.match, NOW + 21)[11], "ACTIVE")

    def test_qrt_for_one_park_does_not_stop_another_park_on_the_same_band(self):
        payload = [pota(), pota(reference="IT-0760")]
        spots, _, stopped = programs.read_pota(payload, NOW, 300)
        proof = snapshot(NOW).rbn
        self.state.apply(models.Snapshot(spots, proof, [], NOW, stopped), NOW)
        payload = [pota(age=0, comments="QRT"), pota(age=0, reference="IT-0760")]
        spots, _, stopped = programs.read_pota(payload, NOW + 1, 300)
        self.state.apply(models.Snapshot(spots, [], [], NOW + 1, stopped), NOW + 1)
        first = self.state.entries[("IU1PZC", "20m", "IT-0759", "POTA")]
        second = self.state.entries[("IU1PZC", "20m", "IT-0760", "POTA")]
        self.assertIsNone(first.active_match(NOW + 1))
        self.assertIsNotNone(second.active_match(NOW + 1))
        self.assertEqual(len(self.state.entries), 2)

    def test_wide_terminal_shows_full_name_and_comment_with_band_second(self):
        full_name = "Nadwiślański Park Krajobrazowy imienia Jana Kochanowskiego w Kotlinie Sandomierskiej"
        first = snapshot(NOW)
        first.spots[0] = models.ActivationSpot("IU1PZC", "IT-0759", 14052.9, NOW, full_name)
        self.state.apply(first, NOW)
        second = snapshot(NOW + 10, 14053.1, False)
        second.spots[0] = models.ActivationSpot("IU1PZC", "IT-0759", 14053.1, NOW + 10, full_name)
        self.state.apply(second, NOW + 10)
        frame = views_ui.screen_frame(self.state, NOW + 10, 30, 280)
        header = next(text for text, _ in frame if text.startswith("Program"))
        self.assertEqual(header.replace("│", " ").split()[:4], ["Program", "Activator", "Band", "Spot"])
        self.assertLess(header.index("RBN kHz"), header.index("Reference"))
        row = next(text for text, _ in frame if is_row(text, "IU1PZC"))
        self.assertIn(f"IT-0759 ({full_name})", row)
        self.assertIn("ACTIVE NEW UPD | Spot 14052.9 → 14053.1 kHz", row)
        self.assertNotIn("…", row)
        self.assertLess(layout_ui.cell_width(row), 280)

    def test_wrapped_reference_name_inherits_new_normal_and_history_styles(self):
        name = "Rozległy rezerwat przyrody obejmujący dolinę rzeki oraz sąsiednie lasy i tereny podmokłe"
        for scenario, elapsed, expected in (("new", 0, "new"), ("normal", 31, "normal"),
                                             ("history active", 310, "normal"), ("inactive", 301, "history")):
            for width in (40, 80, 120):
                with self.subTest(scenario=scenario, width=width):
                    state = monitoring.Monitor(self.args)
                    data = snapshot(NOW)
                    data.spots[0] = models.ActivationSpot("IU1PZC", "IT-0759", 14052.9, NOW, name)
                    state.apply(data, NOW)
                    if scenario == "history active":
                        data = snapshot(NOW + elapsed, heard=False)
                        data.spots[0] = models.ActivationSpot("IU1PZC", "IT-0759", 14052.9, NOW + elapsed, name)
                        state.apply(data, NOW + elapsed)
                    frame = views_ui.screen_frame(state, NOW + elapsed, 50, width)
                    row = next((text, style) for text, style in frame if is_row(text, "IU1PZC"))
                    self.assertEqual(row[1], expected)
                    chunks = layout_ui.wrap_cells(f"IT-0759 ({name})", width - 5)
                    start = next(i for i, (text, _) in enumerate(frame) if text == "    " + chunks[0])
                    self.assertEqual(frame[start:start + len(chunks)], [("    " + text, expected) for text in chunks])

    def test_wrapped_name_highlight_expires_and_acknowledgement_clears_it_with_row(self):
        name = "Rozległy rezerwat przyrody obejmujący dolinę rzeki oraz sąsiednie lasy i tereny podmokłe"
        data = snapshot(NOW)
        data.spots[0] = models.ActivationSpot("IU1PZC", "IT-0759", 14052.9, NOW, name)
        self.state.apply(data, NOW)
        prefix = "    " + layout_ui.wrap_cells(f"IT-0759 ({name})", 75)[0]
        for elapsed, expected in ((29, "new"), (30, "normal")):
            frame = views_ui.screen_frame(self.state, NOW + elapsed, 50, 80)
            self.assertEqual(next(style for text, style in frame if text == prefix), expected)
        self.state.acknowledge(NOW + 1)
        frame = views_ui.screen_frame(self.state, NOW + 1, 50, 80)
        self.assertEqual(next(style for text, style in frame if text == prefix), "normal")

    def test_full_name_wraps_without_loss_in_narrow_terminal(self):
        name = "Rozległy rezerwat przyrody obejmujący dolinę rzeki oraz sąsiednie lasy i tereny podmokłe"
        data = snapshot(NOW)
        data.spots[0] = models.ActivationSpot("IU1PZC", "IT-0759", 14052.9, NOW, name)
        self.state.apply(data, NOW)
        frame = views_ui.screen_frame(self.state, NOW, 30, 80)
        row_index = next(i for i, (text, _) in enumerate(frame) if is_row(text, "IU1PZC"))
        details = []
        for text, _ in frame[row_index + 1:]:
            if not text.startswith("    ") or not text.strip() or "First seen:" in text:
                break
            details.append(text.strip())
            self.assertLess(layout_ui.cell_width(text), 80)
        self.assertEqual(" ".join(details), f"IT-0759 ({name})")
        self.assertNotIn("…", " ".join(details))

    def test_wide_unicode_park_name_preserves_highlight_offsets(self):
        name = "京都府自然公園" * 5
        data = snapshot(NOW)
        data.spots[0] = models.ActivationSpot("IU1PZC", "IT-0759", 14052.9, NOW, name)
        self.state.apply(data, NOW)
        data.spots[0] = models.ActivationSpot("IU1PZC", "IT-0759", 14053.1, NOW + 1, name)
        data.rbn = []
        self.state.apply(data, NOW + 1)
        row = next(text for text, _ in views_ui.screen_frame(self.state, NOW + 1, 30, 260)
                   if is_row(text, "IU1PZC"))
        self.assertIn(name, row)
        self.assertLess(layout_ui.cell_width(row), 260)
        self.assertEqual([row[a:b] for a, b in row.highlights], ["14053.1"])


if __name__ == "__main__":
    unittest.main()
