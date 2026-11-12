"""Run the monitor in plain or interactive terminal mode."""

from __future__ import annotations

import argparse
import math
import os
from queue import Empty, Queue
import sys
from threading import Event, Thread
import time

from .models import ActivationSpot, RbnSpot, Snapshot, SourceError
from .monitor import Monitor
from .polling import Poller
from .ui.layout import ColumnLayout
from .ui.colors import colors_enabled
from .ui.views import render, screen_frame
from .values import clean


def demo_data(now: float, receivers: list[str], programs: list[str] | None = None) -> tuple[list[ActivationSpot], list[RbnSpot]]:
    pota = [ActivationSpot("IU1PZC", "IT-0759", 14052.9, now - 80, "Parco Naturale del Monte Tre Denti", (45.0, 7.3)),
            ActivationSpot("YO5BDL", "RO-0155", 14054.0, now - 120, "Simion Bărnuțiu Central Park", (46.77, 23.58))]
    rbn = [RbnSpot("IU1PZC", receivers[0], 14052.9, now - 42, 18, 23),
           RbnSpot("YO5BDL", receivers[0], 14054.0, now - 35, 12, 21)]
    if len(receivers) > 1:
        rbn.append(RbnSpot("YO5BDL", receivers[1], 14054.1, now - 58, 9, 21))
    if programs is not None:
        pota.extend([ActivationSpot("SP9DEMO/P", "SP/BZ-001", 7032, now - 120, "Demo summit", (49.57, 19.53), "SOTA"),
                     ActivationSpot("SP8DEMO/P", "SPFF-0001", 14044, now - 480, "Demo nature reserve", (50.2, 23.1), "WWFF")])
        rbn.extend([RbnSpot("SP9DEMO/P", receivers[0], 7032, now - 50, 14, 22),
                    RbnSpot("SP8DEMO/P", receivers[0], 14044, now - 40, 16, 24)])
        pota = [p for p in pota if p.program in programs]
    return pota, rbn


def run_plain(args: argparse.Namespace) -> int:
    poller, state = Poller(args), Monitor(args)
    color = colors_enabled(args.color)
    warning = state.qth_warning()
    if state.restore_count or state.restore_skipped:
        print(render(state, time.time(), color=color), flush=True)
        state.qth_warning_until = 0
    elif warning:
        print(warning, flush=True)  # Show before the first network request.
        state.qth_warning_until = 0  # Append-only output needs only one startup notice.
    interactive = sys.stdout.isatty()
    while True:
        started = time.monotonic()
        retry_after, failed = 0.0, False
        try:
            if args.demo:
                now = time.time()
                old = now - 600
                if "POTA" in args.programs:
                    state.apply(Snapshot([ActivationSpot("SP9DEMO/P", "PL-0001", 7028, old)],
                                         [RbnSpot("SP9DEMO/P", args.spotters[0], 7028, old, 15, 20)], [], old), old)
                pota, rbn = demo_data(now, args.spotters, args.programs)
                snapshot = Snapshot(pota, rbn, [], now)
            else:
                snapshot = poller.poll(started)
            now = time.time()
            new = state.apply(snapshot, now)
            failed = bool(snapshot.errors)
            if new and args.bell and interactive:
                print("\a", end="", flush=True)
        except SourceError as exc:
            failed, retry_after = True, exc.retry_after
            state.fail(str(exc))
        print(render(state, time.time(), color=color), flush=True)
        if args.once or args.demo:
            return 1 if failed else 0
        finished = time.monotonic()
        delay = max(0, poller.next_poll_at(started, finished) - finished, retry_after)
        print(f"\nNext cycle in {math.ceil(delay)} s. Exit: Ctrl+C.\n", flush=True)
        time.sleep(delay)


def poll_worker(args: argparse.Namespace, events: Queue, stop: Event, small_batches: Event | None = None) -> None:
    """Run network polling in one worker; keep terminal I/O on the main thread."""
    poller = Poller(args, small_batches)
    while not stop.is_set():
        started = time.monotonic()
        retry_after = 0.0
        events.put(("loading", None))
        try:
            events.put(("snapshot", poller.poll(started)))
        except SourceError as exc:
            retry_after = exc.retry_after
            events.put(("error", str(exc)))
        except Exception as exc:
            events.put(("error", f"Fetch error ({type(exc).__name__}): {clean(exc)}"))
        finished = time.monotonic()
        delay = max(0, poller.next_poll_at(started, finished) - finished, retry_after)
        events.put(("scheduled", finished + delay))
        if stop.wait(delay):
            return


def run_terminal(screen, args: argparse.Namespace) -> int:
    from .ui.terminal import CursesDisplay

    display, state = CursesDisplay(screen, colors=colors_enabled(args.color)), Monitor(args)
    layout = ColumnLayout()
    events, stop, small_batches = Queue(), Event(), Event()
    worker = Thread(target=poll_worker, args=(args, events, stop, small_batches), daemon=True)
    worker.start()
    next_frame_at, last_size = 0.0, None
    try:
        while True:
            now = time.time()
            changed = False
            while True:
                try:
                    kind, data = events.get_nowait()
                except Empty:
                    break
                changed = True
                if kind == "loading":
                    state.loading = True
                    state.next_poll_at = None
                elif kind == "error":
                    state.fail(data)
                elif kind == "scheduled":
                    state.next_poll_at = data
                elif kind == "snapshot":
                    new = state.apply(data, now)
                    if new and args.bell:
                        display.bell()
            height, width = screen.getmaxyx()
            tick = time.monotonic()
            if changed or (height, width) != last_size or tick >= next_frame_at:
                display.draw(screen_frame(state, now, height, width, layout))
                next_frame_at, last_size = tick + 1, (height, width)
            key = screen.getch()  # The input timeout keeps timers responsive.
            if key == ord(" "):
                state.acknowledge(time.time())
                next_frame_at = 0
            if key in (ord("b"), ord("B")) and not small_batches.is_set():
                small_batches.set()
                state.batch_mode = True
                next_frame_at = 0
                if state.log:
                    state.log.record("RBN_BATCHES_ENABLED", time.time(), detail="keyboard acknowledgement")
            if key in (ord("q"), ord("Q"), 3):
                return 0
    finally:
        # Do not wait for an in-flight HTTPS request when quitting.
        stop.set()


def run(args: argparse.Namespace) -> int:
    fullscreen = (not (args.once or args.demo or args.no_clear)
                  and sys.stdout.isatty() and sys.stdin.isatty()
                  and os.environ.get("TERM", "dumb") != "dumb")
    if fullscreen:
        try:
            import curses
        except ImportError:
            print("curses is unavailable; starting plain text mode (--no-clear).", file=sys.stderr)
        else:
            return curses.wrapper(run_terminal, args)
    return run_plain(args)
