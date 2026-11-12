"""Maidenhead locators, great-circle distances and bearings."""

from __future__ import annotations

import math
import re


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
