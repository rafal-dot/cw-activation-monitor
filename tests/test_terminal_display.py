import unittest
from unittest.mock import Mock, patch

from cw_monitor.ui import terminal as ui
from cw_monitor.ui.layout import HighlightedText


class TerminalDisplayTests(unittest.TestCase):
    def color_display(self, colors=256, pairs=64, reject_extended=False):
        screen = Mock()
        screen.getmaxyx.return_value = (4, 60)

        def init(number, foreground, background):
            if reject_extended and foreground >= 16:
                raise ui.curses.error("unsupported colour")

        with patch.object(ui.curses, "curs_set"), \
             patch.object(ui.curses, "has_colors", return_value=True), \
             patch.object(ui.curses, "start_color"), \
             patch.object(ui.curses, "COLORS", colors, create=True), \
             patch.object(ui.curses, "COLOR_PAIRS", pairs, create=True), \
             patch.object(ui.curses, "init_pair", side_effect=init) as initialized, \
             patch.object(ui.curses, "color_pair", side_effect=lambda n: n << 8):
            display = ui.CursesDisplay(screen)
        return screen, display, initialized

    def display(self):
        screen = Mock()
        screen.getmaxyx.return_value = (4, 30)
        with patch.object(ui.curses, "curs_set"), patch.object(ui.curses, "has_colors", return_value=False):
            display = ui.CursesDisplay(screen)
        return screen, display

    def test_highlights_use_black_text_without_bold_brightening(self):
        screen = Mock()
        with patch.object(ui.curses, "curs_set"), \
             patch.object(ui.curses, "has_colors", return_value=True), \
             patch.object(ui.curses, "start_color"), \
             patch.object(ui.curses, "init_pair") as pair, \
             patch.object(ui.curses, "color_pair", side_effect=lambda n: n << 8):
            display = ui.CursesDisplay(screen)
        pair.assert_any_call(1, ui.curses.COLOR_BLACK, ui.curses.COLOR_YELLOW)
        pair.assert_any_call(4, ui.curses.COLOR_BLACK, ui.curses.COLOR_WHITE)
        for style in ("new", "banner"):
            self.assertEqual(display.attributes[style], 1 << 8)
            self.assertFalse(display.attributes[style] & ui.curses.A_REVERSE)
            self.assertFalse(display.attributes[style] & ui.curses.A_BOLD)
        self.assertEqual(display.attributes["updated_cell"], ui.curses.A_UNDERLINE | (4 << 8))
        self.assertFalse(display.attributes["updated_cell"] & ui.curses.A_BOLD)

    def test_monochrome_terminal_uses_bold_reverse_video(self):
        _, display = self.display()
        for style in ("new", "banner"):
            self.assertEqual(display.attributes[style], ui.curses.A_BOLD | ui.curses.A_REVERSE)

    def test_identical_frame_writes_nothing_and_does_not_clear_screen(self):
        screen, display = self.display()
        with patch.object(ui.curses, "doupdate") as update:
            display.draw([("IU1PZC 18 dB", "normal")])
            screen.reset_mock()
            update.reset_mock()
            display.draw([("IU1PZC 18 dB", "normal")])
        screen.addstr.assert_not_called()
        screen.erase.assert_not_called()
        screen.noutrefresh.assert_not_called()
        update.assert_not_called()

    def test_changed_snr_writes_only_changed_cells(self):
        screen, display = self.display()
        with patch.object(ui.curses, "doupdate"):
            display.draw([("IU1PZC 18 dB", "normal")])
            screen.reset_mock()
            display.draw([("IU1PZC 19 dB", "normal")])
        screen.addstr.assert_called_once_with(0, 8, "9", 0)
        screen.erase.assert_not_called()

    def test_unicode_separator_does_not_force_full_row_redraw(self):
        screen, display = self.display()
        text = "IU1PZC │    18 dB"
        separators = [(text.index("│"), text.index("│") + 1)]
        with patch.object(ui.curses, "doupdate"):
            display.draw([(HighlightedText(text, [], separators), "normal")])
            screen.reset_mock()
            display.draw([(HighlightedText(text.replace("18", "19"), [], separators), "normal")])
        screen.addstr.assert_called_once_with(0, text.index("8"), "9", 0)
        screen.erase.assert_not_called()

    def test_separator_and_changed_value_keep_distinct_styles_in_new_row(self):
        screen, display = self.display()
        text = "IU1PZC │ 14053.1"
        separators = [(7, 8)]
        with patch.object(ui.curses, "doupdate"):
            display.draw([(HighlightedText(text, [(9, 16)], separators), "new")])
            screen.addstr.assert_any_call(0, 7, "│", display.attributes["separator"])
            screen.addstr.assert_any_call(0, 9, "14053.1", display.attributes["updated_cell"])
            screen.reset_mock()
            display.draw([(HighlightedText(text, [], separators), "new")])
        screen.addstr.assert_called_once_with(0, 9, "14053.1", display.attributes["new"])
        self.assertTrue(display.attributes["table_header"] & ui.curses.A_BOLD)

    def test_updated_field_has_its_own_style_and_identical_frame_writes_nothing(self):
        screen, display = self.display()
        text = HighlightedText("IU1PZC 14053.1 18 dB", [(7, 14)])
        with patch.object(ui.curses, "doupdate"):
            display.draw([(text, "normal")])
            screen.addstr.assert_any_call(0, 7, "14053.1", display.attributes["updated_cell"])
            screen.addstr.assert_any_call(0, 0, "IU1PZC ", 0)
            screen.reset_mock()
            display.draw([(text, "normal")])
        screen.addstr.assert_not_called()
        screen.erase.assert_not_called()

    def test_highlight_expiration_repaints_only_field_even_when_text_is_unchanged(self):
        screen, display = self.display()
        text = "IU1PZC 14053.1 18 dB"
        with patch.object(ui.curses, "doupdate"):
            display.draw([(HighlightedText(text, [(7, 14)]), "normal")])
            screen.reset_mock()
            display.draw([(text, "normal")])
        screen.addstr.assert_called_once_with(0, 7, "14053.1", 0)
        screen.erase.assert_not_called()

    def test_field_highlight_uses_terminal_cells_after_a_wide_character(self):
        screen, display = self.display()
        with patch.object(ui.curses, "doupdate"):
            display.draw([(HighlightedText("界 14053.1", [(2, 9)]), "normal")])
        screen.addstr.assert_any_call(0, 3, "14053.1", display.attributes["updated_cell"])

    def test_removed_row_is_erased_without_clearing_screen(self):
        screen, display = self.display()
        with patch.object(ui.curses, "doupdate"):
            display.draw([("IU1PZC", "normal"), ("YO5BDL", "history")])
            screen.reset_mock()
            display.draw([("IU1PZC", "normal")])
        screen.addstr.assert_called_once()
        self.assertEqual(screen.addstr.call_args.args[:2], (1, 0))
        self.assertFalse(screen.addstr.call_args.args[2].strip())
        screen.erase.assert_not_called()

    def test_resize_redraws_safely(self):
        screen, display = self.display()
        with patch.object(ui.curses, "doupdate"):
            display.draw([("IU1PZC", "normal")])
            screen.getmaxyx.return_value = (2, 10)
            display.draw([("IU1PZC 18 dB", "normal")])
        screen.erase.assert_called_once()
        self.assertEqual(display.size, (2, 10))

    def test_width_accounts_for_wide_characters(self):
        self.assertEqual(ui.fit_cells("ABC界DEF", 5), "ABC界")
        self.assertEqual(ui.fit_cells("ABC界DEF", 4), "ABC ")
        self.assertEqual(ui.fit_cells("A\x1b\nB", 3), "AB ")

    def test_band_colour_is_confined_to_its_cell_and_history_stays_dimmed(self):
        screen, display, _ = self.color_display()
        text = HighlightedText("POTA 20m 14052.9", [], band_spans=[(5, 8, "20m")])
        with patch.object(ui.curses, "doupdate"):
            display.draw([(text, "normal")])
            screen.addstr.assert_any_call(0, 0, "POTA ", 0)
            screen.addstr.assert_any_call(0, 5, "20m", display.band_attributes["20m"])
            screen.reset_mock()
            display.draw([(text, "history")])
        screen.addstr.assert_any_call(0, 5, "20m", ui.curses.A_DIM | display.band_attributes["20m"])

    def test_new_and_updated_highlights_take_priority_over_band_colour(self):
        screen, display, _ = self.color_display()
        text = "POTA 20m 14052.9"
        with patch.object(ui.curses, "doupdate"):
            display.draw([(HighlightedText(text, [], band_spans=[(5, 8, "20m")]), "new")])
            row_calls = [call for call in screen.addstr.call_args_list if call.args[0] == 0]
            self.assertEqual(len(row_calls), 1)
            self.assertEqual(row_calls[0].args[3], display.attributes["new"])
            screen.reset_mock()
            display.draw([(HighlightedText(text, [(5, 8)], band_spans=[(5, 8, "20m")]), "new")])
            screen.addstr.assert_called_once_with(0, 5, "20m", display.attributes["updated_cell"])
            screen.reset_mock()
            display.draw([(HighlightedText(text, [], band_spans=[(5, 8, "20m")]), "normal")])
        screen.addstr.assert_any_call(0, 5, "20m", display.band_attributes["20m"])

    def test_band_colour_does_not_break_incremental_redraw_or_repeat_unchanged_frame(self):
        screen, display, _ = self.color_display()
        text = "POTA 20m 14052.9"
        initial = HighlightedText(text, [], band_spans=[(5, 8, "20m")])
        with patch.object(ui.curses, "doupdate") as update:
            display.draw([(initial, "normal")])
            screen.reset_mock()
            update.reset_mock()
            display.draw([(initial, "normal")])
            screen.addstr.assert_not_called()
            update.assert_not_called()
            changed = HighlightedText(text[:-1] + "8", [], band_spans=[(5, 8, "20m")])
            display.draw([(changed, "normal")])
        screen.addstr.assert_called_once_with(0, len(text) - 1, "8", 0)
        screen.erase.assert_not_called()

    def test_changed_band_style_with_identical_text_repaints_only_the_band(self):
        screen, display, _ = self.color_display()
        with patch.object(ui.curses, "doupdate"):
            display.draw([(HighlightedText("POTA 20m", [], band_spans=[(5, 8, "20m")]), "normal")])
            screen.reset_mock()
            display.draw([(HighlightedText("POTA 20m", [], band_spans=[(5, 8, "40m")]), "normal")])
        screen.addstr.assert_called_once_with(0, 5, "20m", display.band_attributes["40m"])

    def test_band_spans_follow_terminal_cell_positions_after_wide_characters(self):
        screen, display, _ = self.color_display()
        with patch.object(ui.curses, "doupdate"):
            display.draw([(HighlightedText("界 20m", [], band_spans=[(2, 5, "20m")]), "normal")])
        screen.addstr.assert_any_call(0, 3, "20m", display.band_attributes["20m"])

    def test_unknown_bands_and_monochrome_terminals_keep_normal_text(self):
        screen, display, _ = self.color_display()
        unknown = HighlightedText("POTA —", [], band_spans=[(5, 6, "—")])
        with patch.object(ui.curses, "doupdate"):
            display.draw([(unknown, "normal")])
        self.assertTrue(all(call.args[3] == 0 for call in screen.addstr.call_args_list))
        screen, display = self.display()
        self.assertEqual(display.band_attributes, {})
        with patch.object(ui.curses, "doupdate"):
            display.draw([(HighlightedText("POTA 20m", [], band_spans=[(5, 8, "20m")]), "normal")])
        self.assertTrue(all(call.args[3] == 0 for call in screen.addstr.call_args_list))

    def test_palette_adapts_to_eight_colours_and_limited_pair_capacity(self):
        _, display, initialized = self.color_display(colors=8, pairs=8)
        self.assertTrue(all(call.args[0] < 8 and call.args[1] < 8 for call in initialized.call_args_list))
        self.assertNotEqual(display.band_attributes["20m"], display.band_attributes["40m"])
        self.assertNotEqual(display.band_attributes["30m"], display.band_attributes["40m"])
        _, limited, initialized = self.color_display(colors=256, pairs=6)
        self.assertTrue(all(call.args[0] < 6 for call in initialized.call_args_list))
        self.assertTrue(all(value in (2 << 8, 5 << 8) for value in limited.band_attributes.values()))

    def test_palette_falls_back_when_terminal_rejects_extended_colours(self):
        _, display, _ = self.color_display(reject_extended=True)
        _, basic, _ = self.color_display(colors=8, pairs=64)
        self.assertEqual(display.band_attributes, basic.band_attributes)

    def test_extended_palette_distinguishes_common_bands(self):
        _, display, _ = self.color_display()
        attributes = [display.band_attributes[band] for band in ("160m", "80m", "40m", "30m", "20m", "15m", "10m")]
        self.assertEqual(len(set(attributes)), len(attributes))


if __name__ == "__main__":
    unittest.main()
