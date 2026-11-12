"""Share optional log writers within one monitor session."""

from __future__ import annotations

import argparse

from .events import EventLog
from .summary import DailySummary
from ..values import clean


def get_log(args: argparse.Namespace) -> EventLog | None:
    if not hasattr(args, "_event_log"):
        if args.summary_file is not None:
            get_summary(args)  # Acquire the summary lock before upgrading either log.
        args._event_log = EventLog(args.log_file, args.log_level) if args.log_file is not None else None
    return args._event_log


def get_summary(args: argparse.Namespace) -> DailySummary | None:
    if not hasattr(args, "_daily_summary"):
        args._daily_summary = DailySummary(args.summary_file) if args.summary_file is not None else None
    return args._daily_summary


def close_logs(args: argparse.Namespace | None) -> list[str]:
    errors = []
    for label, name in (("SUMMARY", "_daily_summary"), ("LOG", "_event_log")):
        writer = getattr(args, name, None)
        if writer is None:
            continue
        try:
            writer.close()
        except OSError as exc:
            errors.append(f"{label} ERROR: {clean(exc)}")
        else:
            if writer.error:
                errors.append(f"{label} ERROR: {writer.error}")
    return errors
