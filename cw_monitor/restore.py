"""Reconstruct recent history from existing CSV files."""

from __future__ import annotations

import argparse
import math
import re
import time

from .config import CALL_PATTERN, PROGRAMS
from .geo import locator_coordinates
from .models import Entry, Match, RbnSpot
from .storage.session import get_log, get_summary
from .values import band_for, frequency, max_spot_age, timestamp


def startup_entries(args: argparse.Namespace) -> tuple[dict[tuple, Entry], int]:
    """Replay event lifecycles and enrich them with the independent daily summary."""
    if hasattr(args, "_startup_entries"):
        return args._startup_entries
    entries, replayed_at, skipped = {}, {}, 0
    now = time.time()
    log, summary = get_log(args), get_summary(args)
    if not args.demo and log:
        for raw in log.records():
            try:
                if None in raw or any(value is None for value in raw.values()):
                    raise ValueError("wrong column count")
                event = raw["event"]
                if event not in {"NEW", "BAND", "UPDATE", "HISTORY", "INACTIVE", "ACTIVE", "ALERT", "FILTER_ACK"}:
                    continue
                program, call = raw["program"], raw["callsign"]
                if program not in PROGRAMS:
                    raise ValueError("invalid program")
                if program not in args.programs:
                    continue
                when, source_time = timestamp(raw["timestamp_utc"]), timestamp(raw["source_time_utc"])
                freq = frequency(raw["spot_khz"])
                if when > now or source_time > when:
                    continue
                if not CALL_PATTERN.fullmatch(call) or raw["band"] != band_for(freq):
                    raise ValueError("invalid callsign or band")
                pattern = r"[A-Z0-9]+/[A-Z0-9]+-\d+" if program == "SOTA" else r"[A-Z0-9]+-\d+"
                references = tuple(re.findall(pattern, raw["reference"]))
                if not references or "/".join(references) != raw["reference"]:
                    raise ValueError("invalid reference")
                qths = raw["qth"].split("/") if raw["qth"] else [""] * len(references)
                if len(qths) != len(references):
                    raise ValueError("mismatched reference QTH fields")
                qths = [qth.strip().upper() for qth in qths]
                for qth in qths:
                    if qth:
                        locator_coordinates(qth)
                columns = [raw[name].split("/") if raw[name] else []
                           for name in ("spotters", "rbn_khz", "snr_db", "wpm")]
                if len({len(values) for values in columns}) != 1:
                    raise ValueError("mismatched spotter fields")
                receptions = []
                for receiver, rbn_freq, snr, wpm in zip(*columns):
                    if not CALL_PATTERN.fullmatch(receiver):
                        raise ValueError("invalid spotter")
                    reception = RbnSpot(call, receiver, frequency(rbn_freq), None, int(snr), int(wpm))
                    if receiver in args.spotters:
                        receptions.append(reception)
                receptions.sort(key=lambda r: args.spotters.index(r.receiver))
                for reference, qth in zip(references, qths):
                    match = Match(call, (reference,), freq, receptions,
                                  location=locator_coordinates(qth) if qth else None, program=program,
                                  reference_qths={reference: qth} if qth else {})
                    key = match.key
                    if when < replayed_at.get(key, -math.inf):
                        continue
                    replayed_at[key] = when
                    old = entries.get(key)
                    if old is None or event in {"NEW", "BAND"}:
                        entry = Entry(match, {reference: source_time + max_spot_age(args, program)},
                                      0, 0, 0, created_at=when, archived=True, spot_time=source_time,
                                      restored=True, awaiting_source=True,
                                      first_seen_known=event in {"NEW", "BAND"})
                        entries[key] = entry
                    else:
                        entry = old
                        if source_time >= entry.spot_time:
                            entry.match, entry.spot_time = match, source_time
                            entry.spot_deadlines = {reference: source_time + max_spot_age(args, program)}
                    if event in {"ALERT", "FILTER_ACK"}:
                        entry.alerted = True
                    elif "snr-filter=pending" in raw["detail"]:
                        entry.alerted = False
                    if event == "INACTIVE":
                        entry.eligible = False
                        entry.inactive_since = min(when, max(entry.spot_deadlines.values()))
                    elif event in {"NEW", "BAND", "UPDATE", "ACTIVE", "ALERT"}:
                        entry.eligible, entry.inactive_since = True, None
            except (ValueError, TypeError, OverflowError):
                skipped += 1
    if not args.demo and summary:
        for row in sorted(summary.rows.values(), key=lambda r: r.last_seen):
            if row.program not in args.programs or row.last_seen > now:
                continue
            match = Match(row.callsign, (row.reference,), row.frequency, [],
                          {row.reference: row.name} if row.name else {},
                          location=locator_coordinates(row.qth) if row.qth else None, program=row.program,
                          reference_qths={row.reference: row.qth} if row.qth else {})
            entry = entries.get(match.key)
            if entry is None:
                entry = Entry(match, {row.reference: row.last_seen + max_spot_age(args, row.program)},
                              0, 0, 0, created_at=row.first_seen, archived=True, spot_time=row.last_seen,
                              restored=True, awaiting_source=True, first_seen_known=False)
                entries[match.key] = entry
            else:
                if row.name and row.last_seen >= entry.spot_time:
                    entry.match.reference_names[row.reference] = row.name
                elif row.name and not entry.match.reference_names.get(row.reference):
                    entry.match.reference_names[row.reference] = row.name
                if row.qth and (row.last_seen >= entry.spot_time or not entry.match.reference_qths.get(row.reference)):
                    entry.match.reference_qths[row.reference] = row.qth
                    entry.match.location = locator_coordinates(row.qth)
                if row.last_seen > entry.spot_time:
                    entry.match.frequency, entry.spot_time = row.frequency, row.last_seen
                    entry.spot_deadlines = {row.reference: row.last_seen + max_spot_age(args, row.program)}
                    entry.eligible, entry.inactive_since = True, None
    retained = {}
    for key, entry in sorted(entries.items(), key=lambda item: item[1].spot_time):
        deadline = max(entry.spot_deadlines.values())
        if deadline < now and entry.inactive_since is None:
            entry.inactive_since = deadline
        expires = entry.inactive_since if entry.inactive_since is not None else deadline
        if now - expires > args.history_max_age:
            continue
        entry.rank = len(retained) + 1
        entry.logged_active = bool(entry.eligible and entry.match.receptions and now <= deadline)
        retained[key] = entry
    args._startup_entries = retained, skipped
    return args._startup_entries
