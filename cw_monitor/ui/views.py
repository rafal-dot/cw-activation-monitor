"""Plain-text snapshots and full-screen terminal frames."""

from __future__ import annotations

import argparse
from datetime import datetime
import math
import textwrap
import time
from typing import TYPE_CHECKING
from urllib.parse import quote, urlencode

from ..config import RBN_URL, UTC
from ..models import ChangedField, Entry, Match
from ..values import band_for, clean, max_spot_age
from .table import COMMENT_LEGEND, FREQUENCY_COLUMNS, HEADERS, NUMERIC_FIELDS, TABLE_FIELDS, comment_legend, row_values
from .layout import ColumnLayout, HighlightedText, cell_width, compact_values, fit_column, wrap_cells
from .colors import ansi_text, measurement_spans, value_spans
if TYPE_CHECKING:
    from ..monitor import Monitor


def source_status(state: Monitor) -> str:
    if state.error:
        return "SOURCE ERROR: " + state.error + " | last known data"
    if state.loading:
        return "Fetching updates from sources or RBN…"
    if state.warnings:
        return "WARNING: " + " | ".join(state.warnings)
    return f"Sources: HTTPS every {state.args.interval:g}s | {', '.join(state.args.spotters)} | reception ≠ QSO"


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


def render(state: Monitor, now: float, color: bool = False) -> str:
    """Build a complete snapshot for one-shot, plain or redirected output."""
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
                parts = [fit_column(value, w, index > 0 and field in NUMERIC_FIELDS)
                         for field, value, w in zip(TABLE_FIELDS, row, widths)]
                text = " | ".join(parts)
                if color and index:
                    entry = items[index - 1][0]
                    spans, highlights, offset = [], [], 0
                    for field, part in zip(TABLE_FIELDS, parts):
                        spans.extend((offset + a, offset + b, c) for a, b, c in
                                     value_spans(field, part, args.snr_thresholds, args.wpm_thresholds))
                        if now < entry.updated_until and FREQUENCY_COLUMNS.get(field) in entry.updated_fields:
                            highlights.append((offset + len(part) - len(part.lstrip()), offset + len(part.rstrip())))
                        offset += len(part) + 3
                    style = ("new" if not historical and now < entry.flash_until else
                             "history" if entry.active_match(now) is None else "normal")
                    text = ansi_text(HighlightedText(text, highlights, color_spans=spans), style)
                lines.append(text)
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


def screen_frame(state: Monitor, now: float, height: int, width: int, layout: ColumnLayout | None = None) -> list[tuple[str, str]]:
    """Allocate columns to content and give newest entries screen priority."""
    if layout is None:
        layout = ColumnLayout()
    active, history = state.rows(now)
    args = state.args
    notices = reports(state)
    layout.resize((height, width))
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
    table_bottom = height - len(footer) - 1  # Reserve the last line for controls.
    lines = [("/".join(args.programs) + " CW → RBN | " + ", ".join(args.spotters)
              + f" | QTH: {args.qth_label}", "title"),
             (new_banner(active, now), "banner" if any(now < e.new_until for e, _ in active) else "muted")]
    # Prioritize reports while preserving room for the table.
    max_reports = max(1, height - 9)
    lines.extend((text, "error") for text in notices[:max_reports])
    if len(notices) > max_reports:
        lines[-1] = (lines[-1][0] + f" (+{len(notices) - max_reports} reports)", "error")
    items = active + history
    columns = layout.choose(
        (height, width), [row_values(e, m, now, park_name_length=args.park_name_length,
                                     qth=args.qth, distance_unit=args.distance_unit) for e, m in items],
        max((len(m.receptions) for _, m in items), default=1),
        max((len(",".join(m.references)) for _, m in items), default=4),
        max(*(max_spot_age(args, p) for p in args.programs), args.history_max_age))

    def format_row(values: list[str], changed_fields: set[ChangedField] | None = None) -> str:
        header = values == HEADERS
        values = compact_values(values)
        parts, highlights, separators, band_spans, colors, offset = [], [], [], [], [], 0
        separator = layout.separator
        for index, (i, w) in enumerate(columns):
            if index:
                parts.append(separator)
                if "│" in separator:
                    begin = offset + separator.index("│")
                    separators.append((begin, begin + 1))
                offset += len(separator)
            part = fit_column(values[i], w, not header and i in NUMERIC_FIELDS)
            if changed_fields and FREQUENCY_COLUMNS.get(i) in changed_fields:
                highlights.append((offset + len(part) - len(part.lstrip()), offset + len(part.rstrip())))
            if not header and i == 9:
                band_spans.append((offset + len(part) - len(part.lstrip()), offset + len(part.rstrip()), values[i]))
            if not header:
                colors.extend((offset + a, offset + b, c) for a, b, c in
                              value_spans(i, part, args.snr_thresholds, args.wpm_thresholds))
            parts.append(part)
            offset += len(part)
        return HighlightedText("".join(parts), highlights, separators, band_spans, colors)

    shown_active = shown_history = 0
    for title, items, historical in (("ACTIVE / NEW", active, False), ("HISTORY", history, True)):
        available = table_bottom - len(lines)
        if available < 3:
            break
        lines.append((f"{title} ({len(items)})", "history" if historical else "title"))
        lines.append((format_row(HEADERS), "table_header"))
        if height >= 10 and available >= 4:
            joiner = "─┼─" if "│" in layout.separator else "─"
            lines.append((joiner.join("─" * w for _, w in columns), "separator"))
        count = 0
        if not items:
            lines.append(("No entries.", "muted"))
        park_width = next((w for field, w in columns if field == 2), 0)
        age_in_table = any(field == 7 for field, _ in columns)
        comment_in_table = any(field == 11 for field, _ in columns)
        comment_width = next((w for field, w in columns if field == 11), 0)
        comment_start = sum(w + len(layout.separator) for field, w in columns if field != 11)
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
                age_detail = "RBN age: " + values[7].replace(" / ", "/")
                if cell_width(age_detail) > width - 5:
                    age_detail = "RBN age: " + values[7]
                details.extend((text, detail_style) for text in wrap_cells(age_detail, max(2, width - 5)))
            if not comment_in_table:
                details.extend((text, detail_style) for text in textwrap.wrap(
                    "Comment: " + full_comment, width=max(1, width - 5), break_on_hyphens=False))
            if separate_name:
                details.extend((text, style) for text in wrap_cells(park_details, max(2, width - 5)))
            measurements = [HEADERS[field] + ": " + values[field].replace(" / ", "/")
                            for field in (4, 5, 6) if not any(i == field for i, _ in columns)]
            if measurements:
                for text in wrap_cells(" | ".join(measurements), max(2, width - 5)):
                    spans = measurement_spans(text, args.snr_thresholds, args.wpm_thresholds)
                    details.append((HighlightedText(text, [], color_spans=spans), style))
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
                    colors = [(a + 4, b + 4, c) for a, b, c in getattr(detail, "color_spans", ())]
                    lines.append((HighlightedText("    " + detail, [], color_spans=colors), line_style))
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
    if width >= 60:
        controls = "Space: acknowledge | b: batches" + (" ON" if state.batch_mode else "") + " | q: quit"
    else:
        controls = "b:ON | q:quit" if state.batch_mode else "b:split | q:quit"
    lines.append((f"Hidden A/H: {hidden} | {controls}", "muted"))
    return lines
