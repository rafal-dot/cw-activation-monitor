"""Regressions for transport, log ownership and terminal responsiveness."""

import argparse
import contextlib
import csv
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from cw_monitor import cli, models, monitor, runtime, values
from cw_monitor.sources import http, programs
from cw_monitor.storage import events, session, summary
from cw_monitor.ui import terminal, views
from test_pota_alert import NOW, pota, rbn


class TransportTests(unittest.TestCase):
    def test_oversized_valid_json_prefix_is_rejected_without_unbounded_read(self):
        body = io.BytesIO(b'[]' + b' ' * 40)
        with patch.object(http, 'MAX_RESPONSE_BYTES', 32), \
                patch.object(http, 'urlopen', return_value=body), \
                self.assertRaisesRegex(models.SourceError, 'exceeds 32 bytes'):
            http.get_json('https://example.invalid/spots', 5)
        self.assertTrue(body.closed)

    def test_valid_bounded_response_is_decoded(self):
        body = io.BytesIO('["Bărnuțiu"]'.encode())
        with patch.object(http, 'urlopen', return_value=body):
            self.assertEqual(http.get_json('https://example.invalid/spots', 5), ['Bărnuțiu'])
        self.assertTrue(body.closed)

    def test_http_error_closes_response_and_rejects_nonfinite_retry_after(self):
        for retry, expected in [('inf', 0), ('NaN', 0), ('60', 60), ('invalid', 0)]:
            body = io.BytesIO(b'busy')
            error = HTTPError('https://example.invalid', 429, 'busy', {'Retry-After': retry}, body)
            with self.subTest(retry=retry), patch.object(http, 'urlopen', side_effect=error), \
                    self.assertRaises(models.SourceError) as caught:
                http.get_text(error.url, 5)
            self.assertEqual(caught.exception.retry_after, expected)
            self.assertTrue(body.closed)

    def test_boolean_frequency_is_invalid_in_source_data(self):
        with self.assertRaises(ValueError):
            values.frequency(True)
        spots, skipped = programs.select_pota([pota(), pota(frequency=True, activator='BAD1')], NOW, 300)
        self.assertEqual([spot.call for spot in spots], ['IU1PZC'])
        self.assertEqual(skipped, 1)


class LogOwnershipTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_event_log_rejects_second_writer_and_releases_lock_on_close(self):
        path = self.root / 'events.csv'
        first = events.EventLog(path, 'events')
        self.addCleanup(first.close)
        first.record('ONE', NOW)
        original = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'already in use'):
            events.EventLog(path, 'events')
        self.assertEqual(path.read_bytes(), original)
        first.close()
        second = events.EventLog(path, 'events')
        self.addCleanup(second.close)
        second.record('TWO', NOW)
        self.assertEqual([row['event'] for row in second.records()], ['ONE', 'TWO'])

    def test_constructor_failure_releases_event_log_lock(self):
        path = self.root / 'events.csv'
        path.write_text('wrong,header\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'header'):
            events.EventLog(path, 'events')
        path.write_text(','.join(events.EventLog.fields) + '\n', encoding='utf-8')
        writer = events.EventLog(path, 'events')
        writer.close()

    def test_csv_readers_accept_utf8_bom_without_losing_first_field(self):
        event_path = self.root / 'events.csv'
        with event_path.open('w', encoding='utf-8-sig', newline='') as output:
            writer = csv.DictWriter(output, fieldnames=events.EventLog.fields)
            writer.writeheader()
            writer.writerow({'timestamp_utc': events.EventLog.utc(NOW), 'event': 'TEST', 'detail': 'Bărnuțiu'})
        event_log = events.EventLog(event_path, 'events')
        self.addCleanup(event_log.close)
        event_log.record('NEXT', NOW)
        rows = list(event_log.records())
        self.assertEqual(rows[0]['timestamp_utc'], events.EventLog.utc(NOW))
        self.assertEqual(rows[0]['detail'], 'Bărnuțiu')
        self.assertEqual(rows[1]['event'], 'NEXT')

        daily_path = self.root / 'daily.csv'
        daily = summary.DailySummary(daily_path)
        match = models.Match('IU1PZC', ('IT-0759',), 14052.9, [rbn()], {'IT-0759': 'Bărnuțiu'})
        daily.observe(match, NOW)
        daily.close()
        daily_path.write_bytes(b'\xef\xbb\xbf' + daily_path.read_bytes())
        restored = summary.DailySummary(daily_path)
        self.addCleanup(restored.close)
        self.assertEqual(next(iter(restored.rows.values())).name, 'Bărnuțiu')

    def test_daily_replace_preserves_existing_file_permissions(self):
        path = self.root / 'daily.csv'
        writer = summary.DailySummary(path)
        self.addCleanup(writer.close)
        path.chmod(0o640)
        writer.observe(models.Match('IU1PZC', ('IT-0759',), 14052.9, [rbn()]), NOW)
        writer.flush()
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_session_closes_both_writers_when_one_close_fails(self):
        daily, event_log = Mock(error=''), Mock(error='')
        daily.close.side_effect = OSError('disk full')
        errors = session.close_logs(argparse.Namespace(_daily_summary=daily, _event_log=event_log))
        self.assertEqual(errors, ['SUMMARY ERROR: disk full'])
        event_log.close.assert_called_once_with()

    def test_final_summary_write_failure_is_reported_with_exit_code_two(self):
        path = self.root / 'daily.csv'

        def run(args):
            args._daily_summary.observe(models.Match('IU1PZC', ('IT-0759',), 14052.9, [rbn()]), NOW)
            args._daily_summary._write = Mock(side_effect=OSError('disk full'))
            return 0

        output = io.StringIO()
        with patch.object(cli, 'run', side_effect=run), contextlib.redirect_stderr(output):
            self.assertEqual(cli.main(['--demo', '--summary-file', str(path)]), 2)
        self.assertIn('SUMMARY ERROR: disk full', output.getvalue())
        # The failed writer must also release its lock.
        reopened = summary.DailySummary(path)
        reopened.close()


class TerminalLoopTests(unittest.TestCase):
    def drive(self, count, actions=None):
        actions = actions or {}
        tick = [100.0]
        screen, display = Mock(), Mock()
        screen.getmaxyx.return_value = (30, 240)
        calls = []

        def getch():
            index = len(calls)
            calls.append(index)
            tick[0] += .25
            action = actions.get(index, -1)
            if action == 'resize':
                screen.getmaxyx.return_value = (31, 250)
                action = -1
            return ord('q') if index == count - 1 else action

        screen.getch.side_effect = getch
        args = cli.arguments(['--programs', 'POTA', '--qth', 'KO02KF'])
        state = monitor.Monitor(args)
        spot = models.ActivationSpot('IU1PZC', 'IT-0759', 14052.9, NOW)
        state.apply(models.Snapshot([spot], [rbn()], [], NOW), NOW)
        frames = []
        display.draw.side_effect = lambda frame: frames.append((tick[0], frame))
        with patch.object(runtime, 'Thread'), patch.object(terminal, 'CursesDisplay', return_value=display), \
                patch.object(runtime, 'Monitor', return_value=state), \
                patch.object(runtime.time, 'monotonic', side_effect=lambda: tick[0]), \
                patch.object(runtime.time, 'time', return_value=NOW):
            self.assertEqual(runtime.run_terminal(screen, args), 0)
        return frames, state, calls

    def test_idle_frames_are_built_once_per_second_while_keys_are_polled(self):
        frames, _, calls = self.drive(17)
        self.assertEqual(len(calls), 17)
        self.assertEqual([tick for tick, _ in frames], [100, 101, 102, 103, 104])

    def test_acknowledgement_batch_mode_and_resize_redraw_immediately(self):
        frames, state, _ = self.drive(10, {1: ord(' '), 3: ord('b'), 5: 'resize'})
        self.assertEqual([tick for tick, _ in frames], [100, 100.5, 101, 101.5])
        self.assertTrue(state.batch_mode)
        self.assertTrue(any('b: batches ON' in text for text, _ in frames[-1][1]))
        self.assertFalse(any(style == 'new' for _, style in frames[-1][1]))

    def test_plain_and_terminal_column_order_matches_requested_signal_group(self):
        args = cli.arguments(['--programs', 'POTA', '--qth', 'KO02KF'])
        state = monitor.Monitor(args)
        state.apply(models.Snapshot([models.ActivationSpot('IU1PZC', 'IT-0759', 14052.9, NOW)],
                                    [rbn()], [], NOW), NOW)
        plain = next(line for line in views.render(state, NOW).splitlines() if line.startswith('Program'))
        terminal_headers = [line for line, style in views.screen_frame(state, NOW, 30, 240) if style == 'table_header']
        for header in [plain] + terminal_headers:
            columns = header.replace('│', '|').split('|')
            names = [name.strip() for name in columns]
            start = names.index('RBN kHz')
            self.assertEqual(names[start:start + 4], ['RBN kHz', 'SNR dB' if header == plain else 'dB', 'WPM', 'Reference'])

    def test_sort_menu_applies_once_cancels_drafts_and_keeps_ticking_and_resizing(self):
        from cw_monitor.sorting import SortOrder
        with patch('cw_monitor.monitor.save_sort', return_value='') as save:
            frames, state, calls = self.drive(14, {0: ord('s'), 1: ord('5'), 2: ord('d'), 3: 10,
                                                   6: ord('s'), 7: ord('3'), 8: 27, 10: 'resize'})
        self.assertEqual(len(calls), 14)
        self.assertEqual(state.sort, SortOrder('distance', 'desc'))
        save.assert_called_once_with(SortOrder('distance', 'desc'))
        self.assertTrue(any('Sort activations' in text for _, frame in frames for text, _ in frame))
        self.assertTrue(any('Sort: Dist ↓' in text for text, _ in frames[-1][1]))
        self.assertEqual(len(frames[-1][1]), 31)

    def test_filter_menu_applies_once_peeks_cancels_and_keeps_resize_and_quit_responsive(self):
        from cw_monitor.filtering import SnrFilter
        with patch('cw_monitor.monitor.save_filter', return_value='') as save:
            frames, state, calls = self.drive(14, {0: ord('f'), 1: ord('2'), 2: ord('0'), 3: ord('v'), 4: 10,
                                                   7: ord('f'), 8: ord('o'), 9: 27, 10: 'resize'})
        self.assertEqual(len(calls), 14)
        self.assertEqual(state.snr_filter, SnrFilter(20, True))
        save.assert_called_once_with(SnrFilter(20, True))
        self.assertTrue(any('Minimum SNR filter' in text for _, frame in frames for text, _ in frame))
        self.assertTrue(any('Filtered (shown): 1' in text for text, _ in frames[-1][1]))
        self.assertEqual(len(frames[-1][1]), 31)
