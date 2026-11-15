"""Source scheduling and shared RBN confirmation requests."""

from __future__ import annotations

import argparse
from pathlib import Path
from queue import Queue
from threading import Event, Thread
import time

from .friends import FriendIndex
from .config import MAX_RESPONSE_BYTES, POTA_URL, WWFF_URL
from .models import ActivationSpot, Match, RbnSpot, Snapshot, SourceError
from .restore import startup_entries
from .snr import resolve_snr
from .values import band_for
from .sources import http
from .sources.programs import read_pota, read_program
from .sources.rbn import RbnClient
from .storage.session import get_log
from .values import clean, max_spot_age, station_band


class Poller:
    """Independent sources and shared RBN requests, with confirmations per identity."""

    def __init__(self, args: argparse.Namespace, small_batches: Event | None = None):
        self.args = args
        self.log = get_log(args)
        self.rbn = RbnClient(args.timeout, small_batches, args.interval)
        if args.rbn_small_batches:
            self.rbn.small_batches.set()
        self.confirmed = {key: list(entry.match.receptions)
                          for key, entry in startup_entries(args)[0].items() if entry.match.receptions}
        self.source_spots: dict[str, list[ActivationSpot]] = {}
        self.server_retry_at: dict[str, float] = {}
        self.recent_spots: dict[tuple, ActivationSpot] = {}
        self.retry_at: dict[str, float] = {}
        self.failure_counts: dict[str, int] = {}
        self.source_errors: dict[str, str] = {}
        self.next_source_at: float | None = None
        self.cached_failed: set[str] = set()
        self.cached_warnings: list[str] = []
        self.cached_inactive: set[tuple] = set()

    def backoff(self, service: str, error: Exception, base: float) -> None:
        if service == "SOTA":
            return  # Local file repair does not create service traffic.
        count = min(8, self.failure_counts.get(service, 0) + 1)
        self.failure_counts[service] = count
        delay = max(min(900, base * 2 ** (count - 1)), getattr(error, "retry_after", 0))
        self.server_retry_at[service] = time.monotonic() + max(0, getattr(error, "retry_after", 0))
        self.retry_at[service] = time.monotonic() + delay

    def poll(self, started: float, force: bool = False) -> Snapshot:
        if force or self.next_source_at is None or started >= self.next_source_at:
            self.next_source_at = started + self.args.interval
            return self.fetch(force=True) if force else self.fetch()
        return self.fetch(refresh_sources=False)

    def next_poll_at(self, started: float, finished: float) -> float:
        rbn_due = max(started + self.rbn.poll_interval, self.retry_at.get("RBN", 0))
        source_due = self.next_source_at if self.next_source_at is not None else started + self.args.interval
        return max(finished, min(rbn_due, source_due))

    def read_source(self, program: str) -> tuple[list[ActivationSpot], int, set[tuple]]:
        if program == "SOTA":
            try:
                with Path(self.args.sota_file).open("rb") as source:
                    data = source.read(MAX_RESPONSE_BYTES + 1)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise ValueError("file exceeds 8 MB")
                payload = http.strict_json(data)
            except (OSError, ValueError, UnicodeError, RecursionError) as exc:
                raise SourceError(f"SOTA file: {clean(exc)}") from exc
        else:
            payload = http.get_json(POTA_URL if program == "POTA" else WWFF_URL, self.args.timeout)
        now = time.time()
        age = max_spot_age(self.args, program)
        return read_pota(payload, now, age) if program == "POTA" else read_program(payload, program, now, age)

    def read_sources(self, force: bool = False) -> tuple[list[ActivationSpot], set[str], list[str], list[str], set[tuple]]:
        args = self.args
        # Daemon threads preserve immediate q/Ctrl+C exit during slow requests.
        results = Queue()
        pending_sources, failed, warnings, errors, inactive, spots = [], set(), [], [], set(), []

        def collect(program):
            try:
                results.put((program, self.read_source(program), None))
            except Exception as exc:
                results.put((program, None, exc))

        for program in args.programs:
            if time.monotonic() < (self.server_retry_at if force else self.retry_at).get(program, 0):
                failed.add(program)
                errors.append(self.source_errors[program] + (" (manual refresh deferred by Retry-After)" if force else " (retry delayed)"))
                spots.extend(self.source_spots.get(program, []))
            else:
                pending_sources.append(program)
                Thread(target=collect, args=(program,), daemon=True).start()
        for _ in pending_sources:
            program, data, exc = results.get()
            if exc is not None:
                failed.add(program)
                message = f"{program}: {clean(exc)}"
                errors.append(message)
                self.source_errors[program] = message
                if self.log:
                    self.log.record("SOURCE_READ_ERROR", time.time(), detail=message, debug=True)
                self.backoff(program, exc, args.interval)
                spots.extend(self.source_spots.get(program, []))
                continue
            selected, skipped, stopped = data
            self.source_errors.pop(program, None)
            self.failure_counts.pop(program, None)
            self.retry_at.pop(program, None)
            self.server_retry_at.pop(program, None)
            if self.log:
                self.log.record("SOURCE_READ", time.time(), detail=f"{program}: CW={len(selected)}, skipped={skipped}, inactive={len(stopped)}", debug=True)
            self.source_spots[program] = selected
            spots.extend(selected)
            inactive.update(stopped)
            if skipped:
                warnings.append(f"{program}: skipped {skipped} invalid entries")
        return spots, failed, warnings, errors, inactive

    def fetch(self, refresh_sources: bool = True, force: bool = False) -> Snapshot:
        args = self.args
        if refresh_sources:
            spots, failed, warnings, errors, inactive = self.read_sources(force=True) if force else self.read_sources()
            self.cached_failed = set(failed)
            self.cached_warnings = list(warnings)
            self.cached_inactive = set(inactive)
        else:
            spots = [p for values in self.source_spots.values() for p in values]
            failed, warnings, inactive = set(self.cached_failed), list(self.cached_warnings), set(self.cached_inactive)
            errors = [self.source_errors[program]
                      + (" (retry delayed)" if time.monotonic() < (self.server_retry_at if force else self.retry_at).get(program, 0) else "")
                      for program in args.programs if program in failed]
        latest = {}
        now = time.time()
        for p in spots:
            if not 0 <= now - p.time <= max_spot_age(args, p.program):
                continue
            key = p.key
            if key not in latest or p.time >= latest[key].time:
                latest[key] = p
        self.recent_spots.update(latest)
        self.recent_spots = {key: spot for key, spot in self.recent_spots.items()
                             if key not in inactive and spot.program in args.programs and 0 <= now - spot.time <= max_spot_age(args, spot.program)}
        if force:
            latest = {**self.recent_spots, **latest}
        friends = args.friends if args.friends_alerts else ()
        friend_index = FriendIndex(friends)
        friend_calls = {friend.call for friend in friends}
        pending = [p for key, p in latest.items() if force or key not in self.confirmed or any(value is not None for value in resolve_snr(args.min_snr, args.max_snr, args.band_snr, band_for(p.frequency))) or args.filter_spotters is not None]
        received = []
        try:
            if time.monotonic() < (self.server_retry_at if force else self.retry_at).get("RBN", 0):
                errors.append(self.source_errors["RBN"] + (" (manual refresh deferred by Retry-After)" if force else " (retry delayed)"))
            else:
                received = self.rbn.fetch(sorted({p.call for p in pending} | friend_calls), args.spotters, args.rbn_max_age, friends=friends)
                warnings.extend(self.rbn.warnings)
                self.failure_counts.pop("RBN", None)
                self.retry_at.pop("RBN", None)
                self.server_retry_at.pop("RBN", None)
        except SourceError as exc:
            self.source_errors["RBN"] = "RBN: " + clean(exc)
            self.backoff("RBN", exc, max(15, self.rbn.poll_interval))
            errors.append(self.source_errors["RBN"])
        now = time.time()
        by_call: dict[str, list[RbnSpot]] = {}
        for reception in received:
            by_call.setdefault(reception.call, []).append(reception)
        for p in pending:
            same_call = by_call.get(p.call, [])
            hits = [r for r in same_call if r.receiver in args.spotters
                    and r.time is not None and 0 <= now - r.time <= args.rbn_max_age
                    and station_band(r.call, r.frequency) == station_band(p.call, p.frequency)
                    and abs(r.frequency - p.frequency) <= args.tolerance + 1e-9]
            if hits:
                heard = {r.receiver: r for r in self.confirmed.get(p.key, [])}
                for hit in hits:
                    previous = heard.get(hit.receiver)
                    if previous is None or previous.time is None or hit.time > previous.time:
                        heard[hit.receiver] = hit
                self.confirmed[p.key] = [heard[r] for r in args.spotters if r in heard]
            elif self.log:
                same_receiver = [r for r in same_call if r.receiver in args.spotters]
                current = [r for r in same_receiver if r.time is not None and 0 <= now - r.time <= args.rbn_max_age]
                same_band = [r for r in current if station_band(r.call, r.frequency) == station_band(p.call, p.frequency)]
                if not same_call:
                    reason = "no recent CW report in selected spotter stream"
                elif not same_receiver:
                    reason = "spotter not selected"
                elif not current:
                    reason = "RBN timestamp outside allowed age"
                elif not same_band:
                    reason = "band mismatch"
                else:
                    reason = f"frequency mismatch: spot={p.frequency:g}, RBN=" + "/".join(f"{r.frequency:g}" for r in same_band)
                detail = f"{p.program} {p.call} {p.reference}: {reason}; age limit={args.rbn_max_age:g}s, tolerance={args.tolerance:g}kHz"
                self.log.record("RBN_REJECT", now, Match(p.call, (p.reference,), p.frequency, [],
                                location=p.location, program=p.program,
                                reference_qths={p.reference: p.qth} if p.qth else {}),
                                detail, p.time, debug=True)
        # Cached confirmations keep their original reception time.
        cached = {r for key in latest for r in self.confirmed.get(key, [])}
        if self.log:
            for detail in self.rbn.request_log:
                self.log.record("RBN_QUERY", now, detail=detail, debug=True)
            self.log.record("RBN_FETCH", now, detail=self.rbn.status + " | " + self.rbn.diagnostic_status + " | " + " | ".join(warnings + errors), debug=True)
        return Snapshot(list(latest.values()), list(cached), warnings, now, inactive, failed, errors, self.rbn.status, [r for r in received if friend_index.find(r.call) is not None])
