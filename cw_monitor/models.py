"""Activation identities, receptions and lifecycle data."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math

from .geo import location_locator
from .values import activation_key


class ChangedField(Enum):
    """Semantic changes, independent of the terminal's column positions."""

    SPOT_FREQUENCY = "spot_khz"
    RBN_FREQUENCY = "rbn_khz"


class SourceError(Exception):
    """A fetch failure or an unsupported source format."""

    def __init__(self, message: str, retry_after: float = 0):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class ActivationSpot:
    call: str
    reference: str
    frequency: float  # kHz
    time: float       # Unix UTC
    name: str = field(default="", compare=False)
    location: tuple[float, float] | None = field(default=None, compare=False)
    program: str = "POTA"
    qth: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        if not self.qth and self.location is not None:
            object.__setattr__(self, "qth", location_locator(self.location))

    @property
    def key(self) -> tuple:
        return activation_key(self.call, self.frequency, self.reference, self.program)


@dataclass(frozen=True)
class RbnSpot:
    call: str
    receiver: str
    frequency: float
    time: float | None
    snr: int
    wpm: int


@dataclass
class Match:
    call: str
    references: tuple[str, ...]
    frequency: float
    receptions: list[RbnSpot]
    reference_names: dict[str, str] = field(default_factory=dict)
    location: tuple[float, float] | None = None
    program: str = "POTA"
    reference_qths: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(self.references) == 1 and self.location is not None and not self.reference_qths.get(self.references[0]):
            self.reference_qths[self.references[0]] = location_locator(self.location)

    @property
    def key(self) -> tuple:
        return activation_key(self.call, self.frequency, self.references[0], self.program)


@dataclass
class Snapshot:
    spots: list[ActivationSpot]
    rbn: list[RbnSpot]
    warnings: list[str]
    time: float
    inactive: set[tuple] = field(default_factory=set)
    failed_programs: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)
    rbn_status: str = ""


@dataclass
class Entry:
    match: Match
    spot_deadlines: dict[str, float]
    rank: int
    new_until: float
    flash_until: float
    eligible: bool = True
    created_at: float = 0
    archived: bool = False
    inactive_since: float | None = None
    updated_until: float = 0
    previous_frequency: float | None = None
    previous_rbn_frequency: float | None = None
    updated_fields: set[ChangedField] = field(default_factory=set)
    spot_time: float = 0
    previous_bands: tuple[str, ...] = ()
    logged_active: bool = True
    restored: bool = False
    awaiting_source: bool = False
    first_seen_known: bool = True
    alerted: bool = True  # Legacy/restored records have already been announced.

    @property
    def last_heard(self) -> float | None:
        return max((r.time for r in self.match.receptions if r.time is not None), default=None)

    def active_match(self, now: float) -> Match | None:
        references = tuple(p for p in self.match.references if now <= self.spot_deadlines[p])
        # RBN freshness is checked at initial confirmation. Afterwards the
        # program deadline controls activity; reception times stay original.
        receptions = [r for r in self.match.receptions if r.time is None or r.time <= now]
        if self.eligible and not self.awaiting_source and references and receptions:
            return Match(self.match.call, references, self.match.frequency, receptions,
                         {p: name for p, name in self.match.reference_names.items() if p in references},
                         self.match.location, self.match.program,
                         {p: qth for p, qth in self.match.reference_qths.items() if p in references})
        return None


def latest_reception(match: Match) -> RbnSpot | None:
    return max(match.receptions, key=lambda r: r.time if r.time is not None else -math.inf, default=None)


def update_description(entry: Entry, match: Match, now: float) -> str:
    if now >= entry.updated_until:
        return ""
    fields = []
    if ChangedField.SPOT_FREQUENCY in entry.updated_fields and entry.previous_frequency is not None:
        fields.append(f"Spot {entry.previous_frequency:.1f} → {match.frequency:.1f} kHz")
    if ChangedField.RBN_FREQUENCY in entry.updated_fields and entry.previous_rbn_frequency is not None:
        freq = latest_reception(match).frequency
        fields.append(f"RBN {entry.previous_rbn_frequency:.1f} → {freq:.1f} kHz")
    return ", ".join(fields)
