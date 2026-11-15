"""Immutable RBN telemetry and a bounded display average, independent of polling control."""

from collections import deque
from dataclasses import dataclass
from enum import Enum
import math


class RbnMode(Enum):
    WAITING = "waiting"
    IDLE = "idle"
    STREAM = "stream"
    BATCHES = "batches"


@dataclass(frozen=True)
class RbnBatch:
    received: int
    requested: int


@dataclass(frozen=True)
class RbnTrafficSample:
    count: int
    seconds: float


@dataclass(frozen=True)
class RbnStatus:
    mode: RbnMode = RbnMode.WAITING
    batch: RbnBatch | None = None
    interval: float | None = None
    spotters: tuple[str, ...] = ()
    last: RbnTrafficSample | None = None
    average_per_minute: float | None = None
    complete: bool = False


class RbnTraffic:
    def __init__(self):
        self._samples: deque[RbnTrafficSample] = deque(maxlen=5)
        self.last: RbnTrafficSample | None = None
        self.complete = False

    @property
    def average_per_minute(self) -> float | None:
        if not self._samples:
            return None
        return sum(s.count for s in self._samples) * 60 / sum(s.seconds for s in self._samples)

    def observe(self, count: int, seconds: float, complete: bool) -> None:
        if not math.isfinite(seconds) or seconds <= 0:
            return
        self.last = RbnTrafficSample(count, seconds)
        self.complete = complete
        # Incomplete windows are undercounts, not evidence of lower traffic.
        if complete:
            self._samples.append(self.last)
