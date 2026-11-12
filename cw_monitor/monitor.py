"""Activation lifecycle, confirmations and history state."""

from __future__ import annotations

import argparse
from dataclasses import replace
import time

from .config import PROGRAMS
from .models import ActivationSpot, ChangedField, Entry, Match, Snapshot, latest_reception, update_description
from .restore import startup_entries
from .sources.programs import join_spots
from .storage.session import get_log, get_summary
from .values import clean, max_spot_age
from .sorting import SortOrder, ordered_rows
from .storage.preferences import save_sort, save_filter
from .filtering import SnrFilter, accepts_snr


class Monitor:
    """Maintain activations, history and expiry independently of network polling."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.log = get_log(args)
        self.summary = get_summary(args)
        restored, self.restore_skipped = startup_entries(args)
        self.entries = {key: replace(entry) for key, entry in restored.items()}
        self.station_bands: dict[tuple[str, str], set[str]] = {}
        for call, band, reference, program in self.entries:
            self.station_bands.setdefault((call, program), set()).add(band)
        self.spots: list[ActivationSpot] = []
        self.updated_at: float | None = None
        self.error = ""
        self.warnings: list[str] = []
        self.loading = False
        self.next_poll_at: float | None = None  # Monotonic time, as in the worker schedule.
        self.rank = len(self.entries)
        self.rbn_status = ""
        self.batch_mode = args.rbn_small_batches
        self.qth_warning_until = time.monotonic() + 30
        self.restore_count = len(self.entries)
        self.sort = SortOrder.resolve(args.sort, args.sort_order)
        self.sort_error = getattr(args, "sort_error", "")
        self.snr_filter = SnrFilter(args.min_snr, args.show_filtered)
        self.filter_error = getattr(args, "filter_error", "")
        self._acknowledge_filter(time.time())

    def set_sort(self, order: SortOrder) -> None:
        self.sort = order
        self.sort_error = save_sort(order)

    def _acknowledge_filter(self, now: float) -> None:
        for entry in self.entries.values():
            if not entry.alerted and accepts_snr(entry.match.receptions, self.snr_filter.minimum):
                entry.alerted = True
                self.log_entry("FILTER_ACK", entry, now, "revealed by filter setting; no alert")

    def set_filter(self, value: SnrFilter) -> None:
        changed = self.snr_filter.minimum != value.minimum
        self.snr_filter = value
        self.args.min_snr, self.args.show_filtered = value.minimum, value.show_filtered
        if changed:
            self._acknowledge_filter(time.time())
        self.filter_error = save_filter(value)

    def qth_warning(self) -> str:
        if self.args.qth is None and time.monotonic() < self.qth_warning_until:
            return "WARNING: QTH not set; cannot estimate distance or azimuth. Use --qth LOCATOR."
        return ""

    def log_entry(self, event: str, entry: Entry, now: float, detail: str = "") -> None:
        if self.log:
            if not entry.alerted:
                detail = (detail + " | " if detail else "") + "snr-filter=pending"
            self.log.record(event, now, entry.match, detail, entry.spot_time)

    def archive(self, entry: Entry, now: float) -> None:
        if not entry.archived:
            entry.archived = True
            self.log_entry("HISTORY", entry, now)

    def source_error(self, message: str, now: float) -> None:
        if self.log and message != self.error:
            self.log.record("SOURCE_ERROR" if message else "SOURCE_OK", now, detail=message or self.error)
        self.error = message

    def apply(self, snapshot: Snapshot, now: float) -> list[Match]:
        args = self.args
        self.refresh_lifecycle(now)
        current: dict[tuple, ActivationSpot] = {}
        for p in snapshot.spots:
            if 0 <= now - p.time <= max_spot_age(args, p.program):
                key = p.key
                if key not in current or p.time >= current[key].time:
                    current[key] = p
        for p in self.spots:
            key = p.key
            if ((key in current and p.time > current[key].time) or p.program in snapshot.failed_programs) and 0 <= now - p.time <= max_spot_age(args, p.program):
                current[key] = p
        fresh = {m.key: m for m in join_spots(list(current.values()), snapshot.rbn, args.spotters, now,
                                             {p: max_spot_age(args, p) for p in PROGRAMS}, args.rbn_max_age, args.tolerance)}
        new = []
        for key, latest in sorted(current.items(), key=lambda item: (item[1].time, item[0])):
            entry = self.entries.get(key)
            if entry is None and key not in fresh:
                continue
            if entry is not None and not entry.match.receptions and key not in fresh:
                continue  # A daily summary alone does not identify a confirming receiver.
            if entry is not None and latest.time < entry.spot_time:
                continue  # Delayed API reports cannot roll back the latest frequency.
            heard = {}
            for match in ([entry.match] if entry else []) + ([fresh[key]] if key in fresh else []):
                for reception in match.receptions:
                    if (reception.receiver not in heard or (reception.time is not None
                            and (heard[reception.receiver].time is None
                                 or reception.time > heard[reception.receiver].time))):
                        heard[reception.receiver] = reception
            name = latest.name or (entry.match.reference_names.get(latest.reference, "") if entry else "")
            qth = latest.qth or (entry.match.reference_qths.get(latest.reference, "") if entry else "")
            match = Match(latest.call, (latest.reference,), latest.frequency,
                          [heard[r] for r in args.spotters if r in heard],
                          {latest.reference: name},
                          latest.location or (entry.match.location if entry else None), latest.program,
                          {latest.reference: qth})
            deadlines = {latest.reference: latest.time + max_spot_age(args, latest.program)}
            if entry is None:
                self.rank += 1
                bands = self.station_bands.setdefault((match.call, match.program), set())
                previous_bands = tuple(sorted(bands)) if key[1] not in bands else ()
                bands.add(key[1])
                qualifies = accepts_snr(match.receptions, self.snr_filter.minimum)
                entry = Entry(match, deadlines, self.rank, now + 60 if qualifies else 0,
                              now + args.highlight_seconds if qualifies else 0, alerted=qualifies,
                              created_at=now, spot_time=latest.time,
                              previous_bands=previous_bands)
                self.entries[key] = entry
                self.log_entry("BAND" if entry.previous_bands else "NEW", entry, now,
                               "previous bands: " + "/".join(entry.previous_bands) if entry.previous_bands else "")
                if qualifies:
                    new.append(match)
            else:
                old_match, old_time = entry.match, entry.spot_time
                changed_fields = set()
                if entry.match.frequency != match.frequency:
                    entry.previous_frequency = entry.match.frequency
                    changed_fields.add(ChangedField.SPOT_FREQUENCY)
                previous_rbn, latest_rbn = latest_reception(entry.match), latest_reception(match)
                if previous_rbn and latest_rbn and previous_rbn.frequency != latest_rbn.frequency:
                    entry.previous_rbn_frequency = previous_rbn.frequency
                    changed_fields.add(ChangedField.RBN_FREQUENCY)
                if changed_fields:
                    entry.updated_fields = changed_fields
                    entry.updated_until = now + 60
                entry.match, entry.spot_deadlines, entry.eligible = match, deadlines, True
                entry.spot_time, entry.inactive_since = latest.time, None
                if old_match != match or old_time != latest.time:
                    self.log_entry("UPDATE", entry, now, update_description(entry, match, now) or "source report/metadata updated")
            if self.summary and match.program not in snapshot.failed_programs and match.receptions:
                self.summary.observe(match, latest.time)
        for key, entry in self.entries.items():
            if entry.match.program not in snapshot.failed_programs:
                entry.awaiting_source = False
            if key in snapshot.inactive and key not in current:
                entry.eligible = False
                self.archive(entry, now)
                if entry.inactive_since is None:
                    entry.inactive_since = now
            elif key not in current and entry.match.program not in snapshot.failed_programs:
                # Absence in one read retains the last deadline and entry.
                # An archived entry may still be ACTIVE until that deadline.
                self.archive(entry, now)
        self.spots, self.updated_at = list(current.values()), snapshot.time
        self.source_error(" | ".join(snapshot.errors), now)
        self.warnings, self.loading = snapshot.warnings, False
        if snapshot.rbn_status:
            self.rbn_status = snapshot.rbn_status
        self.refresh_lifecycle(now)
        for entry in self.entries.values():
            if (not entry.alerted and entry.active_match(now) is not None
                    and accepts_snr(entry.match.receptions, self.snr_filter.minimum)):
                entry.alerted = True
                entry.new_until, entry.flash_until = now + 60, now + args.highlight_seconds
                self.log_entry("ALERT", entry, now, "first qualifying SNR")
                new.append(entry.match)
        if self.summary:
            self.summary.flush()
        return list(reversed(new))

    def acknowledge(self, now: float) -> None:
        """Dismiss current highlights without changing entries, deadlines or order."""
        for entry in self.entries.values():
            entry.new_until = min(entry.new_until, now)
            entry.flash_until = min(entry.flash_until, now)
            entry.updated_until = min(entry.updated_until, now)

    def fail(self, message: str) -> None:
        # Fetch failure preserves confirmations and their source deadlines.
        self.source_error(clean(message), time.time())
        self.loading = False

    def refresh_lifecycle(self, now: float) -> None:
        for entry in self.entries.values():
            if entry.awaiting_source:
                if entry.inactive_since is None and now > max(entry.spot_deadlines.values()):
                    entry.inactive_since = max(entry.spot_deadlines.values())
                continue
            active = entry.active_match(now) is not None
            if active != entry.logged_active:
                self.log_entry("ACTIVE" if active else "INACTIVE", entry, now)
                entry.logged_active = active
            if now - entry.created_at >= self.args.new_max_age:
                self.archive(entry, now)
            if not active:
                self.archive(entry, now)
                if entry.inactive_since is None:
                    entry.inactive_since = min(now, max(entry.spot_deadlines.values(), default=now))

    def rows(self, now: float, include_filtered: bool = False) -> tuple[list[tuple[Entry, Match]], list[tuple[Entry, Match]]]:
        self.refresh_lifecycle(now)
        active, history = [], []
        for entry in self.entries.values():
            if (not include_filtered and not self.snr_filter.show_filtered
                    and not accepts_snr(entry.match.receptions, self.snr_filter.minimum)):
                continue
            match = entry.active_match(now)
            if match:
                active.append((entry, match))
            elif (entry.awaiting_source and entry.inactive_since is None) or (entry.inactive_since is not None
                           and 0 <= now - entry.inactive_since <= self.args.history_max_age):
                history.append((entry, entry.match))
        return (ordered_rows(active, self.sort, self.args.qth, now, self.snr_filter.minimum),
                ordered_rows(history, self.sort, self.args.qth, now, self.snr_filter.minimum))

    def filtered_count(self, now: float) -> int:
        return sum(not accepts_snr(match.receptions, self.snr_filter.minimum)
                   for group in self.rows(now, include_filtered=True) for _, match in group)
