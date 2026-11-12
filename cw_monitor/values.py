"""Validation and formatting of shared scalar values."""

from __future__ import annotations

import argparse
from datetime import datetime
import math
import unicodedata

from .config import BANDS, UTC


def clean(value: object) -> str:
    """Strip control characters before external values reach the terminal."""
    return "".join(c for c in str(value) if not unicodedata.category(c).startswith("C"))


def receiver_call(value: str) -> str:
    # Remove the skimmer marker, preserving actual SSIDs such as -2.
    return value.strip().upper().removesuffix("-#").removesuffix("#")


def frequency(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("Invalid frequency")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError("Invalid frequency")
    return result


def timestamp(value: str) -> float:
    # POTA can supply UTC timestamps without a trailing Z or offset.
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.timestamp()


def max_spot_age(args: argparse.Namespace, program: str) -> float:
    return getattr(args, program.lower() + "_max_age")


def age_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def band_for(freq: float) -> str:
    return next((label for low, high, label in BANDS if low <= freq <= high), "—")


def station_band(call: str, freq: float) -> tuple[str, str]:
    band = band_for(freq)
    return call, band if band != "—" else f"{freq:g}kHz"


def activation_key(call: str, freq: float, park: str, program: str = "POTA") -> tuple[str, str, str, str]:
    return (*station_band(call, freq), park, program)
