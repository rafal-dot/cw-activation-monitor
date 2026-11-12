"""SNR eligibility is independent of activity, history retention and colours."""

from dataclasses import dataclass
from .models import RbnSpot


def accepts_snr(receptions: list[RbnSpot], minimum: int | None) -> bool:
    return minimum is None or any(r.snr >= minimum for r in receptions)


@dataclass(frozen=True)
class SnrFilter:
    minimum: int | None = None
    show_filtered: bool = False

    def __post_init__(self) -> None:
        if self.minimum is not None and (type(self.minimum) is not int or not -100 <= self.minimum <= 100):
            raise ValueError("Minimum SNR must be an integer from -100 to 100 dB, or off")
        if type(self.show_filtered) is not bool:
            raise ValueError("Show filtered must be true or false")

    @property
    def label(self) -> str:
        return "Filter: Off" if self.minimum is None else f"Min: {self.minimum} dB"
