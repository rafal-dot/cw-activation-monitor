import contextlib
import io
import os
import re
import unittest
from unittest.mock import Mock, patch

from cw_monitor import cli, models, monitor, runtime
from cw_monitor.ui import colors, terminal, views
from cw_monitor.ui.layout import HighlightedText
from test_pota_alert import NOW, rbn


class ColorPolicyTests(unittest.TestCase):
    def test_defaults_boundaries_and_custom_thresholds(self):
        args = cli.arguments([])
        self.assertEqual((args.color, args.snr_thresholds, args.wpm_thresholds), ('auto', (10, 20), (15, 24)))
        for value, expected in [(-5, 'red'), (9, 'red'), (10, 'yellow'), (19, 'yellow'), (20, 'green')]:
            self.assertEqual(colors.value_spans(5, str(value), (10, 20), (15, 24))[0][2], expected)
        for value, expected in [(15, 'green'), (16, 'yellow'), (24, 'yellow'), (25, 'red')]:
            self.assertEqual(colors.value_spans(6, str(value), (10, 20), (15, 24))[0][2], expected)
        args = cli.arguments(['--wpm-thresholds', '12,20', '--snr-thresholds=-5,5'])
        self.assertEqual(colors.value_spans(6, '15', args.snr_thresholds, args.wpm_thresholds)[0][2], 'yellow')
        self.assertEqual(colors.value_spans(5, '5', args.snr_thresholds, args.wpm_thresholds)[0][2], 'green')

    def test_invalid_thresholds_fail_clearly(self):
        for option, value in [('--wpm-thresholds', '0,24'), ('--wpm-thresholds', '24,15'),
                              ('--wpm-thresholds', '15,15'), ('--snr-thresholds', '10'),
                              ('--snr-thresholds', 'nan,20'), ('--snr-thresholds', '1,2,3')]:
            with self.subTest(option=option, value=value), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                cli.arguments([option, value])

    def test_auto_respects_terminal_redirection_and_no_color(self):
        stream = Mock()
        stream.isatty.return_value = True
        with patch.dict(os.environ, {'TERM': 'xterm-256color'}, clear=True):
            self.assertTrue(colors.colors_enabled('auto', stream))
            self.assertFalse(colors.colors_enabled('never', stream))
            stream.isatty.return_value = False
            self.assertFalse(colors.colors_enabled('auto', stream))
            self.assertTrue(colors.colors_enabled('always', stream))
            stream.isatty.return_value = True
            with patch.dict(os.environ, NO_COLOR=''):
                self.assertFalse(colors.colors_enabled('auto', stream))
                self.assertTrue(colors.colors_enabled('always', stream))
            with patch.dict(os.environ, TERM='dumb'):
                self.assertFalse(colors.colors_enabled('auto', stream))

    def state(self, args=None):
        state = monitor.Monitor(args or cli.arguments(['--programs', 'POTA', '--qth', 'KO02KF']))
        spot = models.ActivationSpot('IU1PZC', 'IT-0759', 14052.9, NOW)
        data = [rbn(snr=20), models.RbnSpot('IU1PZC', 'SQ5J', 14052.9, NOW - 30, 9, 15)]
        state.apply(models.Snapshot([spot], data, [], NOW), NOW)
        return state

    def test_receiver_order_and_headers_stay_unchanged(self):
        state = self.state()
        rows = views.screen_frame(state, NOW + 31, 40, 240)
        text = next(text for text, _ in rows if 'IU1PZC' in text and 'IT-0759' in text and '│' in text)
        actual = [(text[a:b], color) for a, b, color in text.color_spans]
        self.assertEqual(actual, [('POTA', 'yellow'), ('20', 'green'), ('9', 'red'),
                                  ('23', 'yellow'), ('15', 'green')])
        self.assertTrue(all(not text.color_spans for text, style in rows if style == 'table_header'))

    def test_narrow_measurement_details_keep_individual_colours(self):
        state = self.state()
        frame = views.screen_frame(state, NOW + 31, 50, 45)
        cells = [(text[a:b], c) for text, _ in frame for a, b, c in getattr(text, 'color_spans', ())]
        for value in [('20', 'green'), ('9', 'red'), ('23', 'yellow'), ('15', 'green')]:
            self.assertIn(value, cells)

    def test_ansi_layout_preserves_text_width_and_highlight_priority(self):
        state = self.state()
        plain = views.render(state, NOW + 31)
        styled = views.render(state, NOW + 31, color=True)
        self.assertEqual(re.sub(r'\x1b\[[0-9;]*m', '', styled), plain)
        self.assertIn('\x1b[33mPOTA', styled)
        self.assertIn('\x1b[32m20', styled)
        text = HighlightedText('20', [(1, 2)], color_spans=[(0, 2, 'red')])
        self.assertEqual(colors.ansi_text(text, 'new'), '\x1b[30;43m2\x1b[4;30;47m0\x1b[0m')

    def test_plain_cli_never_emits_ansi_when_colors_are_disabled(self):
        output = io.StringIO()
        args = cli.arguments(['--demo', '--color', 'never'])
        with contextlib.redirect_stdout(output):
            self.assertEqual(runtime.run_plain(args), 0)
        self.assertNotIn('\x1b', output.getvalue())
        output = io.StringIO()
        args = cli.arguments(['--demo', '--color', 'always'])
        with contextlib.redirect_stdout(output):
            self.assertEqual(runtime.run_plain(args), 0)
        self.assertIn('\x1b[', output.getvalue())


