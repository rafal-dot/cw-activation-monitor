"""Conservative base callsign parsing shared by Friends matching paths."""

import re

from .config import CALL_PATTERN

_BASE = re.compile(r"[A-Z0-9]*[0-9][A-Z]+(?:-\d+)?")
_PREFIX = re.compile(r"(?:[A-Z]{1,2}(?:[0-9]{1,2}[A-Z]?)?|[0-9][A-Z]{1,2}[0-9]?[A-Z]?)")
_SUFFIXES = frozenset(("P", "M", "MM", "AM", "QRP"))
_DISTRICT = re.compile(r"[0-9]{1,2}")


def base_callsign(raw: str) -> str | None:
    full = raw.strip().upper()
    if not CALL_PATTERN.fullmatch(full):
        return None
    parts = full.split("/")
    while len(parts) > 1 and (parts[-1] in _SUFFIXES or _DISTRICT.fullmatch(parts[-1])):
        parts.pop()
    bases = [part for part in parts if _BASE.fullmatch(part)]
    if len(bases) != 1:
        return None
    prefixes = [part for part in parts if part != bases[0]]
    if len(prefixes) > 1 or any(not _PREFIX.fullmatch(part) for part in prefixes):
        return None
    return bases[0]
