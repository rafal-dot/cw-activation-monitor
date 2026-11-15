"""Friends CSV interoperability, independent RBN matching and restart-safe alerts."""

import contextlib
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

from cw_monitor import cli, models, monitor, polling
from cw_monitor.friends import Friend, FriendTracker
from cw_monitor.storage.friends import decode_friends, encode_friends, FriendHistory
from cw_monitor.storage.session import close_logs
from cw_monitor.friend_audio import FriendAudio, samples, timing, CODES
from cw_monitor.ui.views import render, screen_frame
from test_pota_alert import NOW


class FriendsTests(unittest.TestCase):
    def setUp(self):
        self.args = cli.arguments([])
        self.friend = Friend("G4YBU/P", "Łukasz", "Test", "QRP, friend")
        self.args.friends, self.args.friends_alerts = [self.friend], True

    def reading(self, stamp=NOW, freq=14044, snr=18, receiver="SP5GQ"):
        return models.RbnSpot(self.friend.call, receiver, freq, stamp, snr, 20)

    def test_csv_roundtrip_bom_quotes_multiline_unicode_and_comment_limit(self):
        rows = [replace(self.friend, comment='Park, "north"\n🙂')]
        encoded = encode_friends(rows)
        self.assertTrue(encoded.startswith("callsign,first_name,last_name,comment\r\n"))
        self.assertEqual(decode_friends("\ufeff" + encoded), rows)
        self.assertEqual(Friend("SP5RFL", comment="🙂" * 128).validate().comment, "🙂" * 128)
        with self.assertRaises(ValueError):
            Friend("SP5RFL", comment="🙂" * 129).validate()
        for text in ("bad,header", "callsign,first_name,last_name,comment\nBAD CALL,a,b,c",
                     "callsign,first_name,last_name,comment\nSP5RFL,a,b,c\nsp5rfl,c,d,e"):
            with self.assertRaises(ValueError):
                decode_friends(text)

    def test_fresh_receptions_deduplicate_repeat_pages_spotters_and_restore_but_new_band_and_gap_alert(self):
        tracker = FriendTracker()
        self.assertEqual(len(tracker.observe([self.reading()], NOW, self.args)), 1)
        self.assertEqual(tracker.observe([self.reading()], NOW + 15, self.args), [])
        self.assertEqual(tracker.observe([self.reading(NOW + 30, receiver="SQ5J")], NOW + 30, self.args), [])
        self.assertEqual(len(tracker.observe([self.reading(NOW + 40, freq=7032)], NOW + 40, self.args)), 1)
        reopened = FriendTracker(tracker.markers())
        self.assertEqual(reopened.observe([self.reading(NOW + 30)], NOW + 45, self.args), [])
        self.assertTrue(reopened.active(NOW + 45, self.args))
        self.assertEqual(len(reopened.observe([self.reading(NOW + 331)], NOW + 331, self.args)), 1)

    def test_filters_apply_to_one_spotter_and_first_qualifying_report_alerts(self):
        self.args.min_snr, self.args.max_snr = 15, 25
        tracker = FriendTracker()
        self.assertEqual(tracker.observe([self.reading(snr=10), self.reading(snr=30, receiver="SQ5J")], NOW, self.args), [])
        self.assertEqual(len(tracker.observe([self.reading(NOW + 1, snr=20)], NOW + 1, self.args)), 1)
        self.args.bands = frozenset({"40m"})
        self.assertEqual(tracker.active(NOW + 1, self.args), [])
        self.args.friends_alerts = False
        self.assertEqual(tracker.observe([self.reading(NOW + 400)], NOW + 400, self.args), [])

    def test_exact_full_call_required_and_invalid_or_unselected_spotter_is_ignored(self):
        tracker = FriendTracker()
        rows = [replace(self.reading(), call="G4YBU"), self.reading(receiver="OTHER1"), self.reading(NOW + 10)]
        self.assertEqual(tracker.observe(rows, NOW, self.args), [])

    def test_friends_query_shared_stream_even_when_all_program_feeds_are_empty(self):
        client = polling.Poller(self.args)
        with patch.object(client, "read_source", return_value=([], 0, set())), patch.object(time, "time", return_value=NOW), \
             patch.object(client.rbn, "fetch", return_value=[self.reading()]) as fetch:
            snapshot = client.fetch()
            self.assertEqual(fetch.call_args.args[0], ["G4YBU/P"])
            self.assertEqual(snapshot.spots, [])
            self.assertEqual(snapshot.friend_receptions, [self.reading()])
            self.args.friends_alerts = False
            snapshot = client.fetch()
            self.assertEqual(fetch.call_args.args[0], [])
            self.assertEqual(snapshot.friend_receptions, [])

    def test_history_file_roundtrip_lock_and_restart_keep_readings_visible_without_repeating_alert(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "friends.csv"
            path.write_text(encode_friends([self.friend]), encoding="utf-8")
            args = cli.arguments(["--friends-file", str(path)])
            try:
                with patch.object(time, "time", return_value=NOW):
                    state = monitor.Monitor(args)
                    state.apply(models.Snapshot([], [], [], NOW, friend_receptions=[self.reading()]), NOW)
                    self.assertEqual(len(state.new_friends), 1)
                    frame = render(state, NOW)
                    self.assertIn("FRND (1)", frame)
                    self.assertIn("G4YBU/P", frame)
                    self.assertIn("Łukasz Test", frame)
                    self.assertIn("FRND NEW:", "\n".join(text for text, _ in screen_frame(state, NOW, 30, 120)))
                    with self.assertRaises(ValueError):
                        FriendHistory(args.friends_state_file)
            finally:
                close_logs(args)
            reopened = cli.arguments(["--friends-file", str(path)])
            try:
                with patch.object(time, "time", return_value=NOW + 10):
                    restored = monitor.Monitor(reopened)
                    restored.apply(models.Snapshot([], [], [], NOW, friend_receptions=[self.reading()]), NOW + 10)
                    self.assertEqual(restored.new_friends, [])
                    self.assertEqual(len(restored.friends.active(NOW + 10, reopened)), 1)
            finally:
                close_logs(reopened)

    def test_invalid_csv_and_input_output_aliases_fail_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "friends.csv"
            path.write_text(encode_friends([self.friend]), encoding="utf-8")
            original = path.read_bytes()
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.arguments(["--friends-file", str(path), "--friends-state-file", str(path)])
            self.assertEqual(original, path.read_bytes())
            self.assertFalse(cli.arguments(["--friends-file", str(path), "--no-friends-alerts"]).friends_alerts)
            path.write_text("broken", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.arguments(["--friends-file", str(path)])

    def test_new_friend_priority_red_notice_expiry_and_restart_are_independent_of_old_friends(self):
        from cw_monitor.ui.views import friend_banner
        args = cli.arguments(["--programs", "POTA", "--qth", "KO02KF"])
        args.friends, args.friends_alerts = [Friend("NEWFRND"), Friend("OLDFRND")], True
        state = monitor.Monitor(args)
        old = models.RbnSpot("OLDFRND", "SP5GQ", 7032, NOW - 120, 18, 20)
        state.apply(models.Snapshot([], [], [], NOW - 120, friend_receptions=[old]), NOW - 120)
        fresh = models.RbnSpot("NEWFRND", "SP5GQ", 14044, NOW, 18, 20)
        old = replace(old, time=NOW)
        spot = models.ActivationSpot("POTANEW", "PL-0001", 14044, NOW)
        program = models.RbnSpot("POTANEW", "SP5GQ", 14044, NOW, 18, 20)
        state.apply(models.Snapshot([spot], [program], [], NOW, friend_receptions=[fresh, old]), NOW)
        frame = screen_frame(state, NOW, 40, 180)
        fresh_index = next(i for i, (text, _) in enumerate(frame) if "FRND NEW: NEWFRND" in text)
        program_index = next(i for i, (text, _) in enumerate(frame) if text.strip().startswith("POTA") and "POTANEW" in text)
        old_index = next(i for i, (text, _) in enumerate(frame) if "FRND: OLDFRND" in text)
        self.assertLess(fresh_index, program_index); self.assertLess(program_index, old_index)
        self.assertEqual(frame[1][1], "friend_banner")
        self.assertIn("\x1b[1;31m", render(state, NOW, color=True))
        restored = FriendTracker(state.friends.markers())
        self.assertEqual(restored.observe([fresh, old], NOW + 1, args), [])
        self.assertTrue(all(row.new_until == 0 for row in restored.active(NOW + 1, args)))
        state.acknowledge(NOW + 1)
        self.assertEqual(friend_banner(state, NOW + 1), "")

    def test_morse_worker_queues_cq_calls_once_and_never_adds_program_name(self):
        process = Mock()
        process.returncode = 0
        process.communicate.return_value = (b"", b"")
        with patch("shutil.which", return_value="/usr/bin/aplay"), patch("subprocess.Popen", return_value=process) as play, \
             patch("cw_monitor.friend_audio.samples", return_value=b"\0\0") as pcm:
            audio = FriendAudio(20, 750)
            try:
                self.assertEqual(audio.submit(["G4YBU/P", "G4YBU/P", "SP5TEST"]), "")
            finally:
                audio.close(wait=True)
            self.assertEqual([call.args[0] for call in pcm.call_args_list], ["CQ G4YBU/P", "CQ SP5TEST"])
            self.assertEqual(play.call_count, 2)
            self.assertTrue(all("shell" not in call.kwargs for call in play.call_args_list))

    def test_pcm_paris_timing_and_missing_player_have_no_extra_python_dependencies(self):
        self.assertEqual(len(CODES), 38)
        self.assertEqual(timing("E E"), [(True, 1), (False, 7), (True, 1)])
        self.assertEqual(len(samples("E", 20, 750)), 2 * 960)
        self.assertEqual(len(samples("CQ G4YBU/P", 20, 750)) % 2, 0)
        with patch("shutil.which", return_value=None), patch("subprocess.Popen") as process:
            audio = FriendAudio(20, 750)
            try:
                self.assertIn("unavailable", audio.submit(["SP5RFL"]))
            finally:
                audio.close()
            process.assert_not_called()
