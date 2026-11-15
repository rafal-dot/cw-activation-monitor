"""Base-call matching with full operating callsign/band reception identities."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .config import CALL_PATTERN
from .callsigns import base_callsign
from .models import RbnSpot
from .values import station_band, band_for
from .snr import resolve_snr

MAX_FRIENDS = 1000


@dataclass(frozen=True)
class Friend:
    call: str
    first_name: str = ""
    last_name: str = ""
    comment: str = ""

    @property
    def name(self) -> str:
        return " ".join(filter(None, (self.first_name, self.last_name)))

    def validate(self) -> Friend:
        if not CALL_PATTERN.fullmatch(self.call):
            raise ValueError("Invalid full friend callsign")
        if any(len(value) > 128 for value in (self.first_name, self.last_name, self.comment)):
            raise ValueError("Names and comments must not exceed 128 characters each")
        if any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in self.first_name + self.last_name):
            raise ValueError("Control character in friend name")
        if any((ord(c) < 32 or 127 <= ord(c) <= 159) and c not in "\n\t" for c in self.comment):
            raise ValueError("Control character in friend comment")
        return self


class FriendIndex:
    """Exact entries win, then a bare base entry, then a stable same-base alias."""

    def __init__(self, friends=()):
        self.exact = {friend.call: friend for friend in friends}
        self.bases = {}
        for friend in sorted(friends, key=lambda friend: friend.call, reverse=True):
            base = base_callsign(friend.call)
            if base is not None:
                self.bases[base] = self.exact.get(base, friend)

    def find(self, call: str) -> Friend | None:
        return self.exact.get(call) or self.bases.get(base_callsign(call))


@dataclass(frozen=True)
class FriendSeen:
    call: str
    band: str
    first_seen: float
    last_seen: float
    alerted: bool

    @property
    def key(self):
        return self.call, self.band


@dataclass(frozen=True)
class FriendReception:
    friend: Friend
    readings: tuple[RbnSpot, ...]
    first_seen: float
    new_until: float = 0.0

    @property
    def latest(self) -> RbnSpot:
        return max(self.readings, key=lambda r: r.time)

    @property
    def call(self) -> str:
        return self.latest.call

    @property
    def key(self):
        return station_band(self.call, self.latest.frequency)


def accepts(reading: RbnSpot, args) -> bool:
    low, high = resolve_snr(args.min_snr, args.max_snr, args.band_snr, band_for(reading.frequency))
    return (reading.receiver in args.spotters
            and (args.filter_spotters is None or reading.receiver in args.filter_spotters)
            and (args.bands is None or station_band(reading.call, reading.frequency)[1] in args.bands)
            and (low is None or reading.snr >= low)
            and (high is None or reading.snr <= high))


class FriendTracker:
    def __init__(self, saved=()):
        self.seen = {item.key: item for item in saved}
        self.receptions: dict[tuple, FriendReception] = {}

    def markers(self):
        return sorted(self.seen.values(), key=lambda row: row.key)

    def observe(self, readings: list[RbnSpot], now: float, args) -> list[FriendReception]:
        friends = FriendIndex(args.friends)
        self.seen = {key: row for key, row in self.seen.items() if friends.find(row.call) is not None and 0 <= now - row.last_seen <= max(86400, args.rbn_max_age)}
        self.receptions = {key: row for key, row in self.receptions.items() if key in self.seen}
        if not args.friends_alerts:
            return []
        groups = {}
        for reading in readings:
            if (friends.find(reading.call) is not None and reading.receiver in args.spotters and reading.time is not None
                    and 0 <= now - reading.time <= args.rbn_max_age):
                groups.setdefault(station_band(reading.call, reading.frequency), []).append(reading)
        alerts = []
        for key, incoming in groups.items():
            previous = self.seen.get(key)
            last = max(r.time for r in incoming)
            if previous is not None and last < previous.last_seen:
                continue
            fresh = previous is None or last - previous.last_seen > args.rbn_max_age
            first = now if fresh else previous.first_seen
            old = self.receptions.get(key)
            latest = {}
            for reading in (() if fresh or old is None else old.readings) + tuple(incoming):
                if 0 <= now - reading.time <= args.rbn_max_age and (reading.receiver not in latest or reading.time > latest[reading.receiver].time):
                    latest[reading.receiver] = reading
            record = FriendReception(friends.find(key[0]), tuple(latest[s] for s in args.spotters if s in latest), first, 0 if fresh or old is None else old.new_until)
            eligible = any(accepts(r, args) for r in record.readings)
            alerted = not fresh and previous.alerted
            if not alerted and eligible:
                record = replace(record, new_until=now + 60)
                alerts.append(record)
            self.seen[key] = FriendSeen(*key, first, last, alerted or eligible)
            self.receptions[key] = record
        if len(self.seen) > 10000:
            keep = sorted(self.seen.values(), key=lambda row: row.last_seen, reverse=True)[:10000]
            self.seen = {row.key: row for row in keep}
            self.receptions = {key: row for key, row in self.receptions.items() if key in self.seen}
        return sorted(alerts, key=lambda row: (-row.latest.time, row.key))

    def acknowledge_new(self, now: float):
        self.receptions = {key: replace(row, new_until=min(now, row.new_until)) for key, row in self.receptions.items()}

    def acknowledge_filters(self, now: float, args) -> None:
        for entry in self.active(now, args):
            old = self.seen.get(entry.key)
            if old is not None and not old.alerted:
                self.seen[entry.key] = FriendSeen(old.call, old.band, old.first_seen, old.last_seen, True)

    def active(self, now: float, args) -> list[FriendReception]:
        if not args.friends_alerts:
            return []
        friends = FriendIndex(args.friends)
        result = []
        for row in self.receptions.values():
            friend = friends.find(row.call)
            if friend is None:
                continue
            current = tuple(r for r in row.readings if 0 <= now - r.time <= args.rbn_max_age and accepts(r, args))
            if current:
                result.append(FriendReception(friend, current, row.first_seen, row.new_until))
        return sorted(result, key=lambda row: (-row.latest.time, row.key))
