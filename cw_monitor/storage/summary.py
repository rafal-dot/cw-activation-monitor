"""Validated, atomic daily activation summaries."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import re
import tempfile
from threading import Lock

from ..config import CALL_PATTERN, PROGRAMS, UTC
from ..geo import locator_coordinates
from ..models import Match
from ..values import clean, frequency, station_band, timestamp
from .files import lock_csv, upgrade_csv_schema


@dataclass
class SummaryRow:
    date: str
    program: str
    callsign: str
    reference: str
    name: str
    band: str
    first_seen: float
    last_seen: float
    frequency: float
    qth: str = ""

    @property
    def key(self) -> tuple:
        return self.date, self.program, self.callsign, self.reference, self.band

    def values(self) -> list[str]:
        return [self.date,
                datetime.fromtimestamp(self.first_seen, UTC).isoformat(),
                datetime.fromtimestamp(self.last_seen, UTC).isoformat(),
                self.program, self.callsign, self.reference, self.name, self.qth, self.band, str(self.frequency)]


class DailySummary:
    """One persistent row per source UTC day and confirmed activation identity."""

    fields = ("date_utc", "first_seen_utc", "last_seen_utc", "program", "callsign", "reference", "name", "qth", "band",
              "last_frequency_khz")

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.rows: dict[tuple, SummaryRow] = {}
        self.error, self.dirty, self.closed = "", False, False
        self.lock = Lock()
        self.lock_file = lock_csv(self.path, "Daily summary")
        try:
            if self.path.exists() and self.path.stat().st_size:
                self._load()
                upgrade_csv_schema(self.path, self.fields)
            else:
                self._write()
        except BaseException:
            self.lock_file.close()
            raise

    def _merge(self, incoming: SummaryRow) -> None:
        current = self.rows.get(incoming.key)
        if current is None:
            self.rows[incoming.key] = incoming
            self.dirty = True
            return
        old = current.values()
        if incoming.name and (not current.name or incoming.last_seen >= current.last_seen):
            current.name = incoming.name
        if incoming.qth and (not current.qth or incoming.last_seen >= current.last_seen):
            current.qth = incoming.qth
        if incoming.last_seen >= current.last_seen:
            current.frequency = incoming.frequency
        current.first_seen = min(current.first_seen, incoming.first_seen)
        current.last_seen = max(current.last_seen, incoming.last_seen)
        self.dirty |= current.values() != old

    def _load(self) -> None:
        duplicate = False
        try:
            with self.path.open(encoding="utf-8-sig", newline="") as source:
                reader = csv.DictReader(source, strict=True)
                if tuple(reader.fieldnames or ()) not in (self.fields, tuple(f for f in self.fields if f != "qth")):
                    raise ValueError("different CSV header")
                for raw in reader:
                    if None in raw or any(value is None for value in raw.values()):
                        raise ValueError(f"wrong column count at line {reader.line_num}")
                    first, last = timestamp(raw["first_seen_utc"]), timestamp(raw["last_seen_utc"])
                    qth = raw.get("qth", "").strip().upper()
                    if qth:
                        locator_coordinates(qth)
                    freq = frequency(raw["last_frequency_khz"])
                    pattern = r"[A-Z0-9]+/[A-Z0-9]+-\d+" if raw["program"] == "SOTA" else r"[A-Z0-9]+-\d+"
                    if (raw["program"] not in PROGRAMS or not CALL_PATTERN.fullmatch(raw["callsign"])
                            or not re.fullmatch(pattern, raw["reference"])
                            or first > last
                            or any(datetime.fromtimestamp(t, UTC).date().isoformat() != raw["date_utc"] for t in (first, last))
                            or station_band(raw["callsign"], freq)[1] != raw["band"]):
                        raise ValueError(f"invalid identity, timestamps or band at line {reader.line_num}")
                    row = SummaryRow(raw["date_utc"], raw["program"], raw["callsign"], raw["reference"],
                                     " ".join(clean(raw["name"]).split()), raw["band"], first, last, freq, qth)
                    duplicate |= row.key in self.rows
                    self._merge(row)
            self.dirty = duplicate
        except (csv.Error, UnicodeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Invalid daily summary file {self.path}: {exc}") from exc

    def observe(self, match: Match, source_time: float) -> None:
        day = datetime.fromtimestamp(source_time, UTC).date().isoformat()
        with self.lock:
            if self.closed:
                return
            for reference in match.references:
                name = " ".join(clean(match.reference_names.get(reference, "")).split())
                self._merge(SummaryRow(day, match.program, match.call, reference, name,
                                       station_band(match.call, match.frequency)[1], source_time,
                                       source_time, match.frequency, match.reference_qths.get(reference, "")))

    def _write(self) -> None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=self.path.parent,
                                             prefix="." + self.path.name + ".", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                writer = csv.writer(output)
                writer.writerow(self.fields)
                for key in sorted(self.rows):
                    writer.writerow(self.rows[key].values())
                output.flush()
                os.fsync(output.fileno())
            if self.path.exists():
                os.chmod(temporary, self.path.stat().st_mode & 0o777)
            os.replace(temporary, self.path)
            self.dirty, self.error = False, ""
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _flush_locked(self) -> None:
        if self.dirty and not self.closed:
            try:
                self._write()
            except OSError as exc:
                self.error = clean(exc)

    def flush(self) -> None:
        with self.lock:
            self._flush_locked()

    def close(self) -> None:
        with self.lock:
            if not self.closed:
                self._flush_locked()
                self.closed = True
                self.lock_file.close()
