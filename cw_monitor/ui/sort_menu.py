"""Non-blocking sort menu; polling, expiry and keyboard input keep running."""

import curses

from ..sorting import SORT_FIELDS, SortOrder


class SortMenu:
    def __init__(self, order: SortOrder):
        self.order = order
        self.closed = False

    def handle(self, key: int) -> SortOrder | None:
        fields = list(SORT_FIELDS)
        index = fields.index(self.order.field)
        if key in (27, ord("s"), ord("S")):
            self.closed = True
        elif key in (10, 13, curses.KEY_ENTER):
            self.closed = True
            return self.order
        elif key in (curses.KEY_UP, curses.KEY_DOWN) or ord("1") <= key < ord("1") + len(fields):
            index = (index + (-1 if key == curses.KEY_UP else 1)) % len(fields) if key in (curses.KEY_UP, curses.KEY_DOWN) else key - ord("1")
            self.order = SortOrder.resolve(fields[index], None)
        elif key in (curses.KEY_LEFT, curses.KEY_RIGHT, ord("d"), ord("D")):
            direction = "desc" if self.order.direction == "asc" else "asc"
            self.order = SortOrder(self.order.field, direction)
        return None

    def overlay(self, frame: list[tuple[str, str]], height: int, width: int) -> list[tuple[str, str]]:
        if height < 8 or width < 40:
            return frame
        lines = [("Sort activations", "title")]
        fields = list(SORT_FIELDS.items())
        capacity = min(len(fields), height - 3)
        index = list(SORT_FIELDS).index(self.order.field)
        start = max(0, min(index - capacity // 2, len(fields) - capacity))
        lines.extend((f"{i} [{'x' if field == self.order.field else ' '}] {label}",
                      "table_header" if field == self.order.field else "normal")
                     for i, (field, label) in enumerate(fields[start:start + capacity], start + 1))
        lines.extend([(f"Direction: {self.order.direction_label}", "normal"),
                      ("Enter=apply Esc=cancel d=direction", "muted")])
        start = max(0, (height - len(lines)) // 2)
        indent = " " * min(4, max(0, (width - 35) // 2))
        result = list(frame)
        result[start:start + len(lines)] = [(indent + text, style) for text, style in lines]
        return result
