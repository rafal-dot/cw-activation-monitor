"""Deterministic activation ordering using source timestamps and unrounded values."""

from __future__ import annotations

from dataclasses import dataclass
import time

from .geo import distance_km
from .models import Entry, Match
from .filtering import accepts_snr


SORT_FIELDS = {"last": "Last activity", "first": "First seen", "db": "Signal (dB)",
               "wpm": "WPM", "distance": "Distance"}


@dataclass(frozen=True)
class SortOrder:
    field: str = "last"
    direction: str = "desc"

    def __post_init__(self) -> None:
        if self.field not in SORT_FIELDS or self.direction not in ("asc", "desc"):
            raise ValueError("Invalid sort field or direction")

    @classmethod
    def resolve(cls, field: str | None, direction: str | None, saved: SortOrder | None = None) -> SortOrder:
        saved = saved or cls()
        return cls(field or saved.field, direction or (
            ("desc" if field in ("last", "first") else "asc") if field else saved.direction))

    @property
    def label(self) -> str:
        return SORT_FIELDS[self.field] + (" ↓" if self.direction == "desc" else " ↑")

    @property
    def short_label(self) -> str:
        return {"last": "Last", "first": "First", "db": "dB", "wpm": "WPM", "distance": "Dist"}[self.field] + (
            " ↓" if self.direction == "desc" else " ↑")

    @property
    def direction_label(self) -> str:
        if self.field in ("last", "first"):
            return "Newest first" if self.direction == "desc" else "Oldest first"
        return "Descending" if self.direction == "desc" else "Ascending"


def ordered_rows(rows: list[tuple[Entry, Match]], order: SortOrder,
                 origin: tuple[float, float] | None, now: float | None = None,
                 minimum_snr: int | None = None) -> list[tuple[Entry, Match]]:
    """Newest-first time sorts prioritize the current NEW group, then use normal keys."""
    now = time.time() if now is None else now
    prioritize_new = order.field in ("last", "first") and order.direction == "desc"
    def key(row):
        entry, match = row
        fresh = (prioritize_new and now < entry.new_until and entry.active_match(now) is not None
                 and accepts_snr(match.receptions, minimum_snr))
        snr = max((r.snr for r in match.receptions), default=None)
        if order.field == "last":
            value = entry.spot_time
        elif order.field == "first":
            value = entry.created_at if entry.first_seen_known else None
        elif order.field == "db":
            value = snr
        elif order.field == "wpm":
            value = max((r.wpm for r in match.receptions), default=None)
        else:
            value = distance_km(origin, match.location)
        return (not fresh, -entry.new_until if fresh else 0,
                value is None, (-value if order.direction == "desc" else value) if value is not None else 0,
                -entry.spot_time, snr is None, -snr if snr is not None else 0,
                -entry.rank, match.program, match.call, match.key[1], match.key[2])
    return sorted(rows, key=key)
