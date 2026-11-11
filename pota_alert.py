#!/usr/bin/env python3
"""CW activations from POTA, WWFF and optional SOTA files, confirmed by RBN."""

from __future__ import annotations

import argparse
from collections import deque
import csv
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
from queue import Empty, Queue
import re
import shutil
import sys
import tempfile
import textwrap
from threading import Event, Lock, Thread
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


POTA_URL = "https://api.pota.app/spot/activator"
WWFF_URL = "https://spots.wwff.co/static/spots.json"
PROGRAMS = ("POTA", "SOTA", "WWFF")
RBN_URL = "https://www.reversebeacon.net"
USER_AGENT = "cw-activation-monitor/0.3.0 (read-only activation/RBN monitor)"
UTC = timezone.utc
CALL_PATTERN = re.compile(r"[A-Z0-9]+(?:/[A-Z0-9]+)*(?:-\d+)?")
# Zakresy klasyfikacji z tabeli pasm aplikacji POTA; wartości w kHz.
BANDS = (
    (135.7, 137.8, "2190m"), (472, 479, "630m"), (501, 504, "560m"),
    (1800, 2000, "160m"), (3500, 4000, "80m"), (5060, 5450, "60m"),
    (7000, 7300, "40m"), (10100, 10150, "30m"), (14000, 14350, "20m"),
    (18068, 18168, "17m"), (21000, 21450, "15m"), (24890, 24990, "12m"),
    (28000, 29700, "10m"), (40000, 45000, "8m"), (50000, 54000, "6m"),
    (54000.001, 69900, "5m"), (70000, 71000, "4m"), (144000, 148000, "2m"),
    (222000, 225000, "1.25m"), (420000, 450000, "70cm"),
    (902000, 928000, "33cm"), (1240000, 1300000, "23cm"),
    (2300000, 2450000, "13cm"), (3300000, 3500000, "9cm"),
    (5650000, 5925000, "6cm"), (10000000, 10500000, "3cm"),
    (24000000, 24250000, "1.25cm"), (47000000, 47200000, "6mm"),
    (75500000, 81000000, "4mm"), (119980000, 123000000, "2.5mm"),
    (134000000, 149000000, "2mm"), (241000000, 250000000, "1mm"),
    (275000000, 30000000000, "submm"),
)


class SourceError(Exception):
    """Błąd pobierania lub nieobsługiwany format źródła."""

    def __init__(self, message: str, retry_after: float = 0):
        super().__init__(message)
        self.retry_after = retry_after


def clean(value: object) -> str:
    """Nie przepuszczaj sekwencji sterujących z zewnętrznych danych do terminala."""
    return "".join(c for c in str(value) if not unicodedata.category(c).startswith("C"))


def receiver_call(value: str) -> str:
    # -# jest oznaczeniem skimmera, a nie częścią znaku. Zachowaj SSID, np. -2.
    return value.strip().upper().removesuffix("-#").removesuffix("#")


def frequency(value: object) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError("Invalid frequency")
    return result


def timestamp(value: str) -> float:
    # POTA zwraca czas UTC także bez końcowego Z/offsetu.
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.timestamp()


def coordinates(latitude: object, longitude: object) -> tuple[float, float] | None:
    """Validate optional source coordinates without discarding an otherwise valid spot."""
    if isinstance(latitude, bool) or isinstance(longitude, bool):
        return None
    try:
        lat, lon = float(latitude), float(longitude)
    except (TypeError, ValueError, OverflowError):
        return None
    if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon
    return None


def locator_coordinates(locator: str) -> tuple[float, float]:
    """Return the centre of a 2-, 4-, 6- or 8-character Maidenhead cell."""
    locator = locator.strip().upper()
    if not re.fullmatch(r"[A-R]{2}(?:[0-9]{2}(?:[A-X]{2}(?:[0-9]{2})?)?)?", locator):
        raise ValueError("expected a Maidenhead locator (2, 4, 6 or 8 characters)")
    lon, lat = -180.0, -90.0
    lon_step, lat_step = 20.0, 10.0
    for offset in range(0, len(locator), 2):
        if offset:
            divisor = 24 if offset == 4 else 10
            lon_step, lat_step = lon_step / divisor, lat_step / divisor
        base = ord("A") if offset in (0, 4) else ord("0")
        lon += (ord(locator[offset]) - base) * lon_step
        lat += (ord(locator[offset + 1]) - base) * lat_step
    return lat + lat_step / 2, lon + lon_step / 2


def qth_coordinates(value: str) -> tuple[float, float]:
    if "," in value:
        parts = value.split(",")
        result = coordinates(*parts) if len(parts) == 2 else None
        if result is None:
            raise ValueError("expected latitude,longitude within -90..90 and -180..180")
        return result
    return locator_coordinates(value)


def location_locator(location: tuple[float, float]) -> str:
    lat, lon = location
    x = min(4319, max(0, math.floor((lon + 180) * 12)))
    y = min(4319, max(0, math.floor((lat + 90) * 24)))
    return (chr(65 + x // 240) + chr(65 + y // 240)
            + str(x // 24 % 10) + str(y // 24 % 10)
            + chr(65 + x % 24) + chr(65 + y % 24))


def reference_qth(item: dict) -> str:
    for key in ("grid8", "grid6", "qth", "locator", "grid", "grid4"):
        value = str(item.get(key) or "").strip().upper()
        try:
            locator_coordinates(value)
        except ValueError:
            continue
        if len(value) >= 6:
            return value
    location = coordinates(item.get("latitude"), item.get("longitude"))
    if location is not None:
        return location_locator(location)
    for key in ("grid4", "qth", "locator", "grid"):
        value = str(item.get(key) or "").strip().upper()
        try:
            locator_coordinates(value)
            return value
        except ValueError:
            continue
    return ""


def park_coordinates(item: dict) -> tuple[float, float] | None:
    result = coordinates(item.get("latitude"), item.get("longitude"))
    if result is not None:
        return result
    qth = reference_qth(item)
    return locator_coordinates(qth) if qth else None


def distance_text(origin: tuple[float, float] | None,
                  destination: tuple[float, float] | None, unit: str) -> str:
    if origin is None or destination is None:
        return "—"
    lat1, lon1 = map(math.radians, origin)
    lat2, lon2 = map(math.radians, destination)
    a = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    km = 6371.0088 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, a))))
    return f"{km / 1.609344 if unit == 'mi' else km:.0f} {unit}"


def azimuth_text(origin: tuple[float, float] | None,
                 destination: tuple[float, float] | None) -> str:
    """Initial great-circle bearing, clockwise from true north."""
    if origin is None or destination is None:
        return "—"
    lat1, lon1 = map(math.radians, origin)
    lat2, lon2 = map(math.radians, destination)
    delta = lon2 - lon1
    y = math.sin(delta) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta)
    if abs(math.cos(lat1)) < 1e-12 or math.hypot(x, y) < 1e-12:
        return "—"  # No unique bearing at the origin pole, same point or antipode.
    bearing = math.degrees(math.atan2(y, x)) % 360
    return f"{round(bearing) % 360:03d}°"


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
        # Wybór najnowszego wpisu przed filtrowaniem zapobiega przywróceniu
        # starszego spotu CW po nowszym QRT lub zmianie emisji.
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


def max_spot_age(args: argparse.Namespace, program: str) -> float:
    return getattr(args, program.lower() + "_max_age")


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
        freq = max(activations, key=lambda p: p.time).frequency
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
                                max(activations, key=lambda p: p.time).location, program,
                                {p.reference: p.qth for p in activations if p.qth}))
    return sorted(result, key=lambda row: (-max(s.time for s in row.receptions), row.call))


def get_text(url: str, timeout: float) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/html"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.read(8_000_001).decode("utf-8")
    except HTTPError as exc:
        delay = 0.0
        retry = exc.headers.get("Retry-After", "")
        if retry:
            try:
                delay = max(0, float(retry))
            except ValueError:
                from email.utils import parsedate_to_datetime
                try:
                    delay = max(0, parsedate_to_datetime(retry).timestamp() - time.time())
                except (ValueError, TypeError, OverflowError):
                    pass
        raise SourceError(f"HTTP {exc.code}: {url.split('?')[0]}", delay) from exc
    except (URLError, TimeoutError, OSError, UnicodeError) as exc:
        raise SourceError(f"Connection to {url.split('?')[0]}: {clean(exc)}") from exc


def get_json(url: str, timeout: float) -> object:
    try:
        return json.loads(get_text(url, timeout))
    except ValueError as exc:
        raise SourceError(f"Invalid JSON: {url.split('?')[0]}") from exc


class RbnLoad:
    """Estimate rows required for a window; grow immediately and shrink slowly."""

    levels = (10, 20, 50, 100)

    def __init__(self):
        self.limit = 10
        self.average: float | None = None
        self.peaks: deque[float] = deque(maxlen=10)
        self.quiet = 0

    def estimate(self, seconds: float) -> int:
        rate = max((self.average or 0) * 1.5, max(self.peaks, default=0) * 1.2)
        needed = rate * max(0, seconds) / 60
        return next((n for n in self.levels if n >= needed), 100)

    def start(self, seconds: float) -> int:
        self.limit = max(self.limit, self.estimate(seconds))
        return self.limit

    def grow(self) -> int:
        self.limit = next((n for n in self.levels if n > self.limit), 100)
        self.quiet = 0
        return self.limit

    def observe(self, count: int, elapsed: float, next_window: float, complete: bool) -> None:
        if not complete:
            self.limit, self.quiet = 100, 0
            return  # A truncated count is a lower bound, never a low-traffic sample.
        if elapsed <= 0:
            return
        rate = count * 60 / elapsed
        self.average = rate if self.average is None else 0.3 * rate + 0.7 * self.average
        self.peaks.append(rate)
        wanted = self.estimate(next_window)
        if wanted > self.limit:
            self.limit, self.quiet = wanted, 0
        elif wanted < self.limit:
            self.quiet += 1
            if self.quiet >= 3:
                self.limit = self.levels[self.levels.index(self.limit) - 1]
                self.quiet = 0
        else:
            self.quiet = 0


