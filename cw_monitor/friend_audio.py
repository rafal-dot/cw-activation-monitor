"""Nonblocking Friends Morse audio using standard Python and a local Linux player."""

from __future__ import annotations

from array import array
import math
from queue import Queue, Empty, Full
import shutil
import subprocess
import sys
from threading import Event, Lock, Thread

CODES = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/-", (
    ".-", "-...", "-.-.", "-..", ".", "..-.", "--.", "....", "..", ".---", "-.-", ".-..", "--", "-.", "---", ".--.",
    "--.-", ".-.", "...", "-", "..-", "...-", ".--", "-..-", "-.--", "--..", "-----", ".----", "..---", "...--", "....-",
    ".....", "-....", "--...", "---..", "----.", "-..-.", "-....-")))
RATE = 16000


def timing(message: str):
    parts, gap = [], 0
    for character in message.upper():
        if character == " ":
            gap = 7
            continue
        if gap:
            parts.append((False, gap))
        for index, symbol in enumerate(CODES[character]):
            if index:
                parts.append((False, 1))
            parts.append((True, 1 if symbol == "." else 3))
        gap = 3
    return parts


def samples(message: str, wpm: int, tone: int) -> bytes:
    if not 5 <= wpm <= 60 or not 200 <= tone <= 2000 or len(message) > 67:
        raise ValueError("Invalid Friends Morse settings/message")
    dit, ramp = int(RATE * 1.2 / wpm), RATE * 3 // 1000
    pcm = array("h")
    for active, units in timing(message):
        length = units * dit
        if not active:
            pcm.extend([0] * length)
        else:
            for index in range(length):
                edge = min(index, length - index - 1, ramp) / ramp
                envelope = (1 - math.cos(math.pi * edge)) / 2
                pcm.append(int(math.sin(2 * math.pi * tone * index / RATE) * envelope * 10000))
    if sys.byteorder != "little":
        pcm.byteswap()
    return pcm.tobytes()


class FriendAudio:
    def __init__(self, wpm: int, tone: int):
        self.wpm, self.tone = wpm, tone
        player = shutil.which("paplay") or shutil.which("aplay")
        self.command = ([player, "--raw", "--format=s16le", f"--rate={RATE}", "--channels=1"] if player and player.endswith("paplay")
                        else [player, "-q", "-t", "raw", "-f", "S16_LE", "-r", str(RATE), "-c", "1"] if player else None)
        self.queue = Queue(maxsize=1000)
        self.errors = Queue()
        self.stop = Event()
        self.lock = Lock()
        self.process = None
        self.worker = Thread(target=self._play, daemon=True)
        self.worker.start()

    def submit(self, calls):
        if self.command is None:
            return "Friends Morse unavailable: install paplay or aplay; the terminal bell remains available."
        for call in dict.fromkeys(calls):
            try:
                self.queue.put_nowait("CQ " + call)
            except Full:
                return "Friends Morse queue is full; some announcements were skipped."
        return self.error()

    def error(self):
        message = ""
        while True:
            try:
                message = self.errors.get_nowait()
            except Empty:
                return message

    def _play(self):
        while not self.stop.is_set():
            try:
                message = self.queue.get(timeout=.1)
            except Empty:
                continue
            try:
                data = samples(message, self.wpm, self.tone)
                if self.stop.is_set():
                    continue
                with self.lock:
                    if self.stop.is_set():
                        continue
                    self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                    process = self.process
                _, error = process.communicate(data)
                if process.returncode and not self.stop.is_set():
                    self.errors.put("Friends Morse playback failed: " + error.decode("utf-8", errors="replace")[:300])
            except (OSError, ValueError, KeyError) as exc:
                self.errors.put(f"Friends Morse playback failed: {exc}")
            finally:
                with self.lock:
                    self.process = None
                self.queue.task_done()

    def close(self, wait=False):
        try:
            if wait:
                self.queue.join()
        finally:
            self.stop.set()
            with self.lock:
                if self.process is not None:
                    try:
                        self.process.terminate()
                    except ProcessLookupError:
                        pass
            self.worker.join(timeout=1)


def announce(state):
    if not state.args.bell or not state.new_friends:
        return False
    if not hasattr(state.args, "_friends_audio"):
        state.args._friends_audio = FriendAudio(state.args.morse_wpm, state.args.morse_tone)
    state.friends_audio_error = state.args._friends_audio.submit([row.friend.call for row in state.new_friends])
    return not state.friends_audio_error
