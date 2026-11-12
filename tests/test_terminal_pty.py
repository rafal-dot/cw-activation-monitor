"""Sprawdzenie prawdziwego curses w pseudoterminalu Linuksa, bez internetu."""

import fcntl
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import termios
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PtyTests(unittest.TestCase):
    def test_no_repeated_clear_resize_and_quit_during_slow_fetch(self):
        program = """
import time
from cw_monitor import cli, models, polling, runtime
args = cli.arguments([])
args.interval = 0.6
counter = 0
def fetch(self):
    global counter
    counter += 1
    if counter > 3:
        time.sleep(10)
    now = time.time()
    freq = 14052.9 if counter == 1 else 14053.1 if counter == 2 else 7028.0
    heard = [] if counter == 2 else [models.RbnSpot('IU1PZC', 'SP5GQ', freq, now - 42, 18, 23)]
    warnings = ['BATCHES ACK'] if self.rbn.small_batches.is_set() else []
    return models.Snapshot([models.ActivationSpot('IU1PZC', 'IT-0759', freq, now)], heard, warnings, now)
polling.Poller.fetch = fetch
raise SystemExit(runtime.run(args))
"""
        master, slave = pty.openpty()
        original_flags = termios.tcgetattr(slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
        environment = dict(os.environ, TERM="xterm-256color", PYTHONIOENCODING="utf-8")
        process = subprocess.Popen([sys.executable, "-c", program], cwd=ROOT, env=environment,
                                   stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        output = bytearray()

        def drain(duration):
            until = time.monotonic() + duration
            while time.monotonic() < until:
                if select.select([master], [], [], min(0.05, max(0, until - time.monotonic())))[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output.extend(chunk)

        try:
            drain(0.35)
            os.write(master, b"b")
            drain(1.75)  # Three snapshots; the fourth fetch is slow.
            text = output.decode("utf-8", errors="replace")
            self.assertIn("IU1PZC", text)
            self.assertIn("ACTIVE", text)
            self.assertIn("Comment", text)
            self.assertIn("BATCHES ACK", text)
            self.assertIn("ACTIVE", text)
            self.assertIn("UPD", text)
            self.assertNotIn("^UPDATED:", text)
            self.assertIn("NEW BAND:", text)
            self.assertLessEqual(text.count("\x1b[2J"), 1)
            before_acknowledgement = len(output)
            os.write(master, b" ")
            drain(0.3)
            acknowledgement = output[before_acknowledgement:].decode("utf-8", errors="replace")
            self.assertIn("NEW:", acknowledgement)
            self.assertNotIn("\x1b[2J", acknowledgement)
            # Zmiana rozmiaru nie może zgubić obsługi klawiatury ani wywołać błędu curses.
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 8, 40, 0, 0))
            import signal
            process.send_signal(signal.SIGWINCH)
            drain(0.25)
            before_quit = time.monotonic()
            os.write(master, b"q")
            process.wait(timeout=2)
            drain(0.1)
            self.assertLess(time.monotonic() - before_quit, 2)
            self.assertEqual(process.returncode, 0, output.decode("utf-8", errors="replace"))
            self.assertNotIn(b"Traceback", output)
            self.assertEqual(termios.tcgetattr(slave), original_flags)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            os.close(master)
            os.close(slave)


if __name__ == "__main__":
    unittest.main()
