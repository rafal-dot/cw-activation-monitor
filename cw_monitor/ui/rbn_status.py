"""Render polling parameters separately from newly detected CW traffic."""

import math

from ..rbn_status import RbnMode, RbnStatus
from .layout import cell_width, wrap_cells


def batch_text(status: RbnStatus) -> str:
    rows = f"{status.batch.received}/{status.batch.requested} rows" if status.batch else None
    if status.mode == RbnMode.WAITING:
        return "RBN: waiting"
    if status.mode == RbnMode.IDLE:
        return "RBN: " + (f"{rows} · idle" if rows else "idle (no unconfirmed activations)")
    cadence = f" · every {status.interval:g}s" if status.interval is not None else ""
    return "RBN: " + (rows or "waiting") + cadence


def traffic_text(status: RbnStatus) -> str | None:
    if status.last is None:
        return "CW: Collecting…" if status.mode in (RbnMode.STREAM, RbnMode.BATCHES) else None
    last = f"last {status.last.count} / {math.ceil(status.last.seconds)}s"
    average = f"~{status.average_per_minute:.1f}/min" if status.average_per_minute is not None else None
    if not status.complete:
        return f"CW: {last} (partial) · " + (f"last avg {average}" if average else "Collecting…")
    return f"CW: {last} · avg " + (average or "Collecting…")


def polling_lines(status: RbnStatus, refresh: str, width: int | None = None, *, demo: bool = False) -> list[str]:
    packet = "RBN: offline demo" if demo else batch_text(status)
    traffic = None if demo else traffic_text(status)
    lines = [refresh, packet]
    if width is not None:
        width = max(1, width)
        combined = f"{refresh} | {packet}"
        if cell_width(combined) <= width:
            lines = [combined]
    if traffic:
        lines.append(traffic)
    return [part for line in lines for part in (wrap_cells(line, width) if width is not None else [line])]
