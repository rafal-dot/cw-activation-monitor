"""Table values and presentation labels without terminal I/O."""

from __future__ import annotations

from ..geo import azimuth_text, distance_text
from ..models import ChangedField, Entry, Match, latest_reception, update_description
from ..values import age_text, band_for, clean


HEADERS = ["", "Activator", "Reference", "Spot kHz", "Receiver", "SNR dB", "WPM", "RBN age", "RBN kHz", "Band", "Status", "Comment",
           "First seen", "Last spot", "Distance", "Program", "Azimuth"]


TABLE_FIELDS = (15, 1, 9, 3, 8, 5, 6, 2, 4, 12, 13, 7, 14, 16, 11)


NUMERIC_FIELDS = frozenset((3, 5, 6, 7, 8, 12, 13, 14, 16))
FREQUENCY_COLUMNS = {3: ChangedField.SPOT_FREQUENCY, 8: ChangedField.RBN_FREQUENCY}


COMMENT_LEGEND = ("NEW=new entry BND=new band UPD=kHz change RESTORED=from log | "
                  "status: ACTIVE/INACTIVE/UNKNOWN (awaiting source)")


def comment_legend(width: int) -> str:
    return COMMENT_LEGEND if len(COMMENT_LEGEND) < width else "NEW=new BND=band UPD=kHz"


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
