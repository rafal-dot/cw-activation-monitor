"""Parse program feeds and match activations to RBN receptions."""

from __future__ import annotations

import math
import re

from ..config import CALL_PATTERN
from ..geo import park_coordinates, reference_qth
from ..models import ActivationSpot, Match, RbnSpot, SourceError
from ..values import activation_key, clean, frequency, station_band, timestamp


def read_pota(payload: object, now: float, max_age: float) -> tuple[list[ActivationSpot], int, set[tuple]]:
    if not isinstance(payload, list):
        raise SourceError("POTA: expected a JSON list of spots")
    latest: dict[tuple, tuple[float, dict, float]] = {}
    skipped = 0
    for item in payload:
        try:
            call = item["activator"].strip().upper()
            park = item["reference"].strip().upper()
            when = timestamp(item["spotTime"])
            freq = frequency(item["frequency"])
            if not CALL_PATTERN.fullmatch(call) or not re.fullmatch(r"[A-Z0-9]+-\d+", park):
                raise ValueError("Invalid callsign or park")
            key = activation_key(call, freq, park)
            if key not in latest or when >= latest[key][0]:
                latest[key] = (when, item, freq)
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            skipped += 1
    selected, inactive = [], set()
    for (call, band, park, program), (when, item, freq) in latest.items():
        # Choose the latest report before filtering so an older CW spot
        # cannot override a newer QRT or mode change.
        if not 0 <= now - when <= max_age:
            continue
        if str(item.get("mode", "")).upper() != "CW" or item.get("invalid"):
            inactive.add((call, band, park, program))
            continue
        if re.search(r"\bQRT\b", str(item.get("comments", "")), re.IGNORECASE):
            inactive.add((call, band, park, program))
            continue
        try:
            name = " ".join(clean(item.get("name") or item.get("parkName") or "").split())
            selected.append(ActivationSpot(call, park, freq, when, name, park_coordinates(item),
                                           qth=reference_qth(item)))
        except (KeyError, TypeError, ValueError, OverflowError):
            skipped += 1
    if payload and skipped == len(payload):
        raise SourceError("POTA: could not parse any entries; check the data format")
    inactive.difference_update(p.key for p in selected)
    return selected, skipped, inactive


def select_pota(payload: object, now: float, max_age: float) -> tuple[list[ActivationSpot], int]:
    selected, skipped, _ = read_pota(payload, now, max_age)
    return selected, skipped


def read_program(payload: object, program: str, now: float, max_age: float) -> tuple[list[ActivationSpot], int, set[tuple]]:
    """Read WWFF JSON or a local SOTA export; never connect to a SOTA API."""
    if not isinstance(payload, list):
        raise SourceError(f"{program}: expected a JSON list of spots")
    normalized, skipped = [], 0
    for item in payload:
        try:
            if program == "SOTA" and item.get("associationCode") == "DEPRECATED":
                raise SourceError("SOTA: deprecated API response; use a current local JSON export")
            call = str(item.get("activator") or item["activatorCallsign"]).strip().upper()
            reference = item.get("reference")
            if not reference:
                reference = f"{item['associationCode']}/{item['summitCode']}"
            reference = str(reference).strip().upper()
            pattern = r"[A-Z0-9]+/[A-Z0-9]+-\d+" if program == "SOTA" else r"[A-Z0-9]+-\d+"
            if not CALL_PATTERN.fullmatch(call) or not re.fullmatch(pattern, reference):
                raise ValueError("Invalid callsign or reference")
            freq = frequency(item["frequency_khz"]) if "frequency_khz" in item else frequency(item["frequency"]) * 1000
            when = float(item["spot_time"]) if "spot_time" in item else timestamp(item["timeStamp"])
            if not math.isfinite(when):
                raise ValueError("Invalid timestamp")
            normalized.append((ActivationSpot(call, reference, freq, when,
                               " ".join(clean(item.get("reference_name") or item.get("summitDetails") or "").split()),
                               park_coordinates(item), program, reference_qth(item)),
                               str(item.get("mode", "")).strip().upper(),
                               str(item.get("remarks") or item.get("comments") or ""), bool(item.get("invalid"))))
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            skipped += 1
    if payload and skipped == len(payload):
        raise SourceError(f"{program}: could not parse any entries; check the data format")
    latest = {}
    for data in normalized:
        spot = data[0]
        if spot.key not in latest or spot.time >= latest[spot.key][0].time:
            latest[spot.key] = data
    selected, inactive = [], set()
    for spot, mode, comment, invalid in latest.values():
        if not 0 <= now - spot.time <= max_age:
            continue
        if mode != "CW" or invalid or re.search(r"\b(?:QRT|TEST)\b", comment, re.IGNORECASE):
            inactive.add(spot.key)
        else:
            selected.append(spot)
    return selected, skipped, inactive


def join_spots(spots: list[ActivationSpot], rbn: list[RbnSpot], receivers: list[str],
               now: float, pota_age: float | dict[str, float], rbn_age: float, tolerance: float) -> list[Match]:
    groups: dict[tuple, list[ActivationSpot]] = {}
    for spot in spots:
        age_limit = pota_age[spot.program] if isinstance(pota_age, dict) else pota_age
        if 0 <= now - spot.time <= age_limit:
            groups.setdefault(spot.key, []).append(spot)
    by_call: dict[str, list[RbnSpot]] = {}
    for spot in rbn:
        if spot.receiver in receivers and spot.time is not None and 0 <= now - spot.time <= rbn_age:
            by_call.setdefault(spot.call, []).append(spot)
    result = []
    for (call, band, park, program), activations in groups.items():
        latest = max(activations, key=lambda p: p.time)
        freq = latest.frequency
        heard: dict[str, RbnSpot] = {}
        for spot in by_call.get(call, []):
            if station_band(call, spot.frequency)[1] != band or abs(spot.frequency - freq) > tolerance + 1e-9:
                continue
            if spot.receiver not in heard or spot.time > heard[spot.receiver].time:
                heard[spot.receiver] = spot
        if heard:
            result.append(Match(call, (park,), freq,
                                [heard[r] for r in receivers if r in heard],
                                {p.reference: p.name for p in activations if p.name},
                                latest.location, program,
                                {p.reference: p.qth for p in activations if p.qth}))
    return sorted(result, key=lambda row: (-max(s.time for s in row.receptions), row.call))