@dataclass
class RbnStream:
    load: RbnLoad = field(default_factory=RbnLoad)
    watermark: float | None = None
    spots: dict[str, RbnSpot] = field(default_factory=dict)
    sampled_ids: set[str] = field(default_factory=set)


class RbnClient:
    """Odczyt tego samego źródła HTTPS, którego używa strona RBN.

    To wewnętrzny interfejs strony, więc sprawdzamy wersję i schemat zamiast
    zakładać na stałe kolejność kolumn lub identyfikator emisji CW.
    """

    def __init__(self, timeout: float, small_batches: Event | None = None):
        self.timeout = timeout
        self.version = ""
        self.fields: list[str] = []
        self.cw_mode = ""
        self.warnings: list[str] = []
        self.small_batches = small_batches if small_batches is not None else Event()
        self.streams: dict[tuple, RbnStream] = {}
        self.status = "RBN: waiting"
        self.request_log: list[str] = []

    def initialize(self) -> None:
        page = get_text(RBN_URL + "/main.php", self.timeout)
        version = re.search(r'RBN_version_hash\s*=\s*[\"\']([a-zA-Z0-9]+)[\"\']', page)
        meta = get_json(RBN_URL + "/spots.php?meta=1", self.timeout)
        required = {"de", "dx", "freq", "db", "wpm", "epoch", "mode"}
        if not version or not isinstance(meta, dict):
            raise SourceError("RBN: unrecognized website version or metadata")
        fields = meta.get("spot_fields")
        modes = meta.get("modes")
        if not isinstance(fields, list) or not required.issubset(fields) or not isinstance(modes, dict):
            raise SourceError("RBN: spot data schema has changed")
        cw_mode = next((str(k) for k, v in modes.items()
                        if isinstance(v, dict) and v.get("mode") == "cw"), None)
        if cw_mode is None:
            raise SourceError("RBN: CW mode missing from metadata")
        self.version, self.fields, self.cw_mode = version[1], fields, cw_mode

    def _request(self, calls: list[str] | None, receivers: list[str], max_age: float,
                 limit: int) -> tuple[dict[str, RbnSpot], int, int]:
        for attempt in range(2):
            if not self.version:
                self.initialize()
            params = {"h": self.version, "cde": ",".join(receivers),
                      "ma": math.ceil(max_age), "m": self.cw_mode, "s": 0, "r": limit}
            if calls is not None:
                params["cdx"] = ",".join(calls)
            data = get_json(RBN_URL + "/spots.php?" + urlencode(params), self.timeout)
            if not isinstance(data, dict):
                raise SourceError("RBN: expected a JSON object")
            if data.get("ver_h") != self.version:
                self.version = ""
                if attempt == 0:
                    continue
                raise SourceError("RBN: website version changed; retrying next cycle")
            if data.get("error") or data.get("impossible"):
                raise SourceError("RBN: " + clean(data.get("error") or data["impossible"]))
            raw = data.get("spots", {})
            if raw == []:  # Puste tablice PHP bywają kodowane jako [].
                raw = {}
            if not isinstance(raw, dict):
                raise SourceError("RBN: unsupported spot list format")
            result, skipped = {}, 0
            for spot_id, values in raw.items():
                try:
                    if not isinstance(values, list) or len(values) != len(self.fields):
                        raise SourceError("RBN: spot row format has changed")
                    item = dict(zip(self.fields, values))
                    if str(item["mode"]) != self.cw_mode:
                        continue
                    call = str(item["dx"]).strip().upper()
                    receiver = receiver_call(str(item["de"]))
                    if (calls is not None and call not in calls) or receiver not in receivers:
                        continue
                    when = float(item["epoch"])
                    if not math.isfinite(when) or when < 0:
                        raise ValueError("Invalid timestamp")
                    result[str(spot_id)] = RbnSpot(call, receiver, frequency(item["freq"]), when,
                                                  int(item["db"]), int(item["wpm"]))
                except (KeyError, TypeError, ValueError, OverflowError):
                    skipped += 1
            if skipped:
                self.warnings.append(f"RBN: skipped {skipped} invalid spots")
            if raw and skipped == len(raw):
                raise SourceError("RBN: could not parse any spots")
            self.request_log.append(f"{'stream' if calls is None else 'batches'} "
                                    f"receivers={','.join(receivers)} calls={','.join(calls) if calls else 'all'} "
                                    f"limit={limit} received={len(raw)} skipped={skipped}")
            return result, len(raw), skipped
        raise AssertionError("Unreachable")

    def _query(self, calls: list[str], receivers: list[str], max_age: float) -> dict[str, RbnSpot]:
        result, count, skipped = self._request(calls, receivers, max_age, 100)
        if count >= 100:
            if len(calls) > 1:
                middle = len(calls) // 2
                return {**self._query(calls[:middle], receivers, max_age),
                        **self._query(calls[middle:], receivers, max_age)}
            if len(receivers) > 1:
                merged = {}
                for receiver in receivers:
                    merged.update(self._query(calls, [receiver], max_age))
                return merged
            self.warnings.append(f"RBN: 100-spot limit reached for {calls[0]} / {receivers[0]}; "
                                 "results may be incomplete even in smaller batches")
        return result

    def fetch(self, calls: list[str], receivers: list[str], max_age: float) -> list[RbnSpot]:
        self.warnings = []
        self.request_log = []
        if not calls:
            return []
        started = time.time()
        key = tuple(sorted(receivers)), max_age
        stream = self.streams.setdefault(key, RbnStream())
        if stream.watermark is not None and stream.watermark > started:
            stream = self.streams[key] = RbnStream()
        overlap = 15.0
        target = max(started - max_age, stream.watermark - overlap) if stream.watermark is not None else started - max_age
        window = max(1.0, started - target)
        previous = stream.watermark
        limit = stream.load.start(window)
        while True:
            rows, count, skipped = self._request(None, receivers, max_age, limit)
            now = time.time()
            stream.spots.update(rows)
            stream.spots = {i: s for i, s in stream.spots.items() if 0 <= now - s.time <= max_age}
            oldest = min((s.time for s in rows.values()), default=started)
            complete = not skipped and (count < limit or oldest < target)
            if complete or limit == 100:
                break
            limit = stream.load.grow()
        elapsed = min(max_age, started - previous) if previous is not None else max_age
        sample_start = max(started - max_age, previous) if previous is not None else started - max_age
        new_count = sum(i not in stream.sampled_ids and s.time >= sample_start for i, s in stream.spots.items())
        stream.load.observe(new_count, elapsed, min(max_age, max(1.0, elapsed) + overlap), complete)
        if complete:
            stream.watermark = started
            stream.sampled_ids = set(stream.spots)
        else:
            self.warnings.append("RBN stream incomplete at 100 rows; press b for smaller batches "
                                 "or use --rbn-small-batches in text mode")
            if self.small_batches.is_set():
                for start in range(0, len(calls), 20):
                    stream.spots.update(self._query(calls[start:start + 20], receivers, max_age))
                self.warnings = [w for w in self.warnings if not w.startswith("RBN stream incomplete")]
        now = time.time()
        stream.spots = {i: s for i, s in stream.spots.items() if 0 <= now - s.time <= max_age}
        mode = "stream" if complete else "batches" if self.small_batches.is_set() else "INCOMPLETE"
        average = "—" if stream.load.average is None else f"{stream.load.average:.1f}"
        peak = f"{max(stream.load.peaks):.1f}" if stream.load.peaks else "—"
        self.status = f"RBN {mode}: rows {limit}→{stream.load.limit} | avg {average}/min | peak {peak}/min"
        selected = set(calls)
        return [s for s in stream.spots.values() if s.call in selected]


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


