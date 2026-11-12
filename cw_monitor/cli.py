"""CW activations from POTA, WWFF and optional SOTA files, confirmed by RBN."""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import sys

from .config import CALL_PATTERN, PROGRAMS
from .geo import qth_coordinates
from .runtime import run
from .storage.session import close_logs, get_log, get_summary
from .values import clean, receiver_call


def thresholds(value: str) -> tuple[int, int]:
    try:
        low, high = (int(part.strip()) for part in value.split(","))
        if low >= high:
            raise ValueError
        return low, high
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected two increasing integers, e.g. 15,24") from exc


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spotters", nargs="+", default=["SP5GQ", "SQ5J"],
                        help="receiver callsigns (default: SP5GQ SQ5J)")
    parser.add_argument("--programs", nargs="+", type=str.upper, choices=PROGRAMS,
                        help="programs to monitor (default: POTA WWFF, plus SOTA with --sota-file)")
    parser.add_argument("--sota-file", type=Path,
                        help="local SOTA spots JSON file; no SOTA API connection is made")
    parser.add_argument("--qth", default=os.environ.get("CW_ACTIVATION_QTH"),
                        help="home Maidenhead locator or latitude,longitude (also CW_ACTIVATION_QTH); unset disables distance")
    parser.add_argument("--distance-unit", choices=("km", "mi"), default="km",
                        help="distance units (default: km)")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                        help="colours: auto for interactive colour terminals, always, or never (default: auto)")
    parser.add_argument("--snr-thresholds", type=thresholds, default=(10, 20), metavar="LOW,HIGH",
                        help="SNR dB: below LOW red, below HIGH yellow, otherwise green (default: 10,20)")
    parser.add_argument("--wpm-thresholds", type=thresholds, default=(15, 24), metavar="SLOW,MEDIUM",
                        help="WPM: up to SLOW green, up to MEDIUM yellow, otherwise red (default: 15,24)")
    parser.add_argument("--interval", type=float, default=60,
                        help="source polling interval in seconds, minimum 60; RBN can poll faster when busy")
    parser.add_argument("--pota-max-age", type=float, default=300, help="maximum POTA spot age in seconds")
    parser.add_argument("--sota-max-age", type=float, default=600, help="maximum SOTA spot age in seconds (600)")
    parser.add_argument("--wwff-max-age", type=float, default=900, help="maximum WWFF spot age in seconds (900)")
    parser.add_argument("--rbn-max-age", type=float, default=300,
                        help="maximum age of the initial RBN confirmation in seconds")
    parser.add_argument("--history-max-age", type=float, default=1800,
                        help="history retention after program spot activity ends, in seconds (1800)")
    parser.add_argument("--new-max-age", type=float, default=300,
                        help="time a new program/callsign/band/reference stays in ACTIVE / NEW, in seconds (300)")
    parser.add_argument("--highlight-seconds", type=float, default=30,
                        help="new row highlight duration in seconds (30)")
    parser.add_argument("--reference-name-length", "--park-name-length", dest="park_name_length", type=int, default=None,
                        help="reference name limit; full name by default, 0 hides names (legacy alias: --park-name-length)")
    parser.add_argument("--tolerance", type=float, default=0.5, help="frequency tolerance in kHz (0.5)")
    parser.add_argument("--timeout", type=float, default=20, help="timeout for each HTTPS request in seconds")
    parser.add_argument("--rbn-small-batches", action="store_true",
                        help="allow targeted callsign/receiver batches if the RBN stream exceeds 100 rows")
    parser.add_argument("--log-file", type=Path, help="append activation events to a UTF-8 CSV file")
    parser.add_argument("--summary-file", type=Path,
                        help="update a daily CSV with full names, first/last source spots and last frequency; reload on restart")
    parser.add_argument("--log-level", choices=("events", "debug"), default="events",
                        help="CSV log detail (default: events); debug also records fetches and rejected matches")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--demo", action="store_true", help="show fictional data once, without network access")
    parser.add_argument("--links", action="store_true", help="show RBN and program reference links")
    parser.add_argument("--bell", action="store_true", help="ring the terminal bell for new confirmations")
    parser.add_argument("--no-clear", action="store_true", help="plain text: keep successive snapshots instead of using curses")
    args = parser.parse_args(argv)
    if args.wpm_thresholds[0] < 1:
        parser.error("--wpm-thresholds: expected positive increasing integers")
    if args.log_file is not None and args.summary_file is not None:
        same_path = args.log_file.resolve() == args.summary_file.resolve()
        if args.log_file.exists() and args.summary_file.exists():
            same_path |= os.path.samefile(args.log_file, args.summary_file)
        if same_path:
            parser.error("--log-file and --summary-file must be different files")
    args.programs = list(dict.fromkeys(args.programs or (list(PROGRAMS) if args.demo else
                                    ["POTA", "WWFF"] + (["SOTA"] if args.sota_file else []))))
    if "SOTA" in args.programs and args.sota_file is None and not args.demo:
        parser.error("--programs SOTA requires --sota-file; direct SOTA API access is not enabled")
    args.qth_label = clean(args.qth).strip() if args.qth is not None else "not set"
    if args.qth is not None:
        try:
            args.qth = qth_coordinates(args.qth_label)
        except ValueError as exc:
            parser.error(f"--qth: {exc}")
    for name in ("interval", "pota_max_age", "sota_max_age", "wwff_max_age", "rbn_max_age", "history_max_age", "new_max_age", "highlight_seconds", "timeout", "tolerance"):
        value = getattr(args, name)
        if not math.isfinite(value) or value < 0 or (name != "tolerance" and value == 0):
            parser.error(f"--{name.replace('_', '-')}: expected a finite positive number")
    if args.interval < 60:
        parser.error("--interval must be at least 60 seconds")
    if args.park_name_length is not None and args.park_name_length < 0:
        parser.error("--reference-name-length / --park-name-length must not be negative")
    args.spotters = list(dict.fromkeys(receiver_call(r) for r in args.spotters))
    if not all(CALL_PATTERN.fullmatch(r) for r in args.spotters):
        parser.error("--spotters: invalid receiver callsign")
    return args


def main(argv: list[str] | None = None) -> int:
    args = None
    result = 0
    try:
        args = arguments(argv)
        get_log(args)  # Fail clearly before networking if the log cannot be opened.
        get_summary(args)
        result = run(args)
    except BrokenPipeError:
        result = 0
    except (OSError, ValueError) as exc:
        print("Configuration/file error: " + clean(exc), file=sys.stderr)
        result = 2
    except KeyboardInterrupt:
        print("\nMonitor stopped.", file=sys.stderr)
    finally:
        errors = close_logs(args)
        for message in errors:
            print(message, file=sys.stderr)
        if errors:
            result = 2
    return result
