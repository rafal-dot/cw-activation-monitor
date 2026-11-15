"""Inclusive SNR rules, with complete per-band overrides and immutable persisted drafts."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .config import BANDS, COMMON_BANDS

# Keep frequency recognition broader than the user-facing band selectors so
# historical entries and preferences from older releases remain readable.
RECOGNIZED_BANDS = tuple(label for _, _, label in BANDS)
BAND_LABELS = COMMON_BANDS


def validate_bounds(minimum, maximum):
    for name, value in (("Minimum", minimum), ("Maximum", maximum)):
        if value is not None and (type(value) is not int or not -100 <= value <= 100):
            raise ValueError(f"{name} SNR must be an integer from -100 to 100 dB, or off")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError("Minimum SNR must not exceed maximum SNR")


@dataclass(frozen=True)
class BandSnrRule:
    enabled: bool = False
    minimum: int | None = None
    maximum: int | None = None

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("Band SNR enabled must be true or false")
        validate_bounds(self.minimum, self.maximum)


def checked_rules(rules):
    if not isinstance(rules, Mapping) or any(band not in COMMON_BANDS or not isinstance(rule, BandSnrRule) for band, rule in rules.items()):
        raise ValueError("Invalid band SNR settings")
    return MappingProxyType(dict(rules))


def resolve_snr(minimum, maximum, rules, band):
    rule = rules.get(band) if rules and band in COMMON_BANDS else None
    return (rule.minimum, rule.maximum) if rule is not None and rule.enabled else (minimum, maximum)


def bounds_label(minimum, maximum):
    if minimum is not None and maximum is not None:
        return f"{minimum}–{maximum} dB"
    if minimum is not None:
        return f"≥{minimum} dB"
    if maximum is not None:
        return f"≤{maximum} dB"
    return "Off"


def rules_from_json(value):
    if not isinstance(value, dict):
        raise ValueError("Band SNR settings must be an object")
    result = {}
    for band, rule in value.items():
        if band not in RECOGNIZED_BANDS:
            raise ValueError("Unknown band in SNR settings")
        if not isinstance(rule, dict) or set(rule) - {"enabled", "minimum", "maximum"}:
            raise ValueError("Invalid band SNR rule")
        if "enabled" not in rule:
            raise ValueError("Band SNR enabled flag is missing")
        result[band] = BandSnrRule(rule["enabled"], rule.get("minimum"), rule.get("maximum"))
    # Older releases offered every recognized band; retain other preferences
    # while removing overrides that can no longer be edited.
    return checked_rules({band: rule for band, rule in result.items() if band in COMMON_BANDS})


def rules_to_json(rules):
    return {band: {"enabled": rule.enabled, "minimum": rule.minimum, "maximum": rule.maximum}
            for band, rule in sorted(rules.items())}