class CursesValueColorTests(unittest.TestCase):
    def display(self, enabled=True, supported=True, count=8):
        screen = Mock()
        screen.getmaxyx.return_value = (4, 60)
        with contextlib.ExitStack() as stack:
            for name, kwargs in [('curs_set', {}), ('has_colors', {'return_value': supported}),
                                 ('start_color', {}), ('init_pair', {}),
                                 ('COLORS', {'new': count}), ('COLOR_PAIRS', {'new': 64}),
                                 ('color_pair', {'side_effect': lambda n: n << 8})]:
                stack.enter_context(patch.object(terminal.curses, name, create=True, **kwargs))
            display = terminal.CursesDisplay(screen, colors=enabled)
        return screen, display

    def test_programs_and_threshold_values_have_distinct_basic_colours(self):
        screen, display = self.display()
        initialized = []
        for program, color in colors.PROGRAM_COLORS.items():
            text = HighlightedText(program, [], color_spans=[(0, len(program), color)])
            with patch.object(terminal.curses, 'doupdate'):
                display.draw([(text, 'normal')])
            initialized.append(display.value_attributes[color])
        self.assertEqual(len(set(initialized)), 3)
        self.assertEqual(screen.addstr.call_args_list[-1].args[3], display.value_attributes['blue'])

    def test_new_and_update_override_value_colours_and_history_stays_dim(self):
        screen, display = self.display()
        text = HighlightedText('20', [], color_spans=[(0, 2, 'green')])
        with patch.object(terminal.curses, 'doupdate'):
            display.draw([(text, 'new')])
            row_calls = [call.args for call in screen.addstr.call_args_list if call.args[0] == 0]
            self.assertEqual(row_calls[0][:2], (0, 0))
            self.assertEqual(row_calls[0][2].strip(), '20')
            self.assertEqual(row_calls[0][3], display.attributes['new'])
            screen.reset_mock()
            display.draw([(HighlightedText('20', [(0, 2)], color_spans=[(0, 2, 'green')]), 'new')])
            screen.addstr.assert_called_once_with(0, 0, '20', display.attributes['updated_cell'])
            screen.reset_mock()
            display.draw([(text, 'history')])
            screen.addstr.assert_any_call(0, 0, '20', terminal.curses.A_DIM | display.value_attributes['green'])

    def test_semantic_style_changes_repaint_only_changed_cells(self):
        screen, display = self.display()
        with patch.object(terminal.curses, 'doupdate'):
            text = HighlightedText('POTA 20', [], color_spans=[(0, 4, 'yellow'), (5, 7, 'green')])
            display.draw([(text, 'normal')])
            screen.reset_mock()
            display.draw([(text, 'normal')])
            screen.addstr.assert_not_called()
            display.draw([(HighlightedText('POTA 20', [], color_spans=[(0, 4, 'yellow'), (5, 7, 'red')]), 'normal')])
            screen.addstr.assert_called_once_with(0, 5, '20', display.value_attributes['red'])
        screen.erase.assert_not_called()

    def test_never_and_monochrome_terminals_keep_style_but_no_colours(self):
        for enabled, supported in [(False, True), (True, False)]:
            screen, display = self.display(enabled, supported)
            self.assertEqual(display.value_attributes, {})
            self.assertEqual(display.band_attributes, {})
            with patch.object(terminal.curses, 'doupdate'):
                display.draw([(HighlightedText('20', [], color_spans=[(0, 2, 'green')]), 'normal')])
            self.assertTrue(all(call.args[3] == 0 for call in screen.addstr.call_args_list))
