"""Terminal-cell widths, column allocation and styled text spans."""

from __future__ import annotations

import unicodedata

from ..values import age_text
from .table import HEADERS, TABLE_FIELDS


class HighlightedText(str):
    """A row with character spans for independently styled cells."""

    def __new__(cls, text: str, highlights: list[tuple[int, int]],
                separators: list[tuple[int, int]] | None = None,
                band_spans: list[tuple[int, int, str]] | None = None,
                color_spans: list[tuple[int, int, str]] | None = None):
        result = super().__new__(cls, text)
        result.highlights = tuple(highlights)
        result.separators = tuple(separators or ())
        result.band_spans = tuple(band_spans or ())
        result.color_spans = tuple(color_spans or ())
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
    """Grow column widths with content and reset them after terminal resizing."""

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
                continue  # Reference gets the space remaining after essential data.
            needed = max(cell_width(row[field]) for row in rows)
            if field == 11:
                needed = max(cell_width(row[field].split(" | ", 1)[0]) for row in rows)
            if field == 0:
                needed = 3  # Expiring markers must not shift the layout.
            elif field == 1:
                needed = max(cell_width(HEADERS[1]), max((cell_width(row[1]) + 1 for row in rows[1:]), default=0))
            elif field == 7:
                needed = max(needed, receivers * len(age_text(age_limit)) + max(0, receivers - 1))
            self.widths[field] = max(self.widths.get(field, 0), needed)
        widths = dict(self.widths)
        widths[2] = max(len(HEADERS[2]), park_width)
        fields = list(TABLE_FIELDS)
        budget = max(0, size[1] - 1)

        def used() -> int:
            return sum(widths[f] for f in fields) + len(self.separator) * (len(fields) - 1)

        # Move optional measurements below the row when space runs out.
        # Preserve both frequencies, band and full reception ages.
        for optional in (16, 14, 12, 13, 2, 6, 5, 4):
            if used() <= budget:
                break
            fields.remove(optional)
        if used() > budget:
            fields.remove(11)  # Show Comment below the row in very narrow windows.
        if used() > budget:
            # In the smallest windows, reception ages use a separate detail line.
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