def upgrade_qth_csv(path: Path, fields: tuple[str, ...]) -> None:
    """Add the optional QTH column to the preceding schema, preserving a backup."""
    if not path.exists() or not path.stat().st_size:
        return
    original = path.stat()
    with path.open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source, strict=True)
        header = tuple(reader.fieldnames or ())
        if header == fields:
            return
        if header != tuple(name for name in fields if name != "qth"):
            raise ValueError(f"File has a different CSV header: {path}")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent,
                                             prefix="." + path.name + ".", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                writer = csv.DictWriter(output, fieldnames=fields)
                writer.writeheader()
                for row in reader:
                    if None in row or any(value is None for value in row.values()):
                        raise ValueError(f"Wrong column count in {path}, line {reader.line_num}")
                    writer.writerow(dict(row, qth=""))
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, original.st_mode & 0o777)
            current = path.stat()
            if (current.st_ino, current.st_size, current.st_mtime_ns) != (original.st_ino, original.st_size, original.st_mtime_ns):
                raise ValueError(f"CSV changed during upgrade: {path}")
            backup_dir = path.parent / ".csv-backups"
            backup_dir.mkdir(exist_ok=True)
            backup = Path(tempfile.mkdtemp(prefix="before-qth-", dir=backup_dir)) / path.name
            shutil.copy2(path, backup)
            if path.stat().st_mtime_ns != original.st_mtime_ns or path.stat().st_size != original.st_size:
                raise ValueError(f"CSV changed during upgrade: {path}")
            os.replace(temporary, path)
        except csv.Error as exc:
            raise ValueError(f"Invalid CSV file {path}: {exc}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class EventLog:
    fields = ("timestamp_utc", "source_time_utc", "event", "program", "callsign",
              "reference", "qth", "band", "spot_khz", "rbn_khz", "receivers", "snr_db", "wpm", "detail")

    def __init__(self, path: Path, level: str):
        self.path = path
        self.level, self.error, self.lock, self.closed = level, "", Lock(), False
        upgrade_qth_csv(path, self.fields)
        if path.exists() and path.stat().st_size:
            with path.open(encoding="utf-8", newline="") as source:
                if tuple(next(csv.reader(source), ())) != self.fields:
                    raise ValueError(f"Log file has a different CSV header: {path}")
            with path.open("rb") as source:
                source.seek(-1, os.SEEK_END)
                needs_newline = source.read(1) not in (b"\n", b"\r")
        else:
            needs_newline = False
        self.file = path.open("a", encoding="utf-8", newline="")
        self.writer = csv.writer(self.file)
        if self.file.tell() == 0:
            self.writer.writerow(self.fields)
            self.file.flush()
        self.needs_newline = needs_newline

    def records(self):
        """Read saved events before the polling thread starts."""
        with self.lock, self.path.open(encoding="utf-8", newline="") as source:
            try:
                yield from csv.DictReader(source, strict=True)
            except (csv.Error, UnicodeError) as exc:
                raise ValueError(f"Cannot restore event log {self.path}: {exc}") from exc

    @staticmethod
    def utc(when: float) -> str:
        return datetime.fromtimestamp(when, UTC).isoformat(timespec="seconds")

    def record(self, event: str, now: float, match: Match | None = None,
               detail: str = "", spot_time: float | None = None, debug: bool = False) -> None:
        if debug and self.level != "debug":
            return
        values = {"timestamp_utc": self.utc(now), "event": event, "detail": clean(detail)}
        if match is not None:
            values.update(source_time_utc=self.utc(spot_time) if spot_time is not None else "",
                          program=match.program, callsign=match.call, reference="/".join(match.references),
                          qth="/".join(match.reference_qths.get(p, "") for p in match.references),
                          band=band_for(match.frequency), spot_khz=f"{match.frequency:.1f}",
                          rbn_khz="/".join(f"{r.frequency:.1f}" for r in match.receptions),
                          receivers="/".join(r.receiver for r in match.receptions),
                          snr_db="/".join(str(r.snr) for r in match.receptions),
                          wpm="/".join(str(r.wpm) for r in match.receptions))
        with self.lock:
            if self.closed or self.error:
                return
            try:
                if self.needs_newline:
                    self.file.write("\n")
                    self.needs_newline = False
                self.writer.writerow([values.get(name, "") for name in self.fields])
                self.file.flush()
            except OSError as exc:
                self.error = clean(exc)

    def close(self) -> None:
        with self.lock:
            if not self.closed:
                self.closed = True
                try:
                    self.file.close()
                except OSError as exc:
                    self.error = clean(exc)


def get_log(args: argparse.Namespace) -> EventLog | None:
    if not hasattr(args, "_event_log"):
        if args.summary_file is not None:
            get_summary(args)  # Acquire the summary lock before upgrading either log.
        args._event_log = EventLog(args.log_file, args.log_level) if args.log_file is not None else None
    return args._event_log


@dataclass
class SummaryRow:
    date: str
    program: str
    callsign: str
    reference: str
    name: str
    band: str
    first_seen: float
    last_seen: float
    frequency: float
    qth: str = ""

    @property
    def key(self) -> tuple:
        return self.date, self.program, self.callsign, self.reference, self.band

    def values(self) -> list[str]:
        return [self.date,
                datetime.fromtimestamp(self.first_seen, UTC).isoformat(),
                datetime.fromtimestamp(self.last_seen, UTC).isoformat(),
                self.program, self.callsign, self.reference, self.name, self.qth, self.band, str(self.frequency)]


class DailySummary:
    """One persistent row per source UTC day and confirmed activation identity."""

    fields = ("date_utc", "first_seen_utc", "last_seen_utc", "program", "callsign", "reference", "name", "qth", "band",
              "last_frequency_khz")

    def __init__(self, path: Path):
        import fcntl

        self.path = path.resolve()
        self.rows: dict[tuple, SummaryRow] = {}
        self.error, self.dirty, self.closed = "", False, False
        self.lock = Lock()
        self.lock_file = self.path.with_name("." + self.path.name + ".lock").open("a")
        try:
            try:
                fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError(f"Daily summary is already in use: {self.path}") from exc
            if self.path.exists() and self.path.stat().st_size:
                self._load()
                upgrade_qth_csv(self.path, self.fields)
            else:
                self._write()
        except Exception:
            self.lock_file.close()
            raise

    def _merge(self, incoming: SummaryRow) -> None:
        current = self.rows.get(incoming.key)
        if current is None:
            self.rows[incoming.key] = incoming
            self.dirty = True
            return
        old = current.values()
        if incoming.name and (not current.name or incoming.last_seen >= current.last_seen):
            current.name = incoming.name
        if incoming.qth and (not current.qth or incoming.last_seen >= current.last_seen):
            current.qth = incoming.qth
        if incoming.last_seen >= current.last_seen:
            current.frequency = incoming.frequency
        current.first_seen = min(current.first_seen, incoming.first_seen)
        current.last_seen = max(current.last_seen, incoming.last_seen)
        self.dirty |= current.values() != old

    def _load(self) -> None:
        duplicate = False
        try:
            with self.path.open(encoding="utf-8", newline="") as source:
                reader = csv.DictReader(source, strict=True)
                if tuple(reader.fieldnames or ()) not in (self.fields, tuple(f for f in self.fields if f != "qth")):
                    raise ValueError("different CSV header")
                for raw in reader:
                    if None in raw or any(value is None for value in raw.values()):
                        raise ValueError(f"wrong column count at line {reader.line_num}")
                    first, last = timestamp(raw["first_seen_utc"]), timestamp(raw["last_seen_utc"])
                    qth = raw.get("qth", "").strip().upper()
                    if qth:
                        locator_coordinates(qth)
                    freq = frequency(raw["last_frequency_khz"])
                    pattern = r"[A-Z0-9]+/[A-Z0-9]+-\d+" if raw["program"] == "SOTA" else r"[A-Z0-9]+-\d+"
                    if (raw["program"] not in PROGRAMS or not CALL_PATTERN.fullmatch(raw["callsign"])
                            or not re.fullmatch(pattern, raw["reference"])
                            or first > last
                            or any(datetime.fromtimestamp(t, UTC).date().isoformat() != raw["date_utc"] for t in (first, last))
                            or station_band(raw["callsign"], freq)[1] != raw["band"]):
                        raise ValueError(f"invalid identity, timestamps or band at line {reader.line_num}")
                    row = SummaryRow(raw["date_utc"], raw["program"], raw["callsign"], raw["reference"],
                                     " ".join(clean(raw["name"]).split()), raw["band"], first, last, freq, qth)
                    duplicate |= row.key in self.rows
                    self._merge(row)
            self.dirty = duplicate
        except (csv.Error, UnicodeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Invalid daily summary file {self.path}: {exc}") from exc

    def observe(self, match: Match, source_time: float) -> None:
        day = datetime.fromtimestamp(source_time, UTC).date().isoformat()
        with self.lock:
            if self.closed:
                return
            for reference in match.references:
                name = " ".join(clean(match.reference_names.get(reference, "")).split())
                self._merge(SummaryRow(day, match.program, match.call, reference, name,
                                       station_band(match.call, match.frequency)[1], source_time,
                                       source_time, match.frequency, match.reference_qths.get(reference, "")))

    def _write(self) -> None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=self.path.parent,
                                             prefix="." + self.path.name + ".", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                writer = csv.writer(output)
                writer.writerow(self.fields)
                for key in sorted(self.rows):
                    writer.writerow(self.rows[key].values())
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            self.dirty, self.error = False, ""
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _flush_locked(self) -> None:
        if self.dirty and not self.closed:
            try:
                self._write()
            except OSError as exc:
                self.error = clean(exc)

    def flush(self) -> None:
        with self.lock:
            self._flush_locked()

    def close(self) -> None:
        with self.lock:
            if not self.closed:
                self._flush_locked()
                self.closed = True
                self.lock_file.close()


def get_summary(args: argparse.Namespace) -> DailySummary | None:
    if not hasattr(args, "_daily_summary"):
        args._daily_summary = DailySummary(args.summary_file) if args.summary_file is not None else None
    return args._daily_summary


class Poller:
    """Independent sources and shared RBN requests, with confirmations per identity."""

    def __init__(self, args: argparse.Namespace, small_batches: Event | None = None):
        self.args = args
        self.log = get_log(args)
        self.rbn = RbnClient(args.timeout, small_batches)
        if args.rbn_small_batches:
            self.rbn.small_batches.set()
        self.confirmed = {key: list(entry.match.receptions)
                          for key, entry in startup_entries(args)[0].items() if entry.match.receptions}
        self.source_spots: dict[str, list[ActivationSpot]] = {}
        self.retry_at: dict[str, float] = {}
        self.source_errors: dict[str, str] = {}

    def read_source(self, program: str) -> tuple[list[ActivationSpot], int, set[tuple]]:
        if program == "SOTA":
            try:
                with Path(self.args.sota_file).open("rb") as source:
                    data = source.read(8_000_001)
                if len(data) > 8_000_000:
                    raise ValueError("file exceeds 8 MB")
                payload = json.loads(data)
            except (OSError, ValueError, UnicodeError) as exc:
                raise SourceError(f"SOTA file: {clean(exc)}") from exc
        else:
            payload = get_json(POTA_URL if program == "POTA" else WWFF_URL, self.args.timeout)
        now = time.time()
        age = max_spot_age(self.args, program)
        return read_pota(payload, now, age) if program == "POTA" else read_program(payload, program, now, age)

    def fetch(self) -> Snapshot:
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
            if time.monotonic() < self.retry_at.get(program, 0):
                failed.add(program)
                errors.append(self.source_errors[program] + " (Retry-After)")
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
                self.retry_at[program] = time.monotonic() + getattr(exc, "retry_after", 0)
                spots.extend(self.source_spots.get(program, []))
                continue
            selected, skipped, stopped = data
            if self.log:
                self.log.record("SOURCE_READ", time.time(), detail=f"{program}: CW={len(selected)}, skipped={skipped}, inactive={len(stopped)}", debug=True)
            self.source_spots[program] = selected
            spots.extend(selected)
            inactive.update(stopped)
            if skipped:
                warnings.append(f"{program}: skipped {skipped} invalid entries")
        latest = {}
        now = time.time()
        for p in spots:
            if not 0 <= now - p.time <= max_spot_age(args, p.program):
                continue
            key = p.key
            if key not in latest or p.time >= latest[key].time:
                latest[key] = p
        pending = [p for key, p in latest.items() if key not in self.confirmed]
        received = []
        try:
            if time.monotonic() < self.retry_at.get("RBN", 0):
                errors.append(self.source_errors["RBN"] + " (Retry-After)")
            else:
                received = self.rbn.fetch(sorted({p.call for p in pending}), args.spotters, args.rbn_max_age)
                warnings.extend(self.rbn.warnings)
        except SourceError as exc:
            self.source_errors["RBN"] = "RBN: " + clean(exc)
            self.retry_at["RBN"] = time.monotonic() + exc.retry_after
            errors.append(self.source_errors["RBN"])
        now = time.time()
        for p in pending:
            hits = [r for r in received if r.call == p.call and r.receiver in args.spotters
                    and 0 <= now - r.time <= args.rbn_max_age
                    and station_band(r.call, r.frequency) == station_band(p.call, p.frequency)
                    and abs(r.frequency - p.frequency) <= args.tolerance + 1e-9]
            if hits:
                self.confirmed[p.key] = hits
            elif self.log:
                same_call = [r for r in received if r.call == p.call]
                same_receiver = [r for r in same_call if r.receiver in args.spotters]
                current = [r for r in same_receiver if 0 <= now - r.time <= args.rbn_max_age]
                same_band = [r for r in current if station_band(r.call, r.frequency) == station_band(p.call, p.frequency)]
                if not same_call:
                    reason = "no recent CW report in selected receiver stream"
                elif not same_receiver:
                    reason = "receiver not selected"
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
        # Odczyty z pamięci zachowują oryginalny czas odbioru. Nie są odświeżane
        # przez sam fakt ponownego pobrania POTA.
        cached = {r for key in latest for r in self.confirmed.get(key, [])}
        if self.log:
            for detail in self.rbn.request_log:
                self.log.record("RBN_QUERY", now, detail=detail, debug=True)
            self.log.record("RBN_FETCH", now, detail=self.rbn.status + " | " + " | ".join(warnings + errors), debug=True)
        return Snapshot(list(latest.values()), list(cached), warnings, now, inactive, failed, errors, self.rbn.status)


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
    updated_fields: set[int] = field(default_factory=set)
    spot_time: float = 0
    previous_bands: tuple[str, ...] = ()
    logged_active: bool = True
    restored: bool = False
    awaiting_source: bool = False
    first_seen_known: bool = True

    @property
    def last_heard(self) -> float | None:
        return max((r.time for r in self.match.receptions if r.time is not None), default=None)

    def active_match(self, now: float) -> Match | None:
        references = tuple(p for p in self.match.references if now <= self.spot_deadlines[p])
        # Świeżość RBN sprawdzamy przy pierwszym potwierdzeniu. Później
        # o aktywności decyduje ważność POTA, a czas odbioru pozostaje oryginalny.
        receptions = [r for r in self.match.receptions if r.time is None or r.time <= now]
        if self.eligible and not self.awaiting_source and references and receptions:
            return Match(self.match.call, references, self.match.frequency, receptions,
                         {p: name for p, name in self.match.reference_names.items() if p in references},
                         self.match.location, self.match.program,
                         {p: qth for p, qth in self.match.reference_qths.items() if p in references})
        return None


def latest_reception(match: Match) -> RbnSpot | None:
    return max(match.receptions, key=lambda r: r.time if r.time is not None else -math.inf, default=None)


def startup_entries(args: argparse.Namespace) -> tuple[dict[tuple, Entry], int]:
    """Replay event lifecycles and enrich them with the independent daily summary."""
    if hasattr(args, "_startup_entries"):
        return args._startup_entries
    entries, replayed_at, skipped = {}, {}, 0
    now = time.time()
    log, summary = get_log(args), get_summary(args)
    if not args.demo and log:
        for raw in log.records():
            try:
                if None in raw or any(value is None for value in raw.values()):
                    raise ValueError("wrong column count")
                event = raw["event"]
                if event not in {"NEW", "BAND", "UPDATE", "HISTORY", "INACTIVE", "ACTIVE"}:
                    continue
                program, call = raw["program"], raw["callsign"]
                if program not in PROGRAMS:
                    raise ValueError("invalid program")
                if program not in args.programs:
                    continue
                when, source_time = timestamp(raw["timestamp_utc"]), timestamp(raw["source_time_utc"])
                freq = frequency(raw["spot_khz"])
                if when > now or source_time > when:
                    continue
                if not CALL_PATTERN.fullmatch(call) or raw["band"] != band_for(freq):
                    raise ValueError("invalid callsign or band")
                pattern = r"[A-Z0-9]+/[A-Z0-9]+-\d+" if program == "SOTA" else r"[A-Z0-9]+-\d+"
                references = tuple(re.findall(pattern, raw["reference"]))
                if not references or "/".join(references) != raw["reference"]:
                    raise ValueError("invalid reference")
                qths = raw["qth"].split("/") if raw["qth"] else [""] * len(references)
                if len(qths) != len(references):
                    raise ValueError("mismatched reference QTH fields")
                qths = [qth.strip().upper() for qth in qths]
                for qth in qths:
                    if qth:
                        locator_coordinates(qth)
                columns = [raw[name].split("/") if raw[name] else []
                           for name in ("receivers", "rbn_khz", "snr_db", "wpm")]
                if len({len(values) for values in columns}) != 1:
                    raise ValueError("mismatched receiver fields")
                receptions = []
                for receiver, rbn_freq, snr, wpm in zip(*columns):
                    if not CALL_PATTERN.fullmatch(receiver):
                        raise ValueError("invalid receiver")
                    reception = RbnSpot(call, receiver, frequency(rbn_freq), None, int(snr), int(wpm))
                    if receiver in args.spotters:
                        receptions.append(reception)
                receptions.sort(key=lambda r: args.spotters.index(r.receiver))
                for reference, qth in zip(references, qths):
                    match = Match(call, (reference,), freq, receptions,
                                  location=locator_coordinates(qth) if qth else None, program=program,
                                  reference_qths={reference: qth} if qth else {})
                    key = match.key
                    if when < replayed_at.get(key, -math.inf):
                        continue
                    replayed_at[key] = when
                    old = entries.get(key)
                    if old is None or event in {"NEW", "BAND"}:
                        entry = Entry(match, {reference: source_time + max_spot_age(args, program)},
                                      0, 0, 0, created_at=when, archived=True, spot_time=source_time,
                                      restored=True, awaiting_source=True,
                                      first_seen_known=event in {"NEW", "BAND"})
                        entries[key] = entry
                    else:
                        entry = old
                        if source_time >= entry.spot_time:
                            entry.match, entry.spot_time = match, source_time
                            entry.spot_deadlines = {reference: source_time + max_spot_age(args, program)}
                    if event == "INACTIVE":
                        entry.eligible = False
                        entry.inactive_since = min(when, max(entry.spot_deadlines.values()))
                    elif event in {"NEW", "BAND", "UPDATE", "ACTIVE"}:
                        entry.eligible, entry.inactive_since = True, None
            except (ValueError, TypeError, OverflowError):
                skipped += 1
    if not args.demo and summary:
        for row in sorted(summary.rows.values(), key=lambda r: r.last_seen):
            if row.program not in args.programs or row.last_seen > now:
                continue
            match = Match(row.callsign, (row.reference,), row.frequency, [],
                          {row.reference: row.name} if row.name else {},
                          location=locator_coordinates(row.qth) if row.qth else None, program=row.program,
                          reference_qths={row.reference: row.qth} if row.qth else {})
            entry = entries.get(match.key)
            if entry is None:
                entry = Entry(match, {row.reference: row.last_seen + max_spot_age(args, row.program)},
                              0, 0, 0, created_at=row.first_seen, archived=True, spot_time=row.last_seen,
                              restored=True, awaiting_source=True, first_seen_known=False)
                entries[match.key] = entry
            else:
                if row.name and row.last_seen >= entry.spot_time:
                    entry.match.reference_names[row.reference] = row.name
                elif row.name and not entry.match.reference_names.get(row.reference):
                    entry.match.reference_names[row.reference] = row.name
                if row.qth and (row.last_seen >= entry.spot_time or not entry.match.reference_qths.get(row.reference)):
                    entry.match.reference_qths[row.reference] = row.qth
                    entry.match.location = locator_coordinates(row.qth)
                if row.last_seen > entry.spot_time:
                    entry.match.frequency, entry.spot_time = row.frequency, row.last_seen
                    entry.spot_deadlines = {row.reference: row.last_seen + max_spot_age(args, row.program)}
                    entry.eligible, entry.inactive_since = True, None
    retained = {}
    for key, entry in sorted(entries.items(), key=lambda item: item[1].spot_time):
        deadline = max(entry.spot_deadlines.values())
        if deadline < now and entry.inactive_since is None:
            entry.inactive_since = deadline
        expires = entry.inactive_since if entry.inactive_since is not None else deadline
        if now - expires > args.history_max_age:
            continue
        entry.rank = len(retained) + 1
        entry.logged_active = bool(entry.eligible and entry.match.receptions and now <= deadline)
        retained[key] = entry
    args._startup_entries = retained, skipped
    return args._startup_entries


class Monitor:
    """Stan ekranu, historia i ważność danych niezależne od cyklu pobierania."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.log = get_log(args)
        self.summary = get_summary(args)
        restored, self.restore_skipped = startup_entries(args)
        self.entries = {key: replace(entry) for key, entry in restored.items()}
        self.spots: list[ActivationSpot] = []
        self.updated_at: float | None = None
        self.error = ""
        self.warnings: list[str] = []
        self.loading = False
        self.next_poll_at: float | None = None  # Zegar monotoniczny, tak jak harmonogram wątku.
        self.column_layout = ColumnLayout()
        self.rank = len(self.entries)
        self.rbn_status = ""
        self.qth_warning_until = time.monotonic() + 30
        self.restore_count = len(self.entries)

    def qth_warning(self) -> str:
        if self.args.qth is None and time.monotonic() < self.qth_warning_until:
            return "WARNING: QTH not set; cannot estimate distance or azimuth. Use --qth LOCATOR."
        return ""

    def log_entry(self, event: str, entry: Entry, now: float, detail: str = "") -> None:
        if self.log:
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
        groups: dict[tuple, list[ActivationSpot]] = {}
        for p in current.values():
            groups.setdefault(p.key, []).append(p)
        new = []
        for key, spots in sorted(groups.items(), key=lambda item: (max(p.time for p in item[1]), item[0])):
            entry = self.entries.get(key)
            if entry is None and key not in fresh:
                continue
            if entry is not None and not entry.match.receptions and key not in fresh:
                continue  # A daily summary alone does not identify a confirming receiver.
            latest = max(spots, key=lambda p: p.time)
            if entry is not None and latest.time < entry.spot_time:
                continue  # Opóźniony odczyt API nie cofa ostatniej częstotliwości.
            heard = {}
            for match in ([entry.match] if entry else []) + ([fresh[key]] if key in fresh else []):
                for reception in match.receptions:
                    if (reception.receiver not in heard or (reception.time is not None
                            and (heard[reception.receiver].time is None
                                 or reception.time > heard[reception.receiver].time))):
                        heard[reception.receiver] = reception
            match = Match(latest.call, tuple(sorted({p.reference for p in spots})), latest.frequency,
                          [heard[r] for r in args.spotters if r in heard],
                          {p.reference: p.name or (entry.match.reference_names.get(p.reference, "") if entry else "")
                           for p in spots},
                          latest.location or (entry.match.location if entry else None), latest.program,
                          {p.reference: p.qth or (entry.match.reference_qths.get(p.reference, "") if entry else "")
                           for p in spots})
            deadlines = {p.reference: p.time + max_spot_age(args, p.program) for p in spots}
            if entry is None:
                self.rank += 1
                entry = Entry(match, deadlines, self.rank, now + 60, now + args.highlight_seconds,
                              created_at=now, spot_time=latest.time,
                              previous_bands=tuple(sorted({old_key[1] for old_key in self.entries
                                                           if old_key[0] == match.call and old_key[3] == match.program}))
                              if not any(old_key[:2] == key[:2] and old_key[3] == key[3] for old_key in self.entries) else ())
                self.entries[key] = entry
                self.log_entry("BAND" if entry.previous_bands else "NEW", entry, now,
                               "previous bands: " + "/".join(entry.previous_bands) if entry.previous_bands else "")
                new.append(match)
            else:
                old_match, old_time = entry.match, entry.spot_time
                changed_fields = set()
                if entry.match.frequency != match.frequency:
                    entry.previous_frequency = entry.match.frequency
                    changed_fields.add(3)
                previous_rbn, latest_rbn = latest_reception(entry.match), latest_reception(match)
                if previous_rbn and latest_rbn and previous_rbn.frequency != latest_rbn.frequency:
                    entry.previous_rbn_frequency = previous_rbn.frequency
                    changed_fields.add(8)
                if changed_fields:
                    entry.updated_fields = changed_fields
                    entry.updated_until = now + 60
                entry.match, entry.spot_deadlines, entry.eligible = match, deadlines, True
                entry.spot_time, entry.inactive_since = latest.time, None
                if old_match != match or old_time != latest.time:
                    self.log_entry("UPDATE", entry, now, update_description(entry, match, now) or "source report/metadata updated")
            if self.summary and match.program not in snapshot.failed_programs:
                if match.receptions:
                    self.summary.observe(match, latest.time)
        for key, entry in self.entries.items():
            if entry.match.program not in snapshot.failed_programs:
                entry.awaiting_source = False
            if key in snapshot.inactive and key not in groups:
                entry.eligible = False
                self.archive(entry, now)
                if entry.inactive_since is None:
                    entry.inactive_since = now
            elif key not in groups and entry.match.program not in snapshot.failed_programs:
                # Brak w jednym odczycie nie usuwa wpisu i nie kasuje terminu
                # ważności ostatniego POTA. W historii nadal może być ACTIVE.
                self.archive(entry, now)
        self.spots, self.updated_at = list(current.values()), snapshot.time
        self.source_error(" | ".join(snapshot.errors), now)
        self.warnings, self.loading = snapshot.warnings, False
        if snapshot.rbn_status:
            self.rbn_status = snapshot.rbn_status
        self.refresh_lifecycle(now)
        if self.summary:
            self.summary.flush()
        return list(reversed(new))

    def acknowledge(self, now: float) -> None:
        """Wycisz obecne wyróżnienia, zachowując wpisy, ważność i kolejność."""
        for entry in self.entries.values():
            entry.new_until = min(entry.new_until, now)
            entry.flash_until = min(entry.flash_until, now)
            entry.updated_until = min(entry.updated_until, now)

    def fail(self, message: str) -> None:
        # Błąd pobierania nie usuwa znanego potwierdzenia. Lokalny termin
        # ważności POTA nadal obowiązuje, a raport nad tabelą sygnalizuje błąd.
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

    def rows(self, now: float) -> tuple[list[tuple[Entry, Match]], list[tuple[Entry, Match]]]:
        self.refresh_lifecycle(now)
        active, history = [], []
        for entry in self.entries.values():
            match = entry.active_match(now)
            if match and not entry.archived:
                active.append((entry, match))
            elif match or (entry.awaiting_source and entry.inactive_since is None) or (entry.inactive_since is not None
                           and 0 <= now - entry.inactive_since <= self.args.history_max_age):
                history.append((entry, match or entry.match))
        active.sort(key=lambda row: -row[0].rank)
        history.sort(key=lambda row: (row[0].active_match(now) is None,
                                      -row[0].spot_time, row[1].call, row[1].key[1], row[1].key[2]))
        return active, history


def age_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def band_for(freq: float) -> str:
    return next((label for low, high, label in BANDS if low <= freq <= high), "—")


def station_band(call: str, freq: float) -> tuple[str, str]:
    band = band_for(freq)
    return call, band if band != "—" else f"{freq:g}kHz"


def activation_key(call: str, freq: float, park: str, program: str = "POTA") -> tuple[str, str, str, str]:
    return (*station_band(call, freq), park, program)


def reference_label(match: Match, name_length: int | None) -> str:
    labels = []
    for park in match.references:
        name = " ".join(clean(match.reference_names.get(park, "")).split())
        if name and name_length != 0:
            if name_length is not None and len(name) > name_length:
                name = name[:name_length - 1].rstrip() + "…"
            labels.append(f"{park} ({name})")
        else:
            labels.append(park)
        if match.reference_qths.get(park):
            labels[-1] += f" [{match.reference_qths[park]}]"
    return ", ".join(labels)


def row_values(entry: Entry, match: Match, now: float, history: bool = False,
               park_name_length: int | None = None, qth: tuple[float, float] | None = None,
               distance_unit: str = "km") -> list[str]:
    marker = ("UPD" if now < entry.updated_until else
              ("BND" if entry.previous_bands else ">>>") if not history and now < entry.new_until else "")
    status = "UNKNOWN" if entry.awaiting_source else "ACTIVE" if entry.active_match(now) else "INACTIVE"
    keywords = []
    if 0 <= now - entry.created_at < 60 and now < entry.new_until:
        keywords.append("NEW")
    if entry.previous_bands and now < entry.new_until:
        keywords.append("BND")
    if now < entry.updated_until:
        keywords.append("UPD")
    if entry.restored:
        keywords.append("RESTORED")
    comment = " ".join([status] + keywords)
    description = update_description(entry, match, now)
    if description:
        comment += " | " + description
    reception = latest_reception(match)
    rbn_frequency = (f"{reception.frequency:.1f}" if reception else "—")
    if match.receptions and all(r.time is None for r in match.receptions):
        rbn_frequency = " / ".join(dict.fromkeys(f"{r.frequency:.1f}" for r in match.receptions))
    return [marker, match.call,
            reference_label(match, park_name_length), f"{match.frequency:.1f}",
            " / ".join(s.receiver for s in match.receptions) or "—",
            " / ".join(str(s.snr) for s in match.receptions) or "—",
            " / ".join(str(s.wpm) for s in match.receptions) or "—",
            " / ".join(age_text(now - s.time) if s.time is not None else "—" for s in match.receptions) or "—",
            rbn_frequency,
            band_for(match.frequency), status, comment,
            age_text(now - entry.created_at) if entry.first_seen_known else "—", age_text(now - entry.spot_time),
            distance_text(qth, match.location, distance_unit), match.program,
            azimuth_text(qth, match.location)]


HEADERS = ["", "Activator", "Reference", "Spot kHz", "Receiver", "SNR dB", "WPM", "RBN age", "RBN kHz", "Band", "Status", "Comment",
           "First seen", "Last spot", "Distance", "Program", "Azimuth"]
TABLE_FIELDS = (15, 1, 9, 3, 8, 2, 4, 5, 6, 12, 13, 7, 14, 16, 11)
NUMERIC_FIELDS = frozenset((3, 5, 6, 7, 8, 12, 13, 14, 16))
COMMENT_LEGEND = ("NEW=new entry BND=new band UPD=kHz change RESTORED=from log | "
                  "status: ACTIVE/INACTIVE/UNKNOWN (awaiting source)")


def comment_legend(width: int) -> str:
    return COMMENT_LEGEND if len(COMMENT_LEGEND) < width else "NEW=new BND=band UPD=kHz"


class HighlightedText(str):
    """Tekst wiersza z zakresami znaków, które należy wyróżnić osobno."""

    def __new__(cls, text: str, highlights: list[tuple[int, int]],
                separators: list[tuple[int, int]] | None = None):
        result = super().__new__(cls, text)
        result.highlights = tuple(highlights)
        result.separators = tuple(separators or ())
        return result


def compact_values(values: list[str]) -> list[str]:
    if values == HEADERS:
        values = values.copy()
        values[5] = "dB"
    return [value.replace(" / ", "/") for value in values]


def cell_width(text: str) -> int:
    return sum(0 if unicodedata.combining(c) else
               2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def take_cells(text: str, width: int) -> str:
    used = 0
    for index, char in enumerate(text):
        size = cell_width(char)
        if used + size > width:
            return text[:index]
        used += size
    return text


def fit_column(text: str, width: int, right: bool = False) -> str:
    if cell_width(text) > width:
        text = take_cells(text, max(0, width - 1)) + "…"
    padding = " " * max(0, width - cell_width(text))
    return padding + text if right else text + padding


def wrap_cells(text: str, width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        if line and cell_width(line + " " + word) <= width:
            line += " " + word
            continue
        if line:
            lines.append(line)
            line = ""
        while cell_width(word) > width:
            chunk = take_cells(word, width)
            if not chunk:
                break
            lines.append(chunk)
            word = word[len(chunk):]
        line = word
    if line:
        lines.append(line)
    return lines


class ColumnLayout:
    """Szerokości pól rosną wraz z zawartością i resetują się po zmianie rozmiaru."""

    def __init__(self):
        self.size: tuple[int, int] | None = None
        self.widths: dict[int, int] = {}
        self.separator = " "

    def resize(self, size: tuple[int, int]) -> None:
        if self.size != size:
            self.size, self.widths = size, {}

    def choose(self, size: tuple[int, int], rows: list[list[str]], receivers: int,
               park_width: int, age_limit: float) -> list[tuple[int, int]]:
        self.resize(size)
        self.separator = " │ " if size[1] >= 120 else " "
        rows = [compact_values(row) for row in [HEADERS] + rows]
        for field in range(len(HEADERS)):
            if field == 2:
                continue  # Park dostaje miejsce pozostałe po czytelnych danych.
            needed = max(cell_width(row[field]) for row in rows)
            if field == 11:
                needed = max(cell_width(row[field].split(" | ", 1)[0]) for row in rows)
            if field == 0:
                needed = 3  # Znacznik nowych pozycji nie zmienia układu przy wygasaniu.
            elif field == 1:
                needed = max(cell_width(HEADERS[1]), max((cell_width(row[1]) + 1 for row in rows[1:]), default=0))
            elif field == 7:
                minute_digits = max(2, len(str(int(age_limit // 60))))
                needed = max(needed, receivers * (minute_digits + 3) + max(0, receivers - 1))
            self.widths[field] = max(self.widths.get(field, 0), needed)
        widths = dict(self.widths)
        widths[2] = max(len(HEADERS[2]), park_width)
        fields = list(TABLE_FIELDS)
        budget = max(0, size[1] - 1)

        def used() -> int:
            return sum(widths[f] for f in fields) + len(self.separator) * (len(fields) - 1)

        # Nowe liczniki przechodzą pod wpis, gdy zabraknie miejsca w tabeli.
        # Zachowaj oba źródła częstotliwości, pasmo i pełny wiek odbiorów.
        for optional in (16, 14, 12, 13, 2, 6, 5, 4):
            if used() <= budget:
                break
            fields.remove(optional)
        if used() > budget:
            fields.remove(11)  # Comment pokazujemy pod wierszem w bardzo małym oknie.
        if used() > budget:
            # W najmniejszych oknach wiek dostaje osobną linię pod wpisem.
            if widths[1] - (used() - budget) >= 6:
                widths[1] -= used() - budget
            else:
                fields.remove(7)
            if used() > budget:
                widths[1] = max(6, widths[1] - (used() - budget))
        if 11 in fields:
            target = max(cell_width(row[11]) for row in rows)
            widths[11] += min(max(0, target - widths[11]), max(0, budget - used()))
            self.widths[11] = max(self.widths[11], widths[11])
        if 2 in fields:
            park_target = max(cell_width(row[2]) for row in rows)
            widths[2] += min(max(0, park_target - widths[2]), max(0, budget - used()))
        return [(field, widths[field]) for field in fields]


def source_status(state: Monitor) -> str:
    if state.error:
        return "SOURCE ERROR: " + state.error + " | last known data"
    if state.loading:
        return "Fetching activation spots and RBN confirmations…"
    if state.warnings:
        return "WARNING: " + " | ".join(state.warnings)
    return f"HTTPS every {state.args.interval:g}s | {', '.join(state.args.spotters)} | reception ≠ QSO"


def refresh_status(state: Monitor) -> str:
    if state.loading:
        return "HTTPS: fetching…"
    if state.next_poll_at is None:
        return "HTTPS: waiting…"
    remaining = max(0, math.ceil(state.next_poll_at - time.monotonic()))
    minutes, seconds = divmod(remaining, 60)
    return f"HTTPS in {minutes:02d}:{seconds:02d}"


def reports(state: Monitor) -> list[str]:
    result = ["SOURCE ERROR: " + state.error + " | last known data"] if state.error else []
    if state.log and state.log.error:
        result.append("LOG ERROR: " + state.log.error)
    if state.summary and state.summary.error:
        result.append("SUMMARY ERROR: " + state.summary.error)
    warning = state.qth_warning()
    if warning:
        result.append(warning)
    if state.restore_count and time.monotonic() < state.qth_warning_until:
        pending = sum(entry.awaiting_source for entry in state.entries.values())
        result.append(f"RESTORED: {state.restore_count} entries from saved logs (HISTORY)."
                      + (f" Awaiting source data: {pending}." if pending else ""))
    if state.restore_skipped:
        result.append(f"WARNING: skipped {state.restore_skipped} invalid event log records during restore")
    return result + ["WARNING: " + clean(w) for w in state.warnings]


def new_banner(active: list[tuple[Entry, Match]], now: float) -> str:
    descriptions = [f"{m.call} {m.program} {','.join(m.references)} | "
                    + (f"NEW BAND: {'/'.join(e.previous_bands)} → {band_for(m.frequency)} | "
                       if e.previous_bands else "")
                    + f"Spot {m.frequency:.1f} kHz"
                    + (f", RBN {max(m.receptions, key=lambda r: r.time).frequency:.1f} kHz"
                       if m.frequency != max(m.receptions, key=lambda r: r.time).frequency else "")
                    + " | "
                    + ", ".join(f"{r.receiver} {r.snr} dB" for r in m.receptions)
                    for e, m in active if now < e.new_until]
    if not descriptions:
        return "NEW: —"
    title = "NEW ACTIVATION" if len(descriptions) == 1 else "NEW ACTIVATIONS"
    return f">>> {title} ({len(descriptions)}): " + "; ".join(descriptions) + " <<<"


def update_description(entry: Entry, match: Match, now: float) -> str:
    if now >= entry.updated_until:
        return ""
    fields = []
    if 3 in entry.updated_fields and entry.previous_frequency is not None:
        fields.append(f"Spot {entry.previous_frequency:.1f} → {match.frequency:.1f} kHz")
    if 8 in entry.updated_fields and entry.previous_rbn_frequency is not None:
        freq = latest_reception(match).frequency
        fields.append(f"RBN {entry.previous_rbn_frequency:.1f} → {freq:.1f} kHz")
    return ", ".join(fields)


def links_for(match: Match, args: argparse.Namespace, history: bool = False) -> list[str]:
    max_age = args.history_max_age if history else args.rbn_max_age
    params = urlencode({"spotted_call": match.call, "spotter_call": ",".join(args.spotters),
                        "max_age": f"{max_age / 60:g},minutes", "rows": 100})
    links = [f"  {match.call} RBN: {RBN_URL}/main.php?{params}"]
    for reference in match.references:
        if match.program == "POTA":
            url = f"https://pota.app/#/park/{quote(reference, safe='')}"
        elif match.program == "SOTA":
            url = f"https://www.sota.org.uk/Summit/{quote(reference, safe='/')}"
        else:
            url = "https://spots.wwff.co/search?" + urlencode({"q": reference})
        links.append(f"  {reference} {match.program}: {url}")
    return links


def limits_text(args: argparse.Namespace) -> str:
    return " | ".join(f"{p} ≤ {max_spot_age(args, p):g}s" for p in args.programs)


def render(state: Monitor, now: float) -> str:
    """Pełna migawka dla --once, --no-clear i przekierowanego wyjścia."""
    args = state.args
    active, history = state.rows(now)
    clock = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    count = sum(0 <= now - p.time <= max_spot_age(args, p.program) for p in state.spots)
    lines = [f"{'/'.join(args.programs)} CW → RBN | {clock}" + (" | DEMO — FICTIONAL DATA" if args.demo else ""),
             new_banner(active, now)]
    for entry, match in active:
        if now < entry.flash_until:
            lines.append(f"ALERT: {match.call} — heard by "
                         + ", ".join(s.receiver for s in match.receptions))
    lines.extend(reports(state))
    for title, items, historical in (("ACTIVE / NEW", active, False), ("HISTORY", history, True)):
        lines.extend(["", title])
        rows = [HEADERS] + [row_values(e, m, now, historical, args.park_name_length,
                                      args.qth, args.distance_unit) for e, m in items]
        rows = [[row[i] for i in TABLE_FIELDS] for row in rows]
        if items:
            widths = [max(cell_width(row[i]) for row in rows) for i in range(len(TABLE_FIELDS))]
            for index, row in enumerate(rows):
                lines.append(" | ".join(fit_column(value, w, index > 0 and field in NUMERIC_FIELDS)
                                        for field, value, w in zip(TABLE_FIELDS, row, widths)))
                if index == 0:
                    lines.append("-+-".join("-" * w for w in widths))
        else:
            lines.append("No entries." if historical else "No new entries.")
        for entry, match in items:
            if args.links:
                lines.extend(links_for(match, args, historical))
    confirmed_active = sum(e.active_match(now) is not None for e, _ in active + history)
    lines.extend(["", f"Fresh CW spots: {count} | confirmed: {confirmed_active} | history: {len(history)}",
                  f"{limits_text(args)} | RBN start ≤ {args.rbn_max_age:g}s | "
                  f"history ≤ {args.history_max_age:g}s | tolerance ±{args.tolerance:g} kHz"])
    if not state.error and not state.warnings:
        lines.append(source_status(state))
    if state.rbn_status:
        lines.append(state.rbn_status)
    lines.append(COMMENT_LEGEND)
    lines.append("First seen: time since detection; Last spot: age of the latest program report.")
    lines.append(f"Distance: great-circle distance from QTH {args.qth_label} to the reference location ({args.distance_unit})."
                 if args.qth is not None else "Distance: set --qth or CW_ACTIVATION_QTH to use your home location.")
    lines.append("Azimuth: initial bearing from QTH, clockwise from true north (000° N, 090° E).")
    lines.append("SNR, WPM and RBN age follow receiver order. ACTIVE entries may also appear in history.")
    return "\n".join(lines)


def screen_frame(state: Monitor, now: float, height: int, width: int) -> list[tuple[str, str]]:
    """Szerokości dopasowane do danych i pierwszeństwo najnowszych wpisów."""
    active, history = state.rows(now)
    args = state.args
    notices = reports(state)
    state.column_layout.resize((height, width))
    if height < 8 or width < 40:
        lines = [("CW → RBN — enlarge terminal (min. 40×8)", "title"),
                 (notices[0] if notices else source_status(state), "error" if notices else "normal"),
                 (new_banner(active, now), "banner")]
        return lines[:height]
    updated = (datetime.fromtimestamp(state.updated_at, UTC).strftime("%H:%M:%S UTC")
               if state.updated_at is not None else "waiting")
    footer = [(f"{refresh_status(state)} | updated: {updated}", "muted")]
    if height >= 10:
        footer.append((f"{limits_text(args)} | RBN start ≤ {args.rbn_max_age:g}s | history ≤ {args.history_max_age:g}s | ±{args.tolerance:g} kHz", "muted"))
    if height >= 14 and state.rbn_status:
        footer.append((state.rbn_status, "muted"))
    if height >= 10 or not notices:
        footer.append((comment_legend(width), "muted"))
    table_bottom = height - len(footer) - 1  # Ostatni wiersz: ukryte wpisy i klawisze.
    lines = [("/".join(args.programs) + " CW → RBN | " + ", ".join(args.spotters)
              + f" | QTH: {args.qth_label}", "title"),
             (new_banner(active, now), "banner" if any(now < e.new_until for e, _ in active) else "muted")]
    # Raporty mają pierwszeństwo, lecz nawet ich nadmiar nie zasłania całej tabeli.
    max_reports = max(1, height - 9)
    lines.extend((text, "error") for text in notices[:max_reports])
    if len(notices) > max_reports:
        lines[-1] = (lines[-1][0] + f" (+{len(notices) - max_reports} reports)", "error")
    items = active + history
    columns = state.column_layout.choose(
        (height, width), [row_values(e, m, now, park_name_length=args.park_name_length,
                                     qth=args.qth, distance_unit=args.distance_unit) for e, m in items],
        max((len(m.receptions) for _, m in items), default=1),
        max((len(",".join(m.references)) for _, m in items), default=4),
        max(*(max_spot_age(args, p) for p in args.programs), args.history_max_age))

    def format_row(values: list[str], changed_fields: set[int] | None = None) -> str:
        header = values == HEADERS
        values = compact_values(values)
        parts, highlights, separators, offset = [], [], [], 0
        separator = state.column_layout.separator
        for index, (i, w) in enumerate(columns):
            if index:
                parts.append(separator)
                if "│" in separator:
                    begin = offset + separator.index("│")
                    separators.append((begin, begin + 1))
                offset += len(separator)
            part = fit_column(values[i], w, not header and i in NUMERIC_FIELDS)
            if changed_fields and i in changed_fields:
                highlights.append((offset + len(part) - len(part.lstrip()), offset + len(part.rstrip())))
            parts.append(part)
            offset += len(part)
        return HighlightedText("".join(parts), highlights, separators)

    shown_active = shown_history = 0
    for title, items, historical in (("ACTIVE / NEW", active, False), ("HISTORY", history, True)):
        available = table_bottom - len(lines)
        if available < 3:
            break
        lines.append((f"{title} ({len(items)})", "history" if historical else "title"))
        lines.append((format_row(HEADERS), "table_header"))
        if height >= 10 and available >= 4:
            joiner = "─┼─" if "│" in state.column_layout.separator else "─"
            lines.append((joiner.join("─" * w for _, w in columns), "separator"))
        count = 0
        if not items:
            lines.append(("No entries.", "muted"))
        park_width = next((w for field, w in columns if field == 2), 0)
        age_in_table = any(field == 7 for field, _ in columns)
        comment_in_table = any(field == 11 for field, _ in columns)
        comment_width = next((w for field, w in columns if field == 11), 0)
        comment_start = sum(w + len(state.column_layout.separator) for field, w in columns if field != 11)
        for entry, match in items:
            if len(lines) >= table_bottom:
                break
            style = ("new" if not historical and now < entry.flash_until else
                     "history" if entry.active_match(now) is None else "normal")
            values = row_values(entry, match, now, historical, args.park_name_length,
                                args.qth, args.distance_unit)
            park_details = values[2]
            separate_name = not park_width or bool(args.park_name_length != 0 and match.reference_names
                                                   and cell_width(park_details) > park_width)
            if separate_name:
                values[2] = ",".join(match.references)
            full_comment = values[11]
            comment_lines = textwrap.wrap(full_comment, width=comment_width, break_on_hyphens=False) if comment_width else []
            if comment_lines:
                values[11] = comment_lines[0]
            changed = entry.updated_fields if now < entry.updated_until else set()
            lines.append((format_row(values, changed), style))
            for continuation in comment_lines[1:]:
                if len(lines) < table_bottom:
                    lines.append((" " * comment_start + continuation, "muted"))
            details = []
            detail_style = "history" if historical else "muted"
            if not age_in_table:
                details.append(("RBN age: " + values[7].replace(" / ", "/"), detail_style))
            if not comment_in_table:
                details.extend((text, detail_style) for text in textwrap.wrap(
                    "Comment: " + full_comment, width=max(1, width - 5), break_on_hyphens=False))
            if separate_name:
                details.extend((text, style) for text in wrap_cells(park_details, max(2, width - 5)))
            measurements = [HEADERS[field] + ": " + values[field].replace(" / ", "/")
                            for field in (4, 5, 6) if not any(i == field for i, _ in columns)]
            if measurements:
                details.extend((text, detail_style) for text in wrap_cells(" | ".join(measurements), max(2, width - 5)))
            age_details = [HEADERS[field] + ": " + values[field] for field in (12, 13, 14, 16)
                           if not any(i == field for i, _ in columns)]
            packed = ""
            for detail in age_details:
                combined = packed + " | " + detail if packed else detail
                if packed and cell_width(combined) > width - 5:
                    details.extend((text, detail_style) for text in wrap_cells(packed, max(2, width - 5)))
                    packed = detail
                else:
                    packed = combined
            if packed:
                details.extend((text, detail_style) for text in wrap_cells(packed, max(2, width - 5)))
            for detail, line_style in details:
                if len(lines) < table_bottom:
                    lines.append(("    " + detail, line_style))
            count += 1
        if historical:
            shown_history = count
        else:
            shown_active = count
    if args.links:
        for _, match in (active + history):
            for link in links_for(match, args, not any(match is m for _, m in active)):
                if len(lines) < table_bottom:
                    lines.append((link, "muted"))
    while len(lines) < table_bottom:
        lines.append(("", "normal"))
    lines.extend(footer)
    hidden = f"{len(active) - shown_active}/{len(history) - shown_history}"
    controls = "Space: acknowledge | b: batches | q: quit" if width >= 60 else "b:split | q:quit"
    lines.append((f"Hidden A/H: {hidden} | {controls}", "muted"))
    return lines


def demo_data(now: float, receivers: list[str], programs: list[str] | None = None) -> tuple[list[ActivationSpot], list[RbnSpot]]:
    pota = [ActivationSpot("IU1PZC", "IT-0759", 14052.9, now - 80, "Parco Naturale del Monte Tre Denti", (45.0, 7.3)),
            ActivationSpot("YO5BDL", "RO-0155", 14054.0, now - 120, "Simion Bărnuțiu Central Park", (46.77, 23.58))]
    rbn = [RbnSpot("IU1PZC", receivers[0], 14052.9, now - 42, 18, 23),
           RbnSpot("YO5BDL", receivers[0], 14054.0, now - 35, 12, 21)]
    if len(receivers) > 1:
        rbn.append(RbnSpot("YO5BDL", receivers[1], 14054.1, now - 58, 9, 21))
    if programs is not None:
        pota.extend([ActivationSpot("SP9DEMO/P", "SP/BZ-001", 7032, now - 120, "Demo summit", (49.57, 19.53), "SOTA"),
                     ActivationSpot("SP8DEMO/P", "SPFF-0001", 14044, now - 480, "Demo nature reserve", (50.2, 23.1), "WWFF")])
        rbn.extend([RbnSpot("SP9DEMO/P", receivers[0], 7032, now - 50, 14, 22),
                    RbnSpot("SP8DEMO/P", receivers[0], 14044, now - 40, 16, 24)])
        pota = [p for p in pota if p.program in programs]
    return pota, rbn


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spotters", nargs="+", default=["SP5GQ", "SQ5J"],
                        help="receiver callsigns (default: SP5GQ SQ5J)")
    parser.add_argument("--programs", nargs="+", type=str.upper, choices=PROGRAMS,
                        help="programs to monitor (default: POTA WWFF, plus SOTA with --sota-file)")
    parser.add_argument("--sota-file", type=Path,
                        help="local SOTA spots JSON file; no SOTA API connection is made")
    parser.add_argument("--qth", default=os.environ.get("CW_ACTIVATION_QTH"),
                        help="home Maidenhead locator or latitude,longitude (also CW_ACTIVATION_QTH); unset disables distance")
    parser.add_argument("--distance-unit", choices=("km", "mi"), default="km",
                        help="distance units (default: km)")
    parser.add_argument("--interval", type=float, default=60, help="polling interval in seconds, minimum 60")
    parser.add_argument("--pota-max-age", type=float, default=300, help="maximum POTA spot age in seconds")
    parser.add_argument("--sota-max-age", type=float, default=600, help="maximum SOTA spot age in seconds (600)")
    parser.add_argument("--wwff-max-age", type=float, default=900, help="maximum WWFF spot age in seconds (900)")
    parser.add_argument("--rbn-max-age", type=float, default=300,
                        help="maximum age of the initial RBN confirmation in seconds")
    parser.add_argument("--history-max-age", type=float, default=1800,
                        help="history retention after program spot activity ends, in seconds (1800)")
    parser.add_argument("--new-max-age", type=float, default=300,
                        help="time a new program/callsign/band/reference stays in ACTIVE / NEW, in seconds (300)")
    parser.add_argument("--highlight-seconds", type=float, default=30,
                        help="new row highlight duration in seconds (30)")
    parser.add_argument("--reference-name-length", "--park-name-length", dest="park_name_length", type=int, default=None,
                        help="reference name limit; full name by default, 0 hides names (legacy alias: --park-name-length)")
    parser.add_argument("--tolerance", type=float, default=0.5, help="frequency tolerance in kHz (0.5)")
    parser.add_argument("--timeout", type=float, default=20, help="timeout for each HTTPS request in seconds")
    parser.add_argument("--rbn-small-batches", action="store_true",
                        help="allow targeted callsign/receiver batches if the RBN stream exceeds 100 rows")
    parser.add_argument("--log-file", type=Path, help="append activation events to a UTF-8 CSV file")
    parser.add_argument("--summary-file", type=Path,
                        help="update a daily CSV with full names, first/last source spots and last frequency; reload on restart")
    parser.add_argument("--log-level", choices=("events", "debug"), default="events",
                        help="CSV log detail (default: events); debug also records fetches and rejected matches")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--demo", action="store_true", help="show fictional data once, without network access")
    parser.add_argument("--links", action="store_true", help="show RBN and program reference links")
    parser.add_argument("--bell", action="store_true", help="ring the terminal bell for new confirmations")
    parser.add_argument("--no-clear", action="store_true", help="plain text: keep successive snapshots instead of using curses")
    args = parser.parse_args(argv)
    if args.log_file is not None and args.summary_file is not None:
        same_path = args.log_file.resolve() == args.summary_file.resolve()
        if args.log_file.exists() and args.summary_file.exists():
            same_path |= os.path.samefile(args.log_file, args.summary_file)
        if same_path:
            parser.error("--log-file and --summary-file must be different files")
    args.programs = list(dict.fromkeys(args.programs or (list(PROGRAMS) if args.demo else
                                    ["POTA", "WWFF"] + (["SOTA"] if args.sota_file else []))))
    if "SOTA" in args.programs and args.sota_file is None and not args.demo:
        parser.error("--programs SOTA requires --sota-file; direct SOTA API access is not enabled")
    args.qth_label = clean(args.qth).strip() if args.qth is not None else "not set"
    if args.qth is not None:
        try:
            args.qth = qth_coordinates(args.qth_label)
        except ValueError as exc:
            parser.error(f"--qth: {exc}")
    for name in ("interval", "pota_max_age", "sota_max_age", "wwff_max_age", "rbn_max_age", "history_max_age", "new_max_age", "highlight_seconds", "timeout", "tolerance"):
        value = getattr(args, name)
        if not math.isfinite(value) or value < 0 or (name != "tolerance" and value == 0):
            parser.error(f"--{name.replace('_', '-')}: expected a finite positive number")
    if args.interval < 60:
        parser.error("--interval must be at least 60 seconds")
    if args.park_name_length is not None and args.park_name_length < 0:
        parser.error("--reference-name-length / --park-name-length must not be negative")
    args.spotters = list(dict.fromkeys(receiver_call(r) for r in args.spotters))
    if not all(CALL_PATTERN.fullmatch(r) for r in args.spotters):
        parser.error("--spotters: invalid receiver callsign")
    return args


def run_plain(args: argparse.Namespace) -> int:
    poller, state = Poller(args), Monitor(args)
    warning = state.qth_warning()
    if state.restore_count or state.restore_skipped:
        print(render(state, time.time()), flush=True)
        state.qth_warning_until = 0
    elif warning:
        print(warning, flush=True)  # Show before the first network request.
        state.qth_warning_until = 0  # Append-only output needs only one startup notice.
    interactive = sys.stdout.isatty()
    while True:
        started = time.monotonic()
        retry_after, failed = 0.0, False
        try:
            if args.demo:
                now = time.time()
                old = now - 600
                if "POTA" in args.programs:
                    state.apply(Snapshot([ActivationSpot("SP9DEMO/P", "PL-0001", 7028, old)],
                                         [RbnSpot("SP9DEMO/P", args.spotters[0], 7028, old, 15, 20)], [], old), old)
                pota, rbn = demo_data(now, args.spotters, args.programs)
                snapshot = Snapshot(pota, rbn, [], now)
            else:
                snapshot = poller.fetch()
            now = time.time()
            new = state.apply(snapshot, now)
            failed = bool(snapshot.errors)
            if new and args.bell and interactive:
                print("\a", end="", flush=True)
        except SourceError as exc:
            failed, retry_after = True, exc.retry_after
            state.fail(str(exc))
        print(render(state, time.time()), flush=True)
        if args.once or args.demo:
            return 1 if failed else 0
        delay = max(0, args.interval - (time.monotonic() - started), retry_after)
        print(f"\nNext cycle in {math.ceil(delay)} s. Exit: Ctrl+C.\n", flush=True)
        time.sleep(delay)


def poll_worker(args: argparse.Namespace, events: Queue, stop: Event, small_batches: Event | None = None) -> None:
    """Jeden wątek sieciowy; wszystkie operacje terminala pozostają w głównym."""
    poller = Poller(args, small_batches)
    while not stop.is_set():
        started = time.monotonic()
        retry_after = 0.0
        events.put(("loading", None))
        try:
            events.put(("snapshot", poller.fetch()))
        except SourceError as exc:
            retry_after = exc.retry_after
            events.put(("error", str(exc)))
        except Exception as exc:
            events.put(("error", f"Fetch error ({type(exc).__name__}): {clean(exc)}"))
        finished = time.monotonic()
        delay = max(0, args.interval - (finished - started), retry_after)
        events.put(("scheduled", finished + delay))
        if stop.wait(delay):
            return


def run_terminal(screen, args: argparse.Namespace) -> int:
    from terminal_display import CursesDisplay

    display, state = CursesDisplay(screen), Monitor(args)
    events, stop, small_batches = Queue(), Event(), Event()
    worker = Thread(target=poll_worker, args=(args, events, stop, small_batches), daemon=True)
    worker.start()
    try:
        while True:
            now = time.time()
            while True:
                try:
                    kind, data = events.get_nowait()
                except Empty:
                    break
                if kind == "loading":
                    state.loading = True
                    state.next_poll_at = None
                elif kind == "error":
                    state.fail(data)
                elif kind == "scheduled":
                    state.next_poll_at = data
                elif kind == "snapshot":
                    new = state.apply(data, now)
                    if new and args.bell:
                        display.bell()
            height, width = screen.getmaxyx()
            display.draw(screen_frame(state, now, height, width))
            key = screen.getch()  # timeout pozwala odświeżyć wiek i ważność lokalnie.
            if key == ord(" "):
                state.acknowledge(time.time())
            if key in (ord("b"), ord("B")) and not small_batches.is_set():
                small_batches.set()
                if state.log:
                    state.log.record("RBN_BATCHES_ENABLED", time.time(), detail="keyboard acknowledgement")
            if key in (ord("q"), ord("Q"), 3):
                return 0
    finally:
        # Nie czekaj na zakończenie trwającego żądania HTTPS przy wychodzeniu.
        stop.set()


def run(args: argparse.Namespace) -> int:
    fullscreen = (not (args.once or args.demo or args.no_clear)
                  and sys.stdout.isatty() and sys.stdin.isatty()
                  and os.environ.get("TERM", "dumb") != "dumb")
    if fullscreen:
        try:
            import curses
        except ImportError:
            print("curses is unavailable; starting plain text mode (--no-clear).", file=sys.stderr)
        else:
            return curses.wrapper(run_terminal, args)
    return run_plain(args)


def main(argv: list[str] | None = None) -> int:
    args = None
    try:
        args = arguments(argv)
        get_log(args)  # Fail clearly before networking if the log cannot be opened.
        get_summary(args)
        return run(args)
    except BrokenPipeError:
        return 0
    except (OSError, ValueError) as exc:
        print("Configuration/file error: " + clean(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
        return 0
    finally:
        if args is not None and getattr(args, "_daily_summary", None):
            args._daily_summary.close()
        if args is not None and getattr(args, "_event_log", None):
            args._event_log.close()


if __name__ == "__main__":
    sys.exit(main())
