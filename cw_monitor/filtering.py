"""Local eligibility, independent of activity, retention and colours."""

from dataclasses import dataclass, field
from collections.abc import Mapping
from .models import RbnSpot, Match
from .config import CALL_PATTERN
from .snr import BAND_LABELS, BandSnrRule, checked_rules, resolve_snr, validate_bounds
from .values import band_for



def accepts_snr(receptions: list[RbnSpot], minimum: int | None, maximum: int | None = None) -> bool:
    return minimum is None and maximum is None or any(
        (minimum is None or r.snr >= minimum) and (maximum is None or r.snr <= maximum)
        for r in receptions)


@dataclass(frozen=True)
class SnrFilter:
    minimum: int | None = None
    show_filtered: bool = False
    bands: frozenset[str] | None = None  # None includes every band, including unknown bands.
    spotters: frozenset[str] | None = None  # None accepts any monitored spotter.
    maximum: int | None = None
    per_band: Mapping[str, BandSnrRule] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_bounds(self.minimum, self.maximum)
        object.__setattr__(self, "per_band", checked_rules(self.per_band))
        if type(self.show_filtered) is not bool:
            raise ValueError("Show filtered must be true or false")
        if self.bands is not None and (not isinstance(self.bands, frozenset) or not self.bands
                                       or not self.bands.issubset(BAND_LABELS)):
            raise ValueError("Select at least one supported band from 160m to 70cm, or all bands")
        if self.spotters is not None and (not isinstance(self.spotters, frozenset) or not self.spotters
                or not all(isinstance(s, str) and CALL_PATTERN.fullmatch(s) for s in self.spotters)):
            raise ValueError("Select at least one valid spotter, or all monitored spotters")

    def accepts(self, match: Match) -> bool:
        return not filter_reasons(match, self.minimum, self.bands, self.spotters, self.maximum, self.per_band)

    @property
    def label(self) -> str:
        return "Filters: Off" if self.minimum is None and self.maximum is None and self.bands is None and self.spotters is None and not any(rule.enabled for rule in self.per_band.values()) else "Filters: On"


def filter_reasons(match: Match, minimum: int | None = None,
                   bands: frozenset[str] | None = None,
                   spotters: frozenset[str] | None = None, maximum: int | None = None, per_band: Mapping[str, BandSnrRule] | None = None) -> tuple[str, ...]:
    minimum, maximum = resolve_snr(minimum, maximum, per_band, band_for(match.frequency))
    receptions = match.receptions if spotters is None else [r for r in match.receptions if r.receiver in spotters]
    return (("band",) if bands is not None and band_for(match.frequency) not in bands else ()) + (
        ("spotter",) if spotters is not None and not receptions else ()) + (
        ("dB",) if not accepts_snr(receptions, minimum, maximum) else ())
