"""Append-only activation and diagnostic CSV events."""

from __future__ import annotations

import csv
from contextlib import ExitStack
from datetime import datetime
import os
from pathlib import Path
from threading import Lock

from ..config import UTC
from ..models import Match
from ..values import band_for, clean
from .files import lock_csv, upgrade_csv_schema


class EventLog:
    fields = ("timestamp_utc", "source_time_utc", "event", "program", "callsign",
              "reference", "qth", "band", "spot_khz", "rbn_khz", "spotters", "snr_db", "wpm", "detail")

    def __init__(self, path: Path, level: str):
        self.path = path = path.resolve()
        self.level, self.error, self.lock, self.closed = level, "", Lock(), False
        with ExitStack() as resources:
            self.lock_file = resources.enter_context(lock_csv(path, "Event log"))
            upgrade_csv_schema(path, self.fields)
            needs_newline = False
            if path.exists() and path.stat().st_size:
                with path.open("rb") as source:
                    source.seek(-1, os.SEEK_END)
                    needs_newline = source.read(1) not in (b"\n", b"\r")
            self.file = resources.enter_context(path.open("a", encoding="utf-8", newline=""))
            self.writer = csv.writer(self.file)
            if self.file.tell() == 0:
                self.writer.writerow(self.fields)
                self.file.flush()
            self.needs_newline = needs_newline
            resources.pop_all()

    def records(self):
        """Read saved events before the polling thread starts."""
        with self.lock, self.path.open(encoding="utf-8-sig", newline="") as source:
            try:
                yield from csv.DictReader(source, strict=True)
            except (csv.Error, UnicodeError) as exc:
                raise ValueError(f"Cannot restore event log {self.path}: {exc}") from exc

    @staticmethod
    def utc(when: float) -> str:
        return datetime.fromtimestamp(when, UTC).isoformat(timespec="seconds")

    def record(self, event: str, now: float, match: Match | None = None,
               detail: str = "", spot_time: float | None = None, debug: bool = False) -> None:
        if debug and self.level != "debug":
            return
        values = {"timestamp_utc": self.utc(now), "event": event, "detail": clean(detail)}
        if match is not None:
            values.update(source_time_utc=self.utc(spot_time) if spot_time is not None else "",
                          program=match.program, callsign=match.call, reference="/".join(match.references),
                          qth="/".join(match.reference_qths.get(p, "") for p in match.references),
                          band=band_for(match.frequency), spot_khz=f"{match.frequency:.1f}",
                          rbn_khz="/".join(f"{r.frequency:.1f}" for r in match.receptions),
                          spotters="/".join(r.receiver for r in match.receptions),
                          snr_db="/".join(str(r.snr) for r in match.receptions),
                          wpm="/".join(str(r.wpm) for r in match.receptions))
        with self.lock:
            if self.closed or self.error:
                return
            try:
                if self.needs_newline:
                    self.file.write("\n")
                    self.needs_newline = False
                self.writer.writerow([values.get(name, "") for name in self.fields])
                self.file.flush()
            except OSError as exc:
                self.error = clean(exc)

    def close(self) -> None:
        with self.lock:
            if not self.closed:
                self.closed = True
                try:
                    self.file.close()
                except OSError as exc:
                    self.error = clean(exc)
                finally:
                    self.lock_file.close()
